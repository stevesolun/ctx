from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctx.engine.content import MaterialIdentity
from ctx.engine.engine import CtxEngine, CtxEngineError
from ctx.engine.installation import (
    InstallConsentPolicy,
    InstallExecutionBinding,
    InstallPlanDescriptor,
)
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate, CapabilitySelection
from ctx.engine.planning_v3 import (
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
    ManualPlanningAuthority,
)
from ctx.engine.protocol import EngineEvent, HostAction
from ctx.engine.store import (
    InstallActionAlreadyClaimed,
    InstallActionClaimExpired,
    InstallExecutionOutcomeRequired,
    SQLiteEngineStore,
)
from tests.engine import test_engine_install_coordinator as support


def _typed_selection(kind: str) -> tuple[CapabilityPlanSelectionV3, InstallPlanDescriptor]:
    name = f"remote-{kind.replace('-', '')}-testing"
    capability_id = f"{kind}:{name}"
    material = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=support._digest(f"material:{kind}"),
        content_bytes=512,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id=f"ctx-{kind}-installer-v1",
        plan_digest=support.INSTALL_PLAN_DIGEST,
        provenance_digest=support._digest(f"installation-snapshot:{kind}"),
        result_material_identity_digest=material.identity_digest,
    )
    selection = CapabilityPlanSelectionV3(
        presentation=CapabilityCandidate(
            capability_id=capability_id,
            kind=kind,
            name=name,
            source_digest=support.SOURCE_DIGEST,
            normalized_score_ppm=900_000,
            matching_signals=("python", "testing"),
            reason_codes=("exact-tag-match",),
            actionability="install",
            install_descriptor_digest=descriptor.descriptor_digest,
            install_plan_digest=descriptor.plan_digest,
        ),
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=capability_id,
            kind=kind,
            catalog_namespace_digest=support._digest(f"catalog-namespace:{kind}"),
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
    return selection, descriptor


def _configure_selection(
    monkeypatch: pytest.MonkeyPatch,
    selection: CapabilityPlanSelectionV3,
) -> None:
    monkeypatch.setattr(support, "_selection", lambda: selection)


def _policy_for(kind: str, mode: str) -> InstallConsentPolicy:
    modes = {
        "skill_mode": "ask-each-time",
        "agent_mode": "ask-each-time",
        "mcp_server_mode": "ask-each-time",
    }
    modes[{"skill": "skill_mode", "agent": "agent_mode", "mcp-server": "mcp_server_mode"}[kind]] = (
        mode
    )
    return InstallConsentPolicy(**modes)


def _binding(descriptor: InstallPlanDescriptor) -> InstallExecutionBinding:
    return InstallExecutionBinding(
        driver_id=descriptor.installer_id,
        driver_digest=support.INSTALLER_DIGEST,
        host_identity_digest=support._digest(f"host:{descriptor.kind}"),
        target_identity_digest=support._digest(f"target:{descriptor.capability_id}"),
    )


def _claim(
    engine: CtxEngine,
    action: HostAction,
    selection: CapabilityPlanSelectionV3,
    descriptor: InstallPlanDescriptor,
    policy: InstallConsentPolicy,
    *,
    execution_binding: InstallExecutionBinding | None = None,
) -> InstallExecutionBinding:
    binding = execution_binding or _binding(descriptor)
    engine.authorize_install(
        action,
        selection,
        descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy.policy_digest,
        execution_binding=binding,
    )
    return binding


def _receipt_event(
    kind: str,
    action: HostAction,
    event_id: str,
    *,
    revision: int = 4,
) -> EngineEvent:
    payload: dict[str, object] = {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
    }
    if kind == "ActionApplied":
        payload["verification"] = support._install_receipt_verification(action)
    elif kind == "ActionFailed":
        payload["error"] = {"code": "installer-failed"}
    else:
        payload["reason"] = "expired"
    return support._event(kind, revision, event_id, payload=payload)


@pytest.mark.parametrize("kind", ["skill", "agent", "mcp-server"])
@pytest.mark.parametrize(
    ("mode", "decision_basis"),
    [
        ("ask-each-time", "interactive"),
        ("preapproved-auto", "preapproved-policy"),
    ],
)
def test_claim_accepts_all_installable_kinds_and_both_consent_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    mode: str,
    decision_basis: str,
) -> None:
    selection, descriptor = _typed_selection(kind)
    _configure_selection(monkeypatch, selection)
    policy = _policy_for(kind, mode)
    engine, _ = support._engine(
        tmp_path,
        policy=policy,
        decision_basis=decision_basis,
        descriptor=descriptor,
    )

    _claim(engine, support._pending_install(engine), selection, descriptor, policy)


