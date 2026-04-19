"""ctx_monitor.py -- Local HTTP dashboard for ctx skill/agent activity.

``ctx-monitor serve [--port 8765]`` starts a zero-dependency HTTP server
(stdlib http.server) that renders the audit log + skill-events.jsonl +
sidecars into a browser UI at http://localhost:8765/.

Routes:

    /                           Home — summary stats + session list + links
    /sessions                   List of sessions (from audit + events jsonl)
    /session/<id>               Skills + agents seen in that session
    /skills                     Grade distribution + sortable table
    /skill/<slug>               Sidecar breakdown + timeline of audit events
    /events                     Live SSE stream of new audit-log lines
    /api/sessions.json          JSON index for scripting
    /api/skill/<slug>.json      Sidecar passthrough

Design notes:

- No Flask / Starlette / FastAPI dependency. stdlib only — keeps
  ``pip install claude-ctx`` lean. Server is single-threaded (good
  enough for a local dev dashboard; not meant to be exposed on the
  network).
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
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


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


def _wiki_entity_path(slug: str) -> Path | None:
    """Resolve a slug to its wiki entity page under skills/ or agents/.

    Wiki layout: ``entities/skills/<slug>.md`` or ``entities/agents/<slug>.md``.
    Returns the first match, or ``None`` if neither exists.
    """
    # Validate slug so a crafted request can't escape the wiki tree.
    if not _SAFE_SLUG_RE.match(slug):
        return None
    for sub in ("skills", "agents"):
        p = _wiki_dir() / "entities" / sub / f"{slug}.md"
        if p.exists():
            return p
    return None


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---\\n...\\n---\\n`` frontmatter from body. Minimal parser
    that treats each top-level ``key: value`` as a string — no nested
    YAML, because our wiki pages don't use it.
    """
    m = __import__("re").match(r"^---\n(.*?)\n---\s*\n?", text, flags=__import__("re").DOTALL)
    if m is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def _read_manifest() -> dict:
    """Return the current ~/.claude/skill-manifest.json or an empty shell."""
    path = _manifest_path()
    if not path.exists():
        return {"load": [], "unload": [], "warnings": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"load": [], "unload": [], "warnings": []}


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


def _load_sidecar(slug: str) -> dict | None:
    path = _sidecar_dir() / f"{slug}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _all_sidecars() -> list[dict]:
    d = _sidecar_dir()
    if not d.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(d.glob("*.json")):
        if p.name.startswith(".") or p.name.endswith(".lifecycle.json"):
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
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
        "<a href='/graph'>Graph</a>"
        "<a href='/sessions'>Sessions</a>"
        "<a href='/logs'>Logs</a>"
        "<a href='/events'>Live</a>"
        "</div>"
        + body
        + "</body></html>"
    )


# ─── Graph neighborhood (for /graph) ────────────────────────────────────────


