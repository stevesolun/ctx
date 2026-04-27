"""Tests for the clean-host contract runner."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scripts.clean_host_contract import (
    CommandRunner,
    CompletedCommand,
    assert_inside,
    isolated_env,
    make_paths,
    run_contract,
    venv_script,
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
    ) -> CompletedCommand:
        del cwd, env, check
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
    assert any("ctx-scan-repo" in call and "--recommend" in call for call in joined)
    assert any("ctx run" in call or "ctx.exe run" in call for call in joined)
    assert any("--deny-tool ctx__wiki_get" in call for call in joined)
