from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest

from scripts import ctx_ab_exposure_ledger as exposure


def _private_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def test_builder_merges_private_sources_without_revealing_identities(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private = tmp_path / "private"
    selection = _private_file(
        private / "selection.json",
        _canonical(
            {
                "protocol_id": "synthetic",
                "analysis_instance_ids": ["synthetic-selection-task"],
                "analysis_repository_map": {
                    "synthetic-selection-task": "https://github.com/owner/repo.git"
                },
                "canary_instance_id": None,
                "canary_repository": None,
            }
        ),
    )
    evidence = _private_file(
        private / "summary.json",
        json.dumps(
            [
                {"scenario": "synthetic-evidence-task", "arm": "baseline"},
                {"scenario": "synthetic-evidence-task", "arm": "ctx-full"},
            ]
        ).encode(),
    )
    explicit = _private_file(
        private / "explicit.txt",
        b"synthetic-explicit-task\n",
    )
    salt_file = _private_file(private / "salt.txt", ("b" * 64 + "\n").encode())
    output = private / "exposure-ledger.json"

    assert (
        exposure.main(
            [
                "--selection",
                str(selection),
                "--evidence",
                str(evidence),
                "--instance-id-file",
                str(explicit),
                "--salt-file",
                str(salt_file),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = output.read_bytes()
    document = json.loads(payload)
    assert payload == exposure.canonical_ledger_bytes(document)
    assert set(document) == {
        "schema_version",
        "salt",
        "instance_id_hmac_sha256",
    }
    assert document["instance_id_hmac_sha256"] == sorted(
        hmac.new(
            bytes.fromhex("b" * 64),
            instance_id.encode(),
            hashlib.sha256,
        ).hexdigest()
        for instance_id in (
            "synthetic-selection-task",
            "synthetic-evidence-task",
            "synthetic-explicit-task",
        )
    )
    assert (
        exposure.load_authenticated_ledger(
            output,
            hashlib.sha256(payload).hexdigest(),
        )
        == document
    )
    stdout = capsys.readouterr().out
    assert stdout.strip() == hashlib.sha256(payload).hexdigest()
    assert "synthetic-" not in stdout
    if os.name != "nt":
        assert output.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("failure", ["duplicate-hash", "unsorted", "noncanonical"])
def test_authenticated_loader_rejects_malformed_ledger(tmp_path: Path, failure: str) -> None:
    digest = "1" * 64
    hashes = [digest]
    if failure == "duplicate-hash":
        hashes.append(digest)
    elif failure == "unsorted":
        hashes = ["f" * 64, "0" * 64]
    document = {
        "schema_version": 1,
        "salt": "c" * 64,
        "instance_id_hmac_sha256": hashes,
    }
    payload = _canonical(document)
    if failure == "noncanonical":
        payload += b"\n"
    path = _private_file(tmp_path / "private" / "ledger.json", payload)

    with pytest.raises(ValueError, match="exposure ledger"):
        exposure.load_authenticated_ledger(path, hashlib.sha256(payload).hexdigest())


def test_builder_rejects_duplicate_and_unsafe_inputs(tmp_path: Path) -> None:
    private = tmp_path / "private"
    explicit = _private_file(private / "explicit.txt", b"synthetic-task\nsynthetic-task\n")
    output = private / "ledger.json"

    with pytest.raises(ValueError, match="duplicate"):
        exposure.build_exposure_ledger(
            output=output,
            instance_id_paths=[explicit],
            salt="d" * 64,
        )

    target = _private_file(private / "target.txt", b"synthetic-task\n")
    link = private / "link.txt"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="private input"):
        exposure.build_exposure_ledger(
            output=output,
            instance_id_paths=[link],
            salt="d" * 64,
        )


def test_builder_rejects_zero_historical_inputs(tmp_path: Path) -> None:
    output = tmp_path / "private" / "ledger.json"

    with pytest.raises(ValueError, match="at least one historical source input"):
        exposure.build_exposure_ledger(
            output=output,
            salt="d" * 64,
        )

    assert not output.exists()


def test_builder_rejects_empty_existing_ledger(tmp_path: Path) -> None:
    private = tmp_path / "private"
    empty = {
        "schema_version": 1,
        "salt": "d" * 64,
        "instance_id_hmac_sha256": [],
    }
    existing = _private_file(private / "existing.json", _canonical(empty))
    output = private / "merged.json"

    with pytest.raises(ValueError, match="at least one historical task hash"):
        exposure.build_exposure_ledger(
            output=output,
            ledger_paths=[existing],
        )

    assert not output.exists()
