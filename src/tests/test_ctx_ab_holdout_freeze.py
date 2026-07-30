from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any, Callable

import pytest

from scripts import ctx_ab_benchmark as benchmark
from scripts import ctx_ab_exposure_ledger as exposure_ledger
from scripts import ctx_ab_holdout_freeze as freezer


PROTOCOL_ID = "production-graph-holdout-v2"
FROZEN_AT = "2026-07-30T12:34:56+03:00"
ACQUISITION_FROZEN_AT = "2026-07-30T08:00:00Z"
EXPOSURE_SALT = "0" * 64
ORIGIN_URL = "https://github.com/stevesolun/ctx.git"
PINS = {
    "bridge_sha256": "1" * 64,
    "docker_cli_sha256": "2" * 64,
    "docker_daemon_id": "daemon-fixture",
    "docker_package_sha256": "3" * 64,
    "docker_server_version": "29.5.2",
    "namespace": "swebench",
    "python_environment_sha256": "4" * 64,
    "python_sha256": "5" * 64,
    "revision": "6" * 40,
    "run_evaluation_sha256": "7" * 64,
    "schema_version": 1,
}


def _canonical(value: object, *, newline: bool = False) -> bytes:
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return data + (b"\n" if newline else b"")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_committed_v1_protocol_checkout_preserves_authenticated_bytes() -> None:
    relative_path = freezer.V1_PROTOCOL_PATH.relative_to(freezer.ROOT).as_posix()
    attributes = (freezer.ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    data = freezer.V1_PROTOCOL_PATH.read_bytes()

    assert f"{relative_path} text eol=lf" in attributes
    assert b"\r" not in data
    assert _sha256(data) == freezer.V1_PROTOCOL_SHA256
    assert freezer._supported_v1_protocol()["protocol_id"] == "production-graph-holdout-v1"


def _phase(
    *,
    phase: str,
    image_id: str,
) -> dict[str, Any]:
    resolved = phase == "green"
    return {
        "artifact_bytes": 100,
        "artifact_count": 8,
        "artifact_manifest_sha256": ("8" if phase == "red" else "9") * 64,
        "container_policy_count": 1,
        "exact_selector_identity": True,
        "fail_to_pass_count": 1,
        "image_id": image_id,
        "pass_to_pass_count": 1,
        "phase": phase,
        "runtime_identity_sha256": "a" * 64,
        "status_counts": {"PASSED": 2} if resolved else {"FAILED": 1, "PASSED": 1},
        "verifier_evidence_sha256": ("b" if phase == "red" else "c") * 64,
    }


def _fixture_documents(
    *,
    source_commit: str | None = None,
    source_tree_sha1: str | None = None,
    source_bundle_sha256: str | None = None,
) -> dict[str, dict[str, Any]]:
    scenario_ids = [f"owner__repo-{index}-task" for index in range(10)]
    repository_map = {
        scenario_id: f"https://github.com/owner/repo-{index}.git"
        for index, scenario_id in enumerate(scenario_ids)
    }
    v1 = json.loads(freezer.V1_PROTOCOL_PATH.read_bytes())
    exposure_document = {
        "instance_id_hmac_sha256": [
            exposure_ledger.instance_id_hmac_sha256(
                EXPOSURE_SALT,
                "historical-control-task",
            )
        ],
        "salt": EXPOSURE_SALT,
        "schema_version": 1,
    }
    exposure_sha256 = _sha256(exposure_ledger.canonical_ledger_bytes(exposure_document))
    product_inputs = {
        "benchmark_script_sha256": _sha256(Path(benchmark.__file__).read_bytes()),
        "catalog_archive_sha256": _sha256(benchmark.PRODUCTION_CATALOG_ARCHIVE.read_bytes()),
        "codex_binary_sha256": "d" * 64,
        "origin_main_revision": "e" * 40,
        "origin_url": ORIGIN_URL,
        "provider_config_sha256": benchmark.codex_provider_config_sha256("openai"),
        "revision": "e" * 40,
        "runtime_availability_sha256": _sha256(
            benchmark.PRODUCTION_RUNTIME_AVAILABILITY.read_bytes()
        ),
    }
    protocol = freezer.build_acquisition_protocol(
        v1=v1,
        frozen_at=ACQUISITION_FROZEN_AT,
        acquisition_frozen_at=ACQUISITION_FROZEN_AT,
        product_inputs=product_inputs,
        verifier_pins=PINS,
        exposure_ledger_sha256=exposure_sha256,
    )
    selection = {
        "protocol_id": PROTOCOL_ID,
        "analysis_instance_ids": scenario_ids,
        "analysis_repository_map": repository_map,
        "canary_instance_id": None,
        "canary_repository": None,
    }
    scenarios: list[dict[str, Any]] = []
    for index, scenario_id in enumerate(scenario_ids):
        allowed_changes = [f"src/feature_{index}.py"]
        commit = source_commit or f"{index:x}" * 40
        fail_to_pass = [f"tests/test_{index}.py::test_{index}"]
        pass_to_pass = [f"tests/test_{index}.py"]
        image_id = "sha256:" + hashlib.sha256(scenario_id.encode()).hexdigest()
        scenarios.append(
            {
                "allowed_changes": allowed_changes,
                "benchmark_class": "historical",
                "commit": commit,
                "ctx_context": [],
                "expected_test_count": 1,
                "id": scenario_id,
                "language": "python",
                "official_verifier_binding": {
                    "allowed_paths_sha256": _sha256(_canonical(allowed_changes)),
                    "base_commit": commit,
                    "bridge_sha256": PINS["bridge_sha256"],
                    "dataset_row_sha256": _sha256(scenario_id.encode()),
                    "dataset_sha256": protocol["universe"]["selection_jsonl_sha256"],
                    "docker_cli_sha256": PINS["docker_cli_sha256"],
                    "docker_daemon_id_sha256": _sha256(str(PINS["docker_daemon_id"]).encode()),
                    "docker_package_sha256": PINS["docker_package_sha256"],
                    "docker_server_version": PINS["docker_server_version"],
                    "fail_to_pass_sha256": _sha256(_canonical(fail_to_pass)),
                    "harness_revision": PINS["revision"],
                    "harness_source_sha256": "f" * 64,
                    "image_content_digest": image_id,
                    "pass_to_pass_sha256": _sha256(_canonical(pass_to_pass)),
                    "python_environment_sha256": PINS["python_environment_sha256"],
                    "python_sha256": PINS["python_sha256"],
                    "repository_tree_sha1": source_tree_sha1 or "f" * 40,
                    "repository_url": repository_map[scenario_id],
                    "run_evaluation_sha256": PINS["run_evaluation_sha256"],
                    "runtime_identity_sha256": "a" * 64,
                    "schema_version": 1,
                },
                "query": f"Implement feature {index}",
                "red_failure_contains": "FAILED",
                "reference_patch": (
                    f"diff --git a/src/feature_{index}.py b/src/feature_{index}.py\n"
                    f"--- a/src/feature_{index}.py\n"
                    f"+++ b/src/feature_{index}.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
                "regression_verify": [["{python}", "-m", "pytest", "-q", *pass_to_pass]],
                "repo_url": repository_map[scenario_id],
                "reconstructed_test_sha256": hashlib.sha256(
                    f"def test_{index}():\n    assert True\n".encode()
                ).hexdigest(),
                "task": f"Implement the deterministic feature number {index}.",
                "test_body": f"def test_{index}():\n    assert True\n",
                "test_path": f"tests/test_{index}.py",
                "verify": ["{python}", "-m", "pytest", "-q", *fail_to_pass],
            }
        )
    scenario_pack = {"scenarios": scenarios, "version": 1}
    source_map = {
        "repositories": {
            row["repo_url"]: {
                "base_commit": row["commit"],
                "bundle_path": f"bundles/repo-{index}.bundle",
                "bundle_sha256": source_bundle_sha256 or _sha256(f"bundle-{index}\n".encode()),
                "tree_sha1": row["official_verifier_binding"]["repository_tree_sha1"],
            }
            for index, row in enumerate(scenarios)
        },
        "schema_version": 1,
    }
    selection_sha256 = _sha256(_canonical(selection))
    scenario_pack_sha256 = _sha256(_canonical(scenario_pack))
    collision = {
        "catalog_archive_sha256": protocol["product_inputs"]["catalog_archive_sha256"],
        "collision_count": 0,
        "collision_free": True,
        "guard": "runtime-pack-distinctive-evidence-v1",
        "runtime_availability_sha256": protocol["product_inputs"]["runtime_availability_sha256"],
        "scenario_ids": sorted(scenario_ids),
        "scenarios_sha256": scenario_pack_sha256,
    }
    reconstructed = {
        "guard": "reconstructed-test-dependency-v1",
        "module_sha256": {row["id"]: row["reconstructed_test_sha256"] for row in scenarios},
        "selection_sha256": selection_sha256,
    }
    controls: dict[str, Any] = {
        "all_scenarios_passed": True,
        "guard": "holdout-control-results-v1",
        "scenario_count": 10,
        "scenario_pack_sha256": scenario_pack_sha256,
        "scenario_results": {},
        "selection_sha256": selection_sha256,
        "verifier_pins_sha256": _sha256(_canonical(PINS)),
    }
    for index, row in enumerate(scenarios):
        scenario_id = str(row["id"])
        image_id = "sha256:" + hashlib.sha256(scenario_id.encode()).hexdigest()
        controls["scenario_results"][scenario_id] = {
            "changed_test_module_green": True,
            "elapsed_seconds": 10.0 + index,
            "green_evidence_sha256": "c" * 64,
            "module_evidence_sha256": "9" * 64,
            "official_swebench": {
                "green": _phase(phase="green", image_id=image_id),
                "image_id": image_id,
                "pins_sha256": _sha256(_canonical(PINS)),
                "red": _phase(phase="red", image_id=image_id),
            },
            "parent_with_test_patch_red": True,
            "reconstructed_test_sha256": row["reconstructed_test_sha256"],
            "red_evidence_sha256": "b" * 64,
            "reference_patch_green": True,
            "timeout_compliant": True,
            "timeout_seconds": 900,
        }
    environment = {
        "codex": {"version": "1.2.3"},
        "evaluator": {
            "backend": benchmark.OFFICIAL_HOLDOUT_BACKEND,
            "pins_sha256": _sha256(_canonical(PINS)),
        },
        "limits": {
            "agent_timeout_seconds": 420,
            "arms": ["baseline", "ctx-light"],
            "measured_concurrency": 1,
            "pair_count": 30,
            "retries": 0,
            "sandbox_contract": benchmark.OFFICIAL_SANDBOX_CONTRACT,
            "task_count": 10,
            "trials_per_scenario": 3,
        },
        "model": "gpt-5.5",
        "product_revision": protocol["product_inputs"]["revision"],
        "protocol_id": PROTOCOL_ID,
        "provider": "openai",
        "python": {
            "dependencies_sha256": "2" * 64,
            "executable_sha256": "1" * 64,
            "version": "3.12.11",
        },
        "schema_version": 1,
    }
    return {
        "protocol": protocol,
        "exposure_ledger": exposure_document,
        "selection": selection,
        "scenario_pack": scenario_pack,
        "source_map": source_map,
        "collision": collision,
        "reconstructed": reconstructed,
        "controls": controls,
        "environment": environment,
    }


def _write_json(
    path: Path,
    value: object,
    *,
    canonical: bool = True,
    newline: bool = False,
) -> Path:
    path.write_bytes(
        _canonical(value, newline=newline)
        if canonical
        else (json.dumps(value, indent=2) + "\n").encode()
    )
    path.chmod(0o600)
    return path


def _fixture_paths(
    tmp_path: Path,
    *,
    mutate: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Path]]:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    bundles = private / "bundles"
    bundles.mkdir(mode=0o700)
    source_repository = tmp_path / "source-repository"
    source_repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source_repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ctx@example.test"],
        cwd=source_repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ctx benchmark"],
        cwd=source_repository,
        check=True,
    )
    (source_repository / "source.py").write_text("value = 'base'\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=source_repository, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=source_repository, check=True)
    subprocess.run(["git", "branch", "base", "HEAD"], cwd=source_repository, check=True)
    source_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source_tree_sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=source_repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    first_bundle = bundles / "repo-0.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(first_bundle), "refs/heads/base"],
        cwd=source_repository,
        check=True,
    )
    source_bundle_sha256 = _sha256(first_bundle.read_bytes())
    for index in range(10):
        bundle = bundles / f"repo-{index}.bundle"
        if index:
            shutil.copyfile(first_bundle, bundle)
        bundle.chmod(0o600)
    documents = _fixture_documents(
        source_commit=source_commit,
        source_tree_sha1=source_tree_sha1,
        source_bundle_sha256=source_bundle_sha256,
    )
    if mutate is not None:
        mutate(documents)
    paths = {
        "protocol_path": _write_json(
            tmp_path / "protocol.json",
            documents["protocol"],
            newline=True,
        ),
        "exposure_ledger_path": _write_json(
            private / "exposure-ledger.json",
            documents["exposure_ledger"],
        ),
        "selection_path": _write_json(
            private / "selection.json",
            documents["selection"],
        ),
        "scenario_pack_path": _write_json(
            private / "scenario-pack.json",
            documents["scenario_pack"],
        ),
        "source_map_path": _write_json(
            private / "source-map.json",
            documents["source_map"],
        ),
        "collision_path": _write_json(
            private / "collision-attestation.json",
            documents["collision"],
        ),
        "reconstructed_path": _write_json(
            private / "reconstructed-test-attestation.json",
            documents["reconstructed"],
        ),
        "controls_path": _write_json(
            private / "control-results.json",
            documents["controls"],
        ),
        "environment_path": _write_json(
            private / "environment.json",
            documents["environment"],
        ),
        "schedule_path": private / "execution-schedule.json",
        "output_path": tmp_path / "execution-protocol.json",
    }
    return documents, paths


