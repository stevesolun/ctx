"""
Tests for backup_retention + prune_by_policy.

Covers:
  - keep_latest protects the N newest snapshots
  - keep_daily protects one snapshot per UTC day for M days
  - protection sets unionise (never intersect before union)
  - keep_latest=0 and keep_daily=0 prunes everything
  - snapshots with missing/zero created_at are never deleted
  - future-dated snapshots are not counted toward keep_daily
  - dry-run reports the same plan without mutating disk
  - auto-prune runs after snapshot_if_changed and respects config
  - CLI: prune --policy --dry-run --json emits a plan
  - CLI: prune --keep still works (backward compat)
  - legacy prune_snapshots(keep=N) API is unchanged
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import backup_config as bc  # noqa: E402
import backup_mirror as bm  # noqa: E402
import backup_retention as br  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FakeSnap:
    """Minimal SnapshotInfo-lookalike for pure plan_prune tests."""

    snapshot_id: str
    created_at: float
    path: str = ""


def _force_retention(monkeypatch, *, keep_latest: int, keep_daily: int) -> None:
    """Replace bm._CFG with a clone that carries the desired retention.

    BackupConfig is frozen, so we can't mutate a single field — we swap
    the whole _CFG reference. dataclasses.replace handles everything
    else.
    """
    from dataclasses import replace
    new_cfg = replace(
        bm._CFG,
        retention=bc.BackupRetention(
            keep_latest=keep_latest, keep_daily=keep_daily,
        ),
    )
    monkeypatch.setattr(bm, "_CFG", new_cfg)


def _day_ts(day_offset: int, *, hour: int = 12) -> float:
    """Return a UTC epoch timestamp ``day_offset`` days before 2026-04-19 noon."""
    # 2026-04-19T12:00:00Z == 1776513600
    base = 1776513600
    return float(base - day_offset * 86_400 + (hour - 12) * 3600)


# ── plan_prune: keep_latest ─────────────────────────────────────────────────


def test_keep_latest_protects_n_newest():
    snaps = [
        FakeSnap(f"snap-{i}", _day_ts(i))
        for i in range(10)
    ]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=3, keep_daily=0),
                         now=_day_ts(0))
    assert plan.keep == ("snap-0", "snap-1", "snap-2")
    assert plan.delete == tuple(f"snap-{i}" for i in range(3, 10))
    assert plan.protected_by_latest == ("snap-0", "snap-1", "snap-2")
    assert plan.protected_by_daily == ()


def test_keep_latest_zero_prunes_all_when_daily_also_zero():
    snaps = [FakeSnap(f"snap-{i}", _day_ts(i)) for i in range(3)]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=0, keep_daily=0),
                         now=_day_ts(0))
    assert plan.keep == ()
    assert set(plan.delete) == {"snap-0", "snap-1", "snap-2"}


def test_keep_latest_larger_than_input_keeps_everything():
    snaps = [FakeSnap(f"snap-{i}", _day_ts(i)) for i in range(2)]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=99, keep_daily=0),
                         now=_day_ts(0))
    assert set(plan.keep) == {"snap-0", "snap-1"}
    assert plan.delete == ()


# ── plan_prune: keep_daily ──────────────────────────────────────────────────


def test_keep_daily_keeps_newest_per_day():
    # Three snapshots on day 0 (today), two on day 1, one on day 5.
    snaps = [
        FakeSnap("today-a", _day_ts(0, hour=9)),
        FakeSnap("today-b", _day_ts(0, hour=15)),   # newest of today
        FakeSnap("today-c", _day_ts(0, hour=12)),
        FakeSnap("yest-a",  _day_ts(1, hour=8)),
        FakeSnap("yest-b",  _day_ts(1, hour=20)),   # newest of yesterday
        FakeSnap("old",     _day_ts(5, hour=0)),    # outside 3-day window
    ]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=0, keep_daily=3),
                         now=_day_ts(0))
    # keep_daily=3 covers today, yesterday, and one more — but day-2, day-3,
    # day-4 have no snapshots. "M most recent days that actually contain
    # snapshots" means we grab today + yesterday + day-5.
    assert set(plan.protected_by_daily) == {"today-b", "yest-b", "old"}
    assert "today-a" in plan.delete
    assert "today-c" in plan.delete
    assert "yest-a" in plan.delete


def test_keep_daily_ignores_future_dated_snapshots():
    snaps = [
        FakeSnap("future", _day_ts(-2)),       # 2 days in the future
        FakeSnap("today",  _day_ts(0)),
        FakeSnap("yest",   _day_ts(1)),
    ]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=0, keep_daily=2),
                         now=_day_ts(0))
    # Future-dated snapshots don't consume the keep_daily budget.
    assert "future" not in plan.protected_by_daily
    assert set(plan.protected_by_daily) == {"today", "yest"}
    # The future-dated one is still in delete (not protected).
    assert "future" in plan.delete


def test_keep_daily_zero_keeps_nothing_on_its_own():
    snaps = [FakeSnap("a", _day_ts(0)), FakeSnap("b", _day_ts(1))]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=0, keep_daily=0),
                         now=_day_ts(0))
    assert plan.protected_by_daily == ()
    assert plan.keep == ()


# ── plan_prune: union semantics ─────────────────────────────────────────────


def test_protection_sets_union_not_intersect():
    # Newest-by-time AND newest-by-day both apply; union keeps both.
    snaps = [
        FakeSnap("today",    _day_ts(0, hour=12)),   # newest of today, newest overall
        FakeSnap("yest-old", _day_ts(1, hour=0)),
        FakeSnap("yest-new", _day_ts(1, hour=23)),   # newest of yesterday
        FakeSnap("older",    _day_ts(5, hour=0)),
    ]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=1, keep_daily=2),
                         now=_day_ts(0))
    # keep_latest=1 -> {today}; keep_daily=2 -> {today, yest-new}
    # union = {today, yest-new}
    assert set(plan.keep) == {"today", "yest-new"}
    assert set(plan.delete) == {"yest-old", "older"}


# ── plan_prune: safety net ──────────────────────────────────────────────────


def test_snapshots_with_zero_created_at_are_protected():
    # A malformed manifest gives created_at=0. We never delete those —
    # we can't place them in time, so operators get to see them.
    snaps = [
        FakeSnap("dated",    _day_ts(0)),
        FakeSnap("undated",  0.0),
        FakeSnap("negative", -1.0),
    ]
    plan = br.plan_prune(snaps, bc.BackupRetention(keep_latest=0, keep_daily=0),
                         now=_day_ts(0))
    assert "undated" in plan.keep
    assert "negative" in plan.keep
    assert "dated" in plan.delete


def test_negative_retention_rejected():
    # Defence in depth: even if someone bypassed BackupConfig's validation
    # to hand-construct a negative policy, plan_prune refuses to run
    # rather than silently deleting nothing or everything.
    bad = bc.BackupRetention.__new__(bc.BackupRetention)
    object.__setattr__(bad, "keep_latest", -1)
    object.__setattr__(bad, "keep_daily", 0)
    with pytest.raises(ValueError):
        br.plan_prune([], bad)


# ── prune_by_policy: integration on disk ────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    home.mkdir()
    backups = home / "backups"
    monkeypatch.setattr(bm, "CLAUDE_HOME", home)
    monkeypatch.setattr(bm, "BACKUPS_DIR", backups)
    (home / "settings.json").write_text('{"k": 1}', encoding="utf-8")
    return home


def _make_snapshot_at(fake_home: Path, created_at: float,
                      snapshot_id: str) -> Path:
    """Create a minimal fake snapshot folder with manifest.json."""
    backups = fake_home / "backups"
    backups.mkdir(exist_ok=True)
    snap = backups / snapshot_id
    snap.mkdir()
    (snap / "manifest.json").write_text(
        json.dumps({
            "snapshot_id": snapshot_id,
            "created_at": created_at,
            "claude_home": str(fake_home),
            "entries": [],
        }),
        encoding="utf-8",
    )
    return snap


def test_prune_by_policy_dry_run_mutates_nothing(fake_home):
    _make_snapshot_at(fake_home, _day_ts(0), "s-0")
    _make_snapshot_at(fake_home, _day_ts(3), "s-3")
    _make_snapshot_at(fake_home, _day_ts(7), "s-7")
    policy = bc.BackupRetention(keep_latest=1, keep_daily=0)
    plan = bm.prune_by_policy(retention=policy, dry_run=True, now=_day_ts(0))
    assert set(plan.delete) == {"s-3", "s-7"}
    # On-disk: nothing deleted.
    assert (fake_home / "backups" / "s-3").is_dir()
    assert (fake_home / "backups" / "s-7").is_dir()


def test_prune_by_policy_deletes_non_protected(fake_home):
    _make_snapshot_at(fake_home, _day_ts(0), "s-0")
    _make_snapshot_at(fake_home, _day_ts(3), "s-3")
    _make_snapshot_at(fake_home, _day_ts(7), "s-7")
    policy = bc.BackupRetention(keep_latest=1, keep_daily=0)
    plan = bm.prune_by_policy(retention=policy, now=_day_ts(0))
    assert set(plan.delete) == {"s-3", "s-7"}
    assert (fake_home / "backups" / "s-0").is_dir()
    assert not (fake_home / "backups" / "s-3").exists()
    assert not (fake_home / "backups" / "s-7").exists()


def test_snapshot_if_changed_triggers_autoprune(fake_home, monkeypatch):
    # Pre-seed two older snapshots, with recent-a newer than older-b.
    _make_snapshot_at(fake_home, _day_ts(10), "recent-a")
    _make_snapshot_at(fake_home, _day_ts(20), "older-b")
    _force_retention(monkeypatch, keep_latest=2, keep_daily=0)

    result = bm.snapshot_if_changed(reason="autoprune-test")
    assert result.snapshot_path is not None
    # keep_latest=2 -> newest snapshot (just taken) + recent-a survive;
    # older-b is evicted because it's the 3rd oldest.
    surviving = {p.name for p in (fake_home / "backups").iterdir() if p.is_dir()}
    assert result.snapshot_path.name in surviving
    assert "recent-a" in surviving
    assert "older-b" not in surviving


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_prune_policy_dry_run_json(fake_home, monkeypatch, capsys):
    _make_snapshot_at(fake_home, _day_ts(0), "s-0")
    _make_snapshot_at(fake_home, _day_ts(5), "s-5")
    _make_snapshot_at(fake_home, _day_ts(9), "s-9")
    # Shrink active config so the test is deterministic.
    _force_retention(monkeypatch, keep_latest=1, keep_daily=0)
    rc = bm.main(["prune", "--policy", "--dry-run", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert set(payload["delete"]) == {"s-5", "s-9"}
    # Dry-run: folders remain.
    assert (fake_home / "backups" / "s-5").is_dir()


def test_cli_prune_policy_text(fake_home, monkeypatch, capsys):
    _make_snapshot_at(fake_home, _day_ts(0), "s-0")
    _make_snapshot_at(fake_home, _day_ts(5), "s-5")
    _force_retention(monkeypatch, keep_latest=1, keep_daily=0)
    rc = bm.main(["prune", "--policy"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[prune]" in out
    assert "kept=1" in out
    assert "removed=1" in out


def test_cli_prune_requires_policy_or_keep(fake_home, capsys):
    rc = bm.main(["prune"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--policy" in err or "--keep" in err


def test_cli_prune_keep_still_works(fake_home, capsys):
    # Backward compat: existing test_backup_mirror.py covers this path too,
    # but we double-check here that the new dispatch doesn't break it.
    _make_snapshot_at(fake_home, _day_ts(0), "s-0")
    _make_snapshot_at(fake_home, _day_ts(1), "s-1")
    rc = bm.main(["prune", "--keep", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "kept 1 newest" in out
