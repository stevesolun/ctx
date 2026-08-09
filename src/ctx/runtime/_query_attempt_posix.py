"""Bounded descriptor-relative scratch storage for native query hooks.

The pool has eight fixed stripes.  A stripe lock serializes both the durable
terminal check and its transient SQLite work.  Clean calls remove all scratch
state before a terminal may be committed; a process crash can therefore leave
at most one fixed-name orphan per stripe.  The next owner reconciles that
orphan without clocks or process-liveness guesses.

This module intentionally supports only platforms with POSIX directory-relative
operations.  Native Windows cleanup needs HANDLE identity, DACL, reparse-point,
and deletion-disposition support before recommend/shadow mode can claim the
same containment contract. Owner-private, rename-protected ancestry excludes
unrelated-user path substitution. A malicious process running as the same user
is outside this local cooperative-process boundary. Root identity is still
rechecked before and after every transient attempt so detectable root-path
replacement fails closed instead of producing a terminal delivery claim.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import secure_directory, supports_secure_directory_fds


ATTEMPT_STRIPE_COUNT: Final = 8
_DIRECTORY_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_MAX_ATTEMPT_ENTRIES: Final = 16
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_READ_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_ATTEMPT_FILES: Final = frozenset(
    {
        "engine.sqlite3",
        "engine.sqlite3-wal",
        "engine.sqlite3-shm",
        "engine.sqlite3-journal",
        "benefit.sqlite3",
        "benefit.sqlite3-wal",
        "benefit.sqlite3-shm",
        "benefit.sqlite3-journal",
        "benefit.sqlite3.lock",
    }
)
_INSTALL_LOCK_DIRECTORY: Final = "install-execution-locks"
_ROOT_ENTRY_NAMES: Final = frozenset(
    {
        *(f"slot-{index}" for index in range(ATTEMPT_STRIPE_COUNT)),
        *(f"slot-{index}-quarantine" for index in range(ATTEMPT_STRIPE_COUNT)),
    }
)


class QueryAttemptStorageError(RuntimeError):
    """Transient query scratch state could not be contained safely."""


class QueryAttemptStorageConflict(QueryAttemptStorageError):
    """Scratch storage contains unknown, linked, or replaced material."""


class QueryAttemptStorageUnsupported(QueryAttemptStorageError):
    """The platform lacks the native primitives required for secure cleanup."""


class QueryAttemptSlot:
    __slots__ = ("_root", "_root_fd", "_root_identity", "_slot_name")

    def __init__(
        self,
        *,
        root: Path,
        root_fd: int,
        root_identity: tuple[int, int],
        slot_name: str,
    ) -> None:
        self._root = root
        self._root_fd = root_fd
        self._root_identity = root_identity
        self._slot_name = slot_name

    @contextmanager
    def transient_directory(self) -> Iterator[Path]:
        """Create one exact scratch directory and purge it before returning."""

        _create_private_directory_at(self._root_fd, self._slot_name)
        _require_path_identity(self._root, self._root_identity)
        primary_error: BaseException | None = None
        try:
            yield self._root / self._slot_name
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                _quarantine_and_purge(
                    self._root_fd,
                    slot_name=self._slot_name,
                    quarantine_name=_quarantine_name(self._slot_name),
                )
                _validate_root_namespace(self._root_fd)
                _require_path_identity(self._root, self._root_identity)
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(
                    f"query attempt cleanup also failed with {type(cleanup_error).__name__}"
                )


class QueryAttemptPool:
    """Eight fixed scratch/lock stripes behind one acquisition interface."""

    __slots__ = (
        "_lock_root",
        "_lock_root_identity",
        "_root",
        "_root_identity",
        "_timeout",
    )

    def __init__(self, *, root: Path, lock_root: Path, timeout: float) -> None:
        _require_supported_platform()
        if not isinstance(root, Path) or not isinstance(lock_root, Path):
            raise TypeError("query attempt roots must be Paths")
        if type(timeout) not in {int, float} or timeout <= 0:
            raise ValueError("query attempt lock timeout must be positive")
        self._root = _canonical_directory_path(root)
        self._lock_root = _canonical_directory_path(lock_root)
        _validate_protected_ancestry(self._root)
        _validate_protected_ancestry(self._lock_root)
        self._root_identity = _directory_identity(self._root)
        self._lock_root_identity = _directory_identity(self._lock_root)
        self._timeout = float(timeout)

    @contextmanager
    def acquire(self, delivery_key_digest: str) -> Iterator[QueryAttemptSlot]:
        """Acquire one fixed stripe and reconcile its prior crash residue."""

        _require_digest(delivery_key_digest)
        stripe = int(delivery_key_digest[:16], 16) % ATTEMPT_STRIPE_COUNT
        slot_name = f"slot-{stripe}"
        lock_target = self._lock_root / f"query-attempt-stripe-{stripe}"
        with _open_bound_directory(self._lock_root, self._lock_root_identity):
            pass
        with secure_file_lock(lock_target, timeout=self._timeout):
            with _open_bound_directory(
                self._lock_root,
                self._lock_root_identity,
            ):
                pass
            with _open_bound_directory(self._root, self._root_identity) as root_fd:
                _recover_stripe(root_fd, slot_name=slot_name)
                yield QueryAttemptSlot(
                    root=self._root,
                    root_fd=root_fd,
                    root_identity=self._root_identity,
                    slot_name=slot_name,
                )


def query_attempt_pool_supported() -> bool:
    dir_fd_support = getattr(os, "supports_dir_fd", ())
    fd_support = getattr(os, "supports_fd", ())
    return (
        os.name != "nt"
        and callable(getattr(os, "geteuid", None))
        and supports_secure_directory_fds()
        and os.rmdir in dir_fd_support
        and os.scandir in fd_support
    )


def validate_query_state_root_parent(path: Path) -> None:
    """Authenticate existing ancestry before the delivery runtime writes state."""

    _require_supported_platform()
    if not isinstance(path, Path):
        raise TypeError("query state root must be a Path")
    absolute = Path(os.path.abspath(path))
    existing = absolute
    while not os.path.lexists(existing):
        parent = existing.parent
        if parent == existing:
            raise QueryAttemptStorageConflict("query state root has no existing directory ancestor")
        existing = parent
    canonical_existing = _canonical_directory_path(existing)
    metadata = os.stat(canonical_existing, follow_symlinks=False)
    if existing == absolute:
        _validate_private_directory(metadata)
    else:
        _validate_creation_parent(metadata)
    _validate_protected_ancestry(canonical_existing)


def _require_supported_platform() -> None:
    if not query_attempt_pool_supported():
        raise QueryAttemptStorageUnsupported(
            "secure query attempt cleanup is unavailable on this platform"
        )


def _directory_identity(path: Path) -> tuple[int, int]:
    with secure_directory(path, create=False) as directory:
        descriptor = _directory_fd(directory)
        metadata = os.fstat(descriptor)
        _validate_private_directory(metadata)
        current = os.stat(directory.path, follow_symlinks=False)
        if not os.path.samestat(metadata, current):
            raise QueryAttemptStorageConflict("query attempt root changed while opening")
        return metadata.st_dev, metadata.st_ino


def _canonical_directory_path(path: Path) -> Path:
    """Use the shared secure opener's narrow system-alias canonicalization."""

    with secure_directory(path, create=False) as directory:
        canonical = getattr(directory, "path", None)
        if not isinstance(canonical, Path):
            raise QueryAttemptStorageUnsupported(
                "canonical query attempt directory paths are unavailable"
            )
        return canonical


