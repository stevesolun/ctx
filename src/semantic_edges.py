#!/usr/bin/env python3
"""
semantic_edges.py -- Cosine-similarity edges between wiki entity pages.

Produces a dict ``{(node_id_a, node_id_b): cosine}`` from a list of
``SemanticNode`` tuples. Used by ``wiki_graphify.build_graph`` to
augment the tag- and slug-token-driven edges with context-level
similarity (e.g. a Python-linting MCP linked to a code-reviewer agent
even when they share no explicit tags).

Design constraints:

  - **Batched embedding** — N can be 13,000+ entities; a per-node
    model call would be prohibitive. We batch through
    ``embedding_backend.get_embedder().embed`` which already handles
    the sentence-transformers ``encode`` batching internally.
  - **Top-K per row** — a dense N×N cosine matrix is 700MB at N=13,039
    and produces 85M edges, most of them noise. We take the top_k
    neighbors per node and drop pairs whose cosine is below
    ``min_cosine``. Yields at most N×top_k edges; after dedup on
    unordered pairs roughly half that.
  - **Content-hash cache** — a persistent ``.npz`` keyed by SHA-256
    of the embedded text. A regraphify after ingesting 200 new
    entities only embeds those 200; the other 12,800 are pulled
    from cache in O(1). Cache invalidates automatically when the
    model identifier changes.

The module is deliberately import-light: sentence-transformers and
numpy are imported lazily inside ``compute_semantic_edges`` so that
``ctx_config`` / ``wiki_graphify`` can import this file at module
load without pulling heavy deps into the hot import path.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    import numpy as np

_logger = logging.getLogger(__name__)

__all__ = [
    "SemanticNode",
    "compute_semantic_edges",
    "TopKState",
]

# ── Top-K state persistence (incremental mode) ───────────────────────────────
#
# On a full regraphify we embed + compute top-K neighbors for every node.
# On an incremental regraphify we only recompute top-K for nodes whose
# text changed, whose neighbors' text changed, or that appeared/vanished.
# The state file holds per-node content hashes + top-K neighbor lists so
# the next run can diff and skip.

_TOPK_STATE_FILENAME = "topk-state.json"
_TOPK_STATE_VERSION = 1


@dataclass(frozen=True)
class SemanticNode:
    """One entity's view for semantic edge building.

    ``text`` is what gets embedded; we concatenate name + description.
    Empty text is allowed but will embed to the null-vector of the
    model; callers that want to skip empty-text nodes should filter
    upstream.
    """

    node_id: str
    text: str


@dataclass(frozen=True)
class TopKState:
    """Persistent top-K neighbor cache for incremental regraphify.

    Invalidation axes: a change to ``model_id``, ``top_k``, or
    ``build_floor`` makes the cached top-K meaningless (different
    filter or different geometry), so the orchestrator rebuilds
    from scratch. A change to the entity body text invalidates
    that one entity's entry; its neighbors may or may not be
    affected depending on whether the changed entity was in their
    top-K (that cascade is resolved by ``_partition_for_incremental``).

    ``nodes`` maps node_id to:
      - content_hash: SHA-256 of the text we embedded
      - top_k: [[neighbor_id, cosine], ...] — descending by cosine
    """

    version: int
    model_id: str
    top_k: int
    build_floor: float
    nodes: dict[str, dict]  # node_id -> {"content_hash": ..., "top_k": [[id, score], ...]}

    def is_compatible(
        self, *, model_id: str, top_k: int, build_floor: float,
    ) -> bool:
        """True when a cached state can feed the current run as-is."""
        return (
            self.version == _TOPK_STATE_VERSION
            and self.model_id == model_id
            and self.top_k == top_k
            # build_floor must match exactly; a lower current floor
            # would let the cache admit pairs it dropped previously,
            # a higher one would orphan cached pairs we should keep.
            and abs(self.build_floor - build_floor) < 1e-9
        )


def _topk_state_path(cache_dir: Path) -> Path:
    return cache_dir / _TOPK_STATE_FILENAME


def _load_topk_state(cache_dir: Path) -> TopKState | None:
    """Load the prior run's top-K state. Returns None on any issue.

    Tolerates missing file, bad JSON, wrong schema version — anything
    unexpected resets to "no cache" and forces a full rebuild.
    """
    import json  # noqa: PLC0415 — keep the cold-path import local

    path = _topk_state_path(cache_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("semantic_edges: topk-state load failed (%s)", exc)
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return TopKState(
            version=int(raw.get("version", 0)),
            model_id=str(raw.get("model_id", "")),
            top_k=int(raw.get("top_k", 0)),
            build_floor=float(raw.get("build_floor", 0.0)),
            nodes=dict(raw.get("nodes", {})),
        )
    except (TypeError, ValueError) as exc:
        _logger.warning("semantic_edges: topk-state shape invalid (%s)", exc)
        return None


def _save_topk_state(cache_dir: Path, state: TopKState) -> None:
    """Atomically persist the top-K state via a tmp-file + os.replace."""
    import json  # noqa: PLC0415
    from _fs_utils import atomic_write_text  # noqa: PLC0415

    payload = {
        "version": state.version,
        "model_id": state.model_id,
        "top_k": state.top_k,
        "build_floor": state.build_floor,
        "nodes": state.nodes,
    }
    cache_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_topk_state_path(cache_dir), json.dumps(payload))


def _partition_for_incremental(
    current_nodes: list[SemanticNode],
    prior: TopKState,
) -> tuple[set[str], set[str]]:
    """Partition current nodes into (need_recompute, unchanged).

    A node needs recomputation when:
      1. It's new (no prior entry).
      2. Its content hash changed.
      3. Any neighbor in its prior top-K has itself been invalidated
         — either removed from the current set, or its hash changed.

    Propagation is conservative single-pass: we compute the first-order
    "affected" set (new + hash-changed + removed), then for every
    unchanged node check if any of its prior top-K neighbors fell into
    that set. That covers the common case (a handful of entities
    updated in the latest ingest). It does NOT iterate to stability —
    second-order shifts (where A's neighbor B drops because B's
    neighbor C changed) are picked up on the next full rebuild.
    The trade-off is acceptable because cosine scores are relative-
    rank stable for small changes; transitive rotation of the tail
    of a top-20 list rarely changes the recommendations.
    """
    current_by_id: dict[str, str] = {
        n.node_id: _content_hash(n.text) for n in current_nodes
    }
    prior_ids = set(prior.nodes.keys())
    current_ids = set(current_by_id.keys())

    new = current_ids - prior_ids
    removed = prior_ids - current_ids
    overlap = current_ids & prior_ids

    changed: set[str] = set()
    for nid in overlap:
        cached_hash = prior.nodes[nid].get("content_hash", "")
        if cached_hash != current_by_id[nid]:
            changed.add(nid)

    first_order_affected = new | changed | removed

    # Second pass: any unchanged node whose prior top-K contained an
    # affected node must be recomputed.
    contaminated: set[str] = set()
    for nid in overlap - changed:
        prior_neighbors = prior.nodes[nid].get("top_k", [])
        for entry in prior_neighbors:
            # entry is [neighbor_id, score]; defensive in case of shape drift.
            if not entry:
                continue
            neighbor_id = entry[0] if isinstance(entry, list) else str(entry)
            if neighbor_id in first_order_affected:
                contaminated.add(nid)
                break

    need_recompute = new | changed | contaminated
    unchanged = current_ids - need_recompute
    return need_recompute, unchanged


# ────────────────────────────────────────────────────────────────────
# Cache
# ────────────────────────────────────────────────────────────────────


def _content_hash(text: str) -> str:
    """SHA-256 of the embedded text. Stable across runs and platforms."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_file(cache_dir: Path) -> Path:
    return cache_dir / "embeddings.npz"


