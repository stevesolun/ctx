from __future__ import annotations

import hashlib
from pathlib import Path

import networkx as nx

from ctx.adapters.claude_code.engine_hook import render_recommendation_hook
from ctx.adapters.codex.engine_adapter import render_recommendation_context
from ctx.core.resolve.engine_candidates import GraphCandidateSource
from ctx.engine.engine import CtxEngine
from ctx.engine.planner import (
    BoundedCapabilityPlanner,
    ReplayDecisionPlanner,
)
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.reducer import PLANNING_REDUCER_VERSION
from ctx.engine.replay import (
    DefaultReplayInputFactory,
    ObservationReference,
    StructuredSurrogate,
)
from ctx.engine.state import EngineState
from ctx.engine.store import SQLiteEngineStore


NOW = "2026-08-01T12:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
    catalog_snapshot_digest: str,
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
        catalog_snapshot_digest=catalog_snapshot_digest,
        semantic_model_digest=_digest("model"),
        semantic_index_digest=_digest("index"),
        work_signature=_digest("work"),
        random_seed=17,
    )


def _graph() -> nx.Graph:
    graph = nx.Graph()
    for node_id, kind in (
        ("skill:python-tdd", "skill"),
        ("agent:python-reviewer", "agent"),
        ("mcp-server:python-docs", "mcp-server"),
        ("harness:python-runner", "harness"),
        ("skill:python-security", "skill"),
        ("skill:python-lint", "skill"),
        ("skill:python-types", "skill"),
    ):
        graph.add_node(
            node_id,
            label=node_id.split(":", 1)[1],
            type=kind,
            tags=["python"],
        )
    return graph


def _normalize(
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
            "requested_limit": 5,
        },
    )


def test_graph_engine_and_both_host_renderers_share_one_exact_bundle(
    tmp_path: Path,
) -> None:
    source = GraphCandidateSource(_graph())
    planner = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(source),
        planner_version="planner-v1",
    )
    engine = CtxEngine(
        store=SQLiteEngineStore(tmp_path / "engine.sqlite3"),
        replay_factory=DefaultReplayInputFactory(
            observation_normalizer=_normalize,
            decision_planner=planner,
            reducer_version=PLANNING_REDUCER_VERSION,
        ),
    )
    engine.process(
        _event(
            "SessionStarted",
            0,
            "event-start",
            source.catalog_snapshot_digest,
        )
    )

    transition = engine.process(
        _event(
            "IntentObserved",
            1,
            "event-intent",
            source.catalog_snapshot_digest,
            payload={
                "observation_ref": {
                    "provider_id": "host-buffer",
                    "opaque_id": "observation-1",
                    "content_digest": _digest("python-work"),
                }
            },
        )
    )

    capabilities = transition.actions[0].payload["capabilities"]
    assert [row["capability_id"] for row in capabilities] == [
        "agent:python-reviewer",
        "harness:python-runner",
        "mcp-server:python-docs",
        "skill:python-lint",
        "skill:python-security",
    ]
    codex_context = render_recommendation_context(transition)
    claude_envelope = render_recommendation_hook(transition)
    assert codex_context is not None
    assert claude_envelope is not None
    assert claude_envelope["hookSpecificOutput"]["additionalContext"] == codex_context


def test_every_committed_graph_bundle_is_renderer_ready_at_evidence_boundary(
    tmp_path: Path,
) -> None:
    long_signals = tuple(sorted(f"signal-{index:02d}-" + "x" * 109 for index in range(28)))
    graph = nx.Graph()
    for index in range(3):
        graph.add_node(
            f"skill:boundary-{index}",
            label=f"boundary-{index}",
            type="skill",
            tags=list(long_signals),
        )
    source = GraphCandidateSource(graph)
    planner = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(source),
        planner_version="planner-v1",
    )

    def normalize_boundary(
        _reference: ObservationReference,
        _state: EngineState | None,
    ) -> StructuredSurrogate:
        return StructuredSurrogate.create(
            schema_id="ctx.observation.current-work",
            schema_version=1,
            value={
                "signals": list(long_signals),
                "languages": [],
                "baseline_capability_ids": [],
                "active_capability_ids": [],
                "rejected_capability_ids": [],
                "requested_limit": 5,
            },
        )

    engine = CtxEngine(
        store=SQLiteEngineStore(tmp_path / "engine-boundary.sqlite3"),
        replay_factory=DefaultReplayInputFactory(
            observation_normalizer=normalize_boundary,
            decision_planner=planner,
            reducer_version=PLANNING_REDUCER_VERSION,
        ),
    )
    engine.process(
        _event("SessionStarted", 0, "event-boundary-start", source.catalog_snapshot_digest)
    )
    transition = engine.process(
        _event(
            "IntentObserved",
            1,
            "event-boundary-intent",
            source.catalog_snapshot_digest,
            payload={
                "observation_ref": {
                    "provider_id": "host-buffer",
                    "opaque_id": "observation-boundary",
                    "content_digest": _digest("boundary-work"),
                }
            },
        )
    )

    codex_context = render_recommendation_context(transition)
    claude_envelope = render_recommendation_hook(transition)
    assert codex_context is not None
    assert claude_envelope is not None
    assert claude_envelope["hookSpecificOutput"]["additionalContext"] == codex_context
