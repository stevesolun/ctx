"""One-shot, host-neutral installation execution boundary.

The journal authorizes an exact action; a trusted registry chooses the concrete
driver, host, and target.  Executable material remains behind ``connect`` and
cannot be reached until the durable one-use claim exists.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import Literal, NoReturn, Protocol, SupportsIndex

from ctx.engine.engine import CtxEngine, CtxEngineError
from ctx.engine.installation import (
    INSTALLABLE_CAPABILITY_KINDS,
    InstallExecutionBinding,
    InstallPlanDescriptor,
)
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3
from ctx.engine.protocol import INSTALL_ACTION_PAYLOAD_SCHEMA_V3, INSTALL_RECEIPT_SCHEMA_V3
from ctx.engine.protocol import EngineEvent, HostAction, Transition
from ctx.engine.store import (
    InstallActionAlreadyClaimed,
    InstallActionClaimGuard,
    InstallExecutionStatus,
    RevisionConflict,
)
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import reject_symlink_path


_SETTLEMENT_ATTEMPTS = 3


class InstallExecutionError(CtxEngineError):
    """Base class for coordinator-owned execution failures."""


class InstallExecutionHandleConsumed(InstallExecutionError):
    """A one-shot execution handle was invoked more than once."""


class InstallExecutionProcessMismatch(InstallExecutionError):
    """A handle was invoked from a process other than its creator."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallDriverObservation:
    """Safe, bounded result of independent physical-state reconciliation."""

    state: Literal["installed-exact", "absent", "conflict", "indeterminate"]
    verification_digest: str
    observed_material_identity_digest: str | None

    def __post_init__(self) -> None:
        if self.state not in {"installed-exact", "absent", "conflict", "indeterminate"}:
            raise ValueError("unsupported install observation state")
        _require_digest(self.verification_digest, "verification_digest")
        if self.observed_material_identity_digest is not None:
            _require_digest(
                self.observed_material_identity_digest,
                "observed_material_identity_digest",
            )
        if self.state == "installed-exact":
            if self.observed_material_identity_digest is None:
                raise ValueError("installed-exact observation requires a material identity")
        elif self.observed_material_identity_digest is not None:
            raise ValueError("non-installed observations cannot authorize material")


class HeldInstallDriver(Protocol):
    def apply_once(self) -> None: ...

    def reconcile(self) -> InstallDriverObservation: ...


class InstallDriverFactory(Protocol):
    def connect(
        self,
        request: InstallDriverRequest,
    ) -> AbstractContextManager[HeldInstallDriver]: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallDriverRequest:
    """Exact non-content authority passed to a driver only after the claim."""

    action: HostAction
    descriptor: InstallPlanDescriptor
    binding: InstallExecutionBinding

    def __post_init__(self) -> None:
        if not isinstance(self.action, HostAction):
            raise TypeError("action must be a HostAction")
        if not isinstance(self.descriptor, InstallPlanDescriptor):
            raise TypeError("descriptor must be an InstallPlanDescriptor")
        if not isinstance(self.binding, InstallExecutionBinding):
            raise TypeError("binding must be an InstallExecutionBinding")


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallDriverRegistration:
    """Trusted, prose-free binding from installer identity to one factory."""

    binding: InstallExecutionBinding
    capability_kind: str
    factory: InstallDriverFactory

    def __post_init__(self) -> None:
        if not isinstance(self.binding, InstallExecutionBinding):
            raise TypeError("binding must be an InstallExecutionBinding")
        if self.capability_kind not in INSTALLABLE_CAPABILITY_KINDS:
            raise ValueError("capability_kind is not an installable capability kind")
        if not callable(getattr(self.factory, "connect", None)):
            raise TypeError("factory must expose a connect context manager")


