"""Activity and runtime page renderers for ctx-monitor."""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

LayoutFn = Callable[[str, str], str]
JsonlReader = Callable[[Path, int | None], list[dict[str, Any]]]


def render_sessions_index(
    *,
    layout: LayoutFn,
    summarize_sessions: Callable[[], list[dict[str, Any]]],
) -> str:
    """Render the session index page."""
    sessions = summarize_sessions()
    rows = []
    for session in sessions:
        sid = session["session_id"]
        rows.append(
            f"<tr>"
            f"<td><a href='/session/{html.escape(sid)}'><code>{html.escape(sid[:32])}</code></a></td>"
            f"<td class='muted'>{html.escape(session['first_seen'] or '-')}</td>"
            f"<td class='muted'>{html.escape(session['last_seen'] or '-')}</td>"
            f"<td>{len(session['skills_loaded'])}</td>"
            f"<td>{len(session['skills_unloaded'])}</td>"
            f"<td>{len(session['agents_loaded'])}</td>"
            f"<td>{len(session['agents_unloaded'])}</td>"
            f"<td>{len(session['mcps_loaded'])}</td>"
            f"<td>{len(session['mcps_unloaded'])}</td>"
            f"<td>{session['lifecycle_transitions']}</td>"
            f"</tr>"
        )
    body = (
        "<h1>Sessions</h1>"
        f"<p class='muted'>{len(sessions)} unique sessions observed.</p>"
        "<table>"
        "<tr><th>Session</th><th>First seen</th><th>Last seen</th>"
        "<th>Skills↑</th><th>Skills↓</th>"
        "<th>Agents↑</th><th>Agents↓</th>"
        "<th>MCPs↑</th><th>MCPs↓</th><th>Lifecycle</th></tr>" + "".join(rows) + "</table>"
    )
    return layout("Sessions", body)


def render_session_detail(
    session_id: str,
    *,
    layout: LayoutFn,
    session_detail: Callable[[str], dict[str, Any]],
) -> str:
    """Render one session timeline."""
    detail = session_detail(session_id)
    audit = detail["audit_entries"]
    events = detail["load_events"]

    audit_rows = "".join(
        f"<tr><td class='muted'>{html.escape(row.get('ts', ''))}</td>"
        f"<td><span class='pill'>{html.escape(row.get('event', ''))}</span></td>"
        f"<td><code>{html.escape(row.get('subject', ''))}</code></td>"
        f"<td class='muted'>{html.escape(json.dumps(row.get('meta', {}))[:80])}</td></tr>"
        for row in audit
    )
    event_rows = "".join(
        f"<tr><td class='muted'>{html.escape(row.get('timestamp', ''))}</td>"
        f"<td>{html.escape(row.get('event', ''))}</td>"
        f"<td><code>{html.escape(row.get('skill') or row.get('agent') or '')}</code></td></tr>"
        for row in events
    )

    body = (
        f"<h1>Session {html.escape(session_id)}</h1>"
        f"<div class='card'><strong>{len(audit)}</strong> audit entries &middot; "
        f"<strong>{len(events)}</strong> load/unload events</div>"
        "<h2>Audit timeline</h2>"
        "<table><tr><th>ts</th><th>event</th><th>subject</th><th>meta</th></tr>"
        + audit_rows
        + "</table>"
        "<h2>Load/unload events</h2>"
        "<table><tr><th>ts</th><th>event</th><th>subject</th></tr>" + event_rows + "</table>"
    )
    return layout(f"Session {session_id}", body)


