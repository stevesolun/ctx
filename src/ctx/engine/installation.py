"""Typed installation plans and per-kind consent routing.

The objects in this module are deliberately prose-free.  They can identify an
authenticated installer plan, but they cannot carry a command, credential, or
filesystem path.  Concrete installer ports keep executable material ephemeral
and may prepare it only for an exact engine-issued action.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ClassVar,
    Literal,
    Mapping,
    Protocol,
    SupportsIndex,
    TypeAlias,
    runtime_checkable,
)

from ctx.engine.capability_schema import validate_capability_identity
from ctx.engine.content import MaterialIdentity
from ctx.engine.planner import CapabilitySelection
from ctx.engine.protocol import INSTALL_CONSENT_REQUEST_SCHEMA_V3, HostAction, ScopeRef

if TYPE_CHECKING:
    from ctx.engine.planning_v3 import CapabilityPlanSelectionV3


INSTALLABLE_CAPABILITY_KINDS = frozenset({"skill", "agent", "mcp-server"})
INSTALL_CONSENT_MODES = frozenset({"preapproved-auto", "ask-each-time"})
INSTALL_OPERATIONS = frozenset({"install"})
INSTALL_DECISION_BASES = frozenset({"interactive", "preapproved-policy"})

_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_DESCRIPTOR_V1_SCHEMA = "ctx.install-plan-descriptor-v1"
_DESCRIPTOR_V2_SCHEMA = "ctx.install-plan-descriptor-v2"
_POLICY_SCHEMA = "ctx.install-consent-policy-v1"
_PREPARED_SCHEMA = "ctx.prepared-install-plan-v1"
_PREPARED_V2_SCHEMA = "ctx.prepared-install-plan-v2"
_EXECUTION_BINDING_SCHEMA = "ctx.install-execution-binding-v1"


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a canonical token")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _capability_identity(capability_id: object, kind: object) -> tuple[str, str]:
    capability_id, kind = validate_capability_identity(capability_id, kind)
    if kind not in INSTALLABLE_CAPABILITY_KINDS:
        raise ValueError("kind is not an installable capability kind")
    return capability_id, kind


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def install_action_authorization_digest(
    *,
    action: HostAction,
    selection: CapabilityPlanSelectionV3,
    descriptor: InstallPlanDescriptor,
    catalog_snapshot_digest: str,
    policy_snapshot_digest: str,
) -> str:
    """Bind one install claim to exact journaled schema-v3 authority."""

    from ctx.engine.planning_v3 import CapabilityPlanSelectionV3

    if not isinstance(action, HostAction):
        raise TypeError("action must be a HostAction")
    if not isinstance(selection, CapabilityPlanSelectionV3):
        raise TypeError("selection must be a CapabilityPlanSelectionV3")
    if not isinstance(descriptor, InstallPlanDescriptor):
        raise TypeError("descriptor must be an InstallPlanDescriptor")
    _digest(catalog_snapshot_digest, "catalog_snapshot_digest")
    _digest(policy_snapshot_digest, "policy_snapshot_digest")
    authority = {
        "action": {
            "content_digest": action.content_digest,
            "json": action.to_json(),
        },
        "catalog_snapshot_digest": catalog_snapshot_digest,
        "descriptor": descriptor.to_dict(),
        "policy_snapshot_digest": policy_snapshot_digest,
        "schema": "ctx.install-action-authorization-v1",
        "selection": selection.to_mapping(),
    }
    encoded = json.dumps(
        authority,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def activation_action_authorization_digest(
    *,
    action: HostAction,
    execution_binding: InstallExecutionBinding,
    host_descriptor_digest: str,
) -> str:
    """Bind an activation claim to exact journal and verifier identities."""

    if not isinstance(action, HostAction):
        raise TypeError("action must be a HostAction")
    if not isinstance(execution_binding, InstallExecutionBinding):
        raise TypeError("execution_binding must be an InstallExecutionBinding")
    _digest(host_descriptor_digest, "host_descriptor_digest")
    return _canonical_digest(
        {
            "action_content_digest": action.content_digest,
            "action_json": action.to_json(),
            "execution_binding_digest": execution_binding.binding_digest,
            "host_descriptor_digest": host_descriptor_digest,
            "schema": "ctx.activation-action-authorization-v1",
        }
    )


def _exact_mapping(
    value: object,
    fields: frozenset[str],
    name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} field names must be strings")
    unknown = set(value) - fields
    missing = fields - set(value)
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{name} is missing fields: {', '.join(sorted(missing))}")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallPlanDescriptor:
    """Authenticated, prose-free identity for one catalog installation plan."""

    capability_id: str
    kind: str
    installer_id: str
    plan_digest: str
    provenance_digest: str
    descriptor_digest: str
    target_scope: str = "user"
    rollback_strategy: str = "atomic-restore"
    permission_expansion: bool = False
    credential_requirement: bool = False
    result_material_identity_digest: str | None = None
    schema_version: int = 1

    _V1_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "capability_id",
            "descriptor_digest",
            "installer_id",
            "kind",
            "plan_digest",
            "permission_expansion",
            "provenance_digest",
            "rollback_strategy",
            "schema",
            "target_scope",
            "credential_requirement",
        }
    )
    _V2_FIELDS: ClassVar[frozenset[str]] = _V1_FIELDS | frozenset(
        {"result_material_identity_digest"}
    )

    def __post_init__(self) -> None:
        _capability_identity(self.capability_id, self.kind)
        _token(self.installer_id, "installer_id")
        _digest(self.plan_digest, "plan_digest")
        _digest(self.provenance_digest, "provenance_digest")
        supplied_digest = _digest(self.descriptor_digest, "descriptor_digest")
        if self.target_scope != "user":
            raise ValueError("target_scope must be user")
        if self.rollback_strategy != "atomic-restore":
            raise ValueError("rollback_strategy must be atomic-restore")
        _boolean(self.permission_expansion, "permission_expansion")
        _boolean(self.credential_requirement, "credential_requirement")
        if self.schema_version not in {1, 2}:
            raise ValueError("install plan descriptor schema version is unsupported")
        if self.schema_version == 1:
            if self.result_material_identity_digest is not None:
                raise ValueError("v1 install plan cannot bind result material identity")
        else:
            _digest(
                self.result_material_identity_digest,
                "result_material_identity_digest",
            )
        if self.recomputed_descriptor_digest != supplied_digest:
            raise ValueError("descriptor_digest does not match descriptor fields")

    def _digest_mapping(self) -> dict[str, str | bool]:
        result: dict[str, str | bool] = {
            "capability_id": self.capability_id,
            "installer_id": self.installer_id,
            "kind": self.kind,
            "plan_digest": self.plan_digest,
            "permission_expansion": self.permission_expansion,
            "provenance_digest": self.provenance_digest,
            "rollback_strategy": self.rollback_strategy,
            "schema": (
                _DESCRIPTOR_V1_SCHEMA if self.schema_version == 1 else _DESCRIPTOR_V2_SCHEMA
            ),
            "target_scope": self.target_scope,
            "credential_requirement": self.credential_requirement,
        }
        if self.schema_version == 2:
            assert self.result_material_identity_digest is not None
            result["result_material_identity_digest"] = self.result_material_identity_digest
        return result

    @property
    def recomputed_descriptor_digest(self) -> str:
        return _canonical_digest(self._digest_mapping())

    def to_dict(self) -> dict[str, str | bool]:
        return {
            **self._digest_mapping(),
            "descriptor_digest": self.descriptor_digest,
        }

    def matches_result_material(self, material: MaterialIdentity) -> bool:
        """Whether v2 names this exact capability-bound material identity."""

        if not isinstance(material, MaterialIdentity):
            raise TypeError("material must be a MaterialIdentity")
        return bool(
            self.schema_version == 2
            and self.capability_id == material.capability_id
            and self.kind == material.kind
            and self.result_material_identity_digest == material.identity_digest
        )

    @classmethod
    def create(
        cls,
        *,
        capability_id: str,
        kind: str,
        installer_id: str,
        plan_digest: str,
        provenance_digest: str,
        target_scope: str = "user",
        rollback_strategy: str = "atomic-restore",
        permission_expansion: bool = False,
        credential_requirement: bool = False,
        result_material_identity_digest: str | None = None,
    ) -> InstallPlanDescriptor:
        schema_version = 2 if result_material_identity_digest is not None else 1
        digest_mapping: dict[str, object] = {
            "capability_id": capability_id,
            "installer_id": installer_id,
            "kind": kind,
            "plan_digest": plan_digest,
            "permission_expansion": permission_expansion,
            "provenance_digest": provenance_digest,
            "rollback_strategy": rollback_strategy,
            "schema": (_DESCRIPTOR_V2_SCHEMA if schema_version == 2 else _DESCRIPTOR_V1_SCHEMA),
            "target_scope": target_scope,
            "credential_requirement": credential_requirement,
        }
        if schema_version == 2:
            digest_mapping["result_material_identity_digest"] = result_material_identity_digest
        return cls(
            capability_id=capability_id,
            kind=kind,
            installer_id=installer_id,
            plan_digest=plan_digest,
            provenance_digest=provenance_digest,
            descriptor_digest=_canonical_digest(digest_mapping),
            target_scope=target_scope,
            rollback_strategy=rollback_strategy,
            permission_expansion=permission_expansion,
            credential_requirement=credential_requirement,
            result_material_identity_digest=result_material_identity_digest,
            schema_version=schema_version,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstallPlanDescriptor:
        if not isinstance(value, Mapping):
            raise ValueError("install plan descriptor must be an object")
        schema = value.get("schema")
        if schema == _DESCRIPTOR_V1_SCHEMA:
            parsed = _exact_mapping(value, cls._V1_FIELDS, "install plan descriptor")
            schema_version = 1
        elif schema == _DESCRIPTOR_V2_SCHEMA:
            parsed = _exact_mapping(value, cls._V2_FIELDS, "install plan descriptor")
            schema_version = 2
        else:
            raise ValueError("install plan descriptor schema is unsupported")
        return cls(
            capability_id=parsed["capability_id"],  # type: ignore[arg-type]
            kind=parsed["kind"],  # type: ignore[arg-type]
            installer_id=parsed["installer_id"],  # type: ignore[arg-type]
            plan_digest=parsed["plan_digest"],  # type: ignore[arg-type]
            provenance_digest=parsed["provenance_digest"],  # type: ignore[arg-type]
            descriptor_digest=parsed["descriptor_digest"],  # type: ignore[arg-type]
            target_scope=parsed["target_scope"],  # type: ignore[arg-type]
            rollback_strategy=parsed["rollback_strategy"],  # type: ignore[arg-type]
            permission_expansion=parsed["permission_expansion"],  # type: ignore[arg-type]
            credential_requirement=parsed["credential_requirement"],  # type: ignore[arg-type]
            result_material_identity_digest=parsed.get("result_material_identity_digest"),  # type: ignore[arg-type]
            schema_version=schema_version,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallPlanningBundle:
    """Exact install descriptor and full material identity frozen together."""

    descriptor: InstallPlanDescriptor
    result_material: MaterialIdentity

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, InstallPlanDescriptor):
            raise TypeError("descriptor must be an InstallPlanDescriptor")
        if not isinstance(self.result_material, MaterialIdentity):
            raise TypeError("result_material must be a MaterialIdentity")
        if self.descriptor.schema_version != 2:
            raise ValueError("install planning bundle requires descriptor schema v2")
        if not self.descriptor.matches_result_material(self.result_material):
            raise ValueError("install descriptor does not match the full result material")


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedInstallPlan:
    """Ephemeral plan envelope bound to one action, selection, and snapshot.

    The execution token is an opaque adapter-local identifier.  It is not a
    command and cannot contain whitespace, path separators, or shell syntax.
    """

    capability_id: str
    kind: str
    installer_id: str
    action_id: str
    action_content_digest: str
    selection_source_digest: str
    catalog_snapshot_digest: str
    plan_digest: str
    provenance_digest: str
    descriptor_digest: str
    consent_policy_digest: str
    execution_token: str
    operation: str = "install"
    target_scope: str = "user"
    rollback_strategy: str = "atomic-restore"
    permission_expansion: bool = False
    credential_requirement: bool = False
    result_material_identity_digest: str | None = None

    def __post_init__(self) -> None:
        try:
            _capability_identity(self.capability_id, self.kind)
        except ValueError as exc:
            raise ValueError("prepared plan kind and capability_id are inconsistent") from exc
        for field_name in ("installer_id", "action_id", "execution_token"):
            _token(getattr(self, field_name), field_name)
        for field_name in (
            "action_content_digest",
            "selection_source_digest",
            "catalog_snapshot_digest",
            "plan_digest",
            "provenance_digest",
            "descriptor_digest",
            "consent_policy_digest",
        ):
            _digest(getattr(self, field_name), field_name)
        if self.operation not in INSTALL_OPERATIONS:
            raise ValueError("operation must be install")
        if self.target_scope != "user":
            raise ValueError("target_scope must be user")
        if self.rollback_strategy != "atomic-restore":
            raise ValueError("rollback_strategy must be atomic-restore")
        _boolean(self.permission_expansion, "permission_expansion")
        _boolean(self.credential_requirement, "credential_requirement")
        if self.result_material_identity_digest is not None:
            _digest(
                self.result_material_identity_digest,
                "result_material_identity_digest",
            )

    def to_dict(self) -> dict[str, str | bool]:
        """Return a safe audit projection; executable material is never included."""

        result: dict[str, str | bool] = {
            "action_content_digest": self.action_content_digest,
            "action_id": self.action_id,
            "capability_id": self.capability_id,
            "catalog_snapshot_digest": self.catalog_snapshot_digest,
            "consent_policy_digest": self.consent_policy_digest,
            "credential_requirement": self.credential_requirement,
            "descriptor_digest": self.descriptor_digest,
            "installer_id": self.installer_id,
            "kind": self.kind,
            "operation": self.operation,
            "permission_expansion": self.permission_expansion,
            "plan_digest": self.plan_digest,
            "provenance_digest": self.provenance_digest,
            "rollback_strategy": self.rollback_strategy,
            "schema": (
                _PREPARED_SCHEMA
                if self.result_material_identity_digest is None
                else _PREPARED_V2_SCHEMA
            ),
            "selection_source_digest": self.selection_source_digest,
            "target_scope": self.target_scope,
        }
        if self.result_material_identity_digest is not None:
            result["result_material_identity_digest"] = self.result_material_identity_digest
        return result

    def matches_descriptor(self, descriptor: InstallPlanDescriptor) -> bool:
        return (
            self.capability_id == descriptor.capability_id
            and self.kind == descriptor.kind
            and self.installer_id == descriptor.installer_id
            and self.plan_digest == descriptor.plan_digest
            and self.provenance_digest == descriptor.provenance_digest
            and self.descriptor_digest == descriptor.descriptor_digest
            and self.target_scope == descriptor.target_scope
            and self.rollback_strategy == descriptor.rollback_strategy
            and self.permission_expansion == descriptor.permission_expansion
            and self.credential_requirement == descriptor.credential_requirement
            and self.result_material_identity_digest == descriptor.result_material_identity_digest
        )

    def matches_authorization(
        self,
        descriptor: InstallPlanDescriptor,
        decision: InstallAuthorizationDecision,
    ) -> bool:
        """Confirm post-grant preparation retained descriptor and policy bindings."""

        return (
            decision.authorization_eligible
            and self.matches_descriptor(descriptor)
            and self.consent_policy_digest == decision.policy_snapshot_digest
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallExecutionBinding:
    """Non-executable identity of the concrete driver, host, and target.

    This value is safe audit metadata, not a bearer capability. It contains no
    command, path, content, credential, callback, or execution token. The
    authoritative store burns it into the one-use install claim immediately
    before a runtime handle may expose executable material to a trusted driver.
    """

    driver_id: str
    driver_digest: str
    host_identity_digest: str
    target_identity_digest: str
    binding_digest: str = field(init=False)

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "binding_digest",
            "driver_digest",
            "driver_id",
            "host_identity_digest",
            "schema",
            "target_identity_digest",
        }
    )

    def __post_init__(self) -> None:
        _token(self.driver_id, "driver_id")
        for field_name in (
            "driver_digest",
            "host_identity_digest",
            "target_identity_digest",
        ):
            _digest(getattr(self, field_name), field_name)
        object.__setattr__(self, "binding_digest", _canonical_digest(self._digest_mapping()))

    def _digest_mapping(self) -> dict[str, str]:
        return {
            "driver_digest": self.driver_digest,
            "driver_id": self.driver_id,
            "host_identity_digest": self.host_identity_digest,
            "schema": _EXECUTION_BINDING_SCHEMA,
            "target_identity_digest": self.target_identity_digest,
        }

    def to_dict(self) -> dict[str, str]:
        return {**self._digest_mapping(), "binding_digest": self.binding_digest}

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstallExecutionBinding:
        parsed = _exact_mapping(value, cls._FIELDS, "install execution binding")
        if parsed["schema"] != _EXECUTION_BINDING_SCHEMA:
            raise ValueError("install execution binding schema is unsupported")
        binding = cls(
            driver_id=parsed["driver_id"],  # type: ignore[arg-type]
            driver_digest=parsed["driver_digest"],  # type: ignore[arg-type]
            host_identity_digest=parsed["host_identity_digest"],  # type: ignore[arg-type]
            target_identity_digest=parsed["target_identity_digest"],  # type: ignore[arg-type]
        )
        if binding.binding_digest != _digest(parsed["binding_digest"], "binding_digest"):
            raise ValueError("binding_digest does not match execution binding fields")
        return binding

    @classmethod
    def from_json(cls, value: str) -> InstallExecutionBinding:
        if not isinstance(value, str):
            raise TypeError("install execution binding JSON must be text")
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("install execution binding must be valid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError("install execution binding JSON must encode an object")
        binding = cls.from_dict(decoded)
        if binding.to_json() != value:
            raise ValueError("install execution binding JSON must use canonical encoding")
        return binding


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallConsentPolicy:
    """Immutable independent consent mode for each installable capability kind."""

    skill_mode: str = "ask-each-time"
    agent_mode: str = "ask-each-time"
    mcp_server_mode: str = "ask-each-time"
    policy_digest: str = field(init=False)

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"agent_mode", "mcp_server_mode", "policy_digest", "schema", "skill_mode"}
    )

    def __post_init__(self) -> None:
        for field_name in ("skill_mode", "agent_mode", "mcp_server_mode"):
            if getattr(self, field_name) not in INSTALL_CONSENT_MODES:
                raise ValueError(f"{field_name} is not a declared install consent mode")
        object.__setattr__(self, "policy_digest", _canonical_digest(self._digest_mapping()))

    def _digest_mapping(self) -> dict[str, str]:
        return {
            "agent_mode": self.agent_mode,
            "mcp_server_mode": self.mcp_server_mode,
            "schema": _POLICY_SCHEMA,
            "skill_mode": self.skill_mode,
        }

    def to_dict(self) -> dict[str, str]:
        return {**self._digest_mapping(), "policy_digest": self.policy_digest}

    def mode_for(self, kind: str) -> str:
        if kind == "skill":
            return self.skill_mode
        if kind == "agent":
            return self.agent_mode
        if kind == "mcp-server":
            return self.mcp_server_mode
        raise ValueError("kind is not an installable capability kind")

    @classmethod
    def safe_default(cls) -> InstallConsentPolicy:
        return cls()

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstallConsentPolicy:
        parsed = _exact_mapping(value, cls._FIELDS, "install consent policy")
        if parsed["schema"] != _POLICY_SCHEMA:
            raise ValueError("install consent policy schema is unsupported")
        policy = cls(
            skill_mode=parsed["skill_mode"],  # type: ignore[arg-type]
            agent_mode=parsed["agent_mode"],  # type: ignore[arg-type]
            mcp_server_mode=parsed["mcp_server_mode"],  # type: ignore[arg-type]
        )
        supplied_digest = _digest(parsed["policy_digest"], "policy_digest")
        if policy.policy_digest != supplied_digest:
            raise ValueError("policy_digest does not match policy fields")
        return policy


@runtime_checkable
class HeldInstallConsentPolicyAuthority(Protocol):
    """Host-held proof that one consent policy remains authoritative.

    The supplying context manager must hold the external policy lock for the
    entire engine transition. ``assert_current`` rechecks the protected policy
    immediately before the journal commit; this protocol does not provide or
    emulate that external authority.
    """

    @property
    def policy(self) -> InstallConsentPolicy: ...

    def assert_current(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractiveInstallDecisionReservation:
    """Exact host-authenticated decision identity for one external reservation."""

    scope: ScopeRef
    event_id: str
    event_content_digest: str
    consent_id: str
    decision: str
    policy_snapshot_digest: str
    requested_action_id: str
    requested_action_kind: str
    requested_action_content_digest: str
    requested_action_precondition_revision: int
    install_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ScopeRef):
            raise TypeError("scope must be a ScopeRef")
        for field_name in ("event_id", "consent_id", "requested_action_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be non-empty text")
        for field_name in (
            "event_content_digest",
            "policy_snapshot_digest",
            "requested_action_content_digest",
        ):
            _digest(getattr(self, field_name), field_name)
        if self.decision not in {"granted", "denied"}:
            raise ValueError("decision must be granted or denied")
        if self.requested_action_kind != "InstallCapability":
            raise ValueError("requested_action_kind must be InstallCapability")
        if (
            type(self.requested_action_precondition_revision) is not int
            or self.requested_action_precondition_revision < 1
        ):
            raise ValueError("requested_action_precondition_revision must be positive")
        if not isinstance(self.install_expires_at, str) or not self.install_expires_at:
            raise ValueError("install_expires_at must be non-empty text")


InteractiveInstallDecisionGuard: TypeAlias = Callable[
    [InteractiveInstallDecisionReservation],
    AbstractContextManager[None],
]
"""External one-shot broker for an authenticated interactive decision.

