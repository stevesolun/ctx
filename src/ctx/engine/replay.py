"""Privacy-safe replay inputs for the host-neutral CTX engine.

The ingress event is deliberately separated from the durable reducer event.
``preflight`` is pure and cheap; only ``prepare`` may call an injected typed
observation normalizer.  Replay consumes the resulting ``ReplayInput`` and
never calls either operation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, TypeAlias

from ctx.engine.capability_schema import (
    CAPABILITY_KINDS,
    MAX_CANONICAL_TOKEN_CHARS,
    MAX_MATCHING_SIGNALS,
    MAX_REASON_CODES,
    MAX_SELECTED_CAPABILITIES,
    PRESENTED_ACTIONABILITY_STATES,
    SHA256_HEX_CHARS,
)
from ctx.engine.protocol import (
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
    EngineEvent,
    ProtocolValidationError,
    ScopeRef,
)
from ctx.engine.state import HOST_LEVELS, EngineState
from ctx.engine.store import JournalRecord, StreamId


REPLAY_SCHEMA = "ctx.engine.replay-input"
REPLAY_SCHEMA_VERSION = 1
DEFAULT_NORMALIZER_VERSION = "ctx-normalizer-structured-v1"
DEFAULT_REDUCER_VERSION = "ctx-reducer-v1"
_PLANNING_REDUCER_VERSION = "ctx-reducer-v2"
_INSTALL_PLANNING_REDUCER_VERSION = "ctx-reducer-v3"
_PROMPT_CONTEXT_REDUCER_VERSION = "ctx-reducer-v4"
_INSTALL_PLANNING_REDUCER_VERSIONS = frozenset(
    {_INSTALL_PLANNING_REDUCER_VERSION, _PROMPT_CONTEXT_REDUCER_VERSION}
)
MAX_JSON_DEPTH = 8
MAX_SURROGATE_BYTES = 16 * 1024
MAX_REPLAY_BYTES = 64 * 1024
MAX_INGRESS_BYTES = 256 * 1024
MAX_INGRESS_STRING_BYTES = 64 * 1024
MAX_COLLECTION_ITEMS = 100
MAX_INGRESS_TOTAL_ITEMS = 500

_OPAQUE_TOKEN_RE = re.compile(
    rf"\A[A-Za-z0-9][A-Za-z0-9._:@-]{{0,{MAX_CANONICAL_TOKEN_CHARS - 1}}}\Z"
)
_SHA256_RE = re.compile(rf"\A[0-9a-f]{{{SHA256_HEX_CHARS}}}\Z")
_OBSERVATION_KINDS = frozenset(
    {"WorkspaceObserved", "IntentObserved", "DevelopmentObserved", "ValidationObserved"}
)
_PLANNING_OBSERVATION_KINDS = frozenset({"IntentObserved", "DevelopmentObserved"})
_EMPTY_PAYLOAD_KINDS = frozenset({"TurnStarting", "TurnEnded", "SessionEnded"})
_DIGEST_METADATA_FIELDS = (
    "host_descriptor_digest",
    "catalog_snapshot_digest",
    "semantic_model_digest",
    "semantic_index_digest",
    "work_signature",
)
_TOKEN_METADATA_FIELDS = (
    "correlation_id",
    "causation_id",
    "engine_version",
    "planner_version",
    "policy_version",
)
_HOST_STATES = frozenset({"active", "inactive", "installed", "prepared"})
_CAPABILITY_KINDS = CAPABILITY_KINDS
_CAPABILITY_ACTIONABILITY = PRESENTED_ACTIONABILITY_STATES
_CAPABILITY_NAME_RE = re.compile(rf"\A[a-z0-9][a-z0-9._@-]{{0,{MAX_CANONICAL_TOKEN_CHARS - 1}}}\Z")
_MAX_WORK_SIGNALS = 32
_MAX_WORK_LANGUAGES = 10
_MAX_CONTEXT_CAPABILITY_IDS = 100
_MAX_MATCHING_SIGNALS = MAX_MATCHING_SIGNALS
_MAX_REASON_CODES = MAX_REASON_CODES
_PLAN_STATUSES = frozenset({"ready", "abstained", "degraded"})
_ABSTENTION_CODES = frozenset({"no-signals", "below-threshold", "no-relevant-capability"})
_DEGRADED_CODES = frozenset({"catalog-unavailable", "planner-failed"})
_CURRENT_WORK_FIELDS = frozenset(
    {
        "signals",
        "languages",
        "baseline_capability_ids",
        "active_capability_ids",
        "rejected_capability_ids",
        "requested_limit",
    }
)
_CAPABILITY_PLAN_FIELDS = frozenset({"status", "abstention_code", "capabilities"})
_CAPABILITY_PLAN_V3_FIELDS = frozenset(
    {"status", "abstention_code", "benefit_audit", "capabilities"}
)
_PLANNED_CAPABILITY_FIELDS = frozenset(
    {
        "capability_id",
        "kind",
        "name",
        "catalog_entry_digest",
        "normalized_score_ppm",
        "matching_signals",
        "reason_codes",
        "actionability",
    }
)
_PLANNED_CAPABILITY_V2_FIELDS = _PLANNED_CAPABILITY_FIELDS | frozenset(
    {"install_descriptor_digest", "install_plan_digest"}
)
_PLANNED_CAPABILITY_V3_FIELDS = _PLANNED_CAPABILITY_V2_FIELDS | frozenset(
    {"authority", "benefit", "catalog_identity"}
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
_V3_ABSTENTION_CODES = frozenset({"below-net-benefit", "limit-zero", "no-feasible-capability"})
_BENEFIT_RESULT_SCHEMA_ID = "ctx.benefit-selection-result-v1"
_BENEFIT_POLICY_SCHEMA_ID = "ctx.net-benefit-policy-v3"
_BENEFIT_SELECTION_ALGORITHM_ID = "ctx.greedy-bounded-subset-exchange-v1"
_MAX_BENEFIT_CANDIDATES = 512
_MAX_BENEFIT_SEARCH_EVALUATIONS = 1_000_000
_MAX_UTILITY_UNITS = 2**63 - 1
_REPLAY_FIELDS = frozenset(
    {
        "decision_surrogate",
        "normalizer_version",
        "observation_surrogate",
        "reducer_event",
        "reducer_version",
        "schema",
        "schema_version",
        "source_event_content_digest",
    }
)
_SURROGATE_FIELDS = frozenset({"schema_id", "schema_version", "value", "value_digest"})

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Mapping[str, "JsonValue"] | tuple["JsonValue", ...]


class ReplayError(ValueError):
    """Base class for replay-boundary failures."""


class ReplayValidationError(ReplayError):
    """A replay value violates its strict structural contract."""


class ReplayPrivacyError(ReplayValidationError):
    """An ingress value is unsafe for the authoritative journal."""


class ReplayBindingError(ReplayValidationError):
    """Replay data does not match its external journal metadata."""


class UnsupportedReplayEvent(ReplayValidationError):
    """No privacy-safe normalizer is available for an event."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReplayValidationError("replay value is not canonical-JSON compatible") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReplayValidationError("replay JSON contains a duplicate object key")
        result[key] = value
    return result


