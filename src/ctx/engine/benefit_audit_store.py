"""Private durable storage for full schema-v3 net-benefit audit results.

Replay and engine state retain only the compact digest-bound audit reference.
This store keeps the full, privacy-safe candidate assessments under their exact
``BenefitSelectionResult.result_digest`` for later audit and revalidation.
Rows are immutable: an existing digest is either byte-identical or rejected.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ctx.engine.benefit import (
    MAX_BENEFIT_RESULT_JSON_BYTES,
    BenefitSelectionResult,
    BenefitValidationError,
)
from ctx.engine.planning_v3 import BenefitAuditStoreUnavailable
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import ensure_secure_directory, reject_symlink_path, secure_directory


_PRIVATE_FILE_MODE = 0o600
_BUSY_TIMEOUT_MS = 30_000
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS benefit_audit_results (
    result_digest   TEXT PRIMARY KEY NOT NULL,
    result_json     BLOB NOT NULL,
    byte_length     INTEGER NOT NULL CHECK (byte_length >= 0),
    content_digest  TEXT NOT NULL
) WITHOUT ROWID;
"""
_TABLE_SQL = (
    _SCHEMA.strip().removesuffix(";").replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)
)
_EXPECTED_COLUMNS = {
    "result_digest": ("TEXT", 1, 1),
    "result_json": ("BLOB", 1, 0),
    "byte_length": ("INTEGER", 1, 0),
    "content_digest": ("TEXT", 1, 0),
}
_SELECT_RESULT = """
SELECT CASE
           WHEN typeof(result_json) = 'blob' AND length(result_json) <= ?
           THEN result_json
           ELSE NULL
       END AS result_json,
       length(result_json) AS actual_byte_length,
       CASE
           WHEN typeof(byte_length) = 'integer' THEN byte_length
           ELSE NULL
       END AS byte_length,
       CASE
           WHEN typeof(content_digest) = 'text' AND length(content_digest) = 64
           THEN content_digest
           ELSE NULL
       END AS content_digest
  FROM benefit_audit_results
 WHERE result_digest = ?
"""


class BenefitAuditStoreError(RuntimeError):
    """Base class for non-operational audit-store integrity failures."""


class BenefitAuditDigestCollision(BenefitAuditStoreError):
    """A result digest is already bound to different canonical bytes."""


class BenefitAuditCorruption(BenefitAuditStoreError):
    """Persisted audit bytes or database structure violate their contract."""


