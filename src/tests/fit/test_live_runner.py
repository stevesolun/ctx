"""The three ways a live trial can lie: it can damage the repository it
measures, it can accept a verdict the agent wrote for itself, or it can measure
source code that is not the source code it changed.

All three are tested against real Git, a real (tiny) test command and -- for the
third -- a real ``pip install -e``, rather than mocks, because all three
failures live entirely in the interaction with those tools: a mocked ``git``
cannot reproduce a ``.git`` *file* that points somewhere else, a mocked test
command cannot exit zero because its specification was deleted, and no fake
driver can reproduce an editable install resolving ``import demo`` to a source
tree the trial never touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import TrialResult
from ctx.fit.live_runner import (
    INVALID_TESTS_MODIFIED,
    AgentInvocation,
    AgentOutcome,
    CampaignEnvironment,
    make_live_runner,
)
from ctx.fit.tasks import FitTask

#: The fixture repository's own test command. A script rather than pytest so a
#: trial costs milliseconds and nothing leaks in from the outer pytest run. Its
#: shape is the one that matters: several checks, one verdict, exit code only.
RUN_CHECKS = """\
import pathlib
import subprocess
import sys

failed = [
    str(check)
    for check in sorted(pathlib.Path("tests").glob("check_*.py"))
    if subprocess.run([sys.executable, str(check)]).returncode != 0
]
sys.exit(1 if failed else 0)
"""

_BROKEN_ADD = "def add(a, b):\n    raise NotImplementedError\n"
_WORKING_ADD = "def add(a, b):\n    return a + b\n"

_CHECK_CALC = """\
import sys

sys.path.insert(0, ".")
from src.calc import add

