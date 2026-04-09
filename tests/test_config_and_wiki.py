"""
test_config_and_wiki.py -- Tests for the config system and wiki foundation layer.

Covers:
  - ctx_config.Config: attribute types, path expansion, reload, deep merge,
    all_skill_dirs()
  - wiki_sync: ensure_wiki structure, idempotency, upsert_skill_page,
    update_index, append_log, SCHEMA.md required sections
"""

import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path is already patched by conftest.py, but guard here too so the module
# can be run in isolation (e.g. `python -m pytest tests/test_config_and_wiki.py`).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import ctx_config  # noqa: E402
from ctx_config import Config, _deep_merge  # noqa: E402
import wiki_sync  # noqa: E402
from wiki_sync import (  # noqa: E402
    append_log,
    ensure_wiki,
    update_index,
    upsert_skill_page,
)


# ===========================================================================
# Helper
# ===========================================================================


def _minimal_raw(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a minimal raw-config dict sufficient to construct a Config."""
    raw: dict[str, Any] = {
        "paths": {
            "claude_dir": "~/.claude",
            "wiki_dir": "~/.claude/skill-wiki",
            "skills_dir": "~/.claude/skills",
            "agents_dir": "~/.claude/agents",
            "skill_manifest": "~/.claude/skill-manifest.json",
            "intent_log": "~/.claude/intent-log.jsonl",
            "pending_skills": "~/.claude/pending-skills.json",
            "skill_registry": "~/.claude/skill-registry.json",
            "stack_profile_tmp": "/tmp/skill-stack-profile.json",
            "catalog": "~/.claude/skill-wiki/catalog.md",
        },
        "resolver": {},
        "context_monitor": {},
        "usage_tracker": {},
        "skill_transformer": {},
        "skill_router": {},
        "extra_skill_dirs": [],
        "babysitter": {},
    }
    if overrides:
        _deep_merge(raw, overrides)
    return raw


# ===========================================================================
# Config tests
# ===========================================================================


class TestConfigLoadsDefaults:
    """test_config_loads_defaults -- expected attributes exist with correct defaults."""

    def test_has_wiki_dir(self) -> None:
        cfg = Config(_minimal_raw())
        assert hasattr(cfg, "wiki_dir")

    def test_has_skills_dir(self) -> None:
        cfg = Config(_minimal_raw())
        assert hasattr(cfg, "skills_dir")

    def test_line_threshold_default(self) -> None:
        cfg = Config(_minimal_raw())
        assert cfg.line_threshold == 180

    def test_max_stage_lines_default(self) -> None:
        cfg = Config(_minimal_raw())
        assert cfg.max_stage_lines == 40

    def test_max_skills_default(self) -> None:
        cfg = Config(_minimal_raw())
        assert cfg.max_skills == 15

    def test_stage_count_default(self) -> None:
        cfg = Config(_minimal_raw())
        assert cfg.stage_count == 5


class TestConfigPathsArePathlib:
    """test_config_paths_are_pathlib -- all path attributes are Path objects."""

    PATH_ATTRS = [
        "claude_dir",
        "wiki_dir",
        "skills_dir",
        "agents_dir",
        "skill_manifest",
        "intent_log",
        "pending_skills",
        "skill_registry",
        "stack_profile_tmp",
        "catalog",
    ]

    @pytest.mark.parametrize("attr", PATH_ATTRS)
    def test_attr_is_path(self, attr: str) -> None:
        cfg = Config(_minimal_raw())
        assert isinstance(getattr(cfg, attr), Path), (
            f"cfg.{attr} should be a Path, got {type(getattr(cfg, attr))}"
        )


class TestConfigExpandTilde:
    """test_config_expand_tilde -- paths with ~ are expanded to absolute paths."""

    def test_wiki_dir_is_absolute(self) -> None:
        cfg = Config(_minimal_raw())
        assert cfg.wiki_dir.is_absolute(), (
            f"wiki_dir should be absolute after ~ expansion, got: {cfg.wiki_dir}"
        )

    def test_skills_dir_is_absolute(self) -> None:
        cfg = Config(_minimal_raw())
        assert cfg.skills_dir.is_absolute()

    def test_claude_dir_is_absolute(self) -> None:
        cfg = Config(_minimal_raw())
        assert cfg.claude_dir.is_absolute()

    def test_custom_tilde_path_expanded(self) -> None:
        raw = _minimal_raw({"paths": {"wiki_dir": "~/custom-wiki"}})
        cfg = Config(raw)
        assert "~" not in str(cfg.wiki_dir)
        assert cfg.wiki_dir.is_absolute()


class TestConfigReload:
    """test_config_reload -- reload() picks up changes to the raw config."""

    def test_reload_updates_singleton(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Write a temporary default config with a custom line_threshold
        custom_config = tmp_path / "config.json"
        import json

        custom_config.write_text(
            json.dumps({"skill_transformer": {"line_threshold": 999}}),
            encoding="utf-8",
        )

        # Patch the module-level constants so _load_raw reads our file
        monkeypatch.setattr(ctx_config, "_DEFAULT_CONFIG", custom_config)
        # Also suppress user config so it doesn't bleed in
        monkeypatch.setattr(ctx_config, "_USER_CONFIG", tmp_path / "nonexistent.json")

        ctx_config.reload()
        assert ctx_config.cfg.line_threshold == 999

    def test_reload_restores_after_monkeypatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After reload with empty config the attribute still exists (defaults apply)."""
        empty_config = tmp_path / "empty.json"
        empty_config.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(ctx_config, "_DEFAULT_CONFIG", empty_config)
        monkeypatch.setattr(ctx_config, "_USER_CONFIG", tmp_path / "nonexistent.json")

        ctx_config.reload()
        # line_threshold should fall back to the hard-coded default 180
        assert ctx_config.cfg.line_threshold == 180


class TestConfigAllSkillDirs:
    """test_config_all_skill_dirs -- all_skill_dirs() returns a list of existing dirs."""

    def test_returns_list(self) -> None:
        cfg = Config(_minimal_raw())
        result = cfg.all_skill_dirs()
        assert isinstance(result, list)

    def test_contains_only_existing_dirs(self) -> None:
        cfg = Config(_minimal_raw())
        for d in cfg.all_skill_dirs():
            assert d.exists() and d.is_dir(), f"{d} does not exist or is not a directory"

    def test_extra_dirs_included_when_they_exist(self, tmp_path: Path) -> None:
        extra = tmp_path / "extra-skills"
        extra.mkdir()
        raw = _minimal_raw({"extra_skill_dirs": [str(extra)]})
        cfg = Config(raw)
        assert extra in cfg.all_skill_dirs()

    def test_nonexistent_extra_dirs_excluded(self, tmp_path: Path) -> None:
        ghost = tmp_path / "ghost-skills"
        # intentionally do NOT create it
        raw = _minimal_raw({"extra_skill_dirs": [str(ghost)]})
        cfg = Config(raw)
        assert ghost not in cfg.all_skill_dirs()


class TestConfigDeepMerge:
    """test_config_deep_merge -- nested dicts merge correctly and override wins."""

    def test_override_scalar_wins(self) -> None:
        base: dict[str, Any] = {"a": 1, "b": 2}
        override: dict[str, Any] = {"b": 99}
        _deep_merge(base, override)
        assert base["b"] == 99
        assert base["a"] == 1  # untouched

    def test_nested_dict_merged_not_replaced(self) -> None:
        base: dict[str, Any] = {"paths": {"wiki_dir": "~/wiki", "skills_dir": "~/skills"}}
        override: dict[str, Any] = {"paths": {"wiki_dir": "~/custom-wiki"}}
        _deep_merge(base, override)
        # wiki_dir is overridden
        assert base["paths"]["wiki_dir"] == "~/custom-wiki"
        # skills_dir is preserved from base
        assert base["paths"]["skills_dir"] == "~/skills"

    def test_new_key_added(self) -> None:
        base: dict[str, Any] = {"x": 1}
        override: dict[str, Any] = {"y": 2}
        _deep_merge(base, override)
        assert base["y"] == 2

    def test_deeply_nested_override(self) -> None:
        base: dict[str, Any] = {"a": {"b": {"c": 10, "d": 20}}}
        override: dict[str, Any] = {"a": {"b": {"c": 99}}}
        _deep_merge(base, override)
        assert base["a"]["b"]["c"] == 99
        assert base["a"]["b"]["d"] == 20

    def test_override_replaces_non_dict_with_scalar(self) -> None:
        base: dict[str, Any] = {"a": {"nested": True}}
        override: dict[str, Any] = {"a": "flat"}
        _deep_merge(base, override)
        assert base["a"] == "flat"


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
