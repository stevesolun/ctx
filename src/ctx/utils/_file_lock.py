"""
_file_lock.py -- Cross-platform advisory file lock.

Used by the toolbox and skill-health modules to serialize read-modify-write
cycles on shared config/manifest files (e.g. ~/.claude/skill-manifest.json,
~/.claude/toolbox-runs/<hash>.verdict.json) so that concurrent agent sessions
do not clobber each other's writes.

Usage:

    with file_lock(manifest_path):
        data = json.loads(manifest_path.read_text())
        data["load"].append(...)
        manifest_path.write_text(json.dumps(data))

The lock is advisory -- it only blocks other callers that also use ``file_lock``.
It does not protect against processes that ignore locking.

On POSIX we use fcntl.flock (whole-file exclusive). On Windows we use
msvcrt.locking on the companion .lock file so we don't hold a handle to the
file the caller is about to replace.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ctx.utils._fs_utils import secure_directory, supports_secure_directory_fds

_SECURE_LOCK_OPEN_ATTEMPTS = 8
_SECURE_LOCK_OPEN_RETRY_SECONDS = 0.005

if sys.platform == "win32":
    import msvcrt  # type: ignore[import-not-found]
else:
    import fcntl  # type: ignore[import-not-found]


@contextmanager
def file_lock(target: Path, timeout: float = 10.0) -> Iterator[None]:
    """Acquire an exclusive advisory lock on ``target``.

    Creates ``target.with_suffix(target.suffix + '.lock')`` as the lock
    file so we don't hold a handle to the target itself (which callers
    typically replace via ``os.replace``). The lock file is left on disk
    after release -- it's cheap and avoids a race where two processes
    both try to create-and-lock at once.

    Raises TimeoutError if the lock cannot be acquired within ``timeout``.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    deadline = time.monotonic() + max(0.0, timeout)
    stable_fd = -1
    if sys.platform != "win32":
        stable_fd = _open_stable_path_lock(target)
        try:
            _acquire(stable_fd, max(0.0, deadline - time.monotonic()))
        except BaseException:
            os.close(stable_fd)
            raise
    directory_fd = -1
    try:
        if sys.platform != "win32":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(str(target.parent), directory_flags)
            opened_directory = os.fstat(directory_fd)
            current_directory = os.stat(target.parent)
            if not os.path.samestat(opened_directory, current_directory):
                raise ValueError("file lock directory changed while acquiring the lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(lock_path), flags, 0o600)
        try:
            opened = os.fstat(fd)
            current = os.stat(lock_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or not os.path.samestat(opened, current)
            ):
                raise ValueError("file lock companion violates its integrity contract")
            _acquire(fd, max(0.0, deadline - time.monotonic()))
            try:
                current = os.stat(lock_path, follow_symlinks=False)
                if not os.path.samestat(opened, current):
                    raise ValueError("file lock companion changed while acquiring the lock")
                yield
            finally:
                _release(fd)
        finally:
            os.close(fd)
    finally:
        if directory_fd != -1:
            os.close(directory_fd)
        if stable_fd != -1:
            _release(stable_fd)
            os.close(stable_fd)


def _open_stable_path_lock(target: Path) -> int:
    """Open a path-keyed lock outside a replaceable target directory."""

    user_identity = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
    root = Path(tempfile.gettempdir()) / f"ctx-file-locks-{user_identity}"
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("stable file-lock root violates its integrity contract")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ValueError("stable file-lock root is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("stable file-lock root must use owner-only permissions")
    normalized = os.path.normcase(os.path.abspath(target))
    digest = hashlib.sha256(os.fsencode(normalized)).hexdigest()
    lock_path = root / f"{digest}.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    opened = os.fstat(descriptor)
    current = lock_path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not os.path.samestat(opened, current)
    ):
        os.close(descriptor)
        raise ValueError("stable file lock violates its integrity contract")
    return descriptor


@contextmanager
def secure_file_lock(target: Path, timeout: float = 10.0) -> Iterator[None]:
    """Lock a child of an already-existing, securely pinned directory.

    Unlike :func:`file_lock`, this variant never creates parent directories and
    will not follow a symlinked parent or companion lock file. It is intended
    for security-sensitive stores that validate and create their directory
    structure before acquiring the cooperating-process lock.
    """

    target = Path(target)
    lock_name = target.with_suffix(target.suffix + ".lock").name
    with secure_directory(target.parent, create=False) as directory:
        if not supports_secure_directory_fds():  # pragma: no cover - Windows only
            with file_lock(directory.path / target.name, timeout=timeout):
                yield
            return

        directory_fd = directory._directory_fd
        if directory_fd is None:
            raise RuntimeError("secure directory descriptor is unavailable")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = _open_secure_lock_file(directory_fd, lock_name, flags)
        try:
            metadata = os.fstat(fd)
            try:
                current = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise ValueError("secure lock file cannot be authenticated") from None
            if (
                not stat.S_ISREG(metadata.st_mode)
                or not os.path.samestat(metadata, current)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise ValueError("secure lock file violates its integrity contract")
            _acquire(fd, timeout)
            try:
                try:
                    current = os.stat(lock_name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    raise ValueError("secure lock file changed while acquiring the lock") from None
                if not os.path.samestat(metadata, current):
                    raise ValueError("secure lock file changed while acquiring the lock")
                yield
            finally:
                _release(fd)
        finally:
            os.close(fd)


def _open_secure_lock_file(directory_fd: int, lock_name: str, flags: int) -> int:
    """Open/create a pinned lock child, tolerating Darwin's transient ENOENT race."""

    for attempt in range(_SECURE_LOCK_OPEN_ATTEMPTS):
        try:
            return os.open(lock_name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError) or attempt + 1 >= _SECURE_LOCK_OPEN_ATTEMPTS:
                raise ValueError("secure lock file cannot be opened safely") from None
            time.sleep(_SECURE_LOCK_OPEN_RETRY_SECONDS)
    raise AssertionError("secure lock open retry loop exhausted unexpectedly")


def _acquire(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if sys.platform == "win32":
                # Lock 1 byte at offset 0 non-blockingly.
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except (OSError, BlockingIOError):
            if time.monotonic() >= deadline:
                raise TimeoutError("file_lock: timed out acquiring lock")
            time.sleep(0.05)


def _release(fd: int) -> None:
    try:
        if sys.platform == "win32":
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
