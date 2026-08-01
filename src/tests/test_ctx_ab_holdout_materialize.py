from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any

import pytest

from scripts import ctx_ab_exposure_ledger as exposure_ledger
from scripts import ctx_ab_holdout as holdout
from scripts import ctx_ab_holdout_freeze as freezer
from scripts import ctx_ab_holdout_materialize as materializer


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "benchmarks" / "ctx_ab" / "holdout-protocol-v1.json"
REVISION = "1" * 40
RUN_EVALUATION_SHA256 = "2" * 64
BRIDGE_SHA256 = "3" * 64
PYTHON_SHA256 = "4" * 64
PYTHON_ENVIRONMENT_SHA256 = "5" * 64
DOCKER_PACKAGE_SHA256 = "6" * 64
DOCKER_CLI_SHA256 = "7" * 64
DOCKER_DAEMON_ID = "ctx-test-daemon"
DOCKER_SERVER_VERSION = "29.5.2"
HIDDEN_ARTIFACT = "PRIVATE-HIDDEN-EVALUATOR-CONTENT"
EXPOSURE_SALT = "b" * 64
ORIGIN_URL = "https://github.com/stevesolun/ctx.git"


@dataclass
class Fixture:
    archive: Path
    docker_cli: Path
    docker_host: str
    exposure_ledger: Path
    output: Path
    protocol: dict[str, Any]
    protocol_path: Path
    repositories: dict[str, Path]
    rows: list[dict[str, Any]]
    rows_path: Path
    runtime: Path
    selection: dict[str, Any]
    selection_path: Path
    source_map: Path
    swebench_checkout: Path
    swebench_python: Path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _diff(repo: Path, path: Path, changed: str) -> str:
    path.write_text(changed, encoding="utf-8")
    patch = _git(repo, "diff", "--", path.relative_to(repo).as_posix())
    _git(repo, "checkout", "--", path.relative_to(repo).as_posix())
    return patch


