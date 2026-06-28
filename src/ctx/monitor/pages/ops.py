"""Operational dashboard pages for ctx-monitor."""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

LayoutFn = Callable[[str, str], str]
NormalizeEntityTypeFn = Callable[[str], str | None]


def render_kpi(
    summary: Any | None,
    *,
    layout: LayoutFn,
    normalize_entity_type: NormalizeEntityTypeFn,
) -> str:
    """Render the KPI dashboard from an already computed summary."""
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
        return layout("KPIs", empty)

    total = summary.total
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
            summary.hard_floor_counts.items(),
            key=lambda kv: (-kv[1], kv[0]),
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
        normalized = normalize_entity_type(entity_type)
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
    ) or (
        "<tr><td colspan='8' class='muted'>"
        "No active D/F grade entries — corpus is healthy."
        "</td></tr>"
    )

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
    subject_blurb = " &middot; ".join(
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
        f"<span class='muted'>&middot; {subject_blurb}</span>"
        f"<div style='margin-top:0.5rem;'>{grade_pills}</div>"
        "<div style='margin-top:0.4rem;'>"
        "<a href='/api/kpi.json'>JSON</a> &middot; "
        "<a href='/skills'>skill cards &rarr;</a></div>"
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
        "<span class='muted'>(active or watch &middot; grade D/F &middot; "
        "sorted by D-streak desc, score asc)</span>"
        "<table><tr><th>Slug</th><th>Type</th><th>Category</th><th>Grade</th>"
        "<th>Score</th><th>State</th><th>D-streak</th><th>Hard floor</th></tr>"
        + demotion_rows + "</table></div>"
        "<div class='card'><strong>Archived</strong>"
        "<table><tr><th>Slug</th><th>Type</th><th>Category</th>"
        "<th>Last grade</th><th>Computed at</th></tr>"
        + archived_rows + "</table></div>"
    )
    return layout("KPIs", body)


def render_status(
    status: dict[str, Any],
    *,
    layout: LayoutFn,
    queue_status_names: tuple[str, ...],
) -> str:
    """Render queue and graph/wiki artifact state for operator checks."""
    queue = status["queue"]
    telemetry = status.get("telemetry", {})
    artifacts = status["artifacts"]
    counts = queue.get("counts", {})
    count_pills = " ".join(
        f"<span class='pill'>{html.escape(name)}: {int(counts.get(name, 0))}</span>"
        for name in queue_status_names
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
        ("graph_packs", "graph packs"),
        ("graph_delta_json", "graph-delta.json"),
        ("communities_json", "communities.json"),
        ("graph_store", "graph-store.sqlite3"),
        ("wiki_packs", "wiki packs"),
        ("pack_compaction", "pack compaction"),
        ("wiki_graph_tar", "wiki-graph.tar.gz"),
        ("skills_sh_catalog", "skill-index.json.gz"),
    )
    artifact_rows = "".join(
        "<tr>"
        f"<td><code>{label}</code></td>"
        f"<td>{'yes' if artifacts[key].get('exists') else 'no'}</td>"
        f"<td>{int(artifacts[key].get('size') or 0):,}</td>"
        f"<td class='muted'>{_artifact_detail(artifacts[key])}</td>"
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
    telemetry_html = _telemetry_card(telemetry if isinstance(telemetry, dict) else {})
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
        f"{telemetry_html}"
        "<div class='card'><strong>Recent queue jobs</strong>"
        "<table><tr><th>ID</th><th>Kind</th><th>Status</th><th>Attempts</th>"
        "<th>Source</th><th>Worker</th><th>Last error</th></tr>"
        + job_rows
        + "</table></div>"
        "<div class='card'><strong>Artifact versions</strong>"
        "<table><tr><th>Artifact</th><th>Exists</th><th>Bytes</th><th>Details</th><th>Path</th></tr>"
        + artifact_rows
        + "</table></div>"
        f"<div class='card'><strong>Artifact promotions ({artifacts.get('promotion_count', 0)})</strong>"
        "<table><tr><th>Status</th><th>Time</th><th>Hash</th><th>Target</th></tr>"
        + promotion_rows
        + "</table></div>"
    )
    return layout("Status", body)


