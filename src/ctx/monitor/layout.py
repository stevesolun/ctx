"""Shared HTML shell for the ctx monitor dashboard."""

from __future__ import annotations

import html
import json
from importlib.resources import files


NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("home", "Home", "/"),
    ("loaded", "Loaded", "/loaded"),
    ("skills", "Skills", "/skills"),
    ("skillspector", "SkillSpector", "/skillspector"),
    ("wiki", "Wiki", "/wiki"),
    ("graph", "Graph", "/graph"),
    ("manage", "Manage", "/manage"),
    ("harness", "Harness Setup", "/harness"),
    ("docs", "Docs", "/docs"),
    ("config", "Config", "/config"),
    ("status", "Status", "/status"),
    ("kpi", "KPIs", "/kpi"),
    ("runtime", "Runtime", "/runtime"),
    ("sessions", "Sessions", "/sessions"),
    ("logs", "Logs", "/logs"),
    ("events", "Live", "/events"),
)


def monitor_asset_text(name: str) -> str:
    """Read a packaged dashboard asset."""
    return files("ctx").joinpath("assets", name).read_text(encoding="utf-8")


def monitor_inline_script(name: str) -> str:
    return f"<script>\n{monitor_asset_text(name).rstrip()}\n</script>"


_CSS = monitor_asset_text("monitor.css")


def layout(title: str, body: str) -> str:
    """Wrap body HTML in the standard page chrome."""
    nav_html = "".join(
        f"<a href='{html.escape(href)}' data-nav-key='{html.escape(key)}' "
        "draggable='true' title='Drag to reorder dashboard tabs'>"
        f"{html.escape(label)}</a>"
        for key, label, href in NAV_ITEMS
    )
    nav_default_keys = html.escape(
        json.dumps([key for key, _label, _href in NAV_ITEMS]),
        quote=True,
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} — ctx monitor</title>"
        f"<style>{_CSS}</style></head><body>"
        "<div class='app-shell'>"
        "<header class='app-header'>"
        "<a class='app-brand' href='/' aria-label='ctx monitor home'>"
        "<span class='app-brand-mark'>ctx</span><span>monitor</span></a>"
        "<div class='app-header-meta'>local graph, wiki, skills, agents, MCPs, and harnesses</div>"
        "</header>"
        "<div class='nav' id='dashboard-nav' "
        "data-nav-storage-key='ctx-monitor-nav-order' "
        f"data-nav-default-keys='{nav_default_keys}' "
        "aria-label='Dashboard navigation'>"
        + nav_html
        + "<button type='button' id='nav-reset' class='nav-reset' "
          "title='Reset dashboard tab order'>reset</button>"
        "</div>"
        + monitor_inline_script("monitor-nav.js")
        + "<main class='app-main'>"
        + body
        + "</main></div></body></html>"
    )
