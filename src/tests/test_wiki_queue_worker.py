"""Tests for draining durable wiki queue jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ctx.core.wiki import wiki_queue, wiki_queue_worker


def _write_entity(wiki: Path, relpath: str, text: str) -> Path:
    path = wiki / relpath
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_process_next_entity_upsert_succeeds_and_refreshes_index(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    entity_path = _write_entity(
        wiki,
        "entities/skills/alpha.md",
        "# alpha\n\n## Usage\n\nUse alpha.\n",
    )
    queued = wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="skill",
        slug="alpha",
        entity_path=entity_path,
        content=entity_path.read_text(encoding="utf-8"),
        action="created",
        source="test",
        now=10.0,
    )
    update_index = MagicMock()
    monkeypatch.setattr(wiki_queue_worker, "update_index", update_index)

    result = wiki_queue_worker.process_next(
        wiki,
        worker_id="worker-a",
        lease_seconds=30.0,
        now=20.0,
    )

    assert result is not None
    assert result.job_id == queued.id
    assert result.status == wiki_queue.STATUS_SUCCEEDED
    assert wiki_queue.get_job(wiki_queue.queue_db_path(wiki), queued.id).status == (
        wiki_queue.STATUS_SUCCEEDED
    )
    update_index.assert_called_once_with(str(wiki), ["alpha"], subject_type="skills")


def test_process_next_retries_hash_mismatch(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    entity_path = _write_entity(wiki, "entities/agents/beta.md", "# beta\n")
    queued = wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="agent",
        slug="beta",
        entity_path=entity_path,
        content=entity_path.read_text(encoding="utf-8"),
        action="created",
        source="test",
        now=10.0,
    )
    entity_path.write_text("# beta changed\n", encoding="utf-8")
    update_index = MagicMock()
    monkeypatch.setattr(wiki_queue_worker, "update_index", update_index)

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.job_id == queued.id
    assert result.status == wiki_queue.STATUS_PENDING
    current = wiki_queue.get_job(wiki_queue.queue_db_path(wiki), queued.id)
    assert current.status == wiki_queue.STATUS_PENDING
    assert "content hash mismatch" in str(current.last_error)
    update_index.assert_not_called()


def test_process_next_rejects_entity_path_escape(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    db_path = wiki_queue.queue_db_path(wiki)
    queued = wiki_queue.enqueue(
        db_path,
        kind=wiki_queue.ENTITY_UPSERT_JOB,
        payload={
            "entity_type": "skill",
            "slug": "escape",
            "entity_path": "../escape.md",
            "content_hash": "0" * 64,
        },
        now=10.0,
    )

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.job_id == queued.id
    assert result.status == wiki_queue.STATUS_PENDING
    current = wiki_queue.get_job(db_path, queued.id)
    assert "escapes wiki root" in str(current.last_error)


def test_drain_queue_honors_limit(tmp_path: Path, monkeypatch: Any) -> None:
    wiki = tmp_path / "wiki"
    first = _write_entity(wiki, "entities/mcp-servers/a/alpha.md", "# alpha\n")
    second = _write_entity(wiki, "entities/harnesses/beta.md", "# beta\n")
    first_job = wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="mcp-server",
        slug="alpha",
        entity_path=first,
        content=first.read_text(encoding="utf-8"),
        action="created",
        source="test",
        now=10.0,
    )
    second_job = wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="harness",
        slug="beta",
        entity_path=second,
        content=second.read_text(encoding="utf-8"),
        action="created",
        source="test",
        now=11.0,
    )
    monkeypatch.setattr(wiki_queue_worker, "update_index", MagicMock())

    results = wiki_queue_worker.drain_queue(
        wiki,
        worker_id="worker-a",
        limit=1,
        now=20.0,
    )

    assert [result.job_id for result in results] == [first_job.id]
    assert wiki_queue.get_job(wiki_queue.queue_db_path(wiki), first_job.id).status == (
        wiki_queue.STATUS_SUCCEEDED
    )
    assert wiki_queue.get_job(wiki_queue.queue_db_path(wiki), second_job.id).status == (
        wiki_queue.STATUS_PENDING
    )


def test_drain_queue_rejects_negative_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limit must be >= 0"):
        wiki_queue_worker.drain_queue(tmp_path / "wiki", worker_id="worker-a", limit=-1)
