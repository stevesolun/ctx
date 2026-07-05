from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_no_mistakes_wrapper_prefers_valid_worktree_venv_over_broken_override(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(
        Path("scripts/no_mistakes_codex_env.sh"),
        script_dir / "no_mistakes_codex_env.sh",
    )

    worktree_bin = repo / ".venv" / "bin"
    worktree_bin.mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    _write_executable(worktree_bin / "python", "#!/usr/bin/env sh\ncat >/dev/null\n")

    broken_bin = tmp_path / "broken" / "bin"
    broken_bin.mkdir(parents=True)
    _write_executable(broken_bin / "python", "#!/usr/bin/env sh\ncat >/dev/null\nexit 1\n")

    fake_codex = tmp_path / "codex"
    _write_executable(
        fake_codex,
        "#!/usr/bin/env sh\n"
        "printf 'resolved=%s\\nvenv=%s\\npath=%s\\n' "
        '"$CTX_NO_MISTAKES_PYTHON_BIN_RESOLVED" "$VIRTUAL_ENV" "$PATH"\n',
    )

    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["CTX_NO_MISTAKES_PYTHON_BIN"] = str(broken_bin)
    env["CTX_NO_MISTAKES_CODEX_RESOURCES"] = str(tmp_path / "codex-resources")
    env["CTX_NO_MISTAKES_REAL_CODEX"] = str(fake_codex)

    result = subprocess.run(
        ["bash", str(script_dir / "no_mistakes_codex_env.sh")],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert f"resolved={worktree_bin}" in result.stdout
    assert f"venv={repo / '.venv'}" in result.stdout
    assert result.stdout.splitlines()[2].startswith(f"path={worktree_bin}:")


def test_no_mistakes_repo_config_defines_deterministic_commands() -> None:
    config = yaml.safe_load(Path(".no-mistakes.yaml").read_text(encoding="utf-8"))

    assert config["commands"] == {
        "test": "scripts/no_mistakes_run.sh test",
        "lint": "scripts/no_mistakes_run.sh lint",
        "format": "scripts/no_mistakes_run.sh format",
    }
    assert config["auto_fix"]["review"] == 3
    assert config["auto_fix"]["test"] == 3
    assert config["auto_fix"]["lint"] == 3


def test_no_mistakes_run_script_uses_trusted_python_for_configured_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(
        Path("scripts/no_mistakes_run.sh"),
        script_dir / "no_mistakes_run.sh",
    )

    fake_bin = repo / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    (repo / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    log_path = tmp_path / "python-args.log"
    _write_executable(
        fake_bin / "python",
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '-':\n"
        "    sys.stdin.read()\n"
        "    raise SystemExit(0)\n"
        "log = pathlib.Path(os.environ['FAKE_PYTHON_LOG'])\n"
        "with log.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(repr(sys.argv[1:]) + '\\n')\n",
    )

    env = os.environ.copy()
    env["CTX_NO_MISTAKES_PYTHON_BIN"] = str(fake_bin)
    env["FAKE_PYTHON_LOG"] = str(log_path)

    subprocess.run(
        ["bash", str(script_dir / "no_mistakes_run.sh"), "test"],
        cwd=repo,
        env=env,
        check=True,
    )
    subprocess.run(
        ["bash", str(script_dir / "no_mistakes_run.sh"), "lint"],
        cwd=repo,
        env=env,
        check=True,
    )

    assert log_path.read_text(encoding="utf-8").splitlines() == [
        "['scripts/ci_preflight.py', '--profile', 'pr']",
        "['-m', 'ruff', 'check', '.']",
        "['-m', 'ruff', 'format', '--check', 'src', 'hooks', 'scripts']",
        "['-m', 'mypy', 'src']",
    ]
