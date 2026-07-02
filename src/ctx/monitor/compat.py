# mypy: disable-error-code=attr-defined
"""Compatibility layer for the local ctx runtime and catalog dashboard.

``ctx-monitor serve [--port 8765]`` starts a zero-dependency threaded HTTP server
(stdlib http.server) that renders the audit log + skill-events.jsonl +
sidecars into a browser UI at http://localhost:8765/.

Routes:

    /                           Home — summary stats + session list + links
    /loaded                     Live manifest view + load/unload actions
    /sessions                   List of sessions (skills/agents/MCP activity)
    /session/<id>               Skills + agents seen in that session
    /skills                     Sidecar card grid with grade + score filters
    /skillspector               SkillSpector audit tab with graph-aware filters
    /skill/<slug>               Sidecar breakdown + timeline of audit events
    /wiki                       Wiki entity index — all pages with search
    /wiki/<slug>?type=<entity>  One wiki entity page (frontmatter + body)
    /graph                      Built-in graph explorer + popular seeds
    /graph?slug=<slug>&type=... Focus graph view on a specific entity
    /manage                     Search/edit/delete/import catalog entities
    /harness                    Manual harness setup for user-owned LLMs
    /docs                       Local docs index + public docs handoff
    /config                     Editable ctx config with defaults fallback
    /status                     Durable queue + graph/wiki artifact state
    /kpi                        Grade / lifecycle / category KPIs
    /runtime                    Generic harness validation/escalation ledger
    /logs                       Filterable tail of ctx-audit.jsonl
    /events                     Live SSE stream of new audit-log lines
    /api/sessions.json          JSON index for scripting
    /api/manifest.json          Raw ~/.claude/skill-manifest.json
    /api/status.json            Queue counts + artifact promotion metadata
    /api/runtime.json           Generic harness validation/escalation summary
    /api/skillspector.json      SkillSpector audit records + filters
    /api/skill/<slug>.json      Sidecar passthrough
    /api/graph/<slug>.json      Dashboard-shaped neighborhood; accepts type
    /api/entities/search.json   Search wiki entities across supported types
    /api/entity/<slug>.json     Wiki entity frontmatter + Markdown body
    /api/config.json            Effective/default/user config
    /api/kpi.json               DashboardSummary passthrough

Design notes:

- No Flask / Starlette / FastAPI dependency. Request handling is threaded
  so one open SSE client cannot monopolize the local dashboard. Repo-doc
  rendering uses the package's Markdown dependencies for MkDocs-like output.
- GET views read append-only files. POST mutation endpoints require
  loopback access, a per-process token, and same-origin headers.
- SSE endpoint tails ``~/.claude/ctx-audit.jsonl`` and pushes each new
  line as a server-sent event. Clients auto-reconnect.
- Security: binds to 127.0.0.1 by default. ``--host`` override requires
  an explicit flag to emphasize the local-dev-only intent.

This is a minimal dashboard. Power users should pipe the audit log
into Grafana / Loki / whatever; ``ctx-monitor`` is the zero-config
starting point.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ctx import dashboard_entities
from ctx.core import entity_types as core_entity_types
from ctx.core.quality.skillspector_service import render_scan_report
from ctx.core.quality.skillspector_service import run_skillspector_scan_text
from ctx.core.wiki import wiki_queue
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.monitor import app as _monitor_app
from ctx.monitor.layout import layout as _layout
from ctx.monitor.layout import monitor_asset_text as _monitor_asset_text
from ctx.monitor.layout import monitor_inline_script as _monitor_inline_script
from ctx.monitor.api import mutations as _mutation_api
from ctx.monitor.api import readonly as _readonly_api
from ctx.monitor import cli as _monitor_cli
from ctx.monitor import routes as _monitor_routes
from ctx.monitor import state as _monitor_state
from ctx.monitor.pages import activity as _activity_page
from ctx.monitor.pages import config as _config_page
from ctx.monitor.pages import docs as _docs_page
from ctx.monitor.pages import graph as _graph_page
from ctx.monitor.pages import harness as _harness_page
from ctx.monitor.pages import home as _home_page
from ctx.monitor.pages import loaded as _loaded_page
from ctx.monitor.pages import manage as _manage_page
from ctx.monitor.pages import ops as _ops_page
from ctx.monitor.pages import skills as _skills_page
from ctx.monitor.pages import skillspector as _skillspector_page
from ctx.monitor.pages import wiki as _wiki_page
from ctx.monitor.server import MonitorHandlerDeps as _MonitorHandlerDeps
from ctx.monitor.server import MonitorServer as _MonitorServer
from ctx.monitor.server import build_monitor_handler as _build_monitor_handler
from ctx.monitor.server import make_monitor_server as _make_server
from ctx.monitor import security as _monitor_security
from ctx.monitor.services import audit as _audit_service
from ctx.monitor.services import cache as _cache_service
from ctx.monitor.services import config as _config_service
from ctx.monitor.services import graph as _graph_service
from ctx.monitor.services import harness as _harness_service
from ctx.monitor.services import kpi as _kpi_service
from ctx.monitor.services import lifecycle as _lifecycle_service
from ctx.monitor.services import manifest as _manifest_service
from ctx.monitor.services import runtime as _runtime_service
from ctx.monitor.services import sidecars as _sidecar_service
from ctx.monitor.services import skillspector as _skillspector_service
from ctx.monitor.services import status as _status_service
from ctx.monitor.services import wiki as _wiki_service
from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import safe_atomic_write_text as _safe_atomic_write_text


_MONITOR_TOKEN = ""
_MONITOR_MUTATIONS_ENABLED = True
_WIKI_RENDER_CACHE_KEY: tuple[Any, ...] | None = None
_WIKI_RENDER_CACHE_VALUE: str | None = None
_WIKI_INDEX_LIMIT_PER_TYPE = 500
_SKILLS_PAGE_DEFAULT_LIMIT = 100
_SKILLS_PAGE_MAX_LIMIT = 500
_MAX_POST_BODY_BYTES = _monitor_security.MAX_POST_BODY_BYTES
_DASHBOARD_INDEX_MEMBER = "graphify-out/dashboard-neighborhoods.sqlite3"
_READ_TOKEN_COOKIE = _monitor_security.READ_TOKEN_COOKIE


# ─── Data sources ────────────────────────────────────────────────────────────


def _source_root() -> Path:
    source = Path(__file__).resolve()
    for parent in source.parents:
        if (parent / "pyproject.toml").is_file() or (parent / "graph").is_dir():
            return parent
    return source.parents[3]


_host_allows_mutations = _monitor_security.host_allows_mutations
_request_host_name = _monitor_security.request_host_name
_origin_host_name = _monitor_security.origin_host_name
_read_token_cookie = _monitor_security.read_token_cookie


def _state_path(factory: Callable[[Path | None], Path]) -> Callable[[], Path]:
    def path() -> Path:
        return factory(_claude_dir())

    return path


def _wiki_call(factory: Callable[..., Any]) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        return factory(_wiki_dir(), *args, **kwargs)

    return call


_claude_dir = _monitor_state.claude_dir
_audit_log_path = _state_path(_monitor_state.audit_log_path)
_events_jsonl_path = _state_path(_monitor_state.events_jsonl_path)
_runtime_lifecycle_path = _monitor_state.runtime_lifecycle_path
_manifest_path = _state_path(_monitor_state.manifest_path)
_sidecar_dir = _state_path(_monitor_state.sidecar_dir)
_wiki_dir = _state_path(_monitor_state.wiki_dir)
_user_config_path = _state_path(_monitor_state.user_config_path)


def _wiki_pack_pages() -> dict[str, str] | None:
    return _wiki_service.wiki_pack_pages(_wiki_dir())


def _load_dashboard_graph() -> Any:
    from ctx.core.graph.resolve_graph import load_graph as _lg  # type: ignore

    return _graph_service.load_dashboard_graph(_wiki_dir(), _lg)


def _dashboard_graph_source_cache_key(
    graph_path: Path,
    overlay_path: Path,
) -> tuple[Any, ...] | None:
    return _graph_service.dashboard_graph_source_cache_key(graph_path, overlay_path)


def _dashboard_file_cache_key(path: Path) -> tuple[str, float, int] | None:
    return _graph_service.dashboard_file_cache_key(path)


def _dashboard_graph_pack_cache_key(packs_dir: Path) -> tuple[tuple[str, float, int], ...]:
    return _graph_service.dashboard_graph_pack_cache_key(packs_dir)


_mcp_shard = core_entity_types.mcp_shard


_DASHBOARD_ENTITY_SOURCES: tuple[tuple[str, str, bool], ...] = (
    core_entity_types.entity_source_specs()
)
_DASHBOARD_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type for _, entity_type, _ in _DASHBOARD_ENTITY_SOURCES
)
_DEFAULT_GRAPH_FOCUS_SLUG = "github"


_normalize_dashboard_entity_type = _wiki_service.normalize_entity_type
_is_safe_slug = _wiki_service.is_safe_slug


def _audit_entity_type(row: dict) -> str | None:
    raw_meta = row.get("meta")
    meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    for raw in (
        meta.get("entity_type"),
        row.get("entity_type"),
        row.get("subject_type"),
        row.get("type"),
    ):
        normalized = _normalize_dashboard_entity_type(raw)
        if normalized:
            return normalized
    event = str(row.get("event") or "")
    prefix, _, _ = event.partition(".")
    return _normalize_dashboard_entity_type(prefix)


_wiki_entity_path = _wiki_call(_wiki_service.entity_path)
_wiki_entity_target_path = _wiki_call(_wiki_service.entity_target_path)
_iter_wiki_entity_paths = _wiki_call(_wiki_service.iter_entity_paths)
_wiki_entity_detail = _wiki_call(_wiki_service.entity_detail)
_wiki_pack_entity_from_relpath = _wiki_service.pack_entity_from_relpath
_read_wiki_entity_text = _wiki_call(_wiki_service.read_entity_text)


def _search_wiki_entities(
    query: str = "",
    entity_type: str | None = None,
    *,
    limit: int = 80,
) -> list[dict[str, Any]]:
    indexed = _search_wiki_entities_from_index(query, entity_type, limit=limit)
    if indexed is not None:
        return indexed
    return dashboard_entities.search_wiki_entities(
        query,
        entity_type,
        limit=limit,
        deps=_entity_crud_deps(),
    )


def _search_wiki_entities_from_index(
    query: str = "",
    entity_type: str | None = None,
    *,
    limit: int = 80,
) -> list[dict[str, Any]] | None:
    return _wiki_service.search_entities_from_index(
        _dashboard_graph_index_path(),
        query,
        entity_type,
        limit=limit,
        index_matches_manifest=_dashboard_index_matches_manifest,
    )


def _queue_entity_refresh(
    *,
    entity_type: str,
    slug: str,
    entity_path: Path,
    content: str,
    action: str,
) -> None:
    _wiki_service.queue_entity_refresh(
        _wiki_dir(),
        entity_type=entity_type,
        slug=slug,
        entity_path=entity_path,
        content=content,
        action=action,
    )


def _write_entity_text(path: Path, content: str) -> None:
    _safe_atomic_write_text(path, content, encoding="utf-8")


def _scan_skill_entity_content(slug: str, content: str) -> tuple[bool, str]:
    result = run_skillspector_scan_text(slug, content)
    if result.passed:
        return True, "SkillSpector: passed"
    return (
        False,
        f"SkillSpector security scan did not pass: {result.status}\n\n{render_scan_report(result)}",
    )


def _entity_crud_deps() -> dashboard_entities.EntityCrudDeps:
    return dashboard_entities.EntityCrudDeps(
        is_safe_slug=_is_safe_slug,
        normalize_entity_type=_normalize_dashboard_entity_type,
        wiki_entity_detail=_wiki_entity_detail,
        wiki_entity_target_path=_wiki_entity_target_path,
        wiki_entity_path=_wiki_entity_path,
        iter_wiki_entity_paths=_iter_wiki_entity_paths,
        read_manifest=_read_manifest,
        perform_unload=_perform_unload,
        queue_entity_refresh=lambda entity_type, slug, entity_path, content, action: (
            _queue_entity_refresh(
                entity_type=entity_type,
                slug=slug,
                entity_path=entity_path,
                content=content,
                action=action,
            )
        ),
        file_lock=file_lock,
        write_entity_text=_write_entity_text,
        parse_frontmatter=_parse_frontmatter,
        frontmatter_tags=lambda value: _frontmatter_tags(value, limit=None),
        frontmatter_text=_frontmatter_text,
        display_slug=_display_slug,
        display_label=lambda value: _display_label(value),
        entity_wiki_href=_entity_wiki_href,
        scan_skill_content=_scan_skill_entity_content,
    )


def _entity_runtime_deps() -> dashboard_entities.EntityRuntimeDeps:
    return _lifecycle_service.entity_runtime_deps(
        wiki_dir=_wiki_dir,
        claude_dir=_claude_dir,
        audit_log_path=_audit_log_path,
        manifest_path=_manifest_path,
    )


def _upsert_wiki_entity(payload: dict[str, Any]) -> tuple[bool, str]:
    return dashboard_entities.upsert_wiki_entity(payload, deps=_entity_crud_deps())


def _delete_wiki_entity(slug: str, entity_type: str) -> tuple[bool, str]:
    return dashboard_entities.delete_wiki_entity(
        slug,
        entity_type,
        deps=_entity_crud_deps(),
    )


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split frontmatter from body using the canonical wiki parser."""
    return parse_frontmatter_and_body(text)


