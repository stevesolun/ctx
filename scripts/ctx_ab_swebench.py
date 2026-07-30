#!/usr/bin/env python3
"""Run one authenticated official SWE-bench verification phase in Docker."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any


PHASES = frozenset({"red", "green", "scored"})
PASSING_STATUSES = frozenset({"PASSED", "XFAIL"})
RED_FAILURE_STATUSES = frozenset({"FAILED", "ERROR"})
KNOWN_STATUSES = PASSING_STATUSES | RED_FAILURE_STATUSES | {"SKIPPED"}
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
INSTANCE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,200}$")
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
IMAGE_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
SAFE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
TEST_PATH_COMPONENTS = frozenset({"test", "tests", "testing", "__tests__"})
CONTAINER_MEMORY_BYTES = 8 * 1024 * 1024 * 1024
CONTAINER_NANO_CPUS = 4_000_000_000
CONTAINER_PIDS_LIMIT = 2048
CONTAINER_SECURITY_OPT = "no-new-privileges:true"
RUN_LABEL = "ctx.benchmark.run_id"
PROCESS_MARKER = "CTX_SWEBENCH_PROCESS_TOKEN"
REQUIRED_ARTIFACTS = (
    "report.json",
    "raw-status.json",
    "eval.sh",
    "test_output.txt",
    "run_instance.log",
    "patch.diff",
)
MAX_EVIDENCE_BYTES = 256 * 1024 * 1024
SCRIPT_PATH = Path(__file__).resolve()


class SWEbenchVerificationError(RuntimeError):
    """The official SWE-bench bridge failed closed."""

    def __init__(
        self,
        message: str = "SWE-bench verification failed",
        *,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float
    timed_out: bool = False
    reaped_descendants: int = 0
    residual_descendants: tuple[int, ...] = ()


def _descendant_pids(root_pid: int) -> list[int]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, parent_text = line.split()
            children.setdefault(int(parent_text), []).append(int(pid_text))
        except (ValueError, TypeError):
            continue
    found: list[int] = []
    pending = [root_pid]
    while pending:
        current = pending.pop()
        direct = children.get(current, [])
        found.extend(direct)
        pending.extend(direct)
    return found


def _signal_process_tree(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    descendants = _descendant_pids(process.pid)
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in reversed(descendants):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _terminate_process_tree(process: subprocess.Popen[str]) -> tuple[str, str]:
    _signal_process_tree(process, signal.SIGTERM)
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired as first:
        _signal_process_tree(process, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            stdout = first.stdout if isinstance(first.stdout, str) else ""
            stderr = first.stderr if isinstance(first.stderr, str) else ""
            return stdout, stderr + "\nprocess tree did not reap within 12 seconds"


def _marked_process_pids(token: str) -> tuple[set[int], str | None]:
    try:
        result = subprocess.run(
            ["/bin/ps", "eww", "-A", "-o", "pid=", "-o", "args="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return set(), f"process marker scan failed: {exc}"
    if result.returncode:
        return set(), f"process marker scan exited {result.returncode}"
    marker = f"{PROCESS_MARKER}={token}"
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or marker not in fields[1]:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid != os.getpid():
            pids.add(pid)
    return pids, None


def _cleanup_marked_processes(token: str) -> tuple[int, tuple[int, ...], str | None]:
    pids, error = _marked_process_pids(token)
    if error:
        return 0, (), error
    signaled = set(pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 2
    while pids and time.monotonic() < deadline:
        time.sleep(0.05)
        pids, error = _marked_process_pids(token)
        if error:
            return len(signaled), (), error
        signaled.update(pids)
    for pid in pids:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (ProcessLookupError, PermissionError):
            pass
    if pids:
        time.sleep(0.05)
    residual, error = _marked_process_pids(token)
    return len(signaled), tuple(sorted(residual)), error


def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 600,
    input_text: str | None = None,
    contain_descendants: bool = False,
) -> CommandResult:
    started = time.perf_counter()
    process_token = secrets.token_hex(16) if contain_descendants else None
    child_env = env
    if process_token is not None:
        child_env = dict(os.environ if env is None else env)
        child_env[PROCESS_MARKER] = process_token
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=child_env,
        text=True,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    reaped = 0
    residual: tuple[int, ...] = ()
    cleanup_error: str | None = None

    def cleanup_marked_processes() -> None:
        nonlocal reaped, residual, cleanup_error
        if process_token is None:
            return
        count, remaining, error = _cleanup_marked_processes(process_token)
        reaped += count
        residual = remaining
        cleanup_error = cleanup_error or error

    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        cleanup_marked_processes()
        stdout, stderr = _terminate_process_tree(process)
    except BaseException:
        cleanup_marked_processes()
        _terminate_process_tree(process)
        raise
    finally:
        cleanup_marked_processes()
    returncode = process.returncode or (124 if timed_out else 0)
    if cleanup_error or residual:
        returncode = returncode or 125
        detail = cleanup_error or f"residual descendants: {list(residual)}"
        stderr = f"{stderr}\nprocess containment failed: {detail}".lstrip()
    return CommandResult(
        returncode,
        stdout,
        stderr,
        time.perf_counter() - started,
        timed_out=timed_out,
        reaped_descendants=reaped,
        residual_descendants=residual,
    )


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SWEbenchVerificationError("SWE-bench verification deadline expired")
    return remaining


def _command_evidence(argv: Sequence[str]) -> dict[str, Any]:
    preimage = {"argv": list(argv)}
    encoded = _canonical_bytes(preimage)
    return {
        "argc": len(argv),
        "bytes": len(encoded),
        "executable": Path(argv[0]).name if argv else None,
        "sha256": _sha256(encoded),
    }


def _process_evidence(result: Any | None, *, error_type: str | None = None) -> dict[str, Any]:
    if result is None:
        preimage: dict[str, Any] = {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "elapsed": 0.0,
            "timed_out": False,
            "reaped_descendants": 0,
            "residual_descendants": [],
            "error_type": error_type,
        }
    else:
        preimage = {
            "returncode": int(result.returncode),
            "stdout": str(result.stdout),
            "stderr": str(result.stderr),
            "elapsed": float(result.elapsed),
            "timed_out": bool(result.timed_out),
            "reaped_descendants": int(result.reaped_descendants),
            "residual_descendants": list(result.residual_descendants),
            "error_type": error_type,
        }
    encoded = _canonical_bytes(preimage)
    stdout = str(preimage.pop("stdout"))
    stderr = str(preimage.pop("stderr"))
    residual_descendants = list(preimage.pop("residual_descendants"))
    return {
        **preimage,
        "bytes": len(encoded),
        "residual_descendant_count": len(residual_descendants),
        "residual_descendants_sha256": _sha256(_canonical_bytes(residual_descendants)),
        "sha256": _sha256(encoded),
        "stderr_bytes": len(stderr.encode("utf-8")),
        "stderr_sha256": _sha256(stderr.encode("utf-8")),
        "stdout_bytes": len(stdout.encode("utf-8")),
        "stdout_sha256": _sha256(stdout.encode("utf-8")),
    }


def _regular_file(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise SWEbenchVerificationError(f"{label} must be absolute")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise SWEbenchVerificationError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(mode):
        raise SWEbenchVerificationError(f"{label} must be a regular file")
    return resolved


def _executable(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise SWEbenchVerificationError(f"{label} must be absolute")
    candidate = Path(os.path.abspath(path))
    _regular_file(candidate, label=label)
    if not os.access(candidate, os.X_OK):
        raise SWEbenchVerificationError(f"{label} must be executable")
    return candidate


def _checkout(path: Path) -> Path:
    if not path.is_absolute():
        raise SWEbenchVerificationError("SWE-bench checkout must be absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SWEbenchVerificationError("SWE-bench checkout is unavailable") from exc
    if not resolved.is_dir():
        raise SWEbenchVerificationError("SWE-bench checkout must be a directory")
    return resolved


def _unix_docker_host(value: str) -> tuple[str, Path]:
    if not value.startswith("unix://") or any(character in value for character in "?#\0"):
        raise SWEbenchVerificationError("DOCKER_HOST must name a unix socket")
    raw_path = value.removeprefix("unix://")
    path = Path(raw_path)
    if not path.is_absolute():
        raise SWEbenchVerificationError("DOCKER_HOST socket path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
    except OSError as exc:
        raise SWEbenchVerificationError("DOCKER_HOST socket is unavailable") from exc
    if not stat.S_ISSOCK(mode):
        raise SWEbenchVerificationError("DOCKER_HOST must name a unix socket")
    return f"unix://{resolved}", resolved


def _private_write_json(path: Path, value: Mapping[str, Any]) -> None:
    data = _canonical_bytes(value)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def _safe_relative(path: Path, root: Path) -> str:
    try:
        resolved = path.resolve(strict=True)
        relative = resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SWEbenchVerificationError("evidence path escaped the private run") from exc
    if path.is_symlink():
        raise SWEbenchVerificationError("evidence artifacts must not be symlinks")
    return relative.as_posix()


def _artifact_evidence(path: Path, root: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    if not path.exists():
        return {"bytes": 0, "name": path.name, "present": False, "sha256": None}
    _safe_relative(path, root)
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode):
        raise SWEbenchVerificationError("evidence artifact must be a regular file")
    size = path.stat().st_size
    if size > MAX_EVIDENCE_BYTES:
        raise SWEbenchVerificationError("evidence artifact exceeds the retained-size limit")
    data = path.read_bytes()
    return {
        "bytes": len(data),
        "name": Path(relative).name,
        "present": True,
        "sha256": _sha256(data),
    }


def _json_file(path: Path, root: Path) -> dict[str, Any]:
    evidence = _artifact_evidence(path, root)
    if evidence["present"] is not True:
        raise SWEbenchVerificationError("required JSON evidence is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SWEbenchVerificationError("required JSON evidence is invalid") from exc
    if not isinstance(value, dict):
        raise SWEbenchVerificationError("required JSON evidence must be an object")
    return value


def _safe_production_python_path(value: str) -> bool:
    if (
        not value
        or not SAFE_PATH_PATTERN.fullmatch(value)
        or "\\" in value
        or value.startswith("/")
    ):
        return False
    pure = PurePosixPath(value)
    if str(pure) != value or ".." in pure.parts or pure.suffix != ".py":
        return False
    lowered = tuple(part.lower() for part in pure.parts)
    filename = lowered[-1]
    return not (
        any(part in TEST_PATH_COMPONENTS for part in lowered[:-1])
        or filename == "test.py"
        or filename.startswith("test_")
        or filename.endswith("_test.py")
    )


def select_mode_target(
    allowed_paths: Sequence[str],
    tracked_modes: Mapping[str, str],
) -> str:
    """Choose a deterministic tracked 100644 Python production path."""
    candidates = sorted(
        {
            path
            for path in allowed_paths
            if isinstance(path, str)
            and _safe_production_python_path(path)
            and tracked_modes.get(path) == "100644"
        }
    )
    if not candidates:
        raise SWEbenchVerificationError("red control has no safe mode-only target")
    return candidates[0]


def mode_only_patch(path: str) -> str:
    """Build the inert 100644-to-100755 Git mode patch used by red controls."""
    if not _safe_production_python_path(path):
        raise SWEbenchVerificationError("mode-only target is not a production Python path")
    return f"diff --git a/{path} b/{path}\nold mode 100644\nnew mode 100755\n"


def _string_list(value: Any, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SWEbenchVerificationError("test selector lists are invalid")
    items = list(value)
    if not allow_empty and not items:
        raise SWEbenchVerificationError("FAIL_TO_PASS must not be empty")
    if len(items) != len(set(items)):
        raise SWEbenchVerificationError("test selector lists contain duplicates")
    return items


def _report_group(entry: Mapping[str, Any], expected: Sequence[str]) -> dict[str, list[str]]:
    success = _string_list(entry.get("success"), allow_empty=True)
    failure = _string_list(entry.get("failure"), allow_empty=True)
    if set(success) & set(failure) or set(success) | set(failure) != set(expected):
        raise SWEbenchVerificationError("official report selector identity is inconsistent")
    return {"success": success, "failure": failure}


def _normalize_raw_statuses(
    statuses: Mapping[str, str],
    expected: set[str],
) -> tuple[dict[str, str], dict[str, int]]:
    observations: dict[str, list[str]] = {selector: [] for selector in expected}
    exact_selectors: set[str] = set()
    for raw_selector, raw_status in statuses.items():
        if not isinstance(raw_selector, str) or not isinstance(raw_status, str):
            raise SWEbenchVerificationError("raw test statuses are malformed")
        matches = [selector for selector in expected if raw_selector == selector]
        if matches:
            exact_selectors.add(matches[0])
        if not matches:
            matches = [
                selector
                for selector in expected
                if "/" in raw_selector
                and raw_selector.endswith(f":{selector}")
                and SAFE_PATH_PATTERN.fullmatch(raw_selector.replace(":", "/"))
            ]
        if len(matches) != 1:
            raise SWEbenchVerificationError("raw test selector identity is not exact")
        observations[matches[0]].append(raw_status)
    if exact_selectors != expected or any(
        not values or len(set(values)) != 1 for values in observations.values()
    ):
        raise SWEbenchVerificationError("raw test selector occurrences conflict or are missing")
    return (
        {selector: values[0] for selector, values in observations.items()},
        {selector: len(values) for selector, values in observations.items()},
    )


def validate_outcome(
    *,
    phase: str,
    fail_to_pass: Sequence[str],
    pass_to_pass: Sequence[str],
    statuses: Mapping[str, str],
    report_entry: Mapping[str, Any],
    official_resolution: str,
) -> dict[str, Any]:
    """Validate exact raw and grouped official SWE-bench outcomes."""
    if phase not in PHASES:
        raise SWEbenchVerificationError("unsupported SWE-bench verification phase")
    f2p = _string_list(list(fail_to_pass), allow_empty=False)
    p2p = _string_list(list(pass_to_pass), allow_empty=True)
    if set(f2p) & set(p2p):
        raise SWEbenchVerificationError("FAIL_TO_PASS and PASS_TO_PASS overlap")
    expected = set(f2p) | set(p2p)
    if not isinstance(statuses, Mapping):
        raise SWEbenchVerificationError("raw test selector identity is not exact")
    raw, occurrences = _normalize_raw_statuses(statuses, expected)
    if any(status not in KNOWN_STATUSES or status == "SKIPPED" for status in raw.values()):
        raise SWEbenchVerificationError("raw test status is missing, skipped, or unsupported")

    red = phase == "red"
    f2p_allowed = RED_FAILURE_STATUSES if red else PASSING_STATUSES
    if any(raw[selector] not in f2p_allowed for selector in f2p):
        raise SWEbenchVerificationError("FAIL_TO_PASS statuses violate the phase contract")
    if any(raw[selector] not in PASSING_STATUSES for selector in p2p):
        raise SWEbenchVerificationError("PASS_TO_PASS statuses violate the phase contract")

    for key, expected_value in (
        ("patch_is_None", False),
        ("patch_exists", True),
        ("patch_successfully_applied", True),
        ("resolved", not red),
    ):
        if report_entry.get(key) is not expected_value:
            raise SWEbenchVerificationError("official report patch or resolution state is invalid")
    expected_resolution = "RESOLVED_NO" if red else "RESOLVED_FULL"
    if official_resolution != expected_resolution:
        raise SWEbenchVerificationError("official resolution does not match the phase")

    tests_status = report_entry.get("tests_status")
    if not isinstance(tests_status, Mapping):
        raise SWEbenchVerificationError("official report omitted test status groups")
    f2p_report = tests_status.get("FAIL_TO_PASS")
    p2p_report = tests_status.get("PASS_TO_PASS")
    if not isinstance(f2p_report, Mapping) or not isinstance(p2p_report, Mapping):
        raise SWEbenchVerificationError("official report omitted required status groups")
    normalized_f2p = _report_group(f2p_report, f2p)
    normalized_p2p = _report_group(p2p_report, p2p)
    expected_f2p_success = [] if red else f2p
    expected_f2p_failure = f2p if red else []
    if (
        set(normalized_f2p["success"]) != set(expected_f2p_success)
        or set(normalized_f2p["failure"]) != set(expected_f2p_failure)
        or set(normalized_p2p["success"]) != set(p2p)
        or normalized_p2p["failure"]
    ):
        raise SWEbenchVerificationError("official grouped statuses violate the phase contract")
    return {
        "exact_selector_identity": True,
        "fail_to_pass_count": len(f2p),
        "phase": phase,
        "pass_to_pass_count": len(p2p),
        "exact_selector_keys_present": True,
        "parsed_status_key_count": sum(occurrences.values()),
        "resolution": official_resolution,
        "resolved": not red,
        "status_counts": {
            status: sum(1 for value in raw.values() if value == status)
            for status in sorted(set(raw.values()))
        },
    }


def _auth_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _freeze_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise SWEbenchVerificationError("authenticated input contains a symlink")
        if path.is_dir():
            os.chmod(path, 0o500)
        elif path.is_file():
            executable = bool(path.stat().st_mode & 0o111)
            os.chmod(path, 0o500 if executable else 0o400)
        else:
            raise SWEbenchVerificationError("authenticated input has an unsupported file type")
    os.chmod(root, 0o500)


def _snapshot_file(
    source: Path,
    *,
    inputs: Path,
    label: str,
    expected_sha256: str,
) -> Path:
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise SWEbenchVerificationError(f"{label} snapshot identity is invalid")
    data = _regular_file(source, label=label).read_bytes()
    if _sha256(data) != expected_sha256:
        raise SWEbenchVerificationError(f"{label} identity drifted")
    inputs.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = inputs / f"{label.lower().replace(' ', '-')}-{expected_sha256}{source.suffix}"
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    os.chmod(destination, 0o400)
    if _sha256(destination.read_bytes()) != expected_sha256:
        raise SWEbenchVerificationError(f"{label} snapshot drifted")
    return destination


def worker_environment(
    *,
    checkout: Path,
    private_cwd: Path,
    docker_host: str,
) -> dict[str, str]:
    """Return the complete, secret-free worker environment."""
    return {
        "DOCKER_HOST": docker_host,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "HOME": str(private_cwd / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "PATH": os.defpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(checkout),
        "PYTHONUTF8": "1",
        "TMPDIR": str(private_cwd / "tmp"),
        "TZ": "UTC",
    }


def _git_executable() -> Path:
    candidate = shutil.which("git", path=os.defpath)
    if not candidate:
        raise SWEbenchVerificationError("SWE-bench harness authentication failed")
    return _executable(Path(candidate), label="Git executable")


def _materialize_authenticated_harness(
    *,
    source_checkout: Path,
    destination: Path,
    git: Path,
    expected_revision: str,
    expected_run_evaluation_sha256: str,
    deadline: float,
) -> dict[str, Any]:
    if not REVISION_PATTERN.fullmatch(expected_revision) or not SHA256_PATTERN.fullmatch(
        expected_run_evaluation_sha256
    ):
        raise SWEbenchVerificationError("SWE-bench harness authentication failed")
    clone_command = [
        str(git),
        "-c",
        "core.hooksPath=/dev/null",
        "clone",
        "--quiet",
        "--no-checkout",
        "--no-hardlinks",
        "--",
        str(source_checkout),
        str(destination),
    ]
    clone = _run_process(
        clone_command,
        cwd=destination.parent,
        env=_auth_environment(),
        timeout=min(120.0, _remaining(deadline)),
        contain_descendants=True,
    )
    if clone.returncode or clone.timed_out or clone.residual_descendants:
        raise SWEbenchVerificationError("SWE-bench harness authentication failed")
    checkout_command = [
        str(git),
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(destination),
        "checkout",
        "--quiet",
        "--detach",
        expected_revision,
    ]
    checked_out = _run_process(
        checkout_command,
        cwd=destination,
        env=_auth_environment(),
        timeout=min(60.0, _remaining(deadline)),
        contain_descendants=True,
    )
    if checked_out.returncode or checked_out.timed_out or checked_out.residual_descendants:
        raise SWEbenchVerificationError("SWE-bench harness authentication failed")
    revision_command = [
        str(git),
        "-C",
        str(destination),
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
    ]
    revision = _run_process(
        revision_command,
        cwd=destination,
        env=_auth_environment(),
        timeout=min(15.0, _remaining(deadline)),
        contain_descendants=True,
    )
    if revision.returncode or revision.stdout.strip() != expected_revision:
        raise SWEbenchVerificationError("SWE-bench harness authentication failed")
    status_command = [
        str(git),
        "-C",
        str(destination),
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    ]
    status_result = _run_process(
        status_command,
        cwd=destination,
        env=_auth_environment(),
        timeout=min(15.0, _remaining(deadline)),
        contain_descendants=True,
    )
    run_evaluation = _regular_file(
        destination / "swebench/harness/run_evaluation.py",
        label="run_evaluation.py",
    )
    observed_sha256 = _sha256(run_evaluation.read_bytes())
    if (
        revision.returncode
        or revision.stdout.strip() != expected_revision
        or status_result.returncode
        or status_result.stdout.strip()
        or observed_sha256 != expected_run_evaluation_sha256
    ):
        raise SWEbenchVerificationError("SWE-bench harness authentication failed")
    source_manifest = _package_manifest(destination / "swebench")
    _freeze_tree(destination)
    return {
        "commands": [
            {
                "command": _command_evidence(clone_command),
                "process": _process_evidence(clone),
            },
            {
                "command": _command_evidence(checkout_command),
                "process": _process_evidence(checked_out),
            },
            {
                "command": _command_evidence(revision_command),
                "process": _process_evidence(revision),
            },
            {
                "command": _command_evidence(status_command),
                "process": _process_evidence(status_result),
            },
        ],
        "git_revision": expected_revision,
        "run_evaluation_sha256": observed_sha256,
        "source_file_count": source_manifest["file_count"],
        "source_sha256": source_manifest["sha256"],
    }


def _validate_scored_patch(
    *,
    patch: str,
    allowed_paths: Sequence[str],
    git: Path,
    cwd: Path,
    deadline: float,
) -> tuple[list[str], dict[str, Any]]:
    encoded = patch.encode("utf-8")
    if not patch.strip() or len(encoded) > 1_000_000:
        raise SWEbenchVerificationError("scored patch is empty or exceeds the size limit")
    command = [str(git), "apply", "--numstat", "-z", "--no-unsafe-paths", "-"]
    result = _run_process(
        command,
        cwd=cwd,
        env=_auth_environment(),
        input_text=patch,
        timeout=min(30.0, _remaining(deadline)),
        contain_descendants=True,
    )
    if result.returncode or result.timed_out or result.residual_descendants:
        raise SWEbenchVerificationError("scored patch metadata is invalid")
    summary_command = [str(git), "apply", "--summary", "--no-unsafe-paths", "-"]
    summary = _run_process(
        summary_command,
        cwd=cwd,
        env=_auth_environment(),
        input_text=patch,
        timeout=min(30.0, _remaining(deadline)),
        contain_descendants=True,
    )
    if (
        summary.returncode
        or summary.timed_out
        or summary.residual_descendants
        or any(
            line.lstrip().startswith(("rename ", "copy ")) for line in summary.stdout.splitlines()
        )
    ):
        raise SWEbenchVerificationError("scored patch renames or copies repository paths")
    paths: list[str] = []
    for row in result.stdout.split("\0"):
        if not row:
            continue
        fields = row.split("\t", 2)
        if len(fields) != 3 or not fields[2]:
            raise SWEbenchVerificationError("scored patch path metadata is malformed")
        paths.append(fields[2])
    allowed = set(allowed_paths)
    if not paths or any(path not in allowed for path in paths):
        raise SWEbenchVerificationError("scored patch changes a disallowed path")
    return sorted(set(paths)), {
        "bytes": len(encoded),
        "command": _command_evidence(command),
        "path_count": len(set(paths)),
        "paths_sha256": _sha256(_canonical_bytes(sorted(set(paths)))),
        "process": _process_evidence(result),
        "sha256": _sha256(encoded),
        "summary_command": _command_evidence(summary_command),
        "summary_process": _process_evidence(summary),
    }


def _python_environment_identity(
    *,
    python: Path,
    expected_sha256: str,
    cwd: Path,
    deadline: float,
) -> dict[str, Any]:
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise SWEbenchVerificationError("SWE-bench Python environment identity is invalid")
    command = [
        str(python),
        "-m",
        "pip",
        "--disable-pip-version-check",
        "freeze",
        "--all",
    ]
    environment = {
        **_auth_environment(),
        "HOME": str(cwd / "home"),
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONNOUSERSITE": "1",
    }
    result = _run_process(
        command,
        cwd=cwd,
        env=environment,
        timeout=min(60.0, _remaining(deadline)),
        contain_descendants=True,
    )
    lines = sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
    observed = _sha256(_canonical_bytes(lines))
    if (
        result.returncode
        or result.timed_out
        or result.residual_descendants
        or observed != expected_sha256
    ):
        raise SWEbenchVerificationError("SWE-bench Python environment identity drifted")
    return {
        "command": _command_evidence(command),
        "distribution_count": len(lines),
        "process": _process_evidence(result),
        "sha256": observed,
    }


def _package_manifest(root: Path) -> dict[str, Any]:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise SWEbenchVerificationError("Python package root is invalid")
    files: list[dict[str, Any]] = []
    for path in sorted(resolved.rglob("*")):
        if path.is_symlink():
            raise SWEbenchVerificationError("Python package contains a symlink")
        if not path.is_file() or path.suffix not in {".py", ".pyi"}:
            continue
        data = path.read_bytes()
        files.append(
            {
                "bytes": len(data),
                "path": path.relative_to(resolved).as_posix(),
                "sha256": _sha256(data),
            }
        )
    if not files:
        raise SWEbenchVerificationError("Python package contains no source files")
    return {
        "file_count": len(files),
        "sha256": _sha256(_canonical_bytes(files)),
    }


def _package_root_for_name(package: str) -> Path:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", package):
        raise SWEbenchVerificationError("Python package name is invalid")
    spec = importlib.util.find_spec(package)
    if spec is None or not isinstance(spec.origin, str):
        raise SWEbenchVerificationError("Python package is unavailable")
    root = Path(spec.origin).resolve(strict=True).parent
    try:
        root.relative_to(Path(sys.prefix).resolve(strict=True))
    except ValueError as exc:
        raise SWEbenchVerificationError("Python package escaped the pinned environment") from exc
    return root


def _package_manifest_for_name(package: str) -> dict[str, Any]:
    return _package_manifest(_package_root_for_name(package))


def _package_snapshot_for_name(package: str, destination: Path) -> dict[str, Any]:
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise SWEbenchVerificationError("Python package snapshot destination is invalid")
    source = _package_root_for_name(package)
    shutil.copytree(source, destination, symlinks=False)
    manifest = _package_manifest(destination)
    _freeze_tree(destination)
    return manifest


def _python_package_snapshot(
    *,
    python: Path,
    bridge: Path,
    package: str,
    expected_sha256: str,
    destination: Path,
    cwd: Path,
    deadline: float,
) -> tuple[Path, dict[str, Any]]:
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise SWEbenchVerificationError("Python package identity is invalid")
    command = [
        str(python),
        str(bridge),
        "package-snapshot",
        "--package",
        package,
        "--destination",
        str(destination),
    ]
    result = _run_process(
        command,
        cwd=cwd,
        env={
            **_auth_environment(),
            "HOME": str(cwd / "home"),
            "PYTHONNOUSERSITE": "1",
        },
        timeout=min(30.0, _remaining(deadline)),
        contain_descendants=True,
    )
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SWEbenchVerificationError("Python package identity probe failed") from exc
    if (
        result.returncode
        or result.timed_out
        or result.residual_descendants
        or not isinstance(manifest, dict)
        or manifest.get("sha256") != expected_sha256
        or not isinstance(manifest.get("file_count"), int)
        or manifest["file_count"] < 1
        or not destination.is_dir()
        or _package_manifest(destination) != manifest
    ):
        raise SWEbenchVerificationError("Python package identity drifted")
    return (
        destination,
        {
            "command": _command_evidence(command),
            "file_count": manifest["file_count"],
            "process": _process_evidence(result),
            "sha256": expected_sha256,
        },
    )


def _new_run_id(phase: str) -> str:
    return f"ctx-sb-{phase}-{secrets.token_hex(10)}"


def _model_name(phase: str) -> str:
    return f"ctx-swebench-{phase}"


def _prepare_private_cwd(path: Path) -> Path:
    if not path.is_absolute():
        raise SWEbenchVerificationError("private worker CWD must be absolute")
    resolved = path.resolve(strict=False)
    if resolved.exists():
        raise SWEbenchVerificationError("private worker CWD must not already exist")
    resolved.mkdir(mode=0o700, parents=True)
    os.chmod(resolved, 0o700)
    for name in ("home", "tmp"):
        child = resolved / name
        child.mkdir(mode=0o700)
        os.chmod(child, 0o700)
    return resolved


def _docker_ids(stdout: str) -> list[str]:
    ids = [line.strip() for line in stdout.splitlines() if line.strip()]
    if any(not CONTAINER_ID_PATTERN.fullmatch(item) for item in ids):
        raise SWEbenchVerificationError("Docker cleanup returned invalid container identifiers")
    return sorted(set(ids))


def _docker_query(
    *,
    docker_cli: Path,
    docker_host: str,
    arguments: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    deadline: float,
) -> tuple[list[str], dict[str, Any]]:
    command = [str(docker_cli), "--host", docker_host, *arguments]
    result = _run_process(
        command,
        cwd=cwd,
        env=dict(env),
        timeout=min(15.0, _remaining(deadline)),
        contain_descendants=True,
    )
    if result.returncode or result.timed_out or result.residual_descendants:
        raise SWEbenchVerificationError("Docker authentication or inventory failed")
    values = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    return values, {
        "command": _command_evidence(command),
        "count": len(values),
        "process": _process_evidence(result),
        "sha256": _sha256(_canonical_bytes(values)),
    }


def _docker_identity(
    *,
    docker_cli: Path,
    docker_host: str,
    expected_daemon_id: str,
    expected_server_version: str,
    cwd: Path,
    env: Mapping[str, str],
    deadline: float,
) -> dict[str, Any]:
    if (
        not expected_daemon_id
        or len(expected_daemon_id) > 200
        or not expected_server_version
        or len(expected_server_version) > 100
    ):
        raise SWEbenchVerificationError("Docker daemon identity is invalid")
    daemon_ids, daemon_evidence = _docker_query(
        docker_cli=docker_cli,
        docker_host=docker_host,
        arguments=("info", "--format", "{{.ID}}"),
        cwd=cwd,
        env=env,
        deadline=deadline,
    )
    versions, version_evidence = _docker_query(
        docker_cli=docker_cli,
        docker_host=docker_host,
        arguments=("version", "--format", "{{.Server.Version}}"),
        cwd=cwd,
        env=env,
        deadline=deadline,
    )
    if daemon_ids != [expected_daemon_id] or versions != [expected_server_version]:
        raise SWEbenchVerificationError("Docker daemon identity drifted")
    return {
        "daemon_id_sha256": _sha256(expected_daemon_id.encode()),
        "info": daemon_evidence,
        "server_version": expected_server_version,
        "version": version_evidence,
    }


def _docker_inventory(
    *,
    docker_cli: Path,
    docker_host: str,
    cwd: Path,
    env: Mapping[str, str],
    deadline: float,
) -> tuple[dict[str, frozenset[str]], dict[str, Any]]:
    commands = {
        "containers": ("ps", "--all", "--quiet", "--no-trunc"),
        "images": ("image", "ls", "--all", "--no-trunc", "--quiet"),
        "networks": ("network", "ls", "--quiet", "--no-trunc"),
        "volumes": ("volume", "ls", "--quiet"),
    }
    values: dict[str, frozenset[str]] = {}
    evidence: dict[str, Any] = {}
    for name, arguments in commands.items():
        observed, record = _docker_query(
            docker_cli=docker_cli,
            docker_host=docker_host,
            arguments=arguments,
            cwd=cwd,
            env=env,
            deadline=deadline,
        )
        values[name] = frozenset(observed)
        evidence[name] = record
    return values, evidence


def _cleanup_containers(
    *,
    docker_cli: Path,
    docker_host: str,
    run_id: str,
    cwd: Path,
    env: Mapping[str, str],
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    residual: list[str] = []
    deadline = time.monotonic() + 30

    def execute(argv: list[str]) -> Any | None:
        try:
            result = _run_process(
                argv,
                cwd=cwd,
                env=dict(env),
                timeout=min(10.0, _remaining(deadline)),
                contain_descendants=True,
            )
        except BaseException as exc:
            records.append(
                {
                    "command": _command_evidence(argv),
                    "process": _process_evidence(None, error_type=type(exc).__name__),
                }
            )
            errors.append("cleanup process failed")
            return None
        records.append(
            {
                "command": _command_evidence(argv),
                "process": _process_evidence(result),
            }
        )
        if result.returncode or result.timed_out or result.residual_descendants:
            errors.append("cleanup command failed")
        return result

    for _attempt in range(3):
        list_command = [
            str(docker_cli),
            "--host",
            docker_host,
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={RUN_LABEL}={run_id}",
        ]
        listed = execute(list_command)
        if listed is None or listed.returncode:
            break
        try:
            residual = _docker_ids(str(listed.stdout))
        except SWEbenchVerificationError:
            errors.append("cleanup list was invalid")
            break
        if not residual:
            break
        removed = execute(
            [
                str(docker_cli),
                "--host",
                docker_host,
                "rm",
                "--force",
                *residual,
            ]
        )
        if removed is None:
            break

    final_command = [
        str(docker_cli),
        "--host",
        docker_host,
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label={RUN_LABEL}={run_id}",
    ]
    final = execute(final_command)
    verification_complete = final is not None and final.returncode == 0
    if final is not None and final.returncode == 0:
        try:
            residual = _docker_ids(str(final.stdout))
        except SWEbenchVerificationError:
            residual = []
            errors.append("final cleanup list was invalid")
    else:
        errors.append("final cleanup verification failed")
    return {
        "commands": records,
        "errors": errors,
        "ok": not errors and not residual and verification_complete,
        "residual_container_ids": residual,
        "verification_complete": verification_complete,
    }


def _container_policy_attestation(
    container: Any,
    *,
    run_id: str,
    expected_image_id: str,
) -> dict[str, Any]:
    container.reload()
    attrs = container.attrs
    if not isinstance(attrs, Mapping):
        raise SWEbenchVerificationError("Docker container inspection is invalid")
    host = attrs.get("HostConfig")
    config = attrs.get("Config")
    mounts = attrs.get("Mounts")
    if (
        not isinstance(host, Mapping)
        or not isinstance(config, Mapping)
        or not isinstance(mounts, list)
    ):
        raise SWEbenchVerificationError("Docker container inspection is incomplete")
    labels = config.get("Labels")
    security_opt = host.get("SecurityOpt") or []
    cap_drop = host.get("CapDrop") or []
    if (
        not isinstance(labels, Mapping)
        or labels.get(RUN_LABEL) != run_id
        or attrs.get("Image") != expected_image_id
        or host.get("NetworkMode") != "none"
        or host.get("Memory") != CONTAINER_MEMORY_BYTES
        or host.get("NanoCpus") != CONTAINER_NANO_CPUS
        or host.get("PidsLimit") != CONTAINER_PIDS_LIMIT
        or host.get("Privileged") is not False
        or host.get("PidMode") not in (None, "")
        or host.get("IpcMode") not in (None, "", "private")
        or host.get("UTSMode") not in (None, "")
        or host.get("UsernsMode") not in (None, "")
        or host.get("CgroupnsMode") != "private"
        or host.get("Binds") not in (None, [])
        or mounts
        or cap_drop != ["ALL"]
        or CONTAINER_SECURITY_OPT not in security_opt
    ):
        raise SWEbenchVerificationError("Docker container policy was not enforced")
    return {
        "cap_drop": ["ALL"],
        "cgroupns_mode": "private",
        "image_id": expected_image_id,
        "memory_bytes": CONTAINER_MEMORY_BYTES,
        "mount_count": 0,
        "nano_cpus": CONTAINER_NANO_CPUS,
        "network_mode": "none",
        "pids_limit": CONTAINER_PIDS_LIMIT,
        "privileged": False,
        "security_opt": CONTAINER_SECURITY_OPT,
    }


class _HardenedContainerCollection:
    def __init__(
        self,
        collection: Any,
        *,
        run_id: str,
        expected_image_id: str,
        attestations: list[dict[str, Any]],
    ) -> None:
        self._collection = collection
        self._run_id = run_id
        self._expected_image_id = expected_image_id
        self._attestations = attestations

    def __getattr__(self, name: str) -> Any:
        return getattr(self._collection, name)

    def create(self, *args: Any, **kwargs: Any) -> Any:
        for field in ("binds", "devices", "device_requests", "mounts", "tmpfs", "volumes"):
            if kwargs.get(field):
                raise SWEbenchVerificationError("Docker host resources are forbidden")
        for field in ("cgroupns", "ipc_mode", "pid_mode", "uts_mode", "userns_mode"):
            if kwargs.get(field):
                raise SWEbenchVerificationError("Docker host namespaces are forbidden")
        if kwargs.get("privileged") is True or kwargs.get("cap_add"):
            raise SWEbenchVerificationError("Docker privilege escalation is forbidden")
        labels = kwargs.get("labels") or {}
        if not isinstance(labels, Mapping) or (
            RUN_LABEL in labels and labels.get(RUN_LABEL) != self._run_id
        ):
            raise SWEbenchVerificationError("Docker container labels are invalid")
        kwargs.update(
            {
                "cap_drop": ["ALL"],
                "cgroupns": "private",
                "init": True,
                "labels": {**dict(labels), RUN_LABEL: self._run_id},
                "mem_limit": CONTAINER_MEMORY_BYTES,
                "nano_cpus": CONTAINER_NANO_CPUS,
                "network_mode": "none",
                "pids_limit": CONTAINER_PIDS_LIMIT,
                "privileged": False,
                "security_opt": [CONTAINER_SECURITY_OPT],
            }
        )
        if args:
            args = (self._expected_image_id, *args[1:])
            kwargs.pop("image", None)
        else:
            kwargs["image"] = self._expected_image_id
        container = self._collection.create(*args, **kwargs)
        self._attestations.append(
            _container_policy_attestation(
                container,
                run_id=self._run_id,
                expected_image_id=self._expected_image_id,
            )
        )
        return container


class _HardenedDockerClient:
    def __init__(
        self,
        client: Any,
        *,
        run_id: str,
        expected_image_id: str,
        attestations: list[dict[str, Any]],
    ) -> None:
        self._client = client
        self.containers = _HardenedContainerCollection(
            client.containers,
            run_id=run_id,
            expected_image_id=expected_image_id,
            attestations=attestations,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _raw_document(
    value: Mapping[str, Any], *, phase: str, instance_id: str
) -> tuple[
    list[str],
    list[str],
    dict[str, str],
    str,
]:
    if (
        value.get("schema_version") != 1
        or value.get("phase") != phase
        or value.get("instance_id") != instance_id
    ):
        raise SWEbenchVerificationError("raw status evidence identity is invalid")
    expected = value.get("expected")
    statuses = value.get("statuses")
    resolution = value.get("official_resolution")
    if (
        not isinstance(expected, Mapping)
        or not isinstance(statuses, Mapping)
        or not isinstance(resolution, str)
    ):
        raise SWEbenchVerificationError("raw status evidence is incomplete")
    f2p = _string_list(expected.get("FAIL_TO_PASS"), allow_empty=False)
    p2p = _string_list(expected.get("PASS_TO_PASS"), allow_empty=True)
    normalized_statuses: dict[str, str] = {}
    for key, status_value in statuses.items():
        if not isinstance(key, str) or not isinstance(status_value, str):
            raise SWEbenchVerificationError("raw status evidence is malformed")
        normalized_statuses[key] = status_value
    return f2p, p2p, normalized_statuses, resolution


def _validate_parent_artifacts(
    *,
    phase: str,
    instance_id: str,
    run_id: str,
    model_name: str,
    allowed_paths: Sequence[str],
    expected_image_id: str | None,
    model_patch: str | None,
    work_dir: Path,
    log_dir: Path,
) -> dict[str, Any]:
    worker = _json_file(work_dir / "worker-result.json", work_dir)
    if (
        worker.get("ok") is not True
        or worker.get("phase") != phase
        or worker.get("run_id") != run_id
        or worker.get("model_name") != model_name
        or worker.get("log_dir") != log_dir.relative_to(work_dir).as_posix()
    ):
        raise SWEbenchVerificationError("worker result identity is invalid")
    image_id = worker.get("image_id")
    container_policy = worker.get("container_policy")
    if (
        not isinstance(image_id, str)
        or not IMAGE_ID_PATTERN.fullmatch(image_id)
        or (expected_image_id is not None and image_id != expected_image_id)
        or not isinstance(container_policy, list)
        or not container_policy
        or not all(
            item
            == {
                "cap_drop": ["ALL"],
                "cgroupns_mode": "private",
                "image_id": image_id,
                "memory_bytes": CONTAINER_MEMORY_BYTES,
                "mount_count": 0,
                "nano_cpus": CONTAINER_NANO_CPUS,
                "network_mode": "none",
                "pids_limit": CONTAINER_PIDS_LIMIT,
                "privileged": False,
                "security_opt": CONTAINER_SECURITY_OPT,
            }
            for item in container_policy
        )
    ):
        raise SWEbenchVerificationError("worker runtime attestation is invalid")
    report = _json_file(log_dir / "report.json", work_dir)
    raw = _json_file(log_dir / "raw-status.json", work_dir)
    if set(report) != {instance_id} or not isinstance(report.get(instance_id), Mapping):
        raise SWEbenchVerificationError("official report instance identity is invalid")
    f2p, p2p, statuses, resolution = _raw_document(
        raw,
        phase=phase,
        instance_id=instance_id,
    )
    outcome = validate_outcome(
        phase=phase,
        fail_to_pass=f2p,
        pass_to_pass=p2p,
        statuses=statuses,
        report_entry=report[instance_id],
        official_resolution=resolution,
    )
    patch = (log_dir / "patch.diff").read_text(encoding="utf-8")
    if phase == "red":
        matching_targets = [
            path
            for path in allowed_paths
            if _safe_production_python_path(path) and patch == mode_only_patch(path)
        ]
        if len(matching_targets) != 1:
            raise SWEbenchVerificationError("red mode-only patch evidence is invalid")
        outcome["mode_target_sha256"] = _sha256(matching_targets[0].encode())
    elif phase == "scored" and patch != model_patch:
        raise SWEbenchVerificationError("scored patch evidence changed across the worker boundary")
    outcome["container_policy_count"] = len(container_policy)
    outcome["image_id"] = image_id
    return outcome


def verify_swebench(
    *,
    phase: str,
    dataset_path: Path,
    instance_id: str,
    allowed_paths: Sequence[str],
    swebench_checkout: Path,
    swebench_python: Path,
    expected_revision: str,
    expected_run_evaluation_sha256: str,
    expected_bridge_sha256: str,
    expected_dataset_sha256: str,
    expected_python_sha256: str,
    expected_python_environment_sha256: str,
    expected_docker_package_sha256: str,
    docker_cli: Path,
    expected_docker_cli_sha256: str,
    docker_host: str,
    expected_docker_daemon_id: str,
    expected_docker_server_version: str,
    work_dir: Path,
    timeout: float = 900,
    model_patch: str | None = None,
    namespace: str | None = None,
    expected_image_id: str | None = None,
    allow_image_pull: bool = False,
) -> dict[str, Any]:
    """Verify one red, gold-green, or scored prediction with official SWE-bench."""
    if phase not in PHASES:
        raise SWEbenchVerificationError("unsupported SWE-bench verification phase")
    if not INSTANCE_PATTERN.fullmatch(instance_id):
        raise SWEbenchVerificationError("SWE-bench instance identifier is invalid")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise SWEbenchVerificationError("SWE-bench timeout must be positive and finite")
    if phase in {"red", "green"} and model_patch is not None:
        raise SWEbenchVerificationError("red and green phases select their own control patches")
    if phase == "scored" and (not isinstance(model_patch, str) or not model_patch.strip()):
        raise SWEbenchVerificationError("scored phase requires a non-empty prediction patch")
    if namespace != "swebench":
        raise SWEbenchVerificationError("official SWE-bench namespace is required")
    if not isinstance(allow_image_pull, bool):
        raise SWEbenchVerificationError("image acquisition policy is invalid")
    if allow_image_pull and phase != "red":
        raise SWEbenchVerificationError("only the acquisition red control may pull an image")
    if phase in {"green", "scored"} and (
        expected_image_id is None or not IMAGE_ID_PATTERN.fullmatch(expected_image_id)
    ):
        raise SWEbenchVerificationError("green and scored phases require a pinned image ID")
    if expected_image_id is not None and not IMAGE_ID_PATTERN.fullmatch(expected_image_id):
        raise SWEbenchVerificationError("expected image ID is invalid")
    normalized_allowed = list(allowed_paths)
    if (
        not normalized_allowed
        or len(normalized_allowed) != len(set(normalized_allowed))
        or not all(
            isinstance(path, str) and _safe_production_python_path(path)
            for path in normalized_allowed
        )
    ):
        raise SWEbenchVerificationError("allowed production paths are invalid")
    if phase == "red" and not any(
        _safe_production_python_path(path) for path in normalized_allowed
    ):
        raise SWEbenchVerificationError("red control has no safe mode-only target")

    deadline = time.monotonic() + timeout
    source_checkout = _checkout(swebench_checkout)
    python = _executable(swebench_python, label="SWE-bench Python")
    docker = _executable(docker_cli, label="Docker CLI")
    dataset = _regular_file(dataset_path, label="SWE-bench dataset")
    for expected, observed, label in (
        (expected_bridge_sha256, _sha256(SCRIPT_PATH.read_bytes()), "bridge"),
        (expected_dataset_sha256, _sha256(dataset.read_bytes()), "dataset"),
        (expected_python_sha256, _sha256(python.read_bytes()), "Python"),
        (expected_docker_cli_sha256, _sha256(docker.read_bytes()), "Docker CLI"),
    ):
        if not SHA256_PATTERN.fullmatch(expected) or observed != expected:
            raise SWEbenchVerificationError(f"SWE-bench {label} identity drifted")
    authenticated_host, _socket_path = _unix_docker_host(docker_host)
    private_cwd = _prepare_private_cwd(work_dir)
    inputs = private_cwd / "inputs"
    inputs.mkdir(mode=0o700)
    bridge_snapshot = _snapshot_file(
        SCRIPT_PATH,
        inputs=inputs,
        label="bridge",
        expected_sha256=expected_bridge_sha256,
    )
    dataset_snapshot = _snapshot_file(
        dataset,
        inputs=inputs,
        label="dataset",
        expected_sha256=expected_dataset_sha256,
    )
    python_environment = _python_environment_identity(
        python=python,
        expected_sha256=expected_python_environment_sha256,
        cwd=private_cwd,
        deadline=deadline,
    )
    docker_package_path, docker_package = _python_package_snapshot(
        python=python,
        bridge=bridge_snapshot,
        package="docker",
        expected_sha256=expected_docker_package_sha256,
        destination=(inputs / f"docker-package-{expected_docker_package_sha256}" / "docker"),
        cwd=private_cwd,
        deadline=deadline,
    )
    git = _git_executable()
    checkout = inputs / f"swebench-{expected_revision}"
    authentication = _materialize_authenticated_harness(
        source_checkout=source_checkout,
        destination=checkout,
        git=git,
        expected_revision=expected_revision,
        expected_run_evaluation_sha256=expected_run_evaluation_sha256,
        deadline=deadline,
    )
    os.chmod(inputs, 0o500)
    environment = worker_environment(
        checkout=checkout,
        private_cwd=private_cwd,
        docker_host=authenticated_host,
    )
    run_id = _new_run_id(phase)
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise SWEbenchVerificationError("generated SWE-bench run ID is invalid")
    model_name = _model_name(phase)
    docker_identity = _docker_identity(
        docker_cli=docker,
        docker_host=authenticated_host,
        expected_daemon_id=expected_docker_daemon_id,
        expected_server_version=expected_docker_server_version,
        cwd=private_cwd,
        env=environment,
        deadline=deadline,
    )
    baseline_inventory, baseline_inventory_evidence = _docker_inventory(
        docker_cli=docker,
        docker_host=authenticated_host,
        cwd=private_cwd,
        env=environment,
        deadline=deadline,
    )
    patch_paths: list[str] = []
    patch_evidence: dict[str, Any] | None = None
    if phase == "scored":
        assert model_patch is not None
        patch_paths, patch_evidence = _validate_scored_patch(
            patch=model_patch,
            allowed_paths=normalized_allowed,
            git=git,
            cwd=private_cwd,
            deadline=deadline,
        )
    request = {
        "allow_image_pull": allow_image_pull,
        "allowed_paths": normalized_allowed,
        "dataset_path": str(dataset_snapshot),
        "docker_package_path": str(docker_package_path),
        "expected_docker_daemon_id": expected_docker_daemon_id,
        "expected_docker_package_sha256": expected_docker_package_sha256,
        "expected_docker_server_version": expected_docker_server_version,
        "expected_harness_source_file_count": authentication["source_file_count"],
        "expected_harness_source_sha256": authentication["source_sha256"],
        "expected_image_id": expected_image_id,
        "expected_bridge_sha256": expected_bridge_sha256,
        "expected_dataset_sha256": expected_dataset_sha256,
        "expected_revision": expected_revision,
        "expected_run_evaluation_sha256": expected_run_evaluation_sha256,
        "git_cli": str(git),
        "harness_timeout_seconds": max(1, int(_remaining(deadline))),
        "instance_id": instance_id,
        "model_name": model_name,
        "model_patch": model_patch,
        "model_patch_sha256": _sha256(model_patch.encode()) if model_patch is not None else None,
        "namespace": namespace,
        "patch_paths": patch_paths,
        "phase": phase,
        "run_id": run_id,
        "schema_version": 1,
        "swebench_checkout": str(checkout),
    }
    request_path = private_cwd / "worker-request.json"
    result_path = private_cwd / "worker-result.json"
    _private_write_json(request_path, request)
    command = [
        str(python),
        str(bridge_snapshot),
        "worker",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
    ]

    process: Any | None = None
    process_error_type: str | None = None
    final_inventory: dict[str, frozenset[str]] = {}
    final_inventory_evidence: dict[str, Any] = {}
    inventory_error_type: str | None = None
    try:
        process = _run_process(
            command,
            cwd=private_cwd,
            env=environment,
            timeout=_remaining(deadline),
            contain_descendants=True,
        )
    except BaseException as exc:
        process_error_type = type(exc).__name__
    finally:
        cleanup = _cleanup_containers(
            docker_cli=docker,
            docker_host=authenticated_host,
            run_id=run_id,
            cwd=private_cwd,
            env=environment,
        )
        try:
            final_inventory, final_inventory_evidence = _docker_inventory(
                docker_cli=docker,
                docker_host=authenticated_host,
                cwd=private_cwd,
                env=environment,
                deadline=time.monotonic() + 60,
            )
        except BaseException as exc:
            inventory_error_type = type(exc).__name__

    process_preimage = {
        "command": command,
        "environment": environment,
        "error_type": process_error_type,
        "process": {
            "elapsed": float(process.elapsed) if process is not None else 0.0,
            "residual_descendants": (
                list(process.residual_descendants) if process is not None else []
            ),
            "returncode": int(process.returncode) if process is not None else None,
            "stderr": str(process.stderr) if process is not None else "",
            "stdout": str(process.stdout) if process is not None else "",
            "timed_out": bool(process.timed_out) if process is not None else False,
        },
    }
    parent_preimage_path = private_cwd / "parent-process.json"
    _private_write_json(parent_preimage_path, process_preimage)

    log_dir = private_cwd / "logs/run_evaluation" / run_id / model_name / instance_id
    artifact_paths = {name: log_dir / name for name in REQUIRED_ARTIFACTS}
    artifact_paths["worker-request.json"] = request_path
    artifact_paths["worker-result.json"] = result_path
    artifact_paths["parent-process.json"] = parent_preimage_path
    if phase == "red":
        artifact_paths["mode-probe.log"] = private_cwd / "mode-probe.log"
    artifacts: dict[str, Any] = {}
    artifact_error_type: str | None = None
    try:
        artifacts = {
            name: _artifact_evidence(path, private_cwd) for name, path in artifact_paths.items()
        }
    except SWEbenchVerificationError as exc:
        artifact_error_type = type(exc).__name__

    evidence: dict[str, Any] = {
        "artifacts": artifacts,
        "authentication": authentication,
        "cleanup": cleanup,
        "command": _command_evidence(command),
        "docker_identity": docker_identity,
        "docker_package": docker_package,
        "environment": {
            "keys": sorted(environment),
            "sha256": _sha256(_canonical_bytes(environment)),
        },
        "inventory": {
            "baseline": baseline_inventory_evidence,
            "final": final_inventory_evidence,
        },
        "input_snapshots": {
            "bridge_sha256": _sha256(bridge_snapshot.read_bytes()),
            "dataset_sha256": _sha256(dataset_snapshot.read_bytes()),
            "docker_package": _package_manifest(docker_package_path),
            "harness_source": _package_manifest(checkout / "swebench"),
        },
        "model_name": model_name,
        "patch": patch_evidence,
        "phase": phase,
        "process": _process_evidence(process, error_type=process_error_type),
        "python_environment": python_environment,
        "run_id": run_id,
        "schema_version": 1,
    }
    audit_path = private_cwd / "verification-evidence.json"
    audit = {
        "cleanup": {
            "command_count": len(cleanup["commands"]),
            "error_count": len(cleanup["errors"]),
            "ok": cleanup["ok"],
            "residual_container_count": len(cleanup["residual_container_ids"]),
            "verification_complete": cleanup["verification_complete"],
        },
        "inventory": {
            stage: {
                name: {
                    "count": record.get("count"),
                    "sha256": record.get("sha256"),
                }
                for name, record in records.items()
            }
            for stage, records in evidence["inventory"].items()
        },
        "phase": phase,
        "process": evidence["process"],
        "run_id": run_id,
        "schema_version": 1,
    }
    try:
        _private_write_json(audit_path, audit)
        artifacts[audit_path.name] = _artifact_evidence(audit_path, private_cwd)
    except (OSError, SWEbenchVerificationError) as exc:
        evidence["audit_error_type"] = type(exc).__name__
        raise SWEbenchVerificationError(evidence=evidence) from exc
    if artifact_error_type:
        evidence["artifact_error_type"] = artifact_error_type
        raise SWEbenchVerificationError(evidence=evidence)
    if not cleanup["ok"]:
        raise SWEbenchVerificationError(
            "SWE-bench Docker containment failed",
            evidence=evidence,
        )
    if evidence["input_snapshots"] != {
        "bridge_sha256": expected_bridge_sha256,
        "dataset_sha256": expected_dataset_sha256,
        "docker_package": {
            "file_count": docker_package["file_count"],
            "sha256": expected_docker_package_sha256,
        },
        "harness_source": {
            "file_count": authentication["source_file_count"],
            "sha256": authentication["source_sha256"],
        },
    }:
        raise SWEbenchVerificationError(
            "SWE-bench authenticated inputs drifted",
            evidence=evidence,
        )
    containment_drift = (
        inventory_error_type is not None
        or not final_inventory
        or baseline_inventory.get("containers") != final_inventory.get("containers")
        or baseline_inventory.get("networks") != final_inventory.get("networks")
        or baseline_inventory.get("volumes") != final_inventory.get("volumes")
        or (
            not allow_image_pull
            and baseline_inventory.get("images") != final_inventory.get("images")
        )
        or (
            allow_image_pull
            and not baseline_inventory.get("images", frozenset()).issubset(
                final_inventory.get("images", frozenset())
            )
        )
    )
    if containment_drift:
        evidence["inventory_error_type"] = inventory_error_type
        raise SWEbenchVerificationError(
            "SWE-bench Docker inventory drifted",
            evidence=evidence,
        )
    if (
        process is None
        or process.returncode
        or process.timed_out
        or process.residual_descendants
        or process_error_type
    ):
        raise SWEbenchVerificationError(evidence=evidence)
    if any(artifacts[name]["present"] is not True for name in REQUIRED_ARTIFACTS):
        raise SWEbenchVerificationError(evidence=evidence)
    try:
        evidence["validation"] = _validate_parent_artifacts(
            phase=phase,
            instance_id=instance_id,
            run_id=run_id,
            model_name=model_name,
            allowed_paths=normalized_allowed,
            expected_image_id=expected_image_id,
            model_patch=model_patch,
            work_dir=private_cwd,
            log_dir=log_dir,
        )
        if allow_image_pull:
            added_images = final_inventory["images"] - baseline_inventory["images"]
            if added_images not in (
                frozenset(),
                frozenset({str(evidence["validation"]["image_id"])}),
            ):
                raise SWEbenchVerificationError("official image pull changed extra images")
    except SWEbenchVerificationError as exc:
        raise SWEbenchVerificationError(evidence=evidence) from exc
    return evidence


def _worker_authenticate(request: Mapping[str, Any]) -> Path:
    checkout = _checkout(Path(str(request["swebench_checkout"])))
    expected_revision = str(request["expected_revision"])
    expected_sha256 = str(request["expected_run_evaluation_sha256"])
    expected_bridge_sha256 = str(request["expected_bridge_sha256"])
    expected_source_sha256 = str(request.get("expected_harness_source_sha256") or "")
    expected_source_file_count = request.get("expected_harness_source_file_count")
    git = _executable(Path(str(request["git_cli"])), label="Git executable")
    run_evaluation = _regular_file(
        checkout / "swebench/harness/run_evaluation.py",
        label="run_evaluation.py",
    )
    if (
        not REVISION_PATTERN.fullmatch(expected_revision)
        or not SHA256_PATTERN.fullmatch(expected_sha256)
        or not SHA256_PATTERN.fullmatch(expected_bridge_sha256)
        or _sha256(SCRIPT_PATH.read_bytes()) != expected_bridge_sha256
        or _sha256(run_evaluation.read_bytes()) != expected_sha256
        or not SHA256_PATTERN.fullmatch(expected_source_sha256)
        or not isinstance(expected_source_file_count, int)
        or isinstance(expected_source_file_count, bool)
        or expected_source_file_count < 1
    ):
        raise SWEbenchVerificationError("SWE-bench worker authentication failed")
    revision = subprocess.run(
        [str(git), "-C", str(checkout), "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=checkout,
        env=_auth_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    dirty = subprocess.run(
        [
            str(git),
            "-C",
            str(checkout),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ],
        cwd=checkout,
        env=_auth_environment(),
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if (
        revision.returncode
        or revision.stdout.strip() != expected_revision
        or dirty.returncode
        or dirty.stdout.strip()
    ):
        raise SWEbenchVerificationError("SWE-bench worker authentication failed")
    source_manifest = _package_manifest(checkout / "swebench")
    if source_manifest != {
        "file_count": expected_source_file_count,
        "sha256": expected_source_sha256,
    }:
        raise SWEbenchVerificationError("SWE-bench worker source snapshot drifted")
    return run_evaluation


def _require_authenticated_module(module: Any, checkout: Path) -> None:
    raw_path = getattr(module, "__file__", None)
    if not isinstance(raw_path, str):
        raise SWEbenchVerificationError("imported SWE-bench module has no source path")
    try:
        Path(raw_path).resolve(strict=True).relative_to(checkout.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SWEbenchVerificationError(
            "imported SWE-bench module escaped the authenticated checkout"
        ) from exc


def _tracked_mode_map(
    *,
    spec: Any,
    client: Any,
    allowed_paths: Sequence[str],
    run_id: str,
    docker_build: Any,
    docker_utils: Any,
) -> dict[str, str]:
    docker_logging = importlib.import_module("swebench.harness.docker_build")
    logger = docker_logging.setup_logger(f"{run_id}-mode-probe", Path("mode-probe.log"))
    container = None
    tracked: dict[str, str] = {}
    try:
        container = docker_build.build_container(
            spec,
            client,
            f"{run_id}-mode-probe",
            logger,
            False,
            False,
        )
        container.start()
        for candidate in sorted(set(allowed_paths)):
            if not _safe_production_python_path(candidate):
                continue
            result = container.exec_run(
                ["git", "ls-files", "--stage", "--", candidate],
                workdir="/testbed",
                user="root",
            )
            if result.exit_code:
                continue
            output = result.output.decode("utf-8", errors="strict").strip()
            lines = output.splitlines()
            if len(lines) != 1 or "\t" not in lines[0]:
                continue
            metadata, observed_path = lines[0].split("\t", 1)
            fields = metadata.split()
            if (
                len(fields) == 3
                and fields[0] in {"100644", "100755"}
                and fields[2] == "0"
                and observed_path == candidate
            ):
                tracked[candidate] = fields[0]
    finally:
        docker_utils.cleanup_container(client, container, logger)
        docker_logging.close_logger(logger)
    return tracked


def _run_official_instance(run_evaluation: Any, *args: Any) -> Any:
    previous_umask = os.umask(0o022)
    try:
        return run_evaluation.run_instance(*args)
    finally:
        os.umask(previous_umask)


def _worker_run(request: Mapping[str, Any], result_path: Path) -> None:
    os.umask(0o077)
    run_evaluation_path = _worker_authenticate(request)
    checkout = Path(str(request["swebench_checkout"])).resolve(strict=True)
    expected_docker_package_sha256 = request.get("expected_docker_package_sha256")
    docker_package_path = Path(str(request.get("docker_package_path") or ""))
    if (
        not isinstance(expected_docker_package_sha256, str)
        or not SHA256_PATTERN.fullmatch(expected_docker_package_sha256)
        or not docker_package_path.is_absolute()
        or docker_package_path.is_symlink()
        or not docker_package_path.is_dir()
        or docker_package_path.name != "docker"
        or _package_manifest(docker_package_path)["sha256"] != expected_docker_package_sha256
    ):
        raise SWEbenchVerificationError("worker Docker package identity drifted")
    sys.path.insert(0, str(docker_package_path.parent))
    importlib.invalidate_caches()
    docker_module = importlib.import_module("docker")
    try:
        Path(str(docker_module.__file__)).resolve(strict=True).relative_to(
            docker_package_path.resolve(strict=True)
        )
    except ValueError as exc:
        raise SWEbenchVerificationError(
            "worker Docker package escaped the authenticated snapshot"
        ) from exc
    if _package_manifest(docker_package_path)["sha256"] != expected_docker_package_sha256:
        raise SWEbenchVerificationError("worker Docker package changed during import")
    run_evaluation = importlib.import_module("swebench.harness.run_evaluation")
    if Path(str(run_evaluation.__file__)).resolve() != run_evaluation_path:
        raise SWEbenchVerificationError("imported SWE-bench harness path is not authenticated")
    constants = importlib.import_module("swebench.harness.constants")
    docker_build = importlib.import_module("swebench.harness.docker_build")
    docker_utils = importlib.import_module("swebench.harness.docker_utils")
    grading = importlib.import_module("swebench.harness.grading")
    test_spec_module = importlib.import_module("swebench.harness.test_spec.test_spec")
    harness_utils = importlib.import_module("swebench.harness.utils")
    for module in (
        run_evaluation,
        constants,
        docker_build,
        docker_utils,
        grading,
        test_spec_module,
        harness_utils,
    ):
        _require_authenticated_module(module, checkout)

    phase = str(request["phase"])
    instance_id = str(request["instance_id"])
    run_id = str(request["run_id"])
    model_name = str(request["model_name"])
    if (
        request.get("schema_version") != 1
        or phase not in PHASES
        or not INSTANCE_PATTERN.fullmatch(instance_id)
        or not RUN_ID_PATTERN.fullmatch(run_id)
        or model_name != _model_name(phase)
    ):
        raise SWEbenchVerificationError("worker request identity is invalid")
    dataset_path = _regular_file(
        Path(str(request["dataset_path"])),
        label="SWE-bench dataset",
    )
    expected_dataset_sha256 = request.get("expected_dataset_sha256")
    if (
        not isinstance(expected_dataset_sha256, str)
        or not SHA256_PATTERN.fullmatch(expected_dataset_sha256)
        or _sha256(dataset_path.read_bytes()) != expected_dataset_sha256
    ):
        raise SWEbenchVerificationError("worker dataset identity drifted")
    rows = harness_utils.load_swebench_dataset(str(dataset_path), "test", [instance_id])
    if len(rows) != 1 or rows[0].get("instance_id") != instance_id:
        raise SWEbenchVerificationError("worker dataset selection is not exact")
    row = rows[0]
    namespace = request.get("namespace")
    if namespace != "swebench":
        raise SWEbenchVerificationError("worker official namespace is invalid")
    spec = test_spec_module.make_test_spec(
        row,
        namespace=namespace,
        base_image_tag="latest",
        env_image_tag="latest",
        instance_image_tag="latest",
        arch="x86_64",
    )
    client = docker_module.from_env()
    try:
        info = client.info()
        version = client.version()
        if (
            not isinstance(info, Mapping)
            or info.get("ID") != request.get("expected_docker_daemon_id")
            or not isinstance(version, Mapping)
            or version.get("Version") != request.get("expected_docker_server_version")
        ):
            raise SWEbenchVerificationError("worker Docker daemon identity drifted")
        allow_image_pull = request.get("allow_image_pull")
        expected_image_id = request.get("expected_image_id")
        if not isinstance(allow_image_pull, bool) or (
            expected_image_id is not None
            and (
                not isinstance(expected_image_id, str)
                or not IMAGE_ID_PATTERN.fullmatch(expected_image_id)
            )
        ):
            raise SWEbenchVerificationError("worker image policy is invalid")
        try:
            initial_image = client.images.get(spec.instance_image_key)
            initial_image_id: str | None = str(initial_image.id)
        except docker_module.errors.ImageNotFound:
            initial_image_id = None
        if initial_image_id is None and allow_image_pull:
            client.images.pull(spec.instance_image_key)
            initial_image = client.images.get(spec.instance_image_key)
            initial_image_id = str(initial_image.id)
        if not allow_image_pull and initial_image_id is None:
            raise SWEbenchVerificationError("pinned instance image is unavailable")
        if initial_image_id is None or not IMAGE_ID_PATTERN.fullmatch(initial_image_id):
            raise SWEbenchVerificationError("instance image identity is invalid")
        if expected_image_id is not None and initial_image_id != expected_image_id:
            raise SWEbenchVerificationError("pinned instance image drifted")
        pinned_image_id = initial_image_id
        container_policy: list[dict[str, Any]] = []
        hardened_client = _HardenedDockerClient(
            client,
            run_id=run_id,
            expected_image_id=pinned_image_id,
            attestations=container_policy,
        )
        allowed_paths_value = request.get("allowed_paths")
        if not isinstance(allowed_paths_value, list) or not all(
            isinstance(path, str) for path in allowed_paths_value
        ):
            raise SWEbenchVerificationError("worker allowed paths are invalid")
        allowed_paths = list(allowed_paths_value)
        mode_target: str | None = None
        patch: str
        if phase == "red":
            tracked = _tracked_mode_map(
                spec=spec,
                client=hardened_client,
                allowed_paths=allowed_paths,
                run_id=run_id,
                docker_build=docker_build,
                docker_utils=docker_utils,
            )
            mode_target = select_mode_target(allowed_paths, tracked)
            patch = mode_only_patch(mode_target)
        elif phase == "green":
            gold_patch = row.get("patch")
            if not isinstance(gold_patch, str) or not gold_patch.strip():
                raise SWEbenchVerificationError("gold patch is unavailable")
            patch = gold_patch
        else:
            prediction_patch = request.get("model_patch")
            if not isinstance(prediction_patch, str) or not prediction_patch.strip():
                raise SWEbenchVerificationError("scored prediction patch is unavailable")
            if _sha256(prediction_patch.encode()) != request.get("model_patch_sha256"):
                raise SWEbenchVerificationError("scored prediction patch identity drifted")
            patch_paths = request.get("patch_paths")
            if (
                not isinstance(patch_paths, list)
                or not patch_paths
                or not all(isinstance(path, str) and path in allowed_paths for path in patch_paths)
            ):
                raise SWEbenchVerificationError("scored prediction paths are invalid")
            patch = prediction_patch

        timeout_value = request.get("harness_timeout_seconds")
        if (
            isinstance(timeout_value, bool)
            or not isinstance(timeout_value, int)
            or timeout_value <= 0
        ):
            raise SWEbenchVerificationError("worker harness timeout is invalid")
        prediction = {
            constants.KEY_INSTANCE_ID: instance_id,
            constants.KEY_MODEL: model_name,
            constants.KEY_PREDICTION: patch,
        }
        # SWE-bench copies host-created patch/eval files into images that may use a
        # non-root user. The enclosing run directory remains private.
        result = _run_official_instance(
            run_evaluation,
            spec,
            prediction,
            False,
            False,
            # The proxy is mandatory: official build_container reads client.containers.
            # Passing the raw client would silently restore Docker's default network.
            hardened_client,
            run_id,
            timeout_value,
            False,
        )
        if result.get("completed") is not True:
            raise SWEbenchVerificationError("official SWE-bench instance did not complete")
        log_dir = constants.RUN_EVALUATION_LOG_DIR / run_id / model_name / instance_id
        report_path = log_dir / constants.LOG_REPORT
        test_output_path = log_dir / constants.LOG_TEST_OUTPUT
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if set(report) != {instance_id} or not isinstance(report.get(instance_id), Mapping):
            raise SWEbenchVerificationError("official report identity is invalid")
        statuses, found = grading.get_logs_eval(spec, str(test_output_path))
        if not found or not isinstance(statuses, dict):
            raise SWEbenchVerificationError("official raw test statuses are unavailable")
        tests_status = report[instance_id].get("tests_status")
        if not isinstance(tests_status, Mapping):
            raise SWEbenchVerificationError("official grouped test statuses are unavailable")
        resolution = grading.get_resolution_status(tests_status)
        raw_status = {
            "expected": {
                "FAIL_TO_PASS": list(spec.FAIL_TO_PASS),
                "PASS_TO_PASS": list(spec.PASS_TO_PASS),
            },
            "instance_id": instance_id,
            "official_resolution": resolution,
            "phase": phase,
            "schema_version": 1,
            "statuses": statuses,
        }
        _private_write_json(log_dir / "raw-status.json", raw_status)
        validate_outcome(
            phase=phase,
            fail_to_pass=spec.FAIL_TO_PASS,
            pass_to_pass=spec.PASS_TO_PASS,
            statuses=statuses,
            report_entry=report[instance_id],
            official_resolution=resolution,
        )
        _worker_authenticate(request)
        if (
            _sha256(dataset_path.read_bytes()) != expected_dataset_sha256
            or _package_manifest(docker_package_path)["sha256"] != expected_docker_package_sha256
            or _sha256(SCRIPT_PATH.read_bytes()) != request.get("expected_bridge_sha256")
        ):
            raise SWEbenchVerificationError("authenticated worker inputs changed during use")
        final_image = client.images.get(spec.instance_image_key)
        image_id = str(final_image.id)
        if image_id != pinned_image_id or (
            expected_image_id is not None and image_id != expected_image_id
        ):
            raise SWEbenchVerificationError("instance image identity drifted")
        if not container_policy:
            raise SWEbenchVerificationError("no evaluation container policy was attested")
        worker_result: dict[str, Any] = {
            "container_policy": container_policy,
            "image_id": image_id,
            "log_dir": log_dir.as_posix(),
            "model_name": model_name,
            "ok": True,
            "phase": phase,
            "run_id": run_id,
        }
        if mode_target is not None:
            worker_result["mode_target"] = mode_target
        _private_write_json(result_path, worker_result)
    finally:
        client.close()


def _worker_main(request_path: Path, result_path: Path) -> int:
    os.umask(0o077)
    try:
        request_value = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request_value, dict):
            raise SWEbenchVerificationError("worker request must be an object")
        _worker_run(request_value, result_path)
    except Exception as exc:
        try:
            _private_write_json(
                result_path,
                {
                    "error_type": type(exc).__name__,
                    "ok": False,
                    "schema_version": 1,
                },
            )
        except OSError:
            pass
        print("official SWE-bench worker failed", file=sys.stderr)
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("worker")
    worker.add_argument("--request", type=Path, required=True)
    worker.add_argument("--result", type=Path, required=True)
    package_manifest = subparsers.add_parser("package-manifest")
    package_manifest.add_argument("--package", required=True)
    package_snapshot = subparsers.add_parser("package-snapshot")
    package_snapshot.add_argument("--package", required=True)
    package_snapshot.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "worker":
        return _worker_main(args.request, args.result)
    if args.command == "package-manifest":
        print(json.dumps(_package_manifest_for_name(args.package), sort_keys=True))
        return 0
    if args.command == "package-snapshot":
        print(
            json.dumps(
                _package_snapshot_for_name(args.package, args.destination),
                sort_keys=True,
            )
        )
        return 0
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
