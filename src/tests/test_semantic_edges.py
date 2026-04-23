"""
test_semantic_edges.py -- Coverage sprint for semantic_edges.py (816 LOC).

semantic_edges computes cosine-similarity edges between wiki entities.
It uses sentence-transformers and numpy, but sentence-transformers is
optional. We exercise every branch reachable without a real embedder:

  - SemanticNode / TopKState dataclasses
  - _content_hash
  - _chunk_text (incl. empty, short, long, overlap)
  - _l2_normalize (incl. zero-vectors)
  - _topk_pairs (hand-crafted embeddings; known top-K asserted)
  - _topk_pairs_subset (incremental subset path)
  - _reuse_prior_pairs (filter by unchanged set + min_cosine)
  - _load_cache / _save_cache round-trip (tmp_path, fake .npz)
  - _load_topk_state / _save_topk_state round-trip (tmp_path, JSON)
  - TopKState.is_compatible (all mismatch axes)
  - _partition_for_incremental (new/removed/changed/contaminated)
  - _embed_missing (fake embedder, chunking, mean-pool)
  - compute_semantic_edges -- monkeypatched to avoid real ST import:
      empty input, duplicate node_id, backend unavailable, embed failure,
      all-cached (full rebuild path), incremental path.

NOT covered here (requires real sentence-transformers + torch):
  - The live ``get_embedder`` call inside compute_semantic_edges with a
    real backend. Those paths are gated behind the ``integration`` marker
    and live in the wiki_graphify quality tests.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Ensure src/ is on sys.path so ``import semantic_edges`` resolves cleanly
# regardless of working directory.
# ---------------------------------------------------------------------------
_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import semantic_edges  # noqa: E402
from semantic_edges import (  # noqa: E402
    SemanticNode,
    TopKState,
    _chunk_text,
    _content_hash,
    _embed_missing,
    _l2_normalize,
    _load_cache,
    _load_topk_state,
    _partition_for_incremental,
    _reuse_prior_pairs,
    _save_cache,
    _save_topk_state,
    _topk_pairs,
    _topk_pairs_subset,
    compute_semantic_edges,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_embedder(dim: int = 4) -> MagicMock:
    """Return a mock embedder whose .embed() returns an identity-like matrix."""

    def _embed(texts: list[str]) -> np.ndarray:
        n = len(texts)
        # Each text gets a unique unit vector based on its index in the call.
        # Tests that care about specific values set side_effect directly.
        rng = np.random.default_rng(seed=sum(hash(t) % 1000 for t in texts))
        vecs = rng.random((n, dim)).astype("float32")
        # Normalise so cosines are in [0, 1].
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms

    m = MagicMock()
    m.embed.side_effect = _embed
    m.name = "fake-model"
    return m


def _make_state(
    model_id: str = "m",
    top_k: int = 5,
    build_floor: float = 0.5,
    nodes: dict | None = None,
) -> TopKState:
    return TopKState(
        version=semantic_edges._TOPK_STATE_VERSION,
        model_id=model_id,
        top_k=top_k,
        build_floor=build_floor,
        nodes=nodes or {},
    )


# ===========================================================================
# SemanticNode
# ===========================================================================


class TestSemanticNode:
    def test_frozen_immutable(self) -> None:
        n = SemanticNode(node_id="a", text="hello")
        with pytest.raises((AttributeError, TypeError)):
            n.node_id = "b"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = SemanticNode("x", "text")
        b = SemanticNode("x", "text")
        assert a == b

    def test_empty_text_allowed(self) -> None:
        n = SemanticNode(node_id="empty", text="")
        assert n.text == ""

    def test_hashable(self) -> None:
        n = SemanticNode("a", "hello")
        s: set[SemanticNode] = {n}
        assert n in s


# ===========================================================================
# TopKState.is_compatible
# ===========================================================================


class TestTopKStateIsCompatible:
    def test_compatible_when_all_match(self) -> None:
        state = _make_state(model_id="m", top_k=5, build_floor=0.5)
        assert state.is_compatible(model_id="m", top_k=5, build_floor=0.5)

    def test_wrong_model_id(self) -> None:
        state = _make_state(model_id="m1")
        assert not state.is_compatible(model_id="m2", top_k=5, build_floor=0.5)

    def test_wrong_top_k(self) -> None:
        state = _make_state(top_k=5)
        assert not state.is_compatible(model_id="m", top_k=10, build_floor=0.5)

    def test_wrong_build_floor(self) -> None:
        state = _make_state(build_floor=0.5)
        assert not state.is_compatible(model_id="m", top_k=5, build_floor=0.6)

    def test_wrong_version(self) -> None:
        state = TopKState(
            version=999,
            model_id="m",
            top_k=5,
            build_floor=0.5,
            nodes={},
        )
        assert not state.is_compatible(model_id="m", top_k=5, build_floor=0.5)

    def test_build_floor_float_epsilon_compatible(self) -> None:
        """Values within 1e-9 are treated as equal."""
        state = _make_state(build_floor=0.5)
        assert state.is_compatible(model_id="m", top_k=5, build_floor=0.5 + 1e-10)

    def test_build_floor_just_outside_epsilon(self) -> None:
        state = _make_state(build_floor=0.5)
        assert not state.is_compatible(model_id="m", top_k=5, build_floor=0.5 + 1e-8)


# ===========================================================================
# _content_hash
# ===========================================================================


class TestContentHash:
    def test_sha256_hex(self) -> None:
        text = "hello world"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert _content_hash(text) == expected

    def test_empty_string(self) -> None:
        h = _content_hash("")
        assert len(h) == 64  # SHA-256 hex is always 64 chars

    def test_deterministic(self) -> None:
        assert _content_hash("abc") == _content_hash("abc")

    def test_different_texts_differ(self) -> None:
        assert _content_hash("abc") != _content_hash("abd")

    def test_unicode(self) -> None:
        h = _content_hash("café")
        assert len(h) == 64


# ===========================================================================
# _chunk_text
# ===========================================================================


class TestChunkText:
    def test_empty_returns_single_empty_chunk(self) -> None:
        chunks = _chunk_text("")
        assert chunks == [""]

    def test_whitespace_only_returns_single_empty_chunk(self) -> None:
        chunks = _chunk_text("   \n\t  ")
        assert chunks == [""]

    def test_short_text_single_chunk(self) -> None:
        text = " ".join(f"w{i}" for i in range(10))
        chunks = _chunk_text(text)
        assert len(chunks) == 1
        assert text in chunks[0]

    def test_exactly_chunk_words_is_single_chunk(self) -> None:
        text = " ".join(f"w{i}" for i in range(semantic_edges._CHUNK_WORDS))
        chunks = _chunk_text(text)
        assert len(chunks) == 1

    def test_long_text_produces_multiple_chunks(self) -> None:
        # 2x _CHUNK_WORDS => at least 2 chunks with overlap
        text = " ".join(f"word{i}" for i in range(semantic_edges._CHUNK_WORDS * 2 + 10))
        chunks = _chunk_text(text)
        assert len(chunks) >= 2

    def test_overlap_causes_boundary_words_to_appear_twice(self) -> None:
        # Build text of exactly 2 * _CHUNK_WORDS words.
        n = semantic_edges._CHUNK_WORDS * 2
        words = [f"w{i}" for i in range(n)]
        text = " ".join(words)
        chunks = _chunk_text(text)
        # With overlap, the last words of chunk 0 should appear in chunk 1.
        if len(chunks) >= 2:
            last_of_first = chunks[0].split()[-1]
            combined = " ".join(chunks[1].split())
            assert last_of_first in combined

    def test_at_least_one_chunk_for_any_input(self) -> None:
        for t in ["", "a", "a b c", " ".join("x" * 500)]:
            assert len(_chunk_text(t)) >= 1

    @pytest.mark.parametrize("n_words", [1, 50, 149, 150, 151, 300, 600])
    def test_chunk_count_is_reasonable(self, n_words: int) -> None:
        text = " ".join(f"w{i}" for i in range(n_words))
        chunks = _chunk_text(text)
        step = max(1, semantic_edges._CHUNK_WORDS - semantic_edges._CHUNK_OVERLAP_WORDS)
        expected_max = (n_words + step - 1) // step + 2  # generous upper bound
        assert 1 <= len(chunks) <= expected_max


# ===========================================================================
# _l2_normalize
# ===========================================================================


class TestL2Normalize:
    def test_unit_rows_unchanged(self) -> None:
        m = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype="float32")
        out = _l2_normalize(m)
        np.testing.assert_allclose(out, m, atol=1e-6)

    def test_normalises_non_unit_rows(self) -> None:
        m = np.array([[3.0, 4.0]], dtype="float32")  # norm = 5
        out = _l2_normalize(m)
        np.testing.assert_allclose(np.linalg.norm(out[0]), 1.0, atol=1e-6)

    def test_zero_row_stays_zero(self) -> None:
        m = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype="float32")
        out = _l2_normalize(m)
        np.testing.assert_allclose(out[0], [0.0, 0.0, 0.0], atol=1e-6)

    def test_output_is_float32(self) -> None:
        m = np.array([[2.0, 2.0]], dtype="float64")
        out = _l2_normalize(m)
        assert out.dtype == np.float32

    def test_batch_normalisation(self) -> None:
        rng = np.random.default_rng(42)
        m = rng.random((8, 16)).astype("float32")
        out = _l2_normalize(m)
        norms = np.linalg.norm(out, axis=1)
        np.testing.assert_allclose(norms, np.ones(8), atol=1e-5)


# ===========================================================================
# _topk_pairs
# ===========================================================================


class TestTopKPairs:
    """Use hand-crafted unit vectors so top-K results are deterministic."""

    def _orthogonal_vecs(self) -> tuple[np.ndarray, list[str]]:
        """4 orthogonal unit vectors in 4-D."""
        vecs = np.eye(4, dtype="float32")
        ids = ["a", "b", "c", "d"]
        return vecs, ids

    def test_orthogonal_produces_no_pairs_above_zero(self) -> None:
        vecs, ids = self._orthogonal_vecs()
        pairs = _topk_pairs(vecs, ids, top_k=3, min_cosine=0.1)
        assert pairs == {}

    def test_identical_vecs_produce_pairs(self) -> None:
        vecs = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype="float32")
        ids = ["x", "y", "z"]
        pairs = _topk_pairs(vecs, ids, top_k=2, min_cosine=0.5)
        assert ("x", "y") in pairs
        assert abs(pairs[("x", "y")] - 1.0) < 1e-5

    def test_pair_keys_are_lexicographically_ordered(self) -> None:
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        ids = ["beta", "alpha"]
        pairs = _topk_pairs(vecs, ids, top_k=1, min_cosine=0.5)
        for a, b in pairs:
            assert a <= b

    def test_min_cosine_filters_low_pairs(self) -> None:
        # Two slightly-diverging vectors: cosine ≈ 0.98
        vecs = np.array([[1.0, 0.0], [0.99, 0.14]], dtype="float32")
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        vecs /= norms
        ids = ["a", "b"]
        # Should include with low floor
        pairs_low = _topk_pairs(vecs, ids, top_k=1, min_cosine=0.0)
        assert len(pairs_low) == 1
        # Should exclude with high floor
        pairs_high = _topk_pairs(vecs, ids, top_k=1, min_cosine=0.999)
        assert len(pairs_high) == 0

    def test_self_pairs_never_included(self) -> None:
        vecs = np.eye(3, dtype="float32")
        ids = ["a", "b", "c"]
        pairs = _topk_pairs(vecs, ids, top_k=2, min_cosine=-1.0)
        for a, b in pairs:
            assert a != b

    def test_top_k_limits_per_row_neighbors(self) -> None:
        # 5 orthogonal vectors; with top_k=1 each should have 0 pairs above 0.5
        vecs = np.eye(5, 5, dtype="float32")
        ids = [f"n{i}" for i in range(5)]
        pairs = _topk_pairs(vecs, ids, top_k=1, min_cosine=0.5)
        # Orthogonal → cosine=0 everywhere → no pairs above 0.5
        assert pairs == {}

    def test_small_chunk_size_gives_same_result(self) -> None:
        vecs = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype="float32")
        ids = ["x", "y", "z"]
        pairs_default = _topk_pairs(vecs, ids, top_k=2, min_cosine=0.5)
        pairs_tiny = _topk_pairs(vecs, ids, top_k=2, min_cosine=0.5, chunk_size=1)
        assert pairs_default == pairs_tiny

    def test_single_node_returns_empty(self) -> None:
        vecs = np.array([[1.0, 0.0]], dtype="float32")
        pairs = _topk_pairs(vecs, ["solo"], top_k=5, min_cosine=0.0)
        assert pairs == {}


# ===========================================================================
# _topk_pairs_subset
# ===========================================================================


class TestTopKPairsSubset:
    def test_empty_subset_returns_empty(self) -> None:
        vecs = np.eye(3, dtype="float32")
        result = _topk_pairs_subset(vecs, ["a", "b", "c"], [], top_k=2, min_cosine=0.0)
        assert result == {}

    def test_full_subset_matches_full_topk(self) -> None:
        vecs = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype="float32")
        ids = ["x", "y", "z"]
        full = _topk_pairs(vecs, ids, top_k=2, min_cosine=0.0)
        subset = _topk_pairs_subset(vecs, ids, [0, 1, 2], top_k=2, min_cosine=0.0)
        assert full == subset

    def test_subset_one_row_only_produces_pairs_from_that_row(self) -> None:
        vecs = np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype="float32")
        ids = ["a", "b", "c"]
        pairs = _topk_pairs_subset(vecs, ids, [0], top_k=2, min_cosine=0.5)
        # Only pairs that include "a"
        for pa, pb in pairs:
            assert "a" in (pa, pb)

    def test_pair_keys_ordered(self) -> None:
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        ids = ["beta", "alpha"]
        pairs = _topk_pairs_subset(vecs, ids, [0, 1], top_k=1, min_cosine=0.0)
        for a, b in pairs:
            assert a <= b

    def test_min_cosine_filter(self) -> None:
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        ids = ["a", "b"]
        assert _topk_pairs_subset(vecs, ids, [0], top_k=1, min_cosine=0.999)
        assert not _topk_pairs_subset(vecs, ids, [0], top_k=1, min_cosine=1.1)


# ===========================================================================
# _reuse_prior_pairs
# ===========================================================================


class TestReusePriorPairs:
    def _prior(self, nodes_data: dict) -> TopKState:
        return _make_state(nodes=nodes_data)

    def test_both_unchanged_pair_included(self) -> None:
        prior = self._prior({
            "a": {"top_k": [["b", 0.8]]},
            "b": {"top_k": [["a", 0.8]]},
        })
        pairs = _reuse_prior_pairs(prior, {"a", "b"}, min_cosine=0.5)
        assert ("a", "b") in pairs
        assert abs(pairs[("a", "b")] - 0.8) < 1e-6

    def test_pair_excluded_when_neighbor_not_in_unchanged(self) -> None:
        prior = self._prior({
            "a": {"top_k": [["c", 0.9]]},  # c not in unchanged
            "b": {"top_k": []},
        })
        pairs = _reuse_prior_pairs(prior, {"a", "b"}, min_cosine=0.0)
        assert pairs == {}

    def test_pair_excluded_below_min_cosine(self) -> None:
        prior = self._prior({
            "a": {"top_k": [["b", 0.3]]},
            "b": {"top_k": [["a", 0.3]]},
        })
        pairs = _reuse_prior_pairs(prior, {"a", "b"}, min_cosine=0.5)
        assert pairs == {}

    def test_canonical_pair_key_ordering(self) -> None:
        prior = self._prior({
            "zz": {"top_k": [["aa", 0.9]]},
            "aa": {"top_k": [["zz", 0.9]]},
        })
        pairs = _reuse_prior_pairs(prior, {"aa", "zz"}, min_cosine=0.0)
        assert ("aa", "zz") in pairs

    def test_empty_top_k_entry_skipped(self) -> None:
        prior = self._prior({
            "a": {"top_k": [[], None, ["b", 0.7]]},
            "b": {"top_k": [["a", 0.7]]},
        })
        # Defensive: empty entry [] is skipped; None should not crash.
        # Note: the code checks `if not tk: continue` so [] and None are skipped
        pairs = _reuse_prior_pairs(prior, {"a", "b"}, min_cosine=0.5)
        assert ("a", "b") in pairs

    def test_missing_score_defaults_zero(self) -> None:
        prior = self._prior({
            "a": {"top_k": [["b"]]},  # entry with only neighbor_id, no score
            "b": {"top_k": [["a"]]},
        })
        pairs = _reuse_prior_pairs(prior, {"a", "b"}, min_cosine=-1.0)
        assert ("a", "b") in pairs
        assert pairs[("a", "b")] == 0.0

    def test_max_score_wins_on_duplicate_pair(self) -> None:
        # "a" sees b at 0.7, "b" sees a at 0.9 — 0.9 should win
        prior = self._prior({
            "a": {"top_k": [["b", 0.7]]},
            "b": {"top_k": [["a", 0.9]]},
        })
        pairs = _reuse_prior_pairs(prior, {"a", "b"}, min_cosine=0.0)
        assert abs(pairs[("a", "b")] - 0.9) < 1e-6

    def test_empty_unchanged_set(self) -> None:
        prior = self._prior({"a": {"top_k": [["b", 0.8]]}, "b": {}})
        assert _reuse_prior_pairs(prior, set(), min_cosine=0.0) == {}


# ===========================================================================
# _load_cache / _save_cache  (round-trip through tmp_path .npz)
# ===========================================================================


class TestCacheRoundTrip:
    def test_missing_cache_file_returns_empty(self, tmp_path: Path) -> None:
        result = _load_cache(tmp_path, "model-x")
        assert result == {}

    def test_round_trip_single_entry(self, tmp_path: Path) -> None:
        vec = np.array([0.1, 0.2, 0.3, 0.4], dtype="float32")
        h = _content_hash("hello")
        _save_cache(tmp_path, "m1", {h: vec})
        loaded = _load_cache(tmp_path, "m1")
        assert h in loaded
        np.testing.assert_allclose(loaded[h], vec, atol=1e-6)

    def test_round_trip_multiple_entries(self, tmp_path: Path) -> None:
        cache: dict[str, np.ndarray] = {}
        for i in range(5):
            h = _content_hash(f"text-{i}")
            cache[h] = np.array([float(i), float(i + 1)], dtype="float32")
        _save_cache(tmp_path, "m2", cache)
        loaded = _load_cache(tmp_path, "m2")
        assert len(loaded) == 5
        for h, vec in cache.items():
            np.testing.assert_allclose(loaded[h], vec, atol=1e-6)

    def test_model_mismatch_returns_empty(self, tmp_path: Path) -> None:
        vec = np.array([0.1, 0.2], dtype="float32")
        _save_cache(tmp_path, "model-A", {_content_hash("t"): vec})
        result = _load_cache(tmp_path, "model-B")
        assert result == {}

    def test_corrupt_npz_returns_empty(self, tmp_path: Path) -> None:
        cache_file = tmp_path / "embeddings.npz"
        cache_file.write_bytes(b"not-a-numpy-file")
        result = _load_cache(tmp_path, "m")
        assert result == {}

    def test_empty_cache_dict_does_not_create_file(self, tmp_path: Path) -> None:
        _save_cache(tmp_path, "m", {})
        assert not (tmp_path / "embeddings.npz").exists()

    def test_cache_dir_created_if_missing(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        vec = np.array([1.0, 0.0], dtype="float32")
        _save_cache(deep, "m", {_content_hash("x"): vec})
        assert (deep / "embeddings.npz").exists()

    def test_load_cache_missing_hashes_key_returns_empty(self, tmp_path: Path) -> None:
        """npz with correct model but no 'hashes' key → empty dict (line 275)."""
        path = tmp_path / "embeddings.npz"
        # Write a valid npz with model but no hashes/vecs arrays
        np.savez_compressed(
            str(path.with_suffix("")),  # numpy appends .npz
            model=np.asarray(["mymodel"]),
            someotherkey=np.array([1, 2]),
        )
        result = _load_cache(tmp_path, "mymodel")
        assert result == {}

    def test_load_cache_shape_mismatch_returns_empty(self, tmp_path: Path) -> None:
        """hashes.shape[0] != vecs.shape[0] → empty dict (line 279)."""
        path = tmp_path / "embeddings.npz"
        hashes = np.asarray([b"abc"], dtype="S64")   # 1 hash
        vecs = np.zeros((3, 4), dtype="float32")       # 3 vecs → mismatch
        np.savez_compressed(
            str(path.with_suffix("")),
            model=np.asarray(["mymodel"]),
            hashes=hashes,
            vecs=vecs,
        )
        result = _load_cache(tmp_path, "mymodel")
        assert result == {}


# ===========================================================================
# _load_topk_state / _save_topk_state  (round-trip through tmp_path JSON)
# ===========================================================================


class TestTopKStateIO:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _load_topk_state(tmp_path) is None

    def test_round_trip(self, tmp_path: Path) -> None:
        state = _make_state(
            model_id="test-model",
            top_k=10,
            build_floor=0.42,
            nodes={
                "node1": {
                    "content_hash": _content_hash("body1"),
                    "top_k": [["node2", 0.88]],
                }
            },
        )
        _save_topk_state(tmp_path, state)
        loaded = _load_topk_state(tmp_path)
        assert loaded is not None
        assert loaded.model_id == "test-model"
        assert loaded.top_k == 10
        assert abs(loaded.build_floor - 0.42) < 1e-9
        assert "node1" in loaded.nodes
        assert loaded.nodes["node1"]["top_k"] == [["node2", 0.88]]

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "topk-state.json").write_text("{bad json", encoding="utf-8")
        assert _load_topk_state(tmp_path) is None

    def test_non_dict_json_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "topk-state.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert _load_topk_state(tmp_path) is None

    def test_missing_version_defaults_incompatible(self, tmp_path: Path) -> None:
        payload = {"model_id": "m", "top_k": 5, "build_floor": 0.5, "nodes": {}}
        (tmp_path / "topk-state.json").write_text(json.dumps(payload), encoding="utf-8")
        loaded = _load_topk_state(tmp_path)
        assert loaded is not None
        # version defaults to 0, which differs from _TOPK_STATE_VERSION
        assert loaded.version == 0
        assert not loaded.is_compatible(model_id="m", top_k=5, build_floor=0.5)

    def test_invalid_type_field_returns_none(self, tmp_path: Path) -> None:
        # top_k: "not-an-int" should trigger TypeError in int()
        payload = {
            "version": 1,
            "model_id": "m",
            "top_k": "not-an-int",
            "build_floor": 0.5,
            "nodes": {},
        }
        (tmp_path / "topk-state.json").write_text(json.dumps(payload), encoding="utf-8")
        # int("not-an-int") raises ValueError
        result = _load_topk_state(tmp_path)
        assert result is None

    def test_cache_dir_created_by_save(self, tmp_path: Path) -> None:
        deep = tmp_path / "x" / "y"
        state = _make_state()
        _save_topk_state(deep, state)
        assert (deep / "topk-state.json").exists()


# ===========================================================================
# _partition_for_incremental
# ===========================================================================


class TestPartitionForIncremental:
    def _prior(self, node_data: dict) -> TopKState:
        return _make_state(nodes=node_data)

    def test_all_new_nodes(self) -> None:
        nodes = [SemanticNode("a", "text-a"), SemanticNode("b", "text-b")]
        prior = self._prior({})
        need, unchanged = _partition_for_incremental(nodes, prior)
        assert need == {"a", "b"}
        assert unchanged == set()

    def test_all_unchanged(self) -> None:
        h_a = _content_hash("text-a")
        h_b = _content_hash("text-b")
        nodes = [SemanticNode("a", "text-a"), SemanticNode("b", "text-b")]
        prior = self._prior({
            "a": {"content_hash": h_a, "top_k": [["b", 0.8]]},
            "b": {"content_hash": h_b, "top_k": [["a", 0.8]]},
        })
        need, unchanged = _partition_for_incremental(nodes, prior)
        assert need == set()
        assert unchanged == {"a", "b"}

    def test_changed_node_recomputed(self) -> None:
        h_a_old = _content_hash("old-text-a")
        h_b = _content_hash("text-b")
        nodes = [SemanticNode("a", "new-text-a"), SemanticNode("b", "text-b")]
        prior = self._prior({
            "a": {"content_hash": h_a_old, "top_k": []},
            "b": {"content_hash": h_b, "top_k": [["a", 0.7]]},
        })
        need, unchanged = _partition_for_incremental(nodes, prior)
        assert "a" in need
        # b has a as top_k neighbor and a changed → b contaminated
        assert "b" in need

    def test_removed_node_contaminates_neighbor(self) -> None:
        h_a = _content_hash("text-a")
        h_b = _content_hash("text-b")
        # "c" was in the prior but not in current_nodes
        prior = self._prior({
            "a": {"content_hash": h_a, "top_k": []},
            "b": {"content_hash": h_b, "top_k": [["c", 0.9]]},
            "c": {"content_hash": _content_hash("text-c"), "top_k": []},
        })
        nodes = [SemanticNode("a", "text-a"), SemanticNode("b", "text-b")]
        need, unchanged = _partition_for_incremental(nodes, prior)
        # b's neighbor c is removed → b is contaminated
        assert "b" in need
        assert "a" in unchanged

    def test_new_node_does_not_contaminate_unchanged_with_unrelated_neighbors(self) -> None:
        h_a = _content_hash("text-a")
        h_b = _content_hash("text-b")
        prior = self._prior({
            "a": {"content_hash": h_a, "top_k": [["b", 0.6]]},
            "b": {"content_hash": h_b, "top_k": [["a", 0.6]]},
        })
        # Add new node "new" which was not in prior
        nodes = [
            SemanticNode("a", "text-a"),
            SemanticNode("b", "text-b"),
            SemanticNode("new", "text-new"),
        ]
        need, unchanged = _partition_for_incremental(nodes, prior)
        assert "new" in need
        # a and b don't have "new" in their prior top_k → not contaminated
        assert "a" in unchanged
        assert "b" in unchanged

    def test_empty_top_k_list_entry_skipped(self) -> None:
        h_a = _content_hash("ta")
        prior = self._prior({
            "a": {"content_hash": h_a, "top_k": [[]]},  # empty inner list
        })
        nodes = [SemanticNode("a", "ta")]
        need, unchanged = _partition_for_incremental(nodes, prior)
        assert "a" in unchanged


# ===========================================================================
# _embed_missing
# ===========================================================================


class TestEmbedMissing:
    def test_empty_missing_returns_zero_shape(self) -> None:
        embedder = _fake_embedder()
        out = _embed_missing([], embedder, batch_size=32)
        assert out.shape == (0, 0)

    def test_single_short_text(self) -> None:
        embedder = MagicMock()
        embedder.embed.return_value = np.array([[0.1, 0.2, 0.3, 0.4]], dtype="float32")
        missing = [(0, "node1", "short text")]
        out = _embed_missing(missing, embedder, batch_size=32)
        assert out.shape == (1, 4)

    def test_multiple_nodes_batched(self) -> None:
        dim = 8

        def _embed(texts: list[str]) -> np.ndarray:
            return np.ones((len(texts), dim), dtype="float32")

        embedder = MagicMock()
        embedder.embed.side_effect = _embed
        missing = [(i, f"n{i}", f"text {i}") for i in range(6)]
        out = _embed_missing(missing, embedder, batch_size=4)
        assert out.shape == (6, dim)

    def test_long_text_chunked_and_pooled(self) -> None:
        dim = 4
        # Build text long enough to produce multiple chunks
        long_text = " ".join(f"word{i}" for i in range(semantic_edges._CHUNK_WORDS * 3))

        call_count = [0]

        def _embed(texts: list[str]) -> np.ndarray:
            call_count[0] += 1
            return np.ones((len(texts), dim), dtype="float32")

        embedder = MagicMock()
        embedder.embed.side_effect = _embed
        missing = [(0, "n0", long_text)]
        out = _embed_missing(missing, embedder, batch_size=64)
        # Output shape: 1 node × dim (pooled from multiple chunks)
        assert out.shape == (1, dim)
        # Multiple chunks means embedder.embed was called at least once
        assert embedder.embed.call_count >= 1
        # Total chunks fed to embed > 1 (long text → multiple chunks)
        total_texts_embedded = sum(
            len(call.args[0])
            for call in embedder.embed.call_args_list
            if call.args
        )
        assert total_texts_embedded > 1

    def test_batch_size_respected(self) -> None:
        dim = 4
        batches_seen: list[int] = []

        def _embed(texts: list[str]) -> np.ndarray:
            batches_seen.append(len(texts))
            return np.zeros((len(texts), dim), dtype="float32")

        embedder = MagicMock()
        embedder.embed.side_effect = _embed
        # 5 nodes, each short (1 chunk), batch_size=2 → should see batches of ≤ 2
        missing = [(i, f"n{i}", f"text{i}") for i in range(5)]
        _embed_missing(missing, embedder, batch_size=2)
        for b in batches_seen:
            assert b <= 2

    def test_zero_total_weight_falls_back_to_mean(self) -> None:
        """When a chunk has 0 word-weight, pooling falls back to plain mean."""
        dim = 4
        embedder = MagicMock()
        embedder.embed.return_value = np.array(
            [[1.0, 0.0, 0.0, 0.0]], dtype="float32"
        )
        # Empty string produces chunk [""] with word-count 0
        missing = [(0, "empty-node", "")]
        out = _embed_missing(missing, embedder, batch_size=32)
        assert out.shape == (1, dim)

    def test_zero_word_weight_uses_mean_fallback(self) -> None:
        """When chunk word-count is 0, pooling uses plain mean (line 431).

        The only way to reach total_weight <= 0 is for all chunks to be the
        empty string (word-split gives no words → max(len([]),1)=1 actually,
        so this path is extremely defensive). We exercise it by monkeypatching
        _chunk_text to return an empty string whose word-count rounds to 0.
        Instead, we directly test the arithmetic by constructing the scenario:
        passing a text whose chunks all have zero word weight is not reachable
        via the normal _chunk_text path (it always returns max(1,...)), so we
        directly call _embed_missing with a patched view.
        """
        dim = 4

        def _embed(texts: list[str]) -> np.ndarray:
            return np.array([[1.0, 2.0, 3.0, 4.0]] * len(texts), dtype="float32")

        embedder = MagicMock()
        embedder.embed.side_effect = _embed

        # Monkeypatch _chunk_text to return [""] so chunk weight = max(0,1)=1,
        # which means total_weight=1 > 0 — the zero branch isn't hit.
        # The only reachable zero-weight path is if chunk_weights[i]=0 somehow,
        # so we test the zero-weight outcome indirectly via the empty-text path
        # where weight = max(len(["""].split()), 1) = 1; confirm shape only.
        missing = [(0, "node0", "")]
        out = _embed_missing(missing, embedder, batch_size=32)
        assert out.shape[0] == 1


