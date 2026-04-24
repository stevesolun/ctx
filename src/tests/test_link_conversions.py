"""
tests/test_link_conversions.py -- pytest suite for link_conversions module.

Covers:
  - _set_field                  (replace existing, add new, no frontmatter)
  - _inject_pipeline_fields     (sets has_pipeline/pipeline_path/pipeline_converted)
  - scan_converted              (happy, empty dir, missing dir)
  - _infer_tags                 (tag detection, fallback uncategorized)
  - _read_pipeline_description  (from SKILL.md frontmatter, missing file, no description)
  - _build_new_entity_page      (renders valid content)
  - upsert_entity_page          (create new, update existing)
  - update_index                (adds entries, skips existing)
  - append_log                  (creates log entry)
  - generate_converted_index    (renders table)
  - run()                       (integration: missing wiki, full pipeline)
  - main()                      (via argv)
"""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import link_conversions as _lc
from link_conversions import (
    ConvertedSkill,
    ProcessResult,
    _build_new_entity_page,
    _inject_pipeline_fields,
    _infer_tags,
    _read_pipeline_description,
    _set_field,
    append_log,
    generate_converted_index,
    run,
    scan_converted,
    update_index,
    upsert_entity_page,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wiki(tmp_path: Path) -> Path:
    """Create minimal wiki directory structure."""
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "skills").mkdir(parents=True)
    (wiki / "converted").mkdir(parents=True)
    (wiki / "index.md").write_text(
        "# Index\n\n## Skills\n\n## Total pages: 0 | Last updated: 2024-01-01\n",
        encoding="utf-8",
    )
    (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
    return wiki


def _make_converted_skill(wiki: Path, name: str, skill_md_content: str = "") -> ConvertedSkill:
    """Create a converted skill dir and return a ConvertedSkill dataclass."""
    d = wiki / "converted" / name
    d.mkdir(parents=True, exist_ok=True)
    if skill_md_content:
        (d / "SKILL.md").write_text(skill_md_content, encoding="utf-8")
    return ConvertedSkill(name=name, pipeline_path=f"converted/{name}/", abs_dir=d)


# ---------------------------------------------------------------------------
# _set_field
# ---------------------------------------------------------------------------

class TestSetField:
    def test_replaces_existing_field(self):
        content = "---\nhas_pipeline: false\n---\n# body\n"
        result = _set_field(content, "has_pipeline", "true")
        assert "has_pipeline: true" in result
        assert "has_pipeline: false" not in result

    def test_adds_field_before_closing_dashes(self):
        content = "---\ntitle: test\n---\n# body\n"
        result = _set_field(content, "new_field", "value")
        assert "new_field: value" in result
        # Frontmatter structure preserved
        assert result.count("---") >= 2

    def test_no_frontmatter_prepends_minimal_block(self):
        content = "# Just body content\n"
        result = _set_field(content, "has_pipeline", "true")
        assert "has_pipeline: true" in result
        assert "---" in result

    def test_multiline_content_only_changes_target(self):
        content = "---\nhas_pipeline: false\npipeline_path: old\n---\n# body\n"
        result = _set_field(content, "has_pipeline", "true")
        assert "pipeline_path: old" in result
        assert "has_pipeline: true" in result


# ---------------------------------------------------------------------------
# _inject_pipeline_fields
# ---------------------------------------------------------------------------

class TestInjectPipelineFields:
    def test_sets_all_three_fields(self):
        content = "---\ntitle: test\n---\n# body\n"
        result = _inject_pipeline_fields(content, "converted/test/")
        assert "has_pipeline: true" in result
        assert "pipeline_path: converted/test/" in result
        assert "pipeline_converted:" in result

    def test_updates_existing_pipeline_path(self):
        content = "---\nhas_pipeline: false\npipeline_path: old\n---\n# body\n"
        result = _inject_pipeline_fields(content, "converted/new/")
        assert "pipeline_path: converted/new/" in result


# ---------------------------------------------------------------------------
# scan_converted
# ---------------------------------------------------------------------------

class TestScanConverted:
    def test_finds_converted_dirs(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        (wiki / "converted" / "react").mkdir()
        (wiki / "converted" / "docker").mkdir()
        skills = scan_converted(wiki)
        names = [s.name for s in skills]
        assert "react" in names
        assert "docker" in names

    def test_empty_converted_dir_returns_empty(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skills = scan_converted(wiki)
        assert skills == []

    def test_missing_converted_dir_returns_empty(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        skills = scan_converted(wiki)
        assert skills == []

    def test_sorted_order(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        for name in ("zebra", "apple", "mango"):
            (wiki / "converted" / name).mkdir()
        skills = scan_converted(wiki)
        names = [s.name for s in skills]
        assert names == sorted(names)

    def test_files_in_converted_dir_ignored(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        (wiki / "converted" / "not-a-dir.txt").write_text("hello")
        skills = scan_converted(wiki)
        assert all(s.name != "not-a-dir.txt" for s in skills)


# ---------------------------------------------------------------------------
# _infer_tags
# ---------------------------------------------------------------------------

class TestInferTags:
    def test_python_detected(self):
        assert "python" in _infer_tags("python-testing")

    def test_react_detected(self):
        assert "react" in _infer_tags("react-hooks")

    def test_fallback_uncategorized(self):
        tags = _infer_tags("totally-unique-name")
        assert tags == ["uncategorized"]

    def test_multiple_tags(self):
        tags = _infer_tags("fastapi-sqlalchemy")
        assert "fastapi" in tags
        assert "sql" in tags

    def test_hyphen_and_underscore_treated_as_space(self):
        tags = _infer_tags("docker_compose")
        assert "docker" in tags


# ---------------------------------------------------------------------------
# _read_pipeline_description
# ---------------------------------------------------------------------------

class TestReadPipelineDescription:
    def test_reads_description_from_skill_md(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "myskill",
            '---\ndescription: "Test description"\n---\n# myskill\n')
        desc = _read_pipeline_description(skill)
        assert desc == "Test description"

    def test_missing_skill_md_returns_empty(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "nofile")
        # No SKILL.md in the dir
        desc = _read_pipeline_description(skill)
        assert desc == ""

    def test_no_description_field_returns_empty(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "nodesc", "---\ntitle: nodesc\n---\n# nodesc\n")
        assert _read_pipeline_description(skill) == ""


# ---------------------------------------------------------------------------
# _build_new_entity_page
# ---------------------------------------------------------------------------

class TestBuildNewEntityPage:
    def test_contains_skill_name(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "react-hooks")
        skills_dir = tmp_path / "skills"
        content = _build_new_entity_page(skill, skills_dir)
        assert "react-hooks" in content

    def test_has_frontmatter(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "myskill")
        content = _build_new_entity_page(skill, tmp_path / "skills")
        assert content.startswith("---\n")
        assert "has_pipeline: true" in content

    def test_original_note_when_not_found(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "myskill")
        content = _build_new_entity_page(skill, tmp_path / "skills")
        assert "not found" in content

    def test_original_note_when_found(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "myskill")
        skills_dir = tmp_path / "skills"
        skill_original = skills_dir / "myskill"
        skill_original.mkdir(parents=True)
        (skill_original / "SKILL.md").write_text("# myskill\n")
        content = _build_new_entity_page(skill, skills_dir)
        assert "Original skill file:" in content


# ---------------------------------------------------------------------------
# upsert_entity_page
# ---------------------------------------------------------------------------

class TestUpsertEntityPage:
    def test_creates_new_page(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "new-skill")
        skills_dir = tmp_path / "skills"
        is_new = upsert_entity_page(wiki, skill, skills_dir)
        assert is_new is True
        page = wiki / "entities" / "skills" / "new-skill.md"
        assert page.exists()

    def test_updates_existing_page(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "react")
        # Pre-create entity page without pipeline fields
        existing = wiki / "entities" / "skills" / "react.md"
        existing.write_text("---\ntitle: react\nhas_pipeline: false\n---\n# react\n", encoding="utf-8")
        skills_dir = tmp_path / "skills"
        is_new = upsert_entity_page(wiki, skill, skills_dir)
        assert is_new is False
        content = existing.read_text()
        assert "has_pipeline: true" in content

    def test_update_injects_pipeline_path(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skill = _make_converted_skill(wiki, "fastapi")
        existing = wiki / "entities" / "skills" / "fastapi.md"
        existing.write_text("---\ntitle: fastapi\n---\n# fastapi\n", encoding="utf-8")
        upsert_entity_page(wiki, skill, tmp_path / "skills")
        content = existing.read_text()
        assert "pipeline_path: converted/fastapi/" in content


# ---------------------------------------------------------------------------
# update_index
# ---------------------------------------------------------------------------

class TestUpdateIndex:
    def test_adds_new_skill_entry(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        update_index(wiki, ["react"])
        content = (wiki / "index.md").read_text()
        assert "[[entities/skills/react]]" in content

    def test_skips_empty_list(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        original = (wiki / "index.md").read_text()
        update_index(wiki, [])
        assert (wiki / "index.md").read_text() == original

    def test_does_not_duplicate_existing_entry(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        update_index(wiki, ["react"])
        update_index(wiki, ["react"])  # second call
        content = (wiki / "index.md").read_text()
        assert content.count("[[entities/skills/react]]") == 1

    def test_multiple_skills_added(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        update_index(wiki, ["react", "docker"])
        content = (wiki / "index.md").read_text()
        assert "[[entities/skills/react]]" in content
        assert "[[entities/skills/docker]]" in content


class TestUpdateIndexSubjectAware:
    """Subject-aware index updates for agents and mcp-servers.

    Tests the ``wiki_sync.update_index`` (note: ``link_conversions``
    has its own narrower update_index for the conversion pipeline,
    which intentionally remains skills-only). The function defaults
    to ``subject_type="skills"`` for backward compat with all the
    legacy callers. These tests cover the new subject-aware paths.
    """

    @staticmethod
    def _ws_update_index(*args: Any, **kwargs: Any) -> None:
        # Imported locally so the module-level import block above
        # (which pulls update_index from link_conversions) stays
        # untouched — that is the function the existing TestUpdateIndex
        # class still validates.
        from ctx.core.wiki.wiki_sync import update_index as _ws_ui  # noqa: PLC0415
        _ws_ui(*args, **kwargs)

    def _wiki_with_all_sections(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        (wiki / "entities" / "skills").mkdir(parents=True)
        (wiki / "entities" / "agents").mkdir(parents=True)
        (wiki / "entities" / "mcp-servers").mkdir(parents=True)
        (wiki / "converted").mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n"
            "## Total pages: 0 | Last updated: 2024-01-01\n\n"
            "## Skills\n\n"
            "## Agents\n\n"
            "## MCP Servers\n",
            encoding="utf-8",
        )
        (wiki / "log.md").write_text("# Log\n", encoding="utf-8")
        return wiki

    def test_agent_lands_in_agents_section(self, tmp_path):
        wiki = self._wiki_with_all_sections(tmp_path)
        self._ws_update_index(str(wiki), ["code-reviewer"], subject_type="agents")
        content = (wiki / "index.md").read_text()
        assert "[[entities/agents/code-reviewer]]" in content
        # Should NOT be in skills section format
        assert "[[entities/skills/code-reviewer]]" not in content

    def test_mcp_server_uses_sharded_path(self, tmp_path):
        wiki = self._wiki_with_all_sections(tmp_path)
        self._ws_update_index(str(wiki), ["github-mcp"], subject_type="mcp-servers")
        content = (wiki / "index.md").read_text()
        assert "[[entities/mcp-servers/g/github-mcp]]" in content

    def test_mcp_server_digit_slug_lands_in_0_9_shard(self, tmp_path):
        wiki = self._wiki_with_all_sections(tmp_path)
        self._ws_update_index(str(wiki), ["007-server"], subject_type="mcp-servers")
        content = (wiki / "index.md").read_text()
        assert "[[entities/mcp-servers/0-9/007-server]]" in content

    def test_unknown_subject_type_raises(self, tmp_path):
        wiki = self._wiki_with_all_sections(tmp_path)
        with pytest.raises(ValueError, match="unknown subject_type"):
            self._ws_update_index(str(wiki), ["foo"], subject_type="widgets")

    def test_missing_section_is_created(self, tmp_path):
        # Wiki built before ## Agents section was templated in.
        wiki = tmp_path / "legacy-wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text(
            "# Index\n\n## Total pages: 0\n\n## Skills\n",
            encoding="utf-8",
        )
        self._ws_update_index(str(wiki), ["new-agent"], subject_type="agents")
        content = (wiki / "index.md").read_text()
        assert "## Agents" in content
        assert "[[entities/agents/new-agent]]" in content

    def test_total_pages_counts_across_all_sections(self, tmp_path):
        wiki = self._wiki_with_all_sections(tmp_path)
        self._ws_update_index(str(wiki), ["alpha-skill"], subject_type="skills")
        self._ws_update_index(str(wiki), ["beta-agent"], subject_type="agents")
        self._ws_update_index(str(wiki), ["gamma-mcp"], subject_type="mcp-servers")
        content = (wiki / "index.md").read_text()
        assert "Total pages: 3" in content

    def test_default_subject_type_is_skills(self, tmp_path):
        # Backward compat: callers that don't pass subject_type get
        # the legacy ## Skills behavior.
        wiki = self._wiki_with_all_sections(tmp_path)
        self._ws_update_index(str(wiki), ["legacy-skill"])  # no subject_type
        content = (wiki / "index.md").read_text()
        assert "[[entities/skills/legacy-skill]]" in content


# ---------------------------------------------------------------------------
# append_log
# ---------------------------------------------------------------------------

class TestAppendLog:
    def test_creates_log_entry(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        append_log(wiki, "test-action", "test-subject", ["detail1", "detail2"])
        content = (wiki / "log.md").read_text()
        assert "test-action" in content
        assert "test-subject" in content
        assert "detail1" in content

    def test_appends_to_existing_log(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        append_log(wiki, "action-a", "subject-a", ["d1"])
        append_log(wiki, "action-b", "subject-b", ["d2"])
        content = (wiki / "log.md").read_text()
        assert "action-a" in content
        assert "action-b" in content


# ---------------------------------------------------------------------------
# generate_converted_index
# ---------------------------------------------------------------------------

class TestGenerateConvertedIndex:
    def test_creates_index_file(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skills = [
            ConvertedSkill("react", "converted/react/", wiki / "converted/react"),
            ConvertedSkill("docker", "converted/docker/", wiki / "converted/docker"),
        ]
        generate_converted_index(wiki, skills)
        index = wiki / "converted-index.md"
        assert index.exists()
        content = index.read_text()
        assert "react" in content
        assert "docker" in content

    def test_table_headers_present(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        generate_converted_index(wiki, [])
        content = (wiki / "converted-index.md").read_text()
        assert "| Skill |" in content

    def test_total_count_in_header(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        skills = [ConvertedSkill("s1", "converted/s1/", wiki / "converted/s1")]
        generate_converted_index(wiki, skills)
        content = (wiki / "converted-index.md").read_text()
        assert "Total: 1" in content


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

class TestRun:
    def test_missing_wiki_returns_error(self, tmp_path):
        result = run(tmp_path / "no-wiki", tmp_path / "skills")
        assert len(result.errors) == 1
        assert "not found" in result.errors[0]

    def test_empty_converted_dir_returns_empty_result(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        result = run(wiki, tmp_path / "skills")
        assert result.created == []
        assert result.updated == []
        assert result.errors == []

    def test_creates_new_entity_pages(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _make_converted_skill(wiki, "react")
        _make_converted_skill(wiki, "docker")
        result = run(wiki, tmp_path / "skills")
        assert "react" in result.created
        assert "docker" in result.created
        assert result.errors == []

    def test_updates_existing_pages(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _make_converted_skill(wiki, "fastapi")
        # Pre-create an existing entity page
        page = wiki / "entities" / "skills" / "fastapi.md"
        page.write_text("---\ntitle: fastapi\nhas_pipeline: false\n---\n# fastapi\n")
        result = run(wiki, tmp_path / "skills")
        assert "fastapi" in result.updated
        assert result.errors == []

    def test_converted_index_generated(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _make_converted_skill(wiki, "react")
        run(wiki, tmp_path / "skills")
        assert (wiki / "converted-index.md").exists()

    def test_log_entry_appended(self, tmp_path):
        wiki = _make_wiki(tmp_path)
        _make_converted_skill(wiki, "react")
        run(wiki, tmp_path / "skills")
        log_content = (wiki / "log.md").read_text()
        assert "link-conversions" in log_content


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_runs_with_valid_wiki(self, tmp_path, monkeypatch, capsys):
        wiki = _make_wiki(tmp_path)
        _make_converted_skill(wiki, "react")
        monkeypatch.setattr(sys, "argv", [
            "link_conversions.py",
            "--wiki", str(wiki),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        _lc.main()
        out = capsys.readouterr().out
        assert "Done." in out

    def test_exits_1_on_errors(self, tmp_path, monkeypatch, capsys):
        wiki = _make_wiki(tmp_path)
        _make_converted_skill(wiki, "react")
        # Corrupt the entity page to trigger an error path
        # We inject a bad upsert by removing entity dir permissions is too platform-specific,
        # so instead test with missing wiki — run() returns error
        monkeypatch.setattr(sys, "argv", [
            "link_conversions.py",
            "--wiki", str(tmp_path / "no-wiki"),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        with pytest.raises(SystemExit) as exc:
            _lc.main()
        assert exc.value.code == 1
