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
    G = nx.Graph()
    G.add_node("skill:python-patterns", label="python-patterns", type="skill", tags=["python"])
    G.add_node(
        "agent:code-reviewer",
        label="code-reviewer",
        type="agent",
        tags=["review"],
        quality_score=0.95,
        usage_score=0.8,
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
    G.add_edge("skill:python-patterns", "agent:code-reviewer", weight=0.9, shared_tags=["review"])
    G.add_edge("skill:python-patterns", "mcp-server:github-mcp-server", weight=0.8, shared_tags=["github"])
    G.add_edge("skill:python-patterns", "harness:langgraph", weight=0.7, shared_tags=["agent"])
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)
    _write_wiki_entity(fake_claude, "skill", "python-patterns", "# python-patterns\n")
    _write_wiki_entity(fake_claude, "agent", "code-reviewer", "# code-reviewer\n")

    harness = _start_monitor(monkeypatch, fake_load=False)
    try:
        page.goto(f"{harness.base_url}/graph?slug=python-patterns&type=skill")
        page.wait_for_selector("[data-testid='graph-renderer']", timeout=5000)
        assert "4 nodes" in page.locator("#msg").inner_text()
        assert page.locator("[data-testid='graph-svg-node']").count() == 4
        assert page.locator("[data-testid='graph-fallback-node']").count() == 4
        assert "Graph renderer unavailable" not in page.locator("#cy").inner_text()
        reviewer_radius = float(page.locator(
            "[data-3d-node-id='agent:code-reviewer'] [data-testid='graph-svg-node']",
        ).get_attribute("r") or "0")
        mcp_radius = float(page.locator(
            "[data-3d-node-id='mcp-server:github-mcp-server'] [data-testid='graph-svg-node']",
        ).get_attribute("r") or "0")
        assert reviewer_radius > mcp_radius

        page.fill("#tag-filter", "review")
        _wait_for_browser_state(
            page,
            "() => document.getElementById('graph-match-count').textContent === '2 visible'",
            timeout=5.0,
        )
        page.locator("[data-testid='graph-fallback-node'][data-slug='code-reviewer']").click()
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
