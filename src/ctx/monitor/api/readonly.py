"""Read-only JSON route payloads for ctx-monitor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


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
        except ValueError as exc:
            return ReadOnlyApiResponse({"detail": str(exc)}, status=400)
        if detail is None:
            return ReadOnlyApiResponse({"detail": f"no wiki entity for {slug}"}, status=404)
        return ReadOnlyApiResponse(detail)
    if name == "api_skill":
        slug = params["slug"]
        sidecar = deps.load_sidecar(slug, query.get("type"))
        if sidecar is None:
            return ReadOnlyApiResponse(None, status=404, not_found_detail=f"no sidecar for {slug}")
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
