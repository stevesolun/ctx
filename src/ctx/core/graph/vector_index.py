"""Persistent vector indexes for graph semantic attach.

The default backend is exact cosine over normalized NumPy arrays. Optional
HNSW support is loaded only when requested so default installs stay portable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import _replace_with_retry, atomic_write_json

_INDEX_VERSION = 1
_META_NAME = "vector-index.meta.json"
_NUMPY_NAME = "vector-index.numpy.npz"
_HNSW_NAME = "vector-index.hnsw.bin"


class VectorIndexUnavailable(RuntimeError):
    """Raised when a requested vector backend is not installed."""


@dataclass(frozen=True)
class Neighbor:
    node_id: str
    score: float


@dataclass(frozen=True)
class VectorIndexMeta:
    version: int
    index_kind: str
    metric: str
    model_id: str
    dim: int
    dtype: str
    normalized: bool
    node_count: int
    node_ids_sha256: str
    content_hashes_sha256: str
    content_fingerprint: str
    created_at: str


class NumpyFlatVectorIndex:
    def __init__(
        self,
        *,
        meta: VectorIndexMeta,
        node_ids: list[str],
        content_hashes: list[str],
        vectors: np.ndarray,
    ) -> None:
        self.meta = meta
        self.node_ids = node_ids
        self.content_hashes = content_hashes
        self.vectors = _normalize(vectors)
        self._row_by_node = {node_id: index for index, node_id in enumerate(node_ids)}

    def query(
        self,
        vectors: np.ndarray,
        *,
        top_k: int,
        min_score: float,
        exclude_node_ids: set[str] | None = None,
    ) -> list[list[Neighbor]]:
        queries = _normalize_query_vectors(vectors, expected_dim=self.meta.dim)
        if top_k <= 0 or self.vectors.size == 0:
            return [[] for _ in range(len(queries))]
        scores = queries @ self.vectors.T
        if exclude_node_ids:
            for node_id in exclude_node_ids:
                row = self._row_by_node.get(node_id)
                if row is not None:
                    scores[:, row] = -np.inf
        rows: list[list[Neighbor]] = []
        limit = min(top_k, len(self.node_ids))
        for query_scores in scores:
            if limit == len(self.node_ids):
                candidate_idx = np.argsort(-query_scores)
            else:
                candidate_idx = np.argpartition(-query_scores, limit - 1)[:limit]
                candidate_idx = candidate_idx[np.argsort(-query_scores[candidate_idx])]
            neighbors = [
                Neighbor(self.node_ids[int(index)], float(query_scores[int(index)]))
                for index in candidate_idx
                if float(query_scores[int(index)]) >= min_score
            ]
            rows.append(neighbors[:top_k])
        return rows

    def save(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        meta_path = cache_dir / _META_NAME
        with file_lock(meta_path):
            _save_numpy_data(cache_dir / _NUMPY_NAME, self)
            atomic_write_json(meta_path, asdict(self.meta))


class HnswlibVectorIndex(NumpyFlatVectorIndex):
    def __init__(
        self,
        *,
        meta: VectorIndexMeta,
        node_ids: list[str],
        content_hashes: list[str],
        vectors: np.ndarray,
        hnsw_index: Any,
    ) -> None:
        super().__init__(
            meta=meta,
            node_ids=node_ids,
            content_hashes=content_hashes,
            vectors=vectors,
        )
        self._hnsw_index = hnsw_index

    def query(
        self,
        vectors: np.ndarray,
        *,
        top_k: int,
        min_score: float,
        exclude_node_ids: set[str] | None = None,
    ) -> list[list[Neighbor]]:
        queries = _normalize_query_vectors(vectors, expected_dim=self.meta.dim)
        if top_k <= 0 or self.vectors.size == 0:
            return [[] for _ in range(len(queries))]
        extra = len(exclude_node_ids or ())
        k = min(len(self.node_ids), top_k + extra)
        labels, distances = self._hnsw_index.knn_query(queries, k=k)
        excluded = exclude_node_ids or set()
        rows: list[list[Neighbor]] = []
        for row_labels, row_distances in zip(labels, distances, strict=True):
            neighbors: list[Neighbor] = []
            for label, distance in zip(row_labels, row_distances, strict=True):
                node_id = self.node_ids[int(label)]
                if node_id in excluded:
                    continue
                score = 1.0 - float(distance)
                if score >= min_score:
                    neighbors.append(Neighbor(node_id, score))
                if len(neighbors) >= top_k:
                    break
            rows.append(neighbors)
        return rows

    def save(self, cache_dir: Path) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        meta_path = cache_dir / _META_NAME
        with file_lock(meta_path):
            _save_numpy_data(cache_dir / _NUMPY_NAME, self)
            tmp = cache_dir / f".{_HNSW_NAME}.tmp"
            self._hnsw_index.save_index(str(tmp))
            _replace_with_retry(str(tmp), cache_dir / _HNSW_NAME)
            atomic_write_json(meta_path, asdict(self.meta))


class MergedVectorIndex:
    """Query several compatible vector indexes as one logical index.

    This is the base+delta primitive: a release can ship an immutable base
    vector index while local entity upserts append a small delta index. Query
    callers get one merged top-k result without rebuilding the base.
    """

    def __init__(self, indexes: list[NumpyFlatVectorIndex]) -> None:
        if not indexes:
            raise ValueError("at least one vector index is required")
        first = indexes[0].meta
        for index in indexes[1:]:
            if (
                index.meta.metric != first.metric
                or index.meta.model_id != first.model_id
                or index.meta.dim != first.dim
                or index.meta.normalized != first.normalized
            ):
                raise ValueError("vector indexes are incompatible")
        self.meta = first
        self.indexes = list(indexes)

    def query(
        self,
        vectors: np.ndarray,
        *,
        top_k: int,
        min_score: float,
        exclude_node_ids: set[str] | None = None,
    ) -> list[list[Neighbor]]:
        queries = _normalize_query_vectors(vectors, expected_dim=self.meta.dim)
        if top_k <= 0:
            return [[] for _ in range(len(queries))]
        merged_rows = [dict[str, float]() for _ in range(len(queries))]
        for index in self.indexes:
            rows = index.query(
                queries,
                top_k=top_k,
                min_score=min_score,
                exclude_node_ids=exclude_node_ids,
            )
            for row_index, neighbors in enumerate(rows):
                merged = merged_rows[row_index]
                for neighbor in neighbors:
                    previous = merged.get(neighbor.node_id)
                    if previous is None or neighbor.score > previous:
                        merged[neighbor.node_id] = neighbor.score
        return [
            [
                Neighbor(node_id, score)
                for node_id, score in sorted(
                    row.items(),
                    key=lambda item: (-item[1], item[0]),
                )[:top_k]
            ]
            for row in merged_rows
        ]


def build_vector_index(
    *,
    kind: str,
    model_id: str,
    node_ids: list[str],
    content_hashes: list[str],
    vectors: np.ndarray,
    ann_enabled_above_nodes: int = 250_000,
) -> NumpyFlatVectorIndex:
    _validate_inputs(node_ids, content_hashes, vectors)
    normalized = _normalize(vectors)
    selected_kind = _select_index_kind(
        kind,
        node_count=len(node_ids),
        ann_enabled_above_nodes=ann_enabled_above_nodes,
    )
    meta = _make_meta(
        index_kind=selected_kind,
        model_id=model_id,
        node_ids=node_ids,
        content_hashes=content_hashes,
        vectors=normalized,
    )
    if selected_kind == "hnswlib":
        return _build_hnswlib_index(
            meta=meta,
            node_ids=node_ids,
            content_hashes=content_hashes,
            vectors=normalized,
        )
    return NumpyFlatVectorIndex(
        meta=meta,
        node_ids=list(node_ids),
        content_hashes=list(content_hashes),
        vectors=normalized,
    )


def load_vector_index(
    cache_dir: Path,
    *,
    expected_model_id: str,
    expected_content_fingerprint: str,
) -> NumpyFlatVectorIndex | None:
    meta_path = cache_dir / _META_NAME
    try:
        with file_lock(meta_path):
            meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
            meta = VectorIndexMeta(**meta_raw)
            if (
                meta.version != _INDEX_VERSION
                or meta.metric != "cosine"
                or meta.model_id != expected_model_id
                or meta.content_fingerprint != expected_content_fingerprint
            ):
                return None
            node_ids, content_hashes, vectors = _load_numpy_data(cache_dir / _NUMPY_NAME)
            if (
                meta.node_count != len(node_ids)
                or vectors.ndim != 2
                or meta.dim != int(vectors.shape[1])
                or meta.node_ids_sha256 != _hash_list(node_ids)
                or meta.content_hashes_sha256 != _hash_list(content_hashes)
            ):
                return None
            if meta.index_kind == "hnswlib":
                hnsw = _load_hnswlib_index(cache_dir / _HNSW_NAME, meta)
                if hnsw is None:
                    return None
                return HnswlibVectorIndex(
                    meta=meta,
                    node_ids=node_ids,
                    content_hashes=content_hashes,
                    vectors=vectors,
                    hnsw_index=hnsw,
                )
            if meta.index_kind == "numpy-flat":
                return NumpyFlatVectorIndex(
                    meta=meta,
                    node_ids=node_ids,
                    content_hashes=content_hashes,
                    vectors=vectors,
                )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def upsert_numpy_flat_index_entry(
    cache_dir: Path,
    *,
    model_id: str,
    node_id: str,
    content_hash: str,
    vector: np.ndarray,
) -> NumpyFlatVectorIndex:
    """Create or update one row in a small portable delta vector index."""
    if not model_id:
        raise ValueError("model_id must be non-empty")
    if not node_id:
        raise ValueError("node_id must be non-empty")
    if not content_hash:
        raise ValueError("content_hash must be non-empty")
    vector_row = _single_vector_row(vector)
    cache_dir = Path(cache_dir)
    upsert_lock = cache_dir / ".vector-index-upsert"
    with file_lock(upsert_lock):
        existing = _load_existing_delta_index(cache_dir, model_id=model_id)
        if existing is None:
            node_ids: list[str] = []
            content_hashes: list[str] = []
            vectors = np.empty((0, vector_row.shape[1]), dtype=np.float32)
        else:
            if existing.meta.dim != int(vector_row.shape[1]):
                raise ValueError(
                    f"vector dim {vector_row.shape[1]} does not match existing "
                    f"index dim {existing.meta.dim}"
                )
            node_ids = list(existing.node_ids)
            content_hashes = list(existing.content_hashes)
            vectors = np.asarray(existing.vectors, dtype=np.float32).copy()

        if node_id in node_ids:
            row_index = node_ids.index(node_id)
            content_hashes[row_index] = content_hash
            vectors[row_index] = vector_row[0]
        else:
            node_ids.append(node_id)
            content_hashes.append(content_hash)
            vectors = np.vstack([vectors, vector_row])

        index = build_vector_index(
            kind="numpy-flat",
            model_id=model_id,
            node_ids=node_ids,
            content_hashes=content_hashes,
            vectors=vectors,
        )
        index.save(cache_dir)
        return index


def _load_existing_delta_index(
    cache_dir: Path,
    *,
    model_id: str,
) -> NumpyFlatVectorIndex | None:
    meta_path = cache_dir / _META_NAME
    if not meta_path.is_file():
        return None
    try:
        meta_raw = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = VectorIndexMeta(**meta_raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"existing vector index metadata is unreadable: {exc}") from exc
    index = load_vector_index(
        cache_dir,
        expected_model_id=model_id,
        expected_content_fingerprint=meta.content_fingerprint,
    )
    if index is None:
        raise ValueError("existing vector index is incompatible or unreadable")
    return index


def content_fingerprint(node_ids: list[str], content_hashes: list[str]) -> str:
    payload = "\n".join(
        f"{node_id}\t{content_hash}"
        for node_id, content_hash in zip(node_ids, content_hashes, strict=True)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_meta(
    *,
    index_kind: str,
    model_id: str,
    node_ids: list[str],
    content_hashes: list[str],
    vectors: np.ndarray,
) -> VectorIndexMeta:
    return VectorIndexMeta(
        version=_INDEX_VERSION,
        index_kind=index_kind,
        metric="cosine",
        model_id=model_id,
        dim=int(vectors.shape[1]),
        dtype="float32",
        normalized=True,
        node_count=len(node_ids),
        node_ids_sha256=_hash_list(node_ids),
        content_hashes_sha256=_hash_list(content_hashes),
        content_fingerprint=content_fingerprint(node_ids, content_hashes),
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def _select_index_kind(
    kind: str,
    *,
    node_count: int,
    ann_enabled_above_nodes: int,
) -> str:
    if kind == "numpy-flat":
        return "numpy-flat"
    if kind == "hnswlib":
        if _import_hnswlib() is None:
            raise VectorIndexUnavailable("hnswlib optional dependency is not installed")
        return "hnswlib"
    if kind == "auto":
        if node_count >= ann_enabled_above_nodes and _import_hnswlib() is not None:
            return "hnswlib"
        return "numpy-flat"
    raise ValueError(f"unknown vector index kind: {kind}")


def _build_hnswlib_index(
    *,
    meta: VectorIndexMeta,
    node_ids: list[str],
    content_hashes: list[str],
    vectors: np.ndarray,
) -> HnswlibVectorIndex:
    hnswlib = _import_hnswlib()
    if hnswlib is None:
        raise VectorIndexUnavailable("hnswlib optional dependency is not installed")
    index = hnswlib.Index(space="cosine", dim=meta.dim)
    index.init_index(max_elements=len(node_ids), ef_construction=200, M=16)
    index.add_items(vectors, np.arange(len(node_ids), dtype=np.int64))
    index.set_ef(64)
    return HnswlibVectorIndex(
        meta=meta,
        node_ids=list(node_ids),
        content_hashes=list(content_hashes),
        vectors=vectors,
        hnsw_index=index,
    )


def _load_hnswlib_index(path: Path, meta: VectorIndexMeta) -> Any | None:
    hnswlib = _import_hnswlib()
    if hnswlib is None or not path.is_file():
        return None
    index = hnswlib.Index(space="cosine", dim=meta.dim)
    index.load_index(str(path), max_elements=meta.node_count)
    index.set_ef(64)
    return index


def _import_hnswlib() -> Any | None:
    try:
        import hnswlib  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        return None
    return hnswlib


def _save_numpy_data(path: Path, index: NumpyFlatVectorIndex) -> None:
    tmp_stem = path.with_name("vector-index.numpy.tmp")
    np.savez_compressed(
        tmp_stem,
        node_ids=np.asarray(index.node_ids, dtype="U"),
        content_hashes=np.asarray(index.content_hashes, dtype="U"),
        vecs=index.vectors.astype("float32"),
    )
    tmp_real = tmp_stem.with_name(tmp_stem.name + ".npz")
    _replace_with_retry(str(tmp_real), path)


def _load_numpy_data(path: Path) -> tuple[list[str], list[str], np.ndarray]:
    with np.load(path, allow_pickle=False) as data:
        node_ids = [str(value) for value in data["node_ids"].tolist()]
        content_hashes = [str(value) for value in data["content_hashes"].tolist()]
        vectors = np.asarray(data["vecs"], dtype=np.float32)
    return node_ids, content_hashes, vectors


def _validate_inputs(
    node_ids: list[str],
    content_hashes: list[str],
    vectors: np.ndarray,
) -> None:
    if len(node_ids) != len(content_hashes):
        raise ValueError("node_ids and content_hashes must have the same length")
    if len(set(node_ids)) != len(node_ids):
        raise ValueError("duplicate node_id in vector index")
    if vectors.ndim != 2:
        raise ValueError("vectors must be a 2D array")
    if vectors.shape[0] != len(node_ids):
        raise ValueError("vectors row count must match node_ids")


def _single_vector_row(vector: np.ndarray) -> np.ndarray:
    row = np.asarray(vector, dtype=np.float32)
    if row.ndim == 1:
        row = row.reshape(1, -1)
    if row.ndim != 2 or row.shape[0] != 1 or row.shape[1] <= 0:
        raise ValueError("vector must be a single non-empty row")
    return row


def _normalize(vectors: np.ndarray) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError("vectors must be a 2D array")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (matrix / norms).astype(np.float32)


def _normalize_query_vectors(vectors: np.ndarray, *, expected_dim: int) -> np.ndarray:
    queries = _normalize(vectors)
    actual_dim = int(queries.shape[1])
    if actual_dim != expected_dim:
        raise ValueError(f"query vector dim {actual_dim} does not match index dim {expected_dim}")
    return queries


def _hash_list(values: list[str]) -> str:
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()
