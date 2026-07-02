"""Quality sidecar list page renderer for ctx-monitor."""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

LayoutFn = Callable[[str, str], str]
EntityTypeFn = Callable[[dict[str, Any]], str]


def render_skills(
    *,
    payload: dict[str, Any],
    query_params: dict[str, str] | None,
    entity_types: tuple[str, ...],
    layout: LayoutFn,
    sidecar_entity_type: EntityTypeFn,
) -> str:
    """Render the paginated quality sidecar grid."""
    sidecars = payload["items"]

    cards = "".join(_sidecar_card(sidecar, sidecar_entity_type) for sidecar in sidecars)

    start_index = ((payload["page"] - 1) * payload["limit"]) + 1 if payload["total"] else 0
    end_index = min(payload["page"] * payload["limit"], payload["total"])
    summary = (
        f"Showing {start_index}-{end_index} of {payload['total']} matching sidecars"
        if payload["filtered"]
        else f"Showing {start_index}-{end_index} of {payload['catalog_total']} sidecars"
    )
    query_base = {key: value for key, value in (query_params or {}).items() if key not in {"page"}}

    def page_href(page: int) -> str:
        params = {
            **query_base,
            "page": str(max(1, page)),
            "limit": str(payload["limit"]),
        }
        query = "&".join(
            f"{quote(str(key))}={quote(str(value))}"
            for key, value in params.items()
            if str(value).strip()
        )
        return "/skills" + (f"?{query}" if query else "")

    prev_link = (
        f"<a href='{html.escape(page_href(payload['page'] - 1))}'>previous</a>"
        if payload["has_prev"]
        else "<span class='muted'>previous</span>"
    )
    next_link = (
        f"<a href='{html.escape(page_href(payload['page'] + 1))}'>next</a>"
        if payload["has_next"]
        else "<span class='muted'>next</span>"
    )
    pagination = (
        "<div class='card' style='display:flex; justify-content:space-between; "
        "align-items:center; gap:1rem;'>"
        f"<span id='match-count' class='muted'>{html.escape(summary)} &middot; page "
        f"{payload['page']} of {payload['pages']}</span>"
        f"<span>{prev_link} &middot; {next_link}</span>"
        "</div>"
    )
    selected_type = ",".join(payload["types"])
    selected_grade = ",".join(payload["grades"])
    type_options = "<option value=''>all types</option>" + "".join(
        f"<option value='{html.escape(entity_type)}'"
        f"{' selected' if selected_type == entity_type else ''}>{html.escape(entity_type)}</option>"
        for entity_type in entity_types
    )
    grade_options = "<option value=''>all grades</option>" + "".join(
        f"<option value='{grade}'{' selected' if selected_grade == grade else ''}>{grade}</option>"
        for grade in ("A", "B", "C", "D", "F")
    )
    limit_options = "".join(
        f"<option value='{limit}'{' selected' if payload['limit'] == limit else ''}>{limit}</option>"
        for limit in (50, 100, 200, 500)
    )
    hide_checked = " checked" if payload["hide_floor"] else ""

    body = (
        "<h1>Quality sidecars</h1>"
        f"<p class='muted'>{payload['catalog_total']} sidecars &middot; click any card to drill in.</p>"
        + pagination
        + "<div style='display:grid; grid-template-columns:220px 1fr; gap:1.25rem; align-items:start;'>"
        "<aside style='position:sticky; top:1rem;'>"
        "<form class='card' id='skills-filter-form' method='get' action='/skills'>"
        "<strong>Search</strong>"
        f"<input type='text' id='skill-search' name='q' value='{html.escape(payload['q'])}' placeholder='filter by slug...' "
        "style='width:100%; margin-top:0.4rem; padding:0.35rem 0.5rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<label style='display:block; margin-top:0.6rem; font-size:0.82rem;'>Type"
        f"<select class='type-filter' name='type' style='width:100%; margin-top:0.25rem;'>{type_options}</select>"
        "</label>"
        "<label style='display:block; margin-top:0.6rem; font-size:0.82rem;'>Grade"
        f"<select class='grade-filter' name='grade' style='width:100%; margin-top:0.25rem;'>{grade_options}</select>"
        "</label>"
        "<label style='display:block; margin-top:0.6rem; font-size:0.82rem;'>Limit"
        f"<select name='limit' style='width:100%; margin-top:0.25rem;'>{limit_options}</select>"
        "</label>"
        "<label style='display:block; padding:0.55rem 0 0.35rem;'>"
        f"<input type='checkbox' id='hide-floor' name='hide_floor' value='1'{hide_checked}> hide floored</label>"
        "<button type='submit' style='width:100%;'>apply</button>"
        "</form>"
        "</aside>"
        "<div id='card-grid' style='display:grid; "
        "grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:0.7rem;'>"
        + cards
        + "</div>"
        "</div>"
        "<script>\n"
        "document.querySelectorAll('#skills-filter-form select').forEach(el => {\n"
        "  el.addEventListener('change', () => el.form.submit());\n"
        "});\n"
        "</script>"
    )
    return layout("Skills", body)


def _sidecar_card(sidecar: dict[str, Any], sidecar_entity_type: EntityTypeFn) -> str:
    slug = str(sidecar.get("slug", ""))
    grade = str(sidecar.get("grade", "F"))
    entity_type = sidecar_entity_type(sidecar)
    raw_score = float(sidecar.get("raw_score", 0.0) or 0.0)
    hard_floor = str(sidecar.get("hard_floor") or "")
    display_type = str(sidecar.get("type", sidecar.get("subject_type", "skill")))
    floor_fragment = f" &middot; {html.escape(hard_floor)}" if hard_floor else ""
    return (
        f"<div class='skill-card' data-slug='{html.escape(slug)}' "
        f"data-grade='{html.escape(grade)}' "
        f"data-type='{html.escape(entity_type)}' "
        f"data-floor='{html.escape(hard_floor)}' "
        "style='border:1px solid #e5e7eb; border-radius:6px; padding:0.7rem 0.9rem; "
        "display:flex; flex-direction:column; gap:0.3rem;'>"
        "<div style='display:flex; justify-content:space-between; align-items:center;'>"
        f"<code style='font-size:0.85rem;'>{html.escape(slug)}</code>"
        f"<span class='pill grade-{html.escape(grade)}'>{html.escape(grade)}</span>"
        "</div>"
        "<div class='muted' style='font-size:0.78rem;'>"
        f"score {raw_score:.3f} &middot; {html.escape(display_type)}"
        f"{floor_fragment}"
        "</div>"
        "<div style='display:flex; gap:0.4rem; margin-top:0.2rem;'>"
        f"<a href='/skill/{html.escape(slug)}?type={html.escape(entity_type)}' style='font-size:0.78rem;'>sidecar</a>"
        f"<a href='/wiki/{html.escape(slug)}?type={html.escape(entity_type)}' style='font-size:0.78rem;'>wiki</a>"
        f"<a href='/graph?slug={html.escape(slug)}&amp;type={html.escape(entity_type)}' style='font-size:0.78rem;'>graph</a>"
        "</div>"
        "</div>"
    )
