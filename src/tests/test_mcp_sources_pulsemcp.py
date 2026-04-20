"""
tests/test_mcp_sources_pulsemcp.py -- Unit tests for mcp_sources.pulsemcp
(HTML scraping mode).

Phase 2b.5 replaced the authenticated JSON API path with public-listing
HTML scraping. Tests now cover:
  - _split_cards: chunks one listing page into per-card HTML slices
  - _parse_listing: pure parser fixture-driven
  - _to_record: per-card mapping to McpRecord-compatible dicts
  - _PulsemcpSource.fetch: page-by-page iteration with limit + cache
  - SOURCE singleton attributes
All HTTP is mocked via mcp_sources.base._build_opener — no real network.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_FIXTURE_DIR = Path(__file__).parent / "fixtures"

from mcp_sources.pulsemcp import (  # type: ignore[import-untyped]  # noqa: E402
    LISTING_BASE,
    SOURCE,
    _PulsemcpSource,
    _parse_listing,
    _split_cards,
    _to_record,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _excerpt_html() -> str:
    """3-card real-HTML excerpt saved from pulsemcp.com/servers?page=1."""
    return (_FIXTURE_DIR / "pulsemcp_listing_excerpt.html").read_text(encoding="utf-8")


def _excerpt_bytes() -> bytes:
    return (_FIXTURE_DIR / "pulsemcp_listing_excerpt.html").read_bytes()


def _make_paginated_opener(pages: list[bytes]) -> Any:
    """Return a fake opener whose .open() returns each page's body in sequence.

    After exhausting the list, returns an empty page so the source's
    end-of-results detection ('zero cards parsed → stop') fires
    gracefully rather than raising IndexError.
    """
    state = {"i": 0}

    def _open(req: Any, timeout: float = 30.0) -> Any:
        i = state["i"]
        state["i"] += 1
        body = pages[i] if i < len(pages) else b"<html><body></body></html>"
        resp = MagicMock()
        resp.read.return_value = body
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    fake = MagicMock()
    fake.open.side_effect = _open
    return fake


def _isolate_wiki(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point cfg.wiki_dir at a tmp dir so cache writes don't pollute the user's wiki."""
    from ctx_config import cfg  # noqa: PLC0415

    wiki = tmp_path / "skill-wiki"
    wiki.mkdir(exist_ok=True)
    monkeypatch.setattr(cfg, "wiki_dir", wiki)


# ---------------------------------------------------------------------------
# _split_cards — page → per-card slices
# ---------------------------------------------------------------------------


class TestSplitCards:
    def test_excerpt_yields_three_cards(self) -> None:
        cards = _split_cards(_excerpt_html())
        assert len(cards) == 3

    def test_each_card_contains_data_test_id(self) -> None:
        for card in _split_cards(_excerpt_html()):
            assert "mcp-server-card-" in card

    def test_empty_html_returns_empty(self) -> None:
        assert _split_cards("<html><body></body></html>") == []

    def test_no_cards_returns_empty(self) -> None:
        assert _split_cards("<a href='/about'>About</a>") == []


# ---------------------------------------------------------------------------
# _to_record — per-card mapping
# ---------------------------------------------------------------------------


class TestToRecord:
    def test_first_card_has_required_fields(self) -> None:
        cards = _split_cards(_excerpt_html())
        record = _to_record(cards[0])
        assert record is not None
        assert record["sources"] == ["pulsemcp"]
        assert record["name"]
        assert record["homepage_url"].startswith(LISTING_BASE)

    def test_classification_official_becomes_official_tag(self) -> None:
        # First card in the fixture is microsoft-playwright (official).
        cards = _split_cards(_excerpt_html())
        record = _to_record(cards[0])
        assert record is not None
        assert "official" in record.get("tags", [])

    def test_classification_community_becomes_community_tag(self) -> None:
        # Second card is ktanaka101-duckdb (community).
        cards = _split_cards(_excerpt_html())
        record = _to_record(cards[1])
        assert record is not None
        assert "community" in record.get("tags", [])

    def test_no_data_test_id_returns_none(self) -> None:
        bogus = "<a href='/servers/foo'><h3>Foo</h3></a>"
        assert _to_record(bogus) is None

    def test_homepage_url_uses_slug_from_test_id(self) -> None:
        cards = _split_cards(_excerpt_html())
        record = _to_record(cards[0])
        assert record is not None
        # Real fixture's first card is microsoft-playwright.
        assert record["homepage_url"].endswith("/microsoft-playwright")

    def test_round_trips_through_mcp_record(self) -> None:
        from mcp_entity import McpRecord  # noqa: PLC0415

        cards = _split_cards(_excerpt_html())
        for card in cards:
            raw = _to_record(card)
            assert raw is not None, f"card returned None: {card[:200]}"
            # Must not raise
            rec = McpRecord.from_dict(raw)
            assert rec.slug
            assert rec.name


# ---------------------------------------------------------------------------
# _parse_listing — full page
# ---------------------------------------------------------------------------


class TestParseListing:
    def test_excerpt_returns_three_records(self) -> None:
        records = _parse_listing(_excerpt_html())
        assert len(records) == 3

    def test_empty_page_returns_empty_list(self) -> None:
        assert _parse_listing("<html><body></body></html>") == []

    def test_every_record_has_pulsemcp_source(self) -> None:
        for record in _parse_listing(_excerpt_html()):
            assert record["sources"] == ["pulsemcp"]


# ---------------------------------------------------------------------------
# _PulsemcpSource.fetch — pagination + limit
# ---------------------------------------------------------------------------


class TestFetch:
    @pytest.fixture(autouse=True)
    def _isolated_wiki(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Cache writes must go to tmp_path, not the user's real wiki.
        _isolate_wiki(monkeypatch, tmp_path)

    def test_single_page_yields_three_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Page 1 returns the excerpt (3 cards), page 2+ returns empty
        # so the loop terminates.
        fake_opener = _make_paginated_opener([_excerpt_bytes()])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch())

        assert len(records) == 3

    def test_two_pages_yield_six_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two non-empty pages followed by empty page → 3+3=6 records
        fake_opener = _make_paginated_opener([_excerpt_bytes(), _excerpt_bytes()])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch())

        assert len(records) == 6

    def test_limit_short_circuits_within_first_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_opener = _make_paginated_opener([_excerpt_bytes()])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch(limit=2))

        assert len(records) == 2
        # Only one HTTP call — second page never fetched
        assert fake_opener.open.call_count == 1

    def test_empty_first_page_returns_no_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_opener = _make_paginated_opener([b"<html><body></body></html>"])
        monkeypatch.setattr("mcp_sources.base._build_opener", lambda: fake_opener)

        source = _PulsemcpSource()
        records = list(source.fetch())

        assert records == []


# ---------------------------------------------------------------------------
# SOURCE singleton
# ---------------------------------------------------------------------------


class TestSourceSingleton:
    def test_source_name_is_pulsemcp(self) -> None:
        assert SOURCE.name == "pulsemcp"

    def test_source_homepage_is_listing_url(self) -> None:
        assert SOURCE.homepage == "https://www.pulsemcp.com/servers"

    def test_source_satisfies_protocol(self) -> None:
        from mcp_sources.base import Source  # noqa: PLC0415

        assert isinstance(SOURCE, Source)
