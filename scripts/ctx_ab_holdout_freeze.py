#!/usr/bin/env python3
"""Freeze authenticated V2 holdout artifacts for confirmatory execution."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import tempfile
from typing import Any

from scripts import ctx_ab_benchmark as benchmark
from scripts import ctx_ab_exposure_ledger as exposure_ledger
from scripts import ctx_ab_failure_evidence as failure_evidence
from scripts import ctx_ab_holdout as holdout


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = ROOT / ".gate" / "ctx-ab-private"
V1_PROTOCOL_PATH = ROOT / "benchmarks" / "ctx_ab" / "holdout-protocol-v1.json"
V1_PROTOCOL_SHA256 = "14c3e623b6a3dced3b41769a9e8b60faed5c921aa4f1456d4bde907f1f8a60fa"
PROTOCOL_ID = "production-graph-holdout-v2"
SEED_PREFIX = b"ctx-holdout-selection-v2\0"
CANDIDATE_PARTITION_PREFIX = holdout.V2_CANDIDATE_PARTITION_PREFIX
PROTOCOL_GENERATION = 1
REPOSITORY_COUNT = 10
TRIALS_PER_SCENARIO = 3
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
ARMS = ("baseline", "ctx-light")
ACQUISITION_EXECUTION_INPUT_KEYS = frozenset(
    {
        "acquisition_protocol_sha256",
        "collision_attestation_sha256",
        "control_results_sha256",
        "execution_environment_sha256",
        "execution_schedule_sha256",
        "reconstructed_test_attestation_sha256",
        "scenario_pack_sha256",
        "selection_output_sha256",
        "source_map_sha256",
    }
)
SOURCE_MAP_KEYS = frozenset({"repositories", "schema_version"})
SOURCE_MAP_REPOSITORY_KEYS = frozenset(
    {
        "base_commit",
        "bundle_path",
        "bundle_sha256",
        "tree_sha1",
    }
)
SELECTION_KEYS = frozenset(
    {
        "analysis_instance_ids",
        "analysis_repository_map",
        "canary_instance_id",
        "canary_repository",
        "protocol_id",
    }
)
SCENARIO_KEYS = frozenset(
    {
        "allowed_changes",
        "benchmark_class",
        "commit",
        "ctx_context",
        "expected_test_count",
        "id",
        "language",
        "official_verifier_binding",
        "query",
        "red_failure_contains",
        "reference_patch",
        "regression_verify",
        "repo_url",
        "reconstructed_test_sha256",
        "task",
        "test_body",
        "test_path",
        "verify",
    }
)
VERIFIER_BINDING_KEYS = frozenset(
    {
        "allowed_paths_sha256",
        "base_commit",
        "bridge_sha256",
        "dataset_row_sha256",
        "dataset_sha256",
        "docker_cli_sha256",
        "docker_daemon_id_sha256",
        "docker_package_sha256",
        "docker_server_version",
        "fail_to_pass_sha256",
        "harness_revision",
        "harness_source_sha256",
        "image_content_digest",
        "pass_to_pass_sha256",
        "python_environment_sha256",
        "python_sha256",
        "repository_tree_sha1",
        "repository_url",
        "run_evaluation_sha256",
        "runtime_identity_sha256",
        "schema_version",
    }
)
COLLISION_KEYS = frozenset(
    {
        "catalog_archive_sha256",
        "collision_count",
        "collision_free",
        "guard",
        "runtime_availability_sha256",
        "scenario_ids",
        "scenarios_sha256",
    }
)
RECONSTRUCTED_KEYS = frozenset(
    {
        "guard",
        "module_sha256",
        "selection_sha256",
    }
)
MATERIALIZATION_CONTROL_KEYS = frozenset(
    {
        "all_scenarios_passed",
        "guard",
        "scenario_count",
        "scenario_pack_sha256",
        "scenario_results",
        "selection_sha256",
        "verifier_pins_sha256",
    }
)
SCENARIO_CONTROL_KEYS = frozenset(
    {
        "changed_test_module_green",
        "elapsed_seconds",
        "green_evidence_sha256",
        "module_evidence_sha256",
        "official_swebench",
        "parent_with_test_patch_red",
        "reconstructed_test_sha256",
        "red_evidence_sha256",
        "reference_patch_green",
        "timeout_compliant",
        "timeout_seconds",
    }
)
OFFICIAL_CONTROL_KEYS = frozenset({"green", "image_id", "pins_sha256", "red"})
PHASE_KEYS = frozenset(
    {
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
    }
)
VERIFIER_PIN_KEYS = frozenset(
    {
        "bridge_sha256",
        "docker_cli_sha256",
        "docker_daemon_id",
        "docker_package_sha256",
        "docker_server_version",
        "namespace",
        "python_environment_sha256",
        "python_sha256",
        "revision",
        "run_evaluation_sha256",
        "schema_version",
    }
)
PRODUCT_INPUT_KEYS = frozenset(
    {
        "benchmark_script_sha256",
        "catalog_archive_sha256",
        "codex_binary_sha256",
        "origin_main_revision",
        "origin_url",
        "provider_config_sha256",
        "revision",
        "runtime_availability_sha256",
    }
)
ENVIRONMENT_KEYS = frozenset(
    {
        "codex",
        "evaluator",
        "limits",
        "model",
        "product_revision",
        "protocol_id",
        "provider",
        "python",
        "schema_version",
    }
)
LIMIT_KEYS = frozenset(
    {
        "agent_timeout_seconds",
        "arms",
        "catalog_cache_hit",
        "measured_concurrency",
        "pair_count",
        "retries",
        "sandbox_contract",
        "task_count",
        "trials_per_scenario",
    }
)


class FreezeError(RuntimeError):
    """The holdout cannot be execution-frozen."""


@dataclass(frozen=True)
class SourceBundle:
    """Authenticated source bundle pinned by the private source map."""

    base_commit: str
    bundle_path: Path
    bundle_sha256: str
    tree_sha1: str


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return data + (b"\n" if newline else b"")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise FreezeError(f"JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def _json_object(data: bytes, *, label: str) -> dict[str, Any]:
    def reject_constant(_value: str) -> None:
        raise FreezeError(f"{label} contains a non-finite JSON number")

    try:
        value = json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FreezeError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise FreezeError(f"{label} must contain an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], *, label: str) -> None:
    if set(value) != expected:
        raise FreezeError(f"{label} has an unsupported shape")


def _require_sha256(value: object, *, label: str) -> str:
    text = str(value or "")
    if SHA256.fullmatch(text) is None:
        raise FreezeError(f"{label} is not a SHA-256")
    return text


def _require_text(value: object, *, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FreezeError(f"{label} is invalid")
    return value


def _canonical_timestamp(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise FreezeError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FreezeError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise FreezeError(f"{label} must include a timezone")
    normalized = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if value != normalized:
        raise FreezeError(f"{label} must use canonical UTC form")
    return value


def _supported_v1_protocol() -> dict[str, Any]:
    try:
        data = V1_PROTOCOL_PATH.read_bytes()
    except OSError as exc:
        raise FreezeError("committed V1 protocol is unavailable") from exc
    if not secrets.compare_digest(_sha256(data), V1_PROTOCOL_SHA256):
        raise FreezeError("committed V1 protocol identity changed")
    protocol = _json_object(data, label="committed V1 protocol")
    if (
        protocol.get("schema_version") != 1
        or protocol.get("protocol_id") != "production-graph-holdout-v1"
        or not isinstance(protocol.get("universe"), dict)
        or not isinstance(protocol.get("static_candidate_rules"), dict)
        or not isinstance(protocol.get("ranking"), dict)
        or not isinstance(protocol.get("claim_gates"), dict)
        or not isinstance(protocol.get("analysis"), dict)
        or not isinstance(protocol.get("pre_execution_blinding"), dict)
    ):
        raise FreezeError("committed V1 protocol is unsupported")
    return protocol


def build_acquisition_protocol(
    *,
    v1: Mapping[str, Any],
    frozen_at: str,
    acquisition_frozen_at: str,
    product_inputs: Mapping[str, Any],
    verifier_pins: Mapping[str, Any],
    exposure_ledger_sha256: str | None = None,
) -> dict[str, Any]:
    """Derive the complete fixed V2 acquisition design from the V1 contract."""
    protocol = deepcopy(dict(v1))
    universe = protocol.get("universe")
    claim_gates = protocol.get("claim_gates")
    analysis = protocol.get("analysis")
    blinding = protocol.get("pre_execution_blinding")
    if (
        protocol.get("schema_version") != 1
        or protocol.get("protocol_id") != "production-graph-holdout-v1"
        or not isinstance(universe, dict)
        or not isinstance(claim_gates, dict)
        or not isinstance(analysis, dict)
        or not isinstance(blinding, dict)
    ):
        raise FreezeError("committed V1 protocol is unsupported")
    dataset_revision = str(universe.get("revision") or "")
    if REVISION.fullmatch(dataset_revision) is None:
        raise FreezeError("committed V1 dataset revision is invalid")

    protocol["schema_version"] = 2
    protocol["protocol_id"] = PROTOCOL_ID
    protocol["protocol_generation"] = PROTOCOL_GENERATION
    protocol["stage"] = "acquisition-frozen"
    protocol["frozen_at"] = frozen_at
    protocol["acquisition_frozen_at"] = acquisition_frozen_at
    if exposure_ledger_sha256 is not None:
        protocol["exposure_ledger_sha256"] = _require_sha256(
            exposure_ledger_sha256,
            label="exposure ledger identity",
        )
    protocol.pop("execution_frozen_at", None)
    protocol.pop("canary_policy", None)
    protocol["product_inputs"] = dict(product_inputs)
    protocol["selection_seed"] = _sha256(
        SEED_PREFIX
        + str(PROTOCOL_GENERATION).encode("ascii")
        + b"\0"
        + dataset_revision.encode("ascii")
    )
    protocol["selection_seed_input"] = (
        "fixed literal ctx-holdout-selection-v2 NUL decimal protocol generation "
        "NUL external dataset revision"
    )
    protocol["candidate_partition_seed"] = _sha256(
        CANDIDATE_PARTITION_PREFIX + dataset_revision.encode("ascii")
    )
    protocol["candidate_partition_seed_input"] = (
        "fixed literal ctx-holdout-candidate-partition-v2 NUL external dataset revision"
    )
    protocol["selection"] = {
        "analysis_repositories": REPOSITORY_COUNT,
        "analysis_scenarios": REPOSITORY_COUNT,
        "candidate_slot": PROTOCOL_GENERATION - 1,
        "ctx_context": [],
        "eligible_candidates_per_repository_required": PROTOCOL_GENERATION,
        "eligible_repositories_required": REPOSITORY_COUNT,
        "first_scenario_rule": (
            "candidate at the zero-based candidate_slot from the stable candidate-partition "
            "ranking for each of the first ten generation-ranked eligible repositories"
        ),
        "private_canary": False,
        "query": "first 240 characters of whitespace-normalized problem_statement",
        "replacement_after_control_failure": "forbidden",
        "strategy": "one-per-repository",
        "task": "exact problem_statement bytes from the frozen dataset row",
    }

    fixed_claim_gates = deepcopy(claim_gates)
    fixed_claim_gates.update(
        {
            "paired_trials_per_scenario": TRIALS_PER_SCENARIO,
            "minimum_repositories_with_verified_delivery": REPOSITORY_COUNT,
            "required_benefiting_repositories": 9,
        }
    )
    protocol["claim_gates"] = fixed_claim_gates
    fixed_analysis = deepcopy(analysis)
    fixed_analysis.update(
        {
            "overall_token_effect": (
                "equal-weight median of the ten repository uncached-provider-token effects"
            ),
            "overall_time_effect": (
                "equal-weight median of the ten repository development-seconds effects"
            ),
            "support_test": "exact one-sided sign test across ten repository effects",
            "delivery": "at least one trusted verified CTX delivery in every repository",
        }
    )
    protocol["analysis"] = fixed_analysis
    protocol["control_requirements"] = [
        item.replace("all seven selected scenarios", "all ten selected scenarios")
        for item in protocol.get("control_requirements", [])
        if isinstance(item, str)
    ]
    protocol["freeze_manifest_requirements"] = [
        item.replace("private seven-scenario pack", "private ten-scenario pack")
        .replace("all seven selected test modules", "all ten selected test modules")
        .replace("all seven scenarios", "all ten scenarios")
        for item in protocol.get("freeze_manifest_requirements", [])
        if isinstance(item, str)
    ]
    fixed_blinding = deepcopy(blinding)
    for field in ("allowed_before_freeze", "forbidden_before_freeze"):
        value = fixed_blinding.get(field)
        if isinstance(value, str):
            fixed_blinding[field] = value.replace(" or canary", "").replace(
                "selected or canary",
                "selected",
            )
    protocol["pre_execution_blinding"] = fixed_blinding
    protocol["official_swebench_verifier"] = dict(verifier_pins)
    protocol["execution_inputs"] = {key: None for key in sorted(ACQUISITION_EXECUTION_INPUT_KEYS)}
    return protocol


def _read_regular_bytes(path: Path, *, label: str, private: bool) -> bytes:
    try:
        resolved = path.resolve(strict=False)
        if path.is_symlink():
            raise FreezeError(f"{label} must be an owner-only single-link regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, FreezeError):
            raise
        raise FreezeError(f"{label} must be an owner-only single-link regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (private and stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO))
        ):
            raise FreezeError(f"{label} must be an owner-only single-link regular file")
        if private:
            private_root = PRIVATE_ROOT.resolve()
            if ROOT.resolve() in resolved.parents and private_root not in resolved.parents:
                raise FreezeError(f"{label} inside the repository must use the private root")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _paths_are_distinct(paths: Mapping[str, Path]) -> None:
    entries = list(paths.items())
    for index, (left_label, left) in enumerate(entries):
        for right_label, right in entries[index + 1 :]:
            if left.resolve(strict=False) == right.resolve(strict=False):
                raise FreezeError(f"{left_label} and {right_label} must be distinct")
            if left.exists() and right.exists() and os.path.samefile(left, right):
                raise FreezeError(f"{left_label} and {right_label} must be distinct")


def _regular_file_sha256(path: Path, *, label: str, private: bool) -> str:
    try:
        resolved = path.resolve(strict=False)
        if path.is_symlink():
            raise FreezeError(f"{label} must be an owner-only single-link regular file")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, FreezeError):
            raise
        raise FreezeError(f"{label} must be an owner-only single-link regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (private and stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO))
        ):
            raise FreezeError(f"{label} must be an owner-only single-link regular file")
        if private:
            private_root = PRIVATE_ROOT.resolve()
            if ROOT.resolve() in resolved.parents and private_root not in resolved.parents:
                raise FreezeError(f"{label} inside the repository must use the private root")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_source_map(source_map_path: Path) -> tuple[dict[str, SourceBundle], str]:
    """Authenticate the canonical private map and each base-closure bundle."""
    source_map_bytes = _read_regular_bytes(
        source_map_path,
        label="private source map",
        private=True,
    )
    document = _json_object(source_map_bytes, label="private source map")
    if source_map_bytes != _canonical_bytes(document):
        raise FreezeError("private source map must use canonical JSON bytes")
    _exact_keys(document, SOURCE_MAP_KEYS, label="private source map")
    repositories = document.get("repositories")
    if document.get("schema_version") != 1 or not isinstance(repositories, dict):
        raise FreezeError("private source map has an unsupported shape")

    try:
        lexical_root = source_map_path.parent
        source_root = lexical_root.resolve(strict=True)
    except OSError as exc:
        raise FreezeError("private source map root is unavailable") from exc
    bundles: dict[str, SourceBundle] = {}
    resolved_paths: set[Path] = set()
    for canonical_url, raw_entry in repositories.items():
        if (
            not isinstance(canonical_url, str)
            or benchmark.GITHUB_REPO_URL.fullmatch(canonical_url) is None
            or not isinstance(raw_entry, dict)
        ):
            raise FreezeError("private source map repository entry is invalid")
        _exact_keys(
            raw_entry,
            SOURCE_MAP_REPOSITORY_KEYS,
            label="private source map repository entry",
        )
        base_commit = str(raw_entry.get("base_commit") or "")
        tree_sha1 = str(raw_entry.get("tree_sha1") or "")
        bundle_sha256 = _require_sha256(
            raw_entry.get("bundle_sha256"),
            label="private source bundle identity",
        )
        if REVISION.fullmatch(base_commit) is None or REVISION.fullmatch(tree_sha1) is None:
            raise FreezeError("private source map repository identity is invalid")

        raw_bundle_path = raw_entry.get("bundle_path")
        if not isinstance(raw_bundle_path, str) or "\\" in raw_bundle_path:
            raise FreezeError("source bundle path must be a normalized relative POSIX path")
        relative = PurePosixPath(raw_bundle_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.as_posix() != raw_bundle_path
        ):
            raise FreezeError("source bundle path must be a normalized relative POSIX path")
        candidate = lexical_root.joinpath(*relative.parts)
        cursor = lexical_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise FreezeError("private source bundle path must not traverse a symlink")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(source_root)
        except (OSError, ValueError) as exc:
            raise FreezeError("private source bundle escaped its source-map root") from exc
        if resolved == source_map_path.resolve(strict=True) or resolved in resolved_paths:
            raise FreezeError("private source bundle paths must be distinct")
        observed_sha256 = _regular_file_sha256(
            resolved,
            label="private source bundle",
            private=True,
        )
        if not secrets.compare_digest(observed_sha256, bundle_sha256):
            raise FreezeError("private source bundle identity changed")
        resolved_paths.add(resolved)
        bundles[canonical_url] = SourceBundle(
            base_commit=base_commit,
            bundle_path=resolved,
            bundle_sha256=bundle_sha256,
            tree_sha1=tree_sha1,
        )
    return bundles, _sha256(source_map_bytes)


def _validate_source_bundle_closure(source: SourceBundle) -> None:
    """Prove one authenticated bundle contains only the pinned base closure."""
    expected_head = f"{source.base_commit} refs/heads/base"
    try:
        listed_heads = benchmark._checked_git(
            ["bundle", "list-heads", str(source.bundle_path)],
            cwd=source.bundle_path.parent,
            label="private source bundle head inventory",
        ).splitlines()
        if listed_heads != [expected_head]:
            raise FreezeError("private source bundle is not an exact base-commit closure")

        with tempfile.TemporaryDirectory(
            prefix=".freeze-source-",
            dir=source.bundle_path.parent,
        ) as raw_root:
            workspace = Path(raw_root) / "repository"
            benchmark._checked_git(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--no-hardlinks",
                    str(source.bundle_path),
                    str(workspace),
                ],
                cwd=Path(raw_root),
                label="private source bundle clone",
                timeout=1800,
            )
            benchmark._checked_git(
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "checkout",
                    "--quiet",
                    "--detach",
                    source.base_commit,
                ],
                cwd=workspace,
                label="private source bundle checkout",
            )
            head = benchmark._checked_git(
                ["rev-parse", "HEAD"],
                cwd=workspace,
                label="private source bundle commit",
            )
            tree = benchmark._checked_git(
                ["rev-parse", "HEAD^{tree}"],
                cwd=workspace,
                label="private source bundle tree",
            )
            future = benchmark._checked_git(
                ["rev-list", "--all", "--not", source.base_commit],
                cwd=workspace,
                label="private source bundle future-history audit",
            )
            unreachable = benchmark._checked_git(
                ["fsck", "--full", "--strict", "--unreachable", "--no-reflogs"],
                cwd=workspace,
                label="private source bundle object audit",
                timeout=1800,
            )
            benchmark._checked_git(
                ["remote", "remove", "origin"],
                cwd=workspace,
                label="private source bundle remote removal",
            )
            remotes = benchmark._checked_git(
                ["remote"],
                cwd=workspace,
                label="private source bundle remote audit",
            )
            status = benchmark._checked_git(
                ["status", "--porcelain=v1", "--untracked-files=all"],
                cwd=workspace,
                label="private source bundle clean-tree audit",
            )
            if (
                head != source.base_commit
                or tree != source.tree_sha1
                or future
                or unreachable
                or remotes
                or status
            ):
                raise FreezeError("private source bundle is not an exact base-commit closure")
    except FreezeError:
        raise
    except RuntimeError as exc:
        raise FreezeError("private source bundle closure validation failed") from exc


def validate_acquisition_protocol(
    protocol: dict[str, Any],
    *,
    benchmark_script_path: Path | None = None,
    catalog_archive_path: Path | None = None,
    runtime_availability_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the protocol contract shared by materialization and freezing."""
    execution_inputs = protocol.get("execution_inputs")
    product_inputs = protocol.get("product_inputs")
    universe = protocol.get("universe")
    pins = protocol.get("official_swebench_verifier")
    exposure_ledger_sha256 = protocol.get("exposure_ledger_sha256")
    if (
        protocol.get("schema_version") != 2
        or protocol.get("protocol_id") != PROTOCOL_ID
        or protocol.get("stage") != "acquisition-frozen"
        or SHA256.fullmatch(str(exposure_ledger_sha256 or "")) is None
        or not isinstance(execution_inputs, dict)
        or set(execution_inputs) != ACQUISITION_EXECUTION_INPUT_KEYS
        or any(value is not None for value in execution_inputs.values())
        or not isinstance(product_inputs, dict)
        or set(product_inputs) != PRODUCT_INPUT_KEYS
        or not isinstance(universe, dict)
        or not isinstance(pins, dict)
    ):
        raise FreezeError("protocol is not a fresh supported V2 acquisition freeze")
    frozen_at = _canonical_timestamp(protocol.get("frozen_at"), label="protocol frozen_at")
    acquisition_frozen_at = _canonical_timestamp(
        protocol.get("acquisition_frozen_at"),
        label="protocol acquisition_frozen_at",
    )
    _exact_keys(pins, VERIFIER_PIN_KEYS, label="official verifier pins")
    if (
        pins.get("schema_version") != 1
        or pins.get("namespace") != "swebench"
        or REVISION.fullmatch(str(pins.get("revision") or "")) is None
    ):
        raise FreezeError("official verifier pins are invalid")
    for field in VERIFIER_PIN_KEYS:
        if field.endswith("_sha256"):
            _require_sha256(pins.get(field), label=f"official verifier {field}")
    _require_text(pins.get("docker_daemon_id"), label="Docker daemon identity", maximum=200)
    _require_text(pins.get("docker_server_version"), label="Docker server version", maximum=100)
    product_files = {
        "benchmark_script_sha256": benchmark_script_path or Path(benchmark.__file__),
        "catalog_archive_sha256": catalog_archive_path or benchmark.PRODUCTION_CATALOG_ARCHIVE,
        "runtime_availability_sha256": (
            runtime_availability_path or benchmark.PRODUCTION_RUNTIME_AVAILABILITY
        ),
    }
    for field, path in product_files.items():
        expected = _require_sha256(product_inputs.get(field), label=f"product {field}")
        try:
            observed = _sha256(path.read_bytes())
        except OSError as exc:
            raise FreezeError(f"frozen product input is unavailable: {field}") from exc
        if observed != expected:
            raise FreezeError(f"frozen product input changed: {field}")
    if REVISION.fullmatch(str(product_inputs.get("revision") or "")) is None:
        raise FreezeError("product revision is invalid")
    origin_url = str(product_inputs.get("origin_url") or "")
    origin_main_revision = str(product_inputs.get("origin_main_revision") or "")
    if (
        benchmark.GITHUB_REPO_URL.fullmatch(origin_url) is None
        or REVISION.fullmatch(origin_main_revision) is None
        or origin_main_revision != product_inputs.get("revision")
    ):
        raise FreezeError("product origin/main identity is invalid")
    _require_sha256(
        product_inputs.get("codex_binary_sha256"),
        label="product Codex binary identity",
    )
    _require_sha256(
        product_inputs.get("provider_config_sha256"),
        label="product provider configuration identity",
    )
    _require_sha256(
        universe.get("selection_jsonl_sha256"),
        label="frozen dataset identity",
    )
    expected_protocol = build_acquisition_protocol(
        v1=_supported_v1_protocol(),
        frozen_at=frozen_at,
        acquisition_frozen_at=acquisition_frozen_at,
        product_inputs=product_inputs,
        verifier_pins=pins,
        exposure_ledger_sha256=str(exposure_ledger_sha256),
    )
    if _canonical_bytes(protocol) != _canonical_bytes(expected_protocol):
        raise FreezeError("protocol fixed V2 acquisition design drifted")
    return dict(pins)


