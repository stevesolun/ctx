from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import ctx_ab_holdout as selector


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "benchmarks" / "ctx_ab" / "holdout-protocol-v1.json"


def _protocol(
    *,
    strategy: str | None = None,
    repositories: int = 10,
    private_canary: bool | None = None,
    candidates_per_repository: int = 1,
) -> dict[str, Any]:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    rules = protocol["selection"]
    rules["analysis_repositories"] = repositories
    rules["analysis_scenarios"] = repositories
    rules["eligible_candidates_per_repository_required"] = candidates_per_repository
    effective_private_canary = (
        private_canary if private_canary is not None else strategy != "one-per-repository"
    )
    rules["eligible_repositories_required"] = repositories + int(effective_private_canary)
    if strategy is None:
        rules.pop("strategy", None)
    else:
        rules["strategy"] = strategy
    if private_canary is None:
        rules.pop("private_canary", None)
    else:
        rules["private_canary"] = private_canary
    return protocol


def _v2_protocol(*, generation: int) -> dict[str, Any]:
    protocol = _protocol(
        strategy="one-per-repository",
        private_canary=False,
        candidates_per_repository=generation,
    )
    protocol["schema_version"] = 2
    protocol["protocol_id"] = "production-graph-holdout-v2"
    protocol["protocol_generation"] = generation
    protocol["candidate_partition_seed"] = hashlib.sha256(
        selector.V2_CANDIDATE_PARTITION_PREFIX
        + str(protocol["universe"]["revision"]).encode("ascii")
    ).hexdigest()
    protocol["selection"]["candidate_slot"] = generation - 1
    return protocol


def _ledger(repositories: int, *, candidates_per_repository: int = 1) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for repo_index in range(repositories):
        for candidate_index in range(candidates_per_repository):
            rows.append(
                {
                    "instance_id": f"repo-{repo_index}-candidate-{candidate_index}",
                    "repo": f"owner/repo-{repo_index}",
                    "production_paths": f"src/repo_{repo_index}/feature_{candidate_index}.py",
                    "test_path": f"tests/repo_{repo_index}/test_{candidate_index}.py",
                    "status": "eligible",
                }
            )
    return rows


def test_legacy_strategy_preserves_exact_v1_selection() -> None:
    protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    rows = [
        {
            "instance_id": f"owner__repo-{repo}-{suffix}",
            "repo": f"owner/repo-{repo}",
            "production_paths": f"src/{repo}/{suffix}.py",
            "test_path": f"tests/{repo}/test_{suffix}.py",
            "status": "eligible",
        }
        for repo, suffixes in zip("abcdefg", ("ab", "cd", "ef", "12", "34", "56", "78"))
        for suffix in suffixes
    ]

    assert selector.select_rows(rows, protocol) == {
        "protocol_id": "production-graph-holdout-v1",
        "analysis_instance_ids": [
            "owner__repo-g-8",
            "owner__repo-c-e",
            "owner__repo-b-c",
            "owner__repo-d-2",
            "owner__repo-e-4",
            "owner__repo-g-7",
        ],
        "analysis_repository_map": {
            "owner__repo-g-8": "https://github.com/owner/repo-g.git",
            "owner__repo-c-e": "https://github.com/owner/repo-c.git",
            "owner__repo-b-c": "https://github.com/owner/repo-b.git",
            "owner__repo-d-2": "https://github.com/owner/repo-d.git",
            "owner__repo-e-4": "https://github.com/owner/repo-e.git",
            "owner__repo-g-7": "https://github.com/owner/repo-g.git",
        },
        "canary_instance_id": "owner__repo-a-a",
        "canary_repository": "https://github.com/owner/repo-a.git",
    }


def test_one_per_repository_selects_deterministic_ten_repo_analysis() -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    ledger = _ledger(10, candidates_per_repository=2)

    first = selector.select_rows(ledger, protocol)
    second = selector.select_rows(list(reversed(ledger)), protocol)
    ranked = sorted(
        {selector.canonical_repo_url(str(row["repo"])) for row in ledger},
        key=lambda repo: (selector._digest(str(protocol["selection_seed"]), repo), repo),
    )
    expected_ids = [
        min(
            (row for row in ledger if selector.canonical_repo_url(str(row["repo"])) == repository),
            key=lambda row: (
                selector._digest(
                    str(protocol["selection_seed"]),
                    str(row["instance_id"]),
                ),
                str(row["instance_id"]),
            ),
        )["instance_id"]
        for repository in ranked
    ]

    assert first == second
    assert len(first["analysis_instance_ids"]) == 10
    assert first["analysis_instance_ids"] == expected_ids
    assert list(first["analysis_repository_map"].values()) == ranked[:10]
    assert len(set(first["analysis_repository_map"].values())) == 10
    assert first["canary_instance_id"] is None
    assert first["canary_repository"] is None
    assert selector._validated_selection(first, protocol)[0] == first["analysis_instance_ids"]


