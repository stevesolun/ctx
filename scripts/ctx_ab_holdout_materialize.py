#!/usr/bin/env python3
"""Materialize and control-check a frozen private CTX holdout."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ctx_ab_benchmark as benchmark  # noqa: E402
from scripts import ctx_ab_exposure_ledger as exposure_ledger  # noqa: E402
from scripts import ctx_ab_holdout as holdout  # noqa: E402
from scripts import ctx_ab_holdout_freeze as freezer  # noqa: E402
from scripts import ctx_ab_swebench as swebench  # noqa: E402


OUTPUT_FILES = {
    "scenario_pack": "scenario-pack.json",
    "collision": "collision-attestation.json",
    "reconstructed": "reconstructed-test-attestation.json",
    "controls": "control-results.json",
}
VERIFICATION_DIR = "official-verification"
VERIFIER_PROTOCOL_KEY = "official_swebench_verifier"
PROTOCOL_ID = freezer.PROTOCOL_ID
SCENARIO_COUNT = 10


class MaterializationError(RuntimeError):
    """A private holdout failed deterministic materialization."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


@dataclass(frozen=True)
class VerifierRuntime:
    """Operator-local paths for a protocol-pinned official verifier."""

    swebench_checkout: Path
    swebench_python: Path
    docker_cli: Path
    docker_host: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MaterializationError("JSON input must contain an object")
    return value


