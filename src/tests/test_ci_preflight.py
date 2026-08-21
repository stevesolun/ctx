from __future__ import annotations

from datetime import datetime
import hashlib
import io
import json
import shlex
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from scripts import ci_preflight
from scripts import local_fast_gate
from scripts.ci_preflight import _run_no_test_policy_for_files
from scripts.ci_preflight import Check
from scripts.ci_preflight import PUBLIC_DOCS_TRACKER_TESTS
from scripts.ci_preflight import select_checks
from ctx.core.graph import release_artifacts


def _checks_for(files: list[str], *, profile: str = "pr") -> list[Check]:
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=files,
        profile=profile,
        python="python",
    )
    return checks


def _names_for(files: list[str], *, profile: str = "pr") -> list[str]:
    checks = _checks_for(files, profile=profile)
    return [check.name for check in checks]


def _workflow_docs_tracker_tests() -> tuple[str, ...]:
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["docs-check"]["steps"]
    run = next(
        step["run"] for step in steps if step.get("name") == "Run public docs tracker checks"
    )
    command = " ".join(line.rstrip("\\").strip() for line in run.splitlines() if line.strip())
    argv = shlex.split(command)

    assert argv[:5] == ["python", "-m", "pytest", "-q", "--no-cov"]
    return tuple(argv[5:])


def _workflow_unit_linux_coverage_command() -> list[str]:
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["unit-linux"]["steps"]
    run = next(step["run"] for step in steps if step.get("name") == "Run tests with coverage gate")
    return shlex.split(run)


def _workflow_step_names(job: str) -> list[str]:
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text(encoding="utf-8"))
    return [str(step.get("name", "")) for step in workflow["jobs"][job]["steps"]]


def test_pr_docs_workflow_tracker_tests_match_preflight() -> None:
    assert _workflow_docs_tracker_tests() == PUBLIC_DOCS_TRACKER_TESTS


def test_preflight_runs_docs_gate_for_docs_changes() -> None:
    names = _names_for(["docs/index.md"])
    tracker_checks = _checks_for(["qa/bug_smoke_status.csv"])
    tracker_names = [check.name for check in tracker_checks]
    public_docs_tracker = next(
        check for check in tracker_checks if check.name == "public docs tracker"
    )

    assert "repo stats" in names
    assert "docs strict build" in names
    assert "no-test policy" not in names
    assert "unit-linux equivalent" not in names
    assert "docs strict build" in tracker_names
    assert "unit-linux equivalent" not in tracker_names
    assert "src/tests/test_bug_smoke_tracker.py" in public_docs_tracker.argv
    assert "src/tests/test_dashboard_user_story_tracker.py" in public_docs_tracker.argv


def test_preflight_runs_source_gates_for_source_changes() -> None:
    names = _names_for(["src/ctx/adapters/generic/loop.py"])
    workflow_unit_command = _workflow_unit_linux_coverage_command()
    unit_check = next(
        check
        for check in _checks_for(["src/ctx/adapters/generic/loop.py"])
        if check.name == "unit-linux equivalent"
    )
    unit_hydrate = next(
        check
        for check in _checks_for(["src/ctx/adapters/generic/loop.py"])
        if check.name == "hydrate benchmark catalog"
    )
    lanes = local_fast_gate.group_checks(_checks_for(["src/ctx/adapters/generic/loop.py"]))
    lanes_by_name = {
        lane.name: [check.name for check in lane.checks]
        for lane in lanes
        if lane.name in {"unit", "canary", "contract", "clean-host"}
    }

    assert "no-test policy" in names
    assert "ruff format" in names
    assert "ruff" in names
    assert "mypy" in names
    assert "unit-linux equivalent" in names
    assert "A-Z canary" in names
    assert "clean host contract" in names
    assert lanes_by_name == {
        "unit": ["hydrate benchmark catalog", "unit-linux equivalent"],
        "canary": ["A-Z canary"],
        "contract": ["contract compatibility local"],
        "clean-host": ["clean host contract"],
    }
    assert unit_check.argv[-4:] == (
        "-n",
        "auto",
        "--dist=loadfile",
        "--max-worker-restart=0",
    )
    assert workflow_unit_command[-4:] == list(unit_check.argv[-4:])
    assert unit_hydrate.argv == (
        "python",
        "scripts/graph_release_manifest.py",
        "hydrate",
        "--manifest",
        "graph/release-artifacts.json",
        "--repo-root",
        ".",
        "--artifact",
        "graph/wiki-graph-runtime.tar.gz",
    )


