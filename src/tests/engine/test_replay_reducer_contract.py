from __future__ import annotations

import hashlib

import pytest

from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.reducer import (
    InvalidEventError,
    reduce_replay_v1,
    reduce_replay_v2,
)
from ctx.engine.replay import ReplayInput, StructuredSurrogate


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


def _event(kind: str, revision: int, event_id: str) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=_scope(),
        expected_revision=revision,
        occurred_at=NOW,
        payload={"host_level": "query-only"} if kind == "SessionStarted" else {},
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


def _replay(event: EngineEvent, decision: StructuredSurrogate | None = None) -> ReplayInput:
    return ReplayInput(
        source_event_content_digest=event.content_digest,
        reducer_event=event,
        decision_surrogate=decision,
        reducer_version="ctx-reducer-v2",
    )


def _decision(
    *,
    status: str = "ready",
    abstention_code: str | None = None,
) -> StructuredSurrogate:
    capabilities = (
        [
            {
                "capability_id": "skill:python-testing",
                "kind": "skill",
                "name": "python-testing",
                "catalog_entry_digest": _digest("python-testing"),
                "normalized_score_ppm": 950_000,
                "matching_signals": ["python", "testing"],
                "reason_codes": ["locally-available", "signal-match"],
                "actionability": "load",
            },
            {
                "capability_id": "mcp-server:docs",
                "kind": "mcp-server",
                "name": "docs",
                "catalog_entry_digest": _digest("docs"),
                "normalized_score_ppm": 800_000,
                "matching_signals": ["python"],
                "reason_codes": ["signal-match"],
                "actionability": "manual",
            },
        ]
        if status == "ready"
        else []
    )
    return StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=1,
        value={
            "status": status,
            "abstention_code": abstention_code,
            "capabilities": capabilities,
        },
    )


def test_v1_replay_wrapper_preserves_the_frozen_reducer_behavior() -> None:
    replay = _replay(_event("SessionStarted", 0, "event-start"))

    state, transition = reduce_replay_v1(None, replay)

    assert state.revision == 1
    assert transition.actions == ()


def test_v2_ready_decision_presents_the_exact_ordered_global_bundle() -> None:
    state, _ = reduce_replay_v2(None, _replay(_event("SessionStarted", 0, "event-start")))
    decision = _decision()

    next_state, transition = reduce_replay_v2(
        state,
        _replay(_event("IntentObserved", 1, "event-intent"), decision),
    )

    assert next_state.revision == 2
    assert len(transition.actions) == 1
    action = transition.actions[0]
    assert action.kind == "PresentBundle"
    assert action.precondition_revision == 2
    assert action.payload["plan_digest"] == decision.value_digest
    assert [row["capability_id"] for row in action.payload["capabilities"]] == [
        "skill:python-testing",
        "mcp-server:docs",
    ]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("abstained", "below-threshold"),
        ("degraded", "catalog-unavailable"),
    ],
)
def test_v2_empty_decision_emits_only_a_typed_diagnostic(status: str, code: str) -> None:
    state, _ = reduce_replay_v2(None, _replay(_event("SessionStarted", 0, "event-start")))

    _next_state, transition = reduce_replay_v2(
        state,
        _replay(
            _event("DevelopmentObserved", 1, "event-development"),
            _decision(status=status, abstention_code=code),
        ),
    )

    assert transition.actions == ()
    assert transition.diagnostics == ({"code": code},)


def test_v2_rejects_a_decision_on_a_nonplanning_event() -> None:
    with pytest.raises(InvalidEventError, match="decision"):
        reduce_replay_v2(
            None,
            _replay(_event("SessionStarted", 0, "event-start"), _decision()),
        )
