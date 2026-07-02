from __future__ import annotations

from pathlib import Path

from scripts.ci_preflight import _run_no_test_policy_for_files
from scripts.ci_preflight import select_checks


def _names_for(files: list[str], *, profile: str = "pr") -> list[str]:
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=files,
        profile=profile,
        python="python",
    )
    return [check.name for check in checks]


def test_preflight_runs_docs_gate_for_docs_changes() -> None:
    names = _names_for(["docs/index.md"])

    assert "repo stats" in names
    assert "docs strict build" in names
    assert "no-test policy" not in names
    assert "unit-linux equivalent" not in names


def test_preflight_runs_source_gates_for_source_changes() -> None:
    names = _names_for(["src/ctx/adapters/generic/loop.py"])

    assert "no-test policy" in names
    assert "ruff format" in names
    assert "ruff" in names
    assert "mypy" in names
    assert "unit-linux equivalent" in names
    assert "A-Z canary" in names
    assert "clean host contract" in names


def test_preflight_runs_graph_validation_for_graph_artifacts() -> None:
    names = _names_for(["graph/wiki-graph.tar.gz"])

    assert "graph artifact validation" in names
    assert "no-test policy" not in names
    assert "unit-linux equivalent" not in names


def test_preflight_no_test_policy_invocation_uses_current_dirty_file_set() -> None:
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=["scripts/ci_preflight.py"],
        profile="pr",
        python="python",
    )

    policy = next(check for check in checks if check.name == "no-test policy")
    assert policy.argv[0] == "python"
    assert Path(policy.argv[1]).name == "ci_preflight.py"
    assert policy.argv[2:] == ("--base", "origin/main", "--internal-no-test-policy")


def test_internal_no_test_policy_fails_dirty_contract_without_tests() -> None:
    assert _run_no_test_policy_for_files(["src/ctx/adapters/generic/loop.py"]) == 1


def test_internal_no_test_policy_accepts_dirty_contract_with_tests() -> None:
    assert (
        _run_no_test_policy_for_files(
            [
                "src/ctx/adapters/generic/loop.py",
                "src/tests/test_harness_loop.py",
            ]
        )
        == 0
    )


def test_preflight_pr_profile_runs_package_build_for_source_prs() -> None:
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=["pyproject.toml"],
        profile="pr",
        python="python",
    )

    assert "build wheel" in [check.name for check in checks]
    assert "twine check" in [check.name for check in checks]


def test_preflight_full_profile_forces_source_gates_for_docs_changes() -> None:
    names = _names_for(["docs/index.md"], profile="full")

    assert "unit-linux equivalent" in names
    assert "build wheel" in names


def test_preflight_runs_browser_and_similarity_when_classified() -> None:
    names = _names_for([".github/workflows/test.yml"])

    assert "browser monitor security" in names
    assert "similarity precision/recall" in names
