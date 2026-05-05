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

from mcp_sources.awesome_mcp import SOURCE, _parse_readme  # type: ignore[import-untyped]  # noqa: E402


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
        assert [r["name"] for r in result] == ["valid-mcp"]


# ---------------------------------------------------------------------------
# _parse_readme — excerpt fixture (5 well-formed entries)
# ---------------------------------------------------------------------------


class TestParseReadmeExcerpt:
    def test_excerpt_produces_correct_entry_count(self) -> None:
        result = _parse_readme(_excerpt())
        assert len(result) == 6

    def test_excerpt_produces_exact_expected_names(self) -> None:
        result = _parse_readme(_excerpt())
        assert [entry["name"] for entry in result] == [
            "github-mcp",
            "gitlab-mcp",
            "postgres-mcp",
            "sqlite-mcp",
            "notion-mcp",
            "another-tool",
        ]

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
        result = {entry["name"]: entry for entry in _parse_readme(_excerpt())}
        assert result["github-mcp"]["github_url"] == (
            "https://github.com/modelcontextprotocol/github-mcp"
        )
        assert result["gitlab-mcp"]["github_url"] == "https://github.com/acmecorp/gitlab-mcp"
        assert result["postgres-mcp"]["github_url"] == "https://github.com/example/postgres-mcp"
        assert result["sqlite-mcp"]["github_url"] == "https://github.com/example/sqlite-mcp"
        assert result["another-tool"]["github_url"] == "https://github.com/example/another-tool"
        assert "github_url" not in result["notion-mcp"]

    def test_non_github_url_extracted_as_homepage_url(self) -> None:
        # The fixture has notion-mcp linking to www.notion.so — not github
        result = {entry["name"]: entry for entry in _parse_readme(_excerpt())}
        assert result["notion-mcp"]["homepage_url"] == (
            "https://www.notion.so/integrations/notion-mcp"
        )
        assert "homepage_url" not in result["github-mcp"]

    def test_section_header_produces_tag_on_entries_below(self) -> None:
        # Entries under "### 🐙 Version Control" should carry a version-control tag
        result = {entry["name"]: entry for entry in _parse_readme(_excerpt())}
        assert result["github-mcp"]["tags"] == ["version-control"]
        assert result["gitlab-mcp"]["tags"] == ["version-control"]
        assert result["postgres-mcp"]["tags"] == ["databases"]
        assert result["sqlite-mcp"]["tags"] == ["databases"]
        assert result["notion-mcp"]["tags"] == ["productivity"]
        assert result["another-tool"]["tags"] == ["productivity"]

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
