"""Host-neutral consent and installation dispatcher for one reviewed release skill."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from ctx.core.install_consent_broker_store import (
    ConsentBrokerError,
    InstallConsentChallenge,
    SignedHumanDecisionAssertion,
)
from ctx.core.install_policy_store import (
    has_persisted_install_policy,
    hold_current_install_policy,
    load_current_install_policy,
)
from ctx.engine.benefit_audit_store import SQLiteBenefitAuditStore
from ctx.engine.benefit import BenefitSelectionResult
from ctx.engine.content import MaterialIdentity
from ctx.engine.engine import CtxEngine, CtxEngineError
from ctx.engine.installation import (
    InstallConsentDirective,
    InstallDecisionEvidenceQuery,
    InstallPlanDescriptor,
    InteractiveInstallDecisionGuard,
    InteractiveInstallDecisionReservation,
    route_install_consent_request,
)
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planner import WorkObservation
from ctx.engine.planning_v3 import (
    AuthenticatedNetBenefitPlanner,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
)
from ctx.engine.protocol import EngineEvent, HostAction, PrivacyLabel, ScopeRef, Transition
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION, INSTALLER_DIGEST
from ctx.engine.replay import (
    DefaultReplayInputFactory,
    ObservationReference,
    PlanningContext,
    ReplayInput,
    StructuredSurrogate,
)
from ctx.engine.state import CapabilityStateV3
from ctx.engine.store import SQLiteEngineStore, StreamId
from ctx.runtime.authenticated_benefit import capability_presentation_digest
from ctx.runtime.benefit_closure import (
    EligibleCatalogClaim,
    QueryCapabilityEligibility,
    eligible_catalog_claim_digest,
)
from ctx.runtime.install_execution import (
    InstallDriverRegistry,
    InstallDriverRequest,
    InstallExecutionError,
    prepare_install_execution,
)
from ctx.runtime.install_consent_broker import (
    InstallConsentBrokerService,
    InstallConsentEvidenceProviderUnavailable,
    install_consent_selection_digest,
)
from ctx.runtime.planning_v3 import AuthenticatedReplayDecisionPlannerV3
from ctx.runtime.production_catalog import (
    RELEASE_QUERY_CATALOG_ROOT_SHA256,
    ReleasePinnedQueryCatalog,
    open_release_pinned_query_catalog,
)
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.skill_cas import SkillCasRuntimeConfig
from ctx.runtime.workspace_identity import (
    WorkspaceIdentity,
    WorkspaceIdentityError,
    capture_workspace_identity,
)


RELEASE_SKILL_DISPATCH_ENGINE_VERSION = "ctx-release-skill-dispatch-v1"
RELEASE_SKILL_DISPATCH_PLANNER_VERSION = "ctx-release-skill-planner-v3"
_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")


class ReleaseSkillDispatchError(RuntimeError):
    """The reviewed release-skill flow could not preserve its authority chain."""


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSkillConsentChallengeProjection:
    """Primitive-only challenge identity safe to hand to an untrusted host UI."""

    challenge_id: str
    challenge_digest: str
    audience: str
    capability_id: str
    kind: str
    source_digest: str
    catalog_snapshot_digest: str
    plan_id: str
    install_plan_digest: str
    descriptor_digest: str
    execution_binding_digest: str
    selection_digest: str
    material_identity_digest: str
    requested_action_id: str
    requested_action_kind: str
    requested_action_content_digest: str
    requested_action_precondition_revision: int
    policy_snapshot_digest: str
    release_root_digest: str
    permission_expansion: bool
    credential_requirement: bool
    expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.challenge_id, str) or _TOKEN_RE.fullmatch(self.challenge_id) is None:
            raise ValueError("challenge_id must be a canonical token")
        if not isinstance(self.capability_id, str) or not self.capability_id:
            raise ValueError("capability_id must be non-empty text")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind must be non-empty text")
        if not isinstance(self.plan_id, str) or _TOKEN_RE.fullmatch(self.plan_id) is None:
            raise ValueError("plan_id must be a canonical token")
        if (
            not isinstance(self.requested_action_id, str)
            or _TOKEN_RE.fullmatch(self.requested_action_id) is None
        ):
            raise ValueError("requested_action_id must be a canonical token")
        if self.requested_action_kind != "InstallCapability":
            raise ValueError("requested_action_kind must be InstallCapability")
        for field_name in (
            "challenge_digest",
            "source_digest",
            "catalog_snapshot_digest",
            "install_plan_digest",
            "descriptor_digest",
            "execution_binding_digest",
            "material_identity_digest",
            "requested_action_content_digest",
            "policy_snapshot_digest",
            "release_root_digest",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        if _DIGEST_RE.fullmatch(self.selection_digest) is None:
            raise ValueError("selection_digest must be a lowercase SHA-256 digest")
        if _TOKEN_RE.fullmatch(self.audience) is None:
            raise ValueError("audience must be a canonical token")
        if (
            type(self.requested_action_precondition_revision) is not int
            or self.requested_action_precondition_revision < 1
        ):
            raise ValueError("requested_action_precondition_revision must be positive")
        if type(self.permission_expansion) is not bool:
            raise TypeError("permission_expansion must be a boolean")
        if type(self.credential_requirement) is not bool:
            raise TypeError("credential_requirement must be a boolean")
        try:
            expires_at = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise ValueError("expires_at must be an RFC 3339 timestamp") from None
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise ValueError("expires_at must include an offset")


def _safe_consent_challenge(
    challenge: InstallConsentChallenge,
) -> ReleaseSkillConsentChallengeProjection:
    """Project a broker value without exposing its scope or workspace identity."""

    if not isinstance(challenge, InstallConsentChallenge):
        raise TypeError("challenge must be an InstallConsentChallenge")
    return ReleaseSkillConsentChallengeProjection(
        challenge_id=challenge.challenge_id,
        challenge_digest=challenge.challenge_digest,
        audience=challenge.audience,
        capability_id=challenge.capability_id,
        kind=challenge.kind,
        source_digest=challenge.source_digest,
        catalog_snapshot_digest=challenge.catalog_snapshot_digest,
        plan_id=challenge.plan_id,
        install_plan_digest=challenge.install_plan_digest,
        descriptor_digest=challenge.descriptor_digest,
        execution_binding_digest=challenge.execution_binding_digest,
        selection_digest=challenge.selection_digest,
        material_identity_digest=challenge.material_identity_digest,
        requested_action_id=challenge.requested_action_id,
        requested_action_kind=challenge.requested_action_kind,
        requested_action_content_digest=challenge.requested_action_content_digest,
        requested_action_precondition_revision=(challenge.requested_action_precondition_revision),
        policy_snapshot_digest=challenge.policy_snapshot_digest,
        release_root_digest=challenge.release_root_digest,
        permission_expansion=challenge.permission_expansion,
        credential_requirement=challenge.credential_requirement,
        expires_at=challenge.expires_at,
    )


def _require_exact_broker_challenge(
    *,
    challenge: InstallConsentChallenge,
    directive: InstallConsentDirective,
    install_action: HostAction,
    execution_binding_digest: str,
    selection_digest: str,
    audience: str,
    workspace_identity_digest: str,
    release_root_digest: str,
) -> None:
    """Recheck broker output against dispatcher-held identities without deriving it."""

    challenge_identity: tuple[object, ...] = (
        challenge.challenge_id,
        challenge.audience,
        challenge.workspace_identity_digest,
        challenge.scope,
        challenge.capability_id,
        challenge.kind,
        challenge.source_digest,
        challenge.catalog_snapshot_digest,
        challenge.plan_id,
        challenge.install_plan_digest,
        challenge.descriptor_digest,
        challenge.execution_binding_digest,
        challenge.selection_digest,
        challenge.material_identity_digest,
        challenge.requested_action_id,
        challenge.requested_action_kind,
        challenge.requested_action_content_digest,
        challenge.requested_action_precondition_revision,
        challenge.policy_snapshot_digest,
        challenge.release_root_digest,
        challenge.permission_expansion,
        challenge.credential_requirement,
        challenge.expires_at,
    )
    expected_identity: tuple[object, ...] = (
        directive.consent_id,
        audience,
        workspace_identity_digest,
        install_action.scope,
        directive.capability_id,
        directive.kind,
        directive.source_digest,
        directive.catalog_snapshot_digest,
        directive.plan_id,
        directive.install_plan_digest,
        directive.descriptor_digest,
        execution_binding_digest,
        selection_digest,
        directive.result_material_identity_digest,
        install_action.action_id,
        install_action.kind,
        install_action.content_digest,
        install_action.precondition_revision,
        directive.policy_snapshot_digest,
        release_root_digest,
        directive.permission_expansion,
        directive.credential_requirement,
        install_action.expires_at,
    )
    if challenge_identity != expected_identity:
        raise ReleaseSkillDispatchError(
            "broker challenge changed the exact dispatcher install identity"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSkillInstallRequest:
    """Bounded host inputs for one idempotent release-skill install session."""

    host_context_id: str
    host_identity_digest: str
    task: str
    language: str
    session_id: str
    workspace: Path
    journal_path: Path
    benefit_audit_path: Path
    policy_store_root: Path
    skill_store_root: Path
    occurred_at: str
    _workspace_identity: WorkspaceIdentity = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in ("host_context_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a canonical token")
        if (
            not isinstance(self.host_identity_digest, str)
            or _DIGEST_RE.fullmatch(self.host_identity_digest) is None
        ):
            raise ValueError("host_identity_digest must be a lowercase SHA-256 digest")
        for field_name in (
            "workspace",
            "journal_path",
            "benefit_audit_path",
            "policy_store_root",
            "skill_store_root",
        ):
            if not isinstance(getattr(self, field_name), Path):
                raise TypeError(f"{field_name} must be a Path")
        if not isinstance(self.task, str) or not isinstance(self.language, str):
            raise TypeError("task and language must be strings")
        try:
            occurred_at = datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            raise ValueError("occurred_at must be an RFC 3339 timestamp") from None
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must include an offset")
        try:
            workspace_identity = capture_workspace_identity(self.workspace)
        except WorkspaceIdentityError as exc:
            raise ValueError(str(exc)) from None
        object.__setattr__(self, "_workspace_identity", workspace_identity)
        paths = tuple(
            Path(os.path.normcase(os.path.abspath(os.fspath(getattr(self, field_name)))))
            for field_name in (
                "workspace",
                "journal_path",
                "benefit_audit_path",
                "policy_store_root",
                "skill_store_root",
            )
        )
        if len(set(paths)) != len(paths):
            raise ValueError("dispatcher state and material paths must be distinct")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSkillDispatchResult:
    """Authority-free result of the exact consent/install/receipt sequence."""

    status: Literal[
        "abstained",
        "consent-required",
        "denied",
        "installed",
        "failed",
        "indeterminate",
    ]
    capability_id: str | None
    release_root_digest: str
    consent: InstallConsentDirective | None = None
    challenge: ReleaseSkillConsentChallengeProjection | None = None
    install_action_content_digest: str | None = None
    install_receipt_content_digest: str | None = None
    installed_lineage_digest: str | None = None
    activation_action_content_digest: str | None = None

    def __post_init__(self) -> None:
        if self.release_root_digest != RELEASE_QUERY_CATALOG_ROOT_SHA256:
            raise ValueError("dispatch result does not match the release root")
        if self.challenge is not None:
            if (
                self.status != "consent-required"
                or self.consent is None
                or not self.consent.requires_prompt
                or self.challenge.challenge_id != self.consent.consent_id
                or self.challenge.capability_id != self.consent.capability_id
                or self.challenge.policy_snapshot_digest != self.consent.policy_snapshot_digest
                or self.challenge.requested_action_id != self.consent.requested_action_id
                or self.challenge.requested_action_content_digest
                != self.consent.requested_action_content_digest
                or self.challenge.release_root_digest != self.release_root_digest
            ):
                raise ValueError("challenge does not match the exact pending consent")
        for field_name in (
            "install_action_content_digest",
            "install_receipt_content_digest",
            "installed_lineage_digest",
            "activation_action_content_digest",
        ):
            value = getattr(self, field_name)
            if value is not None and _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        if self.activation_action_content_digest is not None and (
            self.status != "installed"
            or self.installed_lineage_digest is None
            or self.install_receipt_content_digest is None
        ):
            raise ValueError("activation eligibility requires exact installed lineage")
        if self.status == "installed" and (
            self.install_action_content_digest is None
            or self.install_receipt_content_digest is None
            or self.installed_lineage_digest is None
            or self.activation_action_content_digest is None
        ):
            raise ValueError("installed result requires receipt-bound activation eligibility")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSkillRelevanceProbe:
    """Authority-free result of pure authenticated release-skill planning."""

    relevant: bool
    capability_id: str | None
    release_root_digest: str
    observation_digest: str
    planning_environment_digest: str

    def __post_init__(self) -> None:
        if type(self.relevant) is not bool:
            raise TypeError("relevant must be a boolean")
        if self.capability_id != (RELEASE_INSTALL_SKILL_ID if self.relevant else None):
            raise ValueError("relevance probe capability identity is inconsistent")
        if self.release_root_digest != RELEASE_QUERY_CATALOG_ROOT_SHA256:
            raise ValueError("relevance probe does not match the release root")
        for field_name in ("observation_digest", "planning_environment_digest"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


ReleaseSkillRecoveryPhase = Literal[
    "no-stream",
    "no-lifecycle",
    "abstained",
    "planned",
    "pending-consent",
    "decision-committed",
    "install-claimed",
    "install-outcome-recorded",
    "installed-inactive",
    "terminal-denied",
    "terminal-failed",
    "active",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseSkillRecoveryStatus:
    """Read-only classification of exact durable release-skill work."""

    requires_recovery: bool
    phase: ReleaseSkillRecoveryPhase
    revision: int
    capability_id: str | None
    release_root_digest: str

    def __post_init__(self) -> None:
        recovery_phases = {
            "planned",
            "pending-consent",
            "decision-committed",
            "install-claimed",
            "install-outcome-recorded",
            "installed-inactive",
        }
        if type(self.requires_recovery) is not bool:
            raise TypeError("requires_recovery must be a boolean")
        if self.requires_recovery != (self.phase in recovery_phases):
            raise ValueError("recovery phase and requirement are inconsistent")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("recovery revision must be non-negative")
        if self.phase == "no-stream" and self.revision != 0:
            raise ValueError("no-stream recovery status must have revision zero")
        capability_phases = recovery_phases | {
            "terminal-denied",
            "terminal-failed",
            "active",
        }
        expected_capability_id = (
            RELEASE_INSTALL_SKILL_ID if self.phase in capability_phases else None
        )
        if self.capability_id != expected_capability_id:
            raise ValueError("recovery capability identity is inconsistent")
        if self.release_root_digest != RELEASE_QUERY_CATALOG_ROOT_SHA256:
            raise ValueError("recovery status does not match the release root")


@dataclass(frozen=True, slots=True)
class _InstallHostPolicy:
    host_policy_snapshot_digest: str

    @classmethod
    def for_request(cls, request: ReleaseSkillInstallRequest) -> _InstallHostPolicy:
        return cls(
            host_policy_snapshot_digest=_digest(
                {
                    "actionability": "install",
                    "capability_id": RELEASE_INSTALL_SKILL_ID,
                    "host_context_id": request.host_context_id,
                    "host_identity_digest": request.host_identity_digest,
                    "schema": "ctx.release-skill-install-host-policy-v1",
                }
            )
        )

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        eligible = (
            presentation.capability_id == RELEASE_INSTALL_SKILL_ID
            and presentation.kind == "skill"
            and presentation.actionability == "install"
        )
        return QueryCapabilityEligibility(
            presentation_digest=capability_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=eligible_catalog_claim_digest(claim),
            available=eligible,
            permissions_allowed=eligible,
            credentials_available=eligible,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class _DispatchProvenance:
    """Exact event metadata inherited from the durable planning decision."""

    catalog_snapshot_digest: str
    host_descriptor_digest: str
    reviewed_policy_digest: str
    semantic_index_digest: str
    work_signature: str

    @classmethod
    def from_event(cls, event: EngineEvent) -> _DispatchProvenance:
        values = {
            "catalog_snapshot_digest": event.catalog_snapshot_digest,
            "host_descriptor_digest": event.host_descriptor_digest,
            "reviewed_policy_digest": event.policy_version,
            "semantic_index_digest": event.semantic_index_digest,
            "work_signature": event.work_signature,
        }
        if any(not isinstance(value, str) for value in values.values()):
            raise ReleaseSkillDispatchError("committed release planning provenance is incomplete")
        return cls(**values)  # type: ignore[arg-type]


class _ClaimBoundReleaseBodySource:
    """Recheck the durable driver claim before release content is decoded."""

    def __init__(self, engine: CtxEngine, catalog: ReleasePinnedQueryCatalog) -> None:
        self._engine = engine
        self._catalog = catalog

    def load(self, request: InstallDriverRequest, material: MaterialIdentity) -> str:
        status = self._engine.install_execution_status(request.action)
        if not status.claimed or status.execution_binding_digest != request.binding.binding_digest:
            raise ReleaseSkillDispatchError(
                "release skill content requires an exact durable install claim"
            )
        return self._catalog._load_install_skill_body(  # noqa: SLF001
            self._engine,
            request,
            material,
        )


class _DispatchObservationLease:
    """Deterministic one-use lease over an already sanitized work observation."""

    def __init__(
        self,
        observation: WorkObservation,
        request: ReleaseSkillInstallRequest,
    ) -> None:
        self._surrogate = _work_observation_surrogate(observation)
        self.reference = ObservationReference(
            provider_id="release-skill-dispatch",
            opaque_id=_digest(
                {
                    "host_context_id": request.host_context_id,
                    "session_id": request.session_id,
                    "surrogate_digest": self._surrogate.value_digest,
                }
            )[:32],
            content_digest=self._surrogate.value_digest,
        )
        self._consumed = False

    def __call__(
        self,
        reference: ObservationReference,
        _state: object,
    ) -> StructuredSurrogate:
        if self._consumed or reference != self.reference:
            raise ReleaseSkillDispatchError("release work observation lease is unavailable")
        self._consumed = True
        return self._surrogate

    def close(self) -> None:
        self._consumed = True


def _work_observation_surrogate(observation: WorkObservation) -> StructuredSurrogate:
    if not isinstance(observation, WorkObservation):
        raise TypeError("observation must be a WorkObservation")
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": observation.signals,
            "languages": observation.languages,
            "baseline_capability_ids": observation.baseline_capability_ids,
            "active_capability_ids": observation.active_capability_ids,
            "rejected_capability_ids": observation.rejected_capability_ids,
            "requested_limit": observation.requested_limit,
        },
    )


class _ProbeBenefitAuditSink:
    """Validate planning without creating a durable management audit record."""

    def store(self, result: BenefitSelectionResult) -> str:
        if not isinstance(result, BenefitSelectionResult):
            raise TypeError("probe result must be a BenefitSelectionResult")
        return result.result_digest


def _scope(request: ReleaseSkillInstallRequest) -> ScopeRef:
    try:
        request._workspace_identity.assert_current()
    except WorkspaceIdentityError:
        raise ReleaseSkillDispatchError("workspace identity changed or became unsafe") from None
    workspace_digest = request._workspace_identity.digest
    session_digest = hashlib.sha256(request.session_id.encode("utf-8")).hexdigest()
    return ScopeRef(
        tenant_id="local",
        workspace_id=f"workspace-{workspace_digest}",
        repository_id=f"repository-{workspace_digest}",
        session_id=request.session_id,
        exposure_id=f"exposure-{session_digest}",
        host_context_id=request.host_context_id,
    )


def _event(
    *,
    request: ReleaseSkillInstallRequest,
    provenance: _DispatchProvenance,
    scope: ScopeRef,
    kind: str,
    expected_revision: int,
    suffix: str,
    payload: Mapping[str, object],
) -> EngineEvent:
    session_digest = hashlib.sha256(request.session_id.encode("utf-8")).hexdigest()
    event_id = f"ctx-release-install-{suffix}-{session_digest[:24]}"
    start_id = f"ctx-release-install-start-{session_digest[:24]}"
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=scope,
        expected_revision=expected_revision,
        occurred_at=request.occurred_at,
        payload=payload,
        privacy=PrivacyLabel(classification="private", retention="local"),
        correlation_id=f"ctx-release-install-{session_digest[:24]}",
        causation_id=request.host_context_id if kind == "SessionStarted" else start_id,
        engine_version=RELEASE_SKILL_DISPATCH_ENGINE_VERSION,
        planner_version=RELEASE_SKILL_DISPATCH_PLANNER_VERSION,
        policy_version=provenance.reviewed_policy_digest,
        host_descriptor_digest=provenance.host_descriptor_digest,
        catalog_snapshot_digest=provenance.catalog_snapshot_digest,
        semantic_model_digest=_digest("ctx-release-skill-lexical-v1"),
        semantic_index_digest=provenance.semantic_index_digest,
        work_signature=provenance.work_signature,
        random_seed=0,
    )


def _release_skill_host_descriptor_digest(request: ReleaseSkillInstallRequest) -> str:
    return _digest(
        {
            "host_context_id": request.host_context_id,
            "host_identity_digest": request.host_identity_digest,
            "level": "managing",
            "release_root_digest": RELEASE_QUERY_CATALOG_ROOT_SHA256,
            "schema": "ctx.release-skill-install-host-v2",
        }
    )


def _activation_action(engine: CtxEngine, scope: ScopeRef) -> HostAction | None:
    snapshot = engine.snapshot(scope)
    if snapshot.state is None:
        return None
    actions = tuple(
        pending.action
        for pending in snapshot.state.pending_effects
        if pending.effect == "activate" and pending.action.entity_id == RELEASE_INSTALL_SKILL_ID
    )
    if len(actions) > 1:
        raise ReleaseSkillDispatchError("release skill has multiple pending activations")
    return actions[0] if actions else None


def _persisted_event(
    store: SQLiteEngineStore,
    scope: ScopeRef,
    kind: str,
    *,
    consent_id: str | None = None,
) -> EngineEvent:
    event = _find_persisted_event(
        store,
        scope,
        kind,
        consent_id=consent_id,
    )
    if event is None:
        raise ReleaseSkillDispatchError(
            f"release skill journal has no exact committed {kind} event"
        )
    return event


def _find_persisted_event(
    store: SQLiteEngineStore,
    scope: ScopeRef,
    kind: str,
    *,
    consent_id: str | None = None,
) -> EngineEvent | None:
    records = tuple(store.records(StreamId.from_scope(scope)))
    matches = tuple(
        event
        for record in records
        for event in (ReplayInput.from_json(record.replay_json).reducer_event,)
        if event.kind == kind
        and (consent_id is None or event.payload.get("consent_id") == consent_id)
    )
    return matches[-1] if matches else None


def _persisted_action(
    store: SQLiteEngineStore,
    scope: ScopeRef,
    kind: str,
    *,
    consent_id: str | None = None,
    content_digest: str | None = None,
    requested_action_content_digest: str | None = None,
) -> HostAction:
    records = tuple(store.records(StreamId.from_scope(scope)))
    actions = tuple(
        action
        for record in records
        for action in Transition.from_json(record.transition_json).actions
        if action.kind == kind
        and action.entity_id == RELEASE_INSTALL_SKILL_ID
        and (consent_id is None or action.consent_id == consent_id)
        and (content_digest is None or action.content_digest == content_digest)
        and (
            requested_action_content_digest is None
            or action.payload.get("requested_action_content_digest")
            == requested_action_content_digest
        )
    )
    if not actions:
        raise ReleaseSkillDispatchError(
            f"release skill journal has no exact committed {kind} action"
        )
    return actions[-1]


def _committed_consent_directive(
    *,
    consent_request: HostAction,
    install_action: HostAction,
    selection: CapabilityPlanSelectionV3,
    descriptor: InstallPlanDescriptor,
    decision_event: EngineEvent,
) -> InstallConsentDirective:
    """Recover an authority-free directive from an exact committed decision."""

    presentation = selection.presentation
    authority = selection.authority
    payload = decision_event.payload
    requested_identity = {
        "requested_action_id": install_action.action_id,
        "requested_action_kind": install_action.kind,
        "requested_action_content_digest": install_action.content_digest,
        "requested_action_precondition_revision": install_action.precondition_revision,
    }
    decision_basis = payload.get("decision_basis")
    policy_snapshot_digest = payload.get("policy_snapshot_digest")
    if (
        not isinstance(authority, InstallPlanningAuthority)
        or descriptor != authority.descriptor
        or decision_event.kind != "UserDecision"
        or decision_event.scope != install_action.scope
        or decision_event.scope != consent_request.scope
        or decision_event.expected_revision + 1 != install_action.precondition_revision
        or consent_request.kind != "RequestConsent"
        or consent_request.consent_id is None
        or consent_request.consent_id != install_action.consent_id
        or payload.get("consent_id") != install_action.consent_id
        or any(payload.get(key) != value for key, value in requested_identity.items())
        or any(
            consent_request.payload.get(key) != value for key, value in requested_identity.items()
        )
        or install_action.entity_id != presentation.capability_id
        or install_action.source_digest != presentation.source_digest
        or install_action.catalog_snapshot_id is None
        or install_action.plan_id is None
        or consent_request.plan_id != install_action.plan_id
        or consent_request.catalog_snapshot_id != install_action.catalog_snapshot_id
        or install_action.payload.get("install_plan_descriptor") != descriptor.to_dict()
        or install_action.payload.get("result_material") != authority.result_material.to_dict()
        or install_action.payload.get("policy_snapshot_digest") != policy_snapshot_digest
        or consent_request.payload.get("policy_snapshot_digest") != policy_snapshot_digest
        or decision_basis not in {"interactive", "preapproved-policy"}
        or not isinstance(policy_snapshot_digest, str)
    ):
        raise ReleaseSkillDispatchError(
            "committed install decision lost its exact consent authority binding"
        )
    if descriptor.permission_expansion:
        reason_code = "permission-expansion-requires-consent"
    elif descriptor.credential_requirement:
        reason_code = "credentials-require-consent"
    elif decision_basis == "preapproved-policy":
        reason_code = "matching-preapproved-policy"
    else:
        reason_code = "per-install-consent-required"
    return InstallConsentDirective(
        consent_id=consent_request.consent_id,
        capability_id=presentation.capability_id,
        kind=presentation.kind,
        source_digest=presentation.source_digest,
        catalog_snapshot_digest=install_action.catalog_snapshot_id,
        plan_id=install_action.plan_id,
        install_plan_digest=descriptor.plan_digest,
        descriptor_digest=descriptor.descriptor_digest,
        installer_id=descriptor.installer_id,
        provenance_digest=descriptor.provenance_digest,
        permission_expansion=descriptor.permission_expansion,
        credential_requirement=descriptor.credential_requirement,
        decision_basis=decision_basis,
        policy_snapshot_digest=policy_snapshot_digest,
        reason_code=reason_code,
        requested_action_id=install_action.action_id,
        requested_action_kind=install_action.kind,
        requested_action_content_digest=install_action.content_digest,
        requested_action_precondition_revision=install_action.precondition_revision,
        result_material_identity_digest=authority.result_material.identity_digest,
    )


def _committed_interactive_reservation(
    *,
    decision_event: EngineEvent,
    directive: InstallConsentDirective,
    install_action: HostAction,
) -> InteractiveInstallDecisionReservation:
    """Rebuild the engine's exact reservation identity from durable facts only."""

    decision = decision_event.payload.get("decision")
    expected_payload = directive.decision_payload(decision) if isinstance(decision, str) else None
    if (
        decision not in {"granted", "denied"}
        or not directive.requires_prompt
        or expected_payload is None
        or dict(decision_event.payload) != expected_payload
        or decision_event.scope != install_action.scope
        or decision_event.expected_revision + 1 != install_action.precondition_revision
        or install_action.expires_at is None
    ):
        raise ReleaseSkillDispatchError(
            "committed interactive decision lost its exact broker reservation binding"
        )
    return InteractiveInstallDecisionReservation(
        scope=decision_event.scope,
        event_id=decision_event.event_id,
        event_content_digest=decision_event.content_digest,
        consent_id=directive.consent_id,
        decision=decision,
        policy_snapshot_digest=directive.policy_snapshot_digest,
        requested_action_id=directive.requested_action_id,
        requested_action_kind=directive.requested_action_kind,
        requested_action_content_digest=directive.requested_action_content_digest,
        requested_action_precondition_revision=(directive.requested_action_precondition_revision),
        install_expires_at=install_action.expires_at,
    )


