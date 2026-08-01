from __future__ import annotations

import ast
import csv
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml
from yaml.nodes import ScalarNode

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))

import ctx  # noqa: E402
import ctx.api as ctx_api  # noqa: E402
from ctx.monitor import routes as monitor_routes  # noqa: E402
from scripts.ci_preflight import PUBLIC_DOCS_TRACKER_TESTS  # noqa: E402
from scripts.ci_preflight import select_checks  # noqa: E402

TRACKER = repo_root / "docs" / "qa" / "feature-user-story-status.csv"
DASHBOARD_TRACKER = repo_root / "docs" / "qa" / "dashboard-user-story-status.csv"
TOOL_SELECTION_TRACKER = repo_root / "qa" / "tool-selection-token-history" / "tracker.csv"
CANONICAL_TRACKER = repo_root / "qa" / "feature_status.csv"
BUG_SMOKE_TRACKER = repo_root / "qa" / "bug_smoke_status.csv"
BENCHMARK_TRACKER = repo_root / "qa" / "ctx_benchmark_status.csv"
SOURCE_ROOT = repo_root / "src"
MKDOCS = repo_root / "mkdocs.yml"
README = repo_root / "README.md"
PASS_STATUSES = {"Tested Pass", "Retested Pass"}
VALIDATION_STATUSES = {"Needs Validation"}
FIX_STATUSES = {"Needs Fix"}
ACTIONABLE_STATUSES = (
    PASS_STATUSES
    | VALIDATION_STATUSES
    | FIX_STATUSES
    | {
        "Blocked/Human Decision",
    }
)
CANONICAL_STATUSES = ACTIONABLE_STATUSES | {
    "Needs Story",
    "Blocked",
    "Blocked/Human Decision",
    "Deprecated",
}
CANONICAL_STATUS_OVERRIDES: dict[str, dict[str, str]] = {
    "CLI-032": {
        "source_status": "Tested Pass",
        "canonical_status": "Retested Pass",
        "owner_lane": "Security/Supply Chain Lane",
        "review_note": "bounded registry contract",
        "validation_status": "commit 137135f7",
    },
}
ALLOWED_UNPARENTED_DASHBOARD_API_DUPLICATE_ROUTES: frozenset[str] = frozenset()
RETIRED_STALE_TRACKER_PHRASES = (
    "pending no-mistakes",
    "uncommitted in-memory lease patch",
    "lacks a production-ctx-run engine",
    "production engine is not implemented",
    "needs implementation commit",
    "requires a real ctx-run engine",
)
PROJECTION_PARENTS = {
    "DASH-PAGE-001": "DASH-001",
    "DASH-PAGE-002": "DASH-002",
    "DASH-PAGE-003": "DASH-003",
    "DASH-PAGE-004": "DASH-004",
    "DASH-PAGE-005": "DASH-005",
    "DASH-PAGE-006": "DASH-007",
    "DASH-PAGE-007": "DASH-008",
    "DASH-PAGE-008": "DASH-009",
    "DASH-PAGE-009": "DASH-010",
    "DASH-PAGE-010": "DASH-011",
    "DASH-PAGE-011": "DASH-012",
    "DASH-PAGE-012": "DASH-013",
    "DASH-PAGE-013": "DASH-014",
    "DASH-PAGE-014": "DASH-015",
    "DASH-PAGE-015": "DASH-016",
    "DASH-PAGE-016": "DASH-017",
    "DASH-PAGE-017": "DASH-017",
    "DASH-PAGE-018": "DASH-005",
    "DASH-PAGE-019": "DASH-005",
    "DASH-PAGE-020": "DASH-018",
    "DASH-PAGE-021": "DASH-019",
    "DASH-PAGE-022": "DASH-006",
    "GRAPH-004": "CLI-036",
    "GRAPH-005": "CLI-034",
    "HARNESS-002": "CLI-026",
    "SEC-001": "DIST-004",
}


def _tracker_rows() -> list[dict[str, str]]:
    with TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _dashboard_tracker_rows() -> list[dict[str, str]]:
    with DASHBOARD_TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _tool_selection_tracker_rows() -> list[dict[str, str]]:
    with TOOL_SELECTION_TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _canonical_tracker_rows() -> list[dict[str, str]]:
    with CANONICAL_TRACKER.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _supporting_tracker_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _canonical_root(feature_id: str, rows_by_id: dict[str, dict[str, str]]) -> str:
    seen: set[str] = set()
    current = feature_id
    while True:
        assert current not in seen, f"canonical parent cycle includes {current}"
        seen.add(current)
        parent = rows_by_id[current]["parent_feature_id"]
        if not parent:
            return current
        assert parent in rows_by_id, f"{current} references missing parent {parent}"
        current = parent


