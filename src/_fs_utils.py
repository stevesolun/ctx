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
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = ["atomic_write_text", "atomic_write_bytes", "atomic_write_json"]


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write *text* to *path* atomically.

    Uses a temp file in the same directory so that the final ``os.replace``
    stays on the same filesystem (avoids cross-device rename failures).
    Creates parent directories if they are missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
        _replace_with_retry(tmp, path)
    except Exception:
        _unlink_silent(tmp)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write raw *data* to *path* atomically.

    Same temp-file-in-same-dir + ``os.replace`` strategy as
    :func:`atomic_write_text`.  Creates parent directories if missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _replace_with_retry(tmp, path)
    except Exception:
        _unlink_silent(tmp)
        raise


def atomic_write_json(path: Path, obj: Any, indent: int | None = 2) -> None:
    """Serialise *obj* as JSON and write to *path* atomically.

    Produces a trailing newline for clean diffs.  Uses UTF-8 encoding.
    Creates parent directories if missing.
    """
    atomic_write_text(path, json.dumps(obj, indent=indent) + "\n", encoding="utf-8")


# ── Internal helpers ──────────────────────────────────────────────────────────


def _replace_with_retry(src: str, dst: Path, *, attempts: int = 3, delay: float = 0.05) -> None:
    """Call ``os.replace(src, dst)``, retrying on ``PermissionError``.

    On POSIX, ``os.replace`` is a single atomic syscall.  On Windows it can
    raise ``PermissionError`` when another process holds the destination open.
    We retry *attempts* times, sleeping *delay* seconds between each try.
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


def _unlink_silent(path: str) -> None:
    """Delete *path* without raising if it is already gone."""
    try:
        os.unlink(path)
    except OSError:
        pass
