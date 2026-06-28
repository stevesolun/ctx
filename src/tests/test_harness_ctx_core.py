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
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import networkx as nx
import pytest

from ctx.adapters.generic.ctx_core_tools import (
    CtxCoreToolbox,
    _clamp_int,
    _excerpt,
    _file_signature,
    _query_to_tags,
    make_tool_executor,
)
from ctx.adapters.generic.providers import ToolCall, ToolDefinition
from ctx.core.graph.graph_packs import write_base_pack, write_overlay_pack
from ctx.core.wiki.wiki_packs import write_wiki_base_pack, write_wiki_overlay_pack


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
    G.add_node(
        "skill:no-mistakes",
        label="no-mistakes",
        type="skill",
        tags=["git", "validation", "pre-commit", "ship", "workflow"],
        category="workflow",
        invoke_command='no-mistakes axi run --intent "<intent>"',
        security_review="external-gate",
    )
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
    return CtxCoreToolbox(
        wiki_dir=wiki_dir,
        graph_path=graph_path,
        lifecycle_dir=tmp_path / "runtime",
    )


def test_graph_cache_reloads_when_graph_json_changes(tmp_path: Path) -> None:
    graph_path = tmp_path / "graph.json"

    def write_graph(target: str) -> None:
        graph = nx.Graph()
        graph.add_node("skill:seed", label="seed", type="skill", tags=[])
        graph.add_node(f"skill:{target}", label=target, type="skill", tags=[])
        graph.add_edge("skill:seed", f"skill:{target}", weight=1.0)
        graph_path.write_text(
            json.dumps(nx.node_link_data(graph, edges="edges")),
            encoding="utf-8",
        )

    write_graph("old-target")
    toolbox = CtxCoreToolbox(wiki_dir=tmp_path / "wiki", graph_path=graph_path)
    first = json.loads(toolbox.dispatch(ToolCall(
        id="c1",
        name="ctx__graph_query",
        arguments={"seeds": ["seed"], "max_hops": 1},
    )))

    write_graph("new-target")
    second = json.loads(toolbox.dispatch(ToolCall(
        id="c2",
        name="ctx__graph_query",
        arguments={"seeds": ["seed"], "max_hops": 1},
    )))

    assert first["results"][0]["name"] == "old-target"
    assert second["results"][0]["name"] == "new-target"


def test_graph_cache_reloads_when_graph_pack_overlay_changes(tmp_path: Path) -> None:
    graph_dir = tmp_path / "graphify-out"
    graph_path = graph_dir / "graph.json"
    packs_dir = graph_dir / "packs"
    base = nx.Graph()
    base.add_node("skill:seed", label="seed", type="skill", tags=[])
    base.add_node("skill:old-target", label="old-target", type="skill", tags=[])
    base.add_edge("skill:seed", "skill:old-target", weight=1.0)
    write_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-1",
        model_id="model-1",
        graph=base,
    )
    toolbox = CtxCoreToolbox(wiki_dir=tmp_path / "wiki", graph_path=graph_path)

    first = json.loads(toolbox.dispatch(ToolCall(
        id="c1",
        name="ctx__graph_query",
        arguments={"seeds": ["seed"], "max_hops": 1},
    )))
    write_overlay_pack(
        pack_dir=packs_dir / "overlay-new-target",
        pack_id="overlay-new-target",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-1",
        model_id="model-1",
        nodes=[{"id": "skill:new-target", "label": "new-target", "type": "skill", "tags": []}],
        edges=[{"source": "skill:seed", "target": "skill:new-target", "weight": 1.0}],
        tombstones=[],
    )
    second = json.loads(toolbox.dispatch(ToolCall(
        id="c2",
        name="ctx__graph_query",
        arguments={"seeds": ["seed"], "max_hops": 1},
    )))

    first_names = {item["name"] for item in first["results"]}
    second_names = {item["name"] for item in second["results"]}
    assert "old-target" in first_names
    assert "new-target" not in first_names
    assert "new-target" in second_names


