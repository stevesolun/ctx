#!/usr/bin/env python3
"""
cosine_ranker.py -- Top-K cosine similarity ranker over a corpus.

The intake gate needs to answer "which existing skills/agents are
nearest to this candidate?" fast enough to run on every ``skill_add``.
For a corpus of ~2k subjects at ~384 dims, a pure-numpy dot product
is ~1 ms on a laptop — no FAISS or external index needed.

Contract:

  - Vectors are assumed L2-normalised (the ``Embedder`` Protocol
    guarantees this). We re-normalise query vectors defensively so
    callers supplying raw vectors still get valid cosine scores.
  - ``CosineRanker`` is a read-only view; if the corpus changes the
    caller rebuilds. This keeps the hot path free of locks.
  - ``topk`` uses ``np.argpartition`` followed by a K-sized sort so
    cost scales with K, not with corpus size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

import numpy as np


@runtime_checkable
class CorpusCacheLike(Protocol):
    """Minimal surface required by :meth:`CosineRanker.from_cache`.

    Any cache — real, fake, in-memory — that returns a
    ``dict[subject_id -> 1-D ndarray]`` will do.
    """

    def load_all(self) -> dict[str, np.ndarray]: ...


@dataclass(frozen=True)
class RankedMatch:
    """One (subject_id, cosine_score) pair returned from ``topk``."""

    subject_id: str
    score: float


class CosineRanker:
    """Immutable top-K ranker over a stacked corpus matrix.

    Construct with :meth:`from_vectors` or :meth:`from_cache`. The
    resulting instance is a simple view — the corpus matrix is owned
    outright and never mutated. Rebuild on corpus change.
    """

    def __init__(self, matrix: np.ndarray, subject_ids: Sequence[str]) -> None:
        # Empty corpus is valid: matrix (0, 0), ids ().
        if matrix.ndim != 2:
            raise ValueError(f"matrix must be 2-D, got shape {matrix.shape}")
        if matrix.shape[0] != len(subject_ids):
            raise ValueError(
                f"matrix row count {matrix.shape[0]} does not match "
                f"subject_ids length {len(subject_ids)}"
            )
        if matrix.dtype != np.float32:
            matrix = matrix.astype(np.float32, copy=False)
        # Contiguous layout keeps the dot product cache-friendly.
        self._matrix = np.ascontiguousarray(matrix)
        self._ids: tuple[str, ...] = tuple(subject_ids)

    # ── Constructors ─────────────────────────────────────────────

    @classmethod
    def from_vectors(cls, vectors: Mapping[str, np.ndarray]) -> "CosineRanker":
        """Build from ``{subject_id: 1-D vector}``.

        All vectors must share a dimension. Vectors are L2-normalised
        on construction so the corpus is self-consistent even when
        callers stitched vectors in from mixed sources.
        """
        if not vectors:
            return cls(np.zeros((0, 0), dtype=np.float32), ())
        ids = sorted(vectors.keys())
        first = vectors[ids[0]]
        if first.ndim != 1:
            raise ValueError(f"expected 1-D vectors, got shape {first.shape}")
        dim = first.shape[0]
        rows: list[np.ndarray] = []
        for sid in ids:
            v = vectors[sid]
            if v.ndim != 1 or v.shape[0] != dim:
                raise ValueError(
                    f"vector for {sid!r} has shape {v.shape}; "
                    f"expected ({dim},)"
                )
            rows.append(v.astype(np.float32, copy=False))
        matrix = np.vstack(rows)
        return cls(_l2_normalize_rows(matrix), ids)

    @classmethod
    def from_cache(cls, cache: "CorpusCacheLike") -> "CosineRanker":
        """Build from a :class:`corpus_cache.CorpusCache`.

        Accepts any object with a ``load_all() -> dict[str, ndarray]``
        method. Declared as a structural Protocol to keep the import
        graph acyclic — ``cosine_ranker`` does not depend on
        ``corpus_cache`` directly.
        """
        return cls.from_vectors(cache.load_all())

    # ── Introspection ────────────────────────────────────────────

    @property
    def size(self) -> int:
        return len(self._ids)

    @property
    def dim(self) -> int:
        if self._matrix.size == 0:
            return 0
        return int(self._matrix.shape[1])

    @property
    def subject_ids(self) -> tuple[str, ...]:
        return self._ids

    # ── Ranking ──────────────────────────────────────────────────

    def topk(self, query: np.ndarray, k: int = 5) -> list[RankedMatch]:
        """Return the top-``k`` matches by descending cosine score.

        Returns an empty list on empty corpus or ``k <= 0``. If
        ``k`` exceeds corpus size, every row is returned.
        """
        if k <= 0 or self.size == 0:
            return []
        if not isinstance(query, np.ndarray) or query.ndim != 1:
            raise ValueError(f"query must be a 1-D numpy array, got {query!r}")
        if query.shape[0] != self.dim:
            raise ValueError(
                f"query dim {query.shape[0]} does not match corpus dim {self.dim}"
            )
        q = query.astype(np.float32, copy=False)
        norm = float(np.linalg.norm(q))
        if norm > 0.0:
            q = q / norm

        scores = self._matrix @ q  # (N,)
        k = min(k, scores.shape[0])
        if k == scores.shape[0]:
            order = np.argsort(-scores, kind="stable")
        else:
            # argpartition gives the top-k unordered; sort just those k.
            top_idx = np.argpartition(-scores, k - 1)[:k]
            order = top_idx[np.argsort(-scores[top_idx], kind="stable")]

        return [
            RankedMatch(subject_id=self._ids[int(i)], score=float(scores[int(i)]))
            for i in order
        ]


# ────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────


def _l2_normalize_rows(mat: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalisation with zero-vector safety.

    Kept local (rather than importing from ``embedding_backend``) so
    this module has no hard dependency on the embedding layer. The
    two implementations are trivial and stable.
    """
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return (mat / norms).astype(np.float32, copy=False)


__all__ = ["CosineRanker", "RankedMatch", "CorpusCacheLike"]
