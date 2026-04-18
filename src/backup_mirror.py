#!/usr/bin/env python3
"""
backup_mirror.py -- Timestamped backup mirror of critical Claude files.

Mirrors the user's most important local state so that a broken hook, a
merge mishap, or a mis-applied heal does not cost them their work. The
following trees are mirrored:

    ~/.claude/settings.json                           -> settings.json
    ~/.claude/skill-manifest.json                     -> skill-manifest.json
    ~/.claude/pending-skills.json                     -> pending-skills.json
    ~/.claude/agents/<name>.md                        -> agents/<name>.md
    ~/.claude/skills/<name>/SKILL.md                  -> skills/<name>/SKILL.md
    ~/.claude/projects/<slug>/memory/*.md             -> memory/<slug>/*.md

Each snapshot lives under ``~/.claude/backups/<timestamp>/`` with a
companion ``manifest.json`` recording the absolute source path, SHA-256
digest, and byte size of every captured file.

Commands:

    python src/backup_mirror.py create
        Take a fresh snapshot. Prints the snapshot directory on stdout.

    python src/backup_mirror.py list
        Show snapshots newest-first with file counts and total size.

    python src/backup_mirror.py verify [--snapshot <id>]
        Re-hash every file in the snapshot against its manifest entry.
        Exit 2 if any hash mismatch or missing file.

    python src/backup_mirror.py restore --snapshot <id> [--dry-run]
        Restore the snapshot over the live tree. Without --dry-run this
        overwrites live files; with --dry-run it prints what would change.

    python src/backup_mirror.py prune --keep <N>
        Delete all but the N newest snapshots.

Design notes:

- Never follows symlinks (os.walk with followlinks=False) to avoid
  capturing unrelated trees if the user has symlinked ~/.claude anywhere.
- Per-file size cap (MAX_FILE_BYTES) prevents a rogue multi-GB log from
  ballooning backups. Files over the cap are recorded in manifest with
  ``skipped: "too_large"`` but their content is not copied.
- Atomic writes via tempfile + os.replace so a crashed snapshot never
  leaves a half-written file under the snapshot directory name.
- Restore validates every source digest before touching live files. If
  any mismatch is found the restore aborts before any file is modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


CLAUDE_HOME = Path(os.path.expanduser("~/.claude"))
BACKUPS_DIR = CLAUDE_HOME / "backups"

# Top-level JSON files we always mirror if present.
TOP_FILES: tuple[str, ...] = (
    "settings.json",
    "skill-manifest.json",
    "pending-skills.json",
)

# Per-file cap: anything larger is manifested but not copied.
MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB

# Directories inside ~/.claude that we copy as trees.
TREE_SOURCES: tuple[tuple[str, str], ...] = (
    # (relative source under CLAUDE_HOME, relative destination in snapshot)
    ("agents", "agents"),
    ("skills", "skills"),
)

# Snapshot ID is a UTC timestamp so lexical sort == chronological sort.
# Microsecond suffix avoids collisions when snapshots are taken in quick
# succession (e.g. test runs or scripted automation).
SNAPSHOT_FMT = "%Y%m%dT%H%M%SZ"


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManifestEntry:
    source: str        # absolute path at time of backup
    dest: str          # relative path under the snapshot dir
    size: int
    sha256: str | None  # None if skipped
    skipped: str | None  # reason code when content not copied

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotInfo:
    snapshot_id: str
    path: str
    created_at: float
    file_count: int
    total_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)


# ── Snapshot discovery ──────────────────────────────────────────────────────


def _iter_top_files() -> Iterable[Path]:
    for name in TOP_FILES:
        p = CLAUDE_HOME / name
        if p.is_file():
            yield p


def _iter_tree(src_rel: str) -> Iterable[Path]:
    root = CLAUDE_HOME / src_rel
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            yield Path(dirpath) / name


def _iter_memory_files() -> Iterable[tuple[str, Path]]:
    """
    Walk ~/.claude/projects/<slug>/memory and yield (slug, path) pairs so
    the mirror preserves per-project memory grouping.
    """
    projects = CLAUDE_HOME / "projects"
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
                yield slug_dir.name, Path(dirpath) / name


# ── Hashing + atomic copy ───────────────────────────────────────────────────


def _sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=dest.name + ".", dir=str(dest.parent))
    try:
        os.close(fd)
        shutil.copy2(str(src), tmp)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── Create ──────────────────────────────────────────────────────────────────


def _new_snapshot_id(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    base = time.strftime(SNAPSHOT_FMT, time.gmtime(ts))
    # Append microseconds so back-to-back snapshots don't collide.
    micro = int((ts - int(ts)) * 1_000_000)
    return f"{base[:-1]}.{micro:06d}Z"


def create_snapshot(backups_dir: Path | None = None,
                    now: float | None = None) -> Path:
    """Produce a fresh backup snapshot and return its directory."""
    # Read module globals at call time so monkeypatches apply.
    backups_dir = backups_dir if backups_dir is not None else BACKUPS_DIR
    claude_home = CLAUDE_HOME
    backups_dir.mkdir(parents=True, exist_ok=True)
    snap_id = _new_snapshot_id(now)
    snap_path = backups_dir / snap_id
    snap_path.mkdir(parents=True, exist_ok=False)

    entries: list[ManifestEntry] = []

    for src in _iter_top_files():
        entries.append(_capture_file(src, snap_path, src.name))

    for src_rel, dest_rel in TREE_SOURCES:
        root = claude_home / src_rel
        for src in _iter_tree(src_rel):
            rel = src.relative_to(root)
            dest_rel_path = Path(dest_rel) / rel
            entries.append(_capture_file(src, snap_path, dest_rel_path.as_posix()))

    for slug, src in _iter_memory_files():
        memory_root = claude_home / "projects" / slug / "memory"
        rel = src.relative_to(memory_root)
        dest_rel_path = Path("memory") / slug / rel
        entries.append(_capture_file(src, snap_path, dest_rel_path.as_posix()))

    manifest = {
        "snapshot_id": snap_id,
        "created_at": now if now is not None else time.time(),
        "claude_home": str(claude_home),
        "entries": [e.to_dict() for e in entries],
    }
    _atomic_write_text(
        snap_path / "manifest.json",
        json.dumps(manifest, indent=2),
    )
    return snap_path


def _capture_file(src: Path, snap_path: Path, dest_rel: str) -> ManifestEntry:
    dest = snap_path / dest_rel
    try:
        size = src.stat().st_size
    except OSError:
        return ManifestEntry(
            source=str(src),
            dest=dest_rel,
            size=0,
            sha256=None,
            skipped="stat_failed",
        )
    if size > MAX_FILE_BYTES:
        return ManifestEntry(
            source=str(src),
            dest=dest_rel,
            size=size,
            sha256=None,
            skipped="too_large",
        )
    try:
        _atomic_copy(src, dest)
    except OSError:
        return ManifestEntry(
            source=str(src),
            dest=dest_rel,
            size=size,
            sha256=None,
            skipped="copy_failed",
        )
    digest = _sha256_file(dest)
    return ManifestEntry(
        source=str(src),
        dest=dest_rel,
        size=size,
        sha256=digest,
        skipped=None,
    )


# ── List ────────────────────────────────────────────────────────────────────


def list_snapshots(backups_dir: Path | None = None) -> list[SnapshotInfo]:
    backups_dir = backups_dir if backups_dir is not None else BACKUPS_DIR
    if not backups_dir.is_dir():
        return []
    out: list[SnapshotInfo] = []
    for child in sorted(backups_dir.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        manifest = child / "manifest.json"
        if not manifest.is_file():
            continue
        try:
            raw = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries = raw.get("entries") or []
        total = sum(int(e.get("size") or 0) for e in entries)
        out.append(SnapshotInfo(
            snapshot_id=str(raw.get("snapshot_id") or child.name),
            path=str(child),
            created_at=float(raw.get("created_at") or 0),
            file_count=len(entries),
            total_bytes=total,
        ))
    return out


# ── Verify ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VerifyReport:
    snapshot_id: str
    checked: int
    missing: tuple[str, ...]
    hash_mismatch: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.hash_mismatch

    def to_dict(self) -> dict:
        return asdict(self)


def verify_snapshot(snap_path: Path) -> VerifyReport:
    manifest = json.loads((snap_path / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("entries") or []
    missing: list[str] = []
    mismatch: list[str] = []
    skipped: list[str] = []
    checked = 0
    for raw in entries:
        dest_rel = str(raw.get("dest") or "")
        expected = raw.get("sha256")
        skip_reason = raw.get("skipped")
        if skip_reason:
            skipped.append(f"{dest_rel}: {skip_reason}")
            continue
        file_path = snap_path / dest_rel
        if not file_path.is_file():
            missing.append(dest_rel)
            continue
        actual = _sha256_file(file_path)
        if actual != expected:
            mismatch.append(dest_rel)
        checked += 1
    return VerifyReport(
        snapshot_id=str(manifest.get("snapshot_id") or snap_path.name),
        checked=checked,
        missing=tuple(missing),
        hash_mismatch=tuple(mismatch),
        skipped=tuple(skipped),
    )


# ── Restore ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RestoreReport:
    snapshot_id: str
    restored: tuple[str, ...]
    skipped: tuple[str, ...]
    dry_run: bool

    def to_dict(self) -> dict:
        return asdict(self)


def restore_snapshot(snap_path: Path,
                     claude_home: Path | None = None,
                     dry_run: bool = False) -> RestoreReport:
    """
    Restore the given snapshot over ``claude_home``. Refuses to start
    unless the snapshot verifies clean.
    """
    claude_home = claude_home if claude_home is not None else CLAUDE_HOME
    verify = verify_snapshot(snap_path)
    if not verify.ok:
        raise RuntimeError(
            f"snapshot {verify.snapshot_id} failed verification; "
            f"refusing to restore. Missing: {verify.missing}, "
            f"hash mismatch: {verify.hash_mismatch}."
        )

    manifest = json.loads((snap_path / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("entries") or []

    restored: list[str] = []
    skipped: list[str] = []
    for raw in entries:
        dest_rel = str(raw.get("dest") or "")
        if raw.get("skipped"):
            skipped.append(dest_rel)
            continue
        src = snap_path / dest_rel
        # Map snapshot layout back onto CLAUDE_HOME.
        target = _resolve_restore_target(dest_rel, claude_home)
        if not dry_run:
            _atomic_copy(src, target)
        restored.append(str(target))
    return RestoreReport(
        snapshot_id=str(manifest.get("snapshot_id") or snap_path.name),
        restored=tuple(restored),
        skipped=tuple(skipped),
        dry_run=dry_run,
    )


def _resolve_restore_target(dest_rel: str, claude_home: Path) -> Path:
    """
    Snapshot layout -> live layout. Inverse of the mapping used in
    create_snapshot.
    """
    parts = Path(dest_rel).parts
    if not parts:
        raise ValueError(f"empty dest: {dest_rel!r}")

    # Top-level JSON files.
    if dest_rel in TOP_FILES:
        return claude_home / dest_rel

    # memory/<slug>/<rel...>
    if parts[0] == "memory" and len(parts) >= 3:
        slug = parts[1]
        rel = Path(*parts[2:])
        return claude_home / "projects" / slug / "memory" / rel

    # agents/... and skills/...
    for src_rel, dest_head in TREE_SOURCES:
        if parts[0] == dest_head:
            rel = Path(*parts[1:]) if len(parts) > 1 else Path()
            return claude_home / src_rel / rel

    raise ValueError(f"unrecognised dest layout: {dest_rel!r}")


# ── Prune ───────────────────────────────────────────────────────────────────


def prune_snapshots(keep: int,
                    backups_dir: Path | None = None) -> tuple[str, ...]:
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")
    backups_dir = backups_dir if backups_dir is not None else BACKUPS_DIR
    snaps = list_snapshots(backups_dir)
    to_remove = snaps[keep:]
    removed: list[str] = []
    for snap in to_remove:
        shutil.rmtree(snap.path, ignore_errors=True)
        removed.append(snap.snapshot_id)
    return tuple(removed)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _resolve_snapshot_arg(arg: str | None,
                          backups_dir: Path) -> Path:
    snaps = list_snapshots(backups_dir)
    if not snaps:
        raise FileNotFoundError("no snapshots exist")
    if arg in (None, "", "latest"):
        return Path(snaps[0].path)
    for s in snaps:
        if s.snapshot_id == arg:
            return Path(s.path)
    raise FileNotFoundError(f"no snapshot matches {arg!r}")


def cmd_create(args: argparse.Namespace) -> int:
    snap_path = create_snapshot()
    print(str(snap_path))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    snaps = list_snapshots()
    if args.json:
        print(json.dumps([s.to_dict() for s in snaps], indent=2))
        return 0
    if not snaps:
        print("No snapshots.")
        return 0
    for s in snaps:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(s.created_at))
        kb = s.total_bytes / 1024
        print(f"{s.snapshot_id}  {ts}  files={s.file_count}  size={kb:.1f} KB")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        snap_path = _resolve_snapshot_arg(args.snapshot, BACKUPS_DIR)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    report = verify_snapshot(snap_path)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(
            f"[verify] {report.snapshot_id}  checked={report.checked}  "
            f"missing={len(report.missing)}  mismatch={len(report.hash_mismatch)}"
        )
        for m in report.missing:
            print(f"  missing: {m}")
        for m in report.hash_mismatch:
            print(f"  mismatch: {m}")
    return 0 if report.ok else 2


def cmd_restore(args: argparse.Namespace) -> int:
    try:
        snap_path = _resolve_snapshot_arg(args.snapshot, BACKUPS_DIR)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        report = restore_snapshot(snap_path, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Run `python src/backup_mirror.py verify --snapshot "
            f"{snap_path.name}` to diagnose.",
            file=sys.stderr,
        )
        return 2
    label = "[restore:dry-run]" if report.dry_run else "[restore]"
    print(f"{label} {report.snapshot_id}  files={len(report.restored)}  "
          f"skipped={len(report.skipped)}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    removed = prune_snapshots(args.keep)
    for r in removed:
        print(f"removed {r}")
    print(f"kept {args.keep} newest snapshot(s); removed {len(removed)}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Backup mirror for ~/.claude state.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="Take a new snapshot.")
    c.set_defaults(func=cmd_create)

    ls = sub.add_parser("list", help="List snapshots newest-first.")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    v = sub.add_parser("verify", help="Verify a snapshot against its manifest.")
    v.add_argument("--snapshot", default="latest",
                   help="Snapshot ID or 'latest'.")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("restore", help="Restore a snapshot over the live tree.")
    r.add_argument("--snapshot", default="latest")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(func=cmd_restore)

    pr = sub.add_parser("prune", help="Keep only the N newest snapshots.")
    pr.add_argument("--keep", type=int, required=True)
    pr.set_defaults(func=cmd_prune)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


# Keep Sequence import where mypy expects it for main(argv=...).
from typing import Sequence  # noqa: E402


if __name__ == "__main__":
    sys.exit(main())
