"""
test_intake_gate.py -- Tests for the Phase 2 intake gate.

Coverage:

  - IntakeConfig validation (thresholds ordering, ranges)
  - compose_corpus_text strategy (description + body, cap)
  - Structural hard-fail checks (frontmatter, H1, H2, length)
  - Similarity checks (DUPLICATE at >=0.93, NEAR_DUPLICATE at >=0.85)
  - Connectivity check (disabled, small corpus, not-enough, enough)
  - Short-circuit: structural failure bypasses embedding
  - Empty-corpus path: allow=True, no network/model work
  - Dim mismatch raises
  - IntakeDecision.failures / warnings split correctly
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import cosine_ranker as cr  # noqa: E402
import intake_gate as ig  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Test doubles
# ────────────────────────────────────────────────────────────────────


@dataclass
class _FakeEmbedder:
    """Returns pre-arranged vectors. One vector per ``embed`` call.

    The test controls similarity by choosing the vector and the
    corpus the ranker was built from.
    """

    vector: np.ndarray
    _dim: int = 4

    @property
    def dim(self) -> int:
        return int(self.vector.shape[0])

    @property
    def name(self) -> str:
        return "fake-embedder"

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        # Ignore ``texts``; the test has already picked the vector it
        # wants the gate to see.
        return np.asarray([self.vector], dtype=np.float32)


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)


def _make_ranker(vectors: dict[str, np.ndarray]) -> cr.CosineRanker:
    return cr.CosineRanker.from_vectors(vectors)


# ────────────────────────────────────────────────────────────────────
# Sample candidate markdown
# ────────────────────────────────────────────────────────────────────


VALID_MD = """---
name: test-skill
description: A well-formed test skill used by the intake gate tests.
---
# Test Skill

## Overview

