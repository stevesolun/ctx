from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from ctx.engine import store as store_module
from ctx.engine.engine import CtxEngine
from ctx.engine.content import MaterialIdentity
from ctx.engine.installation import (
    InstallExecutionBinding,
    InstallPlanDescriptor,
    install_action_authorization_digest,
)
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.protocol import (
    INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    INSTALL_RECEIPT_SCHEMA_V3,
    HostAction,
    ScopeRef,
    Transition,
)
from ctx.engine.store import (
    EngineStoreError,
    InstallActionAlreadyClaimed,
    InstallActionClaimExpired,
    InstallActionClaimGuard,
    InstallActionClaimRequest,
    InstallActionClaimRequired,
    InstallExecutionOutcomeRequest,
    InstallExecutionOutcomeRequired,
    JournalCorruption,
    JournalRecord,
    RevisionConflict,
    SQLiteEngineStore,
    StreamId,
)
from tests.engine import test_engine_install_coordinator as support


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-claim-1",
        exposure_id="exposure-1",
        host_context_id="host-1",
    )


def _action(*, expires_at: str = "2026-08-01T13:00:00Z") -> HostAction:
    capability_id = "skill:testing"
    catalog = CatalogCapabilityIdentity.create(
        capability_id=capability_id,
        kind="skill",
        catalog_namespace_digest=_digest("catalog"),
    )
    material = MaterialIdentity.create(
        capability_id=capability_id,
        kind="skill",
        content_sha256=_digest("content"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind="skill",
        installer_id="ctx-local-skill-installer-v1",
        plan_digest=_digest("plan"),
        provenance_digest=_digest("provenance"),
        credential_requirement=False,
        result_material_identity_digest=material.identity_digest,
    )
    return HostAction(
        action_id="action-install-1",
        kind="InstallCapability",
        scope=_scope(),
        precondition_revision=1,
        entity_id=capability_id,
        source_digest=_digest("source"),
        plan_id="plan-1",
        catalog_snapshot_id=_digest("catalog-snapshot"),
        consent_id="consent-1",
        expires_at=expires_at,
        required_host_feature="installation",
        payload={
            "schema": INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
            "capability_kind": "skill",
            "catalog_identity": catalog.to_dict(),
            "result_material": material.to_dict(),
            "install_plan_descriptor": descriptor.to_dict(),
            "installer_digest": _digest("installer"),
            "policy_snapshot_digest": _digest("policy"),
        },
        verification={
            "receipt_required": True,
            "expected_state": "installed",
            "receipt_schema": INSTALL_RECEIPT_SCHEMA_V3,
        },
        rollback={
            "kind": "UninstallCapability",
            "installer_id": descriptor.installer_id,
        },
    )


def _record(*, revision: int, event_id: str, action: HostAction | None = None) -> JournalRecord:
    scope = _scope()
    transition = Transition(
        event_id=event_id,
        scope=scope,
        from_revision=revision - 1,
        to_revision=revision,
        actions=() if action is None else (action,),
    )
    replay_json = _canonical({"event_id": event_id, "scope": scope.to_dict()})
    return JournalRecord(
        stream_id=StreamId.from_scope(scope),
        revision=revision,
        event_id=event_id,
        event_content_digest=_digest(replay_json),
        replay_json=replay_json,
        transition_json=transition.to_json(),
        result_state_json=_canonical({"revision": revision}),
        privacy_classification="private",
        retention_class="local",
        reducer_version="reducer-v3",
    )


def _seed(store: SQLiteEngineStore, action: HostAction) -> tuple[JournalRecord, str]:
    committed = store.commit(
        expected_revision=0,
        record=_record(revision=1, event_id="issuing-event", action=action),
    )
    return committed.record, committed.record.record_digest


def _request(action: HostAction, head_digest: str) -> InstallActionClaimRequest:
    return InstallActionClaimRequest(
        stream_id=StreamId.from_scope(action.scope),
        expected_revision=1,
        expected_head_record_digest=head_digest,
        action_json=action.to_json(),
        authorization_digest=_digest("authorization"),
        execution_binding_json=_execution_binding(action).to_json(),
    )


def _execution_binding(action: HostAction) -> InstallExecutionBinding:
    raw_descriptor = action.payload.get("install_plan_descriptor")
    assert isinstance(raw_descriptor, Mapping)
    descriptor = InstallPlanDescriptor.from_dict(raw_descriptor)
    driver_digest = action.payload.get("installer_digest")
    assert isinstance(driver_digest, str)
    return InstallExecutionBinding(
        driver_id=descriptor.installer_id,
        driver_digest=driver_digest,
        host_identity_digest=_digest("install-host"),
        target_identity_digest=_digest("install-target"),
    )


def _record_outcome(
    store: SQLiteEngineStore,
    action: HostAction,
    request: InstallActionClaimRequest,
    *,
    outcome: Literal["applied", "failed"],
) -> InstallActionClaimGuard:
    material = action.payload.get("result_material")
    assert isinstance(material, Mapping)
    identity_digest = material.get("identity_digest")
    assert isinstance(identity_digest, str)
    binding = InstallExecutionBinding.from_json(request.execution_binding_json)
    return store.record_install_outcome(
        InstallExecutionOutcomeRequest(
            stream_id=request.stream_id,
            action_json=action.to_json(),
            execution_binding_digest=binding.binding_digest,
            outcome=outcome,
            observed_material_identity_digest=(identity_digest if outcome == "applied" else None),
            verification_digest=_digest(f"{outcome}-verification"),
        ),
        trusted_utc_now=_now,
    ).settlement_guard


def _now() -> datetime:
    return datetime(2026, 8, 1, 12, 30, tzinfo=UTC)


def _engine_seed(
    tmp_path: Path,
    *,
    trusted_utc_now: Callable[[], datetime] = _now,
) -> tuple[CtxEngine, SQLiteEngineStore, HostAction, InstallActionClaimRequest]:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    engine, policy = support._engine(
        tmp_path,
        store=store,
        trusted_utc_now=trusted_utc_now,
    )
    action = support._pending_install(engine)
    selection = support._selection()
    descriptor = support._descriptor()
    snapshot = engine.snapshot(action.scope)
    assert snapshot.record_digest is not None
    request = InstallActionClaimRequest(
        stream_id=StreamId.from_scope(action.scope),
        expected_revision=snapshot.revision,
        expected_head_record_digest=snapshot.record_digest,
        action_json=action.to_json(),
        authorization_digest=install_action_authorization_digest(
            action=action,
            selection=selection,
            descriptor=descriptor,
            catalog_snapshot_digest=support.CATALOG_DIGEST,
            policy_snapshot_digest=policy.policy_digest,
        ),
        execution_binding_json=_execution_binding(action).to_json(),
    )
    return engine, store, action, request


def test_existing_database_adds_claim_tables_without_changing_journal(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(path)
    action = _action()
    issued, _ = _seed(store, action)

    reopened = SQLiteEngineStore(path)

    assert list(reopened.records(issued.stream_id))[0].record_digest == issued.record_digest
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "engine_install_claims",
        "engine_install_outcomes",
        "engine_install_claim_settlements",
    } <= tables


def test_atomic_claim_is_durable_one_use_across_store_instances(tmp_path: Path) -> None:
    _, first, _, request = _engine_seed(tmp_path)
    path = first.path
    second = SQLiteEngineStore(path)

    def claim(store: SQLiteEngineStore) -> object:
        try:
            return store.claim_install(request, trusted_utc_now=_now)
        except InstallActionAlreadyClaimed as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, (first, second)))

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, InstallActionAlreadyClaimed) for item in outcomes) == 1
    with pytest.raises(InstallActionAlreadyClaimed):
        SQLiteEngineStore(path).claim_install(request, trusted_utc_now=_now)


