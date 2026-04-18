"""
test_flatten_agents.py -- Regression tests for the nested-agent promoter.

The /agents Library only auto-discovers top-level .md files. flatten_agents.py
walks nested category subdirs and copies real agents (YAML frontmatter with
name:) to top-level siblings, leaving reference notes alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import flatten_agents


@pytest.fixture()
def agents_tree(tmp_path: Path) -> Path:
    root = tmp_path / "agents"
    # Real agent nested under a category folder.
    nested_agent = root / "design" / "design-brand-guardian.md"
    nested_agent.parent.mkdir(parents=True)
    nested_agent.write_text(
        "---\nname: Brand Guardian\ndescription: x\n---\n\n# Brand Guardian\n",
        encoding="utf-8",
    )
    # Real agent nested without the prefix convention.
    bare = root / "game-development" / "level-designer.md"
    bare.parent.mkdir(parents=True)
    bare.write_text(
        "---\nname: Level Designer\n---\n\n# Level Designer\n",
        encoding="utf-8",
    )
    # Reference note — no frontmatter. Must be skipped.
    ref = root / "skill-router" / "check-gates.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("# Check Gates — skill-router\n\nJust a note.\n", encoding="utf-8")
    # Existing top-level agent (already discoverable).
    (root / "already-top.md").write_text(
        "---\nname: Already\n---\n\n# Already\n",
        encoding="utf-8",
    )
    return root


def test_dry_run_produces_plan_but_no_files(agents_tree):
    plan, warnings = flatten_agents.plan_flatten(agents_tree)
    names = {dst.name for _, dst in plan}
    assert names == {"design-brand-guardian.md", "level-designer.md"}
    assert warnings == []
    # Top-level copies do NOT exist yet.
    assert not (agents_tree / "design-brand-guardian.md").exists()


def test_apply_promotes_nested_agents_to_top_level(agents_tree):
    plan, _ = flatten_agents.plan_flatten(agents_tree)
    copied = flatten_agents.apply_plan(plan, verbose=False)
    assert copied == 2
    assert (agents_tree / "design-brand-guardian.md").exists()
    assert (agents_tree / "level-designer.md").exists()
    # Originals untouched.
    assert (agents_tree / "design" / "design-brand-guardian.md").exists()


def test_reference_notes_are_skipped(agents_tree):
    plan, _ = flatten_agents.plan_flatten(agents_tree)
    # check-gates.md has no frontmatter — never in the plan.
    srcs = {src.name for src, _ in plan}
    assert "check-gates.md" not in srcs


def test_collision_with_identical_content_is_silent(agents_tree):
    # Pre-create a sibling with identical content.
    nested = agents_tree / "design" / "design-brand-guardian.md"
    (agents_tree / "design-brand-guardian.md").write_bytes(nested.read_bytes())
    plan, warnings = flatten_agents.plan_flatten(agents_tree)
    srcs = {src.name for src, _ in plan}
    assert "design-brand-guardian.md" not in srcs
    assert warnings == []


def test_collision_with_different_content_warns(agents_tree):
    (agents_tree / "design-brand-guardian.md").write_text(
        "---\nname: Different\n---\n",
        encoding="utf-8",
    )
    plan, warnings = flatten_agents.plan_flatten(agents_tree)
    assert any("collision" in w for w in warnings)


def test_has_name_frontmatter_detects_real_agents(tmp_path):
    agent = tmp_path / "agent.md"
    agent.write_text("---\nname: X\n---\n", encoding="utf-8")
    assert flatten_agents.has_name_frontmatter(agent) is True


def test_has_name_frontmatter_rejects_notes(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# No frontmatter here\n", encoding="utf-8")
    assert flatten_agents.has_name_frontmatter(note) is False


def test_has_name_frontmatter_rejects_frontmatter_without_name(tmp_path):
    stub = tmp_path / "stub.md"
    stub.write_text("---\ntype: reference\n---\n", encoding="utf-8")
    assert flatten_agents.has_name_frontmatter(stub) is False
