#!/usr/bin/env python3
"""Run reproducible feature-development trials with and without ctx context."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from math import comb
from pathlib import Path
from statistics import median
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIOS = ROOT / "benchmarks" / "ctx_ab" / "scenarios.yaml"
ORIGINAL_CODEX_HOME = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
GITHUB_REPO_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git")
INCIDENT_FIELDS = (
    "timestamp",
    "scenario",
    "arm",
    "attempt",
    "stage",
    "severity",
    "status",
    "message",
    "evidence",
)
PROCESS_MARKER = "CTX_BENCHMARK_PROCESS_TOKEN"
TREATMENT_ARMS = ("baseline", "ctx-light", "ctx-full")
PRODUCTION_CATALOG_ENGINE = "codex-production-catalog"
BENCHMARK_ENGINES = ("codex-controlled", "production-ctx-run", PRODUCTION_CATALOG_ENGINE)
PRODUCTION_CATALOG_ARCHIVE = ROOT / "graph" / "wiki-graph-runtime.tar.gz"
PRODUCTION_RUNTIME_AVAILABILITY = ROOT / "src" / "ctx" / "assets" / "runtime-availability.json"
PRODUCTION_PRIVATE_RUN_ROOT = ROOT / ".gate" / "ctx-ab-runs"
PRODUCTION_PRIVATE_SCENARIO_ROOT = ROOT / ".gate" / "ctx-ab-private"
PRODUCTION_CATALOG_CACHE_VERSION = 2
PRODUCTION_CATALOG_BODY_MAX_BYTES = 16 * 1024
PRODUCTION_TOOL_OUTPUT_LIMIT_BYTES = 32 * 1024
PRODUCTION_CATALOG_MCP_TOOLS = ("ctx__recommend_bundle", "ctx__wiki_get")
PRODUCTION_POLICY_ABSTENTION_LEVEL = "production_catalog_policy_abstention"
PRODUCT_CLAIM_MIN_SCENARIOS = 6
PRODUCT_CLAIM_MIN_REPOSITORIES = 5
PRODUCT_CLAIM_MIN_TRIALS = 6
PRODUCT_BENEFIT_RATIO_MAX = 0.85
PRODUCT_OTHER_RATIO_MAX = 1.10
PRODUCT_SUPPORT_ALPHA = 0.05
_LANGUAGE_TAG_ALIASES = {
    "c": frozenset({"c"}),
    "cpp": frozenset({"cpp", "c++", "cplusplus"}),
    "csharp": frozenset({"csharp", "c#", "dotnet"}),
    "go": frozenset({"go", "golang"}),
    "java": frozenset({"java"}),
    "javascript": frozenset({"javascript", "js", "node"}),
    "php": frozenset({"php"}),
    "python": frozenset({"python", "py"}),
    "rust": frozenset({"rust", "rs"}),
    "typescript": frozenset({"typescript", "ts"}),
}
_NON_INTENT_MATCH_TAGS = frozenset(
    {
        "ctx",
        "local",
        "no-api",
        "no-api-key",
        "no-api-keys",
        "offline",
    }
)
SUCCESSFUL_CTX_RUN_STOP_REASONS = frozenset({"completed"})
SUCCESSFUL_LIFECYCLE_STATUSES = frozenset({"completed", "successful"})
ENTITY_TRANSITION_ACTIONS = frozenset(
    {
        "load_requested",
        "load_applied",
        "used",
        "unload_requested",
        "unload_applied",
    }
)
EVIDENCE_TRUST_BOUNDARY = (
    "The ctx run payload and lifecycle ledger are same-process artifacts. "
    "Their SHA-256 digests identify the exact recorded bytes but do not provide "
    "cryptographically independent attestation."
)
PRODUCTION_CTX_TOOL_NAMES = (
    "ctx__recommend_bundle",
    "ctx__wiki_get",
    "ctx__load_entity",
    "ctx__mark_entity_used",
    "ctx__unload_entity",
)
_PRODUCTION_CTX_MCP_ANCHOR = "ctx-benchmark-control"
ARM_PERMUTATIONS = (
    ("baseline", "ctx-light", "ctx-full"),
    ("baseline", "ctx-full", "ctx-light"),
    ("ctx-light", "baseline", "ctx-full"),
    ("ctx-light", "ctx-full", "baseline"),
    ("ctx-full", "baseline", "ctx-light"),
    ("ctx-full", "ctx-light", "baseline"),
)


@dataclass(frozen=True)
class Scenario:
    id: str
    repo_url: str
    commit: str
    task: str
    query: str
    language: str
    benchmark_class: str
    test_path: str
    test_body: str
    verify: tuple[str, ...]
    expected_test_count: int
    regression_verify: tuple[tuple[str, ...], ...]
    red_failure_contains: str
    reference_patch: str
    allowed_changes: tuple[str, ...]
    context: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float
    timed_out: bool = False
    reaped_descendants: int = 0
    residual_descendants: tuple[int, ...] = ()


class _VerifiedProductionResult(dict[str, Any]):
    """In-process result whose final attested fields have not changed."""

    __slots__ = ("_sealed_sha256",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        super().__init__(values)
        self._sealed_sha256: str | None = None

    def seal(self) -> None:
        if (
            self.get("repository_state_matches_start_at_end") is not True
            or self.get("environment_manifest_matches_start_at_end") is not True
        ):
            raise RuntimeError("production result cannot be sealed before final attestation")
        self._sealed_sha256 = self._current_sha256()

    def is_sealed(self) -> bool:
        return bool(
            self._sealed_sha256
            and secrets.compare_digest(self._sealed_sha256, self._current_sha256())
        )

    def _current_sha256(self) -> str:
        payload = json.dumps(self, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CatalogSnapshot:
    wiki_dir: Path
    provenance: dict[str, Any]
    cache_hit: bool = False
    prepare_seconds: float = 0.0


def _safe_relative_path(value: object, *, field: str) -> str:
    raw = str(value or "").strip()
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts or raw != path.as_posix():
        raise ValueError(f"{field} must be a normalized relative POSIX path: {raw!r}")
    return raw


def _validated_command(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a non-empty string list")
    return tuple(value)


class IncidentLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=INCIDENT_FIELDS).writeheader()

    def add(
        self,
        *,
        scenario: str,
        arm: str,
        attempt: int,
        stage: str,
        message: str,
        evidence: str,
        severity: str = "error",
        status: str = "open",
    ) -> None:
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "scenario": scenario,
            "arm": arm,
            "attempt": attempt,
            "stage": stage,
            "severity": severity,
            "status": status,
            "message": message,
            "evidence": evidence,
        }
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=INCIDENT_FIELDS).writerow(row)

    def resolve_attempts(
        self,
        *,
        scenario: str,
        arm: str,
        attempts: set[int],
        resolved_by: int,
    ) -> int:
        with self.path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        resolved = 0
        for row in rows:
            try:
                attempt = int(row["attempt"])
            except (KeyError, ValueError):
                continue
            if (
                row.get("scenario") != scenario
                or row.get("arm") != arm
                or row.get("status") != "open"
                or attempt not in attempts
            ):
                continue
            row["status"] = "resolved"
            row["evidence"] = (
                f"{row.get('evidence', '').rstrip()}; recovered by attempt {resolved_by}"
            ).lstrip("; ")
            resolved += 1
        if resolved:
            with self.path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=INCIDENT_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
        return resolved

    def unresolved_count(self) -> int:
        with self.path.open(newline="", encoding="utf-8") as fh:
            return sum(row.get("status") == "open" for row in csv.DictReader(fh))


def load_scenarios(path: Path) -> list[Scenario]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = raw.get("scenarios") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("scenarios.yaml must contain a non-empty scenarios list")
    scenarios: list[Scenario] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each scenario must be an object")
        scenario_id = str(row.get("id") or "").strip()
        if not SAFE_NAME.fullmatch(scenario_id) or scenario_id in seen:
            raise ValueError(f"invalid or duplicate scenario id: {scenario_id!r}")
        seen.add(scenario_id)
        context = row.get("ctx_context")
        verify = row.get("verify")
        regression_verify = row.get("regression_verify")
        allowed_changes = row.get("allowed_changes")
        if (
            not isinstance(context, list)
            or not isinstance(regression_verify, list)
            or not regression_verify
        ):
            raise ValueError(
                f"{scenario_id}: ctx_context must be a list and regression_verify "
                "must be a non-empty list"
            )
        if not isinstance(allowed_changes, list) or not allowed_changes:
            raise ValueError(f"{scenario_id}: allowed_changes must be a non-empty list")
        expected_test_count = row.get("expected_test_count")
        if not isinstance(expected_test_count, int) or expected_test_count < 1:
            raise ValueError(f"{scenario_id}: expected_test_count must be a positive integer")
        commit = str(row["commit"])
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"{scenario_id}: commit must be a full lowercase SHA-1")
        repo_url = str(row["repo_url"])
        if GITHUB_REPO_URL.fullmatch(repo_url) is None:
            raise ValueError(f"{scenario_id}: repo_url must be an HTTPS GitHub .git URL")
        benchmark_class = str(row.get("benchmark_class") or "").strip()
        if benchmark_class not in {"trivial", "historical", "escalation"}:
            raise ValueError(
                f"{scenario_id}: benchmark_class must be 'trivial', 'historical', or 'escalation'"
            )
        validated_context: list[dict[str, Any]] = []
        for item in context:
            if not isinstance(item, dict) or item.get("type") not in {
                "skill",
                "agent",
                "mcp-server",
            }:
                raise ValueError(f"{scenario_id}: invalid ctx_context entry")
            slug = str(item.get("slug") or "")
            if not SAFE_NAME.fullmatch(slug):
                raise ValueError(f"{scenario_id}: invalid context slug: {slug!r}")
            validated_context.append(dict(item))
        reference_patch = str(row.get("reference_patch") or "")
        red_failure_contains = str(row.get("red_failure_contains") or "").strip()
        if not reference_patch.strip() or "../" in reference_patch:
            raise ValueError(f"{scenario_id}: reference_patch is missing or unsafe")
        if not red_failure_contains:
            raise ValueError(f"{scenario_id}: red_failure_contains must be non-empty")
        scenarios.append(
            Scenario(
                id=scenario_id,
                repo_url=repo_url,
                commit=commit,
                task=str(row["task"]).strip(),
                query=str(row["query"]).strip(),
                language=str(row.get("language") or "python"),
                benchmark_class=benchmark_class,
                test_path=_safe_relative_path(row["test_path"], field=f"{scenario_id}.test_path"),
                test_body=str(row["test_body"]),
                verify=_validated_command(verify, field=f"{scenario_id}.verify"),
                expected_test_count=expected_test_count,
                regression_verify=tuple(
                    _validated_command(command, field=f"{scenario_id}.regression_verify")
                    for command in regression_verify
                ),
                red_failure_contains=red_failure_contains,
                reference_patch=reference_patch,
                allowed_changes=tuple(
                    _safe_relative_path(path, field=f"{scenario_id}.allowed_changes")
                    for path in allowed_changes
                ),
                context=tuple(validated_context),
            )
        )
    return scenarios


def validate_runtime_pack_scenario_independence(
    scenarios: list[Scenario],
    *,
    availability_path: Path = PRODUCTION_RUNTIME_AVAILABILITY,
    scenarios_path: Path | None = None,
    archive_path: Path | None = None,
) -> dict[str, str]:
    """Reject distinctive frozen evidence in the mutable runtime context pack."""
    try:
        availability_bytes = availability_path.read_bytes()
        payload = json.loads(availability_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime availability pack is unreadable: {availability_path}") from exc

    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [text for item in value.values() for text in strings(item)]
        if isinstance(value, list):
            return [text for item in value for text in strings(item)]
        return []

    def normalize(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value)).casefold().replace("\\", "/")
        text = re.sub(r"\s*/\s*", "/", text)
        return " ".join(re.findall(r"[a-z0-9_./:+-]+", text)).replace(".git", "")

    def distinctive_fragments(value: object) -> set[str]:
        normalized = normalize(value)
        tokens = normalized.split()
        fragments = {
            token for token in tokens if len(token) >= 8 and "_" in token and len(set(token)) >= 6
        }
        for start in range(max(0, len(tokens) - 7)):
            window = tokens[start : start + 8]
            fragment = " ".join(window)
            if len(fragment) >= 48 and len(set(window)) >= 5:
                fragments.add(fragment)
        if len(normalized) >= 48 and len(set(tokens)) >= 5:
            fragments.add(normalized)
        return fragments

    catalog_text = "\n".join(normalize(value) for value in strings(payload))
    collisions: list[str] = []
    for scenario in scenarios:
        repository = "/".join(scenario.repo_url.removesuffix(".git").rsplit("/", 2)[-2:])
        exact_probes: list[tuple[str, str]] = [
            ("scenario_id", scenario.id),
            ("repository", repository),
            ("commit", scenario.commit),
            ("test_path", scenario.test_path),
        ]
        exact_probes.extend(
            ("allowed_change", path)
            for path in scenario.allowed_changes
            if len(normalize(path)) >= 16
        )
        fragment_sources: list[tuple[str, str]] = [
            ("query", scenario.query),
            ("task", scenario.task),
            ("test_body", scenario.test_body),
            ("reference_patch", scenario.reference_patch),
        ]
        fragment_sources.extend(
            ("context_body", str(item[key]))
            for item in scenario.context
            for key in ("body", "description", "instructions")
            if isinstance(item.get(key), str)
        )
        scenario_fingerprint = hashlib.sha256(
            f"{scenario.id}\0{scenario.commit}".encode()
        ).hexdigest()[:12]
        for field, value in exact_probes:
            normalized = normalize(value)
            if normalized and normalized in catalog_text:
                collisions.append(f"scenario={scenario_fingerprint}:{field}")
        for field, value in fragment_sources:
            if any(fragment in catalog_text for fragment in distinctive_fragments(value)):
                collisions.append(f"scenario={scenario_fingerprint}:{field}")

    if collisions:
        raise ValueError(
            "runtime availability pack contains frozen scenario evidence: "
            + ", ".join(sorted(set(collisions)))
        )
    attestation = {
        "guard": "runtime-pack-distinctive-evidence-v1",
        "runtime_availability_sha256": hashlib.sha256(availability_bytes).hexdigest(),
    }
    if scenarios_path is not None:
        attestation["scenarios_sha256"] = _sha256_file(scenarios_path)
    if archive_path is not None:
        attestation["catalog_archive_sha256"] = _sha256_file(archive_path)
    return attestation


def verify_scenario_independence_attestation(
    attestation: Mapping[str, str],
    *,
    scenarios_path: Path,
    snapshot: CatalogSnapshot,
) -> None:
    """Bind the independence check to the scenario and catalog bytes in use."""
    current = {
        "scenarios_sha256": _sha256_file(scenarios_path),
        "runtime_availability_sha256": str(snapshot.provenance["runtime_availability_sha256"]),
        "catalog_archive_sha256": str(snapshot.provenance["archive_sha256"]),
    }
    mismatches = sorted(
        field for field, value in current.items() if attestation.get(field) != value
    )
    if mismatches:
        raise ValueError(
            "benchmark integrity inputs changed after independence validation: "
            + ", ".join(mismatches)
        )


def _descendant_pids(root_pid: int) -> list[int]:
    if os.name == "nt":
        return []
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
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            process.kill()
        return
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
    if os.name == "nt":
        return set(), None
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


def _verification_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 600,
    input_text: str | None = None,
    resource_limits: bool = False,
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
        start_new_session=(os.name != "nt"),
        preexec_fn=_verification_limits if resource_limits and os.name != "nt" else None,
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
        stderr = f"{stderr}\nbenchmark process containment failed: {detail}".lstrip()
    return CommandResult(
        returncode,
        stdout,
        stderr,
        time.perf_counter() - started,
        timed_out=timed_out,
        reaped_descendants=reaped,
        residual_descendants=residual,
    )


def ensure_repo_cache(scenario: Scenario, cache_root: Path) -> Path:
    cache = cache_root / scenario.id
    cache_root.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        result = run_process(
            ["git", "clone", "--mirror", scenario.repo_url, str(cache)],
            cwd=cache_root,
            timeout=300,
        )
        if result.returncode:
            raise RuntimeError(f"clone failed: {result.stderr.strip()}")
    present = run_process(
        ["git", "cat-file", "-e", f"{scenario.commit}^{{commit}}"], cwd=cache, timeout=30
    )
    if present.returncode:
        fetched = run_process(["git", "fetch", "origin", scenario.commit], cwd=cache, timeout=300)
        if fetched.returncode:
            raise RuntimeError(f"commit fetch failed: {fetched.stderr.strip()}")
    return cache


def prepare_workspace(
    scenario: Scenario,
    cache: Path,
    destination: Path,
    *,
    include_evaluator_test: bool = True,
) -> str:
    cloned = run_process(["git", "clone", str(cache), str(destination)], cwd=destination.parent)
    if cloned.returncode:
        raise RuntimeError(f"local clone failed: {cloned.stderr.strip()}")
    checked_out = run_process(
        ["git", "checkout", "--detach", scenario.commit], cwd=destination, timeout=60
    )
    if checked_out.returncode:
        raise RuntimeError(f"checkout failed: {checked_out.stderr.strip()}")
    test_hash = hashlib.sha256(scenario.test_body.encode("utf-8")).hexdigest()
    if include_evaluator_test:
        materialize_evaluator_test(scenario, destination, expected_hash=test_hash)
    return test_hash


def materialize_evaluator_test(
    scenario: Scenario,
    workspace: Path,
    *,
    expected_hash: str,
) -> None:
    if hashlib.sha256(scenario.test_body.encode("utf-8")).hexdigest() != expected_hash:
        raise RuntimeError("benchmark-owned test definition changed during the trial")
    test_path = workspace / scenario.test_path
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(scenario.test_body, encoding="utf-8")
    if hashlib.sha256(test_path.read_bytes()).hexdigest() != expected_hash:
        raise RuntimeError("benchmark-owned test could not be materialized exactly")


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_catalog_archive(archive: Path) -> None:
    """Validate every member with the shipped graph installer's safety policy."""
    from ctx_init import _validate_graph_tar_member  # noqa: PLC0415

    if not archive.is_file() or archive.is_symlink():
        raise ValueError(f"catalog archive must be a regular file: {archive}")
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf:
            _validate_graph_tar_member(member)


def _install_shipped_catalog(claude_dir: Path, *, archive: Path) -> int:
    from ctx_init import build_graph  # noqa: PLC0415

    if archive.resolve() != PRODUCTION_CATALOG_ARCHIVE.resolve():
        raise ValueError("custom production catalog archives are not supported by ctx_init")
    return build_graph(claude=claude_dir, force=True, install_mode="runtime")


def _catalog_overlay_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"catalog overlay line {line_number} is not an object")
        records.append(
            {
                key: row.get(key)
                for key in ("overlay_id", "replace_scope", "source", "provenance")
                if row.get(key) is not None
            }
        )
    if not records:
        raise ValueError("installed catalog has no graph overlays")
    return records


def _validate_runtime_availability_files(
    wiki_dir: Path,
    *,
    availability_path: Path | None = None,
) -> list[dict[str, Any]]:
    availability_path = availability_path or PRODUCTION_RUNTIME_AVAILABILITY
    payload = json.loads(availability_path.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("runtime availability pack has no entries")
    declared: dict[str, bytes] = {}
    for entry_index, entry in enumerate(entries):
        files = entry.get("files") if isinstance(entry, dict) else None
        if not isinstance(files, list) or not files:
            raise ValueError(f"runtime availability entry {entry_index} has no files")
        for file_index, file_spec in enumerate(files):
            if not isinstance(file_spec, dict) or not isinstance(file_spec.get("content"), str):
                raise ValueError(
                    f"runtime availability entry {entry_index} file {file_index} is invalid"
                )
            relative = _safe_relative_path(
                file_spec.get("path"),
                field=f"runtime availability entry {entry_index} file {file_index}",
            )
            if relative in declared:
                raise ValueError(f"runtime availability file is declared twice: {relative}")
            declared[relative] = file_spec["content"].encode("utf-8")
    records: list[dict[str, Any]] = []
    for relative, expected in sorted(declared.items()):
        installed = wiki_dir / relative
        if installed.is_symlink() or not installed.is_file() or installed.read_bytes() != expected:
            raise ValueError(
                f"installed catalog runtime file does not match availability pack: {relative}"
            )
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(expected).hexdigest(),
                "size_bytes": len(expected),
            }
        )
    return records


def _catalog_provenance(
    archive: Path,
    wiki_dir: Path,
    *,
    runtime_availability_sha256: str,
) -> dict[str, Any]:
    graph_dir = wiki_dir / "graphify-out"
    manifest_path = graph_dir / "graph-export-manifest.json"
    overlay_path = graph_dir / "entity-overlays.jsonl"
    graph_store_path = graph_dir / "graph-store.sqlite3"
    runtime_skill_path = wiki_dir / "converted" / "ctx-python-testing" / "SKILL.md"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not str(manifest.get("export_id") or "").strip():
        raise ValueError("installed catalog graph export manifest is invalid")
    required = (overlay_path, graph_store_path, runtime_skill_path)
    if missing := [
        path.relative_to(wiki_dir).as_posix() for path in required if not path.is_file()
    ]:
        raise ValueError(f"installed catalog provenance files are missing: {missing}")
    try:
        availability_path = PRODUCTION_RUNTIME_AVAILABILITY.relative_to(ROOT).as_posix()
    except ValueError:
        availability_path = str(PRODUCTION_RUNTIME_AVAILABILITY.resolve())
    return {
        "cache_version": PRODUCTION_CATALOG_CACHE_VERSION,
        "installer": "ctx_init.build_graph",
        "install_mode": "runtime",
        "archive_path": str(archive),
        "archive_sha256": _sha256_file(archive),
        "archive_size_bytes": archive.stat().st_size,
        "runtime_availability_path": availability_path,
        "runtime_availability_sha256": runtime_availability_sha256,
        "runtime_availability_files": _validate_runtime_availability_files(wiki_dir),
        "graph_export_id": str(manifest["export_id"]),
        "graph_export_manifest_path": "graphify-out/graph-export-manifest.json",
        "graph_export_manifest_sha256": _sha256_file(manifest_path),
        "graph_store_path": "graphify-out/graph-store.sqlite3",
        "graph_store_sha256": _sha256_file(graph_store_path),
        "overlay_path": "graphify-out/entity-overlays.jsonl",
        "overlay_sha256": _sha256_file(overlay_path),
        "overlay_records": _catalog_overlay_records(overlay_path),
        "runtime_skill_path": "converted/ctx-python-testing/SKILL.md",
        "runtime_skill_sha256": _sha256_file(runtime_skill_path),
    }


def _freeze_catalog_tree(root: Path) -> None:
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"installed catalog contains a symlink: {path}")
        if path.is_dir():
            path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        elif path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP)
        else:
            raise ValueError(f"installed catalog contains an unsupported path: {path}")
    root.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


def _remove_catalog_staging(path: Path) -> None:
    if not path.exists():
        return
    for directory in [path, *(item for item in path.rglob("*") if item.is_dir())]:
        directory.chmod(stat.S_IRWXU)
    shutil.rmtree(path)


