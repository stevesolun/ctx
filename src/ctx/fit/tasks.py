"""Deriving representative tasks from a repository.

A benchmark built on artificial tasks measures nothing about a real codebase,
so CTX Fit derives its tasks from the repository itself. This is the hardest
honest problem in the product: a task that is trivial, ambiguous, or already
solved yields a confident and meaningless verdict.

The V1 source is **revert-and-reimplement**. Take a commit that changed both
source and tests, revert only the source change, and keep the test. The task is
then "make these tests pass again". This source is chosen because it is the only
one that satisfies every validity requirement at once:

- it **starts red by construction** — the retained test exercises code that was
  just removed, so a task that does not start red is detectably invalid;
- verification is **the repository's own test**, not a judgement;
- the work is **real** — it was actually done by a human in this codebase;
- provenance is **exact** — a commit SHA, not a guess.

Its weakness is equally clear and is recorded rather than hidden: the solution
exists in the repository's own history, and may exist in a model's training
data. Every derived task therefore carries a contamination note, and the
report must never present a historical task as if it were unseen work.

Nothing here executes a model. Deriving tasks reads Git history only.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

TASK_SCHEMA = "ctx.fit.task-v1"

TaskSource = Literal["historical-revert", "user-specified", "generated"]

#: Why each source can or cannot be trusted, surfaced in the report so a reader
#: can discount a result appropriately.
SOURCE_CAVEAT: dict[TaskSource, str] = {
    "historical-revert": (
        "the original solution exists in this repository's history and may exist in "
        "model training data; treat as an upper bound on real-world performance"
    ),
    "user-specified": "supplied by the user; CTX cannot vouch for its difficulty",
    "generated": "synthesized by CTX; not work anyone actually needed done",
}

_MAX_FILES_IN_COMMIT = 6
_GIT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class FitTask:
    """One unit of real work a candidate configuration will be asked to do."""

    task_id: str
    title: str
    source: TaskSource
    provenance: str
    """Exactly where this task came from, e.g. a commit SHA."""
    source_paths: tuple[str, ...]
    """Files the task must change. Reverted before the trial begins."""
    test_paths: tuple[str, ...]
    """Files that prove the task was done. Never reverted, never shown as a hint."""
    verify_command: tuple[str, ...]
    starts_red: bool | None = None
    """None until verified. A task that does not start red is not a valid task."""

    @property
    def caveat(self) -> str:
        return SOURCE_CAVEAT[self.source]

    @property
    def is_valid(self) -> bool:
        """Only a task proven to start red may be used in an experiment."""

        return self.starts_red is True

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": TASK_SCHEMA,
            "task_id": self.task_id,
            "title": self.title,
            "source": self.source,
            "provenance": self.provenance,
            "source_paths": list(self.source_paths),
            "test_paths": list(self.test_paths),
            "verify_command": list(self.verify_command),
            "starts_red": self.starts_red,
            "is_valid": self.is_valid,
            "caveat": self.caveat,
        }


@dataclass(frozen=True, slots=True)
class TaskSet:
    tasks: tuple[FitTask, ...] = ()
    considered: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def valid(self) -> tuple[FitTask, ...]:
        return tuple(task for task in self.tasks if task.is_valid)

    def to_dict(self) -> dict[str, object]:
        return {
            "tasks": [task.to_dict() for task in self.tasks],
            "considered": self.considered,
            "valid_count": len(self.valid),
            "warnings": list(self.warnings),
        }


def _git(repo: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(repo), *args),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _has_parent(repo: Path, sha: str) -> bool:
    """Can ``<sha>^`` be resolved at all?

    A revert-and-reimplement task is prepared by laying down the state *before*
    the commit, so the first commit in a history has nothing to revert to and
    the trial dies as an infrastructure failure. Shallow clones make this the
    common case rather than the exotic one.
    """

    return _git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^") is not None


def _paths_in_tree(repo: Path, treeish: str, paths: tuple[str, ...]) -> frozenset[str]:
    """Which of ``paths`` exist in ``treeish``. Empty if the tree cannot be read.

    ``-z`` because ``ls-tree`` otherwise quotes paths outside ASCII, which would
    silently fail the membership test it exists to answer.
    """

    listing = _git(repo, "ls-tree", "-r", "--name-only", "-z", treeish, "--", *paths)
    if listing is None:
        return frozenset()
    return frozenset(item for item in listing.split("\0") if item)


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return (
        "/test" in lowered
        or lowered.startswith("test")
        or "_test." in lowered
        or ".test." in lowered
        or ".spec." in lowered
    )


def derive_tasks(
    repo_path: str | Path,
    *,
    verify_command: tuple[str, ...],
    limit: int = 5,
    scan_commits: int = 60,
) -> TaskSet:
    """Derive candidate tasks from recent history. Reads Git only.

    Tasks are returned with ``starts_red=None``: this function proposes tasks,
    it does not validate them. Validation requires actually running the test
    against the reverted tree, which is a separate, explicitly requested step.
    """

    repo = Path(repo_path)
    warnings: list[str] = []

    if not (repo / ".git").exists():
        return TaskSet(
            warnings=(
                "no Git history: revert-and-reimplement tasks cannot be derived, and "
                "CTX Fit will not invent synthetic work in its place",
            )
        )

    log = _git(repo, "log", "--format=%H", f"-{scan_commits}")
    if not log:
        return TaskSet(warnings=("git history could not be read",))

    tasks: list[FitTask] = []
    considered = 0
    non_python = 0
    unpreparable = 0

    for sha in log.split():
        if len(tasks) >= limit:
            break
        listing = _git(repo, "show", "--name-only", "--format=", sha)
        if not listing:
            continue
        files = [line.strip() for line in listing.splitlines() if line.strip()]
        if not files or len(files) > _MAX_FILES_IN_COMMIT:
            # Large commits make ambiguous tasks: too many ways to "solve" them.
            continue

        python_files = [item for item in files if item.endswith(".py")]
        if not python_files:
            non_python += 1
        source_paths = tuple(item for item in python_files if not _is_test_path(item))
        test_paths = tuple(item for item in python_files if _is_test_path(item))
        if not source_paths or not test_paths:
            continue

        considered += 1

        # A task that cannot be *prepared* is worse than no task. It is counted
        # into task_count, priced into the plan the user approves, and then
        # fails as infrastructure on every candidate — which blanks the whole
        # campaign's cost and forces a permanent no-verdict. Preparation is
        # "export <sha>, then export <sha>^ over the source paths", so both
        # trees have to actually contain what the task names.
        if not _has_parent(repo, sha):
            unpreparable += 1
            continue
        before = _paths_in_tree(repo, f"{sha}^", source_paths)
        if any(path not in before for path in source_paths):
            # The commit added this source file. Reverting restores files from
            # the parent; it cannot delete one the parent never had, so the
            # implementation would still be sitting there for the agent to find.
            unpreparable += 1
            continue
        if any(path not in _paths_in_tree(repo, sha, test_paths) for path in test_paths):
            # The commit deleted the test, so the file that decides this task
            # is not in the tree the trial would run against.
            unpreparable += 1
            continue

        subject = _git(repo, "log", "-1", "--format=%s", sha) or ""
        tasks.append(
            FitTask(
                task_id=f"revert-{sha[:12]}",
                title=subject.strip()[:100] or f"reimplement {source_paths[0]}",
                source="historical-revert",
                provenance=f"commit {sha}",
                source_paths=source_paths,
                test_paths=test_paths,
                verify_command=verify_command,
            )
        )

    if unpreparable:
        warnings.append(
            f"skipped {unpreparable} otherwise-suitable commit(s) that cannot be "
            "reverted: they have no parent commit, or they added the source file "
            "the task would have to remove"
        )
    if not tasks:
        if non_python and not considered:
            # Task derivation reads Python only, while `is_fit_evaluable` does
            # not — so a Jest repository is told it can be evaluated and then
            # refused. Naming the restriction is the least this can do.
            warnings.append(
                "task derivation reads Python source only, and none of the "
                f"{non_python} recent commit(s) examined changed a .py file"
            )
        warnings.append(
            "no commit in recent history changed both source and tests in a small "
            "enough diff to make an unambiguous task"
        )
    return TaskSet(tasks=tuple(tasks), considered=considered, warnings=tuple(warnings))


__all__ = [
    "SOURCE_CAVEAT",
    "TASK_SCHEMA",
    "FitTask",
    "TaskSet",
    "TaskSource",
    "derive_tasks",
]
