"""
test_wiki_utils.py -- Tests for wiki_utils module.

Covers:
  - FRONTMATTER_RE: regex matching YAML-style frontmatter
  - SAFE_NAME_RE: regex for skill name validation
  - parse_frontmatter(): YAML frontmatter parsing with list/quoted value handling
  - parse_frontmatter_and_body(): frontmatter + body text separation
  - get_field(): single field extraction
  - validate_skill_name(): skill name validation and error handling
"""

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path is already patched by conftest.py, but guard here too so the module
# can be run in isolation (e.g. `python -m pytest tests/test_wiki_utils.py`).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from wiki_utils import (  # noqa: E402
    FRONTMATTER_RE,
    SAFE_NAME_RE,
    get_field,
    parse_frontmatter,
    parse_frontmatter_and_body,
    validate_skill_name,
)


# ===========================================================================
# Tests for parse_frontmatter()
# ===========================================================================


class TestParseFrontmatter:
    """test_parse_frontmatter -- YAML-style frontmatter parsing."""

    def test_happy_path_multiple_fields(self) -> None:
        """Parse valid frontmatter with multiple key-value pairs."""
        text = """---
title: My Skill
tags: python, testing
description: A test skill
---

# Body content here"""
        result = parse_frontmatter(text)
        assert result["title"] == "My Skill"
        assert result["tags"] == "python, testing"
        assert result["description"] == "A test skill"

    def test_empty_frontmatter_block(self) -> None:
        """Return empty dict when no frontmatter block exists."""
        text = "# Just a heading\n\nNo frontmatter here"
        result = parse_frontmatter(text)
        assert result == {}

    def test_missing_frontmatter_block(self) -> None:
        """Return empty dict when text doesn't start with ---."""
        text = "Some text without frontmatter"
        result = parse_frontmatter(text)
        assert result == {}

    def test_list_values_parsed_as_list(self) -> None:
        """Parse [a, b, c] values as list[str]."""
        text = """---
tags: [python, testing, automation]
skills: [skill1, skill2]
---

Body"""
        result = parse_frontmatter(text)
        assert result["tags"] == ["python", "testing", "automation"]
        assert result["skills"] == ["skill1", "skill2"]

    def test_quoted_scalar_values_have_quotes_stripped(self) -> None:
        """Strip surrounding quotes from quoted values."""
        text = '''---
title: "My Title"
description: 'Another Description'
---

Body'''
        result = parse_frontmatter(text)
        assert result["title"] == "My Title"
        assert result["description"] == "Another Description"

    def test_colon_in_value_parsed_correctly(self) -> None:
        """Handle colons in values (e.g., file paths, URLs)."""
        text = """---
path: C:/foo/bar
url: https://example.com:8080/path
---

Body"""
        result = parse_frontmatter(text)
        assert result["path"] == "C:/foo/bar"
        assert result["url"] == "https://example.com:8080/path"

    def test_field_with_empty_value(self) -> None:
        """Parse fields with empty values."""
        text = """---
title: My Title
empty_field:
description: A description
---

Body"""
        result = parse_frontmatter(text)
        assert result["title"] == "My Title"
        assert result["empty_field"] == ""
        assert result["description"] == "A description"

    def test_lines_without_colon_are_skipped(self) -> None:
        """Skip lines that don't contain a colon."""
        text = """---
title: My Title
this line has no colon
description: My Description
---

Body"""
        result = parse_frontmatter(text)
        assert result["title"] == "My Title"
        assert result["description"] == "My Description"
        assert "this line has no colon" not in result

    def test_whitespace_is_stripped_from_keys_and_values(self) -> None:
        """Strip leading/trailing whitespace from keys and values."""
        text = """---
  title  :  My Title
  description:Another description
---

Body"""
        result = parse_frontmatter(text)
        assert result["title"] == "My Title"
        assert result["description"] == "Another description"

    def test_quoted_list_items_have_quotes_stripped(self) -> None:
        """Quoted items in lists have quotes stripped."""
        text = """---
tags: ["python", 'testing', automation]
---

Body"""
        result = parse_frontmatter(text)
        assert result["tags"] == ["python", "testing", "automation"]


# ===========================================================================
# Tests for parse_frontmatter_and_body()
# ===========================================================================


