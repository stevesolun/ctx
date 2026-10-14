from __future__ import annotations

import csv
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))

from ctx.monitor import routes as monitor_routes  # noqa: E402

CANONICAL_TRACKER = repo_root / "qa" / "feature_status.csv"
POINTER = repo_root / "docs" / "qa" / "dashboard-user-story-status.csv"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_dashboard_csv_is_a_non_authoritative_pointer() -> None:
    rows = _rows(POINTER)
    assert len(rows) == 1
    assert rows[0]["artifact_kind"] == "historical-generated-pointer"
    assert rows[0]["canonical_tracker"] == "qa/feature_status.csv"
    assert rows[0]["authority"] == "false"
    assert "only current authority" in rows[0]["notes"]


def test_canonical_tracker_covers_every_monitor_route() -> None:
    canonical = _rows(CANONICAL_TRACKER)
    dashboard_rows = [
        row
        for row in canonical
        if row["surface"].startswith("Dashboard") or row["entrypoint_or_route"].startswith("/")
    ]
    tracker = "\n".join(" ".join(row.values()) for row in dashboard_rows)
    route_patterns: list[str] = []
    route_patterns.extend(href for _key, _label, href in monitor_routes.NAV_ROUTES)
    route_patterns.extend(sorted(monitor_routes.PAGE_ROUTES))
    route_patterns.extend(sorted(monitor_routes.GET_API_ROUTES))
    route_patterns.extend(monitor_routes.GET_API_PATTERNS)
    route_patterns.extend(sorted(monitor_routes.POST_API_ROUTES))
    route_patterns.extend(("/session/<session_id>", "/skill/<slug>", "/wiki/<slug>"))
    assert [route for route in dict.fromkeys(route_patterns) if route not in tracker] == []


def test_dashboard_rows_have_no_exact_feature_route_duplicates() -> None:
    canonical = _rows(CANONICAL_TRACKER)
    seen: dict[tuple[str, str], str] = {}
    for row in canonical:
        if not (
            row["surface"].startswith("Dashboard") or row["entrypoint_or_route"].startswith("/")
        ):
            continue
        key = (row["feature"].strip().casefold(), row["entrypoint_or_route"].strip().casefold())
        assert key not in seen, (
            f"{row['feature_id']} duplicates {seen[key]} for {row['entrypoint_or_route']}"
        )
        seen[key] = row["feature_id"]