assert add(1, 2) == 3
"""


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), *args), capture_output=True, text=True, check=True
    )
    return completed.stdout


def _repo_with_a_task(
    root: Path,
    *,
    gitattributes: str | None = None,
    scaffold_files: dict[str, str] | None = None,
) -> tuple[Path, str]:
    """A repository whose last commit added a check and the code that satisfies it.

    ``scaffold_files`` land in the *first* commit, so they are part of the tree
    the task's commit exports -- which is where a repository's pre-existing
    problems live.
    """

    repo = root / "origin"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fit@example.com")
    _git(repo, "config", "user.name", "Fit")

    if gitattributes is not None:
        (repo / ".gitattributes").write_text(gitattributes, encoding="utf-8")
    for name, body in (scaffold_files or {}).items():
        (repo / name).write_text(body, encoding="utf-8")
    (repo / "run_checks.py").write_text(RUN_CHECKS, encoding="utf-8")
    (repo / "src" / "calc.py").write_text(_BROKEN_ADD, encoding="utf-8")
    (repo / "tests" / "check_other.py").write_text("assert True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: scaffold")

    (repo / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
    (repo / "tests" / "check_calc.py").write_text(_CHECK_CALC, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: real addition")

    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _task(sha: str, **overrides: object) -> FitTask:
    fields: dict[str, object] = {
        "task_id": "revert-addition",
        "title": "reimplement addition",
        "source": "historical-revert",
        "provenance": f"commit {sha}",
        "source_paths": ("src/calc.py",),
        "test_paths": ("tests/check_calc.py",),
        "verify_command": (sys.executable, "run_checks.py"),
        "starts_red": True,
    }
    fields.update(overrides)
    return FitTask(**fields)  # type: ignore[arg-type]


def _candidate(candidate_id: str = "candidate-a") -> CandidateConfiguration:
    return CandidateConfiguration(
        candidate_id=candidate_id,
        role="baseline",
        capability_ids=(),
        model=None,
        instructions=(),
        selection_reason="fixture",
    )


def _trial(repo: Path, driver: object, task: FitTask) -> TrialResult:
    runner = make_live_runner(repo, driver)  # type: ignore[arg-type]
    return runner(_candidate(), task, 0)


# --- FITBUG-004: the trial must not touch the repository it measures ---------


def _untouchable_state(origin: Path, worktree: Path) -> dict[str, bytes]:
    """Every byte a trial is forbidden to change, read straight off disk.

    Read rather than asked for: ``git status`` may legitimately rewrite the
    index to refresh stat data, so probing through Git would hide exactly the
    write this is looking for.
    """

    admin = origin / ".git" / "worktrees" / worktree.name
    state: dict[str, bytes] = {}
    for label, path in (
        ("origin/HEAD", origin / ".git" / "HEAD"),
        ("origin/index", origin / ".git" / "index"),
        ("origin/packed-refs", origin / ".git" / "packed-refs"),
        ("worktree/HEAD", admin / "HEAD"),
        ("worktree/index", admin / "index"),
    ):
        state[label] = path.read_bytes() if path.is_file() else b"<absent>"
    for ref in sorted((origin / ".git" / "refs").rglob("*")):
        if ref.is_file():
            state[f"ref {ref.relative_to(origin)}"] = ref.read_bytes()
    for item in sorted(worktree.rglob("*")):
        if item.is_file() and ".git" not in item.parts:
            state[f"checkout {item.relative_to(worktree)}"] = item.read_bytes()
    return state


def test_a_trial_run_from_a_linked_worktree_leaves_the_real_repository_untouched(
    tmp_path: Path,
) -> None:
    """FITBUG-004: a worktree's ``.git`` is a *file* aimed at the real repository.

    Copying it into the "isolated" workspace aimed every git command there --
    including this module's own revert -- at the user's index, refs and HEAD.
    """

    origin, sha = _repo_with_a_task(tmp_path)
    worktree = tmp_path / "feature-worktree"
    _git(origin, "worktree", "add", "-q", "-b", "feature", str(worktree))
    assert (worktree / ".git").is_file()  # the precondition the bug needs

    before = _untouchable_state(origin, worktree)
    # An agent that really ran and really did not solve it. Reporting spend
    # matters: a driver that finishes without spending anything is now treated
    # as a harness fault, which would abort the trial before it proved isolation.
    tried_and_failed = lambda invocation: AgentOutcome(  # noqa: E731
        completed=False, input_tokens=3000, output_tokens=400, cost_usd=0.05
    )
    result = _trial(worktree, tried_and_failed, _task(sha))
    after = _untouchable_state(origin, worktree)

    assert after == before, "the trial wrote into the repository it was measuring"
    # The trial must still have been a real trial, not an aborted one.
    assert result.outcome == "failed"


def test_the_workspace_is_the_pinned_commit_not_the_users_checkout(tmp_path: Path) -> None:
    """Uncommitted work must neither reach the workspace nor leave a way back to it."""

    origin, sha = _repo_with_a_task(tmp_path)
    (origin / "UNCOMMITTED.txt").write_text("work in progress\n", encoding="utf-8")

    seen: dict[str, bool] = {}

    def observe(invocation: AgentInvocation) -> AgentOutcome:
        seen["leaked_file"] = (invocation.workspace / "UNCOMMITTED.txt").exists()
        seen["has_git"] = (invocation.workspace / ".git").exists()
        seen["reverted"] = (invocation.workspace / "src" / "calc.py").read_text() == _BROKEN_ADD
        seen["kept_the_spec"] = (invocation.workspace / "tests" / "check_calc.py").is_file()
        return AgentOutcome(completed=False)

    _trial(origin, observe, _task(sha))

    assert seen["leaked_file"] is False
    # No `.git` at all: a copied one is either the isolation bug above or, in a
    # plain repository, the answer key -- the very commit being reimplemented.
    assert seen["has_git"] is False
    assert seen["reverted"] is True
    assert seen["kept_the_spec"] is True


def test_a_task_that_names_no_source_paths_is_refused(tmp_path: Path) -> None:
    """With no paths to revert, "revert" would mean reverting the tests too."""

    origin, sha = _repo_with_a_task(tmp_path)

    result = _trial(
        origin,
        lambda invocation: AgentOutcome(completed=True),
        _task(sha, source_paths=()),
    )

    assert result.outcome == "infrastructure-failure"


def test_a_task_whose_source_did_not_exist_before_the_commit_is_not_scored(
    tmp_path: Path,
) -> None:
    origin, sha = _repo_with_a_task(tmp_path)

    result = _trial(
        origin,
        lambda invocation: AgentOutcome(completed=True),
        _task(sha, source_paths=("src/nowhere.py",)),
    )

    assert result.outcome == "infrastructure-failure"
    assert "src/nowhere.py" in result.detail


def test_a_specification_the_repository_refuses_to_export_is_not_scored(
    tmp_path: Path,
) -> None:
    """``git archive`` honours ``export-ignore`` and still exits zero.

    A repository that keeps its tests out of its own exports would otherwise
    hand the trial a tree with no specification in it -- and a suite that
    passes because the only failing check was never unpacked.
    """

    origin, sha = _repo_with_a_task(tmp_path, gitattributes="tests/check_calc.py export-ignore\n")

    result = _trial(origin, lambda invocation: AgentOutcome(completed=True), _task(sha))

    assert result.outcome == "infrastructure-failure"
    assert "tests/check_calc.py" in result.detail


def test_a_task_that_does_not_start_red_is_not_scored(tmp_path: Path) -> None:
    """The revert must actually break something, or the task proves nothing."""

    origin, sha = _repo_with_a_task(tmp_path)
    invoked: list[str] = []

    def driver(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.task_title)
        return AgentOutcome(completed=True)

    # `run_checks.py` is reverted to its identical self, so nothing breaks.
    result = _trial(origin, driver, _task(sha, source_paths=("run_checks.py",)))

    assert result.outcome == "infrastructure-failure"
    assert "red" in result.detail
    assert invoked == [], "no agent should be paid for a task that proves nothing"


# --- FITBUG-018/019: non-zero is not the same fact as "the task starts red" --


def test_a_repository_that_is_already_failing_cannot_judge_a_candidate(tmp_path: Path) -> None:
    """FITBUG-018: one unrelated broken test made every candidate fail, after spend.

    The suite exits non-zero before the revert and after the agent, so the gate
    waves the trial through and the verdict step blames the candidate for a
    failure that was in the repository all along.
    """

    origin, sha = _repo_with_a_task(
        tmp_path, scaffold_files={"tests/check_broken.py": "raise SystemExit(1)\n"}
    )
    invoked: list[str] = []

    def perfect(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.task_title)
        (invocation.workspace / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
        return AgentOutcome(completed=True, cost_usd=0.42)

    result = _trial(origin, perfect, _task(sha))

    assert result.outcome == "infrastructure-failure", result.detail
    assert result.counts_toward_reliability is False
    assert invoked == [], "no agent should be paid to fix a repository that was already red"
    # A suite can also fail because the environment CTX built for it is missing
    # a test dependency, and that is CTX's to fix rather than the repository's.
    # The two read alike unless the message says which interpreter ran.
    assert "declares no installable Python package" in result.detail


def test_a_workspace_with_no_usable_test_runner_is_not_a_candidate_failure(
    tmp_path: Path,
) -> None:
    """FITBUG-019: "No module named pytest" exits non-zero, which is not redness."""

    origin, sha = _repo_with_a_task(tmp_path)
    invoked: list[str] = []

    def perfect(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.task_title)
        (invocation.workspace / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
        return AgentOutcome(completed=True, cost_usd=0.42)

    result = _trial(
        origin,
        perfect,
        _task(sha, verify_command=(sys.executable, "-m", "no_such_test_runner")),
    )

    assert result.outcome == "infrastructure-failure", result.detail
    assert result.counts_toward_reliability is False
    assert invoked == []


def test_a_red_gate_that_never_finished_is_not_read_as_red(tmp_path: Path) -> None:
    """A verify command that hangs on the reverted tree proves nothing.

    ``_run`` reports the timeout as ``None``, and ``None == 0`` is False, so an
    unfinished gate used to read as "this task starts red" -- hiring and
    billing an agent on the strength of a run that never produced a result.
    """

    hangs_when_reverted = (
        "import pathlib\n"
        "import sys\n"
        "import time\n"
        "\n"
        'if "NotImplementedError" in pathlib.Path("src/calc.py").read_text():\n'
        "    time.sleep(60)\n"
        "sys.exit(0)\n"
    )
    origin, sha = _repo_with_a_task(
        tmp_path, scaffold_files={"slow_checks.py": hangs_when_reverted}
    )
    invoked: list[str] = []

    def driver(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.task_title)
        return AgentOutcome(completed=True, cost_usd=0.42)

    runner = make_live_runner(
        origin,
        driver,
        verify_timeout=2,
    )
    result = runner(_candidate(), _task(sha, verify_command=(sys.executable, "slow_checks.py")), 0)

    assert result.outcome == "infrastructure-failure", result.detail
    assert "redness is unproven" in result.detail
    assert invoked == []


# --- FITBUG-039: the documented trial timeout has to actually bound a trial --


def test_an_agent_that_outruns_the_trial_timeout_is_abandoned(tmp_path: Path) -> None:
    """``trial_timeout`` was accepted, documented, and never applied."""

    origin, sha = _repo_with_a_task(tmp_path)
    released = threading.Event()
    entered = threading.Event()

    def never_returns(invocation: AgentInvocation) -> AgentOutcome:
        entered.set()
        # Bounded so a failing assertion cannot leave a thread wedged on a tree
        # the temporary directory is about to remove.
        released.wait(30)
        return AgentOutcome(completed=True, cost_usd=99.0)

    try:
        runner = make_live_runner(origin, never_returns, trial_timeout=1)
        result = runner(_candidate(), _task(sha), 0)
    finally:
        released.set()

    assert entered.is_set(), "the driver was never called, so nothing was timed out"
    assert result.outcome == "inconclusive", result.detail
    assert "still running" in result.detail
    # Unknown spend stays unknown: the driver never reported any.
    assert result.cost_usd is None


# --- FITBUG-005: the specification must survive the trial that depends on it -


def test_an_agent_that_deletes_the_failing_test_is_not_recorded_verified(
    tmp_path: Path,
) -> None:
    """FITBUG-005: the suite exits zero once the only failing check is gone."""

    origin, sha = _repo_with_a_task(tmp_path)

    def cheat(invocation: AgentInvocation) -> AgentOutcome:
        # Implements nothing. Removes the check that would have judged it.
        (invocation.workspace / "tests" / "check_calc.py").unlink()
        return AgentOutcome(completed=True, cost_usd=0.02, input_tokens=100, output_tokens=5)

    result = _trial(origin, cheat, _task(sha))

    assert result.outcome != "verified"
    assert result.outcome == INVALID_TESTS_MODIFIED
    assert "tests/check_calc.py" in result.detail
    # Void, not lost: the trial neither credits nor blames the candidate...
    assert result.counts_toward_reliability is False
    # ...but the money it spent is still charged to it.
    assert result.cost_usd == 0.02


def test_an_agent_that_neuters_the_failing_test_is_not_recorded_verified(
    tmp_path: Path,
) -> None:
    origin, sha = _repo_with_a_task(tmp_path)

    def cheat(invocation: AgentInvocation) -> AgentOutcome:
        (invocation.workspace / "tests" / "check_calc.py").write_text(
            "# nothing to see here\n", encoding="utf-8"
        )
        return AgentOutcome(completed=True, cost_usd=0.02)

    result = _trial(origin, cheat, _task(sha))

    assert result.outcome == INVALID_TESTS_MODIFIED


def test_a_task_with_no_test_files_cannot_be_scored(tmp_path: Path) -> None:
    """Nothing to protect means nothing to trust: the verdict is unguardable."""

    origin, sha = _repo_with_a_task(tmp_path)

    result = _trial(
        origin,
        lambda invocation: AgentOutcome(completed=True),
        _task(sha, test_paths=()),
    )

    assert result.outcome == "infrastructure-failure"


def test_an_agent_that_actually_does_the_work_is_still_recorded_verified(
    tmp_path: Path,
) -> None:
    """The guard must not turn honest passes into void trials."""

    origin, sha = _repo_with_a_task(tmp_path)

    def honest(invocation: AgentInvocation) -> AgentOutcome:
        (invocation.workspace / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
        return AgentOutcome(completed=True, cost_usd=0.11)

    result = _trial(origin, honest, _task(sha))

    assert result.outcome == "verified"
    assert result.counts_toward_reliability is True
    assert result.cost_usd == 0.11


def test_an_agent_that_fails_honestly_is_recorded_failed(tmp_path: Path) -> None:
    origin, sha = _repo_with_a_task(tmp_path)

    def wrong(invocation: AgentInvocation) -> AgentOutcome:
        (invocation.workspace / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a * b\n", encoding="utf-8"
        )
        return AgentOutcome(completed=True, cost_usd=0.09)

    result = _trial(origin, wrong, _task(sha))

    assert result.outcome == "failed"


def test_a_driver_that_never_reached_a_model_is_not_blamed_on_the_candidate(
    tmp_path: Path,
) -> None:
    """A harness fault must not be recorded as evidence against a configuration.

    This is the shape of the defect that made a broken driver argv report "no
    candidate works on your repository" after running zero agents: the agent
    never ran, the tests were simply still red, and the candidate wore it.
    """

    origin, sha = _repo_with_a_task(tmp_path)

    def never_ran(invocation: AgentInvocation) -> AgentOutcome:
        return AgentOutcome(completed=False, detail="harness exited 2 before contacting a model")

    result = _trial(origin, never_ran, _task(sha))

    assert result.outcome == "infrastructure-failure", result.detail
    assert "harness exited 2" in result.detail


def test_an_agent_that_burned_tokens_without_finishing_is_a_real_failure(
    tmp_path: Path,
) -> None:
    """The converse: spend proves a model ran, so not finishing is the candidate's."""

    origin, sha = _repo_with_a_task(tmp_path)

    def gave_up(invocation: AgentInvocation) -> AgentOutcome:
        return AgentOutcome(
            completed=False,
            input_tokens=4000,
            output_tokens=900,
            cost_usd=0.07,
            detail="max iterations reached",
        )

    result = _trial(origin, gave_up, _task(sha))

    assert result.outcome == "failed", result.detail
    assert result.cost_usd == 0.07


