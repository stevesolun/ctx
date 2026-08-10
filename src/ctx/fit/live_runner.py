"""The real trial runner: execute a candidate, then let the repository judge it.

This is the seam between "the pipeline works" and "we know something about your
repository". Everything else in CTX Fit is arithmetic over what this module
reports, so its honesty determines the honesty of the whole product.

Four properties are non-negotiable here.

**Isolation.** Each trial runs in its own throwaway workspace, materialized
from the repository's object store at a pinned commit. Only read-only Git
plumbing is ever pointed at the user's repository, so a trial can never see or
disturb another trial's edits, and the user's working tree is never touched --
including when that working tree is a linked ``git worktree``, whose ``.git``
is a *file* aimed back at the real repository.

**The repository judges, not the agent.** Success means the repository's own
test command exited zero after the agent finished, with the task's test files
byte-for-byte as they were handed over. What the agent said about its own work
is not consulted, and an agent that edited the specification has not satisfied
it: that trial is void.

**The task must have started red.** Before the agent runs, the reverted tree is
tested and must fail. A task that already passes cannot distinguish a working
configuration from a broken one, so it is discarded rather than scored.

**Cost is reported or admitted unknown.** A trial whose spend could not be
measured reports ``None``, which poisons the candidate total rather than
flattering it.
"""

from __future__ import annotations

import hashlib
import subprocess
import tarfile
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

#: Exporting a whole tree is heavier than the path-limited checkout this
#: replaced, so it gets more room before it is called a hung repository.
_EXPORT_TIMEOUT_SECONDS = 120

#: The trial's specification changed while the trial was running, so the agent
#: -- not the repository -- decided the verdict. Such a trial measured nothing:
#: it must never be read as ``verified``, and it must not be charged to the
#: candidate as ``failed`` either. ``TrialOutcome`` in :mod:`ctx.fit.execution`
#: does not name this value yet; every consumer there keys off the scored set
#: ``{verified, failed, inconclusive}``, so a value outside it is already
#: handled exactly as a void trial should be -- excluded from reliability while
#: its spend is still counted.
INVALID_TESTS_MODIFIED = "invalid-tests-modified"


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


