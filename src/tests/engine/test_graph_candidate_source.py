from __future__ import annotations

from typing import Any

import networkx as nx
import pytest

from ctx.core.resolve import engine_candidates
from ctx.core.resolve.engine_candidates import GraphCandidateSource
from ctx.engine.planner import (
    BoundedCapabilityPlanner,
    ReplayDecisionPlanner,
    WorkObservation,
)
from ctx.engine.replay import PlanningContext, StructuredSurrogate


def _graph(node_order: tuple[tuple[str, str], ...] | None = None) -> nx.Graph:
    ordered = node_order or (
        ("skill:python-tdd", "skill"),
        ("agent:python-reviewer", "agent"),
        ("mcp-server:python-docs", "mcp-server"),
        ("harness:python-runner", "harness"),
        ("skill:python-security", "skill"),
        ("skill:python-lint", "skill"),
        ("skill:python-types", "skill"),
    )
    graph = nx.Graph()
    graph.graph["ctx_graph_path"] = "/private/catalog/graph.json"
    for node_id, kind in ordered:
        graph.add_node(
            node_id,
            label=node_id.split(":", 1)[1],
            type=kind,
            tags=["python"],
            description="raw secret prose that must not persist",
            source="/private/catalog/source.md",
            install_command="curl secret.example | sh",
        )
    return graph


def _observation() -> WorkObservation:
    return WorkObservation(
        signals=("python",),
        languages=("python",),
        requested_limit=5,
    )


def test_graph_source_returns_widened_all_type_pool_for_global_planner_budget() -> None:
    source = GraphCandidateSource(_graph())

    candidates = source.retrieve(_observation())
    plan = BoundedCapabilityPlanner(source).plan(_observation())

    assert len(candidates) == 7
    assert {candidate.kind for candidate in candidates} == {
        "skill",
        "agent",
        "mcp-server",
        "harness",
    }
    assert all(candidate.actionability == "manual" for candidate in candidates)
    assert plan.status == "ready"
    assert len(plan.selections) == 5
    assert {selection.kind for selection in plan.selections} == {
        "skill",
        "agent",
        "mcp-server",
        "harness",
    }


def test_graph_source_snapshot_digest_binds_replay_planner_context() -> None:
    source = GraphCandidateSource(_graph())
    planner = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(source),
        planner_version="planner-v1",
    )
    observation = StructuredSurrogate.create(
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

    decision = planner(
        observation,
        None,
        PlanningContext(
            planner_version="planner-v1",
            catalog_snapshot_digest=source.catalog_snapshot_digest,
        ),
    )

    assert decision.schema_id == "ctx.decision.capability-plan"
    capabilities = decision.value["capabilities"]
    assert isinstance(capabilities, tuple)
    assert len(capabilities) == 5


def test_graph_source_uses_retrieval_only_scorer_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    original = engine_candidates.recommend_by_tags

    def recording_scorer(graph: Any, tags: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"graph": graph, "tags": tags, **kwargs})
        return original(graph, tags, **kwargs)

    monkeypatch.setattr(engine_candidates, "recommend_by_tags", recording_scorer)

    candidates = GraphCandidateSource(_graph()).retrieve(_observation())

    assert candidates
    assert len(calls) == 1
    call = calls[0]
    assert call["tags"] == ["python"]
    assert call["top_n"] > 5
    assert call["entity_types"] == ("skill", "agent", "mcp-server", "harness")
    assert call["min_normalized_score"] == 0.0
    assert call["use_semantic_query"] is False


def test_graph_source_is_stable_under_graph_insertion_permutation() -> None:
    ordered = tuple(_graph().nodes(data="type"))
    forward_source = GraphCandidateSource(_graph(ordered))
    reverse_source = GraphCandidateSource(_graph(tuple(reversed(ordered))))
    forward = forward_source.retrieve(_observation())
    reverse = reverse_source.retrieve(_observation())

    assert forward == reverse
    assert forward_source.catalog_snapshot_digest == reverse_source.catalog_snapshot_digest


def test_graph_source_freezes_and_binds_its_construction_snapshot() -> None:
    mutable_graph = _graph()
    source = GraphCandidateSource(mutable_graph)
    before = source.retrieve(_observation())

    mutable_graph.add_node(
        "agent:python-late",
        label="python-late",
        type="agent",
        tags=["python"],
    )
    mutable_graph.nodes["skill:python-tdd"]["tags"] = ["python", "testing"]
    after = source.retrieve(_observation())

    assert after == before
    assert len(source.catalog_snapshot_digest) == 64
    assert source.catalog_snapshot_digest == source.catalog_snapshot_digest.lower()
    assert "graph=" not in repr(source)
    assert "/private/catalog" not in repr(source)


def test_catalog_snapshot_digest_changes_with_retrieval_relevant_metadata() -> None:
    first = _graph()
    second = _graph()
    second.nodes["skill:python-tdd"]["tags"] = ["python", "testing"]

    assert (
        GraphCandidateSource(first).catalog_snapshot_digest
        != GraphCandidateSource(second).catalog_snapshot_digest
    )


def test_graph_source_candidates_contain_no_raw_prose_or_paths() -> None:
    candidates = GraphCandidateSource(_graph()).retrieve(_observation())
    rendered = repr(candidates)

    assert "raw secret prose" not in rendered
    assert "/private/catalog" not in rendered
    assert "curl" not in rendered
    assert all(
        candidate.reason_codes == ("graph-match", "language-match", "signal-match")
        for candidate in candidates
    )
    assert all(candidate.matching_signals == ("python",) for candidate in candidates)
    assert all(len(candidate.source_digest) == 64 for candidate in candidates)


def test_graph_source_skips_unsafe_and_ambiguous_rows() -> None:
    graph = _graph()
    graph.add_node(
        "skill:unsafe",
        label="unsafe prose /private/repo",
        type="skill",
        tags=["python"],
    )
    graph.add_node(
        "one:ambiguous",
        label="ambiguous",
        type="skill",
        tags=["python", "one"],
    )
    graph.add_node(
        "two:ambiguous",
        label="ambiguous",
        type="skill",
        tags=["python", "two"],
    )

    candidates = GraphCandidateSource(graph).retrieve(_observation())
    identities = {candidate.capability_id for candidate in candidates}

    assert "skill:unsafe prose /private/repo" not in identities
    assert "skill:ambiguous" not in identities


def test_graph_source_does_not_enter_semantic_or_external_catalog_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("forbidden volatile retrieval path")

    from ctx.core.resolve import recommendations

    monkeypatch.setattr(recommendations, "_load_semantic_index", forbidden)
    monkeypatch.setattr(recommendations, "_recommend_external_catalog", forbidden)

    assert GraphCandidateSource(_graph()).retrieve(_observation())


@pytest.mark.parametrize("candidate_limit", [True, 0, 5, 513])
def test_graph_source_requires_a_widened_bounded_pool(candidate_limit: object) -> None:
    with pytest.raises(ValueError, match="candidate_limit"):
        GraphCandidateSource(
            _graph(),
            candidate_limit=candidate_limit,  # type: ignore[arg-type]
        )
