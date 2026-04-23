"""
test_safe_name.py -- unit tests for _safe_name validators.

Pins the security-auditor H-2 (relpath traversal) and H-3 (Windows drive-
relative source name) findings. These validators are shared between
mcp_canonical_index (relpath) and mcp_enrich / mcp_ingest (source name),
so regressions here would reopen both attack surfaces simultaneously.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from _safe_name import (
    is_safe_relpath,
    is_safe_source_name,
    validate_relpath,
    validate_source_name,
)


# ────────────────────────────────────────────────────────────────────
# Source-name validator
# ────────────────────────────────────────────────────────────────────


class TestIsSafeSourceName:
    @pytest.mark.parametrize("name", [
        "pulsemcp",
        "awesome-mcp",
        "glama",
        "mcp-get",
        "foo.v2",
        "a",
        "0",
        "a" * 128,  # exactly the max length
    ])
    def test_accepts_valid_names(self, name: str):
        assert is_safe_source_name(name)

    @pytest.mark.parametrize("name", [
        "",              # empty
        "-pulsemcp",     # leading hyphen
        ".pulsemcp",     # leading dot (hidden file)
        "_pulsemcp",     # leading underscore (disallowed: first char must be [a-z0-9])
        "PULSEMCP",      # uppercase (regex is lowercase)
        "pulse mcp",     # space
        "pulse$mcp",     # shell metachar
        "a" * 129,       # too long
    ])
    def test_rejects_malformed(self, name: str):
        assert not is_safe_source_name(name)

    @pytest.mark.parametrize("name", [
        "foo/bar",       # directory separator (posix)
        "foo\\bar",      # directory separator (windows)
        "../etc/passwd",
        "..",
        "/etc/passwd",
        "\\windows\\system32",
    ])
    def test_rejects_path_separators(self, name: str):
        """H-3 regression — traversal-shaped names must be blocked."""
        assert not is_safe_source_name(name)

    @pytest.mark.parametrize("name", [
        "C:evil",         # Windows drive-relative (the H-3 exploit)
        "C:",
        "c:foo",
        "C:\\Windows",    # drive-absolute
        "Z:payload.json",
    ])
    def test_rejects_windows_drive_relative(self, name: str):
        """H-3 regression — Windows drive-relative / absolute must be blocked.

        Prior validator only checked ``/ \\ .``; ``C:evil`` passed through
        and landed wherever drive C's CWD happened to be at the time.
        """
        assert not is_safe_source_name(name)

    @pytest.mark.parametrize("name", [
        "CON", "con", "Con",      # case-insensitive
        "PRN",
        "NUL",
        "AUX",
        "COM1", "COM9",
        "LPT1", "LPT9",
    ])
    def test_rejects_windows_reserved_device_names(self, name: str):
        """``NUL.json`` on Windows writes to the null device silently."""
        assert not is_safe_source_name(name)

    # Strix vuln-0003: Windows device-name reservation survives a suffix
    # or trailing dot/space. `con.txt`, `aux.md`, `nul.`, `com1.log`, and
    # `lpt1.txt` all still resolve to the device endpoint on Windows.
    @pytest.mark.parametrize("name", [
        "con.txt", "CON.TXT", "Con.Log",
        "aux.md", "AUX.json",
        "prn.bak",
        "nul.",          # trailing dot — Windows strips it then matches NUL
        "com1.log", "COM9.cfg",
        "lpt1.txt", "lpt9.ini",
    ])
    def test_rejects_reserved_name_with_suffix_or_trailing_dot(
        self, name: str,
    ) -> None:
        assert not is_safe_source_name(name), (
            f"{name!r} slipped past the Windows reserved-name normalize"
        )

    def test_rejects_non_string(self):
        assert not is_safe_source_name(None)              # type: ignore[arg-type]
        assert not is_safe_source_name(123)               # type: ignore[arg-type]
        assert not is_safe_source_name(["pulsemcp"])      # type: ignore[arg-type]


class TestValidateSourceName:
    def test_raises_on_invalid(self):
        with pytest.raises(ValueError, match="invalid source name"):
            validate_source_name("C:evil")

    def test_custom_field_in_error_message(self):
        with pytest.raises(ValueError, match="invalid mcp_source name"):
            validate_source_name("../etc/passwd", field="mcp_source")

    def test_silent_on_valid(self):
        # Must not raise.
        validate_source_name("pulsemcp")


# ────────────────────────────────────────────────────────────────────
# Relpath validator — the H-2 attack surface
# ────────────────────────────────────────────────────────────────────


class TestIsSafeRelpath:
    @pytest.fixture()
    def root(self, tmp_path: Path) -> Path:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "foo.md").write_text("body", encoding="utf-8")
        (tmp_path / "subdir" / "nested").mkdir(parents=True)
        return tmp_path

    @pytest.mark.parametrize("rel", [
        "a/foo.md",                    # shallow
        "subdir/nested/x.md",          # nested
        "a/b/c/d/e/f/deep.md",         # very nested — no need to exist
        "hooks/backup_on_change.py",
    ])
    def test_accepts_safe_relative_paths(self, root: Path, rel: str):
        assert is_safe_relpath(root, rel)

    @pytest.mark.parametrize("rel", [
        "",
        "../etc/passwd",
        "../../../../tmp/evil",
        "a/../../../outside",
        "..",
        "a/../..",
    ])
    def test_rejects_traversal(self, root: Path, rel: str):
        """H-2 regression — ``..`` components in the stored relpath."""
        assert not is_safe_relpath(root, rel)

    @pytest.mark.parametrize("rel", [
        "/etc/passwd",
        "/foo",
        "\\windows\\system32",
        "\\foo",
    ])
    def test_rejects_absolute(self, root: Path, rel: str):
        assert not is_safe_relpath(root, rel)

    @pytest.mark.parametrize("rel", [
        "C:evil",          # drive-relative: resolves against drive C's CWD
        "C:/foo",
        "C:\\foo",
        "Z:payload.md",
    ])
    def test_rejects_windows_drive_relative(self, root: Path, rel: str):
        """H-3 cousin — drive-relative in a stored relpath is always wrong."""
        assert not is_safe_relpath(root, rel)

    def test_rejects_non_string(self, root: Path):
        assert not is_safe_relpath(root, None)            # type: ignore[arg-type]
        assert not is_safe_relpath(root, 42)              # type: ignore[arg-type]

    def test_empty_string_rejected(self, root: Path):
        assert not is_safe_relpath(root, "")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-only: symlink requires admin on Windows")
    def test_rejects_symlink_escape(self, tmp_path: Path):
        """A symlink INSIDE root that points OUTSIDE root must be caught.

        Even with no ``..`` in the relpath, following a symlink to
        /etc/passwd is traversal.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret").write_text("sensitive", encoding="utf-8")

        root = tmp_path / "root"
        root.mkdir()
        # Symlink root/escape -> outside
        try:
            (root / "escape").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("Platform does not support symlinks in this context")

        # "escape/secret" is a relative path with no ``..``, but after
        # symlink resolution it lands at outside/secret.
        assert not is_safe_relpath(root, "escape/secret")


class TestValidateRelpath:
    def test_raises_on_traversal(self, tmp_path: Path):
        with pytest.raises(ValueError, match="invalid relpath"):
            validate_relpath(tmp_path, "../../etc/passwd")

    def test_custom_field_in_error_message(self, tmp_path: Path):
        with pytest.raises(ValueError, match="invalid entity_path"):
            validate_relpath(tmp_path, "C:evil", field="entity_path")

    def test_silent_on_valid(self, tmp_path: Path):
        validate_relpath(tmp_path, "a/foo.md")
