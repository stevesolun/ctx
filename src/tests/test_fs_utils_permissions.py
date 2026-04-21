"""
tests/test_fs_utils_permissions.py -- Phase 6a regression: atomic_write_* sets 0o600.

Phase 2.5 security reviewer flagged that ``tempfile.mkstemp`` defaults to
0o600 on POSIX but ``os.replace`` can inherit the destination's more
permissive mode. Phase 6a adds an explicit chmod before the replace to
pin the mode across platforms. This test pins that invariant.

On Windows the chmod is a best-effort no-op (unix bits aren't mapped),
so the permission assertions skip on that platform.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from _fs_utils import atomic_write_bytes, atomic_write_json, atomic_write_text  # noqa: E402


_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows filesystems don't honour unix permission bits",
)


def _mode_bits(p: Path) -> int:
    """Return the low 9 permission bits (owner/group/other rwx)."""
    return stat.S_IMODE(p.stat().st_mode)


@_POSIX_ONLY
def test_atomic_write_text_creates_file_with_0o600(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    atomic_write_text(target, "shhh")
    assert _mode_bits(target) == 0o600


@_POSIX_ONLY
def test_atomic_write_bytes_creates_file_with_0o600(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    atomic_write_bytes(target, b"\x00\x01\x02")
    assert _mode_bits(target) == 0o600


@_POSIX_ONLY
def test_atomic_write_json_creates_file_with_0o600(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"hello": "world"})
    assert _mode_bits(target) == 0o600


@_POSIX_ONLY
def test_overwrite_pins_permissions_to_0o600(tmp_path: Path) -> None:
    # Regression for the exact Phase 2.5 finding: os.replace onto an
    # existing, more-permissive file must result in 0o600, not the
    # destination's pre-existing mode.
    target = tmp_path / "replaced.txt"
    target.write_text("original", encoding="utf-8")
    os.chmod(target, 0o644)  # world-readable, simulating the bug scenario
    assert _mode_bits(target) == 0o644  # sanity

    atomic_write_text(target, "replaced")
    assert _mode_bits(target) == 0o600, (
        "atomic_write_text must chmod the tmp to 0o600 before os.replace "
        "so the final file is owner-only, regardless of prior dest mode"
    )


def test_atomic_write_text_still_works_on_windows_without_chmod_error(
    tmp_path: Path,
) -> None:
    # Cross-platform smoke: the write must succeed even though Windows
    # ignores most unix permission bits. The chmod attempt must not
    # raise — the helper catches OSError silently.
    target = tmp_path / "smoke.txt"
    atomic_write_text(target, "cross-platform works")
    assert target.read_text(encoding="utf-8") == "cross-platform works"