class TestParseFrontmatterAndBody:
    """test_parse_frontmatter_and_body -- frontmatter + body separation."""

    def test_returns_dict_and_body_text(self) -> None:
        """Return (frontmatter_dict, body_text) tuple correctly."""
        text = """---
title: My Skill
---

# Body Heading

Body paragraph here.
More content."""
        fm, body = parse_frontmatter_and_body(text)
        assert fm["title"] == "My Skill"
        assert "Body Heading" in body
        assert "Body paragraph" in body

    def test_no_frontmatter_returns_empty_dict_and_full_text(self) -> None:
        """Return ({}, full_text) when no frontmatter block exists."""
        text = "# Heading\n\nContent without frontmatter"
        fm, body = parse_frontmatter_and_body(text)
        assert fm == {}
        assert body == text

    def test_body_is_stripped_of_leading_trailing_whitespace(self) -> None:
        """Strip leading/trailing whitespace from body."""
        text = """---
title: Test
---


  # Heading with leading spaces

Content"""
        fm, body = parse_frontmatter_and_body(text)
        assert fm["title"] == "Test"
        assert body.startswith("# Heading")
        assert not body.startswith(" ")

    def test_empty_body_after_frontmatter(self) -> None:
        """Handle case where frontmatter is followed by nothing."""
        text = """---
title: Test
---"""
        fm, body = parse_frontmatter_and_body(text)
        assert fm["title"] == "Test"
        assert body == ""


# ===========================================================================
# Tests for get_field()
# ===========================================================================


class TestGetField:
    """test_get_field -- single field extraction."""

    def test_returns_field_value_from_frontmatter(self) -> None:
        """Extract a field value by name from frontmatter."""
        text = """---
title: My Skill
description: A test skill
tags: python
---

Body"""
        assert get_field(text, "title") == "My Skill"
        assert get_field(text, "description") == "A test skill"
        assert get_field(text, "tags") == "python"

    def test_returns_empty_string_for_missing_field(self) -> None:
        """Return empty string when field is not found."""
        text = """---
title: My Skill
---

Body"""
        assert get_field(text, "missing") == ""
        assert get_field(text, "nonexistent") == ""

    def test_handles_fields_with_colons_in_value(self) -> None:
        """Correctly handle field values containing colons."""
        text = """---
path: C:/Users/test/file.txt
url: http://example.com:8080/api
---

Body"""
        assert get_field(text, "path") == "C:/Users/test/file.txt"
        assert get_field(text, "url") == "http://example.com:8080/api"

    def test_returns_stripped_value(self) -> None:
        """Return value with stripped whitespace."""
        text = """---
title:   My Skill
---

Body"""
        assert get_field(text, "title") == "My Skill"

    def test_multiline_is_case_sensitive(self) -> None:
        """Field search is case-sensitive."""
        text = """---
title: My Skill
Title: Different Value
---

Body"""
        assert get_field(text, "title") == "My Skill"
        assert get_field(text, "Title") == "Different Value"


# ===========================================================================
# Tests for validate_skill_name()
# ===========================================================================


class TestValidateSkillName:
    """test_validate_skill_name -- skill name validation."""

    def test_valid_simple_name(self) -> None:
        """Accept simple alphanumeric names."""
        assert validate_skill_name("python") == "python"
        assert validate_skill_name("a") == "a"
        assert validate_skill_name("123") == "123"

    def test_valid_name_with_hyphen(self) -> None:
        """Accept names with hyphens."""
        assert validate_skill_name("my-skill") == "my-skill"
        assert validate_skill_name("a-b-c") == "a-b-c"

    def test_valid_name_with_underscore(self) -> None:
        """Accept names with underscores."""
        assert validate_skill_name("my_skill") == "my_skill"
        assert validate_skill_name("skill_v2") == "skill_v2"

    def test_valid_name_with_dot(self) -> None:
        """Accept names with dots."""
        assert validate_skill_name("skill.name") == "skill.name"
        assert validate_skill_name("a.b.c") == "a.b.c"

    def test_valid_name_mixed_characters(self) -> None:
        """Accept names with mixed valid characters."""
        assert validate_skill_name("my-skill_v2.1") == "my-skill_v2.1"
        assert validate_skill_name("a1-b2_c3.d4") == "a1-b2_c3.d4"

    def test_invalid_empty_name(self) -> None:
        """Reject empty string."""
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("")

    def test_invalid_name_too_long(self) -> None:
        """Reject names longer than 128 characters."""
        long_name = "a" * 200
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name(long_name)

    def test_invalid_name_starts_with_dash(self) -> None:
        """Reject names starting with a hyphen."""
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("-start-with-dash")

    def test_invalid_name_starts_with_underscore(self) -> None:
        """Reject names starting with an underscore."""
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("_starts_with_underscore")

    def test_invalid_name_starts_with_dot(self) -> None:
        """Reject names starting with a dot."""
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name(".starts_with_dot")

    def test_invalid_name_with_spaces(self) -> None:
        """Reject names containing spaces."""
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("has spaces")

    def test_invalid_name_path_traversal(self) -> None:
        """Reject path traversal sequences."""
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("../etc/passwd")

    def test_invalid_name_with_special_characters(self) -> None:
        """Reject names with invalid special characters."""
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("skill@name")
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("skill#name")
        with pytest.raises(ValueError, match="Invalid skill name"):
            validate_skill_name("skill$name")

    def test_returns_input_on_success(self) -> None:
        """Return the same string on successful validation."""
        name = "my-valid-skill"
        result = validate_skill_name(name)
        assert result == name
        assert result is name  # Same object