def _load_authenticated_protocol(path: Path, *, expected_sha256: str) -> dict[str, Any]:
    if not isinstance(expected_sha256, str) or freezer.SHA256.fullmatch(expected_sha256) is None:
        raise MaterializationError("acquisition protocol authentication failed")
    protocol_bytes = path.read_bytes()
    if not secrets.compare_digest(_sha256(protocol_bytes), expected_sha256):
        raise MaterializationError("acquisition protocol authentication failed")
    value = json.loads(protocol_bytes)
    if not isinstance(value, dict):
        raise MaterializationError("acquisition protocol must contain an object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise MaterializationError("canonical JSONL must contain object rows")
    return rows


def _string_list(value: object, *, field: str) -> list[str]:
    try:
        items = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise MaterializationError(f"{field} must be a JSON string list") from exc
    if (
        not isinstance(items, list)
        or not items
        or not all(isinstance(item, str) and item for item in items)
    ):
        raise MaterializationError(f"{field} must be a non-empty JSON string list")
    return items


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MaterializationError("control verification exceeded the frozen timeout")
    return remaining


def _run(
    argv: list[str],
    *,
    cwd: Path,
    deadline: float,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=_remaining(deadline))
        return ProcessResult(process.returncode, stdout, stderr, False)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        return ProcessResult(process.returncode, stdout, stderr, True)


def _checked(
    argv: list[str],
    *,
    cwd: Path,
    deadline: float,
    input_text: str | None = None,
) -> ProcessResult:
    result = _run(argv, cwd=cwd, deadline=deadline, input_text=input_text)
    if result.timed_out:
        raise MaterializationError("control verification exceeded the frozen timeout")
    if result.returncode:
        raise MaterializationError("repository materialization command failed")
    return result


def _validate_runtime(runtime: VerifierRuntime) -> None:
    for path in (
        runtime.swebench_checkout,
        runtime.swebench_python,
        runtime.docker_cli,
    ):
        if not path.is_absolute():
            raise MaterializationError("official verifier runtime paths must be absolute")
    if not isinstance(runtime.docker_host, str) or not runtime.docker_host:
        raise MaterializationError("official verifier Docker host is invalid")


def _phase_summary(
    evidence: Mapping[str, Any],
    *,
    phase: str,
    fail_to_pass_count: int,
    pass_to_pass_count: int,
    pins: Mapping[str, Any],
    dataset_sha256: str,
) -> dict[str, Any]:
    validation = evidence.get("validation")
    authentication = evidence.get("authentication")
    python_environment = evidence.get("python_environment")
    docker_package = evidence.get("docker_package")
    docker_identity = evidence.get("docker_identity")
    cleanup = evidence.get("cleanup")
    process = evidence.get("process")
    artifacts = evidence.get("artifacts")
    input_snapshots = evidence.get("input_snapshots")
    expected_resolution = "RESOLVED_NO" if phase == "red" else "RESOLVED_FULL"
    expected_resolved = phase != "red"
    if (
        evidence.get("schema_version") != 1
        or evidence.get("phase") != phase
        or not isinstance(validation, Mapping)
        or validation.get("phase") != phase
        or validation.get("exact_selector_identity") is not True
        or validation.get("exact_selector_keys_present") is not True
        or validation.get("fail_to_pass_count") != fail_to_pass_count
        or validation.get("pass_to_pass_count") != pass_to_pass_count
        or validation.get("resolution") != expected_resolution
        or validation.get("resolved") is not expected_resolved
        or not isinstance(validation.get("container_policy_count"), int)
        or isinstance(validation.get("container_policy_count"), bool)
        or validation["container_policy_count"] < 1
        or not swebench.IMAGE_ID_PATTERN.fullmatch(str(validation.get("image_id") or ""))
        or not isinstance(authentication, Mapping)
        or authentication.get("git_revision") != pins["revision"]
        or authentication.get("run_evaluation_sha256") != pins["run_evaluation_sha256"]
        or not swebench.SHA256_PATTERN.fullmatch(str(authentication.get("source_sha256") or ""))
        or isinstance(authentication.get("source_file_count"), bool)
        or not isinstance(authentication.get("source_file_count"), int)
        or authentication["source_file_count"] < 1
        or not isinstance(python_environment, Mapping)
        or python_environment.get("sha256") != pins["python_environment_sha256"]
        or isinstance(python_environment.get("distribution_count"), bool)
        or not isinstance(python_environment.get("distribution_count"), int)
        or python_environment["distribution_count"] < 1
        or not isinstance(docker_package, Mapping)
        or docker_package.get("sha256") != pins["docker_package_sha256"]
        or isinstance(docker_package.get("file_count"), bool)
        or not isinstance(docker_package.get("file_count"), int)
        or docker_package["file_count"] < 1
        or not isinstance(docker_identity, Mapping)
        or docker_identity.get("server_version") != pins["docker_server_version"]
        or docker_identity.get("daemon_id_sha256")
        != _sha256(str(pins["docker_daemon_id"]).encode())
        or not isinstance(input_snapshots, Mapping)
        or input_snapshots.get("bridge_sha256") != pins["bridge_sha256"]
        or input_snapshots.get("dataset_sha256") != dataset_sha256
        or input_snapshots.get("docker_package")
        != {
            "file_count": docker_package.get("file_count"),
            "sha256": pins["docker_package_sha256"],
        }
        or input_snapshots.get("harness_source")
        != {
            "file_count": authentication.get("source_file_count"),
            "sha256": authentication.get("source_sha256"),
        }
        or not isinstance(cleanup, Mapping)
        or cleanup.get("ok") is not True
        or not isinstance(process, Mapping)
        or process.get("returncode") != 0
        or process.get("timed_out") is not False
        or process.get("residual_descendant_count") != 0
        or not isinstance(artifacts, Mapping)
    ):
        raise MaterializationError(f"official {phase} control evidence is invalid")

    expected_artifacts = {
        *swebench.REQUIRED_ARTIFACTS,
        "parent-process.json",
        "verification-evidence.json",
        "worker-request.json",
        "worker-result.json",
    }
    if phase == "red":
        expected_artifacts.add("mode-probe.log")
    if set(artifacts) != expected_artifacts:
        raise MaterializationError(f"official {phase} artifacts are incomplete")
    artifact_bytes = 0
    for name in sorted(expected_artifacts):
        item = artifacts.get(name)
        if (
            not isinstance(item, Mapping)
            or item.get("present") is not True
            or item.get("name") != name
            or isinstance(item.get("bytes"), bool)
            or not isinstance(item.get("bytes"), int)
            or item["bytes"] < 0
            or not swebench.SHA256_PATTERN.fullmatch(str(item.get("sha256") or ""))
        ):
            raise MaterializationError(f"official {phase} artifacts are invalid")
        artifact_bytes += item["bytes"]
    status_counts = validation.get("status_counts")
    if (
        not isinstance(status_counts, Mapping)
        or not status_counts
        or not all(
            isinstance(status, str)
            and status in swebench.KNOWN_STATUSES
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count >= 0
            for status, count in status_counts.items()
        )
        or sum(status_counts.values()) != fail_to_pass_count + pass_to_pass_count
    ):
        raise MaterializationError(f"official {phase} status evidence is invalid")

    identity = {
        "authentication": {
            "git_revision": authentication["git_revision"],
            "run_evaluation_sha256": authentication["run_evaluation_sha256"],
            "source_file_count": authentication["source_file_count"],
            "source_sha256": authentication["source_sha256"],
        },
        "docker_identity": {
            "daemon_id_sha256": docker_identity["daemon_id_sha256"],
            "server_version": docker_identity["server_version"],
        },
        "docker_package": {
            "file_count": docker_package["file_count"],
            "sha256": docker_package["sha256"],
        },
        "input_snapshots": input_snapshots,
        "python_environment": {
            "distribution_count": python_environment.get("distribution_count"),
            "sha256": python_environment["sha256"],
        },
    }
    runtime_identity_sha256 = _sha256(_canonical_bytes(identity))
    semantic_evidence = {
        "container_policy_count": validation["container_policy_count"],
        "exact_selector_identity": True,
        "fail_to_pass_count": fail_to_pass_count,
        "image_id": validation["image_id"],
        "pass_to_pass_count": pass_to_pass_count,
        "phase": phase,
        "resolved": expected_resolved,
        "runtime_identity_sha256": runtime_identity_sha256,
        "status_counts": dict(sorted(status_counts.items())),
    }
    return {
        "artifact_bytes": artifact_bytes,
        "artifact_count": len(expected_artifacts),
        "artifact_manifest_sha256": _sha256(_canonical_bytes(artifacts)),
        **semantic_evidence,
        "raw_verifier_evidence_sha256": _sha256(_canonical_bytes(evidence)),
        "verifier_evidence_sha256": _sha256(_canonical_bytes(semantic_evidence)),
    }


def _control_phase_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "artifact_bytes",
        "artifact_count",
        "artifact_manifest_sha256",
        "container_policy_count",
        "exact_selector_identity",
        "fail_to_pass_count",
        "image_id",
        "pass_to_pass_count",
        "phase",
        "runtime_identity_sha256",
        "status_counts",
        "verifier_evidence_sha256",
    )
    return {field: summary[field] for field in fields}


