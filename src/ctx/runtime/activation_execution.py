"""One-shot, host-neutral activation execution boundary.

Mirror of :mod:`ctx.runtime.install_execution` for the applied activation path.
The journal authorizes an exact ``ActivateCapability`` action; this boundary
sequences the engine's atomic one-use claim, one-shot verified outcome, and
idempotent receipt settlement so callers never reach engine-private methods
(``_issue_activation_outcome_permit`` / ``_record_activation_outcome``) or the
private ``composition._engine`` slot to activate an installed-inactive
capability.

The caller supplies the independent physical verification digest and the
committed host descriptor.  A generic physical activation driver registry does
not exist yet (only the release-skill actuator performs real CAS verification);
wiring one, plus the failed/expired/indeterminate recovery path, is the next
managed-activation slice.  This boundary is therefore intentionally
applied-only: any non-applied host outcome must be handled by that later slice
rather than silently reconciled here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Literal, NoReturn, SupportsIndex

from ctx.engine.engine import CtxEngine, CtxEngineError
from ctx.engine.installation import InstallExecutionBinding
from ctx.engine.protocol import (
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    EngineEvent,
    HostAction,
    Transition,
)
from ctx.engine.store import (
    ActivationActionAlreadyClaimed,
    ActivationActionClaimGuard,
    ActivationExecutionStatus,
    RevisionConflict,
)
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import reject_symlink_path

_SETTLEMENT_ATTEMPTS = 3
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class ActivationExecutionError(CtxEngineError):
    """Base class for coordinator-owned activation execution failures."""


class ActivationExecutionHandleConsumed(ActivationExecutionError):
    """A one-shot activation execution handle was invoked more than once."""


class ActivationExecutionProcessMismatch(ActivationExecutionError):
    """A handle was invoked from a process other than its creator."""


def _require_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ActivationExecutionError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationExecutionReport:
    """Safe result of an applied activation; no path, content, lease, or token."""

    outcome: Literal["applied"]
    claim_was_new: bool
    settled: bool
    execution_binding_digest: str
    verification_digest: str
    observed_material_identity_digest: str
    transition: Transition | None

    def __post_init__(self) -> None:
        if self.outcome != "applied":
            raise ValueError("activation execution report supports only the applied outcome")
        _require_digest(self.execution_binding_digest, "execution_binding_digest")
        _require_digest(self.verification_digest, "verification_digest")
        _require_digest(
            self.observed_material_identity_digest,
            "observed_material_identity_digest",
        )
        if self.transition is not None and not self.settled:
            raise ValueError("receipt transition requires a settled execution")


class _ActivationExecutionHandle:
    __slots__ = (
        "_action",
        "_binding",
        "_consume_lock",
        "_consumed",
        "_engine",
        "_expected_host_descriptor_digest",
        "_lock_target",
        "_observed_material_identity_digest",
        "_pid",
        "_verification_digest",
    )

    def __init__(
        self,
        *,
        engine: CtxEngine,
        action: HostAction,
        execution_binding: InstallExecutionBinding,
        expected_host_descriptor_digest: str,
        observed_material_identity_digest: str,
        verification_digest: str,
        lock_target: Path,
    ) -> None:
        self._engine = engine
        self._action = action
        self._binding = execution_binding
        self._expected_host_descriptor_digest = expected_host_descriptor_digest
        self._observed_material_identity_digest = observed_material_identity_digest
        self._verification_digest = verification_digest
        self._lock_target = Path(lock_target)
        self._pid = os.getpid()
        self._consumed = False
        self._consume_lock = Lock()

    def execute(self) -> ActivationExecutionReport:
        if os.getpid() != self._pid:
            raise ActivationExecutionProcessMismatch(
                "activation execution handle cannot cross a process boundary"
            )
        with self._consume_lock:
            if self._consumed:
                raise ActivationExecutionHandleConsumed(
                    "activation execution handle has already been consumed"
                )
            self._consumed = True
        with secure_file_lock(self._lock_target, timeout=30.0):
            return self._execute_locked()

    def _execute_locked(self) -> ActivationExecutionReport:
        status = self._engine.activation_execution_status(self._action)
        if status.claimed and status.execution_binding_digest != self._binding.binding_digest:
            raise ActivationExecutionError("durable activation claim names a different target")
        if status.settled:
            return self._status_report(status, claim_was_new=False, transition=None)

        claim_was_new = False
        if not status.claimed:
            try:
                self._engine.authorize_activation(
                    self._action,
                    execution_binding=self._binding,
                    expected_host_descriptor_digest=self._expected_host_descriptor_digest,
                )
                claim_was_new = True
            except ActivationActionAlreadyClaimed:
                claim_was_new = False
            status = self._engine.activation_execution_status(self._action)
            if (
                not status.claimed
                or status.execution_binding_digest != self._binding.binding_digest
            ):
                raise ActivationExecutionError("activation claim was not durably bound")

        if status.outcome_recorded:
            if status.outcome_digest is None:
                raise ActivationExecutionError("recorded activation outcome has no digest")
            guard = ActivationActionClaimGuard(
                action_id=self._action.action_id,
                action_content_digest=self._action.content_digest,
                mode="applied",
                execution_outcome_digest=status.outcome_digest,
            )
        else:
            permit = self._engine._issue_activation_outcome_permit(  # noqa: SLF001
                self._action,
                self._binding,
            )
            guard = self._engine._record_activation_outcome(  # noqa: SLF001
                self._action,
                execution_binding=self._binding,
                execution_authority=permit,
                observed_material_identity_digest=self._observed_material_identity_digest,
                verification_digest=self._verification_digest,
            )
        observed_at = self._engine.activation_execution_status(self._action).observed_at
        return self._settle_guard(guard, observed_at=observed_at, claim_was_new=claim_was_new)

    def _settle_guard(
        self,
        guard: ActivationActionClaimGuard,
        *,
        observed_at: str | None,
        claim_was_new: bool,
    ) -> ActivationExecutionReport:
        if observed_at is None:
            raise ActivationExecutionError("durable activation outcome has no observation time")
        for _ in range(_SETTLEMENT_ATTEMPTS):
            current = self._engine.activation_execution_status(self._action)
            if current.settled:
                return self._status_report(current, claim_was_new=claim_was_new, transition=None)
            snapshot = self._engine.snapshot(self._action.scope)
            receipt = _activation_receipt_event(
                action=self._action,
                guard=guard,
                expected_revision=snapshot.revision,
                observed_at=observed_at,
            )
            try:
                transition = self._engine.process_activation_receipt(receipt, guard)
            except RevisionConflict:
                continue
            return ActivationExecutionReport(
                outcome="applied",
                claim_was_new=claim_was_new,
                settled=True,
                execution_binding_digest=self._binding.binding_digest,
                verification_digest=self._verification_digest,
                observed_material_identity_digest=self._observed_material_identity_digest,
                transition=transition,
            )
        raise ActivationExecutionError("activation receipt settlement remained contended")

    def _status_report(
        self,
        status: ActivationExecutionStatus,
        *,
        claim_was_new: bool,
        transition: Transition | None,
    ) -> ActivationExecutionReport:
        if not status.settled or status.outcome_digest is None:
            raise ActivationExecutionError("settled activation has no durable outcome")
        return ActivationExecutionReport(
            outcome="applied",
            claim_was_new=claim_was_new,
            settled=True,
            execution_binding_digest=self._binding.binding_digest,
            verification_digest=self._verification_digest,
            observed_material_identity_digest=self._observed_material_identity_digest,
            transition=transition,
        )

    def __copy__(self) -> NoReturn:
        raise TypeError("activation execution handle cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("activation execution handle cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("activation execution handle cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("activation execution handle cannot be serialized")

    def __repr__(self) -> str:
        return "<one-shot-activation-execution>"


def prepare_activation_execution(
    *,
    engine: CtxEngine,
    action: HostAction,
    execution_binding: InstallExecutionBinding,
    expected_host_descriptor_digest: str,
    verification_digest: str,
) -> _ActivationExecutionHandle:
    """Create a non-executable applied-activation handle; no host mutation yet."""

    if not isinstance(engine, CtxEngine):
        raise TypeError("engine must be a CtxEngine")
    if not isinstance(action, HostAction):
        raise TypeError("action must be a HostAction")
    if not isinstance(execution_binding, InstallExecutionBinding):
        raise TypeError("execution_binding must be an InstallExecutionBinding")
    if not isinstance(expected_host_descriptor_digest, str):
        raise TypeError("expected_host_descriptor_digest must be a string")
    if (
        action.kind != "ActivateCapability"
        or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
    ):
        raise ActivationExecutionError("execution requires an exact schema-v3 activation action")
    material_identity = action.payload.get("material_identity")
    if not isinstance(material_identity, Mapping):
        raise ActivationExecutionError("activation action has no material identity")
    observed_material_identity_digest = _require_digest(
        material_identity.get("identity_digest"),
        "observed_material_identity_digest",
    )
    _require_digest(verification_digest, "verification_digest")
    # The per-action lock domain is keyed on the action id and owned by the
    # durable journal; the activation action id is distinct from any install
    # action, so reusing the journal's canonical execution lock is collision
    # free and gives cross-process settlement safety identical to install.
    lock_target = engine._install_execution_lock_target(action)  # noqa: SLF001
    reject_symlink_path(lock_target.parent)
    if not lock_target.parent.is_dir():
        raise ActivationExecutionError("canonical activation lock root does not exist")
    return _ActivationExecutionHandle(
        engine=engine,
        action=action,
        execution_binding=execution_binding,
        expected_host_descriptor_digest=expected_host_descriptor_digest,
        observed_material_identity_digest=observed_material_identity_digest,
        verification_digest=verification_digest,
        lock_target=lock_target,
    )


def _activation_receipt_event(
    *,
    action: HostAction,
    guard: ActivationActionClaimGuard,
    expected_revision: int,
    observed_at: str,
) -> EngineEvent:
    verification = action.verification
    if not isinstance(verification, Mapping) or "expected_state" not in verification:
        raise ActivationExecutionError("activation action has no verification expected state")
    payload: dict[str, object] = {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
        "verification": {
            "schema": MATERIAL_RECEIPT_SCHEMA_V3,
            "host_state": verification["expected_state"],
            "capability_id": action.entity_id,
            "capability_kind": action.payload["capability_kind"],
            "catalog_identity": action.payload["catalog_identity"],
            "material_identity": action.payload["material_identity"],
            "authorized_material": action.payload["authorized_material"],
        },
    }
    return EngineEvent(
        event_id=f"ctx-activation-receipt-{guard.execution_outcome_digest}",
        kind="ActionApplied",
        scope=action.scope,
        expected_revision=expected_revision,
        occurred_at=observed_at,
        payload=payload,
        privacy=action.privacy,
        correlation_id=action.plan_id,
        causation_id=action.action_id,
    )


__all__ = [
    "ActivationExecutionError",
    "ActivationExecutionHandleConsumed",
    "ActivationExecutionProcessMismatch",
    "ActivationExecutionReport",
    "prepare_activation_execution",
]
