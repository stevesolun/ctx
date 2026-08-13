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

An isolated *directory* is not yet an isolated *import*. Under an editable
install -- ``pip install -e .`` on a src-layout project, which is the standard
Python layout and what this repository itself uses -- the ambient interpreter
resolves the package through a ``.pth`` in its own site-packages that points at
the user's real source tree. The trial then reverts the source on disk and the
suite goes on importing the original, so the task is discarded as "did not
start red" and CTX Fit cannot evaluate its own repository (FITBUG-016).
:class:`CampaignEnvironment` closes that gap: one interpreter per campaign,
with the trial's workspace installed into it, re-aimed at each new workspace.
When such an environment cannot be built the trial refuses and says why --
never silently, because a task that vanishes without a reason is the half of
this defect the user actually sees.

**The repository judges, not the agent.** Success means the repository's own
test command exited zero after the agent finished, with the task's test files
byte-for-byte as they were handed over. What the agent said about its own work
is not consulted, and an agent that edited the specification has not satisfied
it: that trial is void.

**The task must have started red.** Before the agent runs, the workspace is
tested twice: green at the task's commit, then red once the source change is
reverted. Both halves are load-bearing. Without the green half, "the suite
exits non-zero" is indistinguishable from "the suite cannot run here" or "this
repository has an unrelated failing test", and either one turns every candidate
into a failure it never earned. A task that does not swing green-to-red is
discarded rather than scored.

**Cost is reported or admitted unknown.** A trial whose spend could not be
measured reports ``None``, which poisons the candidate total rather than
flattering it.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Mapping
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

#: Building the campaign's environment resolves a whole dependency set, which
#: can legitimately take minutes on a cold cache -- but not forever. A campaign
#: that hangs here has spent the user's time and learned nothing, so exceeding
#: this is an infrastructure failure with the cause stated.
DEFAULT_ENVIRONMENT_TIMEOUT_SECONDS = 900

#: Re-aiming a built environment at the next trial's workspace rewrites one
#: path file and resolves nothing, so it gets a fraction of the budget. This is
#: what makes one-environment-per-campaign affordable: the first trial pays for
#: the dependency set and every later trial pays for a path rewrite.
DEFAULT_REPOINT_TIMEOUT_SECONDS = 180

#: Extras that conventionally carry a project's test dependencies, best first.
#: The repository's own verify command is nearly always its test suite, and a
#: suite cannot run against runtime dependencies alone.
_TEST_EXTRAS: tuple[str, ...] = ("dev", "test", "tests", "testing", "develop")

#: Files whose contents decide what the environment has to contain. When none
#: of them is present there is no package to install -- and, just as important,
#: nothing that could have been installed elsewhere to shadow the workspace.
_PACKAGING_FILES: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
)

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


def _run(
    command: tuple[str, ...],
    cwd: Path,
    timeout: int,
    env: Mapping[str, str] | None = None,
) -> tuple[int | None, str]:
    """Run ``command``. ``env`` of None inherits this process's environment."""

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=None if env is None else dict(env),
        )
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except OSError as exc:
        return None, f"could not execute: {exc}"
    return completed.returncode, (completed.stdout + completed.stderr)[-2000:]


def _packaging_evidence(workspace: Path) -> tuple[Path, ...]:
    """The files in ``workspace`` that declare what has to be installed."""

    return tuple(path for name in _PACKAGING_FILES if (path := workspace / name).is_file())