def _telemetry_card(status: dict[str, Any]) -> str:
    spool = _dict_or_empty(status.get("spool"))
    export_status = _dict_or_empty(status.get("export_status"))
    checkpoint = _dict_or_empty(status.get("checkpoint"))
    latest = _dict_or_empty(spool.get("latest_event"))
    latest_text = "none"
    if latest:
        latest_text = (
            f"{html.escape(str(latest.get('event_name') or ''))} "
            f"<span class='muted'>{html.escape(str(latest.get('outcome') or ''))} "
            f"{html.escape(str(latest.get('ts') or ''))}</span>"
        )
    export_label = str(export_status.get("status") or "unknown")
    export_error = export_status.get("error_kind") or export_status.get("error")
    export_error_html = (
        f"<p class='error'>Export error: {html.escape(str(export_error))}</p>"
        if export_error
        else ""
    )
    return (
        "<div class='card'><strong>Telemetry health</strong>"
        "<table><tr><th>Area</th><th>Status</th><th>Details</th></tr>"
        "<tr><td>capture</td>"
        f"<td><span class='pill'>{'enabled' if status.get('enabled') else 'disabled'}</span></td>"
        f"<td class='muted'>mode {html.escape(str(status.get('mode') or ''))}; "
        f"events: {int(spool.get('event_count') or 0):,}; "
        f"malformed: {int(spool.get('malformed_records') or 0):,}; "
        f"path {html.escape(str(spool.get('path') or ''))}</td></tr>"
        "<tr><td>latest</td>"
        f"<td>{latest_text}</td>"
        f"<td class='muted'>sources: {html.escape(_compact_counts(spool.get('sources')))}</td></tr>"
        "<tr><td>export</td>"
        f"<td><span class='pill'>{html.escape(export_label)}</span></td>"
        f"<td class='muted'>sink {html.escape(str(status.get('export_sink') or ''))}; "
        f"enabled: {'yes' if status.get('export_enabled') else 'no'}; "
        f"attempted/exported/failed: {int(export_status.get('attempted') or 0):,}/"
        f"{int(export_status.get('exported') or 0):,}/"
        f"{int(export_status.get('failed') or 0):,}; "
        f"checkpoint: {'yes' if checkpoint.get('exists') else 'no'}</td></tr>"
        "</table>"
        f"{export_error_html}"
        "</div>"
    )


def _compact_counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return "none"
    return ", ".join(f"{key}: {value[key]}" for key in sorted(value))


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _artifact_detail(status: dict[str, Any]) -> str:
    if "needs_compaction" in status:
        need = "needed" if status.get("needs_compaction") else "not needed"
        readiness = "ready" if status.get("can_compact_now") else "not ready"
        detail = (
            f"compaction: {need}, "
            f"{int(status.get('max_overlay_count') or 0)} overlays / "
            f"threshold {int(status.get('overlay_threshold') or 0)}, "
            f"{readiness}"
        )
    elif "pack_count" in status:
        detail = (
            f"packs: {int(status.get('pack_count') or 0)} "
            f"(base {int(status.get('base_count') or 0)}, "
            f"overlay {int(status.get('overlay_count') or 0)})"
        )
    elif isinstance(status.get("counts"), dict):
        counts = status["counts"]
        detail = (
            "published graph: "
            f"{int(counts.get('nodes') or 0):,} nodes, "
            f"{int(counts.get('edges') or 0):,} edges"
        )
    elif {"fresh", "nodes", "edges"} <= set(status):
        freshness = "fresh" if status.get("fresh") else "stale or missing"
        detail = (
            f"local store: {freshness}, "
            f"{int(status.get('nodes') or 0)} nodes, "
            f"{int(status.get('edges') or 0)} edges"
        )
    else:
        return ""
    error = status.get("error")
    if error:
        detail += f" - {error}"
    errors = status.get("errors")
    if isinstance(errors, list) and errors:
        detail += f" - {'; '.join(str(item) for item in errors[:3])}"
    return html.escape(detail)