def _load_cache(cache_dir: Path, model_id: str) -> dict[str, "np.ndarray"]:
    """Load ``{content_hash: embedding}`` from the .npz file.

    Returns an empty dict when the cache is missing, corrupt, or
    recorded under a different model. A different ``model_id`` means
    the vectors are incompatible; we throw away everything rather
    than silently mixing dimensions/geometries.
    """
    import numpy as np  # noqa: PLC0415 — lazy import, optional dep

    path = _cache_file(cache_dir)
    if not path.is_file():
        return {}
    try:
        data = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as exc:
        _logger.warning("semantic_edges: cache load failed (%s); starting fresh", exc)
        return {}

    cached_model = ""
    if "model" in data.files:
        arr = data["model"]
        if arr.size:
            cached_model = str(arr.item()) if arr.ndim == 0 else str(arr[0])
    if cached_model != model_id:
        _logger.info(
            "semantic_edges: cache model %r != requested %r; invalidating",
            cached_model, model_id,
        )
        return {}

    if "hashes" not in data.files or "vecs" not in data.files:
        return {}
    hashes = data["hashes"]
    vecs = data["vecs"]
    if hashes.shape[0] != vecs.shape[0]:
        return {}
    out: dict[str, np.ndarray] = {}
    for h, v in zip(hashes, vecs, strict=False):
        key = h.decode("utf-8") if isinstance(h, bytes) else str(h)
        out[key] = v
    return out


