"""Wiki/entity rendering helpers for ctx-monitor."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote


WikiLinkFn = Callable[[str], tuple[str, str]]
TruncateFn = Callable[[str, int], tuple[str, bool]]
MetricRowFn = Callable[[str, Any], str]

_WIKI_INLINE_RE = re.compile(
    r"(`[^`\n]+`|\[\[[^\]\n]+\]\]|(?<!!)\[[^\]\n]+\]\([^\s()\n]+(?:\s+\"[^\"]*\")?\))",
)


def markdown_link_href(target: str) -> str | None:
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


def render_wiki_inline(text: str, *, wiki_link_href: WikiLinkFn) -> str:
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
            href, fallback_label = wiki_link_href(target)
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
                safe_href = markdown_link_href(target)
                if safe_href is None:
                    out.append(html.escape(label))
                else:
                    out.append(
                        f"<a href='{html.escape(safe_href)}'>{html.escape(label)}</a>",
                    )
        last = match.end()
    out.append(html.escape(text[last:]))
    return "".join(out)


def render_wiki_markdown(markdown_text: str, *, wiki_link_href: WikiLinkFn) -> str:
    """Render a conservative Markdown subset without adding dependencies."""
    lines = markdown_text.splitlines()
    out: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    code_lines: list[str] = []
    in_code = False

    def render_inline(value: str) -> str:
        return render_wiki_inline(value, wiki_link_href=wiki_link_href)

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{render_inline(' '.join(paragraph))}</p>")
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
            out.append(f"<h{level}>{render_inline(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if bullet:
            flush_paragraph()
            list_items.append(render_inline(bullet.group(1).strip()))
            continue
        flush_list()
        paragraph.append(stripped)

    flush_code()
    flush_paragraph()
    flush_list()
    return "".join(out) if out else "<p class='muted'>No body.</p>"


def entity_tab_script() -> str:
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


def render_entity_tabs(
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
        + entity_tab_script()
    )


def render_quality_drilldown(
    sidecar: dict[str, Any] | None,
    embedded_quality_markdown: str | None = None,
    *,
    wiki_link_href: WikiLinkFn,
    truncate_text: TruncateFn,
) -> str:
    """Explain quality score signals for a wiki entity."""
    if sidecar is None:
        if embedded_quality_markdown:
            quality_markdown = embedded_quality_markdown.strip()
            if not re.search(r"^#{1,6}\s+Quality\b", quality_markdown, re.IGNORECASE | re.MULTILINE):
                quality_markdown = "## Quality\n\n" + quality_markdown
            return (
                "<div class='card wiki-body'>"
                + render_wiki_markdown(quality_markdown, wiki_link_href=wiki_link_href)
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
        evidence_preview, evidence_truncated = truncate_text(evidence_text, 420)
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


def subgraph_quality_cell(sidecar: dict[str, Any] | None) -> str:
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
        return f"{label} ({entity_type}) - no sidecar"
    grade = str(sidecar.get("grade", "F"))
    score = float(sidecar.get("raw_score", sidecar.get("score", 0.0)) or 0.0)
    floor = str(sidecar.get("hard_floor") or "").strip()
    floor_text = f" - floor {floor}" if floor else ""
    return f"{label} ({entity_type}) - grade {grade} - score {score:.3f}{floor_text}"


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


def render_entity_subgraph_svg(
    *,
    node_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    center: str,
    sidecar_by_id: dict[str, dict[str, Any] | None],
    graph_type_from_node_id: Callable[[str, str], str],
    graph_slug_from_node_id: Callable[[str], str],
    display_label: Callable[..., str],
    display_slug: Callable[[str], str],
    entity_wiki_href: Callable[[str, str | None], str],
    json_for_script: Callable[[Any], str],
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
        node_type = graph_type_from_node_id(
            node_id, str(node.get("type") or "skill"),
        )
        node_slug = graph_slug_from_node_id(node_id)
        label = display_label(node.get("label"), fallback_slug=node_slug)
        sidecar = sidecar_by_id.get(node_id)
        node_payload.append({
            "id": node_id,
            "slug": node_slug,
            "label": label,
            "type": node_type,
            "href": entity_wiki_href(node_slug, node_type),
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
                f"{display_slug(graph_slug_from_node_id(source))} -> "
                f"{display_slug(graph_slug_from_node_id(target))} - weight {weight:.3f} "
                f"- shared {shared}"
            ),
        })

    nodes_json = json_for_script(node_payload)
    edges_json = json_for_script(edge_payload)

    return (
        "<div data-testid='entity-subgraph-graph' "
        "style='border:1px solid #e5e7eb; border-radius:8px; "
        "background:#f8fafc; margin:1rem 0; overflow:hidden;'>"
        "<div style='display:flex; align-items:center; gap:0.5rem; "
        "padding:0.45rem 0.6rem; border-bottom:1px solid #e5e7eb; background:#fff;'>"
        "<button id='entity-subgraph-zoom-in' type='button'>zoom in</button>"
        "<button id='entity-subgraph-zoom-out' type='button'>zoom out</button>"
        "<span class='muted'>drag to rotate - wheel to zoom - hover nodes or edges</span>"
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


def render_wiki_index_page(
    *,
    entries: list[dict[str, Any]],
    selected_type: str | None,
    initial_query: str,
    total_available: int,
    type_counts: dict[str, int],
    grade_by_key: dict[tuple[str, str], str],
    dashboard_entity_types: tuple[str, ...],
    layout: Callable[[str, str], str],
) -> str:
    """Render the searchable wiki catalog page from already-loaded entries."""
    suggestions = "".join(
        f"<option value='{html.escape(str(e['slug']))}' "
        f"label='{html.escape(str(e.get('display_slug') or e['slug']))}'>"
        for e in entries[:1000]
    )

    cards = "".join(
        "<a class='wiki-card' "
        f"data-slug='{html.escape(str(e['slug']))}' "
        f"data-display-slug='{html.escape(str(e.get('display_slug') or e['slug']))}' "
        f"data-type='{html.escape(str(e['type']))}' "
        f"data-tags='{html.escape(' '.join(e.get('search_tags', e['tags'])).lower())}' "
        f"href='/wiki/{html.escape(str(e['slug']))}?type={html.escape(str(e['type']))}' "
        "style='border:1px solid #e5e7eb; border-radius:6px; "
        "padding:0.6rem 0.8rem; text-decoration:none; color:inherit; "
        "display:flex; flex-direction:column; gap:0.25rem;'>"
        "<div style='display:flex; justify-content:space-between; align-items:center; gap:0.4rem;'>"
        f"<code style='font-size:0.84rem;'>{html.escape(str(e.get('display_slug') or e['slug']))}</code>"
        + (
            f"<span class='pill grade-{html.escape(grade_by_key[(str(e['slug']), str(e['type']))])}'>"
            f"{html.escape(grade_by_key[(str(e['slug']), str(e['type']))])}</span>"
            if grade_by_key.get((str(e["slug"]), str(e["type"])))
            else f"<span class='pill'>{html.escape(str(e['type']))}</span>"
        )
        + "</div>"
        f"<div class='muted' style='font-size:0.78rem; line-height:1.3;'>"
        f"{html.escape(str(e['description'] or '(no description)'))}"
        "</div>"
        + (
            f"<div class='muted' style='font-size:0.72rem;'>"
            f"{' - '.join(html.escape(str(t)) for t in e['tags'][:5])}</div>"
            if e["tags"]
            else ""
        )
        + "</a>"
        for e in entries
    )

    type_checkboxes = "".join(
        f"<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        f"<span><input type='checkbox' class='wiki-type-filter' value='{entity_type}' "
        f"{'checked' if selected_type is None or selected_type == entity_type else ''}> {entity_type}</span>"
        f"<span class='muted' style='font-size:0.78rem;'>{type_counts.get(entity_type, 0):,}</span>"
        f"</label>"
        for entity_type in dashboard_entity_types
    )
    badge_links = "".join(
        f"<a class='pill entity-type-{html.escape(entity_type)}' href='/wiki?type={quote(entity_type)}'>"
        f"{html.escape(entity_type)}</a>"
        for entity_type in dashboard_entity_types
    )

    body = (
        "<h1>Wiki</h1>"
        f"<p class='muted'>{len(entries):,} shown of {total_available:,} entity pages under "
        f"<code>~/.claude/skill-wiki/entities/</code> - "
        "search by slug / description / tag, pick a suggestion, "
        "or click a tile to read the full page.</p>"
        "<div class='card' style='display:flex; gap:0.45rem; flex-wrap:wrap; align-items:center;'>"
        f"<strong>Catalog shortcuts</strong>{badge_links}</div>"
        "<div style='display:grid; grid-template-columns:220px 1fr; gap:1.25rem; align-items:start;'>"
        "<aside style='position:sticky; top:1rem;'>"
        "<div class='card'><strong>Search</strong>"
        f"<datalist id='wiki-entity-suggestions'>{suggestions}</datalist>"
        "<input type='text' id='wiki-search' placeholder='slug / tag / text...' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'></div>"
        "<div class='card'><strong>Type</strong>" + type_checkboxes + "</div>"
        "<div class='card'><span id='wiki-match-count' class='muted'>-</span></div>"
        "</aside>"
        "<div id='wiki-grid' style='display:grid; "
        "grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:0.6rem;'>"
        + (
            cards
            or "<p class='muted'>No wiki entities found. "
            "Extract <code>graph/wiki-graph.tar.gz</code> into "
            "<code>~/.claude/skill-wiki/</code> to populate.</p>"
        )
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
    return layout("Wiki", body)


def runtime_graph_center_data(graph: dict) -> dict[str, Any] | None:
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


def runtime_graph_metric_row(label: str, value: object) -> str:
    if value is None or value == "":
        value_html = "<span class='muted'>unknown</span>"
    elif isinstance(value, float):
        value_html = f"<code>{value:.3f}</code>"
    else:
        value_html = f"<code>{html.escape(str(value))}</code>"
    return f"<tr><td class='muted'>{html.escape(label)}</td><td>{value_html}</td></tr>"


def render_runtime_entity_action(
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


def render_runtime_entity_load_script(
    slug: str,
    entity_type: str,
    *,
    mutations_enabled: bool,
    monitor_token: str,
) -> str:
    return (
        "<script>\n"
        f"const CTX_RUNTIME_ENTITY_MUTATIONS_ENABLED = {json.dumps(mutations_enabled)};\n"
        f"const CTX_RUNTIME_ENTITY_TOKEN = {json.dumps(monitor_token if mutations_enabled else '')};\n"
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


def render_runtime_graph_entity(
    slug: str,
    entity_type: str | None = None,
    *,
    mutations_enabled: bool | None = None,
    monitor_mutations_enabled: bool,
    monitor_token: str,
    normalize_dashboard_entity_type: Callable[[object], str | None],
    graph_neighborhood: Callable[..., dict],
    graph_slug_from_node_id: Callable[[str], str],
    graph_type_from_node_id: Callable[[str, str], str],
    display_label: Callable[..., str],
    display_slug: Callable[[str], str],
    load_sidecar: Callable[..., dict | None],
    render_quality_drilldown: Callable[[dict | None], str],
    render_entity_subgraph: Callable[[str, str | None], str],
    render_entity_tabs: Callable[..., str],
    layout: Callable[[str, str], str],
) -> str | None:
    """Render graph metadata when the fast runtime graph lacks a full wiki page."""
    normalized_type = normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and normalized_type is None:
        return None
    graph = graph_neighborhood(slug, hops=1, limit=32, entity_type=normalized_type)
    data = runtime_graph_center_data(graph)
    if data is None:
        return None

    node_id = str(data.get("id") or graph.get("center") or "")
    resolved_slug = graph_slug_from_node_id(node_id) or slug
    resolved_type = graph_type_from_node_id(
        node_id,
        str(data.get("type") or normalized_type or "skill"),
    )
    label = display_label(data.get("label"), fallback_slug=resolved_slug)
    shown_slug = display_slug(resolved_slug)
    description = str(data.get("description") or "").strip()
    tags = [str(tag) for tag in data.get("tags", []) if str(tag).strip()][:12]
    sidecar = load_sidecar(resolved_slug, entity_type=resolved_type)
    quality_score = data.get("quality_score")
    usage_score = data.get("usage_score")
    degree = data.get("degree")
    mutations = monitor_mutations_enabled if mutations_enabled is None else mutations_enabled

    quality_html = (
        render_quality_drilldown(sidecar)
        if isinstance(sidecar, dict)
        else (
            "<div class='card'>"
            "<h2>Runtime graph quality</h2>"
            "<p class='muted'>No full quality sidecar is installed for this entity. "
            "The runtime graph still exposes the ranking signals available at graph build time.</p>"
            "<table class='frontmatter-table'><tr><th>Signal</th><th>Value</th></tr>"
            + runtime_graph_metric_row("quality_score", quality_score)
            + runtime_graph_metric_row("usage_score", usage_score)
            + runtime_graph_metric_row("degree", degree)
            + "</table></div>"
        )
    )
    return render_runtime_graph_entity_page(
        label=label,
        node_id=node_id,
        resolved_slug=resolved_slug,
        resolved_type=resolved_type,
        display_slug=shown_slug,
        description=description,
        tags=tags,
        quality_score=quality_score,
        usage_score=usage_score,
        degree=degree,
        quality_html=quality_html,
        subgraph_html=render_entity_subgraph(resolved_slug, resolved_type),
        action_html=render_runtime_entity_action(
            resolved_slug,
            resolved_type,
            mutations_enabled=mutations,
        ),
        load_script_html=render_runtime_entity_load_script(
            resolved_slug,
            resolved_type,
            mutations_enabled=mutations,
            monitor_token=monitor_token,
        ),
        runtime_graph_metric_row=runtime_graph_metric_row,
        render_entity_tabs=render_entity_tabs,
        layout=layout,
    )


def render_wiki_entity(
    slug: str,
    entity_type: str | None = None,
    *,
    mutations_enabled: bool | None = None,
    entity_path: Callable[[str, str | None], Any | None],
    read_entity_text: Callable[[str, str | None, Any], str | None],
    parse_frontmatter: Callable[[str], tuple[dict[str, Any], str]],
    load_sidecar: Callable[..., dict | None],
    render_runtime_graph_entity: Callable[..., str | None],
    dashboard_entity_types: tuple[str, ...],
    display_slug: Callable[[str], str],
    frontmatter_text: Callable[[Any], str],
    truncate_text: TruncateFn,
    extract_embedded_quality_block: Callable[[str], tuple[str, str | None]],
    strip_duplicate_wiki_heading: Callable[[str, str], str],
    render_entity_subgraph: Callable[[str, str | None], str],
    render_entity_tabs: Callable[..., str],
    render_quality_drilldown: Callable[[dict[str, Any] | None, str | None], str],
    render_wiki_markdown: Callable[[str], str],
    layout: Callable[[str, str], str],
) -> str:
    """Render one wiki entity page, falling back to runtime graph metadata."""
    path = entity_path(slug, entity_type)
    if path is None:
        runtime_html = render_runtime_graph_entity(
            slug,
            entity_type=entity_type,
            mutations_enabled=mutations_enabled,
        )
        if runtime_html is not None:
            return runtime_html
        return layout(
            slug,
            f"<h1>{html.escape(slug)}</h1>"
            f"<p class='muted'>No wiki page found for <code>{html.escape(slug)}</code>. "
            f"Try <a href='/skills'>the skills index</a>.</p>",
        )
    raw = read_entity_text(slug, entity_type, path)
    if raw is None:
        return layout(
            slug,
            f"<h1>{html.escape(slug)}</h1><p class='muted'>read error: page unavailable</p>",
        )
    meta, md_body = parse_frontmatter(raw)
    sidecar = load_sidecar(slug, entity_type=entity_type)
    return render_wiki_entity_page(
        slug=slug,
        entity_type=entity_type,
        meta=meta,
        md_body=md_body,
        sidecar=sidecar if isinstance(sidecar, dict) else None,
        dashboard_entity_types=dashboard_entity_types,
        display_slug=display_slug,
        frontmatter_text=frontmatter_text,
        truncate_text=truncate_text,
        extract_embedded_quality_block=extract_embedded_quality_block,
        strip_duplicate_wiki_heading=strip_duplicate_wiki_heading,
        render_entity_subgraph=render_entity_subgraph,
        render_entity_tabs=render_entity_tabs,
        render_quality_drilldown=render_quality_drilldown,
        render_wiki_markdown=render_wiki_markdown,
        layout=layout,
    )


def render_runtime_graph_entity_page(
    *,
    label: str,
    node_id: str,
    resolved_slug: str,
    resolved_type: str,
    display_slug: str,
    description: str,
    tags: list[str],
    quality_score: Any,
    usage_score: Any,
    degree: Any,
    quality_html: str,
    subgraph_html: str,
    action_html: str,
    load_script_html: str,
    runtime_graph_metric_row: MetricRowFn,
    render_entity_tabs: Callable[..., str],
    layout: Callable[[str, str], str],
) -> str:
    """Render graph metadata when the fast runtime graph lacks a full wiki page."""
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
        + runtime_graph_metric_row("slug", display_slug)
        + runtime_graph_metric_row("type", resolved_type)
        + runtime_graph_metric_row("node_id", node_id)
        + runtime_graph_metric_row("quality_score", quality_score)
        + runtime_graph_metric_row("usage_score", usage_score)
        + runtime_graph_metric_row("degree", degree)
        + "</table></div>"
        "</div>"
        + action_html
    )
    body = (
        f"<h1>{html.escape(label)}</h1>"
        + quality_summary
        + render_entity_tabs(
            overview_html=overview_html,
            subgraph_html=subgraph_html,
            quality_html=quality_html,
        )
        + load_script_html
    )
    return layout(label, body)


def render_wiki_entity_page(
    *,
    slug: str,
    entity_type: str | None,
    meta: dict[str, Any],
    md_body: str,
    sidecar: dict[str, Any] | None,
    dashboard_entity_types: tuple[str, ...],
    display_slug: Callable[[str], str],
    frontmatter_text: Callable[[Any], str],
    truncate_text: TruncateFn,
    extract_embedded_quality_block: Callable[[str], tuple[str, str | None]],
    strip_duplicate_wiki_heading: Callable[[str, str], str],
    render_entity_subgraph: Callable[[str, str | None], str],
    render_entity_tabs: Callable[..., str],
    render_quality_drilldown: Callable[[dict[str, Any] | None, str | None], str],
    render_wiki_markdown: Callable[[str], str],
    layout: Callable[[str, str], str],
) -> str:
    """Render one expanded wiki entity page from already-loaded Markdown."""
    display = display_slug(slug)
    type_suffix = (
        f"&amp;type={html.escape(entity_type)}"
        if entity_type in dashboard_entity_types
        else ""
    )

    fm_row_parts = []
    for key, value_raw in sorted(meta.items()):
        value, truncated = truncate_text(frontmatter_text(value_raw), 120)
        marker = " <span class='muted'>(truncated)</span>" if truncated else ""
        fm_row_parts.append(
            f"<tr><td class='muted'>{html.escape(key)}</td>"
            f"<td><code>{html.escape(value)}</code>{marker}</td></tr>"
        )
    fm_rows = "".join(fm_row_parts)

    quality_summary_html = ""
    if sidecar is not None:
        grade = str(sidecar.get("grade", "F"))
        score = float(sidecar.get("raw_score", 0.0) or 0.0)
        hard_floor = str(sidecar.get("hard_floor") or "")
        floor_html = (
            " &middot; floor " + html.escape(hard_floor)
            if hard_floor
            else ""
        )
        quality_summary_html = (
            "<div class='card'>"
            f"<strong>Quality</strong> <span class='pill grade-{html.escape(grade)}'>"
            f"{html.escape(grade)}</span> "
            f"score <strong>{score:.3f}</strong>"
            f"{floor_html}"
            f"<div style='margin-top:0.4rem;'>"
            "<a href='#quality' data-open-entity-tab='quality'>quality drilldown &rarr;</a> &middot; "
            f"<a href='/skill/{html.escape(slug)}?type={html.escape(entity_type or '')}'>sidecar detail &rarr;</a> &middot; "
            f"<a href='/graph?slug={html.escape(slug)}{type_suffix}'>graph neighborhood &rarr;</a>"
            "</div></div>"
        )

    md_body_without_quality, embedded_quality_markdown = extract_embedded_quality_block(md_body)
    display_body = strip_duplicate_wiki_heading(md_body_without_quality, slug)
    body_preview, body_truncated = truncate_text(display_body, 12000)
    body_html = render_wiki_markdown(body_preview)
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
    body = (
        f"<h1>{html.escape(display)}</h1>"
        + quality_summary_html
        + render_entity_tabs(
            overview_html=overview_html,
            subgraph_html=render_entity_subgraph(slug, entity_type),
            quality_html=render_quality_drilldown(sidecar, embedded_quality_markdown),
        )
    )
    return layout(display, body)
