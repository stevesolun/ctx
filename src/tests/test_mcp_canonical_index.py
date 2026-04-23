"""
tests/test_mcp_canonical_index.py -- Sidecar-index tests for Phase 6b.

Contracts pinned:
  - load_index returns an empty-but-valid structure when the sidecar is
    missing, corrupt, wrong schema version, or has the wrong shape.
  - save_index lays the file down atomically and lookup round-trips.
  - lookup returns None when the indexed path no longer exists on disk
    (stale entries read as misses so callers trigger scan-and-repair).
  - upsert with persist=False accumulates in memory without writing.
  - remove is no-op when key absent.
  - rebuild_from_scan walks *.md files, honours normalized URLs, and
    skips pages without a github_url.
  - The mcp_add dedup path uses the index cache then falls back to a
    disk scan that repairs the cache (miss), or drops the stale entry
    (hit pointing at nothing).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_canonical_index as mci  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────


def _write_entity(
    mcp_dir: Path,
    shard: str,
    slug: str,
    *,
    github_url: str | None = None,
    description: str = "test entity",
) -> Path:
    """Create a minimal MCP entity page under ``mcp_dir/<shard>/<slug>.md``."""
    page = mcp_dir / shard / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {slug}",
        f"description: {description}",
    ]
    if github_url is not None:
        lines.append(f"github_url: {github_url}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {slug}")
    page.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return page


# ── load_index ───────────────────────────────────────────────────────────────


class TestLoadIndex:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        idx = mci.load_index(tmp_path)
        assert idx["version"] == mci.INDEX_VERSION
        assert idx["by_github_url"] == {}

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / mci.INDEX_FILENAME).write_text("{not json", encoding="utf-8")
        idx = mci.load_index(tmp_path)
        assert idx["by_github_url"] == {}

    def test_wrong_version_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / mci.INDEX_FILENAME).write_text(
            json.dumps({"version": 99, "updated": "x", "by_github_url": {"u": {"slug": "s", "relpath": "p"}}}),
            encoding="utf-8",
        )
        idx = mci.load_index(tmp_path)
        assert idx["by_github_url"] == {}

    def test_wrong_top_level_shape_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / mci.INDEX_FILENAME).write_text("[]", encoding="utf-8")
        idx = mci.load_index(tmp_path)
        assert idx["by_github_url"] == {}

    def test_drops_malformed_entries(self, tmp_path: Path) -> None:
        (tmp_path / mci.INDEX_FILENAME).write_text(
            json.dumps(
                {
                    "version": mci.INDEX_VERSION,
                    "updated": "2026-04-20T00:00:00Z",
                    "by_github_url": {
                        "https://github.com/a/b": {"slug": "a-b", "relpath": "a/a-b.md"},
                        "https://github.com/c/d": "not an object",
                        "https://github.com/e/f": {"slug": 123, "relpath": "e/e-f.md"},
                        "https://github.com/g/h": {"slug": "g-h"},  # missing relpath
                    },
                }
            ),
            encoding="utf-8",
        )
        idx = mci.load_index(tmp_path)
        assert list(idx["by_github_url"].keys()) == ["https://github.com/a/b"]


# ── save_index / upsert / lookup ────────────────────────────────────────────


class TestSaveLookup:
    def test_upsert_persists_and_is_readable(self, tmp_path: Path) -> None:
        mci.upsert(
            tmp_path,
            "https://github.com/foo/bar",
            slug="foo-bar",
            relpath="f/foo-bar.md",
        )
        # Create the file the index claims exists so lookup confirms it.
        _write_entity(tmp_path, "f", "foo-bar")

        path = mci.lookup(tmp_path, "https://github.com/foo/bar")
        assert path is not None
        assert path.name == "foo-bar.md"

    def test_lookup_miss_returns_none(self, tmp_path: Path) -> None:
        assert mci.lookup(tmp_path, "https://github.com/nope/nope") is None

    def test_lookup_stale_entry_returns_none(self, tmp_path: Path) -> None:
        # Entry in the index but no file on disk -> caller treats as miss.
        mci.upsert(
            tmp_path,
            "https://github.com/ghost/repo",
            slug="ghost-repo",
            relpath="g/ghost-repo.md",
        )
        assert mci.lookup(tmp_path, "https://github.com/ghost/repo") is None

    def test_upsert_no_persist_defers_write(self, tmp_path: Path) -> None:
        idx = mci._empty_index()
        mci.upsert(
            tmp_path,
            "https://github.com/a/b",
            slug="a-b",
            relpath="a/a-b.md",
            index=idx,
            persist=False,
        )
        # In-memory index holds the entry…
        assert "https://github.com/a/b" in idx["by_github_url"]
        # …but nothing was written to disk yet.
        assert not (tmp_path / mci.INDEX_FILENAME).exists()

    def test_sidecar_file_is_hidden_filename(self, tmp_path: Path) -> None:
        mci.upsert(
            tmp_path,
            "https://github.com/x/y",
            slug="x-y",
            relpath="x/x-y.md",
        )
        assert (tmp_path / ".canonical-index.json").is_file()


# ── remove ───────────────────────────────────────────────────────────────────


class TestRemove:
    def test_remove_existing(self, tmp_path: Path) -> None:
        mci.upsert(tmp_path, "https://github.com/a/b", slug="a-b", relpath="a/a-b.md")
        mci.remove(tmp_path, "https://github.com/a/b")
        idx = mci.load_index(tmp_path)
        assert idx["by_github_url"] == {}

    def test_remove_missing_is_noop(self, tmp_path: Path) -> None:
        # Must not raise.
        mci.remove(tmp_path, "https://github.com/not/there")


# ── rebuild_from_scan ────────────────────────────────────────────────────────


class TestRebuild:
    def test_rebuilds_from_entities(self, tmp_path: Path) -> None:
        _write_entity(tmp_path, "a", "alpha", github_url="https://github.com/org/Alpha")
        _write_entity(tmp_path, "b", "beta", github_url="https://github.com/Org/Beta")
        _write_entity(tmp_path, "c", "gamma")  # no github_url -> skipped

        idx, indexed, skipped = mci.rebuild_from_scan(tmp_path)

        assert indexed == 2
        assert skipped == 1
        # URLs are normalized (lowercased)
        assert "https://github.com/org/alpha" in idx["by_github_url"]
        assert "https://github.com/org/beta" in idx["by_github_url"]
        alpha = idx["by_github_url"]["https://github.com/org/alpha"]
        assert alpha["slug"] == "alpha"
        assert alpha["relpath"].endswith("alpha.md")

    def test_rebuild_is_idempotent(self, tmp_path: Path) -> None:
        _write_entity(tmp_path, "a", "alpha", github_url="https://github.com/org/alpha")

        idx1, _, _ = mci.rebuild_from_scan(tmp_path)
        idx2, _, _ = mci.rebuild_from_scan(tmp_path)
        assert idx1["by_github_url"] == idx2["by_github_url"]

    def test_rebuild_skips_sidecar_itself(self, tmp_path: Path) -> None:
        # A prior rebuild leaves the sidecar in place; the next rebuild
        # must not regress by trying to parse the sidecar as an entity.
        # (The walker already filters hidden-prefixed names and non-.md
        # files; this test pins the behaviour.)
        _write_entity(tmp_path, "a", "alpha", github_url="https://github.com/org/alpha")
        mci.rebuild_from_scan(tmp_path)
        # Second rebuild must match the first.
        idx, indexed, _ = mci.rebuild_from_scan(tmp_path)
        assert indexed == 1

    def test_rebuild_missing_dir_returns_empty(self, tmp_path: Path) -> None:
        idx, indexed, skipped = mci.rebuild_from_scan(tmp_path / "does-not-exist")
        assert indexed == 0
        assert skipped == 0
        assert idx["by_github_url"] == {}


# ── Integration: mcp_add._find_existing_by_github_url with cache ────────────


class TestMcpAddIntegration:
    """Exercise the cached dedup path through mcp_add's public helper."""

    def _import_find(self) -> object:
        # Lazy import to keep this test file loadable when mcp_add's
        # heavier deps (yaml, wiki_sync) have a transient issue.
        from mcp_add import _find_existing_by_github_url  # noqa: PLC0415
        return _find_existing_by_github_url

    def test_cache_hit_with_file_present(self, tmp_path: Path) -> None:
        find = self._import_find()
        _write_entity(
            tmp_path, "f", "foo-bar",
            github_url="https://github.com/foo/bar",
        )
        # Prime the index.
        mci.upsert(
            tmp_path, "https://github.com/foo/bar",
            slug="foo-bar", relpath="f/foo-bar.md",
        )
        result = find(tmp_path, "https://github.com/foo/bar")
        assert result is not None
        assert result.name == "foo-bar.md"

    def test_cache_miss_repairs_on_scan_hit(self, tmp_path: Path) -> None:
        find = self._import_find()
        _write_entity(
            tmp_path, "f", "foo-bar",
            github_url="https://github.com/foo/bar",
        )
        # Index empty — first call must scan, find it, and upsert.
        result = find(tmp_path, "https://github.com/foo/bar")
        assert result is not None

        idx = mci.load_index(tmp_path)
        assert "https://github.com/foo/bar" in idx["by_github_url"]

    def test_stale_cache_drops_entry(self, tmp_path: Path) -> None:
        find = self._import_find()
        # Index claims there's an entity at ghost/repo but nothing exists.
        mci.upsert(
            tmp_path, "https://github.com/ghost/repo",
            slug="ghost-repo", relpath="g/ghost-repo.md",
        )
        result = find(tmp_path, "https://github.com/ghost/repo")
        assert result is None

        # Stale entry must be removed.
        idx = mci.load_index(tmp_path)
        assert "https://github.com/ghost/repo" not in idx["by_github_url"]

    def test_no_github_url_returns_none(self, tmp_path: Path) -> None:
        find = self._import_find()
        assert find(tmp_path, None) is None
        assert find(tmp_path, "") is None
        # Non-github URLs are not dedupable.
        assert find(tmp_path, "https://example.com/not-github") is None

    def test_case_insensitive_match(self, tmp_path: Path) -> None:
        find = self._import_find()
        _write_entity(
            tmp_path, "f", "foo-bar",
            github_url="https://github.com/Foo/Bar",
        )
        # Normalized lookup with different casing still finds it.
        result = find(tmp_path, "https://GitHub.com/foo/bar/")
        assert result is not None


