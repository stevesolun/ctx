"""Read-only JSON route payloads for the ctx dashboard."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ctx.utils._safe_name import is_safe_source_name


_RUNTIME_GRAPH_ENTITY_TYPES = ("skill", "agent", "mcp-server", "harness")
_RUNTIME_GRAPH_TITLE_MAX_CHARS = 200
_RUNTIME_GRAPH_DESCRIPTION_MAX_CHARS = 1_000
_RUNTIME_GRAPH_BODY_MAX_CHARS = 4_000
_RUNTIME_GRAPH_TAG_MAX_CHARS = 80
_RUNTIME_GRAPH_TAG_LIMIT = 12
_GRAPH_SCALAR_TYPES = (str, int, float, bool)


@dataclass(frozen=True)
class ReadOnlyApiResponse:
    payload: Any
    status: int = 200
    not_found_detail: str | None = None


@dataclass(frozen=True)
class ReadOnlyApiDeps:
    summarize_sessions: Callable[[], Any]
    read_manifest: Callable[[], Any]
    status_payload: Callable[[], Any]
    kpi_summary: Callable[[], Any | None]
    grade_distribution_payload: Callable[[], Any]
    sidecar_page_payload: Callable[[dict[str, str]], Any]
    runtime_lifecycle_summary: Callable[[], Any]
    skillspector_audit_payload: Callable[[dict[str, str]], Any]
    effective_config_payload: Callable[[], Any]
    search_wiki_entities: Callable[[str, str | None, int], list[dict[str, Any]]]
    wiki_entity_detail: Callable[[str, str | None], Any | None]
    load_sidecar: Callable[[str, str | None], Any | None]
    graph_neighborhood: Callable[[str, int, int, str | None], Any]
    normalize_dashboard_entity_type: Callable[[str | None], str | None]


def _grade_payload_from_summary(summary: Any) -> dict[str, Any] | None:
    """Return grade counts from the cached KPI summary when available."""
    if summary is None:
        return None
    to_dict = getattr(summary, "to_dict", None)
    data = to_dict() if callable(to_dict) else summary
    if not isinstance(data, Mapping):
        return None
    raw_counts = data.get("grade_counts")
    if not isinstance(raw_counts, Mapping):
        return None
    grades: dict[str, int] = {}
    for grade in ("A", "B", "C", "D", "F"):
        try:
            grades[grade] = int(raw_counts.get(grade) or 0)
        except (TypeError, ValueError):
            grades[grade] = 0
    return {"grades": grades, "total": sum(grades.values())}


def _bounded_graph_text(
    value: Any,
    *,
    default: str,
    max_chars: int,
) -> tuple[bool, str]:
    if value is None or value == "":
        return True, default[:max_chars]
    if not isinstance(value, _GRAPH_SCALAR_TYPES):
        return False, ""
    try:
        return True, str(value)[:max_chars]
    except (OverflowError, ValueError):
        return False, ""


def _runtime_graph_entity_detail_for_type(
    slug: str,
    entity_type: str,
    deps: ReadOnlyApiDeps,
) -> dict[str, Any] | None:
    graph = deps.graph_neighborhood(slug, 1, 1, entity_type)
    if not isinstance(graph, Mapping):
        return None
    center = graph.get("center")
    nodes = graph.get("nodes")
    if not isinstance(center, str) or not center or not isinstance(nodes, list):
        return None

    center_data: Mapping[str, Any] | None = None
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        data = node.get("data")
        if isinstance(data, Mapping) and data.get("id") == center:
            center_data = data
            break
    if center_data is None:
        return None

    prefix, separator, resolved_slug = center.partition(":")
    raw_type = center_data.get("type")
    if raw_type is not None and not isinstance(raw_type, str):
        return None
    resolved_type = deps.normalize_dashboard_entity_type(raw_type or prefix)
    if (
        not separator
        or resolved_slug != slug
        or resolved_type is None
        or center != f"{resolved_type}:{slug}"
        or resolved_type != entity_type
    ):
        return None

    title_ok, title = _bounded_graph_text(
        center_data.get("label"),
        default=slug,
        max_chars=_RUNTIME_GRAPH_TITLE_MAX_CHARS,
    )
    description_ok, description = _bounded_graph_text(
        center_data.get("description"),
        default="",
        max_chars=_RUNTIME_GRAPH_DESCRIPTION_MAX_CHARS,
    )
    body_ok, body = _bounded_graph_text(
        center_data.get("description"),
        default="",
        max_chars=_RUNTIME_GRAPH_BODY_MAX_CHARS,
    )
    if not title_ok or not description_ok or not body_ok:
        return None

    raw_tags = center_data.get("tags")
    if raw_tags is None:
        raw_tags = []
    if not isinstance(raw_tags, list):
        return None
    tags: list[str] = []
    for raw_tag in raw_tags[:_RUNTIME_GRAPH_TAG_LIMIT]:
        tag_ok, tag = _bounded_graph_text(
            raw_tag,
            default="",
            max_chars=_RUNTIME_GRAPH_TAG_MAX_CHARS,
        )
        if not tag_ok:
            return None
        if tag:
            tags.append(tag)

    return {
        "slug": slug,
        "type": resolved_type,
        "path": "",
        "frontmatter": {
            "title": title,
            "type": resolved_type,
            "description": description,
            "tags": tags,
            "source": "runtime-graph",
        },
        "body": body,
    }


def _runtime_graph_entity_detail(
    slug: str,
    requested_type: str | None,
    deps: ReadOnlyApiDeps,
) -> dict[str, Any] | None:
    """Return an exact graph-backed entity when no full wiki page is installed."""
    if not is_safe_source_name(slug):
        return None
    normalized_type = (
        deps.normalize_dashboard_entity_type(requested_type) if requested_type is not None else None
    )
    if requested_type is not None and normalized_type is None:
        raise ValueError(f"unsupported entity_type: {requested_type!r}")

    candidate_types = (
        (normalized_type,) if normalized_type is not None else _RUNTIME_GRAPH_ENTITY_TYPES
    )
    matches = [
        detail
        for entity_type in candidate_types
        if (detail := _runtime_graph_entity_detail_for_type(slug, entity_type, deps)) is not None
    ]
    if len(matches) > 1:
        raise ValueError(
            f"multiple runtime graph entity types match {slug!r}; "
            "the type query parameter is required"
        )
    return matches[0] if matches else None


def handle_readonly_route(
    name: str,
    params: Mapping[str, str],
    qs: Mapping[str, str],
    deps: ReadOnlyApiDeps,
) -> ReadOnlyApiResponse | None:
    """Return the JSON response for read-only API routes, if this route is one."""
    query = dict(qs)
    if name == "api_sessions":
        return ReadOnlyApiResponse(deps.summarize_sessions())
    if name == "api_manifest":
        return ReadOnlyApiResponse(deps.read_manifest())
    if name == "api_status":
        return ReadOnlyApiResponse(deps.status_payload())
    if name == "api_kpi":
        summary = deps.kpi_summary()
        if summary is None:
            return ReadOnlyApiResponse({"total": 0, "detail": "no sidecars yet"})
        to_dict = getattr(summary, "to_dict", None)
        return ReadOnlyApiResponse(to_dict() if callable(to_dict) else summary)
    if name == "api_grades":
        summary_payload = _grade_payload_from_summary(deps.kpi_summary())
        if summary_payload is not None:
            return ReadOnlyApiResponse(summary_payload)
        return ReadOnlyApiResponse(deps.grade_distribution_payload())
    if name == "api_sidecars":
        return ReadOnlyApiResponse(deps.sidecar_page_payload(query))
    if name == "api_runtime":
        return ReadOnlyApiResponse(deps.runtime_lifecycle_summary())
    if name == "api_skillspector":
        return ReadOnlyApiResponse(deps.skillspector_audit_payload(query))
    if name == "api_config":
        return ReadOnlyApiResponse(deps.effective_config_payload())
    if name == "api_entities_search":
        try:
            limit = max(1, min(int(query.get("limit", 80)), 200))
            results = deps.search_wiki_entities(
                query.get("q", ""),
                query.get("type") or None,
                limit,
            )
        except ValueError as exc:
            return ReadOnlyApiResponse({"detail": str(exc)}, status=400)
        return ReadOnlyApiResponse({"results": results, "total": len(results)})
    if name == "api_entity":
        slug = params["slug"]
        try:
            detail = deps.wiki_entity_detail(slug, query.get("type"))
            if detail is None:
                detail = _runtime_graph_entity_detail(slug, query.get("type"), deps)
        except ValueError as exc:
            return ReadOnlyApiResponse({"detail": str(exc)}, status=400)
        if detail is None:
            return ReadOnlyApiResponse({"detail": f"no wiki entity for {slug}"}, status=404)
        return ReadOnlyApiResponse(detail)
    if name == "api_skill":
        slug = params["slug"]
        sidecar = deps.load_sidecar(slug, query.get("type"))
        if sidecar is None:
            return ReadOnlyApiResponse({"detail": f"no sidecar for {slug}"}, status=404)
        return ReadOnlyApiResponse(sidecar)
    if name == "api_graph":
        slug = params["slug"]
        requested_type = query.get("type")
        graph_entity_type = deps.normalize_dashboard_entity_type(requested_type)
        if requested_type is not None and graph_entity_type is None:
            return ReadOnlyApiResponse(
                {"detail": f"unsupported entity_type: {requested_type!r}"},
                status=400,
            )
        try:
            hops = max(1, min(int(query.get("hops", 1)), 3))
            limit = max(5, min(int(query.get("limit", 40)), 150))
        except ValueError:
            return ReadOnlyApiResponse(
                {"detail": "hops and limit must be integers"},
                status=400,
            )
        return ReadOnlyApiResponse(
            deps.graph_neighborhood(
                slug,
                hops,
                limit,
                graph_entity_type,
            ),
        )
    return None