def _make_repo(root: Path, index: int) -> tuple[Path, list[dict[str, object]]]:
    repo = root / f"repo-{index}"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ctx@example.invalid")
    _git(repo, "config", "user.name", "CTX Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    for candidate in range(2):
        (repo / "src" / f"feature_{candidate}.py").write_text(
            "def value():\n    return 0\n",
            encoding="utf-8",
        )
        (repo / "tests" / f"test_feature_{candidate}.py").write_text(
            "def test_existing():\n    assert True\n",
            encoding="utf-8",
        )
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD").strip()

    rows: list[dict[str, object]] = []
    for candidate in range(2):
        product = repo / "src" / f"feature_{candidate}.py"
        test = repo / "tests" / f"test_feature_{candidate}.py"
        patch = _diff(
            repo,
            product,
            (
                "def value():\n"
                "    result = 1\n"
                "    result += 0\n"
                "    result *= 1\n"
                "    result -= 0\n"
                "    return result\n"
            ),
        )
        test_patch = _diff(
            repo,
            test,
            (
                f"from feature_{candidate} import value\n\n\n"
                "def test_existing():\n"
                "    assert True\n\n\n"
                "def test_new_behavior():\n"
                "    assert value() == 1\n"
            ),
        )
        test_id = f"tests/test_feature_{candidate}.py"
        rows.append(
            {
                "row_idx": index * 2 + candidate,
                "repo": f"owner/repo-{index}",
                "instance_id": f"private-holdout-{index}-{candidate}",
                "base_commit": commit,
                "patch": patch,
                "test_patch": test_patch,
                "problem_statement": (
                    "PRIVATE-HOLDOUT-TASK implement the deterministic local value behavior "
                    "while preserving existing callers and proving the focused regression "
                    "through the repository test module without external services or keys."
                ),
                "hints_text": "",
                "created_at": "2026-01-01",
                "version": "1",
                "FAIL_TO_PASS": json.dumps([f"{test_id}::test_new_behavior"]),
                "PASS_TO_PASS": json.dumps([f"{test_id}::test_existing"]),
                "environment_setup_commit": commit,
                "difficulty": "easy",
            }
        )
    return repo, rows


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _canonical_acquisition(value: object) -> bytes:
    return _canonical(value) + b"\n"


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    repos = tmp_path / "repos"
    repos.mkdir()
    bundles = tmp_path / "source-bundles"
    bundles.mkdir(mode=0o700)
    rows: list[dict[str, object]] = []
    repositories: dict[str, Path] = {}
    sources: dict[str, Any] = {"repositories": {}, "schema_version": 1}
    for index in range(10):
        repo, repo_rows = _make_repo(repos, index)
        rows.extend(repo_rows)
        canonical_url = holdout.canonical_repo_url(f"owner/repo-{index}")
        bundle = bundles / f"repo-{index}.bundle"
        _git(repo, "branch", "base", "HEAD")
        _git(repo, "bundle", "create", str(bundle), "refs/heads/base")
        bundle.chmod(0o600)
        commit = _git(repo, "rev-parse", "HEAD").strip()
        tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
        repositories[canonical_url] = repo
        sources["repositories"][canonical_url] = {
            "base_commit": commit,
            "bundle_path": bundle.relative_to(tmp_path).as_posix(),
            "bundle_sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
            "tree_sha1": tree,
        }

    rows_bytes = b"".join(_canonical(row) + b"\n" for row in rows)
    rows_path = tmp_path / "rows.jsonl"
    rows_path.write_bytes(rows_bytes)
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_bytes(_canonical({"entities": []}))
    archive_path = tmp_path / "catalog.tar.gz"
    archive_path.write_bytes(b"synthetic-catalog")
    swebench_checkout = tmp_path / "SWE-bench"
    swebench_checkout.mkdir()
    swebench_python = _executable(tmp_path / "swebench-python")
    docker_cli = _executable(tmp_path / "docker")
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
    exposure_path = tmp_path / "exposure-ledger.json"
    exposure_bytes = exposure_ledger.canonical_ledger_bytes(exposure_document)
    exposure_path.write_bytes(exposure_bytes)
    exposure_path.chmod(0o600)

    v1 = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    v1["universe"]["expected_rows"] = len(rows)
    v1["universe"]["selection_jsonl_sha256"] = hashlib.sha256(rows_bytes).hexdigest()
    benchmark_path = Path(str(materializer.benchmark.__file__))
    product_inputs = {
        "benchmark_script_sha256": hashlib.sha256(benchmark_path.read_bytes()).hexdigest(),
        "catalog_archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        "codex_binary_sha256": "a" * 64,
        "origin_main_revision": str(v1["product_inputs"]["revision"]),
        "origin_url": ORIGIN_URL,
        "provider_config_sha256": materializer.benchmark.codex_provider_config_sha256("openai"),
        "revision": str(v1["product_inputs"]["revision"]),
        "runtime_availability_sha256": hashlib.sha256(runtime_path.read_bytes()).hexdigest(),
    }
    verifier_pins = {
        "bridge_sha256": BRIDGE_SHA256,
        "docker_cli_sha256": DOCKER_CLI_SHA256,
        "docker_daemon_id": DOCKER_DAEMON_ID,
        "docker_package_sha256": DOCKER_PACKAGE_SHA256,
        "docker_server_version": DOCKER_SERVER_VERSION,
        "namespace": "swebench",
        "python_environment_sha256": PYTHON_ENVIRONMENT_SHA256,
        "python_sha256": PYTHON_SHA256,
        "revision": REVISION,
        "run_evaluation_sha256": RUN_EVALUATION_SHA256,
        "schema_version": 1,
    }
    protocol = freezer.build_acquisition_protocol(
        v1=v1,
        frozen_at=str(v1["frozen_at"]),
        acquisition_frozen_at=str(v1["acquisition_frozen_at"]),
        product_inputs=product_inputs,
        verifier_pins=verifier_pins,
        exposure_ledger_sha256=hashlib.sha256(exposure_bytes).hexdigest(),
    )
    monkeypatch.setattr(
        freezer,
        "_supported_v1_protocol",
        lambda: json.loads(json.dumps(v1)),
    )
    selection = holdout.select_rows(
        [holdout.evaluate_row(row, protocol) for row in rows],
        protocol,
    )
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_bytes(_canonical_acquisition(protocol))
    selection_path = tmp_path / "selection.json"
    selection_path.write_bytes(_canonical(selection))
    source_map_path = tmp_path / "sources.json"
    source_map_path.write_bytes(_canonical(sources))
    source_map_path.chmod(0o600)
    return Fixture(
        archive=archive_path,
        docker_cli=docker_cli,
        docker_host="unix:///tmp/ctx-materializer-test.sock",
        exposure_ledger=exposure_path,
        output=tmp_path / "private-output",
        protocol=protocol,
        protocol_path=protocol_path,
        repositories=repositories,
        rows=rows,
        rows_path=rows_path,
        runtime=runtime_path,
        selection=selection,
        selection_path=selection_path,
        source_map=source_map_path,
        swebench_checkout=swebench_checkout,
        swebench_python=swebench_python,
    )


def _run(fixture: Fixture) -> dict[str, str]:
    return materializer.materialize(
        protocol_path=fixture.protocol_path,
        expected_acquisition_protocol_sha256=hashlib.sha256(
            fixture.protocol_path.read_bytes()
        ).hexdigest(),
        exposure_ledger_path=fixture.exposure_ledger,
        rows_path=fixture.rows_path,
        selection_path=fixture.selection_path,
        source_map_path=fixture.source_map,
        runtime_availability_path=fixture.runtime,
        catalog_archive_path=fixture.archive,
        output=fixture.output,
        failure_evidence_output=fixture.output.parent / "materialization-failure-evidence",
        swebench_checkout=fixture.swebench_checkout,
        swebench_python=fixture.swebench_python,
        docker_cli=fixture.docker_cli,
        docker_host=fixture.docker_host,
    )


class VerifierDouble:
    def __init__(
        self,
        fixture: Fixture,
        *,
        fail_phase: str | None = None,
        invalid_phase: str | None = None,
        runtime_drift_phase: str | None = None,
    ) -> None:
        self.fixture = fixture
        self.fail_phase = fail_phase
        self.invalid_phase = invalid_phase
        self.runtime_drift_phase = runtime_drift_phase
        self.calls: list[dict[str, Any]] = []
        self.rows = {str(row["instance_id"]): row for row in fixture.rows}

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        phase = str(kwargs["phase"])
        if phase == self.fail_phase:
            raise materializer.swebench.SWEbenchVerificationError(f"synthetic {phase} failure")
        instance_id = str(kwargs["instance_id"])
        row = self.rows[instance_id]
        fail_to_pass = json.loads(str(row["FAIL_TO_PASS"]))
        pass_to_pass = json.loads(str(row["PASS_TO_PASS"]))
        image_id = "sha256:" + hashlib.sha256(instance_id.encode()).hexdigest()
        if phase == "green":
            assert kwargs["expected_image_id"] == image_id
        run_id = f"ctx-sb-{phase}-{len(self.calls):020d}"
        model_name = f"ctx-swebench-{phase}"
        work_dir = Path(kwargs["work_dir"])
        log_dir = work_dir / "logs" / "run_evaluation" / run_id / model_name / instance_id
        log_dir.mkdir(mode=0o700, parents=True)
        root_names = {
            "parent-process.json",
            "verification-evidence.json",
            "worker-request.json",
            "worker-result.json",
        }
        if phase == "red":
            root_names.add("mode-probe.log")
        artifact_names = {*materializer.swebench.REQUIRED_ARTIFACTS, *root_names}
        artifact_evidence: dict[str, dict[str, object]] = {}
        hidden_selector = fail_to_pass[0]
        for name in sorted(artifact_names):
            target = work_dir / name if name in root_names else log_dir / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            content = (
                f"{HIDDEN_ARTIFACT}|{instance_id}|{hidden_selector}\n"
                if name == "test_output.txt"
                else f"{phase}|{name}\n"
            ).encode()
            target.write_bytes(content)
            target.chmod(0o600)
            artifact_evidence[name] = {
                "bytes": len(content),
                "name": name,
                "present": True,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        validation = {
            "container_policy_count": 2 if phase == "red" else 1,
            "exact_selector_identity": True,
            "exact_selector_keys_present": True,
            "fail_to_pass_count": len(fail_to_pass),
            "image_id": image_id,
            "parsed_status_key_count": len(fail_to_pass) + len(pass_to_pass),
            "pass_to_pass_count": len(pass_to_pass),
            "phase": phase,
            "resolution": "RESOLVED_NO" if phase == "red" else "RESOLVED_FULL",
            "resolved": phase != "red",
            "status_counts": (
                {"FAILED": len(fail_to_pass), "PASSED": len(pass_to_pass)}
                if phase == "red"
                else {"PASSED": len(fail_to_pass) + len(pass_to_pass)}
            ),
        }
        if phase == self.invalid_phase:
            validation["exact_selector_identity"] = False
        pins = self.fixture.protocol[materializer.VERIFIER_PROTOCOL_KEY]
        harness_source_sha256 = "9" * 64 if phase == self.runtime_drift_phase else "8" * 64
        return {
            "artifacts": artifact_evidence,
            "authentication": {
                "git_revision": pins["revision"],
                "run_evaluation_sha256": pins["run_evaluation_sha256"],
                "source_file_count": 42,
                "source_sha256": harness_source_sha256,
            },
            "cleanup": {"ok": True},
            "docker_identity": {
                "daemon_id_sha256": hashlib.sha256(
                    str(pins["docker_daemon_id"]).encode()
                ).hexdigest(),
                "server_version": pins["docker_server_version"],
            },
            "docker_package": {
                "file_count": 17,
                "sha256": pins["docker_package_sha256"],
            },
            "input_snapshots": {
                "bridge_sha256": pins["bridge_sha256"],
                "dataset_sha256": self.fixture.protocol["universe"]["selection_jsonl_sha256"],
                "docker_package": {
                    "file_count": 17,
                    "sha256": pins["docker_package_sha256"],
                },
                "harness_source": {
                    "file_count": 42,
                    "sha256": harness_source_sha256,
                },
            },
            "model_name": model_name,
            "phase": phase,
            "process": {
                "residual_descendant_count": 0,
                "returncode": 0,
                "timed_out": False,
            },
            "python_environment": {
                "distribution_count": 23,
                "sha256": pins["python_environment_sha256"],
            },
            "run_id": run_id,
            "schema_version": 1,
            "validation": validation,
        }


def _install_verifier(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_phase: str | None = None,
    invalid_phase: str | None = None,
    runtime_drift_phase: str | None = None,
) -> VerifierDouble:
    double = VerifierDouble(
        fixture,
        fail_phase=fail_phase,
        invalid_phase=invalid_phase,
        runtime_drift_phase=runtime_drift_phase,
    )
    monkeypatch.setattr(materializer.swebench, "verify_swebench", double)
    return double


def test_materializes_ten_official_controls_and_evaluator_bound_attestations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)

    hashes = _run(fixture)

    output = fixture.output
    assert set(hashes) == {
        "scenario_pack",
        "collision",
        "reconstructed",
        "controls",
    }
    assert len(double.calls) == 20
    assert not (output.parent / "materialization-failure-evidence").exists()
    retained = output / materializer.VERIFICATION_DIR
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE((output / name).stat().st_mode) == 0o600
            for name in materializer.OUTPUT_FILES.values()
        )
        assert stat.S_IMODE(retained.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
            for path in retained.rglob("*")
        )
    assert len(list(retained.glob("scenario-*"))) == 10

    scenario_bytes = (output / "scenario-pack.json").read_bytes()
    controls_bytes = (output / "control-results.json").read_bytes()
    scenario_pack = json.loads(scenario_bytes)
    scenarios = scenario_pack["scenarios"]
    controls = json.loads(controls_bytes)
    assert scenario_pack["version"] == 1
    assert len(scenarios) == 10
    assert controls["all_scenarios_passed"] is True
    assert "all_seven_passed" not in controls
    assert set(controls["scenario_results"]) == {row["id"] for row in scenarios}
    assert controls["guard"] == "holdout-control-results-v1"
    assert controls["scenario_count"] == 10
    assert not (output / "execution-schedule.json").exists()
    assert all(row["ctx_context"] == [] for row in scenarios)
    assert all(row["reconstructed_test_sha256"] for row in scenarios)
    assert all(
        result["official_swebench"]["red"]["status_counts"]["FAILED"] == 1
        and result["official_swebench"]["green"]["status_counts"] == {"PASSED": 2}
        and result["official_swebench"]["red"]["image_id"]
        == result["official_swebench"]["green"]["image_id"]
        and result["official_swebench"]["image_id"]
        == result["official_swebench"]["green"]["image_id"]
        for result in controls["scenario_results"].values()
    )
    assert all(
        row["official_verifier_binding"]["base_commit"] == row["commit"]
        and row["official_verifier_binding"]["repository_url"] == row["repo_url"]
        and row["official_verifier_binding"]["image_content_digest"]
        == controls["scenario_results"][row["id"]]["official_swebench"]["image_id"]
        and row["official_verifier_binding"]["dataset_sha256"]
        == fixture.protocol["universe"]["selection_jsonl_sha256"]
        for row in scenarios
    )

    protocol = fixture.protocol
    collision_path = output / "collision-attestation.json"
    reconstructed_path = output / "reconstructed-test-attestation.json"
    environment_path = tmp_path / "execution-environment.json"
    schedule_path = tmp_path / "execution-schedule.json"
    frozen_protocol_path = tmp_path / "execution-protocol.json"
    environment_path.write_bytes(
        _canonical(
            {
                "codex": {
                    "runtime_contract": {
                        "arms": ["baseline", "ctx-light"],
                        "model_auto_compact_token_limit": 200_000,
                        "model_reasoning_effort": "high",
                    },
                    "version": "test",
                },
                "evaluator": {
                    "backend": materializer.benchmark.OFFICIAL_HOLDOUT_BACKEND,
                    "pins_sha256": hashlib.sha256(
                        _canonical(protocol[materializer.VERIFIER_PROTOCOL_KEY])
                    ).hexdigest(),
                },
                "limits": {
                    "agent_timeout_seconds": 900,
                    "arms": ["baseline", "ctx-light"],
                    "catalog_cache_hit": False,
                    "measured_concurrency": 1,
                    "pair_count": 30,
                    "retries": 0,
                    "sandbox_contract": materializer.benchmark.OFFICIAL_SANDBOX_CONTRACT,
                    "task_count": 10,
                    "trials_per_scenario": 3,
                },
                "model": "test-model",
                "product_revision": protocol["product_inputs"]["revision"],
                "protocol_id": materializer.PROTOCOL_ID,
                "provider": "openai",
                "python": {
                    "dependencies_sha256": PYTHON_ENVIRONMENT_SHA256,
                    "executable_sha256": PYTHON_SHA256,
                    "version": "3.12.0",
                },
                "schema_version": 1,
            }
        )
    )
    fixture.selection_path.chmod(0o600)
    environment_path.chmod(0o600)
    monkeypatch.setattr(
        materializer.benchmark,
        "PRODUCTION_CATALOG_ARCHIVE",
        fixture.archive,
    )
    monkeypatch.setattr(
        materializer.benchmark,
        "PRODUCTION_RUNTIME_AVAILABILITY",
        fixture.runtime,
    )
    freezer.freeze_protocol(
        protocol_path=fixture.protocol_path,
        exposure_ledger_path=fixture.exposure_ledger,
        expected_acquisition_protocol_sha256=hashlib.sha256(
            fixture.protocol_path.read_bytes()
        ).hexdigest(),
        selection_path=fixture.selection_path,
        scenario_pack_path=output / "scenario-pack.json",
        source_map_path=fixture.source_map,
        collision_path=collision_path,
        reconstructed_path=reconstructed_path,
        controls_path=output / "control-results.json",
        environment_path=environment_path,
        schedule_path=schedule_path,
        output_path=frozen_protocol_path,
        frozen_at="2026-07-30T12:34:56+03:00",
    )
    protocol_bytes = frozen_protocol_path.read_bytes()
    loaded = materializer.benchmark.load_execution_frozen_holdout(
        protocol_path=frozen_protocol_path,
        expected_protocol_sha256=hashlib.sha256(protocol_bytes).hexdigest(),
        selection_path=fixture.selection_path,
        scenario_pack_path=output / "scenario-pack.json",
        collision_path=collision_path,
        reconstructed_path=reconstructed_path,
        control_results_path=output / "control-results.json",
        environment_path=environment_path,
        schedule_path=schedule_path,
        source_map_path=fixture.source_map,
    )
    assert len(loaded.scenarios) == 10
    assert len(loaded.schedule.assignments) == 30
    assert loaded.image_ids == {
        scenario_id: result["official_swebench"]["image_id"]
        for scenario_id, result in controls["scenario_results"].items()
    }