def _require_path_identity(path: Path, expected: tuple[int, int]) -> None:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt root path is unavailable") from exc
    _validate_private_directory(current)
    if (current.st_dev, current.st_ino) != expected:
        raise QueryAttemptStorageConflict("query attempt root path identity changed")


def _validate_protected_ancestry(root: Path) -> None:
    """Reject ancestors whose entries unrelated users may rename."""

    current = root
    try:
        current_metadata = os.stat(current, follow_symlinks=False)
        while current != Path(current.anchor):
            parent = current.parent
            parent_metadata = os.stat(parent, follow_symlinks=False)
            if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(parent_metadata.st_mode):
                raise QueryAttemptStorageConflict("query attempt ancestor is not a real directory")
            if parent_metadata.st_uid not in {0, os.geteuid()}:
                raise QueryAttemptStorageConflict("query attempt ancestor has an untrusted owner")
            writable = stat.S_IMODE(parent_metadata.st_mode) & 0o022
            sticky = bool(parent_metadata.st_mode & stat.S_ISVTX)
            if writable and not (sticky and current_metadata.st_uid in {0, os.geteuid()}):
                raise QueryAttemptStorageConflict("query attempt ancestor permits unsafe rename")
            current = parent
            current_metadata = parent_metadata
    except QueryAttemptStorageError:
        raise
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt ancestry cannot be authenticated") from exc


