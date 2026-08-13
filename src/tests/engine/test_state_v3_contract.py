"""Strict schema-v3 state codec and historical-state compatibility contracts."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ctx.engine.benefit import ABSTENTION_CODES
from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity, InstalledMaterialLineage
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import (
    BenefitAuditReference,
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
    LoadPlanningAuthority,
    ManualPlanningAuthority,
    PlanningAuthority,
)
from ctx.engine.protocol import (
    INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    HostAction,
    ScopeRef,
)
from ctx.engine.state import (
    CapabilityState,
    CapabilityStateV3,
    CommittedPlanV3,
    EngineState,
    LeaseRef,
    PendingConsent,
    PendingEffect,
    PlanCapabilityV3,
    StateValidationError,
)


def _digest(character: str) -> str:
    return character * 64


def _scope(*, exposure_id: str = "exposure-a") -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-a",
        workspace_id="workspace-a",
        repository_id="repo-a",
        session_id="session-a",
        exposure_id=exposure_id,
        host_context_id="codex-a",
    )


def _lease(index: int = 1) -> LeaseRef:
    return LeaseRef(
        lease_id=f"lease-{index}",
        owner_id=f"owner-{index}",
        exposure_id="exposure-a",
    )


def _catalog_identity(name: str = "python-debugger") -> CatalogCapabilityIdentity:
    return CatalogCapabilityIdentity.create(
        capability_id=f"skill:{name}",
        kind="skill",
        catalog_namespace_digest=_digest("1"),
    )


def _material_identity(name: str = "python-debugger") -> MaterialIdentity:
    return MaterialIdentity.create(
        capability_id=f"skill:{name}",
        kind="skill",
        content_sha256=_digest("2"),
        content_bytes=120,
    )


def _catalog_material(name: str = "python-debugger") -> AuthorizedMaterial:
    identity = _material_identity(name)
    descriptor = MaterialDescriptor.create(
        capability_id=identity.capability_id,
        kind=identity.kind,
        actionability="load",
        content_sha256=identity.content_sha256,
        content_bytes=identity.content_bytes,
        estimated_tokens=30,
        provenance_digest=_digest("3"),
        material_identity_digest=identity.identity_digest,
    )
    return AuthorizedMaterial.from_catalog(
        catalog_identity_digest=_catalog_identity(name).identity_digest,
        descriptor=descriptor,
    )


def _install_descriptor(name: str = "python-debugger") -> InstallPlanDescriptor:
    return InstallPlanDescriptor.create(
        capability_id=f"skill:{name}",
        kind="skill",
        installer_id="ctx-install-actuator-v1",
        plan_digest=_digest("4"),
        provenance_digest=_digest("5"),
        result_material_identity_digest=_material_identity(name).identity_digest,
    )


def _installed_material(name: str = "python-debugger") -> AuthorizedMaterial:
    lineage = InstalledMaterialLineage.create(
        capability_id=f"skill:{name}",
        kind="skill",
        catalog_identity_digest=_catalog_identity(name).identity_digest,
        material_identity_digest=_material_identity(name).identity_digest,
        origin_install_descriptor_digest=_install_descriptor(name).descriptor_digest,
        install_action_content_digest=_digest("6"),
        install_receipt_content_digest=_digest("7"),
    )
    return AuthorizedMaterial.from_installed(lineage)


def _presentation(name: str, actionability: str) -> CapabilityCandidate:
    install = _install_descriptor(name) if actionability == "install" else None
    return CapabilityCandidate(
        capability_id=f"skill:{name}",
        kind="skill",
        name=name,
        source_digest=_digest("8"),
        normalized_score_ppm=800_000,
        matching_signals=("python",),
        reason_codes=("relevant",),
        actionability=actionability,
        install_descriptor_digest=None if install is None else install.descriptor_digest,
        install_plan_digest=None if install is None else install.plan_digest,
    )


def _plan_capability(
    actionability: str = "load",
    *,
    name: str = "python-debugger",
) -> PlanCapabilityV3:
    authority: PlanningAuthority
    if actionability == "load":
        authority = LoadPlanningAuthority(material=_catalog_material(name))
    elif actionability == "install":
        authority = InstallPlanningAuthority(
            descriptor=_install_descriptor(name),
            result_material=_material_identity(name),
        )
    else:
        authority = ManualPlanningAuthority()
    tier = "advisory" if actionability == "manual" else "executable"
    return PlanCapabilityV3(
        selection=CapabilityPlanSelectionV3(
            presentation=_presentation(name, actionability),
            catalog_identity=_catalog_identity(name),
            benefit=CapabilityBenefitProjection(
                tier=tier,
                individual_net_benefit_u=100,
                marginal_net_benefit_u=50,
            ),
            authority=authority,
        )
    )


def _audit(
    *,
    requested_limit: int = 5,
    candidate_pool_count: int = 1,
    search_evaluation_count: int = 5,
) -> BenefitAuditReference:
    return BenefitAuditReference(
        result_schema_id="ctx.benefit-selection-result-v1",
        result_digest=_digest("a"),
        policy_schema_id="ctx.net-benefit-policy-v3",
        policy_digest=_digest("b"),
        selection_algorithm_id="ctx.greedy-bounded-subset-exchange-v1",
        calibration_digest=_digest("c"),
        requested_limit=requested_limit,
        candidate_pool_count=candidate_pool_count,
        search_evaluation_count=search_evaluation_count,
    )


def _committed_plan(*rows: PlanCapabilityV3) -> CommittedPlanV3:
    return CommittedPlanV3(
        plan_id="plan-a",
        catalog_snapshot_id=_digest("d"),
        decision_digest=_digest("e"),
        status="ready",
        abstention_code=None,
        benefit_audit=_audit(candidate_pool_count=max(1, len(rows))),
        capabilities=rows,
    )


def _capability_state(
    selection: PlanCapabilityV3,
    *,
    installation: str,
    current_authorized_material: AuthorizedMaterial | None,
    active: bool = False,
    rollback_held: bool = False,
    leases: tuple[LeaseRef, ...] | None = None,
    plan_id: str = "plan-a",
    catalog_snapshot_id: str | None = None,
) -> CapabilityStateV3:
    return CapabilityStateV3(
        selection=selection,
        material_identity=_material_identity(selection.name),
        current_authorized_material=current_authorized_material,
        installation=installation,
        plan_id=plan_id,
        catalog_snapshot_id=(_digest("d") if catalog_snapshot_id is None else catalog_snapshot_id),
        leases=(_lease(),) if leases is None else leases,
        activation="active" if active else "inactive",
        activation_lease_id="lease-1" if active else None,
        rollback_held=rollback_held,
        rollback_owner_id="replacement-owner" if rollback_held else None,
    )


def _state(
    plan: CommittedPlanV3,
    *capabilities: CapabilityStateV3,
    pending_effects: tuple[PendingEffect, ...] = (),
    pending_consents: tuple[PendingConsent, ...] = (),
    last_manual_bundle: tuple[str, ...] = (),
    rollback_requested_capability_ids: tuple[str, ...] = (),
    blocked_install_descriptor_digests: tuple[str, ...] = (),
) -> EngineState:
    return EngineState(
        revision=6,
        scope=_scope(),
        host_level="managing",
        host_descriptor_digest=_digest("f"),
        capabilities=capabilities,
        pending_effects=pending_effects,
        pending_consents=pending_consents,
        committed_plan=plan,
        last_manual_bundle=last_manual_bundle,
        rollback_requested_capability_ids=rollback_requested_capability_ids,
        blocked_install_descriptor_digests=blocked_install_descriptor_digests,
        install_policy_snapshot_digest=_digest("9"),
        _contract_version=3,
    )


def _install_action(capability: CapabilityStateV3) -> HostAction:
    authority = capability.selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    return HostAction(
        action_id="install-1",
        kind="InstallCapability",
        scope=_scope(),
        precondition_revision=6,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        consent_id="consent-1",
        required_host_feature="installation",
        payload={
            "schema": INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
            "capability_kind": capability.kind,
            "catalog_identity": capability.catalog_identity.to_dict(),
            "result_material": capability.material_identity.to_dict(),
            "install_plan_descriptor": authority.descriptor.to_dict(),
            "installer_digest": _digest("0"),
            "policy_snapshot_digest": _digest("9"),
        },
        verification={
            "receipt_required": True,
            "expected_state": "installed",
            "receipt_schema": INSTALL_RECEIPT_SCHEMA_V3,
        },
        rollback={
            "kind": "UninstallCapability",
            "installer_id": authority.descriptor.installer_id,
        },
    )


def _material_action(
    capability: CapabilityStateV3,
    *,
    kind: str = "ActivateCapability",
    action_id: str = "activate-1",
    lease_id: str = "lease-1",
    exposure_id: str = "exposure-a",
) -> HostAction:
    assert capability.current_authorized_material is not None
    expected_state = {
        "ActivateCapability": "active",
        "PrepareExposure": "prepared",
        "DeactivateCapability": "inactive",
    }[kind]
    rollback = {
        "ActivateCapability": {"kind": "DeactivateCapability"},
        "PrepareExposure": {
            "kind": "cleanup-prepared-exposure",
            "exposure_id": exposure_id,
        },
        "DeactivateCapability": {
            "kind": "ActivateCapability",
            "source_digest": capability.source_digest,
        },
    }[kind]
    return HostAction(
        action_id=action_id,
        kind=kind,
        scope=_scope(exposure_id=exposure_id),
        precondition_revision=6,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        lease_id=lease_id,
        expires_at="2026-08-02T12:00:00Z",
        required_host_feature="activation",
        payload={
            "schema": MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
            "capability_kind": capability.kind,
            "catalog_identity": capability.catalog_identity.to_dict(),
            "material_identity": capability.material_identity.to_dict(),
            "authorized_material": capability.current_authorized_material.to_dict(),
        },
        verification={
            "receipt_required": True,
            "expected_state": expected_state,
            "receipt_schema": MATERIAL_RECEIPT_SCHEMA_V3,
        },
        rollback=rollback,
    )


def test_v3_load_state_round_trips_exact_plan_row_material_and_pending_action() -> None:
    row = _plan_capability("load")
    capability = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_catalog_material(),
    )
    pending = PendingEffect(action=_material_action(capability), effect="activate")
    state = _state(_committed_plan(row), capability, pending_effects=(pending,))

    encoded = state.to_json()
    decoded = EngineState.from_json(encoded)

    assert decoded == state
    assert decoded.to_json() == encoded
    assert decoded.to_dict()["state_schema"] == "ctx.engine-state-v3"
    decoded_capability = decoded.capabilities[0]
    assert isinstance(decoded_capability, CapabilityStateV3)
    assert decoded_capability.selection == row
    assert decoded_capability.current_authorized_material == _catalog_material()
    assert decoded_capability.material_identity == _material_identity()


def test_v3_absent_install_decision_and_receipt_promoted_material_round_trip() -> None:
    row = _plan_capability("install")
    absent = _capability_state(
        row,
        installation="absent",
        current_authorized_material=None,
    )
    promoted = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_installed_material(),
    )

    for capability in (absent, promoted):
        state = _state(_committed_plan(row), capability)
        decoded = EngineState.from_json(state.to_json())
        assert decoded == state

    assert absent.installation == "absent"
    assert absent.actionability == "install"
    assert promoted.installation == "installed"
    assert promoted.actionability == "install"
    assert promoted.installed_lineage == _installed_material().installed_material_lineage


def test_v3_pending_install_and_consent_persist_the_exact_precomputed_action() -> None:
    row = _plan_capability("install")
    capability = _capability_state(
        row,
        installation="absent",
        current_authorized_material=None,
    )
    action = _install_action(capability)

    awaiting_receipt = _state(
        _committed_plan(row),
        capability,
        pending_effects=(PendingEffect(action=action, effect="install"),),
    )
    awaiting_consent = _state(
        _committed_plan(row),
        capability,
        pending_consents=(PendingConsent(consent_id="consent-1", install_action=action),),
    )

    for state in (awaiting_receipt, awaiting_consent):
        assert EngineState.from_json(state.to_json()) == state


def test_v3_pending_install_rejects_another_valid_descriptor_and_material() -> None:
    row = _plan_capability("install")
    capability = _capability_state(
        row,
        installation="absent",
        current_authorized_material=None,
    )
    action = _install_action(capability)
    other_material = MaterialIdentity.create(
        capability_id=capability.capability_id,
        kind=capability.kind,
        content_sha256=_digest("a"),
        content_bytes=121,
    )
    other_descriptor = InstallPlanDescriptor.create(
        capability_id=capability.capability_id,
        kind=capability.kind,
        installer_id="ctx-install-actuator-v1",
        plan_digest=_digest("b"),
        provenance_digest=_digest("c"),
        result_material_identity_digest=other_material.identity_digest,
    )
    values = action.to_dict()
    values["payload"]["result_material"] = other_material.to_dict()
    values["payload"]["install_plan_descriptor"] = other_descriptor.to_dict()
    substituted = HostAction.from_dict(values)

    with pytest.raises(StateValidationError, match="install.*authority|runtime capability"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(PendingEffect(action=substituted, effect="install"),),
        )


def test_manual_selection_is_committed_but_never_creates_lifecycle_state() -> None:
    manual = _plan_capability("manual", name="manual-advice")
    state = _state(
        _committed_plan(manual),
        last_manual_bundle=(manual.capability_id,),
    )

    assert EngineState.from_json(state.to_json()) == state
    assert state.capabilities == ()

    with pytest.raises(StateValidationError, match="manual"):
        _capability_state(
            manual,
            installation="installed",
            current_authorized_material=_catalog_material("manual-advice"),
        )


@pytest.mark.parametrize(
    ("actionability", "installation", "current_authorized_material", "match"),
    [
        ("install", "absent", None, "material_identity"),
        ("load", "installed", None, "current_authorized_material"),
    ],
)
def test_v3_capability_rejects_install_without_material_or_load_without_authority(
    actionability: str,
    installation: str,
    current_authorized_material: AuthorizedMaterial | None,
    match: str,
) -> None:
    row = _plan_capability(actionability)
    values = {
        "selection": row,
        "material_identity": None if actionability == "install" else _material_identity(),
        "current_authorized_material": current_authorized_material,
        "installation": installation,
        "plan_id": "plan-a",
        "catalog_snapshot_id": _digest("d"),
        "leases": (_lease(),),
    }
    with pytest.raises(StateValidationError, match=match):
        CapabilityStateV3(**values)  # type: ignore[arg-type]


def test_v3_rejects_substituted_catalog_material_benefit_and_lineage() -> None:
    row = _plan_capability("load")
    capability = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_catalog_material(),
    )
    state = _state(_committed_plan(row), capability)

    invalid = state.to_dict()
    invalid["committed_plan"]["capabilities"][0]["benefit"]["marginal_net_benefit_u"] = 0
    with pytest.raises(StateValidationError, match="benefit|marginal"):
        EngineState.from_dict(invalid)

    invalid = state.to_dict()
    invalid["capabilities"][0]["material_identity"]["content_bytes"] = 121
    with pytest.raises(StateValidationError, match="material_identity"):
        EngineState.from_dict(invalid)

    install = _plan_capability("install")
    promoted = _state(
        _committed_plan(install),
        _capability_state(
            install,
            installation="installed",
            current_authorized_material=_installed_material(),
        ),
    )
    invalid = promoted.to_dict()
    invalid["capabilities"][0]["current_authorized_material"]["installed_material_lineage"][
        "lineage_digest"
    ] = _digest("0")
    with pytest.raises(StateValidationError, match="lineage"):
        EngineState.from_dict(invalid)

    invalid = state.to_dict()
    invalid["committed_plan"]["capabilities"][0]["catalog_identity"]["catalog_namespace_digest"] = (
        _digest("0")
    )
    with pytest.raises(StateValidationError, match="catalog identity|identity_digest"):
        EngineState.from_dict(invalid)


def test_v3_rejects_more_than_five_rows_unknown_fields_and_state_plan_mismatch() -> None:
    rows = tuple(_plan_capability("load", name=f"cap-{index}") for index in range(6))
    with pytest.raises(StateValidationError, match="five"):
        _committed_plan(*rows)

    row = _plan_capability("load")
    state = _state(
        _committed_plan(row),
        _capability_state(
            row,
            installation="installed",
            current_authorized_material=_catalog_material(),
        ),
    )
    invalid = state.to_dict()
    invalid["surprise"] = True
    with pytest.raises(StateValidationError, match="unknown"):
        EngineState.from_dict(invalid)

    invalid = state.to_dict()
    invalid["capabilities"][0]["selection"]["capability_id"] = "skill:other"
    with pytest.raises(StateValidationError, match="selection|committed|identity"):
        EngineState.from_dict(invalid)


def test_v3_rejects_the_untagged_unreleased_draft_projection() -> None:
    row = _plan_capability("load")
    state = _state(
        _committed_plan(row),
        _capability_state(
            row,
            installation="installed",
            current_authorized_material=_catalog_material(),
        ),
    )
    untagged = state.to_dict()
    untagged.pop("state_schema")

    with pytest.raises(StateValidationError, match="complete supported schema"):
        EngineState.from_dict(untagged)


def test_v3_abstained_plan_persists_exact_nine_field_audit_without_rows() -> None:
    for code in ABSTENTION_CODES:
        plan = CommittedPlanV3(
            plan_id="plan-a",
            catalog_snapshot_id=_digest("d"),
            decision_digest=_digest("e"),
            status="abstained",
            abstention_code=code,
            benefit_audit=_audit(
                requested_limit=0 if code == "limit-zero" else 5,
                search_evaluation_count=(5 if code == "below-net-benefit" else 0),
            ),
            capabilities=(),
        )
        decoded = EngineState.from_json(_state(plan).to_json())
        assert decoded.committed_plan == plan
        assert set(plan.to_dict()["benefit_audit"]) == {
            "result_schema_id",
            "result_digest",
            "policy_schema_id",
            "policy_digest",
            "selection_algorithm_id",
            "calibration_digest",
            "requested_limit",
            "candidate_pool_count",
            "search_evaluation_count",
        }


@pytest.mark.parametrize(
    ("code", "audit"),
    [
        ("limit-zero", _audit(requested_limit=1, search_evaluation_count=0)),
        (
            "below-net-benefit",
            _audit(candidate_pool_count=0, search_evaluation_count=0),
        ),
        ("no-feasible-capability", _audit(search_evaluation_count=1)),
    ],
)
def test_v3_abstention_rejects_impossible_compact_audit_semantics(
    code: str,
    audit: BenefitAuditReference,
) -> None:
    with pytest.raises(StateValidationError, match="audit"):
        CommittedPlanV3(
            plan_id="plan-a",
            catalog_snapshot_id=_digest("d"),
            decision_digest=_digest("e"),
            status="abstained",
            abstention_code=code,
            benefit_audit=audit,
            capabilities=(),
        )


def test_historical_state_bytes_remain_untagged_and_stable() -> None:
    capability = CapabilityState(
        capability_id="skill:legacy",
        source_digest="legacy-source",
        plan_id="legacy-plan",
        catalog_snapshot_id="legacy-catalog",
        leases=(_lease(),),
    )
    state = EngineState(
        revision=1,
        scope=_scope(),
        host_level="query-only",
        host_descriptor_digest="legacy-host",
        capabilities=(capability,),
    )

    encoded = state.to_json()
    assert "state_schema" not in encoded
    assert "committed_plan" not in encoded
    assert EngineState.from_json(encoded).to_json() == encoded


def test_v3_pending_action_must_exactly_match_runtime_material() -> None:
    row = _plan_capability("load")
    capability = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_catalog_material(),
    )
    action = _material_action(capability)
    values = action.to_dict()
    values["payload"]["authorized_material"] = _installed_material().to_dict()
    substituted = HostAction.from_dict(values)

    with pytest.raises(StateValidationError, match="pending action.*material|authorized"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(PendingEffect(action=substituted, effect="activate"),),
        )


@pytest.mark.parametrize(
    "action",
    [
        lambda capability: _material_action(capability, lease_id="unowned-lease"),
        lambda capability: _material_action(capability, exposure_id="other-exposure"),
    ],
)
def test_v3_activation_requires_the_exact_current_lease_and_exposure(
    action: Callable[[CapabilityStateV3], HostAction],
) -> None:
    row = _plan_capability("load")
    capability = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_catalog_material(),
    )
    pending_action = action(capability)

    with pytest.raises(StateValidationError, match="activation.*lease"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(PendingEffect(action=pending_action, effect="activate"),),
        )


def test_v3_prepare_and_deactivate_require_effect_specific_lease_authority() -> None:
    row = _plan_capability("load")
    capability = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_catalog_material(),
        active=True,
    )
    prepare = _material_action(
        capability,
        kind="PrepareExposure",
        exposure_id="other-exposure",
    )
    deactivate = _material_action(
        capability,
        kind="DeactivateCapability",
        action_id="deactivate-1",
        lease_id="not-the-activation-lease",
    )

    with pytest.raises(StateValidationError, match="preparation.*lease|exact exposure"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(PendingEffect(action=prepare, effect="prepare"),),
        )
    with pytest.raises(StateValidationError, match="deactivation lease"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(PendingEffect(action=deactivate, effect="deactivate"),),
        )


def test_v3_rollback_activation_requires_and_accepts_exact_held_authority() -> None:
    row = _plan_capability("load")
    capability = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_catalog_material(),
        rollback_held=True,
    )
    rollback = _material_action(
        capability,
        action_id="rollback-activate-1",
        lease_id=f"rollback:{capability.capability_id}",
    )
    state = _state(
        _committed_plan(row),
        capability,
        pending_effects=(PendingEffect(action=rollback, effect="rollback-activate"),),
        rollback_requested_capability_ids=(capability.capability_id,),
    )

    assert EngineState.from_json(state.to_json()) == state

    with pytest.raises(StateValidationError, match="held rollback authority"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(
                PendingEffect(
                    action=_material_action(
                        capability,
                        action_id="rollback-wrong-lease",
                        lease_id="rollback:wrong",
                    ),
                    effect="rollback-activate",
                ),
            ),
            rollback_requested_capability_ids=(capability.capability_id,),
        )


def test_v3_rejects_multiple_pending_operations_for_one_capability() -> None:
    row = _plan_capability("load")
    capability = _capability_state(
        row,
        installation="installed",
        current_authorized_material=_catalog_material(),
    )
    first = _material_action(capability, action_id="activate-1")
    second = _material_action(capability, action_id="activate-2")

    with pytest.raises(StateValidationError, match="multiple pending lifecycle"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(
                PendingEffect(action=first, effect="activate"),
                PendingEffect(action=second, effect="activate"),
            ),
        )


def test_v3_rejects_install_waiting_for_consent_and_receipt_under_distinct_ids() -> None:
    row = _plan_capability("install")
    capability = _capability_state(
        row,
        installation="absent",
        current_authorized_material=None,
    )
    receipt_action = _install_action(capability)
    consent_values = receipt_action.to_dict()
    consent_values["action_id"] = "install-2"
    consent_values["consent_id"] = "consent-2"
    consent_action = HostAction.from_dict(consent_values)

    with pytest.raises(StateValidationError, match="consent and a pending effect"):
        _state(
            _committed_plan(row),
            capability,
            pending_effects=(PendingEffect(action=receipt_action, effect="install"),),
            pending_consents=(
                PendingConsent(consent_id="consent-2", install_action=consent_action),
            ),
        )


def test_v3_row_codec_rejects_unknown_authority_and_partial_benefit() -> None:
    row = _plan_capability("load")
    mapping = row.to_dict()
    mapping["authority"]["surprise"] = True
    with pytest.raises(StateValidationError, match="unknown"):
        PlanCapabilityV3.from_dict(mapping)

    mapping = row.to_dict()
    mapping["benefit"].pop("tier")
    with pytest.raises(StateValidationError, match="missing.*tier"):
        PlanCapabilityV3.from_dict(mapping)


def test_ready_plan_allows_unleased_omitted_active_capability_to_retire() -> None:
    current = _plan_capability("load", name="current")
    retiring = _plan_capability("load", name="retiring")
    capability = _capability_state(
        retiring,
        installation="installed",
        current_authorized_material=_catalog_material("retiring"),
        active=True,
        leases=(),
        plan_id="plan-old",
        catalog_snapshot_id=_digest("c"),
    )
    deactivation = _material_action(
        capability,
        kind="DeactivateCapability",
        action_id="retire-1",
    )
    state = _state(
        _committed_plan(current),
        capability,
        pending_effects=(PendingEffect(action=deactivation, effect="deactivate"),),
    )

    assert EngineState.from_json(state.to_json()) == state


def test_abstained_plan_allows_unleased_active_capability_to_cool() -> None:
    retiring = _plan_capability("load", name="retiring")
    capability = _capability_state(
        retiring,
        installation="installed",
        current_authorized_material=_catalog_material("retiring"),
        active=True,
        leases=(),
        plan_id="plan-old",
        catalog_snapshot_id=_digest("c"),
    )
    plan = CommittedPlanV3(
        plan_id="plan-a",
        catalog_snapshot_id=_digest("d"),
        decision_digest=_digest("e"),
        status="abstained",
        abstention_code="no-feasible-capability",
        benefit_audit=_audit(search_evaluation_count=0),
        capabilities=(),
    )
    deactivation = _material_action(
        capability,
        kind="DeactivateCapability",
        action_id="cool-1",
    )
    state = _state(
        plan,
        capability,
        pending_effects=(PendingEffect(action=deactivation, effect="deactivate"),),
    )

    assert EngineState.from_json(state.to_json()) == state


def test_ready_omitted_pending_install_remains_bound_to_prior_snapshot() -> None:
    current = _plan_capability("load", name="current")
    installing = _plan_capability("install", name="installing")
    capability = _capability_state(
        installing,
        installation="absent",
        current_authorized_material=None,
        leases=(),
        plan_id="plan-old",
        catalog_snapshot_id=_digest("c"),
    )
    install_action = _install_action(capability)
    state = _state(
        _committed_plan(current),
        capability,
        pending_effects=(PendingEffect(action=install_action, effect="install"),),
    )

    assert EngineState.from_json(state.to_json()) == state
    assert state.pending_effects[0].action.plan_id == "plan-old"


def test_degraded_plan_preserves_prior_runtime_leases_and_plan_identity() -> None:
    prior = _plan_capability("load", name="prior")
    capability = _capability_state(
        prior,
        installation="installed",
        current_authorized_material=_catalog_material("prior"),
        active=True,
        plan_id="plan-old",
        catalog_snapshot_id=_digest("c"),
    )
    degraded = CommittedPlanV3(
        plan_id="plan-a",
        catalog_snapshot_id=_digest("d"),
        decision_digest=_digest("e"),
        status="degraded",
        abstention_code="planner-failed",
        benefit_audit=None,
        capabilities=(),
    )
    state = _state(degraded, capability)

    assert EngineState.from_json(state.to_json()) == state
    assert state.capabilities[0].plan_id == "plan-old"
    assert state.capabilities[0].leases == (_lease(),)

    with pytest.raises(StateValidationError, match="omitted.*mint"):
        _state(
            degraded,
            _capability_state(
                prior,
                installation="installed",
                current_authorized_material=_catalog_material("prior"),
                active=True,
            ),
        )


def test_omitted_runtime_cannot_claim_current_plan_identity_for_rollback() -> None:
    current = _plan_capability("load", name="current")
    omitted = _plan_capability("load", name="omitted")
    capability = _capability_state(
        omitted,
        installation="installed",
        current_authorized_material=_catalog_material("omitted"),
        rollback_held=True,
        leases=(),
    )

    with pytest.raises(StateValidationError, match="omitted.*current-plan"):
        _state(
            _committed_plan(current),
            capability,
            rollback_requested_capability_ids=(capability.capability_id,),
        )


def test_pending_activation_rejects_active_or_self_rollback_target() -> None:
    new_row = _plan_capability("load", name="new")
    old_row = _plan_capability("load", name="old")
    new = _capability_state(
        new_row,
        installation="installed",
        current_authorized_material=_catalog_material("new"),
    )
    old = _capability_state(
        old_row,
        installation="installed",
        current_authorized_material=_catalog_material("old"),
        active=True,
        rollback_held=True,
        leases=(),
    )
    activation = _material_action(new, action_id="activate-new")

    with pytest.raises(StateValidationError, match="invalid rollback"):
        _state(
            _committed_plan(new_row, old_row),
            new,
            old,
            pending_effects=(
                PendingEffect(
                    action=activation,
                    effect="activate",
                    rollback_capability_id=old.capability_id,
                ),
            ),
        )
    with pytest.raises(StateValidationError, match="invalid rollback"):
        self_target = _capability_state(
            new_row,
            installation="installed",
            current_authorized_material=_catalog_material("new"),
            rollback_held=True,
        )
        _state(
            _committed_plan(new_row),
            self_target,
            pending_effects=(
                PendingEffect(
                    action=activation,
                    effect="activate",
                    rollback_capability_id=self_target.capability_id,
                ),
            ),
        )


def test_blocked_install_descriptor_requires_current_or_retained_authority() -> None:
    load = _plan_capability("load")
    loaded = _capability_state(
        load,
        installation="installed",
        current_authorized_material=_catalog_material(),
    )
    with pytest.raises(StateValidationError, match="blocked install descriptor"):
        _state(
            _committed_plan(load),
            loaded,
            blocked_install_descriptor_digests=(_digest("0"),),
        )

    install = _plan_capability("install", name="installing")
    installing = _capability_state(
        install,
        installation="absent",
        current_authorized_material=None,
    )
    descriptor_digest = _install_descriptor("installing").descriptor_digest
    state = _state(
        _committed_plan(install),
        installing,
        blocked_install_descriptor_digests=(descriptor_digest,),
    )
    assert EngineState.from_json(state.to_json()) == state


def test_ready_plan_rejects_leased_unselected_runtime_capability() -> None:
    selected = _plan_capability("load")
    other = _plan_capability("load", name="other")
    capability = _capability_state(
        other,
        installation="installed",
        current_authorized_material=_catalog_material("other"),
        plan_id="plan-old",
        catalog_snapshot_id=_digest("c"),
    )

    with pytest.raises(StateValidationError, match="unselected.*leases"):
        _state(_committed_plan(selected), capability)


def test_inactive_unselected_capability_cannot_survive_persisted_ready_state() -> None:
    selected = _plan_capability("load")
    other = _plan_capability("load", name="other")
    capability = _capability_state(
        other,
        installation="installed",
        current_authorized_material=_catalog_material("other"),
        leases=(),
        plan_id="plan-old",
        catalog_snapshot_id=_digest("c"),
    )
    state = _state(_committed_plan(selected), capability)

    with pytest.raises(StateValidationError, match="inactive unselected"):
        state.to_json()


def test_promotion_rejects_installed_lineage_from_another_descriptor() -> None:
    row = _plan_capability("install")
    other_descriptor = _install_descriptor("other")
    material = _material_identity()
    wrong_lineage = InstalledMaterialLineage.create(
        capability_id=material.capability_id,
        kind=material.kind,
        catalog_identity_digest=_catalog_identity().identity_digest,
        material_identity_digest=material.identity_digest,
        origin_install_descriptor_digest=other_descriptor.descriptor_digest,
        install_action_content_digest=_digest("6"),
        install_receipt_content_digest=_digest("7"),
    )

    with pytest.raises(StateValidationError, match="descriptor|promotion"):
        _capability_state(
            row,
            installation="installed",
            current_authorized_material=AuthorizedMaterial.from_installed(wrong_lineage),
        )
