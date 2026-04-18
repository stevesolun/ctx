"""
test_council_runner.py -- Regression tests for council_runner.

Covers:
- Scope resolution: explicit / full / diff / graph-blast / dynamic.
- Plan hashing stability across invocations.
- Dedup cache: "cached" policy reuses, "fresh" always regenerates.
- Persistence + history listing.
- Atomic write on persist_plan survives a simulated crash.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import council_runner as cr
import toolbox_config as tc


@pytest.fixture()
def tmp_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect RUNS_DIR to a tmp folder."""
    runs = tmp_path / "toolbox-runs"
    monkeypatch.setattr(cr, "RUNS_DIR", runs)
    return runs


@pytest.fixture()
def seeded_tset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Global config has one toolbox; per-repo config absent."""
    tset = tc.ToolboxSet(
        toolboxes={
            "ship-it": tc.Toolbox(
                name="ship-it",
                description="test",
                post=("code-reviewer", "security-reviewer"),
                scope=tc.Scope(analysis="diff"),
                dedup=tc.Dedup(window_seconds=600, policy="cached"),
            ),
            "fresh-box": tc.Toolbox(
                name="fresh-box",
                post=("code-reviewer",),
                scope=tc.Scope(analysis="full"),
                dedup=tc.Dedup(window_seconds=60, policy="fresh"),
            ),
        },
        active=("ship-it",),
    )
    g_path = tmp_path / "global.json"
    tc.save_global(tset, g_path)
    monkeypatch.setattr(tc, "global_config_path", lambda: g_path)
    return tset


def test_scope_explicit_wins(seeded_tset):
    tb = seeded_tset.toolboxes["ship-it"]
    files, mode = cr.resolve_scope(
        tb, repo_root=Path.cwd(), explicit_files=["x.py", "y.py", "x.py"]
    )
    assert files == ["x.py", "y.py"]
    assert mode == "explicit"


def test_scope_full_without_git_returns_empty(seeded_tset, tmp_path: Path):
    tb = seeded_tset.toolboxes["fresh-box"]
    files, mode = cr.resolve_scope(tb, repo_root=tmp_path)
    assert mode == "full"
    assert files == []  # not a git repo


def test_scope_graph_blast_expands_one_hop():
    tb = tc.Toolbox(
        name="t", post=("code-reviewer",),
        scope=tc.Scope(analysis="graph-blast"),
    )
    edges = {"a.py": {"b.py", "c.py"}, "b.py": {"d.py"}}
    # Explicit files path doesn't use edges, so test helper directly
    result = cr._graph_blast_files(["a.py"], edges)
    assert result == ["a.py", "b.py", "c.py"]
    result2 = cr._graph_blast_files(["a.py", "b.py"], edges)
    assert "d.py" in result2


def test_scope_dynamic_falls_back_to_full_when_no_diff(seeded_tset, tmp_path):
    tb = tc.Toolbox(
        name="t", post=("x",),
        scope=tc.Scope(analysis="dynamic"),
    )
    files, mode = cr.resolve_scope(tb, repo_root=tmp_path)
    assert mode == "dynamic:full"


def test_hash_plan_is_stable():
    h1 = cr._hash_plan("ship-it", ("a", "b"), ("x.py",), "diff")
    h2 = cr._hash_plan("ship-it", ("a", "b"), ("x.py",), "diff")
    assert h1 == h2
    h3 = cr._hash_plan("ship-it", ("a", "b"), ("x.py",), "full")
    assert h3 != h1


def test_build_plan_persists_and_dedups(seeded_tset, tmp_runs, monkeypatch):
    # Force resolve_scope to return a known set so we don't depend on git
    monkeypatch.setattr(
        cr, "resolve_scope",
        lambda tb, repo_root, explicit_files=None, graph_edges=None: (["a.py"], "diff"),
    )
    plan1 = cr.build_plan("ship-it", now=1000.0)
    assert plan1.source == "fresh"
    cr.persist_plan(plan1)

    # Same inputs => cached lookup returns the persisted plan
    plan2 = cr.build_plan("ship-it", now=1050.0)
    assert plan2.source == "cached"
    assert plan2.plan_hash == plan1.plan_hash
    # created_at reflects the ORIGINAL plan, not the new call
    assert plan2.created_at == 1000.0


def test_build_plan_cache_expires_outside_window(seeded_tset, tmp_runs, monkeypatch):
    monkeypatch.setattr(
        cr, "resolve_scope",
        lambda tb, repo_root, explicit_files=None, graph_edges=None: (["a.py"], "diff"),
    )
    plan1 = cr.build_plan("ship-it", now=1000.0)
    cr.persist_plan(plan1)
    # window is 600s; ask 700s later
    real_time = cr.time.time
    monkeypatch.setattr(cr.time, "time", lambda: 1700.0)
    plan2 = cr.build_plan("ship-it")
    assert plan2.source == "fresh"
    monkeypatch.setattr(cr.time, "time", real_time)


def test_fresh_policy_never_caches(seeded_tset, tmp_runs, monkeypatch):
    monkeypatch.setattr(
        cr, "resolve_scope",
        lambda tb, repo_root, explicit_files=None, graph_edges=None: (["a.py"], "full"),
    )
    # fresh-box has policy="fresh" \u2014 should never return cached
    plan1 = cr.build_plan("fresh-box", now=1000.0)
    cr.persist_plan(plan1)
    plan2 = cr.build_plan("fresh-box", now=1010.0)
    assert plan2.source == "fresh"


def test_unknown_toolbox_raises(seeded_tset):
    with pytest.raises(KeyError):
        cr.build_plan("does-not-exist")


def test_persist_plan_atomic_on_crash(seeded_tset, tmp_runs, monkeypatch):
    monkeypatch.setattr(
        cr, "resolve_scope",
        lambda tb, repo_root, explicit_files=None, graph_edges=None: (["a.py"], "diff"),
    )
    plan = cr.build_plan("ship-it", now=1000.0)
    # Pre-existing good plan
    persisted = cr.persist_plan(plan)
    original = persisted.read_bytes()

    def boom(*a, **kw):
        raise OSError("simulated")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        cr.persist_plan(plan)

    assert persisted.read_bytes() == original


def test_cmd_history_filters_by_toolbox(seeded_tset, tmp_runs, monkeypatch, capsys):
    # Persist two plans for different toolboxes
    monkeypatch.setattr(
        cr, "resolve_scope",
        lambda tb, repo_root, explicit_files=None, graph_edges=None: (["a.py"], "diff"),
    )
    p1 = cr.build_plan("ship-it", now=1000.0)
    cr.persist_plan(p1)
    monkeypatch.setattr(
        cr, "resolve_scope",
        lambda tb, repo_root, explicit_files=None, graph_edges=None: (["a.py"], "full"),
    )
    p2 = cr.build_plan("fresh-box", now=2000.0)
    cr.persist_plan(p2)

    # History of ship-it only shows p1
    assert cr.main(["history", "--toolbox", "ship-it", "--limit", "10"]) == 0
    out = capsys.readouterr().out
    entries = json.loads(out)
    assert len(entries) == 1
    assert entries[0]["toolbox"] == "ship-it"


def test_cmd_purge_removes_old_and_corrupt(tmp_runs, capsys):
    tmp_runs.mkdir(parents=True, exist_ok=True)
    old = tmp_runs / "old.json"
    old.write_text(json.dumps({"toolbox": "x", "created_at": 0}), encoding="utf-8")
    corrupt = tmp_runs / "corrupt.json"
    corrupt.write_text("not json", encoding="utf-8")
    fresh = tmp_runs / "fresh.json"
    fresh.write_text(
        json.dumps({"toolbox": "y", "created_at": time.time()}),
        encoding="utf-8",
    )

    assert cr.main(["purge", "--older-than-days", "1"]) == 0
    assert not old.exists()
    assert not corrupt.exists()
    assert fresh.exists()
