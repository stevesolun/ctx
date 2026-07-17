from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

import import_mattpocock_skills as importer


@dataclass(frozen=True)
class ImportTree:
    import_root: Path
    manifest_path: Path
    target: Path
    source_skill: Path
    source_body: str
    manifest: dict[str, Any]
    entry: dict[str, Any]


@pytest.fixture
def import_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportTree:
    import_root = tmp_path / "imported-skills" / "mattpocock"
    source_dir = import_root / "fixture-skill"
    source_dir.mkdir(parents=True)

    source_body = "# Fixture skill\n\nFixture instructions.\n"
    source_skill = source_dir / "SKILL.md"
    source_skill.write_text(source_body, encoding="utf-8")
    (source_dir / "NOTICE.txt").write_text("support notice\n", encoding="utf-8")
    (source_dir / "assets").mkdir()
    (source_dir / "assets" / "helper.bin").write_bytes(b"\x00fixture-support\xff")

    entry: dict[str, Any] = {
        "slug": "fixture-skill",
        "source_path": "fixture-skill/SKILL.md",
        "support_files": ["NOTICE.txt", "assets/helper.bin"],
    }
    manifest: dict[str, Any] = {
        "upstream": "https://example.test/mattpocock/skills",
        "upstream_revision": "0123456789abcdef0123456789abcdef01234567",
        "license": "MIT",
        "entries": [entry],
    }
    manifest_path = import_root / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(importer, "IMPORT_ROOT", import_root)
    monkeypatch.setattr(importer, "MANIFEST_PATH", manifest_path)

    return ImportTree(
        import_root=import_root,
        manifest_path=manifest_path,
        target=tmp_path / "target-skills",
        source_skill=source_skill,
        source_body=source_body,
        manifest=manifest,
        entry=entry,
    )