def _install_decision_evidence_query(
    *,
    store: SQLiteEngineStore,
    decision_event: EngineEvent,
    directive: InstallConsentDirective,
    install_action: HostAction,
) -> InstallDecisionEvidenceQuery:
    """Bind broker recovery to the exact pre-decision journal head."""

    reservation = _committed_interactive_reservation(
        decision_event=decision_event,
        directive=directive,
        install_action=install_action,
    )
    return _reservation_evidence_query(store=store, reservation=reservation)


def _reservation_evidence_query(
    *,
    store: SQLiteEngineStore,
    reservation: InteractiveInstallDecisionReservation,
) -> InstallDecisionEvidenceQuery:
    expected_head_revision = reservation.requested_action_precondition_revision - 1
    records = tuple(store.records(StreamId.from_scope(reservation.scope)))
    if expected_head_revision == 0:
        expected_head_record_digest = None
    else:
        previous = next(
            (record for record in records if record.revision == expected_head_revision),
            None,
        )
        if previous is None or previous.record_digest == "":
            raise ReleaseSkillDispatchError(
                "install decision evidence lost its exact expected journal head"
            )
        expected_head_record_digest = previous.record_digest
    return InstallDecisionEvidenceQuery(
        scope=reservation.scope,
        consent_id=reservation.consent_id,
        decision=reservation.decision,
        decision_basis="interactive",
        policy_snapshot_digest=reservation.policy_snapshot_digest,
        requested_action_id=reservation.requested_action_id,
        requested_action_kind=reservation.requested_action_kind,
        requested_action_content_digest=reservation.requested_action_content_digest,
        requested_action_precondition_revision=(reservation.requested_action_precondition_revision),
        event_id=reservation.event_id,
        event_content_digest=reservation.event_content_digest,
        expected_head_revision=expected_head_revision,
        expected_head_record_digest=expected_head_record_digest,
    )


