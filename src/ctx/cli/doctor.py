"""``ctx doctor`` — diagnose why CTX Fit cannot do something.

A support tool, deliberately outside the normal product flow. Its job is to
turn "it didn't work" into a specific, actionable cause: no provider
credentials, no catalog, no Git, no tests.
"""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ctx.fit.verification import VerificationCommand

PROVIDER_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")

#: Mirrors the fallback ``ctx fit`` uses when nothing was discovered, so doctor
#: derives tasks against the same command the product would.
_FALLBACK_TEST_COMMAND = ("python", "-m", "pytest", "-q")

_NODE_LAUNCHERS = frozenset({"npm", "npx", "pnpm", "yarn"})

_DiagnosticState = Literal["ok", "no", "unknown"]


@dataclass(frozen=True, slots=True)
class _Diagnostic:
    """One observation and which verdict it is allowed to affect."""

    state: _DiagnosticState
    detail: str
    blocks_plan: bool = False
    blocks_live: bool = False

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def _observed(
    result: tuple[bool, str], *, blocks_plan: bool = False, blocks_live: bool = False
) -> _Diagnostic:
    ok, detail = result
    return _Diagnostic(
        state="ok" if ok else "no",
        detail=detail,
        blocks_plan=blocks_plan,
        blocks_live=blocks_live,
    )


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


def _resolve_selected_model(repo: Path) -> tuple[str | None, _Diagnostic]:
    """Apply the same default/applied precedence as experiment resolution."""

    from ctx.fit.applied_configuration import (
        AppliedConfigurationError,
        load_applied_configuration,
    )
    from ctx.fit.experiment import DEFAULT_MODEL

    try:
        applied = load_applied_configuration(repo)
    except AppliedConfigurationError as exc:
        return None, _Diagnostic(
            state="no",
            detail=f"selected model is unknown because {exc}",
            blocks_plan=True,
            blocks_live=True,
        )
    if applied is not None:
        return applied.model, _Diagnostic(
            state="ok", detail=f"selected applied model `{applied.model}`"
        )
    return DEFAULT_MODEL, _Diagnostic(
        state="ok", detail=f"selected default model `{DEFAULT_MODEL}`"
    )


def _check_provider(model: str | None) -> tuple[bool, str]:
    if model is None:
        return False, "no matching credential can be selected while the model is unknown"

    from ctx.fit.providers import resolve_model_credential

    credential = resolve_model_credential(model)
    name = credential.environment_variable
    if name is None:
        return False, (
            f"selected model `{model}` has no credential environment resolved by the "
            "ctx run provider contract; evaluation would use simulation"
        )
    if credential.configured:
        return True, (
            f"selected model `{model}`; matching credential configured: {name} "
            "(configured, not authenticated)"
        )
    return False, (
        f"selected model `{model}` requires {name}; no matching credential is configured, "
        "so evaluation would use simulation"
    )


def _check_pricing(model: str | None) -> tuple[bool, str]:
    """Require a complete rate for the model the experiment would execute."""

    if model is None:
        return False, "selected model is unknown, so exact pricing cannot be derived"

    from ctx.fit.experiment import ModelPrice

    price = ModelPrice.from_litellm(model)
    if price is None:
        return False, f"no exact LiteLLM pricing is available for selected model `{model}`"
    return True, f"exact pricing available for selected model `{model}` ({price.source})"


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


def _check_live_driver() -> tuple[bool, str]:
    """Run the real driver's no-spend prerequisite check.

    This calls no model and starts no subprocess. The provider module checks
    the CTX harness, the npx filesystem-MCP launcher, and the supported host
    sandbox using the same builder live execution calls before creating a
    workspace. npx itself also needs Node on the scrubbed PATH, which the
    builder's executable lookup alone cannot establish.
    """

    from ctx.fit.providers import provider_diagnostics

    ok, detail = provider_diagnostics()
    if not ok:
        return False, detail
    isolated_path = os.environ.get("PATH", os.defpath)
    if shutil.which("node", path=isolated_path) is None:
        return False, (
            "node is not on the PATH inherited by the isolated harness, so the "
            "npx workspace filesystem MCP cannot start"
        )
    return True, "ctx harness, npx filesystem MCP runtime, and platform sandbox are available"


def _check_verification_runtime(command: VerificationCommand) -> _Diagnostic:
    """Observe executable availability without pretending to run the suite.

    Repository verification inherits the caller's PATH inside a scrubbed HOME.
    npm, pnpm, and yarn launch JavaScript through Node, so both executables are
    prerequisites even though discovery records only the package manager argv.
    Presence is still not proof that repository dependencies work: doctor is
    deliberately read-only and neither installs nor executes repository code.
    """

    isolated_path = os.environ.get("PATH", os.defpath)
    launcher = command.command[0]
    required = [launcher]
    if launcher in _NODE_LAUNCHERS and "node" not in required:
        required.insert(0, "node")
    missing = tuple(name for name in required if shutil.which(name, path=isolated_path) is None)
    rendered = " and ".join(missing)
    if missing:
        return _Diagnostic(
            state="no",
            detail=(
                f"{rendered} {'are' if len(missing) > 1 else 'is'} not on the PATH inherited "
                "by isolated verification"
            ),
            blocks_live=True,
        )

    executable_detail = ", ".join(required)
    if command.validated:
        return _Diagnostic(
            state="ok",
            detail=f"validated verification command has executable runtime: {executable_detail}",
        )
    return _Diagnostic(
        state="unknown",
        detail=(
            f"verification runtime found on the isolated PATH ({executable_detail}), but "
            "doctor does not execute the command or install its dependencies; live "
            "verification is not yet proven runnable"
        ),
        blocks_live=True,
    )


