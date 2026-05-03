from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.ci_classifier import classify_paths, main
from scripts.ci_required import failed_required_jobs


def test_docs_only_classification() -> None:
    flags = classify_paths(["README.md", "docs/install.md", "graph/README.md"])

    assert flags == {
        "browser_changed": False,
        "ci_changed": False,
        "docs_only": True,
        "graph_changed": True,
        "package_changed": False,
        "source_changed": False,
    }


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
    needs: dict[str, dict[str, Any]] = {
        "static": {"result": "success"},
        "no-test-no-merge": {"result": "skipped"},
    }

    assert failed_required_jobs(needs, event_name="push") == {}
    assert failed_required_jobs(needs, event_name="pull_request") == {
        "no-test-no-merge": "skipped",
    }


def test_ci_required_rejects_failed_dependency() -> None:
    needs: dict[str, dict[str, Any]] = {
        "static": {"result": "success"},
        "test": {"result": "failure"},
    }

    assert failed_required_jobs(needs, event_name="push") == {"test": "failure"}


def test_ci_required_allows_full_matrix_skip_on_pr_only() -> None:
    needs: dict[str, dict[str, Any]] = {
        "unit-linux": {"result": "success"},
        "test": {"result": "skipped"},
    }

    assert failed_required_jobs(needs, event_name="pull_request") == {}
    assert failed_required_jobs(needs, event_name="push") == {"test": "skipped"}


def test_ci_required_allows_browser_skip_for_unrelated_pr_only() -> None:
    needs: dict[str, dict[str, Any]] = {
        "classify": {"result": "success", "outputs": {"browser_changed": "false"}},
        "browser-security": {"result": "skipped"},
    }

    assert failed_required_jobs(needs, event_name="pull_request") == {}
    assert failed_required_jobs(needs, event_name="push") == {
        "browser-security": "skipped",
    }


def test_ci_required_rejects_browser_skip_when_classifier_requests_it() -> None:
    needs: dict[str, dict[str, Any]] = {
        "classify": {"result": "success", "outputs": {"browser_changed": "true"}},
        "browser-security": {"result": "skipped"},
    }

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "browser-security": "skipped",
    }
