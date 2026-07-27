from __future__ import annotations

import csv
import re
import subprocess
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]

TRACKER = repo_root / "qa" / "bug_smoke_status.csv"
BENCHMARK_TRACKER = repo_root / "qa" / "ctx_benchmark_status.csv"
GENERATED_NAMES = {".DS_Store"}
GENERATED_SUFFIXES = {".pyc", ".pyo", ".tmp", ".bak", ".orig"}
STATUSES = {
    "Needs Triage",
    "Needs Validation",
    "Needs Fix",
    "Retested Pass",
    "Blocked/Human Decision",
    "False Positive",
}
FIX_STATUSES = {"Fixed", "Blocked", "In Progress", "Not Started", "N/A"}
BENCHMARK_STATUSES = {
    "Resolved",
    "Needs Validation",
    "Needs Fix",
    "Blocked/Human Decision",
}
CLOSED_NEXT_ACTION_PREFIX = "Closed;"


def _tracker_rows() -> list[dict[str, str]]:
    with TRACKER.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _benchmark_tracker_rows() -> list[dict[str, str]]:
    with BENCHMARK_TRACKER.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_bug_smoke_tracker_has_valid_rows() -> None:
    rows = _tracker_rows()
    required = (
        "finding_id",
        "category",
        "scope",
        "surface",
        "file_or_pattern",
        "source_evidence",
        "severity",
        "expected_behavior",
        "discovery_method",
        "status",
        "bug_summary",
        "repro_or_detection",
        "fix_strategy",
        "fix_status",
        "validation_command",
        "retest_evidence",
        "last_verified_at",
        "owner",
        "review_status",
        "next_action",
    )

    assert rows
    assert len({row["finding_id"] for row in rows}) == len(rows)
    for row in rows:
        assert None not in row, f"{row.get('finding_id', '<unknown>')} has extra CSV columns"
        for key in required:
            assert row[key].strip(), f"{row.get('finding_id', '<unknown>')} missing {key}"
        assert row["severity"] in {"Low", "Medium", "High", "Critical"}
        assert row["status"] in STATUSES
        assert row["fix_status"] in FIX_STATUSES
        if row["status"] == "Retested Pass":
            assert row["fix_status"] == "Fixed"
            assert row["retest_evidence"].startswith("PASS:")
            assert row["next_action"].startswith(CLOSED_NEXT_ACTION_PREFIX), (
                f"{row['finding_id']} has non-closed next_action"
            )
        if row["status"] == "Blocked/Human Decision":
            assert row["owner"] == "Human Owner"
            assert row["fix_status"] == "Blocked"


def test_ctx_benchmark_tracker_has_valid_rows() -> None:
    rows = _benchmark_tracker_rows()
    required = (
        "id",
        "area",
        "user_story",
        "expected_behavior",
        "status",
        "evidence",
        "repro",
        "risk",
        "fix",
        "reviewer_verdict",
        "last_updated",
    )

    assert rows
    assert len({row["id"] for row in rows}) == len(rows)
    for row in rows:
        assert None not in row, f"{row.get('id', '<unknown>')} has extra CSV columns"
        for key in required:
            assert row[key].strip(), f"{row.get('id', '<unknown>')} missing {key}"
        assert re.fullmatch(r"BENCH-\d{3}", row["id"])
        assert row["status"] in BENCHMARK_STATUSES
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["last_updated"])


def test_git_tracks_no_generated_garbage_artifacts() -> None:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    generated = []
    for line in result.stdout.splitlines():
        path = Path(line)
        if path.name in GENERATED_NAMES or path.suffix in GENERATED_SUFFIXES:
            generated.append(line)
        elif "__pycache__" in path.parts:
            generated.append(line)

    assert generated == []
