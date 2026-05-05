from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.ci_classifier import classify_paths, main
from scripts.ci_no_test_policy import evaluate_policy, is_release_metadata_only
from scripts.ci_required import REQUIRED_JOBS, failed_required_jobs


def _required_needs(
    **overrides: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    needs: dict[str, dict[str, Any]] = {
        name: {"result": "success"} for name in REQUIRED_JOBS
    }
    needs.update(overrides)
    return needs


def test_docs_only_classification() -> None:
    flags = classify_paths(["README.md", "docs/install.md", "graph/README.md"])

    assert flags == {
        "browser_changed": False,
        "ci_changed": False,
        "docs_only": True,
        "graph_changed": True,
        "graph_only": False,
        "package_changed": False,
        "source_changed": False,
    }


def test_docs_tooling_changes_are_docs_only() -> None:
    flags = classify_paths(["mkdocs.yml", "requirements-docs.txt"])

    assert flags["docs_only"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is False


def test_graph_artifacts_are_graph_only_not_docs_only() -> None:
    flags = classify_paths(["graph/wiki-graph.tar.gz", "graph/communities.json"])

    assert flags["docs_only"] is False
    assert flags["graph_changed"] is True
    assert flags["graph_only"] is True
    assert flags["source_changed"] is False


def test_mixed_graph_and_source_change_is_not_graph_only() -> None:
    flags = classify_paths(["graph/wiki-graph.tar.gz", "src/ctx/adapters/generic/loop.py"])

    assert flags["graph_changed"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is True


def test_source_change_marks_source_and_package() -> None:
    flags = classify_paths(["src/ctx/adapters/generic/loop.py"])

    assert flags["source_changed"] is True
    assert flags["package_changed"] is True
    assert flags["docs_only"] is False


def test_workflow_change_fails_open_for_future_gates() -> None:
    flags = classify_paths([".github/workflows/test.yml"])

    assert flags["ci_changed"] is True
    assert flags["browser_changed"] is True
    assert flags["package_changed"] is True
    assert flags["source_changed"] is True
    assert flags["docs_only"] is False


def test_no_test_policy_covers_ci_package_contract_files() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "scripts/ci_no_test_policy.py" in workflow


def test_no_test_policy_exempts_release_metadata_only_changes() -> None:
    files = ["CHANGELOG.md", "pyproject.toml", "src/ctx/__init__.py"]
    diffs = {
        "CHANGELOG.md": "+## [0.7.4] - 2026-05-05\n",
        "pyproject.toml": '-version = "0.7.3"\n+version = "0.7.4"\n',
        "src/ctx/__init__.py": '-__version__ = "0.7.3"\n+__version__ = "0.7.4"\n',
    }

    assert is_release_metadata_only(files, diffs)
    result = evaluate_policy(files, (), diffs)
    assert result.passed is True
    assert result.message == "Policy exempted for release metadata-only changes."


def test_no_test_policy_rejects_pyproject_dependency_change_without_tests() -> None:
    files = ["pyproject.toml"]
    diffs = {"pyproject.toml": '+    "new-dependency>=1",\n'}

    assert not is_release_metadata_only(files, diffs)
    result = evaluate_policy(files, (), diffs)
    assert result.passed is False


def test_ci_required_expected_jobs_match_workflow_needs() -> None:
    lines = Path(".github/workflows/test.yml").read_text(encoding="utf-8").splitlines()
    jobs: set[str] = set()
    in_ci_required = False
    in_needs = False
    for line in lines:
        if line == "  ci-required:":
            in_ci_required = True
            continue
        if in_ci_required and line.startswith("  ") and not line.startswith("    "):
            break
        if not in_ci_required:
            continue
        if line == "    needs:":
            in_needs = True
            continue
        if in_needs and line.startswith("      - "):
            jobs.add(line.removeprefix("      - "))
            continue
        if in_needs and line.strip():
            break

    assert jobs == REQUIRED_JOBS


def test_browser_security_paths_are_classified() -> None:
    flags = classify_paths(["src/tests/test_ctx_monitor_browser.py"])

    assert flags["browser_changed"] is True
    assert flags["source_changed"] is True


def test_main_writes_github_outputs(tmp_path: Path, monkeypatch) -> None:
    changed = tmp_path / "changed-files.txt"
    output = tmp_path / "github-output.txt"
    changed.write_text("pyproject.toml\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert main([str(changed)]) == 0

    written = output.read_text(encoding="utf-8").splitlines()
    assert "package_changed=true" in written
    assert "source_changed=true" in written
    assert "docs_only=false" in written


def test_ci_required_allows_pr_policy_skip_on_push_only() -> None:
    needs = _required_needs(**{"no-test-no-merge": {"result": "skipped"}})

    assert failed_required_jobs(needs, event_name="push") == {}
    assert failed_required_jobs(needs, event_name="pull_request") == {
        "no-test-no-merge": "skipped",
    }


def test_ci_required_rejects_failed_dependency() -> None:
    needs = _required_needs(test={"result": "failure"})

    assert failed_required_jobs(needs, event_name="push") == {"test": "failure"}


def test_ci_required_rejects_missing_required_dependencies() -> None:
    needs: dict[str, dict[str, Any]] = {
        "classify": {"result": "success"},
        "static": {"result": "success"},
    }

    failures = failed_required_jobs(needs, event_name="push")

    assert failures["package-smoke"] == "missing"
    assert failures["clean-host-contract"] == "missing"
    assert failures["test"] == "missing"


def test_ci_required_allows_full_matrix_skip_on_pr_only() -> None:
    needs = _required_needs(test={"result": "skipped"})

    assert failed_required_jobs(needs, event_name="pull_request") == {}
    assert failed_required_jobs(needs, event_name="push") == {"test": "skipped"}


def test_ci_required_allows_heavy_jobs_to_skip_on_docs_only_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"browser_changed": "false", "docs_only": "true"},
        },
        **{
            "graph-check": {"result": "skipped"},
            "static": {"result": "skipped"},
            "unit-linux": {"result": "skipped"},
            "e2e-canary": {"result": "skipped"},
            "package-build": {"result": "skipped"},
            "package-smoke": {"result": "skipped"},
            "similarity-integration": {"result": "skipped"},
            "clean-host-contract": {"result": "skipped"},
            "no-test-no-merge": {"result": "skipped"},
            "browser-security": {"result": "skipped"},
            "test": {"result": "skipped"},
        },
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}


def test_ci_required_rejects_missing_docs_check_on_docs_only_pr() -> None:
    needs = _required_needs(
        classify={"result": "success", "outputs": {"docs_only": "true"}},
        **{"docs-check": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "docs-check": "skipped",
    }


def test_ci_required_allows_heavy_jobs_to_skip_on_graph_only_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {
                "browser_changed": "false",
                "docs_only": "false",
                "graph_only": "true",
            },
        },
        **{
            "docs-check": {"result": "skipped"},
            "static": {"result": "skipped"},
            "unit-linux": {"result": "skipped"},
            "e2e-canary": {"result": "skipped"},
            "package-build": {"result": "skipped"},
            "package-smoke": {"result": "skipped"},
            "similarity-integration": {"result": "skipped"},
            "clean-host-contract": {"result": "skipped"},
            "no-test-no-merge": {"result": "skipped"},
            "browser-security": {"result": "skipped"},
            "test": {"result": "skipped"},
        },
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}


def test_ci_required_rejects_missing_graph_check_on_graph_only_pr() -> None:
    needs = _required_needs(
        classify={"result": "success", "outputs": {"graph_only": "true"}},
        **{"graph-check": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "graph-check": "skipped",
    }


def test_ci_required_allows_browser_skip_for_unrelated_pr_only() -> None:
    needs = _required_needs(
        classify={"result": "success", "outputs": {"browser_changed": "false"}},
        **{"browser-security": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}
    assert failed_required_jobs(needs, event_name="push") == {
        "browser-security": "skipped",
    }


def test_ci_required_rejects_missing_similarity_gate_on_source_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"docs_only": "false", "graph_only": "false"},
        },
        **{"similarity-integration": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "similarity-integration": "skipped",
    }


def test_ci_required_rejects_browser_skip_when_classifier_requests_it() -> None:
    needs = _required_needs(
        classify={"result": "success", "outputs": {"browser_changed": "true"}},
        **{"browser-security": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "browser-security": "skipped",
    }
