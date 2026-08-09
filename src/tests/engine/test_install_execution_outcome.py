from __future__ import annotations

from pathlib import Path

import pytest

from ctx.engine.engine import CtxEngine, CtxEngineError
from ctx.engine.installation import InstallExecutionBinding, InstallPlanDescriptor
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, InstallPlanningAuthority
from ctx.engine.protocol import HostAction
from ctx.engine.store import InstallExecutionOutcomeRequired, SQLiteEngineStore
from tests.engine import test_engine_install_claim as claim_support
from tests.engine import test_engine_install_coordinator as support


def _binding() -> InstallExecutionBinding:
    return InstallExecutionBinding(
        driver_id=support._descriptor().installer_id,
        driver_digest=support.INSTALLER_DIGEST,
        host_identity_digest=support._digest("host:codex"),
        target_identity_digest=support._digest("target:user-skills"),
    )


def _claimed(
    tmp_path: Path,
) -> tuple[
    CtxEngine,
    HostAction,
    CapabilityPlanSelectionV3,
    InstallPlanDescriptor,
    InstallExecutionBinding,
]:
    engine, policy = support._engine(tmp_path)
    action = support._pending_install(engine)
    selection = support._selection()
    descriptor = support._descriptor()
    binding = _binding()
    engine.authorize_install(
        action,
        selection,
        descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy.policy_digest,
        execution_binding=binding,
    )
    return engine, action, selection, descriptor, binding


def test_claim_binds_exact_driver_host_and_target(tmp_path: Path) -> None:
    engine, action, _selection, _descriptor, binding = _claimed(tmp_path)

    status = engine.install_execution_status(action)

    assert status.claimed
    assert not status.outcome_recorded
    assert not status.settled
    assert status.execution_binding_digest == binding.binding_digest


def test_raw_install_receipt_cannot_settle_without_verified_driver_outcome(
    tmp_path: Path,
) -> None:
    engine, action, _selection, _descriptor, _binding_value = _claimed(tmp_path)
    receipt = claim_support._receipt_event("ActionApplied", action, "forged-receipt")

    with pytest.raises(InstallExecutionOutcomeRequired):
        engine.process(receipt)


def test_verified_outcome_is_required_and_atomically_bound_to_settlement(
    tmp_path: Path,
) -> None:
    engine, action, selection, _descriptor, binding = _claimed(tmp_path)
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    material = authority.result_material
    guard = engine._record_install_outcome(  # noqa: SLF001 - coordinator seam.
        action,
        execution_binding=binding,
        execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
            action, binding
        ),
        outcome="applied",
        observed_material_identity_digest=material.identity_digest,
        verification_digest=support._digest("verified-target-bytes"),
    )
    receipt = claim_support._receipt_event("ActionApplied", action, "verified-receipt")

    transition = engine.process_install_receipt(receipt, guard)

    assert transition.to_revision == 5
    status = engine.install_execution_status(action)
    assert status.outcome_recorded
    assert status.settled
    assert status.outcome == "applied"


def test_failed_outcome_requires_verified_absence_and_matching_failed_receipt(
    tmp_path: Path,
) -> None:
    engine, action, _selection, _descriptor, binding = _claimed(tmp_path)
    guard = engine._record_install_outcome(  # noqa: SLF001 - coordinator seam.
        action,
        execution_binding=binding,
        execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
            action, binding
        ),
        outcome="failed",
        observed_material_identity_digest=None,
        verification_digest=support._digest("verified-target-absent"),
    )

    with pytest.raises(CtxEngineError, match="outcome"):
        engine.process_install_receipt(
            claim_support._receipt_event("ActionApplied", action, "wrong-kind"),
            guard,
        )

    transition = engine.process_install_receipt(
        claim_support._receipt_event("ActionFailed", action, "verified-failure"),
        guard,
    )
    assert transition.to_revision == 5


def test_applied_outcome_rejects_wrong_material_and_binding_substitution(
    tmp_path: Path,
) -> None:
    engine, action, _selection, _descriptor, binding = _claimed(tmp_path)

    with pytest.raises(CtxEngineError, match="material"):
        engine._record_install_outcome(  # noqa: SLF001 - coordinator seam.
            action,
            execution_binding=binding,
            execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
                action, binding
            ),
            outcome="applied",
            observed_material_identity_digest=support._digest("wrong-material"),
            verification_digest=support._digest("verification"),
        )

    with pytest.raises(CtxEngineError, match="binding"):
        substituted_binding = InstallExecutionBinding(
            driver_id=binding.driver_id,
            driver_digest=binding.driver_digest,
            host_identity_digest=binding.host_identity_digest,
            target_identity_digest=support._digest("substituted-target"),
        )
        engine._record_install_outcome(  # noqa: SLF001 - coordinator seam.
            action,
            execution_binding=substituted_binding,
            execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
                action, substituted_binding
            ),
            outcome="failed",
            observed_material_identity_digest=None,
            verification_digest=support._digest("verification"),
        )


def test_execution_status_survives_restart_without_reminting_authority(
    tmp_path: Path,
) -> None:
    engine, action, _selection, _descriptor, binding = _claimed(tmp_path)
    restarted, _policy = support._engine(
        tmp_path,
        store=SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3"),
    )

    status = restarted.install_execution_status(action)

    assert status.claimed
    assert status.execution_binding_digest == binding.binding_digest
    assert not status.outcome_recorded


def test_outcome_cannot_be_recorded_without_a_claim(tmp_path: Path) -> None:
    engine, _policy = support._engine(tmp_path)
    action = support._pending_install(engine)

    with pytest.raises(InstallExecutionOutcomeRequired, match="claim"):
        engine._record_install_outcome(  # noqa: SLF001 - coordinator seam.
            action,
            execution_binding=_binding(),
            execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
                action, _binding()
            ),
            outcome="failed",
            observed_material_identity_digest=None,
            verification_digest=support._digest("verified-target-absent"),
        )
