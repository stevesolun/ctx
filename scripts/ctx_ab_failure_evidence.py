#!/usr/bin/env python3
"""Atomically preserve owner-only private evidence for CTX A/B failures."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import traceback
from typing import Any


FAILURE_GUARD = "ctx-ab-private-failure-v1"
FAILURE_MANIFEST = "artifact-manifest.json"


class FailureEvidenceError(RuntimeError):
    """Private failure evidence could not be published safely."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _exception_chain(exc: BaseException) -> list[dict[str, str]]:
    chain: list[dict[str, str]] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({"message": str(current), "type": type(current).__name__})
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return chain


def _harden_private_tree(root: Path) -> None:
    os.chmod(root, 0o700)
    for path in root.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            continue
        if stat.S_ISDIR(metadata.st_mode):
            os.chmod(path, 0o700)
        elif stat.S_ISREG(metadata.st_mode):
            os.chmod(path, 0o700 if metadata.st_mode & 0o111 else 0o600)
        else:
            raise FailureEvidenceError("private failure evidence contains a special file")


def _artifact_manifest(root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == FAILURE_MANIFEST:
            continue
        metadata = path.lstat()
        record: dict[str, Any] = {
            "mode": stat.S_IMODE(metadata.st_mode),
            "path": relative,
        }
        if stat.S_ISDIR(metadata.st_mode):
            record["type"] = "directory"
        elif stat.S_ISREG(metadata.st_mode):
            data = path.read_bytes()
            record.update(
                {
                    "bytes": len(data),
                    "sha256": _sha256(data),
                    "type": "file",
                }
            )
        elif stat.S_ISLNK(metadata.st_mode):
            record.update(
                {
                    "sha256": _sha256(os.fsencode(os.readlink(path))),
                    "type": "symlink",
                }
            )
        else:
            raise FailureEvidenceError("private failure evidence contains a special file")
        entries.append(record)
    return {
        "entries": entries,
        "entry_count": len(entries),
        "guard": FAILURE_GUARD,
        "manifest_sha256": _sha256(_canonical_bytes(entries)),
        "schema_version": 1,
    }


def validate_destination(destination: Path, *, repository_root: Path) -> None:
    if not destination.is_absolute():
        raise FailureEvidenceError("failure evidence destination must be absolute")
    resolved = destination.resolve(strict=False)
    private_root = (repository_root / ".gate" / "ctx-ab-private").resolve()
    if repository_root.resolve() in resolved.parents and private_root not in resolved.parents:
        raise FailureEvidenceError(
            "failure evidence inside the repository must use the private root"
        )
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FailureEvidenceError("failure evidence destination already exists")


def publish_failure(
    *,
    destination: Path,
    operation: str,
    exc: BaseException,
    repository_root: Path,
    staging: Path | None = None,
) -> dict[str, Any]:
    """Publish a private immutable exception bundle, optionally with staged raw evidence."""

    validate_destination(destination, repository_root=repository_root)
    owns_staging = staging is None
    if staging is None:
        staging = Path(tempfile.mkdtemp(prefix=".ctx-ab-failure-", dir=destination.parent))
        os.chmod(staging, 0o700)
    elif staging.is_symlink() or not staging.is_dir():
        raise FailureEvidenceError("failure evidence staging root is unsafe")

    failure = {
        "exception_chain": _exception_chain(exc),
        "guard": FAILURE_GUARD,
        "operation": operation,
        "schema_version": 1,
        "traceback": "".join(traceback.format_exception(exc)),
    }
    try:
        _harden_private_tree(staging)
        _private_write(staging / "failure.json", _canonical_bytes(failure))
        manifest = _artifact_manifest(staging)
        _private_write(staging / FAILURE_MANIFEST, _canonical_bytes(manifest))
        staging.rename(destination)
    except BaseException:
        # Never delete a partially captured failure. A hidden owner-only staging
        # directory is safer than destroying the only root-cause evidence.
        if owns_staging and staging.exists():
            os.chmod(staging, 0o700)
        raise
    return manifest


def already_preserved(destination: Path) -> bool:
    return (
        destination.is_dir()
        and not destination.is_symlink()
        and (destination / "failure.json").is_file()
        and (destination / FAILURE_MANIFEST).is_file()
    )
