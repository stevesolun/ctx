from __future__ import annotations

import ast
import csv
import json
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

import yaml
from yaml.nodes import ScalarNode

repo_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo_root / "src"))

import ctx  # noqa: E402
import ctx.api as ctx_api  # noqa: E402
from ctx.monitor import routes as monitor_routes  # noqa: E402
from ctx.mcp_server import server as mcp_server  # noqa: E402
from scripts.ci_preflight import PUBLIC_DOCS_TRACKER_TESTS  # noqa: E402
from scripts.ci_preflight import select_checks  # noqa: E402

CANONICAL_TRACKER = repo_root / "qa" / "feature_status.csv"
POINTER_TRACKERS = (
    repo_root / "docs" / "qa" / "feature-user-story-status.csv",
    repo_root / "docs" / "qa" / "dashboard-user-story-status.csv",
    repo_root / "docs" / "qa" / "user-behavior-stories.csv",
)
BUG_SMOKE_TRACKER = repo_root / "qa" / "bug_smoke_status.csv"
BENCHMARK_TRACKER = repo_root / "qa" / "ctx_benchmark_status.csv"
SOURCE_ROOT = repo_root / "src"
MKDOCS = repo_root / "mkdocs.yml"
README = repo_root / "README.md"

PASS_STATUSES = {"Tested Pass", "Retested Pass"}
CANONICAL_STATUSES = {
    *PASS_STATUSES,
    "Needs Validation",
    "Needs Fix",
    "Needs Story",
    "Blocked",
    "Blocked/Human Decision",
    "Deprecated",
}
VERIFICATION_MODES = {"argv", "checklist", "manual", "deprecated"}
PRODUCT_STORIES = {
    "FIT-001": "ctx fit",
    "FIT-002": "ctx; ctx fit; ctx fit --json",
    "FIT-003": "ctx doctor",
    "FIT-004": "ctx advanced run|resume|sessions",
    "FIT-005": "ctx fit --dry-run; ctx fit --test --budget USD [--yes]",
    "FIT-006": "ctx fit --test --budget USD --apply [--yes]",
    "FIT-007": "ctx fit --test --budget USD --pr [--yes]",
}
LEGACY_INTERNAL_IDS = {"ENGINE-001", "ENGINE-002", "MAINT-016", "MAINT-020"}
RETIRED_STALE_TRACKER_PHRASES = (
    "pending no-mistakes",
    "uncommitted in-memory lease patch",
    "production engine is not implemented",
    "needs implementation commit",
)


def _canonical_rows() -> list[dict[str, str]]:
    with CANONICAL_TRACKER.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _supporting_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _row_text(row: dict[str, str]) -> str:
    return " ".join(value for value in row.values() if value)


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
    config = yaml.load(MKDOCS.read_text(encoding="utf-8"), Loader=_MkDocsNavLoader)
    docs_dir = config.get("docs_dir", "docs")
    return [f"{docs_dir}/{path}" for path in _nav_markdown_paths(config["nav"])]


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
    argv = command.split()
    return tuple(arg for arg in argv if arg.startswith("src/tests/"))