def _validate_creation_parent(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise QueryAttemptStorageConflict("query state ancestor is not a real directory")
    if metadata.st_uid not in {0, os.geteuid()}:
        raise QueryAttemptStorageConflict("query state ancestor has an untrusted owner")
    writable = stat.S_IMODE(metadata.st_mode) & 0o022
    if writable and not bool(metadata.st_mode & stat.S_ISVTX):
        raise QueryAttemptStorageConflict("query state ancestor permits unsafe creation")


@contextmanager
def _open_bound_directory(path: Path, expected: tuple[int, int]) -> Iterator[int]:
    with secure_directory(path, create=False) as directory:
        descriptor = _directory_fd(directory)
        metadata = os.fstat(descriptor)
        _validate_private_directory(metadata)
        if (metadata.st_dev, metadata.st_ino) != expected:
            raise QueryAttemptStorageConflict("query attempt root identity changed")
        yield descriptor


def _recover_stripe(root_fd: int, *, slot_name: str) -> None:
    quarantine_name = _quarantine_name(slot_name)
    if _stat_optional(root_fd, quarantine_name) is not None:
        _purge_named_attempt(root_fd, quarantine_name)
    if _stat_optional(root_fd, slot_name) is not None:
        _quarantine_and_purge(
            root_fd,
            slot_name=slot_name,
            quarantine_name=quarantine_name,
        )
    _validate_root_namespace(root_fd)


def _quarantine_name(slot_name: str) -> str:
    return f"{slot_name}-quarantine"


def _validate_root_namespace(root_fd: int) -> None:
    entries = _bounded_entry_names(root_fd, maximum=len(_ROOT_ENTRY_NAMES))
    if set(entries) - _ROOT_ENTRY_NAMES:
        raise QueryAttemptStorageConflict("query attempt root contains unknown entries")


def _quarantine_and_purge(
    root_fd: int,
    *,
    slot_name: str,
    quarantine_name: str,
) -> None:
    if _stat_optional(root_fd, quarantine_name) is not None:
        raise QueryAttemptStorageConflict("query attempt quarantine is already occupied")
    source_fd, source_metadata = _open_exact_private_directory(root_fd, slot_name)
    try:
        os.rename(
            slot_name,
            quarantine_name,
            src_dir_fd=root_fd,
            dst_dir_fd=root_fd,
        )
        renamed = os.stat(quarantine_name, dir_fd=root_fd, follow_symlinks=False)
        if not os.path.samestat(source_metadata, renamed):
            raise QueryAttemptStorageConflict("query attempt changed during quarantine")
        os.fsync(root_fd)
    except QueryAttemptStorageError:
        raise
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt quarantine failed") from exc
    finally:
        os.close(source_fd)
    _purge_named_attempt(root_fd, quarantine_name)


def _purge_named_attempt(root_fd: int, name: str) -> None:
    attempt_fd, attempt_metadata = _open_exact_private_directory(root_fd, name)
    try:
        entries = _bounded_entry_names(attempt_fd, maximum=_MAX_ATTEMPT_ENTRIES)
        unknown = set(entries) - _ATTEMPT_FILES - {_INSTALL_LOCK_DIRECTORY}
        if unknown:
            raise QueryAttemptStorageConflict("query attempt contains unknown entries")

        file_metadata: dict[str, os.stat_result] = {}
        install_lock_metadata: os.stat_result | None = None
        for entry in entries:
            if entry == _INSTALL_LOCK_DIRECTORY:
                install_lock_metadata = _require_empty_private_directory(attempt_fd, entry)
                continue
            metadata = os.stat(entry, dir_fd=attempt_fd, follow_symlinks=False)
            _validate_private_file(metadata)
            file_metadata[entry] = metadata

        for entry in sorted(file_metadata):
            _unlink_exact_file(attempt_fd, entry, file_metadata[entry])
        if install_lock_metadata is not None:
            current_nested = os.stat(
                _INSTALL_LOCK_DIRECTORY,
                dir_fd=attempt_fd,
                follow_symlinks=False,
            )
            if not os.path.samestat(install_lock_metadata, current_nested):
                raise QueryAttemptStorageConflict("query install lock directory changed")
            os.rmdir(_INSTALL_LOCK_DIRECTORY, dir_fd=attempt_fd)
        os.fsync(attempt_fd)
    except QueryAttemptStorageError:
        raise
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt purge failed") from exc
    finally:
        os.close(attempt_fd)

    current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    if not os.path.samestat(attempt_metadata, current):
        raise QueryAttemptStorageConflict("query attempt changed before removal")
    try:
        os.rmdir(name, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt directory removal failed") from exc


def _create_private_directory_at(root_fd: int, name: str) -> None:
    if _stat_optional(root_fd, name) is not None:
        raise QueryAttemptStorageConflict("query attempt slot is already occupied")
    try:
        os.mkdir(name, mode=_DIRECTORY_MODE, dir_fd=root_fd)
        created = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        _validate_private_directory(created)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            if not os.path.samestat(created, os.fstat(descriptor)):
                raise QueryAttemptStorageConflict("query attempt changed while creating")
        finally:
            os.close(descriptor)
        os.fsync(root_fd)
    except QueryAttemptStorageError:
        raise
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt creation failed") from exc


def _require_empty_private_directory(parent_fd: int, name: str) -> os.stat_result:
    descriptor, metadata = _open_exact_private_directory(parent_fd, name)
    try:
        _bounded_entry_names(descriptor, maximum=0)
    finally:
        os.close(descriptor)
    return metadata


def _bounded_entry_names(directory_fd: int, *, maximum: int) -> tuple[str, ...]:
    """Enumerate at most ``maximum + 1`` entries from one pinned directory."""

    if type(maximum) is not int or maximum < 0:
        raise ValueError("maximum directory entries must be a non-negative integer")
    names: list[str] = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if len(names) == maximum:
                    raise QueryAttemptStorageConflict("query attempt entry budget is exceeded")
                names.append(entry.name)
    except QueryAttemptStorageError:
        raise
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt enumeration failed") from exc
    return tuple(names)


def _unlink_exact_file(parent_fd: int, name: str, expected: os.stat_result) -> None:
    descriptor = -1
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        _validate_private_file(opened)
        if not os.path.samestat(expected, opened):
            raise QueryAttemptStorageConflict("query attempt file changed while opening")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not os.path.samestat(opened, current):
            raise QueryAttemptStorageConflict("query attempt file changed before removal")
        os.unlink(name, dir_fd=parent_fd)
        unlinked = os.fstat(descriptor)
        if unlinked.st_nlink != opened.st_nlink - 1:
            raise QueryAttemptStorageConflict("query attempt file changed during removal")
        if _stat_optional(parent_fd, name) is not None:
            raise QueryAttemptStorageConflict("query attempt file name was replaced")
    except QueryAttemptStorageError:
        raise
    except OSError as exc:
        raise QueryAttemptStorageError("query attempt file removal failed") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _open_exact_private_directory(parent_fd: int, name: str) -> tuple[int, os.stat_result]:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _validate_private_directory(before)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        _validate_private_directory(opened)
        if not os.path.samestat(before, opened):
            raise QueryAttemptStorageConflict("query attempt directory changed while opening")
        return descriptor, opened
    except QueryAttemptStorageError:
        if descriptor != -1:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor != -1:
            os.close(descriptor)
        raise QueryAttemptStorageError("query attempt directory cannot be opened") from exc


def _stat_optional(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _validate_private_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise QueryAttemptStorageConflict("query attempt entry must be a real directory")
    if metadata.st_uid != os.geteuid():
        raise QueryAttemptStorageConflict("query attempt directory has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != _DIRECTORY_MODE:
        raise QueryAttemptStorageConflict("query attempt directory must be owner-private")


def _validate_private_file(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise QueryAttemptStorageConflict("query attempt entry must be a regular file")
    if metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
        raise QueryAttemptStorageConflict("query attempt file identity is unsafe")
    if stat.S_IMODE(metadata.st_mode) != _FILE_MODE:
        raise QueryAttemptStorageConflict("query attempt file must be owner-private")


def _directory_fd(directory: object) -> int:
    descriptor = getattr(directory, "_directory_fd", None)
    if not isinstance(descriptor, int):
        raise QueryAttemptStorageUnsupported(
            "descriptor-relative query attempt cleanup is unavailable"
        )
    return descriptor


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError("delivery_key_digest must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "ATTEMPT_STRIPE_COUNT",
    "QueryAttemptPool",
    "QueryAttemptSlot",
    "QueryAttemptStorageConflict",
    "QueryAttemptStorageError",
    "QueryAttemptStorageUnsupported",
    "query_attempt_pool_supported",
    "validate_query_state_root_parent",
]
