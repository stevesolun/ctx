"""Regression tests for the LoopFlow / agent-loop adapter."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shlex
from pathlib import Path
import threading
from typing import Any

import networkx as nx
import pytest
import ctx.api as ctx_api
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall
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

    assert calls == [50]
    assert payload["capabilities"]["skills"] == [
        {"id": "skill:python-patterns", "name": "python-patterns", "type": "skill", "score": 80}
    ]


def test_loopflow_session_rejections_filter_primary_capabilities(monkeypatch) -> None:
    memory: dict[str, list[str]] = {}

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, permissions, top_k
        return [
            {
                "id": "skill:rejected-helper",
                "name": "rejected-helper",
                "type": "skill",
                "score": 99,
                "installable": True,
                "load_status": "local-wiki",
            },
            {
                "id": "skill:accepted-helper",
                "name": "accepted-helper",
                "type": "skill",
                "score": 90,
                "installable": True,
                "load_status": "local-wiki",
            },
        ]

    def fake_rejections(
        rejected: list[str] | None = None,
        *,
        session_id: str | None = None,
        rejection_mode: str = "use",
    ) -> list[str]:
        assert session_id is not None
        explicit = list(rejected or [])
        if rejection_mode == "ignore":
            return explicit
        if rejection_mode == "replace":
            memory[session_id] = explicit
        else:
            memory[session_id] = list(dict.fromkeys(memory.get(session_id, []) + explicit))
        return memory[session_id]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)
    monkeypatch.setattr(loopflow.ctx_api, "recommendation_rejections", fake_rejections)

    first = loopflow.recommend_for_loop(
        goal="python api",
        permissions={"skills"},
        rejected=["skill:rejected-helper"],
        session_id="loop-session",
        top_k=2,
    )
    remembered = loopflow.recommend_for_loop(
        goal="python api",
        permissions={"skills"},
        session_id="loop-session",
        top_k=2,
    )
    stateless = loopflow.recommend_for_loop(
        goal="python api",
        permissions={"skills"},
        top_k=2,
    )

    assert first["selection"] == {
        "selected": [],
        "rejected": ["skill:rejected-helper"],
        "session_bound": True,
        "rejection_mode": "use",
    }
    assert [row["id"] for row in remembered["capabilities"]["skills"]] == ["skill:accepted-helper"]
    assert [row["id"] for row in stateless["capabilities"]["skills"]] == [
        "skill:rejected-helper",
        "skill:accepted-helper",
    ]


def test_loopflow_session_rejections_filter_and_backfill_harnesses(monkeypatch) -> None:
    monkeypatch.setattr(
        loopflow.ctx_api,
        "recommendation_rejections",
        lambda rejected=None, **kwargs: list(rejected or []),
    )
    requested_top_k: list[int] = []

    def fake_harnesses(goal: str, *, top_k: int, **kwargs: Any) -> list[dict[str, Any]]:
        del goal, kwargs
        requested_top_k.append(top_k)
        return [
            {"id": "harness:rejected-runner", "name": "rejected-runner", "type": "harness"},
            {"id": "harness:accepted-runner", "name": "accepted-runner", "type": "harness"},
        ]

    monkeypatch.setattr(loopflow, "recommend_harnesses", fake_harnesses)

    payload = loopflow.recommend_for_loop(
        goal="run a local agent loop",
        permissions={"harnesses"},
        own_llm=True,
        model_provider="ollama",
        rejected=["harness:rejected-runner"],
        session_id="harness-session",
        top_k=1,
    )

    assert requested_top_k == [7]
    assert [row["id"] for row in payload["capabilities"]["harnesses"]] == [
        "harness:accepted-runner"
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


def test_loopflow_primary_capabilities_apply_context_policy(monkeypatch) -> None:
    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, permissions, top_k
        return [
            {
                "name": "local-go-helper",
                "type": "skill",
                "installable": True,
                "load_status": "local-wiki",
                "matching_tags": ["go", "api"],
                "score": 91,
            },
            {
                "name": "local-python-helper",
                "type": "skill",
                "installable": True,
                "load_status": "local-wiki",
                "matching_tags": ["python", "api"],
                "score": 88,
            },
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="LoCoBench python feature_implementation. No local API keys. Need local files.",
        permissions={"skills"},
        top_k=1,
    )

    assert payload["capabilities"]["skills"] == [
        {
            "name": "local-python-helper",
            "type": "skill",
            "score": 88,
            "installable": True,
            "load_status": "local-wiki",
        }
    ]


def test_loopflow_language_context_overfetches_primary_candidates(monkeypatch) -> None:
    fetch_counts: list[int] = []

    def fake_recommend_rows(
        query: str,
        *,
        permissions: set[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        del query, permissions
        fetch_counts.append(top_k)
        rows = [
            {
                "name": f"generic-api-helper-{index}",
                "type": "skill",
                "installable": True,
                "load_status": "local-wiki",
                "matching_tags": ["api"],
                "tags": ["go", "api"],
                "score": 100 - index,
            }
            for index in range(min(top_k, 6))
        ]
        if top_k > 6:
            rows.append(
                {
                    "name": "backend-api-helper",
                    "type": "skill",
                    "installable": True,
                    "load_status": "local-wiki",
                    "matching_tags": ["api"],
                    "tags": ["python", "api"],
                    "score": 88,
                }
            )
        return rows

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="LoCoBench python api helper",
        permissions={"skills"},
        top_k=1,
    )

    assert fetch_counts == [50]
    assert payload["capabilities"]["skills"] == [
        {
            "name": "backend-api-helper",
            "type": "skill",
            "score": 88,
            "installable": True,
            "load_status": "local-wiki",
        }
    ]


def test_project_owned_fallback_survives_context_filtering_and_owned_llm(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wiki = tmp_path / "wiki"
    converted = wiki / "converted" / "ctx-python-testing"
    converted.mkdir(parents=True)
    (converted / "SKILL.md").write_text("# Python testing\n", encoding="utf-8")

    graph = nx.Graph()
    for index in range(60):
        graph.add_node(
            f"skill:remote-{index}",
            label=f"remote-{index}",
            type="skill",
            tags=["python", "api"],
            source_catalog="skills.sh",
            status="remote-cataloged",
        )
    graph.add_node(
        "skill:ctx-python-testing",
        label="ctx-python-testing",
        type="skill",
        tags=["python", "testing"],
        source="ctx-runtime-availability",
        status="local-wiki",
    )

    ranked_queries: list[str] = []

    def fake_recommend_by_tags(
        candidate_graph: Any,
        tags: list[str],
        *,
        top_n: int,
        query: str | None,
        entity_types: tuple[str, ...] | set[str] | None,
        min_normalized_score: float,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        del tags, entity_types, min_normalized_score, kwargs
        assert query is not None
        ranked_queries.append(query)
        if candidate_graph.number_of_nodes() == 1:
            return [
                {
                    "name": "ctx-python-testing",
                    "type": "skill",
                    "score": 80,
                    "source": "ctx-runtime-availability",
                    "status": "local-wiki",
                }
            ]
        return [
            {
                "name": f"remote-{index}",
                "type": "skill",
                "score": 100 - index,
                "source_catalog": "skill-index",
                "status": "available",
                "install_command": f"ctx-skill-install remote-{index}",
            }
            for index in range(min(top_n, 50))
        ]

    monkeypatch.setattr(loopflow, "_recommendation_graph", lambda: graph)
    monkeypatch.setattr(loopflow.ctx_api, "default_wiki_dir", lambda: wiki)
    monkeypatch.setattr(loopflow, "recommend_by_tags", fake_recommend_by_tags)

    plain = loopflow.recommend_for_loop(
        goal="Implement and test a local Python API feature with no API keys",
        permissions={"skills"},
        top_k=1,
    )
    owned = loopflow.recommend_for_loop(
        goal="Implement and test a local Python API feature with no API keys",
        permissions={"skills"},
        own_llm=True,
        model_provider="openai",
        model="gpt-5.5",
        top_k=1,
    )

    expected = [
        {
            "id": "skill:ctx-python-testing",
            "name": "ctx-python-testing",
            "type": "skill",
            "score": 80,
            "source": "ctx-runtime-availability",
            "status": "local-wiki",
            "installable": True,
            "load_status": "local-wiki",
            "source_path": "converted/ctx-python-testing/SKILL.md",
        }
    ]
    assert plain["capabilities"]["skills"] == expected
    assert owned["capabilities"]["skills"] == expected
    assert all("openai" not in query and "gpt-5.5" not in query for query in ranked_queries)
    assert "model: openai gpt-5.5" in owned["context"]["query"]


def test_real_capability_rows_receive_stable_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    graph = nx.Graph()
    graph.add_node("skill:one", label="one", type="skill", tags=["local"])
    monkeypatch.setattr(loopflow, "_recommendation_graph", lambda: graph)
    monkeypatch.setattr(loopflow.ctx_api, "default_wiki_dir", lambda: tmp_path)
    monkeypatch.setattr(loopflow, "query_to_tags", lambda query: ["local"])
    monkeypatch.setattr(
        loopflow,
        "recommend_by_tags",
        lambda *args, **kwargs: [
            {"name": "testing", "type": "skill", "score": 3},
            {"name": "reviewer", "type": "agent", "score": 2},
            {"name": "filesystem", "type": "mcp-server", "score": 1},
        ],
    )

    rows = loopflow._recommend_capability_rows(
        "local recommendations",
        permissions={"skills", "agents", "mcps"},
        top_k=1,
    )

    assert [row["id"] for row in rows] == [
        "skill:testing",
        "agent:reviewer",
        "mcp-server:filesystem",
    ]


def test_loopflow_local_no_key_loop_filters_related_recommendations(monkeypatch) -> None:
    monkeypatch.setattr(loopflow, "_recommend_capability_rows", lambda *args, **kwargs: [])

    def fake_recommend_related(
        selected: list[str],
        *,
        rejected: list[str] | None = None,
        max_hops: int = 2,
        top_n: int = 5,
    ) -> list[dict[str, Any]]:
        assert selected == ["skill:local-helper"]
        assert rejected == []
        assert max_hops == 2
        assert top_n == 50
        return [
            {
                "id": "skill:remote-api",
                "name": "remote-api",
                "type": "skill",
                "status": "available",
                "source_catalog": "skill-index",
                "install_command": "ctx-skill-install remote-api",
                "selection_state": "suggested_related",
            },
            {
                "id": "skill:go-api",
                "name": "go-api",
                "type": "skill",
                "installable": True,
                "load_status": "local-wiki",
                "matching_tags": ["go", "api"],
                "selection_state": "suggested_related",
            },
            {
                "id": "skill:python-api",
                "name": "python-api",
                "type": "skill",
                "installable": True,
                "load_status": "local-wiki",
                "matching_tags": ["python", "api"],
                "selection_state": "suggested_related",
            },
        ]

    monkeypatch.setattr(loopflow.ctx_api, "recommend_related", fake_recommend_related)

    payload = loopflow.recommend_for_loop(
        goal="LoCoBench python feature_implementation. No local API keys. Need local files.",
        permissions={"skills"},
        selected=["skill:local-helper"],
        top_k=3,
    )

    assert payload["related_recommendations"] == [
        {
            "id": "skill:python-api",
            "name": "python-api",
            "type": "skill",
            "installable": True,
            "load_status": "local-wiki",
            "selection_state": "suggested_related",
        }
    ]


def test_related_recommendations_include_availability_metadata(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    converted = wiki / "converted" / "local-helper"
    converted.mkdir(parents=True)
    (converted / "SKILL.md").write_text("# Local helper\n", encoding="utf-8")

    graph = nx.Graph()
    graph.add_node("skill:seed", label="seed", type="skill", tags=["python"])
    graph.add_node(
        "skill:local-helper",
        label="local-helper",
        type="skill",
        tags=["python", "api"],
        status="cataloged",
    )
    graph.add_edge("skill:seed", "skill:local-helper", weight=1.0, shared_tags=["python"])

    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph.json").write_text(
        json.dumps(nx.node_link_data(graph, edges="edges")),
        encoding="utf-8",
    )

    payload = json.loads(
        CtxCoreToolbox(wiki_dir=wiki).dispatch(
            ToolCall(
                id="c1",
                name="ctx__recommend_related",
                arguments={"selected": ["skill:seed"], "top_n": 1},
            )
        )
    )

    assert payload["results"][0]["id"] == "skill:local-helper"
    assert payload["results"][0]["tags"] == ["python", "api"]
    assert payload["results"][0]["installable"] is True
    assert payload["results"][0]["load_status"] == "local-wiki"
    assert payload["results"][0]["source_path"] == "converted/local-helper/SKILL.md"


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
        assert top_n == 50
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
            "id": "skill:local-helper",
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
        goal="backend task",
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
        goal="backend task",
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
        goal="backend task",
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
    assert capability_queries == [
        "fix checkout e2e loopflow done when: "
        '"pytest src/tests/test_loopflow_adapter.py -q" passes, pnpm lint passes'
    ]
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


def test_main_forwards_rejection_session_flags(monkeypatch, capsys) -> None:
    captured: dict[str, Any] = {}

    def fake_recommend_for_loop(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(loopflow, "recommend_for_loop", fake_recommend_for_loop)

    assert (
        loopflow.main(
            [
                "--goal",
                "review api",
                "--permissions",
                "skills",
                "--rejected",
                "skill:legacy-helper",
                "--session-id",
                "loop-cli-session",
                "--rejection-mode",
                "replace",
                "--compact",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == {"ok": True}
    assert captured["rejected"] == ["skill:legacy-helper"]
    assert captured["session_id"] == "loop-cli-session"
    assert captured["rejection_mode"] == "replace"


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


def test_activation_leases_share_context_until_last_loop_releases() -> None:
    leases = loopflow.ActivationLeaseRegistry()
    applied: list[loopflow.ActivationLeaseActions] = []

    outer = leases.sync(
        "session.outer",
        desired=["skill:security-review", "mcp:filesystem"],
        permissions={"skills", "mcps"},
        apply=applied.append,
        used=["mcp-server:filesystem"],
    )
    nested = leases.sync(
        "session.inner",
        desired=["skill:security-review"],
        permissions={"skills"},
        apply=applied.append,
        used=["skill:security-review"],
    )

    assert outer.as_dict() == {
        "keep": [],
        "load": ["mcp-server:filesystem", "skill:security-review"],
        "use": ["mcp-server:filesystem"],
        "unload": [],
    }
    assert nested.as_dict() == {
        "keep": ["skill:security-review"],
        "load": [],
        "use": ["skill:security-review"],
        "unload": [],
    }
    assert leases.active_context() == (
        "mcp-server:filesystem",
        "skill:security-review",
    )

    outer_release = leases.release("session.outer", apply=applied.append)

    assert outer_release.as_dict() == {
        "keep": ["skill:security-review"],
        "load": [],
        "use": [],
        "unload": ["mcp-server:filesystem"],
    }
    assert leases.active_context() == ("skill:security-review",)

    nested_release = leases.release("session.inner", apply=applied.append)

    assert nested_release.as_dict() == {
        "keep": [],
        "load": [],
        "use": [],
        "unload": ["skill:security-review"],
    }
    assert leases.active_context() == ()
    assert (
        leases.release("session.inner", apply=applied.append) == loopflow.ActivationLeaseActions()
    )
    assert applied == [
        outer,
        nested,
        outer_release,
        nested_release,
        loopflow.ActivationLeaseActions(),
    ]


def test_activation_lease_permissions_fail_closed_without_changing_ownership() -> None:
    leases = loopflow.ActivationLeaseRegistry()
    initial = leases.sync(
        "session.owner",
        desired=["skill:security-review"],
        permissions={"skills"},
        apply=lambda _actions: None,
    )
    assert initial.load == ("skill:security-review",)

    with pytest.raises(ValueError, match="not granted by permissions"):
        leases.sync(
            "session.blocked",
            desired=["skill:security-review"],
            permissions={"mcps"},
            apply=lambda _actions: None,
        )
    with pytest.raises(ValueError, match="used entities must also be desired"):
        leases.sync(
            "session.owner",
            desired=["skill:security-review"],
            permissions={"skills", "agents"},
            apply=lambda _actions: None,
            used=["agent:reviewer"],
        )

    assert leases.active_context() == ("skill:security-review",)
    assert leases.release("session.owner", apply=lambda _actions: None).unload == (
        "skill:security-review",
    )


def test_activation_lease_failed_actions_leave_truthful_retryable_state() -> None:
    leases = loopflow.ActivationLeaseRegistry()

    def fail(_actions: loopflow.ActivationLeaseActions) -> None:
        raise RuntimeError("host action failed")

    with pytest.raises(RuntimeError, match="host action failed"):
        leases.sync(
            "session.owner",
            desired=["skill:security-review"],
            permissions={"skills"},
            apply=fail,
        )
    assert leases.active_context() == ()
    retry = leases.sync(
        "session.owner",
        desired=["skill:security-review"],
        permissions={"skills"},
        apply=lambda _actions: None,
    )
    assert retry.load == ("skill:security-review",)

    with pytest.raises(RuntimeError, match="host action failed"):
        leases.release("session.owner", apply=fail)
    assert leases.active_context() == ("skill:security-review",)
    assert leases.release("session.owner", apply=lambda _actions: None).unload == (
        "skill:security-review",
    )


def test_activation_lease_context_releases_after_loop_failure() -> None:
    leases = loopflow.ActivationLeaseRegistry()
    applied: list[loopflow.ActivationLeaseActions] = []
    used = ["agent:reviewer"]

    with pytest.raises(RuntimeError, match="loop failed"):
        with leases.lease(
            "session.owner",
            desired=["agent:reviewer"],
            permissions={"agents"},
            apply=applied.append,
            used=lambda: used,
        ):
            raise RuntimeError("loop failed")

    assert applied[0].load == ("agent:reviewer",)
    assert applied[1].use == ("agent:reviewer",)
    assert applied[2].unload == ("agent:reviewer",)
    assert leases.active_context() == ()


def test_activation_lease_context_rejects_duplicate_live_id() -> None:
    leases = loopflow.ActivationLeaseRegistry()
    applied: list[loopflow.ActivationLeaseActions] = []

    with leases.lease(
        "session.same",
        desired=["skill:reviewer"],
        permissions={"skills"},
        apply=applied.append,
    ):
        with pytest.raises(ValueError, match="already active"):
            with leases.lease(
                "session.same",
                desired=["skill:reviewer"],
                permissions={"skills"},
                apply=applied.append,
            ):
                pytest.fail("duplicate lease context entered")
        with pytest.raises(RuntimeError, match="active context manager"):
            leases.release("session.same", apply=applied.append)
        assert leases.active_context() == ("skill:reviewer",)

    assert [actions.load for actions in applied] == [("skill:reviewer",), ()]
    assert [actions.unload for actions in applied] == [(), ("skill:reviewer",)]
    assert leases.active_context() == ()


@pytest.mark.parametrize(
    ("lease_id", "entity_id", "error"),
    [
        (None, "skill:security-review", TypeError),
        ("session.owner", None, TypeError),
        ("session.owner", "skill:../escape", ValueError),
        ("session.owner", "skill:CON", ValueError),
    ],
)
def test_activation_lease_rejects_unsafe_boundary_ids(
    lease_id: object,
    entity_id: object,
    error: type[Exception],
) -> None:
    leases = loopflow.ActivationLeaseRegistry()

    with pytest.raises(error):
        leases.sync(
            lease_id,  # type: ignore[arg-type]
            desired=[entity_id],  # type: ignore[list-item]
            permissions={"skills"},
            apply=lambda _actions: None,
        )

    assert leases.active_context() == ()


def test_activation_leases_serialize_parallel_shared_context() -> None:
    leases = loopflow.ActivationLeaseRegistry()
    owners = 16
    entered = threading.Barrier(owners)
    applied: list[loopflow.ActivationLeaseActions] = []

    def run(index: int) -> None:
        with leases.lease(
            f"session.{index}",
            desired=["mcp-server:filesystem"],
            permissions={"mcps"},
            apply=applied.append,
        ):
            entered.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=owners) as pool:
        list(pool.map(run, range(owners)))

    assert sum(bool(actions.load) for actions in applied) == 1
    assert sum(bool(actions.unload) for actions in applied) == 1
    assert leases.active_context() == ()


def test_activation_lease_callback_cannot_reenter_registry() -> None:
    leases = loopflow.ActivationLeaseRegistry()

    def reenter(_actions: loopflow.ActivationLeaseActions) -> None:
        leases.sync(
            "session.inner",
            desired=["skill:reviewer"],
            permissions={"skills"},
            apply=lambda _nested: None,
        )

    with pytest.raises(RuntimeError, match="must not invoke or wait"):
        leases.sync(
            "session.outer",
            desired=["skill:reviewer"],
            permissions={"skills"},
            apply=reenter,
        )

    assert leases.active_context() == ()


def test_activation_lease_callback_worker_reentry_fails_without_deadlock() -> None:
    leases = loopflow.ActivationLeaseRegistry()
    nested_errors: list[Exception] = []

    def reenter_from_worker(_actions: loopflow.ActivationLeaseActions) -> None:
        def nested() -> None:
            try:
                leases.sync(
                    "session.inner",
                    desired=["skill:reviewer"],
                    permissions={"skills"},
                    apply=lambda _nested: None,
                )
            except Exception as exc:  # noqa: BLE001 - asserted below.
                nested_errors.append(exc)

        worker = threading.Thread(target=nested)
        worker.start()
        worker.join(timeout=1)
        assert not worker.is_alive()

    leases.sync(
        "session.outer",
        desired=["skill:reviewer"],
        permissions={"skills"},
        apply=reenter_from_worker,
    )

    assert len(nested_errors) == 1
    assert isinstance(nested_errors[0], loopflow.ActivationLeaseBusyError)
    assert "busy" in str(nested_errors[0])
    assert leases.active_context() == ("skill:reviewer",)


def test_activation_lease_independent_waiter_serializes_after_slow_callback() -> None:
    leases = loopflow.ActivationLeaseRegistry()
    callback_started = threading.Event()
    callback_continue = threading.Event()
    waiter_done = threading.Event()
    errors: list[Exception] = []

    def slow_apply(_actions: loopflow.ActivationLeaseActions) -> None:
        callback_started.set()
        assert callback_continue.wait(timeout=2)

    def first() -> None:
        try:
            leases.sync(
                "session.first",
                desired=["skill:first"],
                permissions={"skills"},
                apply=slow_apply,
            )
        except Exception as exc:  # noqa: BLE001 - asserted below.
            errors.append(exc)

    def independent() -> None:
        try:
            assert callback_started.wait(timeout=2)
            leases.sync(
                "session.parallel",
                desired=["skill:parallel"],
                permissions={"skills"},
                apply=lambda _actions: None,
                wait_for_transition=True,
            )
            waiter_done.set()
        except Exception as exc:  # noqa: BLE001 - asserted below.
            errors.append(exc)

    first_thread = threading.Thread(target=first)
    waiter_thread = threading.Thread(target=independent)
    first_thread.start()
    waiter_thread.start()
    assert callback_started.wait(timeout=2)
    assert not waiter_done.wait(timeout=0.05)
    callback_continue.set()
    first_thread.join(timeout=2)
    waiter_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not waiter_thread.is_alive()
    assert errors == []
    assert leases.active_context() == ("skill:first", "skill:parallel")
