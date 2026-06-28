from __future__ import annotations

import csv
import sys
import tomllib
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))

import ctx  # noqa: E402
import ctx.api as ctx_api  # noqa: E402
from ctx.monitor import routes as monitor_routes  # noqa: E402

TRACKER = repo_root / "docs" / "qa" / "feature-user-story-status.csv"
DASHBOARD_TRACKER = repo_root / "docs" / "qa" / "dashboard-user-story-status.csv"
README = repo_root / "README.md"
PASS_STATUSES = {"Tested Pass", "Retested Pass"}
VALIDATION_STATUSES = {"Needs Validation"}
FIX_STATUSES = {"Needs Fix"}
ACTIONABLE_STATUSES = PASS_STATUSES | VALIDATION_STATUSES | FIX_STATUSES


def _tracker_rows() -> list[dict[str, str]]:
    with TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tracker_text() -> str:
    return "\n".join(" ".join(row.values()) for row in _tracker_rows())


def _row_text(row: dict[str, str]) -> str:
    return " ".join(value for value in row.values() if value)


def _rows_for_surface(rows: list[dict[str, str]], surface: str) -> list[dict[str, str]]:
    return [row for row in rows if row["surface"] == surface]


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
        assert row["status"] in ACTIONABLE_STATUSES
        if row["status"] in FIX_STATUSES:
            for key in ("error_id", "error_summary", "fix_status"):
                assert row[key].strip(), (
                    f"{row.get('feature_id', '<unknown>')} has "
                    f"{row['status']} without {key}"
                )
        if row["status"] in VALIDATION_STATUSES:
            assert row["notes"].strip(), (
                f"{row.get('feature_id', '<unknown>')} needs validation "
                "without a validation note"
            )


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


def test_feature_user_story_tracker_covers_distribution_workflows() -> None:
    workflows = (
        ".github/workflows/test.yml",
        ".github/workflows/docs.yml",
        ".github/workflows/huggingface-sync.yml",
        ".github/workflows/publish.yml",
        ".github/workflows/clean-host-contract.yml",
        ".github/workflows/xdist-experiment.yml",
    )
    tracker = _tracker_text()

    assert [workflow for workflow in workflows if workflow not in tracker] == []


def test_feature_user_story_tracker_covers_maintainer_scripts() -> None:
    scripts = sorted((repo_root / "scripts").glob("*.py"))
    tracker = _tracker_text()
    script_paths = [script.relative_to(repo_root).as_posix() for script in scripts]

    assert scripts
    assert [path for path in script_paths if path not in tracker] == []


def test_feature_user_story_tracker_covers_public_docs_assets() -> None:
    assets = sorted((repo_root / "docs" / "assets" / "javascripts").glob("*.js"))
    tracker = _tracker_text()
    asset_paths = [asset.relative_to(repo_root).as_posix() for asset in assets]

    assert assets
    assert [path for path in asset_paths if path not in tracker] == []


def test_readme_shows_user_story_examples_from_tracker() -> None:
    readme = README.read_text(encoding="utf-8")
    tracker_rows = _tracker_rows()
    with DASHBOARD_TRACKER.open(newline="", encoding="utf-8") as f:
        dashboard_rows = list(csv.DictReader(f))
    tracker_ids = {row["feature_id"] for row in tracker_rows}

    assert "## Example user stories" in readme
    assert "docs/qa/feature-user-story-status.csv" in readme
    assert "docs/qa/dashboard-user-story-status.csv" in readme
    assert "supporting detail ledger" in readme
    for feature_id in ("CLI-002", "CLI-026", "API-011"):
        assert feature_id in readme
    assert dashboard_rows
    assert {row["status"] for row in dashboard_rows} <= PASS_STATUSES
    required_ids = ("DASH-001", "DASH-007", "API-011")
    assert [row_id for row_id in required_ids if row_id not in tracker_ids] == []

    required_surface_markers = (
        "ctx.api and ctx top-level re-exports",
        "ctx__recommend_bundle, ctx__graph_query, ctx__wiki_search, ctx__wiki_get",
        "ctx__observe_dev_event, ctx__load_entity, ctx__mark_entity_used",
        "McpClient and McpRouter",
        "output_format and _response_format",
    )
    tracker = _tracker_text()

    assert [marker for marker in required_surface_markers if marker not in tracker] == []
    python_api_rows = _rows_for_surface(tracker_rows, "Python API")
    python_api_text = " ".join(_row_text(row) for row in python_api_rows)
    public_api_names = sorted(
        set(ctx_api.__all__)
        | {
            name
            for name in ctx.__all__
            if name != "__version__"
            and hasattr(ctx_api, name)
            and getattr(ctx, name) is getattr(ctx_api, name)
        }
    )
    assert python_api_rows
    assert [name for name in public_api_names if name not in python_api_text] == []
    for marker in ("src/ctx/api.py", "src/ctx/__init__.py", "src/tests/test_public_api.py"):
        assert marker in python_api_text

    mcp_core_rows = _rows_for_surface(tracker_rows, "MCP/Core Tools")
    assert mcp_core_rows
    tool_names = sorted(
        definition.name
        for definition in ctx_api.CtxCoreToolbox().tool_definitions()
    )
    assert [
        name
        for name in tool_names
        if not any(name in _row_text(row) for row in mcp_core_rows)
    ] == []