def test_red_green_invocations_are_symmetric_and_host_pytest_is_never_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    commands: list[list[str]] = []
    original_run = materializer._run

    def recording_run(argv: list[str], **kwargs: Any) -> materializer.ProcessResult:
        commands.append(list(argv))
        return original_run(argv, **kwargs)

    monkeypatch.setattr(materializer, "_run", recording_run)
    _run(fixture)

    assert len(double.calls) == 20
    pins = fixture.protocol[materializer.VERIFIER_PROTOCOL_KEY]
    excluded = {
        "allow_image_pull",
        "expected_image_id",
        "phase",
        "timeout",
        "work_dir",
    }
    for red, green in zip(double.calls[::2], double.calls[1::2], strict=True):
        assert red["phase"] == "red"
        assert green["phase"] == "green"
        assert red["instance_id"] == green["instance_id"]
        assert red["allow_image_pull"] is True
        assert "expected_image_id" not in red
        assert green["allow_image_pull"] is False
        assert green["expected_image_id"] == (
            "sha256:" + hashlib.sha256(str(red["instance_id"]).encode()).hexdigest()
        )
        assert red["expected_bridge_sha256"] == pins["bridge_sha256"]
        assert (
            red["expected_dataset_sha256"]
            == hashlib.sha256(fixture.rows_path.read_bytes()).hexdigest()
        )
        assert red["expected_revision"] == pins["revision"]
        assert red["expected_run_evaluation_sha256"] == pins["run_evaluation_sha256"]
        assert red["expected_python_sha256"] == pins["python_sha256"]
        assert red["expected_python_environment_sha256"] == pins["python_environment_sha256"]
        assert red["expected_docker_package_sha256"] == pins["docker_package_sha256"]
        assert red["expected_docker_cli_sha256"] == pins["docker_cli_sha256"]
        assert red["expected_docker_daemon_id"] == pins["docker_daemon_id"]
        assert red["expected_docker_server_version"] == pins["docker_server_version"]
        assert {key: value for key, value in red.items() if key not in excluded} == {
            key: value for key, value in green.items() if key not in excluded
        }
    assert all(
        Path(command[0]).name != "pytest"
        and not any(command[index : index + 2] == ["-m", "pytest"] for index in range(len(command)))
        for command in commands
    )


