"""Regression tests for the LoopFlow / agent-loop adapter."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

import pytest
import ctx.api as ctx_api
from ctx.adapters import loopflow


_EXPECTED_READ_ONLY_MCP_TOOL_NAMES = [
    "ctx__recommend_bundle",
    "ctx__graph_query",
    "ctx__recommend_related",
    "ctx__wiki_search",
    "ctx__wiki_get",
]


def _expected_scoped_mcp_args(*entity_types: str) -> list[str]:
    return [
        "--allow-tools",
        ",".join(_EXPECTED_READ_ONLY_MCP_TOOL_NAMES),
        "--entity-types",
        ",".join(entity_types),
    ]


class _FakeGraph:
    def number_of_nodes(self) -> int:
        return 10


def test_parse_loop_file_reads_loopflow_context(tmp_path: Path) -> None:
    loop_file = tmp_path / "rate-limit.loop"
    loop_file.write_text(
        "\n".join(
            [
                'loop "add API rate limiting":',
                "  goal: requests are rate-limited per API key",
                '  done when "pnpm test rate-limit" passes',
                "  look at: the API, middleware, and the last failure",
                "  ctx grants: skills, mcp",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    parsed = loopflow.parse_loop_file(loop_file)

    assert parsed["name"] == "add API rate limiting"
    assert parsed["goal"] == "requests are rate-limited per API key"
    assert parsed["look_at"] == ["the API", "middleware", "and the last failure"]
    assert parsed["done_when"] == ['"pnpm test rate-limit" passes']
    assert parsed["permissions"] == ["skills", "mcps"]


def test_recommend_for_loop_respects_capability_permissions(
    monkeypatch,
) -> None:
    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        assert "checkout e2e" in query
        assert permissions == {"skills", "mcps"}
        assert top_k == 9
        return [
            {"name": "playwright-debug", "type": "skill", "score": 91},
            {"name": "browser-agent", "type": "agent", "score": 85},
            {"name": "filesystem", "type": "mcp-server", "score": 80},
        ]

    def fake_recommend_related(
        selected: list[str],
        *,
        rejected: list[str] | None = None,
        max_hops: int = 2,
        top_n: int = 5,
    ) -> list[dict[str, Any]]:
        assert selected == ["skill:playwright-debug"]
        assert rejected == ["mcp-server:filesystem"]
        assert max_hops == 2
        assert top_n == 50
        return [
            {
                "id": "agent:browser-agent",
                "name": "browser-agent",
                "type": "agent",
                "reason": "filtered by permissions",
                "selection_state": "suggested_related",
            },
            {
                "id": "agent:browser-helper",
                "name": "browser-helper",
                "type": "agent",
                "reason": "filtered by permissions",
                "selection_state": "suggested_related",
            },
            {
                "id": "skill:browser-test-plan",
                "name": "browser-test-plan",
                "type": "skill",
                "reason": "related via playwright-debug",
                "selection_state": "suggested_related",
            },
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow.ctx_api, "recommend_related", fake_recommend_related)

    payload = loopflow.recommend_for_loop(
        goal="fix checkout e2e",
        loop_kind="agent-loop",
        permissions={"skills", "mcps"},
        selected=["skill:playwright-debug"],
        rejected=["mcp-server:filesystem"],
        top_k=2,
    )

    assert payload["adapter"] == "agent-loop"
    assert payload["permissions"] == {
        "skills": True,
        "agents": False,
        "mcps": True,
        "harnesses": False,
    }
    assert payload["capabilities"]["skills"] == []
    assert payload["capabilities"]["agents"] == []
    assert payload["capabilities"]["mcps"] == []
    assert payload["related_recommendations"] == [
        {
            "id": "skill:browser-test-plan",
            "name": "browser-test-plan",
            "type": "skill",
            "reason": "related via playwright-debug",
            "selection_state": "suggested_related",
        }
    ]
    assert payload["mcp_server"] == {
        "name": "ctx",
        "command": "ctx-mcp-server",
        "args": _expected_scoped_mcp_args("skill", "mcp-server"),
        "tools": _EXPECTED_READ_ONLY_MCP_TOOL_NAMES,
    }


def test_loopflow_excludes_bare_selection_names_and_backfills(monkeypatch) -> None:
    calls: list[int] = []

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, permissions
        calls.append(top_k)
        return [
            {"id": "skill:fastapi-pro", "name": "fastapi-pro", "type": "skill", "score": 99},
            {
                "id": "skill:python-patterns",
                "name": "python-patterns",
                "type": "skill",
                "score": 80,
            },
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow.ctx_api, "recommend_related", lambda *args, **kwargs: [])

    payload = loopflow.recommend_for_loop(
        goal="python api",
        permissions={"skills"},
        selected=["fastapi-pro"],
        top_k=1,
    )

    assert calls == [7]
    assert payload["capabilities"]["skills"] == [
        {"id": "skill:python-patterns", "name": "python-patterns", "type": "skill", "score": 80}
    ]


def test_mcp_server_tools_are_filtered_by_permission_groups(monkeypatch) -> None:
    monkeypatch.setattr(loopflow, "_recommend_capability_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    mcps_only = loopflow.recommend_for_loop(
        goal="recommend only mcp servers",
        permissions={"mcps"},
    )
    assert mcps_only["mcp_server"] == {
        "name": "ctx",
        "command": "ctx-mcp-server",
        "args": _expected_scoped_mcp_args("mcp-server"),
        "tools": _EXPECTED_READ_ONLY_MCP_TOOL_NAMES,
    }

    core_recommendations = loopflow.recommend_for_loop(
        goal="recommend core capabilities",
        permissions={"skills", "agents", "mcps"},
    )
    assert core_recommendations["mcp_server"] == {
        "name": "ctx",
        "command": "ctx-mcp-server",
        "args": _expected_scoped_mcp_args("skill", "agent", "mcp-server"),
        "tools": _EXPECTED_READ_ONLY_MCP_TOOL_NAMES,
    }
    assert not {
        "ctx__observe_dev_event",
        "ctx__load_entity",
        "ctx__mark_entity_used",
        "ctx__record_validation",
        "ctx__record_escalation",
        "ctx__unload_entity",
        "ctx__session_end",
        "ctx__session_state",
    }.intersection(core_recommendations["mcp_server"]["tools"])

    all_grants = loopflow.recommend_for_loop(
        goal="recommend every capability",
        permissions={"skills", "agents", "mcps", "harnesses"},
    )
    assert all_grants["mcp_server"]["command"] == "ctx-mcp-server"
    assert all_grants["mcp_server"]["args"] == []
    expected_tool_names = ctx_api.ctx_core_tool_names()
    assert all_grants["mcp_server"]["tools"] == expected_tool_names
    assert {
        "ctx__load_entity",
        "ctx__record_validation",
        "ctx__session_state",
    } <= set(all_grants["mcp_server"]["tools"])


def test_missing_and_empty_permissions_stay_empty(monkeypatch) -> None:
    def fail_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        raise AssertionError("_recommend_capability_rows should not run without grants")

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fail_recommend_rows)

    for payload in (
        loopflow.recommend_for_loop(goal="deny all recommendations"),
        loopflow.recommend_for_loop(goal="deny all recommendations", permissions=set()),
    ):
        assert payload["permissions"] == {
            "skills": False,
            "agents": False,
            "mcps": False,
            "harnesses": False,
        }
        assert payload["capabilities"] == {
            "skills": [],
            "agents": [],
            "mcps": [],
            "harnesses": [],
        }
        assert payload["loopflow"]["use_tools"] is None
        assert payload["loopflow"]["use_skills"] is None
        assert payload["mcp_server"] == {
            "name": "ctx",
            "command": None,
            "args": [],
            "tools": [],
        }


def test_loopflow_skill_hint_requires_skills_permission(monkeypatch) -> None:
    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        return [
            {"name": "security-review", "type": "skill"},
            {"name": "filesystem", "type": "mcp-server"},
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="recommend only tools",
        permissions={"mcps"},
    )

    assert payload["permissions"]["skills"] is False
    assert payload["capabilities"]["skills"] == []
    assert [row["name"] for row in payload["capabilities"]["mcps"]] == ["filesystem"]
    assert payload["loopflow"]["use_tools"] == 'use tools from the "ctx" server'
    assert payload["loopflow"]["use_skills"] is None
    assert payload["mcp_server"] == {
        "name": "ctx",
        "command": "ctx-mcp-server",
        "args": _expected_scoped_mcp_args("mcp-server"),
        "tools": _EXPECTED_READ_ONLY_MCP_TOOL_NAMES,
    }


def test_loopflow_tool_hint_requires_mcps_permission(monkeypatch) -> None:
    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        return [
            {"name": "security-review", "type": "skill"},
            {"name": "filesystem", "type": "mcp-server"},
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="recommend only skills",
        permissions={"skills"},
    )

    assert payload["permissions"]["mcps"] is False
    assert [row["name"] for row in payload["capabilities"]["skills"]] == ["security-review"]
    assert payload["capabilities"]["mcps"] == []
    assert payload["loopflow"]["use_tools"] is None
    assert payload["loopflow"]["use_skills"] == "use skills: security-review"
    assert payload["mcp_server"] == {
        "name": "ctx",
        "command": None,
        "args": [],
        "tools": [],
    }


def test_loopflow_skill_hint_excludes_installable_catalog_skills(monkeypatch) -> None:
    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, permissions, top_k
        return [
            {
                "name": "remote-security",
                "type": "skill",
                "status": "available",
                "source_catalog": "skill-index",
                "install_command": "ctx-skill-install remote-security",
                "detail_url": "https://example.test/remote-security",
                "score": 92,
            },
            {"name": "security-review", "type": "skill", "status": "installed", "score": 88},
            {"name": "remote-tests", "type": "skill", "status": "available", "score": 70},
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="review auth changes",
        permissions={"skills"},
    )

    assert payload["capabilities"]["skills"] == [
        {
            "name": "remote-security",
            "type": "skill",
            "score": 92,
            "source_catalog": "skill-index",
            "status": "available",
            "detail_url": "https://example.test/remote-security",
            "install_command": "ctx-skill-install remote-security",
        },
        {"name": "security-review", "type": "skill", "score": 88, "status": "installed"},
        {"name": "remote-tests", "type": "skill", "score": 70, "status": "available"},
    ]
    assert payload["loopflow"]["use_skills"] == "use skills: security-review"


def test_loopflow_local_no_key_loop_hides_non_loadable_skill_recommendations(
    monkeypatch,
) -> None:
    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, permissions, top_k
        return [
            {
                "name": "remote-api-planner",
                "type": "skill",
                "status": "available",
                "source_catalog": "skill-index",
                "install_command": "npx skills add remote-api-planner",
                "score": 92,
            },
            {
                "name": "local-javascript-helper",
                "type": "skill",
                "installable": True,
                "load_status": "local-wiki",
                "score": 88,
            },
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="LoCoBench javascript feature_implementation. No local API keys. Need local files.",
        permissions={"skills"},
    )

    assert payload["capabilities"]["skills"] == [
        {
            "name": "local-javascript-helper",
            "type": "skill",
            "score": 88,
            "installable": True,
            "load_status": "local-wiki",
        }
    ]
    assert payload["loopflow"]["use_skills"] == "use skills: local-javascript-helper"


def test_loopflow_local_filter_uses_enriched_wiki_availability(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki = tmp_path / "wiki"
    skills = wiki / "entities" / "skills"
    skills.mkdir(parents=True)
    (skills / "cataloged-only.md").write_text(
        "---\nname: cataloged-only\ntags: [python]\n---\n# Cataloged\n",
        encoding="utf-8",
    )
    converted = wiki / "converted" / "local-helper"
    converted.mkdir(parents=True)
    (converted / "SKILL.md").write_text("# Local helper\n", encoding="utf-8")

    monkeypatch.setattr(loopflow, "query_to_tags", lambda query: ["python"])
    monkeypatch.setattr(loopflow, "_recommendation_graph", lambda: _FakeGraph())
    monkeypatch.setattr(loopflow.ctx_api, "default_wiki_dir", lambda: wiki)

    def fake_recommend_by_tags(
        graph: Any,
        tags: list[str],
        *,
        top_n: int,
        query: str | None,
        entity_types: tuple[str, ...] | set[str] | None,
        min_normalized_score: float,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del graph, tags, query, min_normalized_score, kwargs
        assert top_n == 6
        assert entity_types == ("skill",)
        return [
            {"name": "cataloged-only", "type": "skill", "score": 91},
            {"name": "local-helper", "type": "skill", "score": 80},
        ]

    monkeypatch.setattr(loopflow, "recommend_by_tags", fake_recommend_by_tags)

    payload = loopflow.recommend_for_loop(
        goal="LoCoBench python feature_implementation. No local API keys. Need local files.",
        permissions={"skills"},
        top_k=1,
    )

    assert payload["capabilities"]["skills"] == [
        {
            "name": "local-helper",
            "type": "skill",
            "score": 80,
            "installable": True,
            "load_status": "local-wiki",
            "source_path": "converted/local-helper/SKILL.md",
        }
    ]


def test_loopflow_skill_hint_requires_returned_skill_capabilities(monkeypatch) -> None:
    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, permissions, top_k
        return [{"name": "filesystem", "type": "mcp-server"}]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="recommend only mcp servers",
        permissions={"skills", "mcps"},
    )

    assert payload["capabilities"]["skills"] == []
    assert payload["capabilities"]["mcps"] == [{"name": "filesystem", "type": "mcp-server"}]
    assert payload["loopflow"]["use_skills"] is None


def test_mcps_only_uses_type_filtered_recommendations(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(loopflow, "query_to_tags", lambda query: ["python"])
    monkeypatch.setattr(loopflow, "_recommendation_graph", lambda: _FakeGraph())

    def fake_recommend_by_tags(
        graph: Any,
        tags: list[str],
        *,
        top_n: int,
        query: str | None,
        entity_types: tuple[str, ...] | set[str] | None,
        min_normalized_score: float,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del graph, tags, query, min_normalized_score, kwargs
        calls.append(tuple(entity_types or ()))
        assert top_n == 1
        if entity_types == ("mcp-server",):
            return [{"name": "filesystem", "type": "mcp-server", "score": 80}]
        return [{"name": f"skill-{index}", "type": "skill"} for index in range(5)]

    monkeypatch.setattr(loopflow, "recommend_by_tags", fake_recommend_by_tags)

    payload = loopflow.recommend_for_loop(
        goal="python task",
        permissions={"mcps"},
        top_k=1,
    )

    assert calls == [("mcp-server",)]
    assert payload["capabilities"]["skills"] == []
    assert [row["name"] for row in payload["capabilities"]["mcps"]] == ["filesystem"]


def test_agents_only_uses_type_filtered_recommendations(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(loopflow, "query_to_tags", lambda query: ["python"])
    monkeypatch.setattr(loopflow, "_recommendation_graph", lambda: _FakeGraph())

    def fake_recommend_by_tags(
        graph: Any,
        tags: list[str],
        *,
        top_n: int,
        query: str | None,
        entity_types: tuple[str, ...] | set[str] | None,
        min_normalized_score: float,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del graph, tags, query, min_normalized_score, kwargs
        calls.append(tuple(entity_types or ()))
        assert top_n == 2
        if entity_types == ("agent",):
            return [{"name": "browser-agent", "type": "agent", "score": 78}]
        return [{"name": f"skill-{index}", "type": "skill"} for index in range(5)]

    monkeypatch.setattr(loopflow, "recommend_by_tags", fake_recommend_by_tags)

    payload = loopflow.recommend_for_loop(
        goal="python task",
        permissions={"agents"},
        top_k=2,
    )

    assert calls == [("agent",)]
    assert payload["capabilities"]["skills"] == []
    assert [row["name"] for row in payload["capabilities"]["agents"]] == ["browser-agent"]


def test_multi_grant_recommendations_use_single_combined_graph_call(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(loopflow, "query_to_tags", lambda query: ["python"])
    monkeypatch.setattr(loopflow, "_recommendation_graph", lambda: _FakeGraph())

    def fake_recommend_by_tags(
        graph: Any,
        tags: list[str],
        *,
        top_n: int,
        query: str | None,
        entity_types: tuple[str, ...] | set[str] | None,
        min_normalized_score: float,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del graph, tags, query, min_normalized_score, kwargs
        calls.append(tuple(entity_types or ()))
        assert top_n == 6
        assert entity_types == ("skill", "agent", "mcp-server")
        return [
            {"name": "security-review", "type": "skill", "score": 92},
            {"name": "browser-agent", "type": "agent", "score": 88},
            {"name": "filesystem", "type": "mcp-server", "score": 84},
        ]

    monkeypatch.setattr(loopflow, "recommend_by_tags", fake_recommend_by_tags)

    payload = loopflow.recommend_for_loop(
        goal="python task",
        permissions={"skills", "agents", "mcps"},
        top_k=2,
    )

    assert calls == [("skill", "agent", "mcp-server")]
    assert [row["name"] for row in payload["capabilities"]["skills"]] == ["security-review"]
    assert [row["name"] for row in payload["capabilities"]["agents"]] == ["browser-agent"]
    assert [row["name"] for row in payload["capabilities"]["mcps"]] == ["filesystem"]


def test_done_when_signals_feed_recommendation_queries(monkeypatch) -> None:
    capability_queries: list[str] = []
    harness_queries: list[str] = []

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del permissions, top_k
        capability_queries.append(query)
        return []

    def fake_recommend_harnesses(
        goal: str,
        *,
        top_k: int,
        model_provider: str | None = None,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        del top_k, model_provider, model
        harness_queries.append(goal)
        return [{"name": "local-agent-loop", "type": "harness"}]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow, "recommend_harnesses", fake_recommend_harnesses)

    payload = loopflow.recommend_for_loop(
        goal="fix checkout e2e",
        permissions={"skills", "harnesses"},
        own_llm=True,
        model_provider="ollama",
        model="llama3.1",
        done_when=[
            '"pytest src/tests/test_loopflow_adapter.py -q" passes',
            "pnpm lint passes",
        ],
        harness_requirements={"verification": "playwright smoke"},
    )

    assert payload["context"]["done_when"] == [
        '"pytest src/tests/test_loopflow_adapter.py -q" passes',
        "pnpm lint passes",
    ]
    assert (
        'done when: "pytest src/tests/test_loopflow_adapter.py -q" passes, pnpm lint passes'
        in payload["context"]["query"]
    )
    assert capability_queries == [payload["context"]["query"]]
    assert (
        'done when: "pytest src/tests/test_loopflow_adapter.py -q" passes, pnpm lint passes'
        in harness_queries[0]
    )
    assert "playwright smoke" in harness_queries[0]
    assert "ollama llama3.1 harness" in harness_queries[0]


def test_api_helpers_reuse_cached_toolbox(monkeypatch) -> None:
    constructions = 0
    graph_loads = 0
    graph = _FakeGraph()

    class _FakeToolbox:
        def __init__(self) -> None:
            nonlocal constructions
            constructions += 1

        def tool_definitions(self) -> list[Any]:
            return [type("_ToolDefinition", (), {"name": "ctx__recommend_bundle"})()]

        def _ensure_graph(self) -> _FakeGraph:
            nonlocal graph_loads
            graph_loads += 1
            return graph

    monkeypatch.setattr(ctx_api, "CtxCoreToolbox", _FakeToolbox)
    monkeypatch.setattr(ctx_api, "_default_toolbox", None)
    try:
        assert ctx_api.ctx_core_tool_names() == ["ctx__recommend_bundle"]
        assert ctx_api.recommendation_graph() is graph
        assert ctx_api.recommendation_graph() is graph
    finally:
        ctx_api._default_toolbox = None

    assert constructions == 1
    assert graph_loads == 2


def test_harnesses_require_user_owned_llm(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_recommend_harnesses(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append({"args": args, "kwargs": kwargs})
        return [{"name": "local-agent-loop", "type": "harness", "fit_score": 0.9}]

    monkeypatch.setattr(
        loopflow,
        "_recommend_capability_rows",
        lambda query, *, permissions, top_k: (_ for _ in ()).throw(
            AssertionError("_recommend_capability_rows should not run for harness-only grants")
        ),
    )
    monkeypatch.setattr(loopflow, "recommend_harnesses", fake_recommend_harnesses)

    blocked = loopflow.recommend_for_loop(
        goal="run with a private model",
        permissions={"harnesses"},
    )
    assert blocked["capabilities"]["harnesses"] == []
    assert blocked["warnings"] == [
        "harnesses permission granted but --own-llm/user-owned model consent was not declared"
    ]
    assert calls == []

    metadata_only = loopflow.recommend_for_loop(
        goal="run with a private model",
        permissions={"harnesses"},
        model_provider="ollama",
        model="llama3.1",
    )
    assert metadata_only["capabilities"]["harnesses"] == []
    assert metadata_only["warnings"] == [
        "harnesses permission granted but --own-llm/user-owned model consent was not declared"
    ]
    assert calls == []

    allowed = loopflow.recommend_for_loop(
        goal="run with a private model",
        permissions={"harnesses"},
        own_llm=True,
        model_provider="ollama",
        model="llama3.1",
        harness_requirements={"runtime": "local workstation"},
    )
    assert calls == [
        {
            "args": ("run with a private model local workstation ollama llama3.1 harness",),
            "kwargs": {
                "top_k": 5,
                "model_provider": "ollama",
                "model": "llama3.1",
            },
        }
    ]
    assert allowed["capabilities"]["harnesses"][0]["name"] == "local-agent-loop"
    assert shlex.split(allowed["agent_loop"]["harness_install"]) == [
        "ctx-harness-install",
        "--dry-run",
        "--goal=run with a private model",
        "--model-provider=ollama",
        "--model=llama3.1",
        "--harness-runtime=local workstation",
        "--",
        "local-agent-loop",
    ]


def test_harness_install_command_is_shell_quoted(monkeypatch) -> None:
    def fake_recommend_harnesses(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"name": "-local $(touch bad)", "type": "harness", "fit_score": 0.9}]

    monkeypatch.setattr(
        loopflow,
        "_recommend_capability_rows",
        lambda query, *, permissions, top_k: (_ for _ in ()).throw(
            AssertionError("_recommend_capability_rows should not run for harness-only grants")
        ),
    )
    monkeypatch.setattr(loopflow, "recommend_harnesses", fake_recommend_harnesses)

    payload = loopflow.recommend_for_loop(
        goal="-run $(touch bad)",
        permissions={"harnesses"},
        own_llm=True,
        model_provider="-open`whoami`",
        model="-llama; rm -rf .",
        harness_requirements={
            "runtime": "-local $(touch bad)",
            "api_key_env": "-OPENAI_API_KEY",
        },
    )

    command = payload["agent_loop"]["harness_install"]

    assert command.startswith("ctx-harness-install --dry-run")
    assert command.endswith("-- '-local $(touch bad)'")
    assert shlex.split(command) == [
        "ctx-harness-install",
        "--dry-run",
        "--goal=-run $(touch bad)",
        "--model-provider=-open`whoami`",
        "--model=-llama; rm -rf .",
        "--harness-runtime=-local $(touch bad)",
        "--api-key-env=-OPENAI_API_KEY",
        "--",
        "-local $(touch bad)",
    ]


def test_unknown_harness_requirements_warn_without_crashing(monkeypatch) -> None:
    monkeypatch.setattr(
        loopflow,
        "_recommend_capability_rows",
        lambda query, *, permissions, top_k: (_ for _ in ()).throw(
            AssertionError("_recommend_capability_rows should not run for harness-only grants")
        ),
    )
    monkeypatch.setattr(
        loopflow,
        "recommend_harnesses",
        lambda *args, **kwargs: [{"name": "local-agent-loop", "type": "harness"}],
    )

    payload = loopflow.recommend_for_loop(
        goal="run with a private model",
        permissions={"harnesses"},
        own_llm=True,
        harness_requirements={
            "runtime": "local workstation",
            "unknown": "ignored",
        },
    )

    assert payload["warnings"] == ["ignored unknown harness requirement(s): unknown"]
    assert shlex.split(payload["agent_loop"]["harness_install"]) == [
        "ctx-harness-install",
        "--dry-run",
        "--goal=run with a private model",
        "--harness-runtime=local workstation",
        "--",
        "local-agent-loop",
    ]


def test_main_api_key_env_reaches_harness_install(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        loopflow,
        "_recommend_capability_rows",
        lambda query, *, permissions, top_k: (_ for _ in ()).throw(
            AssertionError("_recommend_capability_rows should not run for harness-only grants")
        ),
    )
    monkeypatch.setattr(
        loopflow,
        "recommend_harnesses",
        lambda *args, **kwargs: [{"name": "remote-agent-loop", "type": "harness"}],
    )

    assert (
        loopflow.main(
            [
                "--goal",
                "run remote loop",
                "--permissions",
                "harnesses",
                "--own-llm",
                "--model-provider",
                "openai",
                "--model",
                "gpt-4o",
                "--api-key-env",
                "OPENAI_API_KEY",
                "--compact",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert shlex.split(payload["agent_loop"]["harness_install"]) == [
        "ctx-harness-install",
        "--dry-run",
        "--goal=run remote loop",
        "--model-provider=openai",
        "--model=gpt-4o",
        "--api-key-env=OPENAI_API_KEY",
        "--",
        "remote-agent-loop",
    ]


def test_main_emits_json_from_loop_file(tmp_path: Path, monkeypatch, capsys) -> None:
    loop_file = tmp_path / "review.loop"
    failure_file = tmp_path / "failure.txt"
    loop_file.write_text(
        "\n".join(
            [
                'loop "review upload":',
                "  goal: no high-severity upload findings",
                "  look at: upload.py, tests/upload_test.py",
                '  done when "pytest tests/upload_test.py -q" passes',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    failure_file.write_text("semgrep found upload risk", encoding="utf-8")

    capability_queries: list[str] = []

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del permissions, top_k
        capability_queries.append(query)
        return [{"name": "security-review", "type": "skill"}]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    assert (
        loopflow.main(
            [
                "--loop-file",
                str(loop_file),
                "--last-failure-file",
                str(failure_file),
                "--permissions",
                "skills,agents,mcps",
                "--compact",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert payload["context"]["goal"] == "no high-severity upload findings"
    assert payload["context"]["done_when"] == ['"pytest tests/upload_test.py -q" passes']
    assert '"pytest tests/upload_test.py -q" passes' in payload["context"]["query"]
    assert capability_queries == [
        "no high-severity upload findings review upload loopflow "
        "context: upload.py, tests/upload_test.py "
        'done when: "pytest tests/upload_test.py -q" passes '
        "last failure: semgrep found upload risk"
    ]
    assert "semgrep found upload risk" not in payload["context"]["query"]
    assert "semgrep found upload risk" not in serialized_payload
    assert payload["context"]["last_failure_present"] is True
    assert "python -m ctx.adapters.loopflow" in payload["agent_loop"]["before_plan"]
    assert "python -m ctx.adapters.loopflow" in payload["loopflow"]["before_plan"]
    assert payload["loopflow"]["use_tools"] == 'use tools from the "ctx" server'
    assert payload["loopflow"]["use_skills"] == "use skills: security-review"


def test_main_uses_loop_file_ctx_grants_when_cli_permissions_absent(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    loop_file = tmp_path / "review.loop"
    loop_file.write_text(
        "\n".join(
            [
                'loop "review upload":',
                "  goal: no high-severity upload findings",
                "  ctx grants: skills, mcps",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, top_k
        assert permissions == {"skills", "mcps"}
        return [
            {"name": "security-review", "type": "skill"},
            {"name": "filesystem", "type": "mcp-server"},
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    assert loopflow.main(["--loop-file", str(loop_file), "--compact"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["permissions"] == {
        "skills": True,
        "agents": False,
        "mcps": True,
        "harnesses": False,
    }
    assert payload["mcp_server"] == {
        "name": "ctx",
        "command": "ctx-mcp-server",
        "args": _expected_scoped_mcp_args("skill", "mcp-server"),
        "tools": _EXPECTED_READ_ONLY_MCP_TOOL_NAMES,
    }


def test_main_cli_permissions_override_loop_file_ctx_grants(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    loop_file = tmp_path / "review.loop"
    loop_file.write_text(
        "\n".join(
            [
                'loop "review upload":',
                "  goal: no high-severity upload findings",
                "  ctx grants: skills",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, top_k
        assert permissions == {"mcps"}
        return [
            {"name": "security-review", "type": "skill"},
            {"name": "filesystem", "type": "mcp-server"},
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    assert (
        loopflow.main(
            [
                "--loop-file",
                str(loop_file),
                "--permissions",
                "mcps",
                "--compact",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["permissions"] == {
        "skills": False,
        "agents": False,
        "mcps": True,
        "harnesses": False,
    }
    assert payload["capabilities"]["skills"] == []
    assert payload["capabilities"]["mcps"] == [{"name": "filesystem", "type": "mcp-server"}]


def test_look_at_paths_are_sanitized_in_public_payload(monkeypatch) -> None:
    raw_paths = [
        "/private/repos/customer-alpha/src/payments/checkout.py",
        r"C:\sensitive\customer-beta\src\billing\handler.ts",
    ]
    capability_queries: list[str] = []

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del permissions, top_k
        capability_queries.append(query)
        return [{"name": "security-review", "type": "skill"}]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    payload = loopflow.recommend_for_loop(
        goal="review checkout handling",
        look_at=raw_paths,
        permissions={"skills"},
    )

    serialized_payload = json.dumps(payload, sort_keys=True)
    expected_hashes = [hashlib.sha256(path.encode("utf-8")).hexdigest()[:16] for path in raw_paths]
    assert capability_queries == [
        "review checkout handling loopflow "
        "context: /private/repos/customer-alpha/src/payments/checkout.py, "
        r"C:\sensitive\customer-beta\src\billing\handler.ts"
    ]
    assert payload["context"]["look_at"] == {
        "count": 2,
        "items": [
            {"basename": "checkout.py", "path_hash": expected_hashes[0]},
            {"basename": "handler.ts", "path_hash": expected_hashes[1]},
        ],
    }
    assert "basename=checkout.py" in payload["context"]["query"]
    assert "basename=handler.ts" in payload["context"]["query"]
    assert "count=2" in payload["context"]["query"]
    for raw_path in raw_paths:
        assert raw_path not in payload["context"]["query"]
        assert raw_path not in serialized_payload


def test_last_failure_match_fields_stay_out_of_capability_payload(monkeypatch) -> None:
    secret = "ctxsecretneedle"
    capability_queries: list[str] = []

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del permissions, top_k
        capability_queries.append(query)
        return [
            {
                "name": "security-review",
                "type": "skill",
                "score": 91,
                "matching_tags": [secret],
                "shared_tags": [secret],
                "tags": [secret],
                "fit_reason": f"matched {secret}",
                "reliability_reason": f"validated {secret}",
            }
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    payload = loopflow.recommend_for_loop(
        goal="review upload handling",
        last_failure=f"stack trace mentions {secret}",
        permissions={"skills"},
    )

    serialized_payload = json.dumps(payload, sort_keys=True)
    assert capability_queries == [
        f"review upload handling loopflow last failure: stack trace mentions {secret}"
    ]
    assert payload["context"]["last_failure_present"] is True
    assert secret not in payload["context"]["query"]
    assert secret not in serialized_payload
    assert payload["capabilities"]["skills"] == [
        {"name": "security-review", "type": "skill", "score": 91}
    ]


def test_main_loop_file_read_errors_are_argparse_errors(
    tmp_path: Path,
    capsys,
) -> None:
    missing_loop_file = tmp_path / "missing.loop"

    with pytest.raises(SystemExit) as exc_info:
        loopflow.main(["--loop-file", str(missing_loop_file)])

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "could not read --loop-file" in stderr
    assert str(missing_loop_file) in stderr


def test_main_last_failure_file_read_errors_are_argparse_errors(
    tmp_path: Path,
    capsys,
) -> None:
    missing_failure_file = tmp_path / "missing-failure.txt"

    with pytest.raises(SystemExit) as exc_info:
        loopflow.main(
            [
                "--goal",
                "fix checkout",
                "--last-failure-file",
                str(missing_failure_file),
            ]
        )

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "could not read --last-failure-file" in stderr
    assert str(missing_failure_file) in stderr


def test_main_empty_permissions_fail_closed(monkeypatch, capsys) -> None:
    def fail_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        raise AssertionError("_recommend_capability_rows should not run without grants")

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fail_recommend_rows)
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    for argv in (
        [
            "--goal",
            "deny all recommendations",
            "--permissions",
            "",
            "--compact",
        ],
        [
            "--goal",
            "deny all recommendations",
            "--compact",
        ],
    ):
        assert loopflow.main(argv) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["permissions"] == {
            "skills": False,
            "agents": False,
            "mcps": False,
            "harnesses": False,
        }
        assert payload["capabilities"] == {
            "skills": [],
            "agents": [],
            "mcps": [],
            "harnesses": [],
        }
        assert payload["loopflow"]["use_tools"] is None
        assert payload["loopflow"]["use_skills"] is None