def _contains_contiguous_slice(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return any(haystack[index : index + len(needle)] == needle for index in range(len(haystack)))


def test_canonical_tracker_is_the_only_authored_authority() -> None:
    rows = _canonical_rows()
    assert rows
    assert {row["source_tracker"] for row in rows} == {"qa/feature_status.csv"}

    for path in POINTER_TRACKERS:
        pointer_rows = _supporting_rows(path)
        assert pointer_rows == [
            {
                "artifact_kind": (
                    "generated-view-pointer"
                    if path.name == "user-behavior-stories.csv"
                    else "historical-generated-pointer"
                ),
                "canonical_tracker": "qa/feature_status.csv",
                "authority": "false",
                "notes": pointer_rows[0]["notes"],
            }
        ]
        assert (
            "only current authority" in pointer_rows[0]["notes"]
            or "no independently authored" in pointer_rows[0]["notes"]
        )


def test_canonical_tracker_schema_paths_status_and_freshness_are_valid() -> None:
    rows = _canonical_rows()
    required = (
        "feature_id",
        "parent_feature_id",
        "source_tracker",
        "surface",
        "feature",
        "entrypoint_or_route",
        "source_evidence",
        "risk_level",
        "user_story",
        "expected_behavior",
        "setup_preconditions",
        "test_command_or_steps",
        "verification_mode",
        "status",
        "evidence",
        "last_verified_at",
        "verified_at_commit",
        "owner_lane",
        "review_status",
        "review_notes",
        "fix_strategy",
        "validation_status",
    )
    workspace_paths = set(
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    )
    assert len(rows) == len({row["feature_id"] for row in rows})
    for row in rows:
        assert None not in row, f"{row.get('feature_id', '<unknown>')} has extra CSV columns"
        for key in required:
            if key in {"parent_feature_id", "last_verified_at", "verified_at_commit"}:
                continue
            assert row[key].strip(), f"{row['feature_id']} missing {key}"
        assert row["risk_level"] in {"Low", "Medium", "High", "Critical"}
        assert row["status"] in CANONICAL_STATUSES
        assert row["verification_mode"] in VERIFICATION_MODES
        evidence_text = f"{row['source_evidence']} {row['test_command_or_steps']}"
        evidence_paths = re.findall(
            r"(?:(?:src|scripts|hooks|docs|qa|benchmarks)/|\.github/)[A-Za-z0-9_./*?{}-]+",
            evidence_text,
        )
        for evidence_path in evidence_paths:
            if any(marker in evidence_path for marker in "*?{}"):
                continue
            path = repo_root / evidence_path
            assert path.exists(), f"{row['feature_id']} references missing {evidence_path}"
            tracked = evidence_path in workspace_paths
            if path.is_dir():
                prefix = evidence_path.rstrip("/") + "/"
                tracked = any(candidate.startswith(prefix) for candidate in workspace_paths)
            assert tracked, f"{row['feature_id']} references untracked {evidence_path}"

        if row["status"] in PASS_STATUSES:
            assert re.fullmatch(r"[0-9a-f]{40}", row["verified_at_commit"])
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["last_verified_at"])
            assert row["retest_evidence"].startswith("PASS:")
        elif row["status"] == "Needs Validation":
            assert row["last_verified_at"] == ""
            assert row["verified_at_commit"] == ""
            assert row["retest_evidence"] == ""
            assert row["evidence"] == "No current-tree evidence recorded."
        elif row["status"] == "Deprecated":
            assert row["verification_mode"] == "deprecated"


def test_verification_contracts_are_machine_parseable() -> None:
    for row in _canonical_rows():
        contract = json.loads(row["test_command_or_steps"])
        assert isinstance(contract, list)
        assert all(isinstance(item, str) and item.strip() for item in contract)
        assert not any("..." in item or "…" in item for item in contract)
        mode = row["verification_mode"]
        if mode == "argv":
            assert contract[:3] == [".venv/bin/python", "-m", "pytest"]
            assert "-q" in contract
            assert any(item.startswith("src/tests/") for item in contract)
            assert not any(item in {";", "&&", "||", "|"} for item in contract)
        elif mode in {"checklist", "manual"}:
            assert contract
        else:
            assert mode == "deprecated"
            assert contract == []


def test_parent_graph_and_exact_feature_routes_are_unique() -> None:
    rows = _canonical_rows()
    rows_by_id = {row["feature_id"]: row for row in rows}
    for feature_id in rows_by_id:
        _canonical_root(feature_id, rows_by_id)

    by_feature_route: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        key = (row["feature"].strip().casefold(), row["entrypoint_or_route"].strip().casefold())
        by_feature_route[key].append(row["feature_id"])
    duplicates = {key: ids for key, ids in by_feature_route.items() if len(ids) > 1}
    assert duplicates == {}


