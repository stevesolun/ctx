from __future__ import annotations

import copy
import hashlib
import pickle
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import FrozenInstanceError, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import networkx as nx
import pytest

from ctx.core.graph.graph_store import build_graph_store
from ctx.engine.benefit import BenefitCandidate, EvidenceSummary, NetBenefitPolicy, ResourceCosts
from ctx.engine.content import MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import (
    InstallAuthorizer,
    InstallConsentPolicy,
    InstallExecutionBinding,
    InstallPlanDescriptor,
    InstallPlanningBundle,
    PreparedInstallPlan,
)
from ctx.engine.planner import CapabilityCandidate, CapabilitySelection, WorkObservation
from ctx.engine.protocol import (
    INSTALL_RECEIPT_SCHEMA_V3,
    EngineEvent,
    HostAction,
    ScopeRef,
)
from ctx.engine.replay import (
    ObservationNormalizer,
    ObservationReference,
    StructuredSurrogate,
)
from ctx.engine.engine import EngineSnapshot
from ctx.engine.state import CommittedPlanV3, EngineState
from ctx.engine.store import (
    ActivationExecutionStatus,
    EventIdCollision,
    RevisionConflict,
    StreamId,
)
from ctx.runtime.composition import EngineComposition, open_engine_composition
from ctx.runtime.managed_query import (
    ManagedAdvanceResult,
    ManagedQueryError,
    PreparedManagedQuery,
    _project_prepared_managed_query_snapshot,
    advance_managed_query,
    prepare_managed_query,
    reopen_managed_query,
)


NOW = "2026-08-02T12:00:00Z"
CATALOG_NAMESPACE_DIGEST = hashlib.sha256(b"managed-query-catalog").hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(suffix: str = "one") -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id=f"session-{suffix}",
        exposure_id=f"exposure-{suffix}",
        host_context_id="host-neutral",
    )


def _candidate(
    capability_id: str,
    *,
    actionability: str,
    installation_snapshot_digest: str | None = None,
) -> CapabilityCandidate:
    kind, name = capability_id.split(":", 1)
    descriptor = (
        None
        if installation_snapshot_digest is None
        else _install_bundle(capability_id, installation_snapshot_digest).descriptor
    )
    return CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=_digest(f"source:{capability_id}:{actionability}"),
        normalized_score_ppm=900_000,
        matching_signals=("python",),
        reason_codes=("graph-match",),
        actionability=actionability,
        install_descriptor_digest=(None if descriptor is None else descriptor.descriptor_digest),
        install_plan_digest=(None if descriptor is None else descriptor.plan_digest),
    )


def _install_bundle(capability_id: str, snapshot_digest: str) -> InstallPlanningBundle:
    kind = capability_id.split(":", 1)[0]
    material = MaterialIdentity.create(
        capability_id=capability_id,
        kind=kind,
        content_sha256=_digest(f"installed:{capability_id}"),
        content_bytes=64,
    )
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id=f"ctx-{kind}-installer-v1",
        plan_digest=_digest(f"install-plan:{capability_id}"),
        provenance_digest=snapshot_digest,
        result_material_identity_digest=material.identity_digest,
    )
    return InstallPlanningBundle(descriptor=descriptor, result_material=material)