def test_authenticated_v2_semantic_evidence_is_deterministic_across_identical_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _install_verifier(fixture, monkeypatch)

    first_hashes = _run(fixture)
    first_bytes = {
        name: (fixture.output / filename).read_bytes()
        for name, filename in materializer.OUTPUT_FILES.items()
    }
    fixture.output = tmp_path / "private-output-second"
    second_hashes = _run(fixture)
    second_bytes = {
        name: (fixture.output / filename).read_bytes()
        for name, filename in materializer.OUTPUT_FILES.items()
    }

    for name in ("scenario_pack", "collision", "reconstructed"):
        assert first_hashes[name] == second_hashes[name]
        assert first_bytes[name] == second_bytes[name]
    first_controls = json.loads(first_bytes["controls"])
    second_controls = json.loads(second_bytes["controls"])
    for document in (first_controls, second_controls):
        for result in document["scenario_results"].values():
            result.pop("elapsed_seconds")
    assert first_controls == second_controls


@pytest.mark.parametrize("phase", ["red", "green"])
def test_failed_official_control_is_no_go_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch, fail_phase=phase)

    with pytest.raises(materializer.MaterializationError, match=f"official {phase} control"):
        _run(fixture)

    assert not fixture.output.exists()
    assert len(double.calls) == (1 if phase == "red" else 2)
    assert not list(fixture.output.parent.glob(".official-verification-*"))
    failure_root = fixture.output.parent / "materialization-failure-evidence"
    failure = json.loads((failure_root / "failure.json").read_text(encoding="utf-8"))
    assert failure["guard"] == "ctx-ab-private-failure-v1"
    assert failure["operation"] == "holdout-materialization"
    assert [item["type"] for item in failure["exception_chain"]] == [
        "MaterializationError",
        "SWEbenchVerificationError",
    ]
    assert (failure_root / "raw-control-work" / "scenario-0").is_dir()
    if phase == "green":
        assert (failure_root / "scenario-000" / "red" / "test_output.txt").is_file()
    manifest = json.loads((failure_root / "artifact-manifest.json").read_text(encoding="utf-8"))
    assert manifest["guard"] == "ctx-ab-private-failure-v1"
    assert manifest["entry_count"] == len(manifest["entries"])
    assert (
        manifest["manifest_sha256"] == hashlib.sha256(_canonical(manifest["entries"])).hexdigest()
    )
    if os.name != "nt":
        assert stat.S_IMODE(failure_root.stat().st_mode) == 0o700
        assert stat.S_IMODE((failure_root / "failure.json").stat().st_mode) == 0o600
        assert all(
            path.is_symlink() or path.lstat().st_mode & 0o077 == 0
            for path in failure_root.rglob("*")
        )