def test_current_product_stories_and_cli_truth_are_explicit() -> None:
    rows = {row["feature_id"]: row for row in _canonical_rows()}
    assert {
        feature_id: rows[feature_id]["entrypoint_or_route"] for feature_id in PRODUCT_STORIES
    } == PRODUCT_STORIES
    assert rows["CLI-001"]["parent_feature_id"] == "FIT-004"
    assert rows["CLI-001"]["entrypoint_or_route"] == "ctx run|resume|sessions compatibility aliases"
    assert rows["META-001"]["parent_feature_id"] == "FIT-002"
    assert "Fit doctor and advanced" in rows["META-001"]["expected_behavior"]
    for feature_id in LEGACY_INTERNAL_IDS:
        assert rows[feature_id]["surface"] == "Legacy/Internal"
        assert rows[feature_id]["status"] == "Deprecated"


def test_only_live_console_script_names_are_advertised() -> None:
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    live = set(pyproject["project"]["scripts"])
    advertised: dict[str, list[str]] = defaultdict(list)
    for row in _canonical_rows():
        for token in re.findall(
            r"(?<![\w./-])(ctx-[a-z0-9-]+)(?![\w-])",
            row["entrypoint_or_route"],
        ):
            advertised[token].append(row["feature_id"])
    assert sorted(set(advertised) - live) == []
    tracker = "\n".join(_row_text(row) for row in _canonical_rows())
    assert sorted(script for script in live if script not in tracker) == []


def test_canonical_tracker_attributes_every_substantive_python_module() -> None:
    exact_source_paths = {
        match
        for row in _canonical_rows()
        for match in re.findall(r"\bsrc/[A-Za-z0-9_./-]+\.py\b", row["source_evidence"])
    }
    production_modules = {
        path.relative_to(repo_root).as_posix()
        for path in SOURCE_ROOT.rglob("*.py")
        if _is_substantive_python_module(path)
    }
    assert sorted(production_modules - exact_source_paths) == []


def test_public_python_and_mcp_tool_surfaces_are_explicit() -> None:
    rows = _canonical_rows()
    python_api_text = " ".join(_row_text(row) for row in rows if row["surface"] == "Python API")
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
    assert [name for name in public_api_names if name not in python_api_text] == []

    mcp_text = " ".join(_row_text(row) for row in rows if row["surface"] == "MCP/Core Tools")
    default_names = {definition.name for definition in ctx_api.CtxCoreToolbox().tool_definitions()}
    opt_in_names = {
        definition.name
        for definition in ctx_api.CtxCoreToolbox(
            allowed_tool_names={"ctx__loop_provision", "ctx__loop_topup"}
        ).tool_definitions()
    }
    assert sorted(name for name in default_names | opt_in_names if name not in mcp_text) == []
    rows_by_id = {row["feature_id"]: row for row in rows}
    assert rows_by_id["MCP-005"]["entrypoint_or_route"] == "ctx__loop_provision, ctx__loop_topup"
    assert "hidden by default" in rows_by_id["MCP-005"]["expected_behavior"]

    server_story = rows_by_id["CLI-024"]
    assert set(mcp_server._HANDLERS) == {  # noqa: SLF001
        "initialize",
        "ping",
        "tools/list",
        "tools/call",
    }
    assert "neither resources nor prompts" in server_story["expected_behavior"]
    assert "src/tests/test_mcp_server.py" in json.loads(server_story["test_command_or_steps"])