def _private_write(path: Path, data: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _retain_phase_artifacts(
    *,
    evidence: Mapping[str, Any],
    instance_id: str,
    phase: str,
    source_root: Path,
    destination_root: Path,
    summary: Mapping[str, Any],
) -> None:
    run_id = evidence.get("run_id")
    model_name = evidence.get("model_name")
    artifacts = evidence.get("artifacts")
    if (
        not isinstance(run_id, str)
        or not swebench.RUN_ID_PATTERN.fullmatch(run_id)
        or not isinstance(model_name, str)
        or model_name != f"ctx-swebench-{phase}"
        or not isinstance(artifacts, Mapping)
    ):
        raise MaterializationError(f"official {phase} artifact identity is invalid")
    destination_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(destination_root.parent, 0o700)
    destination_root.mkdir(mode=0o700, exist_ok=False)
    os.chmod(destination_root, 0o700)
    log_root = source_root / "logs" / "run_evaluation" / run_id / model_name / instance_id
    root_names = {
        "mode-probe.log",
        "parent-process.json",
        "verification-evidence.json",
        "worker-request.json",
        "worker-result.json",
    }
    for name, item in sorted(artifacts.items()):
        if not isinstance(name, str) or not isinstance(item, Mapping):
            raise MaterializationError(f"official {phase} artifact evidence is invalid")
        source = source_root / name if name in root_names else log_root / name
        try:
            resolved = source.resolve(strict=True)
            resolved.relative_to(source_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise MaterializationError(
                f"official {phase} artifact escaped its private root"
            ) from exc
        if source.is_symlink() or not source.is_file():
            raise MaterializationError(f"official {phase} artifact is not a regular file")
        data = source.read_bytes()
        if (
            len(data) != item.get("bytes")
            or _sha256(data) != item.get("sha256")
            or len(data) > swebench.MAX_EVIDENCE_BYTES
        ):
            raise MaterializationError(f"official {phase} artifact changed before retention")
        _private_write(destination_root / name, data)
    _private_write(destination_root / "evidence.json", _canonical_bytes(evidence))
    _private_write(destination_root / "summary.json", _canonical_bytes(summary))


def _repo_source(
    row: dict[str, Any],
    sources: Mapping[str, freezer.SourceBundle],
) -> freezer.SourceBundle:
    repo = str(row["repo"]).strip().lower()
    url = holdout.canonical_repo_url(repo)
    source = sources.get(url)
    if source is None:
        raise MaterializationError("selected repository is absent from the source map")
    if source.base_commit != str(row.get("base_commit") or ""):
        raise MaterializationError("selected repository source commit is stale")
    return source


def _validate_source_bundle_heads(
    source: freezer.SourceBundle,
    *,
    cwd: Path,
    deadline: float,
) -> None:
    listed_heads = _checked(
        ["git", "bundle", "list-heads", str(source.bundle_path)],
        cwd=cwd,
        deadline=deadline,
    ).stdout.splitlines()
    if not listed_heads or any(
        not line.startswith(f"{source.base_commit} ") for line in listed_heads
    ):
        raise MaterializationError("repository source bundle exposes an unpinned ref")


def _scenario(
    row: dict[str, Any],
    *,
    dataset_path: Path,
    protocol: dict[str, Any],
    pins: dict[str, Any],
    retained_root: Path,
    runtime: VerifierRuntime,
    source: freezer.SourceBundle,
    slot: int,
    work_root: Path,
    timeout: float,
) -> tuple[dict[str, Any], benchmark.Scenario, dict[str, Any], str]:
    started = time.monotonic()
    deadline = started + timeout
    workspace = work_root / f"scenario-{slot}"
    _validate_source_bundle_heads(source, cwd=work_root, deadline=deadline)
    _checked(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "clone",
            "--quiet",
            "--no-checkout",
            "--no-hardlinks",
            str(source.bundle_path),
            str(workspace),
        ],
        cwd=work_root,
        deadline=deadline,
    )
    commit = str(row["base_commit"])
    _checked(
        ["git", "-c", "core.hooksPath=/dev/null", "checkout", "--quiet", "--detach", commit],
        cwd=workspace,
        deadline=deadline,
    )
    observed = _checked(["git", "rev-parse", "HEAD"], cwd=workspace, deadline=deadline)
    if observed.stdout.strip() != commit:
        raise MaterializationError("repository checkout did not reach the pinned commit")
    future_history = _checked(
        ["git", "rev-list", "--all", "--not", commit],
        cwd=workspace,
        deadline=deadline,
    )
    unreachable = _checked(
        ["git", "fsck", "--full", "--unreachable", "--no-reflogs"],
        cwd=workspace,
        deadline=deadline,
    )
    if future_history.stdout.strip() or unreachable.stdout.strip():
        raise MaterializationError("repository source bundle is not a base-commit closure")
    _checked(["git", "remote", "remove", "origin"], cwd=workspace, deadline=deadline)
    remotes = _checked(["git", "remote"], cwd=workspace, deadline=deadline)
    if remotes.stdout.strip():
        raise MaterializationError("repository source bundle retained an external remote")
    tree = _checked(
        ["git", "rev-parse", "HEAD^{tree}"],
        cwd=workspace,
        deadline=deadline,
    ).stdout.strip()
    status_result = _checked(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=workspace,
        deadline=deadline,
    )
    if (
        not swebench.REVISION_PATTERN.fullmatch(tree)
        or tree != source.tree_sha1
        or status_result.stdout
    ):
        raise MaterializationError("repository checkout is not an exact clean tree")

    evaluated = holdout.evaluate_row(row, protocol)
    if evaluated["status"] != "eligible":
        raise MaterializationError("selected row is no longer statically eligible")
    test_path = str(evaluated["test_path"])
    production_paths = str(evaluated["production_paths"]).split("|")
    _checked(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=workspace,
        deadline=deadline,
        input_text=str(row["test_patch"]),
    )
    test_file = workspace / test_path
    try:
        resolved_test_file = test_file.resolve(strict=True)
        resolved_test_file.relative_to(workspace.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise MaterializationError(
            "test patch reconstructed a path outside the repository"
        ) from exc
    if test_file.is_symlink() or not resolved_test_file.is_file():
        raise MaterializationError("test patch did not reconstruct a complete test module")
    test_source = resolved_test_file.read_text(encoding="utf-8")
    holdout.validate_reconstructed_test_module(test_source)
    test_sha256 = _sha256(test_source.encode())

    focused = _string_list(row["FAIL_TO_PASS"], field="FAIL_TO_PASS")
    regressions = _string_list(row["PASS_TO_PASS"], field="PASS_TO_PASS")
    scenario_id = str(row["instance_id"])
    common_verifier = {
        "allowed_paths": production_paths,
        "dataset_path": dataset_path,
        "docker_cli": runtime.docker_cli,
        "docker_host": runtime.docker_host,
        "expected_bridge_sha256": pins["bridge_sha256"],
        "expected_dataset_sha256": protocol["universe"]["selection_jsonl_sha256"],
        "expected_docker_cli_sha256": pins["docker_cli_sha256"],
        "expected_docker_daemon_id": pins["docker_daemon_id"],
        "expected_docker_package_sha256": pins["docker_package_sha256"],
        "expected_docker_server_version": pins["docker_server_version"],
        "expected_python_environment_sha256": pins["python_environment_sha256"],
        "expected_python_sha256": pins["python_sha256"],
        "expected_revision": pins["revision"],
        "expected_run_evaluation_sha256": pins["run_evaluation_sha256"],
        "instance_id": scenario_id,
        "namespace": pins["namespace"],
        "swebench_checkout": runtime.swebench_checkout,
        "swebench_python": runtime.swebench_python,
    }
    red_root = work_root / f"official-{slot:03d}-red"
    try:
        red_evidence = swebench.verify_swebench(
            **common_verifier,
            phase="red",
            work_dir=red_root,
            timeout=_remaining(deadline),
            allow_image_pull=True,
        )
        red_summary = _phase_summary(
            red_evidence,
            phase="red",
            fail_to_pass_count=len(focused),
            pass_to_pass_count=len(regressions),
            pins=pins,
            dataset_sha256=str(protocol["universe"]["selection_jsonl_sha256"]),
        )
        _retain_phase_artifacts(
            evidence=red_evidence,
            instance_id=scenario_id,
            phase="red",
            source_root=red_root,
            destination_root=retained_root / f"scenario-{slot:03d}" / "red",
            summary=red_summary,
        )
    except Exception as exc:
        raise MaterializationError("official red control failed") from exc

    image_id = str(red_summary["image_id"])
    green_root = work_root / f"official-{slot:03d}-green"
    try:
        green_evidence = swebench.verify_swebench(
            **common_verifier,
            phase="green",
            work_dir=green_root,
            timeout=_remaining(deadline),
            expected_image_id=image_id,
            allow_image_pull=False,
        )
        green_summary = _phase_summary(
            green_evidence,
            phase="green",
            fail_to_pass_count=len(focused),
            pass_to_pass_count=len(regressions),
            pins=pins,
            dataset_sha256=str(protocol["universe"]["selection_jsonl_sha256"]),
        )
        if green_summary["image_id"] != image_id:
            raise MaterializationError("official green image identity drifted")
        if green_summary["runtime_identity_sha256"] != red_summary["runtime_identity_sha256"]:
            raise MaterializationError("official verifier runtime identity drifted")
        _retain_phase_artifacts(
            evidence=green_evidence,
            instance_id=scenario_id,
            phase="green",
            source_root=green_root,
            destination_root=retained_root / f"scenario-{slot:03d}" / "green",
            summary=green_summary,
        )
    except Exception as exc:
        raise MaterializationError("official green control failed") from exc

    elapsed = time.monotonic() - started
    if elapsed > timeout:
        raise MaterializationError("control verification exceeded the frozen timeout")
    red_marker = next(
        (
            status
            for status in ("FAILED", "ERROR")
            if int(red_summary["status_counts"].get(status, 0)) > 0
        ),
        "",
    )
    if not red_marker:
        raise MaterializationError("official red control omitted a failure status")

    task = str(row["problem_statement"])
    query = " ".join(task.split())[:240]
    repo_url = holdout.canonical_repo_url(str(row["repo"]))
    verify_command = ["{python}", "-m", "pytest", "-q", *focused]
    regression_command = ["{python}", "-m", "pytest", "-q", *regressions]
    verifier_binding = {
        "allowed_paths_sha256": _sha256(_canonical_bytes(production_paths)),
        "base_commit": commit,
        "bridge_sha256": pins["bridge_sha256"],
        "dataset_row_sha256": _sha256(_canonical_bytes(row)),
        "dataset_sha256": protocol["universe"]["selection_jsonl_sha256"],
        "docker_cli_sha256": pins["docker_cli_sha256"],
        "docker_daemon_id_sha256": _sha256(str(pins["docker_daemon_id"]).encode()),
        "docker_package_sha256": pins["docker_package_sha256"],
        "docker_server_version": pins["docker_server_version"],
        "fail_to_pass_sha256": _sha256(_canonical_bytes(focused)),
        "harness_revision": pins["revision"],
        "harness_source_sha256": red_evidence["authentication"]["source_sha256"],
        "image_content_digest": image_id,
        "pass_to_pass_sha256": _sha256(_canonical_bytes(regressions)),
        "python_environment_sha256": pins["python_environment_sha256"],
        "python_sha256": pins["python_sha256"],
        "repository_tree_sha1": tree,
        "repository_url": repo_url,
        "run_evaluation_sha256": pins["run_evaluation_sha256"],
        "runtime_identity_sha256": red_summary["runtime_identity_sha256"],
        "schema_version": 1,
    }
    scenario_row = {
        "allowed_changes": production_paths,
        "benchmark_class": "historical",
        "commit": commit,
        "ctx_context": [],
        "expected_test_count": len(focused),
        "id": scenario_id,
        "language": "python",
        "official_verifier_binding": verifier_binding,
        "query": query,
        "red_failure_contains": red_marker,
        "reference_patch": str(row["patch"]),
        "regression_verify": [regression_command],
        "repo_url": repo_url,
        "reconstructed_test_sha256": test_sha256,
        "task": task,
        "test_body": test_source,
        "test_path": test_path,
        "verify": verify_command,
    }
    scenario = benchmark.Scenario(
        id=scenario_id,
        repo_url=repo_url,
        commit=commit,
        task=task,
        query=query,
        language="python",
        benchmark_class="historical",
        test_path=test_path,
        test_body=test_source,
        verify=tuple(verify_command),
        expected_test_count=len(focused),
        regression_verify=(tuple(regression_command),),
        red_failure_contains=red_marker,
        reference_patch=str(row["patch"]),
        allowed_changes=tuple(production_paths),
        context=(),
    )
    control = {
        "changed_test_module_green": True,
        "elapsed_seconds": round(elapsed, 6),
        "green_evidence_sha256": green_summary["verifier_evidence_sha256"],
        "module_evidence_sha256": green_summary["artifact_manifest_sha256"],
        "official_swebench": {
            "green": _control_phase_summary(green_summary),
            "image_id": image_id,
            "pins_sha256": _sha256(_canonical_bytes(pins)),
            "red": _control_phase_summary(red_summary),
        },
        "parent_with_test_patch_red": True,
        "reconstructed_test_sha256": test_sha256,
        "red_evidence_sha256": red_summary["verifier_evidence_sha256"],
        "reference_patch_green": True,
        "timeout_compliant": True,
        "timeout_seconds": timeout,
    }
    return scenario_row, scenario, control, test_source


def _validate_output_destination(output: Path) -> None:
    resolved = output.resolve(strict=False)
    private_root = (ROOT / ".gate" / "ctx-ab-private").resolve()
    if ROOT.resolve() in resolved.parents and private_root not in resolved.parents:
        raise MaterializationError("holdout output inside the repository must use the private root")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise MaterializationError("holdout output already exists")


def _write_artifacts(
    output: Path,
    artifacts: dict[str, bytes],
    *,
    retained_evidence: Path,
) -> None:
    _validate_output_destination(output)
    temp = Path(tempfile.mkdtemp(prefix=".materialize-", dir=output.parent))
    os.chmod(temp, 0o700)
    try:
        for key, data in artifacts.items():
            path = temp / OUTPUT_FILES[key]
            _private_write(path, data)
            if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise MaterializationError("private artifact permissions are unsafe")
        retained_evidence.rename(temp / VERIFICATION_DIR)
        temp.rename(output)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def materialize(
    *,
    protocol_path: Path,
    expected_acquisition_protocol_sha256: str,
    exposure_ledger_path: Path,
    rows_path: Path,
    selection_path: Path,
    source_map_path: Path,
    runtime_availability_path: Path,
    catalog_archive_path: Path,
    output: Path,
    swebench_checkout: Path,
    swebench_python: Path,
    docker_cli: Path,
    docker_host: str,
) -> dict[str, str]:
    protocol = _load_authenticated_protocol(
        protocol_path,
        expected_sha256=expected_acquisition_protocol_sha256,
    )
    try:
        freezer._paths_are_distinct(
            {
                "protocol": protocol_path,
                "exposure ledger": exposure_ledger_path,
                "rows": rows_path,
                "selection": selection_path,
                "source map": source_map_path,
                "runtime availability": runtime_availability_path,
                "catalog archive": catalog_archive_path,
                "output": output,
            }
        )
    except freezer.FreezeError as exc:
        raise MaterializationError("materialization inputs must not alias") from exc
    try:
        pins = freezer.validate_acquisition_protocol(
            protocol,
            benchmark_script_path=Path(benchmark.__file__),
            catalog_archive_path=catalog_archive_path,
            runtime_availability_path=runtime_availability_path,
        )
    except freezer.FreezeError as exc:
        raise MaterializationError(
            f"materializer requires a valid acquisition-frozen V2 protocol: {exc}"
        ) from exc
    try:
        exposure_document = exposure_ledger.load_authenticated_ledger(
            exposure_ledger_path,
            str(protocol["exposure_ledger_sha256"]),
        )
    except (OSError, ValueError) as exc:
        raise MaterializationError("authenticated exposure ledger is invalid") from exc
    timeout = protocol["timeouts"]["control_verification_seconds"]
    rows_bytes = rows_path.read_bytes()
    rows_sha256 = _sha256(rows_bytes)
    if rows_sha256 != protocol["universe"].get("selection_jsonl_sha256"):
        raise MaterializationError("canonical JSONL does not match the acquisition freeze")
    rows = _load_jsonl(rows_path)
    if len(rows) != int(protocol["universe"]["expected_rows"]):
        raise MaterializationError("canonical JSONL row count does not match the protocol")
    selection_bytes = selection_path.read_bytes()
    selection = _load_json(selection_path)
    if selection_bytes != _canonical_bytes(selection):
        raise MaterializationError("private selection must use canonical JSON bytes")
    ledger = holdout.reject_historical_exposures(
        [holdout.evaluate_row(row, protocol) for row in rows],
        exposure_document,
    )
    if selection != holdout.select_rows(ledger, protocol):
        raise MaterializationError("private selection does not match deterministic selection")
    try:
        holdout.require_exposure_disjoint_selection(selection, exposure_document)
    except ValueError as exc:
        raise MaterializationError("private selection intersects historical exposure") from exc
    selected_ids, repository_map = holdout._validated_selection(selection, protocol)
    if len(selected_ids) != SCENARIO_COUNT or len(set(repository_map.values())) != SCENARIO_COUNT:
        raise MaterializationError(
            "V2 materialization requires exactly ten tasks from ten repositories"
        )
    rows_by_id = {str(row.get("instance_id") or ""): row for row in rows}
    if len(rows_by_id) != len(rows) or any(item not in rows_by_id for item in selected_ids):
        raise MaterializationError("selected rows are missing or duplicated")
    try:
        sources, _source_map_sha256 = freezer.validate_source_map(source_map_path)
    except freezer.FreezeError as exc:
        raise MaterializationError(f"private source map is invalid: {exc}") from exc
    expected_source_urls = {
        holdout.canonical_repo_url(str(rows_by_id[scenario_id]["repo"]))
        for scenario_id in selected_ids
    }
    if set(sources) != expected_source_urls:
        raise MaterializationError("private source map does not match selected repositories")
    if _sha256(runtime_availability_path.read_bytes()) != protocol["product_inputs"].get(
        "runtime_availability_sha256"
    ):
        raise MaterializationError("runtime availability does not match the product freeze")
    if _sha256(catalog_archive_path.read_bytes()) != protocol["product_inputs"].get(
        "catalog_archive_sha256"
    ):
        raise MaterializationError("catalog archive does not match the product freeze")
    runtime = VerifierRuntime(
        swebench_checkout=swebench_checkout,
        swebench_python=swebench_python,
        docker_cli=docker_cli,
        docker_host=docker_host,
    )
    _validate_runtime(runtime)
    _validate_output_destination(output)

    scenario_rows: list[dict[str, Any]] = []
    scenarios: list[benchmark.Scenario] = []
    controls: dict[str, Any] = {}
    reconstructed: dict[str, str] = {}
    retained_evidence = Path(tempfile.mkdtemp(prefix=".official-verification-", dir=output.parent))
    os.chmod(retained_evidence, 0o700)
    try:
        with tempfile.TemporaryDirectory(prefix="ctx-holdout-materialize-") as raw_work:
            work_root = Path(raw_work)
            source_preflight_deadline = time.monotonic() + float(timeout)
            for source_bundle in sources.values():
                _validate_source_bundle_heads(
                    source_bundle,
                    cwd=work_root,
                    deadline=source_preflight_deadline,
                )
            for slot, scenario_id in enumerate(selected_ids):
                row = rows_by_id[scenario_id]
                scenario_row, scenario, control, reconstructed_source = _scenario(
                    row,
                    dataset_path=rows_path.resolve(strict=True),
                    protocol=protocol,
                    pins=pins,
                    retained_root=retained_evidence,
                    runtime=runtime,
                    source=_repo_source(row, sources),
                    slot=slot,
                    work_root=work_root,
                    timeout=float(timeout),
                )
                scenario_rows.append(scenario_row)
                scenarios.append(scenario)
                controls[scenario_id] = control
                reconstructed[scenario_id] = reconstructed_source

        scenario_pack_bytes = _canonical_bytes({"scenarios": scenario_rows, "version": 1})
        scenario_pack_sha256 = _sha256(scenario_pack_bytes)
        collision: dict[str, Any] = dict(
            benchmark.validate_runtime_pack_scenario_independence(
                scenarios,
                availability_path=runtime_availability_path,
                archive_path=catalog_archive_path,
            )
        )
        collision.update(
            {
                "collision_count": 0,
                "collision_free": True,
                "scenario_ids": sorted(selected_ids),
                "scenarios_sha256": scenario_pack_sha256,
            }
        )
        reconstructed_attestation = holdout.build_reconstructed_test_attestation(
            selection,
            protocol,
            reconstructed,
        )
        selection_sha256 = _sha256(selection_bytes)
        control_results = {
            "all_scenarios_passed": len(controls) == SCENARIO_COUNT,
            "guard": "holdout-control-results-v1",
            "scenario_count": SCENARIO_COUNT,
            "scenario_results": controls,
            "scenario_pack_sha256": scenario_pack_sha256,
            "selection_sha256": selection_sha256,
            "verifier_pins_sha256": _sha256(_canonical_bytes(pins)),
        }
        artifacts = {
            "scenario_pack": scenario_pack_bytes,
            "collision": _canonical_bytes(collision),
            "reconstructed": _canonical_bytes(reconstructed_attestation),
            "controls": _canonical_bytes(control_results),
        }
        _write_artifacts(
            output,
            artifacts,
            retained_evidence=retained_evidence,
        )
        return {key: _sha256(value) for key, value in artifacts.items()}
    finally:
        if retained_evidence.exists():
            shutil.rmtree(retained_evidence, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--expected-acquisition-protocol-sha256", required=True)
    parser.add_argument("--exposure-ledger", type=Path, required=True)
    parser.add_argument("--rows", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--source-map", type=Path, required=True)
    parser.add_argument("--runtime-availability", type=Path, required=True)
    parser.add_argument("--catalog-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--swebench-checkout", type=Path, required=True)
    parser.add_argument("--swebench-python", type=Path, required=True)
    parser.add_argument("--docker-cli", type=Path, required=True)
    parser.add_argument("--docker-host", required=True)
    args = parser.parse_args(argv)
    try:
        hashes = materialize(
            protocol_path=args.protocol,
            expected_acquisition_protocol_sha256=args.expected_acquisition_protocol_sha256,
            exposure_ledger_path=args.exposure_ledger,
            rows_path=args.rows,
            selection_path=args.selection,
            source_map_path=args.source_map,
            runtime_availability_path=args.runtime_availability,
            catalog_archive_path=args.catalog_archive,
            output=args.output,
            swebench_checkout=args.swebench_checkout,
            swebench_python=args.swebench_python,
            docker_cli=args.docker_cli,
            docker_host=args.docker_host,
        )
        controls = _load_json(args.output / OUTPUT_FILES["controls"])
        scenario_count = controls["scenario_count"]
    except (MaterializationError, ValueError, OSError, KeyError) as exc:
        parser.exit(
            2, f"materialization failed; private details suppressed ({type(exc).__name__})\n"
        )
    print(
        f"materialized {scenario_count} private scenarios; "
        + " ".join(f"{key}_sha256={value}" for key, value in sorted(hashes.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
