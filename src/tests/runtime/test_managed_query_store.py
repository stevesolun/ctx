from __future__ import annotations

import copy
import hashlib
import inspect
import os
import pickle
import shutil
import sqlite3
import stat
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

import ctx.runtime.managed_query_store as managed_query_store_module
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.runtime.managed_query_store import (
    ManagedQueryStoreCapacityExceeded,
    ManagedQueryStoreConflict,
    ManagedQueryStoreCorruption,
    ManagedQueryStoreError,
    ManagedQueryStoreNotFound,
    open_managed_query_store,
)


KEY = b"k" * 32
ARTIFACT_MANIFEST_DIGEST = hashlib.sha256(b"artifact-manifest").hexdigest()
PLANNING_ENVIRONMENT_DIGEST = hashlib.sha256(b"planning-environment").hexdigest()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scope(*, exposure_id: str = "exposure-1") -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id=exposure_id,
        host_context_id="host-neutral",
    )


def _events(
    *,
    kind: str = "IntentObserved",
    scope: ScopeRef | None = None,
) -> tuple[EngineEvent, EngineEvent]:
    exact_scope = scope or _scope()
    common = {
        "scope": exact_scope,
        "occurred_at": "2026-08-03T12:00:00Z",
        "correlation_id": "plan-1",
        "engine_version": "ctx-engine-v1",
        "planner_version": "ctx-authenticated-net-benefit-planner-v3",
        "policy_version": "policy-v1",
        "host_descriptor_digest": _digest("host"),
        "catalog_snapshot_digest": PLANNING_ENVIRONMENT_DIGEST,
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
        kind=kind,
        expected_revision=1,
        payload={
            "observation_ref": {
                "provider_id": "ctx-managed-artifact-observation-v1",
                "opaque_id": f"manifest-{ARTIFACT_MANIFEST_DIGEST}",
                "content_digest": _digest("surrogate"),
            }
        },
        causation_id=started.event_id,
        **common,  # type: ignore[arg-type]
    )
    return started, decision


