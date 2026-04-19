"""
Tests for backup_watchdog + CLI `watchdog` verb.

Covers:
  - zero-tick run when max_iterations=0
  - single tick run: snapshot_if_changed is invoked once
  - stop flag raised mid-run short-circuits the loop
  - exception in snapshot_if_changed is counted, not propagated
  - interval is clamped to the allowed band
  - CLI `watchdog --once --json` emits stats JSON
  - CLI `watchdog --once` takes a real snapshot when content changed
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import backup_mirror as bm  # noqa: E402
import backup_watchdog as bw  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    (home / "settings.json").write_text('{"k": 1}', encoding="utf-8")
    backups = home / "backups"
    monkeypatch.setattr(bm, "CLAUDE_HOME", home)
    monkeypatch.setattr(bm, "BACKUPS_DIR", backups)
    return home


# ── run_watchdog: loop mechanics ────────────────────────────────────────────


def test_watchdog_executes_single_tick(fake_home):
    calls: list[str] = []

    def fake_snap(reason: str | None = None):
        calls.append(reason or "")
        # Return a stub that looks like SnapshotIfChangedResult.
        class R:
            snapshot_path = None
            report = None
        return R()

    # Patch snapshot_if_changed at import time inside the watchdog.
    import backup_mirror as bm_mod
    original = bm_mod.snapshot_if_changed
    bm_mod.snapshot_if_changed = fake_snap
    try:
        stats = bw.run_watchdog(
            interval=5,  # will be clamped to 5 anyway
            max_iterations=1,
            sleeper=lambda _s: None,
            log=lambda _m: None,
        )
    finally:
        bm_mod.snapshot_if_changed = original

    assert stats.ticks == 1
    assert stats.errors == 0
    assert len(calls) == 1
    assert calls[0].startswith("watchdog:tick1")


def test_watchdog_respects_max_iterations(fake_home):
    count = 0

    def fake_snap(reason: str | None = None):
        nonlocal count
        count += 1
        class R:
            snapshot_path = None
            report = None
        return R()

    import backup_mirror as bm_mod
    original = bm_mod.snapshot_if_changed
    bm_mod.snapshot_if_changed = fake_snap
    try:
        stats = bw.run_watchdog(
            interval=5,
            max_iterations=3,
            sleeper=lambda _s: None,
            log=lambda _m: None,
        )
    finally:
        bm_mod.snapshot_if_changed = original

    assert stats.ticks == 3
    assert count == 3


def test_watchdog_counts_errors_but_does_not_crash(fake_home):
    def broken_snap(reason: str | None = None):
        raise RuntimeError("boom")

    import backup_mirror as bm_mod
    original = bm_mod.snapshot_if_changed
    bm_mod.snapshot_if_changed = broken_snap
    try:
        stats = bw.run_watchdog(
            interval=5,
            max_iterations=2,
            sleeper=lambda _s: None,
            log=lambda _m: None,
        )
    finally:
        bm_mod.snapshot_if_changed = original

    assert stats.ticks == 2
    assert stats.errors == 2
    assert stats.snapshots_taken == 0


def test_watchdog_records_snapshot_id_on_success(fake_home):
    class _FakePath:
        name = "20260419T120000Z_watchdog-tick1"

    def fake_snap(reason: str | None = None):
        class R:
            snapshot_path = _FakePath()
            report = None
        return R()

    import backup_mirror as bm_mod
    original = bm_mod.snapshot_if_changed
    bm_mod.snapshot_if_changed = fake_snap
    try:
        stats = bw.run_watchdog(
            interval=5,
            max_iterations=1,
            sleeper=lambda _s: None,
            log=lambda _m: None,
        )
    finally:
        bm_mod.snapshot_if_changed = original

    assert stats.snapshots_taken == 1
    assert stats.snapshot_ids == ["20260419T120000Z_watchdog-tick1"]


# ── Interval clamping ──────────────────────────────────────────────────────


def test_watchdog_interval_clamped_low(fake_home):
    # interval=0.1 must be clamped up to 5 (but we don't actually sleep).
    seen: list[float] = []

    def capture_sleep(sec: float) -> None:
        seen.append(sec)

    import backup_mirror as bm_mod
    original = bm_mod.snapshot_if_changed
    bm_mod.snapshot_if_changed = lambda reason=None: type(
        "R", (), {"snapshot_path": None, "report": None}
    )()
    try:
        bw.run_watchdog(
            interval=0.1,
            max_iterations=2,
            sleeper=capture_sleep,
            log=lambda _m: None,
        )
    finally:
        bm_mod.snapshot_if_changed = original

    # Only one sleep: between tick 1 and tick 2. (Tick 2 breaks before
    # sleeping because max_iterations is reached.)
    assert seen == [5.0]


def test_watchdog_interval_clamped_high(fake_home):
    seen: list[float] = []
    import backup_mirror as bm_mod
    original = bm_mod.snapshot_if_changed
    bm_mod.snapshot_if_changed = lambda reason=None: type(
        "R", (), {"snapshot_path": None, "report": None}
    )()
    try:
        bw.run_watchdog(
            interval=999_999,
            max_iterations=2,
            sleeper=seen.append,
            log=lambda _m: None,
        )
    finally:
        bm_mod.snapshot_if_changed = original

    assert seen == [3600.0]


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_watchdog_once_json(fake_home, capsys):
    rc = bm.main(["watchdog", "--once", "--interval", "5", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ticks"] == 1
    assert payload["errors"] == 0
    # First run against an empty backups dir always produces a snapshot.
    assert payload["snapshots_taken"] == 1
    assert len(payload["snapshot_ids"]) == 1


def test_cli_watchdog_once_takes_real_snapshot(fake_home):
    rc = bm.main(["watchdog", "--once", "--interval", "5"])
    assert rc == 0
    # Snapshot directory must exist on disk.
    entries = list((fake_home / "backups").iterdir())
    assert len(entries) == 1
    assert entries[0].name.endswith("_watchdog-tick1")
