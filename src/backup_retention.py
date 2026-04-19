"""
backup_retention.py -- Policy-aware snapshot retention planning.

Pure-function module: given a list of :class:`SnapshotInfo` and a
:class:`BackupRetention` policy, return the snapshots that should be
kept and the snapshots that should be deleted. No filesystem writes
happen here — the caller decides whether to act on the plan.

Policy semantics
----------------

``keep_latest = N``
    Always keep the ``N`` most-recent snapshots (by ``created_at``).

``keep_daily = M``
    For each of the ``M`` most-recent calendar days (UTC) that have
    at least one snapshot, keep the newest snapshot from that day.

The protected set is the **union** of the two rules. A snapshot only
gets pruned when it is in neither set. When both rules are zero, every
snapshot is prunable — that is the opt-in "keep nothing" setting.

Snapshots with ``created_at <= 0`` (malformed manifest / missing
timestamp) are always protected. We refuse to delete something we
can't confidently place in time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from backup_config import BackupRetention


@dataclass(frozen=True)
class RetentionPlan:
    """Result of planning a prune pass against a retention policy."""

    keep: tuple[str, ...]       # snapshot_ids preserved
    delete: tuple[str, ...]     # snapshot_ids to prune
    protected_by_latest: tuple[str, ...]  # subset of keep, by keep_latest rule
    protected_by_daily: tuple[str, ...]   # subset of keep, by keep_daily rule

    def to_dict(self) -> dict:
        return {
            "keep": list(self.keep),
            "delete": list(self.delete),
            "protected_by_latest": list(self.protected_by_latest),
            "protected_by_daily": list(self.protected_by_daily),
        }


def _utc_date(epoch: float) -> str:
    """YYYY-MM-DD string for a Unix timestamp, UTC."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def plan_prune(
    snapshots: Sequence,  # Sequence[SnapshotInfo]
    retention: BackupRetention,
    *,
    now: float | None = None,
) -> RetentionPlan:
    """Decide which snapshots survive a retention sweep.

    Parameters
    ----------
    snapshots
        Iterable of objects with ``snapshot_id: str`` and
        ``created_at: float`` attributes. Order does not matter.
    retention
        Policy to apply.
    now
        UTC timestamp treated as "right now" for the keep_daily window.
        Defaults to :func:`time.time`. Tests pin this for determinism.

    Returns
    -------
    RetentionPlan
        Structured report of which snapshot IDs should be kept and
        which can be deleted. Never raises on empty input.
    """
    if retention.keep_latest < 0 or retention.keep_daily < 0:
        raise ValueError("retention.keep_latest and keep_daily must be >= 0")

    now_ts = now if now is not None else time.time()
    items = list(snapshots)

    # Stable newest-first ordering; ties broken by snapshot_id so the
    # plan is deterministic even when two snapshots share a timestamp.
    items.sort(key=lambda s: (-float(s.created_at), s.snapshot_id))

    # Any snapshot missing a usable timestamp is preserved untouched.
    # Rationale: we never want to silently delete something we can't
    # place in time — that's how operators lose forensic evidence.
    undated_ids = [s.snapshot_id for s in items if float(s.created_at) <= 0]
    dated = [s for s in items if float(s.created_at) > 0]

    # Rule 1 — keep_latest.
    latest_ids = [s.snapshot_id for s in dated[: retention.keep_latest]]

    # Rule 2 — keep_daily: newest snapshot per UTC day, for the most
    # recent M days that actually contain snapshots.
    daily_ids: list[str] = []
    if retention.keep_daily > 0:
        today = _utc_date(now_ts)
        seen_days: dict[str, str] = {}  # day → snapshot_id of the day's newest
        for s in dated:  # already newest-first
            day = _utc_date(float(s.created_at))
            # Don't protect future-dated snapshots under "past M days".
            if day > today:
                continue
            if day not in seen_days:
                seen_days[day] = s.snapshot_id
        # Take the M most-recent days that actually appeared.
        recent_days = sorted(seen_days.keys(), reverse=True)[: retention.keep_daily]
        daily_ids = [seen_days[d] for d in recent_days]

    protected = set(latest_ids) | set(daily_ids) | set(undated_ids)

    keep: list[str] = []
    delete: list[str] = []
    # Preserve newest-first ordering in both output tuples.
    for s in items:
        if s.snapshot_id in protected:
            keep.append(s.snapshot_id)
        else:
            delete.append(s.snapshot_id)

    return RetentionPlan(
        keep=tuple(keep),
        delete=tuple(delete),
        protected_by_latest=tuple(latest_ids),
        protected_by_daily=tuple(daily_ids),
    )
