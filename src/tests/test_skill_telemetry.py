"""
test_skill_telemetry.py -- Regression tests for skill_telemetry.

Covers the Phase 1 telemetry contract:

  - event validation: type enum, skill-name sanitization, empty session_id
  - round-trip: log_event -> read_events yields identical records
  - malformed-line tolerance: partial/corrupt lines are skipped
  - concurrent appends: two processes writing concurrently end up with
    two well-formed lines (no interleaving corruption)
  - retention window math: boundary and default-cap cases
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skill_telemetry as st  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Event validation
# ────────────────────────────────────────────────────────────────────


def _iso(offset_seconds: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat(timespec="seconds")


def test_event_accepts_all_defined_types() -> None:
    for kind in ("load", "unload", "override", "switch_away"):
        st.TelemetryEvent(event=kind, skill="x", timestamp=_iso(), session_id="s1")


def test_event_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="invalid event type"):
        st.TelemetryEvent(event="explode", skill="x", timestamp=_iso(), session_id="s1")


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "../etc/passwd",
        "skill/with/slash",
        "skill with space",
        "-leading-dash",
        "a" * 200,
    ],
)
def test_event_rejects_unsafe_skill_name(bad_name: str) -> None:
    with pytest.raises(ValueError, match="invalid skill name"):
        st.TelemetryEvent(event="load", skill=bad_name, timestamp=_iso(), session_id="s1")


def test_event_rejects_empty_session_id() -> None:
    with pytest.raises(ValueError, match="session_id"):
        st.TelemetryEvent(event="load", skill="x", timestamp=_iso(), session_id="")


def test_event_rejects_non_iso_timestamp() -> None:
    with pytest.raises(ValueError, match="timestamp must be ISO-8601"):
        st.TelemetryEvent(
            event="load", skill="x", timestamp="not-a-time", session_id="s1"
        )


# ────────────────────────────────────────────────────────────────────
# Round-trip
# ────────────────────────────────────────────────────────────────────


def test_log_event_and_read_events_round_trip(tmp_path: Path) -> None:
    events_path = tmp_path / "skill-events.jsonl"

    rec_a = st.log_event(
        "load", "python-patterns", "sess-1",
        meta={"source": "unit-test"}, path=events_path,
    )
    rec_b = st.log_event(
        "unload", "python-patterns", "sess-1", path=events_path,
    )
    rec_c = st.log_event(
        "override", "flask", "sess-1",
        meta={"replaced_with": "fastapi"}, path=events_path,
    )

    got = list(st.read_events(path=events_path))
    assert [e.event for e in got] == ["load", "unload", "override"]
    assert got[0].event_id == rec_a.event_id
    assert got[1].event_id == rec_b.event_id
    assert got[2].event_id == rec_c.event_id
    assert got[0].meta == {"source": "unit-test"}
    assert got[2].meta == {"replaced_with": "fastapi"}


def test_read_events_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert list(st.read_events(path=tmp_path / "nope.jsonl")) == []


def test_read_events_skips_malformed_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events_path = tmp_path / "skill-events.jsonl"
    # One valid event, one garbage line, one event missing a required key,
    # one blank line, one valid event.
    good1 = {
        "event": "load", "skill": "x", "timestamp": _iso(),
        "session_id": "s1", "event_id": "e1", "meta": {},
    }
    good2 = {
        "event": "unload", "skill": "x", "timestamp": _iso(),
        "session_id": "s1", "event_id": "e2", "meta": {},
    }
    missing_key = {"event": "load", "skill": "x", "timestamp": _iso()}
    events_path.write_text(
        json.dumps(good1) + "\n"
        + "not-json-at-all\n"
        + json.dumps(missing_key) + "\n"
        + "\n"
        + json.dumps(good2) + "\n",
        encoding="utf-8",
    )

    got = list(st.read_events(path=events_path))
    assert [e.event_id for e in got] == ["e1", "e2"]
    captured = capsys.readouterr()
    assert "skipping malformed event" in captured.err


# ────────────────────────────────────────────────────────────────────
# Concurrency
# ────────────────────────────────────────────────────────────────────


def test_concurrent_appends_produce_wellformed_lines(tmp_path: Path) -> None:
    events_path = tmp_path / "skill-events.jsonl"
    N = 32

    def worker(i: int) -> None:
        st.log_event("load", f"skill{i:03d}", f"sess-{i}", path=events_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(N)))

    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == N
    for line in lines:
        obj = json.loads(line)
        assert obj["event"] == "load"
        assert obj["session_id"].startswith("sess-")


# ────────────────────────────────────────────────────────────────────
# Retention window
# ────────────────────────────────────────────────────────────────────


def test_retention_window_floor_applies_below_threshold() -> None:
    # 30-min session at 20% = 6 min, below 20-min floor -> 1200 s
    assert st.retention_window_seconds(30 * 60) == pytest.approx(20 * 60)


def test_retention_window_fraction_applies_above_threshold() -> None:
    # 4-hour session at 20% = 48 min, above 20-min floor -> 48 * 60 s
    assert st.retention_window_seconds(4 * 3600) == pytest.approx(48 * 60)


def test_retention_window_rejects_negative_session() -> None:
    with pytest.raises(ValueError):
        st.retention_window_seconds(-1)


def test_retention_window_rejects_bad_fraction() -> None:
    with pytest.raises(ValueError):
        st.retention_window_seconds(60, fraction=1.5)


def test_is_retained_true_at_and_above_window() -> None:
    t0 = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=20)
    assert st.is_retained(t0.isoformat(), t1.isoformat(), session_seconds=600) is True


def test_is_retained_false_below_window() -> None:
    t0 = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=19, seconds=59)
    assert st.is_retained(t0.isoformat(), t1.isoformat(), session_seconds=600) is False


def test_is_retained_rejects_reversed_timestamps() -> None:
    t0 = datetime(2026, 4, 18, 12, 0, 0, tzinfo=timezone.utc)
    t1 = t0 - timedelta(minutes=1)
    with pytest.raises(ValueError):
        st.is_retained(t0.isoformat(), t1.isoformat(), session_seconds=600)


# ────────────────────────────────────────────────────────────────────
# Default path resolution
# ────────────────────────────────────────────────────────────────────


def test_default_events_path_under_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Reload the module with patched HOME so DEFAULT_EVENTS_PATH re-resolves.
    import importlib
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    reloaded = importlib.reload(st)
    expected = tmp_path / ".claude" / "skill-events.jsonl"
    assert reloaded.DEFAULT_EVENTS_PATH == expected