def _freeze(
    paths: dict[str, Path],
    *,
    expected_acquisition_protocol_sha256: str | None = None,
) -> dict[str, Any]:
    expected = expected_acquisition_protocol_sha256 or _sha256(paths["protocol_path"].read_bytes())
    return freezer.freeze_protocol(
        **paths,
        frozen_at=FROZEN_AT,
        expected_acquisition_protocol_sha256=expected,
    )


def test_schedule_is_deterministic_complete_balanced_and_runner_compatible() -> None:
    documents = _fixture_documents()
    assert documents["protocol"]["execution_inputs"]["acquisition_protocol_sha256"] is None
    assert documents["protocol"]["execution_inputs"]["source_map_sha256"] is None
    assert documents["protocol"]["exposure_ledger_sha256"] == _sha256(
        exposure_ledger.canonical_ledger_bytes(documents["exposure_ledger"])
    )
    assert documents["protocol"]["product_inputs"]["origin_url"] == ORIGIN_URL
    assert (
        documents["protocol"]["product_inputs"]["origin_main_revision"]
        == documents["protocol"]["product_inputs"]["revision"]
    )
    assert freezer.validate_acquisition_protocol(documents["protocol"]) == PINS
    rebuilt = freezer.build_acquisition_protocol(
        v1=freezer._supported_v1_protocol(),
        frozen_at=ACQUISITION_FROZEN_AT,
        acquisition_frozen_at=ACQUISITION_FROZEN_AT,
        product_inputs=documents["protocol"]["product_inputs"],
        verifier_pins=PINS,
        exposure_ledger_sha256=documents["protocol"]["exposure_ledger_sha256"],
    )
    assert _canonical(rebuilt, newline=True) == _canonical(
        documents["protocol"],
        newline=True,
    )

    first = freezer.build_execution_schedule(
        documents["selection"],
        documents["protocol"],
    )
    second = freezer.build_execution_schedule(
        deepcopy(documents["selection"]),
        deepcopy(documents["protocol"]),
    )

    assert first == second
    assert set(first) == {
        "assignment_count",
        "assignments",
        "baseline_first_count",
        "ctx_light_first_count",
        "protocol_id",
        "schema_version",
        "trials_per_scenario",
    }
    assert len(first["assignments"]) == 30
    assert len({(row["scenario"], row["trial"]) for row in first["assignments"]}) == 30
    assert sum(row["arms"][0] == "baseline" for row in first["assignments"]) == 15
    assert sum(row["arms"][0] == "ctx-light" for row in first["assignments"]) == 15
    schedule_bytes = _canonical(first, newline=True)
    validated = benchmark.validate_frozen_schedule(
        schedule_bytes,
        expected_sha256=_sha256(schedule_bytes),
        protocol_id=PROTOCOL_ID,
        scenario_ids=documents["selection"]["analysis_instance_ids"],
    )
    assert len(validated.assignments) == 30


