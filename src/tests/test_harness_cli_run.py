"""
test_harness_cli_run.py -- `ctx run` / `ctx resume` / `ctx sessions` CLI.

Every test mocks LiteLLM and (when needed) the MCP router so no real
subprocess or network happens. The goal is pinning:

  * argv parsing for all 3 subcommands
  * provider key-env auto-detection
  * MCP spec parser (preset vs explicit)
  * session-start metadata capture
  * exit codes
  * stdout / stderr separation (--quiet, --json)
  * resume round-trip

Real-provider smoke lives in an integration-marked suite we'll add
once the full H1-H7 stack is proven on a live model.
"""

from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import ctx.cli.run as run_cli
from ctx.cli.run import (
    _compile_tool_policy,
    _model_provider_prefix,
    _parse_mcp_spec,
    _resolve_api_key_env,
    _split_mcp_invocation,
    main,
)
from ctx.adapters.generic.providers import ToolCall


# ── Fixture: fake litellm so --provider ollama (no key) works ───────────────


@pytest.fixture()
def fake_litellm(monkeypatch: pytest.MonkeyPatch):
    """Drop a stub `litellm` module into sys.modules.

    Records every call to `completion` and returns a canned
    stop-response so the loop terminates after one turn.
    """
    fake = types.ModuleType("litellm")
    calls: list[dict[str, Any]] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return {
            "choices": [
                {
                    "message": {"content": "final answer", "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        }

    fake.completion = completion  # type: ignore[attr-defined]
    fake._calls = calls           # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


def _tool_call_completion(name: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": name, "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }


# ── _model_provider_prefix ─────────────────────────────────────────────────


class TestProviderPrefix:
    @pytest.mark.parametrize(
        "model,prefix",
        [
            ("openrouter/anthropic/claude", "openrouter"),
            ("ollama/llama3.1", "ollama"),
            ("openai/gpt-4", "openai"),
            ("bare-model-no-slash", "bare-model-no-slash"),
        ],
    )
    def test_parse(self, model: str, prefix: str) -> None:
        assert _model_provider_prefix(model) == prefix


# ── _resolve_api_key_env ──────────────────────────────────────────────────


class TestResolveApiKeyEnv:
    def test_explicit_wins(self) -> None:
        assert _resolve_api_key_env("MY_KEY", "ollama/x", None) == "MY_KEY"

    def test_explicit_empty_is_none(self) -> None:
        """Empty string is an explicit 'no key' (Ollama via CLI override)."""
        assert _resolve_api_key_env("", "openrouter/x", None) is None

    def test_inferred_from_model_prefix(self) -> None:
        assert _resolve_api_key_env(None, "openrouter/x", None) == "OPENROUTER_API_KEY"
        assert _resolve_api_key_env(None, "anthropic/claude", None) == "ANTHROPIC_API_KEY"

    def test_inferred_from_provider_flag(self) -> None:
        assert _resolve_api_key_env(None, "custom-x", "openai") == "OPENAI_API_KEY"

    def test_ollama_returns_none(self) -> None:
        assert _resolve_api_key_env(None, "ollama/llama3", None) is None

    def test_unknown_provider_returns_none(self) -> None:
        assert _resolve_api_key_env(None, "unknown/x", None) is None


# ── _parse_mcp_spec ────────────────────────────────────────────────────────


class TestParseMcpSpec:
    def test_preset_filesystem(self) -> None:
        cfg = _parse_mcp_spec("filesystem")
        assert cfg.name == "filesystem"
        assert cfg.command == "npx"
        assert "server-filesystem" in " ".join(cfg.args)

    def test_preset_github(self) -> None:
        cfg = _parse_mcp_spec("github")
        assert cfg.name == "github"

    def test_explicit_form(self) -> None:
        cfg = _parse_mcp_spec("fs:npx -y pkg /tmp")
        assert cfg.name == "fs"
        assert cfg.command == "npx"
        assert cfg.args == ("-y", "pkg", "/tmp")

    def test_explicit_form_preserves_quoted_args(self) -> None:
        cfg = _parse_mcp_spec(r'fs:npx -y pkg "C:\My Project"')
        assert cfg.name == "fs"
        assert cfg.command == "npx"
        assert cfg.args == ("-y", "pkg", r"C:\My Project")

    def test_windows_style_split_preserves_backslashes(self) -> None:
        assert _split_mcp_invocation(r'cmd "C:\My Project"') == [
            "cmd",
            r"C:\My Project",
        ]

    def test_explicit_single_command(self) -> None:
        cfg = _parse_mcp_spec("raw:myserver")
        assert cfg.name == "raw"
        assert cfg.command == "myserver"
        assert cfg.args == ()

    def test_filesystem_colon_path_uses_preset_command(self) -> None:
        cfg = _parse_mcp_spec("filesystem:/tmp/project")
        assert cfg.name == "filesystem"
        assert cfg.command == "npx"
        assert cfg.args[-1] == "/tmp/project"
        assert "server-filesystem" in " ".join(cfg.args)

    def test_unknown_bare_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_mcp_spec("not-a-preset")

    def test_empty_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_mcp_spec("")

    def test_empty_name_or_command_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_mcp_spec(":command-only")
        with pytest.raises(SystemExit):
            _parse_mcp_spec("name:")

    def test_whitespace_trimmed(self) -> None:
        cfg = _parse_mcp_spec("  filesystem  ")
        assert cfg.name == "filesystem"


# ── Subcommand: run ────────────────────────────────────────────────────────


class TestToolPolicy:
    def test_allow_and_deny_patterns(self) -> None:
        policy = _compile_tool_policy(["ctx__*"], ["ctx__wiki_get"])
        assert policy is not None

        assert policy(ToolCall(id="1", name="ctx__recommend_bundle", arguments={})) is None
        assert "matched deny pattern" in (
            policy(ToolCall(id="2", name="ctx__wiki_get", arguments={})) or ""
        )
        assert "no allow pattern matched" in (
            policy(ToolCall(id="3", name="filesystem__read_file", arguments={})) or ""
        )

    def test_empty_patterns_disable_policy(self) -> None:
        assert _compile_tool_policy([], []) is None


class TestRunCommand:
    def test_happy_path_writes_session(
        self, fake_litellm: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "run",
                "--model", "ollama/llama3",
                "--task", "say hi",
                "--sessions-dir", str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0
        # One session file created.
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        # Stdout is the final answer; stderr has the [ctx] status lines.
        captured = capsys.readouterr()
        assert "final answer" in captured.out

    def test_session_id_flag_pins_id(
        self, fake_litellm: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/llama3",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--session-id", "pinned-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert (tmp_path / "pinned-session.jsonl").is_file()

    def test_session_id_reuse_is_rejected_without_overwrite(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "pinned-session.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        exit_code = main(
            [
                "run",
                "--model", "ollama/llama3",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--session-id", "pinned-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert path.read_text(encoding="utf-8") == "sentinel\n"
        assert "already exists" in captured.err

    def test_session_id_reuse_can_overwrite_with_flag(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "pinned-session.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        exit_code = main(
            [
                "run",
                "--model", "ollama/llama3",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--session-id", "pinned-session",
                "--overwrite-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0
        assert "sentinel" not in path.read_text(encoding="utf-8")

    def test_metadata_recorded(
        self, fake_litellm: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model", "openrouter/anthropic/claude",
                "--task", "task-content",
                "--sessions-dir", str(tmp_path),
                "--session-id", "meta-test",
                "--no-ctx-tools",
                "--budget-usd", "1.5",
                "--api-key-env", "CUSTOM_OPENROUTER_KEY",
                "--base-url", "https://openrouter.example/api",
                "--quiet",
            ]
        )
        path = tmp_path / "meta-test.jsonl"
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        event = json.loads(first_line)
        assert event["type"] == "session_start"
        assert event["task"] == "task-content"
        assert event["model"] == "openrouter/anthropic/claude"
        assert event["provider_prefix"] == "openrouter"
        assert event["provider"] == "openrouter"
        assert event["api_key_env"] == "CUSTOM_OPENROUTER_KEY"
        assert event["base_url"] == "https://openrouter.example/api"
        assert event["budget_usd"] == 1.5

    def test_tool_policy_metadata_recorded(
        self, fake_litellm: Any, tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--session-id", "policy-meta",
                "--allow-tool", "ctx__*",
                "--deny-tool", "ctx__wiki_get",
                "--quiet",
            ]
        )
        first_line = (tmp_path / "policy-meta.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()[0]
        event = json.loads(first_line)
        assert event["tool_policy"] == {
            "allow": ["ctx__*"],
            "deny": ["ctx__wiki_get"],
        }

    def test_deny_tool_blocks_model_tool_call(
        self, fake_litellm: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            return _tool_call_completion("ctx__wiki_get")

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "call denied tool",
                "--sessions-dir", str(tmp_path),
                "--deny-tool", "ctx__wiki_get",
                "--json",
                "--quiet",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == "tool_denied"
        assert "matched deny pattern" in payload["detail"]

    def test_json_output(
        self, fake_litellm: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--no-ctx-tools",
                "--json",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["stop_reason"] == "completed"
        assert payload["final_message"] == "final answer"
        assert "usage" in payload
        assert "session_id" in payload

    def test_model_required(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit):
            main(["run", "--task", "hi"])

    def test_task_required(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        with pytest.raises(SystemExit):
            main(["run", "--model", "ollama/x"])

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            ("--max-iterations", "0"),
            ("--max-tokens", "0"),
            ("--budget-tokens", "0"),
            ("--budget-usd", "0"),
            ("--evaluator-rounds", "0"),
        ],
    )
    def test_positive_numeric_flags_reject_zero(
        self, flag: str, value: str,
    ) -> None:
        with pytest.raises(SystemExit):
            main(["run", "--model", "ollama/x", "--task", "hi", flag, value])

    def test_invalid_session_id_returns_error(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--session-id", "../bad",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "invalid session_id" in captured.err

    def test_no_ctx_tools_skips_extra_tools(
        self, fake_litellm: Any, tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        # Check the call passed tools=None (or no tools).
        first_call = fake_litellm._calls[0]
        assert "tools" not in first_call  # loop passes None → omitted

    def test_system_prompt_override(
        self, fake_litellm: Any, tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--system-prompt", "be terse",
                "--sessions-dir", str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        first_call = fake_litellm._calls[0]
        msgs = first_call["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "be terse"

    def test_system_prompt_from_stdin(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO("stdin-prompt"))
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--system-prompt", "-",
                "--sessions-dir", str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        first_call = fake_litellm._calls[0]
        assert first_call["messages"][0]["content"] == "stdin-prompt"

    def test_runtime_lifecycle_events_recorded(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        exit_code = main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--sessions-dir", str(tmp_path / "sessions"),
                "--session-id", "lifecycle-run",
                "--quiet",
            ]
        )
        assert exit_code == 0
        capsys.readouterr()

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
        ]
        assert events[0]["session_id"] == "lifecycle-run"
        assert events[0]["payload"]["task"] == "hi"


# ── Subcommand: sessions ──────────────────────────────────────────────────


class TestSessionsCommand:
    def test_detail_missing_session_returns_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            ["sessions", "not-there", "--sessions-dir", str(tmp_path)]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "session log not found" in captured.err

    def test_list_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["sessions", "--sessions-dir", str(tmp_path)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no sessions" in captured.out.lower()

    def test_list_after_run(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "hi",
                "--sessions-dir", str(tmp_path),
                "--session-id", "listed",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()  # drop run output
        main(["sessions", "--sessions-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert "listed" in captured.out

    def test_list_json(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for sid in ("alpha", "beta"):
            main(
                [
                    "run",
                    "--model", "ollama/x",
                    "--task", "hi",
                    "--sessions-dir", str(tmp_path),
                    "--session-id", sid,
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )
        capsys.readouterr()
        main(["sessions", "--sessions-dir", str(tmp_path), "--json"])
        captured = capsys.readouterr()
        ids = json.loads(captured.out)
        assert ids == ["alpha", "beta"]

    def test_detail_view(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "detail-task",
                "--sessions-dir", str(tmp_path),
                "--session-id", "detail",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        exit_code = main(
            ["sessions", "detail", "--sessions-dir", str(tmp_path)]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "detail" in captured.out
        assert "detail-task" in captured.out

    def test_detail_json(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "jdetail",
                "--sessions-dir", str(tmp_path),
                "--session-id", "jdetail",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        main(
            [
                "sessions", "jdetail",
                "--sessions-dir", str(tmp_path),
                "--json",
            ]
        )
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["session_id"] == "jdetail"
        assert payload["metadata"]["task"] == "jdetail"


# ── Subcommand: resume ────────────────────────────────────────────────────

class TestResumeCommand:
    @staticmethod
    def _write_session_with_mcp(tmp_path: Path, session_id: str) -> None:
        (tmp_path / f"{session_id}.jsonl").write_text(
            json.dumps({
                "type": "session_start",
                "ts": "t",
                "session_id": session_id,
                "task": "old",
                "model": "ollama/x",
                "ctx_tools_enabled": False,
                "mcp": [
                    {
                        "name": "danger",
                        "command": "definitely-not-a-real-mcp-command",
                        "args": ["--from-session"],
                    }
                ],
            })
            + "\n",
            encoding="utf-8",
        )

    def test_resume_after_initial_run(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Initial run.
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "first",
                "--sessions-dir", str(tmp_path),
                "--session-id", "resumable",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        # Resume.
        exit_code = main(
            [
                "resume", "resumable",
                "--task", "follow-up",
                "--sessions-dir", str(tmp_path),
                "--quiet",
            ]
        )
        assert exit_code == 0
        # Session file now has BOTH runs — count 'stop' events.
        text = (tmp_path / "resumable.jsonl").read_text(encoding="utf-8")
        stop_count = sum(
            1 for line in text.splitlines()
            if line and json.loads(line)["type"] == "stop"
        )
        assert stop_count == 2

    def test_resume_records_runtime_lifecycle_events(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        sessions_dir = tmp_path / "sessions"
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "first",
                "--sessions-dir", str(sessions_dir),
                "--session-id", "lifecycle-resume",
                "--quiet",
            ]
        )
        capsys.readouterr()

        exit_code = main(
            [
                "resume", "lifecycle-resume",
                "--task", "follow-up",
                "--sessions-dir", str(sessions_dir),
                "--quiet",
            ]
        )
        assert exit_code == 0

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
            "dev_event",
            "session_end",
        ]
        assert events[2]["event_type"] == "resume_task"
        assert events[2]["payload"]["task"] == "follow-up"

    def test_resume_reuses_recorded_provider_settings(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LOCAL_KEY", "secret-value")
        main(
            [
                "run",
                "--model", "openai/local-model",
                "--task", "first",
                "--sessions-dir", str(tmp_path),
                "--session-id", "provider-resume",
                "--api-key-env", "LOCAL_KEY",
                "--base-url", "http://127.0.0.1:8000/v1",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        fake_litellm._calls.clear()

        exit_code = main(
            [
                "resume", "provider-resume",
                "--task", "follow-up",
                "--sessions-dir", str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        resume_call = fake_litellm._calls[-1]
        assert resume_call["api_base"] == "http://127.0.0.1:8000/v1"
        assert resume_call["api_key"] == "secret-value"

    def test_resume_inherits_recorded_tool_policy(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model", "ollama/x",
                "--task", "first",
                "--sessions-dir", str(tmp_path),
                "--session-id", "policy-resume",
                "--deny-tool", "ctx__wiki_get",
                "--quiet",
            ]
        )
        capsys.readouterr()

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            return _tool_call_completion("ctx__wiki_get")

        fake_litellm.completion = completion
        exit_code = main(
            [
                "resume", "policy-resume",
                "--task", "follow-up",
                "--sessions-dir", str(tmp_path),
                "--json",
                "--quiet",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == "tool_denied"

    def test_resume_skips_recorded_mcp_by_default(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_session_with_mcp(tmp_path, "tampered")
        exit_code = main(
            [
                "resume", "tampered",
                "--task", "follow-up",
                "--sessions-dir", str(tmp_path),
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "recorded MCP server(s) skipped" in captured.err

    def test_resume_restores_recorded_mcp_only_with_flag(
        self, fake_litellm: Any, tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        restored: list[Any] = []

        class FakeRouter:
            def __init__(self, configs: list[Any]) -> None:
                restored.extend(configs)

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def list_tools(self) -> list[Any]:
                return []

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                raise AssertionError(f"unexpected tool call: {name} {arguments}")

        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        self._write_session_with_mcp(tmp_path, "restore-mcp")
        exit_code = main(
            [
                "resume", "restore-mcp",
                "--task", "follow-up",
                "--sessions-dir", str(tmp_path),
                "--restore-session-mcp",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert len(restored) == 1
        assert restored[0].command == "definitely-not-a-real-mcp-command"
        assert "restoring MCP server danger" in captured.err

    def test_resume_without_model_in_session_requires_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Hand-write a session log with no 'model' in metadata.
        path = tmp_path / "no-model.jsonl"
        path.write_text(
            json.dumps({"type": "session_start", "ts": "t",
                        "session_id": "no-model", "task": "old"})
            + "\n"
            + json.dumps({"type": "message", "ts": "t",
                          "session_id": "no-model",
                          "role": "user", "content": "hi"})
            + "\n",
            encoding="utf-8",
        )
        exit_code = main(
            ["resume", "no-model", "--task", "go",
             "--sessions-dir", str(tmp_path)]
        )
        assert exit_code == 1

    def test_resume_missing_session(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            ["resume", "not-there", "--task", "go",
             "--sessions-dir", str(tmp_path)]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "session log not found" in captured.err
