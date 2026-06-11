from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from scripts.ci_classifier import classify_paths, main
from scripts.ci_no_test_policy import evaluate_policy, is_release_metadata_only
from scripts.ci_required import REQUIRED_JOBS, failed_required_jobs


def _workflow_paths() -> tuple[Path, ...]:
    return tuple(sorted(Path(".github/workflows").glob("*.yml")))


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
        "docs_changed": True,
        "docs_only": True,
        "graph_artifact_changed": False,
        "graph_changed": True,
        "graph_only": False,
        "package_changed": False,
        "similarity_changed": False,
        "source_changed": False,
    }


def test_docs_tooling_changes_are_docs_only() -> None:
    flags = classify_paths(["mkdocs.yml", "requirements-docs.txt"])

    assert flags["docs_only"] is True
    assert flags["docs_changed"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is False


def test_graph_artifacts_are_graph_only_not_docs_only() -> None:
    flags = classify_paths([
        "graph/wiki-graph.tar.gz",
        "graph/wiki-graph-runtime.tar.gz",
        "graph/communities.json",
    ])

    assert flags["docs_changed"] is False
    assert flags["docs_only"] is False
    assert flags["graph_artifact_changed"] is True
    assert flags["graph_changed"] is True
    assert flags["graph_only"] is True
    assert flags["similarity_changed"] is False
    assert flags["source_changed"] is False


def test_graph_preview_html_is_graph_artifact() -> None:
    flags = classify_paths(["graph/viz-overview.html"])

    assert flags["docs_changed"] is False
    assert flags["docs_only"] is False
    assert flags["graph_artifact_changed"] is True
    assert flags["graph_changed"] is True
    assert flags["graph_only"] is True


def test_unknown_graph_file_is_graph_artifact() -> None:
    flags = classify_paths(["graph/notes.json"])

    assert flags["docs_changed"] is False
    assert flags["docs_only"] is False
    assert flags["graph_artifact_changed"] is True
    assert flags["graph_changed"] is True
    assert flags["graph_only"] is True


def test_graph_readme_is_docs_not_graph_artifact() -> None:
    flags = classify_paths(["graph/README.md"])

    assert flags["docs_changed"] is True
    assert flags["docs_only"] is True
    assert flags["graph_artifact_changed"] is False
    assert flags["graph_changed"] is True
    assert flags["graph_only"] is True


def test_mixed_graph_and_source_change_is_not_graph_only() -> None:
    flags = classify_paths(["graph/wiki-graph.tar.gz", "src/ctx/adapters/generic/loop.py"])

    assert flags["graph_artifact_changed"] is True
    assert flags["graph_changed"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is True


def test_mixed_source_docs_and_graph_artifact_requests_specific_gates() -> None:
    flags = classify_paths([
        "src/ctx/core/wiki/wiki_graphify.py",
        "docs/knowledge-graph.md",
        "graph/wiki-graph.tar.gz",
    ])

    assert flags["docs_changed"] is True
    assert flags["docs_only"] is False
    assert flags["graph_artifact_changed"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is True


def test_similarity_classifier_covers_ranking_and_intake_modules() -> None:
    for path in (
        "src/corpus_cache.py",
        "src/cosine_ranker.py",
        "src/ctx_config.py",
        "src/intake_gate.py",
    ):
        flags = classify_paths([path])

        assert flags["similarity_changed"] is True
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
    assert flags["similarity_changed"] is True
    assert flags["source_changed"] is True
    assert flags["docs_changed"] is False
    assert flags["docs_only"] is False


def test_no_test_policy_covers_ci_package_contract_files() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "scripts/ci_no_test_policy.py" in workflow


def test_no_test_policy_treats_all_workflows_as_contract_files() -> None:
    for workflow in (path.as_posix() for path in _workflow_paths()):
        result = evaluate_policy([workflow], (), {workflow: "+name: changed\n"})

        assert result.passed is False
        assert result.contract_files == (workflow,)


def test_ci_workflows_default_to_read_only_token_permissions() -> None:
    for workflow_path in _workflow_paths():
        workflow = workflow_path.read_text(encoding="utf-8")

        assert "\npermissions:\n  contents: read\n" in workflow


def test_graph_artifact_job_uses_release_asset_fallback_for_lfs_budget() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "Resolve graph artifacts from release assets" in workflow
    assert "Resolving graph artifacts from matching release assets" in workflow
    assert "git lfs pull" not in workflow
    assert 'tag_name.startswith("graph-artifacts-")' in workflow
    assert "sha256:{expected_oid} size:{expected_size}" in workflow
    assert "Hydrated {path_name} from" in workflow
    assert "graph/wiki-graph-runtime.tar.gz" in workflow
    assert "python src/validate_graph_artifacts.py" in workflow
    assert "validating pointer metadata only" not in workflow


def test_publish_oidc_permission_is_limited_to_publish_job() -> None:
    workflow = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")
    header = workflow.split("\njobs:\n", maxsplit=1)[0]
    publish_job = workflow.split("\n  publish:\n", maxsplit=1)[1]

    assert "id-token: write" not in header
    assert "id-token: write" in publish_job


def test_publish_workflow_rejects_existing_pypi_versions() -> None:
    workflow = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert "Reject already published PyPI version" in workflow
    assert "https://pypi.org/pypi/{name}/{package_version}/json" in workflow
    assert "already exists on PyPI" in workflow


def test_publish_workflow_validates_and_uploads_graph_assets() -> None:
    workflow = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert "Resolve release graph artifacts from release assets" in workflow
    assert "Resolving graph artifacts from matching release assets" in workflow
    assert "git lfs pull" not in workflow
    assert 'tag_name.startswith("graph-artifacts-")' in workflow
    assert "sha256:{expected_oid} size:{expected_size}" in workflow
    assert "Validate release graph artifacts" in workflow
    assert "python src/validate_graph_artifacts.py" in workflow
    assert "python src/update_repo_stats.py --check" in workflow
    assert "graph-release-assets" in workflow
    assert "gh release upload" in workflow
    assert '--repo "$GITHUB_REPOSITORY"' in workflow
    assert "needs.release-assets.result == 'success'" in workflow
    assert "github.event_name == 'workflow_dispatch' || needs.release-assets.result == 'success'" not in workflow
    assert "continue-on-error: true" not in workflow
    assert "needs.release-assets.result == 'skipped'" not in workflow
    assert "PyPI publish will continue without release asset upload" not in workflow
    assert "graph_assets_available" in workflow


def test_changelog_defines_current_release_link() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert version_match is not None
    version = version_match.group(1)
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## [{version}]" in changelog
    assert f"[{version}]: https://github.com/stevesolun/ctx/releases/tag/v{version}" in changelog


def test_pre_commit_refreshes_all_repo_stats_outputs() -> None:
    hook = Path(".githooks/pre-commit").read_text(encoding="utf-8")

    assert "skills-sh-catalog\\.json\\.gz" in hook
    assert "docs/(index|knowledge-graph|catalog)\\.md" in hook
    assert "git add README.md docs/index.md docs/knowledge-graph.md docs/catalog.md" in hook
    assert (
        "README.md, docs/index.md, docs/knowledge-graph.md, and docs/catalog.md "
        "refreshed"
    ) in hook
    assert "CTX_REPO_STATS_TIMEOUT:-240s" in hook
    assert 'timeout "$STATS_TIMEOUT"' in hook


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


def test_no_test_policy_exempts_release_metadata_with_generated_stats() -> None:
    files = [
        "CHANGELOG.md",
        "README.md",
        "docs/index.md",
        "docs/knowledge-graph.md",
        "pyproject.toml",
        "src/ctx/__init__.py",
    ]
    diffs = {
        "CHANGELOG.md": "+## [0.7.17] - 2026-05-09\n",
        "README.md": (
            "-[![Tests](https://img.shields.io/badge/Tests-3693_collected-brightgreen.svg)]"
            "(https://github.com/stevesolun/ctx/actions/workflows/test.yml)\n"
            "+[![Tests](https://img.shields.io/badge/Tests-3696_collected-brightgreen.svg)]"
            "(https://github.com/stevesolun/ctx/actions/workflows/test.yml)\n"
        ),
        "docs/index.md": (
            "-    3,693 tests collected. Ships console scripts.\n"
            "+    3,696 tests collected. Ships console scripts.\n"
        ),
        "docs/knowledge-graph.md": (
            "-| Total nodes | **102,927** |\n"
            "+| Total nodes | **102,928** |\n"
            "-The shipped artifact currently records **102,927 nodes**, "
            "**2,913,959 edges**, **52 Louvain communities**, "
            "**1,683,192 semantic edges**, **897,784 tag edges**,\n"
            "+The shipped artifact currently records **102,928 nodes**, "
            "**2,913,960 edges**, **52 Louvain communities**, "
            "**1,683,193 semantic edges**, **897,784 tag edges**,\n"
        ),
        "pyproject.toml": '-version = "0.7.16"\n+version = "0.7.17"\n',
        "src/ctx/__init__.py": '-__version__ = "0.7.16"\n+__version__ = "0.7.17"\n',
    }

    assert is_release_metadata_only(files, diffs)
    result = evaluate_policy(files, (), diffs)
    assert result.passed is True


def test_no_test_policy_exempts_release_metadata_with_docs_version_line() -> None:
    files = [
        "CHANGELOG.md",
        "docs/index.md",
        "pyproject.toml",
        "src/__init__.py",
        "src/ctx/__init__.py",
    ]
    diffs = {
        "CHANGELOG.md": "+## [1.0.3] - 2026-05-11\n",
        "docs/index.md": (
            "-    **v1.0.2** - MIT, CI-matrixed.\n"
            "+    **v1.0.3** - MIT, CI-matrixed.\n"
        ),
        "pyproject.toml": '-version = "1.0.2"\n+version = "1.0.3"\n',
        "src/__init__.py": '-__version__ = "1.0.2"\n+__version__ = "1.0.3"\n',
        "src/ctx/__init__.py": '-__version__ = "1.0.2"\n+__version__ = "1.0.3"\n',
    }

    assert is_release_metadata_only(files, diffs)
    result = evaluate_policy(files, (), diffs)
    assert result.passed is True


def test_no_test_policy_rejects_release_metadata_with_arbitrary_readme_change() -> None:
    files = ["CHANGELOG.md", "README.md", "pyproject.toml", "src/ctx/__init__.py"]
    diffs = {
        "CHANGELOG.md": "+## [0.7.17] - 2026-05-09\n",
        "README.md": "+New feature prose.\n",
        "pyproject.toml": '-version = "0.7.16"\n+version = "0.7.17"\n',
        "src/ctx/__init__.py": '-__version__ = "0.7.16"\n+__version__ = "0.7.17"\n',
    }

    assert not is_release_metadata_only(files, diffs)
    result = evaluate_policy(files, (), diffs)
    assert result.passed is False


def test_no_test_policy_rejects_readme_version_prose_change() -> None:
    files = ["CHANGELOG.md", "README.md", "pyproject.toml", "src/ctx/__init__.py"]
    diffs = {
        "CHANGELOG.md": "+## [1.0.3] - 2026-05-11\n",
        "README.md": "-**v1.0.2** install notes.\n+**v1.0.3** install notes.\n",
        "pyproject.toml": '-version = "1.0.2"\n+version = "1.0.3"\n',
        "src/ctx/__init__.py": '-__version__ = "1.0.2"\n+__version__ = "1.0.3"\n',
    }

    assert not is_release_metadata_only(files, diffs)
    result = evaluate_policy(files, (), diffs)
    assert result.passed is False


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


def test_similarity_paths_are_classified() -> None:
    flags = classify_paths(["src/ctx/core/graph/semantic_edges.py"])

    assert flags["similarity_changed"] is True
    assert flags["source_changed"] is True


def test_embedding_backend_change_runs_similarity_gate() -> None:
    flags = classify_paths(["src/embedding_backend.py"])

    assert flags["similarity_changed"] is True
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
    assert "docs_changed=false" in written
    assert "docs_only=false" in written


def test_main_handles_utf8_bom_changed_files(tmp_path: Path, monkeypatch) -> None:
    changed = tmp_path / "changed-files.txt"
    output = tmp_path / "github-output.txt"
    changed.write_text("\ufeffgraph/wiki-graph.tar.gz\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    assert main([str(changed)]) == 0

    written = output.read_text(encoding="utf-8").splitlines()
    assert "graph_artifact_changed=true" in written
    assert "graph_only=true" in written


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
    assert failures["contract-compat"] == "missing"
    assert failures["test"] == "missing"


def test_ci_required_allows_full_matrix_skip_on_pr_only() -> None:
    needs = _required_needs(test={"result": "skipped"})

    assert failed_required_jobs(needs, event_name="pull_request") == {}
    assert failed_required_jobs(needs, event_name="push") == {"test": "skipped"}


def test_ci_required_allows_heavy_jobs_to_skip_on_docs_only_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {
                "browser_changed": "false",
                "docs_changed": "true",
                "docs_only": "true",
                "graph_artifact_changed": "false",
            },
        },
        **{
            "graph-check": {"result": "skipped"},
            "static": {"result": "skipped"},
            "unit-linux": {"result": "skipped"},
            "contract-compat": {"result": "skipped"},
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
        classify={
            "result": "success",
            "outputs": {"docs_changed": "true", "docs_only": "true"},
        },
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
                "docs_changed": "false",
                "docs_only": "false",
                "graph_artifact_changed": "true",
                "graph_only": "true",
            },
        },
        **{
            "docs-check": {"result": "skipped"},
            "static": {"result": "skipped"},
            "unit-linux": {"result": "skipped"},
            "contract-compat": {"result": "skipped"},
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
        classify={
            "result": "success",
            "outputs": {"graph_artifact_changed": "true", "graph_only": "true"},
        },
        **{"graph-check": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "graph-check": "skipped",
    }


def test_ci_required_allows_graph_check_skip_for_nonartifact_graph_change() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {
                "docs_only": "false",
                "graph_artifact_changed": "false",
                "graph_changed": "true",
                "graph_only": "true",
            },
        },
        **{"graph-check": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}


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
            "outputs": {
                "docs_only": "false",
                "graph_only": "false",
                "similarity_changed": "true",
            },
        },
        **{"similarity-integration": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "similarity-integration": "skipped",
    }


def test_ci_required_rejects_missing_similarity_gate_on_graph_only_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {
                "docs_only": "false",
                "graph_only": "true",
                "graph_artifact_changed": "true",
                "similarity_changed": "true",
            },
        },
        **{"similarity-integration": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "similarity-integration": "skipped",
    }


def test_ci_required_allows_similarity_skip_for_unrelated_source_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {
                "docs_only": "false",
                "graph_only": "false",
                "similarity_changed": "false",
            },
        },
        **{"similarity-integration": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}


def test_ci_required_rejects_contract_compat_skip_on_source_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"docs_only": "false", "graph_only": "false"},
        },
        **{"contract-compat": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "contract-compat": "skipped",
    }


def test_ci_required_rejects_browser_skip_when_classifier_requests_it() -> None:
    needs = _required_needs(
        classify={"result": "success", "outputs": {"browser_changed": "true"}},
        **{"browser-security": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "browser-security": "skipped",
    }


def test_ci_required_rejects_missing_docs_check_on_mixed_docs_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"docs_changed": "true", "docs_only": "false"},
        },
        **{"docs-check": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "docs-check": "skipped",
    }


def test_ci_required_rejects_missing_graph_check_on_mixed_artifact_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"graph_artifact_changed": "true", "graph_only": "false"},
        },
        **{"graph-check": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "graph-check": "skipped",
    }
