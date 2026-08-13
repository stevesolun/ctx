from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, replace

import pytest

from ctx.engine.content import MaterialIdentity
from ctx.engine.installation import (
    InstallConsentRoutingError,
    InstallConsentPolicy,
    InstallPlanningBundle,
    InstallPlanDescriptor,
    PreparedInstallPlan,
    route_install_authorization,
    route_install_consent_request,
)
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate, CapabilitySelection
from ctx.engine.planning_v3 import (
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
)
from ctx.engine.protocol import EngineEvent, HostAction, ScopeRef
from ctx.engine.reducer import reduce_replay_v3
from ctx.engine.replay import ReplayInput, StructuredSurrogate
from ctx.engine.state import CommittedPlanV3


INSTALLABLE_CAPABILITY_CASES = (
    ("skill:python-testing", "skill"),
    ("agent:reviewer", "agent"),
    ("mcp-server:repository-tools", "mcp-server"),
)
NOW = "2026-08-02T12:00:00Z"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _descriptor(
    *,
    capability_id: str = "skill:python-testing",
    kind: str = "skill",
    permission_expansion: bool = False,
    credential_requirement: bool = False,
) -> InstallPlanDescriptor:
    return InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id="ctx-builtin-installer-v1",
        plan_digest=_digest("plan"),
        provenance_digest=_digest("provenance"),
        permission_expansion=permission_expansion,
        credential_requirement=credential_requirement,
    )


def _prepared(
    descriptor: InstallPlanDescriptor,
    policy: InstallConsentPolicy,
    **overrides: object,
) -> PreparedInstallPlan:
    values: dict[str, object] = {
        "capability_id": descriptor.capability_id,
        "kind": descriptor.kind,
        "installer_id": descriptor.installer_id,
        "action_id": "install-action-1",
        "action_content_digest": _digest("action"),
        "selection_source_digest": _digest("selection"),
        "catalog_snapshot_digest": _digest("catalog"),
        "plan_digest": descriptor.plan_digest,
        "provenance_digest": descriptor.provenance_digest,
        "descriptor_digest": descriptor.descriptor_digest,
        "consent_policy_digest": policy.policy_digest,
        "execution_token": "ephemeral-install-1",
        "permission_expansion": descriptor.permission_expansion,
        "credential_requirement": descriptor.credential_requirement,
    }
    values.update(overrides)
    return PreparedInstallPlan(**values)  # type: ignore[arg-type]


def _selection(descriptor: InstallPlanDescriptor) -> CapabilitySelection:
    return CapabilitySelection(
        capability_id=descriptor.capability_id,
        kind=descriptor.kind,
        name=descriptor.capability_id.split(":", 1)[1],
        source_digest=_digest("selection"),
        normalized_score_ppm=900_000,
        matching_signals=("python", "testing"),
        reason_codes=("exact-tag-match",),
        actionability="install",
        install_descriptor_digest=descriptor.descriptor_digest,
        install_plan_digest=descriptor.plan_digest,
    )


def _request(
    descriptor: InstallPlanDescriptor,
    policy: InstallConsentPolicy,
) -> HostAction:
    selection = _selection(descriptor)
    return HostAction(
        action_id="request-consent-1",
        kind="RequestConsent",
        scope=ScopeRef(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            repository_id="repository-1",
            session_id="session-1",
            exposure_id="exposure-1",
            host_context_id="host-1",
        ),
        precondition_revision=3,
        entity_id=selection.capability_id,
        source_digest=selection.source_digest,
        plan_id="plan-1",
        catalog_snapshot_id=_digest("catalog"),
        consent_id="consent-1",
        required_host_feature="installation-consent",
        payload={
            "install_descriptor_digest": descriptor.descriptor_digest,
            "install_plan_digest": descriptor.plan_digest,
            "installer_id": "ctx-install-plan-executor-v1",
            "installer_digest": _digest("executor"),
            "policy_snapshot_digest": policy.policy_digest,
            "requested_action_id": "install-action-1",
            "requested_action_kind": "InstallCapability",
            "requested_action_content_digest": _digest("install-action"),
            "requested_action_precondition_revision": 4,
        },
    )


