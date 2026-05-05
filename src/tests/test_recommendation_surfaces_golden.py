"""Golden tests for the shared recommendation engine.

These tests deliberately cross adapter boundaries. The same synthetic graph
must produce the same ranked bundle from:
  - ctx.core.resolve.recommendations.recommend_by_tags
  - Claude Code's context_monitor.graph_suggest hook
  - generic harness ctx__recommend_bundle
  - public ctx.recommend_bundle
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import networkx as nx
import pytest

import ctx.api
import scan_repo
from ctx.adapters.claude_code.hooks import context_monitor
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall
from ctx.core.graph.resolve_graph import load_graph
from ctx.core.resolve.recommendations import query_to_tags, recommend_by_tags
from ctx.core.resolve.resolve_skills import resolve


def _write_golden_graph(graph_path: Path) -> nx.Graph:
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph = nx.Graph()
    graph.graph["external_catalog_nodes"] = {"skills.sh": 1}
    graph.graph["source_catalog_nodes"] = {"skills.sh": 1}
    graph.add_node(
        "skill:fastapi-python-async",
        label="fastapi-python-async",
        type="skill",
        tags=["fastapi", "python", "async"],
    )
    graph.add_node(
        "agent:fastapi-code-reviewer",
        label="fastapi-code-reviewer",
        type="agent",
        tags=["fastapi", "code", "review"],
    )
    graph.add_node(
        "mcp-server:fastapi-docs",
        label="fastapi-docs",
        type="mcp-server",
        tags=["fastapi", "docs"],
    )
    graph.add_node(
        "skill:legacy-build",
        label="legacy-build",
        type="skill",
        tags=["build"],
    )
    graph.add_edge(
        "skill:fastapi-python-async",
        "agent:fastapi-code-reviewer",
        weight=1.0,
        shared_tags=["fastapi"],
    )
    graph.add_edge(
        "skill:fastapi-python-async",
        "mcp-server:fastapi-docs",
        weight=0.8,
        shared_tags=["fastapi"],
    )
    graph_path.write_text(
        json.dumps(nx.node_link_data(graph, edges="edges")),
        encoding="utf-8",
    )
    return graph


def _rows(results: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(str(row["name"]), str(row["type"])) for row in results]


def test_recommendation_surfaces_share_order_type_and_normalized_score(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_dir = tmp_path / "claude"
    graph_path = claude_dir / "skill-wiki" / "graphify-out" / "graph.json"
    _write_golden_graph(graph_path)
    import ctx_config
    monkeypatch.setattr(
        ctx_config.cfg,
        "graph_semantic_cache_dir",
        tmp_path / "semantic-cache",
    )

    query = "fastapi python async review code docs"
    tags = query_to_tags(query)
    graph = load_graph(graph_path)
    direct = recommend_by_tags(graph, tags, top_n=3, query=query)

    monkeypatch.setattr(context_monitor, "CLAUDE_DIR", claude_dir)
    hook = context_monitor.graph_suggest(tags, top_k=3)

    toolbox = CtxCoreToolbox(
        wiki_dir=claude_dir / "skill-wiki",
        graph_path=graph_path,
    )
    toolbox_payload = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="golden",
                name="ctx__recommend_bundle",
                arguments={"query": query, "top_k": 3},
            )
        )
    )
    toolbox_results = toolbox_payload["results"]

    monkeypatch.setattr(ctx.api, "_default_toolbox", toolbox)
    public = ctx.api.recommend_bundle(query, top_k=3)

    expected = [
        ("fastapi-python-async", "skill"),
        ("fastapi-code-reviewer", "agent"),
        ("fastapi-docs", "mcp-server"),
    ]
    assert _rows(direct) == expected
    assert _rows(hook) == expected
    assert _rows(toolbox_results) == expected
    assert _rows(public) == expected

    for surface_results in (direct, hook, toolbox_results, public):
        assert surface_results[0]["normalized_score"] == 1.0
        assert all("normalized_score" in row for row in surface_results)


def test_hook_uses_shared_query_min_score_and_top_k(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_dir = tmp_path / "claude"
    graph_path = claude_dir / "skill-wiki" / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(context_monitor, "CLAUDE_DIR", claude_dir)
    import ctx_config
    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(
            recommendation_top_k=2,
            recommendation_min_normalized_score=0.75,
        ),
    )
    calls: dict[str, Any] = {}

    class FakeGraph:
        def number_of_nodes(self) -> int:
            return 1

    def fake_recommend_by_tags(graph: Any, tags: list[str], **kwargs: Any) -> list[dict]:
        calls["tags"] = tags
        calls.update(kwargs)
        return [{"name": "fastapi-pro", "type": "skill"}]

    monkeypatch.setitem(
        sys.modules,
        "ctx.core.graph.resolve_graph",
        type("FakeGraphModule", (), {"load_graph": staticmethod(lambda _path=None: FakeGraph())}),
    )
    monkeypatch.setitem(
        sys.modules,
        "ctx.core.resolve.recommendations",
        type("FakeRecommendModule", (), {"recommend_by_tags": staticmethod(fake_recommend_by_tags)}),
    )

    out = context_monitor.graph_suggest(["fastapi", "python"], top_k=99)

    assert out == [{"name": "fastapi-pro", "type": "skill"}]
    assert calls["tags"] == ["fastapi", "python"]
    assert calls["top_n"] == 2
    assert calls["query"] == "fastapi python"
    assert calls["min_normalized_score"] == 0.75
    assert calls["entity_types"] == ("skill", "agent", "mcp-server")
    assert calls["use_semantic_query"] is True


def test_generic_toolbox_enables_semantic_query_scoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_path = tmp_path / "skill-wiki" / "graphify-out" / "graph.json"
    _write_golden_graph(graph_path)
    calls: dict[str, Any] = {}

    def fake_recommend_by_tags(
        graph: Any,
        tags: list[str],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        calls["tags"] = tags
        calls.update(kwargs)
        return [
            {
                "name": "fastapi-python-async",
                "type": "skill",
                "score": 1.0,
                "normalized_score": 1.0,
            }
        ]

    monkeypatch.setitem(
        sys.modules,
        "ctx.core.resolve.recommendations",
        type(
            "FakeRecommendModule",
            (),
            {
                "query_to_tags": staticmethod(lambda query: query.split()),
                "recommend_by_tags": staticmethod(fake_recommend_by_tags),
            },
        ),
    )
    toolbox = CtxCoreToolbox(
        wiki_dir=tmp_path / "skill-wiki",
        graph_path=graph_path,
    )

    payload = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="semantic",
                name="ctx__recommend_bundle",
                arguments={"query": "fastapi python", "top_k": 3},
            )
        )
    )

    assert payload["results"][0]["name"] == "fastapi-python-async"
    assert calls["query"] == "fastapi python"
    assert calls["entity_types"] == ("skill", "agent", "mcp-server")
    assert calls["use_semantic_query"] is True


def test_scan_repo_recommendations_use_shared_graph_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "skill-wiki"
    graph_path = wiki / "graphify-out" / "graph.json"
    _write_golden_graph(graph_path)
    import ctx_config
    monkeypatch.setattr(
        ctx_config,
        "cfg",
        SimpleNamespace(
            wiki_dir=wiki,
            recommendation_top_k=5,
            recommendation_min_normalized_score=0.30,
        ),
    )
    profile = {
        "repo_path": str(tmp_path),
        "project_type": "api-service",
        "languages": [{"name": "python", "confidence": 0.9}],
        "frameworks": [{"name": "fastapi", "confidence": 0.9}],
        "infrastructure": [],
        "data_stores": [],
        "testing": [],
        "ai_tooling": [],
        "build_system": [],
        "docs": [],
    }

    scan_repo._print_recommendations(str(tmp_path), profile)
    out = capsys.readouterr().out

    assert "-- Skills (1) --" in out
    assert "-- Agents (1) --" in out
    assert "-- MCP Servers (1) --" in out
    assert "fastapi-python-async" in out
    assert "fastapi-code-reviewer" in out
    assert "fastapi-docs" in out


def test_resolver_preserves_graph_entity_type_and_normalized_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.core.resolve import resolve_skills

    class FakeGraph:
        def number_of_nodes(self) -> int:
            return 1

    def fake_hits(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "name": "top-advisor",
                "type": "agent",
                "score": 80.0,
                "normalized_score": 1.0,
                "shared_tags": ["fastapi"],
                "via": ["fastapi"],
            },
            {
                "name": "mid-skill",
                "type": "skill",
                "score": 60.0,
                "normalized_score": 0.5,
                "shared_tags": ["fastapi"],
                "via": ["fastapi"],
            },
        ]

    monkeypatch.setattr(resolve_skills, "_GRAPH_AVAILABLE", True)
    monkeypatch.setattr(resolve_skills, "_load_graph", lambda: FakeGraph())
    monkeypatch.setattr(resolve_skills, "_resolve_by_seeds", fake_hits)

    available = {
        "fastapi": {"path": str(tmp_path / "fastapi" / "SKILL.md"), "name": "fastapi"},
        "top-advisor": {
            "path": str(tmp_path / "top-advisor" / "SKILL.md"),
            "name": "top-advisor",
        },
        "mid-skill": {
            "path": str(tmp_path / "mid-skill" / "SKILL.md"),
            "name": "mid-skill",
        },
    }
    profile = {
        "repo_path": "/tmp/repo",
        "languages": [],
        "frameworks": [
            {"name": "fastapi", "confidence": 0.9, "evidence": ["main.py"]},
        ],
        "infrastructure": [],
        "data_stores": [],
        "testing": [],
        "ai_tooling": [],
        "build_system": [],
        "docs": [],
    }

    manifest = resolve(profile, available, {})
    by_name = {entry["skill"]: entry for entry in manifest["load"]}

    assert by_name["top-advisor"]["entity_type"] == "agent"
    assert by_name["top-advisor"]["type"] == "agent"
    assert by_name["top-advisor"]["priority"] == 15
    assert by_name["mid-skill"]["entity_type"] == "skill"
    assert by_name["mid-skill"]["type"] == "skill"
    assert by_name["mid-skill"]["priority"] == 9
