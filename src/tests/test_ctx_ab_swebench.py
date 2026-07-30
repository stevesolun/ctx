from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import time
from typing import Any, cast
import weakref

import pytest

from scripts import ctx_ab_benchmark as benchmark
from scripts import ctx_ab_swebench as bridge


REVISION = "a" * 40
FAIL_TO_PASS = ["tests/test_feature.py::test_new_behavior"]
PASS_TO_PASS = ["tests/test_feature.py::test_existing_behavior"]
DAEMON_ID = "ctx-test-daemon"
SERVER_VERSION = "28.3.2"
IMAGE_ID = f"sha256:{'b' * 64}"
PYTHON_ENVIRONMENT = "docker==7.1.0\nswebench==4.1.0\n"
PYTHON_ENVIRONMENT_SHA256 = bridge._sha256(
    bridge._canonical_bytes(sorted(PYTHON_ENVIRONMENT.splitlines()))
)
DOCKER_PACKAGE_CONTENT = b"# authenticated docker fixture\n"
DOCKER_PACKAGE_MANIFEST = {
    "file_count": 1,
    "sha256": bridge._sha256(
        bridge._canonical_bytes(
            [
                {
                    "bytes": len(DOCKER_PACKAGE_CONTENT),
                    "path": "__init__.py",
                    "sha256": bridge._sha256(DOCKER_PACKAGE_CONTENT),
                }
            ]
        )
    ),
}
DOCKER_PACKAGE_SHA256 = str(DOCKER_PACKAGE_MANIFEST["sha256"])


