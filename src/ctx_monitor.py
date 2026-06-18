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
import threading
import time
import zlib
from collections import defaultdict, deque
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ctx import dashboard_docs, dashboard_entities, dashboard_graph
from ctx.core import entity_types as core_entity_types
from ctx.core.wiki import wiki_queue
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text
from ctx.utils._fs_utils import safe_atomic_write_text as _safe_atomic_write_text
from ctx.utils._safe_name import is_safe_source_name


_MONITOR_TOKEN = ""
_MONITOR_MUTATIONS_ENABLED = True
_GRAPH_CACHE_KEY: tuple[Any, ...] | None = None
_GRAPH_CACHE_VALUE: Any | None = None
_PACKAGED_GRAPH_EXPORT_ID_CACHE: str | None | bool = None
_OVERLAY_INDEX_COVERAGE_CACHE_KEY: tuple[Any, ...] | None = None
_OVERLAY_INDEX_COVERAGE_CACHE_VALUE: bool | None = None
_SIDECAR_INDEX_CACHE_KEY: tuple[tuple[Path, float, int], ...] | None = None
_SIDECAR_INDEX_CACHE_VALUE: dict[tuple[str, str], dict] | None = None
_SIDECAR_FILTER_CACHE_SIGNATURE: tuple[Any, ...] | None = None
_SIDECAR_FILTER_CACHE_VALUE: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
_KPI_SUMMARY_CACHE_KEY: tuple[Any, ...] | None = None
_KPI_SUMMARY_CACHE_VALUE: Any | None = None
_KPI_SUMMARY_CACHE_AT = 0.0
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
    return Path(os.path.expanduser("~/.claude"))


def _audit_log_path() -> Path:
    # Avoid importing ctx_audit_log here so the monitor can run even if
    # ctx_audit_log is absent for some reason.
    return _claude_dir() / "ctx-audit.jsonl"


def _events_jsonl_path() -> Path:
    return _claude_dir() / "skill-events.jsonl"


def _runtime_lifecycle_path() -> Path:
    from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore

    return RuntimeLifecycleStore().events_path


def _manifest_path() -> Path:
    return _claude_dir() / "skill-manifest.json"


def _sidecar_dir() -> Path:
    return _claude_dir() / "skill-quality"


def _wiki_dir() -> Path:
    return _claude_dir() / "skill-wiki"


def _user_config_path() -> Path:
    return _claude_dir() / "skill-system-config.json"


def _load_dashboard_graph() -> Any:
    """Load the wiki graph once per graph.json file version."""
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE

    graph_path = _wiki_dir() / "graphify-out" / "graph.json"
    overlay_path = graph_path.with_name("entity-overlays.jsonl")
    from ctx.core.graph.resolve_graph import load_graph as _lg  # type: ignore

    if not graph_path.exists():
        _GRAPH_CACHE_KEY = None
        _GRAPH_CACHE_VALUE = None
        return _lg(graph_path)

    stat = graph_path.stat()
    overlay_key = None
    if overlay_path.exists():
        overlay_stat = overlay_path.stat()
        overlay_key = (overlay_stat.st_mtime, overlay_stat.st_size)
    cache_key = (graph_path.resolve(), stat.st_mtime, stat.st_size, id(_lg), overlay_key)
    if _GRAPH_CACHE_KEY == cache_key and _GRAPH_CACHE_VALUE is not None:
        return _GRAPH_CACHE_VALUE

    try:
        graph = _lg(graph_path, apply_runtime_filter=False)
    except TypeError:
        graph = _lg(graph_path)
    _GRAPH_CACHE_KEY = cache_key
    _GRAPH_CACHE_VALUE = graph
    return graph


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
    for _sub, current_type, _recursive in _DASHBOARD_ENTITY_SOURCES:
        if entity_type is not None and entity_type != current_type:
            continue
        p = core_entity_types.entity_page_path(_wiki_dir(), current_type, slug)
        if p is None:
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
    base = _wiki_dir() / "entities"
    if not base.is_dir():
        return []
    rows: list[tuple[str, str, Path]] = []
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
                rows.append((slug, current_type, path))
    return sorted(rows, key=lambda row: (row[1], row[0].lower(), row[2].as_posix()))


def _wiki_entity_detail(slug: str, entity_type: str | None = None) -> dict[str, Any] | None:
    normalized = _normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized is None:
        raise ValueError(f"unsupported entity_type: {entity_type!r}")
    path = _wiki_entity_path(slug, entity_type=normalized)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _parse_frontmatter(text)
    detected_type = normalized or _normalize_dashboard_entity_type(frontmatter.get("type")) or "skill"
    return {
        "slug": slug,
        "type": detected_type,
        "path": str(path),
        "frontmatter": frontmatter,
        "body": body,
    }


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


_WIKI_INLINE_RE = re.compile(
    r"(`[^`\n]+`|\[\[[^\]\n]+\]\]|(?<!!)\[[^\]\n]+\]\([^\s()\n]+(?:\s+\"[^\"]*\")?\))",
)
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
    """Return a safe href for normal Markdown links, or None to suppress it."""
    cleaned = target.strip()
    if not cleaned:
        return None
    if cleaned.startswith("//"):
        return None
    if cleaned.startswith(("/", "#")):
        return cleaned
    if re.match(r"^https?://", cleaned, re.IGNORECASE):
        return cleaned
    if re.match(r"^mailto:[^@\s]+@[^@\s]+$", cleaned, re.IGNORECASE):
        return cleaned
    return None


