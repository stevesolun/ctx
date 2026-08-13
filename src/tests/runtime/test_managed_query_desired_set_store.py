from __future__ import annotations

import copy
import hashlib
import os
import pickle
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import ctx.runtime.managed_query_store as store_module
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.runtime.managed_query_store import (
    ManagedDesiredSetRecord,
    ManagedQueryStoreCapacityExceeded,
    ManagedQueryStoreConflict,
    ManagedQueryStoreCorruption,
    ManagedQueryStoreNotFound,
    open_managed_query_store,
)


KEY = b"d" * 32
MANIFEST_DIGEST = hashlib.sha256(b"desired-set-manifest").hexdigest()
ENVIRONMENT_DIGEST = hashlib.sha256(b"desired-set-environment").hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(*, session_id: str = "session-1", exposure_id: str = "exposure-1") -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id=session_id,
        exposure_id=exposure_id,
        host_context_id="host-neutral",
    )


def _query_events(*, scope: ScopeRef | None = None) -> tuple[EngineEvent, EngineEvent]:
    exact_scope = scope or _scope()
    common = {
        "scope": exact_scope,
        "occurred_at": "2026-08-03T12:00:00Z",
        "correlation_id": "plan-1",
        "engine_version": "ctx-engine-v1",
        "planner_version": "ctx-authenticated-net-benefit-planner-v3",
        "policy_version": "policy-v1",
        "host_descriptor_digest": _digest("host"),
        "catalog_snapshot_digest": ENVIRONMENT_DIGEST,
        "semantic_model_digest": _digest("semantic-model"),
        "semantic_index_digest": _digest("semantic-index"),
        "work_signature": _digest("work"),
        "random_seed": 0,
    }
    started = EngineEvent(
        event_id="event-start",
        kind="SessionStarted",
        expected_revision=0,
        payload={"host_level": "managing"},
        causation_id="host-start",
        **common,  # type: ignore[arg-type]
    )
    decision = EngineEvent(
        event_id="event-decision",
        kind="IntentObserved",
        expected_revision=1,
        payload={
            "observation_ref": {
                "provider_id": "ctx-managed-artifact-observation-v1",
                "opaque_id": f"manifest-{MANIFEST_DIGEST}",
                "content_digest": _digest("surrogate"),
            }
        },
        causation_id=started.event_id,
        **common,  # type: ignore[arg-type]
    )
    return started, decision


def _open_planned_query(
    path: Path,
    *,
    scope: ScopeRef | None = None,
):
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, decision = _query_events(scope=scope)
    query = store.register(
        logical_query_id=_digest(f"query:{decision.scope.session_id}"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=MANIFEST_DIGEST,
        planning_environment_digest=ENVIRONMENT_DIGEST,
    )
    query = store.mark_planned(
        query.query_ref,
        plan_id="plan-1",
        decision_digest=_digest("plan-decision"),
        journal_revision=2,
        journal_record_digest=_digest("plan-journal-record"),
    )
    return store, query


def _desired_event(
    *,
    scope: ScopeRef | None = None,
    expected_revision: int = 2,
    event_id: str = "event-desired-1",
    capability_ids: tuple[str, ...] = ("skill:testing",),
) -> EngineEvent:
    rows = [
        {
            "actionability": "load",
            "capability_id": capability_id,
            "install_descriptor_digest": None,
            "install_plan_digest": None,
            "kind": capability_id.split(":", 1)[0],
            "lease_id": f"lease-{index}",
            "source_digest": _digest(f"source:{capability_id}"),
        }
        for index, capability_id in enumerate(capability_ids)
    ]
    return EngineEvent(
        event_id=event_id,
        kind="ReassessmentRequested",
        scope=scope or _scope(),
        expected_revision=expected_revision,
        occurred_at="2026-08-03T12:01:00Z",
        payload={
            "desired_capabilities": rows,
            "owner_id": "owner-1",
            "policy_snapshot_digest": _digest("policy-snapshot"),
        },
        correlation_id="plan-1",
        causation_id="event-decision",
        engine_version="ctx-engine-v1",
        planner_version="ctx-authenticated-net-benefit-planner-v3",
        policy_version="policy-v1",
        host_descriptor_digest=_digest("host"),
        catalog_snapshot_digest=ENVIRONMENT_DIGEST,
        semantic_model_digest=_digest("semantic-model"),
        semantic_index_digest=_digest("semantic-index"),
        work_signature=_digest("work"),
        random_seed=0,
    )


def _reserve_process(arguments: tuple[str, str, int]) -> tuple[str, str]:
    path_text, choice_id, revision = arguments
    store = open_managed_query_store(path=Path(path_text), installation_hmac_key=KEY)
    query = store.load_by_scope_and_plan(_scope(), "plan-1")
    try:
        record = store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest(choice_id),
            capability_ids=("skill:testing",),
            event=_desired_event(
                expected_revision=revision,
                event_id=f"event-{choice_id}",
            ),
        )
    except ManagedQueryStoreConflict:
        return "conflict", choice_id
    return "reserved", record.desired_set_ref