@pytest.mark.parametrize("job", ["unit-linux", "test"])
def test_clean_checkout_test_jobs_hydrate_runtime_catalog_before_pytest(job: str) -> None:
    names = _workflow_step_names(job)

    assert names.index("Resolve benchmark catalog from exact release manifest") < names.index(
        "Run tests with coverage gate" if job == "unit-linux" else "Run tests without coverage"
    )


def test_xdist_auto_worker_count_is_resource_capped(monkeypatch) -> None:
    from tests import conftest

    monkeypatch.setattr(conftest.os, "cpu_count", lambda: 64)
    assert conftest._xdist_auto_worker_count() == 8

    monkeypatch.setattr(conftest.os, "cpu_count", lambda: 4)
    assert conftest._xdist_auto_worker_count() == 4

    monkeypatch.setattr(conftest.os, "cpu_count", lambda: None)
    assert conftest._xdist_auto_worker_count() == 1


def test_preflight_smoke_profile_runs_only_fast_source_gates() -> None:
    names = _names_for(["src/ctx/adapters/generic/loop.py"], profile="smoke")

    assert "whitespace" in names
    assert "repo stats" in names
    assert "no-test policy" in names
    assert "ruff format" in names
    assert "ruff" in names
    assert "mypy" not in names
    assert "unit-linux equivalent" not in names
    assert "build wheel" not in names


def test_preflight_smoke_profile_keeps_docs_tracker_without_strict_build() -> None:
    names = _names_for(["docs/index.md"], profile="smoke")

    assert "public docs tracker" in names
    assert "docs strict build" not in names


def test_preflight_runs_graph_validation_for_graph_artifacts() -> None:
    names = _names_for(["graph/wiki-graph.tar.gz"])
    lanes = local_fast_gate.group_checks(_checks_for(["graph/wiki-graph.tar.gz"]))
    graph_lane = next(lane for lane in lanes if lane.name == "graph")
    graph_names = [check.name for check in graph_lane.checks]

    assert names.index("hydrate graph release assets") < names.index("graph artifact validation")
    assert "graph artifact validation" in names
    assert "no-test policy" not in names
    assert "unit-linux equivalent" not in names
    assert graph_names == ["hydrate graph release assets", "graph artifact validation"]
    assert graph_lane.checks[0].argv == (
        "python",
        "scripts/graph_release_manifest.py",
        "hydrate",
        "--manifest",
        "graph/release-artifacts.json",
        "--repo-root",
        ".",
    )


def test_local_fast_whitespace_check_runs_against_base() -> None:
    whitespace = next(
        check for check in _checks_for(["src/ctx/cli/run.py"]) if check.name == "whitespace"
    )
    lanes = local_fast_gate.group_checks([whitespace])

    assert lanes[0].name == "cheap"
    assert lanes[0].checks[0].argv == (
        "python",
        "scripts/ci_preflight.py",
        "--base",
        "origin/main",
        "--internal-whitespace",
    )


def test_local_fast_lane_filters_are_composable() -> None:
    lanes = local_fast_gate.group_checks(_checks_for([".github/workflows/test.yml"]))

    filtered = local_fast_gate.filter_lanes(
        lanes,
        include=("static", "unit", "browser"),
        skip=("browser",),
    )

    assert [lane.name for lane in filtered] == ["static", "unit"]


def test_local_fast_reserves_cpu_for_nested_xdist(monkeypatch) -> None:
    monkeypatch.setattr(local_fast_gate.os, "cpu_count", lambda: 18)
    unit_check = Check(
        "unit-linux equivalent",
        ("python", "-m", "pytest", "-q", "-n", "auto", "--dist=loadfile"),
    )

    unit_lane = local_fast_gate.group_checks([unit_check])[0]

    assert unit_lane.checks[0].argv[-3:] == ("-n", "4", "--dist=loadfile")
    assert local_fast_gate._default_jobs() == 9