def _recovery_status(
    phase: ReleaseSkillRecoveryPhase,
    revision: int,
) -> ReleaseSkillRecoveryStatus:
    capability_id = (
        RELEASE_INSTALL_SKILL_ID
        if phase
        in {
            "planned",
            "pending-consent",
            "decision-committed",
            "install-claimed",
            "install-outcome-recorded",
            "installed-inactive",
            "terminal-denied",
            "terminal-failed",
            "active",
        }
        else None
    )
    return ReleaseSkillRecoveryStatus(
        requires_recovery=phase
        in {
            "planned",
            "pending-consent",
            "decision-committed",
            "install-claimed",
            "install-outcome-recorded",
            "installed-inactive",
        },
        phase=phase,
        revision=revision,
        capability_id=capability_id,
        release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
    )


def inspect_release_skill_recovery_status(
    request: ReleaseSkillInstallRequest,
) -> ReleaseSkillRecoveryStatus:
    """Classify exact durable lifecycle work without creating or changing state."""

    if not isinstance(request, ReleaseSkillInstallRequest):
        raise TypeError("request must be a ReleaseSkillInstallRequest")
    scope = _scope(request)
    try:
        request.journal_path.lstat()
    except FileNotFoundError:
        return _recovery_status("no-stream", 0)
    except OSError as exc:
        raise ReleaseSkillDispatchError(
            "release skill recovery journal path could not be inspected"
        ) from exc
    try:
        store = SQLiteEngineStore.open_read_only(request.journal_path)
        engine = CtxEngine(
            store=store,
            replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
        )
        snapshot = engine.snapshot(scope)
        state = snapshot.state
        if state is None:
            return _recovery_status("no-stream", 0)
        if state.scope != scope:
            raise ReleaseSkillDispatchError("committed release skill scope changed")
        capability = state.capability(RELEASE_INSTALL_SKILL_ID)
        if not isinstance(capability, CapabilityStateV3):
            phase: ReleaseSkillRecoveryPhase = (
                "no-lifecycle" if state.committed_plan is None else "abstained"
            )
            return _recovery_status(phase, snapshot.revision)
        if capability.activation == "active":
            return _recovery_status("active", snapshot.revision)
        pending_consents = tuple(
            item
            for item in state.pending_consents
            if item.install_action.entity_id == RELEASE_INSTALL_SKILL_ID
        )
        if len(pending_consents) > 1:
            raise ReleaseSkillDispatchError("release skill has multiple durable pending consents")
        if pending_consents:
            return _recovery_status("pending-consent", snapshot.revision)
        pending_installs = tuple(
            item.action
            for item in state.pending_effects
            if item.effect == "install" and item.action.entity_id == RELEASE_INSTALL_SKILL_ID
        )
        if len(pending_installs) > 1:
            raise ReleaseSkillDispatchError("release skill has multiple durable pending installs")
        if capability.installation == "installed":
            if capability.installed_lineage is None:
                raise ReleaseSkillDispatchError(
                    "installed release skill has no exact durable lineage"
                )
            return _recovery_status("installed-inactive", snapshot.revision)
        latest_decision = _find_persisted_event(store, scope, "UserDecision")
        if latest_decision is not None and latest_decision.payload.get("decision") == "denied":
            return _recovery_status("terminal-denied", snapshot.revision)
        install_action = pending_installs[0] if pending_installs else None
        if install_action is None:
            try:
                install_action = _persisted_action(store, scope, "InstallCapability")
            except ReleaseSkillDispatchError:
                install_action = None
        if install_action is None:
            return _recovery_status("planned", snapshot.revision)
        if (
            latest_decision is None
            or latest_decision.payload.get("decision") != "granted"
            or latest_decision.payload.get("consent_id") != install_action.consent_id
        ):
            raise ReleaseSkillDispatchError(
                "release skill install action has no exact committed grant"
            )
        execution = store.install_execution_status(
            StreamId.from_scope(scope),
            install_action.action_id,
        )
        if execution.settled and execution.outcome == "failed":
            return _recovery_status("terminal-failed", snapshot.revision)
        if execution.outcome_recorded:
            return _recovery_status("install-outcome-recorded", snapshot.revision)
        if execution.claimed:
            return _recovery_status("install-claimed", snapshot.revision)
        return _recovery_status("decision-committed", snapshot.revision)
    except ReleaseSkillDispatchError:
        raise
    except (CtxEngineError, OSError, TypeError, ValueError) as exc:
        raise ReleaseSkillDispatchError(
            "release skill recovery status could not be verified"
        ) from exc


