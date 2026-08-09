from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import cast

import pytest

from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.protocol import (
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    EngineEvent,
    HostAction,
    ProtocolValidationError,
    ScopeRef,
)
from ctx.engine.reducer import (
    INSTALLATION_REDUCER_VERSION,
    InvalidEventError,
    RevisionConflictError,
    reduce,
    reduce_replay_v1,
    reduce_replay_v2,
    reduce_replay_v3,
)
from ctx.engine.replay import ReplayInput, StructuredSurrogate
from ctx.engine.state import EngineState, StateValidationError


NOW = "2026-08-01T12:00:00Z"


def _scope(
    *,
    exposure_id: str = "exposure-parent",
    parent_exposure_id: str | None = None,
) -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id=exposure_id,
        parent_exposure_id=parent_exposure_id,
        host_context_id="host-1",
    )


def _event(
    kind: str,
    revision: int,
    *,
    event_id: str,
    payload: dict[str, object] | None = None,
    scope: ScopeRef | None = None,
    replay_metadata: bool = True,
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=scope or _scope(),
        expected_revision=revision,
        occurred_at=NOW,
        payload=payload or {},
        correlation_id="plan-1",
        engine_version="engine-v1" if replay_metadata else None,
        planner_version="planner-v1" if replay_metadata else None,
        policy_version="policy-v1" if replay_metadata else None,
        host_descriptor_digest="host-descriptor-v1" if replay_metadata else None,
        catalog_snapshot_digest="catalog-snapshot-1" if replay_metadata else None,
        semantic_model_digest="semantic-model-v1" if replay_metadata else None,
        semantic_index_digest="semantic-index-v1" if replay_metadata else None,
        work_signature="work-signature-v1" if replay_metadata else None,
        random_seed=17 if replay_metadata else None,
    )


def _start(*, host_level: str = "activating"):
    return reduce(
        None,
        _event(
            "SessionStarted",
            0,
            event_id="event-start",
            payload={"host_level": host_level},
        ),
    )


def _desired(
    state,
    capabilities: Iterable[tuple[str, str, str]],
    *,
    owner_id: str = "owner-parent",
    event_id: str = "event-desired",
    scope: ScopeRef | None = None,
):
    payload: dict[str, object] = {
        "owner_id": owner_id,
        "desired_capabilities": [
            {
                "capability_id": capability_id,
                "source_digest": source_digest,
                "lease_id": lease_id,
            }
            for capability_id, source_digest, lease_id in capabilities
        ],
    }
    return reduce(
        state,
        _event(
            "ReassessmentRequested",
            state.revision,
            event_id=event_id,
            payload=payload,
            scope=scope,
        ),
    )


def _receipt(
    state,
    action: HostAction,
    *,
    applied: bool,
    event_id: str,
    host_state: str | None = None,
    replay_metadata: bool = True,
):
    kind = "ActionApplied" if applied else "ActionFailed"
    payload: dict[str, object] = {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
    }
    if applied:
        payload["verification"] = {
            "host_state": (
                host_state if host_state is not None else action.verification["expected_state"]
            )
        }
    else:
        payload["error"] = {"code": "host_failure"}
    return reduce(
        state,
        _event(
            kind,
            state.revision,
            event_id=event_id,
            payload=payload,
            scope=action.scope,
            replay_metadata=replay_metadata,
        ),
    )


def _actions(transition, kind: str) -> tuple[HostAction, ...]:
    return tuple(action for action in transition.actions if action.kind == kind)


def _v3_applied_verification(action: HostAction) -> dict[str, object]:
    if action.kind == "InstallCapability":
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
    return {
        "schema": MATERIAL_RECEIPT_SCHEMA_V3,
        "host_state": action.verification["expected_state"],
        "capability_id": action.entity_id,
        "capability_kind": action.payload["capability_kind"],
        "catalog_identity": action.payload["catalog_identity"],
        "material_identity": action.payload["material_identity"],
        "authorized_material": action.payload["authorized_material"],
    }


def _install_descriptor_payload(action: HostAction) -> Mapping[str, object]:
    value = action.payload["install_plan_descriptor"]
    assert isinstance(value, Mapping)
    return value


def _desired_rows(payload: Mapping[str, object]) -> list[dict[str, object]]:
    value = payload["desired_capabilities"]
    assert isinstance(value, list)
    assert all(isinstance(item, dict) for item in value)
    return cast(list[dict[str, object]], value)


def _v3_event(
    kind: str,
    revision: int,
    event_id: str,
    *,
    payload: dict[str, object] | None = None,
    correlation_id: str = "plan-install-1",
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=_scope(),
        expected_revision=revision,
        occurred_at=NOW,
        payload=payload or {},
        correlation_id=correlation_id,
        engine_version="engine-v3",
        planner_version="planner-v2",
        policy_version="policy-v3",
        host_descriptor_digest="1" * 64,
        catalog_snapshot_digest="2" * 64,
        semantic_model_digest="3" * 64,
        semantic_index_digest="4" * 64,
        work_signature="5" * 64,
        random_seed=23,
    )


def _v3_replay(
    event: EngineEvent,
    *,
    decision: StructuredSurrogate | None = None,
) -> ReplayInput:
    return ReplayInput(
        source_event_content_digest=hashlib.sha256(event.to_json().encode()).hexdigest(),
        reducer_event=event,
        decision_surrogate=decision,
        reducer_version=INSTALLATION_REDUCER_VERSION,
    )


def _catalog_identity(capability_id: str) -> CatalogCapabilityIdentity:
    return CatalogCapabilityIdentity.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        catalog_namespace_digest="f" * 64,
    )


def _material_identity(capability_id: str, salt: str) -> MaterialIdentity:
    return MaterialIdentity.create(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        content_sha256=hashlib.sha256(salt.encode()).hexdigest(),
        content_bytes=32,
    )


def _v3_row(
    capability_id: str,
    actionability: str,
    source_digest: str,
    *,
    score: int = 900_000,
    install_plan_digest: str | None = None,
) -> dict[str, object]:
    kind, name = capability_id.split(":", 1)
    identity = _catalog_identity(capability_id)
    row: dict[str, object] = {
        "capability_id": capability_id,
        "kind": kind,
        "name": name,
        "catalog_entry_digest": source_digest,
        "normalized_score_ppm": score,
        "matching_signals": ["python", "testing"],
        "reason_codes": ["exact-tag-match"],
        "actionability": actionability,
        "install_descriptor_digest": None,
        "install_plan_digest": None,
        "catalog_identity": identity.to_dict(),
        "benefit": {
            "tier": "advisory" if actionability == "manual" else "executable",
            "individual_net_benefit_u": 600_000,
            "marginal_net_benefit_u": 600_000,
        },
    }
    if actionability == "manual":
        row["authority"] = {"type": "manual"}
    elif actionability == "load":
        material = _material_identity(capability_id, f"load:{capability_id}")
        load_descriptor = MaterialDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            actionability="load",
            content_sha256=material.content_sha256,
            content_bytes=material.content_bytes,
            estimated_tokens=8,
            provenance_digest="f" * 64,
            material_identity_digest=material.identity_digest,
        )
        row["authority"] = {
            "type": "load",
            "material": AuthorizedMaterial.from_catalog(
                catalog_identity_digest=identity.identity_digest,
                descriptor=load_descriptor,
            ).to_dict(),
        }
    else:
        result = _material_identity(capability_id, f"install:{capability_id}")
        install_descriptor = InstallPlanDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            installer_id="skill-installer",
            plan_digest=install_plan_digest or hashlib.sha256(capability_id.encode()).hexdigest(),
            provenance_digest="f" * 64,
            result_material_identity_digest=result.identity_digest,
        )
        row["install_descriptor_digest"] = install_descriptor.descriptor_digest
        row["install_plan_digest"] = install_descriptor.plan_digest
        row["authority"] = {
            "type": "install",
            "descriptor": install_descriptor.to_dict(),
            "result_material": result.to_dict(),
        }
    return row


