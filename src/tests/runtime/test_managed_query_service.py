from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import pickle
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import networkx as nx
import pytest

import ctx.runtime as ctx_runtime
import ctx.runtime.managed_query_service as managed_query_service_module
from ctx.core.graph.graph_store import build_graph_store
from ctx.core.install_consent_broker_store import (
    ConsentBrokerDecisionRejected,
    ConsentBrokerExpired,
    HumanDecisionVerifier,
    SQLiteInstallConsentBrokerStore,
    SignedHumanDecisionAssertion,
)
from ctx.core.install_policy_store import persist_install_policy
from ctx.core.resolve.engine_candidates import IndexedGraphCandidateSource
from ctx.engine.benefit import BenefitCandidate, EvidenceSummary, NetBenefitPolicy, ResourceCosts
from ctx.engine.content import MaterialDescriptor, MaterialIdentity
from ctx.engine.engine import CtxEngine
from ctx.engine.installation import (
    InstallConsentPolicy,
    InstallExecutionBinding,
    InstallPlanDescriptor,
    InstallPlanningBundle,
    PreparedInstallPlan,
)
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3
from ctx.engine.planner import CapabilityCandidate, WorkObservation
from ctx.engine.protocol import (
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_RECEIPT_SCHEMA_V3,
    EngineEvent,
    HostAction,
    ScopeRef,
    Transition,
)
from ctx.engine.replay import ObservationReference, ReplayInput, StructuredSurrogate
from ctx.engine.state import CommittedPlanV3, EngineState
from ctx.engine.store import SQLiteEngineStore, StreamId
from ctx.runtime.composition import (
    EngineComposition,
    open_engine_composition,
    open_managed_engine_composition,
)
from ctx.runtime.managed_artifact_registry import (
    ManagedArtifactHandle,
    ManagedArtifactRegistry,
    open_managed_artifact_registry,
)
from ctx.runtime.managed_query_service import (
    ManagedConsentResolutionResult,
    ManagedConsentChallengeProjection,
    ManagedDesiredSetBusyError,
    ManagedDesiredSetConflictError,
    ManagedDesiredSetRequest,
    ManagedDesiredSetResult,
    ManagedDesiredSetSupersededError,
    ManagedQueryHeadDriftError,
    ManagedQueryInput,
    ManagedQueryRequest,
    ManagedQueryService,
    ManagedQueryServiceError,
    ManagedQueryServiceResult,
    ManagedQuerySupersededError,
    open_managed_query_service,
)
from ctx.runtime.install_consent_broker import InstallConsentBrokerService
from ctx.runtime.install_consent_authenticators import (
    HumanDecisionVerifierRegistration,
    TrustedHumanDecisionVerifierRegistry,
    UnknownHumanDecisionVerifier,
    encode_signed_human_decision_assertion,
)
from ctx.runtime.skill_cas import SkillCasRuntimeConfig
from ctx.runtime.managed_query_store import (
    ManagedQueryRecord,
    ManagedQueryStore,
    ManagedQueryStoreConflict,
    open_managed_query_store,
)


NOW = "2026-08-03T12:00:00Z"
CATALOG_NAMESPACE_DIGEST = hashlib.sha256(b"managed-service-catalog").hexdigest()
STORE_KEY = hashlib.sha256(b"managed-service-store-key").digest()
WORKSPACE_DIGEST = hashlib.sha256(b"managed-service-workspace").hexdigest()
RELEASE_ROOT_DIGEST = hashlib.sha256(b"managed-service-release-root").hexdigest()
CONSENT_AUDIENCE = "ctx-managed-query"
INSTALLABLE_SKILL_BODY = (
    "---\nname: installable\ndescription: managed service install fixture\n---\n"
    "Use the managed service.\n"
)
CONSENT_TEST_SECRET = b"managed-query-consent-test-key"


class _ConsentTestVerifier(HumanDecisionVerifier):
    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        return hmac.compare_digest(
            assertion.proof,
            hmac.digest(CONSENT_TEST_SECRET, signing_bytes, "sha256"),
        )


def test_runtime_package_exports_managed_desired_set_contract() -> None:
    assert ctx_runtime.ManagedDesiredSetRequest is ManagedDesiredSetRequest
    assert ctx_runtime.ManagedDesiredSetResult is ManagedDesiredSetResult
    assert ctx_runtime.ManagedDesiredSetBusyError is ManagedDesiredSetBusyError
    assert ctx_runtime.ManagedDesiredSetConflictError is ManagedDesiredSetConflictError
    assert ctx_runtime.ManagedDesiredSetSupersededError is ManagedDesiredSetSupersededError
    assert ctx_runtime.ManagedConsentResolutionResult is ManagedConsentResolutionResult
    assert "ManagedConsentResolutionResult" in managed_query_service_module.__all__


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id="host-neutral",
    )


@dataclass
class _Facts:
    benefit_facts_snapshot_digest: str = field(
        default_factory=lambda: _digest("managed-service-facts")
    )
    calls: list[str] = field(default_factory=list)

    def benefit_candidate(
        self,
        presentation: CapabilityCandidate,
        _observation: WorkObservation,
    ) -> BenefitCandidate:
        self.calls.append(presentation.capability_id)
        return BenefitCandidate(
            capability_id=presentation.capability_id,
            source_digest=presentation.source_digest,
            resource_profile_digest=_digest(f"profile:{presentation.capability_id}"),
            availability=("advisory" if presentation.actionability == "manual" else "executable"),
            expected_task_benefit_ppm=800_000,
            relevance_ppm=900_000,
            trust_ppm=1_000_000,
            costs=ResourceCosts(),
            evidence=EvidenceSummary(
                capability_id=presentation.capability_id,
                kind=presentation.kind,
                source_digest=presentation.source_digest,
                evidence_window_digest=_digest(f"window:{presentation.capability_id}"),
                opportunity_observable=False,
            ),
            source_trusted=True,
            security_approved=True,
            permissions_allowed=True,
            credentials_available=True,
        )


@dataclass
class _MaterialPort:
    material_snapshot_digest: str = field(
        default_factory=lambda: _digest("managed-service-materials")
    )

    def describe(self, capability_id: str, kind: str) -> MaterialDescriptor:
        return MaterialDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            actionability="manual",
            content_sha256=None,
            content_bytes=0,
            estimated_tokens=0,
            provenance_digest=self.material_snapshot_digest,
            material_identity_digest=None,
        )

    def prepare(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("service planning must not prepare material")


@dataclass
class _InstallPort:
    installation_snapshot_digest: str = field(
        default_factory=lambda: _digest("managed-service-installs")
    )
    bundles: dict[str, InstallPlanningBundle] = field(default_factory=dict)

    def describe_bundle(self, capability_id: str, _kind: str) -> InstallPlanningBundle | None:
        return self.bundles.get(capability_id)

    def describe(self, capability_id: str, _kind: str) -> InstallPlanDescriptor | None:
        bundle = self.bundles.get(capability_id)
        return None if bundle is None else bundle.descriptor

    def prepare(self, *_args: object, **_kwargs: object) -> PreparedInstallPlan:
        raise AssertionError("service planning must not prepare installation")


@dataclass
class _InputAuthority:
    values: dict[str, ManagedQueryInput]
    calls: list[str] = field(default_factory=list)

    def resolve(self, current_work_ref: str) -> ManagedQueryInput:
        self.calls.append(current_work_ref)
        try:
            return self.values[current_work_ref]
        except KeyError:
            raise LookupError("current-work reference is unavailable") from None


@dataclass
class _BodySource:
    load_calls: int = 0
    fail_loads_remaining: int = 0

    def load(self, *_args: object, **_kwargs: object) -> str:
        self.load_calls += 1
        if self.fail_loads_remaining:
            self.fail_loads_remaining -= 1
            raise RuntimeError("simulated trusted body-source failure")
        return INSTALLABLE_SKILL_BODY


@dataclass(frozen=True)
class _Setup:
    registry: ManagedArtifactRegistry
    artifact: ManagedArtifactHandle
    store: ManagedQueryStore
    store_path: Path
    journal_path: Path
    audit_path: Path
    policy_root: Path
    facts: _Facts
    policy: NetBenefitPolicy
    materials: _MaterialPort
    installs: _InstallPort
    inputs: _InputAuthority
    body_source: _BodySource
    skill_runtime: SkillCasRuntimeConfig
    consent_broker: InstallConsentBrokerService
    consent_store_path: Path
    verifier_registry: TrustedHumanDecisionVerifierRegistry
    managed_input: ManagedQueryInput
    service: ManagedQueryService
    clock: Callable[[], datetime]


def _surrogate(requested_limit: int) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": ["python"],
            "languages": ["python"],
            "baseline_capability_ids": [],
            "active_capability_ids": [],
            "rejected_capability_ids": [],
            "requested_limit": requested_limit,
        },
    )


def _install_bundle(
    capability_id: str,
    snapshot_digest: str,
    *,
    permission_expansion: bool = False,
    credential_requirement: bool = False,
) -> InstallPlanningBundle:
    kind = capability_id.split(":", 1)[0]
    material = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=hashlib.sha256(INSTALLABLE_SKILL_BODY.encode("utf-8")).hexdigest(),
        content_bytes=len(INSTALLABLE_SKILL_BODY.encode("utf-8")),
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id=f"ctx-{kind}-installer-v1",
        plan_digest=_digest(f"install-plan:{capability_id}"),
        provenance_digest=snapshot_digest,
        permission_expansion=permission_expansion,
        credential_requirement=credential_requirement,
        result_material_identity_digest=material.identity_digest,
    )
    return InstallPlanningBundle(descriptor=descriptor, result_material=material)


def _events(artifact: ManagedArtifactHandle) -> tuple[EngineEvent, EngineEvent]:
    scope = _scope()
    common = {
        "scope": scope,
        "occurred_at": NOW,
        "correlation_id": "plan-initial",
        "engine_version": "ctx-engine-v1",
        "planner_version": artifact.planning_schema_version,
        "policy_version": "policy-v1",
        "host_descriptor_digest": _digest("host-neutral-managing"),
        "catalog_snapshot_digest": artifact.planning_environment_digest,
        "semantic_model_digest": _digest("semantic-model-disabled"),
        "semantic_index_digest": _digest("semantic-index-disabled"),
        "work_signature": _digest("managed-service-work"),
        "random_seed": 0,
    }
    started = EngineEvent(
        event_id="event-session-started",
        kind="SessionStarted",
        expected_revision=0,
        payload={"host_level": "managing"},
        causation_id="cause-session",
        **common,  # type: ignore[arg-type]
    )
    reference = artifact.observation_reference
    intent = EngineEvent(
        event_id="event-intent-observed",
        kind="IntentObserved",
        expected_revision=1,
        payload={
            "observation_ref": {
                "provider_id": reference.provider_id,
                "opaque_id": reference.opaque_id,
                "content_digest": reference.content_digest,
            }
        },
        causation_id=started.event_id,
        **common,  # type: ignore[arg-type]
    )
    return started, intent