def test_claim_is_burned_across_engine_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection, descriptor = _typed_selection("skill")
    _configure_selection(monkeypatch, selection)
    database = tmp_path / "engine" / "journal.sqlite3"
    engine, policy = support._engine(
        tmp_path,
        descriptor=descriptor,
        store=SQLiteEngineStore(database),
    )
    action = support._pending_install(engine)
    _claim(engine, action, selection, descriptor, policy)

    restarted = CtxEngine(
        store=SQLiteEngineStore(database),
        trusted_utc_now=lambda: support.BEFORE_EXPIRY,
    )
    with pytest.raises(InstallActionAlreadyClaimed):
        _claim(restarted, action, selection, descriptor, policy)


@pytest.mark.parametrize(
    "substitution",
    [
        "scope",
        "selection",
        "kind",
        "catalog",
        "material",
        "descriptor",
        "installer",
        "policy",
        "action",
    ],
)
def test_claim_rejects_typed_authority_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    selection, descriptor = _typed_selection("skill")
    _configure_selection(monkeypatch, selection)
    engine, policy = support._engine(tmp_path, descriptor=descriptor)
    action = support._pending_install(engine)
    submitted_action = action
    submitted_selection = selection
    submitted_descriptor = descriptor
    catalog_digest = support.CATALOG_DIGEST
    policy_digest = policy.policy_digest

    if substitution == "scope":
        submitted_action = replace(
            action,
            scope=replace(action.scope, exposure_id="substituted-exposure"),
        )
    elif substitution == "selection":
        submitted_selection = replace(
            selection,
            benefit=replace(selection.benefit, marginal_net_benefit_u=599_999),
        )
    elif substitution == "kind":
        submitted_selection, submitted_descriptor = _typed_selection("agent")
    elif substitution == "catalog":
        submitted_selection = replace(
            selection,
            catalog_identity=CatalogCapabilityIdentity.create(
                capability_id=selection.presentation.capability_id,
                kind=selection.presentation.kind,
                catalog_namespace_digest=support._digest("other-catalog"),
            ),
        )
    elif substitution == "material":
        replacement_material = MaterialIdentity.create(
            capability_id=selection.presentation.capability_id,
            kind=selection.presentation.kind,
            content_sha256=support._digest("other-material"),
            content_bytes=999,
        )
        submitted_descriptor = InstallPlanDescriptor.create(
            capability_id=selection.presentation.capability_id,
            kind=selection.presentation.kind,
            installer_id=descriptor.installer_id,
            plan_digest=descriptor.plan_digest,
            provenance_digest=descriptor.provenance_digest,
            result_material_identity_digest=replacement_material.identity_digest,
        )
        submitted_selection = replace(
            selection,
            presentation=replace(
                selection.presentation,
                install_descriptor_digest=submitted_descriptor.descriptor_digest,
            ),
            authority=InstallPlanningAuthority(
                descriptor=submitted_descriptor,
                result_material=replacement_material,
            ),
        )
    elif substitution == "descriptor":
        submitted_descriptor = InstallPlanDescriptor.create(
            capability_id=selection.presentation.capability_id,
            kind=selection.presentation.kind,
            installer_id="ctx-substituted-installer-v1",
            plan_digest=descriptor.plan_digest,
            provenance_digest=descriptor.provenance_digest,
            result_material_identity_digest=descriptor.result_material_identity_digest,
        )
        submitted_selection = replace(
            selection,
            presentation=replace(
                selection.presentation,
                install_descriptor_digest=submitted_descriptor.descriptor_digest,
            ),
            authority=InstallPlanningAuthority(
                descriptor=submitted_descriptor,
                result_material=selection.authority.result_material,  # type: ignore[union-attr]
            ),
        )
    elif substitution == "installer":
        raw_action = action.to_dict()
        raw_action["payload"]["installer_digest"] = support._digest("other-installer")
        submitted_action = HostAction.from_dict(raw_action)
    elif substitution == "policy":
        policy_digest = support._digest("other-policy")
    else:
        submitted_action = replace(action, action_id="substituted-action")

    if substitution == "catalog":
        catalog_digest = support._digest("other-catalog-snapshot")

    with pytest.raises(CtxEngineError):
        engine.authorize_install(
            submitted_action,
            submitted_selection,
            submitted_descriptor,
            expected_catalog_snapshot_digest=catalog_digest,
            expected_policy_digest=policy_digest,
            execution_binding=_binding(submitted_descriptor),
        )