class SQLiteBenefitAuditStore:
    """Append-only, digest-addressed SQLite audit store.

    Operational filesystem, locking, capacity, and SQLite-open failures are
    surfaced as :class:`BenefitAuditStoreUnavailable`, which is the planner's
    one fail-soft seam. Invalid caller values, digest collisions, and corrupted
    persisted bytes remain distinct fail-closed errors.
    """

    def __init__(self, path: Path, *, busy_timeout_ms: int = _BUSY_TIMEOUT_MS) -> None:
        self.path = Path(path)
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be an integer >= 1")
        self._busy_timeout_ms = busy_timeout_ms
        self._prepare_parent()
        try:
            with secure_file_lock(
                self.path,
                timeout=self._busy_timeout_ms / 1000,
            ):
                created_metadata = self._prepare_path()
                try:
                    with self._connect(initialize=created_metadata is not None):
                        pass
                except Exception:
                    if created_metadata is not None:
                        _cleanup_failed_initialization(
                            self.path,
                            created_metadata,
                            cleanup_sidecars=True,
                        )
                    raise
        except (BenefitAuditStoreError, BenefitAuditStoreUnavailable):
            raise
        except (OSError, TimeoutError) as exc:
            raise BenefitAuditStoreUnavailable(
                f"benefit audit store initialization is unavailable: {self.path}"
            ) from exc

    def store(self, result: BenefitSelectionResult) -> str:
        """Durably bind a validated result digest to exact canonical bytes."""

        if not isinstance(result, BenefitSelectionResult):
            raise TypeError("result must be a BenefitSelectionResult")
        payload = result.to_json().encode("utf-8")
        content_digest = hashlib.sha256(payload).hexdigest()
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    _SELECT_RESULT,
                    (MAX_BENEFIT_RESULT_JSON_BYTES, result.result_digest),
                ).fetchone()
                if row is not None:
                    stored = _bounded_stored_payload(row)
                    if stored != payload:
                        raise BenefitAuditDigestCollision(
                            "benefit result digest is already bound to different bytes"
                        )
                    _validate_stored_metadata(
                        stored,
                        byte_length=row["byte_length"],
                        content_digest=row["content_digest"],
                    )
                    decoded = _decode_stored_result(stored, result.result_digest)
                    if decoded != result:
                        raise BenefitAuditCorruption(
                            "stored benefit result does not equal the submitted result"
                        )
                    connection.execute("COMMIT")
                    return result.result_digest
                connection.execute(
                    """
                    INSERT INTO benefit_audit_results (
                        result_digest, result_json, byte_length, content_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (result.result_digest, payload, len(payload), content_digest),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return result.result_digest

    def load(self, result_digest: str) -> BenefitSelectionResult:
        """Load and fully revalidate one exact digest-bound audit result."""

        _require_sha256(result_digest, "result_digest")
        with self._connect() as connection:
            row = connection.execute(
                _SELECT_RESULT,
                (MAX_BENEFIT_RESULT_JSON_BYTES, result_digest),
            ).fetchone()
        if row is None:
            raise KeyError(result_digest)
        stored = _bounded_stored_payload(row)
        _validate_stored_metadata(
            stored,
            byte_length=row["byte_length"],
            content_digest=row["content_digest"],
        )
        return _decode_stored_result(stored, result_digest)

    def _prepare_parent(self) -> None:
        try:
            reject_symlink_path(self.path)
            ensure_secure_directory(self.path.parent)
            reject_symlink_path(self.path)
            _require_private_directory(self.path.parent)
        except OSError as exc:
            raise BenefitAuditStoreUnavailable(
                f"benefit audit store path could not be prepared: {self.path}"
            ) from exc

    def _prepare_path(self) -> os.stat_result | None:
        created_metadata: os.stat_result | None = None
        try:
            reject_symlink_path(self.path)
            ensure_secure_directory(self.path.parent)
            reject_symlink_path(self.path)
            _require_private_directory(self.path.parent)
            if not self.path.exists():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(self.path, flags, _PRIVATE_FILE_MODE)
                except FileExistsError:
                    # Another same-user initializer may have won the O_EXCL race.
                    if not self.path.exists():
                        raise
                else:
                    try:
                        created_metadata = os.fstat(descriptor)
                        if hasattr(os, "fchmod"):
                            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
                        created_metadata = os.fstat(descriptor)
                    finally:
                        os.close(descriptor)
                    _require_no_sqlite_sidecars(self.path)
            _require_private_file(self.path)
            if os.name == "nt":  # POSIX mode bits do not model Windows ACLs.
                os.chmod(self.path, _PRIVATE_FILE_MODE)
        except Exception as exc:
            if created_metadata is not None:
                _cleanup_failed_initialization(
                    self.path,
                    created_metadata,
                    cleanup_sidecars=False,
                )
            if not isinstance(exc, OSError):
                raise
            raise BenefitAuditStoreUnavailable(
                f"benefit audit store path could not be prepared: {self.path}"
            ) from exc
        return created_metadata

    @contextmanager
    def _connect(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            reject_symlink_path(self.path)
            _require_private_directory(self.path.parent)
            _require_private_file(self.path)
            _secure_sqlite_files(self.path)
        except OSError as exc:
            raise BenefitAuditStoreUnavailable(
                f"benefit audit store path is unavailable: {self.path}"
            ) from exc
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA trusted_schema = OFF")
            if initialize:
                connection.executescript(_SCHEMA)
            _require_schema(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            _secure_sqlite_files(self.path)
            yield connection
        except BenefitAuditStoreError:
            raise
        except BenefitAuditStoreUnavailable:
            raise
        except sqlite3.ProgrammingError as exc:
            raise BenefitAuditStoreError("benefit audit store programming failure") from exc
        except sqlite3.OperationalError as exc:
            if _is_operational_unavailability(exc):
                raise BenefitAuditStoreUnavailable(
                    f"benefit audit store is unavailable: {self.path}"
                ) from exc
            raise BenefitAuditCorruption("benefit audit database operation is invalid") from exc
        except sqlite3.DatabaseError as exc:
            raise BenefitAuditCorruption(
                f"benefit audit database is unreadable: {self.path}"
            ) from exc
        except OSError as exc:
            raise BenefitAuditStoreUnavailable(
                f"benefit audit store filesystem is unavailable: {self.path}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()


def _decode_stored_result(payload: bytes, result_digest: str) -> BenefitSelectionResult:
    try:
        result = BenefitSelectionResult.from_json(payload)
    except (BenefitValidationError, TypeError, ValueError) as exc:
        raise BenefitAuditCorruption("persisted benefit result is invalid") from exc
    if result.result_digest != result_digest:
        raise BenefitAuditCorruption("persisted benefit result is bound to the wrong digest")
    return result


def _blob_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    raise BenefitAuditCorruption("persisted benefit result is not a byte string")


def _bounded_stored_payload(row: sqlite3.Row) -> bytes:
    actual_byte_length = row["actual_byte_length"]
    if (
        type(actual_byte_length) is not int
        or not 0 <= actual_byte_length <= MAX_BENEFIT_RESULT_JSON_BYTES
    ):
        raise BenefitAuditCorruption("persisted benefit result exceeds its bounded size")
    return _blob_bytes(row["result_json"])


def _validate_stored_metadata(
    payload: bytes,
    *,
    byte_length: object,
    content_digest: object,
) -> None:
    if type(byte_length) is not int or byte_length != len(payload):
        raise BenefitAuditCorruption("persisted benefit result byte length is invalid")
    if not isinstance(content_digest, str) or hashlib.sha256(payload).hexdigest() != content_digest:
        raise BenefitAuditCorruption("persisted benefit result content digest is invalid")


def _require_schema(connection: sqlite3.Connection) -> None:
    objects = connection.execute(
        """
        SELECT type, name, tbl_name, sql
          FROM sqlite_master
         WHERE name NOT LIKE 'sqlite_%'
         ORDER BY type, name
        """
    ).fetchall()
    if len(objects) != 1 or (
        str(objects[0]["type"]),
        str(objects[0]["name"]),
        str(objects[0]["tbl_name"]),
        str(objects[0]["sql"]),
    ) != (
        "table",
        "benefit_audit_results",
        "benefit_audit_results",
        _TABLE_SQL,
    ):
        raise BenefitAuditCorruption("benefit audit database objects are invalid")
    rows = connection.execute("PRAGMA table_info(benefit_audit_results)").fetchall()
    signature = {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in rows
    }
    if signature != _EXPECTED_COLUMNS:
        raise BenefitAuditCorruption("benefit audit database schema is invalid")


def _require_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"benefit audit database parent must be a real directory: {path}")
    if os.name == "nt":
        return
    if metadata.st_uid != os.geteuid():
        raise ValueError(f"benefit audit database parent must be owned by the current user: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o700 != 0o700:
        raise ValueError(f"benefit audit database parent must be owner-private (0700): {path}")


def _require_private_file(path: Path) -> None:
    metadata = _require_owned_regular_file(path)
    if os.name == "nt":
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o600 != 0o600:
        raise ValueError(f"benefit audit database file must be owner-private (0600): {path}")


def _require_owned_regular_file(path: Path) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"benefit audit database file must be a regular file: {path}")
    if os.name != "nt" and metadata.st_uid != os.geteuid():
        raise ValueError(f"benefit audit database file must be owned by the current user: {path}")
    return metadata


def _secure_sqlite_files(path: Path) -> None:
    _require_private_file(path)
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        if not os.path.lexists(candidate):
            continue
        reject_symlink_path(candidate)
        _require_owned_regular_file(candidate)
        os.chmod(candidate, _PRIVATE_FILE_MODE)
        _require_private_file(candidate)


def _require_no_sqlite_sidecars(path: Path) -> None:
    if any(os.path.lexists(Path(f"{path}{suffix}")) for suffix in _SQLITE_SIDECAR_SUFFIXES):
        raise BenefitAuditCorruption(
            "a newly created benefit audit database has pre-existing SQLite sidecars"
        )


def _cleanup_failed_initialization(
    path: Path,
    expected: os.stat_result,
    *,
    cleanup_sidecars: bool,
) -> None:
    """Remove only the exact DB inode and sidecars created by a failed first initialization."""

    with secure_directory(path.parent, create=False) as directory:
        directory_fd = directory._directory_fd
        if directory_fd is None:  # pragma: no cover - Windows only
            try:
                current = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            if not os.path.samestat(expected, current):
                raise BenefitAuditCorruption(
                    "new benefit audit database changed during failed initialization"
                )
            if cleanup_sidecars:
                for suffix in _SQLITE_SIDECAR_SUFFIXES:
                    candidate = Path(f"{path}{suffix}")
                    if os.path.lexists(candidate):
                        _require_owned_regular_file(candidate)
                        candidate.unlink()
            path.unlink()
            return

        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not os.path.samestat(expected, current):
            raise BenefitAuditCorruption(
                "new benefit audit database changed during failed initialization"
            )
        if cleanup_sidecars:
            for suffix in _SQLITE_SIDECAR_SUFFIXES:
                name = f"{path.name}{suffix}"
                try:
                    sidecar = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(sidecar.st_mode)
                    or sidecar.st_nlink != 1
                    or (os.name != "nt" and sidecar.st_uid != os.geteuid())
                ):
                    raise BenefitAuditCorruption(
                        "failed benefit audit initialization left an unsafe SQLite sidecar"
                    )
                os.unlink(name, dir_fd=directory_fd)
        os.unlink(path.name, dir_fd=directory_fd)


def _require_sha256(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _is_operational_unavailability(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "busy",
            "disk i/o",
            "database or disk is full",
            "locked",
            "out of memory",
            "readonly",
            "unable to open",
        )
    )


__all__ = [
    "BenefitAuditCorruption",
    "BenefitAuditDigestCollision",
    "BenefitAuditStoreError",
    "SQLiteBenefitAuditStore",
]