def _setup(
    tmp_path: Path,
    *,
    requested_limit: int = 5,
    installable: bool = False,
    installable_count: int = 1,
    permission_expansion: bool = False,
    credential_requirement: bool = False,
    trusted_now: datetime = datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    trusted_clock: Callable[[], datetime] | None = None,
) -> _Setup:
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    capabilities = (
        tuple(
            "skill:installable" if index == 0 else f"skill:installable-{index + 1}"
            for index in range(installable_count)
        )
        if installable
        else (
            "agent:reviewer",
            "harness:python-runner",
            "mcp-server:docs",
            "skill:lint",
            "skill:test",
            "skill:types",
        )
    )
    graph = nx.Graph()
    for capability_id in capabilities:
        kind, name = capability_id.split(":", 1)
        graph.add_node(capability_id, label=name, type=kind, tags=["python"])
    graph_path = tmp_path / "graph.sqlite3"
    build_graph_store(graph_path, graph)
    graph_path.chmod(0o400)
    graph_digest = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    facts = _Facts()
    policy = NetBenefitPolicy(
        calibration_digest=_digest("managed-service-calibration"),
        minimum_relevance_ppm=1,
    )
    materials = _MaterialPort()
    installs = _InstallPort()
    if installable:
        for capability_id in capabilities:
            installs.bundles[capability_id] = _install_bundle(
                capability_id,
                installs.installation_snapshot_digest,
                permission_expansion=permission_expansion,
                credential_requirement=credential_requirement,
            )
    surrogate = _surrogate(requested_limit)
    with IndexedGraphCandidateSource(
        graph_path,
        graph_digest,
        material_port=materials,  # type: ignore[arg-type]
        install_plan_port=installs,
    ) as source:
        retrieval_digest = source.catalog_snapshot_digest

    def normalizer(
        _reference: ObservationReference,
        _state: EngineState | None,
    ) -> StructuredSurrogate:
        return surrogate

    with open_engine_composition(
        graph_artifact_path=graph_path,
        graph_artifact_sha256=graph_digest,
        journal_path=tmp_path / "identity-journal.sqlite3",
        observation_normalizer=normalizer,
        benefit_facts_port=facts,
        net_benefit_policy=policy,
        catalog_namespace_digest=CATALOG_NAMESPACE_DIGEST,
        benefit_audit_path=tmp_path / "identity-audit.sqlite3",
        material_port=materials,  # type: ignore[arg-type]
        install_bundle_port=installs,
        planner_version="ctx-managed-service-planner-v3",
    ) as identity:
        planning_environment_digest = identity.catalog_snapshot_digest

    registry = open_managed_artifact_registry(root=tmp_path / "registry")
    artifact = registry.ingest_graph_store(
        graph_store_path=graph_path,
        expected_graph_artifact_digest=graph_digest,
        planning_environment_digest=planning_environment_digest,
        catalog_namespace_digest=CATALOG_NAMESPACE_DIGEST,
        catalog_retrieval_digest=retrieval_digest,
        benefit_facts_snapshot_digest=facts.benefit_facts_snapshot_digest,
        benefit_policy_snapshot_digest=policy.policy_digest,
        material_snapshot_digest=materials.material_snapshot_digest,
        installation_snapshot_digest=installs.installation_snapshot_digest,
        observation_surrogate=surrogate,
        planning_schema_version="ctx-managed-service-planner-v3",
    )
    started, intent = _events(artifact)
    managed_input = ManagedQueryInput(
        artifact=artifact,
        session_started=started,
        decision_event=intent,
    )
    inputs = _InputAuthority(values={"work-one": managed_input})
    store_path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=store_path, installation_hmac_key=STORE_KEY)
    journal_path = (tmp_path / "private" / "journal.sqlite3").absolute()
    audit_path = (tmp_path / "private" / "audit.sqlite3").absolute()
    policy_root = (tmp_path / "private" / "install-policy").absolute()
    consent_store_path = (tmp_path / "private" / "consent.sqlite3").absolute()
    clock = trusted_clock or (lambda: trusted_now)
    consent_broker = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(
            consent_store_path,
            audience=CONSENT_AUDIENCE,
        ),
        verifier=None,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=clock,
    )
    verifier_registry = TrustedHumanDecisionVerifierRegistry(
        (
            HumanDecisionVerifierRegistration(
                audience=CONSENT_AUDIENCE,
                authenticator_id="managed-test-authenticator",
                principal_digest=_digest("managed-test-principal"),
                verifier=_ConsentTestVerifier(),
            ),
        )
    )
    skill_root = tmp_path / "private" / "skills"
    skill_root.mkdir(mode=0o700)
    body_source = _BodySource()
    skill_runtime = SkillCasRuntimeConfig(
        skill_store_root=skill_root,
        body_source=body_source,
        installer_id="ctx-skill-installer-v1",
        host_identity_digest=_digest("managed-service-host"),
    )
    service = open_managed_query_service(
        registry=registry,
        query_store=store,
        journal_path=journal_path,
        benefit_audit_path=audit_path,
        benefit_facts_port=facts,
        net_benefit_policy=policy,
        material_port=materials,  # type: ignore[arg-type]
        install_bundle_port=installs,
        input_authority=inputs,
        consent_broker=consent_broker,
        policy_store_root=policy_root,
        trusted_utc_now=clock,
        verifier_registry=verifier_registry,
        skill_cas_runtime=skill_runtime,
    )
    return _Setup(
        registry=registry,
        artifact=artifact,
        store=store,
        store_path=store_path,
        journal_path=journal_path,
        audit_path=audit_path,
        policy_root=policy_root,
        facts=facts,
        policy=policy,
        materials=materials,
        installs=installs,
        inputs=inputs,
        body_source=body_source,
        skill_runtime=skill_runtime,
        consent_broker=consent_broker,
        consent_store_path=consent_store_path,
        verifier_registry=verifier_registry,
        managed_input=managed_input,
        service=service,
        clock=clock,
    )


def _register(setup: _Setup, logical_query_id: str) -> ManagedQueryRecord:
    value = setup.managed_input
    return setup.store.register(
        logical_query_id=logical_query_id,
        session_started=value.session_started,
        decision_event=value.decision_event,
        artifact_manifest_digest=value.artifact.manifest_digest,
        planning_environment_digest=value.artifact.planning_environment_digest,
    )


def _open_peer_service(setup: _Setup) -> ManagedQueryService:
    return open_managed_query_service(
        registry=setup.registry,
        query_store=setup.store,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        input_authority=setup.inputs,
        consent_broker=setup.consent_broker,
        policy_store_root=setup.policy_root,
        trusted_utc_now=lambda: datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        verifier_registry=setup.verifier_registry,
        skill_cas_runtime=setup.skill_runtime,
    )


def _prepare_public_consent(
    setup: _Setup,
    *,
    label: str = "signed-consent",
) -> ManagedDesiredSetResult:
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest(f"{label}:query"),
            current_work_ref="work-one",
        )
    )
    capability_id = prepared.prepared.selections[0].capability_id
    result = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest(f"{label}:desired"),
            capability_ids=(capability_id,),
        )
    )
    assert result.status == "consent-required"
    assert result.challenge is not None
    return result


def _consent_assertion_payload(
    challenge: ManagedConsentChallengeProjection,
    *,
    decision: str = "granted",
    nonce: str = "managed-consent-nonce-1",
    principal_digest: str | None = None,
    authenticator_id: str = "managed-test-authenticator",
    audience: str = CONSENT_AUDIENCE,
    issued_at: str = "2026-08-03T12:00:00Z",
    expires_at: str = "2026-08-03T12:30:00Z",
    valid_proof: bool = True,
) -> bytes:
    unsigned = SignedHumanDecisionAssertion(
        challenge_digest=challenge.challenge_digest,
        decision=decision,
        principal_digest=principal_digest or _digest("managed-test-principal"),
        authenticator_id=authenticator_id,
        audience=audience,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
        proof=b"unsigned",
    )
    assertion = replace(
        unsigned,
        proof=(
            hmac.digest(CONSENT_TEST_SECRET, unsigned.signing_bytes(), "sha256")
            if valid_proof
            else b"invalid-proof"
        ),
    )
    return encode_signed_human_decision_assertion(assertion)


def _commit_pending_consent(
    setup: _Setup,
    *,
    logical_query_id: str = "pending-consent-query",
) -> tuple[ManagedQueryServiceResult, HostAction]:
    initial = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest(logical_query_id),
            current_work_ref="work-one",
        )
    )
    original = setup.managed_input.decision_event
    with open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.artifact,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        policy_store_root=setup.policy_root,
        skill_cas_runtime=setup.skill_runtime,
    ) as composition:
        snapshot = composition.snapshot(original.scope)
        assert snapshot.state is not None
        committed = snapshot.state.committed_plan
        assert isinstance(committed, CommittedPlanV3)
        capability = committed.capabilities[0]
        desired = composition.process(
            replace(
                original,
                event_id="event-managed-consent-desired",
                kind="ReassessmentRequested",
                expected_revision=2,
                payload={
                    "owner_id": "owner-managed-consent",
                    "policy_snapshot_digest": InstallConsentPolicy.safe_default().policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability.capability_id,
                            "source_digest": capability.source_digest,
                            "kind": capability.kind,
                            "actionability": capability.actionability,
                            "install_descriptor_digest": capability.install_descriptor_digest,
                            "install_plan_digest": capability.install_plan_digest,
                            "lease_id": "lease-managed-consent",
                        }
                    ],
                },
            )
        )
    requests = tuple(action for action in desired.actions if action.kind == "RequestConsent")
    assert len(requests) == 1
    return initial, requests[0]


def _commit_consent_decision(setup: _Setup, request: HostAction) -> None:
    original = setup.managed_input.decision_event
    with open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.artifact,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        policy_store_root=setup.policy_root,
        interactive_install_decision_guard=lambda _reservation: nullcontext(),
        skill_cas_runtime=setup.skill_runtime,
        trusted_utc_now=setup.clock,
    ) as composition:
        composition.process(
            replace(
                original,
                event_id="event-managed-consent-race-decision",
                kind="UserDecision",
                expected_revision=3,
                occurred_at="2026-08-03T12:01:00Z",
                payload={
                    "consent_id": request.consent_id,
                    "decision": "granted",
                    "decision_basis": "interactive",
                    "policy_snapshot_digest": InstallConsentPolicy.safe_default().policy_digest,
                    "requested_action_id": request.payload["requested_action_id"],
                    "requested_action_kind": request.payload["requested_action_kind"],
                    "requested_action_content_digest": request.payload[
                        "requested_action_content_digest"
                    ],
                    "requested_action_precondition_revision": request.payload[
                        "requested_action_precondition_revision"
                    ],
                },
            )
        )


def test_service_sets_an_empty_desired_subset_without_replanning(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-empty-query"),
            current_work_ref="work-one",
        )
    )
    calls_before = tuple(setup.facts.calls)
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-empty-choice"),
        capability_ids=(),
    )

    result = setup.service.set_desired(request)

    assert type(result) is ManagedDesiredSetResult
    assert result.query_ref == prepared.query_ref
    assert result.logical_choice_id == request.logical_choice_id
    assert result.capability_ids == ()
    assert result.deferred_capability_ids == ()
    assert result.status == "reconciled"
    assert result.reason_code is None
    assert result.plan_id == prepared.prepared.plan_id
    assert result.decision_digest == prepared.prepared.decision_digest
    assert result.journal_revision == 3
    assert len(result.journal_record_digest) == 64
    assert result.actions == ()
    assert result.challenge is None
    assert tuple(setup.facts.calls) == calls_before
    stored = setup.store.load_latest_desired_set(prepared.query_ref)
    assert stored.committed
    assert stored.desired_set_ref == result.desired_set_ref
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(result)
        with pytest.raises(TypeError):
            operation(request)


def test_service_sets_an_install_desired_subset_and_requires_consent(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-install-query"),
            current_work_ref="work-one",
        )
    )
    calls_before = tuple(setup.facts.calls)
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-install-choice"),
        capability_ids=("skill:installable",),
    )

    first = setup.service.set_desired(request)
    repeated = setup.service.set_desired(request)

    assert first.status == repeated.status == "consent-required"
    assert first.reason_code is repeated.reason_code is None
    assert first.capability_ids == repeated.capability_ids == ("skill:installable",)
    assert first.deferred_capability_ids == repeated.deferred_capability_ids == ()
    assert first.desired_set_ref == repeated.desired_set_ref
    assert first.journal_revision == repeated.journal_revision == 3
    assert first.journal_record_digest == repeated.journal_record_digest
    assert [action.kind for action in first.actions] == ["RequestConsent"]
    assert first.actions == repeated.actions
    assert first.challenge == repeated.challenge
    assert type(first.challenge) is ManagedConsentChallengeProjection
    assert tuple(setup.facts.calls) == calls_before
    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (1,)
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (3,)


def test_desired_retry_refuses_interactive_effect_without_signed_execution_path(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-pending-effect-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-pending-effect-choice"),
        capability_ids=("skill:installable",),
    )
    desired = setup.service.set_desired(request)
    assert desired.status == "consent-required"
    original = setup.managed_input.decision_event

    with open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.artifact,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        policy_store_root=setup.policy_root,
        interactive_install_decision_guard=lambda _reservation: nullcontext(),
        skill_cas_runtime=setup.skill_runtime,
        trusted_utc_now=setup.clock,
    ) as composition:
        snapshot = composition.snapshot(original.scope)
        assert snapshot.state is not None
        pending = snapshot.state.pending_consents[0]
        consent_request = setup.service._current_consent_request(  # noqa: SLF001
            scope=original.scope,
            revision=snapshot.revision,
            consent_id=pending.consent_id,
            install_action=pending.install_action,
        )
        granted = composition.process(
            replace(
                original,
                event_id="event-desired-pending-effect-granted",
                kind="UserDecision",
                expected_revision=snapshot.revision,
                payload={
                    "consent_id": consent_request.consent_id,
                    "decision": "granted",
                    "decision_basis": "interactive",
                    "policy_snapshot_digest": InstallConsentPolicy.safe_default().policy_digest,
                    "requested_action_id": consent_request.payload["requested_action_id"],
                    "requested_action_kind": consent_request.payload["requested_action_kind"],
                    "requested_action_content_digest": consent_request.payload[
                        "requested_action_content_digest"
                    ],
                    "requested_action_precondition_revision": consent_request.payload[
                        "requested_action_precondition_revision"
                    ],
                },
            )
        )
        assert [action.kind for action in granted.actions] == ["InstallCapability"]
        observed = composition.process(
            replace(
                original,
                event_id="event-desired-pending-effect-observed",
                kind="ProviderSubmissionObserved",
                expected_revision=granted.to_revision,
                payload={"capabilities": []},
            )
        )
        assert observed.actions == ()
        state = composition.snapshot(original.scope).state
        assert state is not None
        assert [(item.effect, item.action.kind) for item in state.pending_effects] == [
            ("install", "InstallCapability")
        ]

    with pytest.raises(ManagedQueryServiceError, match="interactive install decision"):
        setup.service.set_desired(request)

    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)


@pytest.mark.parametrize(
    "event_kind",
    ["ProviderSubmissionObserved", "ToolCallObserved", "TurnStarting"],
)
def test_actionless_revision_reoffers_consent_and_retry_never_reports_reconciled(
    tmp_path: Path,
    event_kind: str,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest(f"stale-consent-{event_kind}-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest(f"stale-consent-{event_kind}-choice"),
        capability_ids=("skill:installable",),
    )
    initial = setup.service.set_desired(request)
    assert initial.status == "consent-required"
    original = setup.managed_input.decision_event
    payload: dict[str, object]
    if event_kind == "ProviderSubmissionObserved":
        payload = {"capabilities": []}
    elif event_kind == "ToolCallObserved":
        payload = {
            "capability_id": "skill:installable",
            "source_digest": prepared.prepared.selections[0].source_digest,
            "outcome": "failed",
        }
    else:
        payload = {}
    with open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.artifact,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        policy_store_root=setup.policy_root,
        skill_cas_runtime=setup.skill_runtime,
    ) as composition:
        consumed = composition.process(
            replace(
                original,
                event_id=f"event-stale-consent-{event_kind}",
                kind=event_kind,
                expected_revision=3,
                payload=payload,
            )
        )
        state = composition.snapshot(original.scope).state

    assert tuple(action.kind for action in consumed.actions) == ("RequestConsent",)
    assert state is not None
    assert len(state.pending_consents) == 1

    retried = setup.service.set_desired(request)

    assert retried.status == "consent-required"
    assert retried.reason_code is None
    assert retried.deferred_capability_ids == ()
    assert retried.failed_capability_ids == ()
    assert retried.challenge is not None
    assert tuple(action.kind for action in retried.actions) == ("RequestConsent",)