def test_cli_preserves_early_failure_without_printing_private_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(**_kwargs: Any) -> dict[str, str]:
        raise RuntimeError("private-task-id and private detail")

    monkeypatch.setattr(materializer, "materialize", fail)
    failure_root = tmp_path / "materialization-failure"
    argv = [
        "--protocol",
        str(tmp_path / "protocol"),
        "--expected-acquisition-protocol-sha256",
        "0" * 64,
        "--exposure-ledger",
        str(tmp_path / "exposure"),
        "--rows",
        str(tmp_path / "rows"),
        "--selection",
        str(tmp_path / "selection"),
        "--source-map",
        str(tmp_path / "source-map"),
        "--runtime-availability",
        str(tmp_path / "runtime"),
        "--catalog-archive",
        str(tmp_path / "catalog"),
        "--output",
        str(tmp_path / "output"),
        "--failure-evidence-output",
        str(failure_root),
        "--swebench-checkout",
        str(tmp_path / "swebench"),
        "--swebench-python",
        str(tmp_path / "python"),
        "--docker-cli",
        str(tmp_path / "docker"),
        "--docker-host",
        "unix:///tmp/test-docker.sock",
    ]

    with pytest.raises(SystemExit) as raised:
        materializer.main(argv)

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err == (
        "materialization failed; private details suppressed (RuntimeError); evidence=preserved\n"
    )
    assert "private-task" not in captured.err
    failure = json.loads((failure_root / "failure.json").read_text(encoding="utf-8"))
    assert failure["operation"] == "holdout-materialization"
    assert failure["exception_chain"] == [
        {
            "message": "private-task-id and private detail",
            "type": "RuntimeError",
        }
    ]


