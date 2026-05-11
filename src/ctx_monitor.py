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
    /skill/<slug>               Sidecar breakdown + timeline of audit events
    /wiki                       Wiki entity index — all pages with search
    /wiki/<slug>?type=<entity>  One wiki entity page (frontmatter + body)
    /graph                      Built-in graph explorer + popular seeds
    /graph?slug=<slug>&type=... Focus graph view on a specific entity
    /status                     Durable queue + graph/wiki artifact state
    /kpi                        Grade / lifecycle / category KPIs
    /runtime                    Generic harness validation/escalation ledger
    /logs                       Filterable tail of ctx-audit.jsonl
    /events                     Live SSE stream of new audit-log lines
    /api/sessions.json          JSON index for scripting
    /api/manifest.json          Raw ~/.claude/skill-manifest.json
    /api/status.json            Queue counts + artifact promotion metadata
    /api/runtime.json           Generic harness validation/escalation summary
    /api/skill/<slug>.json      Sidecar passthrough
    /api/graph/<slug>.json      Dashboard-shaped neighborhood; accepts type
    /api/kpi.json               DashboardSummary passthrough

Design notes:

- No Flask / Starlette / FastAPI dependency. stdlib only — keeps
  ``pip install claude-ctx`` lean. Request handling is threaded so one
  open SSE client cannot monopolize the local dashboard.
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
import html
import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ctx.core.wiki import wiki_queue
from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text
from ctx.utils._safe_name import is_safe_source_name


_MONITOR_TOKEN = ""
_MONITOR_MUTATIONS_ENABLED = True
_GRAPH_CACHE_KEY: tuple[Path, float, int, int] | None = None
_GRAPH_CACHE_VALUE: Any | None = None
_WIKI_INDEX_LIMIT_PER_TYPE = 500
_GRAPH_REPORT_RE = re.compile(r"Nodes:\s*([\d,]+)\s*\|\s*Edges:\s*([\d,]+)")
_MAX_POST_BODY_BYTES = 64 * 1024


# ─── Data sources ────────────────────────────────────────────────────────────


def _host_allows_mutations(host: str) -> bool:
    normalized = (host or "").strip().strip("[]").rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


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


def _load_dashboard_graph() -> Any:
    """Load the wiki graph once per graph.json file version."""
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE

    graph_path = _wiki_dir() / "graphify-out" / "graph.json"
    from ctx.core.graph.resolve_graph import load_graph as _lg  # type: ignore

    if not graph_path.exists():
        _GRAPH_CACHE_KEY = None
        _GRAPH_CACHE_VALUE = None
        return _lg(graph_path)

    stat = graph_path.stat()
    cache_key = (graph_path.resolve(), stat.st_mtime, stat.st_size, id(_lg))
    if _GRAPH_CACHE_KEY == cache_key and _GRAPH_CACHE_VALUE is not None:
        return _GRAPH_CACHE_VALUE

    graph = _lg(graph_path)
    _GRAPH_CACHE_KEY = cache_key
    _GRAPH_CACHE_VALUE = graph
    return graph


def _mcp_shard(slug: str) -> str:
    first = slug[0] if slug else ""
    return first if first.isalpha() else "0-9"


_DASHBOARD_ENTITY_SOURCES: tuple[tuple[str, str, bool], ...] = (
    ("skills", "skill", False),
    ("agents", "agent", False),
    ("mcp-servers", "mcp-server", True),
    ("harnesses", "harness", False),
)
_DASHBOARD_ENTITY_TYPES: tuple[str, ...] = tuple(
    entity_type for _, entity_type, _ in _DASHBOARD_ENTITY_SOURCES
)


def _normalize_dashboard_entity_type(raw: object) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    normalized = {
        "skills": "skill",
        "skill": "skill",
        "agents": "agent",
        "agent": "agent",
        "mcp": "mcp-server",
        "mcp-server": "mcp-server",
        "mcp-servers": "mcp-server",
        "harness": "harness",
        "harnesses": "harness",
    }.get(value, value)
    return normalized if normalized in _DASHBOARD_ENTITY_TYPES else None


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
    for sub, current_type, recursive in _DASHBOARD_ENTITY_SOURCES:
        if entity_type is not None and entity_type != current_type:
            continue
        p = (
            _wiki_dir() / "entities" / sub / _mcp_shard(slug) / f"{slug}.md"
            if recursive
            else _wiki_dir() / "entities" / sub / f"{slug}.md"
        )
        if p.exists():
            return p
    return None


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


_WIKI_INLINE_RE = re.compile(r"(`[^`\n]+`|\[\[[^\]\n]+\]\])")


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
    return f"/wiki/{quote(slug)}{suffix}", slug


