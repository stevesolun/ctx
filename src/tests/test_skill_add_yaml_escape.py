"""Regression tests for P2-13: YAML frontmatter injection via unescaped paths."""

import sys
from pathlib import Path

import yaml

# Ensure src/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from skill_add import build_entity_page


def _parse_frontmatter(page: str) -> dict:
    """Extract and parse the YAML frontmatter block from a markdown page."""
    assert page.startswith("---\n"), "Page must start with YAML frontmatter"
    end = page.index("\n---\n", 4)
    fm_body = page[4:end]
    return yaml.safe_load(fm_body)


class TestYamlFrontmatterEscape:
    """Paths with special YAML characters must not produce malformed frontmatter."""

    def _build(self, path_str: str) -> str:
        return build_entity_page(
            name="test-skill",
            tags=["python"],
            line_count=50,
            has_pipeline=False,
            original_path=Path(path_str),
            pipeline_path=None,
            related=[],
            scan_sources=[],
        )

    def test_path_with_colon_is_valid_yaml(self):
        page = self._build("/home/user/.claude/skills/C:/some:path/SKILL.md")
        fm = _parse_frontmatter(page)
        # The colon must survive in the value (Path may normalize separators on Windows)
        assert "some:path" in fm["original_path"]

    def test_path_with_double_quote_is_valid_yaml(self):
        page = self._build('/path/with "quotes"/SKILL.md')
        fm = _parse_frontmatter(page)
        assert '"quotes"' in fm["original_path"]

    def test_path_with_single_quote_is_valid_yaml(self):
        page = self._build("/path/with 'apostrophe'/SKILL.md")
        fm = _parse_frontmatter(page)
        assert "apostrophe" in fm["original_path"]

    def test_path_with_backslash_is_valid_yaml(self):
        page = self._build(r"C:\Users\foo\SKILL.md")
        fm = _parse_frontmatter(page)
        assert "foo" in fm["original_path"]

    def test_path_with_newline_is_valid_yaml(self):
        """A newline in the path must not break out of the frontmatter value."""
        page = self._build("/path/with\nnewline/SKILL.md")
        fm = _parse_frontmatter(page)
        # yaml.safe_dump encodes newlines inside a scalar — value must still be a string
        assert isinstance(fm["original_path"], str)

    def test_round_trip_preserves_type(self):
        """All frontmatter scalar types survive a safe_load round-trip."""
        page = self._build("/normal/path/SKILL.md")
        fm = _parse_frontmatter(page)
        assert isinstance(fm["use_count"], int)
        assert isinstance(fm["has_pipeline"], bool)
        assert fm["avg_session_rating"] is None
        assert isinstance(fm["tags"], list)
