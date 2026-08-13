"""Runtime lifecycle readers for the ctx dashboard."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ctx.adapters.generic.runtime_lifecycle import normalize_historical_token_usage

_TOKEN_ATTRIBUTIONS = ("exact", "estimated", "unavailable")
_SELECTION_SOURCES = ("user", "system", "host", "unknown")
_TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "total_tokens",
)


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


def _normalized_token_usage(raw: Any) -> dict[str, Any]:
    return normalize_historical_token_usage(raw)


def _empty_token_usage_summary() -> dict[str, Any]:
    return {
        "records": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tokens_reported": True,
        "cost_usd": 0.0,
        "by_attribution": {key: 0 for key in _TOKEN_ATTRIBUTIONS},
    }


def _merge_token_usage(summary: dict[str, Any], usage: dict[str, Any]) -> None:
    summary["records"] = int(summary.get("records") or 0) + 1
    attribution = str(usage["attribution"])
    summary["by_attribution"][attribution] = (
        int(summary["by_attribution"].get(attribution) or 0) + 1
    )
    for key in _TOKEN_USAGE_FIELDS:
        value = usage[key]
        current = summary.get(key)
        summary[key] = None if current is None or value is None else int(current) + value
    summary["tokens_reported"] = bool(
        summary.get("tokens_reported") and usage["tokens_reported"] is True
    )
    cost = usage["cost_usd"]
    current_cost = summary.get("cost_usd")
    summary["cost_usd"] = (
        None if current_cost is None or cost is None else round(float(current_cost) + cost, 8)
    )


def _tool_key(event: dict[str, Any]) -> tuple[str, str] | None:
    entity_type = str(event.get("entity_type") or "")
    slug = str(event.get("slug") or "")
    if not entity_type or not slug:
        return None
    return entity_type, slug


def _load_key(event: dict[str, Any]) -> tuple[str, str, str] | None:
    key = _tool_key(event)
    if key is None:
        return None
    return str(event.get("session_id") or "unknown"), key[0], key[1]


def _selection_source(value: Any) -> str:
    source = str(value or "unknown").lower()
    return source if source in _SELECTION_SOURCES else "unknown"


def _evidence_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"evidence_present": False, "evidence_length": 0}
    text = value.strip()
    return {"evidence_present": bool(text), "evidence_length": len(text)}


def _usage_bucket(
    buckets: dict[Any, dict[str, Any]],
    key: Any,
    **labels: Any,
) -> dict[str, Any]:
    bucket = buckets.get(key)
    if bucket is None:
        bucket = dict(labels)
        bucket.update(_empty_token_usage_summary())
        buckets[key] = bucket
    return bucket


def _usage_rows(buckets: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        buckets.values(),
        key=lambda row: (
            -int(row.get("total_tokens") or 0),
            str(row.get("entity_type") or ""),
            str(row.get("slug") or ""),
            str(row.get("session_id") or ""),
            str(row.get("selection_source") or ""),
        ),
    )


def _runtime_tool_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    active: dict[tuple[str, str, str], dict[str, Any]] = {}
    selection_sources = {key: 0 for key in _SELECTION_SOURCES}
    token_usage = _empty_token_usage_summary()
    usage_by_tool: dict[Any, dict[str, Any]] = {}
    usage_by_type: dict[Any, dict[str, Any]] = {}
    usage_by_session: dict[Any, dict[str, Any]] = {}
    usage_by_source: dict[Any, dict[str, Any]] = {}
    recent_tool_usage: list[dict[str, Any]] = []
    loaded_total = 0
    selected_total = 0
    used_total = 0

    for event in events:
        action = event.get("action")
        key = _tool_key(event)
        load_key = _load_key(event)
        if action == "load_requested" and key is not None and load_key is not None:
            loaded_total += 1
            selected = bool(event.get("selected", False))
            if selected:
                selected_total += 1
            source = _selection_source(event.get("selection_source"))
            selection_sources[source] += 1
            active[load_key] = {
                "session_id": load_key[0],
                "entity_type": key[0],
                "slug": key[1],
                "selected": selected,
                "selection_source": source,
            }
        elif action == "unload_requested" and load_key is not None:
            active.pop(load_key, None)
        elif action == "used" and key is not None:
            used_total += 1
            raw_usage = event.get("token_usage")
            normalized_usage = _normalized_token_usage(raw_usage)
            _merge_token_usage(token_usage, normalized_usage)
            session_id = str(event.get("session_id") or "unknown")
            active_entry = active.get(load_key) if load_key is not None else None
            source = _selection_source(
                active_entry.get("selection_source") if active_entry is not None else None
            )
            _merge_token_usage(
                _usage_bucket(
                    usage_by_tool,
                    key,
                    entity_type=key[0],
                    slug=key[1],
                ),
                normalized_usage,
            )
            _merge_token_usage(
                _usage_bucket(usage_by_type, key[0], entity_type=key[0]),
                normalized_usage,
            )
            _merge_token_usage(
                _usage_bucket(usage_by_session, session_id, session_id=session_id),
                normalized_usage,
            )
            _merge_token_usage(
                _usage_bucket(
                    usage_by_source,
                    source,
                    selection_source=source,
                ),
                normalized_usage,
            )
            recent_tool_usage.append(
                {
                    "created_at": event.get("created_at"),
                    "session_id": session_id,
                    "entity_type": key[0],
                    "slug": key[1],
                    "selection_source": source,
                    **_evidence_metadata(event.get("evidence")),
                    "token_usage": normalized_usage,
                }
            )

    active_values = list(active.values())
    return {
        "tool_selection": {
            "loaded_total": loaded_total,
            "active_loaded_total": len(active_values),
            "selected_total": selected_total,
            "active_selected_total": sum(1 for item in active_values if item["selected"]),
            "selection_sources": selection_sources,
            "used_total": used_total,
        },
        "token_usage": token_usage,
        "token_usage_history": {
            "by_tool": _usage_rows(usage_by_tool),
            "by_type": _usage_rows(usage_by_type),
            "by_session": _usage_rows(usage_by_session),
            "by_source": _usage_rows(usage_by_source),
        },
        "recent_tool_usage": recent_tool_usage[-20:],
    }


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
    all_events = read_jsonl(path, limit=None)
    events = [event for event in all_events if event.get("action") in {"validation", "escalation"}]
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
    summary = {
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
    summary.update(_runtime_tool_summary(all_events))
    return summary