def _export_tree(
    repo: Path, treeish: str, destination: Path, *, paths: tuple[str, ...] = ()
) -> tuple[str | None, frozenset[str]]:
    """Unpack ``treeish`` out of ``repo``'s object store into ``destination``.

    Returns an error string (or None) and the files actually written.

    ``git archive`` is the whole point: it reads objects and writes a tar to
    stdout, so it cannot touch the source repository's index, refs, HEAD or
    working tree no matter what ``repo/.git`` turns out to be. Running a
    *writing* command such as ``git checkout`` against a copied ``.git`` is how
    a trial ended up rewriting the user's real index (FITBUG-004): in a linked
    worktree that ``.git`` is a file holding an absolute path back to the
    original repository, and copying it copies the aim, not the target.

    The written set is reported because ``git archive`` honours
    ``export-ignore``: a repository can omit a tracked file from its own export
    and still exit zero, so "the command succeeded" does not mean "the file is
    there". The caller has to check.
    """

    command = ["git", "-C", str(repo), "archive", "--format=tar", treeish]
    if paths:
        command.extend(("--", *paths))

    with tempfile.TemporaryDirectory(prefix="ctx-fit-export-") as staging:
        bundle = Path(staging) / "tree.tar"
        try:
            with bundle.open("wb") as sink:
                completed = subprocess.run(
                    command,
                    stdout=sink,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=_EXPORT_TIMEOUT_SECONDS,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return f"reading {treeish} from the repository timed out", frozenset()
        except OSError as exc:
            return f"git could not be run: {exc}", frozenset()
        if completed.returncode != 0:
            detail = completed.stderr.strip()[:200]
            return f"could not read {treeish} from the repository: {detail}", frozenset()

        try:
            destination.mkdir(parents=True, exist_ok=True)
            with tarfile.open(bundle) as archive:
                written = frozenset(item.name for item in archive.getmembers() if item.isfile())
                if hasattr(tarfile, "data_filter"):
                    archive.extractall(destination, filter="data")
                else:  # pragma: no cover - Python without extraction filters
                    archive.extractall(destination)
        except (OSError, tarfile.TarError) as exc:
            return f"workspace could not be created: {exc}", frozenset()
    return None, written


def _was_written(path: str, written: frozenset[str]) -> bool:
    """Did the export cover ``path``, whether it names a file or a directory?"""

    return path in written or any(item.startswith(f"{path.rstrip('/')}/") for item in written)


def _prepare_workspace(repo: Path, task: FitTask, destination: Path) -> str | None:
    """Build the task's starting tree, then revert the task's source change.

    Returns an error string, or None on success. The test files are pointedly
    *not* reverted: they are the specification the agent must satisfy.

    Nothing is copied out of the user's checkout, so the workspace is the
    pinned commit rather than whatever happens to be lying around in it, and it
    deliberately has no ``.git``. A copied one is either an alias for the user's
    real repository or, in a plain clone, the answer key: the very commit the
    agent is being asked to reimplement.
    """

    sha = task.provenance.removeprefix("commit ").strip()
    if not sha:
        return "task has no commit to revert"
    if not task.source_paths:
        # Reverting "everything" would revert the tests too, handing the agent
        # a green tree and calling it a pass.
        return "task names no source paths, so there is nothing to revert"

    error, _ = _export_tree(repo, sha, destination)
    if error is not None:
        return error

    error, reverted = _export_tree(repo, f"{sha}^", destination, paths=task.source_paths)
    if error is not None:
        return error
    if unreverted := tuple(path for path in task.source_paths if not _was_written(path, reverted)):
        return f"the repository's export rules hid {', '.join(unreverted)} from the revert"
    if absent := tuple(path for path in task.test_paths if not (destination / path).is_file()):
        # Silence here would be the worst kind: the suite could pass simply
        # because the test that judges the task never made it into the tree.
        return f"the workspace is missing the tests that decide this task: {', '.join(absent)}"
    return None


def _specification_digests(workspace: Path, test_paths: tuple[str, ...]) -> dict[str, str | None]:
    """Content digests of the files that decide the verdict.

    ``None`` records "not present", so a deletion reads as a change rather than
    as an absence of evidence.
    """

    digests: dict[str, str | None] = {}
    for path in test_paths:
        try:
            digests[path] = hashlib.sha256((workspace / path).read_bytes()).hexdigest()
        except OSError:
            digests[path] = None
    return digests


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

        if not task.test_paths:
            # Without a named specification there is nothing to protect, and an
            # unprotected verdict is one the agent can write for itself.
            return result(
                "infrastructure-failure",
                detail="task names no test files, so its verdict cannot be trusted",
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

            # The specification as handed over. The prompt asks the agent not to
            # touch it, but a request is not a control: deleting or emptying the
            # one failing test makes the suite exit zero, which would otherwise
            # be recorded as a verified trial for work nobody did.
            specification = _specification_digests(workspace, task.test_paths)

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

            # A trial in which no model was ever contacted teaches nothing about
            # the candidate. Without this check the tests are simply re-run,
            # found still red, and the candidate is blamed -- which is how a
            # broken driver argv once reported "no candidate works on your
            # repository" after running zero agents. Spending nothing while
            # failing to finish is the signature of a harness fault; burning
            # tokens and still not finishing is a real candidate failure.
            spent_nothing = not any((agent.input_tokens, agent.output_tokens, agent.cost_usd))
            if not agent.completed and spent_nothing:
                return result(
                    "infrastructure-failure",
                    detail=(
                        "the agent never ran and nothing was spent, so this trial says "
                        f"nothing about the candidate: {agent.detail or 'no detail reported'}"
                    ),
                )

            after = _specification_digests(workspace, task.test_paths)
            tampered = sorted(path for path in specification if after[path] != specification[path])
            if tampered:
                # Do not even ask for a verdict: it would be the agent's.
                return result(
                    INVALID_TESTS_MODIFIED,
                    input_tokens=agent.input_tokens,
                    output_tokens=agent.output_tokens,
                    cost_usd=agent.cost_usd,
                    detail=(
                        "the tests that decide this trial were changed during it "
                        f"({', '.join(tampered)}); the trial is void"
                    ),
                )

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
    "INVALID_TESTS_MODIFIED",
    "AgentDriver",
    "AgentInvocation",
    "AgentOutcome",
    "make_live_runner",
]