_frontmatter_text = _wiki_service.frontmatter_text
_truncate_text = _wiki_service.truncate_text


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str).replace("</", "<\\/")


_frontmatter_tags = _wiki_service.frontmatter_tags


_WIKI_QUALITY_BLOCK_RE = re.compile(
    r"<!--\s*quality:begin\s*-->\s*(.*?)\s*<!--\s*quality:end\s*-->",
    re.IGNORECASE | re.DOTALL,
)


def _wiki_link_href(target: str) -> tuple[str, str]:
    """Return dashboard href + display label for an Obsidian-style wikilink."""
    normalized = target.strip().replace("\\", "/").removesuffix(".md")
    parts = [part for part in normalized.split("/") if part]
    entity_type = ""
    if len(parts) >= 3 and parts[0] == "entities":
        entity_type = {
            "skills": "skill",
            "agents": "agent",
            "mcp-servers": "mcp-server",
            "harnesses": "harness",
        }.get(parts[1], "")
    slug = parts[-1] if parts else normalized
    if not _is_safe_slug(slug):
        return "#", slug or target
    suffix = f"?type={quote(entity_type)}" if entity_type else ""
    return f"/wiki/{quote(slug)}{suffix}", _display_slug(slug)


def _markdown_link_href(target: str) -> str | None:
    return _wiki_page.markdown_link_href(target)


