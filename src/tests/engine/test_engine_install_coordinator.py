from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctx.engine.content import MaterialIdentity
from ctx.engine.engine import CtxEngine, CtxEngineError
from ctx.engine.installation import (
    HeldInstallConsentPolicyAuthority,
    InstallConsentPolicy,
    InstallExecutionBinding,
    InstallPlanDescriptor,
    InteractiveInstallDecisionGuard,
    InteractiveInstallDecisionReservation,
)
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import (
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
)
from ctx.engine.protocol import (
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    EngineEvent,
    HostAction,
    ScopeRef,
)
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION, INSTALLER_DIGEST
from ctx.engine.replay import (
    DefaultReplayInputFactory,
    ObservationReference,
    PlanningContext,
    StructuredSurrogate,
)
from ctx.engine.state import EngineState
from ctx.engine.store import (
    ActivationActionClaimGuard,
    CommitResult,
    InstallActionAlreadyClaimed,
    InstallActionClaimGuard,
    InstallExecutionOutcomeRequired,
    JournalRecord,
    SQLiteEngineStore,
    StreamId,
)


NOW = "2026-08-01T12:00:00Z"
CATALOG_DIGEST = "4" * 64
SOURCE_DIGEST = "6" * 64
INSTALL_PLAN_DIGEST = "7" * 64
BEFORE_EXPIRY = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)


class _HeldPolicyAuthority:
    def __init__(
        self,
        policy: InstallConsentPolicy,
        on_assert_current: Callable[[], None] | None = None,
    ) -> None:
        self.policy = policy
        self.assertions = 0
        self._on_assert_current = on_assert_current

    def assert_current(self) -> None:
        self.assertions += 1
        if self._on_assert_current is not None:
            self._on_assert_current()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-install-1",
        exposure_id="exposure-1",
        host_context_id="host-1",
    )


def _event(
    kind: str,
    revision: int,
    event_id: str,
    *,
    payload: dict[str, object] | None = None,
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=_scope(),
        expected_revision=revision,
        occurred_at=NOW,
        payload=payload or {},
        correlation_id="plan-install-1",
        causation_id="cause-install-1",
        engine_version="engine-v3",
        planner_version="planner-v3",
        policy_version="policy-v1",
        host_descriptor_digest=_digest("host"),
        catalog_snapshot_digest=CATALOG_DIGEST,
        semantic_model_digest=_digest("model"),
        semantic_index_digest=_digest("index"),
        work_signature=_digest("work"),
        random_seed=17,
    )


def _normalizer(
    _reference: ObservationReference,
    _state: EngineState | None,
) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": ["python", "testing"],
            "languages": ["python"],
            "baseline_capability_ids": [],
            "active_capability_ids": [],
            "rejected_capability_ids": [],
            "requested_limit": 5,
        },
    )


def _planner(
    _observation: StructuredSurrogate,
    _state: EngineState | None,
    _context: PlanningContext,
) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": {
                "result_schema_id": "ctx.benefit-selection-result-v1",
                "result_digest": _digest("benefit-result"),
                "policy_schema_id": "ctx.net-benefit-policy-v3",
                "policy_digest": _digest("benefit-policy"),
                "selection_algorithm_id": "ctx.greedy-bounded-subset-exchange-v1",
                "calibration_digest": _digest("calibration"),
                "requested_limit": 5,
                "candidate_pool_count": 1,
                "search_evaluation_count": 1,
            },
            "capabilities": [_selection().to_mapping()],
        },
    )


def _catalog_identity() -> CatalogCapabilityIdentity:
    return CatalogCapabilityIdentity.create(
        capability_id="skill:remote-testing",
        kind="skill",
        catalog_namespace_digest=_digest("catalog-namespace"),
    )


def _material(*, salt: str = "installed-result") -> MaterialIdentity:
    return MaterialIdentity.create(
        capability_id="skill:remote-testing",
        kind="skill",
        content_sha256=_digest(f"material:{salt}"),
        content_bytes=256,
    )