def test_desired_request_canonicalizes_committed_plan_order_and_manual_deferral(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=3)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-manual-query"),
            current_work_ref="work-one",
        )
    )
    plan_order = tuple(item.capability_id for item in prepared.prepared.selections)
    requested = tuple(reversed(plan_order[:2]))

    result = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-manual-choice"),
            capability_ids=requested,
        )
    )

    assert result.capability_ids == plan_order[:2]
    assert result.deferred_capability_ids == plan_order[:2]
    assert result.status == "manual-deferred"
    assert result.reason_code == "manual-capability-requires-user-action"
    assert result.challenge is None


@pytest.mark.parametrize(
    ("capability_ids", "error_type", "message"),
    [
        (["skill:test"], TypeError, "exact tuple"),
        (("skill:test", "skill:test"), ValueError, "unique"),
        (
            tuple(f"skill:test-{index}" for index in range(6)),
            ValueError,
            "more than five",
        ),
    ],
)
def test_desired_request_rejects_unsafe_subsets(
    capability_ids: object,
    error_type: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error_type, match=message):
        ManagedDesiredSetRequest(
            query_ref="mqr_" + "a" * 64,
            logical_choice_id=_digest("unsafe-desired-request"),
            capability_ids=capability_ids,  # type: ignore[arg-type]
        )


def test_service_rejects_unknown_desired_capability_before_durable_mutation(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-unknown-query"),
            current_work_ref="work-one",
        )
    )

    with pytest.raises(ManagedQueryServiceError, match="not contained"):
        setup.service.set_desired(
            ManagedDesiredSetRequest(
                query_ref=prepared.query_ref,
                logical_choice_id=_digest("desired-unknown-choice"),
                capability_ids=("skill:not-in-plan",),
            )
        )

    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (0,)
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (2,)


def test_preapproved_desired_install_executes_once_and_retries_from_receipt(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-auto-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-auto-choice"),
        capability_ids=("skill:installable",),
    )

    first = setup.service.set_desired(request)
    repeated = setup.service.set_desired(request)

    assert first.status == repeated.status == "effect-pending"
    assert first.reason_code is repeated.reason_code is None
    assert first.deferred_capability_ids == repeated.deferred_capability_ids == ()
    assert first.failed_capability_ids == repeated.failed_capability_ids == ()
    assert first.challenge is repeated.challenge is None
    assert first.desired_set_ref == repeated.desired_set_ref
    assert tuple(action.kind for action in first.actions) == ("ActivateCapability",)
    assert repeated.actions == first.actions
    assert setup.body_source.load_calls == 1
    bundle = setup.installs.bundles["skill:installable"]
    installed = setup.skill_runtime.skill_store_root / bundle.result_material.content_sha256
    assert installed.read_text(encoding="utf-8") == INSTALLABLE_SKILL_BODY
    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)
    with sqlite3.connect(setup.journal_path) as connection:
        rows = tuple(
            connection.execute(
                "SELECT event_id, replay_json FROM engine_journal ORDER BY revision"
            ).fetchall()
        )
    assert len(rows) == 5
    assert rows[3][0].startswith("ctx-auto-install:")
    assert ReplayInput.from_json(rows[3][1]).reducer_event.kind == "UserDecision"
    assert ReplayInput.from_json(rows[4][1]).reducer_event.kind == "ActionApplied"


def test_two_services_converge_on_one_preapproved_install_and_receipt(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-auto-concurrent-query"),
            current_work_ref="work-one",
        )
    )
    peer = _open_peer_service(setup)
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-auto-concurrent-choice"),
        capability_ids=("skill:installable",),
    )
    barrier = Barrier(2)

    def attempt(service: ManagedQueryService) -> ManagedDesiredSetResult:
        barrier.wait()
        return service.set_desired(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, (setup.service, peer)))

    assert results[0].desired_set_ref == results[1].desired_set_ref
    assert results[0].status == results[1].status == "effect-pending"
    assert results[0].actions == results[1].actions
    assert setup.body_source.load_calls == 1
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (5,)
        assert connection.execute(
            "SELECT count(*) FROM engine_install_claim_settlements"
        ).fetchone() == (1,)

    recovered = setup.service.set_desired(request)

    assert recovered.status == "effect-pending"
    assert recovered.failed_capability_ids == ()
    assert setup.body_source.load_calls == 1
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (5,)
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM engine_install_claim_settlements"
        ).fetchone() == (1,)


def test_preapproved_install_requires_captured_actuator_before_grant(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-auto-no-actuator-query"),
            current_work_ref="work-one",
        )
    )
    without_actuator = open_managed_query_service(
        registry=setup.registry,
        query_store=setup.store,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        input_authority=setup.inputs,
        consent_broker=setup.consent_broker,
        policy_store_root=setup.policy_root,
    )

    with pytest.raises(RuntimeError, match="no physical installation actuator"):
        without_actuator.set_desired(
            ManagedDesiredSetRequest(
                query_ref=prepared.query_ref,
                logical_choice_id=_digest("desired-auto-no-actuator-choice"),
                capability_ids=("skill:installable",),
            )
        )

    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        rows = tuple(
            connection.execute(
                "SELECT replay_json FROM engine_journal ORDER BY revision"
            ).fetchall()
        )
    assert tuple(ReplayInput.from_json(row[0]).reducer_event.kind for row in rows) == (
        "SessionStarted",
        "IntentObserved",
        "ReassessmentRequested",
    )


def test_preapproved_policy_race_fails_before_grant_and_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-auto-policy-race-query"),
            current_work_ref="work-one",
        )
    )
    original = EngineComposition.resolve_install_execution_binding
    changed = False

    def change_policy_after_binding(
        composition: EngineComposition,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
    ) -> InstallExecutionBinding:
        nonlocal changed
        binding = original(composition, action, selection)
        if not changed:
            changed = True
            persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
        return binding

    monkeypatch.setattr(
        EngineComposition,
        "resolve_install_execution_binding",
        change_policy_after_binding,
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-auto-policy-race-choice"),
        capability_ids=("skill:installable",),
    )

    with pytest.raises(RuntimeError, match="policy"):
        setup.service.set_desired(request)

    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (3,)


def test_indeterminate_preapproved_install_is_explicit_then_settles_failed_once(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    setup.body_source.fail_loads_remaining = 1
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-auto-indeterminate-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-auto-indeterminate-choice"),
        capability_ids=("skill:installable",),
    )

    indeterminate = setup.service.set_desired(request)
    failed = setup.service.set_desired(request)
    repeated = setup.service.set_desired(request)

    assert indeterminate.status == "lifecycle-deferred"
    assert indeterminate.reason_code == "automatic-install-indeterminate"
    assert indeterminate.deferred_capability_ids == ("skill:installable",)
    assert indeterminate.failed_capability_ids == ()
    assert failed.status == repeated.status == "lifecycle-deferred"
    assert failed.reason_code == repeated.reason_code == "automatic-install-failed"
    assert (
        failed.deferred_capability_ids == repeated.deferred_capability_ids == ("skill:installable",)
    )
    assert failed.failed_capability_ids == repeated.failed_capability_ids == ("skill:installable",)
    assert setup.body_source.load_calls == 1
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM engine_install_claim_settlements"
        ).fetchone() == (1,)


def test_failed_auto_install_progresses_to_next_capability_without_losing_failure(
    tmp_path: Path,
) -> None:
    setup = _setup(
        tmp_path,
        requested_limit=2,
        installable=True,
        installable_count=2,
    )
    setup.body_source.fail_loads_remaining = 1
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-auto-mixed-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-auto-mixed-choice"),
        capability_ids=("skill:installable", "skill:installable-2"),
    )

    first = setup.service.set_desired(request)
    progressed = setup.service.set_desired(request)
    repeated = setup.service.set_desired(request)

    assert first.reason_code == "automatic-install-indeterminate"
    assert progressed.status == repeated.status == "effect-pending"
    assert progressed.deferred_capability_ids == repeated.deferred_capability_ids == ()
    assert (
        progressed.failed_capability_ids == repeated.failed_capability_ids == ("skill:installable",)
    )
    assert tuple(action.kind for action in progressed.actions) == ("ActivateCapability",)
    assert setup.body_source.load_calls == 2
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM engine_install_claim_settlements"
        ).fetchone() == (2,)


def test_preapproved_install_recovers_after_crash_following_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("auto-crash-after-decision-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("auto-crash-after-decision-choice"),
        capability_ids=("skill:installable",),
    )
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            EngineComposition,
            "execute_install",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash after automatic decision")
            ),
        )
        with pytest.raises(RuntimeError, match="after automatic decision"):
            setup.service.set_desired(request)

    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (4,)
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
    # The exact grant is already journaled. A later policy change cannot revoke
    # its pending physical action or force a second consent decision.
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)

    recovered = setup.service.set_desired(request)

    assert recovered.status == "effect-pending"
    assert recovered.failed_capability_ids == ()
    assert setup.body_source.load_calls == 1


def test_preapproved_install_rejects_changed_target_after_committed_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("auto-changed-target-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("auto-changed-target-choice"),
        capability_ids=("skill:installable",),
    )
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            EngineComposition,
            "execute_install",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash after bound automatic decision")
            ),
        )
        with pytest.raises(RuntimeError, match="after bound automatic decision"):
            setup.service.set_desired(request)

    changed_root = tmp_path / "private" / "changed-skills"
    changed_root.mkdir(mode=0o700)
    changed_source = _BodySource()
    changed_runtime = SkillCasRuntimeConfig(
        skill_store_root=changed_root,
        body_source=changed_source,
        installer_id="ctx-skill-installer-v1",
        host_identity_digest=_digest("managed-service-host"),
    )
    reopened = open_managed_query_service(
        registry=setup.registry,
        query_store=setup.store,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        input_authority=setup.inputs,
        consent_broker=setup.consent_broker,
        policy_store_root=setup.policy_root,
        trusted_utc_now=lambda: datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        skill_cas_runtime=changed_runtime,
    )

    with pytest.raises(ManagedQueryServiceError, match="captured actuator"):
        reopened.set_desired(request)

    assert setup.body_source.load_calls == changed_source.load_calls == 0
    assert tuple(setup.skill_runtime.skill_store_root.iterdir()) == ()
    assert tuple(changed_root.iterdir()) == ()
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (0,)


def test_preapproved_install_never_reapplies_after_crash_following_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("auto-crash-after-claim-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("auto-crash-after-claim-choice"),
        capability_ids=("skill:installable",),
    )
    original = CtxEngine.authorize_install

    def crash_after_claim(engine: CtxEngine, *args: object, **kwargs: object) -> None:
        original(engine, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("simulated crash after durable install claim")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(CtxEngine, "authorize_install", crash_after_claim)
        with pytest.raises(RuntimeError, match="after durable install claim"):
            setup.service.set_desired(request)

    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (0,)

    recovered = setup.service.set_desired(request)

    assert recovered.status == "lifecycle-deferred"
    assert recovered.reason_code == "automatic-install-failed"
    assert recovered.failed_capability_ids == ("skill:installable",)
    assert setup.body_source.load_calls == 0


def test_preapproved_install_expired_before_claim_retires_once_and_next_choice_progresses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_clock=lambda: current[0],
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("auto-expired-before-claim-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("auto-expired-before-claim-choice"),
        capability_ids=("skill:installable",),
    )
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            EngineComposition,
            "execute_install",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated automatic crash before install claim")
            ),
        )
        with pytest.raises(RuntimeError, match="before install claim"):
            setup.service.set_desired(request)

    current[0] = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    retired = setup.service.set_desired(request)
    repeated = setup.service.set_desired(request)

    assert retired.journal_revision == repeated.journal_revision
    assert retired.journal_record_digest == repeated.journal_record_digest
    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (0,)
    expiry_events = tuple(
        record
        for record in SQLiteEngineStore(setup.journal_path).records(StreamId.from_scope(_scope()))
        if ReplayInput.from_json(record.replay_json).reducer_event.kind == "ActionExpired"
    )
    assert len(expiry_events) == 1

    progressed = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("auto-expired-before-claim-next-choice"),
            capability_ids=("skill:installable",),
            expected_previous_desired_set_ref=retired.desired_set_ref,
        )
    )
    assert progressed.reason_code != "automatic-install-failed"
    assert setup.body_source.load_calls == 1
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (1,)


def test_preapproved_install_recovers_after_crash_following_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("auto-crash-after-outcome-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("auto-crash-after-outcome-choice"),
        capability_ids=("skill:installable",),
    )
    original = CtxEngine._record_install_outcome  # noqa: SLF001

    def crash_after_outcome(engine: CtxEngine, *args: object, **kwargs: object) -> object:
        original(engine, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("simulated crash after durable install outcome")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(CtxEngine, "_record_install_outcome", crash_after_outcome)
        with pytest.raises(RuntimeError, match="after durable install outcome"):
            setup.service.set_desired(request)

    assert setup.body_source.load_calls == 1
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM engine_install_claim_settlements"
        ).fetchone() == (0,)

    recovered = setup.service.set_desired(request)

    assert recovered.status == "effect-pending"
    assert recovered.failed_capability_ids == ()
    assert setup.body_source.load_calls == 1


