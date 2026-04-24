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

from ctx.cli.run import (
    _MCP_PRESETS,
    _PROVIDER_KEY_ENV,
    _model_provider_prefix,
    _parse_mcp_spec,
    _resolve_api_key_env,
    main,
)


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

    def test_explicit_single_command(self) -> None:
        cfg = _parse_mcp_spec("raw:myserver")
        assert cfg.name == "raw"
        assert cfg.command == "myserver"
        assert cfg.args == ()

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
        assert event["budget_usd"] == 1.5

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


# ── Subcommand: sessions ──────────────────────────────────────────────────


class TestSessionsCommand:
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
            1 for l in text.splitlines()
            if l and json.loads(l)["type"] == "stop"
        )
        assert stop_count == 2

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
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(FileNotFoundError):
            main(
                ["resume", "not-there", "--task", "go",
                 "--sessions-dir", str(tmp_path)]
            )