def _supporting_ids(value: str) -> set[str]:
    return set(re.findall(r"\b(?:AUDIT|BENCH)-\d{3}\b", value))


def _is_substantive_python_module(path: Path) -> bool:
    relative = path.relative_to(SOURCE_ROOT)
    if relative == Path("__init__.py") or relative.parts[0] == "tests":
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    meaningful = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    return path.name != "__init__.py" or bool(meaningful)


def _tracker_text() -> str:
    return "\n".join(" ".join(row.values()) for row in _tracker_rows())


def _row_text(row: dict[str, str]) -> str:
    return " ".join(value for value in row.values() if value)


def _rows_for_surface(rows: list[dict[str, str]], surface: str) -> list[dict[str, str]]:
    return [row for row in rows if row["surface"] == surface]


class _MkDocsNavLoader(yaml.SafeLoader):
    pass


def _mkdocs_python_name(
    loader: _MkDocsNavLoader,
    suffix: str,  # noqa: ARG001
    node: yaml.Node,
) -> str:
    if not isinstance(node, ScalarNode):
        raise TypeError(f"Expected scalar YAML node, got {type(node).__name__}")
    return loader.construct_scalar(node)


_MkDocsNavLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:",
    _mkdocs_python_name,
)


def _nav_markdown_paths(nav_items: list[object]) -> list[str]:
    paths: list[str] = []
    for item in nav_items:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            for value in item.values():
                if isinstance(value, str):
                    paths.append(value)
                elif isinstance(value, list):
                    paths.extend(_nav_markdown_paths(value))
    return [path for path in paths if path.endswith(".md")]


def _mkdocs_nav_markdown_paths() -> list[str]:
    config = yaml.load(
        MKDOCS.read_text(encoding="utf-8"),
        Loader=_MkDocsNavLoader,
    )
    docs_dir = config.get("docs_dir", "docs")
    nav = config["nav"]
    return list(dict.fromkeys(f"{docs_dir}/{path}" for path in _nav_markdown_paths(nav)))


def _relative_file_paths(root: Path, pattern: str) -> list[str]:
    return [
        path.relative_to(repo_root).as_posix()
        for path in sorted(root.glob(pattern))
        if path.is_file()
    ]


def _workflow_pytest_paths(workflow_path: Path, step_name: str) -> tuple[str, ...]:
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    runs = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if step.get("name") == step_name
    ]
    assert len(runs) == 1
    command = " ".join(line.rstrip("\\").strip() for line in runs[0].splitlines() if line.strip())
    argv = shlex.split(command)

    assert argv[:5] == ["python", "-m", "pytest", "-q", "--no-cov"]
    return tuple(arg for arg in argv[5:] if arg.startswith("src/tests/"))