The returned context must authenticate and reserve the exact decision on
entry, remain held while CTX commits, release it on exceptional exit, and
settle it exactly once only on a clean exit. CTX invokes this contract but does
not supply the broker or treat an event payload as user authority.
"""


def _bounded_identity(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 256
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(f"{name} must be bounded non-empty identity text")
    return value


def _install_decision_stream_identity_digest(scope: ScopeRef) -> str:
    return _canonical_digest(
        {
            "repository_id": scope.repository_id,
            "schema": "ctx.install-decision-stream-identity-v1",
            "session_id": scope.session_id,
            "tenant_id": scope.tenant_id,
            "workspace_id": scope.workspace_id,
        }
    )


class _InstallDecisionEvidenceSerializationGuard:
    def __deepcopy__(self, memo: object) -> _InstallDecisionEvidenceSerializationGuard:
        del memo
        raise TypeError("committed install decision evidence cannot be serialized")

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("committed install decision evidence cannot be serialized")


_INSTALL_DECISION_EVIDENCE_SERIALIZATION_GUARD = _InstallDecisionEvidenceSerializationGuard()


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallDecisionEvidenceQuery:
    """Exact expected journal identity; this value grants no authority itself."""

    scope: ScopeRef
    consent_id: str
    decision: str
    decision_basis: str
    policy_snapshot_digest: str
    requested_action_id: str
    requested_action_kind: str
    requested_action_content_digest: str
    requested_action_precondition_revision: int
    event_id: str
    event_content_digest: str
    expected_head_revision: int
    expected_head_record_digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ScopeRef):
            raise TypeError("scope must be a ScopeRef")
        for name in ("consent_id", "requested_action_id", "event_id"):
            _bounded_identity(getattr(self, name), name)
        if self.decision not in {"granted", "denied"}:
            raise ValueError("decision must be granted or denied")
        if self.decision_basis not in INSTALL_DECISION_BASES:
            raise ValueError("decision_basis is not a declared installation decision basis")
        if self.requested_action_kind != "InstallCapability":
            raise ValueError("requested_action_kind must be InstallCapability")
        for name in (
            "policy_snapshot_digest",
            "requested_action_content_digest",
            "event_content_digest",
        ):
            _digest(getattr(self, name), name)
        if type(self.expected_head_revision) is not int or self.expected_head_revision < 0:
            raise ValueError("expected_head_revision must be a non-negative integer")
        if (
            type(self.requested_action_precondition_revision) is not int
            or self.requested_action_precondition_revision != self.expected_head_revision + 1
        ):
            raise ValueError(
                "requested_action_precondition_revision must target the expected next revision"
            )
        if self.expected_head_revision == 0:
            if self.expected_head_record_digest is not None:
                raise ValueError("genesis expected head cannot carry a record digest")
        else:
            _digest(self.expected_head_record_digest, "expected_head_record_digest")

    @property
    def stream_identity_digest(self) -> str:
        return _install_decision_stream_identity_digest(self.scope)


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class CommittedInstallDecisionEvidence:
    """Opaque process-bound proof issued from one fully validated journal."""

    scope: ScopeRef
    stream_identity_digest: str
    consent_id: str
    decision: str
    decision_basis: str
    policy_snapshot_digest: str
    requested_action_id: str
    requested_action_kind: str
    requested_action_content_digest: str
    requested_action_precondition_revision: int
    event_id: str
    event_content_digest: str
    committed_revision: int
    record_digest: str
    previous_record_digest: str | None
    result_state_digest: str
    _serialization_guard: object = field(repr=False, compare=False)
    _issuer_identity: object = field(repr=False, compare=False)
    _seal: str = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError(
            "CommittedInstallDecisionEvidence can only be issued by an authoritative store"
        )

    def __copy__(self) -> CommittedInstallDecisionEvidence:
        raise TypeError("committed install decision evidence cannot be copied")

    def __deepcopy__(self, memo: object) -> CommittedInstallDecisionEvidence:
        del memo
        raise TypeError("committed install decision evidence cannot be copied")

    def __reduce__(self) -> str | tuple[Any, ...]:
        raise TypeError("committed install decision evidence cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[Any, ...]:
        del protocol
        raise TypeError("committed install decision evidence cannot be serialized")


InstallDecisionEvidenceStatus: TypeAlias = Literal[
    "committed",
    "absent-at-expected-head",
    "head-advanced",
    "event-collision",
    "corrupt",
    "unavailable",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallDecisionEvidenceLookup:
    """Authoritative result held under a store-owned evidence transaction."""

    status: InstallDecisionEvidenceStatus
    evidence: CommittedInstallDecisionEvidence | None = None
    observed_head_revision: int | None = None
    observed_head_record_digest: str | None = None

    def __post_init__(self) -> None:
        if self.status == "committed":
            if not isinstance(self.evidence, CommittedInstallDecisionEvidence):
                raise ValueError("committed result requires opaque evidence")
        elif self.evidence is not None:
            raise ValueError("non-committed result cannot carry authority")
        if (self.observed_head_revision is None) != (self.observed_head_record_digest is None):
            if self.observed_head_revision != 0 or self.observed_head_record_digest is not None:
                raise ValueError("observed head revision and digest are inconsistent")
        if self.observed_head_revision is not None and (
            type(self.observed_head_revision) is not int or self.observed_head_revision < 0
        ):
            raise ValueError("observed_head_revision must be non-negative")
        if self.observed_head_record_digest is not None:
            _digest(self.observed_head_record_digest, "observed_head_record_digest")


class InstallDecisionEvidenceRejected(RuntimeError):
    """Opaque evidence did not originate from or match the issuing store."""


@runtime_checkable
class CommittedInstallDecisionEvidenceProvider(Protocol):
    """Trusted owner-held lookup and revalidation boundary for journal evidence."""

    def inspect_install_decision(
        self,
        query: InstallDecisionEvidenceQuery,
    ) -> AbstractContextManager[InstallDecisionEvidenceLookup]: ...

    def revalidate_install_decision_evidence(
        self,
        evidence: CommittedInstallDecisionEvidence,
        *,
        query: InstallDecisionEvidenceQuery,
    ) -> CommittedInstallDecisionEvidence: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallAuthorizationDecision:
    """Pure routing result; it grants no authority by itself."""

    decision_basis: str
    policy_snapshot_digest: str
    reason_code: str
    authorization_eligible: bool = True

    def __post_init__(self) -> None:
        if self.decision_basis not in INSTALL_DECISION_BASES:
            raise ValueError("decision_basis is not a declared installation decision basis")
        _digest(self.policy_snapshot_digest, "policy_snapshot_digest")
        _token(self.reason_code, "reason_code")
        _boolean(self.authorization_eligible, "authorization_eligible")

    @property
    def is_preapproved(self) -> bool:
        return self.authorization_eligible and self.decision_basis == "preapproved-policy"

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "authorization_eligible": self.authorization_eligible,
            "decision_basis": self.decision_basis,
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "reason_code": self.reason_code,
        }


class InstallConsentRoutingError(ValueError):
    """A consent request does not match current policy and typed plan identity."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallConsentDirective:
    """Safe host-neutral result for either UI prompting or automatic consent."""

    consent_id: str
    capability_id: str
    kind: str
    source_digest: str
    catalog_snapshot_digest: str
    plan_id: str
    install_plan_digest: str
    descriptor_digest: str
    installer_id: str
    provenance_digest: str
    permission_expansion: bool
    credential_requirement: bool
    decision_basis: str
    policy_snapshot_digest: str
    reason_code: str
    requested_action_id: str
    requested_action_kind: str
    requested_action_content_digest: str
    requested_action_precondition_revision: int
    result_material_identity_digest: str | None = None

    def __post_init__(self) -> None:
        _capability_identity(self.capability_id, self.kind)
        for field_name in (
            "consent_id",
            "installer_id",
            "plan_id",
            "reason_code",
            "requested_action_id",
        ):
            _token(getattr(self, field_name), field_name)
        for field_name in (
            "source_digest",
            "catalog_snapshot_digest",
            "install_plan_digest",
            "descriptor_digest",
            "provenance_digest",
            "policy_snapshot_digest",
            "requested_action_content_digest",
        ):
            _digest(getattr(self, field_name), field_name)
        _boolean(self.permission_expansion, "permission_expansion")
        _boolean(self.credential_requirement, "credential_requirement")
        if self.decision_basis not in INSTALL_DECISION_BASES:
            raise ValueError("decision_basis is not a declared installation decision basis")
        if self.requested_action_kind != "InstallCapability":
            raise ValueError("requested_action_kind must be InstallCapability")
        if (
            type(self.requested_action_precondition_revision) is not int
            or self.requested_action_precondition_revision < 1
        ):
            raise ValueError("requested_action_precondition_revision must be positive")
        if self.result_material_identity_digest is not None:
            _digest(
                self.result_material_identity_digest,
                "result_material_identity_digest",
            )

    @property
    def requires_prompt(self) -> bool:
        return self.decision_basis == "interactive"

    def decision_payload(self, decision: str) -> dict[str, str | int]:
        if decision not in {"granted", "denied"}:
            raise ValueError("decision must be granted or denied")
        return {
            "consent_id": self.consent_id,
            "decision": decision,
            "decision_basis": self.decision_basis,
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "requested_action_id": self.requested_action_id,
            "requested_action_kind": self.requested_action_kind,
            "requested_action_content_digest": self.requested_action_content_digest,
            "requested_action_precondition_revision": (self.requested_action_precondition_revision),
        }

    def automatic_grant_payload(self) -> dict[str, str | int] | None:
        if self.requires_prompt:
            return None
        return self.decision_payload("granted")


