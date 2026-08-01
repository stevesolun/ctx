from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

import pytest

from scripts import ctx_ab_failure_evidence as evidence


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def test_publishes_chained_private_failure_and_authenticated_staging(
    tmp_path: Path,
) -> None:
    staging = tmp_path / ".staging"
    staging.mkdir(mode=0o700)
    raw = staging / "raw"
    raw.mkdir(mode=0o755)
    (raw / "detail.txt").write_text("private task detail", encoding="utf-8")
    (raw / "detail.txt").chmod(0o644)
    destination = tmp_path / "failure"

    try:
        try:
            raise ValueError("private cause")
        except ValueError as cause:
            raise RuntimeError("private outer") from cause
    except RuntimeError as exc:
        manifest = evidence.publish_failure(
            destination=destination,
            operation="test-operation",
            exc=exc,
            repository_root=tmp_path / "unrelated-repository",
            staging=staging,
        )

    assert not staging.exists()
    assert evidence.already_preserved(destination)
    failure = json.loads((destination / "failure.json").read_text(encoding="utf-8"))
    assert failure["exception_chain"] == [
        {"message": "private outer", "type": "RuntimeError"},
        {"message": "private cause", "type": "ValueError"},
    ]
    assert failure["operation"] == "test-operation"
    assert "private cause" in failure["traceback"]
    assert (
        manifest["manifest_sha256"] == hashlib.sha256(_canonical(manifest["entries"])).hexdigest()
    )
    assert json.loads((destination / evidence.FAILURE_MANIFEST).read_bytes()) == manifest
    assert (destination / "raw" / "detail.txt").read_text(encoding="utf-8") == (
        "private task detail"
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "raw").stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "raw" / "detail.txt").stat().st_mode) == 0o600


def test_refuses_overwrite_and_nonprivate_repository_destination(tmp_path: Path) -> None:
    destination = tmp_path / "failure"
    destination.mkdir()
    with pytest.raises(evidence.FailureEvidenceError, match="already exists"):
        evidence.publish_failure(
            destination=destination,
            operation="test",
            exc=RuntimeError("detail"),
            repository_root=tmp_path / "repository",
        )

    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(evidence.FailureEvidenceError, match="private root"):
        evidence.publish_failure(
            destination=repository / "public-failure",
            operation="test",
            exc=RuntimeError("detail"),
            repository_root=repository,
        )


def test_requires_absolute_destination(tmp_path: Path) -> None:
    with pytest.raises(evidence.FailureEvidenceError, match="absolute"):
        evidence.publish_failure(
            destination=Path("relative-failure"),
            operation="test",
            exc=RuntimeError("detail"),
            repository_root=tmp_path,
        )
