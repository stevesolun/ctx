"""Dashboard graph response contract helpers."""

from __future__ import annotations

from typing import Any


GRAPH_SCHEMA = {
    "name": "ctx.dashboard.graph.neighborhood",
    "version": 1,
}

GRAPH_LAYOUT = {
    "kind": "radial-3d",
    "node_size_field": "node_size",
    "node_size_min": 8.0,
    "node_size_max": 24.0,
    "edge_weight_field": "weight",
}

ENTITY_TYPES = ("skill", "agent", "mcp-server", "harness")
SOURCE_EXPLANATIONS = {
    "dashboard-index": "Served from the cached dashboard index for fast cold-start graph browsing.",
    "networkx": "Served from the full in-memory graph because the cached dashboard index was unavailable or not current.",
}


def _node_data(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise ValueError("graph node must be an object")
    data = node.get("data")
    if not isinstance(data, dict):
        raise ValueError("graph node data must be an object")
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise ValueError("graph node data.id must be a non-empty string")
    return data


def _edge_data(edge: Any) -> dict[str, Any]:
    if not isinstance(edge, dict):
        raise ValueError("graph edge must be an object")
    data = edge.get("data")
    if not isinstance(data, dict):
        raise ValueError("graph edge data must be an object")
    if not isinstance(data.get("source"), str) or not data["source"]:
        raise ValueError("graph edge data.source must be a non-empty string")
    if not isinstance(data.get("target"), str) or not data["target"]:
        raise ValueError("graph edge data.target must be a non-empty string")
    return data


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def graph_insights(
    nodes: list[Any],
    edges: list[Any],
    *,
    center: str | None,
    source: str,
) -> dict[str, Any]:
    """Return dashboard-visible counts after validating graph response shape."""
    by_type = {entity_type: 0 for entity_type in ENTITY_TYPES}
    max_degree = 0
    center_degree = 0
    valid_ids: set[str] = set()

    for node in nodes:
        data = _node_data(node)
        node_id = str(data["id"])
        valid_ids.add(node_id)
        entity_type = str(data.get("type") or "skill")
        if entity_type in by_type:
            by_type[entity_type] += 1
        degree = _int_value(data.get("degree"))
        max_degree = max(max_degree, degree)
        if center is not None and node_id == center:
            center_degree = degree

    for edge in edges:
        data = _edge_data(edge)
        if data["source"] not in valid_ids or data["target"] not in valid_ids:
            raise ValueError("graph edge endpoints must exist in nodes")

    return {
        "source": source,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "by_type": by_type,
        "max_degree": max_degree,
        "center_degree": center_degree,
    }


def graph_explanations(payload: dict[str, Any], *, source: str) -> dict[str, str]:
    """Return plain-English graph/search explanations for the dashboard."""
    resolved = payload.get("resolved")
    if isinstance(resolved, dict) and resolved.get("query") and resolved.get("slug"):
        if str(resolved.get("query")) != str(resolved.get("slug")):
            focus = f"Focus search resolved {resolved['query']!r} to {resolved['slug']!r}."
        else:
            focus = f"Focus search matched {resolved['slug']!r}."
    else:
        center = payload.get("center")
        focus = f"Focus search matched {center}." if center else "No graph focus matched."

    return {
        "source": SOURCE_EXPLANATIONS.get(
            source,
            f"Served from graph source {source}.",
        ),
        "search": (
            "Focus search tries exact or normalized slug first, then display "
            "slug, title, and tag matches."
        ),
        "layout": (
            "Node size is cached metadata based on quality, usage, and graph "
            "degree; it is bounded so important nodes stand out without "
            "dominating the graph."
        ),
        "edges": (
            "Edges are sorted by weight and expose shared_tags, reasons, and "
            "available semantic/tag/slug-token signals."
        ),
        "focus": focus,
    }


def enrich_neighborhood(payload: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Attach schema, layout, and insight metadata to a graph neighborhood."""
    nodes = payload.get("nodes")
    edges = payload.get("edges")
    if not isinstance(nodes, list):
        raise ValueError("graph payload nodes must be a list")
    if not isinstance(edges, list):
        raise ValueError("graph payload edges must be a list")
    center = payload.get("center")
    if center is not None and not isinstance(center, str):
        raise ValueError("graph payload center must be a string or null")

    enriched = dict(payload)
    enriched["schema"] = dict(GRAPH_SCHEMA)
    enriched["layout"] = dict(GRAPH_LAYOUT)
    enriched["insights"] = graph_insights(
        nodes,
        edges,
        center=center,
        source=source,
    )
    enriched["explanations"] = graph_explanations(payload, source=source)
    return enriched