def test_claim_checks_expiry_and_exact_current_head_under_write_lock(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    action = _action(expires_at="2026-08-01T12:30:00Z")
    _, head_digest = _seed(store, action)

    with pytest.raises(InstallActionClaimExpired):
        store.claim_install(_request(action, head_digest), trusted_utc_now=_now)

    live = _action()
    other_store = SQLiteEngineStore(tmp_path / "other" / "journal.sqlite3")
    _, live_head = _seed(other_store, live)
    with pytest.raises(RevisionConflict):
        other_store.claim_install(
            InstallActionClaimRequest(
                stream_id=StreamId.from_scope(live.scope),
                expected_revision=1,
                expected_head_record_digest=_digest("wrong-head"),
                action_json=live.to_json(),
                authorization_digest=_digest("authorization"),
                execution_binding_json=_execution_binding(live).to_json(),
            ),
            trusted_utc_now=_now,
        )


def test_claim_uses_the_protocol_canonical_fractional_timestamp_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(support, "NOW", "2026-08-01T12:00:00.123450Z")
    _, store, action, request = _engine_seed(tmp_path)
    assert action.expires_at == "2026-08-01T13:00:00.12345Z"

    claim = store.claim_install(
        request,
        trusted_utc_now=lambda: datetime(
            2026,
            8,
            1,
            12,
            30,
            0,
            123450,
            tzinfo=UTC,
        ),
    )

    assert claim.action_expires_at == "2026-08-01T13:00:00.12345Z"
    assert claim.claimed_at == "2026-08-01T12:30:00.12345Z"


def test_receipt_requires_exact_claim_and_settles_once(tmp_path: Path) -> None:
    engine, store, action, request = _engine_seed(tmp_path)
    receipt = support._event(
        "ActionApplied",
        4,
        "receipt-event",
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "verification": support._install_receipt_verification(action),
        },
    )
    missing_claim_guard = InstallActionClaimGuard(
        action_id=action.action_id,
        action_content_digest=action.content_digest,
        mode="applied",
        execution_outcome_digest=_digest("missing-outcome"),
    )
    with pytest.raises(InstallExecutionOutcomeRequired):
        engine.process(receipt)
    with pytest.raises(InstallActionClaimRequired):
        engine.process_install_receipt(receipt, missing_claim_guard)
    store.claim_install(request, trusted_utc_now=_now)
    with pytest.raises(InstallExecutionOutcomeRequired):
        engine.process_install_receipt(receipt, missing_claim_guard)
    guard = _record_outcome(store, action, request, outcome="applied")
    committed = engine.process_install_receipt(receipt, guard)
    assert engine.process_install_receipt(receipt, guard) == committed


