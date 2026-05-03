"""Worker for durable wiki maintenance queue jobs."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ctx.core.wiki import wiki_queue
from ctx.core.wiki.wiki_sync import update_index
from ctx.utils._fs_utils import reject_symlink_path
from ctx_config import cfg

_ENTITY_SUBJECT_TYPES = {
    "skill": "skills",
    "agent": "agents",
    "mcp-server": "mcp-servers",
    "harness": "harnesses",
}


@dataclass(frozen=True)
class ProcessResult:
    job_id: int
    kind: str
    status: str
    message: str


def process_next(
    wiki_path: Path,
    *,
    worker_id: str,
    lease_seconds: float = 60.0,
    retry_delay_seconds: float = 5.0,
    now: float | None = None,
) -> ProcessResult | None:
    """Lease and process one ready wiki maintenance job."""
    db_path = wiki_queue.queue_db_path(wiki_path)
    job = wiki_queue.lease_next(
        db_path,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        kinds=(wiki_queue.ENTITY_UPSERT_JOB,),
        now=now,
    )
    if job is None:
        return None

    try:
        message = _process_job(wiki_path, job)
    except Exception as exc:  # noqa: BLE001 - failures are persisted into queue state.
        failed = wiki_queue.mark_failed(
            db_path,
            job.id,
            error=str(exc),
            retry=True,
            delay_seconds=retry_delay_seconds,
            now=now,
        )
        return ProcessResult(
            job_id=failed.id,
            kind=failed.kind,
            status=failed.status,
            message=str(failed.last_error or exc),
        )

    succeeded = wiki_queue.mark_succeeded(db_path, job.id, now=now)
    return ProcessResult(
        job_id=succeeded.id,
        kind=succeeded.kind,
        status=succeeded.status,
        message=message,
    )


def drain_queue(
    wiki_path: Path,
    *,
    worker_id: str,
    limit: int | None = None,
    lease_seconds: float = 60.0,
    retry_delay_seconds: float = 5.0,
    now: float | None = None,
) -> list[ProcessResult]:
    """Process ready queue jobs until empty or *limit* is reached."""
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0 (got {limit})")
    results: list[ProcessResult] = []
    while limit is None or len(results) < limit:
        result = process_next(
            wiki_path,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            retry_delay_seconds=retry_delay_seconds,
            now=now,
        )
        if result is None:
            break
        results.append(result)
    return results


def _process_job(wiki_path: Path, job: wiki_queue.QueueJob) -> str:
    if job.kind != wiki_queue.ENTITY_UPSERT_JOB:
        raise ValueError(f"unsupported wiki queue job kind: {job.kind}")
    return _process_entity_upsert(wiki_path, job.payload)


def _process_entity_upsert(wiki_path: Path, payload: dict[str, Any]) -> str:
    entity_type = _required_string(payload, "entity_type")
    slug = _required_string(payload, "slug")
    expected_hash = _required_string(payload, "content_hash")
    subject_type = _ENTITY_SUBJECT_TYPES.get(entity_type)
    if subject_type is None:
        raise ValueError(f"unsupported entity_type for entity-upsert: {entity_type}")

    entity_path = _resolve_entity_path(wiki_path, _required_string(payload, "entity_path"))
    text = entity_path.read_text(encoding="utf-8")
    actual_hash = sha256(text.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            "content hash mismatch for "
            f"{entity_type}:{slug}: expected {expected_hash}, got {actual_hash}"
        )

    update_index(str(wiki_path), [slug], subject_type=subject_type)
    return f"refreshed {subject_type} index for {slug}"


def _resolve_entity_path(wiki_path: Path, raw_path: str) -> Path:
    wiki_root = Path(wiki_path).resolve()
    candidate_path = Path(raw_path)
    candidate = candidate_path.resolve() if candidate_path.is_absolute() else (
        wiki_root / candidate_path
    ).resolve()
    if not candidate.is_relative_to(wiki_root):
        raise ValueError(f"entity_path escapes wiki root: {raw_path}")
    reject_symlink_path(candidate)
    return candidate


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"entity-upsert payload requires non-empty {key}")
    return value.strip()


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Drain ctx wiki maintenance queue jobs")
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki root path")
    parser.add_argument("--worker-id", default=_default_worker_id(), help="Queue worker ID")
    parser.add_argument("--limit", type=int, default=None, help="Maximum jobs to process")
    parser.add_argument("--once", action="store_true", help="Process at most one job")
    parser.add_argument("--lease-seconds", type=float, default=60.0, help="Lease duration")
    parser.add_argument("--retry-delay-seconds", type=float, default=5.0, help="Retry delay")
    args = parser.parse_args(argv)

    if args.once and args.limit is not None:
        parser.error("use either --once or --limit, not both")
    limit = 1 if args.once else args.limit

    try:
        results = drain_queue(
            Path(os.path.expanduser(args.wiki)),
            worker_id=args.worker_id,
            limit=limit,
            lease_seconds=args.lease_seconds,
            retry_delay_seconds=args.retry_delay_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should surface queue failures cleanly.
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No ready wiki queue jobs.")
        return

    failed = False
    for result in results:
        print(f"{result.status}: {result.kind}#{result.job_id} - {result.message}")
        if result.status != wiki_queue.STATUS_SUCCEEDED:
            failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
