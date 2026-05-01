# mypy: disable-error-code=attr-defined
"""ctx_monitor.py -- Local HTTP dashboard for ctx runtime and catalog activity.

``ctx-monitor serve [--port 8765]`` starts a zero-dependency threaded HTTP server
(stdlib http.server) that renders the audit log + skill-events.jsonl +
sidecars into a browser UI at http://localhost:8765/.

Routes:

    /                           Home — summary stats + session list + links
    /loaded                     Live manifest view + load/unload actions
    /sessions                   List of sessions (from audit + events jsonl)
    /session/<id>               Skills + agents seen in that session
    /skills                     Sidecar card grid with grade + score filters
    /skill/<slug>               Sidecar breakdown + timeline of audit events
    /wiki                       Wiki entity index — all pages with search
    /wiki/<slug>?type=<entity>  One wiki entity page (frontmatter + body)
    /graph                      Cytoscape graph explorer + popular seeds
    /graph?slug=<slug>&type=... Focus cytoscape on a specific entity
    /kpi                        Grade / lifecycle / category KPIs
    /logs                       Filterable tail of ctx-audit.jsonl
    /events                     Live SSE stream of new audit-log lines
    /api/sessions.json          JSON index for scripting
    /api/manifest.json          Raw ~/.claude/skill-manifest.json
    /api/skill/<slug>.json      Sidecar passthrough
    /api/graph/<slug>.json      Cytoscape-shaped neighborhood; accepts type
    /api/kpi.json               DashboardSummary passthrough

Design notes:

- No Flask / Starlette / FastAPI dependency. stdlib only — keeps
  ``pip install claude-ctx`` lean. Request handling is threaded so one
  open SSE client cannot monopolize the local dashboard.
- Reads append-only files; never mutates them.
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
import json
import os
import secrets
import sys
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body
from ctx.utils._safe_name import is_safe_source_name


_MONITOR_TOKEN = ""
_GRAPH_CACHE_KEY: tuple[Path, float, int, int] | None = None
_GRAPH_CACHE_VALUE: Any | None = None


# ─── Data sources ────────────────────────────────────────────────────────────


def _claude_dir() -> Path:
    return Path(os.path.expanduser("~/.claude"))


def _audit_log_path() -> Path:
    # Avoid importing ctx_audit_log here so the monitor can run even if
    # ctx_audit_log is absent for some reason.
    return _claude_dir() / "ctx-audit.jsonl"


def _events_jsonl_path() -> Path:
    return _claude_dir() / "skill-events.jsonl"


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


def _frontmatter_tags(value: Any, *, limit: int = 6) -> list[str]:
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
        if len(out) >= limit:
            break
    return out


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


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit is not None:
        out = out[-limit:]
    return out


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
        subject = line.get("skill") or line.get("agent") or ""
        if action == "load" and subject:
            row["skills_loaded"].add(subject)
        elif action == "unload" and subject:
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
.grade-A { background: #d1fae5; color: #065f46; }
.grade-B { background: #dbeafe; color: #1e3a8a; }
.grade-C { background: #fef3c7; color: #78350f; }
.grade-D { background: #fed7aa; color: #7c2d12; }
.grade-F { background: #fee2e2; color: #7f1d1d; }
code, pre { background: rgba(0,0,0,0.06); padding: 0 0.3rem; border-radius: 3px;
            font-family: "SF Mono", Monaco, Consolas, monospace; font-size: 0.85rem; }
pre { padding: 0.6rem 0.8rem; overflow-x: auto; }
.muted { color: #6b7280; font-size: 0.85rem; }
.nav { display: flex; gap: 1rem; margin-bottom: 1.5rem; }
.card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem 1.25rem;
        margin-bottom: 1rem; }
@media (prefers-color-scheme: dark) {
    body { background: #0f172a; color: #e2e8f0; }
    th { background: rgba(255,255,255,0.05); }
    tr:hover { background: rgba(255,255,255,0.03); }
    .card { border-color: #334155; }
    code, pre { background: rgba(255,255,255,0.06); }
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
        "<a href='/kpi'>KPIs</a>"
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
    """Return cytoscape-shaped {nodes, edges} for the N-hop neighborhood.

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
    entity_types = (
        (entity_type,)
        if entity_type in _DASHBOARD_ENTITY_TYPES
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
                "tags": data.get("tags", [])[:6],
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
                    edges_out.append({
                        "data": {
                            "id": f"{edge_key[0]}__{edge_key[1]}",
                            "source": nid,
                            "target": other,
                            "weight": edata.get("weight", 1),
                            "shared_tags": edata.get("shared_tags", [])[:4],
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
            f"<td>{s['lifecycle_transitions']}</td>"
            f"</tr>"
        )
    body = (
        "<h1>Sessions</h1>"
        f"<p class='muted'>{len(sessions)} unique sessions observed.</p>"
        "<table>"
        "<tr><th>Session</th><th>First seen</th><th>Last seen</th>"
        "<th>Skills↑</th><th>Skills↓</th><th>Agents↑</th><th>Lifecycle</th></tr>"
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
    audit = [r for r in _read_jsonl(_audit_log_path())
             if r.get("subject") == slug]
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


def _top_degree_seeds(limit: int = 18) -> list[dict]:
    """Pick high-degree nodes from the graph as seed suggestions.

    Used by ``/graph`` landing page so the first-time visitor has
    something to click. Falls back to empty on any graph-load failure.
    """
    try:
        G = _load_dashboard_graph()
    except Exception:  # noqa: BLE001
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
    """Interactive graph view — cytoscape-rendered N-hop neighborhood.

    Cytoscape.js is loaded from a CDN. This is a local-dev dashboard
    so the cost of one external asset is acceptable; stdlib-only
    remains the server invariant.
    """
    focus_slug = focus or ""
    focus_js = json.dumps(focus_slug)
    focus_type_js = json.dumps(focus_type or "")
    gstats = _graph_stats()
    seeds = _top_degree_seeds() if not focus_slug and gstats.get("available") else []
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
        # cytoscape canvas on the right. Client-side JS hides nodes by
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
        # Right: cytoscape canvas
        "<div id='cy' style='width:100%; height:75vh; border:1px solid #ddd; "
        "border-radius:6px; background:#fafafa;'></div>"
        "</div>"
        "<script src='https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js'></script>"
        "<script>\n"
        f"const initial = {focus_js};\n"
        f"const initialType = {focus_type_js};\n"
        "const cy = cytoscape({\n"
        "  container: document.getElementById('cy'),\n"
        "  style: [\n"
        "    { selector: 'node', style: {\n"
        "      'label': 'data(label)', 'font-size': '10px',\n"
        "      'text-valign': 'center', 'color': '#111',\n"
        "      'background-color': '#6366f1', 'width': 22, 'height': 22,\n"
        "    }},\n"
        "    { selector: 'node[type = \"agent\"]', style: {\n"
        "      'background-color': '#f59e0b',\n"  # amber for agents
        "    }},\n"
        "    { selector: 'node[type = \"mcp-server\"]', style: {\n"
        "      'background-color': '#ef4444',\n"  # red for MCPs so the
        # dashboard entity types are visually distinct at a glance in the graph.
        "      'shape': 'diamond', 'width': 24, 'height': 24,\n"
        "    }},\n"
        "    { selector: 'node[type = \"harness\"]', style: {\n"
        "      'background-color': '#22c55e',\n"
        "      'shape': 'hexagon', 'width': 26, 'height': 26,\n"
        "    }},\n"
        "    { selector: 'node[depth = 0]', style: {\n"
        "      'background-color': '#10b981', 'width': 34, 'height': 34,\n"
        "      'font-weight': 'bold',\n"
        "    }},\n"
        "    { selector: 'node.hidden-by-filter', style: {\n"
        "      'display': 'none',\n"
        "    }},\n"
        "    { selector: 'edge', style: {\n"
        "      'width': 'mapData(weight, 1, 10, 0.5, 4)',\n"
        "      'line-color': '#cbd5e1', 'curve-style': 'straight',\n"
        "    }},\n"
        "  ],\n"
        "  layout: { name: 'cose', animate: false, padding: 30 },\n"
        "});\n"
        "cy.on('tap', 'node', (e) => {\n"
        # Node IDs are prefixed by their entity type.
        "  const nodeType = e.target.data('type') || '';\n"
        "  const slug = e.target.id().replace(/^(skill|agent|mcp-server|harness):/, '');\n"
        "  const suffix = nodeType ? '?type=' + encodeURIComponent(nodeType) : '';\n"
        "  window.location.href = '/wiki/' + encodeURIComponent(slug) + suffix;\n"
        "});\n"
        # ── Client-side filtering (type + tag substring) ─────────────
        "function applyFilters() {\n"
        "  const allowedTypes = new Set(\n"
        "    Array.from(document.querySelectorAll('.graph-type-filter'))\n"
        "      .filter(cb => cb.checked).map(cb => cb.value));\n"
        "  const tagQ = (document.getElementById('tag-filter').value || '')\n"
        "    .trim().toLowerCase();\n"
        "  const counts = {skill: 0, agent: 0, 'mcp-server': 0, harness: 0};\n"
        "  let visible = 0;\n"
        "  cy.nodes().forEach(n => {\n"
        "    const t = n.data('type');\n"
        "    const isFocus = n.data('depth') === 0;\n"
        "    const tags = Array.from(n.data('tags') || []).map(x => String(x).toLowerCase());\n"
        "    const typeOk = isFocus || allowedTypes.has(t);\n"
        "    const tagOk = !tagQ || tags.some(tag => tag.includes(tagQ));\n"
        "    const hidden = !(typeOk && tagOk);\n"
        "    n.toggleClass('hidden-by-filter', hidden);\n"
        "    if (!hidden) {\n"
        "      visible++;\n"
        "      if (t in counts) counts[t]++;\n"
        "    }\n"
        "  });\n"
        "  cy.edges().forEach(e => {\n"
        "    const src = cy.getElementById(e.data('source'));\n"
        "    const tgt = cy.getElementById(e.data('target'));\n"
        "    const srcHidden = src.hasClass('hidden-by-filter');\n"
        "    const tgtHidden = tgt.hasClass('hidden-by-filter');\n"
        "    e.toggleClass('hidden-by-filter', srcHidden || tgtHidden);\n"
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
        "  cy.elements().remove();\n"
        "  cy.add([...g.nodes, ...g.edges]);\n"
        "  cy.layout({ name: 'cose', animate: false, padding: 30 }).run();\n"
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

    fm_rows = "".join(
        f"<tr><td class='muted'>{html.escape(k)}</td>"
        f"<td><code>{html.escape(_frontmatter_text(v)[:120])}</code></td></tr>"
        for k, v in sorted(meta.items())
    )

    sidecar_html = ""
    if sidecar is not None:
        sidecar_html = (
            "<div class='card'>"
            f"<strong>Quality</strong> <span class='pill grade-{html.escape(sidecar.get('grade', 'F'))}'>"
            f"{html.escape(sidecar.get('grade', 'F'))}</span> "
            f"score <strong>{sidecar.get('raw_score', 0.0):.3f}</strong>"
            f"{' · floor ' + html.escape(sidecar.get('hard_floor','')) if sidecar.get('hard_floor') else ''}"
            f"<div style='margin-top:0.4rem;'>"
            f"<a href='/skill/{html.escape(slug)}?type={html.escape(entity_type or '')}'>sidecar detail →</a> · "
            f"<a href='/graph?slug={html.escape(slug)}{type_suffix}'>graph neighborhood →</a>"
            "</div></div>"
        )

    body = (
        f"<h1>{html.escape(slug)}</h1>"
        + sidecar_html
        + "<div style='display:grid; grid-template-columns:1fr 280px; gap:1rem;'>"
        f"<div class='card'><pre style='white-space:pre-wrap; font-family:\"SF Mono\", Consolas, monospace; "
        f"font-size:0.88rem;'>{html.escape(md_body[:12000])}</pre></div>"
        f"<div class='card'><strong>Frontmatter</strong>"
        "<table style='font-size:0.85rem;'>"
        "<tr><th>Field</th><th>Value</th></tr>"
        + (fm_rows or "<tr><td class='muted' colspan='2'>none</td></tr>")
        + "</table></div>"
        "</div>"
    )
    return _layout(slug, body)


def _wiki_index_entries() -> list[dict]:
    """List every wiki entity page under ~/.claude/skill-wiki/entities/.

    Returns a slug-sorted list of ``{slug, type, tags, description, path}``.
    Reads only the YAML frontmatter (cheap) — never parses full bodies.
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
        paths = sorted(d.rglob("*.md") if recursive else d.glob("*.md"))
        for path in paths:
            slug = path.stem
            if not _is_safe_slug(slug):
                continue
            try:
                # Read only the first ~2 KB — enough for frontmatter.
                head = path.read_text(encoding="utf-8", errors="replace")[:2048]
            except OSError:
                continue
            meta, _ = _parse_frontmatter(head)
            tags_preview = _frontmatter_tags(meta.get("tags", ""))
            out.append({
                "slug": slug,
                "type": entity_type,
                "tags": tags_preview,
                "description": _frontmatter_text(meta.get("description", ""))[:200],
            })
    return out


def _render_wiki_index() -> str:
    """Card grid of every wiki entity — search + type filter + sidecar grades."""
    entries = _wiki_index_entries()
    # Join with grade pills where a sidecar exists.
    grade_by_key: dict[tuple[str, str], str] = {}
    for sc in _all_sidecars():
        slug = sc.get("slug")
        if slug:
            grade_by_key[(str(slug), _sidecar_entity_type(sc))] = sc.get("grade", "")

    type_counts = {entity_type: 0 for entity_type in _DASHBOARD_ENTITY_TYPES}
    for e in entries:
        type_counts[e["type"]] = type_counts.get(e["type"], 0) + 1

    cards = "".join(
        "<a class='wiki-card' "
        f"data-slug='{html.escape(e['slug'])}' "
        f"data-type='{html.escape(e['type'])}' "
        f"data-tags='{html.escape(' '.join(e['tags']).lower())}' "
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
        f"<p class='muted'>{len(entries)} entity pages under "
        f"<code>~/.claude/skill-wiki/entities/</code> · "
        "search by slug / description / tag, or click a card to read the page.</p>"
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
            "<code>ctx-skill-quality score --all</code> to populate "
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

    demotion_rows = "".join(
        "<tr>"
        f"<td><a href='/skill/{html.escape(c['slug'])}'><code>{html.escape(c['slug'])}</code></a></td>"
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
        f"<td><a href='/skill/{html.escape(a['slug'])}'><code>{html.escape(a['slug'])}</code></a></td>"
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


def _render_events() -> str:
    """SSE endpoint page. The server emits events at /api/events.stream."""
    return _layout(
        "Live events",
        "<h1>Live events</h1>"
        "<p class='muted'>Tails <code>~/.claude/ctx-audit.jsonl</code> "
        "via server-sent events.</p>"
        "<pre id='stream' style='min-height:20rem; max-height:70vh; "
        "overflow-y:scroll; font-size:0.78rem;'></pre>"
        "<script>\n"
        "const src = new EventSource('/api/events.stream');\n"
        "const pre = document.getElementById('stream');\n"
        "src.onmessage = (e) => { pre.textContent += e.data + '\\n'; "
        "pre.scrollTop = pre.scrollHeight; };\n"
        "src.onerror = () => { pre.textContent += '-- stream error; "
        "reconnecting --\\n'; };\n"
        "</script>",
    )


def _render_loaded() -> str:
    """Live view of ~/.claude/skill-manifest.json with load/unload actions.

    Groups manifest entries by ``entity_type`` (skill / agent / mcp-server / harness)
    with a per-section count. Unload button posts both the slug and
    entity_type so the server routes correctly — MCPs need
    ``claude mcp remove``, skills + agents take the file-copy path.
    Legacy entries without entity_type default to ``skill`` (what the
    pre-install_utils manifest implicitly assumed).
    """
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
            f"data-etype='{html.escape(etype)}'>unload</button></td>"
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
        f"data-etype='{html.escape(_etype(e))}'>load</button></td>"
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
        "<button type='submit' style='margin-left:0.5rem;'>load</button>"
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
        f"const CTX_MONITOR_TOKEN = {json.dumps(_MONITOR_TOKEN)};\n"
        "async function post(url, body) {\n"
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
        "  const r = await post('/api/load', {slug, entity_type});\n"
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


def _perform_load(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
    """Install/load one entity from the wiki. Returns (ok, message)."""
    if not _is_safe_slug(slug):
        return False, f"invalid slug: {slug!r}"
    if entity_type not in _DASHBOARD_ENTITY_TYPES:
        return False, f"unsupported entity_type: {entity_type!r}"
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
            result = install_mcp(slug, wiki_dir=_wiki_dir(), auto=True)
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
    try:
        if entity_type == "agent":
            from ctx_audit_log import log_agent_event
            log_agent_event("agent.loaded", slug, actor="user",
                            meta={"via": "ctx-monitor"})
        elif entity_type == "skill":
            from ctx_audit_log import log_skill_event
            log_skill_event("skill.loaded", slug, actor="user",
                            meta={"via": "ctx-monitor"})
    except Exception:  # noqa: BLE001
        pass
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
            result = uninstall_mcp(slug, wiki_dir=_wiki_dir(), force=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if result.status not in ("uninstalled",):
            return False, f"uninstall failed: {result.message or result.status}"
        return True, f"unloaded mcp:{slug}"

    # skill or agent — both flow through the same skill_unload module.
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

    def _mutation_authorized(self) -> bool:
        token = self.headers.get("X-CTX-Monitor-Token") or ""
        return bool(_MONITOR_TOKEN) and secrets.compare_digest(token, _MONITOR_TOKEN)

    def do_GET(self) -> None:  # noqa: N802 — stdlib signature
        # Parse once so we can reuse the query string for /graph?slug=…
        raw_path, _, raw_query = self.path.partition("?")
        path = raw_path
        qs = {}
        if raw_query:
            from urllib.parse import parse_qs
            qs = {k: v[0] for k, v in parse_qs(raw_query).items()}
        try:
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
                self._send_html(_render_loaded())
            elif path == "/logs":
                self._send_html(_render_logs())
            elif path == "/graph":
                self._send_html(_render_graph(qs.get("slug"), qs.get("type")))
            elif path == "/wiki":
                self._send_html(_render_wiki_index())
            elif path.startswith("/wiki/"):
                slug = path.split("/wiki/", 1)[1]
                self._send_html(_render_wiki_entity(slug, qs.get("type")))
            elif path == "/kpi":
                self._send_html(_render_kpi())
            elif path == "/events":
                self._send_html(_render_events())
            elif path == "/api/sessions.json":
                self._send_json(_summarize_sessions())
            elif path == "/api/manifest.json":
                self._send_json(_read_manifest())
            elif path == "/api/kpi.json":
                summary = _kpi_summary()
                self._send_json(summary.to_dict() if summary is not None else {
                    "total": 0, "detail": "no sidecars yet",
                })
            elif path.startswith("/api/skill/") and path.endswith(".json"):
                slug = path[len("/api/skill/"): -len(".json")]
                sidecar = _load_sidecar(slug, entity_type=qs.get("type"))
                if sidecar is None:
                    self._send_404(f"no sidecar for {slug}")
                else:
                    self._send_json(sidecar)
            elif path.startswith("/api/graph/") and path.endswith(".json"):
                slug = path[len("/api/graph/"): -len(".json")]
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
                    slug, hops=hops, limit=limit, entity_type=qs.get("type"),
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
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not self._same_origin():
                self._send_json_status(
                    403, {"detail": "cross-origin POST denied"},
                )
                return
            if not self._mutation_authorized():
                self._send_json_status(
                    403, {"detail": "monitor token required"},
                )
                return
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.lower() != "application/json":
                self._send_json_status(415, {"detail": "JSON body required"})
                return
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json_status(400, {"detail": "invalid JSON body"})
                return

            if path == "/api/load":
                slug = str(body.get("slug", "")).strip()
                etype = str(body.get("entity_type", "skill")).strip() or "skill"
                ok, msg = _perform_load(slug, entity_type=etype)
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
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, obj: Any) -> None:
        raw = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_404(self, detail: str) -> None:
        body = f"<h1>404</h1><p>{html.escape(detail)}</p>".encode()
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_500(self, exc: BaseException) -> None:
        self.log_error("render error: %s", exc)
        body = f"<h1>500</h1><pre>{html.escape(repr(exc))}</pre>".encode()
        self.send_response(500)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_audit_log(self) -> None:
        """Server-sent events: tail the audit log line-by-line."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
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
    return _MonitorServer((host, port), _MonitorHandler)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the monitor. Blocks until Ctrl+C."""
    global _MONITOR_TOKEN
    _MONITOR_TOKEN = secrets.token_urlsafe(32)
    server = _make_monitor_server(host, port)
    url = f"http://{host}:{port}/"
    print(f"ctx-monitor serving at {url}  (Ctrl+C to stop)", flush=True)
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
