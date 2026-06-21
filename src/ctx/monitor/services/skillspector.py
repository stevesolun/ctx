"""Read-only SkillSpector audit helpers for ctx-monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ctx.core.quality.skillspector_monitor import (
    build_skillspector_audit_payload,
    load_skill_families_from_communities,
    load_skill_metadata_from_dashboard_index,
    load_skillspector_audit_records,
)


def first_existing_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def audit_path(wiki_dir: Path, repo_graph_dir: Path) -> Path:
    return first_existing_path(
        wiki_dir / "security" / "skillspector-audit.jsonl.gz",
        repo_graph_dir / "skillspector-audit.jsonl.gz",
    )


def communities_path(wiki_dir: Path, repo_graph_dir: Path) -> Path | None:
    candidates = (
        wiki_dir / "graphify-out" / "communities.json",
        repo_graph_dir / "communities.json",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def index_path(
    dashboard_index_path: Path,
    dashboard_index_matches_manifest: Callable[[Path], bool],
) -> Path | None:
    if dashboard_index_path.is_file() and dashboard_index_matches_manifest(dashboard_index_path):
        return dashboard_index_path
    return None


def limit(query: dict[str, str]) -> int:
    try:
        return max(1, min(int(query.get("limit", 100)), 500))
    except ValueError:
        return 100


def audit_payload(
    wiki_dir: Path,
    repo_graph_dir: Path,
    dashboard_index_path: Path,
    dashboard_index_matches_manifest: Callable[[Path], bool],
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    query = query or {}
    path = audit_path(wiki_dir, repo_graph_dir)
    records = load_skillspector_audit_records(path)
    payload = build_skillspector_audit_payload(
        records,
        metadata_by_slug=load_skill_metadata_from_dashboard_index(
            index_path(dashboard_index_path, dashboard_index_matches_manifest)
        ),
        families_by_slug=load_skill_families_from_communities(
            communities_path(wiki_dir, repo_graph_dir)
        ),
        query=query.get("q", ""),
        status=query.get("status", ""),
        severity=query.get("severity", ""),
        tag=query.get("tag", ""),
        family=query.get("family", ""),
        limit=limit(query),
    )
    payload["audit_path"] = str(path)
    payload["audit_available"] = path.is_file()
    return payload