def test_freeze_binds_all_inputs_and_emits_runner_ready_private_outputs(
    tmp_path: Path,
) -> None:
    documents, paths = _fixture_paths(tmp_path)

    hashes = _freeze(paths)

    schedule_bytes = paths["schedule_path"].read_bytes()
    frozen = json.loads(paths["output_path"].read_bytes())
    if os.name != "nt":
        assert stat.S_IMODE(paths["schedule_path"].stat().st_mode) == 0o600
        assert stat.S_IMODE(paths["output_path"].stat().st_mode) == 0o644
    assert frozen["stage"] == "execution-frozen"
    assert frozen["execution_frozen_at"] == "2026-07-30T09:34:56Z"
    assert frozen["exposure_ledger_sha256"] == documents["protocol"]["exposure_ledger_sha256"]
    assert frozen["execution_inputs"] == hashes
    assert hashes["acquisition_protocol_sha256"] == _sha256(paths["protocol_path"].read_bytes())
    assert hashes["selection_output_sha256"] == _sha256(paths["selection_path"].read_bytes())
    assert hashes["scenario_pack_sha256"] == _sha256(paths["scenario_pack_path"].read_bytes())
    assert hashes["source_map_sha256"] == _sha256(paths["source_map_path"].read_bytes())
    assert hashes["control_results_sha256"] == _sha256(paths["controls_path"].read_bytes())
    assert hashes["execution_schedule_sha256"] == _sha256(schedule_bytes)
    assert hashes["execution_environment_sha256"] == _sha256(paths["environment_path"].read_bytes())
    loaded = benchmark.load_execution_frozen_holdout(
        protocol_path=paths["output_path"],
        expected_protocol_sha256=_sha256(paths["output_path"].read_bytes()),
        selection_path=paths["selection_path"],
        scenario_pack_path=paths["scenario_pack_path"],
        collision_path=paths["collision_path"],
        reconstructed_path=paths["reconstructed_path"],
        control_results_path=paths["controls_path"],
        environment_path=paths["environment_path"],
        schedule_path=paths["schedule_path"],
        source_map_path=paths["source_map_path"],
    )
    assert loaded.execution_conditions == documents["environment"]
    assert len(loaded.schedule.assignments) == 30