def _contains_contiguous_slice(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return any(haystack[index : index + len(needle)] == needle for index in range(len(haystack)))


def test_canonical_feature_status_tracker_merges_supporting_ledgers() -> None:
    rows = _canonical_tracker_rows()
    feature_rows = _tracker_rows()
    dashboard_rows = _dashboard_tracker_rows()
    tool_selection_rows = _tool_selection_tracker_rows()
    expected_ids = (
        {row["feature_id"] for row in feature_rows}
        | {row["dashboard_id"] for row in dashboard_rows}
        | {row["ID"] for row in tool_selection_rows}
    )
    supporting_statuses = {row["feature_id"]: row["status"] for row in feature_rows}
    supporting_statuses.update({row["dashboard_id"]: row["status"] for row in dashboard_rows})
    supporting_statuses.update({row["ID"]: row["Status"] for row in tool_selection_rows})
    required = (
        "feature_id",
        "source_tracker",
        "surface",
        "feature",
        "entrypoint_or_route",
        "source_evidence",
        "risk_level",
        "user_story",
        "expected_behavior",
        "test_command_or_steps",
        "verification_mode",
        "status",
        "evidence",
        "last_verified_at",
        "owner_lane",
        "review_status",
        "review_notes",
        "fix_strategy",
        "validation_status",
    )

    assert rows
    canonical_ids = {row["feature_id"] for row in rows}
    tracked_paths = set(
        subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    assert expected_ids <= canonical_ids
    assert len(rows) == len(canonical_ids)
    for row in rows:
        assert None not in row, f"{row.get('feature_id', '<unknown>')} has extra CSV columns"
        for key in required:
            assert row[key].strip(), f"{row.get('feature_id', '<unknown>')} missing {key}"
        evidence_text = f"{row['source_evidence']} {row['test_command_or_steps']}"
        evidence_paths = re.findall(
            r"(?:(?:src|scripts|hooks|docs|qa)/|\.github/)[A-Za-z0-9_./*?{}-]+",
            evidence_text,
        )
        for evidence_path in evidence_paths:
            if any(marker in evidence_path for marker in "*?{}"):
                continue
            path = repo_root / evidence_path
            assert path.exists(), (
                f"{row['feature_id']} references missing evidence path {evidence_path}"
            )
            if row["status"] in PASS_STATUSES:
                tracked = evidence_path in tracked_paths
                if path.is_dir():
                    prefix = evidence_path.rstrip("/") + "/"
                    tracked = any(candidate.startswith(prefix) for candidate in tracked_paths)
                assert tracked, (
                    f"{row['feature_id']} completed row references untracked evidence "
                    f"{evidence_path}"
                )
        assert row["source_tracker"] in {
            "docs/qa/feature-user-story-status.csv",
            "docs/qa/dashboard-user-story-status.csv",
            "qa/tool-selection-token-history/tracker.csv",
            "expert-lane/api-mcp-harness",
            "expert-lane/telemetry-release-governance",
            "expert-lane/cli-package-inventory",
        }
        assert row["risk_level"] in {"Low", "Medium", "High", "Critical"}
        assert row["status"] in CANONICAL_STATUSES
        source_status = supporting_statuses.get(row["feature_id"])
        if source_status is not None:
            if row["status"] == source_status:
                assert row["feature_id"] not in CANONICAL_STATUS_OVERRIDES
            else:
                override = CANONICAL_STATUS_OVERRIDES.get(row["feature_id"])
                assert override is not None, (
                    f"{row['feature_id']} status differs from supporting ledger without "
                    "a canonical override contract"
                )
                assert source_status == override["source_status"]
                assert row["status"] == override["canonical_status"]
                assert row["owner_lane"] == override["owner_lane"]
                assert override["review_note"] in row["review_notes"].lower()
                assert override["validation_status"] in row["validation_status"].lower()
        if row["status"] in FIX_STATUSES or row["bug_summary"]:
            for key in ("bug_id", "bug_summary", "bug_repro", "fix_status"):
                assert row[key].strip(), (
                    f"{row.get('feature_id', '<unknown>')} has bug evidence without {key}"
                )
        if row["status"] == "Blocked/Human Decision":
            assert row["owner_lane"] == "Human Owner"
            assert "out of scope" in row["validation_status"].lower()

    assert set(CANONICAL_STATUS_OVERRIDES) <= canonical_ids


def test_canonical_tracker_has_valid_parent_graph() -> None:
    rows = _canonical_tracker_rows()
    rows_by_id = {row["feature_id"]: row for row in rows}

    assert rows_by_id["LANE-D-039"]["parent_feature_id"] == ""
    for feature_id in rows_by_id:
        _canonical_root(feature_id, rows_by_id)


def test_canonical_tracker_resolves_projection_collisions() -> None:
    rows = _canonical_tracker_rows()
    rows_by_id = {row["feature_id"]: row for row in rows}
    rows_by_entrypoint: dict[str, list[str]] = {}
    for row in rows:
        rows_by_entrypoint.setdefault(row["entrypoint_or_route"], []).append(row["feature_id"])

    for feature_id, parent_id in PROJECTION_PARENTS.items():
        assert rows_by_id[feature_id]["parent_feature_id"] == parent_id

    for entrypoint, feature_ids in rows_by_entrypoint.items():
        if len(feature_ids) < 2:
            continue
        roots = {_canonical_root(feature_id, rows_by_id) for feature_id in feature_ids}
        assert len(roots) == 1, (
            f"{entrypoint} duplicate tracker rows resolve to multiple canonical roots: "
            f"{sorted(roots)}"
        )


def test_canonical_tracker_links_nonterminal_supporting_ledgers() -> None:
    canonical_rows = _canonical_tracker_rows()
    bug_rows = _supporting_tracker_rows(BUG_SMOKE_TRACKER)
    benchmark_rows = _supporting_tracker_rows(BENCHMARK_TRACKER)
    references = {
        reference for row in canonical_rows for reference in _supporting_ids(row["bug_id"])
    }
    known_bug_ids = {row["finding_id"] for row in bug_rows}
    known_benchmark_ids = {row["id"] for row in benchmark_rows}
    nonterminal_bug_ids = {
        row["finding_id"]
        for row in bug_rows
        if row["status"] not in {"Retested Pass", "False Positive"}
    }
    nonterminal_benchmark_ids = {row["id"] for row in benchmark_rows if row["status"] != "Resolved"}
    canonical_by_reference: dict[str, list[dict[str, str]]] = {}
    for row in canonical_rows:
        for reference in _supporting_ids(row["bug_id"]):
            canonical_by_reference.setdefault(reference, []).append(row)

    assert references <= known_bug_ids | known_benchmark_ids
    assert nonterminal_bug_ids <= references
    assert nonterminal_benchmark_ids <= references
    for reference in nonterminal_bug_ids | nonterminal_benchmark_ids:
        linked_rows = canonical_by_reference[reference]
        assert any(row["status"] not in PASS_STATUSES | {"Deprecated"} for row in linked_rows), (
            f"{reference} remains nonterminal but all linked canonical features are closed"
        )


def test_canonical_retested_pass_rows_have_retest_evidence() -> None:
    for row in _canonical_tracker_rows():
        if row["status"] == "Retested Pass":
            assert row["retest_evidence"].startswith("PASS:"), (
                f"{row['feature_id']} Retested Pass lacks PASS: retest evidence"
            )


def test_canonical_trackers_have_no_retired_stale_completion_prose() -> None:
    for path in (CANONICAL_TRACKER, BUG_SMOKE_TRACKER, BENCHMARK_TRACKER):
        tracker = path.read_text(encoding="utf-8").lower()
        stale = [
            phrase
            for phrase in RETIRED_STALE_TRACKER_PHRASES
            if re.search(rf"\b{re.escape(phrase)}\b", tracker)
        ]
        assert stale == []


def test_retired_windows_gate_is_not_an_active_tracker_contract() -> None:
    tracked_ids = {
        "DIST-002",
        "MAINT-015",
        "MAINT-003",
        "MAINT-005",
        "LANE-D-014",
        "LANE-D-015",
        "LANE-D-018",
        "MAINT-016",
        "B-MCP-003",
    }
    canonical_active_fields = (
        "user_story",
        "expected_behavior",
        "setup_preconditions",
        "test_command_or_steps",
        "fix_status",
        "notes",
        "review_status",
        "review_notes",
        "fix_strategy",
        "validation_status",
    )
    supporting_active_fields = (
        "user_story",
        "expected_behavior",
        "setup_preconditions",
        "test_command_or_steps",
        "error_summary",
        "fix_status",
        "notes",
    )
    retired_gate_markers = (
        "windows-high-risk",
        "windows_changed",
        "native windows",
        "windows 3.12",
        "windows importer",
        "windows skip",
    )

    canonical_rows = {
        row["feature_id"]: row
        for row in _canonical_tracker_rows()
        if row["feature_id"] in tracked_ids
    }
    assert canonical_rows.keys() == tracked_ids
    for feature_id, row in canonical_rows.items():
        active_contract = " ".join(row[field] for field in canonical_active_fields).lower()
        assert not any(marker in active_contract for marker in retired_gate_markers), (
            f"{feature_id} still advertises the retired Windows gate in active fields"
        )

    supporting_ids = {"DIST-002", "MAINT-016"}
    supporting_rows = {
        row["feature_id"]: row for row in _tracker_rows() if row["feature_id"] in supporting_ids
    }
    assert supporting_rows.keys() == supporting_ids
    for feature_id, row in supporting_rows.items():
        active_contract = " ".join(row[field] for field in supporting_active_fields).lower()
        assert not any(marker in active_contract for marker in retired_gate_markers), (
            f"{feature_id} supporting row still advertises the retired Windows gate"
        )

    benchmark_ids = {"BENCH-010", "BENCH-034", "BENCH-039", "BENCH-040", "BENCH-042"}
    benchmark_rows = {
        row["id"]: row
        for row in _supporting_tracker_rows(BENCHMARK_TRACKER)
        if row["id"] in benchmark_ids
    }
    assert benchmark_rows.keys() == benchmark_ids
    audit_rows = {
        row["finding_id"]: row
        for row in _supporting_tracker_rows(BUG_SMOKE_TRACKER)
        if row["finding_id"] == "AUDIT-079"
    }
    assert audit_rows.keys() == {"AUDIT-079"}
    active_obligations = " ".join(
        [
            *(
                row[field]
                for row in benchmark_rows.values()
                for field in ("expected_behavior", "repro", "fix")
            ),
            *(
                row[field]
                for row in audit_rows.values()
                for field in (
                    "expected_behavior",
                    "repro_or_detection",
                    "fix_strategy",
                    "review_status",
                    "next_action",
                )
            ),
        ]
    ).lower()
    assert "native windows" not in active_obligations
    assert "windows job" not in active_obligations
    assert "posix and windows" not in active_obligations


def test_canonical_tracker_attributes_every_substantive_python_module() -> None:
    exact_source_paths = {
        match
        for row in _canonical_tracker_rows()
        for match in re.findall(
            r"\bsrc/[A-Za-z0-9_./-]+\.py\b",
            row["source_evidence"],
        )
    }
    production_modules = {
        path.relative_to(repo_root).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if _is_substantive_python_module(path)
    }

    assert sorted(production_modules - exact_source_paths) == []


def test_canonical_dashboard_api_duplicate_routes_are_modeled() -> None:
    rows_by_route: dict[str, list[dict[str, str]]] = {}
    for row in _canonical_tracker_rows():
        rows_by_route.setdefault(row["entrypoint_or_route"], []).append(row)

    for route, rows in rows_by_route.items():
        if route in ALLOWED_UNPARENTED_DASHBOARD_API_DUPLICATE_ROUTES:
            continue
        if not route.startswith("/api/") or len(rows) < 2:
            continue
        if {row["surface"] for row in rows} != {"Dashboard API"}:
            continue

        row_ids = {row["feature_id"] for row in rows}
        roots = [row for row in rows if not row["parent_feature_id"]]
        children = [row for row in rows if row["parent_feature_id"] in row_ids]
        assert len(roots) == 1, f"{route} must have exactly one unparented canonical root"
        assert roots[0]["source_tracker"] == "docs/qa/feature-user-story-status.csv"
        assert len(children) == len(rows) - 1, (
            f"{route} duplicate Dashboard API rows must parent-link to the canonical root"
        )
        assert {row["parent_feature_id"] for row in children} == {roots[0]["feature_id"]}


def test_canonical_doc_nav_rows_have_page_specific_evidence() -> None:
    for row in _canonical_tracker_rows():
        if not row["feature_id"].startswith("DOC-NAV-"):
            continue

        route = row["entrypoint_or_route"]
        assert route.startswith("docs/"), f"{row['feature_id']} does not point at docs/"
        assert route in row["evidence"], (
            f"{row['feature_id']} evidence must name the specific nav page"
        )


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
                    f"{row.get('feature_id', '<unknown>')} has {row['status']} without {key}"
                )
        if row["status"] in VALIDATION_STATUSES:
            assert row["notes"].strip(), (
                f"{row.get('feature_id', '<unknown>')} needs validation without a validation note"
            )


def test_feature_user_story_tracker_covers_all_console_scripts() -> None:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = sorted(pyproject["project"]["scripts"])
    tracker = _tracker_text()
    required_runtime_markers = (
        "--planner",
        "--evaluator",
        "--contract",
        "--restore-session-mcp",
        "credential_env",
        "ctx-harness-install --update",
        "ctx-harness-install --uninstall",
        "ctx.cli.run",
        "ctx.runtime_lifecycle.record",
    )

    assert scripts
    assert [script for script in scripts if script not in tracker] == []
    assert [marker for marker in required_runtime_markers if marker not in tracker] == []


def test_canonical_feature_status_tracks_harness_install_safety_flags() -> None:
    tracker = "\n".join(_row_text(row) for row in _canonical_tracker_rows())
    required_flags = ("--approve-commands", "--run-verify", "--keep-files")

    assert [flag for flag in required_flags if flag not in tracker] == []


def test_feature_user_story_tracker_covers_monitor_route_inventory() -> None:
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


def test_feature_user_story_tracker_covers_distribution_workflows() -> None:
    workflow_dir = repo_root / ".github" / "workflows"
    workflows = sorted(
        path.relative_to(repo_root).as_posix()
        for path in workflow_dir.iterdir()
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    )
    tracker = "\n".join(_row_text(row) for row in _canonical_tracker_rows())
    hf_workflow = (workflow_dir / "huggingface-sync.yml").read_text(encoding="utf-8")

    assert workflows
    assert [workflow for workflow in workflows if workflow not in tracker] == []
    assert ".github/dependabot.yml" in tracker
    docs_tracker_tests = _workflow_pytest_paths(
        workflow_dir / "docs.yml",
        "Validate public docs tracker",
    )
    publish_canary_tests = _workflow_pytest_paths(
        workflow_dir / "publish.yml",
        "Release canary tests",
    )

    assert docs_tracker_tests == PUBLIC_DOCS_TRACKER_TESTS
    assert _contains_contiguous_slice(publish_canary_tests, PUBLIC_DOCS_TRACKER_TESTS)
    assert "github.repository == 'stevesolun/ctx'" in hf_workflow
    assert "Missing HF_TOKEN" in hf_workflow


def test_feature_user_story_tracker_covers_maintainer_scripts() -> None:
    scripts = sorted((repo_root / "scripts").glob("*.py"))
    tracker = "\n".join(_row_text(row) for row in _canonical_tracker_rows())
    script_paths = [script.relative_to(repo_root).as_posix() for script in scripts]
    hook_paths = _relative_file_paths(repo_root / "hooks", "*.py")

    assert scripts
    assert hook_paths
    assert [path for path in script_paths if path not in tracker] == []
    assert [path for path in hook_paths if path not in tracker] == []


def test_feature_user_story_tracker_covers_public_docs_assets() -> None:
    asset_paths = _relative_file_paths(repo_root / "docs" / "assets" / "javascripts", "*.js")
    service_paths = _relative_file_paths(repo_root / "docs" / "services", "**/*")
    toolbox_template_paths = _relative_file_paths(
        repo_root / "docs" / "toolbox" / "templates",
        "*.json",
    )
    tracker_rows = _canonical_tracker_rows()
    tracker = "\n".join(_row_text(row) for row in tracker_rows)
    public_asset_paths = asset_paths + service_paths + toolbox_template_paths
    nav_doc_paths = _mkdocs_nav_markdown_paths()

    assert asset_paths
    assert service_paths
    assert toolbox_template_paths
    assert [path for path in public_asset_paths if path not in tracker] == []
    assert nav_doc_paths
    assert [
        path
        for path in nav_doc_paths
        if not any(row["entrypoint_or_route"] == path for row in tracker_rows)
    ] == []
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=[toolbox_template_paths[0]],
        profile="pr",
        python=sys.executable,
    )
    assert "public docs tracker" in [check.name for check in checks]


