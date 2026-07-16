"""
test_skill_add_detector.py -- Tests for the installed skill-add hook flow.

Covers:
  - main(): installed stdin Write/Edit behavior, conversion, and malformed input
  - validate_user_supplied_slug: accepts valid names, rejects traversal / invalid chars
  - is_in_skill_dir: containment check works as expected
"""

from __future__ import annotations

import errno
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skill_add_detector as sad  # noqa: E402
from ctx.utils._file_lock import file_lock  # noqa: E402


def _configure_installed_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path]:
    skills = tmp_path / "skills"
    wiki = tmp_path / "wiki"
    registry = tmp_path / "skill-registry.json"
    catalog = wiki / "catalog.md"
    skills.mkdir()
    wiki.mkdir()
    registry.write_text(json.dumps({"skill_dirs": [str(skills)]}), encoding="utf-8")
    catalog.write_text(
        "| Name | Type | Lines | Over limit | Path |\n| --- | --- | ---: | --- | --- |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sad, "REGISTRY_PATH", registry)
    monkeypatch.setattr(sad, "CATALOG_PATH", catalog)
    monkeypatch.setattr(sad, "WIKI_DIR", wiki)
    monkeypatch.setattr(sad, "LINE_THRESHOLD", 180)
    return skills, wiki, catalog


def _run_stdin_main(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "argv", ["skill_add_detector", "--from-stdin"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    sad.main()


def _wait_for_lock_attempt(
    marker: Path,
    process: subprocess.Popen[str],
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            pytest.fail(
                f"detector exited before lock attempt: rc={process.returncode}\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        time.sleep(0.025)
    pytest.fail(f"timed out waiting for detector lock attempt: {marker}")


# ────────────────────────────────────────────────────────────────────
# Installed PostToolUse stdin flow
# ────────────────────────────────────────────────────────────────────


def test_main_installed_stdin_write_edit_registers_short_skill_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skills, wiki, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    skill_file = skills / "short-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# Short skill\n\nUse the short flow.\n", encoding="utf-8")

    for tool_name in ("Write", "Edit"):
        _run_stdin_main(
            monkeypatch,
            {"tool_name": tool_name, "tool_input": {"file_path": str(skill_file)}},
        )

    content = catalog.read_text(encoding="utf-8")
    expected = f"| short-skill | skill | 3 |  | `{skill_file}` |"
    assert content.count("| short-skill |") == 1
    assert expected in content
    assert not (wiki / "converted" / "short-skill").exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("relative_path", "candidate_kind"),
    [
        (Path("not-a-skill") / "NOTSKILL.md", "file"),
        (Path("wrong-case") / "skill.md", "file"),
        (Path("missing-skill") / "SKILL.md", "missing"),
        (Path("directory-skill") / "SKILL.md", "directory"),
    ],
)
def test_main_ignores_nonexact_or_missing_skill_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    relative_path: Path,
    candidate_kind: str,
) -> None:
    skills, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    candidate = skills / relative_path
    candidate.parent.mkdir()
    if candidate_kind == "file":
        candidate.write_text("# Not a skill\n", encoding="utf-8")
    elif candidate_kind == "directory":
        candidate.mkdir()
    original_catalog = catalog.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_stdin_main(
            monkeypatch,
            {"tool_name": "Write", "tool_input": {"file_path": str(candidate)}},
        )

    assert exc_info.value.code == 0
    assert catalog.read_text(encoding="utf-8") == original_catalog


def test_main_resolves_stdin_candidate_once_strictly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skills, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    skill_file = skills / "single-resolve" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# Skill\n", encoding="utf-8")
    original_resolve = Path.resolve
    candidate_resolutions: list[bool] = []

    def track_resolve(path: Path, strict: bool = False) -> Path:
        if path == skill_file:
            candidate_resolutions.append(strict)
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", track_resolve)

    _run_stdin_main(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": str(skill_file)}},
    )

    assert candidate_resolutions == [True]
    assert f"| single-resolve | skill | 1 |  | `{skill_file}` |" in catalog.read_text(
        encoding="utf-8"
    )


def test_main_ignores_unreadable_skill_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skills, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    skill_file = skills / "unreadable-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# Unreadable\n", encoding="utf-8")
    original_catalog = catalog.read_text(encoding="utf-8")
    original_read_text = Path.read_text

    def fail_skill_read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        if path == skill_file:
            raise PermissionError("test read failure")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", fail_skill_read)

    with pytest.raises(SystemExit) as exc_info:
        _run_stdin_main(
            monkeypatch,
            {"tool_name": "Write", "tool_input": {"file_path": str(skill_file)}},
        )

    assert exc_info.value.code == 0
    assert catalog.read_text(encoding="utf-8") == original_catalog


def test_catalog_upsert_matches_only_name_column(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skills, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + "| existing | skill | 1 |  | `/tmp/existing/SKILL.md` |\n"
        + "Keep this prose | skill | mention unchanged.\n",
        encoding="utf-8",
    )
    skill_file = skills / "skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# Skill\n", encoding="utf-8")
    original_atomic_write = sad.safe_atomic_write_text
    atomic_writes: list[tuple[Path, str, str]] = []

    def track_atomic_write(
        path: Path,
        text: str,
        encoding: str = "utf-8",
    ) -> None:
        atomic_writes.append((path, text, encoding))
        original_atomic_write(path, text, encoding=encoding)

    monkeypatch.setattr(sad, "safe_atomic_write_text", track_atomic_write)

    _run_stdin_main(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": str(skill_file)}},
    )

    content = catalog.read_text(encoding="utf-8")
    assert content.count("| skill | skill |") == 1
    assert f"| skill | skill | 1 |  | `{skill_file}` |" in content
    assert "Keep this prose | skill | mention unchanged." in content
    assert atomic_writes == [(catalog, content, "utf-8")]


