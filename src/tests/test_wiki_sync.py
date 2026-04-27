"""
test_wiki_sync.py -- Comprehensive coverage sprint for wiki_sync.py (468 LOC).

wiki_sync owns every write path into the skill wiki: directory scaffolding,
raw scan persistence, skill entity creation/update, index management, log
append, usage tracking, and stale marking.  A regression in any of these
silently corrupts the wiki that every downstream router and query depends on.

Test layout
-----------
TestEnsureWiki          -- ensure_wiki(): idempotency, dirs, seed files
TestSaveScan            -- save_scan(): filename convention, JSON round-trip
TestSanitizeYamlValue   -- _sanitize_yaml_value(): injection prevention
TestUpsertSkillPage     -- upsert_skill_page(): create, update, validation
TestEntityIndexLink     -- _entity_index_link(): shard logic per subject type
TestUpdateIndex         -- update_index(): insert, dedup, counter, missing sections
TestAppendLog           -- append_log(): file creation, append semantics
TestUpsertUsage         -- upsert_usage(): count increment, session_count init
TestMarkStale           -- mark_stale(): status replacement, missing-file guard

Each class uses pytest tmp_path so the real user home is never touched.
Monkeypatching is used to pin the module-level TODAY constant where date
stability matters for assertions.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ctx.core.wiki import wiki_sync
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_DATE = "2024-01-15"


def _pin_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix wiki_sync.TODAY to a stable date for deterministic assertions."""
    monkeypatch.setattr(wiki_sync, "TODAY", _FIXED_DATE)


def _minimal_skill_page(date: str = _FIXED_DATE) -> str:
    """Return the smallest valid skill page that upsert_skill_page can update."""
    return (
        f"---\n"
        f"title: my-skill\n"
        f"updated: {date}\n"
        f"use_count: 3\n"
        f"last_used: {date}\n"
        f"status: installed\n"
        f"---\n\n"
        f"# my-skill\n"
    )


def _minimal_index(date: str = _FIXED_DATE) -> str:
    return (
        f"# Skill Wiki Index\n\n"
        f"> Last updated: {date} | Total pages: 0\n\n"
        f"## Skills\n\n"
        f"## Agents\n\n"
        f"## Plugins\n\n"
        f"## MCP Servers\n\n"
        f"## Concepts\n\n"
    )