def _selection() -> CapabilityPlanSelectionV3:
    descriptor = _descriptor()
    return CapabilityPlanSelectionV3(
        presentation=CapabilityCandidate(
            capability_id="skill:remote-testing",
            kind="skill",
            name="remote-testing",
            source_digest=SOURCE_DIGEST,
            normalized_score_ppm=900_000,
            matching_signals=("python", "testing"),
            reason_codes=("exact-tag-match",),
            actionability="install",
            install_descriptor_digest=descriptor.descriptor_digest,
            install_plan_digest=INSTALL_PLAN_DIGEST,
        ),
        catalog_identity=_catalog_identity(),
        benefit=CapabilityBenefitProjection(
            tier="executable",
            individual_net_benefit_u=600_000,
            marginal_net_benefit_u=600_000,
        ),
        authority=InstallPlanningAuthority(
            descriptor=descriptor,
            result_material=_material(),
        ),
    )


def _descriptor(
    *,
    result_material: MaterialIdentity | None = None,
    credential_requirement: bool = False,
) -> InstallPlanDescriptor:
    material = result_material or _material()
    return InstallPlanDescriptor.create(
        capability_id="skill:remote-testing",
        kind="skill",
        installer_id="ctx-local-skill-installer-v1",
        plan_digest=INSTALL_PLAN_DIGEST,
        provenance_digest=_digest("installation-snapshot"),
        credential_requirement=credential_requirement,
        result_material_identity_digest=material.identity_digest,
    )


def _execution_binding(
    descriptor: InstallPlanDescriptor | None = None,
) -> InstallExecutionBinding:
    descriptor = descriptor or _descriptor()
    return InstallExecutionBinding(
        driver_id=descriptor.installer_id,
        driver_digest=INSTALLER_DIGEST,
        host_identity_digest=_digest("host-installation-authority-v1"),
        target_identity_digest=_digest("ctx-skill-cas-target-v1"),
    )


def _desired_row(*, lease_id: str = "lease-1") -> dict[str, object]:
    selection = _selection()
    presentation = selection.presentation
    return {
        "capability_id": presentation.capability_id,
        "source_digest": presentation.source_digest,
        "lease_id": lease_id,
        "kind": presentation.kind,
        "actionability": presentation.actionability,
        "install_descriptor_digest": presentation.install_descriptor_digest,
        "install_plan_digest": presentation.install_plan_digest,
    }


def _install_receipt_verification(action: HostAction) -> dict[str, object]:
    return {
        "schema": INSTALL_RECEIPT_SCHEMA_V3,
        "host_state": "installed",
        "capability_id": action.entity_id,
        "capability_kind": action.payload["capability_kind"],
        "catalog_identity": action.payload["catalog_identity"],
        "material_identity": action.payload["result_material"],
        "install_plan_descriptor": action.payload["install_plan_descriptor"],
        "installer_digest": action.payload["installer_digest"],
        "policy_snapshot_digest": action.payload["policy_snapshot_digest"],
    }


def _material_receipt_verification(action: HostAction) -> dict[str, object]:
    return {
        "schema": MATERIAL_RECEIPT_SCHEMA_V3,
        "host_state": action.verification["expected_state"],
        "capability_id": action.entity_id,
        "capability_kind": action.payload["capability_kind"],
        "catalog_identity": action.payload["catalog_identity"],
        "material_identity": action.payload["material_identity"],
        "authorized_material": action.payload["authorized_material"],
    }


