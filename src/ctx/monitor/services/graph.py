"""Read-only graph artifact loading helpers for ctx-monitor."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import zlib
from pathlib import Path
from typing import Any, Callable

from ctx import dashboard_graph
from ctx.core import entity_types as core_entity_types
from ctx.monitor.services import graph_artifacts
from ctx.utils._safe_name import is_safe_source_name


active_dashboard_overlay_records = graph_artifacts.active_dashboard_overlay_records
archive_graph_export_id = graph_artifacts.archive_graph_export_id
dashboard_graph_has_runtime_overlays = graph_artifacts.dashboard_graph_has_runtime_overlays
dashboard_graph_index_archives = graph_artifacts.dashboard_graph_index_archives
dashboard_graph_manifest_export_id = graph_artifacts.dashboard_graph_manifest_export_id
dashboard_index_covers_runtime_overlays = graph_artifacts.dashboard_index_covers_runtime_overlays
dashboard_index_covers_runtime_overlays_for_wiki = (
    graph_artifacts.dashboard_index_covers_runtime_overlays_for_wiki
)
dashboard_index_matches_manifest = graph_artifacts.dashboard_index_matches_manifest
dashboard_index_meta = graph_artifacts.dashboard_index_meta
dashboard_index_uncovered_overlay_nodes = graph_artifacts.dashboard_index_uncovered_overlay_nodes
dashboard_overlay_index_coverage_key = graph_artifacts.dashboard_overlay_index_coverage_key
dashboard_overlay_matches_known_release = graph_artifacts.dashboard_overlay_matches_known_release
dashboard_uncovered_runtime_overlay_nodes = (
    graph_artifacts.dashboard_uncovered_runtime_overlay_nodes
)
dashboard_uncovered_runtime_overlay_nodes_for_wiki = (
    graph_artifacts.dashboard_uncovered_runtime_overlay_nodes_for_wiki
)
ensure_dashboard_graph_index = graph_artifacts.ensure_dashboard_graph_index
packaged_graph_export_id = graph_artifacts.packaged_graph_export_id

_GRAPH_CACHE_KEY: tuple[Any, ...] | None = None
_GRAPH_CACHE_VALUE: Any | None = None
_DASHBOARD_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type for _, entity_type, _ in core_entity_types.entity_source_specs()
)
_GRAPH_REPORT_RE = re.compile(r"Nodes:\s*([\d,]+)\s*\|\s*Edges:\s*([\d,]+)")


class GraphNeighborhoodDeps:
    def __init__(
        self,
        *,
        normalize_entity_type: Callable[[str | None], str | None],
        store_neighborhood: Callable[[str, int, int, str | None], dict[str, Any] | None],
        index_neighborhood: Callable[[str, int, int, str | None], dict[str, Any] | None],
        index_path: Callable[[], Path],
        has_runtime_overlays: Callable[[], bool],
        index_covers_runtime_overlays: Callable[[Path], bool],
        index_matches_manifest: Callable[[Path], bool],
        uncovered_runtime_overlay_nodes: Callable[[Path], set[str] | None],
        load_graph: Callable[[], Any],
        node_size: Callable[..., dict[str, Any]],
        score_payload: Callable[[str, Any], dict[str, float | None]],
    ) -> None:
        self.normalize_entity_type = normalize_entity_type
        self.store_neighborhood = store_neighborhood
        self.index_neighborhood = index_neighborhood
        self.index_path = index_path
        self.has_runtime_overlays = has_runtime_overlays
        self.index_covers_runtime_overlays = index_covers_runtime_overlays
        self.index_matches_manifest = index_matches_manifest
        self.uncovered_runtime_overlay_nodes = uncovered_runtime_overlay_nodes
        self.load_graph = load_graph
        self.node_size = node_size
        self.score_payload = score_payload


def reset_caches() -> None:
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE

    _GRAPH_CACHE_KEY = None
    _GRAPH_CACHE_VALUE = None
    graph_artifacts.reset_caches()


def cached_dashboard_graph() -> Any | None:
    return _GRAPH_CACHE_VALUE


def dashboard_file_cache_key(path: Path) -> tuple[str, float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), stat.st_mtime, stat.st_size)


def dashboard_graph_pack_cache_key(
    packs_dir: Path,
) -> tuple[tuple[str, float, int], ...]:
    if not packs_dir.is_dir():
        return ()
    try:
        files = sorted(path for path in packs_dir.rglob("*") if path.is_file())
    except OSError:
        return (("<unreadable>", 0.0, 0),)
    rows: list[tuple[str, float, int]] = []
    for path in files:
        try:
            stat = path.stat()
            relpath = path.relative_to(packs_dir).as_posix()
        except OSError:
            rows.append((path.name, 0.0, 0))
            continue
        rows.append((relpath, stat.st_mtime, stat.st_size))
    return tuple(rows)


def dashboard_graph_source_cache_key(
    graph_path: Path,
    overlay_path: Path,
) -> tuple[Any, ...] | None:
    graph_key = dashboard_file_cache_key(graph_path)
    overlay_key = dashboard_file_cache_key(overlay_path)
    pack_key = dashboard_graph_pack_cache_key(graph_path.parent / "packs")
    if graph_key is None and not pack_key:
        return None
    return (graph_key, overlay_key, pack_key)


def dashboard_graph_index_path(wiki_dir: Path) -> Path:
    return wiki_dir / "graphify-out" / "dashboard-neighborhoods.sqlite3"


def graph_report_stats(report: Path) -> dict[str, Any] | None:
    try:
        match = _GRAPH_REPORT_RE.search(
            report.read_text(encoding="utf-8", errors="replace"),
        )
    except OSError:
        return None
    if not match:
        return None
    return {
        "nodes": int(match.group(1).replace(",", "")),
        "edges": int(match.group(2).replace(",", "")),
        "available": True,
    }


def dashboard_index_graph_stats(index_path: Path) -> dict[str, Any] | None:
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
        try:
            meta = {
                row[0]: json.loads(row[1]) for row in conn.execute("SELECT key,value FROM meta")
            }
            nodes = int(meta.get("nodes_count") or 0)
            return {
                "nodes": nodes,
                "edges": int(meta.get("edges_count") or 0),
                "available": nodes > 0,
            }
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return None


def dashboard_index_wiki_stats(
    index_path: Path,
    *,
    index_matches_manifest: Callable[[Path], bool],
) -> dict[str, int | bool] | None:
    if not index_path.is_file() or not index_matches_manifest(index_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
        try:
            rows = {
                str(row[0]): int(row[1])
                for row in conn.execute("SELECT type,COUNT(*) FROM nodes GROUP BY type")
            }
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None

    stats: dict[str, int | bool] = {
        "skills": rows.get("skill", 0),
        "agents": rows.get("agent", 0),
        "mcps": rows.get("mcp-server", 0),
        "harnesses": rows.get("harness", 0),
    }
    stats["total"] = (
        int(stats["skills"]) + int(stats["agents"]) + int(stats["mcps"]) + int(stats["harnesses"])
    )
    stats["split_known"] = True
    return stats


def top_degree_seeds_from_index(index_path: Path, limit: int = 18) -> list[dict[str, Any]]:
    if not index_path.is_file():
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id,label,type,degree FROM nodes ORDER BY degree DESC,id LIMIT ?",
            (max(1, limit),),
        ).fetchall()
    except (OSError, sqlite3.Error, TimeoutError):
        return []
    finally:
        if conn is not None:
            conn.close()
    return [
        {
            "slug": graph_slug_from_node_id(str(row["id"])),
            "type": graph_type_from_node_id(str(row["id"]), str(row["type"] or "skill")),
            "degree": int(row["degree"] or 0),
            "label": row["label"] or graph_slug_from_node_id(str(row["id"])),
        }
        for row in rows
    ]


def top_degree_seeds_from_graph(graph: Any, limit: int = 18) -> list[dict[str, Any]]:
    if graph is None or graph.number_of_nodes() == 0:
        return []
    ranked = sorted(graph.degree, key=lambda kv: -kv[1])[:limit]
    out: list[dict[str, Any]] = []
    for node_id, degree in ranked:
        prefix, _, slug = str(node_id).partition(":")
        seed_type = {
            "mcp-server": "mcp-server",
            "harness": "harness",
            "agent": "agent",
        }.get(prefix, "skill")
        out.append(
            {
                "slug": slug,
                "type": seed_type,
                "degree": int(degree),
                "label": graph.nodes[node_id].get("label", slug),
            }
        )
    return out


def _slugish(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _display_slug(slug: str) -> str:
    return str(slug or "").removeprefix("skills-sh-")


def _display_label(value: Any, *, fallback_slug: str = "") -> str:
    return _display_slug(str(value or fallback_slug or ""))


def graph_slug_from_node_id(node_id: str) -> str:
    return node_id.split(":", 1)[-1]


def graph_type_from_node_id(node_id: str, fallback: str = "skill") -> str:
    prefix = node_id.split(":", 1)[0] if ":" in node_id else ""
    return {
        "skill": "skill",
        "agent": "agent",
        "mcp-server": "mcp-server",
        "harness": "harness",
    }.get(prefix, fallback)


def normalize_dashboard_entity_type(raw: object) -> str | None:
    return core_entity_types.normalize_entity_type(
        raw,
        allowed=_DASHBOARD_ENTITY_TYPES,
    )


def unit_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return max(0.0, min(1.0, score))


def dashboard_score_payload(field: str, value: Any) -> dict[str, float | None]:
    score = unit_score(value)
    payload: dict[str, float | None] = {field: score}
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return payload
    if math.isfinite(raw) and score is not None and raw != score:
        payload[f"{field}_raw"] = raw
    return payload


def node_size_from_scores(
    *,
    quality: Any,
    usage: Any,
    degree: int,
    max_degree: int,
) -> dict[str, Any]:
    quality_value = 0.35 if quality is None else float(quality)
    usage_value = 0.0 if usage is None else float(usage)
    popularity = (
        math.log1p(max(0, degree)) / math.log1p(max(1, max_degree)) if max_degree > 0 else 0.0
    )
    signal = max(
        0.0,
        min(1.0, 0.45 * quality_value + 0.35 * usage_value + 0.20 * popularity),
    )
    return {
        "node_size": round(8.0 + signal * 16.0, 2),
        "size_signal": round(signal, 4),
        "size_reason": (
            f"quality {quality_value:.3f}; usage {usage_value:.3f}; popularity {popularity:.3f}"
        ),
    }


def graph_node_size(
    nid: str,
    data: dict[str, Any],
    *,
    entity_type: str,
    degree: int,
    max_degree: int,
    sidecar_score_inputs: Callable[[str, str], tuple[float | None, float | None]],
) -> dict[str, Any]:
    """Return bounded visual size metadata for a graph node."""
    slug = graph_slug_from_node_id(nid)
    quality = unit_score(data.get("quality_score"))
    usage = unit_score(data.get("usage_score"))
    if quality is None or usage is None:
        sidecar_quality, sidecar_usage = sidecar_score_inputs(slug, entity_type)
        quality = quality if quality is not None else sidecar_quality
        usage = usage if usage is not None else sidecar_usage
    return node_size_from_scores(
        quality=quality,
        usage=usage,
        degree=degree,
        max_degree=max_degree,
    )


def index_node_size(
    *,
    slug: str,
    entity_type: str,
    quality: Any,
    usage: Any,
    degree: int,
    max_degree: int,
    sidecar_score_inputs: Callable[[str, str], tuple[float | None, float | None]],
) -> dict[str, Any]:
    quality_value = unit_score(quality)
    usage_value = unit_score(usage)
    if quality_value is None or usage_value is None:
        sidecar_quality, sidecar_usage = sidecar_score_inputs(slug, entity_type)
        quality_value = quality_value if quality_value is not None else sidecar_quality
        usage_value = usage_value if usage_value is not None else sidecar_usage
    return node_size_from_scores(
        quality=quality_value,
        usage=usage_value,
        degree=degree,
        max_degree=max_degree,
    )


def resolve_index_center(
    conn: sqlite3.Connection,
    raw_query: str,
    entity_type: str | None,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    raw_query = str(raw_query or "").strip()
    if not raw_query or "/" in raw_query or "\\" in raw_query or ".." in raw_query:
        return None, None, []
    normalized_query = _slugish(raw_query)
    if not normalized_query or not is_safe_source_name(normalized_query):
        return None, None, []

    normalized_type = core_entity_types.normalize_entity_type(
        entity_type,
        allowed=_DASHBOARD_ENTITY_TYPES,
    )
    if entity_type is not None and normalized_type is None:
        return None, None, []
    entity_types = (normalized_type,) if normalized_type is not None else _DASHBOARD_ENTITY_TYPES

    candidates: list[str] = []
    for candidate in (raw_query, normalized_query):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for current_type in entity_types:
        for candidate_slug in candidates:
            row = conn.execute(
                "SELECT node_id FROM slug_index WHERE slug=? AND type=? LIMIT 1",
                (candidate_slug, current_type),
            ).fetchone()
            if row is not None:
                return str(row["node_id"]), None, [candidate_slug]

    where = ""
    params: list[Any] = []
    if normalized_type is not None:
        where = "WHERE s.type=?"
        params.append(normalized_type)
    rows = conn.execute(
        "SELECT s.slug,s.type,s.node_id,n.label,n.tags,n.degree "
        "FROM slug_index s JOIN nodes n ON n.id=s.node_id "
        f"{where}",
        params,
    )
    matches: list[tuple[tuple[int, int, int], str, str]] = []
    query_tokens = set(normalized_query.split("-"))
    for row in rows:
        node_slug = str(row["slug"] or "")
        label = _display_label(row["label"], fallback_slug=node_slug)
        haystacks = {
            _slugish(node_slug),
            _slugish(_display_slug(node_slug)),
            _slugish(label),
        }
        try:
            tags = json.loads(row["tags"] or "[]")
        except (TypeError, json.JSONDecodeError):
            tags = []
        if isinstance(tags, list):
            haystacks.update(_slugish(str(tag)) for tag in tags[:12])
        rank = None
        if normalized_query in haystacks:
            rank = 0
        elif any(h.startswith(normalized_query) for h in haystacks):
            rank = 1
        elif any(normalized_query in h for h in haystacks):
            rank = 2
        elif query_tokens and all(any(token in h for h in haystacks) for token in query_tokens):
            rank = 3
        if rank is None:
            continue
        try:
            degree = int(row["degree"] or 0)
        except (TypeError, ValueError):
            degree = 0
        matches.append(((rank, len(node_slug), -degree), str(row["node_id"]), node_slug))

    matches.sort(key=lambda item: item[0])
    suggestions: list[str] = []
    for _, _node_id, suggestion in matches[:8]:
        display_suggestion = _display_slug(suggestion)
        if display_suggestion not in suggestions:
            suggestions.append(display_suggestion)
    if not matches:
        return None, None, suggestions
    center = matches[0][1]
    return (
        center,
        {"query": raw_query, "slug": graph_slug_from_node_id(center), "id": center},
        suggestions,
    )


def dashboard_index_neighborhood(
    conn: sqlite3.Connection,
    slug: str,
    *,
    hops: int,
    limit: int,
    entity_type: str | None,
    node_size: Callable[..., dict[str, Any]],
    score_payload: Callable[[str, Any], dict[str, float | None]],
) -> dict[str, Any] | None:
    try:
        meta = {
            row["key"]: json.loads(row["value"])
            for row in conn.execute("SELECT key,value FROM meta")
        }
        max_degree = int(meta.get("max_degree") or 1)
        top_k = int(meta.get("top_k") or 0)
        if hops > 1 or (top_k > 0 and limit > top_k):
            return None

        center, resolved, suggestions = resolve_index_center(conn, slug, entity_type)
        if center is None:
            return {"nodes": [], "edges": [], "center": None, "suggestions": suggestions}

        nodes_out: dict[str, dict[str, Any]] = {}
        edges_out: list[dict[str, Any]] = []
        emitted_edges: set[tuple[str, str]] = set()
        frontier = [center]
        seen = {center}

        def add_node(node_id: str, depth: int) -> None:
            if node_id in nodes_out:
                return
            row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if row is None:
                return
            tags = json.loads(row["tags"] or "[]")
            degree = int(row["degree"] or 0)
            node_type = str(row["type"] or graph_type_from_node_id(node_id))
            node_slug = graph_slug_from_node_id(node_id)
            size_data = node_size(
                slug=node_slug,
                entity_type=node_type,
                quality=row["quality_score"],
                usage=row["usage_score"],
                degree=degree,
                max_degree=max_degree,
            )
            label = _display_label(row["label"], fallback_slug=node_slug)
            nodes_out[node_id] = {
                "data": {
                    "id": node_id,
                    "label": label,
                    "type": node_type,
                    "depth": depth,
                    "degree": degree,
                    "tags": tags[:6],
                    "description": row["description"] or "",
                    **score_payload("quality_score", row["quality_score"]),
                    **score_payload("usage_score", row["usage_score"]),
                    "filter_tokens": [
                        node_id,
                        row["label"],
                        node_slug,
                        _display_slug(node_slug),
                        label,
                        *tags,
                    ],
                    **size_data,
                },
            }

        add_node(center, 0)
        for depth in range(1, hops + 1):
            next_frontier: list[str] = []
            for node_id in frontier:
                row = conn.execute(
                    "SELECT payload FROM neighbors WHERE source=?",
                    (node_id,),
                ).fetchone()
                if row is None:
                    continue
                neighbors = json.loads(zlib.decompress(row["payload"]).decode("utf-8"))
                for edge in neighbors:
                    if len(nodes_out) >= limit:
                        break
                    other = str(edge.get("target") or "")
                    if not other:
                        continue
                    add_node(other, depth)
                    edge_a, edge_b = sorted((node_id, other))
                    edge_key = (edge_a, edge_b)
                    if edge_key not in emitted_edges and other in nodes_out:
                        emitted_edges.add(edge_key)
                        shared_tags = list(edge.get("shared_tags") or [])[:4]
                        for current in (node_id, other):
                            tokens = nodes_out[current]["data"].setdefault(
                                "filter_tokens",
                                [],
                            )
                            tokens.extend(shared_tags)
                        edges_out.append(
                            {
                                "data": {
                                    "id": f"{edge_key[0]}__{edge_key[1]}",
                                    "source": node_id,
                                    "target": other,
                                    "weight": edge.get("weight", 1),
                                    "shared_tags": shared_tags,
                                    "reasons": edge.get("reasons", []),
                                    "semantic": edge.get("semantic"),
                                    "tag_sim": edge.get("tag_sim"),
                                    "slug_token_sim": edge.get("slug_token_sim"),
                                    "source_overlap": edge.get("source_overlap"),
                                },
                            }
                        )
                    if other not in seen:
                        seen.add(other)
                        next_frontier.append(other)
                if len(nodes_out) >= limit:
                    break
            frontier = next_frontier
            if len(nodes_out) >= limit:
                break
        return dashboard_graph.enrich_neighborhood(
            {
                "nodes": list(nodes_out.values()),
                "edges": edges_out,
                "center": center,
                "resolved": resolved or {"source": "dashboard-index"},
                "suggestions": [],
            },
            source="dashboard-index",
        )
    except (OSError, sqlite3.Error, json.JSONDecodeError, zlib.error, KeyError, TypeError):
        return None


def dashboard_index_neighborhood_from_path(
    index_path: Path | None,
    slug: str,
    *,
    hops: int,
    limit: int,
    entity_type: str | None,
    sidecar_score_inputs: Callable[[str, str], tuple[float | None, float | None]],
) -> dict[str, Any] | None:
    if index_path is None or not index_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        return dashboard_index_neighborhood(
            conn,
            slug,
            hops=hops,
            limit=limit,
            entity_type=entity_type,
            node_size=lambda **kwargs: index_node_size(
                **kwargs,
                sidecar_score_inputs=sidecar_score_inputs,
            ),
            score_payload=dashboard_score_payload,
        )
    finally:
        conn.close()


def graph_store_neighborhood(
    graph_dir: Path,
    slug: str,
    *,
    hops: int,
    limit: int,
    entity_type: str | None,
    node_size: Callable[..., dict[str, Any]],
    score_payload: Callable[[str, Any], dict[str, float | None]],
) -> dict[str, Any] | None:
    if hops > 1:
        return None
    store_path = graph_dir / "graph-store.sqlite3"
    if not store_path.is_file():
        return None
    try:
        from ctx.core.graph.graph_store import (  # noqa: PLC0415
            graph_store_is_fresh,
            load_neighborhood,
            search_nodes,
        )
    except ImportError:
        return None
    try:
        if not graph_store_is_fresh(store_path, graph_dir):
            return None
        center, resolved, suggestions = resolve_graph_store_center(
            store_path,
            slug,
            entity_type,
            search_nodes,
        )
        if center is None:
            return {"nodes": [], "edges": [], "center": None, "suggestions": suggestions}
        neighborhood = load_neighborhood(store_path, center, limit=max(1, limit - 1))
    except (OSError, sqlite3.DatabaseError, ValueError, TypeError):
        return None
    return dashboard_payload_from_graph_store(
        center=center,
        resolved=resolved or {"source": "graph-store"},
        suggestions=suggestions,
        neighborhood=neighborhood,
        node_size=node_size,
        score_payload=score_payload,
    )


def graph_neighborhood(
    slug: str,
    *,
    hops: int = 1,
    limit: int = 40,
    entity_type: str | None = None,
    deps: GraphNeighborhoodDeps,
) -> dict[str, Any]:
    if "/" in slug or "\\" in slug or ".." in slug:
        return {"nodes": [], "edges": [], "center": None}
    normalized_entity_type = deps.normalize_entity_type(entity_type)
    stored = deps.store_neighborhood(slug, hops, limit, normalized_entity_type)
    if stored is not None:
        return stored
    index_path = deps.index_path()
    has_runtime_overlays = deps.has_runtime_overlays()
    index_covers_overlays = not has_runtime_overlays or deps.index_covers_runtime_overlays(
        index_path
    )
    if index_covers_overlays:
        indexed = deps.index_neighborhood(slug, hops, limit, normalized_entity_type)
        if indexed is not None:
            return indexed
    elif hops == 1 and index_path.is_file() and deps.index_matches_manifest(index_path):
        indexed = deps.index_neighborhood(slug, hops, limit, normalized_entity_type)
        center = indexed.get("center") if isinstance(indexed, dict) else None
        uncovered = deps.uncovered_runtime_overlay_nodes(index_path)
        if indexed is not None and isinstance(center, str) and uncovered is not None:
            if center not in uncovered:
                return indexed
    try:
        graph = deps.load_graph()
    except Exception:  # noqa: BLE001 - graph is advisory; blank on error
        return {"nodes": [], "edges": [], "center": None}
    if graph.number_of_nodes() == 0:
        return {"nodes": [], "edges": [], "center": None}

    if entity_type is not None and normalized_entity_type is None:
        return {"nodes": [], "edges": [], "center": None}
    center, resolved, suggestions = resolve_graph_center(
        graph,
        slug,
        normalized_entity_type,
    )
    if center is None:
        return {"nodes": [], "edges": [], "center": None}

    nodes_out: dict[str, dict[str, Any]] = {}
    edges_out: list[dict[str, Any]] = []
    emitted_edges: set[tuple[str, str]] = set()
    frontier = [center]
    seen: set[str] = {center}
    try:
        max_degree = max((int(degree) for _node, degree in graph.degree()), default=1)
    except Exception:  # noqa: BLE001
        max_degree = 1

    def add_node(node_id: str, depth: int) -> None:
        if node_id in nodes_out:
            return
        data = dict(graph.nodes.get(node_id, {}))
        node_slug = graph_slug_from_node_id(node_id)
        label = _display_label(data.get("label"), fallback_slug=node_slug)
        tags = list(data.get("tags", []))
        node_type = str(data.get("type") or graph_type_from_node_id(node_id))
        try:
            degree = int(graph.degree[node_id])
        except Exception:  # noqa: BLE001
            degree = 0
        nodes_out[node_id] = {
            "data": {
                "id": node_id,
                "label": label,
                "type": node_type,
                "depth": depth,
                "degree": degree,
                "tags": tags[:6],
                "description": data.get("description", ""),
                **deps.score_payload("quality_score", data.get("quality_score")),
                **deps.score_payload("usage_score", data.get("usage_score")),
                "filter_tokens": [
                    node_id,
                    label,
                    node_slug,
                    _display_slug(node_slug),
                    *tags,
                ],
                **deps.node_size(
                    node_id,
                    data,
                    entity_type=node_type,
                    degree=degree,
                    max_degree=max_degree,
                ),
            },
        }

    add_node(center, 0)

    for depth in range(1, hops + 1):
        next_frontier: list[str] = []
        for node_id in frontier:
            neighbors = sorted(
                graph[node_id].items(),
                key=lambda kv: -kv[1].get("weight", 1),
            )
            for other, edata in neighbors:
                if len(nodes_out) >= limit:
                    break
                add_node(other, depth)
                edge_key = tuple(sorted((node_id, other)))
                if edge_key not in emitted_edges:
                    emitted_edges.add(edge_key)
                    shared_tags = edata.get("shared_tags", [])[:4]
                    for current in (node_id, other):
                        tokens = nodes_out[current]["data"].setdefault(
                            "filter_tokens",
                            [],
                        )
                        tokens.extend(shared_tags)
                    edges_out.append(
                        {
                            "data": {
                                "id": f"{edge_key[0]}__{edge_key[1]}",
                                "source": node_id,
                                "target": other,
                                "weight": edata.get("weight", 1),
                                "shared_tags": shared_tags,
                                "reasons": edata.get("reasons", []),
                                "semantic": edata.get("semantic"),
                                "tag_sim": edata.get("tag_sim"),
                                "slug_token_sim": edata.get("slug_token_sim"),
                                "source_overlap": edata.get("source_overlap"),
                            },
                        }
                    )
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
            if len(nodes_out) >= limit:
                break
        frontier = next_frontier
        if len(nodes_out) >= limit:
            break

    return dashboard_graph.enrich_neighborhood(
        {
            "nodes": list(nodes_out.values()),
            "edges": edges_out,
            "center": center,
            "resolved": resolved,
            "suggestions": suggestions,
        },
        source="networkx",
    )


def resolve_graph_center(
    graph: Any,
    slug: str,
    entity_type: str | None,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    raw_query = str(slug or "").strip()
    if not raw_query or "/" in raw_query or "\\" in raw_query or ".." in raw_query:
        return None, None, []
    normalized_query = _slugish(raw_query)
    if not normalized_query or not is_safe_source_name(normalized_query):
        return None, None, []

    entity_types = (entity_type,) if entity_type is not None else _DASHBOARD_ENTITY_TYPES
    for current_type in entity_types:
        for candidate_slug in (raw_query, normalized_query):
            candidate = f"{current_type}:{candidate_slug}"
            if candidate in graph:
                return candidate, None, [candidate_slug]

    matches: list[tuple[tuple[int, int, int], str, str]] = []
    query_tokens = set(normalized_query.split("-"))
    for node_id in graph.nodes:
        node_type = graph_type_from_node_id(str(node_id))
        if node_type not in entity_types:
            continue
        data = graph.nodes.get(node_id, {})
        node_slug = graph_slug_from_node_id(str(node_id))
        label = _display_label(data.get("label"), fallback_slug=node_slug)
        haystacks = {
            _slugish(node_slug),
            _slugish(_display_slug(node_slug)),
            _slugish(label),
        }
        tags = data.get("tags", [])
        if isinstance(tags, list):
            haystacks.update(_slugish(str(tag)) for tag in tags[:12])
        rank = None
        if normalized_query in haystacks:
            rank = 0
        elif any(h.startswith(normalized_query) for h in haystacks):
            rank = 1
        elif any(normalized_query in h for h in haystacks):
            rank = 2
        elif query_tokens and all(
            any(token in haystack for haystack in haystacks) for token in query_tokens
        ):
            rank = 3
        if rank is None:
            continue
        try:
            degree = int(graph.degree[node_id])
        except Exception:  # noqa: BLE001
            degree = 0
        matches.append(((rank, len(node_slug), -degree), str(node_id), node_slug))

    matches.sort(key=lambda item: item[0])
    suggestions: list[str] = []
    for _, _node_id, suggestion in matches[:8]:
        display_suggestion = _display_slug(suggestion)
        if display_suggestion not in suggestions:
            suggestions.append(display_suggestion)
    if not matches:
        return None, None, suggestions
    center = matches[0][1]
    resolved_slug = graph_slug_from_node_id(center)
    return center, {"query": raw_query, "slug": resolved_slug, "id": center}, suggestions


def resolve_graph_store_center(
    store_path: Path,
    raw_query: str,
    entity_type: str | None,
    search_nodes: Callable[..., list[dict[str, Any]]],
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    raw_query = str(raw_query or "").strip()
    if not raw_query or "/" in raw_query or "\\" in raw_query or ".." in raw_query:
        return None, None, []
    normalized_query = _slugish(raw_query)
    if not normalized_query or not is_safe_source_name(normalized_query):
        return None, None, []

    entity_types = (entity_type,) if entity_type is not None else _DASHBOARD_ENTITY_TYPES
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for query in (raw_query, normalized_query):
        for row in search_nodes(store_path, query, limit=25):
            node_id = str(row.get("id") or "")
            if not node_id or node_id in seen_ids:
                continue
            seen_ids.add(node_id)
            rows.append(row)

    suggestions: list[str] = []
    for row in rows[:8]:
        node_id = str(row.get("id") or "")
        node_slug = graph_slug_from_node_id(node_id)
        display_suggestion = _display_slug(node_slug)
        if display_suggestion not in suggestions:
            suggestions.append(display_suggestion)

    matches: list[tuple[tuple[int, int], str, str]] = []
    for row in rows:
        node_id = str(row.get("id") or "")
        node_type = str(row.get("type") or graph_type_from_node_id(node_id))
        if node_type not in entity_types:
            continue
        node_slug = graph_slug_from_node_id(node_id)
        label = _display_label(row.get("label"), fallback_slug=node_slug)
        haystacks = {
            _slugish(node_slug),
            _slugish(_display_slug(node_slug)),
            _slugish(label),
        }
        for tag in row.get("tags") or []:
            haystacks.add(_slugish(str(tag)))
        if normalized_query in haystacks:
            rank = 0
        elif any(h.startswith(normalized_query) for h in haystacks):
            rank = 1
        elif any(normalized_query in h for h in haystacks):
            rank = 2
        else:
            continue
        matches.append(((rank, len(node_slug)), node_id, node_slug))

    matches.sort(key=lambda item: item[0])
    if not matches:
        return None, None, suggestions
    center = matches[0][1]
    resolved_slug = graph_slug_from_node_id(center)
    return center, {"query": raw_query, "slug": resolved_slug, "id": center}, suggestions


def dashboard_payload_from_graph_store(
    *,
    center: str,
    resolved: dict[str, str],
    suggestions: list[str],
    neighborhood: dict[str, list[dict[str, Any]]],
    node_size: Callable[..., dict[str, Any]],
    score_payload: Callable[[str, Any], dict[str, float | None]],
) -> dict[str, Any]:
    raw_nodes = neighborhood.get("nodes", [])
    raw_edges = neighborhood.get("edges", [])
    degree_by_node: dict[str, int] = {str(node.get("id") or ""): 0 for node in raw_nodes}
    for edge in raw_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if source in degree_by_node:
            degree_by_node[source] += 1
        if target in degree_by_node:
            degree_by_node[target] += 1
    max_degree = max(degree_by_node.values(), default=1)

    nodes_out: list[dict[str, Any]] = []
    for node in raw_nodes:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        node_slug = graph_slug_from_node_id(node_id)
        node_type = str(node.get("type") or graph_type_from_node_id(node_id))
        tags = [str(tag) for tag in node.get("tags", []) if isinstance(tag, str)]
        label = _display_label(node.get("label"), fallback_slug=node_slug)
        degree = degree_by_node.get(node_id, 0)
        size_data = node_size(
            node_id,
            {},
            entity_type=node_type,
            degree=degree,
            max_degree=max_degree,
        )
        nodes_out.append(
            {
                "data": {
                    "id": node_id,
                    "label": label,
                    "type": node_type,
                    "depth": 0 if node_id == center else 1,
                    "degree": degree,
                    "tags": tags[:6],
                    "description": "",
                    **score_payload("quality_score", None),
                    **score_payload("usage_score", None),
                    "filter_tokens": [
                        node_id,
                        label,
                        node_slug,
                        _display_slug(node_slug),
                        *tags,
                    ],
                    **size_data,
                },
            }
        )

    edges_out: list[dict[str, Any]] = []
    for edge in raw_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        raw_attrs = edge.get("attrs")
        attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
        edge_key = tuple(sorted((source, target)))
        raw_shared_tags = attrs.get("shared_tags")
        shared_tags = (
            [str(tag) for tag in raw_shared_tags[:4]] if isinstance(raw_shared_tags, list) else []
        )
        raw_reasons = attrs.get("reasons")
        reasons = [str(reason) for reason in raw_reasons] if isinstance(raw_reasons, list) else []
        edges_out.append(
            {
                "data": {
                    "id": f"{edge_key[0]}__{edge_key[1]}",
                    "source": source,
                    "target": target,
                    "weight": edge.get("weight", attrs.get("weight", 1)),
                    "shared_tags": shared_tags,
                    "reasons": reasons,
                    "semantic": attrs.get("semantic", attrs.get("semantic_sim")),
                    "tag_sim": attrs.get("tag_sim"),
                    "slug_token_sim": attrs.get("slug_token_sim"),
                    "source_overlap": attrs.get("source_overlap"),
                },
            }
        )

    return dashboard_graph.enrich_neighborhood(
        {
            "nodes": nodes_out,
            "edges": edges_out,
            "center": center,
            "resolved": resolved,
            "suggestions": suggestions,
        },
        source="graph-store",
    )


def load_dashboard_graph(
    wiki_dir: Path,
    load_graph: Callable[..., Any],
) -> Any:
    """Load the dashboard graph once per graph artifact version."""
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE

    graph_path = wiki_dir / "graphify-out" / "graph.json"
    overlay_path = graph_path.with_name("entity-overlays.jsonl")
    source_key = dashboard_graph_source_cache_key(graph_path, overlay_path)
    if source_key is None:
        _GRAPH_CACHE_KEY = None
        _GRAPH_CACHE_VALUE = None
        return load_graph(graph_path)

    cache_key = (id(load_graph), source_key)
    if _GRAPH_CACHE_KEY == cache_key and _GRAPH_CACHE_VALUE is not None:
        return _GRAPH_CACHE_VALUE

    try:
        graph = load_graph(graph_path, apply_runtime_filter=False)
    except TypeError:
        graph = load_graph(graph_path)
    _GRAPH_CACHE_KEY = cache_key
    _GRAPH_CACHE_VALUE = graph
    return graph


def dashboard_graph_stats(
    wiki_dir: Path,
    *,
    ensure_index: Callable[[], Path | None],
    load_graph: Callable[[], Any],
) -> dict[str, Any]:
    """Top-line graph stats for dashboard home and API responses."""
    report_stats = graph_report_stats(wiki_dir / "graphify-out" / "graph-report.md")
    if report_stats is not None:
        return report_stats

    index_path = ensure_index()
    if index_path is not None and index_path.is_file():
        index_stats = dashboard_index_graph_stats(index_path)
        if index_stats is not None:
            return index_stats
    try:
        graph = load_graph()
    except Exception:  # noqa: BLE001
        return {"nodes": 0, "edges": 0, "available": False}
    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "available": graph.number_of_nodes() > 0,
    }


def graph_neighborhood_for_monitor(
    slug: str,
    *,
    hops: int,
    limit: int,
    entity_type: str | None,
    wiki_dir: Path,
    index_path: Callable[[], Path],
    ensure_index: Callable[[], Path | None],
    load_graph: Callable[[], Any],
    sidecar_score_inputs: Callable[[str, str], tuple[float | None, float | None]],
) -> dict[str, Any]:
    """Resolve a dashboard graph neighborhood through the fastest valid reader."""

    def _index_neighborhood(
        current_slug: str,
        current_hops: int,
        current_limit: int,
        current_type: str | None,
    ) -> dict[str, Any] | None:
        return dashboard_index_neighborhood_from_path(
            ensure_index(),
            current_slug,
            hops=current_hops,
            limit=current_limit,
            entity_type=current_type,
            sidecar_score_inputs=sidecar_score_inputs,
        )

    def _store_neighborhood(
        current_slug: str,
        current_hops: int,
        current_limit: int,
        current_type: str | None,
    ) -> dict[str, Any] | None:
        return graph_store_neighborhood(
            wiki_dir / "graphify-out",
            current_slug,
            hops=current_hops,
            limit=current_limit,
            entity_type=current_type,
            node_size=lambda *args, **kwargs: graph_node_size(
                *args,
                **kwargs,
                sidecar_score_inputs=sidecar_score_inputs,
            ),
            score_payload=dashboard_score_payload,
        )

    return graph_neighborhood(
        slug,
        hops=hops,
        limit=limit,
        entity_type=entity_type,
        deps=GraphNeighborhoodDeps(
            normalize_entity_type=normalize_dashboard_entity_type,
            store_neighborhood=_store_neighborhood,
            index_neighborhood=_index_neighborhood,
            index_path=index_path,
            has_runtime_overlays=lambda: dashboard_graph_has_runtime_overlays(wiki_dir),
            index_covers_runtime_overlays=lambda path: (
                dashboard_index_covers_runtime_overlays_for_wiki(path, wiki_dir)
            ),
            index_matches_manifest=lambda path: dashboard_index_matches_manifest(
                path,
                wiki_dir,
            ),
            uncovered_runtime_overlay_nodes=lambda path: (
                dashboard_uncovered_runtime_overlay_nodes_for_wiki(path, wiki_dir)
            ),
            load_graph=load_graph,
            node_size=lambda *args, **kwargs: graph_node_size(
                *args,
                **kwargs,
                sidecar_score_inputs=sidecar_score_inputs,
            ),
            score_payload=dashboard_score_payload,
        ),
    )