def test_settlement_insert_failure_rolls_back_receipt_and_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, store, action, request = _engine_seed(tmp_path)
    store.claim_install(request, trusted_utc_now=_now)
    guard = _record_outcome(store, action, request, outcome="failed")
    receipt = support._event(
        "ActionFailed",
        4,
        "receipt-event",
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "error": {"code": "installer-failed"},
        },
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected settlement failure")

    monkeypatch.setattr(SQLiteEngineStore, "_insert_install_settlement", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected settlement failure"):
        engine.process_install_receipt(receipt, guard)

    assert store.load_head(StreamId.from_scope(action.scope)).revision == 4
    assert "receipt-event" not in {
        item.event_id for item in store.records(StreamId.from_scope(action.scope))
    }


def test_expired_guard_requires_action_to_remain_unclaimed(tmp_path: Path) -> None:
    current = [_now()]
    engine, store, action, request = _engine_seed(
        tmp_path,
        trusted_utc_now=lambda: current[0],
    )
    store.claim_install(request, trusted_utc_now=_now)
    current[0] = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
    with pytest.raises(InstallActionAlreadyClaimed):
        engine.process(
            support._event(
                "ActionExpired",
                4,
                "expiry-event",
                payload={
                    "action_id": action.action_id,
                    "action_kind": action.kind,
                    "action_content_digest": action.content_digest,
                    "action_precondition_revision": action.precondition_revision,
                    "reason": "expired",
                },
            )
        )


@pytest.mark.parametrize(
    "target",
    ["claim-schema", "claim-row", "outcome-schema", "outcome-row"],
)
def test_malformed_security_storage_fails_closed_without_rebuild(
    tmp_path: Path, target: str
) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    _, store, action, request = _engine_seed(tmp_path)
    store.claim_install(request, trusted_utc_now=_now)
    if target.startswith("outcome-"):
        _record_outcome(store, action, request, outcome="applied")
    with sqlite3.connect(path) as connection:
        table = (
            "engine_install_outcomes" if target.startswith("outcome-") else "engine_install_claims"
        )
        if target.endswith("-schema"):
            connection.execute(f"ALTER TABLE {table} ADD COLUMN forged TEXT")
        elif table == "engine_install_claims":
            connection.execute(
                "UPDATE engine_install_claims SET authorization_digest = ?",
                (_digest("forged"),),
            )
        else:
            connection.execute(
                "UPDATE engine_install_outcomes SET verification_digest = ?",
                (_digest("forged"),),
            )

    with pytest.raises(JournalCorruption):
        SQLiteEngineStore(path)
    with sqlite3.connect(path) as connection:
        if target.endswith("-schema"):
            assert "forged" in {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
        elif table == "engine_install_claims":
            assert connection.execute(
                "SELECT authorization_digest FROM engine_install_claims"
            ).fetchone()[0] == _digest("forged")
        else:
            assert connection.execute(
                "SELECT verification_digest FROM engine_install_outcomes"
            ).fetchone()[0] == _digest("forged")


def test_self_consistent_forged_claim_is_rejected_by_journal_anchors(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    _, store, _, request = _engine_seed(tmp_path)
    store.claim_install(request, trusted_utc_now=_now)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM engine_install_claims").fetchone()
        assert row is not None
        forged = dict(row)
        forged.update(
            {
                "issuing_record_digest": _digest("forged-issuing"),
                "claimed_head_record_digest": _digest("forged-head"),
                "claimed_head_state_digest": _digest("forged-state"),
                "authorization_digest": _digest("forged-authorization"),
            }
        )
        claim_content = {
            key: forged[key]
            for key in (
                "action_content_digest",
                "action_expires_at",
                "action_id",
                "action_json",
                "action_kind",
                "authorization_digest",
                "claimed_at",
                "claimed_head_record_digest",
                "claimed_head_revision",
                "claimed_head_state_digest",
                "execution_binding_digest",
                "execution_binding_json",
                "issuing_record_digest",
                "precondition_revision",
                "stream_key",
            )
        }
        connection.execute(
            """
            UPDATE engine_install_claims
               SET issuing_record_digest = ?, claimed_head_record_digest = ?,
                   claimed_head_state_digest = ?, authorization_digest = ?, claim_digest = ?
            """,
            (
                forged["issuing_record_digest"],
                forged["claimed_head_record_digest"],
                forged["claimed_head_state_digest"],
                forged["authorization_digest"],
                _digest(_canonical(claim_content)),
            ),
        )

    with pytest.raises(JournalCorruption):
        SQLiteEngineStore(path)


def test_self_consistent_claim_cannot_extend_the_journaled_action_expiry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    _, store, _, request = _engine_seed(tmp_path)
    store.claim_install(request, trusted_utc_now=_now)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM engine_install_claims").fetchone()
        assert row is not None
        forged = dict(row)
        forged.update(
            {
                "action_expires_at": "2027-08-01T13:00:00Z",
                "claimed_at": "2026-08-01T13:00:01Z",
            }
        )
        claim_content = {
            key: forged[key]
            for key in (
                "action_content_digest",
                "action_expires_at",
                "action_id",
                "action_json",
                "action_kind",
                "authorization_digest",
                "claimed_at",
                "claimed_head_record_digest",
                "claimed_head_revision",
                "claimed_head_state_digest",
                "execution_binding_digest",
                "execution_binding_json",
                "issuing_record_digest",
                "precondition_revision",
                "stream_key",
            )
        }
        connection.execute(
            """
            UPDATE engine_install_claims
               SET action_expires_at = ?, claimed_at = ?, claim_digest = ?
            """,
            (
                forged["action_expires_at"],
                forged["claimed_at"],
                _digest(_canonical(claim_content)),
            ),
        )

    with pytest.raises(JournalCorruption):
        SQLiteEngineStore(path)


def test_self_consistent_nonexistent_settlement_receipt_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    engine, store, action, request = _engine_seed(tmp_path)
    store.claim_install(request, trusted_utc_now=_now)
    guard = _record_outcome(store, action, request, outcome="applied")
    engine.process_install_receipt(
        support._event(
            "ActionApplied",
            4,
            "receipt-event",
            payload={
                "action_id": action.action_id,
                "action_kind": action.kind,
                "action_content_digest": action.content_digest,
                "action_precondition_revision": action.precondition_revision,
                "verification": support._install_receipt_verification(action),
            },
        ),
        guard,
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM engine_install_claim_settlements").fetchone()
        assert row is not None
        values = dict(row)
        values.update(
            {
                "receipt_event_id": "nonexistent-receipt",
                "receipt_event_content_digest": _digest("nonexistent-event"),
                "receipt_record_digest": _digest("nonexistent-record"),
            }
        )
        settlement_content = {
            key: values[key]
            for key in (
                "action_content_digest",
                "action_id",
                "claim_digest",
                "outcome",
                "receipt_event_content_digest",
                "receipt_event_id",
                "receipt_record_digest",
                "stream_key",
            )
        }
        connection.execute(
            """
            UPDATE engine_install_claim_settlements
               SET receipt_event_id = ?, receipt_event_content_digest = ?,
                   receipt_record_digest = ?, settlement_digest = ?
            """,
            (
                values["receipt_event_id"],
                values["receipt_event_content_digest"],
                values["receipt_record_digest"],
                _digest(_canonical(settlement_content)),
            ),
        )

    with pytest.raises(JournalCorruption):
        SQLiteEngineStore(path)


def test_unexpected_after_insert_trigger_cannot_restore_claim_reuse(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    _, store, _, request = _engine_seed(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER delete_install_claim AFTER INSERT ON engine_install_claims
            BEGIN
                DELETE FROM engine_install_claims
                 WHERE stream_key = NEW.stream_key AND action_id = NEW.action_id;
            END
            """
        )

    with pytest.raises(JournalCorruption, match="unexpected triggers"):
        store.claim_install(request, trusted_utc_now=_now)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM engine_install_claims").fetchone()[0] == 0
        connection.execute("DROP TRIGGER delete_install_claim")
    store.claim_install(request, trusted_utc_now=_now)
    with pytest.raises(InstallActionAlreadyClaimed):
        store.claim_install(request, trusted_utc_now=_now)


def test_claim_insert_is_reread_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store, _, request = _engine_seed(tmp_path)
    monkeypatch.setattr(store_module, "_insert_claim", lambda *_args: None)

    with pytest.raises(JournalCorruption, match="not durably preserved"):
        store.claim_install(request, trusted_utc_now=_now)

    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM engine_install_claims").fetchone()[0] == 0


def test_settlement_insert_is_reread_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, store, action, request = _engine_seed(tmp_path)
    store.claim_install(request, trusted_utc_now=_now)
    guard = _record_outcome(store, action, request, outcome="failed")
    monkeypatch.setattr(
        SQLiteEngineStore,
        "_insert_install_settlement",
        staticmethod(lambda *_args, **_kwargs: None),
    )
    receipt = support._event(
        "ActionFailed",
        4,
        "receipt-reread",
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "error": {"code": "installer-failed"},
        },
    )

    with pytest.raises(JournalCorruption, match="settlement was not durably preserved"):
        engine.process_install_receipt(receipt, guard)

    assert store.load_head(StreamId.from_scope(action.scope)).revision == 4
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM engine_install_claim_settlements").fetchone()[
                0
            ]
            == 0
        )


def test_trusted_clock_failure_does_not_expose_private_exception_text(tmp_path: Path) -> None:
    _, store, _, request = _engine_seed(tmp_path)

    def failed_clock() -> datetime:
        raise RuntimeError("private-clock-provider-detail")

    with pytest.raises(EngineStoreError) as raised:
        store.claim_install(request, trusted_utc_now=failed_clock)

    assert str(raised.value) == "trusted UTC clock failed"
    assert raised.value.__cause__ is None