def _v3_plan(rows: list[dict[str, object]]) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": {
                "result_schema_id": "ctx.benefit-selection-result-v1",
                "result_digest": hashlib.sha256(b"result").hexdigest(),
                "policy_schema_id": "ctx.net-benefit-policy-v3",
                "policy_digest": hashlib.sha256(b"benefit-policy").hexdigest(),
                "selection_algorithm_id": "ctx.greedy-bounded-subset-exchange-v1",
                "calibration_digest": hashlib.sha256(b"calibration").hexdigest(),
                "requested_limit": 5,
                "candidate_pool_count": len(rows),
                "search_evaluation_count": max(1, len(rows)),
            },
            "capabilities": rows,
        },
    )


def _install_plan() -> StructuredSurrogate:
    return _v3_plan(
        [_v3_row("skill:remote-testing", "install", "6" * 64, install_plan_digest="7" * 64)]
    )


def _two_install_plan() -> StructuredSurrogate:
    return _v3_plan(
        [
            _v3_row("skill:remote-alpha", "install", "a" * 64, install_plan_digest="c" * 64),
            _v3_row(
                "skill:remote-beta",
                "install",
                "d" * 64,
                score=800_000,
                install_plan_digest="c" * 64,
            ),
        ]
    )


def _manual_plan() -> StructuredSurrogate:
    return _v3_plan([_v3_row("skill:manual-advice", "manual", "b" * 64)])


def _install_desired(policy_snapshot_digest: str = "8" * 64) -> dict[str, object]:
    row = _v3_row("skill:remote-testing", "install", "6" * 64, install_plan_digest="7" * 64)
    return {
        "owner_id": "owner-install",
        "policy_snapshot_digest": policy_snapshot_digest,
        "desired_capabilities": [
            {
                "capability_id": "skill:remote-testing",
                "source_digest": "6" * 64,
                "lease_id": "lease-install",
                "kind": "skill",
                "actionability": "install",
                "install_descriptor_digest": row["install_descriptor_digest"],
                "install_plan_digest": row["install_plan_digest"],
            }
        ],
    }


def _two_install_desired() -> dict[str, object]:
    alpha = _v3_row("skill:remote-alpha", "install", "a" * 64, install_plan_digest="c" * 64)
    beta = _v3_row("skill:remote-beta", "install", "d" * 64, install_plan_digest="c" * 64)
    return {
        "owner_id": "owner-install",
        "policy_snapshot_digest": "8" * 64,
        "desired_capabilities": [
            {
                "capability_id": "skill:remote-alpha",
                "source_digest": "a" * 64,
                "lease_id": "lease-alpha",
                "kind": "skill",
                "actionability": "install",
                "install_descriptor_digest": alpha["install_descriptor_digest"],
                "install_plan_digest": "c" * 64,
            },
            {
                "capability_id": "skill:remote-beta",
                "source_digest": "d" * 64,
                "lease_id": "lease-beta",
                "kind": "skill",
                "actionability": "install",
                "install_descriptor_digest": beta["install_descriptor_digest"],
                "install_plan_digest": "c" * 64,
            },
        ],
    }


def _manual_desired() -> dict[str, object]:
    return {
        "owner_id": "owner-manual",
        "policy_snapshot_digest": "8" * 64,
        "desired_capabilities": [
            {
                "capability_id": "skill:manual-advice",
                "source_digest": "b" * 64,
                "lease_id": "lease-manual",
                "kind": "skill",
                "actionability": "manual",
                "install_descriptor_digest": None,
                "install_plan_digest": None,
            }
        ],
    }


def _retention_plan(*, include_active: bool, include_manual: bool) -> StructuredSurrogate:
    capabilities: list[dict[str, object]] = []
    if include_active:
        capabilities.append(_v3_row("skill:active-testing", "load", "9" * 64, score=800_000))
    if include_manual:
        capabilities.append(_v3_row("skill:manual-advice", "manual", "b" * 64, score=700_000))
    return _v3_plan(capabilities)


def _retention_desired(*, include_active: bool, include_manual: bool) -> dict[str, object]:
    capabilities: list[dict[str, object]] = []
    if include_active:
        capabilities.append(
            {
                "capability_id": "skill:active-testing",
                "source_digest": "9" * 64,
                "lease_id": "lease-active-testing",
                "kind": "skill",
                "actionability": "load",
                "install_descriptor_digest": None,
                "install_plan_digest": None,
            }
        )
    if include_manual:
        capabilities.append(
            {
                "capability_id": "skill:manual-advice",
                "source_digest": "b" * 64,
                "lease_id": "lease-manual-advice",
                "kind": "skill",
                "actionability": "manual",
                "install_descriptor_digest": None,
                "install_plan_digest": None,
            }
        )
    return {
        "owner_id": "owner-retention",
        "policy_snapshot_digest": "8" * 64,
        "desired_capabilities": capabilities,
    }


def _v3_install_requested():
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(_v3_event("SessionStarted", 0, "v3-start", payload={"host_level": "managing"})),
    )
    state, plan_transition = reduce_replay_v3(
        state,
        _v3_replay(_v3_event("IntentObserved", 1, "v3-plan"), decision=_install_plan()),
    )
    assert _actions(plan_transition, "PresentBundle")
    state, transition = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                2,
                "v3-desired",
                payload=_install_desired(),
            )
        ),
    )
    return state, transition


def _decision_for(request: HostAction, *, decision: str) -> EngineEvent:
    return _v3_event(
        "UserDecision",
        request.precondition_revision,
        f"v3-decision-{decision}",
        payload={
            "consent_id": request.consent_id or "",
            "decision": decision,
            "decision_basis": "interactive",
            "policy_snapshot_digest": "8" * 64,
            "requested_action_id": request.payload["requested_action_id"],
            "requested_action_kind": request.payload["requested_action_kind"],
            "requested_action_content_digest": request.payload["requested_action_content_digest"],
            "requested_action_precondition_revision": request.payload[
                "requested_action_precondition_revision"
            ],
        },
    )


def _expiry_for(request: HostAction, *, event_id: str = "v3-consent-expired") -> EngineEvent:
    return _v3_event(
        "InstallConsentExpired",
        request.precondition_revision,
        event_id,
        payload={
            "consent_id": request.consent_id or "",
            "policy_snapshot_digest": request.payload["policy_snapshot_digest"],
            "requested_action_id": request.payload["requested_action_id"],
            "requested_action_kind": request.payload["requested_action_kind"],
            "requested_action_content_digest": request.payload["requested_action_content_digest"],
            "requested_action_precondition_revision": request.payload[
                "requested_action_precondition_revision"
            ],
            "install_expires_at": "2026-08-01T13:00:00Z",
        },
    )