def _policy_for_kind(kind: str, mode: str) -> InstallConsentPolicy:
    modes = {
        "skill_mode": "ask-each-time",
        "agent_mode": "ask-each-time",
        "mcp_server_mode": "ask-each-time",
    }
    modes[
        {
            "skill": "skill_mode",
            "agent": "agent_mode",
            "mcp-server": "mcp_server_mode",
        }[kind]
    ] = mode
    return InstallConsentPolicy(**modes)  # type: ignore[arg-type]


def _v3_event(
    kind: str,
    revision: int,
    event_id: str,
    *,
    payload: Mapping[str, object] | None = None,
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=ScopeRef(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            repository_id="repository-1",
            session_id="session-v3",
            exposure_id="exposure-v3",
            host_context_id="host-v3",
        ),
        expected_revision=revision,
        occurred_at=NOW,
        payload={} if payload is None else payload,
        correlation_id="plan-v3",
        engine_version="engine-v3",
        planner_version="planner-v3",
        policy_version="policy-v3",
        host_descriptor_digest=_digest("v3-host"),
        catalog_snapshot_digest=_digest("v3-catalog"),
        semantic_model_digest=_digest("v3-model"),
        semantic_index_digest=_digest("v3-index"),
        work_signature=_digest("v3-work"),
        random_seed=17,
    )


def _v3_replay(
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


def _reducer_v3_consent_request(
    capability_id: str,
    policy: InstallConsentPolicy,
    *,
    permission_expansion: bool = False,
    credential_requirement: bool = False,
) -> tuple[HostAction, CapabilityPlanSelectionV3, InstallPlanDescriptor]:
    kind, name = capability_id.split(":", 1)
    source_digest = _digest(f"v3-entry:{capability_id}")
    material = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"v3-content:{capability_id}"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id=f"ctx-{kind}-installer-v1",
        plan_digest=_digest(f"v3-plan:{capability_id}"),
        provenance_digest=_digest(f"v3-provenance:{capability_id}"),
        permission_expansion=permission_expansion,
        credential_requirement=credential_requirement,
        result_material_identity_digest=material.identity_digest,
    )
    selection = CapabilityPlanSelectionV3(
        presentation=CapabilityCandidate(
            capability_id=capability_id,
            kind=kind,
            name=name,
            source_digest=source_digest,
            normalized_score_ppm=900_000,
            matching_signals=("python",),
            reason_codes=("signal-match",),
            actionability="install",
            install_descriptor_digest=descriptor.descriptor_digest,
            install_plan_digest=descriptor.plan_digest,
        ),
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=capability_id,
            kind=kind,
            catalog_namespace_digest=_digest("v3-catalog-namespace"),
        ),
        benefit=CapabilityBenefitProjection(
            tier="executable",
            individual_net_benefit_u=600_000,
            marginal_net_benefit_u=600_000,
        ),
        authority=InstallPlanningAuthority(
            descriptor=descriptor,
            result_material=material,
        ),
    )
    plan = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=3,
        value={
            "status": "ready",
            "abstention_code": None,
            "benefit_audit": {
                "result_schema_id": "ctx.benefit-selection-result-v1",
                "result_digest": _digest(f"v3-benefit-result:{capability_id}"),
                "policy_schema_id": "ctx.net-benefit-policy-v3",
                "policy_digest": _digest("v3-benefit-policy"),
                "selection_algorithm_id": "ctx.greedy-bounded-subset-exchange-v1",
                "calibration_digest": _digest("v3-calibration"),
                "requested_limit": 5,
                "candidate_pool_count": 1,
                "search_evaluation_count": 1,
            },
            "capabilities": [selection.to_mapping()],
        },
    )
    state, _ = reduce_replay_v3(
        None,
        _v3_replay(
            _v3_event(
                "SessionStarted",
                0,
                f"start-{kind}",
                payload={"host_level": "managing"},
            )
        ),
    )
    state, _ = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event("IntentObserved", 1, f"plan-{kind}"),
            decision=plan,
        ),
    )
    state, transition = reduce_replay_v3(
        state,
        _v3_replay(
            _v3_event(
                "ReassessmentRequested",
                2,
                f"desired-{kind}",
                payload={
                    "owner_id": "owner-1",
                    "policy_snapshot_digest": policy.policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability_id,
                            "source_digest": source_digest,
                            "lease_id": f"lease-{kind}",
                            "kind": kind,
                            "actionability": "install",
                            "install_descriptor_digest": descriptor.descriptor_digest,
                            "install_plan_digest": descriptor.plan_digest,
                        }
                    ],
                },
            )
        ),
    )
    requests = tuple(action for action in transition.actions if action.kind == "RequestConsent")
    assert len(requests) == 1
    assert isinstance(state.committed_plan, CommittedPlanV3)
    committed_selection = state.committed_plan.capabilities[0].selection
    return requests[0], committed_selection, descriptor


