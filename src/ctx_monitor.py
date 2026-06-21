# mypy: disable-error-code=attr-defined
"""ctx_monitor.py -- Local HTTP dashboard for ctx runtime and catalog activity.

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

import argparse
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import secrets
import sqlite3
import socket
import sys
import tarfile
import time
import zlib
from collections import defaultdict, deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from ctx import dashboard_entities, dashboard_graph
from ctx.core import entity_types as core_entity_types
from ctx.core.wiki import wiki_queue
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.monitor.layout import layout as _layout
from ctx.monitor.layout import monitor_asset_text as _monitor_asset_text
from ctx.monitor.layout import monitor_inline_script as _monitor_inline_script
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
from ctx.monitor.server import MonitorServer as _MonitorServer
from ctx.monitor.server import make_monitor_server as _make_server
from ctx.monitor.server import server_shutdown_requested as _server_shutdown_requested
from ctx.monitor.services import config as _config_service
from ctx.monitor.services import graph as _graph_service
from ctx.monitor.services import sidecars as _sidecar_service
from ctx.monitor.services import skillspector as _skillspector_service
from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text
from ctx.utils._fs_utils import safe_atomic_write_text as _safe_atomic_write_text
from ctx.utils._safe_name import is_safe_source_name


_MONITOR_TOKEN = ""
_MONITOR_MUTATIONS_ENABLED = True
_PACKAGED_GRAPH_EXPORT_ID_CACHE: str | None | bool = None
_OVERLAY_INDEX_COVERAGE_CACHE_KEY: tuple[Any, ...] | None = None
_OVERLAY_INDEX_COVERAGE_CACHE_VALUE: bool | None = None
_KPI_SUMMARY_CACHE_KEY: tuple[Any, ...] | None = None
_KPI_SUMMARY_CACHE_VALUE: Any | None = None
_KPI_SUMMARY_CACHE_AT = 0.0
_WIKI_PACK_CACHE_KEY: tuple[tuple[str, float, int], ...] | None = None
_WIKI_PACK_CACHE_VALUE: dict[str, str] | None = None
_WIKI_RENDER_CACHE_KEY: tuple[Any, ...] | None = None
_WIKI_RENDER_CACHE_VALUE: str | None = None
_WIKI_INDEX_LIMIT_PER_TYPE = 500
_SKILLS_PAGE_DEFAULT_LIMIT = 100
_SKILLS_PAGE_MAX_LIMIT = 500
_KPI_SUMMARY_CACHE_SECONDS = 30
_GRAPH_REPORT_RE = re.compile(r"Nodes:\s*([\d,]+)\s*\|\s*Edges:\s*([\d,]+)")
_MAX_POST_BODY_BYTES = 64 * 1024
_DASHBOARD_INDEX_MEMBER = "graphify-out/dashboard-neighborhoods.sqlite3"
_READ_TOKEN_COOKIE = "ctx_monitor_read_token"


# ─── Data sources ────────────────────────────────────────────────────────────


def _host_allows_mutations(host: str) -> bool:
    normalized = (host or "").strip().strip("[]").rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _request_host_name(host_header: str) -> str:
    value = (host_header or "").strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end].rstrip(".").lower() if end != -1 else ""
    return value.rsplit(":", 1)[0].rstrip(".").lower()


def _origin_host_name(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    return (parsed.hostname or "").rstrip(".").lower()


def _read_token_cookie(cookie_header: str) -> str:
    if not cookie_header:
        return ""
    try:
        cookie = SimpleCookie()
        cookie.load(cookie_header)
    except CookieError:
        return ""
    morsel = cookie.get(_READ_TOKEN_COOKIE)
    return morsel.value if morsel is not None else ""


def _claude_dir() -> Path:
    return _monitor_state.claude_dir()


def _audit_log_path() -> Path:
    return _monitor_state.audit_log_path(_claude_dir())


def _events_jsonl_path() -> Path:
    return _monitor_state.events_jsonl_path(_claude_dir())


def _runtime_lifecycle_path() -> Path:
    return _monitor_state.runtime_lifecycle_path()


def _manifest_path() -> Path:
    return _monitor_state.manifest_path(_claude_dir())


def _sidecar_dir() -> Path:
    return _monitor_state.sidecar_dir(_claude_dir())


def _wiki_dir() -> Path:
    return _monitor_state.wiki_dir(_claude_dir())


def _user_config_path() -> Path:
    return _monitor_state.user_config_path(_claude_dir())


def _wiki_pack_pages() -> dict[str, str] | None:
    """Return merged wiki-pack pages, or None when packs are not installed."""
    global _WIKI_PACK_CACHE_KEY, _WIKI_PACK_CACHE_VALUE

    packs_dir = _wiki_dir() / "wiki-packs"
    if not packs_dir.is_dir():
        _WIKI_PACK_CACHE_KEY = None
        _WIKI_PACK_CACHE_VALUE = None
        return None
    key: list[tuple[str, float, int]] = []
    for path in sorted(packs_dir.rglob("*")):
        if not path.is_file() or path.name not in {
            "wiki-pack-manifest.json",
            "pages.jsonl",
            "tombstones.jsonl",
        }:
            continue
        stat = path.stat()
        key.append((path.relative_to(packs_dir).as_posix(), stat.st_mtime, stat.st_size))
    cache_key = tuple(key)
    if _WIKI_PACK_CACHE_KEY == cache_key and _WIKI_PACK_CACHE_VALUE is not None:
        return _WIKI_PACK_CACHE_VALUE

    from ctx.core.wiki.wiki_packs import load_merged_wiki_pages  # noqa: PLC0415

    pages = load_merged_wiki_pages(packs_dir)
    _WIKI_PACK_CACHE_KEY = cache_key
    _WIKI_PACK_CACHE_VALUE = pages
    return pages


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


def _mcp_shard(slug: str) -> str:
    return core_entity_types.mcp_shard(slug)


_DASHBOARD_ENTITY_SOURCES: tuple[tuple[str, str, bool], ...] = core_entity_types.entity_source_specs()
_DASHBOARD_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type for _, entity_type, _ in _DASHBOARD_ENTITY_SOURCES
)
_DEFAULT_GRAPH_FOCUS_SLUG = "github"


def _normalize_dashboard_entity_type(raw: object) -> str | None:
    return core_entity_types.normalize_entity_type(
        raw,
        allowed=_DASHBOARD_ENTITY_TYPES,
    )


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


def _wiki_entity_path(slug: str, entity_type: str | None = None) -> Path | None:
    """Resolve a slug to its wiki entity page.

    Wiki layout: ``entities/skills/<slug>.md``, ``entities/agents/<slug>.md``,
    ``entities/harnesses/<slug>.md``, or sharded
    ``entities/mcp-servers/<first-char>/<slug>.md``. Returns the first match
    unless ``entity_type`` disambiguates duplicate slugs.
    """
    # Validate slug so a crafted request can't escape the wiki tree.
    if not _is_safe_slug(slug):
        return None
    pack_pages = _wiki_pack_pages()
    for _sub, current_type, _recursive in _DASHBOARD_ENTITY_SOURCES:
        if entity_type is not None and entity_type != current_type:
            continue
        p = core_entity_types.entity_page_path(_wiki_dir(), current_type, slug)
        if p is None:
            continue
        if pack_pages is not None:
            relpath = core_entity_types.entity_relpath(current_type, slug)
            if relpath is not None and relpath.as_posix() in pack_pages:
                return p
            continue
        if p.exists():
            return p
    return None


def _wiki_entity_target_path(slug: str, entity_type: str) -> Path:
    """Return the canonical wiki entity path for a new/updated entity."""
    if not _is_safe_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    normalized = _normalize_dashboard_entity_type(entity_type)
    if normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    path = core_entity_types.entity_page_path(_wiki_dir(), normalized, slug)
    if path is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    return path


def _iter_wiki_entity_paths(
    entity_type: str | None = None,
) -> list[tuple[str, str, Path]]:
    normalized = _normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    pack_pages = _wiki_pack_pages()
    if pack_pages is not None:
        pack_rows: list[tuple[str, str, Path]] = []
        for relpath in sorted(pack_pages):
            parsed = _wiki_pack_entity_from_relpath(relpath)
            if parsed is None:
                continue
            slug, current_type = parsed
            if normalized is not None and normalized != current_type:
                continue
            path = core_entity_types.entity_page_path(_wiki_dir(), current_type, slug)
            if path is not None:
                pack_rows.append((slug, current_type, path))
        return sorted(pack_rows, key=lambda row: (row[1], row[0].lower(), row[2].as_posix()))
    base = _wiki_dir() / "entities"
    if not base.is_dir():
        return []
    file_rows: list[tuple[str, str, Path]] = []
    for sub, current_type, recursive in _DASHBOARD_ENTITY_SOURCES:
        if normalized is not None and normalized != current_type:
            continue
        root = base / sub
        if not root.is_dir():
            continue
        paths = root.rglob("*.md") if recursive else root.glob("*.md")
        for path in paths:
            slug = path.stem
            if _is_safe_slug(slug):
                file_rows.append((slug, current_type, path))
    return sorted(file_rows, key=lambda row: (row[1], row[0].lower(), row[2].as_posix()))


def _wiki_entity_detail(slug: str, entity_type: str | None = None) -> dict[str, Any] | None:
    normalized = _normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    path = _wiki_entity_path(slug, entity_type=normalized)
    if path is None:
        return None
    text = _read_wiki_entity_text(slug, normalized, path)
    if text is None:
        return None
    frontmatter, body = _parse_frontmatter(text)
    detected_type = normalized or _normalize_dashboard_entity_type(frontmatter.get("type")) or "skill"
    return {
        "slug": slug,
        "type": detected_type,
        "path": str(path),
        "frontmatter": frontmatter,
        "body": body,
    }


def _wiki_pack_entity_from_relpath(relpath: str) -> tuple[str, str] | None:
    path = Path(relpath)
    parts = path.parts
    if len(parts) < 3 or parts[0] != "entities" or path.suffix != ".md":
        return None
    entity_type = core_entity_types.ENTITY_TYPE_FOR_SUBJECT_TYPE.get(parts[1])
    if entity_type not in _DASHBOARD_ENTITY_TYPES:
        return None
    slug = path.stem
    if not _is_safe_slug(slug):
        return None
    if entity_type == "mcp-server":
        if len(parts) != 4 or parts[2] != core_entity_types.mcp_shard(slug):
            return None
    elif len(parts) != 3:
        return None
    return slug, entity_type


def _read_wiki_entity_text(
    slug: str,
    entity_type: str | None,
    path: Path,
) -> str | None:
    pack_pages = _wiki_pack_pages()
    if pack_pages is not None:
        entity_types = [entity_type] if entity_type is not None else list(_DASHBOARD_ENTITY_TYPES)
        for current_type in entity_types:
            relpath = core_entity_types.entity_relpath(current_type, slug)
            if relpath is not None and relpath.as_posix() in pack_pages:
                return pack_pages[relpath.as_posix()]
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


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
    terms = [term for term in re.split(r"\s+", query.lower().strip()) if term]
    if not terms:
        return None
    normalized = _normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    index_path = _dashboard_graph_index_path()
    if not index_path.is_file() or not _dashboard_index_matches_manifest(index_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        where: list[str] = []
        params: list[object] = []
        if normalized is not None:
            where.append("type = ?")
            params.append(normalized)
        for term in terms:
            where.append(
                "lower(id || ' ' || coalesce(label,'') || ' ' || "
                "coalesce(type,'') || ' ' || coalesce(tags,'') || ' ' || "
                "coalesce(description,'')) LIKE ?"
            )
            params.append(f"%{term}%")
        sql = (
            "SELECT id,label,type,tags,description,quality_score,usage_score,degree "
            f"FROM nodes WHERE {' AND '.join(where)} "
            "ORDER BY CASE "
            "WHEN lower(label) = ? THEN 0 "
            "WHEN lower(id) = ? THEN 1 "
            "WHEN lower(label) LIKE ? THEN 2 "
            "WHEN lower(id) LIKE ? THEN 3 "
            "ELSE 4 END, degree DESC, label COLLATE NOCASE "
            "LIMIT ?"
        )
        first = terms[0]
        params.extend([first, first, f"{first}%", f"%:{first}%", max(1, limit)])
        rows = conn.execute(sql, params).fetchall()
    except (sqlite3.Error, TypeError):
        return None
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for node_id, label, row_type, raw_tags, description, quality, usage, degree in rows:
        current_type = _normalize_dashboard_entity_type(row_type) or _graph_type_from_node_id(str(node_id))
        slug = _graph_slug_from_node_id(str(node_id))
        try:
            tags = json.loads(raw_tags or "[]")
        except json.JSONDecodeError:
            tags = []
        if not isinstance(tags, list):
            tags = []
        results.append({
            "slug": slug,
            "display_slug": _display_slug(slug),
            "type": current_type,
            "title": _display_label(label, fallback_slug=slug),
            "description": str(description or ""),
            "tags": [str(tag) for tag in tags[:12]],
            "path": "",
            "href": _entity_wiki_href(slug, current_type),
            "quality_score": quality,
            "usage_score": usage,
            "degree": int(degree or 0),
        })
    return results


def _queue_entity_refresh(
    *,
    entity_type: str,
    slug: str,
    entity_path: Path,
    content: str,
    action: str,
) -> None:
    wiki = _wiki_dir()
    wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type=entity_type,
        slug=slug,
        entity_path=entity_path,
        content=content,
        action=action,
        source="ctx-monitor",
    )
    if action == "delete":
        return
    wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"reason": f"entity-{action}", "entity_type": entity_type, "slug": slug},
        source="ctx-monitor",
    )


def _write_entity_text(path: Path, content: str) -> None:
    _safe_atomic_write_text(path, content, encoding="utf-8")


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
        queue_entity_refresh=lambda entity_type, slug, entity_path, content, action: _queue_entity_refresh(
            entity_type=entity_type,
            slug=slug,
            entity_path=entity_path,
            content=content,
            action=action,
        ),
        file_lock=file_lock,
        write_entity_text=_write_entity_text,
        parse_frontmatter=_parse_frontmatter,
        frontmatter_tags=lambda value: _frontmatter_tags(value, limit=None),
        frontmatter_text=_frontmatter_text,
        display_slug=_display_slug,
        display_label=lambda value: _display_label(value),
        entity_wiki_href=_entity_wiki_href,
    )


def _entity_runtime_deps() -> dashboard_entities.EntityRuntimeDeps:
    return dashboard_entities.EntityRuntimeDeps(
        is_safe_slug=_is_safe_slug,
        normalize_entity_type=_normalize_dashboard_entity_type,
        wiki_dir=_wiki_dir,
        claude_dir=_claude_dir,
        log_dashboard_entity_event=_log_dashboard_entity_event,
        remove_loaded_manifest_entry=_remove_loaded_manifest_entry,
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


def _frontmatter_text(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return ""
    return str(value)


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(value) <= limit:
        return value, False
    if limit <= 3:
        return value[:limit], True
    return value[: limit - 3].rstrip() + "...", True


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str).replace("</", "<\\/")


def _frontmatter_tags(value: Any, *, limit: int | None = 6) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw = _frontmatter_text(value)
        raw_items = raw.replace("[", "").replace("]", "").split(",")
    out: list[str] = []
    for item in raw_items:
        tok = str(item).strip().strip("'\"")
        if tok:
            out.append(tok)
        if limit is not None and len(out) >= limit:
            break
    return out


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
    quality_blocks = [
        match.group(1).strip()
        for match in matches
        if match.group(1).strip()
    ]
    body = _WIKI_QUALITY_BLOCK_RE.sub("\n\n", markdown_text)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    quality_markdown = "\n\n".join(quality_blocks).strip() or None
    return body, quality_markdown


def _slugish(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _display_slug(slug: str) -> str:
    """Return the user-facing slug while preserving raw IDs for links/actions."""
    text = str(slug or "")
    return text.removeprefix("skills-sh-")


def _display_label(value: Any, *, fallback_slug: str = "") -> str:
    text = str(value or fallback_slug or "")
    return _display_slug(text)


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


def _entity_wiki_href(slug: str, entity_type: str | None = None) -> str:
    suffix = f"?type={quote(entity_type)}" if entity_type in _DASHBOARD_ENTITY_TYPES else ""
    return f"/wiki/{quote(slug)}{suffix}"


def _graph_type_from_node_id(node_id: str, fallback: str = "skill") -> str:
    prefix = node_id.split(":", 1)[0] if ":" in node_id else ""
    return {
        "skill": "skill",
        "agent": "agent",
        "mcp-server": "mcp-server",
        "harness": "harness",
    }.get(prefix, fallback)


def _subgraph_sidecar(slug: str, entity_type: str) -> dict[str, Any] | None:
    sidecar = _load_sidecar(slug, entity_type=entity_type)
    return sidecar if isinstance(sidecar, dict) else None


def _subgraph_quality_cell(sidecar: dict[str, Any] | None) -> str:
    if sidecar is None:
        return "<span class='muted'>no sidecar</span>"
    grade = html.escape(str(sidecar.get("grade", "F")))
    score = float(sidecar.get("raw_score", sidecar.get("score", 0.0)) or 0.0)
    floor = str(sidecar.get("hard_floor") or "").strip()
    floor_html = (
        f" <span class='muted'>floor {html.escape(floor)}</span>"
        if floor
        else ""
    )
    return (
        f"<span class='pill grade-{grade}'>{grade}</span> "
        f"<code>{score:.3f}</code>{floor_html}"
    )


def _subgraph_node_title(
    label: str,
    entity_type: str,
    sidecar: dict[str, Any] | None,
) -> str:
    if sidecar is None:
        return f"{label} ({entity_type}) · no sidecar"
    grade = str(sidecar.get("grade", "F"))
    score = float(sidecar.get("raw_score", sidecar.get("score", 0.0)) or 0.0)
    floor = str(sidecar.get("hard_floor") or "").strip()
    floor_text = f" · floor {floor}" if floor else ""
    return f"{label} ({entity_type}) · grade {grade} · score {score:.3f}{floor_text}"


def _subgraph_node_fill(entity_type: str) -> str:
    return {
        "agent": "#f59e0b",
        "mcp-server": "#ef4444",
        "harness": "#22c55e",
        "skill": "#6366f1",
    }.get(entity_type, "#64748b")


def _subgraph_grade_stroke(sidecar: dict[str, Any] | None) -> str:
    grade = str((sidecar or {}).get("grade") or "")
    return {
        "A": "#059669",
        "B": "#2563eb",
        "C": "#d97706",
        "D": "#ea580c",
        "F": "#dc2626",
    }.get(grade, "#ffffff")


def _render_entity_subgraph_svg(
    node_by_id: dict[str, dict[str, Any]],
    edges: list[dict],
    center: str,
    sidecar_by_id: dict[str, dict[str, Any] | None],
) -> str:
    """Render an embedded, interactive 3D graph for wiki entity pages."""
    width = 980
    height = 380
    node_payload: list[dict[str, Any]] = []
    for node_id, node in sorted(
        node_by_id.items(),
        key=lambda item: (
            0 if item[0] == center else 1,
            str(item[1].get("label") or item[0]),
        ),
    ):
        node = node_by_id[node_id]
        node_type = _graph_type_from_node_id(
            node_id, str(node.get("type") or "skill"),
        )
        node_slug = _graph_slug_from_node_id(node_id)
        label = _display_label(node.get("label"), fallback_slug=node_slug)
        sidecar = sidecar_by_id.get(node_id)
        node_payload.append({
            "id": node_id,
            "slug": node_slug,
            "label": label,
            "type": node_type,
            "href": _entity_wiki_href(node_slug, node_type),
            "title": _subgraph_node_title(label, node_type, sidecar),
            "fill": _subgraph_node_fill(node_type),
            "stroke": _subgraph_grade_stroke(sidecar),
            "is_center": node_id == center,
        })

    edge_payload: list[dict[str, Any]] = []
    for edge in edges:
        data = edge.get("data", {})
        source = str(data.get("source", ""))
        target = str(data.get("target", ""))
        if source not in node_by_id or target not in node_by_id:
            continue
        shared = ", ".join(str(tag) for tag in data.get("shared_tags", [])[:6]) or "none"
        weight = float(data.get("weight", 0.0) or 0.0)
        edge_payload.append({
            "source": source,
            "target": target,
            "weight": weight,
            "title": (
                f"{_display_slug(_graph_slug_from_node_id(source))} ↔ "
                f"{_display_slug(_graph_slug_from_node_id(target))} · weight {weight:.3f} "
                f"· shared {shared}"
            ),
        })

    nodes_json = _json_for_script(node_payload)
    edges_json = _json_for_script(edge_payload)

    return (
        "<div data-testid='entity-subgraph-graph' "
        "style='border:1px solid #e5e7eb; border-radius:8px; "
        "background:#f8fafc; margin:1rem 0; overflow:hidden;'>"
        "<div style='display:flex; align-items:center; gap:0.5rem; "
        "padding:0.45rem 0.6rem; border-bottom:1px solid #e5e7eb; background:#fff;'>"
        "<button id='entity-subgraph-zoom-in' type='button'>zoom in</button>"
        "<button id='entity-subgraph-zoom-out' type='button'>zoom out</button>"
        "<span class='muted'>drag to rotate · wheel to zoom · hover nodes or edges</span>"
        "</div>"
        f"<svg data-testid='entity-subgraph-3d' viewBox='0 0 {width} {height}' "
        "width='100%' height='380' role='img' aria-label='Embedded 3D entity subgraph' "
        "style='display:block; background:#f8fafc; touch-action:none;'></svg>"
        "<div style='display:grid; grid-template-columns:1fr 1fr; gap:0.5rem; "
        "padding:0.45rem 0.6rem; border-top:1px solid #e5e7eb; background:#fff;'>"
        "<div data-testid='entity-subgraph-node-detail' class='muted'>"
        "Hover a node for sidecar grade/score/floor.</div>"
        "<div data-testid='entity-subgraph-edge-detail' class='muted'>"
        "Hover an edge for weight and shared signals.</div></div>"
        "<script>\n"
        "(function () {\n"
        f"  const nodes = {nodes_json};\n"
        f"  const edges = {edges_json};\n"
        f"  const width = {width};\n"
        f"  const height = {height};\n"
        "  const svg = document.querySelector('[data-testid=\"entity-subgraph-3d\"]');\n"
        "  const nodeDetail = document.querySelector('[data-testid=\"entity-subgraph-node-detail\"]');\n"
        "  const edgeDetail = document.querySelector('[data-testid=\"entity-subgraph-edge-detail\"]');\n"
        "  if (!svg) return;\n"
        "  const points = new Map();\n"
        "  const center = nodes.find(n => n.is_center) || nodes[0];\n"
        "  if (!center) return;\n"
        "  points.set(center.id, {x: 0, y: 0, z: 0});\n"
        "  nodes.filter(n => n.id !== center.id).forEach((n, idx) => {\n"
        "    const i = idx + 1;\n"
        "    const phi = Math.acos(1 - 2 * i / Math.max(2, nodes.length));\n"
        "    const theta = Math.PI * (3 - Math.sqrt(5)) * i;\n"
        "    const radius = 250;\n"
        "    points.set(n.id, {x: radius * Math.cos(theta) * Math.sin(phi), y: radius * Math.sin(theta) * Math.sin(phi), z: radius * Math.cos(phi)});\n"
        "  });\n"
        "  let yaw = -0.4;\n"
        "  let pitch = 0.55;\n"
        "  let zoom = 1;\n"
        "  function escapeHtml(s) { return String(s).replace(/[&<>\"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch])); }\n"
        "  function project(p) {\n"
        "    const cyaw = Math.cos(yaw), syaw = Math.sin(yaw);\n"
        "    const cp = Math.cos(pitch), sp = Math.sin(pitch);\n"
        "    const x1 = p.x * cyaw - p.z * syaw;\n"
        "    const z1 = p.x * syaw + p.z * cyaw;\n"
        "    const y1 = p.y * cp - z1 * sp;\n"
        "    const z2 = p.y * sp + z1 * cp;\n"
        "    const scale = zoom * 620 / (760 + z2);\n"
        "    return {x: width / 2 + x1 * scale, y: height / 2 + y1 * scale, z: z2, scale};\n"
        "  }\n"
        "  function attachHover() {\n"
        "    svg.querySelectorAll('[data-node-detail]').forEach(n => n.addEventListener('mouseenter', () => { nodeDetail.textContent = n.dataset.nodeDetail || ''; }));\n"
        "    svg.querySelectorAll('[data-edge-detail]').forEach(e => e.addEventListener('mouseenter', () => { edgeDetail.textContent = e.dataset.edgeDetail || ''; }));\n"
        "  }\n"
        "  function draw() {\n"
        "    const projected = new Map();\n"
        "    points.forEach((p, id) => projected.set(id, project(p)));\n"
        "    const edgeLines = edges.map(e => {\n"
        "      const s = projected.get(e.source);\n"
        "      const t = projected.get(e.target);\n"
        "      if (!s || !t) return '';\n"
        "      const w = Math.max(1, Math.min(4, 1 + Math.sqrt(Math.max(0, Number(e.weight || 1)))));\n"
        "      return '<line x1=\"' + s.x.toFixed(1) + '\" y1=\"' + s.y.toFixed(1) + '\" x2=\"' + t.x.toFixed(1) + '\" y2=\"' + t.y.toFixed(1) + '\" stroke=\"#64748b\" stroke-opacity=\"0.55\" stroke-width=\"' + w.toFixed(2) + '\" />';\n"
        "    }).join('');\n"
        "    const nodeEls = nodes.slice().sort((a, b) => (projected.get(a.id)?.z || 0) - (projected.get(b.id)?.z || 0)).map(n => {\n"
        "      const p = projected.get(n.id) || {x: width / 2, y: height / 2, z: 0, scale: 1};\n"
        "      const r = Math.max(7, (n.is_center ? 18 : 12) * Math.max(0.7, p.scale));\n"
        "      return '<a href=\"' + escapeHtml(n.href) + '\"><g data-testid=\"entity-subgraph-node\" data-node-detail=\"' + escapeHtml(n.title) + '\"><title>' + escapeHtml(n.title) + '</title><circle cx=\"' + p.x.toFixed(1) + '\" cy=\"' + p.y.toFixed(1) + '\" r=\"' + r + '\" fill=\"' + escapeHtml(n.fill) + '\" stroke=\"' + escapeHtml(n.stroke) + '\" stroke-width=\"3\" /><text x=\"' + p.x.toFixed(1) + '\" y=\"' + (p.y + r + 14).toFixed(1) + '\" text-anchor=\"middle\" font-size=\"11\" fill=\"#111827\" style=\"pointer-events:none;\">' + escapeHtml(String(n.label).slice(0, 28)) + '</text></g></a>';\n"
        "    }).join('');\n"
        "    const edgeHits = edges.map(e => {\n"
        "      const s = projected.get(e.source);\n"
        "      const t = projected.get(e.target);\n"
        "      if (!s || !t) return '';\n"
        "      const hx1 = s.x + (t.x - s.x) * 0.18, hy1 = s.y + (t.y - s.y) * 0.18;\n"
        "      const hx2 = s.x + (t.x - s.x) * 0.82, hy2 = s.y + (t.y - s.y) * 0.82;\n"
        "      return '<line data-testid=\"entity-subgraph-edge\" data-edge-detail=\"' + escapeHtml(e.title) + '\" x1=\"' + hx1.toFixed(1) + '\" y1=\"' + hy1.toFixed(1) + '\" x2=\"' + hx2.toFixed(1) + '\" y2=\"' + hy2.toFixed(1) + '\" stroke=\"transparent\" stroke-width=\"12\" style=\"pointer-events:stroke;\"><title>' + escapeHtml(e.title) + '</title></line>';\n"
        "    }).join('');\n"
        "    svg.innerHTML = '<rect width=\"100%\" height=\"100%\" fill=\"#f8fafc\" />' + edgeLines + nodeEls + edgeHits;\n"
        "    attachHover();\n"
        "  }\n"
        "  document.getElementById('entity-subgraph-zoom-in')?.addEventListener('click', () => { zoom = Math.min(2.5, zoom * 1.18); draw(); });\n"
        "  document.getElementById('entity-subgraph-zoom-out')?.addEventListener('click', () => { zoom = Math.max(0.35, zoom / 1.18); draw(); });\n"
        "  let dragging = false, lastX = 0, lastY = 0;\n"
        "  svg.addEventListener('pointerdown', ev => { if (ev.target.closest('[data-3d-node-id]') || ev.target.closest('[data-edge-detail]')) return; dragging = true; lastX = ev.clientX; lastY = ev.clientY; svg.setPointerCapture(ev.pointerId); });\n"
        "  svg.addEventListener('pointerup', ev => { dragging = false; try { svg.releasePointerCapture(ev.pointerId); } catch (_) {} });\n"
        "  svg.addEventListener('pointermove', ev => { if (!dragging) return; yaw += (ev.clientX - lastX) * 0.01; pitch += (ev.clientY - lastY) * 0.01; pitch = Math.max(-1.35, Math.min(1.35, pitch)); lastX = ev.clientX; lastY = ev.clientY; draw(); });\n"
        "  svg.addEventListener('wheel', ev => { ev.preventDefault(); zoom = Math.max(0.35, Math.min(2.5, zoom * (ev.deltaY < 0 ? 1.08 : 0.92))); draw(); }, {passive:false});\n"
        "  draw();\n"
        "})();\n"
        "</script>"
        "</div>"
    )


def _render_entity_subgraph(slug: str, entity_type: str | None = None) -> str:
    """Render a compact 1-hop subgraph table for wiki entity pages."""
    graph = _graph_neighborhood(slug, hops=1, limit=32, entity_type=entity_type)
    center = graph.get("center")
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    if not center:
        return (
            "<div class='card'>"
            "<p class='muted'>No graph node was found for this entity.</p>"
            "</div>"
        )
    node_by_id = {
        str(node.get("data", {}).get("id", "")): node.get("data", {})
        for node in nodes
    }
    sidecar_by_id = {
        node_id: _subgraph_sidecar(
            _graph_slug_from_node_id(node_id),
            _graph_type_from_node_id(node_id, str(node.get("type") or "skill")),
        )
        for node_id, node in node_by_id.items()
    }
    rows: list[str] = []
    for edge in edges:
        data = edge.get("data", {})
        source = str(data.get("source", ""))
        target = str(data.get("target", ""))
        other_id = target if source == center else source
        if other_id == center or other_id not in node_by_id:
            continue
        other = node_by_id[other_id]
        other_type = _graph_type_from_node_id(other_id, str(other.get("type", "skill")))
        other_slug = other_id.split(":", 1)[-1]
        shared = ", ".join(str(tag) for tag in data.get("shared_tags", [])[:6])
        shared_html = html.escape(shared) if shared else "<span class='muted'>none</span>"
        quality_html = _subgraph_quality_cell(sidecar_by_id.get(other_id))
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(_entity_wiki_href(other_slug, other_type))}'>"
            f"{html.escape(str(other.get('label') or other_slug))}</a></td>"
            f"<td><span class='pill entity-type-{html.escape(other_type)}'>"
            f"{html.escape(other_type)}</span></td>"
            f"<td>{quality_html}</td>"
            f"<td><code>{float(data.get('weight', 0.0)):.3f}</code></td>"
            f"<td>{shared_html}</td>"
            "</tr>"
        )
    table = (
        "<table><tr><th>Entity</th><th>Type</th><th>Quality sidecar</th>"
        "<th>Weight</th><th>Shared signals</th></tr>"
        + ("".join(rows) if rows else "<tr><td colspan='5' class='muted'>No neighbors under the current limit.</td></tr>")
        + "</table>"
    )
    return (
        "<div class='card'>"
        "<h2>Subgraph</h2>"
        f"<p class='muted'>{len(nodes)} nodes and {len(edges)} edges in the 1-hop neighborhood.</p>"
        + _render_entity_subgraph_svg(node_by_id, edges, center, sidecar_by_id)
        + table
        + "</div>"
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
    _atomic_write_text(_manifest_path(), json.dumps(manifest, indent=2) + "\n")


def _read_skill_manifest_only() -> dict:
    """Read the mutable skill manifest without synthetic harness rows."""
    path = _manifest_path()
    if not path.exists():
        return {"load": [], "unload": [], "warnings": []}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"load": [], "unload": [], "warnings": []}
    if not isinstance(manifest, dict):
        return {"load": [], "unload": [], "warnings": []}
    if not isinstance(manifest.get("load"), list):
        manifest["load"] = []
    if not isinstance(manifest.get("unload"), list):
        manifest["unload"] = []
    if not isinstance(manifest.get("warnings"), list):
        manifest["warnings"] = []
    return manifest


def _remove_loaded_manifest_entry(slug: str, entity_type: str) -> list[dict]:
    """Remove loaded rows for one entity tuple and return removed rows."""
    path = _manifest_path()
    with file_lock(path):
        manifest = _read_skill_manifest_only()
        removed: list[dict] = []
        remaining: list[dict] = []
        for entry in manifest.get("load", []):
            entry_type = str(entry.get("entity_type") or "skill")
            if entry.get("skill") == slug and entry_type == entity_type:
                removed.append(entry)
            else:
                remaining.append(entry)
        if not removed:
            return []
        manifest["load"] = remaining
        unloaded = {
            (entry.get("skill"), str(entry.get("entity_type") or "skill"))
            for entry in manifest.get("unload", [])
        }
        preserved: dict[str, object] = {}
        for field in ("command", "json_config", "priority", "reason"):
            value = removed[0].get(field)
            if value not in (None, ""):
                preserved[field] = value
        if (slug, entity_type) not in unloaded:
            entry = {
                "skill": slug,
                "entity_type": entity_type,
                "source": removed[0].get("source") or "ctx-monitor",
            }
            entry.update(preserved)
            manifest.setdefault("unload", []).append(entry)
        elif preserved:
            for entry in manifest.get("unload", []):
                if (
                    entry.get("skill") == slug
                    and str(entry.get("entity_type") or "skill") == entity_type
                ):
                    for field, value in preserved.items():
                        entry.setdefault(field, value)
                    break
        _save_manifest(manifest)
        return removed


def _log_dashboard_entity_event(
    entity_type: str,
    action: str,
    slug: str,
) -> None:
    """Append a dashboard-visible audit row for a load/unload action."""
    try:
        from ctx_audit_log import log
        if entity_type == "skill":
            log(
                f"skill.{action}",
                subject_type="skill",
                subject=slug,
                actor="user",
                meta={"via": "ctx-monitor"},
                path=_audit_log_path(),
            )
        elif entity_type == "agent":
            log(
                f"agent.{action}",
                subject_type="agent",
                subject=slug,
                actor="user",
                meta={"via": "ctx-monitor"},
                path=_audit_log_path(),
            )
        elif entity_type == "mcp-server":
            log(
                "toolbox.triggered",
                subject_type="toolbox",
                subject=slug,
                actor="user",
                meta={
                    "via": "ctx-monitor",
                    "entity_type": "mcp-server",
                    "action": action,
                },
                path=_audit_log_path(),
            )
    except Exception:  # noqa: BLE001
        pass


def _read_manifest() -> dict:
    """Return current loaded entities from the skill manifest plus harness installs."""
    path = _manifest_path()
    manifest: dict[str, Any]
    if not path.exists():
        manifest = {"load": [], "unload": [], "warnings": []}
    else:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {"load": [], "unload": [], "warnings": []}
    if not isinstance(manifest, dict):
        manifest = {"load": [], "unload": [], "warnings": []}
    load_rows = manifest.setdefault("load", [])
    if not isinstance(load_rows, list):
        load_rows = []
        manifest["load"] = load_rows
    manifest.setdefault("unload", [])
    manifest.setdefault("warnings", [])
    existing = {
        (str(row.get("entity_type") or "skill"), str(row.get("skill") or ""))
        for row in load_rows
        if isinstance(row, dict)
    }
    for row in _read_harness_install_rows():
        key = ("harness", str(row.get("skill") or ""))
        if key not in existing:
            load_rows.append(row)
            existing.add(key)
    return manifest


def _read_harness_install_rows() -> list[dict]:
    """Return installed harness records as manifest-compatible load rows."""
    root = _claude_dir() / "harness-installs"
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("status") != "installed":
            continue
        slug = str(data.get("slug") or path.stem).strip()
        if not slug or not _is_safe_slug(slug):
            continue
        rows.append({
            "skill": slug,
            "entity_type": "harness",
            "source": "ctx-harness-install",
            "command": data.get("target") or data.get("repo_url") or "",
            "installed_at": data.get("installed_at", ""),
            "status": data.get("status", "installed"),
        })
    return rows


def _queue_job_summary(job: wiki_queue.QueueJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "worker_id": job.worker_id,
        "leased_until": job.leased_until,
        "available_at": job.available_at,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "source": job.payload.get("source"),
        "payload_keys": sorted(str(key) for key in job.payload),
    }


def _queue_status() -> dict[str, Any]:
    """Return durable wiki/graph queue state without creating the DB."""
    db_path = wiki_queue.queue_db_path(_wiki_dir())
    counts = {
        wiki_queue.STATUS_PENDING: 0,
        wiki_queue.STATUS_RUNNING: 0,
        wiki_queue.STATUS_SUCCEEDED: 0,
        wiki_queue.STATUS_FAILED: 0,
        wiki_queue.STATUS_CANCELLED: 0,
    }
    if not db_path.exists():
        return {
            "available": False,
            "db_path": str(db_path),
            "total": 0,
            "counts": counts,
            "recent_jobs": [],
        }
    try:
        raw_counts = wiki_queue.count_jobs_by_status(db_path)
        recent = wiki_queue.list_recent_jobs(db_path, limit=20)
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "db_path": str(db_path),
            "total": 0,
            "counts": counts,
            "recent_jobs": [],
            "error": str(exc),
        }
    for status, count in raw_counts.items():
        counts[status] = count
    return {
        "available": True,
        "db_path": str(db_path),
        "total": sum(raw_counts.values()),
        "counts": counts,
        "recent_jobs": [_queue_job_summary(job) for job in recent],
    }


def _file_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "size": 0, "mtime": None}
    try:
        stat = path.stat()
    except OSError as exc:
        return {
            "path": str(path),
            "exists": False,
            "size": 0,
            "mtime": None,
            "error": str(exc),
        }
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def _pack_dir_status(packs_dir: Path, *, manifest_name: str) -> dict[str, Any]:
    """Return summary state for a modular base/overlay pack directory."""
    if not packs_dir.exists():
        return {
            "path": str(packs_dir),
            "exists": False,
            "size": 0,
            "mtime": None,
            "pack_count": 0,
            "base_count": 0,
            "overlay_count": 0,
            "pack_ids": [],
        }
    if not packs_dir.is_dir():
        return {
            "path": str(packs_dir),
            "exists": False,
            "size": 0,
            "mtime": None,
            "pack_count": 0,
            "base_count": 0,
            "overlay_count": 0,
            "pack_ids": [],
            "error": "pack path is not a directory",
        }
    total_size = 0
    newest = 0.0
    pack_ids: list[str] = []
    base_count = 0
    overlay_count = 0
    errors: list[str] = []
    try:
        files = [path for path in packs_dir.rglob("*") if path.is_file()]
    except OSError as exc:
        return {
            "path": str(packs_dir),
            "exists": False,
            "size": 0,
            "mtime": None,
            "pack_count": 0,
            "base_count": 0,
            "overlay_count": 0,
            "pack_ids": [],
            "error": str(exc),
        }
    for path in files:
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        total_size += stat.st_size
        newest = max(newest, stat.st_mtime)
        if path.name != manifest_name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{path.name}: manifest is not an object")
            continue
        pack_id = str(payload.get("pack_id") or path.parent.name)
        pack_ids.append(pack_id)
        pack_type = payload.get("pack_type")
        if pack_type == "base":
            base_count += 1
        elif pack_type == "overlay":
            overlay_count += 1
        else:
            errors.append(f"{pack_id}: unknown pack_type {pack_type!r}")
    status: dict[str, Any] = {
        "path": str(packs_dir),
        "exists": True,
        "size": total_size,
        "mtime": newest or None,
        "pack_count": len(pack_ids),
        "base_count": base_count,
        "overlay_count": overlay_count,
        "pack_ids": sorted(pack_ids)[:25],
    }
    if errors:
        status["error"] = "; ".join(errors[:5])
    return status


def _graph_store_status(graph_dir: Path) -> dict[str, Any]:
    """Return SQLite operational-store state for the active graph directory."""
    db_path = graph_dir / "graph-store.sqlite3"
    status = _file_status(db_path)
    try:
        from ctx.core.graph.graph_store import validate_graph_store  # noqa: PLC0415

        validation = validate_graph_store(db_path, graph_dir)
    except (OSError, ValueError) as exc:
        validation = {
            "ok": False,
            "fresh": False,
            "nodes": 0,
            "edges": 0,
            "errors": [str(exc)],
        }
    node_count = validation.get("nodes")
    edge_count = validation.get("edges")
    status.update({
        "ok": bool(validation.get("ok")),
        "fresh": bool(validation.get("fresh")),
        "nodes": node_count if isinstance(node_count, int) else 0,
        "edges": edge_count if isinstance(edge_count, int) else 0,
        "errors": validation.get("errors") if isinstance(validation.get("errors"), list) else [],
    })
    return status


def _pack_compaction_artifact_status(wiki: Path) -> dict[str, Any]:
    """Return coordinated graph/wiki pack compaction state for /status."""
    try:
        from ctx.core.wiki.pack_compaction import pack_compaction_status  # noqa: PLC0415

        status = pack_compaction_status(wiki_path=wiki, validate=False)
    except Exception as exc:  # noqa: BLE001 - status should render degraded state.
        return {
            "path": str(wiki),
            "exists": False,
            "size": 0,
            "mtime": None,
            "error": str(exc),
        }
    graph_pack_count = status.get("graph_pack_count")
    wiki_pack_count = status.get("wiki_pack_count")
    return {
        "path": str(wiki),
        "exists": bool(
            (graph_pack_count if isinstance(graph_pack_count, int) else 0)
            or (wiki_pack_count if isinstance(wiki_pack_count, int) else 0)
        ),
        "size": 0,
        "mtime": None,
        **status,
    }


def _repo_graph_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "graph"


def _first_existing_file_status(*paths: Path) -> dict[str, Any]:
    for path in paths:
        if path.exists():
            return _file_status(path)
    return _file_status(paths[0])


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


def _promotion_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    previous = _dict_or_empty(data.get("previous"))
    candidate = _dict_or_empty(data.get("candidate"))
    current = _dict_or_empty(data.get("current"))
    return {
        "path": str(path),
        "status": data.get("status"),
        "target": data.get("target"),
        "started_at": data.get("started_at"),
        "promoted_at": data.get("promoted_at"),
        "previous_sha256": previous.get("sha256"),
        "previous_size": previous.get("size"),
        "candidate_sha256": candidate.get("sha256"),
        "candidate_size": candidate.get("size"),
        "current_sha256": current.get("sha256"),
        "current_size": current.get("size"),
    }


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _artifact_status() -> dict[str, Any]:
    """Return shipped graph/wiki artifact file state and promotion metadata."""
    wiki = _wiki_dir()
    graph_dir = wiki / "graphify-out"
    claude_graph_dir = _claude_dir() / "graph"
    repo_graph_dir = _repo_graph_dir()
    promotion_paths = sorted(
        {
            *graph_dir.glob("*.promotion.json"),
            *wiki.glob("*.promotion.json"),
            *claude_graph_dir.glob("*.promotion.json"),
        },
        key=lambda path: str(path),
    )
    promotions = [
        promotion
        for promotion in (_promotion_status(path) for path in promotion_paths)
        if promotion is not None
    ]
    return {
        "graph_json": _file_status(graph_dir / "graph.json"),
        "graph_packs": _pack_dir_status(
            graph_dir / "packs",
            manifest_name="graph-pack-manifest.json",
        ),
        "graph_delta_json": _file_status(graph_dir / "graph-delta.json"),
        "communities_json": _file_status(graph_dir / "communities.json"),
        "graph_store": _graph_store_status(graph_dir),
        "wiki_packs": _pack_dir_status(
            wiki / "wiki-packs",
            manifest_name="wiki-pack-manifest.json",
        ),
        "pack_compaction": _pack_compaction_artifact_status(wiki),
        "wiki_graph_tar": _first_existing_file_status(
            claude_graph_dir / "wiki-graph.tar.gz",
            repo_graph_dir / "wiki-graph.tar.gz",
        ),
        "skills_sh_catalog": _first_existing_file_status(
            wiki / "external-catalogs" / "skills-sh" / "catalog.json",
            claude_graph_dir / "skills-sh-catalog.json.gz",
            repo_graph_dir / "skills-sh-catalog.json.gz",
        ),
        "promotion_count": len(promotions),
        "promotions": promotions,
    }


def _status_payload() -> dict[str, Any]:
    return {
        "queue": _queue_status(),
        "artifacts": _artifact_status(),
    }


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    if limit is not None and limit <= 0:
        return []
    out: deque[dict] | list[dict]
    out = deque(maxlen=limit) if limit is not None else []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                out.append(event)
    return list(out)


def _runtime_lifecycle_events(limit: int | None = 200) -> list[dict[str, Any]]:
    events = _read_jsonl(_runtime_lifecycle_path(), limit=limit)
    return [
        event for event in events
        if event.get("action") in {"validation", "escalation"}
    ]


def _runtime_escalation_key(event: dict[str, Any]) -> str:
    for field in ("escalation_id", "event_id", "id"):
        value = event.get(field)
        if value:
            return str(value)
    return "\0".join(
        str(event.get(field) or "")
        for field in ("session_id", "trigger", "reason", "severity")
    )


def _runtime_lifecycle_summary(limit: int = 200) -> dict[str, Any]:
    events = _runtime_lifecycle_events(limit=None)
    validations = [
        event for event in events if event.get("action") == "validation"
    ]
    escalations = [
        event for event in events if event.get("action") == "escalation"
    ]
    open_by_key: dict[str, dict[str, Any]] = {}
    for event in escalations:
        key = _runtime_escalation_key(event)
        status = str(event.get("status") or "open").lower()
        if status == "open":
            open_by_key[key] = event
        else:
            open_by_key.pop(key, None)
    open_escalations = list(open_by_key.values())
    validation_failures = [
        event for event in validations
        if str(event.get("status") or "").lower() in {"failed", "error"}
    ]
    sessions = sorted({
        str(event.get("session_id") or "")
        for event in events
        if event.get("session_id")
    })
    return {
        "path": str(_runtime_lifecycle_path()),
        "events_total": len(events),
        "validations_total": len(validations),
        "validation_failures": len(validation_failures),
        "escalations_total": len(escalations),
        "open_escalations_total": len(open_escalations),
        "latest_validation": validations[-1] if validations else None,
        "recent_validations": validations[-20:],
        "open_escalations": open_escalations[-20:],
        "sessions": sessions,
    }


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
    """Join audit-log session events with skill-events.jsonl load/unloads."""
    audit = _read_jsonl(_audit_log_path())
    events = _read_jsonl(_events_jsonl_path())

    by_session: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "session_id": "",
            "first_seen": None,
            "last_seen": None,
            "skills_loaded": set(),
            "skills_unloaded": set(),
            "agents_loaded": set(),
            "agents_unloaded": set(),
            "mcps_loaded": set(),
            "mcps_unloaded": set(),
            "score_updates": 0,
            "lifecycle_transitions": 0,
        }
    )

    for line in audit:
        sid = line.get("session_id") or "unknown"
        row = by_session[sid]
        row["session_id"] = sid
        ts = line.get("ts")
        if ts and (row["first_seen"] is None or ts < row["first_seen"]):
            row["first_seen"] = ts
        if ts and (row["last_seen"] is None or ts > row["last_seen"]):
            row["last_seen"] = ts
        event = line.get("event", "")
        if event == "skill.loaded":
            row["skills_loaded"].add(line.get("subject", ""))
        elif event == "skill.unloaded":
            row["skills_unloaded"].add(line.get("subject", ""))
        elif event == "agent.loaded":
            row["agents_loaded"].add(line.get("subject", ""))
        elif event == "agent.unloaded":
            row["agents_unloaded"].add(line.get("subject", ""))
        elif event == "toolbox.triggered":
            raw_meta = line.get("meta")
            meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
            if meta.get("entity_type") == "mcp-server":
                action = meta.get("action")
                if action == "loaded":
                    row["mcps_loaded"].add(line.get("subject", ""))
                elif action == "unloaded":
                    row["mcps_unloaded"].add(line.get("subject", ""))
        elif event.endswith(".score_updated"):
            row["score_updates"] += 1
        elif event in ("skill.archived", "skill.demoted", "skill.restored",
                       "skill.deleted", "agent.archived", "agent.demoted",
                       "agent.restored", "agent.deleted"):
            row["lifecycle_transitions"] += 1

    for line in events:
        sid = line.get("session_id") or "unknown"
        row = by_session[sid]
        row["session_id"] = sid
        ts = line.get("timestamp")
        if ts and (row["first_seen"] is None or ts < row["first_seen"]):
            row["first_seen"] = ts
        if ts and (row["last_seen"] is None or ts > row["last_seen"]):
            row["last_seen"] = ts
        action = line.get("event")
        entity_type = (
            _audit_entity_type(line)
            or ("agent" if line.get("agent") else None)
            or ("mcp-server" if line.get("mcp") or line.get("mcp_server") else None)
            or ("skill" if line.get("skill") else None)
        )
        if entity_type == "agent":
            subject = line.get("agent")
        elif entity_type == "mcp-server":
            subject = line.get("mcp") or line.get("mcp_server")
        else:
            subject = line.get("skill")
        if action == "load" and subject:
            if entity_type == "agent":
                row["agents_loaded"].add(subject)
            elif entity_type == "mcp-server":
                row["mcps_loaded"].add(subject)
            else:
                row["skills_loaded"].add(subject)
        elif action == "unload" and subject:
            if entity_type == "agent":
                row["agents_unloaded"].add(subject)
            elif entity_type == "mcp-server":
                row["mcps_unloaded"].add(subject)
            else:
                row["skills_unloaded"].add(subject)

    summaries: list[dict] = []
    for row in by_session.values():
        summaries.append({
            "session_id": row["session_id"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "skills_loaded": sorted(row["skills_loaded"]),
            "skills_unloaded": sorted(row["skills_unloaded"]),
            "agents_loaded": sorted(row["agents_loaded"]),
            "agents_unloaded": sorted(row["agents_unloaded"]),
            "mcps_loaded": sorted(row["mcps_loaded"]),
            "mcps_unloaded": sorted(row["mcps_unloaded"]),
            "score_updates": row["score_updates"],
            "lifecycle_transitions": row["lifecycle_transitions"],
        })
    summaries.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    return summaries


def _grade_distribution() -> dict[str, int]:
    return _sidecar_service.grade_distribution(_sidecar_dir())


def _grade_distribution_payload() -> dict[str, Any]:
    return _sidecar_service.grade_distribution_payload(_sidecar_dir())


def _session_detail(session_id: str) -> dict:
    audit = _read_jsonl(_audit_log_path())
    events = _read_jsonl(_events_jsonl_path())
    session_audit = [r for r in audit if r.get("session_id") == session_id]
    session_events = [e for e in events if e.get("session_id") == session_id]
    return {
        "session_id": session_id,
        "audit_entries": session_audit,
        "load_events": session_events,
    }


# ─── HTML rendering ──────────────────────────────────────────────────────────




# ─── Graph neighborhood (for /graph) ────────────────────────────────────────


def _graph_slug_from_node_id(node_id: str) -> str:
    return node_id.split(":", 1)[-1] if ":" in node_id else node_id


def _resolve_graph_center(
    G: Any,
    slug: str,
    entity_type: str | None,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    """Resolve exact and fuzzy graph focus queries to one graph node id."""
    raw_query = str(slug or "").strip()
    if not raw_query or "/" in raw_query or "\\" in raw_query or ".." in raw_query:
        return None, None, []
    normalized_query = _slugish(raw_query)
    if not normalized_query or not _is_safe_slug(normalized_query):
        return None, None, []

    entity_types = (
        (entity_type,)
        if entity_type is not None
        else _DASHBOARD_ENTITY_TYPES
    )
    for current_type in entity_types:
        for candidate_slug in (raw_query, normalized_query):
            candidate = f"{current_type}:{candidate_slug}"
            if candidate in G:
                return candidate, None, [candidate_slug]

    matches: list[tuple[tuple[int, int, int], str, str]] = []
    query_tokens = set(normalized_query.split("-"))
    for node_id in G.nodes:
        node_type = _graph_type_from_node_id(str(node_id))
        if node_type not in entity_types:
            continue
        data = G.nodes.get(node_id, {})
        node_slug = _graph_slug_from_node_id(str(node_id))
        label = _display_label(data.get("label"), fallback_slug=node_slug)
        haystacks = {_slugish(node_slug), _slugish(_display_slug(node_slug)), _slugish(label)}
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
            any(token in h for h in haystacks) for token in query_tokens
        ):
            rank = 3
        if rank is None:
            continue
        try:
            degree = int(G.degree[node_id])
        except Exception:  # noqa: BLE001
            degree = 0
        matches.append(((rank, len(node_slug), -degree), str(node_id), node_slug))

    matches.sort(key=lambda item: item[0])
    suggestions = []
    for _, _node_id, suggestion in matches[:8]:
        display_suggestion = _display_slug(suggestion)
        if display_suggestion not in suggestions:
            suggestions.append(display_suggestion)
    if not matches:
        return None, None, suggestions
    center = matches[0][1]
    resolved_slug = _graph_slug_from_node_id(center)
    return (
        center,
        {"query": raw_query, "slug": resolved_slug, "id": center},
        suggestions,
    )


def _unit_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score):
        return None
    return max(0.0, min(1.0, score))


def _dashboard_score_payload(field: str, value: Any) -> dict[str, float | None]:
    score = _unit_score(value)
    payload: dict[str, float | None] = {field: score}
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return payload
    if math.isfinite(raw) and score is not None and raw != score:
        payload[f"{field}_raw"] = raw
    return payload


def _format_count(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _load_direct_sidecar(slug: str, entity_type: str | None = None) -> dict | None:
    if not _is_safe_slug(slug):
        return None
    for path in (
        _sidecar_dir() / f"{slug}.json",
        _sidecar_dir() / "mcp" / f"{slug}.json",
    ):
        if not path.exists():
            continue
        sidecar = _read_sidecar_file(path)
        if sidecar is None:
            continue
        if entity_type is None or _sidecar_entity_type(sidecar) == entity_type:
            return sidecar
    return None


def _sidecar_score_inputs(slug: str, entity_type: str) -> tuple[float | None, float | None]:
    sidecar = _load_direct_sidecar(slug, entity_type=entity_type)
    if not isinstance(sidecar, dict):
        return None, None
    quality = _unit_score(sidecar.get("score", sidecar.get("raw_score")))
    usage = None
    signals = sidecar.get("signals")
    if isinstance(signals, dict):
        telemetry = signals.get("telemetry")
        if isinstance(telemetry, dict):
            usage = _unit_score(telemetry.get("score"))
    return quality, usage


def _graph_node_size(
    nid: str,
    data: dict[str, Any],
    *,
    entity_type: str,
    degree: int,
    max_degree: int,
) -> dict[str, Any]:
    """Return bounded visual size metadata for a graph node."""
    slug = nid.split(":", 1)[-1]
    quality = _unit_score(data.get("quality_score"))
    usage = _unit_score(data.get("usage_score"))
    if quality is None or usage is None:
        sidecar_quality, sidecar_usage = _sidecar_score_inputs(slug, entity_type)
        quality = quality if quality is not None else sidecar_quality
        usage = usage if usage is not None else sidecar_usage

    quality_value = 0.35 if quality is None else quality
    usage_value = 0.0 if usage is None else usage
    popularity = (
        math.log1p(max(0, degree)) / math.log1p(max(1, max_degree))
        if max_degree > 0
        else 0.0
    )
    signal = max(
        0.0,
        min(1.0, 0.45 * quality_value + 0.35 * usage_value + 0.20 * popularity),
    )
    return {
        "node_size": round(8.0 + signal * 16.0, 2),
        "size_signal": round(signal, 4),
        "size_reason": (
            f"quality {quality_value:.3f}; usage {usage_value:.3f}; "
            f"popularity {popularity:.3f}"
        ),
    }


def _dashboard_graph_index_path() -> Path:
    return _wiki_dir() / "graphify-out" / "dashboard-neighborhoods.sqlite3"


def _dashboard_graph_manifest_export_id() -> str | None:
    manifest_path = _wiki_dir() / "graphify-out" / "graph-export-manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    export_id = data.get("export_id") if isinstance(data, dict) else None
    if not isinstance(export_id, str) or not export_id.strip():
        return None
    return export_id.strip()


def _dashboard_index_meta(index_path: Path) -> dict[str, Any] | None:
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = conn.execute("SELECT key,value FROM meta").fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    try:
        return {str(key): json.loads(str(value)) for key, value in rows}
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _dashboard_index_matches_manifest(index_path: Path) -> bool:
    manifest_export_id = _dashboard_graph_manifest_export_id()
    if manifest_export_id is None:
        return False
    meta = _dashboard_index_meta(index_path)
    if meta is None:
        return False
    return meta.get("export_id") == manifest_export_id


def _dashboard_graph_has_runtime_overlays() -> bool:
    overlay = _wiki_dir() / "graphify-out" / "entity-overlays.jsonl"
    try:
        return overlay.is_file() and overlay.stat().st_size > 0
    except OSError:
        return False


def _overlay_index_coverage_key(index_path: Path, overlay: Path) -> tuple[Any, ...] | None:
    try:
        index_stat = index_path.stat()
        overlay_stat = overlay.stat()
    except OSError:
        return None
    return (
        index_path.resolve(),
        index_stat.st_mtime,
        index_stat.st_size,
        overlay.resolve(),
        overlay_stat.st_mtime,
        overlay_stat.st_size,
        _dashboard_graph_manifest_export_id(),
    )


def _active_dashboard_overlay_records(overlay: Path) -> list[dict[str, Any]] | None:
    try:
        from ctx.core.graph.entity_overlays import active_overlay_records

        rows = []
        for line in overlay.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                return None
            rows.append(payload)
        return [dict(row) for row in active_overlay_records(rows)]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _dashboard_overlay_matches_known_release(overlay: Path) -> bool:
    try:
        from ctx_init import _GRAPH_ENTITY_OVERLAY_SHA256
    except (ImportError, AttributeError):
        return False
    if not isinstance(_GRAPH_ENTITY_OVERLAY_SHA256, str) or not _GRAPH_ENTITY_OVERLAY_SHA256:
        return False
    try:
        data = overlay.read_bytes().replace(b"\r\n", b"\n")
        return hashlib.sha256(data).hexdigest() == _GRAPH_ENTITY_OVERLAY_SHA256
    except OSError:
        return False


def _dashboard_index_uncovered_overlay_nodes(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    require_edges: bool,
) -> set[str] | None:
    uncovered: set[str] = set()
    neighbor_targets: dict[str, set[str]] = {}

    def node_exists(node_id: str) -> bool:
        return bool(conn.execute(
            "SELECT 1 FROM nodes WHERE id=? LIMIT 1",
            (node_id,),
        ).fetchone())

    def indexed_neighbors(node_id: str) -> set[str]:
        cached = neighbor_targets.get(node_id)
        if cached is not None:
            return cached
        row = conn.execute(
            "SELECT payload FROM neighbors WHERE source=?",
            (node_id,),
        ).fetchone()
        targets: set[str] = set()
        if row is not None:
            try:
                payload = json.loads(zlib.decompress(row["payload"]).decode("utf-8"))
            except (TypeError, json.JSONDecodeError, zlib.error):
                payload = []
            if isinstance(payload, list):
                targets = {
                    str(edge.get("target"))
                    for edge in payload
                    if isinstance(edge, dict) and isinstance(edge.get("target"), str)
                }
        neighbor_targets[node_id] = targets
        return targets

    for record in records:
        nodes = record.get("nodes", [])
        edges = record.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            return None
        if not nodes and edges:
            return None
        for node in nodes:
            if not isinstance(node, dict):
                return None
            node_id = node.get("id")
            if not isinstance(node_id, str):
                return None
            if not node_exists(node_id):
                uncovered.add(node_id)
        if not require_edges:
            continue
        for edge in edges:
            if not isinstance(edge, dict):
                return None
            source = edge.get("source")
            target = edge.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                return None
            source_exists = node_exists(source)
            target_exists = node_exists(target)
            if not source_exists:
                uncovered.add(source)
            if not target_exists:
                uncovered.add(target)
            if not source_exists or not target_exists:
                uncovered.update((source, target))
                continue
            if target not in indexed_neighbors(source) and source not in indexed_neighbors(target):
                uncovered.update((source, target))
    return uncovered


def _dashboard_uncovered_runtime_overlay_nodes(index_path: Path) -> set[str] | None:
    overlay = _wiki_dir() / "graphify-out" / "entity-overlays.jsonl"
    try:
        if not overlay.is_file() or overlay.stat().st_size == 0:
            return set()
    except OSError:
        return set()
    if not index_path.is_file() or not _dashboard_index_matches_manifest(index_path):
        return None
    records = _active_dashboard_overlay_records(overlay)
    if records is None:
        return None
    require_edges = not _dashboard_overlay_matches_known_release(overlay)
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return _dashboard_index_uncovered_overlay_nodes(
                conn,
                records,
                require_edges=require_edges,
            )
        finally:
            conn.close()
    except (OSError, sqlite3.Error, KeyError, TypeError):
        return None


def _dashboard_index_covers_runtime_overlays(index_path: Path) -> bool:
    """Return True when the shipped SQLite index already includes overlays.

    ``ctx-init`` may install ``entity-overlays.jsonl`` beside a graph export.
    Older dashboard code treated any overlay file as runtime-only and skipped
    the SQLite fast path. Current release artifacts can already contain those
    overlay nodes and edges in ``dashboard-neighborhoods.sqlite3``; in that
    case loading the full graph just to merge the same small known-release
    overlay is wasted cold-start work. Local/user overlays still fall back to
    the full graph merge so newly attached edges remain visible.
    """
    overlay = _wiki_dir() / "graphify-out" / "entity-overlays.jsonl"
    try:
        if not overlay.is_file() or overlay.stat().st_size == 0:
            return True
    except OSError:
        return True
    if not index_path.is_file() or not _dashboard_index_matches_manifest(index_path):
        return False

    global _OVERLAY_INDEX_COVERAGE_CACHE_KEY, _OVERLAY_INDEX_COVERAGE_CACHE_VALUE
    cache_key = _overlay_index_coverage_key(index_path, overlay)
    if (
        cache_key is not None
        and _OVERLAY_INDEX_COVERAGE_CACHE_KEY == cache_key
        and _OVERLAY_INDEX_COVERAGE_CACHE_VALUE is not None
    ):
        return _OVERLAY_INDEX_COVERAGE_CACHE_VALUE

    uncovered = _dashboard_uncovered_runtime_overlay_nodes(index_path)
    coverage = uncovered == set()

    if cache_key is not None:
        _OVERLAY_INDEX_COVERAGE_CACHE_KEY = cache_key
        _OVERLAY_INDEX_COVERAGE_CACHE_VALUE = coverage
    return coverage


def _dashboard_graph_index_archives() -> list[Path]:
    module_root = Path(__file__).resolve().parent.parent
    roots = (module_root,)
    names = ("wiki-graph-runtime.tar.gz", "wiki-graph.tar.gz")
    seen: set[Path] = set()
    archives: list[Path] = []
    for root in roots:
        for name in names:
            candidate = (root / "graph" / name).resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file():
                archives.append(candidate)
    return archives


def _packaged_graph_export_id() -> str | None:
    global _PACKAGED_GRAPH_EXPORT_ID_CACHE
    if isinstance(_PACKAGED_GRAPH_EXPORT_ID_CACHE, bool):
        return None
    if isinstance(_PACKAGED_GRAPH_EXPORT_ID_CACHE, str):
        return _PACKAGED_GRAPH_EXPORT_ID_CACHE
    module_root = Path(__file__).resolve().parent.parent
    try:
        data = json.loads(
            (module_root / "graph" / "communities.json").read_text(
                encoding="utf-8",
            )
        )
    except (OSError, json.JSONDecodeError):
        _PACKAGED_GRAPH_EXPORT_ID_CACHE = False
        return None
    export_id = data.get("export_id") if isinstance(data, dict) else None
    if isinstance(export_id, str) and export_id.strip():
        _PACKAGED_GRAPH_EXPORT_ID_CACHE = export_id.strip()
        return export_id.strip()
    _PACKAGED_GRAPH_EXPORT_ID_CACHE = False
    return None


def _archive_graph_export_id(archive: Path) -> str | None:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                member = tar.getmember("./graphify-out/graph-export-manifest.json")
            except KeyError:
                member = tar.getmember("graphify-out/graph-export-manifest.json")
            source = tar.extractfile(member)
            if source is None:
                return None
            try:
                data = json.loads(source.read().decode("utf-8", errors="replace"))
            finally:
                source.close()
    except (KeyError, OSError, tarfile.TarError, json.JSONDecodeError):
        return None
    export_id = data.get("export_id") if isinstance(data, dict) else None
    return export_id.strip() if isinstance(export_id, str) and export_id.strip() else None


def _ensure_dashboard_graph_index() -> Path | None:
    target = _dashboard_graph_index_path()
    if target.is_file():
        if _dashboard_index_matches_manifest(target):
            return target
        try:
            target.unlink()
        except OSError:
            return None

    manifest_export_id = _dashboard_graph_manifest_export_id()
    packaged_export_id = _packaged_graph_export_id()
    if (
        manifest_export_id is not None
        and packaged_export_id is not None
        and manifest_export_id != packaged_export_id
    ):
        return None

    archives = _dashboard_graph_index_archives()
    if not archives:
        return None
    if manifest_export_id is None:
        return None

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(target):
            if target.is_file():
                if _dashboard_index_matches_manifest(target):
                    return target
                try:
                    target.unlink()
                except OSError:
                    return None
            for archive in archives:
                archive_export_id = packaged_export_id or _archive_graph_export_id(archive)
                if manifest_export_id and archive_export_id and archive_export_id != manifest_export_id:
                    continue
                try:
                    with tarfile.open(archive, "r:gz") as tar:
                        try:
                            member = tar.getmember(f"./{_DASHBOARD_INDEX_MEMBER}")
                        except KeyError:
                            member = tar.getmember(_DASHBOARD_INDEX_MEMBER)
                        if not member.isfile():
                            continue
                        source = tar.extractfile(member)
                        if source is None:
                            continue
                        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
                        try:
                            with tmp.open("wb") as out:
                                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                    out.write(chunk)
                            if not _dashboard_index_matches_manifest(tmp):
                                continue
                            os.replace(tmp, target)
                            return target
                        finally:
                            source.close()
                            if tmp.exists():
                                tmp.unlink()
                except (KeyError, OSError, tarfile.TarError):
                    continue
    except TimeoutError:
        return None
    return target if target.is_file() else None


def _index_node_size(
    *,
    slug: str,
    entity_type: str,
    quality: Any,
    usage: Any,
    degree: int,
    max_degree: int,
) -> dict[str, Any]:
    quality_value = _unit_score(quality)
    usage_value = _unit_score(usage)
    if quality_value is None or usage_value is None:
        sidecar_quality, sidecar_usage = _sidecar_score_inputs(slug, entity_type)
        quality_value = quality_value if quality_value is not None else sidecar_quality
        usage_value = usage_value if usage_value is not None else sidecar_usage
    q = 0.35 if quality_value is None else quality_value
    u = 0.0 if usage_value is None else usage_value
    popularity = (
        math.log1p(max(0, degree)) / math.log1p(max(1, max_degree))
        if max_degree > 0
        else 0.0
    )
    signal = max(0.0, min(1.0, 0.45 * q + 0.35 * u + 0.20 * popularity))
    return {
        "node_size": round(8.0 + signal * 16.0, 2),
        "size_signal": round(signal, 4),
        "size_reason": f"quality {q:.3f}; usage {u:.3f}; popularity {popularity:.3f}",
    }


def _resolve_index_center(
    conn: sqlite3.Connection,
    raw_query: str,
    entity_type: str | None,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    raw_query = str(raw_query or "").strip()
    if not raw_query or "/" in raw_query or "\\" in raw_query or ".." in raw_query:
        return None, None, []
    normalized_query = _slugish(raw_query)
    if not normalized_query or not _is_safe_slug(normalized_query):
        return None, None, []

    entity_types = (
        (entity_type,)
        if entity_type is not None
        else _DASHBOARD_ENTITY_TYPES
    )
    candidates = []
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
    if entity_type is not None:
        where = "WHERE s.type=?"
        params.append(entity_type)
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
        haystacks = {_slugish(node_slug), _slugish(_display_slug(node_slug)), _slugish(label)}
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
        elif query_tokens and all(
            any(token in h for h in haystacks) for token in query_tokens
        ):
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
        {"query": raw_query, "slug": _graph_slug_from_node_id(center), "id": center},
        suggestions,
    )


def _graph_neighborhood_from_index(
    slug: str,
    *,
    hops: int,
    limit: int,
    entity_type: str | None,
) -> dict | None:
    index_path = _ensure_dashboard_graph_index()
    if index_path is None or not index_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    try:
        meta = {
            row["key"]: json.loads(row["value"])
            for row in conn.execute("SELECT key,value FROM meta")
        }
        max_degree = int(meta.get("max_degree") or 1)
        top_k = int(meta.get("top_k") or 0)
        if hops > 1 or (top_k > 0 and limit > top_k):
            return None

        center, resolved, suggestions = _resolve_index_center(conn, slug, entity_type)
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
            node_type = str(row["type"] or _graph_type_from_node_id(node_id))
            node_slug = _graph_slug_from_node_id(node_id)
            size_data = _index_node_size(
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
                    **_dashboard_score_payload("quality_score", row["quality_score"]),
                    **_dashboard_score_payload("usage_score", row["usage_score"]),
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
                                "filter_tokens", []
                            )
                            tokens.extend(shared_tags)
                        edges_out.append({
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
                        })
                    if other not in seen:
                        seen.add(other)
                        next_frontier.append(other)
                if len(nodes_out) >= limit:
                    break
            frontier = next_frontier
            if len(nodes_out) >= limit:
                break
        return dashboard_graph.enrich_neighborhood({
            "nodes": list(nodes_out.values()),
            "edges": edges_out,
            "center": center,
            "resolved": resolved or {"source": "dashboard-index"},
            "suggestions": [],
        }, source="dashboard-index")
    except (OSError, sqlite3.Error, json.JSONDecodeError, zlib.error, KeyError, TypeError):
        return None
    finally:
        conn.close()


def _graph_neighborhood_from_store(
    slug: str,
    *,
    hops: int,
    limit: int,
    entity_type: str | None,
) -> dict | None:
    if hops > 1:
        return None
    graph_dir = _wiki_dir() / "graphify-out"
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
        center, resolved, suggestions = _resolve_graph_store_center(
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
    return _dashboard_payload_from_graph_store(
        center=center,
        resolved=resolved or {"source": "graph-store"},
        suggestions=suggestions,
        neighborhood=neighborhood,
    )


def _resolve_graph_store_center(
    store_path: Path,
    raw_query: str,
    entity_type: str | None,
    search_nodes: Any,
) -> tuple[str | None, dict[str, str] | None, list[str]]:
    raw_query = str(raw_query or "").strip()
    if not raw_query or "/" in raw_query or "\\" in raw_query or ".." in raw_query:
        return None, None, []
    normalized_query = _slugish(raw_query)
    if not normalized_query or not _is_safe_slug(normalized_query):
        return None, None, []

    entity_types = (
        (entity_type,)
        if entity_type is not None
        else _DASHBOARD_ENTITY_TYPES
    )
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
        node_slug = _graph_slug_from_node_id(node_id)
        display_suggestion = _display_slug(node_slug)
        if display_suggestion not in suggestions:
            suggestions.append(display_suggestion)

    matches: list[tuple[tuple[int, int], str, str]] = []
    for row in rows:
        node_id = str(row.get("id") or "")
        node_type = str(row.get("type") or _graph_type_from_node_id(node_id))
        if node_type not in entity_types:
            continue
        node_slug = _graph_slug_from_node_id(node_id)
        label = _display_label(row.get("label"), fallback_slug=node_slug)
        haystacks = {_slugish(node_slug), _slugish(_display_slug(node_slug)), _slugish(label)}
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
    resolved_slug = _graph_slug_from_node_id(center)
    return center, {"query": raw_query, "slug": resolved_slug, "id": center}, suggestions


def _dashboard_payload_from_graph_store(
    *,
    center: str,
    resolved: dict[str, str],
    suggestions: list[str],
    neighborhood: dict[str, list[dict[str, Any]]],
) -> dict:
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
        node_slug = _graph_slug_from_node_id(node_id)
        node_type = str(node.get("type") or _graph_type_from_node_id(node_id))
        tags = [str(tag) for tag in node.get("tags", []) if isinstance(tag, str)]
        label = _display_label(node.get("label"), fallback_slug=node_slug)
        degree = degree_by_node.get(node_id, 0)
        size_data = _graph_node_size(
            node_id,
            {},
            entity_type=node_type,
            degree=degree,
            max_degree=max_degree,
        )
        nodes_out.append({
            "data": {
                "id": node_id,
                "label": label,
                "type": node_type,
                "depth": 0 if node_id == center else 1,
                "degree": degree,
                "tags": tags[:6],
                "description": "",
                **_dashboard_score_payload("quality_score", None),
                **_dashboard_score_payload("usage_score", None),
                "filter_tokens": [
                    node_id,
                    label,
                    node_slug,
                    _display_slug(node_slug),
                    *tags,
                ],
                **size_data,
            },
        })

    edges_out: list[dict[str, Any]] = []
    for edge in raw_edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        raw_attrs = edge.get("attrs")
        attrs: dict[str, Any] = raw_attrs if isinstance(raw_attrs, dict) else {}
        edge_key = tuple(sorted((source, target)))
        raw_shared_tags = attrs.get("shared_tags")
        shared_tags = (
            [str(tag) for tag in raw_shared_tags[:4]]
            if isinstance(raw_shared_tags, list)
            else []
        )
        raw_reasons = attrs.get("reasons")
        reasons = (
            [str(reason) for reason in raw_reasons]
            if isinstance(raw_reasons, list)
            else []
        )
        edges_out.append({
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
        })

    return dashboard_graph.enrich_neighborhood({
        "nodes": nodes_out,
        "edges": edges_out,
        "center": center,
        "resolved": resolved,
        "suggestions": suggestions,
    }, source="graph-store")


def _graph_neighborhood(
    slug: str,
    hops: int = 1,
    limit: int = 40,
    entity_type: str | None = None,
) -> dict:
    """Return dashboard-shaped {nodes, edges} for the N-hop neighborhood.

    Uses ``resolve_graph.load_graph`` so the NetworkX 'links' vs 'edges'
    schema is handled centrally. Returns an empty shape if the graph
    hasn't been built or the slug isn't a node.
    """
    if "/" in slug or "\\" in slug or ".." in slug:
        return {"nodes": [], "edges": [], "center": None}
    normalized_entity_type = _normalize_dashboard_entity_type(entity_type)
    stored = _graph_neighborhood_from_store(
        slug,
        hops=hops,
        limit=limit,
        entity_type=normalized_entity_type,
    )
    if stored is not None:
        return stored
    index_path = _dashboard_graph_index_path()
    has_runtime_overlays = _dashboard_graph_has_runtime_overlays()
    index_covers_overlays = (
        not has_runtime_overlays
        or _dashboard_index_covers_runtime_overlays(index_path)
    )
    if index_covers_overlays:
        indexed = _graph_neighborhood_from_index(
            slug,
            hops=hops,
            limit=limit,
            entity_type=normalized_entity_type,
        )
        if indexed is not None:
            return indexed
    elif hops == 1 and index_path.is_file() and _dashboard_index_matches_manifest(index_path):
        indexed = _graph_neighborhood_from_index(
            slug,
            hops=hops,
            limit=limit,
            entity_type=normalized_entity_type,
        )
        center = indexed.get("center") if isinstance(indexed, dict) else None
        uncovered = _dashboard_uncovered_runtime_overlay_nodes(index_path)
        if indexed is not None and isinstance(center, str) and uncovered is not None:
            if center not in uncovered:
                return indexed
    try:
        G = _load_dashboard_graph()
    except Exception:  # noqa: BLE001 — graph is advisory; blank on error
        return {"nodes": [], "edges": [], "center": None}
    if G.number_of_nodes() == 0:
        return {"nodes": [], "edges": [], "center": None}

    center = None
    if entity_type is not None and normalized_entity_type is None:
        return {"nodes": [], "edges": [], "center": None}
    center, resolved, suggestions = _resolve_graph_center(
        G, slug, normalized_entity_type,
    )
    if center is None:
        return {"nodes": [], "edges": [], "center": None}

    nodes_out: dict[str, dict] = {}
    edges_out: list[dict] = []
    emitted_edges: set[tuple[str, str]] = set()
    frontier = [center]
    seen: set[str] = {center}
    try:
        max_degree = max((int(degree) for _node, degree in G.degree()), default=1)
    except Exception:  # noqa: BLE001
        max_degree = 1

    def _add_node(nid: str, depth: int) -> None:
        if nid in nodes_out:
            return
        data = dict(G.nodes.get(nid, {}))
        node_slug = nid.split(":", 1)[-1]
        label = _display_label(data.get("label"), fallback_slug=node_slug)
        tags = list(data.get("tags", []))
        default_type = (
            "mcp-server" if nid.startswith("mcp-server:")
            else "harness" if nid.startswith("harness:")
            else "agent" if nid.startswith("agent:")
            else "skill"
        )
        ntype = data.get("type") or default_type
        try:
            degree = int(G.degree[nid])
        except Exception:  # noqa: BLE001
            degree = 0
        size_data = _graph_node_size(
            nid,
            data,
            entity_type=str(ntype),
            degree=degree,
            max_degree=max_degree,
        )
        nodes_out[nid] = {
            "data": {
                "id": nid,
                "label": label,
                "type": ntype,
                "depth": depth,
                "degree": degree,
                "tags": tags[:6],
                "description": data.get("description", ""),
                **_dashboard_score_payload("quality_score", data.get("quality_score")),
                **_dashboard_score_payload("usage_score", data.get("usage_score")),
                "filter_tokens": [nid, label, node_slug, _display_slug(node_slug), *tags],
                **size_data,
            },
        }

    _add_node(center, 0)

    for depth in range(1, hops + 1):
        next_frontier: list[str] = []
        for nid in frontier:
            # Sort neighbors by edge weight so we pick the strongest
            # connections first under the ``limit`` cap.
            neighbors = sorted(
                G[nid].items(),
                key=lambda kv: -kv[1].get("weight", 1),
            )
            for other, edata in neighbors:
                if len(nodes_out) >= limit:
                    break
                _add_node(other, depth)
                edge_key = tuple(sorted((nid, other)))
                if edge_key not in emitted_edges:
                    emitted_edges.add(edge_key)
                    shared_tags = edata.get("shared_tags", [])[:4]
                    for node_id in (nid, other):
                        tokens = nodes_out[node_id]["data"].setdefault(
                            "filter_tokens", []
                        )
                        tokens.extend(shared_tags)
                    edges_out.append({
                        "data": {
                            "id": f"{edge_key[0]}__{edge_key[1]}",
                            "source": nid,
                            "target": other,
                            "weight": edata.get("weight", 1),
                            "shared_tags": shared_tags,
                            "reasons": edata.get("reasons", []),
                            "semantic": edata.get("semantic"),
                            "tag_sim": edata.get("tag_sim"),
                            "slug_token_sim": edata.get("slug_token_sim"),
                            "source_overlap": edata.get("source_overlap"),
                        },
                    })
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
            if len(nodes_out) >= limit:
                break
        frontier = next_frontier
        if len(nodes_out) >= limit:
            break

    return dashboard_graph.enrich_neighborhood({
        "nodes": list(nodes_out.values()),
        "edges": edges_out,
        "center": center,
        "resolved": resolved,
        "suggestions": suggestions,
    }, source="networkx")


def _graph_stats() -> dict:
    """Top-line graph stats for the home page."""
    report = _wiki_dir() / "graphify-out" / "graph-report.md"
    try:
        match = _GRAPH_REPORT_RE.search(
            report.read_text(encoding="utf-8", errors="replace"),
        )
        if match:
            return {
                "nodes": int(match.group(1).replace(",", "")),
                "edges": int(match.group(2).replace(",", "")),
                "available": True,
            }
    except OSError:
        pass

    index_path = _ensure_dashboard_graph_index()
    if index_path is not None and index_path.is_file():
        try:
            conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
            try:
                meta = {
                    row[0]: json.loads(row[1])
                    for row in conn.execute("SELECT key,value FROM meta")
                }
                return {
                    "nodes": int(meta.get("nodes_count") or 0),
                    "edges": int(meta.get("edges_count") or 0),
                    "available": int(meta.get("nodes_count") or 0) > 0,
                }
            finally:
                conn.close()
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            pass
    try:
        G = _load_dashboard_graph()
    except Exception:  # noqa: BLE001
        return {"nodes": 0, "edges": 0, "available": False}
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "available": G.number_of_nodes() > 0,
    }


def _wiki_stats_from_dashboard_index() -> dict[str, int] | None:
    index_path = _dashboard_graph_index_path()
    if not index_path.is_file() or not _dashboard_index_matches_manifest(index_path):
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

    stats = {
        "skills": rows.get("skill", 0),
        "agents": rows.get("agent", 0),
        "mcps": rows.get("mcp-server", 0),
        "harnesses": rows.get("harness", 0),
    }
    stats["total"] = sum(stats.values())
    stats["split_known"] = True
    return stats


def _wiki_stats() -> dict:
    """Entity counts across all dashboard-supported entity types.

    MCPs are sharded by first-char under ``entities/mcp-servers/<shard>/``
    so we recurse rather than the flat glob used for skills + agents.
    Home page consumes ``total`` for the headline number and the
    individual counts for the dashboard entity-type detail
    line.
    """
    indexed = _wiki_stats_from_dashboard_index()
    if indexed is not None:
        return indexed

    if _wiki_pack_pages() is not None:
        stats = {"skills": 0, "agents": 0, "mcps": 0, "harnesses": 0}
        for _slug, entity_type, _path in _iter_wiki_entity_paths():
            if entity_type == "skill":
                stats["skills"] += 1
            elif entity_type == "agent":
                stats["agents"] += 1
            elif entity_type == "mcp-server":
                stats["mcps"] += 1
            elif entity_type == "harness":
                stats["harnesses"] += 1
        stats["total"] = sum(stats.values())
        stats["split_known"] = True
        return stats

    base = _wiki_dir() / "entities"
    graph_out = _wiki_dir() / "graphify-out"
    if graph_out.is_dir() and (graph_out / "graph-report.md").is_file():
        graph_stats = _graph_stats()
        return {
            "skills": 0,
            "agents": 0,
            "mcps": 0,
            "harnesses": 0,
            "total": int(graph_stats.get("nodes") or 0),
            "split_known": False,
        }
    skills = len(list((base / "skills").glob("*.md"))) if (base / "skills").is_dir() else 0
    agents = len(list((base / "agents").glob("*.md"))) if (base / "agents").is_dir() else 0
    mcp_dir = base / "mcp-servers"
    mcps = len(list(mcp_dir.rglob("*.md"))) if mcp_dir.is_dir() else 0
    harnesses = len(list((base / "harnesses").glob("*.md"))) if (base / "harnesses").is_dir() else 0
    return {
        "skills": skills,
        "agents": agents,
        "mcps": mcps,
        "harnesses": harnesses,
        "total": skills + agents + mcps + harnesses,
        "split_known": True,
    }


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
    if (
        _dashboard_graph_has_runtime_overlays()
        and not _dashboard_index_covers_runtime_overlays(index_path)
    ):
        return []
    ensured_index_path = _ensure_dashboard_graph_index()
    if ensured_index_path is None or not ensured_index_path.is_file():
        return []
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"file:{ensured_index_path.as_posix()}?mode=ro", uri=True)
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
            "slug": _graph_slug_from_node_id(str(row["id"])),
            "type": _graph_type_from_node_id(str(row["id"]), str(row["type"] or "skill")),
            "degree": int(row["degree"] or 0),
            "label": row["label"] or _graph_slug_from_node_id(str(row["id"])),
        }
        for row in rows
    ]


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
    if G.number_of_nodes() == 0:
        return []
    ranked = sorted(G.degree, key=lambda kv: -kv[1])[:limit]
    out: list[dict] = []
    for node_id, degree in ranked:
        prefix, _, slug = node_id.partition(":")
        seed_type = (
            "mcp-server" if prefix == "mcp-server"
            else "harness" if prefix == "harness"
            else "agent" if prefix == "agent"
            else "skill"
        )
        out.append({
            "slug": slug,
            "type": seed_type,
            "degree": int(degree),
            "label": G.nodes[node_id].get("label", slug),
        })
    return out


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
    center = str(graph.get("center") or "")
    if not center:
        return None
    for node in graph.get("nodes", []):
        if not isinstance(node, dict):
            continue
        data = node.get("data", {})
        if isinstance(data, dict) and str(data.get("id") or "") == center:
            return data
    return None


def _runtime_graph_metric_row(label: str, value: object) -> str:
    if value is None or value == "":
        value_html = "<span class='muted'>unknown</span>"
    elif isinstance(value, float):
        value_html = f"<code>{value:.3f}</code>"
    else:
        value_html = f"<code>{html.escape(str(value))}</code>"
    return f"<tr><td class='muted'>{html.escape(label)}</td><td>{value_html}</td></tr>"


def _render_runtime_entity_action(
    slug: str,
    entity_type: str,
    *,
    mutations_enabled: bool,
) -> str:
    escaped_slug = html.escape(slug)
    escaped_type = html.escape(entity_type)
    if entity_type == "harness":
        return (
            "<div class='card'>"
            "<h2>Install harness</h2>"
            "<p class='muted'>Harnesses are installed through the harness CLI so ctx can "
            "collect the model, goal, and verification details before wiring recommendations.</p>"
            f"<pre><code>ctx-harness-install {escaped_slug} --dry-run\n"
            f"ctx-harness-install {escaped_slug}</code></pre>"
            "</div>"
        )

    disabled = " disabled" if not mutations_enabled else ""
    disabled_note = (
        "<p class='muted'>Load/install actions are disabled because this dashboard is not "
        "bound to loopback.</p>"
        if not mutations_enabled
        else ""
    )
    return (
        "<div class='card'>"
        "<h2>Load or install</h2>"
        "<p class='muted'>Use this when the backing wiki contains the installable entity. "
        "If runtime mode only installed graph metadata, install the full wiki first.</p>"
        f"<button type='button' class='action-btn' data-testid='runtime-entity-load' "
        f"data-runtime-slug='{escaped_slug}' data-runtime-type='{escaped_type}'{disabled}>"
        "Load / install from current wiki</button>"
        f"{disabled_note}"
        "<p id='runtime-entity-load-result' class='muted'></p>"
        "</div>"
    )


def _render_runtime_entity_load_script(
    slug: str,
    entity_type: str,
    *,
    mutations_enabled: bool,
) -> str:
    return (
        "<script>\n"
        f"const CTX_RUNTIME_ENTITY_MUTATIONS_ENABLED = {json.dumps(mutations_enabled)};\n"
        f"const CTX_RUNTIME_ENTITY_TOKEN = {json.dumps(_MONITOR_TOKEN if mutations_enabled else '')};\n"
        f"const CTX_RUNTIME_ENTITY_SLUG = {json.dumps(slug)};\n"
        f"const CTX_RUNTIME_ENTITY_TYPE = {json.dumps(entity_type)};\n"
        "document.querySelectorAll('[data-testid=\"runtime-entity-load\"]').forEach(function (button) {\n"
        "  button.addEventListener('click', async function () {\n"
        "    const result = document.getElementById('runtime-entity-load-result');\n"
        "    if (!CTX_RUNTIME_ENTITY_MUTATIONS_ENABLED) {\n"
        "      if (result) result.textContent = 'mutations disabled on non-loopback bind';\n"
        "      return;\n"
        "    }\n"
        "    button.disabled = true;\n"
        "    if (result) result.textContent = 'loading...';\n"
        "    try {\n"
        "      const response = await fetch('/api/load', {\n"
        "        method: 'POST',\n"
        "        headers: {'Content-Type': 'application/json', 'X-CTX-Monitor-Token': CTX_RUNTIME_ENTITY_TOKEN},\n"
        "        body: JSON.stringify({slug: CTX_RUNTIME_ENTITY_SLUG, entity_type: CTX_RUNTIME_ENTITY_TYPE})\n"
        "      });\n"
        "      const payload = await response.json();\n"
        "      const message = payload.detail || payload.msg || response.status;\n"
        "      if (result) result.textContent = (payload.ok ? 'loaded: ' : 'not loaded: ') + message;\n"
        "    } catch (error) {\n"
        "      if (result) result.textContent = 'load failed: ' + error;\n"
        "    } finally {\n"
        "      button.disabled = false;\n"
        "    }\n"
        "  });\n"
        "});\n"
        "</script>"
    )


def _render_runtime_graph_entity(
    slug: str,
    entity_type: str | None = None,
    *,
    mutations_enabled: bool | None = None,
) -> str | None:
    """Render graph metadata when the fast runtime graph lacks a full wiki page."""
    normalized_type = _normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized_type is None:
        return None
    graph = _graph_neighborhood(slug, hops=1, limit=32, entity_type=normalized_type)
    data = _runtime_graph_center_data(graph)
    if data is None:
        return None

    node_id = str(data.get("id") or graph.get("center") or "")
    resolved_slug = _graph_slug_from_node_id(node_id) or slug
    resolved_type = _graph_type_from_node_id(
        node_id,
        str(data.get("type") or normalized_type or "skill"),
    )
    label = _display_label(data.get("label"), fallback_slug=resolved_slug)
    display_slug = _display_slug(resolved_slug)
    description = str(data.get("description") or "").strip()
    tags = [str(tag) for tag in data.get("tags", []) if str(tag).strip()][:12]
    sidecar = _load_sidecar(resolved_slug, entity_type=resolved_type)
    quality_score = data.get("quality_score")
    usage_score = data.get("usage_score")
    degree = data.get("degree")
    mutations = _MONITOR_MUTATIONS_ENABLED if mutations_enabled is None else mutations_enabled

    quality_html = (
        _render_quality_drilldown(sidecar)
        if isinstance(sidecar, dict)
        else (
            "<div class='card'>"
            "<h2>Runtime graph quality</h2>"
            "<p class='muted'>No full quality sidecar is installed for this entity. "
            "The runtime graph still exposes the ranking signals available at graph build time.</p>"
            "<table class='frontmatter-table'><tr><th>Signal</th><th>Value</th></tr>"
            + _runtime_graph_metric_row("quality_score", quality_score)
            + _runtime_graph_metric_row("usage_score", usage_score)
            + _runtime_graph_metric_row("degree", degree)
            + "</table></div>"
        )
    )
    return _wiki_page.render_runtime_graph_entity_page(
        label=label,
        node_id=node_id,
        resolved_slug=resolved_slug,
        resolved_type=resolved_type,
        display_slug=display_slug,
        description=description,
        tags=tags,
        quality_score=quality_score,
        usage_score=usage_score,
        degree=degree,
        quality_html=quality_html,
        subgraph_html=_render_entity_subgraph(resolved_slug, entity_type=resolved_type),
        action_html=_render_runtime_entity_action(
            resolved_slug,
            resolved_type,
            mutations_enabled=mutations,
        ),
        load_script_html=_render_runtime_entity_load_script(
            resolved_slug,
            resolved_type,
            mutations_enabled=mutations,
        ),
        runtime_graph_metric_row=_runtime_graph_metric_row,
        render_entity_tabs=_render_entity_tabs,
        layout=_layout,
    )


def _render_wiki_entity(
    slug: str,
    entity_type: str | None = None,
    *,
    mutations_enabled: bool | None = None,
) -> str:
    """Render one wiki entity page (frontmatter + body)."""
    path = _wiki_entity_path(slug, entity_type=entity_type)
    if path is None:
        runtime_html = _render_runtime_graph_entity(
            slug,
            entity_type=entity_type,
            mutations_enabled=mutations_enabled,
        )
        if runtime_html is not None:
            return runtime_html
        return _layout(
            slug,
            f"<h1>{html.escape(slug)}</h1>"
            f"<p class='muted'>No wiki page found for <code>{html.escape(slug)}</code>. "
            f"Try <a href='/skills'>the skills index</a>.</p>",
        )
    raw = _read_wiki_entity_text(slug, entity_type, path)
    if raw is None:
        return _layout(
            slug,
            f"<h1>{html.escape(slug)}</h1><p class='muted'>read error: page unavailable</p>",
        )
    meta, md_body = _parse_frontmatter(raw)
    sidecar = _load_sidecar(slug, entity_type=entity_type)
    return _wiki_page.render_wiki_entity_page(
        slug=slug,
        entity_type=entity_type,
        meta=meta,
        md_body=md_body,
        sidecar=sidecar if isinstance(sidecar, dict) else None,
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
    indexed = _wiki_index_entries_from_dashboard_index(limit_per_type)
    if indexed is not None:
        return indexed

    paths = _iter_wiki_entity_paths()
    if not paths:
        return []
    # Preserve per-type sampling order while reading from the merged wiki view.
    sources = _DASHBOARD_ENTITY_SOURCES
    out: list[dict] = []
    for _sub, entity_type, _recursive in sources:
        seen_for_type = 0
        for slug, current_type, path in paths:
            if current_type != entity_type:
                continue
            if limit_per_type is not None and seen_for_type >= limit_per_type:
                break
            text = _read_wiki_entity_text(slug, current_type, path)
            if text is None:
                continue
            # Read only the first ~2 KB - enough for frontmatter.
            head = text[:2048]
            meta, _ = _parse_frontmatter(head)
            all_tags = _frontmatter_tags(meta.get("tags", ""), limit=None)
            description, _truncated = _truncate_text(
                _frontmatter_text(meta.get("description", "")),
                200,
            )
            out.append({
                "slug": slug,
                "display_slug": _display_slug(slug),
                "type": entity_type,
                "tags": all_tags[:6],
                "search_tags": all_tags,
                "description": description,
            })
            seen_for_type += 1
    return out


def _wiki_index_entries_from_dashboard_index(
    limit_per_type: int | None,
) -> list[dict] | None:
    index_path = _dashboard_graph_index_path()
    if not index_path.is_file() or not _dashboard_index_matches_manifest(index_path):
        return None

    out: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{index_path.as_posix()}?mode=ro", uri=True)
        try:
            for _sub, entity_type, _recursive in _DASHBOARD_ENTITY_SOURCES:
                params: list[Any] = [entity_type]
                limit_sql = ""
                if limit_per_type is not None:
                    limit_sql = " LIMIT ?"
                    params.append(max(0, int(limit_per_type)))
                rows = conn.execute(
                    "SELECT id,label,type,tags,description,quality_score FROM nodes "
                    "WHERE type=? ORDER BY lower(label), id" + limit_sql,
                    params,
                )
                for (
                    node_id,
                    label,
                    row_type,
                    tags_raw,
                    description_raw,
                    quality_score,
                ) in rows:
                    node_id_text = str(node_id)
                    slug = (
                        node_id_text.split(":", 1)[1]
                        if ":" in node_id_text
                        else str(label)
                    )
                    if not _is_safe_slug(slug):
                        continue
                    try:
                        parsed_tags = json.loads(str(tags_raw or "[]"))
                    except json.JSONDecodeError:
                        parsed_tags = []
                    all_tags = [
                        str(tag) for tag in parsed_tags
                        if isinstance(tag, str)
                    ]
                    description, _truncated = _truncate_text(
                        _frontmatter_text(description_raw),
                        200,
                    )
                    out.append({
                        "slug": slug,
                        "display_slug": _display_slug(str(label or slug)),
                        "type": str(row_type or entity_type),
                        "tags": all_tags[:6],
                        "search_tags": all_tags,
                        "description": description,
                        "grade": _grade_from_quality_score(quality_score),
                    })
        finally:
            conn.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None
    return out


def _grade_from_quality_score(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return ""
    if score >= 0.80:
        return "A"
    if score >= 0.60:
        return "B"
    if score >= 0.40:
        return "C"
    if score >= 0.0:
        return "D"
    return ""


def _wiki_render_cache_key(
    selected_type: str | None,
    query: str,
) -> tuple[Any, ...] | None:
    index_path = _dashboard_graph_index_path()
    if not index_path.is_file() or not _dashboard_index_matches_manifest(index_path):
        return None
    try:
        index_stat = index_path.stat()
        source_stat = Path(__file__).stat()
    except OSError:
        return None
    try:
        css_hash = hashlib.sha256(
            _monitor_asset_text("monitor.css").encode("utf-8")
        ).hexdigest()
    except Exception:
        css_hash = ""
    return (
        "wiki-index-v1",
        selected_type or "",
        query,
        str(index_path.resolve()),
        index_stat.st_mtime_ns,
        index_stat.st_size,
        _dashboard_graph_manifest_export_id() or "",
        source_stat.st_mtime_ns,
        source_stat.st_size,
        css_hash,
    )


def _disk_cache_token(cache_key: tuple[Any, ...]) -> str:
    return json.dumps(cache_key, separators=(",", ":"), sort_keys=True)


def _read_disk_cache_payload(path: Path, cache_token: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != 1 or data.get("cache_token") != cache_token:
        return None
    return data


def _write_disk_cache_payload(
    path: Path,
    cache_token: str,
    payload: dict[str, Any],
    *,
    sort_keys: bool = False,
) -> None:
    try:
        _atomic_write_text(
            path,
            json.dumps(
                {
                    "schema_version": 1,
                    "cache_token": cache_token,
                    **payload,
                },
                ensure_ascii=False,
                sort_keys=sort_keys,
            ) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        return


def _read_html_disk_cache(path: Path, cache_token: str) -> str | None:
    data = _read_disk_cache_payload(path, cache_token)
    if data is None:
        return None
    html_text = data.get("html")
    return html_text if isinstance(html_text, str) else None


def _write_html_disk_cache(path: Path, cache_token: str, html_text: str) -> None:
    _write_disk_cache_payload(path, cache_token, {"html": html_text})


def _wiki_render_disk_cache_path() -> Path:
    return _claude_dir() / ".ctx-monitor-wiki-cache.json"


def _render_wiki_index(entity_type: str | None = None, query: str = "") -> str:
    """Card grid of every wiki entity — search + type filter + sidecar grades."""
    selected_type = _normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and selected_type is None:
        return _layout(
            "Wiki",
            f"<div class='error'>Unsupported entity type: {html.escape(entity_type)}</div>",
        )
    initial_query = query.strip()
    cache_key = _wiki_render_cache_key(selected_type, initial_query)
    global _WIKI_RENDER_CACHE_KEY, _WIKI_RENDER_CACHE_VALUE
    if cache_key is not None:
        if _WIKI_RENDER_CACHE_KEY == cache_key and _WIKI_RENDER_CACHE_VALUE is not None:
            return _WIKI_RENDER_CACHE_VALUE
        cache_token = _disk_cache_token(cache_key)
        cached = _read_html_disk_cache(_wiki_render_disk_cache_path(), cache_token)
        if cached is not None:
            _WIKI_RENDER_CACHE_KEY = cache_key
            _WIKI_RENDER_CACHE_VALUE = cached
            return cached
    else:
        cache_token = ""
    entries = _wiki_index_entries()
    wstats = _wiki_stats()
    total_available = int(wstats.get("total") or len(entries))
    # Join with grade pills where a sidecar exists.
    grade_by_key: dict[tuple[str, str], str] = {}
    for entry in entries:
        slug = str(entry["slug"])
        row_type = str(entry["type"])
        grade = str(entry.get("grade") or "")
        if grade:
            grade_by_key[(slug, row_type)] = grade
            continue
        sidecar = _load_sidecar(slug, entity_type=row_type)
        if sidecar:
            grade_by_key[(slug, row_type)] = str(sidecar.get("grade") or "")

    type_counts = {
        "skill": int(wstats.get("skills") or 0),
        "agent": int(wstats.get("agents") or 0),
        "mcp-server": int(wstats.get("mcps") or 0),
        "harness": int(wstats.get("harnesses") or 0),
    }

    html_out = _wiki_page.render_wiki_index_page(
        entries=entries,
        selected_type=selected_type,
        initial_query=initial_query,
        total_available=total_available,
        type_counts=type_counts,
        grade_by_key=grade_by_key,
        dashboard_entity_types=_DASHBOARD_ENTITY_TYPES,
        layout=_layout,
    )
    if cache_key is not None:
        _write_html_disk_cache(_wiki_render_disk_cache_path(), cache_token, html_out)
        _WIKI_RENDER_CACHE_KEY = cache_key
        _WIKI_RENDER_CACHE_VALUE = html_out
    return html_out



def _docs_roots() -> list[Path]:
    return _docs_page.docs_roots(Path.cwd(), Path(__file__).resolve().parent.parent)


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
    """Return catalog harness pages for the manual dashboard wizard."""
    harness_dir = _wiki_dir() / "entities" / "harnesses"
    if not harness_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(harness_dir.glob("*.md"), key=lambda p: p.stem.lower()):
        if len(rows) >= limit:
            break
        slug = path.stem
        if not _is_safe_slug(slug):
            continue
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        meta, _body = _parse_frontmatter(head)
        sidecar = _harness_wizard_sidecar(slug) or {}
        score = float(sidecar.get("raw_score", sidecar.get("score", 0.0)) or 0.0)
        tags = _frontmatter_tags(meta.get("tags", ""), limit=None)
        description, _truncated = _truncate_text(
            _frontmatter_text(meta.get("description", "")),
            260,
        )
        repo_url = _frontmatter_text(
            meta.get("repo_url")
            or meta.get("github_url")
            or meta.get("homepage_url")
            or ""
        )
        rows.append({
            "slug": slug,
            "title": _frontmatter_text(meta.get("title") or meta.get("name") or slug),
            "description": description,
            "tags": tags[:12],
            "score": score,
            "grade": str(sidecar.get("grade") or ""),
            "repo_url": repo_url,
        })
    return sorted(rows, key=lambda row: (-float(row["score"]), str(row["slug"])))


def _harness_wizard_sidecar(slug: str) -> dict[str, Any] | None:
    """Load harness sidecar candidates without scanning every sidecar file."""
    if not _is_safe_slug(slug):
        return None
    for path in (
        _sidecar_dir() / f"{slug}.json",
        _sidecar_dir() / f"{slug}-harness.json",
    ):
        if not path.exists():
            continue
        sidecar = _read_sidecar_file(path)
        if sidecar is None:
            continue
        if sidecar.get("slug") == slug and _sidecar_entity_type(sidecar) == "harness":
            return sidecar
    return None


def _render_harness_wizard() -> str:
    """Manual harness interview for users who bring their own LLM."""
    return _harness_page.render_harness_wizard(
        harnesses=_harness_wizard_entries(),
        layout=_layout,
    )


def _kpi_summary_cache_key(sidecar_dir: Path) -> tuple[Any, ...]:
    parts: list[tuple[str, str, int, int, int, int]] = []
    for root in (sidecar_dir, sidecar_dir / "mcp"):
        try:
            root_name = str(root.resolve())
        except OSError:
            root_name = str(root)
        buckets = {
            "quality": [0, 0, 0, 0],
            "lifecycle": [0, 0, 0, 0],
        }
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    name = entry.name
                    if (
                        name.startswith(".")
                        or not name.endswith(".json")
                        or not entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    bucket = (
                        "lifecycle"
                        if name.endswith(".lifecycle.json")
                        else "quality"
                    )
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    data = buckets[bucket]
                    data[0] += 1
                    data[1] += int(stat.st_size)
                    data[2] = max(data[2], int(stat.st_mtime_ns))
                    data[3] = (data[3] + int(stat.st_mtime_ns)) & ((1 << 63) - 1)
        except OSError:
            pass
        for bucket, values in buckets.items():
            parts.append((root_name, bucket, values[0], values[1], values[2], values[3]))
    return tuple(parts)


def _kpi_summary_disk_cache_path(sidecar_dir: Path) -> Path:
    return sidecar_dir / ".dashboard-kpi-summary.json"


def _dashboard_summary_from_dict(summary_cls: Any, data: Any) -> Any | None:
    if not isinstance(data, dict):
        return None

    def dict_field(name: str) -> dict[str, Any]:
        value = data.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def list_field(name: str) -> list[dict[str, Any]]:
        value = data.get(name)
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    try:
        return summary_cls(
            generated_at=str(data.get("generated_at") or ""),
            total=int(data.get("total") or 0),
            by_subject=dict_field("by_subject"),
            grade_counts=dict_field("grade_counts"),
            lifecycle_counts=dict_field("lifecycle_counts"),
            category_breakdown=list_field("category_breakdown"),
            hard_floor_counts=dict_field("hard_floor_counts"),
            low_quality_candidates=list_field("low_quality_candidates"),
            archived=list_field("archived"),
        )
    except (TypeError, ValueError):
        return None


def _read_kpi_summary_disk_cache(
    sidecar_dir: Path,
    cache_token: str,
    summary_cls: Any,
) -> Any | None:
    data = _read_disk_cache_payload(_kpi_summary_disk_cache_path(sidecar_dir), cache_token)
    if data is None:
        return None
    return _dashboard_summary_from_dict(summary_cls, data.get("summary"))


def _write_kpi_summary_disk_cache(
    sidecar_dir: Path,
    cache_token: str,
    summary: Any,
) -> None:
    _write_disk_cache_payload(
        _kpi_summary_disk_cache_path(sidecar_dir),
        cache_token,
        {"summary": summary.to_dict()},
        sort_keys=True,
    )


def _kpi_summary():
    """Compute the KPI DashboardSummary using the default source layout.

    Returns ``None`` if the kpi_dashboard module can't be imported or
    the required directories don't exist — the caller renders an
    explanatory empty state instead of failing.
    """
    try:
        from kpi_dashboard import DashboardSummary  # type: ignore
        from kpi_dashboard import generate  # type: ignore
        from ctx_lifecycle import LifecycleSources  # type: ignore
    except Exception:  # noqa: BLE001 — KPIs are advisory
        return None
    sidecar_dir = _sidecar_dir()
    if not sidecar_dir.is_dir():
        return None
    cache_key = _kpi_summary_cache_key(sidecar_dir)
    global _KPI_SUMMARY_CACHE_AT, _KPI_SUMMARY_CACHE_KEY, _KPI_SUMMARY_CACHE_VALUE
    if (
        _KPI_SUMMARY_CACHE_KEY == cache_key
        and _KPI_SUMMARY_CACHE_VALUE is not None
        and time.monotonic() - _KPI_SUMMARY_CACHE_AT < _KPI_SUMMARY_CACHE_SECONDS
    ):
        return _KPI_SUMMARY_CACHE_VALUE
    cache_token = _disk_cache_token(cache_key)
    summary = _read_kpi_summary_disk_cache(sidecar_dir, cache_token, DashboardSummary)
    if summary is not None:
        _KPI_SUMMARY_CACHE_KEY = cache_key
        _KPI_SUMMARY_CACHE_VALUE = summary
        _KPI_SUMMARY_CACHE_AT = time.monotonic()
        return summary
    try:
        from ctx_config import cfg  # type: ignore
        sources = LifecycleSources(
            skills_dir=cfg.skills_dir,
            agents_dir=cfg.agents_dir,
            sidecar_dir=sidecar_dir,
        )
    except Exception:  # noqa: BLE001 — fallback: sidecar-only
        sources = LifecycleSources(
            skills_dir=sidecar_dir,
            agents_dir=sidecar_dir,
            sidecar_dir=sidecar_dir,
        )
    try:
        summary = generate(sources=sources, top_n=25)
    except Exception:  # noqa: BLE001
        return None
    _write_kpi_summary_disk_cache(sidecar_dir, cache_token, summary)
    _KPI_SUMMARY_CACHE_KEY = cache_key
    _KPI_SUMMARY_CACHE_VALUE = summary
    _KPI_SUMMARY_CACHE_AT = time.monotonic()
    return summary


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


# ─── Mutation endpoints ──────────────────────────────────────────────────────


def _is_safe_slug(slug: str) -> bool:
    return is_safe_source_name(slug)


def _perform_load(
    slug: str,
    entity_type: str = "skill",
    *,
    command: str | None = None,
    json_config: str | None = None,
) -> tuple[bool, str]:
    return dashboard_entities.perform_load(
        slug,
        entity_type,
        command=command,
        json_config=json_config,
        deps=_entity_runtime_deps(),
    )


def _perform_unload(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
    return dashboard_entities.perform_unload(
        slug,
        entity_type,
        deps=_entity_runtime_deps(),
    )


# ─── HTTP handler ────────────────────────────────────────────────────────────


class _MonitorHandler(BaseHTTPRequestHandler):
    # Silence the per-request access log spam. Users running
    # ctx-monitor get a clean stdout; errors still surface via
    # log_error() below.
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    # CSRF defense. Dashboard mutation endpoints (/api/load, /api/unload)
    # require same-origin POSTs plus a per-process token injected into the
    # served dashboard page.
    def _same_origin(self) -> bool:
        request_host = _request_host_name(self.headers.get("Host", ""))
        if not _host_allows_mutations(request_host):
            return False
        origin = self.headers.get("Origin") or ""
        if origin:
            return _origin_host_name(origin) == request_host
        # No Origin header (curl, direct tool calls) is acceptable only
        # when the mutation token below is also present.
        return True

    def _mutations_enabled(self) -> bool:
        return bool(
            getattr(self.server, "_ctx_mutations_enabled", _MONITOR_MUTATIONS_ENABLED),
        )

    def _mutation_authorized(self) -> bool:
        token = self.headers.get("X-CTX-Monitor-Token") or ""
        return (
            self._mutations_enabled()
            and bool(_MONITOR_TOKEN)
            and secrets.compare_digest(token, _MONITOR_TOKEN)
        )

    def _api_reads_enabled(self) -> bool:
        return self._mutations_enabled()

    def _read_authorized(self, qs: dict[str, str]) -> bool:
        request_host = _request_host_name(self.headers.get("Host", ""))
        if self._mutations_enabled():
            return _host_allows_mutations(request_host)
        token = (
            self.headers.get("X-CTX-Monitor-Token")
            or qs.get("token", "")
            or _read_token_cookie(self.headers.get("Cookie", ""))
        )
        return bool(_MONITOR_TOKEN) and secrets.compare_digest(token, _MONITOR_TOKEN)

    def _send_security_headers(self, *, html_response: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if getattr(self, "_ctx_set_read_cookie", False):
            self.send_header(
                "Set-Cookie",
                f"{_READ_TOKEN_COOKIE}={_MONITOR_TOKEN}; Path=/; "
                "HttpOnly; SameSite=Strict",
            )
        if html_response:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )

    def _content_length(self) -> int | None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return 0
        try:
            length = int(raw)
        except ValueError:
            self._send_json_status(400, {"detail": "invalid Content-Length"})
            return None
        if length < 0:
            self._send_json_status(400, {"detail": "invalid Content-Length"})
            return None
        if length > _MAX_POST_BODY_BYTES:
            self._send_json_status(413, {"detail": "JSON body too large"})
            return None
        return length

    def _read_json_body(self) -> dict[str, Any] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.lower() != "application/json":
            self._send_json_status(415, {"detail": "JSON body required"})
            return None
        length = self._content_length()
        if length is None:
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json_status(400, {"detail": "invalid JSON body"})
            return None
        if not isinstance(body, dict):
            self._send_json_status(400, {"detail": "JSON object body required"})
            return None
        return body

    def _discard_small_body(self) -> None:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return
        try:
            length = int(raw)
        except ValueError:
            return
        if 0 < length <= _MAX_POST_BODY_BYTES:
            self.rfile.read(length)

    def _handle_get_route(
        self,
        route: _monitor_routes.RouteMatch,
        qs: dict[str, str],
    ) -> None:
        name = route.name
        params = route.params
        if name == "home":
            self._send_html(_render_home())
        elif name == "sessions_index":
            self._send_html(_render_sessions_index())
        elif name == "session_detail":
            self._send_html(_render_session_detail(params["session_id"]))
        elif name == "skills":
            self._send_html(_render_skills(qs))
        elif name == "skillspector":
            self._send_html(_render_skillspector(qs))
        elif name == "skill_detail":
            self._send_html(_render_skill_detail(params["slug"], qs.get("type")))
        elif name == "loaded":
            self._send_html(_render_loaded(self._mutations_enabled()))
        elif name == "logs":
            self._send_html(_render_logs())
        elif name == "graph":
            self._send_html(_render_graph(qs.get("slug"), qs.get("type")))
        elif name == "manage":
            self._send_html(_render_manage(self._mutations_enabled()))
        elif name == "harness":
            self._send_html(_render_harness_wizard())
        elif name == "docs":
            self._send_html(_render_docs())
        elif name == "config":
            self._send_html(_render_config())
        elif name == "status":
            self._send_html(_render_status())
        elif name == "wiki_index":
            self._send_html(_render_wiki_index(qs.get("type"), qs.get("q", "")))
        elif name == "wiki_entity":
            self._send_html(
                _render_wiki_entity(
                    params["slug"],
                    qs.get("type"),
                    mutations_enabled=self._mutations_enabled(),
                ),
            )
        elif name == "kpi":
            self._send_html(_render_kpi())
        elif name == "runtime":
            self._send_html(_render_runtime_lifecycle())
        elif name == "events":
            self._send_html(_render_events())
        elif name == "api_sessions":
            self._send_json(_summarize_sessions())
        elif name == "api_manifest":
            self._send_json(_read_manifest())
        elif name == "api_status":
            self._send_json(_status_payload())
        elif name == "api_kpi":
            summary = _kpi_summary()
            self._send_json(summary.to_dict() if summary is not None else {
                "total": 0, "detail": "no sidecars yet",
            })
        elif name == "api_grades":
            self._send_json(_grade_distribution_payload())
        elif name == "api_sidecars":
            self._send_json(_sidecar_page_payload(qs))
        elif name == "api_runtime":
            self._send_json(_runtime_lifecycle_summary())
        elif name == "api_skillspector":
            self._send_json(_skillspector_audit_payload(qs))
        elif name == "api_config":
            self._send_json(_effective_config_payload())
        elif name == "api_entities_search":
            try:
                limit = max(1, min(int(qs.get("limit", 80)), 200))
                results = _search_wiki_entities(
                    qs.get("q", ""),
                    qs.get("type") or None,
                    limit=limit,
                )
            except ValueError as exc:
                self._send_json_status(400, {"detail": str(exc)})
                return
            self._send_json({"results": results, "total": len(results)})
        elif name == "api_entity":
            slug = params["slug"]
            try:
                detail = _wiki_entity_detail(slug, qs.get("type"))
            except ValueError as exc:
                self._send_json_status(400, {"detail": str(exc)})
                return
            if detail is None:
                self._send_json_status(404, {"detail": f"no wiki entity for {slug}"})
            else:
                self._send_json(detail)
        elif name == "api_skill":
            slug = params["slug"]
            sidecar = _load_sidecar(slug, entity_type=qs.get("type"))
            if sidecar is None:
                self._send_404(f"no sidecar for {slug}")
            else:
                self._send_json(sidecar)
        elif name == "api_graph":
            slug = params["slug"]
            requested_type = qs.get("type")
            graph_entity_type = _normalize_dashboard_entity_type(requested_type)
            if requested_type is not None and graph_entity_type is None:
                self._send_json_status(
                    400,
                    {"detail": f"unsupported entity_type: {requested_type!r}"},
                )
                return
            try:
                hops = max(1, min(int(qs.get("hops", 1)), 3))
                limit = max(5, min(int(qs.get("limit", 40)), 150))
            except ValueError:
                self._send_json_status(
                    400,
                    {"detail": "hops and limit must be integers"},
                )
                return
            self._send_json(_graph_neighborhood(
                slug, hops=hops, limit=limit, entity_type=graph_entity_type,
            ))
        elif name == "api_events_stream":
            self._stream_audit_log()
        else:
            self._send_404(name)

    def do_GET(self) -> None:  # noqa: N802 — stdlib signature
        target = _monitor_routes.parse_request_target(self.path)
        path = target.path
        qs = target.query
        try:
            self._ctx_set_read_cookie = False
            read_authorized = getattr(self, "_read_authorized", lambda _qs: True)
            if not read_authorized(qs):
                if path.startswith("/api/"):
                    self._send_json_status(
                        403,
                        {"detail": "monitor read token required on non-loopback bind"},
                    )
                else:
                    self._send_html_status(
                        403,
                        "<h1>403</h1>"
                        "<p>monitor read token required on non-loopback bind</p>",
                    )
                return
            query_token = qs.get("token", "")
            self._ctx_set_read_cookie = (
                not self._mutations_enabled()
                and bool(query_token)
                and bool(_MONITOR_TOKEN)
                and secrets.compare_digest(query_token, _MONITOR_TOKEN)
            )
            route = _monitor_routes.match_get_route(path)
            if route is None:
                self._send_404(path)
                return
            self._handle_get_route(route, qs)
        except (BrokenPipeError, ConnectionAbortedError):
            # Browser disconnected mid-response — benign for a local
            # dashboard; nothing to do.
            return
        except Exception as exc:  # noqa: BLE001 — last-resort handler
            self._send_500(exc)

    def do_POST(self) -> None:  # noqa: N802 — stdlib signature
        """Mutation endpoints. Same-origin only; JSON body required."""
        path = _monitor_routes.parse_request_target(self.path).path
        try:
            if not self._mutations_enabled():
                self._discard_small_body()
                self._send_json_status(
                    403, {"detail": "monitor mutations disabled on non-loopback bind"},
                )
                return
            if not self._same_origin():
                self._discard_small_body()
                self._send_json_status(
                    403, {"detail": "cross-origin POST denied"},
                )
                return
            if not self._mutation_authorized():
                self._discard_small_body()
                self._send_json_status(
                    403, {"detail": "monitor token required"},
                )
                return
            body = self._read_json_body()
            if body is None:
                return

            route = _monitor_routes.match_post_route(path)
            if route is None:
                self._send_404(path)
                return

            if route.name == "api_load":
                slug = str(body.get("slug", "")).strip()
                etype = str(body.get("entity_type", "skill")).strip() or "skill"
                command = body.get("command")
                json_config = body.get("json_config")
                kwargs: dict[str, str] = {}
                if isinstance(command, str) and command:
                    kwargs["command"] = command
                if isinstance(json_config, str) and json_config:
                    kwargs["json_config"] = json_config
                ok, msg = _perform_load(slug, entity_type=etype, **kwargs)
                self._send_json_status(
                    200 if ok else 400, {"ok": ok, "detail": msg},
                )
            elif route.name == "api_unload":
                slug = str(body.get("slug", "")).strip()
                # entity_type defaults to "skill" for backward compat with
                # existing JS that only sends {slug}. New /loaded page
                # sends {slug, entity_type} so MCPs flow through the
                # subprocess unload path.
                etype = str(body.get("entity_type", "skill")).strip() or "skill"
                ok, msg = _perform_unload(slug, entity_type=etype)
                self._send_json_status(
                    200 if ok else 400, {"ok": ok, "detail": msg},
                )
            elif route.name == "api_config":
                updates = body.get("updates", {})
                if not isinstance(updates, dict):
                    self._send_json_status(
                        400, {"ok": False, "detail": "updates must be an object"},
                    )
                    return
                result = _save_config_updates(updates)
                self._send_json_status(200 if result.get("ok") else 400, result)
            elif route.name == "api_entity_upsert":
                ok, msg = _upsert_wiki_entity(body)
                self._send_json_status(
                    200 if ok else 400, {"ok": ok, "detail": msg},
                )
            elif route.name == "api_entity_delete":
                slug = str(body.get("slug", "")).strip()
                etype = str(body.get("entity_type", "skill")).strip() or "skill"
                ok, msg = _delete_wiki_entity(slug, etype)
                self._send_json_status(
                    200 if ok else 400, {"ok": ok, "detail": msg},
                )
            else:
                self._send_404(path)
        except (BrokenPipeError, ConnectionAbortedError):
            return
        except Exception as exc:  # noqa: BLE001
            self._send_500(exc)

    def _send_json_status(self, status: int, obj: Any) -> None:
        raw = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._send_security_headers(html_response=True)
        self.end_headers()
        self.wfile.write(raw)

    def _send_html_status(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._send_security_headers(html_response=True)
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, obj: Any) -> None:
        raw = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _send_404(self, detail: str) -> None:
        body = f"<h1>404</h1><p>{html.escape(detail)}</p>".encode()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(html_response=True)
        self.end_headers()
        self.wfile.write(body)

    def _send_500(self, exc: BaseException) -> None:
        self.log_error("render error: %s", exc)
        body = f"<h1>500</h1><pre>{html.escape(repr(exc))}</pre>".encode()
        self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers(html_response=True)
        self.end_headers()
        self.wfile.write(body)

    def _stream_audit_log(self) -> None:
        """Server-sent events: tail the audit log line-by-line."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_security_headers()
        self.end_headers()

        path = _audit_log_path()
        position = path.stat().st_size if path.exists() else 0
        last_heartbeat = time.monotonic()
        try:
            while not _server_shutdown_requested(self.server):
                if path.exists() and path.stat().st_size > position:
                    with path.open("r", encoding="utf-8") as f:
                        f.seek(position)
                        for line in f:
                            if not line.strip():
                                continue
                            self.wfile.write(f"data: {line.rstrip()}\n\n".encode())
                            self.wfile.flush()
                        position = f.tell()
                    last_heartbeat = time.monotonic()
                elif time.monotonic() - last_heartbeat > 25:
                    # SSE heartbeat comment — keeps proxies from timing out
                    # on idle streams. Also detects dead clients (write
                    # will raise BrokenPipeError).
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last_heartbeat = time.monotonic()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _make_monitor_server(host: str, port: int) -> _MonitorServer:
    global _MONITOR_MUTATIONS_ENABLED
    _MONITOR_MUTATIONS_ENABLED = _host_allows_mutations(host)
    return _make_server(
        host,
        port,
        _MonitorHandler,
        mutations_enabled=_MONITOR_MUTATIONS_ENABLED,
    )


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the monitor. Blocks until Ctrl+C."""
    global _MONITOR_TOKEN
    server = _make_monitor_server(host, port)
    _MONITOR_TOKEN = secrets.token_urlsafe(32)
    mutations_enabled = bool(getattr(server, "_ctx_mutations_enabled", False))
    url = f"http://{_monitor_display_host(host)}:{port}/"
    if not mutations_enabled:
        url = f"{url}?token={_MONITOR_TOKEN}"
    print(f"ctx-monitor serving at {url}  (Ctrl+C to stop)", flush=True)
    if not mutations_enabled:
        print(
            "ctx-monitor: non-loopback bind; read token required and "
            "load/unload mutations disabled",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("ctx-monitor: shutdown", flush=True)
    finally:
        server.server_close()


def _monitor_display_host(host: str) -> str:
    """Return a URL host users can paste into a browser."""
    if host in {"0.0.0.0", "::"}:
        try:
            candidate = socket.gethostbyname(socket.gethostname())
        except OSError:
            candidate = ""
        if candidate and not candidate.startswith("127."):
            return candidate
        return "localhost"
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctx-monitor",
        description="Local HTTP dashboard for ctx skill/agent activity.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="Start the monitor web server")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument(
        "--host", default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1; use 0.0.0.0 to expose — be careful)",
    )

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        serve(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