def _snapshot_files(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _hardlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.hardlink_to(target)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")


def _enable_checked_path_fallback(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_guard: Callable[[Path], None] | None = None,
) -> None:
    monkeypatch.setattr(importer, "_supports_directory_fds", lambda: False)
    if importer.os.name == "nt" and on_guard is None:
        return

    next_handle = 0

    def open_guard(path: Path) -> int:
        nonlocal next_handle
        if on_guard is not None:
            on_guard(path)
        next_handle += 1
        return next_handle

    monkeypatch.setattr(importer, "_supports_windows_path_guards", lambda: True)
    monkeypatch.setattr(importer, "_open_windows_directory_guard", open_guard)
    monkeypatch.setattr(importer, "_close_windows_directory_guard", lambda _handle: None)


def _transaction_artifacts(root: Path) -> list[Path]:
    suffixes = (".tmp", ".rollback", ".recovery", ".rejected")
    return [path for path in root.rglob("*") if path.name.endswith(suffixes)]


def _create_fallback_destination_parents(import_tree: ImportTree) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    (skill_dir / "assets").mkdir(parents=True)


def test_dry_run_reports_changes_without_writing(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("existing skill\n", encoding="utf-8")
    (skill_dir / "assets" / "helper.bin").write_bytes(b"existing support")
    before = _snapshot_files(import_tree.target)

    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )
    importer.main()

    output = capsys.readouterr().out
    assert "[UPD]" in output
    assert "Mode: dry-run" in output
    assert "Entries: 1  new: 0  updated: 1  unchanged: 0" in output
    assert _snapshot_files(import_tree.target) == before
    assert not (skill_dir / "NOTICE.txt").exists()


def test_absent_target_dry_run_reports_new_without_writing(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    importer.main()

    output = capsys.readouterr().out
    assert "[NEW]" in output
    assert "Entries: 1  new: 1  updated: 0  unchanged: 0" in output
    assert not import_tree.target.exists()


def test_public_deploy_entry_result_remains_a_three_tuple(import_tree: ImportTree) -> None:
    result = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=True,
    )

    assert len(result) == 3


def test_install_copies_attribution_and_support_files(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_header = "<!-- mattpocock-import: upstream=old rev=old license=old -->\n"
    import_tree.source_skill.write_text(old_header + import_tree.source_body, encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--install", "--target", str(import_tree.target)],
    )

    importer.main()

    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    expected_header = (
        "<!-- mattpocock-import: "
        "upstream=https://example.test/mattpocock/skills "
        "rev=0123456789ab license=MIT -->\n"
    )
    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8") == (
        expected_header + import_tree.source_body
    )
    assert (skill_dir / "NOTICE.txt").read_text(encoding="utf-8") == "support notice\n"
    assert (skill_dir / "assets" / "helper.bin").read_bytes() == b"\x00fixture-support\xff"

    output = capsys.readouterr().out
    assert f"Creating target dir: {import_tree.target}" in output
    assert "[NEW]" in output
    assert "(+2 support)" in output
    assert "Mode: install" in output
    assert "Entries: 1  new: 1  updated: 0  unchanged: 0" in output
    assert "Next steps:" in output


def test_checked_path_fallback_stages_and_installs_all_entry_writes(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_fallback_destination_parents(import_tree)
    _enable_checked_path_fallback(monkeypatch)

    destination, changed, support_paths = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed is True
    assert import_tree.source_body in destination.read_text(encoding="utf-8")
    assert (destination.parent / "NOTICE.txt").read_text(encoding="utf-8") == "support notice\n"
    assert (destination.parent / "assets" / "helper.bin").read_bytes() == (
        b"\x00fixture-support\xff"
    )
    assert support_paths == [
        import_tree.source_skill.parent / "NOTICE.txt",
        import_tree.source_skill.parent / "assets" / "helper.bin",
    ]
    assert not [path for path in destination.parent.rglob("*") if path.name.endswith(".tmp")]


@pytest.mark.parametrize("writer", ["native", "fallback"])
def test_install_does_not_require_fchmod(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    if writer == "native" and not importer._supports_directory_fds():
        pytest.skip("directory-relative writer is unavailable on this platform")
    if writer == "fallback":
        _create_fallback_destination_parents(import_tree)
        _enable_checked_path_fallback(monkeypatch)
    monkeypatch.delattr(importer.os, "fchmod", raising=False)

    destination, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed is True
    assert import_tree.source_body in destination.read_text(encoding="utf-8")
    assert not _transaction_artifacts(import_tree.target)

    _, changed_again, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed_again is False


def test_install_fails_closed_without_safe_filesystem_primitives(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "_supports_directory_fds", lambda: False)
    monkeypatch.setattr(importer, "_supports_windows_path_guards", lambda: False)

    with pytest.raises(RuntimeError, match="secure source read unavailable"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert not import_tree.target.exists()


def test_checked_path_fallback_creates_missing_destination_directories(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_checked_path_fallback(monkeypatch)

    destination, changed, support_paths = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed is True
    assert import_tree.source_body in destination.read_text(encoding="utf-8")
    assert (destination.parent / "NOTICE.txt").read_text(encoding="utf-8") == "support notice\n"
    assert (destination.parent / "assets" / "helper.bin").read_bytes() == (
        b"\x00fixture-support\xff"
    )
    assert support_paths == [
        import_tree.source_skill.parent / "NOTICE.txt",
        import_tree.source_skill.parent / "assets" / "helper.bin",
    ]


def test_native_path_guard_capability_tracks_windows() -> None:
    assert importer._supports_windows_path_guards() is (importer.os.name == "nt")


def test_checked_path_fallback_detects_parent_swap_while_acquiring_guards(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    (skill_dir / "assets").mkdir(parents=True)
    sentinel = skill_dir / "SKILL.md"
    sentinel.write_text("stale skill\n", encoding="utf-8")
    moved = import_tree.target / "moved-skill"
    swapped = False

    def swap_skill_parent(path: Path) -> None:
        nonlocal swapped
        if path == skill_dir and not swapped:
            skill_dir.rename(moved)
            skill_dir.mkdir()
            swapped = True

    _enable_checked_path_fallback(monkeypatch, on_guard=swap_skill_parent)

    with pytest.raises(ValueError, match="changed while acquiring guard"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert swapped is True
    assert (moved / "SKILL.md").read_text(encoding="utf-8") == "stale skill\n"
    assert not (skill_dir / "SKILL.md").exists()


def test_reinstall_is_idempotent(import_tree: ImportTree) -> None:
    destination, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )
    assert changed is True
    before = _snapshot_files(import_tree.target)

    second_destination, changed, support_paths = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert second_destination == destination
    assert changed is False
    assert support_paths == [
        import_tree.source_skill.parent / "NOTICE.txt",
        import_tree.source_skill.parent / "assets" / "helper.bin",
    ]
    assert _snapshot_files(import_tree.target) == before


@pytest.mark.parametrize("mode", ["--dry-run", "--install"])
def test_cli_reports_unchanged_existing_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )
    before = _snapshot_files(import_tree.target)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", mode, "--target", str(import_tree.target)],
    )

    importer.main()

    output = capsys.readouterr().out
    assert "[   ]" in output
    assert "Entries: 1  new: 0  updated: 0  unchanged: 1" in output
    assert _snapshot_files(import_tree.target) == before


def test_cli_uses_install_time_destination_state_for_update_marker(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_deploy = importer._deploy_entry_with_status
    injected = False

    def create_destination_after_preview(
        entry: dict,
        manifest: dict,
        target_dir: Path,
        dry_run: bool,
    ) -> tuple[Path, bool, list[Path], bool]:
        nonlocal injected
        if not dry_run and not injected:
            destination = target_dir / "mattpocock-fixture-skill" / "SKILL.md"
            destination.parent.mkdir(parents=True)
            destination.write_text("created after preview\n", encoding="utf-8")
            injected = True
        return original_deploy(entry, manifest, target_dir, dry_run)

    monkeypatch.setattr(importer, "_deploy_entry_with_status", create_destination_after_preview)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--install", "--target", str(import_tree.target)],
    )

    importer.main()

    output = capsys.readouterr().out
    assert injected is True
    assert "[UPD]" in output
    assert "Entries: 1  new: 0  updated: 1  unchanged: 0" in output


@pytest.mark.parametrize("slug", [None, "", "../escape", "nested/slug", "Uppercase", "safe\n"])
def test_rejects_unsafe_slugs(import_tree: ImportTree, slug: object) -> None:
    entry = {**import_tree.entry, "slug": slug}

    with pytest.raises(ValueError, match="slug"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


@pytest.mark.parametrize("source_path", ["../outside/SKILL.md", "/outside/SKILL.md"])
def test_rejects_source_path_traversal(import_tree: ImportTree, source_path: str) -> None:
    entry = {**import_tree.entry, "source_path": source_path}

    with pytest.raises(ValueError, match="source_path: path traversal denied"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


def test_rejects_source_symlink_escape(import_tree: ImportTree, tmp_path: Path) -> None:
    outside = tmp_path / "outside-source"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside\n", encoding="utf-8")
    _symlink_or_skip(import_tree.import_root / "linked-skill", outside, target_is_directory=True)
    entry = {**import_tree.entry, "source_path": "linked-skill/SKILL.md"}

    with pytest.raises(ValueError, match="source_path: .* resolves outside"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


@pytest.mark.parametrize("support_file", ["../outside.txt", "/outside.txt"])
def test_rejects_support_file_traversal(import_tree: ImportTree, support_file: str) -> None:
    entry = {**import_tree.entry, "support_files": [support_file]}

    with pytest.raises(ValueError, match="support_files: path traversal denied"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


def test_rejects_support_file_symlink_escape(import_tree: ImportTree, tmp_path: Path) -> None:
    outside = tmp_path / "outside-support.txt"
    outside.write_text("outside\n", encoding="utf-8")
    link = import_tree.source_skill.parent / "linked-support.txt"
    _symlink_or_skip(link, outside)
    entry = {**import_tree.entry, "support_files": [link.name]}

    with pytest.raises(ValueError, match="support_files: .* resolves outside"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative source reads"
)
@pytest.mark.parametrize("payload", ["skill", "support"])
def test_source_parent_swap_after_resolution_is_rejected(
    import_tree: ImportTree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    source_dir = import_tree.source_skill.parent
    moved = import_tree.import_root / "moved-fixture-skill"
    outside = tmp_path / "outside-source-parent"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside skill\n", encoding="utf-8")
    (outside / "NOTICE.txt").write_text("outside notice\n", encoding="utf-8")
    original_resolve = importer._resolve_within
    selected_field = "source_path" if payload == "skill" else "support_files"
    swapped = False

    def resolve_then_swap(root: Path, candidate_rel: str, *, field: str) -> Path:
        nonlocal swapped
        resolved = original_resolve(root, candidate_rel, field=field)
        if field == selected_field and not swapped:
            source_dir.rename(moved)
            _symlink_or_skip(source_dir, outside, target_is_directory=True)
            swapped = True
        return resolved

    monkeypatch.setattr(importer, "_resolve_within", resolve_then_swap)

    with pytest.raises(ValueError, match=f"{selected_field}: .*changed or is not a regular file"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=True,
        )

    assert swapped is True
    assert not import_tree.target.exists()


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative source reads"
)
@pytest.mark.parametrize("payload", ["skill", "support"])
def test_source_file_swap_after_resolution_is_rejected(
    import_tree: ImportTree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    outside = tmp_path / f"outside-{payload}.txt"
    outside.write_text("outside payload\n", encoding="utf-8")
    original_resolve = importer._resolve_within
    selected_field = "source_path" if payload == "skill" else "support_files"
    swapped = False

    def resolve_then_swap(root: Path, candidate_rel: str, *, field: str) -> Path:
        nonlocal swapped
        resolved = original_resolve(root, candidate_rel, field=field)
        if field == selected_field and not swapped:
            resolved.unlink()
            _symlink_or_skip(resolved, outside)
            swapped = True
        return resolved

    monkeypatch.setattr(importer, "_resolve_within", resolve_then_swap)

    with pytest.raises(ValueError, match=f"{selected_field}: .*changed or is not a regular file"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=True,
        )

    assert swapped is True
    assert outside.read_text(encoding="utf-8") == "outside payload\n"
    assert not import_tree.target.exists()


def test_checked_path_source_parent_swap_while_acquiring_guard_is_rejected(
    import_tree: ImportTree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = import_tree.source_skill.parent
    moved = import_tree.import_root / "moved-fixture-skill"
    outside = tmp_path / "outside-source-parent"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside skill\n", encoding="utf-8")
    swapped = False

    def swap_source_parent(path: Path) -> None:
        nonlocal swapped
        if path == source_dir and not swapped:
            source_dir.rename(moved)
            _symlink_or_skip(source_dir, outside, target_is_directory=True)
            swapped = True

    _enable_checked_path_fallback(monkeypatch, on_guard=swap_source_parent)

    with pytest.raises(ValueError, match="source parent: .*changed while acquiring guard"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=True,
        )

    assert swapped is True
    assert not import_tree.target.exists()


def test_symlink_target_root_is_resolved_once_and_trusted(
    import_tree: ImportTree,
    tmp_path: Path,
) -> None:
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    target_link = tmp_path / "target-link"
    _symlink_or_skip(target_link, real_target, target_is_directory=True)

    destination, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        target_link,
        dry_run=False,
    )

    assert changed is True
    assert destination == real_target / "mattpocock-fixture-skill" / "SKILL.md"
    assert import_tree.source_body in destination.read_text(encoding="utf-8")


def test_rejects_destination_skill_symlink_escape(import_tree: ImportTree, tmp_path: Path) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    skill_dir.mkdir(parents=True)
    outside = tmp_path / "outside-destination.md"
    outside.write_text("do not replace\n", encoding="utf-8")
    _symlink_or_skip(skill_dir / "SKILL.md", outside)

    with pytest.raises(ValueError, match="skill destination: .* resolves outside"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert outside.read_text(encoding="utf-8") == "do not replace\n"


def test_rejects_destination_support_symlink_escape(
    import_tree: ImportTree, tmp_path: Path
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    skill_dir.mkdir(parents=True)
    outside = tmp_path / "outside-support-destination"
    outside.mkdir()
    _symlink_or_skip(skill_dir / "assets", outside, target_is_directory=True)

    with pytest.raises(ValueError, match="support_files destination: .* resolves outside"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert not (outside / "helper.bin").exists()


def test_missing_source_has_actionable_error(import_tree: ImportTree) -> None:
    entry = {**import_tree.entry, "source_path": "missing/SKILL.md"}

    with pytest.raises(FileNotFoundError, match="Source skill missing: .*missing/SKILL.md"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)


def test_missing_manifest_exits_with_build_guidance(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = import_tree.manifest_path.with_name("missing-manifest.json")
    monkeypatch.setattr(importer, "MANIFEST_PATH", missing)

    with pytest.raises(SystemExit) as exc_info:
        importer.load_manifest()

    assert exc_info.value.code == 1
    error = capsys.readouterr().err
    assert f"Manifest not found: {missing}" in error
    assert "Run: python imported-skills/mattpocock/build_manifest.py" in error


def test_cli_requires_an_explicit_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["import_mattpocock_skills.py"])

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    assert exc_info.value.code == 2
    assert "Pass either --install or --dry-run" in capsys.readouterr().err


def test_cli_rejects_conflicting_modes_without_writing(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import_mattpocock_skills.py",
            "--install",
            "--dry-run",
            "--target",
            str(import_tree.target),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    assert exc_info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err
    assert not import_tree.target.exists()


@pytest.mark.parametrize("mode", ["--dry-run", "--install"])
def test_cli_reports_support_only_change_as_update(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )
    support_destination = import_tree.target / "mattpocock-fixture-skill" / "assets" / "helper.bin"
    support_destination.write_bytes(b"stale support")
    before = _snapshot_files(import_tree.target)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", mode, "--target", str(import_tree.target)],
    )

    importer.main()

    output = capsys.readouterr().out
    assert "[UPD]" in output
    assert "Entries: 1  new: 0  updated: 1  unchanged: 0" in output
    if mode == "--dry-run":
        assert _snapshot_files(import_tree.target) == before
    else:
        assert support_destination.read_bytes() == b"\x00fixture-support\xff"


def test_preflight_accepts_omitted_support_files(import_tree: ImportTree) -> None:
    entry = {key: value for key, value in import_tree.entry.items() if key != "support_files"}
    manifest = {**import_tree.manifest, "entries": [entry]}

    preflight = importer._preflight_manifest(manifest, import_tree.target)

    assert preflight == [
        (
            entry,
            (
                import_tree.target / "mattpocock-fixture-skill" / "SKILL.md",
                True,
                [],
            ),
        )
    ]
    assert not import_tree.target.exists()


@pytest.mark.parametrize("mode", ["--dry-run", "--install"])
def test_cli_accepts_omitted_support_files(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    entry = {key: value for key, value in import_tree.entry.items() if key != "support_files"}
    import_tree.manifest["entries"] = [entry]
    import_tree.manifest_path.write_text(json.dumps(import_tree.manifest), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", mode, "--target", str(import_tree.target)],
    )

    importer.main()

    captured = capsys.readouterr()
    assert "[NEW]" in captured.out
    assert "(+" not in captured.out
    assert "Entries: 1  new: 1  updated: 0  unchanged: 0" in captured.out
    assert captured.err == ""
    if mode == "--dry-run":
        assert not import_tree.target.exists()
    else:
        skill_dir = import_tree.target / "mattpocock-fixture-skill"
        assert [path.relative_to(skill_dir) for path in skill_dir.rglob("*")] == [Path("SKILL.md")]


@pytest.mark.parametrize("support_file", ["missing.txt", "assets"])
def test_rejects_declared_support_path_that_is_not_a_regular_file(
    import_tree: ImportTree, support_file: str
) -> None:
    entry = {**import_tree.entry, "support_files": [support_file]}

    with pytest.raises(ValueError, match="support_files: .* is not a regular file"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


def test_support_symlink_preserves_declared_lexical_destination(import_tree: ImportTree) -> None:
    link = import_tree.source_skill.parent / "linked-notice.txt"
    _symlink_or_skip(link, Path("NOTICE.txt"))
    entry = {**import_tree.entry, "support_files": [link.name]}

    destination, changed, support_paths = importer.deploy_entry(
        entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed is True
    assert support_paths == [import_tree.source_skill.parent / "NOTICE.txt"]
    assert (destination.parent / link.name).read_text(encoding="utf-8") == "support notice\n"
    assert not (destination.parent / "NOTICE.txt").exists()


@pytest.mark.parametrize(
    "support_files",
    [
        ["NOTICE.txt", "./NOTICE.txt"],
        ["SKILL.md"],
        ["skill.md"],
        ["\u017fKILL.md"],
        ["NOTICE.txt", "notice.txt"],
    ],
)
def test_rejects_duplicate_support_destinations_before_writing(
    import_tree: ImportTree, support_files: list[str]
) -> None:
    entry = {**import_tree.entry, "support_files": support_files}

    with pytest.raises(ValueError, match="support_files: .*duplicate destination"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


def test_rejects_canonically_equivalent_unicode_support_destinations(
    import_tree: ImportTree,
) -> None:
    composed = "Caf\u00e9.txt"
    decomposed = "Cafe\u0301.txt"
    (import_tree.source_skill.parent / composed).write_text("support\n", encoding="utf-8")
    entry = {**import_tree.entry, "support_files": [composed, decomposed]}

    with pytest.raises(ValueError, match="support_files: .*duplicate destination"):
        importer.deploy_entry(entry, import_tree.manifest, import_tree.target, dry_run=False)

    assert not import_tree.target.exists()


def test_distinct_hard_linked_destinations_are_replaced_independently(
    import_tree: ImportTree,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    skill_dir.mkdir(parents=True)
    destination = skill_dir / "SKILL.md"
    destination.write_text("sentinel\n", encoding="utf-8")
    support_destination = skill_dir / "NOTICE.txt"
    _hardlink_or_skip(support_destination, destination)
    entry = {**import_tree.entry, "support_files": ["NOTICE.txt"]}

    deployed, changed, _ = importer.deploy_entry(
        entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert deployed == destination
    assert changed is True
    assert import_tree.source_body in destination.read_text(encoding="utf-8")
    assert support_destination.read_text(encoding="utf-8") == "support notice\n"
    assert not destination.samefile(support_destination)


def test_install_atomically_replaces_hard_linked_skill_without_changing_outside_inode(
    import_tree: ImportTree,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    skill_dir.mkdir(parents=True)
    outside = import_tree.target.parent / "outside-skill.md"
    outside.write_text("outside skill sentinel\n", encoding="utf-8")
    destination = skill_dir / "SKILL.md"
    _hardlink_or_skip(destination, outside)
    assert destination.samefile(outside)

    deployed, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert deployed == destination
    assert changed is True
    assert outside.read_text(encoding="utf-8") == "outside skill sentinel\n"
    assert not destination.samefile(outside)
    assert import_tree.source_body in destination.read_text(encoding="utf-8")


def test_install_atomically_replaces_hard_linked_support_without_changing_outside_inode(
    import_tree: ImportTree,
) -> None:
    destination, _, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )
    support_destination = destination.parent / "NOTICE.txt"
    support_destination.unlink()
    outside = import_tree.target.parent / "outside-support.txt"
    outside.write_text("support notice\n", encoding="utf-8")
    _hardlink_or_skip(support_destination, outside)
    assert support_destination.samefile(outside)
    source_support = import_tree.source_skill.parent / "NOTICE.txt"
    source_support.write_text("updated support notice\n", encoding="utf-8")

    _, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed is True
    assert outside.read_text(encoding="utf-8") == "support notice\n"
    assert not support_destination.samefile(outside)
    assert support_destination.read_text(encoding="utf-8") == "updated support notice\n"


@pytest.mark.parametrize("relative_path", [Path("SKILL.md"), Path("NOTICE.txt")])
def test_install_detaches_unchanged_hard_link(
    import_tree: ImportTree,
    relative_path: Path,
) -> None:
    destination, _, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )
    selected = destination.parent / relative_path
    expected = selected.read_bytes()
    expected_mode = selected.stat().st_mode & 0o777
    outside = import_tree.target.parent / f"outside-unchanged-{relative_path.name}"
    outside.write_bytes(expected)
    outside.chmod(expected_mode)
    selected.unlink()
    _hardlink_or_skip(selected, outside)
    assert selected.samefile(outside)

    _, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed is True
    assert not selected.samefile(outside)
    assert selected.read_bytes() == expected
    assert outside.read_bytes() == expected


def test_rejects_in_tree_symlinked_support_parent_before_any_write(
    import_tree: ImportTree,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    real_assets = skill_dir / "real-assets"
    real_assets.mkdir(parents=True)
    sentinel = real_assets / "helper.bin"
    sentinel.write_bytes(b"in-tree sentinel")
    _symlink_or_skip(skill_dir / "assets", Path("real-assets"), target_is_directory=True)

    with pytest.raises(ValueError, match=r"destination parent: .*assets.*symlink"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert sentinel.read_bytes() == b"in-tree sentinel"
    assert not (skill_dir / "SKILL.md").exists()
    assert not (skill_dir / "NOTICE.txt").exists()


def test_rejects_in_tree_final_support_symlink_before_any_write(
    import_tree: ImportTree,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    skill_dir.mkdir(parents=True)
    sentinel = skill_dir / "support-sentinel.txt"
    sentinel.write_text("in-tree support sentinel\n", encoding="utf-8")
    _symlink_or_skip(skill_dir / "NOTICE.txt", Path("support-sentinel.txt"))

    with pytest.raises(ValueError, match=r"support_files destination: .*NOTICE\.txt.*symlink"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert sentinel.read_text(encoding="utf-8") == "in-tree support sentinel\n"
    assert not (skill_dir / "SKILL.md").exists()


def test_regular_file_support_parent_fails_before_any_destination_mutation(
    import_tree: ImportTree,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    notice = skill_dir / "NOTICE.txt"
    blocked_parent = skill_dir / "assets"
    skill.write_text("stale skill\n", encoding="utf-8")
    notice.write_text("stale notice\n", encoding="utf-8")
    blocked_parent.write_text("regular-file parent sentinel\n", encoding="utf-8")
    before = _snapshot_files(import_tree.target)

    with pytest.raises(ValueError, match=r"destination parent: .*assets.*not a directory"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert _snapshot_files(import_tree.target) == before


@pytest.mark.parametrize("writer", ["native", "fallback"])
def test_staging_failure_does_not_commit_any_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    if writer == "native" and not importer._supports_directory_fds():
        pytest.skip("directory-relative writer is unavailable on this platform")
    if writer == "fallback":
        _enable_checked_path_fallback(monkeypatch)
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("stale skill\n", encoding="utf-8")
    (skill_dir / "NOTICE.txt").write_text("stale notice\n", encoding="utf-8")
    (skill_dir / "assets" / "helper.bin").write_bytes(b"stale helper")
    before = _snapshot_files(import_tree.target)
    original_write = importer._write_staged_payload
    calls = 0

    def fail_after_second_stage(
        fd: int, write: importer._PreparedWrite
    ) -> importer._DestinationState:
        nonlocal calls
        calls += 1
        state = original_write(fd, write)
        if calls == 2:
            raise OSError("injected staging failure")
        return state

    monkeypatch.setattr(importer, "_write_staged_payload", fail_after_second_stage)

    with pytest.raises(OSError, match="injected staging failure"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert calls == 2
    assert _snapshot_files(import_tree.target) == before
    assert not [path for path in skill_dir.rglob("*") if path.name.endswith(".tmp")]


def test_precommit_destination_swap_does_not_partially_install_entry(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    (skill_dir / "assets").mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    notice = skill_dir / "NOTICE.txt"
    helper = skill_dir / "assets" / "helper.bin"
    skill.write_text("stale skill\n", encoding="utf-8")
    notice.write_text("stale notice\n", encoding="utf-8")
    helper.write_bytes(b"stale helper")
    outside = import_tree.target.parent / "precommit-sentinel.txt"
    outside.write_text("outside sentinel\n", encoding="utf-8")
    original_revalidate = importer._revalidate_staged_writes

    def swap_notice_before_revalidation(
        prepared: importer._PreparedEntry,
        directories: dict[Path, importer._OpenedDirectory],
        staged_writes: list[importer._StagedWrite],
    ) -> None:
        notice.unlink()
        _symlink_or_skip(notice, outside)
        original_revalidate(prepared, directories, staged_writes)

    monkeypatch.setattr(importer, "_revalidate_staged_writes", swap_notice_before_revalidation)

    with pytest.raises(ValueError, match=r"NOTICE\.txt.*changed after preflight"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert skill.read_text(encoding="utf-8") == "stale skill\n"
    assert helper.read_bytes() == b"stale helper"
    assert outside.read_text(encoding="utf-8") == "outside sentinel\n"
    assert notice.is_symlink()
    assert not [path for path in skill_dir.rglob("*") if path.name.endswith(".tmp")]


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_replacing_staged_temp_cannot_inject_payload(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("stale skill\n", encoding="utf-8")
    (skill_dir / "NOTICE.txt").write_text("stale notice\n", encoding="utf-8")
    (skill_dir / "assets" / "helper.bin").write_bytes(b"stale helper")
    before = _snapshot_files(import_tree.target)
    outside = import_tree.target.parent / "staged-temp-sentinel"
    outside.write_bytes(b"outside sentinel")
    original_commit = importer._commit_staged_write
    replaced = False

    def replace_temp_before_rename(staged: importer._StagedWrite) -> None:
        nonlocal replaced
        if not replaced:
            staged.temporary_path.unlink()
            if replacement == "symlink":
                _symlink_or_skip(staged.temporary_path, outside)
            else:
                staged.temporary_path.write_bytes(b"injected payload")
            replaced = True
        original_commit(staged)

    monkeypatch.setattr(importer, "_commit_staged_write", replace_temp_before_rename)

    with pytest.raises(ValueError, match="staged payload .* changed after staging"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert replaced is True
    assert _snapshot_files(import_tree.target) == before
    assert outside.read_bytes() == b"outside sentinel"
    assert not _transaction_artifacts(import_tree.target)


@pytest.mark.parametrize("destination_existed", [False, True])
def test_temp_swap_inside_replace_is_removed_or_restores_previous_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    destination_existed: bool,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    if destination_existed:
        destination.parent.mkdir(parents=True)
        destination.write_text("previous destination\n", encoding="utf-8")
    original_replace = importer._replace_name
    swapped = False

    def swap_after_validation(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if source_name == staged.temporary_name and not swapped:
            staged.temporary_path.unlink()
            staged.temporary_path.write_bytes(b"malicious replacement")
            swapped = True
        original_replace(staged, source_name, destination_name)

    monkeypatch.setattr(importer, "_replace_name", swap_after_validation)

    with pytest.raises(ValueError, match="staged payload .* changed after staging"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert swapped is True
    if destination_existed:
        assert destination.read_text(encoding="utf-8") == "previous destination\n"
    else:
        assert not destination.exists()
    assert not _transaction_artifacts(import_tree.target)


@pytest.mark.skipif(importer.os.name == "nt", reason="POSIX permission bits are required")
def test_mode_tamper_inside_replace_restores_previous_destination_without_fchmod(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    previous_mode = destination.stat().st_mode & 0o777
    original_replace = importer._replace_name
    tampered = False
    monkeypatch.delattr(importer.os, "fchmod", raising=False)

    def change_mode_after_replace(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal tampered
        original_replace(staged, source_name, destination_name)
        if source_name == staged.temporary_name and not tampered:
            staged.write.destination.chmod(0o777)
            tampered = True

    monkeypatch.setattr(importer, "_replace_name", change_mode_after_replace)

    with pytest.raises(ValueError, match="staged payload .* changed after staging"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert tampered is True
    assert destination.read_text(encoding="utf-8") == "previous destination\n"
    assert destination.stat().st_mode & 0o777 == previous_mode
    assert not _transaction_artifacts(import_tree.target)


def test_restore_failure_preserves_prior_destination_at_reported_recovery_path(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    original_replace = importer._replace_name
    swapped = False

    def inject_payload_and_recovery_failures(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal swapped
        if source_name == staged.temporary_name and not swapped:
            staged.temporary_path.unlink()
            staged.temporary_path.write_bytes(b"malicious replacement")
            swapped = True
        if source_name.endswith(".recovery"):
            raise OSError("injected recovery failure")
        original_replace(staged, source_name, destination_name)

    monkeypatch.setattr(importer, "_replace_name", inject_payload_and_recovery_failures)

    with pytest.raises(RuntimeError, match="prior destination preserved at") as exc_info:
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    recovery_paths = list(destination.parent.glob(".SKILL.md.*.recovery"))
    assert swapped is True
    assert len(recovery_paths) == 1
    assert str(recovery_paths[0]) in str(exc_info.value)
    assert recovery_paths[0].read_text(encoding="utf-8") == "previous destination\n"
    assert not destination.exists()


def test_post_success_restore_exception_keeps_verified_prior_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    original_replace = importer._replace_name
    staged_swapped = False

    def interrupt_after_successful_restore(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal staged_swapped
        if source_name == staged.temporary_name and not staged_swapped:
            staged.temporary_path.unlink()
            staged.temporary_path.write_bytes(b"malicious staged payload")
            staged_swapped = True
        original_replace(staged, source_name, destination_name)
        if source_name.endswith(".recovery"):
            raise KeyboardInterrupt("injected post-restore interruption")

    monkeypatch.setattr(importer, "_replace_name", interrupt_after_successful_restore)

    with pytest.raises(ValueError, match="staged payload .* changed after staging"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert staged_swapped is True
    assert destination.read_text(encoding="utf-8") == "previous destination\n"
    assert not _transaction_artifacts(import_tree.target)


def test_post_success_restore_exception_removes_tampered_canonical_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    original_replace = importer._replace_name
    staged_swapped = False

    def tamper_and_interrupt_after_restore(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal staged_swapped
        if source_name == staged.temporary_name and not staged_swapped:
            staged.temporary_path.unlink()
            staged.temporary_path.write_bytes(b"malicious staged payload")
            staged_swapped = True
        original_replace(staged, source_name, destination_name)
        if source_name.endswith(".recovery"):
            staged.write.destination.write_bytes(b"tampered restored payload")
            raise KeyboardInterrupt("injected post-restore interruption")

    monkeypatch.setattr(importer, "_replace_name", tamper_and_interrupt_after_restore)

    with pytest.raises(ValueError, match="recovery payload .* changed after staging"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert staged_swapped is True
    assert not destination.exists()


def test_recovery_snapshot_is_isolated_from_canonical_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    original_create = importer._create_recovery_snapshot
    isolated = False

    def tamper_snapshot_after_creation(
        staged: importer._StagedWrite,
    ) -> importer._RecoverySnapshot | None:
        nonlocal isolated
        snapshot = original_create(staged)
        if snapshot is None or isolated:
            return snapshot
        recovery_path = staged.write.destination.parent / snapshot.name
        assert recovery_path.stat().st_ino != destination.stat().st_ino
        recovery_path.write_bytes(b"malicious recovery payload")
        assert destination.read_text(encoding="utf-8") == "previous destination\n"
        isolated = True
        return snapshot

    monkeypatch.setattr(importer, "_create_recovery_snapshot", tamper_snapshot_after_creation)

    deployed, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert isolated is True
    assert changed is True
    assert import_tree.source_body in deployed.read_text(encoding="utf-8")
    assert not _transaction_artifacts(import_tree.target)


@pytest.mark.parametrize("tamper_kind", ["overwrite", "replace"])
def test_tampered_recovery_payload_is_never_left_at_canonical_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    tamper_kind: str,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    original_replace = importer._replace_name
    staged_swapped = False
    recovery_tampered = False

    def inject_staged_and_recovery_tampering(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal recovery_tampered, staged_swapped
        source_path = staged.write.destination.parent / source_name
        if source_name == staged.temporary_name and not staged_swapped:
            source_path.unlink()
            source_path.write_bytes(b"malicious staged payload")
            staged_swapped = True
        elif source_name.endswith(".recovery") and not recovery_tampered:
            if tamper_kind == "replace":
                source_path.unlink()
            source_path.write_bytes(b"malicious recovery payload")
            recovery_tampered = True
        original_replace(staged, source_name, destination_name)

    monkeypatch.setattr(importer, "_replace_name", inject_staged_and_recovery_tampering)

    with pytest.raises(ValueError, match="recovery payload .* changed after staging"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert staged_swapped is True
    assert recovery_tampered is True
    assert not destination.exists()


def test_recovery_tampered_before_restore_is_reported_as_untrusted(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    original_replace = importer._replace_name
    original_recover = importer._recover_failed_commit
    recovery_path: Path | None = None

    def inject_staged_tampering(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        if source_name == staged.temporary_name:
            staged.temporary_path.unlink()
            staged.temporary_path.write_bytes(b"malicious staged payload")
        original_replace(staged, source_name, destination_name)

    def tamper_before_recovery(
        staged: importer._StagedWrite,
        recovery: importer._RecoverySnapshot | None,
    ) -> None:
        nonlocal recovery_path
        assert recovery is not None
        recovery_path = staged.write.destination.parent / recovery.name
        recovery_path.write_bytes(b"malicious recovery payload")
        original_recover(staged, recovery)

    monkeypatch.setattr(importer, "_replace_name", inject_staged_tampering)
    monkeypatch.setattr(importer, "_recover_failed_commit", tamper_before_recovery)

    with pytest.raises(RuntimeError, match="untrusted recovery payload quarantined at"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert recovery_path is not None
    assert recovery_path.exists()
    assert not destination.exists()


def test_recovery_read_oserror_removes_untrusted_canonical_destination(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous destination\n", encoding="utf-8")
    original_read = importer._read_named_payload
    original_replace = importer._replace_name
    recovery_reads = 0
    staged_swapped = False

    def inject_staged_tampering(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        nonlocal staged_swapped
        if source_name == staged.temporary_name and not staged_swapped:
            staged.temporary_path.unlink()
            staged.temporary_path.write_bytes(b"malicious staged payload")
            staged_swapped = True
        original_replace(staged, source_name, destination_name)

    def fail_second_recovery_read(
        staged: importer._StagedWrite,
        name: str,
    ) -> tuple[Any, bytes]:
        nonlocal recovery_reads
        if name.endswith(".recovery"):
            recovery_reads += 1
            if recovery_reads == 2:
                raise OSError("injected recovery read failure")
        return original_read(staged, name)

    monkeypatch.setattr(importer, "_replace_name", inject_staged_tampering)
    monkeypatch.setattr(importer, "_read_named_payload", fail_second_recovery_read)

    with pytest.raises(RuntimeError, match="prior destination preserved at"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert staged_swapped is True
    assert recovery_reads >= 3
    assert not destination.exists()


@pytest.mark.parametrize("destination_existed", [False, True])
def test_cleanup_refusal_explicitly_reports_untrusted_canonical_content(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    destination_existed: bool,
) -> None:
    destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    if destination_existed:
        destination.parent.mkdir(parents=True)
        destination.write_text("previous destination\n", encoding="utf-8")
    original_replace = importer._replace_name
    original_unlink = importer._unlink_name

    def inject_payload_and_restore_failure(
        staged: importer._StagedWrite,
        source_name: str,
        destination_name: str,
    ) -> None:
        source_path = staged.write.destination.parent / source_name
        if source_name == staged.temporary_name:
            source_path.unlink()
            source_path.write_bytes(b"malicious canonical payload")
        if source_name.endswith(".recovery"):
            raise OSError("injected recovery replace failure")
        original_replace(staged, source_name, destination_name)

    def refuse_canonical_unlink(staged: importer._StagedWrite, name: str) -> None:
        if name == staged.write.destination.name:
            raise OSError("injected canonical cleanup failure")
        original_unlink(staged, name)

    monkeypatch.setattr(importer, "_replace_name", inject_payload_and_restore_failure)
    monkeypatch.setattr(importer, "_unlink_name", refuse_canonical_unlink)

    with pytest.raises(RuntimeError, match="CRITICAL: untrusted canonical content remains at"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert destination.read_bytes() == b"malicious canonical payload"


def test_second_commit_failure_is_recoverable_by_idempotent_rerun(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = import_tree.target / "mattpocock-fixture-skill"
    (skill_dir / "assets").mkdir(parents=True)
    (skill_dir / "NOTICE.txt").write_text("stale notice\n", encoding="utf-8")
    (skill_dir / "assets" / "helper.bin").write_bytes(b"stale helper")
    original_commit = importer._commit_staged_write
    calls = 0

    def fail_second_commit(staged: importer._StagedWrite) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second rename failure")
        original_commit(staged)

    monkeypatch.setattr(importer, "_commit_staged_write", fail_second_commit)

    with pytest.raises(OSError, match="injected second rename failure"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            import_tree.target,
            dry_run=False,
        )

    assert calls == 2
    skill = skill_dir / "SKILL.md"
    assert import_tree.source_body in skill.read_text(encoding="utf-8")
    assert (skill_dir / "NOTICE.txt").read_text(encoding="utf-8") == "stale notice\n"
    assert (skill_dir / "assets" / "helper.bin").read_bytes() == b"stale helper"
    assert not _transaction_artifacts(import_tree.target)

    monkeypatch.setattr(importer, "_commit_staged_write", original_commit)
    _, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )
    _, changed_again, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert changed is True
    assert changed_again is False
    assert (skill_dir / "NOTICE.txt").read_text(encoding="utf-8") == "support notice\n"
    assert (skill_dir / "assets" / "helper.bin").read_bytes() == b"\x00fixture-support\xff"
    assert not _transaction_artifacts(import_tree.target)


def test_final_write_symlink_swap_is_replaced_lexically_without_touching_sentinel(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = import_tree.target.parent / "final-write-sentinel.md"
    outside.write_text("outside sentinel\n", encoding="utf-8")
    original_commit = importer._commit_staged_write
    swapped = False

    def swap_skill_at_final_write(staged: importer._StagedWrite) -> None:
        nonlocal swapped
        if staged.write.destination.name == "SKILL.md":
            _symlink_or_skip(staged.write.destination, outside)
            swapped = True
        original_commit(staged)

    monkeypatch.setattr(importer, "_commit_staged_write", swap_skill_at_final_write)

    destination, changed, _ = importer.deploy_entry(
        import_tree.entry,
        import_tree.manifest,
        import_tree.target,
        dry_run=False,
    )

    assert swapped is True
    assert changed is True
    assert outside.read_text(encoding="utf-8") == "outside sentinel\n"
    assert not destination.is_symlink()
    assert import_tree.source_body in destination.read_text(encoding="utf-8")


def test_portable_destination_key_normalizes_unicode_and_case() -> None:
    assert importer._portable_destination_key("Assets/Caf\u00e9.TXT") == (
        importer._portable_destination_key("assets/Cafe\u0301.txt")
    )


def test_destination_parent_paths_rejects_paths_outside_target(tmp_path: Path) -> None:
    target = tmp_path / "skills"

    with pytest.raises(ValueError, match="is outside target_dir"):
        importer._destination_parent_paths(target, [tmp_path / "outside" / "SKILL.md"])


def test_manifest_validation_labels_unnamed_entries_and_requires_a_list(
    import_tree: ImportTree,
) -> None:
    assert importer._entry_label({}) == "<unnamed>"
    manifest = {**import_tree.manifest, "entries": "not-a-list"}

    with pytest.raises(ValueError, match="manifest.entries: expected list"):
        importer._preflight_manifest(manifest, import_tree.target)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("upstream", "https://example.test/unsafe\ninjected"),
        ("upstream_revision", "abc-->injected"),
        ("license", "MIT\rinjected"),
    ],
)
def test_unsafe_attribution_values_are_rejected(
    import_tree: ImportTree,
    field: str,
    unsafe_value: str,
) -> None:
    manifest = {**import_tree.manifest, field: unsafe_value}

    with pytest.raises(ValueError, match=f"manifest.{field}: unsafe attribution value"):
        importer.deploy_entry(
            import_tree.entry,
            manifest,
            import_tree.target,
            dry_run=True,
        )

    assert not import_tree.target.exists()


def test_invalid_later_entry_does_not_partially_install(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = {
        **import_tree.entry,
        "slug": "broken-skill",
        "source_path": "missing/SKILL.md",
    }
    import_tree.manifest["entries"] = [import_tree.entry, invalid]
    import_tree.manifest_path.write_text(json.dumps(import_tree.manifest), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--install", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "entry 2 ('broken-skill'): Source skill missing:" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


def test_duplicate_destination_slugs_are_rejected_before_install(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    duplicate = {**import_tree.entry}
    import_tree.manifest["entries"] = [import_tree.entry, duplicate]
    import_tree.manifest_path.write_text(json.dumps(import_tree.manifest), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--install", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "entry 2 ('fixture-skill')" in error
    assert "duplicate destination 'mattpocock-fixture-skill'" in error
    assert "entry 1 ('fixture-skill')" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


def test_cross_entry_hard_links_are_replaced_as_independent_destinations(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    second_source_dir = import_tree.import_root / "second-skill"
    second_source_dir.mkdir()
    (second_source_dir / "SKILL.md").write_text("# Second skill\n", encoding="utf-8")
    (second_source_dir / "NOTICE.txt").write_text("second support\n", encoding="utf-8")
    second_entry = {
        "slug": "second-skill",
        "source_path": "second-skill/SKILL.md",
        "support_files": ["NOTICE.txt"],
    }
    import_tree.manifest["entries"] = [import_tree.entry, second_entry]
    import_tree.manifest_path.write_text(json.dumps(import_tree.manifest), encoding="utf-8")

    first_destination = import_tree.target / "mattpocock-fixture-skill" / "SKILL.md"
    first_destination.parent.mkdir(parents=True)
    first_destination.write_text("sentinel\n", encoding="utf-8")
    second_support = import_tree.target / "mattpocock-second-skill" / "NOTICE.txt"
    second_support.parent.mkdir(parents=True)
    _hardlink_or_skip(second_support, first_destination)
    assert second_support.samefile(first_destination)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--install", "--target", str(import_tree.target)],
    )

    importer.main()

    captured = capsys.readouterr()
    assert captured.err == ""
    assert "Entries: 2  new: 1  updated: 1  unchanged: 0" in captured.out
    assert import_tree.source_body in first_destination.read_text(encoding="utf-8")
    assert second_support.read_text(encoding="utf-8") == "second support\n"
    assert not second_support.samefile(first_destination)


def test_malformed_manifest_json_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_tree.manifest_path.write_text('{"entries": [', encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"Invalid manifest JSON: {import_tree.manifest_path}:1:" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


@pytest.mark.parametrize("manifest_root", [[], "entries", 42, None])
def test_non_object_manifest_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    manifest_root: object,
) -> None:
    import_tree.manifest_path.write_text(json.dumps(manifest_root), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert (
        f"Invalid manifest: {import_tree.manifest_path}: expected JSON object, "
        f"got {type(manifest_root).__name__}"
    ) in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


def test_invalid_utf8_manifest_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_tree.manifest_path.write_bytes(b"\xff")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"Unable to read manifest: {import_tree.manifest_path}:" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


def test_invalid_utf8_source_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_tree.source_skill.write_bytes(b"\xff")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "entry 1 ('fixture-skill')" in error
    assert "utf-8" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


def test_unterminated_import_header_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_tree.source_skill.write_text("<!-- mattpocock-import: broken", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "entry 1 ('fixture-skill'): source_path:" in error
    assert "unterminated attribution header" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


def test_existing_file_target_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import_tree.target.write_text("sentinel\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--install", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"Import failed: target: {import_tree.target} is not a directory" in error
    assert "Traceback" not in error
    assert import_tree.target.read_text(encoding="utf-8") == "sentinel\n"


def test_target_symlink_loop_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_resolve = Path.resolve

    def fail_target_resolve(self: Path, strict: bool = False) -> Path:
        if self == import_tree.target:
            raise RuntimeError("symlink loop")
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_target_resolve)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "Import failed: symlink loop" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative creation"
)
def test_target_creation_uses_pinned_ancestor_when_lexical_parent_is_swapped(
    import_tree: ImportTree,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder = tmp_path / "target-holder"
    holder.mkdir()
    target = holder / "target-skills"
    moved_holder = tmp_path / "moved-target-holder"
    outside = tmp_path / "outside-target-holder"
    outside.mkdir()
    real_mkdir = importer.os.mkdir
    swapped = False

    def swap_parent_before_mkdir(
        path: Any,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if path == target.name and dir_fd is not None and not swapped:
            holder.rename(moved_holder)
            _symlink_or_skip(holder, outside, target_is_directory=True)
            swapped = True
        real_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(importer, "_supports_directory_fds", lambda: True)
    monkeypatch.setattr(importer.os, "mkdir", swap_parent_before_mkdir)

    with pytest.raises(ValueError, match="destination parent .* changed after preflight"):
        importer.deploy_entry(
            import_tree.entry,
            import_tree.manifest,
            target,
            dry_run=False,
        )

    assert swapped is True
    assert not (outside / target.name).exists()
    assert (moved_holder / target.name).is_dir()
    assert not _transaction_artifacts(moved_holder)


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative creation"
)
def test_target_creation_error_is_concise_exit_one(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_mkdir = importer.os.mkdir

    def deny_target_creation(
        path: Any,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == import_tree.target.name and dir_fd is not None:
            raise PermissionError("permission denied by test")
        real_mkdir(path, mode=mode, dir_fd=dir_fd)

    monkeypatch.setattr(importer, "_supports_directory_fds", lambda: True)
    monkeypatch.setattr(importer.os, "mkdir", deny_target_creation)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--install", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert "Import failed: entry 1 ('fixture-skill'): permission denied by test" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()


@pytest.mark.parametrize(
    ("entry_updates", "message"),
    [
        ({"slug": "../escape"}, "slug:"),
        ({"support_files": ["missing.txt"]}, "support_files:"),
    ],
)
def test_cli_reports_known_entry_failures_with_entry_name(
    import_tree: ImportTree,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry_updates: dict[str, Any],
    message: str,
) -> None:
    entry = {**import_tree.entry, **entry_updates}
    import_tree.manifest["entries"] = [entry]
    import_tree.manifest_path.write_text(json.dumps(import_tree.manifest), encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["import_mattpocock_skills.py", "--dry-run", "--target", str(import_tree.target)],
    )

    with pytest.raises(SystemExit) as exc_info:
        importer.main()

    error = capsys.readouterr().err
    assert exc_info.value.code == 1
    assert f"entry 1 ({entry['slug']!r}): {message}" in error
    assert "Traceback" not in error
    assert not import_tree.target.exists()
