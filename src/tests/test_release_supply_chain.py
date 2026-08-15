from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import tomllib
from typing import Any

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
import pytest
import yaml  # type: ignore[import-untyped]

from scripts.validate_release_sbom import (
    RELEASE_RUNTIME_EXTRAS,
    validate_release_sbom,
)


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "publish.yml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
_VERSION_CANDIDATES = (
    "0.8.1",
    "0.16.5",
    "1.2.3",
    "1.40",
    "1.52",
    "2.1",
    "3.6",
    "5.0",
    "5.3",
    "5.5",
    "6.0",
    "8.1.8",
    "8.3.3",
    "10.0",
    "11.0",
)


def _project() -> dict[str, Any]:
    with PYPROJECT_PATH.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert isinstance(project, dict)
    return project


def _release_requirements() -> list[Requirement]:
    project = _project()
    raw = list(project["dependencies"])
    optional = project["optional-dependencies"]
    for extra in RELEASE_RUNTIME_EXTRAS:
        raw.extend(optional[extra])
    environment = {key: str(value) for key, value in default_environment().items()}
    return [
        requirement
        for requirement in (Requirement(value) for value in raw)
        if requirement.marker is None or requirement.marker.evaluate(environment)
    ]


def _satisfying_version(requirement: Requirement) -> str:
    for raw_version in _VERSION_CANDIDATES:
        version = Version(raw_version)
        if not requirement.specifier or version in requirement.specifier:
            return str(version)
    raise AssertionError(f"test fixture has no version for {requirement}")


def _fixture_data() -> tuple[dict[str, Any], dict[str, Any]]:
    project = _project()
    root_name = canonicalize_name(project["name"])
    root_ref = f"pkg:pypi/{root_name}@{project['version']}"
    leaf_name = "ctx-runtime-leaf"
    leaf_ref = f"pkg:pypi/{leaf_name}@1.0"
    components: list[dict[str, Any]] = [
        {
            "type": "library",
            "name": leaf_name,
            "version": "1.0",
            "purl": leaf_ref,
            "bom-ref": leaf_ref,
        }
    ]
    distributions: list[dict[str, Any]] = [
        {
            "name": project["name"],
            "version": project["version"],
            "requires": [],
        },
        {"name": leaf_name, "version": "1.0", "requires": []},
    ]
    dependencies: list[dict[str, Any]] = [{"ref": leaf_ref, "dependsOn": []}]
    direct_refs: list[str] = []

    for requirement in _release_requirements():
        name = canonicalize_name(requirement.name)
        version = _satisfying_version(requirement)
        ref = f"pkg:pypi/{name}@{version}"
        components.append(
            {
                "type": "library",
                "name": requirement.name,
                "version": version,
                "purl": ref,
                "bom-ref": ref,
            }
        )
        distributions.append(
            {
                "name": requirement.name,
                "version": version,
                "requires": [f"{leaf_name}>=1"],
            }
        )
        dependencies.append({"ref": ref, "dependsOn": [leaf_ref]})
        direct_refs.append(ref)

    dependencies.append({"ref": root_ref, "dependsOn": direct_refs})
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": project["name"],
                "version": project["version"],
                "purl": root_ref,
                "bom-ref": root_ref,
            }
        },
        "components": components,
        "dependencies": dependencies,
    }
    return sbom, {"distributions": distributions}


