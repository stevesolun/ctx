"""Versioned value objects for the host-neutral CTX engine protocol.

This module deliberately contains no engine policy, reduction, persistence, or
host mutation logic.  It defines the immutable facts that those layers exchange
and an engine-owned canonical JSON representation suitable for hashing and
journaling.

``ctx-canonical-json-v1`` is deliberately narrower than general JSON: object
keys are sorted, insignificant whitespace is removed, numbers must be finite,
duplicate keys and unpaired Unicode surrogates are rejected, and UTF-8 text is
hashed without ASCII escaping.  It is not an implementation of RFC 8785.  Hosts
submit structured protocol values and echo engine-issued digests in receipts;
they are not expected to reproduce these hashes independently.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, TypeAlias

from ctx.engine.capability_schema import CAPABILITY_KINDS, validate_capability_identity
from ctx.engine.content import AuthorizedMaterial, MaterialIdentity
from ctx.engine.lineage import CatalogCapabilityIdentity

if TYPE_CHECKING:
    from ctx.engine.installation import InstallPlanDescriptor


PROTOCOL_VERSION = 1
CANONICALIZATION_SCHEME = "ctx-canonical-json-v1"

# Protocol-v1 envelopes intentionally carry exact schema-discriminated payload
# variants.  These constants name the capability-plan-v3 lifecycle without
# changing the frozen envelope version or any legacy payload bytes.
INSTALL_ACTION_PAYLOAD_SCHEMA_V3 = "ctx.install-capability-payload-v3"
INSTALL_CONSENT_REQUEST_SCHEMA_V3 = "ctx.install-consent-request-v3"
MATERIAL_ACTION_PAYLOAD_SCHEMA_V3 = "ctx.material-action-payload-v3"
INSTALL_RECEIPT_SCHEMA_V3 = "ctx.install-receipt-v3"
MATERIAL_RECEIPT_SCHEMA_V3 = "ctx.material-receipt-v3"
PROMPT_CONTEXT_ACTION_PAYLOAD_SCHEMA_V1 = "ctx.prompt-context-action-payload-v1"
PROMPT_CONTEXT_RECEIPT_SCHEMA_V1 = "ctx.prompt-context-receipt-v1"

EVENT_KINDS = frozenset(
    {
        "SessionStarted",
        "WorkspaceObserved",
        "IntentObserved",
        "DevelopmentObserved",
        "TurnStarting",
        "ProviderSubmissionObserved",
        "ToolCallObserved",
        "ValidationObserved",
        "UserDecision",
        "ActionApplied",
        "ActionFailed",
        "ActionExpired",
        "InstallConsentExpired",
        "ReassessmentRequested",
        "TurnEnded",
        "SessionEnded",
    }
)

ACTION_KINDS = frozenset(
    {
        "PresentBundle",
        "RequestConsent",
        "InstallCapability",
        "ActivateCapability",
        "PrepareExposure",
        "PreparePromptContext",
        "DeactivateCapability",
        "UninstallCapability",
        "Notify",
        "NoChange",
    }
)

_PRIVACY_CLASSIFICATIONS = frozenset(
    {"public", "internal", "private", "confidential", "restricted"}
)
_RETENTION_CLASSES = frozenset(
    {"ephemeral", "session", "workspace", "local", "persistent", "aggregate"}
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | tuple["JsonValue", ...]


class ProtocolValidationError(ValueError):
    """A value cannot be represented safely by this protocol."""


class UnsupportedProtocolVersionError(ProtocolValidationError):
    """A message uses a protocol version this implementation cannot decode."""


def _validate_protocol_version(value: object) -> int:
    if type(value) is not int or value != PROTOCOL_VERSION:
        raise UnsupportedProtocolVersionError(
            f"unsupported protocol version {value!r}; expected {PROTOCOL_VERSION}"
        )
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProtocolValidationError(f"{field_name} must be a non-empty trimmed string")
    _validate_unicode_scalar(value, field_name)
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, field_name)


def _nonnegative_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ProtocolValidationError(f"{field_name} must be a non-negative integer")
    return value


def _validate_unicode_scalar(value: str, field_name: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ProtocolValidationError(f"{field_name} must contain only valid Unicode scalar values")


_RFC3339_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_MACHINE_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")


def _rfc3339(value: object, field_name: str) -> str:
    value = _required_string(value, field_name)
    if _RFC3339_RE.fullmatch(value) is None:
        raise ProtocolValidationError(f"{field_name} must use strict RFC 3339 date-time syntax")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolValidationError(f"{field_name} must be an RFC 3339 timestamp") from exc
    normalized = parsed.astimezone(timezone.utc)
    result = normalized.strftime("%Y-%m-%dT%H:%M:%S")
    if normalized.microsecond:
        result += f".{normalized.microsecond:06d}".rstrip("0")
    return result + "Z"


def _optional_rfc3339(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _rfc3339(value, field_name)


def _freeze_json(value: object, field_name: str = "payload") -> JsonValue:
    """Validate and defensively freeze a JSON-compatible value."""

    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        _validate_unicode_scalar(value, field_name)
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolValidationError(f"{field_name} numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProtocolValidationError(f"{field_name} object keys must be strings")
            _validate_unicode_scalar(key, f"{field_name} object key")
            frozen[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, f"{field_name}[{index}]") for index, item in enumerate(value)
        )
    raise ProtocolValidationError(
        f"{field_name} must contain only JSON-compatible values, got {type(value).__name__}"
    )


def _thaw_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError("message is not canonical-JSON compatible") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, item in pairs:
        if key in decoded:
            raise ProtocolValidationError(f"duplicate JSON key {key!r}")
        decoded[key] = item
    return decoded


def _decode_json(value: str | bytes | bytearray) -> Mapping[str, Any]:
    try:
        decoded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except ProtocolValidationError:
        raise
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ProtocolValidationError("message is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolValidationError("protocol message must be a JSON object")
    return decoded


def _digest_text(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:  # defensive: constructors reject these first
        raise ProtocolValidationError("digest input contains an invalid Unicode scalar") from exc
    return hashlib.sha256(encoded).hexdigest()


def _reject_unknown_fields(
    value: object,
    allowed: frozenset[str],
    object_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{object_name} must be a JSON object")
    if any(not isinstance(field_name, str) for field_name in value):
        raise ProtocolValidationError(f"{object_name} field names must be strings")
    unknown = sorted(set(value) - allowed)
    if unknown:
        rendered = ", ".join(repr(field_name) for field_name in unknown)
        raise ProtocolValidationError(f"{object_name} has unknown field(s): {rendered}")
    return value


def _require_nonempty_mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise ProtocolValidationError(f"{field_name} must be a non-empty JSON object")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    value = _required_string(value, field_name)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ProtocolValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


_SCOPE_FIELDS = frozenset(
    {
        "tenant_id",
        "workspace_id",
        "repository_id",
        "session_id",
        "exposure_id",
        "host_context_id",
        "parent_exposure_id",
    }
)
_PRIVACY_FIELDS = frozenset({"classification", "retention"})
_EVENT_FIELDS = frozenset(
    {
        "catalog_snapshot_digest",
        "causation_id",
        "correlation_id",
        "engine_version",
        "event_id",
        "expected_revision",
        "host_descriptor_digest",
        "kind",
        "occurred_at",
        "payload",
        "planner_version",
        "policy_version",
        "privacy",
        "protocol_version",
        "random_seed",
        "scope",
        "semantic_index_digest",
        "semantic_model_digest",
        "work_signature",
    }
)
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
_TRANSITION_FIELDS = frozenset(
    {
        "actions",
        "diagnostics",
        "event_id",
        "from_revision",
        "protocol_version",
        "scope",
        "to_revision",
    }
)

_RECEIPT_EVENT_KINDS = frozenset({"ActionApplied", "ActionFailed", "ActionExpired"})
_DECISION_CAUSING_EVENT_KINDS = frozenset(
    {
        "WorkspaceObserved",
        "IntentObserved",
        "DevelopmentObserved",
        "TurnStarting",
        "ValidationObserved",
        "ReassessmentRequested",
    }
)
_DECISION_REPLAY_FIELDS = (
    "engine_version",
    "planner_version",
    "policy_version",
    "host_descriptor_digest",
    "catalog_snapshot_digest",
    "semantic_model_digest",
    "semantic_index_digest",
    "work_signature",
    "random_seed",
)
_RECEIPT_IDENTITY_FIELDS = frozenset(
    {
        "action_id",
        "action_kind",
        "action_content_digest",
        "action_precondition_revision",
    }
)
_REQUESTED_ACTION_IDENTITY_FIELDS = frozenset(
    {
        "requested_action_id",
        "requested_action_kind",
        "requested_action_content_digest",
        "requested_action_precondition_revision",
    }
)
_INSTALL_CONSENT_EXPIRY_FIELDS = _REQUESTED_ACTION_IDENTITY_FIELDS | frozenset(
    {
        "consent_id",
        "install_expires_at",
        "policy_snapshot_digest",
    }
)
_INSTALL_DESCRIPTOR_FIELDS = frozenset(
    {
        "install_plan_digest",
        "install_descriptor_digest",
        "installer_id",
        "installer_digest",
        "policy_snapshot_digest",
    }
)
_INSTALL_ACTION_V3_FIELDS = frozenset(
    {
        "schema",
        "capability_kind",
        "catalog_identity",
        "result_material",
        "install_plan_descriptor",
        "installer_digest",
        "policy_snapshot_digest",
    }
)
_INSTALL_CONSENT_REQUEST_V3_FIELDS = _INSTALL_ACTION_V3_FIELDS | (_REQUESTED_ACTION_IDENTITY_FIELDS)
_MATERIAL_ACTION_V3_FIELDS = frozenset(
    {
        "schema",
        "capability_kind",
        "catalog_identity",
        "material_identity",
        "authorized_material",
    }
)
_V3_ACTION_VERIFICATION_FIELDS = frozenset({"receipt_required", "expected_state", "receipt_schema"})
_INSTALL_RECEIPT_V3_FIELDS = frozenset(
    {
        "schema",
        "host_state",
        "capability_id",
        "capability_kind",
        "catalog_identity",
        "material_identity",
        "install_plan_descriptor",
        "installer_digest",
        "policy_snapshot_digest",
    }
)
_MATERIAL_RECEIPT_V3_FIELDS = frozenset(
    {
        "schema",
        "host_state",
        "capability_id",
        "capability_kind",
        "catalog_identity",
        "material_identity",
        "authorized_material",
    }
)
_PROMPT_CONTEXT_ACTION_FIELDS = frozenset(
    {
        "capabilities",
        "execution_intent",
        "plan_digest",
        "presentation_action_content_digest",
        "presentation_action_id",
        "schema",
    }
)
_PROMPT_CONTEXT_CAPABILITY_FIELDS = frozenset(
    {
        "authorized_material",
        "capability_id",
        "capability_kind",
        "catalog_identity",
        "material_identity",
        "source_digest",
    }
)
_PROMPT_CONTEXT_RECEIPT_FIELDS = frozenset(
    {
        "capabilities",
        "host_state",
        "prompt_context_bytes",
        "prompt_context_sha256",
        "schema",
    }
)
_PROMPT_CONTEXT_RECEIPT_CAPABILITY_FIELDS = frozenset(
    {"capability_id", "content_bytes", "content_sha256"}
)
_DECISION_PROVENANCE_FIELDS = frozenset({"decision_basis", "policy_snapshot_digest"})

_MATERIAL_ACTION_EXPECTED_STATES = {
    "ActivateCapability": "active",
    "PrepareExposure": "prepared",
    "DeactivateCapability": "inactive",
}


def _validate_capability_binding(
    capability_id: object,
    kind: object,
    *,
    object_name: str,
) -> None:
    try:
        validate_capability_identity(capability_id, kind)
    except ValueError as exc:
        raise ProtocolValidationError(
            f"{object_name} capability_kind must exactly match capability_id or entity_id"
        ) from exc


def _validate_v3_action_target(
    action: Any,
    capability_id: str,
    kind: str,
    object_name: str,
) -> None:
    _validate_capability_binding(action.entity_id, kind, object_name=object_name)
    if action.entity_id != capability_id:
        raise ProtocolValidationError(
            f"{object_name} entity_id does not match the exact typed capability identity"
        )
    _require_sha256(action.source_digest, f"{object_name} source_digest")
    _require_sha256(action.catalog_snapshot_id, f"{object_name} catalog_snapshot_id")


def _typed_catalog_identity(value: object, object_name: str) -> CatalogCapabilityIdentity:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{object_name} must be a JSON object")
    try:
        return CatalogCapabilityIdentity.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"{object_name} is invalid: {exc}") from exc


def _typed_material_identity(value: object, object_name: str) -> MaterialIdentity:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{object_name} must be a JSON object")
    try:
        return MaterialIdentity.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"{object_name} is invalid: {exc}") from exc


def _typed_authorized_material(value: object, object_name: str) -> AuthorizedMaterial:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{object_name} must be a JSON object")
    try:
        return AuthorizedMaterial.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"{object_name} is invalid: {exc}") from exc


def _typed_install_plan_descriptor_v2(
    value: object,
    *,
    result_material: MaterialIdentity,
    object_name: str,
) -> InstallPlanDescriptor:
    # Imported lazily because installation consumes HostAction at runtime.  The
    # class is fully initialized before any action can be constructed, while
    # protocol import remains acyclic.
    from ctx.engine.installation import InstallPlanDescriptor

    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"{object_name} must be a JSON object")
    try:
        descriptor = InstallPlanDescriptor.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolValidationError(f"{object_name} is invalid: {exc}") from exc
    if descriptor.schema_version != 2:
        raise ProtocolValidationError(f"{object_name} must use the exact v2 descriptor schema")
    if not descriptor.matches_result_material(result_material):
        raise ProtocolValidationError(
            f"{object_name} does not match the exact result_material identity"
        )
    return descriptor


def _validate_v3_install_binding(
    payload: Mapping[str, Any],
    object_name: str,
) -> tuple[str, str, str]:
    kind = _required_string(payload.get("capability_kind"), "capability_kind")
    if kind not in CAPABILITY_KINDS:
        raise ProtocolValidationError("capability_kind is not a declared capability kind")
    catalog_identity = _typed_catalog_identity(
        payload.get("catalog_identity"),
        f"{object_name} catalog_identity",
    )
    result_material = _typed_material_identity(
        payload.get("result_material"),
        f"{object_name} result_material",
    )
    descriptor = _typed_install_plan_descriptor_v2(
        payload.get("install_plan_descriptor"),
        result_material=result_material,
        object_name=f"{object_name} install_plan_descriptor",
    )
    if (
        catalog_identity.capability_id,
        catalog_identity.kind,
        result_material.capability_id,
        result_material.kind,
        descriptor.capability_id,
        descriptor.kind,
    ) != (
        catalog_identity.capability_id,
        kind,
        catalog_identity.capability_id,
        kind,
        catalog_identity.capability_id,
        kind,
    ):
        raise ProtocolValidationError(
            f"{object_name} typed identities must match capability_kind and one capability"
        )
    for field_name in ("installer_digest", "policy_snapshot_digest"):
        _require_sha256(payload.get(field_name), field_name)
    return catalog_identity.capability_id, kind, descriptor.installer_id


def _validate_v3_material_binding(
    payload: Mapping[str, Any],
    object_name: str,
) -> tuple[str, str]:
    kind = _required_string(payload.get("capability_kind"), "capability_kind")
    if kind not in CAPABILITY_KINDS:
        raise ProtocolValidationError("capability_kind is not a declared capability kind")
    catalog_identity = _typed_catalog_identity(
        payload.get("catalog_identity"),
        f"{object_name} catalog_identity",
    )
    material_identity = _typed_material_identity(
        payload.get("material_identity"),
        f"{object_name} material_identity",
    )
    authorized = _typed_authorized_material(
        payload.get("authorized_material"),
        f"{object_name} authorized_material",
    )
    if (
        catalog_identity.capability_id,
        catalog_identity.kind,
        catalog_identity.identity_digest,
        material_identity.capability_id,
        material_identity.kind,
        material_identity.identity_digest,
    ) != (
        authorized.capability_id,
        kind,
        authorized.catalog_identity_digest,
        authorized.capability_id,
        kind,
        authorized.material_identity_digest,
    ):
        raise ProtocolValidationError(
            f"{object_name} typed identities do not match exact authorized_material"
        )
    return catalog_identity.capability_id, kind


def _validate_v3_rollback(
    action: Any,
    *,
    installer_id: str | None = None,
) -> None:
    if action.kind == "InstallCapability":
        fields = frozenset({"kind", "installer_id"})
        rollback = _reject_unknown_fields(action.rollback, fields, "v3 install rollback")
        if set(rollback) != fields:
            missing = sorted(fields - set(rollback))[0]
            raise ProtocolValidationError(f"v3 install rollback is missing {missing}")
        if rollback["kind"] != "UninstallCapability":
            raise ProtocolValidationError("v3 install rollback kind must be UninstallCapability")
        if rollback["installer_id"] != installer_id:
            raise ProtocolValidationError(
                "v3 install rollback installer_id must match install_plan_descriptor"
            )
        return
    if action.kind == "PrepareExposure":
        fields = frozenset({"kind", "exposure_id"})
        rollback = _reject_unknown_fields(action.rollback, fields, "v3 material rollback")
        if set(rollback) != fields:
            missing = sorted(fields - set(rollback))[0]
            raise ProtocolValidationError(f"v3 material rollback is missing {missing}")
        if (
            rollback["kind"] != "cleanup-prepared-exposure"
            or rollback["exposure_id"] != action.scope.exposure_id
        ):
            raise ProtocolValidationError(
                "v3 PrepareExposure rollback must clean the exact exposure"
            )
        return
    if action.kind == "DeactivateCapability":
        fields = frozenset({"kind", "source_digest"})
        rollback = _reject_unknown_fields(action.rollback, fields, "v3 material rollback")
        if set(rollback) != fields:
            missing = sorted(fields - set(rollback))[0]
            raise ProtocolValidationError(f"v3 material rollback is missing {missing}")
        if (
            rollback["kind"] != "ActivateCapability"
            or rollback["source_digest"] != action.source_digest
        ):
            raise ProtocolValidationError(
                "v3 DeactivateCapability rollback must restore the exact source"
            )
        return
    fields = frozenset({"kind"})
    rollback = _reject_unknown_fields(action.rollback, fields, "v3 material rollback")
    if set(rollback) != fields or rollback["kind"] != "DeactivateCapability":
        raise ProtocolValidationError(
            "v3 ActivateCapability rollback must be exact DeactivateCapability"
        )


def _validate_v3_action_verification(
    value: Mapping[str, Any],
    *,
    expected_state: str,
    receipt_schema: str,
) -> None:
    verification = _reject_unknown_fields(
        value,
        _V3_ACTION_VERIFICATION_FIELDS,
        "v3 action verification",
    )
    for field_name in _V3_ACTION_VERIFICATION_FIELDS:
        if field_name not in verification:
            raise ProtocolValidationError(f"v3 action verification is missing {field_name}")
    if verification["receipt_required"] is not True:
        raise ProtocolValidationError("v3 action receipt_required must be true")
    if verification["expected_state"] != expected_state:
        raise ProtocolValidationError(f"v3 action expected_state must be {expected_state}")
    if verification["receipt_schema"] != receipt_schema:
        raise ProtocolValidationError(f"v3 action receipt_schema must be {receipt_schema}")


def _validate_prompt_context_capabilities(
    value: object,
    *,
    object_name: str,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 5:
        raise ProtocolValidationError(f"{object_name} must contain between one and five rows")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        name = f"{object_name}[{index}]"
        row = _reject_unknown_fields(raw, _PROMPT_CONTEXT_CAPABILITY_FIELDS, name)
        if set(row) != _PROMPT_CONTEXT_CAPABILITY_FIELDS:
            missing = sorted(_PROMPT_CONTEXT_CAPABILITY_FIELDS - set(row))[0]
            raise ProtocolValidationError(f"{name} is missing {missing}")
        capability_id, capability_kind = _validate_v3_material_binding(row, name)
        if row["capability_id"] != capability_id or row["capability_kind"] != capability_kind:
            raise ProtocolValidationError(f"{name} capability identity is inconsistent")
        _require_sha256(row["source_digest"], f"{name} source_digest")
        if capability_id in seen:
            raise ProtocolValidationError(f"{object_name} contains a duplicate capability")
        seen.add(capability_id)
        rows.append({key: _thaw_json(row[key]) for key in sorted(row)})
    return tuple(rows)


def _prompt_context_bundle_digest(
    *,
    execution_intent: str,
    plan_digest: str,
    presentation_action_id: str,
    presentation_action_content_digest: str,
    capabilities: tuple[dict[str, Any], ...],
) -> str:
    return _digest_text(
        _canonical_json(
            {
                "capabilities": list(capabilities),
                "execution_intent": execution_intent,
                "plan_digest": plan_digest,
                "presentation_action_content_digest": presentation_action_content_digest,
                "presentation_action_id": presentation_action_id,
                "schema": "ctx.prompt-context-bundle-v1",
            }
        )
    )


def _validate_prompt_context_action(action: Any) -> None:
    payload = _reject_unknown_fields(
        action.payload,
        _PROMPT_CONTEXT_ACTION_FIELDS,
        "PreparePromptContext payload",
    )
    if set(payload) != _PROMPT_CONTEXT_ACTION_FIELDS:
        missing = sorted(_PROMPT_CONTEXT_ACTION_FIELDS - set(payload))[0]
        raise ProtocolValidationError(f"PreparePromptContext payload is missing {missing}")
    if payload["schema"] != PROMPT_CONTEXT_ACTION_PAYLOAD_SCHEMA_V1:
        raise ProtocolValidationError("PreparePromptContext payload schema is unsupported")
    intent = _required_string(payload["execution_intent"], "execution_intent")
    if intent not in {"activate", "experiment"}:
        raise ProtocolValidationError("PreparePromptContext execution_intent is unsupported")
    plan_digest = _require_sha256(payload["plan_digest"], "plan_digest")
    presentation_action_id = _required_string(
        payload["presentation_action_id"],
        "presentation_action_id",
    )
    presentation_action_content_digest = _require_sha256(
        payload["presentation_action_content_digest"],
        "presentation_action_content_digest",
    )
    capabilities = _validate_prompt_context_capabilities(
        payload["capabilities"],
        object_name="PreparePromptContext capabilities",
    )
    if action.entity_id is not None or action.plan_id is None:
        raise ProtocolValidationError("PreparePromptContext must target its exact bundle plan")
    if action.catalog_snapshot_id is None:
        raise ProtocolValidationError("PreparePromptContext requires catalog_snapshot_id")
    expected_source_digest = _prompt_context_bundle_digest(
        execution_intent=intent,
        plan_digest=plan_digest,
        presentation_action_id=presentation_action_id,
        presentation_action_content_digest=presentation_action_content_digest,
        capabilities=capabilities,
    )
    if action.source_digest != expected_source_digest:
        raise ProtocolValidationError("PreparePromptContext source_digest is invalid")
    if (
        action.lease_id is None
        or action.expires_at is None
        or action.required_host_feature != "prompt-context"
    ):
        raise ProtocolValidationError("PreparePromptContext lacks its bounded host authority")
    _validate_v3_action_verification(
        action.verification,
        expected_state="prompt-context-prepared",
        receipt_schema=PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
    )
    rollback = _reject_unknown_fields(
        action.rollback,
        frozenset({"exposure_id", "kind"}),
        "PreparePromptContext rollback",
    )
    if set(rollback) != {"exposure_id", "kind"} or (
        rollback["kind"] != "discard-prompt-context"
        or rollback["exposure_id"] != action.scope.exposure_id
    ):
        raise ProtocolValidationError("PreparePromptContext rollback is invalid")


def _validate_requested_action_identity(payload: Mapping[str, Any], object_name: str) -> None:
    for field_name in _REQUESTED_ACTION_IDENTITY_FIELDS:
        if field_name not in payload:
            raise ProtocolValidationError(f"{object_name} is missing {field_name}")
    _required_string(payload["requested_action_id"], "requested_action_id")
    requested_kind = _required_string(
        payload["requested_action_kind"],
        "requested_action_kind",
    )
    if requested_kind not in _PERSISTENT_ACTION_KINDS:
        raise ProtocolValidationError(
            "requested_action_kind must be InstallCapability or UninstallCapability"
        )
    _require_sha256(
        payload["requested_action_content_digest"],
        "requested_action_content_digest",
    )
    _nonnegative_integer(
        payload["requested_action_precondition_revision"],
        "requested_action_precondition_revision",
    )


def _validate_install_descriptor(payload: Mapping[str, Any], object_name: str) -> None:
    for field_name in _INSTALL_DESCRIPTOR_FIELDS:
        if field_name not in payload:
            raise ProtocolValidationError(f"{object_name} is missing {field_name}")
    _require_sha256(payload["install_plan_digest"], "install_plan_digest")
    _require_sha256(payload["install_descriptor_digest"], "install_descriptor_digest")
    installer_id = _required_string(payload["installer_id"], "installer_id")
    if _MACHINE_TOKEN_RE.fullmatch(installer_id) is None:
        raise ProtocolValidationError("installer_id must be a machine token")
    _require_sha256(payload["installer_digest"], "installer_digest")
    _require_sha256(payload["policy_snapshot_digest"], "policy_snapshot_digest")


def _validate_user_decision_payload(
    payload: Mapping[str, Any],
    expected_revision: int,
) -> None:
    allowed = (
        _REQUESTED_ACTION_IDENTITY_FIELDS | _DECISION_PROVENANCE_FIELDS | {"consent_id", "decision"}
    )
    _reject_unknown_fields(payload, allowed, "UserDecision payload")
    for field_name in ("consent_id", "decision"):
        if field_name not in payload:
            raise ProtocolValidationError(f"UserDecision payload is missing {field_name}")
    _required_string(payload["consent_id"], "consent_id")
    decision = _required_string(payload["decision"], "decision")
    if decision not in {"granted", "denied"}:
        raise ProtocolValidationError("decision must be granted or denied")
    _validate_requested_action_identity(payload, "UserDecision payload")
    present_provenance = set(payload) & _DECISION_PROVENANCE_FIELDS
    if present_provenance and present_provenance != _DECISION_PROVENANCE_FIELDS:
        missing = next(iter(_DECISION_PROVENANCE_FIELDS - present_provenance))
        raise ProtocolValidationError(f"UserDecision payload is missing {missing}")
    if present_provenance:
        if payload["decision_basis"] not in {"interactive", "preapproved-policy"}:
            raise ProtocolValidationError(
                "decision_basis must be interactive or preapproved-policy"
            )
        _require_sha256(payload["policy_snapshot_digest"], "policy_snapshot_digest")
    if payload["requested_action_precondition_revision"] != expected_revision + 1:
        raise ProtocolValidationError(
            "requested_action_precondition_revision must target the next committed revision"
        )


def _validate_install_consent_expired_payload(payload: Mapping[str, Any]) -> None:
    """Validate a machine expiry fact without granting it decision semantics."""

    _reject_unknown_fields(
        payload,
        _INSTALL_CONSENT_EXPIRY_FIELDS,
        "InstallConsentExpired payload",
    )
    missing = sorted(_INSTALL_CONSENT_EXPIRY_FIELDS - set(payload))
    if missing:
        raise ProtocolValidationError(f"InstallConsentExpired payload is missing {missing[0]}")
    _required_string(payload["consent_id"], "consent_id")
    _validate_requested_action_identity(payload, "InstallConsentExpired payload")
    if payload["requested_action_kind"] != "InstallCapability":
        raise ProtocolValidationError(
            "InstallConsentExpired requested_action_kind must be InstallCapability"
        )
    _require_sha256(payload["policy_snapshot_digest"], "policy_snapshot_digest")
    expires_at = _rfc3339(payload["install_expires_at"], "install_expires_at")
    if expires_at != payload["install_expires_at"]:
        raise ProtocolValidationError("install_expires_at must use canonical UTC RFC 3339")


def _validate_receipt_payload(kind: str, payload: Mapping[str, Any]) -> None:
    if kind not in _RECEIPT_EVENT_KINDS:
        return
    kind_fields = {
        "ActionApplied": frozenset({"verification"}),
        "ActionFailed": frozenset({"error"}),
        "ActionExpired": frozenset({"reason"}),
    }[kind]
    _reject_unknown_fields(payload, _RECEIPT_IDENTITY_FIELDS | kind_fields, f"{kind} payload")
    for field_name in _RECEIPT_IDENTITY_FIELDS:
        if field_name not in payload:
            raise ProtocolValidationError(f"{kind} payload is missing {field_name}")
    _required_string(payload["action_id"], "action_id")
    action_kind = _required_string(payload["action_kind"], "action_kind")
    if action_kind not in ACTION_KINDS:
        raise ProtocolValidationError(
            f"action_kind must be a declared action kind, got {action_kind!r}"
        )
    _require_sha256(payload["action_content_digest"], "action_content_digest")
    _nonnegative_integer(
        payload["action_precondition_revision"],
        "action_precondition_revision",
    )
    if kind == "ActionApplied":
        raw_verification = payload.get("verification")
        if not isinstance(raw_verification, Mapping):
            raise ProtocolValidationError("ActionApplied verification must be a JSON object")
        verification_schema = raw_verification.get("schema")
        if verification_schema is None:
            verification = _reject_unknown_fields(
                raw_verification,
                frozenset({"host_state"}),
                "ActionApplied verification",
            )
            if set(verification) != {"host_state"}:
                raise ProtocolValidationError("ActionApplied verification is missing host_state")
            if verification["host_state"] not in {
                "active",
                "inactive",
                "prepared",
                "installed",
            }:
                raise ProtocolValidationError("ActionApplied host_state is unsupported")
        elif verification_schema == INSTALL_RECEIPT_SCHEMA_V3:
            if action_kind != "InstallCapability":
                raise ProtocolValidationError("install receipt schema does not match action_kind")
            verification = _reject_unknown_fields(
                raw_verification,
                _INSTALL_RECEIPT_V3_FIELDS,
                "v3 install receipt verification",
            )
            for field_name in _INSTALL_RECEIPT_V3_FIELDS:
                if field_name not in verification:
                    raise ProtocolValidationError(
                        f"v3 install receipt verification is missing {field_name}"
                    )
            if verification["host_state"] != "installed":
                raise ProtocolValidationError("v3 install receipt host_state must be installed")
            _validate_capability_binding(
                verification["capability_id"],
                verification["capability_kind"],
                object_name="v3 install receipt",
            )
            catalog_identity = _typed_catalog_identity(
                verification["catalog_identity"],
                "v3 install receipt catalog_identity",
            )
            material_identity = _typed_material_identity(
                verification["material_identity"],
                "v3 install receipt material_identity",
            )
            descriptor = _typed_install_plan_descriptor_v2(
                verification["install_plan_descriptor"],
                result_material=material_identity,
                object_name="v3 install receipt install_plan_descriptor",
            )
            for field_name in ("installer_digest", "policy_snapshot_digest"):
                _require_sha256(verification[field_name], field_name)
            if (
                verification["capability_id"],
                verification["capability_kind"],
                catalog_identity.capability_id,
                catalog_identity.kind,
                material_identity.capability_id,
                material_identity.kind,
                descriptor.capability_id,
                descriptor.kind,
            ) != (
                catalog_identity.capability_id,
                catalog_identity.kind,
                catalog_identity.capability_id,
                catalog_identity.kind,
                catalog_identity.capability_id,
                catalog_identity.kind,
                catalog_identity.capability_id,
                catalog_identity.kind,
            ):
                raise ProtocolValidationError(
                    "v3 install receipt typed identities do not match one capability"
                )
        elif verification_schema == MATERIAL_RECEIPT_SCHEMA_V3:
            expected_state = _MATERIAL_ACTION_EXPECTED_STATES.get(action_kind)
            if expected_state is None:
                raise ProtocolValidationError("material receipt schema does not match action_kind")
            verification = _reject_unknown_fields(
                raw_verification,
                _MATERIAL_RECEIPT_V3_FIELDS,
                "v3 material receipt verification",
            )
            for field_name in _MATERIAL_RECEIPT_V3_FIELDS:
                if field_name not in verification:
                    raise ProtocolValidationError(
                        f"v3 material receipt verification is missing {field_name}"
                    )
            if verification["host_state"] != expected_state:
                raise ProtocolValidationError(
                    f"v3 material receipt host_state must be {expected_state}"
                )
            _validate_capability_binding(
                verification["capability_id"],
                verification["capability_kind"],
                object_name="v3 material receipt",
            )
            capability_id, capability_kind = _validate_v3_material_binding(
                verification,
                "v3 material receipt verification",
            )
            if (
                verification["capability_id"],
                verification["capability_kind"],
            ) != (capability_id, capability_kind):
                raise ProtocolValidationError(
                    "v3 material receipt capability_id does not match typed identities"
                )
        elif verification_schema == PROMPT_CONTEXT_RECEIPT_SCHEMA_V1:
            if action_kind != "PreparePromptContext":
                raise ProtocolValidationError(
                    "prompt context receipt schema does not match action_kind"
                )
            verification = _reject_unknown_fields(
                raw_verification,
                _PROMPT_CONTEXT_RECEIPT_FIELDS,
                "prompt context receipt verification",
            )
            if set(verification) != _PROMPT_CONTEXT_RECEIPT_FIELDS:
                missing = sorted(_PROMPT_CONTEXT_RECEIPT_FIELDS - set(verification))[0]
                raise ProtocolValidationError(
                    f"prompt context receipt verification is missing {missing}"
                )
            if verification["host_state"] != "prompt-context-prepared":
                raise ProtocolValidationError(
                    "prompt context receipt host_state must be prompt-context-prepared"
                )
            _require_sha256(
                verification["prompt_context_sha256"],
                "prompt_context_sha256",
            )
            context_bytes = _nonnegative_integer(
                verification["prompt_context_bytes"],
                "prompt_context_bytes",
            )
            if context_bytes == 0 or context_bytes > 32_768:
                raise ProtocolValidationError("prompt_context_bytes is outside its bounded range")
            rows = verification["capabilities"]
            if not isinstance(rows, (list, tuple)) or not 1 <= len(rows) <= 5:
                raise ProtocolValidationError(
                    "prompt context receipt capabilities must contain one to five rows"
                )
            seen: set[str] = set()
            for index, raw in enumerate(rows):
                name = f"prompt context receipt capabilities[{index}]"
                row = _reject_unknown_fields(
                    raw,
                    _PROMPT_CONTEXT_RECEIPT_CAPABILITY_FIELDS,
                    name,
                )
                if set(row) != _PROMPT_CONTEXT_RECEIPT_CAPABILITY_FIELDS:
                    missing = sorted(_PROMPT_CONTEXT_RECEIPT_CAPABILITY_FIELDS - set(row))[0]
                    raise ProtocolValidationError(f"{name} is missing {missing}")
                capability_id = _required_string(row["capability_id"], "capability_id")
                if capability_id in seen:
                    raise ProtocolValidationError(
                        "prompt context receipt contains a duplicate capability"
                    )
                seen.add(capability_id)
                _require_sha256(row["content_sha256"], "content_sha256")
                content_bytes = _nonnegative_integer(row["content_bytes"], "content_bytes")
                if content_bytes == 0 or content_bytes > 6_000:
                    raise ProtocolValidationError("content_bytes is outside its bounded range")
        else:
            raise ProtocolValidationError("ActionApplied verification schema is unsupported")
    elif kind == "ActionFailed":
        error = _reject_unknown_fields(
            payload.get("error"),
            frozenset({"code"}),
            "ActionFailed error",
        )
        if set(error) != {"code"}:
            raise ProtocolValidationError("ActionFailed error is missing code")
        code = _required_string(error["code"], "ActionFailed error code")
        if _MACHINE_TOKEN_RE.fullmatch(code) is None:
            raise ProtocolValidationError("ActionFailed error code must be a machine token")
    else:
        if payload.get("reason") != "expired":
            raise ProtocolValidationError("ActionExpired reason must be expired")


_PHYSICAL_ACTION_KINDS = frozenset(
    {
        "InstallCapability",
        "ActivateCapability",
        "PrepareExposure",
        "PreparePromptContext",
        "DeactivateCapability",
        "UninstallCapability",
    }
)
_LEASE_BOUNDED_ACTION_KINDS = frozenset(
    {"ActivateCapability", "PrepareExposure", "DeactivateCapability"}
)
_PERSISTENT_ACTION_KINDS = frozenset({"InstallCapability", "UninstallCapability"})
_ROLLBACK_REQUIRED_ACTION_KINDS = _PHYSICAL_ACTION_KINDS


def _validate_action_contract(action: Any) -> None:
    """Apply kind-specific safety requirements after generic field freezing."""

    if action.kind == "PreparePromptContext":
        _validate_prompt_context_action(action)
        return
    if action.kind in _PHYSICAL_ACTION_KINDS:
        for field_name in (
            "entity_id",
            "source_digest",
            "plan_id",
            "catalog_snapshot_id",
            "required_host_feature",
        ):
            if getattr(action, field_name) is None:
                raise ProtocolValidationError(
                    f"{field_name} is required for physical action {action.kind}"
                )
        _require_nonempty_mapping(action.verification, "verification")
        if action.kind in _LEASE_BOUNDED_ACTION_KINDS:
            if action.lease_id is None:
                raise ProtocolValidationError(f"lease_id is required for {action.kind}")
            if action.expires_at is None:
                raise ProtocolValidationError(f"expires_at is required for {action.kind}")
        if action.kind in _PERSISTENT_ACTION_KINDS and action.consent_id is None:
            raise ProtocolValidationError(f"consent_id is required for {action.kind}")
        if action.kind in _ROLLBACK_REQUIRED_ACTION_KINDS:
            _require_nonempty_mapping(action.rollback, "rollback")
        if action.kind == "InstallCapability":
            payload_schema = action.payload.get("schema")
            if payload_schema is None:
                _reject_unknown_fields(
                    action.payload,
                    _INSTALL_DESCRIPTOR_FIELDS,
                    "InstallCapability payload",
                )
                _validate_install_descriptor(action.payload, "InstallCapability payload")
            elif payload_schema == INSTALL_ACTION_PAYLOAD_SCHEMA_V3:
                payload = _reject_unknown_fields(
                    action.payload,
                    _INSTALL_ACTION_V3_FIELDS,
                    "v3 InstallCapability payload",
                )
                if set(payload) != _INSTALL_ACTION_V3_FIELDS:
                    missing = sorted(_INSTALL_ACTION_V3_FIELDS - set(payload))[0]
                    raise ProtocolValidationError(
                        f"v3 InstallCapability payload is missing {missing}"
                    )
                capability_id, capability_kind, installer_id = _validate_v3_install_binding(
                    payload,
                    "v3 InstallCapability payload",
                )
                _validate_v3_action_target(
                    action,
                    capability_id,
                    capability_kind,
                    "v3 InstallCapability payload",
                )
                _validate_v3_action_verification(
                    action.verification,
                    expected_state="installed",
                    receipt_schema=INSTALL_RECEIPT_SCHEMA_V3,
                )
                _validate_v3_rollback(action, installer_id=installer_id)
            else:
                raise ProtocolValidationError("InstallCapability payload schema is unsupported")
        elif action.kind in _MATERIAL_ACTION_EXPECTED_STATES:
            payload_schema = action.payload.get("schema")
            if payload_schema is not None:
                if payload_schema != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3:
                    raise ProtocolValidationError(f"{action.kind} payload schema is unsupported")
                payload = _reject_unknown_fields(
                    action.payload,
                    _MATERIAL_ACTION_V3_FIELDS,
                    f"v3 {action.kind} payload",
                )
                if set(payload) != _MATERIAL_ACTION_V3_FIELDS:
                    missing = sorted(_MATERIAL_ACTION_V3_FIELDS - set(payload))[0]
                    raise ProtocolValidationError(f"v3 {action.kind} payload is missing {missing}")
                capability_id, capability_kind = _validate_v3_material_binding(
                    payload,
                    f"v3 {action.kind} payload",
                )
                _validate_v3_action_target(
                    action,
                    capability_id,
                    capability_kind,
                    f"v3 {action.kind} payload",
                )
                _validate_v3_action_verification(
                    action.verification,
                    expected_state=_MATERIAL_ACTION_EXPECTED_STATES[action.kind],
                    receipt_schema=MATERIAL_RECEIPT_SCHEMA_V3,
                )
                _validate_v3_rollback(action)
        return

    if action.kind == "RequestConsent":
        for field_name in (
            "consent_id",
            "entity_id",
            "source_digest",
            "plan_id",
            "catalog_snapshot_id",
            "required_host_feature",
        ):
            if getattr(action, field_name) is None:
                raise ProtocolValidationError(f"{field_name} is required for RequestConsent")
        payload_schema = action.payload.get("schema")
        if payload_schema is None:
            allowed = _REQUESTED_ACTION_IDENTITY_FIELDS | _INSTALL_DESCRIPTOR_FIELDS
            _reject_unknown_fields(action.payload, allowed, "RequestConsent payload")
            _validate_requested_action_identity(action.payload, "RequestConsent payload")
            present_descriptor = set(action.payload) & _INSTALL_DESCRIPTOR_FIELDS
            if present_descriptor and present_descriptor != _INSTALL_DESCRIPTOR_FIELDS:
                missing = next(iter(_INSTALL_DESCRIPTOR_FIELDS - present_descriptor))
                raise ProtocolValidationError(f"RequestConsent payload is missing {missing}")
            if present_descriptor:
                _validate_install_descriptor(action.payload, "RequestConsent payload")
        elif payload_schema == INSTALL_CONSENT_REQUEST_SCHEMA_V3:
            payload = _reject_unknown_fields(
                action.payload,
                _INSTALL_CONSENT_REQUEST_V3_FIELDS,
                "v3 RequestConsent payload",
            )
            if set(payload) != _INSTALL_CONSENT_REQUEST_V3_FIELDS:
                missing = sorted(_INSTALL_CONSENT_REQUEST_V3_FIELDS - set(payload))[0]
                raise ProtocolValidationError(f"v3 RequestConsent payload is missing {missing}")
            _validate_requested_action_identity(payload, "v3 RequestConsent payload")
            if payload["requested_action_kind"] != "InstallCapability":
                raise ProtocolValidationError(
                    "v3 RequestConsent requested_action_kind must be InstallCapability"
                )
            capability_id, capability_kind, _installer_id = _validate_v3_install_binding(
                payload,
                "v3 RequestConsent payload",
            )
            _validate_v3_action_target(
                action,
                capability_id,
                capability_kind,
                "v3 RequestConsent payload",
            )
        else:
            raise ProtocolValidationError("RequestConsent payload schema is unsupported")
        if (
            action.payload["requested_action_precondition_revision"]
            != action.precondition_revision + 1
        ):
            raise ProtocolValidationError(
                "requested_action_precondition_revision must target the next committed revision"
            )
    elif action.kind == "PresentBundle":
        capabilities = action.payload.get("capabilities")
        if not isinstance(capabilities, tuple):
            raise ProtocolValidationError("PresentBundle payload.capabilities must be an array")
        if len(capabilities) > 5:
            raise ProtocolValidationError(
                "PresentBundle cannot contain more than five capabilities"
            )
    elif action.kind == "Notify":
        _required_string(action.payload.get("message"), "Notify payload.message")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScopeRef:
    """Complete ownership and attribution scope for an engine fact."""

    tenant_id: str
    workspace_id: str
    repository_id: str
    session_id: str
    exposure_id: str
    host_context_id: str
    parent_exposure_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "tenant_id",
            "workspace_id",
            "repository_id",
            "session_id",
            "exposure_id",
            "host_context_id",
        ):
            object.__setattr__(
                self, field_name, _required_string(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self,
            "parent_exposure_id",
            _optional_string(self.parent_exposure_id, "parent_exposure_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "exposure_id": self.exposure_id,
            "host_context_id": self.host_context_id,
            "parent_exposure_id": self.parent_exposure_id,
            "repository_id": self.repository_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScopeRef":
        value = _reject_unknown_fields(value, _SCOPE_FIELDS, "scope")
        try:
            return cls(
                tenant_id=value["tenant_id"],
                workspace_id=value["workspace_id"],
                repository_id=value["repository_id"],
                session_id=value["session_id"],
                exposure_id=value["exposure_id"],
                host_context_id=value["host_context_id"],
                parent_exposure_id=value.get("parent_exposure_id"),
            )
        except KeyError as exc:
            raise ProtocolValidationError(f"scope is missing {exc.args[0]}") from exc

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "ScopeRef":
        return cls.from_dict(_decode_json(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class PrivacyLabel:
    """Persistence classification attached to protocol facts and actions."""

    classification: str = "private"
    retention: str = "local"

    def __post_init__(self) -> None:
        if self.classification not in _PRIVACY_CLASSIFICATIONS:
            raise ProtocolValidationError(f"unknown privacy classification {self.classification!r}")
        if self.retention not in _RETENTION_CLASSES:
            raise ProtocolValidationError(f"unknown retention class {self.retention!r}")

    def to_dict(self) -> dict[str, str]:
        return {"classification": self.classification, "retention": self.retention}

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PrivacyLabel":
        value = _reject_unknown_fields(value, _PRIVACY_FIELDS, "privacy label")
        return cls(
            classification=value.get("classification", "private"),
            retention=value.get("retention", "local"),
        )

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "PrivacyLabel":
        return cls.from_dict(_decode_json(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class EventEnvelope:
    """One immutable, revision-checked fact submitted to the engine.

    Events that may cause a new engine decision carry every frozen planner,
    policy, host, catalog, semantic-index, and work input needed for replay.
    Evidence, receipts, and session-boundary events may omit those fields
    because they report or delimit facts whose decisions were already frozen.
    """

    event_id: str
    kind: str
    scope: ScopeRef
    expected_revision: int
    occurred_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    privacy: PrivacyLabel = field(default_factory=PrivacyLabel)
    protocol_version: int = PROTOCOL_VERSION
    correlation_id: str | None = None
    causation_id: str | None = None
    engine_version: str | None = None
    planner_version: str | None = None
    policy_version: str | None = None
    host_descriptor_digest: str | None = None
    catalog_snapshot_digest: str | None = None
    semantic_model_digest: str | None = None
    semantic_index_digest: str | None = None
    work_signature: str | None = None
    random_seed: int | str | None = None

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version)
        object.__setattr__(self, "event_id", _required_string(self.event_id, "event_id"))
        if self.kind not in EVENT_KINDS:
            raise ProtocolValidationError(f"unknown event kind {self.kind!r}")
        if not isinstance(self.scope, ScopeRef):
            raise ProtocolValidationError("scope must be a ScopeRef")
        object.__setattr__(
            self,
            "expected_revision",
            _nonnegative_integer(self.expected_revision, "expected_revision"),
        )
        object.__setattr__(self, "occurred_at", _rfc3339(self.occurred_at, "occurred_at"))
        if not isinstance(self.payload, Mapping):
            raise ProtocolValidationError("payload must be a JSON object")
        _validate_receipt_payload(self.kind, self.payload)
        if self.kind == "UserDecision":
            _validate_user_decision_payload(self.payload, self.expected_revision)
        if self.kind == "InstallConsentExpired":
            _validate_install_consent_expired_payload(self.payload)
        object.__setattr__(self, "payload", _freeze_json(self.payload))
        if not isinstance(self.privacy, PrivacyLabel):
            raise ProtocolValidationError("privacy must be a PrivacyLabel")
        for field_name in (
            "correlation_id",
            "causation_id",
            "engine_version",
            "planner_version",
            "policy_version",
            "host_descriptor_digest",
            "catalog_snapshot_digest",
            "semantic_model_digest",
            "semantic_index_digest",
            "work_signature",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_string(getattr(self, field_name), field_name),
            )
        if self.random_seed is not None and not (
            type(self.random_seed) is int or isinstance(self.random_seed, str)
        ):
            raise ProtocolValidationError("random_seed must be an integer, string, or null")
        if isinstance(self.random_seed, str):
            _validate_unicode_scalar(self.random_seed, "random_seed")
        if self.kind in _DECISION_CAUSING_EVENT_KINDS:
            for field_name in _DECISION_REPLAY_FIELDS:
                if getattr(self, field_name) is None:
                    raise ProtocolValidationError(
                        f"{field_name} is required for decision-causing event {self.kind}"
                    )

    @property
    def identity_digest(self) -> str:
        """Stable identity key; equal IDs intentionally share this digest."""

        return _digest_text(f"ctx-engine-event:{self.protocol_version}:{self.event_id}")

    @property
    def content_digest(self) -> str:
        """Digest of the complete normalized event, used to detect ID collisions."""

        return _digest_text(self.to_json())

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_snapshot_digest": self.catalog_snapshot_digest,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "engine_version": self.engine_version,
            "event_id": self.event_id,
            "expected_revision": self.expected_revision,
            "host_descriptor_digest": self.host_descriptor_digest,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "payload": _thaw_json(self.payload),
            "planner_version": self.planner_version,
            "policy_version": self.policy_version,
            "privacy": self.privacy.to_dict(),
            "protocol_version": self.protocol_version,
            "random_seed": self.random_seed,
            "scope": self.scope.to_dict(),
            "semantic_index_digest": self.semantic_index_digest,
            "semantic_model_digest": self.semantic_model_digest,
            "work_signature": self.work_signature,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        value = _reject_unknown_fields(value, _EVENT_FIELDS, "event")
        _validate_protocol_version(value.get("protocol_version"))
        try:
            scope_value = value["scope"]
            privacy_value = value["privacy"]
            if not isinstance(scope_value, Mapping):
                raise ProtocolValidationError("scope must be a JSON object")
            if not isinstance(privacy_value, Mapping):
                raise ProtocolValidationError("privacy must be a JSON object")
            return cls(
                protocol_version=value["protocol_version"],
                event_id=value["event_id"],
                kind=value["kind"],
                scope=ScopeRef.from_dict(scope_value),
                expected_revision=value["expected_revision"],
                occurred_at=value["occurred_at"],
                payload=value.get("payload", {}),
                privacy=PrivacyLabel.from_dict(privacy_value),
                correlation_id=value.get("correlation_id"),
                causation_id=value.get("causation_id"),
                engine_version=value.get("engine_version"),
                planner_version=value.get("planner_version"),
                policy_version=value.get("policy_version"),
                host_descriptor_digest=value.get("host_descriptor_digest"),
                catalog_snapshot_digest=value.get("catalog_snapshot_digest"),
                semantic_model_digest=value.get("semantic_model_digest"),
                semantic_index_digest=value.get("semantic_index_digest"),
                work_signature=value.get("work_signature"),
                random_seed=value.get("random_seed"),
            )
        except KeyError as exc:
            raise ProtocolValidationError(f"event is missing {exc.args[0]}") from exc

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "EventEnvelope":
        return cls.from_dict(_decode_json(value))


# The public engine consumes facts, while EventEnvelope names their wire shape.
EngineEvent = EventEnvelope


@dataclass(frozen=True, slots=True, kw_only=True)
class HostAction:
    """A requested host effect; construction does not imply application.

    ``precondition_revision`` is the already-committed engine revision against
    which the host is authorized to apply this action.  Because the journal is
    committed before delivery, actions in an ``N -> N+1`` transition target
    ``N+1``, not the event's expected revision ``N``.
    """

    action_id: str
    kind: str
    scope: ScopeRef
    precondition_revision: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    privacy: PrivacyLabel = field(default_factory=PrivacyLabel)
    protocol_version: int = PROTOCOL_VERSION
    entity_id: str | None = None
    source_digest: str | None = None
    plan_id: str | None = None
    catalog_snapshot_id: str | None = None
    consent_id: str | None = None
    lease_id: str | None = None
    expires_at: str | None = None
    required_host_feature: str | None = None
    verification: Mapping[str, Any] = field(default_factory=dict)
    rollback: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version)
        object.__setattr__(self, "action_id", _required_string(self.action_id, "action_id"))
        if self.kind not in ACTION_KINDS:
            raise ProtocolValidationError(f"unknown action kind {self.kind!r}")
        if not isinstance(self.scope, ScopeRef):
            raise ProtocolValidationError("scope must be a ScopeRef")
        object.__setattr__(
            self,
            "precondition_revision",
            _nonnegative_integer(self.precondition_revision, "precondition_revision"),
        )
        for field_name in (
            "entity_id",
            "source_digest",
            "plan_id",
            "catalog_snapshot_id",
            "consent_id",
            "lease_id",
            "required_host_feature",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_string(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "expires_at", _optional_rfc3339(self.expires_at, "expires_at"))
        if not isinstance(self.privacy, PrivacyLabel):
            raise ProtocolValidationError("privacy must be a PrivacyLabel")
        for field_name in ("payload", "verification", "rollback"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise ProtocolValidationError(f"{field_name} must be a JSON object")
            object.__setattr__(self, field_name, _freeze_json(value, field_name))
        _validate_action_contract(self)

    @property
    def identity_digest(self) -> str:
        return _digest_text(f"ctx-engine-action:{self.protocol_version}:{self.action_id}")

    @property
    def content_digest(self) -> str:
        return _digest_text(self.to_json())

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "consent_id": self.consent_id,
            "entity_id": self.entity_id,
            "expires_at": self.expires_at,
            "kind": self.kind,
            "lease_id": self.lease_id,
            "payload": _thaw_json(self.payload),
            "plan_id": self.plan_id,
            "precondition_revision": self.precondition_revision,
            "privacy": self.privacy.to_dict(),
            "protocol_version": self.protocol_version,
            "required_host_feature": self.required_host_feature,
            "rollback": _thaw_json(self.rollback),
            "scope": self.scope.to_dict(),
            "source_digest": self.source_digest,
            "verification": _thaw_json(self.verification),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HostAction":
        value = _reject_unknown_fields(value, _ACTION_FIELDS, "action")
        _validate_protocol_version(value.get("protocol_version"))
        try:
            scope_value = value["scope"]
            privacy_value = value["privacy"]
            if not isinstance(scope_value, Mapping):
                raise ProtocolValidationError("scope must be a JSON object")
            if not isinstance(privacy_value, Mapping):
                raise ProtocolValidationError("privacy must be a JSON object")
            return cls(
                protocol_version=value["protocol_version"],
                action_id=value["action_id"],
                kind=value["kind"],
                scope=ScopeRef.from_dict(scope_value),
                precondition_revision=value["precondition_revision"],
                payload=value.get("payload", {}),
                privacy=PrivacyLabel.from_dict(privacy_value),
                entity_id=value.get("entity_id"),
                source_digest=value.get("source_digest"),
                plan_id=value.get("plan_id"),
                catalog_snapshot_id=value.get("catalog_snapshot_id"),
                consent_id=value.get("consent_id"),
                lease_id=value.get("lease_id"),
                expires_at=value.get("expires_at"),
                required_host_feature=value.get("required_host_feature"),
                verification=value.get("verification", {}),
                rollback=value.get("rollback", {}),
            )
        except KeyError as exc:
            raise ProtocolValidationError(f"action is missing {exc.args[0]}") from exc

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "HostAction":
        return cls.from_dict(_decode_json(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class Transition:
    """Deterministic committed result of processing one engine event.

    A newly processed event always advances exactly one revision.  Duplicate
    replay is handled by the engine/store returning the originally cached
    transition; it is not represented as a new zero-revision protocol result.
    """

    event_id: str
    scope: ScopeRef
    from_revision: int
    to_revision: int
    actions: tuple[HostAction, ...] = ()
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_protocol_version(self.protocol_version)
        object.__setattr__(self, "event_id", _required_string(self.event_id, "event_id"))
        from_revision = _nonnegative_integer(self.from_revision, "from_revision")
        to_revision = _nonnegative_integer(self.to_revision, "to_revision")
        if to_revision != from_revision + 1:
            raise ProtocolValidationError(
                "to_revision must advance exactly one revision from from_revision"
            )
        object.__setattr__(self, "from_revision", from_revision)
        object.__setattr__(self, "to_revision", to_revision)
        if not isinstance(self.scope, ScopeRef):
            raise ProtocolValidationError("scope must be a ScopeRef")
        actions = tuple(self.actions)
        if not all(isinstance(action, HostAction) for action in actions):
            raise ProtocolValidationError("actions must contain only HostAction values")
        for action in actions:
            if action.precondition_revision != to_revision:
                raise ProtocolValidationError(
                    "action precondition_revision must equal committed transition to_revision"
                )
            if action.scope != self.scope:
                raise ProtocolValidationError("action scope must match transition scope")
        object.__setattr__(self, "actions", actions)
        diagnostics: list[Mapping[str, JsonValue]] = []
        for index, diagnostic in enumerate(self.diagnostics):
            if not isinstance(diagnostic, Mapping):
                raise ProtocolValidationError("diagnostics must contain JSON objects")
            frozen = _freeze_json(diagnostic, f"diagnostics[{index}]")
            assert isinstance(frozen, Mapping)
            diagnostics.append(frozen)
        object.__setattr__(self, "diagnostics", tuple(diagnostics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "diagnostics": [_thaw_json(item) for item in self.diagnostics],
            "event_id": self.event_id,
            "from_revision": self.from_revision,
            "protocol_version": self.protocol_version,
            "scope": self.scope.to_dict(),
            "to_revision": self.to_revision,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Transition":
        value = _reject_unknown_fields(value, _TRANSITION_FIELDS, "transition")
        _validate_protocol_version(value.get("protocol_version"))
        try:
            scope_value = value["scope"]
            if not isinstance(scope_value, Mapping):
                raise ProtocolValidationError("scope must be a JSON object")
            raw_actions = value.get("actions", [])
            if not isinstance(raw_actions, list):
                raise ProtocolValidationError("actions must be a JSON array")
            raw_diagnostics = value.get("diagnostics", [])
            if not isinstance(raw_diagnostics, list):
                raise ProtocolValidationError("diagnostics must be a JSON array")
            return cls(
                protocol_version=value["protocol_version"],
                event_id=value["event_id"],
                scope=ScopeRef.from_dict(scope_value),
                from_revision=value["from_revision"],
                to_revision=value["to_revision"],
                actions=tuple(HostAction.from_dict(action) for action in raw_actions),
                diagnostics=tuple(raw_diagnostics),
            )
        except KeyError as exc:
            raise ProtocolValidationError(f"transition is missing {exc.args[0]}") from exc

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "Transition":
        return cls.from_dict(_decode_json(value))


__all__ = [
    "ACTION_KINDS",
    "CANONICALIZATION_SCHEME",
    "EVENT_KINDS",
    "INSTALL_ACTION_PAYLOAD_SCHEMA_V3",
    "INSTALL_CONSENT_REQUEST_SCHEMA_V3",
    "INSTALL_RECEIPT_SCHEMA_V3",
    "MATERIAL_ACTION_PAYLOAD_SCHEMA_V3",
    "MATERIAL_RECEIPT_SCHEMA_V3",
    "PROMPT_CONTEXT_ACTION_PAYLOAD_SCHEMA_V1",
    "PROMPT_CONTEXT_RECEIPT_SCHEMA_V1",
    "PROTOCOL_VERSION",
    "EngineEvent",
    "EventEnvelope",
    "HostAction",
    "PrivacyLabel",
    "ProtocolValidationError",
    "ScopeRef",
    "Transition",
    "UnsupportedProtocolVersionError",
]
