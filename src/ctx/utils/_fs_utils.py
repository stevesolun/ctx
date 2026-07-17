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
``PermissionError`` if the destination is held open; a bounded retry loop
absorbs transient contention before re-raising.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = [
    "atomic_write_text",
    "atomic_write_bytes",
    "atomic_write_json",
    "ensure_secure_directory",
    "reject_symlink_path",
    "safe_atomic_write_text",
    "SecureDirectoryExistsError",
    "secure_directory",
    "supports_secure_directory_fds",
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
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_TEMP_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_WINDOWS_FILE_READ_ATTRIBUTES = 0x80
_WINDOWS_FILE_SHARE_READ = 0x1
_WINDOWS_FILE_SHARE_WRITE = 0x2
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class SecureDirectoryExistsError(FileExistsError):
    """Raised when exclusive secure directory creation finds an existing path."""


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
    with secure_directory(path.parent, create=True) as directory:
        directory.atomic_write_text(path.name, text, encoding=encoding)


def ensure_secure_directory(path: Path) -> None:
    """Create a directory tree without following symlinked components."""
    with secure_directory(path, create=True):
        return


def supports_secure_directory_fds() -> bool:
    supported = getattr(os, "supports_dir_fd", ())
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in supported
        and os.mkdir in supported
        and os.rename in supported
        and os.stat in supported
        and os.unlink in supported
    )


class _SecureDirectory:
    def __init__(self, path: Path, directory_fd: int | None) -> None:
        self.path = path
        self._directory_fd = directory_fd

    def exists(self, name: str) -> bool:
        _validate_child_name(name)
        if self._directory_fd is not None:
            try:
                os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            return True
        return _lstat_optional(self.path / name) is not None

    def atomic_write_text(self, name: str, text: str, encoding: str = "utf-8") -> None:
        _validate_child_name(name)
        if self._directory_fd is not None:
            _atomic_write_text_at(self, name, text, encoding)
            return
        _atomic_write_text_guarded_path(self, name, text, encoding)


@contextmanager
def secure_directory(
    path: Path,
    *,
    create: bool = False,
    exclusive: bool = False,
) -> Iterator[_SecureDirectory]:
    """Pin a real directory while callers perform child operations."""
    absolute = _absolute_anchored_path(path)
    if supports_secure_directory_fds():
        if exclusive:
            if absolute == Path(absolute.anchor):
                raise ValueError("cannot create a filesystem root exclusively")
            parent_fd = _open_anchored_directory(absolute.parent, create=create)
            if parent_fd is None:
                raise FileNotFoundError(absolute.parent)
            directory_fd: int | None = None
            try:
                try:
                    os.mkdir(absolute.name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError as exc:
                    raise SecureDirectoryExistsError(absolute) from exc
                created = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
                directory_fd = os.open(
                    absolute.name,
                    _DIRECTORY_OPEN_FLAGS,
                    dir_fd=parent_fd,
                )
                if not os.path.samestat(created, os.fstat(directory_fd)):
                    raise ValueError(f"directory {absolute} changed while opening")
                yield _SecureDirectory(absolute, directory_fd)
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
                os.close(parent_fd)
            return

        directory_fd = _open_anchored_directory(absolute, create=create)
        if directory_fd is None:
            raise FileNotFoundError(absolute)
        try:
            yield _SecureDirectory(absolute, directory_fd)
        finally:
            os.close(directory_fd)
        return

    if os.name == "nt":
        if exclusive:
            if absolute == Path(absolute.anchor):
                raise ValueError("cannot create a filesystem root exclusively")
            with _guard_windows_directories(absolute.parent, create=create):
                try:
                    absolute.mkdir()
                except FileExistsError as exc:
                    raise SecureDirectoryExistsError(absolute) from exc
                _validate_real_directory(absolute)
                handle = _open_windows_directory_guard(absolute)
                try:
                    _validate_real_directory(absolute)
                    yield _SecureDirectory(absolute, None)
                finally:
                    _close_windows_directory_guard(handle)
            return
        with _guard_windows_directories(absolute, create=create):
            yield _SecureDirectory(absolute, None)
        return

    raise RuntimeError(
        "secure directory access unavailable: this platform must provide "
        "directory-relative filesystem operations or Windows directory handles"
    )


def _absolute_anchored_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    if sys.platform == "darwin":
        for alias, target in _DARWIN_SYSTEM_SYMLINKS.items():
            try:
                relative = absolute.relative_to(alias)
            except ValueError:
                continue
            return target / relative
    return absolute


def _open_anchored_directory(path: Path, *, create: bool) -> int | None:
    absolute = _absolute_anchored_path(path)
    current_fd = os.open(absolute.anchor, _DIRECTORY_OPEN_FLAGS)
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None


def _is_reparse_point(path: Path, metadata: os.stat_result) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return bool(
        getattr(metadata, "st_file_attributes", 0) & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    ) or (callable(is_junction) and is_junction())


def _validate_real_directory(path: Path) -> bool:
    metadata = _lstat_optional(path)
    if metadata is None:
        return False
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(path, metadata)
    ):
        raise ValueError(f"directory {path} must be a real directory")
    return True


