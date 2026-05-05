"""
_fs_utils.py -- Shared atomic file-write helpers for the ctx project.

Why this exists: 14 modules independently implemented nearly identical
``_atomic_write`` / ``_atomic_write_text`` private functions, leading to
subtle divergences (missing Windows retry, missing parent-dir creation,
predictable temp names).  This module provides a single hardened
implementation that all of them delegate to.

The ``atomic_write_*`` family writes via a temp file in the same directory
as the target, then calls ``os.replace()`` which is atomic on POSIX and
best-effort atomic on Windows.  On Windows, ``os.replace()`` raises
``PermissionError`` if the destination is held open; we retry 3 times with
a 50 ms sleep between attempts before re-raising.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_text",
    "atomic_write_bytes",
    "atomic_write_json",
    "reject_symlink_path",
    "safe_atomic_write_text",
]


# Permission mask for newly written files. 0o600 = owner read/write only.
# Phase 2.5 security reviewer noted that on Linux/macOS,
# ``tempfile.mkstemp`` defaults to 0o600 for the temp file, but
# ``os.replace`` can inherit the destination's permissions if the
# target already exists. An explicit chmod before the replace makes
# the intent load-bearing across platforms (Windows ignores the mode
# but doesn't error). Applied to all atomic writers so skill-quality
# sidecars, pulsemcp cache JSONs, and backup manifests all land
# owner-only on multi-user machines.
_FILE_MODE_PRIVATE: int = 0o600
_DARWIN_SYSTEM_SYMLINKS: dict[Path, Path] = {
    Path("/etc"): Path("/private/etc"),
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically.

    Uses a temp file in the same directory so that the final ``os.replace``
    stays on the same filesystem (avoids cross-device rename failures).
    Creates parent directories if they are missing. The written file
    lands with permissions ``0o600`` (owner read/write only).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _chmod_private(tmp)
        _replace_with_retry(tmp, path)
        _fsync_parent_dir(path.parent)
    except Exception:
        _unlink_silent(tmp)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write raw *data* to *path* atomically.

    Same temp-file-in-same-dir + ``os.replace`` strategy as
    :func:`atomic_write_text`.  Creates parent directories if missing.
    Result permissions: ``0o600``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _chmod_private(tmp)
        _replace_with_retry(tmp, path)
        _fsync_parent_dir(path.parent)
    except Exception:
        _unlink_silent(tmp)
        raise


def atomic_write_json(path: Path, obj: Any, indent: int | None = 2) -> None:
    """Serialise *obj* as JSON and write to *path* atomically.

    Produces a trailing newline for clean diffs.  Uses UTF-8 encoding.
    Creates parent directories if missing.
    """
    atomic_write_text(path, json.dumps(obj, indent=indent) + "\n", encoding="utf-8")


def reject_symlink_path(path: Path) -> None:
    """Raise if *path* or any existing ancestor is a symlink."""
    path = Path(path)
    if path.is_absolute():
        current = Path(path.anchor)
        parts = path.parts[1:]
    else:
        current = Path(".")
        parts = path.parts

    for part in parts:
        current = current / part
        if current.is_symlink() and not _is_allowed_system_symlink_ancestor(current):
            raise ValueError(f"refusing to use symlinked path: {current}")
        if not current.exists():
            return


def safe_atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomically write text without following pre-existing symlink paths."""
    reject_symlink_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_path(path)
    atomic_write_text(path, text, encoding=encoding)


def _is_allowed_system_symlink_ancestor(path: Path) -> bool:
    """Return true for macOS system symlink prefixes such as /var."""
    if sys.platform != "darwin":
        return False
    expected = _DARWIN_SYSTEM_SYMLINKS.get(path)
    if expected is None:
        return False
    try:
        return path.resolve(strict=True) == expected
    except OSError:
        return False


# ── Internal helpers ──────────────────────────────────────────────────────────


def _replace_with_retry(src: str, dst: Path, *, attempts: int = 10, delay: float = 0.05) -> None:
    """Call ``os.replace(src, dst)``, retrying on ``PermissionError``.

    On POSIX, ``os.replace`` is a single atomic syscall.  On Windows it can
    raise ``PermissionError`` when another process or thread holds the
    destination open — or even just a transient AV/indexer read handle.
    We retry *attempts* times, sleeping *delay* seconds between each try.

    Defaults (10 * 50ms = 500ms max) were tuned after CI flakes under
    8-thread concurrent writes on windows-latest; 3 * 50ms was not enough
    under load. 500ms total is still fast for interactive work.
    """
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]


def _chmod_private(path: str) -> None:
    """chmod ``path`` to owner-read/write only. Best-effort on Windows.

    ``tempfile.mkstemp`` already creates with 0o600 on POSIX, but
    ``os.replace`` onto an existing destination can inherit the
    destination's more-permissive mode. Calling chmod immediately
    before the replace pins the mode to 0o600 on the temp file so the
    final renamed inode keeps it.
    """
    try:
        os.chmod(path, _FILE_MODE_PRIVATE)
    except OSError:
        # Windows ignores most of the unix bits; cross-filesystem
        # temp placements may also return OSError. Non-fatal: the
        # replace still succeeds, just without the hardened mode.
        pass


def _fsync_parent_dir(path: Path) -> None:
    """Best-effort fsync of a directory after replacing one of its children."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink_silent(path: str) -> None:
    """Delete *path* without raising if it is already gone."""
    try:
        os.unlink(path)
    except OSError:
        pass
