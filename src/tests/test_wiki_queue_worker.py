"""Tests for draining durable wiki queue jobs."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import networkx as nx
import numpy as np
import pytest
from networkx.readwrite import node_link_data

from ctx.core.graph.entity_overlays import active_overlay_records, load_overlay_records
from ctx.core.graph.resolve_graph import load_graph, resolve_by_seeds
from ctx.core.graph.vector_index import build_vector_index
from ctx.core.wiki import wiki_queue, wiki_queue_worker


def _write_entity(wiki: Path, relpath: str, text: str) -> Path:
    path = wiki / relpath
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_base_graph_for_overlay(wiki: Path) -> Path:
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph = nx.Graph()
    graph.add_node(
        "skill:python-testing",
        type="skill",
        label="python-testing",
        tags=["python", "testing"],
    )
    graph.add_node(
        "skill:ruby-testing",
        type="skill",
        label="ruby-testing",
        tags=["ruby", "testing"],
    )
    graph_path = graph_dir / "graph.json"
    graph_path.write_text(
        json.dumps(node_link_data(graph, edges="edges")),
        encoding="utf-8",
    )
    return graph_path


def _build_vector_index_for_overlay(wiki: Path) -> Path:
    index_dir = wiki / ".embedding-cache" / "graph" / "vector-index"
    build_vector_index(
        kind="numpy-flat",
        model_id="test-model",
        node_ids=["skill:python-testing", "skill:ruby-testing"],
        content_hashes=["hash-python", "hash-ruby"],
        vectors=np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    ).save(index_dir)
    return index_dir


def _patch_test_embedder(monkeypatch: Any) -> None:
    fake_embedder = SimpleNamespace(
        name="test-model",
        embed=lambda _texts: np.asarray([[1.0, 0.0]], dtype=np.float32),
    )
    monkeypatch.setitem(
        sys.modules,
        "embedding_backend",
        SimpleNamespace(get_embedder=lambda *_args, **_kwargs: fake_embedder),
    )


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
    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert [job.kind for job in jobs] == [
        wiki_queue.ENTITY_UPSERT_JOB,
        wiki_queue.GRAPH_EXPORT_JOB,
    ]
    assert jobs[1].status == wiki_queue.STATUS_PENDING
    assert jobs[1].payload == {
        "graph_only": True,
        "incremental": True,
        "source": "entity-upsert",
    }


def test_process_next_entity_upsert_runs_incremental_attach_when_index_exists(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    entity_text = "---\ntags:\n  - testing\n---\n# alpha\n"
    entity_path = _write_entity(wiki, "entities/skills/alpha.md", entity_text)
    index_dir = wiki / ".embedding-cache" / "graph" / "vector-index"
    index_dir.mkdir(parents=True)
    (index_dir / "vector-index.meta.json").write_text("{}", encoding="utf-8")
    wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="skill",
        slug="alpha",
        entity_path=entity_path,
        content=entity_text,
        action="created",
        source="test",
        now=10.0,
    )
    monkeypatch.setattr(wiki_queue_worker, "update_index", MagicMock())
    calls: list[dict[str, Any]] = []

    def fake_attach_entity(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "inserted", "record": {}}

    monkeypatch.setattr(wiki_queue_worker, "attach_entity", fake_attach_entity)

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.status == wiki_queue.STATUS_SUCCEEDED
    assert "incremental attach inserted" in result.message
    assert calls == [
        {
            "index_dir": index_dir,
            "overlay_path": wiki / "graphify-out" / "entity-overlays.jsonl",
            "node_id": "skill:alpha",
            "entity_type": "skill",
            "label": "alpha",
            "tags": ["testing"],
            "text": entity_text,
            "vector_json": None,
            "model_id": None,
            "top_k": 20,
            "min_score": 0.5,
            "min_final_weight": 0.03,
        }
    ]


def test_process_next_entity_upsert_real_attach_writes_active_overlay_and_resolver_sees_it(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    graph_path = _write_base_graph_for_overlay(wiki)
    _build_vector_index_for_overlay(wiki)
    entity_text = "---\ntags:\n  - python\n  - testing\n---\n# worker-python\n"
    entity_path = _write_entity(wiki, "entities/skills/worker-python.md", entity_text)
    queued = wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="skill",
        slug="worker-python",
        entity_path=entity_path,
        content=entity_text,
        action="created",
        source="test",
        now=10.0,
    )
    monkeypatch.setattr(wiki_queue_worker, "update_index", MagicMock())
    _patch_test_embedder(monkeypatch)

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.job_id == queued.id
    assert result.status == wiki_queue.STATUS_SUCCEEDED
    assert "incremental attach inserted" in result.message

    overlay_path = wiki / "graphify-out" / "entity-overlays.jsonl"
    records = load_overlay_records(overlay_path)
    active = active_overlay_records(records)
    assert len(active) == 1
    record = active[0]
    assert record["kind"] == "ann_attach"
    assert record["node_id"] == "skill:worker-python"
    assert record["nodes"] == [
        {
            "id": "skill:worker-python",
            "label": "worker-python",
            "title": "worker-python",
            "type": "skill",
            "tags": ["python", "testing"],
            "source": "incremental-attach",
            "content_hash": record["content_hash"],
            "updated": record["created_at"],
        }
    ]
    assert len(record["edges"]) == 1
    edge = record["edges"][0]
    assert edge["target"] == "skill:python-testing"
    assert edge["weight"] == edge["final_weight"]
    assert edge["final_weight"] > 0
    assert edge["semantic_sim"] == pytest.approx(1.0)
    assert edge["score_components"]

    graph = load_graph(graph_path)
    assert graph.has_edge("skill:worker-python", "skill:python-testing")
    resolved = resolve_by_seeds(graph, ["python-testing"], max_hops=1, top_n=5)
    assert any(item["name"] == "worker-python" for item in resolved)


def test_process_next_entity_upsert_does_not_fail_when_incremental_attach_fails(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    entity_path = _write_entity(wiki, "entities/agents/beta.md", "# beta\n")
    index_dir = wiki / ".embedding-cache" / "graph" / "vector-index"
    index_dir.mkdir(parents=True)
    (index_dir / "vector-index.meta.json").write_text("{}", encoding="utf-8")
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
    monkeypatch.setattr(wiki_queue_worker, "update_index", MagicMock())

    def fail_attach_entity(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("embedding backend missing")

    monkeypatch.setattr(wiki_queue_worker, "attach_entity", fail_attach_entity)

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.job_id == queued.id
    assert result.status == wiki_queue.STATUS_SUCCEEDED
    assert "incremental attach skipped (embedding backend missing)" in result.message
    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert [job.kind for job in jobs] == [
        wiki_queue.ENTITY_UPSERT_JOB,
        wiki_queue.GRAPH_EXPORT_JOB,
    ]
    assert jobs[1].status == wiki_queue.STATUS_PENDING
    assert jobs[1].payload == {
        "graph_only": True,
        "incremental": True,
        "source": "entity-upsert",
    }


def test_process_next_entity_delete_queues_full_graph_refresh(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    entity_path = wiki / "entities" / "skills" / "deleted.md"
    queued = wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="skill",
        slug="deleted",
        entity_path=entity_path,
        content="",
        action="delete",
        source="test",
        now=10.0,
    )
    update_index = MagicMock()
    monkeypatch.setattr(wiki_queue_worker, "update_index", update_index)

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.job_id == queued.id
    assert result.status == wiki_queue.STATUS_SUCCEEDED
    assert result.message == "queued full graph refresh for deleted skills entity deleted"
    update_index.assert_not_called()
    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert [job.kind for job in jobs] == [
        wiki_queue.ENTITY_UPSERT_JOB,
        wiki_queue.GRAPH_EXPORT_JOB,
    ]
    assert jobs[1].payload == {
        "graph_only": True,
        "incremental": False,
        "source": "entity-delete",
    }


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


def test_process_next_graph_export_job_uses_maintenance_handler(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    queued = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"graph_only": True},
        source="test",
        now=10.0,
    )
    calls: list[tuple[Path, dict[str, Any]]] = []

    def handle_graph_export(path: Path, payload: dict[str, Any]) -> str:
        calls.append((path, payload))
        return "graph exported"

    monkeypatch.setitem(
        wiki_queue_worker.MAINTENANCE_HANDLERS,
        wiki_queue.GRAPH_EXPORT_JOB,
        handle_graph_export,
    )

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.job_id == queued.id
    assert result.kind == wiki_queue.GRAPH_EXPORT_JOB
    assert result.status == wiki_queue.STATUS_SUCCEEDED
    assert result.message == "graph exported"
    assert calls == [(wiki, {"graph_only": True, "source": "test"})]

    db_path = wiki_queue.queue_db_path(wiki)
    stolen_job = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"graph_only": True},
        source="lease-stolen-test",
        now=30.0,
    )

    def steal_lease(_path: Path, _payload: dict[str, Any]) -> str:
        stolen = wiki_queue.lease_next(db_path, worker_id="worker-b", now=46.0)
        assert stolen is not None
        assert stolen.id == stolen_job.id
        return "graph exported too late"

    monkeypatch.setitem(
        wiki_queue_worker.MAINTENANCE_HANDLERS,
        wiki_queue.GRAPH_EXPORT_JOB,
        steal_lease,
    )

    with pytest.raises(RuntimeError, match="not leased by worker worker-a"):
        wiki_queue_worker.process_next(
            wiki,
            worker_id="worker-a",
            lease_seconds=5.0,
            now=40.0,
        )

    current = wiki_queue.get_job(db_path, stolen_job.id)
    assert current.status == wiki_queue.STATUS_RUNNING
    assert current.worker_id == "worker-b"


def test_process_next_maintenance_job_retries_handler_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    wiki = tmp_path / "wiki"
    queued = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.TAR_REFRESH_JOB,
        payload={"catalog": "graph/skills-sh-catalog.json.gz"},
        source="test",
        now=10.0,
    )

    def fail_refresh(_path: Path, _payload: dict[str, Any]) -> str:
        raise RuntimeError("tar refresh failed")

    monkeypatch.setitem(
        wiki_queue_worker.MAINTENANCE_HANDLERS,
        wiki_queue.TAR_REFRESH_JOB,
        fail_refresh,
    )

    result = wiki_queue_worker.process_next(wiki, worker_id="worker-a", now=20.0)

    assert result is not None
    assert result.job_id == queued.id
    assert result.kind == wiki_queue.TAR_REFRESH_JOB
    assert result.status == wiki_queue.STATUS_PENDING
    current = wiki_queue.get_job(wiki_queue.queue_db_path(wiki), queued.id)
    assert current.status == wiki_queue.STATUS_PENDING
    assert "tar refresh failed" in str(current.last_error)


def test_tar_refresh_handler_uses_from_catalog_for_catalog_payload(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> object:
        calls.append(args)
        return object()

    monkeypatch.setattr(wiki_queue_worker.subprocess, "run", fake_run)

    message = wiki_queue_worker._handle_tar_refresh(
        tmp_path / "wiki",
        {
            "catalog": "graph/skills-sh-catalog.json.gz",
            "wiki_tar": "graph/wiki-graph.tar.gz",
            "drop_body_unavailable": True,
            "source": "test",
        },
    )

    assert message == "tar refresh completed"
    assert len(calls) == 1
    args = calls[0]
    assert "--from-catalog" in args
    assert "--from-api-union" not in args
    assert args[args.index("--from-catalog") + 1] == "graph/skills-sh-catalog.json.gz"
    assert "--update-wiki-tar" in args
    assert "--drop-body-unavailable" in args


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
