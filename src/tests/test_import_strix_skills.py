from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import import_strix_skills as importer


@dataclass
class StrixImportFixture:
    import_root: Path
    manifest_path: Path
    source: Path
    target: Path
    manifest: dict[str, Any]
    entry: dict[str, Any]

    @property
    def destination(self) -> Path:
        return self.target / "strix-coordination-root-agent" / "SKILL.md"


@pytest.fixture()
def strix_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StrixImportFixture:
    import_root = tmp_path / "imported-skills" / "strix"
    manifest_path = import_root / "MANIFEST.json"
    source = import_root / "skills" / "coordination" / "root_agent.md"
    target = tmp_path / "skills"
    entry: dict[str, Any] = {
        "category": "coordination",
        "name": "root-agent",
        "source_path": "skills/coordination/root_agent.md",
    }
    manifest: dict[str, Any] = {
        "upstream": "https://github.com/usestrix/strix",
        "upstream_revision": "0123456789abcdef0123456789abcdef01234567",
        "license": "Apache-2.0",
        "entries": [entry],
    }

    source.parent.mkdir(parents=True)
    source.write_text("# Root agent\n", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(importer, "IMPORT_ROOT", import_root)
    monkeypatch.setattr(importer, "MANIFEST_PATH", manifest_path)

    return StrixImportFixture(
        import_root=import_root,
        manifest_path=manifest_path,
        source=source,
        target=target,
        manifest=manifest,
        entry=entry,
    )


def _run_cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["import_strix_skills.py", *args])
    importer.main()


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def test_dry_run_reports_preview_without_writing(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = {
        path.relative_to(strix_import.import_root): path.read_bytes()
        for path in strix_import.import_root.rglob("*")
        if path.is_file()
    }

    _run_cli(monkeypatch, "--dry-run", "--target", str(strix_import.target))

    after = {
        path.relative_to(strix_import.import_root): path.read_bytes()
        for path in strix_import.import_root.rglob("*")
        if path.is_file()
    }
    output = capsys.readouterr().out
    assert after == before
    assert not strix_import.target.exists()
    assert "[NEW]" in output
    assert "Mode: dry-run" in output
    assert "new/updated: 1" in output


def test_install_writes_current_attribution_and_reports_new(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.source.write_text(
        "<!-- strix-import: stale attribution -->\n# Root agent\n",
        encoding="utf-8",
    )

    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    assert strix_import.destination.read_text(encoding="utf-8") == (
        "<!-- strix-import: upstream=https://github.com/usestrix/strix "
        "rev=0123456789ab license=Apache-2.0 category=coordination -->\n"
        "# Root agent\n"
    )
    output = capsys.readouterr().out
    assert "[NEW]" in output
    assert "Mode: install" in output
    assert "Next steps:" in output


def test_reinstall_is_idempotent_and_does_not_rewrite(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))
    capsys.readouterr()

    def unexpected_write(*_args: object, **_kwargs: object) -> int:
        pytest.fail("idempotent reinstall attempted a filesystem write")

    monkeypatch.setattr(Path, "write_text", unexpected_write)
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    output = capsys.readouterr().out
    assert "[   ]" in output
    assert "new/updated: 0" in output
    assert "unchanged: 1" in output


def test_install_rechecks_unchanged_destination_before_reporting(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))
    capsys.readouterr()
    original_write = importer._write_prepared_entry

    def mutate_then_write(prepared: importer.PreparedEntry) -> tuple[bool, bool]:
        prepared.destination.write_text("tampered after preflight\n", encoding="utf-8")
        return original_write(prepared)

    monkeypatch.setattr(importer, "_write_prepared_entry", mutate_then_write)
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    output = capsys.readouterr().out
    assert "[UPD]" in output
    assert "new/updated: 1" in output
    assert "unchanged: 0" in output
    assert strix_import.destination.read_text(encoding="utf-8").endswith("# Root agent\n")


