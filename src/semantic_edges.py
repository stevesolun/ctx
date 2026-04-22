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
]


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
    path = _cache_file(cache_dir)
    # np.savez_compressed appends ``.npz`` if the filename doesn't
    # already end in it — so we ask numpy to write to a ``.tmp`` name
    # and reconstruct the actual on-disk name (``.tmp.npz``) for the
    # atomic rename.
    tmp_base = path.with_name(path.name + ".tmp")
    np.savez_compressed(tmp_base, hashes=hashes, vecs=vecs, model=np.asarray([model_id]))
    tmp_actual = tmp_base.with_suffix(tmp_base.suffix + ".npz")
    os.replace(tmp_actual, path)


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


def _embed_missing(
    missing: list[tuple[int, str, str]],  # [(row_index, node_id, text)]
    embedder: "object",
    batch_size: int,
) -> "np.ndarray":
    """Embed the texts with unknown content hashes.

    Returns an (M, dim) matrix aligned with the input order.
    ``embedder.embed(list[str]) -> np.ndarray`` is the
    ``EmbedderProtocol`` contract from ``embedding_backend``.
    """
    import numpy as np  # noqa: PLC0415

    if not missing:
        # Callers shouldn't invoke with an empty list, but guard for robustness.
        return np.zeros((0, 0), dtype="float32")

    rows: list[np.ndarray] = []
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        texts = [t for _, _, t in batch]
        matrix = embedder.embed(texts)  # type: ignore[attr-defined]
        rows.append(np.asarray(matrix, dtype="float32"))
    return np.vstack(rows)


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
) -> dict[tuple[str, str], float]:
    """Build the semantic-similarity edge map.

    Args:
        nodes: Iterable of ``SemanticNode`` — one per entity page.
            Duplicate ``node_id`` values are rejected (a programming
            error; IDs must be unique across skills/agents/MCPs).
        top_k: Per-node neighbor cap.
        min_cosine: Minimum cosine to keep an edge.
        batch_size: Embedding batch size. 128 is a good default for
            sentence-transformers on CPU.
        cache_dir: On-disk cache root. Created if missing. Contains
            one ``embeddings.npz`` file keyed by content hash.
        backend: ``embedding_backend`` backend name. Only used to
            drive the embedder factory; the cache dedup already
            accounts for text content.
        model: Optional override of the backend default model. When
            ``None`` the backend's own default is used.

    Returns:
        ``{(node_id_low, node_id_high): cosine}`` with node IDs
        canonicalised to the lexicographically-smaller/larger order.
        Edges with cosine < ``min_cosine`` are dropped.
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

    embedder = get_embedder(backend, model=model)
    model_id = getattr(embedder, "name", f"{backend}:{model or 'default'}")

    cache = _load_cache(cache_dir, model_id)
    _logger.info(
        "semantic_edges: cache hits=%d of %d nodes (model=%s)",
        sum(1 for n in node_list if _content_hash(n.text) in cache),
        len(node_list),
        model_id,
    )

    # Build the row-aligned embedding matrix.
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
        new_vecs = _embed_missing(missing, embedder, batch_size)
        if new_vecs.size:
            dim = int(new_vecs.shape[1])
            for (row_i, _nid, text), vec in zip(missing, new_vecs, strict=True):
                row_vecs[row_i] = vec
                cache[_content_hash(text)] = vec
            _save_cache(cache_dir, model_id, cache)

    if dim <= 0:
        _logger.warning("semantic_edges: no embeddings produced (empty input?)")
        return {}

    # Any slot still None means its embedding wasn't resolved — should
    # be impossible given the logic above, but guard so we don't crash
    # on a None row in the matmul.
    for i, v in enumerate(row_vecs):
        if v is None:
            raise RuntimeError(
                f"internal: node_id={node_list[i].node_id!r} has no embedding row"
            )

    vecs = np.vstack([v for v in row_vecs if v is not None]).astype("float32")
    vecs = _l2_normalize(vecs)
    node_ids = [n.node_id for n in node_list]

    pairs = _topk_pairs(vecs, node_ids, top_k=top_k, min_cosine=min_cosine)
    _logger.info(
        "semantic_edges: produced %d semantic pairs (top_k=%d, min_cosine=%.2f)",
        len(pairs), top_k, min_cosine,
    )
    return pairs
