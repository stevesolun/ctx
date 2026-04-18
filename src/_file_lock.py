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

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
    lock_path.touch(exist_ok=True)

    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        _acquire(fd, timeout)
        try:
            yield
        finally:
            _release(fd)
    finally:
        os.close(fd)


def _acquire(fd: int, timeout: float) -> None:
    import time
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