def test_graph_cache_uses_wiki_packs_when_explicit_graph_path_is_missing(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    graph = nx.Graph()
    graph.add_node("skill:seed", label="seed", type="skill", tags=[])
    graph.add_node("skill:pack-target", label="pack-target", type="skill", tags=[])
    graph.add_edge("skill:seed", "skill:pack-target", weight=1.0)
    write_base_pack(
        pack_dir=wiki / "graphify-out" / "packs" / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-1",
        model_id="model-1",
        graph=graph,
    )
    assert not (wiki / "graphify-out" / "graph.json").exists()

    toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=tmp_path / "missing.json")
    result = json.loads(toolbox.dispatch(ToolCall(
        id="c1",
        name="ctx__graph_query",
        arguments={"seeds": ["seed"], "max_hops": 1},
    )))

    assert [row["name"] for row in result["results"]] == ["pack-target"]


def test_graph_file_signature_detects_same_size_rewrite(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "graph.json"
    fixed_time_ns = 1_700_000_000_000_000_000

    graph_path.write_text('{"target":"old-target"}', encoding="utf-8")
    os.utime(graph_path, ns=(fixed_time_ns, fixed_time_ns))
    first = _file_signature(graph_path)

    graph_path.write_text('{"target":"new-target"}', encoding="utf-8")
    os.utime(graph_path, ns=(fixed_time_ns, fixed_time_ns))
    second = _file_signature(graph_path)

    assert graph_path.stat().st_size == len('{"target":"new-target"}')
    assert first is not None
    assert second is not None
    assert first[:2] == second[:2]
    assert first != second


def test_wiki_page_cache_reloads_when_entity_page_changes(tmp_path: Path) -> None:
    wiki = _build_synthetic_wiki(tmp_path)
    toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=tmp_path / "missing.json")
    first = json.loads(toolbox.dispatch(ToolCall(
        id="c1",
        name="ctx__wiki_search",
        arguments={"query": "newunique"},
    )))

    (wiki / "entities" / "skills" / "new-skill.md").write_text(
        "---\nname: new-skill\ntags: [newunique]\n---\n# New Skill\n",
        encoding="utf-8",
    )
    second = json.loads(toolbox.dispatch(ToolCall(
        id="c2",
        name="ctx__wiki_search",
        arguments={"query": "newunique"},
    )))

    assert first["results"] == []
    assert second["results"][0]["slug"] == "new-skill"


def test_wiki_page_cache_reloads_when_wiki_pack_overlay_changes(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    packs_dir = wiki / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"entities/skills/old-skill.md": "# Old\n"},
    )
    toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=tmp_path / "missing.json")
    first = json.loads(toolbox.dispatch(ToolCall(
        id="c1",
        name="ctx__wiki_search",
        arguments={"query": "packunique"},
    )))

    write_wiki_overlay_pack(
        pack_dir=packs_dir / "overlay-packunique",
        pack_id="overlay-packunique",
        base_export_id="wiki-export-1",
        parent_export_id="wiki-export-1",
        pages={
            "entities/skills/pack-skill.md": (
                "---\n"
                "name: pack-skill\n"
                "tags: [packunique]\n"
                "---\n"
                "# Pack Skill\n"
            ),
        },
        tombstones=[],
    )
    second = json.loads(toolbox.dispatch(ToolCall(
        id="c2",
        name="ctx__wiki_search",
        arguments={"query": "packunique"},
    )))

    assert first["results"] == []
    assert second["results"][0]["slug"] == "pack-skill"


def test_semantic_miss_cache_clears_when_embedding_artifacts_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctx_config
    from ctx.core.resolve import recommendations as rec

    wiki = tmp_path / "wiki"
    cache_dir = wiki / ".embedding-cache" / "graph"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(ctx_config.cfg, "graph_semantic_cache_dir", cache_dir)
    toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=tmp_path / "missing.json")
    graph = nx.Graph()

    toolbox._refresh_semantic_cache_signature()
    rec._semantic_cache[graph] = None
    (cache_dir / "topk-state.json").write_text("{}", encoding="utf-8")
    toolbox._refresh_semantic_cache_signature()

    assert len(rec._semantic_cache) == 0