def test_unchanged_destination_swap_is_rejected(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))
    prepared = importer._preflight_manifest(strix_import.manifest, strix_import.target)[0]
    assert prepared.changed is False
    outside = strix_import.target.parent / "outside-unchanged.md"
    outside.write_text("sentinel\n", encoding="utf-8")
    strix_import.destination.unlink()
    _symlink_or_skip(strix_import.destination, outside)

    with pytest.raises(ValueError, match="must not be a symlink|changed after preflight"):
        importer._write_prepared_entry(prepared)

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative destination reads"
)
def test_unchanged_destination_swap_during_final_read_is_rejected(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))
    prepared = importer._preflight_manifest(strix_import.manifest, strix_import.target)[0]
    outside = strix_import.target.parent / "outside-final-read.md"
    outside.write_text("sentinel\n", encoding="utf-8")
    original_read = importer._read_destination_at

    def read_then_swap(parent_fd: int, current: importer.PreparedEntry) -> str | None:
        content = original_read(parent_fd, current)
        current.destination.unlink()
        _symlink_or_skip(current.destination, outside)
        return content

    monkeypatch.setattr(importer, "_read_destination_at", read_then_swap)

    with pytest.raises(ValueError, match="must not be a symlink|changed after preflight"):
        importer._write_prepared_entry(prepared)

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_changed_source_updates_existing_install(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))
    capsys.readouterr()
    strix_import.source.write_text("# Updated root agent\n", encoding="utf-8")

    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    output = capsys.readouterr().out
    assert "[UPD]" in output
    assert strix_import.destination.read_text(encoding="utf-8").endswith("# Updated root agent\n")


def test_direct_deploy_creates_a_missing_target(strix_import: StrixImportFixture) -> None:
    destination, changed = importer.deploy_entry(
        strix_import.entry,
        strix_import.manifest,
        strix_import.target,
        dry_run=False,
    )

    assert changed is True
    assert destination == strix_import.destination
    assert destination.read_text(encoding="utf-8").endswith("# Root agent\n")


def test_checked_path_fallback_installs_and_updates_atomically(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    guard_depth = 0

    @contextmanager
    def guarded_paths(_target: Path, guarded: Path, *, create_missing: bool = True) -> Any:
        nonlocal guard_depth
        if create_missing:
            guarded.mkdir(parents=True, exist_ok=True)
        guard_depth += 1
        try:
            yield
        finally:
            guard_depth -= 1

    real_replace = os.replace

    def guarded_replace(source: Path | str, destination: Path | str) -> None:
        assert guard_depth > 0
        real_replace(source, destination)

    monkeypatch.setattr(importer, "_supports_directory_fds", lambda: False)
    monkeypatch.setattr(importer, "_supports_windows_path_guards", lambda: True)
    monkeypatch.setattr(importer, "_guard_windows_directories", guarded_paths)
    monkeypatch.setattr(importer.os, "replace", guarded_replace)

    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))
    capsys.readouterr()
    strix_import.source.write_text("# Updated through fallback\n", encoding="utf-8")
    _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    output = capsys.readouterr().out
    assert "[UPD]" in output
    assert strix_import.destination.read_text(encoding="utf-8").endswith(
        "# Updated through fallback\n"
    )
    assert not list(strix_import.destination.parent.glob(".SKILL.md.*"))


def test_install_fails_closed_without_safe_filesystem_primitives(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "_supports_directory_fds", lambda: False)
    monkeypatch.setattr(importer, "_supports_windows_path_guards", lambda: False)

    with pytest.raises(RuntimeError, match="secure source read unavailable"):
        importer.deploy_entry(
            strix_import.entry,
            strix_import.manifest,
            strix_import.target,
            dry_run=False,
        )

    assert not strix_import.target.exists()


def test_windows_guard_context_pins_parents_and_creates_skill_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "skills"
    skill_dir = target / "strix-coordination-root-agent"
    opened: list[Path] = []
    closed: list[int] = []

    def open_guard(path: Path) -> int:
        opened.append(path)
        return len(opened)

    monkeypatch.setattr(importer, "_open_windows_directory_guard", open_guard)
    monkeypatch.setattr(importer, "_close_windows_directory_guard", closed.append)

    with importer._guard_windows_directories(target, skill_dir):
        assert skill_dir.is_dir()

    assert opened == importer._windows_guard_paths(target, skill_dir)
    assert closed == list(reversed(range(1, len(opened) + 1)))


