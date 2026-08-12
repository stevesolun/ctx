from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import yaml

import scripts.ci_no_test_policy as ci_no_test_policy
from scripts.ci_classifier import classify_paths, main
from scripts.ci_no_test_policy import PolicyResult, evaluate_policy, is_release_metadata_only
from scripts.ci_required import REQUIRED_JOBS, failed_required_jobs


def _workflow_paths() -> tuple[Path, ...]:
    return tuple(sorted(Path(".github/workflows").glob("*.yml")))


def _release_version_tuple(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    assert match is not None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _required_needs(
    **overrides: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    needs: dict[str, dict[str, Any]] = {name: {"result": "success"} for name in REQUIRED_JOBS}
    needs.update(overrides)
    return needs


def _dependabot_result(path: str, before: str, after: str) -> PolicyResult:
    return evaluate_policy(
        [path],
        (),
        {path: "diff contents are not trusted for this exemption"},
        actor="dependabot[bot]",
        blobs_by_file={path: (before, after)},
    )


def _pyproject_blob(
    *,
    dependency: str = "example>=1,<3",
    optional: str = "pytest>=8",
    build: str = "setuptools>=77",
    version: str = "1.0.0",
) -> str:
    return (
        "[build-system]\n"
        f"requires = [{json.dumps(build)}]\n"
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        'name = "example"\n'
        f'version = "{version}"\n'
        f"dependencies = [{json.dumps(dependency)}]\n\n"
        "[project.optional-dependencies]\n"
        f"dev = [{json.dumps(optional)}]\n"
    )


def _workflow_blob(
    *,
    checkout_ref: str = "v4",
    setup_ref: str = "v5.1.0",
    env_value: str = "stable",
    fetch_depth: int = 0,
    reverse_steps: bool = False,
) -> str:
    checkout = (
        "      - name: Checkout\n"
        f"        uses: actions/checkout@{checkout_ref}\n"
        "        with:\n"
        f"          fetch-depth: {fetch_depth}\n"
    )
    setup = f"      - name: Setup Python\n        uses: actions/setup-python@{setup_ref}\n"
    steps = setup + checkout if reverse_steps else checkout + setup
    return (
        "name: Tests\n"
        "on: [push]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        f"      NOTE: {json.dumps(env_value)}\n"
        "    steps:\n"
        f"{steps}"
    )


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
        "telemetry_changed": False,
    }


def test_docs_tooling_changes_are_docs_only() -> None:
    flags = classify_paths(["mkdocs.yml", "requirements-docs.txt"])

    assert flags["docs_only"] is True
    assert flags["docs_changed"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is False


def test_security_gate_configs_trigger_ci_lanes() -> None:
    for path in (
        ".github/codeql/codeql-config.yml",
        ".github/dependabot.yml",
        ".github/pip-audit-ignore.txt",
        ".github/requirements-no-test-policy.txt",
    ):
        flags = classify_paths([path])

        assert flags["ci_changed"] is True
        assert flags["docs_only"] is False
        assert flags["package_changed"] is True
        assert flags["source_changed"] is True


def test_pip_audit_policy_has_no_active_blanket_exemptions() -> None:
    policy_path = Path(".github/pip-audit-ignore.txt")

    assert policy_path.is_file()
    active_entries = [
        line.split("#", maxsplit=1)[0].strip()
        for line in policy_path.read_text(encoding="utf-8").splitlines()
        if line.split("#", maxsplit=1)[0].strip()
    ]
    assert active_entries == []


def test_qa_feature_status_tracker_is_docs_only() -> None:
    flags = classify_paths(["qa/feature_status.csv"])

    assert flags["docs_only"] is True
    assert flags["docs_changed"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is False


def test_qa_bug_smoke_status_tracker_is_docs_only() -> None:
    flags = classify_paths(["qa/bug_smoke_status.csv"])

    assert flags["docs_only"] is True
    assert flags["docs_changed"] is True
    assert flags["graph_only"] is False
    assert flags["source_changed"] is False


def test_qa_helper_scripts_are_not_docs_only() -> None:
    flags = classify_paths(["qa/check_feature_status.py"])

    assert flags["docs_only"] is False
    assert flags["docs_changed"] is False
    assert flags["graph_only"] is False


def test_graph_artifacts_are_graph_only_not_docs_only() -> None:
    flags = classify_paths(
        [
            "graph/wiki-graph.tar.gz",
            "graph/wiki-graph-runtime.tar.gz",
            "graph/communities.json",
        ]
    )

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
    flags = classify_paths(
        [
            "src/ctx/core/wiki/wiki_graphify.py",
            "docs/knowledge-graph.md",
            "graph/wiki-graph.tar.gz",
        ]
    )

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


def test_reproducible_build_script_marks_package_changed() -> None:
    flags = classify_paths(["scripts/build_reproducible_dist.py"])

    assert flags["package_changed"] is True
    assert flags["source_changed"] is True


def test_workflow_change_fails_open_for_future_gates() -> None:
    flags = classify_paths([".github/workflows/test.yml"])

    assert flags["ci_changed"] is True
    assert flags["browser_changed"] is True
    assert flags["package_changed"] is True
    assert flags["similarity_changed"] is True
    assert flags["source_changed"] is True
    assert flags["telemetry_changed"] is True
    assert flags["docs_changed"] is False
    assert flags["docs_only"] is False


def test_ci_gate_script_change_fails_open_for_future_gates() -> None:
    for path in (
        ".github/actions/setup/action.yml",
        ".no-mistakes.yaml",
        "scripts/ci_classifier.py",
        "scripts/ci_preflight.py",
        "scripts/ci_required.py",
        "scripts/local_fast_gate.py",
        "scripts/no_mistakes_run.sh",
    ):
        flags = classify_paths([path])

        assert flags["ci_changed"] is True
        assert flags["browser_changed"] is True
        assert flags["package_changed"] is True
        assert flags["similarity_changed"] is True
        assert flags["source_changed"] is True
        assert flags["telemetry_changed"] is True


def test_no_test_policy_covers_ci_package_contract_files() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "scripts/ci_no_test_policy.py" in workflow
    assert "python -m ruff format --check src hooks scripts" in workflow
    assert "release metadata-only changes" in workflow
    assert "no-tests-needed label" in workflow
    assert "PR_ACTOR: ${{ github.actor }}" in workflow
    assert "PR_LABELS_JSON: ${{ toJson(github.event.pull_request.labels.*.name) }}" in workflow
    assert '--labels-json "$PR_LABELS_JSON"' in workflow
    assert '--actor "$PR_ACTOR"' in workflow
    assert "--require-hashes" in workflow
    assert "--only-binary=:all:" in workflow
    assert "-r .github/requirements-no-test-policy.txt" in workflow
    assert "cache-dependency-path: .github/requirements-no-test-policy.txt" in workflow
    assert "packaging>=" not in workflow
    assert "PyYAML>=" not in workflow


def test_no_test_policy_dependencies_are_exact_and_hash_locked() -> None:
    lock_path = Path(".github/requirements-no-test-policy.txt")
    lock = lock_path.read_text(encoding="utf-8")
    records = ci_no_test_policy._requirements_records(lock)

    assert records is not None
    dependencies: dict[str, tuple[str, ...]] = {}
    for _wrapper, spec, hashes in records:
        if spec is None:
            continue
        requirement = ci_no_test_policy.Requirement(spec)
        specifiers = tuple(requirement.specifier)
        assert len(specifiers) == 1
        assert specifiers[0].operator == "=="
        assert hashes
        assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)
        dependencies[requirement.name] = hashes
    assert set(dependencies) == {"packaging", "PyYAML"}


def test_no_test_policy_runs_for_every_pull_request() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    policy_job = workflow.split("\n  no-test-no-merge:\n", maxsplit=1)[1].split(
        "\n  ci-required:", maxsplit=1
    )[0]

    assert "if: ${{ github.event_name == 'pull_request' }}" in policy_job
    assert "docs_only" not in policy_job
    assert "graph_only" not in policy_job
    policy_shell = policy_job.split("        run: |\n", maxsplit=1)[1]
    assert "${{" not in policy_shell
    assert "LABELS='" not in policy_shell


def test_ci_required_requires_no_test_policy_success_on_every_pr() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    required_job = workflow.split("\n  ci-required:\n", maxsplit=1)[1]

    assert "NO_TEST_NO_MERGE_RESULT: ${{ needs.no-test-no-merge.result }}" in required_job
    assert (
        'if [[ "$EVENT_NAME" == "pull_request" && '
        '"$NO_TEST_NO_MERGE_RESULT" != "success" ]]' in required_job
    )
    assert "::error::no-test-no-merge must pass on every pull request" in required_job


def test_dependabot_groups_weekly_python_and_action_updates() -> None:
    config = yaml.safe_load(Path(".github/dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}

    assert config["version"] == 2
    assert set(updates) == {"pip", "github-actions"}
    for ecosystem, group_name in (
        ("pip", "python-dependencies"),
        ("github-actions", "github-actions"),
    ):
        update = updates[ecosystem]
        if ecosystem == "pip":
            assert update["directories"] == ["/", "/.github"]
            assert "directory" not in update
        else:
            assert update["directory"] == "/"
            assert "directories" not in update
        assert update["schedule"] == {
            "interval": "weekly",
            "day": "monday",
            "time": "06:00",
            "timezone": "Etc/UTC",
        }
        assert update["open-pull-requests-limit"] == 2
        assert update["groups"] == {group_name: {"patterns": ["*"]}}


def test_no_test_policy_treats_all_workflows_as_contract_files() -> None:
    for workflow in (path.as_posix() for path in _workflow_paths()):
        result = evaluate_policy([workflow], (), {workflow: "+name: changed\n"})

        assert result.passed is False
        assert result.contract_files == (workflow,)


def test_no_test_policy_requires_tests_for_gate_runtime_scripts() -> None:
    for script in (
        ".github/actions/setup/action.yml",
        "scripts/ci_classifier.py",
        "scripts/local_fast_gate.py",
        "scripts/no_mistakes_run.sh",
    ):
        result = evaluate_policy([script], (), {script: "+echo changed\n"})

        assert result.passed is False
        assert result.contract_files == (script,)


def test_no_test_policy_allows_no_tests_needed_label_for_contract_changes() -> None:
    result = evaluate_policy(
        ["src/ctx/api.py"],
        ("no-tests-needed",),
        {"src/ctx/api.py": "+def adapter_only_helper():\n"},
    )

    assert result.passed is True
    assert result.message == "Policy exempted by no-tests-needed label."
    assert result.contract_files == ("src/ctx/api.py",)
    assert result.test_files == ()


def test_no_test_policy_requires_tests_for_release_sync_artifact_scripts() -> None:
    for script in (
        "scripts/sync_huggingface.py",
        "scripts/pack_full_wiki_tar.py",
        "scripts/graph_artifact_guard.py",
    ):
        result = evaluate_policy([script], (), {script: "+print('changed')\n"})

        assert result.passed is False
        assert result.contract_files == (script,)


def test_ci_workflows_default_to_read_only_token_permissions() -> None:
    for workflow_path in _workflow_paths():
        workflow = workflow_path.read_text(encoding="utf-8")

        assert "\npermissions:\n  contents: read\n" in workflow


def test_graph_artifact_job_uses_release_asset_fallback_for_lfs_budget() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "Resolve graph artifacts from release assets or targeted LFS" in workflow
    assert "Resolving graph artifacts from release cache, or targeted Git LFS" in workflow
    assert 'tag_name.startswith("graph-artifacts-")' in workflow
    assert "sha256:{expected_oid} size:{expected_size}" in workflow
    assert "Pointer for {path_name} is not in release cache" in workflow
    assert "searching release caches before targeted Git LFS" in workflow
    assert '"git", "lfs", "pull", "--include", path_name' in workflow
    assert "GIT_LFS_ACTIVITYTIMEOUT" in workflow
    assert "Hydrated {path_name} from" in workflow
    assert "graph/wiki-graph-runtime.tar.gz" in workflow
    assert "python src/validate_graph_artifacts.py" in workflow
    assert "validating pointer metadata only" not in workflow


def test_similarity_gate_caches_and_predownloads_real_model() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "actions/cache@v4" not in workflow
    assert "actions/cache@caa296126883cff596d87d8935842f9db880ef25 # v5.1.0" in workflow
    assert "Cache MiniLM model" in workflow
    assert "hf-sentence-transformers-all-MiniLM-L6-v2-v1" in workflow
    assert "Pre-download MiniLM model" in workflow
    assert "sentence-transformers/all-MiniLM-L6-v2" in workflow
    assert "CTX_REQUIRE_SIMILARITY_EVAL" in workflow
    assert "src/tests/test_similarity_precision_recall.py" in workflow


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


def test_publish_static_gate_uses_canonical_python_target() -> None:
    workflow = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")
    setup_match = re.search(
        r"- name: Set up Python\n"
        r"\s+uses: actions/setup-python@"
        r"ece7cb06caefa5fff74198d8649806c4678c61a1 # v6\.3\.0\n"
        r"\s+with:\n"
        r'\s+python-version: "([^"]+)"',
        workflow,
    )

    assert setup_match is not None
    assert setup_match.group(1) == "3.11"
    assert "python -m mypy src" in workflow


def test_publish_workflow_validates_and_uploads_graph_assets() -> None:
    workflow = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert "Resolve release graph artifacts from release assets" in workflow
    assert "Resolving graph artifacts from release cache, or targeted Git LFS" in workflow
    assert "searching release caches before targeted Git LFS" in workflow
    assert '"git", "lfs", "pull", "--include", path_name' in workflow
    assert "verify_hydrated_file(graph_tar, expected_oid, expected_size)" in workflow
    assert 'tag_name.startswith("graph-artifacts-")' in workflow
    assert "sha256:{expected_oid} size:{expected_size}" in workflow
    assert "Validate release graph artifacts" in workflow
    assert "python src/validate_graph_artifacts.py" in workflow
    assert "python src/update_repo_stats.py --check" in workflow
    assert "graph-release-assets" in workflow
    assert "gh release upload" in workflow
    assert '--repo "$GITHUB_REPOSITORY"' in workflow
    assert "needs.release-assets.result == 'success'" in workflow
    assert (
        "github.event_name == 'workflow_dispatch' || needs.release-assets.result == 'success'"
        not in workflow
    )
    assert "continue-on-error: true" not in workflow
    assert "needs.release-assets.result == 'skipped'" not in workflow
    assert "PyPI publish will continue without release asset upload" not in workflow
    assert "graph_assets_available" in workflow


def test_publish_workflow_runs_installed_wheel_telemetry_smoke() -> None:
    workflow = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert "Telemetry release smoke" in workflow
    assert "record_event(" in workflow
    assert "record_counter(" in workflow
    assert "ctx-telemetry-export --dry-run --json" in workflow
    assert "ctx-telemetry-export \\" in workflow
    assert "--signal metrics" in workflow
    assert "ctx-telemetry-retention plan --signal all --json" in workflow
    assert "raw-release-telemetry-sentinel" in workflow
    assert "raw telemetry sentinel leaked into exported telemetry" in workflow


def test_changelog_tracks_current_version_or_unreleased_changes() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert version_match is not None
    version = version_match.group(1)
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    if f"## [{version}]" in changelog:
        assert (
            f"[{version}]: https://github.com/stevesolun/ctx/releases/tag/v{version}" in changelog
        )
        return

    release_versions = [
        _release_version_tuple(match.group("version"))
        for match in re.finditer(
            r"^\[(?P<version>\d+\.\d+\.\d+)\]: "
            r"https://github\.com/stevesolun/ctx/releases/tag/v(?P=version)$",
            changelog,
            re.MULTILINE,
        )
    ]
    assert release_versions
    latest_release = max(release_versions)
    assert _release_version_tuple(version) == (
        latest_release[0],
        latest_release[1],
        latest_release[2] + 1,
    )

    unreleased_match = re.search(
        r"## \[Unreleased\](?P<section>.*?)(?=\n## \[|\Z)",
        changelog,
        re.DOTALL,
    )
    assert unreleased_match is not None
    assert unreleased_match.group("section").strip()


def test_pre_commit_refreshes_all_repo_stats_outputs() -> None:
    hook = Path(".githooks/pre-commit").read_text(encoding="utf-8")

    assert "skills-sh-catalog\\.json\\.gz" in hook
    assert "docs/(index|knowledge-graph|catalog)\\.md" in hook
    assert '"$REPO_ROOT/.venv/bin/python"' in hook
    assert '"python3.12"' in hook
    assert '"python3.11"' in hook
    assert "sys.version_info >= (3, 11)" in hook
    assert "Python >=3.11 is required for repo stats" in hook
    assert 'PYTHON="${PYTHON:-python3}"' not in hook
    assert "git add README.md docs/index.md docs/knowledge-graph.md docs/catalog.md" in hook
    assert (
        "README.md, docs/index.md, docs/knowledge-graph.md, and docs/catalog.md refreshed"
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
            "-    **v1.0.2** - MIT, CI-matrixed.\n+    **v1.0.3** - MIT, CI-matrixed.\n"
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


def test_no_test_policy_exempts_dependabot_python_version_updates() -> None:
    before = _pyproject_blob(
        dependency=("hnswlib>=0.8,<0.9; platform_python_implementation == 'CPython'"),
    )
    after = _pyproject_blob(
        dependency=("hnswlib>=0.9,<1; platform_python_implementation == 'CPython'"),
        optional="pytest>=8.4",
        build="setuptools>=80",
    )

    result = _dependabot_result("pyproject.toml", before, after)

    assert result.passed is True
    assert result.message == "Policy exempted for Dependabot dependency version updates."
    assert result.contract_files == ("pyproject.toml",)


def test_no_test_policy_exempts_strict_requirements_version_updates() -> None:
    before = "# docs\nmkdocs==1.6.0  # renderer\npytest>=8\n"
    after = "# docs\nmkdocs==1.6.1  # renderer\npytest>=8.4\n"

    result = _dependabot_result("requirements-docs.txt", before, after)

    assert result.passed is True


def test_no_test_policy_exempts_hashed_requirement_version_updates() -> None:
    before = (
        "packaging==26.1 \\\n"
        f"    --hash=sha256:{'a' * 64}\n"
        "PyYAML==6.0.3 \\\n"
        f"    --hash=sha256:{'b' * 64}\n"
    )
    after = before.replace("packaging==26.1", "packaging==26.2").replace(
        "a" * 64,
        "c" * 64,
    )

    result = _dependabot_result(
        ".github/requirements-no-test-policy.txt",
        before,
        after,
    )

    assert result.passed is True


def test_no_test_policy_exempts_regenerated_hash_cardinality() -> None:
    before = f"packaging==26.1 \\\n    --hash=sha256:{'a' * 64}\n"
    after = f"packaging==26.2 \\\n    --hash=sha256:{'b' * 64} \\\n    --hash=sha256:{'c' * 64}\n"

    result = _dependabot_result(
        ".github/requirements-no-test-policy.txt",
        before,
        after,
    )

    assert result.passed is True


def test_no_test_policy_rejects_unsafe_hashed_requirement_updates() -> None:
    old_hash = "a" * 64
    new_hash = "b" * 64
    before = f"packaging==26.1 \\\n    --hash=sha256:{old_hash}\n"
    cases = (
        f"packaging==26.1 \\\n    --hash=sha256:{new_hash}\n",
        f"packaging==26.2 \\\n    --hash=sha256:{old_hash}\n",
        "packaging==26.2 \\\n    --hash=sha256:not-a-digest\n",
        "packaging==26.2 \\\n    --trusted-host example.test\n",
    )

    for after in cases:
        result = _dependabot_result(
            ".github/requirements-no-test-policy.txt",
            before,
            after,
        )
        assert result.passed is False


def test_no_test_policy_rejects_reordered_unchanged_hash_set() -> None:
    before = f"packaging==26.1 \\\n    --hash=sha256:{'a' * 64} \\\n    --hash=sha256:{'b' * 64}\n"
    after = f"packaging==26.2 \\\n    --hash=sha256:{'b' * 64} \\\n    --hash=sha256:{'a' * 64}\n"

    result = _dependabot_result(
        ".github/requirements-no-test-policy.txt",
        before,
        after,
    )

    assert result.passed is False


def test_no_test_policy_preserves_unchanged_pyproject_direct_source_identity() -> None:
    direct_source = "example @ https://example.test/example-v1.whl"
    before = _pyproject_blob(dependency=direct_source)
    after = _pyproject_blob(dependency=direct_source, optional="pytest>=8.4")

    assert _dependabot_result("pyproject.toml", before, after).passed is True


def test_no_test_policy_exempts_dependabot_action_ref_updates() -> None:
    path = ".github/workflows/test.yml"
    result = _dependabot_result(
        path,
        _workflow_blob(),
        _workflow_blob(checkout_ref="v5", setup_ref="v6.0.1"),
    )

    assert result.passed is True
    assert result.message == "Policy exempted for Dependabot dependency version updates."


def test_no_test_policy_exempts_dependabot_immutable_action_sha_updates() -> None:
    path = ".github/workflows/test.yml"
    old_sha = "a" * 40
    new_sha = "b" * 40

    result = _dependabot_result(
        path,
        _workflow_blob(checkout_ref=old_sha),
        _workflow_blob(checkout_ref=new_sha),
    )

    assert result.passed is True


def test_no_test_policy_exempts_composite_action_step_ref_updates() -> None:
    path = ".github/actions/setup/action.yml"
    before = "name: Setup\nruns:\n  using: composite\n  steps:\n    - uses: actions/cache@v4\n"
    after = before.replace("actions/cache@v4", "actions/cache@v5")

    assert _dependabot_result(path, before, after).passed is True


def test_no_test_policy_exempts_multiple_structurally_valid_dependency_files() -> None:
    files = ("pyproject.toml", ".github/workflows/test.yml")
    blobs = {
        "pyproject.toml": (
            _pyproject_blob(),
            _pyproject_blob(dependency="example>=2,<4"),
        ),
        ".github/workflows/test.yml": (
            _workflow_blob(),
            _workflow_blob(checkout_ref="v5"),
        ),
    }

    result = evaluate_policy(
        files,
        (),
        {path: "ignored" for path in files},
        actor="dependabot[bot]",
        blobs_by_file=blobs,
    )

    assert result.passed is True


def test_no_test_policy_rejects_pyproject_non_dependency_or_location_changes() -> None:
    before = _pyproject_blob()
    cases = (
        _pyproject_blob(dependency="example>=2,<4", version="1.0.1"),
        _pyproject_blob(dependency="pytest>=9", optional="example>=2,<4"),
        _pyproject_blob().replace(
            'dependencies = ["example>=1,<3"]',
            'dependencies = ["example>=2,<4", "added>=1"]',
        ),
    )

    for after in cases:
        assert _dependabot_result("pyproject.toml", before, after).passed is False


def test_no_test_policy_rejects_dependency_identity_and_direct_source_changes() -> None:
    cases = (
        (
            _pyproject_blob(dependency="example>=1"),
            _pyproject_blob(dependency="different>=2"),
        ),
        (
            _pyproject_blob(dependency="example[http]>=1"),
            _pyproject_blob(dependency="example[https]>=2"),
        ),
        (
            _pyproject_blob(dependency="example>=1; platform_system == 'Linux'"),
            _pyproject_blob(dependency="example>=2; platform_system == 'linux'"),
        ),
        (
            _pyproject_blob(dependency="example @ https://example.test/example-v1.whl"),
            _pyproject_blob(dependency="example @ https://example.test/example-v2.whl"),
        ),
    )

    for before, after in cases:
        assert _dependabot_result("pyproject.toml", before, after).passed is False


def test_no_test_policy_rejects_provable_or_ambiguous_dependency_downgrades() -> None:
    cases = (
        ("example>=2", "example>=1"),
        ("example<4", "example<3"),
        ("example==2", "example==1"),
        ("example~=2.4", "example~=2.3"),
        ("example>=2,<4", "example>=3,<3"),
        ("example>=2", "example==3"),
        ("example!=1", "example!=2"),
    )

    for old_requirement, new_requirement in cases:
        result = _dependabot_result(
            "pyproject.toml",
            _pyproject_blob(dependency=old_requirement),
            _pyproject_blob(dependency=new_requirement),
        )
        assert result.passed is False


def test_no_test_policy_rejects_unsatisfiable_dependency_ranges() -> None:
    cases = (
        ("example>=1,<2", "example>=3,<2.5"),
        ("example>1,<2", "example>2,<2"),
        ("example~=2.4,<3", "example~=3.0,<3"),
    )

    for before, after in cases:
        result = _dependabot_result(
            "pyproject.toml",
            _pyproject_blob(dependency=before),
            _pyproject_blob(dependency=after),
        )
        assert result.passed is False


def test_no_test_policy_rejects_requirements_directives_urls_and_moves() -> None:
    cases = (
        (
            "-r base.txt\nmkdocs==1.6.0\n",
            "-r base.txt\nmkdocs==1.6.1\n",
        ),
        (
            "example @ https://example.test/example.whl\nmkdocs==1.6.0\n",
            "example @ https://example.test/example.whl\nmkdocs==1.6.1\n",
        ),
        (
            "# docs\nmkdocs==1.6.0\n",
            "# changed\nmkdocs==1.6.1\n",
        ),
        (
            "mkdocs==1.6.0\npytest>=8\n",
            "pytest>=9\nmkdocs==1.6.1\n",
        ),
        (
            "mkdocs==1.6.0\n",
            "mkdocs==1.6.1\npytest>=8\n",
        ),
    )

    for before, after in cases:
        result = _dependabot_result("requirements-docs.txt", before, after)
        assert result.passed is False


def test_no_test_policy_rejects_requirements_downgrades() -> None:
    for before, after in (
        ("mkdocs>=2\n", "mkdocs>=1\n"),
        ("mkdocs<4\n", "mkdocs<3\n"),
        ("mkdocs==2\n", "mkdocs==1\n"),
    ):
        assert _dependabot_result("constraints-ci.txt", before, after).passed is False


def test_no_test_policy_rejects_workflow_env_moves_and_logic_changes() -> None:
    path = ".github/workflows/test.yml"
    cases = (
        (
            _workflow_blob(env_value="uses: actions/checkout@v4"),
            _workflow_blob(
                checkout_ref="v5",
                env_value="uses: actions/checkout@v5",
            ),
        ),
        (
            _workflow_blob(),
            _workflow_blob(
                checkout_ref="v5",
                setup_ref="v6.0.1",
                reverse_steps=True,
            ),
        ),
        (
            _workflow_blob(),
            _workflow_blob(checkout_ref="v5", fetch_depth=1),
        ),
        (
            _workflow_blob(),
            _workflow_blob(checkout_ref="v5").replace("on: [push]", "true: [push]"),
        ),
        (
            _workflow_blob(),
            _workflow_blob(checkout_ref="v5").replace(
                "name: Tests\n",
                "name: Hidden\nname: Tests\n",
            ),
        ),
    )

    for before, after in cases:
        assert _dependabot_result(path, before, after).passed is False


def test_no_test_policy_rejects_job_level_uses_updates() -> None:
    path = ".github/workflows/reusable.yml"
    before = (
        "name: Reuse\n"
        "on: [push]\n"
        "jobs:\n"
        "  call:\n"
        "    uses: owner/repo/.github/workflows/build.yml@v1\n"
    )
    after = before.replace("@v1", "@v2")

    assert _dependabot_result(path, before, after).passed is False


def test_no_test_policy_rejects_unsafe_or_downgraded_action_refs() -> None:
    path = ".github/workflows/test.yml"
    cases = (
        ("v4", "main"),
        ("main", "v5"),
        ("release-v4", "v5"),
        ("v5", "v4"),
        ("v5.2", "v5.1"),
        ("v5.2", "v6.0.1"),
        ("v4", "abc123"),
        ("a" * 40, "v5"),
        ("v4", "v5-beta"),
        ("v04", "v05"),
    )

    for old_ref, new_ref in cases:
        result = _dependabot_result(
            path,
            _workflow_blob(checkout_ref=old_ref),
            _workflow_blob(checkout_ref=new_ref),
        )
        assert result.passed is False


def test_no_test_policy_rejects_changed_action_identity() -> None:
    path = ".github/workflows/test.yml"
    before = _workflow_blob()
    after = _workflow_blob(checkout_ref="v5").replace(
        "actions/checkout@v5",
        "evil/checkout@v5",
    )

    assert _dependabot_result(path, before, after).passed is False


def test_no_test_policy_dependabot_exemption_fails_closed_without_blobs() -> None:
    path = "pyproject.toml"
    result = evaluate_policy(
        [path],
        (),
        {path: '-dependencies = ["example>=1"]\n+dependencies = ["example>=2"]\n'},
        actor="dependabot[bot]",
    )

    assert result.passed is False
    assert result.message == "Dependabot changed files beyond dependency version updates."


def test_no_test_policy_main_loads_base_and_head_blobs(monkeypatch: Any) -> None:
    path = "pyproject.toml"
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    monkeypatch.setattr(
        ci_no_test_policy,
        "_changed_files",
        lambda base, head: (path,),
    )
    monkeypatch.setattr(
        ci_no_test_policy,
        "_diffs_by_file",
        lambda base, head, files: {path: "ignored"},
    )

    def load_blobs(
        base: str,
        head: str,
        files: tuple[str, ...],
    ) -> dict[str, tuple[str, str]]:
        calls.append((base, head, files))
        return {
            path: (
                _pyproject_blob(),
                _pyproject_blob(dependency="example>=2,<4"),
            )
        }

    monkeypatch.setattr(ci_no_test_policy, "_blobs_by_file", load_blobs)

    assert (
        ci_no_test_policy.main(
            [
                "--base",
                "base-sha",
                "--head",
                "head-sha",
                "--actor",
                "dependabot[bot]",
            ]
        )
        == 0
    )
    assert calls == [("base-sha", "head-sha", (path,))]


def test_no_test_policy_rejects_human_dependency_updates_without_tests() -> None:
    cases = (
        (
            "pyproject.toml",
            '-    "pytest>=8",\n+    "pytest>=8.4",\n',
        ),
        (
            "requirements-docs.txt",
            "-mkdocs==1.6.0\n+mkdocs==1.6.1\n",
        ),
        (
            ".github/workflows/test.yml",
            "-        uses: actions/checkout@v4\n+        uses: actions/checkout@v5\n",
        ),
    )

    for path, diff_text in cases:
        result = evaluate_policy([path], (), {path: diff_text}, actor="stevesolun")

        assert result.passed is False
        assert result.message == "Contract files changed without accompanying tests."


def test_no_test_policy_requires_exact_dependabot_actor() -> None:
    path = "pyproject.toml"
    diff_text = '-    "pytest>=8",\n+    "pytest>=8.4",\n'

    for actor in ("dependabot", "dependabot[bot] ", "renovate[bot]", ""):
        result = evaluate_policy([path], (), {path: diff_text}, actor=actor)

        assert result.passed is False


def test_no_test_policy_rejects_dependabot_non_version_changes() -> None:
    cases = (
        {"src/ctx/api.py": ("ENABLED = False\n", "ENABLED = True\n")},
        {
            ".github/workflows/test.yml": (
                _workflow_blob(),
                _workflow_blob(env_value="changed"),
            )
        },
        {
            ".github/workflows/test.yml": (
                _workflow_blob(),
                _workflow_blob(checkout_ref="v5").replace(
                    "actions/checkout@v5",
                    "evil/checkout@v5",
                ),
            )
        },
        {
            ".github/workflows/test.yml": (
                _workflow_blob(),
                _workflow_blob(checkout_ref="v5", fetch_depth=1),
            )
        },
        {"pyproject.toml": (_pyproject_blob(), _pyproject_blob(version="1.0.1"))},
        {
            "pyproject.toml": (
                _pyproject_blob().replace(
                    'version = "1.0.0"',
                    'version = "1.0.0"\nrequires-python = ">=3.11"',
                ),
                _pyproject_blob().replace(
                    'version = "1.0.0"',
                    'version = "1.0.0"\nrequires-python = ">=3.12"',
                ),
            )
        },
        {
            "pyproject.toml": (
                _pyproject_blob(),
                _pyproject_blob(dependency="different-package>=8.4"),
            )
        },
        {
            "pyproject.toml": (
                _pyproject_blob(dependency="pytest[a]>=8"),
                _pyproject_blob(dependency="pytest[b]>=8.4"),
            )
        },
        {
            "pyproject.toml": (
                _pyproject_blob(
                    dependency="hnswlib>=0.8; python_version < '3.13'",
                ),
                _pyproject_blob(
                    dependency="hnswlib>=0.9; python_version < '3.14'",
                ),
            )
        },
        {
            "pyproject.toml": (
                _pyproject_blob(),
                _pyproject_blob().replace(
                    'dependencies = ["example>=1,<3"]',
                    'dependencies = ["example>=1,<3", "new-dependency>=1"]',
                ),
            )
        },
        {
            "pyproject.toml": (
                _pyproject_blob(),
                _pyproject_blob(dependency="example>=2,<4"),
            ),
            "src/ctx/api.py": ("ENABLED = False\n", "ENABLED = True\n"),
        },
    )

    for blobs in cases:
        result = evaluate_policy(
            blobs,
            (),
            {path: "ignored" for path in blobs},
            actor="dependabot[bot]",
            blobs_by_file=blobs,
        )

        assert result.passed is False
        assert result.message == "Dependabot changed files beyond dependency version updates."


def test_no_test_policy_does_not_allow_dependabot_logic_changes_via_other_exemptions() -> None:
    files = ["src/ctx/api.py", "src/tests/test_api.py"]
    diffs = {
        "src/ctx/api.py": "-ENABLED = False\n+ENABLED = True\n",
        "src/tests/test_api.py": "+def test_enabled():\n+    assert True\n",
    }

    result = evaluate_policy(
        files,
        ("no-tests-needed",),
        diffs,
        actor="dependabot[bot]",
    )

    assert result.passed is False
    assert result.test_files == ("src/tests/test_api.py",)


def test_no_test_policy_treats_dependency_policy_as_contract() -> None:
    for path in (
        ".github/codeql/codeql-config.yml",
        ".github/codeql/custom-queries/example.ql",
        ".github/dependabot.yml",
        ".github/pip-audit-ignore.txt",
        ".github/requirements-no-test-policy.txt",
    ):
        result = evaluate_policy([path], (), {path: "+policy change\n"})

        assert result.passed is False
        assert result.contract_files == (path,)


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


def test_classifier_has_no_platform_specific_windows_lane() -> None:
    for path in ("scripts/ctx_ab_benchmark.py", ".github/workflows/test.yml"):
        assert "windows_changed" not in classify_paths([path])


def test_similarity_paths_are_classified() -> None:
    flags = classify_paths(["src/ctx/core/graph/semantic_edges.py"])

    assert flags["similarity_changed"] is True
    assert flags["source_changed"] is True


def test_telemetry_paths_are_classified() -> None:
    for path in (
        "docs/telemetry.md",
        "src/config.json",
        "src/ctx/telemetry/__init__.py",
        "src/ctx/adapters/generic/runtime_lifecycle.py",
        "src/tests/test_enterprise_telemetry.py",
    ):
        flags = classify_paths([path])

        assert flags["telemetry_changed"] is True


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
    assert "telemetry_changed=false" in written
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


def test_ci_required_allows_full_matrix_skip_on_ci_changed_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"ci_changed": "true"},
        },
        test={"result": "skipped"},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}


def test_ci_required_allows_package_skips_when_classifier_says_unchanged() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"package_changed": "false"},
        },
        **{
            "package-build": {"result": "skipped"},
            "package-smoke": {"result": "skipped"},
        },
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}


