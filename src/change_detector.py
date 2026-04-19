"""
change_detector.py -- Detect file changes between current state and last snapshot.

Used by ``backup_mirror.py`` to implement ``snapshot-if-changed``: we
take a new snapshot only when at least one tracked file has actually
changed (by SHA-256 content hash), not on every hook fire.

The public entry point is :func:`detect_changes`. It is a pure function
— no filesystem writes, no globals — so it can be reused by the
PostToolUse hook, the CLI, and a future daemon trigger.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as _stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from backup_config import BackupConfig


# ── Report types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChangeReport:
    """Diff between current tracked state and a previous snapshot."""

    # Files present now but not in the last snapshot.
    new: tuple[str, ...]
    # Files present in both but with a different content hash.
    changed: tuple[str, ...]
    # Files in the last snapshot but not present now.
    removed: tuple[str, ...]
    # Count of files whose hash is identical to the last snapshot.
    unchanged: int
    # Snapshot ID we compared against, or None if no baseline existed.
    baseline_snapshot: str | None

    @property
    def has_changes(self) -> bool:
        """True if a fresh snapshot would capture new information."""
        return bool(self.new or self.changed or self.removed)

    @property
    def total_current(self) -> int:
        """Total files currently tracked (new + changed + unchanged)."""
        return len(self.new) + len(self.changed) + self.unchanged

    def to_dict(self) -> dict:
        out = asdict(self)
        out["has_changes"] = self.has_changes
        out["total_current"] = self.total_current
        return out


# ── Hashing (duplicated from backup_mirror to keep this module import-cheap) ─


def _sha256_file(path: Path) -> str | None:
    """Content hash, or None if the file is a symlink / unreadable.

    Skipped files won't trigger a change-detected event; they also
    aren't in any snapshot's manifest since ``backup_mirror`` never
    hashes them. Treating both sides identically avoids false positives.
    """
    try:
        if path.is_symlink():
            return None
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ── Iteration (mirrors backup_mirror's discovery rules) ─────────────────────


def _iter_top_files(cfg: BackupConfig, claude_home: Path) -> Iterable[tuple[str, Path]]:
    for name in cfg.top_files:
        src = claude_home / name
        if src.is_file() and not src.is_symlink():
            yield (name, src)


def _iter_tree_files(cfg: BackupConfig, claude_home: Path) -> Iterable[tuple[str, Path]]:
    for tree in cfg.trees:
        root = claude_home / tree.src
        if not root.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                src = Path(dirpath) / name
                # Skip files that will be dropped during capture.
                try:
                    st = os.lstat(src)
                except OSError:
                    continue
                if _stat.S_ISLNK(st.st_mode):
                    continue
                if st.st_size > cfg.max_file_bytes:
                    continue
                rel = src.relative_to(root)
                dest_rel = (Path(tree.dest) / rel).as_posix()
                yield (dest_rel, src)


def _iter_memory_files(cfg: BackupConfig, claude_home: Path) -> Iterable[tuple[str, Path]]:
    if not cfg.memory_glob:
        return
    projects = claude_home / "projects"
    if not projects.is_dir():
        return
    for slug_dir in projects.iterdir():
        if not slug_dir.is_dir():
            continue
        memory_dir = slug_dir / "memory"
        if not memory_dir.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(memory_dir, followlinks=False):
            for name in filenames:
                src = Path(dirpath) / name
                try:
                    st = os.lstat(src)
                except OSError:
                    continue
                if _stat.S_ISLNK(st.st_mode):
                    continue
                if st.st_size > cfg.max_file_bytes:
                    continue
                rel = src.relative_to(memory_dir)
                dest_rel = (Path("memory") / slug_dir.name / rel).as_posix()
                yield (dest_rel, src)


def _current_state(cfg: BackupConfig, claude_home: Path) -> dict[str, str]:
    """Hash every currently-tracked file. Returns {dest_rel: sha256}."""
    state: dict[str, str] = {}
    for dest, src in _iter_top_files(cfg, claude_home):
        digest = _sha256_file(src)
        if digest is not None:
            state[dest] = digest
    for dest, src in _iter_tree_files(cfg, claude_home):
        digest = _sha256_file(src)
        if digest is not None:
            state[dest] = digest
    for dest, src in _iter_memory_files(cfg, claude_home):
        digest = _sha256_file(src)
        if digest is not None:
            state[dest] = digest
    return state


# ── Baseline loading ────────────────────────────────────────────────────────


def _load_snapshot_hashes(snap_path: Path) -> dict[str, str]:
    """Read manifest.json and return {dest: sha256}, skipping skipped entries."""
    manifest_path = snap_path / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = manifest.get("entries") or []
    out: dict[str, str] = {}
    for e in entries:
        dest = e.get("dest")
        digest = e.get("sha256")
        if isinstance(dest, str) and isinstance(digest, str):
            out[dest] = digest
    return out


def _snapshot_id(snap_path: Path) -> str:
    manifest_path = snap_path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return str(manifest.get("snapshot_id") or snap_path.name)
    except (OSError, json.JSONDecodeError):
        return snap_path.name


# ── Public API ──────────────────────────────────────────────────────────────


def detect_changes(
    cfg: BackupConfig,
    claude_home: Path,
    last_snapshot: Path | None,
) -> ChangeReport:
    """Compare current tracked files against a previous snapshot.

    Parameters
    ----------
    cfg
        Config describing what to track (top_files, trees, memory_glob,
        max_file_bytes, excludes).
    claude_home
        Root of the live ~/.claude tree to inspect.
    last_snapshot
        Directory of the previous snapshot, or ``None`` to treat every
        current file as new.

    Returns
    -------
    ChangeReport
        Diff between current state and the snapshot's manifest. Files
        exceeding ``max_file_bytes`` or that are symlinks are ignored on
        both sides so they never produce phantom changes.
    """
    current = _current_state(cfg, claude_home)

    if last_snapshot is None or not last_snapshot.is_dir():
        return ChangeReport(
            new=tuple(sorted(current.keys())),
            changed=(),
            removed=(),
            unchanged=0,
            baseline_snapshot=None,
        )

    baseline = _load_snapshot_hashes(last_snapshot)
    baseline_id = _snapshot_id(last_snapshot)

    new: list[str] = []
    changed: list[str] = []
    unchanged = 0
    for dest, digest in current.items():
        if dest not in baseline:
            new.append(dest)
        elif baseline[dest] != digest:
            changed.append(dest)
        else:
            unchanged += 1
    removed = [d for d in baseline if d not in current]

    return ChangeReport(
        new=tuple(sorted(new)),
        changed=tuple(sorted(changed)),
        removed=tuple(sorted(removed)),
        unchanged=unchanged,
        baseline_snapshot=baseline_id,
    )