def test_monitor_route_inventory_is_covered() -> None:
    route_patterns: list[str] = []
    route_patterns.extend(href for _key, _label, href in monitor_routes.NAV_ROUTES)
    route_patterns.extend(sorted(monitor_routes.PAGE_ROUTES))
    route_patterns.extend(sorted(monitor_routes.GET_API_ROUTES))
    route_patterns.extend(monitor_routes.GET_API_PATTERNS)
    route_patterns.extend(sorted(monitor_routes.POST_API_ROUTES))
    route_patterns.extend(("/session/<session_id>", "/skill/<slug>", "/wiki/<slug>"))
    tracker = "\n".join(_row_text(row) for row in _canonical_rows())
    assert [route for route in dict.fromkeys(route_patterns) if route not in tracker] == []


def test_workflows_maintainer_scripts_docs_and_package_assets_are_covered() -> None:
    tracker = "\n".join(_row_text(row) for row in _canonical_rows())
    workflow_dir = repo_root / ".github" / "workflows"
    workflows = sorted(
        path.relative_to(repo_root).as_posix()
        for path in workflow_dir.iterdir()
        if path.is_file() and path.suffix in {".yml", ".yaml"}
    )
    scripts = [
        path.relative_to(repo_root).as_posix()
        for path in sorted((repo_root / "scripts").glob("*.py"))
    ]
    hooks = _relative_file_paths(repo_root / "hooks", "*.py")
    nav_docs = _mkdocs_nav_markdown_paths()
    public_assets = [
        *_relative_file_paths(repo_root / "docs" / "assets" / "javascripts", "*.js"),
        *_relative_file_paths(repo_root / "docs" / "services", "**/*"),
        *_relative_file_paths(repo_root / "docs" / "toolbox" / "templates", "*.json"),
    ]
    package_assets = [
        "src/ctx/config.json",
        "src/ctx/skill-registry.json",
        *[
            path.relative_to(repo_root).as_posix()
            for path in sorted((repo_root / "src" / "ctx" / "assets").iterdir())
            if path.is_file() and path.name != "__init__.py"
        ],
    ]
    for collection in (workflows, scripts, hooks, nav_docs, public_assets, package_assets):
        assert [path for path in collection if path not in tracker] == []

    docs_tracker_tests = _workflow_pytest_paths(
        workflow_dir / "docs.yml", "Validate public docs tracker"
    )
    publish_canary_tests = _workflow_pytest_paths(
        workflow_dir / "publish.yml", "Release canary tests"
    )
    assert docs_tracker_tests == PUBLIC_DOCS_TRACKER_TESTS
    assert _contains_contiguous_slice(publish_canary_tests, PUBLIC_DOCS_TRACKER_TESTS)
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=[public_assets[-1]],
        profile="pr",
        python=sys.executable,
    )
    assert "public docs tracker" in [check.name for check in checks]


def test_nonterminal_bug_and_benchmark_items_remain_linked() -> None:
    rows = _canonical_rows()
    references = {
        match for row in rows for match in re.findall(r"\b(?:AUDIT|BENCH)-\d{3}\b", row["bug_id"])
    }
    bug_rows = _supporting_rows(BUG_SMOKE_TRACKER)
    benchmark_rows = _supporting_rows(BENCHMARK_TRACKER)
    known = {row["finding_id"] for row in bug_rows} | {row["id"] for row in benchmark_rows}
    assert references <= known
    nonterminal = {
        row["finding_id"]
        for row in bug_rows
        if row["status"] not in {"Retested Pass", "False Positive"}
    } | {row["id"] for row in benchmark_rows if row["status"] != "Resolved"}
    assert nonterminal <= references


def test_canonical_tracker_has_no_retired_stale_completion_prose() -> None:
    for row in _canonical_rows():
        completion_state = " ".join(
            row[field]
            for field in (
                "evidence",
                "retest_evidence",
                "review_status",
                "review_notes",
                "validation_status",
            )
        ).lower()
        assert [
            phrase for phrase in RETIRED_STALE_TRACKER_PHRASES if phrase in completion_state
        ] == []


def test_readme_points_to_the_canonical_tracker() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "qa/feature_status.csv" in readme