def _process_register(path_text: str) -> str:
    store = open_managed_query_store(
        path=Path(path_text),
        installation_hmac_key=KEY,
    )
    started, decision = _events()
    return store.register(
        logical_query_id=_digest("cross-process-query"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    ).query_ref


def _process_competing_register(arguments: tuple[str, str]) -> tuple[str, str]:
    path_text, contender = arguments
    store = open_managed_query_store(
        path=Path(path_text),
        installation_hmac_key=KEY,
    )
    started, decision = _events()
    started = replace(
        started,
        event_id=f"event-start-{contender}",
        causation_id=f"cause-start-{contender}",
    )
    decision = replace(
        decision,
        event_id=f"event-decision-{contender}",
        causation_id=started.event_id,
    )
    try:
        record = store.register(
            logical_query_id=_digest(f"competing-query-{contender}"),
            session_started=started,
            decision_event=decision,
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
        )
    except ManagedQueryStoreConflict:
        return "conflict", contender
    return "registered", record.query_ref


def _process_mark(arguments: tuple[str, str]) -> tuple[str, int | None]:
    path_text, query_ref = arguments
    store = open_managed_query_store(
        path=Path(path_text),
        installation_hmac_key=KEY,
    )
    record = store.mark_planned(
        query_ref,
        plan_id="plan-1",
        decision_digest=_digest("cross-process-decision"),
        journal_revision=2,
        journal_record_digest=_digest("cross-process-journal"),
    )
    return record.query_ref, record.journal_revision


def test_register_load_and_mark_planned_are_exact_and_idempotent(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "managed-query.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, decision = _events()
    logical_query_id = _digest("logical-query")

    registered = store.register(
        logical_query_id=logical_query_id,
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )
    repeated = store.register(
        logical_query_id=logical_query_id,
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )

    assert repeated == registered == store.load(registered.query_ref)
    assert registered.query_ref.startswith("mqr_")
    assert len(registered.query_ref) == 68
    assert registered.planned is False
    assert str(tmp_path) not in repr(registered)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        try:
            operation(registered)
        except TypeError:
            pass
        else:  # pragma: no cover - makes the security contract explicit
            raise AssertionError("managed query records must not be transferable")

    planned = store.mark_planned(
        registered.query_ref,
        plan_id="plan-1",
        decision_digest=_digest("decision"),
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
    )
    repeated_plan = store.mark_planned(
        registered.query_ref,
        plan_id="plan-1",
        decision_digest=_digest("decision"),
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
    )

    assert planned == repeated_plan == store.load(registered.query_ref)
    assert planned.planned is True
    assert store.load_by_scope_and_plan(_scope(), "plan-1") == planned
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "defect",
    [
        "scope",
        "causation",
        "raw-prompt",
        "observation-binding",
        "planning-environment",
        "path-token",
        "unsupported-event",
    ],
)
def test_registration_rejects_unsafe_or_unbound_event_content(
    tmp_path: Path,
    defect: str,
) -> None:
    store = open_managed_query_store(
        path=(tmp_path / "private" / "queries.sqlite3").absolute(),
        installation_hmac_key=KEY,
    )
    started, decision = _events()
    environment = PLANNING_ENVIRONMENT_DIGEST
    if defect == "scope":
        decision = replace(decision, scope=_scope(exposure_id="other-exposure"))
    elif defect == "causation":
        decision = replace(decision, causation_id="another-event")
    elif defect == "raw-prompt":
        payload = decision.to_dict()["payload"]
        assert isinstance(payload, dict)
        decision = replace(decision, payload={**payload, "prompt": "raw secret prompt"})
    elif defect == "observation-binding":
        decision = replace(
            decision,
            payload={
                "observation_ref": {
                    "provider_id": "ctx-managed-artifact-observation-v1",
                    "opaque_id": f"manifest-{_digest('other-manifest')}",
                    "content_digest": _digest("surrogate"),
                }
            },
        )
    elif defect == "planning-environment":
        environment = _digest("other-environment")
    elif defect == "path-token":
        unsafe = replace(_scope(), repository_id="/private/repository")
        started = replace(started, scope=unsafe)
        decision = replace(decision, scope=unsafe)
    elif defect == "unsupported-event":
        decision = replace(decision, kind="SessionEnded")

    with pytest.raises((TypeError, ValueError)) as captured:
        store.register(
            logical_query_id=_digest("unsafe-logical-query"),
            session_started=started,
            decision_event=decision,
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=environment,
        )

    assert "raw secret prompt" not in str(captured.value)
    assert str(tmp_path) not in str(captured.value)


def test_logical_registration_and_completion_substitutions_fail_closed(
    tmp_path: Path,
) -> None:
    store = open_managed_query_store(
        path=(tmp_path / "private" / "queries.sqlite3").absolute(),
        installation_hmac_key=KEY,
    )
    started, decision = _events()
    logical_query_id = _digest("logical-query")
    first = store.register(
        logical_query_id=logical_query_id,
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )
    other_started, other_decision = _events(scope=_scope(exposure_id="exposure-2"))
    with pytest.raises(ManagedQueryStoreConflict, match="logical query identity"):
        store.register(
            logical_query_id=logical_query_id,
            session_started=other_started,
            decision_event=other_decision,
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
        )

    planned = store.mark_planned(
        first.query_ref,
        plan_id="plan-1",
        decision_digest=_digest("decision"),
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
    )
    with pytest.raises(ManagedQueryStoreConflict, match="bound differently"):
        store.mark_planned(
            first.query_ref,
            plan_id="plan-1",
            decision_digest=_digest("substituted-decision"),
            journal_revision=2,
            journal_record_digest=_digest("journal-record"),
        )
    with pytest.raises(ManagedQueryStoreNotFound):
        store.load_by_scope_and_plan(_scope(exposure_id="unknown"), "plan-1")
    assert store.load(first.query_ref) == planned

    with pytest.raises(ManagedQueryStoreConflict, match="scope and plan"):
        store.register(
            logical_query_id=_digest("second-logical-query"),
            session_started=started,
            decision_event=decision,
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
        )


def test_registration_reserves_scope_and_plan_before_completion(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, decision = _events()
    owner = store.register(
        logical_query_id=_digest("scope-plan-owner"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )
    distinct_started = replace(
        started,
        event_id="event-start-substitute",
        causation_id="cause-start-substitute",
    )
    distinct_decision = replace(
        decision,
        event_id="event-decision-substitute",
        causation_id=distinct_started.event_id,
    )

    with pytest.raises(ManagedQueryStoreConflict, match="scope and plan"):
        store.register(
            logical_query_id=_digest("scope-plan-substitute"),
            session_started=distinct_started,
            decision_event=distinct_decision,
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
        )

    assert store.load(owner.query_ref) == owner
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (1,)


def test_later_same_stream_development_allows_new_work_and_exposure_across_reopen(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, intent = _events()
    child_scope = replace(
        intent.scope,
        exposure_id="exposure-child",
        host_context_id="child-agent",
        parent_exposure_id=intent.scope.exposure_id,
    )
    development = replace(
        intent,
        event_id="event-development",
        kind="DevelopmentObserved",
        scope=child_scope,
        expected_revision=3,
        correlation_id="plan-2",
        causation_id="event-turn-ended",
        work_signature=_digest("later-work"),
    )
    registered = store.register(
        logical_query_id=_digest("development-query"),
        session_started=started,
        decision_event=development,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )

    with pytest.raises(ManagedQueryStoreConflict, match="journal revision"):
        store.mark_planned(
            registered.query_ref,
            plan_id="plan-2",
            decision_digest=_digest("development-decision"),
            journal_revision=2,
            journal_record_digest=_digest("development-journal"),
        )
    planned = store.mark_planned(
        registered.query_ref,
        plan_id="plan-2",
        decision_digest=_digest("development-decision"),
        journal_revision=4,
        journal_record_digest=_digest("development-journal"),
    )

    reopened = open_managed_query_store(path=path, installation_hmac_key=KEY)

    assert planned.journal_revision == 4
    assert planned.session_started.to_json() == started.to_json()
    assert planned.decision_event == development
    assert reopened.load(registered.query_ref) == planned
    assert reopened.load_by_scope_and_plan(child_scope, "plan-2") == planned


@pytest.mark.parametrize(
    "defect",
    ["stream", "planner", "catalog", "host", "semantic-model", "semantic-index"],
)
def test_later_development_rejects_changed_stream_or_stable_planning_pin(
    tmp_path: Path,
    defect: str,
) -> None:
    store = open_managed_query_store(
        path=(tmp_path / "private" / "queries.sqlite3").absolute(),
        installation_hmac_key=KEY,
    )
    started, development = _events(kind="DevelopmentObserved")
    development = replace(
        development,
        expected_revision=2,
        correlation_id="plan-2",
        causation_id="event-turn-ended",
        work_signature=_digest("later-work"),
    )
    changes: dict[str, object]
    if defect == "stream":
        changes = {"scope": replace(development.scope, session_id="session-2")}
    elif defect == "planner":
        changes = {"planner_version": "planner-v4"}
    elif defect == "catalog":
        changes = {"catalog_snapshot_digest": _digest("other-catalog")}
    elif defect == "host":
        changes = {"host_descriptor_digest": _digest("other-host")}
    elif defect == "semantic-model":
        changes = {"semantic_model_digest": _digest("other-model")}
    else:
        changes = {"semantic_index_digest": _digest("other-index")}

    with pytest.raises(ValueError):
        store.register(
            logical_query_id=_digest(f"development-{defect}"),
            session_started=started,
            decision_event=replace(development, **changes),  # type: ignore[arg-type]
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
        )


def test_factory_and_registration_do_not_accept_caller_query_references(
    tmp_path: Path,
) -> None:
    assert "query_ref" not in inspect.signature(open_managed_query_store).parameters
    store = open_managed_query_store(
        path=(tmp_path / "private" / "queries.sqlite3").absolute(),
        installation_hmac_key=KEY,
    )
    assert "query_ref" not in inspect.signature(store.register).parameters
    assert str(tmp_path) not in repr(store)
    assert "kkkk" not in repr(store)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(store)

    for key in (b"short", bytearray(KEY), b"x" * 33):
        with pytest.raises(ValueError, match="exactly 32 bytes"):
            open_managed_query_store(
                path=(tmp_path / _digest(repr(key)) / "queries.sqlite3").absolute(),
                installation_hmac_key=key,  # type: ignore[arg-type]
            )

    started, decision = _events()
    with pytest.raises(ValueError, match="decision revision"):
        store.register(
            logical_query_id=_digest("oversized-revision"),
            session_started=started,
            decision_event=replace(decision, expected_revision=(1 << 63) - 1),
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
        )


def test_persisted_registration_contains_only_bounded_safe_protocol_content(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, decision = _events()
    record = store.register(
        logical_query_id=_digest("persisted-query"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT logical_query_id, query_ref, registration_json, registration_bytes, "
            "plan_id, decision_digest, journal_revision, journal_record_digest, row_hmac "
            "FROM managed_queries"
        ).fetchone()
    assert row is not None
    persisted = bytes(row[2]).decode("ascii")
    assert row[0] == record.logical_query_id
    assert row[1] == record.query_ref
    assert len(persisted.encode("ascii")) == row[3]
    assert all(row[index] is None for index in (4, 5, 6, 7))
    assert len(row[8]) == 64
    for forbidden in (str(tmp_path), "prompt", "authority", "credential", "source_code"):
        assert forbidden not in persisted
    assert KEY not in path.read_bytes()
    assert set(record.decision_event.to_dict()["payload"]) == {"observation_ref"}


def test_exact_registration_and_completion_are_cross_process_idempotent(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    path.parent.mkdir(mode=0o700)
    with ProcessPoolExecutor(max_workers=4) as pool:
        references = tuple(pool.map(_process_register, (str(path),) * 12))

    assert len(set(references)) == 1
    query_ref = references[0]
    with ProcessPoolExecutor(max_workers=4) as pool:
        outcomes = tuple(pool.map(_process_mark, ((str(path), query_ref),) * 12))

    assert set(outcomes) == {(query_ref, 2)}
    reopened = open_managed_query_store(path=path, installation_hmac_key=KEY)
    assert reopened.load(query_ref).journal_record_digest == _digest("cross-process-journal")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone()[0] == 1


def test_competing_scope_and_plan_registration_is_cross_process_exclusive(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    path.parent.mkdir(mode=0o700)
    arguments = ((str(path), "left"), (str(path), "right"))

    with ProcessPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(pool.map(_process_competing_register, arguments))

    assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "registered"]
    reopened = open_managed_query_store(path=path, installation_hmac_key=KEY)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone() == (1,)
    registered_ref = next(value for status, value in outcomes if status == "registered")
    assert reopened.load(registered_ref).planned is False


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX hard-exit semantics")
def test_committed_registration_and_completion_survive_process_hard_exit(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - parent performs assertions
        os.close(read_fd)
        try:
            child_store = open_managed_query_store(path=path, installation_hmac_key=KEY)
            started, decision = _events()
            query_ref = child_store.register(
                logical_query_id=_digest("hard-exit-query"),
                session_started=started,
                decision_event=decision,
                artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
                planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
            ).query_ref
            child_store.mark_planned(
                query_ref,
                plan_id="plan-1",
                decision_digest=_digest("hard-exit-decision"),
                journal_revision=2,
                journal_record_digest=_digest("hard-exit-journal"),
            )
            os.write(write_fd, query_ref.encode("ascii"))
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
    query_ref = result.decode("ascii")
    recovered = store.load(query_ref)
    assert recovered.logical_query_id == _digest("hard-exit-query")
    assert recovered.journal_record_digest == _digest("hard-exit-journal")


@pytest.mark.parametrize(
    "corruption",
    ["registration", "row-hmac", "partial-completion", "extra-schema"],
)
def test_load_or_reopen_rejects_persisted_corruption(
    tmp_path: Path,
    corruption: str,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, decision = _events()
    record = store.register(
        logical_query_id=_digest("corruption-query"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )
    with sqlite3.connect(path) as connection:
        if corruption == "registration":
            connection.execute(
                "UPDATE managed_queries SET registration_json = ? WHERE query_ref = ?",
                (b'{"prompt":"raw"}', record.query_ref),
            )
        elif corruption == "row-hmac":
            connection.execute(
                "UPDATE managed_queries SET row_hmac = ? WHERE query_ref = ?",
                ("0" * 64, record.query_ref),
            )
        elif corruption == "partial-completion":
            connection.execute(
                "UPDATE managed_queries SET plan_id = ? WHERE query_ref = ?",
                ("plan-1", record.query_ref),
            )
        else:
            connection.execute("CREATE TABLE injected(value TEXT)")

    if corruption == "extra-schema":
        with pytest.raises(ManagedQueryStoreCorruption, match="schema objects"):
            open_managed_query_store(path=path, installation_hmac_key=KEY)
    else:
        with pytest.raises(ManagedQueryStoreCorruption):
            store.load(record.query_ref)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX inode and mode semantics")
def test_store_rejects_wrong_key_replacement_permissions_and_sidecars(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, decision = _events()
    record = store.register(
        logical_query_id=_digest("filesystem-query"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )

    with pytest.raises(ManagedQueryStoreCorruption, match="installation key"):
        open_managed_query_store(path=path, installation_hmac_key=b"z" * 32)

    displaced = tmp_path / "displaced.sqlite3"
    os.replace(path, displaced)
    shutil.copyfile(displaced, path)
    path.chmod(0o600)
    with pytest.raises(ManagedQueryStoreCorruption, match="authenticated binding"):
        store.load(record.query_ref)

    reopened = open_managed_query_store(path=path, installation_hmac_key=KEY)
    path.chmod(0o644)
    with pytest.raises(ManagedQueryStoreCorruption, match="owner-private"):
        reopened.load(record.query_ref)

    path.chmod(0o600)
    sidecar = Path(f"{path}-wal")
    sidecar.symlink_to(displaced)
    with pytest.raises(ManagedQueryStoreError):
        reopened.load(record.query_ref)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX link semantics")
def test_factory_rejects_database_symlinks_hardlinks_and_preexisting_sidecars(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    target = parent / "target.sqlite3"
    open_managed_query_store(path=target, installation_hmac_key=KEY)

    symlink = parent / "symlink.sqlite3"
    symlink.symlink_to(target)
    with pytest.raises(ManagedQueryStoreError):
        open_managed_query_store(path=symlink, installation_hmac_key=KEY)

    hardlink = parent / "hardlink.sqlite3"
    os.link(target, hardlink)
    with pytest.raises(ManagedQueryStoreError):
        open_managed_query_store(path=hardlink, installation_hmac_key=KEY)
    hardlink.unlink()

    new_path = parent / "new.sqlite3"
    Path(f"{new_path}-journal").write_bytes(b"untrusted")
    with pytest.raises(ManagedQueryStoreCorruption, match="pre-existing SQLite sidecars"):
        open_managed_query_store(path=new_path, installation_hmac_key=KEY)


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-specific")
def test_nested_store_creation_fsyncs_every_new_directory_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fsync = os.fsync
    synced_directories: set[tuple[int, int]] = set()

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.add((metadata.st_dev, metadata.st_ino))
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    first = tmp_path / "first"
    second = first / "second"
    parent = second / "private"
    open_managed_query_store(
        path=(parent / "queries.sqlite3").absolute(),
        installation_hmac_key=KEY,
    )

    for directory in (tmp_path, first, second, parent):
        metadata = directory.stat()
        assert (metadata.st_dev, metadata.st_ino) in synced_directories


def test_failed_first_initialization_never_publishes_partial_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    real_link = managed_query_store_module.os.link

    def fail_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(managed_query_store_module.os, "link", fail_publication)
    with pytest.raises(ManagedQueryStoreError):
        open_managed_query_store(path=path, installation_hmac_key=KEY)

    assert not os.path.lexists(path)
    assert not tuple(path.parent.glob(".ctx-managed-query-init-*.stage"))

    monkeypatch.setattr(managed_query_store_module.os, "link", real_link)
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    started, decision = _events()
    registered = store.register(
        logical_query_id=_digest("after-failed-initialization"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )
    assert store.load(registered.query_ref) == registered


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX hard-exit semantics")
@pytest.mark.parametrize("crash_point", ["before-publication", "after-publication"])
def test_first_open_recovers_from_hard_exit_during_atomic_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - parent performs assertions
        if crash_point == "before-publication":
            monkeypatch.setattr(
                managed_query_store_module.os,
                "link",
                lambda *_args, **_kwargs: os._exit(0),
            )
        else:

            def crash_after_publication(_stage: Path) -> None:
                os._exit(0)

            monkeypatch.setattr(
                managed_query_store_module,
                "_remove_initialization_stage",
                crash_after_publication,
            )
        open_managed_query_store(path=path, installation_hmac_key=KEY)
        os._exit(1)

    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert tuple(path.parent.glob(".ctx-managed-query-init-*.stage"))
    if crash_point == "before-publication":
        assert not os.path.lexists(path)
    else:
        assert path.stat().st_nlink == 2

    store = open_managed_query_store(path=path, installation_hmac_key=KEY)

    assert path.stat().st_nlink == 1
    assert not tuple(path.parent.glob(".ctx-managed-query-init-*.stage"))
    started, decision = _events()
    registered = store.register(
        logical_query_id=_digest(f"recovered-{crash_point}"),
        session_started=started,
        decision_event=decision,
        artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
        planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
    )
    assert store.load(registered.query_ref) == registered


def test_registration_cannot_commit_database_growth_beyond_size_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = (tmp_path / "private" / "queries.sqlite3").absolute()
    store = open_managed_query_store(path=path, installation_hmac_key=KEY)
    original_size = path.stat().st_size
    monkeypatch.setattr(
        managed_query_store_module,
        "_MAX_DATABASE_BYTES",
        original_size + 1,
    )
    started, decision = _events()

    with pytest.raises(ManagedQueryStoreCapacityExceeded):
        store.register(
            logical_query_id=_digest("over-size-bound"),
            session_started=started,
            decision_event=decision,
            artifact_manifest_digest=ARTIFACT_MANIFEST_DIGEST,
            planning_environment_digest=PLANNING_ENVIRONMENT_DIGEST,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM managed_queries").fetchone()[0] == 0
    assert path.stat().st_size <= original_size + 1
