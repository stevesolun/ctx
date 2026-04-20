#!/usr/bin/env python3
"""
mcp_quality_signals.py -- Six deterministic quality-signal extractors for MCP servers.

Signals (all normalized to [0, 1]):

  1. popularity  — GitHub star count, log-scaled.  No enrichment yet → 0.5.
  2. freshness   — Exponential decay from the most-recent commit.  No age → 0.5.
  3. structural  — Five presence checks on McpRecord fields (description,
                   repo URL, tags, transports, language).
  4. graph       — Graph-connectivity score reusing the skill-graph spirit:
                   general degree + cross-type bonus for skill/agent edges.
  5. trust       — Official status, license presence, and author presence.
  6. runtime     — Real-usage telemetry (invocation count, error rate).
                   All inputs default to 0/None → 0.5 until Phase 5 data lands.

Each extractor is a pure function: takes already-loaded inputs, returns a
``SignalResult``.  All I/O is the caller's job; this module is deterministic
and trivially unit-testable.

The MCP scorer (separate module) will compose a weighted sum of these six
results, apply hard floors, and map to a quality grade.
"""

from __future__ import annotations

import math
from typing import Any

from mcp_entity import McpRecord
from quality_signals import SignalResult

__all__ = [
    "popularity_signal",
    "freshness_signal",
    "structural_signal",
    "graph_signal",
    "trust_signal",
    "runtime_signal",
]

# ────────────────────────────────────────────────────────────────────
# Module-scope constants (referenced by tests and the scorer)
# ────────────────────────────────────────────────────────────────────

# popularity_signal
_POPULARITY_STAR_SATURATION = 1_000

# freshness_signal
_FRESHNESS_HALF_LIFE_DAYS = 90.0
_LN2 = math.log(2.0)

# graph_signal
_GRAPH_DEGREE_SATURATION = 20
_GRAPH_DEGREE_WEIGHT = 0.7
_GRAPH_CROSS_TYPE_WEIGHT = 0.3
_GRAPH_CROSS_TYPE_SATURATION = 5

# trust_signal
_TRUST_OFFICIAL_WEIGHT = 0.5
_TRUST_LICENSE_WEIGHT = 0.3
_TRUST_AUTHOR_WEIGHT = 0.2

# runtime_signal
_RUNTIME_USAGE_SATURATION = 10
_RUNTIME_ERROR_PENALTY_SCALE = 5.0  # 20 % errors → 0 reliability


# ────────────────────────────────────────────────────────────────────
# 1. Popularity
# ────────────────────────────────────────────────────────────────────


def popularity_signal(
    *,
    stars: int | None,
    star_saturation: int = _POPULARITY_STAR_SATURATION,
) -> SignalResult:
    """GitHub stars, log-scaled.

    Args:
        stars: GitHub star count from enrichment.  ``None`` means
            enrichment has not run yet (neutral 0.5).
        star_saturation: Star count at which the score saturates to 1.0.
            Defaults to ``_POPULARITY_STAR_SATURATION`` (1 000).

    Returns:
        ``SignalResult`` with evidence keys
        ``{"stars", "neutral_for_unknown"}``.

    Raises:
        ValueError: If ``star_saturation <= 0`` or ``stars < 0``.
    """
    if star_saturation <= 0:
        raise ValueError("star_saturation must be > 0")

    if stars is None:
        return SignalResult(
            score=0.5,
            evidence={"stars": None, "neutral_for_unknown": True},
        )

    if stars < 0:
        raise ValueError("stars must be >= 0")

    if stars == 0:
        score = 0.0
    else:
        # log10(stars + 1) / log10(saturation + 1) — saturates at 1.0
        score = math.log10(stars + 1.0) / math.log10(star_saturation + 1.0)

    evidence: dict[str, Any] = {"stars": stars, "neutral_for_unknown": False}
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# 2. Freshness
# ────────────────────────────────────────────────────────────────────


