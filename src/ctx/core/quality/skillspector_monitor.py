"""Dashboard helpers for ctx-run SkillSpector audit records."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from ctx.core.quality.skillspector_audit import (
    SkillSpectorAuditRecord,
    load_audit_records,
)


STATUS_ORDER = {
    "blocked": 0,
    "findings": 1,
    "not_scanned_no_body": 2,
    "error": 3,
    "missing": 4,
    "passed": 5,
}
SEVERITY_ORDER = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "UNKNOWN": 4,
}


def load_skill_metadata_from_dashboard_index(
    index_path: Path | None,
) -> dict[str, dict[str, Any]]:
    """Load skill tags/title/description from the cached dashboard graph index."""
    if index_path is None or not index_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id,label,tags,description,quality_score,usage_score,degree "
            "FROM nodes WHERE type='skill'"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    metadata: dict[str, dict[str, Any]] = {}
    for row in rows:
        node_id = str(row["id"] or "")
        slug = node_id.split(":", 1)[1] if ":" in node_id else node_id
        if not slug:
            continue
        try:
            raw_tags = json.loads(str(row["tags"] or "[]"))
        except json.JSONDecodeError:
            raw_tags = []
        tags = [str(tag) for tag in raw_tags if isinstance(tag, str)]
        metadata[slug] = {
            "title": str(row["label"] or slug),
            "tags": tags,
            "description": str(row["description"] or ""),
            "quality_score": row["quality_score"],
            "usage_score": row["usage_score"],
            "degree": int(row["degree"] or 0),
        }
    return metadata


def load_skill_families_from_communities(
    communities_path: Path | None,
) -> dict[str, dict[str, str]]:
    """Load graph community labels as skill family metadata."""
    if communities_path is None or not communities_path.is_file():
        return {}
    try:
        payload = json.loads(communities_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    communities = payload.get("communities") if isinstance(payload, dict) else None
    if not isinstance(communities, dict):
        return {}

    families: dict[str, dict[str, str]] = {}
    for raw_id, raw_info in communities.items():
        if not isinstance(raw_info, dict):
            continue
        label = str(raw_info.get("label") or f"community {raw_id}")
        members = raw_info.get("members")
        if not isinstance(members, list):
            continue
        for member in members:
            node_id = str(member)
            if not node_id.startswith("skill:"):
                continue
            slug = node_id.split(":", 1)[1]
            families[slug] = {
                "family": label,
                "family_id": str(raw_id),
            }
    return families


def load_skillspector_audit_records(path: Path) -> dict[str, SkillSpectorAuditRecord]:
    """Load SkillSpector audit records from gzip, returning empty when absent."""
    return load_audit_records(path)


def build_skillspector_audit_payload(
    records: dict[str, SkillSpectorAuditRecord],
    *,
    metadata_by_slug: dict[str, dict[str, Any]] | None = None,
    families_by_slug: dict[str, dict[str, str]] | None = None,
    query: str = "",
    status: str = "",
    severity: str = "",
    tag: str = "",
    family: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Return filterable dashboard payload for SkillSpector records."""
    metadata_by_slug = metadata_by_slug or {}
    families_by_slug = families_by_slug or {}
    all_rows = [
        _row_from_record(
            record,
            metadata_by_slug.get(slug, {}),
            families_by_slug.get(slug, {}),
        )
        for slug, record in records.items()
    ]
    all_rows.sort(key=_row_sort_key)

    filtered = [
        row
        for row in all_rows
        if _row_matches(row, query=query, status=status, severity=severity, tag=tag, family=family)
    ]
    capped_limit = max(1, min(int(limit), 500))
    status_counts = Counter(str(row["status"]) for row in all_rows)
    severity_counts = Counter(str(row["risk_severity"]) for row in all_rows)
    tag_counts = Counter(tag_value for row in all_rows for tag_value in row.get("tags", []))
    family_counts = Counter(str(row["family"]) for row in all_rows if row.get("family"))
    return {
        "summary": {
            "total": len(all_rows),
            "visible": len(filtered),
            "returned": min(len(filtered), capped_limit),
            "problematic": sum(
                count for status_name, count in status_counts.items() if status_name != "passed"
            ),
            "statuses": dict(sorted(status_counts.items(), key=lambda item: _status_rank(item[0]))),
            "severities": dict(
                sorted(severity_counts.items(), key=lambda item: _severity_rank(item[0]))
            ),
        },
        "filters": {
            "query": query,
            "status": status,
            "severity": severity,
            "tag": tag,
            "family": family,
            "limit": capped_limit,
            "statuses": _counter_options(status_counts, rank=_status_rank),
            "severities": _counter_options(severity_counts, rank=_severity_rank),
            "tags": _counter_options(tag_counts, limit=100),
            "families": _counter_options(family_counts, limit=100),
        },
        "records": filtered[:capped_limit],
    }