def _write_json(tmp_path: Path, name: str, value: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _validate(
    tmp_path: Path,
    sbom: dict[str, Any],
    inventory: dict[str, Any],
    *,
    selected_extras: tuple[str, ...] = RELEASE_RUNTIME_EXTRAS,
) -> None:
    validate_release_sbom(
        _write_json(tmp_path, "release.cdx.json", sbom),
        PYPROJECT_PATH,
        _write_json(tmp_path, "resolved-environment.json", inventory),
        selected_extras=selected_extras,
    )


def _workflow() -> dict[str, Any]:
    value = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    steps = job["steps"]
    assert isinstance(steps, list)
    return {str(step["name"]): step for step in steps if isinstance(step, dict) and "name" in step}


def _run_production_guard(
    tmp_path: Path,
    *,
    tag_sha: str,
    main_sha: str,
    expected_sha: str,
    workflow_runs: list[dict[str, Any]],
) -> subprocess.CompletedProcess[str]:
    guard = _steps(_workflow()["jobs"]["build"])[
        "Verify production tag is the exact tested main commit"
    ]["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        """#!/bin/sh
set -eu
if [ "$1" = "fetch" ]; then
  exit 0
fi
if [ "$1" = "rev-parse" ] && [ "$2" = "HEAD^{commit}" ]; then
  printf '%s\\n' "$FAKE_TAG_SHA"
  exit 0
fi
if [ "$1" = "rev-parse" ] && [ "$2" = "refs/remotes/origin/main^{commit}" ]; then
  printf '%s\\n' "$FAKE_MAIN_SHA"
  exit 0
fi
exit 91
""",
        encoding="utf-8",
    )
    git.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/bin/sh
set -eu
expected="api --method GET repos/acme/ctx/actions/workflows/test.yml/runs -f head_sha=$FAKE_TAG_SHA -f branch=main -f event=push -f per_page=100"
if [ "$*" != "$expected" ]; then
  printf 'unexpected gh arguments: %s\\n' "$*" >&2
  exit 92
fi
printf '%s' "$FAKE_WORKFLOW_RUNS"
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "EXPECTED_RELEASE_SHA": expected_sha,
            "FAKE_MAIN_SHA": main_sha,
            "FAKE_TAG_SHA": tag_sha,
            "FAKE_WORKFLOW_RUNS": json.dumps({"workflow_runs": workflow_runs}),
            "GITHUB_REPOSITORY": "acme/ctx",
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
        }
    )
    return subprocess.run(
        ["bash", "-c", guard],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_sbom_validator_accepts_resolved_all_extras_closure(
    tmp_path: Path,
) -> None:
    _validate(tmp_path, *_fixture_data())


def test_release_sbom_validator_rejects_direct_only_inventory(
    tmp_path: Path,
) -> None:
    sbom, inventory = _fixture_data()
    leaf = "ctx-runtime-leaf"
    leaf_ref = "pkg:pypi/ctx-runtime-leaf@1.0"
    sbom["components"] = [
        component for component in sbom["components"] if component["name"] != leaf
    ]
    sbom["dependencies"] = [
        {
            "ref": item["ref"],
            "dependsOn": (
                list(item["dependsOn"])
                if item["ref"] == sbom["metadata"]["component"]["bom-ref"]
                else []
            ),
        }
        for item in sbom["dependencies"]
        if item["ref"] != leaf_ref
    ]

    with pytest.raises(ValueError, match="missing resolved runtime dependency"):
        _validate(tmp_path, sbom, inventory)


def test_release_sbom_validator_rejects_missing_dependency_edge(
    tmp_path: Path,
) -> None:
    sbom, inventory = _fixture_data()
    root_ref = sbom["metadata"]["component"]["bom-ref"]
    root_entry = next(item for item in sbom["dependencies"] if item["ref"] == root_ref)
    root_entry["dependsOn"].pop()

    with pytest.raises(ValueError, match="dependency graph is missing edge"):
        _validate(tmp_path, sbom, inventory)


@pytest.mark.parametrize("tamper", ["name", "version"])
def test_release_sbom_validator_binds_component_purl_identity(
    tmp_path: Path,
    tamper: str,
) -> None:
    sbom, inventory = _fixture_data()
    component = sbom["components"][1]
    if tamper == "name":
        component["purl"] = f"pkg:pypi/unrelated@{component['version']}"
        message = "package URL name does not match"
    else:
        component["purl"] = f"pkg:pypi/{component['name']}@999"
        message = "package URL version does not match"

    with pytest.raises(ValueError, match=message):
        _validate(tmp_path, sbom, inventory)


def test_release_sbom_validator_rejects_version_outside_declared_specifier(
    tmp_path: Path,
) -> None:
    sbom, inventory = _fixture_data()
    component = next(item for item in sbom["components"] if item["name"] == "click")
    component["version"] = "9.0"
    component["purl"] = "pkg:pypi/click@9.0"
    old_ref = component["bom-ref"]
    component["bom-ref"] = component["purl"]
    for item in sbom["dependencies"]:
        if item["ref"] == old_ref:
            item["ref"] = component["bom-ref"]
        item["dependsOn"] = [
            component["bom-ref"] if ref == old_ref else ref for ref in item["dependsOn"]
        ]
    resolved = next(item for item in inventory["distributions"] if item["name"] == "click")
    resolved["version"] = "9.0"

    with pytest.raises(ValueError, match="violates declared requirement"):
        _validate(tmp_path, sbom, inventory)


def test_release_sbom_validator_rejects_omitted_runtime_extra(
    tmp_path: Path,
) -> None:
    sbom, inventory = _fixture_data()

    with pytest.raises(ValueError, match="must cover every supported runtime extra"):
        _validate(
            tmp_path,
            sbom,
            inventory,
            selected_extras=RELEASE_RUNTIME_EXTRAS[:-1],
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"bomFormat": "SPDX"}, "format must be CycloneDX"),
        ({"specVersion": "1.5"}, "specVersion must be 1.6"),
        (
            {
                "metadata": {
                    "component": {
                        "type": "application",
                        "name": "other",
                        "version": "1",
                        "purl": "pkg:pypi/other@1",
                        "bom-ref": "pkg:pypi/other@1",
                    }
                }
            },
            "name does not match",
        ),
    ],
)
def test_release_sbom_validator_rejects_wrong_release_identity(
    tmp_path: Path,
    mutation: dict[str, Any],
    message: str,
) -> None:
    sbom, inventory = _fixture_data()
    for key, value in mutation.items():
        sbom[key] = copy.deepcopy(value)

    with pytest.raises(ValueError, match=message):
        _validate(tmp_path, sbom, inventory)


def test_publish_workflow_resolves_all_extras_before_validated_sbom() -> None:
    build = _workflow()["jobs"]["build"]
    steps = _steps(build)
    names = list(steps)
    install = steps["Smoke install all runtime extras"]["run"]
    generate = steps["Generate and validate CycloneDX runtime SBOM"]["run"]

    assert build["permissions"] == {"actions": "read", "contents": "read"}
    assert "cyclonedx-bom==7.3.0" in steps["Install release tooling"]["run"]
    assert names.index("Smoke install wheel") < names.index("Smoke install all runtime extras")
    assert names.index("Smoke install all runtime extras") < names.index(
        "Generate and validate CycloneDX runtime SBOM"
    )
    assert names.index("Generate and validate CycloneDX runtime SBOM") < names.index(
        "Upload dist artifact"
    )
    assert "${CTX_WHEEL}[ann,browser,embeddings,gcf,harness,viz]" in install
    assert "python -m pip check" in install
    for distribution in (
        "gcf-python",
        "hnswlib",
        "litellm",
        "playwright",
        "plotly",
        "sentence-transformers",
        "torch",
    ):
        assert f'"{distribution}"' in install

    assert "from importlib.metadata import distributions" in generate
    assert "python -m cyclonedx_py environment" in generate
    assert generate.count("python -m cyclonedx_py environment") == 2
    assert "--output-reproducible" in generate
    assert "cmp release-sbom/claude-ctx.cdx.json" in generate
    assert "--inventory release-sbom/resolved-environment.json" in generate
    for extra in RELEASE_RUNTIME_EXTRAS:
        assert f"--extra {extra}" in generate
    assert ".venv-smoke/bin/python" in generate
    assert "scripts/validate_release_sbom.py" in generate


def test_attestation_covers_every_published_artifact_and_blocks_publish() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    jobs = _workflow()["jobs"]
    attest = jobs["attest"]
    attest_steps = _steps(attest)
    publish = jobs["publish"]
    release_assets = jobs["release-assets"]

    assert attest["needs"] == "build"
    assert attest["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["build"]["permissions"] == {"actions": "read", "contents": "read"}
    assert publish["permissions"] == {"contents": "read", "id-token": "write"}

    provenance = attest_steps["Attest release provenance"]
    assert provenance["uses"] == ("actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6")
    for path in (
        "dist/packages/*.whl",
        "dist/packages/*.tar.gz",
        "release-sbom/*.json",
    ):
        assert path in provenance["with"]["subject-path"]

    graph_download = attest_steps["Download graph artifact bundle"]
    graph_attest = attest_steps["Attest graph release provenance"]
    assert graph_download["if"] == "needs.build.outputs.graph_assets_available == 'true'"
    assert graph_download["with"]["name"] == "graph-release-assets"
    assert graph_attest["if"] == "needs.build.outputs.graph_assets_available == 'true'"
    published_graph_paths = {
        "wiki-graph.tar.gz",
        "wiki-graph-runtime.tar.gz",
        "skills-sh-catalog.json.gz",
        "communities.json",
        "entity-overlays.jsonl",
    }
    for name in published_graph_paths:
        assert f"graph-release-assets/{name}" in graph_attest["with"]["subject-path"]
        assert (
            name in _steps(release_assets)["Upload graph assets and SBOM to GitHub release"]["run"]
        )

    sbom = attest_steps["Attest release SBOM"]
    assert sbom["uses"] == "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6"
    assert sbom["with"]["sbom-path"] == "release-sbom/claude-ctx.cdx.json"
    assert "attest" in publish["needs"]
    assert "needs.attest.result == 'success'" in publish["if"]
    assert release_assets["needs"] == ["build", "attest"]
    assert "secrets." not in workflow_text
    assert "snyk" not in workflow_text.lower()


def test_production_publish_requires_exact_main_sha_and_latest_successful_tests(
    tmp_path: Path,
) -> None:
    sha = "a" * 40
    result = _run_production_guard(
        tmp_path,
        tag_sha=sha,
        main_sha=sha,
        expected_sha=sha,
        workflow_runs=[
            {
                "id": 41,
                "run_number": 12,
                "run_attempt": 1,
                "head_sha": sha,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    assert result.returncode == 0, result.stderr
    assert f"release provenance verified: sha={sha} tests_run_id=41" in result.stdout


def test_production_publish_rejects_tag_that_is_not_exact_main_head(tmp_path: Path) -> None:
    result = _run_production_guard(
        tmp_path,
        tag_sha="a" * 40,
        main_sha="b" * 40,
        expected_sha="a" * 40,
        workflow_runs=[],
    )

    assert result.returncode != 0
    assert "is not the exact origin/main head" in result.stderr


def test_production_publish_rejects_checkout_that_differs_from_workflow_sha(
    tmp_path: Path,
) -> None:
    result = _run_production_guard(
        tmp_path,
        tag_sha="a" * 40,
        main_sha="a" * 40,
        expected_sha="b" * 40,
        workflow_runs=[],
    )

    assert result.returncode != 0
    assert "does not match workflow SHA" in result.stderr


def test_production_publish_rejects_latest_failed_test_run_even_after_older_success(
    tmp_path: Path,
) -> None:
    sha = "c" * 40
    common = {
        "head_sha": sha,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
    }
    result = _run_production_guard(
        tmp_path,
        tag_sha=sha,
        main_sha=sha,
        expected_sha=sha,
        workflow_runs=[
            {
                **common,
                "id": 51,
                "run_number": 20,
                "run_attempt": 1,
                "conclusion": "success",
            },
            {
                **common,
                "id": 52,
                "run_number": 21,
                "run_attempt": 1,
                "conclusion": "failure",
            },
        ],
    )

    assert result.returncode != 0
    assert "latest exact-SHA Tests workflow run is not successful" in result.stderr


def test_production_publish_rejects_non_main_or_non_push_test_evidence(tmp_path: Path) -> None:
    sha = "d" * 40
    result = _run_production_guard(
        tmp_path,
        tag_sha=sha,
        main_sha=sha,
        expected_sha=sha,
        workflow_runs=[
            {
                "id": 61,
                "run_number": 22,
                "run_attempt": 1,
                "head_sha": sha,
                "head_branch": "feature",
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    assert result.returncode != 0
    assert "no main-push Tests workflow run exists" in result.stderr


def test_github_release_uses_exact_tagged_changelog_section(tmp_path: Path) -> None:
    release_steps = _steps(_workflow()["jobs"]["release-assets"])
    assert release_steps["Checkout release metadata"]["with"] == {
        "lfs": False,
        "persist-credentials": False,
    }
    extract = release_steps["Extract exact version notes from changelog"]["run"]
    (tmp_path / "CHANGELOG.md").write_text(
        """# Changelog

## [Unreleased]

- Future change.

## [1.2.3] - 2026-08-14

### Added

- Useful shipped behavior.

## [1.2.2] - 2026-08-01

- Previous behavior.
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["TAG_NAME"] = "v1.2.3"

    result = subprocess.run(
        ["bash", "-c", extract],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    notes = (tmp_path / "release-notes.md").read_text(encoding="utf-8")
    assert notes.startswith("## [1.2.3] - 2026-08-14")
    assert "Useful shipped behavior." in notes
    assert "Previous behavior." not in notes
    upload = release_steps["Upload graph assets and SBOM to GitHub release"]["run"]
    assert "--verify-tag" in upload
    assert upload.count("--notes-file release-notes.md") == 2
    assert '--notes "ctx $TAG_NAME"' not in upload


def test_github_release_refuses_missing_version_changelog_section(tmp_path: Path) -> None:
    release_steps = _steps(_workflow()["jobs"]["release-assets"])
    extract = release_steps["Extract exact version notes from changelog"]["run"]
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n- Future change.\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["TAG_NAME"] = "v1.2.3"

    result = subprocess.run(
        ["bash", "-c", extract],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "expected one CHANGELOG.md section for 1.2.3, found 0" in result.stderr


def test_release_keeps_existing_publish_gates_and_exposes_sbom() -> None:
    jobs = _workflow()["jobs"]
    build_steps = _steps(jobs["build"])
    release_steps = _steps(jobs["release-assets"])
    publish_steps = _steps(jobs["publish"])

    required_build_gates = {
        "Validate release target",
        "Reject already published PyPI version",
        "Build distributions",
        "Check distributions",
        "Check distribution contents",
        "Smoke install wheel",
        "Smoke install all runtime extras",
        "Generate and validate CycloneDX runtime SBOM",
    }
    assert required_build_gates <= build_steps.keys()
    assert "Telemetry release smoke" in build_steps["Smoke install wheel"]["run"]
    assert (
        "release-sbom/claude-ctx.cdx.json"
        in release_steps["Upload graph assets and SBOM to GitHub release"]["run"]
    )
    immutable_publish_action = (
        "pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247"
    )
    assert publish_steps["Publish to PyPI"]["uses"] == immutable_publish_action
    assert publish_steps["Publish to TestPyPI"]["uses"] == (immutable_publish_action)
    assert "password" not in json.dumps(publish_steps).lower()