def _save_cache(
    cache_dir: Path,
    model_id: str,
    cache: dict[str, "np.ndarray"],
) -> None:
    """Atomically write the content-hash → embedding map to disk."""
    import numpy as np  # noqa: PLC0415

    cache_dir.mkdir(parents=True, exist_ok=True)
    if not cache:
        return
    keys = sorted(cache.keys())
    hashes = np.asarray(keys, dtype="S64")
    vecs = np.stack([cache[k] for k in keys]).astype("float32")
    from _fs_utils import _replace_with_retry  # noqa: PLC0415 — local import

    path = _cache_file(cache_dir)
    # ``np.savez_compressed`` auto-appends ``.npz`` when the filename
    # doesn't already end that way. Hand it a name without ``.npz`` so
    # the behaviour is deterministic: we know numpy writes
    # ``embeddings.tmp.npz``. ``with_suffix(".npz")`` would REPLACE the
    # ``.tmp`` suffix and hand us the wrong on-disk name — use
    # ``with_name`` with explicit concatenation to append instead.
    tmp_stem = path.with_name("embeddings.tmp")  # on-disk target: embeddings.tmp.npz
    np.savez_compressed(tmp_stem, hashes=hashes, vecs=vecs, model=np.asarray([model_id]))
    tmp_real = tmp_stem.with_name(tmp_stem.name + ".npz")  # "embeddings.tmp.npz"
    # ``_replace_with_retry`` rather than raw ``os.replace`` — on Windows
    # the os-level rename can race with antivirus/indexer handles on a
    # 20MB+ .npz written milliseconds earlier. The wrapper retries 10x
    # at 50ms each, totalling up to 500ms, which beats a 14-min rebuild
    # losing its output to a transient lock.
    _replace_with_retry(str(tmp_real), path)


# ────────────────────────────────────────────────────────────────────
# Embedding
# ────────────────────────────────────────────────────────────────────


def _l2_normalize(matrix: "np.ndarray") -> "np.ndarray":
    """L2-normalize rows so ``matrix @ matrix.T`` is a cosine matrix.

    Rows whose norm is zero (truly empty text that embedded to the
    null vector) are left as zeros — their cosine to everything is 0,
    which is what we want.
    """
    import numpy as np  # noqa: PLC0415

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (matrix / norms).astype("float32")


# Chunking config. all-MiniLM-L6-v2 caps at 256 tokens (~180 words)
# before silent truncation; using 150 words per chunk leaves room for
# the tokenizer's per-word variance without losing trailing content.
# Overlap preserves context that spans chunk boundaries (a sentence
# cut mid-way still gets embedded coherently in one of the chunks).
_CHUNK_WORDS = 150
_CHUNK_OVERLAP_WORDS = 20


def _chunk_text(text: str) -> list[str]:
    """Split *text* into overlapping word-chunks of ~``_CHUNK_WORDS`` words.

    Returns at least one chunk for any non-empty input so callers
    can assume ``len(chunks) >= 1`` when text is present. Falls back
    to a single empty-string chunk for empty input so the mean-pool
    has something to average.

    Tokenization here is whitespace-based, not model-aware, so the
    chunk sizes are approximate — but the embedder itself applies
    the real tokenizer and tolerates slight over/under counts.
    """
    words = text.split()
    if not words:
        return [""]
    step = max(1, _CHUNK_WORDS - _CHUNK_OVERLAP_WORDS)
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + _CHUNK_WORDS, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += step
    return chunks