# ===========================================================================
# Tests for SAFE_NAME_RE
# ===========================================================================


class TestSafeNameRe:
    """test_safe_name_re -- SAFE_NAME_RE regex pattern."""

    def test_regex_matches_valid_names(self) -> None:
        """SAFE_NAME_RE matches all valid names."""
        valid_names = [
            "python",
            "a",
            "my-skill",
            "skill_v2",
            "skill.name",
            "a1-b2_c3.d4",
            "123abc",
        ]
        for name in valid_names:
            assert SAFE_NAME_RE.match(name), f"Expected {name} to match"

    def test_regex_rejects_invalid_names(self) -> None:
        """SAFE_NAME_RE rejects all invalid names."""
        invalid_names = [
            "",
            "-start",
            "_start",
            ".start",
            "has spaces",
            "../path",
            "a" * 200,
            "skill@name",
            "skill#name",
        ]
        for name in invalid_names:
            assert not SAFE_NAME_RE.match(name), f"Expected {name} to NOT match"

    def test_regex_pattern_is_anchored(self) -> None:
        """SAFE_NAME_RE is anchored (^ and $)."""
        # Should not match partial strings
        assert SAFE_NAME_RE.match("valid-name")
        # But should reject if invalid chars appear anywhere
        assert not SAFE_NAME_RE.match("skill@invalid")

    def test_regex_allows_up_to_128_chars(self) -> None:
        """SAFE_NAME_RE allows up to 128 characters."""
        # 128 chars total: starts with letter, rest are valid
        name_128 = "a" + "-" * 127  # 1 + 127 = 128
        assert SAFE_NAME_RE.match(name_128)

        # 129 chars total should not match
        name_129 = "a" + "-" * 128  # 1 + 128 = 129
        assert not SAFE_NAME_RE.match(name_129)


# ===========================================================================
# Tests for FRONTMATTER_RE
# ===========================================================================


class TestFrontmatterRe:
    """test_frontmatter_re -- FRONTMATTER_RE regex pattern."""

    def test_regex_matches_valid_frontmatter(self) -> None:
        """FRONTMATTER_RE matches valid frontmatter blocks."""
        text = """---
title: Test
---

Body"""
        match = FRONTMATTER_RE.match(text)
        assert match is not None
        assert match.group(1).strip() == "title: Test"

    def test_regex_captures_frontmatter_content(self) -> None:
        """FRONTMATTER_RE group(1) contains frontmatter content."""
        text = """---
title: Test
tags: python
---

Body"""
        match = FRONTMATTER_RE.match(text)
        assert match is not None
        content = match.group(1)
        assert "title: Test" in content
        assert "tags: python" in content

    def test_regex_does_not_match_without_leading_dashes(self) -> None:
        """FRONTMATTER_RE requires --- at the start."""
        text = """Content
---
title: Test
---

Body"""
        match = FRONTMATTER_RE.match(text)
        assert match is None

    def test_regex_handles_minimal_frontmatter_block(self) -> None:
        """FRONTMATTER_RE matches minimal frontmatter with just newlines."""
        text = """---

---

Body"""
        match = FRONTMATTER_RE.match(text)
        assert match is not None

    def test_regex_is_greedy_on_multiline(self) -> None:
        """FRONTMATTER_RE handles multiline content correctly."""
        text = """---
title: Test
description: Multi
line content
tags: [a, b, c]
---

Body"""
        match = FRONTMATTER_RE.match(text)
        assert match is not None
        content = match.group(1)
        assert "title: Test" in content
        assert "description: Multi" in content
