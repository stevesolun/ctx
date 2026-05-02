"""Tests for the clean-host contract runner."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scripts.clean_host_contract import (
    CommandRunner,
    CompletedCommand,
    LIVE_CLAUDE_ACK_ENV,
    LIVE_CLAUDE_ACK_VALUE,
    _assert_fake_claude_hook_output,
    _assert_live_claude_sentinel,
    _append_live_claude_sentinel_hooks,
    _live_claude_command,
    assert_inside,
    isolated_env,
    make_paths,
    run_contract,
    venv_script,
    write_fake_claude_cli,
    write_fake_litellm,
    write_tiny_repo,
)


class RecordingRunner(CommandRunner):
    def __init__(self, venv: Path) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.venv = venv

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> CompletedCommand:
        del cwd, env, check, timeout_seconds
        call = tuple(args)
        self.calls.append(call)
        if call[:4] == (sys.executable, "-m", "pip", "wheel"):
            outdir = Path(call[call.index("--wheel-dir") + 1])
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "claude_ctx-0.0.0-py3-none-any.whl").write_bytes(b"wheel")
        elif call[:3] == (sys.executable, "-m", "venv"):
            scripts = self.venv / ("Scripts" if os.name == "nt" else "bin")
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("")
            for name in ("ctx-init", "ctx-scan-repo", "ctx"):
                suffix = ".exe" if os.name == "nt" else ""
                (scripts / f"{name}{suffix}").write_text("")
        elif call and Path(call[0]).name.startswith("ctx-init"):
            home = Path(os.environ.get("CTX_TEST_HOME_OVERRIDE", ""))
            if home:
                settings = home / ".claude" / "settings.json"
                settings.parent.mkdir(parents=True, exist_ok=True)
                settings.write_text("{}", encoding="utf-8")
        elif call and Path(call[0]).name.startswith("ctx-scan-repo"):
            output = Path(call[call.index("--output") + 1])
            output.write_text("{}", encoding="utf-8")
        elif any(Path(part).name == "fake_claude.py" for part in call):
            stdout = json.dumps({
                "hook_commands": 5,
                "failed": 0,
                "commands": [
                    {"command": "ctx.adapters.claude_code.hooks.context_monitor"},
                    {"command": "skill_add_detector"},
                    {"command": "ctx.adapters.claude_code.hooks.bundle_orchestrator"},
                    {"command": "usage_tracker"},
                    {"command": "ctx.adapters.claude_code.hooks.lifecycle_hooks"},
                ],
            })
            return CompletedCommand(call, Path.cwd(), 0, stdout, "")
        elif call and Path(call[0]).name.startswith("claude"):
            if "-p" in call:
                sentinel = self.venv.parent / "live-claude-hooks.jsonl"
                sentinel.write_text(
                    "\n".join([
                        json.dumps({
                            "event": "PostToolUse",
                            "hook_event_name": "PostToolUse",
                            "cwd": str(self.venv.parent / "tiny-fastapi-repo"),
                        }),
                        json.dumps({
                            "event": "Stop",
                            "hook_event_name": "Stop",
                            "cwd": str(self.venv.parent / "tiny-fastapi-repo"),
                        }),
                    ]),
                    encoding="utf-8",
                )
            return CompletedCommand(call, Path.cwd(), 0, "claude preflight\n", "")
        stdout = '{"stop_reason": "tool_denied"}' if "--deny-tool" in call else ""
        rc = 2 if "--deny-tool" in call else 0
        return CompletedCommand(call, Path.cwd(), rc, stdout, "")


def test_isolated_env_redirects_user_state(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    env = isolated_env(paths, extra_pythonpath=paths.fake_modules)

    assert env["HOME"] == str(paths.home)
    assert env["USERPROFILE"] == str(paths.home)
    assert env["APPDATA"] == str(paths.appdata)
    assert env["LOCALAPPDATA"] == str(paths.localappdata)
    assert env["XDG_CONFIG_HOME"] == str(paths.xdg_config)
    assert env["XDG_CACHE_HOME"] == str(paths.xdg_cache)
    assert env["PIP_CACHE_DIR"] == str(paths.pip_cache)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(paths.fake_modules)


def test_isolated_env_does_not_inherit_caller_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_paths(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "src"))

    no_extra = isolated_env(paths)
    with_extra = isolated_env(paths, extra_pythonpath=paths.fake_modules)

    assert "PYTHONPATH" not in no_extra
    assert with_extra["PYTHONPATH"] == str(paths.fake_modules)


def test_isolated_env_does_not_inherit_caller_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_paths(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")
    monkeypatch.setenv("CTX_WIKI_DIR", str(tmp_path / "real-wiki"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "real-claude"))

    env = isolated_env(paths)

    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "CTX_WIKI_DIR" not in env
    assert "CLAUDE_HOME" not in env


def test_assert_inside_rejects_escape(tmp_path: Path) -> None:
    assert_inside(tmp_path / "child", tmp_path)
    with pytest.raises(AssertionError):
        assert_inside(tmp_path.parent, tmp_path)


def test_venv_script_prefers_platform_entrypoint(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    if os.name == "nt":
        scripts = venv / "Scripts"
        expected = scripts / "ctx-init.exe"
    else:
        scripts = venv / "bin"
        expected = scripts / "ctx-init"
    scripts.mkdir(parents=True)
    expected.write_text("", encoding="utf-8")

    assert venv_script(venv, "ctx-init") == expected


def test_fake_litellm_can_emit_stop_or_tool_call(tmp_path: Path) -> None:
    fake = write_fake_litellm(tmp_path)
    body = fake.read_text(encoding="utf-8")

    assert "clean host contract response" in body
    assert "CTX_FAKE_LITELLM_TOOL_CALL" in body
    assert "tool_calls" in body


def test_fake_claude_cli_executes_post_tool_and_stop_hooks(tmp_path: Path) -> None:
    fake = write_fake_claude_cli(tmp_path)
    body = fake.read_text(encoding="utf-8")

    assert "PostToolUse" in body
    assert "Stop" in body
    assert "subprocess.run" in body
    assert "shell=True" in body
    assert "shlex.split(command)" not in body


def test_fake_claude_hook_output_requires_all_generated_hooks() -> None:
    good = json.dumps({
        "hook_commands": 5,
        "failed": 0,
        "commands": [
            {"command": "ctx.adapters.claude_code.hooks.context_monitor"},
            {"command": "skill_add_detector"},
            {"command": "ctx.adapters.claude_code.hooks.bundle_orchestrator"},
            {"command": "usage_tracker"},
            {"command": "ctx.adapters.claude_code.hooks.lifecycle_hooks"},
        ],
    })
    _assert_fake_claude_hook_output(good)

    missing = json.dumps({
        "hook_commands": 4,
        "failed": 0,
        "commands": [{"command": "usage_tracker"}],
    })
    with pytest.raises(AssertionError):
        _assert_fake_claude_hook_output(missing)


def test_tiny_repo_contains_fastapi_signals(tmp_path: Path) -> None:
    write_tiny_repo(tmp_path)

    assert "fastapi" in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "FastAPI" in (tmp_path / "app" / "main.py").read_text(encoding="utf-8")


def test_contract_command_sequence_without_real_build(tmp_path: Path, monkeypatch) -> None:
    paths = make_paths(tmp_path)
    runner = RecordingRunner(paths.venv)
    monkeypatch.setenv("CTX_TEST_HOME_OVERRIDE", str(paths.home))

    run_contract(
        project_root=tmp_path,
        temp_root=tmp_path,
        fast=True,
        runner=runner,
    )

    joined = [" ".join(call) for call in runner.calls]
    assert any("-m pip wheel" in call for call in joined)
    assert any("-m venv" in call for call in joined)
    assert any("-m pip install" in call for call in joined)
    assert any("ctx-init" in call and "--hooks" in call for call in joined)
    assert any("fake_claude.py" in call and "--settings" in call for call in joined)
    assert any("ctx-scan-repo" in call and "--recommend" in call for call in joined)
    assert any("ctx run" in call or "ctx.exe run" in call for call in joined)
    assert any("--deny-tool ctx__wiki_get" in call for call in joined)


def test_live_claude_command_is_bounded_and_streamed(tmp_path: Path) -> None:
    command = _live_claude_command(
        claude_bin=tmp_path / "claude",
        settings_json=tmp_path / "settings.json",
        max_budget_usd=0.02,
    )

    assert command[:2] == [str(tmp_path / "claude"), "--settings"]
    assert "--output-format" in command
    assert "stream-json" in command
    assert "--include-hook-events" in command
    assert "--max-budget-usd" in command
    assert "0.02" in command
    assert "--allowedTools" in command
    assert "Bash(python --version)" in command
    assert "-p" in command


def test_live_claude_sentinel_hooks_are_appended_to_settings(tmp_path: Path) -> None:
    settings_json = tmp_path / "settings.json"
    sentinel_script = tmp_path / "live_sentinel.py"
    sentinel_jsonl = tmp_path / "live_sentinel.jsonl"
    settings_json.write_text(
        json.dumps({
            "hooks": {
                "PostToolUse": [{"matcher": ".*", "hooks": []}],
                "Stop": [{"hooks": []}],
            }
        }),
        encoding="utf-8",
    )

    _append_live_claude_sentinel_hooks(
        settings_json=settings_json,
        python_bin=tmp_path / "python",
        sentinel_script=sentinel_script,
        sentinel_jsonl=sentinel_jsonl,
    )

    settings = json.loads(settings_json.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entries in settings["hooks"].values()
        for entry in entries
        for hook in entry["hooks"]
        if hook.get("command")
    ]
    joined = "\n".join(commands)
    assert str(sentinel_script) in joined
    assert str(sentinel_jsonl) in joined
    assert "PostToolUse" in settings["hooks"]
    assert "Stop" in settings["hooks"]


def test_live_claude_sentinel_requires_post_tool_and_stop(tmp_path: Path) -> None:
    sentinel_jsonl = tmp_path / "sentinel.jsonl"
    sentinel_jsonl.write_text(
        "\n".join([
            json.dumps({"event": "PostToolUse", "cwd": str(tmp_path)}),
            json.dumps({"event": "Stop", "cwd": str(tmp_path)}),
        ]),
        encoding="utf-8",
    )
    _assert_live_claude_sentinel(sentinel_jsonl, expected_cwd=tmp_path)

    sentinel_jsonl.write_text(
        json.dumps({"event": "PostToolUse", "cwd": str(tmp_path)}),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _assert_live_claude_sentinel(sentinel_jsonl, expected_cwd=tmp_path)


def test_live_claude_ack_required_before_running_live_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_paths(tmp_path)
    runner = RecordingRunner(paths.venv)
    monkeypatch.setenv("CTX_TEST_HOME_OVERRIDE", str(paths.home))
    monkeypatch.delenv(LIVE_CLAUDE_ACK_ENV, raising=False)

    with pytest.raises(AssertionError, match=LIVE_CLAUDE_ACK_ENV):
        run_contract(
            project_root=tmp_path,
            temp_root=tmp_path,
            fast=True,
            runner=runner,
            run_live_claude=True,
        )

    assert not any(
        call and Path(call[0]).name.startswith("claude")
        for call in runner.calls
    )


def test_live_claude_gate_runs_only_when_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_paths(tmp_path)
    runner = RecordingRunner(paths.venv)
    claude_bin = tmp_path / ("claude.exe" if os.name == "nt" else "claude")
    monkeypatch.setenv("CTX_TEST_HOME_OVERRIDE", str(paths.home))
    monkeypatch.setenv(LIVE_CLAUDE_ACK_ENV, LIVE_CLAUDE_ACK_VALUE)

    run_contract(
        project_root=tmp_path,
        temp_root=tmp_path,
        fast=True,
        runner=runner,
        run_live_claude=True,
        live_claude_max_budget_usd=0.02,
        live_claude_bin=claude_bin,
    )

    live_calls = [
        call
        for call in runner.calls
        if call and Path(call[0]).name.startswith("claude")
    ]
    assert [call[1:] for call in live_calls[:2]] == [("--version",), ("auth", "status")]
    prompt_calls = [call for call in live_calls if "-p" in call]
    assert len(prompt_calls) == 1
    live_call = prompt_calls[0]
    assert "--settings" in live_call
    assert str(paths.home / ".claude" / "settings.json") in live_call
    assert "--include-hook-events" in live_call
    assert "--max-budget-usd" in live_call
    assert "0.02" in live_call
    assert (paths.root / "live-claude-hooks.jsonl").exists()
