"""Host-neutral activation receipt for the securely installed release skill.

Activation here means only durable CTX lifecycle registration.  It neither
loads skill bytes into a host nor claims that a provider observed them.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ctx.engine.engine import CtxEngine
from ctx.engine.installation import InstallExecutionBinding
from ctx.engine.planning_v3 import InstallPlanningAuthority
from ctx.engine.protocol import (
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    EngineEvent,
    HostAction,
    ScopeRef,
    Transition,
)
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION
from ctx.engine.replay import DefaultReplayInputFactory
from ctx.engine.state import CapabilityStateV3
from ctx.engine.store import (
    ActivationActionAlreadyClaimed,
    ActivationActionClaimExpired,
    ActivationActionClaimGuard,
    EngineStoreError,
    JournalRecord,
    RevisionConflict,
    SQLiteEngineStore,
    StreamId,
)
from ctx.runtime._skill_cas_posix import (
    RootIdentity,
    open_skill_cas_directory,
    skill_cas_root_identity,
)
from ctx.runtime.production_catalog import RELEASE_QUERY_CATALOG_ROOT_SHA256
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.release_skill_dispatcher import (
    ReleaseSkillDispatchError,
    ReleaseSkillInstallRequest,
    _release_skill_host_descriptor_digest,
    _scope,
)
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import reject_symlink_path


_DIGEST_CHARS = frozenset("0123456789abcdef")
_ACTIVATION_SETTLEMENT_ATTEMPTS = 3
_ACTIVATION_VERIFIER_ID = "ctx-release-skill-activation"
_ACTIVATION_VERIFIER_DIGEST = hashlib.sha256(
    b"ctx-release-skill-activation-verifier-v1"
).hexdigest()


class ReleaseSkillActivationError(RuntimeError):
    """Installed release-skill activation could not be proven exactly."""


class _ActivationLockChanged(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSkillActivationEvidence:
    """Authority-free digests proving CTX lifecycle activation, not exposure."""

    status: str
    capability_id: str
    release_root_digest: str
    activation_action_content_digest: str
    activation_receipt_content_digest: str
    activation_record_digest: str
    installed_lineage_digest: str
    material_identity_digest: str
    skill_cas_root_identity_digest: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.status != "active":
            raise ValueError("release skill activation evidence must be active")
        if self.capability_id != RELEASE_INSTALL_SKILL_ID:
            raise ValueError("release skill activation evidence has the wrong capability")
        if self.release_root_digest != RELEASE_QUERY_CATALOG_ROOT_SHA256:
            raise ValueError("release skill activation evidence has the wrong release root")
        for field_name in (
            "release_root_digest",
            "activation_action_content_digest",
            "activation_receipt_content_digest",
            "activation_record_digest",
            "installed_lineage_digest",
            "material_identity_digest",
            "skill_cas_root_identity_digest",
            "evidence_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)
        expected = _evidence_digest(
            capability_id=self.capability_id,
            release_root_digest=self.release_root_digest,
            activation_action_content_digest=self.activation_action_content_digest,
            activation_receipt_content_digest=self.activation_receipt_content_digest,
            activation_record_digest=self.activation_record_digest,
            installed_lineage_digest=self.installed_lineage_digest,
            material_identity_digest=self.material_identity_digest,
            skill_cas_root_identity_digest=self.skill_cas_root_identity_digest,
        )
        if self.evidence_digest != expected:
            raise ValueError("release skill activation evidence digest does not match")


def activate_installed_release_skill(
    request: ReleaseSkillInstallRequest,
    *,
    trusted_utc_now: Callable[[], datetime] | None = None,
) -> ReleaseSkillActivationEvidence:
    """Verify one installed release skill and receipt lifecycle activation.

    The request locates the same stable workspace, journal, and skill CAS used
    during installation.  No action, path, content, lease, or provider claim is
    returned to the caller.
    """

    if not isinstance(request, ReleaseSkillInstallRequest):
        raise TypeError("request must be a ReleaseSkillInstallRequest")
    if trusted_utc_now is not None and not callable(trusted_utc_now):
        raise TypeError("trusted_utc_now must be callable or None")
    if os.name == "nt":
        raise ReleaseSkillActivationError("release skill activation is not available on Windows")
    now_source = trusted_utc_now or (lambda: datetime.now(UTC))

    try:
        scope = _scope(request)
        _require_existing_journal(request.journal_path)
        store = SQLiteEngineStore(request.journal_path)
        engine = CtxEngine(
            store=store,
            replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
            trusted_utc_now=now_source,
        )
        root_identity = skill_cas_root_identity(request.skill_store_root)
        stream_id = StreamId.from_scope(scope)
        for _ in range(_ACTIVATION_SETTLEMENT_ATTEMPTS):
            lock_target, locked_material_digest = _activation_lock_target(
                engine=engine,
                root_identity=root_identity,
                scope=scope,
            )
            with secure_file_lock(lock_target, timeout=30.0):
                try:
                    return _activate_locked(
                        request=request,
                        engine=engine,
                        store=store,
                        stream_id=stream_id,
                        root_identity=root_identity,
                        locked_material_digest=locked_material_digest,
                    )
                except _ActivationLockChanged:
                    continue
        raise ReleaseSkillActivationError("release skill material lock remained contended")
    except ReleaseSkillActivationError:
        raise
    except ReleaseSkillDispatchError as exc:
        if str(exc) == "workspace identity changed or became unsafe":
            raise ReleaseSkillActivationError(str(exc)) from exc
        raise ReleaseSkillActivationError(
            "release skill activation request validation failed"
        ) from exc
    except ActivationActionClaimExpired as exc:
        raise ReleaseSkillActivationError("release skill activation authority expired") from exc
    except EngineStoreError as exc:
        message = str(exc)
        if message == "trusted UTC clock moved before installation":
            raise ReleaseSkillActivationError(message) from exc
        if message == "trusted UTC clock moved before activation claim":
            raise ReleaseSkillActivationError(message) from exc
        if message == "trusted UTC clock failed":
            raise ReleaseSkillActivationError(message) from exc
        raise ReleaseSkillActivationError(
            "release skill activation store validation failed"
        ) from exc
    except Exception as exc:
        raise ReleaseSkillActivationError("release skill activation operation failed") from exc


def inspect_activated_release_skill(
    request: ReleaseSkillInstallRequest,
) -> ReleaseSkillActivationEvidence:
    """Read and verify one already-active release skill without changing state."""

    if not isinstance(request, ReleaseSkillInstallRequest):
        raise TypeError("request must be a ReleaseSkillInstallRequest")
    if os.name == "nt":
        raise ReleaseSkillActivationError("release skill activation is not available on Windows")
    try:
        scope = _scope(request)
        _require_existing_journal(request.journal_path)
        store = SQLiteEngineStore.open_read_only(request.journal_path)
        engine = CtxEngine(
            store=store,
            replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
        )
        root_identity = skill_cas_root_identity(request.skill_store_root)
        stream_id = StreamId.from_scope(scope)
        for _ in range(_ACTIVATION_SETTLEMENT_ATTEMPTS):
            lock_target, locked_material_digest = _activation_lock_target(
                engine=engine,
                root_identity=root_identity,
                scope=scope,
            )
            with secure_file_lock(lock_target, timeout=30.0):
                try:
                    return _inspect_active_locked(
                        request=request,
                        engine=engine,
                        store=store,
                        stream_id=stream_id,
                        root_identity=root_identity,
                        locked_material_digest=locked_material_digest,
                    )
                except _ActivationLockChanged:
                    continue
        raise ReleaseSkillActivationError("release skill material lock remained contended")
    except ReleaseSkillActivationError:
        raise
    except ReleaseSkillDispatchError as exc:
        if str(exc) == "workspace identity changed or became unsafe":
            raise ReleaseSkillActivationError(str(exc)) from exc
        raise ReleaseSkillActivationError(
            "release skill activation request validation failed"
        ) from exc
    except EngineStoreError as exc:
        raise ReleaseSkillActivationError(
            "release skill activation store validation failed"
        ) from exc
    except Exception as exc:
        raise ReleaseSkillActivationError("release skill activation inspection failed") from exc


def _activation_lock_target(
    *,
    engine: CtxEngine,
    root_identity: RootIdentity,
    scope: ScopeRef,
) -> tuple[Path, str]:
    snapshot = engine.snapshot(scope)
    state = snapshot.state
    capability = None if state is None else state.capability(RELEASE_INSTALL_SKILL_ID)
    if not isinstance(capability, CapabilityStateV3):
        raise ReleaseSkillActivationError("release skill is absent from the exact journal")
    material_digest = capability.material_identity.content_sha256
    return Path(root_identity.canonical_root) / material_digest, material_digest


def _activate_locked(
    *,
    request: ReleaseSkillInstallRequest,
    engine: CtxEngine,
    store: SQLiteEngineStore,
    stream_id: StreamId,
    root_identity: RootIdentity,
    locked_material_digest: str,
) -> ReleaseSkillActivationEvidence:
    for _ in range(_ACTIVATION_SETTLEMENT_ATTEMPTS):
        snapshot = engine.snapshot(_scope(request))
        records = tuple(store.records(stream_id))
        if (
            len(records) != snapshot.revision
            or not records
            or records[-1].record_digest != snapshot.record_digest
        ):
            continue
        state = snapshot.state
        if state is None:
            raise ReleaseSkillActivationError("release skill is absent from the exact journal")
        capability = state.capability(RELEASE_INSTALL_SKILL_ID)
        if not isinstance(capability, CapabilityStateV3):
            raise ReleaseSkillActivationError("release skill is absent from the exact journal")
        if capability.installation != "installed" or capability.installed_lineage is None:
            raise ReleaseSkillActivationError("release skill has no exact installed lineage")
        if capability.material_identity.content_sha256 != locked_material_digest:
            raise _ActivationLockChanged
        expected_host_descriptor = _release_skill_host_descriptor_digest(request)
        if state.host_descriptor_digest != expected_host_descriptor:
            raise ReleaseSkillActivationError(
                "release skill journal is not bound to the current release root"
            )

        install_action = _exact_install_action(records, capability)
        _verify_install_target_binding(
            request=request,
            engine=engine,
            root_identity=root_identity,
            capability=capability,
            install_action=install_action,
        )
        activation_action = _exact_activation_action(records, capability)
        execution_binding = InstallExecutionBinding(
            driver_id=_ACTIVATION_VERIFIER_ID,
            driver_digest=_ACTIVATION_VERIFIER_DIGEST,
            host_identity_digest=request.host_identity_digest,
            target_identity_digest=root_identity.digest,
        )
        status = engine.activation_execution_status(activation_action)
        if capability.activation == "active" and not status.claimed:
            raise ReleaseSkillActivationError(
                "legacy active release skill has no verified activation authority"
            )
        if not status.claimed:
            try:
                engine.authorize_activation(
                    activation_action,
                    execution_binding=execution_binding,
                    expected_host_descriptor_digest=expected_host_descriptor,
                )
            except ActivationActionAlreadyClaimed:
                pass
            status = engine.activation_execution_status(activation_action)
        if (
            not status.claimed
            or status.execution_binding_digest != execution_binding.binding_digest
        ):
            raise ReleaseSkillActivationError(
                "activation claim does not match the verified CAS target"
            )
        verification_digest = _verify_exact_skill_material(root_identity, capability)

        if capability.activation == "active":
            if not status.settled or status.observed_at is None or status.outcome_digest is None:
                raise ReleaseSkillActivationError(
                    "active release skill lacks a settled verified activation outcome"
                )
            receipt_record = _exact_activation_receipt(
                records=records,
                action=activation_action,
                observed_at=status.observed_at,
                outcome_digest=status.outcome_digest,
            )
            return _activation_evidence(
                capability=capability,
                action=activation_action,
                receipt_record=receipt_record,
                root_identity=root_identity,
            )
        if capability.activation != "inactive":
            raise ReleaseSkillActivationError("release skill activation state is unsupported")
        pending = tuple(
            item.action
            for item in state.pending_effects
            if item.effect == "activate"
            and item.action.action_id == activation_action.action_id
            and item.action.content_digest == activation_action.content_digest
        )
        if len(pending) != 1:
            raise ReleaseSkillActivationError(
                "release skill has no one exact pending activation authority"
            )
        if not status.outcome_recorded:
            permit = engine._issue_activation_outcome_permit(  # noqa: SLF001
                activation_action,
                execution_binding,
            )
            guard = engine._record_activation_outcome(  # noqa: SLF001
                activation_action,
                execution_binding=execution_binding,
                execution_authority=permit,
                observed_material_identity_digest=capability.material_identity.identity_digest,
                verification_digest=verification_digest,
            )
            status = engine.activation_execution_status(activation_action)
        else:
            if status.outcome_digest is None:
                raise ReleaseSkillActivationError("activation has no applied verified outcome")
            guard = ActivationActionClaimGuard(
                action_id=activation_action.action_id,
                action_content_digest=activation_action.content_digest,
                mode="applied",
                execution_outcome_digest=status.outcome_digest,
            )
        if status.observed_at is None:
            raise ReleaseSkillActivationError("activation outcome has no trusted observation time")
        receipt = _activation_receipt_event(
            activation_action,
            expected_revision=snapshot.revision,
            observed_at=status.observed_at,
            execution_outcome_digest=guard.execution_outcome_digest,
        )
        try:
            engine.process_activation_receipt(receipt, guard)
        except RevisionConflict:
            continue

    raise ReleaseSkillActivationError("release skill activation receipt remained contended")


def _inspect_active_locked(
    *,
    request: ReleaseSkillInstallRequest,
    engine: CtxEngine,
    store: SQLiteEngineStore,
    stream_id: StreamId,
    root_identity: RootIdentity,
    locked_material_digest: str,
) -> ReleaseSkillActivationEvidence:
    """Re-derive settled activation evidence without claiming or recording anything."""

    snapshot = engine.snapshot(_scope(request))
    records = tuple(store.records(stream_id))
    if (
        len(records) != snapshot.revision
        or not records
        or records[-1].record_digest != snapshot.record_digest
    ):
        raise ReleaseSkillActivationError("release skill activation journal changed")
    state = snapshot.state
    if state is None:
        raise ReleaseSkillActivationError("release skill is absent from the exact journal")
    capability = state.capability(RELEASE_INSTALL_SKILL_ID)
    if not isinstance(capability, CapabilityStateV3):
        raise ReleaseSkillActivationError("release skill is absent from the exact journal")
    if capability.installation != "installed" or capability.installed_lineage is None:
        raise ReleaseSkillActivationError("release skill has no exact installed lineage")
    if capability.material_identity.content_sha256 != locked_material_digest:
        raise _ActivationLockChanged
    if capability.activation != "active":
        raise ReleaseSkillActivationError("release skill is not already active")
    expected_host_descriptor = _release_skill_host_descriptor_digest(request)
    if state.host_descriptor_digest != expected_host_descriptor:
        raise ReleaseSkillActivationError(
            "release skill journal is not bound to the current release root"
        )

    install_action = _exact_install_action(records, capability)
    _verify_install_target_binding(
        request=request,
        engine=engine,
        root_identity=root_identity,
        capability=capability,
        install_action=install_action,
    )
    activation_action = _exact_activation_action(records, capability)
    execution_binding = InstallExecutionBinding(
        driver_id=_ACTIVATION_VERIFIER_ID,
        driver_digest=_ACTIVATION_VERIFIER_DIGEST,
        host_identity_digest=request.host_identity_digest,
        target_identity_digest=root_identity.digest,
    )
    status = engine.activation_execution_status(activation_action)
    if (
        not status.claimed
        or not status.settled
        or status.execution_binding_digest != execution_binding.binding_digest
        or status.observed_at is None
        or status.outcome_digest is None
    ):
        raise ReleaseSkillActivationError(
            "active release skill lacks settled verified activation authority"
        )
    _verify_exact_skill_material(root_identity, capability)
    receipt_record = _exact_activation_receipt(
        records=records,
        action=activation_action,
        observed_at=status.observed_at,
        outcome_digest=status.outcome_digest,
    )
    return _activation_evidence(
        capability=capability,
        action=activation_action,
        receipt_record=receipt_record,
        root_identity=root_identity,
    )


def _exact_install_action(
    records: tuple[JournalRecord, ...],
    capability: CapabilityStateV3,
) -> HostAction:
    lineage = capability.installed_lineage
    assert lineage is not None
    matches = tuple(
        action
        for record in records
        for action in Transition.from_json(record.transition_json).actions
        if action.kind == "InstallCapability"
        and action.entity_id == capability.capability_id
        and action.content_digest == lineage.install_action_content_digest
    )
    if len(matches) != 1:
        raise ReleaseSkillActivationError("installed lineage has no one exact install action")
    action = matches[0]
    if (
        action.payload.get("result_material") != capability.material_identity.to_dict()
        or action.payload.get("catalog_identity") != capability.catalog_identity.to_dict()
    ):
        raise ReleaseSkillActivationError("installed lineage contradicts its install action")
    return action


def _verify_install_target_binding(
    *,
    request: ReleaseSkillInstallRequest,
    engine: CtxEngine,
    root_identity: RootIdentity,
    capability: CapabilityStateV3,
    install_action: HostAction,
) -> str:
    authority = capability.selection.authority
    installer_digest = install_action.payload.get("installer_digest")
    if not isinstance(authority, InstallPlanningAuthority) or not isinstance(installer_digest, str):
        raise ReleaseSkillActivationError("installed release skill lacks installer authority")
    binding = InstallExecutionBinding(
        driver_id=authority.descriptor.installer_id,
        driver_digest=installer_digest,
        host_identity_digest=request.host_identity_digest,
        target_identity_digest=root_identity.digest,
    )
    status = engine.install_execution_status(install_action)
    if (
        not status.claimed
        or not status.outcome_recorded
        or not status.settled
        or status.outcome != "applied"
        or status.execution_binding_digest != binding.binding_digest
        or status.observed_at is None
    ):
        raise ReleaseSkillActivationError(
            "current skill CAS does not match the durable install target identity"
        )
    return status.observed_at


def _verify_exact_skill_material(
    root_identity: RootIdentity,
    capability: CapabilityStateV3,
) -> str:
    current = skill_cas_root_identity(Path(root_identity.canonical_root))
    if current != root_identity:
        raise ReleaseSkillActivationError("skill CAS root identity changed before activation")
    material = capability.material_identity
    with open_skill_cas_directory(root_identity) as directory:
        inspection = directory.inspect_exact_utf8(
            material.content_sha256,
            expected_sha256=material.content_sha256,
            expected_bytes=material.content_bytes,
            allowed_links=frozenset({1}),
        )
        directory.revalidate_root()
    if inspection.state != "exact" or inspection.identity is None:
        raise ReleaseSkillActivationError(
            "skill CAS does not contain exact installed UTF-8 material"
        )
    return _canonical_digest(
        {
            "action": "activate",
            "capability_id": capability.capability_id,
            "file_device": inspection.identity.device,
            "file_inode": inspection.identity.inode,
            "material_identity_digest": material.identity_digest,
            "root_identity_digest": root_identity.digest,
            "schema": "ctx.release-skill-activation-observation-v1",
            "state": "exact-installed-utf8",
        }
    )


def _exact_activation_action(
    records: tuple[JournalRecord, ...],
    capability: CapabilityStateV3,
) -> HostAction:
    current = capability.current_authorized_material
    assert current is not None
    matches = tuple(
        action
        for record in records
        for action in Transition.from_json(record.transition_json).actions
        if action.kind == "ActivateCapability"
        and action.entity_id == capability.capability_id
        and action.payload.get("schema") == MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
        and action.payload.get("capability_kind") == capability.kind
        and action.payload.get("catalog_identity") == capability.catalog_identity.to_dict()
        and action.payload.get("material_identity") == capability.material_identity.to_dict()
        and action.payload.get("authorized_material") == current.to_dict()
        and (
            capability.activation_lease_id is None
            or action.lease_id == capability.activation_lease_id
        )
    )
    if len(matches) != 1:
        raise ReleaseSkillActivationError("release skill has no one exact activation action")
    return matches[0]


def _activation_receipt_event(
    action: HostAction,
    *,
    expected_revision: int,
    observed_at: str,
    execution_outcome_digest: str | None = None,
) -> EngineEvent:
    return EngineEvent(
        event_id=_activation_receipt_event_id(action, execution_outcome_digest),
        kind="ActionApplied",
        scope=action.scope,
        expected_revision=expected_revision,
        occurred_at=observed_at,
        payload={
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "verification": {
                "schema": MATERIAL_RECEIPT_SCHEMA_V3,
                "host_state": "active",
                "capability_id": action.entity_id,
                "capability_kind": action.payload["capability_kind"],
                "catalog_identity": action.payload["catalog_identity"],
                "material_identity": action.payload["material_identity"],
                "authorized_material": action.payload["authorized_material"],
            },
        },
        privacy=action.privacy,
        correlation_id=action.plan_id,
        causation_id=action.action_id,
    )


def _exact_activation_receipt(
    *,
    records: tuple[JournalRecord, ...],
    action: HostAction,
    observed_at: str,
    outcome_digest: str,
) -> JournalRecord:
    legacy_event_id = _activation_receipt_event_id(action, None)
    current_event_id = _activation_receipt_event_id(action, outcome_digest)
    candidates = tuple(
        record for record in records if record.event_id in {legacy_event_id, current_event_id}
    )
    if len(candidates) != 1:
        raise ReleaseSkillActivationError("active release skill lacks one exact receipt record")
    record = candidates[0]
    expected = _activation_receipt_event(
        action,
        expected_revision=record.revision - 1,
        observed_at=observed_at,
        execution_outcome_digest=(None if record.event_id == legacy_event_id else outcome_digest),
    )
    transition = Transition.from_json(record.transition_json)
    if (
        record.event_content_digest != expected.content_digest
        or transition.event_id != expected.event_id
        or transition.from_revision != expected.expected_revision
        or transition.actions
    ):
        raise ReleaseSkillActivationError("activation receipt record is not exact")
    return record


def _activation_receipt_event_id(
    action: HostAction,
    execution_outcome_digest: str | None,
) -> str:
    if execution_outcome_digest is None:
        identity_digest = action.content_digest
    else:
        identity_digest = _canonical_digest(
            {
                "action_content_digest": action.content_digest,
                "execution_outcome_digest": _require_digest(
                    execution_outcome_digest,
                    "outcome_digest",
                ),
                "schema": "ctx.activation-receipt-identity-v1",
            }
        )
    return f"ctx-release-activation-receipt-{identity_digest}"


def _activation_evidence(
    *,
    capability: CapabilityStateV3,
    action: HostAction,
    receipt_record: JournalRecord,
    root_identity: RootIdentity,
) -> ReleaseSkillActivationEvidence:
    lineage = capability.installed_lineage
    assert lineage is not None
    fields: dict[str, str] = {
        "capability_id": capability.capability_id,
        "release_root_digest": RELEASE_QUERY_CATALOG_ROOT_SHA256,
        "activation_action_content_digest": action.content_digest,
        "activation_receipt_content_digest": receipt_record.event_content_digest,
        "activation_record_digest": receipt_record.record_digest,
        "installed_lineage_digest": lineage.lineage_digest,
        "material_identity_digest": capability.material_identity.identity_digest,
        "skill_cas_root_identity_digest": root_identity.digest,
    }
    return ReleaseSkillActivationEvidence(
        status="active",
        **fields,
        evidence_digest=_evidence_digest(**fields),
    )


def _evidence_digest(
    *,
    capability_id: str,
    release_root_digest: str,
    activation_action_content_digest: str,
    activation_receipt_content_digest: str,
    activation_record_digest: str,
    installed_lineage_digest: str,
    material_identity_digest: str,
    skill_cas_root_identity_digest: str,
) -> str:
    return _canonical_digest(
        {
            "activation_action_content_digest": activation_action_content_digest,
            "activation_receipt_content_digest": activation_receipt_content_digest,
            "activation_record_digest": activation_record_digest,
            "capability_id": capability_id,
            "installed_lineage_digest": installed_lineage_digest,
            "material_identity_digest": material_identity_digest,
            "release_root_digest": release_root_digest,
            "schema": "ctx.release-skill-activation-evidence-v1",
            "skill_cas_root_identity_digest": skill_cas_root_identity_digest,
            "status": "active",
        }
    )


def _require_existing_journal(path: Path) -> None:
    try:
        reject_symlink_path(path)
        metadata = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError):
        raise ReleaseSkillActivationError("release skill journal is absent or unsafe") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ReleaseSkillActivationError("release skill journal is absent or unsafe")


def _canonical_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_digest(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST_CHARS for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


__all__ = [
    "ReleaseSkillActivationError",
    "ReleaseSkillActivationEvidence",
    "activate_installed_release_skill",
]
