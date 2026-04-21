"""
mcp_canonical_index.py -- Canonical-key sidecar index for MCP entities.

Phase 6b: replaces the O(n) frontmatter scan in
``mcp_add._find_existing_by_github_url`` with an O(1) sidecar lookup.

Why
---
Cross-source dedup (awesome-mcp + pulsemcp cataloging the same upstream
repo under different slugs) needs a github_url → existing-entity lookup.
The Phase 3.6 implementation did ``rglob("*.md")`` + read + YAML-parse on
every add. At 42 entities it was acceptable (~20 ms); at 15k it becomes
5-10 s per add, which dominates the ingest budget.

Model
-----
The sidecar is a *cache*, not the source of truth. The filesystem wins
every tie:

- Missing index  -> fall back to scan, repair by upserting the hit.
- Stale entry (points at deleted file) -> fall back to scan, repair.
- Corrupted JSON -> treat as missing, rebuild on next upsert.

This keeps ingest resilient when the index file is accidentally deleted,
partially written (we use atomic_write_json so this should not happen,
but paranoia is cheap), or diverges from disk state after a manual edit.

Location
--------
``<wiki>/entities/mcp-servers/.canonical-index.json``

The hidden-file prefix keeps it out of the ``rglob("*.md")`` walk and out
of any wiki_query/graphify pass that only looks at entity pages.

Schema (version 1)
------------------
    {
      "version": 1,
      "updated": "2026-04-20T12:34:56Z",
      "by_github_url": {
        "https://github.com/owner/repo": {
          "slug": "owner-repo",
          "relpath": "o/owner-repo.md"
        }
      }
    }

Normalized URL keys only (lowercased, trailing-slash-stripped, github-only)
to match ``McpRecord.canonical_dedup_key``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

from _fs_utils import atomic_write_json

__all__ = [
    "INDEX_FILENAME",
    "CanonicalEntry",
    "CanonicalIndex",
    "load_index",
    "save_index",
    "lookup",
    "upsert",
    "remove",
    "rebuild_from_scan",
]

INDEX_FILENAME = ".canonical-index.json"
INDEX_VERSION = 1


class CanonicalEntry(TypedDict):
    """One mapped entity: canonical URL -> slug + shard-relative path."""

    slug: str
    relpath: str


class CanonicalIndex(TypedDict):
    """On-disk JSON shape. ``by_github_url`` keys are normalized URLs."""

    version: int
    updated: str
    by_github_url: dict[str, CanonicalEntry]


def _now_iso() -> str:
    """Return a UTC ISO-8601 timestamp with seconds precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_index() -> CanonicalIndex:
    """Return a fresh, valid, empty index structure."""
    return {"version": INDEX_VERSION, "updated": _now_iso(), "by_github_url": {}}


def _index_path(mcp_dir: Path) -> Path:
    """Return the sidecar path for a given MCP entity directory."""
    return mcp_dir / INDEX_FILENAME


def load_index(mcp_dir: Path) -> CanonicalIndex:
    """Load the sidecar index. Return an empty index on any failure.

    A missing file, corrupted JSON, schema-version mismatch, or wrong
    top-level shape all collapse to "empty index" — the caller will
    repair by upserting as it discovers entities. This is intentional:
    the index is never authoritative, so fail-open is the right default.
    """
    path = _index_path(mcp_dir)
    if not path.is_file():
        return _empty_index()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_index()
    if not isinstance(data, dict):
        return _empty_index()
    # Treat a version bump as "start over" — future schema changes
    # should provide their own migration path rather than silently
    # consuming v1 data.
    if data.get("version") != INDEX_VERSION:
        return _empty_index()
    by_url = data.get("by_github_url")
    if not isinstance(by_url, dict):
        return _empty_index()
    # Defensive: coerce each entry to the typed shape, drop malformed.
    cleaned: dict[str, CanonicalEntry] = {}
    for url, entry in by_url.items():
        if (
            isinstance(url, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("slug"), str)
            and isinstance(entry.get("relpath"), str)
        ):
            cleaned[url] = {"slug": entry["slug"], "relpath": entry["relpath"]}
    return {
        "version": INDEX_VERSION,
        "updated": str(data.get("updated", _now_iso())),
        "by_github_url": cleaned,
    }


