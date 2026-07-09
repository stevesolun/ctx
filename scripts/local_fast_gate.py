"""Run PR preflight checks in isolated parallel local lanes.

The regular CI preflight stays the serial source of truth. This runner is a
fast local front door: it selects the same checks, groups independent work into
lanes, and runs each lane in a temporary git worktree so caches, graph hydration,
and package builds do not contend with each other.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ci_preflight import Check  # noqa: E402
from scripts.ci_preflight import PROFILE_CHOICES  # noqa: E402
from scripts.ci_preflight import changed_files  # noqa: E402
from scripts.ci_preflight import select_checks  # noqa: E402

LANE_ORDER = (
    "cheap",
    "static",
    "unit",
    "canary",
    "contract",
    "clean-host",
    "docs",
    "graph",
    "telemetry",
    "similarity",
    "browser",
    "package",
    "misc",
)
CHECK_LANES = {
    "whitespace": "cheap",
    "repo stats": "cheap",
    "no-test policy": "cheap",
    "ruff format": "static",
    "ruff": "static",
    "mypy": "static",
    "pip check": "static",
    "unit-linux equivalent": "unit",
    "A-Z canary": "canary",
    "contract compatibility local": "contract",
    "clean host contract": "clean-host",
    "public docs tracker": "docs",
    "docs strict build": "docs",
    "hydrate graph LFS": "graph",
    "graph artifact validation": "graph",
    "telemetry enterprise": "telemetry",
    "similarity precision/recall": "similarity",
    "browser monitor security": "browser",
    "clean preflight dist": "package",
    "build wheel": "package",
    "twine check": "package",
}


@dataclass(frozen=True)
class Lane:
    name: str
    checks: tuple[Check, ...]


@dataclass(frozen=True)
class LaneResult:
    name: str
    returncode: int
    elapsed: float
    check_count: int
    worktree: Path | None = None


@dataclass(frozen=True)
class GateResult:
    returncode: int
    elapsed: float
    worker_count: int
    lanes: tuple[LaneResult, ...]


def _lane_name(check: Check) -> str:
    return CHECK_LANES.get(check.name, "misc")


def group_checks(checks: list[Check]) -> list[Lane]:
    grouped: dict[str, list[Check]] = {lane: [] for lane in LANE_ORDER}
    for check in checks:
        grouped[_lane_name(check)].append(_worktree_safe_check(check))
    return [Lane(name, tuple(grouped[name])) for name in LANE_ORDER if grouped[name]]


def filter_lanes(
    lanes: list[Lane],
    *,
    include: tuple[str, ...] = (),
    skip: tuple[str, ...] = (),
) -> list[Lane]:
    include_set = set(include)
    skip_set = set(skip)
    return [
        lane
        for lane in lanes
        if (not include_set or lane.name in include_set) and lane.name not in skip_set
    ]


def _worktree_safe_check(check: Check) -> Check:
    ci_preflight = (REPO_ROOT / "scripts" / "ci_preflight.py").resolve()
    argv = tuple(
        "scripts/ci_preflight.py" if _same_path_arg(arg, ci_preflight) else arg
        for arg in check.argv
    )
    return Check(check.name, argv, check.env)


def _same_path_arg(arg: str, expected: Path) -> bool:
    try:
        return Path(arg).resolve() == expected
    except OSError:
        return False


def _git_stdout(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True)


def _is_worktree_dirty() -> bool:
    return bool(_git_stdout(["status", "--porcelain"]).strip())


def _create_worktree(lane: str) -> Path:
    parent = Path(tempfile.mkdtemp(prefix="ctx-local-fast-"))
    worktree = parent / lane
    env = os.environ.copy()
    env.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
    subprocess.check_call(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
    )
    return worktree


def _remove_worktree(worktree: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    shutil.rmtree(worktree.parent, ignore_errors=True)


def _run_check(check: Check, *, cwd: Path, index: int, total: int, lane: str) -> int:
    print(
        f"[{lane} {index}/{total}] {check.name}: {' '.join(check.argv)}",
        flush=True,
    )
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    env.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
    if check.env:
        env.update(check.env)
    proc = subprocess.run(check.argv, cwd=cwd, check=False, env=env)
    if proc.returncode != 0:
        print(f"[{lane} fail] {check.name} exited {proc.returncode}", file=sys.stderr)
    return proc.returncode


def run_lane(lane: Lane, *, keep_worktrees: bool) -> LaneResult:
    start = time.monotonic()
    worktree = _create_worktree(lane.name)
    summary_worktree = worktree if keep_worktrees else None
    try:
        for index, check in enumerate(lane.checks, start=1):
            returncode = _run_check(
                check,
                cwd=worktree,
                index=index,
                total=len(lane.checks),
                lane=lane.name,
            )
            if returncode != 0:
                return LaneResult(
                    lane.name,
                    returncode,
                    time.monotonic() - start,
                    len(lane.checks),
                    summary_worktree,
                )
        return LaneResult(
            lane.name,
            0,
            time.monotonic() - start,
            len(lane.checks),
            summary_worktree,
        )
    finally:
        if not keep_worktrees:
            _remove_worktree(worktree)


def _sort_lane_results(results: list[LaneResult]) -> tuple[LaneResult, ...]:
    order = {name: index for index, name in enumerate(LANE_ORDER)}
    return tuple(sorted(results, key=lambda result: order.get(result.name, len(order))))


def run_lanes(
    lanes: list[Lane],
    *,
    jobs: int,
    keep_worktrees: bool = False,
) -> GateResult:
    start = time.monotonic()
    if not lanes:
        print("No local-fast lanes selected.")
        return GateResult(0, 0.0, 0, ())
    worker_count = max(1, min(jobs, len(lanes)))
    print(f"Running {len(lanes)} local-fast lanes with {worker_count} workers.")
    failures: list[LaneResult] = []
    results: list[LaneResult] = []
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(run_lane, lane, keep_worktrees=keep_worktrees): lane for lane in lanes
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            status = "pass" if result.returncode == 0 else "fail"
            print(f"[{status}] {result.name} lane in {result.elapsed:.1f}s")
            if result.returncode != 0:
                failures.append(result)

    if failures:
        for failure in failures:
            print(
                f"[fail] {failure.name} lane exited {failure.returncode}",
                file=sys.stderr,
            )
    return GateResult(
        1 if failures else 0,
        time.monotonic() - start,
        worker_count,
        _sort_lane_results(results),
    )


def write_summary_json(path: Path, result: GateResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "returncode": result.returncode,
        "elapsed_seconds": round(result.elapsed, 3),
        "worker_count": result.worker_count,
        "lanes": [
            {
                "name": lane.name,
                "returncode": lane.returncode,
                "elapsed_seconds": round(lane.elapsed, 3),
                "check_count": lane.check_count,
                "worktree": str(lane.worktree) if lane.worktree else None,
            }
            for lane in result.lanes
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def print_dry_run(lanes: list[Lane]) -> None:
    for lane in lanes:
        print(f"[lane] {lane.name}")
        for check in lane.checks:
            print(f"  - {check.name}: {' '.join(check.argv)}")


def _default_jobs() -> int:
    cpu_count = os.cpu_count() or 2
    return max(1, min(cpu_count, len(LANE_ORDER)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--profile", choices=PROFILE_CHOICES, default="pr")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--jobs", type=int, default=_default_jobs())
    parser.add_argument("--lane", action="append", choices=LANE_ORDER)
    parser.add_argument("--skip-lane", action="append", choices=LANE_ORDER)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-worktrees", action="store_true")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow a dirty worktree; uncommitted changes still are not run in temp worktrees",
    )
    args = parser.parse_args(argv)

    if not shutil.which("git"):
        raise SystemExit("git is required for local_fast_gate")
    if not args.dry_run and not args.allow_dirty and _is_worktree_dirty():
        raise SystemExit(
            "local-fast runs committed HEAD in temp worktrees; commit or stash changes first "
            "(or pass --allow-dirty if you only need a committed-HEAD gate)."
        )

    files = changed_files(args.base)
    checks, notes = select_checks(
        base_ref=args.base,
        files=files,
        profile=args.profile,
        python=args.python,
    )
    lanes = filter_lanes(
        group_checks(checks),
        include=tuple(args.lane or ()),
        skip=tuple(args.skip_lane or ()),
    )
    for note in notes:
        print(f"[note] {note}")
    print("[note] local-fast runs selected committed-HEAD checks in isolated temp worktrees.")
    if args.dry_run:
        print_dry_run(lanes)
        return 0
    result = run_lanes(lanes, jobs=args.jobs, keep_worktrees=args.keep_worktrees)
    if args.summary_json:
        write_summary_json(args.summary_json, result)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