def _symlink_to(target: Path, link: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")


# ---------------------------------------------------------------------------
# TestEnsureWiki
# ---------------------------------------------------------------------------


class TestEnsureWiki:
    def test_creates_all_required_directories(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))

        expected_dirs = [
            wiki,
            wiki / "raw" / "scans",
            wiki / "raw" / "marketplace-dumps",
            wiki / "entities" / "skills",
            wiki / "entities" / "plugins",
            wiki / "entities" / "mcp-servers",
            wiki / "concepts",
            wiki / "comparisons",
            wiki / "queries",
        ]
        for d in expected_dirs:
            assert d.is_dir(), f"Missing directory: {d}"

    def test_creates_schema_md(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        schema = wiki / "SCHEMA.md"
        assert schema.exists()
        text = schema.read_text(encoding="utf-8")
        assert "# Skill Wiki Schema" in text
        assert "Tag Taxonomy" in text

    def test_creates_index_md(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        index = wiki / "index.md"
        assert index.exists()
        text = index.read_text(encoding="utf-8")
        assert "# Skill Wiki Index" in text
        assert "## Skills" in text

    def test_creates_log_md(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        log = wiki / "log.md"
        assert log.exists()
        text = log.read_text(encoding="utf-8")
        assert "# Skill Wiki Log" in text
        assert "Wiki initialized" in text

    def test_idempotent_second_call_does_not_overwrite_files(
        self, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))

        # Overwrite seed files with custom content
        (wiki / "SCHEMA.md").write_text("custom schema", encoding="utf-8")
        (wiki / "index.md").write_text("custom index", encoding="utf-8")
        (wiki / "log.md").write_text("custom log", encoding="utf-8")

        # Second call must not overwrite
        wiki_sync.ensure_wiki(str(wiki))

        assert (wiki / "SCHEMA.md").read_text(encoding="utf-8") == "custom schema"
        assert (wiki / "index.md").read_text(encoding="utf-8") == "custom index"
        assert (wiki / "log.md").read_text(encoding="utf-8") == "custom log"

    def test_idempotent_dirs_already_exist(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        # Second call on an already-fully-built wiki must not raise
        wiki_sync.ensure_wiki(str(wiki))

    def test_schema_contains_today_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        text = (wiki / "SCHEMA.md").read_text(encoding="utf-8")
        assert _FIXED_DATE in text

    def test_log_contains_today_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        text = (wiki / "log.md").read_text(encoding="utf-8")
        assert _FIXED_DATE in text

    def test_index_contains_today_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        text = (wiki / "index.md").read_text(encoding="utf-8")
        assert _FIXED_DATE in text

    def test_accepts_path_with_nested_parents(self, tmp_path: Path) -> None:
        """ensure_wiki should create deeply nested paths without error."""
        wiki = tmp_path / "a" / "b" / "c" / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        assert (wiki / "entities" / "skills").is_dir()

    def test_rejects_symlinked_wiki_root(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        link = tmp_path / "wiki-link"
        target.mkdir()
        _symlink_to(target, link, target_is_directory=True)
        with pytest.raises(ValueError, match="symlinked wiki path"):
            wiki_sync.ensure_wiki(str(link))

    def test_rejects_symlinked_seed_file(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        outside = tmp_path / "outside-index.md"
        outside.write_text("outside\n", encoding="utf-8")
        _symlink_to(outside, wiki / "index.md", target_is_directory=False)
        with pytest.raises(ValueError, match="symlinked wiki path"):
            wiki_sync.ensure_wiki(str(wiki))
        assert outside.read_text(encoding="utf-8") == "outside\n"


# ---------------------------------------------------------------------------
# TestSaveScan
# ---------------------------------------------------------------------------


class TestSaveScan:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        return wiki

    def test_saves_json_file_in_raw_scans(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        profile = {"repo_path": "/home/user/my-project", "project_type": "python"}
        result = wiki_sync.save_scan(str(wiki), profile)
        scan_dir = wiki / "raw" / "scans"
        saved = Path(result)
        assert saved.parent == scan_dir
        assert saved.exists()

    def test_filename_encodes_date_and_repo_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        profile = {"repo_path": "/projects/my-cool-repo"}
        result = wiki_sync.save_scan(str(wiki), profile)
        filename = Path(result).name
        assert filename == f"scan-{_FIXED_DATE}-my-cool-repo.json"

    def test_saved_content_round_trips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        profile = {
            "repo_path": "/projects/test-repo",
            "project_type": "typescript",
            "skills": ["react", "jest"],
        }
        result = wiki_sync.save_scan(str(wiki), profile)
        loaded = json.loads(Path(result).read_text(encoding="utf-8"))
        assert loaded == profile

    def test_save_scan_uses_atomic_json_writer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        profile = {"repo_path": "/projects/test-repo", "project_type": "python"}
        calls: list[tuple[Path, dict, int | None]] = []

        def fake_atomic_write_json(path: Path, obj: dict, indent: int | None = 2) -> None:
            calls.append((path, obj, indent))
            path.write_text(json.dumps(obj, indent=indent) + "\n", encoding="utf-8")

        monkeypatch.setattr(
            wiki_sync,
            "atomic_write_json",
            fake_atomic_write_json,
            raising=False,
        )

        result = wiki_sync.save_scan(str(wiki), profile)

        assert calls == [(Path(result), profile, 2)]

    def test_repo_path_basename_only_used_in_filename(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the final path component should appear in the scan filename."""
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        profile = {"repo_path": "/very/deep/nested/path/target-repo"}
        result = wiki_sync.save_scan(str(wiki), profile)
        assert "target-repo" in Path(result).name
        assert "nested" not in Path(result).name

    @pytest.mark.parametrize("repo_path", [
        "/projects/alpha",
        "C:\\Users\\dev\\beta",
        "/tmp/gamma-123",
    ])
    def test_various_repo_paths(
        self, repo_path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        profile = {"repo_path": repo_path}
        result = wiki_sync.save_scan(str(wiki), profile)
        assert Path(result).exists()


# ---------------------------------------------------------------------------
# TestSanitizeYamlValue
# ---------------------------------------------------------------------------


class TestSanitizeYamlValue:
    """_sanitize_yaml_value is private but critical for injection prevention."""

    def _sanitize(self, value: str) -> str:
        return wiki_sync._sanitize_yaml_value(value)

    def test_plain_string_unchanged(self) -> None:
        assert self._sanitize("hello world") == "hello world"

    def test_strips_leading_colon(self) -> None:
        result = self._sanitize(": injected")
        assert not result.startswith(":")

    def test_strips_multiple_leading_colons(self) -> None:
        result = self._sanitize(":::bad")
        assert not result.startswith(":")

    def test_strips_leading_hash(self) -> None:
        result = self._sanitize("# comment injection")
        assert not result.startswith("#")

    def test_strips_multiple_leading_hashes(self) -> None:
        result = self._sanitize("### heading injection")
        assert not result.startswith("#")

    def test_removes_newlines(self) -> None:
        result = self._sanitize("line1\nline2")
        assert "\n" not in result

    def test_removes_carriage_returns(self) -> None:
        result = self._sanitize("line1\r\nline2")
        assert "\r" not in result
        assert "\n" not in result

    def test_strips_surrounding_whitespace(self) -> None:
        assert self._sanitize("  value  ") == "value"

    def test_empty_string_returns_empty(self) -> None:
        assert self._sanitize("") == ""

    def test_colon_mid_string_preserved(self) -> None:
        """Colon in the middle of a value should not be stripped."""
        result = self._sanitize("http://example.com")
        assert "http" in result
        # Leading colon is stripped; mid-string colons are fine
        assert "example.com" in result

    def test_non_string_coerced(self) -> None:
        """Function calls str() on the input — numbers must not raise."""
        result = self._sanitize(42)  # type: ignore[arg-type]
        assert result == "42"

    @pytest.mark.parametrize("value,expected_clean", [
        ("normal", "normal"),
        ("  spaces  ", "spaces"),
        (":colon-start", "colon-start"),
        ("#hash-start", "hash-start"),
        ("multi\nline\nvalue", "multi line value"),
    ])
    def test_parametrized_sanitize(self, value: str, expected_clean: str) -> None:
        assert self._sanitize(value) == expected_clean


# ---------------------------------------------------------------------------
# TestUpsertSkillPage
# ---------------------------------------------------------------------------


class TestUpsertSkillPage:
    def _wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        return wiki

    def _skill_info(self, **overrides: object) -> dict:
        base = {
            "path": "/skills/my-skill",
            "reason": "python testing framework",
            "repo": "test-repo",
            "priority": 10,
        }
        base.update(overrides)
        return base

    # --- create path ---

    def test_returns_true_for_new_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        is_new = wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        assert is_new is True

    def test_creates_md_file_in_entities_skills(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        page = wiki / "entities" / "skills" / "my-skill.md"
        assert page.exists()

    def test_new_page_contains_frontmatter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        content = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "title: my-skill" in content
        assert f"created: {_FIXED_DATE}" in content
        assert "type: skill" in content

    def test_new_page_tags_inferred_from_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        info = self._skill_info(reason="python fastapi microservice")
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", info)
        content = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "python" in content
        assert "fastapi" in content

    def test_new_page_tags_fallback_to_uncategorized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        info = self._skill_info(reason="some obscure unique framework xyz")
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", info)
        content = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "uncategorized" in content

    def test_new_page_contains_priority_score(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info(priority=42))
        content = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "42" in content

    def test_new_page_use_count_starts_at_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        content = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "use_count: 1" in content

    # --- update path ---

    def test_returns_false_for_existing_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        is_new = wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        assert is_new is False

    def test_update_increments_use_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        page = wiki / "entities" / "skills" / "my-skill.md"
        page.write_text(_minimal_skill_page(), encoding="utf-8")

        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        updated = page.read_text(encoding="utf-8")
        assert "use_count: 4" in updated  # was 3, now 4

    def test_update_bumps_updated_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        page = wiki / "entities" / "skills" / "my-skill.md"
        old_date = "2020-01-01"
        page.write_text(_minimal_skill_page(date=old_date), encoding="utf-8")

        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        updated = page.read_text(encoding="utf-8")
        assert f"updated: {_FIXED_DATE}" in updated

    def test_update_bumps_last_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        page = wiki / "entities" / "skills" / "my-skill.md"
        page.write_text(_minimal_skill_page(date="2020-06-01"), encoding="utf-8")

        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())
        updated = page.read_text(encoding="utf-8")
        assert f"last_used: {_FIXED_DATE}" in updated

    def test_update_non_integer_use_count_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Corrupted use_count should be silently skipped, not raised."""
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        page = wiki / "entities" / "skills" / "my-skill.md"
        content = (
            "---\nupdated: 2020-01-01\nuse_count: NaN\nlast_used: 2020-01-01\n---\n# my-skill\n"
        )
        page.write_text(content, encoding="utf-8")
        # Must not raise
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", self._skill_info())

    # --- validation ---

    @pytest.mark.parametrize("bad_name", [
        "",
        " leading-space",
        "has space",
        "!invalid",
        "-starts-with-dash",
    ])
    def test_invalid_skill_name_raises_value_error(
        self, bad_name: str, tmp_path: Path
    ) -> None:
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        with pytest.raises(ValueError, match="Invalid skill name"):
            wiki_sync.upsert_skill_page(str(wiki), bad_name, self._skill_info())

    @pytest.mark.parametrize("good_name", [
        "my-skill",
        "skill123",
        "Skill.Name",
        "a",
        "my_skill_v2",
    ])
    def test_valid_skill_names_accepted(
        self, good_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        is_new = wiki_sync.upsert_skill_page(str(wiki), good_name, self._skill_info())
        assert is_new is True

    def test_skill_name_in_skill_info_infers_tags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tags should also be inferred when the keyword appears in the skill name."""
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        info = self._skill_info(reason="general purpose")
        wiki_sync.upsert_skill_page(str(wiki), "python-helper", info)
        content = (wiki / "entities" / "skills" / "python-helper.md").read_text(encoding="utf-8")
        assert "python" in content

    def test_path_sanitization_applied_in_new_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A path with a leading colon should be sanitized in the frontmatter."""
        _pin_today(monkeypatch)
        wiki = self._wiki(tmp_path)
        info = self._skill_info(path=": injected-path")
        wiki_sync.upsert_skill_page(str(wiki), "my-skill", info)
        content = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        # The value should be sanitized (no leading colon after "path: ")
        fm_match = re.search(r"^path: (.+)$", content, re.MULTILINE)
        assert fm_match is not None
        assert not fm_match.group(1).startswith(":")


# ---------------------------------------------------------------------------
# TestEntityIndexLink
# ---------------------------------------------------------------------------


class TestEntityIndexLink:
    def test_skills_flat_path(self) -> None:
        result = wiki_sync._entity_index_link("skills", "my-skill")
        assert result == "entities/skills/my-skill"

    def test_agents_flat_path(self) -> None:
        result = wiki_sync._entity_index_link("agents", "my-agent")
        assert result == "entities/agents/my-agent"

    def test_plugins_flat_path(self) -> None:
        result = wiki_sync._entity_index_link("plugins", "my-plugin")
        assert result == "entities/plugins/my-plugin"

    def test_mcp_servers_alpha_sharded(self) -> None:
        result = wiki_sync._entity_index_link("mcp-servers", "github-mcp")
        assert result == "entities/mcp-servers/g/github-mcp"

    def test_mcp_servers_numeric_sharded_to_0_9(self) -> None:
        result = wiki_sync._entity_index_link("mcp-servers", "007-mcp")
        assert result == "entities/mcp-servers/0-9/007-mcp"

    def test_mcp_servers_empty_slug_shard(self) -> None:
        """Empty slug should not crash; shard defaults to 0-9."""
        result = wiki_sync._entity_index_link("mcp-servers", "")
        assert result == "entities/mcp-servers/0-9/"

    @pytest.mark.parametrize("subject_type,slug,expected", [
        ("skills", "react-hooks", "entities/skills/react-hooks"),
        ("agents", "tdd-guide", "entities/agents/tdd-guide"),
        ("plugins", "obsidian-plugin", "entities/plugins/obsidian-plugin"),
        ("mcp-servers", "anthropic-mcp", "entities/mcp-servers/a/anthropic-mcp"),
        ("mcp-servers", "1password-mcp", "entities/mcp-servers/0-9/1password-mcp"),
        ("mcp-servers", "zotero-mcp", "entities/mcp-servers/z/zotero-mcp"),
    ])
    def test_parametrized_link_generation(
        self, subject_type: str, slug: str, expected: str
    ) -> None:
        assert wiki_sync._entity_index_link(subject_type, slug) == expected


# ---------------------------------------------------------------------------
# TestUpdateIndex
# ---------------------------------------------------------------------------


class TestUpdateIndex:
    def _wiki_with_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None = None
    ) -> Path:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        if content is not None:
            (wiki / "index.md").write_text(content, encoding="utf-8")
        return wiki

    def test_empty_entries_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        original = (wiki / "index.md").read_text(encoding="utf-8")
        wiki_sync.update_index(str(wiki), [])
        assert (wiki / "index.md").read_text(encoding="utf-8") == original

    def test_single_skill_entry_added_to_skills_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["my-skill"])
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "[[entities/skills/my-skill]]" in content

    def test_multiple_entries_all_added(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["alpha", "beta", "gamma"])
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "[[entities/skills/alpha]]" in content
        assert "[[entities/skills/beta]]" in content
        assert "[[entities/skills/gamma]]" in content

    def test_duplicate_entry_not_inserted_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["my-skill"])
        wiki_sync.update_index(str(wiki), ["my-skill"])
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert content.count("[[entities/skills/my-skill]]") == 1

    def test_total_pages_counter_updated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["alpha", "beta"])
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "Total pages: 2" in content

    def test_last_updated_set_to_today(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["some-skill"])
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert f"Last updated: {_FIXED_DATE}" in content

    def test_atomic_write_failure_preserves_original_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        index = wiki / "index.md"
        original = index.read_text(encoding="utf-8")

        def fail_atomic_write_text(
            path: Path,
            text: str,
            encoding: str = "utf-8",
        ) -> None:
            del path, text, encoding
            raise OSError("atomic index write failed")

        monkeypatch.setattr(
            wiki_sync,
            "atomic_write_text",
            fail_atomic_write_text,
            raising=False,
        )

        with pytest.raises(OSError, match="atomic index write failed"):
            wiki_sync.update_index(str(wiki), ["some-skill"])

        assert index.read_text(encoding="utf-8") == original

    def test_agent_entry_goes_into_agents_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["my-agent"], subject_type="agents")
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "[[entities/agents/my-agent]]" in content

    def test_mcp_server_entry_uses_sharded_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["github-mcp"], subject_type="mcp-servers")
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "[[entities/mcp-servers/g/github-mcp]]" in content

    def test_invalid_subject_type_raises_value_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        with pytest.raises(ValueError, match="unknown subject_type"):
            wiki_sync.update_index(str(wiki), ["x"], subject_type="nonexistent")

    def test_missing_section_header_appended_at_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If index.md has no ## Agents section, it should be created."""
        index_content = (
            "# Skill Wiki Index\n\n"
            "> Last updated: 2024-01-01 | Total pages: 0\n\n"
            "## Skills\n\n"
        )
        wiki = self._wiki_with_index(tmp_path, monkeypatch, content=index_content)
        wiki_sync.update_index(str(wiki), ["my-agent"], subject_type="agents")
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "## Agents" in content
        assert "[[entities/agents/my-agent]]" in content

    def test_entries_sorted_before_insert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slugs should appear in alphabetical order in the index."""
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["zebra", "alpha", "mango"])
        content = (wiki / "index.md").read_text(encoding="utf-8")
        pos_alpha = content.index("alpha")
        pos_mango = content.index("mango")
        pos_zebra = content.index("zebra")
        assert pos_alpha < pos_mango < pos_zebra

    def test_total_counter_includes_all_entity_types(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["skill-a"], subject_type="skills")
        wiki_sync.update_index(str(wiki), ["agent-a"], subject_type="agents")
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "Total pages: 2" in content

    def test_plugin_entry_goes_into_plugins_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki_with_index(tmp_path, monkeypatch)
        wiki_sync.update_index(str(wiki), ["my-plugin"], subject_type="plugins")
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "[[entities/plugins/my-plugin]]" in content


# ---------------------------------------------------------------------------
# TestAppendLog
# ---------------------------------------------------------------------------


class TestAppendLog:
    def _wiki(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        return wiki

    def test_appends_entry_to_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        wiki_sync.append_log(str(wiki), "scan", "my-repo", ["detail one", "detail two"])
        log = (wiki / "log.md").read_text(encoding="utf-8")
        assert "scan" in log
        assert "my-repo" in log
        assert "detail one" in log
        assert "detail two" in log

    def test_log_entry_uses_today_date(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        wiki_sync.append_log(str(wiki), "scan", "repo", [])
        log = (wiki / "log.md").read_text(encoding="utf-8")
        assert f"[{_FIXED_DATE}]" in log

    def test_second_append_preserves_first_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        wiki_sync.append_log(str(wiki), "scan", "repo-a", ["first"])
        wiki_sync.append_log(str(wiki), "scan", "repo-b", ["second"])
        log = (wiki / "log.md").read_text(encoding="utf-8")
        assert "repo-a" in log
        assert "repo-b" in log
        assert "first" in log
        assert "second" in log

    def test_atomic_write_failure_preserves_original_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        log_path = wiki / "log.md"
        original = log_path.read_text(encoding="utf-8")

        def fail_atomic_write_text(
            path: Path,
            text: str,
            encoding: str = "utf-8",
        ) -> None:
            del path, text, encoding
            raise OSError("atomic log write failed")

        monkeypatch.setattr(
            wiki_sync,
            "atomic_write_text",
            fail_atomic_write_text,
            raising=False,
        )

        with pytest.raises(OSError, match="atomic log write failed"):
            wiki_sync.append_log(str(wiki), "scan", "repo", ["detail"])

        assert log_path.read_text(encoding="utf-8") == original

    def test_empty_details_list_produces_no_bullet_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        original_len = len((wiki / "log.md").read_text(encoding="utf-8").splitlines())
        wiki_sync.append_log(str(wiki), "init", "wiki", [])
        new_lines = (wiki / "log.md").read_text(encoding="utf-8").splitlines()
        # Header "## [date] init | wiki" plus blank line = 2 new lines
        appended_lines = new_lines[original_len:]
        bullet_lines = [line for line in appended_lines if line.startswith("- ")]
        assert bullet_lines == []

    def test_each_detail_line_starts_with_bullet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        wiki_sync.append_log(str(wiki), "scan", "repo", ["alpha", "beta"])
        log = (wiki / "log.md").read_text(encoding="utf-8")
        assert "- alpha" in log
        assert "- beta" in log

    def test_log_section_header_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        wiki_sync.append_log(str(wiki), "update", "some-skill", [])
        log = (wiki / "log.md").read_text(encoding="utf-8")
        assert f"## [{_FIXED_DATE}] update | some-skill" in log


# ---------------------------------------------------------------------------
# TestUpsertUsage
# ---------------------------------------------------------------------------


class TestUpsertUsage:
    def _wiki(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        return wiki

    def _write_page(self, wiki: Path, name: str, content: str) -> Path:
        page = wiki / "entities" / "skills" / f"{name}.md"
        page.write_text(content, encoding="utf-8")
        return page

    def test_nonexistent_page_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        # Must not raise
        wiki_sync.upsert_usage(str(wiki), "ghost-skill", _FIXED_DATE, used=True)

    def test_increments_use_count_when_used_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        self._write_page(wiki, "my-skill", _minimal_skill_page())
        wiki_sync.upsert_usage(str(wiki), "my-skill", _FIXED_DATE, used=True)
        updated = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "use_count: 4" in updated  # 3 -> 4

    def test_does_not_increment_use_count_when_used_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        self._write_page(wiki, "my-skill", _minimal_skill_page())
        wiki_sync.upsert_usage(str(wiki), "my-skill", _FIXED_DATE, used=False)
        updated = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        # use_count should remain 3
        assert "use_count: 3" in updated

    def test_increments_existing_session_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        content = _minimal_skill_page() + "session_count: 5\n"
        # Ensure use_count line exists so session_count replace works
        content = content.replace("use_count: 3\n", "use_count: 3\nsession_count: 5\n", 1)
        # Cleaner: write a page with session_count already present
        page_content = (
            f"---\n"
            f"title: my-skill\n"
            f"updated: {_FIXED_DATE}\n"
            f"use_count: 3\n"
            f"session_count: 5\n"
            f"last_used: {_FIXED_DATE}\n"
            f"status: installed\n"
            f"---\n\n"
            f"# my-skill\n"
        )
        self._write_page(wiki, "my-skill", page_content)
        wiki_sync.upsert_usage(str(wiki), "my-skill", _FIXED_DATE, used=False)
        updated = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "session_count: 6" in updated

    def test_adds_session_count_when_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the page has no session_count field, it should be inserted."""
        wiki = self._wiki(tmp_path, monkeypatch)
        self._write_page(wiki, "my-skill", _minimal_skill_page())
        wiki_sync.upsert_usage(str(wiki), "my-skill", _FIXED_DATE, used=False)
        updated = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "session_count: 1" in updated

    def test_updates_last_used_when_used_true(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        self._write_page(wiki, "my-skill", _minimal_skill_page(date="2020-01-01"))
        wiki_sync.upsert_usage(str(wiki), "my-skill", "2025-07-04", used=True)
        updated = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        assert "last_used: 2025-07-04" in updated

    def test_atomic_write_failure_preserves_original_usage_page(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        page = self._write_page(wiki, "my-skill", _minimal_skill_page())
        original = page.read_text(encoding="utf-8")

        def fail_atomic_write_text(
            path: Path,
            text: str,
            encoding: str = "utf-8",
        ) -> None:
            del path, text, encoding
            raise OSError("atomic usage write failed")

        monkeypatch.setattr(
            wiki_sync,
            "atomic_write_text",
            fail_atomic_write_text,
            raising=False,
        )

        with pytest.raises(OSError, match="atomic usage write failed"):
            wiki_sync.upsert_usage(str(wiki), "my-skill", _FIXED_DATE, used=True)

        assert page.read_text(encoding="utf-8") == original

    def test_does_not_update_last_used_when_used_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        self._write_page(wiki, "my-skill", _minimal_skill_page(date="2020-01-01"))
        wiki_sync.upsert_usage(str(wiki), "my-skill", "2025-07-04", used=False)
        updated = (wiki / "entities" / "skills" / "my-skill.md").read_text(encoding="utf-8")
        # last_used should remain unchanged
        assert "last_used: 2020-01-01" in updated

    def test_corrupted_use_count_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        content = (
            "---\nupdated: 2024-01-01\nuse_count: bad\nlast_used: 2024-01-01\n"
            "session_count: 2\n---\n# my-skill\n"
        )
        self._write_page(wiki, "my-skill", content)
        # Must not raise
        wiki_sync.upsert_usage(str(wiki), "my-skill", _FIXED_DATE, used=True)

    def test_corrupted_session_count_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        content = (
            "---\nupdated: 2024-01-01\nuse_count: 3\nlast_used: 2024-01-01\n"
            "session_count: notanumber\n---\n# my-skill\n"
        )
        self._write_page(wiki, "my-skill", content)
        wiki_sync.upsert_usage(str(wiki), "my-skill", _FIXED_DATE, used=False)


# ---------------------------------------------------------------------------
# TestMarkStale
# ---------------------------------------------------------------------------


class TestMarkStale:
    def _wiki(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        return wiki

    def test_nonexistent_page_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        # Must not raise
        wiki_sync.mark_stale(str(wiki), "ghost-skill")

    def test_sets_status_to_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        page = wiki / "entities" / "skills" / "my-skill.md"
        page.write_text(_minimal_skill_page(), encoding="utf-8")

        wiki_sync.mark_stale(str(wiki), "my-skill")
        updated = page.read_text(encoding="utf-8")
        assert "status: stale" in updated

    def test_replaces_existing_status_not_duplicate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        page = wiki / "entities" / "skills" / "my-skill.md"
        page.write_text(_minimal_skill_page(), encoding="utf-8")

        wiki_sync.mark_stale(str(wiki), "my-skill")
        updated = page.read_text(encoding="utf-8")
        # "status: installed" must be gone, "status: stale" appears exactly once
        assert "status: installed" not in updated
        assert updated.count("status: stale") == 1

    def test_page_without_status_field_not_corrupted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If no status field exists, mark_stale should not crash or add garbage."""
        wiki = self._wiki(tmp_path, monkeypatch)
        page = wiki / "entities" / "skills" / "my-skill.md"
        no_status_content = "---\ntitle: my-skill\nupdated: 2024-01-01\n---\n# my-skill\n"
        page.write_text(no_status_content, encoding="utf-8")
        # _find_field returns None when status is missing, so content.replace is a noop
        wiki_sync.mark_stale(str(wiki), "my-skill")
        # Content should be written back but unchanged (replace did nothing)
        result = page.read_text(encoding="utf-8")
        assert result == no_status_content

    @pytest.mark.parametrize("initial_status", ["installed", "active", "deprecated"])
    def test_any_initial_status_replaced_with_stale(
        self, initial_status: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki = self._wiki(tmp_path, monkeypatch)
        page = wiki / "entities" / "skills" / "my-skill.md"
        content = (
            f"---\ntitle: my-skill\nupdated: {_FIXED_DATE}\n"
            f"status: {initial_status}\n---\n# my-skill\n"
        )
        page.write_text(content, encoding="utf-8")
        wiki_sync.mark_stale(str(wiki), "my-skill")
        updated = page.read_text(encoding="utf-8")
        assert "status: stale" in updated
        assert f"status: {initial_status}" not in updated


# ---------------------------------------------------------------------------
# TestUpdateIndexEdgeCases  (branch coverage for missing-section path)
# ---------------------------------------------------------------------------


class TestUpdateIndexEdgeCases:
    """Cover the branch where the last line of index.md is non-empty when a
    missing section is appended (line 320 in wiki_sync.py)."""

    def test_section_appended_when_file_has_no_trailing_newline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_today(monkeypatch)
        wiki = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki))
        # Write index without a trailing newline so lines[-1] != ""
        index_content = "# Skill Wiki Index\n\n> Last updated: 2024-01-01 | Total pages: 0\n\n## Skills"
        (wiki / "index.md").write_text(index_content, encoding="utf-8")

        wiki_sync.update_index(str(wiki), ["my-agent"], subject_type="agents")
        content = (wiki / "index.md").read_text(encoding="utf-8")
        assert "## Agents" in content
        assert "[[entities/agents/my-agent]]" in content


# ---------------------------------------------------------------------------
# TestMain  (CLI entrypoint smoke coverage)
# ---------------------------------------------------------------------------


class TestMain:
    """Smoke tests for the main() CLI entrypoint using monkeypatched internals."""

    def test_init_flag_calls_ensure_wiki(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _pin_today(monkeypatch)
        wiki_dir = str(tmp_path / "wiki")
        monkeypatch.setattr("sys.argv", ["wiki_sync", "--init", "--wiki", wiki_dir])
        wiki_sync.main()
        out = capsys.readouterr().out
        assert "initialized" in out.lower() or "wiki" in out.lower()
        assert (tmp_path / "wiki" / "SCHEMA.md").exists()

    def test_missing_profile_and_manifest_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wiki_dir = str(tmp_path / "wiki")
        monkeypatch.setattr("sys.argv", ["wiki_sync", "--wiki", wiki_dir])
        with pytest.raises(SystemExit) as exc_info:
            wiki_sync.main()
        assert exc_info.value.code == 1

    def test_full_sync_creates_skill_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _pin_today(monkeypatch)
        wiki_dir = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki_dir))

        profile = {
            "repo_path": str(tmp_path / "my-repo"),
            "project_type": "python",
        }
        manifest = {
            "load": [
                {"skill": "python-testing", "path": "/skills/python-testing", "reason": "python testing", "priority": 5},
            ],
            "unload": [],
            "warnings": [],
        }

        profile_path = tmp_path / "profile.json"
        manifest_path = tmp_path / "manifest.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            [
                "wiki_sync",
                "--profile", str(profile_path),
                "--manifest", str(manifest_path),
                "--wiki", str(wiki_dir),
            ],
        )
        wiki_sync.main()

        out = capsys.readouterr().out
        assert "synced" in out.lower() or "wiki" in out.lower()
        skill_page = wiki_dir / "entities" / "skills" / "python-testing.md"
        assert skill_page.exists()

    def test_full_sync_routes_mixed_manifest_entries_by_entity_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _pin_today(monkeypatch)
        wiki_dir = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki_dir))

        profile = {
            "repo_path": str(tmp_path / "mixed-repo"),
            "project_type": "python",
        }
        manifest = {
            "load": [
                {
                    "skill": "python-testing",
                    "entity_type": "skill",
                    "path": "/skills/python-testing",
                    "reason": "python testing",
                    "priority": 5,
                },
                {
                    "skill": "code-reviewer",
                    "entity_type": "agent",
                    "path": "/agents/code-reviewer.md",
                    "reason": "review agent",
                    "priority": 4,
                },
                {
                    "skill": "github-mcp",
                    "entity_type": "mcp-server",
                    "command": "npx -y @modelcontextprotocol/server-github",
                    "reason": "github mcp integration",
                    "priority": 3,
                },
            ],
            "unload": [],
            "warnings": [],
        }

        profile_path = tmp_path / "profile-mixed.json"
        manifest_path = tmp_path / "manifest-mixed.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            [
                "wiki_sync",
                "--profile", str(profile_path),
                "--manifest", str(manifest_path),
                "--wiki", str(wiki_dir),
            ],
        )
        wiki_sync.main()
        capsys.readouterr()

        skill_page = wiki_dir / "entities" / "skills" / "python-testing.md"
        agent_page = wiki_dir / "entities" / "agents" / "code-reviewer.md"
        mcp_page = wiki_dir / "entities" / "mcp-servers" / "g" / "github-mcp.md"
        assert skill_page.exists()
        assert agent_page.exists()
        assert mcp_page.exists()
        assert not (wiki_dir / "entities" / "skills" / "code-reviewer.md").exists()
        assert not (wiki_dir / "entities" / "skills" / "github-mcp.md").exists()
        assert "type: skill" in skill_page.read_text(encoding="utf-8")
        assert "type: agent" in agent_page.read_text(encoding="utf-8")
        assert "type: mcp-server" in mcp_page.read_text(encoding="utf-8")

        index = (wiki_dir / "index.md").read_text(encoding="utf-8")
        assert "[[entities/skills/python-testing]]" in index
        assert "[[entities/agents/code-reviewer]]" in index
        assert "[[entities/mcp-servers/g/github-mcp]]" in index

    def test_full_sync_appends_to_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        _pin_today(monkeypatch)
        wiki_dir = tmp_path / "wiki"
        wiki_sync.ensure_wiki(str(wiki_dir))

        profile = {"repo_path": str(tmp_path / "proj"), "project_type": "go"}
        manifest = {
            "load": [
                {"skill": "golang-testing", "path": "/s/golang-testing", "reason": "go testing", "priority": 3},
            ],
            "unload": [],
            "warnings": ["one warning"],
        }

        profile_path = tmp_path / "profile2.json"
        manifest_path = tmp_path / "manifest2.json"
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        monkeypatch.setattr(
            "sys.argv",
            [
                "wiki_sync",
                "--profile", str(profile_path),
                "--manifest", str(manifest_path),
                "--wiki", str(wiki_dir),
            ],
        )
        wiki_sync.main()
        capsys.readouterr()

        log = (wiki_dir / "log.md").read_text(encoding="utf-8")
        assert "scan" in log
        assert "proj" in log
