from __future__ import annotations

from datetime import datetime
import hashlib
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
        "unit": ["unit-linux equivalent"],
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

    assert names.index("hydrate graph LFS") < names.index("graph artifact validation")
    assert "graph artifact validation" in names
    assert "no-test policy" not in names
    assert "unit-linux equivalent" not in names
    assert graph_names == ["hydrate graph LFS", "graph artifact validation"]
    assert graph_lane.checks[0].argv[1] == "scripts/ci_preflight.py"


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


def test_preflight_graph_lfs_pointer_verification(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ci_preflight, "REPO_ROOT", tmp_path)
    artifact = tmp_path / "graph" / "wiki-graph.tar.gz"
    artifact.parent.mkdir()
    payload = b"hydrated graph payload"
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    artifact.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{expected_sha256}\n"
        f"size {len(payload)}\n",
        encoding="utf-8",
    )

    pointer = ci_preflight._read_lfs_pointer(artifact)

    assert pointer == ci_preflight.LfsPointer(
        "graph/wiki-graph.tar.gz",
        expected_sha256,
        len(payload),
    )
    artifact.write_bytes(payload)
    ci_preflight._verify_hydrated_lfs_pointer(pointer)


def test_preflight_graph_lfs_pull_uses_per_command_filters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ci_preflight, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ci_preflight.shutil, "which", lambda _name: "/usr/bin/git")
    artifact = tmp_path / "graph" / "wiki-graph.tar.gz"
    artifact.parent.mkdir()
    artifact.write_text(
        f"version https://git-lfs.github.com/spec/v1\noid sha256:{'a' * 64}\nsize 1\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(argv: list[str], **_kwargs) -> Result:
        calls.append(argv)
        return Result()

    verified: list[ci_preflight.LfsPointer] = []
    monkeypatch.setattr(ci_preflight.subprocess, "run", fake_run)
    monkeypatch.setattr(ci_preflight, "_verify_hydrated_lfs_pointer", verified.append)

    assert ci_preflight.hydrate_graph_lfs_artifacts() == 0

    assert len(calls) == 1
    argv = calls[0]
    lfs_index = argv.index("lfs")
    assert argv[:lfs_index] == ["git", *ci_preflight.GIT_LFS_FILTER_CONFIG]
    assert argv[lfs_index:] == [
        "lfs",
        "pull",
        "--include",
        "graph/wiki-graph.tar.gz",
        "--exclude",
        "",
    ]
    assert verified == [
        ci_preflight.LfsPointer("graph/wiki-graph.tar.gz", "a" * 64, 1),
    ]


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
        if step.get("name") == "Resolve graph artifacts from release assets or targeted LFS"
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
    assert "release_asset_wait_seconds = 300" in graph_resolver_script
    assert 'os.environ.get("GITHUB_EVENT_NAME") == "pull_request"' in graph_resolver_script
    assert "release_asset_wait_seconds = 0" in graph_resolver_script
    assert 'env.setdefault("GIT_LFS_CONCURRENTTRANSFERS", "1")' in graph_resolver_script
