"""End-to-end entity authoring through the real graph/index queue worker."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator
import sys

import networkx as nx
from networkx.readwrite import node_link_data
import numpy as np
import pytest

import agent_add
import harness_add
import mcp_add
import skill_add
from ctx.core.graph.entity_overlays import active_overlay_records, load_overlay_records
from ctx.core.graph.graph_packs import build_pack_manifest, write_pack_manifest
from ctx.core.graph.resolve_graph import load_graph
from ctx.core.graph.vector_index import build_vector_index, load_vector_index
from ctx.core.wiki import wiki_queue, wiki_queue_worker
from ctx.core.wiki.wiki_sync import ensure_wiki
from mcp_entity import McpRecord


class _AllowedIntake:
    allow = True
    warnings: tuple[Any, ...] = ()
    failures: tuple[Any, ...] = ()


def _write_graph_fixture(wiki: Path) -> Path:
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph = nx.Graph()
    graph.add_node(
        "skill:python-goal",
        type="skill",
        label="python-goal",
        tags=["python", "testing"],
    )
    graph.add_node(
        "skill:ruby-goal",
        type="skill",
        label="ruby-goal",
        tags=["ruby", "testing"],
    )
    graph.add_node(
        "harness:ollama-model-goal",
        type="harness",
        label="ollama-model-goal",
        tags=["ollama", "local-llm"],
    )
    graph_path = graph_dir / "graph.json"
    graph_path.write_text(
        json.dumps(node_link_data(graph, edges="edges")),
        encoding="utf-8",
    )

    base_dir = graph_dir / "packs" / "base-export-1"
    base_dir.mkdir(parents=True)
    (base_dir / "graph.json").write_text(
        graph_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="test-config",
            model_id="test-model",
            node_count=3,
            edge_count=0,
            artifact_paths=["graph.json"],
        ),
    )

    index_dir = wiki / ".embedding-cache" / "graph" / "vector-index"
    build_vector_index(
        kind="numpy-flat",
        model_id="test-model",
        node_ids=[
            "skill:python-goal",
            "skill:ruby-goal",
            "harness:ollama-model-goal",
        ],
        content_hashes=["python-hash", "ruby-hash", "ollama-hash"],
        vectors=np.asarray(
            [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        ),
    ).save(index_dir)
    return graph_path


def _patch_local_intake_and_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (skill_add, agent_add, mcp_add):
        monkeypatch.setattr(module, "check_intake", lambda *_args, **_kwargs: _AllowedIntake())
        monkeypatch.setattr(module, "record_embedding", lambda **_kwargs: None)

    def embed(texts: list[str]) -> np.ndarray:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            if "ruby" in lowered:
                vectors.append([0.0, 1.0, 0.0])
            elif "ollama" in lowered:
                vectors.append([0.0, 0.0, 1.0])
            else:
                vectors.append([1.0, 0.0, 0.0])
        return np.asarray(vectors, dtype=np.float32)

    fake_embedder = SimpleNamespace(name="test-model", embed=embed)
    monkeypatch.setitem(
        sys.modules,
        "embedding_backend",
        SimpleNamespace(get_embedder=lambda *_args, **_kwargs: fake_embedder),
    )
    monkeypatch.setitem(
        sys.modules,
        "ctx_audit_log",
        SimpleNamespace(log_skill_event=lambda *_args, **_kwargs: None),
    )


def _drain_successfully(
    wiki: Path,
    *,
    expected_job_id: int,
) -> list[wiki_queue_worker.ProcessResult]:
    results = wiki_queue_worker.drain_queue(wiki, worker_id="entity-integration-worker")
    assert results
    assert {result.status for result in results} == {wiki_queue.STATUS_SUCCEEDED}
    assert expected_job_id in {result.job_id for result in results}
    return results


def _remove_index_entry(wiki: Path, link: str) -> None:
    index_path = wiki / "index.md"
    lines = index_path.read_text(encoding="utf-8").splitlines()
    assert any(link in line for line in lines)
    index_path.write_text(
        "\n".join(line for line in lines if link not in line) + "\n",
        encoding="utf-8",
    )


def _artifact_snapshot(wiki: Path, *page_paths: Path) -> dict[str, str]:
    paths = [wiki / "index.md", *page_paths]
    paths.extend(
        path
        for root in (
            wiki / "graphify-out" / "entity-overlays.jsonl",
            wiki / "graphify-out" / "packs",
            wiki / ".embedding-cache" / "graph" / "vector-index-deltas",
            wiki / "wiki-packs",
            wiki / "entities" / "mcp-servers" / ".canonical-index.json",
        )
        for path in ([root] if root.is_file() else root.rglob("*") if root.is_dir() else [])
        if path.is_file()
    )
    return {
        path.relative_to(wiki).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.is_file()
    }


def _skill_text(name: str, language: str) -> str:
    return f"""---
name: {name}
description: Local integration skill for {language} repository verification.
---

# {name}

## Overview

Use {language} checks to validate a repository through the CTX queue worker.

## Procedure

Run the repository-native tests and report the verified result.
"""


def _agent_text() -> str:
    return """---
