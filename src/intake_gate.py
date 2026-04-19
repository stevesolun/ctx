#!/usr/bin/env python3
"""
intake_gate.py -- Quality gate for new skills and agents.

Invoked by ``skill_add`` / ``agent_add`` before a candidate is written
into the wiki. Runs three families of checks:

  1. Structural hard-fail checks (M2.6)
     - Parseable frontmatter with ``name`` + ``description``
     - Body has an H1 title
     - Body has at least one H2 section
     - Body meets a minimum length

  2. Similarity checks (M2.4)
     - Embed the candidate once, rank against the pre-built corpus
     - ``duplicate``: top score >= ``dup_threshold`` (default 0.93)
     - ``near-duplicate``: top score in
       ``[near_dup_threshold, dup_threshold)`` (default 0.80)

  3. Connectivity check (M2.5)
     - When enabled, require ``min_neighbors`` rows scoring at least
       ``min_neighbor_score``. Prevents dropping an orphaned subject
       into the wiki with no semantic anchor.

The three families share one embedding call — the candidate is embedded
once and ranked top-K, where K is large enough to cover both the
similarity ceiling and the connectivity floor.

``IntakeDecision`` (M2.7) aggregates findings. ``allow`` is False iff at
least one finding has severity ``"fail"``. Warnings surface to the
caller but do not block intake; the caller may present them to the
user who can override explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from cosine_ranker import CosineRanker, RankedMatch
from embedding_backend import Embedder
from wiki_utils import parse_frontmatter_and_body


Severity = Literal["warn", "fail"]

# Default thresholds. Rationale (tuned M2.10 against the 70-pair fixture
# corpus — see scripts/tune_similarity_thresholds.py for the sweep):
#   dup       0.93  two subjects that are this close on MiniLM embeddings
#                   overlap heavily in topic AND phrasing — one shadows
#                   the other at routing time, so we refuse the later
#                   one and ask the caller to merge.
#   near_dup  0.80  sits centrally in the empirical gap between
#                   adversarial pairs (max 0.76) and genuine near-dupes
#                   (min 0.82). Delivers P=1.00 / R=1.00 on the corpus.
_DEFAULT_DUP_THRESHOLD = 0.93
_DEFAULT_NEAR_DUP_THRESHOLD = 0.80

# Connectivity: off by default (min_neighbors=0). When enabled, we want
# the candidate to land in a non-empty neighborhood — a wikilink target
# survives even if the nearest match is only moderately similar.
_DEFAULT_MIN_NEIGHBORS = 0
_DEFAULT_MIN_NEIGHBOR_SCORE = 0.30

# Body length floor. Well under the real minimum for a useful skill
# (real skills are >100 lines) but enough to catch empty-stub entries.
_DEFAULT_MIN_BODY_CHARS = 120

# Cap how much of the candidate text is embedded. MiniLM truncates at
# 512 tokens anyway; this bounds the CPU cost on pathological inputs.
_MAX_EMBED_CHARS = 8000

# K for the ranker call. Big enough that the connectivity check has
# room to look beyond the nearest neighbor without a second round-trip.
_RANK_K = 10

_H1_RE = re.compile(r"^\#\s+\S", re.MULTILINE)
_H2_RE = re.compile(r"^\#\#\s+\S", re.MULTILINE)


# ────────────────────────────────────────────────────────────────────
# Config and decision types
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntakeConfig:
    """Thresholds for the intake gate.

    Immutable by design — callers that want different thresholds pass
    a fresh instance instead of mutating a shared one. Defaults are
    tuned against the M2.10 fixture corpus (dup=0.93, near-dup=0.80).
    """

    dup_threshold: float = _DEFAULT_DUP_THRESHOLD
    near_dup_threshold: float = _DEFAULT_NEAR_DUP_THRESHOLD
    min_neighbors: int = _DEFAULT_MIN_NEIGHBORS
    min_neighbor_score: float = _DEFAULT_MIN_NEIGHBOR_SCORE
    min_body_chars: int = _DEFAULT_MIN_BODY_CHARS

    def __post_init__(self) -> None:
        if not 0.0 <= self.near_dup_threshold <= self.dup_threshold <= 1.0:
            raise ValueError(
                "thresholds must satisfy "
                "0 <= near_dup_threshold <= dup_threshold <= 1; "
                f"got near_dup={self.near_dup_threshold}, dup={self.dup_threshold}"
            )
        if self.min_neighbors < 0:
            raise ValueError("min_neighbors must be >= 0")
        if not 0.0 <= self.min_neighbor_score <= 1.0:
            raise ValueError("min_neighbor_score must be in [0, 1]")
        if self.min_body_chars < 0:
            raise ValueError("min_body_chars must be >= 0")


@dataclass(frozen=True)
class IntakeFinding:
    """One check outcome. Only warn/fail findings are emitted; passes
    are implicit."""

    code: str
    severity: Severity
    message: str


@dataclass(frozen=True)
class IntakeDecision:
    """Aggregate result of the intake gate."""

    allow: bool
    findings: tuple[IntakeFinding, ...] = field(default_factory=tuple)
    nearest: tuple[RankedMatch, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> tuple[IntakeFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "fail")

    @property
    def warnings(self) -> tuple[IntakeFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warn")


# ────────────────────────────────────────────────────────────────────
# Text normalisation
# ────────────────────────────────────────────────────────────────────


def compose_corpus_text(raw_md: str) -> str:
    """Canonical text used for both corpus build and candidate embed.

    The intake gate compares candidates against a corpus that MUST have
    been embedded with the same text strategy — otherwise cosine scores
    are meaningless. Exporting this helper lets the corpus builder and
    the gate share one source of truth.

    Strategy: ``description`` (if present) + body, joined by a newline.
    Frontmatter keys other than description are ignored — they're
    metadata and noise, not content.
    """
    fm, body = parse_frontmatter_and_body(raw_md)
    desc = fm.get("description", "")
    parts: list[str] = []
    if isinstance(desc, str) and desc.strip():
        parts.append(desc.strip())
    if body.strip():
        parts.append(body.strip())
    text = "\n".join(parts)
    return text[:_MAX_EMBED_CHARS]


# ────────────────────────────────────────────────────────────────────
# Individual check families
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Parsed:
    """Output of candidate parsing — reused across checks."""

    frontmatter: dict[str, object]
    body: str
    has_frontmatter: bool


def _parse_candidate(raw_md: str) -> _Parsed:
    # parse_frontmatter_and_body returns ({}, text) when no block is
    # found; detect that case explicitly so we can emit a clean error.
    fm, body = parse_frontmatter_and_body(raw_md)
    has_fm = raw_md.lstrip().startswith("---")
    return _Parsed(frontmatter=fm, body=body, has_frontmatter=has_fm)


def _check_structure(
    parsed: _Parsed, config: IntakeConfig
) -> list[IntakeFinding]:
    out: list[IntakeFinding] = []
    if not parsed.has_frontmatter or not parsed.frontmatter:
        out.append(IntakeFinding(
            code="FRONTMATTER_MISSING",
            severity="fail",
            message="candidate has no parseable YAML frontmatter block",
        ))
        # Without a frontmatter we cannot check field presence, so
        # return early with the single blocking finding.
        return out

    name = parsed.frontmatter.get("name")
    if not isinstance(name, str) or not name.strip():
        out.append(IntakeFinding(
            code="FRONTMATTER_FIELD_MISSING_NAME",
            severity="fail",
            message="frontmatter missing required field 'name'",
        ))

    desc = parsed.frontmatter.get("description")
    if not isinstance(desc, str) or not desc.strip():
        out.append(IntakeFinding(
            code="FRONTMATTER_FIELD_MISSING_DESCRIPTION",
            severity="fail",
            message="frontmatter missing required field 'description'",
        ))

    body = parsed.body
    if not _H1_RE.search(body):
        out.append(IntakeFinding(
            code="BODY_MISSING_H1",
            severity="fail",
            message="body has no H1 heading (`# Title`)",
        ))
    if not _H2_RE.search(body):
        out.append(IntakeFinding(
            code="BODY_MISSING_H2",
            severity="fail",
            message="body has no H2 section (`## Section`)",
        ))
    if len(body.strip()) < config.min_body_chars:
        out.append(IntakeFinding(
            code="BODY_TOO_SHORT",
            severity="fail",
            message=(
                f"body has {len(body.strip())} chars; "
                f"minimum is {config.min_body_chars}"
            ),
        ))
    return out


def _check_similarity(
    top: list[RankedMatch], config: IntakeConfig
) -> list[IntakeFinding]:
    if not top:
        return []
    best = top[0]
    if best.score >= config.dup_threshold:
        return [IntakeFinding(
            code="DUPLICATE",
            severity="fail",
            message=(
                f"near-identical match: {best.subject_id!r} "
                f"scores {best.score:.3f} "
                f"(>= dup threshold {config.dup_threshold:.2f})"
            ),
        )]
    if best.score >= config.near_dup_threshold:
        return [IntakeFinding(
            code="NEAR_DUPLICATE",
            severity="warn",
            message=(
                f"similar subject exists: {best.subject_id!r} "
                f"scores {best.score:.3f} "
                f"(>= near-dup threshold {config.near_dup_threshold:.2f})"
            ),
        )]
    return []


def _check_connectivity(
    top: list[RankedMatch], config: IntakeConfig, corpus_size: int
) -> list[IntakeFinding]:
    if config.min_neighbors <= 0:
        return []
    # A tiny corpus cannot fulfil the connectivity requirement by
    # definition — don't punish early adopters. Skip silently.
    if corpus_size < config.min_neighbors:
        return []
    qualified = sum(
        1 for m in top[: config.min_neighbors]
        if m.score >= config.min_neighbor_score
    )
    if qualified < config.min_neighbors:
        return [IntakeFinding(
            code="LOW_CONNECTIVITY",
            severity="warn",
            message=(
                f"only {qualified} of required {config.min_neighbors} "
                f"neighbors scored >= {config.min_neighbor_score:.2f}; "
                "candidate may land as an orphan in the wiki graph"
            ),
        )]
    return []


# ────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────


def run_intake_gate(
    raw_md: str,
    *,
    embedder: Embedder,
    ranker: CosineRanker,
    config: IntakeConfig | None = None,
) -> IntakeDecision:
    """Run all three check families against a single candidate.

    Structural failures short-circuit similarity and connectivity:
    without valid frontmatter we don't know what we're embedding, and
    a broken candidate should be fixed before anyone compares it.
    """
    cfg = config or IntakeConfig()
    parsed = _parse_candidate(raw_md)

    structure_findings = _check_structure(parsed, cfg)
    if any(f.severity == "fail" for f in structure_findings):
        return IntakeDecision(
            allow=False,
            findings=tuple(structure_findings),
            nearest=(),
        )

    if ranker.size == 0:
        # First subject in a fresh corpus. Structure passes, nothing to
        # compare against, nothing to anchor to.
        return IntakeDecision(
            allow=True,
            findings=tuple(structure_findings),
            nearest=(),
        )

    text = compose_corpus_text(raw_md)
    vec = embedder.embed([text])
    if vec.shape[0] != 1 or vec.ndim != 2:
        raise RuntimeError(
            f"embedder returned unexpected shape {vec.shape}; expected (1, dim)"
        )
    query = np.ascontiguousarray(vec[0], dtype=np.float32)
    if query.shape[0] != ranker.dim:
        raise ValueError(
            f"embedder dim {query.shape[0]} does not match corpus dim {ranker.dim}; "
            "rebuild the corpus with the same embedder"
        )

    top = ranker.topk(query, k=min(_RANK_K, ranker.size))

    findings = list(structure_findings)
    findings.extend(_check_similarity(top, cfg))
    findings.extend(_check_connectivity(top, cfg, ranker.size))

    allow = not any(f.severity == "fail" for f in findings)
    return IntakeDecision(
        allow=allow,
        findings=tuple(findings),
        nearest=tuple(top),
    )


__all__ = [
    "IntakeConfig",
    "IntakeFinding",
    "IntakeDecision",
    "Severity",
    "compose_corpus_text",
    "run_intake_gate",
]