def _render_wiki_inline(text: str) -> str:
    """Render a small safe inline Markdown subset used by wiki pages."""
    out: list[str] = []
    last = 0
    for match in _WIKI_INLINE_RE.finditer(text):
        out.append(html.escape(text[last:match.start()]))
        token = match.group(0)
        if token.startswith("`"):
            out.append(f"<code>{html.escape(token[1:-1])}</code>")
        else:
            inner = token[2:-2]
            target, _, label = inner.partition("|")
            href, fallback_label = _wiki_link_href(target)
            link_text = label.strip() or fallback_label
            out.append(
                f"<a href='{html.escape(href)}'>{html.escape(link_text)}</a>",
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


def _slugish(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


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
        rows.append(
            "<tr>"
            f"<td><a href='{html.escape(_entity_wiki_href(other_slug, other_type))}'>"
            f"{html.escape(str(other.get('label') or other_slug))}</a></td>"
            f"<td><span class='pill entity-type-{html.escape(other_type)}'>"
            f"{html.escape(other_type)}</span></td>"
            f"<td><code>{float(data.get('weight', 0.0)):.3f}</code></td>"
            f"<td>{shared_html}</td>"
            "</tr>"
        )
    table = (
        "<table><tr><th>Entity</th><th>Type</th><th>Weight</th><th>Shared signals</th></tr>"
        + ("".join(rows) if rows else "<tr><td colspan='4' class='muted'>No neighbors under the current limit.</td></tr>")
        + "</table>"
    )
    graph_type = entity_type if entity_type in _DASHBOARD_ENTITY_TYPES else _graph_type_from_node_id(center)
    return (
        "<div class='card'>"
        "<h2>Subgraph</h2>"
        f"<p class='muted'>{len(nodes)} nodes and {len(edges)} edges in the 1-hop neighborhood.</p>"
        f"<p><a href='/graph?slug={html.escape(slug)}&amp;type={html.escape(graph_type)}'>"
        "Open interactive graph view</a></p>"
        + table
        + "</div>"
    )


def _render_quality_drilldown(sidecar: dict | None) -> str:
    """Explain quality score signals for a wiki entity."""
    if sidecar is None:
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
    if entity_type is not None:
        for path in _sidecar_files():
            sidecar = _read_sidecar_file(path)
            if sidecar is None:
                continue
            if sidecar.get("slug") == slug and _sidecar_entity_type(sidecar) == entity_type:
                return sidecar
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


def _all_sidecars() -> list[dict]:
    out: list[dict] = []
    for p in _sidecar_files():
        sidecar = _read_sidecar_file(p)
        if sidecar is not None:
            out.append(sidecar)
    return out


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


_CSS = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1100px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
h1 { margin-top: 0; }
a { color: #2563eb; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { text-align: left; padding: 0.4rem 0.8rem; border-bottom: 1px solid #ddd;
         font-size: 0.92rem; }
th { background: rgba(0,0,0,0.04); font-weight: 600; }
tr:hover { background: rgba(0,0,0,0.02); }
.pill { display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
        font-size: 0.8rem; font-weight: 600; background: #e5e7eb; color: #111; }
.entity-type-skill { background: #e0e7ff; color: #312e81; }
.entity-type-agent { background: #fef3c7; color: #78350f; }
.entity-type-mcp-server { background: #fee2e2; color: #7f1d1d; }
.entity-type-harness { background: #dcfce7; color: #14532d; }
.grade-A { background: #d1fae5; color: #065f46; }
.grade-B { background: #dbeafe; color: #1e3a8a; }
.grade-C { background: #fef3c7; color: #78350f; }
.grade-D { background: #fed7aa; color: #7c2d12; }
.grade-F { background: #fee2e2; color: #7f1d1d; }
code, pre { background: rgba(0,0,0,0.06); padding: 0 0.3rem; border-radius: 3px;
            font-family: "SF Mono", Monaco, Consolas, monospace; font-size: 0.85rem; }
pre { padding: 0.6rem 0.8rem; overflow-x: auto; }
.muted { color: #6b7280; font-size: 0.85rem; }
.error { color: #991b1b; background: #fee2e2; border: 1px solid #fecaca;
         border-radius: 6px; padding: 0.5rem 0.65rem; }
.nav { display: flex; gap: 1rem; margin-bottom: 1.5rem; }
.card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem 1.25rem;
        margin-bottom: 1rem; }
.wiki-entity-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
                    gap: 1rem; align-items: start; }
.wiki-body { overflow-wrap: anywhere; }
.wiki-body h1, .wiki-body h2, .wiki-body h3 { margin-top: 0.9rem; }
.wiki-body h1:first-child, .wiki-body h2:first-child, .wiki-body h3:first-child { margin-top: 0; }
.wiki-body ul, .wiki-body ol { padding-left: 1.35rem; }
.wiki-body li { margin: 0.2rem 0; }
.wiki-body pre { white-space: pre-wrap; }
.frontmatter-table { table-layout: fixed; font-size: 0.85rem; }
.frontmatter-table td:last-child code { white-space: normal; overflow-wrap: anywhere; }
.entity-tabs { display: flex; gap: 0.4rem; margin: 0.8rem 0 1rem; flex-wrap: wrap; }
.entity-tab-button { border: 1px solid #d1d5db; border-radius: 6px; background: #fff;
                     color: inherit; cursor: pointer; padding: 0.35rem 0.7rem; }
.entity-tab-button.active { background: #111827; color: #fff; border-color: #111827; }
.entity-tab-panel[hidden] { display: none; }
.quality-signal-table { table-layout: fixed; }
.quality-signal-table td:last-child code { white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 860px) {
    .wiki-entity-grid { grid-template-columns: 1fr; }
}
@media (prefers-color-scheme: dark) {
    body { background: #0f172a; color: #e2e8f0; }
    th { background: rgba(255,255,255,0.05); }
    tr:hover { background: rgba(255,255,255,0.03); }
    .card { border-color: #334155; }
    .entity-tab-button { background: #0f172a; border-color: #334155; }
    .entity-tab-button.active { background: #e2e8f0; color: #0f172a; }
    code, pre { background: rgba(255,255,255,0.06); }
    .error { color: #fecaca; background: rgba(127,29,29,0.25);
             border-color: rgba(248,113,113,0.35); }
}
"""


def _layout(title: str, body: str) -> str:
    """Wrap body HTML in the standard page chrome."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} — ctx monitor</title>"
        f"<style>{_CSS}</style></head><body>"
        "<div class='nav'>"
        "<a href='/'>Home</a>"
        "<a href='/loaded'>Loaded</a>"
        "<a href='/skills'>Skills</a>"
        "<a href='/wiki'>Wiki</a>"
        "<a href='/graph'>Graph</a>"
        "<a href='/status'>Status</a>"
        "<a href='/kpi'>KPIs</a>"
        "<a href='/runtime'>Runtime</a>"
        "<a href='/sessions'>Sessions</a>"
        "<a href='/logs'>Logs</a>"
        "<a href='/events'>Live</a>"
        "</div>"
        + body
        + "</body></html>"
    )


# ─── Graph neighborhood (for /graph) ────────────────────────────────────────


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
    if not _is_safe_slug(slug):
        return {"nodes": [], "edges": [], "center": None}
    try:
        G = _load_dashboard_graph()
    except Exception:  # noqa: BLE001 — graph is advisory; blank on error
        return {"nodes": [], "edges": [], "center": None}
    if G.number_of_nodes() == 0:
        return {"nodes": [], "edges": [], "center": None}

    center = None
    normalized_entity_type = _normalize_dashboard_entity_type(entity_type)
    if entity_type is not None and normalized_entity_type is None:
        return {"nodes": [], "edges": [], "center": None}
    entity_types = (
        (normalized_entity_type,)
        if normalized_entity_type is not None
        else _DASHBOARD_ENTITY_TYPES
    )
    for current_type in entity_types:
        candidate = f"{current_type}:{slug}"
        if candidate in G:
            center = candidate
            break
    if center is None:
        return {"nodes": [], "edges": [], "center": None}

    nodes_out: dict[str, dict] = {}
    edges_out: list[dict] = []
    emitted_edges: set[tuple[str, str]] = set()
    frontier = [center]
    seen: set[str] = {center}

    def _add_node(nid: str, depth: int) -> None:
        if nid in nodes_out:
            return
        data = G.nodes.get(nid, {})
        label = data.get("label", nid.split(":", 1)[-1])
        tags = list(data.get("tags", []))
        default_type = (
            "mcp-server" if nid.startswith("mcp-server:")
            else "harness" if nid.startswith("harness:")
            else "agent" if nid.startswith("agent:")
            else "skill"
        )
        ntype = data.get("type") or default_type
        nodes_out[nid] = {
            "data": {
                "id": nid,
                "label": label,
                "type": ntype,
                "depth": depth,
                "tags": tags[:6],
                "filter_tokens": [nid, label, nid.split(":", 1)[-1], *tags],
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

    return {
        "nodes": list(nodes_out.values()),
        "edges": edges_out,
        "center": center,
    }


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
    try:
        G = _load_dashboard_graph()
    except Exception:  # noqa: BLE001
        return {"nodes": 0, "edges": 0, "available": False}
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "available": G.number_of_nodes() > 0,
    }


def _wiki_stats() -> dict:
    """Entity counts across all dashboard-supported entity types.

    MCPs are sharded by first-char under ``entities/mcp-servers/<shard>/``
    so we recurse rather than the flat glob used for skills + agents.
    Home page consumes ``total`` for the headline number and the
    individual counts for the dashboard entity-type detail
    line.
    """
    base = _wiki_dir() / "entities"
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
    }


def _render_home() -> str:
    sessions = _summarize_sessions()
    grades = _grade_distribution()
    recent = sessions[:10]
    gstats = _graph_stats()
    wstats = _wiki_stats()
    runtime = _runtime_lifecycle_summary()
    audit_lines = sum(1 for _ in _audit_log_path().open(encoding="utf-8")) \
        if _audit_log_path().exists() else 0
    manifest = _read_manifest()
    recent_audit = _read_jsonl(_audit_log_path(), limit=10)

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
        f"<div style='font-size:1.6rem; font-weight:600;'>{len(manifest.get('load', []))}</div>"
        f"<a href='/loaded'>manage →</a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Sidecars</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{sum(grades.values())}</div>"
        f"<a href='/skills'>browse →</a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Wiki entities</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{wstats['total']:,}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>"
        f"{wstats['skills']:,} skills · {wstats['agents']:,} agents · "
        f"{wstats['mcps']:,} MCPs · {wstats['harnesses']:,} harnesses</span></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Knowledge graph</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{gstats['nodes']}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>{gstats['edges']:,} edges</span>"
        f" · <a href='/graph'>explore →</a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Runtime checks</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{runtime['validations_total']}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>"
        f"{runtime['validation_failures']} failed / "
        f"{runtime['open_escalations_total']} open escalations</span>"
        f" / <a href='/runtime'>view -></a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Audit events</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{audit_lines}</div>"
        f"<a href='/logs'>view →</a> · <a href='/events'>live →</a></div>"
        + f"<div class='card'><div class='muted' style='font-size:0.8rem;'>Sessions</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{len(sessions)}</div>"
        f"<a href='/sessions'>browse →</a></div>"
        + "</div>"
        # ── Grade distribution ────────────────────────────────────────
        "<div class='card'><strong>Skill quality grades:</strong> "
        + "".join(
            f"<span class='pill grade-{g}'>{g}: {n}</span> "
            for g, n in grades.items()
        )
        + f"<span class='muted'> · total {sum(grades.values())}</span>"
        "</div>"
        # ── Two-column: recent sessions + recent audit ────────────────
        "<div style='display:grid; grid-template-columns:2fr 1fr; gap:1rem;'>"
        f"<div class='card'><strong>Recent sessions</strong> ({len(sessions)} total)"
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


def _render_skills() -> str:
    sidecars = _all_sidecars()
    sidecars.sort(key=lambda s: (s.get("grade", "F"), -s.get("raw_score", 0.0)))

    # Sidebar stats for the filter UI.
    grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    type_counts = {entity_type: 0 for entity_type in _DASHBOARD_ENTITY_TYPES}
    for sc in sidecars:
        grade_counts[sc.get("grade", "F")] = grade_counts.get(sc.get("grade", "F"), 0) + 1
        st = _sidecar_entity_type(sc)
        type_counts[st] = type_counts.get(st, 0) + 1

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
        f"score {s.get('raw_score', 0.0):.3f} · {html.escape(s.get('subject_type', 'skill'))}"
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

    grade_checkboxes = "".join(
        f"<label style='display:flex; justify-content:space-between; "
        f"padding:0.25rem 0;'>"
        f"<span><input type='checkbox' class='grade-filter' value='{g}' checked> "
        f"<span class='pill grade-{g}'>{g}</span></span>"
        f"<span class='muted' style='font-size:0.78rem;'>{grade_counts[g]}</span>"
        f"</label>"
        for g in ("A", "B", "C", "D", "F")
    )
    type_checkboxes = "".join(
        f"<label style='display:flex; justify-content:space-between; "
        f"padding:0.25rem 0;'>"
        f"<span><input type='checkbox' class='type-filter' value='{t}' checked> {t}</span>"
        f"<span class='muted' style='font-size:0.78rem;'>{type_counts.get(t, 0)}</span>"
        f"</label>"
        for t in _DASHBOARD_ENTITY_TYPES
    )

    body = (
        "<h1>Quality sidecars</h1>"
        f"<p class='muted'>{len(sidecars)} sidecars · click any card to drill in.</p>"
        "<div style='display:grid; grid-template-columns:220px 1fr; gap:1.25rem; align-items:start;'>"
        # ── Left filter sidebar ──────────────────────────────────────
        "<aside style='position:sticky; top:1rem;'>"
        "<div class='card'><strong>Search</strong>"
        "<input type='text' id='skill-search' placeholder='filter by slug…' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'></div>"
        "<div class='card'><strong>Grade</strong>"
        + grade_checkboxes
        + "</div>"
        "<div class='card'><strong>Type</strong>"
        + type_checkboxes
        + "</div>"
        "<div class='card'><strong>Hard floor</strong>"
        "<label style='display:block; padding:0.25rem 0;'>"
        "<input type='checkbox' id='hide-floor'> hide floored</label>"
        "</div>"
        "<div class='card'><span id='match-count' class='muted'>—</span></div>"
        "</aside>"
        # ── Card grid ────────────────────────────────────────────────
        "<div id='card-grid' style='display:grid; "
        "grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:0.7rem;'>"
        + cards
        + "</div>"
        "</div>"
        "<script>\n"
        "const cards = document.querySelectorAll('.skill-card');\n"
        "const search = document.getElementById('skill-search');\n"
        "const hideFloor = document.getElementById('hide-floor');\n"
        "function activeGrades() { return Array.from(document.querySelectorAll('.grade-filter:checked')).map(x => x.value); }\n"
        "function activeTypes() { return Array.from(document.querySelectorAll('.type-filter:checked')).map(x => x.value); }\n"
        "function apply() {\n"
        "  const q = search.value.trim().toLowerCase();\n"
        "  const grades = new Set(activeGrades());\n"
        "  const types = new Set(activeTypes());\n"
        "  const hideF = hideFloor.checked;\n"
        "  let shown = 0;\n"
        "  cards.forEach(c => {\n"
        "    const ok = grades.has(c.dataset.grade) && types.has(c.dataset.type)\n"
        "      && (!q || c.dataset.slug.toLowerCase().includes(q))\n"
        "      && (!hideF || !c.dataset.floor);\n"
        "    c.style.display = ok ? '' : 'none';\n"
        "    if (ok) shown++;\n"
        "  });\n"
        "  document.getElementById('match-count').textContent = shown + ' of ' + cards.length + ' match';\n"
        "}\n"
        "search.addEventListener('input', apply);\n"
        "hideFloor.addEventListener('change', apply);\n"
        "document.querySelectorAll('.grade-filter, .type-filter').forEach(el => el.addEventListener('change', apply));\n"
        "apply();\n"
        "</script>"
    )
    return _layout("Skills", body)


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
        return []
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


def _render_graph(focus: str | None = None, focus_type: str | None = None) -> str:
    """Interactive graph view backed by a dependency-free SVG renderer."""
    focus_slug = focus or ""
    focus_js = json.dumps(focus_slug)
    focus_type_js = json.dumps(focus_type or "")
    gstats = _graph_stats()
    seeds = (
        _top_degree_seeds(allow_load=False)
        if not focus_slug and gstats.get("available")
        else []
    )
    seed_html = ""
    if seeds:
        chips = "".join(
            f"<a href='/graph?slug={html.escape(s['slug'])}&amp;type={html.escape(s['type'])}' "
            f"style='display:inline-block; margin:0.2rem 0.25rem; padding:0.25rem 0.6rem; "
            f"border-radius:999px; background:{'#fef3c7' if s['type']=='agent' else '#fee2e2' if s['type']=='mcp-server' else '#dcfce7' if s['type']=='harness' else '#e0e7ff'}; "
            f"color:#111; font-size:0.82rem; text-decoration:none;'>"
            f"<code style='background:transparent;'>{html.escape(s['slug'])}</code> "
            f"<span class='muted' style='font-size:0.72rem;'>· deg {s['degree']}</span>"
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
        f"value='{html.escape(focus_slug)}' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<select id='focus-type' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<option value=''>auto type</option>"
        f"<option value='skill' {'selected' if focus_type == 'skill' else ''}>skill</option>"
        f"<option value='agent' {'selected' if focus_type == 'agent' else ''}>agent</option>"
        f"<option value='mcp-server' {'selected' if focus_type == 'mcp-server' else ''}>mcp-server</option>"
        f"<option value='harness' {'selected' if focus_type == 'harness' else ''}>harness</option>"
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
        "</aside>"
        # Right: graph list panel
        "<div id='cy' style='width:100%; height:75vh; border:1px solid #ddd; "
        "border-radius:6px; background:#fafafa;'></div>"
        "</div>"
        "<script>\n"
        f"const initial = {focus_js};\n"
        f"const initialType = {focus_type_js};\n"
        "const cyMount = document.getElementById('cy');\n"
        "function nodeColor(type) {\n"
        "  if (type === 'agent') return '#f59e0b';\n"
        "  if (type === 'mcp-server') return '#ef4444';\n"
        "  if (type === 'harness') return '#22c55e';\n"
        "  return '#6366f1';\n"
        "}\n"
        "function escapeHtml(s) { return String(s).replace(/[&<>\"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[ch])); }\n"
        "function nodeSlug(id) { return String(id || '').replace(/^(skill|agent|mcp-server|harness):/, ''); }\n"
        "function nodeDomId(id) { return 'graph-node-' + String(id || '').replace(/[^a-zA-Z0-9_-]/g, '_'); }\n"
        "function wikiHref(data) {\n"
        "  const nodeType = data.type || '';\n"
        "  const suffix = nodeType ? '?type=' + encodeURIComponent(nodeType) : '';\n"
        "  return '/wiki/' + encodeURIComponent(nodeSlug(data.id)) + suffix;\n"
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
        "      + '<code>' + escapeHtml(d.label || nodeSlug(d.id)) + '</code>'\n"
        "      + '<span class=\"pill entity-type-' + escapeHtml(typeKey) + '\">' + escapeHtml(d.type || 'entity') + '</span></a>';\n"
        "  }).join('');\n"
        "  cyMount.innerHTML = '<div data-testid=\"graph-fallback\" style=\"padding:0.75rem; height:100%; overflow:auto;\">'\n"
        "    + '<div class=\"muted\" style=\"margin-bottom:0.5rem;\">Showing list view.</div>'\n"
        "    + rows + '</div>';\n"
        "}\n"
        "function renderSvgGraph(g) {\n"
        "  const nodes = g.nodes || [];\n"
        "  const edges = g.edges || [];\n"
        "  if (!nodes.length) { renderFallback(g); return; }\n"
        "  const width = Math.max(640, Math.floor(cyMount.clientWidth || 800));\n"
        "  const height = Math.max(420, Math.floor(cyMount.clientHeight || 520));\n"
        "  const cx = width / 2;\n"
        "  const cy0 = height / 2;\n"
        "  const center = nodes.find(n => (n.data || {}).depth === 0) || nodes[0];\n"
        "  const centerId = (center.data || {}).id;\n"
        "  const maxDepth = Math.max(1, ...nodes.map(n => Number((n.data || {}).depth || 0)));\n"
        "  const byDepth = new Map();\n"
        "  nodes.forEach(n => {\n"
        "    const d = n.data || {};\n"
        "    if (d.id === centerId) return;\n"
        "    const depth = Number(d.depth || 1);\n"
        "    if (!byDepth.has(depth)) byDepth.set(depth, []);\n"
        "    byDepth.get(depth).push(n);\n"
        "  });\n"
        "  const pos = new Map([[centerId, {x: cx, y: cy0}]]);\n"
        "  Array.from(byDepth.entries()).forEach(([depth, group]) => {\n"
        "    const radius = Math.min(width, height) * (0.18 + 0.28 * (depth / maxDepth));\n"
        "    group.forEach((n, idx) => {\n"
        "      const angle = -Math.PI / 2 + (2 * Math.PI * idx / Math.max(1, group.length));\n"
        "      pos.set((n.data || {}).id, {x: cx + radius * Math.cos(angle), y: cy0 + radius * Math.sin(angle)});\n"
        "    });\n"
        "  });\n"
        "  const edgeHtml = edges.map(e => {\n"
        "    const d = e.data || {};\n"
        "    const s = pos.get(d.source);\n"
        "    const t = pos.get(d.target);\n"
        "    if (!s || !t) return '';\n"
        "    const w = Math.max(1, Math.min(4, 1 + Math.sqrt(Math.max(0, Number(d.weight || 1)))));\n"
        "    return '<line data-svg-edge-source=\"' + escapeHtml(d.source || '') + '\" '\n"
        "      + 'data-svg-edge-target=\"' + escapeHtml(d.target || '') + '\" '\n"
        "      + 'x1=\"' + s.x.toFixed(1) + '\" y1=\"' + s.y.toFixed(1) + '\" '\n"
        "      + 'x2=\"' + t.x.toFixed(1) + '\" y2=\"' + t.y.toFixed(1) + '\" '\n"
        "      + 'stroke=\"#94a3b8\" stroke-opacity=\"0.55\" stroke-width=\"' + w.toFixed(2) + '\" />';\n"
        "  }).join('');\n"
        "  const nodeHtml = nodes.map(n => {\n"
        "    const d = n.data || {};\n"
        "    const p = pos.get(d.id) || {x: cx, y: cy0};\n"
        "    const tags = Array.from(d.filter_tokens || d.tags || []).join(' ');\n"
        "    const label = d.label || nodeSlug(d.id);\n"
        "    const isCenter = d.id === centerId;\n"
        "    const r = isCenter ? 18 : 13;\n"
        "    return '<a href=\"' + escapeHtml(wikiHref(d)) + '\"><g data-testid=\"graph-svg-node\" '\n"
        "      + 'id=\"' + escapeHtml(nodeDomId(d.id)) + '\" data-svg-node-id=\"' + escapeHtml(d.id || '') + '\" '\n"
        "      + 'data-type=\"' + escapeHtml(d.type || '') + '\" data-depth=\"' + escapeHtml(d.depth || 0) + '\" '\n"
        "      + 'data-tags=\"' + escapeHtml(tags.toLowerCase()) + '\">'\n"
        "      + '<title>' + escapeHtml(label + ' (' + (d.type || 'entity') + ')') + '</title>'\n"
        "      + '<circle cx=\"' + p.x.toFixed(1) + '\" cy=\"' + p.y.toFixed(1) + '\" r=\"' + r + '\" '\n"
        "      + 'fill=\"' + nodeColor(d.type) + '\" stroke=\"#fff\" stroke-width=\"2\" />'\n"
        "      + '<text x=\"' + p.x.toFixed(1) + '\" y=\"' + (p.y + r + 14).toFixed(1) + '\" '\n"
        "      + 'text-anchor=\"middle\" font-size=\"11\" fill=\"#111827\">' + escapeHtml(label).slice(0, 28) + '</text>'\n"
        "      + '</g></a>';\n"
        "  }).join('');\n"
        "  renderFallback(g);\n"
        "  const list = cyMount.querySelector('[data-testid=\"graph-fallback\"]');\n"
        "  const heading = list ? list.querySelector('.muted') : null;\n"
        "  if (heading) heading.remove();\n"
        "  const rows = list ? list.innerHTML : '';\n"
        "  cyMount.innerHTML = '<div data-testid=\"graph-renderer\" style=\"height:100%; display:flex; flex-direction:column;\">'\n"
        "    + '<svg data-testid=\"graph-svg\" viewBox=\"0 0 ' + width + ' ' + height + '\" '\n"
        "    + 'style=\"display:block; width:100%; flex:1 1 auto; min-height:0; background:#fafafa;\">'\n"
        "    + '<rect width=\"100%\" height=\"100%\" fill=\"#fafafa\" />' + edgeHtml + nodeHtml + '</svg>'\n"
        "    + '<div data-testid=\"graph-list\" style=\"max-height:32%; overflow:auto; border-top:1px solid #e5e7eb;\">' + rows + '</div>'\n"
        "    + '</div>';\n"
        "}\n"
        "cyMount.innerHTML = '<div data-testid=\"graph-empty\" class=\"muted\" style=\"padding:0.75rem;\">Enter a slug to render the graph.</div>';\n"
        # ── Client-side filtering (type + tag substring) ─────────────
        "function applyFilters() {\n"
        "  const allowedTypes = new Set(\n"
        "    Array.from(document.querySelectorAll('.graph-type-filter'))\n"
        "      .filter(cb => cb.checked).map(cb => cb.value));\n"
        "  const tagQ = (document.getElementById('tag-filter').value || '')\n"
        "    .trim().toLowerCase();\n"
        "  const counts = {skill: 0, agent: 0, 'mcp-server': 0, harness: 0};\n"
        "  let visible = 0;\n"
        "  const hiddenIds = new Set();\n"
        "  document.querySelectorAll('[data-testid=\"graph-fallback-node\"]').forEach(n => {\n"
        "    const t = n.dataset.type;\n"
        "    const isFocus = n.dataset.depth === '0';\n"
        "    const tags = (n.dataset.tags || '').split(/\\s+/).map(x => x.toLowerCase());\n"
        "    const typeOk = isFocus || allowedTypes.has(t);\n"
        "    const tagOk = isFocus || !tagQ || tags.some(tag => tag.includes(tagQ));\n"
        "    const hidden = !(typeOk && tagOk);\n"
        "    n.style.display = hidden ? 'none' : 'flex';\n"
        "    if (hidden) hiddenIds.add(n.dataset.nodeId || '');\n"
        "    if (!hidden) {\n"
        "      visible++;\n"
        "      if (t in counts) counts[t]++;\n"
        "    }\n"
        "  });\n"
        "  document.querySelectorAll('[data-svg-node-id]').forEach(n => {\n"
        "    n.style.display = hiddenIds.has(n.dataset.svgNodeId || '') ? 'none' : '';\n"
        "  });\n"
        "  document.querySelectorAll('[data-svg-edge-source]').forEach(e => {\n"
        "    const hidden = hiddenIds.has(e.dataset.svgEdgeSource || '') || hiddenIds.has(e.dataset.svgEdgeTarget || '');\n"
        "    e.style.display = hidden ? 'none' : '';\n"
        "  });\n"
        "  document.getElementById('graph-count-skill').textContent = counts.skill;\n"
        "  document.getElementById('graph-count-agent').textContent = counts.agent;\n"
        "  document.getElementById('graph-count-mcp-server').textContent = counts['mcp-server'];\n"
        "  document.getElementById('graph-count-harness').textContent = counts.harness;\n"
        "  document.getElementById('graph-match-count').textContent = visible + ' visible';\n"
        "}\n"
        "async function load(slug, entityType = '') {\n"
        "  if (!slug) return;\n"
        "  document.getElementById('msg').textContent = 'loading…';\n"
        "  const suffix = entityType ? '?type=' + encodeURIComponent(entityType) : '';\n"
        "  const r = await fetch('/api/graph/' + encodeURIComponent(slug) + '.json' + suffix);\n"
        "  if (!r.ok) { document.getElementById('msg').textContent = 'not found'; return; }\n"
        "  const g = await r.json();\n"
        "  if (!g.center) { document.getElementById('msg').textContent = 'slug not in graph'; return; }\n"
        "  try { renderSvgGraph(g); } catch (err) { renderFallback(g); }\n"
        "  document.getElementById('msg').textContent = g.nodes.length + ' nodes · ' + g.edges.length + ' edges';\n"
        "  applyFilters();\n"
        "}\n"
        "function selectedFocusType() { return document.getElementById('focus-type').value || ''; }\n"
        "document.getElementById('go').addEventListener('click', () => load(document.getElementById('focus').value.trim(), selectedFocusType()));\n"
        "document.getElementById('focus').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') load(ev.target.value.trim(), selectedFocusType()); });\n"
        "document.querySelectorAll('.graph-type-filter').forEach(cb => cb.addEventListener('change', applyFilters));\n"
        "document.getElementById('tag-filter').addEventListener('input', applyFilters);\n"
        "if (initial) load(initial, initialType);\n"
        "</script>"
    )
    return _layout("Graph", body)


def _render_wiki_entity(slug: str, entity_type: str | None = None) -> str:
    """Render one wiki entity page (frontmatter + body)."""
    path = _wiki_entity_path(slug, entity_type=entity_type)
    if path is None:
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

    display_body = _strip_duplicate_wiki_heading(md_body, slug)
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
    quality_html = _render_quality_drilldown(sidecar)
    tab_script = """
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
    body = (
        f"<h1>{html.escape(slug)}</h1>"
        + quality_summary_html
        + "<div class='entity-tabs' role='tablist' aria-label='Entity sections'>"
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
        + tab_script
    )
    return _layout(slug, body)


def _wiki_index_entries(
    limit_per_type: int | None = _WIKI_INDEX_LIMIT_PER_TYPE,
) -> list[dict]:
    """List every wiki entity page under ~/.claude/skill-wiki/entities/.

    Returns ``{slug, type, tags, description}`` rows. The full Skills.sh
    corpus is too large to render as one HTML page, so the dashboard samples
    a bounded number of pages per entity type.
    """
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
                "type": entity_type,
                "tags": all_tags[:6],
                "search_tags": all_tags,
                "description": description,
            })
            seen_for_type += 1
    return out


def _render_wiki_index() -> str:
    """Card grid of every wiki entity — search + type filter + sidecar grades."""
    entries = _wiki_index_entries()
    wstats = _wiki_stats()
    total_available = int(wstats.get("total") or len(entries))
    # Join with grade pills where a sidecar exists.
    grade_by_key: dict[tuple[str, str], str] = {}
    for sc in _all_sidecars():
        slug = sc.get("slug")
        if slug:
            grade_by_key[(str(slug), _sidecar_entity_type(sc))] = sc.get("grade", "")

    type_counts = {
        "skill": int(wstats.get("skills") or 0),
        "agent": int(wstats.get("agents") or 0),
        "mcp-server": int(wstats.get("mcps") or 0),
        "harness": int(wstats.get("harnesses") or 0),
    }

    cards = "".join(
        "<a class='wiki-card' "
        f"data-slug='{html.escape(e['slug'])}' "
        f"data-type='{html.escape(e['type'])}' "
        f"data-tags='{html.escape(' '.join(e.get('search_tags', e['tags'])).lower())}' "
        f"href='/wiki/{html.escape(e['slug'])}?type={html.escape(e['type'])}' "
        "style='border:1px solid #e5e7eb; border-radius:6px; "
        "padding:0.6rem 0.8rem; text-decoration:none; color:inherit; "
        "display:flex; flex-direction:column; gap:0.25rem;'>"
        "<div style='display:flex; justify-content:space-between; align-items:center; gap:0.4rem;'>"
        f"<code style='font-size:0.84rem;'>{html.escape(e['slug'])}</code>"
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
        f"<span><input type='checkbox' class='wiki-type-filter' value='{t}' checked> {t}</span>"
        f"<span class='muted' style='font-size:0.78rem;'>{type_counts.get(t, 0):,}</span>"
        f"</label>"
        for t in _DASHBOARD_ENTITY_TYPES
    )

    body = (
        "<h1>Wiki</h1>"
        f"<p class='muted'>{len(entries):,} shown of {total_available:,} entity pages under "
        f"<code>~/.claude/skill-wiki/entities/</code> · "
        "search by slug / description / tag within the visible sample, "
        "or click a card to read the page.</p>"
        "<div style='display:grid; grid-template-columns:220px 1fr; gap:1.25rem; align-items:start;'>"
        # Left sidebar
        "<aside style='position:sticky; top:1rem;'>"
        "<div class='card'><strong>Search</strong>"
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
        "function wActiveTypes() { return Array.from(document.querySelectorAll('.wiki-type-filter:checked')).map(x => x.value); }\n"
        "function wApply() {\n"
        "  const q = wsearch.value.trim().toLowerCase();\n"
        "  const types = new Set(wActiveTypes());\n"
        "  let shown = 0;\n"
        "  wcards.forEach(c => {\n"
        "    const hay = (c.dataset.slug + ' ' + (c.textContent||'') + ' ' + c.dataset.tags).toLowerCase();\n"
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
    return _layout("Wiki", body)


def _kpi_summary():
    """Compute the KPI DashboardSummary using the default source layout.

    Returns ``None`` if the kpi_dashboard module can't be imported or
    the required directories don't exist — the caller renders an
    explanatory empty state instead of failing.
    """
    try:
        from kpi_dashboard import generate  # type: ignore
        from ctx_lifecycle import LifecycleSources  # type: ignore
    except Exception:  # noqa: BLE001 — KPIs are advisory
        return None
    sidecar_dir = _sidecar_dir()
    if not sidecar_dir.is_dir():
        return None
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
        return generate(sources=sources, top_n=25)
    except Exception:  # noqa: BLE001
        return None


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
        ("skills_sh_catalog", "skills-sh-catalog.json.gz"),
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
    """Install/load one entity from the wiki. Returns (ok, message)."""
    if not _is_safe_slug(slug):
        return False, f"invalid slug: {slug!r}"
    normalized_entity_type = _normalize_dashboard_entity_type(entity_type)
    if normalized_entity_type is None:
        return False, f"unsupported entity_type: {entity_type!r}"
    entity_type = normalized_entity_type
    if entity_type == "harness":
        return (
            False,
            "harness installs are managed by ctx-harness-install; "
            f"run: ctx-harness-install {slug} --dry-run",
        )
    result: Any
    try:
        if entity_type == "agent":
            from ctx.adapters.claude_code.install.agent_install import install_agent
            result = install_agent(
                slug,
                wiki_dir=_wiki_dir(),
                agents_dir=_claude_dir() / "agents",
            )
        elif entity_type == "mcp-server":
            from ctx.adapters.claude_code.install.mcp_install import install_mcp
            result = install_mcp(
                slug,
                wiki_dir=_wiki_dir(),
                command=command,
                json_config=json_config,
                auto=True,
            )
        else:
            from ctx.adapters.claude_code.install.skill_install import install_skill
            result = install_skill(
                slug,
                wiki_dir=_wiki_dir(),
                skills_dir=_claude_dir() / "skills",
            )
    except ImportError as exc:
        return False, f"install import failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if result.status not in ("installed", "skipped-existing"):
        return False, f"load failed: {result.message or result.status}"
    _log_dashboard_entity_event(entity_type, "loaded", slug)
    return True, result.message or f"loaded {entity_type}:{slug}"


def _perform_unload(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
    """Unload the given entity.

    Routes by ``entity_type``:
      - ``skill`` / ``agent``: ``skill_unload.unload_from_session`` —
        file-copy + manifest update, reversible via /api/load.
      - ``mcp-server``: ``mcp_install.uninstall_mcp`` — wraps
        ``claude mcp remove`` subprocess. Requires the claude CLI on
        PATH; errors surface to the caller.
    """
    if not _is_safe_slug(slug):
        return False, f"invalid slug: {slug!r}"
    normalized_entity_type = _normalize_dashboard_entity_type(entity_type)
    if normalized_entity_type is None:
        return False, f"unsupported entity_type: {entity_type!r}"
    entity_type = normalized_entity_type
    if entity_type == "harness":
        return (
            False,
            "harness installs are managed by ctx-harness-install; "
            f"run: ctx-harness-install {slug} --uninstall --dry-run",
        )
    if entity_type == "mcp-server":
        try:
            from ctx.adapters.claude_code.install.mcp_install import uninstall_mcp
        except ImportError as exc:
            return False, f"mcp_install import failed: {exc}"
        try:
            result = uninstall_mcp(slug, wiki_dir=_wiki_dir())
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if result.status not in ("uninstalled",):
            return False, f"uninstall failed: {result.message or result.status}"
        _log_dashboard_entity_event("mcp-server", "unloaded", slug)
        return True, f"unloaded mcp:{slug}"

    if entity_type == "agent":
        try:
            removed_entries = _remove_loaded_manifest_entry(slug, "agent")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if not removed_entries:
            return False, f"{slug} was not in the loaded set"
        _log_dashboard_entity_event("agent", "unloaded", slug)
        return True, f"unloaded {slug}"

    # Skills keep using the existing skill_unload module so skill-events.jsonl
    # remains compatible with older usage and retention analytics.
    try:
        from ctx.adapters.claude_code.install.skill_unload import unload_from_session
    except ImportError as exc:
        return False, f"skill_unload import failed: {exc}"
    try:
        removed = unload_from_session([slug], entity_type=entity_type)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if not removed:
        return False, f"{slug} was not in the loaded set"
    return True, f"unloaded {', '.join(removed)}"


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
        origin = self.headers.get("Origin") or ""
        if origin:
            host_header = self.headers.get("Host", "")
            expected = f"http://{host_header}"
            return origin == expected
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
        if self._mutations_enabled():
            return True
        token = self.headers.get("X-CTX-Monitor-Token") or qs.get("token", "")
        return bool(_MONITOR_TOKEN) and secrets.compare_digest(token, _MONITOR_TOKEN)

    def _send_security_headers(self, *, html_response: bool = False) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
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
            if path == "/":
                self._send_html(_render_home())
            elif path == "/sessions":
                self._send_html(_render_sessions_index())
            elif path.startswith("/session/"):
                self._send_html(_render_session_detail(path.split("/session/", 1)[1]))
            elif path == "/skills":
                self._send_html(_render_skills())
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
            elif path == "/status":
                self._send_html(_render_status())
            elif path == "/wiki":
                self._send_html(_render_wiki_index())
            elif path.startswith("/wiki/"):
                slug = path.split("/wiki/", 1)[1]
                self._send_html(_render_wiki_entity(slug, qs.get("type")))
            elif path == "/kpi":
                self._send_html(_render_kpi())
            elif path == "/runtime":
                self._send_html(_render_runtime_lifecycle())
            elif path == "/events":
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
            elif path == "/api/runtime.json":
                self._send_json(_runtime_lifecycle_summary())
            elif path.startswith("/api/skill/") and path.endswith(".json"):
                slug = path[len("/api/skill/"): -len(".json")]
                sidecar = _load_sidecar(slug, entity_type=qs.get("type"))
                if sidecar is None:
                    self._send_404(f"no sidecar for {slug}")
                else:
                    self._send_json(sidecar)
            elif path.startswith("/api/graph/") and path.endswith(".json"):
                slug = path[len("/api/graph/"): -len(".json")]
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
    _MONITOR_TOKEN = (
        secrets.token_urlsafe(32)
        if bool(getattr(server, "_ctx_mutations_enabled", False))
        else ""
    )
    url = f"http://{host}:{port}/"
    print(f"ctx-monitor serving at {url}  (Ctrl+C to stop)", flush=True)
    if not bool(getattr(server, "_ctx_mutations_enabled", False)):
        print(
            "ctx-monitor: non-loopback bind; load/unload mutations disabled",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("ctx-monitor: shutdown", flush=True)
    finally:
        server.server_close()


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