def probe_release_skill_install_relevance(
    request: ReleaseSkillInstallRequest,
) -> ReleaseSkillRelevanceProbe:
    """Plan the reviewed install candidate without touching management state.

    The probe uses the same authenticated catalog, host eligibility, typed
    install authority, and net-benefit planner as dispatch.  Its audit sink is
    deliberately process-local because this result grants no authority and is
    recomputed by the durable dispatcher before any persistent action.
    """

    if not isinstance(request, ReleaseSkillInstallRequest):
        raise TypeError("request must be a ReleaseSkillInstallRequest")
    if os.name == "nt":
        raise ReleaseSkillDispatchError("release skill installation is not available on Windows")

    catalog = open_release_pinned_query_catalog()
    prepared = None
    try:
        prepared = catalog.prepare_query(
            task=request.task,
            language=request.language,
            host_policy=_InstallHostPolicy.for_request(request),
        )
        closure = prepared.closure
        observation = _work_observation_surrogate(closure.observation)
        planner = AuthenticatedReplayDecisionPlannerV3(
            planner=AuthenticatedNetBenefitPlanner(
                policy=closure.policy,
                audit_store=_ProbeBenefitAuditSink(),
            ),
            source=closure.source,
            benefit_facts_port=closure.benefit_facts,
            material_port=prepared.material_authority,
            install_bundle_port=prepared.install_authority,
            planner_version=RELEASE_SKILL_DISPATCH_PLANNER_VERSION,
            catalog_namespace_digest=closure.catalog_namespace_digest,
        )
        decision = planner(
            observation,
            None,
            PlanningContext(
                planner_version=planner.planner_version,
                catalog_snapshot_digest=planner.catalog_snapshot_digest,
            ),
        )
        raw_rows = decision.value.get("capabilities")
        if not isinstance(raw_rows, tuple):
            raise ReleaseSkillDispatchError("release relevance plan is malformed")
        relevant = any(
            isinstance(row, Mapping)
            and row.get("capability_id") == RELEASE_INSTALL_SKILL_ID
            and row.get("kind") == "skill"
            and row.get("actionability") == "install"
            for row in raw_rows
        )
        return ReleaseSkillRelevanceProbe(
            relevant=relevant,
            capability_id=RELEASE_INSTALL_SKILL_ID if relevant else None,
            release_root_digest=catalog.release_root_digest,
            observation_digest=observation.value_digest,
            planning_environment_digest=planner.catalog_snapshot_digest,
        )
    except ReleaseSkillDispatchError:
        raise
    except (CtxEngineError, OSError, TypeError, ValueError) as exc:
        raise ReleaseSkillDispatchError("release skill relevance probe failed") from exc
    finally:
        if prepared is not None:
            prepared.close()
        catalog.close()


