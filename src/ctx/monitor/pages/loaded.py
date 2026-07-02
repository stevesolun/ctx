"""Loaded-entity page renderer for ctx-monitor."""

from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Any

LayoutFn = Callable[[str, str], str]


def render_loaded(
    manifest: dict[str, Any],
    *,
    mutations_enabled: bool,
    monitor_token: str,
    layout: LayoutFn,
) -> str:
    """Render the live manifest view with load/unload actions."""
    load_rows = manifest.get("load", [])
    unload_rows = manifest.get("unload", [])

    def etype(entry: dict[str, Any]) -> str:
        # Missing entity_type => legacy skill entry.
        return str(entry.get("entity_type") or "skill")

    by_type: dict[str, list[dict[str, Any]]] = {
        "skill": [],
        "agent": [],
        "mcp-server": [],
        "harness": [],
    }
    for entry in load_rows:
        by_type.setdefault(etype(entry), []).append(entry)

    disabled_attr = "" if mutations_enabled else " disabled"
    mutation_token = monitor_token if mutations_enabled else ""
    mutation_notice = (
        ""
        if mutations_enabled
        else (
            "<div class='card'><strong>Read-only mode.</strong> "
            "Load/unload actions are disabled because ctx-monitor is not "
            "bound to a loopback address.</div>"
        )
    )

    def row(entry: dict[str, Any]) -> str:
        slug = entry.get("skill", "")
        entity_type = etype(entry)
        link = (
            f"<a href='/wiki/{html.escape(slug)}?type={html.escape(entity_type)}'>"
            f"<code>{html.escape(slug)}</code></a>"
        )
        action = (
            f"<td class='muted'><code>ctx-harness-install {html.escape(slug)} "
            f"--uninstall --dry-run</code></td>"
            if entity_type == "harness"
            else (
                f"<td><button class='btn-unload' data-slug='{html.escape(slug)}' "
                f"data-etype='{html.escape(entity_type)}'{disabled_attr}>unload</button></td>"
            )
        )
        return (
            "<tr>"
            f"<td>{link}</td>"
            f"<td class='muted'>{html.escape(entry.get('source', ''))}</td>"
            f"<td class='muted'>{html.escape(str(entry.get('command', '') or entry.get('priority', '—')))[:60]}</td>"
            f"{action}"
            "</tr>"
        )

    def section(title: str, entity_type: str) -> str:
        rows = by_type.get(entity_type, [])
        if not rows:
            return (
                f"<h3 style='margin-top:1.2rem;'>{title} "
                f"<span class='muted' style='font-size:0.85rem;'>(0)</span></h3>"
                "<p class='muted' style='margin-left:0.4rem;'>None loaded.</p>"
            )
        return (
            f"<h3 style='margin-top:1.2rem;'>{title} "
            f"<span class='muted' style='font-size:0.85rem;'>({len(rows)})</span></h3>"
            "<table>"
            "<tr><th>Slug</th><th>Source</th><th>Cmd / priority</th><th></th></tr>"
            + "".join(row(entry) for entry in rows)
            + "</table>"
        )

    unload_html = "".join(
        "<tr>"
        f"<td><code>{html.escape(entry.get('skill', ''))}</code></td>"
        f"<td class='muted'>{html.escape(etype(entry))}</td>"
        f"<td class='muted'>{html.escape(str(entry.get('source', '') or entry.get('reason', ''))[:80])}</td>"
        f"<td><button class='btn-load' data-slug='{html.escape(entry.get('skill', ''))}' "
        f"data-etype='{html.escape(etype(entry))}'"
        f"{' data-command=' + repr(html.escape(str(entry.get('command')))) if entry.get('command') else ''}"
        f"{' data-json-config=' + repr(html.escape(str(entry.get('json_config')))) if entry.get('json_config') else ''}"
        f"{disabled_attr}>load</button></td>"
        "</tr>"
        for entry in unload_rows
    )

    body = (
        "<h1>Loaded entities — skills, agents, MCPs &amp; harnesses</h1>"
        "<div class='card'>"
        f"<strong>{len(load_rows)}</strong> currently loaded "
        "(<span class='muted'>"
        f"{len(by_type.get('skill', []))} skills · "
        f"{len(by_type.get('agent', []))} agents · "
        f"{len(by_type.get('mcp-server', []))} MCPs · "
        f"{len(by_type.get('harness', []))} harnesses</span>) · "
        f"<strong>{len(unload_rows)}</strong> known-unloaded · "
        "<span class='muted'>source: <code>~/.claude/skill-manifest.json</code> "
        "+ <code>~/.claude/harness-installs/*.json</code></span>"
        "</div>"
        f"{mutation_notice}"
        "<h2>Load an entity</h2>"
        "<div class='card'>"
        "<form id='load-form'>"
        "<input type='text' id='load-input' placeholder='slug (e.g. fastapi-pro)' "
        "style='padding:0.35rem 0.6rem; width:18rem; border:1px solid #ccc; "
        "border-radius:4px;'>"
        "<select id='load-type' style='margin-left:0.5rem; padding:0.35rem 0.6rem; "
        "border:1px solid #ccc; border-radius:4px;'>"
        "<option value='skill'>skill</option>"
        "<option value='agent'>agent</option>"
        "<option value='mcp-server'>mcp-server</option>"
        "</select>"
        f"<button type='submit' style='margin-left:0.5rem;'{disabled_attr}>load</button>"
        "<span id='load-msg' class='muted' style='margin-left:0.75rem;'></span>"
        "</form></div>"
        f"<h2>Currently loaded ({len(load_rows)})</h2>"
        + section("Skills", "skill")
        + section("Agents", "agent")
        + section("MCP servers", "mcp-server")
        + section("Harnesses", "harness")
        + f"<h2>Recently unloaded ({len(unload_rows)})</h2>"
        "<table><tr><th>Slug</th><th>Type</th><th>Source / reason</th><th></th></tr>"
        + unload_html
        + "</table>"
        "<script>\n"
        f"const CTX_MONITOR_MUTATIONS_ENABLED = {json.dumps(mutations_enabled)};\n"
        f"const CTX_MONITOR_TOKEN = {json.dumps(mutation_token)};\n"
        "async function post(url, body) {\n"
        "  if (!CTX_MONITOR_MUTATIONS_ENABLED) return {ok:false, msg:'mutations disabled on non-loopback bind'};\n"
        "  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json', 'X-CTX-Monitor-Token':CTX_MONITOR_TOKEN}, body: JSON.stringify(body || {})});\n"
        "  const ok = r.status >= 200 && r.status < 300;\n"
        "  let msg = ''; try { msg = (await r.json()).detail || r.statusText; } catch(_) { msg = r.statusText; }\n"
        "  return {ok, msg};\n"
        "}\n"
        "document.querySelectorAll('.btn-unload').forEach(b => b.addEventListener('click', async () => {\n"
        "  b.disabled = true; const slug = b.dataset.slug; const entity_type = b.dataset.etype || 'skill';\n"
        "  const r = await post('/api/unload', {slug, entity_type});\n"
        "  if (r.ok) location.reload(); else { b.disabled = false; alert('unload failed: ' + r.msg); }\n"
        "}));\n"
        "document.querySelectorAll('.btn-load').forEach(b => b.addEventListener('click', async () => {\n"
        "  b.disabled = true; const slug = b.dataset.slug; const entity_type = b.dataset.etype || 'skill';\n"
        "  const payload = {slug, entity_type};\n"
        "  if (b.dataset.command) payload.command = b.dataset.command;\n"
        "  if (b.dataset.jsonConfig) payload.json_config = b.dataset.jsonConfig;\n"
        "  const r = await post('/api/load', payload);\n"
        "  if (r.ok) location.reload(); else { b.disabled = false; alert('load failed: ' + r.msg); }\n"
        "}));\n"
        "document.getElementById('load-form').addEventListener('submit', async (ev) => {\n"
        "  ev.preventDefault();\n"
        "  const slug = document.getElementById('load-input').value.trim();\n"
        "  const entity_type = document.getElementById('load-type').value;\n"
        "  if (!slug) return;\n"
        "  document.getElementById('load-msg').textContent = 'loading…';\n"
        "  const r = await post('/api/load', {slug, entity_type});\n"
        "  document.getElementById('load-msg').textContent = r.ok ? 'ok — reloading' : ('failed: ' + r.msg);\n"
        "  if (r.ok) setTimeout(() => location.reload(), 400);\n"
        "});\n"
        "</script>"
    )
    return layout("Loaded", body)