def _graph_neighborhood(slug: str, hops: int = 1, limit: int = 40) -> dict:
    """Return cytoscape-shaped {nodes, edges} for the N-hop neighborhood.

    Uses ``resolve_graph.load_graph`` so the NetworkX 'links' vs 'edges'
    schema is handled centrally. Returns an empty shape if the graph
    hasn't been built or the slug isn't a node.
    """
    if not _SAFE_SLUG_RE.match(slug):
        return {"nodes": [], "edges": [], "center": None}
    try:
        from resolve_graph import load_graph as _lg  # type: ignore
        G = _lg()
    except Exception:  # noqa: BLE001 — graph is advisory; blank on error
        return {"nodes": [], "edges": [], "center": None}
    if G.number_of_nodes() == 0:
        return {"nodes": [], "edges": [], "center": None}

    center = None
    for prefix in ("skill:", "agent:"):
        candidate = f"{prefix}{slug}"
        if candidate in G:
            center = candidate
            break
    if center is None:
        return {"nodes": [], "edges": [], "center": None}

    nodes_out: dict[str, dict] = {}
    edges_out: list[dict] = []
    frontier = [center]
    seen: set[str] = {center}

    def _add_node(nid: str, depth: int) -> None:
        if nid in nodes_out:
            return
        data = G.nodes.get(nid, {})
        label = data.get("label", nid.split(":", 1)[-1])
        ntype = data.get("type") or ("agent" if nid.startswith("agent:") else "skill")
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
                edges_out.append({
                    "data": {
                        "id": f"{nid}__{other}",
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
    """Top-line graph stats for the home page. Cached per-request."""
    try:
        from resolve_graph import load_graph as _lg  # type: ignore
        G = _lg()
    except Exception:  # noqa: BLE001
        return {"nodes": 0, "edges": 0, "available": False}
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "available": G.number_of_nodes() > 0,
    }


def _wiki_stats() -> dict:
    base = _wiki_dir() / "entities"
    skills = len(list((base / "skills").glob("*.md"))) if (base / "skills").is_dir() else 0
    agents = len(list((base / "agents").glob("*.md"))) if (base / "agents").is_dir() else 0
    return {"skills": skills, "agents": agents, "total": skills + agents}


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
        f"<div style='font-size:1.6rem; font-weight:600;'>{wstats['total']}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>"
        f"{wstats['skills']} skills · {wstats['agents']} agents</span></div>"
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
        f"<div class='card'><strong>Latest audit events</strong>"
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
    type_counts = {"skill": 0, "agent": 0}
    for sc in sidecars:
        grade_counts[sc.get("grade", "F")] = grade_counts.get(sc.get("grade", "F"), 0) + 1
        st = sc.get("subject_type", "skill")
        type_counts[st] = type_counts.get(st, 0) + 1

    cards = "".join(
        f"<div class='skill-card' data-slug='{html.escape(s.get('slug', ''))}' "
        f"data-grade='{html.escape(s.get('grade', 'F'))}' "
        f"data-type='{html.escape(s.get('subject_type', 'skill'))}' "
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
        f"<a href='/skill/{html.escape(s.get('slug', ''))}' style='font-size:0.78rem;'>sidecar</a>"
        f"<a href='/wiki/{html.escape(s.get('slug', ''))}' style='font-size:0.78rem;'>wiki</a>"
        f"<a href='/graph?slug={html.escape(s.get('slug', ''))}' style='font-size:0.78rem;'>graph</a>"
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
        for t in ("skill", "agent")
    )

    body = (
        "<h1>Skills &amp; agents</h1>"
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


def _render_skill_detail(slug: str) -> str:
    sidecar = _load_sidecar(slug)
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
    body = (
        f"<h1>{html.escape(slug)}</h1>"
        f"<div class='card'>"
        f"<span class='pill grade-{html.escape(sidecar.get('grade', 'F'))}'>grade {html.escape(sidecar.get('grade', 'F'))}</span> "
        f"score <strong>{sidecar.get('raw_score', 0.0):.3f}</strong> "
        f"<span class='muted'>· type {html.escape(sidecar.get('subject_type', ''))}"
        f"{' · floor ' + html.escape(sidecar.get('hard_floor')) if sidecar.get('hard_floor') else ''}</span>"
        "</div>"
        "<h2>Sidecar</h2>"
        f"<pre>{html.escape(json.dumps(sidecar, indent=2)[:4000])}</pre>"
        f"<h2>Audit timeline ({len(audit)} entries)</h2>"
        "<table><tr><th>ts</th><th>event</th><th>actor</th></tr>"
        + audit_rows
        + "</table>"
    )
    return _layout(slug, body)


def _render_graph(focus: str | None = None) -> str:
    """Interactive graph view — cytoscape-rendered N-hop neighborhood.

    Cytoscape.js is loaded from a CDN. This is a local-dev dashboard
    so the cost of one external asset is acceptable; stdlib-only
    remains the server invariant.
    """
    focus_slug = focus or ""
    focus_js = json.dumps(focus_slug)
    body = (
        "<h1>Knowledge graph</h1>"
        "<p class='muted'>Enter a skill or agent slug to explore its "
        "1-hop neighborhood. Edge weight ≈ shared tag count.</p>"
        "<div class='card' style='padding:0.6rem 0.8rem;'>"
        "<input type='text' id='focus' placeholder='skill slug (e.g. python-patterns)' "
        "value='" + html.escape(focus_slug) + "' "
        "style='padding:0.35rem 0.6rem; width:22rem; border:1px solid #ccc; border-radius:4px;'>"
        "<button id='go' style='margin-left:0.5rem;'>explore</button>"
        "<label style='margin-left:1rem;'><input type='checkbox' id='agents-only'> agents only</label>"
        "<span id='msg' class='muted' style='margin-left:0.75rem;'></span>"
        "</div>"
        "<div id='cy' style='width:100%; height:65vh; border:1px solid #ddd; "
        "border-radius:6px; margin-top:1rem; background:#fafafa;'></div>"
        "<script src='https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js'></script>"
        "<script>\n"
        f"const initial = {focus_js};\n"
        "const cy = cytoscape({\n"
        "  container: document.getElementById('cy'),\n"
        "  style: [\n"
        "    { selector: 'node', style: {\n"
        "      'label': 'data(label)', 'font-size': '10px',\n"
        "      'text-valign': 'center', 'color': '#111',\n"
        "      'background-color': '#6366f1', 'width': 22, 'height': 22,\n"
        "    }},\n"
        "    { selector: 'node[type = \"agent\"]', style: {\n"
        "      'background-color': '#f59e0b',\n"
        "    }},\n"
        "    { selector: 'node[depth = 0]', style: {\n"
        "      'background-color': '#10b981', 'width': 34, 'height': 34,\n"
        "      'font-weight': 'bold',\n"
        "    }},\n"
        "    { selector: 'edge', style: {\n"
        "      'width': 'mapData(weight, 1, 10, 0.5, 4)',\n"
        "      'line-color': '#cbd5e1', 'curve-style': 'straight',\n"
        "    }},\n"
        "  ],\n"
        "  layout: { name: 'cose', animate: false, padding: 30 },\n"
        "});\n"
        "cy.on('tap', 'node', (e) => {\n"
        "  const slug = e.target.id().replace(/^(skill|agent):/, '');\n"
        "  window.location.href = '/wiki/' + encodeURIComponent(slug);\n"
        "});\n"
        "async function load(slug) {\n"
        "  if (!slug) return;\n"
        "  document.getElementById('msg').textContent = 'loading…';\n"
        "  const r = await fetch('/api/graph/' + encodeURIComponent(slug) + '.json');\n"
        "  if (!r.ok) { document.getElementById('msg').textContent = 'not found'; return; }\n"
        "  const g = await r.json();\n"
        "  if (!g.center) { document.getElementById('msg').textContent = 'slug not in graph'; return; }\n"
        "  let elements = [...g.nodes, ...g.edges];\n"
        "  if (document.getElementById('agents-only').checked) {\n"
        "    const keep = new Set(g.nodes.filter(n => n.data.type === 'agent' || n.data.depth === 0).map(n => n.data.id));\n"
        "    elements = [...g.nodes.filter(n => keep.has(n.data.id)),\n"
        "                ...g.edges.filter(e => keep.has(e.data.source) && keep.has(e.data.target))];\n"
        "  }\n"
        "  cy.elements().remove();\n"
        "  cy.add(elements);\n"
        "  cy.layout({ name: 'cose', animate: false, padding: 30 }).run();\n"
        "  document.getElementById('msg').textContent = g.nodes.length + ' nodes · ' + g.edges.length + ' edges';\n"
        "}\n"
        "document.getElementById('go').addEventListener('click', () => load(document.getElementById('focus').value.trim()));\n"
        "document.getElementById('focus').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') load(ev.target.value.trim()); });\n"
        "document.getElementById('agents-only').addEventListener('change', () => load(document.getElementById('focus').value.trim()));\n"
        "if (initial) load(initial);\n"
        "</script>"
    )
    return _layout("Graph", body)


def _render_wiki_entity(slug: str) -> str:
    """Render one wiki entity page (frontmatter + body)."""
    path = _wiki_entity_path(slug)
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
    sidecar = _load_sidecar(slug)

    fm_rows = "".join(
        f"<tr><td class='muted'>{html.escape(k)}</td>"
        f"<td><code>{html.escape(v[:120])}</code></td></tr>"
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
            f"<a href='/skill/{html.escape(slug)}'>sidecar detail →</a> · "
            f"<a href='/graph?slug={html.escape(slug)}'>graph neighborhood →</a>"
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
    """Live view of ~/.claude/skill-manifest.json with load/unload actions."""
    manifest = _read_manifest()
    load_rows = manifest.get("load", [])
    unload_rows = manifest.get("unload", [])

    loaded_html = "".join(
        f"<tr>"
        f"<td><a href='/skill/{html.escape(e.get('skill', ''))}'>"
        f"<code>{html.escape(e.get('skill', ''))}</code></a></td>"
        f"<td class='muted'>{html.escape(e.get('source', ''))}</td>"
        f"<td class='muted'>priority {e.get('priority', '—')}</td>"
        f"<td class='muted'>{html.escape(str(e.get('reason', ''))[:70])}</td>"
        f"<td><button class='btn-unload' data-slug='{html.escape(e.get('skill', ''))}'>unload</button></td>"
        f"</tr>"
        for e in load_rows
    )
    unload_html = "".join(
        f"<tr>"
        f"<td><code>{html.escape(e.get('skill', ''))}</code></td>"
        f"<td class='muted'>{html.escape(str(e.get('source', '') or e.get('reason', ''))[:80])}</td>"
        f"<td><button class='btn-load' data-slug='{html.escape(e.get('skill', ''))}'>load</button></td>"
        f"</tr>"
        for e in unload_rows
    )

    body = (
        "<h1>Loaded skills &amp; agents</h1>"
        f"<div class='card'>"
        f"<strong>{len(load_rows)}</strong> currently loaded · "
        f"<strong>{len(unload_rows)}</strong> known-unloaded · "
        f"<span class='muted'>source: <code>~/.claude/skill-manifest.json</code></span>"
        "</div>"
        "<h2>Load a new skill</h2>"
        "<div class='card'>"
        "<form id='load-form'>"
        "<input type='text' id='load-input' placeholder='skill slug (e.g. fastapi-pro)' "
        "style='padding:0.35rem 0.6rem; width:18rem; border:1px solid #ccc; "
        "border-radius:4px;'>"
        "<button type='submit' style='margin-left:0.5rem;'>load</button>"
        "<span id='load-msg' class='muted' style='margin-left:0.75rem;'></span>"
        "</form></div>"
        f"<h2>Currently loaded ({len(load_rows)})</h2>"
        "<table><tr><th>Skill</th><th>Source</th><th>Priority</th>"
        "<th>Reason</th><th></th></tr>" + loaded_html + "</table>"
        f"<h2>Recently unloaded ({len(unload_rows)})</h2>"
        "<table><tr><th>Skill</th><th>Source / reason</th><th></th></tr>"
        + unload_html + "</table>"
        "<script>\n"
        "async function post(url, body) {\n"
        "  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})});\n"
        "  const ok = r.status >= 200 && r.status < 300;\n"
        "  let msg = ''; try { msg = (await r.json()).detail || r.statusText; } catch(_) { msg = r.statusText; }\n"
        "  return {ok, msg};\n"
        "}\n"
        "document.querySelectorAll('.btn-unload').forEach(b => b.addEventListener('click', async () => {\n"
        "  b.disabled = true; const slug = b.dataset.slug;\n"
        "  const r = await post('/api/unload', {slug});\n"
        "  if (r.ok) location.reload(); else { b.disabled = false; alert('unload failed: ' + r.msg); }\n"
        "}));\n"
        "document.querySelectorAll('.btn-load').forEach(b => b.addEventListener('click', async () => {\n"
        "  b.disabled = true; const slug = b.dataset.slug;\n"
        "  const r = await post('/api/load', {slug});\n"
        "  if (r.ok) location.reload(); else { b.disabled = false; alert('load failed: ' + r.msg); }\n"
        "}));\n"
        "document.getElementById('load-form').addEventListener('submit', async (ev) => {\n"
        "  ev.preventDefault();\n"
        "  const slug = document.getElementById('load-input').value.trim();\n"
        "  if (!slug) return;\n"
        "  document.getElementById('load-msg').textContent = 'loading…';\n"
        "  const r = await post('/api/load', {slug});\n"
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


_SAFE_SLUG_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")


def _perform_load(slug: str) -> tuple[bool, str]:
    """Invoke skill_loader.load_skill(slug). Returns (ok, message)."""
    if not _SAFE_SLUG_RE.match(slug):
        return False, f"invalid slug: {slug!r}"
    try:
        from skill_loader import load_skill  # local import — heavy module
    except ImportError as exc:
        return False, f"skill_loader import failed: {exc}"
    try:
        load_skill(slug)
    except Exception as exc:  # noqa: BLE001 — surface the error to the caller
        return False, f"{type(exc).__name__}: {exc}"
    # Audit entry so the dashboard timeline reflects the dashboard-driven load.
    try:
        from ctx_audit_log import log_skill_event
        log_skill_event("skill.loaded", slug, actor="user",
                        meta={"via": "ctx-monitor"})
    except Exception:  # noqa: BLE001 — audit best-effort
        pass
    return True, "loaded"


def _perform_unload(slug: str) -> tuple[bool, str]:
    """Invoke skill_unload.unload_from_session([slug]). Returns (ok, message)."""
    if not _SAFE_SLUG_RE.match(slug):
        return False, f"invalid slug: {slug!r}"
    try:
        from skill_unload import unload_from_session
    except ImportError as exc:
        return False, f"skill_unload import failed: {exc}"
    try:
        removed = unload_from_session([slug])
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if not removed:
        return False, f"{slug} was not in the loaded set"
    return True, f"unloaded {', '.join(removed)}"


# ─── HTTP handler ────────────────────────────────────────────────────────────


class _MonitorHandler(BaseHTTPRequestHandler):
    # Silence the per-request access log spam. Users running
    # ctx-monitor get a clean stdout; errors still surface via
    # log_error() below.
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    # CSRF defense. Dashboard mutation endpoints (/api/load, /api/unload)
    # accept JSON POST only from the same origin we're serving from, so a
    # hostile webpage open in the same browser can't trigger load/unload
    # via a forged fetch(). Serve+bind to 127.0.0.1 by default keeps
    # network-side exposure off the table too.
    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin") or ""
        if origin:
            host_header = self.headers.get("Host", "")
            expected = f"http://{host_header}"
            return origin == expected
        # No Origin header (curl, direct tool calls) — accept, since the
        # server is localhost-bound by default.
        return True

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
                self._send_html(_render_skill_detail(path.split("/skill/", 1)[1]))
            elif path == "/loaded":
                self._send_html(_render_loaded())
            elif path == "/logs":
                self._send_html(_render_logs())
            elif path == "/graph":
                self._send_html(_render_graph(qs.get("slug")))
            elif path.startswith("/wiki/"):
                slug = path.split("/wiki/", 1)[1]
                self._send_html(_render_wiki_entity(slug))
            elif path == "/events":
                self._send_html(_render_events())
            elif path == "/api/sessions.json":
                self._send_json(_summarize_sessions())
            elif path == "/api/manifest.json":
                self._send_json(_read_manifest())
            elif path.startswith("/api/skill/") and path.endswith(".json"):
                slug = path[len("/api/skill/"): -len(".json")]
                sidecar = _load_sidecar(slug)
                if sidecar is None:
                    self._send_404(f"no sidecar for {slug}")
                else:
                    self._send_json(sidecar)
            elif path.startswith("/api/graph/") and path.endswith(".json"):
                slug = path[len("/api/graph/"): -len(".json")]
                hops = max(1, min(int(qs.get("hops", 1)), 3))
                limit = max(5, min(int(qs.get("limit", 40)), 150))
                self._send_json(_graph_neighborhood(slug, hops=hops, limit=limit))
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
            if not self._same_origin():
                self._send_json_status(
                    403, {"detail": "cross-origin POST denied"},
                )
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json_status(400, {"detail": "invalid JSON body"})
                return

            if path == "/api/load":
                slug = str(body.get("slug", "")).strip()
                ok, msg = _perform_load(slug)
                self._send_json_status(
                    200 if ok else 400, {"ok": ok, "detail": msg},
                )
            elif path == "/api/unload":
                slug = str(body.get("slug", "")).strip()
                ok, msg = _perform_unload(slug)
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
            while True:
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


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the monitor. Blocks until Ctrl+C."""
    server = HTTPServer((host, port), _MonitorHandler)
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