def _load_catalog_snapshot(
    snapshot_root: Path,
    *,
    archive_sha256: str,
    runtime_availability_sha256: str,
) -> CatalogSnapshot:
    provenance_path = snapshot_root / "catalog-provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if (
        not isinstance(provenance, dict)
        or provenance.get("cache_version") != PRODUCTION_CATALOG_CACHE_VERSION
        or provenance.get("archive_sha256") != archive_sha256
        or provenance.get("runtime_availability_sha256") != runtime_availability_sha256
    ):
        raise ValueError("cached production catalog provenance does not match shipped inputs")
    wiki_dir = snapshot_root / ".claude" / "skill-wiki"
    runtime_files = _validate_runtime_availability_files(wiki_dir)
    if provenance.get("runtime_availability_files") != runtime_files:
        raise ValueError("cached production catalog runtime files do not match provenance")
    critical = (
        wiki_dir,
        wiki_dir / "graphify-out" / "graph-export-manifest.json",
        wiki_dir / "graphify-out" / "entity-overlays.jsonl",
        wiki_dir / "converted" / "ctx-python-testing" / "SKILL.md",
    )
    if any(not path.exists() or path.is_symlink() for path in critical):
        raise ValueError("cached production catalog is incomplete or symlinked")
    if any(path.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) for path in critical):
        raise ValueError("cached production catalog is not read-only")
    digest_paths = {
        "graph_export_manifest_sha256": wiki_dir / str(provenance["graph_export_manifest_path"]),
        "graph_store_sha256": wiki_dir / str(provenance["graph_store_path"]),
        "overlay_sha256": wiki_dir / str(provenance["overlay_path"]),
        "runtime_skill_sha256": wiki_dir / str(provenance["runtime_skill_path"]),
    }
    if any(_sha256_file(path) != provenance.get(field) for field, path in digest_paths.items()):
        raise ValueError("cached production catalog content does not match its provenance")
    return CatalogSnapshot(wiki_dir=wiki_dir, provenance=dict(provenance))


def prepare_production_catalog(
    cache_root: Path,
    *,
    archive: Path = PRODUCTION_CATALOG_ARCHIVE,
) -> CatalogSnapshot:
    """Build and freeze one product-installed runtime catalog cache."""
    started = time.perf_counter()
    validate_catalog_archive(archive)
    archive_sha256 = _sha256_file(archive)
    runtime_availability_sha256 = _sha256_file(PRODUCTION_RUNTIME_AVAILABILITY)
    catalog_root = cache_root / "production-catalog"
    cache_key = (
        f"v{PRODUCTION_CATALOG_CACHE_VERSION}-{archive_sha256}-{runtime_availability_sha256}"
    )
    snapshot_root = catalog_root / cache_key
    if snapshot_root.is_dir():
        snapshot = _load_catalog_snapshot(
            snapshot_root,
            archive_sha256=archive_sha256,
            runtime_availability_sha256=runtime_availability_sha256,
        )
        return CatalogSnapshot(
            wiki_dir=snapshot.wiki_dir,
            provenance=snapshot.provenance,
            cache_hit=True,
            prepare_seconds=time.perf_counter() - started,
        )

    catalog_root.mkdir(parents=True, exist_ok=True)
    staging = catalog_root / f".{cache_key}.{os.getpid()}.{secrets.token_hex(4)}"
    staging.mkdir()
    try:
        claude_dir = staging / ".claude"
        if _install_shipped_catalog(claude_dir, archive=archive):
            raise RuntimeError("ctx_init.build_graph failed to install the shipped runtime catalog")
        wiki_dir = claude_dir / "skill-wiki"
        provenance = _catalog_provenance(
            archive,
            wiki_dir,
            runtime_availability_sha256=runtime_availability_sha256,
        )
        (staging / "catalog-provenance.json").write_text(
            json.dumps(provenance, indent=2) + "\n",
            encoding="utf-8",
        )
        _freeze_catalog_tree(wiki_dir)
        try:
            staging.rename(snapshot_root)
        except FileExistsError:
            _remove_catalog_staging(staging)
        snapshot = _load_catalog_snapshot(
            snapshot_root,
            archive_sha256=archive_sha256,
            runtime_availability_sha256=runtime_availability_sha256,
        )
        return CatalogSnapshot(
            wiki_dir=snapshot.wiki_dir,
            provenance=snapshot.provenance,
            cache_hit=False,
            prepare_seconds=time.perf_counter() - started,
        )
    except BaseException:
        if staging.exists():
            _remove_catalog_staging(staging)
        raise


def bind_catalog_snapshot(home: Path, snapshot: CatalogSnapshot) -> Path:
    """Point one isolated HOME at the immutable cache without symlinking data."""
    config = home / ".claude" / "skill-system-config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.wiki_dir.is_symlink() or snapshot.wiki_dir.stat().st_mode & stat.S_IWUSR:
        raise ValueError("production catalog snapshot must be a read-only regular directory")
    config.write_text(
        json.dumps({"paths": {"wiki_dir": str(snapshot.wiki_dir)}}, indent=2) + "\n",
        encoding="utf-8",
    )
    return config


def write_ctx_fixture(scenario: Scenario, home: Path) -> Path:
    wiki = home / ".claude" / "skill-wiki"
    nodes: list[dict[str, Any]] = []
    for item in scenario.context:
        entity_type = str(item["type"])
        slug = str(item["slug"])
        entity_id = f"{entity_type}:{slug}"
        tags = [str(tag) for tag in item.get("tags", [])]
        nodes.append({"id": entity_id, "label": slug, "type": entity_type, "tags": tags})
        plural = {"skill": "skills", "agent": "agents", "mcp-server": "mcp-servers"}[entity_type]
        page_dir = wiki / "entities" / plural
        if entity_type == "mcp-server":
            page_dir /= slug[0].lower() if slug and slug[0].isalpha() else "0-9"
        page = page_dir / f"{slug}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\n"
            f"name: {slug}\n"
            f"title: {item.get('title', slug)}\n"
            f"type: {entity_type}\n"
            f"tags: [{', '.join(tags)}]\n"
            "status: active\n"
            "---\n\n"
            f"# {item.get('title', slug)}\n\n{str(item['body']).strip()}\n",
            encoding="utf-8",
        )
        if entity_type == "skill":
            body_path = wiki / "converted" / slug / "SKILL.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(str(item["body"]).strip() + "\n", encoding="utf-8")
            installed_path = home / ".codex" / "skills" / slug / "SKILL.md"
            installed_path.parent.mkdir(parents=True, exist_ok=True)
            description = json.dumps(f"Use when {scenario.query.rstrip('.')}.")
            installed_path.write_text(
                "---\n"
                f"name: {slug}\n"
                f"description: {description}\n"
                "---\n\n"
                f"{str(item['body']).strip()}\n",
                encoding="utf-8",
            )
        elif entity_type == "agent":
            body_path = wiki / "converted-agents" / f"{slug}.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(str(item["body"]).strip() + "\n", encoding="utf-8")
    edges = [
        {"source": nodes[index]["id"], "target": nodes[index + 1]["id"], "weight": 0.8}
        for index in range(len(nodes) - 1)
    ]
    graph_path = wiki / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "graph": {},
                "nodes": nodes,
                "edges": edges,
            }
        ),
        encoding="utf-8",
    )
    return wiki


def _ctx_env(home: Path, lifecycle_root: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in (
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "PATH",
            "PATHEXT",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
            "SYSTEMROOT",
            "WINDIR",
        )
        if os.environ.get(key)
    }
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TEMP": str(tmp),
            "TMP": str(tmp),
            "TMPDIR": str(tmp),
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONNOUSERSITE": "1",
            "CTX_RUNTIME_LIFECYCLE_DIR": str(lifecycle_root),
            "CTX_TELEMETRY_ENABLED": "0",
            "CODEX_HOME": ORIGINAL_CODEX_HOME,
        }
    )
    return env


def recommend_context(
    scenario: Scenario, *, home: Path, lifecycle_root: Path
) -> list[dict[str, Any]]:
    base_command = [
        sys.executable,
        "-m",
        "ctx.cli.recommend",
        scenario.query,
        "--json",
        "--top-k",
        "5",
        "--local-code-task",
        "--no-api-keys",
        "--language",
        scenario.language,
        "--show-unavailable",
    ]

    def invoke(extra: list[str] | None = None) -> dict[str, Any]:
        result = run_process(
            [*base_command, *(extra or [])],
            cwd=ROOT,
            env=_ctx_env(home, lifecycle_root),
            timeout=90,
        )
        if result.returncode:
            raise RuntimeError(f"ctx recommendation failed: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("ctx recommendation returned a non-object payload")
        return payload

    payload = invoke()
    raw_rows = payload.get("results")
    if not isinstance(raw_rows, list):
        raise RuntimeError("ctx recommendation returned no results list")
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    expected = {f"{item['type']}:{item['slug']}" for item in scenario.context}
    found = {str(row.get("id")) for row in rows}
    missing = expected - found
    if missing and rows:
        seed = next(
            (str(row.get("id")) for row in rows if str(row.get("id", "")).startswith("skill:")),
            str(rows[0].get("id")),
        )
        related_payload = invoke(["--selected", seed, "--related-top-n", "5"])
        selection = related_payload.get("selection")
        related_rows = selection.get("related_results") if isinstance(selection, dict) else []
        second_rows = related_payload.get("results")
        combined = [
            *(second_rows if isinstance(second_rows, list) else []),
            *(related_rows if isinstance(related_rows, list) else []),
        ]
        for row in combined:
            if not isinstance(row, dict) or str(row.get("id")) in found:
                continue
            rows.append(dict(row))
            found.add(str(row.get("id")))
        missing = expected - found
    if missing:
        raise RuntimeError(f"ctx recommendation omitted controlled entities: {sorted(missing)}")
    wiki = home / ".claude" / "skill-wiki"
    controlled = [row for row in rows if str(row.get("id")) in expected]
    unavailable: list[str] = []
    for row in controlled:
        entity_id = str(row.get("id"))
        try:
            source_path = _safe_relative_path(
                row.get("source_path"), field=f"{entity_id}.source_path"
            )
        except ValueError:
            unavailable.append(entity_id)
            continue
        if row.get("installable") is not True or not (wiki / source_path).is_file():
            unavailable.append(entity_id)
    if unavailable:
        raise RuntimeError(
            f"ctx recommendation returned unavailable controlled entities: {sorted(unavailable)}"
        )
    return rows


def _catalog_mcp_client(
    *,
    home: Path,
    lifecycle_root: Path,
    session_id: str,
) -> Any:
    from ctx.adapters.generic.tools import McpClient, McpServerConfig  # noqa: PLC0415

    config = McpServerConfig(
        name="ctx-production-catalog",
        command=sys.executable,
        args=(
            "-m",
            "ctx.mcp_server.server",
            "--allow-tools",
            ",".join(PRODUCTION_CATALOG_MCP_TOOLS),
        ),
        env=_ctx_env(home, lifecycle_root),
        startup_timeout=30.0,
        request_timeout=90.0,
    )
    return McpClient(config, session_id=session_id)


def _policy_initial_load_ids(policy: dict[str, Any]) -> tuple[list[str], str]:
    field = "initial_load" if "initial_load" in policy else "load"
    raw = policy.get(field, [])
    if not isinstance(raw, list):
        raise RuntimeError(f"context_policy.{field} must be a list")
    ids = [value.strip() for value in raw if isinstance(value, str) and value.strip()]
    if len(ids) != len(raw) or len(ids) != len(set(ids)):
        raise RuntimeError(f"context_policy.{field} must contain unique non-empty IDs")
    return ids, field


def _selected_policy_skill_candidate(
    candidates: list[dict[str, Any]],
    policy: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str], str]:
    initial_load_ids, policy_field = _policy_initial_load_ids(policy)
    candidate_ids = [str(row.get("id") or "").strip() for row in candidates]
    if any(not entity_id for entity_id in candidate_ids) or len(candidate_ids) != len(
        set(candidate_ids)
    ):
        raise RuntimeError("catalog candidates must contain unique non-empty IDs")
    by_id = dict(zip(candidate_ids, candidates, strict=True))
    selected = next(
        (
            by_id[entity_id]
            for entity_id in initial_load_ids
            if entity_id in by_id
            and str(by_id[entity_id].get("type") or "").strip().lower() == "skill"
            and by_id[entity_id].get("installable") is True
        ),
        None,
    )
    return selected, initial_load_ids, policy_field


def classify_catalog_match_evidence(
    candidate: Mapping[str, Any],
    *,
    language: str,
) -> dict[str, Any]:
    """Decide whether recommendation match evidence adds intent beyond language."""
    raw_language = str(language or "").strip().lower()
    canonical_language = next(
        (
            canonical
            for canonical, aliases in _LANGUAGE_TAG_ALIASES.items()
            if raw_language == canonical or raw_language in aliases
        ),
        raw_language,
    )
    raw_tags = candidate.get("matching_tags")
    normalized: list[str] = []
    valid = bool(canonical_language)
    if not isinstance(raw_tags, list) or not raw_tags:
        valid = False
    else:
        for value in raw_tags:
            if not isinstance(value, str) or not value.strip():
                valid = False
                break
            raw_tag = value.strip().lower()
            normalized.append(
                next(
                    (
                        canonical
                        for canonical, aliases in _LANGUAGE_TAG_ALIASES.items()
                        if raw_tag == canonical or raw_tag in aliases
                    ),
                    raw_tag,
                )
            )
        if len(normalized) != len(set(normalized)):
            valid = False
    if not valid:
        return {
            "decision": "abstain",
            "reason": "insufficient_match_evidence",
            "language": canonical_language,
            "matching_tags": sorted(set(normalized)),
        }
    matching_tags = sorted(set(normalized))
    foreign_languages = (set(matching_tags) & set(_LANGUAGE_TAG_ALIASES)) - {canonical_language}
    if foreign_languages:
        return {
            "decision": "abstain",
            "reason": "conflicting_language_match",
            "language": canonical_language,
            "matching_tags": matching_tags,
        }
    if set(matching_tags) == {canonical_language}:
        return {
            "decision": "abstain",
            "reason": "language_only_match",
            "language": canonical_language,
            "matching_tags": matching_tags,
        }
    intent_tags = set(matching_tags) - {canonical_language} - _NON_INTENT_MATCH_TAGS
    if not intent_tags:
        return {
            "decision": "abstain",
            "reason": "constraint_only_match",
            "language": canonical_language,
            "matching_tags": matching_tags,
        }
    return {
        "decision": "load",
        "reason": "intent_match",
        "language": canonical_language,
        "matching_tags": matching_tags,
    }


def decide_deferred_activation(
    *,
    stage: str,
    candidates: list[dict[str, Any]],
    context_policy: Mapping[str, Any],
    task_evidence: Mapping[str, Any],
    activation_policy: Mapping[str, Any] | None = None,
    run_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    oracle = (
        "evaluator",
        "hidden",
        "oracle",
        "reference_patch",
        "reference.patch",
        "test_body",
    )
    if stage not in {"pre-solve", "post-solve"}:
        raise ValueError("stage must be pre-solve or post-solve")
    policy = dict(activation_policy or {})

    def validate_payload(
        value: object,
        *,
        allowed: set[str],
        field: str,
    ) -> None:
        if not isinstance(value, Mapping):
            raise ValueError(f"{field} must be an object")

        def reject_oracle(item: object) -> None:
            if isinstance(item, Mapping):
                for key, nested in item.items():
                    if any(marker in str(key).lower() for marker in oracle):
                        raise ValueError("deferred activation rejects hidden-oracle evidence")
                    reject_oracle(nested)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    reject_oracle(nested)
            elif isinstance(item, str) and any(marker in item.lower() for marker in oracle):
                raise ValueError("deferred activation rejects hidden-oracle evidence")

        reject_oracle(value)
        unknown = {str(key) for key in value} - allowed
        if unknown:
            raise ValueError(f"{field} contains unsupported keys: {sorted(unknown)}")

    validate_payload(
        task_evidence,
        allowed={
            "language",
            "task_category",
            "predeclared_risks",
            "local_code_task",
            "no_api_keys",
            "repository_paths",
            "tags",
        },
        field="task_evidence",
    )
    validate_payload(
        policy,
        allowed={
            "enabled",
            "allowed_entity_types",
            "allowed_tools",
            "allowed_permissions",
            "max_risk",
        },
        field="activation_policy",
    )
    validate_payload(
        run_evidence or {},
        allowed={"diff_non_empty", "changed_paths"},
        field="run_evidence",
    )
    validate_payload(
        context_policy,
        allowed={
            "baseline",
            "keep",
            "load",
            "initial_load",
            "deferred",
            "manual",
            "unload",
            "replace",
        },
        field="context_policy",
    )

    def strings(value: object, field: str) -> list[str]:
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"{field} must be a string list")
        return [item.strip().lower() for item in value]

    if stage == "pre-solve" and run_evidence is not None:
        raise ValueError("pre-solve does not accept run evidence")
    deferred = context_policy.get("deferred") or []
    if not isinstance(deferred, list) or not all(isinstance(item, str) for item in deferred):
        raise ValueError("context_policy.deferred must be a string list")
    candidate_ids: list[str] = []
    for row in candidates:
        entity_id = str(row.get("id") or "").strip()
        entity_type = str(row.get("type") or "").lower()
        if not entity_id or entity_type not in {"agent", "mcp-server", "skill"}:
            raise ValueError("deferred activation candidate has invalid id or type")
        if not entity_id.startswith(f"{entity_type}:"):
            raise ValueError(f"invalid deferred candidate type: {entity_id}")
        candidate_ids.append(entity_id)
    if len(candidate_ids) != len(set(candidate_ids)) or len(deferred) != len(set(deferred)):
        raise ValueError("deferred activation candidate ids must be unique")
    by_id = dict(zip(candidate_ids, candidates, strict=True))
    retained: list[dict[str, Any]] = []
    for entity_id in deferred:
        if entity_id not in by_id:
            raise ValueError(f"deferred candidate is missing: {entity_id}")
        row = by_id[entity_id]
        entity_type = str(row.get("type") or "").lower()
        tags = strings(row.get("tags") or [], f"{entity_id}.tags")
        permissions = strings(row.get("permissions") or [], f"{entity_id}.permissions")
        source = str(row.get("source") or "")
        raw_source_path = row.get("source_path")
        source_path = (
            _safe_relative_path(raw_source_path, field=f"{entity_id}.source_path")
            if isinstance(raw_source_path, str) and raw_source_path.strip()
            else None
        )
        status = str(row.get("load_status") or row.get("status") or "unknown").lower()
        retained.append(
            {
                "id": entity_id,
                "type": entity_type,
                "loadability": row.get("installable") is True
                and status in {"active", "available", "installed", "loaded", "local-wiki"}
                and bool(source and source_path),
                "permissions": permissions,
                "risk": str(row.get("risk") or "unknown").lower(),
                "tags": tags,
                "external": row.get("external"),
                "requires_api_keys": row.get("requires_api_keys"),
                "provenance": {"source": source, "source_path": source_path},
            }
        )
    result: dict[str, Any] = dict(
        decision="deny",
        selected_ids=[],
        reason="policy_disabled_default_deny",
        deferred_candidates=retained,
    )
    if policy.get("enabled") is not True:
        return result
    allowed_types = set(strings(policy.get("allowed_entity_types", []), "allowed types"))
    allowed_tools = set(strings(policy.get("allowed_tools", []), "allowed tools"))
    allowed_permissions = set(strings(policy.get("allowed_permissions", []), "permissions"))
    risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    max_risk = str(policy.get("max_risk") or "")
    if max_risk not in risk_order:
        raise ValueError("max_risk must be low, medium, high, or critical")
    risks = strings(task_evidence.get("predeclared_risks", []), "predeclared risks")
    terms = set(strings(task_evidence.get("tags", []), "tags") + risks)
    terms.add(str(task_evidence.get("language") or "").lower())
    expected_type = "mcp-server" if stage == "pre-solve" else "agent"
    if not all(task_evidence.get(key) is True for key in ("local_code_task", "no_api_keys")):
        result["reason"] = "task_not_local_no_key"
        return result
    if stage == "post-solve" and not (
        risks
        and run_evidence
        and run_evidence.get("diff_non_empty") is True
        and run_evidence.get("changed_paths")
    ):
        result["reason"] = "post_solve_requires_diff_and_predeclared_risk"
        return result
    for row in retained:
        risk_allowed = row["risk"] in risk_order and risk_order[row["risk"]] <= risk_order[max_risk]
        checks = (
            (row["type"] == expected_type, "wrong_stage_type"),
            (row["type"] in allowed_types, "type_not_allowed"),
            (row["id"].lower() in allowed_tools, "tool_not_allowed"),
            (row["loadability"], "not_loadable"),
            (
                row["external"] is False
                and row["requires_api_keys"] is False
                and {"local", "no-api-key"} <= set(row["tags"]),
                "not_local_no_key",
            ),
            (set(row["permissions"]) <= allowed_permissions, "permission_denied"),
            (row["risk"] in risk_order, "risk_unknown"),
            (risk_allowed, "risk_exceeds_policy"),
            (bool(set(row["tags"]) & terms), "no_public_evidence_match"),
        )
        reason = next((reason for allowed, reason in checks if not allowed), None)
        if reason is None:
            result.update(decision="select", selected_ids=[row["id"]], reason=f"selected_{stage}")
            return result
        result["reason"] = reason
    return result


def recommend_production_catalog(
    scenario: Scenario,
    *,
    home: Path,
    lifecycle_root: Path,
    session_id: str,
    snapshot: CatalogSnapshot,
) -> dict[str, Any]:
    """Query the shipped MCP surface and optionally fetch one policy-selected skill."""
    surface_started = time.perf_counter()
    recommendation_seconds = 0.0
    body_fetch_seconds = 0.0
    with _catalog_mcp_client(
        home=home,
        lifecycle_root=lifecycle_root,
        session_id=session_id,
    ) as client:
        recommendation_started = time.perf_counter()
        raw_recommendation = client.call_tool(
            "ctx__recommend_bundle",
            {
                "query": scenario.query,
                "top_k": 5,
                "local_code_task": True,
                "no_api_keys": True,
                "language": scenario.language,
            },
        )
        recommendation_seconds = time.perf_counter() - recommendation_started
        payload = json.loads(raw_recommendation)
        if not isinstance(payload, dict) or payload.get("error"):
            raise RuntimeError(f"production catalog recommendation failed: {payload!r}")
        raw_candidates = payload.get("results")
        policy = payload.get("context_policy")
        if not isinstance(raw_candidates, list) or not isinstance(policy, dict):
            raise RuntimeError("production catalog response omitted results or context_policy")
        candidates = [dict(row) for row in raw_candidates if isinstance(row, dict)]
        if len(candidates) != len(raw_candidates):
            raise RuntimeError("production catalog returned a malformed candidate")
        candidate_ids = [str(row.get("id") or "").strip() for row in candidates]
        if any(not entity_id for entity_id in candidate_ids):
            raise RuntimeError("production catalog returned a candidate without an id")
        deferred_activation = decide_deferred_activation(
            stage="pre-solve",
            candidates=candidates,
            context_policy=policy,
            task_evidence={
                "language": scenario.language,
                "local_code_task": True,
                "no_api_keys": True,
            },
        )
        selected_row, initial_load_ids, policy_field = _selected_policy_skill_candidate(
            candidates,
            policy,
        )
        selected_item: dict[str, Any] | None = None
        body_provenance: dict[str, Any] | None = None
        policy_abstention: dict[str, Any] | None = None
        selection_skip_reason: str | None = None
        raw_wiki = ""
        if selected_row is not None:
            entity_id = str(selected_row["id"])
            match_evidence = classify_catalog_match_evidence(
                selected_row,
                language=scenario.language,
            )
            if match_evidence["decision"] == "abstain":
                policy_abstention = {"candidate_id": entity_id, **match_evidence}
                selection_skip_reason = str(match_evidence["reason"])
            else:
                slug = str(selected_row.get("name") or entity_id.partition(":")[2]).strip()
                body_started = time.perf_counter()
                raw_wiki = client.call_tool(
                    "ctx__wiki_get",
                    {"slug": slug, "entity_type": "skill"},
                )
                body_fetch_seconds = time.perf_counter() - body_started
                wiki_payload = json.loads(raw_wiki)
                if (
                    not isinstance(wiki_payload, dict)
                    or wiki_payload.get("error")
                    or wiki_payload.get("slug") != slug
                    or wiki_payload.get("entity_type") != "skill"
                ):
                    raise RuntimeError(f"production catalog wiki lookup failed for {entity_id}")
                body = str(wiki_payload.get("body") or "").strip()
                body_bytes = body.encode("utf-8")
                if not body:
                    selection_skip_reason = "selected skill body was empty"
                elif len(body_bytes) > PRODUCTION_CATALOG_BODY_MAX_BYTES:
                    selection_skip_reason = (
                        f"selected skill body exceeded {PRODUCTION_CATALOG_BODY_MAX_BYTES} bytes"
                    )
                else:
                    wiki_path = _safe_relative_path(
                        wiki_payload.get("path"),
                        field=f"{entity_id}.wiki_path",
                    )
                    frontmatter = wiki_payload.get("frontmatter")
                    frontmatter = dict(frontmatter) if isinstance(frontmatter, dict) else {}
                    selected_item = {
                        "id": entity_id,
                        "type": "skill",
                        "slug": slug,
                        "body": body,
                    }
                    body_provenance = {
                        "surface": "ctx MCP ctx__wiki_get",
                        "wiki_path": wiki_path,
                        "wiki_response_sha256": hashlib.sha256(
                            raw_wiki.encode("utf-8")
                        ).hexdigest(),
                        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
                        "body_bytes": len(body_bytes),
                        "frontmatter_source": frontmatter.get("source"),
                        "frontmatter_license": frontmatter.get("license"),
                        "candidate_source": selected_row.get("source"),
                        "candidate_source_path": selected_row.get("source_path"),
                        "catalog_archive_sha256": snapshot.provenance["archive_sha256"],
                        "catalog_graph_export_id": snapshot.provenance["graph_export_id"],
                    }
        elif initial_load_ids:
            selection_skip_reason = (
                "context_policy initial load contained no loadable skill candidate"
            )

    return {
        "query": scenario.query,
        "candidates": candidates,
        "candidate_ids": candidate_ids,
        "context_policy": dict(policy),
        "policy_field": policy_field,
        "policy_initial_load_ids": initial_load_ids,
        "deferred_activation": deferred_activation,
        "selected_item": selected_item,
        "selected_ids": [selected_item["id"]] if selected_item is not None else [],
        "body_provenance": body_provenance,
        "policy_abstention": policy_abstention,
        "selection_skip_reason": selection_skip_reason,
        "recommendation_response_sha256": hashlib.sha256(
            raw_recommendation.encode("utf-8")
        ).hexdigest(),
        "recommendation_seconds": recommendation_seconds,
        "body_fetch_seconds": body_fetch_seconds,
        "surface_seconds": time.perf_counter() - surface_started,
    }