def _embed_missing(
    missing: list[tuple[int, str, str]],  # [(row_index, node_id, text)]
    embedder: "object",
    batch_size: int,
) -> "np.ndarray":
    """Embed ``missing`` texts with chunk + mean-pool.

    Each text is split into word-chunks, all chunks go through the
    embedder in one flat pass (batched by ``batch_size``), and the
    per-node vector is the length-weighted mean of its chunk
    embeddings. This captures the "whole context" of long entity
    bodies instead of silently truncating them to the first 256
    tokens the way a naive ``encode(full_text)`` would.

    Returns an (M, dim) matrix aligned with the input order.
    """
    import numpy as np  # noqa: PLC0415

    if not missing:
        return np.zeros((0, 0), dtype="float32")

    # Phase 1: chunk every text, remember the ranges per node so
    # we can pool correctly after the flat embedding pass.
    all_chunks: list[str] = []
    node_ranges: list[tuple[int, int]] = []  # inclusive-exclusive index range
    chunk_weights: list[int] = []  # word count per chunk for length-weighted pool
    for _row_i, _node_id, text in missing:
        chunks = _chunk_text(text)
        start = len(all_chunks)
        for c in chunks:
            all_chunks.append(c)
            chunk_weights.append(max(len(c.split()), 1))
        node_ranges.append((start, len(all_chunks)))

    # Phase 2: batched embedding over the flat chunk list.
    chunk_embeddings: list[np.ndarray] = []
    for batch_start in range(0, len(all_chunks), batch_size):
        batch = all_chunks[batch_start : batch_start + batch_size]
        matrix = embedder.embed(batch)  # type: ignore[attr-defined]
        chunk_embeddings.append(np.asarray(matrix, dtype="float32"))
    if not chunk_embeddings:
        return np.zeros((0, 0), dtype="float32")
    chunks_matrix = np.vstack(chunk_embeddings)
    dim = chunks_matrix.shape[1]

    # Phase 3: length-weighted mean-pool per node range.
    out = np.zeros((len(missing), dim), dtype="float32")
    weights_arr = np.asarray(chunk_weights, dtype="float32")
    for i, (start, end) in enumerate(node_ranges):
        if end == start:
            continue  # defensive — should have 1 empty chunk at minimum
        node_vecs = chunks_matrix[start:end]
        node_weights = weights_arr[start:end]
        total_weight = float(node_weights.sum())
        if total_weight <= 0.0:
            out[i] = node_vecs.mean(axis=0)
        else:
            out[i] = (node_vecs * (node_weights / total_weight)[:, None]).sum(axis=0)
    return out


# ────────────────────────────────────────────────────────────────────
# Top-K cosine
# ────────────────────────────────────────────────────────────────────


def _topk_pairs(
    vecs: "np.ndarray",
    node_ids: list[str],
    top_k: int,
    min_cosine: float,
    chunk_size: int = 512,
) -> dict[tuple[str, str], float]:
    """Return unordered-pair → max-cosine for top-K neighbors per row.

    We compute cosine in chunks (``chunk_size`` rows × N columns) to
    avoid materialising the full NxN matrix. When a pair (i, j) shows
    up twice (once from row i, once from row j) we keep the higher
    cosine — they should be identical, but floating-point drift in
    different matmul orderings can produce tiny divergences.
    """
    import numpy as np  # noqa: PLC0415

    n = vecs.shape[0]
    out: dict[tuple[str, str], float] = {}

    # top_k + 1 because argpartition returns the node itself (cosine=1.0)
    # as its own nearest neighbor. We mask self below, but asking for
    # one extra is defensive against ties.
    effective_k = min(top_k + 1, n)

    for start in range(0, n, chunk_size):
        end = min(start + chunk_size, n)
        chunk = vecs[start:end]
        # (chunk_rows, N) cosine matrix for this chunk
        sims = chunk @ vecs.T
        # Mask self-pairs: the diagonal of the full matrix maps to
        # column `start + i` for row `i` within the chunk.
        for i in range(end - start):
            sims[i, start + i] = -1.0
        # Top-K columns per row.
        idx_unsorted = np.argpartition(-sims, effective_k - 1, axis=1)[:, :effective_k]
        for i in range(end - start):
            src_id = node_ids[start + i]
            for j in idx_unsorted[i]:
                if j == start + i:
                    continue
                score = float(sims[i, j])
                if score < min_cosine:
                    continue
                dst_id = node_ids[int(j)]
                pair = (src_id, dst_id) if src_id < dst_id else (dst_id, src_id)
                existing = out.get(pair)
                if existing is None or score > existing:
                    out[pair] = score
    return out