def test_descriptor_has_canonical_round_trip_and_rejects_digest_tamper() -> None:
    descriptor = _descriptor()

    assert InstallPlanDescriptor.from_dict(descriptor.to_dict()) == descriptor
    assert descriptor.recomputed_descriptor_digest == descriptor.descriptor_digest

    tampered = descriptor.to_dict()
    tampered["plan_digest"] = _digest("different-plan")
    with pytest.raises(ValueError, match="descriptor_digest"):
        InstallPlanDescriptor.from_dict(tampered)

    tampered_risk = descriptor.to_dict()
    tampered_risk["permission_expansion"] = True
    with pytest.raises(ValueError, match="descriptor_digest"):
        InstallPlanDescriptor.from_dict(tampered_risk)


def test_v1_descriptor_bytes_remain_frozen_without_result_material() -> None:
    descriptor = _descriptor()

    encoded = json.dumps(
        descriptor.to_dict(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )

    assert encoded == (
        '{"capability_id":"skill:python-testing","credential_requirement":false,'
        '"descriptor_digest":"2571612ff39e615a258f9632a346aae775c5d81afa754cf4095b559b40eaeaa6",'
        '"installer_id":"ctx-builtin-installer-v1","kind":"skill",'
        '"permission_expansion":false,'
        '"plan_digest":"64879f7d6b960a01909762d911a32d4582c20010c5641ee90278b644a9e3b525",'
        '"provenance_digest":"96d815328a42cb4ef89d5e0b7a1df6be43b484832c83a7b4596d8402c7c0b12b",'
        '"rollback_strategy":"atomic-restore",'
        '"schema":"ctx.install-plan-descriptor-v1","target_scope":"user"}'
    )
    assert InstallPlanDescriptor.from_dict(json.loads(encoded)).to_dict() == descriptor.to_dict()


def test_v2_descriptor_binds_exact_result_material_identity() -> None:
    first = InstallPlanDescriptor.create(
        capability_id="skill:python-testing",
        kind="skill",
        installer_id="ctx-builtin-installer-v1",
        plan_digest=_digest("plan"),
        provenance_digest=_digest("provenance"),
        result_material_identity_digest=_digest("material-one"),
    )
    second = InstallPlanDescriptor.create(
        capability_id="skill:python-testing",
        kind="skill",
        installer_id="ctx-builtin-installer-v1",
        plan_digest=_digest("plan"),
        provenance_digest=_digest("provenance"),
        result_material_identity_digest=_digest("material-two"),
    )

    assert first.schema_version == 2
    assert first.result_material_identity_digest == _digest("material-one")
    assert first.descriptor_digest != second.descriptor_digest
    assert InstallPlanDescriptor.from_dict(first.to_dict()) == first

    substituted = first.to_dict()
    assert second.result_material_identity_digest is not None
    substituted["result_material_identity_digest"] = second.result_material_identity_digest
    with pytest.raises(ValueError, match="descriptor_digest"):
        InstallPlanDescriptor.from_dict(substituted)

    policy = InstallConsentPolicy()
    prepared = _prepared(
        first,
        policy,
        result_material_identity_digest=first.result_material_identity_digest,
    )
    assert prepared.matches_descriptor(first)
    assert not replace(
        prepared,
        result_material_identity_digest=second.result_material_identity_digest,
    ).matches_descriptor(first)


@pytest.mark.parametrize(("capability_id", "kind"), INSTALLABLE_CAPABILITY_CASES)
def test_v2_result_material_contract_supports_every_installable_kind(
    capability_id: str,
    kind: str,
) -> None:
    material = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"content:{capability_id}"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id="ctx-builtin-installer-v1",
        plan_digest=_digest(f"plan:{capability_id}"),
        provenance_digest=_digest(f"provenance:{capability_id}"),
        result_material_identity_digest=material.identity_digest,
    )
    policy = InstallConsentPolicy()
    prepared = _prepared(
        descriptor,
        policy,
        result_material_identity_digest=material.identity_digest,
    )

    assert InstallPlanDescriptor.from_dict(descriptor.to_dict()) == descriptor
    assert (
        InstallPlanningBundle(
            descriptor=descriptor,
            result_material=material,
        ).result_material
        == material
    )
    assert prepared.matches_descriptor(descriptor)