def test_ci_required_rejects_package_skips_when_classifier_says_changed() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"package_changed": "true"},
        },
        **{
            "package-build": {"result": "skipped"},
            "package-smoke": {"result": "skipped"},
        },
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "package-build": "skipped",
        "package-smoke": "skipped",
    }


def test_ci_required_rejects_package_skips_for_missing_or_malformed_output() -> None:
    for outputs in (
        {},
        {"package_changed": "unknown"},
        {"package_changed": False},
    ):
        needs = _required_needs(
            classify={"result": "success", "outputs": outputs},
            **{
                "package-build": {"result": "skipped"},
                "package-smoke": {"result": "skipped"},
            },
        )

        assert failed_required_jobs(needs, event_name="pull_request") == {
            "package-build": "skipped",
            "package-smoke": "skipped",
        }


def test_ci_required_rejects_package_skips_on_push() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {"package_changed": "false"},
        },
        **{
            "package-build": {"result": "skipped"},
            "package-smoke": {"result": "skipped"},
        },
    )

    assert failed_required_jobs(needs, event_name="push") == {
        "package-build": "skipped",
        "package-smoke": "skipped",
    }


def test_ci_required_allows_heavy_jobs_to_skip_on_docs_only_pr() -> None:
    needs = _required_needs(
        classify={
            "result": "success",
            "outputs": {
                "browser_changed": "false",
                "docs_changed": "true",
                "docs_only": "true",
                "graph_artifact_changed": "false",
                "package_changed": "false",
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
            "browser-security": {"result": "skipped"},
            "test": {"result": "skipped"},
        },
    )

    assert needs["no-test-no-merge"]["result"] == "success"
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
                "package_changed": "false",
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
            "browser-security": {"result": "skipped"},
            "test": {"result": "skipped"},
        },
    )

    assert needs["no-test-no-merge"]["result"] == "success"
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