def test_v3_install_requires_exact_consent_receipt_then_activation() -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    capability = state.capability("skill:remote-testing")
    assert capability is not None and capability.installation == "absent"
    assert not _actions(requested, "InstallCapability")
    assert (
        _install_descriptor_payload(request)["descriptor_digest"]
        == _desired_rows(_install_desired())[0]["install_descriptor_digest"]
    )
    assert _install_descriptor_payload(request)["plan_digest"] == "7" * 64
    assert _install_descriptor_payload(request)["installer_id"] == "skill-installer"
    assert request.payload["policy_snapshot_digest"] == "8" * 64
    assert state.install_policy_snapshot_digest == "8" * 64
    assert "command" not in request.to_json()
    assert EngineState.from_json(state.to_json()) == state

    state, granted = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(request, decision="granted")),
    )
    install = _actions(granted, "InstallCapability")[0]
    assert install.content_digest == request.payload["requested_action_content_digest"]
    assert _install_descriptor_payload(install) == _install_descriptor_payload(request)
    assert install.precondition_revision == state.revision
    assert install.required_host_feature == "installation"

    state, activation = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ActionApplied",
                state.revision,
                "v3-install-applied",
                payload={
                    "action_id": install.action_id,
                    "action_kind": install.kind,
                    "action_content_digest": install.content_digest,
                    "action_precondition_revision": install.precondition_revision,
                    "verification": _v3_applied_verification(install),
                },
            )
        ),
    )
    capability = state.capability("skill:remote-testing")
    assert capability is not None and capability.installation == "installed"
    activate = _actions(activation, "ActivateCapability")[0]
    state, _ = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ActionApplied",
                state.revision,
                "v3-activation-applied",
                payload={
                    "action_id": activate.action_id,
                    "action_kind": activate.kind,
                    "action_content_digest": activate.content_digest,
                    "action_precondition_revision": activate.precondition_revision,
                    "verification": _v3_applied_verification(activate),
                },
            )
        ),
    )
    assert state.active_capability_ids == frozenset({"skill:remote-testing"})


def test_v3_install_request_replay_is_deterministic() -> None:
    left_state, left_transition = _v3_install_requested()
    right_state, right_transition = _v3_install_requested()

    assert right_state == left_state
    assert right_state.to_json() == left_state.to_json()
    assert right_transition == left_transition


def test_install_consent_expiry_retires_only_exact_consent_without_human_decision() -> None:
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event("SessionStarted", 0, "v3-expiry-start", payload={"host_level": "managing"})
        ),
    )
    state, _ = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event("IntentObserved", 1, "v3-expiry-plan"),
            decision=_two_install_plan(),
        ),
    )
    state, first_transition = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                2,
                "v3-expiry-desired",
                payload=_two_install_desired(),
            )
        ),
    )
    first_request = _actions(first_transition, "RequestConsent")[0]
    first_pending = state.pending_consents[0]

    state_after_denial, second_transition = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(first_request, decision="denied")),
    )
    assert _actions(second_transition, "RequestConsent")
    second_pending = state_after_denial.pending_consents[0]
    state_with_two = replace(
        state,
        pending_consents=(first_pending, second_pending),
    )

    event = _expiry_for(first_request)
    next_state, transition = reduce_replay_v3(state_with_two, _v3_replay(event))

    assert next_state.pending_consents == (second_pending,)
    assert next_state.blocked_install_descriptor_digests == ()
    assert transition.actions == ()
    assert transition.diagnostics == (
        {
            "code": "install_consent_expired",
            "capability_id": first_request.entity_id,
            "consent_id": first_request.consent_id,
        },
    )
    assert "decision" not in event.payload


def test_later_reassessment_after_consent_expiry_creates_fresh_request() -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]

    before_expiry = state
    expiry_replay = _v3_replay(_expiry_for(request))
    state, expired = reduce_replay_v3(before_expiry, expiry_replay)
    replayed_state, replayed_transition = reduce_replay_v3(before_expiry, expiry_replay)
    assert replayed_state == state
    assert replayed_state.to_json() == state.to_json()
    assert replayed_transition == expired
    assert expired.actions == ()
    assert state.pending_consents == ()

    state, reassessed = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                state.revision,
                "v3-after-expiry-reassessment",
                payload=_install_desired(),
            )
        ),
    )
    fresh = _actions(reassessed, "RequestConsent")
    assert len(fresh) == 1
    assert fresh[0].consent_id != request.consent_id
    assert (
        fresh[0].payload["requested_action_content_digest"]
        != request.payload["requested_action_content_digest"]
    )
    assert state.pending_consents[0].consent_id == fresh[0].consent_id


@pytest.mark.parametrize(
    "field",
    [
        "consent_id",
        "policy_snapshot_digest",
        "requested_action_id",
        "requested_action_kind",
        "requested_action_content_digest",
        "requested_action_precondition_revision",
        "install_expires_at",
    ],
)
def test_install_consent_expiry_rejects_substituted_binding(field: str) -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    raw = _expiry_for(request).to_dict()
    raw["payload"][field] = (
        cast(int, request.payload["requested_action_precondition_revision"]) + 1
        if field == "requested_action_precondition_revision"
        else "UninstallCapability"
        if field == "requested_action_kind"
        else "9" * 64
        if "digest" in field
        else "2026-08-01T14:00:00Z"
        if field == "install_expires_at"
        else f"wrong-{field}"
    )

    with pytest.raises(
        (InvalidEventError, ProtocolValidationError),
        match="(does not match the exact pending consent|unknown or completed consent|must be InstallCapability)",
    ):
        reduce_replay_v3(state, _v3_replay(EngineEvent.from_dict(raw)))


def test_install_consent_expiry_rejects_unknown_or_completed_consent() -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    event = _expiry_for(request)
    state, _ = reduce_replay_v3(state, _v3_replay(event))
    raw = event.to_dict()
    raw["event_id"] = "v3-repeat-expiry"
    raw["expected_revision"] = state.revision

    with pytest.raises(InvalidEventError, match="unknown or completed consent"):
        reduce_replay_v3(state, _v3_replay(EngineEvent.from_dict(raw)))


@pytest.mark.parametrize("digest_field", ["descriptor_digest", "plan_digest"])
def test_v3_state_rejects_substituted_pending_consent_before_release(
    digest_field: str,
) -> None:
    state, _ = _v3_install_requested()
    forged = state.to_dict()
    forged["pending_consents"][0]["install_action"]["payload"]["install_plan_descriptor"][
        digest_field
    ] = "9" * 64

    with pytest.raises(StateValidationError, match="invalid action"):
        EngineState.from_dict(forged)


@pytest.mark.parametrize("digest_field", ["descriptor_digest", "plan_digest"])
def test_v3_state_rejects_substituted_pending_install_effect(
    digest_field: str,
) -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    state, granted = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(request, decision="granted")),
    )
    assert _actions(granted, "InstallCapability")
    forged = state.to_dict()
    forged["pending_effects"][0]["action"]["payload"]["install_plan_descriptor"][digest_field] = (
        "9" * 64
    )

    with pytest.raises(StateValidationError, match="invalid action"):
        EngineState.from_dict(forged)


def test_intervening_revision_reissues_consent_that_remains_exactly_answerable() -> None:
    state, requested = _v3_install_requested()
    old_request = _actions(requested, "RequestConsent")[0]

    state, intervening = reduce_replay_v3(
        state,
        _v3_replay(_v3_event("TurnEnded", state.revision, "v3-intervening-turn")),
    )

    new_requests = _actions(intervening, "RequestConsent")
    assert len(new_requests) == 1
    new_request = new_requests[0]
    assert new_request.consent_id != old_request.consent_id
    assert (
        _install_descriptor_payload(new_request)["descriptor_digest"]
        == _install_descriptor_payload(old_request)["descriptor_digest"]
    )
    assert (
        _install_descriptor_payload(new_request)["plan_digest"]
        == (_install_descriptor_payload(old_request)["plan_digest"])
    )
    assert (
        new_request.payload["policy_snapshot_digest"]
        == old_request.payload["policy_snapshot_digest"]
    )
    assert new_request.payload["requested_action_precondition_revision"] == state.revision + 1
    assert len(state.pending_consents) == 1
    assert state.pending_consents[0].consent_id == new_request.consent_id

    old_decision = _decision_for(old_request, decision="granted").to_dict()
    old_decision["expected_revision"] = state.revision
    old_decision["payload"]["requested_action_precondition_revision"] = state.revision + 1
    with pytest.raises(InvalidEventError, match="unknown or completed consent"):
        reduce_replay_v3(
            state,
            _v3_replay(EngineEvent.from_dict(old_decision)),
        )

    state, granted = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(new_request, decision="granted")),
    )
    install = _actions(granted, "InstallCapability")[0]
    assert install.content_digest == new_request.payload["requested_action_content_digest"]
    assert install.precondition_revision == state.revision


