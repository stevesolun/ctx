"""ctx_audit_log.py -- Unified append-only audit log for every ctx action.

Every mutation that matters for post-hoc investigation is recorded here as a
single JSONL line in ``~/.claude/ctx-audit.jsonl``:

    skill.added        skill_add.add_skill()
    skill.installed    skill_add / skill_loader
    skill.converted    batch_convert / skill_category
    skill.loaded       context_monitor (from skill-events.jsonl "load")
    skill.unloaded     context_monitor (from skill-events.jsonl "unload")
    skill.used         usage_tracker (signal -> skill match)
    skill.archived     ctx_lifecycle archive transition
    skill.deleted      ctx_lifecycle purge transition
    skill.restored     ctx_lifecycle review-archived --restore
    skill.score_updated skill_quality.persist_quality
    skill.sidecar_rewritten skill_quality.SidecarSink.write
    agent.* (same set, "agent" subject_type)
    session.started    first event in a session_id
    session.ended      Stop hook fires
    backup.snapshot    backup_mirror create/snapshot-if-changed

Schema (per line):

    {
      "ts":         ISO-8601 UTC w/ seconds precision,
      "event":      dotted event name (see list above),
      "subject_type": "skill" | "agent" | "session" | "backup" | "toolbox",
      "subject":    slug or id ("python-patterns", session uuid, etc.),
      "actor":      "hook" | "cli" | "lifecycle" | "user",
      "session_id": optional session id if known,
      "meta":       optional dict with event-specific fields,
    }

The log is append-only. Rotation is by day (``ctx-audit.jsonl`` is current;
``ctx-audit-YYYY-MM-DD.jsonl`` are historical). Callers never truncate.

Readers (``ctx-monitor`` dashboard, postmortem scripts) consume the log;
they never mutate it.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "EVENT_TYPES",
    "audit_log_path",
    "log",
    "log_skill_event",
    "log_agent_event",
    "log_session_event",
    "log_backup_event",
    "rotate_if_needed",
]


# Canonical event names. Keep this list in sync with the docstring above.
EVENT_TYPES: frozenset[str] = frozenset({
    # lifecycle
    "skill.added", "skill.installed", "skill.converted",
    "skill.loaded", "skill.unloaded", "skill.used",
    "skill.watched", "skill.demoted",
    "skill.archived", "skill.deleted", "skill.restored",
    "skill.score_updated", "skill.sidecar_rewritten",
    # agent variants (same semantics)
    "agent.added", "agent.installed",
    "agent.loaded", "agent.unloaded", "agent.used",
    "agent.watched", "agent.demoted",
    "agent.archived", "agent.deleted", "agent.restored",
    "agent.score_updated", "agent.sidecar_rewritten",
    # meta
    "session.started", "session.ended",
    "backup.snapshot", "backup.prune",
    "toolbox.triggered", "toolbox.verdict",
})


SubjectType = Literal["skill", "agent", "session", "backup", "toolbox"]
Actor = Literal["hook", "cli", "lifecycle", "user", "scheduler"]


# Single shared lock per process keeps concurrent write-within-process safe.
# Cross-process safety comes from O_APPEND semantics on POSIX + the atomic
# write-then-fsync pattern below on Windows.
_LOCK = threading.Lock()
_MAX_SAFE_DEPTH = 8


def audit_log_path() -> Path:
    """Return the configured audit log path.

    Honors ``quality.paths.audit_log`` from ``ctx_config.cfg`` when set;
    falls back to ``~/.claude/ctx-audit.jsonl``.
    """
    try:
        from ctx_config import cfg  # local import — avoid cost on test import
        raw = cfg.get("paths", {}) or {}
        configured = raw.get("audit_log") if isinstance(raw, dict) else None
        if isinstance(configured, str) and configured.strip():
            return Path(os.path.expanduser(configured))
    except Exception:  # noqa: BLE001 — config unavailable in some test contexts
        pass
    return Path(os.path.expanduser("~/.claude/ctx-audit.jsonl"))


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, seconds precision, trailing ``Z``."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _json_safe(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> Any:
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth >= _MAX_SAFE_DEPTH:
        return repr(value)
    ident = id(value)
    if ident in seen:
        return "<circular>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        seen.add(ident)
        try:
            return {
                str(_json_safe(k, depth=depth + 1, seen=seen)):
                _json_safe(v, depth=depth + 1, seen=seen)
                for k, v in value.items()
            }
        finally:
            seen.discard(ident)
    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(ident)
        try:
            return [
                _json_safe(item, depth=depth + 1, seen=seen)
                for item in value
            ]
        finally:
            seen.discard(ident)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # noqa: BLE001
            pass
    return repr(value)


def log(
    event: str,
    *,
    subject_type: SubjectType,
    subject: str,
    actor: Actor = "hook",
    session_id: str | None = None,
    meta: dict[str, Any] | None = None,
    path: Path | None = None,
) -> None:
    """Append one audit line. Best-effort — never raises on I/O failure.

    Unknown event names are logged under the literal string but emit a
    stderr warning so callers notice typos early. We refuse to silently
    remap them — audit logs are only useful if ``grep`` on an event name
    matches exactly what the caller wrote.
    """
    if event not in EVENT_TYPES:
        import sys as _sys
        print(
            f"[ctx-audit] warning: unknown event {event!r}; consider adding "
            f"to ctx_audit_log.EVENT_TYPES",
            file=_sys.stderr,
        )

    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": event,
        "subject_type": subject_type,
        "subject": subject,
        "actor": actor,
    }
    if session_id is not None:
        record["session_id"] = session_id
    if meta:
        record["meta"] = meta

    try:
        target = path if path is not None else audit_log_path()
        line = (
            json.dumps(
                _json_safe(record),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        with _LOCK:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Append-only. O_APPEND is atomic per-write for writes up to
            # PIPE_BUF bytes on POSIX; our records are well under that.
            # On Windows we accept best-effort — concurrent writers may
            # still interleave beyond PIPE_BUF, which is acceptable for
            # an audit log (each record is a valid JSON document).
            with open(target, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:  # noqa: BLE001
        # Never raise. Losing an audit line is strictly less bad than
        # taking down a hook that's about to write valuable data
        # elsewhere. Callers depend on this.
        return


# ─── Convenience wrappers ───────────────────────────────────────────────────


def log_skill_event(
    event: str,
    slug: str,
    *,
    actor: Actor = "hook",
    session_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Log a ``skill.*`` event by slug."""
    log(event, subject_type="skill", subject=slug, actor=actor,
        session_id=session_id, meta=meta)


