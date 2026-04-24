#!/usr/bin/env python3
"""
quality_signals.py -- Four deterministic quality-signal extractors.

Signals (all normalized to [0, 1]):

  1. telemetry   — did this skill get loaded? How recently? How often?
  2. intake      — does the current file still pass the structural gate
                   from ``intake_gate``? Missing H1/H2/description lose points.
  3. graph       — how connected is this node in the wiki graph?
                   Isolated nodes score 0; well-linked nodes score ~1.
  4. routing     — when the router considered this skill, did it pick it?
                   Neutral 0.5 when no trace exists yet.

Each extractor is a pure function: takes already-loaded inputs, returns a
``SignalResult``. All I/O is the caller's job; this module is
deterministic and trivially unit-testable.

The scorer in ``skill_quality.py`` composes a weighted sum of these four
results, applies hard floors, and maps to an A/B/C/D grade.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping


# ────────────────────────────────────────────────────────────────────
# Result type
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SignalResult:
    """One signal's score plus the evidence that produced it.

    ``score`` is always clamped to ``[0.0, 1.0]`` in ``__post_init__``;
    extractors may compute out-of-range intermediates freely.
    ``evidence`` carries structured facts that the ``explain`` CLI shows
    to a human auditing why a skill scored what it did.
    """

    score: float
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if math.isnan(self.score) or math.isinf(self.score):
            raise ValueError(f"signal score must be finite, got {self.score!r}")
        if not 0.0 <= self.score <= 1.0:
            # Clamp silently — extractors pass arbitrary intermediates.
            object.__setattr__(self, "score", max(0.0, min(1.0, self.score)))


# ────────────────────────────────────────────────────────────────────
# Telemetry signal
# ────────────────────────────────────────────────────────────────────


# Knobs live at module scope so tests can reference the same constants
# the scorer sees. Weights sum to 1.0 inside this signal.
_TELEMETRY_EVER_LOADED_WEIGHT = 0.35
_TELEMETRY_RECENT_WEIGHT = 0.35
_TELEMETRY_RECENCY_WEIGHT = 0.30

# Scaling: a single recent load earns a small slice; saturates at this
# count so a handful of loads is enough for full recency credit.
_TELEMETRY_RECENT_SATURATION = 3


def telemetry_signal(
    *,
    load_count: int,
    recent_load_count: int,
    last_load_age_days: float | None,
    stale_threshold_days: float,
) -> SignalResult:
    """Score how alive this skill is in the user's recent history.

    Args:
      load_count: Lifetime count of ``load`` events for this skill.
      recent_load_count: Loads within the recent window (caller decides
        the window — typically 14 or 30 days).
      last_load_age_days: How long ago the most recent load happened.
        ``None`` means the skill has never been loaded.
      stale_threshold_days: Loads older than this contribute zero to the
        recency term. Typically ~14–30 days.

    Returns:
      ``SignalResult`` whose evidence is
      ``{"load_count", "recent_load_count", "last_load_age_days",
         "never_loaded"}``.
    """
    if load_count < 0:
        raise ValueError("load_count must be >= 0")
    if recent_load_count < 0:
        raise ValueError("recent_load_count must be >= 0")
    if stale_threshold_days <= 0:
        raise ValueError("stale_threshold_days must be > 0")

    never_loaded = load_count == 0

    # Term 1: ever loaded (binary)
    ever_loaded_term = 1.0 if not never_loaded else 0.0

    # Term 2: recent volume, saturating at _TELEMETRY_RECENT_SATURATION
    recent_term = min(
        1.0, recent_load_count / float(_TELEMETRY_RECENT_SATURATION)
    )

    # Term 3: recency. Linear decay from 1.0 (just loaded) to 0.0 at
    # stale_threshold_days. Never-loaded short-circuits to 0.
    if last_load_age_days is None:
        recency_term = 0.0
    else:
        if last_load_age_days < 0:
            raise ValueError("last_load_age_days must be >= 0 when present")
        recency_term = max(
            0.0, 1.0 - (last_load_age_days / stale_threshold_days)
        )

    score = (
        _TELEMETRY_EVER_LOADED_WEIGHT * ever_loaded_term
        + _TELEMETRY_RECENT_WEIGHT * recent_term
        + _TELEMETRY_RECENCY_WEIGHT * recency_term
    )

    evidence: dict[str, Any] = {
        "load_count": load_count,
        "recent_load_count": recent_load_count,
        "last_load_age_days": last_load_age_days,
        "never_loaded": never_loaded,
    }
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# Intake signal (live structural re-check)
# ────────────────────────────────────────────────────────────────────


# Six structural checks; each failure subtracts 1/6 from a starting 1.0.
# Matches the fields enforced by ``intake_gate._check_structure`` at
# install time, so scoring stays consistent with the gate.
_INTAKE_CHECKS: tuple[str, ...] = (
    "has_frontmatter",
    "has_name",
    "has_description",
    "has_h1",
    "has_h2",
    "body_long_enough",
)

_H1_RE = re.compile(r"^\#\s+\S", re.MULTILINE)
_H2_RE = re.compile(r"^\#\#\s+\S", re.MULTILINE)


def intake_signal(
    raw_md: str,
    *,
    frontmatter: Mapping[str, Any],
    has_frontmatter_block: bool,
    body: str,
    min_body_chars: int,
) -> SignalResult:
    """Re-run the structural intake checks against the current file.

    Deliberately mirrors ``intake_gate._check_structure`` so a skill that
    has silently rotted (e.g. someone stripped the frontmatter) gets
    demoted here without needing a separate persistence layer.

    Args:
      raw_md: The original markdown (unused today but kept for future
        checks; do not remove).
      frontmatter: Parsed frontmatter dict (empty mapping if absent).
      has_frontmatter_block: True iff the file opened with ``---``.
      body: Body text (everything after the frontmatter block).
      min_body_chars: Minimum body length for ``body_long_enough``.

    Returns:
      ``SignalResult`` whose evidence tracks per-check pass/fail plus a
      ``hard_fail`` flag — True when any check that would hard-fail
      install-time intake is currently failing.
    """
    del raw_md  # reserved for future checks; silences unused-arg linters

    if min_body_chars < 0:
        raise ValueError("min_body_chars must be >= 0")

    name = frontmatter.get("name") if isinstance(frontmatter, Mapping) else None
    desc = frontmatter.get("description") if isinstance(frontmatter, Mapping) else None

    results: dict[str, bool] = {
        "has_frontmatter": bool(has_frontmatter_block and frontmatter),
        "has_name": isinstance(name, str) and bool(name.strip()),
        "has_description": isinstance(desc, str) and bool(desc.strip()),
        "has_h1": bool(_H1_RE.search(body)),
        "has_h2": bool(_H2_RE.search(body)),
        "body_long_enough": len(body.strip()) >= min_body_chars,
    }

    passed = sum(1 for k in _INTAKE_CHECKS if results.get(k))
    score = passed / float(len(_INTAKE_CHECKS))

    # hard_fail mirrors intake_gate: any single structural failure is a
    # blocking condition at install time. If present now, the scorer
    # applies an F-grade hard floor downstream.
    hard_fail = not all(results.get(k) for k in _INTAKE_CHECKS)

    evidence: dict[str, Any] = {
        "checks": dict(results),
        "passed": passed,
        "total": len(_INTAKE_CHECKS),
        "hard_fail": hard_fail,
    }
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# Graph connectivity signal
# ────────────────────────────────────────────────────────────────────


# Log-scale: degree=0 → 0.0, degree=1 → ~0.30, degree=_GRAPH_SATURATION → 1.0.
# Picks up the long tail of well-linked skills while keeping isolated
# nodes firmly at zero (no connectivity credit for orphans).
_GRAPH_SATURATION = 20


def graph_signal(
    *,
    degree: int,
    avg_edge_weight: float = 1.0,
) -> SignalResult:
    """Score wiki-graph connectivity for a node.

    Args:
      degree: Number of edges incident to the node.
      avg_edge_weight: Mean ``weight`` across those edges (shared-tag
        count in ``wiki_graphify``). Used as a small bonus multiplier
        so a node with a few strong edges scores close to a node with
        many weak ones.

    Returns:
      ``SignalResult`` with evidence
      ``{"degree", "avg_edge_weight", "is_isolated"}``.
    """
    if degree < 0:
        raise ValueError("degree must be >= 0")
    if avg_edge_weight < 0:
        raise ValueError("avg_edge_weight must be >= 0")

    if degree == 0:
        score = 0.0
    else:
        # log1p(degree) / log1p(saturation) — smooth, monotonic, saturates.
        base = math.log1p(degree) / math.log1p(_GRAPH_SATURATION)
        # Weight bonus: +0 at weight=1, up to +0.10 at weight>=5.
        bonus = min(0.10, max(0.0, (avg_edge_weight - 1.0) * 0.025))
        score = min(1.0, base + bonus)

    evidence: dict[str, Any] = {
        "degree": degree,
        "avg_edge_weight": avg_edge_weight,
        "is_isolated": degree == 0,
    }
    return SignalResult(score=score, evidence=evidence)


# ────────────────────────────────────────────────────────────────────
# Routing hit-rate signal
# ────────────────────────────────────────────────────────────────────


# Below this many observations the hit rate is noise, so we hold the
# score at the neutral prior instead of swinging on one sample.
_ROUTING_MIN_OBSERVATIONS = 3
_ROUTING_NEUTRAL_SCORE = 0.5


def routing_signal(
    *,
    considered: int,
    picked: int,
) -> SignalResult:
    """Score the router hit rate for this skill.

    Args:
      considered: Count of routing decisions in which this skill was a
        candidate.
      picked: Subset of ``considered`` where the router actually picked
        it.

    Returns:
      ``SignalResult`` with evidence
      ``{"considered", "picked", "hit_rate", "no_trace"}``. When
      ``considered < _ROUTING_MIN_OBSERVATIONS`` the score is held at
      neutral 0.5 and ``no_trace`` is True — we don't yet have enough
      data to judge.
    """
    if considered < 0:
        raise ValueError("considered must be >= 0")
    if picked < 0:
        raise ValueError("picked must be >= 0")
    if picked > considered:
        raise ValueError("picked must be <= considered")

    hit_rate = (picked / considered) if considered > 0 else 0.0
    no_trace = considered < _ROUTING_MIN_OBSERVATIONS

    if no_trace:
        score = _ROUTING_NEUTRAL_SCORE
    else:
        score = hit_rate

    evidence: dict[str, Any] = {
        "considered": considered,
        "picked": picked,
        "hit_rate": hit_rate,
        "no_trace": no_trace,
    }
    return SignalResult(score=score, evidence=evidence)


__all__ = [
    "SignalResult",
    "telemetry_signal",
    "intake_signal",
    "graph_signal",
    "routing_signal",
]
