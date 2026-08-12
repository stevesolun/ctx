"""
tests/test_fs_utils_permissions.py -- Phase 6a regression: atomic_write_* sets 0o600.

Phase 2.5 security reviewer flagged that ``tempfile.mkstemp`` defaults to
0o600 but ``os.replace`` can inherit the destination's more permissive mode.
Phase 6a adds an explicit chmod before the replace to pin the mode. These tests
pin that invariant and require mode hardening to fail closed.
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

from ctx.utils._fs_utils import atomic_write_bytes, atomic_write_json, atomic_write_text  # noqa: E402
import ctx.utils._fs_utils as fs_utils  # noqa: E402


def _mode_bits(p: Path) -> int:
    """Return the low 9 permission bits (owner/group/other rwx)."""
    return stat.S_IMODE(p.stat().st_mode)


def test_atomic_write_text_creates_file_with_0o600(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    atomic_write_text(target, "shhh")
    assert _mode_bits(target) == 0o600


def test_atomic_write_bytes_creates_file_with_0o600(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    atomic_write_bytes(target, b"\x00\x01\x02")
    assert _mode_bits(target) == 0o600


def test_atomic_write_json_creates_file_with_0o600(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    atomic_write_json(target, {"hello": "world"})
    assert _mode_bits(target) == 0o600


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


def test_atomic_write_fails_closed_when_private_mode_cannot_be_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "secret.txt"

    def reject_chmod(_path: str, _mode: int) -> None:
        raise OSError("chmod unavailable")

    monkeypatch.setattr(fs_utils.os, "chmod", reject_chmod)

    with pytest.raises(OSError, match="chmod unavailable"):
        atomic_write_text(target, "secret")

    assert not target.exists()