This skill exists only to exercise the intake gate in unit tests.
It has a description, an H1, at least one H2, and enough body text
to satisfy the minimum body length threshold.
"""


def _valid_md(body_extra: str = "") -> str:
    return VALID_MD + body_extra


# ────────────────────────────────────────────────────────────────────
# IntakeConfig
# ────────────────────────────────────────────────────────────────────


def test_config_defaults_match_plan() -> None:
    c = ig.IntakeConfig()
    assert c.dup_threshold == pytest.approx(0.93)
    assert c.near_dup_threshold == pytest.approx(0.85)
    assert c.min_neighbors == 0


def test_config_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError, match="thresholds must satisfy"):
        ig.IntakeConfig(dup_threshold=0.80, near_dup_threshold=0.90)


def test_config_rejects_threshold_above_one() -> None:
    with pytest.raises(ValueError, match="thresholds must satisfy"):
        ig.IntakeConfig(dup_threshold=1.5)


def test_config_rejects_negative_min_neighbors() -> None:
    with pytest.raises(ValueError, match="min_neighbors"):
        ig.IntakeConfig(min_neighbors=-1)


def test_config_rejects_neighbor_score_out_of_range() -> None:
    with pytest.raises(ValueError, match="min_neighbor_score"):
        ig.IntakeConfig(min_neighbor_score=1.1)


def test_config_rejects_negative_min_body_chars() -> None:
    with pytest.raises(ValueError, match="min_body_chars"):
        ig.IntakeConfig(min_body_chars=-1)


# ────────────────────────────────────────────────────────────────────
# compose_corpus_text
# ────────────────────────────────────────────────────────────────────


def test_compose_corpus_text_combines_description_and_body() -> None:
    text = ig.compose_corpus_text(VALID_MD)
    assert "well-formed test skill" in text  # description
    assert "Test Skill" in text  # body H1
    assert "Overview" in text  # body H2


def test_compose_corpus_text_no_frontmatter_returns_body() -> None:
    raw = "# Plain Title\n\n## Section\n\nBody text."
    text = ig.compose_corpus_text(raw)
    assert "Plain Title" in text
    assert "Body text" in text


def test_compose_corpus_text_caps_length() -> None:
    huge = VALID_MD + ("x" * 20_000)
    text = ig.compose_corpus_text(huge)
    assert len(text) <= 8000


def test_compose_corpus_text_skips_empty_description() -> None:
    raw = "---\nname: s\ndescription: \n---\n# T\n\n## S\n\nBody."
    text = ig.compose_corpus_text(raw)
    # No stray leading newline or placeholder for the empty description
    assert not text.startswith("\n")


# ────────────────────────────────────────────────────────────────────
# Structural checks — standalone (empty corpus path)
# ────────────────────────────────────────────────────────────────────


def _empty_gate() -> tuple[_FakeEmbedder, cr.CosineRanker]:
    return _FakeEmbedder(vector=np.zeros(4, dtype=np.float32)), _make_ranker({})


def test_valid_candidate_against_empty_corpus_allowed() -> None:
    emb, ranker = _empty_gate()
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker)
    assert decision.allow is True
    assert decision.findings == ()
    assert decision.nearest == ()


def test_missing_frontmatter_fails() -> None:
    emb, ranker = _empty_gate()
    raw = "# Title\n\n## Section\n\n" + ("body content " * 20)
    decision = ig.run_intake_gate(raw, embedder=emb, ranker=ranker)
    assert decision.allow is False
    codes = [f.code for f in decision.failures]
    assert "FRONTMATTER_MISSING" in codes


def test_empty_frontmatter_block_fails() -> None:
    emb, ranker = _empty_gate()
    raw = "---\n---\n# Title\n\n## Section\n\n" + ("body content " * 20)
    decision = ig.run_intake_gate(raw, embedder=emb, ranker=ranker)
    assert decision.allow is False
    assert any(f.code == "FRONTMATTER_MISSING" for f in decision.failures)


def test_missing_name_field_fails() -> None:
    emb, ranker = _empty_gate()
    raw = (
        "---\ndescription: something\n---\n# T\n\n## S\n\n"
        + ("body content " * 20)
    )
    decision = ig.run_intake_gate(raw, embedder=emb, ranker=ranker)
    assert decision.allow is False
    assert any(
        f.code == "FRONTMATTER_FIELD_MISSING_NAME" for f in decision.failures
    )


def test_missing_description_field_fails() -> None:
    emb, ranker = _empty_gate()
    raw = "---\nname: x\n---\n# T\n\n## S\n\n" + ("body content " * 20)
    decision = ig.run_intake_gate(raw, embedder=emb, ranker=ranker)
    assert decision.allow is False
    assert any(
        f.code == "FRONTMATTER_FIELD_MISSING_DESCRIPTION"
        for f in decision.failures
    )


def test_missing_h1_fails() -> None:
    emb, ranker = _empty_gate()
    raw = (
        "---\nname: x\ndescription: y\n---\n## Section\n\n"
        + ("body content " * 20)
    )
    decision = ig.run_intake_gate(raw, embedder=emb, ranker=ranker)
    assert decision.allow is False
    assert any(f.code == "BODY_MISSING_H1" for f in decision.failures)


def test_missing_h2_fails() -> None:
    emb, ranker = _empty_gate()
    raw = (
        "---\nname: x\ndescription: y\n---\n# Title\n\n"
        + ("body content " * 20)
    )
    decision = ig.run_intake_gate(raw, embedder=emb, ranker=ranker)
    assert decision.allow is False
    assert any(f.code == "BODY_MISSING_H2" for f in decision.failures)


def test_body_too_short_fails() -> None:
    emb, ranker = _empty_gate()
    raw = "---\nname: x\ndescription: y\n---\n# T\n\n## S\n\ntiny body."
    decision = ig.run_intake_gate(raw, embedder=emb, ranker=ranker)
    assert decision.allow is False
    assert any(f.code == "BODY_TOO_SHORT" for f in decision.failures)


def test_structural_failure_short_circuits_embedding() -> None:
    # The embedder must not be called when structure fails — use a
    # wrapper that tracks call count.
    class _CountingEmbedder:
        calls = 0

        @property
        def dim(self) -> int:
            return 4

        @property
        def name(self) -> str:
            return "counting"

        def embed(self, texts: Sequence[str]) -> np.ndarray:
            _CountingEmbedder.calls += 1
            return np.zeros((1, 4), dtype=np.float32)

    ranker = _make_ranker({
        "existing": _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
    })
    raw = "no frontmatter here\n\njust body"
    decision = ig.run_intake_gate(
        raw, embedder=_CountingEmbedder(), ranker=ranker
    )
    assert decision.allow is False
    assert _CountingEmbedder.calls == 0


# ────────────────────────────────────────────────────────────────────
# Similarity checks
# ────────────────────────────────────────────────────────────────────


def _corpus_with_exact_match() -> cr.CosineRanker:
    return _make_ranker({
        "existing-skill": _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
        "other-skill": _unit(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)),
    })


def test_duplicate_fails_at_or_above_threshold() -> None:
    ranker = _corpus_with_exact_match()
    # Query identical to existing-skill → score = 1.0 → DUPLICATE
    emb = _FakeEmbedder(vector=_unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)))
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker)
    assert decision.allow is False
    codes = [f.code for f in decision.failures]
    assert "DUPLICATE" in codes
    assert decision.nearest[0].subject_id == "existing-skill"


def test_near_duplicate_warns_but_allows() -> None:
    ranker = _corpus_with_exact_match()
    # Pick a vector whose dot product with [1,0,0,0] is ~0.88 —
    # between the near-dup and dup thresholds.
    q = np.array([0.88, np.sqrt(1 - 0.88**2), 0.0, 0.0], dtype=np.float32)
    emb = _FakeEmbedder(vector=q)
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker)
    assert decision.allow is True
    codes = [f.code for f in decision.warnings]
    assert "NEAR_DUPLICATE" in codes
    assert not decision.failures


def test_distant_candidate_has_no_similarity_finding() -> None:
    ranker = _corpus_with_exact_match()
    # Orthogonal to both corpus vectors → score = 0
    q = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    emb = _FakeEmbedder(vector=q)
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker)
    assert decision.allow is True
    codes = [f.code for f in decision.findings]
    assert "DUPLICATE" not in codes
    assert "NEAR_DUPLICATE" not in codes


def test_similarity_threshold_exactly_at_boundary() -> None:
    # Score == dup_threshold must still fail (>=, not >).
    ranker = _corpus_with_exact_match()
    q = np.array([0.93, np.sqrt(1 - 0.93**2), 0.0, 0.0], dtype=np.float32)
    emb = _FakeEmbedder(vector=q)
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker)
    assert decision.allow is False


# ────────────────────────────────────────────────────────────────────
# Connectivity check
# ────────────────────────────────────────────────────────────────────


def test_connectivity_disabled_by_default() -> None:
    ranker = _corpus_with_exact_match()
    # Orthogonal query — no neighbor has any similarity
    q = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    emb = _FakeEmbedder(vector=q)
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker)
    assert decision.allow is True
    assert not any(f.code == "LOW_CONNECTIVITY" for f in decision.findings)


def test_connectivity_enabled_flags_orphan() -> None:
    ranker = _corpus_with_exact_match()
    q = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)  # score 0 everywhere
    emb = _FakeEmbedder(vector=q)
    cfg = ig.IntakeConfig(min_neighbors=1, min_neighbor_score=0.30)
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker, config=cfg)
    assert decision.allow is True  # warn, not fail
    codes = [f.code for f in decision.warnings]
    assert "LOW_CONNECTIVITY" in codes


def test_connectivity_passes_when_neighbor_qualified() -> None:
    ranker = _corpus_with_exact_match()
    # 0.5 similarity to existing-skill — above 0.30 floor
    q = np.array([0.5, np.sqrt(1 - 0.5**2), 0.0, 0.0], dtype=np.float32)
    emb = _FakeEmbedder(vector=q)
    cfg = ig.IntakeConfig(min_neighbors=1, min_neighbor_score=0.30)
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker, config=cfg)
    assert decision.allow is True
    assert not any(f.code == "LOW_CONNECTIVITY" for f in decision.findings)


def test_connectivity_skipped_for_small_corpus() -> None:
    # Corpus size = 1 but min_neighbors = 2 → skip silently, don't
    # punish the second subject to ever land in the system.
    ranker = _make_ranker({
        "existing": _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
    })
    q = np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)
    emb = _FakeEmbedder(vector=q)
    cfg = ig.IntakeConfig(min_neighbors=2, min_neighbor_score=0.30)
    decision = ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker, config=cfg)
    assert decision.allow is True
    assert not any(f.code == "LOW_CONNECTIVITY" for f in decision.findings)


# ────────────────────────────────────────────────────────────────────
# Dim mismatch
# ────────────────────────────────────────────────────────────────────


def test_embedder_dim_mismatch_raises() -> None:
    ranker = _make_ranker({
        "existing": _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
    })
    # Embedder yields a 3-D vector, corpus is 4-D
    emb = _FakeEmbedder(vector=_unit(np.array([1.0, 0.0, 0.0], dtype=np.float32)))
    with pytest.raises(ValueError, match="does not match corpus dim"):
        ig.run_intake_gate(VALID_MD, embedder=emb, ranker=ranker)


# ────────────────────────────────────────────────────────────────────
# IntakeDecision slicing
# ────────────────────────────────────────────────────────────────────


def test_decision_splits_failures_from_warnings() -> None:
    d = ig.IntakeDecision(
        allow=False,
        findings=(
            ig.IntakeFinding(code="A", severity="fail", message="x"),
            ig.IntakeFinding(code="B", severity="warn", message="y"),
            ig.IntakeFinding(code="C", severity="fail", message="z"),
        ),
    )
    assert [f.code for f in d.failures] == ["A", "C"]
    assert [f.code for f in d.warnings] == ["B"]


def test_decision_defaults_are_empty_tuples() -> None:
    d = ig.IntakeDecision(allow=True)
    assert d.findings == ()
    assert d.nearest == ()
    assert d.failures == ()
    assert d.warnings == ()