def production_catalog_context_prompt(catalog: dict[str, Any]) -> str:
    selected = catalog.get("selected_item")
    if not isinstance(selected, dict):
        return ""
    body = str(selected.get("body") or "").strip()
    if not body or len(body.encode("utf-8")) > PRODUCTION_CATALOG_BODY_MAX_BYTES:
        raise ValueError("production catalog selected body is missing or exceeds the prompt bound")
    return f"\n\nCTX SELECTED PRODUCTION CATALOG CONTEXT\n[SKILL {selected['slug']}]\n{body}\n"


def verify_production_policy_abstention(
    catalog: Mapping[str, Any],
    *,
    language: str,
    task_prompt_sha256: str,
    delivered_prompt_sha256: str,
    model_turn_observed: bool,
    evaluator_isolation_verified: bool,
    mcp_used: bool,
    agent_attempted: bool,
    lifecycle_events: list[dict[str, Any]] | None = None,
) -> bool:
    evidence = catalog.get("policy_abstention")
    candidate_id = evidence.get("candidate_id") if isinstance(evidence, dict) else None
    candidates = catalog.get("candidates")
    policy = catalog.get("context_policy")
    if (
        not isinstance(candidate_id, str)
        or not isinstance(candidates, list)
        or not all(isinstance(row, dict) for row in candidates)
        or not isinstance(policy, dict)
    ):
        return False
    typed_candidates = [dict(row) for row in candidates]
    derived_candidate_ids = [str(row.get("id") or "").strip() for row in typed_candidates]
    try:
        selected, initial_load_ids, policy_field = _selected_policy_skill_candidate(
            typed_candidates,
            policy,
        )
    except RuntimeError:
        return False
    if selected is None or str(selected.get("id") or "") != candidate_id:
        return False
    expected = {
        "candidate_id": candidate_id,
        **classify_catalog_match_evidence(selected, language=language),
    }
    body_fetch_seconds = catalog.get("body_fetch_seconds")
    checks = (
        evidence == expected,
        expected["decision"] == "abstain",
        catalog.get("candidate_ids") == derived_candidate_ids,
        catalog.get("policy_field") == policy_field,
        catalog.get("policy_initial_load_ids") == initial_load_ids,
        catalog.get("selected_ids") == [],
        catalog.get("selected_item") is None,
        catalog.get("body_provenance") is None,
        isinstance(body_fetch_seconds, int | float),
        not isinstance(body_fetch_seconds, bool),
        body_fetch_seconds == 0.0,
        re.fullmatch(r"[0-9a-f]{64}", task_prompt_sha256) is not None,
        task_prompt_sha256 == delivered_prompt_sha256,
        model_turn_observed,
        evaluator_isolation_verified,
        mcp_used is False,
        agent_attempted is False,
    )
    if not all(checks):
        return False
    if lifecycle_events is None:
        return True
    if len(lifecycle_events) != 2:
        return False
    recommendation_event, terminal_event = lifecycle_events
    payload = recommendation_event.get("payload")
    return bool(
        recommendation_event.get("action") == "dev_event"
        and recommendation_event.get("event_type") == "catalog_recommendation"
        and isinstance(payload, dict)
        and payload.get("candidate_ids") == derived_candidate_ids
        and payload.get("selected_ids") == []
        and payload.get("context_policy") == policy
        and terminal_event.get("action") == "session_end"
        and terminal_event.get("status") == "passed"
    )


def production_skill_use_evidence_reason(
    *,
    production_catalog: bool,
    ctx_enabled: bool,
    context_delivery_verified: bool,
    policy_abstention_verified: bool = False,
) -> str | None:
    if not production_catalog or not ctx_enabled:
        return None
    if policy_abstention_verified:
        return "policy_abstained_before_context_delivery"
    if not context_delivery_verified:
        return "no_skill_delivered"
    return "provider_does_not_expose_semantic_context_attribution"


