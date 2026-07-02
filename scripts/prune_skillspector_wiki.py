#!/usr/bin/env python3
"""Prune SkillSpector removal candidates from shipped graph artifacts.

This is a release-maintenance tool. It does not decide what should be removed;
that policy lives in ``ctx.core.quality.skillspector_remediation``. This script
applies only the plan's ``remove_slugs`` to wiki tarballs, graph JSON, the
dashboard index, and the fallback skill catalog.
"""

from __future__ import annotations

import argparse
import gzip
from io import BytesIO
import json
import re
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ctx.core.quality.skillspector_audit import (  # noqa: E402
    SkillSpectorAuditRecord,
    load_audit_records,
)
from ctx.core.quality.skillspector_remediation import build_remediation_plan  # noqa: E402
from ctx.core.wiki.artifact_promotion import promote_staged_artifact  # noqa: E402
from ctx.utils._fs_utils import atomic_write_bytes, atomic_write_text, reject_symlink_path  # noqa: E402
from scripts.build_dashboard_graph_index import build_dashboard_index  # noqa: E402

GRAPH_EXPORT_NAMES = {
    "graphify-out/graph.json",
    "graphify-out/graph-delta.json",
    "graphify-out/communities.json",
    "graphify-out/graph-report.md",
    "graphify-out/graph-export-manifest.json",
}
CATALOG_MEMBER = "external-catalogs/skills-sh/catalog.json"
AUDIT_MEMBER = "security/skillspector-audit.jsonl.gz"
PREVIEW_HTML_FILES = (
    "sample-top60.html",
    "viz-ai-agents.html",
    "viz-overview.html",
    "viz-python.html",
    "viz-security.html",
)
GZIP_COMPRESSLEVEL = 3
_EXPORT_META_RE = re.compile(
    r'(<meta\s+name=["\']ctx-graph-export-id["\']\s+content=["\'])([^"\']*)(["\'])',
    re.IGNORECASE,
)
_METADATA_RE = re.compile(r"const CTX_GRAPH_METADATA = (\{.*?\});", re.DOTALL)


@dataclass(frozen=True)
class PruneStats:
    remove_slugs: int
    graph_nodes_before: int
    graph_nodes_after: int
    graph_edges_before: int
    graph_edges_after: int
    skill_pages_removed: int
    converted_members_removed: int
    catalog_entries_removed: int
    audit_records_removed: int
    export_id: str


