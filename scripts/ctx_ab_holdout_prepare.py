#!/usr/bin/env python3
"""Prepare authenticated private inputs for the V2 CTX A/B benchmark."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ctx_ab_benchmark as benchmark  # noqa: E402
from scripts import ctx_ab_failure_evidence as failure_evidence  # noqa: E402
from scripts import ctx_ab_holdout as holdout  # noqa: E402
from scripts import ctx_ab_holdout_freeze as freezer  # noqa: E402
from scripts import ctx_ab_swebench as swebench  # noqa: E402


V1_PROTOCOL_RELATIVE = Path("benchmarks/ctx_ab/holdout-protocol-v1.json")
PROTOCOL_ID = freezer.PROTOCOL_ID
PROVIDER = "openai"
SEED_PREFIX = freezer.SEED_PREFIX
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_COUNT = freezer.REPOSITORY_COUNT
PAIR_COUNT = 30
TRIALS_PER_SCENARIO = freezer.TRIALS_PER_SCENARIO
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700


class PrepareError(RuntimeError):
    """A benchmark preparation artifact could not be authenticated."""


@dataclass(frozen=True)
class RepositoryState:
    root: Path
    revision: str
    origin_url: str
    origin_main_revision: str


@dataclass(frozen=True)
class CodexIdentity:
    path: Path
    sha256: str
    version: str
    provider_config_sha256: str


@dataclass(frozen=True)
class PythonIdentity:
    path: Path
    sha256: str
    version: str
    dependencies_sha256: str


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return data + (b"\n" if newline else b"")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PrepareError("JSON input contains duplicate keys")
        value[key] = item
    return value


def _json_object(data: bytes, *, label: str) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise PrepareError(f"{label} contains a non-finite number")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrepareError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PrepareError(f"{label} must contain a JSON object")
    return value


def _command_bytes(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> bytes:
    try:
        result = swebench._run_process(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            timeout=timeout,
            contain_descendants=True,
        )
    except (OSError, subprocess.SubprocessError, swebench.SWEbenchVerificationError) as exc:
        raise PrepareError("authenticated preparation command failed") from exc
    if result.returncode or result.timed_out or result.residual_descendants:
        raise PrepareError("authenticated preparation command failed")
    return result.stdout.encode("utf-8")


def _single_line(data: bytes, *, label: str, maximum: int) -> str:
    try:
        lines = [line.strip() for line in data.decode("utf-8").splitlines() if line.strip()]
    except UnicodeDecodeError as exc:
        raise PrepareError(f"{label} is invalid") from exc
    if len(lines) != 1 or len(lines[0]) > maximum or any(ord(char) < 32 for char in lines[0]):
        raise PrepareError(f"{label} is invalid")
    return lines[0]


def _repository_state(root: Path) -> RepositoryState:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise PrepareError("repository root is unavailable") from exc
    if not resolved.is_dir():
        raise PrepareError("repository root is unavailable")
    revision = _single_line(
        _command_bytes(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=resolved,
            timeout=15,
        ),
        label="repository revision",
        maximum=40,
    )
    if REVISION.fullmatch(revision) is None:
        raise PrepareError("repository revision is invalid")
    status = _command_bytes(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=resolved,
        timeout=30,
    )
    if status:
        raise PrepareError("repository must be clean before benchmark preparation")
    environment = _sanitized_environment()
    origin_url = _single_line(
        _command_bytes(
            ["git", "remote", "get-url", "origin"],
            cwd=resolved,
            timeout=15,
            env=environment,
        ),
        label="repository origin URL",
        maximum=500,
    )
    push_url = _single_line(
        _command_bytes(
            ["git", "remote", "get-url", "--push", "origin"],
            cwd=resolved,
            timeout=15,
            env=environment,
        ),
        label="repository origin push URL",
        maximum=500,
    )
    if benchmark.GITHUB_REPO_URL.fullmatch(origin_url) is None or push_url != origin_url:
        raise PrepareError("repository origin must be one credential-free canonical GitHub URL")
    origin_main_revision = _single_line(
        _command_bytes(
            ["git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"],
            cwd=resolved,
            timeout=15,
            env=environment,
        ),
        label="repository origin/main revision",
        maximum=40,
    )
    remote_main_bytes = _command_bytes(
        ["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"],
        cwd=resolved,
        timeout=60,
        env=environment,
    )
    try:
        remote_lines = remote_main_bytes.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise PrepareError("remote main identity is invalid") from exc
    remote_parts = remote_lines[0].split("\t") if len(remote_lines) == 1 else []
    if (
        REVISION.fullmatch(origin_main_revision) is None
        or len(remote_parts) != 2
        or REVISION.fullmatch(remote_parts[0]) is None
        or remote_parts[1] != "refs/heads/main"
        or origin_main_revision != remote_parts[0]
        or revision != origin_main_revision
    ):
        raise PrepareError("repository HEAD must equal the exact current origin/main revision")
    return RepositoryState(
        root=resolved,
        revision=revision,
        origin_url=origin_url,
        origin_main_revision=origin_main_revision,
    )


def _assert_repository_unchanged(expected: RepositoryState) -> None:
    observed = _repository_state(expected.root)
    if observed != expected:
        raise PrepareError("repository identity changed during benchmark preparation")


def _resolved_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _read_regular_bytes(
    path: Path,
    *,
    label: str,
    private: bool = False,
    executable: bool = False,
    allow_symlink_to_file: bool = False,
) -> tuple[Path, bytes]:
    candidate = _resolved_path(path)
    if candidate.is_symlink() and not allow_symlink_to_file:
        raise PrepareError(f"{label} must be a regular file")
    try:
        resolved = candidate.resolve(strict=True)
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PrepareError(f"{label} is unavailable") from exc
    try:
        if not allow_symlink_to_file and resolved != candidate:
            raise PrepareError(f"{label} must not use symlinks")
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PrepareError(f"{label} must be a single-link regular file")
        if executable and not os.access(resolved, os.X_OK):
            raise PrepareError(f"{label} must be executable")
        if private and stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
            raise PrepareError(f"{label} must be owner-only")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return resolved, data


def _file_digest(path: Path, *, label: str) -> str:
    return _sha256(_read_regular_bytes(path, label=label)[1])


def _private_parent(path: Path) -> Path:
    candidate = _resolved_path(path)
    if candidate.exists() or candidate.is_symlink():
        raise PrepareError("preparation output already exists")
    try:
        candidate.parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise PrepareError("preparation output parent is unavailable") from exc
    if parent != candidate.parent or not parent.is_dir():
        raise PrepareError("preparation output parent must not use symlinks")
    metadata = parent.stat()
    if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE or metadata.st_uid != os.getuid():
        raise PrepareError("preparation output parent must be owner-only")
    return candidate


def _require_private_repository_location(path: Path, *, root: Path) -> None:
    candidate = _resolved_path(path)
    repository = root.resolve(strict=True)
    private_root = repository / ".gate" / "ctx-ab-private"
    if repository in candidate.parents and private_root not in candidate.parents:
        raise PrepareError("repository-local benchmark evidence must use .gate/ctx-ab-private")


def _atomic_private_write(path: Path, data: bytes) -> Path:
    destination = _private_parent(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    installed = False
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise PrepareError("preparation output already exists") from exc
        installed = True
        temporary.unlink()
        metadata = destination.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
        ):
            raise PrepareError("preparation output permissions are unsafe")
        return destination
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if installed:
            destination.unlink(missing_ok=True)
        raise


def _same_path(left: Path, right: Path) -> bool:
    if _resolved_path(left) == _resolved_path(right):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _reject_aliases(paths: Mapping[str, Path]) -> None:
    entries = list(paths.items())
    for index, (_left_label, left) in enumerate(entries):
        for _right_label, right in entries[index + 1 :]:
            if _same_path(left, right):
                raise PrepareError("preparation paths must be distinct")


def _reject_nested_paths(left: Path, right: Path) -> None:
    first = _resolved_path(left)
    second = _resolved_path(right)
    if first == second or first in second.parents or second in first.parents:
        raise PrepareError("preparation output paths must not overlap")


def _stable_digest(path: Path, *, label: str) -> str:
    first = _file_digest(path, label=label)
    second = _file_digest(path, label=label)
    if first != second:
        raise PrepareError(f"{label} changed during authentication")
    return first


def _private_auth_identity() -> tuple[Path, str]:
    auth_path = Path(benchmark.ORIGINAL_CODEX_HOME) / "auth.json"
    resolved, data = _read_regular_bytes(
        auth_path,
        label="Codex authentication",
        private=True,
    )
    return resolved, _sha256(data)


def _provider_config_identity(provider: str) -> tuple[Path, str, str]:
    if provider != PROVIDER:
        raise PrepareError("official benchmark preparation requires the OpenAI provider")
    auth_path, auth_sha256 = _private_auth_identity()
    identity = benchmark.codex_provider_config_sha256(provider)
    if SHA256.fullmatch(identity) is None:
        raise PrepareError("provider configuration identity is unavailable")
    return auth_path, auth_sha256, identity


def _probe_codex(path: Path, *, provider: str) -> CodexIdentity:
    resolved, before = _read_regular_bytes(
        path,
        label="Codex binary",
        executable=True,
        allow_symlink_to_file=True,
    )
    auth_path, auth_before, provider_before = _provider_config_identity(provider)
    version = _single_line(
        _command_bytes([str(resolved), "--version"], cwd=ROOT, timeout=30),
        label="Codex version",
        maximum=200,
    )
    resolved_after, after = _read_regular_bytes(
        path,
        label="Codex binary",
        executable=True,
        allow_symlink_to_file=True,
    )
    auth_path_after, auth_after, provider_after = _provider_config_identity(provider)
    if (
        resolved_after != resolved
        or before != after
        or auth_path_after != auth_path
        or auth_before != auth_after
        or provider_before != provider_after
    ):
        raise PrepareError("Codex runtime identity changed during authentication")
    return CodexIdentity(
        path=resolved,
        sha256=_sha256(before),
        version=version,
        provider_config_sha256=provider_before,
    )


def _sanitized_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
        "PIP_CONFIG_FILE": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _checkout_snapshot(checkout: Path) -> tuple[str, str]:
    try:
        resolved = _resolved_path(checkout).resolve(strict=True)
    except OSError as exc:
        raise PrepareError("SWE-bench checkout is unavailable") from exc
    if not resolved.is_dir() or _resolved_path(checkout) != resolved:
        raise PrepareError("SWE-bench checkout must be a non-symlink directory")
    revision = _single_line(
        _command_bytes(
            ["git", "rev-parse", "--verify", "HEAD^{commit}"],
            cwd=resolved,
            timeout=15,
            env=_sanitized_environment(),
        ),
        label="SWE-bench revision",
        maximum=40,
    )
    if REVISION.fullmatch(revision) is None:
        raise PrepareError("SWE-bench revision is invalid")
    if _command_bytes(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=resolved,
        timeout=30,
        env=_sanitized_environment(),
    ):
        raise PrepareError("SWE-bench checkout must be clean")
    run_evaluation = resolved / "swebench" / "harness" / "run_evaluation.py"
    return revision, _stable_digest(run_evaluation, label="SWE-bench evaluator")


def _python_environment_sha256(python: Path) -> str:
    output = _command_bytes(
        [
            str(python),
            "-m",
            "pip",
            "--disable-pip-version-check",
            "freeze",
            "--all",
        ],
        cwd=ROOT,
        timeout=60,
        env=_sanitized_environment(),
    )
    try:
        lines = sorted(line.strip() for line in output.decode("utf-8").splitlines() if line.strip())
    except UnicodeDecodeError as exc:
        raise PrepareError("SWE-bench Python environment identity is invalid") from exc
    if not lines or len(lines) != len(set(lines)):
        raise PrepareError("SWE-bench Python environment identity is invalid")
    return _sha256(_canonical_bytes(lines))


def _docker_package_sha256(python: Path) -> str:
    output = _command_bytes(
        [
            str(python),
            str(Path(swebench.__file__).resolve()),
            "package-manifest",
            "--package",
            "docker",
        ],
        cwd=ROOT,
        timeout=60,
        env=_sanitized_environment(),
    )
    manifest = _json_object(output, label="Docker Python package identity")
    if (
        set(manifest) != {"file_count", "sha256"}
        or not isinstance(manifest.get("file_count"), int)
        or isinstance(manifest.get("file_count"), bool)
        or int(manifest["file_count"]) < 1
        or SHA256.fullmatch(str(manifest.get("sha256") or "")) is None
    ):
        raise PrepareError("Docker Python package identity is invalid")
    return str(manifest["sha256"])


def _docker_identity(docker_cli: Path, docker_host: str) -> tuple[str, str]:
    try:
        authenticated_host, _ = swebench._unix_docker_host(docker_host)
    except swebench.SWEbenchVerificationError as exc:
        raise PrepareError("Docker runtime identity is unavailable") from exc
    daemon_id = _single_line(
        _command_bytes(
            [
                str(docker_cli),
                "--host",
                authenticated_host,
                "info",
                "--format",
                "{{.ID}}",
            ],
            cwd=ROOT,
            timeout=30,
            env=_sanitized_environment(),
        ),
        label="Docker daemon identity",
        maximum=200,
    )
    server_version = _single_line(
        _command_bytes(
            [
                str(docker_cli),
                "--host",
                authenticated_host,
                "version",
                "--format",
                "{{.Server.Version}}",
            ],
            cwd=ROOT,
            timeout=30,
            env=_sanitized_environment(),
        ),
        label="Docker server version",
        maximum=100,
    )
    return daemon_id, server_version


def _verifier_snapshot(
    *,
    swebench_checkout: Path,
    swebench_python: Path,
    docker_cli: Path,
    docker_host: str,
) -> dict[str, Any]:
    revision, evaluator_sha256 = _checkout_snapshot(swebench_checkout)
    python_path, python_bytes = _read_regular_bytes(
        swebench_python,
        label="SWE-bench Python",
        executable=True,
    )
    docker_path, docker_bytes = _read_regular_bytes(
        docker_cli,
        label="Docker CLI",
        executable=True,
        allow_symlink_to_file=True,
    )
    bridge_sha256 = _stable_digest(Path(swebench.__file__), label="SWE-bench bridge")
    environment_sha256 = _python_environment_sha256(python_path)
    package_sha256 = _docker_package_sha256(python_path)
    daemon_id, server_version = _docker_identity(docker_path, docker_host)
    python_path_after, python_bytes_after = _read_regular_bytes(
        swebench_python,
        label="SWE-bench Python",
        executable=True,
    )
    if python_path_after != python_path or python_bytes_after != python_bytes:
        raise PrepareError("SWE-bench Python changed during authentication")
    return {
        "bridge_sha256": bridge_sha256,
        "docker_cli_sha256": _sha256(docker_bytes),
        "docker_daemon_id": daemon_id,
        "docker_package_sha256": package_sha256,
        "docker_server_version": server_version,
        "namespace": "swebench",
        "python_environment_sha256": environment_sha256,
        "python_sha256": _sha256(python_bytes),
        "revision": revision,
        "run_evaluation_sha256": evaluator_sha256,
        "schema_version": 1,
    }


def _probe_verifier(
    *,
    swebench_checkout: Path,
    swebench_python: Path,
    docker_cli: Path,
    docker_host: str,
) -> dict[str, Any]:
    first = _verifier_snapshot(
        swebench_checkout=swebench_checkout,
        swebench_python=swebench_python,
        docker_cli=docker_cli,
        docker_host=docker_host,
    )
    second = _verifier_snapshot(
        swebench_checkout=swebench_checkout,
        swebench_python=swebench_python,
        docker_cli=docker_cli,
        docker_host=docker_host,
    )
    if first != second:
        raise PrepareError("official verifier identity changed during authentication")
    return first


def _probe_execution_python(path: Path) -> PythonIdentity:
    resolved, before = _read_regular_bytes(
        path,
        label="execution Python",
        executable=True,
    )
    version = _single_line(
        _command_bytes(
            [
                str(resolved),
                "-c",
                "import platform; print(platform.python_version())",
            ],
            cwd=ROOT,
            timeout=30,
            env=_sanitized_environment(),
        ),
        label="execution Python version",
        maximum=100,
    )
    dependencies_sha256 = benchmark.python_dependencies_sha256(resolved)
    resolved_after, after = _read_regular_bytes(
        path,
        label="execution Python",
        executable=True,
    )
    if resolved_after != resolved or before != after:
        raise PrepareError("execution Python identity changed during authentication")
    return PythonIdentity(
        path=resolved,
        sha256=_sha256(before),
        version=version,
        dependencies_sha256=dependencies_sha256,
    )


def _normalized_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PrepareError("protocol timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise PrepareError("protocol timestamp must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _committed_v1_protocol(state: RepositoryState) -> dict[str, Any]:
    relative = V1_PROTOCOL_RELATIVE.as_posix()
    committed = _command_bytes(
        ["git", "show", f"{state.revision}:{relative}"],
        cwd=state.root,
        timeout=30,
    )
    working_path = state.root / V1_PROTOCOL_RELATIVE
    working = _read_regular_bytes(working_path, label="committed V1 protocol")[1]
    if committed != working:
        raise PrepareError("committed V1 protocol does not match the worktree")
    protocol = _json_object(committed, label="committed V1 protocol")
    if (
        protocol.get("schema_version") != 1
        or protocol.get("protocol_id") != "production-graph-holdout-v1"
        or not isinstance(protocol.get("universe"), dict)
        or not isinstance(protocol.get("static_candidate_rules"), dict)
        or not isinstance(protocol.get("ranking"), dict)
    ):
        raise PrepareError("committed V1 protocol is unsupported")
    return protocol


def _build_v2_protocol(
    *,
    v1: Mapping[str, Any],
    revision: str,
    frozen_at: str,
    product_inputs: Mapping[str, str],
    verifier_pins: Mapping[str, Any],
    exposure_ledger_sha256: str | None = None,
) -> dict[str, Any]:
    if product_inputs.get("revision") != revision:
        raise PrepareError("product revision does not match the committed repository")
    freezer_arguments: dict[str, Any] = {}
    if (
        exposure_ledger_sha256 is not None
        and "exposure_ledger_sha256"
        in inspect.signature(freezer.build_acquisition_protocol).parameters
    ):
        freezer_arguments["exposure_ledger_sha256"] = exposure_ledger_sha256
    try:
        protocol = freezer.build_acquisition_protocol(
            v1=v1,
            frozen_at=frozen_at,
            acquisition_frozen_at=frozen_at,
            product_inputs=product_inputs,
            verifier_pins=verifier_pins,
            **freezer_arguments,
        )
    except freezer.FreezeError as exc:
        raise PrepareError("committed V1 protocol is unsupported") from exc
    if exposure_ledger_sha256 is not None and "exposure_ledger_sha256" not in protocol:
        if SHA256.fullmatch(exposure_ledger_sha256) is None:
            raise PrepareError("exposure ledger SHA-256 is invalid")
        protocol["exposure_ledger_sha256"] = exposure_ledger_sha256
    return protocol


def _validated_exposure_ledger(path: Path) -> tuple[dict[str, Any], bytes]:
    ledger, data = _load_private_canonical_json(
        path,
        label="exposure ledger",
        newline=False,
    )
    hashes = ledger.get("instance_id_hmac_sha256")
    if (
        set(ledger) != {"instance_id_hmac_sha256", "salt", "schema_version"}
        or ledger.get("schema_version") != 1
        or isinstance(ledger.get("schema_version"), bool)
        or SHA256.fullmatch(str(ledger.get("salt") or "")) is None
        or not isinstance(hashes, list)
        or not hashes
        or not all(isinstance(value, str) and SHA256.fullmatch(value) for value in hashes)
        or hashes != sorted(hashes)
        or len(hashes) != len(set(hashes))
    ):
        raise PrepareError("exposure ledger has an unsupported shape")
    return ledger, data


def _validate_extended_acquisition_protocol(
    protocol: dict[str, Any],
    *,
    benchmark_script_path: Path | None = None,
    catalog_archive_path: Path | None = None,
    runtime_availability_path: Path | None = None,
) -> dict[str, Any]:
    exposure_sha256 = protocol.get("exposure_ledger_sha256")
    product_inputs = protocol.get("product_inputs")
    if (
        SHA256.fullmatch(str(exposure_sha256 or "")) is None
        or not isinstance(product_inputs, dict)
        or set(product_inputs)
        != set(freezer.PRODUCT_INPUT_KEYS) | {"origin_main_revision", "origin_url"}
        or benchmark.GITHUB_REPO_URL.fullmatch(str(product_inputs.get("origin_url") or "")) is None
        or REVISION.fullmatch(str(product_inputs.get("origin_main_revision") or "")) is None
        or product_inputs.get("origin_main_revision") != product_inputs.get("revision")
    ):
        raise PrepareError("acquisition protocol source-trust identity is invalid")
    base_protocol = json.loads(json.dumps(protocol))
    if (
        "exposure_ledger_sha256"
        not in inspect.signature(freezer.build_acquisition_protocol).parameters
    ):
        base_protocol.pop("exposure_ledger_sha256")
    base_product_inputs = base_protocol["product_inputs"]
    for field in ("origin_main_revision", "origin_url"):
        if field not in freezer.PRODUCT_INPUT_KEYS:
            base_product_inputs.pop(field)
    try:
        return freezer.validate_acquisition_protocol(
            base_protocol,
            benchmark_script_path=benchmark_script_path,
            catalog_archive_path=catalog_archive_path,
            runtime_availability_path=runtime_availability_path,
        )
    except freezer.FreezeError as exc:
        raise PrepareError("acquisition protocol is not supported") from exc


def create_protocol(
    *,
    output_path: Path,
    codex_path: Path,
    provider: str,
    swebench_checkout: Path,
    swebench_python: Path,
    docker_cli: Path,
    docker_host: str,
    exposure_ledger_path: Path,
    frozen_at: str,
    root: Path = ROOT,
) -> str:
    """Create an authenticated acquisition-frozen V2 protocol."""
    state = _repository_state(root)
    output = _resolved_path(output_path)
    exposure_file = _resolved_path(exposure_ledger_path)
    _require_private_repository_location(output, root=state.root)
    _require_private_repository_location(exposure_file, root=state.root)
    product_paths = {
        "benchmark": state.root / "scripts" / "ctx_ab_benchmark.py",
        "catalog": state.root / "graph" / "wiki-graph-runtime.tar.gz",
        "runtime": state.root / "src" / "ctx" / "assets" / "runtime-availability.json",
    }
    _reject_aliases(
        {
            "output": output,
            "exposure ledger": exposure_file,
            "V1 protocol": state.root / V1_PROTOCOL_RELATIVE,
            "benchmark": product_paths["benchmark"],
            "catalog": product_paths["catalog"],
            "runtime": product_paths["runtime"],
            "Codex": codex_path,
            "SWE-bench Python": swebench_python,
            "Docker CLI": docker_cli,
        }
    )
    _private_parent(output)
    v1 = _committed_v1_protocol(state)
    _exposure_ledger, exposure_bytes = _validated_exposure_ledger(exposure_file)
    exposure_sha256 = _sha256(exposure_bytes)
    product_before = {
        name: _stable_digest(path, label=f"product {name}") for name, path in product_paths.items()
    }
    codex = _probe_codex(codex_path, provider=provider)
    verifier = _probe_verifier(
        swebench_checkout=swebench_checkout,
        swebench_python=swebench_python,
        docker_cli=docker_cli,
        docker_host=docker_host,
    )
    product_inputs = {
        "benchmark_script_sha256": product_before["benchmark"],
        "catalog_archive_sha256": product_before["catalog"],
        "codex_binary_sha256": codex.sha256,
        "provider_config_sha256": codex.provider_config_sha256,
        "revision": state.revision,
        "runtime_availability_sha256": product_before["runtime"],
        "origin_main_revision": state.origin_main_revision,
        "origin_url": state.origin_url,
    }
    protocol = _build_v2_protocol(
        v1=v1,
        revision=state.revision,
        frozen_at=_normalized_timestamp(frozen_at),
        product_inputs=product_inputs,
        verifier_pins=verifier,
        exposure_ledger_sha256=exposure_sha256,
    )
    _validate_extended_acquisition_protocol(
        protocol,
        benchmark_script_path=product_paths["benchmark"],
        catalog_archive_path=product_paths["catalog"],
        runtime_availability_path=product_paths["runtime"],
    )
    if product_before != {
        name: _stable_digest(path, label=f"product {name}") for name, path in product_paths.items()
    }:
        raise PrepareError("product inputs changed during protocol preparation")
    if _probe_codex(codex_path, provider=provider) != codex:
        raise PrepareError("Codex runtime identity changed during protocol preparation")
    if (
        _probe_verifier(
            swebench_checkout=swebench_checkout,
            swebench_python=swebench_python,
            docker_cli=docker_cli,
            docker_host=docker_host,
        )
        != verifier
    ):
        raise PrepareError("official verifier identity changed during protocol preparation")
    if (
        _read_regular_bytes(exposure_file, label="exposure ledger", private=True)[1]
        != exposure_bytes
    ):
        raise PrepareError("exposure ledger changed during protocol preparation")
    _assert_repository_unchanged(state)
    data = _canonical_bytes(protocol, newline=True)
    _atomic_private_write(output, data)
    return _sha256(data)


def _load_private_canonical_json(
    path: Path,
    *,
    label: str,
    newline: bool,
) -> tuple[dict[str, Any], bytes]:
    _, data = _read_regular_bytes(path, label=label, private=True)
    value = _json_object(data, label=label)
    if data != _canonical_bytes(value, newline=newline):
        raise PrepareError(f"{label} is not canonical")
    return value, data


def _load_canonical_rows(
    path: Path,
    *,
    required_columns: Sequence[str],
) -> tuple[list[dict[str, str]], bytes]:
    _, data = _read_regular_bytes(path, label="canonical acquisition rows", private=True)
    try:
        lines = data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise PrepareError("canonical acquisition rows are invalid") from exc
    rows: list[dict[str, str]] = []
    canonical = bytearray()
    for line in lines:
        if not line.endswith("\n") or not line.strip():
            raise PrepareError("canonical acquisition rows are invalid")
        value = _json_object(line[:-1].encode("utf-8"), label="canonical acquisition row")
        if list(value) != list(required_columns) or not all(
            isinstance(value.get(column), str) for column in required_columns
        ):
            raise PrepareError("canonical acquisition rows are invalid")
        row = {column: str(value[column]) for column in required_columns}
        encoded = (
            json.dumps(
                row,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            + b"\n"
        )
        canonical.extend(encoded)
        rows.append(row)
    if not rows or bytes(canonical) != data:
        raise PrepareError("canonical acquisition rows are invalid")
    return rows, data


def _load_acquisition_protocol(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[dict[str, Any], bytes]:
    if SHA256.fullmatch(expected_sha256) is None:
        raise PrepareError("expected acquisition protocol SHA-256 is invalid")
    protocol, data = _load_private_canonical_json(
        path,
        label="acquisition protocol",
        newline=True,
    )
    if not secrets.compare_digest(_sha256(data), expected_sha256):
        raise PrepareError("acquisition protocol does not match the expected SHA-256")
    _validate_extended_acquisition_protocol(protocol)
    return protocol, data


def _ensure_private_directory_parent(path: Path) -> Path:
    candidate = _resolved_path(path)
    if candidate.exists() or candidate.is_symlink():
        raise PrepareError("source cache already exists")
    try:
        candidate.parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise PrepareError("source cache parent is unavailable") from exc
    if parent != candidate.parent:
        raise PrepareError("source cache parent must not use symlinks")
    metadata = parent.stat()
    if stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE or metadata.st_uid != os.getuid():
        raise PrepareError("source cache parent must be owner-only")
    return candidate


def _create_authenticated_bundle(
    *,
    url: str,
    commit: str,
    destination: Path,
) -> tuple[str, str]:
    if (
        benchmark.GITHUB_REPO_URL.fullmatch(url) is None
        or REVISION.fullmatch(commit) is None
        or destination.exists()
        or destination.is_symlink()
    ):
        raise PrepareError("source bundle identity is invalid")
    environment = _sanitized_environment()
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.stem}.", dir=destination.parent))
    repository = temporary / "source.git"
    validation = temporary / "validation.git"
    try:
        _command_bytes(
            ["git", "init", "--bare", "--quiet", str(repository)],
            cwd=destination.parent,
            timeout=30,
            env=environment,
        )
        _command_bytes(
            ["git", "-C", str(repository), "remote", "add", "origin", url],
            cwd=destination.parent,
            timeout=30,
            env=environment,
        )
        remote = _single_line(
            _command_bytes(
                ["git", "-C", str(repository), "remote", "get-url", "origin"],
                cwd=destination.parent,
                timeout=30,
                env=environment,
            ),
            label="source repository remote",
            maximum=500,
        )
        if remote != url:
            raise PrepareError("source repository remote authentication failed")
        _command_bytes(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                "fetch",
                "--quiet",
                "--force",
                "--no-tags",
                "origin",
                f"{commit}:refs/heads/base",
            ],
            cwd=destination.parent,
            timeout=1800,
            env=environment,
        )
        observed = _single_line(
            _command_bytes(
                [
                    "git",
                    "-C",
                    str(repository),
                    "rev-parse",
                    "--verify",
                    "refs/heads/base^{commit}",
                ],
                cwd=destination.parent,
                timeout=30,
                env=environment,
            ),
            label="source bundle commit",
            maximum=40,
        )
        refs = _single_line(
            _command_bytes(
                [
                    "git",
                    "-C",
                    str(repository),
                    "for-each-ref",
                    "--format=%(objectname) %(refname)",
                ],
                cwd=destination.parent,
                timeout=30,
                env=environment,
            ),
            label="source bundle refs",
            maximum=100,
        )
        if observed != commit or refs != f"{commit} refs/heads/base":
            raise PrepareError("source bundle commit authentication failed")
        tree_sha1 = _single_line(
            _command_bytes(
                [
                    "git",
                    "-C",
                    str(repository),
                    "rev-parse",
                    "--verify",
                    f"{commit}^{{tree}}",
                ],
                cwd=destination.parent,
                timeout=30,
                env=environment,
            ),
            label="source bundle tree",
            maximum=40,
        )
        if REVISION.fullmatch(tree_sha1) is None:
            raise PrepareError("source bundle tree authentication failed")
        _command_bytes(
            ["git", "-C", str(repository), "remote", "remove", "origin"],
            cwd=destination.parent,
            timeout=30,
            env=environment,
        )
        if _command_bytes(
            ["git", "-C", str(repository), "remote"],
            cwd=destination.parent,
            timeout=30,
            env=environment,
        ):
            raise PrepareError("source bundle staging repository retained a remote")
        _command_bytes(
            [
                "git",
                "-C",
                str(repository),
                "reflog",
                "expire",
                "--expire=now",
                "--all",
            ],
            cwd=destination.parent,
            timeout=30,
            env=environment,
        )
        _command_bytes(
            ["git", "-C", str(repository), "gc", "--prune=now", "--quiet"],
            cwd=destination.parent,
            timeout=1800,
            env=environment,
        )
        if _command_bytes(
            [
                "git",
                "-C",
                str(repository),
                "fsck",
                "--full",
                "--strict",
                "--unreachable",
                "--no-reflogs",
            ],
            cwd=destination.parent,
            timeout=1800,
            env=environment,
        ):
            raise PrepareError("source bundle staging repository contains unreachable objects")
        _command_bytes(
            [
                "git",
                "-C",
                str(repository),
                "bundle",
                "create",
                str(destination),
                "refs/heads/base",
            ],
            cwd=destination.parent,
            timeout=1800,
            env=environment,
        )
        bundle_head = _single_line(
            _command_bytes(
                ["git", "bundle", "list-heads", str(destination)],
                cwd=destination.parent,
                timeout=30,
                env=environment,
            ),
            label="source bundle head",
            maximum=100,
        )
        if bundle_head != f"{commit} refs/heads/base":
            raise PrepareError("source bundle exposes unsupported refs")
        _command_bytes(
            ["git", "init", "--bare", "--quiet", str(validation)],
            cwd=destination.parent,
            timeout=30,
            env=environment,
        )
        _command_bytes(
            [
                "git",
                "-C",
                str(validation),
                "-c",
                "protocol.file.allow=always",
                "fetch",
                "--quiet",
                "--no-tags",
                str(destination),
                "refs/heads/base:refs/heads/base",
            ],
            cwd=destination.parent,
            timeout=1800,
            env=environment,
        )
        if _command_bytes(
            ["git", "-C", str(validation), "remote"],
            cwd=destination.parent,
            timeout=30,
            env=environment,
        ):
            raise PrepareError("offline source materialization retained a remote")
        validated_commit = _single_line(
            _command_bytes(
                [
                    "git",
                    "-C",
                    str(validation),
                    "rev-parse",
                    "--verify",
                    "refs/heads/base^{commit}",
                ],
                cwd=destination.parent,
                timeout=30,
                env=environment,
            ),
            label="materialized source commit",
            maximum=40,
        )
        validated_tree = _single_line(
            _command_bytes(
                [
                    "git",
                    "-C",
                    str(validation),
                    "rev-parse",
                    "--verify",
                    "refs/heads/base^{tree}",
                ],
                cwd=destination.parent,
                timeout=30,
                env=environment,
            ),
            label="materialized source tree",
            maximum=40,
        )
        if validated_commit != commit or validated_tree != tree_sha1:
            raise PrepareError("offline source materialization changed identity")
        if _command_bytes(
            [
                "git",
                "-C",
                str(validation),
                "fsck",
                "--full",
                "--strict",
                "--unreachable",
                "--no-reflogs",
            ],
            cwd=destination.parent,
            timeout=1800,
            env=environment,
        ):
            raise PrepareError("offline source materialization contains unreachable objects")
        return tree_sha1, _stable_digest(destination, label="source bundle")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        _remove_private_tree(temporary)


def _harden_private_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise PrepareError("source bundle cache contains a symlink")
        if path.is_dir():
            path.chmod(PRIVATE_DIRECTORY_MODE)
        elif path.is_file():
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            path.chmod(0o700 if executable else PRIVATE_FILE_MODE)
        else:
            raise PrepareError("source bundle cache contains an unsupported file type")
    root.chmod(PRIVATE_DIRECTORY_MODE)


def _remove_private_tree(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        path.unlink(missing_ok=True)
        return
    for item in path.rglob("*"):
        try:
            if item.is_dir():
                item.chmod(PRIVATE_DIRECTORY_MODE)
            elif item.is_file():
                item.chmod(PRIVATE_FILE_MODE)
        except OSError:
            continue
    try:
        path.chmod(PRIVATE_DIRECTORY_MODE)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def prepare_sources(
    *,
    protocol_path: Path,
    expected_acquisition_protocol_sha256: str,
    exposure_ledger_path: Path,
    rows_path: Path,
    selection_path: Path,
    cache_root: Path,
    output_path: Path,
    workers: int = 4,
    root: Path = ROOT,
) -> str:
    """Create authenticated offline bundles for the ten frozen selected commits."""
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise PrepareError("source worker count must be between one and eight")
    state = _repository_state(root)
    protocol_file = _resolved_path(protocol_path)
    exposure_file = _resolved_path(exposure_ledger_path)
    rows_file = _resolved_path(rows_path)
    selection_file = _resolved_path(selection_path)
    _require_private_repository_location(cache_root, root=state.root)
    _require_private_repository_location(output_path, root=state.root)
    _require_private_repository_location(exposure_file, root=state.root)
    cache = _ensure_private_directory_parent(cache_root)
    output = _private_parent(output_path)
    _reject_aliases(
        {
            "protocol": protocol_file,
            "exposure ledger": exposure_file,
            "rows": rows_file,
            "selection": selection_file,
            "source cache": cache,
            "source map": output,
        }
    )
    _reject_nested_paths(cache, output)
    source_root = output.parent.resolve(strict=True)
    if source_root not in cache.parents:
        raise PrepareError("source bundle cache must be below the source-map parent")
    protocol, protocol_bytes = _load_acquisition_protocol(
        protocol_file,
        expected_sha256=expected_acquisition_protocol_sha256,
    )
    exposure_document, exposure_bytes = _validated_exposure_ledger(exposure_file)
    if not secrets.compare_digest(
        _sha256(exposure_bytes),
        str(protocol.get("exposure_ledger_sha256") or ""),
    ):
        raise PrepareError("exposure ledger does not match the acquisition protocol")
    if (
        protocol["product_inputs"]["revision"] != state.revision
        or protocol["product_inputs"]["origin_main_revision"] != state.origin_main_revision
        or protocol["product_inputs"]["origin_url"] != state.origin_url
    ):
        raise PrepareError("acquisition protocol does not match the committed product")
    universe = protocol.get("universe")
    if not isinstance(universe, dict) or not isinstance(universe.get("required_columns"), list):
        raise PrepareError("acquisition protocol universe is invalid")
    required_columns = list(universe["required_columns"])
    if not all(isinstance(column, str) and column for column in required_columns):
        raise PrepareError("acquisition protocol universe is invalid")
    rows, rows_bytes = _load_canonical_rows(rows_file, required_columns=required_columns)
    if _sha256(rows_bytes) != universe.get("selection_jsonl_sha256") or len(rows) != universe.get(
        "expected_rows"
    ):
        raise PrepareError("canonical acquisition rows do not match the protocol")
    selection, selection_bytes = _load_private_canonical_json(
        selection_file,
        label="canonical selection",
        newline=False,
    )
    try:
        evaluated_rows = [holdout.evaluate_row(row, protocol) for row in rows]
        filtered_rows = holdout.reject_historical_exposures(
            evaluated_rows,
            exposure_document,
        )
        expected_selection = holdout.select_rows(
            filtered_rows,
            protocol,
        )
        holdout.require_exposure_disjoint_selection(
            expected_selection,
            exposure_document,
        )
        selected_ids, repository_map = holdout._validated_selection(selection, protocol)
        holdout.require_exposure_disjoint_selection(
            selection,
            exposure_document,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PrepareError("canonical selection is invalid") from exc
    if selection != expected_selection:
        raise PrepareError("canonical selection does not match deterministic selection")
    if (
        len(selected_ids) != REPOSITORY_COUNT
        or len(repository_map) != REPOSITORY_COUNT
        or len(set(repository_map.values())) != REPOSITORY_COUNT
    ):
        raise PrepareError("canonical selection does not contain ten repositories")
    rows_by_id = {str(row.get("instance_id") or ""): row for row in rows}
    if len(rows_by_id) != len(rows) or any(item not in rows_by_id for item in selected_ids):
        raise PrepareError("canonical selection rows are unavailable")
    specs: list[tuple[str, str]] = []
    for item in selected_ids:
        row = rows_by_id[item]
        url = repository_map[item]
        commit = str(row.get("base_commit") or "")
        if (
            holdout.canonical_repo_url(str(row.get("repo") or "")) != url
            or REVISION.fullmatch(commit) is None
        ):
            raise PrepareError("selected source identity is invalid")
        specs.append((url, commit))
    if len({url for url, _ in specs}) != REPOSITORY_COUNT:
        raise PrepareError("selected source repositories are not distinct")

    cache.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    previous_umask = os.umask(0o077)
    try:
        destinations = {
            url: cache / f"{_sha256(url.encode('utf-8'))}.bundle" for url, _commit in specs
        }
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="ctx-source",
        ) as executor:
            futures = {
                url: executor.submit(
                    _create_authenticated_bundle,
                    url=url,
                    commit=commit,
                    destination=destinations[url],
                )
                for url, commit in specs
            }
            identities: dict[str, tuple[str, str]] = {}
            for url, _commit in specs:
                identities[url] = futures[url].result()
        _harden_private_tree(cache)
        repositories: dict[str, dict[str, str]] = {}
        for url, commit in specs:
            destination = destinations[url]
            try:
                relative = destination.relative_to(source_root).as_posix()
            except ValueError as exc:
                raise PrepareError("source bundle path escaped the source-map parent") from exc
            tree_sha1, expected_bundle_sha256 = identities[url]
            observed_bundle_sha256 = _stable_digest(
                destination,
                label="source bundle",
            )
            if observed_bundle_sha256 != expected_bundle_sha256:
                raise PrepareError("source bundle changed during preparation")
            repositories[url] = {
                "base_commit": commit,
                "bundle_path": relative,
                "bundle_sha256": observed_bundle_sha256,
                "tree_sha1": tree_sha1,
            }
        source_map = {
            "schema_version": 1,
            "repositories": repositories,
        }
        _assert_repository_unchanged(state)
        if (
            _read_regular_bytes(protocol_file, label="acquisition protocol", private=True)[1]
            != protocol_bytes
            or _read_regular_bytes(exposure_file, label="exposure ledger", private=True)[1]
            != exposure_bytes
            or _read_regular_bytes(rows_file, label="canonical acquisition rows", private=True)[1]
            != rows_bytes
            or _read_regular_bytes(selection_file, label="canonical selection", private=True)[1]
            != selection_bytes
        ):
            raise PrepareError("preparation inputs changed while sources were cloned")
        data = _canonical_bytes(source_map)
        _atomic_private_write(output, data)
        return _sha256(data)
    except BaseException:
        _remove_private_tree(cache)
        raise
    finally:
        os.umask(previous_umask)


def _runtime_snapshot(
    *,
    codex_path: Path,
    provider: str,
    swebench_checkout: Path,
    swebench_python: Path,
    docker_cli: Path,
    docker_host: str,
    execution_python: Path,
) -> tuple[CodexIdentity, dict[str, Any], PythonIdentity]:
    return (
        _probe_codex(codex_path, provider=provider),
        _probe_verifier(
            swebench_checkout=swebench_checkout,
            swebench_python=swebench_python,
            docker_cli=docker_cli,
            docker_host=docker_host,
        ),
        _probe_execution_python(execution_python),
    )


def write_environment(
    *,
    protocol_path: Path,
    expected_acquisition_protocol_sha256: str,
    output_path: Path,
    model: str,
    model_reasoning_effort: str,
    model_auto_compact_token_limit: int,
    provider: str,
    agent_timeout_seconds: float,
    codex_path: Path,
    execution_python: Path,
    swebench_checkout: Path,
    swebench_python: Path,
    docker_cli: Path,
    docker_host: str,
    root: Path = ROOT,
) -> str:
    """Write the authenticated execution-environment freeze input."""
    if (
        not isinstance(model, str)
        or not model.strip()
        or model != model.strip()
        or len(model) > 200
        or any(ord(character) < 32 for character in model)
        or isinstance(agent_timeout_seconds, bool)
        or not math.isfinite(agent_timeout_seconds)
        or not 0 < agent_timeout_seconds <= 3600
    ):
        raise PrepareError("execution environment arguments are invalid")
    try:
        codex_runtime_contract = benchmark.normalize_codex_runtime_contract(
            {
                "arms": list(benchmark.OFFICIAL_TREATMENT_ARMS),
                "model_auto_compact_token_limit": model_auto_compact_token_limit,
                "model_reasoning_effort": model_reasoning_effort,
            }
        )
    except ValueError as exc:
        raise PrepareError("Codex runtime contract arguments are invalid") from exc
    state = _repository_state(root)
    protocol_file = _resolved_path(protocol_path)
    _require_private_repository_location(output_path, root=state.root)
    output = _private_parent(output_path)
    _reject_aliases(
        {
            "protocol": protocol_file,
            "output": output,
            "Codex": codex_path,
            "execution Python": execution_python,
            "SWE-bench Python": swebench_python,
            "Docker CLI": docker_cli,
        }
    )
    protocol, protocol_bytes = _load_acquisition_protocol(
        protocol_file,
        expected_sha256=expected_acquisition_protocol_sha256,
    )
    if protocol["product_inputs"]["revision"] != state.revision:
        raise PrepareError("acquisition protocol does not match the committed product")
    before = _runtime_snapshot(
        codex_path=codex_path,
        provider=provider,
        swebench_checkout=swebench_checkout,
        swebench_python=swebench_python,
        docker_cli=docker_cli,
        docker_host=docker_host,
        execution_python=execution_python,
    )
    codex, verifier, python = before
    if (
        codex.sha256 != protocol["product_inputs"]["codex_binary_sha256"]
        or codex.provider_config_sha256 != protocol["product_inputs"]["provider_config_sha256"]
        or verifier != protocol["official_swebench_verifier"]
    ):
        raise PrepareError("runtime identities do not match the acquisition protocol")
    timeout: int | float = (
        int(agent_timeout_seconds)
        if float(agent_timeout_seconds).is_integer()
        else agent_timeout_seconds
    )
    environment = {
        "codex": {
            "runtime_contract": codex_runtime_contract,
            "version": codex.version,
        },
        "evaluator": {
            "backend": benchmark.OFFICIAL_HOLDOUT_BACKEND,
            "pins_sha256": _sha256(_canonical_bytes(verifier)),
        },
        "limits": {
            "agent_timeout_seconds": timeout,
            "arms": ["baseline", "ctx-light"],
            "catalog_cache_hit": False,
            "measured_concurrency": 1,
            "pair_count": PAIR_COUNT,
            "retries": 0,
            "sandbox_contract": benchmark.OFFICIAL_SANDBOX_CONTRACT,
            "task_count": REPOSITORY_COUNT,
            "trials_per_scenario": TRIALS_PER_SCENARIO,
        },
        "model": model,
        "product_revision": state.revision,
        "protocol_id": PROTOCOL_ID,
        "provider": provider,
        "python": {
            "dependencies_sha256": python.dependencies_sha256,
            "executable_sha256": python.sha256,
            "version": python.version,
        },
        "schema_version": 1,
    }
    try:
        freezer._validate_environment(environment, protocol=protocol, pins=verifier)
    except freezer.FreezeError as exc:
        raise PrepareError("execution environment does not satisfy the freezer contract") from exc
    after = _runtime_snapshot(
        codex_path=codex_path,
        provider=provider,
        swebench_checkout=swebench_checkout,
        swebench_python=swebench_python,
        docker_cli=docker_cli,
        docker_host=docker_host,
        execution_python=execution_python,
    )
    if after != before:
        raise PrepareError("runtime identities changed during environment preparation")
    _assert_repository_unchanged(state)
    if (
        _read_regular_bytes(protocol_file, label="acquisition protocol", private=True)[1]
        != protocol_bytes
    ):
        raise PrepareError("acquisition protocol changed during environment preparation")
    data = _canonical_bytes(environment)
    _atomic_private_write(output, data)
    return _sha256(data)


def _default_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _add_verifier_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--swebench-checkout", type=Path, required=True)
    parser.add_argument("--swebench-python", type=Path, required=True)
    parser.add_argument("--docker-cli", type=Path, required=True)
    parser.add_argument("--docker-host", required=True)


def _add_failure_evidence_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--failure-evidence-output", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    protocol_parser = subparsers.add_parser("protocol")
    protocol_parser.add_argument("--output", type=Path, required=True)
    protocol_parser.add_argument("--codex", type=Path, required=True)
    protocol_parser.add_argument("--provider", choices=[PROVIDER], default=PROVIDER)
    protocol_parser.add_argument("--exposure-ledger", type=Path, required=True)
    protocol_parser.add_argument("--frozen-at", default=_default_timestamp())
    _add_verifier_arguments(protocol_parser)
    _add_failure_evidence_argument(protocol_parser)

    sources_parser = subparsers.add_parser("sources")
    sources_parser.add_argument("--protocol", type=Path, required=True)
    sources_parser.add_argument("--expected-acquisition-protocol-sha256", required=True)
    sources_parser.add_argument("--exposure-ledger", type=Path, required=True)
    sources_parser.add_argument("--rows", type=Path, required=True)
    sources_parser.add_argument("--selection", type=Path, required=True)
    sources_parser.add_argument("--cache-root", type=Path, required=True)
    sources_parser.add_argument("--output", type=Path, required=True)
    sources_parser.add_argument("--workers", type=int, choices=range(1, 9), default=4)
    _add_failure_evidence_argument(sources_parser)

    environment_parser = subparsers.add_parser("environment")
    environment_parser.add_argument("--protocol", type=Path, required=True)
    environment_parser.add_argument("--expected-acquisition-protocol-sha256", required=True)
    environment_parser.add_argument("--output", type=Path, required=True)
    environment_parser.add_argument("--model", required=True)
    environment_parser.add_argument(
        "--model-reasoning-effort",
        choices=sorted(benchmark.CODEX_REASONING_EFFORTS),
        required=True,
    )
    environment_parser.add_argument(
        "--model-auto-compact-token-limit",
        type=int,
        required=True,
    )
    environment_parser.add_argument("--provider", choices=[PROVIDER], default=PROVIDER)
    environment_parser.add_argument("--agent-timeout-seconds", type=float, default=900)
    environment_parser.add_argument("--codex", type=Path, required=True)
    environment_parser.add_argument("--python", type=Path, default=Path(sys.executable))
    _add_verifier_arguments(environment_parser)
    _add_failure_evidence_argument(environment_parser)

    args = parser.parse_args(argv)
    try:
        failure_evidence.validate_destination(
            args.failure_evidence_output,
            repository_root=ROOT,
        )
    except failure_evidence.FailureEvidenceError:
        parser.exit(2, "benchmark preparation precondition failed; evidence=unavailable\n")
    try:
        if args.command == "protocol":
            digest = create_protocol(
                output_path=args.output,
                codex_path=args.codex,
                provider=args.provider,
                swebench_checkout=args.swebench_checkout,
                swebench_python=args.swebench_python,
                docker_cli=args.docker_cli,
                docker_host=args.docker_host,
                exposure_ledger_path=args.exposure_ledger,
                frozen_at=args.frozen_at,
            )
            print(f"prepared acquisition protocol sha256={digest}")
        elif args.command == "sources":
            digest = prepare_sources(
                protocol_path=args.protocol,
                expected_acquisition_protocol_sha256=(args.expected_acquisition_protocol_sha256),
                exposure_ledger_path=args.exposure_ledger,
                rows_path=args.rows,
                selection_path=args.selection,
                cache_root=args.cache_root,
                output_path=args.output,
                workers=args.workers,
            )
            print(
                f"prepared {REPOSITORY_COUNT} authenticated source bundles "
                f"source_map_sha256={digest}"
            )
        else:
            digest = write_environment(
                protocol_path=args.protocol,
                expected_acquisition_protocol_sha256=(args.expected_acquisition_protocol_sha256),
                output_path=args.output,
                model=args.model,
                model_reasoning_effort=args.model_reasoning_effort,
                model_auto_compact_token_limit=args.model_auto_compact_token_limit,
                provider=args.provider,
                agent_timeout_seconds=args.agent_timeout_seconds,
                codex_path=args.codex,
                execution_python=args.python,
                swebench_checkout=args.swebench_checkout,
                swebench_python=args.swebench_python,
                docker_cli=args.docker_cli,
                docker_host=args.docker_host,
            )
            print(f"prepared execution environment sha256={digest}")
    except BaseException as exc:
        try:
            failure_evidence.publish_failure(
                destination=args.failure_evidence_output,
                operation=f"holdout-prepare-{args.command}",
                exc=exc,
                repository_root=ROOT,
            )
            evidence_status = "preserved"
        except BaseException:
            evidence_status = "unavailable"
        parser.exit(
            2,
            f"benchmark preparation failed ({type(exc).__name__}); evidence={evidence_status}\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
