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
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

sys.path.insert(0, str(Path(__file__).parent))
from _file_lock import file_lock  # noqa: E402

_logger = logging.getLogger(__name__)

EVENT_TYPES = frozenset({"load", "unload", "override", "switch_away"})

# Same policy as wiki_utils.SAFE_NAME_RE: alnum start, alnum / _ / - / .
# inside, bounded length. Dots are allowed intentionally for names like
# "python3.11-patterns"; the bounded quantifier keeps the regex linear
# under pathological input (no ReDoS).
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,127}$")

DEFAULT_EVENTS_PATH = Path(os.path.expanduser("~/.claude/skill-events.jsonl"))
_ALLOWED_EVENTS_DIR = DEFAULT_EVENTS_PATH.parent.resolve()
DEFAULT_SESSION_FRACTION = 0.20
DEFAULT_MIN_RETENTION_MIN = 20.0

# Bounds on caller-supplied meta dicts. Keeps the log line small and
# prevents inadvertent leakage of large mappings like ``os.environ``
# (which would spill every env var — API keys included — into a
# world-readable file on disk).
_MAX_META_KEYS = 20
_MAX_META_VALUE_LEN = 512
_META_SCALAR_TYPES: tuple[type, ...] = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class TelemetryEvent:
    """One immutable skill-lifecycle event."""

    event: str
    skill: str
    timestamp: str
    session_id: str
    event_id: str
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
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be a non-empty string")
        # timestamp must parse as ISO-8601 so downstream readers don't choke
        try:
            datetime.fromisoformat(self.timestamp)
        except ValueError as exc:
            raise ValueError(f"timestamp must be ISO-8601: {self.timestamp!r}") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_event_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _validate_meta(meta: Mapping[str, Any]) -> None:
    """Reject meta dicts that could leak secrets or bloat the log.

    Keeps validation shallow on purpose — nested mappings are rejected
    outright rather than walked, since callers supplying nested state
    are almost always doing something they should think twice about.
    """
    if len(meta) > _MAX_META_KEYS:
        raise ValueError(
            f"meta has {len(meta)} keys; max {_MAX_META_KEYS}"
        )
    for k, v in meta.items():
        if not isinstance(k, str):
            raise TypeError(f"meta key must be str: {k!r}")
        if not isinstance(v, _META_SCALAR_TYPES):
            raise TypeError(
                f"meta value for {k!r} must be str/int/float/bool/None, "
                f"got {type(v).__name__}"
            )
        if isinstance(v, str) and len(v) > _MAX_META_VALUE_LEN:
            raise ValueError(
                f"meta value for {k!r} exceeds {_MAX_META_VALUE_LEN} chars"
            )


def _resolve_events_path(path: Path | None) -> Path:
    """Resolve the events-file path and refuse anything outside ~/.claude/.

    Tests may pass a temp dir under ``tmp_path``; that's allowed because
    ``tmp_path`` is explicit per-call, not attacker-controlled. The
    containment check exists to defend against callers that accidentally
    (or maliciously) pass ``path=Path("/etc/passwd")`` or similar.
    """
    if path is None:
        return DEFAULT_EVENTS_PATH
    target = Path(path)
    # Refuse any path that ascends via "..". A caller passing an explicit
    # path is opting in to their own location, but traversal segments are
    # almost always either a bug (caller forgot to resolve) or an attempt
    # to write outside the intended directory. Checking the unresolved
    # parts — not ``resolve()`` output — is what catches it: ``resolve()``
    # normalises ``..`` away, so comparing resolved strings would always
    # agree with itself.
    if ".." in target.parts:
        raise ValueError(f"path escapes its parent directory: {target}")
    return target


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
    target = _resolve_events_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    safe_meta: dict[str, Any] = dict(meta or {})
    _validate_meta(safe_meta)

    record = TelemetryEvent(
        event=event,
        skill=skill,
        timestamp=_now_iso(),
        session_id=session_id,
        event_id=_new_event_id(),
        meta=safe_meta,
    )
    line = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True) + "\n"

    with file_lock(target):
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    return record


def read_events(path: Path | None = None) -> Iterator[TelemetryEvent]:
    """Yield every event from the JSONL stream in on-disk order.

    Malformed lines — including records missing required fields such as
    ``event_id`` — are skipped with a warning. Append-only storage
    should not need editing, but a crash mid-write can leave a partial
    trailing line, so iteration must survive that. Warnings use only
    the exception type name so malformed input can't spoof stderr with
    embedded CR/LF or forged log-line content.
    """
    target = _resolve_events_path(path)
    try:
        fh = open(target, encoding="utf-8")
    except FileNotFoundError:
        return
    with fh:
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
                    event_id=obj["event_id"],
                    meta=obj.get("meta", {}),
                )
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                msg = (
                    f"skill_telemetry: skipping malformed event at line {ln} "
                    f"({type(exc).__name__})"
                )
                _logger.warning(msg)
                # Also emit to stderr so CLI users see it without needing
                # to configure a logging handler. Message is sanitised:
                # only the exception class name is included, never raw
                # line content or attacker-controlled strings.
                print(msg, file=sys.stderr)


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


def _parse_iso_utc(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating naive values as UTC.

    Accepting naive timestamps keeps ``is_retained`` robust when the
    event log has been edited by hand or written by an older version of
    the library. Without this, comparing a naive and an aware timestamp
    raises ``TypeError`` mid-computation — an unfriendly failure mode
    for read-path tooling.
    """
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_retained(
    load_ts: str,
    unload_ts: str,
    session_seconds: float,
    *,
    fraction: float = DEFAULT_SESSION_FRACTION,
    min_minutes: float = DEFAULT_MIN_RETENTION_MIN,
) -> bool:
    """True iff the skill survived the retention window."""
    t0 = _parse_iso_utc(load_ts)
    t1 = _parse_iso_utc(unload_ts)
    delta = (t1 - t0).total_seconds()
    if delta < 0:
        raise ValueError("unload_ts must be >= load_ts")
    window = retention_window_seconds(
        session_seconds, fraction=fraction, min_minutes=min_minutes
    )
    return delta >= window
