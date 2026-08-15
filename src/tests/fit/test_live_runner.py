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
from collections.abc import Mapping
from pathlib import Path

import pytest

from ctx.fit.candidates import CandidateConfiguration
from ctx.fit.execution import TrialResult
from ctx.fit.live_runner import (
    DEFAULT_TRIAL_TIMEOUT_SECONDS,
    DEFAULT_VERIFY_TIMEOUT_SECONDS,
    INVALID_TESTS_MODIFIED,
    AgentDriver,
    AgentInvocation,
    AgentOutcome,
    CampaignEnvironment,
    _executable_read_access,
    _run,
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


def _trial(
    repo: Path,
    driver: AgentDriver,
    task: FitTask,
    *,
    trial_timeout: int = DEFAULT_TRIAL_TIMEOUT_SECONDS,
    verify_timeout: int = DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> TrialResult:
    # Most tests in this file exercise trial semantics rather than the external
    # OS boundary. Keep that boundary injectable so the security-specific
    # regression below can exercise the production default without making the
    # entire unit suite depend on a host sandbox executable.
    runner = make_live_runner(
        repo,
        driver,
        trial_timeout=trial_timeout,
        verify_timeout=verify_timeout,
        environment=CampaignEnvironment(repository_executor=_run),
    )
    return runner(_candidate(), task, 0)


def test_repository_verification_can_write_only_inside_its_trial_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One trial must not poison the campaign environment or a later trial."""

    environment = CampaignEnvironment()
    workspace = environment.sandbox_root / "trial-a" / "repo"
    workspace.mkdir(parents=True)
    observed_roots: list[Path] = []

    def record_boundary(command, **kwargs):
        observed_roots.append(kwargs["writable_root"])
        return tuple(command)

    monkeypatch.setattr("ctx.fit.live_runner.sandboxed_command", record_boundary)
    monkeypatch.setattr("ctx.fit.live_runner._run", lambda *_args, **_kwargs: (0, ""))

    try:
        code, _ = environment.run_repository(
            (sys.executable, "-c", "pass"),
            workspace,
            30,
            {"PATH": os.environ.get("PATH", os.defpath)},
        )
    finally:
        environment.close()

    assert code == 0
    assert observed_roots == [workspace]


def test_executable_read_roots_include_every_symlink_hop(tmp_path: Path) -> None:
    cellar = tmp_path / "Cellar" / "tool" / "1.0"
    (cellar / "bin").mkdir(parents=True)
    executable = cellar / "bin" / "tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    opt = tmp_path / "opt" / "tool"
    opt.parent.mkdir()
    opt.symlink_to(cellar, target_is_directory=True)
    shim_directory = tmp_path / "home" / ".local" / "bin"
    shim_directory.mkdir(parents=True)
    (shim_directory / "tool").symlink_to(opt / "bin" / "tool")

    roots, paths = _executable_read_access(("tool",), {"PATH": str(shim_directory)})

    assert shim_directory / "tool" in paths
    assert opt / "bin" / "tool" in paths
    assert executable in paths
    assert shim_directory.parent not in roots
    assert opt not in roots

    if sys.platform == "darwin":
        environment = CampaignEnvironment()
        workspace = environment.sandbox_root / "workspace"
        workspace.mkdir()
        try:
            code, output = environment.run_repository(
                ("tool",), workspace, 30, {"PATH": str(shim_directory)}
            )
        finally:
            environment.close()
        assert code == 0, output


def test_path_shim_does_not_expose_ambient_siblings(tmp_path: Path) -> None:
    local = tmp_path / "home" / ".local"
    shim_directory = local / "bin"
    shim_directory.mkdir(parents=True)
    shim = shim_directory / "python"
    shim.symlink_to(sys.executable)
    secret = local / "ambient-secret.txt"
    secret.write_text("MUST_NOT_CROSS", encoding="utf-8")

    environment = CampaignEnvironment()
    workspace = environment.sandbox_root / "workspace"
    workspace.mkdir()
    try:
        code, output = environment.run_repository(
            (
                str(shim),
                "-c",
                f"from pathlib import Path; print(Path({str(secret)!r}).read_text())",
            ),
            workspace,
            30,
            {"PATH": str(shim_directory)},
        )
    finally:
        environment.close()

    assert code != 0
    assert "MUST_NOT_CROSS" not in output


@pytest.mark.parametrize(
    "command",
    (
        ("python", "-m", "pytest", "-q"),
        ("npm", "run", "test"),
        ("go", "test", "./..."),
        ("cargo", "test"),
        ("make", "test"),
    ),
    ids=("python", "javascript", "go", "rust", "make"),
)
def test_repository_executor_receives_every_language_command_exactly(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    observed: list[tuple[str, ...]] = []

    def record(
        selected: tuple[str, ...],
        _cwd: Path,
        _timeout: int,
        _env: Mapping[str, str] | None,
    ) -> tuple[int, str]:
        observed.append(selected)
        return 0, ""

    environment = CampaignEnvironment(repository_executor=record)
    workspace = environment.sandbox_root / "trial-a" / "repo"
    workspace.mkdir(parents=True)
    try:
        code, _ = environment.run_repository(
            command,
            workspace,
            30,
            {"PATH": os.environ.get("PATH", os.defpath)},
        )
    finally:
        environment.close()

    assert code == 0
    assert observed == [command]


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


def test_a_red_gate_that_never_finished_is_not_read_as_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr("ctx.fit.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("ctx.fit.sandbox.shutil.which", lambda name, path=None: None)
    origin, sha = _repo_with_a_task(
        tmp_path, scaffold_files={"slow_checks.py": hangs_when_reverted}
    )
    invoked: list[str] = []

    def driver(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.task_title)
        return AgentOutcome(completed=True, cost_usd=0.42)

    result = _trial(
        origin,
        driver,
        _task(sha, verify_command=(sys.executable, "slow_checks.py")),
        verify_timeout=2,
    )

    assert result.outcome == "infrastructure-failure", result.detail
    assert "redness is unproven" in result.detail
    assert invoked == []


# --- FITBUG-039: the documented trial timeout has to actually bound a trial --


def test_an_agent_that_outruns_the_trial_timeout_is_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``trial_timeout`` was accepted, documented, and never applied."""

    monkeypatch.setattr("ctx.fit.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("ctx.fit.sandbox.shutil.which", lambda name, path=None: None)
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
        result = _trial(origin, never_returns, _task(sha), trial_timeout=1)
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


def test_an_agent_that_rewrites_the_verification_runner_cannot_earn_a_pass(
    tmp_path: Path,
) -> None:
    """The judge is larger than the one test path named by task history."""

    origin, sha = _repo_with_a_task(tmp_path)

    def rewrite_the_judge(invocation: AgentInvocation) -> AgentOutcome:
        # The source remains broken; only the repository-level judge is made to
        # say yes. Hashing task.test_paths alone did not notice this.
        (invocation.workspace / "run_checks.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8"
        )
        return AgentOutcome(completed=True, cost_usd=0.02)

    result = _trial(origin, rewrite_the_judge, _task(sha))

    assert result.outcome == INVALID_TESTS_MODIFIED
    assert "run_checks.py" in result.detail


def test_non_python_repository_verifier_is_run_unchanged(tmp_path: Path) -> None:
    """Repository-native verification is language-neutral, not Python-wrapped."""

    shell_check = "#!/bin/sh\ngrep -q 'return a + b' src/calc.py\n"
    origin, sha = _repo_with_a_task(
        tmp_path,
        scaffold_files={"check.sh": shell_check},
    )
    (origin / "check.sh").chmod(0o755)
    _git(origin, "add", "check.sh")
    _git(origin, "commit", "-q", "--amend", "--no-edit")
    sha = _git(origin, "rev-parse", "HEAD").strip()
    invoked: list[tuple[str, ...]] = []

    def driver(invocation: AgentInvocation) -> AgentOutcome:
        invoked.append(invocation.verify_command)
        (invocation.workspace / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
        return AgentOutcome(completed=True, cost_usd=0.02)

    result = _trial(
        origin,
        driver,
        _task(sha, verify_command=("./check.sh",)),
    )

    assert result.outcome == "verified", result.detail
    assert result.counts_toward_reliability is True
    assert invoked == [("./check.sh",)]


def test_an_agent_that_writes_outside_the_tasks_editable_paths_is_void(
    tmp_path: Path,
) -> None:
    """Only the task's explicit source paths are candidate-owned output."""

    origin, sha = _repo_with_a_task(tmp_path)

    def expand_scope(invocation: AgentInvocation) -> AgentOutcome:
        (invocation.workspace / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
        (invocation.workspace / "surprise.txt").write_text("not requested\n", encoding="utf-8")
        return AgentOutcome(completed=True, cost_usd=0.03)

    result = _trial(origin, expand_scope, _task(sha))

    assert result.outcome == INVALID_TESTS_MODIFIED
    assert "surprise.txt" in result.detail


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


def test_a_trial_stopped_by_the_per_trial_budget_is_inconclusive(
    tmp_path: Path,
) -> None:
    """CTX's own dollar ceiling cannot count as evidence against a candidate."""

    origin, sha = _repo_with_a_task(tmp_path)

    def budget_stopped(invocation: AgentInvocation) -> AgentOutcome:
        return AgentOutcome(
            completed=True,
            input_tokens=12_000,
            output_tokens=2_500,
            cost_usd=2.0,
            detail="cost_budget",
        )

    result = _trial(origin, budget_stopped, _task(sha))

    assert result.outcome == "inconclusive", result.detail
    assert result.counts_toward_reliability is False
    assert result.cost_usd == 2.0
    assert "cost_budget" in result.detail


def test_a_budget_capped_trial_is_inconclusive_even_when_the_tree_now_passes(
    tmp_path: Path,
) -> None:
    """A CTX cap is experiment truncation, not verified completion."""

    origin, sha = _repo_with_a_task(tmp_path)

    def capped_after_edit(invocation: AgentInvocation) -> AgentOutcome:
        (invocation.workspace / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
        return AgentOutcome(
            completed=True,
            input_tokens=12_000,
            output_tokens=2_500,
            cost_usd=2.0,
            stop_reason="cost_budget",
            logs="bounded provider log",
        )

    result = _trial(origin, capped_after_edit, _task(sha))

    assert result.outcome == "inconclusive", result.detail
    assert result.counts_toward_reliability is False
    assert "cost_budget" in result.detail
    assert "bounded provider log" in result.detail


def test_a_void_trial_still_retains_structured_stop_reason_and_logs(tmp_path: Path) -> None:
    """Attribution evidence must survive early result paths (ADR-015)."""

    origin, sha = _repo_with_a_task(tmp_path)

    def capped_and_tampered(invocation: AgentInvocation) -> AgentOutcome:
        (invocation.workspace / "tests" / "check_calc.py").write_text(
            "# removed specification\n", encoding="utf-8"
        )
        return AgentOutcome(
            completed=True,
            cost_usd=2.0,
            stop_reason="cost_budget",
            logs="bounded provider log",
        )

    result = _trial(origin, capped_and_tampered, _task(sha))

    assert result.outcome == INVALID_TESTS_MODIFIED
    assert "cost_budget" in result.detail
    assert "bounded provider log" in result.detail


def test_a_changed_source_cannot_become_a_symlink_to_an_outside_answer(tmp_path: Path) -> None:
    """Editable scope is not permission to redirect verification off-workspace."""

    origin, sha = _repo_with_a_task(tmp_path)

    def link_to_answer(invocation: AgentInvocation) -> AgentOutcome:
        source = invocation.workspace / "src" / "calc.py"
        source.unlink()
        source.symlink_to(origin / "src" / "calc.py")
        return AgentOutcome(completed=True, cost_usd=0.01)

    result = _trial(origin, link_to_answer, _task(sha))

    assert result.outcome == INVALID_TESTS_MODIFIED
    assert "src/calc.py" in result.detail
    assert "symlink" in result.detail


def test_repository_commands_do_not_inherit_ambient_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malicious test must not receive the launching user's secrets."""

    monkeypatch.setenv("CTX_FIT_REVIEW_SECRET", "must-not-cross")
    origin, sha = _repo_with_a_task(
        tmp_path,
        scaffold_files={
            "tests/check_environment.py": (
                "import os\nassert 'CTX_FIT_REVIEW_SECRET' not in os.environ\n"
            )
        },
    )

    def honest(invocation: AgentInvocation) -> AgentOutcome:
        (invocation.workspace / "src" / "calc.py").write_text(_WORKING_ADD, encoding="utf-8")
        return AgentOutcome(completed=True, cost_usd=0.01)

    # The injected executor deliberately performs no filesystem isolation; the
    # assertion is specifically that the environment crossing the boundary is
    # scrubbed independently of the OS implementation.
    result = _trial(origin, honest, _task(sha))

    assert result.outcome == "verified", result.detail


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


def test_tool_only_pyproject_does_not_trigger_python_package_install(tmp_path: Path) -> None:
    workspace = tmp_path / "native-repository"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    environment = CampaignEnvironment(base_python=tmp_path / "must-not-run")
    try:
        aimed = environment.aim_at(workspace, ("npm", "test"))
    finally:
        environment.close()

    assert aimed.error is None
    assert aimed.env is not None
    assert "declares no installable Python package" in aimed.account


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


def test_dependency_installation_never_receives_network_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, _ = _src_layout_repo(tmp_path)
    observed: list[tuple[tuple[str, ...], bool, Path]] = []

    def capture_boundary(command, **kwargs):
        observed.append((command, kwargs["network"], kwargs["writable_root"]))
        return (sys.executable, "-c", "raise SystemExit(1)")

    monkeypatch.setattr("ctx.fit.live_runner.sandboxed_command", capture_boundary)
    environment = CampaignEnvironment()
    campaign_root = environment.sandbox_root
    try:
        aimed = environment.aim_at(workspace, _PYTEST)
    finally:
        environment.close()

    assert aimed.error is not None
    assert "does not grant network access" in aimed.error
    assert (
        "dependencies must be available from the repository or local installer state" in aimed.error
    )
    assert observed
    assert all("--no-index" in command for command, _network, _root in observed)
    assert all(network is False for _command, network, _root in observed)
    assert all(root == campaign_root for _command, _network, root in observed)


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
# install into them. The fixture carries its own tiny editable-build backend
# and uses unittest so the production no-network dependency boundary is tested
# without a package index. Nothing here calls a model or spends money.

_SRC_LAYOUT_PYPROJECT = """\
[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]

[project]
name = "demo"
version = "0.1.0"

[tool.setuptools]
package-dir = {"" = "src"}
packages = ["demo"]
"""

_SELF_CONTAINED_EDITABLE_BACKEND = """\
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def get_requires_for_build_editable(config_settings=None):
    return []


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    wheel = Path(wheel_directory) / "demo-0.1.0-py3-none-any.whl"
    dist_info = "demo-0.1.0.dist-info"
    with ZipFile(wheel, "w", ZIP_DEFLATED) as archive:
        archive.writestr("demo.pth", str(Path(__file__).parent / "src"))
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\\nGenerator: ctx-fit-test\\n"
            "Root-Is-Purelib: true\\nTag: py3-none-any\\n",
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\\nName: demo\\nVersion: 0.1.0\\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return wheel.name
"""

_DEMO_BROKEN = "def add(a, b):\n    raise NotImplementedError\n"
_DEMO_WORKING = "def add(a, b):\n    return a + b\n"
_DEMO_TEST = """\
import unittest

from demo.calc import add


class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(1, 2), 3)
"""

#: The repository's own test command, resolved through PATH exactly as
#: `ctx fit` derives it for a Python project. An absolute interpreter path here
#: would sidestep the whole question this file is asking.
_PYTEST = ("python", "-m", "pytest", "-q")
_UNITTEST = ("python", "-m", "unittest", "discover", "-s", "tests", "-q")


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
    (repo / "backend.py").write_text(_SELF_CONTAINED_EDITABLE_BACKEND, encoding="utf-8")
    (repo / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "demo" / "calc.py").write_text(_DEMO_BROKEN, encoding="utf-8")
    (repo / "tests" / "test_other.py").write_text(
        "import unittest\n\n\n"
        "class OtherTest(unittest.TestCase):\n"
        "    def test_other(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
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
    if installed.returncode != 0:  # pragma: no cover - environment-dependent
        pytest.skip(f"the self-contained fixture could not be installed: {installed.stderr[-300:]}")
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
    _as_if_run_from(_venv_with(tmp_path / "userenv", "-e", str(origin)), monkeypatch)

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
            verify_command=_UNITTEST,
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
    _as_if_run_from(_venv_with(tmp_path / "userenv", "-e", str(origin)), monkeypatch)

    environment = CampaignEnvironment()
    try:
        # Production trials always materialize below the campaign's one
        # writable root. Keep this lower-level environment test honest about
        # that security precondition as it verifies reuse and re-aiming.
        first, second = environment.sandbox_root / "trial-1", environment.sandbox_root / "trial-2"
        for workspace in (first, second):
            shutil.copytree(origin, workspace, ignore=shutil.ignore_patterns(".git"))
        (second / "src" / "demo" / "calc.py").write_text(_DEMO_BROKEN, encoding="utf-8")

        assert environment.aim_at(first, _UNITTEST).error is None
        venv = environment.venv
        assert venv is not None
        # Two facts that a rebuild would destroy, rather than a stopwatch.
        built_at = (venv / "pyvenv.cfg").stat().st_mtime_ns
        (venv / "built-once.marker").write_text("x", encoding="utf-8")

        aimed = environment.aim_at(second, _UNITTEST)

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
