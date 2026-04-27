"""
test_change_detector.py -- Coverage sprint for change_detector.py.

change_detector is a pure-function diff engine: it hashes live files,
loads a manifest from the last snapshot directory, and returns a
ChangeReport dataclass.  No filesystem writes, no subprocess calls.

Tests are grouped by the internal helper they exercise so regressions
surface at the exact layer where they occur.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import change_detector
from backup_config import BackupConfig, BackupTree


# ── Helpers ──────────────────────────────────────────────────────────────────


def _default_cfg(**kwargs: Any) -> BackupConfig:
    """Return a BackupConfig with trees=() and memory_glob=False by default."""
    base = {
        "top_files": (),
        "trees": (),
        "memory_glob": False,
    }
    base.update(kwargs)
    return BackupConfig(**base)  # type: ignore[arg-type]


def _manifest_json(
    entries: list[dict[str, Any]],
    snapshot_id: str = "snap-001",
) -> str:
    return json.dumps({"snapshot_id": snapshot_id, "entries": entries})


# ── TestChangeReport ─────────────────────────────────────────────────────────


class TestChangeReport:
    """Unit tests for the ChangeReport dataclass properties and methods."""

    def test_has_changes_false_when_all_empty(self) -> None:
        r = change_detector.ChangeReport(
            new=(), changed=(), removed=(), unchanged=5, baseline_snapshot="x"
        )
        assert r.has_changes is False

    def test_has_changes_true_when_new(self) -> None:
        r = change_detector.ChangeReport(
            new=("a.txt",), changed=(), removed=(), unchanged=0, baseline_snapshot=None
        )
        assert r.has_changes is True

    def test_has_changes_true_when_changed(self) -> None:
        r = change_detector.ChangeReport(
            new=(), changed=("b.txt",), removed=(), unchanged=3, baseline_snapshot="x"
        )
        assert r.has_changes is True

    def test_has_changes_true_when_removed(self) -> None:
        r = change_detector.ChangeReport(
            new=(), changed=(), removed=("c.txt",), unchanged=2, baseline_snapshot="x"
        )
        assert r.has_changes is True

    def test_total_current_sums_new_changed_unchanged(self) -> None:
        r = change_detector.ChangeReport(
            new=("a",), changed=("b", "c"), removed=("z",), unchanged=7, baseline_snapshot="x"
        )
        # removed does NOT count toward total_current
        assert r.total_current == 1 + 2 + 7

    def test_total_current_zero_when_nothing_tracked(self) -> None:
        r = change_detector.ChangeReport(
            new=(), changed=(), removed=(), unchanged=0, baseline_snapshot=None
        )
        assert r.total_current == 0

    def test_to_dict_includes_computed_properties(self) -> None:
        r = change_detector.ChangeReport(
            new=("f",), changed=(), removed=(), unchanged=2, baseline_snapshot="s1"
        )
        d = r.to_dict()
        assert d["has_changes"] is True
        assert d["total_current"] == 3
        assert d["new"] == ("f",)
        assert d["baseline_snapshot"] == "s1"

    def test_to_dict_has_changes_false_path(self) -> None:
        r = change_detector.ChangeReport(
            new=(), changed=(), removed=(), unchanged=1, baseline_snapshot="s1"
        )
        d = r.to_dict()
        assert d["has_changes"] is False
        assert d["total_current"] == 1

    def test_report_is_frozen_dataclass(self) -> None:
        r = change_detector.ChangeReport(
            new=(), changed=(), removed=(), unchanged=0, baseline_snapshot=None
        )
        with pytest.raises((AttributeError, TypeError)):
            r.unchanged = 99  # type: ignore[misc]


# ── TestSha256File ────────────────────────────────────────────────────────────


class TestSha256File:
    """Tests for the internal _sha256_file helper."""

    def test_known_content_matches_expected_digest(self, tmp_path: Path) -> None:
        f = tmp_path / "known.txt"
        f.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert change_detector._sha256_file(f) == expected

    def test_empty_file_has_stable_digest(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        digest = change_detector._sha256_file(f)
        assert digest == hashlib.sha256(b"").hexdigest()

    def test_same_content_same_digest(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_bytes(b"content")
        f2.write_bytes(b"content")
        assert change_detector._sha256_file(f1) == change_detector._sha256_file(f2)

    def test_different_content_different_digest(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_bytes(b"content-A")
        f2.write_bytes(b"content-B")
        assert change_detector._sha256_file(f1) != change_detector._sha256_file(f2)

    def test_single_byte_change_changes_digest(self, tmp_path: Path) -> None:
        f = tmp_path / "data.bin"
        f.write_bytes(b"abcdefgh")
        d1 = change_detector._sha256_file(f)
        f.write_bytes(b"abcdefgX")
        d2 = change_detector._sha256_file(f)
        assert d1 != d2

    def test_symlink_returns_none(self, tmp_path: Path) -> None:
        target = tmp_path / "real.txt"
        target.write_bytes(b"data")
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        assert change_detector._sha256_file(link) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert change_detector._sha256_file(tmp_path / "ghost.txt") is None

    def test_returns_lowercase_hex_string(self, tmp_path: Path) -> None:
        f = tmp_path / "f.bin"
        f.write_bytes(b"\xff\xfe")
        digest = change_detector._sha256_file(f)
        assert digest is not None
        assert digest == digest.lower()
        assert len(digest) == 64


# ── TestIterTopFiles ──────────────────────────────────────────────────────────


class TestIterTopFiles:
    """Tests for _iter_top_files."""

    def test_yields_existing_regular_files(self, tmp_path: Path) -> None:
        (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
        cfg = _default_cfg(top_files=("settings.json",))
        results = list(change_detector._iter_top_files(cfg, tmp_path))
        assert len(results) == 1
        name, path = results[0]
        assert name == "settings.json"
        assert path == tmp_path / "settings.json"

    def test_missing_file_not_yielded(self, tmp_path: Path) -> None:
        cfg = _default_cfg(top_files=("missing.json",))
        assert list(change_detector._iter_top_files(cfg, tmp_path)) == []

    def test_symlink_not_yielded(self, tmp_path: Path) -> None:
        target = tmp_path / "real.json"
        target.write_text("{}", encoding="utf-8")
        link = tmp_path / "link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        cfg = _default_cfg(top_files=("link.json",))
        assert list(change_detector._iter_top_files(cfg, tmp_path)) == []

    def test_empty_top_files_yields_nothing(self, tmp_path: Path) -> None:
        cfg = _default_cfg(top_files=())
        assert list(change_detector._iter_top_files(cfg, tmp_path)) == []

    def test_multiple_files_all_yielded(self, tmp_path: Path) -> None:
        for name in ("a.json", "b.md", "c.json"):
            (tmp_path / name).write_text("x", encoding="utf-8")
        cfg = _default_cfg(top_files=("a.json", "b.md", "c.json"))
        names = [n for n, _ in change_detector._iter_top_files(cfg, tmp_path)]
        assert sorted(names) == ["a.json", "b.md", "c.json"]

    def test_directory_not_yielded(self, tmp_path: Path) -> None:
        (tmp_path / "notafile").mkdir()
        cfg = _default_cfg(top_files=("notafile",))
        assert list(change_detector._iter_top_files(cfg, tmp_path)) == []


# ── TestIterTreeFiles ─────────────────────────────────────────────────────────


class TestIterTreeFiles:
    """Tests for _iter_tree_files."""

    def _make_tree(self, base: Path, subpath: str, content: bytes = b"data") -> Path:
        p = base / subpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_yields_files_under_tree(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path, "skills/python/SKILL.md")
        cfg = _default_cfg(trees=(BackupTree(src="skills", dest="skills"),))
        results = list(change_detector._iter_tree_files(cfg, tmp_path))
        dest_rels = [d for d, _ in results]
        assert "skills/python/SKILL.md" in dest_rels

    def test_missing_tree_root_yields_nothing(self, tmp_path: Path) -> None:
        cfg = _default_cfg(trees=(BackupTree(src="nonexistent", dest="nonexistent"),))
        assert list(change_detector._iter_tree_files(cfg, tmp_path)) == []

    def test_file_exceeding_max_bytes_skipped(self, tmp_path: Path) -> None:
        big = tmp_path / "agents" / "big.md"
        big.parent.mkdir(parents=True)
        big.write_bytes(b"x" * 100)
        cfg = _default_cfg(
            trees=(BackupTree(src="agents", dest="agents"),),
            max_file_bytes=50,
        )
        assert list(change_detector._iter_tree_files(cfg, tmp_path)) == []

    def test_file_exactly_at_max_bytes_included(self, tmp_path: Path) -> None:
        f = tmp_path / "agents" / "exact.md"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x" * 50)
        cfg = _default_cfg(
            trees=(BackupTree(src="agents", dest="agents"),),
            max_file_bytes=50,
        )
        results = list(change_detector._iter_tree_files(cfg, tmp_path))
        assert len(results) == 1

    def test_symlink_file_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "agents" / "real.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"content")
        link = tmp_path / "agents" / "link.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        cfg = _default_cfg(trees=(BackupTree(src="agents", dest="agents"),))
        # Only the real file should appear, not the symlink.
        results = list(change_detector._iter_tree_files(cfg, tmp_path))
        dest_rels = [d for d, _ in results]
        assert "agents/link.md" not in dest_rels
        assert "agents/real.md" in dest_rels

    def test_dest_rel_uses_tree_dest_prefix(self, tmp_path: Path) -> None:
        self._make_tree(tmp_path, "raw_agents/subdir/file.md")
        cfg = _default_cfg(trees=(BackupTree(src="raw_agents", dest="my_agents"),))
        results = list(change_detector._iter_tree_files(cfg, tmp_path))
        dest_rels = [d for d, _ in results]
        assert "my_agents/subdir/file.md" in dest_rels

    def test_empty_trees_yields_nothing(self, tmp_path: Path) -> None:
        cfg = _default_cfg(trees=())
        assert list(change_detector._iter_tree_files(cfg, tmp_path)) == []

    def test_nested_subdirectories_walked(self, tmp_path: Path) -> None:
        for subpath in ("skills/a/b/c/deep.md", "skills/top.md"):
            self._make_tree(tmp_path, subpath)
        cfg = _default_cfg(trees=(BackupTree(src="skills", dest="skills"),))
        results = list(change_detector._iter_tree_files(cfg, tmp_path))
        dest_rels = [d for d, _ in results]
        assert "skills/a/b/c/deep.md" in dest_rels
        assert "skills/top.md" in dest_rels


# ── TestIterMemoryFiles ───────────────────────────────────────────────────────


class TestIterMemoryFiles:
    """Tests for _iter_memory_files."""

    def _make_memory_file(
        self, base: Path, slug: str, filename: str, content: bytes = b"mem"
    ) -> Path:
        p = base / "projects" / slug / "memory" / filename
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def test_memory_glob_false_yields_nothing(self, tmp_path: Path) -> None:
        self._make_memory_file(tmp_path, "proj-slug", "MEMORY.md")
        cfg = _default_cfg(memory_glob=False)
        assert list(change_detector._iter_memory_files(cfg, tmp_path)) == []

    def test_missing_projects_dir_yields_nothing(self, tmp_path: Path) -> None:
        cfg = _default_cfg(memory_glob=True)
        assert list(change_detector._iter_memory_files(cfg, tmp_path)) == []

    def test_yields_memory_files_under_projects(self, tmp_path: Path) -> None:
        self._make_memory_file(tmp_path, "my-proj", "NOTE.md")
        cfg = _default_cfg(memory_glob=True)
        results = list(change_detector._iter_memory_files(cfg, tmp_path))
        dest_rels = [d for d, _ in results]
        assert "memory/my-proj/NOTE.md" in dest_rels

    def test_dest_rel_includes_slug_and_filename(self, tmp_path: Path) -> None:
        self._make_memory_file(tmp_path, "alpha", "a.md")
        self._make_memory_file(tmp_path, "beta", "b.md")
        cfg = _default_cfg(memory_glob=True)
        results = list(change_detector._iter_memory_files(cfg, tmp_path))
        dest_rels = {d for d, _ in results}
        assert "memory/alpha/a.md" in dest_rels
        assert "memory/beta/b.md" in dest_rels

    def test_file_exceeding_max_bytes_skipped(self, tmp_path: Path) -> None:
        self._make_memory_file(tmp_path, "proj", "big.md", content=b"x" * 200)
        cfg = _default_cfg(memory_glob=True, max_file_bytes=100)
        assert list(change_detector._iter_memory_files(cfg, tmp_path)) == []

    def test_symlink_memory_file_skipped(self, tmp_path: Path) -> None:
        target = self._make_memory_file(tmp_path, "proj", "real.md")
        link = tmp_path / "projects" / "proj" / "memory" / "link.md"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        cfg = _default_cfg(memory_glob=True)
        results = list(change_detector._iter_memory_files(cfg, tmp_path))
        dest_rels = [d for d, _ in results]
        assert "memory/proj/link.md" not in dest_rels
        assert "memory/proj/real.md" in dest_rels

    def test_project_file_not_dir_ignored(self, tmp_path: Path) -> None:
        """A file directly inside projects/ (not a dir) must be skipped."""
        projects = tmp_path / "projects"
        projects.mkdir(parents=True)
        (projects / "stray-file.txt").write_text("x", encoding="utf-8")
        cfg = _default_cfg(memory_glob=True)
        assert list(change_detector._iter_memory_files(cfg, tmp_path)) == []

    def test_slug_without_memory_subdir_skipped(self, tmp_path: Path) -> None:
        slug_dir = tmp_path / "projects" / "no-memory"
        slug_dir.mkdir(parents=True)
        cfg = _default_cfg(memory_glob=True)
        assert list(change_detector._iter_memory_files(cfg, tmp_path)) == []

    def test_nested_memory_path_dest_rel(self, tmp_path: Path) -> None:
        nested = tmp_path / "projects" / "proj" / "memory" / "sub" / "deep.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_bytes(b"content")
        cfg = _default_cfg(memory_glob=True)
        results = list(change_detector._iter_memory_files(cfg, tmp_path))
        dest_rels = [d for d, _ in results]
        assert "memory/proj/sub/deep.md" in dest_rels


# ── TestLoadSnapshotHashes ────────────────────────────────────────────────────


class TestLoadSnapshotHashes:
    """Tests for the internal _load_snapshot_hashes helper."""

    def test_missing_manifest_returns_empty(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        assert change_detector._load_snapshot_hashes(snap) == {}

    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text("{NOT JSON", encoding="utf-8")
        assert change_detector._load_snapshot_hashes(snap) == {}

    def test_valid_manifest_returns_mapping(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            _manifest_json([{"dest": "settings.json", "sha256": "abc123"}]),
            encoding="utf-8",
        )
        result = change_detector._load_snapshot_hashes(snap)
        assert result == {"settings.json": "abc123"}

    def test_entries_missing_dest_skipped(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            _manifest_json([{"sha256": "abc"}, {"dest": "ok.md", "sha256": "def"}]),
            encoding="utf-8",
        )
        result = change_detector._load_snapshot_hashes(snap)
        assert "ok.md" in result
        assert len(result) == 1

    def test_entries_missing_sha256_skipped(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            _manifest_json([{"dest": "x.md"}, {"dest": "y.md", "sha256": "hash"}]),
            encoding="utf-8",
        )
        result = change_detector._load_snapshot_hashes(snap)
        assert result == {"y.md": "hash"}

    def test_entries_null_values_skipped(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            _manifest_json([{"dest": None, "sha256": "x"}, {"dest": "z.md", "sha256": "y"}]),
            encoding="utf-8",
        )
        result = change_detector._load_snapshot_hashes(snap)
        assert result == {"z.md": "y"}

    def test_empty_entries_returns_empty(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            json.dumps({"snapshot_id": "s", "entries": []}),
            encoding="utf-8",
        )
        assert change_detector._load_snapshot_hashes(snap) == {}

    def test_manifest_without_entries_key_returns_empty(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            json.dumps({"snapshot_id": "s"}),
            encoding="utf-8",
        )
        assert change_detector._load_snapshot_hashes(snap) == {}

    def test_multiple_entries_all_loaded(self, tmp_path: Path) -> None:
        snap = tmp_path / "snap"
        snap.mkdir()
        entries = [
            {"dest": "a.md", "sha256": "hash-a"},
            {"dest": "b.md", "sha256": "hash-b"},
            {"dest": "c.md", "sha256": "hash-c"},
        ]
        (snap / "manifest.json").write_text(
            _manifest_json(entries), encoding="utf-8"
        )
        result = change_detector._load_snapshot_hashes(snap)
        assert len(result) == 3
        assert result["b.md"] == "hash-b"


# ── TestSnapshotId ────────────────────────────────────────────────────────────


class TestSnapshotId:
    """Tests for the internal _snapshot_id helper."""

    def test_reads_snapshot_id_from_manifest(self, tmp_path: Path) -> None:
        snap = tmp_path / "20240101T120000Z"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            json.dumps({"snapshot_id": "custom-id-42", "entries": []}),
            encoding="utf-8",
        )
        assert change_detector._snapshot_id(snap) == "custom-id-42"

    def test_falls_back_to_dir_name_on_missing_manifest(self, tmp_path: Path) -> None:
        snap = tmp_path / "fallback-name"
        snap.mkdir()
        assert change_detector._snapshot_id(snap) == "fallback-name"

    def test_falls_back_to_dir_name_on_corrupt_json(self, tmp_path: Path) -> None:
        snap = tmp_path / "fallback-corrupt"
        snap.mkdir()
        (snap / "manifest.json").write_text("{BAD JSON", encoding="utf-8")
        assert change_detector._snapshot_id(snap) == "fallback-corrupt"

    def test_falls_back_to_dir_name_when_snapshot_id_absent(self, tmp_path: Path) -> None:
        snap = tmp_path / "dir-name-fallback"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            json.dumps({"entries": []}), encoding="utf-8"
        )
        assert change_detector._snapshot_id(snap) == "dir-name-fallback"

    def test_snapshot_id_none_falls_back_to_dir_name(self, tmp_path: Path) -> None:
        snap = tmp_path / "none-id-fallback"
        snap.mkdir()
        (snap / "manifest.json").write_text(
            json.dumps({"snapshot_id": None, "entries": []}),
            encoding="utf-8",
        )
        # None -> falsy -> falls back to snap.name
        assert change_detector._snapshot_id(snap) == "none-id-fallback"


# ── TestDetectChanges ─────────────────────────────────────────────────────────


class TestDetectChanges:
    """Integration tests for the public detect_changes function."""

    def _write_file(self, path: Path, content: bytes = b"content") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _write_manifest(
        self,
        snap: Path,
        entries: list[dict[str, Any]],
        snapshot_id: str = "baseline-snap",
    ) -> None:
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "manifest.json").write_text(
            _manifest_json(entries, snapshot_id), encoding="utf-8"
        )

    # -- No baseline --

    def test_no_baseline_all_files_are_new(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        (claude_home / "settings.json").write_text("{}", encoding="utf-8")
        cfg = _default_cfg(top_files=("settings.json",))
        report = change_detector.detect_changes(cfg, claude_home, last_snapshot=None)
        assert "settings.json" in report.new
        assert report.changed == ()
        assert report.removed == ()
        assert report.unchanged == 0
        assert report.baseline_snapshot is None

    def test_none_snapshot_no_tracked_files(self, tmp_path: Path) -> None:
        cfg = _default_cfg()
        report = change_detector.detect_changes(cfg, tmp_path, last_snapshot=None)
        assert report.new == ()
        assert report.has_changes is False
        assert report.baseline_snapshot is None

    def test_nonexistent_snapshot_dir_treated_as_no_baseline(
        self, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        (claude_home / "CLAUDE.md").write_text("hi", encoding="utf-8")
        cfg = _default_cfg(top_files=("CLAUDE.md",))
        ghost_snap = tmp_path / "nonexistent-snap"
        report = change_detector.detect_changes(cfg, claude_home, ghost_snap)
        assert "CLAUDE.md" in report.new
        assert report.baseline_snapshot is None

    # -- All unchanged --

    def test_all_files_unchanged(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        f = claude_home / "settings.json"
        f.write_bytes(b"same")
        digest = hashlib.sha256(b"same").hexdigest()
        snap = tmp_path / "snap"
        self._write_manifest(snap, [{"dest": "settings.json", "sha256": digest}])
        cfg = _default_cfg(top_files=("settings.json",))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert report.new == ()
        assert report.changed == ()
        assert report.removed == ()
        assert report.unchanged == 1
        assert report.has_changes is False

    # -- New files --

    def test_file_absent_from_baseline_is_new(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        (claude_home / "new.json").write_bytes(b"new content")
        snap = tmp_path / "snap"
        self._write_manifest(snap, [])  # empty baseline
        cfg = _default_cfg(top_files=("new.json",))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert "new.json" in report.new
        assert report.changed == ()
        assert report.removed == ()

    # -- Changed files --

    def test_file_with_different_hash_is_changed(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        f = claude_home / "CLAUDE.md"
        f.write_bytes(b"new-content")
        snap = tmp_path / "snap"
        self._write_manifest(snap, [{"dest": "CLAUDE.md", "sha256": "old-stale-hash"}])
        cfg = _default_cfg(top_files=("CLAUDE.md",))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert "CLAUDE.md" in report.changed
        assert report.new == ()
        assert report.removed == ()

    # -- Removed files --

    def test_file_in_baseline_not_on_disk_is_removed(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        snap = tmp_path / "snap"
        self._write_manifest(snap, [{"dest": "gone.json", "sha256": "dead"}])
        cfg = _default_cfg(top_files=("gone.json",))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert "gone.json" in report.removed
        assert report.new == ()
        assert report.changed == ()

    # -- Combined --

    def test_mixed_new_changed_removed_unchanged(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()

        # new — present now, absent in baseline
        (claude_home / "new.json").write_bytes(b"fresh")
        # changed — present now, different hash in baseline
        (claude_home / "changed.json").write_bytes(b"modified")
        hashlib.sha256(b"modified").hexdigest()
        # unchanged — present now, same hash
        (claude_home / "same.json").write_bytes(b"identical")
        same_digest = hashlib.sha256(b"identical").hexdigest()
        # removed — in baseline, not on disk

        snap = tmp_path / "snap"
        self._write_manifest(snap, [
            {"dest": "changed.json", "sha256": "stale-hash"},
            {"dest": "same.json",    "sha256": same_digest},
            {"dest": "removed.json", "sha256": "any-hash"},
        ])
        cfg = _default_cfg(top_files=("new.json", "changed.json", "same.json", "removed.json"))
        report = change_detector.detect_changes(cfg, claude_home, snap)

        assert "new.json" in report.new
        assert "changed.json" in report.changed
        assert "removed.json" in report.removed
        assert report.unchanged == 1
        assert report.has_changes is True
        assert report.baseline_snapshot == "baseline-snap"

    # -- Snapshot ID propagation --

    def test_baseline_snapshot_id_propagated(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        snap = tmp_path / "snap"
        self._write_manifest(snap, [], snapshot_id="my-snap-xyz")
        cfg = _default_cfg()
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert report.baseline_snapshot == "my-snap-xyz"

    # -- Trees integration --

    def test_tree_files_compared_against_baseline(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        skill_file = claude_home / "skills" / "py" / "SKILL.md"
        skill_file.parent.mkdir(parents=True)
        skill_file.write_bytes(b"skill content")
        digest = hashlib.sha256(b"skill content").hexdigest()

        snap = tmp_path / "snap"
        self._write_manifest(snap, [{"dest": "skills/py/SKILL.md", "sha256": digest}])
        cfg = BackupConfig(
            top_files=(),
            trees=(BackupTree(src="skills", dest="skills"),),
            memory_glob=False,
        )
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert report.unchanged == 1
        assert report.has_changes is False

    # -- Memory files integration --

    def test_memory_files_detected_as_new(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        mem_file = claude_home / "projects" / "proj-x" / "memory" / "MEMORY.md"
        mem_file.parent.mkdir(parents=True)
        mem_file.write_bytes(b"mem data")

        snap = tmp_path / "snap"
        self._write_manifest(snap, [])  # empty baseline
        cfg = _default_cfg(memory_glob=True)
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert any("memory/proj-x" in d for d in report.new)

    # -- Sorted output --

    def test_new_files_are_sorted(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        for name in ("z.json", "a.json", "m.json"):
            (claude_home / name).write_bytes(b"x")
        cfg = _default_cfg(top_files=("z.json", "a.json", "m.json"))
        report = change_detector.detect_changes(cfg, claude_home, last_snapshot=None)
        assert list(report.new) == sorted(report.new)

    def test_changed_files_are_sorted(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        files = ["z.json", "a.json", "m.json"]
        for name in files:
            (claude_home / name).write_bytes(b"current")
        snap = tmp_path / "snap"
        self._write_manifest(snap, [{"dest": n, "sha256": "old"} for n in files])
        cfg = _default_cfg(top_files=tuple(files))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert list(report.changed) == sorted(report.changed)

    # -- Symlink in tracked path --

    def test_symlink_top_file_not_counted(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        target = claude_home / "real.json"
        target.write_bytes(b"content")
        link = claude_home / "settings.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")
        snap = tmp_path / "snap"
        self._write_manifest(snap, [])
        cfg = _default_cfg(top_files=("settings.json",))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        # _iter_top_files skips the symlink → nothing in current state
        assert report.new == ()

    # -- Large file skipped by max_file_bytes --

    def test_oversized_tree_file_never_counted_as_new(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        big = claude_home / "agents" / "huge.md"
        big.parent.mkdir(parents=True)
        big.write_bytes(b"x" * 1000)
        snap = tmp_path / "snap"
        self._write_manifest(snap, [])
        cfg = BackupConfig(
            top_files=(),
            trees=(BackupTree(src="agents", dest="agents"),),
            memory_glob=False,
            max_file_bytes=500,
        )
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert report.new == ()

    # -- Parametrized: baseline_snapshot vs no baseline --

    @pytest.mark.parametrize(
        "last_snapshot_is_none,expected_baseline",
        [
            (True, None),
            (False, "snap-id"),
        ],
    )
    def test_baseline_snapshot_field(
        self,
        tmp_path: Path,
        last_snapshot_is_none: bool,
        expected_baseline: str | None,
    ) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        if last_snapshot_is_none:
            snap = None
        else:
            snap = tmp_path / "snap"
            self._write_manifest(snap, [], snapshot_id="snap-id")
        cfg = _default_cfg()
        report = change_detector.detect_changes(cfg, claude_home, snap)
        assert report.baseline_snapshot == expected_baseline

    # -- Parametrized: has_changes conditions --

    @pytest.mark.parametrize(
        "new,changed,removed,unchanged,expect_changes",
        [
            ((), (), (), 0, False),
            (("a",), (), (), 0, True),
            ((), ("b",), (), 0, True),
            ((), (), ("c",), 0, True),
            ((), (), (), 5, False),
            (("a",), ("b",), ("c",), 2, True),
        ],
    )
    def test_has_changes_parametrized(
        self,
        new: tuple[str, ...],
        changed: tuple[str, ...],
        removed: tuple[str, ...],
        unchanged: int,
        expect_changes: bool,
    ) -> None:
        r = change_detector.ChangeReport(
            new=new,
            changed=changed,
            removed=removed,
            unchanged=unchanged,
            baseline_snapshot=None,
        )
        assert r.has_changes is expect_changes

    # -- Corrupt manifest in snapshot dir --

    def test_corrupt_manifest_treats_all_current_as_new(self, tmp_path: Path) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        (claude_home / "settings.json").write_bytes(b"data")
        snap = tmp_path / "snap"
        snap.mkdir()
        (snap / "manifest.json").write_text("{INVALID", encoding="utf-8")
        cfg = _default_cfg(top_files=("settings.json",))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        # Corrupt manifest → empty baseline → settings.json treated as new
        assert "settings.json" in report.new
        assert report.removed == ()

    # -- total_current property via detect_changes --

    def test_total_current_counts_new_changed_unchanged_not_removed(
        self, tmp_path: Path
    ) -> None:
        claude_home = tmp_path / "home"
        claude_home.mkdir()
        # 2 new files
        (claude_home / "a.json").write_bytes(b"a")
        (claude_home / "b.json").write_bytes(b"b")
        # 1 unchanged
        (claude_home / "c.json").write_bytes(b"same")
        same_d = hashlib.sha256(b"same").hexdigest()
        # 1 changed
        (claude_home / "d.json").write_bytes(b"new")
        snap = tmp_path / "snap"
        self._write_manifest(snap, [
            {"dest": "c.json", "sha256": same_d},
            {"dest": "d.json", "sha256": "old-hash"},
            {"dest": "removed.json", "sha256": "r-hash"},
        ])
        cfg = _default_cfg(top_files=("a.json", "b.json", "c.json", "d.json"))
        report = change_detector.detect_changes(cfg, claude_home, snap)
        # new=2, changed=1, unchanged=1  → total_current=4
        assert report.total_current == 4
