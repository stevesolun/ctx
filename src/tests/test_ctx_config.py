"""
test_ctx_config.py -- Coverage for ctx_config.py (296 LOC).

ctx_config is the single source of truth for paths, thresholds, and
graph build/query parameters. A silent regression in validation would
let a misconfigured user file ship broken thresholds into every
downstream module — so the validation branches are deliberately
exercised with adversarial inputs.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

import ctx_config
from ctx_config import Config, _deep_merge, _expand


# ── Helpers ──────────────────────────────────────────────────────────────────


def _base_cfg(**overrides: Any) -> dict[str, Any]:
    """Minimal valid raw-config dict; tests layer overrides on top."""
    return overrides


# ── _deep_merge ──────────────────────────────────────────────────────────────


class TestDeepMerge:
    def test_new_keys_added(self) -> None:
        base = {"a": 1}
        _deep_merge(base, {"b": 2})
        assert base == {"a": 1, "b": 2}

    def test_scalar_override(self) -> None:
        base = {"a": 1}
        _deep_merge(base, {"a": 2})
        assert base == {"a": 2}

    def test_nested_dict_merge(self) -> None:
        base = {"paths": {"wiki_dir": "/a", "skills_dir": "/b"}}
        _deep_merge(base, {"paths": {"wiki_dir": "/override"}})
        assert base == {"paths": {"wiki_dir": "/override", "skills_dir": "/b"}}

    def test_non_dict_replaces_dict(self) -> None:
        base = {"a": {"nested": 1}}
        _deep_merge(base, {"a": "scalar"})
        assert base == {"a": "scalar"}

    def test_dict_replaces_non_dict(self) -> None:
        base = {"a": "scalar"}
        _deep_merge(base, {"a": {"nested": 1}})
        assert base == {"a": {"nested": 1}}

    def test_empty_override_no_op(self) -> None:
        base = {"a": 1}
        _deep_merge(base, {})
        assert base == {"a": 1}


# ── _expand ──────────────────────────────────────────────────────────────────


class TestExpand:
    def test_tilde(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        out = _expand("~/claude")
        assert "~" not in out
        assert str(tmp_path) in out or "claude" in out

    def test_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CTX_TEST_VAR", "/opt/data")
        assert _expand("$CTX_TEST_VAR/wiki").endswith("data/wiki") \
            or "/opt/data/wiki" in _expand("$CTX_TEST_VAR/wiki")

    def test_plain_string_unchanged(self) -> None:
        assert _expand("/tmp/plain") == "/tmp/plain"


# ── Config defaults ──────────────────────────────────────────────────────────


class TestDefaults:
    def test_packaged_default_config_matches_source_default(self) -> None:
        """The wheel-shipped ctx/config.json must not drift from src/config.json."""
        root = Path(__file__).resolve().parent.parent.parent
        source_default = json.loads(
            (root / "src" / "config.json").read_text(encoding="utf-8")
        )
        packaged_default = json.loads(
            (root / "src" / "ctx" / "config.json").read_text(encoding="utf-8")
        )

        assert packaged_default == source_default
        assert packaged_default["paths"]["stack_profile_tmp"].startswith("~")
        assert "recommendation_min_fit_score" in packaged_default["harness"]

        runtime_default = json.loads(
            resources.files("ctx").joinpath("config.json").read_text(encoding="utf-8")
        )
        runtime_config = Config(runtime_default)
        assert runtime_default == source_default
        assert str(runtime_config.stack_profile_tmp).endswith(
            ".claude\\skill-stack-profile.json"
        ) or str(runtime_config.stack_profile_tmp).endswith(
            ".claude/skill-stack-profile.json"
        )
        assert runtime_config.harness_recommendation_min_fit_score == 0.85
        assert runtime_config.graph_dense_source_threshold == 50
        assert runtime_config.graph_edge_boost_direct_link == 0.10
        assert runtime_config.graph_edge_boost_source_overlap == 0.05
        assert runtime_config.graph_edge_boost_adamic_adar == 0.04
        assert runtime_config.graph_edge_boost_type_affinity == 0.03
        assert runtime_config.graph_edge_boost_usage == 0.02
        assert runtime_config.graph_edge_boost_quality == 0.02

    def test_all_defaults_materialise(self) -> None:
        c = Config({})
        assert c.max_skills == 15
        assert c.recommendation_top_k == 5
        assert c.intent_boost_per_signal == 5
        assert c.graph_edge_weight_semantic == 0.70
        assert c.graph_edge_weight_tags == 0.15
        assert c.graph_edge_weight_tokens == 0.15
        assert c.graph_semantic_top_k == 20
        assert c.graph_semantic_build_floor == 0.50
        assert c.graph_semantic_min_cosine == 0.80
        assert c.intake_enabled is True

    def test_meta_skills_default(self) -> None:
        c = Config({})
        assert "skill-router" in c.meta_skills

    def test_tags_default_non_empty(self) -> None:
        c = Config({})
        assert len(c.all_tags) > 10

    def test_all_skill_dirs_returns_only_existing(
        self, tmp_path: Path
    ) -> None:
        skills = tmp_path / "s"
        skills.mkdir()
        # agents/extra intentionally don't exist on disk.
        raw = {
            "paths": {
                "skills_dir": str(skills),
                "agents_dir": str(tmp_path / "agents-does-not-exist"),
            },
            "extra_skill_dirs": [str(tmp_path / "extra-does-not-exist")],
        }
        c = Config(raw)
        out = c.all_skill_dirs()
        assert out == [skills]


# ── Top-K validation ─────────────────────────────────────────────────────────


class TestRecommendationTopK:
    def test_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="recommendation_top_k"):
            Config({"resolver": {"recommendation_top_k": 0}})

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError, match="recommendation_top_k"):
            Config({"resolver": {"recommendation_top_k": -1}})

    def test_one_accepted(self) -> None:
        c = Config({"resolver": {"recommendation_top_k": 1}})
        assert c.recommendation_top_k == 1

    def test_above_execution_bundle_cap_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"recommendation_top_k.*\[1, 5\]"):
            Config({"resolver": {"recommendation_top_k": 999}})


# ── Graph edge weights ───────────────────────────────────────────────────────


class TestEdgeWeights:
    def test_custom_weights_summing_to_one(self) -> None:
        c = Config({
            "graph": {
                "edge_weights": {"semantic": 0.5, "tags": 0.3, "slug_tokens": 0.2}
            }
        })
        assert c.graph_edge_weight_semantic == 0.5

    def test_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match="edge_weights must sum"):
            Config({
                "graph": {
                    "edge_weights": {
                        "semantic": 0.5, "tags": 0.3, "slug_tokens": 0.5,
                    }
                }
            })

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"edge_weights\.\w+ must be >= 0"):
            Config({
                "graph": {
                    "edge_weights": {
                        "semantic": 1.1, "tags": -0.05, "slug_tokens": -0.05,
                    }
                }
            })

    def test_tolerance_within_1e6(self) -> None:
        """Floats summing to 1.0000001 must still pass."""
        c = Config({
            "graph": {
                "edge_weights": {
                    "semantic": 0.5000001, "tags": 0.25, "slug_tokens": 0.25,
                }
            }
        })
        assert c.graph_edge_weight_semantic == pytest.approx(0.5000001)


# ── Semantic thresholds ──────────────────────────────────────────────────────


class TestSemanticThresholds:
    def test_build_floor_must_be_strictly_positive(self) -> None:
        with pytest.raises(ValueError, match="build_floor must be strictly"):
            Config({"graph": {"semantic": {"build_floor": 0.0}}})

    def test_build_floor_must_be_strictly_less_than_one(self) -> None:
        with pytest.raises(ValueError, match="build_floor must be strictly"):
            Config({"graph": {"semantic": {"build_floor": 1.0}}})

    def test_min_cosine_must_be_strictly_positive(self) -> None:
        with pytest.raises(ValueError, match="min_cosine must be strictly"):
            Config({
                "graph": {"semantic": {"build_floor": 0.1, "min_cosine": 0.0}}
            })

    def test_min_cosine_must_be_strictly_less_than_one(self) -> None:
        with pytest.raises(ValueError, match="min_cosine must be strictly"):
            Config({
                "graph": {"semantic": {"build_floor": 0.5, "min_cosine": 1.0}}
            })

    def test_build_floor_cannot_exceed_min_cosine(self) -> None:
        """Guards the 'silent gaps' invariant."""
        with pytest.raises(ValueError, match="build_floor.*must be <= min_cosine"):
            Config({
                "graph": {"semantic": {"build_floor": 0.9, "min_cosine": 0.5}}
            })

    def test_equal_thresholds_accepted(self) -> None:
        c = Config({
            "graph": {"semantic": {"build_floor": 0.6, "min_cosine": 0.6}}
        })
        assert c.graph_semantic_build_floor == c.graph_semantic_min_cosine == 0.6

    def test_negative_values_rejected(self) -> None:
        with pytest.raises(ValueError):
            Config({"graph": {"semantic": {"build_floor": -0.1}}})


# ── Intake ───────────────────────────────────────────────────────────────────


class TestIntake:
    def test_disabled(self) -> None:
        c = Config({"intake": {"enabled": False}})
        assert c.intake_enabled is False

    def test_custom_thresholds(self) -> None:
        c = Config({
            "intake": {
                "dup_threshold": 0.95,
                "near_dup_threshold": 0.82,
                "min_neighbors": 3,
                "min_body_chars": 200,
            }
        })
        assert c.intake_dup_threshold == 0.95
        assert c.intake_near_dup_threshold == 0.82
        assert c.intake_min_neighbors == 3
        assert c.intake_min_body_chars == 200

    def test_model_none_preserved(self) -> None:
        """Non-string models stay None so downstream can pick a default."""
        c = Config({"intake": {"embedding": {"model": None}}})
        assert c.intake_model is None

    def test_model_string_preserved(self) -> None:
        c = Config({"intake": {"embedding": {"model": "foo"}}})
        assert c.intake_model == "foo"

    def test_base_url_non_string_becomes_none(self) -> None:
        c = Config({"intake": {"embedding": {"base_url": 42}}})
        assert c.intake_base_url is None

    def test_allow_remote_default_false(self) -> None:
        c = Config({})
        assert c.intake_allow_remote is False


# ── get() ────────────────────────────────────────────────────────────────────


class TestGet:
    def test_top_level_key(self) -> None:
        c = Config({"custom": "value"})
        assert c.get("custom") == "value"

    def test_dotted_path(self) -> None:
        c = Config({"nested": {"deep": {"value": 42}}})
        assert c.get("nested.deep.value") == 42

    def test_missing_returns_default(self) -> None:
        c = Config({})
        assert c.get("a.b.c", default="fallback") == "fallback"

    def test_missing_returns_none_by_default(self) -> None:
        c = Config({})
        assert c.get("missing") is None

    def test_non_dict_intermediate_returns_default(self) -> None:
        """get('a.b.c') when a is a scalar should hit default."""
        c = Config({"a": "scalar"})
        assert c.get("a.b", default="fb") == "fb"


# ── _load_raw + reload ───────────────────────────────────────────────────────


class TestLoadRawAndReload:
    def test_user_overrides_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        default = tmp_path / "config.json"
        default.write_text(
            json.dumps({"resolver": {"max_skills": 10}}), encoding="utf-8"
        )
        user = tmp_path / "user.json"
        user.write_text(
            json.dumps({"resolver": {"max_skills": 99}}), encoding="utf-8"
        )
        monkeypatch.setattr(ctx_config, "_DEFAULT_CONFIG", default)
        monkeypatch.setattr(ctx_config, "_USER_CONFIG", user)
        raw = ctx_config._load_raw()
        assert raw["resolver"]["max_skills"] == 99

    def test_corrupt_default_logged_not_raised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        default = tmp_path / "config.json"
        default.write_text("not json", encoding="utf-8")
        user = tmp_path / "never-exists.json"
        monkeypatch.setattr(ctx_config, "_DEFAULT_CONFIG", default)
        monkeypatch.setattr(ctx_config, "_USER_CONFIG", user)
        raw = ctx_config._load_raw()
        assert raw == {}
        err = capsys.readouterr().err
        assert "default config" in err

    def test_corrupt_user_preserves_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        default = tmp_path / "config.json"
        default.write_text(
            json.dumps({"resolver": {"max_skills": 7}}), encoding="utf-8"
        )
        user = tmp_path / "user.json"
        user.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(ctx_config, "_DEFAULT_CONFIG", default)
        monkeypatch.setattr(ctx_config, "_USER_CONFIG", user)
        raw = ctx_config._load_raw()
        assert raw["resolver"]["max_skills"] == 7

    def test_missing_files_empty_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            ctx_config, "_DEFAULT_CONFIG", tmp_path / "missing.json"
        )
        monkeypatch.setattr(
            ctx_config, "_USER_CONFIG", tmp_path / "also-missing.json"
        )
        assert ctx_config._load_raw() == {}

    def test_reload_updates_singleton(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        default = tmp_path / "config.json"
        default.write_text(
            json.dumps({"resolver": {"max_skills": 1}}), encoding="utf-8"
        )
        monkeypatch.setattr(ctx_config, "_DEFAULT_CONFIG", default)
        monkeypatch.setattr(
            ctx_config, "_USER_CONFIG", tmp_path / "u.json"
        )
        ctx_config.reload()
        try:
            assert ctx_config.cfg.max_skills == 1
        finally:
            # Reset the singleton to avoid polluting other tests.
            monkeypatch.undo()
            ctx_config.reload()


# ── build_intake_config ──────────────────────────────────────────────────────


class TestBuildIntakeConfig:
    def test_builds_dataclass(self) -> None:
        c = Config({
            "intake": {
                "dup_threshold": 0.9,
                "near_dup_threshold": 0.7,
                "min_neighbors": 2,
                "min_neighbor_score": 0.4,
                "min_body_chars": 150,
            }
        })
        ic = c.build_intake_config()
        assert ic.dup_threshold == 0.9
        assert ic.near_dup_threshold == 0.7
        assert ic.min_neighbors == 2
        assert ic.min_neighbor_score == 0.4
        assert ic.min_body_chars == 150


# ── Path expansion in paths section ──────────────────────────────────────────


class TestPathExpansion:
    def test_tilde_expanded_in_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        c = Config({"paths": {"wiki_dir": "~/wiki"}})
        assert "~" not in str(c.wiki_dir)

    def test_env_var_expanded_in_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CTX_TEST_WIKI", "/opt/wiki")
        c = Config({"paths": {"wiki_dir": "$CTX_TEST_WIKI"}})
        assert str(c.wiki_dir).endswith("wiki")