def _engine(
    tmp_path: Path,
    *,
    policy: InstallConsentPolicy | None = None,
    decision_basis: str = "interactive",
    verify_preapproval: bool = True,
    descriptor: InstallPlanDescriptor | None = None,
    loaded_policy: InstallConsentPolicy | None = None,
    decision: str | None = "granted",
    verify_interactive: bool = True,
    interactive_guard: InteractiveInstallDecisionGuard | None = None,
    trusted_utc_now: Callable[[], datetime] = lambda: BEFORE_EXPIRY,
    store: SQLiteEngineStore | None = None,
    policy_assert_current: Callable[[], None] | None = None,
) -> tuple[CtxEngine, InstallConsentPolicy]:
    policy = policy or InstallConsentPolicy.safe_default()
    loaded_policy = loaded_policy or policy
    descriptor = descriptor or _descriptor()

    @contextmanager
    def policy_guard(expected_digest: str) -> Iterator[HeldInstallConsentPolicyAuthority]:
        if loaded_policy.policy_digest != expected_digest:
            raise ValueError("current policy changed")
        yield _HeldPolicyAuthority(loaded_policy, policy_assert_current)

    @contextmanager
    def default_interactive_guard(
        _reservation: InteractiveInstallDecisionReservation,
    ) -> Iterator[None]:
        yield

    if decision_basis == "interactive" and verify_interactive and interactive_guard is None:
        interactive_guard = default_interactive_guard

    engine = CtxEngine(
        store=store or SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3"),
        replay_factory=DefaultReplayInputFactory(
            observation_normalizer=_normalizer,
            decision_planner=_planner,
            reducer_version=INSTALLATION_REDUCER_VERSION,
        ),
        install_policy_guard=(
            policy_guard if decision_basis == "preapproved-policy" and verify_preapproval else None
        ),
        interactive_install_decision_guard=interactive_guard,
        install_descriptor_loader=(
            (lambda _capability_id, _kind: descriptor)
            if decision_basis == "preapproved-policy" and verify_preapproval
            else None
        ),
        trusted_utc_now=trusted_utc_now,
    )
    engine.process(
        _event(
            "SessionStarted",
            0,
            "event-start",
            payload={"host_level": "managing"},
        )
    )
    plan = engine.process(
        _event(
            "IntentObserved",
            1,
            "event-plan",
            payload={
                "observation_ref": {
                    "provider_id": "host-buffer",
                    "opaque_id": "observation-1",
                    "content_digest": _digest("normalized-work"),
                }
            },
        )
    )
    assert [action.kind for action in plan.actions] == ["PresentBundle"]
    consent = engine.process(
        _event(
            "ReassessmentRequested",
            2,
            "event-desired",
            payload={
                "owner_id": "owner-1",
                "policy_snapshot_digest": policy.policy_digest,
                "desired_capabilities": [_desired_row()],
            },
        )
    )
    request = consent.actions[0]
    assert request.kind == "RequestConsent"
    if decision is not None:
        decision_transition = engine.process(
            _event(
                "UserDecision",
                3,
                "event-granted",
                payload={
                    "consent_id": request.consent_id or "",
                    "decision": decision,
                    "decision_basis": decision_basis,
                    "policy_snapshot_digest": policy.policy_digest,
                    "requested_action_id": request.payload["requested_action_id"],
                    "requested_action_kind": request.payload["requested_action_kind"],
                    "requested_action_content_digest": request.payload[
                        "requested_action_content_digest"
                    ],
                    "requested_action_precondition_revision": request.payload[
                        "requested_action_precondition_revision"
                    ],
                },
            )
        )
        if decision == "granted":
            assert [action.kind for action in decision_transition.actions] == ["InstallCapability"]
        else:
            assert all(action.kind != "InstallCapability" for action in decision_transition.actions)
    return engine, policy


def _pending_consent_expiry_event(engine: CtxEngine, event_id: str) -> EngineEvent:
    snapshot = engine.snapshot(_scope())
    assert snapshot.state is not None
    assert len(snapshot.state.pending_consents) == 1
    pending = snapshot.state.pending_consents[0]
    action = pending.install_action
    return _event(
        "InstallConsentExpired",
        snapshot.state.revision,
        event_id,
        payload={
            "consent_id": pending.consent_id,
            "policy_snapshot_digest": action.payload["policy_snapshot_digest"],
            "requested_action_id": action.action_id,
            "requested_action_kind": action.kind,
            "requested_action_content_digest": action.content_digest,
            "requested_action_precondition_revision": action.precondition_revision,
            "install_expires_at": action.expires_at or "",
        },
    )


def _pending_install(engine: CtxEngine) -> HostAction:
    snapshot = engine.snapshot(_scope())
    assert snapshot.state is not None
    pending = tuple(item for item in snapshot.state.pending_effects if item.effect == "install")
    assert len(pending) == 1
    return pending[0].action


