"""Route inventory for ctx-monitor."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, unquote


@dataclass(frozen=True)
class ParsedRequestTarget:
    path: str
    query: dict[str, str]


@dataclass(frozen=True)
class RouteMatch:
    name: str
    params: dict[str, str]


NAV_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("home", "Home", "/"),
    ("loaded", "Loaded", "/loaded"),
    ("skills", "Skills", "/skills"),
    ("skillspector", "SkillSpector", "/skillspector"),
    ("wiki", "Wiki", "/wiki"),
    ("graph", "Graph", "/graph"),
    ("manage", "Manage", "/manage"),
    ("harness", "Harness Setup", "/harness"),
    ("docs", "Docs", "/docs"),
    ("config", "Config", "/config"),
    ("status", "Status", "/status"),
    ("kpi", "KPIs", "/kpi"),
    ("runtime", "Runtime", "/runtime"),
    ("sessions", "Sessions", "/sessions"),
    ("logs", "Logs", "/logs"),
    ("events", "Live", "/events"),
)

PAGE_ROUTES: frozenset[str] = frozenset(href for _key, _label, href in NAV_ROUTES) | {
    "/catalog",
    "/catalog/",
    "/live",
}

GET_API_ROUTES: frozenset[str] = frozenset(
    {
        "/api/sessions.json",
        "/api/manifest.json",
        "/api/status.json",
        "/api/kpi.json",
        "/api/grades.json",
        "/api/sidecars.json",
        "/api/runtime.json",
        "/api/skillspector.json",
        "/api/config.json",
        "/api/entities/search.json",
        "/api/events.stream",
    }
)

GET_API_PATTERNS: tuple[str, ...] = (
    "/api/entity/<slug>.json",
    "/api/skill/<slug>.json",
    "/api/graph/<slug>.json",
)

POST_API_ROUTES: frozenset[str] = frozenset(
    {
        "/api/load",
        "/api/unload",
        "/api/config",
        "/api/entity/upsert",
        "/api/entity/delete",
    }
)


_GET_EXACT_ROUTES: dict[str, str] = {
    "/": "home",
    "/sessions": "sessions_index",
    "/skills": "skills",
    "/skillspector": "skillspector",
    "/loaded": "loaded",
    "/logs": "logs",
    "/graph": "graph",
    "/manage": "manage",
    "/harness": "harness",
    "/docs": "docs",
    "/config": "config",
    "/status": "status",
    "/catalog": "wiki_index",
    "/catalog/": "wiki_index",
    "/wiki": "wiki_index",
    "/kpi": "kpi",
    "/runtime": "runtime",
    "/events": "events",
    "/live": "events",
    "/api/sessions.json": "api_sessions",
    "/api/manifest.json": "api_manifest",
    "/api/status.json": "api_status",
    "/api/kpi.json": "api_kpi",
    "/api/grades.json": "api_grades",
    "/api/sidecars.json": "api_sidecars",
    "/api/runtime.json": "api_runtime",
    "/api/skillspector.json": "api_skillspector",
    "/api/config.json": "api_config",
    "/api/entities/search.json": "api_entities_search",
    "/api/events.stream": "api_events_stream",
}

_POST_EXACT_ROUTES: dict[str, str] = {
    "/api/load": "api_load",
    "/api/unload": "api_unload",
    "/api/config": "api_config",
    "/api/entity/upsert": "api_entity_upsert",
    "/api/entity/delete": "api_entity_delete",
}


def parse_request_target(request_path: str) -> ParsedRequestTarget:
    raw_path, _, raw_query = request_path.partition("?")
    query = {key: values[0] for key, values in parse_qs(raw_query).items() if values}
    return ParsedRequestTarget(path=raw_path, query=query)


def match_get_route(path: str) -> RouteMatch | None:
    if route_name := _GET_EXACT_ROUTES.get(path):
        return RouteMatch(route_name, {})
    if path.startswith("/session/"):
        return RouteMatch("session_detail", {"session_id": path.split("/session/", 1)[1]})
    if path.startswith("/skill/"):
        return RouteMatch("skill_detail", {"slug": path.split("/skill/", 1)[1]})
    if path.startswith("/wiki/"):
        return RouteMatch("wiki_entity", {"slug": path.split("/wiki/", 1)[1]})
    if path.startswith("/api/entity/") and path.endswith(".json"):
        return RouteMatch(
            "api_entity",
            {"slug": unquote(path[len("/api/entity/") : -len(".json")])},
        )
    if path.startswith("/api/skill/") and path.endswith(".json"):
        return RouteMatch(
            "api_skill",
            {"slug": unquote(path[len("/api/skill/") : -len(".json")])},
        )
    if path.startswith("/api/graph/") and path.endswith(".json"):
        return RouteMatch(
            "api_graph",
            {"slug": unquote(path[len("/api/graph/") : -len(".json")])},
        )
    return None


def match_post_route(path: str) -> RouteMatch | None:
    if route_name := _POST_EXACT_ROUTES.get(path):
        return RouteMatch(route_name, {})
    return None
