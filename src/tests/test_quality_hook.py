"""
test_quality_hook.py -- Regression tests for hooks/quality_on_session_end.py.

Focuses on the pure helpers: ``_touched_slugs_since`` and the cutoff/state
roundtrip. The subprocess call to ``skill_quality.py recompute`` is
mocked — we're testing the hook's *decision* logic, not the scorer,
which has its own dedicated suite in ``test_skill_quality.py``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOKS = REPO_ROOT / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

import quality_on_session_end as qh  # noqa: E402


NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _write_events(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_touched_slugs_since_cutoff(tmp_path: Path) -> None:
    events = tmp_path / "skill-events.jsonl"
    _write_events(
        events,
        [
            {"event": "load", "skill": "old-one",
             "timestamp": _iso(NOW - timedelta(days=3))},
            {"event": "load", "skill": "fresh-one",
             "timestamp": _iso(NOW - timedelta(hours=1))},
            {"event": "load", "skill": "fresh-two",
             "timestamp": _iso(NOW - timedelta(minutes=10))},
            {"event": "load", "skill": "fresh-one",  # dup
             "timestamp": _iso(NOW - timedelta(minutes=5))},
        ],
    )
    cutoff = NOW - timedelta(hours=2)
    slugs = qh._touched_slugs_since(cutoff, events)
    assert slugs == ["fresh-one", "fresh-two"]


def test_touched_slugs_skips_malformed_lines(tmp_path: Path) -> None:
    events = tmp_path / "skill-events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        "not json\n"
        + json.dumps({"event": "load", "skill": "good",
                      "timestamp": _iso(NOW)})
        + "\n"
        + json.dumps({"event": "load", "skill": 42,
                      "timestamp": _iso(NOW)})  # non-string skill
        + "\n",
        encoding="utf-8",
    )
    slugs = qh._touched_slugs_since(NOW - timedelta(hours=1), events)
    assert slugs == ["good"]


def test_touched_slugs_missing_file_returns_empty(tmp_path: Path) -> None:
    slugs = qh._touched_slugs_since(NOW, tmp_path / "does-not-exist.jsonl")
    assert slugs == []


def test_touched_slugs_caps_at_max(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(qh, "_MAX_SLUGS_PER_RUN", 3, raising=True)
    events = tmp_path / "skill-events.jsonl"
    _write_events(
        events,
        [
            {"event": "load", "skill": f"s{i}",
             "timestamp": _iso(NOW - timedelta(minutes=i))}
            for i in range(10)
        ],
    )
    slugs = qh._touched_slugs_since(NOW - timedelta(hours=1), events)
    assert len(slugs) == 3


def test_read_cutoff_uses_state_file(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"last_run_at": _iso(NOW - timedelta(hours=2))}),
        encoding="utf-8",
    )
    monkeypatch.setattr(qh, "_STATE_PATH", state, raising=True)
    cutoff = qh._read_cutoff()
    assert abs((cutoff - (NOW - timedelta(hours=2))).total_seconds()) < 5


def test_read_cutoff_falls_back_when_state_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(qh, "_STATE_PATH", tmp_path / "absent.json",
                        raising=True)
    cutoff = qh._read_cutoff()
    # Default lookback is 24h; cutoff should be in the past but not ancient.
    delta = datetime.now(timezone.utc) - cutoff
    assert 0 < delta.total_seconds() <= 25 * 3600


def test_write_state_roundtrip(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    monkeypatch.setattr(qh, "_STATE_PATH", state, raising=True)
    qh._write_state(NOW)
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["last_run_at"].startswith("2026-04-19")


def test_invoke_recompute_noops_on_empty(monkeypatch) -> None:
    called = {"n": 0}

    def _bad_subprocess(*a, **kw):
        called["n"] += 1
        raise AssertionError("should not be called")

    monkeypatch.setattr(qh.subprocess, "run", _bad_subprocess, raising=True)
    assert qh._invoke_recompute([]) == 0
    assert called["n"] == 0


def test_main_exits_zero_even_when_subprocess_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    # Point everything at tmp so no real state is touched.
    events = tmp_path / "skill-events.jsonl"
    _write_events(events, [{"event": "load", "skill": "demo",
                            "timestamp": _iso(NOW)}])
    monkeypatch.setattr(qh, "_EVENTS_PATH", events, raising=True)
    monkeypatch.setattr(qh, "_STATE_PATH", tmp_path / "state.json",
                        raising=True)

    def _boom(*a, **kw):
        raise OSError("pretend the subprocess exploded")

    monkeypatch.setattr(qh.subprocess, "run", _boom, raising=True)
    # Provide empty stdin.
    monkeypatch.setattr("sys.stdin", _StdinStub(""))
    rc = qh.main()
    assert rc == 0  # hook never propagates errors


class _StdinStub:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text