Mutation = Callable[[dict[str, dict[str, Any]]], None]


def _set(document: str, *path: str, value: object) -> Mutation:
    def mutate(documents: dict[str, dict[str, Any]]) -> None:
        target: Any = documents[document]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


@pytest.mark.parametrize(
    ("label", "mutate"),
    [
        ("protocol-schema", _set("protocol", "schema_version", value=1)),
        ("protocol-id", _set("protocol", "protocol_id", value="other")),
        ("protocol-generation", _set("protocol", "protocol_generation", value=2)),
        (
            "protocol-generation-and-seed",
            lambda docs: docs["protocol"].update(
                protocol_generation=2,
                selection_seed=_sha256(
                    freezer.SEED_PREFIX
                    + b"2\0"
                    + str(docs["protocol"]["universe"]["revision"]).encode("ascii")
                ),
            ),
        ),
        ("protocol-stage", _set("protocol", "stage", value="execution-frozen")),
        (
            "protocol-exposure-ledger",
            _set("protocol", "exposure_ledger_sha256", value="not-a-sha256"),
        ),
        (
            "protocol-missing-exposure-ledger",
            lambda docs: docs["protocol"].pop("exposure_ledger_sha256"),
        ),
        ("protocol-seed", _set("protocol", "selection_seed", value="0" * 64)),
        (
            "protocol-candidate-partition-seed",
            _set("protocol", "candidate_partition_seed", value="0" * 64),
        ),
        (
            "protocol-candidate-partition-seed-input",
            _set("protocol", "candidate_partition_seed_input", value="other"),
        ),
        (
            "protocol-candidate-slot",
            lambda docs: docs["protocol"]["selection"].update(candidate_slot=1),
        ),
        (
            "protocol-ranking-order",
            lambda docs: docs["protocol"]["ranking"].update(repository_order="reverse"),
        ),
        (
            "protocol-selection-design",
            lambda docs: docs["protocol"]["selection"].update(strategy="best-overall"),
        ),
        (
            "protocol-claim-threshold",
            lambda docs: docs["protocol"]["claim_gates"].update(required_benefiting_repositories=8),
        ),
        (
            "protocol-claim-numeric-type",
            lambda docs: docs["protocol"]["claim_gates"].update(paired_trials_per_scenario=3.0),
        ),
        (
            "protocol-frozen-list",
            lambda docs: docs["protocol"]["excluded_repositories"].pop(),
        ),
        (
            "protocol-frozen-policy",
            lambda docs: docs["protocol"]["analysis"].update(
                claim_scope="all software-development tasks"
            ),
        ),
        (
            "protocol-extra-input",
            lambda docs: docs["protocol"]["execution_inputs"].update(extra=None),
        ),
        (
            "protocol-missing-input",
            lambda docs: docs["protocol"]["execution_inputs"].pop("execution_environment_sha256"),
        ),
        (
            "protocol-missing-source-map-input",
            lambda docs: docs["protocol"]["execution_inputs"].pop("source_map_sha256"),
        ),
        (
            "protocol-missing-acquisition-input",
            lambda docs: docs["protocol"]["execution_inputs"].pop("acquisition_protocol_sha256"),
        ),
        (
            "protocol-stale-input",
            lambda docs: docs["protocol"]["execution_inputs"].update(
                execution_schedule_sha256="1" * 64
            ),
        ),
        (
            "protocol-stale-acquisition-input",
            lambda docs: docs["protocol"]["execution_inputs"].update(
                acquisition_protocol_sha256="1" * 64
            ),
        ),
        (
            "protocol-timeout",
            lambda docs: docs["protocol"]["timeouts"].update(control_verification_seconds=899),
        ),
        (
            "protocol-missing-codex",
            lambda docs: docs["protocol"]["product_inputs"].pop("codex_binary_sha256"),
        ),
        (
            "protocol-missing-origin",
            lambda docs: docs["protocol"]["product_inputs"].pop("origin_url"),
        ),
        (
            "protocol-origin-main-drift",
            lambda docs: docs["protocol"]["product_inputs"].update(origin_main_revision="0" * 40),
        ),
        (
            "protocol-provider-config",
            lambda docs: docs["protocol"]["product_inputs"].update(provider_config_sha256="0" * 64),
        ),
        (
            "selection-extra",
            lambda docs: docs["selection"].update(extra=True),
        ),
        (
            "selection-missing",
            lambda docs: docs["selection"].pop("canary_repository"),
        ),
        (
            "selection-duplicate",
            lambda docs: docs["selection"]["analysis_instance_ids"].__setitem__(
                1,
                docs["selection"]["analysis_instance_ids"][0],
            ),
        ),
        (
            "scenario-pack-extra",
            lambda docs: docs["scenario_pack"].update(extra=True),
        ),
        (
            "scenario-row-extra",
            lambda docs: docs["scenario_pack"]["scenarios"][0].update(extra=True),
        ),
        (
            "scenario-duplicate",
            lambda docs: docs["scenario_pack"]["scenarios"].__setitem__(
                1,
                deepcopy(docs["scenario_pack"]["scenarios"][0]),
            ),
        ),
        (
            "scenario-missing",
            lambda docs: docs["scenario_pack"]["scenarios"].pop(),
        ),
        (
            "scenario-row-missing",
            lambda docs: docs["scenario_pack"]["scenarios"][0].pop("task"),
        ),
        (
            "scenario-order",
            lambda docs: docs["scenario_pack"]["scenarios"].reverse(),
        ),
        (
            "scenario-repository",
            lambda docs: docs["scenario_pack"]["scenarios"][0].update(
                repo_url="https://github.com/wrong/repo.git"
            ),
        ),
        (
            "scenario-verifier-binding",
            lambda docs: docs["scenario_pack"]["scenarios"][0]["official_verifier_binding"].update(
                image_content_digest="sha256:" + "0" * 64
            ),
        ),
        (
            "collision-extra",
            lambda docs: docs["collision"].update(extra=True),
        ),
        (
            "collision-stale",
            _set("collision", "scenarios_sha256", value="0" * 64),
        ),
        (
            "reconstructed-extra",
            lambda docs: docs["reconstructed"].update(extra=True),
        ),
        (
            "reconstructed-stale",
            lambda docs: docs["reconstructed"]["module_sha256"].update(
                {docs["selection"]["analysis_instance_ids"][0]: "0" * 64}
            ),
        ),
        (
            "controls-extra",
            lambda docs: docs["controls"].update(extra=True),
        ),
        (
            "controls-failed",
            _set("controls", "all_scenarios_passed", value=False),
        ),
        (
            "controls-extra-result",
            lambda docs: docs["controls"]["scenario_results"].update(extra={}),
        ),
        (
            "controls-missing-result",
            lambda docs: docs["controls"]["scenario_results"].pop(
                docs["selection"]["analysis_instance_ids"][0]
            ),
        ),
        (
            "controls-image-drift",
            lambda docs: docs["controls"]["scenario_results"][
                docs["selection"]["analysis_instance_ids"][0]
            ]["official_swebench"].update(image_id="sha256:" + "0" * 64),
        ),
        (
            "controls-red-resolved",
            lambda docs: docs["controls"]["scenario_results"][
                docs["selection"]["analysis_instance_ids"][0]
            ]["official_swebench"]["red"].update(phase="green"),
        ),
        (
            "controls-cross-phase-runtime-drift",
            lambda docs: docs["controls"]["scenario_results"][
                docs["selection"]["analysis_instance_ids"][0]
            ]["official_swebench"]["green"].update(runtime_identity_sha256="0" * 64),
        ),
        (
            "environment-extra",
            lambda docs: docs["environment"].update(extra=True),
        ),
        (
            "environment-missing",
            lambda docs: docs["environment"].pop("provider"),
        ),
        (
            "environment-protocol",
            _set("environment", "protocol_id", value="other"),
        ),
        (
            "environment-trials",
            lambda docs: docs["environment"]["limits"].update(trials_per_scenario=2),
        ),
        (
            "environment-retries",
            lambda docs: docs["environment"]["limits"].update(retries=1),
        ),
        (
            "environment-concurrency",
            lambda docs: docs["environment"]["limits"].update(measured_concurrency=2),
        ),
        (
            "environment-non-openai-provider",
            lambda docs: (
                docs["environment"].update(provider="other"),
                docs["protocol"]["product_inputs"].update(
                    provider_config_sha256=benchmark.codex_provider_config_sha256("other")
                ),
            ),
        ),
    ],
)
def test_freeze_rejects_tampered_missing_duplicate_extra_or_stale_inputs(
    tmp_path: Path,
    label: str,
    mutate: Mutation,
) -> None:
    _, paths = _fixture_paths(tmp_path, mutate=mutate)

    with pytest.raises((freezer.FreezeError, ValueError)):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


