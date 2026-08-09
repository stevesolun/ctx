from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ctx.fit.tasks import SOURCE_CAVEAT, TASK_SCHEMA, FitTask, derive_tasks

VERIFY = ("python", "-m", "pytest", "-q")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    )


def _repo_with_history(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git_init(repo)

    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat: add addition helper")
    return repo


def _git_init(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def test_derives_a_task_from_a_paired_source_and_test_commit(tmp_path: Path) -> None:
    result = derive_tasks(_repo_with_history(tmp_path), verify_command=VERIFY)

    assert len(result.tasks) == 1
    task = result.tasks[0]
    assert task.source == "historical-revert"
    assert task.source_paths == ("src/calc.py",)
    assert task.test_paths == ("tests/test_calc.py",)
    assert task.title.startswith("feat: add addition helper")


def test_provenance_is_an_exact_commit(tmp_path: Path) -> None:
    task = derive_tasks(_repo_with_history(tmp_path), verify_command=VERIFY).tasks[0]

    assert task.provenance.startswith("commit ")
    sha = task.provenance.removeprefix("commit ")
    assert len(sha) == 40  # a real object id, not a description


def test_a_task_is_invalid_until_proven_to_start_red(tmp_path: Path) -> None:
    """Proposing a task is not validating it.

    A task that already passes measures nothing, so validity requires actually
    observing the red state rather than assuming it.
    """

    task = derive_tasks(_repo_with_history(tmp_path), verify_command=VERIFY).tasks[0]

    assert task.starts_red is None
    assert task.is_valid is False


def test_every_task_carries_a_contamination_caveat(tmp_path: Path) -> None:
    task = derive_tasks(_repo_with_history(tmp_path), verify_command=VERIFY).tasks[0]

    assert task.caveat
    assert "training data" in task.caveat
    assert set(SOURCE_CAVEAT) == {"historical-revert", "user-specified", "generated"}


def test_commits_touching_only_source_or_only_tests_are_skipped(tmp_path: Path) -> None:
    repo = _repo_with_history(tmp_path)
    (repo / "src" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: source only")

    result = derive_tasks(repo, verify_command=VERIFY)

    assert [task.title for task in result.tasks] == ["feat: add addition helper"]


def test_large_commits_are_skipped_as_ambiguous(tmp_path: Path) -> None:
    repo = _repo_with_history(tmp_path)
    for index in range(8):
        (repo / "src" / f"mod{index}.py").write_text(f"X = {index}\n", encoding="utf-8")
    (repo / "tests" / "test_many.py").write_text("def test_many():\n    assert True\n", "utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feat: sprawling change")

    result = derive_tasks(repo, verify_command=VERIFY)

    assert "sprawling" not in " ".join(task.title for task in result.tasks)


def test_repository_without_git_refuses_rather_than_inventing_tasks(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    result = derive_tasks(plain, verify_command=VERIFY)

    assert result.tasks == ()
    assert any("will not invent synthetic work" in warning for warning in result.warnings)


def test_history_without_a_usable_commit_warns(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("# hi\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs: readme")

    result = derive_tasks(repo, verify_command=VERIFY)

    assert result.tasks == ()
    assert any("no commit in recent history" in warning for warning in result.warnings)


def test_limit_is_respected(tmp_path: Path) -> None:
    repo = _repo_with_history(tmp_path)
    for index in range(4):
        (repo / "src" / f"m{index}.py").write_text(f"X = {index}\n", encoding="utf-8")
        (repo / "tests" / f"test_m{index}.py").write_text(
            "def test_x():\n    assert True\n", "utf-8"
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", f"feat: module {index}")

    result = derive_tasks(repo, verify_command=VERIFY, limit=2)

    assert len(result.tasks) == 2


def test_task_is_serializable_and_versioned(tmp_path: Path) -> None:
    payload = derive_tasks(_repo_with_history(tmp_path), verify_command=VERIFY).to_dict()

    encoded = json.loads(json.dumps(payload, sort_keys=True))
    assert encoded["tasks"][0]["schema"] == TASK_SCHEMA
    assert encoded["valid_count"] == 0  # nothing validated yet


def test_user_specified_task_carries_its_own_caveat() -> None:
    task = FitTask(
        task_id="user-1",
        title="fix the flaky import",
        source="user-specified",
        provenance="supplied on the command line",
        source_paths=("src/app.py",),
        test_paths=("tests/test_app.py",),
        verify_command=VERIFY,
    )

    assert "cannot vouch" in task.caveat