def freshness_signal(
    *,
    last_commit_age_days: float | None,
    half_life_days: float = _FRESHNESS_HALF_LIFE_DAYS,
) -> SignalResult:
    """Exponential decay from 1.0 (just committed) by half-life.

    Args:
        last_commit_age_days: Days since the most recent commit.
            ``None`` means no enrichment yet (neutral 0.5).
        half_life_days: Days at which the score decays to 0.5.
            Defaults to ``_FRESHNESS_HALF_LIFE_DAYS`` (90).

    Returns:
        ``SignalResult`` with evidence keys
        ``{"last_commit_age_days", "neutral_for_unknown"}``.

    Raises:
        ValueError: If ``half_life_days <= 0`` or
            ``last_commit_age_days < 0``.
    """
    if half_life_days <= 0.0:
        raise ValueError("half_life_days must be > 0")

    if last_commit_age_days is None:
        return SignalResult(
            score=0.5,
            evidence={"last_commit_age_days": None, "neutral_for_unknown": True},
        )

    if last_commit_age_days < 0.0:
        raise ValueError("last_commit_age_days must be >= 0 when present")

    score = math.exp(-last_commit_age_days * _LN2 / half_life_days)

    evidence: dict[str, Any] = {
        "last_commit_age_days": last_commit_age_days,
        "neutral_for_unknown": False,
    }
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# 3. Structural
# ────────────────────────────────────────────────────────────────────

_DESCRIPTION_PLACEHOLDER = "No description available."


def structural_signal(
    *,
    record: McpRecord,
) -> SignalResult:
    """Five structural presence checks; each pass adds 1/5 to the score.

    Checks (in order):
        - ``has_description``: description is not the fallback placeholder.
        - ``has_repo_url``: ``github_url`` or ``homepage_url`` is set.
        - ``has_tags``: ``tags`` is non-empty and not just ``["uncategorized"]``.
        - ``has_transports``: ``transports`` is non-empty.
        - ``has_language``: ``language`` is set.

    Args:
        record: A fully-normalised ``McpRecord``.

    Returns:
        ``SignalResult`` with evidence ``{check_name: bool, ...}`` for
        each of the five checks.
    """
    checks: dict[str, bool] = {
        "has_description": bool(record.description)
        and record.description != _DESCRIPTION_PLACEHOLDER,
        "has_repo_url": bool(record.github_url or record.homepage_url),
        "has_tags": bool(record.tags)
        and not (len(record.tags) == 1 and record.tags[0] == "uncategorized"),
        "has_transports": bool(record.transports),
        "has_language": bool(record.language),
    }

    passed = sum(1 for v in checks.values() if v)
    score = passed / 5.0

    evidence: dict[str, Any] = dict(checks)
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# 4. Graph connectivity
# ────────────────────────────────────────────────────────────────────


def graph_signal(
    *,
    degree: int,
    cross_type_degree: int,
    degree_saturation: int = _GRAPH_DEGREE_SATURATION,
) -> SignalResult:
    """Graph-connectivity score with a cross-type bonus.

    Two weighted terms:
        - ``0.7 * min(1, degree / degree_saturation)`` — general
          connectedness.
        - ``0.3 * min(1, cross_type_degree / 5)`` — bonus for edges to
          skill/agent nodes, reflecting the cross-type recommendation
          feature.

    Args:
        degree: Total edge count incident to this MCP node.
        cross_type_degree: Subset of ``degree`` that crosses to skill or
            agent nodes.
        degree_saturation: Degree at which the connectivity term saturates
            to 1.0.  Defaults to ``_GRAPH_DEGREE_SATURATION`` (20).

    Returns:
        ``SignalResult`` with evidence
        ``{"degree", "cross_type_degree", "isolated"}``.

    Raises:
        ValueError: If any count is negative or ``degree_saturation <= 0``.
    """
    if degree < 0:
        raise ValueError("degree must be >= 0")
    if cross_type_degree < 0:
        raise ValueError("cross_type_degree must be >= 0")
    if degree_saturation <= 0:
        raise ValueError("degree_saturation must be > 0")

    degree_term = min(1.0, degree / float(degree_saturation))
    cross_term = min(1.0, cross_type_degree / float(_GRAPH_CROSS_TYPE_SATURATION))

    score = _GRAPH_DEGREE_WEIGHT * degree_term + _GRAPH_CROSS_TYPE_WEIGHT * cross_term

    evidence: dict[str, Any] = {
        "degree": degree,
        "cross_type_degree": cross_type_degree,
        "isolated": degree == 0,
    }
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# 5. Trust
# ────────────────────────────────────────────────────────────────────