@pytest.mark.parametrize("output_name", ["schedule_path", "output_path"])
def test_freeze_rejects_stale_outputs(tmp_path: Path, output_name: str) -> None:
    _, paths = _fixture_paths(tmp_path)
    paths[output_name].write_text("stale", encoding="utf-8")

    with pytest.raises(freezer.FreezeError, match="already exists"):
        _freeze(paths)

    assert paths[output_name].read_text(encoding="utf-8") == "stale"


def test_freeze_authenticates_acquisition_bytes_before_downstream_inputs(
    tmp_path: Path,
) -> None:
    _, paths = _fixture_paths(tmp_path)
    expected = _sha256(paths["protocol_path"].read_bytes())
    paths["protocol_path"].write_bytes(paths["protocol_path"].read_bytes() + b" ")
    paths["selection_path"].unlink()

    with pytest.raises(freezer.FreezeError, match="acquisition protocol identity changed"):
        _freeze(
            paths,
            expected_acquisition_protocol_sha256=expected,
        )

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


def test_freeze_rejects_noncanonical_acquisition_protocol_bytes(
    tmp_path: Path,
) -> None:
    documents, paths = _fixture_paths(tmp_path)
    paths["protocol_path"].write_bytes(
        (json.dumps(documents["protocol"], indent=2) + "\n").encode()
    )

    with pytest.raises(
        freezer.FreezeError,
        match="acquisition protocol must use canonical JSON bytes",
    ):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