def test_readme_shows_user_story_examples_from_tracker() -> None:
    readme = README.read_text(encoding="utf-8")
    tracker_rows = _tracker_rows()
    with DASHBOARD_TRACKER.open(newline="", encoding="utf-8") as f:
        dashboard_rows = list(csv.DictReader(f))
    tracker_ids = {row["feature_id"] for row in tracker_rows}

    assert "## Example user stories" in readme
    assert "qa/feature_status.csv" in readme
    assert "docs/qa/feature-user-story-status.csv" in readme
    assert "docs/qa/dashboard-user-story-status.csv" in readme
    assert "qa/tool-selection-token-history/tracker.csv" in readme
    assert "supporting detail ledger" in readme
    for feature_id in ("CLI-002", "CLI-026", "API-011"):
        assert feature_id in readme
    assert dashboard_rows
    assert {row["status"] for row in dashboard_rows} <= PASS_STATUSES
    required_ids = ("DASH-001", "DASH-007", "API-011")
    assert [row_id for row_id in required_ids if row_id not in tracker_ids] == []

    required_surface_markers = (
        "ctx.api and ctx top-level re-exports",
        "ctx__recommend_bundle, ctx__recommend_related, ctx__graph_query, ctx__wiki_search, ctx__wiki_get",
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
        definition.name for definition in ctx_api.CtxCoreToolbox().tool_definitions()
    )
    assert [
        name for name in tool_names if not any(name in _row_text(row) for row in mcp_core_rows)
    ] == []

    inventory_match = re.search(r"Tests-([0-9]+)_inventory", readme)
    assert inventory_match is not None
    expected = f"{int(inventory_match.group(1)):,}"
    claim_pattern = re.compile(
        r"\bcurrent(?:\s+at)?\s+([0-9][0-9,]*)\s+(?:test\s+)?inventory\b",
        re.IGNORECASE,
    )

    claims: list[tuple[str, str]] = []
    for row in [*tracker_rows, *_canonical_tracker_rows()]:
        for value in row.values():
            if not value:
                continue
            claims.extend((row["feature_id"], match) for match in claim_pattern.findall(value))

    assert [(feature_id, count) for feature_id, count in claims if count != expected] == []
