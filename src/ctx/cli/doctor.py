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
    if verification.has_deterministic_verification:
        command = verification.best("test")
        assert command is not None
        results.append((True, f"tests runnable via `{' '.join(command.command)}`"))
    elif verification.declares_test_command:
        results.append((False, "a test command is declared but no test files were found"))
    else:
        results.append((False, "no test command discovered; nothing could be verified"))
    return results


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report what works and what does not. Never fails the process on a warning."""

    repo = Path(args.repo)
    print("CTX Fit diagnostics\n")

    checks: list[tuple[str, tuple[bool, str]]] = [
        ("provider", _check_provider()),
        ("pricing", _check_pricing()),
        ("catalog", _check_catalog()),
    ]
    for label, (ok, detail) in checks:
        print(f"  [{'ok ' if ok else 'no '}] {label:<10} {detail}")

    print(f"\nRepository: {repo}")
    blocking = 0
    for ok, detail in _check_repo(repo):
        print(f"  [{'ok ' if ok else 'no '}] {detail}")
        blocking += 0 if ok else 1

    print()
    if blocking:
        print(
            "This repository cannot be fully evaluated yet. Fix the items marked "
            "'no' above, then re-run `ctx fit`."
        )
    else:
        print("This repository is ready for `ctx fit --test --budget N`.")
    if not shutil.which("git"):
        print("Note: git is not on PATH, so task derivation is unavailable.")
    return 0


__all__ = ["PROVIDER_ENV_VARS", "cmd_doctor", "register"]