def test_ci_required_rejects_telemetry_skip_when_classifier_requests_it() -> None:
    needs = _required_needs(
        classify={"result": "success", "outputs": {"telemetry_changed": "true"}},
        **{"telemetry-enterprise": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {
        "telemetry-enterprise": "skipped",
    }


def test_ci_required_allows_telemetry_skip_for_unrelated_pr_only() -> None:
    needs = _required_needs(
        classify={"result": "success", "outputs": {"telemetry_changed": "false"}},
        **{"telemetry-enterprise": {"result": "skipped"}},
    )

    assert failed_required_jobs(needs, event_name="pull_request") == {}
    assert failed_required_jobs(needs, event_name="push") == {
        "telemetry-enterprise": "skipped",
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


def test_workflow_runs_focused_telemetry_enterprise_gate() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "telemetry_changed: ${{ steps.classify.outputs.telemetry_changed }}" in workflow
    assert "telemetry-enterprise:" in workflow
    assert "needs.classify.outputs.telemetry_changed == 'true'" in workflow
    assert "src/tests/test_enterprise_telemetry.py" in workflow
    assert "src/tests/test_harness_cli_run.py" in workflow
    assert '-k "telemetry or runtime_lifecycle"' in workflow


def test_workflow_runs_full_pytest_matrix_after_merge_not_on_prs() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    pytest_job = workflow.split("\n  test:\n", maxsplit=1)[1].split(
        "\n  contract-compat:", maxsplit=1
    )[0]

    assert "needs: classify" in pytest_job
    assert "if: ${{ github.event_name != 'pull_request' }}" in pytest_job
    assert "needs.classify.outputs.ci_changed == 'true'" not in pytest_job


def test_primary_workflow_runs_only_on_supported_posix_hosts() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "ubuntu-latest" in workflow
    assert "macos-latest" in workflow
    assert "windows-latest" not in workflow
    assert "windows-high-risk" not in workflow
    assert "windows_changed" not in workflow
    assert '$RUNNER_OS" == "Windows' not in workflow
