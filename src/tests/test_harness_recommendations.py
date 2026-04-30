"""Regression coverage for harness recommendation surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import networkx as nx

import ctx.api
import ctx_init
import scan_repo
from ctx.core.resolve import resolve_skills


def _minimal_profile() -> dict[str, Any]:
    return {
        "repo_path": "/tmp/repo",
        "languages": [],
        "frameworks": [
            {"name": "react", "confidence": 0.95, "evidence": ["package.json"]}
        ],
        "infrastructure": [],
        "data_stores": [],
        "testing": [],
        "ai_tooling": [],
        "build_system": [],
        "docs": [],
    }


class _FakeGraph:
    def number_of_nodes(self) -> int:
        return 1


def _harness_graph() -> nx.Graph:
    graph = nx.Graph()
    graph.add_node(
        "harness:langgraph",
        label="langgraph",
        type="harness",
        tags=["python", "openai", "agents", "graph", "checkpointing", "harness"],
    )
    graph.add_node(
        "harness:weak-match",
        label="weak-match",
        type="harness",
        tags=["python"],
    )
    graph.add_node(
        "skill:python-helper",
        label="python-helper",
        type="skill",
        tags=["python", "openai", "agents", "graph", "harness"],
    )
    return graph


def test_resolver_ignores_harness_graph_hits_in_scan_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolve_skills, "_GRAPH_AVAILABLE", True)
    monkeypatch.setattr(resolve_skills, "_load_graph", lambda: _FakeGraph())

    def fake_resolve_by_seeds(graph: _FakeGraph, seeds: list[str], **kwargs: Any):
        return [
            {
                "name": "text-to-cad",
                "type": "harness",
                "score": 3.0,
                "normalized_score": 0.72,
                "shared_tags": ["cad", "automation"],
                "via": ["react"],
            }
        ]

    monkeypatch.setattr(resolve_skills, "_resolve_by_seeds", fake_resolve_by_seeds)

    available = {
        "react": {
            "path": str(tmp_path / "react" / "SKILL.md"),
            "name": "react",
        }
    }
    manifest = resolve_skills.resolve(_minimal_profile(), available, {})

    assert manifest["harnesses"] == []
    assert [m["name"] for m in manifest["mcp_servers"]] == []
    assert "text-to-cad" not in [entry["skill"] for entry in manifest["load"]]


def test_scan_recommendations_do_not_print_harness_bucket(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolve_skills, "discover_available_skills", lambda _: {})
    monkeypatch.setattr(resolve_skills, "read_wiki_overrides", lambda _: {})
    monkeypatch.setattr(
        resolve_skills,
        "resolve",
        lambda *args, **kwargs: {
            "load": [],
            "mcp_servers": [],
            "harnesses": [
                {
                    "name": "text-to-cad",
                    "score": 4.0,
                    "normalized_score": 0.8,
                    "shared_tags": ["cad", "3d"],
                    "reason": "graph neighbor of react",
                }
            ],
            "warnings": [],
        },
    )

    scan_repo._print_recommendations(str(tmp_path), {"repo_path": str(tmp_path)})
    output = capsys.readouterr().out

    assert "-- Harnesses" not in output
    assert "text-to-cad" not in output


def test_public_api_lists_harness_entities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = tmp_path / "skill-wiki"
    skills = wiki / "entities" / "skills"
    harnesses = wiki / "entities" / "harnesses"
    skills.mkdir(parents=True)
    harnesses.mkdir(parents=True)
    (skills / "python-patterns.md").write_text("# Python Patterns\n", encoding="utf-8")
    (harnesses / "text-to-cad.md").write_text("# Text to CAD\n", encoding="utf-8")

    monkeypatch.setattr(ctx.api, "default_wiki_dir", lambda: wiki)

    assert ctx.api.list_all_entities("harness") == ["text-to-cad"]
    assert "text-to-cad" in ctx.api.list_all_entities()
    assert ctx.api.list_all_entities("plugin") == []


def test_ctx_init_recommends_harnesses_from_dedicated_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctx_init, "_load_recommendation_graph", _harness_graph)

    results = ctx_init.recommend_harnesses(
        "build an openai python agent graph with checkpointing",
        top_k=5,
    )

    assert [row["name"] for row in results] == ["langgraph"]
    assert {row["type"] for row in results} == {"harness"}
    assert results[0]["normalized_score"] >= 0.85


def test_ctx_init_prints_harness_install_handoff_for_custom_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ctx_init, "_load_recommendation_graph", _harness_graph)
    args = type("Args", (), {
        "model_mode": "custom",
        "model": "openai/gpt-5.5",
        "model_provider": "openai",
        "api_key_env": "",
        "base_url": None,
        "goal": "build an openai python agent graph with checkpointing",
        "force": True,
        "validate_model": False,
    })()

    assert ctx_init.run_model_onboarding(args, tmp_path) == 0

    output = capsys.readouterr().out
    assert "recommended harnesses" in output
    assert "ctx-harness-install langgraph --dry-run" in output
