#!/usr/bin/env python3
"""
corpus_cache.py -- On-disk cache of embedding vectors per skill/agent.

The intake similarity check needs an embedding per existing subject
to rank a candidate against. Re-embedding the whole corpus on every
``skill_add`` is wasteful; this module caches each embedding keyed
by (subject_id, content_sha256). Stale entries (body changed) miss
silently and are re-computed on next access.

Layout::

    <root>/
        <backend_key>/
            _manifest.json                         metadata only
            <subject_id>__<sha16>.npy              raw float32 vector

``backend_key`` is derived from the embedder's ``name``. Switching
embedders therefore lands in a separate directory and never mixes
dimensions (ST=384 vs Ollama=768).

Race-freeness: the content hash is encoded in the filename, not
just the manifest. A concurrent ``put`` cannot replace a file under
a reader — the new vector lands at a different path.

Security:
  - ``subject_id`` is validated against a strict regex (prevents
    path traversal via crafted IDs like ``../evil``).
  - Vector files are written atomically (``os.replace`` from a
    sibling tempfile).
  - Manifest writes are serialised via ``_file_lock.file_lock`` so
    concurrent processes don't clobber each other's entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from _file_lock import file_lock  # noqa: E402


DEFAULT_CACHE_ROOT = Path(os.path.expanduser("~/.claude/skills/_embeddings"))

# Same name policy as skill_telemetry: alnum start, alnum/_/-/. inside,
# bounded length. Prevents path-traversal and whitespace injection
# via crafted skill/agent IDs.
_SUBJECT_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,127}$")

# Backend names like "sentence-transformers:all-MiniLM-L6-v2" contain
# characters (":", "/") that are illegal on Windows, so we slugify
# before using the name as a directory component.
_BACKEND_UNSAFE_RE = re.compile(r"[^a-zA-Z0-9_\-\.]")

# 64 bits of content hash in the filename. Within a single subject_id
# the inputs are always distinct strings (we only write on content
# change), so collision probability is effectively zero.
_SHA_FILENAME_LEN = 16


def _slug_backend_key(backend_name: str) -> str:
    """Turn an ``Embedder.name`` into a filesystem-safe directory name."""
    if not isinstance(backend_name, str) or not backend_name:
        raise ValueError("backend_name must be a non-empty string")
    slug = _BACKEND_UNSAFE_RE.sub("_", backend_name).strip("_")
    if not slug or not slug[0].isalnum():
        raise ValueError(f"backend name yields unsafe slug: {backend_name!r}")
    return slug[:64]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CacheEntry:
    """Manifest row for one cached vector."""

    content_sha256: str
    dim: int


class CorpusCache:
    """Per-subject embedding cache.

    Instances are cheap — they only touch disk on ``get``/``put``/
    ``invalidate``. Safe under concurrent writers within a process
    and across processes via ``file_lock`` on the manifest.
    """

    def __init__(self, backend_name: str, *, root: Path | None = None) -> None:
        self._root = (root or DEFAULT_CACHE_ROOT).expanduser()
        self._backend_dir = self._root / _slug_backend_key(backend_name)
        self._manifest_path = self._backend_dir / "_manifest.json"

    @property
    def backend_dir(self) -> Path:
        return self._backend_dir

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    def _ensure_dir(self) -> None:
        self._backend_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_subject_id(subject_id: str) -> None:
        if not isinstance(subject_id, str) or not _SUBJECT_ID_RE.match(subject_id):
            raise ValueError(f"invalid subject_id: {subject_id!r}")

    def _vector_path(self, subject_id: str, content_sha: str) -> Path:
        # Content hash is part of the filename so readers and writers
        # never contend on the same path. Old files become orphans
        # and are swept on the next ``put`` for that subject.
        return self._backend_dir / f"{subject_id}__{content_sha[:_SHA_FILENAME_LEN]}.npy"

    def _read_manifest(self) -> dict[str, dict[str, object]]:
        try:
            with open(self._manifest_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            # Manifest corruption drops the entire cache metadata —
            # we re-embed rather than serving wrong vectors. Vector
            # files on disk become unreachable and will be cleaned
            # up by the next ``put`` for each subject.
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _write_manifest(self, data: dict[str, dict[str, object]]) -> None:
        self._ensure_dir()
        # Atomic write via tempfile + os.replace so a crash mid-write
        # never leaves a partial manifest.
        fd, tmp = tempfile.mkstemp(
            prefix="_manifest.", suffix=".json.tmp", dir=self._backend_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=2)
            os.replace(tmp, self._manifest_path)
        except Exception:
            try:
                Path(tmp).unlink()
            except FileNotFoundError:
                pass
            raise

    def get(self, subject_id: str, content: str) -> np.ndarray | None:
        """Return the cached vector iff the content hash matches.

        Reads the vector file directly by content-hash-encoded path.
        No manifest round-trip — a concurrent ``put`` for the same
        subject lands at a different filename and cannot poison the
        read.
        """
        self._validate_subject_id(subject_id)
        path = self._vector_path(subject_id, _sha256_text(content))
        try:
            vec = np.load(path, allow_pickle=False)
        except (FileNotFoundError, ValueError, OSError):
            return None
        if vec.ndim != 1:
            return None
        if vec.dtype != np.float32:
            vec = vec.astype(np.float32, copy=False)
        return vec

    def put(self, subject_id: str, content: str, vector: np.ndarray) -> None:
        """Store ``vector`` for ``subject_id`` keyed by content hash.

        Writes the vector to a content-addressed filename, then
        updates the manifest inside a file lock. Prior vector files
        for the same subject are removed (orphan sweep).
        """
        self._validate_subject_id(subject_id)
        if not isinstance(vector, np.ndarray) or vector.ndim != 1:
            raise ValueError(f"vector must be a 1-D numpy array, got {vector!r}")
        vec = np.ascontiguousarray(vector, dtype=np.float32)
        sha = _sha256_text(content)

        self._ensure_dir()
        vec_path = self._vector_path(subject_id, sha)

        # Atomic write of the vector: mkstemp in the target directory,
        # np.save, os.replace. Suffix is ``.npy`` so np.save does not
        # append a second extension.
        fd, tmp = tempfile.mkstemp(prefix=".", suffix=".npy", dir=self._backend_dir)
        try:
            os.close(fd)
            np.save(tmp, vec, allow_pickle=False)
            os.replace(tmp, vec_path)
        except Exception:
            try:
                Path(tmp).unlink()
            except FileNotFoundError:
                pass
            raise

        # Update manifest + sweep any previous file for this subject.
        with file_lock(self._manifest_path):
            manifest = self._read_manifest()
            prev = manifest.get(subject_id)
            prev_sha = prev.get("content_sha256") if isinstance(prev, dict) else None
            manifest[subject_id] = {
                "content_sha256": sha,
                "dim": int(vec.shape[0]),
            }
            self._write_manifest(manifest)
            if isinstance(prev_sha, str) and prev_sha != sha:
                old = self._vector_path(subject_id, prev_sha)
                try:
                    old.unlink()
                except FileNotFoundError:
                    pass

    def invalidate(self, subject_id: str) -> bool:
        """Remove an entry. Returns True iff something was removed."""
        self._validate_subject_id(subject_id)
        removed = False
        with file_lock(self._manifest_path):
            manifest = self._read_manifest()
            entry = manifest.pop(subject_id, None)
            if entry is not None:
                self._write_manifest(manifest)
                removed = True
                sha = entry.get("content_sha256") if isinstance(entry, dict) else None
                if isinstance(sha, str):
                    try:
                        self._vector_path(subject_id, sha).unlink()
                    except FileNotFoundError:
                        pass
        return removed

    def entry(self, subject_id: str) -> CacheEntry | None:
        """Return the manifest entry for ``subject_id`` if present."""
        self._validate_subject_id(subject_id)
        data = self._read_manifest().get(subject_id)
        if not isinstance(data, dict):
            return None
        sha = data.get("content_sha256")
        dim = data.get("dim")
        if not isinstance(sha, str) or not isinstance(dim, int):
            return None
        return CacheEntry(content_sha256=sha, dim=dim)

    def subjects(self) -> Iterator[str]:
        """Yield subject IDs currently in the manifest."""
        return iter(sorted(self._read_manifest().keys()))

    def load_all(self) -> dict[str, np.ndarray]:
        """Load every cached vector. Skips any whose ``.npy`` is missing.

        Returns a plain ``dict`` — callers stacking into a matrix
        should order keys explicitly before ``np.vstack`` to keep
        row order stable.
        """
        out: dict[str, np.ndarray] = {}
        manifest = self._read_manifest()
        for sid, meta in manifest.items():
            if not isinstance(meta, dict):
                continue
            sha = meta.get("content_sha256")
            if not isinstance(sha, str):
                continue
            try:
                vec = np.load(
                    self._vector_path(sid, sha), allow_pickle=False
                )
            except (FileNotFoundError, ValueError, OSError):
                continue
            if vec.ndim != 1:
                continue
            if vec.dtype != np.float32:
                vec = vec.astype(np.float32, copy=False)
            out[sid] = vec
        return out

    def size(self) -> int:
        return len(self._read_manifest())

    def clear(self) -> None:
        """Wipe this backend's cache directory. Destructive by design.

        Exposed because users who switch embedding models may want to
        reclaim disk; the caller must be explicit.
        """
        if self._backend_dir.exists():
            shutil.rmtree(self._backend_dir)