@pytest.mark.parametrize("category", ["../escape", "Coordination", "coordination\n", "", None])
def test_category_rejects_unsafe_manifest_values(
    strix_import: StrixImportFixture, category: object
) -> None:
    entry = {**strix_import.entry, "category": category}

    with pytest.raises(ValueError, match="category:"):
        importer.deploy_entry(entry, strix_import.manifest, strix_import.target, dry_run=True)

    assert not strix_import.target.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("upstream", "https://example.test -->\ninjected"),
        ("upstream_revision", "012345\r\ninjected"),
        ("license", "Apache-2.0 -->"),
    ],
)
def test_unsafe_attribution_values_are_rejected(
    strix_import: StrixImportFixture,
    field: str,
    value: str,
) -> None:
    manifest = {**strix_import.manifest, field: value}

    with pytest.raises(ValueError, match=f"manifest.{field}: unsafe attribution value"):
        importer.deploy_entry(
            strix_import.entry,
            manifest,
            strix_import.target,
            dry_run=True,
        )

    assert not strix_import.target.exists()


@pytest.mark.parametrize(
    "name", ["../escape", r"..\escape", "/absolute", r"\absolute", "", "...", None]
)
def test_name_rejects_traversal_or_empty_values(
    strix_import: StrixImportFixture, name: object
) -> None:
    entry = {**strix_import.entry, "name": name}

    with pytest.raises(ValueError, match="name:"):
        importer.deploy_entry(entry, strix_import.manifest, strix_import.target, dry_run=True)

    assert not strix_import.target.exists()


def test_name_is_slugified_to_one_destination_component(
    strix_import: StrixImportFixture,
) -> None:
    entry = {**strix_import.entry, "name": "Nested/Escape (Fast)"}

    destination, changed = importer.deploy_entry(
        entry, strix_import.manifest, strix_import.target, dry_run=True
    )

    assert changed is True
    assert destination == (
        strix_import.target / "strix-coordination-nested-escape-fast" / "SKILL.md"
    )
    assert not strix_import.target.exists()


@pytest.mark.parametrize("source_path", ["../outside.md", "/tmp/outside.md"])
def test_source_path_rejects_lexical_traversal(
    strix_import: StrixImportFixture, source_path: str
) -> None:
    entry = {**strix_import.entry, "source_path": source_path}

    with pytest.raises(ValueError, match="source_path:.*(traversal|outside)"):
        importer.deploy_entry(entry, strix_import.manifest, strix_import.target, dry_run=True)

    assert not strix_import.target.exists()


def test_source_path_rejects_symlink_escape(strix_import: StrixImportFixture) -> None:
    outside = strix_import.import_root.parent / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    link = strix_import.import_root / "skills" / "escape.md"
    _symlink_or_skip(link, outside)
    entry = {**strix_import.entry, "source_path": "skills/escape.md"}

    with pytest.raises(ValueError, match="source_path:.*outside import root"):
        importer.deploy_entry(entry, strix_import.manifest, strix_import.target, dry_run=True)

    assert not strix_import.target.exists()


def test_source_path_accepts_symlink_within_import_root(
    strix_import: StrixImportFixture,
) -> None:
    link = strix_import.import_root / "skills" / "linked.md"
    _symlink_or_skip(link, strix_import.source)
    entry = {**strix_import.entry, "source_path": "skills/linked.md"}

    destination, changed = importer.deploy_entry(
        entry, strix_import.manifest, strix_import.target, dry_run=True
    )

    assert changed is True
    assert destination == strix_import.destination
    assert not strix_import.target.exists()


