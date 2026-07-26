"""
test_harness_cli_run.py -- `ctx run` / `ctx resume` / `ctx sessions` CLI.

Tests mock LiteLLM and use fake or controlled MCP routers so no real provider
network happens. The goal is pinning:

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

import hashlib
import io
import json
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, cast

import pytest

import ctx.adapters.generic.runtime_lifecycle as runtime_lifecycle
import ctx.adapters.generic.tools.mcp_router as mcp_router_module
import ctx.cli.run as run_cli
import ctx.telemetry as telemetry
from ctx.adapters.generic.adaptive_runtime import SelectedSkill
from ctx.adapters.generic.evaluator import EvaluationLoopResult
from ctx.adapters.generic.loop import LoopResult
from ctx.cli.run import (
    _apply_mcp_env_overlays,
    _compile_tool_policy,
    _model_provider_prefix,
    _parse_mcp_spec,
    _resolve_api_key_env,
    _split_mcp_invocation,
    main,
)
from ctx.adapters.generic.providers import ToolCall, ToolDefinition, Usage
from ctx.telemetry import read_events, record_event as real_record_event


_MCP_FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


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
    fake._calls = calls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


def _tool_call_completion(
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments or {}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }


def _submitted_tool_names(call: dict[str, Any]) -> set[str]:
    return {item["function"]["name"] for item in call.get("tools", [])}


def _enable_real_telemetry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    path = tmp_path / "telemetry" / "events.jsonl"
    config = {
        "enabled": True,
        "mode": "local_redacted",
        "path": str(path),
        "export": {"enabled": False},
    }

    def config_get(key: str, default: Any) -> Any:
        return config if key == "telemetry" else default

    monkeypatch.setattr(telemetry, "_config_get", config_get)
    monkeypatch.setattr(telemetry, "record_event", real_record_event)
    monkeypatch.setattr(run_cli, "record_event", real_record_event)
    monkeypatch.setattr(runtime_lifecycle, "record_event", real_record_event)
    return path


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
        assert _resolve_api_key_env(None, "huggingface/org-model", None) == "HF_TOKEN"

    def test_inferred_from_provider_flag(self) -> None:
        assert _resolve_api_key_env(None, "custom-x", "openai") == "OPENAI_API_KEY"
        assert _resolve_api_key_env(None, "custom-x", "huggingface") == "HF_TOKEN"

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
        assert "GITHUB_TOKEN" in cfg.credential_env

    def test_explicit_form(self) -> None:
        cfg = _parse_mcp_spec("fs:npx -y pkg /tmp")
        assert cfg.name == "fs"
        assert cfg.command == "npx"
        assert cfg.args == ("-y", "pkg", "/tmp")
        with_env = _apply_mcp_env_overlays([cfg], ["fs:MY_MCP_TOKEN"])[0]
        assert with_env.credential_env == ("MY_MCP_TOKEN",)

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

    @pytest.mark.parametrize(
        "spec",
        [
            "fs:npx server --token secret-value",
            "fs:npx server --api-key=secret-value",
            "fs:npx server GITHUB_TOKEN=secret-value",
            "fs:npx server ghp_1234567890abcdefghijkl",
        ],
    )
    def test_explicit_form_rejects_inline_secret_args(self, spec: str) -> None:
        with pytest.raises(SystemExit, match="--mcp-env"):
            _parse_mcp_spec(spec)

    def test_explicit_form_allows_secret_indirection_args(self) -> None:
        cfg = _parse_mcp_spec(
            "fs:npx server --token-file /run/secrets/token --credential-env GITHUB_TOKEN"
        )

        assert cfg.args == (
            "server",
            "--token-file",
            "/run/secrets/token",
            "--credential-env",
            "GITHUB_TOKEN",
        )

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

    def test_adaptive_mcp_grants_require_namespaced_patterns(self) -> None:
        configs = [_parse_mcp_spec("alpha:ignored"), _parse_mcp_spec("beta:ignored")]

        assert run_cli._adaptive_mcp_server_names(configs, ("alpha*",)) == ()
        assert run_cli._adaptive_mcp_server_names(configs, ("*",)) == (
            "alpha",
            "beta",
        )
        assert run_cli._adaptive_mcp_server_names(configs, (), ("*",)) == ()
        assert run_cli._adaptive_mcp_server_names(
            configs,
            ("ctx__*", "alpha__read*"),
        ) == ("alpha",)
        assert (
            run_cli._adaptive_mcp_server_names(
                configs,
                ("alpha__*",),
                ("alpha__*",),
            )
            == ()
        )


class TestRunCommand:
    def _write_model_profile(self, root: Path, data: dict[str, Any]) -> None:
        root.mkdir(parents=True)
        (root / "ctx-model-profile.json").write_text(
            json.dumps(data),
            encoding="utf-8",
        )

    def test_happy_path_writes_session(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "say hi",
                "--sessions-dir",
                str(tmp_path),
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

    def test_run_passes_session_id_to_mcp_router(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        router_session_ids: list[str | None] = []

        class FakeRouter:
            started = False

            def __init__(self, configs: list[Any], *, session_id: str | None = None) -> None:
                assert len(configs) == 1
                router_session_ids.append(session_id)

            def start(self) -> None:
                calls.append("start")
                self.started = True

            def stop(self) -> None:
                calls.append("stop")
                self.started = False

            def list_tools(self) -> list[Any]:
                assert self.started
                calls.append("list_tools")
                return []

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                raise AssertionError(f"unexpected tool call: {name} {arguments}")

        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "say hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "mcp-run",
                "--mcp",
                "raw:ignored-command",
                "--no-ctx-tools",
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        assert router_session_ids == ["mcp-run"]
        assert calls == ["start", "list_tools", "stop"]

    def test_adaptive_mcp_is_one_turn_and_policy_bounded(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        events: list[tuple[str, Any]] = []
        routers: list[Any] = []

        class FakeRouter:
            def __init__(
                self,
                configs: list[Any],
                *,
                session_id: str | None = None,
                lazy: bool = False,
            ) -> None:
                assert len(configs) == 2 and session_id == "bounded-mcp"
                assert lazy is True
                self.lazy = lazy
                self.active: tuple[str, ...] = ()
                routers.append(self)

            def start(self) -> None:
                events.append(("start", self.active))

            def stop(self) -> None:
                assert self.active == ()
                events.append(("stop", self.active))

            @property
            def server_names(self) -> list[str]:
                return sorted(self.active)

            def list_tools(self) -> list[ToolDefinition]:
                assert self.active == ()
                events.append(("list_tools", self.active))
                return []

            def activate(
                self,
                server_names: tuple[str, ...],
                *,
                capability_epoch: int | None = None,
            ) -> list[ToolDefinition]:
                assert self.active == ()
                assert capability_epoch == 1
                self.active = tuple(server_names)
                events.append(("activate", self.active))
                return [
                    ToolDefinition(name="server__read", description="read", parameters={}),
                    ToolDefinition(name="server__write", description="write", parameters={}),
                ]

            def deactivate(self, server_names: tuple[str, ...]) -> None:
                assert tuple(server_names) == self.active
                events.append(("deactivate", self.active))
                self.active = ()

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                assert self.active == ("server",)
                assert name == "server__read"
                events.append(("call", name))
                return "bounded result"

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            events.append(("provider", tuple(sorted(_submitted_tool_names(kwargs)))))
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("server__read")
            assert routers[0].active == ()
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "say hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "bounded-mcp",
                "--mcp",
                "server:ignored-command",
                "--mcp",
                "other:ignored-command",
                "--allow-tool",
                "server__read",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert [_submitted_tool_names(call) for call in fake_litellm._calls] == [
            {"server__read"},
            set(),
        ]
        assert events == [
            ("start", ()),
            ("list_tools", ()),
            ("activate", ("server",)),
            ("provider", ("server__read",)),
            ("call", "server__read"),
            ("deactivate", ("server",)),
            ("provider", ()),
            ("stop", ()),
        ]
        metadata = run_cli.load_session("bounded-mcp", sessions_dir=tmp_path).metadata
        assert metadata["ctx_adaptive"]["enabled"] is True
        assert metadata["ctx_adaptive"]["mcp_configured_count"] == 2
        assert metadata["ctx_adaptive"]["mcp_activated_count"] == 1
        assert metadata["ctx_adaptive"]["mcp_fetched_tool_count"] == 2
        assert metadata["ctx_adaptive"]["mcp_submitted_tool_count"] == 1
        assert metadata["ctx_adaptive"]["mcp_submitted_schema_bytes"] > 0
        assert metadata["ctx_adaptive"]["mcp_estimated_schema_tokens"] > 0
        assert metadata["ctx_adaptive"]["mcp_schema_submission_attempted"] is True
        lifecycle_events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        mcp_events = [
            event for event in lifecycle_events if event.get("entity_type") == "mcp-server"
        ]
        assert [(event["action"], event["slug"]) for event in mcp_events] == [
            ("load_requested", "server"),
            ("load_applied", "server"),
            ("used", "server"),
            ("unload_applied", "server"),
        ]

    def test_adaptive_mcp_real_process_stays_lazy_with_ctx_subgroup(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        procs: list[subprocess.Popen[bytes]] = []
        real_popen = mcp_router_module.subprocess.Popen

        def capture_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
            proc = real_popen(*args, **kwargs)
            procs.append(proc)
            return proc

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("live__echo", {"text": "real"})
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        monkeypatch.setattr(mcp_router_module.subprocess, "Popen", capture_popen)
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )
        fake_litellm.completion = completion

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use one live tool",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "real-lazy-mcp",
                "--mcp",
                f"live:{sys.executable} {_MCP_FIXTURE}",
                "--mcp",
                "other:definitely-not-a-real-command",
                "--allow-tool",
                "ctx__recommend_bundle",
                "--allow-tool",
                "live__echo",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert [_submitted_tool_names(call) for call in fake_litellm._calls] == [
            {"ctx__recommend_bundle", "live__echo"},
            set(),
        ]
        assert len(procs) == 1
        procs[0].wait(timeout=2.0)
        assert procs[0].poll() is not None
        metadata = run_cli.load_session("real-lazy-mcp", sessions_dir=tmp_path).metadata
        assert metadata["ctx_adaptive"]["mcp_fetched_tool_count"] >= 2
        assert metadata["ctx_adaptive"]["mcp_submitted_tool_count"] == 1
        assert metadata["ctx_adaptive"]["mcp_schema_submission_attempted"] is True

    def test_adaptive_mcp_policy_denial_is_not_recorded_as_use(self, tmp_path: Path) -> None:
        class FakeRouter:
            def activate(
                self,
                server_names: tuple[str, ...],
                *,
                capability_epoch: int | None = None,
            ) -> list[ToolDefinition]:
                assert capability_epoch == 1
                return []

            def deactivate(self, server_names: tuple[str, ...]) -> None:
                pass

        lifecycle_dir = tmp_path / "runtime"
        lifecycle = run_cli.RuntimeLifecycleStore(root=lifecycle_dir)
        controller = run_cli._AdaptiveMcpController(
            run_cli.AdaptiveRuntimeController(None),
            router=cast(Any, FakeRouter()),
            server_names=("server",),
            configured_count=1,
            lifecycle=lifecycle,
            session_id="denied-mcp",
            allow_patterns=("server__read",),
            deny_patterns=(),
        )
        preparation = controller.prepare_turn(
            1,
            (),
            (),
            deadline_monotonic=None,
            cancel_event=None,
        )
        controller.activate_turn(1, preparation.capability_epoch)
        controller.on_tool_result(
            1,
            preparation.capability_epoch,
            ToolCall(id="denied", name="server__hidden", arguments={}),
            "",
            "policy: capability was not advertised",
        )
        controller.on_tool_result(
            1,
            preparation.capability_epoch,
            ToolCall(id="malformed", name="server__read", arguments={}),
            "",
            "invalid tool call arguments: malformed JSON",
        )
        controller.close_turn(1, preparation.capability_epoch, "tool_denied")

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "load_applied",
            "unload_applied",
        ]

    def test_run_uses_ctx_init_model_profile_when_model_omitted(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        claude = tmp_path / "claude"
        sessions = tmp_path / "sessions"
        self._write_model_profile(
            claude,
            {
                "mode": "custom",
                "provider": "openai",
                "model": "openai/gpt-5.5",
                "api_key_env": "OPENAI_API_KEY",
                "base_url": "https://api.example.test/v1",
                "goal": "build agents",
            },
        )
        monkeypatch.setattr(run_cli, "_claude_dir", lambda: claude)
        monkeypatch.setenv("OPENAI_API_KEY", "profile-secret")

        exit_code = main(
            [
                "run",
                "--task",
                "say hi",
                "--sessions-dir",
                str(sessions),
                "--session-id",
                "profile-run",
                "--no-ctx-tools",
                "--quiet",
            ],
        )

        assert exit_code == 0
        call = fake_litellm._calls[0]
        assert call["model"] == "openai/gpt-5.5"
        assert call["api_key"] == "profile-secret"
        assert call["api_base"] == "https://api.example.test/v1"
        first_line = (
            (sessions / "profile-run.jsonl")
            .read_text(
                encoding="utf-8",
            )
            .splitlines()[0]
        )
        event = json.loads(first_line)
        assert event["model"] == "openai/gpt-5.5"
        assert event["provider"] == "openai"
        assert event["api_key_env"] == "OPENAI_API_KEY"
        captured = capsys.readouterr()
        assert "final answer" in captured.out

    def test_cli_model_override_does_not_inherit_stale_profile_provider(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        claude = tmp_path / "claude"
        sessions = tmp_path / "sessions"
        self._write_model_profile(
            claude,
            {
                "mode": "custom",
                "provider": "openai",
                "model": "openai/gpt-5.5",
                "api_key_env": "OPENAI_API_KEY",
                "base_url": "https://api.example.test/v1",
            },
        )
        monkeypatch.setattr(run_cli, "_claude_dir", lambda: claude)
        monkeypatch.setenv("OPENAI_API_KEY", "profile-secret")

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/qwen",
                "--task",
                "say hi",
                "--sessions-dir",
                str(sessions),
                "--session-id",
                "override-run",
                "--no-ctx-tools",
                "--quiet",
            ],
        )

        assert exit_code == 0
        call = fake_litellm._calls[0]
        assert call["model"] == "ollama/qwen"
        assert "api_key" not in call
        assert "api_base" not in call
        first_line = (
            (sessions / "override-run.jsonl")
            .read_text(
                encoding="utf-8",
            )
            .splitlines()[0]
        )
        event = json.loads(first_line)
        assert event["provider"] == "ollama"
        assert event["api_key_env"] == ""
        assert event["base_url"] == ""

    def test_session_id_flag_pins_id(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "pinned-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert (tmp_path / "pinned-session.jsonl").is_file()

    def test_session_id_reuse_is_rejected_without_overwrite(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        path = tmp_path / "pinned-session.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "pinned-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert path.read_text(encoding="utf-8") == "sentinel\n"
        assert "already exists" in captured.err
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.run"
        ]
        assert len(events) == 1
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "FileExistsError"
        assert failed.payload["ctx.run.phase"] == "failed"
        assert failed.payload["ctx.run.failure_stage"] == "session_create"
        assert "hi" not in telemetry_path.read_text(encoding="utf-8")

    def test_session_id_reuse_can_overwrite_with_flag(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        path = tmp_path / "pinned-session.jsonl"
        path.write_text("sentinel\n", encoding="utf-8")
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/llama3",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "pinned-session",
                "--overwrite-session",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        assert exit_code == 0
        assert "sentinel" not in path.read_text(encoding="utf-8")

    def test_metadata_recorded(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "openrouter/anthropic/claude",
                "--task",
                "task-content",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "meta-test",
                "--no-ctx-tools",
                "--budget-usd",
                "1.5",
                "--api-key-env",
                "CUSTOM_OPENROUTER_KEY",
                "--base-url",
                "https://openrouter.example/api",
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
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "policy-meta",
                "--allow-tool",
                "ctx__*",
                "--deny-tool",
                "ctx__wiki_get",
                "--quiet",
            ]
        )
        first_line = (tmp_path / "policy-meta.jsonl").read_text(encoding="utf-8").splitlines()[0]
        event = json.loads(first_line)
        assert event["tool_policy"] == {
            "allow": ["ctx__*"],
            "deny": ["ctx__wiki_get"],
        }

    def test_deny_tool_blocks_model_tool_call(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            return _tool_call_completion("ctx__wiki_get")

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "call denied tool",
                "--sessions-dir",
                str(tmp_path),
                "--ctx-tool-surface",
                "minimal",
                "--deny-tool",
                "ctx__wiki_get",
                "--json",
                "--quiet",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == "tool_denied"
        assert "matched deny pattern" in payload["detail"]
        assert _submitted_tool_names(fake_litellm._calls[0]) == {"ctx__recommend_bundle"}

    @pytest.mark.parametrize(
        "stop_reason,final_message,detail",
        [
            ("length", "partial", "provider truncated response"),
            ("empty_response", "", "empty content with no tool calls"),
            ("provider_other", "partial", "unexpected finish_reason='other'"),
            ("content_filter", "", "provider reported content_filter finish"),
        ],
    )
    def test_abnormal_stop_reasons_exit_nonzero_in_json_mode(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        stop_reason: str,
        final_message: str,
        detail: str,
    ) -> None:
        def fake_run_loop(**_kwargs: Any) -> types.SimpleNamespace:
            return types.SimpleNamespace(
                stop_reason=stop_reason,
                final_message=final_message,
                iterations=1,
                usage=Usage(input_tokens=5, output_tokens=1),
                messages=(),
                detail=detail,
            )

        monkeypatch.setattr(run_cli, "run_loop", fake_run_loop)

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "abnormal stop",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--json",
                "--quiet",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == stop_reason
        assert payload["final_message"] == final_message
        assert payload["detail"] == detail
        assert payload["usage"] == {
            "tokens_reported": True,
            "input_tokens": 5,
            "output_tokens": 1,
            "total_tokens": 6,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }

    def test_provider_timeout_reaches_real_run_loop(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            time.sleep(0.2)
            return {
                "choices": [
                    {
                        "message": {"content": "late", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        started = time.perf_counter()
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "timeout",
                "--sessions-dir",
                str(tmp_path),
                "--provider-timeout",
                "0.01",
                "--no-ctx-tools",
                "--json",
                "--quiet",
            ]
        )
        elapsed = time.perf_counter() - started

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert elapsed < 1.0
        assert payload["stop_reason"] == "provider_timeout"
        assert payload["detail"] == "provider call timed out after 0.010s"
        assert payload["usage"] == {
            "tokens_reported": False,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }

    def test_json_output(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
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
        assert payload["usage_attribution"] == {
            "scope": "session",
            "attribution": "unavailable",
            "attribution_reason": run_cli._SESSION_USAGE_ATTRIBUTION_REASON,
            "tokens_reported": True,
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }

    def test_usage_telemetry_exposes_cached_and_uncached_tokens(self) -> None:
        result = types.SimpleNamespace(
            stop_reason="completed",
            iterations=1,
            usage=Usage(
                input_tokens=10,
                output_tokens=3,
                cost_usd=0.01,
                cached_input_tokens=6,
            ),
        )

        payload = run_cli._loop_result_payload(result)

        assert payload["ctx.usage.tokens_reported"] is True
        assert payload["ctx.usage.total_tokens"] == 13
        assert payload["ctx.usage.cached_input_tokens"] == 6
        assert payload["ctx.usage.uncached_input_tokens"] == 4
        assert payload["ctx.usage.cost_present"] is True

        invalid_cache = run_cli._usage_token_fields(
            Usage(input_tokens=5, output_tokens=1, cached_input_tokens=8)
        )
        assert invalid_cache["cached_input_tokens"] == 8
        assert invalid_cache["uncached_input_tokens"] is None

    def test_model_required(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(run_cli, "_claude_dir", lambda: tmp_path / "claude")

        assert main(["run", "--task", "hi"]) == 2
        assert "--model is required" in capsys.readouterr().err

    def test_missing_harness_extra_returns_friendly_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        def missing_extra(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(
                "litellm is required for the generic harness provider. "
                "Install with: pip install 'claude-ctx[harness]'"
            )

        monkeypatch.setattr(run_cli, "run_loop", missing_extra)

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )

        captured = capsys.readouterr()
        assert exit_code == 2
        assert "claude-ctx[harness]" in captured.err
        assert "Traceback" not in captured.err

    def test_task_required(
        self,
        capsys: pytest.CaptureFixture[str],
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
        self,
        flag: str,
        value: str,
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
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "../bad",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "invalid session_id" in captured.err

    def test_no_ctx_tools_skips_extra_tools(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        # Check the call passed tools=None (or no tools).
        first_call = fake_litellm._calls[0]
        assert "tools" not in first_call  # loop passes None → omitted
        assert "ctx__" not in first_call["messages"][0]["content"]

    def test_non_ctx_allow_pattern_removes_ctx_prompt_and_schemas(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use only an attached server",
                "--sessions-dir",
                str(tmp_path),
                "--allow-tool",
                "server__*",
                "--quiet",
            ]
        )

        assert exit_code == 0
        first_call = fake_litellm._calls[0]
        assert "tools" not in first_call
        assert "ctx__" not in first_call["messages"][0]["content"]

    def test_ctx_tools_default_to_adaptive_host_surface(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use ctx only if needed",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert len(fake_litellm._calls) == 1
        call = fake_litellm._calls[0]
        assert _submitted_tool_names(call) == set()
        assert "ctx__" not in call["messages"][0]["content"]
        metadata = json.loads(next((tmp_path.glob("*.jsonl"))).read_text().splitlines()[0])
        assert metadata["ctx_tool_surface"] == "adaptive"
        assert metadata["ctx_tool_names"] == []
        assert metadata["ctx_adaptive"]["enabled"] is True

    def test_adaptive_stays_zero_schema_when_secure_reads_unavailable(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "ctx.adapters.generic.adaptive_runtime.secure_skill_reads_available",
            lambda: False,
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use ctx",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert _submitted_tool_names(fake_litellm._calls[0]) == set()
        metadata = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[0])
        assert metadata["ctx_tool_surface"] == "adaptive"
        assert metadata["ctx_adaptive"]["enabled"] is True
        assert metadata["ctx_adaptive"]["skill_selected"] is False

    def test_adaptive_skill_is_request_only_and_not_persisted(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = "EPHEMERAL-CLI-SKILL-BODY"
        selected = SelectedSkill(
            name="focused-skill",
            content=body,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            content_bytes=len(body.encode()),
            score=42.0,
            matched_terms=("focused",),
        )
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(selected, selection_duration_ms=1.5)),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use focused guidance",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "adaptive-ephemeral",
                "--quiet",
            ]
        )

        assert exit_code == 0
        call = fake_litellm._calls[0]
        assert _submitted_tool_names(call) == set()
        assert body in "\n".join(message["content"] for message in call["messages"])
        session_text = (tmp_path / "adaptive-ephemeral.jsonl").read_text(encoding="utf-8")
        assert body not in session_text
        metadata = run_cli.load_session(
            "adaptive-ephemeral",
            sessions_dir=tmp_path,
        ).metadata
        assert metadata["ctx_adaptive"]["selected_context_bytes"] > len(body.encode())
        assert (
            metadata["ctx_adaptive"]["submitted_context_bytes"]
            == metadata["ctx_adaptive"]["selected_context_bytes"]
        )
        assert metadata["ctx_adaptive"]["estimated_selected_context_tokens"] > 0
        assert metadata["ctx_adaptive"]["skill_hash"]

    def test_evaluator_reports_complete_usage(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        final = LoopResult(
            stop_reason="completed",
            final_message="done",
            iterations=1,
            usage=Usage(input_tokens=5, output_tokens=10),
            messages=(),
        )
        monkeypatch.setattr(
            run_cli,
            "run_with_evaluation",
            lambda **_kwargs: EvaluationLoopResult(
                final=final,
                rounds=(),
                plan=None,
                contract=None,
                total_usage=Usage(input_tokens=12, output_tokens=21),
            ),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "evaluate usage",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "evaluator-usage",
                "--no-ctx-tools",
                "--evaluator",
                "--json",
                "--quiet",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["usage"] == {
            "tokens_reported": True,
            "input_tokens": 12,
            "output_tokens": 21,
            "total_tokens": 33,
            "cached_input_tokens": None,
            "uncached_input_tokens": None,
            "cost_usd": None,
        }
        state = run_cli.load_session("evaluator-usage", sessions_dir=tmp_path)
        assert state.usage == Usage(input_tokens=12, output_tokens=21)
        events = [json.loads(line) for line in state.path.read_text().splitlines()]
        stop_events = [event for event in events if event["type"] == "stop"]
        assert len(stop_events) == 1
        assert stop_events[0]["usage"]["input_tokens"] == 12
        assert stop_events[0]["usage"]["output_tokens"] == 21

    def test_evaluator_failure_persists_one_stop_with_completed_round_usage(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        partial = LoopResult(
            stop_reason="completed",
            final_message="partial answer",
            iterations=2,
            usage=Usage(input_tokens=5, output_tokens=10),
            messages=(),
        )

        def fail_after_generator(**kwargs: Any) -> None:
            kwargs["observer"].on_stop(partial)
            raise RuntimeError("private evaluator failure")

        monkeypatch.setattr(run_cli, "run_with_evaluation", fail_after_generator)

        with pytest.raises(RuntimeError, match="private evaluator failure"):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "evaluate failure",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    "evaluator-failure",
                    "--no-ctx-tools",
                    "--evaluator",
                    "--quiet",
                ]
            )

        state = run_cli.load_session("evaluator-failure", sessions_dir=tmp_path)
        assert state.stopped is True
        assert state.stop_reason == "provider_error"
        assert state.usage == Usage(input_tokens=5, output_tokens=10)
        assert state.metadata["ctx_usage"] == {
            "complete": False,
            "scope": "completed_generator_rounds",
        }
        events = [json.loads(line) for line in state.path.read_text().splitlines()]
        stop_events = [event for event in events if event["type"] == "stop"]
        assert len(stop_events) == 1
        assert stop_events[0]["detail"] == ("evaluator orchestration failed: RuntimeError")
        assert "private evaluator failure" not in state.path.read_text(encoding="utf-8")

    def test_cleanup_metadata_error_does_not_mask_evaluator_failure(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fail_evaluator(**_kwargs: Any) -> None:
            raise RuntimeError("primary evaluator failure")

        def fail_session_config(
            _store: run_cli.SessionStore,
            _config: dict[str, Any],
        ) -> None:
            raise KeyError("cleanup exploded")

        monkeypatch.setattr(run_cli, "run_with_evaluation", fail_evaluator)
        monkeypatch.setattr(
            run_cli.SessionStore,
            "write_session_config",
            fail_session_config,
        )

        with pytest.raises(RuntimeError, match="primary evaluator failure"):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "evaluate cleanup",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    "evaluator-cleanup-failure",
                    "--no-ctx-tools",
                    "--evaluator",
                    "--quiet",
                ]
            )

    def test_explicit_minimal_surface_keeps_bootstrap_tools_on_every_iteration(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("ctx__wiki_get")
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use ctx only if needed",
                "--sessions-dir",
                str(tmp_path),
                "--ctx-tool-surface",
                "minimal",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert len(fake_litellm._calls) == 2
        assert all(
            _submitted_tool_names(call) == {"ctx__recommend_bundle", "ctx__wiki_get"}
            for call in fake_litellm._calls
        )
        prompt = fake_litellm._calls[0]["messages"][0]["content"]
        assert "ctx__recommend_bundle" in prompt
        assert "ctx__wiki_get" in prompt
        assert "ctx runtime session id:" not in prompt
        assert "ctx__mark_entity_used" not in prompt

    def test_allow_tool_submits_and_executes_only_selected_ctx_schema(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion(
                    "ctx__mark_entity_used",
                    {
                        "entity_type": "skill",
                        "slug": "focused-skill",
                        "evidence": "used in focused test",
                    },
                )
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use the selected skill",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "selected-schema",
                "--allow-tool",
                "ctx__mark_entity_used",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert all(
            _submitted_tool_names(call) == {"ctx__mark_entity_used"} for call in fake_litellm._calls
        )
        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert "used" in [event["action"] for event in events]
        prompt = fake_litellm._calls[0]["messages"][0]["content"]
        assert "ctx__mark_entity_used.token_usage" in prompt
        assert "ctx__recommend_bundle" not in prompt
        assert "ctx__wiki_get" not in prompt
        assert "ctx__load_entity" not in prompt
        assert "ctx__unload_entity" not in prompt

    def test_full_ctx_tool_surface_preserves_previous_schema_inventory(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "use full ctx tooling",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "full-schema",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )

        expected = {
            definition.name
            for definition in run_cli.CtxCoreToolbox(
                bound_session_id="full-schema"
            ).tool_definitions()
        }
        submitted = _submitted_tool_names(fake_litellm._calls[0])
        assert submitted == expected
        assert len(submitted) == 13
        metadata = json.loads(
            (tmp_path / "full-schema.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert metadata["ctx_tool_surface"] == "full"
        assert set(metadata["ctx_tool_names"]) == expected

    def test_system_prompt_override(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--system-prompt",
                "be terse",
                "--sessions-dir",
                str(tmp_path),
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
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--system-prompt",
                "-",
                "--sessions-dir",
                str(tmp_path),
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
        telemetry_events: list[dict[str, Any]] = []

        def capture_record_event(event_name: str, **kwargs: Any) -> None:
            telemetry_events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(run_cli, "record_event", capture_record_event)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "lifecycle-run",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )
        assert exit_code == 0
        capsys.readouterr()

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
        ]
        assert events[0]["session_id"] == "lifecycle-run"
        assert "task" not in events[0]["payload"]
        assert events[0]["payload"]["task_hash"].startswith("sha256:")
        assert "hi" not in json.dumps(events[0])
        cli_events = [event for event in telemetry_events if event["event_name"] == "ctx.cli.run"]
        assert [event["payload"]["ctx.run.phase"] for event in cli_events] == [
            "started",
            "finished",
        ]
        assert cli_events[0]["payload"]["ctx.task.length"] == len("hi")
        finished_payload = cli_events[-1]["payload"]
        assert finished_payload["ctx.stop_reason"] == "completed"
        assert finished_payload["ctx.usage.scope"] == "session"
        assert finished_payload["ctx.usage.attribution"] == "unavailable"
        assert (
            finished_payload["ctx.usage.attribution_reason"]
            == run_cli._SESSION_USAGE_ATTRIBUTION_REASON
        )
        assert "hi" not in json.dumps([event["payload"] for event in cli_events])
        system_prompt = fake_litellm._calls[0]["messages"][0]["content"]
        assert "ctx__mark_entity_used.token_usage" in system_prompt
        assert "do not allocate session totals across tools" in system_prompt
        tool = next(
            item
            for item in fake_litellm._calls[0]["tools"]
            if item["function"]["name"] == "ctx__load_entity"
        )
        assert "session_id" not in tool["function"]["parameters"]["properties"]
        assert "session_id" not in tool["function"]["parameters"]["required"]

    def test_run_telemetry_correlates_cli_and_runtime_lifecycle(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "trace-run",
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        events = list(read_events(telemetry_path, trusted_root=tmp_path))
        cli_events = [event for event in events if event.event_name == "ctx.cli.run"]
        lifecycle_events = [
            event for event in events if event.event_name == "ctx.runtime_lifecycle.record"
        ]

        assert [event.payload["ctx.run.phase"] for event in cli_events] == [
            "started",
            "finished",
        ]
        assert [event.payload["ctx.lifecycle.action"] for event in lifecycle_events] == [
            "dev_event",
            "session_end",
        ]
        assert cli_events[0].trace_id is not None
        assert cli_events[0].span_id is not None
        assert cli_events[1].trace_id == cli_events[0].trace_id
        assert cli_events[1].span_id == cli_events[0].span_id
        assert all(event.trace_id == cli_events[0].trace_id for event in lifecycle_events)
        assert all(event.parent_span_id == cli_events[0].span_id for event in lifecycle_events)
        assert {event.span_id for event in lifecycle_events}.isdisjoint({cli_events[0].span_id})
        assert "hi" not in telemetry_path.read_text(encoding="utf-8")

    def test_run_exception_telemetry_hashes_provider_error(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)

        def fail_run_loop(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("private provider failure for /Users/example/private-repo")

        monkeypatch.setattr(run_cli, "run_loop", fail_run_loop)

        with pytest.raises(RuntimeError):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "private run task",
                    "--sessions-dir",
                    str(tmp_path / "sessions"),
                    "--session-id",
                    "trace-run-error",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )

        capsys.readouterr()
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.run"
        ]
        assert [event.payload["ctx.run.phase"] for event in events] == [
            "started",
            "failed",
        ]
        failed = events[-1]
        assert failed.outcome == "error"
        assert failed.error_kind == "RuntimeError"
        assert failed.payload["ctx.exception.message_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.stack_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.escaped"] is True
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "private provider failure" not in raw
        assert "/Users/example/private-repo" not in raw
        assert "private run task" not in raw

    def test_run_planner_failure_telemetry_has_trace_and_is_redacted(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)

        class FailingPlanner:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def plan(self, _task: str) -> None:
                raise RuntimeError("private planner failure for /Users/example/private-repo")

        monkeypatch.setattr(run_cli, "Planner", FailingPlanner)

        with pytest.raises(RuntimeError):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "private planner task",
                    "--sessions-dir",
                    str(tmp_path / "sessions"),
                    "--session-id",
                    "trace-planner-error",
                    "--planner",
                    "--no-ctx-tools",
                    "--quiet",
                ]
            )

        capsys.readouterr()
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.run"
        ]
        assert [event.payload["ctx.run.phase"] for event in events] == ["failed"]
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "RuntimeError"
        assert failed.payload["ctx.run.failure_stage"] == "planner"
        assert failed.payload["ctx.exception.message_hash"].startswith("sha256:")
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "private planner failure" not in raw
        assert "/Users/example/private-repo" not in raw
        assert "private planner task" not in raw


# ── Subcommand: sessions ──────────────────────────────────────────────────


class TestSessionsCommand:
    def test_detail_missing_session_returns_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["sessions", "not-there", "--sessions-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "session log not found" in captured.err

    def test_list_empty(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        exit_code = main(["sessions", "--sessions-dir", str(tmp_path)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "no sessions" in captured.out.lower()

    def test_list_after_run(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "hi",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "listed",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()  # drop run output
        main(["sessions", "--sessions-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert "listed" in captured.out

    def test_list_json(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        for sid in ("alpha", "beta"):
            main(
                [
                    "run",
                    "--model",
                    "ollama/x",
                    "--task",
                    "hi",
                    "--sessions-dir",
                    str(tmp_path),
                    "--session-id",
                    sid,
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
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "detail-task",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "detail",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        exit_code = main(["sessions", "detail", "--sessions-dir", str(tmp_path)])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "detail" in captured.out
        assert "detail-task" in captured.out

    def test_detail_json(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "jdetail",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "jdetail",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        main(
            [
                "sessions",
                "jdetail",
                "--sessions-dir",
                str(tmp_path),
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
            json.dumps(
                {
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
                            "credential_env": ["DANGER_TOKEN"],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def test_resume_after_initial_run(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Initial run.
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "resumable",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        # Resume.
        exit_code = main(
            [
                "resume",
                "resumable",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )
        assert exit_code == 0
        # Session file now has BOTH runs — count 'stop' events.
        text = (tmp_path / "resumable.jsonl").read_text(encoding="utf-8")
        stop_count = sum(
            1 for line in text.splitlines() if line and json.loads(line)["type"] == "stop"
        )
        assert stop_count == 2

    def test_legacy_session_without_surface_resumes_full_tool_inventory(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "legacy-surface.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "legacy-surface",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": True,
                    "system_prompt": run_cli._LEGACY_DEFAULT_SYSTEM_PROMPT,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        exit_code = main(
            [
                "resume",
                "legacy-surface",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert len(_submitted_tool_names(fake_litellm._calls[-1])) == 13

    def test_exact_legacy_prompt_does_not_advertise_filtered_ctx_tools(
        self,
        fake_litellm: Any,
        tmp_path: Path,
    ) -> None:
        legacy_prompt = (
            run_cli._LEGACY_DEFAULT_SYSTEM_PROMPT.rstrip()
            + "\n\nctx runtime session id: legacy-filtered\n"
            + "Use this exact session_id when calling ctx lifecycle tools. "
            + "Record ctx__load_entity and ctx__mark_entity_used when relevant.\n"
        )
        (tmp_path / "legacy-filtered.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "legacy-filtered",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": True,
                    "system_prompt": legacy_prompt,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        exit_code = main(
            [
                "resume",
                "legacy-filtered",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--allow-tool",
                "server__*",
                "--quiet",
            ]
        )

        assert exit_code == 0
        call = fake_litellm._calls[-1]
        assert "tools" not in call
        assert "ctx__" not in call["messages"][0]["content"]

    def test_resume_surface_override_rewrites_prompt_and_persists(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "surface-resume",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )
        capsys.readouterr()
        fake_litellm._calls.clear()

        main(
            [
                "resume",
                "surface-resume",
                "--task",
                "switch to minimal",
                "--sessions-dir",
                str(tmp_path),
                "--ctx-tool-surface",
                "minimal",
                "--quiet",
            ]
        )
        capsys.readouterr()
        explicit_call = fake_litellm._calls[-1]
        assert _submitted_tool_names(explicit_call) == {
            "ctx__recommend_bundle",
            "ctx__wiki_get",
        }
        prompt = explicit_call["messages"][0]["content"]
        assert "ctx__recommend_bundle" in prompt
        assert "ctx__wiki_get" in prompt
        assert "ctx__load_entity" not in prompt
        assert "ctx__mark_entity_used" not in prompt
        assert "ctx__unload_entity" not in prompt

        fake_litellm._calls.clear()
        main(
            [
                "resume",
                "surface-resume",
                "--task",
                "inherit minimal",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )
        capsys.readouterr()
        assert _submitted_tool_names(fake_litellm._calls[-1]) == {
            "ctx__recommend_bundle",
            "ctx__wiki_get",
        }
        events = [
            json.loads(line)
            for line in (tmp_path / "surface-resume.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        config_events = [event for event in events if event["type"] == "session_config"]
        assert [event["ctx_tool_surface"] for event in config_events] == ["minimal", "minimal"]

    def test_resume_records_runtime_lifecycle_events(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_events: list[dict[str, Any]] = []

        def capture_record_event(event_name: str, **kwargs: Any) -> None:
            telemetry_events.append({"event_name": event_name, **kwargs})

        monkeypatch.setattr(run_cli, "record_event", capture_record_event)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        sessions_dir = tmp_path / "sessions"
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(sessions_dir),
                "--session-id",
                "lifecycle-resume",
                "--ctx-tool-surface",
                "full",
                "--quiet",
            ]
        )
        capsys.readouterr()

        exit_code = main(
            [
                "resume",
                "lifecycle-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(sessions_dir),
                "--quiet",
            ]
        )
        assert exit_code == 0

        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
            "dev_event",
            "session_end",
        ]
        assert events[2]["event_type"] == "resume_task"
        assert "task" not in events[2]["payload"]
        assert events[2]["payload"]["task_hash"].startswith("sha256:")
        assert "follow-up" not in json.dumps(events[2])
        resume_events = [
            event for event in telemetry_events if event["event_name"] == "ctx.cli.resume"
        ]
        assert [event["payload"]["ctx.run.phase"] for event in resume_events] == [
            "started",
            "finished",
        ]
        assert resume_events[0]["payload"]["ctx.messages.prior_count"] > 0
        assert resume_events[0]["payload"]["ctx.task.length"] == len("follow-up")
        assert resume_events[-1]["payload"]["ctx.stop_reason"] == "completed"
        assert "follow-up" not in json.dumps([event["payload"] for event in resume_events])
        resume_call = fake_litellm._calls[-1]
        tool = next(
            item
            for item in resume_call["tools"]
            if item["function"]["name"] == "ctx__session_state"
        )
        assert "session_id" not in tool["function"]["parameters"]["properties"]
        assert "session_id" not in tool["function"]["parameters"]["required"]

    def test_resume_telemetry_correlates_cli_and_runtime_lifecycle(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        sessions_dir = tmp_path / "sessions"
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(sessions_dir),
                "--session-id",
                "trace-resume",
                "--quiet",
            ]
        )
        capsys.readouterr()

        exit_code = main(
            [
                "resume",
                "trace-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(sessions_dir),
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        events = list(read_events(telemetry_path, trusted_root=tmp_path))
        run_cli_events = [event for event in events if event.event_name == "ctx.cli.run"]
        resume_cli_events = [event for event in events if event.event_name == "ctx.cli.resume"]
        lifecycle_events = [
            event for event in events if event.event_name == "ctx.runtime_lifecycle.record"
        ]
        resume_trace_id = resume_cli_events[0].trace_id
        resume_span_id = resume_cli_events[0].span_id
        resume_lifecycle_events = [
            event for event in lifecycle_events if event.trace_id == resume_trace_id
        ]

        assert [event.payload["ctx.run.phase"] for event in resume_cli_events] == [
            "started",
            "finished",
        ]
        assert [event.payload["ctx.lifecycle.action"] for event in resume_lifecycle_events] == [
            "dev_event",
            "session_end",
        ]
        assert resume_trace_id is not None
        assert resume_span_id is not None
        assert run_cli_events[0].trace_id is not None
        assert resume_cli_events[1].trace_id == resume_trace_id
        assert resume_cli_events[1].span_id == resume_span_id
        assert run_cli_events[0].trace_id != resume_trace_id
        assert all(
            event.payload["ctx.session.previous_trace_id"] == run_cli_events[0].trace_id
            for event in resume_cli_events
        )
        assert all(event.parent_span_id == resume_span_id for event in resume_lifecycle_events)
        session_events = [
            json.loads(line)
            for line in (sessions_dir / "trace-resume.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        session_start = next(event for event in session_events if event["type"] == "session_start")
        assert session_start["initial_trace_id"] == run_cli_events[0].trace_id
        assert "follow-up" not in telemetry_path.read_text(encoding="utf-8")

    def test_resume_exception_telemetry_hashes_provider_error(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        sessions_dir = tmp_path / "sessions"
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(sessions_dir),
                "--session-id",
                "trace-resume-error",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()

        def fail_run_loop(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("private resume provider failure for /Users/example/private-repo")

        monkeypatch.setattr(run_cli, "run_loop", fail_run_loop)

        with pytest.raises(RuntimeError):
            main(
                [
                    "resume",
                    "trace-resume-error",
                    "--task",
                    "private resume task",
                    "--sessions-dir",
                    str(sessions_dir),
                    "--quiet",
                ]
            )

        capsys.readouterr()
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.resume"
        ]
        assert [event.payload["ctx.run.phase"] for event in events] == [
            "started",
            "failed",
        ]
        failed = events[-1]
        assert failed.outcome == "error"
        assert failed.error_kind == "RuntimeError"
        assert failed.payload["ctx.exception.message_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.stack_hash"].startswith("sha256:")
        assert failed.payload["ctx.exception.escaped"] is True
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "private resume provider failure" not in raw
        assert "/Users/example/private-repo" not in raw
        assert "private resume task" not in raw

    def test_runtime_lifecycle_respects_telemetry_disabled(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import ctx.adapters.generic.runtime_lifecycle as runtime_lifecycle

        lifecycle_dir = tmp_path / "runtime"
        monkeypatch.setenv("CTX_RUNTIME_LIFECYCLE_DIR", str(lifecycle_dir))
        monkeypatch.setattr(runtime_lifecycle, "telemetry_enabled", lambda: False)
        monkeypatch.setattr(
            runtime_lifecycle,
            "record_event",
            lambda *args, **kwargs: pytest.fail("disabled telemetry should not emit"),
        )

        exit_code = main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "private task",
                "--sessions-dir",
                str(tmp_path / "sessions"),
                "--session-id",
                "lifecycle-disabled",
                "--quiet",
            ]
        )

        assert exit_code == 0
        capsys.readouterr()
        events = [
            json.loads(line)
            for line in (lifecycle_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [event["action"] for event in events] == [
            "dev_event",
            "session_end",
        ]
        assert all(event["session_id"] == "lifecycle-disabled" for event in events)

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
                "--model",
                "openai/local-model",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "provider-resume",
                "--api-key-env",
                "LOCAL_KEY",
                "--base-url",
                "http://127.0.0.1:8000/v1",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        fake_litellm._calls.clear()

        exit_code = main(
            [
                "resume",
                "provider-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        resume_call = fake_litellm._calls[-1]
        assert resume_call["api_base"] == "http://127.0.0.1:8000/v1"
        assert resume_call["api_key"] == "secret-value"

    def test_resume_preserves_empty_recorded_system_prompt(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--system-prompt",
                "",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "empty-system",
                "--no-ctx-tools",
                "--quiet",
            ]
        )
        capsys.readouterr()
        fake_litellm._calls.clear()

        exit_code = main(
            [
                "resume",
                "empty-system",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--quiet",
            ]
        )

        assert exit_code == 0
        resume_messages = fake_litellm._calls[-1]["messages"]
        assert [message["role"] for message in resume_messages] == [
            "user",
            "assistant",
            "user",
        ]
        message_events = [
            json.loads(line)
            for line in (tmp_path / "empty-system.jsonl").read_text(encoding="utf-8").splitlines()
            if json.loads(line)["type"] == "message"
        ]
        assert [event["content"] for event in message_events].count("final answer") == 2
        assert [event["content"] for event in message_events].count("follow-up") == 1

    def test_resume_inherits_recorded_tool_policy(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(
            [
                "run",
                "--model",
                "ollama/x",
                "--task",
                "first",
                "--sessions-dir",
                str(tmp_path),
                "--session-id",
                "policy-resume",
                "--deny-tool",
                "ctx__wiki_get",
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
                "resume",
                "policy-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--json",
                "--quiet",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 2
        assert payload["stop_reason"] == "tool_denied"

    def test_resume_counts_prior_usage_against_budget(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "budget-resume.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "budget-resume",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": False,
                    "budget_tokens": 10,
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "stop",
                    "ts": "t",
                    "session_id": "budget-resume",
                    "stop_reason": "completed",
                    "usage": {
                        "input_tokens": 6,
                        "output_tokens": 3,
                        "cost_usd": None,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        exit_code = main(
            [
                "resume",
                "budget-resume",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--json",
                "--quiet",
            ]
        )

        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["stop_reason"] == "token_budget"
        assert payload["usage"]["input_tokens"] == 11
        assert payload["usage"]["output_tokens"] == 6

    def test_resume_skips_recorded_mcp_by_default(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._write_session_with_mcp(tmp_path, "tampered")
        exit_code = main(
            [
                "resume",
                "tampered",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert "recorded MCP server(s) skipped" in captured.err

    def test_resume_restores_recorded_mcp_only_with_flag(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        restored: list[Any] = []
        calls: list[str] = []
        router_session_ids: list[str | None] = []

        class FakeRouter:
            started = False

            def __init__(self, configs: list[Any], *, session_id: str | None = None) -> None:
                restored.extend(configs)
                router_session_ids.append(session_id)

            def start(self) -> None:
                calls.append("start")
                self.started = True

            def stop(self) -> None:
                calls.append("stop")
                self.started = False

            def list_tools(self) -> list[Any]:
                assert self.started
                calls.append("list_tools")
                return []

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                raise AssertionError(f"unexpected tool call: {name} {arguments}")

        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        self._write_session_with_mcp(tmp_path, "restore-mcp")
        exit_code = main(
            [
                "resume",
                "restore-mcp",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--restore-session-mcp",
            ]
        )
        captured = capsys.readouterr()
        assert exit_code == 0
        assert len(restored) == 1
        assert restored[0].command == "definitely-not-a-real-mcp-command"
        assert restored[0].credential_env == ("DANGER_TOKEN",)
        assert router_session_ids == ["restore-mcp"]
        assert "restoring MCP server danger" in captured.err
        assert calls == ["start", "list_tools", "stop"]

    def test_adaptive_resume_activates_only_selected_recorded_mcp(
        self,
        fake_litellm: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []

        class FakeRouter:
            def __init__(
                self,
                configs: list[Any],
                *,
                session_id: str | None = None,
                lazy: bool = False,
            ) -> None:
                assert len(configs) == 2
                assert session_id == "adaptive-resume-mcp"
                assert lazy is True
                self.lazy = lazy
                self.active: tuple[str, ...] = ()

            @property
            def server_names(self) -> list[str]:
                return sorted(self.active)

            def start(self) -> None:
                calls.append("start")

            def stop(self) -> None:
                calls.append("stop")

            def list_tools(self) -> list[Any]:
                assert self.active == ()
                calls.append("list_tools")
                return []

            def activate(
                self,
                server_names: tuple[str, ...],
                *,
                capability_epoch: int | None = None,
            ) -> list[ToolDefinition]:
                assert server_names == ("danger",)
                assert capability_epoch == 1
                self.active = server_names
                calls.append("activate:danger")
                return [
                    ToolDefinition(name="danger__read", description="read", parameters={}),
                    ToolDefinition(name="danger__write", description="write", parameters={}),
                ]

            def deactivate(self, server_names: tuple[str, ...]) -> None:
                assert server_names == self.active
                calls.append("deactivate:danger")
                self.active = ()

            def call(self, name: str, arguments: dict[str, Any]) -> str:
                assert self.active == ("danger",)
                assert name == "danger__read"
                calls.append("call:danger__read")
                return "restored result"

        (tmp_path / "adaptive-resume-mcp.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "adaptive-resume-mcp",
                    "task": "old",
                    "model": "ollama/x",
                    "ctx_tools_enabled": True,
                    "ctx_tool_surface": "adaptive",
                    "mcp": [
                        {
                            "name": "danger",
                            "command": "definitely-not-a-real-mcp-command",
                            "args": [],
                            "credential_env": [],
                        },
                        {
                            "name": "other",
                            "command": "also-not-a-real-mcp-command",
                            "args": [],
                            "credential_env": [],
                        },
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(run_cli, "McpRouter", FakeRouter)
        monkeypatch.setattr(
            run_cli.AdaptiveRuntimeController,
            "from_task",
            classmethod(lambda cls, *_args, **_kwargs: cls(None)),
        )

        def completion(**kwargs: Any) -> dict[str, Any]:
            fake_litellm._calls.append(kwargs)
            if len(fake_litellm._calls) == 1:
                return _tool_call_completion("danger__read")
            return {
                "choices": [
                    {
                        "message": {"content": "done", "tool_calls": None},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }

        fake_litellm.completion = completion

        exit_code = main(
            [
                "resume",
                "adaptive-resume-mcp",
                "--task",
                "follow-up",
                "--sessions-dir",
                str(tmp_path),
                "--restore-session-mcp",
                "--allow-tool",
                "ctx__recommend_bundle",
                "--allow-tool",
                "danger__read",
                "--quiet",
            ]
        )

        assert exit_code == 0
        assert [_submitted_tool_names(call) for call in fake_litellm._calls] == [
            {"ctx__recommend_bundle", "danger__read"},
            set(),
        ]
        assert calls == [
            "start",
            "list_tools",
            "activate:danger",
            "call:danger__read",
            "deactivate:danger",
            "stop",
        ]
        metadata = run_cli.load_session("adaptive-resume-mcp", sessions_dir=tmp_path).metadata
        assert metadata["ctx_adaptive"]["mcp_configured_count"] == 2
        assert metadata["ctx_adaptive"]["mcp_activated_count"] == 1

    def test_resume_without_model_in_session_requires_flag(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        # Hand-write a session log with no 'model' in metadata.
        path = tmp_path / "no-model.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "session_start",
                    "ts": "t",
                    "session_id": "no-model",
                    "task": "old",
                    "initial_trace_id": "original-trace-private",
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "message",
                    "ts": "t",
                    "session_id": "no-model",
                    "role": "user",
                    "content": "hi",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        exit_code = main(["resume", "no-model", "--task", "go", "--sessions-dir", str(tmp_path)])
        assert exit_code == 1
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.resume"
        ]
        assert len(events) == 1
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "ValueError"
        assert failed.payload["ctx.run.phase"] == "failed"
        assert failed.payload["ctx.run.failure_stage"] == "validation"
        assert "ctx.session.previous_trace_id" not in failed.payload
        raw = telemetry_path.read_text(encoding="utf-8")
        assert "original-trace-private" not in raw
        assert "go" not in raw

    def test_resume_missing_session(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        telemetry_path = _enable_real_telemetry(monkeypatch, tmp_path)
        exit_code = main(["resume", "not-there", "--task", "go", "--sessions-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "session log not found" in captured.err
        events = [
            event
            for event in read_events(telemetry_path, trusted_root=tmp_path)
            if event.event_name == "ctx.cli.resume"
        ]
        assert len(events) == 1
        failed = events[0]
        assert failed.trace_id is not None
        assert failed.span_id is not None
        assert failed.outcome == "error"
        assert failed.error_kind == "SessionLoadError"
        assert failed.payload["ctx.run.phase"] == "failed"
        assert failed.payload["ctx.run.failure_stage"] == "session_load"
        assert "go" not in telemetry_path.read_text(encoding="utf-8")
