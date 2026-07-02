"""Knowledge graph page for ctx-monitor."""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

from ctx.monitor.pages.graph_client import render_graph_client_script

LayoutFn = Callable[[str, str], str]


def render_graph(
    focus: str | None = None,
    focus_type: str | None = None,
    *,
    graph_stats: Callable[[], dict[str, Any]],
    top_degree_seeds: Callable[..., list[dict[str, Any]]],
    default_focus_slug: str,
    json_for_script: Callable[[Any], str],
    graph_match_default_min_percent: Callable[[], int],
    format_count: Callable[[Any], str],
    layout: LayoutFn,
) -> str:
    """Interactive graph view backed by a dependency-free SVG renderer."""
    focus_slug = focus or ""
    gstats = graph_stats()
    seeds = top_degree_seeds(allow_load=False) if not focus_slug and gstats.get("available") else []
    initial_slug = focus_slug
    initial_type = focus_type or ""
    if not initial_slug and seeds:
        initial_slug = str(seeds[0].get("slug") or "")
        initial_type = str(seeds[0].get("type") or "")
    elif not initial_slug and gstats.get("available"):
        initial_slug = default_focus_slug
    focus_js = json_for_script(initial_slug)
    focus_type_js = json_for_script(initial_type)
    match_default_min = graph_match_default_min_percent()
    seed_html = ""
    if seeds:
        chips = "".join(
            f"<a href='/graph?slug={html.escape(s['slug'])}&amp;type={html.escape(s['type'])}' "
            f"style='display:inline-block; margin:0.2rem 0.25rem; padding:0.25rem 0.6rem; "
            f"border-radius:999px; background:{'#fef3c7' if s['type'] == 'agent' else '#fee2e2' if s['type'] == 'mcp-server' else '#dcfce7' if s['type'] == 'harness' else '#e0e7ff'}; "
            f"color:#111; font-size:0.82rem; text-decoration:none;'>"
            f"<code style='background:transparent;'>{html.escape(s['slug'])}</code> "
            f"<span class='muted' style='font-size:0.72rem;'>- deg {format_count(s['degree'])}</span>"
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
        f"<span class='muted'>{gstats.get('nodes', 0):,} nodes - "
        f"{gstats.get('edges', 0):,} edges</span>"
    )
    body = (
        "<h1>Knowledge graph</h1>"
        f"<p class='muted'>Enter an entity slug to explore its 1-hop "
        f"neighborhood. Edges blend semantic + tag + slug-token "
        f"signals (weight = final_weight). {stats_html}</p>"
        + seed_html
        # Two-column layout - filter sidebar on the left (mirrors /wiki),
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
        f"value='{html.escape(initial_slug)}' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<div id='graph-live-results' data-testid=\"graph-live-results\" "
        "class='graph-live-results' hidden></div>"
        "<select id='focus-type' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<option value=''>auto type</option>"
        f"<option value='skill' {'selected' if initial_type == 'skill' else ''}>skill</option>"
        f"<option value='agent' {'selected' if initial_type == 'agent' else ''}>agent</option>"
        f"<option value='mcp-server' {'selected' if initial_type == 'mcp-server' else ''}>mcp-server</option>"
        f"<option value='harness' {'selected' if initial_type == 'harness' else ''}>harness</option>"
        "</select>"
        "<button id='go' style='margin-top:0.4rem; width:100%;'>"
        "explore</button></div>"
        "<div class='card'><strong>Type</strong>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='skill' checked> skill</span>"
        "<span class='muted' id='graph-count-skill' style='font-size:0.78rem;'>-</span></label>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='agent' checked> agent</span>"
        "<span class='muted' id='graph-count-agent' style='font-size:0.78rem;'>-</span></label>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='mcp-server' checked> mcp-server</span>"
        "<span class='muted' id='graph-count-mcp-server' style='font-size:0.78rem;'>-</span></label>"
        "<label style='display:flex; justify-content:space-between; padding:0.25rem 0;'>"
        "<span><input type='checkbox' class='graph-type-filter' value='harness' checked> harness</span>"
        "<span class='muted' id='graph-count-harness' style='font-size:0.78rem;'>-</span></label>"
        "</div>"
        "<div class='card graph-match-card'><strong>Match</strong>"
        "<div class='graph-match-summary'>"
        "<span id='match-filter-min-value' class='pill'>"
        f"{match_default_min}%</span>"
        "<span class='muted'>-</span>"
        "<span id='match-filter-max-value' class='pill'>100%</span></div>"
        "<div id='match-histogram' data-testid=\"match-histogram\" "
        "class='graph-match-histogram' aria-label='Match distribution'></div>"
        "<div class='graph-range-wrap' data-testid=\"match-range-control\">"
        "<div class='graph-range-track'></div>"
        "<div id='match-range-fill' class='graph-range-fill'></div>"
        "<input type='range' id='match-filter-min' aria-label='minimum match' "
        "min='0' max='100' step='1' "
        f"value='{match_default_min}'>"
        "<input type='range' id='match-filter-max' aria-label='maximum match' "
        "min='0' max='100' step='1' value='100'>"
        "</div>"
        "<div class='graph-range-scale'><span>0%</span><span>100%</span></div>"
        "<p class='muted' style='font-size:0.72rem; margin:0.35rem 0 0 0;'>"
        "Shows nodes with at least one relationship inside this match range.</p></div>"
        "<div class='card'><strong>Tag filter</strong>"
        "<input type='text' id='tag-filter' "
        "placeholder='shared_tag or slug_token' "
        "style='width:100%; margin-top:0.4rem; padding:0.3rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<p class='muted' style='font-size:0.72rem; margin:0.4rem 0 0 0;'>"
        "Filters nodes by tag substring (client-side).</p></div>"
        "<div class='card'>"
        "<span id='graph-match-count' class='muted'>-</span>"
        "</div>"
        "<div class='card'><span id='msg' class='muted'></span></div>"
        "<div class='card'><strong>Why this view?</strong>"
        "<p id='graph-explanation' class='muted' "
        "style='font-size:0.78rem; margin:0.45rem 0 0 0;'>"
        "Search a slug to see why ctx picked this neighborhood and how to read it."
        "</p></div>"
        "</aside>"
        # Right: graph list panel
        "<div id='cy' class='graph-stage'></div>"
        "</div>"
        + render_graph_client_script(
            focus_js=focus_js,
            focus_type_js=focus_type_js,
        )
    )
    return layout("Graph", body)
