from __future__ import annotations

import hashlib
import inspect
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import networkx as nx
import pytest
import ctx.runtime.composition as composition_module

from ctx.core.graph.graph_store import build_graph_store
from ctx.core.install_policy_store import persist_install_policy
from ctx.core.resolve.engine_candidates import IndexedGraphCandidateSource
from ctx.core.resolve.engine_content import AuthenticatedCatalogContentSource
from ctx.engine.benefit import (
    BenefitCandidate,
    EvidenceSummary,
    NetBenefitPolicy,
    ResourceCosts,
)
from ctx.engine.benefit_audit_store import SQLiteBenefitAuditStore
from ctx.engine.content import MaterialDescriptor, MaterialIdentity
from ctx.engine.engine import CtxEngineError
from ctx.engine.installation import (
    InstallConsentPolicy,
    InstallPlanDescriptor,
    InstallPlanningBundle,
    InteractiveInstallDecisionReservation,
    PreparedInstallPlan,
)
from ctx.engine.planner import (
    CandidateAuthorityUnavailable,
    CandidateSourceUnavailable,
    CapabilityCandidate,
    PlannerValidationError,
    WorkObservation,
)
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.replay import (
    ObservationReference,
    ReplayValidationError,
    StructuredSurrogate,
)
from ctx.engine.state import CapabilityStateV3, CommittedPlanV3, EngineState
from ctx.runtime import (
    AgentFileRuntimeConfig,
    EngineComposition,
    SkillCasRuntimeConfig,
    open_engine_composition,
)
from ctx.runtime.composition import open_managed_engine_composition
from ctx.runtime.install_execution import InstallDriverRequest
from ctx.runtime.managed_artifact_registry import (
    ManagedArtifactHandle,
    ManagedArtifactRegistry,
    ManagedArtifactRegistryError,
    open_managed_artifact_registry,
)
from ctx.runtime.managed_query import PreparedManagedQuery


NOW = "2026-08-01T12:00:00Z"
CATALOG_NAMESPACE_DIGEST = hashlib.sha256(b"catalog-namespace-v1").hexdigest()
_REQUIRES_POSIX_MANAGED_REGISTRY = pytest.mark.skipif(
    os.name == "nt",
    reason="managed artifact registry requires POSIX filesystem semantics",
)


def _trusted_now() -> datetime:
    return datetime(2026, 8, 1, 12, 30, tzinfo=UTC)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _artifact_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy() -> NetBenefitPolicy:
    return NetBenefitPolicy(
        calibration_digest=_digest("calibration-v1"),
        minimum_relevance_ppm=1,
    )


@dataclass
class _AuthenticatedFacts:
    benefit_facts_snapshot_digest: str = field(
        default_factory=lambda: _digest("authenticated-benefit-facts-v1")
    )
    expected_task_benefit_ppm: int = 700_000
    missing: bool = False
    calls: list[str] = field(default_factory=list)

    def benefit_candidate(
        self,
        presentation: CapabilityCandidate,
        _observation: WorkObservation,
    ) -> BenefitCandidate | None:
        self.calls.append(presentation.capability_id)
        if self.missing:
            return None
        return BenefitCandidate(
            capability_id=presentation.capability_id,
            source_digest=presentation.source_digest,
            resource_profile_digest=_digest(f"profile:{presentation.capability_id}"),
            availability=("advisory" if presentation.actionability == "manual" else "executable"),
            expected_task_benefit_ppm=self.expected_task_benefit_ppm,
            relevance_ppm=1_000_000,
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


def _graph_artifact(tmp_path: Path) -> Path:
    graph = nx.Graph()
    graph.add_node(
        "skill:python-tdd",
        label="python-tdd",
        type="skill",
        tags=["python", "testing", "unit"],
    )
    path = tmp_path / "graph-store.sqlite3"
    build_graph_store(path, graph)
    path.chmod(0o444)
    return path


def _two_skill_graph_artifact(tmp_path: Path) -> Path:
    graph = nx.Graph()
    for name in ("python-bad", "python-good"):
        graph.add_node(
            f"skill:{name}",
            label=name,
            type="skill",
            tags=["python", "testing", "unit"],
        )
    path = tmp_path / "two-skill-graph-store.sqlite3"
    build_graph_store(path, graph)
    path.chmod(0o444)
    return path


def _agent_graph_artifact(tmp_path: Path) -> Path:
    graph = nx.Graph()
    graph.add_node(
        "agent:reviewer",
        label="reviewer",
        type="agent",
        tags=["python", "testing", "unit"],
    )
    path = tmp_path / "agent-graph-store.sqlite3"
    build_graph_store(path, graph)
    path.chmod(0o444)
    return path


def _cross_type_graph_artifact(tmp_path: Path) -> Path:
    graph = nx.Graph()
    for capability_id, kind in (
        ("skill:python-tdd", "skill"),
        ("agent:reviewer", "agent"),
        ("mcp-server:docs", "mcp-server"),
        ("harness:python-runner", "harness"),
    ):
        graph.add_node(
            capability_id,
            label=capability_id.split(":", 1)[1],
            type=kind,
            tags=["python", "testing", "unit"],
        )
    path = tmp_path / "cross-type-graph-store.sqlite3"
    build_graph_store(path, graph)
    path.chmod(0o444)
    return path


def _normalizer(
    _reference: ObservationReference,
    _state: EngineState | None,
) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": ["python", "testing", "unit"],
            "languages": ["python"],
            "baseline_capability_ids": [],
            "active_capability_ids": [],
            "rejected_capability_ids": [],
            "requested_limit": 5,
        },
    )


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id="host-neutral",
    )


def _event(
    composition: EngineComposition,
    kind: str,
    revision: int,
    event_id: str,
    *,
    payload: dict[str, object] | None = None,
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=_scope(),
        expected_revision=revision,
        occurred_at=NOW,
        payload=payload or {},
        correlation_id="plan-1",
        causation_id="cause-1",
        engine_version="ctx-engine-v1",
        planner_version=composition.planner_version,
        policy_version="policy-v1",
        host_descriptor_digest=_digest("host-neutral-managing-v1"),
        catalog_snapshot_digest=composition.catalog_snapshot_digest,
        semantic_model_digest=_digest("semantic-disabled"),
        semantic_index_digest=_digest("semantic-index-disabled"),
        work_signature=_digest("python-testing-unit"),
        random_seed=0,
    )


def _plan(composition: EngineComposition) -> tuple[dict[str, object], ...]:
    composition.process(
        _event(
            composition,
            "SessionStarted",
            0,
            "event-start",
            payload={"host_level": "managing"},
        )
    )
    transition = composition.process(
        _event(
            composition,
            "IntentObserved",
            1,
            "event-intent",
            payload={
                "observation_ref": {
                    "provider_id": "test-buffer",
                    "opaque_id": "observation-1",
                    "content_digest": _digest("python-testing-unit"),
                }
            },
        )
    )
    assert [action.kind for action in transition.actions] == ["PresentBundle"]
    return transition.actions[0].payload["capabilities"]  # type: ignore[return-value]


def _plan_with_observation_reference(
    composition: EngineComposition,
    reference: ObservationReference,
) -> tuple[dict[str, object], ...]:
    composition.process(
        _event(
            composition,
            "SessionStarted",
            0,
            "event-managed-start",
            payload={"host_level": "managing"},
        )
    )
    transition = composition.process(
        _event(
            composition,
            "IntentObserved",
            1,
            "event-managed-intent",
            payload={
                "observation_ref": {
                    "provider_id": reference.provider_id,
                    "opaque_id": reference.opaque_id,
                    "content_digest": reference.content_digest,
                }
            },
        )
    )
    assert [action.kind for action in transition.actions] == ["PresentBundle"]
    return transition.actions[0].payload["capabilities"]  # type: ignore[return-value]


def _material_port(tmp_path: Path) -> AuthenticatedCatalogContentSource:
    content = b"---\nname: python-tdd\n---\nUse a failing test first.\n"
    skill = tmp_path / "materials" / "python-tdd" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_bytes(content)
    return AuthenticatedCatalogContentSource(
        tmp_path / "materials",
        [
            {
                "capability_id": "skill:python-tdd",
                "path": "python-tdd/SKILL.md",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        ],
    )


def _load_descriptor_for(
    capability_id: str,
    snapshot_digest: str,
) -> MaterialDescriptor:
    kind = capability_id.split(":", 1)[0]
    identity = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"content:{capability_id}"),
        content_bytes=64,
    )
    return MaterialDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        actionability="load",
        content_sha256=identity.content_sha256,
        content_bytes=identity.content_bytes,
        estimated_tokens=16,
        provenance_digest=snapshot_digest,
        material_identity_digest=identity.identity_digest,
    )


@dataclass
class _StaticInstallBundlePort:
    installation_snapshot_digest: str
    bundles: dict[str, InstallPlanningBundle | None]
    remove_after_first_lookup: bool = False
    describe_bundle_calls: list[tuple[str, str]] = field(default_factory=list)

    def describe_bundle(
        self,
        capability_id: str,
        kind: str,
    ) -> InstallPlanningBundle | None:
        self.describe_bundle_calls.append((capability_id, kind))
        bundle = self.bundles.get(capability_id)
        if self.remove_after_first_lookup and len(self.describe_bundle_calls) == 1:
            self.bundles[capability_id] = None
        return bundle

    def describe(self, capability_id: str, kind: str) -> InstallPlanDescriptor | None:
        bundle = self.describe_bundle(capability_id, kind)
        return None if bundle is None else bundle.descriptor

    def prepare(self, *_args: object, **_kwargs: object) -> PreparedInstallPlan:
        raise RuntimeError("static test install port cannot prepare physical installation")


