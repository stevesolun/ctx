"""Tests for the persistent wiki/graph ingest queue."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ctx.core.wiki import wiki_queue


def test_init_queue_enables_wal_and_creates_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "wiki-queue.sqlite3"

    wiki_queue.init_queue(db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        table_count = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='wiki_queue_jobs'",
        ).fetchone()[0]
    assert table_count == 1


def test_enqueue_is_idempotent_by_key(tmp_path: Path) -> None:
    db_path = tmp_path / "wiki-queue.sqlite3"

    first = wiki_queue.enqueue(
        db_path,
        kind="entity-upsert",
        payload={"slug": "alpha"},
        idempotency_key="entity-upsert:skill:alpha",
        now=10.0,
    )
    second = wiki_queue.enqueue(
        db_path,
        kind="entity-upsert",
        payload={"slug": "beta"},
        idempotency_key="entity-upsert:skill:alpha",
        now=11.0,
    )

    assert second.id == first.id
    assert second.payload == {"slug": "alpha"}
    assert wiki_queue.list_jobs(db_path) == [first]


def test_enqueue_maintenance_job_is_idempotent_by_payload(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"

    first = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"graph_only": True, "incremental": True},
        source="test",
        now=10.0,
    )
    second = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"incremental": True, "graph_only": True},
        source="test",
        now=11.0,
    )

    assert second.id == first.id
    assert second.kind == wiki_queue.GRAPH_EXPORT_JOB
    assert second.payload["source"] == "test"
    assert second.payload["graph_only"] is True
    assert wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki)) == [first]


def test_enqueue_maintenance_job_rejects_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported maintenance job kind"):
        wiki_queue.enqueue_maintenance_job(
            tmp_path / "wiki",
            kind="unknown-maintenance",
            payload={},
            source="test",
        )


def test_lease_next_claims_oldest_available_job(tmp_path: Path) -> None:
    db_path = tmp_path / "wiki-queue.sqlite3"
    first = wiki_queue.enqueue(db_path, kind="graph-export", payload={"n": 1}, now=10.0)
    second = wiki_queue.enqueue(db_path, kind="graph-export", payload={"n": 2}, now=11.0)

    leased = wiki_queue.lease_next(
        db_path,
        worker_id="worker-a",
        lease_seconds=30.0,
        now=20.0,
    )

    assert leased is not None
    assert leased.id == first.id
    assert leased.status == "running"
    assert leased.attempts == 1
    assert leased.worker_id == "worker-a"
    assert leased.leased_until == 50.0
    assert wiki_queue.get_job(db_path, second.id).status == "pending"


def test_lease_next_can_filter_job_kinds(tmp_path: Path) -> None:
    db_path = tmp_path / "wiki-queue.sqlite3"
    graph = wiki_queue.enqueue(db_path, kind="graph-export", payload={}, now=10.0)
    entity = wiki_queue.enqueue(db_path, kind="entity-upsert", payload={}, now=11.0)

    leased = wiki_queue.lease_next(
        db_path,
        worker_id="worker-a",
        kinds=("entity-upsert",),
        now=20.0,
    )

    assert leased is not None
    assert leased.id == entity.id
    assert wiki_queue.get_job(db_path, graph.id).status == "pending"


def test_mark_failed_retries_until_max_attempts_then_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "wiki-queue.sqlite3"
    job = wiki_queue.enqueue(
        db_path,
        kind="graph-link-refresh",
        payload={"slug": "alpha"},
        max_attempts=2,
        now=10.0,
    )

    leased = wiki_queue.lease_next(db_path, worker_id="worker-a", now=20.0)
    assert leased is not None
    retry = wiki_queue.mark_failed(
        db_path,
        leased.id,
        error="temporary failure",
        retry=True,
        delay_seconds=15.0,
        now=21.0,
    )
    assert retry.status == "pending"
    assert retry.available_at == 36.0
    assert retry.last_error == "temporary failure"
    assert wiki_queue.lease_next(db_path, worker_id="worker-a", now=30.0) is None

    leased_again = wiki_queue.lease_next(db_path, worker_id="worker-a", now=40.0)
    assert leased_again is not None
    assert leased_again.id == job.id
    terminal = wiki_queue.mark_failed(
        db_path,
        leased_again.id,
        error="permanent failure",
        retry=True,
        now=41.0,
    )
    assert terminal.status == "failed"
    assert terminal.last_error == "permanent failure"
    assert wiki_queue.lease_next(db_path, worker_id="worker-a", now=50.0) is None


def test_expired_running_job_is_recovered_for_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "wiki-queue.sqlite3"
    job = wiki_queue.enqueue(db_path, kind="tar-refresh", payload={}, now=10.0)

    first = wiki_queue.lease_next(
        db_path,
        worker_id="worker-a",
        lease_seconds=5.0,
        now=20.0,
    )
    assert first is not None
    assert first.id == job.id
    assert wiki_queue.lease_next(db_path, worker_id="worker-b", now=24.0) is None

    recovered = wiki_queue.lease_next(db_path, worker_id="worker-b", now=26.0)

    assert recovered is not None
    assert recovered.id == job.id
    assert recovered.worker_id == "worker-b"
    assert recovered.attempts == 2


def test_mark_succeeded_makes_job_unavailable(tmp_path: Path) -> None:
    db_path = tmp_path / "wiki-queue.sqlite3"
    wiki_queue.enqueue(db_path, kind="graph-export", payload={}, now=10.0)
    leased = wiki_queue.lease_next(db_path, worker_id="worker-a", now=20.0)
    assert leased is not None

    done = wiki_queue.mark_succeeded(db_path, leased.id, now=21.0)

    assert done.status == "succeeded"
    assert done.leased_until is None
    assert done.worker_id is None
    assert wiki_queue.lease_next(db_path, worker_id="worker-a", now=22.0) is None


def test_queue_rejects_symlinked_database_path(tmp_path: Path) -> None:
    real = tmp_path / "real.sqlite3"
    link = tmp_path / "queue.sqlite3"
    real.write_text("", encoding="utf-8")
    try:
        link.symlink_to(real)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="symlink"):
        wiki_queue.init_queue(link)