# --- FITBUG-016: the workspace has to be the code that actually runs ---------

#: Enough packaging for a repository to *claim* to be installable. The trials
#: that use it never get as far as installing anything -- the environment they
#: need cannot be built -- but without this the runner would correctly conclude
#: there is nothing to install and never try.
_INSTALLABLE_PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "fixture-under-test"
version = "0.1.0"

[tool.setuptools]
package-dir = {"" = "src"}
py-modules = ["calc"]
"""


def test_a_trial_that_cannot_build_an_environment_refuses_and_says_why(
    tmp_path: Path,
) -> None:
    """The fallback is not optional: a trial that cannot isolate must refuse.

    Without an environment of its own the trial may be scoring an installed
    copy of the user's source instead of its own workspace, so it has to stop
    and say so -- silently continuing is how the reverted source stopped
    mattering in the first place.
    """

    origin, sha = _repo_with_a_task(
        tmp_path, scaffold_files={"pyproject.toml": _INSTALLABLE_PYPROJECT}
    )
    invoked: list[str] = []

    def driver(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.task_title)
        return AgentOutcome(completed=True, cost_usd=0.5)

    runner = make_live_runner(
        origin,
        driver,
        environment=CampaignEnvironment(base_python=tmp_path / "no-such-interpreter"),
    )
    result = runner(_candidate(), _task(sha), 0)

    assert result.outcome == "infrastructure-failure", result.detail
    # The cause, not just the category: the user has to be able to act on it.
    assert "no-such-interpreter" in result.detail
    assert result.counts_toward_reliability is False
    assert invoked == [], "no agent should be paid to work in an environment we do not trust"


def test_building_an_environment_is_bounded_by_a_timeout(tmp_path: Path) -> None:
    """A campaign must not hang while an install thinks about it."""

    origin, sha = _repo_with_a_task(
        tmp_path, scaffold_files={"pyproject.toml": _INSTALLABLE_PYPROJECT}
    )
    invoked: list[str] = []

    def driver(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.task_title)
        return AgentOutcome(completed=True, cost_usd=0.5)

    runner = make_live_runner(origin, driver, environment=CampaignEnvironment(build_timeout=0))
    result = runner(_candidate(), _task(sha), 0)

    assert result.outcome == "infrastructure-failure", result.detail
    assert "timed out" in result.detail
    assert invoked == []


def test_one_environment_serves_every_runner_the_campaign_builds(tmp_path: Path) -> None:
    """The budgeted path builds a *new runner* for every trial, to cap it at the
    authorization that is left. An environment owned by the runner would
    therefore be an environment per trial -- a full dependency resolve each
    time, which is what makes the honest fix unaffordable.

    A base interpreter that records being asked and then refuses stands in for
    the install: it makes "how many times was an environment built" observable
    without a package index.
    """

    origin, sha = _repo_with_a_task(
        tmp_path, scaffold_files={"pyproject.toml": _INSTALLABLE_PYPROJECT}
    )
    attempts = tmp_path / "attempts.log"
    recorder = tmp_path / "recording-python"
    recorder.write_text(f'#!/bin/sh\necho attempt >> "{attempts}"\nexit 1\n', encoding="utf-8")
    recorder.chmod(0o755)

    environment = CampaignEnvironment(base_python=recorder)
    results = [
        make_live_runner(
            origin,
            lambda invocation: AgentOutcome(completed=True, cost_usd=0.5),
            environment=environment,
        )(_candidate(), _task(sha), index)
        for index in (0, 1)
    ]

    recorded = attempts.read_text(encoding="utf-8") if attempts.is_file() else ""
    assert recorded.count("attempt") == 1, (
        "the campaign did not build exactly one environment for itself; a runner "
        f"either built its own or was handed someone else's (attempts: {recorded!r})"
    )
    # The second trial is refused just as loudly as the first: a cause stated
    # once and then dropped is the silent discard wearing a different hat.
    assert [result.outcome for result in results] == ["infrastructure-failure"] * 2
    assert results[1].detail == results[0].detail
    assert "recording-python" in results[1].detail


def test_a_discarded_task_says_which_code_was_under_test(tmp_path: Path) -> None:
    """The half of FITBUG-016 the user actually sees: tasks vanishing silently.

    "Did not start red" is the same sentence whether the revert really changed
    nothing or whether the revert was invisible because an installed copy of the
    source was what ran. The two need different fixes, so they cannot read the
    same.
    """

    origin, sha = _repo_with_a_task(tmp_path)

    result = _trial(
        origin,
        lambda invocation: AgentOutcome(completed=True),
        _task(sha, source_paths=("run_checks.py",)),
    )

    assert result.outcome == "infrastructure-failure"
    assert "did not start red" in result.detail
    # This fixture is a repository of loose scripts: nothing declares a package,
    # so nothing could have been installed from it to shadow the workspace.
    assert "declares no installable Python package" in result.detail


# --- FITBUG-016 against the thing itself: a real `pip install -e` ------------
#
# These are marked `integration` because they build virtual environments and
# install into them, which needs a package index the first time. Nothing here
# calls a model or spends money -- the cost is seconds and disk.

_SRC_LAYOUT_PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "demo"
version = "0.1.0"

[tool.setuptools]
package-dir = {"" = "src"}
packages = ["demo"]
"""