def write_catalog_recommendation_evidence(
    path: Path,
    catalog: dict[str, Any],
    *,
    used_ids: list[str],
    snapshot: CatalogSnapshot,
) -> None:
    path.write_text(
        json.dumps(
            {
                "query": catalog["query"],
                "candidate_ids": catalog["candidate_ids"],
                "selected_ids": catalog["selected_ids"],
                "used_ids": used_ids,
                "candidates": catalog["candidates"],
                "context_policy": catalog["context_policy"],
                "policy_field": catalog["policy_field"],
                "policy_initial_load_ids": catalog["policy_initial_load_ids"],
                "deferred_activation": catalog.get("deferred_activation"),
                "policy_abstention": catalog.get("policy_abstention"),
                "selection_skip_reason": catalog["selection_skip_reason"],
                "body_provenance": catalog["body_provenance"],
                "recommendation_response_sha256": catalog["recommendation_response_sha256"],
                "catalog_provenance": snapshot.provenance,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def catalog_lifecycle_evidence(
    lifecycle_root: Path,
    *,
    session_id: str,
    store: Any,
) -> dict[str, Any]:
    path = lifecycle_root / "events.jsonl"
    if not path.is_file():
        raise RuntimeError("production catalog treatment produced no lifecycle ledger")
    content = path.read_bytes()
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
        row = json.loads(line)
        if not isinstance(row, dict):
            raise RuntimeError(f"catalog lifecycle line {line_number} is not an object")
        if row.get("session_id") == session_id:
            events.append(row)
    if not events or events[-1].get("action") != "session_end":
        raise RuntimeError("production catalog lifecycle did not end the selected session")
    state = store.session_state(session_id=session_id)
    loaded = state.get("loaded") if isinstance(state, dict) else None
    if loaded:
        raise RuntimeError("production catalog lifecycle ended with loaded context")
    return {
        "events": events,
        "actions": [str(event.get("action") or "") for event in events],
        "sha256": hashlib.sha256(content).hexdigest(),
        "final_loaded": [],
        "session_status": events[-1].get("status"),
    }


def arms_for_mode(mode: str) -> tuple[str, ...]:
    if mode == "both":
        return ("baseline", "ctx-light")
    if mode == "all":
        return TREATMENT_ARMS
    if mode not in TREATMENT_ARMS:
        raise ValueError(f"unknown benchmark arm: {mode}")
    return (mode,)


def ordered_arms(scenario_id: str, trial: int, arms: tuple[str, ...]) -> tuple[str, ...]:
    if trial < 1:
        raise ValueError("trial must be >= 1")
    digest = int(hashlib.sha256(scenario_id.encode()).hexdigest()[:8], 16)
    if set(arms) == set(TREATMENT_ARMS) and len(arms) == len(TREATMENT_ARMS):
        return ARM_PERMUTATIONS[(digest + trial - 1) % len(ARM_PERMUTATIONS)]
    if len(arms) == 2 and (digest + trial - 1) % 2:
        return tuple(reversed(arms))
    return arms


def trial_schedule(
    scenarios: list[Scenario], arms: tuple[str, ...], trials: int
) -> list[dict[str, Any]]:
    return [
        {
            "scenario": scenario.id,
            "trial": trial,
            "arms": list(ordered_arms(scenario.id, trial, arms)),
        }
        for scenario in scenarios
        for trial in range(1, trials + 1)
    ]


def treatment_policy_valid(
    treatment_level: str,
    *,
    skill_used: bool,
    mcp_used: bool,
    agent_attempted: bool,
    agent_used: bool,
) -> bool:
    if treatment_level == "baseline":
        return True
    if treatment_level == "ctx-light":
        return skill_used and not mcp_used and not agent_attempted
    if treatment_level == "ctx-full":
        return skill_used and mcp_used and agent_used
    raise ValueError(f"unsupported treatment level: {treatment_level}")


def next_treatment_level(
    arm: str,
    current: str,
    *,
    agent_returncode: object,
    agent_timed_out: object,
    policy_valid: object,
    verification_returncode: object,
) -> str:
    if (
        arm == "ctx-light"
        and current == "ctx-light"
        and agent_returncode == 0
        and agent_timed_out is False
        and policy_valid is True
        and verification_returncode == 1
    ):
        return "ctx-full"
    return current


def make_lifecycle_store(root: Path) -> Any:
    from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore  # noqa: PLC0415

    return RuntimeLifecycleStore(root=root)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def mcp_config(python: str) -> list[str]:
    return [
        "-c",
        f"mcp_servers.ctx-wiki.command={_toml_string(python)}",
        "-c",
        'mcp_servers.ctx-wiki.args=["-m","ctx.mcp_server.server"]',
        "-c",
        'mcp_servers.ctx-wiki.default_tools_approval_mode="approve"',
        "-c",
        'mcp_servers.ctx-wiki.enabled_tools=["ctx__wiki_get"]',
        "-c",
        "mcp_servers.ctx-wiki.required=true",
    ]


def preflight_ctx_mcp(
    scenario: Scenario,
    *,
    home: Path,
    lifecycle_root: Path,
    session_id: str,
) -> dict[str, Any]:
    from ctx.adapters.generic.tools import McpClient, McpServerConfig  # noqa: PLC0415

    skill = next(item for item in scenario.context if item["type"] == "skill")
    config = McpServerConfig(
        name="ctx-wiki",
        command=sys.executable,
        args=("-m", "ctx.mcp_server.server"),
        env={
            "HOME": str(home),
            "PYTHONPATH": str(ROOT / "src"),
            "CTX_RUNTIME_LIFECYCLE_DIR": str(lifecycle_root),
            "CTX_TELEMETRY_ENABLED": "0",
        },
        startup_timeout=5.0,
        request_timeout=5.0,
    )
    client = McpClient(config, session_id=session_id)
    try:
        client.start()
        names = {tool.name for tool in client.list_tools()}
        required = {"ctx__wiki_get", "ctx__recommend_bundle"}
        if missing := required - names:
            raise RuntimeError(f"ctx MCP missing tools: {sorted(missing)}")
        raw = client.call_tool(
            "ctx__wiki_get",
            {"slug": skill["slug"], "entity_type": skill["type"]},
        )
        payload = json.loads(raw)
        if (
            payload.get("slug") != skill["slug"]
            or payload.get("entity_type") != skill["type"]
            or str(skill["body"]).strip() not in str(payload.get("body") or "")
        ):
            raise RuntimeError("ctx MCP returned the wrong benchmark fixture")
        return {
            "status": "passed",
            "tool_count": len(names),
            "probe": "ctx__wiki_get",
            "fixture": f"{skill['type']}:{skill['slug']}",
            "response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
    finally:
        client.stop()


def task_prompt(scenario: Scenario) -> str:
    verify_argv = [part.replace("{python}", sys.executable) for part in scenario.verify]
    verify = f"PYTHONPATH=src {shlex.join(verify_argv)}"
    return (
        "Implement the feature below in this repository.\n\n"
        f"TASK\n{scenario.task}\n\n"
        "REQUIRED LOOP\n"
        "1. Plan: inspect the relevant code and state a short implementation plan.\n"
        "2. Code: implement the smallest complete change that follows repository conventions.\n"
        "3. Test: run the focused verification command and fix failures.\n"
        "4. Check: review the final diff and run git diff --check.\n\n"
        f"FOCUSED VERIFICATION\n{verify}\n\n"
        f"The evaluator owns {scenario.test_path}; do not edit or delete it. "
        "Do not modify any test, test configuration, or import configuration; the "
        "provided PYTHONPATH already selects this clone's source. "
        "Finish only when the focused verification and diff check pass."
    )


def production_catalog_task_prompt(scenario: Scenario) -> str:
    return (
        "Implement the feature below in this repository.\n\n"
        f"TASK\n{scenario.task}\n\n"
        "REQUIRED LOOP\n"
        "1. Plan: inspect the relevant code and state a short implementation plan.\n"
        "2. Code: implement the smallest complete change that follows repository conventions.\n"
        "3. Test: run the repository tests that are relevant to the changed behavior.\n"
        "4. Check: review the final diff and run git diff --check.\n\n"
        "Do not modify any test, test configuration, or import configuration. "
        "Finish only when the implementation and the repository checks you selected pass."
    )


def context_prompt(scenario: Scenario, treatment_level: str) -> str:
    skill = next(item for item in scenario.context if item["type"] == "skill")
    if treatment_level == "ctx-light":
        return (
            "\n\nCTX SELECTED CONTEXT\n"
            f"[SKILL {skill['slug']}]\n{str(skill['body']).strip()}\n\n"
            "Use this selected local skill when relevant. ctx did not select an MCP "
            "or delegated reviewer for this small task; do not add either one."
        )
    if treatment_level != "ctx-full":
        raise ValueError(f"unsupported ctx treatment: {treatment_level}")
    reviewer = next(item for item in scenario.context if item["type"] == "agent")
    marker = f"CTX_REVIEWER:{reviewer['slug']}"
    return (
        "\n\nCTX FULL TREATMENT\n"
        "The ctx-wiki MCP is active for this explicitly selected full treatment. "
        "During Plan, call "
        f"ctx__wiki_get for skill {skill['slug']!r} with entity_type='skill'. "
        "Use the returned skill body instead of searching for another workflow. "
        "After coding and focused tests, use spawn_agent once to delegate this exact "
        "bounded review, then wait once for completion. Begin the spawn_agent prompt "
        f"with exactly {marker!r}, followed by:\n"
        f"[AGENT {reviewer['slug']}]\n{str(reviewer['body']).strip()}\n"
        "Give the reviewer the current working directory, changed-file list, and "
        "focused verification command. Address actionable findings, close the "
        "reviewer without repeating its report, then finish git diff --check."
    )


def _jsonl_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def extract_token_usage(stdout: str) -> dict[str, Any]:
    terminal: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(_jsonl_events(stdout)):
        usage = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(usage, dict):
            terminal.append((index, usage))
    if not terminal:
        return {"attribution": "unavailable", "reason": "Codex JSONL exposed no usage"}
    event_index, usage = terminal[-1]
    required = ("input_tokens", "cached_input_tokens", "output_tokens")
    if not all(isinstance(usage.get(key), int) and usage[key] >= 0 for key in required):
        return {
            "attribution": "unavailable",
            "reason": "terminal turn.completed usage was incomplete",
        }
    normalized = {key: int(usage[key]) for key in required}
    if normalized["cached_input_tokens"] > normalized["input_tokens"]:
        return {
            "attribution": "unavailable",
            "reason": "terminal turn.completed cached input exceeded total input",
        }
    normalized["uncached_input_tokens"] = (
        normalized["input_tokens"] - normalized["cached_input_tokens"]
    )
    for key in ("cache_write_input_tokens", "reasoning_output_tokens"):
        if isinstance(usage.get(key), int) and usage[key] >= 0:
            normalized[key] = int(usage[key])
    normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    return {
        "attribution": "exact",
        "attribution_source": "terminal turn.completed.usage",
        "usage_event_index": event_index,
        **normalized,
    }


def extract_trace_efficiency(stdout: str) -> dict[str, int]:
    completed_items = [
        event["item"]
        for event in _jsonl_events(stdout)
        if event.get("type") == "item.completed" and isinstance(event.get("item"), dict)
    ]
    commands = [item for item in completed_items if item.get("type") == "command_execution"]
    messages = [item for item in completed_items if item.get("type") == "agent_message"]
    command_outputs = [
        len(str(item.get("aggregated_output") or "").encode("utf-8")) for item in commands
    ]
    normalized_commands = [" ".join(str(item.get("command") or "").split()) for item in commands]
    return {
        "completed_item_count": len(completed_items),
        "tool_command_count": len(commands),
        "tool_failure_count": sum(item.get("exit_code") not in {0, None} for item in commands),
        "tool_output_bytes": sum(command_outputs),
        "max_tool_output_bytes": max(command_outputs, default=0),
        "oversized_tool_output_count": sum(
            size > PRODUCTION_TOOL_OUTPUT_LIMIT_BYTES for size in command_outputs
        ),
        "repeated_tool_command_count": len(normalized_commands) - len(set(normalized_commands)),
        "agent_message_count": len(messages),
        "agent_message_bytes": sum(
            len(str(item.get("text") or "").encode("utf-8")) for item in messages
        ),
    }


def _mcp_result_payload(result: object) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    structured = result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        try:
            payload = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def observed_mcp_tool_use(stdout: str, *, slug: str, entity_type: str, expected_body: str) -> bool:
    for event in _jsonl_events(stdout):
        item = event.get("item")
        arguments = item.get("arguments") if isinstance(item, dict) else None
        payload = _mcp_result_payload(item.get("result")) if isinstance(item, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "mcp_tool_call"
            and item.get("server") == "ctx-wiki"
            and item.get("tool") == "ctx__wiki_get"
            and isinstance(arguments, dict)
            and arguments.get("slug") == slug
            and arguments.get("entity_type") == entity_type
            and item.get("status") == "completed"
            and item.get("error") is None
            and isinstance(payload, dict)
            and payload.get("slug") == slug
            and payload.get("entity_type") == entity_type
            and expected_body.strip() in str(payload.get("body") or "")
        ):
            return True
    return False


def required_tool_failures(stdout: str) -> list[str]:
    failures: list[str] = []
    for event in _jsonl_events(stdout):
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        item_type = item.get("type")
        tool = str(item.get("tool") or "")
        required = (
            item_type == "mcp_tool_call"
            and item.get("server") == "ctx-wiki"
            and tool == "ctx__wiki_get"
        ) or (item_type == "collab_tool_call" and tool in {"spawn_agent", "wait", "close_agent"})
        if not required or (item.get("status") == "completed" and item.get("error") is None):
            continue
        error = item.get("error")
        detail = str(error.get("message") if isinstance(error, dict) else error or "")
        failures.append(f"{item_type}:{tool} status={item.get('status')}: {detail}".rstrip())
    return failures


def observed_agent_attempt(stdout: str) -> bool:
    return any(
        isinstance(event.get("item"), dict)
        and event["item"].get("type") == "collab_tool_call"
        and event["item"].get("tool") == "spawn_agent"
        for event in _jsonl_events(stdout)
    )


def observed_agent_review(stdout: str, *, reviewer_slug: str, expected_instructions: str) -> bool:
    states: dict[str, int] = {}
    for event in _jsonl_events(stdout):
        item = event.get("item")
        if (
            event.get("type") != "item.completed"
            or not isinstance(item, dict)
            or item.get("type") != "collab_tool_call"
            or item.get("status") != "completed"
            or item.get("error") is not None
        ):
            continue
        if item.get("tool") == "spawn_agent":
            receivers = item.get("receiver_thread_ids")
            prompt = item.get("prompt")
            marker = f"CTX_REVIEWER:{reviewer_slug}"
            if (
                isinstance(receivers, list)
                and isinstance(prompt, str)
                and prompt.startswith(marker)
                and expected_instructions.strip() in prompt
            ):
                for receiver in receivers:
                    if receiver:
                        states.setdefault(str(receiver), 1)
        elif item.get("tool") == "wait":
            agent_states = item.get("agents_states")
            if isinstance(agent_states, dict):
                for agent_id, state in agent_states.items():
                    key = str(agent_id)
                    if (
                        states.get(key) == 1
                        and isinstance(state, dict)
                        and state.get("status") == "completed"
                        and isinstance(state.get("message"), str)
                        and state["message"].strip()
                    ):
                        states[key] = 2
        elif item.get("tool") == "close_agent":
            receivers = item.get("receiver_thread_ids")
            if isinstance(receivers, list):
                for receiver in receivers:
                    key = str(receiver)
                    if states.get(key) == 2:
                        states[key] = 3
    return 3 in states.values()


def observed_model_turn(stdout: str) -> bool:
    return any(
        event.get("type") in {"turn.started", "turn.completed"} for event in _jsonl_events(stdout)
    )


def close_context_session(
    store: Any,
    selected_items: list[dict[str, Any]],
    *,
    session_id: str,
    model: str,
    status: str,
    usage_evidence: dict[str, str],
    mark_applied: bool = False,
) -> dict[str, float]:
    use_seconds = 0.0
    unload_seconds = 0.0
    for item in selected_items:
        entity_type = str(item["type"])
        if evidence := usage_evidence.get(entity_type):
            used_started = time.perf_counter()
            store.mark_entity_used(
                session_id=session_id,
                entity_type=entity_type,
                slug=str(item["slug"]),
                evidence=evidence,
                token_usage={
                    "attribution": "unavailable",
                    "attribution_reason": "Codex reports session tokens, not per-context tokens",
                    "model": model,
                },
            )
            use_seconds += time.perf_counter() - used_started
        unload_started = time.perf_counter()
        store.unload_entity(
            session_id=session_id,
            entity_type=entity_type,
            slug=str(item["slug"]),
            reason="ephemeral benchmark process ended",
        )
        if mark_applied:
            store.mark_entity_unloaded(
                session_id=session_id,
                entity_type=entity_type,
                slug=str(item["slug"]),
                reason="selected body removed from the bounded benchmark prompt",
            )
        unload_seconds += time.perf_counter() - unload_started
    session_end_started = time.perf_counter()
    store.end_session(
        session_id=session_id,
        status=status,
        summary="A/B benchmark arm completed",
    )
    return {
        "use_seconds": use_seconds,
        "unload_seconds": unload_seconds,
        "session_end_seconds": time.perf_counter() - session_end_started,
    }


def _agent_runtime_roots() -> list[Path]:
    runtime_roots = {
        (ROOT / ".venv").resolve(),
        Path(sys.executable).resolve().parent.parent,
    }
    runtime_roots.update(
        path
        for path in (
            Path("/Library/Developer/CommandLineTools"),
            Path("/Applications/Xcode.app/Contents/Developer"),
        )
        if path.is_dir()
    )
    return [path for path in sorted(runtime_roots) if path.is_dir()]


def _toml_key(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def prepare_isolated_codex_home(
    home: Path,
    *,
    workspace: Path,
    forbidden_reads: Mapping[str, Path],
) -> Path:
    if not forbidden_reads:
        raise ValueError("production benchmark requires explicit evaluator source paths")
    source = Path(ORIGINAL_CODEX_HOME) / "auth.json"
    if not source.is_file():
        raise RuntimeError(f"Codex authentication file is missing: {source}")
    if home.exists() or home.is_symlink():
        raise RuntimeError(f"isolated Codex home already exists: {home}")
    home.mkdir(mode=stat.S_IRWXU, parents=True)
    home.chmod(stat.S_IRWXU)
    temp = home / "tmp"
    temp.mkdir(mode=stat.S_IRWXU)
    destination = home / "auth.json"
    config = home / "config.toml"
    shutil.copyfile(source, destination)
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
    filesystem = [
        '":minimal" = "read"',
        f'{_toml_key(workspace)} = "write"',
        f'{_toml_key(temp)} = "write"',
        *(f'{_toml_key(path)} = "read"' for path in _agent_runtime_roots()),
        *(f'{_toml_key(path)} = "deny"' for path in forbidden_reads.values()),
        f'{_toml_key(destination)} = "deny"',
        f'{_toml_key(config)} = "deny"',
    ]
    config.write_text(
        "\n".join(
            [
                'default_permissions = "ctx_benchmark"',
                'web_search = "disabled"',
                "",
                "[permissions.ctx_benchmark]",
                'description = "Isolated CTX A/B benchmark agent."',
                "",
                "[permissions.ctx_benchmark.filesystem]",
                *filesystem,
                "",
                "[permissions.ctx_benchmark.network]",
                "enabled = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return destination


def remove_isolated_codex_home(home: Path) -> None:
    if not home.exists() and not home.is_symlink():
        return
    if home.is_symlink():
        home.unlink()
        raise RuntimeError("isolated Codex home was replaced by a symlink")
    shutil.rmtree(home)
    if home.exists():
        raise RuntimeError("isolated Codex home cleanup failed")


def production_agent_env(
    base_env: Mapping[str, str],
    *,
    home: Path,
    workspace: Path,
) -> dict[str, str]:
    temp = home / "tmp"
    env = dict(base_env)
    env.update(
        {
            "CODEX_HOME": str(home),
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TEMP": str(temp),
            "TMP": str(temp),
            "TMPDIR": str(temp),
            "NO_COLOR": "",
            "FORCE_COLOR": "",
            "PYTHONPATH": os.pathsep.join((str(workspace / "src"), str(workspace))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PATH": os.pathsep.join(
                (
                    str(Path(sys.executable).parent),
                    env.get("PATH", ""),
                )
            ),
        }
    )
    return env


def _probe_loopback_network(
    *,
    sandbox_prefix: list[str],
    workspace: Path,
    env: dict[str, str],
) -> dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        host, port = listener.getsockname()
        network_probe = [
            sys.executable,
            "-c",
            (
                "import socket; "
                f"s = socket.create_connection(({host!r}, {port}), timeout=2); "
                "s.close()"
            ),
        ]
        parent = run_process(
            network_probe,
            cwd=workspace,
            env=env,
            timeout=5,
        )
        listener.settimeout(1)
        try:
            parent_connection, _ = listener.accept()
        except TimeoutError:
            parent_connected = False
        else:
            parent_connected = True
            parent_connection.close()
        sandbox = run_process(
            [*sandbox_prefix, *network_probe],
            cwd=workspace,
            env=env,
            timeout=5,
        )
        listener.settimeout(0.25)
        try:
            sandbox_connection, _ = listener.accept()
        except TimeoutError:
            sandbox_connected = False
        else:
            sandbox_connected = True
            sandbox_connection.close()
    return {
        "parent_returncode": parent.returncode,
        "parent_connected": parent_connected,
        "sandbox_returncode": sandbox.returncode,
        "sandbox_connected": sandbox_connected,
    }


def verify_agent_sandbox_isolation(
    *,
    codex: str,
    workspace: Path,
    home: Path,
    env: dict[str, str],
    forbidden_reads: Mapping[str, Path],
    project_check: tuple[str, ...],
) -> dict[str, Any]:
    if not forbidden_reads:
        raise ValueError("production benchmark requires explicit evaluator source paths")
    if not project_check:
        raise ValueError("production benchmark requires an agent project check")
    allowed_source = next(
        (path for path in workspace.rglob("*") if path.is_file() and ".git" not in path.parts),
        None,
    )
    if allowed_source is None:
        raise RuntimeError("benchmark workspace has no source file for sandbox preflight")

    def sandboxed(command: list[str]) -> list[str]:
        return [
            codex,
            "sandbox",
            "-P",
            "ctx_benchmark",
            "-C",
            str(workspace),
            "--",
            *command,
        ]

    allowed = run_process(
        sandboxed(["/bin/cat", str(allowed_source)]),
        cwd=workspace,
        env=env,
        timeout=30,
    )
    canary = workspace / ".ctx-agent-sandbox-write-canary"
    writable = run_process(
        sandboxed(["/usr/bin/touch", str(canary)]),
        cwd=workspace,
        env=env,
        timeout=30,
    )
    canary_created = canary.is_file()
    canary.unlink(missing_ok=True)
    git_canary = run_process(
        sandboxed(["/usr/bin/git", "status", "--short"]),
        cwd=workspace,
        env=env,
        timeout=30,
    )
    project_canary = run_process(
        sandboxed(list(project_check)),
        cwd=workspace,
        env=env,
        timeout=180,
        contain_descendants=True,
    )
    sensitive_reads = {
        "credentials": home / "auth.json",
        "sandbox_config": home / "config.toml",
        **forbidden_reads,
    }
    denied: list[dict[str, Any]] = []
    for label, path in sensitive_reads.items():
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"sensitive benchmark file is missing: {label}")
        with resolved.open("rb") as fh:
            fh.read(1)
        result = run_process(
            sandboxed(["/bin/cat", str(resolved)]),
            cwd=workspace,
            env=env,
            timeout=30,
        )
        denial_text = f"{result.stdout}\n{result.stderr}".lower()
        denied.append(
            {
                "label": label,
                "denied": result.returncode == 1
                and (
                    "operation not permitted" in denial_text or "permission denied" in denial_text
                ),
                "returncode": result.returncode,
            }
        )
    network = _probe_loopback_network(
        sandbox_prefix=sandboxed([]),
        workspace=workspace,
        env=env,
    )
    verified = (
        allowed.returncode == 0
        and writable.returncode == 0
        and canary_created
        and git_canary.returncode == 0
        and project_canary.returncode == 0
        and all(row["denied"] for row in denied)
        and network["parent_returncode"] == 0
        and network["parent_connected"]
        and network["sandbox_returncode"] != 0
        and not network["sandbox_connected"]
    )
    if not verified:
        raise RuntimeError(
            "agent sandbox isolation preflight failed: "
            f"allowed={allowed.returncode}, writable={writable.returncode}, "
            f"canary={canary_created}, git={git_canary.returncode}, "
            f"project={project_canary.returncode}, denied={denied}, "
            f"parent_network={network['parent_returncode']}/{network['parent_connected']}, "
            f"sandbox_network={network['sandbox_returncode']}/{network['sandbox_connected']}"
        )
    return {
        "verified": True,
        "profile": "ctx_benchmark",
        "profile_sha256": _sha256_file(home / "config.toml"),
        "network": "restricted",
        "parent_network_canary_returncode": network["parent_returncode"],
        "network_canary_returncode": network["sandbox_returncode"],
        "git_canary_returncode": git_canary.returncode,
        "project_canary_returncode": project_canary.returncode,
        "allowed_source": str(allowed_source.relative_to(workspace)),
        "forbidden_reads": denied,
    }


def codex_command(
    *,
    codex: str,
    model: str,
    workspace: Path,
    prompt: str,
    with_ctx: bool,
    agent_home: Path | None = None,
    isolate_evaluator: bool = False,
) -> list[str]:
    command = [
        codex,
        "-a",
        "never",
        "--enable" if with_ctx else "--disable",
        "multi_agent",
        "-c",
        'web_search="disabled"',
    ]
    if with_ctx:
        command.extend(mcp_config(sys.executable))
    command.extend(
        [
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--model",
            model,
            "--cd",
            str(workspace),
            prompt,
        ]
    )
    if isolate_evaluator:
        if agent_home is None:
            raise ValueError("isolated agent command requires an isolated Codex home")
        command.insert(command.index("exec") + 1, "--strict-config")
    else:
        command.insert(command.index("exec") + 1, "--ignore-user-config")
        command[command.index("--cd") : command.index("--cd")] = [
            "--sandbox",
            "workspace-write",
        ]
    return command


def production_task_prompt(scenario: Scenario, workspace: Path) -> str:
    sources: list[str] = []
    total_bytes = 0
    for relative in scenario.allowed_changes:
        path = workspace / relative
        if not path.is_file():
            raise RuntimeError(f"production source is missing: {relative}")
        body = path.read_text(encoding="utf-8")
        total_bytes += len(body.encode("utf-8"))
        sources.append(f"--- BEGIN {relative} ---\n{body}\n--- END {relative} ---")
    if total_bytes > 256_000:
        raise RuntimeError("production source context exceeds 256000 bytes")
    allowed = ", ".join(scenario.allowed_changes)
    return (
        "Implement the requested feature using only the supplied source files. "
        "You cannot inspect or modify the filesystem directly in this benchmark.\n\n"
        f"TASK\n{scenario.task}\n\n"
        f"ALLOWED CHANGED PATHS\n{allowed}\n\n"
        'Return exactly one JSON object with one string field named "patch". '
        "The patch must be a valid unified Git patch rooted at the repository, "
        "must change at least one allowed path, and must not change any other path. "
        "Do not wrap the JSON in Markdown and do not include commentary.\n\n"
        "CURRENT SOURCES\n" + "\n\n".join(sources)
    )


def production_ctx_command(
    *,
    model: str,
    prompt: str,
    session_id: str,
    sessions_dir: Path,
    with_ctx: bool,
    api_key_env: str | None,
    base_url: str | None,
    max_iterations: int,
    max_tokens: int | None,
    provider_timeout: float,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "ctx.cli.run",
        "run",
        "--model",
        model,
        "--task",
        prompt,
        "--session-id",
        session_id,
        "--sessions-dir",
        str(sessions_dir),
        "--overwrite-session",
        "--json",
        "--quiet",
        "--max-iterations",
        str(max_iterations),
        "--provider-timeout",
        str(provider_timeout),
    ]
    if api_key_env:
        command.extend(["--api-key-env", api_key_env])
    if base_url:
        command.extend(["--base-url", base_url])
    if max_tokens is not None:
        command.extend(["--max-tokens", str(max_tokens)])
    if with_ctx:
        command.extend(["--ctx-tool-surface", "adaptive"])
        for tool_name in PRODUCTION_CTX_TOOL_NAMES:
            command.extend(["--allow-tool", tool_name])
        # The allow-list excludes namespaced MCP tools, so this anchor stays
        # dormant while ctx run composes adaptive skill leasing with core schemas.
        command.extend(
            [
                "--mcp",
                f"{_PRODUCTION_CTX_MCP_ANCHOR}:"
                + shlex.join([sys.executable, "-m", "ctx.mcp_server.server"]),
            ]
        )
    else:
        command.append("--no-ctx-tools")
    return command


def production_ctx_tool_schemas() -> list[dict[str, Any]]:
    """Return the exact bounded ctx-core schemas submitted by the treatment."""
    from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox  # noqa: PLC0415

    definitions = CtxCoreToolbox(
        bound_session_id="ctx-ab-schema",
        allowed_tool_names=PRODUCTION_CTX_TOOL_NAMES,
    ).tool_definitions()
    names = tuple(definition.name for definition in definitions)
    if set(names) != set(PRODUCTION_CTX_TOOL_NAMES) or len(names) != len(PRODUCTION_CTX_TOOL_NAMES):
        raise RuntimeError(
            "production ctx tool inventory does not match the benchmark allow-list: "
            f"expected={list(PRODUCTION_CTX_TOOL_NAMES)!r}, actual={list(names)!r}"
        )
    return [
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": definition.parameters,
            },
        }
        for definition in definitions
    ]


def validate_provider_request_tool_surface(
    payload: object,
    *,
    expected_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fail closed unless an actual provider request carries the exact schemas."""
    if not isinstance(payload, dict):
        raise ValueError("provider request payload is not an object")
    raw_tools = payload.get("tools")
    actual_tools = [] if raw_tools is None else raw_tools
    if not isinstance(actual_tools, list):
        raise ValueError("provider request tools must be a list when present")
    if not isinstance(expected_tools, list):
        raise ValueError("expected provider tools must be a list")

    def indexed(tools: list[Any], *, label: str) -> dict[str, dict[str, Any]]:
        by_name: dict[str, dict[str, Any]] = {}
        for item in tools:
            if not isinstance(item, dict) or item.get("type") != "function":
                raise ValueError(f"{label} contains a malformed function tool")
            function = item.get("function")
            if not isinstance(function, dict):
                raise ValueError(f"{label} contains a malformed function schema")
            name = function.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"{label} contains a tool without a name")
            if name in by_name:
                raise ValueError(f"{label} contains duplicate tool name: {name!r}")
            by_name[name] = item
        return by_name

    actual_by_name = indexed(actual_tools, label="provider request")
    expected_by_name = indexed(expected_tools, label="expected tool surface")
    missing = sorted(set(expected_by_name) - set(actual_by_name))
    extra = sorted(set(actual_by_name) - set(expected_by_name))
    if missing or extra:
        raise ValueError(
            f"provider request tool surface mismatch: missing={missing!r}, extra={extra!r}"
        )
    for name, expected in expected_by_name.items():
        if actual_by_name[name] != expected:
            raise ValueError(f"provider request schema mismatch for tool: {name!r}")

    canonical = json.dumps(
        [actual_by_name[name] for name in sorted(actual_by_name)],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "provider_request_tool_names": sorted(actual_by_name),
        "provider_request_tool_schema_sha256": hashlib.sha256(canonical).hexdigest(),
        "provider_request_tool_surface_observed": True,
    }


def classify_production_evidence(
    *,
    base_url: str | None,
    dry_run: bool,
    provider_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    endpoint_class = "custom_endpoint" if base_url else "provider_default"
    if dry_run:
        return {
            "endpoint_class": endpoint_class,
            "evidence_level": "wiring_only",
            "production_efficiency_eligible": False,
        }
    provenance = provider_provenance or {}
    positively_evidenced = all(
        provenance.get(field) is True
        for field in (
            "provider_identity_verified",
            "provider_endpoint_verified",
            "provider_authentication_verified",
            "provider_response_success",
        )
    )
    if not positively_evidenced:
        return {
            "endpoint_class": ("custom_endpoint" if base_url else "provider_default_unverified"),
            "evidence_level": "functional_only" if base_url else "functional_unverified",
            "production_efficiency_eligible": False,
        }
    return {
        "endpoint_class": "custom_endpoint" if base_url else "live_provider",
        "evidence_level": "live_provider",
        "production_efficiency_eligible": True,
    }


def classify_codex_controlled_evidence(*, dry_run: bool) -> dict[str, Any]:
    return {
        "endpoint_class": "codex_controlled",
        "evidence_level": ("controlled_wiring_only" if dry_run else "controlled_context_delivery"),
        "production_efficiency_eligible": False,
    }


def classify_codex_production_catalog_evidence(*, dry_run: bool) -> dict[str, Any]:
    return {
        "endpoint_class": "codex_cli_oauth",
        "evidence_level": (
            "production_catalog_wiring_only" if dry_run else "production_catalog_context_delivery"
        ),
        "production_efficiency_eligible": not dry_run,
    }


def extract_provider_response_provenance(
    *,
    sessions_dir: Path,
    session_id: str,
    model: str,
    base_url: str | None,
    api_key_env: str | None,
    env: dict[str, str],
    expected_ctx_tool_names: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    session_path = sessions_dir / f"{session_id}.jsonl"
    if not session_path.is_file():
        raise ValueError("ctx run produced no provider session ledger")
    session_bytes = session_path.read_bytes()
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(session_bytes.decode("utf-8").splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"ctx run provider session ledger contains invalid JSON at line {line_number}"
            ) from exc
        if not isinstance(event, dict):
            raise ValueError(f"ctx run provider session ledger line {line_number} is not an object")
        if event.get("session_id") != session_id:
            raise ValueError(
                "ctx run provider session ledger contains a foreign session: "
                f"expected={session_id!r}, actual={event.get('session_id')!r}"
            )
        events.append(event)

    starts = [event for event in events if event.get("type") == "session_start"]
    responses = [event for event in events if event.get("type") == "model_response"]
    if len(starts) != 1:
        raise ValueError("ctx run provider session ledger must contain one session_start")
    if not responses:
        raise ValueError("ctx run provider session ledger contains no model_response")

    start = starts[0]
    configured_provider = str(start.get("provider") or "").strip()
    configured_model = str(start.get("model") or "").strip()
    configured_base_url = str(start.get("base_url") or "").strip()
    configured_api_key_env = str(start.get("api_key_env") or "").strip()
    configured_ctx_tool_names_raw = start.get("ctx_tool_names")
    if configured_ctx_tool_names_raw is None:
        configured_ctx_tool_names: list[str] = []
    elif (
        not isinstance(configured_ctx_tool_names_raw, list)
        or not all(isinstance(name, str) and name for name in configured_ctx_tool_names_raw)
        or len(set(configured_ctx_tool_names_raw)) != len(configured_ctx_tool_names_raw)
    ):
        raise ValueError("ctx run session has malformed configured ctx tool names")
    else:
        configured_ctx_tool_names = list(configured_ctx_tool_names_raw)
    expected_base_url = base_url or ""
    if not configured_provider:
        raise ValueError("ctx run provider identity is missing from session_start")
    if configured_model != model:
        raise ValueError(
            "ctx run provider session model does not match the requested model: "
            f"expected={model!r}, actual={configured_model!r}"
        )
    if configured_base_url != expected_base_url:
        raise ValueError("ctx run provider session base_url does not match the command")
    if api_key_env and configured_api_key_env != api_key_env:
        raise ValueError("ctx run provider session api_key_env does not match the command")
    if expected_ctx_tool_names is not None and set(configured_ctx_tool_names) != set(
        expected_ctx_tool_names
    ):
        raise ValueError(
            "ctx run configured tool surface mismatch: "
            f"expected={sorted(expected_ctx_tool_names)!r}, "
            f"actual={sorted(configured_ctx_tool_names)!r}"
        )

    response_adapters = {str(event.get("provider") or "").strip() for event in responses}
    response_models = {str(event.get("model") or "").strip() for event in responses}
    reported_response_models = {
        str(event.get("response_model") or "").strip() for event in responses
    }
    finish_reasons = [str(event.get("finish_reason") or "").strip() for event in responses]
    if "" in response_adapters or len(response_adapters) != 1:
        raise ValueError("ctx run provider responses have missing or inconsistent adapters")
    if response_models != {model}:
        raise ValueError("ctx run provider responses do not match the requested model")
    response_success = all(reason in {"stop", "tool_calls"} for reason in finish_reasons)
    response_model_verified = reported_response_models == {model}
    auth_mode = "api_key_env" if configured_api_key_env else "none_or_implicit"
    auth_present = bool(configured_api_key_env and env.get(configured_api_key_env))
    authentication_submitted = all(
        event.get("authentication_submitted") is True for event in responses
    )
    authentication_verified = bool(response_success and auth_present and authentication_submitted)
    expected_endpoint_hash = (
        "sha256:" + hashlib.sha256(configured_base_url.encode("utf-8")).hexdigest()
        if configured_base_url
        else None
    )
    response_endpoint_hashes = {
        str(event.get("request_endpoint_hash") or "").strip() for event in responses
    }
    endpoint_request_verified = (
        response_endpoint_hashes == {expected_endpoint_hash}
        if expected_endpoint_hash is not None
        else response_endpoint_hashes == {""}
    )
    identity_verified = bool(response_success and response_model_verified)
    endpoint_verified = bool(response_success and endpoint_request_verified)
    endpoint_source = (
        "custom_endpoint_from_session_config"
        if configured_base_url
        else "provider_default_from_session_config"
    )
    return {
        "provider_identity": configured_provider,
        "provider_identity_source": (
            "session_start_and_provider_reported_model"
            if identity_verified
            else "session_start_config"
        ),
        "provider_identity_verified": identity_verified,
        "provider_adapter": next(iter(response_adapters)),
        "provider_response_models": sorted(response_models),
        "provider_reported_response_models": sorted(
            model for model in reported_response_models if model
        ),
        "provider_response_model_verified": response_model_verified,
        "provider_response_finish_reasons": finish_reasons,
        "provider_response_count": len(responses),
        "provider_response_success": response_success,
        "provider_request_endpoint_hash_verified": endpoint_request_verified,
        "provider_endpoint_evidence": (
            f"{endpoint_source}_with_matching_request_and_successful_response"
            if endpoint_verified
            else endpoint_source
        ),
        "provider_endpoint_verified": endpoint_verified,
        "provider_auth_mode": auth_mode,
        "provider_request_authentication_submitted": authentication_submitted,
        "provider_authentication_evidence": (
            "credential_submitted_with_successful_response"
            if authentication_verified
            else "credential_submitted_without_successful_response"
            if authentication_submitted
            else "configured_api_key_env_present_but_not_submitted"
            if auth_present
            else "configured_api_key_env_missing"
            if configured_api_key_env
            else "not_established"
        ),
        "provider_authentication_verified": authentication_verified,
        "provider_session_sha256": hashlib.sha256(session_bytes).hexdigest(),
        "provider_session_digest_scope": "exact_ctx_run_session_jsonl_bytes",
        "provider_session_path": str(session_path),
        "configured_ctx_tool_names": sorted(configured_ctx_tool_names),
        "configured_ctx_tool_surface_verified": expected_ctx_tool_names is not None,
        "provider_tool_surface_evidence": "ctx_run_session_start_pre_request_config",
    }


def validate_production_payload(
    payload: object,
    *,
    session_id: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("ctx run JSON output is not an object")
    if payload.get("session_id") != session_id:
        raise ValueError(
            "ctx run JSON session_id does not match the requested session: "
            f"expected={session_id!r}, actual={payload.get('session_id')!r}"
        )
    stop_reason = payload.get("stop_reason")
    if stop_reason not in SUCCESSFUL_CTX_RUN_STOP_REASONS:
        raise ValueError(f"ctx run stop_reason is not successful: {stop_reason!r}")
    return payload


def extract_production_usage(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        raise ValueError("ctx run returned no usage object")
    usage = payload["usage"]
    if usage.get("tokens_reported") is not True:
        raise ValueError("ctx run provider did not report exact token usage")
    required = ("input_tokens", "output_tokens", "total_tokens")
    if not all(
        isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool) and usage[key] >= 0
        for key in required
    ):
        raise ValueError("ctx run token usage is incomplete")
    input_tokens = int(usage["input_tokens"])
    output_tokens = int(usage["output_tokens"])
    total_tokens = int(usage["total_tokens"])
    if total_tokens != input_tokens + output_tokens:
        raise ValueError("ctx run total token usage is inconsistent")
    cached = usage.get("cached_input_tokens")
    if cached is not None and (
        not isinstance(cached, int)
        or isinstance(cached, bool)
        or cached < 0
        or cached > input_tokens
    ):
        raise ValueError("ctx run cached input token usage is invalid")
    return {
        "attribution": "exact",
        "attribution_source": "ctx run JSON provider usage",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": input_tokens - cached if isinstance(cached, int) else None,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def extract_production_patch(payload: object) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("final_message"), str):
        raise ValueError("ctx run returned no final_message")
    try:
        message = json.loads(payload["final_message"])
    except json.JSONDecodeError as exc:
        raise ValueError("ctx run final_message is not JSON") from exc
    patch = message.get("patch") if isinstance(message, dict) else None
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("ctx run final_message contains no patch")
    if len(patch.encode("utf-8")) > 1_000_000:
        raise ValueError("ctx run patch exceeds 1000000 bytes")
    return patch


def apply_production_patch(scenario: Scenario, workspace: Path, patch: str) -> list[str]:
    numstat = run_process(
        ["git", "apply", "--numstat", "-z", "--no-unsafe-paths", "-"],
        cwd=workspace,
        input_text=patch,
        timeout=30,
    )
    if numstat.returncode:
        raise ValueError(f"ctx run patch is invalid: {numstat.stderr.strip()}")
    paths: list[str] = []
    for row in numstat.stdout.split("\0"):
        if not row:
            continue
        fields = row.split("\t", 2)
        if len(fields) != 3 or not fields[2]:
            raise ValueError("ctx run patch has malformed path metadata")
        paths.append(fields[2])
    if not paths:
        raise ValueError("ctx run patch changes no files")
    disallowed = set(paths) - set(scenario.allowed_changes)
    if disallowed:
        raise ValueError(f"ctx run patch changes disallowed paths: {sorted(disallowed)}")
    checked = run_process(
        ["git", "apply", "--check", "--no-unsafe-paths", "-"],
        cwd=workspace,
        input_text=patch,
        timeout=30,
    )
    if checked.returncode:
        raise ValueError(f"ctx run patch does not apply: {checked.stderr.strip()}")
    applied = run_process(
        ["git", "apply", "--no-unsafe-paths", "-"],
        cwd=workspace,
        input_text=patch,
        timeout=30,
    )
    if applied.returncode:
        raise ValueError(f"ctx run patch application failed: {applied.stderr.strip()}")
    return paths


def validate_production_lifecycle(
    scenario: Scenario,
    *,
    lifecycle_root: Path,
    session_id: str,
    expect_selected_cycle: bool = True,
) -> dict[str, Any]:
    path = lifecycle_root / "events.jsonl"
    if not path.is_file():
        if expect_selected_cycle:
            raise ValueError("ctx run produced no lifecycle ledger")
        return {
            "selected_id": None,
            "actions": [],
            "session_actions": [],
            "session_status": None,
            "session_event_count": 0,
            "lifecycle_emitted": False,
            "lifecycle_sha256": None,
            "final_loaded": [],
        }
    events: list[dict[str, Any]] = []
    lifecycle_bytes = path.read_bytes()
    for line_number, line in enumerate(lifecycle_bytes.decode("utf-8").splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("ctx run lifecycle ledger contains invalid JSON") from exc
        if not isinstance(event, dict):
            raise ValueError(f"ctx run lifecycle ledger line {line_number} is not an object")
        if event.get("session_id") != session_id:
            raise ValueError(
                "ctx run lifecycle ledger contains a foreign session: "
                f"expected={session_id!r}, actual={event.get('session_id')!r}"
            )
        events.append(event)
    if not events:
        if expect_selected_cycle:
            raise ValueError("ctx run produced no lifecycle events for the requested session")
        return {
            "selected_id": None,
            "actions": [],
            "session_actions": [],
            "session_status": None,
            "session_event_count": 0,
            "lifecycle_emitted": False,
            "lifecycle_sha256": hashlib.sha256(lifecycle_bytes).hexdigest(),
            "final_loaded": [],
        }

    session_actions = [str(event.get("action") or "") for event in events]
    session_start_indices = [
        index for index, action in enumerate(session_actions) if action == "session_start"
    ]
    if session_start_indices and session_start_indices != [0]:
        raise ValueError("ctx run lifecycle session_start must be unique and first when emitted")
    session_end_indices = [
        index for index, action in enumerate(session_actions) if action == "session_end"
    ]
    if len(session_end_indices) != 1:
        raise ValueError("ctx run lifecycle must contain exactly one session_end")
    session_end_index = session_end_indices[0]
    if session_end_index != len(events) - 1:
        raise ValueError("ctx run lifecycle contains events after session_end")
    session_status = str(events[session_end_index].get("status") or "").lower()
    if session_status not in SUCCESSFUL_LIFECYCLE_STATUSES:
        raise ValueError(
            f"ctx run lifecycle session_end status is not successful: {session_status!r}"
        )

    skill = next(item for item in scenario.context if item["type"] == "skill")
    slug = str(skill["slug"])
    unexpected_transitions = [
        event
        for event in events
        if event.get("action") in ENTITY_TRANSITION_ACTIONS
        and (event.get("entity_type"), event.get("slug")) != ("skill", slug)
    ]
    if unexpected_transitions:
        unexpected = unexpected_transitions[0]
        raise ValueError(
            "ctx run lifecycle contains an unexpected entity transition: "
            f"action={unexpected.get('action')!r}, "
            f"entity_type={unexpected.get('entity_type')!r}, "
            f"slug={unexpected.get('slug')!r}"
        )
    actions = [
        str(event.get("action"))
        for event in events
        if event.get("entity_type") == "skill" and event.get("slug") == slug
    ]

    if expect_selected_cycle:
        state = "await_load_request"
        for index, action in enumerate(actions):
            if state == "await_load_request" and action == "load_requested":
                state = "await_load_apply"
            elif state == "await_load_apply" and action == "load_applied":
                state = "await_use"
            elif state == "await_use" and action == "used":
                state = "await_unload"
            elif state == "await_unload" and action == "unload_requested":
                state = "await_unload_apply"
            elif state in {"await_unload", "await_unload_apply"} and action == "unload_applied":
                state = "complete"
            else:
                raise ValueError(
                    f"ctx run lifecycle has invalid transition for skill:{slug}: "
                    f"state={state}, action={action}, index={index}, actions={actions}"
                )
        if state != "complete":
            raise ValueError(f"ctx run lifecycle is incomplete for skill:{slug}: actions={actions}")
    elif actions:
        raise ValueError("baseline ctx run emitted a selected skill lifecycle cycle")

    state = make_lifecycle_store(lifecycle_root).session_state(session_id=session_id)
    if state.get("loaded") != []:
        raise ValueError("ctx run lifecycle ended with loaded context")
    return {
        "selected_id": f"skill:{slug}" if expect_selected_cycle else None,
        "actions": actions,
        "session_actions": session_actions,
        "session_status": session_status,
        "session_event_count": len(events),
        "lifecycle_emitted": True,
        "lifecycle_sha256": hashlib.sha256(lifecycle_bytes).hexdigest(),
        "final_loaded": state["loaded"],
    }


def _verification_env(workspace: Path, temp: Path) -> dict[str, str]:
    return {
        "CODEX_HOME": ORIGINAL_CODEX_HOME,
        "HOME": str(temp),
        "TMPDIR": str(temp),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "PYTHONPATH": str(workspace / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def _run_verified(argv: list[str], *, workspace: Path, timeout: float = 180) -> CommandResult:
    codex = shutil.which("codex")
    if sys.platform != "darwin" or codex is None:
        raise RuntimeError("live verification requires the Codex-managed macOS sandbox")
    temp = workspace.parent / "verification-tmp"
    temp.mkdir(parents=True, exist_ok=True)
    return run_process(
        [
            codex,
            "sandbox",
            "-P",
            ":workspace",
            "--sandbox-state-disable-network",
            "-C",
            str(workspace),
            "--",
            *argv,
        ],
        cwd=workspace,
        env=_verification_env(workspace, temp),
        timeout=timeout,
        resource_limits=True,
        contain_descendants=True,
    )


def _pytest_pass_count(output: str) -> int | None:
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:[\s,]|$)", output)
    return int(matches[-1]) if matches else None


def _focused_verification(scenario: Scenario, workspace: Path, test_hash: str) -> CommandResult:
    test_path = workspace / scenario.test_path
    if not test_path.is_file() or hashlib.sha256(test_path.read_bytes()).hexdigest() != test_hash:
        return CommandResult(1, "", "benchmark-owned test was changed", 0.0)
    argv = [part.replace("{python}", sys.executable) for part in scenario.verify]
    focused = _run_verified(argv, workspace=workspace)
    if focused.returncode:
        return focused
    count = _pytest_pass_count(focused.stdout + focused.stderr)
    if count != scenario.expected_test_count:
        return CommandResult(
            1,
            focused.stdout,
            focused.stderr
            + f"\nexpected {scenario.expected_test_count} focused tests, observed {count}",
            focused.elapsed,
        )
    return focused


def _verify_pinned_head(scenario: Scenario, workspace: Path) -> CommandResult:
    started = time.perf_counter()
    current = run_process(["git", "rev-parse", "HEAD"], cwd=workspace, timeout=30)
    observed = current.stdout.strip()
    if current.returncode or observed != scenario.commit:
        return CommandResult(
            1,
            current.stdout,
            current.stderr
            + f"\nagent changed pinned HEAD: expected {scenario.commit}, observed {observed or 'unknown'}",
            time.perf_counter() - started,
        )
    return CommandResult(0, current.stdout, current.stderr, time.perf_counter() - started)


def _materialize_untracked_changes(scenario: Scenario, workspace: Path) -> CommandResult:
    started = time.perf_counter()
    indexed_test = run_process(
        ["git", "ls-files", "--error-unmatch", scenario.test_path], cwd=workspace, timeout=30
    )
    if not indexed_test.returncode:
        return CommandResult(
            1,
            "",
            "benchmark-owned test entered the git index",
            time.perf_counter() - started,
        )
    untracked = run_process(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=workspace,
        timeout=30,
    )
    if untracked.returncode:
        return CommandResult(
            untracked.returncode,
            untracked.stdout,
            untracked.stderr,
            time.perf_counter() - started,
        )
    paths = [path for path in untracked.stdout.split("\0") if path and path != scenario.test_path]
    if paths:
        added = run_process(["git", "add", "-N", "--", *paths], cwd=workspace, timeout=30)
        if added.returncode:
            return CommandResult(
                added.returncode,
                added.stdout,
                added.stderr,
                time.perf_counter() - started,
            )
    changed = run_process(
        ["git", "diff", "--name-only", "-z", scenario.commit],
        cwd=workspace,
        timeout=30,
    )
    if changed.returncode:
        return CommandResult(
            changed.returncode,
            changed.stdout,
            changed.stderr,
            time.perf_counter() - started,
        )
    changed_paths = {path for path in changed.stdout.split("\0") if path}
    disallowed = changed_paths - set(scenario.allowed_changes)
    if disallowed:
        return CommandResult(
            1,
            changed.stdout,
            f"changes outside scenario allowlist: {sorted(disallowed)}",
            time.perf_counter() - started,
        )
    return CommandResult(0, changed.stdout, "", time.perf_counter() - started)


def verify_workspace(scenario: Scenario, workspace: Path, test_hash: str) -> CommandResult:
    focused = _focused_verification(scenario, workspace, test_hash)
    if focused.returncode:
        return focused
    stdout = focused.stdout
    stderr = focused.stderr
    elapsed = focused.elapsed
    for command in scenario.regression_verify:
        argv = [part.replace("{python}", sys.executable) for part in command]
        regression = _run_verified(argv, workspace=workspace)
        stdout += regression.stdout
        stderr += regression.stderr
        elapsed += regression.elapsed
        if regression.returncode:
            return CommandResult(regression.returncode, stdout, stderr, elapsed)
    materialized = _materialize_untracked_changes(scenario, workspace)
    if materialized.returncode:
        return CommandResult(
            1,
            stdout + materialized.stdout,
            stderr + materialized.stderr,
            elapsed + materialized.elapsed,
        )
    elapsed += materialized.elapsed
    diff_check = run_process(
        ["git", "diff", "--check", scenario.commit],
        cwd=workspace,
        timeout=30,
    )
    return CommandResult(
        diff_check.returncode,
        stdout + diff_check.stdout,
        stderr + diff_check.stderr,
        elapsed + diff_check.elapsed,
    )


def validate_evaluator_controls(
    scenario: Scenario,
    *,
    cache: Path,
    output: Path,
) -> dict[str, Any]:
    controls = output / scenario.id / "controls"
    red_root = controls / "red"
    reference_root = controls / "reference"
    red_workspace = red_root / "repo"
    reference_workspace = reference_root / "repo"
    try:
        red_workspace.parent.mkdir(parents=True, exist_ok=True)
        red_hash = prepare_workspace(scenario, cache, red_workspace)
        red = _focused_verification(scenario, red_workspace, red_hash)
        (controls / "red.log").write_text(
            f"returncode={red.returncode}\n{red.stdout}{red.stderr}", encoding="utf-8"
        )
        if red.returncode in {70, 71} and "sandbox" in red.stderr.lower():
            raise RuntimeError("verification sandbox could not be applied")
        if not red.returncode or scenario.red_failure_contains not in red.stdout + red.stderr:
            raise RuntimeError(
                "evaluator red control did not fail for the expected missing feature: "
                f"{scenario.red_failure_contains!r}"
            )

        reference_workspace.parent.mkdir(parents=True, exist_ok=True)
        reference_hash = prepare_workspace(scenario, cache, reference_workspace)
        applied = run_process(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=reference_workspace,
            input_text=scenario.reference_patch,
            timeout=30,
        )
        if applied.returncode:
            raise RuntimeError(f"reference patch failed: {applied.stderr.strip()}")
        reference = verify_workspace(scenario, reference_workspace, reference_hash)
        (controls / "reference.log").write_text(
            reference.stdout + reference.stderr, encoding="utf-8"
        )
        if reference.returncode:
            raise RuntimeError(f"evaluator reference control failed: {reference.stderr.strip()}")
        result = {
            "status": "passed",
            "red_failure_observed": scenario.red_failure_contains,
            "red_seconds": round(red.elapsed, 6),
            "reference_seconds": round(reference.elapsed, 6),
            "expected_focused_tests": scenario.expected_test_count,
            "regression_commands": [list(command) for command in scenario.regression_verify],
            "reference_patch_sha256": hashlib.sha256(scenario.reference_patch.encode()).hexdigest(),
        }
        (controls / "control.json").write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        return result
    finally:
        for private_root in (red_root, reference_root):
            if private_root.exists():
                shutil.rmtree(private_root)
            if private_root.exists():
                raise RuntimeError(f"evaluator control cleanup failed: {private_root.name}")


def _command_version(argv: list[str]) -> str:
    result = run_process(argv, cwd=ROOT, timeout=30)
    return (result.stdout or result.stderr).strip() if not result.returncode else "unavailable"


def collect_repository_state() -> dict[str, Any]:
    head = run_process(["git", "rev-parse", "HEAD"], cwd=ROOT, timeout=30)
    status = run_process(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT, timeout=30
    )
    tracked_diff = run_process(["git", "diff", "--binary", "HEAD"], cwd=ROOT, timeout=30)
    return {
        "head": head.stdout.strip() if not head.returncode else "unavailable",
        "clean": not status.returncode and not status.stdout.strip(),
        "status": status.stdout.splitlines(),
        "tracked_diff_sha256": (
            hashlib.sha256(tracked_diff.stdout.encode()).hexdigest()
            if not tracked_diff.returncode
            else "unavailable"
        ),
    }


def require_clean_production_repository(repository_state: Mapping[str, Any], *, live: bool) -> None:
    if live and repository_state.get("clean") is not True:
        status = repository_state.get("status")
        changes = ", ".join(str(item) for item in status) if isinstance(status, list) else "unknown"
        raise ValueError(
            f"live {PRODUCTION_CATALOG_ENGINE} requires a clean committed harness; "
            f"commit or restore the listed changes before running: {changes}"
        )


def write_final_repository_attestation(
    output: Path,
    initial_state: Mapping[str, Any],
    initial_manifest: Mapping[str, Any],
) -> tuple[bool, bool, dict[str, Any]]:
    final_state = collect_repository_state()
    state_matches = final_state == dict(initial_state)
    manifest_path = output / "environment.json"
    initial_bytes = (json.dumps(dict(initial_manifest), indent=2) + "\n").encode("utf-8")
    manifest_matches = manifest_path.read_bytes() == initial_bytes
    manifest = dict(initial_manifest)
    manifest["repository_state_end"] = final_state
    manifest["repository_state_matches_start_at_end"] = state_matches
    manifest["environment_manifest_matches_start_at_end"] = manifest_matches
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return state_matches, manifest_matches, final_state


def write_environment_manifest(
    *,
    output: Path,
    scenarios_path: Path,
    scenarios: list[Scenario],
    codex: str,
    model: str,
    run_config: dict[str, Any],
    schedule: list[dict[str, Any]],
    repository_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    revision = run_process(["git", "rev-parse", "HEAD"], cwd=ROOT, timeout=30)
    dependencies = run_process(
        [sys.executable, "-m", "pip", "freeze", "--all"], cwd=ROOT, timeout=60
    )
    scenario_bytes = scenarios_path.read_bytes()
    engine = str(run_config.get("engine") or "codex-controlled")
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "python_executable": sys.executable,
        "execution_engine": engine,
        "codex_binary": codex,
        "codex_version": _command_version([codex, "--version"]),
        "model": model,
        "ctx_revision": revision.stdout.strip() if not revision.returncode else "unavailable",
        "repository_state": repository_state or collect_repository_state(),
        "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scenarios_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        "scenario_ids": [scenario.id for scenario in scenarios],
        "scenario_classes": {scenario.id: scenario.benchmark_class for scenario in scenarios},
        "run_config": run_config,
        "schedule": schedule,
        "dependency_freeze": dependencies.stdout.splitlines()
        if not dependencies.returncode
        else [],
        "verification": {
            "sandbox": "Codex-managed macOS :workspace profile",
            "network": "denied",
            "environment": "allowlist",
            "resource_limits": ["cpu", "file-size", "open-files"],
        },
        "codex_environment_keys": sorted(
            _ctx_env(output / "manifest-home", output / "manifest-lifecycle")
        ),
        "token_scope": (
            "ctx run session provider usage; per-context attribution unavailable"
            if engine == "production-ctx-run"
            else "terminal Codex turn; per-subagent and per-context attribution unavailable"
        ),
        "evidence_trust_boundary": (
            EVIDENCE_TRUST_BOUNDARY if engine == "production-ctx-run" else None
        ),
        "cryptographic_independence": False if engine == "production-ctx-run" else None,
    }
    (output / "environment.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def run_production_trial(
    scenario: Scenario,
    *,
    arm: str,
    attempt: int,
    trial: int,
    retry: int,
    cache: Path,
    output: Path,
    model: str,
    timeout: float,
    dry_run: bool,
    incidents: IncidentLog,
    api_key_env: str | None,
    base_url: str | None,
    max_iterations: int,
    max_tokens: int | None,
    provider_timeout: float,
) -> dict[str, Any]:
    if arm not in {"baseline", "ctx-light"}:
        raise ValueError(f"production ctx run does not support arm: {arm}")
    trial_started = time.perf_counter()
    evidence_classification = classify_production_evidence(
        base_url=base_url,
        dry_run=dry_run,
    )
    run_dir = output / scenario.id / arm / f"attempt-{attempt}"
    workspace = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)
    test_hash = prepare_workspace(scenario, cache, workspace)
    home = run_dir / "home"
    lifecycle_root = run_dir / "lifecycle"
    write_ctx_fixture(scenario, home)
    env = _ctx_env(home, lifecycle_root)
    if api_key_env and api_key_env in os.environ:
        env[api_key_env] = os.environ[api_key_env]
    prompt = production_task_prompt(scenario, workspace)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    session_id = f"ctx-ab-{scenario.id}-{arm}-{attempt}"
    sessions_dir = run_dir / "sessions"
    with_ctx = arm == "ctx-light"
    expected_ctx_tool_names = PRODUCTION_CTX_TOOL_NAMES if with_ctx else ()
    command = production_ctx_command(
        model=model,
        prompt=prompt,
        session_id=session_id,
        sessions_dir=sessions_dir,
        with_ctx=with_ctx,
        api_key_env=api_key_env,
        base_url=base_url,
        max_iterations=max_iterations,
        max_tokens=max_tokens,
        provider_timeout=provider_timeout,
    )
    recorded_command = list(command)
    recorded_command[recorded_command.index("--task") + 1] = f"<sha256:{prompt_hash}>"
    (run_dir / "prompt.sha256").write_text(prompt_hash + "\n", encoding="utf-8")
    (run_dir / "command.json").write_text(
        json.dumps({"argv": recorded_command}, indent=2) + "\n",
        encoding="utf-8",
    )
    if dry_run:
        return {
            "scenario": scenario.id,
            "arm": arm,
            "trial": trial,
            "retry": retry,
            "attempt": attempt,
            "engine": "production-ctx-run",
            "treatment_level": arm,
            "status": "wiring_only",
            **evidence_classification,
            "verification_passed": None,
            "task_prompt_sha256": prompt_hash,
            "delivered_prompt_sha256": prompt_hash,
            "recommended_ids": [],
            "selected_ids": [],
            "used_ids": [],
            "ctx_setup_seconds": 0.0,
            "teardown_seconds": 0.0,
            "total_seconds": round(time.perf_counter() - trial_started, 6),
            "token_attribution": "unavailable",
            "ctx_run_payload_sha256": None,
            "lifecycle_sha256": None,
            "expected_ctx_tool_names": list(expected_ctx_tool_names),
            "configured_ctx_tool_names": None,
            "provider_tool_surface_evidence": "wiring_only",
            "evidence_trust_boundary": EVIDENCE_TRUST_BOUNDARY,
            "cryptographic_independence": False,
            "artifact_dir": str(run_dir),
        }

    measured_started = time.perf_counter()
    agent = run_process(
        command,
        cwd=workspace,
        env=env,
        timeout=timeout,
        contain_descendants=True,
    )
    (run_dir / "ctx-run.json").write_text(agent.stdout, encoding="utf-8")
    (run_dir / "ctx-run.stderr.log").write_text(agent.stderr, encoding="utf-8")
    payload_sha256 = hashlib.sha256(agent.stdout.encode("utf-8")).hexdigest()
    payload: dict[str, Any] | None = None
    provider_provenance: dict[str, Any] = {
        "provider_identity": None,
        "provider_identity_source": None,
        "provider_identity_verified": False,
        "provider_adapter": None,
        "provider_response_models": [],
        "provider_response_finish_reasons": [],
        "provider_response_count": 0,
        "provider_response_success": False,
        "provider_endpoint_evidence": "not_established",
        "provider_endpoint_verified": False,
        "provider_auth_mode": "not_established",
        "provider_authentication_evidence": "not_established",
        "provider_authentication_verified": False,
        "provider_session_sha256": None,
        "provider_session_digest_scope": None,
        "provider_session_path": None,
        "configured_ctx_tool_names": None,
        "configured_ctx_tool_surface_verified": False,
        "provider_tool_surface_evidence": "not_established",
    }
    usage: dict[str, Any] = {
        "attribution": "unavailable",
        "reason": "ctx run provider usage was not validated",
    }
    patch_paths: list[str] = []
    production_errors: list[str] = []
    try:
        decoded = json.loads(agent.stdout)
        payload = validate_production_payload(decoded, session_id=session_id)
        usage = extract_production_usage(payload)
        patch = extract_production_patch(payload)
        (run_dir / "model.patch").write_text(patch, encoding="utf-8")
        if agent.returncode:
            raise ValueError(f"ctx run exited with status {agent.returncode}")
        patch_paths = apply_production_patch(scenario, workspace, patch)
    except (json.JSONDecodeError, ValueError) as exc:
        production_errors.append(str(exc))
    try:
        provider_provenance = extract_provider_response_provenance(
            sessions_dir=sessions_dir,
            session_id=session_id,
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            env=env,
            expected_ctx_tool_names=expected_ctx_tool_names,
        )
    except ValueError as exc:
        production_errors.append(str(exc))

    verification = verify_workspace(scenario, workspace, test_hash)
    (run_dir / "verification.log").write_text(
        verification.stdout + verification.stderr,
        encoding="utf-8",
    )
    lifecycle_evidence: dict[str, Any] | None = None
    lifecycle_path = lifecycle_root / "events.jsonl"
    lifecycle_sha256 = (
        hashlib.sha256(lifecycle_path.read_bytes()).hexdigest()
        if lifecycle_path.is_file()
        else None
    )
    try:
        lifecycle_evidence = validate_production_lifecycle(
            scenario,
            lifecycle_root=lifecycle_root,
            session_id=session_id,
            expect_selected_cycle=with_ctx,
        )
    except ValueError as exc:
        production_errors.append(str(exc))
    lifecycle_valid = lifecycle_evidence is not None
    usage_valid = usage.get("attribution") == "exact"
    passed = bool(
        not agent.returncode
        and patch_paths
        and not verification.returncode
        and usage_valid
        and lifecycle_valid
        and provider_provenance["provider_response_success"] is True
        and not production_errors
    )
    evidence_classification = classify_production_evidence(
        base_url=base_url,
        dry_run=False,
        provider_provenance=provider_provenance,
    )
    if not passed:
        if agent.timed_out:
            production_errors.append("ctx run timed out")
        elif agent.returncode and not any("exited with status" in row for row in production_errors):
            production_errors.append(f"ctx run exited with status {agent.returncode}")
        if verification.returncode:
            production_errors.append(f"verification exited with status {verification.returncode}")
        incidents.add(
            scenario=scenario.id,
            arm=arm,
            attempt=attempt,
            stage="production-ctx-run",
            message="production benchmark arm failed",
            evidence="; ".join(dict.fromkeys(production_errors)),
        )
    selected_id = lifecycle_evidence.get("selected_id") if lifecycle_evidence else None
    selected_ids = [selected_id] if isinstance(selected_id, str) else []
    measured_seconds = time.perf_counter() - measured_started
    return {
        "scenario": scenario.id,
        "arm": arm,
        "trial": trial,
        "retry": retry,
        "attempt": attempt,
        "engine": "production-ctx-run",
        **evidence_classification,
        "benchmark_class": scenario.benchmark_class,
        "treatment_level": arm,
        "escalated": False,
        "first_attempt": retry == 0,
        "status": "passed" if passed else "failed",
        "verification_passed": not verification.returncode,
        "verification_returncode": verification.returncode,
        "agent_returncode": agent.returncode,
        "agent_timed_out": agent.timed_out,
        "task_prompt_sha256": prompt_hash,
        "delivered_prompt_sha256": prompt_hash,
        "recommended_ids": selected_ids,
        "selected_ids": selected_ids,
        "used_ids": selected_ids,
        "policy_valid": lifecycle_valid,
        "production_errors": production_errors,
        "patch_paths": patch_paths,
        "ctx_setup_seconds": 0.0,
        "agent_seconds": round(agent.elapsed, 6),
        "verification_seconds": round(verification.elapsed, 6),
        "teardown_seconds": 0.0,
        "measured_phase_seconds": round(measured_seconds, 6),
        "total_seconds": round(measured_seconds, 6),
        "harness_total_seconds": round(time.perf_counter() - trial_started, 6),
        "token_attribution": usage.pop("attribution"),
        "token_scope": "ctx_run_session",
        "team_token_completeness": "not_applicable",
        "lifecycle_valid": lifecycle_valid,
        "lifecycle_actions": lifecycle_evidence["actions"] if lifecycle_evidence else [],
        "lifecycle_session_actions": (
            lifecycle_evidence["session_actions"] if lifecycle_evidence else []
        ),
        "lifecycle_session_status": (
            lifecycle_evidence["session_status"] if lifecycle_evidence else None
        ),
        "final_loaded": lifecycle_evidence["final_loaded"] if lifecycle_evidence else [],
        "ctx_run_payload_sha256": payload_sha256,
        "ctx_run_payload_digest_scope": "exact_ctx_run_stdout_bytes",
        "lifecycle_sha256": lifecycle_sha256,
        "lifecycle_digest_scope": (
            "entire_isolated_events_jsonl_bytes" if lifecycle_sha256 else None
        ),
        "evidence_trust_boundary": EVIDENCE_TRUST_BOUNDARY,
        "cryptographic_independence": False,
        "expected_ctx_tool_names": list(expected_ctx_tool_names),
        "reaped_descendants": agent.reaped_descendants,
        "residual_descendants": list(agent.residual_descendants),
        **usage,
        "artifact_dir": str(run_dir),
        "lifecycle_events": str(lifecycle_path) if lifecycle_path.is_file() else None,
        "ctx_run_session_id": payload.get("session_id") if payload else None,
        "ctx_run_stop_reason": payload.get("stop_reason") if payload else None,
        **provider_provenance,
    }


def run_trial(
    scenario: Scenario,
    *,
    arm: str,
    treatment_level: str,
    attempt: int,
    trial: int,
    retry: int,
    cache: Path,
    output: Path,
    codex: str,
    model: str,
    timeout: float,
    dry_run: bool,
    incidents: IncidentLog,
    catalog_snapshot: CatalogSnapshot | None = None,
    forbidden_agent_reads: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    trial_started = time.perf_counter()
    production_catalog = catalog_snapshot is not None
    engine_name = PRODUCTION_CATALOG_ENGINE if production_catalog else "codex-controlled"
    ctx_enabled = treatment_level != "baseline"
    full_treatment = treatment_level == "ctx-full"
    if treatment_level not in TREATMENT_ARMS:
        raise ValueError(f"unsupported treatment level: {treatment_level}")
    if production_catalog and full_treatment:
        raise ValueError(f"{PRODUCTION_CATALOG_ENGINE} does not support ctx-full")
    controlled_context_types = {
        str(item.get("type")) for item in scenario.context if isinstance(item, dict)
    }
    if not production_catalog and not {"skill", "agent"}.issubset(controlled_context_types):
        raise ValueError("controlled scenarios require skill and agent context")
    evidence_classification = (
        classify_codex_production_catalog_evidence(dry_run=dry_run)
        if production_catalog
        else classify_codex_controlled_evidence(dry_run=dry_run)
    )
    run_dir = output / scenario.id / arm / f"attempt-{attempt}"
    workspace = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_setup_started = time.perf_counter()
    test_hash = (
        prepare_workspace(
            scenario,
            cache,
            workspace,
            include_evaluator_test=False,
        )
        if production_catalog
        else prepare_workspace(scenario, cache, workspace)
    )
    workspace_setup_seconds = time.perf_counter() - workspace_setup_started
    home = run_dir / "home"
    agent_home = run_dir / "codex-home"
    lifecycle_root = run_dir / "lifecycle"
    env = _ctx_env(home, lifecycle_root)
    agent_env = env
    isolation_evidence: dict[str, Any] | None = None
    task_only_prompt = (
        production_catalog_task_prompt(scenario) if production_catalog else task_prompt(scenario)
    )
    base_prompt = task_only_prompt
    recommendations: list[dict[str, Any]] = []
    recommended_ids: list[str] = []
    selected_ids: list[str] = []
    selected_items: list[dict[str, Any]] = []
    catalog: dict[str, Any] | None = None
    ctx_setup_seconds = 0.0
    recommendation_seconds = 0.0
    body_fetch_seconds = 0.0
    load_seconds = 0.0
    lifecycle_timings = {
        "use_seconds": 0.0,
        "unload_seconds": 0.0,
        "session_end_seconds": 0.0,
    }
    store = make_lifecycle_store(lifecycle_root) if ctx_enabled else None
    session_id = f"ctx-ab-{scenario.id}-{attempt}"
    usage_evidence: dict[str, str] = {}
    session_status = "failed"
    session_closed = False
    teardown_seconds = 0.0

    def close_session() -> None:
        nonlocal lifecycle_timings, session_closed, teardown_seconds
        if not ctx_enabled or store is None or session_closed:
            return
        session_closed = True
        teardown_started = time.perf_counter()
        lifecycle_timings = close_context_session(
            store,
            selected_items,
            session_id=session_id,
            status=session_status,
            model=model,
            usage_evidence=usage_evidence,
            mark_applied=production_catalog,
        )
        teardown_seconds = time.perf_counter() - teardown_started

    def catalog_result_fields() -> dict[str, Any]:
        if catalog_snapshot is None:
            return {}
        return {
            "catalog_archive_sha256": catalog_snapshot.provenance["archive_sha256"],
            "catalog_runtime_availability_sha256": catalog_snapshot.provenance[
                "runtime_availability_sha256"
            ],
            "catalog_graph_export_id": catalog_snapshot.provenance["graph_export_id"],
            "catalog_graph_export_manifest_sha256": catalog_snapshot.provenance[
                "graph_export_manifest_sha256"
            ],
            "catalog_overlay_sha256": catalog_snapshot.provenance["overlay_sha256"],
            "catalog_overlay_records": catalog_snapshot.provenance["overlay_records"],
            "catalog_snapshot_path": str(catalog_snapshot.wiki_dir),
            "catalog_binding": "isolated_home_read_only_config",
            "catalog_cache_hit": catalog_snapshot.cache_hit,
            "catalog_prepare_seconds": round(catalog_snapshot.prepare_seconds, 6),
            "evaluator_visibility": "materialized_after_agent",
            "repair_policy": "fail_closed_no_retries",
        }

    try:
        if ctx_enabled:
            assert store is not None
            setup_started = time.perf_counter()
            if production_catalog:
                assert catalog_snapshot is not None
                bind_catalog_snapshot(home, catalog_snapshot)
                catalog = recommend_production_catalog(
                    scenario,
                    home=home,
                    lifecycle_root=lifecycle_root,
                    session_id=session_id,
                    snapshot=catalog_snapshot,
                )
                recommendations = list(catalog["candidates"])
                recommended_ids = list(catalog["candidate_ids"])
                selected_ids = list(catalog["selected_ids"])
                selected_item = catalog.get("selected_item")
                selected_items = [dict(selected_item)] if isinstance(selected_item, dict) else []
                recommendation_seconds = float(catalog["recommendation_seconds"])
                body_fetch_seconds = float(catalog["body_fetch_seconds"])
                store.record_dev_event(
                    session_id=session_id,
                    event_type="catalog_recommendation",
                    host="codex-cli",
                    cwd=str(workspace),
                    payload={
                        "candidate_ids": recommended_ids,
                        "selected_ids": selected_ids,
                        "context_policy": catalog["context_policy"],
                        "archive_sha256": catalog_snapshot.provenance["archive_sha256"],
                        "graph_export_id": catalog_snapshot.provenance["graph_export_id"],
                    },
                )
                write_catalog_recommendation_evidence(
                    run_dir / "recommendations.json",
                    catalog,
                    used_ids=[],
                    snapshot=catalog_snapshot,
                )
            else:
                write_ctx_fixture(scenario, home)
                recommendations = recommend_context(
                    scenario,
                    home=home,
                    lifecycle_root=lifecycle_root,
                )
                configured = {f"{item['type']}:{item['slug']}": item for item in scenario.context}
                recommended_ids = [
                    str(row.get("id")) for row in recommendations if row.get("id") in configured
                ]
                if full_treatment:
                    selected_items = [dict(item) for item in scenario.context]
                else:
                    selected_skill_id = next(
                        entity_id
                        for entity_id in recommended_ids
                        if configured[entity_id]["type"] == "skill"
                    )
                    selected_items = [dict(configured[selected_skill_id])]
                selected_ids = [f"{item['type']}:{item['slug']}" for item in selected_items]
                (run_dir / "recommendations.json").write_text(
                    json.dumps(
                        {
                            "query": scenario.query,
                            "treatment_level": treatment_level,
                            "recommended_ids": recommended_ids,
                            "selected_ids": selected_ids,
                            "recommendations": recommendations,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            load_started = time.perf_counter()
            for item in selected_items:
                store.load_entity(
                    session_id=session_id,
                    entity_type=str(item["type"]),
                    slug=str(item["slug"]),
                    reason=f"selected by explicit {treatment_level} benchmark policy",
                    selected=True,
                    selection_source="system",
                    source_context={
                        "benchmark": scenario.id,
                        "arm": arm,
                        "treatment_level": treatment_level,
                    },
                )
                if production_catalog:
                    store.mark_entity_loaded(
                        session_id=session_id,
                        entity_type=str(item["type"]),
                        slug=str(item["slug"]),
                        reason="exact bounded body fetched through ctx__wiki_get",
                    )
            load_seconds = time.perf_counter() - load_started
            if full_treatment:
                preflight = preflight_ctx_mcp(
                    scenario,
                    home=home,
                    lifecycle_root=lifecycle_root,
                    session_id=session_id,
                )
                (run_dir / "mcp-preflight.json").write_text(
                    json.dumps(preflight, indent=2) + "\n", encoding="utf-8"
                )
            base_prompt += (
                production_catalog_context_prompt(catalog)
                if production_catalog and catalog is not None
                else context_prompt(scenario, treatment_level)
            )
            ctx_setup_seconds = time.perf_counter() - setup_started
        prompt_hash = hashlib.sha256(task_only_prompt.encode()).hexdigest()
        treatment_hash = hashlib.sha256(base_prompt.encode()).hexdigest()
        (run_dir / "prompt.txt").write_text(base_prompt, encoding="utf-8")
        if dry_run:
            session_status = "preflight"
            close_session()
            lifecycle = (
                catalog_lifecycle_evidence(
                    lifecycle_root,
                    session_id=session_id,
                    store=store,
                )
                if production_catalog and ctx_enabled and store is not None
                else None
            )
            return {
                "scenario": scenario.id,
                "repo_url": scenario.repo_url,
                "arm": arm,
                "trial": trial,
                "retry": retry,
                "attempt": attempt,
                "engine": engine_name,
                "treatment_level": treatment_level,
                "status": "wiring_only",
                **evidence_classification,
                "verification_passed": None,
                "task_prompt_sha256": prompt_hash,
                "delivered_prompt_sha256": treatment_hash,
                "prompt_bytes": len(base_prompt.encode("utf-8")),
                "recommended_ids": recommended_ids,
                "candidate_ids": recommended_ids if production_catalog else None,
                "selected_ids": selected_ids,
                "delivered_ids": [],
                "adopted_ids": [],
                "used_ids": [],
                "context_delivery_verified": False,
                "policy_abstention_applied": bool(
                    catalog is not None and catalog.get("policy_abstention")
                ),
                "policy_abstention_verified": False,
                "policy_abstention_reason": (
                    catalog.get("selection_skip_reason") if catalog is not None else None
                ),
                "evaluator_isolation_verified": False,
                "workspace_setup_seconds": round(workspace_setup_seconds, 6),
                "ctx_setup_seconds": round(ctx_setup_seconds, 6),
                "recommendation_seconds": round(recommendation_seconds, 6),
                "body_fetch_seconds": round(body_fetch_seconds, 6),
                "load_seconds": round(load_seconds, 6),
                "use_seconds": round(lifecycle_timings["use_seconds"], 6),
                "unload_seconds": round(lifecycle_timings["unload_seconds"], 6),
                "teardown_seconds": round(teardown_seconds, 6),
                "total_seconds": round(time.perf_counter() - trial_started, 6),
                "token_attribution": "unavailable",
                "lifecycle_actions": lifecycle["actions"] if lifecycle else [],
                "lifecycle_sha256": lifecycle["sha256"] if lifecycle else None,
                "final_loaded": lifecycle["final_loaded"] if lifecycle else [],
                **catalog_result_fields(),
                "artifact_dir": str(run_dir),
            }
        if production_catalog:
            sensitive_reads = dict(forbidden_agent_reads or {})
            sensitive_reads["delivered_prompt_artifact"] = run_dir / "prompt.txt"
            sibling_prompts = [
                path
                for path in sorted((output / scenario.id).glob("*/attempt-*/prompt.txt"))
                if path.parent != run_dir
            ]
            for index, sibling_prompt in enumerate(sibling_prompts, start=1):
                sensitive_reads[f"sibling_arm_{index}"] = sibling_prompt
            prepare_isolated_codex_home(
                agent_home,
                workspace=workspace,
                forbidden_reads=sensitive_reads,
            )
            agent_env = production_agent_env(
                env,
                home=agent_home,
                workspace=workspace,
            )
            isolation_evidence = verify_agent_sandbox_isolation(
                codex=codex,
                workspace=workspace,
                home=agent_home,
                env=agent_env,
                forbidden_reads=sensitive_reads,
                project_check=tuple(
                    part.replace("{python}", sys.executable)
                    for part in scenario.regression_verify[0]
                ),
            )
            (run_dir / "evaluator-isolation.json").write_text(
                json.dumps(isolation_evidence, indent=2) + "\n",
                encoding="utf-8",
            )
        command = codex_command(
            codex=codex,
            model=model,
            workspace=workspace,
            prompt=base_prompt,
            with_ctx=full_treatment,
            agent_home=agent_home if production_catalog else None,
            isolate_evaluator=production_catalog,
        )
        (run_dir / "command.json").write_text(
            json.dumps(
                {"argv_without_prompt": command[:-1], "prompt_sha256": treatment_hash}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        agent = run_process(
            command,
            cwd=workspace,
            env=agent_env,
            timeout=timeout,
            contain_descendants=True,
        )
        (run_dir / "codex.jsonl").write_text(agent.stdout, encoding="utf-8")
        (run_dir / "codex.stderr.log").write_text(agent.stderr, encoding="utf-8")
        pre_status = run_process(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            timeout=30,
        )
        (run_dir / "post-agent-status.txt").write_text(pre_status.stdout, encoding="utf-8")
        head_check = _verify_pinned_head(scenario, workspace)
        (run_dir / "post-agent-head.log").write_text(
            head_check.stdout + head_check.stderr,
            encoding="utf-8",
        )
        if production_catalog:
            materialize_evaluator_test(
                scenario,
                workspace,
                expected_hash=test_hash,
            )
        verification = verify_workspace(scenario, workspace, test_hash)
        if head_check.returncode:
            verification = CommandResult(
                1,
                head_check.stdout + verification.stdout,
                head_check.stderr + verification.stderr,
                head_check.elapsed + verification.elapsed,
            )
        (run_dir / "verification.log").write_text(
            verification.stdout + verification.stderr, encoding="utf-8"
        )
        status = run_process(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            timeout=30,
        )
        (run_dir / "git-status.txt").write_text(status.stdout, encoding="utf-8")
        diff = run_process(
            ["git", "diff", "--binary", scenario.commit],
            cwd=workspace,
            timeout=30,
        )
        (run_dir / "changes.patch").write_text(diff.stdout, encoding="utf-8")
        usage = extract_token_usage(agent.stdout)
        trace_efficiency = extract_trace_efficiency(agent.stdout)
        scenario_skill = next(
            (item for item in scenario.context if item["type"] == "skill"),
            None,
        )
        selected_skill = next(
            (item for item in selected_items if item.get("type") == "skill"),
            None,
        )
        reviewer = next(
            (item for item in scenario.context if item["type"] == "agent"),
            None,
        )
        if production_catalog:
            mcp_used = False
            agent_used = False
        else:
            if scenario_skill is None or reviewer is None:
                raise ValueError("controlled scenarios require skill and agent context")
            mcp_used = observed_mcp_tool_use(
                agent.stdout,
                slug=str(scenario_skill["slug"]),
                entity_type=str(scenario_skill["type"]),
                expected_body=str(scenario_skill["body"]),
            )
            agent_used = observed_agent_review(
                agent.stdout,
                reviewer_slug=str(reviewer["slug"]),
                expected_instructions=str(reviewer["body"]),
            )
        agent_attempted = observed_agent_attempt(agent.stdout)
        model_turn_observed = observed_model_turn(agent.stdout)
        body_provenance = catalog.get("body_provenance") if catalog is not None else None
        context_delivery_verified = bool(
            production_catalog
            and ctx_enabled
            and len(selected_ids) == 1
            and isinstance(body_provenance, dict)
            and isinstance(body_provenance.get("body_sha256"), str)
            and prompt_hash != treatment_hash
            and model_turn_observed
            and isolation_evidence
            and isolation_evidence.get("verified") is True
        )
        preliminary_policy_abstention_applied = bool(
            production_catalog
            and ctx_enabled
            and catalog is not None
            and verify_production_policy_abstention(
                catalog,
                language=scenario.language,
                task_prompt_sha256=prompt_hash,
                delivered_prompt_sha256=treatment_hash,
                model_turn_observed=model_turn_observed,
                evaluator_isolation_verified=bool(
                    isolation_evidence and isolation_evidence.get("verified") is True
                ),
                mcp_used=mcp_used,
                agent_attempted=agent_attempted,
            )
        )
        policy_abstention_reason = (
            str(catalog.get("selection_skip_reason") or "") if catalog is not None else ""
        )
        preliminary_policy_abstention_verified = bool(
            preliminary_policy_abstention_applied
            and policy_abstention_reason == "language_only_match"
        )
        delivered_ids = selected_ids if context_delivery_verified else []
        skill_used = bool(
            not production_catalog
            and selected_skill
            and (mcp_used if full_treatment else ctx_enabled and model_turn_observed)
        )
        used_ids: list[str] = []
        if ctx_enabled:
            if full_treatment and skill_used:
                assert scenario_skill is not None
                usage_evidence["skill"] = "selected skill body returned by runtime ctx MCP call"
                usage_evidence["mcp-server"] = (
                    "Codex JSONL recorded successful ctx-wiki ctx__wiki_get completion"
                )
                used_ids.extend(
                    [
                        f"skill:{scenario_skill['slug']}",
                        next(
                            entity_id
                            for entity_id in selected_ids
                            if entity_id.startswith("mcp-server:")
                        ),
                    ]
                )
            elif skill_used and not production_catalog:
                usage_evidence["skill"] = "selected skill body supplied in treatment prompt"
                assert selected_skill is not None
                used_ids.append(str(selected_skill.get("id") or f"skill:{selected_skill['slug']}"))
            if full_treatment and agent_used:
                usage_evidence["agent"] = (
                    "Codex JSONL recorded matching spawn, completed wait, and close events"
                )
                used_ids.append(
                    next(entity_id for entity_id in selected_ids if entity_id.startswith("agent:"))
                )
        policy_valid = (
            not mcp_used
            and not agent_attempted
            and (
                not ctx_enabled
                or context_delivery_verified
                or preliminary_policy_abstention_applied
            )
            if production_catalog
            else treatment_policy_valid(
                treatment_level,
                skill_used=skill_used,
                mcp_used=mcp_used,
                agent_attempted=agent_attempted,
                agent_used=agent_used,
            )
        )
        tool_failures = required_tool_failures(agent.stdout) if full_treatment else []
        for failure in tool_failures:
            incidents.add(
                scenario=scenario.id,
                arm=arm,
                attempt=attempt,
                stage="required-tool",
                message="required tool attempt failed before recovery",
                evidence=failure,
                severity="warning" if policy_valid else "error",
                status="resolved" if policy_valid else "open",
            )
        passed = not agent.returncode and not verification.returncode and policy_valid
        session_status = "passed" if passed else "failed"
        if not passed:
            reasons = []
            if agent.timed_out:
                reasons.append("Codex timed out")
            elif agent.returncode:
                reasons.append(f"agent={agent.returncode}")
            if verification.returncode:
                reasons.append(f"verification={verification.returncode}")
            if full_treatment and not mcp_used:
                reasons.append("successful runtime MCP call absent")
            if full_treatment and not agent_used:
                reasons.append("completed delegated reviewer loop absent")
            if treatment_level == "ctx-light" and (mcp_used or agent_attempted):
                reasons.append("ctx-light used an unselected expensive tool")
            if (
                ctx_enabled
                and selected_items
                and not (context_delivery_verified if production_catalog else skill_used)
            ):
                reasons.append(
                    "selected skill context delivery was not verified"
                    if production_catalog
                    else "selected skill use was not observed"
                )
            if (
                production_catalog
                and ctx_enabled
                and not context_delivery_verified
                and not preliminary_policy_abstention_applied
            ):
                reasons.append("context absent without verified policy abstention")
            incidents.add(
                scenario=scenario.id,
                arm=arm,
                attempt=attempt,
                stage=PRODUCTION_CATALOG_ENGINE if production_catalog else "live-trial",
                message="benchmark arm failed",
                evidence="; ".join(reasons),
            )
        if (
            production_catalog
            and catalog is not None
            and context_delivery_verified
            and store is not None
        ):
            assert isinstance(body_provenance, dict)
            store.record_dev_event(
                session_id=session_id,
                event_type="context_delivered",
                host="codex-cli",
                cwd=str(workspace),
                payload={
                    "delivered_ids": delivered_ids,
                    "prompt_sha256": treatment_hash,
                    "body_sha256": body_provenance["body_sha256"],
                },
            )
        close_session()
        lifecycle = (
            catalog_lifecycle_evidence(
                lifecycle_root,
                session_id=session_id,
                store=store,
            )
            if production_catalog and ctx_enabled and store is not None
            else None
        )
        evaluator_isolation_verified = bool(
            isolation_evidence and isolation_evidence.get("verified") is True
        )
        policy_abstention_applied = bool(
            preliminary_policy_abstention_applied
            and catalog is not None
            and lifecycle is not None
            and verify_production_policy_abstention(
                catalog,
                language=scenario.language,
                task_prompt_sha256=prompt_hash,
                delivered_prompt_sha256=treatment_hash,
                model_turn_observed=model_turn_observed,
                evaluator_isolation_verified=evaluator_isolation_verified,
                mcp_used=mcp_used,
                agent_attempted=agent_attempted,
                lifecycle_events=list(lifecycle["events"]),
            )
        )
        if preliminary_policy_abstention_applied and not policy_abstention_applied:
            raise RuntimeError("production catalog policy abstention lifecycle was not verified")
        policy_abstention_verified = bool(
            policy_abstention_applied and preliminary_policy_abstention_verified
        )
        if production_catalog and catalog is not None:
            assert catalog_snapshot is not None
            write_catalog_recommendation_evidence(
                run_dir / "recommendations.json",
                catalog,
                used_ids=used_ids,
                snapshot=catalog_snapshot,
            )
        production_efficiency_eligible = bool(
            evidence_classification["production_efficiency_eligible"]
            and evaluator_isolation_verified
            and model_turn_observed
            and (not ctx_enabled or context_delivery_verified or policy_abstention_verified)
        )
        evidence_level = str(evidence_classification["evidence_level"])
        if policy_abstention_verified:
            evidence_level = PRODUCTION_POLICY_ABSTENTION_LEVEL
        elif production_catalog and ctx_enabled and not context_delivery_verified:
            evidence_level = "production_catalog_ctx_noop"
        elif production_catalog and not production_efficiency_eligible:
            evidence_level = "production_catalog_unverified"
        measured_phase_seconds = (
            ctx_setup_seconds + agent.elapsed + verification.elapsed + teardown_seconds
        )
        return {
            "scenario": scenario.id,
            "repo_url": scenario.repo_url,
            "arm": arm,
            "trial": trial,
            "retry": retry,
            "attempt": attempt,
            "engine": engine_name,
            **evidence_classification,
            "evidence_level": evidence_level,
            "production_efficiency_eligible": production_efficiency_eligible,
            "benchmark_class": scenario.benchmark_class,
            "treatment_level": treatment_level,
            "escalated": treatment_level != arm,
            "first_attempt": retry == 0,
            "status": "passed" if passed else "failed",
            "verification_passed": not verification.returncode,
            "verification_returncode": verification.returncode,
            "agent_returncode": agent.returncode,
            "agent_timed_out": agent.timed_out,
            "task_prompt_sha256": prompt_hash,
            "delivered_prompt_sha256": treatment_hash,
            "prompt_bytes": len(base_prompt.encode("utf-8")),
            "recommended_ids": recommended_ids,
            "candidate_ids": recommended_ids if production_catalog else None,
            "selected_ids": selected_ids,
            "delivered_ids": delivered_ids,
            "adopted_ids": used_ids,
            "used_ids": used_ids,
            "context_delivery_verified": (context_delivery_verified if ctx_enabled else None),
            "policy_abstention_applied": (policy_abstention_applied if ctx_enabled else None),
            "policy_abstention_verified": (policy_abstention_verified if ctx_enabled else None),
            "policy_abstention_reason": (policy_abstention_reason or None),
            "catalog_recommendation_lifecycle_verified": (
                policy_abstention_applied if ctx_enabled else None
            ),
            "evaluator_isolation_verified": evaluator_isolation_verified,
            "pinned_head_verified": not head_check.returncode,
            "policy_valid": policy_valid,
            "workspace_setup_seconds": round(workspace_setup_seconds, 6),
            "ctx_setup_seconds": round(ctx_setup_seconds, 6),
            "recommendation_seconds": round(recommendation_seconds, 6),
            "body_fetch_seconds": round(body_fetch_seconds, 6),
            "load_seconds": round(load_seconds, 6),
            "agent_seconds": round(agent.elapsed, 6),
            "verification_seconds": round(verification.elapsed, 6),
            "use_seconds": round(lifecycle_timings["use_seconds"], 6),
            "unload_seconds": round(lifecycle_timings["unload_seconds"], 6),
            "teardown_seconds": round(teardown_seconds, 6),
            "measured_phase_seconds": round(measured_phase_seconds, 6),
            "total_seconds": round(measured_phase_seconds, 6),
            "harness_total_seconds": round(time.perf_counter() - trial_started, 6),
            "token_attribution": usage.pop("attribution"),
            "token_scope": "terminal_codex_turn",
            "team_token_completeness": "unknown" if agent_attempted else "not_applicable",
            "skill_use_observed": (
                None if production_catalog else skill_used if ctx_enabled else None
            ),
            "skill_use_evidence_unavailable_reason": production_skill_use_evidence_reason(
                production_catalog=production_catalog,
                ctx_enabled=ctx_enabled,
                context_delivery_verified=context_delivery_verified,
                policy_abstention_verified=policy_abstention_verified,
            ),
            "mcp_tool_use_observed": mcp_used if ctx_enabled else None,
            "review_agent_use_observed": agent_used if ctx_enabled else None,
            "review_agent_attempt_observed": agent_attempted if ctx_enabled else None,
            "required_tool_failures": tool_failures,
            "lifecycle_actions": lifecycle["actions"] if lifecycle else [],
            "lifecycle_sha256": lifecycle["sha256"] if lifecycle else None,
            "lifecycle_session_status": lifecycle["session_status"] if lifecycle else None,
            "final_loaded": lifecycle["final_loaded"] if lifecycle else [],
            "reaped_descendants": agent.reaped_descendants,
            "residual_descendants": list(agent.residual_descendants),
            **trace_efficiency,
            **usage,
            **catalog_result_fields(),
            "artifact_dir": str(run_dir),
            "lifecycle_events": str(lifecycle_root / "events.jsonl") if ctx_enabled else None,
        }
    finally:
        try:
            close_session()
        finally:
            if production_catalog:
                remove_isolated_codex_home(agent_home)


def write_summary(output: Path, results: list[dict[str, Any]]) -> None:
    published_results: list[dict[str, Any]] = []
    for row in results:
        published = dict(row)
        if row.get("engine") == PRODUCTION_CATALOG_ENGINE and (
            not isinstance(row, _VerifiedProductionResult) or not row.is_sealed()
        ):
            published["production_efficiency_eligible"] = False
            if (
                row.get("repository_state_matches_start_at_end") is not False
                and row.get("environment_manifest_matches_start_at_end") is not False
            ):
                published["evidence_level"] = "attestation_pending"
        published_results.append(published)
    (output / "summary.json").write_text(
        json.dumps(published_results, indent=2) + "\n",
        encoding="utf-8",
    )
    fields = sorted({key for row in published_results for key in row})
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in published_results:
            writer.writerow(row)
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in results:
        if row.get("production_efficiency_eligible") is not True:
            continue
        if row.get("engine") == PRODUCTION_CATALOG_ENGINE and (
            not isinstance(row, _VerifiedProductionResult) or not row.is_sealed()
        ):
            continue
        key = (str(row.get("scenario")), str(row.get("arm")), int(row.get("trial", 0)))
        grouped.setdefault(key, []).append(row)
    aggregate: list[dict[str, Any]] = []
    for (scenario, arm, trial), attempts in sorted(grouped.items()):
        exact_tokens = [
            int(row["total_tokens"])
            for row in attempts
            if row.get("token_attribution") == "exact" and isinstance(row.get("total_tokens"), int)
        ]
        aggregate.append(
            {
                "scenario": scenario,
                "arm": arm,
                "trial": trial,
                "attempts": len(attempts),
                "first_attempt_passed": attempts[0].get("status") == "passed",
                "eventual_passed": any(row.get("status") == "passed" for row in attempts),
                "retries_used": max(0, len(attempts) - 1),
                "cumulative_seconds": round(
                    sum(float(row.get("total_seconds", 0.0)) for row in attempts), 6
                ),
                "cumulative_exact_tokens": (
                    sum(exact_tokens) if len(exact_tokens) == len(attempts) else None
                ),
            }
        )
    (output / "aggregate.json").write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    if aggregate:
        with (output / "aggregate.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(aggregate[0]))
            writer.writeheader()
            writer.writerows(aggregate)


def build_performance_report(
    results: list[dict[str, Any]],
    *,
    scenario_ids: list[str],
    trials: int,
    arms: tuple[str, ...],
    expected_repositories: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    def verified_policy_abstention(row: Mapping[str, Any]) -> bool:
        task_hash = row.get("task_prompt_sha256")
        body_fetch_seconds = row.get("body_fetch_seconds")
        return bool(
            row.get("policy_abstention_applied") is True
            and row.get("policy_abstention_verified") is True
            and row.get("policy_abstention_reason") == "language_only_match"
            and row.get("evidence_level") == PRODUCTION_POLICY_ABSTENTION_LEVEL
            and row.get("policy_valid") is True
            and row.get("context_delivery_verified") is False
            and all(
                row.get(field) == []
                for field in ("selected_ids", "delivered_ids", "adopted_ids", "used_ids")
            )
            and isinstance(task_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", task_hash) is not None
            and row.get("delivered_prompt_sha256") == task_hash
            and isinstance(body_fetch_seconds, int | float)
            and not isinstance(body_fetch_seconds, bool)
            and body_fetch_seconds == 0
            and row.get("lifecycle_actions") == ["dev_event", "session_end"]
            and row.get("lifecycle_session_status") == "passed"
            and row.get("catalog_recommendation_lifecycle_verified") is True
            and row.get("pinned_head_verified") is True
            and row.get("repository_state_matches_start_at_end") is True
            and row.get("environment_manifest_matches_start_at_end") is True
        )

    def eligible(row: dict[str, Any]) -> bool:
        measured = row.get("measured_phase_seconds")
        if (
            row.get("production_efficiency_eligible") is not True
            or not isinstance(measured, int | float)
            or isinstance(measured, bool)
            or measured <= 0
        ):
            return False
        if row.get("engine") != PRODUCTION_CATALOG_ENGINE:
            return True
        if not isinstance(row, _VerifiedProductionResult) or not row.is_sealed():
            return False
        if (
            row.get("repository_state_matches_start_at_end") is not True
            or row.get("environment_manifest_matches_start_at_end") is not True
        ):
            return False
        if row.get("evaluator_isolation_verified") is not True:
            return False
        return (
            row.get("arm") == "baseline"
            or row.get("context_delivery_verified") is True
            or verified_policy_abstention(row)
        )

    eligible_results = [row for row in results if eligible(row)]
    excluded_results = [row for row in results if not eligible(row)]
    efficiency_claim_allowed = bool(results) and len(eligible_results) == len(results)
    assigned: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in results:
        key = (str(row.get("scenario")), str(row.get("arm")), int(row.get("trial", 0)))
        assigned.setdefault(key, []).append(row)
    expected_result_keys = {
        (scenario_id, arm, trial)
        for scenario_id in scenario_ids
        for arm in arms
        for trial in range(1, trials + 1)
    }
    final_ctx_rows = [
        assigned[(scenario_id, "ctx-light", trial)][-1]
        for scenario_id in scenario_ids
        for trial in range(1, trials + 1)
        if assigned.get((scenario_id, "ctx-light", trial))
    ]
    trusted_final_ctx_rows = [row for row in final_ctx_rows if eligible(row)]
    delivered_context_count = sum(
        row.get("context_delivery_verified") is True for row in trusted_final_ctx_rows
    )
    verified_abstention_count = sum(
        verified_policy_abstention(row) for row in trusted_final_ctx_rows
    )
    ctx_assignment_count = len(final_ctx_rows)
    unverified_noop_count = (
        len(trusted_final_ctx_rows) - delivered_context_count - verified_abstention_count
    )
    untrusted_assignment_count = ctx_assignment_count - len(trusted_final_ctx_rows)
    abstention_only = bool(ctx_assignment_count) and (
        verified_abstention_count == ctx_assignment_count
    )
    attempts: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in eligible_results:
        key = (str(row.get("scenario")), str(row.get("arm")), int(row.get("trial", 0)))
        attempts.setdefault(key, []).append(row)

    def summed(rows: list[dict[str, Any]], field: str) -> float | None:
        if not rows:
            return None
        total = 0.0
        for row in rows:
            value = row.get(field)
            if not isinstance(value, int | float) or isinstance(value, bool):
                return None
            total += value
        return total

    def exact_tokens(rows: list[dict[str, Any]], field: str) -> int | None:
        if not rows:
            return None
        total = 0
        for row in rows:
            value = row.get(field) if row.get("token_attribution") == "exact" else None
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            total += value
        return total

    pairs: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        for trial in range(1, trials + 1):
            baseline_assigned = assigned.get((scenario_id, "baseline", trial), [])
            light_assigned = assigned.get((scenario_id, "ctx-light", trial), [])
            baseline = attempts.get((scenario_id, "baseline", trial), [])
            light = attempts.get((scenario_id, "ctx-light", trial), [])
            baseline_passed = bool(
                baseline_assigned and baseline_assigned[-1].get("status") == "passed"
            )
            light_passed = bool(light_assigned and light_assigned[-1].get("status") == "passed")
            pair: dict[str, Any] = {
                "scenario": scenario_id,
                "trial": trial,
                "baseline_status": (
                    baseline_assigned[-1].get("status") if baseline_assigned else "missing"
                ),
                "ctx_light_status": (
                    light_assigned[-1].get("status") if light_assigned else "missing"
                ),
                "baseline_passed": baseline_passed,
                "ctx_light_passed": light_passed,
                "paired_quality_preserved": not baseline_passed or light_passed,
            }
            if not baseline_assigned or not light_assigned:
                pair.update({"complete": False, "reason": "paired arms missing"})
                pairs.append(pair)
                continue
            if not baseline or not light:
                pair.update({"complete": False, "reason": "paired evidence ineligible"})
                pairs.append(pair)
                continue
            baseline_seconds = summed(baseline, "measured_phase_seconds")
            light_seconds = summed(light, "measured_phase_seconds")
            baseline_harness_seconds = summed(baseline, "harness_total_seconds")
            light_harness_seconds = summed(light, "harness_total_seconds")
            baseline_tokens = exact_tokens(baseline, "total_tokens")
            light_tokens = exact_tokens(light, "total_tokens")
            baseline_uncached = exact_tokens(baseline, "uncached_input_tokens")
            light_uncached = exact_tokens(light, "uncached_input_tokens")
            complete = (
                baseline_passed
                and light_passed
                and baseline_seconds is not None
                and light_seconds is not None
                and baseline_seconds > 0
                and light_seconds > 0
                and baseline_tokens is not None
                and light_tokens is not None
                and baseline_tokens > 0
                and light_tokens > 0
                and baseline_uncached is not None
                and light_uncached is not None
                and baseline_uncached > 0
                and light_uncached > 0
                and all(
                    row.get("team_token_completeness") != "unknown" for row in [*baseline, *light]
                )
            )
            if not complete:
                pair.update(
                    {
                        "complete": False,
                        "reason": "status time or token evidence missing",
                    }
                )
                pairs.append(pair)
                continue
            assert baseline_seconds is not None
            assert light_seconds is not None
            assert baseline_tokens is not None
            assert light_tokens is not None
            assert baseline_uncached is not None
            assert light_uncached is not None
            pair.update(
                {
                    "complete": True,
                    "baseline_first_attempt_passed": baseline[0].get("status") == "passed",
                    "ctx_light_first_attempt_passed": light[0].get("status") == "passed",
                    "time_ratio": round(light_seconds / baseline_seconds, 6),
                    "reported_token_ratio": round(light_tokens / baseline_tokens, 6),
                    "exact_token_ratio": round(light_tokens / baseline_tokens, 6),
                    "uncached_token_ratio": round(light_uncached / baseline_uncached, 6),
                    "harness_time_ratio": (
                        round(light_harness_seconds / baseline_harness_seconds, 6)
                        if baseline_harness_seconds and light_harness_seconds is not None
                        else None
                    ),
                }
            )
            pairs.append(pair)
    complete_pairs = [pair for pair in pairs if pair.get("complete")]
    expected_pairs = len(scenario_ids) * trials
    assignment_complete = (
        set(assigned) == expected_result_keys
        and len(pairs) == expected_pairs
        and all(
            pair["baseline_status"] != "missing" and pair["ctx_light_status"] != "missing"
            for pair in pairs
        )
    )
    evidence_required = (
        trials >= 6 and {"baseline", "ctx-light"}.issubset(set(arms)) and efficiency_claim_allowed
    )
    evidence_complete = (
        efficiency_claim_allowed and assignment_complete and len(complete_pairs) == expected_pairs
    )
    quality_preserved = assignment_complete and all(
        pair["paired_quality_preserved"] for pair in pairs
    )
    median_time_ratio = (
        round(median(float(pair["time_ratio"]) for pair in complete_pairs), 6)
        if complete_pairs
        else None
    )
    median_token_ratio = (
        round(
            median(float(pair["reported_token_ratio"]) for pair in complete_pairs),
            6,
        )
        if complete_pairs
        else None
    )
    median_uncached_token_ratio = (
        round(
            median(float(pair["uncached_token_ratio"]) for pair in complete_pairs),
            6,
        )
        if complete_pairs
        else None
    )
    expected_final_rows = [
        assigned[key][-1] for key in sorted(expected_result_keys) if assigned.get(key)
    ]
    expected_repository_map = dict(expected_repositories or {})
    expected_repository_map_valid = bool(
        expected_repositories is not None
        and set(expected_repository_map) == set(scenario_ids)
        and all(
            isinstance(repository, str) and GITHUB_REPO_URL.fullmatch(repository) is not None
            for repository in expected_repository_map.values()
        )
        and len(set(expected_repository_map.values()))
        == len({repository.casefold() for repository in expected_repository_map.values()})
    )
    repositories_by_scenario: dict[str, set[str]] = {
        scenario_id: set() for scenario_id in scenario_ids
    }
    repository_observations_by_scenario: dict[str, list[str | None]] = {
        scenario_id: [] for scenario_id in scenario_ids
    }
    for row in results:
        scenario_id = str(row.get("scenario"))
        repository = row.get("repo_url")
        if scenario_id not in repositories_by_scenario:
            continue
        repository_value = repository if isinstance(repository, str) and repository else None
        repository_observations_by_scenario[scenario_id].append(repository_value)
        if repository_value is not None:
            repositories_by_scenario[scenario_id].add(repository_value)
    repository_identity_verified = bool(
        expected_repository_map_valid
        and results
        and all(
            str(row.get("scenario")) in expected_repository_map
            and row.get("repo_url") == expected_repository_map[str(row.get("scenario"))]
            for row in results
        )
    )

    scenario_effects: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        scenario_pairs = [pair for pair in complete_pairs if pair.get("scenario") == scenario_id]
        scenario_repositories = sorted(repositories_by_scenario[scenario_id])
        repository_observations = repository_observations_by_scenario[scenario_id]
        expected_repository = (
            expected_repository_map.get(scenario_id) if expected_repository_map_valid else None
        )
        scenario_repository_identity_verified = bool(
            expected_repository
            and repository_observations
            and all(repository == expected_repository for repository in repository_observations)
        )
        scenario_complete = len(scenario_pairs) == trials and scenario_repository_identity_verified
        scenario_effect: dict[str, Any] = {
            "scenario": scenario_id,
            "repository": expected_repository,
            "repository_values": scenario_repositories,
            "repository_observation_count": len(repository_observations),
            "repository_identity_verified": scenario_repository_identity_verified,
            "trial_pair_count": len(scenario_pairs),
            "complete": scenario_complete,
            "quality_preserved": (
                scenario_complete
                and all(pair["paired_quality_preserved"] for pair in scenario_pairs)
            ),
            "median_time_ratio": None,
            "median_uncached_token_ratio": None,
        }
        if scenario_complete:
            scenario_effect["median_time_ratio"] = round(
                median(float(pair["time_ratio"]) for pair in scenario_pairs),
                6,
            )
            scenario_effect["median_uncached_token_ratio"] = round(
                median(float(pair["uncached_token_ratio"]) for pair in scenario_pairs),
                6,
            )
        scenario_effects.append(scenario_effect)

    complete_scenario_effects = [effect for effect in scenario_effects if effect["complete"]]
    repository_effects: list[dict[str, Any]] = []
    for repository in sorted(
        {str(effect["repository"]) for effect in complete_scenario_effects if effect["repository"]}
    ):
        effects = [
            effect for effect in complete_scenario_effects if effect["repository"] == repository
        ]
        repository_time_ratio = round(
            median(float(effect["median_time_ratio"]) for effect in effects),
            6,
        )
        repository_token_ratio = round(
            median(float(effect["median_uncached_token_ratio"]) for effect in effects),
            6,
        )
        repository_effects.append(
            {
                "repository": repository,
                "scenario_count": len(effects),
                "scenarios": sorted(str(effect["scenario"]) for effect in effects),
                "median_time_ratio": repository_time_ratio,
                "median_uncached_token_ratio": repository_token_ratio,
                "quality_preserved": all(effect["quality_preserved"] for effect in effects),
                "non_regression_supported": (
                    repository_time_ratio <= PRODUCT_OTHER_RATIO_MAX
                    and repository_token_ratio <= PRODUCT_OTHER_RATIO_MAX
                ),
                "benefit_supported": (
                    (
                        repository_time_ratio <= PRODUCT_BENEFIT_RATIO_MAX
                        and repository_token_ratio <= PRODUCT_OTHER_RATIO_MAX
                    )
                    or (
                        repository_token_ratio <= PRODUCT_BENEFIT_RATIO_MAX
                        and repository_time_ratio <= PRODUCT_OTHER_RATIO_MAX
                    )
                ),
            }
        )

    observed_repositories = sorted(
        {
            str(row["repo_url"])
            for row in expected_final_rows
            if isinstance(row.get("repo_url"), str) and row["repo_url"]
        }
    )
    canonical_expected_repositories: dict[str, str] = {}
    if expected_repository_map_valid:
        for repository in expected_repository_map.values():
            canonical_expected_repositories.setdefault(repository.casefold(), repository)
    repositories = (
        sorted(canonical_expected_repositories.values())
        if expected_repository_map_valid
        else observed_repositories
    )
    distinct_scenario_count = len(set(scenario_ids))
    repository_support_count = sum(effect["benefit_supported"] for effect in repository_effects)
    repository_support_p_value = (
        round(
            sum(
                comb(len(repository_effects), successes)
                for successes in range(repository_support_count, len(repository_effects) + 1)
            )
            / (2 ** len(repository_effects)),
            12,
        )
        if repository_effects
        else None
    )
    clustered_evidence_complete = bool(
        evidence_complete
        and distinct_scenario_count >= PRODUCT_CLAIM_MIN_SCENARIOS
        and len(repositories) >= PRODUCT_CLAIM_MIN_REPOSITORIES
        and trials >= PRODUCT_CLAIM_MIN_TRIALS
        and repository_identity_verified
        and len(complete_scenario_effects) == distinct_scenario_count
        and len(repository_effects) == len(repositories)
    )
    repository_quality_preserved = bool(repository_effects) and all(
        effect["quality_preserved"] for effect in repository_effects
    )
    all_repositories_non_regressing = bool(repository_effects) and all(
        effect["non_regression_supported"] for effect in repository_effects
    )
    gate_passed = None
    if evidence_required:
        gate_passed = bool(
            evidence_complete
            and quality_preserved
            and median_time_ratio is not None
            and median_time_ratio <= PRODUCT_OTHER_RATIO_MAX
            and median_token_ratio is not None
            and median_token_ratio <= PRODUCT_OTHER_RATIO_MAX
        )
    production_catalog_scored = bool(eligible_results) and all(
        row.get("engine") == PRODUCTION_CATALOG_ENGINE for row in eligible_results
    )
    benefit_evidence_required = bool(
        production_catalog_scored
        and {"baseline", "ctx-light"}.issubset(set(arms))
        and expected_pairs >= 6
    )
    benefit_evidence_complete = bool(
        benefit_evidence_required
        and assignment_complete
        and efficiency_claim_allowed
        and delivered_context_count > 0
    )
    beneficial = (
        bool(
            quality_preserved
            and evidence_complete
            and median_time_ratio is not None
            and median_uncached_token_ratio is not None
            and (
                (
                    median_time_ratio <= PRODUCT_BENEFIT_RATIO_MAX
                    and median_uncached_token_ratio <= PRODUCT_OTHER_RATIO_MAX
                )
                or (
                    median_uncached_token_ratio <= PRODUCT_BENEFIT_RATIO_MAX
                    and median_time_ratio <= PRODUCT_OTHER_RATIO_MAX
                )
            )
        )
        if benefit_evidence_complete
        else None
    )
    benefit_verdict = (
        "beneficial"
        if beneficial is True
        else "not_beneficial"
        if beneficial is False
        else "policy_abstention_only"
        if abstention_only
        else "insufficient_evidence"
        if production_catalog_scored
        else "not_applicable"
    )
    product_claim_eligible = bool(clustered_evidence_complete and benefit_evidence_complete)
    product_beneficial = (
        bool(
            repository_quality_preserved
            and all_repositories_non_regressing
            and repository_support_p_value is not None
            and repository_support_p_value <= PRODUCT_SUPPORT_ALPHA
        )
        if product_claim_eligible
        else None
    )
    arm_outcomes = {}
    for arm in ("baseline", "ctx-light"):
        rows = [
            pair
            for pair in pairs
            if pair[f"{'ctx_light' if arm == 'ctx-light' else 'baseline'}_status"] != "missing"
        ]
        passed = sum(
            bool(pair[f"{'ctx_light' if arm == 'ctx-light' else 'baseline'}_passed"])
            for pair in rows
        )
        arm_outcomes[arm] = {
            "assigned": len(rows),
            "passed": passed,
            "pass_rate": round(passed / len(rows), 6) if rows else None,
        }
    return {
        "status": (
            "functional_only"
            if not efficiency_claim_allowed
            else "passed"
            if gate_passed is True
            else "failed"
            if gate_passed is False
            else "diagnostic"
        ),
        "production_efficiency_claim_allowed": efficiency_claim_allowed,
        "excluded_result_count": len(excluded_results),
        "excluded_evidence_levels": sorted(
            {str(row.get("evidence_level") or "unspecified") for row in excluded_results}
        ),
        "evidence_required": evidence_required,
        "assignment_complete": assignment_complete,
        "evidence_complete": evidence_complete,
        "quality_preserved": quality_preserved,
        "benefit_evidence_required": benefit_evidence_required,
        "benefit_evidence_complete": benefit_evidence_complete,
        "beneficial": beneficial,
        "benefit_verdict": benefit_verdict,
        "ctx_policy_outcomes": {
            "assigned": ctx_assignment_count,
            "context_delivered": delivered_context_count,
            "verified_abstentions": verified_abstention_count,
            "unverified_noops": unverified_noop_count,
            "untrusted_assignments": untrusted_assignment_count,
            "activation_rate": (
                round(delivered_context_count / ctx_assignment_count, 6)
                if ctx_assignment_count
                else None
            ),
            "abstention_rate": (
                round(verified_abstention_count / ctx_assignment_count, 6)
                if ctx_assignment_count
                else None
            ),
        },
        "claim_scope": "product_pilot" if product_claim_eligible else "scenario_set_only",
        "product_claim_eligible": product_claim_eligible,
        "product_benefit_verdict": (
            "beneficial"
            if product_beneficial is True
            else "not_beneficial"
            if product_beneficial is False
            else "insufficient_cross_repo_evidence"
        ),
        "product_beneficial": product_beneficial,
        "distinct_scenario_count": distinct_scenario_count,
        "distinct_repository_count": len(repositories),
        "repositories": repositories,
        "repository_identity": {
            "predeclared_mapping_provided": expected_repositories is not None,
            "predeclared_mapping_valid": expected_repository_map_valid,
            "verified_for_all_attempts": repository_identity_verified,
            "comparison": "exact_frozen_scenario_url",
            "canonical_count_key": "casefolded_https_github_url",
            "observed_repositories": observed_repositories,
        },
        "repository_cluster_analysis": {
            "method": ("scenario_median_then_repository_median_with_exact_one_sided_support"),
            "independent_unit": "repository",
            "repeated_trials_count_as_independent_units": False,
            "complete": clustered_evidence_complete,
            "minimum_scenarios": PRODUCT_CLAIM_MIN_SCENARIOS,
            "minimum_independent_repositories": PRODUCT_CLAIM_MIN_REPOSITORIES,
            "minimum_trials_per_scenario": PRODUCT_CLAIM_MIN_TRIALS,
            "independent_repository_count": len(repository_effects),
            "benefit_support_count": repository_support_count,
            "benefit_support_p_value": repository_support_p_value,
            "support_alpha": PRODUCT_SUPPORT_ALPHA,
            "all_repositories_non_regressing": all_repositories_non_regressing,
            "quality_preserved": repository_quality_preserved,
            "scenario_effects": scenario_effects,
            "repository_effects": repository_effects,
        },
        "intent_to_treat": arm_outcomes,
        "thresholds": {
            "median_time_ratio_max": PRODUCT_OTHER_RATIO_MAX,
            "median_reported_token_ratio_max": PRODUCT_OTHER_RATIO_MAX,
        },
        "benefit_thresholds": {
            "minimum_complete_pairs": PRODUCT_CLAIM_MIN_TRIALS,
            "minimum_scenarios": PRODUCT_CLAIM_MIN_SCENARIOS,
            "minimum_independent_repositories": PRODUCT_CLAIM_MIN_REPOSITORIES,
            "improvement_ratio_max": PRODUCT_BENEFIT_RATIO_MAX,
            "other_ratio_max": PRODUCT_OTHER_RATIO_MAX,
            "primary_token_metric": "uncached_input_tokens",
            "quality_preserved_required": True,
            "repository_support_alpha": PRODUCT_SUPPORT_ALPHA,
        },
        "median_time_ratio": median_time_ratio,
        "median_reported_token_ratio": median_token_ratio,
        "median_exact_token_ratio": median_token_ratio,
        "median_uncached_token_ratio": median_uncached_token_ratio,
        "gate_passed": gate_passed,
        "evidence_trust_boundary": EVIDENCE_TRUST_BOUNDARY,
        "cryptographic_independence": False,
        "pairs": pairs,
    }


def write_performance_report(
    output: Path,
    results: list[dict[str, Any]],
    *,
    scenario_ids: list[str],
    trials: int,
    arms: tuple[str, ...],
    expected_repositories: Mapping[str, str],
) -> dict[str, Any]:
    report = build_performance_report(
        results,
        scenario_ids=scenario_ids,
        trials=trials,
        arms=arms,
        expected_repositories=expected_repositories,
    )
    (output / "performance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument(
        "--engine",
        choices=BENCHMARK_ENGINES,
        default="codex-controlled",
    )
    parser.add_argument(
        "--arm",
        choices=("baseline", "ctx-light", "ctx-full", "both", "all"),
        default="both",
    )
    parser.add_argument("--model", default=os.environ.get("CTX_BENCHMARK_MODEL", "gpt-5.5"))
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--api-key-env")
    parser.add_argument("--base-url")
    parser.add_argument("--max-iterations", type=int, default=25)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--provider-timeout", type=float, default=120.0)
    parser.add_argument("--cache-root", type=Path, default=Path.home() / ".cache/ctx-ab")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    return parser


def dry_run_results_complete(
    results: list[dict[str, Any]],
    *,
    expected_keys: set[tuple[str, str, int]],
    engine: str,
) -> bool:
    expected_evidence_level = {
        "codex-controlled": "controlled_wiring_only",
        PRODUCTION_CATALOG_ENGINE: "production_catalog_wiring_only",
    }.get(engine, "wiring_only")
    observed_keys = {
        (
            str(row.get("scenario")),
            str(row.get("arm")),
            int(row.get("trial", 0)),
        )
        for row in results
    }
    return observed_keys == expected_keys and all(
        row.get("status") == "wiring_only" and row.get("evidence_level") == expected_evidence_level
        for row in results
    )


def _is_system_temp_path(path: Path) -> bool:
    resolved = path.resolve()
    roots = {Path(value).resolve() for value in ("/tmp", "/private/tmp", "/var/tmp")}
    return any(resolved == root or root in resolved.parents for root in roots)


def _validate_production_output_path(path: Path) -> Path:
    gate = ROOT / ".gate"
    if gate.is_symlink() or PRODUCTION_PRIVATE_RUN_ROOT.is_symlink():
        raise ValueError("production benchmark private root must not be a symlink")
    resolved = path.resolve()
    private_root = PRODUCTION_PRIVATE_RUN_ROOT.resolve()
    if resolved == private_root or private_root not in resolved.parents:
        raise ValueError(
            f"{PRODUCTION_CATALOG_ENGINE} output must be beneath {PRODUCTION_PRIVATE_RUN_ROOT}"
        )
    return resolved


def _validate_production_scenarios_path(path: Path, *, live: bool) -> Path:
    if _is_system_temp_path(path):
        raise ValueError(
            f"{PRODUCTION_CATALOG_ENGINE} scenario source must not be under a system "
            "temporary directory"
        )
    resolved = path.resolve()
    if not live:
        return resolved
    private_root = PRODUCTION_PRIVATE_SCENARIO_ROOT.resolve()
    if not private_root.is_dir() or stat.S_IMODE(private_root.stat().st_mode) & (
        stat.S_IRWXG | stat.S_IRWXO
    ):
        raise ValueError("live production scenario root must be an owner-only directory")
    if (
        PRODUCTION_PRIVATE_SCENARIO_ROOT.is_symlink()
        or resolved == private_root
        or private_root not in resolved.parents
    ):
        raise ValueError(
            f"live {PRODUCTION_CATALOG_ENGINE} scenarios must be beneath "
            f"{PRODUCTION_PRIVATE_SCENARIO_ROOT}"
        )
    if path.is_symlink() or not resolved.is_file():
        raise ValueError("live production scenario source must be a regular file")
    if stat.S_IMODE(resolved.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("live production scenario source must be owner-only")
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    independence_attestation: dict[str, str] | None = None
    if args.engine == PRODUCTION_CATALOG_ENGINE:
        try:
            args.scenarios = _validate_production_scenarios_path(
                args.scenarios,
                live=not args.dry_run and not args.list,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    scenarios = load_scenarios(args.scenarios)
    if args.scenario:
        requested = set(args.scenario)
        scenarios = [scenario for scenario in scenarios if scenario.id in requested]
        missing = requested - {scenario.id for scenario in scenarios}
        if missing:
            raise SystemExit(f"unknown scenarios: {', '.join(sorted(missing))}")
    if args.list:
        for scenario in scenarios:
            print(f"{scenario.id}\t{scenario.commit}\t{scenario.repo_url}")
        return 0
    if args.engine == PRODUCTION_CATALOG_ENGINE:
        try:
            independence_attestation = validate_runtime_pack_scenario_independence(
                scenarios,
                scenarios_path=args.scenarios,
                archive_path=PRODUCTION_CATALOG_ARCHIVE,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if args.trials < 1 or args.retries < 0:
        raise SystemExit("--trials must be >= 1 and --retries must be >= 0")
    if (
        args.max_iterations < 1
        or (args.max_tokens is not None and args.max_tokens < 1)
        or args.provider_timeout <= 0
    ):
        raise SystemExit("production ctx limits must be positive")
    if sys.platform != "darwin":
        raise SystemExit("benchmark execution currently requires macOS sandbox-exec")
    repository_state = collect_repository_state()
    try:
        require_clean_production_repository(
            repository_state,
            live=args.engine == PRODUCTION_CATALOG_ENGINE and not args.dry_run,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    run_name = f"ctx-ab-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    output = args.output or (
        PRODUCTION_PRIVATE_RUN_ROOT / run_name
        if args.engine == PRODUCTION_CATALOG_ENGINE
        else Path("/tmp") / run_name
    )
    if args.engine == PRODUCTION_CATALOG_ENGINE:
        try:
            output = _validate_production_output_path(output)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        PRODUCTION_PRIVATE_RUN_ROOT.mkdir(
            mode=stat.S_IRWXU,
            parents=True,
            exist_ok=True,
        )
        PRODUCTION_PRIVATE_RUN_ROOT.chmod(stat.S_IRWXU)
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory must be empty: {output}")
    output.mkdir(mode=stat.S_IRWXU, parents=True, exist_ok=True)
    if args.engine == PRODUCTION_CATALOG_ENGINE:
        output.chmod(stat.S_IRWXU)
    incidents = IncidentLog(output / "incidents.csv")
    arms = arms_for_mode(args.arm)
    if args.engine in {"production-ctx-run", PRODUCTION_CATALOG_ENGINE} and "ctx-full" in arms:
        raise SystemExit(f"{args.engine} supports only baseline, ctx-light, or both")
    if args.engine == PRODUCTION_CATALOG_ENGINE and args.retries:
        raise SystemExit(
            f"{PRODUCTION_CATALOG_ENGINE} currently requires --retries 0: repair retries "
            "must continue from the prior workspace with evaluator failure evidence; "
            "fresh silent retries are an explicit follow-up blocker"
        )
    if (
        args.engine == "production-ctx-run"
        and args.api_key_env
        and not os.environ.get(args.api_key_env)
        and not args.dry_run
    ):
        raise SystemExit(f"provider key environment variable is not set: {args.api_key_env}")
    catalog_snapshot: CatalogSnapshot | None = None
    if args.engine == PRODUCTION_CATALOG_ENGINE:
        try:
            catalog_snapshot = prepare_production_catalog(args.cache_root)
            assert independence_attestation is not None
            verify_scenario_independence_attestation(
                independence_attestation,
                scenarios_path=args.scenarios,
                snapshot=catalog_snapshot,
            )
        except Exception as exc:  # noqa: BLE001 - persist catalog install failures.
            incidents.add(
                scenario="control",
                arm="control",
                attempt=1,
                stage="production-catalog-cache",
                message=type(exc).__name__,
                evidence=str(exc),
            )
            write_summary(output, [])
            print(output)
            return 1
    schedule = trial_schedule(scenarios, arms, args.trials)
    environment_manifest = write_environment_manifest(
        output=output,
        scenarios_path=args.scenarios,
        scenarios=scenarios,
        codex=args.codex,
        model=args.model,
        run_config={
            "engine": args.engine,
            "arm_mode": args.arm,
            "arms": list(arms),
            "trials": args.trials,
            "retries": args.retries,
            "timeout_seconds": args.timeout,
            "max_iterations": args.max_iterations,
            "max_tokens": args.max_tokens,
            "provider_timeout_seconds": args.provider_timeout,
            "api_key_env": args.api_key_env,
            "base_url": args.base_url,
            "dry_run": args.dry_run,
            "cache_root": str(args.cache_root),
            "scenario_filters": list(args.scenario),
            "catalog_provenance": (
                catalog_snapshot.provenance if catalog_snapshot is not None else None
            ),
            "runtime_pack_independence_attestation": independence_attestation,
            "catalog_cache_hit": (
                catalog_snapshot.cache_hit if catalog_snapshot is not None else None
            ),
            "catalog_prepare_seconds": (
                round(catalog_snapshot.prepare_seconds, 6) if catalog_snapshot is not None else None
            ),
            "repair_policy": (
                "fail_closed_no_retries"
                if args.engine == PRODUCTION_CATALOG_ENGINE
                else "engine_default"
            ),
        },
        schedule=schedule,
        repository_state=repository_state,
    )
    results: list[dict[str, Any]] = []
    schedule_by_key = {
        (str(row["scenario"]), int(row["trial"])): tuple(row["arms"]) for row in schedule
    }
    for scenario in scenarios:
        cache: Path | None = None
        failed_cache_attempts: set[int] = set()
        for cache_attempt in range(args.retries + 1):
            try:
                cache = ensure_repo_cache(scenario, args.cache_root)
                if failed_cache_attempts:
                    incidents.resolve_attempts(
                        scenario=scenario.id,
                        arm="control",
                        attempts=failed_cache_attempts,
                        resolved_by=cache_attempt + 1,
                    )
                break
            except Exception as exc:  # noqa: BLE001 - persist cache failures.
                failed_cache_attempts.add(cache_attempt + 1)
                incidents.add(
                    scenario=scenario.id,
                    arm="control",
                    attempt=cache_attempt + 1,
                    stage="repo-cache",
                    message=type(exc).__name__,
                    evidence=str(exc),
                )
        if cache is None:
            continue
        try:
            validate_evaluator_controls(scenario, cache=cache, output=output)
        except Exception as exc:  # noqa: BLE001 - persist control failures.
            incidents.add(
                scenario=scenario.id,
                arm="control",
                attempt=1,
                stage="evaluator-control",
                message=type(exc).__name__,
                evidence=str(exc),
            )
            continue
        for trial in range(1, args.trials + 1):
            for arm in schedule_by_key[(scenario.id, trial)]:
                treatment_level = arm
                failed_attempts: set[int] = set()
                for retry in range(args.retries + 1):
                    attempt = (trial - 1) * (args.retries + 1) + retry + 1
                    try:
                        if args.engine == "production-ctx-run":
                            result = run_production_trial(
                                scenario,
                                arm=arm,
                                attempt=attempt,
                                trial=trial,
                                retry=retry,
                                cache=cache,
                                output=output,
                                model=args.model,
                                timeout=args.timeout,
                                dry_run=args.dry_run,
                                incidents=incidents,
                                api_key_env=args.api_key_env,
                                base_url=args.base_url,
                                max_iterations=args.max_iterations,
                                max_tokens=args.max_tokens,
                                provider_timeout=args.provider_timeout,
                            )
                        else:
                            result = run_trial(
                                scenario,
                                arm=arm,
                                treatment_level=treatment_level,
                                attempt=attempt,
                                trial=trial,
                                retry=retry,
                                cache=cache,
                                output=output,
                                codex=args.codex,
                                model=args.model,
                                timeout=args.timeout,
                                dry_run=args.dry_run,
                                incidents=incidents,
                                catalog_snapshot=(
                                    catalog_snapshot
                                    if args.engine == PRODUCTION_CATALOG_ENGINE
                                    else None
                                ),
                                forbidden_agent_reads=(
                                    {
                                        "scenario_source": args.scenarios.resolve(),
                                        "evaluator_control": output
                                        / scenario.id
                                        / "controls"
                                        / "control.json",
                                    }
                                    if args.engine == PRODUCTION_CATALOG_ENGINE and not args.dry_run
                                    else None
                                ),
                            )
                    except Exception as exc:  # noqa: BLE001 - persist harness failures.
                        incidents.add(
                            scenario=scenario.id,
                            arm=arm,
                            attempt=attempt,
                            stage="harness",
                            message=type(exc).__name__,
                            evidence=str(exc),
                        )
                        result = {
                            "scenario": scenario.id,
                            "arm": arm,
                            "trial": trial,
                            "retry": retry,
                            "attempt": attempt,
                            "status": "harness_error",
                            "error": f"{type(exc).__name__}: {exc}",
                            "endpoint_class": "not_evaluated",
                            "evidence_level": "harness_error",
                            "production_efficiency_eligible": False,
                            "provider_identity": None,
                            "provider_identity_verified": False,
                            "provider_endpoint_verified": False,
                            "provider_auth_mode": "not_evaluated",
                            "provider_authentication_evidence": "not_established",
                            "provider_authentication_verified": False,
                            "provider_response_success": False,
                        }
                    if (
                        args.engine == PRODUCTION_CATALOG_ENGINE
                        and result.get("status") != "harness_error"
                    ):
                        result = _VerifiedProductionResult(result)
                    results.append(result)
                    write_summary(output, results)
                    if args.dry_run or result.get("status") == "passed":
                        if failed_attempts:
                            incidents.resolve_attempts(
                                scenario=scenario.id,
                                arm=arm,
                                attempts=failed_attempts,
                                resolved_by=attempt,
                            )
                        break
                    failed_attempts.add(attempt)
                    if args.engine == "codex-controlled":
                        treatment_level = next_treatment_level(
                            arm,
                            treatment_level,
                            agent_returncode=result.get("agent_returncode"),
                            agent_timed_out=result.get("agent_timed_out"),
                            policy_valid=result.get("policy_valid"),
                            verification_returncode=result.get("verification_returncode"),
                        )
    expected_keys = {
        (scenario.id, arm, trial)
        for scenario in scenarios
        for arm in arms
        for trial in range(1, args.trials + 1)
    }
    (
        repository_state_matches,
        environment_manifest_matches,
        repository_state_end,
    ) = write_final_repository_attestation(
        output,
        repository_state,
        environment_manifest,
    )
    if args.engine == PRODUCTION_CATALOG_ENGINE:
        for result in results:
            result["repository_state_matches_start_at_end"] = repository_state_matches
            result["environment_manifest_matches_start_at_end"] = environment_manifest_matches
        if not repository_state_matches or not environment_manifest_matches:
            incidents.add(
                scenario="control",
                arm="control",
                attempt=1,
                stage="run-attestation",
                message="RunAttestationChanged",
                evidence=json.dumps(
                    {
                        "repository_state_start": repository_state,
                        "repository_state_end": repository_state_end,
                        "repository_state_matches_start_at_end": repository_state_matches,
                        "environment_manifest_matches_start_at_end": (environment_manifest_matches),
                    },
                    sort_keys=True,
                ),
            )
            for result in results:
                result["production_efficiency_eligible"] = False
                result["evidence_level"] = "run_attestation_changed"
        for result in results:
            if (
                isinstance(result, _VerifiedProductionResult)
                and repository_state_matches
                and environment_manifest_matches
            ):
                result.seal()
    write_summary(output, results)
    if args.dry_run:
        print(output)
        return (
            0
            if dry_run_results_complete(
                results,
                expected_keys=expected_keys,
                engine=args.engine,
            )
            and incidents.unresolved_count() == 0
            else 1
        )
    performance = write_performance_report(
        output,
        results,
        scenario_ids=[scenario.id for scenario in scenarios],
        trials=args.trials,
        arms=arms,
        expected_repositories={scenario.id: scenario.repo_url for scenario in scenarios},
    )
    final: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in results:
        final[
            (
                str(row.get("scenario")),
                str(row.get("arm")),
                int(row.get("trial", 0)),
            )
        ] = row
    print(output)
    return (
        0
        if set(final) == expected_keys
        and all(row.get("status") == "passed" for row in final.values())
        and performance.get("gate_passed") is not False
        and incidents.unresolved_count() == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
