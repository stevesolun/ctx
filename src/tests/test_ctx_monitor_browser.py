"""Browser-driven security coverage for ctx-monitor."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import networkx as nx
import pytest

from ctx import api as ctx_api
from ctx.monitor import testing as mt
from ctx.monitor.services import kpi as kpi_service
from ctx.monitor.services import sidecars as sidecar_service

playwright_sync: Any = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.browser


@dataclass
class MonitorHarness:
    base_url: str
    port: int
    calls: list[tuple[str, str]]
    server: Any
    thread: threading.Thread

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture()
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    claude = tmp_path / ".claude"
    (claude / "skill-quality").mkdir(parents=True)
    monkeypatch.setattr(mt, "claude_dir", lambda: claude)
    monkeypatch.setattr(mt, "dashboard_graph_index_archives", lambda: [])
    sidecar_service.reset_caches()
    kpi_service.reset_cache()
    return claude


@pytest.fixture()
def page() -> Iterator[Any]:
    with playwright_sync.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("CI"):
                pytest.fail(f"Playwright Chromium is not available in CI: {exc}")
            pytest.skip(f"Playwright Chromium is not available: {exc}")
        try:
            page = browser.new_page()
            yield page
        finally:
            browser.close()


def _start_monitor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_load: bool,
) -> MonitorHarness:
    monkeypatch.setattr(mt, "MONITOR_TOKEN", "browser-token")
    calls: list[tuple[str, str]] = []
    if fake_load:

        def perform_load(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
            calls.append((slug, entity_type))
            return True, "loaded"

        monkeypatch.setattr(mt, "perform_load", perform_load)

    server = mt.make_monitor_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = int(server.server_port)
    return MonitorHarness(
        base_url=f"http://127.0.0.1:{port}",
        port=port,
        calls=calls,
        server=server,
        thread=thread,
    )


def _write_wiki_entity(root: Path, entity_type: str, slug: str, body: str) -> None:
    sub = {
        "skill": "skills",
        "agent": "agents",
        "mcp-server": "mcp-servers/g",
        "harness": "harnesses",
    }[entity_type]
    path = root / "skill-wiki" / "entities" / sub / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _write_quality_sidecar(root: Path, slug: str, body: dict[str, Any]) -> None:
    path = root / "skill-quality" / f"{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def _write_runtime_events(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _wait_for_browser_state(page: Any, expression: str, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.evaluate(expression):
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for browser state: {expression}")


def _computed_contrast_ratio(page: Any, selector: str) -> float:
    return float(
        page.locator(selector).first.evaluate(
            """
            node => {
              const channels = value => {
                const parts = value.match(/[0-9.]+/g) || [];
                return parts.slice(0, 3).map(Number);
              };
              const luminance = value => {
                const rgb = channels(value).map(channel => {
                  const normalized = channel / 255;
                  return normalized <= 0.04045
                    ? normalized / 12.92
                    : Math.pow((normalized + 0.055) / 1.055, 2.4);
                });
                return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2];
              };
              const style = getComputedStyle(node);
              const foreground = luminance(style.color);
              const background = luminance(style.backgroundColor);
              return (Math.max(foreground, background) + 0.05)
                / (Math.min(foreground, background) + 0.05);
            }
            """
        )
    )


def _assert_page_fits_viewport(page: Any) -> None:
    dimensions = page.evaluate(
        """
        () => ({
          viewport: document.documentElement.clientWidth,
          document: document.documentElement.scrollWidth,
        })
        """
    )
    assert dimensions["document"] <= dimensions["viewport"]


def test_graph_page_uses_builtin_svg_renderer(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    monkeypatch.setattr(mt, "graph_match_default_min_percent", lambda: 3)
    G = nx.Graph()
    G.add_node("skill:python-patterns", label="python-patterns", type="skill", tags=["python"])
    G.add_node(
        "agent:code-reviewer",
        label="code-reviewer",
        type="agent",
        tags=["review"],
        quality_score=18.0,
        usage_score=0.8,
    )
    G.add_node(
        "skill:weak-graph-link",
        label="weak-graph-link",
        type="skill",
        tags=["noise"],
        quality_score=0.2,
    )
    G.add_node(
        "skill:medium-graph-link",
        label="medium-graph-link",
        type="skill",
        tags=["noise"],
        quality_score=0.2,
    )
    G.add_node(
        "mcp-server:github-mcp-server",
        label="github-mcp-server",
        type="mcp-server",
        tags=["github"],
        quality_score=0.1,
        usage_score=0.0,
    )
    G.add_node("harness:langgraph", label="langgraph", type="harness", tags=["agent"])
    G.add_edge(
        "skill:python-patterns",
        "agent:code-reviewer",
        weight=0.9,
        shared_tags=["review"],
        tag_sim=0.3333,
    )
    G.add_edge(
        "skill:python-patterns", "mcp-server:github-mcp-server", weight=0.8, shared_tags=["github"]
    )
    G.add_edge("skill:python-patterns", "harness:langgraph", weight=0.7, shared_tags=["agent"])
    G.add_edge("skill:python-patterns", "skill:medium-graph-link", weight=0.43, tag_sim=0.0)
    G.add_edge("agent:code-reviewer", "skill:weak-graph-link", weight=0.05)
    monkeypatch.setattr(mt, "load_dashboard_graph", lambda: G)
    _write_wiki_entity(fake_claude, "skill", "python-patterns", "# python-patterns\n")
    _write_wiki_entity(fake_claude, "agent", "code-reviewer", "# code-reviewer\n")

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/graph?slug=python-patterns&type=skill")
        page.wait_for_selector("[data-testid='graph-renderer']", timeout=5000)
        assert "5 nodes" in page.locator("#msg").inner_text()
        assert page.locator("[data-testid='graph-svg-node']").count() == 5
        assert page.locator("[data-testid='graph-fallback-node']").count() == 0
        assert page.locator("[data-testid='graph-list']").evaluate("node => node.hidden")
        assert "Graph renderer unavailable" not in page.locator("#cy").inner_text()
        resize_handle = page.locator("[data-testid='graph-inspector-resize']")
        assert resize_handle.count() == 1
        assert (
            page.locator("[data-testid='graph-node-detail']").evaluate(
                "node => getComputedStyle(node).overflowY",
            )
            == "auto"
        )
        assert (
            page.locator("[data-testid='graph-edge-detail']").evaluate(
                "node => node.parentElement?.getAttribute('data-testid')",
            )
            == "graph-node-detail"
        )
        before_resize = page.locator(".graph-inspector-grid").bounding_box()
        assert before_resize is not None
        node_detail_box = page.locator("[data-testid='graph-node-detail']").bounding_box()
        assert node_detail_box is not None
        grid_padding = page.locator(".graph-inspector-grid").evaluate(
            "node => parseFloat(getComputedStyle(node).paddingLeft) + parseFloat(getComputedStyle(node).paddingRight)",
        )
        assert abs(node_detail_box["width"] - (before_resize["width"] - grid_padding)) < 4
        resize_handle.focus()
        page.keyboard.press("ArrowUp")
        _wait_for_browser_state(
            page,
            f"() => document.querySelector('.graph-inspector-grid')"
            f"?.getBoundingClientRect().height > {before_resize['height'] + 10}",
            timeout=5.0,
        )
        after_resize = page.locator(".graph-inspector-grid").bounding_box()
        assert after_resize is not None
        assert after_resize["height"] > before_resize["height"]
        skill_shape = page.locator(
            "[data-3d-node-id='skill:python-patterns'] [data-testid='graph-svg-node']",
        )
        agent_shape = page.locator(
            "[data-3d-node-id='agent:code-reviewer'] [data-testid='graph-svg-node']",
        )
        mcp_shape = page.locator(
            "[data-3d-node-id='mcp-server:github-mcp-server'] [data-testid='graph-svg-node']",
        )
        assert skill_shape.evaluate("node => node.tagName.toLowerCase()") == "circle"
        assert agent_shape.evaluate("node => node.tagName.toLowerCase()") == "polygon"
        assert mcp_shape.evaluate("node => node.tagName.toLowerCase()") == "rect"
        assert skill_shape.get_attribute("data-node-shape") == "skill"
        assert agent_shape.get_attribute("data-node-shape") == "agent"
        assert mcp_shape.get_attribute("data-node-shape") == "mcp-server"

        assert page.locator("[data-testid='match-range-control']").count() == 1
        assert page.locator("#match-histogram .graph-match-bar").count() == 10
        assert page.locator("#match-filter-min").get_attribute("max") == "100"
        assert page.locator("#match-filter-max").get_attribute("max") == "100"
        assert page.locator("#match-filter-min").input_value() == "3"
        assert page.locator("#match-filter-min-value").inner_text() == "3%"
        assert page.locator("#match-filter-max").input_value() == "100"
        assert page.locator("#match-filter-max-value").inner_text() == "100%"
        page.locator("#match-filter-min").evaluate(
            "node => { node.value = '50'; node.dispatchEvent(new Event('input', {bubbles: true})); }",
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('match-filter-min-value').textContent === '50%'",
            timeout=5.0,
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-match-count').textContent === '4 visible'",
            timeout=5.0,
        )
        assert page.locator("#match-histogram .graph-match-bar.active").count() >= 1
        page.locator("#match-histogram [data-match-bin-min='70']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('match-filter-min-value').textContent === '70%'",
            timeout=5.0,
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('match-filter-max-value').textContent === '79%'",
            timeout=5.0,
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-match-count').textContent === '2 visible'",
            timeout=5.0,
        )
        page.locator("#match-filter-min").evaluate(
            "node => { node.value = '50'; node.dispatchEvent(new Event('input', {bubbles: true})); }",
        )
        page.locator("#match-filter-max").evaluate(
            "node => { node.value = '100'; node.dispatchEvent(new Event('input', {bubbles: true})); }",
        )
        assert (
            page.locator("[data-3d-node-id='skill:medium-graph-link']").evaluate(
                "node => getComputedStyle(node).display",
            )
            == "none"
        )
        assert (
            page.locator("[data-testid='graph-svg-edge'][data-edge-weight='0.4300']").evaluate(
                "node => getComputedStyle(node).display",
            )
            == "none"
        )
        page.locator("#match-filter-max").evaluate(
            "node => { node.value = '80'; node.dispatchEvent(new Event('input', {bubbles: true})); }",
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('match-filter-max-value').textContent === '80%'",
            timeout=5.0,
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-match-count').textContent === '3 visible'",
            timeout=5.0,
        )
        assert (
            page.locator("[data-3d-node-id='agent:code-reviewer']").evaluate(
                "node => getComputedStyle(node).display",
            )
            == "none"
        )
        assert (
            page.locator("[data-testid='graph-svg-edge'][data-edge-weight='0.9000']").evaluate(
                "node => getComputedStyle(node).display",
            )
            == "none"
        )
        page.locator("#match-filter-min").evaluate(
            "node => { node.value = '0'; node.dispatchEvent(new Event('input', {bubbles: true})); }",
        )
        page.locator("#match-filter-max").evaluate(
            "node => { node.value = '100'; node.dispatchEvent(new Event('input', {bubbles: true})); }",
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-match-count').textContent === '5 visible'",
            timeout=5.0,
        )

        agent_filter = page.locator(".graph-type-filter[value='agent']")
        agent_filter.uncheck()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-count-agent').textContent === '0'",
            timeout=5.0,
        )
        assert (
            page.locator("[data-3d-node-id='agent:code-reviewer']").evaluate(
                "node => getComputedStyle(node).display",
            )
            == "none"
        )
        assert (
            page.locator(
                "[data-testid='graph-svg-edge'][data-edge-weight='0.9000']",
            ).evaluate("node => getComputedStyle(node).display")
            == "none"
        )
        agent_filter.check()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-match-count').textContent === '5 visible'",
            timeout=5.0,
        )

        strongest_edge = page.locator(
            "[data-testid='graph-3d-edge'][data-edge-weight='0.9000']",
        )
        strongest_edge.hover(force=True)
        _wait_for_browser_state(
            page,
            "() => document.querySelector('[data-testid=\"graph-edge-detail\"]')"
            "?.textContent.includes('90% relation strength')",
            timeout=5.0,
        )
        edge_detail = page.locator("[data-testid='graph-edge-detail']").inner_text()
        assert "shared: review" in edge_detail

        reviewer_radius = float(
            page.locator(
                "[data-3d-node-id='agent:code-reviewer'] [data-testid='graph-svg-node']",
            ).get_attribute("data-radius")
            or "0"
        )
        mcp_radius = float(
            page.locator(
                "[data-3d-node-id='mcp-server:github-mcp-server'] [data-testid='graph-svg-node']",
            ).get_attribute("data-radius")
            or "0"
        )
        assert reviewer_radius > mcp_radius

        center_node = page.locator(
            "[data-3d-node-id='skill:python-patterns'] [data-testid='graph-svg-node']",
        )
        center_node.click()
        _wait_for_browser_state(
            page,
            "() => document.querySelector('[data-testid=\"graph-node-detail-tree\"]')"
            "?.innerText.includes('python-patterns')",
            timeout=5.0,
        )
        center_detail_text = page.locator("[data-testid='graph-node-detail-tree']").inner_text()
        assert "medium-graph-link" in center_detail_text
        assert "match 43%" in center_detail_text
        assert "graph-only links hidden" not in center_detail_text
        assert "tag 0.000" not in center_detail_text
        assert "evidence: none" not in center_detail_text

        graph_node = page.locator(
            "[data-3d-node-id='agent:code-reviewer'] [data-testid='graph-svg-node']",
        )
        graph_node.click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('focus').value === 'code-reviewer'",
            timeout=5.0,
        )
        _wait_for_browser_state(
            page,
            "() => document.querySelector('[data-3d-node-id=\"agent:code-reviewer\"]')"
            "?.getAttribute('data-depth') === '0'",
            timeout=5.0,
        )
        detail_text = page.locator("[data-testid='graph-node-detail-tree']").inner_text()
        assert "Neighbors" in detail_text
        assert "strength 90% relation strength" in detail_text
        assert "tag 33%" in detail_text
        assert "quality: 100%" in detail_text
        assert "raw score clamped" not in detail_text
        assert "graph-only links hidden" not in detail_text
        assert "weak-graph-link" in detail_text
        assert "match 5%" in detail_text
        assert "tag 0.000" not in detail_text
        assert "evidence: none" not in detail_text
        assert "0.333" not in detail_text
        assert page.locator("[data-3d-node-id='agent:code-reviewer']").evaluate(
            "node => node.classList.contains('graph-node-selected')",
        )
        selected_fill = page.locator(
            "[data-3d-node-id='agent:code-reviewer'] [data-testid='graph-svg-node']",
        ).evaluate("node => getComputedStyle(node).fill")
        assert selected_fill == "rgb(250, 204, 21)"
        selected_visible_edges = page.locator(
            "[data-testid='graph-svg-edge'].graph-edge-selected",
        ).count()
        selected_hit_edges = page.locator(
            "[data-testid='graph-3d-edge'].graph-edge-selected",
        ).count()
        assert selected_visible_edges >= 1
        assert selected_hit_edges == 0
        _wait_for_browser_state(
            page,
            "() => document.getElementById('focus').value === 'code-reviewer'",
            timeout=5.0,
        )
        _wait_for_browser_state(
            page,
            "() => document.querySelector('[data-3d-node-id=\"agent:code-reviewer\"]')"
            "?.getAttribute('data-depth') === '0'",
            timeout=5.0,
        )
        page.locator("[data-3d-node-id='agent:code-reviewer']").dblclick()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('focus').value === 'python-patterns'",
            timeout=5.0,
        )

        page.select_option("#focus-type", "agent")
        page.fill("#focus", "code")
        page.wait_for_selector(
            "[data-testid='graph-live-results'] [data-live-slug='code-reviewer']", timeout=5000
        )
        page.locator("[data-live-slug='code-reviewer']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('focus').value === 'code-reviewer'",
            timeout=5.0,
        )

        page.fill("#tag-filter", "review")
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-match-count').textContent === '2 visible'",
            timeout=5.0,
        )
        page.locator(
            "[data-testid='graph-node-detail-tree'] a[href='/wiki/code-reviewer?type=agent']"
        ).click()
        page.wait_for_url("**/wiki/code-reviewer?type=agent", timeout=5000)
        assert "code-reviewer" in page.locator("h1").inner_text()
    finally:
        harness.close()


def test_navigation_drag_order_persists_and_keyboard_reset_restores_defaults(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(harness.base_url)
        page.wait_for_selector("#dashboard-nav", timeout=5000)
        default_order = page.locator("#dashboard-nav a[data-nav-key]").evaluate_all(
            "links => links.map(link => link.dataset.navKey)",
        )
        assert default_order[0] == "home"

        graph_link = page.locator("#dashboard-nav a[data-nav-key='graph']")
        home_link = page.locator("#dashboard-nav a[data-nav-key='home']")
        home_box = home_link.bounding_box()
        assert home_box is not None
        graph_link.drag_to(
            home_link,
            target_position={"x": 1, "y": home_box["height"] / 2},
        )
        _wait_for_browser_state(
            page,
            "() => document.querySelector('#dashboard-nav a[data-nav-key]')?.dataset.navKey === 'graph'",
        )
        stored_order = page.evaluate(
            "() => JSON.parse(localStorage.getItem('ctx-monitor-nav-order') || '[]')",
        )
        assert stored_order[0] == "graph"
        assert sorted(stored_order) == sorted(default_order)

        page.reload()
        page.wait_for_selector("#dashboard-nav", timeout=5000)
        assert (
            page.locator("#dashboard-nav a[data-nav-key]").first.get_attribute(
                "data-nav-key",
            )
            == "graph"
        )

        reset = page.locator("#nav-reset")
        reset.focus()
        assert page.evaluate("() => document.activeElement?.id") == "nav-reset"
        page.keyboard.press("Enter")
        restored_order = page.locator("#dashboard-nav a[data-nav-key]").evaluate_all(
            "links => links.map(link => link.dataset.navKey)",
        )
        assert restored_order == default_order
        assert (
            page.evaluate(
                "() => localStorage.getItem('ctx-monitor-nav-order')",
            )
            is None
        )

        docs_link = page.locator("#dashboard-nav a[data-nav-key='docs']")
        docs_link.focus()
        page.keyboard.press("Enter")
        page.wait_for_url("**/docs", timeout=5000)
        assert page.locator("h1").first.inner_text()
    finally:
        harness.close()


def test_dark_dashboard_surfaces_have_contrast_without_browser_errors(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    graph = nx.Graph()
    graph.add_node("skill:python-patterns", label="python-patterns", type="skill")
    graph.add_node("agent:code-reviewer", label="code-reviewer", type="agent")
    graph.add_edge(
        "skill:python-patterns",
        "agent:code-reviewer",
        weight=0.9,
        shared_tags=["review"],
    )
    monkeypatch.setattr(mt, "load_dashboard_graph", lambda: graph)
    _write_wiki_entity(fake_claude, "skill", "python-patterns", "# Python patterns\n")
    page.emulate_media(color_scheme="dark")
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/config")
        page.wait_for_selector("#config-form", timeout=5000)
        assert _computed_contrast_ratio(page, ".card") >= 4.5
        assert _computed_contrast_ratio(page, "div.card[style*='position:sticky']") >= 4.5
        _assert_page_fits_viewport(page)

        page.goto(f"{harness.base_url}/graph?slug=python-patterns&type=skill")
        page.wait_for_selector("[data-testid='graph-edge-detail']", timeout=5000)
        assert _computed_contrast_ratio(page, ".graph-edge-detail-inline") >= 4.5
        _assert_page_fits_viewport(page)
        assert console_errors == []
        assert page_errors == []
    finally:
        harness.close()


def test_mobile_home_activity_and_wiki_keep_long_content_inside_viewport(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    long_slug = "mobile-" + "entity" * 18
    long_session = "session-" + "identifier" * 16
    _write_wiki_entity(
        fake_claude,
        "skill",
        long_slug,
        "---\n"
        "type: skill\n"
        f"description: {'metadata' * 28}\n"
        "tags: [mobile, responsive]\n"
        "---\n# Mobile entity\n",
    )
    (fake_claude / "ctx-audit.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-09-30T10:00:00Z",
                "event": "skill.loaded",
                "subject": long_slug,
                "subject_type": "skill",
                "session_id": long_session,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (fake_claude / "skill-events.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-09-30T10:00:01Z",
                "event": "load",
                "skill": long_slug,
                "session_id": long_session,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    page.set_viewport_size({"width": 390, "height": 844})

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        for route, selector in (
            ("/", ".app-main"),
            (f"/session/{long_session}", "h1"),
            ("/wiki", ".wiki-card"),
        ):
            page.goto(f"{harness.base_url}{route}")
            page.wait_for_selector(selector, timeout=5000)
            _assert_page_fits_viewport(page)

        search_box = page.locator("#wiki-search").bounding_box()
        first_card = page.locator(".wiki-card").first.bounding_box()
        assert search_box is not None and first_card is not None
        viewport_width = page.evaluate("() => document.documentElement.clientWidth")
        assert search_box["x"] >= 0
        assert search_box["x"] + search_box["width"] <= viewport_width
        assert first_card["x"] >= 0
        assert first_card["x"] + first_card["width"] <= viewport_width
    finally:
        harness.close()


def test_docs_page_search_jumps_to_cross_tab_result(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    del fake_claude
    entries = [
        {
            "title": "Install Guide",
            "path": "docs/install.md",
            "summary": "Install ctx locally.",
            "body": "# Install Guide\n\n## Setup\n\nInstall ctx locally.\n",
        },
        {
            "title": "Graph Guide",
            "path": "graph/README.md",
            "summary": "Runtime graph reference.",
            "body": "# Graph Guide\n\n## Runtime Graph\n\nSearch the runtime graph.\n",
        },
    ]
    monkeypatch.setattr(mt, "docs_index_entries", lambda: entries)
    monkeypatch.setattr(
        mt,
        "docs_tabs",
        lambda _entries: [
            {"label": "Home", "slug": "home", "pages": [entries[0]]},
            {"label": "Repo", "slug": "repo", "pages": [entries[1]]},
        ],
    )

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/docs")
        page.wait_for_selector("#docs-search", timeout=5000)
        assert page.locator(".docs-tab-button.active").inner_text() == "Home"

        page.fill("#docs-search", "runtime graph")
        page.wait_for_selector(".docs-search-result", timeout=5000)
        assert "Graph Guide" in page.locator(".docs-search-result").first.inner_text()
        page.locator(".docs-search-result").first.click()

        _wait_for_browser_state(
            page,
            "() => document.querySelector('.docs-tab-button.active')?.dataset.docTab === 'repo'",
            timeout=5.0,
        )
        assert "runtime-graph" in page.evaluate("() => location.hash")
        assert page.locator("[data-doc-panel='repo']").evaluate("node => !node.hidden")
        assert not page.locator("[data-doc-panel='home']").evaluate("node => !node.hidden")
    finally:
        harness.close()


def test_wiki_page_autocomplete_and_type_filters_update_visible_tiles(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    _write_wiki_entity(
        fake_claude,
        "skill",
        "python-patterns",
        "---\ntype: skill\ndescription: Python patterns\ntags: [python]\n---\n# body\n",
    )
    _write_wiki_entity(
        fake_claude,
        "agent",
        "code-reviewer",
        "---\ntype: agent\ndescription: Review code\ntags: [review]\n---\n# body\n",
    )

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/wiki")
        page.wait_for_selector("#wiki-search", timeout=5000)
        suggestions = page.locator("#wiki-entity-suggestions option")
        assert suggestions.count() == 2
        suggestion_values = suggestions.evaluate_all(
            "options => options.map(option => option.getAttribute('value'))",
        )
        assert "code-reviewer" in suggestion_values

        page.fill("#wiki-search", "review")
        _wait_for_browser_state(
            page,
            "() => document.getElementById('wiki-match-count').textContent === '1 of 2 match'",
            timeout=5.0,
        )
        assert page.locator(".wiki-card:visible").count() == 1
        assert "code-reviewer" in page.locator(".wiki-card:visible").inner_text()

        page.locator(".wiki-type-filter[value='agent']").uncheck()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('wiki-match-count').textContent === '0 of 2 match'",
            timeout=5.0,
        )
        assert page.locator(".wiki-card:visible").count() == 0
    finally:
        harness.close()


def test_skills_page_filters_and_paginates_real_sidecars(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    for index in range(50):
        _write_quality_sidecar(
            fake_claude,
            f"skill-{index:03d}",
            {
                "slug": f"skill-{index:03d}",
                "subject_type": "skill",
                "grade": "A",
                "raw_score": 0.9 - index / 1000,
            },
        )
    for index in range(5):
        _write_quality_sidecar(
            fake_claude,
            f"agent-{index:03d}",
            {
                "slug": f"agent-{index:03d}",
                "subject_type": "agent",
                "grade": "B",
                "raw_score": 0.8 - index / 1000,
            },
        )

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/skills?limit=50")
        page.wait_for_selector(".skill-card", timeout=5000)
        assert page.locator(".skill-card").count() == 50
        assert "Showing 1-50 of 55 sidecars" in page.locator("#match-count").inner_text()

        page.get_by_role("link", name="next").click()
        page.wait_for_url("**page=2**", timeout=5000)
        assert page.locator(".skill-card").count() == 5
        assert "Showing 51-55 of 55 sidecars" in page.locator("#match-count").inner_text()
        assert page.locator(".skill-card[data-slug='skill-049']").count() == 1

        with page.expect_navigation():
            page.select_option(".type-filter", "agent")
        assert page.locator(".skill-card").count() == 5
        assert page.locator(".skill-card[data-type='agent']").count() == 5
        assert "Showing 1-5 of 5 matching sidecars" in page.locator("#match-count").inner_text()

        with page.expect_navigation():
            page.select_option(".grade-filter", "B")
        assert page.locator(".skill-card[data-grade='B']").count() == 5

        page.fill("#skill-search", "agent-003")
        page.locator("#skills-filter-form button[type='submit']").click()
        page.wait_for_url("**q=agent-003**", timeout=5000)
        assert page.locator(".skill-card").count() == 1
        assert page.locator(".skill-card").get_attribute("data-slug") == "agent-003"
    finally:
        harness.close()


def test_wiki_entity_tabs_switch_overview_subgraph_and_quality(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    graph = nx.Graph()
    graph.add_node("skill:python-patterns", label="python-patterns", type="skill")
    graph.add_node("agent:code-reviewer", label="code-reviewer", type="agent")
    graph.add_edge(
        "skill:python-patterns",
        "agent:code-reviewer",
        weight=0.9,
        shared_tags=["review"],
    )
    monkeypatch.setattr(mt, "load_dashboard_graph", lambda: graph)
    _write_wiki_entity(
        fake_claude,
        "skill",
        "python-patterns",
        "---\ntype: skill\ndescription: Durable Python patterns\ntags: [python]\n---\n"
        "# Python patterns\n\nOverview body marker.\n",
    )
    _write_quality_sidecar(
        fake_claude,
        "python-patterns",
        {
            "slug": "python-patterns",
            "subject_type": "skill",
            "grade": "A",
            "raw_score": 0.93,
            "weights": {"documentation": 1.0},
            "signals": {"documentation": {"score": 0.93, "evidence": {"source": "wiki"}}},
        },
    )

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/wiki/python-patterns?type=skill")
        page.wait_for_selector("[data-entity-tab='overview']", timeout=5000)
        assert page.locator("[data-entity-tab-panel='overview']").is_visible()
        assert (
            "Overview body marker"
            in page.locator(
                "[data-entity-tab-panel='overview']",
            ).inner_text()
        )
        assert page.locator("[data-entity-tab-panel='subgraph']").is_hidden()
        assert page.locator("[data-entity-tab-panel='quality']").is_hidden()

        page.locator("[data-entity-tab='subgraph']").click()
        assert page.locator("[data-entity-tab-panel='overview']").is_hidden()
        assert page.locator("[data-entity-tab-panel='subgraph']").is_visible()
        assert (
            "code-reviewer"
            in page.locator(
                "[data-entity-tab-panel='subgraph']",
            ).inner_text()
        )
        assert page.evaluate("() => location.hash") == "#subgraph"

        page.locator("[data-entity-tab='quality']").click()
        assert page.locator("[data-entity-tab-panel='subgraph']").is_hidden()
        assert page.locator("[data-entity-tab-panel='quality']").is_visible()
        quality_text = page.locator("[data-entity-tab-panel='quality']").inner_text()
        assert "score 0.930" in quality_text
        assert "documentation" in quality_text
        assert page.evaluate("() => location.hash") == "#quality"
    finally:
        harness.close()


def test_recommendation_query_selection_and_rejection_update_related_results(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    del fake_claude
    bundle_calls: list[tuple[str, int]] = []
    related_calls: list[tuple[list[str], list[str], int]] = []

    def recommend_bundle(query: str, *, top_k: int) -> list[dict[str, object]]:
        bundle_calls.append((query, top_k))
        return [
            {
                "id": "skill:fastapi-pro",
                "name": "fastapi-pro",
                "type": "skill",
                "normalized_score": 0.91,
                "selection_state": "suggested",
                "tldr": "Build APIs safely.",
                "reason": "matches API work",
            },
            {
                "id": "agent:legacy-reviewer",
                "name": "legacy-reviewer",
                "type": "agent",
                "normalized_score": 0.7,
                "selection_state": "suggested",
                "tldr": "Review legacy code.",
                "reason": "matches review work",
            },
        ]

    def recommend_related(
        selected: list[str],
        *,
        rejected: list[str],
        top_n: int,
    ) -> list[dict[str, object]]:
        related_calls.append((selected, rejected, top_n))
        return [
            {
                "id": "mcp-server:filesystem",
                "name": "filesystem",
                "type": "mcp-server",
                "normalized_score": 0.88,
                "selection_state": "suggested_related",
                "tldr": "Read the workspace.",
                "reason": "supports the selected skill",
            }
        ]

    monkeypatch.setattr(ctx_api, "recommend_bundle", recommend_bundle)
    monkeypatch.setattr(ctx_api, "recommend_related", recommend_related)
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/recommend")
        query_form = page.locator("form[action='/recommend']").first
        query_form.locator("input[name='q']").fill("build api")
        query_form.locator("input[name='top_k']").fill("2")
        query_form.locator("input[name='rejected']").fill("agent:legacy-reviewer")
        query_form.get_by_role("button", name="Recommend").click()
        page.wait_for_url("**q=build+api**", timeout=5000)
        assert bundle_calls == [("build api", 2)]
        assert page.locator("input[value='skill:fastapi-pro']").count() == 1

        page.get_by_role("link", name="Select all").click()
        page.wait_for_load_state("load")
        assert page.locator("input[name='selected']").count() == 2
        assert page.locator("input[name='selected']:checked").count() == 2

        page.get_by_role("link", name="Select none").click()
        page.wait_for_load_state("load")
        assert page.locator("input[name='selected']").count() == 2
        assert page.locator("input[name='selected']:checked").count() == 0
        related_calls.clear()

        page.locator("input[value='skill:fastapi-pro']").check()
        page.get_by_role("button", name="Show related").click()
        page.wait_for_url("**selected=skill%3Afastapi-pro**", timeout=5000)
        assert related_calls == [
            (["skill:fastapi-pro"], ["agent:legacy-reviewer"], 2),
        ]
        assert page.locator("input[value='skill:fastapi-pro']").is_checked()
        assert (
            "mcp-server:filesystem"
            in page.get_by_text("Related recommendations")
            .locator(
                "..",
            )
            .inner_text()
        )
    finally:
        harness.close()


def test_empty_runtime_and_recommendation_outcomes_are_explicit(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    monkeypatch.setattr(
        mt,
        "runtime_lifecycle_path",
        lambda: fake_claude / "runtime" / "events.jsonl",
    )

    def recommend_bundle(query: str, *, top_k: int) -> list[dict[str, object]]:
        del top_k
        if query == "explode":
            raise RuntimeError("controlled recommendation failure")
        return []

    monkeypatch.setattr(ctx_api, "recommend_bundle", recommend_bundle)
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/runtime")
        runtime_text = page.locator("body").inner_text()
        assert "0 validations / 0 failed / 0 open escalations" in runtime_text
        assert "0 loaded / 0 active / 0 selected / 0 used" in runtime_text
        assert "0 records / 0 tokens / $0.0000 cost" in runtime_text
        assert "No tool usage recorded yet." in runtime_text
        assert "No validation checks recorded yet." in runtime_text
        assert "No open escalations." in runtime_text

        page.goto(f"{harness.base_url}/recommend?q=nothing&top_k=3")
        assert page.get_by_text("No recommendations above threshold.").is_visible()

        page.goto(f"{harness.base_url}/recommend?q=explode&top_k=3")
        assert page.get_by_text("Error", exact=True).is_visible()
        assert page.get_by_text(
            "RuntimeError: controlled recommendation failure",
            exact=True,
        ).is_visible()
        assert "Traceback" not in page.locator("body").inner_text()
    finally:
        harness.close()


def test_manage_page_supports_create_search_update_and_delete(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    harness = _start_monitor(monkeypatch, fake_load=False)
    entity_path = fake_claude / "skill-wiki" / "entities" / "agents" / "custom-reviewer.md"
    try:
        page.goto(f"{harness.base_url}/manage")
        page.wait_for_selector("#entity-editor-form", timeout=5000)

        page.fill("input[name='slug']", "custom-reviewer")
        page.select_option("select[name='entity_type']", "agent")
        page.fill("input[name='title']", "Custom Reviewer")
        page.fill("input[name='tags']", "python, review, policy")
        page.fill("input[name='description']", "Reviews Python changes with local policy.")
        page.fill(
            "textarea[name='body']", "# Custom Reviewer\n\nUse before merging Python changes.\n"
        )
        page.locator("#entity-editor-form button[type='submit']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('entity-editor-status').textContent.includes('saved agent:custom-reviewer')",
            timeout=5.0,
        )
        assert entity_path.is_file()

        page.fill("#manage-search", "custom")
        _wait_for_browser_state(
            page,
            "() => document.getElementById('manage-search-status').textContent === '1 result'",
            timeout=5.0,
        )
        page.locator(".manage-result[data-slug='custom-reviewer']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('entity-editor-status').textContent.includes('editing agent:custom-reviewer')",
            timeout=5.0,
        )
        assert page.locator("input[name='title']").input_value() == "Custom Reviewer"

        original_text = entity_path.read_text(encoding="utf-8")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.fill("input[name='title']", "Cancelled Update")
        page.locator("#entity-editor-form button[type='submit']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('entity-editor-status').textContent === 'update cancelled'",
            timeout=5.0,
        )
        assert entity_path.read_text(encoding="utf-8") == original_text

        page.once("dialog", lambda dialog: dialog.accept())
        page.fill("input[name='title']", "Custom Reviewer Updated")
        page.locator("#entity-editor-form button[type='submit']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('entity-editor-status').textContent.includes('saved agent:custom-reviewer')",
            timeout=5.0,
        )
        assert "title: Custom Reviewer Updated" in entity_path.read_text(encoding="utf-8")

        updated_text = entity_path.read_text(encoding="utf-8")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("[data-testid='entity-delete-button']").click()
        assert entity_path.read_text(encoding="utf-8") == updated_text

        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("[data-testid='entity-delete-button']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('entity-editor-status').textContent.includes('deleted agent:custom-reviewer')",
            timeout=5.0,
        )
        assert not entity_path.exists()
    finally:
        harness.close()


def test_config_and_harness_pages_support_browser_wizard_flows(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    _write_wiki_entity(
        fake_claude,
        "harness",
        "langgraph",
        "---\n"
        "title: LangGraph harness\n"
        "type: harness\n"
        "description: Durable Python agent workflows with tool routing.\n"
        "tags: [python, api, local, verification]\n"
        "repo_url: https://github.com/langchain-ai/langgraph\n"
        "---\n"
        "# LangGraph harness\n",
    )
    _write_quality_sidecar(
        fake_claude,
        "langgraph-harness",
        {
            "slug": "langgraph",
            "subject_type": "harness",
            "grade": "A",
            "raw_score": 0.93,
        },
    )

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/config")
        page.wait_for_selector("#config-form", timeout=5000)
        page.fill("input[name='skill_transformer.line_threshold']", "240")
        page.locator("#config-form button[type='submit']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('config-msg').textContent.includes('saved 1 config keys')",
            timeout=5.0,
        )
        config = json.loads((fake_claude / "skill-system-config.json").read_text(encoding="utf-8"))
        assert config["skill_transformer"]["line_threshold"] == 240

        page.goto(f"{harness.base_url}/harness")
        page.wait_for_selector("#harness-wizard-form", timeout=5000)
        page.select_option("select[name='model_provider']", "huggingface")
        page.fill("input[name='model']", "HuggingFaceTB/SmolLM2-135M-Instruct")
        page.fill(
            "textarea[name='goal']",
            "Build a local Python code-review harness with pytest verification.",
        )
        page.check("input[name='tools'][value='browser']")
        page.fill("input[name='verify']", "pytest")
        page.select_option("select[name='privacy']", "secrets allowed by env only")
        _wait_for_browser_state(
            page,
            "() => document.querySelector('[data-testid=\"harness-command-output\"]').textContent.includes('--model-provider \"huggingface\"')",
            timeout=5.0,
        )
        command = page.locator("[data-testid='harness-command-output']").inner_text()
        assert '--model "HuggingFaceTB/SmolLM2-135M-Instruct"' in command
        assert "--plan-on-no-fit" in command
        assert page.locator(".harness-card[data-harness-slug='langgraph']").count() == 1

        isolated_home = fake_claude.parent / "harness-cli-home"
        (isolated_home / ".claude" / "skill-wiki" / "entities" / "harnesses").mkdir(parents=True)
        (isolated_home / ".claude" / "skill-wiki" / "graphify-out").mkdir()
        plan_path = fake_claude.parent / "custom-harness-prd.md"
        argv = shlex.split(command)
        assert argv[:3] == ["python", "-m", "harness_install"]
        argv[0] = sys.executable
        argv.extend(["--plan-output", str(plan_path)])
        secret = "hf_acceptance_secret_must_not_leak"
        env = os.environ.copy()
        env.update({"HOME": str(isolated_home), "HF_TOKEN": secret})
        completed = subprocess.run(
            argv,
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "No harness recommendations matched." in completed.stdout
        assert f"Custom harness plan: {plan_path}" in completed.stdout

        plan = plan_path.read_text(encoding="utf-8")
        assert "Model provider: huggingface" in plan
        assert "Model: HuggingFaceTB/SmolLM2-135M-Instruct" in plan
        assert "Allowed tools/access: files,git,shell,browser" in plan
        assert "Verification: pytest" in plan
        assert "Privacy/network: secrets allowed by env only" in plan
        assert "Preferred ctx attachment: mcp" in plan
        assert secret not in plan
        assert secret not in completed.stdout
        assert secret not in completed.stderr

        page.locator("[data-select-harness='langgraph']").click()
        selected = page.locator("#selected-harness-command").inner_text()
        assert "python -m harness_install langgraph --dry-run" in selected
        assert "ctx-scan-repo --repo . --recommend" in selected
    finally:
        harness.close()


def test_sessions_kpi_and_runtime_pages_render_populated_browser_data(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
    tmp_path: Path,
) -> None:
    (fake_claude / "ctx-audit.jsonl").write_text(
        json.dumps(
            {
                "ts": "2026-06-16T10:00:00Z",
                "event": "skill.loaded",
                "subject": "python-patterns",
                "subject_type": "skill",
                "actor": "hook",
                "session_id": "browser-session",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (fake_claude / "skill-events.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-06-16T10:00:01Z",
                "event": "load",
                "skill": "python-patterns",
                "session_id": "browser-session",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_quality_sidecar(
        fake_claude,
        "alpha",
        {
            "slug": "alpha",
            "subject_type": "skill",
            "grade": "A",
            "raw_score": 0.92,
            "score": 0.92,
            "computed_at": "2026-06-16T10:00:00Z",
        },
    )
    runtime_path = tmp_path / "runtime" / "events.jsonl"
    monkeypatch.setattr(mt, "runtime_lifecycle_path", lambda: runtime_path)
    _write_runtime_events(
        runtime_path,
        [
            {
                "action": "validation",
                "session_id": "browser-session",
                "check_name": "pytest",
                "status": "failed",
                "summary": "one failing test",
                "created_at": "2026-06-16T10:02:00Z",
            },
            {
                "action": "escalation",
                "session_id": "browser-session",
                "trigger": "validation-failed",
                "reason": "pytest failed",
                "status": "open",
                "severity": "blocking",
                "created_at": "2026-06-16T10:03:00Z",
            },
        ],
    )

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/sessions")
        page.wait_for_selector("table", timeout=5000)
        assert "browser-session" in page.locator("body").inner_text()
        assert "1 unique sessions observed" in page.locator("body").inner_text()

        page.goto(f"{harness.base_url}/kpi")
        page.wait_for_selector("h1", timeout=5000)
        kpi_text = page.locator("body").inner_text()
        assert "Total entities: 1" in kpi_text
        assert "Grade distribution" in kpi_text
        assert "A: 1" in kpi_text

        page.goto(f"{harness.base_url}/runtime")
        page.wait_for_selector("h1", timeout=5000)
        runtime_text = page.locator("body").inner_text()
        assert "1 validations / 1 failed / 1 open escalations" in runtime_text
        assert "pytest" in runtime_text
        assert "validation-failed" in runtime_text
    finally:
        harness.close()


def test_events_page_shows_backlog_and_appends_live_events(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    audit_path = fake_claude / "ctx-audit.jsonl"
    audit_path.write_text(
        json.dumps(
            {
                "ts": "2026-04-28T00:00:00Z",
                "event": "skill.loaded",
                "subject": "python-patterns",
                "session_id": "events-page-backlog",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/events")
        page.wait_for_selector("#stream", timeout=5000)
        assert "Showing last 1 audit events" in page.locator("body").inner_text()
        assert "events-page-backlog" in page.locator("#stream").inner_text()

        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": "2026-04-28T00:00:01Z",
                        "event": "agent.loaded",
                        "subject": "repo-reviewer",
                        "session_id": "events-page-live",
                    }
                )
                + "\n"
            )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('stream').textContent.includes('events-page-live')",
            timeout=5.0,
        )
        status_text = page.locator("#stream-status").inner_text()
        assert status_text in {"connected; waiting for new events", "live"}
    finally:
        harness.close()


def test_empty_logs_filter_and_event_stream_reconnect_are_visible(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    audit_path = fake_claude / "ctx-audit.jsonl"
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/logs")
        page.wait_for_selector("#logs", timeout=5000)
        assert "Showing last 0" in page.locator("body").inner_text()
        assert page.locator("#logs tr[data-event]").count() == 0

        audit_path.write_text(
            "\n".join(
                json.dumps(row)
                for row in (
                    {
                        "ts": "2026-09-30T11:00:00Z",
                        "event": "skill.loaded",
                        "subject": "python-patterns",
                        "actor": "hook",
                        "session_id": "filter-session-a",
                    },
                    {
                        "ts": "2026-09-30T11:00:01Z",
                        "event": "agent.loaded",
                        "subject": "code-reviewer",
                        "actor": "hook",
                        "session_id": "filter-session-b",
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
        page.reload()
        page.wait_for_selector("#logs tr[data-event]", timeout=5000)
        assert page.locator("#logs tr[data-event]").count() == 2
        page.fill("#filter", "code-reviewer")
        assert page.locator("#logs tr[data-event]:visible").count() == 1
        assert "agent.loaded" in page.locator("#logs tr[data-event]:visible").inner_text()
        page.fill("#filter", "filter-session-a")
        assert page.locator("#logs tr[data-event]:visible").count() == 1
        assert "python-patterns" in page.locator("#logs tr[data-event]:visible").inner_text()

        audit_path.write_text("", encoding="utf-8")
        attempts = 0

        def interrupt_first_stream(route: Any) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                route.abort()
            else:
                route.continue_()

        page.route("**/api/events.stream", interrupt_first_stream)
        page.goto(f"{harness.base_url}/events")
        page.wait_for_selector("#stream", timeout=5000)
        assert (
            "no audit events recorded yet; waiting for new events"
            in page.locator(
                "#stream",
            ).inner_text()
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('stream-status').textContent === 'stream error; reconnecting'",
            timeout=5.0,
        )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('stream-status').textContent === 'connected; waiting for new events'",
            timeout=8.0,
        )
        assert attempts >= 2

        with audit_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "ts": "2026-09-30T11:00:02Z",
                        "event": "mcp.loaded",
                        "subject": "filesystem",
                        "session_id": "reconnected-session",
                    }
                )
                + "\n"
            )
        _wait_for_browser_state(
            page,
            "() => document.getElementById('stream').textContent.includes('reconnected-session')",
            timeout=5.0,
        )
        assert page.locator("#stream-status").inner_text() == "live"
    finally:
        harness.close()


def test_loaded_page_token_controls_browser_mutations(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    (fake_claude / "skill-manifest.json").write_text(
        json.dumps({"load": [], "unload": [], "warnings": []}),
        encoding="utf-8",
    )
    harness = _start_monitor(monkeypatch, fake_load=True)
    try:
        page.goto(f"{harness.base_url}/loaded")
        page.wait_for_load_state("networkidle")

        missing_token = page.evaluate("""
            async () => {
              const r = await fetch('/api/load', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({slug: 'python-patterns'})
              });
              return {status: r.status, body: await r.json()};
            }
        """)
        assert missing_token["status"] == 403
        assert "token" in missing_token["body"]["detail"]
        assert harness.calls == []

        with_token = page.evaluate("""
            async () => {
              const r = await fetch('/api/load', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  'X-CTX-Monitor-Token': CTX_MONITOR_TOKEN
                },
                body: JSON.stringify({slug: 'python-patterns'})
              });
              return {status: r.status, body: await r.json()};
            }
        """)
        assert with_token == {"status": 200, "body": {"ok": True, "detail": "loaded"}}
        assert harness.calls == [("python-patterns", "skill")]
    finally:
        harness.close()


def test_cross_origin_browser_post_cannot_mutate(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    harness = _start_monitor(monkeypatch, fake_load=True)
    try:
        page.goto("data:text/html,<html><body>cross-origin</body></html>")
        result = page.evaluate(
            """
            async (url) => {
              try {
                await fetch(url, {
                  method: 'POST',
                  headers: {'Content-Type': 'text/plain'},
                  body: JSON.stringify({slug: 'cross-origin'})
                });
              } catch (_) {
                return false;
              }
              return true;
            }
            """,
            f"{harness.base_url}/api/load",
        )
        assert result is False
        assert harness.calls == []
    finally:
        harness.close()


def test_browser_load_rejects_traversal_slug(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/loaded")
        page.wait_for_load_state("networkidle")
        result = page.evaluate("""
            async () => {
              const r = await fetch('/api/load', {
                method: 'POST',
                headers: {
                  'Content-Type': 'application/json',
                  'X-CTX-Monitor-Token': CTX_MONITOR_TOKEN
                },
                body: JSON.stringify({slug: '../secret'})
              });
              return {status: r.status, body: await r.json()};
            }
        """)
        assert result["status"] == 400
        assert "invalid slug" in result["body"]["detail"]
    finally:
        harness.close()


def test_browser_sse_streams_do_not_block_json_requests(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        audit_path = fake_claude / "ctx-audit.jsonl"
        audit_path.write_text("", encoding="utf-8")
        page.goto(f"{harness.base_url}/loaded")
        page.wait_for_load_state("networkidle")
        page.evaluate("""
            () => {
              window.__ctxEvents = [];
              window.__ctxOpenCount = 0;
              window.__ctxSourceA = new EventSource('/api/events.stream');
              window.__ctxSourceB = new EventSource('/api/events.stream');
              window.__ctxSourceA.onopen = () => { window.__ctxOpenCount += 1; };
              window.__ctxSourceB.onopen = () => { window.__ctxOpenCount += 1; };
              window.__ctxSourceA.onmessage = (event) => window.__ctxEvents.push(['a', event.data]);
              window.__ctxSourceB.onmessage = (event) => window.__ctxEvents.push(['b', event.data]);
            }
        """)
        _wait_for_browser_state(
            page,
            "() => window.__ctxOpenCount && window.__ctxOpenCount >= 2",
            timeout=5.0,
        )
        audit_path.write_text(
            json.dumps(
                {
                    "ts": "2026-04-28T00:00:00Z",
                    "event": "skill.loaded",
                    "subject": "python-patterns",
                    "session_id": "browser-sse",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _wait_for_browser_state(
            page,
            "() => window.__ctxEvents && window.__ctxEvents.length >= 2",
            timeout=5.0,
        )
        events = page.evaluate("() => window.__ctxEvents")
        assert {row[0] for row in events} == {"a", "b"}
        assert all("browser-sse" in row[1] for row in events)

        status = page.evaluate("""
            async () => {
              const r = await fetch('/api/sessions.json');
              await r.json();
              return r.status;
            }
        """)
        assert status == 200
        page.evaluate("() => { window.__ctxSourceA.close(); window.__ctxSourceB.close(); }")
    finally:
        harness.close()