def test_preapproved_install_recovers_after_crash_following_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("auto-crash-after-receipt-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("auto-crash-after-receipt-choice"),
        capability_ids=("skill:installable",),
    )
    original = EngineComposition.execute_install

    def crash_after_receipt(
        composition: EngineComposition,
        *args: object,
        **kwargs: object,
    ) -> object:
        original(composition, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("simulated crash after durable install receipt")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(EngineComposition, "execute_install", crash_after_receipt)
        with pytest.raises(RuntimeError, match="after durable install receipt"):
            setup.service.set_desired(request)

    assert setup.body_source.load_calls == 1
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (5,)


def test_desired_result_factory_enforces_canonical_bounded_outcome_subsets(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=2)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-result-invariant-query"),
            current_work_ref="work-one",
        )
    )
    capability_ids = tuple(selection.capability_id for selection in prepared.prepared.selections)
    result = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-result-invariant-choice"),
            capability_ids=capability_ids,
        )
    )
    record = setup.store.load_latest_desired_set(prepared.query_ref)
    token = managed_query_service_module._DESIRED_RESULT_FACTORY_TOKEN  # noqa: SLF001

    with pytest.raises(ManagedQueryServiceError, match="deferred subset"):
        ManagedDesiredSetResult._create(  # noqa: SLF001
            factory_token=token,
            record=record,
            deferred_capability_ids=tuple(reversed(capability_ids)),
            failed_capability_ids=(),
            status="manual-deferred",
            reason_code="manual-capability-requires-user-action",
            actions=result.actions,
            challenge=None,
        )
    with pytest.raises(ManagedQueryServiceError, match="failed subset"):
        ManagedDesiredSetResult._create(  # noqa: SLF001
            factory_token=token,
            record=record,
            deferred_capability_ids=(),
            failed_capability_ids=(capability_ids[0],),
            status="reconciled",
            reason_code=None,
            actions=(),
            challenge=None,
        )
    with pytest.raises(ManagedQueryServiceError, match="failed subset"):
        ManagedDesiredSetResult._create(  # noqa: SLF001
            factory_token=token,
            record=record,
            deferred_capability_ids=capability_ids,
            failed_capability_ids=(capability_ids[0], capability_ids[0]),
            status="manual-deferred",
            reason_code="manual-capability-requires-user-action",
            actions=result.actions,
            challenge=None,
        )
    with pytest.raises(ManagedQueryServiceError, match="deferred subset"):
        ManagedDesiredSetResult._create(  # noqa: SLF001
            factory_token=token,
            record=record,
            deferred_capability_ids=capability_ids,
            failed_capability_ids=(),
            status="effect-pending",
            reason_code=None,
            actions=result.actions,
            challenge=None,
        )


def test_desired_result_factory_requires_challenge_action_correspondence(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-result-challenge-query"),
            current_work_ref="work-one",
        )
    )
    result = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-result-challenge-choice"),
            capability_ids=("skill:installable",),
        )
    )
    assert result.challenge is not None
    record = setup.store.load_latest_desired_set(prepared.query_ref)

    with pytest.raises(ManagedQueryServiceError, match="action summary"):
        ManagedDesiredSetResult._create(  # noqa: SLF001
            factory_token=managed_query_service_module._DESIRED_RESULT_FACTORY_TOKEN,  # noqa: SLF001
            record=record,
            deferred_capability_ids=(),
            failed_capability_ids=(),
            status="consent-required",
            reason_code=None,
            actions=(),
            challenge=result.challenge,
        )


def test_automatic_desired_result_exposes_no_execution_or_content_authority(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-auto-privacy-query"),
            current_work_ref="work-one",
        )
    )

    result = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-auto-privacy-choice"),
            capability_ids=("skill:installable",),
        )
    )

    assert set(result.__slots__) == {
        "actions",
        "capability_ids",
        "challenge",
        "decision_digest",
        "deferred_capability_ids",
        "desired_set_ref",
        "failed_capability_ids",
        "journal_record_digest",
        "journal_revision",
        "logical_choice_id",
        "plan_id",
        "query_ref",
        "reason_code",
        "status",
    }
    assert str(tmp_path) not in repr(result)
    for forbidden in (
        "artifact",
        "body",
        "command",
        "driver",
        "execution_binding",
        "install_action",
        "path",
        "receipt",
        "selection",
        "transition",
    ):
        assert not hasattr(result, forbidden)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(result)


def test_service_accepts_sequential_choices_with_exact_predecessors(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=2)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-sequence-query"),
            current_work_ref="work-one",
        )
    )
    capability_id = prepared.prepared.selections[0].capability_id
    first_request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-sequence-first"),
        capability_ids=(),
    )
    first = setup.service.set_desired(first_request)
    second_request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-sequence-second"),
        capability_ids=(capability_id,),
        expected_previous_desired_set_ref=first.desired_set_ref,
    )
    second = setup.service.set_desired(second_request)
    unloaded = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-sequence-third"),
            capability_ids=(),
            expected_previous_desired_set_ref=second.desired_set_ref,
        )
    )

    assert first.status == unloaded.status == "reconciled"
    assert second.status == "manual-deferred"
    assert first.journal_revision == 3
    assert second.journal_revision == 4
    assert unloaded.journal_revision == 5
    with pytest.raises(ManagedDesiredSetSupersededError):
        setup.service.set_desired(first_request)
    with pytest.raises(ManagedDesiredSetSupersededError):
        setup.service.set_desired(
            ManagedDesiredSetRequest(
                query_ref=prepared.query_ref,
                logical_choice_id=_digest("desired-sequence-stale-fourth"),
                capability_ids=(capability_id,),
                expected_previous_desired_set_ref=first.desired_set_ref,
            )
        )


def test_set_desired_recovers_a_reservation_after_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-reserve-crash-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-reserve-crash-choice"),
        capability_ids=(),
    )
    original = EngineComposition.process

    def crash_on_desired(composition: EngineComposition, event: EngineEvent) -> Transition:
        if event.kind == "ReassessmentRequested":
            raise RuntimeError("simulated crash after desired reservation")
        return original(composition, event)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(EngineComposition, "process", crash_on_desired)
        with pytest.raises(RuntimeError, match="after desired reservation"):
            setup.service.set_desired(request)
    pending = setup.store.load_pending_desired_set(_scope())

    recovered = setup.service.set_desired(request)

    assert recovered.desired_set_ref == pending.desired_set_ref
    assert recovered.status == "reconciled"
    assert setup.store.load_desired_set(pending.desired_set_ref).committed


def test_set_desired_recovers_engine_commit_before_store_mark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-mark-crash-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-mark-crash-choice"),
        capability_ids=(),
    )

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            ManagedQueryStore,
            "mark_desired_set_committed",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash before desired store mark")
            ),
        )
        with pytest.raises(RuntimeError, match="before desired store mark"):
            setup.service.set_desired(request)
    pending = setup.store.load_pending_desired_set(_scope())
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (3,)

    recovered = setup.service.set_desired(request)

    assert recovered.desired_set_ref == pending.desired_set_ref
    assert recovered.journal_revision == 3
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (3,)


def test_set_desired_recovers_after_store_mark_before_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-result-crash-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-result-crash-choice"),
        capability_ids=(),
    )

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            ManagedDesiredSetResult,
            "_create",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash before desired result")
            ),
        )
        with pytest.raises(RuntimeError, match="before desired result"):
            setup.service.set_desired(request)
    committed = setup.store.load_latest_desired_set(prepared.query_ref)
    assert committed.committed

    recovered = setup.service.set_desired(request)

    assert recovered.desired_set_ref == committed.desired_set_ref
    assert recovered.status == "reconciled"


def test_committed_desired_recovery_marks_before_deferring_policy_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-policy-drift-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-policy-drift-choice"),
        capability_ids=("skill:installable",),
    )
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            ManagedQueryStore,
            "mark_desired_set_committed",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash before policy drift mark")
            ),
        )
        with pytest.raises(RuntimeError, match="before policy drift mark"):
            setup.service.set_desired(request)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )

    recovered = setup.service.set_desired(request)

    assert recovered.status == "lifecycle-deferred"
    assert recovered.reason_code == "install-policy-changed-after-desired-commit"
    assert recovered.deferred_capability_ids == ("skill:installable",)
    assert recovered.challenge is None
    assert setup.store.load_latest_desired_set(prepared.query_ref).committed
    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_reserved_desired_policy_drift_commits_and_allows_exact_reassessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-reserved-policy-drift-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-reserved-policy-drift-choice"),
        capability_ids=("skill:installable",),
    )
    original = EngineComposition.process

    def crash_after_reservation(
        composition: EngineComposition,
        event: EngineEvent,
    ) -> Transition:
        if event.kind == "ReassessmentRequested":
            raise RuntimeError("simulated crash before desired policy commit")
        return original(composition, event)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(EngineComposition, "process", crash_after_reservation)
        with pytest.raises(RuntimeError, match="before desired policy commit"):
            setup.service.set_desired(request)
    pending = setup.store.load_pending_desired_set(_scope())
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)

    recovered = setup.service.set_desired(request)

    assert recovered.desired_set_ref == pending.desired_set_ref
    assert recovered.status == "lifecycle-deferred"
    assert recovered.reason_code == "install-policy-changed-after-desired-commit"
    assert recovered.deferred_capability_ids == ("skill:installable",)
    assert setup.store.load_latest_desired_set(prepared.query_ref).committed

    reassessed = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-current-policy-choice"),
            capability_ids=("skill:installable",),
            expected_previous_desired_set_ref=recovered.desired_set_ref,
        )
    )

    assert reassessed.status == "consent-required"
    assert type(reassessed.challenge) is ManagedConsentChallengeProjection
    assert reassessed.journal_revision == recovered.journal_revision + 1
    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM managed_desired_sets WHERE journal_revision IS NOT NULL"
        ).fetchone() == (2,)
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (4,)


def test_pending_desired_blocks_a_replacement_plan_before_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-block-plan-query"),
            current_work_ref="work-one",
        )
    )
    original = EngineComposition.process

    def crash_on_desired(composition: EngineComposition, event: EngineEvent) -> Transition:
        if event.kind == "ReassessmentRequested":
            raise RuntimeError("simulated pending desired")
        return original(composition, event)

    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-block-plan-choice"),
        capability_ids=(),
    )
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(EngineComposition, "process", crash_on_desired)
        with pytest.raises(RuntimeError, match="pending desired"):
            setup.service.set_desired(request)

    started = setup.managed_input.session_started
    development = replace(
        setup.managed_input.decision_event,
        event_id="event-development-blocked-by-desired",
        kind="DevelopmentObserved",
        expected_revision=2,
        correlation_id="plan-after-pending-desired",
        causation_id=setup.managed_input.decision_event.event_id,
    )
    setup.inputs.values["work-after-pending-desired"] = ManagedQueryInput(
        artifact=setup.artifact,
        session_started=started,
        decision_event=development,
    )

    with pytest.raises(ManagedDesiredSetBusyError):
        setup.service.prepare(
            ManagedQueryRequest(
                logical_query_id=_digest("plan-blocked-by-pending-desired"),
                current_work_ref="work-after-pending-desired",
            )
        )

    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (1,)
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (2,)


def test_two_services_serialize_competing_first_desired_choices(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-two-service-query"),
            current_work_ref="work-one",
        )
    )
    peer = _open_peer_service(setup)
    barrier = Barrier(2)
    requests = (
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-two-service-a"),
            capability_ids=(),
        ),
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-two-service-b"),
            capability_ids=(),
        ),
    )

    def attempt(
        service_and_request: tuple[ManagedQueryService, ManagedDesiredSetRequest],
    ) -> ManagedDesiredSetResult | Exception:
        service, desired_request = service_and_request
        barrier.wait()
        try:
            return service.set_desired(desired_request)
        except Exception as exc:  # noqa: BLE001 - concurrent outcome assertion.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(attempt, zip((setup.service, peer), requests, strict=True)))

    assert sum(type(outcome) is ManagedDesiredSetResult for outcome in outcomes) == 1
    assert sum(type(outcome) is ManagedDesiredSetSupersededError for outcome in outcomes) == 1
    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM managed_desired_sets WHERE journal_revision IS NOT NULL"
        ).fetchone() == (1,)


def test_two_services_converge_on_the_same_logical_desired_choice(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-two-service-same-query"),
            current_work_ref="work-one",
        )
    )
    peer = _open_peer_service(setup)
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-two-service-same-choice"),
        capability_ids=(),
    )
    barrier = Barrier(2)

    def attempt(service: ManagedQueryService) -> ManagedDesiredSetResult:
        barrier.wait()
        return service.set_desired(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, (setup.service, peer)))

    assert results[0].desired_set_ref == results[1].desired_set_ref
    assert results[0].journal_record_digest == results[1].journal_record_digest
    assert results[0].actions == results[1].actions
    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (1,)
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_journal").fetchone() == (3,)


