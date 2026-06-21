"""Browser-driven security coverage for ctx-monitor."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import networkx as nx
import pytest

import ctx_monitor as cm
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
    monkeypatch.setattr(cm, "_claude_dir", lambda: claude)
    monkeypatch.setattr(cm, "_dashboard_graph_index_archives", lambda: [])
    sidecar_service.reset_caches()
    monkeypatch.setattr(cm, "_KPI_SUMMARY_CACHE_KEY", None)
    monkeypatch.setattr(cm, "_KPI_SUMMARY_CACHE_VALUE", None)
    monkeypatch.setattr(cm, "_KPI_SUMMARY_CACHE_AT", 0.0)
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
    monkeypatch.setattr(cm, "_MONITOR_TOKEN", "browser-token")
    calls: list[tuple[str, str]] = []
    if fake_load:
        def perform_load(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
            calls.append((slug, entity_type))
            return True, "loaded"

        monkeypatch.setattr(cm, "_perform_load", perform_load)

    server = cm._make_monitor_server("127.0.0.1", 0)
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


def test_graph_page_uses_builtin_svg_renderer(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    page: Any,
) -> None:
    monkeypatch.setattr(cm, "_graph_match_default_min_percent", lambda: 3)
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
    G.add_edge("skill:python-patterns", "agent:code-reviewer", weight=0.9, shared_tags=["review"], tag_sim=0.3333)
    G.add_edge("skill:python-patterns", "mcp-server:github-mcp-server", weight=0.8, shared_tags=["github"])
    G.add_edge("skill:python-patterns", "harness:langgraph", weight=0.7, shared_tags=["agent"])
    G.add_edge("skill:python-patterns", "skill:medium-graph-link", weight=0.43, tag_sim=0.0)
    G.add_edge("agent:code-reviewer", "skill:weak-graph-link", weight=0.05)
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)
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
        assert page.locator("[data-testid='graph-node-detail']").evaluate(
            "node => getComputedStyle(node).overflowY",
        ) == "auto"
        assert page.locator("[data-testid='graph-edge-detail']").evaluate(
            "node => node.parentElement?.getAttribute('data-testid')",
        ) == "graph-node-detail"
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
        assert page.locator("[data-3d-node-id='skill:medium-graph-link']").evaluate(
            "node => getComputedStyle(node).display",
        ) == "none"
        assert page.locator("[data-testid='graph-svg-edge'][data-edge-weight='0.4300']").evaluate(
            "node => getComputedStyle(node).display",
        ) == "none"
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
        assert page.locator("[data-3d-node-id='agent:code-reviewer']").evaluate(
            "node => getComputedStyle(node).display",
        ) == "none"
        assert page.locator("[data-testid='graph-svg-edge'][data-edge-weight='0.9000']").evaluate(
            "node => getComputedStyle(node).display",
        ) == "none"
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

        reviewer_radius = float(page.locator(
            "[data-3d-node-id='agent:code-reviewer'] [data-testid='graph-svg-node']",
        ).get_attribute("data-radius") or "0")
        mcp_radius = float(page.locator(
            "[data-3d-node-id='mcp-server:github-mcp-server'] [data-testid='graph-svg-node']",
        ).get_attribute("data-radius") or "0")
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
        page.locator("[data-3d-node-id='agent:code-reviewer']").dispatch_event("dblclick")
        _wait_for_browser_state(
            page,
            "() => document.getElementById('focus').value === 'python-patterns'",
            timeout=5.0,
        )

        page.fill("#focus", "code")
        page.wait_for_selector("[data-testid='graph-live-results'] [data-live-slug='code-reviewer']", timeout=5000)
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
        page.locator("[data-testid='graph-node-detail-tree'] a[href='/wiki/code-reviewer?type=agent']").click()
        page.wait_for_url("**/wiki/code-reviewer?type=agent", timeout=5000)
        assert "code-reviewer" in page.locator("h1").inner_text()
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
    monkeypatch.setattr(cm, "_docs_index_entries", lambda: entries)
    monkeypatch.setattr(
        cm,
        "_docs_tabs",
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
        page.fill("textarea[name='body']", "# Custom Reviewer\n\nUse before merging Python changes.\n")
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

        page.once("dialog", lambda dialog: dialog.accept())
        page.fill("input[name='title']", "Custom Reviewer Updated")
        page.locator("#entity-editor-form button[type='submit']").click()
        _wait_for_browser_state(
            page,
            "() => document.getElementById('entity-editor-status').textContent.includes('saved agent:custom-reviewer')",
            timeout=5.0,
        )
        assert "title: Custom Reviewer Updated" in entity_path.read_text(encoding="utf-8")

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
    _write_quality_sidecar(fake_claude, "langgraph-harness", {
        "slug": "langgraph",
        "subject_type": "harness",
        "grade": "A",
        "raw_score": 0.93,
    })

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
        page.fill("input[name='verify']", "pytest")
        _wait_for_browser_state(
            page,
            "() => document.querySelector('[data-testid=\"harness-command-output\"]').textContent.includes('--model-provider \"huggingface\"')",
            timeout=5.0,
        )
        command = page.locator("[data-testid='harness-command-output']").inner_text()
        assert "--model \"HuggingFaceTB/SmolLM2-135M-Instruct\"" in command
        assert "--plan-on-no-fit" in command
        assert page.locator(".harness-card[data-harness-slug='langgraph']").count() == 1

        page.locator("[data-select-harness='langgraph']").click()
        selected = page.locator("#selected-harness-command").inner_text()
        assert "ctx-harness-install langgraph --dry-run" in selected
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
        json.dumps({
            "ts": "2026-06-16T10:00:00Z",
            "event": "skill.loaded",
            "subject": "python-patterns",
            "subject_type": "skill",
            "actor": "hook",
            "session_id": "browser-session",
        }) + "\n",
        encoding="utf-8",
    )
    (fake_claude / "skill-events.jsonl").write_text(
        json.dumps({
            "timestamp": "2026-06-16T10:00:01Z",
            "event": "load",
            "skill": "python-patterns",
            "session_id": "browser-session",
        }) + "\n",
        encoding="utf-8",
    )
    _write_quality_sidecar(fake_claude, "alpha", {
        "slug": "alpha",
        "subject_type": "skill",
        "grade": "A",
        "raw_score": 0.92,
        "score": 0.92,
        "computed_at": "2026-06-16T10:00:00Z",
    })
    runtime_path = tmp_path / "runtime" / "events.jsonl"
    monkeypatch.setattr(cm, "_runtime_lifecycle_path", lambda: runtime_path)
    _write_runtime_events(runtime_path, [
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
    ])

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
        json.dumps({
            "ts": "2026-04-28T00:00:00Z",
            "event": "skill.loaded",
            "subject": "python-patterns",
            "session_id": "events-page-backlog",
        }) + "\n",
        encoding="utf-8",
    )
    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/events")
        page.wait_for_selector("#stream", timeout=5000)
        assert "Showing last 1 audit events" in page.locator("body").inner_text()
        assert "events-page-backlog" in page.locator("#stream").inner_text()

        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": "2026-04-28T00:00:01Z",
                "event": "agent.loaded",
                "subject": "repo-reviewer",
                "session_id": "events-page-live",
            }) + "\n")
        _wait_for_browser_state(
            page,
            "() => document.getElementById('stream').textContent.includes('events-page-live')",
            timeout=5.0,
        )
        status_text = page.locator("#stream-status").inner_text()
        assert status_text in {"connected; waiting for new events", "live"}
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
            json.dumps({
                "ts": "2026-04-28T00:00:00Z",
                "event": "skill.loaded",
                "subject": "python-patterns",
                "session_id": "browser-sse",
            }) + "\n",
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
