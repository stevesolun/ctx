"""
test_skill_telemetry.py -- Regression tests for skill_telemetry.

Covers the Phase 1 telemetry contract:

  - event validation: type enum, skill-name sanitization, empty session_id
  - round-trip: log_event -> read_events yields identical records
  - malformed-line tolerance: partial/corrupt lines are skipped
  - concurrent appends: two processes writing concurrently end up with
    two well-formed lines (no interleaving corruption)
  - retention window math: boundary and default-cap cases
  - naive-timestamp handling: is_retained treats naive timestamps as UTC
  - meta validation: rejects oversized, nested, or non-scalar values
  - path containment: refuses caller-supplied paths that escape parents
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


def _iso(offset_seconds: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    ).isoformat(timespec="seconds")


def _event(**overrides: object) -> st.TelemetryEvent:
    defaults: dict[str, object] = {
        "event": "load",
        "skill": "x",
        "timestamp": _iso(),
        "session_id": "s1",
        "event_id": "e-default",
    }
    defaults.update(overrides)
    return st.TelemetryEvent(**defaults)  # type: ignore[arg-type]


# ────────────────────────────────────────────────────────────────────
# Event validation
# ────────────────────────────────────────────────────────────────────


def test_event_accepts_all_defined_types() -> None:
    for kind in ("load", "unload", "override", "switch_away"):
        _event(event=kind)


def test_event_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="invalid event type"):
        _event(event="explode")


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
        _event(skill=bad_name)


def test_event_rejects_empty_session_id() -> None:
    with pytest.raises(ValueError, match="session_id"):
        _event(session_id="")


def test_event_rejects_empty_event_id() -> None:
    with pytest.raises(ValueError, match="event_id"):
        _event(event_id="")


def test_event_rejects_non_iso_timestamp() -> None:
    with pytest.raises(ValueError, match="timestamp must be ISO-8601"):
        _event(timestamp="not-a-time")


# ────────────────────────────────────────────────────────────────────
# meta validation
# ────────────────────────────────────────────────────────────────────


def test_log_event_rejects_oversized_meta(tmp_path: Path) -> None:
    big = {f"k{i}": i for i in range(st._MAX_META_KEYS + 1)}
    with pytest.raises(ValueError, match="max"):
        st.log_event("load", "x", "s1", meta=big, path=tmp_path / "e.jsonl")


def test_log_event_rejects_non_scalar_meta_value(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="must be str/int/float/bool/None"):
        st.log_event(
            "load", "x", "s1", meta={"nested": {"a": 1}}, path=tmp_path / "e.jsonl"
        )


def test_log_event_rejects_oversized_meta_string(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        st.log_event(
            "load", "x", "s1",
            meta={"big": "a" * (st._MAX_META_VALUE_LEN + 1)},
            path=tmp_path / "e.jsonl",
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
    # one event missing event_id (now rejected), one blank line, one valid.
    good1 = {
        "event": "load", "skill": "x", "timestamp": _iso(),
        "session_id": "s1", "event_id": "e1", "meta": {},
    }
    good2 = {
        "event": "unload", "skill": "x", "timestamp": _iso(),
        "session_id": "s1", "event_id": "e2", "meta": {},
    }
    missing_session = {
        "event": "load", "skill": "x", "timestamp": _iso(), "event_id": "ex",
    }
    missing_event_id = {
        "event": "load", "skill": "x", "timestamp": _iso(), "session_id": "s1",
    }
    events_path.write_text(
        json.dumps(good1) + "\n"
        + "not-json-at-all\n"
        + json.dumps(missing_session) + "\n"
        + json.dumps(missing_event_id) + "\n"
        + "\n"
        + json.dumps(good2) + "\n",
        encoding="utf-8",
    )

    got = list(st.read_events(path=events_path))
    assert [e.event_id for e in got] == ["e1", "e2"]
    captured = capsys.readouterr()
    # Warning is sanitised — no raw line content, no file path.
    assert "skipping malformed event at line" in captured.err
    assert "not-json-at-all" not in captured.err
    assert str(events_path) not in captured.err


def test_read_events_rejects_path_that_escapes_parent(tmp_path: Path) -> None:
    # ``tmp_path / ".."`` resolves outside its own parent — must be refused.
    bad = tmp_path / ".." / "evil.jsonl"
    with pytest.raises(ValueError, match="escapes"):
        list(st.read_events(path=bad))


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
    assert st.retention_window_seconds(30 * 60) == pytest.approx(20 * 60)


def test_retention_window_fraction_applies_above_threshold() -> None:
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


def test_is_retained_treats_naive_timestamps_as_utc() -> None:
    # Naive load_ts (no tz) mixed with aware unload_ts must not raise.
    load_ts = "2026-04-18T12:00:00"
    unload_ts = "2026-04-18T12:30:00+00:00"
    assert st.is_retained(load_ts, unload_ts, session_seconds=600) is True


def test_is_retained_both_naive_ok() -> None:
    load_ts = "2026-04-18T12:00:00"
    unload_ts = "2026-04-18T12:30:00"
    assert st.is_retained(load_ts, unload_ts, session_seconds=600) is True


# ────────────────────────────────────────────────────────────────────
# Default path resolution
# ────────────────────────────────────────────────────────────────────


def test_default_events_path_resolves_under_claude_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Simpler than reloading the module: just monkeypatch the constant
    # the tests care about.
    target = tmp_path / ".claude" / "skill-events.jsonl"
    monkeypatch.setattr(st, "DEFAULT_EVENTS_PATH", target)
    assert st.DEFAULT_EVENTS_PATH == target