def log_agent_event(
    event: str,
    slug: str,
    *,
    actor: Actor = "hook",
    session_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Log an ``agent.*`` event by slug."""
    log(event, subject_type="agent", subject=slug, actor=actor,
        session_id=session_id, meta=meta)


def log_session_event(
    event: str,
    session_id: str,
    *,
    actor: Actor = "hook",
    meta: dict[str, Any] | None = None,
) -> None:
    """Log a ``session.*`` event."""
    log(event, subject_type="session", subject=session_id, actor=actor,
        session_id=session_id, meta=meta)


def log_backup_event(
    event: str,
    snapshot_id: str,
    *,
    actor: Actor = "scheduler",
    meta: dict[str, Any] | None = None,
) -> None:
    """Log a ``backup.*`` event (snapshot id or prune set id)."""
    log(event, subject_type="backup", subject=snapshot_id, actor=actor,
        meta=meta)


# ─── Log rotation (call at session-end / Stop hook) ─────────────────────────


def rotate_if_needed(max_bytes: int = 25 * 1024 * 1024) -> Path | None:
    """Rotate the current log if it exceeds ``max_bytes``.

    Renames to ``ctx-audit-YYYYMMDDTHHMMSSZ.jsonl`` alongside the active
    file. Returns the rotated path (or ``None`` if no rotation needed).

    Called from ``quality_on_session_end.py`` so rotation happens at a
    natural boundary — we never rotate mid-session.
    """
    target = audit_log_path()
    if not target.exists() or target.stat().st_size <= max_bytes:
        return None
    stamp = _now_iso().replace(":", "").replace("-", "").replace("Z", "Z")
    rotated = target.with_name(f"{target.stem}-{stamp}{target.suffix}")
    try:
        target.rename(rotated)
    except OSError:
        # Another process may have rotated first; not an error.
        return None
    return rotated
