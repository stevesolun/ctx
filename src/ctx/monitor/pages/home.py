"""Home page renderer for ctx-monitor."""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

LayoutFn = Callable[[str, str], str]
FormatCountFn = Callable[[int], str]


def render_home(
    *,
    manifest: dict[str, Any],
    sessions: list[dict[str, Any]],
    wiki_stats: dict[str, Any],
    graph_stats: dict[str, Any],
    runtime_summary: dict[str, Any],
    audit_lines: int,
    recent_audit: list[dict[str, Any]],
    layout: LayoutFn,
    format_count: FormatCountFn,
) -> str:
    """Render the monitor landing page from precomputed dashboard state."""
    recent = sessions[:10]
    if wiki_stats.get("split_known", True):
        wiki_detail = (
            f"{wiki_stats['skills']:,} skills &middot; {wiki_stats['agents']:,} agents &middot; "
            f"{wiki_stats['mcps']:,} MCPs &middot; {wiki_stats['harnesses']:,} harnesses"
        )
    else:
        wiki_detail = "entity split unavailable; install the current graph index"

    rows = []
    for session in recent:
        sid = session["session_id"]
        rows.append(
            "<tr>"
            f"<td><a href='/session/{html.escape(sid)}'>{html.escape(sid[:20])}</a></td>"
            f"<td class='muted'>{html.escape(session['last_seen'] or '-')}</td>"
            f"<td>{len(session['skills_loaded'])}</td>"
            f"<td>{len(session['skills_unloaded'])}</td>"
            f"<td>{len(session['agents_loaded'])}</td>"
            f"<td>{session['score_updates']}</td>"
            "</tr>"
        )

    audit_rows = "".join(
        f"<tr><td class='muted'>{html.escape((row.get('ts') or '')[-8:])}</td>"
        f"<td><span class='pill'>{html.escape(row.get('event', ''))}</span></td>"
        f"<td><a href='/wiki/{html.escape(row.get('subject', ''))}'><code>{html.escape(row.get('subject', ''))}</code></a></td>"
        "</tr>"
        for row in reversed(recent_audit)
    )

    body = (
        "<h1>ctx monitor</h1>"
        "<div style='display:grid; grid-template-columns:repeat(auto-fit, minmax(180px,1fr));"
        " gap:0.8rem; margin-bottom:1.25rem;'>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Currently loaded</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{format_count(len(manifest.get('load', [])))}</div>"
        "<a href='/loaded'>manage &rarr;</a></div>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Sidecars</div>"
        "<div id='home-sidecar-count' style='font-size:1.6rem; font-weight:600;'>...</div>"
        "<a href='/skills'>browse &rarr;</a></div>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Wiki entities</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{format_count(wiki_stats['total'])}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>{html.escape(wiki_detail)}</span></div>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Knowledge graph</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{format_count(graph_stats['nodes'])}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>{format_count(graph_stats['edges'])} edges</span>"
        " &middot; <a href='/graph'>explore &rarr;</a></div>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Runtime checks</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{format_count(runtime_summary['validations_total'])}</div>"
        f"<span class='muted' style='font-size:0.75rem;'>{format_count(runtime_summary['validation_failures'])} failed / "
        f"{format_count(runtime_summary['open_escalations_total'])} open escalations</span>"
        " / <a href='/runtime'>view -></a></div>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Audit events</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{format_count(audit_lines)}</div>"
        "<a href='/logs'>view &rarr;</a> &middot; <a href='/events'>live &rarr;</a></div>"
        + "<div class='card'><div class='muted' style='font-size:0.8rem;'>Sessions</div>"
        f"<div style='font-size:1.6rem; font-weight:600;'>{format_count(len(sessions))}</div>"
        "<a href='/sessions'>browse &rarr;</a></div>" + "</div>"
        "<div class='card'><strong>Skill quality grades:</strong> "
        + "".join(
            f"<span class='pill grade-{grade}' data-home-grade='{grade}'>{grade}: ...</span> "
            for grade in ("A", "B", "C", "D", "F")
        )
        + "<span id='home-grade-total' class='muted'> &middot; total loading</span>"
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
        'const el = document.querySelector(`[data-home-grade="${g}"]`);'
        "if (el) el.textContent = `${g}: ${fmt(grades[g] || 0)}`;"
        "});"
        "if (countEl) countEl.textContent = fmt(data.total || 0);"
        "if (totalEl) totalEl.textContent = ` - total ${fmt(data.total || 0)}`;"
        "})"
        ".catch(() => {"
        "if (countEl) countEl.textContent = 'open';"
        "if (totalEl) totalEl.textContent = ' - open Skills for counts';"
        "});"
        "})();"
        "</script>"
        "<div style='display:grid; grid-template-columns:2fr 1fr; gap:1rem;'>"
        f"<div class='card'><strong>Recent sessions</strong> ({format_count(len(sessions))} total)"
        + (
            "<table><tr><th>Session</th><th>Last seen</th><th>Load</th>"
            "<th>Unload</th><th>Agents</th><th>Scores</th></tr>" + "".join(rows) + "</table>"
            if recent
            else (
                "<p class='muted'>No sessions recorded yet. Hooks start logging "
                "once you run a Claude Code session with ctx installed.</p>"
            )
        )
        + "</div>"
        "<div class='card'><strong>Latest audit events</strong>"
        + (
            "<table><tr><th>Time</th><th>Event</th><th>Subject</th></tr>" + audit_rows + "</table>"
            if recent_audit
            else "<p class='muted'>No audit events yet.</p>"
        )
        + "</div>"
        "</div>"
    )
    return layout("Home", body)
