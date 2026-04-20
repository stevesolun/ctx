"""
tests/test_mcp_fetch_cli.py -- Smoke tests for the ctx-mcp-fetch CLI.

Contracts tested (Phase 2a locked spec):
  - --list-sources exits 0 and prints a known source name
  - No args -> exits 1 with error on stderr
  - --source unknown -> exits non-zero with error message
  - --source <name> --limit 2 with mocked source -> exactly 2 JSONL lines on stdout
  - Each output line parses as valid JSON
  - Source raising in fetch() -> CLI exits non-zero, error visible on stderr
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    import mcp_fetch  # type: ignore[import-untyped]

    _IMPORT_OK = True
except ImportError:
    _IMPORT_OK = False

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK, reason="awaits Phase 2a wiring: mcp_fetch not yet present"
)


# ---------------------------------------------------------------------------
# Fake source helpers
# ---------------------------------------------------------------------------


class _FakeSource:
    """Minimal Source-protocol implementation for CLI smoke tests."""

    def __init__(
        self,
        name: str = "fake-source",
        records: list[dict[str, Any]] | None = None,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self.name = name
        self.homepage = "https://example.com/fake"
        self._records = records or [
            {"name": f"tool-{i}", "description": f"desc {i}", "sources": ["fake-source"]}
            for i in range(10)
        ]
        self._raise_on_fetch = raise_on_fetch

    def fetch(
        self, *, limit: int | None = None, refresh: bool = False
    ) -> Iterator[dict]:
        if self._raise_on_fetch is not None:
            raise self._raise_on_fetch
        records = self._records
        if limit is not None:
            records = records[:limit]
        return iter(records)


def _patch_sources(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeSource
) -> None:
    """Inject a single fake source into mcp_fetch.SOURCES."""
    monkeypatch.setattr(mcp_fetch, "SOURCES", {fake.name: fake})


# ---------------------------------------------------------------------------
# --list-sources
# ---------------------------------------------------------------------------


class TestListSources:
    def test_list_sources_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="awesome-mcp"))
        monkeypatch.setattr(sys, "argv", ["ctx-mcp-fetch", "--list-sources"])
        with pytest.raises(SystemExit) as exc_info:
            mcp_fetch.main()
        assert exc_info.value.code == 0

    def test_list_sources_prints_source_name(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="awesome-mcp"))
        monkeypatch.setattr(sys, "argv", ["ctx-mcp-fetch", "--list-sources"])
        with pytest.raises(SystemExit):
            mcp_fetch.main()
        out = capsys.readouterr().out
        assert "awesome-mcp" in out


# ---------------------------------------------------------------------------
# No args / missing required args
# ---------------------------------------------------------------------------


class TestNoArgs:
    def test_no_args_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ctx-mcp-fetch"])
        with pytest.raises(SystemExit) as exc_info:
            mcp_fetch.main()
        assert exc_info.value.code != 0

    def test_no_args_error_visible(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["ctx-mcp-fetch"])
        with pytest.raises(SystemExit):
            mcp_fetch.main()
        captured = capsys.readouterr()
        # Error should appear on stderr (argparse default) or stdout
        error_output = captured.err + captured.out
        assert error_output.strip() != "", "Expected some error output for missing args"


# ---------------------------------------------------------------------------
# Unknown source
# ---------------------------------------------------------------------------


class TestUnknownSource:
    def test_unknown_source_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="awesome-mcp"))
        monkeypatch.setattr(sys, "argv", ["ctx-mcp-fetch", "--source", "does-not-exist"])
        with pytest.raises(SystemExit) as exc_info:
            mcp_fetch.main()
        assert exc_info.value.code != 0

    def test_unknown_source_error_message_visible(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="awesome-mcp"))
        monkeypatch.setattr(sys, "argv", ["ctx-mcp-fetch", "--source", "does-not-exist"])
        with pytest.raises(SystemExit):
            mcp_fetch.main()
        captured = capsys.readouterr()
        error_output = captured.err + captured.out
        assert error_output.strip() != ""


# ---------------------------------------------------------------------------
# JSONL output with --limit
# ---------------------------------------------------------------------------


class TestJsonlOutput:
    def test_limit_2_prints_exactly_2_lines(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="fake-source"))
        monkeypatch.setattr(
            sys, "argv", ["ctx-mcp-fetch", "--source", "fake-source", "--limit", "2"]
        )
        with pytest.raises(SystemExit) as exc_info:
            mcp_fetch.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert len(lines) == 2, f"Expected 2 JSONL lines, got {len(lines)}: {out!r}"

    def test_output_lines_are_valid_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="fake-source"))
        monkeypatch.setattr(
            sys, "argv", ["ctx-mcp-fetch", "--source", "fake-source", "--limit", "2"]
        )
        with pytest.raises(SystemExit):
            mcp_fetch.main()
        out = capsys.readouterr().out
        for line in out.splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                pytest.fail(f"Output line is not valid JSON: {line!r} — {exc}")


# ---------------------------------------------------------------------------
# Error handling: source raises during fetch
# ---------------------------------------------------------------------------


class TestFetchError:
    def test_source_exception_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad_source = _FakeSource(
            name="bad-source",
            raise_on_fetch=RuntimeError("simulated fetch failure"),
        )
        _patch_sources(monkeypatch, bad_source)
        monkeypatch.setattr(
            sys, "argv", ["ctx-mcp-fetch", "--source", "bad-source"]
        )
        with pytest.raises(SystemExit) as exc_info:
            mcp_fetch.main()
        assert exc_info.value.code != 0

    def test_source_exception_error_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bad_source = _FakeSource(
            name="bad-source",
            raise_on_fetch=RuntimeError("simulated fetch failure"),
        )
        _patch_sources(monkeypatch, bad_source)
        monkeypatch.setattr(
            sys, "argv", ["ctx-mcp-fetch", "--source", "bad-source"]
        )
        with pytest.raises(SystemExit):
            mcp_fetch.main()
        captured = capsys.readouterr()
        # Error detail must surface somewhere — prefer stderr
        error_output = captured.err + captured.out
        assert error_output.strip() != "", "Expected error output when fetch raises"
