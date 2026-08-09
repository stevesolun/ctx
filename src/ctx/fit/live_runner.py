"""The real trial runner: execute a candidate, then let the repository judge it.

This is the seam between "the pipeline works" and "we know something about your
repository". Everything else in CTX Fit is arithmetic over what this module
reports, so its honesty determines the honesty of the whole product.

Four properties are non-negotiable here.

**Isolation.** Each trial runs in its own throwaway copy of the repository at a
pinned commit. A trial can never see or disturb another trial's edits, and the
user's working tree is never touched.

**The repository judges, not the agent.** Success means the repository's own
test command exited zero after the agent finished. What the agent said about
its own work is not consulted.

**The task must have started red.** Before the agent runs, the reverted tree is
tested and must fail. A task that already passes cannot distinguish a working
configuration from a broken one, so it is discarded rather than scored.

**Cost is reported or admitted unknown.** A trial whose spend could not be
measured reports ``None``, which poisons the candidate total rather than
flattering it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import TrialResult
from ctx.fit.tasks import FitTask

#: A trial should never hang a campaign. Exceeding this is an inconclusive
#: trial, not a failed one: we learned nothing about the candidate.
DEFAULT_TRIAL_TIMEOUT_SECONDS = 900
DEFAULT_VERIFY_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class AgentInvocation:
    """What an agent is asked to do, with no hint of the original solution."""

    workspace: Path
    task_title: str
    files_to_change: tuple[str, ...]
    verify_command: tuple[str, ...]
    capability_ids: tuple[str, ...]
    model: str | None


@dataclass(frozen=True, slots=True)
class AgentOutcome:
    """What an agent reported. Deliberately never used to decide success."""

    completed: bool
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    detail: str = ""


#: Injected so the provider integration stays replaceable and testable.
AgentDriver = Callable[[AgentInvocation], AgentOutcome]


def _run(command: tuple[str, ...], cwd: Path, timeout: int) -> tuple[int | None, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except OSError as exc:
        return None, f"could not execute: {exc}"
    return completed.returncode, (completed.stdout + completed.stderr)[-2000:]


def _prepare_workspace(repo: Path, task: FitTask, destination: Path) -> str | None:
    """Copy the repository and revert the task's source change.

    Returns an error string, or None on success. The test files are pointedly
    *not* reverted: they are the specification the agent must satisfy.
    """

    try:
        shutil.copytree(
            repo,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"
            ),
            dirs_exist_ok=True,
        )
    except OSError as exc:
        return f"workspace could not be created: {exc}"

    sha = task.provenance.removeprefix("commit ").strip()
    if not sha:
        return "task has no commit to revert"

    # Revert only the source paths. `git checkout <sha>^ -- <paths>` restores
    # them to their pre-change state while leaving the tests in place.
    code, output = _run(
        ("git", "checkout", f"{sha}^", "--", *task.source_paths),
        destination,
        timeout=60,
    )
    if code != 0:
        return f"could not revert source paths: {output[:200]}"
    return None


def make_live_runner(
    repo_path: str | Path,
    driver: AgentDriver,
    *,
    trial_timeout: int = DEFAULT_TRIAL_TIMEOUT_SECONDS,
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> Callable[[CandidateConfiguration, FitTask, int], TrialResult]:
    """Build a runner that really executes and really verifies."""

    repo = Path(repo_path)

    def run(candidate: CandidateConfiguration, task: FitTask, index: int) -> TrialResult:
        started = time.monotonic()

        def result(outcome: str, **kwargs: object) -> TrialResult:
            return TrialResult(
                candidate_id=candidate.candidate_id,
                task_id=task.task_id,
                trial_index=index,
                outcome=outcome,  # type: ignore[arg-type]
                elapsed_seconds=round(time.monotonic() - started, 3),
                simulated=False,
                **kwargs,  # type: ignore[arg-type]
            )

        with tempfile.TemporaryDirectory(prefix="ctx-fit-trial-") as scratch:
            workspace = Path(scratch) / "repo"
            error = _prepare_workspace(repo, task, workspace)
            if error is not None:
                return result("infrastructure-failure", detail=error)

            # Red gate. If the reverted tree already passes, the task proves
            # nothing and must not be scored against any candidate.
            code, _ = _run(task.verify_command, workspace, timeout=verify_timeout)
            if code == 0:
                return result(
                    "infrastructure-failure",
                    detail="task did not start red; it cannot distinguish configurations",
                )

            try:
                agent = driver(
                    AgentInvocation(
                        workspace=workspace,
                        task_title=task.title,
                        files_to_change=task.source_paths,
                        verify_command=task.verify_command,
                        capability_ids=candidate.capability_ids,
                        model=candidate.model,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - any driver failure is ours, not the candidate's
                return result("infrastructure-failure", detail=f"agent driver failed: {exc}")

            # The repository decides. The agent's own claim is not consulted.
            code, output = _run(task.verify_command, workspace, timeout=verify_timeout)
            if code is None:
                outcome = "inconclusive"
                detail = f"verification did not complete: {output[:200]}"
            elif code == 0:
                outcome = "verified"
                detail = "repository tests passed after the change"
            else:
                outcome = "failed"
                detail = "repository tests still failing"

            return result(
                outcome,
                input_tokens=agent.input_tokens,
                output_tokens=agent.output_tokens,
                cost_usd=agent.cost_usd,
                detail=detail,
            )

    return run


__all__ = [
    "DEFAULT_TRIAL_TIMEOUT_SECONDS",
    "DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "AgentDriver",
    "AgentInvocation",
    "AgentOutcome",
    "make_live_runner",
]
