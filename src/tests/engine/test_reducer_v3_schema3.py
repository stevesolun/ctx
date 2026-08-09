from __future__ import annotations

import hashlib
from collections.abc import Mapping

import pytest

from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planning_v3 import InstallPlanningAuthority, LoadPlanningAuthority
from ctx.engine.protocol import (
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
    EngineEvent,
    HostAction,
    ScopeRef,
)
from ctx.engine.reducer import InvalidEventError, reduce_replay_v3, reduce_replay_v4
from ctx.engine.replay import ReplayInput, StructuredSurrogate
from ctx.engine.state import CapabilityStateV3, CommittedPlanV3, EngineState


NOW = "2026-08-02T12:00:00Z"
POLICY_DIGEST = hashlib.sha256(b"install-policy").hexdigest()
INSTALLABLE_CAPABILITY_IDS = (
    "skill:remote",
    "agent:reviewer",
    "mcp-server:repository-tools",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(*, exposure_id: str = "exposure-1") -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id=exposure_id,
        host_context_id="host-1",
    )


def _event(
    kind: str,
    revision: int,
    event_id: str,
    *,
    payload: Mapping[str, object] | None = None,
    correlation_id: str = "plan-v3",
    scope: ScopeRef | None = None,
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=_scope() if scope is None else scope,
        expected_revision=revision,
        occurred_at=NOW,
        payload={} if payload is None else payload,
        correlation_id=correlation_id,
        engine_version="engine-v3",
        planner_version="planner-v3",
        policy_version="policy-v3",
        host_descriptor_digest=_digest("host"),
        catalog_snapshot_digest=_digest("catalog"),
        semantic_model_digest=_digest("model"),
        semantic_index_digest=_digest("index"),
        work_signature=_digest("work"),
        random_seed=17,
    )


def _replay(
    event: EngineEvent,
    *,
    decision: StructuredSurrogate | None = None,
) -> ReplayInput:
    return ReplayInput(
        source_event_content_digest=event.content_digest,
        reducer_event=event,
        decision_surrogate=decision,
        reducer_version="ctx-reducer-v3",
    )


def _replay_v4(
    event: EngineEvent,
    *,
    decision: StructuredSurrogate | None = None,
) -> ReplayInput:
    return ReplayInput(
        source_event_content_digest=event.content_digest,
        reducer_event=event,
        decision_surrogate=decision,
        reducer_version="ctx-reducer-v4",
    )


def _catalog_identity(
    capability_id: str,
    *,
    namespace: str = "catalog-namespace",
) -> CatalogCapabilityIdentity:
    return CatalogCapabilityIdentity.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        catalog_namespace_digest=_digest(namespace),
    )


def _material(capability_id: str, salt: str) -> MaterialIdentity:
    return MaterialIdentity.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        content_sha256=_digest(f"content:{salt}"),
        content_bytes=32,
    )


def _presentation(capability_id: str, actionability: str) -> dict[str, object]:
    kind, name = capability_id.split(":", 1)
    return {
        "actionability": actionability,
        "capability_id": capability_id,
        "catalog_entry_digest": _digest(f"entry:{capability_id}"),
        "install_descriptor_digest": None,
        "install_plan_digest": None,
        "kind": kind,
        "matching_signals": ["python"],
        "name": name,
        "normalized_score_ppm": 900_000,
        "reason_codes": ["signal-match"],
    }


def _benefit(tier: str = "executable") -> dict[str, object]:
    return {
        "tier": tier,
        "individual_net_benefit_u": 600_000,
        "marginal_net_benefit_u": 600_000,
    }