def _dependency_digest(evidence: tuple[Path, ...]) -> str:
    """A digest of everything that decides the environment's contents.

    Tasks in one campaign come from different commits, so the second task's
    workspace can declare a dependency the first one never had. Re-pointing
    without noticing would leave the suite failing on a missing import, and the
    green gate would report it as "this repository's own tests do not pass" --
    true, but for a reason CTX created. When the digest moves, the dependency
    set is resolved again; when it does not, the trial pays for a path rewrite.
    """

    digest = hashlib.sha256()
    for path in evidence:
        digest.update(path.name.encode())
        try:
            digest.update(path.read_bytes())
        except OSError:  # pragma: no cover - it was a file a moment ago
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def _test_extra(workspace: Path) -> str | None:
    """The extra that most likely carries this project's test dependencies.

    An install of runtime dependencies alone leaves an environment that cannot
    run the repository's own suite -- which the green gate would correctly, and
    uselessly, report as "this workspace cannot judge a candidate".
    """

    try:
        declared = tomllib.loads((workspace / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    optional = declared.get("project", {}).get("optional-dependencies", {})
    if not isinstance(optional, dict):
        return None
    return next((extra for extra in _TEST_EXTRAS if extra in optional), None)


def _interpreter_environment(venv: Path) -> dict[str, str]:
    """The process environment that makes ``venv`` the interpreter in charge.

    The verify command is the repository's own (``python -m pytest -q``, and
    friends), so it is re-pointed by making this environment's ``bin`` win on
    ``PATH`` rather than by rewriting anyone's argv.

    ``PYTHONHOME`` is dropped because it would aim the environment's
    interpreter at a different installation's standard library, and
    ``PYTHONPATH`` because it is the remaining way for the user's own source
    tree to land on ``sys.path`` ahead of the workspace -- the exact shadowing
    this environment exists to end.
    """

    environment = dict(os.environ)
    bin_directory = venv / ("Scripts" if os.name == "nt" else "bin")
    environment["PATH"] = os.pathsep.join(
        item for item in (str(bin_directory), environment.get("PATH", "")) if item
    )
    environment["VIRTUAL_ENV"] = str(venv)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


@dataclass(frozen=True, slots=True)
class TrialEnvironment:
    """Which interpreter one trial's commands run under, and why."""

    env: dict[str, str] | None = None
    """Process environment for the trial's commands; None means the ambient one."""

    error: str | None = None
    """Why no environment could be built. Set means the trial must not proceed."""

    account: str = ""
    """What was done, in the user's terms. Reported when a task is discarded.

    A discarded task is the only thing the user sees of this machinery, so the
    discard has to say whether the workspace's own code was the code under test.
    """


#: Said when the repository declares no Python package of its own. Nothing can
#: be installed -- and nothing could have been installed *elsewhere* from this
#: tree either, so no installed copy exists to shadow the workspace and the
#: ambient interpreter is the honest answer rather than a shortcut.
_NOTHING_TO_INSTALL = (
    "this repository declares no installable Python package (no pyproject.toml, "
    "setup.py or setup.cfg), so the workspace's own files are what run"
)

_ISOLATED = "the workspace was installed into an environment built for this campaign"


class CampaignEnvironment:
    """One Python environment, built once per campaign, re-aimed per trial.

    Per-trial environments would be correct and unaffordable: every trial in a
    campaign shares one dependency set, so building it once means the first
    trial pays for the resolve and the rest pay for a path rewrite.

    Constructing this object does nothing. No interpreter is created, no
    package is downloaded and nothing is spent until :meth:`aim_at` is called
    from inside a trial the budget gate has already cleared (ADR-013), and even
    then the spend is disk and time -- never model tokens (ADR-004).
    """

    def __init__(
        self,
        *,
        base_python: str | Path | None = None,
        build_timeout: int = DEFAULT_ENVIRONMENT_TIMEOUT_SECONDS,
        repoint_timeout: int = DEFAULT_REPOINT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_python = Path(base_python) if base_python else Path(sys.executable)
        self._build_timeout = build_timeout
        self._repoint_timeout = repoint_timeout
        self._home: tempfile.TemporaryDirectory[str] | None = None
        self._venv: Path | None = None
        self._installed_digest: str | None = None
        self._unbuildable: str | None = None

    @property
    def venv(self) -> Path | None:
        """Where the campaign's environment lives, once it has been built."""

        return self._venv

    def aim_at(self, workspace: Path, verify_command: tuple[str, ...]) -> TrialEnvironment:
        """Make ``workspace`` the code that this campaign's interpreter imports."""

        evidence = _packaging_evidence(workspace)
        if not any(path.name in {"pyproject.toml", "setup.py", "setup.cfg"} for path in evidence):
            return TrialEnvironment(account=_NOTHING_TO_INSTALL)

        if self._unbuildable is not None:
            # An interpreter that could not be created once will not create
            # itself on the next trial, and retrying would charge every
            # remaining trial the full build timeout to reach the same answer.
            # The cause is repeated rather than dropped: each trial states it.
            return TrialEnvironment(error=self._unbuildable)

        if self._venv is None:
            self._unbuildable = self._build()
            if self._unbuildable is not None:
                return TrialEnvironment(error=self._unbuildable)

        error = self._install(workspace, evidence, verify_command)
        if error is not None:
            # Not sticky: this is about *this* workspace -- the commit under
            # test may declare something the next task's commit does not.
            return TrialEnvironment(error=error)

        assert self._venv is not None
        return TrialEnvironment(env=_interpreter_environment(self._venv), account=_ISOLATED)

    def close(self) -> None:
        """Remove the environment. Safe to call more than once."""

        if self._home is not None:
            self._home.cleanup()
            self._home = None
        self._venv = None
        self._installed_digest = None

    def _build(self) -> str | None:
        """Create the interpreter. Returns a stated cause, or None on success."""

        self._home = tempfile.TemporaryDirectory(prefix="ctx-fit-env-", ignore_cleanup_errors=True)
        home = Path(self._home.name)
        venv = home / "env"
        code, output = _run(
            (str(self._base_python), "-m", "venv", str(venv)), home, timeout=self._build_timeout
        )
        if code != 0:
            return (
                "this trial needs an environment of its own so that the workspace -- not an "
                "editable install pointing at your source tree -- is the code under test, and "
                f"one could not be created with {self._base_python}: "
                f"{output.strip()[-200:] or 'no output'}"
            )
        self._venv = venv
        return None

    def _install(
        self, workspace: Path, evidence: tuple[Path, ...], verify_command: tuple[str, ...]
    ) -> str | None:
        """Install ``workspace`` into the environment. Returns a cause or None."""

        assert self._venv is not None
        python = self._venv / ("Scripts" if os.name == "nt" else "bin") / "python"
        digest = _dependency_digest(evidence)
        resolve = digest != self._installed_digest
        extra = _test_extra(workspace)

        attempts: list[tuple[str, ...]] = []
        for target in (f"{workspace}[{extra}]", str(workspace)) if extra else (str(workspace),):
            command = [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--editable",
                target,
            ]
            if resolve:
                # The repository's verify command is its own, and it is nearly
                # always pytest; a project whose test dependencies live outside
                # its extras would otherwise fail the green gate on a missing
                # runner rather than on anything about the candidate.
                if any("pytest" in token for token in verify_command):
                    command.append("pytest")
            else:
                # Editable, so the source is read live: only the path this
                # environment points at has to change, and re-resolving the
                # dependency set is what makes per-trial environments too slow
                # to run.
                command.append("--no-deps")
            attempts.append(tuple(command))

        # The install runs under the same environment the verify command will,
        # so a stray ``PYTHONPATH`` cannot feed the user's source tree to a
        # build backend that imports the project to describe it.
        installing = _interpreter_environment(self._venv)
        failure = ""
        for attempt in attempts:
            code, output = _run(
                attempt,
                workspace,
                self._build_timeout if resolve else self._repoint_timeout,
                installing,
            )
            if code == 0:
                self._installed_digest = digest
                return None
            failure = output.strip()[-300:] or "no output"

        # Falling through the extras attempt to the plain one and still failing
        # is reported once, on the plain install: naming the extra would send
        # the user to fix a dependency group that is not the problem.
        return (
            "the workspace could not be installed into this campaign's environment, so the "
            "code under test would have been whatever is already installed rather than the "
            f"trial's own tree: {failure}"
        )


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
                if not hasattr(tarfile, "data_filter"):
                    # Refuse rather than extract unfiltered. Without the data
                    # filter, a crafted member could write outside the
                    # destination; a trial that cannot be isolated must not run
                    # at all. Every supported interpreter (CPython 3.11.4+)
                    # ships the filter, so this is a guard, not a code path.
                    return (
                        "this interpreter has no tar extraction filter, so an "
                        "isolated workspace cannot be created safely",
                        frozenset(),
                    )
                archive.extractall(destination, filter="data")
        except (OSError, tarfile.TarError) as exc:
            return f"workspace could not be created: {exc}", frozenset()
    return None, written


def _was_written(path: str, written: frozenset[str]) -> bool:
    """Did the export cover ``path``, whether it names a file or a directory?"""

    return path in written or any(item.startswith(f"{path.rstrip('/')}/") for item in written)


def _task_commit(task: FitTask) -> str:
    """The commit the task was derived from, or "" if it names none."""

    return task.provenance.removeprefix("commit ").strip()


def _materialize_commit(repo: Path, task: FitTask, destination: Path) -> str | None:
    """Lay down the task's commit, unchanged. Returns an error string or None.

    Nothing is copied out of the user's checkout, so the workspace is the
    pinned commit rather than whatever happens to be lying around in it, and it
    deliberately has no ``.git``. A copied one is either an alias for the user's
    real repository or, in a plain clone, the answer key: the very commit the
    agent is being asked to reimplement.

    Reverting is a separate step because the tree has to be *tested* in between:
    the untouched commit is the only state we know is supposed to pass.
    """

    if not _task_commit(task):
        return "task has no commit to revert"
    if not task.source_paths:
        # Reverting "everything" would revert the tests too, handing the agent
        # a green tree and calling it a pass.
        return "task names no source paths, so there is nothing to revert"

    error, _ = _export_tree(repo, _task_commit(task), destination)
    if error is not None:
        return error
    if absent := tuple(path for path in task.test_paths if not (destination / path).is_file()):
        # Silence here would be the worst kind: the suite could pass simply
        # because the test that judges the task never made it into the tree.
        return f"the workspace is missing the tests that decide this task: {', '.join(absent)}"
    return None


def _revert_source_change(repo: Path, task: FitTask, destination: Path) -> str | None:
    """Undo the task's source change in place. Returns an error string or None.

    The test files are pointedly *not* reverted: they are the specification the
    agent must satisfy.
    """

    error, reverted = _export_tree(
        repo, f"{_task_commit(task)}^", destination, paths=task.source_paths
    )
    if error is not None:
        return error
    if unreverted := tuple(path for path in task.source_paths if not _was_written(path, reverted)):
        return f"the repository's export rules hid {', '.join(unreverted)} from the revert"
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


def _drive_with_deadline(
    driver: AgentDriver, invocation: AgentInvocation, timeout: int
) -> AgentOutcome | None:
    """Run ``driver``, and stop waiting for it after ``timeout`` seconds.

    Returns None when the deadline passed with the driver still running; any
    exception it raised is re-raised here, so the caller's handling of a broken
    driver is unchanged.

    An :data:`AgentDriver` is an arbitrary callable, so this cannot *stop* one
    -- only stop the campaign from waiting on it. That is the guarantee the
    timeout is documented to give, and the weaker one is still worth having: a
    single wedged provider call otherwise blocks every remaining trial forever.
    The abandoned thread is a daemon precisely so it cannot hold the process
    open after the campaign has moved on.
    """

    outcome: list[AgentOutcome] = []
    failure: list[BaseException] = []

    def call() -> None:
        try:
            outcome.append(driver(invocation))
        except BaseException as exc:  # noqa: BLE001 - re-raised below, in the caller's thread
            failure.append(exc)

    worker = threading.Thread(target=call, name="ctx-fit-trial-agent", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return None
    if failure:
        raise failure[0]
    return outcome[0]


def make_live_runner(
    repo_path: str | Path,
    driver: AgentDriver,
    *,
    trial_timeout: int = DEFAULT_TRIAL_TIMEOUT_SECONDS,
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
    environment: CampaignEnvironment | None = None,
) -> Callable[[CandidateConfiguration, FitTask, int], TrialResult]:
    """Build a runner that really executes and really verifies.

    ``environment`` is the campaign's, and passing it is how "one environment
    per campaign" survives a caller that rebuilds the runner between trials --
    which the budgeted path does, once per trial, to cap each trial at the
    authorization that is actually left. Omitting it gives this runner an
    environment of its own, which is per-campaign only if the runner is.
    """

    repo = Path(repo_path)
    campaign_environment = environment if environment is not None else CampaignEnvironment()

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

        # An abandoned driver (see the trial timeout below) may still be writing
        # in here while the tree is being removed. That is a mess to clean up,
        # not a verdict, so it must not become an exception out of a finished
        # trial.
        with tempfile.TemporaryDirectory(
            prefix="ctx-fit-trial-", ignore_cleanup_errors=True
        ) as scratch:
            workspace = Path(scratch) / "repo"
            error = _materialize_commit(repo, task, workspace)
            if error is not None:
                return result("infrastructure-failure", detail=error)

            # Before either half of the gate: both halves have to be measured
            # on the same interpreter, and it has to be one that imports this
            # workspace. Aiming it only at the reverted tree would compare a
            # green run of the user's installed package against a red run of
            # the workspace, which measures the environment change, not the
            # revert.
            trial_environment = campaign_environment.aim_at(workspace, task.verify_command)
            if trial_environment.error is not None:
                # Stated, not silent. Without an environment the revert below
                # may be invisible to the suite, and the trial would be thrown
                # away as "did not start red" -- a task disappearing for a
                # reason the user was never told (FITBUG-016).
                return result("infrastructure-failure", detail=trial_environment.error)
            verify_env = trial_environment.env

            # Green baseline. The task's own commit is a state the repository is
            # supposed to pass in, so if it does not pass here the fault is the
            # workspace's -- no test runner installed, a suite that needs
            # something this machine lacks, an unrelated test already broken.
            # Skipping this check is what let "exit code is non-zero" stand in
            # for "the task starts red": the gate below would wave the trial
            # through, the same non-zero would come back after the agent ran,
            # and every candidate would be recorded as having failed a task
            # nothing here was ever able to judge (FITBUG-018, FITBUG-019).
            code, output = _run(task.verify_command, workspace, verify_timeout, verify_env)
            if code != 0:
                return result(
                    "infrastructure-failure",
                    detail=(
                        "the repository's own tests do not pass at this task's commit before "
                        "anything is changed, so this workspace cannot judge a candidate "
                        # Which environment ran them is half the diagnosis: a
                        # missing test dependency is the campaign's environment
                        # to fix, and blaming the repository for it sends the
                        # user to repair something that is not broken.
                        f"({trial_environment.account}): "
                        f"{output.strip()[-200:] or 'no output'}"
                    ),
                )

            error = _revert_source_change(repo, task, workspace)
            if error is not None:
                return result("infrastructure-failure", detail=error)

            # Red gate. If the reverted tree already passes, the task proves
            # nothing and must not be scored against any candidate.
            code, output = _run(task.verify_command, workspace, verify_timeout, verify_env)
            if code is None:
                # Not red: unknown. `None == 0` is False, so without this the
                # gate reads a verify command that never finished as proof the
                # task starts red, and an agent is hired on the strength of it.
                return result(
                    "infrastructure-failure",
                    detail=(
                        "the reverted tree could not be tested, so redness is unproven: "
                        f"{output.strip()[-200:] or 'no output'}"
                    ),
                )
            if code == 0:
                return result(
                    "infrastructure-failure",
                    detail=(
                        "task did not start red; it cannot distinguish configurations "
                        f"({trial_environment.account})"
                    ),
                )

            # The specification as handed over. The prompt asks the agent not to
            # touch it, but a request is not a control: deleting or emptying the
            # one failing test makes the suite exit zero, which would otherwise
            # be recorded as a verified trial for work nobody did.
            specification = _specification_digests(workspace, task.test_paths)

            try:
                agent = _drive_with_deadline(
                    driver,
                    AgentInvocation(
                        workspace=workspace,
                        task_title=task.title,
                        files_to_change=task.source_paths,
                        verify_command=task.verify_command,
                        capability_ids=candidate.capability_ids,
                        model=candidate.model,
                    ),
                    trial_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - any driver failure is ours, not the candidate's
                return result("infrastructure-failure", detail=f"agent driver failed: {exc}")

            if agent is None:
                # Inconclusive, not failed: the workspace is still being written
                # to, so any verdict read out of it now would be a race. Cost
                # stays None because the driver never reported one -- unknown
                # spend must poison the total rather than flatter it.
                return result(
                    "inconclusive",
                    detail=(
                        f"the agent was still running after {trial_timeout}s, so the trial "
                        "was abandoned and learned nothing about this candidate"
                    ),
                )

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
            code, output = _run(task.verify_command, workspace, verify_timeout, verify_env)
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
    "DEFAULT_ENVIRONMENT_TIMEOUT_SECONDS",
    "DEFAULT_REPOINT_TIMEOUT_SECONDS",
    "DEFAULT_TRIAL_TIMEOUT_SECONDS",
    "DEFAULT_VERIFY_TIMEOUT_SECONDS",
    "INVALID_TESTS_MODIFIED",
    "AgentDriver",
    "AgentInvocation",
    "AgentOutcome",
    "CampaignEnvironment",
    "TrialEnvironment",
    "make_live_runner",
]
