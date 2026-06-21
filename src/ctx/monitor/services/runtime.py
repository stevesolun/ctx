"""Runtime lifecycle readers for ctx-monitor."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if limit is not None and limit <= 0:
        return []
    out: deque[dict[str, Any]] | list[dict[str, Any]]
    out = deque(maxlen=limit) if limit is not None else []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                out.append(event)
    return list(out)


def lifecycle_events(path: Path, limit: int | None = 200) -> list[dict[str, Any]]:
    events = read_jsonl(path, limit=limit)
    return [
        event for event in events
        if event.get("action") in {"validation", "escalation"}
    ]


def escalation_key(event: dict[str, Any]) -> str:
    for field in ("escalation_id", "event_id", "id"):
        value = event.get(field)
        if value:
            return str(value)
    return "\0".join(
        str(event.get(field) or "")
        for field in ("session_id", "trigger", "reason", "severity")
    )


def lifecycle_summary(path: Path, limit: int = 200) -> dict[str, Any]:
    events = lifecycle_events(path, limit=None)
    validations = [
        event for event in events if event.get("action") == "validation"
    ]
    escalations = [
        event for event in events if event.get("action") == "escalation"
    ]
    open_by_key: dict[str, dict[str, Any]] = {}
    for event in escalations:
        key = escalation_key(event)
        status = str(event.get("status") or "open").lower()
        if status == "open":
            open_by_key[key] = event
        else:
            open_by_key.pop(key, None)
    open_escalations = list(open_by_key.values())
    validation_failures = [
        event for event in validations
        if str(event.get("status") or "").lower() in {"failed", "error"}
    ]
    sessions = sorted({
        str(event.get("session_id") or "")
        for event in events
        if event.get("session_id")
    })
    return {
        "path": str(path),
        "events_total": len(events),
        "validations_total": len(validations),
        "validation_failures": len(validation_failures),
        "escalations_total": len(escalations),
        "open_escalations_total": len(open_escalations),
        "latest_validation": validations[-1] if validations else None,
        "recent_validations": validations[-20:],
        "open_escalations": open_escalations[-20:],
        "sessions": sessions,
    }
