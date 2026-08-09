from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, cast

import pytest

from ctx.engine.benefit import (
    MAX_BENEFIT_RESULT_JSON_BYTES,
    BenefitCandidate,
    BenefitSelectionResult,
    BenefitValidationError,
    EvidenceSummary,
    NetBenefitPolicy,
    ResourceCosts,
)
from ctx.engine.benefit_audit_store import (
    BenefitAuditCorruption,
    BenefitAuditDigestCollision,
    BenefitAuditStoreError,
    SQLiteBenefitAuditStore,
)
from ctx.engine.planning_v3 import BenefitAuditStoreUnavailable


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result(name: str = "focused") -> BenefitSelectionResult:
    capability_id = f"skill:{name}"
    source_digest = _digest(capability_id)
    candidate = BenefitCandidate(
        capability_id=capability_id,
        source_digest=source_digest,
        resource_profile_digest=_digest(f"resources:{name}"),
        availability="executable",
        expected_task_benefit_ppm=800_000,
        relevance_ppm=900_000,
        trust_ppm=950_000,
        costs=ResourceCosts(context_tokens=40),
        evidence=EvidenceSummary(
            capability_id=capability_id,
            kind="skill",
            source_digest=source_digest,
            evidence_window_digest=_digest(f"window:{name}"),
            opportunity_observable=True,
            opportunities_observed=2,
            exposed_count=1,
            effective_outcomes=1,
            validated_outcomes=1,
        ),
        source_trusted=True,
        security_approved=True,
        permissions_allowed=True,
        credentials_available=True,
        coverage_keys=("python", "testing"),
    )
    return NetBenefitPolicy(
        calibration_digest=_digest("calibration"),
        minimum_relevance_ppm=1,
        context_token_cost_u=1,
    ).select((candidate,))


def _database_bytes(path: Path, result_digest: str) -> bytes:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT result_json FROM benefit_audit_results WHERE result_digest = ?",
            (result_digest,),
        ).fetchone()
    assert row is not None
    return bytes(row[0])


def test_result_codec_is_strict_canonical_and_round_trips() -> None:
    result = _result()

    encoded = result.to_json()

    assert encoded == json.dumps(
        json.loads(encoded),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert BenefitSelectionResult.from_json(encoded) == result
    assert BenefitSelectionResult.from_json(encoded.encode("utf-8")) == result


def test_result_codec_rejects_noncanonical_unknown_duplicate_and_oversized_input() -> None:
    result = _result()
    value = json.loads(result.to_json())
    value["unknown"] = "field"

    with pytest.raises(BenefitValidationError, match="fields"):
        BenefitSelectionResult.from_json(json.dumps(value, separators=(",", ":")))
    with pytest.raises(BenefitValidationError, match="duplicate"):
        BenefitSelectionResult.from_json('{"schema":"x","schema":"y"}')
    with pytest.raises(BenefitValidationError, match="canonical"):
        BenefitSelectionResult.from_json(json.dumps(json.loads(result.to_json()), indent=2))
    with pytest.raises(BenefitValidationError, match="bounded"):
        BenefitSelectionResult.from_json(b" " * (MAX_BENEFIT_RESULT_JSON_BYTES + 1))
    with pytest.raises(BenefitValidationError, match="bounded"):
        BenefitSelectionResult.from_json(bytearray(b" " * (MAX_BENEFIT_RESULT_JSON_BYTES + 1)))
    with pytest.raises(BenefitValidationError, match="canonical ASCII"):
        BenefitSelectionResult.from_json('{"schema":"caf\u00e9"}')
    with pytest.raises(BenefitValidationError, match="canonical ASCII"):
        BenefitSelectionResult.from_json('"\ud800"')


def test_store_round_trips_durable_exact_bytes_and_is_byte_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    result = _result()
    store = SQLiteBenefitAuditStore(path)

    assert store.store(result) == result.result_digest
    first_bytes = _database_bytes(path, result.result_digest)
    assert first_bytes == result.to_json().encode("utf-8")

    assert store.store(result) == result.result_digest
    assert _database_bytes(path, result.result_digest) == first_bytes
    assert SQLiteBenefitAuditStore(path).load(result.result_digest) == result


def test_store_never_deletes_prior_results(tmp_path: Path) -> None:
    store = SQLiteBenefitAuditStore(tmp_path / "private" / "benefit-audit.sqlite3")
    first = _result("first")
    second = _result("second")

    store.store(first)
    store.store(second)

    assert store.load(first.result_digest) == first
    assert store.load(second.result_digest) == second


def test_same_digest_with_different_bytes_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    result = _result()
    store = SQLiteBenefitAuditStore(path)
    store.store(result)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE benefit_audit_results SET result_json = ? WHERE result_digest = ?",
            (b"{}", result.result_digest),
        )

    with pytest.raises(BenefitAuditDigestCollision):
        store.store(result)
    with pytest.raises(BenefitAuditCorruption):
        store.load(result.result_digest)


def test_corrupt_persisted_digest_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    result = _result()
    store = SQLiteBenefitAuditStore(path)
    store.store(result)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE benefit_audit_results SET content_digest = ? WHERE result_digest = ?",
            (_digest("wrong"), result.result_digest),
        )

    with pytest.raises(BenefitAuditCorruption, match="content digest"):
        store.load(result.result_digest)


