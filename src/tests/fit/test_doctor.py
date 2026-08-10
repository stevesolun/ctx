"""``ctx doctor`` exists to name a cause, so its verdict must follow its checks.

Doctor's whole value is that it turns "it didn't work" into a specific,
actionable reason. A verdict computed from a subset of the checks destroys that:
the command printed a failing environment check and then, three lines later,
declared the repository ready for a command that exits 1. These tests pin the
verdict to every check that can actually stop `ctx fit --test`.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

import ctx.cli.doctor as doctor_module
from ctx.cli.doctor import PROVIDER_ENV_VARS, cmd_doctor

READY = "This repository is ready"
REFUSES = "would refuse to run here"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True, text=True)


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _repo_with_a_derivable_task(tmp_path: Path) -> Path:
    """Git, tests, and a small commit touching both source and test files."""

    repo = tmp_path / "healthy"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    _init(repo)
    # The scaffolding commit is load-bearing: a task reverts to the commit
    # before its own, so the source file must already exist in that parent.
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: scaffold the calc module")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add addition helper")
    return repo


def _repo_without_a_derivable_task(tmp_path: Path) -> Path:
    """Git and runnable tests, but one oversized commit: the squashed-import case."""

    repo = tmp_path / "squashed"
    (repo / "tests").mkdir(parents=True)
    _init(repo)
    for index in range(7):
        (repo / f"m{index}.py").write_text(
            f"def f{index}():\n    return {index}\n", encoding="utf-8"
        )
    (repo / "tests" / "test_all.py").write_text(
        "from m0 import f0\n\n\ndef test_f0():\n    assert f0() == 0\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial import")
    return repo


def _doctor(repo: Path, capsys: pytest.CaptureFixture[str]) -> str:
    # Diagnostics never fail the process; the verdict is in the text.
    assert cmd_doctor(argparse.Namespace(repo=str(repo))) == 0
    return capsys.readouterr().out


def test_a_failing_environment_check_reaches_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No pricing table means `ctx fit --test` exits 1, so doctor cannot say "ready"."""

    monkeypatch.setattr(
        doctor_module,
        "_check_pricing",
        lambda: (False, "litellm is not installed, so no cost estimate can be derived"),
    )

    out = _doctor(_repo_with_a_derivable_task(tmp_path), capsys)

    assert READY not in out
    assert REFUSES in out
    # Named under the verdict, not merely printed earlier and forgotten.
    assert out.rindex("litellm is not installed") > out.index(REFUSES)


def test_absent_credentials_are_reported_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing key downgrades a run to simulation; it does not stop one.

    Folding every failing check into the verdict must not overshoot into
    refusing a command that would in fact run.
    """

    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    out = _doctor(_repo_with_a_derivable_task(tmp_path), capsys)

    assert REFUSES not in out
    assert READY in out
    assert "proves the pipeline, not this repository" in out


def test_a_repository_with_no_derivable_task_is_not_called_ready(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Task derivation is a hard gate, and the commonest one to fail."""

    out = _doctor(_repo_without_a_derivable_task(tmp_path), capsys)

    assert READY not in out
    assert "no representative task could be derived" in out


def test_git_missing_from_path_blocks_the_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`.git` on disk is not a git binary, and every task comes from the binary."""

    repo = _repo_with_a_derivable_task(tmp_path)  # built while git is still reachable
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    out = _doctor(repo, capsys)

    assert READY not in out
    assert "git is not on PATH" in out


def test_a_discovered_test_command_is_not_reported_as_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Doctor never executes the suite, so it must not claim it was observed to run."""

    out = _doctor(_repo_with_a_derivable_task(tmp_path), capsys)

    assert "test command discovered" in out
    assert "tests runnable via" not in out