def test_final_output_publication_failure_preserves_all_control_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    real_rename = Path.rename

    def fail_final_output_publication(source: Path, target: Path) -> Path:
        if source.name.startswith(".materialize-") and target == fixture.output:
            raise OSError("synthetic final output publication failure")
        return real_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_final_output_publication)

    with pytest.raises(OSError, match="synthetic final output publication failure"):
        _run(fixture)

    assert len(double.calls) == 20
    assert not fixture.output.exists()
    failure_root = fixture.output.parent / "materialization-failure-evidence"
    assert (failure_root / "failure.json").is_file()
    retained = [
        path for path in failure_root.glob("scenario-*") if path.is_dir() and not path.is_symlink()
    ]
    assert len(retained) == 10
    assert not list(fixture.output.parent.glob(".materialize-*"))
    assert not list(fixture.output.parent.glob(".official-verification-*"))


def test_inexact_selector_evidence_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch, invalid_phase="red")

    with pytest.raises(materializer.MaterializationError, match="official red control"):
        _run(fixture)

    assert len(double.calls) == 1
    assert not fixture.output.exists()


def test_cross_phase_runtime_identity_drift_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(
        fixture,
        monkeypatch,
        runtime_drift_phase="green",
    )

    with pytest.raises(materializer.MaterializationError, match="official green control"):
        _run(fixture)

    assert len(double.calls) == 2
    assert not fixture.output.exists()


def test_frozen_dataset_tamper_is_rejected_before_verifier_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    fixture.rows_path.write_bytes(fixture.rows_path.read_bytes() + b"\n")

    with pytest.raises(materializer.MaterializationError, match="acquisition freeze"):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


def test_historically_exposed_top_candidate_is_replaced_and_materialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    exposed_id = str(fixture.selection["analysis_instance_ids"][0])
    exposure_document = {
        "instance_id_hmac_sha256": [
            exposure_ledger.instance_id_hmac_sha256(EXPOSURE_SALT, exposed_id)
        ],
        "salt": EXPOSURE_SALT,
        "schema_version": 1,
    }
    exposure_bytes = exposure_ledger.canonical_ledger_bytes(exposure_document)
    fixture.exposure_ledger.write_bytes(exposure_bytes)
    fixture.exposure_ledger.chmod(0o600)
    fixture.protocol["exposure_ledger_sha256"] = hashlib.sha256(exposure_bytes).hexdigest()
    fixture.protocol_path.write_bytes(_canonical_acquisition(fixture.protocol))
    filtered = holdout.reject_historical_exposures(
        [holdout.evaluate_row(row, fixture.protocol) for row in fixture.rows],
        exposure_document,
    )
    fixture.selection = holdout.select_rows(filtered, fixture.protocol)
    fixture.selection_path.write_bytes(_canonical(fixture.selection))
    double = _install_verifier(fixture, monkeypatch)

    _run(fixture)

    assert exposed_id not in fixture.selection["analysis_instance_ids"]
    assert len(fixture.selection["analysis_instance_ids"]) == 10
    assert len(set(fixture.selection["analysis_repository_map"].values())) == 10
    assert len(double.calls) == 20


