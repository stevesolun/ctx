from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

from scripts import ci_dependency_audit
from scripts.ci_classifier import classify_paths
from scripts.ci_no_test_policy import evaluate_policy


CODEQL_WORKFLOW = Path(".github/workflows/codeql.yml")
CODEQL_CONFIG = Path(".github/codeql/codeql-config.yml")
DEPENDENCY_WORKFLOW = Path(".github/workflows/dependency-audit.yml")
XDIST_WORKFLOW = Path(".github/workflows/xdist-experiment.yml")
HELPER_PATH = Path("scripts/ci_dependency_audit.py")
TEST_PATH = "src/tests/test_enterprise_security_workflows.py"


def _load_yaml(path: Path) -> dict[Any, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def _step(workflow: dict[Any, Any], job: str, name: str) -> dict[str, Any]:
    steps = workflow["jobs"][job]["steps"]
    return next(step for step in steps if step.get("name") == name)


def _write_manifest(
    path: Path,
    *,
    dependencies: list[str],
    optional: dict[str, list[str]],
) -> None:
    lines = [
        "[project]",
        f"dependencies = {json.dumps(dependencies)}",
        "",
        "[project.optional-dependencies]",
    ]
    lines.extend(f"{name} = {json.dumps(requirements)}" for name, requirements in optional.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_codeql_workflow_has_expected_triggers_and_permissions() -> None:
    workflow = _load_yaml(CODEQL_WORKFLOW)
    triggers = _triggers(workflow)

    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]
    assert triggers["schedule"] == [{"cron": "23 4 * * 2"}]
    assert "workflow_dispatch" in triggers
    assert workflow["permissions"] == {
        "contents": "read",
        "security-events": "write",
    }


def test_codeql_uses_current_actions_and_external_config() -> None:
    workflow = _load_yaml(CODEQL_WORKFLOW)
    steps = workflow["jobs"]["analyze"]["steps"]

    assert [step["uses"] for step in steps] == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "github/codeql-action/init@e4fba868fa4b1b91e1fdab776edc8cfbe6e9fb81",
        "github/codeql-action/analyze@e4fba868fa4b1b91e1fdab776edc8cfbe6e9fb81",
    ]
    checkout = _step(workflow, "analyze", "Checkout")
    initialize = _step(workflow, "analyze", "Initialize CodeQL")
    assert checkout["with"]["persist-credentials"] is False
    assert initialize["with"] == {
        "languages": "python",
        "config-file": "./.github/codeql/codeql-config.yml",
    }


def test_codeql_config_excludes_non_product_and_fixture_trees() -> None:
    config = _load_yaml(CODEQL_CONFIG)

    assert config["queries"] == [{"uses": "security-extended"}]
    assert config["paths"] == ["src", "scripts", "hooks"]
    assert set(config["paths-ignore"]) == {
        "imported-skills/**",
        "graph/**",
        "docs/**",
        "**/fixtures/**",
    }


@pytest.mark.parametrize(
    ("path", "job", "group"),
    [
        (
            CODEQL_WORKFLOW,
            "analyze",
            "codeql-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}",
        ),
        (
            DEPENDENCY_WORKFLOW,
            "pip-audit",
            "dependency-audit-${{ github.workflow }}-"
            "${{ github.event.pull_request.number || github.ref }}",
        ),
    ],
)
def test_security_workflows_are_bounded_and_cancel_stale_runs(
    path: Path,
    job: str,
    group: str,
) -> None:
    workflow = _load_yaml(path)

    assert workflow["jobs"][job]["timeout-minutes"] == 20
    assert workflow["concurrency"] == {
        "group": group,
        "cancel-in-progress": True,
    }


def test_dependency_audit_is_weekly_and_manifest_triggered() -> None:
    workflow = _load_yaml(DEPENDENCY_WORKFLOW)
    triggers = _triggers(workflow)
    expected_paths = {
        "pyproject.toml",
        ".github/pip-audit-ignore.txt",
        ".github/workflows/dependency-audit.yml",
        "scripts/ci_dependency_audit.py",
    }

    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]
    assert set(triggers["push"]["paths"]) == expected_paths
    assert set(triggers["pull_request"]["paths"]) == expected_paths
    assert triggers["schedule"] == [{"cron": "41 4 * * 2"}]
    assert set(triggers) == {"push", "pull_request", "schedule"}


