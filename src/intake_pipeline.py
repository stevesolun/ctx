#!/usr/bin/env python3
"""
intake_pipeline.py -- Compose the intake gate with cache + ranker lifecycle.

``skill_add`` and ``agent_add`` both want the same operation:

    1. Embed a candidate once.
    2. Compare it against the existing corpus via :mod:`cosine_ranker`.
    3. Run structural + similarity + connectivity checks via
       :mod:`intake_gate`.
    4. On acceptance, write the new vector into the corpus cache so the
       next candidate can rank against it.

Centralising that here keeps the two CLIs free of embedding knowledge and
ensures both paths use identical text normalisation, thresholds, and
cache keys.

Subject-type namespacing
------------------------
Skills and agents share the embedding model but live in separate ranking
spaces — a new agent should not collide with a skill of the same name
just because their bodies are similar. The cache directory key is
``"{subject_type}:{embedder.name}"``. Subject type is placed first so
the discriminator survives the 64-char slug cap applied by
:class:`corpus_cache.CorpusCache`.

Testability
-----------
``_cached_embedder`` is process-local and reset via :func:`reset_cache`.
Tests patch ``cfg.build_intake_embedder`` to inject a fake that does not
require sentence-transformers. Production code never touches the reset.
"""

from __future__ import annotations

from pathlib import Path

from corpus_cache import CorpusCache
from cosine_ranker import CosineRanker
from ctx_config import cfg
from embedding_backend import Embedder
from intake_gate import IntakeDecision, compose_corpus_text, run_intake_gate


# Only these subject types have a dedicated ranking space. Extending
# this set requires a paired migration of any existing cache.
_SUBJECT_TYPES = frozenset({"skills", "agents"})

# Single-slot embedder cache. Reused across ``check_intake`` +
# ``record_embedding`` calls in the same process so the
# sentence-transformers model loads once even in batch mode.
_cached_embedder: Embedder | None = None


class IntakeRejected(RuntimeError):
    """Raised when the intake gate declines a candidate.

    Carries the full :class:`IntakeDecision` so callers can render
    findings back to the user without a re-run.
    """

    def __init__(self, decision: IntakeDecision) -> None:
        failures = decision.failures
        if failures:
            detail = "\n".join(f"  - {f.code}: {f.message}" for f in failures)
            super().__init__(f"intake gate rejected candidate:\n{detail}")
        else:
            super().__init__("intake gate rejected candidate")
        self.decision = decision


def reset_cache() -> None:
    """Clear the process-local embedder cache.

    Exposed for tests and for callers that hot-swap the intake config at
    runtime. Production ``skill_add`` / ``agent_add`` invocations never
    need to call this.
    """
    global _cached_embedder
    _cached_embedder = None


def _require_subject_type(subject_type: str) -> None:
    if subject_type not in _SUBJECT_TYPES:
        raise ValueError(
            f"subject_type must be one of {sorted(_SUBJECT_TYPES)!r}; "
            f"got {subject_type!r}"
        )


def _embedder() -> Embedder:
    global _cached_embedder
    if _cached_embedder is None:
        _cached_embedder = cfg.build_intake_embedder()
    return _cached_embedder


def _cache_for(embedder: Embedder, subject_type: str) -> CorpusCache:
    # Subject type goes first in the key so it survives the 64-char
    # slug cap applied inside CorpusCache. Embedder.name is appended so
    # switching models lands in a separate directory rather than mixing
    # dimensions (ST=384 vs Ollama=768).
    return CorpusCache(
        f"{subject_type}:{embedder.name}",
        root=cfg.intake_cache_root,
    )


def check_intake(raw_md: str, subject_type: str) -> IntakeDecision:
    """Run the intake gate against the current corpus for ``subject_type``.

    Short-circuits to an ``allow=True`` decision when
    ``cfg.intake_enabled`` is False so the call sites in ``skill_add``
    and ``agent_add`` stay flat.
    """
    _require_subject_type(subject_type)
    if not cfg.intake_enabled:
        return IntakeDecision(allow=True)
    embedder = _embedder()
    ranker = CosineRanker.from_cache(_cache_for(embedder, subject_type))
    return run_intake_gate(
        raw_md,
        embedder=embedder,
        ranker=ranker,
        config=cfg.build_intake_config(),
    )


def record_embedding(
    *, subject_id: str, raw_md: str, subject_type: str
) -> None:
    """Embed ``raw_md`` and store the vector under ``subject_id``.

    No-op when intake is disabled. Empty corpus text (candidate with no
    description and empty body) is also a no-op — the structural gate
    should have blocked it upstream, but we guard here so callers that
    skip the gate don't inject junk vectors.
    """
    _require_subject_type(subject_type)
    if not cfg.intake_enabled:
        return
    text = compose_corpus_text(raw_md)
    if not text.strip():
        return
    embedder = _embedder()
    vecs = embedder.embed([text])
    if vecs.shape[0] != 1:
        raise RuntimeError(
            f"embedder returned {vecs.shape[0]} rows for a single-text batch"
        )
    _cache_for(embedder, subject_type).put(subject_id, text, vecs[0])


__all__ = [
    "IntakeRejected",
    "check_intake",
    "record_embedding",
    "reset_cache",
]