def _row_from_record(
    record: SkillSpectorAuditRecord,
    metadata: dict[str, Any],
    family: dict[str, str],
) -> dict[str, Any]:
    severity = str(record.risk_severity or "UNKNOWN").upper()
    tags = [str(tag) for tag in metadata.get("tags") or [] if str(tag).strip()]
    return {
        "slug": record.slug,
        "title": str(metadata.get("title") or record.slug),
        "description": str(metadata.get("description") or ""),
        "tags": tags,
        "family": family.get("family", ""),
        "family_id": family.get("family_id", ""),
        "status": str(record.status or "error"),
        "risk_score": record.risk_score,
        "risk_severity": severity,
        "recommendation": record.recommendation or "",
        "issues": record.issues,
        "components": record.components,
        "issue_rules": list(record.issue_rules),
        "content_sha256": record.content_sha256 or "",
        "scanned_at": record.scanned_at,
        "scanner_version": record.scanner_version or "",
        "mode": record.mode,
        "error": record.error or "",
        "quality_score": metadata.get("quality_score"),
        "usage_score": metadata.get("usage_score"),
        "degree": metadata.get("degree", 0),
        "href": f"/wiki/{record.slug}?type=skill",
    }


def _row_matches(
    row: dict[str, Any],
    *,
    query: str,
    status: str,
    severity: str,
    tag: str,
    family: str,
) -> bool:
    status_filter = status.strip().lower()
    if status_filter and status_filter != "all" and str(row["status"]).lower() != status_filter:
        return False
    severity_filter = severity.strip().upper()
    if (
        severity_filter
        and severity_filter != "ALL"
        and str(row["risk_severity"]).upper() != severity_filter
    ):
        return False
    tag_filter = tag.strip().lower()
    if tag_filter:
        tags = [str(value).lower() for value in row.get("tags", [])]
        if not any(tag_filter in value for value in tags):
            return False
    family_filter = family.strip().lower()
    if family_filter:
        family_values = {
            str(row.get("family") or "").lower(),
            str(row.get("family_id") or "").lower(),
        }
        if family_filter not in family_values:
            return False
    terms = [term for term in re.split(r"\s+", query.lower().strip()) if term]
    if not terms:
        return True
    haystack = " ".join(
        [
            str(row.get("slug") or ""),
            str(row.get("title") or ""),
            str(row.get("description") or ""),
            str(row.get("family") or ""),
            str(row.get("status") or ""),
            str(row.get("risk_severity") or ""),
            str(row.get("recommendation") or ""),
            str(row.get("error") or ""),
            " ".join(str(tag_value) for tag_value in row.get("tags", [])),
            " ".join(str(rule) for rule in row.get("issue_rules", [])),
        ]
    ).lower()
    return all(term in haystack for term in terms)


def _row_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    risk_score = row.get("risk_score")
    try:
        risk_value = int(risk_score) if risk_score is not None else -1
    except (TypeError, ValueError):
        risk_value = -1
    return (
        _status_rank(str(row.get("status") or "")),
        _severity_rank(str(row.get("risk_severity") or "")),
        -risk_value,
        str(row.get("slug") or "").lower(),
    )


def _status_rank(value: str) -> int:
    return STATUS_ORDER.get(value.lower(), 99)


def _severity_rank(value: str) -> int:
    return SEVERITY_ORDER.get(value.upper(), 99)


def _counter_options(
    counter: Counter[str],
    *,
    rank: Any | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    def sort_key(item: tuple[str, int]) -> tuple[Any, int, str]:
        label, count = item
        return (rank(label) if rank else label.lower(), -count, label.lower())

    items = sorted(counter.items(), key=sort_key)
    if limit is not None:
        items = items[:limit]
    return [{"value": label, "count": count} for label, count in items]
