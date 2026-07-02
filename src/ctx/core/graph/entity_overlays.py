"""Shared helpers for runtime entity graph overlays."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import atomic_write_text

_EMPTY_VALUES: tuple[object, ...] = (None, "", [], {})
_NUMERIC_EDGE_ATTRS = frozenset(
    {
        "weight",
        "final_weight",
        "semantic_sim",
        "tag_sim",
        "token_sim",
        "similarity_score",
    }
)
_LIST_EDGE_ATTRS = frozenset(
    {
        "shared_tags",
        "shared_tokens",
        "shared_sources",
        "edge_reasons",
    }
)


def active_overlay_records(records: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return deduped active records, superseding stale rows by scope."""
    by_attach_key: dict[str, Mapping[str, Any]] = {}
    by_scope: dict[str, Mapping[str, Any]] = {}
    for index, record in enumerate(records):
        if record.get("superseded_at"):
            continue
        node_id = _record_node_id(record)
        if _is_delete_record(record):
            if node_id:
                by_attach_key = {
                    key: value
                    for key, value in by_attach_key.items()
                    if _record_node_id(value) != node_id
                }
                by_scope = {
                    key: value
                    for key, value in by_scope.items()
                    if _record_node_id(value) != node_id
                }
            continue
        attach_key = overlay_attach_key(record, fallback_index=index)
        if attach_key in by_attach_key:
            continue
        by_attach_key[attach_key] = record

        by_scope[overlay_replace_scope(record, fallback_index=index)] = record
    return list(by_scope.values())


def append_overlay_tombstone(path: Path, *, node_id: str, source: str) -> str:
    """Append a delete tombstone so stale overlay nodes stay removed."""
    if not node_id:
        raise ValueError("node_id must be non-empty")
    if not source:
        raise ValueError("source must be non-empty")
    with file_lock(path):
        records = load_overlay_records(path)
        if not records:
            return "skipped"
        records.append(
            {
                "action": "delete",
                "node_id": node_id,
                "source": source,
                "deleted_at": _utc_now(),
            }
        )
        write_overlay_records_atomic(path, records)
        return "inserted"


def _is_delete_record(record: Mapping[str, Any]) -> bool:
    action = record.get("action")
    if isinstance(action, str) and action.strip().lower() in {"delete", "tombstone"}:
        return True
    return bool(record.get("tombstone"))


def overlay_attach_key(record: Mapping[str, Any], *, fallback_index: int = 0) -> str:
    for key in ("attach_key", "idempotency_key", "overlay_id"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    node_id = _record_node_id(record)
    content_hash = record.get("content_hash")
    model_id = record.get("model_id", "legacy")
    if node_id and isinstance(content_hash, str) and content_hash:
        return f"ann:v1:{model_id}:{node_id}:{content_hash}"
    return f"legacy:{fallback_index}:{_canonical_hash(record)}"


def overlay_replace_scope(record: Mapping[str, Any], *, fallback_index: int = 0) -> str:
    value = record.get("replace_scope")
    if isinstance(value, str) and value:
        return value
    node_id = _record_node_id(record)
    model_id = record.get("model_id", "legacy")
    if node_id:
        return f"ann:v1:{model_id}:{node_id}"
    return overlay_attach_key(record, fallback_index=fallback_index)


def merge_node_attrs(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "id" or value in _EMPTY_VALUES:
            continue
        if key not in merged or merged.get(key) in _EMPTY_VALUES:
            merged[key] = value
    return merged


def replace_node_attrs(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay incoming non-empty node attrs onto an existing node."""
    merged = dict(existing)
    for key, value in incoming.items():
        if key == "id" or value in _EMPTY_VALUES:
            continue
        merged[key] = value
    return merged


def merge_edge_attrs(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        if key in {"source", "target"} or value in _EMPTY_VALUES:
            continue
        if key in _NUMERIC_EDGE_ATTRS:
            merged[key] = max(_as_float(merged.get(key)), _as_float(value))
        elif key in _LIST_EDGE_ATTRS:
            merged[key] = _ordered_union(merged.get(key), value)
        elif key == "direct_link":
            merged[key] = bool(merged.get(key)) or bool(value)
        elif key not in merged or merged.get(key) in _EMPTY_VALUES:
            merged[key] = value
    return merged


def replace_edge_attrs(incoming: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the current non-empty edge attrs from an authoritative overlay."""
    return {
        key: value
        for key, value in incoming.items()
        if key not in {"source", "target"} and value not in _EMPTY_VALUES
    }


def load_overlay_records(path: Path) -> list[dict[str, Any]]:
    """Load JSONL overlay records for writer paths.

    Resolver paths intentionally skip bad rows to keep recommendations alive.
    Writer paths fail closed so an attach operation cannot rewrite and drop
    unreadable rows by accident.
    """
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"overlay line {lineno} is invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"overlay line {lineno} is not a JSON object")
        records.append(payload)
    return records


def write_overlay_records_atomic(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    text = "".join(
        json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n"
        for record in records
    )
    atomic_write_text(path, text, encoding="utf-8")


def upsert_overlay_record(path: Path, record: dict[str, Any]) -> str:
    """Append a new active overlay record, superseding older same-scope rows."""
    attach_key = overlay_attach_key(record)
    replace_scope = overlay_replace_scope(record)
    with file_lock(path):
        records = load_overlay_records(path)
        for existing in records:
            if existing.get("superseded_at"):
                continue
            if overlay_attach_key(existing) == attach_key:
                return "unchanged"

        status = "inserted"
        now = _utc_now()
        for existing in records:
            if existing.get("superseded_at"):
                continue
            if overlay_replace_scope(existing) == replace_scope:
                existing["superseded_at"] = now
                status = "replaced"
        records.append(record)
        write_overlay_records_atomic(path, records)
        return status


def _record_node_id(record: Mapping[str, Any]) -> str | None:
    value = record.get("node_id")
    if isinstance(value, str) and value:
        return value
    nodes = record.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, Mapping):
                node_id = node.get("id")
                if isinstance(node_id, str) and node_id:
                    return node_id
    return None


def _canonical_hash(record: Mapping[str, Any]) -> str:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _as_float(value: object) -> float:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _ordered_union(left: object, right: object) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for values in (left, right):
        if isinstance(values, list):
            iterable = values
        else:
            iterable = [values]
        for value in iterable:
            key = json.dumps(value, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
