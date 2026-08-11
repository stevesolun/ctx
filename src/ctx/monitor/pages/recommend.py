"""Recommendation selection page renderer for the ctx dashboard."""

from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

LayoutFn = Callable[[str, str], str]
RecommendFn = Callable[[str, int], list[dict[str, Any]]]
RelatedFn = Callable[[list[str], list[str], int], list[dict[str, Any]]]


def _split_values(raw: str | None) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").split(","):
        item = part.strip()
        if item and item not in seen:
            values.append(item)
            seen.add(item)
    return values


def _top_k(raw: str | None) -> int:
    try:
        value = int(raw or 5)
    except ValueError:
        value = 5
    return max(1, min(value, 5))


def _row_identity(row: dict[str, Any]) -> str:
    row_id = row.get("id")
    if row_id:
        return str(row_id)
    entity_type = str(row.get("type") or row.get("entity_type") or "skill")
    name = str(row.get("name") or row.get("slug") or "")
    return f"{entity_type}:{name}" if name else entity_type


def _row_cell(row: dict[str, Any], key: str, limit: int = 120) -> str:
    return html.escape(str(row.get(key) or ""))[:limit]


def _score_text(row: dict[str, Any]) -> str:
    score = row.get("normalized_score", row.get("score", 0.0))
    try:
        return f"{float(score):.3f}"
    except (TypeError, ValueError):
        return html.escape(str(score))


def _selected_link(query: str, top_k: int, selected: list[str], rejected: list[str]) -> str:
    payload = {"q": query, "top_k": str(top_k)}
    if selected:
        payload["selected"] = ",".join(selected)
    if rejected:
        payload["rejected"] = ",".join(rejected)
    return "/recommend?" + urlencode(payload)


def _render_rows(
    rows: list[dict[str, Any]],
    *,
    selected: list[str],
    selectable: bool,
) -> str:
    selected_set = set(selected)
    body = []
    for row in rows:
        row_id = _row_identity(row)
        checked = " checked" if row_id in selected_set or row.get("name") in selected_set else ""
        checkbox = (
            f"<input type='checkbox' name='selected' value='{html.escape(row_id)}'{checked}>"
            if selectable
            else ""
        )
        body.append(
            "<tr>"
            f"<td>{checkbox}</td>"
            f"<td><code>{html.escape(row_id)}</code></td>"
            f"<td>{_row_cell(row, 'type') or _row_cell(row, 'entity_type')}</td>"
            f"<td>{_row_cell(row, 'name') or _row_cell(row, 'slug')}</td>"
            f"<td>{_score_text(row)}</td>"
            f"<td>{_row_cell(row, 'selection_state')}</td>"
            f"<td>{_row_cell(row, 'tldr')}</td>"
            f"<td>{_row_cell(row, 'reason', 180)}</td>"
            "</tr>"
        )
    return (
        "<table><tr><th>Select</th><th>ID</th><th>Type</th><th>Name</th>"
        "<th>Score</th><th>State</th><th>TLDR</th><th>Reason</th></tr>" + "".join(body) + "</table>"
    )


def render_recommendations(
    *,
    layout: LayoutFn,
    query: dict[str, str],
    recommend_bundle: RecommendFn,
    recommend_related: RelatedFn,
) -> str:
    """Render selectable recommendation rows and related suggestions."""
    q = str(query.get("q") or "").strip()
    top_k = _top_k(query.get("top_k"))
    selected = _split_values(query.get("selected"))
    rejected = _split_values(query.get("rejected"))
    rows: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []
    error = ""

    if q:
        try:
            rows = recommend_bundle(q, top_k)
            if selected:
                related = recommend_related(selected, rejected, top_k)
        except Exception as exc:  # noqa: BLE001 - dashboard must show an error state.
            error = f"{type(exc).__name__}: {exc}"

    all_ids = [_row_identity(row) for row in rows]
    search_form = (
        "<form method='get' action='/recommend' class='card'>"
        "<label>Query <input name='q' value='" + html.escape(q, quote=True) + "'></label> "
        "<label>Top K <input name='top_k' type='number' min='1' max='5' value='"
        + str(top_k)
        + "'></label> "
        "<label>Rejected <input name='rejected' value='"
        + html.escape(",".join(rejected), quote=True)
        + "'></label> "
        "<button type='submit'>Recommend</button>"
        "</form>"
    )
    links = ""
    if q and rows:
        links = (
            "<p>"
            f"<a href='{html.escape(_selected_link(q, top_k, all_ids, rejected), quote=True)}'>"
            "Select all</a> / "
            f"<a href='{html.escape(_selected_link(q, top_k, [], rejected), quote=True)}'>"
            "Select none</a>"
            "</p>"
        )

    results = ""
    if error:
        results = f"<div class='card'><strong>Error</strong><p>{html.escape(error)}</p></div>"
    elif q and rows:
        results = (
            "<form method='get' action='/recommend' class='card'>"
            f"<input type='hidden' name='q' value='{html.escape(q, quote=True)}'>"
            f"<input type='hidden' name='top_k' value='{top_k}'>"
            f"<input type='hidden' name='rejected' value='{html.escape(','.join(rejected), quote=True)}'>"
            "<strong>Recommendations</strong>"
            + links
            + _render_rows(rows, selected=selected, selectable=True)
            + "<button type='submit'>Show related</button>"
            + "</form>"
        )
    elif q:
        results = "<div class='card'><strong>No recommendations above threshold.</strong></div>"

    related_html = ""
    if selected:
        related_html = (
            "<div class='card'><strong>Related recommendations</strong>"
            + (
                _render_rows(related, selected=[], selectable=False)
                if related
                else "<p class='muted'>No related recommendations above threshold.</p>"
            )
            + "</div>"
        )

    return layout(
        "Recommendations", "<h1>Recommendations</h1>" + search_form + results + related_html
    )