def test_two_services_serialize_replacement_planning_against_desired_choice(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-plan-race-query"),
            current_work_ref="work-one",
        )
    )
    development = replace(
        setup.managed_input.decision_event,
        event_id="event-development-racing-desired",
        kind="DevelopmentObserved",
        expected_revision=2,
        correlation_id="plan-racing-desired",
        causation_id=setup.managed_input.decision_event.event_id,
    )
    setup.inputs.values["work-racing-desired"] = ManagedQueryInput(
        artifact=setup.artifact,
        session_started=setup.managed_input.session_started,
        decision_event=development,
    )
    peer = _open_peer_service(setup)
    barrier = Barrier(2)

    def desired_attempt() -> ManagedDesiredSetResult | Exception:
        barrier.wait()
        try:
            return setup.service.set_desired(
                ManagedDesiredSetRequest(
                    query_ref=prepared.query_ref,
                    logical_choice_id=_digest("desired-plan-race-choice"),
                    capability_ids=(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - concurrent outcome assertion.
            return exc

    def planning_attempt() -> ManagedQueryServiceResult | Exception:
        barrier.wait()
        try:
            return peer.prepare(
                ManagedQueryRequest(
                    logical_query_id=_digest("replacement-plan-racing-desired"),
                    current_work_ref="work-racing-desired",
                )
            )
        except Exception as exc:  # noqa: BLE001 - concurrent outcome assertion.
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        desired_future = pool.submit(desired_attempt)
        planning_future = pool.submit(planning_attempt)
        outcomes = (desired_future.result(), planning_future.result())

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    with sqlite3.connect(setup.store_path) as connection:
        desired_rows = connection.execute(
            "SELECT count(*), count(journal_revision) FROM managed_desired_sets"
        ).fetchone()
    assert desired_rows in {(0, 0), (1, 1)}


def test_desired_install_uses_fresh_trusted_time_and_full_digest_identities(
    tmp_path: Path,
) -> None:
    trusted_now = datetime(2026, 8, 3, 15, 30, tzinfo=UTC)
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_now=trusted_now,
    )
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-fresh-time-query"),
            current_work_ref="work-one",
        )
    )

    result = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest("desired-fresh-time-choice"),
            capability_ids=("skill:installable",),
        )
    )

    stored = setup.store.load_latest_desired_set(prepared.query_ref)
    row = stored.event.payload["desired_capabilities"][0]
    assert stored.event.occurred_at == "2026-08-03T15:30:00Z"
    assert stored.event.occurred_at != setup.managed_input.decision_event.occurred_at
    assert stored.event.event_id.startswith("ctx-desired:")
    assert len(stored.event.event_id.removeprefix("ctx-desired:")) == 64
    assert len(stored.event.payload["owner_id"].removeprefix("ctx-desired:")) == 64
    assert len(row["lease_id"].removeprefix("ctx-lease:")) == 64
    lease_mapping = {
        "capability_id": row["capability_id"],
        "kind": row["kind"],
        "owner_id": stored.event.payload["owner_id"],
        "schema": "ctx.managed-desired-lease.v1",
        "source_digest": row["source_digest"],
    }
    expected_lease = hashlib.sha256(
        json.dumps(
            lease_mapping,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    changed_source_lease = hashlib.sha256(
        json.dumps(
            {**lease_mapping, "source_digest": _digest("changed-catalog-source")},
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    ).hexdigest()
    assert row["lease_id"] == f"ctx-lease:{expected_lease}"
    assert changed_source_lease != expected_lease
    assert result.challenge is not None
    assert result.challenge.expires_at == "2026-08-03T16:30:00Z"


@pytest.mark.parametrize("risk", ["permission", "credential"])
def test_preapproved_install_still_requires_consent_for_sensitive_risk(
    tmp_path: Path,
    risk: str,
) -> None:
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        permission_expansion=risk == "permission",
        credential_requirement=risk == "credential",
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest(f"desired-sensitive-{risk}-query"),
            current_work_ref="work-one",
        )
    )

    result = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=prepared.query_ref,
            logical_choice_id=_digest(f"desired-sensitive-{risk}-choice"),
            capability_ids=("skill:installable",),
        )
    )

    assert result.status == "consent-required"
    assert result.reason_code is None
    assert result.deferred_capability_ids == ()
    assert type(result.challenge) is ManagedConsentChallengeProjection


def test_set_desired_fails_typed_when_a_nonservice_writer_steals_reserved_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1)
    prepared = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("desired-stolen-head-query"),
            current_work_ref="work-one",
        )
    )
    request = ManagedDesiredSetRequest(
        query_ref=prepared.query_ref,
        logical_choice_id=_digest("desired-stolen-head-choice"),
        capability_ids=(),
    )
    original = EngineComposition.process

    def crash_on_desired(composition: EngineComposition, event: EngineEvent) -> Transition:
        if event.kind == "ReassessmentRequested":
            raise RuntimeError("simulated reservation crash before stolen head")
        return original(composition, event)

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(EngineComposition, "process", crash_on_desired)
        with pytest.raises(RuntimeError, match="before stolen head"):
            setup.service.set_desired(request)
    pending = setup.store.load_pending_desired_set(_scope())
    with open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.artifact,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        policy_store_root=setup.policy_root,
        skill_cas_runtime=setup.skill_runtime,
    ) as composition:
        composition.process(
            replace(
                pending.event,
                event_id="event-nonservice-stolen-desired-revision",
            )
        )

    with pytest.raises(ManagedDesiredSetConflictError, match="another event"):
        setup.service.set_desired(request)

    still_pending = setup.store.load_pending_desired_set(_scope())
    assert still_pending.desired_set_ref == pending.desired_set_ref
    assert not still_pending.committed


@pytest.mark.parametrize("requested_limit", [0, 1, 5])
def test_service_prepares_one_bounded_ready_or_abstained_result(
    tmp_path: Path,
    requested_limit: int,
) -> None:
    setup = _setup(tmp_path, requested_limit=requested_limit)
    request = ManagedQueryRequest(
        logical_query_id=_digest("logical-query"),
        current_work_ref="work-one",
    )

    result = setup.service.prepare(request)
    record = setup.store.load(result.query_ref)

    assert type(result) is ManagedQueryServiceResult
    assert record.planned
    assert result.prepared.plan_id == record.plan_id == "plan-initial"
    assert len(result.prepared.selections) == requested_limit
    assert len(result.prepared.selections) <= 5
    assert result.prepared.status == ("abstained" if requested_limit == 0 else "ready")
    assert all(
        set(summary.__slots__) == {"action_id", "entity_id", "kind"} for summary in result.actions
    )
    if requested_limit:
        assert [summary.kind for summary in result.actions] == ["PresentBundle"]
    else:
        assert result.actions == ()
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(result)


def test_service_returns_no_challenge_without_current_pending_consent(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=0)

    result = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("no-consent-query"),
            current_work_ref="work-one",
        )
    )

    assert result.challenge is None
    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_service_publishes_one_safe_current_consent_challenge_without_reranking(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    initial, _request = _commit_pending_consent(setup)
    calls_before = tuple(setup.facts.calls)

    result = setup.service.reopen(initial.query_ref)

    challenge = result.challenge
    assert type(challenge) is ManagedConsentChallengeProjection
    assert set(challenge.__slots__) == {
        "audience",
        "capability_id",
        "challenge_digest",
        "expires_at",
        "kind",
    }
    assert challenge.capability_id == "skill:installable"
    assert challenge.kind == "skill"
    assert challenge.audience == CONSENT_AUDIENCE
    assert challenge.expires_at == "2026-08-03T13:00:00Z"
    assert tuple(setup.facts.calls) == calls_before
    assert setup.body_source.load_calls == 0
    assert str(tmp_path) not in repr(challenge)
    assert not hasattr(challenge, "execution_binding")
    assert not hasattr(challenge, "challenge_id")
    assert not hasattr(challenge, "selection")
    assert not hasattr(challenge, "proof")
    assert not hasattr(challenge, "scope")
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(challenge)


def test_service_resolves_signed_grant_and_recovers_installed_inactive(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup)
    assert desired.challenge is not None

    result = setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))
    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert type(result) is ManagedConsentResolutionResult
    assert result.outcome == recovered.outcome == "installed-inactive"
    assert result.reason_code == recovered.reason_code == "physical-install-applied"
    assert result.next_challenge is recovered.next_challenge is None
    assert result.capability_id == desired.challenge.capability_id
    assert result.kind == desired.challenge.kind
    assert result.challenge_digest == desired.challenge.challenge_digest
    assert result.journal_revision >= desired.journal_revision + 2
    assert len(result.journal_record_digest) == 64
    assert setup.body_source.load_calls == 1
    assert set(result.__slots__) == {
        "actions",
        "capability_id",
        "challenge_digest",
        "journal_record_digest",
        "journal_revision",
        "kind",
        "next_challenge",
        "outcome",
        "reason_code",
    }
    assert str(tmp_path) not in repr(result)
    for forbidden in (
        "scope",
        "challenge_id",
        "driver",
        "binding",
        "verifier",
        "proof",
        "principal_digest",
    ):
        assert not hasattr(result, forbidden)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(result)


def test_service_signed_denial_never_invokes_install_driver(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="signed-denial")
    assert desired.challenge is not None

    result = setup.service.resolve_consent(
        _consent_assertion_payload(
            desired.challenge,
            decision="denied",
            nonce="managed-consent-denial",
        )
    )
    skill_root = setup.skill_runtime.skill_store_root
    displaced = skill_root.with_name("skills-denial-recovery-displaced")
    skill_root.rename(displaced)
    skill_root.mkdir(mode=0o700)
    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert result.outcome == recovered.outcome == "denied"
    assert result.reason_code == "human-denied-install"
    assert setup.body_source.load_calls == 0
    skill_root.rmdir()
    displaced.rename(skill_root)


def test_signed_grant_expired_before_claim_is_retired_once_and_can_be_chosen_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_clock=lambda: current[0],
    )
    desired = _prepare_public_consent(setup, label="signed-expired-before-claim")
    assert desired.challenge is not None

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            EngineComposition,
            "execute_install",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash before install claim")
            ),
        )
        with pytest.raises(RuntimeError, match="before install claim"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    current[0] = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    expired = setup.service.recover_consent(desired.challenge.challenge_digest)
    repeated = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert expired.outcome == repeated.outcome == "expired"
    assert expired.reason_code == "install-approval-expired-before-claim"
    assert expired.journal_revision == repeated.journal_revision == desired.journal_revision + 2
    assert expired.journal_record_digest == repeated.journal_record_digest
    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (0,)
    expiry_events = tuple(
        record
        for record in SQLiteEngineStore(setup.journal_path).records(StreamId.from_scope(_scope()))
        if ReplayInput.from_json(record.replay_json).reducer_event.kind == "ActionExpired"
    )
    assert len(expiry_events) == 1

    next_choice = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=desired.query_ref,
            logical_choice_id=_digest("signed-expired-before-claim-next-choice"),
            capability_ids=(desired.challenge.capability_id,),
            expected_previous_desired_set_ref=desired.desired_set_ref,
        )
    )
    assert next_choice.status == "consent-required"
    assert next_choice.challenge is not None
    assert next_choice.challenge.challenge_digest != desired.challenge.challenge_digest


def test_signed_grant_claimed_before_ttl_reconciles_after_ttl_without_reapply_or_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_clock=lambda: current[0],
    )
    desired = _prepare_public_consent(setup, label="signed-claimed-before-expiry")
    assert desired.challenge is not None
    original = CtxEngine.authorize_install

    def crash_after_claim(engine: CtxEngine, *args: object, **kwargs: object) -> None:
        original(engine, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("simulated crash after signed install claim")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(CtxEngine, "authorize_install", crash_after_claim)
        with pytest.raises(RuntimeError, match="after signed install claim"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    current[0] = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "install-failed"
    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (1,)
    assert all(
        ReplayInput.from_json(record.replay_json).reducer_event.kind != "ActionExpired"
        for record in SQLiteEngineStore(setup.journal_path).records(StreamId.from_scope(_scope()))
    )


def test_service_recovery_never_mints_decision_after_precommit_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="precommit-crash")
    assert desired.challenge is not None

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            EngineComposition,
            "process",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated precommit crash")
            ),
        )
        with pytest.raises(RuntimeError, match="precommit crash"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "reauthentication-required"
    assert recovered.next_challenge == desired.challenge
    assert setup.body_source.load_calls == 0
    resolved = setup.service.resolve_consent(
        _consent_assertion_payload(
            desired.challenge,
            nonce="managed-consent-fresh-after-crash",
        )
    )
    assert resolved.outcome == "installed-inactive"
    assert setup.body_source.load_calls == 1


def test_service_recovers_commit_before_broker_settle_without_reauthentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="postcommit-crash")
    assert desired.challenge is not None

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            SQLiteInstallConsentBrokerStore,
            "_settle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash before broker settle")
            ),
        )
        with pytest.raises(RuntimeError, match="guard settlement failed"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "installed-inactive"
    assert setup.body_source.load_calls == 1


def test_decision_ready_grant_recovery_after_challenge_ttl_needs_no_live_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_clock=lambda: current[0],
    )
    desired = _prepare_public_consent(setup, label="decision-ready-expired-grant")
    assert desired.challenge is not None
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            SQLiteInstallConsentBrokerStore,
            "_settle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated decision-ready crash")
            ),
        )
        with pytest.raises(RuntimeError, match="guard settlement failed"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    current[0] = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    skill_root = setup.skill_runtime.skill_store_root
    displaced = skill_root.with_name("skills-decision-ready-expired-displaced")
    skill_root.rename(displaced)
    skill_root.mkdir(mode=0o700)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "expired"
    assert recovered.reason_code == "install-approval-expired-before-claim"
    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (0,)
    skill_root.rmdir()
    displaced.rename(skill_root)


def test_service_rejects_unknown_human_identity_before_state_or_driver_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="wrong-human")
    assert desired.challenge is not None

    def forbidden_lookup(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("broker lookup must follow trusted verifier selection")

    monkeypatch.setattr(
        InstallConsentBrokerService,
        "status_by_challenge_digest",
        forbidden_lookup,
    )
    with pytest.raises(UnknownHumanDecisionVerifier):
        setup.service.resolve_consent(
            _consent_assertion_payload(
                desired.challenge,
                principal_digest=_digest("unregistered-principal"),
            )
        )

    assert setup.body_source.load_calls == 0


def test_service_target_drift_does_not_burn_signed_assertion(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="target-drift")
    assert desired.challenge is not None
    payload = _consent_assertion_payload(desired.challenge)
    skill_root = setup.skill_runtime.skill_store_root
    displaced = skill_root.with_name("skills-displaced")
    skill_root.rename(displaced)
    skill_root.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="root changed"):
        setup.service.resolve_consent(payload)

    skill_root.rmdir()
    displaced.rename(skill_root)
    result = setup.service.resolve_consent(payload)

    assert result.outcome == "installed-inactive"
    assert setup.body_source.load_calls == 1


