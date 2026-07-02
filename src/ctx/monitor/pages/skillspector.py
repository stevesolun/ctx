"""SkillSpector and sidecar detail pages for ctx-monitor."""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Sequence
from typing import Any


LayoutFn = Callable[[str, str], str]


def render_skillspector(payload: dict[str, Any], *, layout: LayoutFn) -> str:
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
    rows = "".join(_record_row(row) for row in records)
    status_counts = summary.get("statuses", {})
    body = (
        "<h1>SkillSpector audit</h1>"
        "<p class='muted'>ctx-run static SkillSpector results for skill bodies. "
        "This is a local ctx audit, not NVIDIA endorsement or certification. "
        "<a href='/api/skillspector.json'>JSON</a></p>"
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
        + (rows if rows else "<tr><td colspan='7' class='muted'>No matching records.</td></tr>")
        + "</table>"
        "</section>"
        "</div>"
        "<script>\n"
        "document.querySelectorAll('form select').forEach(el => el.addEventListener('change', () => el.form.submit()));\n"
        "</script>"
    )
    return layout("SkillSpector", body)


def render_skill_detail(
    slug: str,
    *,
    sidecar: dict[str, Any] | None,
    audit: Sequence[dict[str, Any]],
    layout: LayoutFn,
) -> str:
    if sidecar is None:
        return layout(slug, f"<h1>{html.escape(slug)}</h1><p>No sidecar.</p>")

    audit_rows = "".join(
        f"<tr><td class='muted'>{html.escape(str(row.get('ts', '')))}</td>"
        f"<td><span class='pill'>{html.escape(str(row.get('event', '')))}</span></td>"
        f"<td class='muted'>{html.escape(str(row.get('actor', '')))}</td></tr>"
        for row in audit[-100:]
    )
    hard_floor = sidecar.get("hard_floor")
    hard_floor_html = f" &middot; floor {html.escape(str(hard_floor))}" if hard_floor else ""
    body = (
        f"<h1>{html.escape(slug)}</h1>"
        "<div class='card'>"
        f"<span class='pill grade-{html.escape(str(sidecar.get('grade', 'F')))}'>grade {html.escape(str(sidecar.get('grade', 'F')))}</span> "
        f"score <strong>{float(sidecar.get('raw_score', 0.0)):.3f}</strong> "
        f"<span class='muted'>&middot; type {html.escape(str(sidecar.get('subject_type', '')))}"
        f"{hard_floor_html}</span>"
        "</div>"
        "<h2>Sidecar</h2>"
        f"<pre>{html.escape(json.dumps(sidecar, indent=2)[:4000])}</pre>"
        f"<h2>Audit timeline ({len(audit)} entries)</h2>"
        "<table><tr><th>ts</th><th>event</th><th>actor</th></tr>" + audit_rows + "</table>"
    )
    return layout(slug, body)


def _select_options(
    options: Sequence[dict[str, Any]],
    selected: object,
    *,
    all_label: str,
) -> str:
    selected_text = str(selected or "")
    html_options = [
        f"<option value=''{' selected' if not selected_text else ''}>{html.escape(all_label)}</option>"
    ]
    for option in options:
        value = str(option.get("value") or "")
        count = int(option.get("count") or 0)
        label = f"{value} ({count})"
        is_selected = " selected" if value == selected_text else ""
        html_options.append(
            f"<option value='{html.escape(value)}'{is_selected}>{html.escape(label)}</option>"
        )
    return "".join(html_options)


def _record_row(row: dict[str, Any]) -> str:
    tags = ", ".join(str(tag) for tag in row.get("tags", [])[:6]) or "none"
    rules = ", ".join(str(rule) for rule in row.get("issue_rules", [])[:4]) or "none"
    score = row.get("risk_score")
    risk_score = "n/a" if score is None else str(score)
    return (
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
