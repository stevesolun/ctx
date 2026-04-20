"""
test_intake_pipeline.py -- Tests for the intake_pipeline composition layer.

Coverage:

  - IntakeRejected carries the decision and stringifies failures
  - check_intake / record_embedding subject_type validation
  - Disabled-intake short-circuits (no embedder call at all)
  - Empty-corpus check_intake returns allow=True without embedder call
  - Structural failure returns allow=False without embedder call
  - Record + subsequent check sees the vector (DUPLICATE round-trip)
  - Skills and agents ranking spaces do not collide
  - Non-string / empty corpus text is a no-op for record_embedding
  - Embedder cache is reset between calls via reset_cache()
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

import numpy as np
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import intake_pipeline as pipeline  # noqa: E402
from ctx_config import cfg  # noqa: E402
from intake_gate import IntakeDecision, IntakeFinding  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Test doubles
# ────────────────────────────────────────────────────────────────────


class _KeyedFakeEmbedder:
    """Maps input text to pre-arranged unit vectors.

    Any unmapped text gets a deterministic hash-seeded unit vector so
    two distinct candidates don't collide at 1.0 by accident.
    """

    def __init__(self, dim: int = 8, name: str = "fake-embedder") -> None:
        self._dim = dim
        self._name = name
        self._vectors: dict[str, np.ndarray] = {}
        self.call_count = 0

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return self._name

    def arrange(self, text: str, vector: np.ndarray) -> None:
        v = np.asarray(vector, dtype=np.float32)
        n = np.linalg.norm(v)
        if n > 0:
            v = v / n
        self._vectors[text] = v

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        self.call_count += 1
        rows: list[np.ndarray] = []
        for t in texts:
            if t in self._vectors:
                rows.append(self._vectors[t])
                continue
            seed = abs(hash(t)) % (2**31)
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(self._dim).astype(np.float32)
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
            rows.append(v)
        return np.vstack(rows).astype(np.float32)


def _valid_md(name: str, description: str, extra_body: str = "") -> str:
    body = (
        "## Overview\n\n"
        "This is a fully-formed skill body that satisfies every structural "
        "requirement: frontmatter with name + description, an H1 title, at "
        "least one H2 section, and more than the minimum required body "
        f"length. {extra_body}".strip()
    )
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n"
        f"# {name}\n\n"
        f"{body}\n"
    )


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_intake_pipeline(monkeypatch, tmp_path):
    """Isolate every test: fresh cache root, fresh embedder, enabled gate."""
    pipeline.reset_cache()
    fake = _KeyedFakeEmbedder()
    monkeypatch.setattr(cfg, "build_intake_embedder", lambda: fake)
    monkeypatch.setattr(cfg, "intake_cache_root", tmp_path / "cache")
    monkeypatch.setattr(cfg, "intake_enabled", True)
    yield fake
    pipeline.reset_cache()


# ────────────────────────────────────────────────────────────────────
# IntakeRejected
# ────────────────────────────────────────────────────────────────────


class TestIntakeRejected:
    def test_carries_decision(self):
        finding = IntakeFinding(
            code="DUPLICATE",
            severity="fail",
            message="too close to 'other'",
        )
        decision = IntakeDecision(allow=False, findings=(finding,))
        exc = pipeline.IntakeRejected(decision)
        assert exc.decision is decision
        assert "DUPLICATE" in str(exc)
        assert "too close to 'other'" in str(exc)

    def test_without_failures_renders_generic_message(self):
        # In practice IntakeRejected is only raised when there *are*
        # failures, but the class must not crash if the caller hands
        # over an empty decision.
        decision = IntakeDecision(allow=False)
        exc = pipeline.IntakeRejected(decision)
        assert "rejected" in str(exc).lower()


# ────────────────────────────────────────────────────────────────────
# subject_type validation
# ────────────────────────────────────────────────────────────────────


class TestSubjectTypeValidation:
    def test_check_intake_rejects_unknown_subject_type(self):
        with pytest.raises(ValueError, match="subject_type"):
            pipeline.check_intake("irrelevant", "widgets")

    def test_record_embedding_rejects_unknown_subject_type(self):
        with pytest.raises(ValueError, match="subject_type"):
            pipeline.record_embedding(
                subject_id="x",
                raw_md="irrelevant",
                subject_type="widgets",
            )

    @pytest.mark.parametrize("subject_type", ["skills", "agents", "mcp-servers"])
    def test_accepted_subject_types(self, subject_type):
        decision = pipeline.check_intake(
            _valid_md("ok", "fine skill"),
            subject_type,
        )
        assert decision.allow is True


# ────────────────────────────────────────────────────────────────────
# Disabled gate short-circuits
# ────────────────────────────────────────────────────────────────────


class TestDisabledIntake:
    def test_check_intake_allows_when_disabled(self, monkeypatch, reset_intake_pipeline):
        monkeypatch.setattr(cfg, "intake_enabled", False)
        # Pass garbage — the gate should not inspect it.
        decision = pipeline.check_intake("not even markdown", "skills")
        assert decision.allow is True
        assert decision.findings == ()
        assert reset_intake_pipeline.call_count == 0

    def test_record_embedding_is_noop_when_disabled(self, monkeypatch, reset_intake_pipeline):
        monkeypatch.setattr(cfg, "intake_enabled", False)
        pipeline.record_embedding(
            subject_id="skip",
            raw_md=_valid_md("skip", "skip skill"),
            subject_type="skills",
        )
        assert reset_intake_pipeline.call_count == 0


# ────────────────────────────────────────────────────────────────────
# Structural short-circuit
# ────────────────────────────────────────────────────────────────────


class TestStructuralShortCircuit:
    def test_missing_frontmatter_fails_without_embed(self, reset_intake_pipeline):
        decision = pipeline.check_intake(
            "# just a title and some text\n",
            "skills",
        )
        assert decision.allow is False
        codes = [f.code for f in decision.failures]
        assert "FRONTMATTER_MISSING" in codes
        assert reset_intake_pipeline.call_count == 0

    def test_empty_corpus_allows_valid_candidate_without_embed(
        self, reset_intake_pipeline
    ):
        decision = pipeline.check_intake(
            _valid_md("first", "first skill ever added"),
            "skills",
        )
        assert decision.allow is True
        # Empty corpus path never loads the embedder.
        assert reset_intake_pipeline.call_count == 0


# ────────────────────────────────────────────────────────────────────
# Round-trip: record + subsequent check
# ────────────────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_recorded_candidate_is_seen_by_next_check(self, reset_intake_pipeline):
        # Arrange: force the same text to map to a fixed vector so the
        # record + check round-trip returns cosine 1.0.
        md = _valid_md("alpha", "alpha helper skill")
        from intake_gate import compose_corpus_text
        text = compose_corpus_text(md)
        vec = np.zeros(8, dtype=np.float32)
        vec[0] = 1.0
        reset_intake_pipeline.arrange(text, vec)

        pipeline.record_embedding(
            subject_id="alpha",
            raw_md=md,
            subject_type="skills",
        )

        # Same content submitted as a fresh candidate must trip the
        # duplicate threshold. We re-arrange with a slightly perturbed
        # vector to confirm the ranker is actually running (not a
        # short-circuit on identical text).
        duplicate_md = _valid_md(
            "alpha-copy",
            "alpha helper skill",
            extra_body="Extra sentence to make text distinct.",
        )
        duplicate_text = compose_corpus_text(duplicate_md)
        reset_intake_pipeline.arrange(duplicate_text, vec)  # still vector[0]

        decision = pipeline.check_intake(duplicate_md, "skills")
        assert decision.allow is False
        codes = [f.code for f in decision.failures]
        assert "DUPLICATE" in codes

    def test_distinct_content_passes(self, reset_intake_pipeline):
        from intake_gate import compose_corpus_text
        md_a = _valid_md("alpha", "alpha helper skill")
        md_b = _valid_md("bravo", "something completely unrelated")

        text_a = compose_corpus_text(md_a)
        text_b = compose_corpus_text(md_b)

        vec_a = np.zeros(8, dtype=np.float32)
        vec_a[0] = 1.0
        vec_b = np.zeros(8, dtype=np.float32)
        vec_b[7] = 1.0  # orthogonal

        reset_intake_pipeline.arrange(text_a, vec_a)
        reset_intake_pipeline.arrange(text_b, vec_b)

        pipeline.record_embedding(subject_id="alpha", raw_md=md_a, subject_type="skills")

        decision = pipeline.check_intake(md_b, "skills")
        assert decision.allow is True
        # No DUPLICATE / NEAR_DUPLICATE findings.
        assert decision.findings == ()


# ────────────────────────────────────────────────────────────────────
# Subject-type isolation
# ────────────────────────────────────────────────────────────────────


class TestSubjectTypeIsolation:
    def test_skills_and_agents_do_not_cross_pollute(self, reset_intake_pipeline):
        from intake_gate import compose_corpus_text
        md = _valid_md("shared", "shared description body")
        text = compose_corpus_text(md)
        vec = np.zeros(8, dtype=np.float32)
        vec[0] = 1.0
        reset_intake_pipeline.arrange(text, vec)

        # Record under skills.
        pipeline.record_embedding(
            subject_id="shared", raw_md=md, subject_type="skills",
        )

        # Identical candidate submitted as an agent must NOT see the
        # skill's vector — agents have their own ranking space.
        decision = pipeline.check_intake(md, "agents")
        assert decision.allow is True
        assert all(f.code != "DUPLICATE" for f in decision.findings)

        # Same candidate as a skill DOES collide (sanity check that
        # the vector is in the skill cache).
        decision_skills = pipeline.check_intake(md, "skills")
        codes = [f.code for f in decision_skills.failures]
        assert "DUPLICATE" in codes


# ────────────────────────────────────────────────────────────────────
# record_embedding edge cases
# ────────────────────────────────────────────────────────────────────


class TestRecordEmbeddingEdgeCases:
    def test_empty_corpus_text_is_noop(self, reset_intake_pipeline):
        # No frontmatter description AND no body -> compose_corpus_text
        # returns "". The recorder must not try to embed an empty text.
        pipeline.record_embedding(
            subject_id="blank",
            raw_md="---\nname: blank\n---\n\n",
            subject_type="skills",
        )
        assert reset_intake_pipeline.call_count == 0


# ────────────────────────────────────────────────────────────────────
# Embedder cache reset
# ────────────────────────────────────────────────────────────────────


class TestEmbedderCache:
    def test_reset_cache_forces_rebuild(self, monkeypatch, reset_intake_pipeline):
        first = reset_intake_pipeline  # the fake currently wired in

        # Force the first call so the cache is populated.
        pipeline.record_embedding(
            subject_id="warm",
            raw_md=_valid_md("warm", "warmup skill"),
            subject_type="skills",
        )
        assert first.call_count == 1

        # Swap in a different fake and reset. The next call must use
        # the new fake, not the cached one.
        second = _KeyedFakeEmbedder(name="fake-embedder-v2")
        monkeypatch.setattr(cfg, "build_intake_embedder", lambda: second)
        pipeline.reset_cache()

        pipeline.record_embedding(
            subject_id="other",
            raw_md=_valid_md("other", "another skill"),
            subject_type="skills",
        )
        assert second.call_count == 1
        # First fake received no additional calls.
        assert first.call_count == 1