def test_dependency_audit_has_no_secret_or_write_permissions() -> None:
    workflow = _load_yaml(DEPENDENCY_WORKFLOW)
    text = DEPENDENCY_WORKFLOW.read_text(encoding="utf-8").lower()

    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "id-token" not in text
    assert "snyk" not in text
    assert _step(workflow, "pip-audit", "Checkout")["with"]["persist-credentials"] is False


def test_dependency_workflow_uses_current_actions_and_helper() -> None:
    workflow = _load_yaml(DEPENDENCY_WORKFLOW)
    steps = workflow["jobs"]["pip-audit"]["steps"]
    audit = _step(workflow, "pip-audit", "Audit installable runtime dependencies")
    command = audit["run"]

    assert [step["uses"] for step in steps if "uses" in step] == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
    ]
    setup = _step(
        workflow,
        "pip-audit",
        "Set up Python ${{ matrix.python-version }}",
    )
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"
    assert _step(workflow, "pip-audit", "Install pip-audit")["run"] == (
        'python -m pip install "pip-audit==2.10.1"'
    )
    assert "python scripts/ci_dependency_audit.py" in command
    assert "--manifest pyproject.toml" in command
    assert '--requirements-output "$RUNNER_TEMP/ctx-runtime-requirements.txt"' in command
    assert "--ignore-file .github/pip-audit-ignore.txt" in command
    assert "eval " not in command
    assert "source " not in command


def test_dependency_audit_matrix_covers_supported_os_python_surface() -> None:
    workflow = _load_yaml(DEPENDENCY_WORKFLOW)
    job = workflow["jobs"]["pip-audit"]
    matrix = job["strategy"]["matrix"]

    assert job["name"] == "pip-audit (${{ matrix.os }} / py${{ matrix.python-version }})"
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["strategy"]["fail-fast"] is False
    assert matrix == {
        "os": ["ubuntu-latest", "macos-latest"],
        "python-version": ["3.11", "3.12"],
    }
    assert {
        (operating_system, python_version)
        for operating_system in matrix["os"]
        for python_version in matrix["python-version"]
    } == {
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("macos-latest", "3.11"),
        ("macos-latest", "3.12"),
    }


def test_xdist_experiment_matrix_covers_supported_posix_hosts() -> None:
    workflow = _load_yaml(XDIST_WORKFLOW)
    matrix = workflow["jobs"]["xdist"]["strategy"]["matrix"]

    assert matrix == {
        "os": ["ubuntu-latest", "macos-latest"],
        "python-version": ["3.12"],
    }


