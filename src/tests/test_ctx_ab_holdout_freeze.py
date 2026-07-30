from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import stat
from typing import Any, Callable

import pytest

from scripts import ctx_ab_benchmark as benchmark
from scripts import ctx_ab_holdout_freeze as freezer


PROTOCOL_ID = "production-graph-holdout-v2"
FROZEN_AT = "2026-07-30T12:34:56+03:00"
ACQUISITION_FROZEN_AT = "2026-07-30T08:00:00Z"
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


def _fixture_documents() -> dict[str, dict[str, Any]]:
    scenario_ids = [f"owner__repo-{index}-task" for index in range(10)]
    repository_map = {
        scenario_id: f"https://github.com/owner/repo-{index}.git"
        for index, scenario_id in enumerate(scenario_ids)
    }
    v1 = json.loads(freezer.V1_PROTOCOL_PATH.read_bytes())
    product_inputs = {
        "benchmark_script_sha256": _sha256(Path(benchmark.__file__).read_bytes()),
        "catalog_archive_sha256": _sha256(benchmark.PRODUCTION_CATALOG_ARCHIVE.read_bytes()),
        "codex_binary_sha256": "d" * 64,
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
        commit = f"{index:x}" * 40
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
                    "repository_tree_sha1": "f" * 40,
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
        "selection": selection,
        "scenario_pack": scenario_pack,
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
    documents = _fixture_documents()
    if mutate is not None:
        mutate(documents)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    paths = {
        "protocol_path": _write_json(
            tmp_path / "protocol.json",
            documents["protocol"],
            newline=True,
        ),
        "selection_path": _write_json(
            private / "selection.json",
            documents["selection"],
        ),
        "scenario_pack_path": _write_json(
            private / "scenario-pack.json",
            documents["scenario_pack"],
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
    assert freezer.validate_acquisition_protocol(documents["protocol"]) == PINS

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
    assert stat.S_IMODE(paths["schedule_path"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["output_path"].stat().st_mode) == 0o644
    assert frozen["stage"] == "execution-frozen"
    assert frozen["execution_frozen_at"] == "2026-07-30T09:34:56Z"
    assert frozen["execution_inputs"] == hashes
    assert hashes["acquisition_protocol_sha256"] == _sha256(paths["protocol_path"].read_bytes())
    assert hashes["selection_output_sha256"] == _sha256(paths["selection_path"].read_bytes())
    assert hashes["scenario_pack_sha256"] == _sha256(paths["scenario_pack_path"].read_bytes())
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


def test_cli_requires_expected_acquisition_protocol_digest(
    tmp_path: Path,
) -> None:
    _, paths = _fixture_paths(tmp_path)
    arguments = [
        "--protocol",
        str(paths["protocol_path"]),
        "--selection",
        str(paths["selection_path"]),
        "--scenario-pack",
        str(paths["scenario_pack_path"]),
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
