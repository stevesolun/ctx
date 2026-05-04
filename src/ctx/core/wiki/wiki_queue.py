"""Persistent queue for wiki and graph maintenance jobs.

This module is intentionally small: it owns durable job state, leases, retry
state, and crash recovery. It does not execute jobs. Callers enqueue work and a
future processor leases jobs with explicit worker IDs.
"""

from __future__ import annotations

import json
import sqlite3
import time
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ctx.utils._fs_utils import reject_symlink_path

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

ENTITY_UPSERT_JOB = "entity-upsert"
GRAPH_EXPORT_JOB = "graph-export"
CATALOG_REFRESH_JOB = "catalog-refresh"
TAR_REFRESH_JOB = "tar-refresh"
ARTIFACT_PROMOTION_JOB = "artifact-promotion"
MAINTENANCE_JOB_KINDS = (
    GRAPH_EXPORT_JOB,
    CATALOG_REFRESH_JOB,
    TAR_REFRESH_JOB,
    ARTIFACT_PROMOTION_JOB,
)
WORKER_JOB_KINDS = (ENTITY_UPSERT_JOB, *MAINTENANCE_JOB_KINDS)
QUEUE_DIRNAME = ".ctx"
QUEUE_DB_NAME = "wiki-queue.sqlite3"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_queue_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    idempotency_key TEXT UNIQUE,
    content_hash TEXT,
    worker_id TEXT,
    leased_until REAL,
    available_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wiki_queue_ready
    ON wiki_queue_jobs(status, available_at, id);

CREATE INDEX IF NOT EXISTS idx_wiki_queue_lease
    ON wiki_queue_jobs(status, leased_until);