def _load_row(
    capability_id: str = "skill:local",
    *,
    variant: str = "load",
) -> dict[str, object]:
    identity = _catalog_identity(capability_id, namespace=f"catalog-namespace:{variant}")
    material = _material(capability_id, variant)
    descriptor = MaterialDescriptor.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        actionability="load",
        content_sha256=material.content_sha256,
        content_bytes=material.content_bytes,
        estimated_tokens=8,
        provenance_digest=_digest(f"catalog-snapshot:{variant}"),
        material_identity_digest=material.identity_digest,
    )
    authorized = AuthorizedMaterial.from_catalog(
        catalog_identity_digest=identity.identity_digest,
        descriptor=descriptor,
    )
    return {
        **_presentation(capability_id, "load"),
        "catalog_identity": identity.to_dict(),
        "benefit": _benefit(),
        "authority": {"type": "load", "material": authorized.to_dict()},
    }


def _install_row(capability_id: str = "skill:remote") -> dict[str, object]:
    identity = _catalog_identity(capability_id)
    result_material = _material(capability_id, "install")
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        installer_id="skill-installer",
        plan_digest=_digest(f"plan:{capability_id}"),
        provenance_digest=_digest("install-snapshot"),
        result_material_identity_digest=result_material.identity_digest,
    )
    presentation = _presentation(capability_id, "install")
    presentation["install_descriptor_digest"] = descriptor.descriptor_digest
    presentation["install_plan_digest"] = descriptor.plan_digest
    return {
        **presentation,
        "catalog_identity": identity.to_dict(),
        "benefit": _benefit(),
        "authority": {
            "type": "install",
            "descriptor": descriptor.to_dict(),
            "result_material": result_material.to_dict(),
        },
    }


def _manual_row(capability_id: str = "agent:advisor") -> dict[str, object]:
    return {
        **_presentation(capability_id, "manual"),
        "catalog_identity": _catalog_identity(capability_id).to_dict(),
        "benefit": _benefit("advisory"),
        "authority": {"type": "manual"},
    }


def _audit(
    *,
    candidate_pool_count: int = 3,
    search_evaluation_count: int = 9,
) -> dict[str, object]:
    return {
        "result_schema_id": "ctx.benefit-selection-result-v1",
        "result_digest": _digest("benefit-result"),
        "policy_schema_id": "ctx.net-benefit-policy-v3",
        "policy_digest": _digest("benefit-policy"),
        "selection_algorithm_id": "ctx.greedy-bounded-subset-exchange-v1",
        "calibration_digest": _digest("calibration"),
        "requested_limit": 5,
        "candidate_pool_count": candidate_pool_count,
        "search_evaluation_count": search_evaluation_count,
    }


def _plan() -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": _audit(),
            "capabilities": [_load_row(), _install_row(), _manual_row()],
        },
    )


def _single_install_plan(capability_id: str) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": _audit(
                candidate_pool_count=1,
                search_evaluation_count=1,
            ),
            "capabilities": [_install_row(capability_id)],
        },
    )


def _degraded_plan() -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "degraded",
            "abstention_code": "planner-failed",
            "benefit_audit": None,
            "capabilities": [],
        },
    )


def _actions(transition: object, kind: str) -> tuple[HostAction, ...]:
    actions = getattr(transition, "actions")
    return tuple(action for action in actions if action.kind == kind)


def _started_and_planned():
    state, _ = reduce_replay_v3(
        None,
        _replay(_event("SessionStarted", 0, "start", payload={"host_level": "managing"})),
    )
    return reduce_replay_v3(
        state,
        _replay(_event("IntentObserved", 1, "plan"), decision=_plan()),
    )


def _desired_row(row: Mapping[str, object], lease_id: str) -> dict[str, object]:
    return {
        "capability_id": row["capability_id"],
        "source_digest": row["catalog_entry_digest"],
        "kind": row["kind"],
        "actionability": row["actionability"],
        "install_descriptor_digest": row["install_descriptor_digest"],
        "install_plan_digest": row["install_plan_digest"],
        "lease_id": lease_id,
    }


def _install_requested():
    state, _ = _started_and_planned()
    event = _event(
        "ReassessmentRequested",
        state.revision,
        "desired",
        payload={
            "owner_id": "owner-1",
            "policy_snapshot_digest": POLICY_DIGEST,
            "desired_capabilities": [
                _desired_row(_load_row(), "lease-load"),
                _desired_row(_install_row(), "lease-install"),
            ],
        },
    )
    return reduce_replay_v3(state, _replay(event))


