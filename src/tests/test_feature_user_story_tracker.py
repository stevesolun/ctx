from __future__ import annotations

import csv
import sys
import tomllib
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))

from ctx.monitor import routes as monitor_routes  # noqa: E402

TRACKER = repo_root / "docs" / "qa" / "feature-user-story-status.csv"
README = repo_root / "README.md"


def _tracker_rows() -> list[dict[str, str]]:
    with TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tracker_text() -> str:
    return "\n".join(" ".join(row.values()) for row in _tracker_rows())


def test_feature_user_story_tracker_has_no_empty_core_fields() -> None:
    rows = _tracker_rows()
    assert rows
    required = (
        "feature_id",
        "surface",
        "feature",
        "entrypoint_or_route",
        "user_story",
        "expected_behavior",
        "test_command_or_steps",
        "status",
        "first_test_result",
        "last_verified_at",
    )
    for row in rows:
        for key in required:
            assert row[key].strip(), f"{row.get('feature_id', '<unknown>')} missing {key}"
        assert row["status"] in {"Tested Pass", "Retested Pass"}


def test_feature_user_story_tracker_covers_all_console_scripts() -> None:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = sorted(pyproject["project"]["scripts"])
    tracker = _tracker_text()

    assert scripts
    assert [script for script in scripts if script not in tracker] == []


def test_feature_user_story_tracker_covers_monitor_route_inventory() -> None:
    route_patterns: list[str] = []
    route_patterns.extend(href for _key, _label, href in monitor_routes.NAV_ROUTES)
    route_patterns.extend(sorted(monitor_routes.PAGE_ROUTES))
    route_patterns.extend(sorted(monitor_routes.GET_API_ROUTES))
    route_patterns.extend(monitor_routes.GET_API_PATTERNS)
    route_patterns.extend(sorted(monitor_routes.POST_API_ROUTES))
    route_patterns.extend(("/session/<session_id>", "/skill/<slug>"))
    route_patterns = list(dict.fromkeys(route_patterns))
    tracker = _tracker_text()

    assert route_patterns
    assert [route for route in route_patterns if route not in tracker] == []


def test_readme_shows_user_story_examples_from_tracker() -> None:
    readme = README.read_text(encoding="utf-8")

    assert "## Example user stories" in readme
    assert "docs/qa/feature-user-story-status.csv" in readme
    for feature_id in ("CLI-002", "CLI-026", "API-011"):
        assert feature_id in readme
