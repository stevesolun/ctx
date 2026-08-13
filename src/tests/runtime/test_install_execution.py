from __future__ import annotations

import copy
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Literal

import pytest

from ctx.engine.content import MaterialIdentity
from ctx.engine.engine import CtxEngine, CtxEngineError
from ctx.engine.installation import InstallExecutionBinding, InstallPlanDescriptor
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, InstallPlanningAuthority
from ctx.engine.protocol import HostAction
from ctx.engine.store import InstallActionClaimExpired, SQLiteEngineStore
from ctx.runtime.install_execution import (
    InstallDriverObservation,
    InstallDriverRegistration,
    InstallDriverRegistry,
    InstallDriverRequest,
    InstallExecutionHandleConsumed,
    InstallExecutionProcessMismatch,
    prepare_install_execution,
)
from tests.engine import test_engine_install_coordinator as support


class _Driver:
    def __init__(self, observation: InstallDriverObservation) -> None:
        self.observation = observation
        self.apply_calls = 0
        self.reconcile_calls = 0

    def apply_once(self) -> None:
        self.apply_calls += 1

    def reconcile(self) -> InstallDriverObservation:
        self.reconcile_calls += 1
        return self.observation


class _Factory:
    def __init__(self, engine: CtxEngine, action: HostAction, driver: _Driver) -> None:
        self.engine = engine
        self.action = action
        self.driver = driver
        self.connect_calls = 0

    @contextmanager
    def connect(self, request: InstallDriverRequest) -> Iterator[_Driver]:
        assert request.action == self.action
        status = self.engine.install_execution_status(self.action)
        assert status.claimed
        self.connect_calls += 1
        yield self.driver


def _binding(action: HostAction) -> InstallExecutionBinding:
    descriptor = support._descriptor()
    installer_digest = action.payload.get("installer_digest")
    assert isinstance(installer_digest, str)
    return InstallExecutionBinding(
        driver_id=descriptor.installer_id,
        driver_digest=installer_digest,
        host_identity_digest=support._digest("host:local-test"),
        target_identity_digest=support._digest("target:private-skill-cas"),
    )


def _observation(
    state: Literal["installed-exact", "absent", "conflict", "indeterminate"] = ("installed-exact"),
) -> InstallDriverObservation:
    material_digest = support._material().identity_digest
    return InstallDriverObservation(
        state=state,
        verification_digest=support._digest(f"observation:{state}"),
        observed_material_identity_digest=(material_digest if state == "installed-exact" else None),
    )


def _fixture(
    tmp_path: Path,
    *,
    observation: InstallDriverObservation | None = None,
    trusted_utc_now: Callable[[], datetime] = lambda: support.BEFORE_EXPIRY,
) -> tuple[
    CtxEngine,
    HostAction,
    CapabilityPlanSelectionV3,
    InstallExecutionBinding,
    _Factory,
    InstallDriverRegistry,
]:
    engine, policy = support._engine(tmp_path, trusted_utc_now=trusted_utc_now)
    action = support._pending_install(engine)
    selection = support._selection()
    binding = _binding(action)
    driver = _Driver(observation or _observation())
    factory = _Factory(engine, action, driver)
    registry = InstallDriverRegistry(
        (
            InstallDriverRegistration(
                binding=binding,
                capability_kind="skill",
                factory=factory,
            ),
        )
    )
    assert policy.policy_digest
    return engine, action, selection, binding, factory, registry


def _handle(
    engine: CtxEngine,
    action: HostAction,
    selection: CapabilityPlanSelectionV3,
    registry: InstallDriverRegistry,
):
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    policy_digest = snapshot.state.install_policy_snapshot_digest
    assert policy_digest is not None
    return prepare_install_execution(
        engine=engine,
        action=action,
        selection=selection,
        descriptor=authority.descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy_digest,
        registry=registry,
    )