def test_edit_refreshes_existing_catalog_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skills, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    skill_file = skills / "growing-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# Short\n", encoding="utf-8")
    _run_stdin_main(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": str(skill_file)}},
    )
    skill_file.write_text("\n".join(f"line {i}" for i in range(181)), encoding="utf-8")
    monkeypatch.setattr(
        sad,
        "maybe_convert_to_micro_skill",
        lambda *_args: (False, "test conversion disabled"),
    )

    _run_stdin_main(
        monkeypatch,
        {"tool_name": "Edit", "tool_input": {"file_path": str(skill_file)}},
    )

    content = catalog.read_text(encoding="utf-8")
    assert content.count("| growing-skill | skill |") == 1
    assert f"| growing-skill | skill | 181 | ⚠ | `{skill_file}` |" in content


def test_main_installed_stdin_long_skill_hands_off_to_conversion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skills, wiki, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    skill_file = skills / "long-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text(
        "---\nname: long-skill\ndescription: Long skill\n---\n\n"
        + "\n".join(f"- ensure item {i}" for i in range(190)),
        encoding="utf-8",
    )
    line_count = len(skill_file.read_text(encoding="utf-8").splitlines())

    _run_stdin_main(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": str(skill_file)}},
    )

    content = catalog.read_text(encoding="utf-8")
    assert f"| long-skill | skill | {line_count} | ⚠ | `{skill_file}` |" in content
    converted = wiki / "converted" / "long-skill"
    assert "When this skill triggers, execute the following gated pipeline." in (
        (converted / "SKILL.md").read_text(encoding="utf-8")
    )
    assert (converted / "references" / "01-scope.md").is_file()
    assert "micro-skill gate converted" in capsys.readouterr().out


@pytest.mark.parametrize(
    "payload",
    [
        "{not-json",
        {"tool_name": "Write", "tool_input": {"file_path": 42}},
        {"tool_name": "Write", "tool_input": {"file_path": "\x00SKILL.md"}},
    ],
)
def test_main_installed_stdin_malformed_input_is_nonblocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: object,
) -> None:
    _, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    original_catalog = catalog.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_stdin_main(monkeypatch, payload)

    assert exc_info.value.code == 0
    assert catalog.read_text(encoding="utf-8") == original_catalog


