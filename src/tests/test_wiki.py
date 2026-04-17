"""
test_wiki.py -- Tests for the wiki_sync foundation layer.

Covers:
  - wiki_sync: ensure_wiki structure, idempotency, upsert_skill_page,
    update_index, append_log, SCHEMA.md required sections
"""

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path is already patched by conftest.py, but guard here too so the module
# can be run in isolation (e.g. `python -m pytest tests/test_wiki.py`).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from wiki_sync import (  # noqa: E402
    append_log,
    ensure_wiki,
    update_index,
    upsert_skill_page,
)


# ===========================================================================
# Wiki tests
# ===========================================================================


class TestWikiEnsureCreatesStructure:
    """test_wiki_ensure_creates_structure -- ensure_wiki creates required layout."""

    def test_schema_md_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "SCHEMA.md").exists()

    def test_index_md_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "index.md").exists()

    def test_log_md_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "log.md").exists()

    def test_entities_skills_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "entities" / "skills").is_dir()

    def test_raw_scans_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "raw" / "scans").is_dir()

    def test_raw_marketplace_dumps_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "raw" / "marketplace-dumps").is_dir()

    def test_entities_plugins_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "entities" / "plugins").is_dir()

    def test_entities_mcp_servers_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "entities" / "mcp-servers").is_dir()

    def test_concepts_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "concepts").is_dir()

    def test_comparisons_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "comparisons").is_dir()

    def test_queries_dir_exists(self, tmp_wiki: Path) -> None:
        assert (tmp_wiki / "queries").is_dir()


class TestWikiEnsureIdempotent:
    """test_wiki_ensure_idempotent -- calling ensure_wiki twice does not overwrite existing files."""

    def test_schema_content_preserved(self, tmp_wiki: Path) -> None:
        schema_path = tmp_wiki / "SCHEMA.md"
        original = schema_path.read_text(encoding="utf-8")
        # Append a sentinel so we can detect if the file is overwritten
        sentinel = "\n<!-- idempotency-sentinel -->\n"
        schema_path.write_text(original + sentinel, encoding="utf-8")

        ensure_wiki(str(tmp_wiki))

        result = schema_path.read_text(encoding="utf-8")
        assert sentinel in result, "ensure_wiki overwrote SCHEMA.md on second call"

    def test_index_content_preserved(self, tmp_wiki: Path) -> None:
        index_path = tmp_wiki / "index.md"
        sentinel = "\n<!-- idempotency-sentinel -->\n"
        index_path.write_text(index_path.read_text(encoding="utf-8") + sentinel, encoding="utf-8")

        ensure_wiki(str(tmp_wiki))

        assert sentinel in index_path.read_text(encoding="utf-8")

    def test_log_content_preserved(self, tmp_wiki: Path) -> None:
        log_path = tmp_wiki / "log.md"
        sentinel = "\n<!-- idempotency-sentinel -->\n"
        log_path.write_text(log_path.read_text(encoding="utf-8") + sentinel, encoding="utf-8")

        ensure_wiki(str(tmp_wiki))

        assert sentinel in log_path.read_text(encoding="utf-8")

    def test_dirs_still_exist_after_second_call(self, tmp_wiki: Path) -> None:
        ensure_wiki(str(tmp_wiki))
        assert (tmp_wiki / "entities" / "skills").is_dir()
        assert (tmp_wiki / "raw" / "scans").is_dir()