def test_one_per_repository_supports_private_canary() -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=True)
    selection = selector.select_rows(_ledger(11, candidates_per_repository=2), protocol)
    assert selection["canary_instance_id"] is not None
    assert selection["canary_repository"] is not None
    assert len(selector._validated_selection(selection, protocol)[0]) == 11


def test_one_per_repository_rejects_insufficient_repositories() -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    with pytest.raises(ValueError, match="ten repositories"):
        selector.select_rows(_ledger(9, candidates_per_repository=2), protocol)


def test_one_per_repository_rejects_insufficient_candidates() -> None:
    protocol = _protocol(
        strategy="one-per-repository",
        private_canary=False,
        candidates_per_repository=2,
    )
    with pytest.raises(ValueError, match="ten repositories"):
        selector.select_rows(_ledger(10, candidates_per_repository=1), protocol)


def test_v2_generations_select_disjoint_candidate_slots() -> None:
    ledger = _ledger(10, candidates_per_repository=2)
    first = selector.select_rows(ledger, _v2_protocol(generation=1))
    second = selector.select_rows(ledger, _v2_protocol(generation=2))

    assert set(first["analysis_instance_ids"]).isdisjoint(second["analysis_instance_ids"])
    assert set(first["analysis_repository_map"].values()) == set(
        second["analysis_repository_map"].values()
    )


def test_v2_generation_fails_closed_without_fresh_candidate_slot() -> None:
    protocol = _v2_protocol(generation=2)

    with pytest.raises(ValueError, match="ten repositories with at least two eligible rows"):
        selector.select_rows(_ledger(10, candidates_per_repository=1), protocol)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol_generation", 3),
        ("candidate_partition_seed", "0" * 64),
    ],
)
def test_v2_rejects_candidate_partition_protocol_drift(field: str, value: object) -> None:
    protocol = _v2_protocol(generation=2)
    protocol[field] = value

    with pytest.raises(ValueError, match="candidate partition contract"):
        selector.select_rows(_ledger(10, candidates_per_repository=3), protocol)


def test_v2_rejects_candidate_slot_drift() -> None:
    protocol = _v2_protocol(generation=2)
    protocol["selection"]["candidate_slot"] = 0

    with pytest.raises(ValueError, match="candidate partition contract"):
        selector.select_rows(_ledger(10, candidates_per_repository=2), protocol)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("analysis_scenarios", 9),
        ("eligible_repositories_required", 11),
        ("eligible_candidates_per_repository_required", 0),
    ],
)
def test_one_per_repository_rejects_invalid_cardinalities(field: str, value: int) -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    protocol["selection"][field] = value
    with pytest.raises(ValueError, match="one-per-repository"):
        selector.select_rows(_ledger(11), protocol)


def test_one_per_repository_requires_explicit_canary_mode() -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    protocol["selection"].pop("private_canary")
    with pytest.raises(ValueError, match="explicit private_canary"):
        selector.select_rows(_ledger(10), protocol)


def test_selector_rejects_duplicate_instance_ids() -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    ledger = _ledger(10, candidates_per_repository=2)
    ledger[1]["instance_id"] = ledger[0]["instance_id"]
    with pytest.raises(ValueError, match="duplicate instance IDs"):
        selector.select_rows(ledger, protocol)


def test_v2_selection_validation_requires_distinct_analysis_repositories() -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    selection = selector.select_rows(_ledger(10, candidates_per_repository=2), protocol)
    selector._validated_selection(selection, protocol)
    first_id = selection["analysis_instance_ids"][0]
    second_id = selection["analysis_instance_ids"][1]
    selection["analysis_repository_map"][second_id] = selection["analysis_repository_map"][first_id]
    with pytest.raises(ValueError, match="claim selection is invalid"):
        selector._validated_selection(selection, protocol)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("canary_instance_id", "public-canary"),
        ("canary_repository", "https://github.com/public/sympy.git"),
    ],
)
def test_v2_selection_validation_requires_null_external_canary_fields(
    field: str,
    value: str,
) -> None:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    selection = selector.select_rows(_ledger(10, candidates_per_repository=2), protocol)
    selection[field] = value
    with pytest.raises(ValueError, match="claim selection is invalid"):
        selector._validated_selection(selection, protocol)


