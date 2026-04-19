"""
test_skill_quality_nested_agents.py — P1-6 regression tests.

Verifies that ``_read_skill_source`` finds agent files living in nested
subdirectories of ``agents_dir`` (e.g. ``agents/design/foo.md``), not
only at the flat ``agents/<slug>.md`` level.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skill_quality as sq  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

_AGENT_MD = (
    "---\nname: test-agent\ndescription: A nested test agent.\n---\n"
    "# Test Agent\n\nSome body text for the agent.\n"
)


def _make_sources(tmp_path: Path) -> sq.SignalSources:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    wiki_dir = tmp_path / "wiki"
    events_path = tmp_path / "events.jsonl"
    skills_dir.mkdir()
    agents_dir.mkdir()
    wiki_dir.mkdir()
    events_path.touch()
    return sq.SignalSources(
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
        events_path=events_path,
    )


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


def test_flat_agent_still_found(tmp_path: Path) -> None:
    """Flat layout (agents/<slug>.md) must still work after the rglob change."""
    sources = _make_sources(tmp_path)
    (sources.agents_dir / "flat-agent.md").write_text(_AGENT_MD, encoding="utf-8")

    subject_type, raw_md = sq._read_skill_source("flat-agent", sources)

    assert subject_type == "agent"
    assert "Test Agent" in raw_md


def test_nested_one_level_found(tmp_path: Path) -> None:
    """Agent at agents/design/design-agent.md is discovered via rglob."""
    sources = _make_sources(tmp_path)
    subdir = sources.agents_dir / "design"
    subdir.mkdir()
    (subdir / "design-agent.md").write_text(_AGENT_MD, encoding="utf-8")

    subject_type, raw_md = sq._read_skill_source("design-agent", sources)

    assert subject_type == "agent"
    assert "Test Agent" in raw_md


def test_nested_two_levels_found(tmp_path: Path) -> None:
    """Agent nested two levels deep (agents/ops/infra/ops-agent.md) is found."""
    sources = _make_sources(tmp_path)
    deep = sources.agents_dir / "ops" / "infra"
    deep.mkdir(parents=True)
    (deep / "ops-agent.md").write_text(_AGENT_MD, encoding="utf-8")

    subject_type, raw_md = sq._read_skill_source("ops-agent", sources)

    assert subject_type == "agent"
    assert "Test Agent" in raw_md


def test_nested_agent_not_found_raises(tmp_path: Path) -> None:
    """A slug that does not exist raises FileNotFoundError."""
    sources = _make_sources(tmp_path)

    with pytest.raises(FileNotFoundError, match="no skill or agent file"):
        sq._read_skill_source("ghost-agent", sources)


def test_ambiguous_nested_agent_raises(tmp_path: Path) -> None:
    """Two files with the same name in different subdirs raises FileNotFoundError."""
    sources = _make_sources(tmp_path)
    (sources.agents_dir / "catA").mkdir()
    (sources.agents_dir / "catB").mkdir()
    (sources.agents_dir / "catA" / "dupe.md").write_text(_AGENT_MD, encoding="utf-8")
    (sources.agents_dir / "catB" / "dupe.md").write_text(_AGENT_MD, encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="ambiguous"):
        sq._read_skill_source("dupe", sources)


def test_skill_takes_priority_over_nested_agent(tmp_path: Path) -> None:
    """If both a skill and a nested agent share a slug, the skill wins."""
    sources = _make_sources(tmp_path)
    skill_dir = sources.skills_dir / "shared-slug"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: shared-slug\n---\n# Skill body\n", encoding="utf-8"
    )
    nested = sources.agents_dir / "cat"
    nested.mkdir()
    (nested / "shared-slug.md").write_text(_AGENT_MD, encoding="utf-8")

    subject_type, raw_md = sq._read_skill_source("shared-slug", sources)

    assert subject_type == "skill"
    assert "Skill body" in raw_md
