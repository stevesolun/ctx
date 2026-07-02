from __future__ import annotations

import csv
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))

from ctx.monitor import routes as monitor_routes  # noqa: E402

TRACKER = repo_root / "docs" / "qa" / "dashboard-user-story-status.csv"
PASS_STATUSES = {"Tested Pass", "Retested Pass"}
VALIDATION_STATUSES = {"Needs Validation"}
FIX_STATUSES = {"Needs Fix"}
ACTIONABLE_STATUSES = PASS_STATUSES | VALIDATION_STATUSES | FIX_STATUSES


def _tracker_rows() -> list[dict[str, str]]:
    with TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tracker_text() -> str:
    return "\n".join(" ".join(row.values()) for row in _tracker_rows())


def test_dashboard_user_story_tracker_has_valid_rows() -> None:
    rows = _tracker_rows()
    assert rows
    required = (
        "dashboard_id",
        "surface",
        "page_or_api",
        "route_or_control",
        "source_evidence",
        "user_story",
        "expected_behavior",
        "test_command_or_steps",
        "status",
        "first_test_result",
        "last_verified_at",
    )
    for row in rows:
        assert None not in row, f"{row.get('dashboard_id', '<unknown>')} has extra CSV columns"
        for key in required:
            assert row[key].strip(), f"{row.get('dashboard_id', '<unknown>')} missing {key}"
        assert row["status"] in ACTIONABLE_STATUSES
        if row["status"] in FIX_STATUSES:
            for key in ("error_id", "error_summary", "fix_status"):
                assert row[key].strip(), (
                    f"{row.get('dashboard_id', '<unknown>')} has {row['status']} without {key}"
                )
        if row["status"] in VALIDATION_STATUSES:
            assert row["notes"].strip(), (
                f"{row.get('dashboard_id', '<unknown>')} needs validation without a validation note"
            )


def test_dashboard_user_story_tracker_covers_all_monitor_routes() -> None:
    route_patterns: list[str] = []
    route_patterns.extend(href for _key, _label, href in monitor_routes.NAV_ROUTES)
    route_patterns.extend(sorted(monitor_routes.PAGE_ROUTES))
    route_patterns.extend(sorted(monitor_routes.GET_API_ROUTES))
    route_patterns.extend(monitor_routes.GET_API_PATTERNS)
    route_patterns.extend(sorted(monitor_routes.POST_API_ROUTES))
    route_patterns.extend(("/session/<session_id>", "/skill/<slug>", "/wiki/<slug>"))
    route_patterns = list(dict.fromkeys(route_patterns))
    tracker = _tracker_text()

    assert route_patterns
    assert [route for route in route_patterns if route not in tracker] == []


def test_dashboard_user_story_tracker_records_verified_graph_counts() -> None:
    rows = {row["dashboard_id"]: row for row in _tracker_rows()}
    graph_count = rows["DASH-NUM-001"]

    assert graph_count["status"] == "Tested Pass"
    assert "79,958 nodes" in graph_count["first_test_result"]
    assert "1,778,069 edges" in graph_count["first_test_result"]