def _render_wiki_inline(text: str) -> str:
    """Render a small safe inline Markdown subset used by wiki pages."""
    out: list[str] = []
    last = 0
    for match in _WIKI_INLINE_RE.finditer(text):
        out.append(html.escape(text[last:match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            out.append(f"<code>{html.escape(token[1:-1])}</code>")
        elif token.startswith("[["):
            inner = token[2:-2]
            target, _, label = inner.partition("|")
            href, fallback_label = _wiki_link_href(target)
            link_text = label.strip() or fallback_label
            out.append(
                f"<a href='{html.escape(href)}'>{html.escape(link_text)}</a>",
            )
        else:
            link_match = re.fullmatch(
                r"\[([^\]\n]+)\]\(([^\s()\n]+)(?:\s+\"[^\"]*\")?\)",
                token,
            )
            if not link_match:
                out.append(html.escape(token))
            else:
                label, target = link_match.groups()
                safe_href = _markdown_link_href(target)
                if safe_href is None:
                    out.append(html.escape(label))
                else:
                    out.append(
                        f"<a href='{html.escape(safe_href)}'>{html.escape(label)}</a>",
                    )
        last = match.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def _render_wiki_markdown(markdown_text: str) -> str:
    """Render a conservative Markdown subset without adding dependencies."""
    lines = markdown_text.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_render_wiki_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            out.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    def flush_code() -> None:
        if code_lines:
            out.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
            code_lines.clear()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = min(len(heading.group(1)), 4)
            out.append(f"<h{level}>{_render_wiki_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            list_items.append(_render_wiki_inline(bullet.group(1).strip()))
            continue
        flush_list()
        paragraph.append(stripped)

    flush_code()
    flush_paragraph()
    flush_list()
    return "".join(out) if out else "<p class='muted'>No body.</p>"


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


def _entity_tab_script() -> str:
    return """
<script>
(function () {
  function showEntityTab(name) {
    document.querySelectorAll('[data-entity-tab]').forEach(function (button) {
      var active = button.getAttribute('data-entity-tab') === name;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    document.querySelectorAll('[data-entity-tab-panel]').forEach(function (panel) {
      panel.hidden = panel.getAttribute('data-entity-tab-panel') !== name;
    });
  }
  document.querySelectorAll('[data-entity-tab]').forEach(function (button) {
    button.addEventListener('click', function () {
      var name = button.getAttribute('data-entity-tab');
      showEntityTab(name);
      if (history.replaceState) {
        history.replaceState(null, '', '#' + name);
      }
    });
  });
  document.querySelectorAll('[data-open-entity-tab]').forEach(function (link) {
    link.addEventListener('click', function (event) {
      event.preventDefault();
      var name = link.getAttribute('data-open-entity-tab');
      showEntityTab(name);
      if (history.replaceState) {
        history.replaceState(null, '', '#' + name);
      }
    });
  });
  var initial = (location.hash || '#overview').replace('#', '');
  if (!document.querySelector('[data-entity-tab="' + initial + '"]')) {
    initial = 'overview';
  }
  showEntityTab(initial);
})();
</script>
"""


def _render_entity_tabs(
    *,
    overview_html: str,
    subgraph_html: str,
    quality_html: str,
) -> str:
    return (
        "<div class='entity-tabs' role='tablist' aria-label='Entity sections'>"
        "<button type='button' class='entity-tab-button active' role='tab' aria-selected='true' "
        "data-entity-tab='overview'>Overview</button>"
        "<button type='button' class='entity-tab-button' role='tab' aria-selected='false' "
        "data-entity-tab='subgraph'>Subgraph</button>"
        "<button type='button' class='entity-tab-button' role='tab' aria-selected='false' "
        "data-entity-tab='quality'>Quality</button>"
        "</div>"
        f"<section id='overview' class='entity-tab-panel' data-entity-tab-panel='overview'>{overview_html}</section>"
        f"<section id='subgraph' class='entity-tab-panel' data-entity-tab-panel='subgraph' hidden>{subgraph_html}</section>"
        f"<section id='quality' class='entity-tab-panel' data-entity-tab-panel='quality' hidden>{quality_html}</section>"
        + _entity_tab_script()
    )


def _render_quality_drilldown(
    sidecar: dict | None,
    embedded_quality_markdown: str | None = None,
) -> str:
    """Explain quality score signals for a wiki entity."""
    if sidecar is None:
        if embedded_quality_markdown:
            quality_markdown = embedded_quality_markdown.strip()
            if not re.search(r"^#{1,6}\s+Quality\b", quality_markdown, re.IGNORECASE | re.MULTILINE):
                quality_markdown = "## Quality\n\n" + quality_markdown
            return (
                "<div class='card wiki-body'>"
                + _render_wiki_markdown(quality_markdown)
                + "</div>"
            )
        return (
            "<div class='card'>"
            "<h2>Quality</h2>"
            "<p class='muted'>No quality sidecar exists for this entity yet.</p>"
            "</div>"
    )
    grade = str(sidecar.get("grade", "F"))
    score = float(sidecar.get("raw_score", sidecar.get("score", 0.0)) or 0.0)
    weights_raw = sidecar.get("weights")
    signals_raw = sidecar.get("signals")
    weights: dict[str, Any] = weights_raw if isinstance(weights_raw, dict) else {}
    signals: dict[str, Any] = signals_raw if isinstance(signals_raw, dict) else {}
    signal_rows: list[str] = []
    for name, signal in sorted(signals.items()):
        signal_data = signal if isinstance(signal, dict) else {}
        signal_score = float(signal_data.get("score", 0.0) or 0.0)
        weight = float(weights.get(name, 0.0) or 0.0)
        contribution = signal_score * weight
        evidence = signal_data.get("evidence", {})
        evidence_text = json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)
        evidence_preview, evidence_truncated = _truncate_text(evidence_text, 420)
        truncated_marker = " <span class='muted'>(truncated)</span>" if evidence_truncated else ""
        signal_rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(name))}</code></td>"
            f"<td><code>{signal_score:.3f}</code></td>"
            f"<td><code>{weight:.3f}</code></td>"
            f"<td><code>{contribution:.3f}</code></td>"
            f"<td><code>{html.escape(evidence_preview)}</code>{truncated_marker}</td>"
            "</tr>"
        )
    if not signal_rows:
        signal_rows.append("<tr><td colspan='5' class='muted'>No signal breakdown was recorded.</td></tr>")
    hard_floor = sidecar.get("hard_floor")
    floor_html = f" <span class='muted'>floor {html.escape(str(hard_floor))}</span>" if hard_floor else ""
    return (
        "<div class='card'>"
        "<h2>Quality</h2>"
        f"<p><span class='pill grade-{html.escape(grade)}'>{html.escape(grade)}</span> "
        f"score <strong>{score:.3f}</strong>"
        f"{floor_html}</p>"
        "<p class='muted'>Score is the weighted sum of recorded quality signals. "
        "A hard floor can cap the final grade even when individual signals pass.</p>"
        "<table class='quality-signal-table'>"
        "<tr><th>Signal</th><th>Signal score</th><th>Weight</th><th>Contribution</th><th>Evidence</th></tr>"
        + "".join(signal_rows)
        + "</table>"
        "<details><summary>Raw sidecar JSON</summary>"
        f"<pre>{html.escape(json.dumps(sidecar, indent=2, ensure_ascii=False, default=str)[:6000])}</pre>"
        "</details>"
        "</div>"
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


def _repo_graph_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "graph"


def _first_existing_file_status(*paths: Path) -> dict[str, Any]:
    for path in paths:
        if path.exists():
            return _file_status(path)
    return _file_status(paths[0])


def _first_existing_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def _skillspector_audit_path() -> Path:
    return _first_existing_path(
        _wiki_dir() / "security" / "skillspector-audit.jsonl.gz",
        _repo_graph_dir() / "skillspector-audit.jsonl.gz",
    )


def _skillspector_communities_path() -> Path | None:
    candidates = (
        _wiki_dir() / "graphify-out" / "communities.json",
        _repo_graph_dir() / "communities.json",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def _skillspector_index_path() -> Path | None:
    index_path = _dashboard_graph_index_path()
    if index_path.is_file() and _dashboard_index_matches_manifest(index_path):
        return index_path
    return None


def _skillspector_limit(qs: dict[str, str]) -> int:
    try:
        return max(1, min(int(qs.get("limit", 100)), 500))
    except ValueError:
        return 100


def _skillspector_audit_payload(qs: dict[str, str] | None = None) -> dict[str, Any]:
    from ctx.core.quality.skillspector_monitor import (  # noqa: PLC0415
        build_skillspector_audit_payload,
        load_skill_families_from_communities,
        load_skill_metadata_from_dashboard_index,
        load_skillspector_audit_records,
    )

    qs = qs or {}
    audit_path = _skillspector_audit_path()
    records = load_skillspector_audit_records(audit_path)
    payload = build_skillspector_audit_payload(
        records,
        metadata_by_slug=load_skill_metadata_from_dashboard_index(_skillspector_index_path()),
        families_by_slug=load_skill_families_from_communities(_skillspector_communities_path()),
        query=qs.get("q", ""),
        status=qs.get("status", ""),
        severity=qs.get("severity", ""),
        tag=qs.get("tag", ""),
        family=qs.get("family", ""),
        limit=_skillspector_limit(qs),
    )
    payload["audit_path"] = str(audit_path)
    payload["audit_available"] = audit_path.is_file()
    return payload


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
        "graph_delta_json": _file_status(graph_dir / "graph-delta.json"),
        "communities_json": _file_status(graph_dir / "communities.json"),
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
    raw = str(
        sidecar.get("entity_type")
        or sidecar.get("subject_type")
        or sidecar.get("type")
        or fallback
    )
    return {
        "skills": "skill",
        "skill": "skill",
        "agents": "agent",
        "agent": "agent",
        "mcp": "mcp-server",
        "mcp-server": "mcp-server",
        "mcp-servers": "mcp-server",
        "harness": "harness",
        "harnesses": "harness",
    }.get(raw, raw)


def _sidecar_fallback_type(path: Path) -> str:
    return "mcp-server" if path.parent.name == "mcp" else "skill"


def _read_sidecar_file(path: Path) -> dict | None:
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(sidecar, dict):
        return None
    etype = _sidecar_entity_type(sidecar, _sidecar_fallback_type(path))
    sidecar.setdefault("slug", path.stem)
    sidecar["subject_type"] = etype
    return sidecar


def _load_sidecar(slug: str, entity_type: str | None = None) -> dict | None:
    if not _is_safe_slug(slug):
        return None
    paths = [
        _sidecar_dir() / f"{slug}.json",
        _sidecar_dir() / "mcp" / f"{slug}.json",
    ]
    if entity_type is not None:
        suffixes = [entity_type]
        if entity_type == "mcp-server":
            suffixes.append("mcp")
        for suffix in suffixes:
            paths.append(_sidecar_dir() / f"{slug}-{suffix}.json")

    for path in paths:
        if not path.exists():
            continue
        sidecar = _read_sidecar_file(path)
        if sidecar is None:
            continue
        if entity_type is None or _sidecar_entity_type(sidecar) == entity_type:
            return sidecar
    if entity_type is not None and _SIDECAR_INDEX_CACHE_VALUE is not None:
        return _sidecar_index().get((slug, entity_type))
    return None


def _sidecar_files() -> list[Path]:
    files: list[Path] = []
    for root in (_sidecar_dir(), _sidecar_dir() / "mcp"):
        if not root.is_dir():
            continue
        files.extend(
            p for p in sorted(root.glob("*.json"))
            if not p.name.startswith(".")
            and not p.name.endswith(".lifecycle.json")
        )
    return files


def _sidecar_index_cache_key() -> tuple[tuple[Path, float, int], ...]:
    keys: list[tuple[Path, float, int]] = []
    for path in _sidecar_files():
        stat = path.stat()
        keys.append((path.resolve(), stat.st_mtime, stat.st_size))
    if keys:
        return tuple(keys)
    for root in (_sidecar_dir(), _sidecar_dir() / "mcp"):
        if not root.is_dir():
            continue
        stat = root.stat()
        keys.append((root.resolve(), stat.st_mtime, stat.st_size))
    return tuple(keys)


def _sidecar_index() -> dict[tuple[str, str], dict]:
    global _SIDECAR_INDEX_CACHE_KEY, _SIDECAR_INDEX_CACHE_VALUE

    cache_key = _sidecar_index_cache_key()
    if _SIDECAR_INDEX_CACHE_KEY == cache_key and _SIDECAR_INDEX_CACHE_VALUE is not None:
        return _SIDECAR_INDEX_CACHE_VALUE

    index: dict[tuple[str, str], dict] = {}
    for path in _sidecar_files():
        sidecar = _read_sidecar_file(path)
        if sidecar is None:
            continue
        slug = str(sidecar.get("slug") or path.stem)
        entity_type = _sidecar_entity_type(sidecar)
        index.setdefault((slug, entity_type), sidecar)
    _SIDECAR_INDEX_CACHE_KEY = cache_key
    _SIDECAR_INDEX_CACHE_VALUE = index
    return index


def _all_sidecars() -> list[dict]:
    return list(_sidecar_index().values())


def _skills_page_int(
    value: str | None,
    *,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _skills_query_values(raw: str | None, allowed: set[str]) -> set[str]:
    values = {
        item.strip()
        for item in str(raw or "").split(",")
        if item.strip()
    }
    return {item for item in values if item in allowed}


def _sidecar_sort_key(sidecar: dict) -> tuple[str, float, str]:
    return (
        str(sidecar.get("grade") or "F"),
        -float(sidecar.get("raw_score") or sidecar.get("score") or 0.0),
        str(sidecar.get("slug") or ""),
    )


def _sidecar_card_payload(sidecar: dict) -> dict[str, Any]:
    slug = str(sidecar.get("slug") or "")
    entity_type = _sidecar_entity_type(sidecar)
    return {
        "slug": slug,
        "grade": str(sidecar.get("grade") or "F"),
        "type": entity_type,
        "hard_floor": str(sidecar.get("hard_floor") or ""),
        "raw_score": float(sidecar.get("raw_score") or sidecar.get("score") or 0.0),
        "sidecar_href": f"/skill/{quote(slug)}?type={quote(entity_type)}",
        "wiki_href": f"/wiki/{quote(slug)}?type={quote(entity_type)}",
        "graph_href": f"/graph?slug={quote(slug)}&type={quote(entity_type)}",
    }


def _sidecar_filter_signature(files: list[Path]) -> tuple[Any, ...]:
    signature: list[tuple[str, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature.append((
            str(path.resolve()),
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            int(stat.st_size),
        ))
    if signature:
        return tuple(signature)
    roots = (_sidecar_dir(), _sidecar_dir() / "mcp")
    for root in roots:
        if not root.is_dir():
            signature.append((str(root.resolve()), 0, 0))
            continue
        stat = root.stat()
        signature.append((
            str(root.resolve()),
            int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
            0,
        ))
    return tuple(signature)


def _sidecar_candidate_files(
    files: list[Path],
    *,
    q: str,
    types: set[str],
) -> list[Path]:
    q_lower = q.lower()
    candidates = [
        path for path in files
        if not q_lower or q_lower in path.stem.lower()
    ]
    if not types:
        return candidates
    if types == {"mcp-server"}:
        return [path for path in candidates if path.parent.name == "mcp"]
    if "mcp-server" not in types:
        return [path for path in candidates if path.parent.name != "mcp"]
    return candidates


def _filtered_sidecar_records(
    files: list[Path],
    *,
    q: str,
    types: set[str],
    grades: set[str],
    hide_floor: bool,
) -> list[dict[str, Any]]:
    """Return cached filtered sidecar card records for /skills search."""
    global _SIDECAR_FILTER_CACHE_SIGNATURE, _SIDECAR_FILTER_CACHE_VALUE

    signature = _sidecar_filter_signature(files)
    if _SIDECAR_FILTER_CACHE_SIGNATURE != signature:
        _SIDECAR_FILTER_CACHE_SIGNATURE = signature
        _SIDECAR_FILTER_CACHE_VALUE = {}
    cache_key = (
        q.lower(),
        tuple(sorted(types)),
        tuple(sorted(grades)),
        hide_floor,
    )
    cached = _SIDECAR_FILTER_CACHE_VALUE.get(cache_key)
    if cached is not None:
        return cached

    records: list[dict[str, Any]] = []
    for path in _sidecar_candidate_files(files, q=q, types=types):
        sidecar = _read_sidecar_file(path)
        if sidecar is None:
            continue
        if not _sidecar_matches_filters(
            sidecar,
            q=q,
            types=types,
            grades=grades,
            hide_floor=hide_floor,
        ):
            continue
        records.append(_sidecar_card_payload(sidecar))
    records.sort(key=_sidecar_sort_key)
    if len(_SIDECAR_FILTER_CACHE_VALUE) >= 32:
        _SIDECAR_FILTER_CACHE_VALUE.clear()
    _SIDECAR_FILTER_CACHE_VALUE[cache_key] = records
    return records


def _sidecar_matches_filters(
    sidecar: dict,
    *,
    q: str,
    types: set[str],
    grades: set[str],
    hide_floor: bool,
) -> bool:
    entity_type = _sidecar_entity_type(sidecar)
    grade = str(sidecar.get("grade") or "F")
    floor = str(sidecar.get("hard_floor") or "")
    if types and entity_type not in types:
        return False
    if grades and grade not in grades:
        return False
    if hide_floor and floor:
        return False
    if q:
        return q.lower() in str(sidecar.get("slug") or "").lower()
    return True


def _sidecar_page_payload(qs: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a paginated sidecar payload for /skills and its JSON API."""
    qs = qs or {}
    page = _skills_page_int(qs.get("page"), default=1)
    limit = _skills_page_int(
        qs.get("limit"),
        default=_SKILLS_PAGE_DEFAULT_LIMIT,
        maximum=_SKILLS_PAGE_MAX_LIMIT,
    )
    q = str(qs.get("q") or "").strip()
    types = _skills_query_values(qs.get("type"), set(_DASHBOARD_ENTITY_TYPES))
    grades = _skills_query_values(qs.get("grade"), {"A", "B", "C", "D", "F"})
    hide_floor = str(qs.get("hide_floor") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }

    files = _sidecar_files()
    catalog_total = len(files)
    has_filters = bool(q or types or grades or hide_floor)
    if has_filters:
        sidecars = _filtered_sidecar_records(
            files,
            q=q,
            types=types,
            grades=grades,
            hide_floor=hide_floor,
        )
        total = len(sidecars)
        start = (page - 1) * limit
        page_sidecars = sidecars[start:start + limit]
    else:
        total = catalog_total
        start = (page - 1) * limit
        selected_files = files[start:start + limit]
        page_sidecars = [
            sidecar
            for path in selected_files
            if (sidecar := _read_sidecar_file(path)) is not None
        ]
        if catalog_total <= limit:
            page_sidecars.sort(key=_sidecar_sort_key)

    pages = max(1, math.ceil(total / limit)) if total else 1
    if page > pages:
        page = pages
        return _sidecar_page_payload({
            **qs,
            "page": str(page),
            "limit": str(limit),
        })

    return {
        "items": [_sidecar_card_payload(sidecar) for sidecar in page_sidecars],
        "total": total,
        "catalog_total": catalog_total,
        "page": page,
        "limit": limit,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
        "filtered": has_filters,
        "q": q,
        "types": sorted(types),
        "grades": sorted(grades),
        "hide_floor": hide_floor,
    }


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
    dist = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    for s in _all_sidecars():
        g = s.get("grade")
        if g in dist:
            dist[g] += 1
    return dist


def _grade_distribution_payload() -> dict[str, Any]:
    grades = _grade_distribution()
    return {"grades": grades, "total": sum(grades.values())}


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


def _monitor_asset_text(name: str) -> str:
    """Read a packaged dashboard asset."""
    return files("ctx").joinpath("assets", name).read_text(encoding="utf-8")


def _monitor_inline_script(name: str) -> str:
    return f"<script>\n{_monitor_asset_text(name).rstrip()}\n</script>"


_CSS = _monitor_asset_text("monitor.css")


def _layout(title: str, body: str) -> str:
    """Wrap body HTML in the standard page chrome."""
    nav_items = (
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
    nav_html = "".join(
        f"<a href='{html.escape(href)}' data-nav-key='{html.escape(key)}' "
        "draggable='true' title='Drag to reorder dashboard tabs'>"
        f"{html.escape(label)}</a>"
        for key, label, href in nav_items
    )
    nav_default_keys = html.escape(
        json.dumps([key for key, _label, _href in nav_items]),
        quote=True,
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} — ctx monitor</title>"
        f"<style>{_CSS}</style></head><body>"
        "<div class='app-shell'>"
        "<header class='app-header'>"
        "<a class='app-brand' href='/' aria-label='ctx monitor home'>"
        "<span class='app-brand-mark'>ctx</span><span>monitor</span></a>"
        "<div class='app-header-meta'>local graph, wiki, skills, agents, MCPs, and harnesses</div>"
        "</header>"
        "<div class='nav' id='dashboard-nav' "
        "data-nav-storage-key='ctx-monitor-nav-order' "
        f"data-nav-default-keys='{nav_default_keys}' "
        "aria-label='Dashboard navigation'>"
        + nav_html
        + "<button type='button' id='nav-reset' class='nav-reset' "
          "title='Reset dashboard tab order'>reset</button>"
        "</div>"
        + _monitor_inline_script("monitor-nav.js")
        + "<main class='app-main'>"
        + body
        + "</main></div></body></html>"
    )


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


def _render_home() -> str:
    sessions = _summarize_sessions()
    recent = sessions[:10]
    gstats = _graph_stats()
    wstats = _wiki_stats()
    runtime = _runtime_lifecycle_summary()
    audit_lines = sum(1 for _ in _audit_log_path().open(encoding="utf-8")) \
        if _audit_log_path().exists() else 0
    manifest = _read_manifest()
    recent_audit = _read_jsonl(_audit_log_path(), limit=10)
    if wstats.get("split_known", True):
        wiki_detail = (
            f"{wstats['skills']:,} skills · {wstats['agents']:,} agents · "
            f"{wstats['mcps']:,} MCPs · {wstats['harnesses']:,} harnesses"
        )
    else:
        wiki_detail = "entity split unavailable; install the current graph index"

    rows = []
    for s in recent:
        sid = s["session_id"]
        rows.append(
            f"<tr>"
            f"<td><a href='/session/{html.escape(sid)}'>{html.escape(sid[:20])}</a></td>"
            f"<td class='muted'>{html.escape(s['last_seen'] or '—')}</td>"
            f"<td>{len(s['skills_loaded'])}</td>"
            f"<td>{len(s['skills_unloaded'])}</td>"
            f"<td>{len(s['agents_loaded'])}</td>"
            f"<td>{s['score_updates']}</td>"
            f"</tr>"
        )

    audit_rows = "".join(
        f"<tr><td class='muted'>{html.escape((r.get('ts') or '')[-8:])}</td>"
        f"<td><span class='pill'>{html.escape(r.get('event', ''))}</span></td>"
        f"<td><a href='/wiki/{html.escape(r.get('subject',''))}'><code>{html.escape(r.get('subject',''))}</code></a></td>"
        f"</tr>"
        for r in reversed(recent_audit)
    )

    body = (
        "<h1>ctx monitor</h1>"
        # ── Stat grid ────────────────────────────────────────────────
        "<div style='display:grid; grid-template-columns:repeat(auto-fit, minmax(180px,1fr));"
        " gap:0.8rem; margin-bottom:1.25rem;'>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Currently loaded</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{_format_count(len(manifest.get('load', [])))}</div>"
        f"<a href='/loaded'>manage →</a></div>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Sidecars</div>"
        "<div id='home-sidecar-count' style='font-size:1.6rem; font-weight:600;'>...</div>"
        "<a href='/skills'>browse →</a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Wiki entities</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{_format_count(wstats['total'])}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>"
        f"{html.escape(wiki_detail)}</span></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Knowledge graph</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{_format_count(gstats['nodes'])}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>{_format_count(gstats['edges'])} edges</span>"
        f" · <a href='/graph'>explore →</a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Runtime checks</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{_format_count(runtime['validations_total'])}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>"
        f"{_format_count(runtime['validation_failures'])} failed / "
        f"{_format_count(runtime['open_escalations_total'])} open escalations</span>"
        f" / <a href='/runtime'>view -></a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Audit events</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{_format_count(audit_lines)}</div>"
        f"<a href='/logs'>view →</a> · <a href='/events'>live →</a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Sessions</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{_format_count(len(sessions))}</div>"
        f"<a href='/sessions'>browse →</a></div>"
        + "</div>"
        # ── Grade distribution ────────────────────────────────────────
        "<div class='card'><strong>Skill quality grades:</strong> "
        + "".join(
            f"<span class='pill grade-{g}' data-home-grade='{g}'>{g}: ...</span> "
            for g in ("A", "B", "C", "D", "F")
        )
        + "<span id='home-grade-total' class='muted'> · total loading</span>"
        "</div>"
        "<script>"
        "(() => {"
        "const fmt = n => Number(n || 0).toLocaleString();"
        "const countEl = document.getElementById('home-sidecar-count');"
        "const totalEl = document.getElementById('home-grade-total');"
        "fetch('/api/grades.json').then(r => r.ok ? r.json() : Promise.reject())"
        ".then(data => {"
        "const grades = data.grades || {};"
        "['A','B','C','D','F'].forEach(g => {"
        "const el = document.querySelector(`[data-home-grade=\"${g}\"]`);"
        "if (el) el.textContent = `${g}: ${fmt(grades[g] || 0)}`;"
        "});"
        "if (countEl) countEl.textContent = fmt(data.total || 0);"
        "if (totalEl) totalEl.textContent = ` · total ${fmt(data.total || 0)}`;"
        "})"
        ".catch(() => {"
        "if (countEl) countEl.textContent = 'open';"
        "if (totalEl) totalEl.textContent = ' · open Skills for counts';"
        "});"
        "})();"
        "</script>"
        # ── Two-column: recent sessions + recent audit ────────────────
        "<div style='display:grid; grid-template-columns:2fr 1fr; gap:1rem;'>"
        f"<div class='card'><strong>Recent sessions</strong> ({_format_count(len(sessions))} total)"
        + ("<table>"
           "<tr><th>Session</th><th>Last seen</th><th>Load</th>"
           "<th>Unload</th><th>Agents</th><th>Scores</th></tr>"
           + "".join(rows)
           + "</table>" if recent else
           "<p class='muted'>No sessions recorded yet. Hooks start logging "
           "once you run a Claude Code session with ctx installed.</p>")
        + "</div>"
        "<div class='card'><strong>Latest audit events</strong>"
        + ("<table>"
           "<tr><th>Time</th><th>Event</th><th>Subject</th></tr>"
           + audit_rows
           + "</table>" if recent_audit else
           "<p class='muted'>No audit events yet.</p>")
        + "</div>"
        "</div>"
    )
    return _layout("Home", body)


def _render_sessions_index() -> str:
    sessions = _summarize_sessions()
    rows = []
    for s in sessions:
        sid = s["session_id"]
        rows.append(
            f"<tr>"
            f"<td><a href='/session/{html.escape(sid)}'><code>{html.escape(sid[:32])}</code></a></td>"
            f"<td class='muted'>{html.escape(s['first_seen'] or '—')}</td>"
            f"<td class='muted'>{html.escape(s['last_seen'] or '—')}</td>"
            f"<td>{len(s['skills_loaded'])}</td>"
            f"<td>{len(s['skills_unloaded'])}</td>"
            f"<td>{len(s['agents_loaded'])}</td>"
            f"<td>{len(s['agents_unloaded'])}</td>"
            f"<td>{len(s['mcps_loaded'])}</td>"
            f"<td>{len(s['mcps_unloaded'])}</td>"
            f"<td>{s['lifecycle_transitions']}</td>"
            f"</tr>"
        )
    body = (
        "<h1>Sessions</h1>"
        f"<p class='muted'>{len(sessions)} unique sessions observed.</p>"
        "<table>"
        "<tr><th>Session</th><th>First seen</th><th>Last seen</th>"
        "<th>Skills↑</th><th>Skills↓</th>"
        "<th>Agents↑</th><th>Agents↓</th>"
        "<th>MCPs↑</th><th>MCPs↓</th><th>Lifecycle</th></tr>"
        + "".join(rows)
        + "</table>"
    )
    return _layout("Sessions", body)


def _render_session_detail(session_id: str) -> str:
    detail = _session_detail(session_id)
    audit = detail["audit_entries"]
    events = detail["load_events"]

    audit_rows = "".join(
        f"<tr><td class='muted'>{html.escape(r.get('ts', ''))}</td>"
        f"<td><span class='pill'>{html.escape(r.get('event', ''))}</span></td>"
        f"<td><code>{html.escape(r.get('subject', ''))}</code></td>"
        f"<td class='muted'>{html.escape(json.dumps(r.get('meta', {}))[:80])}</td></tr>"
        for r in audit
    )
    event_rows = "".join(
        f"<tr><td class='muted'>{html.escape(r.get('timestamp', ''))}</td>"
        f"<td>{html.escape(r.get('event', ''))}</td>"
        f"<td><code>{html.escape(r.get('skill') or r.get('agent') or '')}</code></td></tr>"
        for r in events
    )

    body = (
        f"<h1>Session {html.escape(session_id)}</h1>"
        f"<div class='card'><strong>{len(audit)}</strong> audit entries · "
        f"<strong>{len(events)}</strong> load/unload events</div>"
        "<h2>Audit timeline</h2>"
        "<table><tr><th>ts</th><th>event</th><th>subject</th><th>meta</th></tr>"
        + audit_rows
        + "</table>"
        "<h2>Load/unload events</h2>"
        "<table><tr><th>ts</th><th>event</th><th>subject</th></tr>"
        + event_rows
        + "</table>"
    )
    return _layout(f"Session {session_id}", body)


def _render_skills(qs: dict[str, str] | None = None) -> str:
    payload = _sidecar_page_payload(qs)
    sidecars = payload["items"]

    cards = "".join(
        f"<div class='skill-card' data-slug='{html.escape(s.get('slug', ''))}' "
        f"data-grade='{html.escape(s.get('grade', 'F'))}' "
        f"data-type='{html.escape(_sidecar_entity_type(s))}' "
        f"data-floor='{html.escape(s.get('hard_floor') or '')}' "
        f"style='border:1px solid #e5e7eb; border-radius:6px; padding:0.7rem 0.9rem; "
        f"display:flex; flex-direction:column; gap:0.3rem;'>"
        f"<div style='display:flex; justify-content:space-between; align-items:center;'>"
        f"<code style='font-size:0.85rem;'>{html.escape(s.get('slug', ''))}</code>"
        f"<span class='pill grade-{html.escape(s.get('grade', 'F'))}'>{html.escape(s.get('grade', 'F'))}</span>"
        f"</div>"
        f"<div class='muted' style='font-size:0.78rem;'>"
        f"score {s.get('raw_score', 0.0):.3f} · {html.escape(s.get('type', s.get('subject_type', 'skill')))}"
        f"{' · ' + html.escape(s.get('hard_floor','')) if s.get('hard_floor') else ''}"
        f"</div>"
        f"<div style='display:flex; gap:0.4rem; margin-top:0.2rem;'>"
        f"<a href='/skill/{html.escape(s.get('slug', ''))}?type={html.escape(_sidecar_entity_type(s))}' style='font-size:0.78rem;'>sidecar</a>"
        f"<a href='/wiki/{html.escape(s.get('slug', ''))}?type={html.escape(_sidecar_entity_type(s))}' style='font-size:0.78rem;'>wiki</a>"
        f"<a href='/graph?slug={html.escape(s.get('slug', ''))}&amp;type={html.escape(_sidecar_entity_type(s))}' style='font-size:0.78rem;'>graph</a>"
        f"</div>"
        f"</div>"
        for s in sidecars
    )

    start_index = ((payload["page"] - 1) * payload["limit"]) + 1 if payload["total"] else 0
    end_index = min(payload["page"] * payload["limit"], payload["total"])
    summary = (
        f"Showing {start_index}-{end_index} of {payload['total']} matching sidecars"
        if payload["filtered"]
        else f"Showing {start_index}-{end_index} of {payload['catalog_total']} sidecars"
    )
    query_base = {
        key: value
        for key, value in (qs or {}).items()
        if key not in {"page"}
    }

    def page_href(page: int) -> str:
        params = {
            **query_base,
            "page": str(max(1, page)),
            "limit": str(payload["limit"]),
        }
        query = "&".join(
            f"{quote(str(key))}={quote(str(value))}"
            for key, value in params.items()
            if str(value).strip()
        )
        return "/skills" + (f"?{query}" if query else "")

    prev_link = (
        f"<a href='{html.escape(page_href(payload['page'] - 1))}'>previous</a>"
        if payload["has_prev"]
        else "<span class='muted'>previous</span>"
    )
    next_link = (
        f"<a href='{html.escape(page_href(payload['page'] + 1))}'>next</a>"
        if payload["has_next"]
        else "<span class='muted'>next</span>"
    )
    pagination = (
        "<div class='card' style='display:flex; justify-content:space-between; "
        "align-items:center; gap:1rem;'>"
        f"<span id='match-count' class='muted'>{html.escape(summary)} · page "
        f"{payload['page']} of {payload['pages']}</span>"
        f"<span>{prev_link} · {next_link}</span>"
        "</div>"
    )
    selected_type = ",".join(payload["types"])
    selected_grade = ",".join(payload["grades"])
    type_options = "<option value=''>all types</option>" + "".join(
        f"<option value='{html.escape(t)}'"
        f"{' selected' if selected_type == t else ''}>{html.escape(t)}</option>"
        for t in _DASHBOARD_ENTITY_TYPES
    )
    grade_options = "<option value=''>all grades</option>" + "".join(
        f"<option value='{g}'{' selected' if selected_grade == g else ''}>{g}</option>"
        for g in ("A", "B", "C", "D", "F")
    )
    limit_options = "".join(
        f"<option value='{n}'{' selected' if payload['limit'] == n else ''}>{n}</option>"
        for n in (50, 100, 200, 500)
    )
    hide_checked = " checked" if payload["hide_floor"] else ""

    body = (
        "<h1>Quality sidecars</h1>"
        f"<p class='muted'>{payload['catalog_total']} sidecars · click any card to drill in.</p>"
        + pagination
        + "<div style='display:grid; grid-template-columns:220px 1fr; gap:1.25rem; align-items:start;'>"
        # ── Left filter sidebar ──────────────────────────────────────
        "<aside style='position:sticky; top:1rem;'>"
        "<form class='card' id='skills-filter-form' method='get' action='/skills'>"
        "<strong>Search</strong>"
        f"<input type='text' id='skill-search' name='q' value='{html.escape(payload['q'])}' placeholder='filter by slug...' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<label style='display:block; margin-top:0.6rem; font-size:0.82rem;'>Type"
        f"<select class='type-filter' name='type' style='width:100%; margin-top:0.25rem;'>{type_options}</select>"
        "</label>"
        "<label style='display:block; margin-top:0.6rem; font-size:0.82rem;'>Grade"
        f"<select class='grade-filter' name='grade' style='width:100%; margin-top:0.25rem;'>{grade_options}</select>"
        "</label>"
        "<label style='display:block; margin-top:0.6rem; font-size:0.82rem;'>Limit"
        f"<select name='limit' style='width:100%; margin-top:0.25rem;'>{limit_options}</select>"
        "</label>"
        "<label style='display:block; padding:0.55rem 0 0.35rem;'>"
        f"<input type='checkbox' id='hide-floor' name='hide_floor' value='1'{hide_checked}> hide floored</label>"
        "<button type='submit' style='width:100%;'>apply</button>"
        "</form>"
        "</aside>"
        # ── Card grid ────────────────────────────────────────────────
        "<div id='card-grid' style='display:grid; "
        "grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:0.7rem;'>"
        + cards
        + "</div>"
        "</div>"
        "<script>\n"
        "document.querySelectorAll('#skills-filter-form select').forEach(el => {\n"
        "  el.addEventListener('change', () => el.form.submit());\n"
        "});\n"
        "</script>"
    )
    return _layout("Skills", body)


def _select_options(
    options: list[dict[str, Any]],
    selected: str,
    *,
    all_label: str,
) -> str:
    selected_text = str(selected or "")
    html_options = [f"<option value=''>{html.escape(all_label)}</option>"]
    for option in options:
        value = str(option.get("value") or "")
        count = int(option.get("count") or 0)
        label = f"{value} ({count})"
        is_selected = " selected" if value == selected_text else ""
        html_options.append(
            f"<option value='{html.escape(value)}'{is_selected}>{html.escape(label)}</option>"
        )
    return "".join(html_options)


def _render_skillspector(qs: dict[str, str] | None = None) -> str:
    payload = _skillspector_audit_payload(qs)
    summary = payload["summary"]
    filters = payload["filters"]
    records = payload["records"]

    status_options = _select_options(
        filters["statuses"],
        filters["status"],
        all_label="all statuses",
    )
    severity_options = _select_options(
        filters["severities"],
        filters["severity"],
        all_label="all severities",
    )
    tag_options = _select_options(filters["tags"], filters["tag"], all_label="all tags")
    family_options = _select_options(
        filters["families"],
        filters["family"],
        all_label="all graph families",
    )
    limit_options = "".join(
        f"<option value='{n}'{' selected' if filters['limit'] == n else ''}>{n}</option>"
        for n in (50, 100, 200, 500)
    )
    rows = []
    for row in records:
        tags = ", ".join(str(tag) for tag in row.get("tags", [])[:6]) or "none"
        rules = ", ".join(str(rule) for rule in row.get("issue_rules", [])[:4]) or "none"
        score = row.get("risk_score")
        risk_score = "n/a" if score is None else str(score)
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(str(row['href']))}'><code>{html.escape(str(row['slug']))}</code></a>"
            f"<div class='muted'>{html.escape(str(row.get('title') or ''))}</div></td>"
            f"<td><span class='pill'>{html.escape(str(row['status']))}</span></td>"
            f"<td>{html.escape(str(row['risk_severity']))}<div class='muted'>score {html.escape(risk_score)}</div></td>"
            f"<td>{int(row.get('issues') or 0)} issues<br><span class='muted'>{html.escape(rules)}</span></td>"
            f"<td><span class='muted'>{html.escape(tags)}</span></td>"
            f"<td>{html.escape(str(row.get('family') or 'unknown'))}</td>"
            f"<td>{html.escape(str(row.get('recommendation') or ''))}</td>"
            "</tr>"
        )
    status_counts = summary.get("statuses", {})
    body = (
        "<h1>SkillSpector audit</h1>"
        "<p class='muted'>ctx-run static SkillSpector results for skill bodies. "
        "This is a local ctx audit, not NVIDIA endorsement or certification. "
        f"<a href='/api/skillspector.json'>JSON</a></p>"
        "<div class='metric-grid'>"
        f"<div class='metric-card'><strong>{summary['total']:,}</strong><span>scanned records</span></div>"
        f"<div class='metric-card'><strong>{summary['problematic']:,}</strong><span>problematic</span></div>"
        f"<div class='metric-card'><strong>{int(status_counts.get('blocked', 0)):,}</strong><span>blocked</span></div>"
        f"<div class='metric-card'><strong>{int(status_counts.get('findings', 0)):,}</strong><span>with findings</span></div>"
        f"<div class='metric-card'><strong>{int(status_counts.get('not_scanned_no_body', 0)):,}</strong><span>no body</span></div>"
        "</div>"
        "<div style='display:grid; grid-template-columns:260px 1fr; gap:1.25rem; align-items:start;'>"
        "<aside style='position:sticky; top:1rem;'>"
        "<form class='card' method='get' action='/skillspector'>"
        "<strong>Filters</strong>"
        f"<input type='search' name='q' value='{html.escape(str(filters['query']))}' "
        "placeholder='search slug, rule, tag...' "
        "style='width:100%; margin-top:0.5rem; padding:0.4rem 0.5rem;'>"
        "<label style='display:block; margin-top:0.6rem;'>Status"
        f"<select name='status' style='width:100%; margin-top:0.25rem;'>{status_options}</select></label>"
        "<label style='display:block; margin-top:0.6rem;'>Severity"
        f"<select name='severity' style='width:100%; margin-top:0.25rem;'>{severity_options}</select></label>"
        "<label style='display:block; margin-top:0.6rem;'>Tag"
        f"<select name='tag' style='width:100%; margin-top:0.25rem;'>{tag_options}</select></label>"
        "<label style='display:block; margin-top:0.6rem;'>Graph family"
        f"<select name='family' style='width:100%; margin-top:0.25rem;'>{family_options}</select></label>"
        "<label style='display:block; margin-top:0.6rem;'>Limit"
        f"<select name='limit' style='width:100%; margin-top:0.25rem;'>{limit_options}</select></label>"
        "<button type='submit' style='width:100%; margin-top:0.75rem;'>apply</button>"
        f"<p class='muted' style='margin-top:0.75rem;'>source: <code>{html.escape(str(payload['audit_path']))}</code></p>"
        "</form>"
        "</aside>"
        "<section class='card'>"
        f"<strong>{summary['visible']:,}</strong> matching records; showing {summary['returned']:,}."
        "<table class='frontmatter-table' style='margin-top:0.75rem;'>"
        "<tr><th>Skill</th><th>Status</th><th>Risk</th><th>Issues</th><th>Tags</th><th>Family</th><th>Recommendation</th></tr>"
        + ("".join(rows) if rows else "<tr><td colspan='7' class='muted'>No matching records.</td></tr>")
        + "</table>"
        "</section>"
        "</div>"
        "<script>\n"
        "document.querySelectorAll('form select').forEach(el => el.addEventListener('change', () => el.form.submit()));\n"
        "</script>"
    )
    return _layout("SkillSpector", body)


def _render_skill_detail(slug: str, entity_type: str | None = None) -> str:
    sidecar = _load_sidecar(slug, entity_type=entity_type)
    if sidecar is None:
        return _layout(slug, f"<h1>{html.escape(slug)}</h1><p>No sidecar.</p>")
    requested_type = (
        _normalize_dashboard_entity_type(entity_type)
        or _sidecar_entity_type(sidecar)
    )
    audit = [r for r in _read_jsonl(_audit_log_path())
             if r.get("subject") == slug and _audit_entity_type(r) == requested_type]
    audit_rows = "".join(
        f"<tr><td class='muted'>{html.escape(r.get('ts', ''))}</td>"
        f"<td><span class='pill'>{html.escape(r.get('event', ''))}</span></td>"
        f"<td class='muted'>{html.escape(r.get('actor', ''))}</td></tr>"
        for r in audit[-100:]
    )
    hard_floor = sidecar.get("hard_floor")
    hard_floor_html = (
        f" · floor {html.escape(str(hard_floor))}" if hard_floor else ""
    )
    body = (
        f"<h1>{html.escape(slug)}</h1>"
        f"<div class='card'>"
        f"<span class='pill grade-{html.escape(sidecar.get('grade', 'F'))}'>grade {html.escape(sidecar.get('grade', 'F'))}</span> "
        f"score <strong>{sidecar.get('raw_score', 0.0):.3f}</strong> "
        f"<span class='muted'>· type {html.escape(sidecar.get('subject_type', ''))}"
        f"{hard_floor_html}</span>"
        "</div>"
        "<h2>Sidecar</h2>"
        f"<pre>{html.escape(json.dumps(sidecar, indent=2)[:4000])}</pre>"
        f"<h2>Audit timeline ({len(audit)} entries)</h2>"
        "<table><tr><th>ts</th><th>event</th><th>actor</th></tr>"
        + audit_rows
        + "</table>"
    )
    return _layout(slug, body)


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
        G = _load_dashboard_graph() if allow_load else _GRAPH_CACHE_VALUE
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
    try:
        from ctx_config import _read_default_config  # type: ignore

        raw = _read_default_config()
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        path = Path(__file__).with_name("config.json")
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


def _read_user_config_raw() -> dict[str, Any]:
    path = _user_config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_merge_config(base[key], value)
        else:
            base[key] = value


def _config_value(raw: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = raw
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _set_config_value(raw: dict[str, Any], path: str, value: Any) -> None:
    current = raw
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _delete_config_value(raw: dict[str, Any], path: str) -> None:
    current = raw
    parts = path.split(".")
    parents: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        parents.append((current, part))
        current = child
    current.pop(parts[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)


def _config_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {"group": "Knowledge", "path": "knowledge.mode", "type": "choice", "choices": ("shipped", "local", "enriched"), "required": True, "label": "Knowledge source mode", "help": "shipped uses ctx's packaged graph/wiki, local stays private, enriched starts from shipped knowledge and adds your own.", "example": "enriched"},
        {"group": "Recommendation", "path": "resolver.recommendation_top_k", "type": "int", "min": 1, "max": 5, "required": True, "label": "Max mixed recommendations", "help": "Hard cap for the combined skills/agents/MCP recommendation bundle.", "example": 5},
        {"group": "Recommendation", "path": "resolver.recommendation_min_normalized_score", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Minimum recommendation score", "help": "Drops weak skill/agent/MCP matches instead of recommending at all cost.", "example": 0.30},
        {"group": "Recommendation", "path": "resolver.max_skills", "type": "int", "min": 1, "max": 50, "label": "Resolver hard skill ceiling", "help": "Maximum load candidates considered by a resolver call.", "example": 15},
        {"group": "Harness", "path": "harness.recommendation_min_fit_score", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Minimum harness fit score", "help": "Custom/API/local model users only see harnesses at or above this fit floor.", "example": 0.85},
        {"group": "Harness", "path": "harness.recommendation_min_normalized_score", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Harness normalized score floor", "help": "Compatibility display floor for older configs.", "example": 0.85},
        {"group": "Micro-skills", "path": "skill_transformer.line_threshold", "type": "int", "min": 1, "max": 2000, "required": True, "label": "Micro-skill line threshold", "help": "Any SKILL.md above this many lines triggers the micro-skills conversion gate.", "example": 180},
        {"group": "Micro-skills", "path": "skill_transformer.max_stage_lines", "type": "int", "min": 1, "max": 300, "label": "Max staged reference lines", "help": "Target maximum lines for each generated reference stage.", "example": 40},
        {"group": "Micro-skills", "path": "skill_transformer.stage_count", "type": "int", "min": 1, "max": 20, "label": "Stage count", "help": "Target number of staged references for long skills.", "example": 5},
        {"group": "Graph", "path": "graph.min_edge_weight", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Minimum final edge weight", "help": "Edges below this blended score are dropped from graph.json during rebuild.", "example": 0.03},
        {"group": "Graph", "path": "graph.edge_weights.semantic", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Semantic edge weight", "help": "Semantic portion of the blended edge score. Semantic/tags/slug tokens should sum to 1.", "example": 0.70},
        {"group": "Graph", "path": "graph.edge_weights.tags", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Tag edge weight", "help": "Tag-overlap portion of the blended edge score.", "example": 0.15},
        {"group": "Graph", "path": "graph.edge_weights.slug_tokens", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Slug-token edge weight", "help": "Slug-token overlap portion of the blended edge score.", "example": 0.15},
        {"group": "Graph", "path": "graph.semantic.top_k", "type": "int", "min": 1, "max": 200, "label": "Semantic neighbors per entity", "help": "Maximum nearest semantic neighbors retained per entity during graph build.", "example": 20},
        {"group": "Graph", "path": "graph.semantic.build_floor", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Semantic build floor", "help": "Low inclusion bar used when graph embeddings are rebuilt.", "example": 0.50},
        {"group": "Graph", "path": "graph.semantic.min_cosine", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Semantic display floor", "help": "Read-time semantic filter. Raising this is stricter without forcing a rebuild.", "example": 0.80},
        {"group": "Graph", "path": "graph.tag_edges.dense_tag_threshold", "type": "int", "min": 1, "max": 10000, "label": "Dense tag cutoff", "help": "Tags shared by more than this many entities do not create broad noisy cliques.", "example": 500},
        {"group": "Graph", "path": "graph.token_edges.dense_token_threshold", "type": "int", "min": 1, "max": 10000, "label": "Dense slug-token cutoff", "help": "Slug words shared by too many entities are ignored as edge creators.", "example": 30},
        {"group": "Intake", "path": "intake.enabled", "type": "bool", "required": True, "label": "Intake quality gate", "help": "Runs duplicate/near-duplicate and body-quality checks when entities are added or updated.", "example": True},
        {"group": "Intake", "path": "intake.dup_threshold", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Duplicate threshold", "help": "Similarity at or above this is treated as a duplicate.", "example": 0.93},
        {"group": "Intake", "path": "intake.near_dup_threshold", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Near-duplicate threshold", "help": "Similarity at or above this asks the user to update/merge instead of blindly adding.", "example": 0.80},
        {"group": "Paths", "path": "paths.wiki_dir", "type": "str", "required": True, "label": "Wiki directory", "help": "Runtime llm-wiki directory used by dashboard, graph, and recommendation flows.", "example": "~/.claude/skill-wiki"},
        {"group": "Paths", "path": "paths.skills_dir", "type": "str", "required": True, "label": "Skills directory", "help": "Installed local skills directory.", "example": "~/.claude/skills"},
        {"group": "Paths", "path": "paths.agents_dir", "type": "str", "required": True, "label": "Agents directory", "help": "Installed local agents directory.", "example": "~/.claude/agents"},
    )


_CONFIG_REMOVE = object()


def _coerce_config_value(spec: dict[str, Any], raw_value: Any) -> Any:
    if raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == ""):
        return _CONFIG_REMOVE
    kind = spec.get("type", "str")
    if kind == "bool":
        if isinstance(raw_value, bool):
            return raw_value
        text = str(raw_value).strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"{spec['path']} must be true or false")
    if kind == "int":
        if isinstance(raw_value, bool):
            raise ValueError(f"{spec['path']} must be an integer")
        value: int | float = 0
        value = int(raw_value)
    elif kind == "float":
        if isinstance(raw_value, bool):
            raise ValueError(f"{spec['path']} must be a number")
        value = float(raw_value)
    elif kind == "choice":
        choice_value = str(raw_value).strip()
        if choice_value not in spec.get("choices", ()):
            raise ValueError(f"{spec['path']} must be one of {spec.get('choices')}")
        return choice_value
    else:
        text_value = str(raw_value).strip()
        return text_value if text_value else _CONFIG_REMOVE
    if "min" in spec and value < spec["min"]:
        raise ValueError(f"{spec['path']} must be >= {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ValueError(f"{spec['path']} must be <= {spec['max']}")
    return value


def _effective_config_payload() -> dict[str, Any]:
    defaults = _read_default_config_raw()
    user = _read_user_config_raw()
    effective = json.loads(json.dumps(defaults))
    _deep_merge_config(effective, user)
    return {
        "defaults": defaults,
        "user": user,
        "effective": effective,
        "path": str(_user_config_path()),
    }


def _save_config_updates(updates: dict[str, Any]) -> dict[str, Any]:
    specs = {spec["path"]: spec for spec in _config_field_specs()}
    unknown = sorted(set(updates) - set(specs))
    if unknown:
        return {"ok": False, "detail": f"unknown config keys: {', '.join(unknown)}"}
    user_config = _read_user_config_raw()
    try:
        for path, raw_value in updates.items():
            value = _coerce_config_value(specs[path], raw_value)
            if value is _CONFIG_REMOVE:
                _delete_config_value(user_config, path)
            else:
                _set_config_value(user_config, path, value)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "detail": str(exc)}
    config_path = _user_config_path()
    with file_lock(config_path):
        _atomic_write_text(
            config_path,
            json.dumps(user_config, indent=2, sort_keys=True) + "\n",
        )
    return {"ok": True, "detail": f"saved {len(updates)} config keys"}


def _render_config() -> str:
    payload = _effective_config_payload()
    effective = payload["effective"]
    user = payload["user"]
    rows_by_group: dict[str, list[str]] = defaultdict(list)
    for spec in _config_field_specs():
        path = spec["path"]
        value = _config_value(effective, path, "")
        default = _config_value(payload["defaults"], path, "")
        user_value = _config_value(user, path, _CONFIG_REMOVE)
        is_override = user_value is not _CONFIG_REMOVE
        required = bool(spec.get("required"))
        req_html = " <span class='pill grade-A'>Required</span>" if required else ""
        help_text = html.escape(str(spec.get("help", "")))
        control_value = (
            "true" if value is True
            else "false" if value is False
            else str(value)
        )
        default_html = html.escape(json.dumps(default) if not isinstance(default, str) else default)
        example_value = spec.get("example")
        example_html = html.escape(json.dumps(example_value) if not isinstance(example_value, str) else str(example_value))
        common_attrs = (
            f"name='{html.escape(path)}' data-config-path='{html.escape(path)}' "
            f"data-original-value='{html.escape(control_value)}' "
            f"data-default='{default_html}' {'required' if required else ''}"
        )
        if spec.get("type") == "choice":
            options = "".join(
                f"<option value='{html.escape(str(choice))}' {'selected' if str(value) == str(choice) else ''}>"
                f"{html.escape(str(choice))}</option>"
                for choice in spec.get("choices", ())
            )
            control = f"<select {common_attrs}>{options}</select>"
        elif spec.get("type") == "bool":
            control = (
                f"<select {common_attrs}>"
                f"<option value='true' {'selected' if bool(value) else ''}>true</option>"
                f"<option value='false' {'selected' if not bool(value) else ''}>false</option>"
                "</select>"
            )
        elif spec.get("type") in {"int", "float"}:
            step = spec.get("step", 1 if spec.get("type") == "int" else 0.01)
            control = (
                f"<input type='number' {common_attrs} "
                f"min='{html.escape(str(spec.get('min', '')))}' "
                f"max='{html.escape(str(spec.get('max', '')))}' "
                f"step='{html.escape(str(step))}' "
                f"value='{html.escape(str(value))}' placeholder='{default_html}'>"
            )
        else:
            control = (
                f"<input type='text' {common_attrs} "
                f"value='{html.escape(str(value))}' placeholder='{default_html}'>"
            )
        override_html = (
            "<span class='pill grade-B'>override</span>"
            if is_override
            else "<span class='muted'>default</span>"
        )
        clear_html = (
            f"<label style='display:inline-flex; align-items:center; gap:0.35rem; "
            f"margin-top:0.45rem;'>"
            f"<input type='checkbox' data-config-clear='{html.escape(path)}'>"
            "remove user override on save</label>"
            if is_override
            else ""
        )
        rows_by_group[str(spec["group"])].append(
            "<div class='card' style='margin:0 0 0.75rem 0;'>"
            f"<label><strong>{html.escape(str(spec['label']))}</strong>{req_html}<br>"
            f"<code>{html.escape(path)}</code></label>"
            f"<div style='margin-top:0.45rem;'>{control}</div>"
            f"{clear_html}"
            f"<p class='muted' style='margin-bottom:0;'>{help_text}<br>"
            f"Default: <code>{default_html}</code> · Example: <code>{example_html}</code> · "
            f"{override_html}</p>"
            "</div>"
        )
    group_html = "".join(
        "<section style='margin-bottom:1rem;'>"
        f"<h2>{html.escape(group)}</h2>"
        + "".join(rows)
        + "</section>"
        for group, rows in rows_by_group.items()
    )
    token = _MONITOR_TOKEN or ""
    body = (
        "<h1>Config</h1>"
        "<p class='muted'>Edit ctx runtime defaults from the dashboard. Saves only changed fields. For existing overrides, use remove user override to fall back to the shipped default. Important fields are marked Required.</p>"
        f"<p class='muted'>User config: <code>{html.escape(payload['path'])}</code></p>"
        "<form id='config-form'>"
        + group_html
        + "<div class='card' style='position:sticky; bottom:0; background:rgba(255,255,255,0.96);'>"
        "<button type='submit'>save config</button> "
        "<button type='button' id='config-reset'>reset form to effective values</button> "
        "<span id='config-msg' class='muted'></span>"
        "</div></form>"
        "<script>\n"
        f"const CTX_MONITOR_TOKEN = {json.dumps(token)};\n"
        "const form = document.getElementById('config-form');\n"
        "const msg = document.getElementById('config-msg');\n"
        "form.addEventListener('submit', async (ev) => {\n"
        "  ev.preventDefault();\n"
        "  const updates = {};\n"
        "  const clears = new Set(Array.from(form.querySelectorAll('[data-config-clear]:checked')).map(el => el.dataset.configClear));\n"
        "  form.querySelectorAll('[data-config-path]').forEach(el => {\n"
        "    const path = el.dataset.configPath;\n"
        "    if (clears.has(path)) updates[path] = '';\n"
        "    else if (String(el.value) !== String(el.dataset.originalValue || '')) updates[path] = el.value;\n"
        "  });\n"
        "  if (Object.keys(updates).length === 0) { msg.textContent = 'no config changes to save'; return; }\n"
        "  msg.textContent = 'saving...';\n"
        "  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json', 'X-CTX-Monitor-Token':CTX_MONITOR_TOKEN}, body: JSON.stringify({updates})});\n"
        "  let body = {}; try { body = await r.json(); } catch (_) {}\n"
        "  msg.textContent = r.ok && body.ok ? body.detail : ('failed: ' + (body.detail || r.statusText));\n"
        "});\n"
        "document.getElementById('config-reset').addEventListener('click', () => location.reload());\n"
        "</script>"
    )
    return _layout("Config", body)


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
    focus_slug = focus or ""
    gstats = _graph_stats()
    seeds = (
        _top_degree_seeds(allow_load=False)
        if not focus_slug and gstats.get("available")
        else []
    )
    initial_slug = focus_slug
    initial_type = focus_type or ""
    if not initial_slug and seeds:
        initial_slug = str(seeds[0].get("slug") or "")
        initial_type = str(seeds[0].get("type") or "")
    elif not initial_slug and gstats.get("available"):
        initial_slug = _DEFAULT_GRAPH_FOCUS_SLUG
    focus_js = _json_for_script(initial_slug)
    focus_type_js = _json_for_script(initial_type)
    match_default_min = _graph_match_default_min_percent()
    seed_html = ""
    if seeds:
        chips = "".join(
            f"<a href='/graph?slug={html.escape(s['slug'])}&amp;type={html.escape(s['type'])}' "
            f"style='display:inline-block; margin:0.2rem 0.25rem; padding:0.25rem 0.6rem; "
            f"border-radius:999px; background:{'#fef3c7' if s['type']=='agent' else '#fee2e2' if s['type']=='mcp-server' else '#dcfce7' if s['type']=='harness' else '#e0e7ff'}; "
            f"color:#111; font-size:0.82rem; text-decoration:none;'>"
            f"<code style='background:transparent;'>{html.escape(s['slug'])}</code> "
            f"<span class='muted' style='font-size:0.72rem;'>· deg {_format_count(s['degree'])}</span>"
            f"</a>"
            for s in seeds
        )
        seed_html = (
            "<div class='card'><strong>Popular seed slugs</strong> "
            "<span class='muted' style='font-size:0.8rem;'>"
            "(click to explore 1-hop neighborhood)</span>"
            f"<div style='margin-top:0.4rem;'>{chips}</div></div>"
        )
    stats_html = (
        f"<span class='muted'>{gstats.get('nodes', 0):,} nodes · "
        f"{gstats.get('edges', 0):,} edges</span>"
    )
    body = (
        "<h1>Knowledge graph</h1>"
        f"<p class='muted'>Enter an entity slug to explore its 1-hop "
        f"neighborhood. Edges blend semantic + tag + slug-token "
        f"signals (weight = final_weight). {stats_html}</p>"
        + seed_html
        # Two-column layout — filter sidebar on the left (mirrors /wiki),
        # graph list on the right. Client-side JS hides nodes by
        # type + tag without hitting the server so a user can carve out
        # a subgraph without rebuilding anything.
        + "<div style='display:grid; grid-template-columns:240px 1fr; "
          "gap:1rem; align-items:start; margin-top:1rem;'>"
        # Left sidebar
        "<aside style='position:sticky; top:1rem;'>"
        "<div class='card'><strong>Focus</strong>"
        "<input type='text' id='focus' "
        "placeholder='skill / agent / mcp / harness slug' "
        f"value='{html.escape(initial_slug)}' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<div id='graph-live-results' data-testid=\"graph-live-results\" "
        "class='graph-live-results' hidden></div>"
        "<select id='focus-type' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<option value=''>auto type</option>"
        f"<option value='skill' {'selected' if initial_type == 'skill' else ''}>skill</option>"
        f"<option value='agent' {'selected' if initial_type == 'agent' else ''}>agent</option>"
        f"<option value='mcp-server' {'selected' if initial_type == 'mcp-server' else ''}>mcp-server</option>"
        f"<option value='harness' {'selected' if initial_type == 'harness' else ''}>harness</option>"
        "</select>"
        "<button id='go' style='margin-top:0.4rem; width:100%;'>"
        "explore</button></div>"
        "<div class='card'><strong>Type</strong>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='skill' checked> skill</span>"
        "<span class='muted' id='graph-count-skill' style='font-size:0.78rem;'>—</span></label>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='agent' checked> agent</span>"
        "<span class='muted' id='graph-count-agent' style='font-size:0.78rem;'>—</span></label>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='mcp-server' checked> mcp-server</span>"
        "<span class='muted' id='graph-count-mcp-server' style='font-size:0.78rem;'>—</span></label>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='harness' checked> harness</span>"
        "<span class='muted' id='graph-count-harness' style='font-size:0.78rem;'>-</span></label>"
        "</div>"
        "<div class='card graph-match-card'><strong>Match</strong>"
        "<div class='graph-match-summary'>"
        "<span id='match-filter-min-value' class='pill'>"
        f"{match_default_min}%</span>"
        "<span class='muted'>-</span>"
        "<span id='match-filter-max-value' class='pill'>100%</span></div>"
        "<div id='match-histogram' data-testid=\"match-histogram\" "
        "class='graph-match-histogram' aria-label='Match distribution'></div>"
        "<div class='graph-range-wrap' data-testid=\"match-range-control\">"
        "<div class='graph-range-track'></div>"
        "<div id='match-range-fill' class='graph-range-fill'></div>"
        "<input type='range' id='match-filter-min' aria-label='minimum match' "
        "min='0' max='100' step='1' "
        f"value='{match_default_min}'>"
        "<input type='range' id='match-filter-max' aria-label='maximum match' "
        "min='0' max='100' step='1' value='100'>"
        "</div>"
        "<div class='graph-range-scale'><span>0%</span><span>100%</span></div>"
        "<p class='muted' style='font-size:0.72rem; margin:0.35rem 0 0 0;'>"
        "Shows nodes with at least one relationship inside this match range.</p></div>"
        "<div class='card'><strong>Tag filter</strong>"
        "<input type='text' id='tag-filter' "
        "placeholder='shared_tag or slug_token' "
        "style='width:100%; margin-top:0.4rem; padding:0.3rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<p class='muted' style='font-size:0.72rem; margin:0.4rem 0 0 0;'>"
        "Filters nodes by tag substring (client-side).</p></div>"
        "<div class='card'>"
        "<span id='graph-match-count' class='muted'>—</span>"
        "</div>"
        "<div class='card'><span id='msg' class='muted'></span></div>"
        "<div class='card'><strong>Why this view?</strong>"
        "<p id='graph-explanation' class='muted' "
        "style='font-size:0.78rem; margin:0.45rem 0 0 0;'>"
        "Search a slug to see why ctx picked this neighborhood and how to read it."
        "</p></div>"
        "</aside>"
        # Right: graph list panel
        "<div id='cy' class='graph-stage'></div>"
        "</div>"
        "<script>\n"
        f"const initial = {focus_js};\n"
        f"const initialType = {focus_type_js};\n"
        "const cyMount = document.getElementById('cy');\n"
        "const focus = document.getElementById('focus');\n"
        "const focusType = document.getElementById('focus-type');\n"
        "const liveSearchBox = document.getElementById('graph-live-results');\n"
        "let liveSearchTimer = null;\n"
        "let liveSearchAbort = null;\n"
        "let currentGraphQuery = null;\n"
        "let graphReturnStack = [];\n"
        "let pendingDrillKey = '';\n"
        "function nodeColor(type) {\n"
        "  if (type === 'agent') return '#f59e0b';\n"
        "  if (type === 'mcp-server') return '#ef4444';\n"
        "  if (type === 'harness') return '#22c55e';\n"
        "  return '#6366f1';\n"
        "}\n"
        "function escapeHtml(s) { return String(s).replace(/[&<>\"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch])); }\n"
        "function dataAttr(el, name) { return el.getAttribute(name) || ''; }\n"
        "function fmtCount(n) { return Number(n || 0).toLocaleString(); }\n"
        "function rawNodeSlug(id) { return String(id || '').replace(/^(skill|agent|mcp-server|harness):/, ''); }\n"
        "function displaySlug(slug) { return String(slug || '').replace(/^skills-sh-/, ''); }\n"
        "function nodeSlug(id) { return displaySlug(rawNodeSlug(id)); }\n"
        "function nodeDomId(id) { return 'graph-node-' + String(id || '').replace(/[^a-zA-Z0-9_-]/g, '_'); }\n"
        "function graphQueryKey(q) { return q ? (String(q.slug || '') + '|' + String(q.type || '')) : ''; }\n"
        "function sameGraphQuery(a, b) { return graphQueryKey(a) === graphQueryKey(b); }\n"
        "function graphQueryFromNodeData(d) { return {slug: rawNodeSlug(d.id), type: d.type || ''}; }\n"
        "function graphQueryFromPayload(g, requestedSlug, requestedType) {\n"
        "  const centerId = g.center || '';\n"
        "  const centerData = (g.nodes || []).map(n => n.data || {}).find(d => d.id === centerId) || {};\n"
        "  const resolved = g.resolved || {};\n"
        "  return {slug: resolved.slug || requestedSlug || rawNodeSlug(centerId), type: resolved.type || centerData.type || requestedType || ''};\n"
        "}\n"
        "function wikiHref(data) {\n"
        "  const nodeType = data.type || '';\n"
        "  const suffix = nodeType ? '?type=' + encodeURIComponent(nodeType) : '';\n"
        "  return '/wiki/' + encodeURIComponent(rawNodeSlug(data.id)) + suffix;\n"
        "}\n"
        "function renderFallback(g) {\n"
        "  const nodes = g.nodes || [];\n"
        "  const rows = nodes.map(n => {\n"
        "    const d = n.data || {};\n"
        "    const tags = Array.from(d.filter_tokens || d.tags || []).join(' ');\n"
        "    const typeKey = ['skill', 'agent', 'mcp-server', 'harness'].includes(d.type) ? d.type : 'entity';\n"
        "    return '<a data-testid=\"graph-fallback-node\" class=\"graph-fallback-node\" '\n"
        "      + 'data-node-id=\"' + escapeHtml(d.id || '') + '\" '\n"
        "      + 'data-slug=\"' + escapeHtml(nodeSlug(d.id)) + '\" '\n"
        "      + 'data-type=\"' + escapeHtml(d.type || '') + '\" '\n"
        "      + 'data-depth=\"' + escapeHtml(d.depth || 0) + '\" '\n"
        "      + 'data-tags=\"' + escapeHtml(tags.toLowerCase()) + '\" '\n"
        "      + 'href=\"' + escapeHtml(wikiHref(d)) + '\" '\n"
        "      + 'style=\"display:flex; justify-content:space-between; gap:0.75rem; padding:0.45rem 0.6rem; border-bottom:1px solid #e5e7eb; color:inherit; text-decoration:none;\">'\n"
        "      + '<span class=\"graph-fallback-label\">' + escapeHtml(d.label || nodeSlug(d.id)) + '</span>'\n"
        "      + '<span class=\"pill entity-type-' + escapeHtml(typeKey) + '\">' + escapeHtml(d.type || 'entity') + '</span></a>';\n"
        "  }).join('');\n"
        "  cyMount.innerHTML = '<div data-testid=\"graph-fallback\" style=\"padding:0.75rem; height:100%; overflow:auto;\">'\n"
        "    + '<div class=\"muted\" style=\"margin-bottom:0.5rem;\">Showing list view.</div>'\n"
        "    + rows + '</div>';\n"
        "}\n"
        "function nodeDetail(d) {\n"
        "  const tags = Array.from(d.tags || []).join(', ') || 'none';\n"
        "  const size = d.node_size == null ? 'auto' : Number(d.node_size).toFixed(1);\n"
        "  const sizeReason = d.size_reason ? ' · size: ' + size + ' (' + d.size_reason + ')' : ' · size: ' + size;\n"
        "  const desc = d.description ? ' · ' + d.description : '';\n"
        "  return (d.label || nodeSlug(d.id)) + ' · ' + (d.type || 'entity') + desc + ' · tags: ' + tags + ' · quality: ' + qualityText(d.quality_score) + ' · usage: ' + usageText(d.usage_score) + sizeReason;\n"
        "}\n"
        "function nodeRadius(d, isCenter, scale) {\n"
        "  const base = Math.max(isCenter ? 14 : 8, Math.min(24, Number(d.node_size || (isCenter ? 16 : 11))));\n"
        "  const perspective = Math.max(0.8, Math.min(1.2, Number(scale || 1)));\n"
        "  return Math.max(8, Math.min(28, base * perspective));\n"
        "}\n"
        "function polygonPoints(cx, cy, points) {\n"
        "  return points.map(p => (cx + p[0]).toFixed(1) + ',' + (cy + p[1]).toFixed(1)).join(' ');\n"
        "}\n"
        "function nodeShapeSvg(d, p, r) {\n"
        "  const fill = nodeColor(d.type);\n"
        "  const common = ' data-testid=\"graph-svg-node\" data-node-shape=\"' + escapeHtml(d.type || 'skill') + '\" data-radius=\"' + r.toFixed(1) + '\" fill=\"' + fill + '\" stroke=\"#fff\" stroke-width=\"2\"';\n"
        "  if (d.type === 'agent') {\n"
        "    return '<polygon' + common + ' points=\"' + polygonPoints(p.x, p.y, [[0, -r], [r, 0], [0, r], [-r, 0]]) + '\" />';\n"
        "  }\n"
        "  if (d.type === 'mcp-server') {\n"
        "    const side = (r * 1.62).toFixed(1);\n"
        "    return '<rect' + common + ' x=\"' + (p.x - r * 0.81).toFixed(1) + '\" y=\"' + (p.y - r * 0.81).toFixed(1) + '\" width=\"' + side + '\" height=\"' + side + '\" rx=\"4\" />';\n"
        "  }\n"
        "  if (d.type === 'harness') {\n"
        "    return '<polygon' + common + ' points=\"' + polygonPoints(p.x, p.y, [[0, -r], [r * 0.87, -r * 0.5], [r * 0.87, r * 0.5], [0, r], [-r * 0.87, r * 0.5], [-r * 0.87, -r * 0.5]]) + '\" />';\n"
        "  }\n"
        "  return '<circle' + common + ' cx=\"' + p.x.toFixed(1) + '\" cy=\"' + p.y.toFixed(1) + '\" r=\"' + r + '\" />';\n"
        "}\n"
        "function edgeDetail(d) {\n"
        "  const tags = Array.from(d.shared_tags || []).join(', ') || 'none';\n"
        "  const reasons = Array.from(d.reasons || d.edge_reasons || []).join(', ') || (edgeHasEvidence(d) ? 'graph signal' : 'none');\n"
        "  const prefix = isWeakGraphOnlyEdge(d) ? 'Low-confidence auto link: ' : 'Relationship: ';\n"
        "  return prefix + nodeSlug(d.source) + ' ↔ ' + nodeSlug(d.target) + ' · ' + edgeStrengthText(d) + ' · shared: ' + tags + ' · evidence: ' + reasons;\n"
        "}\n"
        "function graphExplanation(g) {\n"
        "  const e = g.explanations || {};\n"
        "  return [e.focus, e.source, e.search, e.layout, e.edges].filter(Boolean).join(' ');\n"
        "}\n"
        "let currentGraphState = null;\n"
        "let currentGraphEdges = [];\n"
        "function numericValue(value) {\n"
        "  if (value == null || value === '') return null;\n"
        "  const n = Number(value);\n"
        "  return Number.isFinite(n) ? n : null;\n"
        "}\n"
        "function clampedUnit(value) {\n"
        "  const n = numericValue(value);\n"
        "  if (n == null) return null;\n"
        "  return Math.max(0, Math.min(1, n));\n"
        "}\n"
        "function percentText(value) {\n"
        "  const n = clampedUnit(value);\n"
        "  return n == null ? 'unknown' : Math.round(n * 100) + '%';\n"
        "}\n"
        "function qualityText(value) {\n"
        "  const raw = numericValue(value);\n"
        "  if (raw == null) return 'not scored yet';\n"
        "  const suffix = raw < 0 || raw > 1 ? ' (raw score clamped)' : '';\n"
        "  return percentText(raw) + suffix;\n"
        "}\n"
        "function usageText(value) {\n"
        "  return numericValue(value) == null ? 'no telemetry yet' : percentText(value);\n"
        "}\n"
        "function scoreText(value) {\n"
        "  return clampedUnit(value) == null ? 'unknown' : percentText(value);\n"
        "}\n"
        "function currentMatchWindow() {\n"
        "  const minInput = document.getElementById('match-filter-min');\n"
        "  const maxInput = document.getElementById('match-filter-max');\n"
        "  const matchA = Math.max(0, Math.min(100, Number(minInput?.value || 0)));\n"
        "  const matchB = Math.max(0, Math.min(100, Number(maxInput?.value || 100)));\n"
        "  const lowPct = Math.min(matchA, matchB);\n"
        "  const highPct = Math.max(matchA, matchB);\n"
        "  return {lowPct, highPct, min: lowPct / 100, max: highPct / 100};\n"
        "}\n"
        "function renderMatchRange(matchWindow) {\n"
        "  document.getElementById('match-filter-min-value').textContent = Math.round(matchWindow.lowPct) + '%';\n"
        "  document.getElementById('match-filter-max-value').textContent = Math.round(matchWindow.highPct) + '%';\n"
        "  const fill = document.getElementById('match-range-fill');\n"
        "  if (fill) {\n"
        "    fill.style.left = matchWindow.lowPct + '%';\n"
        "    fill.style.width = Math.max(0, matchWindow.highPct - matchWindow.lowPct) + '%';\n"
        "  }\n"
        "}\n"
        "function setMatchWindow(lowPct, highPct) {\n"
        "  const minInput = document.getElementById('match-filter-min');\n"
        "  const maxInput = document.getElementById('match-filter-max');\n"
        "  if (!minInput || !maxInput) return;\n"
        "  minInput.value = String(Math.max(0, Math.min(100, Number(lowPct || 0))));\n"
        "  maxInput.value = String(Math.max(0, Math.min(100, Number(highPct || 100))));\n"
        "  applyFilters();\n"
        "}\n"
        "function renderMatchHistogram(edges, matchWindow) {\n"
        "  const box = document.getElementById('match-histogram');\n"
        "  if (!box) return;\n"
        "  const bins = Array(10).fill(0);\n"
        "  (edges || []).forEach(edge => {\n"
        "    const d = edge.data || edge || {};\n"
        "    const weight = clampedUnit(d.weight);\n"
        "    if (weight == null) return;\n"
        "    const idx = Math.min(9, Math.max(0, Math.floor(Math.min(100, weight * 100) / 10)));\n"
        "    bins[idx] += 1;\n"
        "  });\n"
        "  const maxBin = Math.max(1, ...bins);\n"
        "  box.innerHTML = bins.map((count, idx) => {\n"
        "    const start = idx * 10;\n"
        "    const end = idx === 9 ? 100 : idx * 10 + 9;\n"
        "    const active = end >= matchWindow.lowPct && start <= matchWindow.highPct;\n"
        "    const height = Math.max(10, Math.round((count / maxBin) * 100));\n"
        "    const label = start + '-' + end + '%: ' + fmtCount(count) + ' relationships';\n"
        "    return '<button type=\"button\" class=\"graph-match-bar' + (active ? ' active' : '') + '\" '\n"
        "      + 'data-match-bin-min=\"' + start + '\" data-match-bin-max=\"' + end + '\" '\n"
        "      + 'aria-label=\"Filter match ' + label + '\" title=\"' + label + '\" '\n"
        "      + 'style=\"height:' + height + '%\"></button>';\n"
        "  }).join('');\n"
        "  box.querySelectorAll('[data-match-bin-min]').forEach(bar => {\n"
        "    const useBin = () => setMatchWindow(bar.dataset.matchBinMin, bar.dataset.matchBinMax);\n"
        "    bar.addEventListener('click', useBin);\n"
        "    bar.addEventListener('keydown', ev => {\n"
        "      if (ev.key !== 'Enter' && ev.key !== ' ') return;\n"
        "      ev.preventDefault();\n"
        "      useBin();\n"
        "    });\n"
        "  });\n"
        "}\n"
        "function metricChip(label, value) {\n"
        "  const n = numericValue(value);\n"
        "  if (n == null || n <= 0) return '';\n"
        "  return '<span class=\"graph-metric\"><strong>' + escapeHtml(label) + '</strong> ' + escapeHtml(scoreText(value)) + '</span>';\n"
        "}\n"
        "function edgeHasEvidence(edge) {\n"
        "  const d = edge || {};\n"
        "  const shared = Array.from(d.shared_tags || []);\n"
        "  const reasons = Array.from(d.reasons || d.edge_reasons || []);\n"
        "  return shared.length > 0 || reasons.length > 0\n"
        "    || Number(d.semantic || 0) > 0 || Number(d.tag_sim || 0) > 0\n"
        "    || Number(d.slug_token_sim || 0) > 0 || Number(d.source_overlap || 0) > 0;\n"
        "}\n"
        "function isGraphOnlyEdge(edge) {\n"
        "  return !edgeHasEvidence(edge || {});\n"
        "}\n"
        "function isWeakGraphOnlyEdge(edge) {\n"
        "  const d = edge || {};\n"
        "  return Number(d.weight || 0) < 0.15 && isGraphOnlyEdge(d);\n"
        "}\n"
        "function edgeStrengthText(edge) {\n"
        "  const d = edge || {};\n"
        "  const weight = clampedUnit(d.weight) == null ? 'unknown' : percentText(d.weight);\n"
        "  if (isGraphOnlyEdge(d)) return weight + ' graph neighbor';\n"
        "  return weight + ' relation strength';\n"
        "}\n"
        "function edgeSignalHtml(edge) {\n"
        "  const d = edge || {};\n"
        "  const shared = Array.from(d.shared_tags || []);\n"
        "  const reasons = Array.from(d.reasons || d.edge_reasons || []);\n"
        "  if (isGraphOnlyEdge(d)) {\n"
        "    return '<div class=\"graph-edge-signals\"><span class=\"graph-metric\"><strong>match</strong> '\n"
        "      + escapeHtml(percentText(d.weight)) + '</span></div>';\n"
        "  }\n"
        "  const evidence = reasons.length ? reasons.join(', ') : (edgeHasEvidence(d) ? 'graph signal' : 'none');\n"
        "  return '<div class=\"graph-edge-signals\">'\n"
        "    + '<span class=\"graph-metric\"><strong>strength</strong> ' + escapeHtml(edgeStrengthText(d)) + '</span>'\n"
        "    + metricChip('semantic', d.semantic)\n"
        "    + metricChip('tag', d.tag_sim)\n"
        "    + metricChip('slug', d.slug_token_sim)\n"
        "    + metricChip('source', d.source_overlap)\n"
        "    + '</div>'\n"
        "    + '<div class=\"muted\">shared: ' + escapeHtml(shared.join(', ') || 'none')\n"
        "    + ' - evidence: ' + escapeHtml(evidence) + '</div>';\n"
        "}\n"
        "function buildGraphState(nodes, edges) {\n"
        "  const nodesById = new Map();\n"
        "  const adjacency = new Map();\n"
        "  nodes.forEach(n => {\n"
        "    const d = n.data || {};\n"
        "    if (!d.id) return;\n"
        "    nodesById.set(d.id, d);\n"
        "    adjacency.set(d.id, []);\n"
        "  });\n"
        "  edges.forEach(e => {\n"
        "    const d = e.data || {};\n"
        "    const source = nodesById.get(d.source);\n"
        "    const target = nodesById.get(d.target);\n"
        "    if (!source || !target) return;\n"
        "    adjacency.get(d.source).push({node: target, edge: d});\n"
        "    adjacency.get(d.target).push({node: source, edge: d});\n"
        "  });\n"
        "  adjacency.forEach(items => items.sort((a, b) => Number(b.edge.weight || 0) - Number(a.edge.weight || 0)));\n"
        "  return {nodesById, adjacency};\n"
        "}\n"
        "function nodeSummaryHtml(d) {\n"
        "  const tags = Array.from(d.tags || []);\n"
        "  return '<div class=\"graph-selected-summary\">'\n"
        "    + '<div><strong>' + escapeHtml(d.label || nodeSlug(d.id)) + '</strong> '\n"
        "    + '<span class=\"pill entity-type-' + escapeHtml(d.type || 'skill') + '\">' + escapeHtml(d.type || 'entity') + '</span></div>'\n"
        "    + '<a href=\"' + escapeHtml(wikiHref(d)) + '\">open wiki</a>'\n"
        "    + '<div class=\"muted\">Connections: ' + fmtCount(d.degree) + ' in graph - quality: ' + escapeHtml(qualityText(d.quality_score))\n"
        "    + ' - usage: ' + escapeHtml(usageText(d.usage_score)) + '</div>'\n"
        "    + '<div class=\"muted\">tags: ' + escapeHtml(tags.join(', ') || 'none') + '</div>'\n"
        "    + (d.description ? '<p>' + escapeHtml(d.description) + '</p>' : '')\n"
        "    + '</div>';\n"
        "}\n"
        "function renderNodeTree(nodeId) {\n"
        "  if (!currentGraphState) return '<div class=\"muted\">No graph loaded.</div>';\n"
        "  const d = currentGraphState.nodesById.get(nodeId);\n"
        "  if (!d) return '<div class=\"muted\">Node not found in this view.</div>';\n"
        "  const neighbors = currentGraphState.adjacency.get(nodeId) || [];\n"
        "  const visibleNeighbors = neighbors;\n"
        "  const groups = new Map();\n"
        "  visibleNeighbors.forEach(item => {\n"
        "    const type = item.node.type || 'entity';\n"
        "    if (!groups.has(type)) groups.set(type, []);\n"
        "    groups.get(type).push(item);\n"
        "  });\n"
        "  const groupsHtml = Array.from(groups.entries()).map(([type, items]) => {\n"
        "    const rows = items.map(item => {\n"
        "      const n = item.node;\n"
        "      const inner = (currentGraphState.adjacency.get(n.id) || [])\n"
        "        .filter(rel => rel.node.id !== nodeId)\n"
        "        .slice(0, 8);\n"
        "      const innerHtml = inner.length ? '<details class=\"graph-inner-links\"><summary>'\n"
        "        + fmtCount(inner.length) + ' in-view links</summary><ul>'\n"
        "        + inner.map(rel => '<li><span class=\"graph-link-label\">' + escapeHtml(n.label || nodeSlug(n.id)) + '</span> -> <span class=\"graph-link-label\">'\n"
        "          + escapeHtml(rel.node.label || nodeSlug(rel.node.id)) + '</span> '\n"
        "          + '<span class=\"muted\">' + escapeHtml(scoreText(rel.edge.weight)) + '</span></li>').join('')\n"
        "        + '</ul></details>' : '';\n"
        "      return '<li class=\"graph-neighbor-item\">'\n"
        "        + '<div class=\"graph-neighbor-row\"><span><span class=\"graph-neighbor-label\">' + escapeHtml(n.label || nodeSlug(n.id)) + '</span> '\n"
        "        + '<span class=\"pill entity-type-' + escapeHtml(n.type || 'skill') + '\">' + escapeHtml(n.type || 'entity') + '</span></span>'\n"
        "        + '<a href=\"' + escapeHtml(wikiHref(n)) + '\">wiki</a></div>'\n"
        "        + edgeSignalHtml(item.edge)\n"
        "        + innerHtml\n"
        "        + '</li>';\n"
        "    }).join('');\n"
        "    return '<details class=\"graph-neighbor-group\" open><summary>' + escapeHtml(type) + ' (' + fmtCount(items.length) + ')</summary><ul>' + rows + '</ul></details>';\n"
        "  }).join('');\n"
        "  return '<div class=\"graph-neighbor-tree\">' + nodeSummaryHtml(d)\n"
        "    + '<h3>Neighbors</h3>'\n"
        "    + (groupsHtml || '<p class=\"muted\">No neighbors in this view.</p>')\n"
        "    + '</div>';\n"
        "}\n"
        "function renderGraph3d(g) {\n"
        "  const nodes = g.nodes || [];\n"
        "  const edges = g.edges || [];\n"
        "  if (!nodes.length) { renderFallback(g); return; }\n"
        "  currentGraphState = buildGraphState(nodes, edges);\n"
        "  currentGraphEdges = edges;\n"
        "  const width = Math.max(640, Math.floor(cyMount.clientWidth || 800));\n"
        "  const height = Math.max(420, Math.floor(cyMount.clientHeight || 520));\n"
        "  const center = nodes.find(n => (n.data || {}).depth === 0) || nodes[0];\n"
        "  const centerId = (center.data || {}).id;\n"
        "  let selectedNodeId = centerId;\n"
        "  const points = new Map([[centerId, {x: 0, y: 0, z: 0}]]);\n"
        "  const others = nodes.filter(n => (n.data || {}).id !== centerId);\n"
        "  others.forEach((n, idx) => {\n"
        "    const d = n.data || {};\n"
        "    const i = idx + 1;\n"
        "    const phi = Math.acos(1 - 2 * i / Math.max(2, others.length + 1));\n"
        "    const theta = Math.PI * (3 - Math.sqrt(5)) * i;\n"
        "    const depth = Math.max(1, Number(d.depth || 1));\n"
        "    const radius = 180 + depth * 90;\n"
        "    points.set(d.id, {x: radius * Math.cos(theta) * Math.sin(phi), y: radius * Math.sin(theta) * Math.sin(phi), z: radius * Math.cos(phi)});\n"
        "  });\n"
        "  cyMount.innerHTML = '<div data-testid=\"graph-renderer\" class=\"graph-renderer-shell\">'\n"
        "    + '<div class=\"graph-toolbar\">'\n"
        "    + '<button id=\"graph-zoom-in\" type=\"button\">zoom +</button><button id=\"graph-zoom-out\" type=\"button\">zoom -</button>'\n"
        "    + '<span class=\"muted\">drag rotate - wheel zoom - click drill - double-click back</span></div>'\n"
        "    + '<div class=\"graph-canvas-wrap\"><svg data-testid=\"graph-3d\" viewBox=\"0 0 ' + width + ' ' + height + '\" style=\"display:block; width:100%; height:100%; min-height:0; background:transparent; touch-action:none;\"></svg><div id=\"graph-tooltip\" class=\"graph-tooltip\" hidden></div></div>'\n"
        "    + '<div data-testid=\"graph-inspector-resize\" class=\"graph-resize-handle\" role=\"separator\" tabindex=\"0\" aria-orientation=\"horizontal\" aria-label=\"Resize graph details panel\" title=\"Drag to resize details panel; double-click to reset\"></div>'\n"
        "    + '<div class=\"graph-inspector-grid\">'\n"
        "    + '<div data-testid=\"graph-node-detail\" class=\"graph-node-detail-panel\"><div data-testid=\"graph-edge-detail\" class=\"muted graph-edge-detail-inline\">Hover an edge for relationship signals.</div><div data-testid=\"graph-node-detail-tree\" class=\"muted\">Click a node to inspect neighbors.</div></div></div>'\n"
        "    + '<div data-testid=\"graph-list\" class=\"graph-list-panel\" hidden></div></div>';\n"
        "  const rendererShell = cyMount.querySelector('[data-testid=\"graph-renderer\"]');\n"
        "  const svg = cyMount.querySelector('[data-testid=\"graph-3d\"]');\n"
        "  const resizeHandle = cyMount.querySelector('[data-testid=\"graph-inspector-resize\"]');\n"
        "  const nodeDetailBox = cyMount.querySelector('[data-testid=\"graph-node-detail-tree\"]');\n"
        "  const edgeDetailBox = cyMount.querySelector('[data-testid=\"graph-edge-detail\"]');\n"
        "  const tooltip = cyMount.querySelector('#graph-tooltip');\n"
        "  const inspectorStorageKey = 'ctx-monitor-graph-inspector-height';\n"
        "  function clampInspectorHeight(value) {\n"
        "    const shellRect = rendererShell.getBoundingClientRect();\n"
        "    const toolbar = cyMount.querySelector('.graph-toolbar');\n"
        "    const toolbarHeight = toolbar ? toolbar.getBoundingClientRect().height : 42;\n"
        "    const min = 140;\n"
        "    const max = Math.max(min, shellRect.height - toolbarHeight - 300);\n"
        "    return Math.max(min, Math.min(max, Number(value) || 0));\n"
        "  }\n"
        "  function setInspectorHeight(value, persist = true) {\n"
        "    const heightPx = clampInspectorHeight(value);\n"
        "    rendererShell.style.setProperty('--graph-inspector-height', heightPx + 'px');\n"
        "    resizeHandle.setAttribute('aria-valuenow', String(Math.round(heightPx)));\n"
        "    resizeHandle.setAttribute('aria-valuemin', '140');\n"
        "    if (persist) {\n"
        "      try { localStorage.setItem(inspectorStorageKey, String(Math.round(heightPx))); } catch (_) {}\n"
        "    }\n"
        "  }\n"
        "  function resetInspectorHeight() {\n"
        "    rendererShell.style.removeProperty('--graph-inspector-height');\n"
        "    resizeHandle.removeAttribute('aria-valuenow');\n"
        "    try { localStorage.removeItem(inspectorStorageKey); } catch (_) {}\n"
        "  }\n"
        "  try {\n"
        "    const savedInspectorHeight = Number(localStorage.getItem(inspectorStorageKey));\n"
        "    if (Number.isFinite(savedInspectorHeight) && savedInspectorHeight > 0) setInspectorHeight(savedInspectorHeight, false);\n"
        "  } catch (_) {}\n"
        "  resizeHandle.addEventListener('pointerdown', ev => {\n"
        "    ev.preventDefault();\n"
        "    rendererShell.classList.add('graph-resizing');\n"
        "    resizeHandle.setPointerCapture(ev.pointerId);\n"
        "    const onMove = moveEv => {\n"
        "      const shellRect = rendererShell.getBoundingClientRect();\n"
        "      setInspectorHeight(shellRect.bottom - moveEv.clientY);\n"
        "    };\n"
        "    const onUp = upEv => {\n"
        "      rendererShell.classList.remove('graph-resizing');\n"
        "      try { resizeHandle.releasePointerCapture(upEv.pointerId); } catch (_) {}\n"
        "      resizeHandle.removeEventListener('pointermove', onMove);\n"
        "      resizeHandle.removeEventListener('pointerup', onUp);\n"
        "      resizeHandle.removeEventListener('pointercancel', onUp);\n"
        "    };\n"
        "    resizeHandle.addEventListener('pointermove', onMove);\n"
        "    resizeHandle.addEventListener('pointerup', onUp);\n"
        "    resizeHandle.addEventListener('pointercancel', onUp);\n"
        "  });\n"
        "  resizeHandle.addEventListener('keydown', ev => {\n"
        "    const current = Number(getComputedStyle(rendererShell).getPropertyValue('--graph-inspector-height').replace('px', '')) || cyMount.querySelector('.graph-inspector-grid').getBoundingClientRect().height;\n"
        "    if (ev.key === 'ArrowUp') { ev.preventDefault(); setInspectorHeight(current + 28); }\n"
        "    if (ev.key === 'ArrowDown') { ev.preventDefault(); setInspectorHeight(current - 28); }\n"
        "    if (ev.key === 'Home') { ev.preventDefault(); resetInspectorHeight(); }\n"
        "  });\n"
        "  resizeHandle.addEventListener('dblclick', resetInspectorHeight);\n"
        "  let yaw = -0.4;\n"
        "  let pitch = 0.55;\n"
        "  let zoom = 1;\n"
        "  function project(p) {\n"
        "    const cyaw = Math.cos(yaw), syaw = Math.sin(yaw);\n"
        "    const cp = Math.cos(pitch), sp = Math.sin(pitch);\n"
        "    const x1 = p.x * cyaw - p.z * syaw;\n"
        "    const z1 = p.x * syaw + p.z * cyaw;\n"
        "    const y1 = p.y * cp - z1 * sp;\n"
        "    const z2 = p.y * sp + z1 * cp;\n"
        "    const scale = zoom * 600 / (720 + z2);\n"
        "    return {x: width / 2 + x1 * scale, y: height / 2 + y1 * scale, z: z2, scale};\n"
        "  }\n"
        "  function showTooltip(text, ev) {\n"
        "    tooltip.textContent = text || '';\n"
        "    tooltip.hidden = !text;\n"
        "    const rect = cyMount.getBoundingClientRect();\n"
        "    tooltip.style.left = Math.min(rect.width - 260, Math.max(8, ev.clientX - rect.left + 12)) + 'px';\n"
        "    tooltip.style.top = Math.max(8, ev.clientY - rect.top + 12) + 'px';\n"
        "  }\n"
        "  function hideTooltip() { tooltip.hidden = true; }\n"
        "  function highlightSelected() {\n"
        "    svg.querySelectorAll('[data-3d-node-id]').forEach(n => {\n"
        "      n.classList.toggle('graph-node-selected', dataAttr(n, 'data-3d-node-id') === selectedNodeId);\n"
        "    });\n"
        "    const edgeEls = new Set([...svg.querySelectorAll('[data-testid=\"graph-svg-edge\"]')]);\n"
        "    edgeEls.forEach(e => {\n"
        "      const source = dataAttr(e, 'data-3d-edge-source') || dataAttr(e, 'data-svg-edge-source');\n"
        "      const target = dataAttr(e, 'data-3d-edge-target') || dataAttr(e, 'data-svg-edge-target');\n"
        "      e.classList.toggle('graph-edge-selected', source === selectedNodeId || target === selectedNodeId);\n"
        "    });\n"
        "  }\n"
        "  function focusCameraOnNode(nodeId) {\n"
        "    const p = points.get(nodeId);\n"
        "    if (!p) return;\n"
        "    if (nodeId !== centerId) {\n"
        "      yaw = Math.atan2(-p.z, Math.max(1, p.x));\n"
        "      pitch = Math.max(-1.1, Math.min(1.1, Math.atan2(p.y, Math.max(120, Math.hypot(p.x, p.z)))));\n"
        "    }\n"
        "    zoom = Math.max(zoom, 1.45);\n"
        "    drawGraph3d();\n"
        "  }\n"
        "  function selectNode(nodeId, shouldFocus = true) {\n"
        "    if (!nodeId) return;\n"
        "    selectedNodeId = nodeId;\n"
        "    nodeDetailBox.innerHTML = renderNodeTree(nodeId);\n"
        "    if (shouldFocus) focusCameraOnNode(nodeId);\n"
        "    highlightSelected();\n"
        "  }\n"
        "  let nodeClickTimer = null;\n"
        "  function selectNodeElement(node) {\n"
        "    if (!node) return;\n"
        "    const id = dataAttr(node, 'data-3d-node-id');\n"
        "    const d = currentGraphState.nodesById.get(id) || {};\n"
        "    if (d.id) drillIntoNode(d.id); else selectNode(id);\n"
        "  }\n"
        "  function scheduleNodeClick(node) {\n"
        "    if (nodeClickTimer) clearTimeout(nodeClickTimer);\n"
        "    nodeClickTimer = setTimeout(() => {\n"
        "      nodeClickTimer = null;\n"
        "      selectNodeElement(node);\n"
        "    }, 180);\n"
        "  }\n"
        "  function handleNodeDoubleClick() {\n"
        "    if (nodeClickTimer) {\n"
        "      clearTimeout(nodeClickTimer);\n"
        "      nodeClickTimer = null;\n"
        "    }\n"
        "    restorePreviousGraph();\n"
        "  }\n"
        "  function drillIntoNode(nodeId) {\n"
        "    if (!nodeId || !currentGraphState) return;\n"
        "    const d = currentGraphState.nodesById.get(nodeId) || {};\n"
        "    if (!d.id) return;\n"
        "    selectNode(d.id, true);\n"
        "    const query = graphQueryFromNodeData(d);\n"
        "    if (!query.slug || sameGraphQuery(currentGraphQuery, query)) return;\n"
        "    const key = graphQueryKey(query);\n"
        "    if (pendingDrillKey === key) return;\n"
        "    pendingDrillKey = key;\n"
        "    load(query.slug, query.type, {pushCurrent: true}).finally(() => {\n"
        "      if (pendingDrillKey === key) pendingDrillKey = '';\n"
        "    });\n"
        "  }\n"
        "  function restorePreviousGraph() {\n"
        "    const previous = graphReturnStack.pop();\n"
        "    if (!previous || !previous.slug) return;\n"
        "    load(previous.slug, previous.type || '', {pushCurrent: false});\n"
        "  }\n"
        "  window.ctxGraphSelectNodeFromElement = (el) => {\n"
        "    const node = el && el.closest ? el.closest('[data-3d-node-id]') : null;\n"
        "    selectNodeElement(node || el);\n"
        "  };\n"
        "  function attach3dHoverHandlers() {\n"
        "    svg.querySelectorAll('[data-node-detail]').forEach(n => {\n"
        "      n.addEventListener('mousemove', ev => showTooltip(n.dataset.nodeDetail || '', ev));\n"
        "      n.addEventListener('mouseleave', hideTooltip);\n"
        "      n.addEventListener('click', ev => {\n"
        "        ev.preventDefault();\n"
        "        ev.stopPropagation();\n"
        "        if (ev.detail && ev.detail > 1) return;\n"
        "        scheduleNodeClick(n);\n"
        "      });\n"
        "      n.addEventListener('dblclick', ev => {\n"
        "        ev.preventDefault();\n"
        "        ev.stopPropagation();\n"
        "        handleNodeDoubleClick();\n"
        "      });\n"
        "      n.addEventListener('keydown', ev => {\n"
        "        if (ev.key !== 'Enter' && ev.key !== ' ') return;\n"
        "        ev.preventDefault();\n"
        "        scheduleNodeClick(n);\n"
        "      });\n"
        "    });\n"
        "    svg.querySelectorAll('[data-testid=\"graph-svg-node\"]').forEach(circle => {\n"
        "      circle.addEventListener('click', ev => {\n"
        "        ev.preventDefault();\n"
        "        ev.stopPropagation();\n"
        "        const node = ev.target.closest ? ev.target.closest('[data-3d-node-id]') : ev.target.parentNode;\n"
        "        if (ev.detail && ev.detail > 1) return;\n"
        "        scheduleNodeClick(node);\n"
        "      });\n"
        "      circle.addEventListener('dblclick', ev => {\n"
        "        ev.preventDefault();\n"
        "        ev.stopPropagation();\n"
        "        handleNodeDoubleClick();\n"
        "      });\n"
        "    });\n"
        "    svg.querySelectorAll('[data-edge-detail]').forEach(e => {\n"
        "      e.addEventListener('mouseenter', () => { edgeDetailBox.textContent = e.dataset.edgeDetail || ''; });\n"
        "      e.addEventListener('mousemove', ev => showTooltip(e.dataset.edgeDetail || '', ev));\n"
        "      e.addEventListener('mouseleave', hideTooltip);\n"
        "    });\n"
        "  }\n"
        "  function drawGraph3d() {\n"
        "    const projected = new Map();\n"
        "    points.forEach((p, id) => projected.set(id, project(p)));\n"
        "    const edgeHtml = edges.map(e => {\n"
        "      const d = e.data || {};\n"
        "      const s = projected.get(d.source);\n"
        "      const t = projected.get(d.target);\n"
        "      if (!s || !t) return '';\n"
        "      const w = Math.max(0.8, Math.min(2.2, 0.75 + Math.sqrt(Math.max(0, Number(d.weight || 0.4))) * 0.85));\n"
        "      return '<line data-testid=\"graph-svg-edge\" data-3d-edge-source=\"' + escapeHtml(d.source || '') + '\" data-3d-edge-target=\"' + escapeHtml(d.target || '') + '\" data-svg-edge-source=\"' + escapeHtml(d.source || '') + '\" data-svg-edge-target=\"' + escapeHtml(d.target || '') + '\" data-edge-weight=\"' + escapeHtml(Number(d.weight || 0).toFixed(4)) + '\" x1=\"' + s.x.toFixed(1) + '\" y1=\"' + s.y.toFixed(1) + '\" x2=\"' + t.x.toFixed(1) + '\" y2=\"' + t.y.toFixed(1) + '\" stroke=\"#64748b\" stroke-opacity=\"' + (0.35 + Math.min(0.4, Number(d.weight || 0) / 3)).toFixed(2) + '\" stroke-width=\"' + w.toFixed(2) + '\" />';\n"
        "    }).join('');\n"
        "    const nodeHtml = nodes.slice().sort((a, b) => (projected.get((a.data || {}).id)?.z || 0) - (projected.get((b.data || {}).id)?.z || 0)).map(n => {\n"
        "      const d = n.data || {};\n"
        "      const p = projected.get(d.id) || {x: width / 2, y: height / 2, z: 0, scale: 1};\n"
        "      const tags = Array.from(d.filter_tokens || d.tags || []).join(' ');\n"
        "      const label = d.label || nodeSlug(d.id);\n"
        "      const isCenter = d.id === centerId;\n"
        "      const r = nodeRadius(d, isCenter, p.scale);\n"
        "      return '<g data-testid=\"graph-3d-node\" role=\"button\" tabindex=\"0\" id=\"' + escapeHtml(nodeDomId(d.id)) + '\" data-3d-node-id=\"' + escapeHtml(d.id || '') + '\" data-node-detail=\"' + escapeHtml(nodeDetail(d)) + '\" data-type=\"' + escapeHtml(d.type || '') + '\" data-depth=\"' + escapeHtml(d.depth || 0) + '\" data-tags=\"' + escapeHtml(tags.toLowerCase()) + '\"><title>' + escapeHtml(nodeDetail(d)) + '</title>' + nodeShapeSvg(d, p, r) + '<text x=\"' + p.x.toFixed(1) + '\" y=\"' + (p.y + r + 14).toFixed(1) + '\" text-anchor=\"middle\" font-size=\"11\" fill=\"#111827\" style=\"pointer-events:none;\">' + escapeHtml(label).slice(0, 28) + '</text></g>';\n"
        "    }).join('');\n"
        "    const edgeHitHtml = edges.map(e => {\n"
        "      const d = e.data || {};\n"
        "      const s = projected.get(d.source);\n"
        "      const t = projected.get(d.target);\n"
        "      if (!s || !t) return '';\n"
        "      const hx1 = s.x + (t.x - s.x) * 0.18, hy1 = s.y + (t.y - s.y) * 0.18;\n"
        "      const hx2 = s.x + (t.x - s.x) * 0.82, hy2 = s.y + (t.y - s.y) * 0.82;\n"
        "      return '<line data-testid=\"graph-3d-edge\" data-3d-edge-source=\"' + escapeHtml(d.source || '') + '\" data-3d-edge-target=\"' + escapeHtml(d.target || '') + '\" data-svg-edge-source=\"' + escapeHtml(d.source || '') + '\" data-svg-edge-target=\"' + escapeHtml(d.target || '') + '\" data-edge-weight=\"' + escapeHtml(Number(d.weight || 0).toFixed(4)) + '\" data-edge-detail=\"' + escapeHtml(edgeDetail(d)) + '\" x1=\"' + hx1.toFixed(1) + '\" y1=\"' + hy1.toFixed(1) + '\" x2=\"' + hx2.toFixed(1) + '\" y2=\"' + hy2.toFixed(1) + '\" stroke=\"transparent\" stroke-width=\"12\" style=\"pointer-events:stroke;\"><title>' + escapeHtml(edgeDetail(d)) + '</title></line>';\n"
        "    }).join('');\n"
        "    svg.innerHTML = '<rect width=\"100%\" height=\"100%\" fill=\"transparent\" pointer-events=\"all\" />' + edgeHtml + edgeHitHtml + nodeHtml;\n"
        "    attach3dHoverHandlers();\n"
        "    applyFilters();\n"
        "    highlightSelected();\n"
        "  }\n"
        "  document.getElementById('graph-zoom-in').addEventListener('click', () => { zoom = Math.min(2.5, zoom * 1.18); drawGraph3d(); });\n"
        "  document.getElementById('graph-zoom-out').addEventListener('click', () => { zoom = Math.max(0.35, zoom / 1.18); drawGraph3d(); });\n"
        "  let dragging = false, lastX = 0, lastY = 0;\n"
        "  svg.addEventListener('pointerdown', ev => {\n"
        "    if (ev.target.closest && ev.target.closest('[data-3d-node-id], [data-edge-detail]')) return;\n"
        "    dragging = true; lastX = ev.clientX; lastY = ev.clientY; svg.setPointerCapture(ev.pointerId);\n"
        "  });\n"
        "  svg.addEventListener('pointerup', ev => { dragging = false; try { svg.releasePointerCapture(ev.pointerId); } catch (_) {} });\n"
        "  svg.addEventListener('pointermove', ev => { if (!dragging) return; yaw += (ev.clientX - lastX) * 0.01; pitch += (ev.clientY - lastY) * 0.01; pitch = Math.max(-1.35, Math.min(1.35, pitch)); lastX = ev.clientX; lastY = ev.clientY; drawGraph3d(); });\n"
        "  svg.addEventListener('wheel', ev => { ev.preventDefault(); zoom = Math.max(0.35, Math.min(2.5, zoom * (ev.deltaY < 0 ? 1.08 : 0.92))); drawGraph3d(); }, {passive:false});\n"
        "  drawGraph3d();\n"
        "  selectNode(centerId, false);\n"
        "}\n"
        "cyMount.innerHTML = '<div data-testid=\"graph-empty\" class=\"muted\" style=\"padding:0.75rem;\">Enter a slug to render the graph.</div>';\n"
        # ── Client-side filtering (type + tag substring) ─────────────
        "function applyFilters() {\n"
        "  const allowedTypes = new Set(\n"
        "    Array.from(document.querySelectorAll('.graph-type-filter'))\n"
        "      .filter(cb => cb.checked).map(cb => cb.value));\n"
        "  const tagQ = (document.getElementById('tag-filter').value || '')\n"
        "    .trim().toLowerCase();\n"
        "  const matchWindow = currentMatchWindow();\n"
        "  renderMatchRange(matchWindow);\n"
        "  renderMatchHistogram(currentGraphEdges, matchWindow);\n"
        "  const counts = {skill: 0, agent: 0, 'mcp-server': 0, harness: 0};\n"
        "  let visible = 0;\n"
        "  const hiddenIds = new Set();\n"
        "  function edgeInMatchWindow(weight) {\n"
        "    const w = Number(weight || 0);\n"
        "    return w >= matchWindow.min && w <= matchWindow.max;\n"
        "  }\n"
        "  function nodePassesMatch(nodeId, depth) {\n"
        "    if (String(depth || '') === '0' || !currentGraphState) return true;\n"
        "    const links = currentGraphState.adjacency.get(nodeId) || [];\n"
        "    return links.some(item => edgeInMatchWindow(item.edge.weight));\n"
        "  }\n"
        "  function graphNodeHidden(nodeId, type, depth, tagsValue) {\n"
        "    const isFocus = String(depth || '') === '0';\n"
        "    const tags = String(tagsValue || '').split(/\\s+/).map(x => x.toLowerCase());\n"
        "    const typeOk = isFocus || allowedTypes.has(type);\n"
        "    const tagOk = isFocus || !tagQ || tags.some(tag => tag.includes(tagQ));\n"
        "    const matchOk = nodePassesMatch(nodeId, depth);\n"
        "    return !(typeOk && tagOk && matchOk);\n"
        "  }\n"
        "  document.querySelectorAll('[data-testid=\"graph-fallback-node\"]').forEach(n => {\n"
        "    const t = n.dataset.type;\n"
        "    const hidden = graphNodeHidden(n.dataset.nodeId || '', t, n.dataset.depth, n.dataset.tags);\n"
        "    n.style.display = hidden ? 'none' : 'flex';\n"
        "    if (hidden) hiddenIds.add(n.dataset.nodeId || '');\n"
        "    if (!hidden) {\n"
        "      visible++;\n"
        "      if (t in counts) counts[t]++;\n"
        "    }\n"
        "  });\n"
        "  document.querySelectorAll('[data-3d-node-id]').forEach(n => {\n"
        "    const id = dataAttr(n, 'data-3d-node-id');\n"
        "    const t = dataAttr(n, 'data-type');\n"
        "    const hidden = graphNodeHidden(id, t, dataAttr(n, 'data-depth'), dataAttr(n, 'data-tags'));\n"
        "    n.style.display = hidden ? 'none' : '';\n"
        "    if (hidden) hiddenIds.add(id);\n"
        "    if (!hidden) {\n"
        "      visible++;\n"
        "      if (t in counts) counts[t]++;\n"
        "    }\n"
        "  });\n"
        "  const edgeEls = new Set([...document.querySelectorAll('[data-3d-edge-source]'), ...document.querySelectorAll('[data-svg-edge-source]')]);\n"
        "  edgeEls.forEach(e => {\n"
        "    const source = dataAttr(e, 'data-3d-edge-source') || dataAttr(e, 'data-svg-edge-source');\n"
        "    const target = dataAttr(e, 'data-3d-edge-target') || dataAttr(e, 'data-svg-edge-target');\n"
        "    const weight = Number(dataAttr(e, 'data-edge-weight') || 0);\n"
        "    const hidden = hiddenIds.has(source) || hiddenIds.has(target) || !edgeInMatchWindow(weight);\n"
        "    e.style.display = hidden ? 'none' : '';\n"
        "  });\n"
        "  document.getElementById('graph-count-skill').textContent = fmtCount(counts.skill);\n"
        "  document.getElementById('graph-count-agent').textContent = fmtCount(counts.agent);\n"
        "  document.getElementById('graph-count-mcp-server').textContent = fmtCount(counts['mcp-server']);\n"
        "  document.getElementById('graph-count-harness').textContent = fmtCount(counts.harness);\n"
        "  document.getElementById('graph-match-count').textContent = fmtCount(visible) + ' visible';\n"
        "}\n"
        "function hideLiveSearch() {\n"
        "  liveSearchBox.hidden = true;\n"
        "  liveSearchBox.innerHTML = '';\n"
        "}\n"
        "function renderLiveSearchResults(items, query) {\n"
        "  liveSearchBox.hidden = false;\n"
        "  if (!items.length) {\n"
        "    liveSearchBox.innerHTML = '<div class=\"graph-live-empty\">No graph entities match <code>' + escapeHtml(query) + '</code>.</div>';\n"
        "    return;\n"
        "  }\n"
        "  liveSearchBox.innerHTML = items.map(item => {\n"
        "    const tags = Array.from(item.tags || []).slice(0, 4).join(', ');\n"
        "    const title = item.title || item.display_slug || item.slug;\n"
        "    const detail = [item.description, tags ? 'tags: ' + tags : '', item.degree != null ? 'connections ' + fmtCount(item.degree) : '']\n"
        "      .filter(Boolean).join(' - ');\n"
        "    return '<button type=\"button\" class=\"graph-live-result\" data-live-slug=\"' + escapeHtml(item.slug || '') + '\" data-live-type=\"' + escapeHtml(item.type || '') + '\">'\n"
        "      + '<span><code>' + escapeHtml(item.display_slug || item.slug || title) + '</code> '\n"
        "      + '<span class=\"pill entity-type-' + escapeHtml(item.type || 'skill') + '\">' + escapeHtml(item.type || 'entity') + '</span></span>'\n"
        "      + '<small>' + escapeHtml(detail || title) + '</small></button>';\n"
        "  }).join('');\n"
        "  liveSearchBox.querySelectorAll('[data-live-slug]').forEach(btn => {\n"
        "    btn.addEventListener('click', () => {\n"
        "      focus.value = btn.dataset.liveSlug || '';\n"
        "      focusType.value = btn.dataset.liveType || '';\n"
        "      hideLiveSearch();\n"
        "      load(focus.value.trim(), selectedFocusType());\n"
        "    });\n"
        "  });\n"
        "}\n"
        "async function runLiveSearch() {\n"
        "  const query = focus.value.trim();\n"
        "  if (query.length < 2) { hideLiveSearch(); return; }\n"
        "  if (liveSearchAbort) liveSearchAbort.abort();\n"
        "  liveSearchAbort = new AbortController();\n"
        "  liveSearchBox.hidden = false;\n"
        "  liveSearchBox.innerHTML = '<div class=\"graph-live-empty\">Searching...</div>';\n"
        "  const suffix = '';\n"
        "  try {\n"
        "    const r = await fetch('/api/entities/search.json?q=' + encodeURIComponent(query) + suffix + '&limit=12', {signal: liveSearchAbort.signal});\n"
        "    if (!r.ok) throw new Error('search failed');\n"
        "    const payload = await r.json();\n"
        "    renderLiveSearchResults(payload.results || [], query);\n"
        "  } catch (err) {\n"
        "    if (err.name === 'AbortError') return;\n"
        "    liveSearchBox.innerHTML = '<div class=\"graph-live-empty\">Search failed. Press Enter to resolve the slug directly.</div>';\n"
        "  }\n"
        "}\n"
        "function scheduleLiveSearch() {\n"
        "  clearTimeout(liveSearchTimer);\n"
        "  liveSearchTimer = setTimeout(runLiveSearch, 180);\n"
        "}\n"
        "async function load(slug, entityType = '', options = {}) {\n"
        "  if (!slug) return;\n"
        "  hideLiveSearch();\n"
        "  const previousQuery = currentGraphQuery;\n"
        "  document.getElementById('msg').textContent = 'loading…';\n"
        "  const suffix = entityType ? '?type=' + encodeURIComponent(entityType) : '';\n"
        "  const r = await fetch('/api/graph/' + encodeURIComponent(slug) + '.json' + suffix);\n"
        "  if (!r.ok) { document.getElementById('msg').textContent = 'not found'; return; }\n"
        "  const g = await r.json();\n"
        "  if (!g.center) { document.getElementById('msg').textContent = 'slug not in graph'; return; }\n"
        "  const nextQuery = graphQueryFromPayload(g, slug, entityType);\n"
        "  if (options.pushCurrent && previousQuery && !sameGraphQuery(previousQuery, nextQuery)) {\n"
        "    graphReturnStack.push(previousQuery);\n"
        "  }\n"
        "  currentGraphQuery = nextQuery;\n"
        "  focus.value = nextQuery.slug || slug;\n"
        "  focusType.value = nextQuery.type || entityType || '';\n"
        "  try { renderGraph3d(g); } catch (err) { renderFallback(g); }\n"
        "  const resolved = g.resolved && g.resolved.query !== g.resolved.slug ? ' · showing ' + g.resolved.slug + ' for ' + g.resolved.query : '';\n"
        "  document.getElementById('msg').textContent = fmtCount(g.nodes.length) + ' nodes · ' + fmtCount(g.edges.length) + ' edges' + resolved;\n"
        "  document.getElementById('graph-explanation').textContent = graphExplanation(g);\n"
        "  applyFilters();\n"
        "}\n"
        "function selectedFocusType() { return focusType.value || ''; }\n"
        "document.getElementById('go').addEventListener('click', () => load(focus.value.trim(), selectedFocusType()));\n"
        "focus.addEventListener('input', scheduleLiveSearch);\n"
        "focus.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') load(ev.target.value.trim(), selectedFocusType()); });\n"
        "focusType.addEventListener('change', scheduleLiveSearch);\n"
        "document.querySelectorAll('.graph-type-filter').forEach(cb => cb.addEventListener('change', applyFilters));\n"
        "document.getElementById('match-filter-min').addEventListener('input', applyFilters);\n"
        "document.getElementById('match-filter-max').addEventListener('input', applyFilters);\n"
        "document.getElementById('tag-filter').addEventListener('input', applyFilters);\n"
        "if (initial) load(initial, initialType);\n"
        "</script>"
    )
    return _layout("Graph", body)


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

    tag_html = (
        "".join(f"<span class='pill'>{html.escape(tag)}</span> " for tag in tags)
        if tags
        else "<span class='muted'>no tags in runtime graph</span>"
    )
    description_html = (
        f"<p>{html.escape(description)}</p>"
        if description
        else "<p class='muted'>No description is present in the runtime graph metadata.</p>"
    )
    quality_summary = (
        "<div class='card'>"
        "<strong>Runtime graph entity</strong> "
        f"<span class='pill entity-type-{html.escape(resolved_type)}'>{html.escape(resolved_type)}</span> "
        f"<span class='muted'>node <code>{html.escape(node_id)}</code></span>"
        "<div style='margin-top:0.4rem;'>"
        "<a href='#subgraph' data-open-entity-tab='subgraph'>graph neighborhood &rarr;</a> &middot; "
        "<a href='#quality' data-open-entity-tab='quality'>quality drilldown &rarr;</a>"
        "</div></div>"
    )
    overview_html = (
        "<div class='wiki-entity-grid'>"
        "<div class='card wiki-body'>"
        "<h2>Runtime graph entity</h2>"
        + description_html
        + "<h3>Tags</h3>"
        + f"<p>{tag_html}</p>"
        + "<h3>Full wiki page</h3>"
        + "<p class='muted'>This entity exists in the installed runtime graph, but its full "
        "Markdown wiki page is not expanded locally. The graph and recommendation paths still "
        "work. Install the full wiki when you want the complete body/docs in this dashboard.</p>"
        + "<pre><code>ctx-init --graph --graph-install-mode full</code></pre>"
        + "</div>"
        "<div class='card'><strong>Runtime metadata</strong>"
        "<table class='frontmatter-table'><tr><th>Field</th><th>Value</th></tr>"
        + _runtime_graph_metric_row("slug", display_slug)
        + _runtime_graph_metric_row("type", resolved_type)
        + _runtime_graph_metric_row("node_id", node_id)
        + _runtime_graph_metric_row("quality_score", quality_score)
        + _runtime_graph_metric_row("usage_score", usage_score)
        + _runtime_graph_metric_row("degree", degree)
        + "</table></div>"
        "</div>"
        + _render_runtime_entity_action(
            resolved_slug,
            resolved_type,
            mutations_enabled=mutations,
        )
    )
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
    body = (
        f"<h1>{html.escape(label)}</h1>"
        + quality_summary
        + _render_entity_tabs(
            overview_html=overview_html,
            subgraph_html=_render_entity_subgraph(resolved_slug, entity_type=resolved_type),
            quality_html=quality_html,
        )
        + _render_runtime_entity_load_script(
            resolved_slug,
            resolved_type,
            mutations_enabled=mutations,
        )
    )
    return _layout(label, body)


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
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _layout(
            slug,
            f"<h1>{html.escape(slug)}</h1><p class='muted'>read error: {html.escape(str(exc))}</p>",
        )
    meta, md_body = _parse_frontmatter(raw)
    sidecar = _load_sidecar(slug, entity_type=entity_type)
    display_slug = _display_slug(slug)
    type_suffix = (
        f"&amp;type={html.escape(entity_type)}"
        if entity_type in _DASHBOARD_ENTITY_TYPES
        else ""
    )

    fm_row_parts = []
    for k, v in sorted(meta.items()):
        value, truncated = _truncate_text(_frontmatter_text(v), 120)
        marker = " <span class='muted'>(truncated)</span>" if truncated else ""
        fm_row_parts.append(
            f"<tr><td class='muted'>{html.escape(k)}</td>"
            f"<td><code>{html.escape(value)}</code>{marker}</td></tr>"
        )
    fm_rows = "".join(fm_row_parts)

    quality_summary_html = ""
    if sidecar is not None:
        quality_summary_html = (
            "<div class='card'>"
            f"<strong>Quality</strong> <span class='pill grade-{html.escape(sidecar.get('grade', 'F'))}'>"
            f"{html.escape(sidecar.get('grade', 'F'))}</span> "
            f"score <strong>{sidecar.get('raw_score', 0.0):.3f}</strong>"
            f"{' &middot; floor ' + html.escape(sidecar.get('hard_floor','')) if sidecar.get('hard_floor') else ''}"
            f"<div style='margin-top:0.4rem;'>"
            "<a href='#quality' data-open-entity-tab='quality'>quality drilldown &rarr;</a> &middot; "
            f"<a href='/skill/{html.escape(slug)}?type={html.escape(entity_type or '')}'>sidecar detail &rarr;</a> &middot; "
            f"<a href='/graph?slug={html.escape(slug)}{type_suffix}'>graph neighborhood &rarr;</a>"
            "</div></div>"
        )

    md_body_without_quality, embedded_quality_markdown = _extract_embedded_quality_block(md_body)
    display_body = _strip_duplicate_wiki_heading(md_body_without_quality, slug)
    body_preview, body_truncated = _truncate_text(display_body, 12000)
    body_html = _render_wiki_markdown(body_preview)
    body_truncated_html = (
        "<p class='muted'>Body preview truncated at 12,000 characters.</p>"
        if body_truncated
        else ""
    )
    overview_html = (
        "<div class='wiki-entity-grid'>"
        f"<div class='card wiki-body'>{body_html}"
        f"{body_truncated_html}</div>"
        f"<div class='card'><strong>Frontmatter</strong>"
        "<table class='frontmatter-table'>"
        "<tr><th>Field</th><th>Value</th></tr>"
        + (fm_rows or "<tr><td class='muted' colspan='2'>none</td></tr>")
        + "</table></div>"
        "</div>"
    )
    subgraph_html = _render_entity_subgraph(slug, entity_type=entity_type)
    quality_html = _render_quality_drilldown(sidecar, embedded_quality_markdown)
    body = (
        f"<h1>{html.escape(display_slug)}</h1>"
        + quality_summary_html
        + _render_entity_tabs(
            overview_html=overview_html,
            subgraph_html=subgraph_html,
            quality_html=quality_html,
        )
    )
    return _layout(display_slug, body)


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

    base = _wiki_dir() / "entities"
    if not base.is_dir():
        return []
    # MCPs are sharded (one dir per first-char) so we glob recursively;
    # all other dashboard entity types are flat.
    sources = _DASHBOARD_ENTITY_SOURCES
    out: list[dict] = []
    for sub, entity_type, recursive in sources:
        d = base / sub
        if not d.is_dir():
            continue
        paths = sorted(
            d.rglob("*.md") if recursive else d.glob("*.md"),
            key=lambda path: (path.stem.lower(), path.relative_to(d).as_posix().lower()),
        )
        seen_for_type = 0
        for path in paths:
            if limit_per_type is not None and seen_for_type >= limit_per_type:
                break
            slug = path.stem
            if not _is_safe_slug(slug):
                continue
            try:
                # Read only the first ~2 KB — enough for frontmatter.
                head = path.read_text(encoding="utf-8", errors="replace")[:2048]
            except OSError:
                continue
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

    suggestions = "".join(
        f"<option value='{html.escape(e['slug'])}' "
        f"label='{html.escape(e.get('display_slug') or e['slug'])}'>"
        for e in entries[:1000]
    )

    cards = "".join(
        "<a class='wiki-card' "
        f"data-slug='{html.escape(e['slug'])}' "
        f"data-display-slug='{html.escape(e.get('display_slug') or e['slug'])}' "
        f"data-type='{html.escape(e['type'])}' "
        f"data-tags='{html.escape(' '.join(e.get('search_tags', e['tags'])).lower())}' "
        f"href='/wiki/{html.escape(e['slug'])}?type={html.escape(e['type'])}' "
        "style='border:1px solid #e5e7eb; border-radius:6px; "
        "padding:0.6rem 0.8rem; text-decoration:none; color:inherit; "
        "display:flex; flex-direction:column; gap:0.25rem;'>"
        "<div style='display:flex; justify-content:space-between; align-items:center; gap:0.4rem;'>"
        f"<code style='font-size:0.84rem;'>{html.escape(e.get('display_slug') or e['slug'])}</code>"
        + (f"<span class='pill grade-{html.escape(grade_by_key[(e['slug'], e['type'])])}'>"
           f"{html.escape(grade_by_key[(e['slug'], e['type'])])}</span>"
           if grade_by_key.get((e['slug'], e['type'])) else
           f"<span class='pill'>{html.escape(e['type'])}</span>")
        + "</div>"
        f"<div class='muted' style='font-size:0.78rem; line-height:1.3;'>"
        f"{html.escape(e['description'] or '(no description)')}"
        "</div>"
        + (f"<div class='muted' style='font-size:0.72rem;'>"
           f"{' · '.join(html.escape(t) for t in e['tags'][:5])}</div>"
           if e["tags"] else "")
        + "</a>"
        for e in entries
    )

    type_checkboxes = "".join(
        f"<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        f"<span><input type='checkbox' class='wiki-type-filter' value='{t}' "
        f"{'checked' if selected_type is None or selected_type == t else ''}> {t}</span>"
        f"<span class='muted' style='font-size:0.78rem;'>{type_counts.get(t, 0):,}</span>"
        f"</label>"
        for t in _DASHBOARD_ENTITY_TYPES
    )
    badge_links = "".join(
        f"<a class='pill entity-type-{html.escape(t)}' href='/wiki?type={quote(t)}'>"
        f"{html.escape(t)}</a>"
        for t in _DASHBOARD_ENTITY_TYPES
    )

    body = (
        "<h1>Wiki</h1>"
        f"<p class='muted'>{len(entries):,} shown of {total_available:,} entity pages under "
        f"<code>~/.claude/skill-wiki/entities/</code> · "
        "search by slug / description / tag, pick a suggestion, "
        "or click a tile to read the full page.</p>"
        "<div class='card' style='display:flex; gap:0.45rem; flex-wrap:wrap; align-items:center;'>"
        f"<strong>Catalog shortcuts</strong>{badge_links}</div>"
        "<div style='display:grid; grid-template-columns:220px 1fr; gap:1.25rem; align-items:start;'>"
        # Left sidebar
        "<aside style='position:sticky; top:1rem;'>"
        "<div class='card'><strong>Search</strong>"
        f"<datalist id='wiki-entity-suggestions'>{suggestions}</datalist>"
        "<input type='text' id='wiki-search' placeholder='slug / tag / text…' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'></div>"
        "<div class='card'><strong>Type</strong>" + type_checkboxes + "</div>"
        "<div class='card'><span id='wiki-match-count' class='muted'>—</span></div>"
        "</aside>"
        # Card grid
        "<div id='wiki-grid' style='display:grid; "
        "grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:0.6rem;'>"
        + (cards or "<p class='muted'>No wiki entities found. "
           "Extract <code>graph/wiki-graph.tar.gz</code> into "
           "<code>~/.claude/skill-wiki/</code> to populate.</p>")
        + "</div>"
        "</div>"
        "<script>\n"
        "const wcards = document.querySelectorAll('.wiki-card');\n"
        "const wsearch = document.getElementById('wiki-search');\n"
        "wsearch.setAttribute('list', 'wiki-entity-suggestions');\n"
        f"wsearch.value = {json.dumps(initial_query)};\n"
        "function wActiveTypes() { return Array.from(document.querySelectorAll('.wiki-type-filter:checked')).map(x => x.value); }\n"
        "function wApply() {\n"
        "  const q = wsearch.value.trim().toLowerCase();\n"
        "  const types = new Set(wActiveTypes());\n"
        "  let shown = 0;\n"
        "  wcards.forEach(c => {\n"
        "    const hay = (c.dataset.slug + ' ' + c.dataset.displaySlug + ' ' + (c.textContent||'') + ' ' + c.dataset.tags).toLowerCase();\n"
        "    const ok = types.has(c.dataset.type) && (!q || hay.includes(q));\n"
        "    c.style.display = ok ? '' : 'none';\n"
        "    if (ok) shown++;\n"
        "  });\n"
        "  document.getElementById('wiki-match-count').textContent = shown + ' of ' + wcards.length + ' match';\n"
        "}\n"
        "wsearch.addEventListener('input', wApply);\n"
        "document.querySelectorAll('.wiki-type-filter').forEach(el => el.addEventListener('change', wApply));\n"
        "wApply();\n"
        "</script>"
    )
    html_out = _layout("Wiki", body)
    if cache_key is not None:
        _write_html_disk_cache(_wiki_render_disk_cache_path(), cache_token, html_out)
        _WIKI_RENDER_CACHE_KEY = cache_key
        _WIKI_RENDER_CACHE_VALUE = html_out
    return html_out



def _docs_roots() -> list[Path]:
    return dashboard_docs.docs_roots(Path.cwd(), Path(__file__).resolve().parent.parent)


def _docs_render_disk_cache_path() -> Path:
    return dashboard_docs.docs_render_disk_cache_path(_claude_dir())


def _docs_index_entries() -> list[dict[str, Any]]:
    return dashboard_docs.docs_index_entries(_docs_roots())


def _docs_tabs(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return dashboard_docs.docs_tabs(entries, _docs_roots())


def _render_docs_markdown(markdown_text: str, page_anchor: str) -> str:
    return dashboard_docs.render_docs_markdown(
        markdown_text,
        page_anchor,
        fallback_renderer=_render_wiki_markdown,
    )


def _render_docs() -> str:
    return dashboard_docs.render_docs(
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
    initial_results = _search_wiki_entities(limit=40)
    type_options = "".join(
        f"<option value='{html.escape(entity_type)}'>{html.escape(entity_type)}</option>"
        for entity_type in _DASHBOARD_ENTITY_TYPES
    )
    initial_json = _json_for_script(initial_results)
    read_only = (
        ""
        if mutations_enabled
        else (
            "<div class='card'><strong>Read-only mode.</strong> Catalog edits are "
            "disabled because ctx-monitor is not bound to a loopback address.</div>"
        )
    )
    disabled = "" if mutations_enabled else " disabled"
    body = (
        "<h1>Manage catalog</h1>"
        "<p class='muted'>Search skills, agents, MCP servers, and harnesses. "
        "Edit the wiki page, delete stale entries, or add a new entity. Saves "
        "write into <code>~/.claude/skill-wiki/entities/</code> and queue graph "
        "refresh work for the knowledge graph.</p>"
        + read_only
        +
        "<div class='wizard-layout'>"
        "<section class='card'>"
        "<h2>Search catalog</h2>"
        "<div class='wizard-grid'>"
        "<label>Query"
        "<input id='manage-search' type='search' placeholder='slug, tag, description' "
        "autocomplete='off'></label>"
        "<label>Type"
        f"<select id='manage-type'><option value=''>all types</option>{type_options}</select>"
        "</label>"
        "</div>"
        "<p id='manage-search-status' class='muted'>Loading...</p>"
        "<div id='manage-results' class='manage-results'></div>"
        "</section>"
        "<section class='card'>"
        "<h2>Add or update entity</h2>"
        "<form id='entity-editor-form'>"
        "<div class='wizard-grid'>"
        "<label>Slug <span class='pill grade-A'>Required</span>"
        "<input name='slug' required pattern='[a-z0-9][a-z0-9_.+-]*' "
        "placeholder='custom-reviewer'></label>"
        "<label>Type <span class='pill grade-A'>Required</span>"
        f"<select name='entity_type' required>{type_options}</select></label>"
        "<label>Title <span class='pill grade-A'>Required</span>"
        "<input name='title' required placeholder='Custom Reviewer'></label>"
        "<label>Tags"
        "<input name='tags' placeholder='python, review, policy'></label>"
        "<label class='wide'>Description"
        "<input name='description' placeholder='What this entity does and when to use it'></label>"
        "<label class='wide'>Source URL"
        "<input name='source_url' placeholder='https://github.com/org/repo'></label>"
        "<label class='wide'>Markdown body <span class='pill grade-A'>Required</span>"
        "<textarea name='body' required rows='16' "
        "placeholder='# Custom Reviewer\n\nInstall and usage notes...'></textarea></label>"
        "</div>"
        "<div style='display:flex; gap:0.5rem; flex-wrap:wrap; margin-top:0.8rem;'>"
        f"<button type='submit'{disabled}>Save to wiki + graph queue</button>"
        "<button type='button' id='entity-new-button'>New</button>"
        f"<button type='button' id='entity-delete-button' data-testid='entity-delete-button'{disabled}>Delete selected</button>"
        "</div>"
        "<p id='entity-editor-status' class='muted'></p>"
        "</form>"
        "</section>"
        "</div>"
        "<script>\n"
        "window.CTX_MONITOR_MANAGE = {\n"
        f"  mutationsEnabled: {json.dumps(mutations_enabled)},\n"
        f"  token: {json.dumps(token)},\n"
        f"  initialResults: {initial_json}\n"
        "};\n"
        "</script>"
        + _monitor_inline_script("monitor-manage.js")
    )
    return _layout("Manage catalog", body)


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
    harnesses = _harness_wizard_entries()
    provider_options = (
        "openai", "anthropic", "google", "huggingface", "openrouter",
        "ollama", "lm-studio", "local", "other",
    )
    provider_html = "".join(
        f"<option value='{html.escape(provider)}'>{html.escape(provider)}</option>"
        for provider in provider_options
    )
    tool_options = (
        ("files", "Files"),
        ("git", "Git"),
        ("shell", "Shell"),
        ("browser", "Browser"),
        ("http", "HTTP/network"),
        ("package-manager", "Package manager"),
        ("database", "Database"),
    )
    tools_html = "".join(
        "<label style='display:flex; align-items:center; gap:0.35rem;'>"
        f"<input type='checkbox' name='tools' value='{html.escape(value)}' "
        f"{'checked' if value in {'files', 'git', 'shell'} else ''}>"
        f"{html.escape(label)}</label>"
        for value, label in tool_options
    )
    harness_cards = "".join(
        "<div class='harness-card' "
        f"data-harness-slug='{html.escape(row['slug'])}' "
        f"data-harness-text='{html.escape(' '.join([row['slug'], row['title'], row['description'], *row['tags']]).lower())}' "
        f"data-harness-score='{float(row['score']):.3f}'>"
        "<div style='display:flex; justify-content:space-between; gap:0.5rem; align-items:start;'>"
        f"<strong>{html.escape(row['title'])}</strong>"
        + (
            f"<span class='pill grade-{html.escape(row['grade'])}'>{html.escape(row['grade'])}</span>"
            if row["grade"]
            else "<span class='pill entity-type-harness'>harness</span>"
        )
        + "</div>"
        f"<p class='muted' style='margin:0;'>{html.escape(row['description'] or 'No description available.')}</p>"
        + (
            "<div class='muted' style='font-size:0.78rem;'>"
            + " ".join(f"<code>{html.escape(tag)}</code>" for tag in row["tags"][:8])
            + "</div>"
            if row["tags"]
            else ""
        )
        + (
            f"<a class='muted' href='{html.escape(row['repo_url'])}'>{html.escape(row['repo_url'])}</a>"
            if row["repo_url"].startswith(("http://", "https://"))
            else ""
        )
        + f"<code>ctx-harness-install {html.escape(row['slug'])} --dry-run</code>"
        + f"<button type='button' class='secondary' data-select-harness='{html.escape(row['slug'])}'>select</button>"
        + "</div>"
        for row in harnesses
    )
    if not harness_cards:
        harness_cards = (
            "<p class='muted'>No harness pages were found under "
            "<code>~/.claude/skill-wiki/entities/harnesses/</code>. "
            "Use the no-fit PRD output below to build an attachable harness.</p>"
        )

    body = (
        "<div class='setup-header'>"
        "<div><div class='setup-kicker'>Model -> intent -> install -> attach ctx</div>"
        "<h1>Harness Setup</h1>"
        "<p class='muted'>For users running their own API or local model instead of Claude Code. "
        "Interview the model/runtime choice, generate a real ctx harness recommendation command, "
        "then install a harness or produce a no-fit PRD for a custom harness.</p></div>"
        "<span class='pill entity-type-harness'>local/API model path</span>"
        "</div>"
        "<div class='setup-flow'>"
        "<div class='setup-flow-step'><strong>1. Model</strong><span class='muted'>Provider, model slug, endpoint.</span></div>"
        "<div class='setup-flow-step'><strong>2. Intent</strong><span class='muted'>Goal, OS, access, privacy.</span></div>"
        "<div class='setup-flow-step'><strong>3. Install</strong><span class='muted'>Recommend, dry-run, install.</span></div>"
        "<div class='setup-flow-step'><strong>4. Attach ctx</strong><span class='muted'>Graph/wiki recommendations flow into the harness.</span></div>"
        "</div>"
        "<div class='wizard-layout'>"
        "<form id='harness-wizard-form' class='card'>"
        "<div class='wizard-step'><strong>1. Model</strong>"
        "<div class='wizard-grid' style='margin-top:0.65rem;'>"
        "<label>Model provider <span class='pill grade-A'>Required</span>"
        f"<select name='model_provider' required>{provider_html}</select></label>"
        "<label>Model slug <span class='pill grade-A'>Required</span>"
        "<input name='model' required placeholder='openai/gpt-5.5 or ollama/qwen3-coder'></label>"
        "<label class='wide'>API base URL or local endpoint"
        "<input name='endpoint' placeholder='https://api.openai.com/v1 or http://localhost:11434'></label>"
        "</div></div>"
        "<div class='wizard-step'><strong>2. Goal and access</strong>"
        "<div class='wizard-grid' style='margin-top:0.65rem;'>"
        "<label class='wide'>Development goal <span class='pill grade-A'>Required</span>"
        "<textarea name='goal' rows='4' required placeholder='What should the agent build, fix, research, or operate?'></textarea></label>"
        "<label>Runtime / OS"
        "<select name='runtime'><option>windows</option><option>macos</option><option>linux</option>"
        "<option selected>cross-platform</option></select></label>"
        "<label>Autonomy"
        "<select name='autonomy'><option>read-only</option><option selected>repo-write</option>"
        "<option>deploy-capable</option></select></label>"
        "<label class='wide'>Allowed tools"
        f"<div style='display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:0.25rem;'>{tools_html}</div></label>"
        "<label>Verification gates"
        "<input name='verify' placeholder='pytest, ruff, mypy, build, smoke'></label>"
        "<label>Privacy / network"
        "<select name='privacy'><option selected>local repo only</option><option>network allowed</option>"
        "<option>secrets allowed by env only</option><option>offline only</option></select></label>"
        "<label>ctx attachment"
        "<select name='attach_mode'><option selected>mcp</option><option>python</option><option>cli</option></select></label>"
        "</div></div>"
        "<div class='wizard-step'><strong>3. Recommend and install</strong>"
        "<p class='muted'>The dashboard previews catalog matches. The command below calls the real harness recommender and keeps the no-fit path available.</p>"
        "<button type='submit'>build recommendation command</button> "
        "<button type='button' id='harness-reset' class='secondary'>reset</button>"
        "</div>"
        "</form>"
        "<aside class='card'>"
        "<h2 style='margin-top:0;'>Command plan</h2>"
        "<pre class='command-box' data-testid='harness-command-output'>ctx-harness-install --recommend --goal \"...\" --model-provider openai --model openai/gpt-5.5 --top-k 5 --plan-on-no-fit</pre>"
        "<p class='muted'>Run the dry-run first. The installer writes attach files under the harness target so the selected harness can connect to ctx graph/wiki recommendations.</p>"
        "<div id='selected-harness-command' class='muted'>Select a harness card to see install, update, and validation commands.</div>"
        "</aside>"
        "</div>"
        "<section class='card'>"
        "<div style='display:flex; justify-content:space-between; gap:0.75rem; align-items:center; flex-wrap:wrap;'>"
        "<div><h2 style='margin:0;'>Catalog harnesses</h2>"
        "<p class='muted' style='margin:0.2rem 0 0;'>Cards are filtered by the interview text. If none fit, use the no-fit PRD path.</p></div>"
        "<span id='harness-match-count' class='pill entity-type-harness'>0 matches</span>"
        "</div>"
        "<div id='harness-cards' style='display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:0.7rem; margin-top:0.8rem;'>"
        + harness_cards
        + "</div>"
        "</section>"
        "<section class='card'>"
        "<h2>No-fit custom harness PRD</h2>"
        "<p class='muted'>When no catalog harness clears the configured match score, generate a PRD for the user's strong model or engineering team. It must include orchestration, durable state, permissions, verification gates, and ctx recommendation hooks.</p>"
        "<pre class='command-box' id='no-fit-command'>ctx-harness-install --recommend --goal \"...\" --model-provider openai --model openai/gpt-5.5 --plan-on-no-fit --plan-output custom-harness-prd.md</pre>"
        "</section>"
        "<script>\n"
        "(function () {\n"
        "  const form = document.getElementById('harness-wizard-form');\n"
        "  const output = document.querySelector('[data-testid=\"harness-command-output\"]');\n"
        "  const noFit = document.getElementById('no-fit-command');\n"
        "  const selected = document.getElementById('selected-harness-command');\n"
        "  const count = document.getElementById('harness-match-count');\n"
        "  const cards = Array.from(document.querySelectorAll('.harness-card'));\n"
        "  function value(name) { const el = form.elements[name]; return el ? String(el.value || '').trim() : ''; }\n"
        "  function shellQuote(value) { return '\"' + String(value || '').replace(/\\\\/g, '\\\\\\\\').replace(/\"/g, '\\\\\"') + '\"'; }\n"
        "  function checkedTools() { return Array.from(form.querySelectorAll('input[name=\"tools\"]:checked')).map(el => el.value).join(','); }\n"
        "  function arg(flag, val) { return val ? ' ' + flag + ' ' + shellQuote(val) : ''; }\n"
        "  function recommendCommand() {\n"
        "    const tools = checkedTools();\n"
        "    let cmd = 'ctx-harness-install --recommend';\n"
        "    cmd += arg('--goal', value('goal'));\n"
        "    cmd += arg('--model-provider', value('model_provider'));\n"
        "    cmd += arg('--model', value('model'));\n"
        "    cmd += arg('--harness-runtime', value('runtime'));\n"
        "    cmd += arg('--harness-autonomy', value('autonomy'));\n"
        "    cmd += arg('--harness-tools', tools);\n"
        "    cmd += arg('--harness-verify', value('verify'));\n"
        "    cmd += arg('--harness-privacy', value('privacy'));\n"
        "    cmd += arg('--harness-attach-mode', value('attach_mode'));\n"
        "    return cmd + ' --top-k 5 --plan-on-no-fit';\n"
        "  }\n"
        "  function fitCards() {\n"
        "    const intent = [value('goal'), value('model_provider'), value('model'), value('runtime'), value('autonomy'), checkedTools(), value('verify'), value('privacy'), value('attach_mode')].join(' ').toLowerCase();\n"
        "    const terms = intent.split(/[^a-z0-9_.-]+/).filter(Boolean);\n"
        "    const host = document.getElementById('harness-cards');\n"
        "    let visible = 0;\n"
        "    cards.forEach(card => {\n"
        "      const text = card.dataset.harnessText || '';\n"
        "      const base = Number(card.dataset.harnessScore || 0);\n"
        "      const hits = terms.filter(term => text.includes(term)).length;\n"
        "      const fit = base + hits * 0.08;\n"
        "      card.dataset.fit = fit.toFixed(3);\n"
        "      const hide = terms.length > 0 && fit < 0.12;\n"
        "      card.dataset.fitHidden = hide ? 'true' : 'false';\n"
        "      if (!hide) visible++;\n"
        "    });\n"
        "    cards.sort((a, b) => Number(b.dataset.fit || 0) - Number(a.dataset.fit || 0)).forEach(card => host.appendChild(card));\n"
        "    count.textContent = visible + ' matches';\n"
        "  }\n"
        "  function refresh() {\n"
        "    const cmd = recommendCommand();\n"
        "    output.textContent = cmd;\n"
        "    noFit.textContent = cmd + ' --plan-output custom-harness-prd.md';\n"
        "    fitCards();\n"
        "  }\n"
        "  form.addEventListener('submit', ev => { ev.preventDefault(); refresh(); });\n"
        "  form.addEventListener('input', refresh);\n"
        "  document.getElementById('harness-reset').addEventListener('click', () => { form.reset(); refresh(); });\n"
        "  document.querySelectorAll('[data-select-harness]').forEach(btn => btn.addEventListener('click', () => {\n"
        "    const slug = btn.dataset.selectHarness || '';\n"
        "    cards.forEach(card => card.classList.toggle('selected', card.dataset.harnessSlug === slug));\n"
        "    selected.innerHTML = '<pre class=\"command-box\">ctx-harness-install ' + slug + ' --dry-run\\nctx-harness-install ' + slug + '\\nctx-harness-install ' + slug + ' --update --dry-run\\nctx-scan-repo --repo . --recommend\\nctx-monitor serve</pre>';\n"
        "  }));\n"
        "  refresh();\n"
        "})();\n"
        "</script>"
    )
    return _layout("Harness Setup", body)


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
    """HTML-rendered KPI dashboard — grades, lifecycle, categories,
    hard floors, top demotion candidates, archived entities.

    Mirrors the structure of ``kpi_dashboard.render_markdown`` so the
    commit-friendly Markdown digest and the browser view show the
    same numbers.
    """
    summary = _kpi_summary()
    if summary is None or summary.total == 0:
        empty = (
            "<h1>KPIs</h1>"
            "<div class='card'><strong>No KPI data yet.</strong>"
            "<p class='muted' style='margin-top:0.4rem;'>"
            "The KPI dashboard reads from "
            "<code>~/.claude/skill-quality/*.json</code> and "
            "<code>*.lifecycle.json</code>. Run "
            "<code>ctx-skill-quality recompute --all</code> to populate "
            "sidecars, then reload this page.</p>"
            "<p class='muted'>CLI equivalent: "
            "<code>python -m kpi_dashboard render</code></p></div>"
        )
        return _layout("KPIs", empty)

    total = summary.total

    # Grade distribution pills + detail table
    grade_pills = "".join(
        f"<span class='pill grade-{g}'>{g}: {summary.grade_counts.get(g, 0)}</span> "
        for g in ("A", "B", "C", "D", "F")
    )

    def pct(n: int) -> str:
        return f"{(100.0 * n / total):.1f}%" if total else "—"

    grade_rows = "".join(
        f"<tr><td><span class='pill grade-{g}'>{g}</span></td>"
        f"<td>{summary.grade_counts.get(g, 0)}</td>"
        f"<td class='muted'>{pct(summary.grade_counts.get(g, 0))}</td></tr>"
        for g in ("A", "B", "C", "D", "F")
    )

    lifecycle_rows = "".join(
        f"<tr><td><code>{html.escape(state)}</code></td>"
        f"<td>{summary.lifecycle_counts.get(state, 0)}</td></tr>"
        for state in ("active", "watch", "demote", "archive")
    )

    floor_rows = "".join(
        f"<tr><td><code>{html.escape(reason)}</code></td><td>{count}</td></tr>"
        for reason, count in sorted(
            summary.hard_floor_counts.items(), key=lambda kv: (-kv[1], kv[0]),
        )
    ) or "<tr><td colspan='2' class='muted'>No hard floors active.</td></tr>"

    category_rows = "".join(
        "<tr>"
        f"<td>{html.escape(c['category'])}</td>"
        f"<td>{c['count']}</td>"
        f"<td class='muted'>{c['avg_score']:.3f}</td>"
        f"<td><span class='pill grade-A'>{c['grade_mix'].get('A', 0)}</span></td>"
        f"<td><span class='pill grade-B'>{c['grade_mix'].get('B', 0)}</span></td>"
        f"<td><span class='pill grade-C'>{c['grade_mix'].get('C', 0)}</span></td>"
        f"<td><span class='pill grade-D'>{c['grade_mix'].get('D', 0)}</span></td>"
        f"<td><span class='pill grade-F'>{c['grade_mix'].get('F', 0)}</span></td>"
        "</tr>"
        for c in summary.category_breakdown
    ) or "<tr><td colspan='8' class='muted'>No categorized entities.</td></tr>"

    def detail_href(slug: str, entity_type: str) -> str:
        normalized = _normalize_dashboard_entity_type(entity_type)
        suffix = f"?type={html.escape(normalized)}" if normalized else ""
        return f"/skill/{html.escape(slug)}{suffix}"

    demotion_rows = "".join(
        "<tr>"
        f"<td><a href='{detail_href(c['slug'], c['subject_type'])}'><code>{html.escape(c['slug'])}</code></a></td>"
        f"<td class='muted'>{html.escape(c['subject_type'])}</td>"
        f"<td class='muted'>{html.escape(c['category'])}</td>"
        f"<td><span class='pill grade-{html.escape(c['grade'])}'>{html.escape(c['grade'])}</span></td>"
        f"<td class='muted'>{c['score']:.3f}</td>"
        f"<td class='muted'>{html.escape(c['lifecycle_state'])}</td>"
        f"<td>{c['consecutive_d_count']}</td>"
        f"<td class='muted'>{html.escape(c.get('hard_floor') or '—')}</td>"
        "</tr>"
        for c in summary.low_quality_candidates
    ) or "<tr><td colspan='8' class='muted'>No active D/F grade entries — corpus is healthy.</td></tr>"

    archived_rows = "".join(
        "<tr>"
        f"<td><a href='{detail_href(a['slug'], a['subject_type'])}'><code>{html.escape(a['slug'])}</code></a></td>"
        f"<td class='muted'>{html.escape(a['subject_type'])}</td>"
        f"<td class='muted'>{html.escape(a['category'])}</td>"
        f"<td class='muted'>{html.escape(a.get('last_grade') or '—')}</td>"
        f"<td class='muted'>{html.escape(a.get('computed_at') or '—')}</td>"
        "</tr>"
        for a in summary.archived
    ) or "<tr><td colspan='5' class='muted'>None.</td></tr>"

    by_subject = summary.by_subject
    subject_blurb = " · ".join(
        f"{html.escape(s)}: {n}" for s, n in sorted(by_subject.items())
    ) or "—"

    body = (
        "<h1>KPIs</h1>"
        "<p class='muted'>Aggregated from "
        "<code>~/.claude/skill-quality/*.json</code> (quality sidecars) "
        "and <code>*.lifecycle.json</code> (tier sidecars). "
        f"Generated {html.escape(summary.generated_at)}.</p>"
        "<div class='card'>"
        f"<strong>Total entities:</strong> {total} "
        f"<span class='muted'>· {subject_blurb}</span>"
        f"<div style='margin-top:0.5rem;'>{grade_pills}</div>"
        "<div style='margin-top:0.4rem;'>"
        "<a href='/api/kpi.json'>JSON</a> · "
        "<a href='/skills'>skill cards →</a></div>"
        "</div>"
        "<div style='display:grid; grid-template-columns:1fr 1fr; gap:1rem;'>"
        "<div class='card'><strong>Grade distribution</strong>"
        "<table><tr><th>Grade</th><th>Count</th><th>Share</th></tr>"
        + grade_rows + "</table></div>"
        "<div class='card'><strong>Lifecycle tiers</strong>"
        "<table><tr><th>State</th><th>Count</th></tr>"
        + lifecycle_rows + "</table></div>"
        "</div>"
        "<div class='card'><strong>Hard floors active</strong>"
        "<table><tr><th>Reason</th><th>Count</th></tr>"
        + floor_rows + "</table></div>"
        "<div class='card'><strong>By category</strong>"
        "<table><tr><th>Category</th><th>Count</th><th>Avg score</th>"
        "<th>A</th><th>B</th><th>C</th><th>D</th><th>F</th></tr>"
        + category_rows + "</table></div>"
        "<div class='card'><strong>Top demotion candidates</strong> "
        "<span class='muted'>(active or watch · grade D/F · sorted by D-streak desc, score asc)</span>"
        "<table><tr><th>Slug</th><th>Type</th><th>Category</th><th>Grade</th>"
        "<th>Score</th><th>State</th><th>D-streak</th><th>Hard floor</th></tr>"
        + demotion_rows + "</table></div>"
        "<div class='card'><strong>Archived</strong>"
        "<table><tr><th>Slug</th><th>Type</th><th>Category</th>"
        "<th>Last grade</th><th>Computed at</th></tr>"
        + archived_rows + "</table></div>"
    )
    return _layout("KPIs", body)


def _render_status() -> str:
    """Render queue and graph/wiki artifact state for operator checks."""
    status = _status_payload()
    queue = status["queue"]
    artifacts = status["artifacts"]
    counts = queue.get("counts", {})
    count_pills = " ".join(
        f"<span class='pill'>{html.escape(name)}: {int(counts.get(name, 0))}</span>"
        for name in (
            wiki_queue.STATUS_PENDING,
            wiki_queue.STATUS_RUNNING,
            wiki_queue.STATUS_SUCCEEDED,
            wiki_queue.STATUS_FAILED,
            wiki_queue.STATUS_CANCELLED,
        )
    )

    job_rows = "".join(
        "<tr>"
        f"<td>{job.get('id')}</td>"
        f"<td><code>{html.escape(str(job.get('kind') or ''))}</code></td>"
        f"<td><span class='pill'>{html.escape(str(job.get('status') or ''))}</span></td>"
        f"<td>{job.get('attempts')}/{job.get('max_attempts')}</td>"
        f"<td class='muted'>{html.escape(str(job.get('source') or ''))}</td>"
        f"<td class='muted'>{html.escape(str(job.get('worker_id') or ''))}</td>"
        f"<td class='muted'>{html.escape(str(job.get('last_error') or ''))[:120]}</td>"
        "</tr>"
        for job in queue.get("recent_jobs", [])
    ) or "<tr><td colspan='7' class='muted'>No queue jobs recorded.</td></tr>"

    artifact_keys = (
        ("graph_json", "graph.json"),
        ("graph_delta_json", "graph-delta.json"),
        ("communities_json", "communities.json"),
        ("wiki_graph_tar", "wiki-graph.tar.gz"),
        ("skills_sh_catalog", "skill-index.json.gz"),
    )
    artifact_rows = "".join(
        "<tr>"
        f"<td><code>{label}</code></td>"
        f"<td>{'yes' if artifacts[key].get('exists') else 'no'}</td>"
        f"<td>{int(artifacts[key].get('size') or 0):,}</td>"
        f"<td class='muted'>{html.escape(str(artifacts[key].get('path') or ''))}</td>"
        "</tr>"
        for key, label in artifact_keys
    )

    promotion_rows = "".join(
        "<tr>"
        f"<td><span class='pill'>{html.escape(str(row.get('status') or ''))}</span></td>"
        f"<td class='muted'>{html.escape(str(row.get('promoted_at') or row.get('started_at') or ''))}</td>"
        f"<td class='muted'><code>{html.escape(str(row.get('current_sha256') or row.get('candidate_sha256') or ''))[:16]}</code></td>"
        f"<td class='muted'>{html.escape(str(row.get('target') or ''))}</td>"
        "</tr>"
        for row in artifacts.get("promotions", [])
    ) or "<tr><td colspan='4' class='muted'>No promotion metadata recorded.</td></tr>"

    queue_error = queue.get("error")
    if queue_error:
        availability = f"error ({html.escape(str(queue.get('db_path') or ''))})"
    elif queue.get("available"):
        availability = "available"
    else:
        availability = f"not initialized ({html.escape(str(queue.get('db_path') or ''))})"
    queue_error_html = (
        "<p class='error'>Queue DB error: "
        f"{html.escape(str(queue_error))}</p>"
        if queue_error
        else ""
    )
    body = (
        "<h1>Status</h1>"
        "<div class='card'>"
        "<strong>Queue state</strong>"
        f"<p class='muted'>Durable worker DB: {availability}. "
        f"Total jobs: {int(queue.get('total') or 0)}. "
        "<a href='/api/status.json'>JSON</a></p>"
        f"{queue_error_html}"
        f"<div>{count_pills}</div>"
        "</div>"
        "<div class='card'><strong>Recent queue jobs</strong>"
        "<table><tr><th>ID</th><th>Kind</th><th>Status</th><th>Attempts</th>"
        "<th>Source</th><th>Worker</th><th>Last error</th></tr>"
        + job_rows
        + "</table></div>"
        "<div class='card'><strong>Artifact versions</strong>"
        "<table><tr><th>Artifact</th><th>Exists</th><th>Bytes</th><th>Path</th></tr>"
        + artifact_rows
        + "</table></div>"
        f"<div class='card'><strong>Artifact promotions ({artifacts.get('promotion_count', 0)})</strong>"
        "<table><tr><th>Status</th><th>Time</th><th>Hash</th><th>Target</th></tr>"
        + promotion_rows
        + "</table></div>"
    )
    return _layout("Status", body)


def _render_events() -> str:
    """SSE endpoint page. The server emits events at /api/events.stream."""
    entries = _read_jsonl(_audit_log_path(), limit=200)
    event_lines = [
        json.dumps(entry, ensure_ascii=False, default=str)
        for entry in entries
    ]
    initial_stream = "\n".join(event_lines)
    if not initial_stream:
        initial_stream = "-- no audit events recorded yet; waiting for new events --"
    return _layout(
        "Live events",
        "<h1>Live events</h1>"
        "<p class='muted'>Tails <code>~/.claude/ctx-audit.jsonl</code> "
        "via server-sent events.</p>"
        "<div class='card'>"
        f"<strong>Showing last {len(entries)} audit events</strong>; "
        "new writes append below. "
        "<span id='stream-status' class='muted'>connecting...</span>"
        "</div>"
        "<pre id='stream' style='min-height:20rem; max-height:70vh; "
        "overflow-y:scroll; font-size:0.78rem;'>"
        f"{html.escape(initial_stream)}"
        "</pre>"
        "<script>\n"
        "const src = new EventSource('/api/events.stream');\n"
        "const pre = document.getElementById('stream');\n"
        "const status = document.getElementById('stream-status');\n"
        "const appendLine = (line) => {\n"
        "  if (pre.textContent && !pre.textContent.endsWith('\\n')) pre.textContent += '\\n';\n"
        "  pre.textContent += line + '\\n';\n"
        "  pre.scrollTop = pre.scrollHeight;\n"
        "};\n"
        "pre.scrollTop = pre.scrollHeight;\n"
        "src.onopen = () => { status.textContent = 'connected; waiting for new events'; };\n"
        "src.onmessage = (e) => { appendLine(e.data); status.textContent = 'live'; };\n"
        "src.onerror = () => { status.textContent = 'stream error; reconnecting'; };\n"
        "</script>",
    )


def _render_loaded(mutations_enabled: bool | None = None) -> str:
    """Live view of ~/.claude/skill-manifest.json with load/unload actions.

    Groups manifest entries by ``entity_type`` (skill / agent / mcp-server / harness)
    with a per-section count. Unload button posts both the slug and
    entity_type so the server routes correctly — MCPs need
    ``claude mcp remove``, skills + agents take the file-copy path.
    Legacy entries without entity_type default to ``skill`` (what the
    pre-install_utils manifest implicitly assumed).
    """
    if mutations_enabled is None:
        mutations_enabled = _MONITOR_MUTATIONS_ENABLED
    manifest = _read_manifest()
    load_rows = manifest.get("load", [])
    unload_rows = manifest.get("unload", [])

    def _etype(entry: dict) -> str:
        # Missing entity_type => legacy skill entry.
        return str(entry.get("entity_type") or "skill")

    # Split loaded by entity_type for the sectioned layout.
    by_type: dict[str, list[dict]] = {
        "skill": [],
        "agent": [],
        "mcp-server": [],
        "harness": [],
    }
    for e in load_rows:
        by_type.setdefault(_etype(e), []).append(e)

    disabled_attr = "" if mutations_enabled else " disabled"
    mutation_token = _MONITOR_TOKEN if mutations_enabled else ""
    mutation_notice = (
        ""
        if mutations_enabled
        else (
            "<div class='card'><strong>Read-only mode.</strong> "
            "Load/unload actions are disabled because ctx-monitor is not "
            "bound to a loopback address.</div>"
        )
    )

    def _row(e: dict) -> str:
        slug = e.get("skill", "")
        etype = _etype(e)
        link = (
            f"<a href='/wiki/{html.escape(slug)}?type={html.escape(etype)}'>"
            f"<code>{html.escape(slug)}</code></a>"
        )
        action = (
            f"<td class='muted'><code>ctx-harness-install {html.escape(slug)} "
            f"--uninstall --dry-run</code></td>"
            if etype == "harness" else
            f"<td><button class='btn-unload' data-slug='{html.escape(slug)}' "
            f"data-etype='{html.escape(etype)}'{disabled_attr}>unload</button></td>"
        )
        return (
            f"<tr>"
            f"<td>{link}</td>"
            f"<td class='muted'>{html.escape(e.get('source', ''))}</td>"
            f"<td class='muted'>{html.escape(str(e.get('command', '') or e.get('priority', '—')))[:60]}</td>"
            f"{action}"
            f"</tr>"
        )

    def _section(title: str, etype: str) -> str:
        rows = by_type.get(etype, [])
        if not rows:
            return (
                f"<h3 style='margin-top:1.2rem;'>{title} "
                f"<span class='muted' style='font-size:0.85rem;'>(0)</span></h3>"
                f"<p class='muted' style='margin-left:0.4rem;'>"
                f"None loaded.</p>"
            )
        return (
            f"<h3 style='margin-top:1.2rem;'>{title} "
            f"<span class='muted' style='font-size:0.85rem;'>({len(rows)})</span></h3>"
            f"<table>"
            f"<tr><th>Slug</th><th>Source</th><th>Cmd / priority</th><th></th></tr>"
            + "".join(_row(e) for e in rows)
            + "</table>"
        )

    unload_html = "".join(
        f"<tr>"
        f"<td><code>{html.escape(e.get('skill', ''))}</code></td>"
        f"<td class='muted'>{html.escape(_etype(e))}</td>"
        f"<td class='muted'>{html.escape(str(e.get('source', '') or e.get('reason', ''))[:80])}</td>"
        f"<td><button class='btn-load' data-slug='{html.escape(e.get('skill', ''))}' "
        f"data-etype='{html.escape(_etype(e))}'"
        f"{' data-command=' + repr(html.escape(str(e.get('command')))) if e.get('command') else ''}"
        f"{' data-json-config=' + repr(html.escape(str(e.get('json_config')))) if e.get('json_config') else ''}"
        f"{disabled_attr}>load</button></td>"
        f"</tr>"
        for e in unload_rows
    )

    body = (
        "<h1>Loaded entities — skills, agents, MCPs &amp; harnesses</h1>"
        f"<div class='card'>"
        f"<strong>{len(load_rows)}</strong> currently loaded "
        f"(<span class='muted'>"
        f"{len(by_type.get('skill', []))} skills · "
        f"{len(by_type.get('agent', []))} agents · "
        f"{len(by_type.get('mcp-server', []))} MCPs · "
        f"{len(by_type.get('harness', []))} harnesses</span>) · "
        f"<strong>{len(unload_rows)}</strong> known-unloaded · "
        f"<span class='muted'>source: <code>~/.claude/skill-manifest.json</code> "
        f"+ <code>~/.claude/harness-installs/*.json</code></span>"
        "</div>"
        f"{mutation_notice}"
        "<h2>Load an entity</h2>"
        "<div class='card'>"
        "<form id='load-form'>"
        "<input type='text' id='load-input' placeholder='slug (e.g. fastapi-pro)' "
        "style='padding:0.35rem 0.6rem; width:18rem; border:1px solid #ccc; "
        "border-radius:4px;'>"
        "<select id='load-type' style='margin-left:0.5rem; padding:0.35rem 0.6rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<option value='skill'>skill</option>"
        "<option value='agent'>agent</option>"
        "<option value='mcp-server'>mcp-server</option>"
        "</select>"
        f"<button type='submit' style='margin-left:0.5rem;'{disabled_attr}>load</button>"
        "<span id='load-msg' class='muted' style='margin-left:0.75rem;'></span>"
        "</form></div>"
        f"<h2>Currently loaded ({len(load_rows)})</h2>"
        + _section("Skills", "skill")
        + _section("Agents", "agent")
        + _section("MCP servers", "mcp-server")
        + _section("Harnesses", "harness")
        + f"<h2>Recently unloaded ({len(unload_rows)})</h2>"
        "<table><tr><th>Slug</th><th>Type</th><th>Source / reason</th><th></th></tr>"
        + unload_html + "</table>"
        "<script>\n"
        f"const CTX_MONITOR_MUTATIONS_ENABLED = {json.dumps(mutations_enabled)};\n"
        f"const CTX_MONITOR_TOKEN = {json.dumps(mutation_token)};\n"
        "async function post(url, body) {\n"
        "  if (!CTX_MONITOR_MUTATIONS_ENABLED) return {ok:false, msg:'mutations disabled on non-loopback bind'};\n"
        "  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json', 'X-CTX-Monitor-Token':CTX_MONITOR_TOKEN}, body: JSON.stringify(body || {})});\n"
        "  const ok = r.status >= 200 && r.status < 300;\n"
        "  let msg = ''; try { msg = (await r.json()).detail || r.statusText; } catch(_) { msg = r.statusText; }\n"
        "  return {ok, msg};\n"
        "}\n"
        "document.querySelectorAll('.btn-unload').forEach(b => b.addEventListener('click', async () => {\n"
        "  b.disabled = true; const slug = b.dataset.slug; const entity_type = b.dataset.etype || 'skill';\n"
        "  const r = await post('/api/unload', {slug, entity_type});\n"
        "  if (r.ok) location.reload(); else { b.disabled = false; alert('unload failed: ' + r.msg); }\n"
        "}));\n"
        "document.querySelectorAll('.btn-load').forEach(b => b.addEventListener('click', async () => {\n"
        "  b.disabled = true; const slug = b.dataset.slug; const entity_type = b.dataset.etype || 'skill';\n"
        "  const payload = {slug, entity_type};\n"
        "  if (b.dataset.command) payload.command = b.dataset.command;\n"
        "  if (b.dataset.jsonConfig) payload.json_config = b.dataset.jsonConfig;\n"
        "  const r = await post('/api/load', payload);\n"
        "  if (r.ok) location.reload(); else { b.disabled = false; alert('load failed: ' + r.msg); }\n"
        "}));\n"
        "document.getElementById('load-form').addEventListener('submit', async (ev) => {\n"
        "  ev.preventDefault();\n"
        "  const slug = document.getElementById('load-input').value.trim();\n"
        "  const entity_type = document.getElementById('load-type').value;\n"
        "  if (!slug) return;\n"
        "  document.getElementById('load-msg').textContent = 'loading…';\n"
        "  const r = await post('/api/load', {slug, entity_type});\n"
        "  document.getElementById('load-msg').textContent = r.ok ? 'ok — reloading' : ('failed: ' + r.msg);\n"
        "  if (r.ok) setTimeout(() => location.reload(), 400);\n"
        "});\n"
        "</script>"
    )
    return _layout("Loaded", body)


def _render_runtime_lifecycle() -> str:
    summary = _runtime_lifecycle_summary()

    def _event_cell(event: dict[str, Any], key: str, limit: int = 120) -> str:
        return html.escape(str(event.get(key) or ""))[:limit]

    validation_rows = "".join(
        "<tr>"
        f"<td class='muted'>{_event_cell(event, 'created_at')}</td>"
        f"<td><code>{_event_cell(event, 'check_name')}</code></td>"
        f"<td><span class='pill'>{_event_cell(event, 'status')}</span></td>"
        f"<td class='muted'>{_event_cell(event, 'session_id')}</td>"
        f"<td class='muted'>{_event_cell(event, 'summary')}</td>"
        "</tr>"
        for event in reversed(summary["recent_validations"])
    )
    escalation_rows = "".join(
        "<tr>"
        f"<td class='muted'>{_event_cell(event, 'created_at')}</td>"
        f"<td><code>{_event_cell(event, 'trigger')}</code></td>"
        f"<td><span class='pill'>{_event_cell(event, 'severity')}</span></td>"
        f"<td class='muted'>{_event_cell(event, 'session_id')}</td>"
        f"<td class='muted'>{_event_cell(event, 'reason')}</td>"
        "</tr>"
        for event in reversed(summary["open_escalations"])
    )

    body = (
        "<h1>Runtime lifecycle</h1>"
        "<div class='card'>"
        f"<strong>{summary['validations_total']}</strong> validations / "
        f"<strong>{summary['validation_failures']}</strong> failed / "
        f"<strong>{summary['open_escalations_total']}</strong> open escalations"
        f"<br><span class='muted'>source: <code>{html.escape(summary['path'])}</code></span>"
        " / <a href='/api/runtime.json'>JSON</a>"
        "</div>"
        "<div class='card'><strong>Recent validations</strong>"
        + (
            "<table><tr><th>Created</th><th>Check</th><th>Status</th>"
            "<th>Session</th><th>Summary</th></tr>"
            + validation_rows
            + "</table>"
            if validation_rows else
            "<p class='muted'>No validation checks recorded yet.</p>"
        )
        + "</div>"
        "<div class='card'><strong>Open escalations</strong>"
        + (
            "<table><tr><th>Created</th><th>Trigger</th><th>Severity</th>"
            "<th>Session</th><th>Reason</th></tr>"
            + escalation_rows
            + "</table>"
            if escalation_rows else
            "<p class='muted'>No open escalations.</p>"
        )
        + "</div>"
    )
    return _layout("Runtime lifecycle", body)


def _render_logs() -> str:
    """Filterable audit-log viewer — reads the last 500 lines of the log."""
    entries = _read_jsonl(_audit_log_path(), limit=500)
    rows = "".join(
        f"<tr data-event='{html.escape(e.get('event', ''))}' "
        f"data-subject='{html.escape(e.get('subject', ''))}' "
        f"data-session='{html.escape(e.get('session_id', '') or '')}'>"
        f"<td class='muted'>{html.escape(e.get('ts', ''))}</td>"
        f"<td><span class='pill'>{html.escape(e.get('event', ''))}</span></td>"
        f"<td><code>{html.escape(e.get('subject', ''))}</code></td>"
        f"<td class='muted'>{html.escape(e.get('actor', ''))}</td>"
        f"<td class='muted'>{html.escape((e.get('session_id') or '')[:24])}</td>"
        f"<td class='muted'>{html.escape(json.dumps(e.get('meta', {}))[:100])}</td>"
        f"</tr>"
        for e in reversed(entries)
    )
    body = (
        "<h1>Audit log</h1>"
        f"<div class='card'>Showing last {len(entries)} of "
        f"<code>~/.claude/ctx-audit.jsonl</code>. "
        "<a href='/events'>Live stream →</a>"
        "</div>"
        "<div class='card'>"
        "<input type='text' id='filter' placeholder='filter: event/subject/session…' "
        "style='padding:0.35rem 0.6rem; width:20rem; border:1px solid #ccc; border-radius:4px;'>"
        "<span class='muted' style='margin-left:0.75rem;'>"
        "e.g. <code>skill.loaded</code>, <code>kubernetes-deployment</code>, or a session id</span>"
        "</div>"
        "<table id='logs'><tr><th>ts</th><th>event</th><th>subject</th>"
        "<th>actor</th><th>session</th><th>meta</th></tr>" + rows + "</table>"
        "<script>\n"
        "const input = document.getElementById('filter');\n"
        "const rows = document.querySelectorAll('#logs tr[data-event]');\n"
        "input.addEventListener('input', () => {\n"
        "  const q = input.value.toLowerCase();\n"
        "  rows.forEach(r => {\n"
        "    const hay = [r.dataset.event, r.dataset.subject, r.dataset.session].join(' ').toLowerCase();\n"
        "    r.style.display = !q || hay.includes(q) ? '' : 'none';\n"
        "  });\n"
        "});\n"
        "</script>"
    )
    return _layout("Audit log", body)


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


def _server_shutdown_requested(server: Any) -> bool:
    event = getattr(server, "_ctx_shutdown", None)
    return bool(event is not None and event.is_set())


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

    def do_GET(self) -> None:  # noqa: N802 — stdlib signature
        # Parse once so we can reuse the query string for /graph?slug=…
        raw_path, _, raw_query = self.path.partition("?")
        path = raw_path
        qs = {}
        if raw_query:
            from urllib.parse import parse_qs
            qs = {k: v[0] for k, v in parse_qs(raw_query).items()}
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
            if path == "/":
                self._send_html(_render_home())
            elif path == "/sessions":
                self._send_html(_render_sessions_index())
            elif path.startswith("/session/"):
                self._send_html(_render_session_detail(path.split("/session/", 1)[1]))
            elif path == "/skills":
                self._send_html(_render_skills(qs))
            elif path == "/skillspector":
                self._send_html(_render_skillspector(qs))
            elif path.startswith("/skill/"):
                self._send_html(_render_skill_detail(
                    path.split("/skill/", 1)[1],
                    qs.get("type"),
                ))
            elif path == "/loaded":
                self._send_html(_render_loaded(self._mutations_enabled()))
            elif path == "/logs":
                self._send_html(_render_logs())
            elif path == "/graph":
                self._send_html(_render_graph(qs.get("slug"), qs.get("type")))
            elif path == "/manage":
                self._send_html(_render_manage(self._mutations_enabled()))
            elif path == "/harness":
                self._send_html(_render_harness_wizard())
            elif path == "/docs":
                self._send_html(_render_docs())
            elif path == "/config":
                self._send_html(_render_config())
            elif path == "/status":
                self._send_html(_render_status())
            elif path in {"/catalog", "/catalog/"}:
                self._send_html(_render_wiki_index(qs.get("type"), qs.get("q", "")))
            elif path == "/wiki":
                self._send_html(_render_wiki_index(qs.get("type"), qs.get("q", "")))
            elif path.startswith("/wiki/"):
                slug = path.split("/wiki/", 1)[1]
                self._send_html(
                    _render_wiki_entity(
                        slug,
                        qs.get("type"),
                        mutations_enabled=self._mutations_enabled(),
                    ),
                )
            elif path == "/kpi":
                self._send_html(_render_kpi())
            elif path == "/runtime":
                self._send_html(_render_runtime_lifecycle())
            elif path in {"/events", "/live"}:
                self._send_html(_render_events())
            elif path == "/api/sessions.json":
                self._send_json(_summarize_sessions())
            elif path == "/api/manifest.json":
                self._send_json(_read_manifest())
            elif path == "/api/status.json":
                self._send_json(_status_payload())
            elif path == "/api/kpi.json":
                summary = _kpi_summary()
                self._send_json(summary.to_dict() if summary is not None else {
                    "total": 0, "detail": "no sidecars yet",
                })
            elif path == "/api/grades.json":
                self._send_json(_grade_distribution_payload())
            elif path == "/api/sidecars.json":
                self._send_json(_sidecar_page_payload(qs))
            elif path == "/api/runtime.json":
                self._send_json(_runtime_lifecycle_summary())
            elif path == "/api/skillspector.json":
                self._send_json(_skillspector_audit_payload(qs))
            elif path == "/api/config.json":
                self._send_json(_effective_config_payload())
            elif path == "/api/entities/search.json":
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
            elif path.startswith("/api/entity/") and path.endswith(".json"):
                slug = unquote(path[len("/api/entity/"): -len(".json")])
                try:
                    detail = _wiki_entity_detail(slug, qs.get("type"))
                except ValueError as exc:
                    self._send_json_status(400, {"detail": str(exc)})
                    return
                if detail is None:
                    self._send_json_status(404, {"detail": f"no wiki entity for {slug}"})
                else:
                    self._send_json(detail)
            elif path.startswith("/api/skill/") and path.endswith(".json"):
                slug = unquote(path[len("/api/skill/"): -len(".json")])
                sidecar = _load_sidecar(slug, entity_type=qs.get("type"))
                if sidecar is None:
                    self._send_404(f"no sidecar for {slug}")
                else:
                    self._send_json(sidecar)
            elif path.startswith("/api/graph/") and path.endswith(".json"):
                slug = unquote(path[len("/api/graph/"): -len(".json")])
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
            elif path == "/api/events.stream":
                self._stream_audit_log()
            else:
                self._send_404(path)
        except (BrokenPipeError, ConnectionAbortedError):
            # Browser disconnected mid-response — benign for a local
            # dashboard; nothing to do.
            return
        except Exception as exc:  # noqa: BLE001 — last-resort handler
            self._send_500(exc)

    def do_POST(self) -> None:  # noqa: N802 — stdlib signature
        """Mutation endpoints. Same-origin only; JSON body required."""
        path = self.path.split("?", 1)[0]
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

            if path == "/api/load":
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
            elif path == "/api/unload":
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
            elif path == "/api/config":
                updates = body.get("updates", {})
                if not isinstance(updates, dict):
                    self._send_json_status(
                        400, {"ok": False, "detail": "updates must be an object"},
                    )
                    return
                result = _save_config_updates(updates)
                self._send_json_status(200 if result.get("ok") else 400, result)
            elif path == "/api/entity/upsert":
                ok, msg = _upsert_wiki_entity(body)
                self._send_json_status(
                    200 if ok else 400, {"ok": ok, "detail": msg},
                )
            elif path == "/api/entity/delete":
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


class _MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._ctx_shutdown = threading.Event()
        super().__init__(*args, **kwargs)

    def shutdown(self) -> None:
        self._ctx_shutdown.set()
        super().shutdown()

    def server_close(self) -> None:
        self._ctx_shutdown.set()
        super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc_type, _, _ = sys.exc_info()
        if exc_type is not None and issubclass(
            exc_type,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)


def _make_monitor_server(host: str, port: int) -> _MonitorServer:
    global _MONITOR_MUTATIONS_ENABLED
    _MONITOR_MUTATIONS_ENABLED = _host_allows_mutations(host)
    server = _MonitorServer((host, port), _MonitorHandler)
    server._ctx_mutations_enabled = _MONITOR_MUTATIONS_ENABLED
    return server


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