def _install_port() -> tuple[_StaticInstallBundlePort, InstallPlanningBundle]:
    snapshot_digest = _digest("install-catalog-v1")
    result_material = MaterialIdentity.create(
        capability_id="skill:python-tdd",
        kind="skill",
        content_sha256=_digest("installed-python-tdd-material"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id="skill:python-tdd",
        kind="skill",
        installer_id="ctx-skill-installer-v1",
        plan_digest=_digest("install-plan-v1"),
        provenance_digest=snapshot_digest,
        result_material_identity_digest=result_material.identity_digest,
    )
    bundle = InstallPlanningBundle(
        descriptor=descriptor,
        result_material=result_material,
    )
    return (
        _StaticInstallBundlePort(
            installation_snapshot_digest=snapshot_digest,
            bundles={descriptor.capability_id: bundle},
        ),
        bundle,
    )


def _install_bundle_for(
    capability_id: str,
    snapshot_digest: str,
) -> InstallPlanningBundle:
    kind = capability_id.split(":", 1)[0]
    result_material = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"installed:{capability_id}"),
        content_bytes=128,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id="ctx-skill-installer-v1",
        plan_digest=_digest(f"install-plan:{capability_id}"),
        provenance_digest=snapshot_digest,
        result_material_identity_digest=result_material.identity_digest,
    )
    return InstallPlanningBundle(
        descriptor=descriptor,
        result_material=result_material,
    )


def _open_composition(
    tmp_path: Path,
    graph: Path,
    *,
    facts: _AuthenticatedFacts | None = None,
    material_port: AuthenticatedCatalogContentSource | None = None,
    install_bundle_port: _StaticInstallBundlePort | None = None,
    journal_name: str = "journal.sqlite3",
    audit_name: str = "benefit-audit.sqlite3",
    graph_artifact_sha256: str | None = None,
    planner_version: str = "ctx-authenticated-net-benefit-planner-v3",
    policy_store_root: Path | None = None,
    interactive_install_decision_guard: object | None = None,
    trusted_utc_now: object | None = None,
    skill_cas_runtime: SkillCasRuntimeConfig | None = None,
    agent_file_runtime: AgentFileRuntimeConfig | None = None,
) -> EngineComposition:
    return open_engine_composition(
        graph_artifact_path=graph,
        graph_artifact_sha256=graph_artifact_sha256 or _artifact_digest(graph),
        journal_path=tmp_path / journal_name,
        observation_normalizer=_normalizer,
        benefit_facts_port=facts or _AuthenticatedFacts(),
        net_benefit_policy=_policy(),
        catalog_namespace_digest=CATALOG_NAMESPACE_DIGEST,
        benefit_audit_path=tmp_path / audit_name,
        material_port=material_port,
        install_bundle_port=install_bundle_port,
        planner_version=planner_version,
        policy_store_root=policy_store_root,
        interactive_install_decision_guard=interactive_install_decision_guard,  # type: ignore[arg-type]
        trusted_utc_now=trusted_utc_now,  # type: ignore[arg-type]
        skill_cas_runtime=skill_cas_runtime,
        agent_file_runtime=agent_file_runtime,
    )


@dataclass(frozen=True)
class _ManagedCompositionSetup:
    registry: ManagedArtifactRegistry
    handle: ManagedArtifactHandle
    facts: _AuthenticatedFacts
    policy: NetBenefitPolicy
    materials: AuthenticatedCatalogContentSource
    installs: _StaticInstallBundlePort
    retrieval_digest: str
    planning_environment_digest: str
    legacy_capabilities: tuple[dict[str, object], ...] | None


def _managed_composition_setup(
    tmp_path: Path,
    *,
    declared_retrieval_digest: str | None = None,
    declared_planning_environment_digest: str | None = None,
    plan_legacy: bool = False,
) -> _ManagedCompositionSetup:
    graph = _graph_artifact(tmp_path)
    graph.chmod(0o400)
    graph_digest = _artifact_digest(graph)
    facts = _AuthenticatedFacts()
    policy = _policy()
    materials = _material_port(tmp_path)
    installs = _StaticInstallBundlePort(
        installation_snapshot_digest=_digest("empty-install-catalog-v1"),
        bundles={},
    )
    planner_version = "ctx-authenticated-net-benefit-planner-v3"
    with IndexedGraphCandidateSource(
        graph,
        graph_digest,
        material_port=materials,
        install_plan_port=installs,
    ) as source:
        retrieval_digest = source.catalog_snapshot_digest
    legacy_capabilities = None
    with _open_composition(
        tmp_path,
        graph,
        facts=facts,
        material_port=materials,
        install_bundle_port=installs,
        journal_name="legacy-managed-journal.sqlite3",
        audit_name="legacy-managed-audit.sqlite3",
        planner_version=planner_version,
    ) as legacy:
        planning_environment_digest = legacy.catalog_snapshot_digest
        if plan_legacy:
            legacy_capabilities = _plan(legacy)

    surrogate = _normalizer(
        ObservationReference(
            provider_id="test-buffer",
            opaque_id="observation-1",
            content_digest=_digest("python-testing-unit"),
        ),
        None,
    )
    registry_root = tmp_path / "managed-registry"
    first_registry = open_managed_artifact_registry(root=registry_root)
    first_handle = first_registry.ingest_graph_store(
        graph_store_path=graph,
        expected_graph_artifact_digest=graph_digest,
        planning_environment_digest=(
            declared_planning_environment_digest or planning_environment_digest
        ),
        catalog_namespace_digest=CATALOG_NAMESPACE_DIGEST,
        catalog_retrieval_digest=declared_retrieval_digest or retrieval_digest,
        benefit_facts_snapshot_digest=facts.benefit_facts_snapshot_digest,
        benefit_policy_snapshot_digest=policy.policy_digest,
        material_snapshot_digest=materials.material_snapshot_digest,
        installation_snapshot_digest=installs.installation_snapshot_digest,
        observation_surrogate=surrogate,
        planning_schema_version=planner_version,
    )
    graph.unlink()
    restarted_registry = open_managed_artifact_registry(root=registry_root)
    restarted_handle = restarted_registry.reopen(
        manifest_digest=first_handle.manifest_digest,
        planning_environment_digest=first_handle.planning_environment_digest,
    )
    return _ManagedCompositionSetup(
        registry=restarted_registry,
        handle=restarted_handle,
        facts=facts,
        policy=policy,
        materials=materials,
        installs=installs,
        retrieval_digest=retrieval_digest,
        planning_environment_digest=planning_environment_digest,
        legacy_capabilities=legacy_capabilities,
    )


def test_managed_composition_factory_is_artifact_pathless() -> None:
    parameters = inspect.signature(open_managed_engine_composition).parameters

    for forbidden in (
        "graph_artifact_path",
        "graph_artifact_sha256",
        "observation_normalizer",
        "catalog_namespace_digest",
        "planner_version",
    ):
        assert forbidden not in parameters
    assert {
        "registry",
        "artifact",
        "journal_path",
        "benefit_audit_path",
        "benefit_facts_port",
        "net_benefit_policy",
        "material_port",
        "install_bundle_port",
    } <= set(parameters)


@_REQUIRES_POSIX_MANAGED_REGISTRY
def test_managed_composition_reopens_pathless_inputs_with_legacy_equivalence(
    tmp_path: Path,
) -> None:
    setup = _managed_composition_setup(tmp_path, plan_legacy=True)
    with open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.handle,
        journal_path=tmp_path / "restarted-managed-journal.sqlite3",
        benefit_audit_path=tmp_path / "restarted-managed-audit.sqlite3",
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,
        install_bundle_port=setup.installs,
    ) as managed:
        managed_capabilities = _plan_with_observation_reference(
            managed,
            setup.handle.observation_reference,
        )
        assert type(managed) is EngineComposition
        assert managed.catalog_snapshot_digest == setup.planning_environment_digest

    assert managed_capabilities == setup.legacy_capabilities
    assert managed.closed is True
    assert not tuple((tmp_path / "managed-registry").glob("**/.ctx-indexed-snapshot-*"))