def test_legacy_and_manual_selections_cannot_claim_physical_authority(
    tmp_path: Path,
) -> None:
    engine, policy = support._engine(tmp_path)
    action = support._pending_install(engine)
    legacy = CapabilitySelection.from_candidate(support._selection().presentation)
    with pytest.raises(CtxEngineError, match="legacy selections"):
        engine.authorize_install(
            action,
            legacy,
            support._descriptor(),
            expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
            expected_policy_digest=policy.policy_digest,
            execution_binding=_binding(support._descriptor()),
        )

    selection = support._selection()
    manual = replace(
        selection,
        presentation=replace(
            selection.presentation,
            actionability="manual",
            install_descriptor_digest=None,
            install_plan_digest=None,
        ),
        benefit=replace(selection.benefit, tier="advisory"),
        authority=ManualPlanningAuthority(),
    )
    with pytest.raises(CtxEngineError, match="no schema-v3 physical install authority"):
        engine.authorize_install(
            action,
            manual,
            support._descriptor(),
            expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
            expected_policy_digest=policy.policy_digest,
            execution_binding=_binding(support._descriptor()),
        )


@pytest.mark.parametrize("receipt_kind", ["ActionApplied", "ActionFailed"])
def test_install_completion_requires_claim_and_claimed_receipt_settles_once(
    tmp_path: Path,
    receipt_kind: str,
) -> None:
    engine, policy = support._engine(tmp_path)
    action = support._pending_install(engine)
    selection = support._selection()
    descriptor = support._descriptor()
    binding = _binding(descriptor)
    receipt = _receipt_event(receipt_kind, action, f"event-{receipt_kind.lower()}")

    with pytest.raises(InstallExecutionOutcomeRequired):
        engine.process(receipt)

    _claim(
        engine,
        action,
        selection,
        descriptor,
        policy,
        execution_binding=binding,
    )
    with pytest.raises(InstallExecutionOutcomeRequired):
        engine.process(receipt)

    applied = receipt_kind == "ActionApplied"
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    guard = engine._record_install_outcome(  # noqa: SLF001 - coordinator seam.
        action,
        execution_binding=binding,
        execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
            action, binding
        ),
        outcome="applied" if applied else "failed",
        observed_material_identity_digest=(
            authority.result_material.identity_digest if applied else None
        ),
        verification_digest=support._digest(f"verified:{receipt_kind}"),
    )
    transition = engine.process_install_receipt(receipt, guard)
    assert transition.to_revision == 5
    assert engine.process_install_receipt(receipt, guard) == transition


def test_unclaimed_expiry_is_allowed_but_claimed_expiry_is_rejected(
    tmp_path: Path,
) -> None:
    unclaimed_clock = [support.BEFORE_EXPIRY]
    unclaimed, _ = support._engine(
        tmp_path / "unclaimed",
        trusted_utc_now=lambda: unclaimed_clock[0],
    )
    unclaimed_action = support._pending_install(unclaimed)
    unclaimed_clock[0] = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
    expired = unclaimed.process(
        _receipt_event("ActionExpired", unclaimed_action, "event-expired-unclaimed")
    )
    assert expired.to_revision == 5

    claimed_clock = [support.BEFORE_EXPIRY]
    claimed, policy = support._engine(
        tmp_path / "claimed",
        trusted_utc_now=lambda: claimed_clock[0],
    )
    claimed_action = support._pending_install(claimed)
    _claim(claimed, claimed_action, support._selection(), support._descriptor(), policy)
    claimed_clock[0] = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
    with pytest.raises(InstallActionAlreadyClaimed):
        claimed.process(_receipt_event("ActionExpired", claimed_action, "event-expired-claimed"))


def test_unclaimed_expiry_is_rejected_before_trusted_expiry(tmp_path: Path) -> None:
    engine, _ = support._engine(tmp_path)
    action = support._pending_install(engine)

    with pytest.raises(CtxEngineError, match="has not expired"):
        engine.process(_receipt_event("ActionExpired", action, "event-expired-too-early"))


def test_claim_rejects_action_at_trusted_expiry_boundary(tmp_path: Path) -> None:
    current_time = [datetime(2026, 8, 1, 12, 30, tzinfo=UTC)]
    engine, policy = support._engine(
        tmp_path,
        trusted_utc_now=lambda: current_time[0],
    )
    action = support._pending_install(engine)
    current_time[0] = datetime(2026, 8, 1, 13, 0, tzinfo=UTC)
    with pytest.raises(InstallActionClaimExpired):
        _claim(engine, action, support._selection(), support._descriptor(), policy)
