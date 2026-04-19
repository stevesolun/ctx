"""
backup_watchdog.py -- Polling watchdog that snapshots on change.

Complements the PostToolUse hook: the hook catches edits that happen
*inside* a Claude Code session, while the watchdog catches anything
else (manual IDE edits, git pull, external tooling) by polling
``snapshot_if_changed`` on a fixed interval.

Design
------

Zero dependencies — a simple loop that sleeps and calls into
:func:`backup_mirror.snapshot_if_changed`. Change detection is already
SHA-256-gated, so polling is cheap even at 30–60 s intervals.

The loop is fully testable because:

- the clock is injected (``sleeper`` / ``now``);
- the iteration count is capped (``max_iterations``);
- SIGINT / SIGTERM flip a shared flag rather than raise mid-snapshot.

Install
-------

Run it by hand::

    python src/backup_mirror.py watchdog --interval 60

Or register it as a background service using whatever init system fits
(Task Scheduler on Windows, systemd on Linux, launchd on macOS). See
``docs/backup-hook-install.md``.

Exit codes:
    0  stopped cleanly (SIGINT / SIGTERM / max_iterations reached)
    2  unrecoverable config error (e.g. BACKUPS_DIR not writable)
"""

from __future__ import annotations

import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Callable


# Clamp so a mis-configured interval cannot turn the watchdog into a
# busy loop or a near-dead process.
_MIN_INTERVAL_SEC = 5
_MAX_INTERVAL_SEC = 3600


@dataclass
class WatchdogStats:
    """Counters the loop emits to stderr on shutdown."""

    ticks: int = 0
    snapshots_taken: int = 0
    errors: int = 0
    # Snapshot IDs created during this run, newest last.
    snapshot_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ticks": self.ticks,
            "snapshots_taken": self.snapshots_taken,
            "errors": self.errors,
            "snapshot_ids": list(self.snapshot_ids),
        }


class _StopFlag:
    """Tiny holder so the signal handler can flip shared state."""

    def __init__(self) -> None:
        self.stop = False

    def set(self, *_args) -> None:  # signal handler signature
        self.stop = True


def _install_signal_handlers(flag: _StopFlag) -> list[tuple[int, object]]:
    """Install SIGINT/SIGTERM handlers, return previous so we can restore."""
    previous: list[tuple[int, object]] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev = signal.signal(sig, flag.set)
            previous.append((sig, prev))
        except (OSError, ValueError):
            # Non-main thread, or signal not supported on this platform;
            # watchdog still works, it just won't stop on that signal.
            pass
    return previous


def _restore_signal_handlers(previous: list[tuple[int, object]]) -> None:
    for sig, prev in previous:
        try:
            signal.signal(sig, prev)
        except (OSError, ValueError, TypeError):
            pass


def run_watchdog(
    *,
    interval: float = 60.0,
    reason_prefix: str = "watchdog",
    max_iterations: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] | None = None,
) -> WatchdogStats:
    """Run the polling watchdog loop.

    Parameters
    ----------
    interval
        Seconds between polls. Clamped to ``[5, 3600]``.
    reason_prefix
        Prefix for the ``--reason`` label on every snapshot.
    max_iterations
        When set, stop after N polls. Lets tests exit deterministically
        without relying on signals or wall-clock time.
    sleeper
        Injectable sleep function. Tests pass a no-op.
    log
        Injectable writer for diagnostic lines. Defaults to stderr.
    """
    # Intentional import cycle with backup_mirror (see the matching
    # comment at the top of that module). backup_mirror.cmd_watchdog
    # calls run_watchdog, and run_watchdog calls back into
    # backup_mirror.snapshot_if_changed — the call graph is cyclic by
    # design. Importing lazily here keeps the module import graph
    # acyclic so both modules load in either order.
    from backup_mirror import snapshot_if_changed  # noqa: PLC0415

    def emit(msg: str) -> None:
        if log is not None:
            log(msg)
        else:
            print(msg, file=sys.stderr)

    interval = max(_MIN_INTERVAL_SEC, min(_MAX_INTERVAL_SEC, float(interval)))
    stop = _StopFlag()
    previous = _install_signal_handlers(stop)
    stats = WatchdogStats()

    emit(f"[watchdog] start interval={interval:.1f}s prefix={reason_prefix!r}")

    try:
        while not stop.stop:
            stats.ticks += 1
            reason = f"{reason_prefix}:tick{stats.ticks}"
            try:
                result = snapshot_if_changed(reason=reason)
            except Exception as exc:  # noqa: BLE001  (never kill the loop)
                stats.errors += 1
                emit(f"[watchdog] tick {stats.ticks} error: {exc!r}")
            else:
                if result.snapshot_path is not None:
                    stats.snapshots_taken += 1
                    stats.snapshot_ids.append(result.snapshot_path.name)
                    emit(f"[watchdog] tick {stats.ticks} snapshot "
                         f"{result.snapshot_path.name}")

            if max_iterations is not None and stats.ticks >= max_iterations:
                break
            if stop.stop:
                break
            sleeper(interval)
    finally:
        _restore_signal_handlers(previous)

    emit(f"[watchdog] stop ticks={stats.ticks} "
         f"snapshots={stats.snapshots_taken} errors={stats.errors}")
    return stats
