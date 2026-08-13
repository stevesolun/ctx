"""The two ways a live trial can lie: it can damage the repository it measures,
or it can accept a verdict the agent wrote for itself.

Both are tested against real Git and a real (tiny) test command rather than
mocks, because both failures live entirely in the interaction with those tools:
a mocked ``git`` cannot reproduce a ``.git`` *file* that points somewhere else,
and a mocked test command cannot exit zero because its specification was
deleted.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import TrialResult
from ctx.fit.live_runner import (
    INVALID_TESTS_MODIFIED,
    AgentInvocation,
    AgentOutcome,
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