"""


@dataclass(frozen=True)
class QueueJob:
    id: int
    kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    idempotency_key: str | None
    content_hash: str | None
    worker_id: str | None
    leased_until: float | None
    available_at: float
    last_error: str | None
    created_at: float
    updated_at: float


def init_queue(db_path: Path) -> None:
    """Create the queue database and enable SQLite WAL mode."""
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)


def queue_db_path(wiki_path: Path) -> Path:
    """Return the durable maintenance queue path for a wiki root."""
    return Path(wiki_path) / QUEUE_DIRNAME / QUEUE_DB_NAME


def enqueue_entity_upsert(
    wiki_path: Path,
    *,
    entity_type: str,
    slug: str,
    entity_path: Path,
    content: str,
    action: str,
    source: str,
    now: float | None = None,
) -> QueueJob:
    """Queue a wiki entity upsert for graph/wiki artifact refresh.

    The idempotency key includes the content hash. Re-adding unchanged
    content collapses to one job, while a real update creates a new queue item
    for the future worker to process.
    """
    entity_type = _validate_value("entity_type", entity_type)
    slug = _validate_value("slug", slug)
    action = _validate_value("action", action)
    source = _validate_value("source", source)
    content_hash = sha256(content.encode("utf-8")).hexdigest()
    rel_entity_path = _relative_to_wiki(wiki_path, entity_path)
    payload = {
        "entity_type": entity_type,
        "slug": slug,
        "entity_path": rel_entity_path,
        "action": action,
        "source": source,
        "content_hash": content_hash,
    }
    return enqueue(
        queue_db_path(wiki_path),
        kind=ENTITY_UPSERT_JOB,
        payload=payload,
        idempotency_key=f"{ENTITY_UPSERT_JOB}:{entity_type}:{slug}:{content_hash}",
        content_hash=content_hash,
        now=now,
    )


def enqueue_maintenance_job(
    wiki_path: Path,
    *,
    kind: str,
    payload: dict[str, Any],
    source: str,
    max_attempts: int = 3,
    available_at: float | None = None,
    now: float | None = None,
) -> QueueJob:
    """Queue graph/wiki maintenance work for the durable worker."""
    _validate_maintenance_kind(kind)
    source = _validate_value("source", source)
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict, got {type(payload).__name__}")
    job_payload = dict(payload)
    job_payload["source"] = source
    payload_json = _dump_payload(job_payload)
    content_hash = sha256(payload_json.encode("utf-8")).hexdigest()
    return enqueue(
        queue_db_path(wiki_path),
        kind=kind,
        payload=job_payload,
        idempotency_key=f"{kind}:{source}:{content_hash}",
        content_hash=content_hash,
        max_attempts=max_attempts,
        available_at=available_at,
        now=now,
    )


def enqueue(
    db_path: Path,
    *,
    kind: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    content_hash: str | None = None,
    max_attempts: int = 3,
    available_at: float | None = None,
    now: float | None = None,
) -> QueueJob:
    """Insert a pending job or return the existing job for an idempotency key."""
    _validate_kind(kind)
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1 (got {max_attempts})")
    timestamp = _now(now)
    ready_at = timestamp if available_at is None else float(available_at)
    payload_json = _dump_payload(payload)

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM wiki_queue_jobs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return _row_to_job(row)
            cur = conn.execute(
                """
                INSERT INTO wiki_queue_jobs (
                    kind, payload_json, status, attempts, max_attempts,
                    idempotency_key, content_hash, worker_id, leased_until,
                    available_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, 0, ?, ?, ?, NULL, NULL, ?, NULL, ?, ?)
                """,
                (
                    kind,
                    payload_json,
                    STATUS_PENDING,
                    int(max_attempts),
                    idempotency_key,
                    content_hash,
                    ready_at,
                    timestamp,
                    timestamp,
                ),
            )
            if cur.lastrowid is None:
                raise RuntimeError("queue insert did not return a job id")
            job_id = int(cur.lastrowid)
            row = _select_job(conn, job_id)
            conn.execute("COMMIT")
            return _row_to_job(row)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def lease_next(
    db_path: Path,
    *,
    worker_id: str,
    lease_seconds: float = 60.0,
    kinds: Iterable[str] | None = None,
    now: float | None = None,
) -> QueueJob | None:
    """Lease the oldest available pending job, recovering expired leases first."""
    if not worker_id.strip():
        raise ValueError("worker_id must be non-empty")
    if lease_seconds <= 0:
        raise ValueError(f"lease_seconds must be > 0 (got {lease_seconds})")
    timestamp = _now(now)
    kind_filter = tuple(kinds or ())
    for kind in kind_filter:
        _validate_kind(kind)

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _recover_expired_leases(conn, timestamp)
            row = _select_next_ready(conn, timestamp, kind_filter)
            if row is None:
                conn.execute("COMMIT")
                return None
            job_id = int(row["id"])
            conn.execute(
                """
                UPDATE wiki_queue_jobs
                   SET status = ?,
                       attempts = attempts + 1,
                       worker_id = ?,
                       leased_until = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    STATUS_RUNNING,
                    worker_id,
                    timestamp + float(lease_seconds),
                    timestamp,
                    job_id,
                ),
            )
            leased = _select_job(conn, job_id)
            conn.execute("COMMIT")
            return _row_to_job(leased)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_succeeded(db_path: Path, job_id: int, *, now: float | None = None) -> QueueJob:
    """Mark a leased job as succeeded and clear lease metadata."""
    timestamp = _now(now)
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _select_job(conn, job_id)
            conn.execute(
                """
                UPDATE wiki_queue_jobs
                   SET status = ?,
                       worker_id = NULL,
                       leased_until = NULL,
                       last_error = NULL,
                       updated_at = ?
                 WHERE id = ?
                """,
                (STATUS_SUCCEEDED, timestamp, int(job_id)),
            )
            row = _select_job(conn, job_id)
            conn.execute("COMMIT")
            return _row_to_job(row)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_failed(
    db_path: Path,
    job_id: int,
    *,
    error: str,
    retry: bool,
    delay_seconds: float = 0.0,
    now: float | None = None,
) -> QueueJob:
    """Record a job failure, retrying if allowed and attempts remain."""
    timestamp = _now(now)
    delay = max(0.0, float(delay_seconds))
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = _row_to_job(_select_job(conn, job_id))
            should_retry = retry and current.attempts < current.max_attempts
            status = STATUS_PENDING if should_retry else STATUS_FAILED
            available_at = timestamp + delay if should_retry else current.available_at
            conn.execute(
                """
                UPDATE wiki_queue_jobs
                   SET status = ?,
                       worker_id = NULL,
                       leased_until = NULL,
                       available_at = ?,
                       last_error = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (status, available_at, error, timestamp, int(job_id)),
            )
            row = _select_job(conn, job_id)
            conn.execute("COMMIT")
            return _row_to_job(row)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def get_job(db_path: Path, job_id: int) -> QueueJob:
    """Return one queue job by ID."""
    with _connect(db_path) as conn:
        return _row_to_job(_select_job(conn, job_id))


def list_jobs(
    db_path: Path,
    *,
    statuses: Iterable[str] | None = None,
) -> list[QueueJob]:
    """List jobs in stable insertion order."""
    status_filter = tuple(statuses or ())
    with _connect(db_path) as conn:
        if status_filter:
            placeholders = ",".join("?" for _ in status_filter)
            rows = conn.execute(
                f"SELECT * FROM wiki_queue_jobs WHERE status IN ({placeholders}) ORDER BY id",
                status_filter,
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM wiki_queue_jobs ORDER BY id",
            ).fetchall()
    return [_row_to_job(row) for row in rows]


def _connect(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    reject_symlink_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_path(path)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


def _recover_expired_leases(conn: sqlite3.Connection, now: float) -> None:
    conn.execute(
        """
        UPDATE wiki_queue_jobs
           SET status = ?,
               worker_id = NULL,
               leased_until = NULL,
               updated_at = ?
         WHERE status = ?
           AND leased_until IS NOT NULL
           AND leased_until <= ?
           AND attempts < max_attempts
        """,
        (STATUS_PENDING, now, STATUS_RUNNING, now),
    )
    conn.execute(
        """
        UPDATE wiki_queue_jobs
           SET status = ?,
               worker_id = NULL,
               leased_until = NULL,
               updated_at = ?
         WHERE status = ?
           AND leased_until IS NOT NULL
           AND leased_until <= ?
           AND attempts >= max_attempts
        """,
        (STATUS_FAILED, now, STATUS_RUNNING, now),
    )


def _select_next_ready(
    conn: sqlite3.Connection,
    now: float,
    kinds: tuple[str, ...],
) -> sqlite3.Row | None:
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        return conn.execute(
            f"""
            SELECT * FROM wiki_queue_jobs
             WHERE status = ?
               AND available_at <= ?
               AND attempts < max_attempts
               AND kind IN ({placeholders})
             ORDER BY available_at, id
             LIMIT 1
            """,
            (STATUS_PENDING, now, *kinds),
        ).fetchone()
    return conn.execute(
        """
        SELECT * FROM wiki_queue_jobs
         WHERE status = ?
           AND available_at <= ?
           AND attempts < max_attempts
         ORDER BY available_at, id
         LIMIT 1
        """,
        (STATUS_PENDING, now),
    ).fetchone()


def _select_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM wiki_queue_jobs WHERE id = ?",
        (int(job_id),),
    ).fetchone()
    if row is None:
        raise LookupError(f"queue job not found: {job_id}")
    return row


def _row_to_job(row: sqlite3.Row) -> QueueJob:
    return QueueJob(
        id=int(row["id"]),
        kind=str(row["kind"]),
        payload=json.loads(str(row["payload_json"])),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        idempotency_key=row["idempotency_key"],
        content_hash=row["content_hash"],
        worker_id=row["worker_id"],
        leased_until=row["leased_until"],
        available_at=float(row["available_at"]),
        last_error=row["last_error"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _dump_payload(payload: dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be a dict, got {type(payload).__name__}")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _relative_to_wiki(wiki_path: Path, entity_path: Path) -> str:
    try:
        return Path(entity_path).relative_to(Path(wiki_path)).as_posix()
    except ValueError:
        return str(entity_path)


def _validate_value(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _validate_kind(kind: str) -> None:
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("kind must be a non-empty string")


def _validate_maintenance_kind(kind: str) -> None:
    _validate_kind(kind)
    if kind not in MAINTENANCE_JOB_KINDS:
        raise ValueError(f"unsupported maintenance job kind: {kind}")


def _now(now: float | None) -> float:
    return time.time() if now is None else float(now)
