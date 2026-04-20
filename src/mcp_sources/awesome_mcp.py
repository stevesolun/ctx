"""src/mcp_sources/awesome_mcp.py -- Source for github.com/punkpeye/awesome-mcp-servers.

Parses the README markdown into raw McpRecord-shaped dicts.

README structure (as observed 2026-04-20):
- ## Server Implementations  (top-level gate — only parse after this heading)
- ### <emoji> <a name="..."></a><Category Title>  (section headers)
- - [name](url) [badge markup] [emoji flags] - description  (entry lines)

Entry separators may be ` - `, ` – ` (en-dash), or ` — ` (em-dash).
Some entries have no description. Badge markup ([!img](url)) is stripped.
Language is inferred from emoji annotation in the entry line.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import date

from mcp_sources.base import Source, fetch_text, read_cache, write_cache

_logger = logging.getLogger(__name__)


URL = "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md"

# ---------------------------------------------------------------------------
# Language emoji -> language name map (from the README legend)
# ---------------------------------------------------------------------------
_LANG_EMOJI: dict[str, str] = {
    "\U0001f40d": "python",        # 🐍
    "\U0001f4c7": "typescript",    # 📇 (card index dividers — TS/JS)
    "\U0001f3ce\ufe0f": "go",      # 🏎️
    "\U0001f980": "rust",          # 🦀
    "#\ufe0f\u20e3": "csharp",     # #️⃣
    "\u2615": "java",              # ☕
    "\U0001f30a": "c/c++",         # 🌊
    "\U0001f48e": "ruby",          # 💎
}

# Badge image markup: [![alt](img-url)](link-url) — strip entirely
_BADGE_RE = re.compile(r"\[!\[.*?\]\(.*?\)\]\(.*?\)")

# Inline image (no outer link): ![alt](url)
_INLINE_IMG_RE = re.compile(r"!\[.*?\]\(.*?\)")

# Markdown link: [text](url)
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")

# Section heading: ### ... <a name="slug"></a>Title
_SECTION_H3_RE = re.compile(r"^#{1,3}\s+(.+)$")

# Anchor tag used inside headings: <a name="..."></a>
_ANCHOR_TAG_RE = re.compile(r"<a\s[^>]*>.*?</a>", re.IGNORECASE)

# Non-ASCII characters (for stripping emoji before slugifying section names)
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")

# Description separators: ` - `, ` – `, ` — ` (with surrounding spaces)
_DESC_SEP_RE = re.compile(r"\s[-\u2013\u2014]\s")

# The ## heading that gates the server entry section
_SERVER_SECTION_MARKER = "Server Implementations"

# Sections we know are not server entries (after ## Server Implementations ends
# another ## heading appears — we stop at the next ## that isn't a sub-section)
_STOP_MARKER_RE = re.compile(r"^##\s+(?!#)")


def _section_to_tag(raw_heading: str) -> str:
    """Convert a raw ### heading text to a hyphenated lowercase tag.

    Strips HTML anchor tags, emoji/non-ASCII, normalizes whitespace,
    and collapses runs of non-alphanumeric characters to single hyphens.

    Examples::

        "🔗 <a name='aggregators'></a>Aggregators"  -> "aggregators"
        "🔄 <a name='version-control'></a>Version Control" -> "version-control"
        "<a name='bio'></a>Biology, Medicine and Bioinformatics" -> "biology-medicine-and-bioinformatics"
    """
    # Remove HTML anchor tags
    text = _ANCHOR_TAG_RE.sub("", raw_heading)
    # Remove non-ASCII (emoji, etc.)
    text = _NON_ASCII_RE.sub("", text)
    # Lowercase and collapse runs of non-alphanumeric to hyphen
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "uncategorized"


def _detect_language(line: str) -> str | None:
    """Return a language string if a known language emoji is present."""
    for emoji, lang in _LANG_EMOJI.items():
        if emoji in line:
            return lang
    return None


def _strip_badges_and_links(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove badge markup from *text*, return cleaned text + list of (label, url) links."""
    cleaned = _BADGE_RE.sub("", text)
    cleaned = _INLINE_IMG_RE.sub("", cleaned)
    links = _LINK_RE.findall(cleaned)
    return cleaned, links


