"""A-Z validation for entity onboarding plus incremental graph attach."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph
import numpy as np
import pytest

from ctx.core.graph.incremental_attach import attach_entity
from ctx.core.graph.vector_index import build_vector_index
from ctx.core.wiki import wiki_queue, wiki_queue_worker
from ctx.core.wiki.wiki_sync import ensure_wiki


def _allow_intake(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(allow=True, warnings=[], failures=[])


def _fresh_module(name: str) -> Any:
    sys.modules.pop(name, None)
    return importlib.import_module(name)


def _skill_body(name: str, detail: str) -> str:
    return f"""---
name: {name}
description: A focused test skill for validating ctx onboarding and graph attach.
---

# {name}

## Overview

{detail} This body is intentionally longer than the intake minimum so the
same flow can be used with the real structural gate when needed.

## Usage

Use this skill to validate add, update, queue, and graph attach behavior.
"""


def _write_base_graph(wiki: Path) -> None:
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph = nx.Graph()
    graph.add_node(
        "skill:existing-python-helper",
        type="skill",
        label="existing-python-helper",
        tags=["python", "testing"],
        quality_score=0.9,
    )
    graph.add_node(
        "mcp-server:existing-github-mcp",
        type="mcp-server",
        label="existing-github-mcp",
        tags=["github", "testing"],
        quality_score=0.8,
    )
    graph.add_edge(
        "skill:existing-python-helper",
        "mcp-server:existing-github-mcp",
        weight=0.72,
        final_weight=0.72,
        semantic_sim=0.72,
        shared_tags=["testing"],
        reasons=["fixture"],
    )
    (graph_dir / "graph.json").write_text(
        json.dumps(json_graph.node_link_data(graph, edges="links")),
        encoding="utf-8",
    )


def _build_test_index(wiki: Path) -> Path:
    index_dir = wiki / ".embedding-cache" / "graph" / "vector-index"
    index = build_vector_index(
        kind="numpy-flat",
        model_id="test-model",
        node_ids=["skill:existing-python-helper", "mcp-server:existing-github-mcp"],
        content_hashes=["hash-skill", "hash-mcp"],
        vectors=np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    index.save(index_dir)
    return index_dir


def test_entity_onboarding_incremental_attach_a_to_z(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_add = _fresh_module("skill_add")
    mcp_add = _fresh_module("mcp_add")
    harness_add = _fresh_module("harness_add")
    mt = _fresh_module("ctx.monitor.testing")
    mcp_entity = _fresh_module("mcp_entity")

    claude = tmp_path / ".claude"
    wiki = claude / "skill-wiki"
    skills_dir = claude / "skills"
    ensure_wiki(str(wiki))
    skills_dir.mkdir(parents=True)
    _write_base_graph(wiki)

    monkeypatch.setattr(skill_add, "check_intake", _allow_intake)
    monkeypatch.setattr(skill_add, "record_embedding", lambda **_kwargs: None)
    monkeypatch.setattr(mcp_add, "check_intake", _allow_intake)
    monkeypatch.setattr(mcp_add, "record_embedding", lambda **_kwargs: None)
    monkeypatch.setattr(mt, "claude_dir", lambda: claude)
    monkeypatch.setattr(mt, "dashboard_graph_index_archives", lambda: [])

    source = tmp_path / "SKILL.md"
    source.write_text(
        _skill_body("az-skill", "Initial version covers Python testing."),
        encoding="utf-8",
    )

    added = skill_add.add_skill(
        source_path=source,
        name="az-skill",
        wiki_path=wiki,
        skills_dir=skills_dir,
        review_existing=True,
    )
    assert added["is_new_page"] is True
    assert (wiki / "entities" / "skills" / "az-skill.md").is_file()

    first_worker = wiki_queue_worker.process_next(
        wiki,
        worker_id="az-worker",
    )
    assert first_worker is not None
    assert "incremental attach skipped (no vector index)" in first_worker.message
    monkeypatch.setitem(
        wiki_queue_worker.MAINTENANCE_HANDLERS,
        wiki_queue.GRAPH_EXPORT_JOB,
        lambda _wiki, _payload: "graph export skipped in A-Z test",
    )
    cleared = wiki_queue_worker.process_next(wiki, worker_id="az-worker")
    assert cleared is not None
    assert cleared.kind == wiki_queue.GRAPH_EXPORT_JOB

    source.write_text(
        _skill_body("az-skill", "Updated version covers queue recovery."),
        encoding="utf-8",
    )
    review = skill_add.add_skill(
        source_path=source,
        name="az-skill",
        wiki_path=wiki,
        skills_dir=skills_dir,
        review_existing=True,
    )
    assert review["update_required"] is True
    assert "Existing skill already exists: az-skill" in review["update_review"]

    index_dir = _build_test_index(wiki)
    applied = skill_add.add_skill(
        source_path=source,
        name="az-skill",
        wiki_path=wiki,
        skills_dir=skills_dir,
        review_existing=True,
        update_existing=True,
    )
    assert applied["is_new_page"] is False

    attach_calls: list[dict[str, Any]] = []

    def fake_attach_entity(**kwargs: Any) -> dict[str, Any]:
        attach_calls.append(kwargs)
        return {"status": "inserted", "record": {}}

    monkeypatch.setattr(wiki_queue_worker, "attach_entity", fake_attach_entity)
    recovered = wiki_queue_worker.process_next(wiki, worker_id="az-worker")
    assert recovered is not None
    assert "incremental attach inserted" in recovered.message
    assert attach_calls and attach_calls[0]["node_id"] == "skill:az-skill"

    mcp_record = mcp_entity.McpRecord.from_dict(
        {
            "slug": "az-mcp",
            "name": "AZ MCP",
            "description": "A test MCP server used to validate catalog onboarding.",
            "sources": ["az-test"],
            "github_url": "https://github.com/example/az-mcp",
            "tags": ["testing", "github"],
            "transports": ["stdio"],
        }
    )
    mcp_result = mcp_add.add_mcp(record=mcp_record, wiki_path=wiki)
    assert mcp_result["is_new_page"] is True
    assert (wiki / "entities" / "mcp-servers" / "a" / "az-mcp.md").is_file()

    harness_record = harness_add.HarnessRecord.from_dict(
        {
            "repo_url": "https://github.com/example/az-harness",
            "slug": "az-harness",
            "name": "AZ Harness",
            "description": "A test harness for local model setup validation.",
            "tags": ["harness", "local-llm", "testing"],
            "model_providers": ["ollama"],
            "capabilities": ["Run a local LLM with ctx recommendations attached."],
            "setup_commands": ["echo setup"],
            "verify_commands": ["echo verify"],
        }
    )
    harness_result = harness_add.add_harness(record=harness_record, wiki_path=wiki)
    assert harness_result["is_new_page"] is True
    assert (wiki / "entities" / "harnesses" / "az-harness.md").is_file()

    overlay = wiki / "graphify-out" / "entity-overlays.jsonl"
    dry_run = attach_entity(
        index_dir=index_dir,
        overlay_path=overlay,
        node_id="skill:az-dry-run",
        entity_type="skill",
        label="az-dry-run",
        tags=["testing"],
        text="dry run attach",
        vector_json="[1.0, 0.0]",
        model_id="test-model",
        top_k=2,
        min_score=0.0,
        min_final_weight=0.0,
        dry_run=True,
    )
    assert dry_run["status"] == "dry-run"
    assert not overlay.exists()

    with pytest.raises(ValueError, match="vector index metadata not found"):
        attach_entity(
            index_dir=wiki / "missing-index",
            overlay_path=overlay,
            node_id="skill:az-missing-index",
            entity_type="skill",
            label="az-missing-index",
            tags=[],
            text="missing backend",
            vector_json="[1.0, 0.0]",
            model_id="test-model",
            top_k=1,
            min_score=0.0,
            min_final_weight=0.0,
        )

    attached = attach_entity(
        index_dir=index_dir,
        overlay_path=overlay,
        node_id="skill:az-attached",
        entity_type="skill",
        label="az-attached",
        tags=["testing"],
        text="attached skill",
        vector_json="[1.0, 0.0]",
        model_id="test-model",
        top_k=2,
        min_score=0.0,
        min_final_weight=0.0,
    )
    assert attached["status"] == "inserted"
    assert overlay.is_file()

    graph_payload = mt.graph_neighborhood("az-attached", entity_type="skill")
    assert graph_payload["center"] == "skill:az-attached"
    assert {node["data"]["id"] for node in graph_payload["nodes"]} >= {
        "skill:az-attached",
        "skill:existing-python-helper",
    }
    rendered = mt.render_graph("az-attached", "skill")
    assert "Knowledge graph" in rendered
    assert "az-attached" in rendered

    ok, message = mt.delete_wiki_entity("az-skill", "skill")
    assert ok is True, message
    assert not (wiki / "entities" / "skills" / "az-skill.md").exists()
    jobs = wiki_queue.list_jobs(wiki_queue.queue_db_path(wiki))
    assert any(
        job.kind == wiki_queue.ENTITY_UPSERT_JOB
        and job.payload.get("action") == "delete"
        and job.payload.get("slug") == "az-skill"
        for job in jobs
    )