def build_pruned_artifacts(
    *,
    audit_path: Path,
    full_tarball: Path,
    runtime_tarball: Path,
    root_catalog: Path,
    root_communities: Path,
    graph_dir: Path,
    apply: bool,
    now: datetime | None = None,
) -> PruneStats:
    """Prune remove candidates from full/runtime graph artifacts."""
    records = load_audit_records(audit_path)
    plan = build_remediation_plan(records, audit_path=audit_path)
    remove_slugs = set(str(slug) for slug in plan["remove_slugs"])
    remove_node_ids = {f"skill:{slug}" for slug in remove_slugs}
    timestamp = _timestamp(now)

    graph, communities = _read_tar_graph_artifacts(full_tarball)
    graph_before = _graph_counts(graph)
    graph = _prune_graph(graph, remove_node_ids)
    graph_after = _graph_counts(graph)
    export_id = f"ctx-skillspector-prune-{timestamp}-{graph_after[0]}-{graph_after[1]}"
    graph.setdefault("graph", {})["export_id"] = export_id
    graph["graph"]["generated"] = timestamp
    graph["graph"]["skillspector_removed_nodes"] = len(remove_node_ids)
    communities = _prune_communities(
        communities,
        remove_node_ids=remove_node_ids,
        export_id=export_id,
        generated=timestamp,
    )

    audit_records = {slug: record for slug, record in records.items() if slug not in remove_slugs}
    pruned_catalog, catalog_removed = _prune_catalog_file(root_catalog, remove_slugs)
    replacements = (
        _build_replacements(
            graph=graph,
            communities=communities,
            remove_node_ids=remove_node_ids,
            audit_records=audit_records,
            pruned_catalog=pruned_catalog,
            export_id=export_id,
            generated=timestamp,
        )
        if apply
        else {}
    )

    full_stats = _rewrite_tarball(
        full_tarball,
        replacements=replacements,
        remove_slugs=remove_slugs,
        apply=apply,
    )
    if apply:
        runtime_replacements = {
            key: value
            for key, value in replacements.items()
            if key not in {AUDIT_MEMBER, CATALOG_MEMBER}
        }
        runtime_replacements[CATALOG_MEMBER] = _json_bytes(pruned_catalog, compact=False)
        _rewrite_tarball(
            runtime_tarball,
            replacements=runtime_replacements,
            remove_slugs=remove_slugs,
            apply=True,
        )

    if apply:
        atomic_write_text(root_communities, json.dumps(communities, indent=2) + "\n")
        atomic_write_bytes(root_catalog, _gzip_json_bytes(pruned_catalog))
        atomic_write_bytes(audit_path, _audit_bytes(audit_records.values()))
        _refresh_preview_metadata(
            graph_dir,
            export_id=export_id,
            nodes=graph_after[0],
            edges=graph_after[1],
        )

    return PruneStats(
        remove_slugs=len(remove_slugs),
        graph_nodes_before=graph_before[0],
        graph_nodes_after=graph_after[0],
        graph_edges_before=graph_before[1],
        graph_edges_after=graph_after[1],
        skill_pages_removed=full_stats["skill_pages_removed"],
        converted_members_removed=full_stats["converted_members_removed"],
        catalog_entries_removed=catalog_removed,
        audit_records_removed=len(records) - len(audit_records),
        export_id=export_id,
    )


def _build_replacements(
    *,
    graph: dict[str, Any],
    communities: dict[str, Any],
    remove_node_ids: set[str],
    audit_records: dict[str, SkillSpectorAuditRecord],
    pruned_catalog: dict[str, Any],
    export_id: str,
    generated: str,
) -> dict[str, bytes]:
    return {
        "graphify-out/graph.json": _json_bytes(graph, compact=True),
        "graphify-out/dashboard-neighborhoods.sqlite3": _dashboard_index_bytes(graph),
        "graphify-out/graph-delta.json": _json_bytes(
            _render_delta(remove_node_ids, export_id=export_id, generated=generated),
            compact=False,
        ),
        "graphify-out/communities.json": _json_bytes(communities, compact=False),
        "graphify-out/graph-report.md": _render_report(
            graph,
            communities,
            export_id=export_id,
            generated=generated,
            removed=len(remove_node_ids),
        ).encode("utf-8"),
        "graphify-out/graph-export-manifest.json": _json_bytes(
            _render_manifest(graph, communities, export_id=export_id, generated=generated),
            compact=False,
        ),
        AUDIT_MEMBER: _audit_bytes(audit_records.values()),
        CATALOG_MEMBER: _json_bytes(pruned_catalog, compact=False),
    }