def test_target_replacement_after_authentication_never_reaches_broker_guard_or_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="post-auth-target-drift")
    assert desired.challenge is not None
    skill_root = setup.skill_runtime.skill_store_root
    displaced = skill_root.with_name("skills-post-auth-displaced")
    original = InstallConsentBrokerService.authenticate
    replaced = False

    def authenticate_then_replace_target(
        broker: InstallConsentBrokerService,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal replaced
        authorization = original(broker, *args, **kwargs)  # type: ignore[arg-type]
        if not replaced:
            replaced = True
            skill_root.rename(displaced)
            skill_root.mkdir(mode=0o700)
        return authorization

    monkeypatch.setattr(
        InstallConsentBrokerService,
        "authenticate",
        authenticate_then_replace_target,
    )
    with pytest.raises(RuntimeError, match="root changed"):
        setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM engine_journal WHERE event_id LIKE 'ctx-interactive-install:%'"
        ).fetchone() == (0,)

    skill_root.rmdir()
    displaced.rename(skill_root)
    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "reauthentication-required"
    assert recovered.next_challenge == desired.challenge


def test_target_replacement_after_decision_commit_never_reaches_claim_or_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="post-commit-target-drift")
    assert desired.challenge is not None
    skill_root = setup.skill_runtime.skill_store_root
    displaced = skill_root.with_name("skills-post-commit-displaced")
    original = ManagedQueryService._finish_settled_consent  # noqa: SLF001
    replaced = False

    def replace_target_before_execution(
        service: ManagedQueryService,
        *args: object,
        **kwargs: object,
    ) -> ManagedConsentResolutionResult:
        nonlocal replaced
        if not replaced:
            replaced = True
            skill_root.rename(displaced)
            skill_root.mkdir(mode=0o700)
        return original(service, *args, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            ManagedQueryService,
            "_finish_settled_consent",
            replace_target_before_execution,
        )
        with pytest.raises(RuntimeError, match="root changed"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    assert setup.body_source.load_calls == 0
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (0,)

    skill_root.rmdir()
    displaced.rename(skill_root)
    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "installed-inactive"
    assert setup.body_source.load_calls == 1


def test_service_policy_drift_does_not_burn_signed_assertion(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="signed-policy-drift")
    assert desired.challenge is not None
    payload = _consent_assertion_payload(desired.challenge)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )

    with pytest.raises(ManagedQueryHeadDriftError, match="policy changed"):
        setup.service.resolve_consent(payload)

    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)
    result = setup.service.resolve_consent(payload)

    assert result.outcome == "installed-inactive"
    assert setup.body_source.load_calls == 1


def test_concurrent_signed_resolution_executes_install_at_most_once(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="signed-concurrency")
    assert desired.challenge is not None
    payload = _consent_assertion_payload(desired.challenge)
    peer = _open_peer_service(setup)

    def resolve(service: ManagedQueryService) -> str:
        try:
            return service.resolve_consent(payload).outcome
        except ManagedQueryServiceError:
            return "already-consumed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(resolve, (setup.service, peer)))

    assert sorted(outcomes) == ["already-consumed", "installed-inactive"]
    assert setup.body_source.load_calls == 1
    assert (
        setup.service.recover_consent(desired.challenge.challenge_digest).outcome
        == "installed-inactive"
    )


def test_pending_consent_recovery_requires_no_verifier_or_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="pending-recovery")
    assert desired.challenge is not None
    monkeypatch.setattr(
        InstallConsentBrokerService,
        "authenticate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must not authenticate")
        ),
    )

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "consent-required"
    assert recovered.reason_code == "fresh-human-decision-required"
    assert recovered.next_challenge == desired.challenge
    assert setup.body_source.load_calls == 0


def test_terminal_broker_expiry_durably_retires_pending_consent_and_allows_next_choice(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_clock=lambda: current[0],
    )
    desired = _prepare_public_consent(setup, label="terminal-expiry")
    assert desired.challenge is not None
    current[0] = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    skill_root = setup.skill_runtime.skill_store_root
    displaced = skill_root.with_name("skills-expiry-displaced")
    skill_root.rename(displaced)
    skill_root.mkdir(mode=0o700)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        setup.policy_root,
    )

    expired = setup.service.recover_consent(desired.challenge.challenge_digest)
    repeated = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert expired.outcome == repeated.outcome == "expired"
    assert expired.journal_revision == repeated.journal_revision == desired.journal_revision + 1
    assert expired.journal_record_digest == repeated.journal_record_digest
    with setup.service._open_consent_composition(  # noqa: SLF001
        setup.artifact,
        include_actuators=False,
    ) as composition:
        state = composition.snapshot(_scope()).state
        assert state is not None
        assert state.pending_consents == ()
    with sqlite3.connect(setup.journal_path) as connection:
        assert connection.execute("SELECT count(*) FROM engine_install_claims").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM engine_install_outcomes").fetchone() == (0,)

    skill_root.rmdir()
    displaced.rename(skill_root)
    persist_install_policy(InstallConsentPolicy.safe_default(), setup.policy_root)

    next_choice = setup.service.set_desired(
        ManagedDesiredSetRequest(
            query_ref=desired.query_ref,
            logical_choice_id=_digest("terminal-expiry-next-choice"),
            capability_ids=(desired.challenge.capability_id,),
            expected_previous_desired_set_ref=desired.desired_set_ref,
        )
    )
    assert next_choice.status == "consent-required"
    assert next_choice.challenge is not None
    assert next_choice.challenge.challenge_digest != desired.challenge.challenge_digest


def test_consent_expiry_recovers_after_journal_commit_before_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_clock=lambda: current[0],
    )
    desired = _prepare_public_consent(setup, label="expiry-result-crash")
    assert desired.challenge is not None
    current[0] = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            ManagedConsentResolutionResult,
            "_create",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated crash after expiry journal commit")
            ),
        )
        with pytest.raises(RuntimeError, match="after expiry journal commit"):
            setup.service.recover_consent(desired.challenge.challenge_digest)

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "expired"
    assert recovered.journal_revision == desired.journal_revision + 1
    expiry_events = tuple(
        record
        for record in SQLiteEngineStore(setup.journal_path).records(StreamId.from_scope(_scope()))
        if ReplayInput.from_json(record.replay_json).reducer_event.kind == "InstallConsentExpired"
    )
    assert len(expiry_events) == 1


def test_expired_old_challenge_cannot_cancel_refreshed_current_consent(
    tmp_path: Path,
) -> None:
    current = [datetime(2026, 8, 3, 12, 0, tzinfo=UTC)]
    setup = _setup(
        tmp_path,
        requested_limit=1,
        installable=True,
        trusted_clock=lambda: current[0],
    )
    desired = _prepare_public_consent(setup, label="old-expired-refreshed")
    assert desired.challenge is not None
    old_challenge_id = setup.consent_broker.status_by_challenge_digest(
        desired.challenge.challenge_digest
    ).challenge.challenge_id
    original = setup.managed_input.decision_event
    with setup.service._open_consent_composition(setup.artifact) as composition:  # noqa: SLF001
        composition.process(
            replace(
                original,
                event_id="event-refresh-before-old-consent-expiry",
                kind="ProviderSubmissionObserved",
                expected_revision=desired.journal_revision,
                payload={"capabilities": []},
            )
        )
        before = composition.snapshot(_scope())
        assert before.state is not None
        replacement = before.state.pending_consents
        assert len(replacement) == 1
        assert replacement[0].consent_id != old_challenge_id
    current[0] = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "superseded"
    with setup.service._open_consent_composition(setup.artifact) as composition:  # noqa: SLF001
        after = composition.snapshot(_scope())
        assert after.revision == before.revision
        assert after.record_digest == before.record_digest
        assert after.state is not None
        assert after.state.pending_consents == replacement


def test_wrong_assertion_audience_is_rejected_before_broker_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="wrong-audience")
    assert desired.challenge is not None
    monkeypatch.setattr(
        InstallConsentBrokerService,
        "status_by_challenge_digest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("wrong audience must not reach broker lookup")
        ),
    )

    with pytest.raises(ManagedQueryServiceError, match="audience"):
        setup.service.resolve_consent(
            _consent_assertion_payload(
                desired.challenge,
                audience="wrong-managed-audience",
            )
        )

    assert setup.body_source.load_calls == 0


@pytest.mark.parametrize("failure", ["signature", "expired"])
def test_invalid_signed_assertion_does_not_mutate_consent_or_journal(
    tmp_path: Path,
    failure: str,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label=f"invalid-{failure}")
    assert desired.challenge is not None
    journal_before = tuple(
        SQLiteEngineStore(setup.journal_path).records(StreamId.from_scope(_scope()))
    )
    payload = _consent_assertion_payload(
        desired.challenge,
        nonce=f"invalid-{failure}-nonce",
        valid_proof=failure != "signature",
        issued_at=("2026-08-03T11:00:00Z" if failure == "expired" else NOW),
        expires_at=("2026-08-03T11:30:00Z" if failure == "expired" else "2026-08-03T12:30:00Z"),
    )
    expected_error = (
        ConsentBrokerDecisionRejected if failure == "signature" else ConsentBrokerExpired
    )

    with pytest.raises(expected_error):
        setup.service.resolve_consent(payload)

    broker_record = setup.consent_broker.status_by_challenge_digest(
        desired.challenge.challenge_digest
    )
    assert broker_record.state == "pending"
    assert (
        tuple(SQLiteEngineStore(setup.journal_path).records(StreamId.from_scope(_scope())))
        == journal_before
    )
    assert setup.body_source.load_calls == 0


def test_head_drift_is_rejected_before_broker_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="signed-head-drift")
    assert desired.challenge is not None
    head = tuple(SQLiteEngineStore(setup.journal_path).records(StreamId.from_scope(_scope())))[-1]
    request = next(
        action
        for action in Transition.from_json(head.transition_json).actions
        if action.kind == "RequestConsent"
    )
    _commit_consent_decision(setup, request)
    monkeypatch.setattr(
        InstallConsentBrokerService,
        "authenticate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale head must be rejected before authentication")
        ),
    )

    with pytest.raises(ManagedQueryHeadDriftError, match="current pending"):
        setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    assert (
        setup.consent_broker.status_by_challenge_digest(desired.challenge.challenge_digest).state
        == "pending"
    )


def test_signed_install_indeterminate_then_recovers_failed(tmp_path: Path) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    setup.body_source.fail_loads_remaining = 1
    desired = _prepare_public_consent(setup, label="signed-indeterminate")
    assert desired.challenge is not None

    indeterminate = setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))
    failed = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert indeterminate.outcome == "install-indeterminate"
    assert indeterminate.reason_code == "physical-install-outcome-indeterminate"
    assert failed.outcome == "install-failed"
    assert failed.reason_code == "physical-install-failed"
    assert setup.body_source.load_calls == 1


def test_recovery_quarantines_decision_when_journal_head_advanced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="signed-quarantine")
    assert desired.challenge is not None
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            EngineComposition,
            "process",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("simulated precommit crash")
            ),
        )
        with pytest.raises(RuntimeError, match="precommit crash"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))
    original = setup.managed_input.decision_event
    with setup.service._open_consent_composition(setup.artifact) as composition:  # noqa: SLF001
        composition.process(
            replace(
                original,
                event_id="event-signed-consent-head-advanced",
                kind="ProviderSubmissionObserved",
                expected_revision=desired.journal_revision,
                payload={"capabilities": []},
            )
        )

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "quarantined"
    assert recovered.reason_code == "journal-head-advanced"
    assert recovered.next_challenge is None
    assert setup.body_source.load_calls == 0


def test_signed_recovery_after_receipt_crash_does_not_reinvoke_driver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    desired = _prepare_public_consent(setup, label="signed-receipt-crash")
    assert desired.challenge is not None
    original = EngineComposition.execute_install

    def crash_after_receipt(
        composition: EngineComposition,
        *args: object,
        **kwargs: object,
    ) -> object:
        original(composition, *args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("simulated signed crash after receipt")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(EngineComposition, "execute_install", crash_after_receipt)
        with pytest.raises(RuntimeError, match="after receipt"):
            setup.service.resolve_consent(_consent_assertion_payload(desired.challenge))

    recovered = setup.service.recover_consent(desired.challenge.challenge_digest)

    assert recovered.outcome == "installed-inactive"
    assert setup.body_source.load_calls == 1


def test_service_challenge_retry_reopen_and_host_aliases_are_idempotent(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    setup.inputs.values["codex-turn"] = setup.managed_input
    setup.inputs.values["claude-hook"] = setup.managed_input
    initial, _request = _commit_pending_consent(setup, logical_query_id="host-consent-query")
    calls_before = tuple(setup.facts.calls)

    codex = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("host-consent-query"),
            current_work_ref="codex-turn",
        )
    )
    claude = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("host-consent-query"),
            current_work_ref="claude-hook",
        )
    )
    reopened = setup.service.reopen(initial.query_ref)

    assert codex.challenge == claude.challenge == reopened.challenge
    assert codex.challenge is not None
    assert tuple(setup.facts.calls) == calls_before
    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (1,)


def test_service_concurrent_challenge_publication_converges_without_replanning(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, _request = _commit_pending_consent(setup)
    calls_before = tuple(setup.facts.calls)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(setup.service.reopen, (initial.query_ref,) * 8))

    assert len({result.challenge.challenge_digest for result in results if result.challenge}) == 1
    assert tuple(setup.facts.calls) == calls_before
    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (1,)