def test_concurrent_stdin_processes_preserve_distinct_catalog_rows(tmp_path: Path) -> None:
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    skills = claude_dir / "skills"
    wiki = claude_dir / "skill-wiki"
    skills.mkdir(parents=True)
    wiki.mkdir()
    catalog = wiki / "catalog.md"
    catalog.write_text(
        "| Name | Type | Lines | Over limit | Path |\n| --- | --- | ---: | --- | --- |\n",
        encoding="utf-8",
    )
    (claude_dir / "skill-registry.json").write_text(
        json.dumps({"skill_dirs": [str(skills)]}),
        encoding="utf-8",
    )

    coordinator = tmp_path / "coordinator"
    coordinator.mkdir()
    (coordinator / "sitecustomize.py").write_text(
        """\
import os
from contextlib import contextmanager
from pathlib import Path

from ctx.utils import _file_lock

_real_file_lock = _file_lock.file_lock

@contextmanager
def _coordinated_file_lock(target, timeout=10.0):
    Path(os.environ["CTX_TEST_LOCK_ATTEMPT"]).touch()
    with _real_file_lock(target, timeout=timeout):
        yield

_file_lock.file_lock = _coordinated_file_lock
""",
        encoding="utf-8",
    )
    base_env = os.environ.copy()
    python_paths = [str(coordinator), str(SRC_DIR)]
    if existing_pythonpath := base_env.get("PYTHONPATH"):
        python_paths.append(existing_pythonpath)
    base_env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONPATH": os.pathsep.join(python_paths),
        }
    )

    processes: list[subprocess.Popen[str]] = []
    markers: list[Path] = []
    try:
        with file_lock(catalog):
            for skill_name in ("concurrent-alpha", "concurrent-beta"):
                skill_file = skills / skill_name / "SKILL.md"
                skill_file.parent.mkdir()
                skill_file.write_text(f"# {skill_name}\n", encoding="utf-8")
                payload_path = tmp_path / f"{skill_name}.json"
                payload_path.write_text(
                    json.dumps(
                        {
                            "tool_name": "Write",
                            "tool_input": {"file_path": str(skill_file)},
                        }
                    ),
                    encoding="utf-8",
                )
                marker = tmp_path / f"{skill_name}.lock-attempt"
                env = base_env | {"CTX_TEST_LOCK_ATTEMPT": str(marker)}
                with payload_path.open(encoding="utf-8") as stdin:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "skill_add_detector", "--from-stdin"],
                        stdin=stdin,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        env=env,
                    )
                processes.append(process)
                markers.append(marker)

            for marker, process in zip(markers, processes, strict=True):
                _wait_for_lock_attempt(marker, process)

        outputs = [process.communicate(timeout=10) for process in processes]
        assert [process.returncode for process in processes] == [0, 0], outputs
        content = catalog.read_text(encoding="utf-8")
        assert content.count("| concurrent-alpha | skill |") == 1
        assert content.count("| concurrent-beta | skill |") == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()


