"""Wiki index rendering helpers for ctx-monitor."""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from ctx.monitor.pages import wiki_entity, wiki_markdown

TruncateFn = wiki_markdown.TruncateFn
WikiLinkFn = wiki_markdown.WikiLinkFn
markdown_link_href = wiki_markdown.markdown_link_href
render_wiki_inline = wiki_markdown.render_wiki_inline
render_wiki_markdown = wiki_markdown.render_wiki_markdown
entity_tab_script = wiki_entity.entity_tab_script
render_entity_subgraph = wiki_entity.render_entity_subgraph
render_entity_subgraph_svg = wiki_entity.render_entity_subgraph_svg
render_entity_tabs = wiki_entity.render_entity_tabs
render_quality_drilldown = wiki_entity.render_quality_drilldown
render_runtime_graph_entity = wiki_entity.render_runtime_graph_entity
render_runtime_graph_entity_page = wiki_entity.render_runtime_graph_entity_page
render_wiki_entity = wiki_entity.render_wiki_entity
render_wiki_entity_page = wiki_entity.render_wiki_entity_page
runtime_graph_center_data = wiki_entity.runtime_graph_center_data
runtime_graph_metric_row = wiki_entity.runtime_graph_metric_row
subgraph_quality_cell = wiki_entity.subgraph_quality_cell


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


def render_wiki_index(
    entity_type: str | None = None,
    query: str = "",
    *,
    normalize_dashboard_entity_type: Callable[[object], str | None],
    wiki_render_cache_key: Callable[[str | None, str], tuple[Any, ...] | None],
    read_memory_cache: Callable[[tuple[Any, ...]], str | None],
    write_memory_cache: Callable[[tuple[Any, ...], str], None],
    disk_cache_token: Callable[[tuple[Any, ...]], str],
    read_html_disk_cache: Callable[[Any, str], str | None],
    write_html_disk_cache: Callable[[Any, str, str], None],
    wiki_render_disk_cache_path: Callable[[], Any],
    wiki_index_entries: Callable[[], list[dict]],
    search_wiki_entities: Callable[..., list[dict[str, Any]]] | None,
    wiki_stats: Callable[[], dict[str, Any]],
    load_sidecar: Callable[..., dict | None],
    dashboard_entity_types: tuple[str, ...],
    layout: Callable[[str, str], str],
) -> str:
    """Render the searchable wiki index with dashboard cache integration."""
    selected_type = normalize_dashboard_entity_type(entity_type) if entity_type else None
    if entity_type is not None and selected_type is None:
        return layout(
            "Wiki",
            f"<div class='error'>Unsupported entity type: {html.escape(entity_type)}</div>",
        )
    initial_query = query.strip()
    cache_key = wiki_render_cache_key(selected_type, initial_query)
    if cache_key is not None:
        cache_token = disk_cache_token(cache_key)
        if not initial_query:
            cached = read_memory_cache(cache_key)
            if cached is not None:
                return cached
            cached = read_html_disk_cache(wiki_render_disk_cache_path(), cache_token)
            if cached is not None:
                write_memory_cache(cache_key, cached)
                return cached
    else:
        cache_token = ""

    if initial_query and search_wiki_entities is not None:
        uses_bounded_search = True
        entries = search_wiki_entities(initial_query, selected_type, limit=120)
    else:
        uses_bounded_search = False
        entries = wiki_index_entries()
    wstats = wiki_stats()
    total_available = int(wstats.get("total") or len(entries))
    grade_by_key: dict[tuple[str, str], str] = {}
    for entry in entries:
        slug = str(entry["slug"])
        row_type = str(entry["type"])
        grade = str(entry.get("grade") or "")
        if grade:
            grade_by_key[(slug, row_type)] = grade
            continue
        if not uses_bounded_search:
            sidecar = load_sidecar(slug, entity_type=row_type)
            if sidecar:
                grade_by_key[(slug, row_type)] = str(sidecar.get("grade") or "")

    type_counts = {
        "skill": int(wstats.get("skills") or 0),
        "agent": int(wstats.get("agents") or 0),
        "mcp-server": int(wstats.get("mcps") or 0),
        "harness": int(wstats.get("harnesses") or 0),
    }
    html_out = render_wiki_index_page(
        entries=entries,
        selected_type=selected_type,
        initial_query=initial_query,
        total_available=total_available,
        type_counts=type_counts,
        grade_by_key=grade_by_key,
        dashboard_entity_types=dashboard_entity_types,
        layout=layout,
    )
    if cache_key is not None:
        write_html_disk_cache(wiki_render_disk_cache_path(), cache_token, html_out)
        write_memory_cache(cache_key, html_out)
    return html_out
