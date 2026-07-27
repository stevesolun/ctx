"""Exclusion parity between backup capture and change detection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import backup_mirror
import change_detector
from backup_config import BackupConfig, BackupTree
from pytest import MonkeyPatch


def _write_manifest(snapshot: Path, entries: list[dict[str, str]]) -> None:
    snapshot.mkdir()
    (snapshot / "manifest.json").write_text(
        json.dumps({"snapshot_id": "baseline", "entries": entries}),
        encoding="utf-8",
    )


def test_tree_change_detection_skips_configured_destination_exclusion(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "keep.md").write_text("keep", encoding="utf-8")
    (skills / "private.env").write_text("secret", encoding="utf-8")
    cfg = BackupConfig(
        trees=(BackupTree(src="skills", dest="skills"),),
        memory_glob=False,
        excludes=("skills/private.env",),
    )

    destinations = {dest for dest, _path in change_detector._iter_tree_files(cfg, tmp_path)}

    assert destinations == {"skills/keep.md"}


def test_memory_change_detection_skips_configured_destination_exclusion(
    tmp_path: Path,
) -> None:
    memory = tmp_path / "projects" / "demo" / "memory"
    memory.mkdir(parents=True)
    (memory / "keep.md").write_text("keep", encoding="utf-8")
    (memory / "private.md").write_text("secret", encoding="utf-8")
    cfg = BackupConfig(
        trees=(),
        memory_glob=True,
        excludes=("memory/demo/private.md",),
    )

    destinations = {dest for dest, _path in change_detector._iter_memory_files(cfg, tmp_path)}

    assert destinations == {"memory/demo/keep.md"}


def test_detect_changes_filters_excluded_tree_and_memory_baseline_entries(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / "home"
    claude_home.mkdir()
    snapshot = tmp_path / "snapshot"
    _write_manifest(
        snapshot,
        [
            {"dest": "skills/private.env", "sha256": "tree-secret"},
            {"dest": "memory/demo/private.md", "sha256": "memory-secret"},
            {"dest": "skills/removed.md", "sha256": "real-removal"},
        ],
    )
    cfg = BackupConfig(
        top_files=(),
        trees=(BackupTree(src="skills", dest="skills"),),
        memory_glob=True,
        excludes=("skills/private.env", "memory/demo/private.md"),
    )

    report = change_detector.detect_changes(cfg, claude_home, snapshot)

    assert report.removed == ("skills/removed.md",)
    assert report.baseline_snapshot == "baseline"


def test_detect_changes_keeps_configured_top_file_when_exclusion_matches(
    tmp_path: Path,
) -> None:
    claude_home = tmp_path / "home"
    claude_home.mkdir()
    (claude_home / "settings.json").write_text("current", encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    _write_manifest(snapshot, [{"dest": "settings.json", "sha256": "stale"}])
    cfg = BackupConfig(
        top_files=("settings.json",),
        trees=(),
        memory_glob=False,
        excludes=("settings.json",),
    )

    report = change_detector.detect_changes(cfg, claude_home, snapshot)

    assert report.changed == ("settings.json",)
    assert report.new == ()
    assert report.removed == ()


def test_current_state_applies_size_cap_to_configured_top_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "settings.json").write_bytes(b"keep")
    (tmp_path / "oversized.json").write_bytes(b"large")
    cfg = BackupConfig(
        top_files=("settings.json", "oversized.json"),
        trees=(),
        memory_glob=False,
        max_file_bytes=4,
    )

    state = change_detector._current_state(cfg, tmp_path)

    assert state == {"settings.json": hashlib.sha256(b"keep").hexdigest()}


def test_snapshot_if_changed_does_not_repeat_for_oversized_top_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    claude_home = tmp_path / "home"
    claude_home.mkdir()
    backups_dir = tmp_path / "backups"
    normal = claude_home / "settings.json"
    normal.write_bytes(b"keep")
    (claude_home / "oversized.json").write_bytes(b"large")
    cfg = BackupConfig(
        top_files=("settings.json", "oversized.json"),
        trees=(),
        memory_glob=False,
        max_file_bytes=4,
    )
    monkeypatch.setattr(backup_mirror, "CLAUDE_HOME", claude_home)
    monkeypatch.setattr(backup_mirror, "BACKUPS_DIR", backups_dir)
    monkeypatch.setattr(backup_mirror, "_CFG", cfg)
    monkeypatch.setattr(backup_mirror, "TOP_FILES", cfg.top_files)
    monkeypatch.setattr(backup_mirror, "TREE_SOURCES", ())
    monkeypatch.setattr(backup_mirror, "MEMORY_GLOB", False)
    monkeypatch.setattr(backup_mirror, "MAX_FILE_BYTES", cfg.max_file_bytes)
    monkeypatch.setattr(backup_mirror, "SNAPSHOT_FMT", cfg.timestamp_format)

    initial = backup_mirror.snapshot_if_changed(
        backups_dir=backups_dir,
        now=1_700_000_000.0,
    )

    assert initial.snapshot_path is not None
    assert initial.report.new == ("settings.json",)
    assert len(backup_mirror.list_snapshots(backups_dir)) == 1

    repeated = backup_mirror.snapshot_if_changed(
        backups_dir=backups_dir,
        now=1_700_000_001.0,
    )

    assert repeated.snapshot_path is None
    assert not repeated.report.has_changes
    assert repeated.report.unchanged == 1
    assert len(backup_mirror.list_snapshots(backups_dir)) == 1

    normal.unlink()
    removed = backup_mirror.snapshot_if_changed(
        backups_dir=backups_dir,
        now=1_700_000_002.0,
    )

    assert removed.snapshot_path is not None
    assert removed.report.removed == ("settings.json",)
    assert len(backup_mirror.list_snapshots(backups_dir)) == 2
