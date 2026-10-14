"""Browser execution coverage for the public documentation JavaScript."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
CATALOG_DOC = ROOT / "docs" / "catalog.md"
CATALOG_SCRIPT = ROOT / "docs" / "assets" / "javascripts" / "catalog.js"
CATALOG_URL = "https://ctx.test/catalog/"
REPO_STATS_SCRIPT = ROOT / "docs" / "assets" / "javascripts" / "repo-stats-refresh.js"
REPO_STATS_API = "https://api.github.com/repos/stevesolun/ctx"

playwright_sync: Any = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.browser


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


def _catalog_html() -> str:
    """Return the real catalog app and production stylesheet."""
    text = CATALOG_DOC.read_text(encoding="utf-8")
    marker = '<div class="ctx-catalog-app">'
    try:
        app = marker + text.split(marker, maxsplit=1)[1].split("\n\n<style>", maxsplit=1)[0]
        styles = "<style>" + text.split("<style>", maxsplit=1)[1].split("</style>", maxsplit=1)[0]
    except IndexError as exc:
        raise AssertionError(
            "docs/catalog.md no longer contains the public catalog app/styles"
        ) from exc
    return f"<!doctype html><html><head>{styles}</style></head><body>{app}</body></html>"


def _repo_source_html() -> str:
    return """<!doctype html>
<html>
  <body>
    <a class="md-source" href="https://github.com/stevesolun/ctx/">
      <span class="md-source__repository">
        stevesolun/ctx
        <ul class="md-source__facts"><li>stale cached value</li></ul>
      </span>
    </a>
    <a class="md-source" href="https://github.com/example/other">
      <span class="md-source__repository">example/other</span>
    </a>
  </body>
</html>"""


def test_catalog_search_and_type_filters_execute_in_browser(page: Any) -> None:
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.set_content(_catalog_html(), wait_until="domcontentloaded")
    page.add_script_tag(path=str(CATALOG_SCRIPT))

    cards = page.locator(".ctx-catalog-card")
    total_cards = cards.count()
    assert total_cards > 0
    assert page.locator(".ctx-catalog-card:visible").count() == total_cards
    assert page.locator("#ctx-catalog-count").inner_text() == f"{total_cards} tiles shown"

    page.locator("#ctx-catalog-query").fill("architecture")
    assert page.locator(".ctx-catalog-card:visible").count() == 1
    assert page.locator(".ctx-catalog-card:visible h3").inner_text() == "Architecture agents"
    assert page.locator("#ctx-catalog-count").inner_text() == "1 tile shown"
    hidden_layout = page.locator(".ctx-catalog-card[hidden]").evaluate_all(
        """
        cards => cards.map(card => ({
          display: getComputedStyle(card).display,
          height: card.getBoundingClientRect().height,
        }))
        """
    )
    assert len(hidden_layout) == total_cards - 1
    assert all(item == {"display": "none", "height": 0} for item in hidden_layout)

    page.locator("#ctx-catalog-query").fill("")
    mcp_cards = page.locator('.ctx-catalog-card[data-type="mcp-server"]')
    mcp_count = mcp_cards.count()
    assert mcp_count > 0
    page.locator('.ctx-catalog-filters input[value="mcp-server"]').uncheck()
    assert page.locator('.ctx-catalog-card[data-type="mcp-server"]:visible').count() == 0
    assert page.locator(".ctx-catalog-card:visible").count() == total_cards - mcp_count
    assert page.locator("#ctx-catalog-count").inner_text() == (
        f"{total_cards - mcp_count} tiles shown"
    )
    assert page_errors == []


def test_catalog_type_query_hides_nonmatching_card_layout(page: Any) -> None:
    page.route(
        f"{CATALOG_URL}**",
        lambda route: route.fulfill(status=200, content_type="text/html", body=_catalog_html()),
    )
    page.goto(f"{CATALOG_URL}?type=skill", wait_until="domcontentloaded")
    page.add_script_tag(path=str(CATALOG_SCRIPT))

    skill_cards = page.locator('.ctx-catalog-card[data-type="skill"]')
    assert skill_cards.count() == 4
    assert page.locator(".ctx-catalog-card:visible").count() == skill_cards.count()
    assert page.locator("#ctx-catalog-count").inner_text() == "4 tiles shown"
    hidden_layout = page.locator('.ctx-catalog-card:not([data-type="skill"])').evaluate_all(
        """
        cards => cards.map(card => ({
          hidden: card.hidden,
          display: getComputedStyle(card).display,
          height: card.getBoundingClientRect().height,
        }))
        """
    )
    assert hidden_layout
    assert all(item == {"hidden": True, "display": "none", "height": 0} for item in hidden_layout)


def test_repo_stats_refresh_renders_controlled_success_without_network(page: Any) -> None:
    page_errors: list[str] = []
    requests: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def handle_request(route: Any) -> None:
        requests.append(route.request.url)
        if route.request.url == REPO_STATS_API:
            route.fulfill(
                status=200,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
                body=json.dumps({"stargazers_count": 42, "forks_count": 7}),
            )
            return
        route.abort()

    page.route("**/*", handle_request)
    page.set_content(_repo_source_html(), wait_until="domcontentloaded")
    with page.expect_response(REPO_STATS_API) as response_info:
        page.add_script_tag(path=str(REPO_STATS_SCRIPT))
    assert response_info.value.status == 200

    ctx_source = page.locator('.md-source[href="https://github.com/stevesolun/ctx/"]')
    assert ctx_source.locator(".md-source__fact--stars").inner_text() == "42"
    assert ctx_source.locator(".md-source__fact--forks").inner_text() == "7"
    assert ctx_source.locator(".md-source__facts").count() == 1
    assert ctx_source.locator(".md-source__repository--active").count() == 1
    assert (
        page.locator(
            '.md-source[href="https://github.com/example/other"] .md-source__facts'
        ).count()
        == 0
    )
    assert requests == [REPO_STATS_API]
    assert page_errors == []


def test_repo_stats_refresh_clears_stale_data_on_controlled_failure(page: Any) -> None:
    page_errors: list[str] = []
    requests: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def handle_request(route: Any) -> None:
        requests.append(route.request.url)
        if route.request.url == REPO_STATS_API:
            route.fulfill(
                status=503,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
                body=json.dumps({"message": "controlled outage"}),
            )
            return
        route.abort()

    page.route("**/*", handle_request)
    page.set_content(_repo_source_html(), wait_until="domcontentloaded")
    with page.expect_response(REPO_STATS_API) as response_info:
        page.add_script_tag(path=str(REPO_STATS_SCRIPT))
    assert response_info.value.status == 503

    ctx_source = page.locator('.md-source[href="https://github.com/stevesolun/ctx/"]')
    assert ctx_source.locator(".md-source__facts").count() == 0
    assert ctx_source.locator(".md-source__repository--active").count() == 0
    assert requests == [REPO_STATS_API]
    assert page_errors == []
