"""Crash-safe promotion helpers for generated wiki/graph artifacts."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Any

from ctx.utils._fs_utils import atomic_write_json, reject_symlink_path

ArtifactValidator = Callable[[Path], None]


@dataclass(frozen=True)
class ArtifactPromotionResult:
    target: Path
    metadata_path: Path
    previous: dict[str, Any]
    candidate: dict[str, Any]
    current: dict[str, Any]


def promote_staged_artifact(
    staged_path: Path,
    target_path: Path,
    *,
    validate: ArtifactValidator | None = None,
    metadata_path: Path | None = None,
    now: datetime | None = None,
) -> ArtifactPromotionResult:
    """Validate *staged_path*, atomically replace *target_path*, and record metadata.

    The caller owns candidate generation. This helper owns the invariant that a
    validation or replace failure leaves the existing target untouched. Metadata
    is written before replacement with status ``staged`` and then updated to
    ``promoted`` after the atomic swap succeeds, so a crash between those steps
    still leaves enough last-good/candidate hashes to audit recovery.
    """
    staged = Path(staged_path)
    target = Path(target_path)
    if not staged.is_file():
        raise FileNotFoundError(f"staged artifact does not exist: {staged}")
    reject_symlink_path(staged)
    reject_symlink_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_path(target)

    if validate is not None:
        validate(staged)

    metadata = Path(metadata_path) if metadata_path is not None else _default_metadata_path(target)
    reject_symlink_path(metadata)
    metadata.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_path(metadata)

    started_at = _timestamp(now)
    previous = _snapshot(target)
    candidate = _snapshot(staged)
    pending_record = {
        "schema_version": 1,
        "status": "staged",
        "target": str(target),
        "started_at": started_at,
        "previous": previous,
        "candidate": candidate,
    }
    atomic_write_json(metadata, pending_record, indent=2)

    _replace_with_retry(staged, target)

    current = _snapshot(target)
    promoted_record = {
        **pending_record,
        "status": "promoted",
        "promoted_at": _timestamp(),
        "current": current,
    }
    atomic_write_json(metadata, promoted_record, indent=2)
    return ArtifactPromotionResult(
        target=target,
        metadata_path=metadata,
        previous=previous,
        candidate=candidate,
        current=current,
    )


def _default_metadata_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.promotion.json")


def _snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "size": None,
            "sha256": None,
            "mtime_ns": None,
        }
    if not path.is_file():
        raise ValueError(f"artifact path is not a file: {path}")
    stat = path.stat()
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "sha256": _sha256_file(path),
        "mtime_ns": stat.st_mtime_ns,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = 10, delay: float = 0.05) -> None:
    last_exc: Exception | None = None
    for _ in range(max(attempts, 1)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("artifact replace was not attempted")
