#!/usr/bin/env python3
"""Deterministically filter and select a private CTX benchmark holdout."""

from __future__ import annotations

import argparse
import ast
import csv
from collections import defaultdict
import hashlib
import hmac
import json
import math
import os
import re
import stat
import statistics
from pathlib import Path, PurePosixPath
from typing import Any, TextIO

try:
    from scripts import ctx_ab_exposure_ledger as exposure_ledger
except ImportError:  # pragma: no cover - direct script execution
    import ctx_ab_exposure_ledger as exposure_ledger  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "benchmarks" / "ctx_ab" / "holdout-protocol-v1.json"
PRIVATE_ROOT = ROOT / ".gate" / "ctx-ab-private"
V2_CANDIDATE_PARTITION_PREFIX = b"ctx-holdout-candidate-partition-v2\0"
HISTORICAL_EXPOSURE_REJECTION_CODE = "historical-exposure"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DIFF_PATH = re.compile(r"^diff --git a/(.+) b/(.+)$")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@(?: .*)?$")
FORBIDDEN_TEST_IMPORT_ROOTS = {
    "aiohttp",
    "http",
    "httpx",
    "random",
    "requests",
    "socket",
    "subprocess",
    "urllib",
    "urllib3",
}
LEDGER_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "production_paths",
    "test_path",
    "production_changed_lines",
    "status",
    "rejection_code",
)