# ────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────


def compute_semantic_edges(
    nodes: Iterable[SemanticNode],
    *,
    top_k: int,
    min_cosine: float,
    batch_size: int,
    cache_dir: Path,
    backend: str = "sentence-transformers",
    model: str | None = None,
    incremental: bool = True,
) -> dict[tuple[str, str], float]:
    """Build the semantic-similarity edge map.

    Args:
        nodes: Iterable of ``SemanticNode`` — one per entity page.
            Duplicate ``node_id`` values are rejected (a programming
            error; IDs must be unique across skills/agents/MCPs).
        top_k: Per-node neighbor cap.
        min_cosine: Minimum cosine to admit a pair. In the graphify
            caller this is ``build_floor`` — the inclusion threshold,
            not the display filter.
        batch_size: Embedding batch size. 128 is a good default for
            sentence-transformers on CPU.
        cache_dir: On-disk cache root. Contains ``embeddings.npz``
            (content-hash keyed vectors) and ``topk-state.json``
            (per-node top-K neighbor lists for incremental mode).
        backend: ``embedding_backend`` backend name.
        model: Optional override of the backend default model.
        incremental: When True (default), skip top-K recomputation for
            nodes whose content is unchanged AND whose prior top-K
            doesn't reference any changed/removed node. Falls through
            to a full rebuild when the state file is missing or its
            schema/model/top_k/build_floor don't match the current
            run — see ``TopKState.is_compatible``.

    Returns:
        ``{(node_id_low, node_id_high): cosine}`` with node IDs
        canonicalised to the lexicographically-smaller/larger order.
        Pairs with cosine < ``min_cosine`` are dropped.
    """
    import numpy as np  # noqa: PLC0415

    from embedding_backend import get_embedder  # noqa: PLC0415

    node_list = list(nodes)
    if not node_list:
        return {}

    # Fail fast on duplicate IDs — silently deduping them would hide
    # an ingest bug that produced two entities with the same slug.
    seen: set[str] = set()
    for n in node_list:
        if n.node_id in seen:
            raise ValueError(f"duplicate node_id in semantic edges input: {n.node_id!r}")
        seen.add(n.node_id)

    try:
        embedder = get_embedder(backend, model=model)
        model_id = getattr(embedder, "name", f"{backend}:{model or 'default'}")
    except (RuntimeError, ImportError) as exc:
        # Graceful fallback: if the embedding backend can't load
        # (sentence-transformers not installed, ollama daemon down,
        # etc.) we return no semantic pairs rather than crashing the
        # entire graph build. Consumers fall through to tag + token
        # edges only — reduced signal, same operational graph.
        # Users who want semantic must install the embeddings extra:
        #   pip install "claude-ctx[embeddings]"
        _logger.warning(
            "semantic_edges: embedding backend unavailable (%s) — "
            "skipping semantic pairs. Install the 'embeddings' extra "
            "to enable semantic edges.", exc,
        )
        return {}

    cache = _load_cache(cache_dir, model_id)

    # ── Incremental partition (optional) ──────────────────────────────
    # When a prior top-K state exists and is parameter-compatible, we
    # can skip top-K recomputation for unchanged nodes. Otherwise we
    # fall through to the full path.
    prior_state: TopKState | None = None
    need_recompute: set[str] = {n.node_id for n in node_list}  # default: all
    unchanged: set[str] = set()
    if incremental:
        prior_state = _load_topk_state(cache_dir)
        if prior_state is not None and prior_state.is_compatible(
            model_id=model_id, top_k=top_k, build_floor=min_cosine,
        ):
            need_recompute, unchanged = _partition_for_incremental(
                node_list, prior_state,
            )
            _logger.info(
                "semantic_edges: incremental partition — recompute=%d, reuse=%d (of %d)",
                len(need_recompute), len(unchanged), len(node_list),
            )
        else:
            _logger.info(
                "semantic_edges: no compatible prior state; full rebuild"
            )
            prior_state = None  # force full rebuild path

    _logger.info(
        "semantic_edges: embed cache hits=%d of %d nodes (model=%s)",
        sum(1 for n in node_list if _content_hash(n.text) in cache),
        len(node_list),
        model_id,
    )

    # ── Embedding pass (still covers every node; cache makes it cheap) ─
    # We need every node's vector for the top-K pass even in incremental
    # mode, because a recomputing node's top-K is computed against ALL
    # nodes (not just other recomputing nodes).
    dim = -1
    missing: list[tuple[int, str, str]] = []
    row_vecs: list[np.ndarray | None] = [None] * len(node_list)
    for i, n in enumerate(node_list):
        h = _content_hash(n.text)
        if h in cache:
            row_vecs[i] = cache[h]
            if dim == -1:
                dim = int(row_vecs[i].shape[0])  # type: ignore[union-attr]
        else:
            missing.append((i, n.node_id, n.text))

    if missing:
        _logger.info(
            "semantic_edges: embedding %d uncached texts in batches of %d",
            len(missing), batch_size,
        )
        try:
            new_vecs = _embed_missing(missing, embedder, batch_size)
        except (RuntimeError, ImportError) as exc:
            # The embed call lazily loads the backend model
            # (sentence-transformers, torch). Missing dep surfaces here
            # as RuntimeError; ImportError catches the unlikely case of
            # a bare import sneaking past the lazy guard. Either way,
            # fall back to "no semantic pairs" so build_graph still
            # produces a functional tag+token graph.
            _logger.warning(
                "semantic_edges: embedding failed (%s) — skipping semantic "
                "pairs for this run. Install the 'embeddings' extra: "
                "pip install \"claude-ctx[embeddings]\"", exc,
            )
            return {}
        if new_vecs.size:
            dim = int(new_vecs.shape[1])
            for (row_i, _nid, text), vec in zip(missing, new_vecs, strict=True):
                row_vecs[row_i] = vec
                cache[_content_hash(text)] = vec
            _save_cache(cache_dir, model_id, cache)

    if dim <= 0:
        _logger.warning("semantic_edges: no embeddings produced (empty input?)")
        return {}

    for i, v in enumerate(row_vecs):
        if v is None:
            raise RuntimeError(
                f"internal: node_id={node_list[i].node_id!r} has no embedding row"
            )

    vecs = np.vstack([v for v in row_vecs if v is not None]).astype("float32")
    vecs = _l2_normalize(vecs)
    node_ids = [n.node_id for n in node_list]

    # ── Top-K pass (incremental or full) ──────────────────────────────
    if prior_state is not None and unchanged:
        # Incremental path: compute fresh top-K only for ``need_recompute``;
        # reuse prior_state for ``unchanged`` nodes.
        recompute_indices = [
            i for i, nid in enumerate(node_ids) if nid in need_recompute
        ]
        fresh_pairs = _topk_pairs_subset(
            vecs, node_ids, recompute_indices,
            top_k=top_k, min_cosine=min_cosine,
        )
        reused_pairs = _reuse_prior_pairs(
            prior_state, unchanged, min_cosine,
        )
        # fresh_pairs always wins on overlap — it's computed against the
        # current full vector set, so any numeric drift from a changed
        # neighbor's vector is already baked in.
        pairs: dict[tuple[str, str], float] = dict(reused_pairs)
        pairs.update(fresh_pairs)
        _logger.info(
            "semantic_edges: incremental pairs — fresh=%d, reused=%d, total=%d",
            len(fresh_pairs), len(reused_pairs), len(pairs),
        )
    else:
        # Full rebuild path.
        pairs = _topk_pairs(
            vecs, node_ids, top_k=top_k, min_cosine=min_cosine,
        )
        _logger.info(
            "semantic_edges: full-rebuild pairs — %d (top_k=%d, floor=%.2f)",
            len(pairs), top_k, min_cosine,
        )

    # ── Persist the fresh state for the next run's incremental pass ───
    # Materialise per-node top-K from the pair dict. The state stores
    # it per source node (not canonicalised pairs) so next run can
    # check each node's neighbors independently.
    per_node_topk: dict[str, list[list]] = {nid: [] for nid in node_ids}
    for (a, b), score in pairs.items():
        per_node_topk[a].append([b, score])
        per_node_topk[b].append([a, score])
    for nid, entries in per_node_topk.items():
        entries.sort(key=lambda e: -e[1])
        del entries[top_k:]

    new_state = TopKState(
        version=_TOPK_STATE_VERSION,
        model_id=model_id,
        top_k=top_k,
        build_floor=min_cosine,
        nodes={
            nid: {
                "content_hash": _content_hash(node_list[i].text),
                "top_k": per_node_topk[nid],
            }
            for i, nid in enumerate(node_ids)
        },
    )
    try:
        _save_topk_state(cache_dir, new_state)
    except OSError as exc:
        # State is an optimisation — a failure to persist doesn't
        # invalidate the edges we just produced.
        _logger.warning("semantic_edges: topk-state save failed (%s)", exc)

    return pairs


