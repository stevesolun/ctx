from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
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
    env["CTX_NO_MISTAKES_CODEX_RESOURCES"] = str(tmp_path)
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


def test_no_mistakes_wrapper_uses_explicit_codex_resources(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(
        Path("scripts/no_mistakes_codex_env.sh"),
        script_dir / "no_mistakes_codex_env.sh",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    _write_executable(
        fake_codex,
        '#!/bin/sh\nprintf \'codex=%s\\nargs=%s\\n\' "$0" "$*"\n',
    )

    env = os.environ.copy()
    env.pop("CTX_NO_MISTAKES_REAL_CODEX", None)
    env["CTX_NO_MISTAKES_CODEX_RESOURCES"] = str(fake_bin)

    result = subprocess.run(
        ["bash", str(script_dir / "no_mistakes_codex_env.sh"), "review", "--json"],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.stdout.splitlines() == [
        f"codex={fake_codex}",
        "args=review --json",
    ]


def test_no_mistakes_wrapper_rejects_bad_explicit_codex(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(
        Path("scripts/no_mistakes_codex_env.sh"),
        script_dir / "no_mistakes_codex_env.sh",
    )
    missing_codex = tmp_path / "missing-codex"

    env = os.environ.copy()
    env["CTX_NO_MISTAKES_REAL_CODEX"] = str(missing_codex)

    result = subprocess.run(
        ["bash", str(script_dir / "no_mistakes_codex_env.sh"), "--version"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 127
    assert f"Configured Codex executable is not runnable: {missing_codex}" in result.stderr


@pytest.mark.parametrize("candidate_kind", ("directory", "self-symlink"))
def test_no_mistakes_wrapper_rejects_nonfile_or_self_codex(
    tmp_path: Path,
    candidate_kind: str,
) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    wrapper = script_dir / "no_mistakes_codex_env.sh"
    shutil.copy2(Path("scripts/no_mistakes_codex_env.sh"), wrapper)

    candidate = tmp_path / "codex"
    if candidate_kind == "directory":
        candidate.mkdir()
    else:
        candidate.symlink_to(wrapper)

    env = os.environ.copy()
    env["CTX_NO_MISTAKES_REAL_CODEX"] = str(candidate)

    result = subprocess.run(
        ["bash", str(wrapper), "--version"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
    )

    assert result.returncode == 127
    assert f"Configured Codex executable is not runnable: {candidate}" in result.stderr


def test_no_mistakes_wrapper_rejects_bad_explicit_resources(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(
        Path("scripts/no_mistakes_codex_env.sh"),
        script_dir / "no_mistakes_codex_env.sh",
    )
    missing_resources = tmp_path / "missing-resources"

    env = os.environ.copy()
    env.pop("CTX_NO_MISTAKES_REAL_CODEX", None)
    env["CTX_NO_MISTAKES_CODEX_RESOURCES"] = str(missing_resources)

    result = subprocess.run(
        ["bash", str(script_dir / "no_mistakes_codex_env.sh"), "--version"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    expected = missing_resources / "codex"
    assert result.returncode == 127
    assert f"Configured Codex resources do not contain a runnable codex: {expected}" in (
        result.stderr
    )


def test_no_mistakes_wrapper_validates_resources_with_explicit_codex(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    wrapper = script_dir / "no_mistakes_codex_env.sh"
    shutil.copy2(Path("scripts/no_mistakes_codex_env.sh"), wrapper)

    fake_codex = tmp_path / "codex"
    _write_executable(fake_codex, "#!/bin/sh\nexit 0\n")
    missing_resources = tmp_path / "missing-resources"

    env = os.environ.copy()
    env["CTX_NO_MISTAKES_REAL_CODEX"] = str(fake_codex)
    env["CTX_NO_MISTAKES_CODEX_RESOURCES"] = str(missing_resources)

    result = subprocess.run(
        ["bash", str(wrapper), "--version"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    expected = missing_resources / "codex"
    assert result.returncode == 127
    assert f"Configured Codex resources do not contain a runnable codex: {expected}" in (
        result.stderr
    )


def test_no_mistakes_wrapper_discovers_known_app_candidate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    wrapper = script_dir / "no_mistakes_codex_env.sh"
    shutil.copy2(Path("scripts/no_mistakes_codex_env.sh"), wrapper)

    fake_codex = tmp_path / "ChatGPT.app" / "Contents" / "Resources" / "codex"
    fake_codex.parent.mkdir(parents=True)
    _write_executable(fake_codex, "#!/bin/sh\nprintf 'known-app=%s\\n' \"$0\"\n")

    env = os.environ.copy()
    env.pop("CTX_NO_MISTAKES_REAL_CODEX", None)
    env.pop("CTX_NO_MISTAKES_CODEX_RESOURCES", None)
    env["CTX_NO_MISTAKES_CODEX_APP_PATHS"] = str(fake_codex)
    env["PATH"] = "/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(wrapper), "--version"],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.stdout.strip() == f"known-app={fake_codex}"
    assert "/Applications/ChatGPT.app/Contents/Resources/codex" in wrapper.read_text(
        encoding="utf-8"
    )


def test_no_mistakes_wrapper_falls_back_to_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    wrapper = script_dir / "no_mistakes_codex_env.sh"
    shutil.copy2(Path("scripts/no_mistakes_codex_env.sh"), wrapper)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    _write_executable(fake_codex, "#!/bin/sh\nprintf 'path=%s\\n' \"$0\"\n")

    env = os.environ.copy()
    env.pop("CTX_NO_MISTAKES_REAL_CODEX", None)
    env.pop("CTX_NO_MISTAKES_CODEX_RESOURCES", None)
    env["CTX_NO_MISTAKES_CODEX_APP_PATHS"] = ""
    env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin"

    result = subprocess.run(
        ["bash", str(wrapper), "--version"],
        cwd=repo,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.stdout.strip() == f"path={fake_codex}"


def test_no_mistakes_wrapper_fails_when_no_codex_is_available(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    script_dir = repo / "scripts"
    script_dir.mkdir(parents=True)
    wrapper = script_dir / "no_mistakes_codex_env.sh"
    shutil.copy2(Path("scripts/no_mistakes_codex_env.sh"), wrapper)

    env = os.environ.copy()
    env.pop("CTX_NO_MISTAKES_REAL_CODEX", None)
    env.pop("CTX_NO_MISTAKES_CODEX_RESOURCES", None)
    env["CTX_NO_MISTAKES_CODEX_APP_PATHS"] = ""
    env["PATH"] = "/usr/bin:/bin"

    result = subprocess.run(
        ["bash", str(wrapper), "--version"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 127
    assert "Unable to find a runnable Codex executable" in result.stderr


def test_no_mistakes_repo_config_defines_deterministic_commands() -> None:
    config = yaml.safe_load(Path(".no-mistakes.yaml").read_text(encoding="utf-8"))

    assert config["commands"] == {
        "test": "scripts/no_mistakes_run.sh test",
        "lint": "scripts/no_mistakes_run.sh lint",
        "format": "scripts/no_mistakes_run.sh format",
    }
    assert config["auto_fix"]["review"] == 0
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
        ["bash", str(script_dir / "no_mistakes_run.sh"), "fast", "--lane", "static"],
        cwd=repo,
        env=env,
        check=True,
    )
    subprocess.run(
        [
            "bash",
            str(script_dir / "no_mistakes_run.sh"),
            "fast",
            "--summary-json",
            "custom.json",
        ],
        cwd=repo,
        env=env,
        check=True,
    )
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
        (
            "['scripts/local_fast_gate.py', '--profile', 'pr', '--summary-json', "
            "'.gate/local-fast.json', '--lane', 'static']"
        ),
        (
            "['scripts/local_fast_gate.py', '--profile', 'pr', '--summary-json', "
            "'.gate/local-fast.json', '--summary-json', 'custom.json']"
        ),
        "['scripts/ci_preflight.py', '--profile', 'pr']",
        "['-m', 'ruff', 'check', '.']",
        "['-m', 'ruff', 'format', '--check', 'src', 'hooks', 'scripts']",
        "['-m', 'mypy', 'src']",
    ]


def test_no_mistakes_gate_requires_explicit_intent(tmp_path: Path) -> None:
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
        "pathlib.Path(os.environ['FAKE_PYTHON_LOG']).write_text(repr(sys.argv[1:]))\n",
    )

    env = os.environ.copy()
    env["CTX_NO_MISTAKES_PYTHON_BIN"] = str(fake_bin)
    env["FAKE_PYTHON_LOG"] = str(log_path)

    result = subprocess.run(
        ["bash", str(script_dir / "no_mistakes_run.sh"), "gate"],
        cwd=repo,
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 64
    assert "gate requires --intent or --intent-file" in result.stderr
    assert not log_path.exists()


@pytest.mark.parametrize("intent_source", ("argument", "file"))
def test_no_mistakes_gate_runs_local_fast_before_axi_run(
    tmp_path: Path,
    intent_source: str,
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
    python_log = tmp_path / "python-args.log"
    no_mistakes_log = tmp_path / "no-mistakes-args.log"
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
    fake_cmd_dir = tmp_path / "bin"
    fake_cmd_dir.mkdir()
    _write_executable(
        fake_cmd_dir / "no-mistakes",
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "pathlib.Path(os.environ['FAKE_NO_MISTAKES_LOG']).write_text("
        "repr(sys.argv[1:]) + '\\n', encoding='utf-8')\n",
    )

    env = os.environ.copy()
    env["CTX_NO_MISTAKES_PYTHON_BIN"] = str(fake_bin)
    env["FAKE_PYTHON_LOG"] = str(python_log)
    env["FAKE_NO_MISTAKES_LOG"] = str(no_mistakes_log)
    env["PATH"] = f"{fake_cmd_dir}{os.pathsep}{env['PATH']}"
    intent_args = ["--intent", "speed lane split"]
    if intent_source == "file":
        intent_path = tmp_path / "intent.txt"
        intent_path.write_text("speed lane split\n", encoding="utf-8")
        intent_args = ["--intent-file", str(intent_path)]

    subprocess.run(
        [
            "bash",
            str(script_dir / "no_mistakes_run.sh"),
            "gate",
            *intent_args,
            "--skip",
            "ci",
            "--yes",
        ],
        cwd=repo,
        env=env,
        check=True,
    )

    assert python_log.read_text(encoding="utf-8").splitlines() == [
        (
            "['scripts/local_fast_gate.py', '--profile', 'smoke', '--summary-json', "
            "'.gate/local-fast-smoke.json']"
        ),
        (
            "['scripts/local_fast_gate.py', '--profile', 'pr', '--summary-json', "
            "'.gate/local-fast.json']"
        ),
    ]
    assert no_mistakes_log.read_text(encoding="utf-8").strip() == (
        "['axi', 'run', '--intent', 'speed lane split', '--skip', 'ci', '--yes']"
    )
