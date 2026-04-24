"""
test_stack_skill_map.py -- pins the one-source-of-truth invariant.

Before P2.4 there were two maps: resolve_skills.STACK_SKILL_MAP (~40
entries) and usage_tracker.SIGNAL_SKILL_MAP (~20). usage_tracker's
subset missed stacks the resolver knew about, which caused use_count
telemetry to silently fail to increment — lifecycle then flagged the
skills as stale. This test suite fails loudly if a future refactor
reintroduces a divergence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from ctx.core.resolve.stack_skill_map import (
    SIGNAL_SKILL_MAP,
    STACK_SKILL_MAP,
    skills_for,
)


class TestIdentity:
    def test_signal_map_is_stack_map(self):
        """SIGNAL_SKILL_MAP and STACK_SKILL_MAP are the same object.
        Divergence would reopen the bug P2.4 closed."""
        assert SIGNAL_SKILL_MAP is STACK_SKILL_MAP

    def test_map_is_immutable_view(self):
        """MappingProxyType prevents one call site from mutating shared
        state and breaking another. Assigning a key must raise."""
        with pytest.raises(TypeError):
            STACK_SKILL_MAP["new-stack"] = ["whatever"]  # type: ignore[index]

    def test_usage_tracker_import_points_at_shared(self):
        import usage_tracker
        assert usage_tracker.SIGNAL_SKILL_MAP is STACK_SKILL_MAP

    def test_resolve_skills_import_points_at_shared(self):
        from ctx.core.resolve import resolve_skills
        assert resolve_skills.STACK_SKILL_MAP is STACK_SKILL_MAP


class TestSkillsFor:
    def test_known_stack_returns_skills(self):
        assert skills_for("react") == ["react", "frontend-design"]
        assert skills_for("docker") == ["docker"]
        assert skills_for("django") == ["django"]

    def test_unknown_returns_empty_list(self):
        """Explicit empty list — NEVER fall through to [signal]. Pins
        the fix from 1c55d1e: an unmapped signal must not create a
        phantom skill slug downstream."""
        assert skills_for("never-heard-of-it") == []

    def test_returns_a_copy_not_shared_list(self):
        """skills_for returns a list(). Caller mutations (e.g. appending
        a boost skill) must not bleed into the shared map."""
        r = skills_for("react")
        r.append("MUTATED")
        assert "MUTATED" not in STACK_SKILL_MAP["react"]


class TestCoverageBaseline:
    """Pins the minimum set of stacks a caller can count on. If the
    map ever shrinks below this baseline, usage_tracker / resolver
    callers start silently dropping signals — fail the test loudly."""

    _REQUIRED_STACKS = frozenset({
        "fastapi", "django", "flask", "react", "nextjs", "vue",
        "angular", "langchain", "pytorch", "openai-sdk", "anthropic-sdk",
        "docker", "kubernetes", "terraform", "pytest", "jest",
        "playwright", "prisma", "sqlalchemy", "cypress", "dbt",
        "crewai", "huggingface", "github-actions",
    })

    def test_baseline_stacks_all_present(self):
        missing = self._REQUIRED_STACKS - set(STACK_SKILL_MAP)
        assert not missing, (
            f"Lost stacks from STACK_SKILL_MAP: {sorted(missing)}. "
            "usage_tracker / resolver callers for these stacks will "
            "silently drop their signals."
        )

    def test_each_mapping_non_empty(self):
        """An empty-list mapping means 'know the stack but map it to
        nothing'. That used to be a maintenance intermediate state;
        post-P2.4 we require every mapped stack to yield >= 1 skill."""
        for stack, skills in STACK_SKILL_MAP.items():
            assert skills, f"stack {stack!r} maps to empty list"

    def test_no_self_referential_signal_as_skill(self):
        """Defensive: an entry like {"foo": ["foo"]} where "foo" is a
        bare signal with no real skill by that name is a smell — but
        not always wrong (``fastapi`` really does have a fastapi
        skill). We just assert the shape is always list[str]."""
        for skills in STACK_SKILL_MAP.values():
            assert isinstance(skills, list)
            for s in skills:
                assert isinstance(s, str) and s
