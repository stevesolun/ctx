"""Regression tests for the LoopFlow / agent-loop adapter."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest
import ctx.api as ctx_api
from ctx.adapters import loopflow


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
        assert top_k == 2
        return [
            {"name": "playwright-debug", "type": "skill", "score": 91},
            {"name": "browser-agent", "type": "agent", "score": 85},
            {"name": "filesystem", "type": "mcp-server", "score": 80},
        ]

    monkeypatch.setattr(loopflow, "_recommend_capability_rows", fake_recommend_rows)

    payload = loopflow.recommend_for_loop(
        goal="fix checkout e2e",
        loop_kind="agent-loop",
        permissions={"skills", "mcps"},
        top_k=2,
    )

    assert payload["adapter"] == "agent-loop"
    assert payload["permissions"] == {
        "skills": True,
        "agents": False,
        "mcps": True,
        "harnesses": False,
    }
    assert [row["name"] for row in payload["capabilities"]["skills"]] == ["playwright-debug"]
    assert payload["capabilities"]["agents"] == []
    assert [row["name"] for row in payload["capabilities"]["mcps"]] == ["filesystem"]
    assert payload["mcp_server"] == {
        "name": "ctx",
        "command": None,
        "tools": [],
    }


def test_mcp_server_tools_are_filtered_by_permission_groups(monkeypatch) -> None:
    monkeypatch.setattr(loopflow, "_recommend_capability_rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    mcps_only = loopflow.recommend_for_loop(
        goal="recommend only mcp servers",
        permissions={"mcps"},
    )
    assert mcps_only["mcp_server"] == {
        "name": "ctx",
        "command": None,
        "tools": [],
    }

    core_recommendations = loopflow.recommend_for_loop(
        goal="recommend core capabilities",
        permissions={"skills", "agents", "mcps"},
    )
    assert core_recommendations["mcp_server"] == {
        "name": "ctx",
        "command": None,
        "tools": [],
    }

    all_grants = loopflow.recommend_for_loop(
        goal="recommend every capability",
        permissions={"skills", "agents", "mcps", "harnesses"},
    )
    assert all_grants["mcp_server"]["command"] == "ctx-mcp-server"
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
    assert payload["loopflow"]["use_tools"] is None
    assert payload["loopflow"]["use_skills"] is None
    assert payload["mcp_server"] == {
        "name": "ctx",
        "command": None,
        "tools": [],
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
        "harnesses permission granted but no user-owned LLM/model was declared"
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
    assert payload["loopflow"]["use_tools"] is None
    assert payload["loopflow"]["use_skills"] == "use skills: security-review"


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