def _digest(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def canonical_repo_url(repo: str) -> str:
    normalized = repo.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", normalized):
        raise ValueError(f"invalid repository name: {repo!r}")
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise ValueError(f"invalid repository name: {repo!r}")
    return f"https://github.com/{normalized}.git"


def _is_canonical_repo_url(value: object) -> bool:
    if not isinstance(value, str) or not value.startswith("https://github.com/"):
        return False
    repo = value.removeprefix("https://github.com/").removesuffix(".git")
    try:
        return canonical_repo_url(repo) == value
    except ValueError:
        return False


def _parse_patch(patch: str) -> tuple[tuple[str, ...], int, str]:
    lines = patch.splitlines()
    paths: list[str] = []
    changed_lines = 0
    added_lines: list[str] = []
    index = 0
    while index < len(lines):
        match = DIFF_PATH.fullmatch(lines[index])
        if match is None:
            return (), -1, ""
        before, after = match.groups()
        if before != after:
            return (), -1, ""
        path = PurePosixPath(after)
        if path.is_absolute() or ".." in path.parts or path.as_posix() in paths:
            return (), -1, ""
        paths.append(path.as_posix())
        index += 1
        while index < len(lines) and not lines[index].startswith("--- "):
            if not lines[index].startswith(
                ("index ", "new file mode ", "deleted file mode ", "old mode ", "new mode ")
            ):
                return (), -1, ""
            index += 1
        if index + 1 >= len(lines):
            return (), -1, ""
        old_header = lines[index][4:]
        new_header = lines[index + 1][4:] if lines[index + 1].startswith("+++ ") else ""
        if (
            old_header not in {f"a/{after}", "/dev/null"}
            or new_header not in {f"b/{after}", "/dev/null"}
            or old_header == new_header == "/dev/null"
        ):
            return (), -1, ""
        index += 2
        saw_hunk = False
        while index < len(lines) and not lines[index].startswith("diff --git "):
            hunk = HUNK_HEADER.fullmatch(lines[index])
            if hunk is None:
                return (), -1, ""
            saw_hunk = True
            old_expected = int(hunk.group(1) or 1)
            new_expected = int(hunk.group(2) or 1)
            old_seen = 0
            new_seen = 0
            index += 1
            while (
                index < len(lines)
                and not lines[index].startswith("diff --git ")
                and not lines[index].startswith("@@ ")
            ):
                line = lines[index]
                if line == r"\ No newline at end of file":
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    return (), -1, ""
                if line[0] in " -":
                    old_seen += 1
                if line[0] in " +":
                    new_seen += 1
                if line[0] in "+-":
                    changed_lines += 1
                if line[0] == "+":
                    added_lines.append(line[1:])
                index += 1
            if old_seen != old_expected or new_seen != new_expected:
                return (), -1, ""
        if not saw_hunk:
            return (), -1, ""
    return tuple(paths), changed_lines, "\n".join(added_lines)


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return path.endswith(".py") and (
        pure.name.startswith("test_") or any(part in {"test", "tests"} for part in pure.parts)
    )


def _is_product_path(path: str, rules: dict[str, Any]) -> bool:
    pure = PurePosixPath(path)
    if not path.endswith(".py") or _is_test_path(path):
        return False
    if pure.name in rules["excluded_filenames"]:
        return False
    if any(re.search(pattern, pure.name) for pattern in rules["excluded_filename_regex"]):
        return False
    return not any(part in rules["excluded_path_components"] for part in pure.parts[:-1])


def evaluate_row(row: dict[str, Any], protocol: dict[str, Any]) -> dict[str, Any]:
    repo = str(row.get("repo") or "").strip().lower()
    instance_id = str(row.get("instance_id") or "").strip()
    base_commit = str(row.get("base_commit") or "").strip()
    patch = str(row.get("patch") or "")
    test_patch = str(row.get("test_patch") or "")
    problem_statement = str(row.get("problem_statement") or "")
    production_paths, changed_lines, _ = _parse_patch(patch)
    test_paths, _, added_test = _parse_patch(test_patch)
    rules = protocol["static_candidate_rules"]
    rejection = ""
    try:
        canonical_repo_url(repo)
    except ValueError:
        rejection = "row-schema"
    if not rejection and (not instance_id or not re.fullmatch(r"[A-Za-z0-9_.-]+", instance_id)):
        rejection = "row-schema"
    elif not rejection and repo in protocol["excluded_repositories"]:
        rejection = "excluded-repository"
    elif not rejection and not SHA1.fullmatch(base_commit):
        rejection = "base-commit"
    elif not rejection and (
        not 1 <= len(production_paths) <= 3
        or not all(_is_product_path(path, rules) for path in production_paths)
    ):
        rejection = "patch-paths"
    elif not rejection and (len(test_paths) != 1 or not _is_test_path(test_paths[0])):
        rejection = "test-paths"
    elif not rejection and not (
        rules["production_changed_lines"]["minimum"]
        <= changed_lines
        <= rules["production_changed_lines"]["maximum"]
    ):
        rejection = "patch-lines"
    elif not rejection and not (
        rules["problem_statement_words"]["minimum"]
        <= len(problem_statement.split())
        <= rules["problem_statement_words"]["maximum"]
    ):
        rejection = "problem-statement"
    elif not rejection and any(
        re.search(pattern, added_test) for pattern in rules["forbidden_test_regex"]
    ):
        rejection = "test-dependency"
    return {
        "instance_id": instance_id,
        "repo": repo,
        "base_commit": base_commit,
        "production_paths": "|".join(production_paths),
        "test_path": test_paths[0] if len(test_paths) == 1 else "",
        "production_changed_lines": changed_lines,
        "status": "eligible" if not rejection else "rejected",
        "rejection_code": rejection,
    }


def reject_historical_exposures(
    ledger: list[dict[str, Any]],
    exposure_document: dict[str, Any],
) -> list[dict[str, Any]]:
    validated = exposure_ledger.validate_ledger_document(exposure_document)
    exposed = set(validated["instance_id_hmac_sha256"])
    salt = str(validated["salt"])
    filtered: list[dict[str, Any]] = []
    for original in ledger:
        row = dict(original)
        if row.get("status") == "eligible":
            digest = exposure_ledger.instance_id_hmac_sha256(
                salt,
                str(row.get("instance_id") or ""),
            )
            if digest in exposed:
                row["status"] = "rejected"
                row["rejection_code"] = HISTORICAL_EXPOSURE_REJECTION_CODE
        filtered.append(row)
    return filtered


def require_exposure_disjoint_selection(
    selection: dict[str, Any],
    exposure_document: dict[str, Any],
) -> None:
    validated = exposure_ledger.validate_ledger_document(exposure_document)
    identities = selection.get("analysis_instance_ids")
    canary = selection.get("canary_instance_id")
    if not isinstance(identities, list):
        raise ValueError("selection is invalid")
    selected = list(identities)
    if canary is not None:
        selected.append(canary)
    if any(
        not isinstance(instance_id, str)
        or exposure_ledger.contains_instance_id(validated, instance_id)
        for instance_id in selected
    ):
        raise ValueError("selection intersects the authenticated exposure ledger")


def validate_reconstructed_test_module(source: str) -> None:
    """Reject external, nondeterministic, environment, and sleep dependencies."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValueError("reconstructed test module is not valid Python") from exc
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                local = imported.asname or imported.name.split(".", 1)[0]
                aliases[local] = (
                    imported.name if imported.asname else imported.name.split(".", 1)[0]
                )
                if imported.name.split(".", 1)[0] in FORBIDDEN_TEST_IMPORT_ROOTS:
                    raise ValueError("reconstructed test module has a forbidden import")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            if root in FORBIDDEN_TEST_IMPORT_ROOTS:
                raise ValueError("reconstructed test module has a forbidden import")
            for imported in node.names:
                if imported.name == "*" and root in {"builtins", "importlib", "os", "time"}:
                    raise ValueError("reconstructed test module has a forbidden wildcard import")
                local = imported.asname or imported.name
                aliases[local] = f"{node.module}.{imported.name}"
                if (node.module, imported.name) in {
                    ("os", "environ"),
                    ("os", "getenv"),
                    ("time", "sleep"),
                }:
                    raise ValueError("reconstructed test module has a forbidden dependency")

    def qualified_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = qualified_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and qualified_name(node) in {
            "os.environ",
            "os.getenv",
            "time.sleep",
        }:
            raise ValueError("reconstructed test module has a forbidden dependency")
        if not isinstance(node, ast.Call):
            continue
        name = qualified_name(node.func)
        if name.rsplit(".", 1)[-1] == "sleep":
            raise ValueError("reconstructed test module has a forbidden dependency")
        if name in {
            "__import__",
            "builtins.__import__",
            "importlib.import_module",
        }:
            if not node.args:
                raise ValueError("reconstructed test module has a forbidden dynamic import")
            argument = node.args[0]
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                raise ValueError("reconstructed test module has a forbidden dynamic import")
            root = argument.value.split(".", 1)[0]
            if root in FORBIDDEN_TEST_IMPORT_ROOTS | {"os", "time"}:
                raise ValueError("reconstructed test module has a forbidden import")


def select_rows(ledger: list[dict[str, Any]], protocol: dict[str, Any]) -> dict[str, Any]:
    seed = str(protocol["selection_seed"])
    selection_rules = protocol["selection"]
    strategy = selection_rules.get("strategy", "legacy")
    legacy_strategy = strategy is None or strategy == "legacy"
    private_canary = selection_rules.get("private_canary", legacy_strategy)
    required_repositories = int(selection_rules["eligible_repositories_required"])
    candidates_per_repository = int(selection_rules["eligible_candidates_per_repository_required"])
    analysis_repositories = int(selection_rules["analysis_repositories"])
    analysis_scenarios = int(selection_rules["analysis_scenarios"])
    candidate_seed = seed
    candidate_slot = 0
    if not isinstance(private_canary, bool):
        raise ValueError("selection.private_canary must be a boolean")
    if legacy_strategy:
        if (
            not private_canary
            or required_repositories != analysis_repositories + 1
            or analysis_scenarios != analysis_repositories + 1
        ):
            raise ValueError("holdout selection cardinalities are inconsistent")
    elif strategy == "one-per-repository":
        if "private_canary" not in selection_rules:
            raise ValueError("one-per-repository selection requires explicit private_canary")
        if protocol.get("schema_version") == 2:
            generation = protocol.get("protocol_generation")
            dataset_revision = str(protocol.get("universe", {}).get("revision") or "")
            candidate_seed = str(protocol.get("candidate_partition_seed") or "")
            candidate_slot_value = selection_rules.get("candidate_slot")
            expected_candidate_seed = (
                hashlib.sha256(
                    V2_CANDIDATE_PARTITION_PREFIX + dataset_revision.encode("ascii")
                ).hexdigest()
                if SHA1.fullmatch(dataset_revision)
                else ""
            )
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 1
                or not isinstance(candidate_slot_value, int)
                or isinstance(candidate_slot_value, bool)
                or candidate_slot_value != generation - 1
                or candidates_per_repository != generation
                or not SHA256.fullmatch(candidate_seed)
                or not hmac.compare_digest(candidate_seed, expected_candidate_seed)
            ):
                raise ValueError("V2 candidate partition contract is invalid")
            candidate_slot = candidate_slot_value
        expected_repositories = analysis_repositories + (1 if private_canary else 0)
        if (
            analysis_repositories < 1
            or analysis_scenarios != analysis_repositories
            or required_repositories != expected_repositories
            or candidates_per_repository < 1
        ):
            raise ValueError(
                "one-per-repository selection requires analysis_scenarios equal to "
                "analysis_repositories, eligible_repositories_required equal to "
                "analysis_repositories plus one when private_canary is true, and "
                "at least one eligible candidate per repository"
            )
    else:
        raise ValueError(f"unsupported holdout selection strategy: {strategy!r}")
    instance_ids = [str(row.get("instance_id") or "") for row in ledger]
    if len(instance_ids) != len(set(instance_ids)):
        raise ValueError("candidate ledger contains duplicate instance IDs")
    eligible_by_repo: dict[str, list[dict[str, Any]]] = {}
    for row in ledger:
        if row.get("status") == "eligible":
            eligible_by_repo.setdefault(str(row["repo"]), []).append(row)
    ranked_repositories = sorted(
        (
            (_digest(seed, canonical_repo_url(repo)), canonical_repo_url(repo), repo)
            for repo, rows in eligible_by_repo.items()
            if len(rows) >= candidates_per_repository
        ),
        key=lambda item: (item[0], item[1]),
    )
    if len(ranked_repositories) < required_repositories:
        count = (
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
        )
        required_label = (
            count[required_repositories]
            if required_repositories < len(count)
            else str(required_repositories)
        )
        candidate_label = (
            count[candidates_per_repository]
            if 0 <= candidates_per_repository < len(count)
            else str(candidates_per_repository)
        )
        raise ValueError(
            f"holdout requires {required_label} repositories with at least "
            f"{candidate_label} eligible rows"
        )
    selected_repositories = ranked_repositories[:required_repositories]

    def ranked(repo: str) -> list[dict[str, Any]]:
        return sorted(
            eligible_by_repo[repo],
            key=lambda row: (
                _digest(candidate_seed, str(row["instance_id"])),
                str(row["instance_id"]),
            ),
        )

    analysis = [
        ranked(repo)[candidate_slot] for _, _, repo in selected_repositories[:analysis_repositories]
    ]
    if legacy_strategy:
        first_repo_rows = ranked(selected_repositories[0][2])
        occupied = {
            *str(first_repo_rows[0]["production_paths"]).split("|"),
            str(first_repo_rows[0]["test_path"]),
        }
        second = next(
            (
                row
                for row in first_repo_rows[1:]
                if occupied.isdisjoint(
                    {
                        *str(row["production_paths"]).split("|"),
                        str(row["test_path"]),
                    }
                )
            ),
            None,
        )
        if second is None:
            raise ValueError("first ranked repository has no disjoint second candidate")
        analysis.append(second)
    canary_id: str | None = None
    canary_url: str | None = None
    if private_canary:
        canary = ranked(selected_repositories[analysis_repositories][2])[candidate_slot]
        analysis_urls = {canonical_repo_url(str(row["repo"])) for row in analysis}
        canary_url = canonical_repo_url(str(canary["repo"]))
        canary_id = str(canary["instance_id"])
        if canary_url in analysis_urls or canary_id in {
            str(row["instance_id"]) for row in analysis
        }:
            raise ValueError("canary must be disjoint from analysis selections")
    return {
        "protocol_id": protocol["protocol_id"],
        "analysis_instance_ids": [row["instance_id"] for row in analysis],
        "analysis_repository_map": {
            str(row["instance_id"]): canonical_repo_url(str(row["repo"])) for row in analysis
        },
        "canary_instance_id": canary_id,
        "canary_repository": canary_url,
    }


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _validated_selection(
    selection: dict[str, Any],
    protocol: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    selection_rules = protocol["selection"]
    strategy = selection_rules.get("strategy", "legacy")
    legacy_strategy = strategy is None or strategy == "legacy"
    private_canary = selection_rules.get("private_canary", legacy_strategy)
    analysis_repositories = int(selection_rules["analysis_repositories"])
    required_repositories = int(selection_rules["eligible_repositories_required"])
    analysis_scenarios = int(selection_rules["analysis_scenarios"])
    candidates_per_repository = int(selection_rules["eligible_candidates_per_repository_required"])
    if not isinstance(private_canary, bool):
        raise ValueError("selection.private_canary must be a boolean")
    if legacy_strategy:
        if (
            not private_canary
            or required_repositories != analysis_repositories + 1
            or analysis_scenarios != analysis_repositories + 1
        ):
            raise ValueError("claim selection cardinalities are inconsistent")
    elif strategy == "one-per-repository":
        if "private_canary" not in selection_rules:
            raise ValueError("claim selection requires explicit private_canary")
        expected_repositories = analysis_repositories + (1 if private_canary else 0)
        if (
            analysis_repositories < 1
            or analysis_scenarios != analysis_repositories
            or required_repositories != expected_repositories
            or candidates_per_repository < 1
        ):
            raise ValueError("claim selection cardinalities are inconsistent")
    else:
        raise ValueError(f"unsupported holdout selection strategy: {strategy!r}")
    analysis_ids = selection.get("analysis_instance_ids")
    repository_map = selection.get("analysis_repository_map")
    canary_id = selection.get("canary_instance_id")
    canary_repository = selection.get("canary_repository")
    if (
        selection.get("protocol_id") != protocol["protocol_id"]
        or not isinstance(analysis_ids, list)
        or not all(isinstance(value, str) and value for value in analysis_ids)
        or len(analysis_ids) != analysis_scenarios
        or len(set(analysis_ids)) != len(analysis_ids)
        or not isinstance(repository_map, dict)
        or set(repository_map) != set(analysis_ids)
        or not all(
            isinstance(key, str) and isinstance(value, str) and _is_canonical_repo_url(value)
            for key, value in repository_map.items()
        )
        or (
            strategy == "one-per-repository"
            and len(set(repository_map.values())) != analysis_repositories
        )
    ):
        raise ValueError("claim selection is invalid")
    analysis_repository_values = set(repository_map.values())
    if private_canary:
        if (
            not isinstance(canary_id, str)
            or not canary_id
            or canary_id in repository_map
            or not isinstance(canary_repository, str)
            or not _is_canonical_repo_url(canary_repository)
            or canary_repository in analysis_repository_values
        ):
            raise ValueError("claim selection is invalid")
        return [*analysis_ids, canary_id], dict(repository_map)
    if canary_id is not None or canary_repository is not None:
        raise ValueError("claim selection is invalid")
    return [*analysis_ids], dict(repository_map)


def build_reconstructed_test_attestation(
    selection: dict[str, Any],
    protocol: dict[str, Any],
    reconstructed_tests: dict[str, str],
) -> dict[str, Any]:
    selected_ids, _ = _validated_selection(selection, protocol)
    if set(reconstructed_tests) != set(selected_ids) or not all(
        isinstance(source, str) for source in reconstructed_tests.values()
    ):
        raise ValueError("reconstructed tests do not match the frozen selection")
    module_sha256: dict[str, str] = {}
    for scenario_id in sorted(selected_ids):
        source = reconstructed_tests[scenario_id]
        validate_reconstructed_test_module(source)
        module_sha256[scenario_id] = hashlib.sha256(source.encode()).hexdigest()
    return {
        "guard": "reconstructed-test-dependency-v1",
        "selection_sha256": hashlib.sha256(_canonical_json_bytes(selection)).hexdigest(),
        "module_sha256": module_sha256,
    }


def evaluate_repository_claim(
    repository_rows: list[dict[str, Any]],
    protocol: dict[str, Any],
    selection: dict[str, Any],
    *,
    scenario_pack_bytes: bytes,
    collision_attestation_bytes: bytes,
    control_results_bytes: bytes,
    reconstructed_tests: dict[str, str],
) -> dict[str, Any]:
    """Evaluate the preregistered repository-level efficacy gates."""
    execution_inputs = protocol.get("execution_inputs")
    if protocol.get("stage") != "execution-frozen" or not isinstance(execution_inputs, dict):
        raise ValueError("claim evaluation requires an execution-frozen protocol")
    if any(
        SHA256.fullmatch(str(execution_inputs.get(field) or "")) is None
        for field in (
            "selection_output_sha256",
            "scenario_pack_sha256",
            "collision_attestation_sha256",
            "reconstructed_test_attestation_sha256",
            "control_results_sha256",
        )
    ):
        raise ValueError("claim evaluation requires complete frozen execution hashes")
    selected_ids, repository_map = _validated_selection(selection, protocol)
    selection_sha256 = hashlib.sha256(_canonical_json_bytes(selection)).hexdigest()
    if selection_sha256 != execution_inputs["selection_output_sha256"]:
        raise ValueError("claim selection does not match the execution freeze")

    scenario_pack_sha256 = hashlib.sha256(scenario_pack_bytes).hexdigest()
    if scenario_pack_sha256 != execution_inputs["scenario_pack_sha256"]:
        raise ValueError("claim scenario pack does not match the execution freeze")
    try:
        scenario_pack = json.loads(scenario_pack_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("claim scenario pack is invalid") from exc
    scenario_rows = scenario_pack.get("scenarios") if isinstance(scenario_pack, dict) else None
    if (
        not isinstance(scenario_rows, list)
        or not all(
            isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and SHA256.fullmatch(str(row.get("reconstructed_test_sha256") or "")) is not None
            for row in scenario_rows
        )
        or {str(row["id"]) for row in scenario_rows} != set(selected_ids)
        or len(scenario_rows) != len(selected_ids)
    ):
        raise ValueError("claim scenario pack does not match the frozen selection")
    scenario_test_sha256 = {
        str(row["id"]): str(row["reconstructed_test_sha256"]) for row in scenario_rows
    }

    if (
        hashlib.sha256(collision_attestation_bytes).hexdigest()
        != execution_inputs["collision_attestation_sha256"]
    ):
        raise ValueError("claim collision attestation does not match the execution freeze")
    try:
        collision_attestation = json.loads(collision_attestation_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("claim collision attestation is invalid") from exc
    if (
        not isinstance(collision_attestation, dict)
        or collision_attestation.get("guard") != "runtime-pack-distinctive-evidence-v1"
        or collision_attestation.get("runtime_availability_sha256")
        != protocol["product_inputs"]["runtime_availability_sha256"]
        or collision_attestation.get("catalog_archive_sha256")
        != protocol["product_inputs"]["catalog_archive_sha256"]
        or collision_attestation.get("scenarios_sha256") != scenario_pack_sha256
        or collision_attestation.get("collision_free") is not True
        or isinstance(collision_attestation.get("collision_count"), bool)
        or not isinstance(collision_attestation.get("collision_count"), int)
        or collision_attestation.get("collision_count") != 0
        or collision_attestation.get("scenario_ids") != sorted(selected_ids)
    ):
        raise ValueError("claim collision attestation is invalid")

    reconstructed_attestation = build_reconstructed_test_attestation(
        selection,
        protocol,
        reconstructed_tests,
    )
    if (
        hashlib.sha256(_canonical_json_bytes(reconstructed_attestation)).hexdigest()
        != execution_inputs["reconstructed_test_attestation_sha256"]
        or reconstructed_attestation["module_sha256"] != scenario_test_sha256
    ):
        raise ValueError("claim reconstructed tests do not match the execution freeze")

    if (
        hashlib.sha256(control_results_bytes).hexdigest()
        != execution_inputs["control_results_sha256"]
    ):
        raise ValueError("claim control results do not match the execution freeze")
    try:
        control_results = json.loads(control_results_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("claim control results are invalid") from exc
    scenario_results = (
        control_results.get("scenario_results") if isinstance(control_results, dict) else None
    )
    control_timeout = protocol.get("timeouts", {}).get("control_verification_seconds")
    if (
        isinstance(control_timeout, bool)
        or not isinstance(control_timeout, int | float)
        or not math.isfinite(control_timeout)
        or control_timeout <= 0
        or not isinstance(control_results, dict)
        or control_results.get("guard") != "holdout-control-results-v1"
        or control_results.get("selection_sha256") != selection_sha256
        or control_results.get("scenario_pack_sha256") != scenario_pack_sha256
        or (
            control_results.get("all_scenarios_passed", control_results.get("all_seven_passed"))
            is not True
        )
        or not isinstance(scenario_results, dict)
        or set(scenario_results) != set(selected_ids)
    ):
        raise ValueError("claim control results are invalid")
    for scenario_id in selected_ids:
        result = scenario_results[scenario_id]
        if (
            not isinstance(result, dict)
            or result.get("parent_with_test_patch_red") is not True
            or result.get("reference_patch_green") is not True
            or result.get("changed_test_module_green") is not True
            or result.get("timeout_compliant") is not True
            or result.get("reconstructed_test_sha256") != scenario_test_sha256[scenario_id]
            or any(
                SHA256.fullmatch(str(result.get(field) or "")) is None
                for field in (
                    "red_evidence_sha256",
                    "green_evidence_sha256",
                    "module_evidence_sha256",
                )
            )
            or isinstance(result.get("elapsed_seconds"), bool)
            or not isinstance(result.get("elapsed_seconds"), int | float)
            or not math.isfinite(result["elapsed_seconds"])
            or result["elapsed_seconds"] < 0
            or isinstance(result.get("timeout_seconds"), bool)
            or not isinstance(result.get("timeout_seconds"), int | float)
            or not math.isfinite(result["timeout_seconds"])
            or result["timeout_seconds"] <= 0
            or result["timeout_seconds"] != control_timeout
            or result["elapsed_seconds"] > result["timeout_seconds"]
        ):
            raise ValueError("claim control results are invalid")

    expected_scenarios: dict[str, list[str]] = defaultdict(list)
    for scenario_id, repository in repository_map.items():
        expected_scenarios[repository].append(scenario_id)
    expected = int(protocol["selection"]["analysis_repositories"])
    repositories = [str(row.get("repository") or "") for row in repository_rows]
    if (
        len(repository_rows) != expected
        or len(set(repositories)) != expected
        or set(repositories) != set(expected_scenarios)
    ):
        raise ValueError("claim evaluation repositories do not match the frozen selection")

    def ratios(field: str) -> list[float]:
        values: list[float] = []
        for row in repository_rows:
            value = row.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"claim evaluation requires finite non-negative {field}")
            values.append(float(value))
        return values

    token_ratios = ratios("uncached_provider_tokens_ratio")
    time_ratios = ratios("total_seconds_ratio")
    benefiting = sum(value < 1.0 for value in token_ratios)
    support_p = sum(
        math.comb(expected, successes) for successes in range(benefiting, expected + 1)
    ) / (2**expected)
    gates = protocol["claim_gates"]
    overall_token = float(statistics.median(token_ratios))
    overall_time = float(statistics.median(time_ratios))
    paired_trials = int(gates["paired_trials_per_scenario"])
    evidence_complete = True
    for row in repository_rows:
        repository = str(row["repository"])
        scenarios = sorted(expected_scenarios[repository])
        trial_counts = row.get("paired_trials_by_scenario")
        missing_pairs = row.get("missing_pairs")
        unresolved = row.get("unresolved_incidents")
        evidence_complete = evidence_complete and all(
            (
                row.get("scenario_ids") == scenarios,
                isinstance(trial_counts, dict),
                set(trial_counts) == set(scenarios) if isinstance(trial_counts, dict) else False,
                (
                    all(
                        isinstance(count, int)
                        and not isinstance(count, bool)
                        and count == paired_trials
                        for count in trial_counts.values()
                    )
                    if isinstance(trial_counts, dict)
                    else False
                ),
                isinstance(missing_pairs, int) and not isinstance(missing_pairs, bool),
                missing_pairs == 0,
                row.get("token_usage_exact") is True,
                row.get("trusted_policy_outcomes") is True,
                isinstance(unresolved, int) and not isinstance(unresolved, bool),
                unresolved == 0,
            )
        )
    quality_preserved = all(row.get("quality_preserved") is True for row in repository_rows)
    verified_deliveries = sum(row.get("verified_delivery") is True for row in repository_rows)
    incident_free = all(
        isinstance(row.get("unresolved_incidents"), int)
        and not isinstance(row.get("unresolved_incidents"), bool)
        and row.get("unresolved_incidents") == 0
        for row in repository_rows
    )
    passed = all(
        (
            overall_token <= float(gates["primary_endpoint_maximum_ratio"]),
            overall_time <= float(gates["total_seconds_maximum_ratio"]),
            quality_preserved,
            verified_deliveries >= int(gates["minimum_repositories_with_verified_delivery"]),
            benefiting >= int(gates["required_benefiting_repositories"]),
            support_p <= float(gates["exact_one_sided_repository_support_alpha"]),
            incident_free,
            evidence_complete,
        )
    )
    return {
        "overall_token_ratio": overall_token,
        "overall_time_ratio": overall_time,
        "benefiting_repositories": benefiting,
        "verified_delivery_repositories": verified_deliveries,
        "exact_one_sided_sign_p": support_p,
        "quality_preserved": quality_preserved,
        "incident_free": incident_free,
        "evidence_complete": evidence_complete,
        "passed": passed,
    }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError("selection JSONL must contain objects")
    return rows


def _private_text_handle(path: Path) -> TextIO:
    resolved = path.resolve(strict=False)
    private_root = PRIVATE_ROOT.resolve()
    if ROOT.resolve() in resolved.parents and private_root not in resolved.parents:
        raise ValueError("holdout evidence inside the repository must use .gate/ctx-ab-private")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValueError("holdout evidence parent must be owner-only")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("holdout evidence path must be a regular file")
    if path.exists() and path.stat().st_nlink != 1:
        raise ValueError("holdout evidence path must not be a hard link")
    if path.exists():
        path.chmod(0o600)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="")


def _paths_are_distinct(paths: list[Path]) -> bool:
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if left.resolve(strict=False) == right.resolve(strict=False):
                return False
            if left.exists() and right.exists() and os.path.samefile(left, right):
                return False
    return True


def _remove_stale_selection(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    resolved = path.resolve(strict=False)
    private_root = PRIVATE_ROOT.resolve()
    if ROOT.resolve() in resolved.parents and private_root not in resolved.parents:
        raise ValueError("stale selection inside the repository must use .gate/ctx-ab-private")
    if stat.S_IMODE(path.parent.stat().st_mode) != 0o700:
        raise ValueError("stale selection parent must be owner-only")
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("stale selection must be a single-link regular file")
    path.unlink()


def _requires_acquisition_protocol_digest(protocol: dict[str, Any]) -> bool:
    selection = protocol.get("selection")
    return (
        protocol.get("schema_version") == 2
        or protocol.get("protocol_id") == "production-graph-holdout-v2"
        or (isinstance(selection, dict) and selection.get("strategy") == "one-per-repository")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--expected-acquisition-protocol-sha256")
    parser.add_argument("--selection-jsonl", type=Path, required=True)
    parser.add_argument("--exposure-ledger", type=Path)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args(argv)
    paths = [args.protocol, args.selection_jsonl, args.ledger, args.selection]
    if args.exposure_ledger is not None:
        paths.append(args.exposure_ledger)
    if not _paths_are_distinct(paths):
        raise SystemExit("protocol, source, ledger, and selection paths must be distinct")
    expected_protocol_sha256 = args.expected_acquisition_protocol_sha256
    if expected_protocol_sha256 is not None and SHA256.fullmatch(expected_protocol_sha256) is None:
        raise SystemExit("expected acquisition protocol SHA-256 must be 64 lowercase hex digits")
    protocol_bytes = args.protocol.read_bytes()
    if expected_protocol_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(protocol_bytes).hexdigest(),
        expected_protocol_sha256,
    ):
        raise SystemExit("acquisition protocol does not match the expected SHA-256")
    protocol = json.loads(protocol_bytes)
    requires_v2_authentication = _requires_acquisition_protocol_digest(protocol)
    if requires_v2_authentication and expected_protocol_sha256 is None:
        raise SystemExit("V2 selection requires --expected-acquisition-protocol-sha256")
    exposure_document: dict[str, Any] | None = None
    if requires_v2_authentication:
        expected_exposure_sha256 = protocol.get("exposure_ledger_sha256")
        if (
            not isinstance(expected_exposure_sha256, str)
            or SHA256.fullmatch(expected_exposure_sha256) is None
        ):
            raise SystemExit("V2 acquisition protocol lacks an authenticated exposure ledger")
        if args.exposure_ledger is None:
            raise SystemExit(
                "V2 selection requires an authenticated exposure ledger via --exposure-ledger"
            )
        try:
            exposure_document = exposure_ledger.load_authenticated_ledger(
                args.exposure_ledger,
                expected_exposure_sha256,
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(f"authenticated exposure ledger is invalid: {exc}") from None
    elif args.exposure_ledger is not None:
        raise SystemExit("--exposure-ledger is only valid for V2 selection")
    _remove_stale_selection(args.selection)
    universe = protocol["universe"]
    if (
        protocol.get("stage") not in {"acquisition-frozen", "execution-frozen"}
        or SHA256.fullmatch(str(universe.get("raw_parquet_sha256") or "")) is None
        or SHA256.fullmatch(str(universe.get("duckdb_cli_sha256") or "")) is None
        or SHA256.fullmatch(str(universe.get("selection_jsonl_sha256") or "")) is None
    ):
        raise SystemExit("selection requires a frozen authenticated acquisition")
    if (
        hashlib.sha256(args.selection_jsonl.read_bytes()).hexdigest()
        != universe["selection_jsonl_sha256"]
    ):
        raise SystemExit("selection JSONL does not match the frozen SHA-256")
    source_rows = sorted(
        _load_jsonl(args.selection_jsonl),
        key=lambda row: str(row.get("instance_id") or ""),
    )
    if len(source_rows) != protocol["universe"]["expected_rows"]:
        raise SystemExit("selection JSONL row count does not match the frozen universe")
    ledger = [evaluate_row(row, protocol) for row in source_rows]
    if exposure_document is not None:
        ledger = reject_historical_exposures(ledger, exposure_document)
    allowed_rejection_codes = set(protocol["static_candidate_rules"]["rejection_codes"])
    if exposure_document is not None:
        allowed_rejection_codes.add(HISTORICAL_EXPOSURE_REJECTION_CODE)
    if {row["rejection_code"] for row in ledger if row["rejection_code"]} - set(
        allowed_rejection_codes
    ):
        raise SystemExit("selector emitted an undeclared rejection code")
    with _private_text_handle(args.ledger) as handle:
        writer = csv.DictWriter(handle, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerows(ledger)
    selection = select_rows(ledger, protocol)
    if exposure_document is not None:
        require_exposure_disjoint_selection(selection, exposure_document)
    with _private_text_handle(args.selection) as handle:
        handle.write(_canonical_json_bytes(selection).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
