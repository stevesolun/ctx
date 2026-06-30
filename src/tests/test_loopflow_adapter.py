"""Regression tests for the LoopFlow / agent-loop adapter."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from ctx.adapters import loopflow
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox


def test_parse_loop_file_reads_loopflow_context(tmp_path: Path) -> None:
    loop_file = tmp_path / "rate-limit.loop"
    loop_file.write_text(
        '\n'.join(
            [
                'loop "add API rate limiting":',
                "  goal: requests are rate-limited per API key",
                "  done when \"pnpm test rate-limit\" passes",
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
    def fake_recommend_bundle(query: str, *, top_k: int) -> list[dict[str, Any]]:
        assert "checkout e2e" in query
        assert top_k == 8
        return [
            {"name": "playwright-debug", "type": "skill", "score": 91},
            {"name": "browser-agent", "type": "agent", "score": 85},
            {"name": "filesystem", "type": "mcp-server", "score": 80},
        ]

    monkeypatch.setattr(loopflow, "recommend_bundle", fake_recommend_bundle)

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
    assert [row["name"] for row in payload["capabilities"]["skills"]] == [
        "playwright-debug"
    ]
    assert payload["capabilities"]["agents"] == []
    assert [row["name"] for row in payload["capabilities"]["mcps"]] == ["filesystem"]
    assert payload["mcp_server"]["command"] == "ctx-mcp-server"
    expected_tool_names = [
        definition.name for definition in CtxCoreToolbox().tool_definitions()
    ]
    assert payload["mcp_server"]["tools"] == expected_tool_names
    assert {
        "ctx__load_entity",
        "ctx__record_validation",
        "ctx__session_state",
    } <= set(payload["mcp_server"]["tools"])


def test_empty_permissions_stay_empty(monkeypatch) -> None:
    def fail_recommend_bundle(query: str, *, top_k: int) -> list[dict[str, Any]]:
        raise AssertionError("recommend_bundle should not run without grants")

    monkeypatch.setattr(loopflow, "recommend_bundle", fail_recommend_bundle)

    payload = loopflow.recommend_for_loop(
        goal="deny all recommendations",
        permissions=set(),
    )

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
    def fake_recommend_bundle(query: str, *, top_k: int) -> list[dict[str, Any]]:
        return [
            {"name": "security-review", "type": "skill"},
            {"name": "filesystem", "type": "mcp-server"},
        ]

    monkeypatch.setattr(loopflow, "recommend_bundle", fake_recommend_bundle)

    payload = loopflow.recommend_for_loop(
        goal="recommend only tools",
        permissions={"mcps"},
    )

    assert payload["permissions"]["skills"] is False
    assert payload["capabilities"]["skills"] == []
    assert [row["name"] for row in payload["capabilities"]["mcps"]] == ["filesystem"]
    assert payload["loopflow"]["use_tools"] == 'use tools from the "ctx" server'
    assert payload["loopflow"]["use_skills"] is None


def test_loopflow_tool_hint_requires_mcps_permission(monkeypatch) -> None:
    def fake_recommend_bundle(query: str, *, top_k: int) -> list[dict[str, Any]]:
        return [
            {"name": "security-review", "type": "skill"},
            {"name": "filesystem", "type": "mcp-server"},
        ]

    monkeypatch.setattr(loopflow, "recommend_bundle", fake_recommend_bundle)

    payload = loopflow.recommend_for_loop(
        goal="recommend only skills",
        permissions={"skills"},
    )

    assert payload["permissions"]["mcps"] is False
    assert [row["name"] for row in payload["capabilities"]["skills"]] == [
        "security-review"
    ]
    assert payload["capabilities"]["mcps"] == []
    assert payload["loopflow"]["use_tools"] is None
    assert payload["loopflow"]["use_skills"].startswith("use skills: ctx-recommend")
    assert payload["mcp_server"] == {
        "name": "ctx",
        "command": None,
        "tools": [],
    }


def test_harnesses_require_user_owned_llm(monkeypatch) -> None:
    calls: list[str] = []

    def fake_recommend_harnesses(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append("called")
        return [{"name": "local-agent-loop", "type": "harness", "fit_score": 0.9}]

    monkeypatch.setattr(loopflow, "recommend_bundle", lambda query, *, top_k: [])
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
    assert calls == ["called"]
    assert allowed["capabilities"]["harnesses"][0]["name"] == "local-agent-loop"
    assert "ctx-harness-install local-agent-loop --dry-run" in allowed["agent_loop"][
        "harness_install"
    ]
    assert "--model-provider" in allowed["agent_loop"]["harness_install"]


def test_harness_install_command_is_shell_quoted(monkeypatch) -> None:
    def fake_recommend_harnesses(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"name": "local $(touch bad)", "type": "harness", "fit_score": 0.9}]

    monkeypatch.setattr(loopflow, "recommend_bundle", lambda query, *, top_k: [])
    monkeypatch.setattr(loopflow, "recommend_harnesses", fake_recommend_harnesses)

    payload = loopflow.recommend_for_loop(
        goal="run $(touch bad)",
        permissions={"harnesses"},
        own_llm=True,
        model_provider="open`whoami`",
        model="llama; rm -rf .",
        harness_requirements={
            "runtime": "local $(touch bad)",
            "api_key_env": "OPENAI_API_KEY",
        },
    )

    command = payload["agent_loop"]["harness_install"]

    assert command.startswith("ctx-harness-install 'local $(touch bad)' --dry-run")
    assert "'run $(touch bad)'" in command
    assert shlex.split(command) == [
        "ctx-harness-install",
        "local $(touch bad)",
        "--dry-run",
        "--goal",
        "run $(touch bad)",
        "--model-provider",
        "open`whoami`",
        "--model",
        "llama; rm -rf .",
        "--harness-runtime",
        "local $(touch bad)",
        "--api-key-env",
        "OPENAI_API_KEY",
    ]


def test_main_api_key_env_reaches_harness_install(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        loopflow,
        "recommend_bundle",
        lambda query, *, top_k: (_ for _ in ()).throw(
            AssertionError("recommend_bundle should not run for harness-only grants")
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
        "remote-agent-loop",
        "--dry-run",
        "--goal",
        "run remote loop",
        "--model-provider",
        "openai",
        "--model",
        "gpt-4o",
        "--api-key-env",
        "OPENAI_API_KEY",
    ]


def test_main_emits_json_from_loop_file(tmp_path: Path, monkeypatch, capsys) -> None:
    loop_file = tmp_path / "review.loop"
    failure_file = tmp_path / "failure.txt"
    loop_file.write_text(
        '\n'.join(
            [
                'loop "review upload":',
                "  goal: no high-severity upload findings",
                "  look at: upload.py, tests/upload_test.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    failure_file.write_text("semgrep found upload risk", encoding="utf-8")

    monkeypatch.setattr(
        loopflow,
        "recommend_bundle",
        lambda query, *, top_k: [{"name": "security-review", "type": "skill"}],
    )
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
    assert payload["context"]["goal"] == "no high-severity upload findings"
    assert payload["context"]["last_failure_present"] is True
    assert "python -m ctx.adapters.loopflow" in payload["agent_loop"]["before_plan"]
    assert "python -m ctx.adapters.loopflow" in payload["loopflow"]["before_plan"]
    assert payload["loopflow"]["use_tools"] == 'use tools from the "ctx" server'
    assert payload["loopflow"]["use_skills"].startswith("use skills: ctx-recommend")


def test_main_empty_permissions_fail_closed(monkeypatch, capsys) -> None:
    def fail_recommend_bundle(query: str, *, top_k: int) -> list[dict[str, Any]]:
        raise AssertionError("recommend_bundle should not run without grants")

    monkeypatch.setattr(loopflow, "recommend_bundle", fail_recommend_bundle)
    monkeypatch.setattr(loopflow, "recommend_harnesses", lambda *args, **kwargs: [])

    assert (
        loopflow.main(
            [
                "--goal",
                "deny all recommendations",
                "--permissions",
                "",
                "--compact",
            ]
        )
        == 0
    )

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