@pytest.mark.parametrize("kind", ["missing", "tampered", "symlink", "hardlink"])
def test_exposure_ledger_fails_closed_before_selection_or_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    alias = fixture.exposure_ledger.with_suffix(".alias")
    if kind == "missing":
        fixture.exposure_ledger.unlink()
    elif kind == "tampered":
        fixture.exposure_ledger.write_bytes(fixture.exposure_ledger.read_bytes() + b"tampered")
    elif kind == "symlink":
        fixture.exposure_ledger.rename(alias)
        fixture.exposure_ledger.symlink_to(alias)
    else:
        alias.hardlink_to(fixture.exposure_ledger)

    with pytest.raises(
        materializer.MaterializationError,
        match="authenticated exposure ledger",
    ):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "tampered-bundle",
        "reordered",
        "path-traversal",
        "extra-key",
        "hash-mismatch",
    ],
)
def test_source_map_and_bundles_fail_closed_before_verifier_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    document = json.loads(fixture.source_map.read_bytes())
    first = next(iter(document["repositories"].values()))
    bundle = fixture.source_map.parent / first["bundle_path"]
    if kind == "missing":
        fixture.source_map.unlink()
    elif kind == "tampered-bundle":
        bundle.write_bytes(bundle.read_bytes() + b"tampered")
    elif kind == "reordered":
        fixture.source_map.write_bytes(
            json.dumps(
                {
                    "schema_version": document["schema_version"],
                    "repositories": document["repositories"],
                },
                separators=(",", ":"),
            ).encode()
        )
    elif kind == "path-traversal":
        first["bundle_path"] = "../escaped.bundle"
        fixture.source_map.write_bytes(_canonical(document))
    elif kind == "extra-key":
        first["extra"] = True
        fixture.source_map.write_bytes(_canonical(document))
    else:
        first["bundle_sha256"] = "0" * 64
        fixture.source_map.write_bytes(_canonical(document))
    if fixture.source_map.exists():
        fixture.source_map.chmod(0o600)

    with pytest.raises(materializer.MaterializationError, match="private source map"):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_source_bundle_aliases_fail_closed_before_verifier_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    document = json.loads(fixture.source_map.read_bytes())
    first = next(iter(document["repositories"].values()))
    bundle = fixture.source_map.parent / first["bundle_path"]
    alias = bundle.with_suffix(".alias")
    if kind == "symlink":
        bundle.rename(alias)
        bundle.symlink_to(alias)
    else:
        alias.hardlink_to(bundle)

    with pytest.raises(materializer.MaterializationError, match="private source map"):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