class InstallDriverRegistry:
    """Immutable exact-id registry configured by the trusted host composition."""

    def __init__(self, registrations: Iterable[InstallDriverRegistration]) -> None:
        copied: dict[tuple[str, str], InstallDriverRegistration] = {}
        for registration in registrations:
            if not isinstance(registration, InstallDriverRegistration):
                raise TypeError("registrations must contain InstallDriverRegistration values")
            key = (registration.binding.driver_id, registration.capability_kind)
            if key in copied:
                raise ValueError(f"duplicate install driver registration {key!r}")
            copied[key] = registration
        if not copied:
            raise ValueError("at least one install driver registration is required")
        self._registrations = MappingProxyType(copied)

    def resolve(
        self,
        action: HostAction,
        descriptor: InstallPlanDescriptor,
    ) -> InstallDriverRegistration:
        action_kind = action.payload.get("capability_kind")
        if action_kind != descriptor.kind:
            raise InstallExecutionError("install action and descriptor capability kinds differ")
        registration = self._registrations.get((descriptor.installer_id, descriptor.kind))
        if registration is None:
            raise InstallExecutionError("no trusted driver matches the install descriptor kind")
        binding = registration.binding
        if (
            binding.driver_id != descriptor.installer_id
            or binding.driver_digest != action.payload.get("installer_digest")
        ):
            raise InstallExecutionError("trusted driver identity does not match the journal")
        return registration


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallExecutionReport:
    """Safe result; contains no path, command, content, credential, or token."""

    outcome: Literal["applied", "failed", "indeterminate"]
    claim_was_new: bool
    settled: bool
    execution_binding_digest: str
    verification_digest: str | None
    transition: Transition | None

    def __post_init__(self) -> None:
        _require_digest(self.execution_binding_digest, "execution_binding_digest")
        if self.verification_digest is not None:
            _require_digest(self.verification_digest, "verification_digest")
        if self.settled and self.outcome == "indeterminate":
            raise ValueError("indeterminate execution cannot be settled")
        if self.transition is not None and not self.settled:
            raise ValueError("receipt transition requires a settled execution")