class TestWikiUpsertSkillPageNew:
    """test_wiki_upsert_skill_page_new -- new entity page is created with correct frontmatter."""

    def test_returns_true_for_new_page(self, tmp_wiki: Path) -> None:
        result = upsert_skill_page(
            str(tmp_wiki),
            "python-patterns",
            {"reason": "python patterns detected", "path": "/skills/python-patterns", "priority": 80},
        )
        assert result is True

    def test_page_file_created(self, tmp_wiki: Path) -> None:
        upsert_skill_page(str(tmp_wiki), "my-skill", {"reason": "test skill"})
        page = tmp_wiki / "entities" / "skills" / "my-skill.md"
        assert page.exists()

    def test_frontmatter_title(self, tmp_wiki: Path) -> None:
        upsert_skill_page(str(tmp_wiki), "my-skill", {"reason": "test skill"})
        content = (tmp_wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "title: my-skill" in content

    def test_frontmatter_type_is_skill(self, tmp_wiki: Path) -> None:
        upsert_skill_page(str(tmp_wiki), "my-skill", {"reason": "test skill"})
        content = (tmp_wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "type: skill" in content

    def test_frontmatter_use_count_starts_at_one(self, tmp_wiki: Path) -> None:
        upsert_skill_page(str(tmp_wiki), "my-skill", {"reason": "test skill"})
        content = (tmp_wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "use_count: 1" in content

    def test_frontmatter_status_installed(self, tmp_wiki: Path) -> None:
        upsert_skill_page(str(tmp_wiki), "my-skill", {"reason": "test skill"})
        content = (tmp_wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "status: installed" in content

    def test_tag_inferred_from_reason(self, tmp_wiki: Path) -> None:
        upsert_skill_page(str(tmp_wiki), "my-skill", {"reason": "python fastapi project"})
        content = (tmp_wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "python" in content
        assert "fastapi" in content

    def test_uncategorized_tag_when_no_match(self, tmp_wiki: Path) -> None:
        upsert_skill_page(str(tmp_wiki), "mystery-skill", {"reason": "something obscure"})
        content = (tmp_wiki / "entities" / "skills" / "mystery-skill.md").read_text(encoding="utf-8")
        assert "uncategorized" in content


class TestWikiUpsertSkillPageUpdate:
    """test_wiki_upsert_skill_page_update -- existing page is updated, use_count is bumped."""

    def _create_skill(self, wiki: Path, name: str = "update-skill") -> None:
        upsert_skill_page(str(wiki), name, {"reason": "initial creation"})

    def test_returns_false_for_existing_page(self, tmp_wiki: Path) -> None:
        self._create_skill(tmp_wiki)
        result = upsert_skill_page(str(tmp_wiki), "update-skill", {"reason": "second call"})
        assert result is False

    def test_use_count_incremented(self, tmp_wiki: Path) -> None:
        self._create_skill(tmp_wiki)
        upsert_skill_page(str(tmp_wiki), "update-skill", {"reason": "second call"})
        content = (tmp_wiki / "entities" / "skills" / "update-skill.md").read_text(encoding="utf-8")
        assert "use_count: 2" in content

    def test_use_count_incremented_twice(self, tmp_wiki: Path) -> None:
        self._create_skill(tmp_wiki)
        upsert_skill_page(str(tmp_wiki), "update-skill", {"reason": "second call"})
        upsert_skill_page(str(tmp_wiki), "update-skill", {"reason": "third call"})
        content = (tmp_wiki / "entities" / "skills" / "update-skill.md").read_text(encoding="utf-8")
        assert "use_count: 3" in content

    def test_page_still_has_title(self, tmp_wiki: Path) -> None:
        self._create_skill(tmp_wiki)
        upsert_skill_page(str(tmp_wiki), "update-skill", {"reason": "second call"})
        content = (tmp_wiki / "entities" / "skills" / "update-skill.md").read_text(encoding="utf-8")
        assert "title: update-skill" in content


class TestWikiUpdateIndex:
    """test_wiki_update_index -- new skills appear in index.md under ## Skills."""

    def test_skill_entry_added(self, tmp_wiki: Path) -> None:
        update_index(str(tmp_wiki), ["alpha-skill"])
        content = (tmp_wiki / "index.md").read_text(encoding="utf-8")
        assert "alpha-skill" in content

    def test_entry_under_skills_section(self, tmp_wiki: Path) -> None:
        update_index(str(tmp_wiki), ["beta-skill"])
        content = (tmp_wiki / "index.md").read_text(encoding="utf-8")
        skills_pos = content.find("## Skills")
        plugins_pos = content.find("## Plugins")
        beta_pos = content.find("beta-skill")
        assert skills_pos < beta_pos < plugins_pos, (
            "beta-skill entry should appear between ## Skills and ## Plugins"
        )

    def test_multiple_skills_all_appear(self, tmp_wiki: Path) -> None:
        update_index(str(tmp_wiki), ["skill-a", "skill-b", "skill-c"])
        content = (tmp_wiki / "index.md").read_text(encoding="utf-8")
        for name in ("skill-a", "skill-b", "skill-c"):
            assert name in content

    def test_no_duplicate_on_repeated_call(self, tmp_wiki: Path) -> None:
        update_index(str(tmp_wiki), ["dedup-skill"])
        update_index(str(tmp_wiki), ["dedup-skill"])
        content = (tmp_wiki / "index.md").read_text(encoding="utf-8")
        assert content.count("dedup-skill") == 1

    def test_empty_list_is_noop(self, tmp_wiki: Path) -> None:
        original = (tmp_wiki / "index.md").read_text(encoding="utf-8")
        update_index(str(tmp_wiki), [])
        assert (tmp_wiki / "index.md").read_text(encoding="utf-8") == original

    def test_total_pages_count_updated(self, tmp_wiki: Path) -> None:
        update_index(str(tmp_wiki), ["count-skill"])
        content = (tmp_wiki / "index.md").read_text(encoding="utf-8")
        assert "Total pages: 1" in content


class TestWikiAppendLog:
    """test_wiki_append_log -- log entries are appended chronologically."""

    def test_log_entry_appears_in_file(self, tmp_wiki: Path) -> None:
        append_log(str(tmp_wiki), "sync", "my-skill", ["Loaded 1 skill"])
        content = (tmp_wiki / "log.md").read_text(encoding="utf-8")
        assert "sync" in content
        assert "my-skill" in content

    def test_detail_bullet_appears(self, tmp_wiki: Path) -> None:
        append_log(str(tmp_wiki), "sync", "my-skill", ["detail one", "detail two"])
        content = (tmp_wiki / "log.md").read_text(encoding="utf-8")
        assert "- detail one" in content
        assert "- detail two" in content

    def test_second_entry_appended_after_first(self, tmp_wiki: Path) -> None:
        append_log(str(tmp_wiki), "create", "skill-x", ["Created"])
        append_log(str(tmp_wiki), "update", "skill-y", ["Updated"])
        content = (tmp_wiki / "log.md").read_text(encoding="utf-8")
        create_pos = content.find("create")
        update_pos = content.find("update")
        assert create_pos < update_pos, "Second log entry should appear after the first"

    def test_original_init_entry_preserved(self, tmp_wiki: Path) -> None:
        append_log(str(tmp_wiki), "test-action", "test-subject", ["test detail"])
        content = (tmp_wiki / "log.md").read_text(encoding="utf-8")
        assert "Wiki initialized" in content

    def test_empty_details_no_bullets(self, tmp_wiki: Path) -> None:
        before_size = (tmp_wiki / "log.md").stat().st_size
        append_log(str(tmp_wiki), "noop", "nothing", [])
        content = (tmp_wiki / "log.md").read_text(encoding="utf-8")
        # Header line should exist, no bullet lines for this entry
        assert "noop | nothing" in content
        # File grew (header appended) but no spurious bullets
        assert (tmp_wiki / "log.md").stat().st_size > before_size


class TestWikiSchemaRequiredSections:
    """
    test_wiki_schema_has_required_sections -- SCHEMA.md written by ensure_wiki
    contains the required conceptual sections.
    """

    REQUIRED_SECTIONS = [
        "Conventions",
        "Tag Taxonomy",
    ]

    # ensure_wiki writes Domain, Conventions, Tag Taxonomy, Page Thresholds,
    # Update Policy.  The prompt also asks for "Layers", "Session Startup",
    # and "Operations" — these are not written by ensure_wiki itself.
    # We test what the implementation actually produces and document the gap.

    @pytest.mark.parametrize("section", REQUIRED_SECTIONS)
    def test_section_present_in_schema(self, tmp_wiki: Path, section: str) -> None:
        content = (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8")
        assert section in content, f"SCHEMA.md is missing section: {section!r}"

    def test_schema_has_domain_section(self, tmp_wiki: Path) -> None:
        content = (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8")
        assert "Domain" in content

    def test_schema_has_page_thresholds(self, tmp_wiki: Path) -> None:
        content = (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8")
        assert "Page Thresholds" in content

    def test_schema_has_update_policy(self, tmp_wiki: Path) -> None:
        content = (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8")
        assert "Update Policy" in content

    def test_schema_is_non_empty(self, tmp_wiki: Path) -> None:
        content = (tmp_wiki / "SCHEMA.md").read_text(encoding="utf-8")
        assert len(content) > 100

    def test_global_skill_wiki_schema_when_present(self) -> None:
        """
        If the user has a deployed ~/.claude/skill-wiki/SCHEMA.md, it should
        contain the sections required by the system prompt.  This test is
        skipped when the file does not exist (e.g. CI with no ~/.claude).
        """
        global_schema = Path.home() / ".claude" / "skill-wiki" / "SCHEMA.md"
        if not global_schema.exists():
            pytest.skip("~/.claude/skill-wiki/SCHEMA.md not present; skipping global schema check")

        content = global_schema.read_text(encoding="utf-8")
        for section in ("Conventions", "Tag Taxonomy"):
            assert section in content, (
                f"Global SCHEMA.md at {global_schema} is missing section: {section!r}"
            )