def test_verifier_process_runner_is_self_contained(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def poisoned_runner(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("mutable benchmark process runner executed")

    monkeypatch.setattr(benchmark, "run_process", poisoned_runner)

    result = bridge._run_process(
        [sys.executable, "-c", "print('authenticated')"],
        cwd=tmp_path,
        timeout=10,
    )

    assert isinstance(result, bridge.CommandResult)
    assert result.returncode == 0
    assert result.stdout == "authenticated\n"


def test_windows_process_launch_and_tree_signal_use_native_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 4321
        returncode = 0
        stdout = None
        stderr = None

        def communicate(
            self,
            input: str | None = None,
            timeout: float | None = None,
        ) -> tuple[str, str]:
            captured["communicate"] = (input, timeout)
            return "ok\n", ""

        def kill(self) -> None:
            captured["killed"] = True

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProcess:
        captured["argv"] = argv
        captured["popen"] = kwargs
        return FakeProcess()

    def fake_taskkill(argv: list[str], **kwargs: Any) -> None:
        captured["taskkill"] = (argv, kwargs)

    monkeypatch.setattr(bridge.os, "name", "nt")
    monkeypatch.setattr(bridge.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(bridge.subprocess, "run", fake_taskkill)
    monkeypatch.setattr(
        bridge.os,
        "killpg",
        lambda *_args: pytest.fail("Windows must not call os.killpg"),
    )

    result = bridge._run_process(["worker"], cwd=tmp_path, timeout=7)
    bridge._signal_process_tree(
        cast(Any, FakeProcess()),
        signal.SIGTERM,
    )

    assert result.returncode == 0
    assert captured["popen"]["creationflags"] == 512
    assert "start_new_session" not in captured["popen"]
    assert captured["taskkill"][0] == ["taskkill", "/PID", "4321", "/T", "/F"]


def _report_entry(*, resolved: bool) -> dict[str, Any]:
    if resolved:
        fail_success, fail_failure = FAIL_TO_PASS, []
    else:
        fail_success, fail_failure = [], FAIL_TO_PASS
    return {
        "patch_is_None": False,
        "patch_exists": True,
        "patch_successfully_applied": True,
        "resolved": resolved,
        "tests_status": {
            "FAIL_TO_PASS": {"success": fail_success, "failure": fail_failure},
            "PASS_TO_PASS": {"success": PASS_TO_PASS, "failure": []},
            "FAIL_TO_FAIL": {"success": [], "failure": []},
            "PASS_TO_FAIL": {"success": [], "failure": []},
        },
    }


@pytest.mark.parametrize("phase", ["green", "scored"])
def test_exact_red_green_and_scored_status_contract(phase: str) -> None:
    red = bridge.validate_outcome(
        phase="red",
        fail_to_pass=FAIL_TO_PASS,
        pass_to_pass=PASS_TO_PASS,
        statuses={
            FAIL_TO_PASS[0]: "FAILED",
            PASS_TO_PASS[0]: "PASSED",
        },
        report_entry=_report_entry(resolved=False),
        official_resolution="RESOLVED_NO",
    )
    passing = bridge.validate_outcome(
        phase=phase,
        fail_to_pass=FAIL_TO_PASS,
        pass_to_pass=PASS_TO_PASS,
        statuses={
            FAIL_TO_PASS[0]: "PASSED",
            PASS_TO_PASS[0]: "XFAIL",
        },
        report_entry=_report_entry(resolved=True),
        official_resolution="RESOLVED_FULL",
    )

    assert red["resolved"] is False
    assert red["fail_to_pass_count"] == 1
    assert passing["resolved"] is True
    assert passing["pass_to_pass_count"] == 1


@pytest.mark.parametrize(
    ("fail_to_pass", "pass_to_pass", "statuses"),
    [
        (FAIL_TO_PASS, PASS_TO_PASS, {FAIL_TO_PASS[0]: "FAILED"}),
        (
            FAIL_TO_PASS,
            PASS_TO_PASS,
            {FAIL_TO_PASS[0]: "SKIPPED", PASS_TO_PASS[0]: "PASSED"},
        ),
        (
            FAIL_TO_PASS * 2,
            PASS_TO_PASS,
            {FAIL_TO_PASS[0]: "FAILED", PASS_TO_PASS[0]: "PASSED"},
        ),
        (
            FAIL_TO_PASS,
            PASS_TO_PASS,
            {
                FAIL_TO_PASS[0]: "FAILED",
                PASS_TO_PASS[0]: "PASSED",
                "tests/test_feature.py::test_unexpected": "PASSED",
            },
        ),
    ],
)
def test_missing_duplicate_skipped_and_extra_statuses_fail_closed(
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    statuses: dict[str, str],
) -> None:
    with pytest.raises(bridge.SWEbenchVerificationError):
        bridge.validate_outcome(
            phase="red",
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            statuses=statuses,
            report_entry=_report_entry(resolved=False),
            official_resolution="RESOLVED_NO",
        )


def test_file_qualified_status_aliases_must_agree() -> None:
    statuses = {
        FAIL_TO_PASS[0]: "FAILED",
        f"project/tests/test_feature.py:{FAIL_TO_PASS[0]}": "FAILED",
        PASS_TO_PASS[0]: "PASSED",
    }
    outcome = bridge.validate_outcome(
        phase="red",
        fail_to_pass=FAIL_TO_PASS,
        pass_to_pass=PASS_TO_PASS,
        statuses=statuses,
        report_entry=_report_entry(resolved=False),
        official_resolution="RESOLVED_NO",
    )
    assert outcome["parsed_status_key_count"] == 3

    statuses[f"project/tests/test_feature.py:{FAIL_TO_PASS[0]}"] = "PASSED"
    with pytest.raises(bridge.SWEbenchVerificationError, match="conflict"):
        bridge.validate_outcome(
            phase="red",
            fail_to_pass=FAIL_TO_PASS,
            pass_to_pass=PASS_TO_PASS,
            statuses=statuses,
            report_entry=_report_entry(resolved=False),
            official_resolution="RESOLVED_NO",
        )

    with pytest.raises(bridge.SWEbenchVerificationError, match="missing"):
        bridge.validate_outcome(
            phase="red",
            fail_to_pass=FAIL_TO_PASS,
            pass_to_pass=PASS_TO_PASS,
            statuses={
                f"project/tests/test_feature.py:{FAIL_TO_PASS[0]}": "FAILED",
                f"project/tests/test_feature.py:{PASS_TO_PASS[0]}": "PASSED",
            },
            report_entry=_report_entry(resolved=False),
            official_resolution="RESOLVED_NO",
        )


def test_mode_only_patch_requires_tracked_allowed_production_python() -> None:
    selected = bridge.select_mode_target(
        ["tests/test_core.py", "pkg/test.py", "pkg/core.py", "pkg/already_executable.py"],
        {
            "tests/test_core.py": "100644",
            "pkg/test.py": "100644",
            "pkg/core.py": "100644",
            "pkg/already_executable.py": "100755",
        },
    )

    assert selected == "pkg/core.py"
    assert bridge.mode_only_patch(selected) == (
        "diff --git a/pkg/core.py b/pkg/core.py\nold mode 100644\nnew mode 100755\n"
    )
    with pytest.raises(bridge.SWEbenchVerificationError, match="safe mode-only target"):
        bridge.select_mode_target(
            ["tests/test_core.py", "pkg/test.py", "pkg/already_executable.py"],
            {
                "tests/test_core.py": "100644",
                "pkg/test.py": "100644",
                "pkg/already_executable.py": "100755",
            },
        )


@dataclass(frozen=True)
class Inputs:
    checkout: Path
    python: Path
    docker: Path
    dataset: Path
    socket_path: Path
    run_dir: Path
    run_evaluation_sha256: str
    socket_cleanup: Any


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_executable_authentication_preserves_virtualenv_entrypoint(tmp_path: Path) -> None:
    target = _executable(tmp_path / "python3.12")
    entrypoint = tmp_path / "python"
    entrypoint.symlink_to(target.name)

    assert bridge._executable(entrypoint.absolute(), label="Python") == entrypoint.absolute()


def _inputs(tmp_path: Path) -> Inputs:
    checkout = tmp_path / "SWE-bench"
    run_evaluation = checkout / "swebench/harness/run_evaluation.py"
    run_evaluation.parent.mkdir(parents=True)
    run_evaluation.write_text("# pinned harness\n", encoding="utf-8")
    python = _executable(tmp_path / "swebench-python")
    docker = _executable(tmp_path / "docker")
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    socket_path = Path("/tmp") / (
        "ctx-sb-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12] + ".sock"
    )
    socket_path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.close()
    inputs = Inputs(
        checkout=checkout,
        python=python,
        docker=docker,
        dataset=dataset,
        socket_path=socket_path,
        run_dir=tmp_path / "private-run",
        run_evaluation_sha256=hashlib.sha256(run_evaluation.read_bytes()).hexdigest(),
        socket_cleanup=None,
    )
    object.__setattr__(
        inputs,
        "socket_cleanup",
        weakref.finalize(inputs, socket_path.unlink, missing_ok=True),
    )
    return inputs


def _write_worker_artifacts(request: dict[str, Any], run_dir: Path) -> None:
    phase = str(request["phase"])
    instance_id = str(request["instance_id"])
    model_name = str(request["model_name"])
    log_dir = run_dir / "logs/run_evaluation" / str(request["run_id"]) / model_name / instance_id
    log_dir.mkdir(parents=True)
    resolved = phase != "red"
    statuses = {
        FAIL_TO_PASS[0]: "PASSED" if resolved else "ERROR",
        PASS_TO_PASS[0]: "PASSED",
    }
    report = {instance_id: _report_entry(resolved=resolved)}
    raw = {
        "schema_version": 1,
        "phase": phase,
        "instance_id": instance_id,
        "expected": {
            "FAIL_TO_PASS": FAIL_TO_PASS,
            "PASS_TO_PASS": PASS_TO_PASS,
        },
        "statuses": statuses,
        "official_resolution": "RESOLVED_FULL" if resolved else "RESOLVED_NO",
    }
    files = {
        "report.json": json.dumps(report, sort_keys=True),
        "raw-status.json": json.dumps(raw, sort_keys=True),
        "eval.sh": "#!/bin/bash\npytest\n",
        "test_output.txt": "synthetic official output\n",
        "run_instance.log": "patch applied\n",
        "patch.diff": bridge.mode_only_patch("pkg/core.py")
        if phase == "red"
        else "diff --git a/pkg/core.py b/pkg/core.py\n",
    }
    for name, content in files.items():
        (log_dir / name).write_text(content, encoding="utf-8")
    if phase == "red":
        (run_dir / "mode-probe.log").write_text("tracked 100644 target\n", encoding="utf-8")
    result_path = run_dir / "worker-result.json"
    result_path.write_text(
        json.dumps(
            {
                "container_policy": [
                    {
                        "cap_drop": ["ALL"],
                        "cgroupns_mode": "private",
                        "image_id": IMAGE_ID,
                        "memory_bytes": bridge.CONTAINER_MEMORY_BYTES,
                        "mount_count": 0,
                        "nano_cpus": bridge.CONTAINER_NANO_CPUS,
                        "network_mode": "none",
                        "pids_limit": bridge.CONTAINER_PIDS_LIMIT,
                        "privileged": False,
                        "security_opt": bridge.CONTAINER_SECURITY_OPT,
                    }
                ],
                "image_id": IMAGE_ID,
                "ok": True,
                "phase": phase,
                "run_id": request["run_id"],
                "model_name": model_name,
                "log_dir": str(log_dir.relative_to(run_dir)),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _verify_kwargs(inputs: Inputs) -> dict[str, Any]:
    return {
        "phase": "red",
        "dataset_path": inputs.dataset,
        "instance_id": "owner__repo-1",
        "allowed_paths": ["pkg/core.py"],
        "swebench_checkout": inputs.checkout,
        "swebench_python": inputs.python,
        "expected_revision": REVISION,
        "expected_run_evaluation_sha256": inputs.run_evaluation_sha256,
        "expected_bridge_sha256": hashlib.sha256(bridge.SCRIPT_PATH.read_bytes()).hexdigest(),
        "expected_dataset_sha256": hashlib.sha256(inputs.dataset.read_bytes()).hexdigest(),
        "expected_python_sha256": hashlib.sha256(inputs.python.resolve().read_bytes()).hexdigest(),
        "expected_python_environment_sha256": PYTHON_ENVIRONMENT_SHA256,
        "expected_docker_package_sha256": DOCKER_PACKAGE_SHA256,
        "docker_cli": inputs.docker,
        "expected_docker_cli_sha256": hashlib.sha256(
            inputs.docker.resolve().read_bytes()
        ).hexdigest(),
        "docker_host": f"unix://{inputs.socket_path}",
        "expected_docker_daemon_id": DAEMON_ID,
        "expected_docker_server_version": SERVER_VERSION,
        "work_dir": inputs.run_dir,
        "timeout": 30,
        "allow_image_pull": True,
        "namespace": "swebench",
    }


def _fake_package_snapshot(argv: list[str]) -> benchmark.CommandResult:
    destination = Path(argv[argv.index("--destination") + 1])
    destination.mkdir(parents=True)
    (destination / "__init__.py").write_bytes(DOCKER_PACKAGE_CONTENT)
    return benchmark.CommandResult(
        0,
        json.dumps(DOCKER_PACKAGE_MANIFEST),
        "",
        0.01,
    )


def _successful_process_fake(
    inputs: Inputs,
    calls: list[tuple[list[str], dict[str, Any]]],
) -> Any:
    def fake(argv: list[str], **kwargs: Any) -> benchmark.CommandResult:
        calls.append((list(argv), dict(kwargs)))
        if "clone" in argv:
            destination = Path(argv[-1])
            run_evaluation = destination / "swebench/harness/run_evaluation.py"
            run_evaluation.parent.mkdir(parents=True)
            run_evaluation.write_text("# pinned harness\n", encoding="utf-8")
            return benchmark.CommandResult(0, "", "", 0.01)
        if "checkout" in argv:
            return benchmark.CommandResult(0, "", "", 0.01)
        if "rev-parse" in argv:
            return benchmark.CommandResult(0, f"{REVISION}\n", "", 0.01)
        if "status" in argv:
            return benchmark.CommandResult(0, "", "", 0.01)
        if argv[0] == str(inputs.python.resolve()) and "pip" in argv:
            return benchmark.CommandResult(0, PYTHON_ENVIRONMENT, "", 0.01)
        if argv[0] == str(inputs.python.resolve()) and "package-snapshot" in argv:
            return _fake_package_snapshot(argv)
        if argv[0] == str(inputs.python.resolve()):
            request_path = Path(argv[argv.index("--request") + 1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            _write_worker_artifacts(request, inputs.run_dir)
            return benchmark.CommandResult(0, "worker output\n", "", 0.25)
        if argv[0] == str(inputs.docker.resolve()):
            if "info" in argv:
                return benchmark.CommandResult(0, f"{DAEMON_ID}\n", "", 0.01)
            if "version" in argv:
                return benchmark.CommandResult(0, f"{SERVER_VERSION}\n", "", 0.01)
            if "network" in argv:
                return benchmark.CommandResult(0, "network-a\n", "", 0.01)
            return benchmark.CommandResult(0, "", "", 0.01)
        raise AssertionError(f"unexpected command: {argv}")

    return fake


def test_worker_environment_is_hermetic_and_retained_evidence_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setenv("HF_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(bridge, "_run_process", _successful_process_fake(inputs, calls))
    monkeypatch.setattr(bridge, "_new_run_id", lambda _phase: "ctx-sb-red-fixed")

    evidence = bridge.verify_swebench(**_verify_kwargs(inputs))

    worker_call = next(call for call in calls if "worker" in call[0])
    worker_env = worker_call[1]["env"]
    request_path = Path(worker_call[0][worker_call[0].index("--request") + 1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert worker_call[1]["contain_descendants"] is True
    assert worker_call[1]["cwd"] == inputs.run_dir
    assert worker_env["PYTHONPATH"] == str(inputs.run_dir / "inputs" / f"swebench-{REVISION}")
    assert worker_env["DOCKER_HOST"] == f"unix://{inputs.socket_path.resolve()}"
    assert worker_env["HF_DATASETS_OFFLINE"] == "1"
    assert worker_env["HF_HUB_OFFLINE"] == "1"
    assert Path(worker_call[0][1]).parent == inputs.run_dir / "inputs"
    assert Path(request["dataset_path"]).parent == inputs.run_dir / "inputs"
    assert Path(request["dataset_path"]) != inputs.dataset
    assert Path(request["docker_package_path"]).name == "docker"
    assert Path(request["swebench_checkout"]).parent == inputs.run_dir / "inputs"
    assert "HF_TOKEN" not in worker_env
    assert "OPENAI_API_KEY" not in worker_env
    assert set(evidence["artifacts"]) >= {
        "report.json",
        "raw-status.json",
        "eval.sh",
        "test_output.txt",
        "run_instance.log",
        "patch.diff",
    }
    for artifact in evidence["artifacts"].values():
        assert artifact["present"] is True
        assert artifact["sha256"]
        assert "content" not in artifact
    assert evidence["command"]["executable"] == inputs.python.resolve().name
    assert evidence["command"]["sha256"]
    assert evidence["process"]["stdout_bytes"] == len("worker output\n")
    assert evidence["process"]["sha256"]
    assert evidence["cleanup"]["ok"] is True
    serialized = json.dumps(evidence)
    assert FAIL_TO_PASS[0] not in serialized
    assert PASS_TO_PASS[0] not in serialized
    assert "must-not-leak" not in serialized


@pytest.mark.parametrize("target", ["bridge", "dataset", "docker", "harness"])
def test_private_input_mutation_between_worker_start_and_parent_validation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    inputs = _inputs(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    base = _successful_process_fake(inputs, calls)

    def fake(argv: list[str], **kwargs: Any) -> benchmark.CommandResult:
        if "worker" in argv:
            request_path = Path(argv[argv.index("--request") + 1])
            request = json.loads(request_path.read_text(encoding="utf-8"))
            mutation = {
                "bridge": Path(argv[1]),
                "dataset": Path(request["dataset_path"]),
                "docker": Path(request["docker_package_path"]) / "__init__.py",
                "harness": (
                    Path(request["swebench_checkout"])
                    / "swebench"
                    / "harness"
                    / "run_evaluation.py"
                ),
            }[target]
            mutation.chmod(0o600)
            mutation.write_bytes(mutation.read_bytes() + b"# mutation\n")
        return base(argv, **kwargs)

    monkeypatch.setattr(bridge, "_run_process", fake)
    monkeypatch.setattr(bridge, "_new_run_id", lambda _phase: f"ctx-sb-red-mutate-{target}")

    with pytest.raises(bridge.SWEbenchVerificationError, match="inputs drifted"):
        bridge.verify_swebench(**_verify_kwargs(inputs))


@pytest.mark.parametrize("stale", ["revision", "run-evaluation"])
def test_stale_harness_authentication_rejects_before_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stale: str,
) -> None:
    inputs = _inputs(tmp_path)
    calls: list[list[str]] = []

    def fake(argv: list[str], **_kwargs: Any) -> benchmark.CommandResult:
        calls.append(list(argv))
        if "clone" in argv:
            destination = Path(argv[-1])
            run_evaluation = destination / "swebench/harness/run_evaluation.py"
            run_evaluation.parent.mkdir(parents=True)
            run_evaluation.write_text("# pinned harness\n", encoding="utf-8")
            return benchmark.CommandResult(0, "", "", 0.01)
        if "checkout" in argv:
            return benchmark.CommandResult(0, "", "", 0.01)
        if "rev-parse" in argv:
            observed = "b" * 40 if stale == "revision" else REVISION
            return benchmark.CommandResult(0, f"{observed}\n", "", 0.01)
        if "status" in argv:
            return benchmark.CommandResult(0, "", "", 0.01)
        if argv[0] == str(inputs.python.resolve()) and "pip" in argv:
            return benchmark.CommandResult(0, PYTHON_ENVIRONMENT, "", 0.01)
        if argv[0] == str(inputs.python.resolve()) and "package-snapshot" in argv:
            return _fake_package_snapshot(argv)
        raise AssertionError("worker must not start")

    monkeypatch.setattr(bridge, "_run_process", fake)
    kwargs = _verify_kwargs(inputs)
    if stale == "run-evaluation":
        kwargs["expected_run_evaluation_sha256"] = "0" * 64

    with pytest.raises(bridge.SWEbenchVerificationError, match="authentication"):
        bridge.verify_swebench(**kwargs)

    assert not any("worker" in command for command in calls)


def test_timeout_still_cleans_docker_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    docker_calls: list[list[str]] = []

    def fake(argv: list[str], **_kwargs: Any) -> benchmark.CommandResult:
        if "clone" in argv:
            destination = Path(argv[-1])
            run_evaluation = destination / "swebench/harness/run_evaluation.py"
            run_evaluation.parent.mkdir(parents=True)
            run_evaluation.write_text("# pinned harness\n", encoding="utf-8")
            return benchmark.CommandResult(0, "", "", 0.01)
        if "checkout" in argv:
            return benchmark.CommandResult(0, "", "", 0.01)
        if "rev-parse" in argv:
            return benchmark.CommandResult(0, f"{REVISION}\n", "", 0.01)
        if "status" in argv:
            return benchmark.CommandResult(0, "", "", 0.01)
        if argv[0] == str(inputs.python.resolve()) and "pip" in argv:
            return benchmark.CommandResult(0, PYTHON_ENVIRONMENT, "", 0.01)
        if argv[0] == str(inputs.python.resolve()) and "package-snapshot" in argv:
            return _fake_package_snapshot(argv)
        if argv[0] == str(inputs.python.resolve()):
            return benchmark.CommandResult(124, "", "timed out", 30.0, timed_out=True)
        if argv[0] == str(inputs.docker.resolve()):
            docker_calls.append(list(argv))
            if "info" in argv:
                return benchmark.CommandResult(0, f"{DAEMON_ID}\n", "", 0.01)
            if "version" in argv:
                return benchmark.CommandResult(0, f"{SERVER_VERSION}\n", "", 0.01)
            if "network" in argv:
                return benchmark.CommandResult(0, "network-a\n", "", 0.01)
            return benchmark.CommandResult(0, "", "", 0.01)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(bridge, "_run_process", fake)
    monkeypatch.setattr(bridge, "_new_run_id", lambda _phase: "ctx-sb-red-timeout")

    with pytest.raises(bridge.SWEbenchVerificationError) as raised:
        bridge.verify_swebench(**_verify_kwargs(inputs))

    assert raised.value.evidence["process"]["timed_out"] is True
    assert raised.value.evidence["cleanup"]["ok"] is True
    audit_path = inputs.run_dir / "verification-evidence.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600
    assert audit["process"]["timed_out"] is True
    assert audit["process"]["residual_descendant_count"] == 0
    assert audit["cleanup"]["ok"] is True
    assert audit["cleanup"]["residual_container_count"] == 0
    assert audit["inventory"]["baseline"]["containers"] == audit["inventory"]["final"]["containers"]
    assert docker_calls


def test_residual_docker_container_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    successful = _successful_process_fake(inputs, calls)

    def fake(argv: list[str], **kwargs: Any) -> benchmark.CommandResult:
        if argv[0] == str(inputs.docker.resolve()):
            calls.append((list(argv), dict(kwargs)))
            if "info" in argv:
                return benchmark.CommandResult(0, f"{DAEMON_ID}\n", "", 0.01)
            if "version" in argv:
                return benchmark.CommandResult(0, f"{SERVER_VERSION}\n", "", 0.01)
            if "network" in argv:
                return benchmark.CommandResult(0, "network-a\n", "", 0.01)
            if "ps" in argv:
                return benchmark.CommandResult(0, "abc123def456\n", "", 0.01)
            if "rm" in argv:
                return benchmark.CommandResult(0, "abc123def456\n", "", 0.01)
        return successful(argv, **kwargs)

    monkeypatch.setattr(bridge, "_run_process", fake)
    monkeypatch.setattr(bridge, "_new_run_id", lambda _phase: "ctx-sb-red-residual")

    with pytest.raises(bridge.SWEbenchVerificationError, match="containment") as raised:
        bridge.verify_swebench(**_verify_kwargs(inputs))

    assert raised.value.evidence["cleanup"]["ok"] is False
    assert raised.value.evidence["cleanup"]["residual_container_ids"] == ["abc123def456"]


def test_scored_patch_cannot_change_hidden_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    successful = _successful_process_fake(inputs, calls)

    def fake(argv: list[str], **kwargs: Any) -> benchmark.CommandResult:
        if "apply" in argv and "--numstat" in argv:
            return benchmark.CommandResult(
                0,
                "1\t1\ttests/test_feature.py\0",
                "",
                0.01,
            )
        if "apply" in argv and "--summary" in argv:
            return benchmark.CommandResult(0, "", "", 0.01)
        return successful(argv, **kwargs)

    monkeypatch.setattr(bridge, "_run_process", fake)
    kwargs = _verify_kwargs(inputs)
    kwargs.update(
        {
            "allow_image_pull": False,
            "expected_image_id": IMAGE_ID,
            "model_patch": (
                "diff --git a/tests/test_feature.py b/tests/test_feature.py\n"
                "--- a/tests/test_feature.py\n"
                "+++ b/tests/test_feature.py\n"
                "@@ -1 +1 @@\n-assert False\n+assert True\n"
            ),
            "phase": "scored",
        }
    )

    with pytest.raises(bridge.SWEbenchVerificationError, match="disallowed path"):
        bridge.verify_swebench(**kwargs)

    assert not any("worker" in call[0] for call in calls)


def test_scored_patch_cannot_rename_hidden_tests_into_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(tmp_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    successful = _successful_process_fake(inputs, calls)

    def fake(argv: list[str], **kwargs: Any) -> benchmark.CommandResult:
        if "apply" in argv and "--numstat" in argv:
            return benchmark.CommandResult(0, "0\t0\tpkg/core.py\0", "", 0.01)
        if "apply" in argv and "--summary" in argv:
            return benchmark.CommandResult(
                0,
                " rename tests/test_feature.py => pkg/core.py (100%)\n",
                "",
                0.01,
            )
        return successful(argv, **kwargs)

    monkeypatch.setattr(bridge, "_run_process", fake)
    kwargs = _verify_kwargs(inputs)
    kwargs.update(
        {
            "allow_image_pull": False,
            "expected_image_id": IMAGE_ID,
            "model_patch": (
                "diff --git a/tests/test_feature.py b/pkg/core.py\n"
                "similarity index 100%\n"
                "rename from tests/test_feature.py\n"
                "rename to pkg/core.py\n"
            ),
            "phase": "scored",
        }
    )

    with pytest.raises(bridge.SWEbenchVerificationError, match="renames or copies"):
        bridge.verify_swebench(**kwargs)

    assert not any("worker" in call[0] for call in calls)


def test_authenticated_harness_is_clean_commit_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess_commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "benchmark@example.invalid"],
        ["git", "config", "user.name", "Benchmark"],
    ]
    for command in subprocess_commands:
        result = benchmark.run_process(command, cwd=source, timeout=10)
        assert result.returncode == 0
    run_evaluation = source / "swebench/harness/run_evaluation.py"
    run_evaluation.parent.mkdir(parents=True)
    run_evaluation.write_text("# committed\n", encoding="utf-8")
    executable = source / "swebench/harness/run-evaluation"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    assert benchmark.run_process(["git", "add", "."], cwd=source, timeout=10).returncode == 0
    assert (
        benchmark.run_process(
            ["git", "commit", "-qm", "fixture"], cwd=source, timeout=10
        ).returncode
        == 0
    )
    revision = benchmark.run_process(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        timeout=10,
    ).stdout.strip()
    run_evaluation.write_text("# malicious tracked edit\n", encoding="utf-8")
    ignored = source / "swebench/harness/grading.pyc"
    ignored.write_bytes(b"malicious ignored bytecode")
    destination = tmp_path / "clean"

    evidence = bridge._materialize_authenticated_harness(
        source_checkout=source,
        destination=destination,
        git=bridge._git_executable(),
        expected_revision=revision,
        expected_run_evaluation_sha256=hashlib.sha256(b"# committed\n").hexdigest(),
        deadline=time.monotonic() + 30,
    )

    assert (destination / "swebench/harness/run_evaluation.py").read_text() == "# committed\n"
    assert not (destination / "swebench/harness/grading.pyc").exists()
    assert destination.joinpath("swebench/harness/run-evaluation").stat().st_mode & 0o111
    assert (
        benchmark.run_process(
            ["git", "status", "--porcelain=v1"],
            cwd=destination,
            timeout=10,
        ).stdout
        == ""
    )
    assert evidence["git_revision"] == revision


def test_worker_rejects_bridge_hash_drift_before_import(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    run_evaluation = checkout / "swebench/harness/run_evaluation.py"
    run_evaluation.parent.mkdir(parents=True)
    run_evaluation.write_text("# pinned\n", encoding="utf-8")
    git = _executable(tmp_path / "git")

    with pytest.raises(bridge.SWEbenchVerificationError, match="authentication"):
        bridge._worker_authenticate(
            {
                "expected_bridge_sha256": "0" * 64,
                "expected_revision": REVISION,
                "expected_run_evaluation_sha256": hashlib.sha256(
                    run_evaluation.read_bytes()
                ).hexdigest(),
                "git_cli": str(git.absolute()),
                "swebench_checkout": str(checkout.absolute()),
            }
        )


def test_official_instance_uses_readable_umask_and_restores_worker_umask(
    tmp_path: Path,
) -> None:
    observed: dict[str, int] = {}

    class FakeEvaluation:
        @staticmethod
        def run_instance(path: Path) -> str:
            artifact = path / "patch.diff"
            artifact.write_text("patch\n", encoding="utf-8")
            observed["mode"] = artifact.stat().st_mode & 0o777
            return "ok"

    original = os.umask(0o077)
    try:
        assert bridge._run_official_instance(FakeEvaluation, tmp_path) == "ok"
        worker_umask = os.umask(0o077)
        os.umask(worker_umask)
    finally:
        os.umask(original)

    assert observed["mode"] == 0o644
    assert worker_umask == 0o077


class _FakeContainer:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def reload(self) -> None:
        return None


class _FakeCollection:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def create(self, *_args: Any, **kwargs: Any) -> _FakeContainer:
        self.kwargs = kwargs
        container = _FakeContainer()
        container.attrs = {
            "Config": {"Labels": kwargs["labels"]},
            "HostConfig": {
                "Binds": None,
                "CapDrop": kwargs["cap_drop"],
                "CgroupnsMode": kwargs["cgroupns"],
                "Memory": kwargs["mem_limit"],
                "NanoCpus": kwargs["nano_cpus"],
                "NetworkMode": kwargs["network_mode"],
                "PidsLimit": kwargs["pids_limit"],
                "Privileged": kwargs["privileged"],
                "SecurityOpt": kwargs["security_opt"],
            },
            "Image": kwargs["image"],
            "Mounts": [],
        }
        return container


class _FakeDockerClient:
    def __init__(self) -> None:
        self.containers = _FakeCollection()


def test_container_creation_enforces_network_resources_and_no_mounts() -> None:
    client = _FakeDockerClient()
    attestations: list[dict[str, Any]] = []
    hardened = bridge._HardenedDockerClient(
        client,
        run_id="ctx-sb-red-policy",
        expected_image_id=IMAGE_ID,
        attestations=attestations,
    )
    hardened.containers.create(image="fixture")
    assert client.containers.kwargs["image"] == IMAGE_ID
    assert client.containers.kwargs["cap_drop"] == ["ALL"]
    assert client.containers.kwargs["cgroupns"] == "private"
    assert client.containers.kwargs["network_mode"] == "none"
    assert client.containers.kwargs["mem_limit"] == bridge.CONTAINER_MEMORY_BYTES
    assert client.containers.kwargs["nano_cpus"] == bridge.CONTAINER_NANO_CPUS
    assert client.containers.kwargs["pids_limit"] == bridge.CONTAINER_PIDS_LIMIT
    assert attestations[0]["mount_count"] == 0
    assert attestations[0]["image_id"] == IMAGE_ID
    with pytest.raises(bridge.SWEbenchVerificationError, match="host resources"):
        hardened.containers.create(image="fixture", volumes={"/tmp": {}})
    with pytest.raises(bridge.SWEbenchVerificationError, match="privilege"):
        hardened.containers.create(image="fixture", cap_add=["SYS_ADMIN"])
    with pytest.raises(bridge.SWEbenchVerificationError, match="namespaces"):
        hardened.containers.create(image="fixture", pid_mode="host")
    with pytest.raises(bridge.SWEbenchVerificationError, match="namespaces"):
        hardened.containers.create(image="fixture", cgroupns="host")
    with pytest.raises(bridge.SWEbenchVerificationError, match="namespaces"):
        hardened.containers.create(image="fixture", ipc_mode="host")