# ── Tool definitions ────────────────────────────────────────────────────


class TestToolDefinitions:
    def test_ctx_tools_exposed(self, toolbox: CtxCoreToolbox) -> None:
        defs = toolbox.tool_definitions()
        names = [d.name for d in defs]
        assert set(names) == {
            "ctx__recommend_bundle",
            "ctx__graph_query",
            "ctx__wiki_search",
            "ctx__wiki_get",
            "ctx__observe_dev_event",
            "ctx__load_entity",
            "ctx__mark_entity_used",
            "ctx__record_validation",
            "ctx__record_escalation",
            "ctx__unload_entity",
            "ctx__session_end",
            "ctx__session_state",
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

    def test_read_tools_expose_optional_response_format(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        read_tools = {
            "ctx__recommend_bundle",
            "ctx__graph_query",
            "ctx__wiki_search",
            "ctx__wiki_get",
        }
        by_name = {definition.name: definition for definition in toolbox.tool_definitions()}

        for tool_name in read_tools:
            schema = by_name[tool_name].parameters
            output_format = schema["properties"]["output_format"]
            assert output_format["enum"] == ["json", "gcf"]
            assert "output_format" not in schema.get("required", [])
            response_format = schema["properties"]["_response_format"]
            assert response_format["enum"] == ["json", "gcf"]
            assert "_response_format" not in schema.get("required", [])


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

    def test_read_tools_default_to_json(self, toolbox: CtxCoreToolbox) -> None:
        raw = toolbox.dispatch(ToolCall(
            id="c1",
            name="ctx__wiki_search",
            arguments={"query": "python", "top_n": 1},
        ))

        payload = json.loads(raw)
        assert payload["query"] == "python"
        assert payload["results"]

    def test_read_tools_can_opt_into_gcf(
        self,
        toolbox: CtxCoreToolbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(
            sys.modules,
            "gcf",
            SimpleNamespace(
                encode_generic=lambda data: (
                    f"GCF profile=generic\nquery={data['query']}"
                )
            ),
        )

        raw = toolbox.dispatch(ToolCall(
            id="c1",
            name="ctx__wiki_search",
            arguments={
                "query": "python",
                "top_n": 1,
                "_response_format": "gcf",
            },
        ))

        assert raw.startswith("GCF profile=generic\n")
        assert "query=python" in raw

    def test_read_tools_accept_public_output_format_alias(
        self,
        toolbox: CtxCoreToolbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(
            sys.modules,
            "gcf",
            SimpleNamespace(
                encode_generic=lambda data: (
                    f"GCF profile=generic\nquery={data['query']}"
                )
            ),
        )

        raw = toolbox.dispatch(ToolCall(
            id="c1",
            name="ctx__wiki_search",
            arguments={
                "query": "python",
                "top_n": 1,
                "output_format": "gcf",
            },
        ))

        assert raw.startswith("GCF profile=generic\n")
        assert "query=python" in raw

    def test_gcf_opt_in_without_codec_returns_json_error(
        self,
        toolbox: CtxCoreToolbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "gcf", None)

        raw = toolbox.dispatch(ToolCall(
            id="c1",
            name="ctx__wiki_search",
            arguments={
                "query": "python",
                "top_n": 1,
                "_response_format": "gcf",
            },
        ))

        payload = json.loads(raw)
        assert "gcf-python" in payload["error"]
        assert payload["response_format"] == "json"


class TestRuntimeLifecycle:
    def test_lifecycle_tool_emits_core_and_store_telemetry(
        self,
        toolbox: CtxCoreToolbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctx.adapters.generic.ctx_core_tools as core_tools
        import ctx.adapters.generic.runtime_lifecycle as runtime_lifecycle

        events: list[dict[str, Any]] = []

        def capture_record_event(event_name: str, **kwargs: Any) -> None:
            events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(core_tools, "record_event", capture_record_event)
        monkeypatch.setattr(runtime_lifecycle, "record_event", capture_record_event)

        result = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__load_entity",
                arguments={
                    "session_id": "s-1",
                    "entity_type": "skill",
                    "slug": "fastapi-pro",
                },
            ))
        )

        assert result["ok"] is True
        event_names = [event["event_name"] for event in events]
        assert "ctx.runtime_lifecycle.record" in event_names
        assert "ctx.core.lifecycle" in event_names
        for event in events:
            payload = event["payload"]
            assert payload["otel.status_code"] == "OK"
            assert payload["ctx.entity.type"] == "skill"
            assert payload["ctx.slug.hash"].startswith("sha256:")
            assert "fastapi-pro" not in json.dumps(payload)

    def test_lifecycle_tools_append_events(
        self,
        toolbox: CtxCoreToolbox,
        tmp_path: Path,
    ) -> None:
        calls: list[tuple[str, dict[str, Any]]] = [
            ("ctx__observe_dev_event", {
                "session_id": "s-1",
                "event_type": "task",
                "payload": {"goal": "ship api"},
            }),
            ("ctx__load_entity", {
                "session_id": "s-1",
                "entity_type": "skill",
                "slug": "fastapi-pro",
            }),
            ("ctx__mark_entity_used", {
                "session_id": "s-1",
                "entity_type": "skill",
                "slug": "fastapi-pro",
                "evidence": "used in implementation",
            }),
            ("ctx__record_validation", {
                "session_id": "s-1",
                "check_name": "pytest",
                "status": "passed",
                "command": "python -m pytest",
                "summary": "all tests passed",
            }),
            ("ctx__record_escalation", {
                "session_id": "s-1",
                "trigger": "destructive-action",
                "reason": "delete requires user approval",
                "severity": "blocking",
            }),
            ("ctx__unload_entity", {
                "session_id": "s-1",
                "entity_type": "skill",
                "slug": "fastapi-pro",
                "reason": "not needed",
            }),
            ("ctx__session_end", {"session_id": "s-1", "status": "complete"}),
        ]

        for name, arguments in calls:
            result = json.loads(
                toolbox.dispatch(ToolCall(id="c1", name=name, arguments=arguments))
            )
            assert result["ok"] is True

        events = [
            json.loads(line)
            for line in (tmp_path / "runtime" / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "load_requested",
            "used",
            "validation",
            "escalation",
            "unload_requested",
            "session_end",
        ]

    def test_bound_session_id_is_hidden_and_enforced(self, tmp_path: Path) -> None:
        toolbox = CtxCoreToolbox(
            lifecycle_dir=tmp_path / "runtime",
            bound_session_id="host-session",
        )
        lifecycle_defs = [
            definition
            for definition in toolbox.tool_definitions()
            if definition.name.startswith("ctx__")
            and definition.name.rsplit("__", 1)[-1]
            in {
                "observe_dev_event",
                "load_entity",
                "mark_entity_used",
                "record_validation",
                "record_escalation",
                "unload_entity",
                "session_end",
                "session_state",
            }
        ]
        assert lifecycle_defs
        for definition in lifecycle_defs:
            assert "session_id" not in definition.parameters["properties"]
            assert "session_id" not in definition.parameters.get("required", [])

        loaded = json.loads(toolbox.dispatch(ToolCall(
            id="c1",
            name="ctx__load_entity",
            arguments={"entity_type": "skill", "slug": "fastapi-pro"},
        )))
        assert loaded["ok"] is True

        state = json.loads(toolbox.dispatch(ToolCall(
            id="c2",
            name="ctx__session_state",
            arguments={},
        )))
        assert state["ok"] is True
        assert state["session_id"] == "host-session"

        mismatch = json.loads(toolbox.dispatch(ToolCall(
            id="c3",
            name="ctx__session_state",
            arguments={"session_id": "attacker-session"},
        )))
        assert mismatch == {
            "ok": False,
            "error": "session_id is host-bound and cannot be overridden",
        }

    def test_lifecycle_validation_errors_are_structured(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        result = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__load_entity",
                arguments={
                    "session_id": "s-1",
                    "entity_type": "bogus",
                    "slug": "fastapi-pro",
                },
            ))
        )

        assert result["ok"] is False
        assert "entity_type" in result["error"]

    def test_skill_load_records_missing_security_scan_warning(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        result = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__load_entity",
                arguments={
                    "session_id": "s-scan",
                    "entity_type": "skill",
                    "slug": "fastapi-pro",
                },
            ))
        )

        assert result["ok"] is True
        assert result["event"]["security_scan"]["status"] == "not_provided"
        assert (
            result["event"]["security_scan"]["recommended_command"]
            == "ctx-skill-install fastapi-pro --security-scan-required"
        )

        state = json.loads(
            toolbox.dispatch(ToolCall(
                id="c2",
                name="ctx__session_state",
                arguments={"session_id": "s-scan"},
            ))
        )
        assert state["loaded"][0]["security_scan"]["status"] == "not_provided"

    def test_skill_load_accepts_security_scan_proof(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        result = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__load_entity",
                arguments={
                    "session_id": "s-scan-proof",
                    "entity_type": "skill",
                    "slug": "fastapi-pro",
                    "security_scan": {
                        "status": "passed",
                        "required": True,
                        "command": [
                            "skillspector",
                            "scan",
                            "fastapi-pro",
                            "--no-llm",
                        ],
                        "output": "clean",
                    },
                },
            ))
        )

        assert result["ok"] is True
        assert result["event"]["security_scan"] == {
            "status": "passed",
            "scanner": "skillspector",
            "required": True,
            "command": ["skillspector", "scan", "fastapi-pro", "--no-llm"],
            "output": "clean",
        }

        state = json.loads(
            toolbox.dispatch(ToolCall(
                id="c2",
                name="ctx__session_state",
                arguments={"session_id": "s-scan-proof"},
            ))
        )
        assert state["loaded"][0]["security_scan"]["status"] == "passed"

    def test_invalid_security_scan_status_is_structured(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        result = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__load_entity",
                arguments={
                    "session_id": "s-scan",
                    "entity_type": "skill",
                    "slug": "fastapi-pro",
                    "security_scan": {"status": "unknown"},
                },
            ))
        )

        assert result["ok"] is False
        assert "security_scan.status" in result["error"]

    def test_session_state_surfaces_unused_loads_as_unload_candidates(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        for name, arguments in [
            ("ctx__load_entity", {
                "session_id": "s-2",
                "entity_type": "skill",
                "slug": "fastapi-pro",
            }),
            ("ctx__load_entity", {
                "session_id": "s-2",
                "entity_type": "agent",
                "slug": "code-reviewer",
            }),
            ("ctx__mark_entity_used", {
                "session_id": "s-2",
                "entity_type": "agent",
                "slug": "code-reviewer",
                "evidence": "reviewed diff",
            }),
        ]:
            toolbox.dispatch(ToolCall(id="c1", name=name, arguments=arguments))

        result = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__session_state",
                arguments={"session_id": "s-2"},
            ))
        )

        assert result["ok"] is True
        assert [entry["slug"] for entry in result["used"]] == ["code-reviewer"]
        assert [entry["slug"] for entry in result["unload_candidates"]] == [
            "fastapi-pro",
        ]