def _render_wiki_inline(text: str) -> str:
    return _wiki_page.render_wiki_inline(text, wiki_link_href=_wiki_link_href)


def _render_wiki_markdown(markdown_text: str) -> str:
    return _wiki_page.render_wiki_markdown(markdown_text, wiki_link_href=_wiki_link_href)


def _extract_embedded_quality_block(markdown_text: str) -> tuple[str, str | None]:
    matches = list(_WIKI_QUALITY_BLOCK_RE.finditer(markdown_text))
    if not matches:
        return markdown_text, None
    quality_blocks = [match.group(1).strip() for match in matches if match.group(1).strip()]
    body = _WIKI_QUALITY_BLOCK_RE.sub("\n\n", markdown_text)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    quality_markdown = "\n\n".join(quality_blocks).strip() or None
    return body, quality_markdown


def _slugish(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


_display_slug = _wiki_service.display_slug
_display_label = _wiki_service.display_label


def _strip_duplicate_wiki_heading(markdown_text: str, slug: str) -> str:
    """Drop the first H1 if it only repeats the page slug."""
    lines = markdown_text.splitlines()
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        match = re.match(r"^#\s+(.+?)\s*$", line.strip())
        if match and _slugish(match.group(1)) == _slugish(slug):
            del lines[idx]
            while idx < len(lines) and not lines[idx].strip():
                del lines[idx]
        break
    return "\n".join(lines)


_entity_wiki_href = _wiki_service.entity_wiki_href
_graph_type_from_node_id = _wiki_service.graph_type_from_node_id


def _subgraph_sidecar(slug: str, entity_type: str) -> dict[str, Any] | None:
    sidecar = _load_sidecar(slug, entity_type=entity_type)
    return sidecar if isinstance(sidecar, dict) else None


def _render_entity_subgraph_svg(
    node_by_id: dict[str, dict[str, Any]],
    edges: list[dict],
    center: str,
    sidecar_by_id: dict[str, dict[str, Any] | None],
) -> str:
    return _wiki_page.render_entity_subgraph_svg(
        node_by_id=node_by_id,
        edges=edges,
        center=center,
        sidecar_by_id=sidecar_by_id,
        graph_type_from_node_id=_graph_type_from_node_id,
        graph_slug_from_node_id=_graph_slug_from_node_id,
        display_label=_display_label,
        display_slug=_display_slug,
        entity_wiki_href=_entity_wiki_href,
        json_for_script=_json_for_script,
    )


def _render_entity_subgraph(slug: str, entity_type: str | None = None) -> str:
    return _wiki_page.render_entity_subgraph(
        slug,
        entity_type,
        graph_neighborhood=_graph_neighborhood,
        graph_type_from_node_id=_graph_type_from_node_id,
        graph_slug_from_node_id=_graph_slug_from_node_id,
        subgraph_sidecar=_subgraph_sidecar,
        display_label=_display_label,
        display_slug=_display_slug,
        entity_wiki_href=_entity_wiki_href,
        json_for_script=_json_for_script,
    )


def _render_entity_tabs(
    *,
    overview_html: str,
    subgraph_html: str,
    quality_html: str,
) -> str:
    return _wiki_page.render_entity_tabs(
        overview_html=overview_html,
        subgraph_html=subgraph_html,
        quality_html=quality_html,
    )


def _render_quality_drilldown(
    sidecar: dict | None,
    embedded_quality_markdown: str | None = None,
) -> str:
    return _wiki_page.render_quality_drilldown(
        sidecar,
        embedded_quality_markdown,
        wiki_link_href=_wiki_link_href,
        truncate_text=_truncate_text,
    )


def _save_manifest(manifest: dict) -> None:
    _manifest_service.save_manifest(_manifest_path(), manifest)


def _read_skill_manifest_only() -> dict:
    return _manifest_service.read_skill_manifest_only(_manifest_path())


def _remove_loaded_manifest_entry(slug: str, entity_type: str) -> list[dict]:
    return _manifest_service.remove_loaded_manifest_entry(
        _manifest_path(),
        slug,
        entity_type,
    )


def _log_dashboard_entity_event(
    entity_type: str,
    action: str,
    slug: str,
) -> None:
    _audit_service.log_dashboard_entity_event(
        _audit_log_path(),
        entity_type,
        action,
        slug,
    )


def _read_manifest() -> dict:
    return _manifest_service.read_manifest(_manifest_path(), _claude_dir())


def _read_harness_install_rows() -> list[dict]:
    return _manifest_service.read_harness_install_rows(_claude_dir())


def _queue_status() -> dict[str, Any]:
    return _status_service.queue_status(_wiki_dir())


def _repo_graph_dir() -> Path:
    return _source_root() / "graph"


def _skillspector_audit_path() -> Path:
    return _skillspector_service.audit_path(_wiki_dir(), _repo_graph_dir())


def _skillspector_communities_path() -> Path | None:
    return _skillspector_service.communities_path(_wiki_dir(), _repo_graph_dir())


def _skillspector_index_path() -> Path | None:
    return _skillspector_service.index_path(
        _dashboard_graph_index_path(),
        _dashboard_index_matches_manifest,
    )


def _skillspector_limit(qs: dict[str, str]) -> int:
    return _skillspector_service.limit(qs)


def _skillspector_audit_payload(qs: dict[str, str] | None = None) -> dict[str, Any]:
    return _skillspector_service.audit_payload(
        _wiki_dir(),
        _repo_graph_dir(),
        _dashboard_graph_index_path(),
        _dashboard_index_matches_manifest,
        qs,
    )


def _artifact_status() -> dict[str, Any]:
    return _status_service.artifact_status(
        wiki_dir=_wiki_dir(),
        claude_dir=_claude_dir(),
        repo_graph_dir=_repo_graph_dir(),
    )


def _status_payload() -> dict[str, Any]:
    return _status_service.status_payload(
        wiki_dir=_wiki_dir(),
        claude_dir=_claude_dir(),
        repo_graph_dir=_repo_graph_dir(),
    )


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    return _runtime_service.read_jsonl(path, limit=limit)


def _runtime_lifecycle_summary(limit: int = 200) -> dict[str, Any]:
    return _runtime_service.lifecycle_summary(_runtime_lifecycle_path(), limit=limit)


def _sidecar_entity_type(sidecar: dict, fallback: str = "skill") -> str:
    return _sidecar_service.sidecar_entity_type(sidecar, fallback)


def _sidecar_fallback_type(path: Path) -> str:
    return _sidecar_service.sidecar_fallback_type(path)


def _read_sidecar_file(path: Path) -> dict | None:
    return _sidecar_service.read_sidecar_file(path)


def _load_sidecar(slug: str, entity_type: str | None = None) -> dict | None:
    return _sidecar_service.load_sidecar(_sidecar_dir(), slug, entity_type=entity_type)


def _sidecar_files() -> list[Path]:
    return _sidecar_service.sidecar_files(_sidecar_dir())


def _sidecar_index_cache_key() -> tuple[tuple[Path, float, int], ...]:
    return _sidecar_service.sidecar_index_cache_key(_sidecar_dir())


def _sidecar_index() -> dict[tuple[str, str], dict]:
    return _sidecar_service.sidecar_index(_sidecar_dir())


def _all_sidecars() -> list[dict]:
    return _sidecar_service.all_sidecars(_sidecar_dir())


def _skills_page_int(
    value: str | None,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    return _sidecar_service.skills_page_int(
        value,
        default=default,
        minimum=minimum,
        maximum=maximum,
    )


def _skills_query_values(raw: str | None, allowed: set[str]) -> set[str]:
    return _sidecar_service.skills_query_values(raw, allowed)


def _sidecar_sort_key(sidecar: dict) -> tuple[str, float, str]:
    return _sidecar_service.sidecar_sort_key(sidecar)


def _sidecar_card_payload(sidecar: dict) -> dict[str, Any]:
    return _sidecar_service.sidecar_card_payload(sidecar)


def _sidecar_filter_signature(files: list[Path]) -> tuple[Any, ...]:
    return _sidecar_service.sidecar_filter_signature(_sidecar_dir(), files)


def _sidecar_candidate_files(
    files: list[Path],
    *,
    q: str,
    types: set[str],
) -> list[Path]:
    return _sidecar_service.sidecar_candidate_files(files, q=q, types=types)


def _filtered_sidecar_records(
    files: list[Path],
    *,
    q: str,
    types: set[str],
    grades: set[str],
    hide_floor: bool,
) -> list[dict[str, Any]]:
    return _sidecar_service.filtered_sidecar_records(
        _sidecar_dir(),
        files,
        q=q,
        types=types,
        grades=grades,
        hide_floor=hide_floor,
    )


def _sidecar_matches_filters(
    sidecar: dict,
    *,
    q: str,
    types: set[str],
    grades: set[str],
    hide_floor: bool,
) -> bool:
    return _sidecar_service.sidecar_matches_filters(
        sidecar,
        q=q,
        types=types,
        grades=grades,
        hide_floor=hide_floor,
    )


def _sidecar_page_payload(qs: dict[str, str] | None = None) -> dict[str, Any]:
    return _sidecar_service.sidecar_page_payload(
        _sidecar_dir(),
        qs,
        entity_types=_DASHBOARD_ENTITY_TYPES,
        default_limit=_SKILLS_PAGE_DEFAULT_LIMIT,
        max_limit=_SKILLS_PAGE_MAX_LIMIT,
    )


# ─── Aggregations ────────────────────────────────────────────────────────────


def _summarize_sessions() -> list[dict]:
    return _runtime_service.summarize_sessions(
        _read_jsonl(_audit_log_path()),
        _read_jsonl(_events_jsonl_path()),
        audit_entity_type=_audit_entity_type,
    )


def _grade_distribution() -> dict[str, int]:
    return _sidecar_service.grade_distribution(_sidecar_dir())


def _grade_distribution_payload() -> dict[str, Any]:
    return _sidecar_service.grade_distribution_payload(_sidecar_dir())


def _session_detail(session_id: str) -> dict:
    return _runtime_service.session_detail(
        session_id,
        _read_jsonl(_audit_log_path()),
        _read_jsonl(_events_jsonl_path()),
    )


# ─── HTML rendering ──────────────────────────────────────────────────────────


# ─── Graph neighborhood (for /graph) ────────────────────────────────────────


def _graph_slug_from_node_id(node_id: str) -> str:
    return _graph_service.graph_slug_from_node_id(node_id)


def _resolve_graph_center(
    G: Any,
    slug: str,
    entity_type: str | None,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    return _graph_service.resolve_graph_center(G, slug, entity_type)


def _unit_score(value: Any) -> float | None:
    return _graph_service.unit_score(value)


def _dashboard_score_payload(field: str, value: Any) -> dict[str, float | None]:
    return _graph_service.dashboard_score_payload(field, value)


def _format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _load_direct_sidecar(slug: str, entity_type: str | None = None) -> dict | None:
    return _sidecar_service.load_direct_sidecar(
        _sidecar_dir(),
        slug,
        entity_type=entity_type,
    )


def _sidecar_score_inputs(slug: str, entity_type: str) -> tuple[float | None, float | None]:
    return _sidecar_service.sidecar_score_inputs(
        _sidecar_dir(),
        slug,
        entity_type,
        unit_score=_unit_score,
    )


def _graph_node_size(
    nid: str,
    data: dict[str, Any],
    *,
    entity_type: str,
    degree: int,
    max_degree: int,
) -> dict[str, Any]:
    return _graph_service.graph_node_size(
        nid,
        data,
        entity_type=entity_type,
        degree=degree,
        max_degree=max_degree,
        sidecar_score_inputs=_sidecar_score_inputs,
    )


def _dashboard_graph_index_path() -> Path:
    return _graph_service.dashboard_graph_index_path(_wiki_dir())


def _dashboard_graph_manifest_export_id() -> str | None:
    return _graph_service.dashboard_graph_manifest_export_id(_wiki_dir())


def _dashboard_index_meta(index_path: Path) -> dict[str, Any] | None:
    return _graph_service.dashboard_index_meta(index_path)


def _dashboard_index_matches_manifest(index_path: Path) -> bool:
    return _graph_service.dashboard_index_matches_manifest(index_path, _wiki_dir())


def _dashboard_graph_has_runtime_overlays() -> bool:
    return _graph_service.dashboard_graph_has_runtime_overlays(_wiki_dir())


def _overlay_index_coverage_key(index_path: Path, overlay: Path) -> tuple[Any, ...] | None:
    return _graph_service.dashboard_overlay_index_coverage_key(
        index_path,
        overlay,
        _dashboard_graph_manifest_export_id(),
    )


def _active_dashboard_overlay_records(overlay: Path) -> list[dict[str, Any]] | None:
    return _graph_service.active_dashboard_overlay_records(overlay)


def _dashboard_overlay_matches_known_release(overlay: Path) -> bool:
    return _graph_service.dashboard_overlay_matches_known_release(overlay)


def _dashboard_index_uncovered_overlay_nodes(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    require_edges: bool,
) -> set[str] | None:
    return _graph_service.dashboard_index_uncovered_overlay_nodes(
        conn,
        records,
        require_edges=require_edges,
    )


def _dashboard_uncovered_runtime_overlay_nodes(index_path: Path) -> set[str] | None:
    return _graph_service.dashboard_uncovered_runtime_overlay_nodes_for_wiki(
        index_path,
        _wiki_dir(),
    )


def _dashboard_index_covers_runtime_overlays(index_path: Path) -> bool:
    return _graph_service.dashboard_index_covers_runtime_overlays_for_wiki(
        index_path,
        _wiki_dir(),
    )


def _dashboard_graph_index_archives() -> list[Path]:
    module_root = _source_root()
    return _graph_service.dashboard_graph_index_archives(module_root)


def _packaged_graph_export_id() -> str | None:
    module_root = _source_root()
    return _graph_service.packaged_graph_export_id(module_root)


def _archive_graph_export_id(archive: Path) -> str | None:
    return _graph_service.archive_graph_export_id(archive)


def _ensure_dashboard_graph_index() -> Path | None:
    return _graph_service.ensure_dashboard_graph_index(
        target=_dashboard_graph_index_path(),
        manifest_export_id=_dashboard_graph_manifest_export_id,
        packaged_export_id=_packaged_graph_export_id,
        archives=_dashboard_graph_index_archives,
        archive_export_id=_archive_graph_export_id,
        index_matches_manifest=_dashboard_index_matches_manifest,
        index_member=_DASHBOARD_INDEX_MEMBER,
    )


def _index_node_size(
    *,
    slug: str,
    entity_type: str,
    quality: Any,
    usage: Any,
    degree: int,
    max_degree: int,
) -> dict[str, Any]:
    return _graph_service.index_node_size(
        slug=slug,
        entity_type=entity_type,
        quality=quality,
        usage=usage,
        degree=degree,
        max_degree=max_degree,
        sidecar_score_inputs=_sidecar_score_inputs,
    )


def _resolve_index_center(
    conn: sqlite3.Connection,
    raw_query: str,
    entity_type: str | None,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    return _graph_service.resolve_index_center(conn, raw_query, entity_type)


def _graph_neighborhood(
    slug: str,
    hops: int = 1,
    limit: int = 40,
    entity_type: str | None = None,
) -> dict:
    return _graph_service.graph_neighborhood_for_monitor(
        slug,
        hops=hops,
        limit=limit,
        entity_type=entity_type,
        wiki_dir=_wiki_dir(),
        index_path=_dashboard_graph_index_path,
        ensure_index=_ensure_dashboard_graph_index,
        load_graph=_load_dashboard_graph,
        sidecar_score_inputs=_sidecar_score_inputs,
    )


def _graph_stats() -> dict:
    return _graph_service.dashboard_graph_stats(
        _wiki_dir(),
        ensure_index=_ensure_dashboard_graph_index,
        load_graph=_load_dashboard_graph,
    )


def _wiki_stats_from_dashboard_index() -> dict[str, int | bool] | None:
    return _wiki_service.wiki_stats_from_dashboard_index(
        _dashboard_graph_index_path(),
        index_matches_manifest=_dashboard_index_matches_manifest,
    )


def _wiki_stats() -> dict:
    graph_stats = _graph_stats()
    return _wiki_service.wiki_stats(
        _wiki_dir(),
        _dashboard_graph_index_path(),
        index_matches_manifest=_dashboard_index_matches_manifest,
        graph_node_total=int(graph_stats.get("nodes") or 0),
    )


def _count_audit_lines(path: Path) -> int:
    return sum(1 for _ in path.open(encoding="utf-8")) if path.exists() else 0


def _render_home() -> str:
    audit_path = _audit_log_path()
    return _home_page.render_home(
        manifest=_read_manifest(),
        sessions=_summarize_sessions(),
        wiki_stats=_wiki_stats(),
        graph_stats=_graph_stats(),
        runtime_summary=_runtime_lifecycle_summary(),
        audit_lines=_count_audit_lines(audit_path),
        recent_audit=_read_jsonl(audit_path, limit=10),
        layout=_layout,
        format_count=_format_count,
    )


def _render_sessions_index() -> str:
    return _activity_page.render_sessions_index(
        layout=_layout,
        summarize_sessions=_summarize_sessions,
    )


def _render_session_detail(session_id: str) -> str:
    return _activity_page.render_session_detail(
        session_id,
        layout=_layout,
        session_detail=_session_detail,
    )


def _render_skills(qs: dict[str, str] | None = None) -> str:
    return _skills_page.render_skills(
        payload=_sidecar_page_payload(qs),
        query_params=qs,
        entity_types=_DASHBOARD_ENTITY_TYPES,
        layout=_layout,
        sidecar_entity_type=_sidecar_entity_type,
    )


def _render_skillspector(qs: dict[str, str] | None = None) -> str:
    return _skillspector_page.render_skillspector(
        _skillspector_audit_payload(qs),
        layout=_layout,
    )


def _render_skill_detail(slug: str, entity_type: str | None = None) -> str:
    sidecar = _load_sidecar(slug, entity_type=entity_type)
    requested_type = _normalize_dashboard_entity_type(entity_type)
    if sidecar is not None:
        requested_type = requested_type or _sidecar_entity_type(sidecar)
    audit = [
        record
        for record in _read_jsonl(_audit_log_path())
        if record.get("subject") == slug and _audit_entity_type(record) == requested_type
    ]
    return _skillspector_page.render_skill_detail(
        slug,
        sidecar=sidecar,
        audit=audit,
        layout=_layout,
    )


def _top_degree_seeds_from_index(limit: int = 18) -> list[dict]:
    index_path = _dashboard_graph_index_path()
    if _dashboard_graph_has_runtime_overlays() and not _dashboard_index_covers_runtime_overlays(
        index_path
    ):
        return []
    ensured_index_path = _ensure_dashboard_graph_index()
    if ensured_index_path is None or not ensured_index_path.is_file():
        return []
    return _graph_service.top_degree_seeds_from_index(ensured_index_path, limit)


def _top_degree_seeds(limit: int = 18, *, allow_load: bool = True) -> list[dict]:
    """Pick high-degree nodes from the graph as seed suggestions.

    Used by ``/graph`` landing page so the first-time visitor has
    something to click. Falls back to empty on any graph-load failure.
    """
    try:
        G = _load_dashboard_graph() if allow_load else _graph_service.cached_dashboard_graph()
    except Exception:  # noqa: BLE001
        return []
    if G is None:
        return _top_degree_seeds_from_index(limit)
    return _graph_service.top_degree_seeds_from_graph(G, limit)


def _read_default_config_raw() -> dict[str, Any]:
    return _config_service.read_default_config_raw()


def _read_user_config_raw() -> dict[str, Any]:
    return _config_service.read_user_config_raw(_user_config_path())


def _deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> None:
    _config_service.deep_merge_config(base, override)


def _config_value(raw: dict[str, Any], path: str, default: Any = None) -> Any:
    return _config_service.config_value(raw, path, default)


def _set_config_value(raw: dict[str, Any], path: str, value: Any) -> None:
    _config_service.set_config_value(raw, path, value)


def _delete_config_value(raw: dict[str, Any], path: str) -> None:
    _config_service.delete_config_value(raw, path)


def _config_field_specs() -> tuple[dict[str, Any], ...]:
    return _config_service.config_field_specs()


_CONFIG_REMOVE = _config_service.CONFIG_REMOVE


def _coerce_config_value(spec: dict[str, Any], raw_value: Any) -> Any:
    return _config_service.coerce_config_value(spec, raw_value)


def _effective_config_payload() -> dict[str, Any]:
    return _config_service.effective_config_payload(_user_config_path())


def _save_config_updates(updates: dict[str, Any]) -> dict[str, Any]:
    return _config_service.save_config_updates(updates, _user_config_path())


def _render_config() -> str:
    return _config_page.render_config(
        payload=_effective_config_payload(),
        specs=_config_field_specs(),
        monitor_token=_MONITOR_TOKEN or "",
        layout=_layout,
        config_value=_config_value,
        config_remove=_CONFIG_REMOVE,
    )


def _graph_match_default_min_percent() -> int:
    """Return the dashboard's default lower match bound from graph config."""
    try:
        from ctx_config import cfg  # type: ignore

        value = float(getattr(cfg, "graph_edge_min_weight", 0.0))
    except Exception:
        value = 0.0
    if not math.isfinite(value):
        value = 0.0
    return max(0, min(100, int(round(value * 100))))


def _render_graph(focus: str | None = None, focus_type: str | None = None) -> str:
    """Interactive graph view backed by a dependency-free SVG renderer."""
    return _graph_page.render_graph(
        focus=focus,
        focus_type=focus_type,
        graph_stats=_graph_stats,
        top_degree_seeds=_top_degree_seeds,
        default_focus_slug=_DEFAULT_GRAPH_FOCUS_SLUG,
        json_for_script=_json_for_script,
        graph_match_default_min_percent=_graph_match_default_min_percent,
        format_count=_format_count,
        layout=_layout,
    )


def _runtime_graph_center_data(graph: dict) -> dict[str, Any] | None:
    return _wiki_page.runtime_graph_center_data(graph)


def _runtime_graph_metric_row(label: str, value: object) -> str:
    return _wiki_page.runtime_graph_metric_row(label, value)


def _render_runtime_entity_action(
    slug: str,
    entity_type: str,
    *,
    mutations_enabled: bool,
) -> str:
    return _wiki_page.render_runtime_entity_action(
        slug,
        entity_type,
        mutations_enabled=mutations_enabled,
    )


def _render_runtime_entity_load_script(
    slug: str,
    entity_type: str,
    *,
    mutations_enabled: bool,
) -> str:
    return _wiki_page.render_runtime_entity_load_script(
        slug,
        entity_type,
        mutations_enabled=mutations_enabled,
        monitor_token=_MONITOR_TOKEN,
    )


def _render_runtime_graph_entity(
    slug: str,
    entity_type: str | None = None,
    *,
    mutations_enabled: bool | None = None,
) -> str | None:
    return _wiki_page.render_runtime_graph_entity(
        slug,
        entity_type=entity_type,
        mutations_enabled=mutations_enabled,
        monitor_mutations_enabled=_MONITOR_MUTATIONS_ENABLED,
        monitor_token=_MONITOR_TOKEN,
        normalize_dashboard_entity_type=_normalize_dashboard_entity_type,
        graph_neighborhood=_graph_neighborhood,
        graph_slug_from_node_id=_graph_slug_from_node_id,
        graph_type_from_node_id=_graph_type_from_node_id,
        display_label=_display_label,
        display_slug=_display_slug,
        load_sidecar=_load_sidecar,
        render_quality_drilldown=_render_quality_drilldown,
        render_entity_subgraph=_render_entity_subgraph,
        render_entity_tabs=_render_entity_tabs,
        layout=_layout,
    )


def _render_wiki_entity(
    slug: str,
    entity_type: str | None = None,
    *,
    mutations_enabled: bool | None = None,
) -> str:
    return _wiki_page.render_wiki_entity(
        slug,
        entity_type=entity_type,
        mutations_enabled=mutations_enabled,
        entity_path=_wiki_entity_path,
        read_entity_text=_read_wiki_entity_text,
        parse_frontmatter=_parse_frontmatter,
        load_sidecar=_load_sidecar,
        render_runtime_graph_entity=_render_runtime_graph_entity,
        dashboard_entity_types=_DASHBOARD_ENTITY_TYPES,
        display_slug=_display_slug,
        frontmatter_text=_frontmatter_text,
        truncate_text=_truncate_text,
        extract_embedded_quality_block=_extract_embedded_quality_block,
        strip_duplicate_wiki_heading=_strip_duplicate_wiki_heading,
        render_entity_subgraph=_render_entity_subgraph,
        render_entity_tabs=_render_entity_tabs,
        render_quality_drilldown=_render_quality_drilldown,
        render_wiki_markdown=_render_wiki_markdown,
        layout=_layout,
    )


def _wiki_index_entries(
    limit_per_type: int | None = _WIKI_INDEX_LIMIT_PER_TYPE,
) -> list[dict]:
    """List every wiki entity page under ~/.claude/skill-wiki/entities/.

    Returns ``{slug, type, tags, description}`` rows. The full skill inventory
    is too large to render as one HTML page, so the dashboard samples
    a bounded number of pages per entity type.
    """
    return _wiki_service.index_entries(
        _wiki_dir(),
        _dashboard_graph_index_path(),
        limit_per_type=limit_per_type,
        index_matches_manifest=_dashboard_index_matches_manifest,
    )


def _wiki_render_cache_key(
    selected_type: str | None,
    query: str,
) -> tuple[Any, ...] | None:
    return _wiki_service.wiki_render_cache_key(
        _dashboard_graph_index_path(),
        selected_type,
        query,
        source_path=Path(__file__),
        css_text=_monitor_asset_text("monitor.css"),
        manifest_export_id=_dashboard_graph_manifest_export_id() or "",
        index_matches_manifest=_dashboard_index_matches_manifest,
    )


def _wiki_render_disk_cache_path() -> Path:
    return _wiki_service.wiki_render_disk_cache_path(_claude_dir())


def _wiki_render_memory_cache_get(cache_key: tuple[Any, ...]) -> str | None:
    if _WIKI_RENDER_CACHE_KEY == cache_key and _WIKI_RENDER_CACHE_VALUE is not None:
        return _WIKI_RENDER_CACHE_VALUE
    return None


def _wiki_render_memory_cache_set(cache_key: tuple[Any, ...], html_out: str) -> None:
    global _WIKI_RENDER_CACHE_KEY, _WIKI_RENDER_CACHE_VALUE
    _WIKI_RENDER_CACHE_KEY = cache_key
    _WIKI_RENDER_CACHE_VALUE = html_out


def _render_wiki_index(entity_type: str | None = None, query: str = "") -> str:
    return _wiki_page.render_wiki_index(
        entity_type,
        query,
        normalize_dashboard_entity_type=_normalize_dashboard_entity_type,
        wiki_render_cache_key=_wiki_render_cache_key,
        read_memory_cache=_wiki_render_memory_cache_get,
        write_memory_cache=_wiki_render_memory_cache_set,
        disk_cache_token=_cache_service.disk_cache_token,
        read_html_disk_cache=_cache_service.read_html_disk_cache,
        write_html_disk_cache=_cache_service.write_html_disk_cache,
        wiki_render_disk_cache_path=_wiki_render_disk_cache_path,
        wiki_index_entries=_wiki_index_entries,
        search_wiki_entities=_search_wiki_entities,
        wiki_stats=_wiki_stats,
        load_sidecar=_load_sidecar,
        dashboard_entity_types=_DASHBOARD_ENTITY_TYPES,
        layout=_layout,
    )


def _docs_roots() -> list[Path]:
    return _docs_page.docs_roots(Path.cwd(), _source_root())


def _docs_render_disk_cache_path() -> Path:
    return _docs_page.docs_render_disk_cache_path(_claude_dir())


def _docs_index_entries() -> list[dict[str, Any]]:
    return _docs_page.docs_index_entries(_docs_roots())


def _docs_tabs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _docs_page.docs_tabs(entries, _docs_roots())


def _render_docs_markdown(markdown_text: str, page_anchor: str) -> str:
    return _docs_page.render_docs_markdown(
        markdown_text,
        page_anchor,
        fallback_renderer=_render_wiki_markdown,
    )


def _render_docs() -> str:
    return _docs_page.render_docs(
        roots=_docs_roots(),
        layout=_layout,
        asset_text=_monitor_asset_text,
        inline_script=_monitor_inline_script,
        cache_path=_docs_render_disk_cache_path(),
        fallback_markdown=_render_wiki_markdown,
        cache_salt_paths=(Path(__file__),),
        cache_extra=(id(_docs_index_entries), id(_docs_tabs)),
        index_entries=_docs_index_entries,
        tabs_for_entries=_docs_tabs,
        render_markdown_func=_render_docs_markdown,
    )


def _render_manage(mutations_enabled: bool | None = None) -> str:
    """Render catalog management for wiki entities and graph refresh queueing."""
    if mutations_enabled is None:
        mutations_enabled = _MONITOR_MUTATIONS_ENABLED
    token = _MONITOR_TOKEN if mutations_enabled else ""
    return _manage_page.render_manage(
        mutations_enabled=mutations_enabled,
        token=token,
        initial_results_json=_json_for_script(_search_wiki_entities(limit=40)),
        entity_types=_DASHBOARD_ENTITY_TYPES,
        inline_script=_monitor_inline_script,
        layout=_layout,
    )


def _harness_wizard_entries(limit: int = 24) -> list[dict[str, Any]]:
    return _harness_service.harness_wizard_entries(
        _wiki_dir(),
        _sidecar_dir(),
        limit=limit,
    )


def _harness_wizard_sidecar(slug: str) -> dict[str, Any] | None:
    return _harness_service.harness_wizard_sidecar(_sidecar_dir(), slug)


def _render_harness_wizard() -> str:
    """Manual harness interview for users who bring their own LLM."""
    return _harness_page.render_harness_wizard(
        harnesses=_harness_wizard_entries(),
        layout=_layout,
    )


def _kpi_summary():
    return _kpi_service.kpi_summary(_sidecar_dir())


def _render_kpi() -> str:
    return _ops_page.render_kpi(
        _kpi_summary(),
        layout=_layout,
        normalize_entity_type=_normalize_dashboard_entity_type,
    )


def _render_status() -> str:
    return _ops_page.render_status(
        _status_payload(),
        layout=_layout,
        queue_status_names=(
            wiki_queue.STATUS_PENDING,
            wiki_queue.STATUS_RUNNING,
            wiki_queue.STATUS_SUCCEEDED,
            wiki_queue.STATUS_FAILED,
            wiki_queue.STATUS_CANCELLED,
        ),
    )


def _render_events() -> str:
    return _activity_page.render_events(
        layout=_layout,
        read_jsonl=_read_jsonl,
        audit_log_path=_audit_log_path,
    )


def _render_loaded(mutations_enabled: bool | None = None) -> str:
    if mutations_enabled is None:
        mutations_enabled = _MONITOR_MUTATIONS_ENABLED
    return _loaded_page.render_loaded(
        _read_manifest(),
        mutations_enabled=mutations_enabled,
        monitor_token=_MONITOR_TOKEN,
        layout=_layout,
    )


def _render_runtime_lifecycle() -> str:
    return _activity_page.render_runtime_lifecycle(
        layout=_layout,
        runtime_lifecycle_summary=_runtime_lifecycle_summary,
    )


def _render_logs() -> str:
    return _activity_page.render_logs(
        layout=_layout,
        read_jsonl=_read_jsonl,
        audit_log_path=_audit_log_path,
    )


# ─── API adapters and mutation endpoints ─────────────────────────────────────


def _readonly_api_deps() -> _readonly_api.ReadOnlyApiDeps:
    return _readonly_api.ReadOnlyApiDeps(
        summarize_sessions=_summarize_sessions,
        read_manifest=_read_manifest,
        status_payload=_status_payload,
        kpi_summary=_kpi_summary,
        grade_distribution_payload=_grade_distribution_payload,
        sidecar_page_payload=_sidecar_page_payload,
        runtime_lifecycle_summary=_runtime_lifecycle_summary,
        skillspector_audit_payload=_skillspector_audit_payload,
        effective_config_payload=_effective_config_payload,
        search_wiki_entities=lambda query, entity_type, limit: _search_wiki_entities(
            query,
            entity_type,
            limit=limit,
        ),
        wiki_entity_detail=_wiki_entity_detail,
        load_sidecar=lambda slug, entity_type: _load_sidecar(
            slug,
            entity_type=entity_type,
        ),
        graph_neighborhood=lambda slug, hops, limit, entity_type: _graph_neighborhood(
            slug,
            hops=hops,
            limit=limit,
            entity_type=entity_type,
        ),
        normalize_dashboard_entity_type=_normalize_dashboard_entity_type,
    )


def _mutation_api_deps() -> _mutation_api.MutationApiDeps:
    return _mutation_api.MutationApiDeps(
        perform_load=lambda slug, entity_type, kwargs: _perform_load(
            slug,
            entity_type=entity_type,
            **kwargs,
        ),
        perform_unload=lambda slug, entity_type: _perform_unload(
            slug,
            entity_type=entity_type,
        ),
        save_config_updates=_save_config_updates,
        upsert_wiki_entity=_upsert_wiki_entity,
        delete_wiki_entity=_delete_wiki_entity,
    )


def _route_dispatch_deps() -> _monitor_app.RouteDispatchDeps:
    return _monitor_app.RouteDispatchDeps(
        render_home=_render_home,
        render_sessions_index=_render_sessions_index,
        render_session_detail=_render_session_detail,
        render_skills=_render_skills,
        render_skillspector=_render_skillspector,
        render_skill_detail=_render_skill_detail,
        render_loaded=_render_loaded,
        render_logs=_render_logs,
        render_graph=_render_graph,
        render_manage=_render_manage,
        render_harness_wizard=_render_harness_wizard,
        render_docs=_render_docs,
        render_config=_render_config,
        render_status=_render_status,
        render_wiki_index=_render_wiki_index,
        render_wiki_entity=lambda slug, entity_type, mutations_enabled: _render_wiki_entity(
            slug,
            entity_type,
            mutations_enabled=mutations_enabled,
        ),
        render_kpi=_render_kpi,
        render_runtime_lifecycle=_render_runtime_lifecycle,
        render_events=_render_events,
        readonly_api_deps=_readonly_api_deps,
        mutation_api_deps=_mutation_api_deps,
    )


def _perform_load(
    slug: str,
    entity_type: str = "skill",
    *,
    command: str | None = None,
    json_config: str | None = None,
) -> tuple[bool, str]:
    return _lifecycle_service.perform_load(
        slug,
        entity_type,
        command=command,
        json_config=json_config,
        wiki_dir=_wiki_dir,
        claude_dir=_claude_dir,
        audit_log_path=_audit_log_path,
        manifest_path=_manifest_path,
    )


def _perform_unload(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
    return _lifecycle_service.perform_unload(
        slug,
        entity_type,
        wiki_dir=_wiki_dir,
        claude_dir=_claude_dir,
        audit_log_path=_audit_log_path,
        manifest_path=_manifest_path,
    )


# ─── HTTP handler ────────────────────────────────────────────────────────────


def _handle_monitor_get_route(
    handler: Any,
    route: _monitor_routes.RouteMatch,
    qs: dict[str, str],
) -> None:
    _monitor_app.handle_get_route(handler, route, qs, _route_dispatch_deps())


def _handle_monitor_post_route(
    handler: Any,
    route_name: str,
    body: Mapping[str, Any],
    path: str,
) -> None:
    _monitor_app.handle_post_route(
        handler,
        route_name,
        body,
        path,
        _route_dispatch_deps(),
    )


def _monitor_handler_deps() -> _MonitorHandlerDeps:
    return _MonitorHandlerDeps(
        monitor_token=lambda: _MONITOR_TOKEN,
        mutations_enabled_default=lambda: _MONITOR_MUTATIONS_ENABLED,
        host_allows_mutations=_host_allows_mutations,
        request_host_name=_request_host_name,
        origin_host_name=_origin_host_name,
        read_token_cookie=_read_token_cookie,
        read_token_cookie_name=_READ_TOKEN_COOKIE,
        max_post_body_bytes=_MAX_POST_BODY_BYTES,
        audit_log_path=_audit_log_path,
        handle_get_route=_handle_monitor_get_route,
        handle_post_route=_handle_monitor_post_route,
    )


_MonitorHandler = _build_monitor_handler(_monitor_handler_deps())


def _make_monitor_server(host: str, port: int) -> _MonitorServer:
    global _MONITOR_MUTATIONS_ENABLED
    _MONITOR_MUTATIONS_ENABLED = _host_allows_mutations(host)
    return _make_server(
        host,
        port,
        _MonitorHandler,
        mutations_enabled=_MONITOR_MUTATIONS_ENABLED,
    )


def _set_monitor_token(token: str) -> None:
    global _MONITOR_TOKEN
    _MONITOR_TOKEN = token


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    return _monitor_cli.serve(
        host=host,
        port=port,
        make_server=_make_monitor_server,
        set_monitor_token=_set_monitor_token,
        display_host=_monitor_display_host,
    )


def _monitor_display_host(host: str) -> str:
    return _monitor_cli.monitor_display_host(host)


def main(argv: list[str] | None = None) -> int:
    return _monitor_cli.main(argv, serve_func=serve)


if __name__ == "__main__":
    sys.exit(main())
