from __future__ import annotations

import hashlib
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ctx.core.install_policy_store import persist_install_policy
from ctx.engine.installation import InstallConsentPolicy
from ctx.engine.protocol import MATERIAL_RECEIPT_SCHEMA_V3, EngineEvent, HostAction, Transition
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION, InvalidEventError
from ctx.engine.replay import DefaultReplayInputFactory
from ctx.engine.store import (
    ActivationActionClaimGuard,
    ActivationExecutionOutcomeConflict,
    ActivationExecutionOutcomeRequired,
    JournalCorruption,
    SQLiteEngineStore,
    StreamId,
)
from ctx.runtime import ReleaseSkillActivationError, activate_installed_release_skill
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.release_skill_dispatcher import (
    ReleaseSkillInstallRequest,
    _scope,
    dispatch_release_skill_install,
)
import ctx.runtime.release_skill_lifecycle as lifecycle_module

TARGET_SHA256 = "c87c65b5b09f48e27c683fb5ada9d8bc377d6d72d7742ce7aac3c2d3d97ac441"
NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
AFTER_EXPIRY = datetime(2026, 8, 2, 13, 31, tzinfo=UTC)
BEFORE_INSTALL = datetime(2026, 8, 2, 12, 29, tzinfo=UTC)
ACTIVATED_AT = datetime(2026, 8, 2, 12, 31, tzinfo=UTC)
AFTER_ACTIVATION = datetime(2026, 8, 2, 12, 45, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(tmp_path: Path) -> ReleaseSkillInstallRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    skill_root = tmp_path / "skills"
    skill_root.mkdir(mode=0o700)
    if os.name != "nt":
        state_root.chmod(0o700)
        skill_root.chmod(0o700)
    request = ReleaseSkillInstallRequest(
        host_context_id="host-neutral-test",
        host_identity_digest=_digest("host-neutral-test"),
        task="Repair nested Python context-manager state restoration",
        language="Python",
        session_id="release-install-session",
        workspace=workspace,
        journal_path=state_root / "engine.sqlite3",
        benefit_audit_path=state_root / "benefit.sqlite3",
        policy_store_root=state_root / "install-policy",
        skill_store_root=skill_root,
        occurred_at="2026-08-02T12:00:00Z",
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    installed = dispatch_release_skill_install(request, trusted_utc_now=lambda: NOW)
    assert installed.status == "installed"
    return request


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_reopens_stream_verifies_cas_and_commits_exact_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    observed_receipts: list[EngineEvent] = []

    from ctx.engine.engine import CtxEngine

    original = CtxEngine.process_activation_receipt

    def observed_process(
        engine: CtxEngine,
        event: EngineEvent,
        guard: ActivationActionClaimGuard,
    ):
        if (
            event.kind == "ActionApplied"
            and event.payload.get("action_kind") == "ActivateCapability"
        ):
            observed_receipts.append(event)
        return original(engine, event, guard)

    monkeypatch.setattr(CtxEngine, "process_activation_receipt", observed_process)

    evidence = activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    assert evidence.status == "active"
    assert evidence.capability_id == RELEASE_INSTALL_SKILL_ID
    assert evidence.release_root_digest
    assert evidence.activation_action_content_digest
    assert evidence.activation_receipt_content_digest
    assert evidence.activation_record_digest
    assert evidence.installed_lineage_digest
    assert evidence.material_identity_digest
    assert evidence.skill_cas_root_identity_digest
    assert evidence.evidence_digest
    assert {field.name for field in fields(evidence)} == {
        "status",
        "capability_id",
        "release_root_digest",
        "activation_action_content_digest",
        "activation_receipt_content_digest",
        "activation_record_digest",
        "installed_lineage_digest",
        "material_identity_digest",
        "skill_cas_root_identity_digest",
        "evidence_digest",
    }
    assert len(observed_receipts) == 1
    verification = observed_receipts[0].payload["verification"]
    assert verification == {
        "schema": MATERIAL_RECEIPT_SCHEMA_V3,
        "host_state": "active",
        "capability_id": observed_receipts[0].payload["verification"]["capability_id"],
        "capability_kind": "skill",
        "catalog_identity": observed_receipts[0].payload["verification"]["catalog_identity"],
        "material_identity": observed_receipts[0].payload["verification"]["material_identity"],
        "authorized_material": observed_receipts[0].payload["verification"]["authorized_material"],
    }
    assert verification["capability_id"] == RELEASE_INSTALL_SKILL_ID
    assert all(
        field.name not in {"action", "content", "material_bytes", "path"}
        for field in fields(evidence)
    )

    stream_id = StreamId.from_scope(observed_receipts[0].scope)
    head = SQLiteEngineStore(request.journal_path).load_head(stream_id)
    assert head.revision == 6
    assert head.record_digest == evidence.activation_record_digest


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_generic_engine_process_cannot_forge_activation_receipt(tmp_path: Path) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    receipt = lifecycle_module._activation_receipt_event(
        action,
        expected_revision=5,
        observed_at="2026-08-02T12:31:00Z",
    )
    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )

    with pytest.raises(ActivationExecutionOutcomeRequired, match="verified execution outcome"):
        engine.process(receipt)

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_generic_engine_process_cannot_forge_activation_failure(tmp_path: Path) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    failure = EngineEvent(
        event_id=f"ctx-release-activation-failure-{action.content_digest}",
        kind="ActionFailed",
        scope=action.scope,
        expected_revision=5,
        occurred_at="2026-08-02T12:31:00Z",
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "error": {"code": "activation-failed"},
        },
        privacy=action.privacy,
        correlation_id=action.plan_id,
        causation_id=action.action_id,
    )
    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )

    with pytest.raises(ActivationExecutionOutcomeRequired, match="verified execution outcome"):
        engine.process(failure)

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_expired_unclaimed_activation_is_retired_without_host_authority(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    expiry = _activation_expiry_event(action)
    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
        trusted_utc_now=lambda: AFTER_EXPIRY,
    )

    # A genuinely expired, never-claimed activation is retirable: refusing it is
    # what used to wedge the stream forever. The forgery bars are asserted by the
    # sibling tests below (premature clock, tampered payload, settled action).
    engine.process(expiry)

    assert _journal_revision(request) == 6
    with sqlite3.connect(request.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_activation_outcomes").fetchone() == (
            0,
        )
        assert connection.execute(
            "SELECT count(*) FROM engine_activation_claim_settlements"
        ).fetchone() == (0,)


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_expiry_is_refused_before_the_trusted_expiry_instant(tmp_path: Path) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    expiry = _activation_expiry_event(action)
    from ctx.engine.engine import CtxEngine, CtxEngineError

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
        trusted_utc_now=lambda: datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(CtxEngineError, match="has not expired"):
        engine.process(expiry)

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_expiry_payload_tampering_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    tampered = replace(
        _activation_expiry_event(action),
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": _digest("tampered-activation-content"),
            "action_precondition_revision": action.precondition_revision,
            "reason": "expired",
        },
    )
    from ctx.engine.engine import CtxEngine, CtxEngineError

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )

    with pytest.raises(CtxEngineError, match="exact pending authority"):
        engine.process(tampered)

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_settled_activation_cannot_be_retired_as_expired(tmp_path: Path) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)
    settled_revision = _journal_revision(request)
    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )

    # An activation that actually reached the host is settled and no longer
    # pending, so its authority can never be retired as if no host mutation had
    # happened.
    with pytest.raises(InvalidEventError, match="unknown or completed action"):
        engine.process(_activation_expiry_event(action, expected_revision=settled_revision))

    assert _journal_revision(request) == settled_revision


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_guard_derivation_covers_rollback_activation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    receipt = lifecycle_module._activation_receipt_event(
        action,
        expected_revision=5,
        observed_at="2026-08-02T12:31:00Z",
    )
    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    pending = tuple(
        item for item in snapshot.state.pending_effects if item.action.action_id == action.action_id
    )
    assert len(pending) == 1
    rollback_state = SimpleNamespace(
        pending_effects=(replace(pending[0], effect="rollback-activate"),)
    )

    derived = CtxEngine._activation_receipt_claim_guard(  # noqa: SLF001
        receipt,
        rollback_state,  # type: ignore[arg-type]
    )

    # Derivation stays keyed on the action kind, not the pending effect: a
    # rollback-activate receipt must still derive a pending effect so that a
    # missing verified outcome is refused instead of committing unguarded.
    assert derived is not None
    assert derived.action == action
    assert derived.effect == "rollback-activate"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_claim_is_durable_before_cas_material_is_inspected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    observations: list[tuple[bool, bool]] = []
    original = lifecycle_module._verify_exact_skill_material

    def inspect_after_claim(root_identity, capability):
        status = SQLiteEngineStore(request.journal_path).activation_execution_status(
            StreamId.from_scope(action.scope),
            action.action_id,
        )
        observations.append((status.claimed, status.outcome_recorded))
        return original(root_identity, capability)

    monkeypatch.setattr(
        lifecycle_module,
        "_verify_exact_skill_material",
        inspect_after_claim,
    )

    evidence = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: ACTIVATED_AT,
    )

    assert evidence.status == "active"
    assert observations[0] == (True, False)
    assert observations[-1] == (True, True)


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_receipt_uses_actual_durable_observation_time_and_reconciles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    receipts: list[EngineEvent] = []
    from ctx.engine.engine import CtxEngine

    original = CtxEngine.process_activation_receipt

    def observed_process(
        engine: CtxEngine,
        event: EngineEvent,
        guard: ActivationActionClaimGuard,
    ):
        receipts.append(event)
        return original(engine, event, guard)

    monkeypatch.setattr(CtxEngine, "process_activation_receipt", observed_process)

    first = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: ACTIVATED_AT,
    )
    second = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: AFTER_ACTIVATION,
    )

    assert first == second
    assert len(receipts) == 1
    assert receipts[0].occurred_at == "2026-08-02T12:31:00Z"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_retry_reconciles_durable_outcome_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    from ctx.engine.engine import CtxEngine

    original = CtxEngine.process_activation_receipt

    def interrupt_before_receipt(
        engine: CtxEngine,
        event: EngineEvent,
        guard: ActivationActionClaimGuard,
    ):
        raise OSError("injected pre-receipt interruption")

    monkeypatch.setattr(
        CtxEngine,
        "process_activation_receipt",
        interrupt_before_receipt,
    )
    with pytest.raises(ReleaseSkillActivationError, match="operation failed"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: ACTIVATED_AT,
        )

    status = SQLiteEngineStore(request.journal_path).activation_execution_status(
        StreamId.from_scope(action.scope),
        action.action_id,
    )
    assert status.claimed and status.outcome_recorded and not status.settled
    assert status.observed_at == "2026-08-02T12:31:00Z"
    assert _journal_revision(request) == 5

    monkeypatch.setattr(CtxEngine, "process_activation_receipt", original)
    evidence = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: AFTER_ACTIVATION,
    )

    assert evidence.status == "active"
    assert _journal_revision(request) == 6


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_fabricated_activation_settlement_guard_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
        trusted_utc_now=lambda: ACTIVATED_AT,
    )
    original_process = CtxEngine.process_activation_receipt

    def stop_before_receipt(
        engine: CtxEngine,
        event: EngineEvent,
        guard: ActivationActionClaimGuard,
    ):
        raise OSError("stop before receipt")

    monkeypatch.setattr(CtxEngine, "process_activation_receipt", stop_before_receipt)
    with pytest.raises(ReleaseSkillActivationError, match="operation failed"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: ACTIVATED_AT,
        )
    monkeypatch.setattr(CtxEngine, "process_activation_receipt", original_process)
    status = engine.activation_execution_status(action)
    assert status.outcome_digest is not None and status.observed_at is not None
    receipt = lifecycle_module._activation_receipt_event(
        action,
        expected_revision=5,
        observed_at=status.observed_at,
    )
    forged = ActivationActionClaimGuard(
        action_id=action.action_id,
        action_content_digest=action.content_digest,
        mode="applied",
        execution_outcome_digest=_digest("forged-activation-outcome"),
    )

    with pytest.raises(ActivationExecutionOutcomeConflict, match="durable outcome"):
        engine.process_activation_receipt(receipt, forged)

    assert _journal_revision(request) == 5
    engine.process_activation_receipt(
        receipt,
        ActivationActionClaimGuard(
            action_id=action.action_id,
            action_content_digest=action.content_digest,
            mode="applied",
            execution_outcome_digest=status.outcome_digest,
        ),
    )
    evidence = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: AFTER_ACTIVATION,
    )
    assert evidence.status == "active"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_legacy_active_journal_remains_readable_without_fabricated_authority(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: ACTIVATED_AT,
    )
    import sqlite3

    with sqlite3.connect(request.journal_path) as connection:
        connection.execute("DELETE FROM engine_activation_claim_settlements")
        connection.execute("DELETE FROM engine_activation_outcomes")
        connection.execute("DELETE FROM engine_activation_claims")

    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )
    snapshot = engine.snapshot(_activation_scope(request))
    assert snapshot.state is not None
    capability = snapshot.state.capability(RELEASE_INSTALL_SKILL_ID)
    assert capability is not None and capability.activation == "active"
    action = _pending_activation(request)
    status = engine.activation_execution_status(action)
    assert not status.claimed and not status.outcome_recorded and not status.settled

    with pytest.raises(ReleaseSkillActivationError, match="legacy active"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: AFTER_ACTIVATION,
        )

    assert _journal_revision(request) == 6


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_unexpected_activation_failure_does_not_expose_local_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)

    def path_bearing_failure(*, engine, root_identity, scope):
        raise ValueError(f"unsafe local path: {request.journal_path}")

    monkeypatch.setattr(
        lifecycle_module,
        "_activation_lock_target",
        path_bearing_failure,
    )

    with pytest.raises(ReleaseSkillActivationError) as raised:
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: ACTIVATED_AT,
        )

    assert str(raised.value) == "release skill activation operation failed"
    assert str(request.journal_path) not in str(raised.value)


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_settlement_failure_rolls_back_receipt_and_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    original = SQLiteEngineStore._insert_activation_settlement

    def fail_settlement(store, connection, *, claim, guard, receipt):
        raise OSError("injected settlement write failure")

    monkeypatch.setattr(
        SQLiteEngineStore,
        "_insert_activation_settlement",
        fail_settlement,
    )
    with pytest.raises(ReleaseSkillActivationError, match="operation failed"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: ACTIVATED_AT,
        )

    assert _journal_revision(request) == 5
    assert (
        SQLiteEngineStore(request.journal_path)
        .load_head(StreamId.from_scope(_activation_scope(request)))
        .revision
        == 5
    )

    monkeypatch.setattr(
        SQLiteEngineStore,
        "_insert_activation_settlement",
        staticmethod(original),
    )
    action = _pending_activation(request)
    store = SQLiteEngineStore(request.journal_path)
    status = store.activation_execution_status(
        StreamId.from_scope(action.scope),
        action.action_id,
    )
    assert status.outcome_digest is not None
    wrong_time_receipt = lifecycle_module._activation_receipt_event(
        action,
        expected_revision=5,
        observed_at="2026-08-02T12:32:00Z",
    )
    from ctx.engine.engine import CtxEngine

    engine = CtxEngine(
        store=store,
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )
    with pytest.raises(JournalCorruption, match="activation settlement receipt"):
        engine.process_activation_receipt(
            wrong_time_receipt,
            ActivationActionClaimGuard(
                action_id=action.action_id,
                action_content_digest=action.content_digest,
                mode="applied",
                execution_outcome_digest=status.outcome_digest,
            ),
        )
    assert _journal_revision(request) == 5

    evidence = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: AFTER_ACTIVATION,
    )
    assert evidence.status == "active"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_retries_when_material_lock_binding_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    original = lifecycle_module._activation_lock_target
    calls = 0

    def changed_once(*, engine, root_identity, scope):
        nonlocal calls
        calls += 1
        target, digest = original(
            engine=engine,
            root_identity=root_identity,
            scope=scope,
        )
        return (target, _digest("stale-material")) if calls == 1 else (target, digest)

    monkeypatch.setattr(lifecycle_module, "_activation_lock_target", changed_once)

    evidence = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: ACTIVATED_AT,
    )

    assert evidence.status == "active"
    assert calls == 2


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_release_root_binding_mismatch_before_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    monkeypatch.setattr(
        lifecycle_module,
        "_release_skill_host_descriptor_digest",
        lambda _request: _digest("different-release-root"),
    )

    with pytest.raises(ReleaseSkillActivationError, match="current release root"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: ACTIVATED_AT,
        )

    status = SQLiteEngineStore(request.journal_path).activation_execution_status(
        StreamId.from_scope(action.scope),
        action.action_id,
    )
    assert not status.claimed


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_retry_reconciles_to_same_evidence_without_another_record(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    first = activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)
    second = activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    assert second == first
    store = SQLiteEngineStore(request.journal_path)
    records = tuple(store.records(StreamId.from_scope(_activation_scope(request))))
    assert len(records) == 6


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_concurrent_activation_reconciles_to_one_receipt_and_same_evidence(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = tuple(
            pool.submit(
                activate_installed_release_skill,
                request,
                trusted_utc_now=lambda: NOW,
            )
            for _ in range(2)
        )
    evidence = tuple(future.result() for future in futures)

    assert evidence[0] == evidence[1]
    records = tuple(
        SQLiteEngineStore(request.journal_path).records(
            StreamId.from_scope(_activation_scope(request))
        )
    )
    assert len(records) == 6


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_retry_reconciles_after_commit_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    from ctx.engine.engine import CtxEngine

    original = CtxEngine.process_activation_receipt
    interrupted = False

    def interrupt_after_commit(
        engine: CtxEngine,
        event: EngineEvent,
        guard: ActivationActionClaimGuard,
    ):
        nonlocal interrupted
        transition = original(engine, event, guard)
        if event.kind == "ActionApplied" and not interrupted:
            interrupted = True
            raise OSError("injected post-commit interruption")
        return transition

    monkeypatch.setattr(CtxEngine, "process_activation_receipt", interrupt_after_commit)
    with pytest.raises(ReleaseSkillActivationError, match="operation failed"):
        activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    evidence = activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    assert evidence.status == "active"
    assert (
        len(
            tuple(
                SQLiteEngineStore(request.journal_path).records(
                    StreamId.from_scope(_activation_scope(request))
                )
            )
        )
        == 6
    )


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_tampered_skill_without_receipting(tmp_path: Path) -> None:
    request = _request(tmp_path)
    target = request.skill_store_root / TARGET_SHA256
    target.write_bytes(b"x" * target.stat().st_size)

    with pytest.raises(ReleaseSkillActivationError, match="exact installed UTF-8 material"):
        activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_replaced_cas_root_even_with_exact_copied_material(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    original = request.skill_store_root
    retired = tmp_path / "retired-skills"
    original.rename(retired)
    original.mkdir(mode=0o700)
    (original / TARGET_SHA256).write_bytes((retired / TARGET_SHA256).read_bytes())
    original.chmod(0o700)
    (original / TARGET_SHA256).chmod(0o600)

    with pytest.raises(ReleaseSkillActivationError, match="install target identity"):
        activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_workspace_identity_change_before_journal_or_cas_use(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    request.workspace.rename(tmp_path / "retired-workspace")
    request.workspace.mkdir()

    with pytest.raises(ReleaseSkillActivationError, match="workspace identity"):
        activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_expired_pending_authority_without_receipting(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    with pytest.raises(ReleaseSkillActivationError, match="expired"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: AFTER_EXPIRY,
        )

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_expiry_after_claim_before_outcome(tmp_path: Path) -> None:
    request = _request(tmp_path)
    action = _pending_activation(request)
    clock = iter((ACTIVATED_AT, AFTER_EXPIRY))

    with pytest.raises(ReleaseSkillActivationError, match="authority expired"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: next(clock),
        )

    status = SQLiteEngineStore(request.journal_path).activation_execution_status(
        StreamId.from_scope(action.scope),
        action.action_id,
    )
    assert status.claimed and not status.outcome_recorded and not status.settled
    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_clock_rollback_before_install_observation(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    with pytest.raises(ReleaseSkillActivationError, match="before installation"):
        activate_installed_release_skill(
            request,
            trusted_utc_now=lambda: BEFORE_INSTALL,
        )

    assert _journal_revision(request) == 5


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_activation_rejects_symlinked_journal_without_opening_alias(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    real_journal = request.journal_path.with_name("real-engine.sqlite3")
    request.journal_path.rename(real_journal)
    try:
        request.journal_path.symlink_to(real_journal)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ReleaseSkillActivationError, match="journal is absent or unsafe"):
        activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    assert real_journal.exists()


def _activation_scope(request: ReleaseSkillInstallRequest):
    return _scope(request)


def _pending_activation(request: ReleaseSkillInstallRequest):
    records = SQLiteEngineStore(request.journal_path).records(
        StreamId.from_scope(_activation_scope(request))
    )
    actions = tuple(
        action
        for record in records
        for action in Transition.from_json(record.transition_json).actions
        if action.kind == "ActivateCapability"
    )
    assert len(actions) == 1
    return actions[0]


def _activation_expiry_event(action: HostAction, *, expected_revision: int = 5) -> EngineEvent:
    """Build the exact ActionExpired envelope for one pending activation."""

    return EngineEvent(
        event_id=f"ctx-release-activation-expiry-{action.content_digest}",
        kind="ActionExpired",
        scope=action.scope,
        expected_revision=expected_revision,
        occurred_at="2026-08-02T12:31:00Z",
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "reason": "expired",
        },
        privacy=action.privacy,
        correlation_id=action.plan_id,
        causation_id=action.action_id,
    )


def _journal_revision(request: ReleaseSkillInstallRequest) -> int:
    import sqlite3

    with sqlite3.connect(request.journal_path) as connection:
        value = connection.execute("SELECT MAX(revision) FROM engine_journal").fetchone()[0]
    assert isinstance(value, int)
    return value
