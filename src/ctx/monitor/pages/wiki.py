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