def test_freeze_rejects_invalid_acquisition_protocol_digest_shape(
    tmp_path: Path,
) -> None:
    _, paths = _fixture_paths(tmp_path)

    with pytest.raises(freezer.FreezeError, match="SHA-256"):
        _freeze(
            paths,
            expected_acquisition_protocol_sha256="not-a-digest",
        )

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "tampered-bundle",
        "reordered",
        "path-traversal",
        "extra-key",
        "hash-mismatch",
        "base-commit-mismatch",
        "tree-mismatch",
    ],
)
def test_freeze_rejects_untrusted_source_map_or_bundle(
    tmp_path: Path,
    kind: str,
) -> None:
    documents, paths = _fixture_paths(tmp_path)
    source_map = documents["source_map"]
    first = next(iter(source_map["repositories"].values()))
    bundle = paths["source_map_path"].parent / first["bundle_path"]
    if kind == "missing":
        paths["source_map_path"].unlink()
    elif kind == "tampered-bundle":
        bundle.write_bytes(bundle.read_bytes() + b"tampered")
    elif kind == "reordered":
        paths["source_map_path"].write_bytes(
            json.dumps(
                {
                    "schema_version": source_map["schema_version"],
                    "repositories": source_map["repositories"],
                },
                separators=(",", ":"),
            ).encode()
        )
    elif kind == "path-traversal":
        first["bundle_path"] = "../escaped.bundle"
        _write_json(paths["source_map_path"], source_map)
    elif kind == "extra-key":
        first["extra"] = True
        _write_json(paths["source_map_path"], source_map)
    elif kind == "hash-mismatch":
        first["bundle_sha256"] = "0" * 64
        _write_json(paths["source_map_path"], source_map)
    elif kind == "base-commit-mismatch":
        first["base_commit"] = "a" * 40
        _write_json(paths["source_map_path"], source_map)
    else:
        first["tree_sha1"] = "b" * 40
        _write_json(paths["source_map_path"], source_map)
    if paths["source_map_path"].exists():
        paths["source_map_path"].chmod(0o600)

    with pytest.raises(freezer.FreezeError):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "duplicate-path"])
