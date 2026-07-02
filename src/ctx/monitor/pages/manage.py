"""Catalog management page for ctx-monitor."""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Sequence


LayoutFn = Callable[[str, str], str]


def render_manage(
    *,
    mutations_enabled: bool,
    token: str,
    initial_results_json: str,
    entity_types: Sequence[str],
    inline_script: Callable[[str], str],
    layout: LayoutFn,
) -> str:
    type_options = "".join(
        f"<option value='{html.escape(entity_type)}'>{html.escape(entity_type)}</option>"
        for entity_type in entity_types
    )
    read_only = (
        ""
        if mutations_enabled
        else (
            "<div class='card'><strong>Read-only mode.</strong> Catalog edits are "
            "disabled because ctx-monitor is not bound to a loopback address.</div>"
        )
    )
    disabled = "" if mutations_enabled else " disabled"
    body = (
        "<h1>Manage catalog</h1>"
        "<p class='muted'>Search skills, agents, MCP servers, and harnesses. "
        "Edit the wiki page, delete stale entries, or add a new entity. Saves "
        "write into <code>~/.claude/skill-wiki/entities/</code> and queue graph "
        "refresh work for the knowledge graph.</p>" + read_only + "<div class='wizard-layout'>"
        "<section class='card'>"
        "<h2>Search catalog</h2>"
        "<div class='wizard-grid'>"
        "<label>Query"
        "<input id='manage-search' type='search' placeholder='slug, tag, description' "
        "autocomplete='off'></label>"
        "<label>Type"
        f"<select id='manage-type'><option value=''>all types</option>{type_options}</select>"
        "</label>"
        "</div>"
        "<p id='manage-search-status' class='muted'>Loading...</p>"
        "<div id='manage-results' class='manage-results'></div>"
        "</section>"
        "<section class='card'>"
        "<h2>Add or update entity</h2>"
        "<form id='entity-editor-form'>"
        "<div class='wizard-grid'>"
        "<label>Slug <span class='pill grade-A'>Required</span>"
        "<input name='slug' required pattern='[a-z0-9][a-z0-9_.+-]*' "
        "placeholder='custom-reviewer'></label>"
        "<label>Type <span class='pill grade-A'>Required</span>"
        f"<select name='entity_type' required>{type_options}</select></label>"
        "<label>Title <span class='pill grade-A'>Required</span>"
        "<input name='title' required placeholder='Custom Reviewer'></label>"
        "<label>Tags"
        "<input name='tags' placeholder='python, review, policy'></label>"
        "<label class='wide'>Description"
        "<input name='description' placeholder='What this entity does and when to use it'></label>"
        "<label class='wide'>Source URL"
        "<input name='source_url' placeholder='https://github.com/org/repo'></label>"
        "<label class='wide'>Markdown body <span class='pill grade-A'>Required</span>"
        "<textarea name='body' required rows='16' "
        "placeholder='# Custom Reviewer\n\nInstall and usage notes...'></textarea></label>"
        "</div>"
        "<div style='display:flex; gap:0.5rem; flex-wrap:wrap; margin-top:0.8rem;'>"
        f"<button type='submit'{disabled}>Save to wiki + graph queue</button>"
        "<button type='button' id='entity-new-button'>New</button>"
        f"<button type='button' id='entity-delete-button' data-testid='entity-delete-button'{disabled}>Delete selected</button>"
        "</div>"
        "<p id='entity-editor-status' class='muted'></p>"
        "</form>"
        "</section>"
        "</div>"
        "<script>\n"
        "window.CTX_MONITOR_MANAGE = {\n"
        f"  mutationsEnabled: {json.dumps(mutations_enabled)},\n"
        f"  token: {json.dumps(token)},\n"
        f"  initialResults: {initial_results_json}\n"
        "};\n"
        "</script>" + inline_script("monitor-manage.js")
    )
    return layout("Manage catalog", body)
