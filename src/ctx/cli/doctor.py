"""``ctx doctor`` — diagnose why CTX Fit cannot do something.

A support tool, deliberately outside the normal product flow. Its job is to
turn "it didn't work" into a specific, actionable cause: no provider
credentials, no catalog, no Git, no tests.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

PROVIDER_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CTX_FIT_API_KEY")

#: Mirrors the fallback ``ctx fit`` uses when nothing was discovered, so doctor
#: derives tasks against the same command the product would.
_FALLBACK_TEST_COMMAND = ("python", "-m", "pytest", "-q")


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "doctor",
        help="Diagnose the CTX Fit installation and environment.",
        description="Check what CTX Fit can and cannot do here, and why.",
    )
    parser.add_argument(
        "repo",
        nargs="?",
        default=".",
        help="Repository to diagnose (default: the current directory).",
    )


def _check_provider() -> tuple[bool, str]:
    present = [name for name in PROVIDER_ENV_VARS if os.environ.get(name)]
    if present:
        return True, f"credentials found in {', '.join(present)}"
    return False, (
        "no provider credentials; evaluation would run in simulation only. "
        f"Set one of: {', '.join(PROVIDER_ENV_VARS)}"
    )


def _check_pricing() -> tuple[bool, str]:
    try:
        import litellm
    except ImportError:
        return False, "litellm is not installed, so no cost estimate can be derived"
    table = getattr(litellm, "model_cost", None)
    if not isinstance(table, dict) or not table:
        return False, "litellm exposes no pricing table"
    return True, f"pricing available for {len(table)} models"


def _check_catalog() -> tuple[bool, str]:
    from ctx.fit.release_catalog import open_release_candidate_source

    source = open_release_candidate_source()
    if source is None:
        return False, "the capability catalog could not be opened; no candidate can be proposed"
    return True, f"{len(source.entries)} capabilities available"


def _check_git_binary() -> tuple[bool, str]:
    """The binary, not the directory.

    ``.git`` is a filesystem check that passes on a host with no git installed,
    and every task is derived by shelling out to git. Reporting this after the
    verdict, as a note, let doctor call a repository "ready" on a host where
    task derivation cannot run at all.
    """

    if shutil.which("git") is None:
        return False, "git is not on PATH, so no task can be derived from history"
    return True, "git is available"


def _check_repo(repo: Path) -> list[tuple[bool, str]]:
    from ctx.fit.profile import build_fit_profile

    results: list[tuple[bool, str]] = []
    if not repo.is_dir():
        return [(False, f"{repo} is not a directory")]

    has_git = (repo / ".git").exists()
    results.append(
        (
            has_git,
            "Git history present"
            if has_git
            else "no Git history; tasks cannot be derived from commits",
        )
    )

    try:
        profile = build_fit_profile(repo)
    except OSError as exc:
        results.append((False, f"repository could not be profiled: {exc}"))
        return results

    verification = profile.verification
    command = verification.best("test")
    if verification.has_deterministic_verification:
        assert command is not None
        # Discovery, not a trial run: doctor never executes the suite, so it
        # must not claim the command was observed to work.
        results.append((True, f"test command discovered: `{' '.join(command.command)}`"))
    elif verification.declares_test_command:
        results.append((False, "a test command is declared but no test files were found"))
    else:
        results.append((False, "no test command discovered; nothing could be verified"))

    results.append(_check_tasks(repo, command.command if command else _FALLBACK_TEST_COMMAND))
    return results


def _check_tasks(repo: Path, verify_command: tuple[str, ...]) -> tuple[bool, str]:
    """Whether history yields a task at all — the commonest reason ``fit`` refuses.

    ``derive_tasks`` reads Git and nothing else, so this costs a few subprocess
    calls and spends nothing. Asking it here is the difference between naming
    the cause and green-lighting a command that immediately declines.
    """

    from ctx.fit.tasks import derive_tasks

    derived = derive_tasks(repo, verify_command=verify_command, limit=1)
    if derived.tasks:
        return True, f"a representative task can be derived from `{derived.tasks[0].provenance}`"
    return False, (
        "no representative task could be derived from recent history; CTX Fit needs a "
        "small commit that changes both source and test files"
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what works and what does not. Never fails the process on a warning."""

    repo = Path(args.repo)
    print("CTX Fit diagnostics\n")

    provider_ok, provider_detail = _check_provider()
    # Every failing check that would make `ctx fit --test` refuse has to reach
    # the verdict, or doctor contradicts itself. Missing credentials are the one
    # exception: they degrade the run to simulation rather than stopping it, so
    # this check is reported without blocking.
    checks: list[tuple[str, tuple[bool, str], bool]] = [
        ("provider", (provider_ok, provider_detail), False),
        ("pricing", _check_pricing(), True),
        ("catalog", _check_catalog(), True),
        ("git", _check_git_binary(), True),
    ]
    blocking: list[str] = []
    for label, (ok, detail), counts in checks:
        print(f"  [{'ok ' if ok else 'no '}] {label:<10} {detail}")
        if not ok and counts:
            blocking.append(detail)

    print(f"\nRepository: {repo}")
    for ok, detail in _check_repo(repo):
        print(f"  [{'ok ' if ok else 'no '}] {detail}")
        if not ok:
            blocking.append(detail)

    print()
    if blocking:
        print("`ctx fit --test --budget N` would refuse to run here:")
        for detail in blocking:
            print(f"  - {detail}")
        print("\nFix those, then re-run `ctx doctor`.")
    else:
        print("This repository is ready for `ctx fit --test --budget N`.")
        if not provider_ok:
            print(
                "Without provider credentials that run is a simulation: it proves "
                "the pipeline, not this repository."
            )
    return 0


__all__ = ["PROVIDER_ENV_VARS", "cmd_doctor", "register"]