def _single_install_requested(capability_id: str):
    state, _ = reduce_replay_v3(
        None,
        _replay(_event("SessionStarted", 0, "start", payload={"host_level": "managing"})),
    )
    state, _ = reduce_replay_v3(
        state,
        _replay(
            _event("IntentObserved", 1, "plan"),
            decision=_single_install_plan(capability_id),
        ),
    )
    event = _event(
        "ReassessmentRequested",
        state.revision,
        "desired",
        payload={
            "owner_id": "owner-1",
            "policy_snapshot_digest": POLICY_DIGEST,
            "desired_capabilities": [
                _desired_row(_install_row(capability_id), "lease-install"),
            ],
        },
    )
    return reduce_replay_v3(state, _replay(event))


@pytest.mark.parametrize(
    "event_kind",
    ["ProviderSubmissionObserved", "ToolCallObserved", "TurnStarting"],
)
def test_schema3_actionless_revision_reoffers_consumed_install_consent(
    event_kind: str,
) -> None:
    capability_id = "skill:remote"
    state, requested = _single_install_requested(capability_id)
    original = _actions(requested, "RequestConsent")[0]
    payload: Mapping[str, object]
    if event_kind == "ProviderSubmissionObserved":
        payload = {"capabilities": []}
    elif event_kind == "ToolCallObserved":
        payload = {
            "capability_id": capability_id,
            "source_digest": _install_row(capability_id)["catalog_entry_digest"],
            "outcome": "failed",
        }
    else:
        payload = {}

    state, transition = reduce_replay_v3(
        state,
        _replay(
            _event(
                event_kind,
                state.revision,
                f"consume-consent-{event_kind}",
                payload=payload,
            )
        ),
    )

    refreshed = _actions(transition, "RequestConsent")
    assert len(refreshed) == 1
    assert refreshed[0].action_id != original.action_id
    assert refreshed[0].entity_id == capability_id
    assert len(state.pending_consents) == 1
    assert state.pending_consents[0].consent_id == refreshed[0].consent_id
    assert (
        state.pending_consents[0].install_action.action_id
        == refreshed[0].payload["requested_action_id"]
    )


def _decision_for(request: HostAction) -> EngineEvent:
    return _event(
        "UserDecision",
        request.precondition_revision,
        "grant",
        payload={
            "consent_id": request.consent_id or "",
            "decision": "granted",
            "decision_basis": "interactive",
            "policy_snapshot_digest": POLICY_DIGEST,
            "requested_action_id": request.payload["requested_action_id"],
            "requested_action_kind": request.payload["requested_action_kind"],
            "requested_action_content_digest": request.payload["requested_action_content_digest"],
            "requested_action_precondition_revision": request.payload[
                "requested_action_precondition_revision"
            ],
        },
    )


def _receipt_event(state, action: HostAction, verification: Mapping[str, object]) -> EngineEvent:
    return _event(
        "ActionApplied",
        state.revision,
        f"receipt-{action.kind}",
        scope=action.scope,
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "verification": verification,
        },
    )


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


def _install_receipt_verification(
    capability: CapabilityStateV3,
    action: HostAction,
) -> dict[str, object]:
    authority = capability.selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    return {
        "schema": INSTALL_RECEIPT_SCHEMA_V3,
        "host_state": "installed",
        "capability_id": capability.capability_id,
        "capability_kind": capability.kind,
        "catalog_identity": capability.catalog_identity.to_dict(),
        "material_identity": capability.material_identity.to_dict(),
        "install_plan_descriptor": authority.descriptor.to_dict(),
        "installer_digest": action.payload["installer_digest"],
        "policy_snapshot_digest": POLICY_DIGEST,
    }