def test_service_reopen_recovers_after_challenge_publish_before_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, _request = _commit_pending_consent(setup)

    def crash_after_publication(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated crash after challenge publication")

    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(ManagedQueryServiceResult, "_create", crash_after_publication)
        with pytest.raises(RuntimeError, match="simulated crash"):
            setup.service.reopen(initial.query_ref)
    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (1,)

    calls_before = tuple(setup.facts.calls)
    recovered = setup.service.reopen(initial.query_ref)

    assert recovered.challenge is not None
    assert tuple(setup.facts.calls) == calls_before


def test_service_rejects_decision_committed_during_registry_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, request = _commit_pending_consent(setup)
    original = EngineComposition.resolve_install_execution_binding

    def resolve_then_decide(
        composition: EngineComposition,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
    ) -> InstallExecutionBinding:
        binding = original(composition, action, selection)
        _commit_consent_decision(setup, request)
        return binding

    monkeypatch.setattr(
        EngineComposition,
        "resolve_install_execution_binding",
        resolve_then_decide,
    )
    with pytest.raises(ManagedQueryHeadDriftError, match="changed"):
        setup.service.reopen(initial.query_ref)

    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_service_never_mixes_plan_a_result_with_plan_b_consent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, _request = _commit_pending_consent(setup)
    original_resolve = EngineComposition.resolve_install_execution_binding
    original_snapshot = EngineComposition.snapshot
    drifted = False

    def resolve_then_replan(
        composition: EngineComposition,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
    ) -> InstallExecutionBinding:
        nonlocal drifted
        binding = original_resolve(composition, action, selection)
        drifted = True
        return binding

    def snapshot_with_plan_drift(composition: EngineComposition, scope: ScopeRef) -> object:
        current = original_snapshot(composition, scope)
        if not drifted:
            return current
        assert current.state is not None
        committed = current.state.committed_plan
        assert isinstance(committed, CommittedPlanV3)
        return SimpleNamespace(
            revision=current.revision,
            record_digest=_digest("plan-race-b-head"),
            state=SimpleNamespace(
                committed_plan=replace(committed, plan_id="plan-race-b"),
                pending_consents=current.state.pending_consents,
            ),
        )

    monkeypatch.setattr(
        EngineComposition,
        "resolve_install_execution_binding",
        resolve_then_replan,
    )
    monkeypatch.setattr(EngineComposition, "snapshot", snapshot_with_plan_drift)
    with pytest.raises(ManagedQueryHeadDriftError, match="changed"):
        setup.service.reopen(initial.query_ref)

    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_service_rejects_decision_committed_during_broker_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, request = _commit_pending_consent(setup)
    original = InstallConsentBrokerService.prepare

    def publish_then_decide(broker: InstallConsentBrokerService, **kwargs: object) -> object:
        challenge = original(broker, **kwargs)  # type: ignore[arg-type]
        _commit_consent_decision(setup, request)
        return challenge

    monkeypatch.setattr(InstallConsentBrokerService, "prepare", publish_then_decide)
    with pytest.raises(ManagedQueryHeadDriftError, match="stale"):
        setup.service.reopen(initial.query_ref)

    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (1,)


def test_service_rejects_decision_committed_during_broker_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, request = _commit_pending_consent(setup)
    original = InstallConsentBrokerService.status
    decided = False

    def decide_then_status(broker: InstallConsentBrokerService, challenge_id: str) -> object:
        nonlocal decided
        if not decided:
            decided = True
            _commit_consent_decision(setup, request)
        return original(broker, challenge_id)

    monkeypatch.setattr(InstallConsentBrokerService, "status", decide_then_status)
    with pytest.raises(ManagedQueryHeadDriftError, match="stale"):
        setup.service.reopen(initial.query_ref)

    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (1,)


def test_service_projects_only_a_pending_broker_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, _request = _commit_pending_consent(setup)
    original = InstallConsentBrokerService.status

    def non_pending_status(broker: InstallConsentBrokerService, challenge_id: str) -> object:
        return replace(original(broker, challenge_id), state="decision-ready")

    monkeypatch.setattr(InstallConsentBrokerService, "status", non_pending_status)
    calls_before = tuple(setup.facts.calls)
    result = setup.service.reopen(initial.query_ref)

    assert result.challenge is None
    assert tuple(setup.facts.calls) == calls_before
    assert setup.body_source.load_calls == 0


def test_service_fails_closed_when_pending_consent_has_no_publication_authority(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, _request = _commit_pending_consent(setup)
    without_broker = open_managed_query_service(
        registry=setup.registry,
        query_store=setup.store,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        input_authority=setup.inputs,
        policy_store_root=setup.policy_root,
    )

    with pytest.raises(ManagedQueryServiceError, match="publication authorities"):
        without_broker.reopen(initial.query_ref)

    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_service_rejects_pending_consent_without_a_physical_actuator(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, _request = _commit_pending_consent(setup)

    no_actuator = open_managed_query_service(
        registry=setup.registry,
        query_store=setup.store,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        input_authority=setup.inputs,
        consent_broker=setup.consent_broker,
        policy_store_root=setup.policy_root,
    )

    with pytest.raises(RuntimeError, match="no physical installation actuator"):
        no_actuator.reopen(initial.query_ref)

    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_service_rejects_actuator_target_drift_before_broker_publication(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial, _request = _commit_pending_consent(setup)
    skill_root = setup.skill_runtime.skill_store_root
    skill_root.rename(skill_root.with_name("skills-old"))
    skill_root.mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="root changed"):
        setup.service.reopen(initial.query_ref)

    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_service_exact_prepare_retry_and_reopen_plan_only_once(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    request = ManagedQueryRequest(
        logical_query_id=_digest("retry-query"),
        current_work_ref="work-one",
    )

    first = setup.service.prepare(request)
    planned_calls = tuple(setup.facts.calls)
    retried = setup.service.prepare(request)
    reopened = setup.service.reopen(first.query_ref)

    assert setup.facts.calls == list(planned_calls)
    assert first.query_ref == retried.query_ref == reopened.query_ref
    assert first.prepared.decision_digest == retried.prepared.decision_digest
    assert first.prepared.decision_digest == reopened.prepared.decision_digest
    assert first.actions == retried.actions == reopened.actions


@pytest.mark.parametrize("crash_point", ["registered", "revision-one", "committed"])
def test_service_reopen_converges_after_each_pre_mark_crash(
    tmp_path: Path,
    crash_point: str,
) -> None:
    setup = _setup(tmp_path)
    logical_query_id = _digest(f"crash:{crash_point}")
    record = _register(setup, logical_query_id)
    value = setup.managed_input
    if crash_point != "registered":
        with open_managed_engine_composition(
            registry=setup.registry,
            artifact=value.artifact,
            journal_path=setup.journal_path,
            benefit_audit_path=setup.audit_path,
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,  # type: ignore[arg-type]
            install_bundle_port=setup.installs,
        ) as composition:
            if crash_point == "revision-one":
                composition.process(value.session_started)
            else:
                composition.advance_managed_query(
                    session_started=value.session_started,
                    planning_observed=value.decision_event,
                )
    calls_before_reopen = tuple(setup.facts.calls)

    result = setup.service.reopen(record.query_ref)
    planned = setup.store.load(record.query_ref)

    assert planned.planned
    assert result.prepared.plan_id == planned.plan_id
    if crash_point == "committed":
        assert setup.facts.calls == list(calls_before_reopen)
    else:
        assert len(setup.facts.calls) > len(calls_before_reopen)


def test_service_concurrent_same_request_converges_to_one_plan(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    request = ManagedQueryRequest(
        logical_query_id=_digest("concurrent-query"),
        current_work_ref="work-one",
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(setup.service.prepare, (request,) * 8))

    assert {result.query_ref for result in results} == {results[0].query_ref}
    assert {result.prepared.decision_digest for result in results} == {
        results[0].prepared.decision_digest
    }
    planning_calls = tuple(setup.facts.calls)
    setup.service.reopen(results[0].query_ref)
    assert tuple(setup.facts.calls) == planning_calls
    assert set(planning_calls) == {
        "agent:reviewer",
        "harness:python-runner",
        "mcp-server:docs",
        "skill:lint",
        "skill:test",
        "skill:types",
    }


def test_service_concurrent_distinct_logical_ids_reject_one_before_new_planning(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    requests = (
        ManagedQueryRequest(
            logical_query_id=_digest("concurrent-left"),
            current_work_ref="work-one",
        ),
        ManagedQueryRequest(
            logical_query_id=_digest("concurrent-right"),
            current_work_ref="work-one",
        ),
    )

    def attempt(request: ManagedQueryRequest) -> ManagedQueryServiceResult | Exception:
        try:
            return setup.service.prepare(request)
        except Exception as exc:  # the exact typed loser is asserted below
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(attempt, requests))

    assert sum(type(outcome) is ManagedQueryServiceResult for outcome in outcomes) == 1
    assert sum(type(outcome) is ManagedQueryStoreConflict for outcome in outcomes) == 1
    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (1,)


def test_service_rejects_distinct_event_reusing_scope_and_plan_before_mutation(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("scope-plan-owner"),
            current_work_ref="work-one",
        )
    )
    distinct_event = replace(
        setup.managed_input.decision_event,
        event_id="event-distinct-same-plan",
        causation_id="cause-distinct-same-plan",
    )
    setup.inputs.values["same-plan-distinct-event"] = ManagedQueryInput(
        artifact=setup.artifact,
        session_started=setup.managed_input.session_started,
        decision_event=distinct_event,
    )
    calls_before = tuple(setup.facts.calls)
    journal_before = setup.journal_path.read_bytes()

    with pytest.raises(ManagedQueryStoreConflict, match="another logical identity"):
        setup.service.prepare(
            ManagedQueryRequest(
                logical_query_id=_digest("scope-plan-substitute"),
                current_work_ref="same-plan-distinct-event",
            )
        )

    assert tuple(setup.facts.calls) == calls_before
    assert setup.journal_path.read_bytes() == journal_before
    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (1,)


def test_service_rejects_scope_and_plan_reserved_by_unplanned_registration(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    owner = _register(setup, _digest("unplanned-scope-plan-owner"))
    distinct_started = replace(
        setup.managed_input.session_started,
        event_id="event-unplanned-substitute-started",
        causation_id="cause-unplanned-substitute-started",
    )
    distinct_event = replace(
        setup.managed_input.decision_event,
        event_id="event-unplanned-substitute-decision",
        causation_id=distinct_started.event_id,
    )
    setup.inputs.values["unplanned-same-plan-distinct-event"] = ManagedQueryInput(
        artifact=setup.artifact,
        session_started=distinct_started,
        decision_event=distinct_event,
    )

    with pytest.raises(ManagedQueryStoreConflict, match="scope and plan"):
        setup.service.prepare(
            ManagedQueryRequest(
                logical_query_id=_digest("unplanned-scope-plan-substitute"),
                current_work_ref="unplanned-same-plan-distinct-event",
            )
        )

    assert not owner.planned
    assert setup.facts.calls == []
    assert not setup.journal_path.exists()
    assert not setup.audit_path.exists()
    with sqlite3.connect(setup.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (1,)


def test_service_is_host_neutral_for_equivalent_codex_and_claude_refs(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    setup.inputs.values["codex-turn"] = setup.managed_input
    setup.inputs.values["claude-hook"] = setup.managed_input
    logical_id = _digest("host-neutral-query")

    codex = setup.service.prepare(
        ManagedQueryRequest(logical_query_id=logical_id, current_work_ref="codex-turn")
    )
    claude = setup.service.prepare(
        ManagedQueryRequest(logical_query_id=logical_id, current_work_ref="claude-hook")
    )

    assert codex.query_ref == claude.query_ref
    assert codex.prepared.decision_digest == claude.prepared.decision_digest
    assert codex.actions == claude.actions
    assert all(
        setup.facts.calls.count(capability_id) == 2 for capability_id in set(setup.facts.calls)
    )


def test_service_rejects_foreign_registry_handle_before_persistence(tmp_path: Path) -> None:
    owned = _setup(tmp_path / "owned")
    foreign = _setup(tmp_path / "foreign")
    owned.inputs.values["foreign-work"] = foreign.managed_input

    with pytest.raises(ManagedQueryServiceError, match="not issued"):
        owned.service.prepare(
            ManagedQueryRequest(
                logical_query_id=_digest("foreign-query"),
                current_work_ref="foreign-work",
            )
        )

    with sqlite3.connect(owned.store_path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (0,)


def test_service_fails_closed_when_captured_authority_drifts(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    setup.facts.benefit_facts_snapshot_digest = _digest("drifted-facts")

    with pytest.raises(ValueError, match="snapshot does not match"):
        setup.service.prepare(
            ManagedQueryRequest(
                logical_query_id=_digest("drift-query"),
                current_work_ref="work-one",
            )
        )

    assert not setup.journal_path.exists()
    assert not setup.audit_path.exists()


def test_service_factory_rejects_output_path_aliases_before_use(tmp_path: Path) -> None:
    setup = _setup(tmp_path)

    with pytest.raises(ValueError, match="must be distinct"):
        open_managed_query_service(
            registry=setup.registry,
            query_store=setup.store,
            journal_path=setup.store_path,
            benefit_audit_path=setup.audit_path,
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,  # type: ignore[arg-type]
            install_bundle_port=setup.installs,
            input_authority=setup.inputs,
        )


def test_service_factory_with_consent_recovery_authority_does_not_create_journal(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)

    assert not setup.journal_path.exists()
    assert not setup.audit_path.exists()

    colliding_broker = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(
            setup.journal_path,
            audience=CONSENT_AUDIENCE,
        ),
        verifier=None,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="persistence paths must be distinct"):
        open_managed_query_service(
            registry=setup.registry,
            query_store=setup.store,
            journal_path=setup.journal_path,
            benefit_audit_path=setup.audit_path,
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,  # type: ignore[arg-type]
            install_bundle_port=setup.installs,
            input_authority=setup.inputs,
            consent_broker=colliding_broker,
        )
    with pytest.raises(ValueError, match="absolute Path"):
        open_managed_query_service(
            registry=setup.registry,
            query_store=setup.store,
            journal_path=Path("relative-journal.sqlite3"),
            benefit_audit_path=setup.audit_path,
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,  # type: ignore[arg-type]
            install_bundle_port=setup.installs,
            input_authority=setup.inputs,
        )


def test_service_rejects_malformed_or_corrupt_references(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    result = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("corrupt-query"),
            current_work_ref="work-one",
        )
    )
    with pytest.raises(ValueError, match="opaque managed-query reference"):
        setup.service.reopen("../../private/queries.sqlite3")

    with sqlite3.connect(setup.store_path) as connection:
        connection.execute(
            "UPDATE managed_queries SET decision_digest = ? WHERE query_ref = ?",
            (_digest("substituted"), result.query_ref),
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="authentication failed"):
        setup.service.reopen(result.query_ref)


def test_service_public_surface_and_result_do_not_leak_authority_or_content(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path)
    with pytest.raises(ValueError, match="opaque token"):
        ManagedQueryRequest(
            logical_query_id=_digest("privacy-query"),
            current_work_ref=str(tmp_path / "raw prompt and secret"),
        )
    with pytest.raises(TypeError):
        ManagedQueryRequest(  # type: ignore[call-arg]
            logical_query_id=_digest("privacy-query"),
            current_work_ref="work-one",
            raw_prompt="do not persist me",
        )

    result = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("privacy-query"),
            current_work_ref="work-one",
        )
    )

    assert not hasattr(result, "transition")
    assert not hasattr(result, "artifact")
    assert str(tmp_path) not in repr(result)
    assert "work-one" not in repr(result)
    assert all(
        set(action.__slots__) == {"action_id", "entity_id", "kind"} for action in result.actions
    )


def test_service_close_is_idempotent_and_fails_closed(tmp_path: Path) -> None:
    setup = _setup(tmp_path)

    setup.service.close()
    setup.service.close()

    assert setup.service.closed
    with pytest.raises(RuntimeError, match="closed"):
        setup.service.prepare(
            ManagedQueryRequest(
                logical_query_id=_digest("closed-query"),
                current_work_ref="work-one",
            )
        )
    with pytest.raises(RuntimeError, match="closed"):
        setup.service.reopen("mqr_" + "0" * 64)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork contract")
def test_service_is_process_bound(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            setup.service.prepare(
                ManagedQueryRequest(
                    logical_query_id=_digest("forked-query"),
                    current_work_ref="work-one",
                )
            )
        except RuntimeError as exc:
            outcome = b"blocked" if "forked process" in str(exc) else b"wrong-error"
        else:
            outcome = b"allowed"
        os.write(write_fd, outcome)
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        outcome = os.read(read_fd, 32)
    finally:
        os.close(read_fd)
        os.waitpid(child_pid, 0)

    assert outcome == b"blocked"


def test_service_reports_old_plan_as_superseded_without_reranking(tmp_path: Path) -> None:
    setup = _setup(tmp_path)
    first = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("first-plan"),
            current_work_ref="work-one",
        )
    )
    replacement_surrogate = _surrogate(0)
    replacement_artifact = setup.registry.ingest_graph_store(
        graph_store_path=tmp_path / "graph.sqlite3",
        expected_graph_artifact_digest=setup.artifact.graph_artifact_digest,
        planning_environment_digest=setup.artifact.planning_environment_digest,
        catalog_namespace_digest=setup.artifact.catalog_namespace_digest,
        catalog_retrieval_digest=setup.artifact.catalog_retrieval_digest,
        benefit_facts_snapshot_digest=setup.artifact.benefit_facts_snapshot_digest,
        benefit_policy_snapshot_digest=setup.artifact.benefit_policy_snapshot_digest,
        material_snapshot_digest=setup.artifact.material_snapshot_digest,
        installation_snapshot_digest=setup.artifact.installation_snapshot_digest,
        observation_surrogate=replacement_surrogate,
        planning_schema_version=setup.artifact.planning_schema_version,
    )
    original = setup.managed_input.decision_event
    reference = replacement_artifact.observation_reference
    development = EngineEvent(
        event_id="event-development-observed",
        kind="DevelopmentObserved",
        scope=original.scope,
        expected_revision=2,
        occurred_at=NOW,
        payload={
            "observation_ref": {
                "provider_id": reference.provider_id,
                "opaque_id": reference.opaque_id,
                "content_digest": reference.content_digest,
            }
        },
        correlation_id="plan-replacement",
        causation_id=original.event_id,
        privacy=original.privacy,
        engine_version=original.engine_version,
        planner_version=original.planner_version,
        policy_version=original.policy_version,
        host_descriptor_digest=original.host_descriptor_digest,
        catalog_snapshot_digest=original.catalog_snapshot_digest,
        semantic_model_digest=original.semantic_model_digest,
        semantic_index_digest=original.semantic_index_digest,
        work_signature=_digest("replacement-work"),
        random_seed=1,
    )
    setup.inputs.values["work-replacement"] = ManagedQueryInput(
        artifact=replacement_artifact,
        session_started=setup.managed_input.session_started,
        decision_event=development,
    )
    replacement = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("replacement-plan"),
            current_work_ref="work-replacement",
        )
    )
    calls_after_replacement = tuple(setup.facts.calls)

    with pytest.raises(ManagedQuerySupersededError, match="superseded"):
        setup.service.reopen(first.query_ref)

    assert replacement.prepared.plan_id == "plan-replacement"
    assert replacement.prepared.status == "abstained"
    assert replacement.challenge is None
    assert tuple(setup.facts.calls) == calls_after_replacement
    with sqlite3.connect(setup.consent_store_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (0,)


def test_service_summarizes_task_shift_deactivation_without_exposing_authority(
    tmp_path: Path,
) -> None:
    setup = _setup(tmp_path, requested_limit=1, installable=True)
    initial = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("lifecycle-initial"),
            current_work_ref="work-one",
        )
    )
    original = setup.managed_input.decision_event
    bundle = setup.installs.bundles["skill:installable"]
    with open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.artifact,
        journal_path=setup.journal_path,
        benefit_audit_path=setup.audit_path,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,  # type: ignore[arg-type]
        install_bundle_port=setup.installs,
        interactive_install_decision_guard=lambda _reservation: nullcontext(),
        trusted_utc_now=lambda: datetime(2026, 8, 3, 12, 1, tzinfo=UTC),
    ) as composition:
        snapshot = composition.snapshot(original.scope)
        assert snapshot.state is not None
        committed = snapshot.state.committed_plan
        assert isinstance(committed, CommittedPlanV3)
        capability = committed.capabilities[0]
        desired = composition.process(
            replace(
                original,
                event_id="event-lifecycle-desired",
                kind="ReassessmentRequested",
                expected_revision=2,
                payload={
                    "owner_id": "owner-lifecycle",
                    "policy_snapshot_digest": InstallConsentPolicy.safe_default().policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability.capability_id,
                            "source_digest": capability.source_digest,
                            "kind": capability.kind,
                            "actionability": capability.actionability,
                            "install_descriptor_digest": capability.install_descriptor_digest,
                            "install_plan_digest": capability.install_plan_digest,
                            "lease_id": "lease-lifecycle",
                        }
                    ],
                },
            )
        )
        request = desired.actions[0]
        assert request.kind == "RequestConsent"
        granted = composition.process(
            replace(
                original,
                event_id="event-lifecycle-consent-granted",
                kind="UserDecision",
                expected_revision=3,
                payload={
                    "consent_id": request.consent_id,
                    "decision": "granted",
                    "decision_basis": "interactive",
                    "policy_snapshot_digest": InstallConsentPolicy.safe_default().policy_digest,
                    "requested_action_id": request.payload["requested_action_id"],
                    "requested_action_kind": request.payload["requested_action_kind"],
                    "requested_action_content_digest": request.payload[
                        "requested_action_content_digest"
                    ],
                    "requested_action_precondition_revision": request.payload[
                        "requested_action_precondition_revision"
                    ],
                },
            )
        )
        install = granted.actions[0]
        assert install.kind == "InstallCapability"
        binding = InstallExecutionBinding(
            driver_id=bundle.descriptor.installer_id,
            driver_digest=install.payload["installer_digest"],  # type: ignore[arg-type]
            host_identity_digest=_digest("managed-service-host"),
            target_identity_digest=_digest("managed-service-target"),
        )
        engine = composition._engine  # noqa: SLF001 - exact settlement seam under test.
        engine.authorize_install(
            install,
            capability.selection,
            bundle.descriptor,
            expected_catalog_snapshot_digest=composition.catalog_snapshot_digest,
            expected_policy_digest=InstallConsentPolicy.safe_default().policy_digest,
            execution_binding=binding,
        )
        install_guard = engine._record_install_outcome(  # noqa: SLF001
            install,
            execution_binding=binding,
            execution_authority=engine._issue_install_outcome_permit(  # noqa: SLF001
                install,
                binding,
            ),
            outcome="applied",
            observed_material_identity_digest=bundle.result_material.identity_digest,
            verification_digest=_digest("managed-service-install-observation"),
        )
        install_status = engine.install_execution_status(install)
        assert install_status.observed_at is not None
        install_receipt = replace(
            original,
            event_id="event-lifecycle-install-applied",
            kind="ActionApplied",
            expected_revision=4,
            occurred_at=install_status.observed_at,
            payload={
                "action_id": install.action_id,
                "action_kind": install.kind,
                "action_content_digest": install.content_digest,
                "action_precondition_revision": install.precondition_revision,
                "verification": {
                    "schema": INSTALL_RECEIPT_SCHEMA_V3,
                    "host_state": "installed",
                    "capability_id": install.entity_id,
                    "capability_kind": install.payload["capability_kind"],
                    "catalog_identity": install.payload["catalog_identity"],
                    "material_identity": install.payload["result_material"],
                    "install_plan_descriptor": install.payload["install_plan_descriptor"],
                    "installer_digest": install.payload["installer_digest"],
                    "policy_snapshot_digest": install.payload["policy_snapshot_digest"],
                },
            },
        )
        installed = engine.process_install_receipt(install_receipt, install_guard)
        activation = installed.actions[0]
        assert activation.kind == "ActivateCapability"
        engine.authorize_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=setup.managed_input.session_started.host_descriptor_digest
            or "",
        )
        activation_guard = engine._record_activation_outcome(  # noqa: SLF001
            activation,
            execution_binding=binding,
            execution_authority=engine._issue_activation_outcome_permit(  # noqa: SLF001
                activation,
                binding,
            ),
            observed_material_identity_digest=activation.payload["material_identity"][  # type: ignore[index]
                "identity_digest"
            ],
            verification_digest=_digest("managed-service-activation-observation"),
        )
        activation_status = engine.activation_execution_status(activation)
        assert activation_status.observed_at is not None
        activation_receipt = replace(
            original,
            event_id="event-lifecycle-activation-applied",
            kind="ActionApplied",
            expected_revision=5,
            occurred_at=activation_status.observed_at,
            payload={
                "action_id": activation.action_id,
                "action_kind": activation.kind,
                "action_content_digest": activation.content_digest,
                "action_precondition_revision": activation.precondition_revision,
                "verification": {
                    "schema": MATERIAL_RECEIPT_SCHEMA_V3,
                    "host_state": activation.verification["expected_state"],
                    "capability_id": activation.entity_id,
                    "capability_kind": activation.payload["capability_kind"],
                    "catalog_identity": activation.payload["catalog_identity"],
                    "material_identity": activation.payload["material_identity"],
                    "authorized_material": activation.payload["authorized_material"],
                },
            },
        )
        engine.process_activation_receipt(activation_receipt, activation_guard)

    replacement_surrogate = _surrogate(0)
    replacement_artifact = setup.registry.ingest_graph_store(
        graph_store_path=tmp_path / "graph.sqlite3",
        expected_graph_artifact_digest=setup.artifact.graph_artifact_digest,
        planning_environment_digest=setup.artifact.planning_environment_digest,
        catalog_namespace_digest=setup.artifact.catalog_namespace_digest,
        catalog_retrieval_digest=setup.artifact.catalog_retrieval_digest,
        benefit_facts_snapshot_digest=setup.artifact.benefit_facts_snapshot_digest,
        benefit_policy_snapshot_digest=setup.artifact.benefit_policy_snapshot_digest,
        material_snapshot_digest=setup.artifact.material_snapshot_digest,
        installation_snapshot_digest=setup.artifact.installation_snapshot_digest,
        observation_surrogate=replacement_surrogate,
        planning_schema_version=setup.artifact.planning_schema_version,
    )
    reference = replacement_artifact.observation_reference
    development = replace(
        original,
        event_id="event-lifecycle-development",
        kind="DevelopmentObserved",
        expected_revision=6,
        payload={
            "observation_ref": {
                "provider_id": reference.provider_id,
                "opaque_id": reference.opaque_id,
                "content_digest": reference.content_digest,
            }
        },
        correlation_id="plan-lifecycle-development",
        causation_id="event-lifecycle-activation-applied",
        work_signature=_digest("lifecycle-development-work"),
    )
    setup.inputs.values["work-lifecycle-development"] = ManagedQueryInput(
        artifact=replacement_artifact,
        session_started=setup.managed_input.session_started,
        decision_event=development,
    )
    shifted = setup.service.prepare(
        ManagedQueryRequest(
            logical_query_id=_digest("lifecycle-development"),
            current_work_ref="work-lifecycle-development",
        )
    )
    calls_after_shift = tuple(setup.facts.calls)
    reopened = setup.service.reopen(shifted.query_ref)

    assert initial.prepared.status == "ready"
    assert shifted.prepared.status == "abstained"
    assert [(action.kind, action.entity_id) for action in shifted.actions] == [
        ("DeactivateCapability", "skill:installable")
    ]
    assert shifted.actions == reopened.actions
    assert tuple(setup.facts.calls) == calls_after_shift
    assert not hasattr(shifted.actions[0], "payload")