@_REQUIRES_POSIX_MANAGED_REGISTRY
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX process restart semantics")
def test_managed_composition_reopens_with_a_fresh_handle_after_process_restart(
    tmp_path: Path,
) -> None:
    setup = _managed_composition_setup(tmp_path)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions execute in the parent
        os.close(read_fd)
        try:
            registry = open_managed_artifact_registry(root=tmp_path / "managed-registry")
            handle = registry.reopen(
                manifest_digest=setup.handle.manifest_digest,
                planning_environment_digest=setup.handle.planning_environment_digest,
            )
            materials = _material_port(tmp_path)
            installs = _StaticInstallBundlePort(
                installation_snapshot_digest=setup.installs.installation_snapshot_digest,
                bundles={},
            )
            with open_managed_engine_composition(
                registry=registry,
                artifact=handle,
                journal_path=tmp_path / "child-managed-journal.sqlite3",
                benefit_audit_path=tmp_path / "child-managed-audit.sqlite3",
                benefit_facts_port=_AuthenticatedFacts(),
                net_benefit_policy=_policy(),
                material_port=materials,
                install_bundle_port=installs,
            ) as composition:
                if composition.catalog_snapshot_digest != setup.planning_environment_digest:
                    raise AssertionError("restarted composition changed planning identity")
            os.write(write_fd, b"ok")
            os._exit(0)
        except BaseException as exc:
            os.write(write_fd, f"{type(exc).__name__}:{exc}".encode("utf-8")[:2048])
            os._exit(1)

    os.close(write_fd)
    child_result = os.read(read_fd, 2048)
    os.close(read_fd)
    waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0, child_result.decode("utf-8")
    assert child_result == b"ok"


