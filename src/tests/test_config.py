"""
test_config.py -- Tests for the ctx_config.Config system.

Covers:
  - ctx_config.Config: attribute types, path expansion, reload, deep merge,
    all_skill_dirs()
"""

import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path is already patched by conftest.py, but guard here too so the module
# can be run in isolation (e.g. `python -m pytest tests/test_config.py`).
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import ctx_config  # noqa: E402
from ctx_config import Config, _deep_merge  # noqa: E402


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