@pytest.mark.parametrize("outcome", ["denied", "expired"])
def test_denial_or_expiry_immediately_surfaces_next_selected_install(outcome: str) -> None:
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event(
                "SessionStarted", 0, f"v3-{outcome}-start", payload={"host_level": "managing"}
            )
        ),
    )
    state, _ = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event("IntentObserved", 1, f"v3-{outcome}-plan"),
            decision=_two_install_plan(),
        ),
    )
    state, requested = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                2,
                f"v3-{outcome}-desired",
                payload=_two_install_desired(),
            )
        ),
    )
    first_request = _actions(requested, "RequestConsent")[0]
    assert first_request.entity_id == "skill:remote-alpha"
    decision = _decision_for(first_request, decision="denied" if outcome == "denied" else "granted")
    if outcome == "expired":
        raw_decision = decision.to_dict()
        raw_decision["occurred_at"] = "2026-08-01T13:00:00Z"
        decision = EngineEvent.from_dict(raw_decision)

    state, next_transition = reduce_replay_v3(state, _v3_replay(decision))

    next_requests = _actions(next_transition, "RequestConsent")
    assert len(next_requests) == 1
    assert next_requests[0].entity_id == "skill:remote-beta"
    assert _install_descriptor_payload(next_requests[0])["plan_digest"] == "c" * 64
    desired = _desired_rows(_two_install_desired())
    assert (
        _install_descriptor_payload(next_requests[0])["descriptor_digest"]
        == desired[1]["install_descriptor_digest"]
    )
    assert state.blocked_install_descriptor_digests == (desired[0]["install_descriptor_digest"],)
    assert len(state.pending_consents) == 1
    assert state.pending_consents[0].consent_id == next_requests[0].consent_id
    assert {item["code"] for item in next_transition.diagnostics} == {f"install_consent_{outcome}"}

    beta_only = _two_install_desired()
    desired_capabilities = beta_only["desired_capabilities"]
    assert isinstance(desired_capabilities, list)
    beta_only["desired_capabilities"] = desired_capabilities[1:]
    state, pruned = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                state.revision,
                f"v3-{outcome}-beta-only",
                payload=beta_only,
            )
        ),
    )
    assert state.blocked_install_descriptor_digests == ()
    assert _actions(pruned, "RequestConsent")[0].entity_id == "skill:remote-beta"


def test_failed_descriptor_does_not_block_changed_risk_or_provenance_descriptor() -> None:
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event(
                "SessionStarted", 0, "v3-shared-plan-start", payload={"host_level": "managing"}
            )
        ),
    )
    state, _ = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event("IntentObserved", 1, "v3-shared-plan"),
            decision=_two_install_plan(),
        ),
    )
    state, requested = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                2,
                "v3-shared-plan-desired",
                payload=_two_install_desired(),
            )
        ),
    )
    first_request = _actions(requested, "RequestConsent")[0]
    state, granted = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(first_request, decision="granted")),
    )
    first_install = _actions(granted, "InstallCapability")[0]

    state, failed = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ActionFailed",
                state.revision,
                "v3-shared-plan-first-failed",
                payload={
                    "action_id": first_install.action_id,
                    "action_kind": first_install.kind,
                    "action_content_digest": first_install.content_digest,
                    "action_precondition_revision": first_install.precondition_revision,
                    "error": {"code": "redacted-host-failure"},
                },
            )
        ),
    )

    next_requests = _actions(failed, "RequestConsent")
    assert len(next_requests) == 1
    assert next_requests[0].entity_id == "skill:remote-beta"
    assert (
        _install_descriptor_payload(next_requests[0])["plan_digest"]
        == _install_descriptor_payload(first_install)["plan_digest"]
    )
    desired = _desired_rows(_two_install_desired())
    assert (
        _install_descriptor_payload(next_requests[0])["descriptor_digest"]
        == desired[1]["install_descriptor_digest"]
    )
    assert state.blocked_install_descriptor_digests == (desired[0]["install_descriptor_digest"],)
    assert all(action.entity_id != "skill:remote-alpha" for action in failed.actions)


def test_manual_v3_selection_stays_advisory_on_managing_host() -> None:
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event("SessionStarted", 0, "v3-manual-start", payload={"host_level": "managing"})
        ),
    )
    state, planned = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event("IntentObserved", 1, "v3-manual-plan"),
            decision=_manual_plan(),
        ),
    )
    assert _actions(planned, "PresentBundle")

    state, reassessed = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                2,
                "v3-manual-desired",
                payload=_manual_desired(),
            )
        ),
    )

    assert state.capability("skill:manual-advice") is None
    assert "skill:manual-advice" not in state.desired_capability_ids
    assert "skill:manual-advice" not in state.active_capability_ids
    assert not _actions(reassessed, "ActivateCapability")
    assert not state.pending_effects


def test_v3_retains_relevant_active_lease_without_representing_and_cools_when_omitted() -> None:
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event("SessionStarted", 0, "v3-retention-start", payload={"host_level": "managing"})
        ),
    )
    state, initial_bundle = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "IntentObserved",
                1,
                "v3-retention-initial-plan",
                correlation_id="plan-retention-1",
            ),
            decision=_retention_plan(include_active=True, include_manual=False),
        ),
    )
    assert _actions(initial_bundle, "PresentBundle")
    state, requested = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                2,
                "v3-retention-initial-desired",
                payload=_retention_desired(include_active=True, include_manual=False),
                correlation_id="plan-retention-1",
            )
        ),
    )
    activate = _actions(requested, "ActivateCapability")[0]
    state, _ = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ActionApplied",
                state.revision,
                "v3-retention-active",
                payload={
                    "action_id": activate.action_id,
                    "action_kind": activate.kind,
                    "action_content_digest": activate.content_digest,
                    "action_precondition_revision": activate.precondition_revision,
                    "verification": _v3_applied_verification(activate),
                },
            )
        ),
    )
    active = state.capability("skill:active-testing")
    assert active is not None
    assert active.activation == "active"
    assert tuple(lease.lease_id for lease in active.leases) == ("lease-active-testing",)

    state, retained_only = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "IntentObserved",
                state.revision,
                "v3-retention-only-plan",
                correlation_id="plan-retention-2",
            ),
            decision=_retention_plan(include_active=True, include_manual=False),
        ),
    )
    assert not _actions(retained_only, "PresentBundle")
    assert tuple(
        item.capability_id
        for item in state.committed_plan.capabilities  # type: ignore[union-attr]
    ) == ("skill:active-testing",)

    state, relevant_bundle = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "IntentObserved",
                state.revision,
                "v3-retention-relevant-plan",
                correlation_id="plan-retention-3",
            ),
            decision=_retention_plan(include_active=True, include_manual=True),
        ),
    )
    presented = _actions(relevant_bundle, "PresentBundle")
    assert len(presented) == 1
    assert tuple(item["capability_id"] for item in presented[0].payload["capabilities"]) == (
        "skill:manual-advice",
    )
    assert tuple(
        item.capability_id
        for item in state.committed_plan.capabilities  # type: ignore[union-attr]
    ) == ("skill:active-testing", "skill:manual-advice")

    state, retained = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                state.revision,
                "v3-retention-relevant-desired",
                payload=_retention_desired(include_active=False, include_manual=True),
                correlation_id="plan-retention-3",
            )
        ),
    )
    active = state.capability("skill:active-testing")
    assert active is not None
    assert active.activation == "active"
    assert tuple(lease.lease_id for lease in active.leases) == ("lease-active-testing",)
    assert not _actions(retained, "DeactivateCapability")
    assert state.capability("skill:manual-advice") is None

    state, cooling = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "IntentObserved",
                state.revision,
                "v3-retention-irrelevant-plan",
                correlation_id="plan-retention-4",
            ),
            decision=_retention_plan(include_active=False, include_manual=True),
        ),
    )
    state, after_reassessment = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                state.revision,
                "v3-retention-irrelevant-desired",
                payload=_retention_desired(include_active=False, include_manual=True),
                correlation_id="plan-retention-4",
            )
        ),
    )
    active = state.capability("skill:active-testing")
    assert active is not None
    assert not active.desired
    assert _actions(cooling, "DeactivateCapability")[0].entity_id == "skill:active-testing"
    assert not _actions(after_reassessment, "DeactivateCapability")