def route_install_authorization(
    policy: InstallConsentPolicy,
    descriptor: InstallPlanDescriptor,
) -> InstallAuthorizationDecision:
    """Route an authenticated descriptor before releasing its install action."""

    if descriptor.permission_expansion:
        return InstallAuthorizationDecision(
            decision_basis="interactive",
            policy_snapshot_digest=policy.policy_digest,
            reason_code="permission-expansion-requires-consent",
        )
    if descriptor.credential_requirement:
        return InstallAuthorizationDecision(
            decision_basis="interactive",
            policy_snapshot_digest=policy.policy_digest,
            reason_code="credentials-require-consent",
        )
    if policy.mode_for(descriptor.kind) == "preapproved-auto":
        return InstallAuthorizationDecision(
            decision_basis="preapproved-policy",
            policy_snapshot_digest=policy.policy_digest,
            reason_code="matching-preapproved-policy",
        )
    return InstallAuthorizationDecision(
        decision_basis="interactive",
        policy_snapshot_digest=policy.policy_digest,
        reason_code="per-install-consent-required",
    )


def route_install_consent_request(
    request: HostAction,
    selection: CapabilitySelection | CapabilityPlanSelectionV3,
    descriptor: InstallPlanDescriptor,
    policy: InstallConsentPolicy,
) -> InstallConsentDirective:
    """Bind one engine request to the still-current policy before any grant."""

    # ``planning_v3`` imports this module for InstallPlanDescriptor.  Resolve
    # the additive selection type only after both modules have initialized so
    # the legacy import graph and codecs remain unchanged.
    from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, InstallPlanningAuthority

    if (
        not isinstance(request, HostAction)
        or not isinstance(selection, (CapabilitySelection, CapabilityPlanSelectionV3))
        or not isinstance(descriptor, InstallPlanDescriptor)
        or not isinstance(policy, InstallConsentPolicy)
    ):
        raise TypeError("consent routing requires typed request, selection, plan, and policy")

    if isinstance(selection, CapabilityPlanSelectionV3):
        presentation = selection.presentation
        authority = selection.authority
        if (
            not isinstance(authority, InstallPlanningAuthority)
            or request.kind != "RequestConsent"
            or request.consent_id is None
            or request.entity_id != presentation.capability_id
            or request.source_digest != presentation.source_digest
            or request.catalog_snapshot_id is None
            or request.plan_id is None
            or presentation.actionability != "install"
            or presentation.install_descriptor_digest != descriptor.descriptor_digest
            or presentation.install_plan_digest != descriptor.plan_digest
            or presentation.capability_id != descriptor.capability_id
            or presentation.kind != descriptor.kind
            or descriptor != authority.descriptor
            or request.payload.get("schema") != INSTALL_CONSENT_REQUEST_SCHEMA_V3
            or request.payload.get("capability_kind") != presentation.kind
            or request.payload.get("catalog_identity") != selection.catalog_identity.to_dict()
            or request.payload.get("result_material") != authority.result_material.to_dict()
            or request.payload.get("install_plan_descriptor") != descriptor.to_dict()
            or request.payload.get("policy_snapshot_digest") != policy.policy_digest
            or request.payload.get("requested_action_kind") != "InstallCapability"
        ):
            raise InstallConsentRoutingError(
                "consent request does not match current policy and install identity"
            )
        decision = route_install_authorization(policy, descriptor)
        return InstallConsentDirective(
            consent_id=request.consent_id,
            capability_id=presentation.capability_id,
            kind=presentation.kind,
            source_digest=presentation.source_digest,
            catalog_snapshot_digest=request.catalog_snapshot_id,
            plan_id=request.plan_id,
            install_plan_digest=descriptor.plan_digest,
            descriptor_digest=descriptor.descriptor_digest,
            installer_id=descriptor.installer_id,
            provenance_digest=descriptor.provenance_digest,
            permission_expansion=descriptor.permission_expansion,
            credential_requirement=descriptor.credential_requirement,
            decision_basis=decision.decision_basis,
            policy_snapshot_digest=decision.policy_snapshot_digest,
            reason_code=decision.reason_code,
            requested_action_id=request.payload["requested_action_id"],  # type: ignore[arg-type]
            requested_action_kind=request.payload["requested_action_kind"],  # type: ignore[arg-type]
            requested_action_content_digest=request.payload["requested_action_content_digest"],  # type: ignore[arg-type]
            requested_action_precondition_revision=request.payload[
                "requested_action_precondition_revision"
            ],  # type: ignore[arg-type]
            result_material_identity_digest=authority.result_material.identity_digest,
        )

    if (
        request.kind != "RequestConsent"
        or request.consent_id is None
        or request.entity_id != selection.capability_id
        or request.source_digest != selection.source_digest
        or request.catalog_snapshot_id is None
        or request.plan_id is None
        or selection.actionability != "install"
        or selection.install_descriptor_digest != descriptor.descriptor_digest
        or selection.install_plan_digest != descriptor.plan_digest
        or selection.capability_id != descriptor.capability_id
        or selection.kind != descriptor.kind
        or request.payload.get("install_descriptor_digest") != descriptor.descriptor_digest
        or request.payload.get("install_plan_digest") != descriptor.plan_digest
        or request.payload.get("result_material_identity_digest")
        != descriptor.result_material_identity_digest
        or request.payload.get("policy_snapshot_digest") != policy.policy_digest
        or request.payload.get("requested_action_kind") != "InstallCapability"
    ):
        raise InstallConsentRoutingError(
            "consent request does not match current policy and install identity"
        )
    decision = route_install_authorization(policy, descriptor)
    return InstallConsentDirective(
        consent_id=request.consent_id,
        capability_id=selection.capability_id,
        kind=selection.kind,
        source_digest=selection.source_digest,
        catalog_snapshot_digest=request.catalog_snapshot_id,
        plan_id=request.plan_id,
        install_plan_digest=descriptor.plan_digest,
        descriptor_digest=descriptor.descriptor_digest,
        installer_id=descriptor.installer_id,
        provenance_digest=descriptor.provenance_digest,
        permission_expansion=descriptor.permission_expansion,
        credential_requirement=descriptor.credential_requirement,
        decision_basis=decision.decision_basis,
        policy_snapshot_digest=decision.policy_snapshot_digest,
        reason_code=decision.reason_code,
        requested_action_id=request.payload["requested_action_id"],  # type: ignore[arg-type]
        requested_action_kind=request.payload["requested_action_kind"],  # type: ignore[arg-type]
        requested_action_content_digest=request.payload["requested_action_content_digest"],  # type: ignore[arg-type]
        requested_action_precondition_revision=request.payload[
            "requested_action_precondition_revision"
        ],  # type: ignore[arg-type]
        result_material_identity_digest=descriptor.result_material_identity_digest,
    )


