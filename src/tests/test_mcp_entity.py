"""
tests/test_mcp_entity.py -- Unit tests for the McpRecord dataclass.

Coverage:
  - from_dict happy path (full and minimal inputs)
  - Slug normalization (casing, punctuation, empty result raises ValueError)
  - GitHub URL canonicalization (full URL, bare domain, short form, .git suffix)
  - Transport filtering (sorted, lowercased, unknown values dropped)
  - Tags deduplication + lowercase + sort; empty input produces ("uncategorized",)
  - Description defaults and truncation at 300 chars
  - Frozen dataclass enforcement (FrozenInstanceError on field assignment)
  - entity_relpath sharding (alpha first-char, numeric 0-9 bucket)
  - canonical_dedup_key (with and without github_url; case-insensitive URL match)
  - to_frontmatter excludes the raw field
"""

from __future__ import annotations

import json
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mcp_entity import McpRecord  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_data() -> dict[str, Any]:
    """Return a fully-populated raw dict for McpRecord.from_dict."""
    return {
        "name": "GitHub MCP Server",
        "description": "Repository management and GitHub API integration",
        "sources": ["awesome-mcp", "pulsemcp"],
        "github_url": "https://github.com/Org/Repo",
        "homepage_url": "https://example.com",
        "tags": ["github", "git", "api"],
        "transports": ["stdio", "http"],
        "language": "typescript",
        "license": "MIT",
        "author": "acme-org",
        "author_type": "org",
        "stars": 1234,
        "last_commit_at": "2025-01-01",
    }


def _minimal_data() -> dict[str, Any]:
    """Return only the required fields (name + description)."""
    return {
        "name": "My MCP",
        "description": "A minimal MCP server",
    }


# ---------------------------------------------------------------------------
# from_dict: happy paths
# ---------------------------------------------------------------------------