def _prompt_context_receipt_verification(action: HostAction) -> dict[str, object]:
    rows = action.payload["capabilities"]
    assert isinstance(rows, tuple)
    context = b"bounded prepared prompt context"
    return {
        "schema": PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
        "host_state": "prompt-context-prepared",
        "prompt_context_sha256": hashlib.sha256(context).hexdigest(),
        "prompt_context_bytes": len(context),
        "capabilities": [
            {
                "capability_id": row["capability_id"],
                "content_sha256": row["material_identity"]["content_sha256"],
                "content_bytes": row["material_identity"]["content_bytes"],
            }
            for row in rows
        ],
    }


def _active_installed_remote_state(
    capability_id: str = "skill:remote",
) -> EngineState:
    state, requested = _single_install_requested(capability_id)
    request = _actions(requested, "RequestConsent")[0]
    state, granted = reduce_replay_v3(state, _replay(_decision_for(request)))
    install = _actions(granted, "InstallCapability")[0]
    capability = state.capability(capability_id)
    assert isinstance(capability, CapabilityStateV3)
    state, activation_requested = reduce_replay_v3(
        state,
        _replay(
            _receipt_event(
                state,
                install,
                _install_receipt_verification(capability, install),
            )
        ),
    )
    activate = _actions(activation_requested, "ActivateCapability")[0]
    state, _ = reduce_replay_v3(
        state,
        _replay(
            _receipt_event(
                state,
                activate,
                _material_receipt_verification(activate),
            )
        ),
    )
    return state


def test_schema3_prepares_one_prompt_context_without_faking_activation() -> None:
    state, _ = reduce_replay_v4(
        None,
        _replay_v4(
            _event(
                "SessionStarted",
                0,
                "prompt-start",
                payload={"host_level": "prompt-context-activate"},
            )
        ),
    )
    state, requested = reduce_replay_v4(
        state,
        _replay_v4(_event("IntentObserved", 1, "prompt-plan"), decision=_plan()),
    )
    capability = state.capability("skill:local")
    assert isinstance(capability, CapabilityStateV3)
    assert capability.activation == "inactive"
    assert capability.leases == ()
    plan = state.committed_plan
    assert isinstance(plan, CommittedPlanV3)

    actions = _actions(requested, "PreparePromptContext")
    assert len(actions) == 1
    action = actions[0]
    assert tuple(item.kind for item in requested.actions) == (
        "PresentBundle",
        "PreparePromptContext",
    )
    assert action.required_host_feature == "prompt-context"
    assert action.entity_id is None
    assert tuple(row["capability_id"] for row in action.payload["capabilities"]) == ("skill:local",)
    assert action.lease_id is not None
    assert tuple(item.effect for item in state.pending_effects) == ("prompt-context",)
    still_inactive = state.capability("skill:local")
    assert isinstance(still_inactive, CapabilityStateV3)
    assert still_inactive.activation == "inactive"
    assert still_inactive.leases == ()

    state, completed = reduce_replay_v4(
        state,
        _replay_v4(
            _receipt_event(
                state,
                action,
                _prompt_context_receipt_verification(action),
            )
        ),
    )

    assert completed.actions == ()
    assert state.pending_effects == ()
    assert state.evidence_for(action.scope.exposure_id, "skill:local").exposure == "prepared"
    completed_capability = state.capability("skill:local")
    assert isinstance(completed_capability, CapabilityStateV3)
    assert completed_capability.activation == "inactive"
    assert completed_capability.leases == ()


def test_schema3_prompt_context_is_absent_without_explicit_activating_host() -> None:
    state, _ = reduce_replay_v4(
        None,
        _replay_v4(
            _event("SessionStarted", 0, "query-start", payload={"host_level": "query-only"})
        ),
    )
    state, planned = reduce_replay_v4(
        state,
        _replay_v4(_event("IntentObserved", 1, "query-plan"), decision=_plan()),
    )

    assert _actions(planned, "PreparePromptContext") == ()
    assert state.pending_effects == ()