def save_index(mcp_dir: Path, index: CanonicalIndex) -> None:
    """Write the index atomically to the sidecar path.

    Bumps ``updated`` to now. The atomic_write_json helper lays the file
    down at 0o600 via the Phase 6a chmod-before-replace hardening so the
    sidecar never leaks more permissively than the entity pages it
    indexes.
    """
    payload: CanonicalIndex = {
        "version": INDEX_VERSION,
        "updated": _now_iso(),
        "by_github_url": index["by_github_url"],
    }
    atomic_write_json(_index_path(mcp_dir), payload)


def lookup(
    mcp_dir: Path, normalized_url: str, *, index: CanonicalIndex | None = None
) -> Path | None:
    """Return the absolute path for *normalized_url* if the index has it.

    Callers that already hold the index pass it via *index* to avoid
    re-reading the file inside a hot loop. Returns ``None`` on miss OR
    when the indexed path no longer exists on disk — a stale hit is
    indistinguishable from a miss from the caller's point of view, and
    treating it as a miss triggers the scan-and-repair path.
    """
    idx = index if index is not None else load_index(mcp_dir)
    entry = idx["by_github_url"].get(normalized_url)
    if entry is None:
        return None
    candidate = mcp_dir / entry["relpath"]
    if not candidate.is_file():
        return None
    return candidate


def upsert(
    mcp_dir: Path,
    normalized_url: str,
    *,
    slug: str,
    relpath: str,
    index: CanonicalIndex | None = None,
    persist: bool = True,
) -> CanonicalIndex:
    """Insert or update a canonical mapping. Return the mutated index.

    ``persist=False`` lets batch operations (e.g. rebuild_from_scan)
    accumulate changes in memory and write once at the end, avoiding N
    disk writes for N entities.
    """
    idx = index if index is not None else load_index(mcp_dir)
    idx["by_github_url"][normalized_url] = {"slug": slug, "relpath": relpath}
    if persist:
        save_index(mcp_dir, idx)
    return idx


def remove(
    mcp_dir: Path,
    normalized_url: str,
    *,
    index: CanonicalIndex | None = None,
    persist: bool = True,
) -> CanonicalIndex:
    """Drop a mapping. No-op if absent. Return the (possibly mutated) index."""
    idx = index if index is not None else load_index(mcp_dir)
    if normalized_url in idx["by_github_url"]:
        del idx["by_github_url"][normalized_url]
        if persist:
            save_index(mcp_dir, idx)
    return idx


def rebuild_from_scan(mcp_dir: Path) -> tuple[CanonicalIndex, int, int]:
    """Scan every entity page, rebuild the index from scratch.

    Returns ``(index, indexed, skipped)`` where *indexed* counts pages
    with a parseable github_url and *skipped* counts pages that had no
    github_url or malformed frontmatter. Both together sum to the total
    ``*.md`` file count under the MCP entity tree.

    Idempotent: running twice produces an identical ``by_github_url``
    map. The ``updated`` timestamp will differ.

    Implementation note: we import mcp_add lazily to avoid a circular
    import — mcp_add itself wants to call ``lookup`` from this module.
    """
    from mcp_add import _normalize_github_url, _parse_frontmatter  # noqa: PLC0415

    index = _empty_index()
    indexed = 0
    skipped = 0

    if not mcp_dir.is_dir():
        return index, indexed, skipped

    for page in mcp_dir.rglob("*.md"):
        # Skip non-entity files that might land under the tree later.
        if page.name.startswith("."):
            skipped += 1
            continue
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue
        fm = _parse_frontmatter(text)
        normalized = _normalize_github_url(fm.get("github_url"))
        if normalized is None:
            skipped += 1
            continue
        # Prefer the filename stem over the frontmatter ``name`` field:
        # the stem is the validated filesystem-safe slug produced by
        # ``McpRecord.slug``, whereas the ``name`` field may store the
        # original upstream display name (e.g. ``1mcp/agent`` for a
        # file at ``0-9/1mcp-agent.md``).
        slug = page.stem
        relpath = page.relative_to(mcp_dir).as_posix()
        upsert(
            mcp_dir,
            normalized,
            slug=slug,
            relpath=relpath,
            index=index,
            persist=False,
        )
        indexed += 1

    save_index(mcp_dir, index)
    return index, indexed, skipped
