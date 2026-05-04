from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ctx.core.wiki import artifact_promotion


def test_promote_staged_artifact_validates_replaces_and_records_metadata(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.txt"
    staged = tmp_path / "candidate.txt"
    target.write_bytes(b"old\n")
    staged.write_bytes(b"new\n")
    validated: list[Path] = []

    result = artifact_promotion.promote_staged_artifact(
        staged,
        target,
        validate=lambda path: validated.append(path),
        now=datetime(2026, 5, 4, tzinfo=timezone.utc),
    )

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert validated == [staged]
    assert target.read_bytes() == b"new\n"
    assert not staged.exists()
    assert metadata["status"] == "promoted"
    assert metadata["previous"]["exists"] is True
    assert metadata["previous"]["size"] == 4
    assert metadata["candidate"]["size"] == 4
    assert metadata["current"]["sha256"] == metadata["candidate"]["sha256"]


def test_promote_staged_artifact_validation_failure_preserves_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "artifact.txt"
    staged = tmp_path / "candidate.txt"
    target.write_bytes(b"old\n")
    staged.write_bytes(b"new\n")

    def fail_validation(_path: Path) -> None:
        raise ValueError("candidate failed validation")

    with pytest.raises(ValueError, match="candidate failed validation"):
        artifact_promotion.promote_staged_artifact(
            staged,
            target,
            validate=fail_validation,
        )

    assert target.read_bytes() == b"old\n"
    assert staged.exists()
    assert not target.with_name("artifact.txt.promotion.json").exists()


def test_promote_staged_artifact_replace_failure_preserves_target_and_last_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.txt"
    staged = tmp_path / "candidate.txt"
    target.write_bytes(b"old\n")
    staged.write_bytes(b"new\n")

    def locked_replace(_src: Path, _dst: Path) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(artifact_promotion, "_replace_with_retry", locked_replace)

    with pytest.raises(PermissionError, match="locked"):
        artifact_promotion.promote_staged_artifact(staged, target)

    metadata = json.loads(
        target.with_name("artifact.txt.promotion.json").read_text(encoding="utf-8")
    )
    assert target.read_bytes() == b"old\n"
    assert staged.exists()
    assert metadata["status"] == "staged"
    assert metadata["previous"]["sha256"] != metadata["candidate"]["sha256"]
