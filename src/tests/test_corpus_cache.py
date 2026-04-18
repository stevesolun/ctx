"""
test_corpus_cache.py -- Tests for the Phase 2 on-disk embedding cache.

Focus areas:

  - subject_id validation (rejects traversal, whitespace, empty)
  - backend key slugging (isolates backends with unsafe name chars)
  - get/put round-trip with content-hash keyed filenames
  - content-change invalidation (stale entries miss silently)
  - orphan sweep: put on new content removes the old vector file
  - invalidate removes manifest row + vector file
  - load_all / subjects / size
  - backend isolation: two caches in the same root do not cross-pollute
  - concurrent writers (ThreadPoolExecutor)
  - dtype coercion (non-float32 input is coerced, output is float32)
  - 1-D enforcement
  - manifest JSON corruption is recovered by dropping the cache
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import corpus_cache as cc  # noqa: E402


def _vec(n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=n).astype(np.float32)
    return v / (np.linalg.norm(v) or 1.0)


# ────────────────────────────────────────────────────────────────────
# Backend key slugging
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,expected",
    [
        ("sentence-transformers:all-MiniLM-L6-v2", "sentence-transformers_all-MiniLM-L6-v2"),
        ("ollama:nomic-embed-text", "ollama_nomic-embed-text"),
        ("simple-name", "simple-name"),
    ],
)
def test_slug_backend_key_replaces_unsafe_chars(name: str, expected: str) -> None:
    assert cc._slug_backend_key(name) == expected


@pytest.mark.parametrize("bad", ["", "::::", "///", "  "])
def test_slug_backend_key_rejects_empty_or_unsafe(bad: str) -> None:
    with pytest.raises(ValueError):
        cc._slug_backend_key(bad)


def test_slug_backend_key_caps_length() -> None:
    very_long = "x" * 500
    assert len(cc._slug_backend_key(very_long)) == 64


# ────────────────────────────────────────────────────────────────────
# subject_id validation
# ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "../escape",
        "has/slash",
        "has\\backslash",
        "has space",
        "-leading-dash",
        ".leading-dot",
        "a" * 200,
    ],
)
def test_get_rejects_invalid_subject_id(bad_id: str, tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    with pytest.raises(ValueError, match="invalid subject_id"):
        cache.get(bad_id, "content")


@pytest.mark.parametrize(
    "bad_id",
    ["", "../escape", "has/slash", "has\\backslash"],
)
def test_put_rejects_invalid_subject_id(bad_id: str, tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    with pytest.raises(ValueError, match="invalid subject_id"):
        cache.put(bad_id, "content", _vec(8, 0))


# ────────────────────────────────────────────────────────────────────
# Round-trip
# ────────────────────────────────────────────────────────────────────


def test_put_then_get_returns_same_vector(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    v = _vec(8, 1)
    cache.put("python-patterns", "body", v)
    got = cache.get("python-patterns", "body")
    assert got is not None
    np.testing.assert_array_equal(got, v)
    assert got.dtype == np.float32


def test_get_missing_subject_returns_none(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    assert cache.get("nobody", "anything") is None


def test_get_with_changed_content_returns_none(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("skill", "original", _vec(8, 2))
    assert cache.get("skill", "modified") is None


def test_put_replaces_vector_on_new_content(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    v1, v2 = _vec(8, 3), _vec(8, 4)
    cache.put("skill", "first", v1)
    cache.put("skill", "second", v2)
    # Old content hash should not be retrievable.
    assert cache.get("skill", "first") is None
    got = cache.get("skill", "second")
    assert got is not None
    np.testing.assert_array_equal(got, v2)


def test_put_orphan_sweep_removes_old_file(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("skill", "first", _vec(8, 5))
    # Exactly one .npy exists.
    npys = list(cache.backend_dir.glob("skill__*.npy"))
    assert len(npys) == 1
    cache.put("skill", "second", _vec(8, 6))
    npys = list(cache.backend_dir.glob("skill__*.npy"))
    assert len(npys) == 1  # old one deleted


# ────────────────────────────────────────────────────────────────────
# Metadata / manifest helpers
# ────────────────────────────────────────────────────────────────────


def test_entry_returns_metadata(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("skill", "body", _vec(16, 7))
    e = cache.entry("skill")
    assert e is not None
    assert e.dim == 16
    assert len(e.content_sha256) == 64


def test_entry_missing_returns_none(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    assert cache.entry("nobody") is None


def test_subjects_yields_sorted(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    for sid in ["charlie", "alpha", "bravo"]:
        cache.put(sid, "b", _vec(4, hash(sid) & 0xFFFF))
    assert list(cache.subjects()) == ["alpha", "bravo", "charlie"]


def test_size_counts_entries(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    assert cache.size() == 0
    cache.put("a", "x", _vec(4, 0))
    cache.put("b", "x", _vec(4, 1))
    assert cache.size() == 2


def test_load_all_returns_every_vector(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    vectors = {f"s{i}": _vec(8, i) for i in range(5)}
    for sid, v in vectors.items():
        cache.put(sid, "body-" + sid, v)
    loaded = cache.load_all()
    assert set(loaded.keys()) == set(vectors.keys())
    for sid, v in vectors.items():
        np.testing.assert_array_equal(loaded[sid], v)


def test_load_all_skips_missing_files(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("a", "body", _vec(4, 0))
    cache.put("b", "body", _vec(4, 1))
    # Manually delete b's .npy to simulate corruption.
    list(cache.backend_dir.glob("b__*.npy"))[0].unlink()
    loaded = cache.load_all()
    assert set(loaded.keys()) == {"a"}


# ────────────────────────────────────────────────────────────────────
# Invalidation
# ────────────────────────────────────────────────────────────────────


def test_invalidate_removes_entry(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("s", "body", _vec(4, 0))
    assert cache.invalidate("s") is True
    assert cache.get("s", "body") is None
    assert cache.size() == 0
    # Vector file gone too.
    assert list(cache.backend_dir.glob("s__*.npy")) == []


def test_invalidate_missing_returns_false(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    assert cache.invalidate("nothing-here") is False


# ────────────────────────────────────────────────────────────────────
# Backend isolation
# ────────────────────────────────────────────────────────────────────


def test_two_backends_do_not_share_entries(tmp_path: Path) -> None:
    st_cache = cc.CorpusCache("sentence-transformers:all-MiniLM-L6-v2", root=tmp_path)
    ollama_cache = cc.CorpusCache("ollama:nomic-embed-text", root=tmp_path)

    st_cache.put("shared-id", "body", _vec(8, 10))
    assert ollama_cache.get("shared-id", "body") is None
    assert st_cache.get("shared-id", "body") is not None

    ollama_cache.put("shared-id", "body", _vec(16, 11))
    # Both now have entries, but sizes are independent.
    assert st_cache.size() == 1
    assert ollama_cache.size() == 1
    assert st_cache.backend_dir != ollama_cache.backend_dir


# ────────────────────────────────────────────────────────────────────
# Concurrency
# ────────────────────────────────────────────────────────────────────


def test_concurrent_puts_all_land(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    N = 32

    def worker(i: int) -> None:
        cache.put(f"s{i:03d}", f"body-{i}", _vec(8, i))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(N)))

    assert cache.size() == N
    loaded = cache.load_all()
    assert len(loaded) == N
    for i in range(N):
        np.testing.assert_array_equal(loaded[f"s{i:03d}"], _vec(8, i))


# ────────────────────────────────────────────────────────────────────
# Input validation
# ────────────────────────────────────────────────────────────────────


def test_put_rejects_2d_vector(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    with pytest.raises(ValueError, match="1-D"):
        cache.put("s", "body", np.zeros((2, 4), dtype=np.float32))


def test_put_coerces_non_float32_input(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    v = np.arange(8, dtype=np.float64)
    cache.put("s", "body", v)
    got = cache.get("s", "body")
    assert got is not None
    assert got.dtype == np.float32
    np.testing.assert_array_equal(got, v.astype(np.float32))


# ────────────────────────────────────────────────────────────────────
# Corruption recovery
# ────────────────────────────────────────────────────────────────────


def test_corrupt_manifest_drops_cache_silently(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("s", "body", _vec(4, 0))
    # Clobber the manifest with garbage.
    cache.manifest_path.write_text("{{{ not json", encoding="utf-8")
    assert cache.size() == 0
    # size() reports 0 but the raw .npy is still there; a fresh put
    # must succeed without exploding.
    cache.put("s", "body-new", _vec(4, 1))
    got = cache.get("s", "body-new")
    assert got is not None


def test_manifest_non_dict_is_dropped(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache._ensure_dir()
    cache.manifest_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert cache.size() == 0


# ────────────────────────────────────────────────────────────────────
# clear()
# ────────────────────────────────────────────────────────────────────


def test_clear_wipes_backend_dir(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.put("s", "body", _vec(4, 0))
    assert cache.backend_dir.exists()
    cache.clear()
    assert not cache.backend_dir.exists()
    assert cache.size() == 0


def test_clear_on_empty_cache_is_ok(tmp_path: Path) -> None:
    cache = cc.CorpusCache("st", root=tmp_path)
    cache.clear()  # no error