def _v2_cli_arguments(
    tmp_path: Path,
    *,
    protocol_bytes: bytes,
    expected_protocol_sha256: str | None,
) -> tuple[list[str], Path, Path, Path]:
    protocol_path = tmp_path / "protocol.json"
    source_path = tmp_path / "source.jsonl"
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    ledger_path = private / "ledger.csv"
    selection_path = private / "selection.json"
    protocol_path.write_bytes(protocol_bytes)
    source_path.write_text("{}\n", encoding="utf-8")
    arguments = [
        "--protocol",
        str(protocol_path),
        "--selection-jsonl",
        str(source_path),
        "--ledger",
        str(ledger_path),
        "--selection",
        str(selection_path),
    ]
    if expected_protocol_sha256 is not None:
        arguments.extend(
            [
                "--expected-acquisition-protocol-sha256",
                expected_protocol_sha256,
            ]
        )
    return arguments, source_path, ledger_path, selection_path


def _v2_cli_protocol(source_path: Path) -> dict[str, Any]:
    protocol = _protocol(strategy="one-per-repository", private_canary=False)
    protocol["schema_version"] = 2
    protocol["protocol_id"] = "production-graph-holdout-v2"
    protocol["protocol_generation"] = 1
    protocol["candidate_partition_seed"] = hashlib.sha256(
        selector.V2_CANDIDATE_PARTITION_PREFIX
        + str(protocol["universe"]["revision"]).encode("ascii")
    ).hexdigest()
    protocol["selection"]["candidate_slot"] = 0
    protocol["stage"] = "acquisition-frozen"
    protocol["universe"]["expected_rows"] = 10
    protocol["universe"]["raw_parquet_sha256"] = "1" * 64
    protocol["universe"]["duckdb_cli_sha256"] = "2" * 64
    protocol["universe"]["selection_jsonl_sha256"] = hashlib.sha256(
        source_path.read_bytes()
    ).hexdigest()
    return protocol


def test_v2_selector_cli_accepts_matching_acquisition_protocol_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder_protocol = b"{}"
    arguments, source_path, ledger_path, selection_path = _v2_cli_arguments(
        tmp_path,
        protocol_bytes=placeholder_protocol,
        expected_protocol_sha256=hashlib.sha256(placeholder_protocol).hexdigest(),
    )
    protocol_bytes = json.dumps(
        _v2_cli_protocol(source_path),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_bytes(protocol_bytes)
    digest_index = arguments.index("--expected-acquisition-protocol-sha256") + 1
    arguments[digest_index] = hashlib.sha256(protocol_bytes).hexdigest()
    rows = [
        {
            **row,
            "base_commit": "",
            "production_changed_lines": 0,
            "rejection_code": "",
        }
        for row in _ledger(10)
    ]
    monkeypatch.setattr(selector, "_load_jsonl", lambda _: rows)
    monkeypatch.setattr(selector, "evaluate_row", lambda row, _: row)

    assert selector.main(arguments) == 0
    assert ledger_path.is_file()
    assert selection_path.is_file()


@pytest.mark.parametrize("failure", ["missing", "drift"])
def test_v2_selector_cli_authenticates_protocol_before_private_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    placeholder_protocol = b"{}"
    arguments, source_path, ledger_path, selection_path = _v2_cli_arguments(
        tmp_path,
        protocol_bytes=placeholder_protocol,
        expected_protocol_sha256=None,
    )
    protocol_bytes = json.dumps(
        _v2_cli_protocol(source_path),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (tmp_path / "protocol.json").write_bytes(protocol_bytes)
    if failure == "drift":
        arguments.extend(
            [
                "--expected-acquisition-protocol-sha256",
                hashlib.sha256(protocol_bytes).hexdigest(),
            ]
        )
        (tmp_path / "protocol.json").write_bytes(protocol_bytes + b"\n")
    ledger_path.write_text("preserve-ledger", encoding="utf-8")
    selection_path.write_text("preserve-selection", encoding="utf-8")

    def fail_private_read(_: Path) -> list[dict[str, Any]]:
        raise AssertionError("private rows were read before protocol authentication")

    monkeypatch.setattr(selector, "_load_jsonl", fail_private_read)
    message = (
        "requires --expected-acquisition-protocol-sha256"
        if failure == "missing"
        else "does not match the expected SHA-256"
    )
    with pytest.raises(SystemExit, match=message):
        selector.main(arguments)

    assert ledger_path.read_text(encoding="utf-8") == "preserve-ledger"
    assert selection_path.read_text(encoding="utf-8") == "preserve-selection"