def test_local_fast_main_accepts_repeated_lane_args(monkeypatch, capsys) -> None:
    checks = [
        Check("ruff", ("python", "-m", "ruff", "check", "src")),
        Check("unit-linux equivalent", ("python", "-m", "pytest", "-q")),
        Check("build wheel", ("python", "-m", "build")),
    ]
    monkeypatch.setattr(local_fast_gate.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(local_fast_gate, "changed_files", lambda _base: ["src/ctx/cli/run.py"])
    monkeypatch.setattr(
        local_fast_gate,
        "select_checks",
        lambda **_kwargs: (checks, []),
    )

    assert local_fast_gate.main(["--dry-run", "--lane", "static", "--lane", "unit"]) == 0

    out = capsys.readouterr().out
    assert "[lane] static" in out
    assert "[lane] unit" in out
    assert "[lane] package" not in out


def test_local_fast_main_accepts_smoke_profile(monkeypatch, capsys) -> None:
    checks = [
        Check("ruff", ("python", "-m", "ruff", "check", "src")),
        Check("mypy", ("python", "-m", "mypy", "src")),
    ]
    seen_profiles: list[str] = []
    monkeypatch.setattr(local_fast_gate.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(local_fast_gate, "changed_files", lambda _base: ["src/ctx/cli/run.py"])

    def fake_select_checks(**kwargs):
        seen_profiles.append(kwargs["profile"])
        return checks[:1], []

    monkeypatch.setattr(local_fast_gate, "select_checks", fake_select_checks)

    assert local_fast_gate.main(["--dry-run", "--profile", "smoke"]) == 0

    assert seen_profiles == ["smoke"]
    assert "[lane] static" in capsys.readouterr().out


def test_local_fast_summary_json_records_lane_timings(tmp_path: Path) -> None:
    summary_path = tmp_path / "gate" / "summary.json"
    result = local_fast_gate.GateResult(
        returncode=0,
        elapsed=1.2345,
        worker_count=2,
        lanes=(
            local_fast_gate.LaneResult(
                name="cheap",
                returncode=0,
                elapsed=0.4567,
                check_count=3,
            ),
        ),
    )

    local_fast_gate.write_summary_json(
        summary_path,
        result,
        head_sha="a" * 40,
        base_ref="origin/main",
        base_sha="c" * 40,
        profile="pr",
        source_worktree_dirty_at_selection=False,
        changed_file_paths=["README.md"],
        started_at="2026-07-28T00:00:00+00:00",
        finished_at="2026-07-28T00:00:01+00:00",
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload == {
        "base_ref": "origin/main",
        "base_sha": "c" * 40,
        "changed_file_paths": ["README.md"],
        "committed_head_only": True,
        "elapsed_seconds": 1.234,
        "finished_at": "2026-07-28T00:00:01+00:00",
        "head_sha": "a" * 40,
        "lanes": [
            {
                "check_count": 3,
                "elapsed_seconds": 0.457,
                "name": "cheap",
                "returncode": 0,
                "worktree": None,
            }
        ],
        "profile": "pr",
        "returncode": 0,
        "schema_version": 2,
        "source_worktree_dirty_at_selection": False,
        "started_at": "2026-07-28T00:00:00+00:00",
        "worker_count": 2,
    }


def test_local_fast_main_pins_head_and_writes_provenance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    head_sha = "b" * 40
    base_sha = "c" * 40
    summary_path = tmp_path / "summary.json"
    captured: dict[str, object] = {}
    selected_inputs: dict[str, object] = {}
    changed_file_inputs: list[tuple[str, str]] = []
    checks = [Check("whitespace", ("python", "-V"))]
    monkeypatch.setattr(local_fast_gate.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(local_fast_gate, "_is_worktree_dirty", lambda: False)

    def git_stdout(args: list[str]) -> str:
        return (base_sha if args[0] == "merge-base" else head_sha) + "\n"

    def changed_files(base: str, *, head_ref: str) -> list[str]:
        changed_file_inputs.append((base, head_ref))
        return ["README.md"]

    monkeypatch.setattr(local_fast_gate, "_git_stdout", git_stdout)
    monkeypatch.setattr(local_fast_gate, "changed_files", changed_files)

    def select_checks(**kwargs):
        selected_inputs.update(kwargs)
        return checks, []

    monkeypatch.setattr(local_fast_gate, "select_checks", select_checks)

    def run_lanes(_lanes, **kwargs):
        captured.update(kwargs)
        return local_fast_gate.GateResult(0, 0.5, 1, ())

    monkeypatch.setattr(local_fast_gate, "run_lanes", run_lanes)

    assert (
        local_fast_gate.main(
            [
                "--base",
                "origin/main",
                "--profile",
                "pr",
                "--summary-json",
                str(summary_path),
            ]
        )
        == 0
    )

    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert captured["revision"] == head_sha
    assert changed_file_inputs == [(base_sha, head_sha)]
    assert selected_inputs["base_ref"] == base_sha
    assert payload["head_sha"] == head_sha
    assert payload["base_ref"] == "origin/main"
    assert payload["base_sha"] == base_sha
    assert payload["changed_file_paths"] == ["README.md"]
    assert payload["profile"] == "pr"
    assert payload["committed_head_only"] is True
    assert payload["source_worktree_dirty_at_selection"] is False
    started_at = datetime.fromisoformat(payload["started_at"])
    finished_at = datetime.fromisoformat(payload["finished_at"])
    started_offset = started_at.utcoffset()
    finished_offset = finished_at.utcoffset()
    assert started_offset is not None
    assert started_offset.total_seconds() == 0
    assert finished_offset is not None
    assert finished_offset.total_seconds() == 0
    assert started_at <= finished_at


def test_local_fast_main_rejects_head_change_during_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first_head = "a" * 40
    second_head = "b" * 40
    base_sha = "c" * 40
    observed_heads = iter((first_head, second_head))
    summary_path = tmp_path / "summary.json"
    checks = [Check("whitespace", ("python", "-V"))]
    monkeypatch.setattr(local_fast_gate.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(local_fast_gate, "_is_worktree_dirty", lambda: False)

    def git_stdout(args: list[str]) -> str:
        return (base_sha if args[0] == "merge-base" else next(observed_heads)) + "\n"

    monkeypatch.setattr(local_fast_gate, "_git_stdout", git_stdout)
    monkeypatch.setattr(
        local_fast_gate,
        "changed_files",
        lambda _base, *, head_ref: ["README.md"],
    )
    monkeypatch.setattr(local_fast_gate, "select_checks", lambda **_kwargs: (checks, []))
    monkeypatch.setattr(
        local_fast_gate,
        "run_lanes",
        lambda *_args, **_kwargs: pytest.fail("lanes must not run after HEAD changes"),
    )

    with pytest.raises(SystemExit, match="HEAD changed while local-fast selected checks"):
        local_fast_gate.main(["--summary-json", str(summary_path)])

    assert not summary_path.exists()


def test_local_fast_omits_deleted_worktree_paths(monkeypatch, tmp_path: Path) -> None:
    worktree = tmp_path / "cheap"
    removed: list[Path] = []
    lane = local_fast_gate.Lane(
        name="cheap",
        checks=(Check("whitespace", ("python", "-V")),),
    )
    revisions: list[str] = []

    def create_worktree(_name: str, *, revision: str) -> Path:
        revisions.append(revision)
        return worktree

    monkeypatch.setattr(local_fast_gate, "_create_worktree", create_worktree)
    monkeypatch.setattr(local_fast_gate, "_remove_worktree", removed.append)
    monkeypatch.setattr(local_fast_gate, "_run_check", lambda *args, **_kwargs: 0)

    result = local_fast_gate.run_lane(lane, keep_worktrees=False, revision="a" * 40)

    assert result.worktree is None
    assert revisions == ["a" * 40]
    assert removed == [worktree]


def test_local_fast_kept_worktree_paths_remain_in_summary(monkeypatch, tmp_path: Path) -> None:
    worktree = tmp_path / "cheap"
    removed: list[Path] = []
    lane = local_fast_gate.Lane(
        name="cheap",
        checks=(Check("whitespace", ("python", "-V")),),
    )
    monkeypatch.setattr(
        local_fast_gate,
        "_create_worktree",
        lambda _name, *, revision: worktree,
    )
    monkeypatch.setattr(local_fast_gate, "_remove_worktree", removed.append)
    monkeypatch.setattr(local_fast_gate, "_run_check", lambda *args, **_kwargs: 0)

    result = local_fast_gate.run_lane(lane, keep_worktrees=True)

    assert result.worktree == worktree
    assert removed == []


def _write_release_artifact_manifest(
    root: Path,
    *,
    tracked_payload: bytes = b"tracked graph payload",
    archive_payload: bytes = b"release archive payload",
) -> Path:
    manifest_path = root / "graph" / "release-artifacts.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payloads = {
        "communities.json": tracked_payload,
        "entity-overlays.jsonl": b"tracked overlay payload",
        "skills-sh-catalog.json.gz": b"tracked catalog payload",
        "wiki-graph-runtime.tar.gz": b"release runtime payload",
        "wiki-graph.tar.gz": archive_payload,
    }
    specs = (
        ("graph/communities.json", "communities.json", False),
        ("graph/entity-overlays.jsonl", "entity-overlays.jsonl", False),
        ("graph/skills-sh-catalog.json.gz", "skills-sh-catalog.json.gz", False),
        ("graph/wiki-graph-runtime.tar.gz", "wiki-graph-runtime.tar.gz", True),
        ("graph/wiki-graph.tar.gz", "wiki-graph.tar.gz", True),
    )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": "stevesolun/ctx",
                "source_release_tag": "v1.0.21",
                "artifacts": [
                    {
                        "path": path,
                        "asset_name": asset_name,
                        "size": len(payloads[asset_name]),
                        "sha256": hashlib.sha256(payloads[asset_name]).hexdigest(),
                        "hydrate": hydrate,
                    }
                    for path, asset_name, hydrate in specs
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def test_preflight_hydrates_missing_archives_from_exact_release_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci_preflight, "REPO_ROOT", tmp_path)
    tracked_payload = b"tracked graph payload"
    archive_payload = b"release archive payload"
    manifest_path = _write_release_artifact_manifest(
        tmp_path,
        tracked_payload=tracked_payload,
        archive_payload=archive_payload,
    )
    tracked_assets = {
        "communities.json": tracked_payload,
        "entity-overlays.jsonl": b"tracked overlay payload",
        "skills-sh-catalog.json.gz": b"tracked catalog payload",
    }
    for asset_name, payload in tracked_assets.items():
        (tmp_path / "graph" / asset_name).write_bytes(payload)
    release_assets = {
        "wiki-graph-runtime.tar.gz": b"release runtime payload",
        "wiki-graph.tar.gz": archive_payload,
    }

    requested_urls: list[str] = []

    def fake_urlopen(url: str, *, timeout: int) -> io.BytesIO:
        requested_urls.append(url)
        assert timeout == 120
        return io.BytesIO(release_assets[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(release_artifacts.urllib.request, "urlopen", fake_urlopen)

    release_artifacts.hydrate_and_verify(repo_root=tmp_path, manifest_path=manifest_path)
    assert requested_urls == [
        "https://github.com/stevesolun/ctx/releases/download/v1.0.21/wiki-graph-runtime.tar.gz",
        "https://github.com/stevesolun/ctx/releases/download/v1.0.21/wiki-graph.tar.gz",
    ]
    assert (tmp_path / "graph" / "wiki-graph.tar.gz").read_bytes() == archive_payload


def test_preflight_fails_closed_on_existing_graph_artifact_tamper(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci_preflight, "REPO_ROOT", tmp_path)
    manifest_path = _write_release_artifact_manifest(tmp_path)
    tracked = tmp_path / "graph" / "communities.json"
    tracked.write_bytes(b"tampered")
    monkeypatch.setattr(
        release_artifacts.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("tamper must fail before network access"),
    )

    with pytest.raises(ValueError, match="artifact mismatch"):
        release_artifacts.hydrate_and_verify(repo_root=tmp_path, manifest_path=manifest_path)
    assert tracked.read_bytes() == b"tampered"


def test_release_artifact_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    manifest_path = _write_release_artifact_manifest(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown manifest fields"):
        release_artifacts.load_manifest(manifest_path)


def test_preflight_no_test_policy_invocation_uses_current_dirty_file_set() -> None:
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=["scripts/ci_preflight.py"],
        profile="pr",
        python="python",
    )

    policy = next(check for check in checks if check.name == "no-test policy")
    assert policy.argv[0] == "python"
    assert Path(policy.argv[1]).name == "ci_preflight.py"
    assert policy.argv[2:] == ("--base", "origin/main", "--internal-no-test-policy")


def test_internal_no_test_policy_fails_dirty_contract_without_tests() -> None:
    assert _run_no_test_policy_for_files(["src/ctx/adapters/generic/loop.py"]) == 1


def test_internal_no_test_policy_accepts_dirty_contract_with_tests() -> None:
    assert (
        _run_no_test_policy_for_files(
            [
                "src/ctx/adapters/generic/loop.py",
                "src/tests/test_harness_loop.py",
            ]
        )
        == 0
    )


def test_internal_no_test_policy_rejects_metadata_without_diff_context() -> None:
    assert _run_no_test_policy_for_files(["pyproject.toml"], diffs_by_file={}) == 1


def test_preflight_pr_profile_runs_package_build_for_source_prs() -> None:
    checks, _notes = select_checks(
        base_ref="origin/main",
        files=["pyproject.toml"],
        profile="pr",
        python="python",
    )
    build = next(check for check in checks if check.name == "build wheel")
    twine = next(check for check in checks if check.name == "twine check")

    assert build.argv == (
        "python",
        "scripts/build_reproducible_dist.py",
        "--verify",
        "--output-dir",
        ".ci-preflight-dist",
    )
    assert "verified_artifact_paths" in twine.argv[-1]
    assert "glob" not in twine.argv[-1]


def test_publish_workflow_consumes_only_manifest_verified_artifacts() -> None:
    workflow = Path(".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert 'python -m twine check "$CTX_WHEEL" "$CTX_SDIST"' in workflow
    assert "twine check dist/*" not in workflow
    assert workflow.count("--check-output dist") >= 3
    assert workflow.count("packages-dir: dist/packages/") == 2
    assert "dist/packages/*.whl" in workflow
    assert "dist/packages/*.tar.gz" in workflow


def test_preflight_full_profile_forces_source_gates_for_docs_changes() -> None:
    names = _names_for(["docs/index.md"], profile="full")

    assert "unit-linux equivalent" in names
    assert "build wheel" in names


def test_preflight_runs_browser_and_similarity_when_classified() -> None:
    names = _names_for([".github/workflows/test.yml"])
    lanes = local_fast_gate.group_checks(_checks_for([".github/workflows/test.yml"]))
    lane_names = [lane.name for lane in lanes]
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text(encoding="utf-8"))
    graph_steps = workflow["jobs"]["graph-check"]["steps"]
    graph_resolver_script = next(
        step["run"]
        for step in graph_steps
        if "graph_release_manifest.py hydrate" in str(step.get("run", ""))
    )

    assert "browser monitor security" in names
    assert "similarity precision/recall" in names
    assert "cheap" in lane_names
    assert "static" in lane_names
    assert "unit" in lane_names
    assert "canary" in lane_names
    assert "contract" in lane_names
    assert "clean-host" in lane_names
    assert "telemetry" in lane_names
    assert "similarity" in lane_names
    assert "browser" in lane_names
    assert "package" in lane_names
    assert "--manifest graph/release-artifacts.json" in graph_resolver_script
    assert "git lfs" not in graph_resolver_script.lower()


def test_every_workflow_parses_under_the_strict_loader() -> None:
    """GitHub rejects duplicate mapping keys; ``yaml.safe_load`` does not.

    A merge once left ``clean-host-contract`` declared twice in test.yml, the
    first with an empty ``steps:``. Every test that reads the workflow used
    ``yaml.safe_load``, which silently keeps the last duplicate, so 8500 tests
    stayed green on a workflow GitHub Actions would have refused to run --
    leaving a contributor with no green to reach and no way to tell it was not
    their fault. ``_WorkflowLoader`` already fails on duplicate keys; this makes
    every workflow go through it.
    """

    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from ci_no_test_policy import _WorkflowLoader

    workflows = sorted((Path(__file__).resolve().parents[2] / ".github/workflows").glob("*.yml"))
    assert workflows, "no workflows found to validate"

    for workflow in workflows:
        try:
            yaml.load(workflow.read_text(encoding="utf-8"), Loader=_WorkflowLoader)
        except yaml.YAMLError as exc:
            raise AssertionError(f"{workflow.name} is not valid for GitHub Actions: {exc}") from exc