def _parse_readme(text: str) -> list[dict]:  # noqa: C901 — complexity justified by format variance
    """Walk the README headings + bullets, yield one dict per server entry.

    Pure function — no I/O. Designed to be easy to unit-test against a
    fixture excerpt.

    Args:
        text: Full contents of the README markdown file.

    Returns:
        List of raw dicts, each acceptable to ``McpRecord.from_dict()``.
    """
    records: list[dict] = []
    skipped = 0

    in_server_section = False
    current_tag: str = "uncategorized"

    for raw_line in text.splitlines():
        line = raw_line.rstrip()

        # ----------------------------------------------------------------
        # Track ## / ### headings
        # ----------------------------------------------------------------
        if line.startswith("##"):
            # A bare ## (not ###) is either the gate-in or the gate-out
            if not line.startswith("###"):
                if _SERVER_SECTION_MARKER in line:
                    in_server_section = True
                elif in_server_section:
                    # Next top-level ## ends the server listing
                    in_server_section = False
                continue

            # ### section inside server implementations
            if in_server_section:
                m = _SECTION_H3_RE.match(line)
                if m:
                    current_tag = _section_to_tag(m.group(1))
            continue

        if not in_server_section:
            continue

        # ----------------------------------------------------------------
        # Entry lines start with `- ` (possibly leading whitespace)
        # ----------------------------------------------------------------
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue

        entry_body = stripped[2:]  # drop the leading "- "

        # Strip badge markup first, collect all plain links
        clean_body, links = _strip_badges_and_links(entry_body)

        if not links:
            # No markdown link at all — skip (section description, back-to-top, etc.)
            skipped += 1
            _logger.debug("skip (no link): %r", entry_body[:80])
            continue

        # First link is the primary entry link
        name, primary_url = links[0]
        name = name.strip()
        if not name:
            skipped += 1
            _logger.debug("skip (empty name): %r", entry_body[:80])
            continue

        # Classify URL
        github_url: str | None = None
        homepage_url: str | None = None
        primary_url = primary_url.strip()
        if re.match(r"^https?://(?:www\.)?github\.com/", primary_url, re.IGNORECASE):
            github_url = primary_url
        else:
            homepage_url = primary_url if primary_url else None

        # ----------------------------------------------------------------
        # Extract description: text after the first ` - `/ ` – `/ ` — ` separator
        # ----------------------------------------------------------------
        # Work on the cleaned body (badges removed) but preserve the original
        # link text so the separator search is stable.
        # We find the separator that appears AFTER the first link's closing `)`
        first_link_end = clean_body.find(")", clean_body.find("]("))
        search_from = first_link_end + 1 if first_link_end != -1 else 0
        tail = clean_body[search_from:]

        desc_match = _DESC_SEP_RE.search(tail)
        if desc_match:
            description: str | None = tail[desc_match.end():].strip()
            if not description:
                description = None
        else:
            description = None

        # Detect language from the full original line (before cleaning)
        language = _detect_language(entry_body)

        record: dict = {
            "name": name,
            "sources": ["awesome-mcp"],
            "tags": [current_tag],
        }
        if description:
            record["description"] = description
        if github_url:
            record["github_url"] = github_url
        if homepage_url:
            record["homepage_url"] = homepage_url
        if language:
            record["language"] = language

        records.append(record)

    _logger.info("parsed %d entries, skipped %d", len(records), skipped)
    return records


# ---------------------------------------------------------------------------
# Source class
# ---------------------------------------------------------------------------


class _AwesomeMcpSource:
    name = "awesome-mcp"
    homepage = "https://github.com/punkpeye/awesome-mcp-servers"

    def fetch(self, *, limit: int | None = None, refresh: bool = False) -> Iterator[dict]:
        """Yield raw records. Caches the README at raw/marketplace-dumps/awesome-mcp/.

        Args:
            limit: If set, yield at most this many records.
            refresh: If ``True``, bypass the cache and re-fetch from network.

        Yields:
            Raw dicts suitable for ``McpRecord.from_dict()``.
        """
        cache_basename = f"README-{date.today().isoformat()}.md"
        text: str | None = None
        if not refresh:
            text = read_cache(self.name, cache_basename)
        if text is None:
            text = fetch_text(URL)
            write_cache(self.name, cache_basename, text)
        records = _parse_readme(text)
        if limit is not None:
            records = records[:limit]
        for r in records:
            yield r


SOURCE: Source = _AwesomeMcpSource()
