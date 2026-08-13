from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from ctx.engine.engine import (
    CtxEngine,
    ReducerRegistry,
    ReplayDivergenceError,
    SnapshotContentionError,
    UnsupportedReducerVersionError,
)
from ctx.engine.protocol import EngineEvent, ScopeRef, Transition
from ctx.engine.reducer import (
    INSTALLATION_REDUCER_VERSION,
    PLANNING_REDUCER_VERSION,
    reduce,
    reduce_replay_v1,
)
from ctx.engine.replay import (
    DEFAULT_REDUCER_VERSION,
    DefaultReplayInputFactory,
    ObservationReference,
    PreflightReplayInput,
    ReplayInput,
    ReplayPrivacyError,
    ReplayValidationError,
    StructuredSurrogate,
)
from ctx.engine.state import EngineState
from ctx.engine.store import (
    CommitResult,
    JournalRecord,
    RevisionConflict,
    SQLiteEngineStore,
    StoredHead,
    StreamId,
)


NOW = "2026-08-01T12:00:00Z"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _scope(*, session_id: str = "session-1") -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id=session_id,
        exposure_id="exposure-1",
        host_context_id="host-1",
    )


def _event(
    kind: str,
    *,
    revision: int,
    event_id: str,
    scope: ScopeRef | None = None,
    payload: dict[str, object] | None = None,
) -> EngineEvent:
    return EngineEvent(
        event_id=event_id,
        kind=kind,
        scope=scope or _scope(),
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


class _LoggingFactory:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.delegate = DefaultReplayInputFactory()
        self.prepare_calls = 0

    def preflight(self, event: EngineEvent) -> PreflightReplayInput:
        self.log.append("preflight")
        return self.delegate.preflight(event)

    def prepare(
        self,
        preflight: PreflightReplayInput,
        state: EngineState | None,
        *,
        decision_surrogate: None = None,
    ) -> ReplayInput:
        self.log.append("prepare")
        self.prepare_calls += 1
        return self.delegate.prepare(
            preflight,
            state,
            decision_surrogate=decision_surrogate,
        )


class _LoggingStore:
    def __init__(self, delegate: SQLiteEngineStore, log: list[str]) -> None:
        self.delegate = delegate
        self.log = log

    def load_head(self, stream_id: StreamId) -> StoredHead:
        self.log.append("load_head")
        return self.delegate.load_head(stream_id)

    def cached_transition(
        self,
        stream_id: StreamId,
        event_id: str,
        event_content_digest: str,
    ) -> Transition | None:
        self.log.append("cached")
        return self.delegate.cached_transition(stream_id, event_id, event_content_digest)

    def records(
        self,
        stream_id: StreamId,
        *,
        after_revision: int = 0,
    ) -> Iterator[JournalRecord]:
        self.log.append("records")
        return self.delegate.records(stream_id, after_revision=after_revision)

    def repair_projection(
        self,
        stream_id: StreamId,
        *,
        at_revision: int,
        state_json: str,
        record_digest: str,
    ) -> bool:
        self.log.append("repair")
        return self.delegate.repair_projection(
            stream_id,
            at_revision=at_revision,
            state_json=state_json,
            record_digest=record_digest,
        )

    def commit(self, *, expected_revision: int, record: JournalRecord) -> CommitResult:
        self.log.append("commit")
        return self.delegate.commit(expected_revision=expected_revision, record=record)


def test_empty_snapshot_and_genesis_process_use_the_real_journal(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    engine = CtxEngine(store=store)

    empty = engine.snapshot(_scope())
    transition = engine.process(_event("SessionStarted", revision=0, event_id="event-1"))
    current = engine.snapshot(_scope())

    assert empty.revision == 0
    assert empty.state is None
    assert empty.record_digest is None
    assert transition.from_revision == 0
    assert transition.to_revision == 1
    assert current.revision == 1
    assert current.state is not None
    assert current.state.revision == 1
    assert [record.event_id for record in store.records(current.stream_id)] == ["event-1"]


def test_default_reducer_registry_is_exact_and_immutable() -> None:
    registry = ReducerRegistry.default()

    assert callable(registry.resolve(DEFAULT_REDUCER_VERSION))
    assert callable(registry.resolve(PLANNING_REDUCER_VERSION))
    assert callable(registry.resolve(INSTALLATION_REDUCER_VERSION))
    with pytest.raises(UnsupportedReducerVersionError):
        registry.resolve("ctx-reducer-v999")
    with pytest.raises(TypeError):
        registry.versions[DEFAULT_REDUCER_VERSION] = lambda state, event: (state, event)  # type: ignore[index,assignment,return-value]


def test_unsafe_preflight_fails_before_any_store_access(tmp_path: Path) -> None:
    log: list[str] = []
    store = _LoggingStore(
        SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3"),
        log,
    )
    factory = _LoggingFactory(log)
    engine = CtxEngine(store=store, replay_factory=factory)
    unsafe = _event(
        "DevelopmentObserved",
        revision=0,
        event_id="event-unsafe",
        payload={"prompt": "raw-secret", "path": "/private/repo/app.py"},
    )

    with pytest.raises(ReplayPrivacyError):
        engine.process(unsafe)

    assert log == ["preflight"]
    assert factory.prepare_calls == 0


def test_cached_duplicate_stops_before_snapshot_prepare_and_reducer(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(path)
    event = _event("SessionStarted", revision=0, event_id="event-1")
    original = CtxEngine(store=store).process(event)
    log: list[str] = []
    factory = _LoggingFactory(log)
    reducer_calls = 0

    def counted_reducer(
        state: EngineState | None,
        replay: ReplayInput,
    ) -> tuple[EngineState, Transition]:
        nonlocal reducer_calls
        reducer_calls += 1
        return ReducerRegistry.default().resolve(DEFAULT_REDUCER_VERSION)(state, replay)

    engine = CtxEngine(
        store=_LoggingStore(store, log),
        replay_factory=factory,
        reducers=ReducerRegistry({DEFAULT_REDUCER_VERSION: counted_reducer}),
    )

    duplicate = engine.process(event)

    assert duplicate.to_json() == original.to_json()
    assert log == ["preflight", "cached"]
    assert factory.prepare_calls == 0
    assert reducer_calls == 0


def test_stale_revision_rechecks_cache_before_prepare(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    CtxEngine(store=store).process(_event("SessionStarted", revision=0, event_id="event-1"))
    log: list[str] = []
    factory = _LoggingFactory(log)
    engine = CtxEngine(store=_LoggingStore(store, log), replay_factory=factory)

    with pytest.raises(RevisionConflict) as raised:
        engine.process(_event("TurnStarting", revision=0, event_id="event-stale"))

    assert raised.value.actual == 1
    assert log == ["preflight", "cached", "load_head", "cached"]
    assert factory.prepare_calls == 0


def test_second_cache_check_returns_concurrent_identical_commit(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    event = _event("SessionStarted", revision=0, event_id="event-race")
    log: list[str] = []
    factory = _LoggingFactory(log)

    class RacingStore(_LoggingStore):
        calls = 0

        def cached_transition(
            self,
            stream_id: StreamId,
            event_id: str,
            event_content_digest: str,
        ) -> Transition | None:
            self.calls += 1
            self.log.append("cached")
            if self.calls == 1:
                CtxEngine(store=self.delegate).process(event)
                return None
            return self.delegate.cached_transition(
                stream_id,
                event_id,
                event_content_digest,
            )

    transition = CtxEngine(
        store=RacingStore(store, log),
        replay_factory=factory,
    ).process(event)

    assert transition.to_revision == 1
    assert log == ["preflight", "cached", "load_head", "cached"]
    assert factory.prepare_calls == 0


def _journal_record(
    event: EngineEvent,
    *,
    reducer_version: str = DEFAULT_REDUCER_VERSION,
    result_state: EngineState | None = None,
) -> JournalRecord:
    factory = DefaultReplayInputFactory(reducer_version=reducer_version)
    replay = factory.prepare(factory.preflight(event), None)
    reduced, transition = reduce(None, replay.reducer_event)
    return JournalRecord(
        stream_id=StreamId.from_scope(event.scope),
        revision=1,
        event_id=event.event_id,
        event_content_digest=event.content_digest,
        replay_json=replay.to_json(),
        transition_json=transition.to_json(),
        result_state_json=(result_state or reduced).to_json(),
        privacy_classification=event.privacy.classification,
        retention_class=event.privacy.retention,
        reducer_version=reducer_version,
    )


def test_valid_snapshot_semantically_replays_every_record(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    writer = CtxEngine(store=store)
    writer.process(_event("SessionStarted", revision=0, event_id="event-1"))
    writer.process(_event("TurnStarting", revision=1, event_id="event-2"))
    calls = 0

    def counted_reducer(
        state: EngineState | None,
        replay: ReplayInput,
    ) -> tuple[EngineState, Transition]:
        nonlocal calls
        calls += 1
        return reduce_replay_v1(state, replay)

    snapshot = CtxEngine(
        store=store,
        reducers=ReducerRegistry({DEFAULT_REDUCER_VERSION: counted_reducer}),
    ).snapshot(_scope())

    assert snapshot.revision == 2
    assert calls == 2


def test_semantically_forged_journal_state_is_replay_divergence(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    event = _event("SessionStarted", revision=0, event_id="event-1")
    forged_state = EngineState(
        revision=1,
        scope=event.scope,
        host_level="activating",
        host_descriptor_digest=event.host_descriptor_digest,
    )
    store.commit(
        expected_revision=0,
        record=_journal_record(event, result_state=forged_state),
    )

    with pytest.raises(ReplayDivergenceError) as raised:
        CtxEngine(store=store).snapshot(event.scope)

    assert raised.value.component == "state"
    assert "activating" not in str(raised.value)


def test_unknown_historical_reducer_has_no_fallback(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    event = _event("SessionStarted", revision=0, event_id="event-1")
    store.commit(
        expected_revision=0,
        record=_journal_record(event, reducer_version="ctx-reducer-v999"),
    )

    with pytest.raises(UnsupportedReducerVersionError) as raised:
        CtxEngine(store=store).snapshot(event.scope)

    assert raised.value.version == "ctx-reducer-v999"
    assert "ctx-reducer-v999" not in str(raised.value)


def test_snapshot_repairs_missing_projection_from_full_replay(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(path)
    event = _event("SessionStarted", revision=0, event_id="event-1")
    CtxEngine(store=store).process(event)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM engine_streams WHERE stream_key = ?",
            (StreamId.from_scope(event.scope).key,),
        )

    repaired = CtxEngine(store=store).snapshot(event.scope)

    assert repaired.revision == 1
    assert repaired.projection_repaired is True
    assert store.load_head(repaired.stream_id).projection_valid is True


def test_snapshot_rejects_projection_without_authoritative_journal(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(path)
    event = _event("SessionStarted", revision=0, event_id="event-1")
    CtxEngine(store=store).process(event)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM engine_journal WHERE stream_key = ?",
            (StreamId.from_scope(event.scope).key,),
        )

    with pytest.raises(ReplayDivergenceError) as raised:
        CtxEngine(store=store).snapshot(event.scope)

    assert raised.value.revision == 0
    assert raised.value.component == "projection"


def test_projection_repair_race_reloads_newer_journal_head(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    first = _event("SessionStarted", revision=0, event_id="event-1")
    CtxEngine(store=store).process(first)
    stream_id = StreamId.from_scope(first.scope)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM engine_streams WHERE stream_key = ?", (stream_id.key,))

    class RepairRaceStore(_LoggingStore):
        raced = False

        def repair_projection(
            self,
            stream_id: StreamId,
            *,
            at_revision: int,
            state_json: str,
            record_digest: str,
        ) -> bool:
            if not self.raced:
                self.raced = True
                CtxEngine(store=self.delegate).process(
                    _event("TurnStarting", revision=1, event_id="event-2")
                )
            return self.delegate.repair_projection(
                stream_id,
                at_revision=at_revision,
                state_json=state_json,
                record_digest=record_digest,
            )

    snapshot = CtxEngine(store=RepairRaceStore(store, [])).snapshot(first.scope)

    assert snapshot.revision == 2
    assert snapshot.projection_repaired is False


def test_snapshot_head_churn_is_bounded(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    event = _event("SessionStarted", revision=0, event_id="event-1")
    CtxEngine(store=store).process(event)

    class ChurningStore(_LoggingStore):
        loads = 0

        def load_head(self, stream_id: StreamId) -> StoredHead:
            self.loads += 1
            head = self.delegate.load_head(stream_id)
            if self.loads % 2 == 0:
                return replace(head, record_digest="0" * 64)
            return head

    racing = ChurningStore(store, [])
    with pytest.raises(SnapshotContentionError):
        CtxEngine(store=racing).snapshot(event.scope)

    assert racing.loads == 6


def test_normalizer_failure_is_masked_and_does_not_advance_journal(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    CtxEngine(store=store).process(_event("SessionStarted", revision=0, event_id="event-1"))
    secret = "raw-normalizer-secret"

    def failing_normalizer(
        _: ObservationReference,
        __: EngineState | None,
    ) -> StructuredSurrogate:
        raise RuntimeError(secret)

    factory = DefaultReplayInputFactory(observation_normalizer=failing_normalizer)
    observed = _event(
        "DevelopmentObserved",
        revision=1,
        event_id="event-2",
        payload={
            "observation_ref": {
                "provider_id": "host-buffer",
                "opaque_id": "observation-2",
                "content_digest": _digest(secret),
            }
        },
    )

    with pytest.raises(ReplayValidationError) as raised:
        CtxEngine(store=store, replay_factory=factory).process(observed)

    assert secret not in str(raised.value)
    assert store.load_head(StreamId.from_scope(observed.scope)).revision == 1
    assert [record.event_id for record in store.records(StreamId.from_scope(observed.scope))] == [
        "event-1"
    ]


def test_commit_failure_does_not_return_or_persist_reducer_output(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    log: list[str] = []

    class FailingCommitStore(_LoggingStore):
        def commit(
            self,
            *,
            expected_revision: int,
            record: JournalRecord,
        ) -> CommitResult:
            self.log.append("commit")
            raise RuntimeError("durable commit failed")

    event = _event("SessionStarted", revision=0, event_id="event-1")
    with pytest.raises(RuntimeError, match="durable commit failed"):
        CtxEngine(store=FailingCommitStore(store, log)).process(event)

    assert log[-1] == "commit"
    assert store.load_head(StreamId.from_scope(event.scope)).revision == 0
    assert tuple(store.records(StreamId.from_scope(event.scope))) == ()


def test_commit_cas_conflict_never_overwrites_concurrent_winner(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    CtxEngine(store=store).process(_event("SessionStarted", revision=0, event_id="event-1"))
    winner = _event("TurnStarting", revision=1, event_id="event-winner")

    class CommitRaceStore(_LoggingStore):
        raced = False

        def commit(
            self,
            *,
            expected_revision: int,
            record: JournalRecord,
        ) -> CommitResult:
            if not self.raced:
                self.raced = True
                CtxEngine(store=self.delegate).process(winner)
            return self.delegate.commit(expected_revision=expected_revision, record=record)

    loser = _event("TurnStarting", revision=1, event_id="event-loser")
    with pytest.raises(RevisionConflict) as raised:
        CtxEngine(store=CommitRaceStore(store, [])).process(loser)

    assert raised.value.actual == 2
    records = tuple(store.records(StreamId.from_scope(loser.scope)))
    assert [record.event_id for record in records] == ["event-1", "event-winner"]


def test_observation_raw_bytes_and_opaque_handle_are_never_persisted(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(path)
    CtxEngine(store=store).process(_event("SessionStarted", revision=0, event_id="event-1"))
    raw_secret = "private prompt and source diff sentinel"
    opaque_id = "observation-handle-secret"

    def normalizer(
        reference: ObservationReference,
        _: EngineState | None,
    ) -> StructuredSurrogate:
        assert reference.opaque_id == opaque_id
        return StructuredSurrogate.create(
            schema_id="ctx.observation.opaque-ref",
            schema_version=1,
            value={
                "provider_id": reference.provider_id,
                "content_digest": reference.content_digest,
            },
        )

    observed = _event(
        "DevelopmentObserved",
        revision=1,
        event_id="event-2",
        payload={
            "observation_ref": {
                "provider_id": "host-buffer",
                "opaque_id": opaque_id,
                "content_digest": _digest(raw_secret),
            }
        },
    )
    CtxEngine(
        store=store,
        replay_factory=DefaultReplayInputFactory(observation_normalizer=normalizer),
    ).process(observed)

    persisted = b"".join(
        candidate.read_bytes()
        for candidate in path.parent.iterdir()
        if candidate.name.startswith(path.name)
    )
    assert raw_secret.encode() not in persisted
    assert opaque_id.encode() not in persisted


def test_prepared_input_cannot_mutate_the_preflight_reducer_event(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")

    class MutatingFactory(DefaultReplayInputFactory):
        def prepare(
            self,
            preflight: PreflightReplayInput,
            state: EngineState | None,
            *,
            decision_surrogate: StructuredSurrogate | None = None,
        ) -> ReplayInput:
            replay = super().prepare(
                preflight,
                state,
                decision_surrogate=decision_surrogate,
            )
            return replace(
                replay,
                reducer_event=replace(
                    replay.reducer_event,
                    payload={"host_level": "activating"},
                ),
            )

    event = _event(
        "SessionStarted",
        revision=0,
        event_id="event-1",
        payload={"host_level": "query-only"},
    )
    with pytest.raises(ReplayDivergenceError) as raised:
        CtxEngine(store=store, replay_factory=MutatingFactory()).process(event)

    assert raised.value.component == "prepared-input"
    assert store.load_head(StreamId.from_scope(event.scope)).revision == 0


def test_forged_commit_result_is_not_returned(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")

    class ForgingStore(_LoggingStore):
        def commit(
            self,
            *,
            expected_revision: int,
            record: JournalRecord,
        ) -> CommitResult:
            result = self.delegate.commit(
                expected_revision=expected_revision,
                record=record,
            )
            forged = replace(result.transition, diagnostics=({"code": "forged"},))
            return replace(result, transition=forged)

    event = _event("SessionStarted", revision=0, event_id="event-1")
    with pytest.raises(ReplayDivergenceError) as raised:
        CtxEngine(store=ForgingStore(store, [])).process(event)

    assert raised.value.component == "commit-result"
    assert store.load_head(StreamId.from_scope(event.scope)).revision == 1


def test_forged_commit_record_metadata_is_not_accepted(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")

    class ForgingStore(_LoggingStore):
        def commit(
            self,
            *,
            expected_revision: int,
            record: JournalRecord,
        ) -> CommitResult:
            result = self.delegate.commit(
                expected_revision=expected_revision,
                record=record,
            )
            forged_record = replace(result.record, privacy_classification="public")
            return replace(result, revision=result.revision + 1, record=forged_record)

    event = _event("SessionStarted", revision=0, event_id="event-1")
    with pytest.raises(ReplayDivergenceError) as raised:
        CtxEngine(store=ForgingStore(store, [])).process(event)

    assert raised.value.component == "commit-result"