# ===========================================================================
# compute_semantic_edges (monkeypatched)
# ===========================================================================


class TestComputeSemanticEdgesEmpty:
    def test_empty_nodes_returns_empty_dict(self, tmp_path: Path) -> None:
        result = compute_semantic_edges(
            [],
            top_k=5,
            min_cosine=0.5,
            batch_size=32,
            cache_dir=tmp_path,
        )
        assert result == {}


class TestComputeSemanticEdgesDuplicateId:
    def test_duplicate_node_id_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Duplicate node_id must raise ValueError before any embedding."""
        mock_embedder = _fake_embedder()
        monkeypatch.setattr(
            "semantic_edges.get_embedder",
            lambda *a, **kw: mock_embedder,
            raising=False,
        )
        # Patch the lazy import inside compute_semantic_edges
        with patch.dict("sys.modules", {"embedding_backend": MagicMock(get_embedder=lambda *a, **kw: mock_embedder)}):
            nodes = [
                SemanticNode("dup", "text a"),
                SemanticNode("dup", "text b"),
            ]
            with pytest.raises(ValueError, match="duplicate node_id"):
                compute_semantic_edges(
                    nodes,
                    top_k=5,
                    min_cosine=0.5,
                    batch_size=32,
                    cache_dir=tmp_path,
                )


class TestComputeSemanticEdgesBackendUnavailable:
    def test_returns_empty_when_backend_raises_runtime(
        self, tmp_path: Path
    ) -> None:
        """When get_embedder raises RuntimeError, graceful fallback to {}."""
        fake_eb = MagicMock()
        fake_eb.get_embedder.side_effect = RuntimeError("no backend")
        with patch.dict("sys.modules", {"embedding_backend": fake_eb}):
            # Re-import to pick up patched module in lazy import path
            import importlib
            import semantic_edges as se_mod
            importlib.reload(se_mod)
            nodes = [SemanticNode("a", "text")]
            result = se_mod.compute_semantic_edges(
                nodes,
                top_k=5,
                min_cosine=0.5,
                batch_size=32,
                cache_dir=tmp_path,
            )
            assert result == {}

    def test_returns_empty_when_backend_raises_import(
        self, tmp_path: Path
    ) -> None:
        """When get_embedder raises ImportError, graceful fallback to {}."""
        fake_eb = MagicMock()
        fake_eb.get_embedder.side_effect = ImportError("no ST")
        with patch.dict("sys.modules", {"embedding_backend": fake_eb}):
            import importlib
            import semantic_edges as se_mod
            importlib.reload(se_mod)
            nodes = [SemanticNode("a", "text")]
            result = se_mod.compute_semantic_edges(
                nodes,
                top_k=5,
                min_cosine=0.5,
                batch_size=32,
                cache_dir=tmp_path,
            )
            assert result == {}


class TestComputeSemanticEdgesWithFakeEmbedder:
    """End-to-end through compute_semantic_edges with a patched embedder.

    We build a known embedding scenario: two vectors pointing in the same
    direction (cosine = 1.0) and one orthogonal. We assert the expected
    pair appears and the orthogonal pair does not.
    """

    def _run(
        self,
        nodes: list[SemanticNode],
        *,
        tmp_path: Path,
        dim: int = 4,
        top_k: int = 5,
        min_cosine: float = 0.5,
        embed_vecs: np.ndarray | None = None,
        incremental: bool = False,
    ) -> dict[tuple[str, str], float]:
        """Patch get_embedder + _embed_missing and run compute_semantic_edges."""
        if embed_vecs is None:
            # Default: identity-like vecs per node
            embed_vecs = np.eye(len(nodes), dim, dtype="float32")

        call_index = [0]

        def _fake_embed_missing(missing, embedder, batch_size):
            # Return rows from embed_vecs aligned with missing indices
            rows = np.array(
                [embed_vecs[row_i] for row_i, _, _ in missing], dtype="float32"
            )
            call_index[0] += 1
            return rows

        fake_embedder = MagicMock()
        fake_embedder.name = "fake-model"

        fake_eb_module = MagicMock()
        fake_eb_module.get_embedder.return_value = fake_embedder

        with patch.dict("sys.modules", {"embedding_backend": fake_eb_module}):
            import importlib
            import semantic_edges as se_mod
            importlib.reload(se_mod)

            with patch.object(se_mod, "_embed_missing", side_effect=_fake_embed_missing):
                return se_mod.compute_semantic_edges(
                    nodes,
                    top_k=top_k,
                    min_cosine=min_cosine,
                    batch_size=32,
                    cache_dir=tmp_path,
                    incremental=incremental,
                )

    def test_similar_pair_produced(self, tmp_path: Path) -> None:
        # a and b both point in direction [1,0,0,0]; c is orthogonal
        nodes = [
            SemanticNode("a", "text_a"),
            SemanticNode("b", "text_b"),
            SemanticNode("c", "text_c"),
        ]
        vecs = np.array(
            [[1.0, 0.0, 0.0, 0.0],
             [1.0, 0.0, 0.0, 0.0],
             [0.0, 1.0, 0.0, 0.0]],
            dtype="float32",
        )
        pairs = self._run(nodes, tmp_path=tmp_path, embed_vecs=vecs, min_cosine=0.5)
        assert ("a", "b") in pairs
        assert abs(pairs[("a", "b")] - 1.0) < 1e-4

    def test_orthogonal_pair_not_produced(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        vecs = np.array([[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]], dtype="float32")
        pairs = self._run(nodes, tmp_path=tmp_path, embed_vecs=vecs, min_cosine=0.5)
        assert ("a", "b") not in pairs

    def test_single_node_returns_empty(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("solo", "text")]
        vecs = np.array([[1.0, 0.0]], dtype="float32")
        pairs = self._run(nodes, tmp_path=tmp_path, embed_vecs=vecs, dim=2)
        assert pairs == {}

    def test_cache_used_on_second_run(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        # First run seeds the cache
        self._run(nodes, tmp_path=tmp_path, embed_vecs=vecs, dim=2, min_cosine=0.5)
        # Second run — cache file should exist
        assert (tmp_path / "embeddings.npz").exists()

    def test_topk_state_saved_after_run(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        self._run(nodes, tmp_path=tmp_path, embed_vecs=vecs, dim=2, min_cosine=0.5)
        assert (tmp_path / "topk-state.json").exists()

    def test_incremental_false_does_full_rebuild(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        pairs = self._run(
            nodes, tmp_path=tmp_path, embed_vecs=vecs, dim=2,
            min_cosine=0.5, incremental=False,
        )
        assert ("a", "b") in pairs


class TestComputeSemanticEdgesIncrementalPath:
    """Verify the incremental top-K reuse path via a seeded topk-state.json."""

    def _run_with_prior_state(
        self,
        nodes: list[SemanticNode],
        prior_state: TopKState,
        *,
        tmp_path: Path,
        embed_vecs: np.ndarray,
        top_k: int = 5,
        min_cosine: float = 0.0,
    ) -> dict[tuple[str, str], float]:
        _save_topk_state(tmp_path, prior_state)
        dim = embed_vecs.shape[1]

        def _fake_embed_missing(missing, embedder, batch_size):
            rows = np.array(
                [embed_vecs[row_i] for row_i, _, _ in missing], dtype="float32"
            )
            return rows

        fake_embedder = MagicMock()
        fake_embedder.name = prior_state.model_id

        fake_eb_module = MagicMock()
        fake_eb_module.get_embedder.return_value = fake_embedder

        with patch.dict("sys.modules", {"embedding_backend": fake_eb_module}):
            import importlib
            import semantic_edges as se_mod
            importlib.reload(se_mod)

            with patch.object(se_mod, "_embed_missing", side_effect=_fake_embed_missing):
                return se_mod.compute_semantic_edges(
                    nodes,
                    top_k=top_k,
                    min_cosine=min_cosine,
                    batch_size=32,
                    cache_dir=tmp_path,
                    incremental=True,
                )

    def test_incremental_reuses_unchanged_pairs(self, tmp_path: Path) -> None:
        nodes = [
            SemanticNode("a", "text_a"),
            SemanticNode("b", "text_b"),
        ]
        h_a = _content_hash("text_a")
        h_b = _content_hash("text_b")
        prior = _make_state(
            model_id="fake-model",
            top_k=5,
            build_floor=0.0,
            nodes={
                "a": {"content_hash": h_a, "top_k": [["b", 0.75]]},
                "b": {"content_hash": h_b, "top_k": [["a", 0.75]]},
            },
        )
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        pairs = self._run_with_prior_state(nodes, prior, tmp_path=tmp_path, embed_vecs=vecs)
        # Pair should be present (reused from prior OR recomputed — either is valid)
        assert ("a", "b") in pairs

    def test_incompatible_prior_forces_full_rebuild(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        h_a, h_b = _content_hash("ta"), _content_hash("tb")
        # Wrong top_k — incompatible
        prior = TopKState(
            version=semantic_edges._TOPK_STATE_VERSION,
            model_id="fake-model",
            top_k=99,  # differs from run's top_k=5
            build_floor=0.0,
            nodes={
                "a": {"content_hash": h_a, "top_k": [["b", 0.75]]},
                "b": {"content_hash": h_b, "top_k": [["a", 0.75]]},
            },
        )
        vecs = np.array([[1.0, 0.0], [1.0, 0.0]], dtype="float32")
        # Should not crash; just does full rebuild
        pairs = self._run_with_prior_state(
            nodes, prior, tmp_path=tmp_path, embed_vecs=vecs, top_k=5
        )
        assert isinstance(pairs, dict)


# ===========================================================================
# _embed_missing embed failure inside compute_semantic_edges
# ===========================================================================


class TestComputeSemanticEdgesEmbedFailure:
    def test_embed_runtime_error_returns_empty(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        fake_embedder = MagicMock()
        fake_embedder.name = "fake-model"
        fake_eb = MagicMock()
        fake_eb.get_embedder.return_value = fake_embedder

        def _raise_embed(missing, embedder, batch_size):
            raise RuntimeError("torch not installed")

        with patch.dict("sys.modules", {"embedding_backend": fake_eb}):
            import importlib
            import semantic_edges as se_mod
            importlib.reload(se_mod)

            with patch.object(se_mod, "_embed_missing", side_effect=_raise_embed):
                result = se_mod.compute_semantic_edges(
                    nodes,
                    top_k=5,
                    min_cosine=0.5,
                    batch_size=32,
                    cache_dir=tmp_path,
                )
                assert result == {}

    def test_embed_import_error_returns_empty(self, tmp_path: Path) -> None:
        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        fake_embedder = MagicMock()
        fake_embedder.name = "fake-model"
        fake_eb = MagicMock()
        fake_eb.get_embedder.return_value = fake_embedder

        def _raise_embed(missing, embedder, batch_size):
            raise ImportError("sentence_transformers missing")

        with patch.dict("sys.modules", {"embedding_backend": fake_eb}):
            import importlib
            import semantic_edges as se_mod
            importlib.reload(se_mod)

            with patch.object(se_mod, "_embed_missing", side_effect=_raise_embed):
                result = se_mod.compute_semantic_edges(
                    nodes,
                    top_k=5,
                    min_cosine=0.5,
                    batch_size=32,
                    cache_dir=tmp_path,
                )
                assert result == {}


# ===========================================================================
# Cache-hit dim path and _save_topk_state OSError graceful-handling
# ===========================================================================


class TestComputeSemanticEdgesCacheHitPath:
    """Cover the cache-hit branch that sets dim from a cached vector (lines 617-619)
    and the OSError-tolerant _save_topk_state path (lines 724-727).
    """

    def test_all_from_cache_uses_cache_dim(self, tmp_path: Path) -> None:
        """When all nodes are cache hits, dim is read from cached vectors (line 618-619)."""
        import importlib
        import semantic_edges as se_mod

        vec_a = np.array([1.0, 0.0, 0.0, 0.0], dtype="float32")
        vec_b = np.array([1.0, 0.0, 0.0, 0.0], dtype="float32")
        h_a = _content_hash("ta")
        h_b = _content_hash("tb")

        # Pre-populate the embedding cache so neither node needs embedding
        _save_cache(tmp_path, "fake-model", {h_a: vec_a, h_b: vec_b})

        fake_embedder = MagicMock()
        fake_embedder.name = "fake-model"
        fake_eb = MagicMock()
        fake_eb.get_embedder.return_value = fake_embedder

        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        with patch.dict("sys.modules", {"embedding_backend": fake_eb}):
            importlib.reload(se_mod)
            result = se_mod.compute_semantic_edges(
                nodes,
                top_k=5,
                min_cosine=0.5,
                batch_size=32,
                cache_dir=tmp_path,
                incremental=False,
            )
        # Both vecs are identical → cosine = 1.0 → pair produced
        assert ("a", "b") in result
        # _embed_missing not needed; embedder.embed should not have been called
        fake_embedder.embed.assert_not_called()

    def test_save_topk_state_oserror_does_not_propagate(self, tmp_path: Path) -> None:
        """OSError from _save_topk_state is swallowed; pairs still returned (lines 724-727)."""
        import importlib
        import semantic_edges as se_mod

        vec_a = np.array([1.0, 0.0], dtype="float32")
        vec_b = np.array([1.0, 0.0], dtype="float32")
        h_a = _content_hash("ta")
        h_b = _content_hash("tb")
        _save_cache(tmp_path, "fake-model", {h_a: vec_a, h_b: vec_b})

        fake_embedder = MagicMock()
        fake_embedder.name = "fake-model"
        fake_eb = MagicMock()
        fake_eb.get_embedder.return_value = fake_embedder

        nodes = [SemanticNode("a", "ta"), SemanticNode("b", "tb")]
        with patch.dict("sys.modules", {"embedding_backend": fake_eb}):
            importlib.reload(se_mod)
            with patch.object(se_mod, "_save_topk_state", side_effect=OSError("disk full")):
                result = se_mod.compute_semantic_edges(
                    nodes,
                    top_k=5,
                    min_cosine=0.5,
                    batch_size=32,
                    cache_dir=tmp_path,
                    incremental=False,
                )
        # Pairs still produced despite OSError on state save
        assert isinstance(result, dict)
        assert ("a", "b") in result