def _promoted_load_row(
    capability: CapabilityStateV3,
    material: AuthorizedMaterial,
) -> dict[str, object]:
    presentation = _presentation(capability.capability_id, "load")
    presentation["catalog_entry_digest"] = capability.source_digest
    return {
        **presentation,
        "catalog_identity": capability.catalog_identity.to_dict(),
        "benefit": _benefit(),
        "authority": {"type": "load", "material": material.to_dict()},
    }


def _catalog_material_for_installed(
    capability: CapabilityStateV3,
) -> AuthorizedMaterial:
    material = capability.material_identity
    descriptor = MaterialDescriptor.create(
        capability_id=capability.capability_id,
        kind=capability.kind,
        actionability="load",
        content_sha256=material.content_sha256,
        content_bytes=material.content_bytes,
        estimated_tokens=8,
        provenance_digest=_digest("refreshed-catalog-snapshot"),
        material_identity_digest=material.identity_digest,
    )
    return AuthorizedMaterial.from_catalog(
        catalog_identity_digest=capability.catalog_identity.identity_digest,
        descriptor=descriptor,
    )


def test_schema3_plan_persists_exact_audit_and_authority_without_manual_lifecycle() -> None:
    state, transition = _started_and_planned()

    assert isinstance(state.committed_plan, CommittedPlanV3)
    assert state.committed_plan.benefit_audit is not None
    assert state.committed_plan.benefit_audit.to_mapping() == _audit()
    assert [item.to_dict() for item in state.committed_plan.capabilities] == [
        _load_row(),
        _install_row(),
        _manual_row(),
    ]
    assert state.last_manual_bundle == ("agent:advisor",)
    assert {item.capability_id for item in state.capabilities} == {"skill:local", "skill:remote"}
    load = state.capability("skill:local")
    install = state.capability("skill:remote")
    assert isinstance(load, CapabilityStateV3)
    assert isinstance(load.selection.authority, LoadPlanningAuthority)
    assert load.installation == "installed"
    assert load.current_authorized_material == load.selection.authority.material
    assert isinstance(install, CapabilityStateV3)
    assert isinstance(install.selection.authority, InstallPlanningAuthority)
    assert install.installation == "absent"
    assert install.current_authorized_material is None
    assert install.material_identity == install.selection.authority.result_material
    bundle = _actions(transition, "PresentBundle")[0]
    assert [item["capability_id"] for item in bundle.payload["capabilities"]] == [
        "skill:local",
        "skill:remote",
        "agent:advisor",
    ]


@pytest.mark.parametrize("capability_id", INSTALLABLE_CAPABILITY_IDS)
def test_schema3_install_receipt_promotes_exact_lineage_then_reconciles_activation(
    capability_id: str,
) -> None:
    state, requested = _single_install_requested(capability_id)
    request = _actions(requested, "RequestConsent")[0]
    assert request.payload["schema"] == "ctx.install-consent-request-v3"

    state, granted = reduce_replay_v3(state, _replay(_decision_for(request)))
    install = _actions(granted, "InstallCapability")[0]
    assert install.verification["receipt_schema"] == INSTALL_RECEIPT_SCHEMA_V3
    capability = state.capability(capability_id)
    assert isinstance(capability, CapabilityStateV3)
    authority = capability.selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    verification = {
        "schema": INSTALL_RECEIPT_SCHEMA_V3,
        "host_state": "installed",
        "capability_id": capability.capability_id,
        "capability_kind": capability.kind,
        "catalog_identity": capability.catalog_identity.to_dict(),
        "material_identity": capability.material_identity.to_dict(),
        "install_plan_descriptor": authority.descriptor.to_dict(),
        "installer_digest": install.payload["installer_digest"],
        "policy_snapshot_digest": POLICY_DIGEST,
    }
    receipt = _receipt_event(state, install, verification)

    state, transition = reduce_replay_v3(state, _replay(receipt))

    capability = state.capability(capability_id)
    assert isinstance(capability, CapabilityStateV3)
    assert capability.installation == "installed"
    assert capability.current_authorized_material is not None
    lineage = capability.current_authorized_material.installed_material_lineage
    assert lineage is not None
    assert lineage.install_action_content_digest == install.content_digest
    assert lineage.install_receipt_content_digest == receipt.content_digest
    activate = next(
        action
        for action in _actions(transition, "ActivateCapability")
        if action.entity_id == capability_id
    )
    assert (
        activate.payload["authorized_material"] == capability.current_authorized_material.to_dict()
    )
    assert activate.verification["receipt_schema"] == MATERIAL_RECEIPT_SCHEMA_V3