class CapabilityInstallPlanPort(Protocol):
    """Host-neutral boundary for authenticated, typed installation plans."""

    installation_snapshot_digest: str

    def describe(self, capability_id: str, kind: str) -> InstallPlanDescriptor | None: ...

    def prepare(
        self,
        action: HostAction,
        selection: CapabilitySelection,
        descriptor: InstallPlanDescriptor,
        *,
        expected_catalog_snapshot_digest: str,
        expected_policy_digest: str,
        authority: InstallAuthorizer | None = None,
    ) -> PreparedInstallPlan: ...


class CapabilityInstallBundlePort(Protocol):
    """Pinned source of complete, authenticated schema-v3 install authority."""

    installation_snapshot_digest: str

    def describe_bundle(
        self,
        capability_id: str,
        kind: str,
    ) -> InstallPlanningBundle | None: ...


class InstallAuthorizer(Protocol):
    """One-shot, journal-backed claim for an exact installation action.

    A point-in-time validator is insufficient: a concrete implementation must
    atomically claim the pending action, enforce expiry, and make replay or
    concurrent reuse impossible before executable install material is exposed.
    The concrete CTX engine accepts only schema-v3 installation selections;
    legacy values remain in this additive protocol union solely so existing
    install-plan ports can migrate without changing their call shape.
    """

    def authorize_install(
        self,
        action: HostAction,
        selection: CapabilitySelection | CapabilityPlanSelectionV3,
        descriptor: InstallPlanDescriptor,
        *,
        expected_catalog_snapshot_digest: str,
        expected_policy_digest: str,
        execution_binding: InstallExecutionBinding,
    ) -> None: ...


__all__ = [
    "CommittedInstallDecisionEvidence",
    "CommittedInstallDecisionEvidenceProvider",
    "CapabilityInstallBundlePort",
    "CapabilityInstallPlanPort",
    "HeldInstallConsentPolicyAuthority",
    "INSTALLABLE_CAPABILITY_KINDS",
    "INSTALL_CONSENT_MODES",
    "INSTALL_DECISION_BASES",
    "INSTALL_OPERATIONS",
    "InstallAuthorizationDecision",
    "InstallAuthorizer",
    "InstallConsentDirective",
    "InstallConsentPolicy",
    "InstallConsentRoutingError",
    "InstallDecisionEvidenceLookup",
    "InstallDecisionEvidenceQuery",
    "InstallDecisionEvidenceRejected",
    "InstallDecisionEvidenceStatus",
    "InstallExecutionBinding",
    "InteractiveInstallDecisionGuard",
    "InteractiveInstallDecisionReservation",
    "InstallPlanDescriptor",
    "InstallPlanningBundle",
    "PreparedInstallPlan",
    "activation_action_authorization_digest",
    "install_action_authorization_digest",
    "route_install_authorization",
    "route_install_consent_request",
]