# ── Sidecar permissions (POSIX only) ─────────────────────────────────────────


_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filesystems don't honour unix permission bits",
)


@_POSIX_ONLY
def test_sidecar_file_is_0o600(tmp_path: Path) -> None:
    import os
    import stat
    mci.upsert(tmp_path, "https://github.com/a/b", slug="a-b", relpath="a/a-b.md")
    mode = stat.S_IMODE(os.stat(tmp_path / mci.INDEX_FILENAME).st_mode)
    assert mode == 0o600, f"sidecar must be owner-only; got {oct(mode)}"


# ── H-2 regression: relpath traversal via poisoned index ────────────────────


class TestRelpathTraversalRegression:
    """Security-auditor finding H-2.

    Before the fix, load_index accepted any string for ``relpath``. A
    poisoned ``.canonical-index.json`` with
      ``{"by_github_url": {"https://g/a/b": {"slug": "x", "relpath":
        "../../../../hooks/backup_on_change.py"}}}``
    would cause ``lookup`` to return a path OUTSIDE mcp_dir, which
    ``mcp_add._rewrite_frontmatter`` would then overwrite via
    ``atomic_write_text`` — an arbitrary-file-write primitive reachable
    from any process that could write to the shared wiki (cloud sync,
    shared vault, merged PR).
    """

    def test_load_index_drops_traversal_relpath(self, tmp_path: Path) -> None:
        import json as _json
        sidecar = tmp_path / mci.INDEX_FILENAME
        sidecar.write_text(_json.dumps({
            "version": 1,
            "updated": "2026-04-22T00:00:00Z",
            "by_github_url": {
                "https://github.com/safe/ok": {"slug": "ok", "relpath": "o/ok.md"},
                "https://github.com/evil/one": {
                    "slug": "evil", "relpath": "../../../../hooks/backup_on_change.py",
                },
                "https://github.com/evil/two": {
                    "slug": "evil2", "relpath": "/etc/passwd",
                },
            },
        }), encoding="utf-8")

        idx = mci.load_index(tmp_path)
        urls = set(idx["by_github_url"].keys())
        # Safe entry survives; both poisoned entries dropped.
        assert "https://github.com/safe/ok" in urls
        assert "https://github.com/evil/one" not in urls
        assert "https://github.com/evil/two" not in urls

    def test_load_index_drops_windows_drive_relative(self, tmp_path: Path) -> None:
        """``C:evil`` resolves against drive C's CWD on Windows — rejected."""
        import json as _json
        sidecar = tmp_path / mci.INDEX_FILENAME
        sidecar.write_text(_json.dumps({
            "version": 1,
            "updated": "2026-04-22T00:00:00Z",
            "by_github_url": {
                "https://github.com/x/y": {"slug": "y", "relpath": "C:evil.md"},
            },
        }), encoding="utf-8")
        idx = mci.load_index(tmp_path)
        assert idx["by_github_url"] == {}

    def test_upsert_rejects_traversal_relpath(self, tmp_path: Path) -> None:
        """Write-side validation mirrors the read side."""
        with pytest.raises(ValueError, match="invalid relpath"):
            mci.upsert(
                tmp_path, "https://github.com/x/y",
                slug="y", relpath="../../../hooks/evil.py",
            )

    def test_upsert_rejects_absolute_relpath(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid relpath"):
            mci.upsert(
                tmp_path, "https://github.com/x/y",
                slug="y", relpath="/etc/passwd",
            )

    def test_upsert_accepts_safe_relpath(self, tmp_path: Path) -> None:
        """Sanity: legitimate relpaths still work after the validator."""
        idx = mci.upsert(
            tmp_path, "https://github.com/x/y",
            slug="y", relpath="x/y.md",
        )
        assert "https://github.com/x/y" in idx["by_github_url"]