@pytest.mark.parametrize(
    ("old_policy", "new_policy"),
    [("8" * 64, "9" * 64), ("9" * 64, "8" * 64)],
    ids=["ask-to-auto", "auto-to-ask"],
)
def test_policy_change_replaces_unanswered_consent_request(
    old_policy: str,
    new_policy: str,
) -> None:
    state, requested = _v3_install_requested()
    if old_policy != "8" * 64:
        state, requested = reduce_replay_v3(
            state,
            _v3_replay(
                _v3_event(
                    "ReassessmentRequested",
                    state.revision,
                    "v3-old-policy",
                    payload=_install_desired(old_policy),
                )
            ),
        )
    old_request = _actions(requested, "RequestConsent")[0]

    state, changed = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                state.revision,
                "v3-new-policy",
                payload=_install_desired(new_policy),
            )
        ),
    )
    new_request = _actions(changed, "RequestConsent")[0]

    assert new_request.consent_id != old_request.consent_id
    assert old_request.payload["policy_snapshot_digest"] == old_policy
    assert new_request.payload["policy_snapshot_digest"] == new_policy
    assert state.install_policy_snapshot_digest == new_policy
    assert len(state.pending_consents) == 1
    assert state.pending_consents[0].consent_id == new_request.consent_id


def test_policy_change_does_not_cancel_granted_physical_install() -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    state, granted = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(request, decision="granted")),
    )
    install = _actions(granted, "InstallCapability")[0]

    state, changed = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                state.revision,
                "v3-policy-after-grant",
                payload=_install_desired("9" * 64),
            )
        ),
    )

    assert not changed.actions
    assert state.install_policy_snapshot_digest == "8" * 64
    assert state.pending_effects[0].action == install
    assert install.payload["policy_snapshot_digest"] == "8" * 64


def test_v3_denial_and_failed_install_block_the_exact_descriptor() -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    state, denied = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(request, decision="denied")),
    )
    assert not denied.actions
    descriptor_digest = _desired_rows(_install_desired())[0]["install_descriptor_digest"]
    assert state.blocked_install_descriptor_digests == (descriptor_digest,)

    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    state, granted = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(request, decision="granted")),
    )
    install = _actions(granted, "InstallCapability")[0]
    state, failed = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ActionFailed",
                state.revision,
                "v3-install-failed",
                payload={
                    "action_id": install.action_id,
                    "action_kind": install.kind,
                    "action_content_digest": install.content_digest,
                    "action_precondition_revision": install.precondition_revision,
                    "error": {"code": "redacted-host-failure"},
                },
            )
        ),
    )
    assert not failed.actions
    assert state.blocked_install_descriptor_digests == (descriptor_digest,)
    capability = state.capability("skill:remote-testing")
    assert capability is not None and capability.installation == "absent"


def test_v3_unclaimed_expired_install_stays_absent_without_marking_driver_failed() -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    state, granted = reduce_replay_v3(
        state,
        _v3_replay(_decision_for(request, decision="granted")),
    )
    install = _actions(granted, "InstallCapability")[0]
    state, transition = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ActionExpired",
                state.revision,
                "v3-install-expired",
                payload={
                    "action_id": install.action_id,
                    "action_kind": install.kind,
                    "action_content_digest": install.content_digest,
                    "action_precondition_revision": install.precondition_revision,
                    "reason": "expired",
                },
            )
        ),
    )

    capability = state.capability("skill:remote-testing")
    assert capability is not None and capability.installation == "absent"
    assert state.blocked_install_descriptor_digests == ()
    assert not transition.actions


def test_v3_rejects_wrong_decision_and_intervening_revision() -> None:
    state, requested = _v3_install_requested()
    request = _actions(requested, "RequestConsent")[0]
    wrong = _decision_for(request, decision="granted").to_dict()
    wrong["payload"]["requested_action_content_digest"] = "9" * 64
    with pytest.raises(InvalidEventError, match="exact requested install"):
        reduce_replay_v3(state, _v3_replay(EngineEvent.from_dict(wrong)))

    wrong_policy = _decision_for(request, decision="granted").to_dict()
    wrong_policy["payload"]["policy_snapshot_digest"] = "9" * 64
    with pytest.raises(InvalidEventError, match="policy snapshot"):
        reduce_replay_v3(state, _v3_replay(EngineEvent.from_dict(wrong_policy)))

    state, _ = reduce_replay_v3(
        state,
        _v3_replay(_v3_event("TurnEnded", state.revision, "v3-intervening")),
    )
    with pytest.raises(RevisionConflictError, match="expected revision"):
        reduce_replay_v3(
            state,
            _v3_replay(_decision_for(request, decision="granted")),
        )


@pytest.mark.parametrize("mutated_field", ["source_digest", "install_descriptor_digest"])
def test_v3_reassessment_cannot_invent_or_mutate_planner_identity(
    mutated_field: str,
) -> None:
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event("SessionStarted", 0, "v3-identity-start", payload={"host_level": "managing"})
        ),
    )
    state, _ = reduce_replay_v3(
        state,
        _v3_replay(_v3_event("IntentObserved", 1, "v3-identity-plan"), decision=_install_plan()),
    )
    desired = _install_desired()
    desired_capabilities = desired["desired_capabilities"]
    assert isinstance(desired_capabilities, list)
    desired_capability = desired_capabilities[0]
    assert isinstance(desired_capability, dict)
    desired_capability[mutated_field] = "9" * 64
    with pytest.raises(InvalidEventError, match="committed identity"):
        reduce_replay_v3(
            state,
            _v3_replay(
                _v3_event("ReassessmentRequested", state.revision, "v3-mutated", payload=desired)
            ),
        )


def test_reducer_versions_cannot_downgrade_or_reinterpret_stream_state() -> None:
    v3_state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event("SessionStarted", 0, "v3-version-start", payload={"host_level": "managing"})
        ),
    )
    next_event = _v3_replay(_v3_event("TurnEnded", 1, "v3-version-next"))
    with pytest.raises(InvalidEventError, match="cannot continue"):
        reduce_replay_v1(v3_state, next_event)
    with pytest.raises(InvalidEventError, match="cannot continue"):
        reduce_replay_v2(v3_state, next_event)

    legacy_state, _ = _start()
    with pytest.raises(InvalidEventError, match="requires an installation-contract"):
        reduce_replay_v3(
            legacy_state,
            _v3_replay(_v3_event("TurnEnded", legacy_state.revision, "legacy-to-v3")),
        )


def test_only_session_started_can_create_state_and_it_advances_revision() -> None:
    with pytest.raises(InvalidEventError, match="SessionStarted"):
        reduce(
            None,
            _event("IntentObserved", 0, event_id="event-invalid-first"),
        )

    state, transition = _start()

    assert state.revision == 1
    assert state.session_status == "active"
    assert transition.from_revision == 0
    assert transition.to_revision == 1

    with pytest.raises(InvalidEventError, match="already started"):
        reduce(
            state,
            _event("SessionStarted", 1, event_id="event-second-start"),
        )