def render_events(
    *,
    layout: LayoutFn,
    read_jsonl: JsonlReader,
    audit_log_path: Callable[[], Path],
) -> str:
    """Render the SSE live-events page."""
    entries = read_jsonl(audit_log_path(), 200)
    event_lines = [json.dumps(entry, ensure_ascii=False, default=str) for entry in entries]
    initial_stream = "\n".join(event_lines)
    if not initial_stream:
        initial_stream = "-- no audit events recorded yet; waiting for new events --"
    return layout(
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


def render_runtime_lifecycle(
    *,
    layout: LayoutFn,
    runtime_lifecycle_summary: Callable[[], dict[str, Any]],
) -> str:
    """Render runtime validation and escalation status."""
    summary = runtime_lifecycle_summary()

    def event_cell(event: dict[str, Any], key: str, limit: int = 120) -> str:
        return html.escape(str(event.get(key) or ""))[:limit]

    validation_rows = "".join(
        "<tr>"
        f"<td class='muted'>{event_cell(event, 'created_at')}</td>"
        f"<td><code>{event_cell(event, 'check_name')}</code></td>"
        f"<td><span class='pill'>{event_cell(event, 'status')}</span></td>"
        f"<td class='muted'>{event_cell(event, 'session_id')}</td>"
        f"<td class='muted'>{event_cell(event, 'summary')}</td>"
        "</tr>"
        for event in reversed(summary["recent_validations"])
    )
    escalation_rows = "".join(
        "<tr>"
        f"<td class='muted'>{event_cell(event, 'created_at')}</td>"
        f"<td><code>{event_cell(event, 'trigger')}</code></td>"
        f"<td><span class='pill'>{event_cell(event, 'severity')}</span></td>"
        f"<td class='muted'>{event_cell(event, 'session_id')}</td>"
        f"<td class='muted'>{event_cell(event, 'reason')}</td>"
        "</tr>"
        for event in reversed(summary["open_escalations"])
    )
    selection = summary.get("tool_selection") or {}
    sources = selection.get("selection_sources") or {}
    source_rows = "".join(
        f"<tr><td>{html.escape(str(source))}</td><td>{html.escape(str(count))}</td></tr>"
        for source, count in sorted(sources.items())
    )
    token_usage = summary.get("token_usage") or {}
    by_attribution = token_usage.get("by_attribution") or {}
    attribution_rows = "".join(
        f"<tr><td>{html.escape(str(attribution))}</td><td>{html.escape(str(count))}</td></tr>"
        for attribution, count in sorted(by_attribution.items())
    )

    def usage_value(value: Any) -> str:
        return "unavailable" if value is None else html.escape(str(value))

    def usage_cell(row: dict[str, Any], key: str) -> str:
        usage = row.get("token_usage")
        if not isinstance(usage, dict):
            return "unavailable"
        return usage_value(usage.get(key))

    token_total_rows = "".join(
        f"<tr><td>{label}</td><td>{usage_value(token_usage.get(key))}</td></tr>"
        for label, key in (
            ("Input", "input_tokens"),
            ("Cache read", "cached_input_tokens"),
            ("Cache write", "cache_write_input_tokens"),
            ("Uncached input", "uncached_input_tokens"),
            ("Output", "output_tokens"),
        )
    )
    total_tokens = usage_value(token_usage.get("total_tokens"))
    cost_value = token_usage.get("cost_usd")
    cost = "unavailable" if cost_value is None else f"${float(cost_value):.4f}"

    recent_usage_rows = "".join(
        "<tr>"
        f"<td class='muted'>{event_cell(row, 'created_at')}</td>"
        f"<td class='muted'>{event_cell(row, 'session_id')}</td>"
        f"<td>{event_cell(row, 'entity_type')}</td>"
        f"<td><code>{event_cell(row, 'slug')}</code></td>"
        f"<td>{usage_cell(row, 'total_tokens')}</td>"
        f"<td><span class='pill'>{usage_cell(row, 'attribution') or 'unavailable'}</span></td>"
        "</tr>"
        for row in reversed(summary.get("recent_tool_usage") or [])
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
        "<div class='card table-scroll'><strong>Tool selection</strong>"
        f"<p><strong>{selection.get('loaded_total', 0)}</strong> loaded / "
        f"<strong>{selection.get('active_loaded_total', 0)}</strong> active / "
        f"<strong>{selection.get('selected_total', 0)}</strong> selected / "
        f"<strong>{selection.get('used_total', 0)}</strong> used</p>"
        + (
            "<table><tr><th>Source</th><th>Loads</th></tr>" + source_rows + "</table>"
            if source_rows
            else "<p class='muted'>No tool selections recorded yet.</p>"
        )
        + "</div>"
        "<div class='card table-scroll'><strong>Token usage</strong>"
        f"<p><strong>{token_usage.get('records', 0)}</strong> records / "
        f"<strong>{total_tokens}</strong> tokens / "
        f"<strong>{cost}</strong> cost</p>"
        "<table><tr><th>Token class</th><th>Total</th></tr>"
        + token_total_rows
        + "</table>"
        + (
            "<table><tr><th>Attribution</th><th>Records</th></tr>" + attribution_rows + "</table>"
            if attribution_rows
            else "<p class='muted'>No token usage records yet.</p>"
        )
        + "</div>"
        "<div class='card table-scroll'><strong>Recent tool usage</strong>"
        + (
            "<table><tr><th>Created</th><th>Session</th><th>Type</th><th>Slug</th>"
            "<th>Tokens</th><th>Attribution</th></tr>" + recent_usage_rows + "</table>"
            if recent_usage_rows
            else "<p class='muted'>No tool usage recorded yet.</p>"
        )
        + "</div>"
        "<div class='card table-scroll'><strong>Recent validations</strong>"
        + (
            "<table><tr><th>Created</th><th>Check</th><th>Status</th>"
            "<th>Session</th><th>Summary</th></tr>" + validation_rows + "</table>"
            if validation_rows
            else "<p class='muted'>No validation checks recorded yet.</p>"
        )
        + "</div>"
        "<div class='card table-scroll'><strong>Open escalations</strong>"
        + (
            "<table><tr><th>Created</th><th>Trigger</th><th>Severity</th>"
            "<th>Session</th><th>Reason</th></tr>" + escalation_rows + "</table>"
            if escalation_rows
            else "<p class='muted'>No open escalations.</p>"
        )
        + "</div>"
    )
    return layout("Runtime lifecycle", body)


def render_logs(
    *,
    layout: LayoutFn,
    read_jsonl: JsonlReader,
    audit_log_path: Callable[[], Path],
) -> str:
    """Render the filterable audit-log viewer."""
    entries = read_jsonl(audit_log_path(), 500)
    rows = "".join(
        f"<tr data-event='{html.escape(entry.get('event', ''))}' "
        f"data-subject='{html.escape(entry.get('subject', ''))}' "
        f"data-session='{html.escape(entry.get('session_id', '') or '')}'>"
        f"<td class='muted'>{html.escape(entry.get('ts', ''))}</td>"
        f"<td><span class='pill'>{html.escape(entry.get('event', ''))}</span></td>"
        f"<td><code>{html.escape(entry.get('subject', ''))}</code></td>"
        f"<td class='muted'>{html.escape(entry.get('actor', ''))}</td>"
        f"<td class='muted'>{html.escape((entry.get('session_id') or '')[:24])}</td>"
        f"<td class='muted'>{html.escape(json.dumps(entry.get('meta', {}))[:100])}</td>"
        f"</tr>"
        for entry in reversed(entries)
    )
    body = (
        "<h1>Audit log</h1>"
        f"<div class='card'>Showing last {len(entries)} of "
        f"<code>~/.claude/ctx-audit.jsonl</code>. "
        "<a href='/events'>Live stream &rarr;</a>"
        "</div>"
        "<div class='card'>"
        "<input type='text' id='filter' placeholder='filter: event/subject/session...' "
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
    return layout("Audit log", body)
