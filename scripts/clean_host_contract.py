"""Clean-host contract runner for ctx release hardening.

The contract builds the current tree into a wheel, installs it into a
fresh virtualenv, points all user-state environment variables at a temp
root, then drives real console scripts. It is intentionally not a public
entrypoint yet; this is release infrastructure.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


LIVE_CLAUDE_ACK_ENV = "CTX_LIVE_CLAUDE_ACK"
LIVE_CLAUDE_ACK_VALUE = "uses_quota"
LIVE_CLAUDE_DEFAULT_BUDGET_USD = 0.05
LIVE_CLAUDE_MAX_BUDGET_USD = 1.0
LIVE_CLAUDE_PROMPT_TIMEOUT_SECONDS = 180.0
LIVE_CLAUDE_PREFLIGHT_TIMEOUT_SECONDS = 30.0

_LIVE_CLAUDE_AUTH_EXACT_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BETA_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
}
_LIVE_CLAUDE_AUTH_PREFIX_ENV = (
    "AWS_",
    "GOOGLE_",
    "GCLOUD_",
    "VERTEXAI_",
    "ANTHROPIC_",
)
_LIVE_CLAUDE_PLATFORM_ENV = {
    "PATH",
    "Path",
    "PATHEXT",
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
}
_CLEAN_HOST_PLATFORM_ENV = _LIVE_CLAUDE_PLATFORM_ENV | {
    "LANG",
    "LC_ALL",
    "TZ",
}
_EXPECTED_LIVE_CLAUDE_EVENTS = ("PostToolUse", "Stop")


@dataclass(frozen=True)
class ContractPaths:
    root: Path
    home: Path
    appdata: Path
    localappdata: Path
    xdg_config: Path
    xdg_cache: Path
    pip_cache: Path
    dist: Path
    venv: Path
    fake_modules: Path
    tiny_repo: Path
    sessions: Path


@dataclass(frozen=True)
class CompletedCommand:
    args: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        check: bool = True,
        timeout_seconds: float | None = None,
    ) -> CompletedCommand:
        print("+ " + " ".join(args), flush=True)
        try:
            result = subprocess.run(
                list(args),
                cwd=cwd,
                env=dict(env),
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SystemExit(
                f"command timed out after {timeout_seconds}s: {' '.join(args)}"
            ) from exc
        if result.stdout.strip():
            print(result.stdout.rstrip())
        if result.stderr.strip():
            print(result.stderr.rstrip(), file=sys.stderr)
        completed = CompletedCommand(
            args=tuple(args),
            cwd=cwd,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if check and result.returncode != 0:
            raise SystemExit(
                f"command failed with exit {result.returncode}: {' '.join(args)}"
            )
        return completed


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def make_paths(root: Path) -> ContractPaths:
    return ContractPaths(
        root=root,
        home=root / "home",
        appdata=root / "appdata",
        localappdata=root / "localappdata",
        xdg_config=root / "xdg-config",
        xdg_cache=root / "xdg-cache",
        pip_cache=root / "pip-cache",
        dist=root / "dist",
        venv=root / "venv",
        fake_modules=root / "fake-modules",
        tiny_repo=root / "tiny-fastapi-repo",
        sessions=root / "sessions",
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def assert_inside(path: Path, root: Path) -> None:
    if not _is_relative_to(path, root):
        raise AssertionError(f"path escaped temp root: {path} not under {root}")


def isolated_env(paths: ContractPaths, *, extra_pythonpath: Path | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _CLEAN_HOST_PLATFORM_ENV
    }
    env.update({
        "HOME": str(paths.home),
        "USERPROFILE": str(paths.home),
        "APPDATA": str(paths.appdata),
        "LOCALAPPDATA": str(paths.localappdata),
        "XDG_CONFIG_HOME": str(paths.xdg_config),
        "XDG_CACHE_HOME": str(paths.xdg_cache),
        "PIP_CACHE_DIR": str(paths.pip_cache),
        "PYTHONUTF8": "1",
    })
    if extra_pythonpath is not None:
        env["PYTHONPATH"] = str(extra_pythonpath)
    return env


def live_claude_env(paths: ContractPaths) -> dict[str, str]:
    """Build a narrow env for opt-in live Claude checks.

    The fake contract can inherit the parent env because it never calls a
    hosted model. The live gate is different: it should keep auth and platform
    plumbing, but not point Claude back at the user's real home/config tree.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if (
            key in _LIVE_CLAUDE_PLATFORM_ENV
            or key in _LIVE_CLAUDE_AUTH_EXACT_ENV
            or key.startswith(_LIVE_CLAUDE_AUTH_PREFIX_ENV)
        ):
            env[key] = value
    env.update({
        "HOME": str(paths.home),
        "USERPROFILE": str(paths.home),
        "APPDATA": str(paths.appdata),
        "LOCALAPPDATA": str(paths.localappdata),
        "XDG_CONFIG_HOME": str(paths.xdg_config),
        "XDG_CACHE_HOME": str(paths.xdg_cache),
        "PYTHONUTF8": "1",
    })
    env.pop("CLAUDE_HOME", None)
    return env


def venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def venv_script(venv: Path, name: str) -> Path:
    candidates = (
        [venv / "Scripts" / f"{name}.exe", venv / "Scripts" / name]
        if os.name == "nt"
        else [venv / "bin" / name]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def write_fake_litellm(fake_modules: Path) -> Path:
    fake_modules.mkdir(parents=True, exist_ok=True)
    path = fake_modules / "litellm.py"
    path.write_text(
        '''"""Process-local fake LiteLLM for clean-host contract runs."""\n'''
        "from __future__ import annotations\n\n"
        "import json\n"
        "import os\n\n"
        "def completion(**kwargs):\n"
        "    tool_name = os.environ.get('CTX_FAKE_LITELLM_TOOL_CALL')\n"
        "    if tool_name:\n"
        "        return {\n"
        "            'choices': [{\n"
        "                'message': {\n"
        "                    'content': '',\n"
        "                    'tool_calls': [{\n"
        "                        'id': 'clean-host-call-1',\n"
        "                        'type': 'function',\n"
        "                        'function': {\n"
        "                            'name': tool_name,\n"
        "                            'arguments': json.dumps({'slug': 'python-patterns'}),\n"
        "                        },\n"
        "                    }],\n"
        "                },\n"
        "                'finish_reason': 'tool_calls',\n"
        "            }],\n"
        "            'usage': {'prompt_tokens': 5, 'completion_tokens': 1},\n"
        "        }\n"
        "    return {\n"
        "        'choices': [{\n"
        "            'message': {'content': 'clean host contract response', 'tool_calls': None},\n"
        "            'finish_reason': 'stop',\n"
        "        }],\n"
        "        'usage': {'prompt_tokens': 5, 'completion_tokens': 3},\n"
        "    }\n",
        encoding="utf-8",
    )
    return path


def write_fake_claude_cli(fake_modules: Path) -> Path:
    """Write a tiny Claude-Code-like host that executes generated hooks.

    This does not call Anthropic APIs. It reads the isolated settings.json
    produced by ctx-init and invokes the configured hook command strings with
    representative stdin payloads, which catches broken module paths and hook
    schema drift from the installed wheel.
    """
    fake_modules.mkdir(parents=True, exist_ok=True)
    path = fake_modules / "fake_claude.py"
    path.write_text(
        '''"""Deterministic Claude Code hook smoke host for clean-host tests."""\n'''
        "from __future__ import annotations\n\n"
        "import argparse\n"
        "import json\n"
        "import os\n"
        "import re\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "PAYLOADS = {\n"
        "    'PostToolUse': {\n"
        "        'hook_event_name': 'PostToolUse',\n"
        "        'tool_name': 'Bash',\n"
        "        'tool_input': {'command': 'pip install fastapi pytest'},\n"
        "    },\n"
        "    'Stop': {\n"
        "        'hook_event_name': 'Stop',\n"
        "        'session_id': 'clean-host-fake-claude',\n"
        "    },\n"
        "}\n\n"
        "def _settings_path(raw: str) -> Path:\n"
        "    if raw:\n"
        "        return Path(raw)\n"
        "    return Path(os.path.expanduser('~/.claude/settings.json'))\n\n"
        "def _commands(settings: dict, event: str) -> list[str]:\n"
        "    payload = PAYLOADS[event]\n"
        "    tool_name = str(payload.get('tool_name') or '')\n"
        "    out: list[str] = []\n"
        "    for entry in settings.get('hooks', {}).get(event, []):\n"
        "        if not isinstance(entry, dict):\n"
        "            continue\n"
        "        matcher = str(entry.get('matcher') or '.*')\n"
        "        if event == 'PostToolUse' and not re.search(matcher, tool_name):\n"
        "            continue\n"
        "        for hook in entry.get('hooks', []):\n"
        "            if isinstance(hook, dict) and hook.get('type') == 'command':\n"
        "                command = hook.get('command')\n"
        "                if isinstance(command, str) and command.strip():\n"
        "                    out.append(command)\n"
        "    return out\n\n"
        "def main() -> int:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--settings', default='')\n"
        "    parser.add_argument('--cwd', default='')\n"
        "    parser.add_argument('-p', '--print', action='store_true')\n"
        "    parser.add_argument('prompt', nargs='*')\n"
        "    args = parser.parse_args()\n"
        "    settings = json.loads(_settings_path(args.settings).read_text(encoding='utf-8'))\n"
        "    cwd = args.cwd or os.getcwd()\n"
        "    records = []\n"
        "    for event in ('PostToolUse', 'Stop'):\n"
        "        payload = json.dumps(PAYLOADS[event])\n"
        "        for command in _commands(settings, event):\n"
        "            result = subprocess.run(\n"
        "                command,\n"
        "                cwd=cwd,\n"
        "                env=os.environ.copy(),\n"
        "                input=payload,\n"
        "                text=True,\n"
        "                capture_output=True,\n"
        "                check=False,\n"
        "                shell=True,\n"
        "            )\n"
        "            records.append({\n"
        "                'event': event,\n"
        "                'command': command,\n"
        "                'returncode': result.returncode,\n"
        "                'stdout': result.stdout[-500:],\n"
        "                'stderr': result.stderr[-500:],\n"
        "            })\n"
        "    failed = [r for r in records if r['returncode'] != 0]\n"
        "    print(json.dumps({'hook_commands': len(records), 'failed': len(failed), 'commands': records}))\n"
        "    return 1 if failed else 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    return path


def write_tiny_repo(path: Path) -> None:
    (path / "app").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        "[project]\n"
        "name = \"tiny-fastapi-contract\"\n"
        "version = \"0.1.0\"\n"
        "dependencies = [\"fastapi\", \"pytest\"]\n",
        encoding="utf-8",
    )
    (path / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (path / "tests" / "test_health.py").write_text(
        "def test_contract_fixture():\n"
        "    assert True\n",
        encoding="utf-8",
    )


def _single_wheel(dist: Path) -> Path:
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise AssertionError(f"expected exactly one wheel in {dist}, found {len(wheels)}")
    return wheels[0]


def _prepare_dirs(paths: ContractPaths) -> None:
    for path in (
        paths.home,
        paths.appdata,
        paths.localappdata,
        paths.xdg_config,
        paths.xdg_cache,
        paths.pip_cache,
        paths.dist,
        paths.fake_modules,
        paths.sessions,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _assert_fake_claude_hook_output(stdout: str) -> None:
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"fake Claude hook smoke returned invalid JSON: {stdout!r}") from exc
    if result.get("failed") != 0:
        raise AssertionError(f"fake Claude hook smoke had failures: {stdout}")
    hook_commands = int(result.get("hook_commands") or 0)
    if hook_commands < 5:
        raise AssertionError(f"expected at least 5 generated hook commands, got {hook_commands}")
    rendered = "\n".join(
        str(row.get("command", ""))
        for row in result.get("commands", [])
        if isinstance(row, dict)
    )
    for expected in (
        "ctx.adapters.claude_code.hooks.context_monitor",
        "skill_add_detector",
        "ctx.adapters.claude_code.hooks.bundle_orchestrator",
        "usage_tracker",
        "ctx.adapters.claude_code.hooks.lifecycle_hooks",
    ):
        if expected not in rendered:
            raise AssertionError(f"fake Claude hook smoke did not run {expected}")


def _quote_command(parts: Sequence[str | Path]) -> str:
    values = [str(part) for part in parts]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    return " ".join(shlex.quote(part) for part in values)


def write_live_claude_sentinel_script(path: Path) -> None:
    path.write_text(
        '''"""Append Claude Code hook events to a clean-host sentinel file."""\n'''
        "from __future__ import annotations\n\n"
        "import argparse\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n\n"
        "def main() -> int:\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--event', required=True)\n"
        "    parser.add_argument('--out', required=True)\n"
        "    args = parser.parse_args()\n"
        "    raw = sys.stdin.read()\n"
        "    try:\n"
        "        payload = json.loads(raw) if raw.strip() else {}\n"
        "    except json.JSONDecodeError:\n"
        "        payload = {'invalid_json_prefix': raw[:200]}\n"
        "    out = Path(args.out)\n"
        "    out.parent.mkdir(parents=True, exist_ok=True)\n"
        "    record = {\n"
        "        'event': args.event,\n"
        "        'hook_event_name': payload.get('hook_event_name'),\n"
        "        'tool_name': payload.get('tool_name'),\n"
        "        'cwd': os.getcwd(),\n"
        "        'argv': sys.argv[1:],\n"
        "    }\n"
        "    with out.open('a', encoding='utf-8') as handle:\n"
        "        handle.write(json.dumps(record, sort_keys=True) + '\\n')\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )


def _append_live_claude_sentinel_hooks(
    *,
    settings_json: Path,
    python_bin: Path,
    sentinel_script: Path,
    sentinel_jsonl: Path,
) -> None:
    settings = json.loads(settings_json.read_text(encoding="utf-8"))
    hooks = settings.setdefault("hooks", {})
    post_command = _quote_command((
        python_bin,
        sentinel_script,
        "--event",
        "PostToolUse",
        "--out",
        sentinel_jsonl,
    ))
    stop_command = _quote_command((
        python_bin,
        sentinel_script,
        "--event",
        "Stop",
        "--out",
        sentinel_jsonl,
    ))
    hooks.setdefault("PostToolUse", []).append({
        "matcher": ".*",
        "hooks": [{"type": "command", "command": post_command}],
    })
    hooks.setdefault("Stop", []).append({
        "hooks": [{"type": "command", "command": stop_command}],
    })
    tmp_path = settings_json.with_name(f"{settings_json.name}.live.tmp")
    tmp_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, settings_json)


def _sentinel_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise AssertionError(f"live Claude sentinel was not written: {path}")
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"live Claude sentinel has invalid JSONL: {line}") from exc
        if not isinstance(record, dict):
            raise AssertionError(f"live Claude sentinel record is not an object: {line}")
        records.append(record)
    return records


def _assert_live_claude_sentinel(path: Path, *, expected_cwd: Path) -> None:
    records = _sentinel_records(path)
    events = {str(record.get("event") or "") for record in records}
    for expected in _EXPECTED_LIVE_CLAUDE_EVENTS:
        if expected not in events:
            raise AssertionError(f"live Claude sentinel did not record {expected}")
    expected_resolved = expected_cwd.resolve()
    for record in records:
        cwd = record.get("cwd")
        if cwd is None or Path(str(cwd)).resolve() != expected_resolved:
            raise AssertionError(
                "live Claude sentinel recorded unexpected cwd: "
                f"{cwd!r}, expected {expected_resolved}"
            )
        hook_event = record.get("hook_event_name")
        event = record.get("event")
        if hook_event not in (None, event):
            raise AssertionError(
                f"live Claude sentinel event mismatch: event={event!r}, "
                f"hook_event_name={hook_event!r}"
            )


def _require_live_claude_ack(max_budget_usd: float) -> None:
    if os.environ.get(LIVE_CLAUDE_ACK_ENV) != LIVE_CLAUDE_ACK_VALUE:
        raise AssertionError(
            f"set {LIVE_CLAUDE_ACK_ENV}={LIVE_CLAUDE_ACK_VALUE} to run "
            "the quota-consuming live Claude Code gate"
        )
    if max_budget_usd <= 0 or max_budget_usd > LIVE_CLAUDE_MAX_BUDGET_USD:
        raise AssertionError(
            "live Claude budget must be greater than 0 and no more than "
            f"{LIVE_CLAUDE_MAX_BUDGET_USD} USD"
        )


def _live_claude_command(
    *,
    claude_bin: Path,
    settings_json: Path,
    max_budget_usd: float,
) -> list[str]:
    return [
        str(claude_bin),
        "--settings",
        str(settings_json),
        "--setting-sources",
        "user",
        "--output-format",
        "stream-json",
        "--include-hook-events",
        "--no-session-persistence",
        "--max-budget-usd",
        str(max_budget_usd),
        "--allowedTools",
        "Bash(python --version)",
        "-p",
        "Use Bash to run exactly `python --version`, then stop.",
    ]


def _run_live_claude_gate(
    *,
    runner: CommandRunner,
    paths: ContractPaths,
    python_bin: Path,
    settings_json: Path,
    max_budget_usd: float,
    claude_bin: Path | None,
) -> None:
    _require_live_claude_ack(max_budget_usd)
    resolved_claude = claude_bin if claude_bin is not None else None
    if resolved_claude is None:
        claude = shutil.which("claude")
        resolved_claude = Path(claude) if claude is not None else None
    if resolved_claude is None:
        raise AssertionError("claude executable was not found on PATH")
    live_env = live_claude_env(paths)
    runner.run(
        [str(resolved_claude), "--version"],
        cwd=paths.tiny_repo,
        env=live_env,
        timeout_seconds=LIVE_CLAUDE_PREFLIGHT_TIMEOUT_SECONDS,
    )
    runner.run(
        [str(resolved_claude), "auth", "status"],
        cwd=paths.tiny_repo,
        env=live_env,
        timeout_seconds=LIVE_CLAUDE_PREFLIGHT_TIMEOUT_SECONDS,
    )
    sentinel_script = paths.root / "live-claude-hook-sentinel.py"
    sentinel_jsonl = paths.root / "live-claude-hooks.jsonl"
    write_live_claude_sentinel_script(sentinel_script)
    _append_live_claude_sentinel_hooks(
        settings_json=settings_json,
        python_bin=python_bin,
        sentinel_script=sentinel_script,
        sentinel_jsonl=sentinel_jsonl,
    )
    runner.run(
        _live_claude_command(
            claude_bin=resolved_claude,
            settings_json=settings_json,
            max_budget_usd=max_budget_usd,
        ),
        cwd=paths.tiny_repo,
        env=live_env,
        timeout_seconds=LIVE_CLAUDE_PROMPT_TIMEOUT_SECONDS,
    )
    _assert_live_claude_sentinel(sentinel_jsonl, expected_cwd=paths.tiny_repo)


def run_contract(
    *,
    project_root: Path,
    temp_root: Path,
    fast: bool,
    runner: CommandRunner | None = None,
    run_live_claude: bool = False,
    live_claude_max_budget_usd: float = LIVE_CLAUDE_DEFAULT_BUDGET_USD,
    live_claude_bin: Path | None = None,
) -> None:
    runner = runner or CommandRunner()
    paths = make_paths(temp_root)
    _prepare_dirs(paths)
    write_tiny_repo(paths.tiny_repo)
    write_fake_litellm(paths.fake_modules)
    fake_claude = write_fake_claude_cli(paths.fake_modules)

    env = isolated_env(paths)
    runner.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(paths.dist),
            str(project_root),
        ],
        cwd=project_root,
        env=env,
    )
    runner.run([sys.executable, "-m", "venv", str(paths.venv)], cwd=project_root, env=env)
    wheel = _single_wheel(paths.dist)
    py = venv_python(paths.venv)
    runner.run([str(py), "-m", "pip", "install", str(wheel)], cwd=project_root, env=env)

    run_env = isolated_env(paths, extra_pythonpath=paths.fake_modules)
    ctx_init = venv_script(paths.venv, "ctx-init")
    ctx_scan_repo = venv_script(paths.venv, "ctx-scan-repo")
    ctx = venv_script(paths.venv, "ctx")

    runner.run([str(ctx_init), "--hooks"], cwd=paths.tiny_repo, env=run_env)
    claude_dir = paths.home / ".claude"
    assert_inside(claude_dir, paths.root)
    if not (claude_dir / "settings.json").exists():
        raise AssertionError("ctx-init --hooks did not write isolated settings.json")
    fake_claude_result = runner.run(
        [
            str(py),
            str(fake_claude),
            "--settings",
            str(claude_dir / "settings.json"),
            "--cwd",
            str(paths.tiny_repo),
            "-p",
            "trigger clean-host hook smoke",
        ],
        cwd=paths.tiny_repo,
        env=run_env,
    )
    _assert_fake_claude_hook_output(fake_claude_result.stdout)
    if run_live_claude:
        _run_live_claude_gate(
            runner=runner,
            paths=paths,
            python_bin=py,
            settings_json=claude_dir / "settings.json",
            max_budget_usd=live_claude_max_budget_usd,
            claude_bin=live_claude_bin,
        )

    stack_profile = paths.root / "stack-profile.json"
    runner.run(
        [
            str(ctx_scan_repo),
            "--repo",
            str(paths.tiny_repo),
            "--output",
            str(stack_profile),
            "--recommend",
        ],
        cwd=paths.tiny_repo,
        env=run_env,
    )
    if not stack_profile.exists():
        raise AssertionError("ctx-scan-repo did not write stack profile")

    runner.run(
        [
            str(ctx),
            "run",
            "--model",
            "ollama/clean-host-fake",
            "--task",
            "Return a one-sentence clean-host contract response.",
            "--no-ctx-tools",
            "--sessions-dir",
            str(paths.sessions),
            "--session-id",
            "clean-host-contract",
            "--quiet",
        ],
        cwd=paths.tiny_repo,
        env=run_env,
    )
    runner.run(
        [
            str(ctx),
            "resume",
            "clean-host-contract",
            "--task",
            "Confirm resume works.",
            "--sessions-dir",
            str(paths.sessions),
            "--quiet",
        ],
        cwd=paths.tiny_repo,
        env=run_env,
    )

    denied_env = dict(run_env)
    denied_env["CTX_FAKE_LITELLM_TOOL_CALL"] = "ctx__wiki_get"
    denied = runner.run(
        [
            str(ctx),
            "run",
            "--model",
            "ollama/clean-host-fake",
            "--task",
            "Attempt the wiki tool so policy can deny it.",
            "--sessions-dir",
            str(paths.sessions),
            "--session-id",
            "clean-host-denied-tool",
            "--deny-tool",
            "ctx__wiki_get",
            "--quiet",
            "--json",
        ],
        cwd=paths.tiny_repo,
        env=denied_env,
        check=False,
    )
    if denied.returncode != 2 or '"tool_denied"' not in denied.stdout:
        raise AssertionError(
            "expected denied tool run to exit 2 with tool_denied JSON; "
            f"got rc={denied.returncode}"
        )

    if not fast:
        runner.run([str(ctx_init), "--help"], cwd=paths.tiny_repo, env=run_env)
        runner.run([str(ctx_scan_repo), "--help"], cwd=paths.tiny_repo, env=run_env)
        runner.run([str(ctx), "--help"], cwd=paths.tiny_repo, env=run_env)

    for expected in (paths.home, paths.appdata, paths.localappdata, paths.sessions):
        assert_inside(expected, paths.root)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build/install ctx into a temp clean host and exercise core flows.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Run the core contract only; skip extra help probes.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temp root after the run for debugging.",
    )
    parser.add_argument(
        "--temp-root",
        type=Path,
        help="Use an explicit temp root. Must not already contain important data.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_root(),
        help="Repository root to build. Default: inferred from this script.",
    )
    parser.add_argument(
        "--run-live-claude",
        action="store_true",
        help=(
            "Also run a real Claude Code host smoke. Requires "
            f"{LIVE_CLAUDE_ACK_ENV}={LIVE_CLAUDE_ACK_VALUE} and can consume quota."
        ),
    )
    parser.add_argument(
        "--live-claude-max-budget-usd",
        type=float,
        default=LIVE_CLAUDE_DEFAULT_BUDGET_USD,
        help=(
            "Maximum live Claude Code API budget for --run-live-claude. "
            f"Default: {LIVE_CLAUDE_DEFAULT_BUDGET_USD}."
        ),
    )
    parser.add_argument(
        "--claude-bin",
        type=Path,
        help="Explicit Claude Code executable path for --run-live-claude.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = args.repo_root.resolve()
    if args.temp_root is not None:
        temp_root = args.temp_root.resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        run_contract(
            project_root=project_root,
            temp_root=temp_root,
            fast=args.fast,
            run_live_claude=args.run_live_claude,
            live_claude_max_budget_usd=args.live_claude_max_budget_usd,
            live_claude_bin=args.claude_bin,
        )
        print(f"clean-host contract passed; temp root kept at {temp_root}")
        return 0

    temp_dir = tempfile.mkdtemp(prefix="ctx-clean-host-")
    temp_root = Path(temp_dir).resolve()
    try:
        run_contract(
            project_root=project_root,
            temp_root=temp_root,
            fast=args.fast,
            run_live_claude=args.run_live_claude,
            live_claude_max_budget_usd=args.live_claude_max_budget_usd,
            live_claude_bin=args.claude_bin,
        )
        print(f"clean-host contract passed under {temp_root}")
        return 0
    finally:
        if args.keep_temp:
            print(f"kept temp root: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