@pytest.mark.parametrize(
    ("capability_id", "substituted_capability_id"),
    [
        ("skill:remote", "agent:foreign"),
        ("agent:reviewer", "mcp-server:foreign"),
        ("mcp-server:repository-tools", "skill:foreign"),
    ],
)
def test_schema3_receipt_rejects_typed_substitution_before_promotion(
    capability_id: str,
    substituted_capability_id: str,
) -> None:
    state, requested = _single_install_requested(capability_id)
    request = _actions(requested, "RequestConsent")[0]
    state, granted = reduce_replay_v3(state, _replay(_decision_for(request)))
    install = _actions(granted, "InstallCapability")[0]
    capability = state.capability(capability_id)
    assert isinstance(capability, CapabilityStateV3)
    authority = capability.selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    wrong_identity = _catalog_identity(substituted_capability_id)
    verification = {
        "schema": INSTALL_RECEIPT_SCHEMA_V3,
        "host_state": "installed",
        "capability_id": capability.capability_id,
        "capability_kind": capability.kind,
        "catalog_identity": wrong_identity.to_dict(),
        "material_identity": capability.material_identity.to_dict(),
        "install_plan_descriptor": authority.descriptor.to_dict(),
        "installer_digest": install.payload["installer_digest"],
        "policy_snapshot_digest": POLICY_DIGEST,
    }

    with pytest.raises((InvalidEventError, ValueError)):
        receipt = _receipt_event(state, install, verification)
        reduce_replay_v3(state, _replay(receipt))
    unchanged = state.capability(capability_id)
    assert isinstance(unchanged, CapabilityStateV3)
    assert unchanged.installation == "absent"
    assert unchanged.current_authorized_material is None


def test_schema3_degraded_plan_preserves_exact_runtime_authority_and_leases() -> None:
    state, _ = _install_requested()
    runtime_before = (
        state.capabilities,
        state.pending_effects,
        state.pending_consents,
        state.evidence,
        state.blocked_capability_ids,
        state.blocked_deactivation_ids,
        state.blocked_install_descriptor_digests,
        state.install_policy_snapshot_digest,
        state.rollback_requested_capability_ids,
        state.terminal_cleanup_notified_ids,
    )

    state, transition = reduce_replay_v3(
        state,
        _replay(
            _event(
                "IntentObserved",
                state.revision,
                "degraded",
                correlation_id="plan-degraded-v3",
            ),
            decision=_degraded_plan(),
        ),
    )

    assert isinstance(state.committed_plan, CommittedPlanV3)
    assert state.committed_plan.status == "degraded"
    assert state.last_manual_bundle == ()
    assert not transition.actions
    assert {item["code"] for item in transition.diagnostics} == {"planner-failed"}
    assert (
        state.capabilities,
        state.pending_effects,
        state.pending_consents,
        state.evidence,
        state.blocked_capability_ids,
        state.blocked_deactivation_ids,
        state.blocked_install_descriptor_digests,
        state.install_policy_snapshot_digest,
        state.rollback_requested_capability_ids,
        state.terminal_cleanup_notified_ids,
    ) == runtime_before


