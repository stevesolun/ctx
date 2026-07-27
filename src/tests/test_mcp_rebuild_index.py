"""Behavior tests for the ctx-mcp-rebuild-index user story."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import mcp_rebuild_index


def _run_main(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["ctx-mcp-rebuild-index", *args])
    with pytest.raises(SystemExit) as exit_info:
        mcp_rebuild_index.main()
    assert isinstance(exit_info.value.code, int)
    return exit_info.value.code


def test_cli_dry_run_then_write_rebuilds_real_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    mcp_dir = wiki / "entities" / "mcp-servers"
    page = mcp_dir / "a" / "alpha.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\nname: alpha\ngithub_url: https://github.com/Org/Alpha\n---\n# alpha\n",
        encoding="utf-8",
    )
    index_path = mcp_dir / ".canonical-index.json"

    assert _run_main(monkeypatch, "--wiki", str(wiki), "--dry-run") == 0
    assert "[dry-run] would index 1 entities" in capsys.readouterr().out
    assert not index_path.exists()

    assert _run_main(monkeypatch, "--wiki", str(wiki)) == 0
    assert "Canonical index rebuilt: 1 entities indexed" in capsys.readouterr().out
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["by_github_url"] == {
        "https://github.com/org/alpha": {"relpath": "a/alpha.md", "slug": "alpha"}
    }


def test_cli_missing_wiki_exits_two(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "missing"

    assert _run_main(monkeypatch, "--wiki", str(wiki)) == 2
    assert "MCP entity directory or wiki-packs do not exist" in capsys.readouterr().err


def test_cli_rebuild_failure_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "mcp-servers").mkdir(parents=True)

    def fail_rebuild(_path: Path, *, persist: bool):
        raise OSError(f"simulated failure persist={persist}")

    monkeypatch.setattr(mcp_rebuild_index, "rebuild_from_scan", fail_rebuild)

    assert _run_main(monkeypatch, "--wiki", str(wiki)) == 1
    assert "rebuild failed: simulated failure persist=True" in capsys.readouterr().err