def test_engine_consumes_install_authority_without_returning_execution_material(
    tmp_path: Path,
) -> None:
    engine, policy = _engine(tmp_path)
    action = _pending_install(engine)
    execution_binding = _execution_binding()

    result = engine.authorize_install(  # type: ignore[func-returns-value]
        action,
        _selection(),
        _descriptor(),
        expected_catalog_snapshot_digest=CATALOG_DIGEST,
        expected_policy_digest=policy.policy_digest,
        execution_binding=execution_binding,
    )
    assert result is None

    with pytest.raises(InstallActionAlreadyClaimed):
        engine.authorize_install(
            action,
            _selection(),
            _descriptor(),
            expected_catalog_snapshot_digest=CATALOG_DIGEST,
            expected_policy_digest=policy.policy_digest,
            execution_binding=execution_binding,
        )


def test_exact_agent_like_interactive_decision_is_rejected_without_host_guard(
    tmp_path: Path,
) -> None:
    with pytest.raises(CtxEngineError, match="host-authenticated one-shot guard"):
        _engine(tmp_path, verify_interactive=False)


def test_interactive_guard_reserves_exact_grant_until_after_journal_commit(
    tmp_path: Path,
) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    observed: list[tuple[str, int]] = []
    reservations: list[InteractiveInstallDecisionReservation] = []

    @contextmanager
    def guard(reservation: InteractiveInstallDecisionReservation) -> Iterator[None]:
        reservations.append(reservation)
        observed.append(("reserved", store.load_head(StreamId.from_scope(_scope())).revision))
        try:
            yield
        except BaseException:
            observed.append(("released", store.load_head(StreamId.from_scope(_scope())).revision))
            raise
        else:
            observed.append(("settled", store.load_head(StreamId.from_scope(_scope())).revision))

    _engine(tmp_path, store=store, interactive_guard=guard)

    assert observed == [("reserved", 3), ("settled", 4)]
    assert len(reservations) == 1
    reservation = reservations[0]
    assert reservation.scope == _scope()
    assert reservation.decision == "granted"
    assert reservation.requested_action_kind == "InstallCapability"
    assert reservation.requested_action_precondition_revision == 4
    assert reservation.install_expires_at == "2026-08-01T13:00:00Z"