def test_schema3_rejects_same_id_authority_change_while_old_material_is_active() -> None:
    state, _ = _started_and_planned()
    state, requested = reduce_replay_v3(
        state,
        _replay(
            _event(
                "ReassessmentRequested",
                state.revision,
                "activate-old",
                payload={
                    "owner_id": "owner-old",
                    "policy_snapshot_digest": POLICY_DIGEST,
                    "desired_capabilities": [_desired_row(_load_row(), "lease-old-material")],
                },
            )
        ),
    )
    activate = _actions(requested, "ActivateCapability")[0]
    state, _ = reduce_replay_v3(
        state,
        _replay(
            _receipt_event(
                state,
                activate,
                _material_receipt_verification(activate),
            )
        ),
    )
    original = state.capability("skill:local")
    assert isinstance(original, CapabilityStateV3)
    assert original.activation == "active"
    original_authority = original.current_authorized_material

    changed = _load_row(variant="changed-material")
    changed["catalog_entry_digest"] = _digest("entry:skill:local:changed")
    changed_plan = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": _audit(
                candidate_pool_count=1,
                search_evaluation_count=1,
            ),
            "capabilities": [changed],
        },
    )

    with pytest.raises(InvalidEventError, match="prior same-ID runtime authority"):
        reduce_replay_v3(
            state,
            _replay(
                _event(
                    "IntentObserved",
                    state.revision,
                    "changed-same-id",
                    correlation_id="plan-changed-same-id",
                ),
                decision=changed_plan,
            ),
        )

    still_active = state.capability("skill:local")
    assert isinstance(still_active, CapabilityStateV3)
    assert still_active.activation == "active"
    assert still_active.current_authorized_material == original_authority


@pytest.mark.parametrize("capability_id", INSTALLABLE_CAPABILITY_IDS)
def test_schema3_promotes_exact_installed_material_to_load_without_runtime_churn(
    capability_id: str,
) -> None:
    state = _active_installed_remote_state(capability_id)
    state, _ = reduce_replay_v3(
        state,
        _replay(
            _event(
                "ToolCallObserved",
                state.revision,
                "observe-installed-remote",
                payload={
                    "capability_id": capability_id,
                    "source_digest": _install_row(capability_id)["catalog_entry_digest"],
                    "outcome": "succeeded",
                },
            )
        ),
    )
    before = state.capability(capability_id)
    assert isinstance(before, CapabilityStateV3)
    assert before.activation == "active"
    assert before.current_authorized_material is not None
    assert before.installed_lineage is not None
    evidence_before = state.evidence
    refreshed_catalog_material = _catalog_material_for_installed(before)
    promoted_row = _promoted_load_row(before, refreshed_catalog_material)
    promoted_plan = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": _audit(),
            "capabilities": [promoted_row],
        },
    )

    state, _ = reduce_replay_v3(
        state,
        _replay(
            _event(
                "IntentObserved",
                state.revision,
                "promote-installed-remote",
                correlation_id="plan-promote-installed-remote",
            ),
            decision=promoted_plan,
        ),
    )

    promoted = state.capability(capability_id)
    assert isinstance(promoted, CapabilityStateV3)
    assert isinstance(promoted.selection.authority, LoadPlanningAuthority)
    assert promoted.activation == before.activation
    assert promoted.activation_lease_id == before.activation_lease_id
    assert promoted.leases == before.leases
    assert promoted.material_identity == before.material_identity
    assert promoted.current_authorized_material == refreshed_catalog_material
    assert promoted.source_digest == before.source_digest
    assert state.evidence == evidence_before
    assert EngineState.from_json(state.to_json()) == state


def test_schema3_rejects_install_to_load_promotion_with_changed_material() -> None:
    state = _active_installed_remote_state()
    current = state.capability("skill:remote")
    assert isinstance(current, CapabilityStateV3)
    assert current.current_authorized_material is not None
    changed_identity = _material("skill:remote", "changed-after-install")
    changed_descriptor = MaterialDescriptor.create(
        capability_id="skill:remote",
        kind="skill",
        actionability="load",
        content_sha256=changed_identity.content_sha256,
        content_bytes=changed_identity.content_bytes,
        estimated_tokens=8,
        provenance_digest=_digest("changed-after-install-snapshot"),
        material_identity_digest=changed_identity.identity_digest,
    )
    changed_material = AuthorizedMaterial.from_catalog(
        catalog_identity_digest=current.catalog_identity.identity_digest,
        descriptor=changed_descriptor,
    )
    changed_plan = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": _audit(),
            "capabilities": [_promoted_load_row(current, changed_material)],
        },
    )

    with pytest.raises(InvalidEventError, match="prior same-ID runtime authority"):
        reduce_replay_v3(
            state,
            _replay(
                _event(
                    "IntentObserved",
                    state.revision,
                    "reject-changed-promotion-material",
                    correlation_id="plan-reject-changed-promotion-material",
                ),
                decision=changed_plan,
            ),
        )

    unchanged = state.capability("skill:remote")
    assert isinstance(unchanged, CapabilityStateV3)
    assert isinstance(unchanged.selection.authority, InstallPlanningAuthority)
    assert unchanged.activation == "active"
    assert EngineState.from_json(state.to_json()) == state