def test_self_consistent_but_malformed_persisted_bytes_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    result = _result()
    store = SQLiteBenefitAuditStore(path)
    store.store(result)
    corrupt = b"{}"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE benefit_audit_results
               SET result_json = ?, byte_length = ?, content_digest = ?
             WHERE result_digest = ?
            """,
            (corrupt, len(corrupt), hashlib.sha256(corrupt).hexdigest(), result.result_digest),
        )

    with pytest.raises(BenefitAuditCorruption, match="invalid"):
        store.load(result.result_digest)


def test_unexpected_database_trigger_is_treated_as_corruption(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    SQLiteBenefitAuditStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER delete_old_audits BEFORE INSERT ON benefit_audit_results
            BEGIN
                DELETE FROM benefit_audit_results;
            END
            """
        )

    with pytest.raises(BenefitAuditCorruption, match="objects"):
        SQLiteBenefitAuditStore(path)


def test_missing_schema_in_existing_database_fails_closed_without_recreation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    result = _result()
    store = SQLiteBenefitAuditStore(path)
    store.store(result)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE benefit_audit_results")

    with pytest.raises(BenefitAuditCorruption, match="objects"):
        store.load(result.result_digest)

    with sqlite3.connect(path) as connection:
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    assert objects == []


def test_concurrent_first_initializers_serialize_schema_creation(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"

    with ThreadPoolExecutor(max_workers=8) as executor:
        stores = tuple(executor.map(lambda _index: SQLiteBenefitAuditStore(path), range(8)))

    result = _result()
    assert stores[0].store(result) == result.result_digest
    assert all(store.load(result.result_digest) == result for store in stores)


def test_oversized_persisted_blob_is_rejected_before_materialization(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    result = _result()
    store = SQLiteBenefitAuditStore(path)
    store.store(result)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE benefit_audit_results
               SET result_json = zeroblob(?), byte_length = ?
             WHERE result_digest = ?
            """,
            (
                MAX_BENEFIT_RESULT_JSON_BYTES + 1,
                MAX_BENEFIT_RESULT_JSON_BYTES + 1,
                result.result_digest,
            ),
        )

    with pytest.raises(BenefitAuditCorruption, match="bounded size"):
        store.load(result.result_digest)
    with pytest.raises(BenefitAuditCorruption, match="bounded size"):
        store.store(result)


@pytest.mark.parametrize("column", ["byte_length", "content_digest"])
def test_oversized_dynamic_metadata_is_rejected_without_materialization(
    tmp_path: Path,
    column: str,
) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    result = _result()
    store = SQLiteBenefitAuditStore(path)
    store.store(result)
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE benefit_audit_results SET {column} = zeroblob(?) WHERE result_digest = ?",
            (1024 * 1024, result.result_digest),
        )

    with pytest.raises(BenefitAuditCorruption):
        store.load(result.result_digest)


def test_operational_open_and_path_failures_are_store_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied_directory(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("ctx.engine.benefit_audit_store.ensure_secure_directory", denied_directory)
    with pytest.raises(BenefitAuditStoreUnavailable, match="prepare"):
        SQLiteBenefitAuditStore(tmp_path / "private" / "benefit-audit.sqlite3")


def test_symlinked_store_parent_is_security_failure_not_planner_unavailability(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-private"
    real_parent.mkdir(mode=0o700)
    linked_parent = tmp_path / "linked-private"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        SQLiteBenefitAuditStore(linked_parent / "benefit-audit.sqlite3")


def test_operational_sqlite_failures_are_store_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("ctx.engine.benefit_audit_store.sqlite3.connect", unavailable)
    with pytest.raises(BenefitAuditStoreUnavailable, match="unavailable"):
        SQLiteBenefitAuditStore(tmp_path / "private" / "benefit-audit.sqlite3")


def test_failed_first_initialization_cleans_exact_file_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    real_connect = cast(Callable[..., sqlite3.Connection], sqlite3.connect)
    call_count = 0

    def fail_once(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("unable to open database file")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("ctx.engine.benefit_audit_store.sqlite3.connect", fail_once)

    with pytest.raises(BenefitAuditStoreUnavailable, match="unavailable"):
        SQLiteBenefitAuditStore(path)
    assert not path.exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    assert not Path(f"{path}-journal").exists()

    store = SQLiteBenefitAuditStore(path)
    result = _result()
    assert store.store(result) == result.result_digest
    assert store.load(result.result_digest) == result


def test_preexisting_sidecar_is_never_deleted_as_failed_initialization_output(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    path.parent.mkdir(mode=0o700)
    sidecar = Path(f"{path}-wal")
    sentinel = b"pre-existing-sidecar"
    sidecar.write_bytes(sentinel)
    sidecar.chmod(0o600)

    with pytest.raises(BenefitAuditCorruption, match="pre-existing SQLite sidecars"):
        SQLiteBenefitAuditStore(path)

    assert not path.exists()
    assert sidecar.read_bytes() == sentinel


def test_malformed_and_programming_errors_are_not_mislabeled_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteBenefitAuditStore(tmp_path / "private" / "benefit-audit.sqlite3")

    with pytest.raises(TypeError):
        store.store("not-a-result")  # type: ignore[arg-type]
    with pytest.raises(BenefitValidationError):
        BenefitSelectionResult.from_json("{}")
    with pytest.raises(ValueError, match="SHA-256"):
        store.load("not-a-digest")

    def programming_failure(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.ProgrammingError("caller misuse")

    monkeypatch.setattr("ctx.engine.benefit_audit_store.sqlite3.connect", programming_failure)
    with pytest.raises(BenefitAuditStoreError, match="programming"):
        store.load(_digest("missing"))


def test_concurrent_idempotent_writers_preserve_one_exact_result(tmp_path: Path) -> None:
    path = tmp_path / "private" / "benefit-audit.sqlite3"
    store = SQLiteBenefitAuditStore(path)
    result = _result()

    with ThreadPoolExecutor(max_workers=8) as executor:
        digests = tuple(executor.map(store.store, (result,) * 32))

    assert digests == (result.result_digest,) * 32
    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM benefit_audit_results").fetchone()
    assert count == (1,)
    assert store.load(result.result_digest) == result
