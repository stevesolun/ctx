"""Page/API route dispatch for the ctx dashboard."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ctx.monitor import routes
from ctx.monitor.api import mutations as mutation_api
from ctx.monitor.api import readonly as readonly_api


@dataclass(frozen=True)
class RouteDispatchDeps:
    render_home: Callable[[], str]
    render_sessions_index: Callable[[], str]
    render_session_detail: Callable[[str], str]
    render_skills: Callable[[dict[str, str]], str]
    render_skillspector: Callable[[dict[str, str]], str]
    render_skill_detail: Callable[[str, str | None], str]
    render_loaded: Callable[[bool], str]
    render_logs: Callable[[], str]
    render_graph: Callable[[str | None, str | None], str]
    render_recommendations: Callable[[dict[str, str]], str]
    render_manage: Callable[[bool], str]
    render_harness_wizard: Callable[[], str]
    render_docs: Callable[[], str]
    render_config: Callable[[], str]
    render_status: Callable[[], str]
    render_wiki_index: Callable[[str | None, str], str]
    render_wiki_entity: Callable[[str, str | None, bool], str]
    render_kpi: Callable[[], str]
    render_runtime_lifecycle: Callable[[], str]
    render_events: Callable[[], str]
    readonly_api_deps: Callable[[], readonly_api.ReadOnlyApiDeps]
    mutation_api_deps: Callable[[], mutation_api.MutationApiDeps]


def handle_get_route(
    handler: Any,
    route: routes.RouteMatch,
    qs: dict[str, str],
    deps: RouteDispatchDeps,
) -> None:
    name = route.name
    params = route.params
    if name == "home":
        handler._send_html(deps.render_home())
    elif name == "sessions_index":
        handler._send_html(deps.render_sessions_index())
    elif name == "session_detail":
        handler._send_html(deps.render_session_detail(params["session_id"]))
    elif name == "skills":
        handler._send_html(deps.render_skills(qs))
    elif name == "skillspector":
        handler._send_html(deps.render_skillspector(qs))
    elif name == "skill_detail":
        handler._send_html(deps.render_skill_detail(params["slug"], qs.get("type")))
    elif name == "loaded":
        handler._send_html(deps.render_loaded(handler._mutations_enabled()))
    elif name == "logs":
        handler._send_html(deps.render_logs())
    elif name == "graph":
        handler._send_html(deps.render_graph(qs.get("slug"), qs.get("type")))
    elif name == "recommend":
        handler._send_html(deps.render_recommendations(qs))
    elif name == "manage":
        handler._send_html(deps.render_manage(handler._mutations_enabled()))
    elif name == "harness":
        handler._send_html(deps.render_harness_wizard())
    elif name == "docs":
        handler._send_html(deps.render_docs())
    elif name == "config":
        handler._send_html(deps.render_config())
    elif name == "status":
        handler._send_html(deps.render_status())
    elif name == "wiki_index":
        handler._send_html(deps.render_wiki_index(qs.get("type"), qs.get("q", "")))
    elif name == "wiki_entity":
        handler._send_html(
            deps.render_wiki_entity(
                params["slug"],
                qs.get("type"),
                handler._mutations_enabled(),
            ),
        )
    elif name == "kpi":
        handler._send_html(deps.render_kpi())
    elif name == "runtime":
        handler._send_html(deps.render_runtime_lifecycle())
    elif name == "events":
        handler._send_html(deps.render_events())
    elif name == "api_events_stream":
        handler._stream_audit_log()
    else:
        api_response = readonly_api.handle_readonly_route(
            name,
            params,
            qs,
            deps.readonly_api_deps(),
        )
        if api_response is None:
            handler._send_404(name)
        elif api_response.not_found_detail is not None:
            handler._send_404(api_response.not_found_detail)
        elif api_response.status == 200:
            handler._send_json(api_response.payload)
        else:
            handler._send_json_status(api_response.status, api_response.payload)


def handle_post_route(
    handler: Any,
    route_name: str,
    body: Mapping[str, Any],
    path: str,
    deps: RouteDispatchDeps,
) -> None:
    mutation_response = mutation_api.handle_mutation_route(
        route_name,
        body,
        deps.mutation_api_deps(),
    )
    if mutation_response is None:
        handler._send_404(path)
    else:
        handler._send_json_status(
            mutation_response.status,
            mutation_response.payload,
        )