def test_interactive_denial_is_also_reserved_until_commit(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    observed: list[tuple[str, str, int]] = []

    @contextmanager
    def guard(reservation: InteractiveInstallDecisionReservation) -> Iterator[None]:
        observed.append(
            (
                "reserved",
                reservation.decision,
                store.load_head(StreamId.from_scope(_scope())).revision,
            )
        )
        yield
        observed.append(
            (
                "settled",
                reservation.decision,
                store.load_head(StreamId.from_scope(_scope())).revision,
            )
        )

    _engine(tmp_path, store=store, interactive_guard=guard, decision="denied")

    assert observed == [("reserved", "denied", 3), ("settled", "denied", 4)]


def test_interactive_reservation_releases_without_settlement_when_commit_fails(
    tmp_path: Path,
) -> None:
    class FailingDecisionCommitStore(SQLiteEngineStore):
        def commit(
            self,
            *,
            expected_revision: int,
            record: JournalRecord,
            install_claim_guard: InstallActionClaimGuard | None = None,
            activation_claim_guard: ActivationActionClaimGuard | None = None,
        ) -> CommitResult:
            if expected_revision == 3:
                raise RuntimeError("journal unavailable")
            return super().commit(
                expected_revision=expected_revision,
                record=record,
                install_claim_guard=install_claim_guard,
                activation_claim_guard=activation_claim_guard,
            )

    store = FailingDecisionCommitStore(tmp_path / "engine" / "journal.sqlite3")
    observed: list[str] = []

    @contextmanager
    def guard(_reservation: InteractiveInstallDecisionReservation) -> Iterator[None]:
        observed.append("reserved")
        try:
            yield
        except BaseException:
            observed.append("released")
            raise
        else:
            observed.append("settled")

    with pytest.raises(RuntimeError, match="journal unavailable"):
        _engine(tmp_path, store=store, interactive_guard=guard)

    assert observed == ["reserved", "released"]
    assert store.load_head(StreamId.from_scope(_scope())).revision == 3


def test_backdated_install_grant_is_rejected_by_trusted_clock(tmp_path: Path) -> None:
    with pytest.raises(CtxEngineError, match="expired according to trusted clock"):
        _engine(
            tmp_path,
            trusted_utc_now=lambda: datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
        )


def test_install_consent_expiry_requires_trusted_clock_at_exact_expiry(tmp_path: Path) -> None:
    engine, _ = _engine(
        tmp_path,
        decision=None,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    event = _pending_consent_expiry_event(engine, "event-expire-too-early")

    with pytest.raises(CtxEngineError, match="has not expired according to trusted clock"):
        engine.process(event)

    exact_engine, _ = _engine(
        tmp_path / "exact",
        decision=None,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
    )
    transition = exact_engine.process(
        _pending_consent_expiry_event(exact_engine, "event-expire-exact")
    )
    snapshot = exact_engine.snapshot(_scope())
    assert snapshot.state is not None
    assert snapshot.state.pending_consents == ()
    assert snapshot.state.blocked_install_descriptor_digests == ()
    assert transition.actions == ()


def test_install_consent_expiry_rejects_wrong_scope_and_completed_consent(tmp_path: Path) -> None:
    engine, _ = _engine(
        tmp_path,
        decision=None,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
    )
    event = _pending_consent_expiry_event(engine, "event-expire")
    wrong_scope = event.to_dict()
    wrong_scope["scope"]["exposure_id"] = "other-exposure"

    with pytest.raises(CtxEngineError, match="does not match an exact pending consent"):
        engine.process(EngineEvent.from_dict(wrong_scope))

    engine.process(event)
    repeated = event.to_dict()
    repeated["event_id"] = "event-expire-repeated"
    repeated["expected_revision"] = 4
    with pytest.raises(CtxEngineError, match="does not match an exact pending consent"):
        engine.process(EngineEvent.from_dict(repeated))


def test_interactive_guard_entry_failure_is_redacted(tmp_path: Path) -> None:
    @contextmanager
    def failing_guard(
        _reservation: InteractiveInstallDecisionReservation,
    ) -> Iterator[None]:
        raise RuntimeError("private host authentication detail")
        yield

    with pytest.raises(CtxEngineError) as captured:
        _engine(tmp_path, interactive_guard=failing_guard)

    assert str(captured.value) == "interactive install decision guard is unavailable"
    assert "private" not in str(captured.value)


def test_engine_rechecks_current_policy_before_accepting_automatic_grant(
    tmp_path: Path,
) -> None:
    automatic = InstallConsentPolicy(skill_mode="preapproved-auto")

    engine, _persisted = _engine(
        tmp_path / "authorized",
        policy=automatic,
        decision_basis="preapproved-policy",
    )
    assert _pending_install(engine).kind == "InstallCapability"

    with pytest.raises(CtxEngineError, match="current-policy guard"):
        _engine(
            tmp_path / "missing-authority",
            policy=automatic,
            decision_basis="preapproved-policy",
            verify_preapproval=False,
        )

    risky = _descriptor(credential_requirement=True)
    with pytest.raises(CtxEngineError, match="current policy"):
        _engine(
            tmp_path / "risk-requires-prompt",
            policy=automatic,
            decision_basis="preapproved-policy",
            descriptor=risky,
        )

    with pytest.raises(CtxEngineError, match="policy guard"):
        _engine(
            tmp_path / "policy-revoked-before-grant",
            policy=automatic,
            loaded_policy=InstallConsentPolicy.safe_default(),
            decision_basis="preapproved-policy",
        )

    substituted_material = _material(salt="substituted-current-result")
    with pytest.raises(CtxEngineError, match="current policy"):
        _engine(
            tmp_path / "material-substitution",
            policy=automatic,
            decision_basis="preapproved-policy",
            descriptor=_descriptor(result_material=substituted_material),
        )


def test_preapproved_policy_cannot_be_used_to_deny_without_interactive_authority(
    tmp_path: Path,
) -> None:
    automatic = InstallConsentPolicy(skill_mode="preapproved-auto")

    with pytest.raises(CtxEngineError, match="may only grant"):
        _engine(
            tmp_path,
            policy=automatic,
            decision_basis="preapproved-policy",
            decision="denied",
        )


def test_preapproved_policy_is_reasserted_immediately_before_commit(tmp_path: Path) -> None:
    automatic = InstallConsentPolicy(skill_mode="preapproved-auto")
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    observed_revisions: list[int] = []

    _engine(
        tmp_path,
        policy=automatic,
        decision_basis="preapproved-policy",
        store=store,
        policy_assert_current=lambda: observed_revisions.append(
            store.load_head(StreamId.from_scope(_scope())).revision
        ),
    )

    assert observed_revisions == [3]
    assert store.load_head(StreamId.from_scope(_scope())).revision == 4


def test_install_expiry_is_rechecked_after_policy_reassertion_before_commit(
    tmp_path: Path,
) -> None:
    automatic = InstallConsentPolicy(skill_mode="preapproved-auto")
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    current_time = [BEFORE_EXPIRY]

    with pytest.raises(CtxEngineError, match="expired according to trusted clock"):
        _engine(
            tmp_path,
            policy=automatic,
            decision_basis="preapproved-policy",
            store=store,
            trusted_utc_now=lambda: current_time[0],
            policy_assert_current=lambda: current_time.__setitem__(
                0,
                datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
            ),
        )

    assert store.load_head(StreamId.from_scope(_scope())).revision == 3


def test_install_receipt_activates_then_v3_exposure_authorization_uses_decision_digest(
    tmp_path: Path,
) -> None:
    engine, _policy = _engine(tmp_path)
    install = _pending_install(engine)
    execution_binding = _execution_binding()
    engine.authorize_install(
        install,
        _selection(),
        _descriptor(),
        expected_catalog_snapshot_digest=CATALOG_DIGEST,
        expected_policy_digest=_policy.policy_digest,
        execution_binding=execution_binding,
    )

    receipt = _event(
        "ActionApplied",
        4,
        "event-install-applied",
        payload={
            "action_id": install.action_id,
            "action_kind": install.kind,
            "action_content_digest": install.content_digest,
            "action_precondition_revision": install.precondition_revision,
            "verification": _install_receipt_verification(install),
        },
    )
    with pytest.raises(InstallExecutionOutcomeRequired, match="verified execution outcome"):
        engine.process(receipt)

    outcome_guard = engine._record_install_outcome(  # noqa: SLF001 - coordinator seam.
        install,
        execution_binding=execution_binding,
        execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
            install, execution_binding
        ),
        outcome="applied",
        observed_material_identity_digest=_material().identity_digest,
        verification_digest=_digest("verified-skill-cas-observation"),
    )
    activation_transition = engine.process_install_receipt(receipt, outcome_guard)
    activation = activation_transition.actions[0]
    assert activation.kind == "ActivateCapability"
    engine.authorize_activation(
        activation,
        execution_binding=execution_binding,
        expected_host_descriptor_digest=_digest("host"),
    )
    activation_guard = engine._record_activation_outcome(  # noqa: SLF001
        activation,
        execution_binding=execution_binding,
        execution_authority=engine._issue_activation_outcome_permit(  # noqa: SLF001
            activation,
            execution_binding,
        ),
        observed_material_identity_digest=_material().identity_digest,
        verification_digest=_digest("verified-activation-material-observation"),
    )
    activation_status = engine.activation_execution_status(activation)
    assert activation_status.observed_at is not None
    engine.process_activation_receipt(
        replace(
            _event(
                "ActionApplied",
                5,
                "event-activation-applied",
                payload={
                    "action_id": activation.action_id,
                    "action_kind": activation.kind,
                    "action_content_digest": activation.content_digest,
                    "action_precondition_revision": activation.precondition_revision,
                    "verification": _material_receipt_verification(activation),
                },
            ),
            occurred_at=activation_status.observed_at,
        ),
        activation_guard,
    )
    preparation = engine.process(_event("TurnStarting", 6, "event-turn-starting")).actions[0]
    assert preparation.kind == "PrepareExposure"

    engine.authorize_exposure(
        preparation,
        _selection(),
        expected_catalog_snapshot_digest=CATALOG_DIGEST,
    )