@dataclass
class _Facts:
    benefit_facts_snapshot_digest: str = field(
        default_factory=lambda: _digest("managed-benefit-facts")
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
class _MaterialAuthority:
    capability_ids: set[str]
    material_snapshot_digest: str = field(default_factory=lambda: _digest("material-snapshot"))

    def describe(self, capability_id: str, kind: str) -> MaterialDescriptor:
        if capability_id not in self.capability_ids:
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
            provenance_digest=self.material_snapshot_digest,
            material_identity_digest=identity.identity_digest,
        )

    def prepare(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("managed planning must not prepare material")


@dataclass
class _InstallAuthority:
    installation_snapshot_digest: str
    bundles: dict[str, InstallPlanningBundle | None]

    def describe(self, capability_id: str, _kind: str) -> InstallPlanDescriptor | None:
        bundle = self.bundles.get(capability_id)
        return None if bundle is None else bundle.descriptor

    def describe_bundle(self, capability_id: str, _kind: str) -> InstallPlanningBundle | None:
        return self.bundles.get(capability_id)

    def prepare(
        self,
        _action: HostAction,
        _selection: CapabilitySelection,
        _descriptor: InstallPlanDescriptor,
        *,
        expected_catalog_snapshot_digest: str,
        expected_policy_digest: str,
        authority: InstallAuthorizer | None = None,
    ) -> PreparedInstallPlan:
        del expected_catalog_snapshot_digest, expected_policy_digest, authority
        raise AssertionError("candidate retrieval must not prepare an install plan")


def _normalizer(requested_limit: int):
    def normalize(
        _reference: ObservationReference,
        _state: EngineState | None,
    ) -> StructuredSurrogate:
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

    return normalize


def _composition(
    tmp_path: Path,
    *,
    candidates: tuple[CapabilityCandidate, ...],
    requested_limit: int = 5,
    material_authority: _MaterialAuthority | None = None,
    install_authority: _InstallAuthority | None = None,
    facts: _Facts | None = None,
    observation_normalizer: ObservationNormalizer | None = None,
    interactive_install_decision_guard: object | None = None,
    trusted_utc_now: object | None = None,
    suffix: str = "one",
) -> EngineComposition:
    graph = nx.Graph()
    for candidate in candidates:
        graph.add_node(
            candidate.capability_id,
            label=candidate.name,
            type=candidate.kind,
            tags=["python"],
        )
    graph_path = tmp_path / f"managed-{suffix}-graph.sqlite3"
    if not graph_path.exists():
        build_graph_store(graph_path, graph)
        graph_path.chmod(0o444)
    return open_engine_composition(
        graph_artifact_path=graph_path,
        graph_artifact_sha256=hashlib.sha256(graph_path.read_bytes()).hexdigest(),
        journal_path=tmp_path / f"managed-{suffix}.sqlite3",
        observation_normalizer=(
            _normalizer(requested_limit)
            if observation_normalizer is None
            else observation_normalizer
        ),
        benefit_facts_port=_Facts() if facts is None else facts,
        net_benefit_policy=NetBenefitPolicy(
            calibration_digest=_digest("calibration"),
            minimum_relevance_ppm=1,
        ),
        catalog_namespace_digest=CATALOG_NAMESPACE_DIGEST,
        benefit_audit_path=tmp_path / f"managed-{suffix}-audit.sqlite3",
        material_port=material_authority,  # type: ignore[arg-type]
        install_bundle_port=install_authority,
        planner_version="ctx-managed-query-planner-v3",
        interactive_install_decision_guard=interactive_install_decision_guard,  # type: ignore[arg-type]
        trusted_utc_now=trusted_utc_now,  # type: ignore[arg-type]
    )


def _event(
    composition: EngineComposition,
    *,
    kind: str,
    revision: int,
    suffix: str,
    payload: dict[str, object],
) -> EngineEvent:
    scope = _scope(suffix)
    return EngineEvent(
        event_id=f"event-{kind.lower()}-{suffix}",
        kind=kind,
        scope=scope,
        expected_revision=revision,
        occurred_at=NOW,
        payload=payload,
        correlation_id=f"plan-{suffix}",
        causation_id=f"cause-{suffix}",
        engine_version="ctx-engine-v1",
        planner_version=composition.planner_version,
        policy_version="policy-v1",
        host_descriptor_digest=_digest("host-neutral-managing"),
        catalog_snapshot_digest=composition.catalog_snapshot_digest,
        semantic_model_digest=_digest("semantic-disabled"),
        semantic_index_digest=_digest("semantic-index-disabled"),
        work_signature=_digest(f"work:{suffix}"),
        random_seed=0,
    )


def _prepare(
    composition: EngineComposition,
    *,
    suffix: str,
) -> PreparedManagedQuery:
    return prepare_managed_query(
        composition=composition,
        session_started=_event(
            composition,
            kind="SessionStarted",
            revision=0,
            suffix=suffix,
            payload={"host_level": "managing"},
        ),
        intent_observed=_event(
            composition,
            kind="IntentObserved",
            revision=1,
            suffix=suffix,
            payload={
                "observation_ref": {
                    "provider_id": "managed-query-test",
                    "opaque_id": f"observation-{suffix}",
                    "content_digest": _digest(f"work:{suffix}"),
                }
            },
        ),
    )


def _managed_events(
    composition: EngineComposition,
    *,
    suffix: str,
) -> tuple[EngineEvent, EngineEvent]:
    return (
        _event(
            composition,
            kind="SessionStarted",
            revision=0,
            suffix=suffix,
            payload={"host_level": "managing"},
        ),
        _event(
            composition,
            kind="IntentObserved",
            revision=1,
            suffix=suffix,
            payload={
                "observation_ref": {
                    "provider_id": "managed-query-test",
                    "opaque_id": f"observation-{suffix}",
                    "content_digest": _digest(f"work:{suffix}"),
                }
            },
        ),
    )


def _development_event(
    initial_intent: EngineEvent,
    *,
    revision: int,
    attempt: str,
    scope: ScopeRef | None = None,
) -> EngineEvent:
    return replace(
        initial_intent,
        event_id=f"event-developmentobserved-{attempt}",
        kind="DevelopmentObserved",
        scope=initial_intent.scope if scope is None else scope,
        expected_revision=revision,
        payload={
            "observation_ref": {
                "provider_id": "managed-query-test",
                "opaque_id": f"observation-{attempt}",
                "content_digest": _digest(f"work:{attempt}"),
            }
        },
        correlation_id=f"plan-{attempt}",
        causation_id=f"cause-{attempt}",
        work_signature=_digest(f"work:{attempt}"),
    )


def _projection_fields(prepared: PreparedManagedQuery) -> tuple[object, ...]:
    return (
        prepared.status,
        prepared.abstention_code,
        prepared.plan_id,
        prepared.planning_environment_digest,
        prepared.decision_digest,
        prepared.journal_revision,
        prepared.journal_record_digest,
        prepared.benefit_result_digest,
        prepared.requested_limit,
        prepared.candidate_pool_count,
        prepared.search_evaluation_count,
        tuple(
            (
                item.capability_id,
                item.kind,
                item.name,
                item.actionability,
                item.matching_signals,
                item.reason_codes,
                item.benefit_tier,
                item.individual_net_benefit_u,
                item.marginal_net_benefit_u,
                item.source_digest,
                item.catalog_identity_digest,
                item.install_descriptor_digest,
                item.install_plan_digest,
            )
            for item in prepared.selections
        ),
    )


def _explanatory_fields(prepared: PreparedManagedQuery) -> tuple[object, ...]:
    fields = _projection_fields(prepared)
    return (*fields[:5], *fields[7:])


def _snapshot_at_revision(
    snapshot: EngineSnapshot,
    revision: int,
    *,
    record_digest: str | None = None,
    committed_plan: CommittedPlanV3 | None = None,
) -> EngineSnapshot:
    assert snapshot.state is not None
    state = replace(
        snapshot.state,
        revision=revision,
        committed_plan=(
            snapshot.state.committed_plan if committed_plan is None else committed_plan
        ),
    )
    return EngineSnapshot(
        stream_id=snapshot.stream_id,
        revision=revision,
        state=state,
        record_digest=record_digest or _digest(f"record:{revision}"),
    )


def test_one_committed_plan_globally_bounds_four_competing_types(tmp_path: Path) -> None:
    candidates = tuple(
        _candidate(capability_id, actionability="manual")
        for capability_id in (
            "agent:reviewer",
            "harness:python-runner",
            "mcp-server:docs",
            "skill:lint",
            "skill:test",
            "skill:types",
        )
    )
    with _composition(tmp_path, candidates=candidates, suffix="bounded") as composition:
        prepared = _prepare(composition, suffix="bounded")
        snapshot = composition.snapshot(_scope("bounded"))

    assert prepared.status == "ready"
    assert len(prepared.selections) == 5
    assert {item.kind for item in prepared.selections} == {
        "skill",
        "agent",
        "mcp-server",
        "harness",
    }
    assert prepared.benefit_result_digest is not None
    assert all(item.name for item in prepared.selections)
    assert all("python" in item.matching_signals for item in prepared.selections)
    assert all("graph-match" in item.reason_codes for item in prepared.selections)
    assert all(item.marginal_net_benefit_u > 0 for item in prepared.selections)
    assert snapshot.state is not None
    committed = snapshot.state.committed_plan
    assert isinstance(committed, CommittedPlanV3)
    assert (
        prepared.plan_id,
        prepared.planning_environment_digest,
        prepared.decision_digest,
        prepared.benefit_result_digest,
        prepared.journal_revision,
        prepared.journal_record_digest,
    ) == (
        committed.plan_id,
        committed.catalog_snapshot_id,
        committed.decision_digest,
        committed.benefit_audit.result_digest if committed.benefit_audit else None,
        snapshot.revision,
        snapshot.record_digest,
    )
    assert tuple(item.capability_id for item in prepared.selections) == tuple(
        item.capability_id for item in committed.capabilities
    )
    assert tuple(
        (
            item.name,
            item.matching_signals,
            item.reason_codes,
            item.benefit_tier,
            item.individual_net_benefit_u,
            item.marginal_net_benefit_u,
        )
        for item in prepared.selections
    ) == tuple(
        (
            item.name,
            item.selection.presentation.matching_signals,
            item.selection.presentation.reason_codes,
            item.benefit.tier,
            item.benefit.individual_net_benefit_u,
            item.benefit.marginal_net_benefit_u,
        )
        for item in committed.capabilities
    )


def test_zero_limit_is_one_audited_abstention(tmp_path: Path) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(
        tmp_path,
        candidates=candidates,
        requested_limit=0,
        suffix="zero",
    ) as composition:
        prepared = _prepare(composition, suffix="zero")

    assert prepared.status == "abstained"
    assert prepared.abstention_code == "limit-zero"
    assert prepared.selections == ()
    assert prepared.benefit_result_digest is not None


def test_mixed_projection_uses_existing_typed_authority_without_reranking(
    tmp_path: Path,
) -> None:
    installation_snapshot = _digest("mixed-installation")
    candidates = (
        _candidate("skill:loaded", actionability="load"),
        _candidate(
            "agent:installer",
            actionability="install",
            installation_snapshot_digest=installation_snapshot,
        ),
        _candidate("mcp-server:manual-docs", actionability="manual"),
        _candidate("harness:manual-runner", actionability="manual"),
    )
    load_candidate = candidates[0]
    install_bundle = _install_bundle("agent:installer", installation_snapshot)
    with _composition(
        tmp_path,
        candidates=candidates,
        material_authority=_MaterialAuthority(
            capability_ids={load_candidate.capability_id},
        ),
        install_authority=_InstallAuthority(
            installation_snapshot_digest=installation_snapshot,
            bundles={install_bundle.descriptor.capability_id: install_bundle},
        ),
        suffix="mixed",
    ) as composition:
        prepared = _prepare(composition, suffix="mixed")

    assert tuple(
        (item.capability_id, item.kind, item.actionability) for item in prepared.selections
    ) == (
        ("agent:installer", "agent", "install"),
        ("skill:loaded", "skill", "load"),
        ("harness:manual-runner", "harness", "manual"),
        ("mcp-server:manual-docs", "mcp-server", "manual"),
    )


def test_graph_uninstalled_row_requires_exact_install_authority(tmp_path: Path) -> None:
    installation_snapshot = _digest("graph-installation")
    agent_bundle = _install_bundle("agent:reviewer", installation_snapshot)
    install_authority = _InstallAuthority(
        installation_snapshot_digest=installation_snapshot,
        bundles={"agent:reviewer": agent_bundle, "mcp-server:docs": None},
    )
    candidates = (
        _candidate(
            "agent:reviewer",
            actionability="install",
            installation_snapshot_digest=installation_snapshot,
        ),
        _candidate("mcp-server:docs", actionability="manual"),
    )
    with _composition(
        tmp_path,
        candidates=candidates,
        install_authority=install_authority,
        suffix="graph",
    ) as composition:
        prepared = _prepare(composition, suffix="graph")

    projected = {item.capability_id: item.actionability for item in prepared.selections}
    assert projected == {"agent:reviewer": "install", "mcp-server:docs": "manual"}


def test_prepared_projection_is_factory_issued_immutable_and_nonserializable(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="trusted production composition"):
        EngineComposition()
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(
        tmp_path,
        candidates=candidates,
        suffix="sealed",
    ) as composition:
        prepared = _prepare(composition, suffix="sealed")
        with pytest.raises(AttributeError, match="immutable"):
            composition._engine = object()  # type: ignore[assignment,attr-defined,misc]
        with pytest.raises(TypeError, match="copied"):
            copy.copy(composition)
        with pytest.raises(TypeError, match="serialized"):
            pickle.dumps(composition)

    with pytest.raises(TypeError, match="factory"):
        PreparedManagedQuery()  # type: ignore[call-arg]
    with pytest.raises((FrozenInstanceError, AttributeError)):
        prepared.status = "abstained"  # type: ignore[misc]
    with pytest.raises(TypeError, match="copied"):
        copy.copy(prepared)
    with pytest.raises(TypeError, match="copied"):
        copy.deepcopy(prepared)
    with pytest.raises(TypeError, match="serialized"):
        pickle.dumps(prepared)
    assert not hasattr(prepared, "to_dict")
    assert not hasattr(prepared, "__dict__")
    assert all(not hasattr(item, "__dict__") for item in prepared.selections)
    representation = repr(prepared)
    assert prepared.decision_digest in representation
    assert "prompt" not in representation
    assert str(tmp_path) not in representation
    assert "credential" not in representation


def test_snapshot_projection_reopens_revision_two_and_later_heads_without_live_authority(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(tmp_path, candidates=candidates, suffix="snapshot") as composition:
        prepared = _prepare(composition, suffix="snapshot")
        committed_snapshot = composition.snapshot(_scope("snapshot"))

    reopened = _project_prepared_managed_query_snapshot(
        snapshot=committed_snapshot,
        expected_plan_id=prepared.plan_id,
    )
    assert _projection_fields(reopened) == _projection_fields(prepared)

    later_snapshot = _snapshot_at_revision(committed_snapshot, 7)
    later = _project_prepared_managed_query_snapshot(
        snapshot=later_snapshot,
        expected_plan_id=prepared.plan_id,
    )
    assert _projection_fields(later) == (
        *_projection_fields(prepared)[:5],
        7,
        _digest("record:7"),
        *_projection_fields(prepared)[7:],
    )
    with pytest.raises(AttributeError, match="immutable"):
        later.journal_revision = 8  # type: ignore[misc]
    with pytest.raises(TypeError, match="copied"):
        copy.deepcopy(later)
    with pytest.raises(TypeError, match="serialized"):
        pickle.dumps(later)


def test_snapshot_projection_preserves_exact_degraded_plan_without_authority(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(tmp_path, candidates=candidates, suffix="degraded") as composition:
        _prepare(composition, suffix="degraded")
        committed_snapshot = composition.snapshot(_scope("degraded"))
        catalog_snapshot_digest = composition.catalog_snapshot_digest
    assert committed_snapshot.state is not None
    degraded = CommittedPlanV3(
        plan_id="plan-degraded-reopen",
        catalog_snapshot_id=catalog_snapshot_digest,
        decision_digest=_digest("degraded-decision"),
        status="degraded",
        abstention_code="planner-failed",
        benefit_audit=None,
        capabilities=(),
    )
    degraded_state = replace(
        committed_snapshot.state,
        revision=4,
        committed_plan=degraded,
        last_manual_bundle=(),
    )
    degraded_snapshot = EngineSnapshot(
        stream_id=committed_snapshot.stream_id,
        revision=4,
        state=degraded_state,
        record_digest=_digest("record:4"),
    )

    projected = _project_prepared_managed_query_snapshot(
        snapshot=degraded_snapshot,
        expected_plan_id=degraded.plan_id,
    )

    assert projected.status == "degraded"
    assert projected.abstention_code == "planner-failed"
    assert projected.plan_id == degraded.plan_id
    assert projected.journal_revision == 4
    assert projected.selections == ()
    assert projected.benefit_result_digest is None
    assert projected.requested_limit is None
    assert projected.candidate_pool_count is None
    assert projected.search_evaluation_count is None


@pytest.mark.parametrize("expected_plan_id", ["", "contains whitespace", 7])
def test_snapshot_projection_rejects_invalid_expected_plan_identity(
    tmp_path: Path,
    expected_plan_id: object,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(tmp_path, candidates=candidates, suffix="plan-identity") as composition:
        _prepare(composition, suffix="plan-identity")
        snapshot = composition.snapshot(_scope("plan-identity"))

    with pytest.raises((TypeError, ManagedQueryError), match="expected_plan_id"):
        _project_prepared_managed_query_snapshot(
            snapshot=snapshot,
            expected_plan_id=expected_plan_id,  # type: ignore[arg-type]
        )


def test_snapshot_projection_rejects_plan_substitution(tmp_path: Path) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(tmp_path, candidates=candidates, suffix="plan-match") as composition:
        prepared = _prepare(composition, suffix="plan-match")
        snapshot = composition.snapshot(_scope("plan-match"))

    with pytest.raises(ManagedQueryError, match="plan identity"):
        _project_prepared_managed_query_snapshot(
            snapshot=snapshot,
            expected_plan_id=f"{prepared.plan_id}-substituted",
        )


def test_snapshot_projection_rejects_absent_or_invalid_authoritative_state(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(tmp_path, candidates=candidates, suffix="invalid-head") as composition:
        _prepare(composition, suffix="invalid-head")
        snapshot = composition.snapshot(_scope("invalid-head"))
    assert snapshot.state is not None

    empty = EngineSnapshot(
        stream_id=snapshot.stream_id,
        revision=0,
        state=None,
        record_digest=None,
    )
    no_plan = EngineSnapshot(
        stream_id=snapshot.stream_id,
        revision=2,
        state=EngineState(
            revision=2,
            scope=snapshot.state.scope,
            host_level=snapshot.state.host_level,
            host_descriptor_digest=snapshot.state.host_descriptor_digest,
        ),
        record_digest=_digest("no-plan"),
    )
    invalid_digest = EngineSnapshot(
        stream_id=snapshot.stream_id,
        revision=2,
        state=snapshot.state,
        record_digest="not-a-digest",
    )
    invalid_state = object.__new__(EngineSnapshot)
    object.__setattr__(invalid_state, "stream_id", snapshot.stream_id)
    object.__setattr__(invalid_state, "revision", 2)
    object.__setattr__(invalid_state, "state", object())
    object.__setattr__(invalid_state, "record_digest", _digest("invalid-state"))
    object.__setattr__(invalid_state, "projection_repaired", False)
    wrong_stream = EngineSnapshot(
        stream_id=StreamId.from_scope(_scope("substituted-stream")),
        revision=snapshot.revision,
        state=snapshot.state,
        record_digest=snapshot.record_digest,
    )

    for candidate, message in (
        (empty, "revision"),
        (no_plan, "committed"),
        (invalid_digest, "journal_record_digest"),
        (invalid_state, "state"),
        (wrong_stream, "stream"),
    ):
        with pytest.raises((TypeError, ManagedQueryError), match=message):
            _project_prepared_managed_query_snapshot(snapshot=candidate)

    with pytest.raises(TypeError, match="EngineSnapshot"):
        _project_prepared_managed_query_snapshot(snapshot=object())  # type: ignore[arg-type]


def test_managed_advance_commits_revision_zero_and_resumes_revision_one(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)

    with _composition(tmp_path, candidates=candidates, suffix="advance-zero") as composition:
        session_started, intent_observed = _managed_events(
            composition,
            suffix="advance-zero",
        )
        advanced = advance_managed_query(
            composition=composition,
            session_started=session_started,
            planning_observed=intent_observed,
        )
        prepared = advanced.prepared
        assert advanced.transition.event_id == intent_observed.event_id
        assert prepared.journal_revision == 2
        assert prepared.plan_id == intent_observed.correlation_id

    with _composition(tmp_path, candidates=candidates, suffix="advance-one") as composition:
        session_started, intent_observed = _managed_events(
            composition,
            suffix="advance-one",
        )
        started = composition.process(session_started)
        assert started.to_revision == 1
        advanced = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        prepared = advanced.prepared
        assert advanced.transition.from_revision == 1
        assert prepared.journal_revision == 2
        assert prepared.plan_id == intent_observed.correlation_id


def test_managed_reopen_and_exact_advance_retry_are_planner_free(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    facts = _Facts()
    with _composition(
        tmp_path,
        candidates=candidates,
        facts=facts,
        suffix="reopen",
    ) as composition:
        session_started, intent_observed = _managed_events(composition, suffix="reopen")
        advanced = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        prepared = advanced.prepared
        facts.calls.clear()

        reopened = reopen_managed_query(
            composition=composition,
            scope=intent_observed.scope,
            expected_plan_id=prepared.plan_id,
        )
        retry = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        retried = retry.prepared

    assert facts.calls == []
    assert retry.transition == advanced.transition
    assert _projection_fields(reopened) == _projection_fields(prepared)
    assert _projection_fields(retried) == _projection_fields(prepared)


def test_managed_development_commits_latest_plan_across_later_stream_heads(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    facts = _Facts()
    with _composition(
        tmp_path,
        candidates=candidates,
        facts=facts,
        suffix="development",
    ) as composition:
        session_started, intent_observed = _managed_events(
            composition,
            suffix="development",
        )
        composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        child_scope = replace(
            intent_observed.scope,
            exposure_id="exposure-development-child",
            host_context_id="child-agent",
            parent_exposure_id=intent_observed.scope.exposure_id,
        )
        development = _development_event(
            intent_observed,
            revision=2,
            attempt="development-later",
            scope=child_scope,
        )

        advanced = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=development,
        )
        assert advanced.prepared.plan_id == development.correlation_id
        assert advanced.prepared.journal_revision == 3
        assert advanced.transition.from_revision == 2
        assert advanced.transition.to_revision == 3

        composition.process(
            replace(
                development,
                event_id="event-turnended-development-later",
                kind="TurnEnded",
                expected_revision=3,
                payload={},
            )
        )
        facts.calls.clear()
        reopened = composition.reopen_managed_query(
            child_scope,
            expected_plan_id=development.correlation_id,
        )
        retried = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=development,
        )

    assert facts.calls == []
    assert retried.transition == advanced.transition
    assert reopened.journal_revision == retried.prepared.journal_revision == 4
    assert _explanatory_fields(reopened) == _explanatory_fields(advanced.prepared)
    assert _explanatory_fields(retried.prepared) == _explanatory_fields(advanced.prepared)


def test_managed_advance_result_is_factory_issued_immutable_and_nonserializable(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="factory"):
        ManagedAdvanceResult()
    candidates = (_candidate("skill:test", actionability="manual"),)
    with _composition(tmp_path, candidates=candidates, suffix="advance-sealed") as composition:
        session_started, intent_observed = _managed_events(
            composition,
            suffix="advance-sealed",
        )
        result = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )

    with pytest.raises(AttributeError, match="immutable"):
        result.prepared = result.prepared  # type: ignore[misc]
    with pytest.raises(TypeError, match="copied"):
        copy.copy(result)
    with pytest.raises(TypeError, match="copied"):
        copy.deepcopy(result)
    with pytest.raises(TypeError, match="serialized"):
        pickle.dumps(result)
    assert not hasattr(result, "__dict__")


def test_managed_advance_rejects_changed_retry_and_stale_head_without_planning(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    facts = _Facts()
    with _composition(
        tmp_path,
        candidates=candidates,
        facts=facts,
        suffix="stale",
    ) as composition:
        session_started, intent_observed = _managed_events(composition, suffix="stale")
        composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        facts.calls.clear()
        before_collision = composition.snapshot(intent_observed.scope)
        with pytest.raises(EventIdCollision):
            composition.advance_managed_query(
                session_started=replace(
                    session_started,
                    payload={"host_level": "activating"},
                ),
                planning_observed=intent_observed,
            )
        changed_retry = replace(
            intent_observed,
            payload={
                "observation_ref": {
                    "provider_id": "managed-query-test",
                    "opaque_id": "observation-stale-substituted",
                    "content_digest": _digest("work:stale-substituted"),
                }
            },
        )
        with pytest.raises(EventIdCollision):
            composition.advance_managed_query(
                session_started=session_started,
                planning_observed=changed_retry,
            )
        assert composition.snapshot(intent_observed.scope) == before_collision
        assert facts.calls == []

        composition.process(
            replace(
                intent_observed,
                event_id="event-turnended-stale",
                kind="TurnEnded",
                expected_revision=2,
                payload={},
            )
        )
        stale_development = _development_event(
            intent_observed,
            revision=2,
            attempt="stale-development",
        )
        before_stale = composition.snapshot(intent_observed.scope)
        with pytest.raises(RevisionConflict) as captured:
            composition.advance_managed_query(
                session_started=session_started,
                planning_observed=stale_development,
            )
        assert composition.snapshot(intent_observed.scope) == before_stale

    assert captured.value.expected == 2
    assert captured.value.actual == 3
    assert facts.calls == []
    assert composition.closed
    assert before_stale.revision == 3


@pytest.mark.parametrize(
    ("binding", "message"),
    [
        ("kind", "IntentObserved"),
        ("stream", "stable stream"),
        ("catalog", "planning environment"),
        ("planner", "planner"),
        ("host", "host_descriptor_digest"),
        ("semantic-model", "semantic_model_digest"),
        ("semantic-index", "semantic_index_digest"),
        ("plan", "plan and work identity"),
        ("work", "plan and work identity"),
    ],
)
def test_managed_advance_rejects_substituted_event_identity_before_start(
    tmp_path: Path,
    binding: str,
    message: str,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    facts = _Facts()
    with _composition(
        tmp_path,
        candidates=candidates,
        facts=facts,
        suffix=f"identity-{binding}",
    ) as composition:
        session_started, intent_observed = _managed_events(
            composition,
            suffix=f"identity-{binding}",
        )
        if binding == "kind":
            intent_observed = replace(intent_observed, kind="DevelopmentObserved")
        elif binding == "stream":
            intent_observed = replace(intent_observed, scope=_scope("foreign-stream"))
        elif binding == "catalog":
            intent_observed = replace(
                intent_observed,
                catalog_snapshot_digest=_digest("foreign-catalog"),
            )
        elif binding == "planner":
            intent_observed = replace(intent_observed, planner_version="foreign-planner")
        elif binding == "host":
            intent_observed = replace(
                intent_observed,
                host_descriptor_digest=_digest("foreign-host"),
            )
        elif binding == "semantic-model":
            intent_observed = replace(
                intent_observed,
                semantic_model_digest=_digest("foreign-semantic-model"),
            )
        elif binding == "semantic-index":
            intent_observed = replace(
                intent_observed,
                semantic_index_digest=_digest("foreign-semantic-index"),
            )
        elif binding == "plan":
            intent_observed = replace(intent_observed, correlation_id="plan-foreign")
        else:
            intent_observed = replace(
                intent_observed,
                work_signature=_digest("foreign-work"),
            )

        before = composition.snapshot(session_started.scope)
        with pytest.raises(ManagedQueryError, match=message):
            composition.advance_managed_query(
                session_started=session_started,
                planning_observed=intent_observed,
            )
        assert composition.snapshot(session_started.scope) == before

    assert facts.calls == []


def test_managed_advance_rejects_pending_consent_and_effect_before_planning(
    tmp_path: Path,
) -> None:
    capability_id = "skill:pending"
    installation_snapshot = _digest("pending-installation")
    bundle = _install_bundle(capability_id, installation_snapshot)
    candidates = (
        _candidate(
            capability_id,
            actionability="install",
            installation_snapshot_digest=installation_snapshot,
        ),
    )
    facts = _Facts()
    with _composition(
        tmp_path,
        candidates=candidates,
        install_authority=_InstallAuthority(
            installation_snapshot_digest=installation_snapshot,
            bundles={capability_id: bundle},
        ),
        facts=facts,
        interactive_install_decision_guard=lambda _reservation: nullcontext(),
        trusted_utc_now=lambda: datetime(2026, 8, 2, 12, 1, tzinfo=UTC),
        suffix="pending",
    ) as composition:
        session_started, intent_observed = _managed_events(composition, suffix="pending")
        composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        snapshot = composition.snapshot(intent_observed.scope)
        assert snapshot.state is not None
        committed = snapshot.state.committed_plan
        assert isinstance(committed, CommittedPlanV3)
        capability = committed.capabilities[0]
        desired = composition.process(
            replace(
                intent_observed,
                event_id="event-pending-desired",
                kind="ReassessmentRequested",
                expected_revision=2,
                payload={
                    "owner_id": "owner-pending",
                    "policy_snapshot_digest": InstallConsentPolicy.safe_default().policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability.capability_id,
                            "source_digest": capability.source_digest,
                            "kind": capability.kind,
                            "actionability": capability.actionability,
                            "install_descriptor_digest": capability.install_descriptor_digest,
                            "install_plan_digest": capability.install_plan_digest,
                            "lease_id": "lease-pending",
                        }
                    ],
                },
            )
        )
        request = desired.actions[0]
        assert request.kind == "RequestConsent"
        facts.calls.clear()
        pending_consent = composition.snapshot(intent_observed.scope)
        with pytest.raises(ManagedQueryError, match="settle pending authority"):
            composition.advance_managed_query(
                session_started=session_started,
                planning_observed=_development_event(
                    intent_observed,
                    revision=3,
                    attempt="pending-consent",
                ),
            )
        assert composition.snapshot(intent_observed.scope) == pending_consent
        assert facts.calls == []

        granted = composition.process(
            replace(
                intent_observed,
                event_id="event-pending-consent-granted",
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
        assert granted.actions[0].kind == "InstallCapability"
        pending_effect = composition.snapshot(intent_observed.scope)
        with pytest.raises(ManagedQueryError, match="settle pending authority"):
            composition.advance_managed_query(
                session_started=session_started,
                planning_observed=_development_event(
                    intent_observed,
                    revision=4,
                    attempt="pending-effect",
                ),
            )
        assert composition.snapshot(intent_observed.scope) == pending_effect

    assert facts.calls == []


def test_managed_advance_rejects_ended_session_but_reopen_remains_available(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    facts = _Facts()
    with _composition(
        tmp_path,
        candidates=candidates,
        facts=facts,
        suffix="ended",
    ) as composition:
        session_started, intent_observed = _managed_events(composition, suffix="ended")
        initial = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        composition.process(
            replace(
                intent_observed,
                event_id="event-sessionended-ended",
                kind="SessionEnded",
                expected_revision=2,
                payload={},
            )
        )
        facts.calls.clear()
        reopened = composition.reopen_managed_query(
            intent_observed.scope,
            expected_plan_id=initial.prepared.plan_id,
        )
        ended = composition.snapshot(intent_observed.scope)
        with pytest.raises(ManagedQueryError, match="session has ended"):
            composition.advance_managed_query(
                session_started=session_started,
                planning_observed=_development_event(
                    intent_observed,
                    revision=3,
                    attempt="ended-later",
                ),
            )
        assert composition.snapshot(intent_observed.scope) == ended

    assert reopened.journal_revision == 3
    assert _explanatory_fields(reopened) == _explanatory_fields(initial.prepared)
    assert facts.calls == []


def test_concurrent_managed_development_attempts_commit_exactly_one_winner(
    tmp_path: Path,
) -> None:
    candidates = (_candidate("skill:test", actionability="manual"),)
    barrier = Barrier(2)
    base_normalizer = _normalizer(5)

    def racing_normalizer(
        reference: ObservationReference,
        state: EngineState | None,
    ) -> StructuredSurrogate:
        if reference.opaque_id in {
            "observation-race-first",
            "observation-race-second",
        }:
            barrier.wait(timeout=5)
        return base_normalizer(reference, state)

    first = _composition(
        tmp_path,
        candidates=candidates,
        observation_normalizer=racing_normalizer,
        suffix="race",
    )
    second = _composition(
        tmp_path,
        candidates=candidates,
        observation_normalizer=racing_normalizer,
        suffix="race",
    )
    with first, second:
        session_started, intent_observed = _managed_events(first, suffix="race")
        first.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        attempts = (
            _development_event(
                intent_observed,
                revision=2,
                attempt="race-first",
            ),
            _development_event(
                intent_observed,
                revision=2,
                attempt="race-second",
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(
                executor.submit(
                    composition.advance_managed_query,
                    session_started=session_started,
                    planning_observed=attempt,
                )
                for composition, attempt in zip((first, second), attempts, strict=True)
            )
            results: list[ManagedAdvanceResult] = []
            errors: list[BaseException] = []
            for future in futures:
                try:
                    results.append(future.result(timeout=10))
                except BaseException as exc:
                    errors.append(exc)

        assert len(results) == 1, errors
        assert len(errors) == 1
        assert isinstance(errors[0], RevisionConflict)
        winner = results[0]
        latest = first.reopen_managed_query(
            intent_observed.scope,
            expected_plan_id=winner.prepared.plan_id,
        )
        snapshot = first.snapshot(intent_observed.scope)

    assert latest.plan_id == winner.prepared.plan_id
    assert snapshot.revision == 3


def test_managed_development_preserves_exact_lifecycle_transition_on_retry(
    tmp_path: Path,
) -> None:
    capability_id = "skill:installed"
    installation_snapshot = _digest("lifecycle-installation")
    bundle = _install_bundle(capability_id, installation_snapshot)
    candidates = (
        _candidate(
            capability_id,
            actionability="install",
            installation_snapshot_digest=installation_snapshot,
        ),
    )
    facts = _Facts()
    initial_normalizer = _normalizer(5)
    abstaining_normalizer = _normalizer(0)

    def phase_normalizer(
        reference: ObservationReference,
        state: EngineState | None,
    ) -> StructuredSurrogate:
        normalizer = (
            abstaining_normalizer
            if reference.opaque_id == "observation-lifecycle-later"
            else initial_normalizer
        )
        return normalizer(reference, state)

    with _composition(
        tmp_path,
        candidates=candidates,
        install_authority=_InstallAuthority(
            installation_snapshot_digest=installation_snapshot,
            bundles={capability_id: bundle},
        ),
        facts=facts,
        observation_normalizer=phase_normalizer,
        interactive_install_decision_guard=lambda _reservation: nullcontext(),
        trusted_utc_now=lambda: datetime(2026, 8, 2, 12, 1, tzinfo=UTC),
        suffix="lifecycle",
    ) as composition:
        session_started, intent_observed = _managed_events(
            composition,
            suffix="lifecycle",
        )
        composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        snapshot = composition.snapshot(intent_observed.scope)
        assert snapshot.state is not None
        committed = snapshot.state.committed_plan
        assert isinstance(committed, CommittedPlanV3)
        capability = committed.capabilities[0]
        desired = composition.process(
            replace(
                intent_observed,
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
                intent_observed,
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
            host_identity_digest=_digest("managed-query-host"),
            target_identity_digest=_digest("managed-query-target"),
        )
        engine = composition._engine  # noqa: SLF001 - exact coordinator settlement seam.
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
            verification_digest=_digest("managed-query-install-observation"),
        )
        install_status = engine.install_execution_status(install)
        assert install_status.observed_at is not None
        install_receipt = replace(
            intent_observed,
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
        applied = composition.execute_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
            verification_digest=_digest("managed-query-activation-observation"),
        )
        assert applied.outcome == "applied"
        assert applied.settled is True
        assert applied.claim_was_new is True
        # Re-entry after a settled activation is idempotent: no new claim, no
        # journal advance, and no fresh receipt transition.
        head_after_activation = composition.snapshot(intent_observed.scope).revision
        replayed = composition.execute_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
            verification_digest=_digest("managed-query-activation-observation"),
        )
        assert replayed.outcome == "applied"
        assert replayed.settled is True
        assert replayed.claim_was_new is False
        assert replayed.transition is None
        assert composition.snapshot(intent_observed.scope).revision == head_after_activation
        development = _development_event(
            intent_observed,
            revision=6,
            attempt="lifecycle-later",
        )

        advanced = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=development,
        )
        facts.calls.clear()
        retried = composition.advance_managed_query(
            session_started=session_started,
            planning_observed=development,
        )

    assert advanced.prepared.status == "abstained"
    assert [action.kind for action in advanced.transition.actions] == ["DeactivateCapability"]
    assert retried.transition == advanced.transition
    assert facts.calls == []


@contextmanager
def _pending_activation_composition(tmp_path: Path, *, suffix: str):
    """Yield a composition holding one installed-inactive skill with a pending
    ``ActivateCapability`` action, plus the exact action and execution binding."""

    capability_id = "skill:installed"
    installation_snapshot = _digest(f"{suffix}-installation")
    bundle = _install_bundle(capability_id, installation_snapshot)
    candidates = (
        _candidate(
            capability_id,
            actionability="install",
            installation_snapshot_digest=installation_snapshot,
        ),
    )
    with _composition(
        tmp_path,
        candidates=candidates,
        install_authority=_InstallAuthority(
            installation_snapshot_digest=installation_snapshot,
            bundles={capability_id: bundle},
        ),
        facts=_Facts(),
        observation_normalizer=_normalizer(5),
        interactive_install_decision_guard=lambda _reservation: nullcontext(),
        trusted_utc_now=lambda: datetime(2026, 8, 2, 12, 1, tzinfo=UTC),
        suffix=suffix,
    ) as composition:
        session_started, intent_observed = _managed_events(composition, suffix=suffix)
        composition.advance_managed_query(
            session_started=session_started,
            planning_observed=intent_observed,
        )
        snapshot = composition.snapshot(intent_observed.scope)
        assert snapshot.state is not None
        committed = snapshot.state.committed_plan
        assert isinstance(committed, CommittedPlanV3)
        capability = committed.capabilities[0]
        desired = composition.process(
            replace(
                intent_observed,
                event_id=f"event-{suffix}-desired",
                kind="ReassessmentRequested",
                expected_revision=2,
                payload={
                    "owner_id": f"owner-{suffix}",
                    "policy_snapshot_digest": InstallConsentPolicy.safe_default().policy_digest,
                    "desired_capabilities": [
                        {
                            "capability_id": capability.capability_id,
                            "source_digest": capability.source_digest,
                            "kind": capability.kind,
                            "actionability": capability.actionability,
                            "install_descriptor_digest": capability.install_descriptor_digest,
                            "install_plan_digest": capability.install_plan_digest,
                            "lease_id": f"lease-{suffix}",
                        }
                    ],
                },
            )
        )
        request = desired.actions[0]
        assert request.kind == "RequestConsent"
        granted = composition.process(
            replace(
                intent_observed,
                event_id=f"event-{suffix}-consent-granted",
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
            host_identity_digest=_digest("managed-query-host"),
            target_identity_digest=_digest("managed-query-target"),
        )
        engine = composition._engine  # noqa: SLF001 - exact coordinator settlement seam.
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
            execution_authority=engine._issue_install_outcome_permit(install, binding),  # noqa: SLF001
            outcome="applied",
            observed_material_identity_digest=bundle.result_material.identity_digest,
            verification_digest=_digest("managed-query-install-observation"),
        )
        install_status = engine.install_execution_status(install)
        assert install_status.observed_at is not None
        install_receipt = replace(
            intent_observed,
            event_id=f"event-{suffix}-install-applied",
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
        yield composition, session_started, intent_observed, activation, binding


def _activation_observation() -> str:
    return _digest("managed-query-activation-observation")


def test_execute_activation_recovers_a_recorded_but_unsettled_outcome(tmp_path: Path) -> None:
    with _pending_activation_composition(tmp_path, suffix="rec-outcome") as (
        composition,
        session_started,
        intent_observed,
        activation,
        binding,
    ):
        engine = composition._engine  # noqa: SLF001 - crash-injection seam.
        # Simulate a crash after the durable outcome but before the receipt.
        engine.authorize_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
        )
        engine._record_activation_outcome(  # noqa: SLF001
            activation,
            execution_binding=binding,
            execution_authority=engine._issue_activation_outcome_permit(activation, binding),  # noqa: SLF001
            observed_material_identity_digest=activation.payload["material_identity"][  # type: ignore[index]
                "identity_digest"
            ],
            verification_digest=_activation_observation(),
        )
        status = engine.activation_execution_status(activation)
        assert status.outcome_recorded is True
        assert status.settled is False
        head_before = composition.snapshot(intent_observed.scope).revision

        report = composition.execute_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
            verification_digest=_activation_observation(),
        )

        assert report.outcome == "applied"
        assert report.settled is True
        assert report.claim_was_new is False
        assert report.transition is not None
        assert composition.snapshot(intent_observed.scope).revision == head_before + 1
        assert engine.activation_execution_status(activation).settled is True


def test_execute_activation_recovers_after_a_claim_without_outcome(tmp_path: Path) -> None:
    with _pending_activation_composition(tmp_path, suffix="rec-claim") as (
        composition,
        session_started,
        intent_observed,
        activation,
        binding,
    ):
        engine = composition._engine  # noqa: SLF001 - crash-injection seam.
        # Simulate a crash after the durable claim but before the outcome.
        engine.authorize_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
        )
        status = engine.activation_execution_status(activation)
        assert status.claimed is True
        assert status.outcome_recorded is False
        head_before = composition.snapshot(intent_observed.scope).revision

        report = composition.execute_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
            verification_digest=_activation_observation(),
        )

        assert report.outcome == "applied"
        assert report.settled is True
        assert report.claim_was_new is False
        assert composition.snapshot(intent_observed.scope).revision == head_before + 1


def test_execute_activation_tolerates_a_lost_claim_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _pending_activation_composition(tmp_path, suffix="race-claim") as (
        composition,
        session_started,
        intent_observed,
        activation,
        binding,
    ):
        engine = composition._engine  # noqa: SLF001 - race-injection seam.
        # A competing owner has already burned the exact one-use claim.
        engine.authorize_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
        )
        real_status = engine.activation_execution_status
        unclaimed = ActivationExecutionStatus(
            claimed=False,
            outcome_recorded=False,
            settled=False,
            execution_binding_digest=None,
            outcome_digest=None,
            observed_at=None,
        )
        calls = {"n": 0}

        def flaky_status(action: object) -> ActivationExecutionStatus:
            calls["n"] += 1
            if calls["n"] == 1:
                # Force execute_activation into the authorize branch even though
                # the claim already exists, so the ActivationActionAlreadyClaimed
                # catch and re-read path are exercised deterministically.
                return unclaimed
            return real_status(action)

        monkeypatch.setattr(engine, "activation_execution_status", flaky_status)

        report = composition.execute_activation(
            activation,
            execution_binding=binding,
            expected_host_descriptor_digest=session_started.host_descriptor_digest or "",
            verification_digest=_activation_observation(),
        )

        assert report.outcome == "applied"
        assert report.settled is True
        assert report.claim_was_new is False
        assert calls["n"] >= 2
