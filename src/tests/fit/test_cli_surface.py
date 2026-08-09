"""The public command surface is part of the product, so it is tested like one."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ADVERTISED = ("fit", "doctor", "advanced")
HIDDEN_BUT_WORKING = ("run", "resume", "sessions")


def _ctx(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ctx", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_help_advertises_only_the_product_surface() -> None:
    """A one-command product must not present a toolbox in its help."""

    result = _ctx("--help")

    assert result.returncode == 0
    usage = result.stdout.splitlines()[0]
    for command in ADVERTISED:
        assert command in usage, usage
    for command in HIDDEN_BUT_WORKING:
        assert command not in usage, f"{command} should not be advertised: {usage}"
    # argparse renders SUPPRESS literally for subparser choices; catch that.
    assert "==SUPPRESS==" not in result.stdout


@pytest.mark.parametrize("command", HIDDEN_BUT_WORKING)
def test_historical_commands_keep_working(command: str) -> None:
    """Hiding a command from help must not break scripts that still call it."""

    result = _ctx(command, "--help")

    assert result.returncode == 0
    assert f"usage: ctx {command}" in result.stdout


@pytest.mark.parametrize("command", HIDDEN_BUT_WORKING)
def test_advanced_namespace_reaches_the_same_command(command: str) -> None:
    result = _ctx("advanced", command, "--help")

    assert result.returncode == 0
    assert f"usage: ctx {command}" in result.stdout


def test_bare_ctx_runs_the_product_rather_than_erroring() -> None:
    """Typing the one command must do something useful, not print an error."""

    result = _ctx()

    assert result.returncode == 0
    assert "Repository:" in result.stdout
    assert "AI agent readiness" in result.stdout


def test_doctor_reports_why_a_real_run_is_not_possible() -> None:
    result = _ctx("doctor")

    assert result.returncode == 0
    assert "CTX Fit diagnostics" in result.stdout
    # Every check reports ok or no, never silence.
    assert "provider" in result.stdout
    assert "catalog" in result.stdout