def dispatch_release_skill_install(
    request: ReleaseSkillInstallRequest,
    *,
    consent_broker: InstallConsentBrokerService | None = None,
    decision_assertion: SignedHumanDecisionAssertion | None = None,
    trusted_utc_now: Callable[[], datetime] | None = None,
) -> ReleaseSkillDispatchResult:
    """Recommend, authorize, install, receipt, and return activation eligibility."""

    if not isinstance(request, ReleaseSkillInstallRequest):
        raise TypeError("request must be a ReleaseSkillInstallRequest")
    if consent_broker is not None and not isinstance(consent_broker, InstallConsentBrokerService):
        raise TypeError("consent_broker must be an InstallConsentBrokerService or None")
    if decision_assertion is not None and not isinstance(
        decision_assertion, SignedHumanDecisionAssertion
    ):
        raise TypeError("decision_assertion must be a SignedHumanDecisionAssertion or None")
    if consent_broker is None and decision_assertion is not None:
        raise ValueError("decision_assertion requires a consent broker")
    if os.name == "nt":
        raise ReleaseSkillDispatchError("release skill installation is not available on Windows")

    requested_scope = _scope(request)
    catalog = open_release_pinned_query_catalog()
    observation_lease: _DispatchObservationLease | None = None
    prepared = None
    try:
        store = SQLiteEngineStore(request.journal_path)
        install_policy = load_current_install_policy(request.policy_store_root)
        active_interactive_guard: InteractiveInstallDecisionGuard | None = None

        def make_engine(
            replay_factory: DefaultReplayInputFactory,
            descriptor_loader: Callable[[str, str], object | None] | None = None,
        ) -> CtxEngine:
            return CtxEngine(
                store=store,
                replay_factory=replay_factory,
                install_policy_guard=lambda digest: hold_current_install_policy(
                    digest,
                    root=request.policy_store_root,
                ),
                interactive_install_decision_guard=active_interactive_guard,
                install_descriptor_loader=descriptor_loader,  # type: ignore[arg-type]
                trusted_utc_now=trusted_utc_now,
            )

        engine = make_engine(
            DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION)
        )
        snapshot = engine.snapshot(requested_scope)
        if snapshot.state is not None and snapshot.state.scope != requested_scope:
            raise ReleaseSkillDispatchError("committed release skill scope changed")
        pending_release_consent = bool(
            snapshot.state is not None
            and any(
                item.install_action.entity_id == RELEASE_INSTALL_SKILL_ID
                for item in snapshot.state.pending_consents
            )
        )
        committed_release_decision = _find_persisted_event(
            store,
            requested_scope,
            "UserDecision",
        )
        if (
            snapshot.state is not None
            and snapshot.state.install_policy_snapshot_digest is not None
            and snapshot.state.install_policy_snapshot_digest != install_policy.policy_digest
            and (pending_release_consent or committed_release_decision is None)
        ):
            raise ReleaseSkillDispatchError(
                "install consent policy changed after the committed request"
            )

        scope = requested_scope
        provenance: _DispatchProvenance
        if snapshot.revision < 2:
            prepared = catalog.prepare_query(
                task=request.task,
                language=request.language,
                host_policy=_InstallHostPolicy.for_request(request),
            )
            observation_lease = _DispatchObservationLease(
                prepared.closure.observation,
                request,
            )
            reference = observation_lease.reference
            closure = prepared.closure
            planner = AuthenticatedReplayDecisionPlannerV3(
                planner=AuthenticatedNetBenefitPlanner(
                    policy=closure.policy,
                    audit_store=SQLiteBenefitAuditStore(request.benefit_audit_path),
                ),
                source=closure.source,
                benefit_facts_port=closure.benefit_facts,
                material_port=prepared.material_authority,
                install_bundle_port=prepared.install_authority,
                planner_version=RELEASE_SKILL_DISPATCH_PLANNER_VERSION,
                catalog_namespace_digest=closure.catalog_namespace_digest,
            )
            current_provenance = _DispatchProvenance(
                catalog_snapshot_digest=planner.catalog_snapshot_digest,
                host_descriptor_digest=_release_skill_host_descriptor_digest(request),
                reviewed_policy_digest=closure.policy_digest,
                semantic_index_digest=closure.catalog_retrieval_snapshot_digest,
                work_signature=reference.content_digest,
            )
            if snapshot.revision == 0:
                provenance = current_provenance
            else:
                started = _DispatchProvenance.from_event(
                    _persisted_event(store, scope, "SessionStarted")
                )
                if (
                    started.host_descriptor_digest,
                    started.reviewed_policy_digest,
                    started.semantic_index_digest,
                ) != (
                    current_provenance.host_descriptor_digest,
                    current_provenance.reviewed_policy_digest,
                    current_provenance.semantic_index_digest,
                ):
                    raise ReleaseSkillDispatchError(
                        "release planning authority changed after session start"
                    )
                provenance = current_provenance

            prepared_authority = prepared.install_authority

            def prepared_descriptor_loader(capability_id: str, kind: str):
                if prepared_authority is None:
                    return None
                bundle = prepared_authority.describe_bundle(capability_id, kind)
                return None if bundle is None else bundle.descriptor

            engine = make_engine(
                DefaultReplayInputFactory(
                    observation_normalizer=observation_lease,
                    decision_planner=planner,
                    reducer_version=INSTALLATION_REDUCER_VERSION,
                ),
                prepared_descriptor_loader,
            )

            def make_planning_event(
                kind: str,
                expected_revision: int,
                suffix: str,
                payload: Mapping[str, object],
            ) -> EngineEvent:
                return _event(
                    request=request,
                    provenance=provenance,
                    scope=scope,
                    kind=kind,
                    expected_revision=expected_revision,
                    suffix=suffix,
                    payload=dict(payload),
                )

            if snapshot.revision == 0:
                engine.process(
                    make_planning_event(
                        "SessionStarted",
                        0,
                        "start",
                        {"host_level": "managing"},
                    )
                )
                snapshot = engine.snapshot(scope)
            if snapshot.revision == 1:
                engine.process(
                    make_planning_event(
                        "IntentObserved",
                        1,
                        "intent",
                        {
                            "observation_ref": {
                                "provider_id": reference.provider_id,
                                "opaque_id": reference.opaque_id,
                                "content_digest": reference.content_digest,
                            }
                        },
                    )
                )
                snapshot = engine.snapshot(scope)
        else:
            provenance = _DispatchProvenance.from_event(
                _persisted_event(store, scope, "IntentObserved")
            )

        state = snapshot.state
        capability = None if state is None else state.capability(RELEASE_INSTALL_SKILL_ID)
        if not isinstance(capability, CapabilityStateV3):
            return ReleaseSkillDispatchResult(
                status="abstained",
                capability_id=None,
                release_root_digest=catalog.release_root_digest,
            )
        selection = capability.selection.selection
        authority = selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            raise ReleaseSkillDispatchError("selected release skill has no install authority")

        def durable_descriptor_loader(capability_id: str, kind: str):
            if (
                capability_id == selection.presentation.capability_id
                and kind == selection.presentation.kind
            ):
                return authority.descriptor
            return None

        engine = make_engine(
            DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
            durable_descriptor_loader,
        )
        snapshot = engine.snapshot(scope)

        def make_event(
            kind: str,
            expected_revision: int,
            suffix: str,
            payload: Mapping[str, object],
        ) -> EngineEvent:
            return _event(
                request=request,
                provenance=provenance,
                scope=scope,
                kind=kind,
                expected_revision=expected_revision,
                suffix=suffix,
                payload=dict(payload),
            )

        state = snapshot.state
        assert state is not None
        pending_consents = tuple(
            item
            for item in state.pending_consents
            if item.install_action.entity_id == RELEASE_INSTALL_SKILL_ID
        )
        pending_installs = tuple(
            item.action
            for item in state.pending_effects
            if item.effect == "install" and item.action.entity_id == RELEASE_INSTALL_SKILL_ID
        )
        if len(pending_consents) > 1 or len(pending_installs) > 1:
            raise ReleaseSkillDispatchError(
                "release skill has multiple durable install lifecycle phases"
            )
        if (
            not pending_consents
            and not pending_installs
            and capability.installed_lineage is None
            and state.install_policy_snapshot_digest is None
        ):
            reassessment = engine.process(
                make_event(
                    "ReassessmentRequested",
                    snapshot.revision,
                    "desired",
                    {
                        "owner_id": f"release-install:{request.host_context_id}",
                        "policy_snapshot_digest": install_policy.policy_digest,
                        "desired_capabilities": [
                            {
                                "capability_id": selection.presentation.capability_id,
                                "source_digest": selection.presentation.source_digest,
                                "lease_id": f"release-install:{request.session_id}",
                                "kind": selection.presentation.kind,
                                "actionability": selection.presentation.actionability,
                                "install_descriptor_digest": (
                                    selection.presentation.install_descriptor_digest
                                ),
                                "install_plan_digest": (selection.presentation.install_plan_digest),
                            }
                        ],
                    },
                )
            )
            requests = tuple(
                action for action in reassessment.actions if action.kind == "RequestConsent"
            )
            if len(requests) != 1:
                raise ReleaseSkillDispatchError(
                    "release skill did not emit one exact consent request"
                )
            snapshot = engine.snapshot(scope)
            state = snapshot.state
            assert state is not None
            pending_consents = tuple(
                item
                for item in state.pending_consents
                if item.install_action.entity_id == RELEASE_INSTALL_SKILL_ID
            )
            if len(pending_consents) != 1:
                raise ReleaseSkillDispatchError(
                    "release skill reassessment lost its exact pending consent"
                )

        if pending_consents:
            install_action = pending_consents[0].install_action
            awaiting_consent = True
        elif pending_installs:
            install_action = pending_installs[0]
            awaiting_consent = False
        elif capability.installed_lineage is not None:
            install_action = _persisted_action(
                store,
                scope,
                "InstallCapability",
                content_digest=capability.installed_lineage.install_action_content_digest,
            )
            awaiting_consent = False
        else:
            install_action = _persisted_action(store, scope, "InstallCapability")
            awaiting_consent = False
        consent_request = _persisted_action(
            store,
            scope,
            "RequestConsent",
            consent_id=install_action.consent_id,
            requested_action_content_digest=install_action.content_digest,
        )

        committed_decision_event = (
            None
            if awaiting_consent
            else _persisted_event(
                store,
                scope,
                "UserDecision",
                consent_id=install_action.consent_id,
            )
        )
        if committed_decision_event is not None:
            directive = _committed_consent_directive(
                consent_request=consent_request,
                install_action=install_action,
                selection=selection,
                descriptor=authority.descriptor,
                decision_event=committed_decision_event,
            )
        elif has_persisted_install_policy(request.policy_store_root):
            with hold_current_install_policy(
                install_policy.policy_digest,
                root=request.policy_store_root,
            ) as held_policy:
                directive = route_install_consent_request(
                    consent_request,
                    selection,
                    authority.descriptor,
                    held_policy.policy,
                )
                held_policy.assert_current()
        else:
            directive = route_install_consent_request(
                consent_request,
                selection,
                authority.descriptor,
                install_policy,
            )
            if not directive.requires_prompt:
                raise ReleaseSkillDispatchError(
                    "automatic installation requires an explicitly persisted policy"
                )

        runtime = SkillCasRuntimeConfig(
            skill_store_root=request.skill_store_root,
            body_source=_ClaimBoundReleaseBodySource(engine, catalog),
            installer_id=authority.descriptor.installer_id,
            host_identity_digest=request.host_identity_digest,
        )
        driver_registry = InstallDriverRegistry(
            (runtime.registration(driver_digest=INSTALLER_DIGEST),)
        )
        trusted_registration = driver_registry.resolve(install_action, authority.descriptor)

        decision: Literal["granted", "denied"]
        if consent_broker is not None and directive.requires_prompt:
            challenge = consent_broker.prepare(
                directive=directive,
                selection=selection,
                install_action=install_action,
                execution_binding=trusted_registration.binding,
            )
            _require_exact_broker_challenge(
                challenge=challenge,
                directive=directive,
                install_action=install_action,
                execution_binding_digest=trusted_registration.binding.binding_digest,
                selection_digest=install_consent_selection_digest(selection),
                audience=consent_broker.audience,
                workspace_identity_digest=request._workspace_identity.digest,
                release_root_digest=catalog.release_root_digest,
            )
            challenge_projection = _safe_consent_challenge(challenge)
            broker_record = consent_broker.status(challenge.challenge_id)
            if broker_record.challenge != challenge:
                raise ReleaseSkillDispatchError(
                    "broker status does not match the exact prepared challenge"
                )
            if broker_record.state != "settled" and committed_decision_event is not None:
                if decision_assertion is not None:
                    raise ReleaseSkillDispatchError(
                        "committed install consent cannot accept another assertion"
                    )
                reservation = _committed_interactive_reservation(
                    decision_event=committed_decision_event,
                    directive=directive,
                    install_action=install_action,
                )
                evidence_query = _install_decision_evidence_query(
                    store=store,
                    decision_event=committed_decision_event,
                    directive=directive,
                    install_action=install_action,
                )
                reconciliation = consent_broker.reconcile_install_decision(
                    query=evidence_query,
                    reservation=reservation,
                )
                if reconciliation.outcome != "settled":
                    raise ReleaseSkillDispatchError(
                        "committed install decision reconciliation was quarantined as "
                        f"{reconciliation.journal_status}"
                    )
                broker_record = reconciliation.record
            elif broker_record.state == "decision-ready":
                persisted_decision = broker_record.decision
                if persisted_decision not in {"granted", "denied"}:
                    raise ReleaseSkillDispatchError(
                        "decision-ready broker consent has no authenticated decision"
                    )
                prospective_event = make_event(
                    "UserDecision",
                    install_action.precondition_revision - 1,
                    "decision",
                    directive.decision_payload(persisted_decision),
                )
                reservation = _committed_interactive_reservation(
                    decision_event=prospective_event,
                    directive=directive,
                    install_action=install_action,
                )
                evidence_query = _reservation_evidence_query(
                    store=store,
                    reservation=reservation,
                )
                reconciliation = consent_broker.reconcile_install_decision(
                    query=evidence_query,
                    reservation=reservation,
                )
                if reconciliation.outcome == "quarantined":
                    raise ReleaseSkillDispatchError(
                        "decision-ready install reconciliation was quarantined as "
                        f"{reconciliation.journal_status}"
                    )
                broker_record = reconciliation.record
                if reconciliation.outcome == "settled":
                    committed_decision_event = _persisted_event(
                        store,
                        scope,
                        "UserDecision",
                        consent_id=install_action.consent_id,
                    )
            elif broker_record.state == "reserved":
                persisted_decision = broker_record.decision
                if (
                    persisted_decision not in {"granted", "denied"}
                    or broker_record.reservation_event_id is None
                    or broker_record.reservation_event_content_digest is None
                    or install_action.expires_at is None
                ):
                    raise ReleaseSkillDispatchError(
                        "reserved broker consent has no exact decision reservation"
                    )
                reservation = InteractiveInstallDecisionReservation(
                    scope=install_action.scope,
                    event_id=broker_record.reservation_event_id,
                    event_content_digest=(broker_record.reservation_event_content_digest),
                    consent_id=directive.consent_id,
                    decision=persisted_decision,
                    policy_snapshot_digest=directive.policy_snapshot_digest,
                    requested_action_id=directive.requested_action_id,
                    requested_action_kind=directive.requested_action_kind,
                    requested_action_content_digest=(directive.requested_action_content_digest),
                    requested_action_precondition_revision=(
                        directive.requested_action_precondition_revision
                    ),
                    install_expires_at=install_action.expires_at,
                )
                evidence_query = _reservation_evidence_query(
                    store=store,
                    reservation=reservation,
                )
                reconciliation = consent_broker.reconcile_install_decision(
                    query=evidence_query,
                    reservation=reservation,
                )
                if reconciliation.outcome == "quarantined":
                    raise ReleaseSkillDispatchError(
                        "reserved install decision reconciliation was quarantined as "
                        f"{reconciliation.journal_status}"
                    )
                broker_record = reconciliation.record
                if reconciliation.outcome == "settled":
                    committed_decision_event = _persisted_event(
                        store,
                        scope,
                        "UserDecision",
                        consent_id=install_action.consent_id,
                    )
            if broker_record.state == "settled":
                if decision_assertion is not None:
                    raise ReleaseSkillDispatchError(
                        "settled install consent cannot accept another assertion"
                    )
                if awaiting_consent or committed_decision_event is None:
                    raise ReleaseSkillDispatchError(
                        "settled install consent has no exact committed decision"
                    )
                reservation = _committed_interactive_reservation(
                    decision_event=committed_decision_event,
                    directive=directive,
                    install_action=install_action,
                )
                persisted_decision = committed_decision_event.payload.get("decision")
                if (
                    broker_record.decision != persisted_decision
                    or broker_record.reservation_event_id != reservation.event_id
                    or broker_record.reservation_event_content_digest
                    != reservation.event_content_digest
                ):
                    raise ReleaseSkillDispatchError(
                        "settled broker consent does not match the exact committed decision"
                    )
                decision = persisted_decision  # type: ignore[assignment]
            elif broker_record.state in {
                "pending",
                "decision-ready",
                "reauthentication-required",
            }:
                if decision_assertion is None:
                    return ReleaseSkillDispatchResult(
                        status="consent-required",
                        capability_id=selection.presentation.capability_id,
                        release_root_digest=catalog.release_root_digest,
                        consent=directive,
                        challenge=challenge_projection,
                    )
                authorization = consent_broker.authenticate(challenge, decision_assertion)
                authenticated_decision = authorization.decision.decision
                if authenticated_decision not in {"granted", "denied"}:
                    raise ReleaseSkillDispatchError("authenticated install decision is malformed")
                active_interactive_guard = consent_broker.interactive_guard(
                    authorization,
                    execution_binding=trusted_registration.binding,
                )
                engine = make_engine(
                    DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
                    durable_descriptor_loader,
                )
                snapshot = engine.snapshot(scope)
                decision = authenticated_decision  # type: ignore[assignment]
                engine.process(
                    make_event(
                        "UserDecision",
                        snapshot.revision,
                        "decision",
                        directive.decision_payload(decision),
                    )
                )
                snapshot = engine.snapshot(scope)
            else:
                raise ReleaseSkillDispatchError(
                    f"broker install consent is not resumable from {broker_record.state}"
                )
        elif awaiting_consent:
            if decision_assertion is not None:
                raise ReleaseSkillDispatchError(
                    "preapproved installation cannot accept a decision assertion"
                )
            if directive.requires_prompt:
                return ReleaseSkillDispatchResult(
                    status="consent-required",
                    capability_id=selection.presentation.capability_id,
                    release_root_digest=catalog.release_root_digest,
                    consent=directive,
                )
            decision = "granted"
            engine.process(
                make_event(
                    "UserDecision",
                    snapshot.revision,
                    "decision",
                    directive.decision_payload(decision),
                )
            )
            snapshot = engine.snapshot(scope)
        else:
            if decision_assertion is not None:
                raise ReleaseSkillDispatchError(
                    "committed installation cannot accept a decision assertion"
                )
            assert committed_decision_event is not None
            persisted_decision = committed_decision_event.payload.get("decision")
            if persisted_decision not in {"granted", "denied"}:
                raise ReleaseSkillDispatchError("committed install decision is malformed")
            if directive.requires_prompt:
                raise ReleaseSkillDispatchError(
                    "interactive install continuation requires a consent broker"
                )
            decision = persisted_decision  # type: ignore[assignment]
        if decision == "denied":
            return ReleaseSkillDispatchResult(
                status="denied",
                capability_id=selection.presentation.capability_id,
                release_root_digest=catalog.release_root_digest,
                consent=directive,
            )

        report = prepare_install_execution(
            engine=engine,
            action=install_action,
            selection=selection,
            descriptor=authority.descriptor,
            expected_catalog_snapshot_digest=provenance.catalog_snapshot_digest,
            expected_policy_digest=directive.policy_snapshot_digest,
            registry=driver_registry,
        ).execute()
        if report.outcome != "applied":
            return ReleaseSkillDispatchResult(
                status=report.outcome,
                capability_id=selection.presentation.capability_id,
                release_root_digest=catalog.release_root_digest,
                consent=directive,
                install_action_content_digest=install_action.content_digest,
            )

        installed_state = engine.snapshot(scope).state
        installed = (
            None
            if installed_state is None
            else installed_state.capability(selection.presentation.capability_id)
        )
        if not isinstance(installed, CapabilityStateV3) or installed.installation != "installed":
            raise ReleaseSkillDispatchError("applied install has no installed projection")
        lineage = installed.installed_lineage
        activation = _activation_action(engine, scope)
        if activation is None:
            activation = _persisted_action(store, scope, "ActivateCapability")
        if lineage is None:
            raise ReleaseSkillDispatchError(
                "installed release skill has no lineage-bound activation action"
            )
        return ReleaseSkillDispatchResult(
            status="installed",
            capability_id=selection.presentation.capability_id,
            release_root_digest=catalog.release_root_digest,
            consent=directive,
            install_action_content_digest=install_action.content_digest,
            install_receipt_content_digest=lineage.install_receipt_content_digest,
            installed_lineage_digest=lineage.lineage_digest,
            activation_action_content_digest=activation.content_digest,
        )
    except ReleaseSkillDispatchError:
        raise
    except (
        ConsentBrokerError,
        CtxEngineError,
        InstallConsentEvidenceProviderUnavailable,
        InstallExecutionError,
        OSError,
        ValueError,
    ) as exc:
        raise ReleaseSkillDispatchError(str(exc)) from exc
    finally:
        if prepared is not None:
            prepared.close()
        if observation_lease is not None:
            observation_lease.close()
        catalog.close()


__all__ = [
    "RELEASE_SKILL_DISPATCH_ENGINE_VERSION",
    "RELEASE_SKILL_DISPATCH_PLANNER_VERSION",
    "ReleaseSkillConsentChallengeProjection",
    "ReleaseSkillDispatchError",
    "ReleaseSkillDispatchResult",
    "ReleaseSkillInstallRequest",
    "ReleaseSkillRecoveryPhase",
    "ReleaseSkillRecoveryStatus",
    "ReleaseSkillRelevanceProbe",
    "dispatch_release_skill_install",
    "inspect_release_skill_recovery_status",
    "probe_release_skill_install_relevance",
]
