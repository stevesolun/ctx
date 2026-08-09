from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ctx.engine.store import JournalCorruption, SQLiteEngineStore, StreamId


def _schema(path: Path) -> list[tuple[str, str, str, str | None]]:
    with sqlite3.connect(path) as connection:
        return [
            (str(row[0]), str(row[1]), str(row[2]), row[3])
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql
                  FROM sqlite_master
                 ORDER BY type, name
                """
            ).fetchall()
        ]


def _directory_bytes(parent: Path) -> dict[str, bytes]:
    return {
        candidate.name: candidate.read_bytes()
        for candidate in parent.iterdir()
        if candidate.is_file()
    }


def _create_delete_mode_store(path: Path) -> None:
    SQLiteEngineStore(path)
    with sqlite3.connect(path) as connection:
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    assert mode is not None and mode[0] == "delete"


def test_read_only_store_opens_exact_schema_without_mutating_database(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    _create_delete_mode_store(path)
    schema_before = _schema(path)
    files_before = _directory_bytes(path.parent)

    store = SQLiteEngineStore.open_read_only(path)

    assert store.path == path
    assert (
        store.load_head(
            StreamId(
                tenant_id="tenant-1",
                workspace_id="workspace-1",
                repository_id="repository-1",
                session_id="session-1",
            )
        ).revision
        == 0
    )
    assert _schema(path) == schema_before
    assert _directory_bytes(path.parent) == files_before
    with sqlite3.connect(path) as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()
    assert mode is not None and mode[0] == "delete"


def test_read_only_store_rejects_empty_database_without_initializing_it(tmp_path: Path) -> None:
    parent = tmp_path / "engine"
    parent.mkdir(mode=0o700)
    path = parent / "journal.sqlite3"
    path.touch(mode=0o600)
    files_before = _directory_bytes(parent)

    with pytest.raises(JournalCorruption, match="schema"):
        SQLiteEngineStore.open_read_only(path)

    assert _schema(path) == []
    assert _directory_bytes(parent) == files_before
    assert not (parent / "install-execution-locks").exists()


def test_read_only_store_rejects_missing_table_without_repairing_it(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    _create_delete_mode_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE engine_activation_outcomes")
    schema_before = _schema(path)
    files_before = _directory_bytes(path.parent)

    with pytest.raises(JournalCorruption, match="schema"):
        SQLiteEngineStore.open_read_only(path)

    assert _schema(path) == schema_before
    assert _directory_bytes(path.parent) == files_before


def test_read_only_store_rejects_augmented_schema_without_removing_it(tmp_path: Path) -> None:
    path = tmp_path / "engine" / "journal.sqlite3"
    _create_delete_mode_store(path)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unreviewed_extension (value TEXT)")
    schema_before = _schema(path)
    files_before = _directory_bytes(path.parent)

    with pytest.raises(JournalCorruption, match="exact required schema"):
        SQLiteEngineStore.open_read_only(path)

    assert _schema(path) == schema_before
    assert _directory_bytes(path.parent) == files_before
