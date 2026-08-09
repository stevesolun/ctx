from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ctx.engine import store as store_module
from ctx.engine.protocol import ScopeRef, Transition
from ctx.engine.store import (
    EventIdCollision,
    JournalCorruption,
    JournalRecord,
    RevisionConflict,
    SQLiteEngineStore,
    StreamId,
)


def _scope(
    *,
    session_id: str = "session-1",
    exposure_id: str = "exposure-parent",
    host_context_id: str = "host-1",
) -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id=session_id,
        exposure_id=exposure_id,
        host_context_id=host_context_id,
    )


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _record(
    scope: ScopeRef,
    *,
    revision: int,
    event_id: str,
    event_payload: dict[str, object] | None = None,
) -> JournalRecord:
    stream_id = StreamId.from_scope(scope)
    transition = Transition(
        event_id=event_id,
        scope=scope,
        from_revision=revision - 1,
        to_revision=revision,
    )
    payload = event_payload or {"kind": "DevelopmentObserved", "sequence": revision}
    event_json = _canonical({"payload": payload, "scope": scope.to_dict()})
    return JournalRecord(
        stream_id=stream_id,
        revision=revision,
        event_id=event_id,
        event_content_digest=hashlib.sha256(event_json.encode()).hexdigest(),
        replay_json=event_json,
        transition_json=transition.to_json(),
        result_state_json=_canonical({"revision": revision, "event_id": event_id}),
        privacy_classification="private",
        retention_class="local",
        reducer_version="reducer-v1",
    )


def _chain_digest(record: JournalRecord, **overrides: object) -> str:
    values: dict[str, object] = {
        "event_content_digest": record.event_content_digest,
        "event_id": record.event_id,
        "previous_record_digest": record.previous_record_digest,
        "privacy_classification": record.privacy_classification,
        "reducer_version": record.reducer_version,
        "replay_digest": record.replay_digest,
        "retention_class": record.retention_class,
        "result_state_digest": record.result_state_digest,
        "revision": record.revision,
        "stream_key": record.stream_id.key,
        "transition_digest": record.transition_digest,
    }
    values.update(overrides)
    return hashlib.sha256(_canonical(values).encode()).hexdigest()


def test_genesis_commit_is_atomic_and_loadable(tmp_path: Path) -> None:
    db_path = tmp_path / "private" / "engine.sqlite3"
    store = SQLiteEngineStore(db_path)
    record = _record(_scope(), revision=1, event_id="event-1")

    result = store.commit(expected_revision=0, record=record)

    assert result.committed is True
    assert result.revision == 1
    assert result.transition == Transition.from_json(record.transition_json)
    assert store.load_head(record.stream_id).revision == 1
    assert store.load_head(record.stream_id).state_json == record.result_state_json
    assert list(store.records(record.stream_id)) == [result.record]
    if os.name != "nt":
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700


def test_stream_id_excludes_exposure_and_host_context() -> None:
    parent = StreamId.from_scope(_scope())
    child = StreamId.from_scope(_scope(exposure_id="exposure-child", host_context_id="host-child"))

    assert child == parent


