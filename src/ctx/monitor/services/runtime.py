"""Runtime lifecycle readers for ctx-monitor."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any


def read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if limit is not None and limit <= 0:
        return []
    out: deque[dict[str, Any]] | list[dict[str, Any]]
    if limit is not None:
        out = deque(maxlen=limit)
    else:
        out = []
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
    return [event for event in events if event.get("action") in {"validation", "escalation"}]


def summarize_sessions(
    audit: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    audit_entity_type: Callable[[dict[str, Any]], str | None],
) -> list[dict[str, Any]]:
    by_session: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "session_id": "",
            "first_seen": None,
            "last_seen": None,
            "skills_loaded": set(),
            "skills_unloaded": set(),
            "agents_loaded": set(),
            "agents_unloaded": set(),
            "mcps_loaded": set(),
            "mcps_unloaded": set(),
            "score_updates": 0,
            "lifecycle_transitions": 0,
        },
    )

    for line in audit:
        sid = line.get("session_id") or "unknown"
        row = by_session[sid]
        row["session_id"] = sid
        ts = line.get("ts")
        if ts and (row["first_seen"] is None or ts < row["first_seen"]):
            row["first_seen"] = ts
        if ts and (row["last_seen"] is None or ts > row["last_seen"]):
            row["last_seen"] = ts
        event = line.get("event", "")
        if event == "skill.loaded":
            row["skills_loaded"].add(line.get("subject", ""))
        elif event == "skill.unloaded":
            row["skills_unloaded"].add(line.get("subject", ""))
        elif event == "agent.loaded":
            row["agents_loaded"].add(line.get("subject", ""))
        elif event == "agent.unloaded":
            row["agents_unloaded"].add(line.get("subject", ""))
        elif event == "toolbox.triggered":
            raw_meta = line.get("meta")
            meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
            if meta.get("entity_type") == "mcp-server":
                action = meta.get("action")
                if action == "loaded":
                    row["mcps_loaded"].add(line.get("subject", ""))
                elif action == "unloaded":
                    row["mcps_unloaded"].add(line.get("subject", ""))
        elif event.endswith(".score_updated"):
            row["score_updates"] += 1
        elif event in (
            "skill.archived",
            "skill.demoted",
            "skill.restored",
            "skill.deleted",
            "agent.archived",
            "agent.demoted",
            "agent.restored",
            "agent.deleted",
        ):
            row["lifecycle_transitions"] += 1

    for line in events:
        sid = line.get("session_id") or "unknown"
        row = by_session[sid]
        row["session_id"] = sid
        ts = line.get("timestamp")
        if ts and (row["first_seen"] is None or ts < row["first_seen"]):
            row["first_seen"] = ts
        if ts and (row["last_seen"] is None or ts > row["last_seen"]):
            row["last_seen"] = ts
        action = line.get("event")
        entity_type = (
            audit_entity_type(line)
            or ("agent" if line.get("agent") else None)
            or ("mcp-server" if line.get("mcp") or line.get("mcp_server") else None)
            or ("skill" if line.get("skill") else None)
        )
        if entity_type == "agent":
            subject = line.get("agent")
        elif entity_type == "mcp-server":
            subject = line.get("mcp") or line.get("mcp_server")
        else:
            subject = line.get("skill")
        if action == "load" and subject:
            if entity_type == "agent":
                row["agents_loaded"].add(subject)
            elif entity_type == "mcp-server":
                row["mcps_loaded"].add(subject)
            else:
                row["skills_loaded"].add(subject)
        elif action == "unload" and subject:
            if entity_type == "agent":
                row["agents_unloaded"].add(subject)
            elif entity_type == "mcp-server":
                row["mcps_unloaded"].add(subject)
            else:
                row["skills_unloaded"].add(subject)

    summaries: list[dict[str, Any]] = []
    for row in by_session.values():
        summaries.append(
            {
                "session_id": row["session_id"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "skills_loaded": sorted(row["skills_loaded"]),
                "skills_unloaded": sorted(row["skills_unloaded"]),
                "agents_loaded": sorted(row["agents_loaded"]),
                "agents_unloaded": sorted(row["agents_unloaded"]),
                "mcps_loaded": sorted(row["mcps_loaded"]),
                "mcps_unloaded": sorted(row["mcps_unloaded"]),
                "score_updates": row["score_updates"],
                "lifecycle_transitions": row["lifecycle_transitions"],
            }
        )
    summaries.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    return summaries


def session_detail(
    session_id: str,
    audit: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "audit_entries": [record for record in audit if record.get("session_id") == session_id],
        "load_events": [event for event in events if event.get("session_id") == session_id],
    }


def escalation_key(event: dict[str, Any]) -> str:
    for field in ("escalation_id", "event_id", "id"):
        value = event.get(field)
        if value:
            return str(value)
    return "\0".join(
        str(event.get(field) or "") for field in ("session_id", "trigger", "reason", "severity")
    )


def lifecycle_summary(path: Path, limit: int = 200) -> dict[str, Any]:
    events = lifecycle_events(path, limit=None)
    validations = [event for event in events if event.get("action") == "validation"]
    escalations = [event for event in events if event.get("action") == "escalation"]
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
        event
        for event in validations
        if str(event.get("status") or "").lower() in {"failed", "error"}
    ]
    sessions = sorted(
        {str(event.get("session_id") or "") for event in events if event.get("session_id")}
    )
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
