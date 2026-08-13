from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

import pytest

from ctx.engine.engine import CtxEngine
from ctx.engine.planner import (
    BoundedCapabilityPlanner,
    CapabilityCandidate,
    PlannerValidationError,
    ReplayDecisionPlanner,
    WorkObservation,
)
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.reducer import PLANNING_REDUCER_VERSION
from ctx.engine.replay import (
    DefaultReplayInputFactory,
    ObservationReference,
    PlanningContext,
    StructuredSurrogate,
)
from ctx.engine.state import CapabilityState, EngineState
from ctx.engine.store import SQLiteEngineStore, StreamId


NOW = "2026-08-01T12:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _capability_rows(decision: StructuredSurrogate) -> tuple[Mapping[str, object], ...]:
    raw_capabilities = decision.value["capabilities"]
    assert isinstance(raw_capabilities, tuple)
    rows: list[Mapping[str, object]] = []
    for row in raw_capabilities:
        assert isinstance(row, Mapping)
        rows.append(row)
    return tuple(rows)


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id="host-1",
    )


def _event(
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
        engine_version="engine-v1",
        planner_version="planner-v1",
        policy_version="policy-v1",
        host_descriptor_digest=_digest("host"),
        catalog_snapshot_digest=_digest("catalog"),
        semantic_model_digest=_digest("model"),
        semantic_index_digest=_digest("index"),
        work_signature=_digest("work"),
        random_seed=17,
    )


def _normalizer(
    reference: ObservationReference,
    _state: EngineState | None,
) -> StructuredSurrogate:
    assert reference.provider_id == "host-buffer"
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": ["fastapi", "python", "testing"],
            "languages": ["python"],
            "baseline_capability_ids": [],
            "active_capability_ids": [],
            "rejected_capability_ids": [],
            "requested_limit": 5,
        },
    )


def _decision() -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=1,
        value={
            "status": "ready",
            "abstention_code": None,
            "capabilities": [
                {
                    "capability_id": "skill:fastapi-python-testing",
                    "kind": "skill",
                    "name": "fastapi-python-testing",
                    "catalog_entry_digest": _digest("fastapi-python-testing"),
                    "normalized_score_ppm": 950_000,
                    "matching_signals": ["fastapi", "python", "testing"],
                    "reason_codes": ["signal-match"],
                    "actionability": "load",
                }
            ],
        },
    )


def test_planned_observation_is_durable_idempotent_and_replays_without_planning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(path)
    planner_calls = 0

    def planner(
        observation: StructuredSurrogate,
        state: EngineState | None,
        _context: PlanningContext,
    ) -> StructuredSurrogate:
        nonlocal planner_calls
        planner_calls += 1
        assert observation.schema_id == "ctx.observation.current-work"
        assert state is not None and state.revision == 1
        return _decision()

    factory = DefaultReplayInputFactory(
        observation_normalizer=_normalizer,
        decision_planner=planner,
        reducer_version=PLANNING_REDUCER_VERSION,
    )
    engine = CtxEngine(store=store, replay_factory=factory)
    engine.process(_event("SessionStarted", 0, "event-start"))
    raw_opaque_id = "volatile-observation-1"
    observation = _event(
        "IntentObserved",
        1,
        "event-intent",
        payload={
            "observation_ref": {
                "provider_id": "host-buffer",
                "opaque_id": raw_opaque_id,
                "content_digest": _digest("normalized-work-1"),
            }
        },
    )

    transition = engine.process(observation)
    duplicate = engine.process(observation)

    assert planner_calls == 1
    assert duplicate.to_json() == transition.to_json()
    assert [action.kind for action in transition.actions] == ["PresentBundle"]
    assert transition.actions[0].payload["capabilities"][0]["capability_id"] == (
        "skill:fastapi-python-testing"
    )
    records = tuple(store.records(StreamId.from_scope(observation.scope)))
    assert records[-1].reducer_version == PLANNING_REDUCER_VERSION
    assert records[-1].transition_json == transition.to_json()

    snapshot = CtxEngine(store=store).snapshot(observation.scope)

    assert snapshot.revision == 2
    assert planner_calls == 1
    persisted = b"".join(
        candidate.read_bytes()
        for candidate in path.parent.iterdir()
        if candidate.name.startswith(path.name)
    )
    assert raw_opaque_id.encode() not in persisted