def test_freeze_rejects_source_bundle_aliases(
    tmp_path: Path,
    kind: str,
) -> None:
    documents, paths = _fixture_paths(tmp_path)
    entries = list(documents["source_map"]["repositories"].values())
    bundle = paths["source_map_path"].parent / entries[0]["bundle_path"]
    alias = bundle.with_suffix(".alias")
    if kind == "symlink":
        bundle.rename(alias)
        bundle.symlink_to(alias)
    elif kind == "hardlink":
        alias.hardlink_to(bundle)
    else:
        entries[1]["bundle_path"] = entries[0]["bundle_path"]
        entries[1]["bundle_sha256"] = entries[0]["bundle_sha256"]
        _write_json(paths["source_map_path"], documents["source_map"])

    with pytest.raises(freezer.FreezeError):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


def test_freeze_rejects_selection_intersecting_authenticated_exposure(
    tmp_path: Path,
) -> None:
    documents, paths = _fixture_paths(tmp_path)
    selected_id = str(documents["selection"]["analysis_instance_ids"][0])
    exposure_document = {
        "instance_id_hmac_sha256": [
            exposure_ledger.instance_id_hmac_sha256(EXPOSURE_SALT, selected_id)
        ],
        "salt": EXPOSURE_SALT,
        "schema_version": 1,
    }
    exposure_bytes = exposure_ledger.canonical_ledger_bytes(exposure_document)
    paths["exposure_ledger_path"].write_bytes(exposure_bytes)
    paths["exposure_ledger_path"].chmod(0o600)
    documents["protocol"]["exposure_ledger_sha256"] = _sha256(exposure_bytes)
    _write_json(
        paths["protocol_path"],
        documents["protocol"],
        newline=True,
    )

    with pytest.raises(freezer.FreezeError, match="historical exposure"):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


def test_freeze_rejects_empty_authenticated_exposure_ledger(
    tmp_path: Path,
) -> None:
    documents, paths = _fixture_paths(tmp_path)
    empty_ledger = {
        "instance_id_hmac_sha256": [],
        "salt": EXPOSURE_SALT,
        "schema_version": 1,
    }
    exposure_bytes = exposure_ledger.canonical_ledger_bytes(empty_ledger)
    paths["exposure_ledger_path"].write_bytes(exposure_bytes)
    paths["exposure_ledger_path"].chmod(0o600)
    documents["protocol"]["exposure_ledger_sha256"] = _sha256(exposure_bytes)
    _write_json(paths["protocol_path"], documents["protocol"], newline=True)

    with pytest.raises(freezer.FreezeError, match="must not be empty"):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