def test_exact_duplicate_after_later_revision_returns_original_transition(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    first = _record(_scope(), revision=1, event_id="event-1")
    first_result = store.commit(expected_revision=0, record=first)
    store.commit(
        expected_revision=1,
        record=_record(_scope(), revision=2, event_id="event-2"),
    )

    duplicate = store.commit(expected_revision=0, record=first)

    assert duplicate.committed is False
    assert duplicate.revision == 1
    assert duplicate.transition.to_json() == first_result.transition.to_json()
    assert len(list(store.records(first.stream_id))) == 2


def test_event_id_collision_wins_over_stale_revision(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    original = _record(_scope(), revision=1, event_id="event-1")
    store.commit(expected_revision=0, record=original)
    store.commit(
        expected_revision=1,
        record=_record(_scope(), revision=2, event_id="event-2"),
    )
    collision = _record(
        _scope(),
        revision=1,
        event_id="event-1",
        event_payload={"kind": "DevelopmentObserved", "changed": True},
    )

    with pytest.raises(EventIdCollision) as raised:
        store.commit(expected_revision=0, record=collision)

    assert raised.value.event_id == "event-1"
    assert raised.value.stored_digest == original.event_content_digest
    assert raised.value.submitted_digest == collision.event_content_digest


def test_new_event_with_stale_revision_is_rejected(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    store.commit(
        expected_revision=0,
        record=_record(_scope(), revision=1, event_id="event-1"),
    )

    with pytest.raises(RevisionConflict) as raised:
        store.commit(
            expected_revision=0,
            record=_record(_scope(), revision=1, event_id="event-stale"),
        )

    assert raised.value.expected == 0
    assert raised.value.actual == 1


def test_two_writers_cannot_commit_the_same_revision(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    first_store = SQLiteEngineStore(db_path)
    second_store = SQLiteEngineStore(db_path)
    records = (
        _record(_scope(), revision=1, event_id="event-a"),
        _record(_scope(), revision=1, event_id="event-b"),
    )

    def commit(item: tuple[SQLiteEngineStore, JournalRecord]) -> object:
        store, record = item
        try:
            return store.commit(expected_revision=0, record=record)
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(commit, zip((first_store, second_store), records)))

    assert sum(getattr(outcome, "committed", False) is True for outcome in outcomes) == 1
    assert sum(isinstance(outcome, RevisionConflict) for outcome in outcomes) == 1
    assert first_store.load_head(records[0].stream_id).revision == 1
    assert len(list(first_store.records(records[0].stream_id))) == 1


def test_sessions_have_independent_revision_streams(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    first = _record(_scope(session_id="session-a"), revision=1, event_id="event-a")
    second = _record(_scope(session_id="session-b"), revision=1, event_id="event-b")

    store.commit(expected_revision=0, record=first)
    store.commit(expected_revision=0, record=second)

    assert store.load_head(first.stream_id).revision == 1
    assert store.load_head(second.stream_id).revision == 1
    assert [record.event_id for record in store.records(first.stream_id)] == ["event-a"]
    assert [record.event_id for record in store.records(second.stream_id)] == ["event-b"]


def test_event_ids_are_global_across_streams(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    first = _record(_scope(session_id="session-a"), revision=1, event_id="event-shared")
    second = _record(_scope(session_id="session-b"), revision=1, event_id="event-shared")
    store.commit(expected_revision=0, record=first)

    with pytest.raises(EventIdCollision):
        store.commit(expected_revision=0, record=second)

    assert store.load_head(second.stream_id).revision == 0


def test_matching_event_digest_never_crosses_stream_boundary(tmp_path: Path) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    first = _record(_scope(session_id="session-a"), revision=1, event_id="event-shared")
    other_stream = StreamId.from_scope(_scope(session_id="session-b"))
    store.commit(expected_revision=0, record=first)

    with pytest.raises(EventIdCollision):
        store.cached_transition(other_stream, first.event_id, first.event_content_digest)

    cross_stream = replace(
        _record(_scope(session_id="session-b"), revision=1, event_id="event-shared"),
        event_content_digest=first.event_content_digest,
    )
    with pytest.raises(EventIdCollision):
        store.commit(expected_revision=0, record=cross_stream)

    assert store.load_head(other_stream).revision == 0


def test_projection_digest_is_reported_and_repair_is_revision_checked(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    first = _record(_scope(), revision=1, event_id="event-1")
    committed = store.commit(expected_revision=0, record=first)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE engine_streams SET state_json = ? WHERE stream_key = ?",
            (_canonical({"revision": 999}), first.stream_id.key),
        )

    assert store.load_head(first.stream_id).projection_valid is False
    assert (
        store.repair_projection(
            first.stream_id,
            at_revision=1,
            state_json=first.result_state_json,
            record_digest=committed.record.record_digest,
        )
        is True
    )
    repaired = store.load_head(first.stream_id)
    assert repaired.projection_valid is True
    assert repaired.state_json == first.result_state_json

    store.commit(
        expected_revision=1,
        record=_record(_scope(), revision=2, event_id="event-2"),
    )
    assert (
        store.repair_projection(
            first.stream_id,
            at_revision=1,
            state_json=first.result_state_json,
            record_digest=committed.record.record_digest,
        )
        is False
    )
    assert store.load_head(first.stream_id).revision == 2


def test_journal_survives_projection_loss_and_repair_reconstructs_it(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    record = _record(_scope(), revision=1, event_id="event-1")
    committed = store.commit(expected_revision=0, record=record)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "DELETE FROM engine_streams WHERE stream_key = ?",
            (record.stream_id.key,),
        )

    missing = store.load_head(record.stream_id)

    assert missing.revision == 1
    assert missing.projection_valid is False
    assert missing.record_digest == committed.record.record_digest
    assert [item.event_id for item in store.records(record.stream_id)] == ["event-1"]
    cached = store.cached_transition(
        record.stream_id,
        record.event_id,
        record.event_content_digest,
    )
    assert cached is not None
    assert cached.event_id == record.event_id
    assert (
        store.repair_projection(
            record.stream_id,
            at_revision=1,
            state_json=record.result_state_json,
            record_digest=committed.record.record_digest,
        )
        is True
    )
    repaired = store.load_head(record.stream_id)
    assert repaired.revision == 1
    assert repaired.projection_valid is True


def test_commit_extends_valid_journal_when_projection_is_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    first = _record(_scope(), revision=1, event_id="event-1")
    store.commit(expected_revision=0, record=first)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "DELETE FROM engine_streams WHERE stream_key = ?",
            (first.stream_id.key,),
        )

    second = _record(_scope(), revision=2, event_id="event-2")
    store.commit(expected_revision=1, record=second)

    head = store.load_head(first.stream_id)
    assert head.revision == 2
    assert head.projection_valid is True
    assert [item.event_id for item in store.records(first.stream_id)] == [
        "event-1",
        "event-2",
    ]


def test_legacy_projection_foreign_key_is_removed_on_open(tmp_path: Path) -> None:
    parent = tmp_path / "engine"
    parent.mkdir(mode=0o700)
    db_path = parent / "journal.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE engine_streams (stream_key TEXT PRIMARY KEY);
            CREATE TABLE engine_journal (
                event_id TEXT PRIMARY KEY,
                stream_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                event_content_digest TEXT NOT NULL,
                replay_json TEXT NOT NULL,
                replay_digest TEXT NOT NULL,
                transition_json TEXT NOT NULL,
                transition_digest TEXT NOT NULL,
                result_state_json TEXT NOT NULL,
                result_state_digest TEXT NOT NULL,
                previous_record_digest TEXT,
                record_digest TEXT NOT NULL,
                privacy_classification TEXT NOT NULL,
                retention_class TEXT NOT NULL,
                reducer_version TEXT NOT NULL,
                UNIQUE (stream_key, revision),
                FOREIGN KEY (stream_key) REFERENCES engine_streams(stream_key)
            );
            """
        )
    db_path.chmod(0o600)

    SQLiteEngineStore(db_path)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA foreign_key_list(engine_journal)").fetchall() == []


def test_forged_journal_transition_fails_closed_at_every_head_boundary(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    record = _record(_scope(), revision=1, event_id="event-1")
    committed = store.commit(expected_revision=0, record=record)
    forged_transition = _canonical({"forged": True})
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE engine_journal
               SET transition_json = ?, transition_digest = ?
             WHERE event_id = ?
            """,
            (
                forged_transition,
                hashlib.sha256(forged_transition.encode()).hexdigest(),
                record.event_id,
            ),
        )

    with pytest.raises(JournalCorruption):
        store.load_head(record.stream_id)
    with pytest.raises(JournalCorruption):
        store.repair_projection(
            record.stream_id,
            at_revision=1,
            state_json=record.result_state_json,
            record_digest=committed.record.record_digest,
        )
    with pytest.raises(JournalCorruption):
        store.commit(
            expected_revision=1,
            record=_record(_scope(), revision=2, event_id="event-2"),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("replay_json", '{"noncanonical": true}'),
        ("replay_digest", "not-a-digest"),
        ("record_digest", "f" * 64),
    ),
)
def test_malformed_persisted_record_fields_are_typed_as_corruption(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    record = _record(_scope(), revision=1, event_id="event-1")
    store.commit(expected_revision=0, record=record)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"UPDATE engine_journal SET {column} = ? WHERE event_id = ?",
            (value, record.event_id),
        )

    with pytest.raises(JournalCorruption):
        store.load_head(record.stream_id)


def test_invalid_utf8_in_persisted_text_is_typed_as_corruption(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    record = _record(_scope(), revision=1, event_id="event-1")
    store.commit(expected_revision=0, record=record)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE engine_journal
               SET replay_json = CAST(X'80' AS TEXT)
             WHERE event_id = ?
            """,
            (record.event_id,),
        )

    with pytest.raises(JournalCorruption):
        store.load_head(record.stream_id)


def test_invalid_utf8_projection_cannot_hide_authoritative_head(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    record = _record(_scope(), revision=1, event_id="event-1")
    committed = store.commit(expected_revision=0, record=record)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE engine_streams
               SET state_json = CAST(X'80' AS TEXT)
             WHERE stream_key = ?
            """,
            (record.stream_id.key,),
        )

    head = store.load_head(record.stream_id)

    assert head.revision == 1
    assert head.state_json == record.result_state_json
    assert head.record_digest == committed.record.record_digest
    assert head.projection_valid is False
    assert (
        store.repair_projection(
            record.stream_id,
            at_revision=1,
            state_json=record.result_state_json,
            record_digest=committed.record.record_digest,
        )
        is True
    )
    assert store.load_head(record.stream_id).projection_valid is True


def test_malformed_projection_schema_cannot_hide_authoritative_head(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    record = _record(_scope(), revision=1, event_id="event-1")
    committed = store.commit(expected_revision=0, record=record)
    with sqlite3.connect(db_path) as connection:
        connection.execute("ALTER TABLE engine_streams DROP COLUMN state_digest")

    head = store.load_head(record.stream_id)

    assert head.revision == 1
    assert head.state_json == record.result_state_json
    assert head.record_digest == committed.record.record_digest
    assert head.projection_valid is False
    assert (
        store.repair_projection(
            record.stream_id,
            at_revision=1,
            state_json=record.result_state_json,
            record_digest=committed.record.record_digest,
        )
        is True
    )
    assert store.load_head(record.stream_id).projection_valid is True


def test_projection_without_stream_uniqueness_is_rebuilt_for_repair_and_commit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    first = _record(_scope(), revision=1, event_id="event-1")
    committed = store.commit(expected_revision=0, record=first)

    def remove_stream_key_constraint() -> None:
        with sqlite3.connect(db_path) as connection:
            connection.executescript(
                """
                ALTER TABLE engine_streams RENAME TO engine_streams_valid;
                CREATE TABLE engine_streams (
                    stream_key TEXT,
                    tenant_id TEXT,
                    workspace_id TEXT,
                    repository_id TEXT,
                    session_id TEXT,
                    revision INTEGER,
                    state_json TEXT,
                    state_digest TEXT,
                    head_record_digest TEXT
                );
                INSERT INTO engine_streams
                SELECT * FROM engine_streams_valid;
                DROP TABLE engine_streams_valid;
                """
            )

    remove_stream_key_constraint()
    assert store.load_head(first.stream_id).projection_valid is False
    assert (
        store.repair_projection(
            first.stream_id,
            at_revision=1,
            state_json=first.result_state_json,
            record_digest=committed.record.record_digest,
        )
        is True
    )
    assert store.load_head(first.stream_id).projection_valid is True

    remove_stream_key_constraint()
    second = _record(_scope(), revision=2, event_id="event-2")
    store.commit(expected_revision=1, record=second)

    head = store.load_head(first.stream_id)
    assert head.revision == 2
    assert head.projection_valid is True
    assert [item.event_id for item in store.records(first.stream_id)] == [
        "event-1",
        "event-2",
    ]


@pytest.mark.parametrize(
    ("column", "forged_value"),
    (
        ("privacy_classification", "unknown-privacy"),
        ("retention_class", "unknown-retention"),
    ),
)
def test_self_consistent_unknown_policy_label_is_journal_corruption(
    tmp_path: Path,
    column: str,
    forged_value: str,
) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    store = SQLiteEngineStore(db_path)
    source = _record(_scope(), revision=1, event_id="event-1")
    committed = store.commit(expected_revision=0, record=source)
    forged_digest = _chain_digest(committed.record, **{column: forged_value})
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"""
            UPDATE engine_journal
               SET {column} = ?, record_digest = ?
             WHERE event_id = ?
            """,
            (forged_value, forged_digest, source.event_id),
        )

    with pytest.raises(JournalCorruption):
        store.load_head(source.stream_id)


def test_failed_journal_insert_rolls_back_genesis_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    record = _record(_scope(), revision=1, event_id="event-1")

    def fail_insert(_connection: sqlite3.Connection, _record: JournalRecord) -> None:
        raise RuntimeError("injected failure after stream projection insert")

    monkeypatch.setattr(SQLiteEngineStore, "_insert_record", staticmethod(fail_insert))

    with pytest.raises(RuntimeError, match="injected failure"):
        store.commit(expected_revision=0, record=record)

    assert store.load_head(record.stream_id).revision == 0
    assert list(store.records(record.stream_id)) == []


def test_symlinked_database_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "linked.sqlite3"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="symlinked path"):
        SQLiteEngineStore(link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_existing_shared_parent_is_rejected_without_chmod(tmp_path: Path) -> None:
    parent = tmp_path / "shared-engine"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)

    with pytest.raises(ValueError, match="owner-private"):
        SQLiteEngineStore(parent / "journal.sqlite3")

    assert stat.S_IMODE(parent.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_sqlite_permission_hardening_failure_is_not_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "private-engine"
    parent.mkdir(mode=0o700)

    def deny_chmod(_path: os.PathLike[str] | str, _mode: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "chmod", deny_chmod)

    with pytest.raises(PermissionError, match="denied"):
        SQLiteEngineStore(parent / "journal.sqlite3")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and mode contract")
def test_sidecar_symlink_is_rejected_before_its_target_is_chmodded(tmp_path: Path) -> None:
    db_path = tmp_path / "engine" / "journal.sqlite3"
    SQLiteEngineStore(db_path)
    target = tmp_path / "unrelated.txt"
    target.write_text("unrelated", encoding="utf-8")
    target.chmod(0o644)
    sidecar = Path(f"{db_path}-wal")
    try:
        sidecar.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="symlinked path|regular file"):
        store_module._secure_sqlite_files(db_path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644