def test_source_parent_swap_after_resolution_is_rejected(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = strix_import.import_root.parent / "outside-source"
    outside.mkdir()
    (outside / strix_import.source.name).write_text("malicious\n", encoding="utf-8")
    source_parent = strix_import.source.parent
    displaced = strix_import.import_root / "displaced-coordination"
    original_resolve = importer._resolve_within
    swapped = False

    def resolve_then_swap(root: Path, candidate_rel: str, *, field: str) -> Path:
        nonlocal swapped
        resolved = original_resolve(root, candidate_rel, field=field)
        if field == "source_path" and not swapped:
            source_parent.rename(displaced)
            _symlink_or_skip(source_parent, outside, target_is_directory=True)
            swapped = True
        return resolved

    monkeypatch.setattr(importer, "_resolve_within", resolve_then_swap)

    with pytest.raises(ValueError, match="source path changed|must be a real directory"):
        importer.deploy_entry(
            strix_import.entry,
            strix_import.manifest,
            strix_import.target,
            dry_run=False,
        )

    assert swapped is True
    assert not strix_import.target.exists()


def test_missing_source_has_actionable_error(strix_import: StrixImportFixture) -> None:
    entry = {**strix_import.entry, "source_path": "skills/missing.md"}

    with pytest.raises(FileNotFoundError, match="Source skill missing:.*missing.md"):
        importer.deploy_entry(entry, strix_import.manifest, strix_import.target, dry_run=True)


def test_destination_rejects_skill_directory_symlink_escape(
    strix_import: StrixImportFixture,
) -> None:
    strix_import.target.mkdir()
    outside = strix_import.target.parent / "outside-skill"
    outside.mkdir()
    skill_dir = strix_import.destination.parent
    _symlink_or_skip(skill_dir, outside, target_is_directory=True)

    with pytest.raises(ValueError, match="skill dir .*outside target_dir"):
        importer.deploy_entry(
            strix_import.entry,
            strix_import.manifest,
            strix_import.target,
            dry_run=False,
        )

    assert not (outside / "SKILL.md").exists()


def test_destination_rejects_skill_file_symlink_escape(
    strix_import: StrixImportFixture,
) -> None:
    strix_import.destination.parent.mkdir(parents=True)
    outside = strix_import.target.parent / "outside.md"
    outside.write_text("sentinel\n", encoding="utf-8")
    _symlink_or_skip(strix_import.destination, outside)

    with pytest.raises(ValueError, match="destination .*outside target_dir"):
        importer.deploy_entry(
            strix_import.entry,
            strix_import.manifest,
            strix_import.target,
            dry_run=False,
        )

    assert outside.read_text(encoding="utf-8") == "sentinel\n"


def test_install_rejects_hard_linked_destination_without_changing_outside(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.destination.parent.mkdir(parents=True)
    outside = strix_import.target.parent / "outside-hard-link.md"
    outside.write_text("sentinel\n", encoding="utf-8")
    try:
        os.link(outside, strix_import.destination)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "hard-linked" in error
    assert "Traceback" not in error
    assert outside.read_text(encoding="utf-8") == "sentinel\n"
    assert strix_import.destination.read_text(encoding="utf-8") == "sentinel\n"


def test_install_rejects_parent_swapped_to_outside_symlink(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outside = strix_import.target.parent / "outside-swapped-parent"
    outside.mkdir()
    original_write = importer._write_prepared_entry

    def swap_parent_then_write(prepared: importer.PreparedEntry) -> None:
        prepared.target_root.mkdir(parents=True, exist_ok=True)
        _symlink_or_skip(prepared.destination.parent, outside, target_is_directory=True)
        original_write(prepared)

    monkeypatch.setattr(importer, "_write_prepared_entry", swap_parent_then_write)

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "changed after preflight" in error or "symlink" in error
    assert "Traceback" not in error
    assert not (outside / "SKILL.md").exists()


def test_cli_requires_an_explicit_mode(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--target", str(strix_import.target))

    assert exc_info.value.code == 2
    assert "Pass either --install or --dry-run" in capsys.readouterr().err
    assert not strix_import.target.exists()


def test_cli_rejects_conflicting_modes_without_writing(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_cli(
            monkeypatch,
            "--install",
            "--dry-run",
            "--target",
            str(strix_import.target),
        )

    assert exc_info.value.code == 2
    assert "Pass only one of --install or --dry-run" in capsys.readouterr().err
    assert not strix_import.target.exists()


def test_missing_manifest_reports_path_and_recovery_command(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.manifest_path.unlink()

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--dry-run", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"Manifest not found: {strix_import.manifest_path}" in error
    assert "Run: python imported-skills/strix/build_manifest.py" in error
    assert not strix_import.target.exists()


def test_source_symlink_loop_is_a_concise_exit_one_error(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop = strix_import.source.parent / "loop.md"
    _symlink_or_skip(loop, loop)
    entry = {**strix_import.entry, "source_path": "skills/coordination/loop.md"}
    strix_import.manifest["entries"] = [entry]
    strix_import.manifest_path.write_text(json.dumps(strix_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--dry-run", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "could not be resolved" in error
    assert "Traceback" not in error
    assert not strix_import.target.exists()


def test_duplicate_destination_slugs_are_rejected_before_install(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    duplicate = {
        **strix_import.entry,
        "name": "Root Agent",
    }
    strix_import.manifest["entries"] = [strix_import.entry, duplicate]
    strix_import.manifest_path.write_text(json.dumps(strix_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "entry 2 ('Root Agent')" in error
    assert "duplicate destination slug 'strix-coordination-root-agent'" in error
    assert "entry 1 ('root-agent')" in error
    assert "Traceback" not in error
    assert not strix_import.target.exists()


def test_canonical_destination_aliases_are_rejected_before_install(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.target.mkdir()
    shared = strix_import.target / "shared"
    shared.mkdir()
    first_dir = strix_import.target / "strix-coordination-root-agent"
    second_dir = strix_import.target / "strix-coordination-second-agent"
    _symlink_or_skip(first_dir, shared, target_is_directory=True)
    _symlink_or_skip(second_dir, shared, target_is_directory=True)
    second = {**strix_import.entry, "name": "second-agent"}
    strix_import.manifest["entries"] = [strix_import.entry, second]
    strix_import.manifest_path.write_text(json.dumps(strix_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "duplicate canonical destination" in error
    assert "entry 1 ('root-agent')" in error
    assert "Traceback" not in error
    assert not (shared / "SKILL.md").exists()


def test_destination_inode_aliases_are_rejected_before_install(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = strix_import.destination
    second = strix_import.target / "strix-coordination-second-agent" / "SKILL.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("sentinel\n", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    second_entry = {**strix_import.entry, "name": "second-agent"}
    strix_import.manifest["entries"] = [strix_import.entry, second_entry]
    strix_import.manifest_path.write_text(json.dumps(strix_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "duplicate destination inode" in error
    assert "entry 1 ('root-agent')" in error
    assert "Traceback" not in error
    assert first.read_text(encoding="utf-8") == "sentinel\n"
    assert second.read_text(encoding="utf-8") == "sentinel\n"


def test_invalid_later_entry_does_not_partially_install(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = {
        **strix_import.entry,
        "name": "broken-agent",
        "category": "../escape",
    }
    strix_import.manifest["entries"] = [strix_import.entry, invalid]
    strix_import.manifest_path.write_text(json.dumps(strix_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "entry 2 ('broken-agent'): category:" in error
    assert "Traceback" not in error
    assert not strix_import.target.exists()


def test_malformed_manifest_json_is_a_concise_exit_one_error(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.manifest_path.write_text('{"entries": [', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"Invalid manifest JSON: {strix_import.manifest_path}" in error
    assert "line 1 column" in error
    assert "Traceback" not in error
    assert not strix_import.target.exists()


def test_non_object_manifest_is_a_concise_exit_one_error(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.manifest_path.write_text("[]", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"Invalid manifest: {strix_import.manifest_path}: expected a JSON object" in error
    assert "Traceback" not in error
    assert not strix_import.target.exists()


def test_invalid_utf8_manifest_is_a_concise_exit_one_error(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.manifest_path.write_bytes(b"\xff\xfe")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"Unable to read manifest: {strix_import.manifest_path}" in error
    assert "Traceback" not in error
    assert not strix_import.target.exists()


def test_file_as_target_is_a_concise_exit_one_error(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    strix_import.target.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "Error:" in error
    assert "Traceback" not in error
    assert strix_import.target.read_text(encoding="utf-8") == "sentinel\n"


def test_write_failure_is_a_concise_exit_one_error(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def deny_write(_prepared: importer.PreparedEntry) -> None:
        raise PermissionError("write denied")

    monkeypatch.setattr(importer, "_write_prepared_entry", deny_write)

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "Error: write denied" in error
    assert "Traceback" not in error


def test_missing_source_is_a_concise_error_naming_the_entry(
    strix_import: StrixImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = {
        **strix_import.entry,
        "name": "missing-agent",
        "source_path": "skills/missing.md",
    }
    strix_import.manifest["entries"] = [missing]
    strix_import.manifest_path.write_text(json.dumps(strix_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_cli(monkeypatch, "--install", "--target", str(strix_import.target))

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "entry 1 ('missing-agent'): Source skill missing:" in error
    assert "Traceback" not in error
    assert not strix_import.target.exists()
