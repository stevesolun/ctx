"""Config page renderer for ctx-monitor."""

from __future__ import annotations

import html
import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any

LayoutFn = Callable[[str, str], str]
ConfigValueFn = Callable[[dict[str, Any], str, Any], Any]


def render_config(
    *,
    payload: dict[str, Any],
    specs: Iterable[dict[str, Any]],
    monitor_token: str,
    layout: LayoutFn,
    config_value: ConfigValueFn,
    config_remove: object,
) -> str:
    """Render editable ctx config controls from the effective config payload."""
    effective = payload["effective"]
    user = payload["user"]
    rows_by_group: dict[str, list[str]] = defaultdict(list)
    for spec in specs:
        path = spec["path"]
        value = config_value(effective, path, "")
        default = config_value(payload["defaults"], path, "")
        user_value = config_value(user, path, config_remove)
        is_override = user_value is not config_remove
        required = bool(spec.get("required"))
        req_html = " <span class='pill grade-A'>Required</span>" if required else ""
        help_text = html.escape(str(spec.get("help", "")))
        control_value = "true" if value is True else "false" if value is False else str(value)
        default_html = html.escape(json.dumps(default) if not isinstance(default, str) else default)
        example_value = spec.get("example")
        example_html = html.escape(
            json.dumps(example_value) if not isinstance(example_value, str) else str(example_value)
        )
        common_attrs = (
            f"name='{html.escape(path)}' data-config-path='{html.escape(path)}' "
            f"data-original-value='{html.escape(control_value)}' "
            f"data-default='{default_html}' {'required' if required else ''}"
        )
        if spec.get("type") == "choice":
            options = "".join(
                f"<option value='{html.escape(str(choice))}' {'selected' if str(value) == str(choice) else ''}>"
                f"{html.escape(str(choice))}</option>"
                for choice in spec.get("choices", ())
            )
            control = f"<select {common_attrs}>{options}</select>"
        elif spec.get("type") == "bool":
            control = (
                f"<select {common_attrs}>"
                f"<option value='true' {'selected' if bool(value) else ''}>true</option>"
                f"<option value='false' {'selected' if not bool(value) else ''}>false</option>"
                "</select>"
            )
        elif spec.get("type") in {"int", "float"}:
            step = spec.get("step", 1 if spec.get("type") == "int" else 0.01)
            control = (
                f"<input type='number' {common_attrs} "
                f"min='{html.escape(str(spec.get('min', '')))}' "
                f"max='{html.escape(str(spec.get('max', '')))}' "
                f"step='{html.escape(str(step))}' "
                f"value='{html.escape(str(value))}' placeholder='{default_html}'>"
            )
        else:
            control = (
                f"<input type='text' {common_attrs} "
                f"value='{html.escape(str(value))}' placeholder='{default_html}'>"
            )
        override_html = (
            "<span class='pill grade-B'>override</span>"
            if is_override
            else "<span class='muted'>default</span>"
        )
        clear_html = (
            "<label style='display:inline-flex; align-items:center; gap:0.35rem; "
            "margin-top:0.45rem;'>"
            f"<input type='checkbox' data-config-clear='{html.escape(path)}'>"
            "remove user override on save</label>"
            if is_override
            else ""
        )
        rows_by_group[str(spec["group"])].append(
            "<div class='card' style='margin:0 0 0.75rem 0;'>"
            f"<label><strong>{html.escape(str(spec['label']))}</strong>{req_html}<br>"
            f"<code>{html.escape(path)}</code></label>"
            f"<div style='margin-top:0.45rem;'>{control}</div>"
            f"{clear_html}"
            f"<p class='muted' style='margin-bottom:0;'>{help_text}<br>"
            f"Default: <code>{default_html}</code> &middot; Example: <code>{example_html}</code> &middot; "
            f"{override_html}</p>"
            "</div>"
        )
    group_html = "".join(
        "<section style='margin-bottom:1rem;'>"
        f"<h2>{html.escape(group)}</h2>" + "".join(rows) + "</section>"
        for group, rows in rows_by_group.items()
    )
    body = (
        "<h1>Config</h1>"
        "<p class='muted'>Edit ctx runtime defaults from the dashboard. Saves only changed fields. "
        "For existing overrides, use remove user override to fall back to the shipped default. "
        "Important fields are marked Required.</p>"
        f"<p class='muted'>User config: <code>{html.escape(payload['path'])}</code></p>"
        "<form id='config-form'>"
        + group_html
        + "<div class='card' style='position:sticky; bottom:0; background:rgba(255,255,255,0.96);'>"
        "<button type='submit'>save config</button> "
        "<button type='button' id='config-reset'>reset form to effective values</button> "
        "<span id='config-msg' class='muted'></span>"
        "</div></form>"
        "<script>\n"
        f"const CTX_MONITOR_TOKEN = {json.dumps(monitor_token)};\n"
        "const form = document.getElementById('config-form');\n"
        "const msg = document.getElementById('config-msg');\n"
        "form.addEventListener('submit', async (ev) => {\n"
        "  ev.preventDefault();\n"
        "  const updates = {};\n"
        "  const clears = new Set(Array.from(form.querySelectorAll('[data-config-clear]:checked')).map(el => el.dataset.configClear));\n"
        "  form.querySelectorAll('[data-config-path]').forEach(el => {\n"
        "    const path = el.dataset.configPath;\n"
        "    if (clears.has(path)) updates[path] = '';\n"
        "    else if (String(el.value) !== String(el.dataset.originalValue || '')) updates[path] = el.value;\n"
        "  });\n"
        "  if (Object.keys(updates).length === 0) { msg.textContent = 'no config changes to save'; return; }\n"
        "  msg.textContent = 'saving...';\n"
        "  const r = await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json', 'X-CTX-Monitor-Token':CTX_MONITOR_TOKEN}, body: JSON.stringify({updates})});\n"
        "  let body = {}; try { body = await r.json(); } catch (_) {}\n"
        "  msg.textContent = r.ok && body.ok ? body.detail : ('failed: ' + (body.detail || r.statusText));\n"
        "});\n"
        "document.getElementById('config-reset').addEventListener('click', () => location.reload());\n"
        "</script>"
    )
    return layout("Config", body)