def test_schema3_defers_overlapping_exposure_prepare_until_prior_receipt() -> None:
    state, _ = _started_and_planned()
    state, requested = reduce_replay_v3(
        state,
        _replay(
            _event(
                "ReassessmentRequested",
                state.revision,
                "activate-parent",
                payload={
                    "owner_id": "owner-parent",
                    "policy_snapshot_digest": POLICY_DIGEST,
                    "desired_capabilities": [_desired_row(_load_row(), "lease-parent")],
                },
            )
        ),
    )
    activate = _actions(requested, "ActivateCapability")[0]
    state, _ = reduce_replay_v3(
        state,
        _replay(
            _receipt_event(
                state,
                activate,
                _material_receipt_verification(activate),
            )
        ),
    )
    child_scope = _scope(exposure_id="exposure-child")
    state, _ = reduce_replay_v3(
        state,
        _replay(
            _event(
                "ReassessmentRequested",
                state.revision,
                "lease-child",
                scope=child_scope,
                payload={
                    "owner_id": "owner-child",
                    "policy_snapshot_digest": POLICY_DIGEST,
                    "desired_capabilities": [_desired_row(_load_row(), "lease-child")],
                },
            )
        ),
    )

    state, parent_prepare = reduce_replay_v3(
        state,
        _replay(_event("TurnStarting", state.revision, "prepare-parent")),
    )
    prepare = _actions(parent_prepare, "PrepareExposure")[0]
    state, child_deferred = reduce_replay_v3(
        state,
        _replay(
            _event(
                "TurnStarting",
                state.revision,
                "prepare-child",
                scope=child_scope,
            )
        ),
    )

    assert not _actions(child_deferred, "PrepareExposure")
    assert len(state.pending_effects) == 1
    assert state.pending_effects[0].action == prepare


def test_schema3_exact_deactivation_receipt_prunes_retired_row_before_persistence() -> None:
    state, _ = _started_and_planned()
    state, requested = reduce_replay_v3(
        state,
        _replay(
            _event(
                "ReassessmentRequested",
                state.revision,
                "activate-before-retirement",
                payload={
                    "owner_id": "owner-retirement",
                    "policy_snapshot_digest": POLICY_DIGEST,
                    "desired_capabilities": [_desired_row(_load_row(), "lease-retirement")],
                },
            )
        ),
    )
    activate = _actions(requested, "ActivateCapability")[0]
    state, _ = reduce_replay_v3(
        state,
        _replay(
            _receipt_event(
                state,
                activate,
                _material_receipt_verification(activate),
            )
        ),
    )
    retirement_plan = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": _audit(),
            "capabilities": [_manual_row()],
        },
    )
    state, cooling = reduce_replay_v3(
        state,
        _replay(
            _event(
                "IntentObserved",
                state.revision,
                "retire-load",
                correlation_id="plan-retire-load",
            ),
            decision=retirement_plan,
        ),
    )
    deactivate = _actions(cooling, "DeactivateCapability")[0]

    state, _ = reduce_replay_v3(
        state,
        _replay(
            _receipt_event(
                state,
                deactivate,
                _material_receipt_verification(deactivate),
            )
        ),
    )

    assert state.capability("skill:local") is None
    assert EngineState.from_json(state.to_json()) == state