class TestFromDictHappyPath:
    def test_full_input_all_fields_populated(self) -> None:
        data = _full_data()
        record = McpRecord.from_dict(data)

        assert record.name == "GitHub MCP Server"
        assert record.description == "Repository management and GitHub API integration"
        assert "awesome-mcp" in record.sources
        assert "pulsemcp" in record.sources
        assert record.github_url == "https://github.com/Org/Repo"
        assert record.homepage_url == "https://example.com"
        assert "github" in record.tags
        assert "stdio" in record.transports
        assert record.language == "typescript"
        assert record.license == "MIT"
        assert record.author == "acme-org"
        assert record.author_type == "org"
        assert record.stars == 1234
        assert record.last_commit_at == "2025-01-01"

    def test_full_input_raw_field_preserved(self) -> None:
        data = _full_data()
        record = McpRecord.from_dict(data)
        # raw must hold the original dict
        assert record.raw["name"] == data["name"]
        assert record.raw["stars"] == 1234

    def test_minimal_input_optional_fields_default_to_none(self) -> None:
        record = McpRecord.from_dict(_minimal_data())

        assert record.github_url is None
        assert record.homepage_url is None
        assert record.language is None
        assert record.license is None
        assert record.author is None
        assert record.author_type is None
        assert record.stars is None
        assert record.last_commit_at is None

    def test_minimal_input_sources_defaults_to_empty_tuple(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        assert isinstance(record.sources, tuple)
        assert len(record.sources) == 0

    def test_minimal_input_transports_defaults_to_empty_tuple(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        assert isinstance(record.transports, tuple)

    def test_from_dict_returns_mcp_record_instance(self) -> None:
        assert isinstance(McpRecord.from_dict(_minimal_data()), McpRecord)


# ---------------------------------------------------------------------------
# Slug normalization
# ---------------------------------------------------------------------------


class TestSlugNormalization:
    def test_spaces_become_hyphens(self) -> None:
        record = McpRecord.from_dict({**_minimal_data(), "name": "GitHub MCP Server"})
        assert record.slug == "github-mcp-server"

    def test_double_separators_collapsed(self) -> None:
        record = McpRecord.from_dict({**_minimal_data(), "name": "foo__bar..baz"})
        assert record.slug == "foo-bar-baz"

    def test_leading_trailing_separators_stripped(self) -> None:
        record = McpRecord.from_dict({**_minimal_data(), "name": "  --hello-- "})
        assert record.slug == "hello"

    def test_all_punctuation_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            McpRecord.from_dict({**_minimal_data(), "name": "!!!"})

    def test_slug_is_lowercase(self) -> None:
        record = McpRecord.from_dict({**_minimal_data(), "name": "My Cool Server"})
        assert record.slug == record.slug.lower()

    def test_slug_no_special_chars(self) -> None:
        record = McpRecord.from_dict({**_minimal_data(), "name": "Foo & Bar (v2)"})
        # Result must contain only [a-z0-9-]
        import re
        assert re.fullmatch(r"[a-z0-9][a-z0-9\-]*[a-z0-9]?", record.slug) is not None


# ---------------------------------------------------------------------------
# GitHub URL canonicalization
# ---------------------------------------------------------------------------


class TestGithubUrlCanonicalization:
    def test_full_https_url_kept(self) -> None:
        data = {**_minimal_data(), "github_url": "https://github.com/Org/Repo"}
        record = McpRecord.from_dict(data)
        assert record.github_url == "https://github.com/Org/Repo"

    def test_bare_domain_gets_https_scheme(self) -> None:
        data = {**_minimal_data(), "github_url": "github.com/Org/Repo/"}
        record = McpRecord.from_dict(data)
        assert record.github_url == "https://github.com/Org/Repo"

    def test_short_form_org_repo_expanded(self) -> None:
        data = {**_minimal_data(), "github_url": "Org/Repo"}
        record = McpRecord.from_dict(data)
        assert record.github_url == "https://github.com/Org/Repo"

    def test_http_url_upgraded_to_https(self) -> None:
        data = {**_minimal_data(), "github_url": "http://github.com/Org/Repo.git"}
        record = McpRecord.from_dict(data)
        assert record.github_url == "https://github.com/Org/Repo"

    def test_trailing_slash_stripped(self) -> None:
        data = {**_minimal_data(), "github_url": "https://github.com/Org/Repo/"}
        record = McpRecord.from_dict(data)
        assert not record.github_url.endswith("/")  # type: ignore[union-attr]

    def test_dot_git_suffix_stripped(self) -> None:
        data = {**_minimal_data(), "github_url": "https://github.com/Org/Repo.git"}
        record = McpRecord.from_dict(data)
        assert not record.github_url.endswith(".git")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Transport filtering
# ---------------------------------------------------------------------------


class TestTransportsFiltering:
    def test_sorted_lowercased_unknown_dropped(self) -> None:
        data = {
            **_minimal_data(),
            "transports": ["stdio", "Foo", "HTTP", "websocket"],
        }
        record = McpRecord.from_dict(data)
        # "Foo" is not a known transport and must be dropped
        assert record.transports == ("http", "stdio", "websocket")

    def test_transports_are_sorted(self) -> None:
        data = {**_minimal_data(), "transports": ["websocket", "stdio", "http"]}
        record = McpRecord.from_dict(data)
        assert record.transports == tuple(sorted(record.transports))

    def test_empty_transports_returns_empty_tuple(self) -> None:
        record = McpRecord.from_dict({**_minimal_data(), "transports": []})
        assert record.transports == ()

    def test_duplicate_transports_deduplicated(self) -> None:
        data = {**_minimal_data(), "transports": ["stdio", "stdio", "http"]}
        record = McpRecord.from_dict(data)
        assert record.transports.count("stdio") == 1


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


class TestTags:
    def test_tags_are_lowercased_sorted_deduped(self) -> None:
        data = {**_minimal_data(), "tags": ["GitHub", "git", "API", "github"]}
        record = McpRecord.from_dict(data)
        # No duplicates
        assert len(record.tags) == len(set(record.tags))
        # Sorted
        assert record.tags == tuple(sorted(record.tags))
        # Lowercased
        assert all(t == t.lower() for t in record.tags)

    def test_empty_tags_produces_uncategorized(self) -> None:
        record = McpRecord.from_dict({**_minimal_data(), "tags": []})
        assert record.tags == ("uncategorized",)

    def test_missing_tags_key_produces_uncategorized(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        assert record.tags == ("uncategorized",)


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------


class TestDescription:
    def test_empty_description_gets_default(self) -> None:
        data = {**_minimal_data(), "description": ""}
        record = McpRecord.from_dict(data)
        assert record.description == "No description available."

    def test_long_description_truncated_with_ellipsis(self) -> None:
        long_desc = "A" * 350
        data = {**_minimal_data(), "description": long_desc}
        record = McpRecord.from_dict(data)
        assert len(record.description) <= 303  # 300 chars + "..."
        assert record.description.endswith("...")

    def test_short_description_unchanged(self) -> None:
        desc = "A short description."
        data = {**_minimal_data(), "description": desc}
        record = McpRecord.from_dict(data)
        assert record.description == desc

    def test_exactly_300_char_description_not_truncated(self) -> None:
        desc = "B" * 300
        data = {**_minimal_data(), "description": desc}
        record = McpRecord.from_dict(data)
        assert not record.description.endswith("...")
        assert record.description == desc


# ---------------------------------------------------------------------------
# Frozen dataclass
# ---------------------------------------------------------------------------


class TestFrozen:
    def test_assigning_field_raises_frozen_instance_error(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        with pytest.raises(FrozenInstanceError):
            record.slug = "new-slug"  # type: ignore[misc]

    def test_assigning_name_raises_frozen_instance_error(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        with pytest.raises(FrozenInstanceError):
            record.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# entity_relpath
# ---------------------------------------------------------------------------


class TestEntityRelpath:
    def test_alpha_slug_uses_first_char_bucket(self) -> None:
        data = {**_minimal_data(), "name": "github-mcp"}
        record = McpRecord.from_dict(data)
        assert record.entity_relpath() == Path("g/github-mcp.md")

    def test_numeric_slug_uses_0_9_bucket(self) -> None:
        data = {**_minimal_data(), "name": "007-server"}
        record = McpRecord.from_dict(data)
        assert record.entity_relpath() == Path("0-9/007-server.md")

    def test_relpath_ends_with_dot_md(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        assert record.entity_relpath().suffix == ".md"

    def test_relpath_stem_matches_slug(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        assert record.entity_relpath().stem == record.slug


# ---------------------------------------------------------------------------
# canonical_dedup_key
# ---------------------------------------------------------------------------


class TestCanonicalDedupKey:
    def test_with_github_url_uses_normalised_url(self) -> None:
        data = {**_minimal_data(), "github_url": "https://github.com/Org/Repo"}
        record = McpRecord.from_dict(data)
        key = record.canonical_dedup_key()
        assert "github.com/org/repo" in key.lower()

    def test_without_github_url_uses_slug_prefix(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        assert record.canonical_dedup_key().startswith("slug:")

    def test_different_case_same_github_url_produces_same_key(self) -> None:
        data_upper = {**_minimal_data(), "github_url": "https://github.com/ORG/REPO"}
        data_lower = {**_minimal_data(), "github_url": "https://github.com/org/repo"}
        record_upper = McpRecord.from_dict(data_upper)
        record_lower = McpRecord.from_dict(data_lower)
        assert record_upper.canonical_dedup_key() == record_lower.canonical_dedup_key()


# ---------------------------------------------------------------------------
# to_frontmatter
# ---------------------------------------------------------------------------


class TestToFrontmatter:
    def test_raw_field_not_in_frontmatter(self) -> None:
        record = McpRecord.from_dict(_full_data())
        fm = record.to_frontmatter()
        assert "raw" not in fm

    def test_frontmatter_contains_slug(self) -> None:
        record = McpRecord.from_dict(_full_data())
        fm = record.to_frontmatter()
        assert "slug" in fm

    def test_frontmatter_contains_type_mcp_server(self) -> None:
        record = McpRecord.from_dict(_full_data())
        fm = record.to_frontmatter()
        assert fm.get("type") == "mcp-server"

    def test_frontmatter_returns_dict(self) -> None:
        record = McpRecord.from_dict(_minimal_data())
        assert isinstance(record.to_frontmatter(), dict)

    def test_frontmatter_sources_serializable(self) -> None:
        """sources must be a list (not a tuple) so yaml.dump doesn't add !!python/tuple."""
        record = McpRecord.from_dict(_full_data())
        fm = record.to_frontmatter()
        # If sources is present it must be a list, not a tuple
        if "sources" in fm:
            assert isinstance(fm["sources"], list)


# ---------------------------------------------------------------------------
# Fixture loading smoke-test
# ---------------------------------------------------------------------------


class TestFixtureFiles:
    def test_github_fixture_loads_as_valid_mcp_record(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "mcp_github.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        record = McpRecord.from_dict(data)
        assert record.slug  # non-empty slug
        assert record.github_url is not None

    def test_pulsemcp_fixture_loads_as_valid_mcp_record(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "mcp_pulsemcp.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        record = McpRecord.from_dict(data)
        assert record.slug
        assert record.stars == 12500
