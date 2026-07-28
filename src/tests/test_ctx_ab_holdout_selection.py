from __future__ import annotations

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