def test_handle_is_nonserializable_and_connects_only_after_durable_claim(
    tmp_path: Path,
) -> None:
    engine, action, selection, binding, factory, registry = _fixture(tmp_path)
    handle = _handle(engine, action, selection, registry)

    assert factory.connect_calls == 0
    assert not engine.install_execution_status(action).claimed
    with pytest.raises(TypeError):
        copy.copy(handle)
    with pytest.raises(TypeError):
        copy.deepcopy(handle)
    with pytest.raises(TypeError):
        pickle.dumps(handle)

    report = handle.execute()

    assert report.outcome == "applied"
    assert report.claim_was_new
    assert report.settled
    assert report.execution_binding_digest == binding.binding_digest
    assert factory.connect_calls == 1
    assert factory.driver.apply_calls == 1
    assert factory.driver.reconcile_calls == 1
    assert engine.install_execution_status(action).settled
    assert not hasattr(engine, "record_install_outcome")

    with pytest.raises(InstallExecutionHandleConsumed):
        handle.execute()


def test_existing_claim_is_reconciliation_only(tmp_path: Path) -> None:
    engine, action, selection, binding, factory, registry = _fixture(tmp_path)
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    policy_digest = snapshot.state.install_policy_snapshot_digest
    assert policy_digest is not None
    engine.authorize_install(
        action,
        selection,
        authority.descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy_digest,
        execution_binding=binding,
    )

    report = _handle(engine, action, selection, registry).execute()

    assert report.outcome == "applied"
    assert not report.claim_was_new
    assert factory.driver.apply_calls == 0
    assert factory.driver.reconcile_calls == 1


def test_recovery_settles_existing_outcome_without_driver_connection(tmp_path: Path) -> None:
    engine, action, selection, binding, factory, registry = _fixture(tmp_path)
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    policy_digest = snapshot.state.install_policy_snapshot_digest
    assert policy_digest is not None
    engine.authorize_install(
        action,
        selection,
        authority.descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy_digest,
        execution_binding=binding,
    )
    engine._record_install_outcome(  # noqa: SLF001 - crash-recovery fixture.
        action,
        execution_binding=binding,
        execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
            action, binding
        ),
        outcome="applied",
        observed_material_identity_digest=support._material().identity_digest,
        verification_digest=support._digest("durable-before-crash"),
    )

    report = _handle(engine, action, selection, registry).execute()

    assert report.outcome == "applied"
    assert report.settled
    assert not report.claim_was_new
    assert factory.connect_calls == 0
    assert factory.driver.apply_calls == 0


def test_indeterminate_observation_never_mints_outcome_or_receipt(tmp_path: Path) -> None:
    engine, action, selection, _binding_value, factory, registry = _fixture(
        tmp_path,
        observation=_observation("indeterminate"),
    )

    report = _handle(engine, action, selection, registry).execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    status = engine.install_execution_status(action)
    assert status.claimed
    assert not status.outcome_recorded
    assert factory.driver.apply_calls == 1


def test_concurrent_handles_apply_physical_effect_at_most_once(tmp_path: Path) -> None:
    engine, action, selection, _binding_value, factory, registry = _fixture(tmp_path)
    first = _handle(engine, action, selection, registry)
    second = _handle(engine, action, selection, registry)
    barrier = threading.Barrier(2)

    def run(handle: object):
        barrier.wait()
        return handle.execute()  # type: ignore[attr-defined]

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = tuple(pool.map(run, (first, second)))

    assert factory.driver.apply_calls == 1
    assert factory.connect_calls == 1
    assert all(report.settled for report in reports)
    assert sum(report.claim_was_new for report in reports) == 1


def test_process_mismatch_consumes_no_driver_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, selection, _binding_value, factory, registry = _fixture(tmp_path)
    handle = _handle(engine, action, selection, registry)
    monkeypatch.setattr("ctx.runtime.install_execution.os.getpid", lambda: 999_999)

    with pytest.raises(InstallExecutionProcessMismatch):
        handle.execute()

    assert factory.connect_calls == 0
    assert not engine.install_execution_status(action).claimed


def test_registry_rejects_driver_digest_substitution_before_claim(tmp_path: Path) -> None:
    engine, action, selection, binding, factory, _registry = _fixture(tmp_path)
    substituted = InstallExecutionBinding(
        driver_id=binding.driver_id,
        driver_digest=support._digest("substituted-driver"),
        host_identity_digest=binding.host_identity_digest,
        target_identity_digest=binding.target_identity_digest,
    )
    registry = InstallDriverRegistry(
        (
            InstallDriverRegistration(
                binding=substituted,
                capability_kind="skill",
                factory=factory,
            ),
        )
    )

    with pytest.raises(CtxEngineError, match="driver"):
        _handle(engine, action, selection, registry)

    assert factory.connect_calls == 0
    assert not engine.install_execution_status(action).claimed