def _topk_pairs_subset(
    vecs: "np.ndarray",
    node_ids: list[str],
    subset_indices: list[int],
    *,
    top_k: int,
    min_cosine: float,
    chunk_size: int = 512,
) -> dict[tuple[str, str], float]:
    """Top-K cosine but only for rows whose index is in ``subset_indices``.

    Used by the incremental path: we want fresh top-K for changed and
    new nodes but we still evaluate them against the FULL vector set
    (their neighbors may be unchanged nodes). Returns the same
    unordered-pair → cosine shape as ``_topk_pairs``.
    """
    import numpy as np  # noqa: PLC0415

    if not subset_indices:
        return {}

    out: dict[tuple[str, str], float] = {}
    effective_k = min(top_k + 1, vecs.shape[0])

    # Chunk the subset so memory stays bounded regardless of how many
    # nodes changed.
    for chunk_start in range(0, len(subset_indices), chunk_size):
        chunk_indices = subset_indices[chunk_start : chunk_start + chunk_size]
        chunk = vecs[chunk_indices]
        sims = chunk @ vecs.T  # (chunk_rows, N_total)

        # Mask self-pairs. For each chunk-local row i, the absolute
        # row index is chunk_indices[i] — that's the column to mask.
        for i, abs_i in enumerate(chunk_indices):
            sims[i, abs_i] = -1.0

        idx_unsorted = np.argpartition(-sims, effective_k - 1, axis=1)[:, :effective_k]
        for i, abs_i in enumerate(chunk_indices):
            src_id = node_ids[abs_i]
            for j in idx_unsorted[i]:
                j = int(j)
                if j == abs_i:
                    continue
                score = float(sims[i, j])
                if score < min_cosine:
                    continue
                dst_id = node_ids[j]
                pair = (src_id, dst_id) if src_id < dst_id else (dst_id, src_id)
                existing = out.get(pair)
                if existing is None or score > existing:
                    out[pair] = score
    return out


def _reuse_prior_pairs(
    prior: TopKState,
    unchanged: set[str],
    min_cosine: float,
) -> dict[tuple[str, str], float]:
    """Lift pairs from the prior state for nodes marked unchanged.

    A pair ``(A, B)`` is reused when BOTH A and B are in ``unchanged``
    — otherwise its score may have shifted (one side's vector changed)
    and we should let the fresh top-K pass re-derive it. The fresh
    pass will also produce (A, B) from A's perspective if B is
    unchanged and A is in the recompute set, so we don't drop real
    edges by this rule.
    """
    out: dict[tuple[str, str], float] = {}
    for nid in unchanged:
        entry = prior.nodes.get(nid, {})
        for tk in entry.get("top_k", []):
            if not tk:
                continue
            neighbor = tk[0]
            score = float(tk[1]) if len(tk) > 1 else 0.0
            if neighbor not in unchanged:
                continue
            if score < min_cosine:
                continue
            pair = (nid, neighbor) if nid < neighbor else (neighbor, nid)
            existing = out.get(pair)
            if existing is None or score > existing:
                out[pair] = score
    return out
