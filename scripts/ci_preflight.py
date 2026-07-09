"""Run local checks that mirror the required GitHub PR gates.

This is intentionally conservative: it uses the same path classifier as
`.github/workflows/test.yml`, runs the local equivalents of required jobs, and
prints any CI-only coverage that cannot be reproduced on the current OS.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ci_classifier import classify_paths  # noqa: E402
from scripts.ci_no_test_policy import evaluate_policy  # noqa: E402

PUBLIC_DOCS_TRACKER_TESTS = (
    "src/tests/test_bug_smoke_tracker.py",
    "src/tests/test_feature_user_story_tracker.py",
    "src/tests/test_dashboard_user_story_tracker.py",
    "src/tests/test_toolbox_cli.py",
)
PROFILE_CHOICES = ("smoke", "pr", "full")


GRAPH_VALIDATE_ARGS = (
    "src/validate_graph_artifacts.py",
    "--graph-dir",
    "graph",
    "--deep",
    "--min-nodes",
    "79000",
    "--min-edges",
    "1700000",
    "--min-skills-sh-nodes",
    "67000",
    "--min-semantic-edges",
    "1000000",
    "--expected-nodes",
    "79958",
    "--expected-edges",
    "1778069",
    "--expected-semantic-edges",
    "1088763",
    "--expected-harness-nodes",
    "207",
    "--expected-skills-sh-nodes",
    "67028",
    "--expected-skills-sh-catalog-entries",
    "67024",
    "--expected-skills-sh-converted",
    "67024",
    "--expected-skill-pages",
    "68494",
    "--expected-agent-pages",
    "467",
    "--expected-mcp-pages",
    "10790",
    "--expected-harness-pages",
    "207",
    "--line-threshold",
    "180",
    "--max-stage-lines",
    "40",
)
GRAPH_LFS_ARTIFACTS = (
    "graph/wiki-graph.tar.gz",
    "graph/wiki-graph-runtime.tar.gz",
)
GRAPH_LFS_MAX_FALLBACK_SIZES = {
    "graph/wiki-graph.tar.gz": 350_000_000,
    "graph/wiki-graph-runtime.tar.gz": 150_000_000,
}
LFS_POINTER_PREFIX = "version https://git-lfs.github.com/spec/v1"
LFS_POINTER_MAX_BYTES = 4096
GIT_LFS_FILTER_CONFIG = (
    "-c",
    "filter.lfs.process=git-lfs filter-process",
    "-c",
    "filter.lfs.smudge=git-lfs smudge -- %f",
    "-c",
    "filter.lfs.clean=git-lfs clean -- %f",
    "-c",
    "filter.lfs.required=true",
)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class LfsPointer:
    path: str
    sha256: str
    size: int


def _run_git(args: list[str], *, allow_failure: bool = False) -> list[str]:
    proc = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if allow_failure:
            return []
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _run_git_text(args: list[str], *, allow_failure: bool = False) -> str:
    proc = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if allow_failure:
            return ""
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def _diff_base(base_ref: str) -> str:
    merge_base = _run_git(["merge-base", base_ref, "HEAD"], allow_failure=True)
    return merge_base[0] if merge_base else base_ref


def changed_files(base_ref: str) -> list[str]:
    base = _diff_base(base_ref)
    paths = set(_run_git(["diff", "--name-only", base, "HEAD"], allow_failure=True))
    paths.update(_run_git(["diff", "--name-only"], allow_failure=True))
    paths.update(_run_git(["diff", "--cached", "--name-only"], allow_failure=True))
    paths.update(_run_git(["ls-files", "--others", "--exclude-standard"], allow_failure=True))
    return sorted(path.replace("\\", "/") for path in paths)


def _diffs_for_files(base_ref: str, files: list[str]) -> dict[str, str]:
    base = _diff_base(base_ref)
    diffs: dict[str, str] = {}
    for path in files:
        parts = (
            _run_git_text(["diff", "--unified=0", base, "HEAD", "--", path], allow_failure=True),
            _run_git_text(["diff", "--cached", "--unified=0", "--", path], allow_failure=True),
            _run_git_text(["diff", "--unified=0", "--", path], allow_failure=True),
        )
        diff_text = "\n".join(part for part in parts if part)
        if diff_text:
            diffs[path] = diff_text
    return diffs


def _read_lfs_pointer(path: Path) -> LfsPointer | None:
    if not path.exists():
        return None
    prefix_bytes = LFS_POINTER_PREFIX.encode("utf-8")
    with path.open("rb") as fh:
        prefix = fh.read(len(prefix_bytes))
        if prefix != prefix_bytes:
            return None
        pointer_bytes = prefix + fh.read(LFS_POINTER_MAX_BYTES - len(prefix_bytes))
    expected_oid = ""
    expected_size = 0
    pointer = pointer_bytes.decode("utf-8", errors="replace")
    for line in pointer.splitlines():
        if line.startswith("oid sha256:"):
            expected_oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            expected_size = int(line.split(" ", 1)[1].strip())
    if expected_oid and expected_size:
        return LfsPointer(path.relative_to(REPO_ROOT).as_posix(), expected_oid, expected_size)
    raise RuntimeError(f"{path.relative_to(REPO_ROOT)} has incomplete Git LFS pointer metadata")


def _file_sha256_and_size(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            total += len(chunk)
    return sha.hexdigest(), total


def _verify_hydrated_lfs_pointer(pointer: LfsPointer) -> None:
    path = REPO_ROOT / pointer.path
    if _read_lfs_pointer(path) is not None:
        raise RuntimeError(f"{pointer.path} is still a Git LFS pointer after hydration")
    actual_sha256, actual_size = _file_sha256_and_size(path)
    if actual_sha256 != pointer.sha256 or actual_size != pointer.size:
        raise RuntimeError(
            f"{pointer.path} does not match its Git LFS pointer: "
            f"sha256:{actual_sha256} size:{actual_size}"
        )


def hydrate_graph_lfs_artifacts() -> int:
    pointers = [
        pointer
        for relpath in GRAPH_LFS_ARTIFACTS
        if (pointer := _read_lfs_pointer(REPO_ROOT / relpath)) is not None
    ]
    if not pointers:
        print("Graph LFS artifacts are already hydrated.")
        return 0

    if not shutil.which("git"):
        print("git is required to hydrate graph LFS artifacts", file=sys.stderr)
        return 127

    env = os.environ.copy()
    env.setdefault("GIT_LFS_SKIP_SMUDGE", "1")
    env.setdefault("GIT_LFS_ACTIVITYTIMEOUT", "600")
    env.setdefault("GIT_LFS_DIALTIMEOUT", "120")
    env.setdefault("GIT_LFS_TLSTIMEOUT", "120")
    for pointer in pointers:
        max_size = GRAPH_LFS_MAX_FALLBACK_SIZES[pointer.path]
        if pointer.size > max_size:
            print(
                f"Refusing Git LFS fallback for {pointer.path}: "
                f"pointer size {pointer.size} exceeds cap {max_size}",
                file=sys.stderr,
            )
            return 1
        print(f"Hydrating {pointer.path} from Git LFS sha256:{pointer.sha256} size:{pointer.size}")
        proc = subprocess.run(
            [
                "git",
                *GIT_LFS_FILTER_CONFIG,
                "lfs",
                "pull",
                "--include",
                pointer.path,
                "--exclude",
                "",
            ],
            cwd=REPO_ROOT,
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            print(f"git lfs pull failed for {pointer.path}", file=sys.stderr)
            return proc.returncode
        try:
            _verify_hydrated_lfs_pointer(pointer)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return 0


def _run_whitespace_check(base_ref: str) -> int:
    base = _diff_base(base_ref)
    commands = (
        ["diff", "--check", base, "HEAD"],
        ["diff", "--cached", "--check"],
        ["diff", "--check"],
    )
    for args in commands:
        proc = subprocess.run(["git", *args], check=False)
        if proc.returncode != 0:
            return proc.returncode
    return 0


def select_checks(
    *,
    base_ref: str,
    files: list[str],
    profile: str,
    python: str,
) -> tuple[list[Check], list[str]]:
    flags = classify_paths(files)
    checks: list[Check] = [
        Check("whitespace", (python, __file__, "--base", base_ref, "--internal-whitespace")),
        Check("repo stats", (python, "src/update_repo_stats.py", "--check")),
    ]
    notes = [
        "GitHub still runs Windows/macOS matrix jobs; local preflight covers the "
        "same contracts on this host."
    ]

    smoke_profile = profile == "smoke"
    source_required = profile == "full" or (not flags["docs_only"] and not flags["graph_only"])
    policy_required = not flags["docs_only"] and not flags["graph_only"]
    if policy_required:
        checks.append(
            Check(
                "no-test policy",
                (python, __file__, "--base", base_ref, "--internal-no-test-policy"),
            )
        )

    if source_required:
        checks.extend(
            [
                Check(
                    "ruff format",
                    (python, "-m", "ruff", "format", "--check", "src", "hooks", "scripts"),
                ),
                Check("ruff", (python, "-m", "ruff", "check", "src", "hooks", "scripts")),
            ]
        )
        if not smoke_profile:
            checks.extend(
                [
                    Check("mypy", (python, "-m", "mypy", "src")),
                    Check("pip check", (python, "-m", "pip", "check")),
                    Check(
                        "unit-linux equivalent",
                        (
                            python,
                            "-m",
                            "pytest",
                            "-q",
                            "-m",
                            "not browser and not integration",
                            "--cov=src",
                            "--cov-report=term-missing",
                            "--cov-fail-under=40",
                        ),
                    ),
                    Check(
                        "A-Z canary",
                        (
                            python,
                            "-m",
                            "pytest",
                            "-q",
                            "--no-cov",
                            "src/tests/test_alive_loop_e2e.py",
                            "src/tests/test_fuzz_yaml_rendering.py",
                        ),
                    ),
                    Check(
                        "contract compatibility local",
                        (
                            python,
                            "-m",
                            "pytest",
                            "-q",
                            "--no-cov",
                            "src/tests/test_clean_host_contract.py",
                            "src/tests/test_package_scaffold.py",
                        ),
                    ),
                    Check(
                        "clean host contract",
                        (python, "scripts/clean_host_contract.py", "--fast"),
                    ),
                ]
            )

    if flags["docs_changed"]:
        checks.append(
            Check(
                "public docs tracker",
                (
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-cov",
                    *PUBLIC_DOCS_TRACKER_TESTS,
                ),
            )
        )
        if not smoke_profile:
            checks.append(Check("docs strict build", (python, "-m", "mkdocs", "build", "--strict")))

    if not smoke_profile and (profile == "full" or flags["telemetry_changed"]):
        checks.append(
            Check(
                "telemetry enterprise",
                (
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-cov",
                    "src/tests/test_enterprise_telemetry.py",
                    "src/tests/test_harness_cli_run.py",
                    "-k",
                    "telemetry or runtime_lifecycle",
                ),
            )
        )

    if not smoke_profile and flags["graph_artifact_changed"]:
        checks.append(
            Check("hydrate graph LFS", (python, __file__, "--internal-hydrate-graph-lfs"))
        )
        checks.append(Check("graph artifact validation", (python, *GRAPH_VALIDATE_ARGS)))

    if not smoke_profile and source_required and flags["similarity_changed"]:
        checks.append(
            Check(
                "similarity precision/recall",
                (
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-cov",
                    "-m",
                    "integration",
                    "src/tests/test_similarity_precision_recall.py",
                ),
                env={"CTX_REQUIRE_SIMILARITY_EVAL": "1"},
            )
        )

    if not smoke_profile and source_required and flags["browser_changed"]:
        checks.append(
            Check(
                "browser monitor security",
                (
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-cov",
                    "-m",
                    "browser",
                    "src/tests/test_ctx_monitor_browser.py",
                ),
            )
        )

    if not smoke_profile and source_required:
        out_dir = ".ci-preflight-dist"
        twine_script = (
            "import glob, subprocess, sys; "
            f"files=glob.glob({str(out_dir + '/*')!r}); "
            "sys.exit(2 if not files else subprocess.call("
            "[sys.executable, '-m', 'twine', 'check', *files]))"
        )
        checks.extend(
            [
                Check(
                    "clean preflight dist",
                    (
                        python,
                        "-c",
                        f"import shutil; shutil.rmtree({out_dir!r}, ignore_errors=True)",
                    ),
                ),
                Check("build wheel", (python, "-m", "build", "--outdir", out_dir)),
                Check("twine check", (python, "-c", twine_script)),
            ]
        )

    if files:
        notes.insert(0, f"Changed files vs {base_ref}: {len(files)}")
    else:
        notes.insert(0, "No changed files detected; running baseline cheap checks only.")
    if smoke_profile:
        notes.append("Smoke profile skips slow PR gates; run --profile pr before no-mistakes/PR.")

    return checks, notes


def _run_no_test_policy_for_files(
    files: list[str],
    *,
    base_ref: str = "origin/main",
    diffs_by_file: dict[str, str] | None = None,
) -> int:
    if diffs_by_file is None:
        diffs_by_file = _diffs_for_files(base_ref, files)
    result = evaluate_policy(files, (), diffs_by_file)
    print(result.message)
    if result.contract_files:
        print("Contract files:")
        print("\n".join(result.contract_files))
    if result.test_files:
        print("Test files:")
        print("\n".join(result.test_files))
    if not result.passed:
        print("::error::Policy violation - contract files changed but no tests changed.")
        print("Fix: add/update tests, or use release metadata-only changes.")
        return 1
    return 0


def run_checks(checks: list[Check], *, dry_run: bool) -> int:
    for index, check in enumerate(checks, start=1):
        print(f"[{index}/{len(checks)}] {check.name}: {' '.join(check.argv)}", flush=True)
        if dry_run:
            continue
        env = os.environ.copy()
        if check.env:
            env.update(check.env)
        start = time.monotonic()
        proc = subprocess.run(check.argv, check=False, env=env)
        elapsed = time.monotonic() - start
        if proc.returncode != 0:
            print(
                f"[fail] {check.name} exited {proc.returncode} after {elapsed:.1f}s",
                file=sys.stderr,
            )
            return proc.returncode
        print(f"[pass] {check.name} in {elapsed:.1f}s", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="origin/main",
        help="base ref for changed-file detection",
    )
    parser.add_argument(
        "--profile",
        choices=PROFILE_CHOICES,
        default="pr",
        help=(
            "smoke runs cheap first-pass checks; pr mirrors required PR checks; "
            "full forces source gates for any change set"
        ),
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable to run checks with",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print selected checks without running them",
    )
    parser.add_argument(
        "--internal-no-test-policy",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--internal-hydrate-graph-lfs",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--internal-whitespace",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if not shutil.which("git"):
        raise SystemExit("git is required for ci_preflight")

    if args.internal_hydrate_graph_lfs:
        return hydrate_graph_lfs_artifacts()
    if args.internal_whitespace:
        return _run_whitespace_check(args.base)

    files = changed_files(args.base)
    if args.internal_no_test_policy:
        return _run_no_test_policy_for_files(files, base_ref=args.base)

    checks, notes = select_checks(
        base_ref=args.base,
        files=files,
        profile=args.profile,
        python=args.python,
    )
    for note in notes:
        print(f"[note] {note}")
    return run_checks(checks, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
