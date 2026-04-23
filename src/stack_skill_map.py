"""
stack_skill_map.py -- Single source of truth for stack-signal -> skill mapping.

Code-reviewer HIGH (P2.4). Prior state: ``resolve_skills.STACK_SKILL_MAP``
(~40 entries) and ``usage_tracker.SIGNAL_SKILL_MAP`` (~20 entries) both
mapped stack signals to skill slugs, with no shared source. Missing
entries in ``usage_tracker`` caused ``use_count`` to never increment
for skills in stacks like ``angular``, ``crewai``, ``cypress``,
``dbt``, ``django``, ``docker``, etc. — telemetry looked like those
skills were unused, and the lifecycle module then flagged them for
unload. Spurious unload suggestions.

This module consolidates both. Callers that want the full resolver
mapping import ``STACK_SKILL_MAP``. Callers that want the lighter
signal-level mapping used by ``usage_tracker`` import the same dict
— it's the SAME mapping; the old "minimal" subset was a mistake,
not a deliberate scoping decision.

When adding a new stack:
  1. Add the entry here (not in a caller-local copy).
  2. If the new stack surfaces via ``context_monitor.extract_signals``,
     make sure the signal name matches a key here.
  3. Tests in ``test_stack_skill_map.py`` pin the one-source-of-truth
     invariant so a future divergence fails loudly.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


# ── Canonical mapping ────────────────────────────────────────────────
#
# Immutable view — callers that import STACK_SKILL_MAP from this
# module get a MappingProxyType so they can't accidentally mutate
# shared state from one call site and break another.

_RAW: dict[str, list[str]] = {
    # ── Web frameworks ──────────────────────────────────────────────
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "express": ["express"],
    "nestjs": ["nestjs"],
    "react": ["react", "frontend-design"],
    "nextjs": ["nextjs", "react", "frontend-design"],
    "vue": ["vue", "frontend-design"],
    "nuxt": ["nuxt", "vue", "frontend-design"],
    "angular": ["angular", "frontend-design"],
    "svelte": ["svelte", "frontend-design"],
    # ── AI / ML ─────────────────────────────────────────────────────
    "langchain": ["langchain"],
    "llamaindex": ["llamaindex"],
    "crewai": ["crewai"],
    "pytorch": ["pytorch"],
    "tensorflow": ["tensorflow"],
    "huggingface": ["huggingface"],
    "openai-sdk": ["openai-sdk"],
    "anthropic-sdk": ["anthropic-sdk"],
    "mcp": ["mcp-dev"],
    # ── Infrastructure ──────────────────────────────────────────────
    "docker": ["docker"],
    "docker-compose": ["docker"],
    "kubernetes": ["kubernetes"],
    "terraform": ["terraform"],
    "github-actions": ["github-actions"],
    "gitlab-ci": ["gitlab-ci"],
    "aws-cdk": ["aws"],
    "vercel": ["vercel"],
    # ── Data ────────────────────────────────────────────────────────
    "sqlalchemy": ["sqlalchemy"],
    "prisma": ["prisma"],
    "typeorm": ["typeorm"],
    "drizzle": ["drizzle"],
    "redis": ["redis"],
    "dbt": ["dbt"],
    # ── Testing ─────────────────────────────────────────────────────
    "pytest": ["pytest"],
    "jest": ["jest"],
    "vitest": ["vitest"],
    "playwright": ["playwright"],
    "cypress": ["cypress"],
    # ── Docs ────────────────────────────────────────────────────────
    "openapi": ["openapi"],
    "mkdocs": ["mkdocs"],
    "docusaurus": ["docusaurus"],
    # ── Build tooling ───────────────────────────────────────────────
    "vite": ["vite"],
    "webpack": ["webpack"],
    "turborepo": ["turborepo"],
}


STACK_SKILL_MAP: Mapping[str, list[str]] = MappingProxyType(_RAW)

# Legacy alias retained so ``usage_tracker.SIGNAL_SKILL_MAP`` imports
# keep resolving. New code should use ``STACK_SKILL_MAP`` directly.
SIGNAL_SKILL_MAP: Mapping[str, list[str]] = STACK_SKILL_MAP


def skills_for(signal: str) -> list[str]:
    """Return the skill slugs for *signal*. Empty list for unmapped.

    Do NOT fall through to ``[signal]`` — that's the bug code-reviewer
    flagged in ``usage_tracker.signals_to_skills`` (fixed in 1c55d1e).
    An unmapped signal means "we don't know the skills for this stack",
    not "assume the signal name is a skill name".
    """
    return list(STACK_SKILL_MAP.get(signal, []))
