"""Pure deterministic state reduction for current-work capability sessions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, NoReturn, cast

from .content import AuthorizedMaterial, MaterialIdentity
from .lineage import (
    CapabilityLineageBinding,
    InstalledMaterialLineage,
    classify_lineage_transition,
)
from .planning_v3 import (
    InstallPlanningAuthority,
    LoadPlanningAuthority,
    ManualPlanningAuthority,
)
from .protocol import (
    INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    INSTALL_CONSENT_REQUEST_SCHEMA_V3,
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    PROMPT_CONTEXT_ACTION_PAYLOAD_SCHEMA_V1,
    PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
    EngineEvent,
    HostAction,
    ScopeRef,
    Transition,
)
from .replay import ReplayInput
from .state import (
    HOST_LEVELS,
    CapabilityEvidence,
    CapabilityState,
    CapabilityStateV3,
    CommittedPlanV3,
    EngineState,
    LeaseRef,
    PendingConsent,
    PendingEffect,
    PlanCapability,
    PlanCapabilityV3,
)


MAX_ACTIVE_CAPABILITIES = 5
PLANNING_REDUCER_VERSION = "ctx-reducer-v2"
INSTALLATION_REDUCER_VERSION = "ctx-reducer-v3"
PROMPT_CONTEXT_REDUCER_VERSION = "ctx-reducer-v4"
INSTALLER_ID = "ctx-install-plan-executor-v1"
INSTALLER_DIGEST = hashlib.sha256(
    b"ctx-install-plan-executor-v1:typed-plan-only:no-raw-command"
).hexdigest()
_RECEIPT_KINDS = frozenset({"ActionApplied", "ActionFailed", "ActionExpired"})
_PLANNING_EVENT_KINDS = frozenset({"IntentObserved", "DevelopmentObserved"})


class ReducerError(ValueError):
    """Base class for deterministic reducer rejection."""


class InvalidEventError(ReducerError):
    """An event is well-formed protocol data but invalid for this state."""


class RevisionConflictError(ReducerError):
    """An event was derived from a different state revision."""


def _invalid(message: str) -> NoReturn:
    raise InvalidEventError(message)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be a non-empty trimmed string")
    return value


def _required_sha256(value: object, field_name: str) -> str:
    value = _required_text(value, field_name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _invalid(f"{field_name} must be an array")
    return value


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _invalid(f"{field_name} must be an object")
    return value


def _thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _replace_capability(
    state: EngineState,
    capability: CapabilityState | CapabilityStateV3,
) -> EngineState:
    capabilities = {item.capability_id: item for item in state.capabilities}
    capabilities[capability.capability_id] = capability
    return replace(
        state,
        capabilities=tuple(capabilities[key] for key in sorted(capabilities)),
    )


def _replace_evidence(
    state: EngineState,
    evidence: CapabilityEvidence,
) -> EngineState:
    values = {(item.exposure_id, item.capability_id): item for item in state.evidence}
    values[(evidence.exposure_id, evidence.capability_id)] = evidence
    return replace(state, evidence=tuple(values[key] for key in sorted(values)))


def _same_session(left: ScopeRef, right: ScopeRef) -> bool:
    return (
        left.tenant_id,
        left.workspace_id,
        left.repository_id,
        left.session_id,
    ) == (
        right.tenant_id,
        right.workspace_id,
        right.repository_id,
        right.session_id,
    )


def _action_id(
    event: EngineEvent,
    kind: str,
    capability_id: str | None,
    ordinal: int,
) -> str:
    material = ":".join(
        (
            "ctx-engine-action-v1",
            event.content_digest,
            kind,
            capability_id or "session",
            str(ordinal),
        )
    )
    return "action-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _make_action(
    *,
    event: EngineEvent,
    revision: int,
    kind: str,
    capability: CapabilityState | CapabilityStateV3 | None,
    ordinal: int,
    lease_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    rollback: Mapping[str, Any] | None = None,
) -> HostAction:
    capability_id = None if capability is None else capability.capability_id
    source_digest = None if capability is None else capability.source_digest
    physical = kind in {
        "ActivateCapability",
        "PrepareExposure",
        "DeactivateCapability",
    }
    expected_state = {
        "ActivateCapability": "active",
        "PrepareExposure": "prepared",
        "DeactivateCapability": "inactive",
    }.get(kind)
    expires_at = None
    if physical:
        occurred_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        expires_at = (occurred_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    return HostAction(
        action_id=_action_id(event, kind, capability_id, ordinal),
        kind=kind,
        scope=event.scope,
        precondition_revision=revision,
        entity_id=capability_id,
        source_digest=source_digest,
        plan_id=(None if capability is None else capability.plan_id),
        catalog_snapshot_id=(None if capability is None else capability.catalog_snapshot_id),
        lease_id=lease_id,
        expires_at=expires_at,
        required_host_feature=("activation" if physical else None),
        payload=payload or {},
        verification=(
            {"receipt_required": True, "expected_state": expected_state} if physical else {}
        ),
        rollback=rollback or {},
        privacy=event.privacy,
    )


def _consent_id(event: EngineEvent, capability_id: str) -> str:
    material = f"ctx-engine-consent-v1:{event.content_digest}:{capability_id}"
    return "consent-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _make_install_consent(
    *,
    event: EngineEvent,
    revision: int,
    capability: CapabilityState,
    policy_snapshot_digest: str,
) -> tuple[HostAction, HostAction]:
    """Precompute and bind the exact install that one later decision may release."""

    if (
        capability.actionability != "install"
        or capability.install_plan_digest is None
        or capability.install_descriptor_digest is None
    ):
        _invalid("install consent requires an installable committed capability")
    consent_id = _consent_id(event, capability.capability_id)
    occurred_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    expires_at = (occurred_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    # This identity names the host-neutral typed-plan executor, not a package
    # manager or graph-provided command. The authenticated install-plan digest
    # remains the sole adapter-specific authority.
    descriptor = {
        "install_descriptor_digest": capability.install_descriptor_digest,
        "install_plan_digest": capability.install_plan_digest,
        "installer_id": INSTALLER_ID,
        "installer_digest": INSTALLER_DIGEST,
        "policy_snapshot_digest": _required_sha256(
            policy_snapshot_digest,
            "policy_snapshot_digest",
        ),
    }
    install = HostAction(
        action_id=_action_id(event, "InstallCapability", capability.capability_id, 0),
        kind="InstallCapability",
        scope=event.scope,
        precondition_revision=revision + 1,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        consent_id=consent_id,
        expires_at=expires_at,
        required_host_feature="installation",
        payload=descriptor,
        verification={"receipt_required": True, "expected_state": "installed"},
        rollback={"kind": "UninstallCapability", "installer_id": INSTALLER_ID},
        privacy=event.privacy,
    )
    requested_identity = {
        "requested_action_id": install.action_id,
        "requested_action_kind": install.kind,
        "requested_action_content_digest": install.content_digest,
        "requested_action_precondition_revision": install.precondition_revision,
    }
    request = HostAction(
        action_id=_action_id(event, "RequestConsent", capability.capability_id, 0),
        kind="RequestConsent",
        scope=event.scope,
        precondition_revision=revision,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        consent_id=consent_id,
        required_host_feature="installation-consent",
        payload={**requested_identity, **descriptor},
        privacy=event.privacy,
    )
    return request, install


def _pending_for(
    state: EngineState,
    capability_id: str,
    *effects: str,
) -> bool:
    return any(
        pending.action.entity_id == capability_id and pending.effect in effects
        for pending in state.pending_effects
    )


def _with_pending(
    state: EngineState,
    action: HostAction,
    *,
    effect: str,
    rollback_capability_id: str | None = None,
) -> EngineState:
    return replace(
        state,
        pending_effects=(
            *state.pending_effects,
            PendingEffect(
                action=action,
                effect=effect,
                rollback_capability_id=rollback_capability_id,
            ),
        ),
    )


def _manual_bundle(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    bundle = tuple(sorted(state.desired_capability_ids))[:MAX_ACTIVE_CAPABILITIES]
    diagnostics: list[Mapping[str, Any]] = [
        {
            "code": "host_activation_unsupported",
            "host_level": state.host_level,
        }
    ]
    if len(state.desired_capability_ids) > MAX_ACTIVE_CAPABILITIES:
        diagnostics.append(
            {
                "code": "active_capability_budget_exhausted",
                "limit": MAX_ACTIVE_CAPABILITIES,
            }
        )
    if not bundle or bundle == state.last_manual_bundle:
        return state, (), tuple(diagnostics)
    action = _make_action(
        event=event,
        revision=revision,
        kind="PresentBundle",
        capability=None,
        ordinal=0,
        payload={
            "capabilities": [
                {"capability_id": capability_id, "manual": True} for capability_id in bundle
            ]
        },
    )
    return replace(state, last_manual_bundle=bundle), (action,), tuple(diagnostics)


def _reconcile(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if state.session_status == "ended":
        blocked_active = tuple(
            capability
            for capability in cast(tuple[CapabilityState, ...], state.capabilities)
            if capability.activation == "active"
            and capability.capability_id in state.blocked_deactivation_ids
        )
        if blocked_active:
            manual = next(
                (
                    capability
                    for capability in blocked_active
                    if capability.capability_id not in state.terminal_cleanup_notified_ids
                ),
                None,
            )
            if manual is None:
                return state, (), ()
            action = _make_action(
                event=event,
                revision=revision,
                kind="Notify",
                capability=None,
                ordinal=0,
                payload={
                    "message": (
                        "CTX could not deactivate "
                        f"{manual.capability_id}; manual cleanup is required"
                    )
                },
            )
            state = replace(
                state,
                terminal_cleanup_notified_ids=(
                    *state.terminal_cleanup_notified_ids,
                    manual.capability_id,
                ),
            )
            return (
                state,
                (action,),
                (
                    {
                        "code": "terminal_cleanup_failed_manual_recovery_required",
                        "capability_id": manual.capability_id,
                    },
                ),
            )
        cleanup = next(
            (
                capability
                for capability in state.capabilities
                if capability.activation == "active"
                and capability.capability_id not in state.blocked_deactivation_ids
                and not _pending_for(state, capability.capability_id, "deactivate")
                and not _pending_for(state, capability.capability_id, "prepare")
            ),
            None,
        )
        if cleanup is None:
            return state, (), ()
        action = _make_action(
            event=event,
            revision=revision,
            kind="DeactivateCapability",
            capability=cleanup,
            ordinal=0,
            lease_id=(cleanup.activation_lease_id or f"session-end:{cleanup.capability_id}"),
            rollback={
                "kind": "ActivateCapability",
                "source_digest": cleanup.source_digest,
            },
        )
        return (
            _with_pending(state, action, effect="deactivate"),
            (action,),
            (),
        )
    if state.host_level not in {"activating", "managing"}:
        return _manual_bundle(state, event, revision)

    actions: list[HostAction] = []
    diagnostics: list[Mapping[str, Any]] = []

    # Consent is serialized because each request binds an install action to the
    # immediately following committed revision. An intervening event therefore
    # invalidates the decision instead of silently retargeting host mutation.
    if state.pending_consents:
        return state, (), ()
    absent_install = next(
        (
            capability
            for capability in cast(tuple[CapabilityState, ...], state.capabilities)
            if capability.desired
            and capability.installation == "absent"
            and capability.actionability == "install"
            and capability.install_descriptor_digest not in state.blocked_install_descriptor_digests
            and not _pending_for(state, capability.capability_id, "install")
        ),
        None,
    )
    if absent_install is not None:
        if state.install_policy_snapshot_digest is None:
            _invalid("install consent requires a committed policy snapshot")
        request, install = _make_install_consent(
            event=event,
            revision=revision,
            capability=absent_install,
            policy_snapshot_digest=state.install_policy_snapshot_digest,
        )
        state = replace(
            state,
            pending_consents=(
                *state.pending_consents,
                PendingConsent(consent_id=request.consent_id or "", install_action=install),
            ),
        )
        return state, (request,), ()

    # A failed replacement asks for rollback before any new activation work.
    for capability_id in state.rollback_requested_capability_ids:
        capability = state.capability(capability_id)
        if (
            capability is None
            or capability.activation == "active"
            or _pending_for(state, capability_id, "activate", "rollback-activate")
        ):
            continue
        action = _make_action(
            event=event,
            revision=revision,
            kind="ActivateCapability",
            capability=capability,
            ordinal=len(actions),
            lease_id=f"rollback:{capability_id}",
            rollback={"kind": "DeactivateCapability"},
        )
        state = _with_pending(state, action, effect="rollback-activate")
        actions.append(action)
        break
    if state.rollback_requested_capability_ids or any(
        pending.effect == "rollback-activate" for pending in state.pending_effects
    ):
        return state, tuple(actions), ()

    inactive_desired = [
        capability
        for capability in state.capabilities
        if capability.desired
        and capability.actionability != "manual"
        and capability.installation == "installed"
        and capability.activation == "inactive"
        and capability.capability_id not in state.blocked_capability_ids
        and not _pending_for(state, capability.capability_id, "activate")
    ]
    active_undesired = [
        capability
        for capability in state.capabilities
        if capability.activation == "active"
        and not capability.desired
        and not capability.rollback_held
        and capability.capability_id not in state.blocked_deactivation_ids
        and not _pending_for(state, capability.capability_id, "deactivate")
        and not _pending_for(state, capability.capability_id, "prepare")
    ]

    replacement_in_progress = any(
        capability.rollback_held and capability.activation == "inactive"
        for capability in state.capabilities
    )

    # Replacement is deliberately sequential unless an adapter later declares
    # an atomic-swap feature. Never request a sixth physical activation.
    if active_undesired and not replacement_in_progress:
        replacing = bool(inactive_desired)
        capability = active_undesired[0]
        replacement_owner_id = inactive_desired[0].leases[0].owner_id if replacing else None
        updated = replace(
            capability,
            rollback_held=replacing,
            rollback_owner_id=replacement_owner_id,
        )
        state = _replace_capability(state, updated)
        action = _make_action(
            event=event,
            revision=revision,
            kind="DeactivateCapability",
            capability=updated,
            ordinal=0,
            lease_id=(updated.activation_lease_id or f"release:{updated.capability_id}"),
            rollback={
                "kind": "ActivateCapability",
                "source_digest": updated.source_digest,
            },
        )
        state = _with_pending(state, action, effect="deactivate")
        actions.append(action)
        return state, tuple(actions), ()

    active_count = len(state.active_capability_ids)
    reserved_count = sum(
        pending.effect in {"activate", "rollback-activate"} for pending in state.pending_effects
    )
    capacity = max(0, MAX_ACTIVE_CAPABILITIES - active_count - reserved_count)
    for capability in inactive_desired[:capacity]:
        assigned_rollbacks = {
            pending.rollback_capability_id
            for pending in state.pending_effects
            if pending.effect == "activate" and pending.rollback_capability_id is not None
        }
        rollback_capability = next(
            (
                candidate
                for candidate in state.capabilities
                if candidate.rollback_held
                and candidate.activation == "inactive"
                and candidate.capability_id not in assigned_rollbacks
            ),
            None,
        )
        lease_id = capability.leases[0].lease_id
        action = _make_action(
            event=event,
            revision=revision,
            kind="ActivateCapability",
            capability=capability,
            ordinal=len(actions),
            lease_id=lease_id,
            rollback={"kind": "DeactivateCapability"},
        )
        state = _with_pending(
            state,
            action,
            effect="activate",
            rollback_capability_id=(
                None if rollback_capability is None else rollback_capability.capability_id
            ),
        )
        actions.append(action)

    if len(inactive_desired) > capacity:
        diagnostics.append(
            {
                "code": "active_capability_budget_exhausted",
                "limit": MAX_ACTIVE_CAPABILITIES,
                "deferred": len(inactive_desired) - capacity,
            }
        )
    return state, tuple(actions), tuple(diagnostics)


def _update_desired(state: EngineState, event: EngineEvent) -> EngineState:
    plan_id = _required_text(event.correlation_id, "correlation_id")
    catalog_snapshot_id = _required_text(
        event.catalog_snapshot_digest,
        "catalog_snapshot_digest",
    )
    owner_id = _required_text(event.payload.get("owner_id"), "owner_id")
    raw_desired = _sequence(
        event.payload.get("desired_capabilities"),
        "desired_capabilities",
    )
    desired: list[tuple[str, str, str]] = []
    lease_ids: set[str] = set()
    for index, raw in enumerate(raw_desired):
        item = _mapping(raw, f"desired_capabilities[{index}]")
        capability_id = _required_text(
            item.get("capability_id"),
            f"desired_capabilities[{index}].capability_id",
        )
        source_digest = _required_text(
            item.get("source_digest"),
            f"desired_capabilities[{index}].source_digest",
        )
        lease_id = _required_text(
            item.get("lease_id"),
            f"desired_capabilities[{index}].lease_id",
        )
        if lease_id in lease_ids:
            _invalid(f"duplicate lease_id {lease_id!r}")
        lease_ids.add(lease_id)
        desired.append((capability_id, source_digest, lease_id))

    existing_lease_owners = {
        lease.lease_id: (capability.capability_id, lease.owner_id)
        for capability in state.capabilities
        for lease in capability.leases
        if lease.owner_id != owner_id
    }
    for capability_id, _source_digest, lease_id in desired:
        if lease_id in existing_lease_owners:
            _invalid(
                f"lease_id {lease_id!r} already belongs to {existing_lease_owners[lease_id][0]!r}"
            )

    protected_rollbacks = {
        capability_id
        for pending in state.pending_effects
        for capability_id in (
            pending.action.entity_id,
            pending.rollback_capability_id,
        )
        if capability_id is not None
        and pending.effect in {"deactivate", "activate", "rollback-activate"}
    }
    protected_rollbacks.update(state.rollback_requested_capability_ids)
    capabilities = {
        capability.capability_id: replace(
            capability,
            rollback_held=(
                capability.rollback_held
                if capability.rollback_owner_id != owner_id
                or capability.capability_id in protected_rollbacks
                else False
            ),
            rollback_owner_id=(
                capability.rollback_owner_id
                if capability.rollback_owner_id != owner_id
                or capability.capability_id in protected_rollbacks
                else None
            ),
            leases=tuple(lease for lease in capability.leases if lease.owner_id != owner_id),
        )
        for capability in state.capabilities
    }
    for capability_id, source_digest, lease_id in desired:
        capability = capabilities.get(capability_id)
        if capability is None:
            capability = CapabilityState(
                capability_id=capability_id,
                source_digest=source_digest,
                plan_id=plan_id,
                catalog_snapshot_id=catalog_snapshot_id,
                kind=capability_id.split(":", 1)[0],
            )
        elif capability.source_digest != source_digest:
            _invalid(f"source_digest for {capability_id!r} does not match session identity")
        lease = LeaseRef(
            lease_id=lease_id,
            owner_id=owner_id,
            exposure_id=event.scope.exposure_id,
        )
        capability = replace(
            capability,
            leases=tuple(sorted((*capability.leases, lease), key=lambda item: item.lease_id)),
        )
        capabilities[capability_id] = capability

    return replace(
        state,
        capabilities=tuple(capabilities[key] for key in sorted(capabilities)),
        blocked_capability_ids=tuple(
            capability_id
            for capability_id in state.blocked_capability_ids
            if capability_id in capabilities and capabilities[capability_id].desired
        ),
        blocked_deactivation_ids=(),
    )


def _matches_plan_identity(capability: CapabilityState, selection: PlanCapability) -> bool:
    return (
        capability.source_digest,
        capability.kind,
        capability.actionability,
        capability.install_descriptor_digest,
        capability.install_plan_digest,
    ) == (
        selection.source_digest,
        selection.kind,
        selection.actionability,
        selection.install_descriptor_digest,
        selection.install_plan_digest,
    )


def _v3_capability(state: EngineState, capability_id: str) -> CapabilityStateV3 | None:
    capability = state.capability(capability_id)
    if capability is None:
        return None
    if not isinstance(capability, CapabilityStateV3):
        raise AssertionError("schema-v3 state contains a historical capability")
    return capability


def _replace_capability_v3(state: EngineState, capability: CapabilityStateV3) -> EngineState:
    capabilities = {item.capability_id: item for item in state.capabilities}
    capabilities[capability.capability_id] = capability
    return replace(
        state,
        capabilities=tuple(capabilities[key] for key in sorted(capabilities)),
    )


def _can_prune_retired_v3(state: EngineState, capability: CapabilityStateV3) -> bool:
    plan = state.committed_plan
    selected_ids = (
        {item.capability_id for item in plan.capabilities}
        if isinstance(plan, CommittedPlanV3)
        else set()
    )
    return bool(
        isinstance(plan, CommittedPlanV3)
        and plan.status in {"ready", "abstained"}
        and capability.capability_id not in selected_ids
        and capability.activation == "inactive"
        and not capability.leases
        and not capability.rollback_held
        and capability.capability_id not in state.rollback_requested_capability_ids
        and not any(
            pending.action.entity_id == capability.capability_id
            for pending in state.pending_effects
        )
        and not any(
            pending.install_action.entity_id == capability.capability_id
            for pending in state.pending_consents
        )
    )


def _remove_capability_v3(state: EngineState, capability: CapabilityStateV3) -> EngineState:
    capability_id = capability.capability_id
    descriptor_digest = (
        capability.selection.authority.descriptor.descriptor_digest
        if isinstance(capability.selection.authority, InstallPlanningAuthority)
        else None
    )
    return replace(
        state,
        capabilities=tuple(
            item for item in state.capabilities if item.capability_id != capability_id
        ),
        evidence=tuple(item for item in state.evidence if item.capability_id != capability_id),
        blocked_capability_ids=tuple(
            value for value in state.blocked_capability_ids if value != capability_id
        ),
        blocked_deactivation_ids=tuple(
            value for value in state.blocked_deactivation_ids if value != capability_id
        ),
        blocked_install_descriptor_digests=tuple(
            value
            for value in state.blocked_install_descriptor_digests
            if value != descriptor_digest
        ),
        rollback_requested_capability_ids=tuple(
            value for value in state.rollback_requested_capability_ids if value != capability_id
        ),
        terminal_cleanup_notified_ids=tuple(
            value for value in state.terminal_cleanup_notified_ids if value != capability_id
        ),
    )


def _load_material_identity(selection: PlanCapabilityV3) -> MaterialIdentity:
    authority = selection.authority
    if not isinstance(authority, LoadPlanningAuthority):
        _invalid("load material identity requires exact load authority")
    descriptor = authority.material.catalog_material_descriptor
    if descriptor is None or descriptor.content_sha256 is None:
        _invalid("load authority is missing exact catalog material identity")
    material = MaterialIdentity.create(
        capability_id=selection.capability_id,
        kind=selection.kind,
        content_sha256=descriptor.content_sha256,
        content_bytes=descriptor.content_bytes,
    )
    if material.identity_digest != authority.material.material_identity_digest:
        _invalid("load authority material digest does not match exact catalog content")
    return material


def _new_capability_v3(
    selection: PlanCapabilityV3,
    committed: CommittedPlanV3,
) -> CapabilityStateV3 | None:
    authority = selection.authority
    if isinstance(authority, ManualPlanningAuthority):
        return None
    if isinstance(authority, LoadPlanningAuthority):
        return CapabilityStateV3(
            selection=selection,
            material_identity=_load_material_identity(selection),
            current_authorized_material=authority.material,
            installation="installed",
            plan_id=committed.plan_id,
            catalog_snapshot_id=committed.catalog_snapshot_id,
        )
    if isinstance(authority, InstallPlanningAuthority):
        return CapabilityStateV3(
            selection=selection,
            material_identity=authority.result_material,
            current_authorized_material=None,
            installation="absent",
            plan_id=committed.plan_id,
            catalog_snapshot_id=committed.catalog_snapshot_id,
        )
    _invalid("schema-v3 plan row has unsupported authority")


def _runtime_lineage_binding_v3(capability: CapabilityStateV3) -> CapabilityLineageBinding:
    authority = capability.selection.authority
    descriptor_digest: str | None = None
    installed_lineage_digest: str | None = None
    if isinstance(authority, InstallPlanningAuthority):
        descriptor_digest = authority.descriptor.descriptor_digest
        lineage = capability.installed_lineage
        installed_lineage_digest = None if lineage is None else lineage.lineage_digest
    return CapabilityLineageBinding(
        capability_id=capability.capability_id,
        kind=capability.kind,
        catalog_identity_digest=capability.catalog_identity.identity_digest,
        actionability=capability.actionability,
        material_identity_digest=capability.material_identity.identity_digest,
        install_descriptor_digest=descriptor_digest,
        installed_material_lineage_digest=installed_lineage_digest,
    )


def _selection_lineage_binding_v3(selection: PlanCapabilityV3) -> CapabilityLineageBinding:
    authority = selection.authority
    if isinstance(authority, LoadPlanningAuthority):
        material_identity_digest = authority.material.material_identity_digest
        descriptor_digest = None
    elif isinstance(authority, InstallPlanningAuthority):
        material_identity_digest = authority.result_material.identity_digest
        descriptor_digest = authority.descriptor.descriptor_digest
    else:
        _invalid("manual schema-v3 selection has no lifecycle lineage binding")
    return CapabilityLineageBinding(
        capability_id=selection.capability_id,
        kind=selection.kind,
        catalog_identity_digest=selection.catalog_identity.identity_digest,
        actionability=selection.actionability,
        material_identity_digest=material_identity_digest,
        install_descriptor_digest=descriptor_digest,
    )


def _promote_install_to_load_v3(
    state: EngineState,
    current: CapabilityStateV3,
    selection: PlanCapabilityV3,
    committed: CommittedPlanV3,
) -> CapabilityStateV3 | None:
    """Preserve live runtime state only for exact receipt-proven material promotion."""

    proposed_authority = selection.authority
    if not isinstance(current.selection.authority, InstallPlanningAuthority) or not isinstance(
        proposed_authority, LoadPlanningAuthority
    ):
        return None
    transition = classify_lineage_transition(
        _runtime_lineage_binding_v3(current),
        _selection_lineage_binding_v3(selection),
        installed_lineage=current.installed_lineage,
        has_pending_effect=any(
            pending.action.entity_id == current.capability_id for pending in state.pending_effects
        )
        or any(
            pending.install_action.entity_id == current.capability_id
            for pending in state.pending_consents
        ),
    )
    if transition.transition != "install-to-load":
        return None
    # Schema-v3 evidence is source-bound and must remain exact across the
    # promotion. A source refresh is a new selection, not a lineage promotion.
    if current.source_digest != selection.source_digest:
        return None
    return replace(
        current,
        selection=selection,
        current_authorized_material=proposed_authority.material,
        plan_id=committed.plan_id,
        catalog_snapshot_id=committed.catalog_snapshot_id,
    )


def _commit_plan_v3(
    state: EngineState,
    event: EngineEvent,
    decision: Any,
) -> tuple[EngineState, tuple[PlanCapabilityV3, ...]]:
    committed = CommittedPlanV3.from_dict(
        {
            "plan_id": _required_text(event.correlation_id, "correlation_id"),
            "catalog_snapshot_id": _required_sha256(
                event.catalog_snapshot_digest,
                "catalog_snapshot_digest",
            ),
            "decision_digest": decision.value_digest,
            **_thaw_json(decision.value),
        }
    )
    previous = {item.capability_id: cast(CapabilityStateV3, item) for item in state.capabilities}
    if committed.status == "degraded":
        return (
            replace(
                state,
                committed_plan=committed,
                last_manual_bundle=(),
                _contract_version=3,
            ),
            committed.capabilities,
        )
    if state.pending_effects or state.pending_consents:
        _invalid("a ready or abstained plan cannot replace pending schema-v3 authority")
    capabilities: dict[str, CapabilityStateV3] = {}
    for selection in committed.capabilities:
        if isinstance(selection.authority, ManualPlanningAuthority):
            continue
        current = previous.get(selection.capability_id)
        if current is not None and current.selection == selection:
            capabilities[selection.capability_id] = replace(
                current,
                plan_id=committed.plan_id,
                catalog_snapshot_id=committed.catalog_snapshot_id,
            )
            continue
        if current is not None:
            promoted = _promote_install_to_load_v3(state, current, selection, committed)
            if promoted is not None:
                capabilities[selection.capability_id] = promoted
                continue
            safe_rebind = (
                current.installation == "absent"
                and current.activation == "inactive"
                and not current.leases
                and not current.rollback_held
            )
            if not safe_rebind:
                _invalid("schema-v3 selection changed while prior same-ID runtime authority exists")
        created = _new_capability_v3(selection, committed)
        if created is not None:
            capabilities[selection.capability_id] = created
    for capability_id, current in previous.items():
        if capability_id in capabilities or current.activation != "active":
            continue
        capabilities[capability_id] = replace(current, leases=())
    manual_ids = tuple(
        item.capability_id
        for item in committed.capabilities
        if isinstance(item.authority, ManualPlanningAuthority)
    )
    return (
        replace(
            state,
            committed_plan=committed,
            capabilities=tuple(capabilities[key] for key in sorted(capabilities)),
            evidence=tuple(item for item in state.evidence if item.capability_id in capabilities),
            last_manual_bundle=manual_ids,
            blocked_capability_ids=tuple(
                value for value in state.blocked_capability_ids if value in capabilities
            ),
            blocked_deactivation_ids=tuple(
                value for value in state.blocked_deactivation_ids if value in capabilities
            ),
            blocked_install_descriptor_digests=tuple(
                value
                for value in state.blocked_install_descriptor_digests
                if any(
                    isinstance(item.authority, InstallPlanningAuthority)
                    and item.authority.descriptor.descriptor_digest == value
                    for item in committed.capabilities
                )
            ),
            rollback_requested_capability_ids=tuple(
                value for value in state.rollback_requested_capability_ids if value in capabilities
            ),
            terminal_cleanup_notified_ids=tuple(
                value for value in state.terminal_cleanup_notified_ids if value in capabilities
            ),
            _contract_version=3,
        ),
        committed.capabilities,
    )


def _update_desired_v3(state: EngineState, event: EngineEvent) -> EngineState:
    """Lease only exact, fully authenticated rows from the committed v3 plan."""

    committed = state.committed_plan
    if not isinstance(committed, CommittedPlanV3):
        _invalid("v3 reassessment requires a committed capability plan")
    if committed.status != "ready":
        _invalid("v3 reassessment requires a ready committed capability plan")
    if event.correlation_id != committed.plan_id:
        _invalid("reassessment plan_id does not match the committed plan")
    if event.catalog_snapshot_digest != committed.catalog_snapshot_id:
        _invalid("reassessment catalog snapshot does not match the committed plan")
    if set(event.payload) != {
        "owner_id",
        "desired_capabilities",
        "policy_snapshot_digest",
    }:
        _invalid("v3 reassessment requires exact desired and policy fields")
    policy_snapshot_digest = _required_sha256(
        event.payload["policy_snapshot_digest"],
        "policy_snapshot_digest",
    )
    pending_install = any(pending.effect == "install" for pending in state.pending_effects)
    if pending_install:
        if state.install_policy_snapshot_digest is None:
            raise AssertionError("pending schema-v3 install lacks its policy snapshot")
        policy_snapshot_digest = state.install_policy_snapshot_digest
    elif policy_snapshot_digest != state.install_policy_snapshot_digest:
        state = replace(
            state,
            install_policy_snapshot_digest=policy_snapshot_digest,
            pending_consents=(),
        )
    owner_id = _required_text(event.payload["owner_id"], "owner_id")
    raw_desired = _sequence(event.payload["desired_capabilities"], "desired_capabilities")
    planned = {item.capability_id: item for item in committed.capabilities}
    desired: list[tuple[PlanCapabilityV3, str]] = []
    lease_ids: set[str] = set()
    for index, raw in enumerate(raw_desired):
        item = _mapping(raw, f"desired_capabilities[{index}]")
        lease_id = _required_text(
            item.get("lease_id"),
            f"desired_capabilities[{index}].lease_id",
        )
        capability_id = _required_text(
            item.get("capability_id"),
            f"desired_capabilities[{index}].capability_id",
        )
        if lease_id in lease_ids:
            _invalid(f"duplicate lease_id {lease_id!r}")
        lease_ids.add(lease_id)
        selection = planned.get(capability_id)
        if selection is None:
            _invalid(f"capability {capability_id!r} was not in the committed plan")
        expected = {
            "capability_id": selection.capability_id,
            "source_digest": selection.source_digest,
            "kind": selection.kind,
            "actionability": selection.actionability,
            "install_descriptor_digest": selection.install_descriptor_digest,
            "install_plan_digest": selection.install_plan_digest,
        }
        if set(item) != {*expected, "lease_id"} or any(
            item[field_name] != value for field_name, value in expected.items()
        ):
            _invalid(f"desired capability {capability_id!r} does not match committed identity")
        desired.append((selection, lease_id))

    explicit_ids = {selection.capability_id for selection, _lease_id in desired}
    for raw_capability in state.capabilities:
        capability = cast(CapabilityStateV3, raw_capability)
        if capability.capability_id in explicit_ids or capability.activation != "active":
            continue
        selection = planned.get(capability.capability_id)
        if selection is None or capability.selection != selection:
            continue
        for lease in capability.leases:
            if lease.owner_id == owner_id:
                if lease.lease_id in lease_ids:
                    _invalid(f"duplicate lease_id {lease.lease_id!r}")
                lease_ids.add(lease.lease_id)
                desired.append((selection, lease.lease_id))

    foreign_leases = {
        lease.lease_id: capability.capability_id
        for capability in state.capabilities
        for lease in capability.leases
        if lease.owner_id != owner_id
    }
    for _selection, lease_id in desired:
        if lease_id in foreign_leases:
            _invalid(f"lease_id {lease_id!r} already belongs to {foreign_leases[lease_id]!r}")

    capabilities = {
        item.capability_id: replace(
            cast(CapabilityStateV3, item),
            leases=tuple(lease for lease in item.leases if lease.owner_id != owner_id),
        )
        for item in state.capabilities
    }
    for selection, lease_id in desired:
        if isinstance(selection.authority, ManualPlanningAuthority):
            continue
        desired_capability = capabilities.get(selection.capability_id)
        if desired_capability is None or desired_capability.selection != selection:
            _invalid("desired executable capability lacks exact committed runtime authority")
        lease = LeaseRef(
            lease_id=lease_id,
            owner_id=owner_id,
            exposure_id=event.scope.exposure_id,
        )
        capabilities[selection.capability_id] = replace(
            desired_capability,
            leases=tuple(
                sorted((*desired_capability.leases, lease), key=lambda item: item.lease_id)
            ),
        )

    retained_install_descriptors = {
        capability.selection.authority.descriptor.descriptor_digest
        for capability in capabilities.values()
        if capability.desired
        and isinstance(capability.selection.authority, InstallPlanningAuthority)
    }
    return replace(
        state,
        capabilities=tuple(capabilities[key] for key in sorted(capabilities)),
        blocked_capability_ids=tuple(
            value
            for value in state.blocked_capability_ids
            if value in capabilities and capabilities[value].desired
        ),
        blocked_deactivation_ids=(),
        blocked_install_descriptor_digests=tuple(
            value
            for value in state.blocked_install_descriptor_digests
            if value in retained_install_descriptors
        ),
        install_policy_snapshot_digest=policy_snapshot_digest,
        _contract_version=3,
    )


def _make_material_action_v3(
    *,
    event: EngineEvent,
    revision: int,
    kind: str,
    capability: CapabilityStateV3,
    ordinal: int,
    lease_id: str,
) -> HostAction:
    current = capability.current_authorized_material
    if current is None or capability.installation != "installed":
        _invalid("schema-v3 material action requires installed authorized material")
    expected_state = {
        "ActivateCapability": "active",
        "PrepareExposure": "prepared",
        "DeactivateCapability": "inactive",
    }.get(kind)
    if expected_state is None:
        _invalid("schema-v3 material action kind is unsupported")
    occurred_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    expires_at = (occurred_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    if kind == "ActivateCapability":
        rollback: Mapping[str, Any] = {"kind": "DeactivateCapability"}
    elif kind == "PrepareExposure":
        rollback = {
            "kind": "cleanup-prepared-exposure",
            "exposure_id": event.scope.exposure_id,
        }
    else:
        rollback = {"kind": "ActivateCapability", "source_digest": capability.source_digest}
    return HostAction(
        action_id=_action_id(event, kind, capability.capability_id, ordinal),
        kind=kind,
        scope=event.scope,
        precondition_revision=revision,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        lease_id=lease_id,
        expires_at=expires_at,
        required_host_feature="activation",
        payload={
            "schema": MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
            "capability_kind": capability.kind,
            "catalog_identity": capability.catalog_identity.to_dict(),
            "material_identity": capability.material_identity.to_dict(),
            "authorized_material": current.to_dict(),
        },
        verification={
            "receipt_required": True,
            "expected_state": expected_state,
            "receipt_schema": MATERIAL_RECEIPT_SCHEMA_V3,
        },
        rollback=rollback,
        privacy=event.privacy,
    )


def _prompt_context_bundle_digest(
    *,
    execution_intent: str,
    plan_digest: str,
    presentation_action: HostAction,
    capabilities: list[dict[str, object]],
) -> str:
    encoded = json.dumps(
        {
            "capabilities": capabilities,
            "execution_intent": execution_intent,
            "plan_digest": plan_digest,
            "presentation_action_content_digest": presentation_action.content_digest,
            "presentation_action_id": presentation_action.action_id,
            "schema": "ctx.prompt-context-bundle-v1",
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _make_prompt_context_action_v4(
    *,
    state: EngineState,
    event: EngineEvent,
    revision: int,
    presentation_action: HostAction,
) -> HostAction | None:
    plan = state.committed_plan
    if not isinstance(plan, CommittedPlanV3) or plan.status != "ready":
        return None
    execution_intent = {
        "prompt-context-activate": "activate",
        "prompt-context-experiment": "experiment",
    }.get(state.host_level)
    if execution_intent is None:
        return None
    capabilities: list[dict[str, object]] = []
    for selection in plan.capabilities:
        if not isinstance(selection.authority, LoadPlanningAuthority):
            continue
        capability = _v3_capability(state, selection.capability_id)
        if (
            capability is None
            or capability.selection != selection
            or capability.installation != "installed"
            or capability.current_authorized_material is None
        ):
            _invalid("prompt context selection lacks exact current material authority")
        capabilities.append(
            {
                "authorized_material": capability.current_authorized_material.to_dict(),
                "capability_id": capability.capability_id,
                "capability_kind": capability.kind,
                "catalog_identity": capability.catalog_identity.to_dict(),
                "material_identity": capability.material_identity.to_dict(),
                "source_digest": capability.source_digest,
            }
        )
    if not capabilities:
        return None
    occurred_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    source_digest = _prompt_context_bundle_digest(
        execution_intent=execution_intent,
        plan_digest=plan.decision_digest,
        presentation_action=presentation_action,
        capabilities=capabilities,
    )
    return HostAction(
        action_id=_action_id(event, "PreparePromptContext", None, 0),
        kind="PreparePromptContext",
        scope=event.scope,
        precondition_revision=revision,
        entity_id=None,
        source_digest=source_digest,
        plan_id=plan.plan_id,
        catalog_snapshot_id=plan.catalog_snapshot_id,
        lease_id=f"prompt-context:{event.content_digest[:48]}",
        expires_at=(occurred_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        required_host_feature="prompt-context",
        payload={
            "schema": PROMPT_CONTEXT_ACTION_PAYLOAD_SCHEMA_V1,
            "execution_intent": execution_intent,
            "plan_digest": plan.decision_digest,
            "presentation_action_id": presentation_action.action_id,
            "presentation_action_content_digest": presentation_action.content_digest,
            "capabilities": capabilities,
        },
        verification={
            "receipt_required": True,
            "expected_state": "prompt-context-prepared",
            "receipt_schema": PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
        },
        rollback={
            "kind": "discard-prompt-context",
            "exposure_id": event.scope.exposure_id,
        },
        privacy=event.privacy,
    )


def _make_install_consent_v3(
    *,
    event: EngineEvent,
    revision: int,
    capability: CapabilityStateV3,
    policy_snapshot_digest: str,
) -> tuple[HostAction, HostAction]:
    authority = capability.selection.authority
    if not isinstance(authority, InstallPlanningAuthority):
        _invalid("schema-v3 install consent requires exact install authority")
    policy_snapshot_digest = _required_sha256(
        policy_snapshot_digest,
        "policy_snapshot_digest",
    )
    consent_id = _consent_id(event, capability.capability_id)
    occurred_at = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    expires_at = (occurred_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    typed_payload: dict[str, Any] = {
        "schema": INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
        "capability_kind": capability.kind,
        "catalog_identity": capability.catalog_identity.to_dict(),
        "result_material": capability.material_identity.to_dict(),
        "install_plan_descriptor": authority.descriptor.to_dict(),
        "installer_digest": INSTALLER_DIGEST,
        "policy_snapshot_digest": policy_snapshot_digest,
    }
    install = HostAction(
        action_id=_action_id(event, "InstallCapability", capability.capability_id, 0),
        kind="InstallCapability",
        scope=event.scope,
        precondition_revision=revision + 1,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        consent_id=consent_id,
        expires_at=expires_at,
        required_host_feature="installation",
        payload=typed_payload,
        verification={
            "receipt_required": True,
            "expected_state": "installed",
            "receipt_schema": INSTALL_RECEIPT_SCHEMA_V3,
        },
        rollback={
            "kind": "UninstallCapability",
            "installer_id": authority.descriptor.installer_id,
        },
        privacy=event.privacy,
    )
    requested_identity = {
        "requested_action_id": install.action_id,
        "requested_action_kind": install.kind,
        "requested_action_content_digest": install.content_digest,
        "requested_action_precondition_revision": install.precondition_revision,
    }
    request = HostAction(
        action_id=_action_id(event, "RequestConsent", capability.capability_id, 0),
        kind="RequestConsent",
        scope=event.scope,
        precondition_revision=revision,
        entity_id=capability.capability_id,
        source_digest=capability.source_digest,
        plan_id=capability.plan_id,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        consent_id=consent_id,
        required_host_feature="installation-consent",
        payload={
            **typed_payload,
            "schema": INSTALL_CONSENT_REQUEST_SCHEMA_V3,
            **requested_identity,
        },
        privacy=event.privacy,
    )
    return request, install


def _reconcile_v3(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if state.session_status == "ended":
        cleanup = next(
            (
                cast(CapabilityStateV3, capability)
                for capability in state.capabilities
                if capability.activation == "active"
                and capability.capability_id not in state.blocked_deactivation_ids
                and not _pending_for(state, capability.capability_id, "deactivate", "prepare")
            ),
            None,
        )
        if cleanup is None:
            return state, (), ()
        action = _make_material_action_v3(
            event=event,
            revision=revision,
            kind="DeactivateCapability",
            capability=cleanup,
            ordinal=0,
            lease_id=cleanup.activation_lease_id or f"session-end:{cleanup.capability_id}",
        )
        return _with_pending(state, action, effect="deactivate"), (action,), ()

    if state.host_level not in {"activating", "managing"}:
        return (
            state,
            (),
            ({"code": "host_activation_unsupported", "host_level": state.host_level},),
        )
    if state.pending_consents:
        return state, (), ()

    absent_install = next(
        (
            cast(CapabilityStateV3, capability)
            for capability in state.capabilities
            if capability.desired
            and capability.installation == "absent"
            and isinstance(
                cast(CapabilityStateV3, capability).selection.authority,
                InstallPlanningAuthority,
            )
            and cast(
                InstallPlanningAuthority,
                cast(CapabilityStateV3, capability).selection.authority,
            ).descriptor.descriptor_digest
            not in state.blocked_install_descriptor_digests
            and not _pending_for(state, capability.capability_id, "install")
        ),
        None,
    )
    if absent_install is not None:
        if state.install_policy_snapshot_digest is None:
            _invalid("install consent requires a committed policy snapshot")
        request, install = _make_install_consent_v3(
            event=event,
            revision=revision,
            capability=absent_install,
            policy_snapshot_digest=state.install_policy_snapshot_digest,
        )
        return (
            replace(
                state,
                pending_consents=(
                    *state.pending_consents,
                    PendingConsent(consent_id=request.consent_id or "", install_action=install),
                ),
            ),
            (request,),
            (),
        )

    actions: list[HostAction] = []
    for capability_id in state.rollback_requested_capability_ids:
        capability = _v3_capability(state, capability_id)
        if (
            capability is None
            or capability.activation == "active"
            or _pending_for(state, capability_id, "activate", "rollback-activate")
        ):
            continue
        action = _make_material_action_v3(
            event=event,
            revision=revision,
            kind="ActivateCapability",
            capability=capability,
            ordinal=0,
            lease_id=f"rollback:{capability_id}",
        )
        state = _with_pending(state, action, effect="rollback-activate")
        actions.append(action)
        break
    if state.rollback_requested_capability_ids or any(
        pending.effect == "rollback-activate" for pending in state.pending_effects
    ):
        return state, tuple(actions), ()

    inactive_desired = [
        cast(CapabilityStateV3, capability)
        for capability in state.capabilities
        if capability.desired
        and capability.installation == "installed"
        and capability.activation == "inactive"
        and capability.capability_id not in state.blocked_capability_ids
        and not _pending_for(state, capability.capability_id, "activate")
    ]
    active_undesired = [
        cast(CapabilityStateV3, capability)
        for capability in state.capabilities
        if capability.activation == "active"
        and not capability.desired
        and not capability.rollback_held
        and capability.capability_id not in state.blocked_deactivation_ids
        and not _pending_for(state, capability.capability_id, "deactivate", "prepare")
    ]
    replacement_in_progress = any(
        capability.rollback_held and capability.activation == "inactive"
        for capability in state.capabilities
    )
    if active_undesired and not replacement_in_progress:
        replacing = bool(inactive_desired)
        capability = active_undesired[0]
        replacement_owner_id = inactive_desired[0].leases[0].owner_id if replacing else None
        updated = replace(
            capability,
            rollback_held=replacing,
            rollback_owner_id=replacement_owner_id,
        )
        state = _replace_capability_v3(state, updated)
        action = _make_material_action_v3(
            event=event,
            revision=revision,
            kind="DeactivateCapability",
            capability=updated,
            ordinal=0,
            lease_id=updated.activation_lease_id or f"release:{updated.capability_id}",
        )
        return _with_pending(state, action, effect="deactivate"), (action,), ()

    active_count = len(state.active_capability_ids)
    reserved_count = sum(
        pending.effect in {"activate", "rollback-activate"} for pending in state.pending_effects
    )
    capacity = max(0, MAX_ACTIVE_CAPABILITIES - active_count - reserved_count)
    for capability in inactive_desired[:capacity]:
        assigned_rollbacks = {
            pending.rollback_capability_id
            for pending in state.pending_effects
            if pending.effect == "activate" and pending.rollback_capability_id is not None
        }
        rollback_capability = next(
            (
                cast(CapabilityStateV3, candidate)
                for candidate in state.capabilities
                if candidate.rollback_held
                and candidate.activation == "inactive"
                and candidate.capability_id not in assigned_rollbacks
            ),
            None,
        )
        action = _make_material_action_v3(
            event=event,
            revision=revision,
            kind="ActivateCapability",
            capability=capability,
            ordinal=len(actions),
            lease_id=capability.leases[0].lease_id,
        )
        state = _with_pending(
            state,
            action,
            effect="activate",
            rollback_capability_id=(
                None if rollback_capability is None else rollback_capability.capability_id
            ),
        )
        actions.append(action)
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    if len(inactive_desired) > capacity:
        diagnostics = (
            {
                "code": "active_capability_budget_exhausted",
                "limit": MAX_ACTIVE_CAPABILITIES,
                "deferred": len(inactive_desired) - capacity,
            },
        )
    return state, tuple(actions), diagnostics


def _receipt_pending_v3(state: EngineState, event: EngineEvent) -> PendingEffect:
    pending = _receipt_pending(state, event)
    if event.kind != "ActionApplied":
        return pending
    verification = _mapping(event.payload["verification"], "verification")
    action = pending.action
    if action.kind == "InstallCapability":
        expected = {
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
    elif action.kind in {"ActivateCapability", "PrepareExposure", "DeactivateCapability"}:
        expected = {
            "schema": MATERIAL_RECEIPT_SCHEMA_V3,
            "host_state": action.verification["expected_state"],
            "capability_id": action.entity_id,
            "capability_kind": action.payload["capability_kind"],
            "catalog_identity": action.payload["catalog_identity"],
            "material_identity": action.payload["material_identity"],
            "authorized_material": action.payload["authorized_material"],
        }
    elif action.kind == "PreparePromptContext":
        action_rows = _sequence(action.payload["capabilities"], "capabilities")
        receipt_rows = _sequence(verification.get("capabilities"), "capabilities")
        expected_rows = []
        for index, raw in enumerate(action_rows):
            row = _mapping(raw, f"action capabilities[{index}]")
            material = _mapping(row["material_identity"], "material_identity")
            expected_rows.append(
                {
                    "capability_id": row["capability_id"],
                    "content_sha256": material["content_sha256"],
                    "content_bytes": material["content_bytes"],
                }
            )
        if tuple(dict(_mapping(row, "receipt capability")) for row in receipt_rows) != tuple(
            expected_rows
        ):
            _invalid("prompt context receipt does not match exact action capabilities")
        expected = {
            "schema": PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
            "host_state": "prompt-context-prepared",
            "prompt_context_sha256": verification.get("prompt_context_sha256"),
            "prompt_context_bytes": verification.get("prompt_context_bytes"),
            "capabilities": receipt_rows,
        }
    else:
        _invalid("schema-v3 receipt action kind is unsupported")
    if dict(verification) != expected:
        _invalid("schema-v3 receipt does not byte-match the exact pending action authority")
    return pending


def _apply_receipt_v3(
    state: EngineState,
    pending: PendingEffect,
    event: EngineEvent,
) -> EngineState:
    state = _remove_pending(state, pending)
    if pending.effect == "prompt-context":
        rows = _sequence(pending.action.payload["capabilities"], "capabilities")
        for raw in rows:
            row = _mapping(raw, "prompt context capability")
            prompt_capability_id = _required_text(row["capability_id"], "capability_id")
            source_digest = _required_sha256(row["source_digest"], "source_digest")
            evidence = state.evidence_for(
                pending.action.scope.exposure_id,
                prompt_capability_id,
            )
            if evidence.exposure == "unexposed":
                state = _replace_evidence(
                    state,
                    replace(evidence, source_digest=source_digest, exposure="prepared"),
                )
        return state
    capability_id = pending.action.entity_id
    if capability_id is None:
        _invalid("schema-v3 physical action lacks capability identity")
    capability = _v3_capability(state, capability_id)
    if capability is None:
        _invalid("schema-v3 receipt references missing runtime capability")
    if pending.effect == "install":
        authority = capability.selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            _invalid("schema-v3 install receipt lacks committed install authority")
        lineage = InstalledMaterialLineage.create(
            capability_id=capability.capability_id,
            kind=capability.kind,
            catalog_identity_digest=capability.catalog_identity.identity_digest,
            material_identity_digest=capability.material_identity.identity_digest,
            origin_install_descriptor_digest=authority.descriptor.descriptor_digest,
            install_action_content_digest=pending.action.content_digest,
            install_receipt_content_digest=event.content_digest,
        )
        return _replace_capability_v3(
            state,
            replace(
                capability,
                installation="installed",
                current_authorized_material=AuthorizedMaterial.from_installed(lineage),
            ),
        )
    if pending.effect in {"activate", "rollback-activate"}:
        state = _replace_capability_v3(
            state,
            replace(
                capability,
                activation="active",
                activation_lease_id=pending.action.lease_id,
            ),
        )
        if pending.rollback_capability_id is not None:
            rollback = _v3_capability(state, pending.rollback_capability_id)
            if rollback is not None:
                cleared = replace(rollback, rollback_held=False, rollback_owner_id=None)
                state = (
                    _remove_capability_v3(state, cleared)
                    if _can_prune_retired_v3(state, cleared)
                    else _replace_capability_v3(state, cleared)
                )
        if pending.effect == "rollback-activate":
            state = replace(
                state,
                rollback_requested_capability_ids=tuple(
                    value
                    for value in state.rollback_requested_capability_ids
                    if value != capability_id
                ),
            )
        return state
    if pending.effect == "deactivate":
        deactivated = replace(capability, activation="inactive", activation_lease_id=None)
        if _can_prune_retired_v3(state, deactivated):
            return _remove_capability_v3(state, deactivated)
        return _replace_capability_v3(state, deactivated)
    if pending.effect == "prepare":
        evidence = state.evidence_for(pending.action.scope.exposure_id, capability_id)
        if evidence.exposure == "unexposed":
            return _replace_evidence(
                state,
                replace(evidence, source_digest=capability.source_digest, exposure="prepared"),
            )
        return state
    _invalid("schema-v3 pending effect is unsupported")


def _fail_receipt_v3(state: EngineState, pending: PendingEffect) -> EngineState:
    state = _remove_pending(state, pending)
    if pending.effect == "prompt-context":
        return state
    capability_id = pending.action.entity_id
    if capability_id is None:
        _invalid("schema-v3 physical action lacks capability identity")
    if pending.effect == "install":
        descriptor = _mapping(
            pending.action.payload["install_plan_descriptor"],
            "install_plan_descriptor",
        )
        digest = _required_sha256(descriptor["descriptor_digest"], "descriptor_digest")
        return replace(
            state,
            blocked_install_descriptor_digests=tuple(
                sorted({*state.blocked_install_descriptor_digests, digest})
            ),
        )
    if pending.effect == "activate":
        state = replace(
            state,
            blocked_capability_ids=tuple(sorted({*state.blocked_capability_ids, capability_id})),
        )
        if pending.rollback_capability_id is not None and state.session_status != "ended":
            state = replace(
                state,
                rollback_requested_capability_ids=tuple(
                    sorted(
                        {
                            *state.rollback_requested_capability_ids,
                            pending.rollback_capability_id,
                        }
                    )
                ),
            )
        return state
    if pending.effect == "deactivate":
        capability = _v3_capability(state, capability_id)
        if capability is not None:
            state = _replace_capability_v3(
                state,
                replace(capability, rollback_held=False, rollback_owner_id=None),
            )
        return replace(
            state,
            blocked_deactivation_ids=tuple(
                sorted({*state.blocked_deactivation_ids, capability_id})
            ),
        )
    if pending.effect == "rollback-activate":
        return replace(
            state,
            rollback_requested_capability_ids=tuple(
                value for value in state.rollback_requested_capability_ids if value != capability_id
            ),
        )
    return state


def _receipt_pending(state: EngineState, event: EngineEvent) -> PendingEffect:
    action_id = _required_text(event.payload.get("action_id"), "action_id")
    pending = next(
        (item for item in state.pending_effects if item.action.action_id == action_id),
        None,
    )
    if pending is None:
        _invalid(f"receipt references unknown or completed action {action_id!r}")
    if event.scope != pending.action.scope:
        _invalid("receipt scope does not match pending action scope")
    if event.payload.get("action_kind") != pending.action.kind:
        _invalid("receipt action_kind does not match pending action")
    if event.payload.get("action_content_digest") != pending.action.content_digest:
        _invalid("receipt action_content_digest does not match pending action")
    if event.payload.get("action_precondition_revision") != pending.action.precondition_revision:
        _invalid("receipt action_precondition_revision does not match pending action")
    if event.kind == "ActionApplied":
        verification = _mapping(event.payload.get("verification"), "verification")
        expected_state = pending.action.verification.get("expected_state")
        if verification.get("host_state") != expected_state:
            _invalid("receipt verification contradicts the requested physical state")
    return pending


def _remove_pending(state: EngineState, pending: PendingEffect) -> EngineState:
    return replace(
        state,
        pending_effects=tuple(item for item in state.pending_effects if item != pending),
    )


def _apply_receipt(state: EngineState, pending: PendingEffect) -> EngineState:
    state = _remove_pending(state, pending)
    capability_id = pending.action.entity_id
    if capability_id is None:
        return state
    capability = state.capability(capability_id)
    if capability is None:
        _invalid(f"pending action entity {capability_id!r} no longer exists")
    if pending.effect == "install":
        capability = replace(capability, installation="installed")
        state = _replace_capability(state, capability)
    elif pending.effect in {"activate", "rollback-activate"}:
        capability = replace(
            capability,
            activation="active",
            activation_lease_id=pending.action.lease_id,
        )
        state = _replace_capability(state, capability)
        rollback_id = pending.rollback_capability_id
        if rollback_id is not None:
            old = state.capability(rollback_id)
            if old is not None:
                state = _replace_capability(
                    state,
                    replace(old, rollback_held=False, rollback_owner_id=None),
                )
        if pending.effect == "rollback-activate":
            state = replace(
                state,
                rollback_requested_capability_ids=tuple(
                    value
                    for value in state.rollback_requested_capability_ids
                    if value != capability_id
                ),
            )
    elif pending.effect == "deactivate":
        state = _replace_capability(
            state,
            replace(
                capability,
                activation="inactive",
                activation_lease_id=None,
            ),
        )
    elif pending.effect == "prepare":
        evidence = state.evidence_for(
            pending.action.scope.exposure_id,
            capability_id,
        )
        if evidence.exposure == "unexposed":
            state = _replace_evidence(
                state,
                replace(
                    evidence,
                    source_digest=capability.source_digest,
                    exposure="prepared",
                ),
            )
    return state


def _fail_receipt(state: EngineState, pending: PendingEffect) -> EngineState:
    state = _remove_pending(state, pending)
    capability_id = pending.action.entity_id
    if capability_id is None:
        return state
    if pending.effect == "install":
        install_descriptor_digest = pending.action.payload.get("install_descriptor_digest")
        if isinstance(install_descriptor_digest, str):
            state = replace(
                state,
                blocked_install_descriptor_digests=tuple(
                    sorted(
                        {
                            *state.blocked_install_descriptor_digests,
                            install_descriptor_digest,
                        }
                    )
                ),
            )
    elif pending.effect == "activate":
        state = replace(
            state,
            blocked_capability_ids=tuple(sorted({*state.blocked_capability_ids, capability_id})),
        )
        if pending.rollback_capability_id is not None:
            if state.session_status == "ended":
                rollback = state.capability(pending.rollback_capability_id)
                if rollback is not None:
                    state = _replace_capability(
                        state,
                        replace(rollback, rollback_held=False, rollback_owner_id=None),
                    )
            else:
                state = replace(
                    state,
                    rollback_requested_capability_ids=tuple(
                        sorted(
                            {
                                *state.rollback_requested_capability_ids,
                                pending.rollback_capability_id,
                            }
                        )
                    ),
                )
    elif pending.effect == "deactivate":
        capability = state.capability(capability_id)
        if capability is not None:
            state = _replace_capability(
                state,
                replace(
                    capability,
                    rollback_held=False,
                    rollback_owner_id=None,
                ),
            )
        state = replace(
            state,
            blocked_deactivation_ids=tuple(
                sorted({*state.blocked_deactivation_ids, capability_id})
            ),
        )
    elif pending.effect == "rollback-activate":
        state = replace(
            state,
            rollback_requested_capability_ids=tuple(
                value for value in state.rollback_requested_capability_ids if value != capability_id
            ),
        )
    return state


def _observe_submission(
    state: EngineState,
    event: EngineEvent,
) -> tuple[EngineState, tuple[Mapping[str, Any], ...]]:
    raw_capabilities = _sequence(
        event.payload.get("capabilities"),
        "capabilities",
    )
    diagnostics: list[Mapping[str, Any]] = []
    for index, raw in enumerate(raw_capabilities):
        item = _mapping(raw, f"capabilities[{index}]")
        capability_id = _required_text(
            item.get("capability_id"),
            f"capabilities[{index}].capability_id",
        )
        source_digest = _required_text(
            item.get("source_digest"),
            f"capabilities[{index}].source_digest",
        )
        capability = state.capability(capability_id)
        if capability is None or capability.source_digest != source_digest:
            diagnostics.append(
                {
                    "code": "source_digest_mismatch",
                    "capability_id": capability_id,
                }
            )
            continue
        evidence = state.evidence_for(event.scope.exposure_id, capability_id)
        state = _replace_evidence(
            state,
            replace(
                evidence,
                source_digest=source_digest,
                exposure="submitted",
            ),
        )
    return state, tuple(diagnostics)


def _observe_invocation(
    state: EngineState,
    event: EngineEvent,
) -> tuple[EngineState, tuple[Mapping[str, Any], ...]]:
    capability_id = _required_text(
        event.payload.get("capability_id"),
        "capability_id",
    )
    source_digest = _required_text(
        event.payload.get("source_digest"),
        "source_digest",
    )
    outcome = event.payload.get("outcome")
    if outcome not in {"failed", "succeeded"}:
        _invalid("outcome must be 'failed' or 'succeeded'")
    capability = state.capability(capability_id)
    if capability is None or capability.source_digest != source_digest:
        return state, (
            {
                "code": "source_digest_mismatch",
                "capability_id": capability_id,
            },
        )
    evidence = state.evidence_for(event.scope.exposure_id, capability_id)
    state = _replace_evidence(
        state,
        replace(
            evidence,
            source_digest=source_digest,
            invocation=f"invoked-{outcome}",
        ),
    )
    return state, ()


def _prepare_exposure(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if state.host_level not in {"activating", "managing"}:
        return (
            state,
            (),
            (
                {
                    "code": "host_activation_unsupported",
                    "host_level": state.host_level,
                },
            ),
        )
    actions: list[HostAction] = []
    for capability in state.capabilities:
        evidence = state.evidence_for(
            event.scope.exposure_id,
            capability.capability_id,
        )
        already_pending = any(
            pending.effect == "prepare"
            and pending.action.entity_id == capability.capability_id
            and pending.action.scope.exposure_id == event.scope.exposure_id
            for pending in state.pending_effects
        )
        if (
            capability.activation != "active"
            or not any(lease.exposure_id == event.scope.exposure_id for lease in capability.leases)
            or evidence.exposure != "unexposed"
            or already_pending
        ):
            continue
        action = _make_action(
            event=event,
            revision=revision,
            kind="PrepareExposure",
            capability=capability,
            ordinal=len(actions),
            lease_id=(
                capability.activation_lease_id
                or f"exposure:{event.scope.exposure_id}:{capability.capability_id}"
            ),
            rollback={
                "kind": "cleanup-prepared-exposure",
                "exposure_id": event.scope.exposure_id,
            },
        )
        state = _with_pending(state, action, effect="prepare")
        actions.append(action)
    return state, tuple(actions), ()


def _retry_terminal_cleanup(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if set(event.payload) != {"retry_failed_deactivations"}:
        _invalid("ended-session ReassessmentRequested must contain only retry_failed_deactivations")
    raw_ids = _sequence(
        event.payload.get("retry_failed_deactivations"),
        "retry_failed_deactivations",
    )
    capability_ids = tuple(
        _required_text(value, f"retry_failed_deactivations[{index}]")
        for index, value in enumerate(raw_ids)
    )
    if not capability_ids or len(capability_ids) > MAX_ACTIVE_CAPABILITIES:
        _invalid("retry_failed_deactivations must contain between one and five IDs")
    if len(set(capability_ids)) != len(capability_ids):
        _invalid("retry_failed_deactivations must not contain duplicate IDs")
    blocked = set(state.blocked_deactivation_ids)
    active = state.active_capability_ids
    if any(value not in blocked or value not in active for value in capability_ids):
        _invalid("terminal cleanup retry must target blocked active capabilities")
    requested = set(capability_ids)
    state = replace(
        state,
        blocked_deactivation_ids=tuple(
            value for value in state.blocked_deactivation_ids if value not in requested
        ),
        terminal_cleanup_notified_ids=tuple(
            value for value in state.terminal_cleanup_notified_ids if value not in requested
        ),
    )
    state, actions, diagnostics = _reconcile(state, event, revision)
    return (
        state,
        actions,
        (
            *diagnostics,
            {
                "code": "terminal_cleanup_retry_requested",
                "capability_ids": list(capability_ids),
            },
        ),
    )


def _handle_install_decision(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if set(event.payload) != {
        "consent_id",
        "decision",
        "decision_basis",
        "policy_snapshot_digest",
        "requested_action_id",
        "requested_action_kind",
        "requested_action_content_digest",
        "requested_action_precondition_revision",
    }:
        _invalid("v3 UserDecision requires exact decision provenance and action identity")
    consent_id = _required_text(event.payload["consent_id"], "consent_id")
    pending = next(
        (item for item in state.pending_consents if item.consent_id == consent_id),
        None,
    )
    if pending is None:
        _invalid("UserDecision references unknown or completed consent")
    install = pending.install_action
    if event.scope != install.scope:
        _invalid("UserDecision scope does not match pending consent")
    expected_identity = {
        "requested_action_id": install.action_id,
        "requested_action_kind": install.kind,
        "requested_action_content_digest": install.content_digest,
        "requested_action_precondition_revision": install.precondition_revision,
    }
    if any(event.payload[key] != value for key, value in expected_identity.items()):
        _invalid("UserDecision does not match the exact requested install")
    if event.payload["policy_snapshot_digest"] != install.payload["policy_snapshot_digest"]:
        _invalid("UserDecision policy snapshot does not match pending install")
    if install.precondition_revision != revision:
        _invalid("UserDecision was not the immediate next committed revision")
    state = replace(
        state,
        pending_consents=tuple(item for item in state.pending_consents if item != pending),
    )
    descriptor = _mapping(
        install.payload["install_plan_descriptor"],
        "install_plan_descriptor",
    )
    install_descriptor_digest = _required_sha256(
        descriptor["descriptor_digest"],
        "descriptor_digest",
    )
    decision_time = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
    expiry_time = datetime.fromisoformat((install.expires_at or "").replace("Z", "+00:00"))
    expired = decision_time >= expiry_time
    if event.payload["decision"] == "denied" or expired:
        code = "install_consent_expired" if expired else "install_consent_denied"
        state = replace(
            state,
            blocked_install_descriptor_digests=tuple(
                sorted(
                    {
                        *state.blocked_install_descriptor_digests,
                        install_descriptor_digest,
                    }
                )
            ),
        )
        state, actions, diagnostics = _reconcile_v3(state, event, revision)
        return (
            state,
            actions,
            (
                {"code": code, "capability_id": install.entity_id},
                *diagnostics,
            ),
        )
    state = _with_pending(state, install, effect="install")
    return state, (install,), ()


def _handle_install_consent_expiry(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    """Retire one exact expired request without recording a human denial."""

    consent_id = _required_text(event.payload["consent_id"], "consent_id")
    pending = next(
        (item for item in state.pending_consents if item.consent_id == consent_id),
        None,
    )
    if pending is None:
        _invalid("InstallConsentExpired references unknown or completed consent")
    install = pending.install_action
    expected_identity = {
        "policy_snapshot_digest": install.payload.get("policy_snapshot_digest"),
        "requested_action_id": install.action_id,
        "requested_action_kind": install.kind,
        "requested_action_content_digest": install.content_digest,
        "requested_action_precondition_revision": install.precondition_revision,
        "install_expires_at": install.expires_at,
    }
    if event.scope != install.scope or any(
        event.payload.get(key) != value for key, value in expected_identity.items()
    ):
        _invalid("InstallConsentExpired does not match the exact pending consent")
    if install.precondition_revision != revision:
        _invalid("InstallConsentExpired does not match the exact pending consent revision")
    return (
        replace(
            state,
            pending_consents=tuple(item for item in state.pending_consents if item != pending),
        ),
        (),
        (
            {
                "code": "install_consent_expired",
                "capability_id": install.entity_id,
                "consent_id": consent_id,
            },
        ),
    )


def _prepare_exposure_v3(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if state.host_level not in {"activating", "managing"}:
        return (
            state,
            (),
            ({"code": "host_activation_unsupported", "host_level": state.host_level},),
        )
    actions: list[HostAction] = []
    for raw_capability in state.capabilities:
        capability = cast(CapabilityStateV3, raw_capability)
        evidence = state.evidence_for(event.scope.exposure_id, capability.capability_id)
        already_pending = any(
            pending.action.entity_id == capability.capability_id
            for pending in state.pending_effects
        )
        if (
            capability.activation != "active"
            or not any(lease.exposure_id == event.scope.exposure_id for lease in capability.leases)
            or evidence.exposure != "unexposed"
            or already_pending
        ):
            continue
        action = _make_material_action_v3(
            event=event,
            revision=revision,
            kind="PrepareExposure",
            capability=capability,
            ordinal=len(actions),
            lease_id=(
                capability.activation_lease_id
                or f"exposure:{event.scope.exposure_id}:{capability.capability_id}"
            ),
        )
        state = _with_pending(state, action, effect="prepare")
        actions.append(action)
    return state, tuple(actions), ()


def _retry_terminal_cleanup_v3(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if set(event.payload) != {"retry_failed_deactivations"}:
        _invalid("ended-session ReassessmentRequested must contain only retry_failed_deactivations")
    values = tuple(
        _required_text(value, f"retry_failed_deactivations[{index}]")
        for index, value in enumerate(
            _sequence(event.payload["retry_failed_deactivations"], "retry_failed_deactivations")
        )
    )
    if not values or len(values) > MAX_ACTIVE_CAPABILITIES or len(set(values)) != len(values):
        _invalid("retry_failed_deactivations must contain one to five unique IDs")
    if any(
        value not in state.blocked_deactivation_ids or value not in state.active_capability_ids
        for value in values
    ):
        _invalid("terminal cleanup retry must target blocked active capabilities")
    state = replace(
        state,
        blocked_deactivation_ids=tuple(
            value for value in state.blocked_deactivation_ids if value not in values
        ),
        terminal_cleanup_notified_ids=tuple(
            value for value in state.terminal_cleanup_notified_ids if value not in values
        ),
    )
    state, actions, diagnostics = _reconcile_v3(state, event, revision)
    return (
        state,
        actions,
        (
            *diagnostics,
            {"code": "terminal_cleanup_retry_requested", "capability_ids": list(values)},
        ),
    )


def _handle_event_v3(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if event.kind == "UserDecision":
        return _handle_install_decision(state, event, revision)
    if event.kind == "InstallConsentExpired":
        return _handle_install_consent_expiry(state, event, revision)
    had_pending_consent = bool(state.pending_consents)
    if had_pending_consent and event.kind not in _PLANNING_EVENT_KINDS:
        # A consent binds an install to the immediately next revision. Any
        # other event consumes that revision, so retire the stale request
        # before reducing and deterministically offer a fresh one afterward.
        state = replace(state, pending_consents=())
    if event.kind == "ReassessmentRequested" and state.session_status != "ended":
        return _reconcile_v3(_update_desired_v3(state, event), event, revision)
    if event.kind == "ReassessmentRequested":
        return _retry_terminal_cleanup_v3(state, event, revision)
    if event.kind in _RECEIPT_KINDS:
        pending = _receipt_pending_v3(state, event)
        if event.kind == "ActionApplied":
            state = _apply_receipt_v3(state, pending, event)
        elif event.kind == "ActionExpired" and (
            pending.effect == "install"
            or (
                pending.effect == "activate"
                # Both engine gates require the exact schema-v3 payload before
                # asserting the trusted clock and the durable no-claim proof;
                # requiring it here too keeps all three in agreement instead of
                # relying on a distant state-validation invariant.
                and pending.action.payload.get("schema") == MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
            )
        ):
            # An approval or activation window expiring before any physical
            # claim is not a driver failure. Retire only the stale authority so
            # a later desired choice can request a fresh approval. Blocking the
            # capability here would turn a recoverable expiry into a permanent
            # soft wedge, because blocked ids survive re-planning.
            state = _remove_pending(state, pending)
            if (
                pending.effect == "activate"
                and pending.rollback_capability_id is not None
                and state.session_status != "ended"
            ):
                # The displaced capability is already inactive and rollback-held,
                # so it must still be re-requested or it stays down forever.
                state = replace(
                    state,
                    rollback_requested_capability_ids=tuple(
                        sorted(
                            {
                                *state.rollback_requested_capability_ids,
                                pending.rollback_capability_id,
                            }
                        )
                    ),
                )
            return state, (), ()
        else:
            state = _fail_receipt_v3(state, pending)
        if pending.effect == "prompt-context":
            return _refresh_stale_consent_v3(
                state,
                event,
                revision,
                had_pending_consent=had_pending_consent,
            )
        return _reconcile_v3(state, event, revision)
    if event.kind == "ProviderSubmissionObserved":
        state, diagnostics = _observe_submission(state, event)
        return _refresh_stale_consent_v3(
            state,
            event,
            revision,
            had_pending_consent=had_pending_consent,
            diagnostics=diagnostics,
        )
    if event.kind == "ToolCallObserved":
        state, diagnostics = _observe_invocation(state, event)
        return _refresh_stale_consent_v3(
            state,
            event,
            revision,
            had_pending_consent=had_pending_consent,
            diagnostics=diagnostics,
        )
    if event.kind == "TurnStarting":
        state, prepared_actions, diagnostics = _prepare_exposure_v3(state, event, revision)
        return _refresh_stale_consent_v3(
            state,
            event,
            revision,
            had_pending_consent=had_pending_consent,
            actions=prepared_actions,
            diagnostics=diagnostics,
        )
    if event.kind == "SessionEnded":
        retained_rollbacks = {
            pending.rollback_capability_id
            for pending in state.pending_effects
            if pending.effect == "activate" and pending.rollback_capability_id is not None
        }
        capabilities = tuple(
            replace(
                cast(CapabilityStateV3, capability),
                leases=(),
                rollback_held=(
                    capability.rollback_held and capability.capability_id in retained_rollbacks
                ),
                rollback_owner_id=(
                    capability.rollback_owner_id
                    if capability.capability_id in retained_rollbacks
                    else None
                ),
            )
            for capability in state.capabilities
        )
        state = replace(
            state,
            session_status="ended",
            capabilities=capabilities,
            blocked_deactivation_ids=(),
            rollback_requested_capability_ids=(),
        )
        return _reconcile_v3(state, event, revision)
    actions: tuple[HostAction, ...] = ()
    base_diagnostics: tuple[Mapping[str, Any], ...] = ()
    if (
        not had_pending_consent
        or event.kind in _PLANNING_EVENT_KINDS
        or state.session_status == "ended"
        or state.pending_consents
    ):
        return state, actions, base_diagnostics
    state, refreshed_actions, refreshed_diagnostics = _reconcile_v3(state, event, revision)
    return state, (*actions, *refreshed_actions), (*base_diagnostics, *refreshed_diagnostics)


def _refresh_stale_consent_v3(
    state: EngineState,
    event: EngineEvent,
    revision: int,
    *,
    had_pending_consent: bool,
    actions: tuple[HostAction, ...] = (),
    diagnostics: tuple[Mapping[str, Any], ...] = (),
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    """Reoffer authority when this revision consumed an exact pending consent."""

    if not had_pending_consent or state.session_status == "ended" or state.pending_consents:
        return state, actions, diagnostics
    state, refreshed_actions, refreshed_diagnostics = _reconcile_v3(state, event, revision)
    return (
        state,
        (*actions, *refreshed_actions),
        (*diagnostics, *refreshed_diagnostics),
    )


def _handle_event(
    state: EngineState,
    event: EngineEvent,
    revision: int,
) -> tuple[EngineState, tuple[HostAction, ...], tuple[Mapping[str, Any], ...]]:
    if event.kind == "InstallConsentExpired":
        _invalid("InstallConsentExpired requires the installation contract")
    if state.session_status == "ended" and event.kind == "ReassessmentRequested":
        return _retry_terminal_cleanup(state, event, revision)
    if event.kind == "ReassessmentRequested":
        state = _update_desired(state, event)
        return _reconcile(state, event, revision)
    if event.kind in _RECEIPT_KINDS:
        pending = _receipt_pending(state, event)
        if event.kind == "ActionApplied":
            state = _apply_receipt(state, pending)
        else:
            state = _fail_receipt(state, pending)
        return _reconcile(state, event, revision)
    if event.kind == "ProviderSubmissionObserved":
        state, diagnostics = _observe_submission(state, event)
        return state, (), diagnostics
    if event.kind == "ToolCallObserved":
        state, diagnostics = _observe_invocation(state, event)
        return state, (), diagnostics
    if event.kind == "TurnStarting":
        return _prepare_exposure(state, event, revision)
    if event.kind == "SessionEnded":
        retained_rollbacks = {
            pending.rollback_capability_id
            for pending in state.pending_effects
            if pending.effect == "activate" and pending.rollback_capability_id is not None
        }
        capabilities = tuple(
            replace(
                capability,
                leases=(),
                rollback_held=(
                    capability.rollback_held and capability.capability_id in retained_rollbacks
                ),
                rollback_owner_id=(
                    capability.rollback_owner_id
                    if capability.capability_id in retained_rollbacks
                    else None
                ),
            )
            for capability in state.capabilities
        )
        state = replace(
            state,
            session_status="ended",
            capabilities=capabilities,
            blocked_deactivation_ids=(),
            rollback_requested_capability_ids=(),
        )
        return _reconcile(state, event, revision)
    return state, (), ()


def _reduce_event(
    state: EngineState | None,
    event: EngineEvent,
    *,
    installation_contract: bool,
) -> tuple[EngineState, Transition]:
    """Reduce one immutable event into a new projection and typed transition."""

    if state is None:
        if event.kind != "SessionStarted":
            _invalid("SessionStarted must be the first event")
        if event.expected_revision != 0:
            raise RevisionConflictError(f"expected revision 0, received {event.expected_revision}")
        if event.host_descriptor_digest is None:
            _invalid("SessionStarted requires host_descriptor_digest")
        host_level = event.payload.get("host_level", "query-only")
        if host_level not in HOST_LEVELS:
            _invalid(f"unknown host_level {host_level!r}")
        next_revision = 1
        state = EngineState(
            revision=next_revision,
            scope=event.scope,
            host_level=host_level,
            host_descriptor_digest=event.host_descriptor_digest,
            _contract_version=3 if installation_contract else 1,
        )
        transition = Transition(
            event_id=event.event_id,
            scope=event.scope,
            from_revision=0,
            to_revision=next_revision,
        )
        return state, transition

    if event.expected_revision != state.revision:
        raise RevisionConflictError(
            f"expected revision {state.revision}, received {event.expected_revision}"
        )
    if not _same_session(state.scope, event.scope):
        _invalid("event scope does not match the current-work session scope")
    if (
        event.host_descriptor_digest is not None
        and event.host_descriptor_digest != state.host_descriptor_digest
    ):
        _invalid("host descriptor changed within a current-work session")
    if event.kind == "SessionStarted":
        _invalid("session is already started")
    if state.session_status == "ended" and event.kind not in {
        *_RECEIPT_KINDS,
        "ReassessmentRequested",
    }:
        _invalid(
            "session has ended; only pending receipts or explicit cleanup recovery are accepted"
        )

    next_revision = state.revision + 1
    handler = _handle_event_v3 if installation_contract else _handle_event
    reduced, actions, diagnostics = handler(state, event, next_revision)
    reduced = replace(
        reduced,
        revision=next_revision,
        _contract_version=3 if installation_contract else state._contract_version,
    )
    if len(reduced.active_capability_ids) > MAX_ACTIVE_CAPABILITIES:
        raise AssertionError("active capability budget invariant violated")
    transition = Transition(
        event_id=event.event_id,
        scope=event.scope,
        from_revision=state.revision,
        to_revision=next_revision,
        actions=actions,
        diagnostics=diagnostics,
    )
    return reduced, transition


def reduce(
    state: EngineState | None,
    event: EngineEvent,
) -> tuple[EngineState, Transition]:
    """Reduce one immutable event with the historical v1 event contract."""

    return _reduce_event(state, event, installation_contract=False)


def reduce_replay_v1(
    state: EngineState | None,
    replay: ReplayInput,
) -> tuple[EngineState, Transition]:
    """Preserve the exact v1 event-only behavior for historical replay."""

    if not isinstance(replay, ReplayInput):
        _invalid("replay input is required")
    if state is not None and state._contract_version != 1:
        _invalid("v1 reducer cannot continue an installation-contract stream")
    return reduce(state, replay.reducer_event)


def reduce_replay_v2(
    state: EngineState | None,
    replay: ReplayInput,
) -> tuple[EngineState, Transition]:
    """Reduce an event and its durable query-only recommendation decision."""

    if not isinstance(replay, ReplayInput):
        _invalid("replay input is required")
    if state is not None and state._contract_version != 1:
        _invalid("v2 reducer cannot continue an installation-contract stream")
    event = replay.reducer_event
    decision = replay.decision_surrogate
    if decision is not None and event.kind not in _PLANNING_EVENT_KINDS:
        _invalid("decision is not valid for this event kind")
    if event.kind in _PLANNING_EVENT_KINDS and decision is None:
        _invalid("planning event is missing its durable decision")

    next_state, transition = reduce(state, event)
    if decision is None:
        return next_state, transition
    if decision.schema_id != "ctx.decision.capability-plan" or decision.schema_version != 1:
        _invalid("decision does not use the capability plan schema")

    status = decision.value["status"]
    if status == "ready":
        capabilities = [
            dict(_mapping(item, f"decision.capabilities[{index}]"))
            for index, item in enumerate(
                _sequence(decision.value["capabilities"], "decision.capabilities")
            )
        ]
        action = _make_action(
            event=event,
            revision=transition.to_revision,
            kind="PresentBundle",
            capability=None,
            ordinal=0,
            payload={
                "plan_digest": decision.value_digest,
                "capabilities": capabilities,
            },
        )
        return next_state, replace(
            transition,
            actions=(*transition.actions, action),
        )

    code = decision.value["abstention_code"]
    return next_state, replace(
        transition,
        diagnostics=(*transition.diagnostics, {"code": code}),
    )


def _reduce_replay_v3_family(
    state: EngineState | None,
    replay: ReplayInput,
    *,
    prompt_context: bool,
) -> tuple[EngineState, Transition]:
    """Reduce schema-v3 plans while preserving exact historical v3 behavior."""

    if not isinstance(replay, ReplayInput):
        _invalid("replay input is required")
    if state is not None and state._contract_version != 3:
        _invalid("v3 reducer requires an installation-contract stream")
    event = replay.reducer_event
    decision = replay.decision_surrogate
    if decision is not None and event.kind not in _PLANNING_EVENT_KINDS:
        _invalid("decision is not valid for this event kind")
    if event.kind in _PLANNING_EVENT_KINDS and decision is None:
        _invalid("planning event is missing its durable decision")

    next_state, transition = _reduce_event(state, event, installation_contract=True)
    if decision is None:
        return next_state, transition
    if decision.schema_id != "ctx.decision.capability-plan" or decision.schema_version != 3:
        _invalid("v3 reducer requires capability plan schema version 3")

    raw_capabilities = _sequence(decision.value["capabilities"], "decision.capabilities")
    parsed_rows = tuple(
        PlanCapabilityV3.from_dict(_thaw_json(_mapping(raw, f"decision.capabilities[{index}]")))
        for index, raw in enumerate(raw_capabilities)
    )
    retained_active_ids = {
        row.capability_id
        for row in parsed_rows
        if (
            state is not None
            and (capability := state.capability(row.capability_id)) is not None
            and isinstance(capability, CapabilityStateV3)
            and capability.activation == "active"
            and capability.selection == row
        )
    }
    before_runtime = (
        None
        if state is None
        else (
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
    )
    next_state, committed_rows = _commit_plan_v3(next_state, event, decision)
    status = decision.value["status"]
    if status == "degraded":
        after_runtime = (
            next_state.capabilities,
            next_state.pending_effects,
            next_state.pending_consents,
            next_state.evidence,
            next_state.blocked_capability_ids,
            next_state.blocked_deactivation_ids,
            next_state.blocked_install_descriptor_digests,
            next_state.install_policy_snapshot_digest,
            next_state.rollback_requested_capability_ids,
            next_state.terminal_cleanup_notified_ids,
        )
        if before_runtime is not None and after_runtime != before_runtime:
            raise AssertionError("degraded schema-v3 plan mutated preserved runtime authority")
        return next_state, replace(
            transition,
            diagnostics=(
                *transition.diagnostics,
                {"code": decision.value["abstention_code"]},
            ),
        )

    if prompt_context:
        lifecycle_actions: tuple[HostAction, ...] = ()
        lifecycle_diagnostics: tuple[Mapping[str, Any], ...] = ()
    else:
        next_state, lifecycle_actions, lifecycle_diagnostics = _reconcile_v3(
            next_state,
            event,
            transition.to_revision,
        )
    if status == "ready":
        capabilities = [
            row.to_dict() for row in committed_rows if row.capability_id not in retained_active_ids
        ]
        actions = (*transition.actions, *lifecycle_actions)
        presentation_action: HostAction | None = None
        if capabilities:
            presentation_action = _make_action(
                event=event,
                revision=transition.to_revision,
                kind="PresentBundle",
                capability=None,
                ordinal=len(actions),
                payload={
                    "plan_digest": decision.value_digest,
                    "capabilities": capabilities,
                },
            )
            actions = (*actions, presentation_action)
        if prompt_context:
            if presentation_action is None:
                _invalid("prompt context plan requires an exact presentation action")
            prompt_action = _make_prompt_context_action_v4(
                state=next_state,
                event=event,
                revision=transition.to_revision,
                presentation_action=presentation_action,
            )
            if prompt_action is None:
                lifecycle_diagnostics = (
                    *lifecycle_diagnostics,
                    {"code": "prompt_context_unavailable"},
                )
            else:
                next_state = _with_pending(
                    next_state,
                    prompt_action,
                    effect="prompt-context",
                )
                actions = (*actions, prompt_action)
        return next_state, replace(
            transition,
            actions=actions,
            diagnostics=(*transition.diagnostics, *lifecycle_diagnostics),
        )
    return next_state, replace(
        transition,
        actions=(*transition.actions, *lifecycle_actions),
        diagnostics=(
            *transition.diagnostics,
            *lifecycle_diagnostics,
            {"code": decision.value["abstention_code"]},
        ),
    )


def reduce_replay_v3(
    state: EngineState | None,
    replay: ReplayInput,
) -> tuple[EngineState, Transition]:
    """Reduce the authenticated recommendation-to-install consent contract."""

    return _reduce_replay_v3_family(state, replay, prompt_context=False)


def reduce_replay_v4(
    state: EngineState | None,
    replay: ReplayInput,
) -> tuple[EngineState, Transition]:
    """Reduce explicit, bundle-level ephemeral prompt-context preparation."""

    if replay.reducer_version != PROMPT_CONTEXT_REDUCER_VERSION:
        _invalid("v4 prompt context reducer requires its exact reducer version")
    return _reduce_replay_v3_family(state, replay, prompt_context=True)


__all__ = [
    "MAX_ACTIVE_CAPABILITIES",
    "INSTALLATION_REDUCER_VERSION",
    "PROMPT_CONTEXT_REDUCER_VERSION",
    "PLANNING_REDUCER_VERSION",
    "InvalidEventError",
    "ReducerError",
    "RevisionConflictError",
    "reduce",
    "reduce_replay_v1",
    "reduce_replay_v2",
    "reduce_replay_v3",
    "reduce_replay_v4",
]
