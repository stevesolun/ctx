"""Regression tests for P2-14: markdown table injection via unescaped file paths."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from skill_add_detector import _escape_md_cell


class TestEscapeMdCell:
    """_escape_md_cell must produce single-cell-safe strings."""

    def test_pipe_is_escaped(self):
        result = _escape_md_cell("path/with|pipe/SKILL.md")
        assert "|" not in result or result.count(r"\|") > 0
        # Specifically: literal unescaped pipe must be gone
        assert "\\|" in result

    def test_newline_collapsed_to_space(self):
        result = _escape_md_cell("path/with\nnewline")
        assert "\n" not in result
        assert " " in result

    def test_crlf_collapsed_to_space(self):
        result = _escape_md_cell("path/with\r\nnewline")
        assert "\r" not in result
        assert "\n" not in result

    def test_backtick_is_escaped(self):
        result = _escape_md_cell("path/with`backtick`/SKILL.md")
        assert "\\`" in result

    def test_combined_injection_renders_single_row(self):
        """A path with pipe + newline + backtick must not break table structure."""
        malicious = "skill|name\n`exec`|injection"
        result = _escape_md_cell(malicious)
        # No raw pipe or newline remains
        assert "\n" not in result
        # All pipes escaped
        for segment in result.split("\\|"):
            assert "|" not in segment

    def test_clean_path_unchanged_functionally(self):
        """Normal paths with no special chars are returned functionally equivalent."""
        clean = "/home/user/.claude/skills/fastapi-pro/SKILL.md"
        result = _escape_md_cell(clean)
        assert "fastapi-pro" in result
        assert "/" in result  # slashes are not escaped