def test_revision_and_session_scope_are_checked_before_reduction() -> None:
    state, _ = _start()

    with pytest.raises(RevisionConflictError, match="expected revision 1"):
        reduce(
            state,
            _event("IntentObserved", 0, event_id="event-stale"),
        )

    other_session = ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-other",
        exposure_id="exposure-parent",
        host_context_id="host-1",
    )
    with pytest.raises(InvalidEventError, match="scope"):
        reduce(
            state,
            _event(
                "IntentObserved",
                1,
                event_id="event-other-session",
                scope=other_session,
            ),
        )


def test_activation_is_desired_before_it_is_receipt_confirmed() -> None:
    state, _ = _start()
    state, transition = _desired(
        state,
        [("skill:python-testing", "digest-testing", "lease-parent-testing")],
    )

    activation = _actions(transition, "ActivateCapability")
    assert len(activation) == 1
    assert state.desired_capability_ids == frozenset({"skill:python-testing"})
    assert state.active_capability_ids == frozenset()

    state, _ = _receipt(
        state,
        activation[0],
        applied=True,
        event_id="event-activation-applied",
    )

    assert state.active_capability_ids == frozenset({"skill:python-testing"})


def test_global_belt_is_five_unique_capabilities_across_parent_and_child() -> None:
    state, _ = _start()
    five = [
        (f"skill:capability-{index}", f"digest-{index}", f"lease-parent-{index}")
        for index in range(5)
    ]
    state, transition = _desired(state, five)
    for index, action in enumerate(_actions(transition, "ActivateCapability")):
        state, _ = _receipt(
            state,
            action,
            applied=True,
            event_id=f"event-activate-{index}",
        )

    child_scope = _scope(
        exposure_id="exposure-child",
        parent_exposure_id="exposure-parent",
    )
    state, duplicate_transition = _desired(
        state,
        [("skill:capability-0", "digest-0", "lease-child-shared")],
        owner_id="owner-child",
        event_id="event-child-shared",
        scope=child_scope,
    )
    state, sixth_transition = _desired(
        state,
        [("skill:capability-6", "digest-6", "lease-child-sixth")],
        owner_id="owner-child",
        event_id="event-child-sixth",
        scope=child_scope,
    )

    assert state.active_capability_ids == frozenset(item[0] for item in five)
    assert not _actions(duplicate_transition, "ActivateCapability")
    assert not _actions(sixth_transition, "ActivateCapability")
    assert any(
        diagnostic["code"] == "active_capability_budget_exhausted"
        for diagnostic in sixth_transition.diagnostics
    )


def test_shared_activation_deactivates_only_after_last_owner_releases() -> None:
    state, _ = _start()
    shared = ("mcp:repository-tools", "digest-mcp", "lease-parent")
    state, requested = _desired(state, [shared])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-shared-active",
    )
    child_scope = _scope(
        exposure_id="exposure-child",
        parent_exposure_id="exposure-parent",
    )
    state, joined = _desired(
        state,
        [(shared[0], shared[1], "lease-child")],
        owner_id="owner-child",
        event_id="event-child-joins",
        scope=child_scope,
    )
    assert not _actions(joined, "ActivateCapability")

    state, parent_released = _desired(
        state,
        [],
        event_id="event-parent-releases",
    )
    assert not _actions(parent_released, "DeactivateCapability")

    state, child_released = _desired(
        state,
        [],
        owner_id="owner-child",
        event_id="event-child-releases",
        scope=child_scope,
    )
    deactivation = _actions(child_released, "DeactivateCapability")
    assert len(deactivation) == 1
    assert state.active_capability_ids == frozenset({shared[0]})

    state, _ = _receipt(
        state,
        deactivation[0],
        applied=True,
        event_id="event-shared-inactive",
    )
    assert state.active_capability_ids == frozenset()


def test_provider_submission_records_exposure_but_not_invocation() -> None:
    state, _ = _start()
    capability = ("skill:python-testing", "digest-testing", "lease-parent")
    state, requested = _desired(state, [capability])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-skill-active",
    )

    state, _ = reduce(
        state,
        _event(
            "ProviderSubmissionObserved",
            state.revision,
            event_id="event-provider-submission",
            payload={
                "capabilities": [
                    {
                        "capability_id": capability[0],
                        "source_digest": capability[1],
                    }
                ]
            },
        ),
    )

    evidence = state.evidence_for("exposure-parent", capability[0])
    assert evidence.exposure == "submitted"
    assert evidence.invocation == "not-invoked"

    state, _ = reduce(
        state,
        _event(
            "ToolCallObserved",
            state.revision,
            event_id="event-tool-call-failed",
            payload={
                "capability_id": capability[0],
                "source_digest": capability[1],
                "outcome": "failed",
            },
        ),
    )
    evidence = state.evidence_for("exposure-parent", capability[0])
    assert evidence.exposure == "submitted"
    assert evidence.invocation == "invoked-failed"


def test_query_only_host_presents_manual_bundle_without_claiming_activation() -> None:
    state, _ = _start(host_level="query-only")
    state, transition = _desired(
        state,
        [("agent:security-review", "digest-agent", "lease-agent")],
    )

    assert not _actions(transition, "ActivateCapability")
    assert len(_actions(transition, "PresentBundle")) == 1
    assert state.active_capability_ids == frozenset()
    assert any(
        diagnostic["code"] == "host_activation_unsupported" for diagnostic in transition.diagnostics
    )


def test_terminal_session_rejects_new_work_but_accepts_pending_receipt() -> None:
    state, _ = _start()
    state, requested = _desired(
        state,
        [("skill:python-testing", "digest-testing", "lease-parent")],
    )
    action = _actions(requested, "ActivateCapability")[0]
    state, _ = reduce(
        state,
        _event(
            "SessionEnded",
            state.revision,
            event_id="event-ended",
        ),
    )

    with pytest.raises(InvalidEventError, match="ended"):
        reduce(
            state,
            _event(
                "IntentObserved",
                state.revision,
                event_id="event-after-ended",
            ),
        )

    state, _ = _receipt(
        state,
        action,
        applied=True,
        event_id="event-late-receipt",
    )
    assert state.active_capability_ids == frozenset({"skill:python-testing"})


def test_receipt_must_match_the_exact_pending_action_digest() -> None:
    state, _ = _start()
    state, requested = _desired(
        state,
        [("skill:python-testing", "digest-testing", "lease-parent")],
    )
    action = _actions(requested, "ActivateCapability")[0]
    mismatched = _event(
        "ActionApplied",
        state.revision,
        event_id="event-mismatched-receipt",
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": "0" * 64,
            "action_precondition_revision": action.precondition_revision,
            "verification": {"host_state": "active"},
        },
        scope=action.scope,
    )

    with pytest.raises(InvalidEventError, match="content_digest"):
        reduce(state, mismatched)

    assert state.active_capability_ids == frozenset()


def test_turn_preparation_receipt_does_not_claim_submission_or_use() -> None:
    state, _ = _start()
    capability = ("skill:python-testing", "digest-testing", "lease-parent")
    state, requested = _desired(state, [capability])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-skill-active-for-preparation",
    )

    state, turn = reduce(
        state,
        _event(
            "TurnStarting",
            state.revision,
            event_id="event-turn-starting",
        ),
    )
    preparation = _actions(turn, "PrepareExposure")
    assert len(preparation) == 1

    state, _ = _receipt(
        state,
        preparation[0],
        applied=True,
        event_id="event-preparation-applied",
    )

    evidence = state.evidence_for("exposure-parent", capability[0])
    assert evidence.exposure == "prepared"
    assert evidence.invocation == "not-invoked"