def test_registry_rejects_capability_kind_substitution_before_claim(tmp_path: Path) -> None:
    engine, action, selection, binding, factory, _registry = _fixture(tmp_path)
    registry = InstallDriverRegistry(
        (
            InstallDriverRegistration(
                binding=binding,
                capability_kind="agent",
                factory=factory,
            ),
        )
    )

    with pytest.raises(CtxEngineError, match="kind"):
        _handle(engine, action, selection, registry)

    assert factory.connect_calls == 0
    assert not engine.install_execution_status(action).claimed


def test_registry_rejects_duplicate_same_kind_registration(tmp_path: Path) -> None:
    _engine, _action, _selection_value, binding, factory, _registry = _fixture(tmp_path)
    registration = InstallDriverRegistration(
        binding=binding,
        capability_kind="skill",
        factory=factory,
    )

    with pytest.raises(ValueError, match="duplicate install driver registration"):
        InstallDriverRegistry((registration, registration))


def test_registry_rejects_action_descriptor_kind_mismatch_before_claim(
    tmp_path: Path,
) -> None:
    engine, action, selection, _binding_value, factory, registry = _fixture(tmp_path)
    skill_descriptor = support._descriptor()
    agent_material = MaterialIdentity.create(
        capability_id="agent:reviewer",
        kind="agent",
        content_sha256=support._digest("agent-body"),
        content_bytes=1,
    )
    agent_descriptor = InstallPlanDescriptor.create(
        capability_id=agent_material.capability_id,
        kind=agent_material.kind,
        installer_id=skill_descriptor.installer_id,
        plan_digest=skill_descriptor.plan_digest,
        provenance_digest=skill_descriptor.provenance_digest,
        result_material_identity_digest=agent_material.identity_digest,
    )
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    policy_digest = snapshot.state.install_policy_snapshot_digest
    assert policy_digest is not None

    with pytest.raises(CtxEngineError, match="action and descriptor capability kinds differ"):
        prepare_install_execution(
            engine=engine,
            action=action,
            selection=selection,
            descriptor=agent_descriptor,
            expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
            expected_policy_digest=policy_digest,
            registry=registry,
        )

    assert factory.connect_calls == 0
    assert not engine.install_execution_status(action).claimed


def test_fatal_apply_is_reconciled_and_settled_before_reraise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, selection, _binding_value, factory, registry = _fixture(
        tmp_path,
        observation=_observation("absent"),
    )

    def interrupt() -> None:
        factory.driver.apply_calls += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(factory.driver, "apply_once", interrupt)

    with pytest.raises(KeyboardInterrupt):
        _handle(engine, action, selection, registry).execute()

    status = engine.install_execution_status(action)
    assert status.settled
    assert status.outcome == "failed"
    assert factory.driver.reconcile_calls == 1


def test_install_lock_target_is_canonical_for_same_durable_journal(tmp_path: Path) -> None:
    engine, action, _selection_value, _binding_value, _factory, _registry = _fixture(tmp_path)
    restarted, _policy = support._engine(
        tmp_path,
        store=SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3"),
    )

    first = engine._install_execution_lock_target(action)  # noqa: SLF001
    second = restarted._install_execution_lock_target(action)  # noqa: SLF001

    assert first == second
    assert first.parent.name == "install-execution-locks"


def test_handle_rechecks_exact_expiry_boundary_before_driver_connection(
    tmp_path: Path,
) -> None:
    current = [support.BEFORE_EXPIRY]
    engine, action, selection, _binding_value, factory, registry = _fixture(
        tmp_path,
        trusted_utc_now=lambda: current[0],
    )
    assert action.expires_at is not None
    current[0] = datetime.fromisoformat(action.expires_at.replace("Z", "+00:00"))

    with pytest.raises(InstallActionClaimExpired):
        _handle(engine, action, selection, registry).execute()

    assert factory.connect_calls == 0
    assert factory.driver.apply_calls == 0
    assert not engine.install_execution_status(action).claimed