def _validated_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    return validate_acquisition_protocol(protocol)


def _validated_selection(
    selection: dict[str, Any],
    protocol: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    _exact_keys(selection, SELECTION_KEYS, label="selection")
    try:
        selected_ids, repository_map = holdout._validated_selection(selection, protocol)
    except (KeyError, TypeError, ValueError) as exc:
        raise FreezeError("selection is invalid") from exc
    if (
        len(selected_ids) != 10
        or len(repository_map) != 10
        or len(set(repository_map.values())) != 10
    ):
        raise FreezeError("V2 selection must contain ten tasks from ten repositories")
    return selected_ids, repository_map


def _validated_scenarios(
    scenario_pack: dict[str, Any],
    *,
    selected_ids: list[str],
    repository_map: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    _exact_keys(scenario_pack, frozenset({"scenarios", "version"}), label="scenario pack")
    rows = scenario_pack.get("scenarios")
    if (
        scenario_pack.get("version") != 1
        or not isinstance(rows, list)
        or len(rows) != 10
        or not all(isinstance(row, dict) for row in rows)
    ):
        raise FreezeError("scenario pack is invalid")
    scenario_rows = list(rows)
    if [row.get("id") for row in scenario_rows] != selected_ids:
        raise FreezeError("scenario pack order or identities do not match the selection")
    try:
        loaded = benchmark._load_scenarios_document(scenario_pack)
    except (KeyError, TypeError, ValueError) as exc:
        raise FreezeError("scenario pack is not executable by the benchmark runner") from exc
    if [scenario.id for scenario in loaded] != selected_ids:
        raise FreezeError("runner scenario identities do not match the selection")
    hashes: dict[str, str] = {}
    for row in scenario_rows:
        _exact_keys(row, SCENARIO_KEYS, label="scenario row")
        scenario_id = str(row["id"])
        allowed = row.get("allowed_changes")
        regression = row.get("regression_verify")
        expected_count = row.get("expected_test_count")
        if (
            row.get("repo_url") != repository_map[scenario_id]
            or REVISION.fullmatch(str(row.get("commit") or "")) is None
            or row.get("benchmark_class") != "historical"
            or row.get("language") != "python"
            or row.get("ctx_context") != []
            or not isinstance(expected_count, int)
            or isinstance(expected_count, bool)
            or expected_count < 1
            or not isinstance(allowed, list)
            or not allowed
            or not all(isinstance(path, str) and path for path in allowed)
            or not isinstance(row.get("verify"), list)
            or not row["verify"]
            or not isinstance(regression, list)
            or not regression
            or not all(isinstance(command, list) and command for command in regression)
        ):
            raise FreezeError(f"{scenario_id}: scenario values are unsupported")
        for field in (
            "query",
            "red_failure_contains",
            "reference_patch",
            "task",
            "test_body",
            "test_path",
        ):
            _require_text(row.get(field), label=f"{scenario_id}.{field}", maximum=100_000)
        _require_sha256(
            row.get("reconstructed_test_sha256"),
            label=f"{scenario_id}.reconstructed_test_sha256",
        )
        hashes[scenario_id] = _sha256(_canonical_bytes(row))
    return scenario_rows, hashes


def _validate_collision(
    collision: dict[str, Any],
    *,
    protocol: dict[str, Any],
    selected_ids: list[str],
    scenario_pack_sha256: str,
) -> None:
    _exact_keys(collision, COLLISION_KEYS, label="collision attestation")
    product_inputs = protocol["product_inputs"]
    if (
        collision.get("guard") != "runtime-pack-distinctive-evidence-v1"
        or collision.get("runtime_availability_sha256")
        != product_inputs["runtime_availability_sha256"]
        or collision.get("catalog_archive_sha256") != product_inputs["catalog_archive_sha256"]
        or collision.get("scenarios_sha256") != scenario_pack_sha256
        or collision.get("collision_free") is not True
        or collision.get("collision_count") != 0
        or isinstance(collision.get("collision_count"), bool)
        or collision.get("scenario_ids") != sorted(selected_ids)
    ):
        raise FreezeError("collision attestation is stale or invalid")


def _validate_reconstructed(
    reconstructed: dict[str, Any],
    *,
    selected_ids: list[str],
    selection_sha256: str,
    scenario_test_sha256: Mapping[str, str],
) -> None:
    _exact_keys(reconstructed, RECONSTRUCTED_KEYS, label="reconstructed test attestation")
    module_sha256 = reconstructed.get("module_sha256")
    if (
        reconstructed.get("guard") != "reconstructed-test-dependency-v1"
        or reconstructed.get("selection_sha256") != selection_sha256
        or not isinstance(module_sha256, dict)
        or set(module_sha256) != set(selected_ids)
        or module_sha256 != scenario_test_sha256
    ):
        raise FreezeError("reconstructed test attestation is stale or invalid")
    for scenario_id, digest in module_sha256.items():
        _require_sha256(digest, label=f"{scenario_id} reconstructed test identity")


def _validate_phase(
    phase: dict[str, Any],
    *,
    expected_phase: str,
    image_id: str,
) -> None:
    _exact_keys(phase, PHASE_KEYS, label=f"official {expected_phase} phase")
    fail_count = phase.get("fail_to_pass_count")
    pass_count = phase.get("pass_to_pass_count")
    status_counts = phase.get("status_counts")
    if (
        phase.get("phase") != expected_phase
        or phase.get("image_id") != image_id
        or phase.get("exact_selector_identity") is not True
        or not isinstance(fail_count, int)
        or isinstance(fail_count, bool)
        or fail_count < 1
        or not isinstance(pass_count, int)
        or isinstance(pass_count, bool)
        or pass_count < 0
        or not isinstance(status_counts, dict)
        or not status_counts
        or not all(
            isinstance(key, str)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
            for key, value in status_counts.items()
        )
        or sum(status_counts.values()) != fail_count + pass_count
    ):
        raise FreezeError(f"official {expected_phase} phase is invalid")
    if expected_phase == "red":
        if sum(int(status_counts.get(key, 0)) for key in ("FAILED", "ERROR")) < 1:
            raise FreezeError("official red phase did not preserve a red result")
    elif status_counts != {"PASSED": fail_count + pass_count}:
        raise FreezeError("official green phase did not fully resolve")
    for field in (
        "artifact_manifest_sha256",
        "runtime_identity_sha256",
        "verifier_evidence_sha256",
    ):
        _require_sha256(phase.get(field), label=f"official {expected_phase} {field}")
    for field in ("artifact_bytes", "artifact_count", "container_policy_count"):
        value = phase.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise FreezeError(f"official {expected_phase} {field} is invalid")


def _validate_verifier_binding(
    row: Mapping[str, Any],
    *,
    protocol: Mapping[str, Any],
    pins: Mapping[str, Any],
    image_id: str,
    runtime_identity_sha256: str,
) -> None:
    scenario_id = str(row["id"])
    binding = row.get("official_verifier_binding")
    if not isinstance(binding, dict):
        raise FreezeError(f"{scenario_id}: official verifier binding is missing")
    _exact_keys(binding, VERIFIER_BINDING_KEYS, label=f"{scenario_id} verifier binding")
    verify = row.get("verify")
    regression = row.get("regression_verify")
    if (
        not isinstance(verify, list)
        or verify[:4] != ["{python}", "-m", "pytest", "-q"]
        or len(verify) < 5
        or not isinstance(regression, list)
        or len(regression) != 1
        or not isinstance(regression[0], list)
        or regression[0][:4] != ["{python}", "-m", "pytest", "-q"]
        or len(regression[0]) < 5
    ):
        raise FreezeError(f"{scenario_id}: verifier selectors are unsupported")
    expected = {
        "allowed_paths_sha256": _sha256(_canonical_bytes(row["allowed_changes"])),
        "base_commit": row["commit"],
        "bridge_sha256": pins["bridge_sha256"],
        "dataset_sha256": protocol["universe"]["selection_jsonl_sha256"],
        "docker_cli_sha256": pins["docker_cli_sha256"],
        "docker_daemon_id_sha256": _sha256(str(pins["docker_daemon_id"]).encode()),
        "docker_package_sha256": pins["docker_package_sha256"],
        "docker_server_version": pins["docker_server_version"],
        "fail_to_pass_sha256": _sha256(_canonical_bytes(verify[4:])),
        "harness_revision": pins["revision"],
        "image_content_digest": image_id,
        "pass_to_pass_sha256": _sha256(_canonical_bytes(regression[0][4:])),
        "python_environment_sha256": pins["python_environment_sha256"],
        "python_sha256": pins["python_sha256"],
        "repository_url": row["repo_url"],
        "run_evaluation_sha256": pins["run_evaluation_sha256"],
        "runtime_identity_sha256": runtime_identity_sha256,
        "schema_version": 1,
    }
    if any(binding.get(field) != value for field, value in expected.items()):
        raise FreezeError(f"{scenario_id}: official verifier binding drifted")
    for field in ("dataset_row_sha256", "harness_source_sha256"):
        _require_sha256(binding.get(field), label=f"{scenario_id} binding {field}")
    if REVISION.fullmatch(str(binding.get("repository_tree_sha1") or "")) is None:
        raise FreezeError(f"{scenario_id}: repository tree identity is invalid")


def _validate_controls(
    controls: dict[str, Any],
    *,
    protocol: dict[str, Any],
    pins: Mapping[str, Any],
    selected_ids: list[str],
    scenario_pack_sha256: str,
    selection_sha256: str,
    scenario_test_sha256: Mapping[str, str],
    scenario_rows: Mapping[str, Mapping[str, Any]],
) -> None:
    _exact_keys(controls, MATERIALIZATION_CONTROL_KEYS, label="materialization controls")
    results = controls.get("scenario_results")
    pins_sha256 = _sha256(_canonical_bytes(pins))
    if (
        controls.get("guard") != "holdout-control-results-v1"
        or controls.get("all_scenarios_passed") is not True
        or controls.get("scenario_count") != 10
        or controls.get("selection_sha256") != selection_sha256
        or controls.get("scenario_pack_sha256") != scenario_pack_sha256
        or controls.get("verifier_pins_sha256") != pins_sha256
        or not isinstance(results, dict)
        or set(results) != set(selected_ids)
    ):
        raise FreezeError("materialization controls are stale or invalid")
    timeout = protocol["timeouts"]["control_verification_seconds"]
    for scenario_id in selected_ids:
        result = results[scenario_id]
        if not isinstance(result, dict):
            raise FreezeError(f"{scenario_id}: materialization control is invalid")
        _exact_keys(result, SCENARIO_CONTROL_KEYS, label=f"{scenario_id} control")
        official = result.get("official_swebench")
        elapsed = result.get("elapsed_seconds")
        if (
            result.get("parent_with_test_patch_red") is not True
            or result.get("reference_patch_green") is not True
            or result.get("changed_test_module_green") is not True
            or result.get("timeout_compliant") is not True
            or result.get("timeout_seconds") != timeout
            or not isinstance(elapsed, int | float)
            or isinstance(elapsed, bool)
            or not math.isfinite(float(elapsed))
            or not 0 <= float(elapsed) <= float(timeout)
            or result.get("reconstructed_test_sha256") != scenario_test_sha256[scenario_id]
            or not isinstance(official, dict)
        ):
            raise FreezeError(f"{scenario_id}: materialization control values are invalid")
        for field in (
            "green_evidence_sha256",
            "module_evidence_sha256",
            "red_evidence_sha256",
        ):
            _require_sha256(result.get(field), label=f"{scenario_id} {field}")
        _exact_keys(official, OFFICIAL_CONTROL_KEYS, label=f"{scenario_id} official control")
        image_id = str(official.get("image_id") or "")
        if IMAGE_ID.fullmatch(image_id) is None or official.get("pins_sha256") != pins_sha256:
            raise FreezeError(f"{scenario_id}: official control identity is invalid")
        red = official.get("red")
        green = official.get("green")
        if not isinstance(red, dict) or not isinstance(green, dict):
            raise FreezeError(f"{scenario_id}: official phases are missing")
        _validate_phase(red, expected_phase="red", image_id=image_id)
        _validate_phase(green, expected_phase="green", image_id=image_id)
        if (
            result["red_evidence_sha256"] != red["verifier_evidence_sha256"]
            or result["green_evidence_sha256"] != green["verifier_evidence_sha256"]
            or result["module_evidence_sha256"] != green["artifact_manifest_sha256"]
        ):
            raise FreezeError(f"{scenario_id}: official evidence identity drifted")
        if any(
            red[field] != green[field]
            for field in (
                "fail_to_pass_count",
                "image_id",
                "pass_to_pass_count",
                "runtime_identity_sha256",
            )
        ):
            raise FreezeError(f"{scenario_id}: red/green verifier identity drifted")
        _validate_verifier_binding(
            scenario_rows[scenario_id],
            protocol=protocol,
            pins=pins,
            image_id=image_id,
            runtime_identity_sha256=str(red["runtime_identity_sha256"]),
        )


def _validate_environment(
    environment: dict[str, Any],
    *,
    protocol: dict[str, Any],
    pins: Mapping[str, Any],
) -> None:
    _exact_keys(environment, ENVIRONMENT_KEYS, label="execution environment")
    limits = environment.get("limits")
    evaluator = environment.get("evaluator")
    codex = environment.get("codex")
    python = environment.get("python")
    if (
        environment.get("schema_version") != 1
        or environment.get("protocol_id") != protocol["protocol_id"]
        or environment.get("product_revision") != protocol["product_inputs"]["revision"]
        or not isinstance(limits, dict)
        or not isinstance(evaluator, dict)
        or not isinstance(codex, dict)
        or not isinstance(python, dict)
    ):
        raise FreezeError("execution environment identity is invalid")
    _exact_keys(limits, LIMIT_KEYS, label="execution limits")
    _exact_keys(evaluator, frozenset({"backend", "pins_sha256"}), label="evaluator identity")
    _exact_keys(
        codex,
        frozenset({"runtime_contract", "version"}),
        label="Codex identity",
    )
    _exact_keys(
        python,
        frozenset({"dependencies_sha256", "executable_sha256", "version"}),
        label="Python identity",
    )
    timeout = limits.get("agent_timeout_seconds")
    if (
        evaluator.get("backend") != benchmark.OFFICIAL_HOLDOUT_BACKEND
        or evaluator.get("pins_sha256") != _sha256(_canonical_bytes(pins))
        or limits.get("trials_per_scenario") != 3
        or limits.get("retries") != 0
        or limits.get("arms") != list(ARMS)
        or limits.get("catalog_cache_hit") is not False
        or limits.get("task_count") != 10
        or limits.get("pair_count") != 30
        or limits.get("measured_concurrency") != 1
        or limits.get("sandbox_contract") != benchmark.OFFICIAL_SANDBOX_CONTRACT
        or not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or not 0 < float(timeout) <= 3600
    ):
        raise FreezeError("execution environment values are unsupported")
    try:
        normalized_runtime_contract = benchmark.normalize_codex_runtime_contract(
            codex.get("runtime_contract")
        )
    except ValueError as exc:
        raise FreezeError("Codex runtime contract is invalid") from exc
    if codex.get("runtime_contract") != normalized_runtime_contract:
        raise FreezeError("Codex runtime contract is not normalized")
    _require_text(environment.get("model"), label="model")
    provider = _require_text(environment.get("provider"), label="provider")
    if provider != "openai":
        raise FreezeError("official execution requires the OpenAI provider")
    if protocol["product_inputs"][
        "provider_config_sha256"
    ] != benchmark.codex_provider_config_sha256(provider):
        raise FreezeError("provider configuration does not match the product freeze")
    _require_text(codex.get("version"), label="Codex version")
    _require_text(python.get("version"), label="Python version")
    _require_sha256(python.get("dependencies_sha256"), label="Python dependency identity")
    _require_sha256(python.get("executable_sha256"), label="Python executable identity")


def build_execution_schedule(
    selection: dict[str, Any],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    """Build the deterministic globally counterbalanced V2 pair schedule."""
    selected_ids, repository_map = _validated_selection(selection, protocol)
    trials = protocol.get("claim_gates", {}).get("paired_trials_per_scenario")
    if trials != 3 or len(repository_map) != 10:
        raise FreezeError("V2 execution requires ten repositories and three pairs per task")
    assignments: list[dict[str, Any]] = []
    for trial in range(1, trials + 1):
        for index, scenario_id in enumerate(selected_ids):
            arms = ARMS if (index + trial - 1) % 2 == 0 else tuple(reversed(ARMS))
            assignments.append(
                {
                    "arms": list(arms),
                    "scenario": scenario_id,
                    "trial": trial,
                }
            )
    if (
        len(assignments) != 30
        or len({(row["scenario"], row["trial"]) for row in assignments}) != 30
        or sum(row["arms"][0] == "baseline" for row in assignments) != 15
        or sum(row["arms"][0] == "ctx-light" for row in assignments) != 15
    ):
        raise FreezeError("execution schedule is not a complete global 15/15 assignment")
    return {
        "assignment_count": 30,
        "assignments": assignments,
        "baseline_first_count": 15,
        "ctx_light_first_count": 15,
        "protocol_id": protocol["protocol_id"],
        "schema_version": 1,
        "trials_per_scenario": trials,
    }


def validate_execution_schedule(
    schedule: dict[str, Any],
    selection: dict[str, Any],
    protocol: dict[str, Any],
) -> None:
    if schedule != build_execution_schedule(selection, protocol):
        raise FreezeError("execution schedule does not match the frozen assignment")


def _ensure_output_parent(path: Path, *, private: bool) -> None:
    try:
        path.parent.mkdir(mode=0o700 if private else 0o755, parents=True, exist_ok=True)
    except OSError as exc:
        raise FreezeError("output parent is unavailable") from exc
    if private and stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise FreezeError("private output parent must be owner-only")


def _stage_bytes(path: Path, data: bytes, *, mode: int) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary_path
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)
        raise


def _install_outputs(outputs: list[tuple[Path, bytes, int]]) -> None:
    staged: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for path, data, mode in outputs:
            staged.append((path, _stage_bytes(path, data, mode=mode)))
        for path, temporary in staged:
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise FreezeError(f"output already exists: {path}") from exc
            temporary.unlink()
            installed.append(path)
    except BaseException:
        for path in reversed(installed):
            path.unlink(missing_ok=True)
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)
        raise


def freeze_protocol(
    *,
    protocol_path: Path,
    exposure_ledger_path: Path,
    selection_path: Path,
    scenario_pack_path: Path,
    source_map_path: Path,
    collision_path: Path,
    reconstructed_path: Path,
    controls_path: Path,
    environment_path: Path,
    schedule_path: Path,
    output_path: Path,
    frozen_at: str,
    expected_acquisition_protocol_sha256: str,
) -> dict[str, str]:
    """Authenticate materialization evidence and emit an execution freeze."""
    all_paths = {
        "protocol": protocol_path,
        "exposure ledger": exposure_ledger_path,
        "selection": selection_path,
        "scenario pack": scenario_pack_path,
        "source map": source_map_path,
        "collision": collision_path,
        "reconstructed tests": reconstructed_path,
        "materialization controls": controls_path,
        "environment": environment_path,
        "schedule output": schedule_path,
        "protocol output": output_path,
    }
    _paths_are_distinct(all_paths)
    for label, path in (
        ("schedule output", schedule_path),
        ("protocol output", output_path),
    ):
        if path.exists() or path.is_symlink():
            raise FreezeError(f"{label} already exists")
    protocol_bytes = _read_regular_bytes(protocol_path, label="protocol", private=False)
    expected_acquisition_sha256 = _require_sha256(
        expected_acquisition_protocol_sha256,
        label="expected acquisition protocol identity",
    )
    observed_acquisition_sha256 = _sha256(protocol_bytes)
    if not secrets.compare_digest(
        observed_acquisition_sha256,
        expected_acquisition_sha256,
    ):
        raise FreezeError("acquisition protocol identity changed")
    try:
        parsed_time = datetime.fromisoformat(frozen_at)
    except ValueError as exc:
        raise FreezeError("execution freeze timestamp is invalid") from exc
    if parsed_time.tzinfo is None:
        raise FreezeError("execution freeze timestamp must include a timezone")

    protocol = _json_object(protocol_bytes, label="protocol")
    pins = _validated_protocol(protocol)
    if protocol_bytes != _canonical_bytes(protocol, newline=True):
        raise FreezeError("acquisition protocol must use canonical JSON bytes")
    input_paths = {
        "exposure_ledger": exposure_ledger_path,
        "selection": selection_path,
        "scenario_pack": scenario_pack_path,
        "source_map": source_map_path,
        "collision": collision_path,
        "reconstructed": reconstructed_path,
        "controls": controls_path,
        "environment": environment_path,
    }
    blobs = {
        name: _read_regular_bytes(path, label=name, private=True)
        for name, path in input_paths.items()
    }
    values = {name: _json_object(data, label=name) for name, data in blobs.items()}
    try:
        validated_exposure = exposure_ledger.validate_ledger_document(values["exposure_ledger"])
    except ValueError as exc:
        raise FreezeError("authenticated exposure ledger is invalid") from exc
    if not validated_exposure["instance_id_hmac_sha256"]:
        raise FreezeError("authenticated exposure ledger must not be empty")
    if blobs["exposure_ledger"] != exposure_ledger.canonical_ledger_bytes(
        validated_exposure
    ) or not secrets.compare_digest(
        _sha256(blobs["exposure_ledger"]),
        str(protocol["exposure_ledger_sha256"]),
    ):
        raise FreezeError("authenticated exposure ledger identity changed")
    selected_ids, repository_map = _validated_selection(values["selection"], protocol)
    try:
        holdout.require_exposure_disjoint_selection(
            values["selection"],
            validated_exposure,
        )
    except ValueError as exc:
        raise FreezeError("selection intersects authenticated historical exposure") from exc
    selection_canonical = _canonical_bytes(values["selection"])
    if blobs["selection"] != selection_canonical:
        raise FreezeError("selection must use canonical materializer JSON bytes")
    selection_sha256 = _sha256(selection_canonical)
    scenario_pack_sha256 = _sha256(blobs["scenario_pack"])
    scenario_rows, _ = _validated_scenarios(
        values["scenario_pack"],
        selected_ids=selected_ids,
        repository_map=repository_map,
    )
    source_bundles, source_map_sha256 = validate_source_map(source_map_path)
    if not secrets.compare_digest(source_map_sha256, _sha256(blobs["source_map"])):
        raise FreezeError("private source map changed during execution freeze")
    expected_sources = {
        str(row["repo_url"]): (
            str(row["commit"]),
            str(row["official_verifier_binding"]["repository_tree_sha1"]),
        )
        for row in scenario_rows
    }
    if set(source_bundles) != set(expected_sources):
        raise FreezeError("private source map does not match the selected repositories")
    validated_source_closures: set[tuple[str, str, str]] = set()
    for repository_url, (base_commit, tree_sha1) in expected_sources.items():
        source = source_bundles[repository_url]
        if source.base_commit != base_commit or source.tree_sha1 != tree_sha1:
            raise FreezeError("private source map repository identity is stale")
        closure_identity = (
            source.bundle_sha256,
            source.base_commit,
            source.tree_sha1,
        )
        if closure_identity not in validated_source_closures:
            _validate_source_bundle_closure(source)
            validated_source_closures.add(closure_identity)
    scenario_test_sha256 = {
        str(row["id"]): str(row["reconstructed_test_sha256"]) for row in scenario_rows
    }
    _validate_collision(
        values["collision"],
        protocol=protocol,
        selected_ids=selected_ids,
        scenario_pack_sha256=scenario_pack_sha256,
    )
    _validate_reconstructed(
        values["reconstructed"],
        selected_ids=selected_ids,
        selection_sha256=selection_sha256,
        scenario_test_sha256=scenario_test_sha256,
    )
    _validate_environment(
        values["environment"],
        protocol=protocol,
        pins=pins,
    )
    schedule = build_execution_schedule(values["selection"], protocol)
    schedule_bytes = _canonical_bytes(schedule, newline=True)
    schedule_sha256 = _sha256(schedule_bytes)
    _validate_controls(
        values["controls"],
        protocol=protocol,
        pins=pins,
        selected_ids=selected_ids,
        scenario_pack_sha256=scenario_pack_sha256,
        selection_sha256=selection_sha256,
        scenario_test_sha256=scenario_test_sha256,
        scenario_rows={str(row["id"]): row for row in scenario_rows},
    )
    execution_inputs: dict[str, str] = {
        "acquisition_protocol_sha256": observed_acquisition_sha256,
        "collision_attestation_sha256": _sha256(blobs["collision"]),
        "control_results_sha256": _sha256(blobs["controls"]),
        "execution_environment_sha256": _sha256(blobs["environment"]),
        "execution_schedule_sha256": schedule_sha256,
        "reconstructed_test_attestation_sha256": _sha256(blobs["reconstructed"]),
        "scenario_pack_sha256": scenario_pack_sha256,
        "selection_output_sha256": selection_sha256,
        "source_map_sha256": source_map_sha256,
    }
    frozen = deepcopy(protocol)
    frozen["stage"] = "execution-frozen"
    frozen["execution_frozen_at"] = parsed_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
    frozen["execution_inputs"] = execution_inputs
    protocol_bytes = (
        json.dumps(frozen, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()

    _ensure_output_parent(schedule_path, private=True)
    _ensure_output_parent(output_path, private=False)
    _install_outputs(
        [
            (schedule_path, schedule_bytes, 0o600),
            (output_path, protocol_bytes, 0o644),
        ]
    )
    return execution_inputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--exposure-ledger", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--scenario-pack", type=Path, required=True)
    parser.add_argument("--source-map", type=Path, required=True)
    parser.add_argument("--collision", type=Path, required=True)
    parser.add_argument("--reconstructed", type=Path, required=True)
    parser.add_argument("--controls", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--schedule", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--failure-evidence-output", type=Path, required=True)
    parser.add_argument(
        "--expected-acquisition-protocol-sha256",
        required=True,
    )
    parser.add_argument(
        "--frozen-at",
        default=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    args = parser.parse_args(argv)
    try:
        failure_evidence.validate_destination(
            args.failure_evidence_output,
            repository_root=ROOT,
        )
    except failure_evidence.FailureEvidenceError:
        parser.exit(2, "execution freeze precondition failed; evidence=unavailable\n")
    try:
        hashes = freeze_protocol(
            protocol_path=args.protocol,
            exposure_ledger_path=args.exposure_ledger,
            selection_path=args.selection,
            scenario_pack_path=args.scenario_pack,
            source_map_path=args.source_map,
            collision_path=args.collision,
            reconstructed_path=args.reconstructed,
            controls_path=args.controls,
            environment_path=args.environment,
            schedule_path=args.schedule,
            output_path=args.output,
            frozen_at=args.frozen_at,
            expected_acquisition_protocol_sha256=args.expected_acquisition_protocol_sha256,
        )
    except BaseException as exc:
        try:
            failure_evidence.publish_failure(
                destination=args.failure_evidence_output,
                operation="holdout-execution-freeze",
                exc=exc,
                repository_root=ROOT,
            )
            evidence_status = "preserved"
        except BaseException:
            evidence_status = "unavailable"
        parser.exit(
            2,
            f"execution freeze failed ({type(exc).__name__}); evidence={evidence_status}\n",
        )
    print(
        "execution-frozen "
        + " ".join(
            f"{key}={value}" for key, value in sorted(hashes.items()) if isinstance(value, str)
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