@_REQUIRES_POSIX_MANAGED_REGISTRY
@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_managed_composition_rejects_every_inherited_child_operation(
    tmp_path: Path,
) -> None:
    setup = _managed_composition_setup(tmp_path)
    journal = tmp_path / "fork-inherited-journal.sqlite3"
    audit = tmp_path / "fork-inherited-audit.sqlite3"
    composition = open_managed_engine_composition(
        registry=setup.registry,
        artifact=setup.handle,
        journal_path=journal,
        benefit_audit_path=audit,
        benefit_facts_port=setup.facts,
        net_benefit_policy=setup.policy,
        material_port=setup.materials,
        install_bundle_port=setup.installs,
    )
    session_started = _event(
        composition,
        "SessionStarted",
        0,
        "fork-inherited-start",
        payload={"host_level": "managing"},
    )
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - assertions execute in the parent
        os.close(read_fd)
        try:
            operations = (
                lambda: composition.closed,
                lambda: composition.process(session_started),
                lambda: composition.snapshot(_scope()),
                lambda: composition.prepare_managed_query(
                    session_started=session_started,
                    intent_observed=session_started,
                ),
                lambda: composition.authorize_exposure(
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    expected_catalog_snapshot_digest=composition.catalog_snapshot_digest,
                ),
                lambda: composition.execute_install(
                    object(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    expected_policy_digest=_digest("unused-policy"),
                ),
                composition.__enter__,
                lambda: composition.__exit__(None, None, None),
                composition.close,
            )
            for operation in operations:
                try:
                    operation()
                except RuntimeError as exc:
                    if "forked process" not in str(exc):
                        raise
                else:
                    raise AssertionError("inherited composition operation was accepted")
            os.write(write_fd, b"ok")
            os._exit(0)
        except BaseException as exc:
            os.write(write_fd, f"{type(exc).__name__}:{exc}".encode("utf-8")[:2048])
            os._exit(1)

    os.close(write_fd)
    child_result = os.read(read_fd, 2048)
    os.close(read_fd)
    waited_pid, status = os.waitpid(child_pid, 0)
    try:
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == 0, child_result.decode("utf-8")
        assert child_result == b"ok"
        assert composition.closed is False
        snapshot = composition.snapshot(_scope())
        assert snapshot.revision == 0
        assert snapshot.state is None
        assert not audit.exists()
    finally:
        composition.close()


@_REQUIRES_POSIX_MANAGED_REGISTRY
@pytest.mark.parametrize(
    ("binding", "message"),
    [
        ("benefit-facts", "benefit facts snapshot"),
        ("benefit-policy", "benefit policy snapshot"),
        ("material", "material snapshot"),
        ("installation", "installation snapshot"),
    ],
)
def test_managed_composition_rejects_authority_snapshot_substitution_before_outputs(
    tmp_path: Path,
    binding: str,
    message: str,
) -> None:
    setup = _managed_composition_setup(tmp_path)
    facts = setup.facts
    policy = setup.policy
    if binding == "benefit-facts":
        facts.benefit_facts_snapshot_digest = _digest("substituted-benefit-facts")
    elif binding == "benefit-policy":
        policy = NetBenefitPolicy(
            calibration_digest=_digest("substituted-calibration"),
            minimum_relevance_ppm=1,
        )
    elif binding == "material":
        setup.materials.material_snapshot_digest = _digest("substituted-materials")
    else:
        setup.installs.installation_snapshot_digest = _digest("substituted-installs")
    journal = tmp_path / f"{binding}-managed-journal.sqlite3"
    audit = tmp_path / f"{binding}-managed-audit.sqlite3"

    with pytest.raises(ValueError, match=message):
        open_managed_engine_composition(
            registry=setup.registry,
            artifact=setup.handle,
            journal_path=journal,
            benefit_audit_path=audit,
            benefit_facts_port=facts,
            net_benefit_policy=policy,
            material_port=setup.materials,
            install_bundle_port=setup.installs,
        )

    assert not journal.exists()
    assert not audit.exists()
    assert not tuple((tmp_path / "managed-registry").glob("**/.ctx-indexed-snapshot-*"))


@_REQUIRES_POSIX_MANAGED_REGISTRY
@pytest.mark.parametrize(
    ("missing_port", "message"),
    [("material", "material_port.*contract"), ("installation", "install bundle contract")],
)
def test_managed_composition_requires_both_concrete_authority_ports(
    tmp_path: Path,
    missing_port: str,
    message: str,
) -> None:
    setup = _managed_composition_setup(tmp_path)

    with pytest.raises(TypeError, match=message):
        open_managed_engine_composition(
            registry=setup.registry,
            artifact=setup.handle,
            journal_path=tmp_path / "missing-port-journal.sqlite3",
            benefit_audit_path=tmp_path / "missing-port-audit.sqlite3",
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=(None if missing_port == "material" else setup.materials),  # type: ignore[arg-type]
            install_bundle_port=(None if missing_port == "installation" else setup.installs),  # type: ignore[arg-type]
        )


@_REQUIRES_POSIX_MANAGED_REGISTRY
def test_managed_composition_rejects_a_foreign_registry_handle_before_outputs(
    tmp_path: Path,
) -> None:
    setup = _managed_composition_setup(tmp_path)
    foreign = open_managed_artifact_registry(root=tmp_path / "foreign-registry")
    journal = tmp_path / "foreign-journal.sqlite3"
    audit = tmp_path / "foreign-audit.sqlite3"

    with pytest.raises(ManagedArtifactRegistryError, match="not issued"):
        open_managed_engine_composition(
            registry=foreign,
            artifact=setup.handle,
            journal_path=journal,
            benefit_audit_path=audit,
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,
            install_bundle_port=setup.installs,
        )

    assert not journal.exists()
    assert not audit.exists()


@_REQUIRES_POSIX_MANAGED_REGISTRY
def test_managed_composition_recomputes_catalog_retrieval_before_outputs(
    tmp_path: Path,
) -> None:
    setup = _managed_composition_setup(
        tmp_path,
        declared_retrieval_digest=_digest("substituted-retrieval"),
    )
    journal = tmp_path / "retrieval-journal.sqlite3"
    audit = tmp_path / "retrieval-audit.sqlite3"

    with pytest.raises(ManagedArtifactRegistryError, match="catalog retrieval digest"):
        open_managed_engine_composition(
            registry=setup.registry,
            artifact=setup.handle,
            journal_path=journal,
            benefit_audit_path=audit,
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,
            install_bundle_port=setup.installs,
        )

    assert not journal.exists()
    assert not audit.exists()
    assert not tuple((tmp_path / "managed-registry").glob("**/.ctx-indexed-snapshot-*"))


@_REQUIRES_POSIX_MANAGED_REGISTRY
def test_managed_composition_recomputes_planning_environment_before_outputs(
    tmp_path: Path,
) -> None:
    setup = _managed_composition_setup(
        tmp_path,
        declared_planning_environment_digest=_digest("substituted-environment"),
    )
    journal = tmp_path / "environment-journal.sqlite3"
    audit = tmp_path / "environment-audit.sqlite3"

    with pytest.raises(ValueError, match="planning environment"):
        open_managed_engine_composition(
            registry=setup.registry,
            artifact=setup.handle,
            journal_path=journal,
            benefit_audit_path=audit,
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,
            install_bundle_port=setup.installs,
        )

    assert not journal.exists()
    assert not audit.exists()
    assert not tuple((tmp_path / "managed-registry").glob("**/.ctx-indexed-snapshot-*"))


@_REQUIRES_POSIX_MANAGED_REGISTRY
def test_managed_composition_temporal_fact_drift_leaves_no_output_store(
    tmp_path: Path,
) -> None:
    setup = _managed_composition_setup(tmp_path)

    class _TemporallyChangingFacts:
        def __init__(self) -> None:
            self.digest_reads = 0

        @property
        def benefit_facts_snapshot_digest(self) -> str:
            self.digest_reads += 1
            if self.digest_reads <= 3:
                return setup.handle.benefit_facts_snapshot_digest
            return _digest("temporally-substituted-benefit-facts")

        def benefit_candidate(
            self,
            _presentation: CapabilityCandidate,
            _observation: WorkObservation,
        ) -> None:
            raise AssertionError("composition validation must not query benefit facts")

    facts = _TemporallyChangingFacts()
    journal = tmp_path / "temporal-drift-journal.sqlite3"
    audit = tmp_path / "temporal-drift-audit.sqlite3"

    with pytest.raises(ValueError, match="planning environment changed"):
        open_managed_engine_composition(
            registry=setup.registry,
            artifact=setup.handle,
            journal_path=journal,
            benefit_audit_path=audit,
            benefit_facts_port=facts,  # type: ignore[arg-type]
            net_benefit_policy=setup.policy,
            material_port=setup.materials,
            install_bundle_port=setup.installs,
        )

    assert facts.digest_reads == 4
    assert not journal.exists()
    assert not audit.exists()
    assert not tuple((tmp_path / "managed-registry").glob("**/.ctx-indexed-snapshot-*"))


@_REQUIRES_POSIX_MANAGED_REGISTRY
def test_managed_composition_preserves_binding_error_when_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = _managed_composition_setup(
        tmp_path,
        declared_retrieval_digest=_digest("substituted-retrieval"),
    )
    original_close = IndexedGraphCandidateSource.close

    def cleanup_then_fail(source: IndexedGraphCandidateSource) -> None:
        original_close(source)
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(IndexedGraphCandidateSource, "close", cleanup_then_fail)
    with pytest.raises(
        ManagedArtifactRegistryError,
        match="catalog retrieval digest",
    ) as captured:
        open_managed_engine_composition(
            registry=setup.registry,
            artifact=setup.handle,
            journal_path=tmp_path / "cleanup-journal.sqlite3",
            benefit_audit_path=tmp_path / "cleanup-audit.sqlite3",
            benefit_facts_port=setup.facts,
            net_benefit_policy=setup.policy,
            material_port=setup.materials,
            install_bundle_port=setup.installs,
        )

    assert captured.value.__notes__ == ["managed indexed-source cleanup also failed with OSError"]


def test_composition_plans_authenticated_loadable_material(tmp_path: Path) -> None:
    graph = _graph_artifact(tmp_path)
    facts = _AuthenticatedFacts()
    audit_path = tmp_path / "benefit-audit.sqlite3"

    with _open_composition(
        tmp_path,
        graph,
        facts=facts,
        material_port=_material_port(tmp_path),
    ) as composition:
        capabilities = _plan(composition)
        snapshot = composition.snapshot(_scope())

    assert len(capabilities) == 1
    assert capabilities[0]["capability_id"] == "skill:python-tdd"
    assert capabilities[0]["actionability"] == "load"
    assert capabilities[0]["install_descriptor_digest"] is None
    assert capabilities[0]["install_plan_digest"] is None
    assert capabilities[0]["authority"]["type"] == "load"  # type: ignore[index]
    assert capabilities[0]["benefit"]["tier"] == "executable"  # type: ignore[index]
    # The schema-v3 adapter validates every authenticated read again before
    # emitting its first plan, so unchanged snapshot digests cannot hide drift.
    assert facts.calls == ["skill:python-tdd", "skill:python-tdd"]
    assert snapshot.state is not None
    assert isinstance(snapshot.state.committed_plan, CommittedPlanV3)
    audit = snapshot.state.committed_plan.benefit_audit
    assert audit is not None
    persisted = SQLiteBenefitAuditStore(audit_path).load(audit.result_digest)
    assert persisted.result_digest == audit.result_digest
    assert persisted.policy_digest == _policy().policy_digest


def test_composition_prepares_one_safe_plan_under_owned_graph_lifetime(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)

    with _open_composition(tmp_path, graph) as composition:
        session_started = _event(
            composition,
            "SessionStarted",
            0,
            "managed-start",
            payload={"host_level": "managing"},
        )
        intent_observed = _event(
            composition,
            "IntentObserved",
            1,
            "managed-intent",
            payload={
                "observation_ref": {
                    "provider_id": "test-buffer",
                    "opaque_id": "managed-observation",
                    "content_digest": _digest("python-testing-unit"),
                }
            },
        )

        prepared = composition.prepare_managed_query(
            session_started=session_started,
            intent_observed=intent_observed,
        )
        snapshot = composition.snapshot(_scope())

    assert type(prepared) is PreparedManagedQuery
    assert prepared.journal_revision == snapshot.revision == 2
    assert prepared.journal_record_digest == snapshot.record_digest
    assert tuple(item.capability_id for item in prepared.selections) == ("skill:python-tdd",)
    assert not hasattr(prepared.selections[0], "authority")


def test_composition_prepares_one_global_cross_type_plan_from_real_graph(
    tmp_path: Path,
) -> None:
    graph = _cross_type_graph_artifact(tmp_path)

    with _open_composition(tmp_path, graph) as composition:
        prepared = composition.prepare_managed_query(
            session_started=_event(
                composition,
                "SessionStarted",
                0,
                "cross-type-start",
                payload={"host_level": "managing"},
            ),
            intent_observed=_event(
                composition,
                "IntentObserved",
                1,
                "cross-type-intent",
                payload={
                    "observation_ref": {
                        "provider_id": "test-buffer",
                        "opaque_id": "cross-type-observation",
                        "content_digest": _digest("python-testing-unit"),
                    }
                },
            ),
        )

    assert len(prepared.selections) == 4
    assert {item.kind for item in prepared.selections} == {
        "skill",
        "agent",
        "mcp-server",
        "harness",
    }
    assert {item.actionability for item in prepared.selections} == {"manual"}


def test_composition_skips_malformed_load_authority_and_keeps_valid_peer(
    tmp_path: Path,
) -> None:
    graph = _two_skill_graph_artifact(tmp_path)
    bad_capability_id = "skill:python-bad"
    good_capability_id = "skill:python-good"
    snapshot_digest = _digest("mixed-material-snapshot")
    identity = MaterialIdentity.create(
        capability_id=good_capability_id,
        kind="skill",
        content_sha256=_digest("python-good-content"),
        content_bytes=64,
    )
    good_descriptor = MaterialDescriptor.create(
        capability_id=good_capability_id,
        kind="skill",
        actionability="load",
        content_sha256=identity.content_sha256,
        content_bytes=identity.content_bytes,
        estimated_tokens=16,
        provenance_digest=snapshot_digest,
        material_identity_digest=identity.identity_digest,
    )

    class _MixedMaterialPort:
        material_snapshot_digest = snapshot_digest

        def describe(self, capability_id: str, _kind: str) -> MaterialDescriptor:
            if capability_id == bad_capability_id:
                return cast(MaterialDescriptor, object())
            return good_descriptor

        def prepare(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("planning must not prepare material")

    with _open_composition(
        tmp_path,
        graph,
        material_port=cast(AuthenticatedCatalogContentSource, _MixedMaterialPort()),
    ) as composition:
        capabilities = _plan(composition)

    assert [row["capability_id"] for row in capabilities] == [good_capability_id]
    assert capabilities[0]["authority"]["type"] == "load"  # type: ignore[index]
    assert all(row["capability_id"] != bad_capability_id for row in capabilities)


def test_pinned_material_port_treats_valid_to_malformed_as_global_drift() -> None:
    capability_id = "skill:python-bad"
    snapshot_digest = _digest("changing-material-snapshot")
    identity = MaterialIdentity.create(
        capability_id=capability_id,
        kind="skill",
        content_sha256=_digest("changing-material-content"),
        content_bytes=64,
    )
    descriptor = MaterialDescriptor.create(
        capability_id=capability_id,
        kind="skill",
        actionability="load",
        content_sha256=identity.content_sha256,
        content_bytes=identity.content_bytes,
        estimated_tokens=16,
        provenance_digest=snapshot_digest,
        material_identity_digest=identity.identity_digest,
    )

    class _ChangingMaterialPort:
        material_snapshot_digest = snapshot_digest
        malformed = False

        def describe(self, _capability_id: str, _kind: str) -> MaterialDescriptor:
            if self.malformed:
                return cast(MaterialDescriptor, object())
            return descriptor

        def prepare(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("planning must not prepare material")

    port = _ChangingMaterialPort()
    pinned = composition_module._PinnedMaterialPort(cast(AuthenticatedCatalogContentSource, port))
    assert pinned.describe(capability_id, "skill") == descriptor
    port.malformed = True

    with pytest.raises(ValueError, match="changed within"):
        pinned.describe(capability_id, "skill")


def test_pinned_material_port_remembers_delegate_candidate_local_exceptions() -> None:
    capability_id = "skill:python-bad"
    snapshot_digest = _digest("exception-material-snapshot")
    identity = MaterialIdentity.create(
        capability_id=capability_id,
        kind="skill",
        content_sha256=_digest("exception-material-content"),
        content_bytes=64,
    )
    descriptor = MaterialDescriptor.create(
        capability_id=capability_id,
        kind="skill",
        actionability="load",
        content_sha256=identity.content_sha256,
        content_bytes=identity.content_bytes,
        estimated_tokens=16,
        provenance_digest=snapshot_digest,
        material_identity_digest=identity.identity_digest,
    )

    class _ExceptionMaterialPort:
        material_snapshot_digest = snapshot_digest

        def __init__(self, *, unavailable: bool) -> None:
            self.unavailable = unavailable

        def describe(self, _capability_id: str, _kind: str) -> MaterialDescriptor:
            if self.unavailable:
                raise CandidateAuthorityUnavailable("injected material failure")
            return descriptor

        def prepare(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("planning must not prepare material")

    exception_first = _ExceptionMaterialPort(unavailable=True)
    pinned_exception_first = composition_module._PinnedMaterialPort(
        cast(AuthenticatedCatalogContentSource, exception_first)
    )
    for _ in range(2):
        with pytest.raises(CandidateAuthorityUnavailable, match="injected"):
            pinned_exception_first.describe(capability_id, "skill")
    exception_first.unavailable = False
    with pytest.raises(ValueError, match="changed within"):
        pinned_exception_first.describe(capability_id, "skill")

    valid_first = _ExceptionMaterialPort(unavailable=False)
    pinned_valid_first = composition_module._PinnedMaterialPort(
        cast(AuthenticatedCatalogContentSource, valid_first)
    )
    assert pinned_valid_first.describe(capability_id, "skill") == descriptor
    valid_first.unavailable = True
    with pytest.raises(ValueError, match="changed within"):
        pinned_valid_first.describe(capability_id, "skill")


@pytest.mark.parametrize("returned_value", [None, object()], ids=["none", "object"])
def test_pinned_material_port_distinguishes_exception_from_malformed_return(
    returned_value: object,
) -> None:
    capability_id = "skill:python-bad"
    snapshot_digest = _digest("exception-vs-malformed-material")

    class _ExceptionOrMalformedMaterialPort:
        material_snapshot_digest = snapshot_digest

        def __init__(self, *, raises: bool) -> None:
            self.raises = raises

        def describe(self, _capability_id: str, _kind: str) -> MaterialDescriptor:
            if self.raises:
                raise CandidateAuthorityUnavailable("injected material failure")
            return cast(MaterialDescriptor, returned_value)

        def prepare(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("planning must not prepare material")

    exception_first = _ExceptionOrMalformedMaterialPort(raises=True)
    pinned_exception_first = composition_module._PinnedMaterialPort(
        cast(AuthenticatedCatalogContentSource, exception_first)
    )
    with pytest.raises(CandidateAuthorityUnavailable, match="injected"):
        pinned_exception_first.describe(capability_id, "skill")
    exception_first.raises = False
    with pytest.raises(ValueError, match="changed within"):
        pinned_exception_first.describe(capability_id, "skill")

    malformed_first = _ExceptionOrMalformedMaterialPort(raises=False)
    pinned_malformed_first = composition_module._PinnedMaterialPort(
        cast(AuthenticatedCatalogContentSource, malformed_first)
    )
    with pytest.raises(CandidateAuthorityUnavailable, match="does not match"):
        pinned_malformed_first.describe(capability_id, "skill")
    malformed_first.raises = True
    with pytest.raises(ValueError, match="changed within"):
        pinned_malformed_first.describe(capability_id, "skill")


@pytest.mark.parametrize(
    ("first_value", "second_value"),
    [(None, object()), (object(), None)],
    ids=["none-to-object", "object-to-none"],
)
def test_pinned_material_port_distinguishes_none_from_malformed_object(
    first_value: object,
    second_value: object,
) -> None:
    capability_id = "skill:python-bad"

    class _ChangingMalformedMaterialPort:
        material_snapshot_digest = _digest("none-vs-malformed-material")

        def __init__(self) -> None:
            self.value = first_value

        def describe(self, _capability_id: str, _kind: str) -> MaterialDescriptor:
            return cast(MaterialDescriptor, self.value)

        def prepare(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("planning must not prepare material")

    delegate = _ChangingMalformedMaterialPort()
    pinned = composition_module._PinnedMaterialPort(
        cast(AuthenticatedCatalogContentSource, delegate)
    )
    with pytest.raises(CandidateAuthorityUnavailable, match="does not match"):
        pinned.describe(capability_id, "skill")

    delegate.value = second_value
    with pytest.raises(ValueError, match="changed within"):
        pinned.describe(capability_id, "skill")


@pytest.mark.parametrize("initially_unavailable", [False, True])
def test_composed_material_exception_transition_is_global_across_observations(
    tmp_path: Path,
    initially_unavailable: bool,
) -> None:
    graph = _two_skill_graph_artifact(tmp_path)
    bad_capability_id = "skill:python-bad"
    good_capability_id = "skill:python-good"
    snapshot_digest = _digest("observation-material-snapshot")
    descriptors = {
        capability_id: _load_descriptor_for(capability_id, snapshot_digest)
        for capability_id in (bad_capability_id, good_capability_id)
    }

    class _ObservationMaterialPort:
        material_snapshot_digest = snapshot_digest
        unavailable = initially_unavailable

        def describe(self, capability_id: str, _kind: str) -> MaterialDescriptor:
            if self.unavailable and capability_id == bad_capability_id:
                raise CandidateAuthorityUnavailable("injected material failure")
            return descriptors[capability_id]

        def prepare(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("planning must not prepare material")

    delegate = _ObservationMaterialPort()
    pinned = composition_module._PinnedMaterialPort(
        cast(AuthenticatedCatalogContentSource, delegate)
    )
    first_observation = WorkObservation(
        signals=("python", "testing", "unit"),
        languages=("python",),
    )
    second_observation = WorkObservation(
        signals=("python", "review", "testing", "unit"),
        languages=("python",),
    )
    with IndexedGraphCandidateSource(
        graph,
        _artifact_digest(graph),
        material_port=pinned,
    ) as source:
        first = {item.capability_id: item for item in source.retrieve(first_observation)}
        assert good_capability_id in first
        assert (bad_capability_id in first) is not initially_unavailable
        delegate.unavailable = not initially_unavailable
        with pytest.raises(CandidateSourceUnavailable, match="material descriptor retrieval"):
            source.retrieve(second_observation)


def test_composition_preserves_manual_advice_and_zero_selection_abstention(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)

    with _open_composition(tmp_path, graph) as composition:
        capability = _plan(composition)[0]

    assert capability["actionability"] == "manual"
    assert capability["authority"] == {"type": "manual"}
    assert capability["benefit"]["tier"] == "advisory"  # type: ignore[index]

    low_facts = _AuthenticatedFacts(expected_task_benefit_ppm=0)
    with _open_composition(
        tmp_path,
        graph,
        facts=low_facts,
        journal_name="abstained-journal.sqlite3",
        audit_name="abstained-audit.sqlite3",
    ) as composition:
        composition.process(
            _event(
                composition,
                "SessionStarted",
                0,
                "abstained-start",
                payload={"host_level": "managing"},
            )
        )
        transition = composition.process(
            _event(
                composition,
                "IntentObserved",
                1,
                "abstained-intent",
                payload={
                    "observation_ref": {
                        "provider_id": "test-buffer",
                        "opaque_id": "abstained-observation",
                        "content_digest": _digest("abstained-observation"),
                    }
                },
            )
        )
        snapshot = composition.snapshot(_scope())

    assert transition.actions == ()
    assert transition.diagnostics[-1]["code"] == "below-net-benefit"
    assert snapshot.state is not None
    assert isinstance(snapshot.state.committed_plan, CommittedPlanV3)
    assert snapshot.state.committed_plan.status == "abstained"
    assert snapshot.state.committed_plan.capabilities == ()


def test_composition_fails_closed_without_exact_authenticated_benefit_facts(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)

    with _open_composition(
        tmp_path,
        graph,
        facts=_AuthenticatedFacts(missing=True),
    ) as composition:
        composition.process(
            _event(
                composition,
                "SessionStarted",
                0,
                "missing-facts-start",
                payload={"host_level": "managing"},
            )
        )
        with pytest.raises(ReplayValidationError, match="decision planning failed"):
            composition.process(
                _event(
                    composition,
                    "IntentObserved",
                    1,
                    "missing-facts-intent",
                    payload={
                        "observation_ref": {
                            "provider_id": "test-buffer",
                            "opaque_id": "missing-facts-observation",
                            "content_digest": _digest("missing-facts-observation"),
                        }
                    },
                )
            )


def test_composition_rejects_material_snapshot_drift(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)
    material_port = _material_port(tmp_path)

    with _open_composition(
        tmp_path,
        graph,
        material_port=material_port,
    ) as composition:
        composition.process(
            _event(
                composition,
                "SessionStarted",
                0,
                "event-start",
                payload={"host_level": "managing"},
            )
        )

        material_port.material_snapshot_digest = _digest("changed-material-snapshot")
        transition = composition.process(
            _event(
                composition,
                "IntentObserved",
                1,
                "event-after-material-change",
                payload={
                    "observation_ref": {
                        "provider_id": "test-buffer",
                        "opaque_id": "observation-after-change",
                        "content_digest": _digest("python-testing-unit-after-change"),
                    }
                },
            )
        )

    assert {diagnostic["code"] for diagnostic in transition.diagnostics} == {"catalog-unavailable"}


def test_composition_fails_closed_when_authenticated_fact_snapshot_drifts(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)
    facts = _AuthenticatedFacts()

    with _open_composition(tmp_path, graph, facts=facts) as composition:
        composition.process(
            _event(
                composition,
                "SessionStarted",
                0,
                "fact-drift-start",
                payload={"host_level": "managing"},
            )
        )
        facts.benefit_facts_snapshot_digest = _digest("substituted-benefit-facts")
        with pytest.raises(ReplayValidationError, match="decision planning failed"):
            composition.process(
                _event(
                    composition,
                    "IntentObserved",
                    1,
                    "fact-drift-intent",
                    payload={
                        "observation_ref": {
                            "provider_id": "test-buffer",
                            "opaque_id": "fact-drift-observation",
                            "content_digest": _digest("fact-drift-observation"),
                        }
                    },
                )
            )


def test_composition_plans_absent_capability_from_typed_install_port(tmp_path: Path) -> None:
    graph = _graph_artifact(tmp_path)
    install_port, bundle = _install_port()

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=install_port,
    ) as composition:
        capabilities = _plan(composition)

    assert len(capabilities) == 1
    assert capabilities[0]["actionability"] == "install"
    assert capabilities[0]["install_descriptor_digest"] == bundle.descriptor.descriptor_digest
    assert capabilities[0]["install_plan_digest"] == bundle.descriptor.plan_digest
    assert capabilities[0]["authority"] == {
        "type": "install",
        "descriptor": bundle.descriptor.to_dict(),
        "result_material": bundle.result_material.to_dict(),
    }


def test_composition_skips_malformed_install_authority_and_keeps_valid_peer(
    tmp_path: Path,
) -> None:
    graph = _two_skill_graph_artifact(tmp_path)
    bad_capability_id = "skill:python-bad"
    good_capability_id = "skill:python-good"
    snapshot_digest = _digest("mixed-install-snapshot")
    good_bundle = _install_bundle_for(good_capability_id, snapshot_digest)
    port = _StaticInstallBundlePort(
        installation_snapshot_digest=snapshot_digest,
        bundles={
            bad_capability_id: cast(InstallPlanningBundle, object()),
            good_capability_id: good_bundle,
        },
    )

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=port,
    ) as composition:
        capabilities = _plan(composition)

    assert [row["capability_id"] for row in capabilities] == [good_capability_id]
    assert capabilities[0]["authority"]["type"] == "install"  # type: ignore[index]
    assert all(row["capability_id"] != bad_capability_id for row in capabilities)


def test_pinned_install_port_treats_valid_to_malformed_as_global_drift() -> None:
    capability_id = "skill:python-bad"
    snapshot_digest = _digest("changing-install-snapshot")
    bundle = _install_bundle_for(capability_id, snapshot_digest)
    port = _StaticInstallBundlePort(
        installation_snapshot_digest=snapshot_digest,
        bundles={capability_id: bundle},
    )
    pinned = composition_module._PinnedInstallBundlePort(port)
    assert pinned.describe_bundle(capability_id, "skill") == bundle
    port.bundles[capability_id] = cast(InstallPlanningBundle, object())

    with pytest.raises(ValueError, match="changed within"):
        pinned.describe_bundle(capability_id, "skill")


def test_pinned_install_port_remembers_delegate_candidate_local_exceptions() -> None:
    capability_id = "skill:python-bad"
    snapshot_digest = _digest("exception-install-snapshot")
    bundle = _install_bundle_for(capability_id, snapshot_digest)

    class _ExceptionInstallPort(_StaticInstallBundlePort):
        unavailable: bool

        def describe_bundle(
            self,
            candidate_id: str,
            kind: str,
        ) -> InstallPlanningBundle | None:
            if self.unavailable:
                raise CandidateAuthorityUnavailable("injected install failure")
            return super().describe_bundle(candidate_id, kind)

    exception_first = _ExceptionInstallPort(
        installation_snapshot_digest=snapshot_digest,
        bundles={capability_id: bundle},
    )
    exception_first.unavailable = True
    pinned_exception_first = composition_module._PinnedInstallBundlePort(exception_first)
    for _ in range(2):
        with pytest.raises(CandidateAuthorityUnavailable, match="injected"):
            pinned_exception_first.describe_bundle(capability_id, "skill")
    exception_first.unavailable = False
    with pytest.raises(ValueError, match="changed within"):
        pinned_exception_first.describe_bundle(capability_id, "skill")

    valid_first = _ExceptionInstallPort(
        installation_snapshot_digest=snapshot_digest,
        bundles={capability_id: bundle},
    )
    valid_first.unavailable = False
    pinned_valid_first = composition_module._PinnedInstallBundlePort(valid_first)
    assert pinned_valid_first.describe_bundle(capability_id, "skill") == bundle
    valid_first.unavailable = True
    with pytest.raises(ValueError, match="changed within"):
        pinned_valid_first.describe_bundle(capability_id, "skill")


@pytest.mark.parametrize("returned_value", [None, object()], ids=["none", "object"])
def test_pinned_install_port_distinguishes_exception_from_returned_value(
    returned_value: object,
) -> None:
    capability_id = "skill:python-bad"
    snapshot_digest = _digest("exception-vs-returned-install")

    class _ExceptionOrReturnedInstallPort:
        installation_snapshot_digest = snapshot_digest

        def __init__(self, *, raises: bool) -> None:
            self.raises = raises

        def describe_bundle(
            self,
            _capability_id: str,
            _kind: str,
        ) -> InstallPlanningBundle | None:
            if self.raises:
                raise CandidateAuthorityUnavailable("injected install failure")
            return cast(InstallPlanningBundle | None, returned_value)

    exception_first = _ExceptionOrReturnedInstallPort(raises=True)
    pinned_exception_first = composition_module._PinnedInstallBundlePort(
        cast(_StaticInstallBundlePort, exception_first)
    )
    with pytest.raises(CandidateAuthorityUnavailable, match="injected"):
        pinned_exception_first.describe_bundle(capability_id, "skill")
    exception_first.raises = False
    with pytest.raises(ValueError, match="changed within"):
        pinned_exception_first.describe_bundle(capability_id, "skill")

    returned_first = _ExceptionOrReturnedInstallPort(raises=False)
    pinned_returned_first = composition_module._PinnedInstallBundlePort(
        cast(_StaticInstallBundlePort, returned_first)
    )
    if returned_value is None:
        assert pinned_returned_first.describe_bundle(capability_id, "skill") is None
    else:
        with pytest.raises(CandidateAuthorityUnavailable, match="does not match"):
            pinned_returned_first.describe_bundle(capability_id, "skill")
    returned_first.raises = True
    with pytest.raises(ValueError, match="changed within"):
        pinned_returned_first.describe_bundle(capability_id, "skill")


@pytest.mark.parametrize("initially_unavailable", [False, True])
def test_composed_install_exception_transition_is_global_across_observations(
    tmp_path: Path,
    initially_unavailable: bool,
) -> None:
    graph = _two_skill_graph_artifact(tmp_path)
    bad_capability_id = "skill:python-bad"
    good_capability_id = "skill:python-good"
    snapshot_digest = _digest("observation-install-snapshot")

    class _ObservationInstallPort(_StaticInstallBundlePort):
        unavailable = initially_unavailable

        def describe_bundle(
            self,
            capability_id: str,
            kind: str,
        ) -> InstallPlanningBundle | None:
            if self.unavailable and capability_id == bad_capability_id:
                raise CandidateAuthorityUnavailable("injected install failure")
            return super().describe_bundle(capability_id, kind)

    delegate = _ObservationInstallPort(
        installation_snapshot_digest=snapshot_digest,
        bundles={
            capability_id: _install_bundle_for(capability_id, snapshot_digest)
            for capability_id in (bad_capability_id, good_capability_id)
        },
    )
    pinned = composition_module._PinnedInstallBundlePort(delegate)
    first_observation = WorkObservation(
        signals=("python", "testing", "unit"),
        languages=("python",),
    )
    second_observation = WorkObservation(
        signals=("python", "review", "testing", "unit"),
        languages=("python",),
    )
    with IndexedGraphCandidateSource(
        graph,
        _artifact_digest(graph),
        install_plan_port=pinned,
    ) as source:
        first = {item.capability_id: item for item in source.retrieve(first_observation)}
        assert good_capability_id in first
        assert (bad_capability_id in first) is not initially_unavailable
        delegate.unavailable = not initially_unavailable
        with pytest.raises(CandidateSourceUnavailable, match="install descriptor retrieval"):
            source.retrieve(second_observation)


def test_composition_rejects_install_authority_removed_between_retrieval_and_planning(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)
    install_port, _bundle = _install_port()
    install_port.remove_after_first_lookup = True

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=install_port,
    ) as composition:
        composition.process(
            _event(
                composition,
                "SessionStarted",
                0,
                "removed-install-start",
                payload={"host_level": "managing"},
            )
        )
        with pytest.raises(ReplayValidationError, match="decision planning failed"):
            composition.process(
                _event(
                    composition,
                    "IntentObserved",
                    1,
                    "removed-install-intent",
                    payload={
                        "observation_ref": {
                            "provider_id": "test-buffer",
                            "opaque_id": "removed-install-observation",
                            "content_digest": _digest("removed-install-observation"),
                        }
                    },
                )
            )


def test_composition_wires_current_policy_and_exact_descriptor_for_auto_grant(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)
    install_port, bundle = _install_port()
    policy_root = tmp_path / "install-policy"
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    assert persist_install_policy(policy, policy_root) == policy.policy_digest

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=install_port,
        policy_store_root=policy_root,
        trusted_utc_now=_trusted_now,
    ) as composition:
        capability = _plan(composition)[0]
        consent = composition.process(
            _event(
                composition,
                "ReassessmentRequested",
                2,
                "event-desired",
                payload={
                    "owner_id": "runtime-owner-1",
                    "policy_snapshot_digest": policy.policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability["capability_id"],
                            "source_digest": capability["catalog_entry_digest"],
                            "lease_id": "lease-1",
                            "kind": capability["kind"],
                            "actionability": capability["actionability"],
                            "install_descriptor_digest": capability["install_descriptor_digest"],
                            "install_plan_digest": capability["install_plan_digest"],
                        }
                    ],
                },
            )
        )
        request = consent.actions[0]
        assert request.kind == "RequestConsent"

        granted = composition.process(
            _event(
                composition,
                "UserDecision",
                3,
                "event-auto-grant",
                payload={
                    "consent_id": request.consent_id or "",
                    "decision": "granted",
                    "decision_basis": "preapproved-policy",
                    "policy_snapshot_digest": policy.policy_digest,
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

    assert [action.kind for action in granted.actions] == ["InstallCapability"]
    assert install_port.describe_bundle_calls[-1] == (
        bundle.descriptor.capability_id,
        bundle.descriptor.kind,
    )


@pytest.mark.skipif(os.name == "nt", reason="built-in skill CAS is POSIX-only")
def test_composition_owns_and_executes_built_in_skill_cas_driver(tmp_path: Path) -> None:
    graph = _graph_artifact(tmp_path)
    body = "---\nname: python-tdd\ndescription: composition CAS test\n---\nTest first.\n"
    encoded = body.encode("utf-8")
    snapshot_digest = _digest("install-catalog-cas")
    material = MaterialIdentity.create(
        capability_id="skill:python-tdd",
        kind="skill",
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        content_bytes=len(encoded),
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=material.capability_id,
        kind=material.kind,
        installer_id="ctx-skill-installer-v1",
        plan_digest=_digest("install-plan-cas"),
        provenance_digest=snapshot_digest,
        result_material_identity_digest=material.identity_digest,
    )
    install_port = _StaticInstallBundlePort(
        installation_snapshot_digest=snapshot_digest,
        bundles={
            descriptor.capability_id: InstallPlanningBundle(
                descriptor=descriptor,
                result_material=material,
            )
        },
    )
    skill_root = tmp_path / "ctx-private" / "skills"
    skill_root.mkdir(parents=True, mode=0o700)
    skill_root.chmod(0o700)
    load_calls: list[str] = []

    class _BodySource:
        def load(
            self,
            request: InstallDriverRequest,
            expected_material: MaterialIdentity,
        ) -> str:
            load_calls.append(request.action.action_id)
            assert expected_material == material
            return body

    policy_root = tmp_path / "install-policy-cas"
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    persist_install_policy(policy, policy_root)
    runtime = SkillCasRuntimeConfig(
        skill_store_root=skill_root,
        body_source=_BodySource(),
        installer_id=descriptor.installer_id,
        host_identity_digest=_digest("composition-host"),
    )

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=install_port,
        policy_store_root=policy_root,
        trusted_utc_now=_trusted_now,
        skill_cas_runtime=runtime,
    ) as composition:
        capability = _plan(composition)[0]
        consent = composition.process(
            _event(
                composition,
                "ReassessmentRequested",
                2,
                "event-cas-desired",
                payload={
                    "owner_id": "runtime-owner-cas",
                    "policy_snapshot_digest": policy.policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability["capability_id"],
                            "source_digest": capability["catalog_entry_digest"],
                            "lease_id": "lease-cas",
                            "kind": capability["kind"],
                            "actionability": capability["actionability"],
                            "install_descriptor_digest": capability["install_descriptor_digest"],
                            "install_plan_digest": capability["install_plan_digest"],
                        }
                    ],
                },
            )
        )
        request = consent.actions[0]
        granted = composition.process(
            _event(
                composition,
                "UserDecision",
                3,
                "event-cas-grant",
                payload={
                    "consent_id": request.consent_id or "",
                    "decision": "granted",
                    "decision_basis": "preapproved-policy",
                    "policy_snapshot_digest": policy.policy_digest,
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
        snapshot = composition.snapshot(_scope())
        assert snapshot.state is not None
        state = snapshot.state.capability(descriptor.capability_id)
        assert isinstance(state, CapabilityStateV3)

        report = composition.execute_install(
            install,
            state.selection.selection,
            expected_policy_digest=policy.policy_digest,
        )

    assert report.outcome == "applied"
    assert report.settled
    assert load_calls == [install.action_id]
    assert (skill_root / material.content_sha256).read_text(encoding="utf-8") == body


@pytest.mark.skipif(os.name == "nt", reason="built-in POSIX actuators are disabled")
def test_composition_routes_agent_with_skill_and_agent_drivers_registered(
    tmp_path: Path,
) -> None:
    graph = _agent_graph_artifact(tmp_path)
    body = (
        "---\nname: reviewer\ndescription: composition agent test\n---\nReview the current task.\n"
    )
    encoded = body.encode("utf-8")
    snapshot_digest = _digest("install-catalog-agent")
    material = MaterialIdentity.create(
        capability_id="agent:reviewer",
        kind="agent",
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        content_bytes=len(encoded),
    )
    shared_installer_id = "ctx-shared-text-installer-v1"
    descriptor = InstallPlanDescriptor.create(
        capability_id=material.capability_id,
        kind=material.kind,
        installer_id=shared_installer_id,
        plan_digest=_digest("install-plan-agent"),
        provenance_digest=snapshot_digest,
        result_material_identity_digest=material.identity_digest,
    )
    install_port = _StaticInstallBundlePort(
        installation_snapshot_digest=snapshot_digest,
        bundles={
            descriptor.capability_id: InstallPlanningBundle(
                descriptor=descriptor,
                result_material=material,
            )
        },
    )
    private_root = tmp_path / "ctx-private"
    private_root.mkdir(mode=0o700)
    private_root.chmod(0o700)
    skill_root = private_root / "skills"
    agent_root = private_root / "inactive-agents"
    for root in (skill_root, agent_root):
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    agent_load_calls: list[str] = []

    class _SkillBodySource:
        def load(
            self,
            _request: InstallDriverRequest,
            _material: MaterialIdentity,
        ) -> str:
            raise AssertionError("agent install must not connect the skill driver")

    class _AgentBodySource:
        def load(
            self,
            request: InstallDriverRequest,
            expected_material: MaterialIdentity,
        ) -> str:
            agent_load_calls.append(request.action.action_id)
            assert expected_material == material
            return body

    policy_root = tmp_path / "install-policy-agent"
    policy = InstallConsentPolicy(agent_mode="preapproved-auto")
    persist_install_policy(policy, policy_root)
    skill_runtime = SkillCasRuntimeConfig(
        skill_store_root=skill_root,
        body_source=_SkillBodySource(),
        installer_id=shared_installer_id,
        host_identity_digest=_digest("composition-host"),
    )
    agent_runtime = AgentFileRuntimeConfig(
        inactive_agent_root=agent_root,
        body_source=_AgentBodySource(),
        installer_id=shared_installer_id,
        host_identity_digest=_digest("composition-host"),
    )

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=install_port,
        policy_store_root=policy_root,
        trusted_utc_now=_trusted_now,
        skill_cas_runtime=skill_runtime,
        agent_file_runtime=agent_runtime,
    ) as composition:
        capability = _plan(composition)[0]
        consent = composition.process(
            _event(
                composition,
                "ReassessmentRequested",
                2,
                "event-agent-desired",
                payload={
                    "owner_id": "runtime-owner-agent",
                    "policy_snapshot_digest": policy.policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability["capability_id"],
                            "source_digest": capability["catalog_entry_digest"],
                            "lease_id": "lease-agent",
                            "kind": capability["kind"],
                            "actionability": capability["actionability"],
                            "install_descriptor_digest": capability["install_descriptor_digest"],
                            "install_plan_digest": capability["install_plan_digest"],
                        }
                    ],
                },
            )
        )
        request = consent.actions[0]
        granted = composition.process(
            _event(
                composition,
                "UserDecision",
                3,
                "event-agent-grant",
                payload={
                    "consent_id": request.consent_id or "",
                    "decision": "granted",
                    "decision_basis": "preapproved-policy",
                    "policy_snapshot_digest": policy.policy_digest,
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
        snapshot = composition.snapshot(_scope())
        assert snapshot.state is not None
        state = snapshot.state.capability(descriptor.capability_id)
        assert isinstance(state, CapabilityStateV3)

        report = composition.execute_install(
            install,
            state.selection.selection,
            expected_policy_digest=policy.policy_digest,
        )

    assert report.outcome == "applied"
    assert report.settled
    assert agent_load_calls == [install.action_id]
    assert (agent_root / "reviewer.md").read_text(encoding="utf-8") == body


def test_runtime_package_does_not_export_raw_install_driver_construction() -> None:
    import ctx.runtime as runtime

    assert not hasattr(runtime, "InstallDriverRegistry")
    assert not hasattr(runtime, "prepare_install_execution")


@pytest.mark.skipif(os.name == "nt", reason="built-in skill CAS is POSIX-only")
def test_changed_skill_target_fails_before_composition_creates_authoritative_state(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)
    skill_root = tmp_path / "ctx-private" / "skills"
    skill_root.mkdir(parents=True, mode=0o700)
    skill_root.chmod(0o700)

    class _BodySource:
        def load(
            self,
            _request: InstallDriverRequest,
            _material: MaterialIdentity,
        ) -> str:
            raise AssertionError("body source must not be reached during composition")

    runtime = SkillCasRuntimeConfig(
        skill_store_root=skill_root,
        body_source=_BodySource(),
        installer_id="ctx-skill-installer-v1",
        host_identity_digest=_digest("changed-composition-host"),
    )
    original = skill_root.with_name("skills-original")
    skill_root.rename(original)
    skill_root.mkdir(mode=0o700)
    skill_root.chmod(0o700)

    with pytest.raises(RuntimeError, match="root changed"):
        _open_composition(tmp_path, graph, skill_cas_runtime=runtime)

    assert not (tmp_path / "journal.sqlite3").exists()
    assert not (tmp_path / "benefit-audit.sqlite3").exists()


@pytest.mark.skipif(os.name == "nt", reason="agent-file actuator is POSIX-only")
def test_changed_agent_target_fails_before_composition_creates_authoritative_state(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)
    agent_root = tmp_path / "ctx-private" / "inactive-agents"
    agent_root.mkdir(parents=True, mode=0o700)
    agent_root.chmod(0o700)

    class _BodySource:
        def load(
            self,
            _request: InstallDriverRequest,
            _material: MaterialIdentity,
        ) -> str:
            raise AssertionError("body source must not be reached during composition")

    runtime = AgentFileRuntimeConfig(
        inactive_agent_root=agent_root,
        body_source=_BodySource(),
        installer_id="ctx-agent-installer-v1",
        host_identity_digest=_digest("changed-agent-composition-host"),
    )
    original = agent_root.with_name("inactive-agents-original")
    agent_root.rename(original)
    agent_root.mkdir(mode=0o700)
    agent_root.chmod(0o700)

    with pytest.raises(RuntimeError, match="root changed"):
        _open_composition(tmp_path, graph, agent_file_runtime=runtime)

    assert not (tmp_path / "journal.sqlite3").exists()
    assert not (tmp_path / "benefit-audit.sqlite3").exists()


def test_composition_wires_host_authenticated_interactive_decision_guard(
    tmp_path: Path,
) -> None:
    graph = _graph_artifact(tmp_path)
    install_port, _descriptor = _install_port()
    policy = InstallConsentPolicy.safe_default()
    reservations: list[InteractiveInstallDecisionReservation] = []

    @contextmanager
    def decision_guard(
        reservation: InteractiveInstallDecisionReservation,
    ) -> Iterator[None]:
        reservations.append(reservation)
        yield

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=install_port,
        interactive_install_decision_guard=decision_guard,
        trusted_utc_now=_trusted_now,
    ) as composition:
        capability = _plan(composition)[0]
        consent = composition.process(
            _event(
                composition,
                "ReassessmentRequested",
                2,
                "event-interactive-desired",
                payload={
                    "owner_id": "runtime-owner-1",
                    "policy_snapshot_digest": policy.policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability["capability_id"],
                            "source_digest": capability["catalog_entry_digest"],
                            "lease_id": "lease-1",
                            "kind": capability["kind"],
                            "actionability": capability["actionability"],
                            "install_descriptor_digest": capability["install_descriptor_digest"],
                            "install_plan_digest": capability["install_plan_digest"],
                        }
                    ],
                },
            )
        )
        request = consent.actions[0]
        granted = composition.process(
            _event(
                composition,
                "UserDecision",
                3,
                "event-interactive-grant",
                payload={
                    "consent_id": request.consent_id or "",
                    "decision": "granted",
                    "decision_basis": "interactive",
                    "policy_snapshot_digest": policy.policy_digest,
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

    assert [action.kind for action in granted.actions] == ["InstallCapability"]
    assert len(reservations) == 1
    assert reservations[0].decision == "granted"
    assert reservations[0].requested_action_id == granted.actions[0].action_id


def test_composition_rejects_changed_descriptor_during_auto_grant(tmp_path: Path) -> None:
    graph = _graph_artifact(tmp_path)
    install_port, bundle = _install_port()
    policy_root = tmp_path / "install-policy"
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    persist_install_policy(policy, policy_root)

    with _open_composition(
        tmp_path,
        graph,
        install_bundle_port=install_port,
        policy_store_root=policy_root,
        trusted_utc_now=_trusted_now,
    ) as composition:
        capability = _plan(composition)[0]
        consent = composition.process(
            _event(
                composition,
                "ReassessmentRequested",
                2,
                "event-desired",
                payload={
                    "owner_id": "runtime-owner-1",
                    "policy_snapshot_digest": policy.policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability["capability_id"],
                            "source_digest": capability["catalog_entry_digest"],
                            "lease_id": "lease-1",
                            "kind": capability["kind"],
                            "actionability": capability["actionability"],
                            "install_descriptor_digest": capability["install_descriptor_digest"],
                            "install_plan_digest": capability["install_plan_digest"],
                        }
                    ],
                },
            )
        )
        request = consent.actions[0]
        install_port.bundles[bundle.descriptor.capability_id] = None

        with pytest.raises(CtxEngineError, match="authority lookup failed"):
            composition.process(
                _event(
                    composition,
                    "UserDecision",
                    3,
                    "event-auto-grant",
                    payload={
                        "consent_id": request.consent_id or "",
                        "decision": "granted",
                        "decision_basis": "preapproved-policy",
                        "policy_snapshot_digest": policy.policy_digest,
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


def test_composition_closes_safely_and_cannot_be_reentered(tmp_path: Path) -> None:
    graph = _graph_artifact(tmp_path)
    composition = _open_composition(tmp_path, graph)

    with composition:
        assert composition.closed is False
        assert not hasattr(composition, "engine")

    assert composition.closed is True
    composition.close()
    with pytest.raises(RuntimeError, match="closed"):
        composition.__enter__()
    with pytest.raises(RuntimeError, match="closed"):
        composition.process(
            _event(
                composition,
                "SessionStarted",
                0,
                "event-after-close",
                payload={"host_level": "managing"},
            )
        )

    with _open_composition(
        tmp_path,
        graph,
    ) as reopened:
        assert reopened.snapshot(_scope()).revision == 0


def test_composition_rejects_invalid_artifact_and_port(tmp_path: Path) -> None:
    graph = _graph_artifact(tmp_path)

    with pytest.raises(CandidateSourceUnavailable, match="unavailable"):
        _open_composition(
            tmp_path,
            graph,
            graph_artifact_sha256="f" * 64,
            journal_name="wrong-digest.sqlite3",
            audit_name="wrong-digest-audit.sqlite3",
        )

    class _InvalidInstallPort:
        installation_snapshot_digest = "not-a-digest"

        def describe_bundle(self, _capability_id: str, _kind: str) -> None:
            return None

    with pytest.raises(TypeError, match="install bundle contract"):
        open_engine_composition(
            graph_artifact_path=graph,
            graph_artifact_sha256=_artifact_digest(graph),
            journal_path=tmp_path / "invalid-port.sqlite3",
            observation_normalizer=_normalizer,
            benefit_facts_port=_AuthenticatedFacts(),
            net_benefit_policy=_policy(),
            catalog_namespace_digest=CATALOG_NAMESPACE_DIGEST,
            benefit_audit_path=tmp_path / "invalid-port-audit.sqlite3",
            install_bundle_port=_InvalidInstallPort(),  # type: ignore[arg-type]
        )


def test_composition_cleans_up_source_when_later_validation_fails(tmp_path: Path) -> None:
    graph = _graph_artifact(tmp_path)

    with pytest.raises(PlannerValidationError, match="planner_version"):
        _open_composition(
            tmp_path,
            graph,
            planner_version="not a safe planner version",
        )

    assert not tuple(tmp_path.glob(".ctx-indexed-snapshot-*"))


def test_composition_preserves_construction_error_when_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _graph_artifact(tmp_path)

    def fail_close(_source: IndexedGraphCandidateSource) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(IndexedGraphCandidateSource, "close", fail_close)

    with pytest.raises(PlannerValidationError, match="planner_version") as captured:
        _open_composition(
            tmp_path,
            graph,
            planner_version="not a safe planner version",
        )

    assert captured.value.__notes__ == ["CTX candidate-source cleanup also failed with OSError"]


def test_composition_preserves_body_error_when_context_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _graph_artifact(tmp_path)
    composition = _open_composition(tmp_path, graph)
    original_close = IndexedGraphCandidateSource.close

    def fail_close(_source: IndexedGraphCandidateSource) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(IndexedGraphCandidateSource, "close", fail_close)
    with pytest.raises(RuntimeError, match="body failure") as captured:
        with composition:
            raise RuntimeError("body failure")

    assert captured.value.__notes__ == ["CTX candidate-source cleanup also failed with OSError"]
    assert composition.closed is False
    monkeypatch.setattr(IndexedGraphCandidateSource, "close", original_close)
    composition.close()
