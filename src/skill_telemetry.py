#!/usr/bin/env python3
"""
skill_telemetry.py -- Append-only event stream for skill/agent lifecycle.

Phase 1 of the skill-quality-scoring plan: captures raw load / unload /
override / switch_away events to a JSONL stream so later phases can
score behavioural signals. Read-only capture; no scoring, no
enforcement.

Events:
  - load         skill was loaded into the session manifest
  - unload       skill was explicitly unloaded
  - override     user overrode a skill suggestion (demoted or replaced)
  - switch_away  skill was loaded but the user moved to a different one

Storage: ~/.claude/skill-events.jsonl (append-only JSONL).
Concurrent writers are serialized via _file_lock.file_lock.

Retention helper:
  retention_window_seconds(session_seconds)
    = max(session_fraction * session_seconds, min_minutes * 60)

  is_retained(load_ts, unload_ts, session_seconds)
    returns True iff (unload - load) >= retention_window_seconds.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

sys.path.insert(0, str(Path(__file__).parent))
from _file_lock import file_lock  # noqa: E402

EVENT_TYPES = frozenset({"load", "unload", "override", "switch_away"})

# Same policy as wiki_utils.SAFE_NAME_RE: alnum start, alnum / _ / - / .
# inside, bounded length. Mirrors upstream sanitization.
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,127}$")

DEFAULT_EVENTS_PATH = Path(os.path.expanduser("~/.claude/skill-events.jsonl"))
DEFAULT_SESSION_FRACTION = 0.20
DEFAULT_MIN_RETENTION_MIN = 20.0


@dataclass(frozen=True)
class TelemetryEvent:
    """One immutable skill-lifecycle event."""

    event: str
    skill: str
    timestamp: str
    session_id: str
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.event not in EVENT_TYPES:
            raise ValueError(
                f"invalid event type {self.event!r}; expected one of {sorted(EVENT_TYPES)}"
            )
        if not isinstance(self.skill, str) or not _SKILL_NAME_RE.match(self.skill):
            raise ValueError(f"invalid skill name: {self.skill!r}")
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("session_id must be a non-empty string")
        # timestamp must parse as ISO-8601 so downstream readers don't choke
        try:
            datetime.fromisoformat(self.timestamp)
        except ValueError as exc:
            raise ValueError(f"timestamp must be ISO-8601: {self.timestamp!r}") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(
    event: str,
    skill: str,
    session_id: str,
    *,
    meta: Mapping[str, Any] | None = None,
    path: Path | None = None,
) -> TelemetryEvent:
    """Append a single event to the JSONL stream.

    Returns the event that was written (callers stash ``event_id`` to
    pair a later unload with its load).
    """
    target = Path(path) if path is not None else DEFAULT_EVENTS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    record = TelemetryEvent(
        event=event,
        skill=skill,
        timestamp=_now_iso(),
        session_id=session_id,
        meta=dict(meta or {}),
    )
    line = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n"

    with file_lock(target):
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    return record


def read_events(path: Path | None = None) -> Iterator[TelemetryEvent]:
    """Yield every event from the JSONL stream in on-disk order.

    Malformed lines are skipped with a stderr warning. Append-only
    storage should not need editing, but a crash mid-write can leave a
    partial trailing line — iteration must survive that.
    """
    target = Path(path) if path is not None else DEFAULT_EVENTS_PATH
    if not target.exists():
        return
    with open(target, encoding="utf-8") as fh:
        for ln, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                yield TelemetryEvent(
                    event=obj["event"],
                    skill=obj["skill"],
                    timestamp=obj["timestamp"],
                    session_id=obj["session_id"],
                    event_id=obj.get("event_id", uuid.uuid4().hex),
                    meta=obj.get("meta", {}),
                )
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                print(
                    f"skill_telemetry: skipping malformed event at {target}:{ln}: {exc}",
                    file=sys.stderr,
                )


def retention_window_seconds(
    session_seconds: float,
    *,
    fraction: float = DEFAULT_SESSION_FRACTION,
    min_minutes: float = DEFAULT_MIN_RETENTION_MIN,
) -> float:
    """Per plan: max(fraction * session, min_minutes * 60)."""
    if session_seconds < 0:
        raise ValueError("session_seconds must be >= 0")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be in [0, 1]")
    if min_minutes < 0:
        raise ValueError("min_minutes must be >= 0")
    return max(fraction * session_seconds, min_minutes * 60.0)


def is_retained(
    load_ts: str,
    unload_ts: str,
    session_seconds: float,
    *,
    fraction: float = DEFAULT_SESSION_FRACTION,
    min_minutes: float = DEFAULT_MIN_RETENTION_MIN,
) -> bool:
    """True iff the skill survived the retention window."""
    t0 = datetime.fromisoformat(load_ts)
    t1 = datetime.fromisoformat(unload_ts)
    delta = (t1 - t0).total_seconds()
    if delta < 0:
        raise ValueError("unload_ts must be >= load_ts")
    window = retention_window_seconds(
        session_seconds, fraction=fraction, min_minutes=min_minutes
    )
    return delta >= window
