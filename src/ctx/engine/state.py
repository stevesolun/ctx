"""Immutable state and value objects for the CTX capability reducer."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, NoReturn, cast

from .content import AuthorizedMaterial, MaterialIdentity
from .lineage import CatalogCapabilityIdentity, InstalledMaterialLineage
from .protocol import (
    INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    HostAction,
    ScopeRef,
)

if TYPE_CHECKING:
    from .planning_v3 import (
        BenefitAuditReference,
        CapabilityBenefitProjection,
        CapabilityPlanSelectionV3,
        PlanningAuthority,
    )


HOST_LEVELS = frozenset(
    {
        "query-only",
        "observing",
        "activating",
        "managing",
        "prompt-context-activate",
        "prompt-context-experiment",
    }
)
ACTIVATION_STATES = frozenset({"inactive", "active"})
INSTALLATION_STATES = frozenset({"absent", "installed"})
CAPABILITY_KINDS = frozenset({"skill", "agent", "mcp", "mcp-server", "harness"})
ACTIONABILITY_STATES = frozenset({"load", "install", "manual"})
EXPOSURE_STATES = frozenset({"unexposed", "prepared", "submitted"})
INVOCATION_STATES = frozenset({"not-invoked", "invoked-failed", "invoked-succeeded"})
PENDING_EFFECTS = frozenset(
    {
        "install",
        "activate",
        "rollback-activate",
        "deactivate",
        "prepare",
        "prompt-context",
    }
)
SESSION_STATUSES = frozenset({"active", "ended"})
MAX_ACTIVE_CAPABILITIES = 5


class StateValidationError(ValueError):
    """Persisted engine state violates its canonical projection contract."""


def _invalid(message: str) -> NoReturn:
    raise StateValidationError(message)


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid(f"{field_name} must be a non-empty trimmed string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _invalid(f"{field_name} must contain valid Unicode scalar values")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _sha256(value: object, field_name: str) -> str:
    value = _required_text(value, field_name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        _invalid(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _exact_fields(
    value: object,
    fields: frozenset[str],
    object_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _invalid(f"{object_name} must be a JSON object")
    if any(not isinstance(field_name, str) for field_name in value):
        _invalid(f"{object_name} field names must be strings")
    unknown = sorted(set(value) - fields)
    if unknown:
        _invalid(f"{object_name} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(fields - set(value))
    if missing:
        _invalid(f"{object_name} is missing field(s): {', '.join(missing)}")
    return value


def _compatible_fields(
    value: object,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    object_name: str,
) -> Mapping[str, Any]:
    """Decode a strict current object while allowing named historical omissions."""

    if not isinstance(value, Mapping):
        _invalid(f"{object_name} must be a JSON object")
    if any(not isinstance(field_name, str) for field_name in value):
        _invalid(f"{object_name} field names must be strings")
    unknown = sorted(set(value) - allowed)
    if unknown:
        _invalid(f"{object_name} has unknown field(s): {', '.join(unknown)}")
    missing = sorted(required - set(value))
    if missing:
        _invalid(f"{object_name} is missing field(s): {', '.join(missing)}")
    return value


def _array(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        _invalid(f"{field_name} must be a JSON array")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise StateValidationError("state is not canonical-JSON compatible") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, item in pairs:
        if key in decoded:
            _invalid(f"duplicate JSON key {key!r}")
        decoded[key] = item
    return decoded


def _decode_canonical_json(value: str | bytes | bytearray) -> Mapping[str, Any]:
    if isinstance(value, (bytes, bytearray)):
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StateValidationError("state must be valid UTF-8 JSON") from exc
    elif isinstance(value, str):
        text = value
    else:
        _invalid("state must be JSON text or UTF-8 bytes")
    try:
        decoded = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except StateValidationError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise StateValidationError("state must be valid JSON") from exc
    if not isinstance(decoded, dict):
        _invalid("state JSON root must be an object")
    if _canonical_json(decoded) != text:
        _invalid("state JSON must use canonical encoding")
    return decoded


_LEASE_FIELDS = frozenset({"lease_id", "owner_id", "exposure_id"})
_CAPABILITY_FIELDS = frozenset(
    {
        "actionability",
        "activation",
        "activation_lease_id",
        "capability_id",
        "catalog_snapshot_id",
        "install_plan_digest",
        "install_descriptor_digest",
        "installation",
        "kind",
        "leases",
        "plan_id",
        "rollback_held",
        "rollback_owner_id",
        "source_digest",
    }
)
_HISTORICAL_CAPABILITY_FIELDS = _CAPABILITY_FIELDS - frozenset(
    {
        "actionability",
        "install_descriptor_digest",
        "install_plan_digest",
        "installation",
        "kind",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {"capability_id", "exposure", "exposure_id", "invocation", "source_digest"}
)
_PENDING_FIELDS = frozenset({"action", "effect", "rollback_capability_id"})
_PENDING_CONSENT_FIELDS = frozenset({"consent_id", "install_action"})
_PLAN_CAPABILITY_FIELDS = frozenset(
    {
        "actionability",
        "capability_id",
        "install_plan_digest",
        "install_descriptor_digest",
        "kind",
        "source_digest",
    }
)
_COMMITTED_PLAN_FIELDS = frozenset(
    {"capabilities", "catalog_snapshot_id", "decision_digest", "plan_id"}
)
_PLAN_CAPABILITY_V3_FIELDS = frozenset(
    {
        "actionability",
        "authority",
        "benefit",
        "capability_id",
        "catalog_entry_digest",
        "catalog_identity",
        "install_descriptor_digest",
        "install_plan_digest",
        "kind",
        "matching_signals",
        "name",
        "normalized_score_ppm",
        "reason_codes",
    }
)
_BENEFIT_AUDIT_FIELDS = frozenset(
    {
        "calibration_digest",
        "candidate_pool_count",
        "policy_digest",
        "policy_schema_id",
        "requested_limit",
        "result_digest",
        "result_schema_id",
        "search_evaluation_count",
        "selection_algorithm_id",
    }
)
_BENEFIT_PROJECTION_FIELDS = frozenset(
    {"individual_net_benefit_u", "marginal_net_benefit_u", "tier"}
)
_LOAD_AUTHORITY_FIELDS = frozenset({"material", "type"})
_INSTALL_AUTHORITY_FIELDS = frozenset({"descriptor", "result_material", "type"})
_MANUAL_AUTHORITY_FIELDS = frozenset({"type"})
_COMMITTED_PLAN_V3_FIELDS = frozenset(
    {
        "abstention_code",
        "benefit_audit",
        "capabilities",
        "catalog_snapshot_id",
        "decision_digest",
        "plan_id",
        "status",
    }
)
_CAPABILITY_V3_FIELDS = frozenset(
    {
        "activation",
        "activation_lease_id",
        "catalog_snapshot_id",
        "current_authorized_material",
        "installation",
        "leases",
        "material_identity",
        "plan_id",
        "rollback_held",
        "rollback_owner_id",
        "selection",
    }
)
_SCOPE_FIELDS = frozenset(
    {
        "exposure_id",
        "host_context_id",
        "parent_exposure_id",
        "repository_id",
        "session_id",
        "tenant_id",
        "workspace_id",
    }
)
_PRIVACY_FIELDS = frozenset({"classification", "retention"})
_ACTION_FIELDS = frozenset(
    {
        "action_id",
        "catalog_snapshot_id",
        "consent_id",
        "entity_id",
        "expires_at",
        "kind",
        "lease_id",
        "payload",
        "plan_id",
        "precondition_revision",
        "privacy",
        "protocol_version",
        "required_host_feature",
        "rollback",
        "scope",
        "source_digest",
        "verification",
    }
)
_STATE_FIELDS = frozenset(
    {
        "blocked_capability_ids",
        "blocked_deactivation_ids",
        "blocked_install_descriptor_digests",
        "capabilities",
        "committed_plan",
        "evidence",
        "host_descriptor_digest",
        "host_level",
        "install_policy_snapshot_digest",
        "last_manual_bundle",
        "pending_effects",
        "pending_consents",
        "revision",
        "rollback_requested_capability_ids",
        "scope",
        "session_status",
        "terminal_cleanup_notified_ids",
    }
)
_HISTORICAL_STATE_FIELDS = _STATE_FIELDS - frozenset(
    {
        "blocked_install_descriptor_digests",
        "committed_plan",
        "install_policy_snapshot_digest",
        "pending_consents",
    }
)
_STATE_SCHEMA_V3 = "ctx.engine-state-v3"
_STATE_V3_FIELDS = _STATE_FIELDS | frozenset({"state_schema"})


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseRef:
    """One task, subtask, or agent's claim on a capability activation."""

    lease_id: str
    owner_id: str
    exposure_id: str

    def __post_init__(self) -> None:
        for field_name in ("lease_id", "owner_id", "exposure_id"):
            _required_text(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, str]:
        return {
            "exposure_id": self.exposure_id,
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> LeaseRef:
        value = _exact_fields(value, _LEASE_FIELDS, "lease")
        return cls(
            lease_id=value["lease_id"],
            owner_id=value["owner_id"],
            exposure_id=value["exposure_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityState:
    """Reducer-owned logical and receipt-confirmed state for one identity."""

    capability_id: str
    source_digest: str
    plan_id: str
    catalog_snapshot_id: str
    kind: str = "skill"
    actionability: str = "load"
    install_plan_digest: str | None = None
    install_descriptor_digest: str | None = None
    installation: str = "installed"
    leases: tuple[LeaseRef, ...] = ()
    activation: str = "inactive"
    activation_lease_id: str | None = None
    rollback_held: bool = False
    rollback_owner_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "capability_id",
            "source_digest",
            "plan_id",
            "catalog_snapshot_id",
        ):
            _required_text(getattr(self, field_name), field_name)
        if self.kind not in CAPABILITY_KINDS:
            _invalid(f"unknown capability kind {self.kind!r}")
        if self.capability_id.split(":", 1)[0] != self.kind:
            _invalid("capability kind does not match capability_id")
        if self.actionability not in ACTIONABILITY_STATES:
            _invalid(f"unknown actionability {self.actionability!r}")
        _optional_text(self.install_plan_digest, "install_plan_digest")
        _optional_text(self.install_descriptor_digest, "install_descriptor_digest")
        if self.actionability == "install" and self.install_plan_digest is None:
            _invalid("install actionability requires install_plan_digest")
        if self.actionability == "install" and self.install_descriptor_digest is None:
            _invalid("install actionability requires install_descriptor_digest")
        if self.install_plan_digest is not None:
            _sha256(self.install_plan_digest, "install_plan_digest")
        if self.install_descriptor_digest is not None:
            _sha256(self.install_descriptor_digest, "install_descriptor_digest")
        if self.actionability != "install" and self.install_plan_digest is not None:
            _invalid("install_plan_digest is valid only for install actionability")
        if self.actionability != "install" and self.install_descriptor_digest is not None:
            _invalid("install_descriptor_digest is valid only for install actionability")
        if self.installation not in INSTALLATION_STATES:
            _invalid(f"unknown installation state {self.installation!r}")
        if self.activation == "active" and self.installation != "installed":
            _invalid("active capability must be installed")
        if not isinstance(self.leases, tuple) or not all(
            isinstance(lease, LeaseRef) for lease in self.leases
        ):
            _invalid("leases must be an immutable tuple of LeaseRef values")
        lease_ids = tuple(lease.lease_id for lease in self.leases)
        if len(set(lease_ids)) != len(lease_ids):
            _invalid("capability has duplicate lease IDs")
        if lease_ids != tuple(sorted(lease_ids)):
            _invalid("capability leases must be sorted by lease_id")
        if self.activation not in ACTIVATION_STATES:
            _invalid(f"unknown activation state {self.activation!r}")
        _optional_text(self.activation_lease_id, "activation_lease_id")
        if self.activation == "active" and self.activation_lease_id is None:
            _invalid("active capability requires activation_lease_id")
        if self.activation == "inactive" and self.activation_lease_id is not None:
            _invalid("inactive capability cannot have activation_lease_id")
        if not isinstance(self.rollback_held, bool):
            _invalid("rollback_held must be a boolean")
        _optional_text(self.rollback_owner_id, "rollback_owner_id")
        if self.rollback_held != (self.rollback_owner_id is not None):
            _invalid("rollback_held and rollback_owner_id must be set together")

    @property
    def desired(self) -> bool:
        return bool(self.leases)

    def to_dict(self, *, include_installation: bool = True) -> dict[str, Any]:
        result = {
            "activation": self.activation,
            "activation_lease_id": self.activation_lease_id,
            "capability_id": self.capability_id,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "leases": [lease.to_dict() for lease in self.leases],
            "plan_id": self.plan_id,
            "rollback_held": self.rollback_held,
            "rollback_owner_id": self.rollback_owner_id,
            "source_digest": self.source_digest,
        }
        if include_installation:
            result.update(
                actionability=self.actionability,
                install_descriptor_digest=self.install_descriptor_digest,
                install_plan_digest=self.install_plan_digest,
                installation=self.installation,
                kind=self.kind,
            )
        return result

    @classmethod
    def from_dict(cls, value: object) -> CapabilityState:
        value = _compatible_fields(
            value,
            allowed=_CAPABILITY_FIELDS,
            required=_HISTORICAL_CAPABILITY_FIELDS,
            object_name="capability state",
        )
        if frozenset(value) not in {_HISTORICAL_CAPABILITY_FIELDS, _CAPABILITY_FIELDS}:
            missing = sorted(_CAPABILITY_FIELDS - set(value))
            _invalid(f"capability state is missing field(s): {', '.join(missing)}")
        capability_id = value["capability_id"]
        historical_kind = (
            capability_id.split(":", 1)[0]
            if isinstance(capability_id, str) and ":" in capability_id
            else "skill"
        )
        return cls(
            capability_id=capability_id,
            source_digest=value["source_digest"],
            plan_id=value["plan_id"],
            catalog_snapshot_id=value["catalog_snapshot_id"],
            kind=value.get("kind", historical_kind),
            actionability=value.get("actionability", "load"),
            install_descriptor_digest=value.get("install_descriptor_digest"),
            install_plan_digest=value.get("install_plan_digest"),
            installation=value.get("installation", "installed"),
            leases=tuple(
                LeaseRef.from_dict(item)
                for item in _array(value["leases"], "capability state leases")
            ),
            activation=value["activation"],
            activation_lease_id=value["activation_lease_id"],
            rollback_held=value["rollback_held"],
            rollback_owner_id=value["rollback_owner_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityEvidence:
    """Exposure and invocation evidence attributed to one host exposure."""

    exposure_id: str
    capability_id: str
    source_digest: str
    exposure: str = "unexposed"
    invocation: str = "not-invoked"

    def __post_init__(self) -> None:
        for field_name in ("exposure_id", "capability_id", "source_digest"):
            _required_text(getattr(self, field_name), field_name)
        if self.exposure not in EXPOSURE_STATES:
            _invalid(f"unknown exposure state {self.exposure!r}")
        if self.invocation not in INVOCATION_STATES:
            _invalid(f"unknown invocation state {self.invocation!r}")

    def to_dict(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "exposure": self.exposure,
            "exposure_id": self.exposure_id,
            "invocation": self.invocation,
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> CapabilityEvidence:
        value = _exact_fields(value, _EVIDENCE_FIELDS, "capability evidence")
        return cls(
            exposure_id=value["exposure_id"],
            capability_id=value["capability_id"],
            source_digest=value["source_digest"],
            exposure=value["exposure"],
            invocation=value["invocation"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingEffect:
    """A requested host effect awaiting an exact receipt."""

    action: HostAction
    effect: str
    rollback_capability_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, HostAction):
            _invalid("pending effect action must be a HostAction")
        if self.effect not in PENDING_EFFECTS:
            _invalid(f"unknown pending effect {self.effect!r}")
        expected_kind = {
            "install": "InstallCapability",
            "activate": "ActivateCapability",
            "rollback-activate": "ActivateCapability",
            "deactivate": "DeactivateCapability",
            "prepare": "PrepareExposure",
            "prompt-context": "PreparePromptContext",
        }[self.effect]
        if self.action.kind != expected_kind:
            _invalid(f"pending effect {self.effect!r} cannot use action {self.action.kind!r}")
        _optional_text(self.rollback_capability_id, "rollback_capability_id")
        if self.rollback_capability_id is not None and self.effect != "activate":
            _invalid("rollback_capability_id is valid only for activate effects")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.to_dict(),
            "effect": self.effect,
            "rollback_capability_id": self.rollback_capability_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> PendingEffect:
        value = _exact_fields(value, _PENDING_FIELDS, "pending effect")
        action_value = _exact_fields(value["action"], _ACTION_FIELDS, "pending action")
        _exact_fields(action_value["scope"], _SCOPE_FIELDS, "pending action scope")
        _exact_fields(action_value["privacy"], _PRIVACY_FIELDS, "pending action privacy")
        try:
            action = HostAction.from_dict(action_value)
        except (TypeError, ValueError) as exc:
            raise StateValidationError("pending effect contains an invalid action") from exc
        return cls(
            action=action,
            effect=value["effect"],
            rollback_capability_id=value["rollback_capability_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingConsent:
    """One exact precomputed install awaiting a provenance-bearing decision."""

    consent_id: str
    install_action: HostAction

    def __post_init__(self) -> None:
        _required_text(self.consent_id, "consent_id")
        if not isinstance(self.install_action, HostAction):
            _invalid("pending consent install_action must be a HostAction")
        if self.install_action.kind != "InstallCapability":
            _invalid("pending consent must bind InstallCapability")
        if self.install_action.consent_id != self.consent_id:
            _invalid("pending consent ID does not match install action")

    def to_dict(self) -> dict[str, Any]:
        return {
            "consent_id": self.consent_id,
            "install_action": self.install_action.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> PendingConsent:
        value = _exact_fields(value, _PENDING_CONSENT_FIELDS, "pending consent")
        action_value = _exact_fields(value["install_action"], _ACTION_FIELDS, "install action")
        _exact_fields(action_value["scope"], _SCOPE_FIELDS, "install action scope")
        _exact_fields(action_value["privacy"], _PRIVACY_FIELDS, "install action privacy")
        try:
            action = HostAction.from_dict(action_value)
        except (TypeError, ValueError) as exc:
            raise StateValidationError("pending consent contains an invalid action") from exc
        return cls(consent_id=value["consent_id"], install_action=action)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanCapability:
    """Authenticated identity projected from the last committed planner decision."""

    capability_id: str
    source_digest: str
    kind: str
    actionability: str
    install_plan_digest: str | None = None
    install_descriptor_digest: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.capability_id, "capability_id")
        _required_text(self.source_digest, "source_digest")
        if self.kind not in CAPABILITY_KINDS:
            _invalid(f"unknown plan capability kind {self.kind!r}")
        if self.capability_id.split(":", 1)[0] != self.kind:
            _invalid("plan capability kind does not match capability_id")
        if self.actionability not in ACTIONABILITY_STATES:
            _invalid(f"unknown plan actionability {self.actionability!r}")
        _optional_text(self.install_plan_digest, "install_plan_digest")
        _optional_text(self.install_descriptor_digest, "install_descriptor_digest")
        if self.actionability == "install" and self.install_plan_digest is None:
            _invalid("install plan capability requires install_plan_digest")
        if self.actionability == "install" and self.install_descriptor_digest is None:
            _invalid("install plan capability requires install_descriptor_digest")
        if self.install_plan_digest is not None:
            _sha256(self.install_plan_digest, "install_plan_digest")
        if self.install_descriptor_digest is not None:
            _sha256(self.install_descriptor_digest, "install_descriptor_digest")
        if self.actionability != "install" and self.install_plan_digest is not None:
            _invalid("non-install plan capability cannot have install_plan_digest")
        if self.actionability != "install" and self.install_descriptor_digest is not None:
            _invalid("non-install plan capability cannot have install_descriptor_digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "actionability": self.actionability,
            "capability_id": self.capability_id,
            "install_plan_digest": self.install_plan_digest,
            "install_descriptor_digest": self.install_descriptor_digest,
            "kind": self.kind,
            "source_digest": self.source_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> PlanCapability:
        value = _exact_fields(value, _PLAN_CAPABILITY_FIELDS, "plan capability")
        return cls(
            capability_id=value["capability_id"],
            source_digest=value["source_digest"],
            kind=value["kind"],
            actionability=value["actionability"],
            install_plan_digest=value["install_plan_digest"],
            install_descriptor_digest=value["install_descriptor_digest"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CommittedPlan:
    """The exact recommendation decision authorized for later reassessment."""

    plan_id: str
    catalog_snapshot_id: str
    decision_digest: str
    capabilities: tuple[PlanCapability, ...]

    def __post_init__(self) -> None:
        for field_name in ("plan_id", "catalog_snapshot_id", "decision_digest"):
            _required_text(getattr(self, field_name), field_name)
        _sha256(self.decision_digest, "decision_digest")
        if not isinstance(self.capabilities, tuple) or not all(
            isinstance(item, PlanCapability) for item in self.capabilities
        ):
            _invalid("committed plan capabilities must be PlanCapability values")
        ids = tuple(item.capability_id for item in self.capabilities)
        if len(set(ids)) != len(ids):
            _invalid("committed plan contains duplicate capability IDs")
        if len(ids) > MAX_ACTIVE_CAPABILITIES:
            _invalid("committed plan cannot contain more than five capabilities")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capabilities": [item.to_dict() for item in self.capabilities],
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "decision_digest": self.decision_digest,
            "plan_id": self.plan_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> CommittedPlan:
        value = _exact_fields(value, _COMMITTED_PLAN_FIELDS, "committed plan")
        return cls(
            plan_id=value["plan_id"],
            catalog_snapshot_id=value["catalog_snapshot_id"],
            decision_digest=value["decision_digest"],
            capabilities=tuple(
                PlanCapability.from_dict(item)
                for item in _array(value["capabilities"], "committed plan capabilities")
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanCapabilityV3:
    """One exact authenticated schema-v3 recommendation row."""

    selection: CapabilityPlanSelectionV3

    def __post_init__(self) -> None:
        # planning_v3 imports planner -> replay -> state.  Keeping this import
        # local lets state finish initialization before the planning types are
        # resolved and prevents a real module-import cycle.
        from .planning_v3 import CapabilityPlanSelectionV3

        if not isinstance(self.selection, CapabilityPlanSelectionV3):
            _invalid("schema-v3 plan capability requires CapabilityPlanSelectionV3")

    @property
    def capability_id(self) -> str:
        return self.selection.presentation.capability_id

    @property
    def source_digest(self) -> str:
        return self.selection.presentation.source_digest

    @property
    def kind(self) -> str:
        return self.selection.presentation.kind

    @property
    def name(self) -> str:
        return self.selection.presentation.name

    @property
    def actionability(self) -> str:
        return self.selection.presentation.actionability

    @property
    def install_descriptor_digest(self) -> str | None:
        return self.selection.presentation.install_descriptor_digest

    @property
    def install_plan_digest(self) -> str | None:
        return self.selection.presentation.install_plan_digest

    @property
    def catalog_identity(self) -> CatalogCapabilityIdentity:
        return self.selection.catalog_identity

    @property
    def benefit(self) -> CapabilityBenefitProjection:
        return self.selection.benefit

    @property
    def authority(self) -> PlanningAuthority:
        return self.selection.authority

    def to_dict(self) -> dict[str, Any]:
        return self.selection.to_mapping()

    @classmethod
    def from_dict(cls, value: object) -> PlanCapabilityV3:
        value = _exact_fields(value, _PLAN_CAPABILITY_V3_FIELDS, "schema-v3 plan capability")
        benefit_value = _exact_fields(
            value["benefit"],
            _BENEFIT_PROJECTION_FIELDS,
            "schema-v3 benefit projection",
        )
        authority_value = value["authority"]
        if not isinstance(authority_value, Mapping):
            _invalid("schema-v3 planning authority must be a JSON object")
        authority_type = authority_value.get("type")
        if authority_type == "load":
            authority_value = _exact_fields(
                authority_value,
                _LOAD_AUTHORITY_FIELDS,
                "schema-v3 load authority",
            )
        elif authority_type == "install":
            authority_value = _exact_fields(
                authority_value,
                _INSTALL_AUTHORITY_FIELDS,
                "schema-v3 install authority",
            )
        elif authority_type == "manual":
            authority_value = _exact_fields(
                authority_value,
                _MANUAL_AUTHORITY_FIELDS,
                "schema-v3 manual authority",
            )
        else:
            _invalid("schema-v3 planning authority type is unsupported")

        from .installation import InstallPlanDescriptor
        from .planner import CapabilityCandidate
        from .planning_v3 import (
            CapabilityBenefitProjection,
            CapabilityPlanSelectionV3,
            InstallPlanningAuthority,
            LoadPlanningAuthority,
            ManualPlanningAuthority,
        )

        try:
            presentation = CapabilityCandidate(
                capability_id=value["capability_id"],
                kind=value["kind"],
                name=value["name"],
                source_digest=value["catalog_entry_digest"],
                normalized_score_ppm=value["normalized_score_ppm"],
                matching_signals=tuple(
                    _array(value["matching_signals"], "schema-v3 matching_signals")
                ),
                reason_codes=tuple(_array(value["reason_codes"], "schema-v3 reason_codes")),
                actionability=value["actionability"],
                install_descriptor_digest=value["install_descriptor_digest"],
                install_plan_digest=value["install_plan_digest"],
            )
            catalog_identity = CatalogCapabilityIdentity.from_dict(value["catalog_identity"])
            benefit = CapabilityBenefitProjection(
                tier=benefit_value["tier"],
                individual_net_benefit_u=benefit_value["individual_net_benefit_u"],
                marginal_net_benefit_u=benefit_value["marginal_net_benefit_u"],
            )
            authority: Any
            if authority_type == "load":
                authority = LoadPlanningAuthority(
                    material=AuthorizedMaterial.from_dict(authority_value["material"])
                )
            elif authority_type == "install":
                authority = InstallPlanningAuthority(
                    descriptor=InstallPlanDescriptor.from_dict(authority_value["descriptor"]),
                    result_material=MaterialIdentity.from_dict(authority_value["result_material"]),
                )
            else:
                authority = ManualPlanningAuthority()
            selection = CapabilityPlanSelectionV3(
                presentation=presentation,
                catalog_identity=catalog_identity,
                benefit=benefit,
                authority=authority,
            )
        except StateValidationError:
            raise
        except (TypeError, ValueError) as exc:
            raise StateValidationError(
                f"schema-v3 plan capability identity, benefit, or authority is invalid: {exc}"
            ) from exc
        return cls(selection=selection)


def _plan_capability_v3_order(item: PlanCapabilityV3) -> tuple[int, str, str]:
    return (
        0 if item.benefit.tier == "executable" else 1,
        item.capability_id,
        item.source_digest,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CommittedPlanV3:
    """Strict persisted schema-v3 plan with its replay-safe benefit audit."""

    plan_id: str
    catalog_snapshot_id: str
    decision_digest: str
    status: str
    abstention_code: str | None
    benefit_audit: BenefitAuditReference | None
    capabilities: tuple[PlanCapabilityV3, ...]

    def __post_init__(self) -> None:
        from .benefit import ABSTENTION_CODES
        from .planner import DEGRADATION_CODES
        from .planning_v3 import BenefitAuditReference

        _required_text(self.plan_id, "plan_id")
        _sha256(self.catalog_snapshot_id, "catalog_snapshot_id")
        _sha256(self.decision_digest, "decision_digest")
        if not isinstance(self.capabilities, tuple) or not all(
            isinstance(item, PlanCapabilityV3) for item in self.capabilities
        ):
            _invalid("schema-v3 committed plan capabilities must be PlanCapabilityV3 values")
        if len(self.capabilities) > MAX_ACTIVE_CAPABILITIES:
            _invalid("schema-v3 committed plan cannot contain more than five capabilities")
        ids = tuple(item.capability_id for item in self.capabilities)
        if len(set(ids)) != len(ids):
            _invalid("schema-v3 committed plan contains duplicate capability IDs")
        if self.capabilities != tuple(sorted(self.capabilities, key=_plan_capability_v3_order)):
            _invalid("schema-v3 committed plan capabilities are not in canonical order")

        if self.status == "ready":
            if not self.capabilities or self.abstention_code is not None:
                _invalid("ready schema-v3 plan requires rows and no abstention code")
            if not isinstance(self.benefit_audit, BenefitAuditReference):
                _invalid("ready schema-v3 plan requires the exact benefit audit")
        elif self.status == "abstained":
            if self.capabilities or self.abstention_code not in ABSTENTION_CODES:
                _invalid("abstained schema-v3 plan requires no rows and a declared code")
            if not isinstance(self.benefit_audit, BenefitAuditReference):
                _invalid("abstained schema-v3 plan requires the exact benefit audit")
        elif self.status == "degraded":
            if (
                self.capabilities
                or self.abstention_code not in DEGRADATION_CODES
                or self.benefit_audit is not None
            ):
                _invalid("degraded schema-v3 plan requires no rows or benefit audit")
        else:
            _invalid("schema-v3 committed plan status is unsupported")

        if self.benefit_audit is not None:
            if self.benefit_audit.requested_limit < len(self.capabilities):
                _invalid("benefit audit requested_limit is smaller than persisted rows")
            if self.benefit_audit.candidate_pool_count < len(self.capabilities):
                _invalid("benefit audit candidate_pool_count is smaller than persisted rows")
            if self.status == "ready" and self.benefit_audit.search_evaluation_count == 0:
                _invalid("ready benefit audit requires positive search evaluations")
            if self.abstention_code == "limit-zero" and (
                self.benefit_audit.requested_limit != 0
                or self.benefit_audit.search_evaluation_count != 0
            ):
                _invalid("limit-zero benefit audit requires a zero limit and zero search")
            if self.abstention_code == "below-net-benefit" and (
                self.benefit_audit.requested_limit == 0
                or self.benefit_audit.candidate_pool_count == 0
                or self.benefit_audit.search_evaluation_count == 0
            ):
                _invalid(
                    "below-net-benefit audit requires candidates, a positive limit, and search"
                )
            if self.abstention_code == "no-feasible-capability" and (
                self.benefit_audit.requested_limit == 0
                or self.benefit_audit.search_evaluation_count != 0
            ):
                _invalid("no-feasible-capability audit requires a positive limit and zero search")

    def to_dict(self) -> dict[str, Any]:
        return {
            "abstention_code": self.abstention_code,
            "benefit_audit": (
                None if self.benefit_audit is None else self.benefit_audit.to_mapping()
            ),
            "capabilities": [item.to_dict() for item in self.capabilities],
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "decision_digest": self.decision_digest,
            "plan_id": self.plan_id,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, value: object) -> CommittedPlanV3:
        value = _exact_fields(value, _COMMITTED_PLAN_V3_FIELDS, "schema-v3 committed plan")
        audit_value = value["benefit_audit"]
        audit: BenefitAuditReference | None
        if audit_value is None:
            audit = None
        else:
            audit_value = _exact_fields(
                audit_value,
                _BENEFIT_AUDIT_FIELDS,
                "schema-v3 benefit audit",
            )
            from .planning_v3 import BenefitAuditReference

            try:
                audit = BenefitAuditReference(
                    result_schema_id=audit_value["result_schema_id"],
                    result_digest=audit_value["result_digest"],
                    policy_schema_id=audit_value["policy_schema_id"],
                    policy_digest=audit_value["policy_digest"],
                    selection_algorithm_id=audit_value["selection_algorithm_id"],
                    calibration_digest=audit_value["calibration_digest"],
                    requested_limit=audit_value["requested_limit"],
                    candidate_pool_count=audit_value["candidate_pool_count"],
                    search_evaluation_count=audit_value["search_evaluation_count"],
                )
            except (TypeError, ValueError) as exc:
                raise StateValidationError("schema-v3 benefit audit is invalid") from exc
        return cls(
            plan_id=value["plan_id"],
            catalog_snapshot_id=value["catalog_snapshot_id"],
            decision_digest=value["decision_digest"],
            status=value["status"],
            abstention_code=value["abstention_code"],
            benefit_audit=audit,
            capabilities=tuple(
                PlanCapabilityV3.from_dict(item)
                for item in _array(
                    value["capabilities"],
                    "schema-v3 committed plan capabilities",
                )
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityStateV3:
    """Runtime state bound to one exact schema-v3 selection and material."""

    selection: PlanCapabilityV3
    material_identity: MaterialIdentity
    current_authorized_material: AuthorizedMaterial | None
    installation: str
    plan_id: str
    catalog_snapshot_id: str
    leases: tuple[LeaseRef, ...] = ()
    activation: str = "inactive"
    activation_lease_id: str | None = None
    rollback_held: bool = False
    rollback_owner_id: str | None = None

    def __post_init__(self) -> None:
        from .planning_v3 import (
            InstallPlanningAuthority,
            LoadPlanningAuthority,
            ManualPlanningAuthority,
        )

        if not isinstance(self.selection, PlanCapabilityV3):
            _invalid("schema-v3 capability selection must be PlanCapabilityV3")
        if isinstance(self.selection.authority, ManualPlanningAuthority):
            _invalid("manual schema-v3 selections cannot create lifecycle state")
        if not isinstance(self.material_identity, MaterialIdentity):
            _invalid("schema-v3 capability requires exact material_identity")
        _required_text(self.plan_id, "plan_id")
        _sha256(self.catalog_snapshot_id, "catalog_snapshot_id")
        if self.installation not in INSTALLATION_STATES:
            _invalid(f"unknown installation state {self.installation!r}")
        if not isinstance(self.leases, tuple) or not all(
            isinstance(lease, LeaseRef) for lease in self.leases
        ):
            _invalid("leases must be an immutable tuple of LeaseRef values")
        lease_ids = tuple(lease.lease_id for lease in self.leases)
        if len(set(lease_ids)) != len(lease_ids):
            _invalid("schema-v3 capability has duplicate lease IDs")
        if lease_ids != tuple(sorted(lease_ids)):
            _invalid("schema-v3 capability leases must be sorted by lease_id")
        if self.activation not in ACTIVATION_STATES:
            _invalid(f"unknown activation state {self.activation!r}")
        _optional_text(self.activation_lease_id, "activation_lease_id")
        if self.activation == "active" and self.activation_lease_id is None:
            _invalid("active capability requires activation_lease_id")
        if self.activation == "inactive" and self.activation_lease_id is not None:
            _invalid("inactive capability cannot have activation_lease_id")
        if self.activation == "active" and self.installation != "installed":
            _invalid("active capability must be installed")
        if not isinstance(self.rollback_held, bool):
            _invalid("rollback_held must be a boolean")
        _optional_text(self.rollback_owner_id, "rollback_owner_id")
        if self.rollback_held != (self.rollback_owner_id is not None):
            _invalid("rollback_held and rollback_owner_id must be set together")

        expected_identity = (
            self.selection.capability_id,
            self.selection.kind,
        )
        if (
            self.material_identity.capability_id,
            self.material_identity.kind,
        ) != expected_identity:
            _invalid("material_identity does not match schema-v3 selection")

        if isinstance(self.selection.authority, LoadPlanningAuthority):
            load_authority = self.selection.authority.material
            if self.installation != "installed":
                _invalid("load selection must have installed lifecycle material")
            if self.current_authorized_material != load_authority:
                _invalid("current_authorized_material does not match load authority")
        elif isinstance(self.selection.authority, InstallPlanningAuthority):
            install_authority = self.selection.authority
            if self.material_identity != install_authority.result_material:
                _invalid("material_identity does not match install result authority")
            if self.installation == "absent":
                if self.current_authorized_material is not None:
                    _invalid("absent install selection cannot have authorized material")
                if self.activation != "inactive":
                    _invalid("absent install selection must be inactive")
                return
            current = self.current_authorized_material
            if current is None or current.origin != "installed":
                _invalid("promoted install requires installed current_authorized_material")
            lineage = current.installed_material_lineage
            if lineage is None or (
                lineage.origin_install_descriptor_digest
                != install_authority.descriptor.descriptor_digest
            ):
                _invalid("install promotion lineage does not match selected descriptor")
        else:
            _invalid("schema-v3 selection authority is unsupported")

        current = self.current_authorized_material
        if current is None:
            _invalid("installed capability requires current_authorized_material")
        if (
            current.capability_id,
            current.kind,
            current.catalog_identity_digest,
            current.material_identity_digest,
        ) != (
            self.selection.capability_id,
            self.selection.kind,
            self.selection.catalog_identity.identity_digest,
            self.material_identity.identity_digest,
        ):
            _invalid("current_authorized_material identity does not match selection")

    @property
    def capability_id(self) -> str:
        return self.selection.capability_id

    @property
    def source_digest(self) -> str:
        return self.selection.source_digest

    @property
    def kind(self) -> str:
        return self.selection.kind

    @property
    def name(self) -> str:
        return self.selection.name

    @property
    def actionability(self) -> str:
        return self.selection.actionability

    @property
    def catalog_identity(self) -> CatalogCapabilityIdentity:
        return self.selection.catalog_identity

    @property
    def installed_lineage(self) -> InstalledMaterialLineage | None:
        current = self.current_authorized_material
        return None if current is None else current.installed_material_lineage

    @property
    def desired(self) -> bool:
        return bool(self.leases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "activation": self.activation,
            "activation_lease_id": self.activation_lease_id,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "current_authorized_material": (
                None
                if self.current_authorized_material is None
                else self.current_authorized_material.to_dict()
            ),
            "installation": self.installation,
            "leases": [lease.to_dict() for lease in self.leases],
            "material_identity": self.material_identity.to_dict(),
            "plan_id": self.plan_id,
            "rollback_held": self.rollback_held,
            "rollback_owner_id": self.rollback_owner_id,
            "selection": self.selection.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> CapabilityStateV3:
        value = _exact_fields(value, _CAPABILITY_V3_FIELDS, "schema-v3 capability state")
        try:
            material_identity = MaterialIdentity.from_dict(value["material_identity"])
            raw_current = value["current_authorized_material"]
            current = None if raw_current is None else AuthorizedMaterial.from_dict(raw_current)
        except (TypeError, ValueError) as exc:
            raise StateValidationError(
                f"schema-v3 material_identity or authorized lineage is invalid: {exc}"
            ) from exc
        return cls(
            selection=PlanCapabilityV3.from_dict(value["selection"]),
            material_identity=material_identity,
            current_authorized_material=current,
            installation=value["installation"],
            plan_id=value["plan_id"],
            catalog_snapshot_id=value["catalog_snapshot_id"],
            leases=tuple(
                LeaseRef.from_dict(item)
                for item in _array(value["leases"], "schema-v3 capability leases")
            ),
            activation=value["activation"],
            activation_lease_id=value["activation_lease_id"],
            rollback_held=value["rollback_held"],
            rollback_owner_id=value["rollback_owner_id"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineState:
    """Complete immutable projection for one current-work session."""

    revision: int
    scope: ScopeRef
    host_level: str
    host_descriptor_digest: str | None
    session_status: str = "active"
    capabilities: tuple[CapabilityState | CapabilityStateV3, ...] = ()
    pending_effects: tuple[PendingEffect, ...] = ()
    pending_consents: tuple[PendingConsent, ...] = ()
    committed_plan: CommittedPlan | CommittedPlanV3 | None = None
    evidence: tuple[CapabilityEvidence, ...] = ()
    last_manual_bundle: tuple[str, ...] = ()
    blocked_capability_ids: tuple[str, ...] = ()
    blocked_deactivation_ids: tuple[str, ...] = ()
    blocked_install_descriptor_digests: tuple[str, ...] = ()
    install_policy_snapshot_digest: str | None = None
    rollback_requested_capability_ids: tuple[str, ...] = ()
    terminal_cleanup_notified_ids: tuple[str, ...] = ()
    _contract_version: int = field(default=1, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            _invalid("revision must be an integer >= 1")
        if not isinstance(self.scope, ScopeRef):
            _invalid("scope must be a ScopeRef")
        if self.host_level not in HOST_LEVELS:
            _invalid(f"unknown host_level {self.host_level!r}")
        if self._contract_version == 3:
            _sha256(self.host_descriptor_digest, "host_descriptor_digest")
        else:
            _required_text(self.host_descriptor_digest, "host_descriptor_digest")
        if self.session_status not in SESSION_STATUSES:
            _invalid(f"unknown session_status {self.session_status!r}")
        if self._contract_version not in {1, 2, 3}:
            _invalid("unknown state contract version")
        if self.install_policy_snapshot_digest is not None:
            _sha256(
                self.install_policy_snapshot_digest,
                "install_policy_snapshot_digest",
            )
        self._validate_nested_values()
        self._validate_cross_references()

    def _validate_nested_values(self) -> None:
        capability_type: type[CapabilityState] | type[CapabilityStateV3] = (
            CapabilityStateV3 if self._contract_version == 3 else CapabilityState
        )
        for field_name, nested_values, value_type in (
            ("capabilities", self.capabilities, capability_type),
            ("pending_effects", self.pending_effects, PendingEffect),
            ("pending_consents", self.pending_consents, PendingConsent),
            ("evidence", self.evidence, CapabilityEvidence),
        ):
            if not isinstance(nested_values, tuple) or not all(
                isinstance(value, value_type) for value in nested_values
            ):
                _invalid(f"{field_name} must be an immutable tuple of {value_type.__name__}")
        capability_ids = tuple(item.capability_id for item in self.capabilities)
        if len(set(capability_ids)) != len(capability_ids):
            _invalid("duplicate capability ID in projection")
        if capability_ids != tuple(sorted(capability_ids)):
            _invalid("capabilities must be sorted by capability_id")
        lease_ids = tuple(
            lease.lease_id for capability in self.capabilities for lease in capability.leases
        )
        if len(set(lease_ids)) != len(lease_ids):
            _invalid("duplicate lease ID across capabilities")
        evidence_ids = tuple((item.exposure_id, item.capability_id) for item in self.evidence)
        if len(set(evidence_ids)) != len(evidence_ids):
            _invalid("duplicate evidence identity in projection")
        if evidence_ids != tuple(sorted(evidence_ids)):
            _invalid("evidence must be sorted by exposure and capability")
        action_ids = tuple(item.action.action_id for item in self.pending_effects)
        consent_ids = tuple(item.consent_id for item in self.pending_consents)
        consent_action_ids = tuple(item.install_action.action_id for item in self.pending_consents)
        pending_entities = tuple(item.action.entity_id for item in self.pending_effects)
        consent_entities = tuple(item.install_action.entity_id for item in self.pending_consents)
        if len(set(action_ids)) != len(action_ids):
            _invalid("duplicate pending action ID in projection")
        if len(set(consent_ids)) != len(consent_ids):
            _invalid("duplicate pending consent ID in projection")
        if len(set(consent_action_ids)) != len(consent_action_ids):
            _invalid("duplicate pending consent action ID in projection")
        if set(action_ids) & set(consent_action_ids):
            _invalid("install action cannot await consent and receipt simultaneously")
        if self._contract_version == 3:
            if len(set(pending_entities)) != len(pending_entities):
                _invalid("capability cannot have multiple pending lifecycle operations")
            if len(set(consent_entities)) != len(consent_entities):
                _invalid("capability cannot have multiple pending install consents")
            if set(pending_entities) & set(consent_entities):
                _invalid(
                    "install cannot await consent and a pending effect for the same capability"
                )
        plan_type: type[CommittedPlan] | type[CommittedPlanV3] = (
            CommittedPlanV3 if self._contract_version == 3 else CommittedPlan
        )
        if self.committed_plan is not None and not isinstance(self.committed_plan, plan_type):
            _invalid(f"committed_plan must be a {plan_type.__name__} or null")
        if self._contract_version != 3 and (
            self.committed_plan is not None
            or self.pending_consents
            or self.blocked_install_descriptor_digests
            or self.install_policy_snapshot_digest is not None
        ):
            _invalid("historical state cannot contain schema-v3 projection values")
        if len(self.active_capability_ids) > MAX_ACTIVE_CAPABILITIES:
            _invalid("projection cannot contain more than five active capabilities")
        for field_name in (
            "last_manual_bundle",
            "blocked_capability_ids",
            "blocked_deactivation_ids",
            "blocked_install_descriptor_digests",
            "rollback_requested_capability_ids",
            "terminal_cleanup_notified_ids",
        ):
            id_values = cast(tuple[str, ...], getattr(self, field_name))
            if not isinstance(id_values, tuple):
                _invalid(f"{field_name} must be an immutable tuple")
            for index, value in enumerate(id_values):
                _required_text(value, f"{field_name}[{index}]")
                if field_name == "blocked_install_descriptor_digests":
                    _sha256(value, f"{field_name}[{index}]")
            if len(set(id_values)) != len(id_values):
                _invalid(f"{field_name} contains duplicate IDs")
            if id_values != tuple(sorted(id_values)):
                _invalid(f"{field_name} must be sorted")
        if len(self.last_manual_bundle) > MAX_ACTIVE_CAPABILITIES:
            _invalid("last_manual_bundle cannot contain more than five capabilities")

    def _validate_cross_references(self) -> None:
        if self._contract_version == 3:
            self._validate_cross_references_v3()
            return
        capabilities = {
            item.capability_id: item
            for item in cast(tuple[CapabilityState, ...], self.capabilities)
        }
        for evidence in self.evidence:
            capability = capabilities.get(evidence.capability_id)
            if capability is None:
                _invalid("evidence references unknown capability")
            if evidence.source_digest != capability.source_digest:
                _invalid("evidence source_digest does not match capability")
        for pending in self.pending_effects:
            entity_id = pending.action.entity_id
            capability = capabilities.get(entity_id or "")
            if capability is None:
                _invalid("pending action references unknown capability")
            if pending.action.source_digest != capability.source_digest:
                _invalid("pending action source_digest does not match capability")
            if pending.action.plan_id != capability.plan_id:
                _invalid("pending action plan_id does not match capability")
            if pending.action.catalog_snapshot_id != capability.catalog_snapshot_id:
                _invalid("pending action catalog snapshot does not match capability")
            # The pure reducer builds an immutable intermediate projection before
            # replacing its revision with the committed N+1 value.
            if pending.action.precondition_revision > self.revision + 1:
                _invalid("pending action precondition_revision exceeds next state revision")
            if not _same_stream(pending.action.scope, self.scope):
                _invalid("pending action scope belongs to another session")
            if pending.effect == "install":
                if capability.installation != "absent" or capability.activation != "inactive":
                    _invalid("pending install must target an absent inactive capability")
                if (
                    pending.action.payload.get("install_descriptor_digest")
                    != capability.install_descriptor_digest
                ):
                    _invalid("pending install install_descriptor_digest does not match capability")
                if (
                    pending.action.payload.get("install_plan_digest")
                    != capability.install_plan_digest
                ):
                    _invalid("pending install install_plan_digest does not match capability")
            elif pending.effect in {"activate", "rollback-activate"}:
                if capability.activation != "inactive":
                    _invalid("pending activation targets an already active capability")
            elif capability.activation != "active":
                _invalid(f"pending {pending.effect} targets an inactive capability")
            if pending.rollback_capability_id is not None:
                rollback = capabilities.get(pending.rollback_capability_id)
                if rollback is None or not rollback.rollback_held:
                    _invalid("pending activation has invalid rollback capability")
        for pending_consent in self.pending_consents:
            action = pending_consent.install_action
            capability = capabilities.get(action.entity_id or "")
            if capability is None:
                _invalid("pending consent references unknown capability")
            if capability.installation != "absent" or capability.activation != "inactive":
                _invalid("pending consent must target an absent inactive capability")
            if action.source_digest != capability.source_digest:
                _invalid("pending consent source_digest does not match capability")
            if action.plan_id != capability.plan_id:
                _invalid("pending consent plan_id does not match capability")
            if action.catalog_snapshot_id != capability.catalog_snapshot_id:
                _invalid("pending consent catalog snapshot does not match capability")
            if (
                action.payload.get("install_descriptor_digest")
                != capability.install_descriptor_digest
            ):
                _invalid("pending consent install_descriptor_digest does not match capability")
            if action.payload.get("install_plan_digest") != capability.install_plan_digest:
                _invalid("pending consent install_plan_digest does not match capability")
            if action.payload.get("policy_snapshot_digest") != self.install_policy_snapshot_digest:
                _invalid("pending consent policy snapshot does not match current policy")
            # While reducing N -> N+1, consent precomputes install for the
            # immediately following N+2 decision transition.
            if action.precondition_revision > self.revision + 2:
                _invalid("pending consent action exceeds decision target revision")
            if not _same_stream(action.scope, self.scope):
                _invalid("pending consent belongs to another session")
        known = set(capabilities)
        for field_name in (
            "last_manual_bundle",
            "blocked_capability_ids",
            "blocked_deactivation_ids",
            "rollback_requested_capability_ids",
            "terminal_cleanup_notified_ids",
        ):
            if not set(getattr(self, field_name)) <= known:
                _invalid(f"{field_name} references unknown capability")
        if not set(self.terminal_cleanup_notified_ids) <= set(self.blocked_deactivation_ids):
            _invalid("terminal cleanup notification must reference blocked deactivation")

    def _validate_cross_references_v3(self) -> None:
        from .planning_v3 import InstallPlanningAuthority, ManualPlanningAuthority

        capabilities = {
            item.capability_id: item
            for item in cast(tuple[CapabilityStateV3, ...], self.capabilities)
        }
        plan = cast(CommittedPlanV3 | None, self.committed_plan)
        plan_rows = () if plan is None else plan.capabilities
        if plan is None and (
            capabilities or self.last_manual_bundle or self.blocked_install_descriptor_digests
        ):
            _invalid("schema-v3 runtime references require a committed plan")
        rows_by_id = {item.capability_id: item for item in plan_rows}
        for capability in capabilities.values():
            row = rows_by_id.get(capability.capability_id)
            if row is not None:
                if capability.selection != row:
                    _invalid("schema-v3 runtime capability is not the exact committed plan row")
                if plan is None:
                    _invalid("schema-v3 runtime capability requires a committed plan")
                if capability.plan_id != plan.plan_id or capability.catalog_snapshot_id != (
                    plan.catalog_snapshot_id
                ):
                    _invalid("schema-v3 capability plan identity does not match committed plan")
            elif plan is not None:
                if capability.plan_id == plan.plan_id:
                    _invalid("omitted runtime cannot mint current-plan authority")
                if plan.status in {"ready", "abstained"} and capability.leases:
                    _invalid("unselected runtime capability cannot retain current leases")

        expected_manual = (
            tuple(
                item.capability_id
                for item in plan_rows
                if isinstance(item.authority, ManualPlanningAuthority)
            )
            if plan is not None and plan.status == "ready"
            else ()
        )
        if self.last_manual_bundle != expected_manual:
            _invalid("last_manual_bundle does not exactly match committed manual rows")

        for evidence in self.evidence:
            evidence_capability = capabilities.get(evidence.capability_id)
            if evidence_capability is None:
                _invalid("evidence references unknown capability")
            if evidence.source_digest != evidence_capability.source_digest:
                _invalid("evidence source_digest does not match capability")

        for pending in self.pending_effects:
            action = pending.action
            if pending.effect == "prompt-context":
                self._validate_v3_prompt_context_action(pending, capabilities)
                continue
            pending_capability = capabilities.get(action.entity_id or "")
            if pending_capability is None:
                _invalid("pending action references unknown capability")
            self._validate_v3_action_identity(action, pending_capability)
            if action.precondition_revision > self.revision + 1:
                _invalid("pending action precondition_revision exceeds next state revision")
            if not _same_stream(action.scope, self.scope):
                _invalid("pending action scope belongs to another session")
            if pending.effect == "install":
                if (
                    pending_capability.installation != "absent"
                    or pending_capability.activation != "inactive"
                ):
                    _invalid("pending install must target an absent inactive capability")
                self._validate_v3_install_action(action, pending_capability)
            else:
                self._validate_v3_material_action(action, pending_capability)
                if pending.effect in {"activate", "rollback-activate"}:
                    if pending_capability.activation != "inactive":
                        _invalid("pending activation targets an already active capability")
                elif pending_capability.activation != "active":
                    _invalid(f"pending {pending.effect} targets an inactive capability")
                self._validate_v3_pending_lease(pending, pending_capability)
            if pending.rollback_capability_id is not None:
                rollback = capabilities.get(pending.rollback_capability_id)
                if (
                    rollback is None
                    or not rollback.rollback_held
                    or rollback.activation != "inactive"
                    or rollback.capability_id == pending_capability.capability_id
                ):
                    _invalid("pending activation has invalid rollback capability")

        for pending_consent in self.pending_consents:
            action = pending_consent.install_action
            consent_capability = capabilities.get(action.entity_id or "")
            if consent_capability is None:
                _invalid("pending consent references unknown capability")
            if (
                consent_capability.installation != "absent"
                or consent_capability.activation != "inactive"
            ):
                _invalid("pending consent must target an absent inactive capability")
            self._validate_v3_action_identity(action, consent_capability)
            self._validate_v3_install_action(action, consent_capability)
            if action.precondition_revision > self.revision + 2:
                _invalid("pending consent action exceeds decision target revision")
            if not _same_stream(action.scope, self.scope):
                _invalid("pending consent belongs to another session")

        known = set(capabilities)
        for field_name in (
            "blocked_capability_ids",
            "blocked_deactivation_ids",
            "rollback_requested_capability_ids",
            "terminal_cleanup_notified_ids",
        ):
            if not set(getattr(self, field_name)) <= known:
                _invalid(f"{field_name} references unknown capability")
        install_descriptor_authorities = {
            item.selection.authority.descriptor.descriptor_digest
            for item in capabilities.values()
            if isinstance(item.selection.authority, InstallPlanningAuthority)
        } | {
            item.authority.descriptor.descriptor_digest
            for item in plan_rows
            if isinstance(item.authority, InstallPlanningAuthority)
        }
        if not set(self.blocked_install_descriptor_digests) <= (install_descriptor_authorities):
            _invalid("blocked install descriptor lacks retained or current authority")
        if not set(self.terminal_cleanup_notified_ids) <= set(self.blocked_deactivation_ids):
            _invalid("terminal cleanup notification must reference blocked deactivation")

    def _validate_v3_prompt_context_action(
        self,
        pending: PendingEffect,
        capabilities: Mapping[str, CapabilityStateV3],
    ) -> None:
        from .planning_v3 import LoadPlanningAuthority

        action = pending.action
        plan = self.committed_plan
        expected_intent = {
            "prompt-context-activate": "activate",
            "prompt-context-experiment": "experiment",
        }.get(self.host_level)
        if (
            not isinstance(plan, CommittedPlanV3)
            or plan.status != "ready"
            or expected_intent is None
            or action.kind != "PreparePromptContext"
            or action.entity_id is not None
            or action.required_host_feature != "prompt-context"
            or action.plan_id != plan.plan_id
            or action.catalog_snapshot_id != plan.catalog_snapshot_id
            or action.payload.get("plan_digest") != plan.decision_digest
            or action.payload.get("execution_intent") != expected_intent
            or action.precondition_revision > self.revision + 1
            or not _same_stream(action.scope, self.scope)
        ):
            _invalid("pending prompt context action has invalid bundle identity")
        rows = action.payload.get("capabilities")
        if not isinstance(rows, tuple) or not 1 <= len(rows) <= MAX_ACTIVE_CAPABILITIES:
            _invalid("pending prompt context action has invalid capability rows")
        seen: set[str] = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                _invalid("pending prompt context row must be an object")
            capability_id = raw.get("capability_id")
            if not isinstance(capability_id, str) or capability_id in seen:
                _invalid("pending prompt context has invalid capability identity")
            seen.add(capability_id)
            capability = capabilities.get(capability_id)
            if capability is None or not isinstance(
                capability.selection.authority,
                LoadPlanningAuthority,
            ):
                _invalid("pending prompt context is not an exact committed load selection")
            current = capability.current_authorized_material
            expected = {
                "authorized_material": None if current is None else current.to_dict(),
                "capability_id": capability.capability_id,
                "capability_kind": capability.kind,
                "catalog_identity": capability.catalog_identity.to_dict(),
                "material_identity": capability.material_identity.to_dict(),
                "source_digest": capability.source_digest,
            }
            if dict(raw) != expected or capability.installation != "installed":
                _invalid("pending prompt context row does not match current material authority")

    @staticmethod
    def _validate_v3_action_identity(
        action: HostAction,
        capability: CapabilityStateV3,
    ) -> None:
        if action.source_digest != capability.source_digest:
            _invalid("pending action source_digest does not match capability")
        if action.plan_id != capability.plan_id:
            _invalid("pending action plan_id does not match capability")
        if action.catalog_snapshot_id != capability.catalog_snapshot_id:
            _invalid("pending action catalog snapshot does not match capability")

    def _validate_v3_install_action(
        self,
        action: HostAction,
        capability: CapabilityStateV3,
    ) -> None:
        from .planning_v3 import InstallPlanningAuthority

        authority = capability.selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            _invalid("pending install lacks committed install authority")
        payload = action.payload
        if payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3:
            _invalid("pending install does not use schema-v3 payload")
        if (
            payload.get("capability_kind") != capability.kind
            or payload.get("catalog_identity") != capability.catalog_identity.to_dict()
            or payload.get("result_material") != capability.material_identity.to_dict()
            or payload.get("install_plan_descriptor") != authority.descriptor.to_dict()
        ):
            _invalid("pending install typed authority does not match runtime capability")
        if payload.get("policy_snapshot_digest") != self.install_policy_snapshot_digest:
            _invalid("pending install policy snapshot does not match current policy")

    @staticmethod
    def _validate_v3_material_action(
        action: HostAction,
        capability: CapabilityStateV3,
    ) -> None:
        current = capability.current_authorized_material
        payload = action.payload
        if current is None or capability.installation != "installed":
            _invalid("pending material action requires installed authorized material")
        if payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3:
            _invalid("pending material action does not use schema-v3 payload")
        if (
            payload.get("capability_kind") != capability.kind
            or payload.get("catalog_identity") != capability.catalog_identity.to_dict()
            or payload.get("material_identity") != capability.material_identity.to_dict()
            or payload.get("authorized_material") != current.to_dict()
        ):
            _invalid("pending action authorized material does not match runtime capability")

    def _validate_v3_pending_lease(
        self,
        pending: PendingEffect,
        capability: CapabilityStateV3,
    ) -> None:
        action = pending.action
        if pending.effect == "activate":
            lease = next(
                (item for item in capability.leases if item.lease_id == action.lease_id),
                None,
            )
            if lease is None or lease.exposure_id != action.scope.exposure_id:
                _invalid("pending activation is not authorized by the exact current lease")
            return
        if pending.effect == "rollback-activate":
            if (
                not capability.rollback_held
                or capability.capability_id not in self.rollback_requested_capability_ids
                or action.lease_id != f"rollback:{capability.capability_id}"
            ):
                _invalid("pending rollback activation lacks exact held rollback authority")
            return
        if pending.effect == "prepare":
            if action.lease_id != capability.activation_lease_id or not any(
                item.exposure_id == action.scope.exposure_id for item in capability.leases
            ):
                _invalid("pending exposure preparation lacks a lease for the exact exposure")
            return
        if pending.effect == "deactivate" and action.lease_id != capability.activation_lease_id:
            _invalid("pending deactivation lease does not match activation_lease_id")

    def _validate_persisted_projection(self) -> None:
        capabilities = {item.capability_id: item for item in self.capabilities}
        pending_deactivations = {
            item.action.entity_id for item in self.pending_effects if item.effect == "deactivate"
        }
        for pending in self.pending_effects:
            if pending.action.precondition_revision > self.revision:
                _invalid("persisted pending action exceeds state revision")
        for pending_consent in self.pending_consents:
            if pending_consent.install_action.precondition_revision > self.revision + 1:
                _invalid("persisted pending consent exceeds next revision")
        for capability_id in self.rollback_requested_capability_ids:
            capability = capabilities[capability_id]
            if capability.activation != "inactive" or not capability.rollback_held:
                _invalid("rollback request must target inactive rollback-held capability")
        for capability in self.capabilities:
            if (
                capability.activation == "active"
                and not capability.leases
                and not capability.rollback_held
                and capability.capability_id not in pending_deactivations
                and capability.capability_id not in self.blocked_deactivation_ids
            ):
                _invalid("unleased active capability requires cleanup or rollback state")
        if self._contract_version == 3:
            plan = cast(CommittedPlanV3 | None, self.committed_plan)
            selected_ids = (
                set()
                if plan is None or plan.status != "ready"
                else {item.capability_id for item in plan.capabilities}
            )
            rollback_references = {
                pending.rollback_capability_id
                for pending in self.pending_effects
                if pending.rollback_capability_id is not None
            }
            pending_entity_references = {
                pending.action.entity_id
                for pending in self.pending_effects
                if pending.action.entity_id is not None
            }
            for capability in self.capabilities:
                if capability.capability_id in selected_ids:
                    continue
                if plan is not None and plan.status == "degraded":
                    continue
                if (
                    capability.activation == "inactive"
                    and not capability.rollback_held
                    and capability.capability_id not in self.rollback_requested_capability_ids
                    and capability.capability_id not in rollback_references
                    and capability.capability_id not in pending_entity_references
                ):
                    _invalid("inactive unselected capability cannot survive persisted state")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "blocked_capability_ids": list(self.blocked_capability_ids),
            "blocked_deactivation_ids": list(self.blocked_deactivation_ids),
            "capabilities": (
                [cast(CapabilityStateV3, item).to_dict() for item in self.capabilities]
                if self._contract_version == 3
                else [
                    cast(CapabilityState, item).to_dict(include_installation=False)
                    for item in self.capabilities
                ]
            ),
            "evidence": [item.to_dict() for item in self.evidence],
            "host_descriptor_digest": self.host_descriptor_digest,
            "host_level": self.host_level,
            "last_manual_bundle": list(self.last_manual_bundle),
            "pending_effects": [item.to_dict() for item in self.pending_effects],
            "revision": self.revision,
            "rollback_requested_capability_ids": list(self.rollback_requested_capability_ids),
            "scope": self.scope.to_dict(),
            "session_status": self.session_status,
            "terminal_cleanup_notified_ids": list(self.terminal_cleanup_notified_ids),
        }
        if self._contract_version == 3:
            result.update(
                state_schema=_STATE_SCHEMA_V3,
                blocked_install_descriptor_digests=list(self.blocked_install_descriptor_digests),
                committed_plan=(
                    None if self.committed_plan is None else self.committed_plan.to_dict()
                ),
                install_policy_snapshot_digest=self.install_policy_snapshot_digest,
                pending_consents=[item.to_dict() for item in self.pending_consents],
            )
        return result

    def to_json(self) -> str:
        self._validate_persisted_projection()
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> EngineState:
        if not isinstance(value, Mapping):
            _invalid("engine state must be a JSON object")
        if any(not isinstance(field_name, str) for field_name in value):
            _invalid("engine state field names must be strings")
        fields = frozenset(value)
        if fields == _HISTORICAL_STATE_FIELDS:
            value = _exact_fields(value, _HISTORICAL_STATE_FIELDS, "historical engine state")
            contract_version = 1
        elif fields == _STATE_V3_FIELDS:
            value = _exact_fields(value, _STATE_V3_FIELDS, "schema-v3 engine state")
            if value["state_schema"] != _STATE_SCHEMA_V3:
                _invalid("schema-v3 engine state discriminator is unsupported")
            contract_version = 3
        else:
            allowed = _HISTORICAL_STATE_FIELDS | _STATE_V3_FIELDS
            unknown = sorted(fields - allowed)
            if unknown:
                _invalid(f"engine state has unknown field(s): {', '.join(unknown)}")
            _invalid("engine state does not match a complete supported schema")
        scope_value = _exact_fields(value["scope"], _SCOPE_FIELDS, "engine state scope")
        try:
            scope = ScopeRef.from_dict(scope_value)
        except (TypeError, ValueError) as exc:
            raise StateValidationError("engine state contains an invalid scope") from exc
        state = cls(
            revision=value["revision"],
            scope=scope,
            host_level=value["host_level"],
            host_descriptor_digest=value["host_descriptor_digest"],
            session_status=value["session_status"],
            capabilities=tuple(
                (
                    CapabilityStateV3.from_dict(item)
                    if contract_version == 3
                    else CapabilityState.from_dict(item)
                )
                for item in _array(value["capabilities"], "capabilities")
            ),
            pending_effects=tuple(
                PendingEffect.from_dict(item)
                for item in _array(value["pending_effects"], "pending_effects")
            ),
            pending_consents=tuple(
                PendingConsent.from_dict(item)
                for item in _array(value.get("pending_consents", []), "pending_consents")
            ),
            committed_plan=(
                None
                if value.get("committed_plan") is None
                else CommittedPlanV3.from_dict(value["committed_plan"])
            ),
            install_policy_snapshot_digest=value.get("install_policy_snapshot_digest"),
            evidence=tuple(
                CapabilityEvidence.from_dict(item) for item in _array(value["evidence"], "evidence")
            ),
            last_manual_bundle=tuple(_array(value["last_manual_bundle"], "last_manual_bundle")),
            blocked_capability_ids=tuple(
                _array(value["blocked_capability_ids"], "blocked_capability_ids")
            ),
            blocked_deactivation_ids=tuple(
                _array(value["blocked_deactivation_ids"], "blocked_deactivation_ids")
            ),
            blocked_install_descriptor_digests=tuple(
                _array(
                    value.get("blocked_install_descriptor_digests", []),
                    "blocked_install_descriptor_digests",
                )
            ),
            rollback_requested_capability_ids=tuple(
                _array(
                    value["rollback_requested_capability_ids"],
                    "rollback_requested_capability_ids",
                )
            ),
            terminal_cleanup_notified_ids=tuple(
                _array(
                    value["terminal_cleanup_notified_ids"],
                    "terminal_cleanup_notified_ids",
                )
            ),
            _contract_version=contract_version,
        )
        state._validate_persisted_projection()
        return state

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> EngineState:
        return cls.from_dict(_decode_canonical_json(value))

    @property
    def active_capability_ids(self) -> frozenset[str]:
        return frozenset(
            capability.capability_id
            for capability in self.capabilities
            if capability.activation == "active"
        )

    @property
    def desired_capability_ids(self) -> frozenset[str]:
        return frozenset(
            capability.capability_id for capability in self.capabilities if capability.desired
        )

    def capability(
        self,
        capability_id: str,
    ) -> CapabilityState | CapabilityStateV3 | None:
        return next(
            (
                capability
                for capability in self.capabilities
                if capability.capability_id == capability_id
            ),
            None,
        )

    def evidence_for(
        self,
        exposure_id: str,
        capability_id: str,
    ) -> CapabilityEvidence:
        found = next(
            (
                item
                for item in self.evidence
                if item.exposure_id == exposure_id and item.capability_id == capability_id
            ),
            None,
        )
        if found is None:
            capability = self.capability(capability_id)
            source_digest = "" if capability is None else capability.source_digest
            return CapabilityEvidence(
                exposure_id=exposure_id,
                capability_id=capability_id,
                source_digest=source_digest,
            )
        return found


def _same_stream(left: ScopeRef, right: ScopeRef) -> bool:
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


__all__ = [
    "ACTIONABILITY_STATES",
    "ACTIVATION_STATES",
    "CAPABILITY_KINDS",
    "EXPOSURE_STATES",
    "HOST_LEVELS",
    "INVOCATION_STATES",
    "INSTALLATION_STATES",
    "MAX_ACTIVE_CAPABILITIES",
    "PENDING_EFFECTS",
    "SESSION_STATUSES",
    "CapabilityEvidence",
    "CapabilityState",
    "CapabilityStateV3",
    "CommittedPlan",
    "CommittedPlanV3",
    "EngineState",
    "LeaseRef",
    "PendingEffect",
    "PendingConsent",
    "PlanCapability",
    "PlanCapabilityV3",
    "StateValidationError",
]
