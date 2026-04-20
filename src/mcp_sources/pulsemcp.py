"""src/mcp_sources/pulsemcp.py -- Source for pulsemcp.com (HTML scraping mode).

Iterates the public listing pages at https://www.pulsemcp.com/servers
?page=N. Stdlib only (html.parser). No credentials required — the
authenticated JSON API at /api/v0.1 is documented but gated behind
manual approval; scraping the public pages is the only path users
without a partnership API key can take.

Each listing page returns 42 server cards. Total ~12,975 servers
across ~310 pages as of v0.6.5. Detail-page enrichment (github_url,
language, transports) is deferred to Phase 6 — Phase 2b.5 ships only
the listing-card data: slug (from URL), name (from h3), creator,
description, classification (official / community / reference).

The HTML structure is content-addressed via ``data-test-id="mcp-server-card-<slug>"``
attributes which gives us a stable card boundary without depending on
class names that may shift with a frontend redesign.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date
from html.parser import HTMLParser

from mcp_sources.base import Source, fetch_text, read_cache, write_cache

__all__ = ["SOURCE"]

LISTING_BASE = "https://www.pulsemcp.com/servers"
_TOTAL_PAGES_FALLBACK = 310  # As of 2026-04: 12,975 servers / 42 per page = 310

# Card boundary marker — content-addressed so a frontend restyle of
# class names doesn't silently break the parser.
_CARD_TEST_ID_RE = re.compile(r'data-test-id="mcp-server-card-([a-z0-9][a-z0-9_-]+)"')


class _CardTextExtractor(HTMLParser):
    """Stream-extract text content per tag inside one server card.

    Builds an ordered list of ``(tag, attrs, text)`` triples for every
    leaf text node, plus a separate flat string of all text for fallback
    matching. We keep both so the record-mapping step can prefer
    structural matches (h3 → name, gray-500 p → creator) while degrading
    gracefully when the markup shifts.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, dict[str, str], str]] = []
        self._stack: list[tuple[str, dict[str, str]]] = []
        self._buffer: list[str] = []

    def _flush(self) -> None:
        if not self._stack:
            return
        text = "".join(self._buffer).strip()
        if text:
            tag, attrs = self._stack[-1]
            self.items.append((tag, attrs, text))
        self._buffer = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush()
        attrs_dict = {k: (v or "") for k, v in attrs}
        self._stack.append((tag, attrs_dict))

    def handle_endtag(self, tag: str) -> None:
        self._flush()
        if self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._buffer.append(data)


def _split_cards(html: str) -> list[str]:
    """Return the substring slice for each card found in *html*.

    Uses ``data-test-id="mcp-server-card-<slug>"`` as the anchor and
    walks forward to the next card anchor (or end of input) to bound
    the slice. Avoids html.parser overhead for the page-level split.
    """
    anchors = list(_CARD_TEST_ID_RE.finditer(html))
    if not anchors:
        return []
    slices = []
    for i, m in enumerate(anchors):
        # Walk back to the opening <a href="/servers/..." that wraps
        # this card. The data-test-id sits on a <div> inside the <a>,
        # so we step back to the nearest <a tag start.
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
        # Find nearest preceding "<a" — bounded scan.
        scan_from = max(0, m.start() - 500)
        a_open = html.rfind('<a ', scan_from, m.start())
        start = a_open if a_open >= 0 else m.start()
        slices.append(html[start:end])
    return slices


def _slug_from_card(card_html: str) -> str | None:
    """Extract the slug from the card's data-test-id."""
    m = _CARD_TEST_ID_RE.search(card_html)
    return m.group(1) if m else None


def _to_record(card_html: str) -> dict | None:
    """Map one server card's HTML slice to a McpRecord-compatible dict.

    Returns ``None`` when the card is too malformed to use (no slug
    or no name). Listing-page data is sparse: we get slug, name,
    creator, description, classification. github_url, language, and
    transports remain unset until Phase 6 fetches detail pages.
    """
    slug = _slug_from_card(card_html)
    if not slug:
        return None

    extractor = _CardTextExtractor()
    try:
        extractor.feed(card_html)
    except Exception:  # noqa: BLE001 — html.parser may choke on malformed input
        return None

    name: str | None = None
    creator: str | None = None
    description: str | None = None
    classification: str | None = None
    saw_classification_label = False

    for tag, attrs, text in extractor.items:
        cls = attrs.get("class", "")
        if name is None and tag == "h3":
            name = text
            continue
        if creator is None and tag == "p" and "text-gray-500" in cls:
            creator = text
            continue
        if description is None and tag == "p" and "text-pulse-black" in cls and "leading-relaxed" in cls:
            description = text
            continue
        if tag == "p" and "Classification" in text:
            saw_classification_label = True
            continue
        if saw_classification_label and tag == "p" and classification is None:
            # Next text-bearing <p> after the label carries the value.
            classification = text.strip().lower()
            saw_classification_label = False

    if not name:
        # Fall back to the slug itself; better than dropping the card.
        name = slug.replace("-", " ").title()

    record: dict = {
        "name": name,
        "sources": ["pulsemcp"],
        "homepage_url": f"{LISTING_BASE}/{slug}",
    }
    if description:
        record["description"] = description
    if creator:
        record["author"] = creator
    tags: list[str] = []
    if classification == "official":
        tags.append("official")
    elif classification == "community":
        tags.append("community")
    elif classification == "reference":
        tags.append("reference")
    if tags:
        record["tags"] = tags
    return record


def _parse_listing(html: str) -> list[dict]:
    """Parse one listing page's HTML into a list of raw record dicts.

    Pure function — no I/O. Tested against a recorded fixture excerpt.
    Skipped cards (no slug) are dropped silently; partial cards (no
    name) fall back to the slug-derived name rather than being dropped.
    """
    out: list[dict] = []
    for card_html in _split_cards(html):
        record = _to_record(card_html)
        if record is not None:
            out.append(record)
    return out


def _fetch_page(page: int, *, refresh: bool) -> str:
    """Fetch one listing page's HTML, with date-keyed caching."""
    today = date.today().isoformat()
    basename = f"{today}--page-{page:04d}.html"
    source_name = "pulsemcp"

    cached = None if refresh else read_cache(source_name, basename)
    if cached is not None:
        return cached

    url = f"{LISTING_BASE}?page={page}"
    text = fetch_text(url)
    write_cache(source_name, basename, text)
    return text


class _PulsemcpSource:
    name = "pulsemcp"
    homepage = "https://www.pulsemcp.com/servers"

    def fetch(self, *, limit: int | None = None, refresh: bool = False) -> Iterator[dict]:
        """Walk pulsemcp listing pages until exhausted or *limit* reached.

        Each page yields ~42 records. Stops early when:
          - *limit* records have been yielded
          - A page returns 0 cards (means we ran past the end)
          - We hit page _TOTAL_PAGES_FALLBACK (hard ceiling against
            runaway loops if the parser misses the empty signal)

        Args:
            limit: Maximum records to yield. ``None`` yields everything.
            refresh: Bypass the local raw cache and re-fetch from network.

        Yields:
            Raw dicts suitable for ``McpRecord.from_dict()``.
        """
        yielded = 0
        for page in range(1, _TOTAL_PAGES_FALLBACK + 1):
            if limit is not None and yielded >= limit:
                return
            html = _fetch_page(page, refresh=refresh)
            records = _parse_listing(html)
            if not records:
                # Past the last populated page.
                return
            for record in records:
                if limit is not None and yielded >= limit:
                    return
                yield record
                yielded += 1


SOURCE: Source = _PulsemcpSource()
