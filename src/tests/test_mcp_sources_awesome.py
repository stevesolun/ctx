"""
tests/test_mcp_sources_awesome.py -- Unit tests for mcp_sources.awesome_mcp.

Contracts tested (Phase 2a locked spec):
  - _parse_readme(text) -> list[dict]   (pure function, no network)
  - Each dict has name, description, sources=["awesome-mcp"]
  - GitHub links extracted to github_url
  - Non-github links extracted to homepage_url
  - Section headers produce tag entries
  - Malformed lines skipped gracefully
  - Parsed dicts round-trip through McpRecord.from_dict()
  - SOURCE singleton has expected name / homepage attributes
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "awesome_mcp_excerpt.md"

try:
    from mcp_sources.awesome_mcp import SOURCE, _parse_readme  # type: ignore[import-untyped]

    _IMPORT_OK = True
except ImportError:
    _IMPORT_OK = False

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK,
    reason="awaits Phase 2a wiring: mcp_sources.awesome_mcp not yet present",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _excerpt() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_readme — edge cases
# ---------------------------------------------------------------------------


class TestParseReadmeEdgeCases:
    def test_empty_string_returns_empty_list(self) -> None:
        result = _parse_readme("")
        assert result == []

    def test_whitespace_only_returns_empty_list(self) -> None:
        result = _parse_readme("   \n\n  \n")
        assert result == []

    def test_malformed_line_does_not_crash(self) -> None:
        # Parser only emits records found AFTER the ## Server Implementations
        # gate; we include it so the ### subsection below is in scope.
        text = (
            "## Server Implementations\n\n"
            "### 🛠️ Tools\n\n"
            "This line has no link and should be skipped gracefully by the parser.\n"
            "- [valid-mcp](https://github.com/example/valid-mcp) - A valid entry.\n"
        )
        result = _parse_readme(text)
        # The malformed line must be silently skipped; valid entry survives
        assert len(result) >= 1
        names = [r["name"] for r in result]
        assert any("valid-mcp" in n for n in names)


# ---------------------------------------------------------------------------
# _parse_readme — excerpt fixture (5 well-formed entries)
# ---------------------------------------------------------------------------


class TestParseReadmeExcerpt:
    def test_excerpt_produces_correct_entry_count(self) -> None:
        # Fixture has 5 well-formed link entries; 1 malformed line skipped
        result = _parse_readme(_excerpt())
        # There may also be an additional entry after the malformed line
        assert len(result) >= 5

    def test_each_dict_has_name_key(self) -> None:
        result = _parse_readme(_excerpt())
        for entry in result:
            assert "name" in entry, f"Missing 'name' in {entry}"

    def test_each_dict_has_description_key(self) -> None:
        result = _parse_readme(_excerpt())
        for entry in result:
            assert "description" in entry, f"Missing 'description' in {entry}"

    def test_each_dict_has_sources_list(self) -> None:
        result = _parse_readme(_excerpt())
        for entry in result:
            assert "sources" in entry, f"Missing 'sources' in {entry}"
            assert isinstance(entry["sources"], list)

    def test_sources_contains_awesome_mcp(self) -> None:
        result = _parse_readme(_excerpt())
        for entry in result:
            assert "awesome-mcp" in entry["sources"], (
                f"'awesome-mcp' not in sources for {entry['name']}"
            )

    def test_github_url_extracted_for_github_links(self) -> None:
        result = _parse_readme(_excerpt())
        github_entries = [r for r in result if "github.com" in (r.get("github_url") or "")]
        assert len(github_entries) >= 1, "Expected at least one entry with a github_url"

    def test_non_github_url_extracted_as_homepage_url(self) -> None:
        # The fixture has notion-mcp linking to www.notion.so — not github
        result = _parse_readme(_excerpt())
        non_github = [
            r
            for r in result
            if not r.get("github_url") and r.get("homepage_url")
        ]
        assert len(non_github) >= 1, (
            "Expected at least one entry with homepage_url but no github_url"
        )

    def test_section_header_produces_tag_on_entries_below(self) -> None:
        # Entries under "### 🐙 Version Control" should carry a version-control tag
        result = _parse_readme(_excerpt())
        vc_entries = [
            r
            for r in result
            if r.get("name") in ("github-mcp", "gitlab-mcp")
        ]
        assert len(vc_entries) >= 1, "Expected entries from 'Version Control' section"
        for entry in vc_entries:
            tags = entry.get("tags") or []
            tag_str = " ".join(str(t).lower() for t in tags)
            assert "version" in tag_str or "control" in tag_str or "vcs" in tag_str, (
                f"Expected version-control tag for {entry['name']}, got tags={tags}"
            )

    def test_parsed_dicts_round_trip_through_mcp_record(self) -> None:
        from mcp_entity import McpRecord  # noqa: PLC0415

        result = _parse_readme(_excerpt())
        for entry in result:
            try:
                McpRecord.from_dict(entry)
            except Exception as exc:
                pytest.fail(
                    f"McpRecord.from_dict() raised for entry {entry.get('name')!r}: {exc}"
                )


# ---------------------------------------------------------------------------
# SOURCE singleton
# ---------------------------------------------------------------------------


class TestSourceSingleton:
    def test_source_name_is_awesome_mcp(self) -> None:
        assert SOURCE.name == "awesome-mcp"

    def test_source_homepage_is_non_empty_string(self) -> None:
        assert isinstance(SOURCE.homepage, str)
        assert SOURCE.homepage.strip() != ""

    def test_source_homepage_looks_like_url(self) -> None:
        assert SOURCE.homepage.startswith("http"), (
            f"SOURCE.homepage should start with 'http', got: {SOURCE.homepage!r}"
        )
