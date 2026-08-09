from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ctx.engine.engine import CtxEngine
from ctx.engine.planner import WorkObservation
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.replay import DefaultReplayInputFactory
from ctx.engine.store import SQLiteEngineStore, StreamId
from ctx.runtime.query_observation import (
    QueryObservationCapacityExceeded,
    QueryObservationRegistry,
    QueryObservationRegistryClosed,
    QueryObservationUnavailable,
)


NOW = "2026-08-02T12:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _work() -> WorkObservation:
    return WorkObservation(
        signals=("python", "testing"),
        languages=("python",),
        baseline_capability_ids=("skill:baseline",),
        active_capability_ids=("agent:reviewer",),
        rejected_capability_ids=("mcp-server:stale",),
        requested_limit=5,
    )


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id="ctx-run",
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
        correlation_id="plan-1",
        causation_id="cause-1",
        engine_version="ctx-engine-v1",
        planner_version="ctx-planner-v1",
        policy_version="policy-v1",
        host_descriptor_digest=_digest("ctx-run-query-only"),
        catalog_snapshot_digest=_digest("catalog"),
        semantic_model_digest=_digest("semantic-model"),
        semantic_index_digest=_digest("semantic-index"),
        work_signature=_digest("work"),
        random_seed=0,
    )


def test_registered_work_resolves_once_to_exact_current_work_surrogate() -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run", max_pending=2)
    reference = registry.register(_work())

    surrogate = registry(reference, None)

    assert reference.provider_id == "ctx-run"
    assert reference.content_digest == surrogate.value_digest
    assert surrogate.schema_id == "ctx.observation.current-work"
    assert surrogate.schema_version == 1
    assert dict(surrogate.value) == {
        "active_capability_ids": ("agent:reviewer",),
        "baseline_capability_ids": ("skill:baseline",),
        "languages": ("python",),
        "rejected_capability_ids": ("mcp-server:stale",),
        "requested_limit": 5,
        "signals": ("python", "testing"),
    }
    with pytest.raises(QueryObservationUnavailable, match="unavailable"):
        registry(reference, None)


def test_substituted_references_fail_without_consuming_the_registered_work() -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run")
    reference = registry.register(_work())

    substitutions = (
        replace(reference, provider_id="other-host"),
        replace(reference, opaque_id="missing-observation"),
        replace(reference, content_digest="f" * 64),
    )
    for substituted in substitutions:
        with pytest.raises(QueryObservationUnavailable, match="unavailable") as raised:
            registry(substituted, None)
        assert substituted.opaque_id not in str(raised.value)

    assert registry(reference, None).value_digest == reference.content_digest


def test_registry_is_bounded_and_discard_is_exact_and_idempotent() -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run", max_pending=1)
    reference = registry.register(_work())

    with pytest.raises(QueryObservationCapacityExceeded, match="capacity"):
        registry.register(WorkObservation(signals=("rust",), languages=("rust",)))

    assert not registry.discard(replace(reference, content_digest="f" * 64))
    with pytest.raises(QueryObservationCapacityExceeded):
        registry.register(WorkObservation(signals=("rust",), languages=("rust",)))
    assert registry.discard(reference)
    assert not registry.discard(reference)
    assert registry.register(WorkObservation(signals=("rust",), languages=("rust",)))


def test_close_clears_pending_work_and_rejects_future_use() -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run")
    reference = registry.register(_work())

    registry.close()
    registry.close()

    with pytest.raises(QueryObservationUnavailable):
        registry(reference, None)
    with pytest.raises(QueryObservationRegistryClosed, match="closed"):
        registry.register(_work())
    assert not registry.discard(reference)


def test_registry_rejects_untyped_source_material_without_echoing_it() -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run")
    raw = "private prompt /absolute/repo/path and source diff"

    with pytest.raises(TypeError, match="WorkObservation") as raised:
        registry.register(raw)  # type: ignore[arg-type]

    assert raw not in str(raised.value)


def test_concurrent_resolution_has_exactly_one_winner() -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run")
    reference = registry.register(_work())

    def resolve() -> str:
        try:
            return registry(reference, None).value_digest
        except QueryObservationUnavailable:
            return "unavailable"

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(lambda _: resolve(), range(32)))

    assert results.count(reference.content_digest) == 1
    assert results.count("unavailable") == 31


def test_exact_engine_duplicate_uses_cached_transition_after_observation_is_consumed(
    tmp_path: Path,
) -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run")
    reference = registry.register(_work())
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    engine = CtxEngine(
        store=store,
        replay_factory=DefaultReplayInputFactory(observation_normalizer=registry),
    )
    engine.process(
        _event(
            "SessionStarted",
            0,
            "event-start",
            payload={"host_level": "query-only"},
        )
    )
    observed = _event(
        "IntentObserved",
        1,
        "event-intent",
        payload={
            "observation_ref": {
                "provider_id": reference.provider_id,
                "opaque_id": reference.opaque_id,
                "content_digest": reference.content_digest,
            }
        },
    )

    first = engine.process(observed)
    duplicate = engine.process(observed)

    assert duplicate == first
    with pytest.raises(QueryObservationUnavailable):
        registry(reference, None)
    assert [record.event_id for record in store.records(StreamId.from_scope(first.scope))] == [
        "event-start",
        "event-intent",
    ]


def test_normalizer_rejects_non_reference_inputs() -> None:
    registry = QueryObservationRegistry(provider_id="ctx-run")

    with pytest.raises(TypeError, match="ObservationReference"):
        registry("not-a-reference", None)  # type: ignore[arg-type]
