"""Worker for durable wiki maintenance queue jobs."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from ctx.core.wiki.artifact_promotion import promote_staged_artifact
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
MaintenanceHandler = Callable[[Path, dict[str, Any]], str]


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
        kinds=wiki_queue.WORKER_JOB_KINDS,
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
    if job.kind == wiki_queue.ENTITY_UPSERT_JOB:
        return _process_entity_upsert(wiki_path, job.payload)
    handler = MAINTENANCE_HANDLERS.get(job.kind)
    if handler is None:
        raise ValueError(f"unsupported wiki queue job kind: {job.kind}")
    return handler(wiki_path, job.payload)


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


def _handle_graph_export(wiki_path: Path, payload: dict[str, Any]) -> str:
    args = [
        sys.executable,
        "-m",
        "ctx.core.wiki.wiki_graphify",
        "--wiki-dir",
        str(wiki_path),
    ]
    args.append("--full" if payload.get("incremental") is False else "--incremental")
    if payload.get("graph_only", True):
        args.append("--graph-only")
    if payload.get("dry_run"):
        args.append("--dry-run")
    _run_checked(args, label="graph export")
    return "graph export completed"


def _handle_catalog_refresh(_wiki_path: Path, payload: dict[str, Any]) -> str:
    args = _catalog_refresh_args(payload, update_wiki_tar=False)
    _run_checked(args, label="catalog refresh")
    return "catalog refresh completed"


def _handle_tar_refresh(_wiki_path: Path, payload: dict[str, Any]) -> str:
    args = _catalog_refresh_args(payload, update_wiki_tar=True)
    _run_checked(args, label="tar refresh")
    return "tar refresh completed"


def _handle_artifact_promotion(_wiki_path: Path, payload: dict[str, Any]) -> str:
    staged = Path(_required_payload_string(payload, "staged_path"))
    target = Path(_required_payload_string(payload, "target_path"))
    validator = payload.get("validator")
    validate = None
    if validator == "wiki-tar":
        from import_skills_sh_catalog import _validate_wiki_tarball_candidate  # noqa: PLC0415
        validate = _validate_wiki_tarball_candidate
    elif validator not in (None, "", "none"):
        raise ValueError(f"unsupported artifact validator: {validator}")
    result = promote_staged_artifact(staged, target, validate=validate)
    return f"promoted artifact to {result.target}"


def _catalog_refresh_args(payload: dict[str, Any], *, update_wiki_tar: bool) -> list[str]:
    args = [sys.executable, "-m", "import_skills_sh_catalog"]
    if payload.get("fetch"):
        args.append("--fetch")
    else:
        from_catalog = payload.get("from_catalog") or payload.get("catalog")
        from_api_union = payload.get("from_api_union")
        source_flag = "--from-catalog" if from_catalog else "--from-api-union"
        source_value = from_catalog or from_api_union
        if not isinstance(source_value, str) or not source_value.strip():
            raise ValueError(
                "catalog maintenance payload requires fetch=true, from_catalog, "
                "from_api_union, or catalog"
            )
        args.extend([source_flag, source_value.strip()])
    if catalog_out := _optional_payload_string(payload, "catalog_out"):
        args.extend(["--catalog-out", catalog_out])
    if wiki_tar := _optional_payload_string(payload, "wiki_tar"):
        args.extend(["--wiki-tar", wiki_tar])
    if payload.get("drop_body_unavailable"):
        args.append("--drop-body-unavailable")
    if update_wiki_tar:
        args.append("--update-wiki-tar")
    return args


def _run_checked(args: list[str], *, label: str) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"{label} failed with exit {exc.returncode}{suffix}") from exc


def _required_payload_string(payload: dict[str, Any], key: str) -> str:
    value = _optional_payload_string(payload, key)
    if value is None:
        raise ValueError(f"maintenance payload requires non-empty {key}")
    return value


def _optional_payload_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"maintenance payload {key} must be a non-empty string")
    return value.strip()


MAINTENANCE_HANDLERS: dict[str, MaintenanceHandler] = {
    wiki_queue.GRAPH_EXPORT_JOB: _handle_graph_export,
    wiki_queue.CATALOG_REFRESH_JOB: _handle_catalog_refresh,
    wiki_queue.TAR_REFRESH_JOB: _handle_tar_refresh,
    wiki_queue.ARTIFACT_PROMOTION_JOB: _handle_artifact_promotion,
}


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
