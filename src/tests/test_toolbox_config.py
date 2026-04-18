"""
test_toolbox_config.py -- Regression tests for the toolbox data model + loader.

Covers:
- Dataclass validation (scope.analysis / dedup.policy / budget bounds).
- Round-trip: from_dict -> to_dict -> from_dict preserves all fields.
- JSON global config load/save.
- YAML per-repo config load (skipped when PyYAML missing).
- Merge precedence: per-repo overrides global by name.
- Active-list union, with per-repo preferences first.
- Schema-version rejection.
- Atomic save survives a mid-write crash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import toolbox_config as tc


def test_scope_rejects_invalid_analysis():
    with pytest.raises(ValueError, match="scope.analysis"):
        tc.Scope(analysis="bogus")


def test_dedup_rejects_invalid_policy():
    with pytest.raises(ValueError, match="dedup.policy"):
        tc.Dedup(policy="nope")


def test_dedup_rejects_negative_window():
    with pytest.raises(ValueError, match="window_seconds"):
        tc.Dedup(window_seconds=-1)


def test_budget_rejects_nonpositive():
    with pytest.raises(ValueError, match="max_tokens"):
        tc.Budget(max_tokens=0)
    with pytest.raises(ValueError, match="max_seconds"):
        tc.Budget(max_seconds=0)


def test_toolbox_round_trip():
    raw = {
        "description": "end-of-feature council",
        "pre": ["python-patterns"],
        "post": ["code-reviewer", "security-reviewer"],
        "scope": {
            "projects": ["*"],
            "signals": ["python"],
            "analysis": "graph-blast",
        },
        "trigger": {
            "slash": True,
            "pre_commit": True,
            "session_end": False,
            "file_save": "**/auth/**",
        },
        "budget": {"max_tokens": 50_000, "max_seconds": 120},
        "dedup": {"window_seconds": 300, "policy": "cached"},
        "guardrail": True,
    }
    tb = tc.Toolbox.from_dict("test-box", raw)
    assert tb.name == "test-box"
    assert tb.post == ("code-reviewer", "security-reviewer")
    assert tb.trigger.file_save == "**/auth/**"
    assert tb.guardrail is True
    # round-trip
    again = tc.Toolbox.from_dict("test-box", tb.to_dict())
    assert again == tb


def test_toolboxset_rejects_wrong_version():
    with pytest.raises(ValueError, match="Unsupported toolbox config version"):
        tc.ToolboxSet.from_dict({"version": 999, "toolboxes": {}})


def test_toolboxset_with_and_without():
    empty = tc.ToolboxSet.empty()
    tb = tc.Toolbox(name="alpha", post=("x",))
    with_alpha = empty.with_toolbox(tb)
    assert "alpha" in with_alpha.toolboxes
    again = with_alpha.without_toolbox("alpha")
    assert "alpha" not in again.toolboxes


def test_activate_requires_existing():
    empty = tc.ToolboxSet.empty()
    with pytest.raises(KeyError):
        empty.activate("nope")


def test_activate_and_deactivate_idempotent():
    tb = tc.Toolbox(name="alpha")
    tset = tc.ToolboxSet(toolboxes={"alpha": tb}, active=())
    tset2 = tset.activate("alpha")
    assert tset2.active == ("alpha",)
    assert tset2.activate("alpha") is tset2 or tset2.activate("alpha").active == ("alpha",)
    tset3 = tset2.deactivate("alpha")
    assert tset3.active == ()
    assert tset3.deactivate("alpha").active == ()


def test_load_global_returns_empty_on_missing(tmp_path: Path):
    missing = tmp_path / "does-not-exist.json"
    tset = tc.load_global(missing)
    assert tset.toolboxes == {}


def test_load_global_roundtrip(tmp_path: Path):
    tb = tc.Toolbox(name="alpha", description="x", post=("code-reviewer",))
    tset = tc.ToolboxSet(toolboxes={"alpha": tb}, active=("alpha",))
    target = tmp_path / "toolboxes.json"
    tc.save_global(tset, target)
    loaded = tc.load_global(target)
    assert loaded.toolboxes["alpha"].post == ("code-reviewer",)
    assert loaded.active == ("alpha",)


def test_merged_per_repo_overrides_global(tmp_path: Path, monkeypatch):
    # Global toolbox
    g_tb = tc.Toolbox(name="ship-it", description="global", post=("x",))
    g_set = tc.ToolboxSet(toolboxes={"ship-it": g_tb}, active=("ship-it",))
    g_path = tmp_path / "global.json"
    tc.save_global(g_set, g_path)

    # Monkey-patch global_config_path so merged() picks our tmp file
    monkeypatch.setattr(tc, "global_config_path", lambda: g_path)

    # Per-repo config with overriding ship-it + additional toolbox
    if not tc._HAS_YAML:
        pytest.skip("PyYAML not installed")
    import yaml  # type: ignore[import-untyped]

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_cfg = {
        "version": 1,
        "toolboxes": {
            "ship-it": {"description": "repo-local", "post": ["y"]},
            "extra": {"post": ["z"]},
        },
        "active": ["extra"],
    }
    (repo_root / ".toolbox.yaml").write_text(yaml.safe_dump(repo_cfg), encoding="utf-8")

    result = tc.merged(repo_root=repo_root)
    # repo wins on ship-it
    assert result.toolboxes["ship-it"].description == "repo-local"
    assert result.toolboxes["ship-it"].post == ("y",)
    # extra toolbox is present
    assert "extra" in result.toolboxes
    # active list: repo preferences first, union with global
    assert result.active[0] == "extra"
    assert "ship-it" in result.active


def test_invalid_version_in_file_raises(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"version": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        tc.load_global(p)


def test_invalid_json_raises(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json {{{", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid JSON"):
        tc.load_global(p)


def test_save_global_is_atomic_on_crash(tmp_path: Path, monkeypatch):
    tb = tc.Toolbox(name="alpha", post=("code-reviewer",))
    tset = tc.ToolboxSet(toolboxes={"alpha": tb}, active=("alpha",))
    target = tmp_path / "toolboxes.json"
    # Pre-write a known-good file
    tc.save_global(tset, target)
    original = target.read_bytes()

    # Sabotage os.replace so the atomic swap fails
    def boom(*args, **kwargs):
        raise OSError("simulated disk error")

    monkeypatch.setattr("os.replace", boom)
    new_tset = tset.with_toolbox(tc.Toolbox(name="beta"))
    with pytest.raises(OSError):
        tc.save_global(new_tset, target)

    # Original file must still be intact \u2014 no partial write
    assert target.read_bytes() == original