def _validate_destination(path: Path, metadata: os.stat_result | None) -> None:
    if metadata is None:
        return
    if (
        stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(path, metadata)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ValueError(f"destination {path} must be a regular file")


def _validate_child_name(name: str) -> None:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"unsafe child name: {name!r}")


def _atomic_write_text_at(
    directory: _SecureDirectory,
    name: str,
    text: str,
    encoding: str,
) -> None:
    directory_fd = directory._directory_fd
    if directory_fd is None:
        raise RuntimeError("directory descriptor unavailable")
    destination = directory.path / name
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        metadata = None
    _validate_destination(destination, metadata)
    temp_name = f".{name}.{secrets.token_hex(8)}.tmp"
    temp_fd = -1
    try:
        temp_fd = os.open(temp_name, _TEMP_OPEN_FLAGS, _FILE_MODE_PRIVATE, dir_fd=directory_fd)
        with os.fdopen(temp_fd, "w", encoding=encoding, newline="") as handle:
            temp_fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        _validate_destination(destination, metadata)
        os.replace(temp_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temp_name = ""
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        if temp_fd != -1:
            os.close(temp_fd)
        if temp_name:
            try:
                os.unlink(temp_name, dir_fd=directory_fd)
            except OSError:
                pass


def _atomic_write_text_guarded_path(
    directory: _SecureDirectory,
    name: str,
    text: str,
    encoding: str,
) -> None:
    destination = directory.path / name
    _validate_destination(destination, _lstat_optional(destination))
    temp_path = directory.path / f".{name}.{secrets.token_hex(8)}.tmp"
    temp_pending = True
    temp_fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _FILE_MODE_PRIVATE)
    try:
        with os.fdopen(temp_fd, "w", encoding=encoding, newline="") as handle:
            temp_fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _chmod_private(str(temp_path))
        _validate_destination(destination, _lstat_optional(destination))
        _replace_with_retry(str(temp_path), destination)
        temp_pending = False
    finally:
        if temp_fd != -1:
            os.close(temp_fd)
        if temp_pending:
            _unlink_silent(str(temp_path))


def _open_windows_directory_guard(path: Path) -> int:  # pragma: no cover - Windows only
    import ctypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        _WINDOWS_FILE_READ_ATTRIBUTES,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        error = getattr(ctypes, "get_last_error")()
        message = getattr(ctypes, "FormatError")(error)
        raise OSError(error, f"cannot guard directory {path}: {message}")
    return int(handle)


def _close_windows_directory_guard(handle: int) -> None:  # pragma: no cover - Windows only
    import ctypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


def _windows_guard_paths(path: Path) -> list[Path]:
    absolute = Path(os.path.abspath(path))
    paths = [Path(absolute.anchor)]
    for component in absolute.parts[1:]:
        paths.append(paths[-1] / component)
    return paths


@contextmanager
def _guard_windows_directories(path: Path, *, create: bool) -> Iterator[None]:
    handles: list[int] = []
    try:
        for directory in _windows_guard_paths(path):
            if not _validate_real_directory(directory):
                if not create:
                    raise FileNotFoundError(directory)
                directory.mkdir()
                _validate_real_directory(directory)
            handles.append(_open_windows_directory_guard(directory))
            _validate_real_directory(directory)
        yield
    finally:
        for handle in reversed(handles):
            _close_windows_directory_guard(handle)


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