def _commit_process(arguments: tuple[str, str]) -> tuple[str, int | None]:
    path_text, desired_set_ref = arguments
    store = open_managed_query_store(path=Path(path_text), installation_hmac_key=KEY)
    record = store.mark_desired_set_committed(
        desired_set_ref,
        journal_revision=3,
        journal_record_digest=_digest("desired-journal-record"),
        transition_digest=_digest("desired-transition"),
    )
    return record.desired_set_ref, record.journal_revision


def test_desired_set_reserve_load_commit_and_latest_are_exact_and_idempotent(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    event = _desired_event()

    reserved = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("choice-1"),
        capability_ids=("skill:testing",),
        event=event,
    )
    repeated = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("choice-1"),
        capability_ids=("skill:testing",),
        event=event,
    )

    assert type(reserved) is ManagedDesiredSetRecord
    assert repeated == reserved == store.load_desired_set(reserved.desired_set_ref)
    assert store.load_latest_desired_set(query.query_ref) == reserved
    assert store.load_pending_desired_set(_scope()) == reserved
    assert reserved.desired_set_ref.startswith("mds_")
    assert len(reserved.desired_set_ref) == 68
    assert reserved.query_ref == query.query_ref
    assert reserved.plan_id == "plan-1"
    assert reserved.decision_digest == _digest("plan-decision")
    assert reserved.capability_ids == ("skill:testing",)
    assert reserved.event.to_json().encode("utf-8") == event.to_json().encode("utf-8")
    assert reserved.committed is False
    assert str(tmp_path) not in repr(reserved)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(reserved)

    committed = store.mark_desired_set_committed(
        reserved.desired_set_ref,
        journal_revision=3,
        journal_record_digest=_digest("desired-journal-record"),
        transition_digest=_digest("desired-transition"),
    )
    repeated_commit = store.mark_desired_set_committed(
        reserved.desired_set_ref,
        journal_revision=3,
        journal_record_digest=_digest("desired-journal-record"),
        transition_digest=_digest("desired-transition"),
    )

    assert committed == repeated_commit == store.load_latest_desired_set(query.query_ref)
    assert committed.committed is True
    assert committed.journal_revision == 3
    assert committed.journal_record_digest == _digest("desired-journal-record")
    assert committed.transition_digest == _digest("desired-transition")
    with pytest.raises(ManagedQueryStoreNotFound):
        store.load_pending_desired_set(_scope())