def test_source_bundle_with_future_history_is_rejected_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    document = json.loads(fixture.source_map.read_bytes())
    canonical_url, first = next(iter(document["repositories"].items()))
    repo = fixture.repositories[canonical_url]
    (repo / "future-gold.txt").write_text("future solution\n", encoding="utf-8")
    _git(repo, "add", "future-gold.txt")
    _git(repo, "commit", "-qm", "future gold")
    bundle = fixture.source_map.parent / first["bundle_path"]
    bundle.unlink()
    _git(repo, "bundle", "create", str(bundle), "--all")
    bundle.chmod(0o600)
    first["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
    fixture.source_map.write_bytes(_canonical(document))
    fixture.source_map.chmod(0o600)

    with pytest.raises(
        materializer.MaterializationError,
        match="unpinned ref|base-commit closure",
    ):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


def test_requires_exactly_ten_repositories_before_verifier_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    fixture.protocol["selection"].update(
        {
            "analysis_repositories": 9,
            "analysis_scenarios": 9,
            "eligible_repositories_required": 9,
        }
    )
    fixture.protocol_path.write_bytes(_canonical_acquisition(fixture.protocol))
    fixture.selection = holdout.select_rows(
        [holdout.evaluate_row(row, fixture.protocol) for row in fixture.rows],
        fixture.protocol,
    )
    fixture.selection_path.write_bytes(_canonical(fixture.selection))

    with pytest.raises(
        materializer.MaterializationError,
        match="fixed V2 acquisition design drifted",
    ):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


def test_broken_symlink_output_destination_is_rejected_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    fixture.output.symlink_to(tmp_path / "missing-private-output", target_is_directory=True)

    with pytest.raises(materializer.MaterializationError, match="already exists"):
        _run(fixture)

    assert not double.calls
    assert fixture.output.is_symlink()


def test_materialization_never_mutates_future_agent_repository_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _install_verifier(fixture, monkeypatch)

    _run(fixture)

    selected_ids = set(fixture.selection["analysis_instance_ids"])
    for row in fixture.rows:
        if row["instance_id"] not in selected_ids:
            continue
        repo = fixture.repositories[holdout.canonical_repo_url(str(row["repo"]))]
        assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
        product_path = holdout._parse_patch(str(row["patch"]))[0][0]
        test_path = holdout._parse_patch(str(row["test_patch"]))[0][0]
        assert "return 0" in (repo / product_path).read_text(encoding="utf-8")
        assert "test_new_behavior" not in (repo / test_path).read_text(encoding="utf-8")


def test_hidden_raw_artifacts_are_private_and_summaries_are_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _install_verifier(fixture, monkeypatch)
    hashes = _run(fixture)

    selected_id = fixture.selection["analysis_instance_ids"][0]
    selected = next(row for row in fixture.rows if row["instance_id"] == selected_id)
    selector = json.loads(str(selected["FAIL_TO_PASS"]))[0]
    raw = (
        fixture.output / materializer.VERIFICATION_DIR / "scenario-000" / "red" / "test_output.txt"
    ).read_text(encoding="utf-8")
    summary = (
        fixture.output / materializer.VERIFICATION_DIR / "scenario-000" / "red" / "summary.json"
    ).read_text(encoding="utf-8")
    evidence = (
        fixture.output / materializer.VERIFICATION_DIR / "scenario-000" / "red" / "evidence.json"
    ).read_text(encoding="utf-8")
    controls = (fixture.output / "control-results.json").read_text(encoding="utf-8")

    assert HIDDEN_ARTIFACT in raw
    assert selected_id in raw
    assert selector in raw
    for safe_summary in (summary, evidence, controls, json.dumps(hashes)):
        assert HIDDEN_ARTIFACT not in safe_summary
        assert selector not in safe_summary
        assert str(selected["problem_statement"]) not in safe_summary
        assert str(selected["patch"]) not in safe_summary
        assert str(selected["test_patch"]) not in safe_summary


def test_requires_exact_frozen_timeout_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    fixture.protocol["timeouts"]["control_verification_seconds"] = 899
    fixture.protocol_path.write_bytes(_canonical_acquisition(fixture.protocol))

    with pytest.raises(
        materializer.MaterializationError,
        match="fixed V2 acquisition design drifted",
    ):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


@pytest.mark.parametrize(
    "expected_sha256",
    [
        "0" * 64,
        "not-a-sha256",
    ],
)
def test_acquisition_protocol_authentication_fails_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_sha256: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    private_calls: list[str] = []

    def fail_if_called(*args: Any, **kwargs: Any) -> Any:
        private_calls.append("called")
        raise AssertionError("private work must not run before protocol authentication")

    monkeypatch.setattr(materializer, "_load_jsonl", fail_if_called)
    monkeypatch.setattr(materializer, "_repo_source", fail_if_called)
    monkeypatch.setattr(materializer, "_validate_output_destination", fail_if_called)

    with pytest.raises(
        materializer.MaterializationError,
        match=r"^acquisition protocol authentication failed$",
    ):
        materializer.materialize(
            protocol_path=fixture.protocol_path,
            expected_acquisition_protocol_sha256=expected_sha256,
            exposure_ledger_path=tmp_path / "must-not-read-exposure-ledger.json",
            rows_path=tmp_path / "must-not-read-rows.jsonl",
            selection_path=tmp_path / "must-not-read-selection.json",
            source_map_path=tmp_path / "must-not-read-sources.json",
            runtime_availability_path=tmp_path / "must-not-read-runtime.json",
            catalog_archive_path=tmp_path / "must-not-read-catalog.tar.gz",
            output=fixture.output,
            failure_evidence_output=tmp_path / "failure-evidence",
            swebench_checkout=tmp_path / "must-not-use-swebench",
            swebench_python=tmp_path / "must-not-use-python",
            docker_cli=tmp_path / "must-not-use-docker",
            docker_host=fixture.docker_host,
        )

    assert not private_calls
    assert not double.calls
    assert not fixture.output.exists()


def test_acquisition_protocol_byte_drift_is_rejected_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    expected_sha256 = hashlib.sha256(fixture.protocol_path.read_bytes()).hexdigest()
    double = _install_verifier(fixture, monkeypatch)
    fixture.protocol_path.write_bytes(fixture.protocol_path.read_bytes() + b"\n")

    with pytest.raises(
        materializer.MaterializationError,
        match=r"^acquisition protocol authentication failed$",
    ):
        materializer.materialize(
            protocol_path=fixture.protocol_path,
            expected_acquisition_protocol_sha256=expected_sha256,
            exposure_ledger_path=fixture.exposure_ledger,
            rows_path=fixture.rows_path,
            selection_path=fixture.selection_path,
            source_map_path=fixture.source_map,
            runtime_availability_path=fixture.runtime,
            catalog_archive_path=fixture.archive,
            output=fixture.output,
            failure_evidence_output=tmp_path / "failure-evidence",
            swebench_checkout=fixture.swebench_checkout,
            swebench_python=fixture.swebench_python,
            docker_cli=fixture.docker_cli,
            docker_host=fixture.docker_host,
        )

    assert not double.calls
    assert not fixture.output.exists()


def test_requires_complete_pinned_verifier_identity_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    del fixture.protocol[materializer.VERIFIER_PROTOCOL_KEY]["bridge_sha256"]
    fixture.protocol_path.write_bytes(_canonical_acquisition(fixture.protocol))

    with pytest.raises(materializer.MaterializationError, match="unsupported shape"):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda protocol: protocol["execution_inputs"].update(extra=None),
        lambda protocol: protocol["product_inputs"].update(extra="0" * 64),
    ],
)
def test_rejects_protocol_shapes_the_freezer_would_reject_before_private_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Any,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    double = _install_verifier(fixture, monkeypatch)
    mutate(fixture.protocol)
    fixture.protocol_path.write_bytes(_canonical_acquisition(fixture.protocol))

    with pytest.raises(materializer.MaterializationError, match="fresh supported V2"):
        _run(fixture)

    assert not double.calls
    assert not fixture.output.exists()


def test_cli_failure_suppresses_private_identifiers_and_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    fixture.protocol["timeouts"]["control_verification_seconds"] = 899
    fixture.protocol_path.write_bytes(_canonical_acquisition(fixture.protocol))
    with pytest.raises(SystemExit) as raised:
        materializer.main(
            [
                "--protocol",
                str(fixture.protocol_path),
                "--expected-acquisition-protocol-sha256",
                hashlib.sha256(fixture.protocol_path.read_bytes()).hexdigest(),
                "--exposure-ledger",
                str(fixture.exposure_ledger),
                "--rows",
                str(fixture.rows_path),
                "--selection",
                str(fixture.selection_path),
                "--source-map",
                str(fixture.source_map),
                "--runtime-availability",
                str(fixture.runtime),
                "--catalog-archive",
                str(fixture.archive),
                "--output",
                str(fixture.output),
                "--swebench-checkout",
                str(fixture.swebench_checkout),
                "--swebench-python",
                str(fixture.swebench_python),
                "--docker-cli",
                str(fixture.docker_cli),
                "--docker-host",
                fixture.docker_host,
            ]
        )
    assert raised.value.code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "PRIVATE-HOLDOUT-TASK" not in combined
    assert "private-holdout-" not in combined
    assert "owner/repo-" not in combined