_DEMO_BROKEN = "def add(a, b):\n    raise NotImplementedError\n"
_DEMO_WORKING = "def add(a, b):\n    return a + b\n"
_DEMO_TEST = "from demo.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"

#: The repository's own test command, resolved through PATH exactly as
#: `ctx fit` derives it for a Python project. An absolute interpreter path here
#: would sidestep the whole question this file is asking.
_PYTEST = ("python", "-m", "pytest", "-q")


def _src_layout_repo(root: Path) -> tuple[Path, str]:
    """A src-layout package: the standard Python layout, and the broken case.

    ``import demo`` cannot resolve from the tree alone -- ``src`` is not on
    ``sys.path`` -- so the package has to be installed for the suite to run,
    which is precisely what makes an editable install shadow a workspace.
    """

    repo = root / "origin"
    (repo / "src" / "demo").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "fit@example.com")
    _git(repo, "config", "user.name", "Fit")

    (repo / "pyproject.toml").write_text(_SRC_LAYOUT_PYPROJECT, encoding="utf-8")
    (repo / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "demo" / "calc.py").write_text(_DEMO_BROKEN, encoding="utf-8")
    (repo / "tests" / "test_other.py").write_text(
        "def test_other():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: scaffold")

    (repo / "src" / "demo" / "calc.py").write_text(_DEMO_WORKING, encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(_DEMO_TEST, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: real addition")

    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _venv_with(location: Path, *targets: str) -> Path:
    """A real virtual environment with ``targets`` installed, or skip the test."""

    created = subprocess.run(
        (sys.executable, "-m", "venv", str(location)), capture_output=True, text=True
    )
    if created.returncode != 0:  # pragma: no cover - environment-dependent
        pytest.skip(f"no virtual environment can be built here: {created.stderr[-200:]}")
    installed = subprocess.run(
        (str(location / "bin" / "python"), "-m", "pip", "install", "-q", *targets),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if installed.returncode != 0:  # pragma: no cover - needs an index the first time
        pytest.skip(
            f"the fixture could not be installed (no package index?): {installed.stderr[-300:]}"
        )
    return location


def _as_if_run_from(venv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the user's own environment in charge, the way `ctx fit` finds it.

    A user runs `ctx fit` from inside the environment their project is
    installed into. That is not an exotic setup -- it is the only way the
    repository's `python -m pytest` works at all for a src-layout project.
    """

    monkeypatch.setenv("PATH", os.pathsep.join((str(venv / "bin"), os.environ.get("PATH", ""))))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.delenv("PYTHONPATH", raising=False)


@pytest.mark.integration
def test_a_trial_sees_the_reverted_source_under_a_real_editable_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FITBUG-016: the revert was real on disk and invisible to the suite.

    With the user's editable install in charge, ``import demo`` resolved to the
    user's own ``src`` no matter what the workspace contained. The reverted tree
    passed, the task was thrown away as "did not start red", and CTX Fit could
    not evaluate any src-layout repository -- including this one.
    """

    origin, sha = _src_layout_repo(tmp_path)
    _as_if_run_from(_venv_with(tmp_path / "userenv", "-e", str(origin), "pytest"), monkeypatch)

    invoked: list[Path] = []

    def implements_it(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.workspace)
        (invocation.workspace / "src" / "demo" / "calc.py").write_text(
            _DEMO_WORKING, encoding="utf-8"
        )
        return AgentOutcome(completed=True, cost_usd=0.03)

    result = _trial(
        origin,
        implements_it,
        _task(
            sha,
            source_paths=("src/demo/calc.py",),
            test_paths=("tests/test_calc.py",),
            verify_command=_PYTEST,
        ),
    )

    # The red gate is the whole demonstration: it can only have fired if the
    # suite imported the workspace's reverted `calc.py` rather than the
    # install's, which still holds the working implementation.
    assert invoked, f"the task was discarded before any agent ran: {result.detail}"
    assert result.outcome == "verified", result.detail
    # And the user's own tree was neither read as the answer nor written to.
    assert (origin / "src" / "demo" / "calc.py").read_text(encoding="utf-8") == _DEMO_WORKING


@pytest.mark.integration
def test_one_environment_serves_the_whole_campaign_and_follows_each_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The affordability half of the decision, and what it must not cost.

    Per-trial environments would be correct and unusable: every trial in a
    campaign shares one dependency set. So the interpreter is built once and
    only re-aimed -- and re-aiming has to be real, or trials two onward would
    import trial one's deleted workspace.
    """

    origin, _ = _src_layout_repo(tmp_path)
    _as_if_run_from(_venv_with(tmp_path / "userenv", "-e", str(origin), "pytest"), monkeypatch)

    first, second = tmp_path / "trial-1", tmp_path / "trial-2"
    for workspace in (first, second):
        shutil.copytree(origin, workspace, ignore=shutil.ignore_patterns(".git"))
    (second / "src" / "demo" / "calc.py").write_text(_DEMO_BROKEN, encoding="utf-8")

    environment = CampaignEnvironment()
    try:
        assert environment.aim_at(first, _PYTEST).error is None
        venv = environment.venv
        assert venv is not None
        # Two facts that a rebuild would destroy, rather than a stopwatch.
        built_at = (venv / "pyvenv.cfg").stat().st_mtime_ns
        (venv / "built-once.marker").write_text("x", encoding="utf-8")

        aimed = environment.aim_at(second, _PYTEST)

        assert aimed.error is None
        assert environment.venv == venv, "the campaign built a second environment"
        assert (venv / "built-once.marker").is_file(), "the environment was rebuilt"
        assert (venv / "pyvenv.cfg").stat().st_mtime_ns == built_at
        # Re-aimed, not merely reused: the second workspace is what imports now.
        assert aimed.env is not None
        seen = subprocess.run(
            ("python", "-c", "import demo.calc as calc; print(calc.__file__)"),
            cwd=str(second),
            env=aimed.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        resolved = Path(seen.stdout.strip())
        assert resolved.resolve().is_relative_to(second.resolve()), seen.stderr[-300:]
        # ...and it is the second workspace's *content*, not a stale copy.
        assert resolved.read_text(encoding="utf-8") == _DEMO_BROKEN
    finally:
        environment.close()

    # A whole dependency set must not outlive the campaign that needed it.
    assert not venv.exists()
