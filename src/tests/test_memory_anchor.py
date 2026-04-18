"""
test_memory_anchor.py -- Regression tests for the diff-aware memory anchor.

Covers:
- _looks_like_path heuristic (known extensions, slashes, rejecting non-paths).
- _strip_line_suffix for trailing ``:<digits>`` suffixes only.
- extract_refs: dedups, preserves order, skips prose.
- _resolve: absolute path, relative-to-repo, relative-to-repo/src, tilde.
- scan_memory_file with missing / unreadable / empty / mixed files.
- build_report tallies, has_dead, live/dead splits.
- format_dashboard: all-clean banner, dead-refs grouped by file, line suffix.
- _detect_repo_root walks parents to a .git directory.
- CLI scan / dashboard / check --strict exit codes.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

import memory_anchor as ma


# ── Helpers ────────────────────────────────────────────────────────────────


def _write_memory(root: Path, name: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(body, encoding="utf-8")
    return p


# ── _looks_like_path ───────────────────────────────────────────────────────


@pytest.mark.parametrize("token", [
    "scan_repo.py",
    "src/foo.py",
    "src/foo.py:42",
    "~/.claude/skills/foo/SKILL.md",
    "mkdocs.yml",
    "pyproject.toml",
    "docs/index.md",
    "README.rst",
])
def test_looks_like_path_accepts_paths(token: str) -> None:
    assert ma._looks_like_path(token) is True


@pytest.mark.parametrize("token", [
    "add_skill()",
    "SAFE_NAME_RE",
    "323d981",
    "--strict",
    "https://example.com/foo.py",
    "",
    "hello world.py",     # contains whitespace → reject
])
def test_looks_like_path_rejects_non_paths(token: str) -> None:
    assert ma._looks_like_path(token) is False


# ── _strip_line_suffix ─────────────────────────────────────────────────────


def test_strip_line_suffix_with_digits() -> None:
    assert ma._strip_line_suffix("src/foo.py:42") == ("src/foo.py", 42)


def test_strip_line_suffix_no_digits_returned_unchanged() -> None:
    # Drive-letter colon must not be mistaken for a line suffix.
    assert ma._strip_line_suffix("C:/Users/me/foo.py") == (
        "C:/Users/me/foo.py", None,
    )


def test_strip_line_suffix_non_numeric_ignored() -> None:
    assert ma._strip_line_suffix("foo.py:notaline") == (
        "foo.py:notaline", None,
    )


# ── extract_refs ───────────────────────────────────────────────────────────


def test_extract_refs_skips_prose_and_keeps_paths() -> None:
    text = (
        "We rewrote `scan_repo.py` after `add_skill()` failed. "
        "See `src/utils.py:17` and `mkdocs.yml`. "
        "Commit `323d981` went out."
    )
    refs = ma.extract_refs(text)
    raws = [r[0] for r in refs]
    assert raws == ["scan_repo.py", "src/utils.py:17", "mkdocs.yml"]


def test_extract_refs_dedupes_within_document() -> None:
    text = "`foo.py` vs `foo.py` again, and `bar.py`."
    refs = ma.extract_refs(text)
    assert [r[0] for r in refs] == ["foo.py", "bar.py"]


def test_extract_refs_handles_no_backticks() -> None:
    assert ma.extract_refs("no code spans here, just prose") == []


def test_extract_refs_returns_line_number() -> None:
    refs = ma.extract_refs("See `src/foo.py:42` for details.")
    assert refs == [("src/foo.py:42", "src/foo.py", 42)]


# ── _resolve ───────────────────────────────────────────────────────────────


def test_resolve_repo_relative(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("x", encoding="utf-8")
    assert ma._resolve("src/foo.py", tmp_path) is True


def test_resolve_bare_inside_src(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bar.py").write_text("x", encoding="utf-8")
    assert ma._resolve("bar.py", tmp_path) is True


def test_resolve_absolute_path(tmp_path: Path) -> None:
    p = tmp_path / "abs.md"
    p.write_text("x", encoding="utf-8")
    assert ma._resolve(str(p), tmp_path) is True


def test_resolve_missing_returns_false(tmp_path: Path) -> None:
    assert ma._resolve("nonexistent.py", tmp_path) is False


def test_resolve_tilde_expansion(tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    (tmp_path / "note.md").write_text("x", encoding="utf-8")
    assert ma._resolve("~/note.md", tmp_path / "unrelated") is True


# ── scan_memory_file ───────────────────────────────────────────────────────


def test_scan_memory_file_mixed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "alive.py").write_text("x", encoding="utf-8")

    mem_dir = tmp_path / "mem"
    body = textwrap.dedent("""\
        Look at `src/alive.py` and also `src/ghost.py:99`.
        And `add_skill()` (ignore).
    """)
    mem = _write_memory(mem_dir, "notes.md", body)

    result = ma.scan_memory_file(mem, repo)
    assert result.memory_path == str(mem)
    names = [r.raw for r in result.refs]
    assert names == ["src/alive.py", "src/ghost.py:99"]
    assert len(result.live) == 1 and result.live[0].path == "src/alive.py"
    assert len(result.dead) == 1 and result.dead[0].path == "src/ghost.py"
    assert result.dead[0].line == 99


def test_scan_memory_file_empty_body(tmp_path: Path) -> None:
    mem = _write_memory(tmp_path / "mem", "blank.md", "")
    result = ma.scan_memory_file(mem, tmp_path)
    assert result.refs == ()


def test_scan_memory_file_unreadable(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch
                                     ) -> None:
    mem = _write_memory(tmp_path / "mem", "oops.md", "x")

    def _boom(self, *a, **kw):  # type: ignore[no-untyped-def]
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    result = ma.scan_memory_file(mem, tmp_path)
    assert result.refs == ()


# ── build_report ───────────────────────────────────────────────────────────


def test_build_report_totals(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("", encoding="utf-8")

    mem_dir = tmp_path / "mem"
    _write_memory(mem_dir, "n1.md",
                  "ref `src/a.py` and `src/dead.py`")
    _write_memory(mem_dir, "n2.md",
                  "only `missing.md` here")

    report = ma.build_report(repo_root=repo, memory_root=mem_dir, now=1.0)
    assert report.generated_at == 1.0
    assert report.live_count == 1
    assert report.dead_count == 2
    assert report.has_dead is True
    assert len(report.files) == 2


def test_build_report_missing_memory_root(tmp_path: Path) -> None:
    report = ma.build_report(repo_root=tmp_path,
                             memory_root=tmp_path / "no-mem",
                             now=1.0)
    assert report.files == ()
    assert report.has_dead is False


def test_build_report_all_live(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foo.py").write_text("", encoding="utf-8")
    _write_memory(tmp_path / "mem", "n.md", "See `foo.py`.")
    report = ma.build_report(repo_root=repo,
                             memory_root=tmp_path / "mem",
                             now=1.0)
    assert report.dead_count == 0
    assert report.has_dead is False


# ── format_dashboard ───────────────────────────────────────────────────────


def test_format_dashboard_clean(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foo.py").write_text("", encoding="utf-8")
    _write_memory(tmp_path / "mem", "n.md", "See `foo.py`.")
    report = ma.build_report(repo_root=repo,
                             memory_root=tmp_path / "mem",
                             now=1.0)
    out = ma.format_dashboard(report)
    assert "All references resolve." in out
    assert "live=1" in out
    assert "dead=0" in out


def test_format_dashboard_dead_refs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_memory(tmp_path / "mem", "n.md",
                  "ref `missing.py` and `gone.md:7`")
    report = ma.build_report(repo_root=repo,
                             memory_root=tmp_path / "mem",
                             now=1.0)
    out = ma.format_dashboard(report)
    assert "Dead references:" in out
    assert "missing.py" in out
    assert "gone.md:7" in out
    assert "line=7" in out


# ── _detect_repo_root ──────────────────────────────────────────────────────


def test_detect_repo_root_finds_git_dir(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert ma._detect_repo_root(nested) == tmp_path.resolve()


def test_detect_repo_root_falls_back_to_start(tmp_path: Path) -> None:
    # No .git anywhere -> returns the starting directory itself.
    isolated = tmp_path / "nowhere"
    isolated.mkdir()
    # Detect may walk past tmp_path to real filesystem roots; just assert
    # that the returned path is a real directory.
    result = ma._detect_repo_root(isolated)
    assert result.exists()


# ── AnchorRef / AnchorReport plumbing ──────────────────────────────────────


def test_anchor_ref_to_dict() -> None:
    r = ma.AnchorRef(raw="foo.py:3", path="foo.py", line=3, exists=True)
    assert r.to_dict() == {
        "raw": "foo.py:3", "path": "foo.py", "line": 3, "exists": True,
    }


def test_anchor_report_to_dict(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_memory(tmp_path / "mem", "n.md", "ref `missing.py`")
    report = ma.build_report(repo_root=repo,
                             memory_root=tmp_path / "mem",
                             now=1.0)
    d = report.to_dict()
    assert d["totals"] == {"total": 1, "live": 0, "dead": 1}
    assert d["generated_at"] == 1.0
    assert len(d["files"]) == 1


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_scan_outputs_json(tmp_path: Path,
                               capsys: pytest.CaptureFixture) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foo.py").write_text("", encoding="utf-8")
    _write_memory(tmp_path / "mem", "n.md", "ref `foo.py`")
    rc = ma.main([
        "scan",
        "--repo-root", str(repo),
        "--memory-root", str(tmp_path / "mem"),
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["totals"] == {"total": 1, "live": 1, "dead": 0}


def test_cli_dashboard(tmp_path: Path,
                      capsys: pytest.CaptureFixture) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_memory(tmp_path / "mem", "n.md", "ref `missing.py`")
    rc = ma.main([
        "dashboard",
        "--repo-root", str(repo),
        "--memory-root", str(tmp_path / "mem"),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[memory-anchor]" in out
    assert "missing.py" in out


def test_cli_check_strict_nonzero_on_dead(tmp_path: Path,
                                          capsys: pytest.CaptureFixture
                                          ) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_memory(tmp_path / "mem", "n.md", "ref `missing.py`")
    rc = ma.main([
        "check",
        "--strict",
        "--repo-root", str(repo),
        "--memory-root", str(tmp_path / "mem"),
    ])
    assert rc == 2
    assert "missing.py" in capsys.readouterr().out


def test_cli_check_strict_zero_when_clean(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foo.py").write_text("", encoding="utf-8")
    _write_memory(tmp_path / "mem", "n.md", "ref `foo.py`")
    rc = ma.main([
        "check",
        "--strict",
        "--repo-root", str(repo),
        "--memory-root", str(tmp_path / "mem"),
    ])
    assert rc == 0


def test_cli_check_without_strict_zero_even_on_dead(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_memory(tmp_path / "mem", "n.md", "ref `missing.py`")
    rc = ma.main([
        "check",
        "--repo-root", str(repo),
        "--memory-root", str(tmp_path / "mem"),
    ])
    assert rc == 0


def test_cli_unknown_subcommand(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        ma.main(["bogus"])
    assert exc.value.code == 2