def test_runtime_requirements_include_future_non_dev_extras(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    _write_manifest(
        manifest,
        dependencies=["base-package>=1"],
        optional={
            "dev": ["pytest>=8"],
            "alpha": ["alpha-package>=2"],
            "future": ["future-package; python_version >= '3.11'"],
        },
    )

    requirements = ci_dependency_audit.collect_runtime_requirements(manifest)

    assert requirements == (
        "base-package>=1",
        "alpha-package>=2",
        "future-package; python_version >= '3.11'",
    )


@pytest.mark.parametrize(
    "bad_requirement",
    [
        "--extra-index-url https://attacker.invalid/simple",
        "-r injected-requirements.txt",
        "safe-package>=1\n--index-url https://attacker.invalid/simple",
        "not a valid requirement ???",
    ],
)
def test_runtime_requirement_parser_rejects_malformed_or_injected_input(
    tmp_path: Path,
    bad_requirement: str,
) -> None:
    manifest = tmp_path / "pyproject.toml"
    _write_manifest(manifest, dependencies=[bad_requirement], optional={})

    with pytest.raises(ci_dependency_audit.AuditInputError):
        ci_dependency_audit.collect_runtime_requirements(manifest)


@pytest.mark.parametrize(
    "direct_reference",
    [
        "demo @ https://example.invalid/demo.whl",
        "demo @ file:///tmp/demo.whl",
        "demo @ git+https://github.com/example/demo.git@main",
    ],
)
def test_runtime_requirement_parser_rejects_direct_references(
    tmp_path: Path,
    direct_reference: str,
) -> None:
    manifest = tmp_path / "pyproject.toml"
    _write_manifest(manifest, dependencies=[direct_reference], optional={})

    with pytest.raises(ci_dependency_audit.AuditInputError, match="direct URL reference"):
        ci_dependency_audit.collect_runtime_requirements(manifest)


def test_ignore_parser_handles_crlf_comments_and_deduplication(tmp_path: Path) -> None:
    ignore_file = tmp_path / "pip-audit-ignore.txt"
    ignore_file.write_bytes(
        b"# reviewed exceptions\r\n"
        b"CVE-2025-12345\r\n"
        b"GHSA-2c3f-4g5h-6j7m # temporary\r\n"
        b"PYSEC-2025-42\r\n"
        b"CVE-2025-12345\r\n"
    )

    assert ci_dependency_audit.parse_ignore_file(ignore_file) == (
        "CVE-2025-12345",
        "GHSA-2c3f-4g5h-6j7m",
        "PYSEC-2025-42",
    )


@pytest.mark.parametrize(
    "payload",
    [
        "CVE-2025-1234 --ignore-vuln GHSA-2c3f-4g5h-6j7m",
        "$(touch /tmp/ctx-audit-injection)",
        "GHSA-zzzz-zzzz-zzzz",
        "cve-2025-1234",
    ],
)
def test_ignore_parser_rejects_malformed_or_injected_ids(
    tmp_path: Path,
    payload: str,
) -> None:
    ignore_file = tmp_path / "pip-audit-ignore.txt"
    ignore_file.write_text(f"{payload}\n", encoding="utf-8")

    with pytest.raises(ci_dependency_audit.AuditInputError, match="invalid vulnerability ID"):
        ci_dependency_audit.parse_ignore_file(ignore_file)


def test_prepare_audit_writes_validated_requirements_and_returns_argv(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    requirements_output = tmp_path / "runtime-requirements.txt"
    ignore_file = tmp_path / "pip-audit-ignore.txt"
    _write_manifest(
        manifest,
        dependencies=["base-package>=1"],
        optional={"dev": ["pytest>=8"], "runtime": ["runtime-package>=2"]},
    )
    ignore_file.write_text("CVE-2025-12345\n", encoding="utf-8")

    argv = ci_dependency_audit.prepare_audit(
        manifest,
        requirements_output,
        ignore_file,
    )

    assert requirements_output.read_text(encoding="utf-8") == (
        "base-package>=1\nruntime-package>=2\n"
    )
    assert argv == (
        sys.executable,
        "-m",
        "pip_audit",
        "--strict",
        "--progress-spinner",
        "off",
        "--requirement",
        str(requirements_output),
        "--ignore-vuln",
        "CVE-2025-12345",
    )


def test_main_invokes_subprocess_with_argv_not_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "pyproject.toml"
    requirements_output = tmp_path / "runtime-requirements.txt"
    _write_manifest(manifest, dependencies=["base-package>=1"], optional={})
    captured: dict[str, object] = {}

    def fake_run(
        command: Sequence[str],
        *,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = tuple(command)
        captured["check"] = check
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(ci_dependency_audit.subprocess, "run", fake_run)

    result = ci_dependency_audit.main(
        [
            "--manifest",
            str(manifest),
            "--requirements-output",
            str(requirements_output),
        ]
    )

    assert result == 0
    assert captured["check"] is False
    assert captured["command"] == ci_dependency_audit.build_pip_audit_argv(
        requirements_output,
        (),
    )


def test_security_workflow_changes_fail_open_in_ci_classifier() -> None:
    for path in (CODEQL_WORKFLOW.as_posix(), DEPENDENCY_WORKFLOW.as_posix()):
        flags = classify_paths([path])

        assert flags["ci_changed"] is True
        assert flags["source_changed"] is True
        assert flags["package_changed"] is True
        assert flags["browser_changed"] is True
        assert flags["similarity_changed"] is True
        assert flags["telemetry_changed"] is True


def test_security_workflows_are_no_test_policy_contracts() -> None:
    workflow_paths = (CODEQL_WORKFLOW.as_posix(), DEPENDENCY_WORKFLOW.as_posix())
    for path in workflow_paths:
        without_test = evaluate_policy([path], (), {path: "+name: changed\n"})
        with_test = evaluate_policy(
            [path, TEST_PATH],
            (),
            {path: "+name: changed\n", TEST_PATH: "+def test_workflow():\n"},
        )

        assert without_test.passed is False
        assert without_test.contract_files == (path,)
        assert with_test.passed is True
        assert with_test.contract_files == (path,)
        assert with_test.test_files == (TEST_PATH,)


def test_dependency_helper_is_no_test_policy_contract() -> None:
    helper = HELPER_PATH.as_posix()
    result = evaluate_policy([helper], (), {helper: "+def changed():\n"})

    assert result.passed is False
    assert result.contract_files == (helper,)