def _decode_canonical_json(
    value: str | bytes | bytearray,
    *,
    maximum_bytes: int,
) -> Mapping[str, Any]:
    if isinstance(value, (bytes, bytearray)):
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ReplayValidationError("replay JSON is not valid UTF-8") from exc
    elif isinstance(value, str):
        text = value
    else:
        raise ReplayValidationError("replay JSON must be text or UTF-8 bytes")
    if len(text.encode("utf-8")) > maximum_bytes:
        raise ReplayValidationError("replay JSON exceeds its size limit")
    try:
        decoded = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ReplayValidationError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise ReplayValidationError("replay JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise ReplayValidationError("replay JSON must contain an object")
    if _canonical_json(decoded) != text:
        raise ReplayValidationError("replay JSON must use the canonical encoding")
    return decoded


def _exact_mapping(
    value: object,
    fields: frozenset[str],
    object_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReplayValidationError(f"{object_name} must be an object")
    if set(value) != fields:
        raise ReplayValidationError(f"{object_name} has missing or unknown fields")
    return value


def _allowed_mapping(
    value: object,
    allowed: frozenset[str],
    object_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReplayValidationError(f"{object_name} must be an object")
    if not set(value).issubset(allowed):
        raise ReplayValidationError(f"{object_name} has unknown fields")
    return value


def _token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _OPAQUE_TOKEN_RE.fullmatch(value) is None:
        raise ReplayPrivacyError(f"{field_name} must be an opaque safe token")
    return value


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ReplayPrivacyError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _positive_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise ReplayValidationError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ReplayValidationError(f"{field_name} must be a non-negative integer")
    return value


def _bounded_nonnegative_integer(value: object, field_name: str, *, maximum: int) -> int:
    result = _nonnegative_integer(value, field_name)
    if result > maximum:
        raise ReplayValidationError(f"{field_name} exceeds its maximum")
    return result


def _freeze_surrogate(value: object, *, depth: int = 0) -> JsonValue:
    if depth > MAX_JSON_DEPTH:
        raise ReplayValidationError("structured surrogate exceeds the maximum depth")
    if value is None or isinstance(value, bool):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ReplayValidationError("structured surrogate numbers must be finite")
        return value
    if isinstance(value, str):
        return _token(value, "structured surrogate string")
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ReplayValidationError("structured surrogate object exceeds its item limit")
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            safe_key = _token(key, "structured surrogate field name")
            frozen[safe_key] = _freeze_surrogate(item, depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ReplayValidationError("structured surrogate array exceeds its item limit")
        return tuple(_freeze_surrogate(item, depth=depth + 1) for item in value)
    raise ReplayValidationError("structured surrogate contains a non-JSON value")


def _validate_ingress_bounds(
    value: object,
    *,
    depth: int = 0,
    item_budget: list[int] | None = None,
) -> int:
    """Bound hostile raw input before canonical serialization and hashing."""

    if item_budget is None:
        item_budget = [0]
    if depth > MAX_JSON_DEPTH:
        raise ReplayValidationError("ingress event exceeds the maximum depth")
    if value is None or isinstance(value, (bool, int, float)):
        return 16
    if isinstance(value, str):
        size = len(value.encode("utf-8"))
        if size > MAX_INGRESS_STRING_BYTES:
            raise ReplayValidationError("ingress event exceeds its size limit")
        return size
    if isinstance(value, Mapping):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ReplayValidationError("ingress object exceeds its item limit")
        item_budget[0] += len(value)
        if item_budget[0] > MAX_INGRESS_TOTAL_ITEMS:
            raise ReplayValidationError("ingress event exceeds its total item limit")
        total = 2
        for key, item in value.items():
            key_size = len(key.encode("utf-8"))
            if key_size > MAX_INGRESS_STRING_BYTES:
                raise ReplayValidationError("ingress event exceeds its per-string size limit")
            total += key_size
            total += _validate_ingress_bounds(
                item,
                depth=depth + 1,
                item_budget=item_budget,
            )
            if total > MAX_INGRESS_BYTES:
                raise ReplayValidationError("ingress event exceeds its size limit")
        return total
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ReplayValidationError("ingress array exceeds its item limit")
        item_budget[0] += len(value)
        if item_budget[0] > MAX_INGRESS_TOTAL_ITEMS:
            raise ReplayValidationError("ingress event exceeds its total item limit")
        total = 2
        for item in value:
            total += _validate_ingress_bounds(
                item,
                depth=depth + 1,
                item_budget=item_budget,
            )
            if total > MAX_INGRESS_BYTES:
                raise ReplayValidationError("ingress event exceeds its size limit")
        return total
    raise ReplayValidationError("ingress event contains a non-JSON value")


def _thaw(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuredSurrogate:
    """Small, typed semantic facts containing no free-form source material."""

    schema_id: str
    schema_version: int
    value: Mapping[str, JsonValue]
    value_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_id", _token(self.schema_id, "schema_id"))
        object.__setattr__(
            self,
            "schema_version",
            _positive_integer(self.schema_version, "schema_version"),
        )
        frozen = _freeze_surrogate(self.value)
        if not isinstance(frozen, Mapping):
            raise ReplayValidationError("structured surrogate value must be an object")
        object.__setattr__(self, "value", frozen)
        expected = _sha256_text(_canonical_json(_thaw(frozen)))
        supplied = _digest(self.value_digest, "value_digest")
        if supplied != expected:
            raise ReplayBindingError("value_digest does not match the structured value")
        if len(self.to_json().encode("utf-8")) > MAX_SURROGATE_BYTES:
            raise ReplayValidationError("structured surrogate exceeds its size limit")

    @classmethod
    def create(
        cls,
        *,
        schema_id: str,
        schema_version: int,
        value: Mapping[str, Any],
    ) -> StructuredSurrogate:
        frozen = _freeze_surrogate(value)
        assert isinstance(frozen, Mapping)
        digest = _sha256_text(_canonical_json(_thaw(frozen)))
        return cls(
            schema_id=schema_id,
            schema_version=schema_version,
            value=frozen,
            value_digest=digest,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "value": _thaw(self.value),
            "value_digest": self.value_digest,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> StructuredSurrogate:
        value = _exact_mapping(value, _SURROGATE_FIELDS, "structured surrogate")
        raw_value = value["value"]
        if not isinstance(raw_value, Mapping):
            raise ReplayValidationError("structured surrogate value must be an object")
        return cls(
            schema_id=value["schema_id"],
            schema_version=value["schema_version"],
            value=raw_value,
            value_digest=value["value_digest"],
        )

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> StructuredSurrogate:
        return cls.from_dict(_decode_canonical_json(value, maximum_bytes=MAX_SURROGATE_BYTES))


def _canonical_token_array(
    value: object,
    field_name: str,
    *,
    maximum: int = MAX_COLLECTION_ITEMS,
) -> tuple[str, ...]:
    tokens = tuple(
        _token(item, f"{field_name}[{index}]")
        for index, item in enumerate(_sequence(value, field_name, maximum=maximum))
    )
    if any(token != token.lower() for token in tokens):
        raise ReplayValidationError(f"{field_name} must use lowercase canonical tokens")
    if len(set(tokens)) != len(tokens):
        raise ReplayValidationError(f"{field_name} must not contain duplicates")
    if tokens != tuple(sorted(tokens)):
        raise ReplayValidationError(f"{field_name} must use canonical order")
    return tokens


def _validate_current_work(value: object) -> None:
    approved = _exact_mapping(value, _CURRENT_WORK_FIELDS, "current-work observation")
    _canonical_token_array(approved["signals"], "signals", maximum=_MAX_WORK_SIGNALS)
    _canonical_token_array(
        approved["languages"],
        "languages",
        maximum=_MAX_WORK_LANGUAGES,
    )
    _canonical_token_array(
        approved["baseline_capability_ids"],
        "baseline_capability_ids",
        maximum=_MAX_CONTEXT_CAPABILITY_IDS,
    )
    _canonical_token_array(
        approved["active_capability_ids"],
        "active_capability_ids",
        maximum=MAX_SELECTED_CAPABILITIES,
    )
    _canonical_token_array(
        approved["rejected_capability_ids"],
        "rejected_capability_ids",
        maximum=_MAX_CONTEXT_CAPABILITY_IDS,
    )
    _bounded_nonnegative_integer(
        approved["requested_limit"],
        "requested_limit",
        maximum=MAX_SELECTED_CAPABILITIES,
    )


def _validate_planned_capability(
    value: object,
    *,
    index: int,
    schema_version: int,
) -> tuple[str, int]:
    field_name = f"capabilities[{index}]"
    fields = _PLANNED_CAPABILITY_FIELDS if schema_version == 1 else _PLANNED_CAPABILITY_V2_FIELDS
    item = _exact_mapping(value, fields, "planned capability")
    capability_id = _token(item["capability_id"], f"{field_name}.capability_id")
    kind = _token(item["kind"], f"{field_name}.kind")
    if capability_id != capability_id.lower() or kind != kind.lower():
        raise ReplayValidationError(f"{field_name} identity must use lowercase canonical tokens")
    if kind not in _CAPABILITY_KINDS:
        raise ReplayValidationError(f"{field_name}.kind is not a recommendable capability kind")
    name = _token(item["name"], f"{field_name}.name")
    if _CAPABILITY_NAME_RE.fullmatch(name) is None:
        raise ReplayValidationError(f"{field_name}.name is not canonical")
    if capability_id != f"{kind}:{name}":
        raise ReplayValidationError(f"{field_name}.capability_id must match its kind and name")
    _digest(item["catalog_entry_digest"], f"{field_name}.catalog_entry_digest")
    score = _bounded_nonnegative_integer(
        item["normalized_score_ppm"],
        f"{field_name}.normalized_score_ppm",
        maximum=1_000_000,
    )
    _canonical_token_array(
        item["matching_signals"],
        f"{field_name}.matching_signals",
        maximum=_MAX_MATCHING_SIGNALS,
    )
    reason_codes = _canonical_token_array(
        item["reason_codes"],
        f"{field_name}.reason_codes",
        maximum=_MAX_REASON_CODES,
    )
    if not reason_codes:
        raise ReplayValidationError(f"{field_name}.reason_codes must not be empty")
    actionability = _token(item["actionability"], f"{field_name}.actionability")
    if actionability != actionability.lower():
        raise ReplayValidationError(f"{field_name}.actionability must use lowercase")
    if actionability not in _CAPABILITY_ACTIONABILITY:
        raise ReplayValidationError(f"{field_name}.actionability is not supported")
    if schema_version == 1:
        if actionability == "install":
            raise ReplayValidationError(f"{field_name} schema v1 cannot bind install plan identity")
    else:
        install_descriptor_digest = item["install_descriptor_digest"]
        install_plan_digest = item["install_plan_digest"]
        if actionability == "install":
            _digest(
                install_descriptor_digest,
                f"{field_name}.install_descriptor_digest",
            )
            _digest(install_plan_digest, f"{field_name}.install_plan_digest")
        elif install_descriptor_digest is not None or install_plan_digest is not None:
            raise ReplayValidationError(
                f"{field_name} install digests are allowed only for install actionability"
            )
    return capability_id, score


def _validate_capability_plan(value: object, *, schema_version: int) -> None:
    approved = _exact_mapping(value, _CAPABILITY_PLAN_FIELDS, "capability plan")
    status = _token(approved["status"], "capability plan status")
    if status not in _PLAN_STATUSES:
        raise ReplayValidationError("capability plan status is not supported")
    capabilities = _sequence(approved["capabilities"], "capabilities")
    validated_capabilities = tuple(
        _validate_planned_capability(item, index=index, schema_version=schema_version)
        for index, item in enumerate(capabilities)
    )
    capability_ids = tuple(capability_id for capability_id, _score in validated_capabilities)
    if len(set(capability_ids)) != len(capability_ids):
        raise ReplayValidationError("capabilities must have unique capability IDs")
    if validated_capabilities != tuple(
        sorted(validated_capabilities, key=lambda item: (-item[1], item[0]))
    ):
        raise ReplayValidationError("capabilities must use canonical ranked order")

    abstention_code = approved["abstention_code"]
    if abstention_code is not None:
        abstention_code = _token(abstention_code, "abstention_code")
    if status == "ready":
        if not capabilities or abstention_code is not None:
            raise ReplayValidationError(
                "ready capability plans require one to five capabilities and no abstention code"
            )
        return
    if capabilities:
        raise ReplayValidationError("non-ready capability plans cannot contain capabilities")
    allowed_codes = _ABSTENTION_CODES if status == "abstained" else _DEGRADED_CODES
    if abstention_code not in allowed_codes:
        raise ReplayValidationError("capability plan status and abstention code do not match")


def _signed_utility(value: object, field_name: str) -> int:
    if type(value) is not int or not -_MAX_UTILITY_UNITS <= value <= _MAX_UTILITY_UNITS:
        raise ReplayValidationError(f"{field_name} exceeds the signed utility bound")
    return value


def _validate_benefit_audit(value: object) -> Mapping[str, Any]:
    audit = _exact_mapping(value, _BENEFIT_AUDIT_FIELDS, "benefit audit")
    if audit["result_schema_id"] != _BENEFIT_RESULT_SCHEMA_ID:
        raise ReplayValidationError("benefit audit result schema is unsupported")
    if audit["policy_schema_id"] != _BENEFIT_POLICY_SCHEMA_ID:
        raise ReplayValidationError("benefit audit policy schema is unsupported")
    if audit["selection_algorithm_id"] != _BENEFIT_SELECTION_ALGORITHM_ID:
        raise ReplayValidationError("benefit audit selection algorithm is unsupported")
    for field_name in ("result_digest", "policy_digest", "calibration_digest"):
        _digest(audit[field_name], f"benefit audit {field_name}")
    _bounded_nonnegative_integer(
        audit["requested_limit"],
        "benefit audit requested_limit",
        maximum=MAX_SELECTED_CAPABILITIES,
    )
    _bounded_nonnegative_integer(
        audit["candidate_pool_count"],
        "benefit audit candidate_pool_count",
        maximum=_MAX_BENEFIT_CANDIDATES,
    )
    _bounded_nonnegative_integer(
        audit["search_evaluation_count"],
        "benefit audit search_evaluation_count",
        maximum=_MAX_BENEFIT_SEARCH_EVALUATIONS,
    )
    return audit


def _validate_v3_catalog_identity(
    value: object,
    *,
    field_name: str,
) -> Any:
    # Runtime-local imports avoid replay -> installation -> planner -> replay
    # initialization cycles while still applying the authoritative typed parser.
    from ctx.engine.lineage import CatalogCapabilityIdentity

    if not isinstance(value, Mapping):
        raise ReplayValidationError(f"{field_name} must be an object")
    try:
        return CatalogCapabilityIdentity.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise ReplayValidationError(f"{field_name} is invalid") from exc


def _validate_v3_load_authority(
    authority: Mapping[str, Any],
    *,
    capability_id: str,
    kind: str,
    catalog_identity_digest: str,
    field_name: str,
) -> None:
    from ctx.engine.content import AuthorizedMaterial

    approved = _exact_mapping(authority, _LOAD_AUTHORITY_FIELDS, f"{field_name} load authority")
    if not isinstance(approved["material"], Mapping):
        raise ReplayValidationError(f"{field_name}.material must be an object")
    try:
        material = AuthorizedMaterial.from_dict(approved["material"])
    except (TypeError, ValueError) as exc:
        raise ReplayValidationError(f"{field_name}.material is invalid") from exc
    if (
        material.capability_id,
        material.kind,
        material.catalog_identity_digest,
    ) != (capability_id, kind, catalog_identity_digest):
        raise ReplayValidationError(
            f"{field_name} load material does not match catalog capability identity"
        )


def _validate_v3_install_authority(
    authority: Mapping[str, Any],
    *,
    capability_id: str,
    kind: str,
    install_descriptor_digest: object,
    install_plan_digest: object,
    field_name: str,
) -> None:
    from ctx.engine.content import MaterialIdentity
    from ctx.engine.installation import InstallPlanDescriptor

    approved = _exact_mapping(
        authority,
        _INSTALL_AUTHORITY_FIELDS,
        f"{field_name} install authority",
    )
    if not isinstance(approved["descriptor"], Mapping) or not isinstance(
        approved["result_material"], Mapping
    ):
        raise ReplayValidationError(
            f"{field_name} install descriptor and result material must be objects"
        )
    try:
        descriptor = InstallPlanDescriptor.from_dict(approved["descriptor"])
        result_material = MaterialIdentity.from_dict(approved["result_material"])
    except (TypeError, ValueError) as exc:
        raise ReplayValidationError(f"{field_name} install authority is invalid") from exc
    if descriptor.schema_version != 2:
        raise ReplayValidationError(f"{field_name} install authority requires descriptor schema v2")
    if not descriptor.matches_result_material(result_material):
        raise ReplayValidationError(
            f"{field_name} install descriptor does not match result material"
        )
    if (
        descriptor.capability_id,
        descriptor.kind,
        result_material.capability_id,
        result_material.kind,
        descriptor.descriptor_digest,
        descriptor.plan_digest,
    ) != (
        capability_id,
        kind,
        capability_id,
        kind,
        install_descriptor_digest,
        install_plan_digest,
    ):
        raise ReplayValidationError(
            f"{field_name} install authority does not match the planned capability"
        )


def _validate_planned_capability_v3(
    value: object,
    *,
    index: int,
) -> tuple[int, str, str]:
    field_name = f"capabilities[{index}]"
    item = _exact_mapping(value, _PLANNED_CAPABILITY_V3_FIELDS, "planned capability v3")
    presentation = {field: item[field] for field in _PLANNED_CAPABILITY_V2_FIELDS}
    capability_id, _score = _validate_planned_capability(
        presentation,
        index=index,
        schema_version=2,
    )
    kind = item["kind"]
    source_digest = item["catalog_entry_digest"]

    catalog_identity = _validate_v3_catalog_identity(
        item["catalog_identity"],
        field_name=f"{field_name}.catalog_identity",
    )
    if (catalog_identity.capability_id, catalog_identity.kind) != (capability_id, kind):
        raise ReplayValidationError(
            f"{field_name} catalog identity does not match planned capability"
        )

    benefit = _exact_mapping(
        item["benefit"],
        _BENEFIT_PROJECTION_FIELDS,
        f"{field_name} benefit projection",
    )
    tier = benefit["tier"]
    if tier not in {"executable", "advisory"}:
        raise ReplayValidationError(f"{field_name} benefit tier is unsupported")
    _signed_utility(
        benefit["individual_net_benefit_u"],
        f"{field_name}.individual_net_benefit_u",
    )
    marginal = _signed_utility(
        benefit["marginal_net_benefit_u"],
        f"{field_name}.marginal_net_benefit_u",
    )
    if marginal < 1:
        raise ReplayValidationError(f"{field_name} marginal net benefit must be positive")

    authority = item["authority"]
    if not isinstance(authority, Mapping):
        raise ReplayValidationError(f"{field_name}.authority must be an object")
    authority_type = authority.get("type")
    actionability = item["actionability"]
    if authority_type != actionability:
        raise ReplayValidationError(f"{field_name} authority type must match actionability")
    if authority_type == "load":
        if tier != "executable":
            raise ReplayValidationError(f"{field_name} load authority must be executable")
        _validate_v3_load_authority(
            authority,
            capability_id=capability_id,
            kind=kind,
            catalog_identity_digest=catalog_identity.identity_digest,
            field_name=field_name,
        )
    elif authority_type == "install":
        if tier != "executable":
            raise ReplayValidationError(f"{field_name} install authority must be executable")
        _validate_v3_install_authority(
            authority,
            capability_id=capability_id,
            kind=kind,
            install_descriptor_digest=item["install_descriptor_digest"],
            install_plan_digest=item["install_plan_digest"],
            field_name=field_name,
        )
    elif authority_type == "manual":
        _exact_mapping(authority, _MANUAL_AUTHORITY_FIELDS, f"{field_name} manual authority")
        if tier != "advisory":
            raise ReplayValidationError(f"{field_name} manual authority must be advisory")
    else:
        raise ReplayValidationError(f"{field_name} authority type is unsupported")
    return (0 if tier == "executable" else 1), capability_id, source_digest


def _validate_capability_plan_v3(value: object) -> None:
    approved = _exact_mapping(value, _CAPABILITY_PLAN_V3_FIELDS, "capability plan v3")
    status = approved["status"]
    if status not in _PLAN_STATUSES:
        raise ReplayValidationError("capability plan v3 status is not supported")
    capabilities = _sequence(
        approved["capabilities"],
        "capability plan v3 capabilities",
        maximum=MAX_SELECTED_CAPABILITIES,
    )
    validated = tuple(
        _validate_planned_capability_v3(item, index=index)
        for index, item in enumerate(capabilities)
    )
    capability_ids = tuple(item[1] for item in validated)
    if len(set(capability_ids)) != len(capability_ids):
        raise ReplayValidationError("capability plan v3 contains duplicate capability IDs")
    if validated != tuple(sorted(validated)):
        raise ReplayValidationError("capability plan v3 capabilities must use canonical order")

    abstention_code = approved["abstention_code"]
    if abstention_code is not None:
        abstention_code = _token(abstention_code, "capability plan v3 abstention_code")
    if status == "degraded":
        if (
            capabilities
            or abstention_code not in _DEGRADED_CODES
            or approved["benefit_audit"] is not None
        ):
            raise ReplayValidationError(
                "degraded capability plan v3 requires no capabilities or benefit audit"
            )
        return

    audit = _validate_benefit_audit(approved["benefit_audit"])
    requested_limit = audit["requested_limit"]
    candidate_pool_count = audit["candidate_pool_count"]
    search_evaluations = audit["search_evaluation_count"]
    if status == "ready":
        if (
            not capabilities
            or abstention_code is not None
            or requested_limit == 0
            or candidate_pool_count == 0
            or search_evaluations == 0
            or len(capabilities) > requested_limit
            or len(capabilities) > candidate_pool_count
        ):
            raise ReplayValidationError(
                "ready capability plan v3 is inconsistent with its benefit audit"
            )
        return

    if capabilities or abstention_code not in _V3_ABSTENTION_CODES:
        raise ReplayValidationError(
            "abstained capability plan v3 requires a declared benefit abstention"
        )
    if abstention_code == "limit-zero":
        if requested_limit != 0 or search_evaluations != 0:
            raise ReplayValidationError("limit-zero benefit abstention is inconsistent")
    elif abstention_code == "no-feasible-capability":
        if requested_limit == 0 or search_evaluations != 0:
            raise ReplayValidationError("no-feasible benefit abstention is inconsistent")
    elif requested_limit == 0 or candidate_pool_count == 0 or search_evaluations == 0:
        raise ReplayValidationError("below-net-benefit abstention is inconsistent")


def _validate_approved_surrogate(
    surrogate: StructuredSurrogate,
    *,
    role: str,
) -> None:
    """Apply exact, engine-owned schemas; callers cannot approve new shapes."""

    identity = (role, surrogate.schema_id, surrogate.schema_version)
    value = surrogate.value
    if identity == ("observation", "ctx.observation.opaque-ref", 1):
        approved = _exact_mapping(
            value,
            frozenset({"provider_id", "content_digest"}),
            "opaque observation surrogate",
        )
        _token(approved["provider_id"], "observation provider_id")
        _digest(approved["content_digest"], "observation content_digest")
        return
    if identity == ("observation", "ctx.observation.current-work", 1):
        _validate_current_work(value)
        return
    if identity == ("decision", "ctx.decision.capability-set", 1):
        approved = _exact_mapping(
            value,
            frozenset({"capabilities"}),
            "capability-set decision surrogate",
        )
        for item in _sequence(approved["capabilities"], "capabilities"):
            _capability(item, with_lease=False)
        return
    if identity in {
        ("decision", "ctx.decision.capability-plan", 1),
        ("decision", "ctx.decision.capability-plan", 2),
    }:
        _validate_capability_plan(value, schema_version=surrogate.schema_version)
        return
    if identity == ("decision", "ctx.decision.capability-plan", 3):
        _validate_capability_plan_v3(value)
        return
    raise ReplayPrivacyError("surrogate schema is not approved for authoritative replay")


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationReference:
    """An in-memory opaque handle which is never included in replay JSON."""

    provider_id: str
    opaque_id: str
    content_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _token(self.provider_id, "provider_id"))
        object.__setattr__(self, "opaque_id", _token(self.opaque_id, "opaque_id"))
        object.__setattr__(
            self,
            "content_digest",
            _digest(self.content_digest, "observation content_digest"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class PreflightReplayInput:
    """Immutable output of pure ingress validation."""

    source_event_content_digest: str
    reducer_event: EngineEvent
    observation_ref: ObservationReference | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_event_content_digest",
            _digest(self.source_event_content_digest, "source_event_content_digest"),
        )
        if not isinstance(self.reducer_event, EngineEvent):
            raise ReplayValidationError("reducer_event must be an EngineEvent")
        if self.observation_ref is not None and not isinstance(
            self.observation_ref, ObservationReference
        ):
            raise ReplayValidationError("observation_ref must be an ObservationReference")


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanningContext:
    """Frozen decision provenance supplied by the validated source event."""

    planner_version: str
    catalog_snapshot_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "planner_version",
            _token(self.planner_version, "planner_version"),
        )
        object.__setattr__(
            self,
            "catalog_snapshot_digest",
            _digest(self.catalog_snapshot_digest, "catalog_snapshot_digest"),
        )


ObservationNormalizer: TypeAlias = Callable[
    [ObservationReference, EngineState | None],
    StructuredSurrogate,
]
DecisionPlanner: TypeAlias = Callable[
    [StructuredSurrogate, EngineState | None, PlanningContext],
    StructuredSurrogate,
]


def _scope_for_replay(scope: ScopeRef) -> ScopeRef:
    return ScopeRef(
        tenant_id=_token(scope.tenant_id, "tenant_id"),
        workspace_id=_token(scope.workspace_id, "workspace_id"),
        repository_id=_token(scope.repository_id, "repository_id"),
        session_id=_token(scope.session_id, "session_id"),
        exposure_id=_token(scope.exposure_id, "exposure_id"),
        host_context_id=_token(scope.host_context_id, "host_context_id"),
        parent_exposure_id=(
            None
            if scope.parent_exposure_id is None
            else _token(scope.parent_exposure_id, "parent_exposure_id")
        ),
    )


def _metadata_for_replay(event: EngineEvent) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field_name in _TOKEN_METADATA_FIELDS:
        value = getattr(event, field_name)
        metadata[field_name] = None if value is None else _token(value, field_name)
    for field_name in _DIGEST_METADATA_FIELDS:
        value = getattr(event, field_name)
        metadata[field_name] = None if value is None else _digest(value, field_name)
    seed = event.random_seed
    if isinstance(seed, str):
        seed = _token(seed, "random_seed")
    elif seed is not None and type(seed) is not int:
        raise ReplayPrivacyError("random_seed must be an integer or opaque safe token")
    metadata["random_seed"] = seed
    return metadata


def _sequence(value: object, field_name: str, *, maximum: int = 5) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes, bytearray)):
        raise ReplayValidationError(f"{field_name} must be an array")
    if len(value) > maximum:
        raise ReplayValidationError(f"{field_name} exceeds the bounded capability limit")
    return value


def _capability(value: object, *, with_lease: bool) -> dict[str, Any]:
    fields = (
        {"capability_id", "source_digest", "lease_id"}
        if with_lease
        else {
            "capability_id",
            "source_digest",
        }
    )
    item = _exact_mapping(value, frozenset(fields), "capability reference")
    result = {
        "capability_id": _token(item["capability_id"], "capability_id"),
        "source_digest": _digest(item["source_digest"], "source_digest"),
    }
    if with_lease:
        result["lease_id"] = _token(item["lease_id"], "lease_id")
    return result


def _planned_capability_reference_v3(value: object) -> dict[str, Any]:
    fields = frozenset(
        {
            "actionability",
            "capability_id",
            "install_descriptor_digest",
            "install_plan_digest",
            "kind",
            "lease_id",
            "source_digest",
        }
    )
    item = _exact_mapping(value, fields, "v3 capability reference")
    capability_id = _token(item["capability_id"], "capability_id")
    kind = _token(item["kind"], "kind")
    if kind not in _CAPABILITY_KINDS or capability_id != capability_id.lower():
        raise ReplayValidationError("v3 capability reference kind is unsupported")
    actionability = _token(item["actionability"], "actionability")
    if actionability not in _CAPABILITY_ACTIONABILITY:
        raise ReplayValidationError("v3 capability reference actionability is unsupported")
    install_descriptor_digest = item["install_descriptor_digest"]
    install_plan_digest = item["install_plan_digest"]
    if actionability == "install":
        install_descriptor_digest = _digest(
            install_descriptor_digest,
            "install_descriptor_digest",
        )
        install_plan_digest = _digest(install_plan_digest, "install_plan_digest")
    elif install_descriptor_digest is not None or install_plan_digest is not None:
        raise ReplayValidationError("install digests are allowed only for install actionability")
    return {
        "actionability": actionability,
        "capability_id": capability_id,
        "install_descriptor_digest": install_descriptor_digest,
        "install_plan_digest": install_plan_digest,
        "kind": kind,
        "lease_id": _token(item["lease_id"], "lease_id"),
        "source_digest": _digest(item["source_digest"], "source_digest"),
    }


def _receipt_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action_id": _token(payload.get("action_id"), "action_id"),
        "action_kind": _token(payload.get("action_kind"), "action_kind"),
        "action_content_digest": _digest(
            payload.get("action_content_digest"), "action_content_digest"
        ),
        "action_precondition_revision": _nonnegative_integer(
            payload.get("action_precondition_revision"),
            "action_precondition_revision",
        ),
    }


def _sanitize_ingress_payload(
    event: EngineEvent,
    *,
    reducer_version: str = DEFAULT_REDUCER_VERSION,
) -> tuple[Mapping[str, Any], ObservationReference | None]:
    payload = event.payload
    if event.kind == "SessionStarted":
        value = _allowed_mapping(payload, frozenset({"host_level"}), "SessionStarted payload")
        host_level = value.get("host_level", "query-only")
        if host_level not in HOST_LEVELS:
            raise ReplayValidationError("SessionStarted host_level is unsupported")
        return {"host_level": host_level}, None
    if event.kind == "ReassessmentRequested":
        active_fields = (
            frozenset({"owner_id", "desired_capabilities", "policy_snapshot_digest"})
            if reducer_version in _INSTALL_PLANNING_REDUCER_VERSIONS
            else frozenset({"owner_id", "desired_capabilities"})
        )
        retry_fields = frozenset({"retry_failed_deactivations"})
        if set(payload) == active_fields:
            desired = [
                (
                    _planned_capability_reference_v3(item)
                    if reducer_version in _INSTALL_PLANNING_REDUCER_VERSIONS
                    else _capability(item, with_lease=True)
                )
                for item in _sequence(payload["desired_capabilities"], "desired_capabilities")
            ]
            result: dict[str, Any] = {
                "owner_id": _token(payload["owner_id"], "owner_id"),
                "desired_capabilities": desired,
            }
            if reducer_version in _INSTALL_PLANNING_REDUCER_VERSIONS:
                result["policy_snapshot_digest"] = _digest(
                    payload["policy_snapshot_digest"],
                    "policy_snapshot_digest",
                )
            return result, None
        if set(payload) == retry_fields:
            retries = [
                _token(item, "retry capability_id")
                for item in _sequence(
                    payload["retry_failed_deactivations"],
                    "retry_failed_deactivations",
                )
            ]
            return {"retry_failed_deactivations": retries}, None
        raise ReplayValidationError("ReassessmentRequested payload has an unsupported schema")
    if event.kind == "ProviderSubmissionObserved":
        value = _exact_mapping(
            payload,
            frozenset({"capabilities"}),
            "ProviderSubmissionObserved payload",
        )
        return {
            "capabilities": [
                _capability(item, with_lease=False)
                for item in _sequence(value["capabilities"], "capabilities")
            ]
        }, None
    if event.kind == "ToolCallObserved":
        value = _exact_mapping(
            payload,
            frozenset({"capability_id", "source_digest", "outcome"}),
            "ToolCallObserved payload",
        )
        outcome = value["outcome"]
        if outcome not in {"failed", "succeeded"}:
            raise ReplayValidationError("ToolCallObserved outcome is unsupported")
        return {
            "capability_id": _token(value["capability_id"], "capability_id"),
            "source_digest": _digest(value["source_digest"], "source_digest"),
            "outcome": outcome,
        }, None
    if event.kind in _EMPTY_PAYLOAD_KINDS:
        _exact_mapping(payload, frozenset(), f"{event.kind} payload")
        return {}, None
    if event.kind == "UserDecision":
        fields = {
            "consent_id",
            "decision",
            "requested_action_id",
            "requested_action_kind",
            "requested_action_content_digest",
            "requested_action_precondition_revision",
        }
        if reducer_version in _INSTALL_PLANNING_REDUCER_VERSIONS:
            fields.update({"decision_basis", "policy_snapshot_digest"})
        value = _exact_mapping(payload, frozenset(fields), "UserDecision payload")
        result = {
            "consent_id": _token(value["consent_id"], "consent_id"),
            "decision": value["decision"],
            "requested_action_id": _token(value["requested_action_id"], "requested_action_id"),
            "requested_action_kind": value["requested_action_kind"],
            "requested_action_content_digest": _digest(
                value["requested_action_content_digest"],
                "requested_action_content_digest",
            ),
            "requested_action_precondition_revision": _nonnegative_integer(
                value["requested_action_precondition_revision"],
                "requested_action_precondition_revision",
            ),
        }
        if reducer_version in _INSTALL_PLANNING_REDUCER_VERSIONS:
            decision_basis = value["decision_basis"]
            if decision_basis not in {"interactive", "preapproved-policy"}:
                raise ReplayValidationError("UserDecision decision_basis is unsupported")
            result["decision_basis"] = decision_basis
            result["policy_snapshot_digest"] = _digest(
                value["policy_snapshot_digest"],
                "policy_snapshot_digest",
            )
        return result, None
    if event.kind == "InstallConsentExpired":
        if reducer_version not in _INSTALL_PLANNING_REDUCER_VERSIONS:
            raise UnsupportedReplayEvent("InstallConsentExpired requires an installation reducer")
        expiry_fields = frozenset(
            {
                "consent_id",
                "install_expires_at",
                "policy_snapshot_digest",
                "requested_action_id",
                "requested_action_kind",
                "requested_action_content_digest",
                "requested_action_precondition_revision",
            }
        )
        value = _exact_mapping(payload, expiry_fields, "InstallConsentExpired payload")
        return {
            "consent_id": _token(value["consent_id"], "consent_id"),
            "install_expires_at": _token(
                value["install_expires_at"],
                "install_expires_at",
            ),
            "policy_snapshot_digest": _digest(
                value["policy_snapshot_digest"],
                "policy_snapshot_digest",
            ),
            "requested_action_id": _token(
                value["requested_action_id"],
                "requested_action_id",
            ),
            "requested_action_kind": value["requested_action_kind"],
            "requested_action_content_digest": _digest(
                value["requested_action_content_digest"],
                "requested_action_content_digest",
            ),
            "requested_action_precondition_revision": _nonnegative_integer(
                value["requested_action_precondition_revision"],
                "requested_action_precondition_revision",
            ),
        }, None
    if event.kind == "ActionApplied":
        value = _allowed_mapping(
            payload,
            frozenset(
                {
                    "action_id",
                    "action_kind",
                    "action_content_digest",
                    "action_precondition_revision",
                    "verification",
                }
            ),
            "ActionApplied payload",
        )
        verification = value.get("verification")
        if not isinstance(verification, Mapping):
            raise ReplayValidationError("ActionApplied verification must be an object")
        if reducer_version in _INSTALL_PLANNING_REDUCER_VERSIONS:
            verification_schema = verification.get("schema")
            allowed_receipts = {
                INSTALL_RECEIPT_SCHEMA_V3,
                MATERIAL_RECEIPT_SCHEMA_V3,
            }
            if reducer_version == _PROMPT_CONTEXT_REDUCER_VERSION:
                allowed_receipts.add(PROMPT_CONTEXT_RECEIPT_SCHEMA_V1)
            if verification_schema not in allowed_receipts:
                raise ReplayValidationError(
                    "schema-v3 ActionApplied requires a typed receipt verification"
                )
            preserved = _freeze_surrogate(verification)
            if not isinstance(preserved, Mapping):
                raise AssertionError("frozen receipt verification is not an object")
            return {
                **_receipt_identity(value),
                "verification": preserved,
            }, None
        host_state = verification.get("host_state")
        if host_state not in _HOST_STATES:
            raise ReplayValidationError("ActionApplied host_state is unsupported")
        return {**_receipt_identity(value), "verification": {"host_state": host_state}}, None
    if event.kind == "ActionFailed":
        value = _allowed_mapping(
            payload,
            frozenset(
                {
                    "action_id",
                    "action_kind",
                    "action_content_digest",
                    "action_precondition_revision",
                    "error",
                    "rollback",
                }
            ),
            "ActionFailed payload",
        )
        return {
            **_receipt_identity(value),
            "error": {"code": "redacted-host-failure"},
        }, None
    if event.kind == "ActionExpired":
        value = _allowed_mapping(
            payload,
            frozenset(
                {
                    "action_id",
                    "action_kind",
                    "action_content_digest",
                    "action_precondition_revision",
                    "reason",
                }
            ),
            "ActionExpired payload",
        )
        return {**_receipt_identity(value), "reason": "expired"}, None
    if event.kind in _OBSERVATION_KINDS:
        if set(payload) != {"observation_ref"}:
            raise ReplayPrivacyError(
                "observation payload must contain only an opaque observation reference"
            )
        value = payload
        if not isinstance(value["observation_ref"], Mapping) or set(value["observation_ref"]) != {
            "provider_id",
            "opaque_id",
            "content_digest",
        }:
            raise ReplayPrivacyError("observation reference has an unsafe schema")
        reference_value = value["observation_ref"]
        reference = ObservationReference(
            provider_id=reference_value["provider_id"],
            opaque_id=reference_value["opaque_id"],
            content_digest=reference_value["content_digest"],
        )
        return {}, reference
    raise UnsupportedReplayEvent("event kind is not supported by the replay boundary")


def _validate_reducer_payload(
    event: EngineEvent,
    *,
    reducer_version: str = DEFAULT_REDUCER_VERSION,
) -> None:
    """Validate an already-sanitized event without resolving observations."""

    if event.kind in _OBSERVATION_KINDS:
        _exact_mapping(event.payload, frozenset(), "reducer observation payload")
        return
    sanitized, reference = _sanitize_ingress_payload(event, reducer_version=reducer_version)
    if reference is not None or _canonical_json(_thaw(sanitized)) != _canonical_json(
        _thaw(event.payload)
    ):
        raise ReplayBindingError("reducer event payload is not in canonical sanitized form")


def _safe_reducer_event(event: EngineEvent, payload: Mapping[str, Any]) -> EngineEvent:
    metadata = _metadata_for_replay(event)
    try:
        return EngineEvent(
            event_id=_token(event.event_id, "event_id"),
            kind=event.kind,
            scope=_scope_for_replay(event.scope),
            expected_revision=event.expected_revision,
            occurred_at=event.occurred_at,
            payload=payload,
            privacy=event.privacy,
            protocol_version=event.protocol_version,
            **metadata,
        )
    except ProtocolValidationError as exc:
        raise ReplayValidationError("sanitized reducer event violates the protocol") from exc


def _validate_persisted_event(event: EngineEvent, *, reducer_version: str) -> None:
    _token(event.event_id, "event_id")
    _scope_for_replay(event.scope)
    _metadata_for_replay(event)
    if event.privacy.retention in {"ephemeral", "aggregate"}:
        raise ReplayPrivacyError("event retention is not safe for authoritative replay")
    _validate_reducer_payload(event, reducer_version=reducer_version)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplayInput:
    """Canonical, privacy-approved input to one deterministic reducer call."""

    source_event_content_digest: str
    reducer_event: EngineEvent
    observation_surrogate: StructuredSurrogate | None = None
    decision_surrogate: StructuredSurrogate | None = None
    normalizer_version: str = DEFAULT_NORMALIZER_VERSION
    reducer_version: str = DEFAULT_REDUCER_VERSION
    schema: str = REPLAY_SCHEMA
    schema_version: int = REPLAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema != REPLAY_SCHEMA:
            raise ReplayValidationError("unsupported replay schema")
        if self.schema_version != REPLAY_SCHEMA_VERSION:
            raise ReplayValidationError("unsupported replay schema version")
        object.__setattr__(
            self,
            "source_event_content_digest",
            _digest(self.source_event_content_digest, "source_event_content_digest"),
        )
        object.__setattr__(
            self,
            "normalizer_version",
            _token(self.normalizer_version, "normalizer_version"),
        )
        object.__setattr__(
            self,
            "reducer_version",
            _token(self.reducer_version, "reducer_version"),
        )
        if not isinstance(self.reducer_event, EngineEvent):
            raise ReplayValidationError("reducer_event must be an EngineEvent")
        _validate_persisted_event(
            self.reducer_event,
            reducer_version=self.reducer_version,
        )
        for field_name in ("observation_surrogate", "decision_surrogate"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, StructuredSurrogate):
                raise ReplayValidationError(f"{field_name} must be a StructuredSurrogate")
        if self.observation_surrogate is not None:
            _validate_approved_surrogate(
                self.observation_surrogate,
                role="observation",
            )
        if self.decision_surrogate is not None:
            _validate_approved_surrogate(self.decision_surrogate, role="decision")
            if self.decision_surrogate.schema_id == "ctx.decision.capability-plan":
                expected_reducer_versions = {
                    1: frozenset({_PLANNING_REDUCER_VERSION}),
                    3: _INSTALL_PLANNING_REDUCER_VERSIONS,
                }.get(self.decision_surrogate.schema_version)
                if expected_reducer_versions is None:
                    raise ReplayValidationError(
                        "decision schema and reducer version are not compatible"
                    )
            else:
                expected_reducer_versions = frozenset({DEFAULT_REDUCER_VERSION})
            if self.reducer_version not in expected_reducer_versions:
                raise ReplayValidationError(
                    "decision schema and reducer version are not compatible"
                )
        if len(_canonical_json(self.to_dict()).encode("utf-8")) > MAX_REPLAY_BYTES:
            raise ReplayValidationError("replay input exceeds its size limit")

    def assert_record_binding(self, record: JournalRecord) -> None:
        if not isinstance(record, JournalRecord):
            raise ReplayBindingError("record must be a JournalRecord")
        if record.event_content_digest != self.source_event_content_digest:
            raise ReplayBindingError("record source event digest does not match replay input")
        if record.reducer_version != self.reducer_version:
            raise ReplayBindingError("record reducer version does not match replay input")
        if record.event_id != self.reducer_event.event_id:
            raise ReplayBindingError("record event id does not match replay input")
        if record.stream_id != StreamId.from_scope(self.reducer_event.scope):
            raise ReplayBindingError("record stream does not match replay input")
        if self.reducer_event.expected_revision != record.revision - 1:
            raise ReplayBindingError("record revision does not match replay input")
        if record.privacy_classification != self.reducer_event.privacy.classification:
            raise ReplayBindingError("record privacy classification does not match replay input")
        if record.retention_class != self.reducer_event.privacy.retention:
            raise ReplayBindingError("record retention class does not match replay input")
        if record.replay_json != self.to_json():
            raise ReplayBindingError("record replay JSON does not match replay input")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_surrogate": (
                None if self.decision_surrogate is None else self.decision_surrogate.to_dict()
            ),
            "normalizer_version": self.normalizer_version,
            "observation_surrogate": (
                None if self.observation_surrogate is None else self.observation_surrogate.to_dict()
            ),
            "reducer_event": self.reducer_event.to_dict(),
            "reducer_version": self.reducer_version,
            "schema": self.schema,
            "schema_version": self.schema_version,
            "source_event_content_digest": self.source_event_content_digest,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReplayInput:
        value = _exact_mapping(value, _REPLAY_FIELDS, "replay input")
        raw_event = value["reducer_event"]
        if not isinstance(raw_event, Mapping):
            raise ReplayValidationError("reducer_event must be an object")
        try:
            reducer_event = EngineEvent.from_dict(raw_event)
        except ProtocolValidationError as exc:
            raise ReplayValidationError("reducer_event violates the protocol") from exc
        if reducer_event.to_dict() != dict(raw_event):
            raise ReplayValidationError("nested replay values must use their exact canonical shape")
        surrogates: dict[str, StructuredSurrogate | None] = {}
        for field_name in ("observation_surrogate", "decision_surrogate"):
            raw = value[field_name]
            if raw is None:
                surrogates[field_name] = None
            elif isinstance(raw, Mapping):
                surrogates[field_name] = StructuredSurrogate.from_dict(raw)
            else:
                raise ReplayValidationError(f"{field_name} must be an object or null")
        return cls(
            schema=value["schema"],
            schema_version=value["schema_version"],
            normalizer_version=value["normalizer_version"],
            source_event_content_digest=value["source_event_content_digest"],
            reducer_version=value["reducer_version"],
            reducer_event=reducer_event,
            observation_surrogate=surrogates["observation_surrogate"],
            decision_surrogate=surrogates["decision_surrogate"],
        )

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> ReplayInput:
        decoded = _decode_canonical_json(value, maximum_bytes=MAX_REPLAY_BYTES)
        result = cls.from_dict(decoded)
        if result.to_json() != _canonical_json(decoded):
            raise ReplayValidationError("nested replay values must use their exact canonical shape")
        return result


@dataclass(frozen=True, slots=True)
class DefaultReplayInputFactory:
    """Fail-closed normalizer for the current reducer's safe event subset."""

    observation_normalizer: ObservationNormalizer | None = None
    normalizer_version: str = DEFAULT_NORMALIZER_VERSION
    reducer_version: str = DEFAULT_REDUCER_VERSION
    decision_planner: DecisionPlanner | None = None
    _validated_normalizer_version: str = field(init=False, repr=False)
    _validated_reducer_version: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_validated_normalizer_version",
            _token(self.normalizer_version, "normalizer_version"),
        )
        object.__setattr__(
            self,
            "_validated_reducer_version",
            _token(self.reducer_version, "reducer_version"),
        )
        if self.observation_normalizer is not None and not callable(self.observation_normalizer):
            raise ReplayValidationError("observation_normalizer must be callable")
        if self.decision_planner is not None and not callable(self.decision_planner):
            raise ReplayValidationError("decision_planner must be callable")
        if (
            self.decision_planner is not None
            and self._validated_reducer_version == DEFAULT_REDUCER_VERSION
        ):
            raise ReplayValidationError("decision_planner requires a planning-aware reducer")

    def preflight(self, event: EngineEvent) -> PreflightReplayInput:
        """Pure validation performed before duplicate lookup or store access."""

        if not isinstance(event, EngineEvent):
            raise ReplayValidationError("event must be an EngineEvent")
        _validate_ingress_bounds(event.payload)
        if event.privacy.retention in {"ephemeral", "aggregate"}:
            raise ReplayPrivacyError("event retention is not safe for authoritative replay")
        _token(event.event_id, "event_id")
        _scope_for_replay(event.scope)
        _metadata_for_replay(event)
        if event.kind == "SessionStarted" and event.host_descriptor_digest is None:
            raise ReplayValidationError("SessionStarted requires a host descriptor digest")
        payload, observation_ref = _sanitize_ingress_payload(
            event,
            reducer_version=self._validated_reducer_version,
        )
        reducer_event = _safe_reducer_event(event, payload)
        source_json = event.to_json()
        if len(source_json.encode("utf-8")) > MAX_INGRESS_BYTES:
            raise ReplayValidationError("ingress event exceeds its size limit")
        return PreflightReplayInput(
            source_event_content_digest=_sha256_text(source_json),
            reducer_event=reducer_event,
            observation_ref=observation_ref,
        )

    def prepare(
        self,
        preflight: PreflightReplayInput,
        state: EngineState | None,
        *,
        decision_surrogate: StructuredSurrogate | None = None,
    ) -> ReplayInput:
        """Run optional semantic work after duplicate and revision checks."""

        if not isinstance(preflight, PreflightReplayInput):
            raise ReplayValidationError("preflight must be a PreflightReplayInput")
        if state is not None and not isinstance(state, EngineState):
            raise ReplayValidationError("state must be an EngineState or null")
        if decision_surrogate is not None and self.decision_planner is not None:
            raise ReplayValidationError(
                "explicit decision surrogate and configured planner are mutually exclusive"
            )
        observation_surrogate = None
        if preflight.observation_ref is not None:
            if self.observation_normalizer is None:
                raise UnsupportedReplayEvent("event requires a typed observation normalizer")
            normalization_failed = False
            try:
                observation_surrogate = self.observation_normalizer(
                    preflight.observation_ref,
                    state,
                )
            except Exception:
                normalization_failed = True
            if normalization_failed:
                raise ReplayValidationError("typed observation normalization failed") from None
            if not isinstance(observation_surrogate, StructuredSurrogate):
                raise ReplayValidationError(
                    "typed observation normalizer returned an invalid surrogate"
                )
            _validate_approved_surrogate(observation_surrogate, role="observation")
            if (
                self.decision_planner is not None
                and preflight.reducer_event.kind in _PLANNING_OBSERVATION_KINDS
            ):
                planned_decision = None
                planning_failed = False
                try:
                    planned_decision = self.decision_planner(
                        observation_surrogate,
                        state,
                        PlanningContext(
                            planner_version=_token(
                                preflight.reducer_event.planner_version,
                                "planner_version",
                            ),
                            catalog_snapshot_digest=_digest(
                                preflight.reducer_event.catalog_snapshot_digest,
                                "catalog_snapshot_digest",
                            ),
                        ),
                    )
                except Exception:
                    planning_failed = True
                if planning_failed:
                    raise ReplayValidationError("typed decision planning failed") from None
                if not isinstance(planned_decision, StructuredSurrogate):
                    raise ReplayValidationError(
                        "typed decision planner returned an invalid surrogate"
                    )
                decision_surrogate = planned_decision
        return ReplayInput(
            source_event_content_digest=preflight.source_event_content_digest,
            reducer_event=preflight.reducer_event,
            observation_surrogate=observation_surrogate,
            decision_surrogate=decision_surrogate,
            normalizer_version=self._validated_normalizer_version,
            reducer_version=self._validated_reducer_version,
        )


__all__ = [
    "DEFAULT_NORMALIZER_VERSION",
    "DEFAULT_REDUCER_VERSION",
    "MAX_JSON_DEPTH",
    "MAX_REPLAY_BYTES",
    "MAX_SURROGATE_BYTES",
    "DefaultReplayInputFactory",
    "DecisionPlanner",
    "ObservationNormalizer",
    "ObservationReference",
    "PlanningContext",
    "PreflightReplayInput",
    "ReplayBindingError",
    "ReplayError",
    "ReplayInput",
    "ReplayPrivacyError",
    "ReplayValidationError",
    "StructuredSurrogate",
    "UnsupportedReplayEvent",
]