# -- runtime validation ledger ----------------------------------------------


class TestRuntimeValidationLedger:
    def test_session_state_surfaces_validation_and_escalation_ledger(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        for name, arguments in [
            ("ctx__record_validation", {
                "session_id": "s-ledger",
                "check_name": "mypy",
                "status": "failed",
                "command": "python -m mypy src",
                "summary": "type gate failed",
                "payload": {"errors": 3},
            }),
            ("ctx__record_escalation", {
                "session_id": "s-ledger",
                "trigger": "validation-failed",
                "reason": "mypy failed after retry",
                "severity": "blocking",
                "payload": {"check_name": "mypy"},
            }),
        ]:
            result = json.loads(
                toolbox.dispatch(ToolCall(id="c1", name=name, arguments=arguments))
            )
            assert result["ok"] is True

        state = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__session_state",
                arguments={"session_id": "s-ledger"},
            ))
        )

        assert state["validations"] == [{
            "check_name": "mypy",
            "status": "failed",
            "command": "python -m mypy src",
            "summary": "type gate failed",
            "entity_type": None,
            "slug": None,
            "payload": {"errors": 3},
        }]
        assert state["escalations"] == [{
            "trigger": "validation-failed",
            "reason": "mypy failed after retry",
            "severity": "blocking",
            "status": "open",
            "entity_type": None,
            "slug": None,
            "payload": {"check_name": "mypy"},
        }]
        assert state["latest_validation_status"] == "failed"
        assert state["open_escalations"] == state["escalations"]

    def test_invalid_validation_status_is_structured(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        result = json.loads(
            toolbox.dispatch(ToolCall(
                id="c1",
                name="ctx__record_validation",
                arguments={
                    "session_id": "s-ledger",
                    "check_name": "pytest",
                    "status": "maybe",
                },
            ))
        )

        assert result["ok"] is False
        assert "status" in result["error"]


# -- recommend_bundle --------------------------------------------------------


def test_session_state_suppresses_current_dev_window_unloads(
    toolbox: CtxCoreToolbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.adapters.generic import runtime_lifecycle

    timestamps = iter([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    monkeypatch.setattr(runtime_lifecycle.time, "time", lambda: next(timestamps))

    for name, arguments in [
        ("ctx__observe_dev_event", {
            "session_id": "s-window",
            "event_type": "task",
        }),
        ("ctx__load_entity", {
            "session_id": "s-window",
            "entity_type": "skill",
            "slug": "fastapi-pro",
        }),
    ]:
        toolbox.dispatch(ToolCall(id="c1", name=name, arguments=arguments))

    current_window = json.loads(
        toolbox.dispatch(ToolCall(
            id="c1",
            name="ctx__session_state",
            arguments={"session_id": "s-window"},
        ))
    )
    assert current_window["unload_candidates"] == []

    for name, arguments in [
        ("ctx__session_end", {"session_id": "s-window"}),
        ("ctx__observe_dev_event", {
            "session_id": "s-window",
            "event_type": "resume",
        }),
    ]:
        toolbox.dispatch(ToolCall(id="c1", name=name, arguments=arguments))

    next_window = json.loads(
        toolbox.dispatch(ToolCall(
            id="c1",
            name="ctx__session_state",
            arguments={"session_id": "s-window"},
        ))
    )
    assert [entry["slug"] for entry in next_window["unload_candidates"]] == [
        "fastapi-pro",
    ]


class TestRecommendBundle:
    def test_recommend_bundle_emits_core_telemetry_without_raw_query(
        self,
        toolbox: CtxCoreToolbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctx.adapters.generic.ctx_core_tools as core_tools

        events: list[dict[str, Any]] = []

        def capture_record_event(event_name: str, **kwargs: Any) -> None:
            events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(core_tools, "record_event", capture_record_event)
        raw_query = "private acme python web api"

        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__recommend_bundle",
                    arguments={"query": raw_query, "top_k": 5},
                )
            )
        )

        event = events[-1]
        payload = event["payload"]
        assert event["event_name"] == "ctx.core.recommend_bundle"
        assert event["source"] == "ctx-core"
        assert event["outcome"] == "ok"
        assert payload["ctx.operation"] == "recommend_bundle"
        assert payload["ctx.query.length"] == len(raw_query)
        assert payload["ctx.query.hash"].startswith("sha256:")
        assert payload["ctx.result.count"] == len(result["results"])
        assert raw_query not in json.dumps(payload)

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

    def test_companion_harnesses_are_separate_from_dev_results(
        self,
        toolbox: CtxCoreToolbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctx_init

        calls: list[dict[str, Any]] = []

        def fake_recommend_harnesses(
            goal: str,
            *,
            top_k: int = 5,
            model_provider: str | None = None,
            model: str | None = None,
        ) -> list[dict[str, Any]]:
            calls.append({
                "goal": goal,
                "top_k": top_k,
                "model_provider": model_provider,
                "model": model,
            })
            return [{
                "name": "langgraph",
                "fit_score": 0.92,
                "normalized_score": 0.88,
                "matching_tags": ["agents"],
                "provider_match": "openai",
                "detail_url": "https://example.test/langgraph",
                "install_command": "ctx-harness-install langgraph",
            }]

        monkeypatch.setattr(ctx_init, "recommend_harnesses", fake_recommend_harnesses)

        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__recommend_bundle",
                    arguments={
                        "query": "python agent workflow",
                        "top_k": 5,
                        "model_provider": "openai",
                        "model": "openai/gpt-5.5",
                    },
                )
            )
        )

        assert calls == [{
            "goal": "python agent workflow",
            "top_k": 5,
            "model_provider": "openai",
            "model": "openai/gpt-5.5",
        }]
        assert all(row["type"] != "harness" for row in result["results"])
        assert result["companion_harnesses"] == [{
            "name": "langgraph",
            "type": "harness",
            "fit_score": 0.92,
            "normalized_score": 0.88,
            "matching_tags": ["agents"],
            "provider_match": "openai",
            "detail_url": "https://example.test/langgraph",
            "install_command": "ctx-harness-install langgraph",
        }]

    def test_companion_harnesses_can_be_empty(
        self,
        toolbox: CtxCoreToolbox,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctx_init

        monkeypatch.setattr(ctx_init, "recommend_harnesses", lambda *a, **kw: [])

        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__recommend_bundle",
                    arguments={
                        "query": "python web api",
                        "model_provider": "ollama",
                    },
                )
            )
        )

        assert result["companion_harnesses"] == []

    def test_workflow_action_metadata_survives_generic_recommendation(
        self,
        toolbox: CtxCoreToolbox,
    ) -> None:
        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__recommend_bundle",
                    arguments={
                        "query": "git validation pre commit ship safely",
                        "top_k": 5,
                    },
                )
            )
        )

        no_mistakes = next(
            row for row in result["results"] if row["name"] == "no-mistakes"
        )
        assert no_mistakes["category"] == "workflow"
        assert (
            no_mistakes["invoke_command"]
            == 'no-mistakes axi run --intent "<intent>"'
        )
        assert no_mistakes["security_review"] == "external-gate"

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

    def test_reads_entity_page_from_wiki_pack_before_stale_file(
        self, tmp_path: Path
    ) -> None:
        wiki = _build_synthetic_wiki(tmp_path)
        (wiki / "entities" / "skills" / "python-patterns.md").write_text(
            "---\n"
            "name: python-patterns\n"
            "title: Stale Physical Page\n"
            "tags: [stale]\n"
            "---\n"
            "# Stale Physical Page\n",
            encoding="utf-8",
        )
        write_wiki_base_pack(
            pack_dir=wiki / "wiki-packs" / "base-export-1",
            pack_id="base-export-1",
            base_export_id="export-1",
            pages={
                "entities/skills/python-patterns.md": (
                    "---\n"
                    "name: python-patterns\n"
                    "title: Fresh Pack Page\n"
                    "tags: [pack]\n"
                    "---\n"
                    "# Fresh Pack Page\n\n"
                    "This content came from the merged wiki pack.\n"
                )
            },
        )
        toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=tmp_path / "missing.json")

        result = json.loads(
            toolbox.dispatch(
                ToolCall(
                    id="c1",
                    name="ctx__wiki_get",
                    arguments={"slug": "python-patterns", "entity_type": "skill"},
                )
            )
        )

        assert "error" not in result
        assert result["frontmatter"]["title"] == "Fresh Pack Page"
        assert "merged wiki pack" in result["body"]


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