def test_full_belt_replacement_is_sequential_and_rolls_back_on_failure() -> None:
    state, _ = _start()
    original = [
        (f"skill:capability-{index}", f"digest-{index}", f"lease-{index}") for index in range(5)
    ]
    state, initial = _desired(state, original)
    for index, action in enumerate(_actions(initial, "ActivateCapability")):
        state, _ = _receipt(
            state,
            action,
            applied=True,
            event_id=f"event-original-active-{index}",
        )

    replacement = (*original[1:], ("skill:replacement", "digest-new", "lease-new"))
    state, removal = _desired(
        state,
        replacement,
        event_id="event-request-replacement",
    )
    assert not _actions(removal, "ActivateCapability")
    deactivation = _actions(removal, "DeactivateCapability")
    assert len(deactivation) == 1
    assert deactivation[0].entity_id == original[0][0]
    assert len(state.active_capability_ids) == 5

    state, after_removal = _receipt(
        state,
        deactivation[0],
        applied=True,
        event_id="event-old-deactivated",
    )
    activation = _actions(after_removal, "ActivateCapability")
    assert len(activation) == 1
    assert activation[0].entity_id == "skill:replacement"
    assert len(state.active_capability_ids) == 4

    state, failed = _receipt(
        state,
        activation[0],
        applied=False,
        event_id="event-replacement-failed",
    )
    rollback = _actions(failed, "ActivateCapability")
    assert len(rollback) == 1
    assert rollback[0].entity_id == original[0][0]

    state, _ = _receipt(
        state,
        rollback[0],
        applied=True,
        event_id="event-rollback-applied",
    )
    assert state.active_capability_ids == frozenset(item[0] for item in original)
    assert "skill:replacement" not in state.active_capability_ids

    state, after_rollback_reassessment = _desired(
        state,
        replacement,
        event_id="event-after-rollback-reassessment",
    )
    cleanup = _actions(after_rollback_reassessment, "DeactivateCapability")
    assert len(cleanup) == 1
    assert cleanup[0].entity_id == original[0][0]


def test_reducing_the_same_normalized_sequence_is_deterministic() -> None:
    start = _event(
        "SessionStarted",
        0,
        event_id="event-deterministic-start",
        payload={"host_level": "activating"},
    )
    desired = _event(
        "ReassessmentRequested",
        1,
        event_id="event-deterministic-desired",
        payload={
            "owner_id": "owner-parent",
            "desired_capabilities": [
                {
                    "capability_id": "skill:python-testing",
                    "source_digest": "digest-testing",
                    "lease_id": "lease-testing",
                }
            ],
        },
    )

    left_state, left_start = reduce(None, start)
    left_state, left_desired = reduce(left_state, desired)
    right_state, right_start = reduce(None, start)
    right_state, right_desired = reduce(right_state, desired)

    assert right_start == left_start
    assert right_desired == left_desired
    assert right_state == left_state


def test_parallel_removals_pair_each_replacement_with_a_distinct_rollback() -> None:
    state, _ = _start()
    original = [
        (f"skill:capability-{index}", f"digest-{index}", f"lease-{index}") for index in range(5)
    ]
    state, initial = _desired(state, original)
    for index, action in enumerate(_actions(initial, "ActivateCapability")):
        state, _ = _receipt(
            state,
            action,
            applied=True,
            event_id=f"event-paired-original-{index}",
        )
    desired = (
        *original[2:],
        ("skill:replacement-a", "digest-new-a", "lease-new-a"),
        ("skill:replacement-b", "digest-new-b", "lease-new-b"),
    )
    state, removal = _desired(
        state,
        desired,
        event_id="event-two-replacements",
    )
    removals = _actions(removal, "DeactivateCapability")
    assert len(removals) == 1

    state, first_addition = _receipt(
        state,
        removals[0],
        applied=True,
        event_id="event-first-old-inactive",
    )
    first_activation = _actions(first_addition, "ActivateCapability")[0]
    first_rollback = next(
        pending.rollback_capability_id
        for pending in state.pending_effects
        if pending.action.action_id == first_activation.action_id
    )
    state, second_removal = _receipt(
        state,
        first_activation,
        applied=True,
        event_id="event-first-new-active",
    )
    second_deactivation = _actions(second_removal, "DeactivateCapability")[0]
    state, second_addition = _receipt(
        state,
        second_deactivation,
        applied=True,
        event_id="event-second-old-inactive",
    )
    second_activation = _actions(second_addition, "ActivateCapability")[0]
    second_rollback = next(
        pending.rollback_capability_id
        for pending in state.pending_effects
        if pending.action.action_id == second_activation.action_id
    )

    assert first_rollback != second_rollback


def test_parent_only_capability_is_not_prepared_for_child_exposure() -> None:
    state, _ = _start()
    state, requested = _desired(
        state,
        [("skill:parent-only", "digest-parent", "lease-parent")],
    )
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-parent-only-active",
        host_state="active",
    )
    child_scope = _scope(
        exposure_id="exposure-child",
        parent_exposure_id="exposure-parent",
    )

    _state, child_turn = reduce(
        state,
        _event(
            "TurnStarting",
            state.revision,
            event_id="event-child-turn-isolated",
            scope=child_scope,
        ),
    )

    assert not _actions(child_turn, "PrepareExposure")


def test_receipt_verification_must_report_the_requested_physical_state() -> None:
    state, _ = _start()
    state, requested = _desired(
        state,
        [("skill:verified", "digest-verified", "lease-verified")],
    )
    action = _actions(requested, "ActivateCapability")[0]

    with pytest.raises(InvalidEventError, match="verification"):
        _receipt(
            state,
            action,
            applied=True,
            event_id="event-contradictory-verification",
            host_state="inactive",
        )

    assert state.active_capability_ids == frozenset()


def test_late_prepare_receipt_cannot_regress_submitted_exposure() -> None:
    state, _ = _start()
    capability = ("skill:late-prepare", "digest-late", "lease-late")
    state, requested = _desired(state, [capability])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-late-active",
        host_state="active",
    )
    state, turn = reduce(
        state,
        _event("TurnStarting", state.revision, event_id="event-late-turn"),
    )
    preparation = _actions(turn, "PrepareExposure")[0]
    state, _ = reduce(
        state,
        _event(
            "ProviderSubmissionObserved",
            state.revision,
            event_id="event-submitted-before-prepare-receipt",
            payload={
                "capabilities": [{"capability_id": capability[0], "source_digest": capability[1]}]
            },
        ),
    )
    state, _ = _receipt(
        state,
        preparation,
        applied=True,
        event_id="event-late-prepare-receipt",
        host_state="prepared",
    )

    assert state.evidence_for("exposure-parent", capability[0]).exposure == "submitted"


def test_session_scope_spans_host_contexts_but_not_other_sessions() -> None:
    state, _ = _start()
    second_host = ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-second-host",
        host_context_id="host-2",
    )

    state, transition = reduce(
        state,
        _event(
            "IntentObserved",
            state.revision,
            event_id="event-second-host",
            scope=second_host,
        ),
    )

    assert transition.to_revision == state.revision


def test_session_end_and_late_activation_receipt_drive_cleanup() -> None:
    state, _ = _start()
    state, requested = _desired(
        state,
        [("skill:cleanup", "digest-cleanup", "lease-cleanup")],
    )
    activation = _actions(requested, "ActivateCapability")[0]
    state, ended = reduce(
        state,
        _event("SessionEnded", state.revision, event_id="event-cleanup-ended"),
    )
    assert not _actions(ended, "DeactivateCapability")

    state, late = _receipt(
        state,
        activation,
        applied=True,
        event_id="event-cleanup-late-active",
        host_state="active",
        replay_metadata=False,
    )
    cleanup = _actions(late, "DeactivateCapability")
    assert len(cleanup) == 1
    assert cleanup[0].plan_id == activation.plan_id
    assert cleanup[0].catalog_snapshot_id == activation.catalog_snapshot_id


