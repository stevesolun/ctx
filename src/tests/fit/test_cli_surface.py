"""The public command surface is part of the product, so it is tested like one."""

from __future__ import annotations

import re
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


def test_unknown_command_error_does_not_leak_the_hidden_surface() -> None:
    """Mistyping a command is how users go looking. It must not answer.

    Hiding a command takes three edits, not two: dropping ``help=`` clears the
    listing and the metavar override clears the usage line, but argparse
    renders ``action.choices`` verbatim in its invalid-choice error -- so
    ``ctx nosuchcmd`` printed the full hidden list right back.
    """

    result = _ctx("nosuchcmd")

    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "invalid choice: 'nosuchcmd'" in combined
    for command in ADVERTISED:
        assert command in combined, combined
    for command in HIDDEN_BUT_WORKING:
        assert command not in combined, f"{command} leaked in the error: {combined}"


def test_unrelated_invalid_choice_errors_are_untouched() -> None:
    """Only the subcommand error is rewritten, not every `choose from`.

    The rewrite is anchored on the subcommand action's metavar. Without that
    anchor it would overwrite the choices of every other flag in the CLI.
    """

    result = _ctx("advanced", "run", "--task", "x", "--ctx-engine-mode", "bogus")

    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "argument --ctx-engine-mode: invalid choice: 'bogus'" in combined
    # 3.12 quotes the choices, 3.11 does not; the point is that argparse's own
    # message is untouched, not which interpreter rendered it.
    assert re.search(r"choose from '?legacy'?, '?shadow'?, '?recommend'?", combined), combined


def test_bare_advanced_is_a_usage_error_not_a_success() -> None:
    """`ctx advanced` supplies no command, so it must exit 2, not 0.

    The help was printed by ``parser.parse_args(["advanced", "--help"])``,
    which raises SystemExit(0) from inside argparse: the ``return 2`` after it
    was unreachable and ``ctx advanced && echo ok`` printed ok.
    """

    for argv in (("advanced",), ("advanced", "--")):
        result = _ctx(*argv)

        assert result.returncode == 2, f"{argv} exited {result.returncode}: {result.stdout}"
        # The help that gets printed must be the advanced parser's own, and it
        # has to name the commands it actually offers.
        assert "usage: ctx advanced" in result.stdout, result.stdout
        for command in HIDDEN_BUT_WORKING:
            assert command in result.stdout, f"{command} missing from: {result.stdout}"


def test_unknown_advanced_command_is_attributed_to_advanced() -> None:
    """`ctx advanced bogus` must say which level rejected the word.

    Dispatch re-entered ``main(rest)`` with a parser whose prog is ``ctx``, so
    the error named the top-level argument and never mentioned ``advanced``.
    """

    result = _ctx("advanced", "bogus")

    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "usage: ctx advanced" in combined, combined
    assert "ctx advanced: error: invalid choice: 'bogus'" in combined, combined


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
