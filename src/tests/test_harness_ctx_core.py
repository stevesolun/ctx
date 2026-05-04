"""
test_harness_ctx_core.py -- CtxCoreToolbox integration with the harness.

Covers:
  * Tool-definition shapes the model will see.
  * Dispatcher routing + ctx__ namespace guard.
  * Each dispatcher's happy path + error paths against a synthetic
    wiki + graph built on tmp_path (no reliance on the real wiki).
  * Query tokenisation + stopword removal.
  * Integer argument clamping.
  * make_tool_executor composition (ctx-owned vs fallback).
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

from ctx.adapters.generic.ctx_core_tools import (
    CtxCoreToolbox,
    _clamp_int,
    _excerpt,
    _query_to_tags,
    make_tool_executor,
)
from ctx.adapters.generic.providers import ToolCall, ToolDefinition


# ── Helpers: build a synthetic wiki + graph for the toolbox ────────────────


def _build_synthetic_graph(tmp_path: Path) -> Path:
    """Write a minimal but valid graph.json under graphify-out/."""
    G = nx.Graph()
    G.graph["external_catalog_nodes"] = {"skills.sh": 1}
    G.graph["source_catalog_nodes"] = {"skills.sh": 1}
    G.add_node("skill:python-patterns", label="python-patterns", type="skill",
               tags=["python", "patterns"])
    G.add_node("skill:fastapi-pro", label="fastapi-pro", type="skill",
               tags=["python", "api", "web"])
    G.add_node("skill:django-pro", label="django-pro", type="skill",
               tags=["python", "web"])
    G.add_node("agent:code-reviewer", label="code-reviewer", type="agent",
               tags=["python", "review"])
    G.add_node("mcp-server:filesystem", label="filesystem", type="mcp-server",
               tags=["filesystem", "io"])
    G.add_edge("skill:python-patterns", "skill:fastapi-pro",
               weight=0.8, shared_tags=["python"])
    G.add_edge("skill:python-patterns", "agent:code-reviewer",
               weight=0.6, shared_tags=["python"])
    G.add_edge("skill:fastapi-pro", "skill:django-pro",
               weight=0.4, shared_tags=["python", "web"])

    out_dir = tmp_path / "graphify-out"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "graph.json"
    data = nx.node_link_data(G, edges="edges")
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _build_synthetic_wiki(tmp_path: Path) -> Path:
    """Create a tiny wiki with a few entity pages + converted stubs."""
    wiki = tmp_path / "wiki"
    skills = wiki / "entities" / "skills"
    agents = wiki / "entities" / "agents"
    mcps = wiki / "entities" / "mcp-servers" / "f"
    skills.mkdir(parents=True)
    agents.mkdir(parents=True)
    mcps.mkdir(parents=True)
    (skills / "python-patterns.md").write_text(
        "---\n"
        "name: python-patterns\n"
        "title: Python Patterns\n"
        "tags: [python, patterns]\n"
        "status: cataloged\n"
        "---\n"
        "# Python Patterns\n\n"
        "Idiomatic Python patterns and best practices.\n",
        encoding="utf-8",
    )
    (skills / "fastapi-pro.md").write_text(
        "---\n"
        "name: fastapi-pro\n"
        "title: FastAPI Pro\n"
        "tags: [python, api, web]\n"
        "status: cataloged\n"
        "---\n"
        "# FastAPI Pro\n\n"
        "Advanced FastAPI patterns for production.\n",
        encoding="utf-8",
    )
    (agents / "code-reviewer.md").write_text(
        "---\n"
        "name: code-reviewer\n"
        "title: Code Reviewer\n"
        "type: agent\n"
        "tags: [review, quality]\n"
        "status: cataloged\n"
        "---\n"
        "# Code Reviewer\n\n"
        "Reviews code for defects and quality risks.\n",
        encoding="utf-8",
    )
    (mcps / "filesystem.md").write_text(
        "---\n"
        "name: filesystem\n"
        "title: Filesystem MCP\n"
        "type: mcp-server\n"
        "tags: [filesystem, io]\n"
        "status: cataloged\n"
        "---\n"
        "# Filesystem MCP\n\n"
        "Filesystem tools for local files.\n",
        encoding="utf-8",
    )
    # Also a converted stub so wiki_query sees has_transformed=True.
    converted = wiki / "converted" / "python-patterns"
    converted.mkdir(parents=True)
    (converted / "SKILL.md").write_text("# body", encoding="utf-8")
    return wiki


@pytest.fixture()
def toolbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CtxCoreToolbox:
    """Toolbox pointed at a synthetic wiki + graph."""
    import ctx_config

    monkeypatch.setattr(
        ctx_config.cfg,
        "graph_semantic_cache_dir",
        tmp_path / "semantic-cache",
    )
    graph_path = _build_synthetic_graph(tmp_path)
    wiki_dir = _build_synthetic_wiki(tmp_path)
    return CtxCoreToolbox(wiki_dir=wiki_dir, graph_path=graph_path)


# ── Tool definitions ────────────────────────────────────────────────────


class TestToolDefinitions:
    def test_four_tools_exposed(self, toolbox: CtxCoreToolbox) -> None:
        defs = toolbox.tool_definitions()
        names = [d.name for d in defs]
        assert set(names) == {
            "ctx__recommend_bundle",
            "ctx__graph_query",
            "ctx__wiki_search",
            "ctx__wiki_get",
        }

    def test_all_are_tool_definitions(self, toolbox: CtxCoreToolbox) -> None:
        for td in toolbox.tool_definitions():
            assert isinstance(td, ToolDefinition)
            assert td.description  # non-empty
            assert td.parameters["type"] == "object"
            assert "properties" in td.parameters

    def test_recommend_requires_query(self, toolbox: CtxCoreToolbox) -> None:
        td = next(
            d for d in toolbox.tool_definitions()
            if d.name == "ctx__recommend_bundle"
        )
        assert td.parameters["required"] == ["query"]

    def test_graph_query_requires_seeds(self, toolbox: CtxCoreToolbox) -> None:
        td = next(
            d for d in toolbox.tool_definitions()
            if d.name == "ctx__graph_query"
        )
        assert td.parameters["required"] == ["seeds"]


# ── Namespace + dispatch ───────────────────────────────────────────────────


class TestDispatchRouting:
    def test_owns(self, toolbox: CtxCoreToolbox) -> None:
        assert toolbox.owns("ctx__recommend_bundle")
        assert toolbox.owns("ctx__anything")
        assert not toolbox.owns("fs__read_file")
        assert not toolbox.owns("no_separator")

    def test_dispatch_rejects_non_ctx_call(self, toolbox: CtxCoreToolbox) -> None:
        with pytest.raises(ValueError, match="non-ctx call"):
            toolbox.dispatch(ToolCall(id="c1", name="fs__read", arguments={}))

    def test_dispatch_unknown_tool(self, toolbox: CtxCoreToolbox) -> None:
        with pytest.raises(ValueError, match="unknown ctx-core tool"):
            toolbox.dispatch(ToolCall(id="c1", name="ctx__bogus", arguments={}))


# ── recommend_bundle ───────────────────────────────────────────────────────


class TestRecommendBundle:
    def test_happy_path_ranks_by_tag_overlap(
        self, toolbox: CtxCoreToolbox
    ) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__recommend_bundle",
                    arguments={"query": "python web api", "top_k": 5},
                )
            )
        )
        assert "error" not in result
        assert result["query"] == "python web api"
        assert "tags" in result
        # python + web + api should score fastapi-pro highly (3 tags match).
        names = [r["name"] for r in result["results"]]
        assert "fastapi-pro" in names

    def test_empty_query(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1", name="ctx__recommend_bundle",
                    arguments={"query": ""},
                )
            )
        )
        assert "error" in result

    def test_pure_stopwords_query(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__recommend_bundle",
                    arguments={"query": "the a an and"},
                )
            )
        )
        assert "error" in result

    def test_top_k_clamped(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__recommend_bundle",
                    arguments={"query": "python", "top_k": 999},
                )
            )
        )
        # top_k clamped to <= 50, and our graph has only 5 entities.
        assert len(result["results"]) <= 50

    def test_missing_graph_returns_empty(self, tmp_path: Path) -> None:
        toolbox = CtxCoreToolbox(
            graph_path=tmp_path / "does-not-exist.json",
            wiki_dir=tmp_path / "wiki",
        )
        result = json.loads(
            toolbox.dispatch(
                ToolCall(id="c1", name="ctx__recommend_bundle",
                         arguments={"query": "python"})
            )
        )
        assert "error" in result
        assert result["results"] == []


# ── graph_query ────────────────────────────────────────────────────────────


class TestGraphQuery:
    def test_happy_path(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__graph_query",
                    arguments={"seeds": ["python-patterns"], "top_n": 5},
                )
            )
        )
        assert "error" not in result
        assert result["seeds"] == ["python-patterns"]
        names = [r["name"] for r in result["results"]]
        # Direct neighbours: fastapi-pro + code-reviewer.
        assert "fastapi-pro" in names or "code-reviewer" in names

    def test_missing_seeds(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(id="c1", name="ctx__graph_query",
                         arguments={"seeds": []})
            )
        )
        assert "error" in result

    def test_seeds_not_list(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(id="c1", name="ctx__graph_query",
                         arguments={"seeds": "python-patterns"})
            )
        )
        assert "error" in result

    def test_max_hops_clamped(self, toolbox: CtxCoreToolbox) -> None:
        # max_hops clamps to 1..4; 100 gets capped.
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__graph_query",
                    arguments={
                        "seeds": ["python-patterns"],
                        "max_hops": 100,
                        "top_n": 5,
                    },
                )
            )
        )
        assert "error" not in result


# ── wiki_search ────────────────────────────────────────────────────────────


class TestWikiSearch:
    def test_happy_path(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1", name="ctx__wiki_search",
                    arguments={"query": "FastAPI patterns"},
                )
            )
        )
        assert "error" not in result
        slugs = [r["slug"] for r in result["results"]]
        # Either of our two pages could match — just confirm we got hits.
        assert len(slugs) >= 1

    def test_empty_query(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(id="c1", name="ctx__wiki_search",
                         arguments={"query": ""})
            )
        )
        assert "error" in result

    def test_result_shape(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1", name="ctx__wiki_search",
                    arguments={"query": "python"},
                )
            )
        )
        if result["results"]:
            row = result["results"][0]
            assert {
                "slug", "title", "entity_type", "wikilink",
                "excerpt", "tags", "status", "score",
            } <= set(row)

    def test_search_includes_agents_and_mcps(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1", name="ctx__wiki_search",
                    arguments={"query": "filesystem review", "top_n": 10},
                )
            )
        )

        by_slug = {row["slug"]: row for row in result["results"]}
        assert by_slug["code-reviewer"]["entity_type"] == "agent"
        assert by_slug["code-reviewer"]["wikilink"] == "[[entities/agents/code-reviewer]]"
        assert by_slug["filesystem"]["entity_type"] == "mcp-server"
        assert by_slug["filesystem"]["wikilink"] == "[[entities/mcp-servers/f/filesystem]]"


# ── wiki_get ───────────────────────────────────────────────────────────────


class TestWikiGet:
    def test_happy_path(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(id="c1", name="ctx__wiki_get",
                         arguments={"slug": "python-patterns"})
            )
        )
        assert "error" not in result
        assert result["slug"] == "python-patterns"
        assert "frontmatter" in result
        assert "body" in result
        assert "Python Patterns" in result["body"]

    def test_missing_slug(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(id="c1", name="ctx__wiki_get", arguments={})
            )
        )
        assert "error" in result

    def test_invalid_slug_rejected(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1", name="ctx__wiki_get",
                    arguments={"slug": "../../etc/passwd"},
                )
            )
        )
        assert "error" in result
        assert "invalid" in result["error"].lower()

    def test_nonexistent_slug(self, toolbox: CtxCoreToolbox) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(id="c1", name="ctx__wiki_get",
                         arguments={"slug": "does-not-exist"})
            )
        )
        assert "error" in result
        assert "looked_in" in result

    def test_entity_type_disambiguates_duplicate_slugs(self, tmp_path: Path) -> None:
        wiki = _build_synthetic_wiki(tmp_path)
        (wiki / "entities" / "skills" / "filesystem.md").write_text(
            "---\n"
            "name: filesystem\n"
            "title: Filesystem Skill\n"
            "type: skill\n"
            "tags: [skill]\n"
            "status: cataloged\n"
            "---\n"
            "# Filesystem Skill\n\n"
            "This is the skill page, not the MCP page.\n",
            encoding="utf-8",
        )
        toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=tmp_path / "missing.json")

        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__wiki_get",
                    arguments={"slug": "filesystem", "entity_type": "mcp-server"},
                )
            )
        )

        assert "error" not in result
        assert result["entity_type"] == "mcp-server"
        assert result["wikilink"] == "[[entities/mcp-servers/f/filesystem]]"
        assert "Filesystem MCP" in result["body"]


# ── _query_to_tags ────────────────────────────────────────────────────────


class TestQueryToTags:
    def test_basic_tokenisation(self) -> None:
        assert _query_to_tags("python web api") == ["python", "web", "api"]

    def test_stopwords_removed(self) -> None:
        out = _query_to_tags("how do I use the python api")
        assert "python" in out
        assert "the" not in out
        assert "how" not in out
        # Too short tokens also dropped: 'do', 'i'.
        assert "do" not in out

    def test_dedup_preserves_order(self) -> None:
        out = _query_to_tags("python python web api python")
        assert out == ["python", "web", "api"]

    def test_hyphens_and_underscores_preserved(self) -> None:
        out = _query_to_tags("react-native state-management my_lib")
        assert "react-native" in out
        assert "state-management" in out
        assert "my_lib" in out

    def test_case_normalised(self) -> None:
        assert _query_to_tags("PYTHON Web") == ["python", "web"]


# ── _clamp_int ────────────────────────────────────────────────────────────


class TestClampInt:
    def test_default(self) -> None:
        assert _clamp_int(None, default=5, lo=1, hi=50) == 5

    def test_in_range(self) -> None:
        assert _clamp_int(10, default=5, lo=1, hi=50) == 10

    def test_below_lo(self) -> None:
        assert _clamp_int(0, default=5, lo=1, hi=50) == 1

    def test_above_hi(self) -> None:
        assert _clamp_int(1000, default=5, lo=1, hi=50) == 50

    def test_invalid_string(self) -> None:
        assert _clamp_int("nope", default=5, lo=1, hi=50) == 5

    def test_string_number(self) -> None:
        assert _clamp_int("7", default=5, lo=1, hi=50) == 7


# ── _excerpt ──────────────────────────────────────────────────────────────


class TestExcerpt:
    def test_empty_body(self) -> None:
        assert _excerpt("", 50) == ""

    def test_skips_heading(self) -> None:
        body = "# Heading\n\nActual body text here.\n"
        assert _excerpt(body, 50) == "Actual body text here."

    def test_trims_to_length(self) -> None:
        body = "a" * 200
        out = _excerpt(body, 50)
        assert len(out) <= 50
        assert out.endswith("…")


# ── make_tool_executor composition ────────────────────────────────────────


class TestMakeToolExecutor:
    def test_ctx_call_routed_to_toolbox(self, toolbox: CtxCoreToolbox) -> None:
        def fallback(_call):
            raise AssertionError("fallback should not fire for ctx__ calls")

        exe = make_tool_executor(toolbox, fallback=fallback)
        out = exe(
            ToolCall(
                id="c1", name="ctx__recommend_bundle",
                arguments={"query": "python", "top_k": 3},
            )
        )
        data = json.loads(out)
        assert "results" in data

    def test_non_ctx_call_delegates_to_fallback(
        self, toolbox: CtxCoreToolbox
    ) -> None:
        calls = []

        def fallback(call):
            calls.append(call)
            return f"fallback-handled:{call.name}"

        exe = make_tool_executor(toolbox, fallback=fallback)
        out = exe(ToolCall(id="c1", name="fs__read_file", arguments={}))
        assert out == "fallback-handled:fs__read_file"
        assert calls and calls[0].name == "fs__read_file"

    def test_no_fallback_raises_on_non_ctx(
        self, toolbox: CtxCoreToolbox
    ) -> None:
        exe = make_tool_executor(toolbox, fallback=None)
        with pytest.raises(ValueError, match="no executor"):
            exe(ToolCall(id="c1", name="anything__else", arguments={}))
