"""Clean-host contract runner for ctx release hardening.

The contract builds the current tree into a wheel, installs it into a
fresh virtualenv, points all user-state environment variables at a temp
root, then drives real console scripts. It is intentionally not a public
entrypoint yet; this is release infrastructure.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


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
    ) -> CompletedCommand:
        print("+ " + " ".join(args), flush=True)
        result = subprocess.run(
            list(args),
            cwd=cwd,
            env=dict(env),
            text=True,
            capture_output=True,
            check=False,
        )
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
    env = os.environ.copy()
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
    env.pop("CTX_WIKI_DIR", None)
    env.pop("CTX_GRAPH_PATH", None)
    env.pop("CLAUDE_HOME", None)
    if extra_pythonpath is not None:
        existing = env.get("PYTHONPATH")
        parts = [str(extra_pythonpath)]
        if existing:
            parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(parts)
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


def run_contract(
    *,
    project_root: Path,
    temp_root: Path,
    fast: bool,
    runner: CommandRunner | None = None,
) -> None:
    runner = runner or CommandRunner()
    paths = make_paths(temp_root)
    _prepare_dirs(paths)
    write_tiny_repo(paths.tiny_repo)
    write_fake_litellm(paths.fake_modules)

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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = args.repo_root.resolve()
    if args.temp_root is not None:
        temp_root = args.temp_root.resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        run_contract(project_root=project_root, temp_root=temp_root, fast=args.fast)
        print(f"clean-host contract passed; temp root kept at {temp_root}")
        return 0

    temp_dir = tempfile.mkdtemp(prefix="ctx-clean-host-")
    temp_root = Path(temp_dir).resolve()
    try:
        run_contract(project_root=project_root, temp_root=temp_root, fast=args.fast)
        print(f"clean-host contract passed under {temp_root}")
        return 0
    finally:
        if args.keep_temp:
            print(f"kept temp root: {temp_root}")
        else:
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
