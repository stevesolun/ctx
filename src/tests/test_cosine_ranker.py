"""
test_cosine_ranker.py -- Tests for the Phase 2 top-K cosine ranker.

Coverage:

  - empty / singleton / k<=0 edge cases
  - ordering: results are sorted descending by score, ties stable
  - k > N returns all N
  - dim mismatch raises
  - query normalisation: raw (un-normalised) query still ranks correctly
  - identity: a vector against its own corpus scores ≈ 1.0
  - antipode: opposite vectors score ≈ -1.0
  - from_cache: adapts any object that exposes ``load_all``
  - from_vectors: rejects mixed-dim, empty OK
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import cosine_ranker as cr  # noqa: E402


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)


# ────────────────────────────────────────────────────────────────────
# Construction
# ────────────────────────────────────────────────────────────────────


def test_empty_corpus_has_size_zero() -> None:
    r = cr.CosineRanker.from_vectors({})
    assert r.size == 0
    assert r.dim == 0
    assert r.subject_ids == ()


def test_from_vectors_rejects_mismatched_dims() -> None:
    with pytest.raises(ValueError, match="expected \\(4,\\)"):
        cr.CosineRanker.from_vectors({
            "a": _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
            "b": _unit(np.array([1.0, 0.0, 0.0], dtype=np.float32)),
        })


def test_from_vectors_rejects_2d() -> None:
    with pytest.raises(ValueError, match="1-D"):
        cr.CosineRanker.from_vectors({"a": np.zeros((2, 2), dtype=np.float32)})


def test_direct_constructor_rejects_row_id_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        cr.CosineRanker(np.zeros((3, 4), dtype=np.float32), ["a", "b"])


def test_direct_constructor_rejects_non_2d_matrix() -> None:
    with pytest.raises(ValueError, match="2-D"):
        cr.CosineRanker(np.zeros((4,), dtype=np.float32), ["a"])


# ────────────────────────────────────────────────────────────────────
# Ranking
# ────────────────────────────────────────────────────────────────────


def _build_small_corpus() -> cr.CosineRanker:
    # 4-dim axis-aligned unit vectors — cosine similarity is the
    # inner product so scores are trivially checkable.
    return cr.CosineRanker.from_vectors({
        "x_axis": _unit(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)),
        "y_axis": _unit(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)),
        "z_axis": _unit(np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)),
        "w_axis": _unit(np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)),
    })


def test_topk_orders_descending_by_score() -> None:
    r = _build_small_corpus()
    # Query close to x_axis but leaning toward y_axis.
    q = np.array([0.9, 0.4, 0.0, 0.0], dtype=np.float32)
    results = r.topk(q, k=4)
    assert [m.subject_id for m in results][:2] == ["x_axis", "y_axis"]
    # Scores strictly non-increasing.
    scores = [m.score for m in results]
    assert all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1))


def test_topk_identity_scores_one() -> None:
    r = _build_small_corpus()
    q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = r.topk(q, k=1)
    assert results[0].subject_id == "x_axis"
    assert results[0].score == pytest.approx(1.0, abs=1e-6)


def test_topk_antipode_scores_negative_one() -> None:
    r = _build_small_corpus()
    q = np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = r.topk(q, k=4)
    # x_axis should be last (score ≈ -1); orthogonal axes score 0.
    assert results[-1].subject_id == "x_axis"
    assert results[-1].score == pytest.approx(-1.0, abs=1e-6)


def test_topk_k_larger_than_corpus_returns_all() -> None:
    r = _build_small_corpus()
    q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = r.topk(q, k=99)
    assert len(results) == 4


def test_topk_k_zero_returns_empty() -> None:
    r = _build_small_corpus()
    q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    assert r.topk(q, k=0) == []


def test_topk_negative_k_returns_empty() -> None:
    r = _build_small_corpus()
    q = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    assert r.topk(q, k=-3) == []


def test_topk_empty_corpus_returns_empty() -> None:
    r = cr.CosineRanker.from_vectors({})
    # dim is 0 on an empty ranker, so any 1-D query short-circuits
    # before the dim check.
    assert r.topk(np.zeros(8, dtype=np.float32), k=5) == []


def test_topk_rejects_dim_mismatch() -> None:
    r = _build_small_corpus()
    with pytest.raises(ValueError, match="does not match corpus dim"):
        r.topk(np.zeros(16, dtype=np.float32), k=1)


def test_topk_rejects_non_1d_query() -> None:
    r = _build_small_corpus()
    with pytest.raises(ValueError, match="1-D"):
        r.topk(np.zeros((2, 4), dtype=np.float32), k=1)


def test_topk_normalises_unnormalised_query() -> None:
    r = _build_small_corpus()
    # Unnormalised query pointing along x. Score must still be ~1.
    q = np.array([7.0, 0.0, 0.0, 0.0], dtype=np.float32)
    results = r.topk(q, k=1)
    assert results[0].subject_id == "x_axis"
    assert results[0].score == pytest.approx(1.0, abs=1e-6)


# ────────────────────────────────────────────────────────────────────
# Larger corpus with argpartition path
# ────────────────────────────────────────────────────────────────────


def test_topk_argpartition_path_matches_full_sort() -> None:
    # 50 random unit vectors in 32-D. Top-5 from argpartition must
    # match top-5 from a pristine full sort.
    rng = np.random.default_rng(42)
    raw = rng.normal(size=(50, 32)).astype(np.float32)
    vectors = {f"id{i:02d}": raw[i] for i in range(50)}
    r = cr.CosineRanker.from_vectors(vectors)
    q = rng.normal(size=32).astype(np.float32)
    got = r.topk(q, k=5)
    expected = r.topk(q, k=50)[:5]
    assert [m.subject_id for m in got] == [m.subject_id for m in expected]


# ────────────────────────────────────────────────────────────────────
# from_cache adapter
# ────────────────────────────────────────────────────────────────────


class _FakeCache:
    def __init__(self, vecs: dict[str, np.ndarray]) -> None:
        self._vecs = vecs

    def load_all(self) -> dict[str, np.ndarray]:
        return self._vecs


def test_from_cache_uses_load_all() -> None:
    fake = _FakeCache({
        "a": _unit(np.array([1.0, 0.0], dtype=np.float32)),
        "b": _unit(np.array([0.0, 1.0], dtype=np.float32)),
    })
    r = cr.CosineRanker.from_cache(fake)
    assert r.size == 2
    assert isinstance(fake, cr.CorpusCacheLike)


def test_from_cache_with_corpus_cache_module(tmp_path: Path) -> None:
    # Live integration — CorpusCache must satisfy CorpusCacheLike.
    import corpus_cache as cc

    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("a", "body-a", _unit(np.array([1.0, 0.0, 0.0], dtype=np.float32)))
    cache.put("b", "body-b", _unit(np.array([0.0, 1.0, 0.0], dtype=np.float32)))
    r = cr.CosineRanker.from_cache(cache)
    assert r.size == 2

    q = np.array([0.9, 0.2, 0.0], dtype=np.float32)
    results = r.topk(q, k=2)
    assert results[0].subject_id == "a"


# ────────────────────────────────────────────────────────────────────
# Input dtype + contiguity
# ────────────────────────────────────────────────────────────────────


def test_constructor_coerces_non_float32_matrix() -> None:
    mat = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float64)
    r = cr.CosineRanker(mat, ["a", "b"])
    assert r.dim == 2
    out = r.topk(np.array([1.0, 0.0], dtype=np.float32), k=1)
    assert out[0].subject_id == "a"