def trust_signal(
    *,
    record: McpRecord,
) -> SignalResult:
    """Three trust indicators combined as a weighted sum.

    Weights:
        - ``0.5`` — ``official_or_org``: ``"official"`` in tags OR
          ``author_type == "org"``.
        - ``0.3`` — ``has_license``: ``license`` field is set.
        - ``0.2`` — ``has_known_author``: ``author`` field is set and
          non-empty.

    Args:
        record: A fully-normalised ``McpRecord``.

    Returns:
        ``SignalResult`` with evidence
        ``{"official_or_org", "has_license", "has_author"}``.
    """
    official_or_org: bool = (
        "official" in record.tags or record.author_type == "org"
    )
    has_license: bool = bool(record.license)
    has_author: bool = bool(record.author)

    score = (
        _TRUST_OFFICIAL_WEIGHT * (1.0 if official_or_org else 0.0)
        + _TRUST_LICENSE_WEIGHT * (1.0 if has_license else 0.0)
        + _TRUST_AUTHOR_WEIGHT * (1.0 if has_author else 0.0)
    )

    evidence: dict[str, Any] = {
        "official_or_org": official_or_org,
        "has_license": has_license,
        "has_author": has_author,
    }
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# 6. Runtime / telemetry
# ────────────────────────────────────────────────────────────────────


def runtime_signal(
    *,
    invocation_count: int = 0,
    error_count: int = 0,
    last_invoked_age_days: float | None = None,
) -> SignalResult:
    """Real-usage telemetry score.

    Until ctx-mcp-load lands (Phase 5+) all inputs default to 0/None and
    this signal returns 0.5 (neutral) so fresh MCPs are not penalised for
    lack of usage data.

    When data exists:
        - ``error_rate = error_count / invocation_count``
        - ``usage_term = min(1, invocation_count / 10)``
        - ``reliability_term = 1.0 - min(1, error_rate * 5)``
        - ``score = (usage_term + reliability_term) / 2``

    ``last_invoked_age_days`` is captured in evidence for future
    recency-decay extension but does not affect the score today.

    Args:
        invocation_count: Total number of times this MCP was invoked.
        error_count: Number of those invocations that produced an error.
        last_invoked_age_days: Days since the most recent invocation.
            ``None`` when never invoked or unknown.

    Returns:
        ``SignalResult`` with evidence
        ``{"invocation_count", "error_count", "error_rate",
           "neutral_no_data"}``.

    Raises:
        ValueError: If any count is negative, ``error_count >
            invocation_count``, or ``last_invoked_age_days < 0``.
    """
    if invocation_count < 0:
        raise ValueError("invocation_count must be >= 0")
    if error_count < 0:
        raise ValueError("error_count must be >= 0")
    if error_count > invocation_count:
        raise ValueError("error_count must be <= invocation_count")
    if last_invoked_age_days is not None and last_invoked_age_days < 0.0:
        raise ValueError("last_invoked_age_days must be >= 0 when present")

    neutral_no_data = invocation_count == 0

    if neutral_no_data:
        error_rate = 0.0
        score = 0.5
    else:
        error_rate = error_count / float(invocation_count)
        usage_term = min(1.0, invocation_count / float(_RUNTIME_USAGE_SATURATION))
        reliability_term = 1.0 - min(1.0, error_rate * _RUNTIME_ERROR_PENALTY_SCALE)
        score = (usage_term + reliability_term) / 2.0

    evidence: dict[str, Any] = {
        "invocation_count": invocation_count,
        "error_count": error_count,
        "error_rate": error_rate,
        "neutral_no_data": neutral_no_data,
    }
    return SignalResult(score=score, evidence=evidence)