@pytest.mark.parametrize("activation_applied", [True, False])
def test_session_end_preserves_inflight_replacement_until_late_receipt(
    activation_applied: bool,
) -> None:
    state, _ = _start()
    original = [
        (f"skill:capability-{index}", f"digest-{index}", f"lease-{index}") for index in range(5)
    ]
    state, initial = _desired(state, original)
    for index, action in enumerate(_actions(initial, "ActivateCapability")):
        state, _ = _receipt(
            state,
            action,
            applied=True,
            event_id=f"event-original-active-{index}",
        )
    replacement = (*original[1:], ("skill:replacement", "digest-new", "lease-new"))
    state, removal = _desired(
        state,
        replacement,
        event_id="event-request-replacement-before-end",
    )
    state, after_removal = _receipt(
        state,
        _actions(removal, "DeactivateCapability")[0],
        applied=True,
        event_id="event-old-deactivated-before-end",
    )
    activation = _actions(after_removal, "ActivateCapability")[0]

    state, ended = reduce(
        state,
        _event("SessionEnded", state.revision, event_id="event-ended-during-replacement"),
    )

    assert state.session_status == "ended"
    assert not _actions(ended, "ActivateCapability")
    original_capability = state.capability(original[0][0])
    assert original_capability is not None
    assert original_capability.rollback_held is True

    state, late_receipt = _receipt(
        state,
        activation,
        applied=activation_applied,
        event_id="event-late-replacement-receipt",
        replay_metadata=False,
    )

    assert not _actions(late_receipt, "ActivateCapability")
    assert state.rollback_requested_capability_ids == ()
    assert all(not capability.rollback_held for capability in state.capabilities)


@pytest.mark.parametrize("prepare_applied", [True, False])
def test_session_end_waits_for_pending_prepare_before_deactivation(
    prepare_applied: bool,
) -> None:
    state, _ = _start()
    capability = ("skill:preparing", "digest-preparing", "lease-preparing")
    state, requested = _desired(state, [capability])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-preparing-active",
    )
    state, turn = reduce(
        state,
        _event("TurnStarting", state.revision, event_id="event-preparing-turn"),
    )
    preparation = _actions(turn, "PrepareExposure")[0]

    state, ended = reduce(
        state,
        _event("SessionEnded", state.revision, event_id="event-ended-during-prepare"),
    )

    assert not _actions(ended, "DeactivateCapability")

    state, preparation_finished = _receipt(
        state,
        preparation,
        applied=prepare_applied,
        event_id="event-late-prepare-finished",
        replay_metadata=False,
    )
    deactivation = _actions(preparation_finished, "DeactivateCapability")
    assert len(deactivation) == 1

    state, cleanup_finished = _receipt(
        state,
        deactivation[0],
        applied=True,
        event_id="event-preparing-cleaned-up",
        replay_metadata=False,
    )

    assert not _actions(cleanup_finished, "PrepareExposure")
    assert state.active_capability_ids == frozenset()


def test_reassessment_waits_for_pending_prepare_before_deactivation() -> None:
    state, _ = _start()
    capability = ("skill:preparing", "digest-preparing", "lease-preparing")
    state, requested = _desired(state, [capability])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-preparing-active-before-reassessment",
    )
    state, turn = reduce(
        state,
        _event("TurnStarting", state.revision, event_id="event-preparing-reassess-turn"),
    )
    preparation = _actions(turn, "PrepareExposure")[0]

    state, reassessed = _desired(
        state,
        [],
        event_id="event-remove-while-preparing",
    )
    assert not _actions(reassessed, "DeactivateCapability")

    _state, preparation_finished = _receipt(
        state,
        preparation,
        applied=True,
        event_id="event-prepare-before-removal",
    )
    assert len(_actions(preparation_finished, "DeactivateCapability")) == 1


def test_failed_deactivation_waits_for_explicit_reassessment() -> None:
    state, _ = _start()
    capability = ("skill:no-tight-retry", "digest-retry", "lease-retry")
    state, requested = _desired(state, [capability])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-retry-active",
        host_state="active",
    )
    state, release = _desired(state, [], event_id="event-retry-release")
    deactivation = _actions(release, "DeactivateCapability")[0]

    state, failed = _receipt(
        state,
        deactivation,
        applied=False,
        event_id="event-deactivation-failed",
    )

    assert not _actions(failed, "DeactivateCapability")
    assert capability[0] in state.active_capability_ids


def test_unrelated_owner_cannot_erase_an_inflight_replacement_rollback() -> None:
    state, _ = _start()
    original = [
        (f"skill:owner-safe-{index}", f"digest-{index}", f"lease-parent-{index}")
        for index in range(5)
    ]
    state, initial = _desired(state, original)
    for index, action in enumerate(_actions(initial, "ActivateCapability")):
        state, _ = _receipt(
            state,
            action,
            applied=True,
            event_id=f"event-owner-safe-active-{index}",
        )
    replacement = (*original[1:], ("skill:new", "digest-new", "lease-new"))
    state, removing = _desired(
        state,
        replacement,
        event_id="event-parent-starts-replacement",
    )
    deactivation = _actions(removing, "DeactivateCapability")[0]

    child_scope = _scope(
        exposure_id="exposure-child",
        parent_exposure_id="exposure-parent",
    )
    state, _ = _desired(
        state,
        [(original[1][0], original[1][1], "lease-child-shared")],
        owner_id="owner-child",
        event_id="event-child-reassesses-during-replacement",
        scope=child_scope,
    )
    state, _ = _desired(
        state,
        replacement,
        event_id="event-parent-repeats-pending-replacement",
    )
    state, adding = _receipt(
        state,
        deactivation,
        applied=True,
        event_id="event-owner-safe-old-inactive",
    )
    activation = _actions(adding, "ActivateCapability")[0]
    pending = next(
        item for item in state.pending_effects if item.action.action_id == activation.action_id
    )

    assert pending.rollback_capability_id == original[0][0]

    state, failed = _receipt(
        state,
        activation,
        applied=False,
        event_id="event-owner-safe-new-failed",
    )
    rollback = _actions(failed, "ActivateCapability")
    assert len(rollback) == 1
    assert rollback[0].entity_id == original[0][0]


def test_failed_terminal_cleanup_emits_manual_recovery_without_retry_loop() -> None:
    state, _ = _start()
    capability = ("skill:terminal-recovery", "digest-terminal", "lease-terminal")
    state, requested = _desired(state, [capability])
    state, _ = _receipt(
        state,
        _actions(requested, "ActivateCapability")[0],
        applied=True,
        event_id="event-terminal-recovery-active",
    )
    state, ended = reduce(
        state,
        _event("SessionEnded", state.revision, event_id="event-terminal-recovery-end"),
    )
    cleanup = _actions(ended, "DeactivateCapability")[0]

    state, failed = _receipt(
        state,
        cleanup,
        applied=False,
        event_id="event-terminal-cleanup-failed",
    )

    assert not _actions(failed, "DeactivateCapability")
    assert len(_actions(failed, "Notify")) == 1
    assert any(
        item["code"] == "terminal_cleanup_failed_manual_recovery_required"
        for item in failed.diagnostics
    )
    assert capability[0] in state.active_capability_ids

    state, recovery = reduce(
        state,
        _event(
            "ReassessmentRequested",
            state.revision,
            event_id="event-terminal-explicit-recovery",
            payload={"retry_failed_deactivations": [capability[0]]},
        ),
    )
    retry = _actions(recovery, "DeactivateCapability")
    assert len(retry) == 1
    assert retry[0].entity_id == capability[0]
    assert any(item["code"] == "terminal_cleanup_retry_requested" for item in recovery.diagnostics)
