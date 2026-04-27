"""
tests/test_skill_add.py -- pytest suite for skill_add module.

External deps (batch_convert, intake_pipeline, wiki_sync) are mocked so
tests run without heavy imports. Internal helpers are tested directly.

Covers:
  - infer_tags                  (known tag in name/content, fallback uncategorized)
  - install_skill               (copies SKILL.md, returns path)
  - maybe_convert               (below threshold, above threshold success, failure)
  - build_entity_page           (frontmatter fields, has_pipeline variants)
  - write_entity_page           (creates new, overwrites existing)
  - find_related_skills         (shared tags, no tags, self excluded)
  - _add_backlink               (adds link, skips if present, no page)
  - wire_backlinks              (calls _add_backlink for each related)
  - detect_scan_sources         (matching file, no match, missing dir)
  - main()                      (no args exits 1, --skill-path without --name exits 1,
                                  scan-dir not found exits 1, both flags exits 1)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

# ---------------------------------------------------------------------------
# Stub out all heavy external deps BEFORE importing skill_add.
# We insert permanent mocks into sys.modules so they survive the import.
# ---------------------------------------------------------------------------

_FAKE_CFG = MagicMock()
_FAKE_CFG.all_tags = [
    "python", "javascript", "typescript", "react", "docker",
    "fastapi", "django", "langchain", "testing", "api", "llm",
    "uncategorized",
]
_FAKE_CFG.line_threshold = 180
_FAKE_CFG.wiki_dir = Path("/tmp/wiki")
_FAKE_CFG.skills_dir = Path("/tmp/skills")

_fake_ctx_config = MagicMock()
_fake_ctx_config.cfg = _FAKE_CFG

_fake_batch_convert = MagicMock()
_fake_intake = MagicMock()
_fake_wiki_sync = MagicMock()

# Inject stubs before skill_add is imported. Plan 001 R6b moved
# wiki_sync under ctx.core.wiki — cover both the canonical and the
# legacy names so whichever one skill_add resolves lands on the mock.
# This block runs at COLLECTION time (module load), which means the
# injection would persist for the whole pytest session if we did not
# clean up. The cleanup at the bottom of this block removes the
# MagicMock entries from sys.modules immediately after skill_add's
# import has captured them into its own namespace — from that point on
# skill_add's bindings (ensure_wiki, append_log, update_index, etc.)
# stay pinned to the mock while the rest of the test session sees the
# real modules. Prior approach that kept the entries live leaked into
# test_link_conversions and test_wiki_*.
_STUBS: list[tuple[str, MagicMock]] = [
    ("ctx_config", _fake_ctx_config),
    ("batch_convert", _fake_batch_convert),
    ("intake_pipeline", _fake_intake),
    ("wiki_sync", _fake_wiki_sync),
    ("ctx.core.wiki.wiki_sync", _fake_wiki_sync),
]
_SAVED_MODULES: dict[str, object] = {}
for _mod_name, _mod in _STUBS:
    _SAVED_MODULES[_mod_name] = sys.modules.get(_mod_name)
    sys.modules[_mod_name] = _mod

import skill_add as _sa  # noqa: E402 -- must come after stubs

# Re-import helpers after module is loaded
from skill_add import (  # noqa: E402
    _add_backlink,
    add_skill,
    build_entity_page,
    detect_scan_sources,
    find_related_skills,
    infer_tags,
    install_skill,
    maybe_convert,
    wire_backlinks,
    write_entity_page,
)

# Clean up the sys.modules stubs NOW that skill_add has been imported
# and its names are bound. skill_add's own reference to (e.g.)
# ensure_wiki is already captured into its namespace, so removing
# the MagicMock from sys.modules does NOT unbind skill_add's name —
# skill_add.ensure_wiki keeps pointing at the mock for the rest of
# the session. Every OTHER test module that imports wiki_sync /
# ctx.core.wiki.wiki_sync afresh gets the real module back. This
# closes the cross-file pollution that broke test_link_conversions
# and test_wiki_* whenever test_skill_add collected earlier.
for _name in ("wiki_sync", "ctx.core.wiki.wiki_sync",
              "batch_convert", "intake_pipeline", "ctx_config"):
    if sys.modules.get(_name) in (_fake_wiki_sync, _fake_batch_convert,
                                   _fake_intake, _fake_ctx_config):
        # Restore whatever was there before (None means "not loaded").
        original = _SAVED_MODULES.get(_name)
        if original is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_entity_page(wiki: Path, name: str, tags: list[str]) -> None:
    entities = wiki / "entities" / "skills"
    entities.mkdir(parents=True, exist_ok=True)
    tag_str = ", ".join(tags)
    content = f"---\ntitle: {name}\ntags: [{tag_str}]\n---\n# {name}\n\n## Related Skills\n\n"
    (entities / f"{name}.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# infer_tags
# ---------------------------------------------------------------------------

class TestInferTags:
    def test_tag_in_name(self):
        tags = infer_tags("python-testing", "")
        assert "python" in tags
        assert "testing" in tags

    def test_tag_in_content(self):
        tags = infer_tags("myskill", "Use react hooks for state management")
        assert "react" in tags

    def test_fallback_uncategorized(self):
        tags = infer_tags("completely-unique-name-xyz", "no matching keywords here at all")
        assert tags == ["uncategorized"]

    def test_no_duplicates(self):
        tags = infer_tags("python", "python is great")
        assert len([t for t in tags if t == "python"]) <= 1

    def test_returns_list(self):
        result = infer_tags("docker-skill", "")
        assert isinstance(result, list)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# install_skill
# ---------------------------------------------------------------------------

class TestInstallSkill:
    def test_copies_skill_md(self, tmp_path):
        source = tmp_path / "src" / "SKILL.md"
        source.parent.mkdir()
        source.write_text("# test skill\n")
        skills_dir = tmp_path / "skills"
        installed = install_skill(source, skills_dir, "my-skill")
        assert installed == skills_dir / "my-skill" / "SKILL.md"
        assert installed.exists()
        assert installed.read_text() == "# test skill\n"

    def test_creates_skill_subdirectory(self, tmp_path):
        source = tmp_path / "SKILL.md"
        source.write_text("# skill\n")
        skills_dir = tmp_path / "skills"
        install_skill(source, skills_dir, "new-skill")
        assert (skills_dir / "new-skill").is_dir()

    def test_overwrites_existing(self, tmp_path):
        source = tmp_path / "SKILL.md"
        source.write_text("# new content\n")
        skills_dir = tmp_path / "skills"
        existing_dir = skills_dir / "existing"
        existing_dir.mkdir(parents=True)
        (existing_dir / "SKILL.md").write_text("# old\n")
        installed = install_skill(source, skills_dir, "existing")
        assert installed.read_text() == "# new content\n"


# ---------------------------------------------------------------------------
# maybe_convert
# ---------------------------------------------------------------------------

class TestMaybeConvert:
    def test_below_threshold_returns_false_none(self, tmp_path):
        installed = tmp_path / "SKILL.md"
        installed.write_text("# short\n")
        converted_root = tmp_path / "converted"
        was_converted, path = maybe_convert(installed, "my-skill", converted_root, 50)
        assert was_converted is False
        assert path is None

    def test_above_threshold_success(self, tmp_path):
        installed = tmp_path / "SKILL.md"
        # Need > 180 lines to trigger conversion. Create content with many lines.
        lines = ["# long skill\n"] + [f"line {i}\n" for i in range(200)]
        installed.write_text("".join(lines))
        converted_root = tmp_path / "converted"
        _fake_batch_convert.convert_skill.return_value = {"status": "converted"}
        was_converted, path = maybe_convert(installed, "my-skill", converted_root, 201)
        assert was_converted is True
        assert path == converted_root / "my-skill"

    def test_above_threshold_failure(self, tmp_path):
        installed = tmp_path / "SKILL.md"
        installed.write_text("# long skill\n")
        converted_root = tmp_path / "converted"
        _fake_batch_convert.convert_skill.return_value = {"status": "error"}
        was_converted, path = maybe_convert(installed, "my-skill", converted_root, 200)
        assert was_converted is False
        assert path is None

    def test_exactly_at_threshold_not_converted(self, tmp_path):
        installed = tmp_path / "SKILL.md"
        installed.write_text("# skill\n")
        converted_root = tmp_path / "converted"
        # line_count == threshold → not converted (condition is > threshold)
        was_converted, _ = maybe_convert(installed, "my-skill", converted_root, _FAKE_CFG.line_threshold)
        assert was_converted is False


# ---------------------------------------------------------------------------
# build_entity_page
# ---------------------------------------------------------------------------

class TestBuildEntityPage:
    def _call(self, **kwargs) -> str:
        defaults = dict(
            name="my-skill",
            tags=["python", "testing"],
            line_count=50,
            has_pipeline=False,
            original_path=Path("/tmp/skills/my-skill/SKILL.md"),
            pipeline_path=None,
            related=[],
            scan_sources=[],
        )
        defaults.update(kwargs)
        return build_entity_page(**defaults)

    def test_contains_name_in_frontmatter(self):
        content = self._call(name="react-hooks")
        assert "title: react-hooks" in content

    def test_has_pipeline_true(self):
        content = self._call(has_pipeline=True, pipeline_path=Path("/tmp/wiki/converted/my-skill"))
        assert "has_pipeline: true" in content

    def test_has_pipeline_false(self):
        content = self._call(has_pipeline=False)
        assert "has_pipeline: false" in content

    def test_tags_in_frontmatter(self):
        content = self._call(tags=["python", "fastapi"])
        assert "python" in content
        assert "fastapi" in content

    def test_no_pipeline_note_in_body(self):
        content = self._call(has_pipeline=False, line_count=50)
        assert "under the" in content
        assert "threshold" in content

    def test_scan_sources_in_frontmatter(self):
        content = self._call(scan_sources=["scan-2024.json"])
        assert "scan-2024.json" in content

    def test_related_links_in_body(self):
        content = self._call(related=["docker", "kubernetes"])
        assert "[[entities/skills/docker]]" in content

    def test_no_related_placeholder(self):
        content = self._call(related=[])
        assert "No related skills" in content

    def test_valid_frontmatter_structure(self):
        content = self._call()
        assert content.startswith("---\n")
        assert "\n---\n" in content


# ---------------------------------------------------------------------------
# write_entity_page
# ---------------------------------------------------------------------------

class TestWriteEntityPage:
    def test_writes_new_page_returns_true(self, tmp_path):
        wiki = tmp_path / "wiki"
        (wiki / "entities" / "skills").mkdir(parents=True)
        is_new = write_entity_page(wiki, "react", "# content\n")
        assert is_new is True
        assert (wiki / "entities" / "skills" / "react.md").read_text() == "# content\n"

    def test_overwrites_existing_returns_false(self, tmp_path):
        wiki = tmp_path / "wiki"
        entities = wiki / "entities" / "skills"
        entities.mkdir(parents=True)
        (entities / "react.md").write_text("# old\n")
        is_new = write_entity_page(wiki, "react", "# new\n")
        assert is_new is False
        assert (entities / "react.md").read_text() == "# new\n"


# ---------------------------------------------------------------------------
# find_related_skills
# ---------------------------------------------------------------------------

class TestFindRelatedSkills:
    def test_finds_skill_with_shared_tag(self, tmp_path):
        wiki = tmp_path / "wiki"
        _make_entity_page(wiki, "existing-skill", ["python", "testing"])
        related = find_related_skills(wiki, "new-skill", ["python"])
        assert "existing-skill" in related

    def test_excludes_self(self, tmp_path):
        wiki = tmp_path / "wiki"
        _make_entity_page(wiki, "my-skill", ["python"])
        related = find_related_skills(wiki, "my-skill", ["python"])
        assert "my-skill" not in related

    def test_no_shared_tags_empty(self, tmp_path):
        wiki = tmp_path / "wiki"
        _make_entity_page(wiki, "other-skill", ["docker"])
        related = find_related_skills(wiki, "new-skill", ["python"])
        assert "other-skill" not in related

    def test_uncategorized_tag_ignored(self, tmp_path):
        wiki = tmp_path / "wiki"
        _make_entity_page(wiki, "other-skill", ["uncategorized"])
        # uncategorized is excluded from tag_set in find_related_skills
        related = find_related_skills(wiki, "new-skill", ["uncategorized"])
        assert "other-skill" not in related

    def test_empty_entities_dir_returns_empty(self, tmp_path):
        wiki = tmp_path / "wiki"
        (wiki / "entities" / "skills").mkdir(parents=True)
        related = find_related_skills(wiki, "new-skill", ["python"])
        assert related == []


# ---------------------------------------------------------------------------
# _add_backlink
# ---------------------------------------------------------------------------

class TestAddBacklink:
    def test_adds_link_under_related_section(self, tmp_path):
        wiki = tmp_path / "wiki"
        _make_entity_page(wiki, "target", ["python"])
        _add_backlink(wiki, "target", "source-skill")
        content = (wiki / "entities" / "skills" / "target.md").read_text()
        assert "[[entities/skills/source-skill]]" in content

    def test_skips_if_link_already_present(self, tmp_path):
        wiki = tmp_path / "wiki"
        _make_entity_page(wiki, "target", ["python"])
        _add_backlink(wiki, "target", "source-skill")
        _add_backlink(wiki, "target", "source-skill")  # second call
        content = (wiki / "entities" / "skills" / "target.md").read_text()
        assert content.count("[[entities/skills/source-skill]]") == 1

    def test_no_page_no_crash(self, tmp_path):
        wiki = tmp_path / "wiki"
        (wiki / "entities" / "skills").mkdir(parents=True)
        _add_backlink(wiki, "nonexistent", "source-skill")  # should not raise


# ---------------------------------------------------------------------------
# wire_backlinks
# ---------------------------------------------------------------------------

class TestWireBacklinks:
    def test_adds_backlinks_for_all_related(self, tmp_path):
        wiki = tmp_path / "wiki"
        _make_entity_page(wiki, "skill-a", ["python"])
        _make_entity_page(wiki, "skill-b", ["docker"])
        wire_backlinks(wiki, "new-skill", ["skill-a", "skill-b"])
        for target in ("skill-a", "skill-b"):
            content = (wiki / "entities" / "skills" / f"{target}.md").read_text()
            assert "[[entities/skills/new-skill]]" in content

    def test_empty_related_no_crash(self, tmp_path):
        wiki = tmp_path / "wiki"
        (wiki / "entities" / "skills").mkdir(parents=True)
        wire_backlinks(wiki, "new-skill", [])


# ---------------------------------------------------------------------------
# detect_scan_sources
# ---------------------------------------------------------------------------

class TestDetectScanSources:
    def test_finds_scan_file_referencing_skill(self, tmp_path):
        wiki = tmp_path / "wiki"
        scans = wiki / "raw" / "scans"
        scans.mkdir(parents=True)
        (scans / "scan-2024.json").write_text('{"skills": ["react", "docker"]}')
        sources = detect_scan_sources(wiki, "react")
        assert "scan-2024.json" in sources

    def test_no_match_returns_empty(self, tmp_path):
        wiki = tmp_path / "wiki"
        scans = wiki / "raw" / "scans"
        scans.mkdir(parents=True)
        (scans / "scan-2024.json").write_text('{"skills": ["docker"]}')
        sources = detect_scan_sources(wiki, "totally-unknown-skill")
        assert sources == []

    def test_missing_scans_dir_returns_empty(self, tmp_path):
        wiki = tmp_path / "wiki"
        wiki.mkdir(parents=True)
        sources = detect_scan_sources(wiki, "react")
        assert sources == []


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_no_args_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "skill_add.py",
            "--wiki", str(tmp_path / "wiki"),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        _fake_wiki_sync.ensure_wiki.return_value = None
        with pytest.raises(SystemExit) as exc:
            _sa.main()
        assert exc.value.code == 1

    def test_both_flags_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "skill_add.py",
            "--skill-path", str(tmp_path / "SKILL.md"),
            "--scan-dir", str(tmp_path),
            "--wiki", str(tmp_path / "wiki"),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        _fake_wiki_sync.ensure_wiki.return_value = None
        with pytest.raises(SystemExit) as exc:
            _sa.main()
        assert exc.value.code == 1

    def test_skill_path_without_name_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "skill_add.py",
            "--skill-path", str(tmp_path / "SKILL.md"),
            "--wiki", str(tmp_path / "wiki"),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        _fake_wiki_sync.ensure_wiki.return_value = None
        with pytest.raises(SystemExit) as exc:
            _sa.main()
        assert exc.value.code == 1

    def test_skill_path_not_found_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "skill_add.py",
            "--skill-path", str(tmp_path / "nonexistent.md"),
            "--name", "my-skill",
            "--wiki", str(tmp_path / "wiki"),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        _fake_wiki_sync.ensure_wiki.return_value = None
        with pytest.raises(SystemExit) as exc:
            _sa.main()
        assert exc.value.code == 1

    def test_scan_dir_not_found_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "skill_add.py",
            "--scan-dir", str(tmp_path / "no-such-dir"),
            "--wiki", str(tmp_path / "wiki"),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        _fake_wiki_sync.ensure_wiki.return_value = None
        with pytest.raises(SystemExit) as exc:
            _sa.main()
        assert exc.value.code == 1

    def test_scan_dir_with_no_skills_exits_0(self, tmp_path, monkeypatch, capsys):
        scan_dir = tmp_path / "scan"
        scan_dir.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "skill_add.py",
            "--scan-dir", str(scan_dir),
            "--wiki", str(tmp_path / "wiki"),
            "--skills-dir", str(tmp_path / "skills"),
        ])
        _fake_wiki_sync.ensure_wiki.return_value = None
        with pytest.raises(SystemExit) as exc:
            _sa.main()
        assert exc.value.code == 0


# ---------------------------------------------------------------------------
# add_skill()
# ---------------------------------------------------------------------------

class TestAddSkill:
    """Tests for the core add_skill orchestration function.

    Because wiki_sync was imported by conftest before our stub, the real
    update_index/append_log are bound in skill_add's namespace. We patch
    them directly on the _sa module.
    """

    def _setup_wiki(self, tmp_path: Path) -> Path:
        wiki = tmp_path / "wiki"
        (wiki / "entities" / "skills").mkdir(parents=True)
        (wiki / "converted").mkdir(parents=True)
        return wiki

    def _setup_intake_allow(self):
        """Configure the intake mock to allow all skills."""
        decision = MagicMock()
        decision.allow = True
        _fake_intake.check_intake.return_value = decision
        _fake_intake.record_embedding.return_value = None
        _fake_intake.record_embedding.side_effect = None

    def test_happy_path_returns_dict(self, tmp_path, monkeypatch):
        wiki = self._setup_wiki(tmp_path)
        skills_dir = tmp_path / "skills"
        self._setup_intake_allow()
        _fake_batch_convert.convert_skill.return_value = {"status": "error"}
        monkeypatch.setattr(_sa, "update_index", MagicMock())
        monkeypatch.setattr(_sa, "append_log", MagicMock())

        source = tmp_path / "SKILL.md"
        source.write_text(
            "---\n"
            "name: myskill\n"
            "description: A test skill for validation purposes that is long enough to pass the intake gate requirements.\n"
            "---\n"
            "# myskill\n"
            "\n"
            "This is a test skill with sufficient body content to pass the intake validation. "
            "It needs at least 120 characters of text outside the frontmatter block.\n"
            "\n"
            "## Usage\n"
            "\n"
            "Use this skill for testing.\n"
        )

        result = add_skill(
            source_path=source,
            name="myskill",
            wiki_path=wiki,
            skills_dir=skills_dir,
        )
        assert result["name"] == "myskill"
        assert "installed" in result
        assert result["converted"] is False
        assert result["is_new_page"] is True

    def test_oversized_file_raises_value_error(self, tmp_path):
        wiki = self._setup_wiki(tmp_path)
        skills_dir = tmp_path / "skills"
        large_file = tmp_path / "big.md"
        large_file.write_bytes(b"x" * (1_048_576 + 1))
        with pytest.raises(ValueError, match="too large"):
            add_skill(
                source_path=large_file,
                name="big-skill",
                wiki_path=wiki,
                skills_dir=skills_dir,
            )

    def test_invalid_name_raises_value_error(self, tmp_path):
        wiki = self._setup_wiki(tmp_path)
        skills_dir = tmp_path / "skills"
        source = tmp_path / "SKILL.md"
        source.write_text("# skill\n")
        with pytest.raises(ValueError, match="Invalid skill name"):
            add_skill(
                source_path=source,
                name="../bad-name",
                wiki_path=wiki,
                skills_dir=skills_dir,
            )

    def test_intake_rejected_raises(self, tmp_path):
        wiki = self._setup_wiki(tmp_path)
        skills_dir = tmp_path / "skills"
        decision = MagicMock()
        decision.allow = False
        _fake_intake.check_intake.return_value = decision
        # IntakeRejected is bound in skill_add at import time from the mock
        # which was MagicMock() — we need it to be a real exception class
        _fake_intake.IntakeRejected = RuntimeError

        source = tmp_path / "SKILL.md"
        source.write_text("# skill\n")
        with pytest.raises(Exception):
            add_skill(
                source_path=source,
                name="rejected-skill",
                wiki_path=wiki,
                skills_dir=skills_dir,
            )

    def test_embedding_failure_is_non_fatal(self, tmp_path, monkeypatch):
        """record_embedding failure should not prevent successful add."""
        wiki = self._setup_wiki(tmp_path)
        skills_dir = tmp_path / "skills"
        decision = MagicMock()
        decision.allow = True
        _fake_intake.check_intake.return_value = decision
        _fake_intake.record_embedding.side_effect = RuntimeError("embedding broken")
        _fake_batch_convert.convert_skill.return_value = {"status": "error"}
        monkeypatch.setattr(_sa, "update_index", MagicMock())
        monkeypatch.setattr(_sa, "append_log", MagicMock())

        source = tmp_path / "SKILL.md"
        source.write_text(
            "---\n"
            "name: myskill\n"
            "description: A test skill for validation purposes that is long enough to pass the intake gate requirements.\n"
            "---\n"
            "# myskill\n"
            "\n"
            "This is a test skill with sufficient body content to pass the intake validation. "
            "It needs at least 120 characters of text outside the frontmatter block.\n"
            "\n"
            "## Usage\n"
            "\n"
            "Use this skill for testing.\n"
        )
        result = add_skill(
            source_path=source,
            name="myskill",
            wiki_path=wiki,
            skills_dir=skills_dir,
        )
        assert result["name"] == "myskill"
        # Reset side_effect for subsequent tests
        _fake_intake.record_embedding.side_effect = None

    def test_converted_skill_shows_in_result(self, tmp_path, monkeypatch):
        """When convert_skill succeeds, result['converted'] is True."""
        wiki = self._setup_wiki(tmp_path)
        skills_dir = tmp_path / "skills"
        decision = MagicMock()
        decision.allow = True
        _fake_intake.check_intake.return_value = decision
        _fake_intake.record_embedding.return_value = None
        _fake_intake.record_embedding.side_effect = None
        _fake_batch_convert.convert_skill.return_value = {"status": "converted"}
        monkeypatch.setattr(_sa, "update_index", MagicMock())
        monkeypatch.setattr(_sa, "append_log", MagicMock())

        lines = [
            "---\n",
            "name: myskill\n",
            "description: A test skill for validation purposes that is long enough to pass the intake gate requirements.\n",
            "---\n",
            "# myskill\n",
            "\n",
            "This is a test skill with sufficient body content to pass the intake validation. "
            "It needs at least 120 characters of text outside the frontmatter block.\n",
            "\n",
            "## Usage\n",
            "\n",
            "Use this skill for testing.\n",
        ] + [f"line {i}\n" for i in range(200)]
        source = tmp_path / "SKILL.md"
        source.write_text("".join(lines))

        result = add_skill(
            source_path=source,
            name="myskill",
            wiki_path=wiki,
            skills_dir=skills_dir,
        )
        assert result["converted"] is True

    def test_main_with_skip_existing_skips(self, tmp_path, monkeypatch, capsys):
        """--skip-existing skips already-installed skills."""
        skills_dir = tmp_path / "skills"
        wiki = self._setup_wiki(tmp_path)
        (skills_dir / "my-skill").mkdir(parents=True)
        (skills_dir / "my-skill" / "SKILL.md").write_text("# pre-existing\n")
        scan_dir = tmp_path / "scan"
        skill_dir = scan_dir / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# new version\n")

        monkeypatch.setattr(sys, "argv", [
            "skill_add.py",
            "--scan-dir", str(scan_dir),
            "--skip-existing",
            "--wiki", str(wiki),
            "--skills-dir", str(skills_dir),
        ])
        _fake_wiki_sync.ensure_wiki.return_value = None
        _sa.main()
        out = capsys.readouterr().out
        assert "skipped" in out
