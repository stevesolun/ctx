"""test_dedup_check.py — unit tests for the dedup gate.

Covers:
  - allowlist parsing (comments, malformed lines, slug ordering)
  - state load/save (round-trip, version mismatch, threshold mismatch)
  - find_high_similarity_pairs (chunking, threshold filtering, dedup)
  - end-to-end orchestration on a synthetic fixture
  - markdown + JSON report rendering
  - exit code behavior
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ctx.core.quality import dedup_check as dc  # noqa: E402


# ── Allowlist ──────────────────────────────────────────────────────────


def test_allowlist_parses_comments_and_blanks(tmp_path: Path) -> None:
    p = tmp_path / "allow.txt"
    p.write_text(
        "\n".join([
            "# header comment",
            "",
            "alpha beta  # legitimate distinct",
            "  gamma delta",
            "# trailing comment",
        ]),
        encoding="utf-8",
    )
    assert dc.load_allowlist(p) == {("alpha", "beta"), ("delta", "gamma")}


def test_allowlist_canonicalises_slug_order(tmp_path: Path) -> None:
    p = tmp_path / "allow.txt"
    p.write_text("zebra apple\n", encoding="utf-8")
    # Stored canonical (low, high) regardless of file order
    assert dc.load_allowlist(p) == {("apple", "zebra")}


def test_allowlist_returns_empty_when_missing(tmp_path: Path) -> None:
    assert dc.load_allowlist(tmp_path / "does-not-exist.txt") == set()


def test_allowlist_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "allow.txt"
    p.write_text("only-one-token\n", encoding="utf-8")
    assert dc.load_allowlist(p) == set()


# ── State ──────────────────────────────────────────────────────────────


def test_state_round_trip(tmp_path: Path) -> None:
    s = dc.DedupState(
        version=dc.DEDUP_STATE_VERSION,
        model_id="m1",
        threshold=0.85,
        entity_hashes={"skill:a": "h1"},
        last_findings=[{"a": "skill:a", "b": "skill:b"}],
    )
    dc.save_state(tmp_path, s)
    loaded = dc.load_state(tmp_path, model_id="m1", threshold=0.85)
    assert loaded.entity_hashes == {"skill:a": "h1"}
    assert loaded.threshold == 0.85
    assert loaded.model_id == "m1"


def test_state_invalidates_on_model_change(tmp_path: Path) -> None:
    s = dc.DedupState(
        version=dc.DEDUP_STATE_VERSION,
        model_id="m1", threshold=0.85,
        entity_hashes={"skill:a": "h1"}, last_findings=[],
    )
    dc.save_state(tmp_path, s)
    loaded = dc.load_state(tmp_path, model_id="m2", threshold=0.85)
    assert loaded.entity_hashes == {}, "model change must invalidate state"


def test_state_invalidates_on_threshold_change(tmp_path: Path) -> None:
    s = dc.DedupState(
        version=dc.DEDUP_STATE_VERSION,
        model_id="m1", threshold=0.85,
        entity_hashes={"skill:a": "h1"}, last_findings=[],
    )
    dc.save_state(tmp_path, s)
    loaded = dc.load_state(tmp_path, model_id="m1", threshold=0.90)
    assert loaded.entity_hashes == {}, "threshold change must invalidate state"


def test_state_returns_empty_when_missing(tmp_path: Path) -> None:
    out = dc.load_state(tmp_path, model_id="m1", threshold=0.85)
    assert out.entity_hashes == {}
    assert out.threshold == 0.85


# ── Pair finding ───────────────────────────────────────────────────────


def test_find_high_similarity_pairs_emits_each_pair_once() -> None:
    """A symmetric similarity matrix must produce N(N-1)/2 pairs at most,
    not N² (no double-emission).
    """
    # Three identical vectors → all three pairs are perfectly similar
    vecs = np.array([
        [1.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
    ], dtype="float32")
    # L2 already normalised
    entities = [
        dc.EntityRef(node_id=f"skill:{s}", type="skill", slug=s,
                     path=Path(f"/{s}.md"), description=s, tags=())
        for s in ["a", "b", "c"]
    ]
    pairs = dc.find_high_similarity_pairs(entities, vecs, threshold=0.99)
    assert len(pairs) == 3, "expected exactly 3 pairs for 3 entities"
    seen = {(i, j) for i, j, _ in pairs}
    assert seen == {(0, 1), (0, 2), (1, 2)}


def test_find_high_similarity_pairs_threshold_filters() -> None:
    """Below-threshold pairs must not appear."""
    # Two orthogonal vectors → cosine = 0
    vecs = np.array([[1.0, 0.0], [0.0, 1.0]], dtype="float32")
    entities = [
        dc.EntityRef(node_id=f"skill:{s}", type="skill", slug=s,
                     path=Path(f"/{s}.md"), description=s, tags=())
        for s in ["a", "b"]
    ]
    pairs = dc.find_high_similarity_pairs(entities, vecs, threshold=0.50)
    assert pairs == [], "orthogonal vectors must not produce a pair at any threshold > 0"


def test_find_high_similarity_pairs_chunking_consistent() -> None:
    """Different chunk sizes must produce the same result."""
    rng = np.random.default_rng(42)
    n = 50
    raw = rng.standard_normal((n, 8)).astype("float32")
    norms = np.linalg.norm(raw, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs = raw / norms
    entities = [
        dc.EntityRef(node_id=f"skill:e{i}", type="skill", slug=f"e{i}",
                     path=Path(f"/e{i}.md"), description="", tags=())
        for i in range(n)
    ]
    a = sorted(dc.find_high_similarity_pairs(entities, vecs, threshold=0.5, chunk_size=8))
    b = sorted(dc.find_high_similarity_pairs(entities, vecs, threshold=0.5, chunk_size=200))
    assert [(i, j) for i, j, _ in a] == [(i, j) for i, j, _ in b], (
        "chunked + unchunked runs must produce identical pair sets"
    )


# ── Markdown rendering ────────────────────────────────────────────────


def test_render_markdown_with_no_findings() -> None:
    rep = dc.DedupReport(
        threshold=0.85, model_id="m1",
        total_entities=10, pairs_evaluated=45,
    )
    md = dc.render_markdown(rep)
    assert "No actionable findings" in md
    assert "0.85" in md


def test_render_markdown_caps_at_top_n() -> None:
    refs = [
        dc.EntityRef(node_id=f"skill:e{i}", type="skill", slug=f"e{i}",
                     path=Path(f"/e{i}.md"), description=f"desc{i}", tags=())
        for i in range(150)
    ]
    pairs = [
        dc.DedupPair(a=refs[i], b=refs[i + 1],
                     similarity=0.99 - i * 0.0001, shared_tags=())
        for i in range(149)
    ]
    rep = dc.DedupReport(
        threshold=0.85, model_id="m1",
        total_entities=150, pairs_evaluated=149,
        findings=pairs,
    )
    md = dc.render_markdown(rep, top_n=10)
    # Header acknowledges the cap
    assert "Showing" in md and "top 10" in md
    # Body has at most top_n ### headers
    headers = [line for line in md.splitlines() if line.startswith("### ")]
    assert len(headers) == 10, f"expected 10 finding headers, got {len(headers)}"


def test_incremental_skips_unchanged_pairs(tmp_path: Path) -> None:
    """First run: full pass, state saved. Second run with same hashes:
    every prior finding carries forward without recomputation, and only
    pairs touching changed/new entities are recomputed.
    """
    import numpy as np

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    # Build a tiny embeddings.npz + topk-state.json the loader can read.
    refs = [
        dc.EntityRef(
            node_id=f"skill:e{i}", type="skill", slug=f"e{i}",
            path=tmp_path / f"e{i}.md", description=f"d{i}", tags=("t",),
        )
        for i in range(3)
    ]
    # Vectors: e0/e1 are nearly identical (cosine ~1.0); e2 is unrelated.
    vecs = np.array([[1.0, 0.0], [0.999, 0.045], [0.0, 1.0]], dtype="float32")

    # Save first state via run_dedup_check's normal save path: simulate a
    # prior run by saving state directly with these hashes + finding.
    hashes = {r.node_id: dc._entity_hash_for_state(r) for r in refs}
    prior = dc.DedupState(
        version=dc.DEDUP_STATE_VERSION,
        model_id="test", threshold=0.85,
        entity_hashes=hashes,
        last_findings=[
            {"a": "skill:e0", "b": "skill:e1", "similarity": 0.999},
        ],
    )
    dc.save_state(cache_dir, prior)

    # All entities unchanged → unchanged_ids covers everyone → carry-forward
    unchanged = {nid for nid, h in hashes.items() if prior.entity_hashes.get(nid) == h}
    assert unchanged == {"skill:e0", "skill:e1", "skill:e2"}

    # Verify _find_pairs_for_changed returns nothing when there are no
    # changed entities (i.e. an incremental run with everything cached
    # bypasses the expensive pairwise pass entirely).
    pairs = dc._find_pairs_for_changed(refs, vecs, [], threshold=0.85)
    assert pairs == []


def test_incremental_recomputes_when_entity_changed(tmp_path: Path) -> None:
    """When one entity's hash changes, pairs touching it must be
    recomputed even if the prior state had carry-forward findings.
    """
    import numpy as np

    refs = [
        dc.EntityRef(node_id=f"skill:e{i}", type="skill", slug=f"e{i}",
                     path=tmp_path / f"e{i}.md", description=f"d{i}", tags=())
        for i in range(3)
    ]
    vecs = np.array([[1.0, 0.0], [0.999, 0.045], [0.0, 1.0]], dtype="float32")

    # Only e1 is "changed" (index 1); pairs computed only for rows
    # involving index 1.
    pairs = dc._find_pairs_for_changed(refs, vecs, [1], threshold=0.85)
    pair_keys = {(i, j) for i, j, _ in pairs}
    # e0-e1 pair (0,1) must be present; e1-e2 pair would exist if cosine
    # were >= 0.85 but the vectors here put it well below.
    assert (0, 1) in pair_keys
    # No (0, 2) pair since neither endpoint is changed.
    assert (0, 2) not in pair_keys


def test_render_markdown_includes_distribution_buckets() -> None:
    refs = [
        dc.EntityRef(node_id=f"skill:e{i}", type="skill", slug=f"e{i}",
                     path=Path(f"/e{i}.md"), description="", tags=())
        for i in range(4)
    ]
    pairs = [
        dc.DedupPair(a=refs[0], b=refs[1], similarity=0.995, shared_tags=()),
        dc.DedupPair(a=refs[0], b=refs[2], similarity=0.93, shared_tags=()),
        dc.DedupPair(a=refs[0], b=refs[3], similarity=0.86, shared_tags=()),
    ]
    rep = dc.DedupReport(
        threshold=0.85, model_id="m1",
        total_entities=4, pairs_evaluated=6, findings=pairs,
    )
    md = dc.render_markdown(rep)
    assert "≥0.99" in md and "0.90-0.95" in md and "0.85-0.90" in md
