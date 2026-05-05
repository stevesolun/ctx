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

import mcp_fetch  # type: ignore[import-untyped]  # noqa: E402


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
        captured = capsys.readouterr()
        assert captured.err == ""
        out = captured.out
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
        assert captured.out == ""
        assert "usage:" in captured.err
        assert "error:" in captured.err


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
        assert captured.out == ""
        assert "unknown source" in captured.err
        assert "does-not-exist" in captured.err
        assert "awesome-mcp" in captured.err


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
        captured = capsys.readouterr()
        assert captured.err == ""
        lines = [ln for ln in captured.out.splitlines() if ln.strip()]
        assert len(lines) == 2, (
            f"Expected 2 JSONL lines, got {len(lines)}: {captured.out!r}"
        )

    def test_output_lines_are_valid_json(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="fake-source"))
        monkeypatch.setattr(
            sys, "argv", ["ctx-mcp-fetch", "--source", "fake-source", "--limit", "2"]
        )
        with pytest.raises(SystemExit):
            mcp_fetch.main()
        captured = capsys.readouterr()
        assert captured.err == ""
        records = []
        for line in captured.out.splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                pytest.fail(f"Output line is not valid JSON: {line!r} — {exc}")


        assert [record["name"] for record in records] == ["tool-0", "tool-1"]

    def test_verbose_success_reports_progress_on_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _patch_sources(monkeypatch, _FakeSource(name="fake-source"))
        monkeypatch.setattr(
            sys,
            "argv",
            ["ctx-mcp-fetch", "--source", "fake-source", "--limit", "1", "-v"],
        )
        with pytest.raises(SystemExit) as exc_info:
            mcp_fetch.main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 0
        assert "[fake-source] emitted 1 record(s)" in captured.err


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
        assert captured.out == ""
        assert "bad-source" in captured.err
        assert "simulated fetch failure" in captured.err


class TestVerboseFlag:
    """Phase 6a: -v/--verbose wires up logging.basicConfig so library
    modules' logger calls (Phase 2.5 cleanup) become visible."""

    def test_configure_logging_no_verbosity_is_noop(self) -> None:
        # Zero verbosity must not touch basicConfig (avoid polluting
        # other tests' logging state).
        import logging  # noqa: PLC0415
        before = len(logging.getLogger().handlers)
        mcp_fetch._configure_logging(0)
        after = len(logging.getLogger().handlers)
        assert before == after, (
            "verbosity=0 must not add logging handlers"
        )

    def test_configure_logging_v1_sets_info_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging  # noqa: PLC0415

        captured: dict[str, object] = {}

        def _capture(**kwargs: object) -> None:
            captured.update(kwargs)

        monkeypatch.setattr(logging, "basicConfig", _capture)
        mcp_fetch._configure_logging(1)
        assert captured.get("level") == logging.INFO

    def test_configure_logging_v2_sets_debug_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging  # noqa: PLC0415

        captured: dict[str, object] = {}
        monkeypatch.setattr(logging, "basicConfig", lambda **kw: captured.update(kw))
        mcp_fetch._configure_logging(2)
        assert captured.get("level") == logging.DEBUG

    def test_verbose_flag_accepted_by_argparser(self) -> None:
        # -v and --verbose must not break the parser.
        parser = mcp_fetch._build_parser()
        # With -v alongside a required arg:
        args = parser.parse_args(["--list-sources", "-v"])
        assert args.verbose == 1

        args = parser.parse_args(["--list-sources", "-vv"])
        assert args.verbose == 2

        args = parser.parse_args(["--list-sources"])
        assert args.verbose == 0
