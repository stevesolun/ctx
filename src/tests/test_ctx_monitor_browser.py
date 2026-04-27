"""Browser-driven security coverage for ctx-monitor."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

import ctx_monitor as cm

playwright_sync: Any = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.browser


@dataclass
class MonitorHarness:
    base_url: str
    port: int
    calls: list[str]
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
    return claude


@pytest.fixture()
def page() -> Iterator[Any]:
    with playwright_sync.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
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
    calls: list[str] = []
    if fake_load:
        def perform_load(slug: str) -> tuple[bool, str]:
            calls.append(slug)
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
        assert harness.calls == ["python-patterns"]
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
        page.evaluate(
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
        page.goto(f"{harness.base_url}/loaded")
        page.wait_for_load_state("networkidle")
        page.evaluate("""
            () => {
              window.__ctxEvents = [];
              window.__ctxSourceA = new EventSource('/api/events.stream');
              window.__ctxSourceB = new EventSource('/api/events.stream');
              window.__ctxSourceA.onmessage = (event) => window.__ctxEvents.push(['a', event.data]);
              window.__ctxSourceB.onmessage = (event) => window.__ctxEvents.push(['b', event.data]);
            }
        """)
        time.sleep(0.7)
        audit_path = fake_claude / "ctx-audit.jsonl"
        audit_path.write_text(
            json.dumps({
                "ts": "2026-04-28T00:00:00Z",
                "event": "skill.loaded",
                "subject": "python-patterns",
                "session_id": "browser-sse",
            }) + "\n",
            encoding="utf-8",
        )
        page.wait_for_function(
            "() => window.__ctxEvents && window.__ctxEvents.length >= 2",
            timeout=5000,
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