@pytest.mark.parametrize(
    ("descriptor_capability_id", "descriptor_kind", "material_capability_id", "material_kind"),
    [
        ("skill:python-testing", "skill", "agent:reviewer", "agent"),
        ("agent:reviewer", "agent", "mcp-server:repository-tools", "mcp-server"),
        ("mcp-server:repository-tools", "mcp-server", "skill:python-testing", "skill"),
    ],
)
def test_v2_bundle_rejects_cross_kind_result_material_substitution(
    descriptor_capability_id: str,
    descriptor_kind: str,
    material_capability_id: str,
    material_kind: str,
) -> None:
    substituted_material = MaterialIdentity.create(
        capability_id=material_capability_id,
        kind=material_kind,
        content_sha256=_digest(f"content:{material_capability_id}"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=descriptor_capability_id,
        kind=descriptor_kind,
        installer_id="ctx-builtin-installer-v1",
        plan_digest=_digest(f"plan:{descriptor_capability_id}"),
        provenance_digest=_digest(f"provenance:{descriptor_capability_id}"),
        result_material_identity_digest=substituted_material.identity_digest,
    )

    assert not descriptor.matches_result_material(substituted_material)
    with pytest.raises(ValueError, match="result material"):
        InstallPlanningBundle(
            descriptor=descriptor,
            result_material=substituted_material,
        )


def test_v2_descriptor_matches_only_the_exact_typed_result_material() -> None:
    material = MaterialIdentity.create(
        capability_id="skill:python-testing",
        kind="skill",
        content_sha256=_digest("content"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=material.capability_id,
        kind=material.kind,
        installer_id="ctx-builtin-installer-v1",
        plan_digest=_digest("plan"),
        provenance_digest=_digest("provenance"),
        result_material_identity_digest=material.identity_digest,
    )

    assert descriptor.matches_result_material(material)
    assert not descriptor.matches_result_material(
        MaterialIdentity.create(
            capability_id="skill:other-testing",
            kind="skill",
            content_sha256=material.content_sha256,
            content_bytes=material.content_bytes,
        )
    )


def test_install_planning_bundle_requires_exact_v2_descriptor_and_full_material() -> None:
    material = MaterialIdentity.create(
        capability_id="skill:python-testing",
        kind="skill",
        content_sha256=_digest("installed-content"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=material.capability_id,
        kind=material.kind,
        installer_id="ctx-builtin-installer-v1",
        plan_digest=_digest("install-plan-v2"),
        provenance_digest=_digest("install-catalog-v2"),
        result_material_identity_digest=material.identity_digest,
    )

    bundle = InstallPlanningBundle(descriptor=descriptor, result_material=material)

    assert bundle.descriptor == descriptor
    assert bundle.result_material == material

    with pytest.raises(ValueError, match="schema v2"):
        InstallPlanningBundle(descriptor=_descriptor(), result_material=material)
    substituted = MaterialIdentity.create(
        capability_id=material.capability_id,
        kind=material.kind,
        content_sha256=_digest("substituted-content"),
        content_bytes=material.content_bytes,
    )
    with pytest.raises(ValueError, match="result material"):
        InstallPlanningBundle(descriptor=descriptor, result_material=substituted)


def test_descriptor_risk_declarations_are_strict_booleans() -> None:
    with pytest.raises(ValueError, match="permission_expansion"):
        InstallPlanDescriptor.create(
            capability_id="skill:python-testing",
            kind="skill",
            installer_id="ctx-installer-v1",
            plan_digest=_digest("plan"),
            provenance_digest=_digest("provenance"),
            permission_expansion=1,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("capability_id", "kind"),
    [
        ("harness:pytest", "harness"),
        ("agent:reviewer", "skill"),
        ("skill:reviewer", "agent"),
        ("skill:", "skill"),
        ("skill:reviewer:extra", "skill"),
    ],
)
def test_descriptor_rejects_unsupported_or_inconsistent_kind(
    capability_id: str,
    kind: str,
) -> None:
    with pytest.raises(ValueError, match="kind"):
        _descriptor(capability_id=capability_id, kind=kind)


@pytest.mark.parametrize(
    ("field_name", "unsafe_value"),
    [
        ("capability_id", "skill:python testing"),
        ("capability_id", "skill:$(touch-pwned)"),
        ("installer_id", "/usr/local/bin/installer"),
        ("installer_id", "pip install package"),
        ("installer_id", "token=${SECRET}"),
    ],
)
def test_descriptor_rejects_unsafe_tokens(field_name: str, unsafe_value: str) -> None:
    values: dict[str, str] = {
        "capability_id": "skill:python-testing",
        "kind": "skill",
        "installer_id": "ctx-installer-v1",
        "plan_digest": _digest("plan"),
        "provenance_digest": _digest("provenance"),
    }
    values[field_name] = unsafe_value

    with pytest.raises(ValueError, match=field_name):
        InstallPlanDescriptor.create(
            capability_id=values["capability_id"],
            kind=values["kind"],
            installer_id=values["installer_id"],
            plan_digest=values["plan_digest"],
            provenance_digest=values["provenance_digest"],
        )


def test_descriptor_rejects_non_user_scope_and_non_atomic_rollback() -> None:
    with pytest.raises(ValueError, match="target_scope"):
        InstallPlanDescriptor.create(
            capability_id="skill:python-testing",
            kind="skill",
            installer_id="ctx-installer-v1",
            plan_digest=_digest("plan"),
            provenance_digest=_digest("provenance"),
            target_scope="system",
        )
    with pytest.raises(ValueError, match="rollback_strategy"):
        InstallPlanDescriptor.create(
            capability_id="skill:python-testing",
            kind="skill",
            installer_id="ctx-installer-v1",
            plan_digest=_digest("plan"),
            provenance_digest=_digest("provenance"),
            rollback_strategy="none",
        )


def test_safe_default_asks_for_each_kind_and_has_canonical_digest() -> None:
    policy = InstallConsentPolicy.safe_default()

    assert policy.mode_for("skill") == "ask-each-time"
    assert policy.mode_for("agent") == "ask-each-time"
    assert policy.mode_for("mcp-server") == "ask-each-time"
    assert InstallConsentPolicy.from_dict(policy.to_dict()) == policy

    tampered = policy.to_dict()
    tampered["skill_mode"] = "preapproved-auto"
    with pytest.raises(ValueError, match="policy_digest"):
        InstallConsentPolicy.from_dict(tampered)


def test_policy_modes_are_independent_by_capability_kind() -> None:
    policy = InstallConsentPolicy(
        skill_mode="preapproved-auto",
        agent_mode="ask-each-time",
        mcp_server_mode="preapproved-auto",
    )

    assert policy.mode_for("skill") == "preapproved-auto"
    assert policy.mode_for("agent") == "ask-each-time"
    assert policy.mode_for("mcp-server") == "preapproved-auto"


def test_clean_exact_plan_uses_configured_decision_basis() -> None:
    descriptor = _descriptor()
    auto = InstallConsentPolicy(skill_mode="preapproved-auto")
    ask = InstallConsentPolicy()

    automatic = route_install_authorization(descriptor=descriptor, policy=auto)
    interactive = route_install_authorization(descriptor=descriptor, policy=ask)

    assert automatic.decision_basis == "preapproved-policy"
    assert automatic.policy_snapshot_digest == auto.policy_digest
    assert automatic.is_preapproved
    assert interactive.decision_basis == "interactive"
    assert interactive.policy_snapshot_digest == ask.policy_digest
    assert not interactive.is_preapproved


def test_consent_dispatcher_auto_grants_only_a_current_preapproved_request() -> None:
    descriptor = _descriptor()
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    request = _request(descriptor, policy)

    directive = route_install_consent_request(
        request,
        _selection(descriptor),
        descriptor,
        policy,
    )

    assert not directive.requires_prompt
    assert directive.permission_expansion is False
    assert directive.credential_requirement is False
    assert directive.provenance_digest == descriptor.provenance_digest
    assert directive.automatic_grant_payload() == {
        "consent_id": "consent-1",
        "decision": "granted",
        "decision_basis": "preapproved-policy",
        "policy_snapshot_digest": policy.policy_digest,
        "requested_action_id": "install-action-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": _digest("install-action"),
        "requested_action_precondition_revision": 4,
    }

    changed = InstallConsentPolicy()
    with pytest.raises(InstallConsentRoutingError, match="current policy"):
        route_install_consent_request(
            request,
            _selection(descriptor),
            descriptor,
            changed,
        )

    substituted = _descriptor(permission_expansion=True)
    assert substituted.plan_digest == descriptor.plan_digest
    assert substituted.descriptor_digest != descriptor.descriptor_digest
    with pytest.raises(InstallConsentRoutingError, match="current policy"):
        route_install_consent_request(
            request,
            _selection(descriptor),
            substituted,
            policy,
        )


def test_consent_dispatcher_preserves_independent_kind_policy_and_risk_ui() -> None:
    descriptor = _descriptor(
        capability_id="agent:python-reviewer",
        kind="agent",
        credential_requirement=True,
    )
    policy = InstallConsentPolicy(
        skill_mode="preapproved-auto",
        agent_mode="ask-each-time",
    )

    directive = route_install_consent_request(
        _request(descriptor, policy),
        _selection(descriptor),
        descriptor,
        policy,
    )

    assert directive.requires_prompt
    assert directive.credential_requirement is True
    assert directive.reason_code == "credentials-require-consent"
    assert directive.automatic_grant_payload() is None
    assert directive.decision_payload("denied")["decision_basis"] == "interactive"


@pytest.mark.parametrize(("capability_id", "kind"), INSTALLABLE_CAPABILITY_CASES)
@pytest.mark.parametrize(
    ("mode", "expected_basis"),
    [
        ("preapproved-auto", "preapproved-policy"),
        ("ask-each-time", "interactive"),
    ],
)
def test_consent_dispatcher_routes_real_reducer_v3_requests_for_every_installable_kind(
    capability_id: str,
    kind: str,
    mode: str,
    expected_basis: str,
) -> None:
    policy = _policy_for_kind(kind, mode)
    request, selection, descriptor = _reducer_v3_consent_request(capability_id, policy)

    directive = route_install_consent_request(request, selection, descriptor, policy)
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)

    assert directive.capability_id == capability_id
    assert directive.kind == kind
    assert directive.decision_basis == expected_basis
    assert directive.requires_prompt is (mode == "ask-each-time")
    assert directive.result_material_identity_digest == authority.result_material.identity_digest
    assert directive.decision_payload("granted") == {
        "consent_id": request.consent_id,
        "decision": "granted",
        "decision_basis": expected_basis,
        "policy_snapshot_digest": policy.policy_digest,
        "requested_action_id": request.payload["requested_action_id"],
        "requested_action_kind": request.payload["requested_action_kind"],
        "requested_action_content_digest": request.payload["requested_action_content_digest"],
        "requested_action_precondition_revision": request.payload[
            "requested_action_precondition_revision"
        ],
    }
    if mode == "preapproved-auto":
        assert directive.automatic_grant_payload() == directive.decision_payload("granted")
    else:
        assert directive.automatic_grant_payload() is None


@pytest.mark.parametrize(
    ("permission_expansion", "credential_requirement", "reason_code"),
    [
        (True, False, "permission-expansion-requires-consent"),
        (False, True, "credentials-require-consent"),
    ],
)
def test_reducer_v3_risk_declarations_override_per_kind_preapproval(
    permission_expansion: bool,
    credential_requirement: bool,
    reason_code: str,
) -> None:
    policy = InstallConsentPolicy(mcp_server_mode="preapproved-auto")
    request, selection, descriptor = _reducer_v3_consent_request(
        "mcp-server:repository-tools",
        policy,
        permission_expansion=permission_expansion,
        credential_requirement=credential_requirement,
    )

    directive = route_install_consent_request(request, selection, descriptor, policy)

    assert directive.requires_prompt
    assert directive.reason_code == reason_code
    assert directive.automatic_grant_payload() is None


def test_reducer_v3_consent_dispatcher_rejects_cross_kind_selection_substitution() -> None:
    policy = InstallConsentPolicy.safe_default()
    request, _selection_value, _descriptor_value = _reducer_v3_consent_request(
        "skill:python-testing",
        policy,
    )
    _other_request, other_selection, other_descriptor = _reducer_v3_consent_request(
        "agent:reviewer",
        policy,
    )

    with pytest.raises(InstallConsentRoutingError, match="install identity"):
        route_install_consent_request(request, other_selection, other_descriptor, policy)


def test_reducer_v3_consent_dispatcher_rejects_catalog_substitution() -> None:
    policy = InstallConsentPolicy.safe_default()
    request, selection, descriptor = _reducer_v3_consent_request(
        "skill:python-testing",
        policy,
    )
    substituted = replace(
        selection,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=selection.presentation.capability_id,
            kind=selection.presentation.kind,
            catalog_namespace_digest=_digest("substituted-catalog-namespace"),
        ),
    )

    with pytest.raises(InstallConsentRoutingError, match="install identity"):
        route_install_consent_request(request, substituted, descriptor, policy)


@pytest.mark.parametrize("substitution", ["descriptor", "material"])
def test_reducer_v3_consent_dispatcher_rejects_descriptor_and_material_substitution(
    substitution: str,
) -> None:
    policy = InstallConsentPolicy.safe_default()
    request, selection, descriptor = _reducer_v3_consent_request(
        "skill:python-testing",
        policy,
    )
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    material = authority.result_material
    if substitution == "material":
        material = MaterialIdentity.create(
            capability_id=material.capability_id,
            kind=material.kind,
            content_sha256=_digest("substituted-result-material"),
            content_bytes=material.content_bytes,
        )
    substituted_descriptor = InstallPlanDescriptor.create(
        capability_id=descriptor.capability_id,
        kind=descriptor.kind,
        installer_id=descriptor.installer_id,
        plan_digest=(
            _digest("substituted-install-plan")
            if substitution == "descriptor"
            else descriptor.plan_digest
        ),
        provenance_digest=descriptor.provenance_digest,
        permission_expansion=descriptor.permission_expansion,
        credential_requirement=descriptor.credential_requirement,
        result_material_identity_digest=material.identity_digest,
    )
    substituted_selection = replace(
        selection,
        presentation=replace(
            selection.presentation,
            install_descriptor_digest=substituted_descriptor.descriptor_digest,
            install_plan_digest=substituted_descriptor.plan_digest,
        ),
        authority=InstallPlanningAuthority(
            descriptor=substituted_descriptor,
            result_material=material,
        ),
    )

    with pytest.raises(InstallConsentRoutingError, match="install identity"):
        route_install_consent_request(
            request,
            substituted_selection,
            substituted_descriptor,
            policy,
        )


def test_reducer_v3_consent_dispatcher_rejects_current_policy_substitution() -> None:
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    request, selection, descriptor = _reducer_v3_consent_request(
        "skill:python-testing",
        policy,
    )

    with pytest.raises(InstallConsentRoutingError, match="current policy"):
        route_install_consent_request(
            request,
            selection,
            descriptor,
            InstallConsentPolicy.safe_default(),
        )


@pytest.mark.parametrize(
    ("permission_expansion", "credential_requirement", "reason_code"),
    [
        (True, False, "permission-expansion-requires-consent"),
        (False, True, "credentials-require-consent"),
    ],
)
def test_risky_descriptors_never_inherit_automatic_preapproval_before_prepare(
    permission_expansion: bool,
    credential_requirement: bool,
    reason_code: str,
) -> None:
    descriptor = _descriptor(
        permission_expansion=permission_expansion,
        credential_requirement=credential_requirement,
    )
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")

    decision = route_install_authorization(descriptor=descriptor, policy=policy)

    assert decision.decision_basis == "interactive"
    assert decision.authorization_eligible
    assert decision.reason_code == reason_code
    assert not decision.is_preapproved


def test_prepared_plan_must_match_authenticated_descriptor_risk_declarations() -> None:
    descriptor = _descriptor(permission_expansion=True)
    policy = InstallConsentPolicy()
    prepared = _prepared(descriptor, policy)
    decision = route_install_authorization(policy, descriptor)

    assert prepared.matches_descriptor(descriptor)
    assert prepared.matches_authorization(descriptor, decision)
    assert not replace(prepared, permission_expansion=False).matches_descriptor(descriptor)
    assert not replace(prepared, credential_requirement=True).matches_descriptor(descriptor)
    assert not replace(
        prepared,
        consent_policy_digest=_digest("stale-policy"),
    ).matches_authorization(descriptor, decision)


def test_uninstall_cannot_be_prepared_through_the_install_plan_contract() -> None:
    descriptor = _descriptor()
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")

    with pytest.raises(ValueError, match="operation must be install"):
        _prepared(descriptor, policy, operation="uninstall")


def test_prepared_plan_is_exactly_action_selection_catalog_and_plan_bound() -> None:
    descriptor = _descriptor()
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    prepared = _prepared(descriptor, policy)

    assert prepared.action_id == "install-action-1"
    assert prepared.action_content_digest == _digest("action")
    assert prepared.selection_source_digest == _digest("selection")
    assert prepared.catalog_snapshot_digest == _digest("catalog")
    assert prepared.plan_digest == descriptor.plan_digest
    assert prepared.descriptor_digest == descriptor.descriptor_digest


def test_durable_projections_cannot_accept_raw_commands_paths_or_secrets() -> None:
    descriptor = _descriptor()
    policy = InstallConsentPolicy()
    prepared = _prepared(descriptor, policy)

    forbidden_names = {"command", "shell", "path", "secret", "credential", "token_value"}
    assert forbidden_names.isdisjoint({item.name for item in fields(InstallPlanDescriptor)})
    assert forbidden_names.isdisjoint({item.name for item in fields(PreparedInstallPlan)})
    assert forbidden_names.isdisjoint(descriptor.to_dict())
    assert forbidden_names.isdisjoint(prepared.to_dict())
    assert "execution_token" not in prepared.to_dict()

    raw = descriptor.to_dict()
    raw["command"] = "pip install unsafe"
    with pytest.raises(ValueError, match="unknown fields"):
        InstallPlanDescriptor.from_dict(raw)


@pytest.mark.parametrize("unsafe_token", ["/tmp/plan", "C:\\tmp\\plan", "$(env)", "has secret"])
def test_prepared_plan_rejects_path_shell_and_secret_shaped_execution_tokens(
    unsafe_token: str,
) -> None:
    descriptor = _descriptor()
    policy = InstallConsentPolicy()

    with pytest.raises(ValueError, match="execution_token"):
        _prepared(descriptor, policy, execution_token=unsafe_token)