class _InstallExecutionHandle:
    __slots__ = (
        "_action",
        "_binding",
        "_consume_lock",
        "_consumed",
        "_descriptor",
        "_engine",
        "_expected_catalog_snapshot_digest",
        "_expected_policy_digest",
        "_factory",
        "_lock_target",
        "_pid",
        "_selection",
    )

    def __init__(
        self,
        *,
        engine: CtxEngine,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
        descriptor: InstallPlanDescriptor,
        expected_catalog_snapshot_digest: str,
        expected_policy_digest: str,
        registration: InstallDriverRegistration,
        lock_target: Path,
    ) -> None:
        self._engine = engine
        self._action = action
        self._selection = selection
        self._descriptor = descriptor
        self._expected_catalog_snapshot_digest = expected_catalog_snapshot_digest
        self._expected_policy_digest = expected_policy_digest
        self._binding = registration.binding
        self._factory = registration.factory
        self._lock_target = Path(lock_target)
        self._pid = os.getpid()
        self._consumed = False
        self._consume_lock = Lock()

    def execute(self) -> InstallExecutionReport:
        if os.getpid() != self._pid:
            raise InstallExecutionProcessMismatch(
                "install execution handle cannot cross a process boundary"
            )
        with self._consume_lock:
            if self._consumed:
                raise InstallExecutionHandleConsumed(
                    "install execution handle has already been consumed"
                )
            self._consumed = True
        with secure_file_lock(self._lock_target, timeout=30.0):
            return self._execute_locked()

    def _execute_locked(self) -> InstallExecutionReport:
        status = self._engine.install_execution_status(self._action)
        if status.claimed and status.execution_binding_digest != self._binding.binding_digest:
            raise InstallExecutionError("durable claim names a different driver or target")
        if status.settled:
            return self._status_report(status, claim_was_new=False, transition=None)

        claim_was_new = False
        if not status.claimed:
            try:
                self._engine.authorize_install(
                    self._action,
                    self._selection,
                    self._descriptor,
                    expected_catalog_snapshot_digest=(self._expected_catalog_snapshot_digest),
                    expected_policy_digest=self._expected_policy_digest,
                    execution_binding=self._binding,
                )
                claim_was_new = True
            except InstallActionAlreadyClaimed:
                claim_was_new = False
            status = self._engine.install_execution_status(self._action)
            if (
                not status.claimed
                or status.execution_binding_digest != self._binding.binding_digest
            ):
                raise InstallExecutionError("install claim was not durably bound")

        if status.outcome_recorded:
            return self._settle_status(status, claim_was_new=claim_was_new)

        observation, fatal = self._run_driver(claim_was_new=claim_was_new)
        if observation is None or observation.state in {"conflict", "indeterminate"}:
            report = InstallExecutionReport(
                outcome="indeterminate",
                claim_was_new=claim_was_new,
                settled=False,
                execution_binding_digest=self._binding.binding_digest,
                verification_digest=(
                    None if observation is None else observation.verification_digest
                ),
                transition=None,
            )
            if fatal is not None:
                raise fatal
            return report

        outcome: Literal["applied", "failed"] = (
            "applied" if observation.state == "installed-exact" else "failed"
        )
        permit = self._engine._issue_install_outcome_permit(  # noqa: SLF001
            self._action,
            self._binding,
        )
        guard = self._engine._record_install_outcome(  # noqa: SLF001
            self._action,
            execution_binding=self._binding,
            execution_authority=permit,
            outcome=outcome,
            observed_material_identity_digest=(observation.observed_material_identity_digest),
            verification_digest=observation.verification_digest,
        )
        report = self._settle_guard(
            guard,
            observed_at=self._engine.install_execution_status(self._action).observed_at,
            claim_was_new=claim_was_new,
            verification_digest=observation.verification_digest,
        )
        if fatal is not None:
            raise fatal
        return report

    def _run_driver(
        self,
        *,
        claim_was_new: bool,
    ) -> tuple[InstallDriverObservation | None, BaseException | None]:
        fatal: BaseException | None = None
        request = InstallDriverRequest(
            action=self._action,
            descriptor=self._descriptor,
            binding=self._binding,
        )
        try:
            manager = self._factory.connect(request)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return None, None
        try:
            driver = manager.__enter__()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return None, None
        observation: InstallDriverObservation | None = None
        try:
            if callable(getattr(driver, "reconcile", None)) and callable(
                getattr(driver, "apply_once", None)
            ):
                if claim_was_new:
                    try:
                        driver.apply_once()
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            fatal = exc
                try:
                    candidate = driver.reconcile()
                    if isinstance(candidate, InstallDriverObservation):
                        observation = candidate
                except BaseException as exc:
                    if not isinstance(exc, Exception):
                        fatal = fatal or exc
        finally:
            try:
                manager.__exit__(None, None, None)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    fatal = fatal or exc
                else:
                    observation = None
        return (
            observation,
            fatal,
        )

    def _settle_status(
        self,
        status: InstallExecutionStatus,
        *,
        claim_was_new: bool,
    ) -> InstallExecutionReport:
        if status.outcome is None or status.outcome_digest is None or status.observed_at is None:
            raise InstallExecutionError("durable execution outcome is incomplete")
        guard = InstallActionClaimGuard(
            action_id=self._action.action_id,
            action_content_digest=self._action.content_digest,
            mode=status.outcome,
            execution_outcome_digest=status.outcome_digest,
        )
        return self._settle_guard(
            guard,
            observed_at=status.observed_at,
            claim_was_new=claim_was_new,
            verification_digest=None,
        )

    def _settle_guard(
        self,
        guard: InstallActionClaimGuard,
        *,
        observed_at: str | None,
        claim_was_new: bool,
        verification_digest: str | None,
    ) -> InstallExecutionReport:
        if observed_at is None:
            raise InstallExecutionError("durable execution outcome has no observation time")
        for _ in range(_SETTLEMENT_ATTEMPTS):
            current = self._engine.install_execution_status(self._action)
            if current.settled:
                return self._status_report(
                    current,
                    claim_was_new=claim_was_new,
                    transition=None,
                )
            snapshot = self._engine.snapshot(self._action.scope)
            receipt = _receipt_event(
                action=self._action,
                guard=guard,
                expected_revision=snapshot.revision,
                observed_at=observed_at,
            )
            try:
                transition = self._engine.process_install_receipt(receipt, guard)
            except RevisionConflict:
                continue
            return InstallExecutionReport(
                outcome=guard.mode,  # type: ignore[arg-type]
                claim_was_new=claim_was_new,
                settled=True,
                execution_binding_digest=self._binding.binding_digest,
                verification_digest=verification_digest,
                transition=transition,
            )
        raise InstallExecutionError("install receipt settlement remained contended")

    def _status_report(
        self,
        status: InstallExecutionStatus,
        *,
        claim_was_new: bool,
        transition: Transition | None,
    ) -> InstallExecutionReport:
        if status.outcome not in {"applied", "failed"}:
            raise InstallExecutionError("settled install has no valid execution outcome")
        return InstallExecutionReport(
            outcome=status.outcome,
            claim_was_new=claim_was_new,
            settled=status.settled,
            execution_binding_digest=self._binding.binding_digest,
            verification_digest=None,
            transition=transition,
        )

    def __copy__(self) -> NoReturn:
        raise TypeError("install execution handle cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("install execution handle cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("install execution handle cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("install execution handle cannot be serialized")

    def __repr__(self) -> str:
        return "<one-shot-install-execution>"


def prepare_install_execution(
    *,
    engine: CtxEngine,
    action: HostAction,
    selection: CapabilityPlanSelectionV3,
    descriptor: InstallPlanDescriptor,
    expected_catalog_snapshot_digest: str,
    expected_policy_digest: str,
    registry: InstallDriverRegistry,
) -> _InstallExecutionHandle:
    """Create a non-executable handle; registry resolution is pure lookup only."""

    if not isinstance(engine, CtxEngine):
        raise TypeError("engine must be a CtxEngine")
    if not isinstance(action, HostAction):
        raise TypeError("action must be a HostAction")
    if not isinstance(selection, CapabilityPlanSelectionV3):
        raise TypeError("selection must be a CapabilityPlanSelectionV3")
    if not isinstance(descriptor, InstallPlanDescriptor):
        raise TypeError("descriptor must be an InstallPlanDescriptor")
    if not isinstance(registry, InstallDriverRegistry):
        raise TypeError("registry must be an InstallDriverRegistry")
    if (
        action.kind != "InstallCapability"
        or action.payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3
    ):
        raise InstallExecutionError("execution requires an exact schema-v3 install action")
    for value, field_name in (
        (expected_catalog_snapshot_digest, "expected_catalog_snapshot_digest"),
        (expected_policy_digest, "expected_policy_digest"),
    ):
        _require_digest(value, field_name)
    lock_target = engine._install_execution_lock_target(action)  # noqa: SLF001
    reject_symlink_path(lock_target.parent)
    if not lock_target.parent.is_dir():
        raise InstallExecutionError("canonical install lock root does not exist")
    registration = registry.resolve(action, descriptor)
    return _InstallExecutionHandle(
        engine=engine,
        action=action,
        selection=selection,
        descriptor=descriptor,
        expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
        expected_policy_digest=expected_policy_digest,
        registration=registration,
        lock_target=lock_target,
    )


def _receipt_event(
    *,
    action: HostAction,
    guard: InstallActionClaimGuard,
    expected_revision: int,
    observed_at: str,
) -> EngineEvent:
    payload: dict[str, object] = {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
    }
    if guard.mode == "applied":
        payload["verification"] = {
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
        kind = "ActionApplied"
    else:
        payload["error"] = {"code": "install-driver-verified-absent"}
        kind = "ActionFailed"
    return EngineEvent(
        event_id=f"ctx-install-receipt-{guard.execution_outcome_digest}",
        kind=kind,
        scope=action.scope,
        expected_revision=expected_revision,
        occurred_at=observed_at,
        payload=payload,
        privacy=action.privacy,
        correlation_id=action.plan_id,
        causation_id=action.action_id,
    )


def _require_digest(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "InstallDriverObservation",
    "InstallExecutionError",
    "InstallExecutionHandleConsumed",
    "InstallExecutionProcessMismatch",
    "InstallExecutionReport",
]