def test_real_planner_uses_one_global_budget_across_all_capability_kinds(
    tmp_path: Path,
) -> None:
    candidates = tuple(
        CapabilityCandidate(
            capability_id=f"{kind}:{name}",
            kind=kind,
            name=name,
            source_digest=_digest(name),
            normalized_score_ppm=score,
            matching_signals=("python",),
            reason_codes=("signal-match",),
            actionability="manual",
        )
        for kind, name, score in (
            ("skill", "python-testing", 950_000),
            ("agent", "python-reviewer", 900_000),
            ("mcp-server", "python-docs", 850_000),
            ("harness", "python-codex", 800_000),
            ("skill", "python-security", 750_000),
            ("agent", "python-migrations", 700_000),
        )
    )

    class StaticSource:
        catalog_snapshot_digest = _digest("catalog")

        def retrieve(
            self,
            _observation: WorkObservation,
        ) -> tuple[CapabilityCandidate, ...]:
            return candidates

    decision_planner = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(StaticSource()),
        planner_version="planner-v1",
    )
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    engine = CtxEngine(
        store=store,
        replay_factory=DefaultReplayInputFactory(
            observation_normalizer=_normalizer,
            decision_planner=decision_planner,
            reducer_version=PLANNING_REDUCER_VERSION,
        ),
    )
    engine.process(_event("SessionStarted", 0, "event-start"))

    transition = engine.process(
        _event(
            "IntentObserved",
            1,
            "event-intent",
            payload={
                "observation_ref": {
                    "provider_id": "host-buffer",
                    "opaque_id": "observation-1",
                    "content_digest": _digest("normalized-work-1"),
                }
            },
        )
    )

    capabilities = transition.actions[0].payload["capabilities"]
    assert len(capabilities) == 5
    assert [row["kind"] for row in capabilities] == [
        "skill",
        "agent",
        "mcp-server",
        "harness",
        "skill",
    ]


def test_nonplanning_observations_are_journaled_without_calling_the_planner(
    tmp_path: Path,
) -> None:
    planner_calls = 0

    def planner(
        _observation: StructuredSurrogate,
        _state: EngineState | None,
        _context: PlanningContext,
    ) -> StructuredSurrogate:
        nonlocal planner_calls
        planner_calls += 1
        return _decision()

    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    engine = CtxEngine(
        store=store,
        replay_factory=DefaultReplayInputFactory(
            observation_normalizer=_normalizer,
            decision_planner=planner,
            reducer_version=PLANNING_REDUCER_VERSION,
        ),
    )
    engine.process(_event("SessionStarted", 0, "event-start"))
    transitions = []
    for revision, kind in enumerate(
        ("WorkspaceObserved", "ValidationObserved"),
        start=1,
    ):
        transitions.append(
            engine.process(
                _event(
                    kind,
                    revision,
                    f"event-{kind.lower()}",
                    payload={
                        "observation_ref": {
                            "provider_id": "host-buffer",
                            "opaque_id": f"observation-{revision}",
                            "content_digest": _digest(f"normalized-work-{revision}"),
                        }
                    },
                )
            )
        )

    assert planner_calls == 0
    assert [transition.actions for transition in transitions] == [(), ()]
    assert engine.snapshot(_scope()).revision == 3


def test_replay_planner_excludes_capabilities_active_in_authoritative_state() -> None:
    active = CapabilityState(
        capability_id="skill:python-testing",
        source_digest=_digest("python-testing"),
        plan_id="plan-1",
        catalog_snapshot_id=_digest("catalog"),
        activation="active",
        activation_lease_id="activation-1",
    )
    state = EngineState(
        revision=1,
        scope=_scope(),
        host_level="activating",
        host_descriptor_digest=_digest("host"),
        capabilities=(active,),
    )
    candidates = (
        CapabilityCandidate(
            capability_id="skill:python-testing",
            kind="skill",
            name="python-testing",
            source_digest=_digest("python-testing"),
            normalized_score_ppm=950_000,
            matching_signals=("python",),
            reason_codes=("signal-match",),
            actionability="manual",
        ),
        CapabilityCandidate(
            capability_id="agent:python-reviewer",
            kind="agent",
            name="python-reviewer",
            source_digest=_digest("python-reviewer"),
            normalized_score_ppm=900_000,
            matching_signals=("python",),
            reason_codes=("signal-match",),
            actionability="manual",
        ),
    )

    class StaticSource:
        catalog_snapshot_digest = _digest("catalog")

        def retrieve(
            self,
            _observation: WorkObservation,
        ) -> tuple[CapabilityCandidate, ...]:
            return candidates

    planner = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(StaticSource()),
        planner_version="planner-v1",
    )
    observation = _normalizer(
        ObservationReference(
            provider_id="host-buffer",
            opaque_id="observation-1",
            content_digest=_digest("work"),
        ),
        None,
    )

    decision = planner(
        observation,
        state,
        PlanningContext(
            planner_version="planner-v1",
            catalog_snapshot_digest=_digest("catalog"),
        ),
    )

    assert [row["capability_id"] for row in _capability_rows(decision)] == ["agent:python-reviewer"]


def test_replay_planner_rejects_event_catalog_or_planner_version_mismatch() -> None:
    class StaticSource:
        catalog_snapshot_digest = _digest("catalog-b")

        def retrieve(
            self,
            _observation: WorkObservation,
        ) -> tuple[CapabilityCandidate, ...]:
            return ()

    planner = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(StaticSource()),
        planner_version="planner-v2",
    )
    observation = _normalizer(
        ObservationReference(
            provider_id="host-buffer",
            opaque_id="observation-1",
            content_digest=_digest("work"),
        ),
        None,
    )

    for context in (
        PlanningContext(
            planner_version="planner-v1",
            catalog_snapshot_digest=_digest("catalog-b"),
        ),
        PlanningContext(
            planner_version="planner-v2",
            catalog_snapshot_digest=_digest("catalog-a"),
        ),
    ):
        with pytest.raises(PlannerValidationError, match="mismatch"):
            planner(observation, None, context)