def _safe_tar_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    first = parts[0]
    if (
        normalized.startswith("/")
        or (len(first) == 2 and first[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return normalized


def _read_tar_graph_artifacts(tarball: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    graph: dict[str, Any] | None = None
    communities: dict[str, Any] | None = None
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf:
            safe_name = _safe_tar_name(member.name)
            if safe_name not in {"graphify-out/graph.json", "graphify-out/communities.json"}:
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            data = json.loads(f.read().decode("utf-8"))
            if safe_name.endswith("graph.json"):
                graph = data
            else:
                communities = data
    if graph is None or communities is None:
        raise ValueError(f"{tarball} is missing graph.json or communities.json")
    return graph, communities


def _graph_edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    raw = graph.get("edges", graph.get("links", []))
    return [edge for edge in raw if isinstance(edge, dict)]


def _graph_counts(graph: dict[str, Any]) -> tuple[int, int]:
    nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
    return len(nodes), len(_graph_edges(graph))


def _prune_graph(graph: dict[str, Any], remove_node_ids: set[str]) -> dict[str, Any]:
    nodes = [
        node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("id") not in remove_node_ids
    ]
    edges = [
        edge
        for edge in _graph_edges(graph)
        if edge.get("source") not in remove_node_ids and edge.get("target") not in remove_node_ids
    ]
    graph_meta = graph.get("graph")
    pruned: dict[str, Any] = {"graph": graph_meta if isinstance(graph_meta, dict) else {}}
    for key, value in graph.items():
        if key not in {"graph", "nodes", "edges", "links"}:
            pruned[key] = value
    pruned["nodes"] = nodes
    pruned["edges"] = edges
    return pruned


def _prune_communities(
    communities: dict[str, Any],
    *,
    remove_node_ids: set[str],
    export_id: str,
    generated: str,
) -> dict[str, Any]:
    raw = communities.get("communities", {})
    kept: dict[str, Any] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not isinstance(value, dict):
                continue
            members = [
                member
                for member in value.get("members", [])
                if isinstance(member, str) and member not in remove_node_ids
            ]
            if members:
                kept[str(key)] = {**value, "members": members}
    return {
        **communities,
        "export_id": export_id,
        "generated": generated,
        "communities": kept,
        "total_communities": len(kept),
    }


def _prune_catalog_file(path: Path, remove_slugs: set[str]) -> tuple[dict[str, Any], int]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        catalog = json.load(f)
    if not isinstance(catalog, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return _prune_catalog(catalog, remove_slugs)


def _prune_catalog(catalog: dict[str, Any], remove_slugs: set[str]) -> tuple[dict[str, Any], int]:
    skills = [item for item in catalog.get("skills", []) if isinstance(item, dict)]
    kept = [item for item in skills if str(item.get("ctx_slug") or "") not in remove_slugs]
    pruned = dict(catalog)
    pruned["skills"] = kept
    pruned["observed_unique_skills"] = len(kept)
    pruned["body_available_count"] = sum(1 for item in kept if item.get("body_available"))
    pruned["body_packaged_count"] = sum(1 for item in kept if item.get("converted_path"))
    pruned["body_hydrated_total_count"] = pruned["body_available_count"]
    pruned["skillspector_removed_count"] = len(skills) - len(kept)
    pruned["skillspector_removed_at"] = datetime.now(UTC).isoformat()
    return pruned, len(skills) - len(kept)


def _rewrite_tarball(
    tarball: Path,
    *,
    replacements: dict[str, bytes],
    remove_slugs: set[str],
    apply: bool,
) -> dict[str, int]:
    stats = {"skill_pages_removed": 0, "converted_members_removed": 0}
    reject_symlink_path(tarball)
    if not apply:
        with tarfile.open(tarball, "r:gz") as src:
            for member in src:
                safe_name = _safe_tar_name(member.name)
                if safe_name is None:
                    continue
                if _is_removed_skill_page(safe_name, remove_slugs):
                    stats["skill_pages_removed"] += 1
                elif _is_removed_converted_member(safe_name, remove_slugs):
                    stats["converted_members_removed"] += 1
        return stats

    staged = tarball.with_name(f"{tarball.name}.staged")
    reject_symlink_path(staged)
    skip_names = set(replacements)
    with (
        tarfile.open(tarball, "r:gz") as src,
        tarfile.open(
            staged,
            "w:gz",
            compresslevel=GZIP_COMPRESSLEVEL,
        ) as dst,
    ):
        for member in src:
            safe_name = _safe_tar_name(member.name)
            if safe_name is None:
                continue
            if safe_name in GRAPH_EXPORT_NAMES or safe_name in skip_names:
                continue
            if safe_name.endswith(".original") or safe_name.endswith(".lock"):
                continue
            if safe_name == ".ctx" or safe_name.startswith(".ctx/"):
                continue
            if _is_removed_skill_page(safe_name, remove_slugs):
                stats["skill_pages_removed"] += 1
                continue
            if _is_removed_converted_member(safe_name, remove_slugs):
                stats["converted_members_removed"] += 1
                continue
            if member.isfile():
                source = src.extractfile(member)
                if source is not None:
                    dst.addfile(member, source)
            elif member.isdir():
                dst.addfile(member)
        for name, payload in sorted(replacements.items()):
            _add_bytes(dst, name=f"./{name}", payload=payload)
    promote_staged_artifact(staged, tarball, validate=_validate_tarball)
    return stats


def _is_removed_skill_page(name: str, remove_slugs: set[str]) -> bool:
    if not name.startswith("entities/skills/") or not name.endswith(".md"):
        return False
    slug = name.removeprefix("entities/skills/").removesuffix(".md")
    return slug in remove_slugs


def _is_removed_converted_member(name: str, remove_slugs: set[str]) -> bool:
    if not name.startswith("converted/"):
        return False
    parts = name.split("/", 2)
    return len(parts) >= 2 and parts[1] in remove_slugs


def _add_bytes(tf: tarfile.TarFile, *, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    info.mtime = 0
    tf.addfile(info, BytesIO(payload))


def _validate_tarball(candidate: Path) -> None:
    seen: set[str] = set()
    with tarfile.open(candidate, "r:gz") as tf:
        for member in tf:
            safe_name = _safe_tar_name(member.name)
            if safe_name is None:
                raise ValueError(f"unsafe tar member: {member.name}")
            if safe_name.endswith(".original") or safe_name.endswith(".lock"):
                raise ValueError(f"transient member leaked: {safe_name}")
            if safe_name == ".ctx" or safe_name.startswith(".ctx/"):
                raise ValueError(f"queue state leaked: {safe_name}")
            seen.add(safe_name)
    missing = sorted((GRAPH_EXPORT_NAMES | {"graphify-out/dashboard-neighborhoods.sqlite3"}) - seen)
    if missing:
        raise ValueError(f"candidate tarball missing graph exports: {missing}")


def _json_bytes(data: Any, *, compact: bool) -> bytes:
    if compact:
        return json.dumps(data, separators=(",", ":")).encode("utf-8")
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _gzip_json_bytes(data: Any) -> bytes:
    return gzip.compress(_json_bytes(data, compact=False), compresslevel=GZIP_COMPRESSLEVEL)


def _audit_bytes(records: Iterable[SkillSpectorAuditRecord]) -> bytes:
    lines = [
        json.dumps(record.to_json(), sort_keys=True, separators=(",", ":"))
        for record in sorted(records, key=lambda item: item.slug)
    ]
    return gzip.compress(
        ("\n".join(lines) + "\n").encode("utf-8"), compresslevel=GZIP_COMPRESSLEVEL
    )


def _dashboard_index_bytes(graph: dict[str, Any]) -> bytes:
    with tempfile.TemporaryDirectory(prefix="ctx-skillspector-prune-index-") as tmp:
        tmp_path = Path(tmp)
        graph_path = tmp_path / "graph.json"
        index_path = tmp_path / "dashboard-neighborhoods.sqlite3"
        graph_path.write_bytes(_json_bytes(graph, compact=True))
        build_dashboard_index(graph_path, index_path)
        return index_path.read_bytes()


def _render_delta(
    removed_node_ids: set[str],
    *,
    export_id: str,
    generated: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "full_rebuild": False,
        "export_id": export_id,
        "generated": generated,
        "removed_nodes": sorted(removed_node_ids),
        "nodes": [],
        "edges": [],
    }


def _render_report(
    graph: dict[str, Any],
    communities: dict[str, Any],
    *,
    export_id: str,
    generated: str,
    removed: int,
) -> str:
    nodes, edges = _graph_counts(graph)
    total_communities = int(communities.get("total_communities") or 0)
    return "\n".join(
        [
            "# Graph Report",
            "",
            f"> Generated: {generated}",
            f"> Export ID: {export_id}",
            f"> Nodes: {nodes} | Edges: {edges} | Communities: {total_communities}",
            "",
            "## SkillSpector Prune",
            "",
            f"- Removed skill nodes: {removed}",
            "",
        ]
    )


def _render_manifest(
    graph: dict[str, Any],
    communities: dict[str, Any],
    *,
    export_id: str,
    generated: str,
) -> dict[str, Any]:
    nodes, edges = _graph_counts(graph)
    return {
        "version": 1,
        "export_id": export_id,
        "generated": generated,
        "artifacts": {
            "graph": "graph.json",
            "delta": "graph-delta.json",
            "communities": "communities.json",
            "report": "graph-report.md",
        },
        "counts": {
            "nodes": nodes,
            "edges": edges,
            "communities": int(communities.get("total_communities") or 0),
        },
    }


def _refresh_preview_metadata(
    graph_dir: Path,
    *,
    export_id: str,
    nodes: int,
    edges: int,
) -> None:
    for filename in PREVIEW_HTML_FILES:
        path = graph_dir / filename
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        text = _EXPORT_META_RE.sub(rf"\g<1>{export_id}\3", text)

        def replace_metadata(match: re.Match[str]) -> str:
            try:
                metadata = json.loads(match.group(1))
            except json.JSONDecodeError:
                metadata = {}
            metadata["export_id"] = export_id
            metadata["source_graph_nodes"] = nodes
            metadata["source_graph_edges"] = edges
            return "const CTX_GRAPH_METADATA = " + json.dumps(metadata, sort_keys=True) + ";"

        text = _METADATA_RE.sub(replace_metadata, text)
        atomic_write_text(path, text, encoding="utf-8")


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _print_stats(stats: PruneStats, *, applied: bool) -> None:
    mode = "applied" if applied else "dry-run"
    print(f"SkillSpector prune {mode}:")
    print(f"  remove slugs: {stats.remove_slugs:,}")
    print(f"  graph nodes: {stats.graph_nodes_before:,} -> {stats.graph_nodes_after:,}")
    print(f"  graph edges: {stats.graph_edges_before:,} -> {stats.graph_edges_after:,}")
    print(f"  skill pages removed: {stats.skill_pages_removed:,}")
    print(f"  converted members removed: {stats.converted_members_removed:,}")
    print(f"  catalog entries removed: {stats.catalog_entries_removed:,}")
    print(f"  audit records removed: {stats.audit_records_removed:,}")
    print(f"  export id: {stats.export_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prune SkillSpector removal candidates from graph/wiki artifacts.",
    )
    parser.add_argument(
        "--audit", type=Path, default=REPO_ROOT / "graph/skillspector-audit.jsonl.gz"
    )
    parser.add_argument("--full-tarball", type=Path, default=REPO_ROOT / "graph/wiki-graph.tar.gz")
    parser.add_argument(
        "--runtime-tarball",
        type=Path,
        default=REPO_ROOT / "graph/wiki-graph-runtime.tar.gz",
    )
    parser.add_argument(
        "--catalog", type=Path, default=REPO_ROOT / "graph/skills-sh-catalog.json.gz"
    )
    parser.add_argument("--communities", type=Path, default=REPO_ROOT / "graph/communities.json")
    parser.add_argument("--graph-dir", type=Path, default=REPO_ROOT / "graph")
    parser.add_argument("--apply", action="store_true", help="Rewrite artifacts in place")
    args = parser.parse_args(argv)

    stats = build_pruned_artifacts(
        audit_path=args.audit,
        full_tarball=args.full_tarball,
        runtime_tarball=args.runtime_tarball,
        root_catalog=args.catalog,
        root_communities=args.communities,
        graph_dir=args.graph_dir,
        apply=args.apply,
    )
    _print_stats(stats, applied=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