name: graph-reviewer
description: Reviews Python changes and runs repository-native tests.
model: inherit
---

# Graph reviewer

Review Python changes, run their tests, and report concrete evidence.
"""


def test_all_authored_entity_types_reach_real_graph_and_index_without_stale_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "wiki"
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    ensure_wiki(str(wiki))
    skills_dir.mkdir()
    agents_dir.mkdir()
    graph_path = _write_graph_fixture(wiki)
    _patch_local_intake_and_embeddings(monkeypatch)

    skill_source = tmp_path / "SKILL.md"
    skill_source.write_text(_skill_text("graph-skill", "Python"), encoding="utf-8")
    skill_result = skill_add.add_skill(
        source_path=skill_source,
        name="graph-skill",
        wiki_path=wiki,
        skills_dir=skills_dir,
        review_existing=True,
    )
    assert skill_result["queued_job_id"] is not None
    skill_link = "[[entities/skills/graph-skill]]"
    _remove_index_entry(wiki, skill_link)
    _drain_successfully(wiki, expected_job_id=skill_result["queued_job_id"])
    assert skill_link in (wiki / "index.md").read_text(encoding="utf-8")

    agent_source = tmp_path / "graph-reviewer.md"
    agent_source.write_text(_agent_text(), encoding="utf-8")
    agent_result = agent_add.add_agent(
        source_path=agent_source,
        name="graph-reviewer",
        wiki_path=wiki,
        agents_dir=agents_dir,
        review_existing=True,
    )
    assert agent_result["queued_job_id"] is not None
    agent_link = "[[entities/agents/graph-reviewer]]"
    _remove_index_entry(wiki, agent_link)
    _drain_successfully(wiki, expected_job_id=agent_result["queued_job_id"])
    assert agent_link in (wiki / "index.md").read_text(encoding="utf-8")

    mcp_record = McpRecord.from_dict(
        {
            "slug": "python-context-mcp",
            "name": "Python Context MCP",
            "description": "Streams Python repository context for deterministic local checks.",
            "sources": ["integration-test"],
            "github_url": "https://github.com/example/python-context-mcp",
            "tags": ["python", "testing"],
            "transports": ["stdio"],
        }
    )
    mcp_result = mcp_add.add_mcp(record=mcp_record, wiki_path=wiki)
    assert mcp_result["queued_job_id"] is not None
    mcp_link = "[[entities/mcp-servers/p/python-context-mcp]]"
    _remove_index_entry(wiki, mcp_link)
    _drain_successfully(wiki, expected_job_id=mcp_result["queued_job_id"])
    assert mcp_link in (wiki / "index.md").read_text(encoding="utf-8")

    harness_record = harness_add.HarnessRecord.from_dict(
        {
            "repo_url": "https://github.com/example/ollama-harness",
            "slug": "ollama-harness",
            "name": "Ollama Harness",
            "description": "Local Ollama harness for repository-native verification.",
            "tags": ["harness", "ollama", "local-llm"],
            "model_providers": ["ollama"],
            "runtimes": ["python"],
            "capabilities": ["Run a local model against repository verification goals."],
            "setup_commands": ["ollama serve"],
            "verify_commands": ["pytest"],
        }
    )
    harness_result = harness_add.add_harness(record=harness_record, wiki_path=wiki)
    assert harness_result["queued_job_id"] is not None
    harness_link = "[[entities/harnesses/ollama-harness]]"
    _remove_index_entry(wiki, harness_link)
    _drain_successfully(wiki, expected_job_id=harness_result["queued_job_id"])
    assert harness_link in (wiki / "index.md").read_text(encoding="utf-8")

    graph = load_graph(graph_path)
    expected_neighbors = {
        "skill:graph-skill": "skill:python-goal",
        "agent:graph-reviewer": "skill:python-goal",
        "mcp-server:python-context-mcp": "skill:python-goal",
        "harness:ollama-harness": "harness:ollama-model-goal",
    }
    for node_id, neighbor_id in expected_neighbors.items():
        assert graph.has_edge(node_id, neighbor_id)
        edge = graph.edges[node_id, neighbor_id]
        assert edge["final_weight"] > 0
        assert edge["score_components"]["semantic"] > 0

    index_text = (wiki / "index.md").read_text(encoding="utf-8")
    for link in (
        skill_link,
        agent_link,
        mcp_link,
        harness_link,
    ):
        assert index_text.count(link) == 1

    harness_page = wiki / "entities" / "harnesses" / "ollama-harness.md"
    assert "model_providers:\n- ollama" in harness_page.read_text(encoding="utf-8")

    skill_source.write_text(_skill_text("graph-skill", "Ruby"), encoding="utf-8")
    applied = skill_add.add_skill(
        source_path=skill_source,
        name="graph-skill",
        wiki_path=wiki,
        skills_dir=skills_dir,
        review_existing=True,
        update_existing=True,
    )
    assert applied["is_new_page"] is False
    assert applied["queued_job_id"] is not None
    _drain_successfully(wiki, expected_job_id=applied["queued_job_id"])

    records = load_overlay_records(wiki / "graphify-out" / "entity-overlays.jsonl")
    skill_records = [record for record in records if record.get("node_id") == "skill:graph-skill"]
    active_skill_records = [
        record
        for record in active_overlay_records(records)
        if record.get("node_id") == "skill:graph-skill"
    ]
    assert len(skill_records) == 2
    assert sum("superseded_at" in record for record in skill_records) == 1
    assert len(active_skill_records) == 1
    active_targets = [edge["target"] for edge in active_skill_records[0]["edges"]]
    assert active_targets == ["skill:ruby-goal"]
    assert len(active_targets) == len(set(active_targets))

    updated_graph = load_graph(graph_path)
    assert updated_graph.has_edge("skill:graph-skill", "skill:ruby-goal")
    assert not updated_graph.has_edge("skill:graph-skill", "skill:python-goal")

    agent_page = wiki / "entities" / "agents" / "graph-reviewer.md"
    before_failed_worker = _artifact_snapshot(wiki, agent_page)
    failed_job = wiki_queue.enqueue_entity_upsert(
        wiki,
        entity_type="agent",
        slug="graph-reviewer",
        entity_path=agent_page,
        content="outdated content that no longer matches the page",
        action="updated",
        source="integration-test",
    )
    failed = wiki_queue_worker.process_next(
        wiki,
        worker_id="entity-integration-worker",
        retry_delay_seconds=3600.0,
    )
    assert failed is not None
    assert failed.job_id == failed_job.id
    assert "content hash mismatch" in failed.message
    assert _artifact_snapshot(wiki, agent_page) == before_failed_worker

    mcp_page = wiki / "entities" / "mcp-servers" / "p" / "python-context-mcp.md"
    before_rejected_review = _artifact_snapshot(wiki, mcp_page)
    review = mcp_add.add_mcp(
        record=McpRecord.from_dict(
            {
                "slug": "python-context-mcp",
                "name": "Python Context MCP",
                "description": "Proposed replacement that still needs user review.",
                "sources": ["proposed-update"],
                "github_url": "https://github.com/example/python-context-mcp",
                "tags": ["python"],
                "transports": ["stdio"],
            }
        ),
        wiki_path=wiki,
        review_existing=True,
    )
    assert review["update_required"] is True
    assert review["queued_job_id"] is None
    assert _artifact_snapshot(wiki, mcp_page) == before_rejected_review

    delta_dir = wiki / ".embedding-cache" / "graph" / "vector-index-deltas" / "local-harness"
    delta_meta = json.loads((delta_dir / "vector-index.meta.json").read_text(encoding="utf-8"))
    delta_index = load_vector_index(
        delta_dir,
        expected_model_id="test-model",
        expected_content_fingerprint=delta_meta["content_fingerprint"],
    )
    assert delta_index is not None
    assert delta_index.node_ids == ["harness:ollama-harness"]


class _GuardedStdin(Iterator[str]):
    def __init__(self, lines: list[str], *, before_second: Callable[[], bool]) -> None:
        self._lines = iter(lines)
        self._before_second = before_second
        self._yielded = 0

    def __iter__(self) -> _GuardedStdin:
        return self

    def __next__(self) -> str:
        if self._yielded == 1 and not self._before_second():
            raise AssertionError("MCP stdin records were preloaded before processing began")
        line = next(self._lines)
        self._yielded += 1
        return line

    def read(self, *_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("MCP stdin batches must be streamed, not preloaded")

    def readlines(self, *_args: Any, **_kwargs: Any) -> list[str]:
        raise AssertionError("MCP stdin batches must be streamed, not preloaded")


def test_mcp_stdin_jsonl_uses_guarded_stream_without_preloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "wiki"
    first_record = {
        "slug": "stream-guard-mcp",
        "name": "Stream Guard MCP",
        "description": "Streams MCP catalog records without preloading the full input.",
        "sources": ["integration-test"],
        "github_url": "https://github.com/example/stream-guard-mcp",
        "tags": ["testing"],
        "transports": ["stdio"],
    }
    second_record = {
        **first_record,
        "slug": "stream-guard-second-mcp",
        "name": "Stream Guard Second MCP",
        "github_url": "https://github.com/example/stream-guard-second-mcp",
    }
    first_page = wiki / "entities" / "mcp-servers" / "s" / "stream-guard-mcp.md"
    guarded_stdin = _GuardedStdin(
        [json.dumps(first_record) + "\n", json.dumps(second_record) + "\n"],
        before_second=first_page.is_file,
    )
    monkeypatch.setattr(sys, "stdin", guarded_stdin)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mcp_add.py", "--from-stdin", "--wiki", str(wiki)],
    )
    monkeypatch.setattr(mcp_add, "check_intake", lambda *_args, **_kwargs: _AllowedIntake())
    monkeypatch.setattr(mcp_add, "record_embedding", lambda **_kwargs: None)

    mcp_add.main()

    assert first_page.is_file()
    assert (wiki / "entities" / "mcp-servers" / "s" / "stream-guard-second-mcp.md").is_file()
    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert len(jobs) == 2
    assert {job.kind for job in jobs} == {wiki_queue.ENTITY_UPSERT_JOB}
