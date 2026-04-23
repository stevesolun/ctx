"""
_safe_name.py -- shared validators for path-derived names and relpaths.

Two security-auditor findings (H-2 canonical_index relpath traversal,
H-3 checkpoint source allowing Windows drive-relative paths) both stem
from the same class of bug: a user- or file-controlled string becomes
a filesystem path component without containment validation. Rather than
fix each site independently and risk drift, both now call into this
module.

Public surface:
  - ``is_safe_source_name(name)``: True when *name* is a plain identifier
    usable as a single-component filename.
  - ``validate_source_name(name)`` -- raises ``ValueError`` on rejection.
  - ``is_safe_relpath(root, relpath)``: True when *relpath* is a
    relative path that resolves INSIDE *root*. Blocks ``..`` components,
    absolute paths, Windows drive-relative (``C:foo``), and symlink
    escape within root.
  - ``validate_relpath(root, relpath)`` -- raises ``ValueError``.

Both validators are conservative: they reject edge cases rather than
attempt to repair. A caller who gets a ``ValueError`` should treat it
as "hostile input" not "try again with different encoding".
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePath, PureWindowsPath
from typing import Final


# Accept ``[a-z0-9][a-z0-9_.-]{0,127}`` — length, leading char, allowed chars.
_SOURCE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,127}$")

# Windows reserved device names. Case-insensitive match; ``NUL`` resolves
# to the null device and ``NUL.json`` would silently succeed as a write
# target on Windows.
_WINDOWS_RESERVED: Final[frozenset[str]] = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})


# ── Source-name validator ────────────────────────────────────────────────────


def is_safe_source_name(name: str) -> bool:
    """True when *name* is safe to interpolate into a single-component filename.

    Rejects:
      - Non-string inputs.
      - Directory separators (``/`` or ``\\``).
      - ``:`` anywhere (covers Windows drive-relative ``C:evil``).
      - Anything failing ``[a-z0-9][a-z0-9_.-]{0,127}``.
      - Windows reserved device names (``CON``, ``PRN``, ``NUL``, ``COM1``...).
    """
    if not isinstance(name, str):
        return False
    if "/" in name or "\\" in name:
        return False
    # Windows drive-relative: ``C:evil`` -- contains ``:`` but no separator.
    if ":" in name:
        return False
    if not _SOURCE_NAME_RE.match(name):
        return False
    if name.upper() in _WINDOWS_RESERVED:
        return False
    return True


def validate_source_name(name: str, *, field: str = "source") -> None:
    """Raise ``ValueError`` when *name* fails :func:`is_safe_source_name`.

    ``field`` names the caller's argument for the error message.
    """
    if not is_safe_source_name(name):
        raise ValueError(f"invalid {field} name: {name!r}")


# ── Relpath validator ────────────────────────────────────────────────────────


def is_safe_relpath(root: Path, relpath: str) -> bool:
    """True when ``root / relpath`` resolves INSIDE ``root``.

    Rejects:
      - Non-string / empty input.
      - Absolute paths (``/foo``, ``\\foo``).
      - Windows drive-relative / drive-absolute (``C:foo``, ``C:\\foo``).
      - Any ``..`` component (even when it would land inside root).
      - Paths that escape root via symlinks after realpath resolution.

    We use ``os.path.realpath`` rather than ``Path.resolve(strict=True)``
    because a non-existent candidate is fine (the caller may be about
    to create it) -- what matters is the prefix after symlink
    resolution.
    """
    if not isinstance(relpath, str) or not relpath:
        return False

    if relpath.startswith(("/", "\\")):
        return False

    pure = PurePath(relpath)
    if pure.is_absolute():
        return False
    if PureWindowsPath(relpath).drive:
        # Covers ``C:\\foo`` (drive-absolute) AND ``C:foo`` (drive-relative).
        return False

    if ".." in pure.parts:
        return False

    try:
        candidate = Path(os.path.realpath(root / relpath))
        real_root = Path(os.path.realpath(root))
    except OSError:
        return False
    try:
        candidate.relative_to(real_root)
    except ValueError:
        return False
    return True


def validate_relpath(root: Path, relpath: str, *, field: str = "relpath") -> None:
    """Raise ``ValueError`` when relpath fails :func:`is_safe_relpath`."""
    if not is_safe_relpath(root, relpath):
        raise ValueError(
            f"invalid {field}: {relpath!r} does not resolve safely under {root!r}"
        )