def test_reservation_conflicts_on_substitution_revision_or_pending_stream(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    original = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("choice-1"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )

    with pytest.raises(ManagedQueryStoreConflict, match="choice identity"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("choice-1"),
            capability_ids=("skill:review",),
            event=_desired_event(capability_ids=("skill:review",)),
        )
    with pytest.raises(ManagedQueryStoreConflict, match="stream revision"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("choice-2"),
            capability_ids=("skill:testing",),
            event=_desired_event(event_id="event-desired-2"),
        )
    with pytest.raises(ManagedQueryStoreConflict, match="pending"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("choice-3"),
            capability_ids=(),
            event=_desired_event(
                expected_revision=3,
                event_id="event-desired-3",
                capability_ids=(),
            ),
        )

    assert store.load_desired_set(original.desired_set_ref) == original
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (1,)


def test_exact_reservation_retry_authenticates_the_bounded_store_scan(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, first_query = _open_planned_query(path)
    first_event = _desired_event()
    first = store.reserve_desired_set(
        query_ref=first_query.query_ref,
        logical_choice_id=_digest("choice-first"),
        capability_ids=("skill:testing",),
        event=first_event,
    )
    second_scope = _scope(session_id="session-2", exposure_id="exposure-2")
    store, second_query = _open_planned_query(path, scope=second_scope)
    second = store.reserve_desired_set(
        query_ref=second_query.query_ref,
        logical_choice_id=_digest("choice-second"),
        capability_ids=("skill:testing",),
        event=_desired_event(scope=second_scope, event_id="event-desired-second-stream"),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE managed_desired_sets SET row_hmac = ? WHERE desired_set_ref = ?",
            ("0" * 64, second.desired_set_ref),
        )

    with pytest.raises(ManagedQueryStoreCorruption, match="authentication"):
        store.reserve_desired_set(
            query_ref=first_query.query_ref,
            logical_choice_id=_digest("choice-first"),
            capability_ids=("skill:testing",),
            event=first_event,
        )
    assert first.desired_set_ref != second.desired_set_ref


def test_committed_choice_allows_a_later_sequential_reservation(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    first = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("choice-1"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )
    store.mark_desired_set_committed(
        first.desired_set_ref,
        journal_revision=3,
        journal_record_digest=_digest("record-1"),
        transition_digest=_digest("transition-1"),
    )

    later_event = _desired_event(
        expected_revision=3,
        event_id="event-desired-later",
        capability_ids=(),
    )
    later_event = replace(later_event, causation_id=first.event.event_id)
    later = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("choice-2"),
        capability_ids=(),
        event=later_event,
    )

    assert later.event.expected_revision == 3
    assert later.capability_ids == ()
    assert store.load_latest_desired_set(query.query_ref) == later
    assert store.load_pending_desired_set(_scope()) == later


def test_reservation_rejects_a_stale_unused_revision_after_a_later_commit(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    later = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("choice-later"),
        capability_ids=("skill:testing",),
        event=_desired_event(expected_revision=5, event_id="event-desired-revision-5"),
    )
    store.mark_desired_set_committed(
        later.desired_set_ref,
        journal_revision=6,
        journal_record_digest=_digest("record-revision-5"),
        transition_digest=_digest("transition-revision-5"),
    )

    with pytest.raises(ManagedQueryStoreConflict, match="latest committed"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("choice-stale"),
            capability_ids=(),
            event=replace(
                _desired_event(
                    expected_revision=4,
                    event_id="event-desired-stale-revision-4",
                    capability_ids=(),
                ),
                causation_id=later.event.event_id,
            ),
        )


@pytest.mark.parametrize(
    ("field_name", "substituted"),
    [
        ("engine_version", "ctx-engine-substituted"),
        ("planner_version", "ctx-planner-substituted"),
        ("policy_version", "policy-substituted"),
        ("host_descriptor_digest", _digest("host-substituted")),
        ("catalog_snapshot_digest", _digest("catalog-substituted")),
        ("semantic_model_digest", _digest("model-substituted")),
        ("semantic_index_digest", _digest("index-substituted")),
        ("work_signature", _digest("work-substituted")),
        ("random_seed", 7),
        ("causation_id", "event-substituted-cause"),
    ],
)
def test_desired_set_event_cannot_substitute_parent_replay_envelope(
    tmp_path: Path,
    field_name: str,
    substituted: object,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)

    with pytest.raises(ManagedQueryStoreConflict, match="envelope|causation"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest(f"substituted-{field_name}"),
            capability_ids=("skill:testing",),
            event=replace(_desired_event(), **{field_name: substituted}),  # type: ignore[arg-type]
        )


def test_desired_set_event_cannot_substitute_parent_privacy_label(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    event = _desired_event()

    with pytest.raises(ManagedQueryStoreConflict, match="envelope"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("substituted-privacy"),
            capability_ids=("skill:testing",),
            event=replace(event, privacy=replace(event.privacy, retention="session")),
        )


def test_later_desired_set_causation_must_name_previous_committed_choice(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    first = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("causation-first"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )
    store.mark_desired_set_committed(
        first.desired_set_ref,
        journal_revision=3,
        journal_record_digest=_digest("causation-record-1"),
        transition_digest=_digest("causation-transition-1"),
    )

    with pytest.raises(ManagedQueryStoreConflict, match="causation"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("causation-second"),
            capability_ids=(),
            event=_desired_event(
                expected_revision=3,
                event_id="event-desired-causation-second",
                capability_ids=(),
            ),
        )

    accepted = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("causation-second"),
        capability_ids=(),
        event=replace(
            _desired_event(
                expected_revision=3,
                event_id="event-desired-causation-second",
                capability_ids=(),
            ),
            causation_id=first.event.event_id,
        ),
    )
    assert accepted.event.causation_id == first.event.event_id


def test_first_choice_for_a_new_plan_resets_causation_to_its_parent_decision(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, first_query = _open_planned_query(path)
    first = store.reserve_desired_set(
        query_ref=first_query.query_ref,
        logical_choice_id=_digest("first-plan-choice"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )
    store.mark_desired_set_committed(
        first.desired_set_ref,
        journal_revision=3,
        journal_record_digest=_digest("first-plan-record"),
        transition_digest=_digest("first-plan-transition"),
    )

    started, decision = _query_events()
    development_started = replace(
        started,
        event_id="event-start-plan-2",
        correlation_id="plan-2",
    )
    development = replace(
        decision,
        event_id="event-development-plan-2",
        kind="DevelopmentObserved",
        expected_revision=3,
        correlation_id="plan-2",
        causation_id=first.event.event_id,
        work_signature=_digest("work-plan-2"),
    )
    second_query = store.register(
        logical_query_id=_digest("query:plan-2"),
        session_started=development_started,
        decision_event=development,
        artifact_manifest_digest=MANIFEST_DIGEST,
        planning_environment_digest=ENVIRONMENT_DIGEST,
    )
    second_query = store.mark_planned(
        second_query.query_ref,
        plan_id="plan-2",
        decision_digest=_digest("plan-2-decision"),
        journal_revision=4,
        journal_record_digest=_digest("plan-2-journal-record"),
    )
    second_event = replace(
        _desired_event(
            expected_revision=4,
            event_id="event-desired-plan-2",
            capability_ids=(),
        ),
        correlation_id="plan-2",
        causation_id=development.event_id,
        work_signature=development.work_signature,
    )

    second = store.reserve_desired_set(
        query_ref=second_query.query_ref,
        logical_choice_id=_digest("second-plan-choice"),
        capability_ids=(),
        event=second_event,
    )

    assert second.event.causation_id == development.event_id
    assert second.event.causation_id != first.event.event_id


@pytest.mark.parametrize(
    "defect",
    [
        "unplanned-parent",
        "wrong-kind",
        "wrong-scope",
        "wrong-plan",
        "duplicate-capability",
        "capability-mismatch",
        "too-many-capabilities",
        "raw-payload",
        "non-digest-choice-id",
    ],
)
def test_reservation_rejects_unbound_or_unsafe_desired_set_content(
    tmp_path: Path,
    defect: str,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    event = _desired_event()
    capability_ids: tuple[str, ...] = ("skill:testing",)
    query_ref = query.query_ref
    logical_choice_id = _digest("choice-unsafe")
    if defect == "unplanned-parent":
        started, decision = _query_events(scope=_scope(session_id="unplanned"))
        query_ref = store.register(
            logical_query_id=_digest("unplanned-query"),
            session_started=started,
            decision_event=decision,
            artifact_manifest_digest=MANIFEST_DIGEST,
            planning_environment_digest=ENVIRONMENT_DIGEST,
        ).query_ref
        event = _desired_event(scope=decision.scope)
    elif defect == "wrong-kind":
        event = replace(event, kind="DevelopmentObserved")
    elif defect == "wrong-scope":
        event = replace(event, scope=_scope(session_id="other-session"))
    elif defect == "wrong-plan":
        event = replace(event, correlation_id="other-plan")
    elif defect == "duplicate-capability":
        capability_ids = ("skill:testing", "skill:testing")
        event = _desired_event(capability_ids=capability_ids)
    elif defect == "capability-mismatch":
        capability_ids = ("skill:other",)
    elif defect == "too-many-capabilities":
        capability_ids = tuple(f"skill:item-{index}" for index in range(6))
        event = _desired_event(capability_ids=capability_ids)
    elif defect == "raw-payload":
        payload = event.to_dict()["payload"]
        assert isinstance(payload, dict)
        event = replace(event, payload={**payload, "prompt": "private raw prompt"})
    elif defect == "non-digest-choice-id":
        logical_choice_id = "choice-unsafe"

    with pytest.raises((TypeError, ValueError, ManagedQueryStoreConflict)) as captured:
        store.reserve_desired_set(
            query_ref=query_ref,
            logical_choice_id=logical_choice_id,
            capability_ids=capability_ids,
            event=event,
        )

    assert "private raw prompt" not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_desired_set_bytes_and_commit_are_authenticated_all_or_none(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    event = _desired_event()
    reserved = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("choice-persisted"),
        capability_ids=("skill:testing",),
        event=event,
    )

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT event_json, event_bytes, capability_ids_json, journal_revision, "
            "journal_record_digest, transition_digest, row_hmac FROM managed_desired_sets"
        ).fetchone()
    assert row is not None
    assert bytes(row[0]) == event.to_json().encode("utf-8")
    assert row[1] == len(event.to_json().encode("utf-8"))
    assert bytes(row[2]) == b'["skill:testing"]'
    assert row[3:6] == (None, None, None)
    assert len(row[6]) == 64
    assert KEY not in path.read_bytes()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE managed_desired_sets SET journal_revision = ? WHERE desired_set_ref = ?",
            (3, reserved.desired_set_ref),
        )
    with pytest.raises(ManagedQueryStoreCorruption, match="commit is partial"):
        store.load_desired_set(reserved.desired_set_ref)


def test_desired_set_exact_retries_and_commit_are_cross_process_safe(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    _open_planned_query(path)

    with ProcessPoolExecutor(max_workers=4) as pool:
        outcomes = tuple(
            pool.map(
                _reserve_process,
                ((str(path), "same-choice", 2),) * 12,
            )
        )
    assert {status for status, _value in outcomes} == {"reserved"}
    references = {value for _status, value in outcomes}
    assert len(references) == 1
    desired_set_ref = next(iter(references))

    with ProcessPoolExecutor(max_workers=4) as pool:
        commits = tuple(
            pool.map(
                _commit_process,
                ((str(path), desired_set_ref),) * 12,
            )
        )
    assert set(commits) == {(desired_set_ref, 3)}
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (1,)


def test_competing_desired_set_stream_revision_is_cross_process_exclusive(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    _open_planned_query(path)

    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(
            pool.map(
                _reserve_process,
                (
                    (str(path), "left-choice", 2),
                    (str(path), "right-choice", 2),
                ),
            )
        )

    assert sorted(status for status, _value in outcomes) == ["conflict", "reserved"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (1,)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX hard-exit semantics")
def test_committed_desired_set_survives_process_hard_exit(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - parent performs assertions
        os.close(read_fd)
        try:
            child_store = open_managed_query_store(path=path, installation_hmac_key=KEY)
            reserved = child_store.reserve_desired_set(
                query_ref=query.query_ref,
                logical_choice_id=_digest("hard-exit-choice"),
                capability_ids=("skill:testing",),
                event=_desired_event(),
            )
            child_store.mark_desired_set_committed(
                reserved.desired_set_ref,
                journal_revision=3,
                journal_record_digest=_digest("hard-exit-record"),
                transition_digest=_digest("hard-exit-transition"),
            )
            os.write(write_fd, reserved.desired_set_ref.encode("ascii"))
            os._exit(0)
        except BaseException as exc:
            os.write(write_fd, f"{type(exc).__name__}:{exc}".encode("utf-8")[:2048])
            os._exit(1)

    os.close(write_fd)
    result = os.read(read_fd, 2048)
    os.close(read_fd)
    waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0, result.decode("utf-8")
    recovered = store.load_desired_set(result.decode("ascii"))
    assert recovered.committed is True
    assert recovered.journal_record_digest == _digest("hard-exit-record")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX hard-exit semantics")
def test_pending_desired_set_survives_hard_exit_and_blocks_competitors(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - parent performs assertions
        try:
            child_store = open_managed_query_store(path=path, installation_hmac_key=KEY)
            child_store.reserve_desired_set(
                query_ref=query.query_ref,
                logical_choice_id=_digest("pending-hard-exit"),
                capability_ids=("skill:testing",),
                event=_desired_event(),
            )
            os._exit(0)
        except BaseException:
            os._exit(1)

    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    pending = store.load_pending_desired_set(_scope())
    assert pending.logical_choice_id == _digest("pending-hard-exit")
    with pytest.raises(ManagedQueryStoreConflict, match="pending"):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("after-hard-exit"),
            capability_ids=(),
            event=_desired_event(
                expected_revision=3,
                event_id="event-after-hard-exit",
                capability_ids=(),
            ),
        )


@pytest.mark.parametrize("corruption", ["event", "choice-ids", "row-hmac"])
def test_desired_set_load_rejects_authenticated_row_corruption(
    tmp_path: Path,
    corruption: str,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    reserved = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("corruption-choice"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )
    with sqlite3.connect(path) as connection:
        if corruption == "event":
            connection.execute(
                "UPDATE managed_desired_sets SET event_json = ? WHERE desired_set_ref = ?",
                (b'{"prompt":"raw"}', reserved.desired_set_ref),
            )
        elif corruption == "choice-ids":
            connection.execute(
                "UPDATE managed_desired_sets SET capability_ids_json = ? WHERE desired_set_ref = ?",
                (b'["skill:other"]', reserved.desired_set_ref),
            )
        else:
            connection.execute(
                "UPDATE managed_desired_sets SET row_hmac = ? WHERE desired_set_ref = ?",
                ("0" * 64, reserved.desired_set_ref),
            )

    with pytest.raises(ManagedQueryStoreCorruption):
        store.load_desired_set(reserved.desired_set_ref)


def test_desired_set_load_rejects_an_orphaned_authenticated_child_row(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    reserved = store.reserve_desired_set(
        query_ref=query.query_ref,
        logical_choice_id=_digest("orphan-choice"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM managed_queries WHERE query_ref = ?",
            (query.query_ref,),
        )

    with pytest.raises(ManagedQueryStoreCorruption, match="parent query"):
        store.load_desired_set(reserved.desired_set_ref)


def test_commit_authenticates_all_sibling_desired_rows_before_mutation(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, first_query = _open_planned_query(path)
    first = store.reserve_desired_set(
        query_ref=first_query.query_ref,
        logical_choice_id=_digest("commit-first-stream"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )
    second_scope = _scope(session_id="commit-session-2", exposure_id="commit-exposure-2")
    store, second_query = _open_planned_query(path, scope=second_scope)
    second = store.reserve_desired_set(
        query_ref=second_query.query_ref,
        logical_choice_id=_digest("commit-second-stream"),
        capability_ids=("skill:testing",),
        event=_desired_event(
            scope=second_scope,
            event_id="event-desired-commit-second-stream",
        ),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE managed_desired_sets SET row_hmac = ? WHERE desired_set_ref = ?",
            ("0" * 64, second.desired_set_ref),
        )

    with pytest.raises(ManagedQueryStoreCorruption, match="authentication"):
        store.mark_desired_set_committed(
            first.desired_set_ref,
            journal_revision=3,
            journal_record_digest=_digest("commit-first-record"),
            transition_digest=_digest("commit-first-transition"),
        )

    with sqlite3.connect(path) as connection:
        target = connection.execute(
            "SELECT journal_revision, journal_record_digest, transition_digest "
            "FROM managed_desired_sets WHERE desired_set_ref = ?",
            (first.desired_set_ref,),
        ).fetchone()
    assert target == (None, None, None)


def test_query_mutators_authenticate_all_desired_rows_before_mutation(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, first_query = _open_planned_query(path)
    reserved = store.reserve_desired_set(
        query_ref=first_query.query_ref,
        logical_choice_id=_digest("query-mutator-sibling"),
        capability_ids=("skill:testing",),
        event=_desired_event(),
    )
    second_scope = _scope(session_id="query-mutator-2", exposure_id="query-mutator-2")
    second_started, second_decision = _query_events(scope=second_scope)
    second_query = store.register(
        logical_query_id=_digest("query-mutator-unplanned"),
        session_started=second_started,
        decision_event=second_decision,
        artifact_manifest_digest=MANIFEST_DIGEST,
        planning_environment_digest=ENVIRONMENT_DIGEST,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE managed_desired_sets SET row_hmac = ? WHERE desired_set_ref = ?",
            ("0" * 64, reserved.desired_set_ref),
        )

    with pytest.raises(ManagedQueryStoreCorruption, match="authentication"):
        store.mark_planned(
            second_query.query_ref,
            plan_id="plan-1",
            decision_digest=_digest("query-mutator-decision"),
            journal_revision=2,
            journal_record_digest=_digest("query-mutator-record"),
        )

    third_scope = _scope(session_id="query-mutator-3", exposure_id="query-mutator-3")
    third_started, third_decision = _query_events(scope=third_scope)
    with pytest.raises(ManagedQueryStoreCorruption, match="authentication"):
        store.register(
            logical_query_id=_digest("query-mutator-not-inserted"),
            session_started=third_started,
            decision_event=third_decision,
            artifact_manifest_digest=MANIFEST_DIGEST,
            planning_environment_digest=ENVIRONMENT_DIGEST,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT plan_id FROM managed_queries WHERE query_ref = ?",
            (second_query.query_ref,),
        ).fetchone() == (None,)
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (2,)


def test_desired_set_capacity_failure_rolls_back_without_partial_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    monkeypatch.setattr(store_module, "_MAX_DESIRED_SET_ROWS", 0)

    with pytest.raises(ManagedQueryStoreCapacityExceeded):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("over-capacity"),
            capability_ids=("skill:testing",),
            event=_desired_event(),
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (0,)


def test_desired_set_operation_fails_closed_when_database_exceeds_size_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store, query = _open_planned_query(path)
    original_size = path.stat().st_size
    monkeypatch.setattr(store_module, "_MAX_DATABASE_BYTES", original_size - 1)

    with pytest.raises(ManagedQueryStoreCapacityExceeded):
        store.reserve_desired_set(
            query_ref=query.query_ref,
            logical_choice_id=_digest("over-size-bound"),
            capability_ids=("skill:testing",),
            event=_desired_event(),
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone() == (0,)
    assert path.stat().st_size == original_size


def test_schema_v1_is_rejected_without_mutating_the_database(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    _open_planned_query(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_managed_desired_sets_query_revision")
        connection.execute("DROP INDEX idx_managed_desired_sets_stream_state")
        connection.execute("DROP TABLE managed_desired_sets")
        connection.execute("PRAGMA user_version = 1")
    before = path.read_bytes()

    with pytest.raises(ManagedQueryStoreCorruption, match="schema version"):
        open_managed_query_store(path=path, installation_hmac_key=KEY)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'managed_desired_sets'"
        ).fetchone() == (0,)