def test_freeze_rejects_source_bundle_replaced_after_materialization(
    tmp_path: Path,
) -> None:
    documents, paths = _fixture_paths(tmp_path)
    source_repository = tmp_path / "source-repository"
    (source_repository / "source.py").write_text("value = 'future gold'\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "future gold"], cwd=source_repository, check=True)
    subprocess.run(["git", "branch", "future", "HEAD"], cwd=source_repository, check=True)
    poisoned_bundle = tmp_path / "poisoned.bundle"
    subprocess.run(
        [
            "git",
            "bundle",
            "create",
            str(poisoned_bundle),
            "refs/heads/base",
            "refs/heads/future",
        ],
        cwd=source_repository,
        check=True,
    )
    first = next(iter(documents["source_map"]["repositories"].values()))
    bundle = paths["source_map_path"].parent / first["bundle_path"]
    shutil.copyfile(poisoned_bundle, bundle)
    bundle.chmod(0o600)
    first["bundle_sha256"] = _sha256(bundle.read_bytes())
    _write_json(paths["source_map_path"], documents["source_map"])

    with pytest.raises(freezer.FreezeError, match="base-commit closure"):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


@pytest.mark.parametrize("kind", ["missing", "tampered", "symlink", "hardlink"])
def test_freeze_rejects_missing_tampered_or_aliased_exposure_ledger(
    tmp_path: Path,
    kind: str,
) -> None:
    _, paths = _fixture_paths(tmp_path)
    ledger = paths["exposure_ledger_path"]
    alias = ledger.with_suffix(".alias")
    if kind == "missing":
        ledger.unlink()
    elif kind == "tampered":
        ledger.write_bytes(ledger.read_bytes() + b"tampered")
    elif kind == "symlink":
        ledger.rename(alias)
        ledger.symlink_to(alias)
    else:
        alias.hardlink_to(ledger)

    with pytest.raises(freezer.FreezeError):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


def test_cli_requires_expected_acquisition_protocol_digest(
    tmp_path: Path,
) -> None:
    _, paths = _fixture_paths(tmp_path)
    arguments = [
        "--protocol",
        str(paths["protocol_path"]),
        "--exposure-ledger",
        str(paths["exposure_ledger_path"]),
        "--selection",
        str(paths["selection_path"]),
        "--scenario-pack",
        str(paths["scenario_pack_path"]),
        "--source-map",
        str(paths["source_map_path"]),
        "--collision",
        str(paths["collision_path"]),
        "--reconstructed",
        str(paths["reconstructed_path"]),
        "--controls",
        str(paths["controls_path"]),
        "--environment",
        str(paths["environment_path"]),
        "--schedule",
        str(paths["schedule_path"]),
        "--output",
        str(paths["output_path"]),
    ]

    with pytest.raises(SystemExit) as error:
        freezer.main(arguments)

    assert error.value.code == 2
    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()


def test_freeze_rejects_non_private_symlink_hardlink_and_missing_inputs(
    tmp_path: Path,
) -> None:
    for kind in ("public", "symlink", "hardlink", "missing"):
        case = tmp_path / kind
        case.mkdir()
        _, paths = _fixture_paths(case)
        source = paths["environment_path"]
        if kind == "public":
            if os.name == "nt":
                continue
            source.chmod(0o644)
        elif kind == "symlink":
            target = source.with_suffix(".target")
            source.rename(target)
            source.symlink_to(target)
        elif kind == "hardlink":
            source.with_suffix(".link").hardlink_to(source)
        else:
            source.unlink()

        with pytest.raises(freezer.FreezeError, match="owner-only single-link regular file"):
            _freeze(paths)

        assert not paths["schedule_path"].exists()
        assert not paths["output_path"].exists()


def test_stage_bytes_does_not_require_fchmod_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "schedule.json"
    monkeypatch.setattr(freezer, "_IS_WINDOWS", True)
    monkeypatch.delattr(freezer.os, "fchmod", raising=False)

    staged = freezer._stage_bytes(output, b"{}\n", mode=0o600)

    assert staged.read_bytes() == b"{}\n"


def test_freeze_rejects_invalid_timestamp_and_aliasing_paths(tmp_path: Path) -> None:
    _, paths = _fixture_paths(tmp_path)
    with pytest.raises(freezer.FreezeError, match="timestamp"):
        freezer.freeze_protocol(
            **paths,
            frozen_at="2026-07-30T12:00:00",
            expected_acquisition_protocol_sha256=_sha256(paths["protocol_path"].read_bytes()),
        )

    paths["output_path"] = paths["schedule_path"]
    with pytest.raises(freezer.FreezeError, match="distinct"):
        _freeze(paths)


def test_freeze_rejects_noncanonical_duplicate_and_nonfinite_json(
    tmp_path: Path,
) -> None:
    for kind in ("noncanonical", "duplicate", "nonfinite"):
        case = tmp_path / kind
        case.mkdir()
        documents, paths = _fixture_paths(case)
        if kind == "noncanonical":
            paths["selection_path"].write_text(
                json.dumps(documents["selection"], indent=2) + "\n",
                encoding="utf-8",
            )
            expected = "canonical"
        elif kind == "duplicate":
            data = paths["environment_path"].read_bytes()
            paths["environment_path"].write_bytes(data.replace(b"{", b'{"schema_version":1,', 1))
            expected = "duplicate"
        else:
            data = paths["environment_path"].read_bytes()
            paths["environment_path"].write_bytes(
                data.replace(b'"model":"gpt-5.5"', b'"model":NaN')
            )
            expected = "non-finite"
        paths["selection_path"].chmod(0o600)
        paths["environment_path"].chmod(0o600)

        with pytest.raises(freezer.FreezeError, match=expected):
            _freeze(paths)

        assert not paths["schedule_path"].exists()
        assert not paths["output_path"].exists()


def test_output_install_failure_rolls_back_private_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, paths = _fixture_paths(tmp_path)
    real_link = freezer.os.link
    calls = 0

    def fail_second_link(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic public output failure")
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(freezer.os, "link", fail_second_link)

    with pytest.raises(OSError, match="synthetic"):
        _freeze(paths)

    assert not paths["schedule_path"].exists()
    assert not paths["output_path"].exists()