def _check_repo(repo: Path) -> list[_Diagnostic]:
    from ctx.fit.profile import build_fit_profile

    results: list[_Diagnostic] = []
    if not repo.is_dir():
        return [
            _Diagnostic(
                state="no",
                detail=f"{repo} is not a directory",
                blocks_plan=True,
                blocks_live=True,
            )
        ]

    has_git = (repo / ".git").exists()
    results.append(
        _Diagnostic(
            state="ok" if has_git else "no",
            detail=(
                "Git history present"
                if has_git
                else "no Git history; tasks cannot be derived from commits"
            ),
            blocks_plan=not has_git,
            blocks_live=not has_git,
        )
    )

    try:
        profile = build_fit_profile(repo)
    except OSError as exc:
        results.append(
            _Diagnostic(
                state="no",
                detail=f"repository could not be profiled: {exc}",
                blocks_plan=True,
                blocks_live=True,
            )
        )
        return results

    verification = profile.verification
    command = verification.best("test")
    if verification.has_deterministic_verification:
        assert command is not None
        # Discovery, not a trial run: doctor never executes the suite, so it
        # must not claim the command was observed to work.
        results.append(
            _Diagnostic(
                state="ok", detail=f"test command discovered: `{' '.join(command.command)}`"
            )
        )
        results.append(_check_verification_runtime(command))
    elif verification.declares_test_command:
        results.append(
            _Diagnostic(
                state="no",
                detail="a test command is declared but no test files were found",
                blocks_plan=True,
                blocks_live=True,
            )
        )
    else:
        results.append(
            _Diagnostic(
                state="no",
                detail="no test command discovered; nothing could be verified",
                blocks_plan=True,
                blocks_live=True,
            )
        )

    results.append(
        _observed(
            _check_tasks(repo, command.command if command else _FALLBACK_TEST_COMMAND),
            blocks_plan=True,
            blocks_live=True,
        )
    )
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

    selected_model, model_check = _resolve_selected_model(repo)
    provider_ok, provider_detail = _check_provider(selected_model)
    # Planning and live readiness are separate claims. Static profile evidence
    # can support a zero-spend plan even when the actual harness, sandbox, or
    # repository runtime is missing. Only a check that blocks planning belongs
    # under "would refuse"; every unproven live prerequisite still prevents the
    # stronger ready verdict.
    checks: list[tuple[str, _Diagnostic]] = [
        ("model", model_check),
        (
            "provider",
            _Diagnostic(
                state="ok" if provider_ok else "unknown",
                detail=provider_detail,
                blocks_live=not provider_ok,
            ),
        ),
        (
            "pricing",
            _observed(_check_pricing(selected_model), blocks_plan=True, blocks_live=True),
        ),
        (
            "catalog",
            _observed(_check_catalog(), blocks_plan=True, blocks_live=True),
        ),
        ("git", _observed(_check_git_binary(), blocks_plan=True, blocks_live=True)),
        ("live driver", _observed(_check_live_driver(), blocks_live=True)),
    ]
    plan_blockers: list[str] = []
    live_gaps: list[str] = []
    markers = {"ok": "ok ", "no": "no ", "unknown": "?? "}
    for label, check in checks:
        print(f"  [{markers[check.state]}] {label:<10} {check.detail}")
        if not check.ok and check.blocks_plan:
            plan_blockers.append(check.detail)
        if not check.ok and check.blocks_live:
            live_gaps.append(check.detail)

    print(f"\nRepository: {repo}")
    for check in _check_repo(repo):
        print(f"  [{markers[check.state]}] {check.detail}")
        if not check.ok and check.blocks_plan:
            plan_blockers.append(check.detail)
        if not check.ok and check.blocks_live:
            live_gaps.append(check.detail)

    print()
    if plan_blockers:
        print("`ctx fit --test --budget N` would refuse to run here:")
        for detail in plan_blockers:
            print(f"  - {detail}")
        print("\nFix those, then re-run `ctx doctor`.")
    elif live_gaps:
        print(
            "This repository has enough static evidence to plan "
            "`ctx fit --test --budget N`, but a live campaign is not yet proven runnable here:"
        )
        for detail in live_gaps:
            print(f"  - {detail}")
        print(
            "\nIt is not ready for live evaluation. `ctx doctor` installs nothing, "
            "executes no repository code, and calls no model."
        )
        if not provider_ok:
            print(
                "Without the selected model's matching credential, `ctx fit --test` uses "
                "simulation: it proves the pipeline, not this repository."
            )
    else:
        print("This repository is ready for a live `ctx fit --test --budget N` campaign.")
    return 0


__all__ = ["PROVIDER_ENV_VARS", "cmd_doctor", "register"]