# ────────────────────────────────────────────────────────────────────
# validate_user_supplied_slug
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "valid",
    [
        "fastapi-pro",
        "my-skill",
        "a",
        "skill1",
        "a" * 64,  # exactly at the 64-char limit (1 start + 63 body)
        "0-zero-start",
    ],
)
def test_validate_skill_name_accepts_valid(valid: str) -> None:
    assert sad.validate_user_supplied_slug(valid) == valid


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "-leading-dash",  # starts with hyphen
        "UpperCase",  # uppercase not allowed
        "with.dot",  # dot not in pattern
        "with_underscore",  # underscore not in pattern
        "with space",  # space
        "valid\n",  # regex must consume the complete slug
        "../etc/passwd",  # traversal
        "a" * 65,  # too long (64 chars is max)
        "../../etc",  # traversal with multiple segments
    ],
)
def test_validate_skill_name_rejects_invalid(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid skill name"):
        sad.validate_user_supplied_slug(bad)


# ────────────────────────────────────────────────────────────────────
# Path traversal: resolved path gives real name
# ────────────────────────────────────────────────────────────────────


def test_main_rejects_traversal_without_changing_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skills, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    (skills / "nested").mkdir()
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("# Outside\n", encoding="utf-8")
    traversal = skills / "nested" / ".." / ".." / "outside" / "SKILL.md"
    assert traversal.is_file()
    assert traversal.resolve(strict=True) == outside
    original_catalog = catalog.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_stdin_main(
            monkeypatch,
            {"tool_name": "Write", "tool_input": {"file_path": str(traversal)}},
        )

    assert exc_info.value.code == 0
    assert catalog.read_text(encoding="utf-8") == original_catalog


def test_main_rejects_in_root_symlink_to_outside_skill(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skills, _, catalog = _configure_installed_paths(monkeypatch, tmp_path)
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("# Outside\n", encoding="utf-8")
    linked_skill = skills / "linked-skill" / "SKILL.md"
    linked_skill.parent.mkdir()
    try:
        linked_skill.symlink_to(outside)
    except NotImplementedError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")
    except OSError as exc:
        unsupported_errno = exc.errno in {
            errno.EACCES,
            errno.EPERM,
            errno.ENOSYS,
            errno.EOPNOTSUPP,
        }
        unsupported_windows_privilege = getattr(exc, "winerror", None) == 1314
        if not (unsupported_errno or unsupported_windows_privilege):
            raise
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    assert linked_skill.is_symlink()
    assert linked_skill.is_file()
    assert linked_skill.resolve(strict=True) == outside
    original_catalog = catalog.read_text(encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_stdin_main(
            monkeypatch,
            {"tool_name": "Write", "tool_input": {"file_path": str(linked_skill)}},
        )

    assert exc_info.value.code == 0
    assert catalog.read_text(encoding="utf-8") == original_catalog


def test_validate_blocks_traversal_in_name_directly() -> None:
    """A name that looks like a traversal is rejected before any FS access."""
    with pytest.raises(ValueError, match="invalid skill name"):
        sad.validate_user_supplied_slug("../etc")


# ────────────────────────────────────────────────────────────────────
# is_in_skill_dir
# ────────────────────────────────────────────────────────────────────


def test_is_in_skill_dir_true_for_file_inside(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    skill_file = skill_dir / "my-skill" / "SKILL.md"
    skill_file.parent.mkdir()
    skill_file.write_text("# skill")
    assert sad.is_in_skill_dir(str(skill_file), [str(skill_dir)]) is True


def test_is_in_skill_dir_false_for_file_outside(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    outside = tmp_path / "other" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text("# not a skill")
    assert sad.is_in_skill_dir(str(outside), [str(skill_dir)]) is False


def test_is_in_skill_dir_rejects_traversal_to_outside(tmp_path: Path) -> None:
    """A path using '..' that resolves outside the skill dir is rejected."""
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    # Construct a traversal: start inside skills but escape with ../..
    traversal = str(skill_dir / ".." / ".." / "evil" / "SKILL.md")
    # After resolve() this should not be inside skill_dir
    assert sad.is_in_skill_dir(traversal, [str(skill_dir)]) is False


def test_long_skill_is_micro_converted_from_hook_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    source_dir = tmp_path / "skills" / "long-skill"
    source_dir.mkdir(parents=True)
    source = source_dir / "SKILL.md"
    source.write_text(
        "---\nname: long-skill\ndescription: Long skill\n---\n\n"
        + "\n".join(f"- ensure item {i}" for i in range(190)),
        encoding="utf-8",
    )
    monkeypatch.setattr(sad, "WIKI_DIR", wiki)
    monkeypatch.setattr(sad, "LINE_THRESHOLD", 180)

    converted, detail = sad.maybe_convert_to_micro_skill(source, "long-skill", 195)

    assert converted is True
    assert detail == str(wiki / "converted" / "long-skill")
    assert source.exists()
    converted_skill = wiki / "converted" / "long-skill" / "SKILL.md"
    assert "When this skill triggers, execute the following gated pipeline." in (
        converted_skill.read_text(encoding="utf-8")
    )
    assert (wiki / "converted" / "long-skill" / "references" / "01-scope.md").is_file()
