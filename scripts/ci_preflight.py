"""Run local checks that mirror the required GitHub PR gates.

This is intentionally conservative: it uses the same path classifier as
`.github/workflows/test.yml`, runs the local equivalents of required jobs, and
prints any CI-only coverage that cannot be reproduced on the current OS.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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


GRAPH_VALIDATE_ARGS = (
    "src/validate_graph_artifacts.py",
    "--graph-dir",
    "graph",
    "--deep",
    "--min-nodes",
    "100000",
    "--min-edges",
    "2000000",
    "--min-skills-sh-nodes",
    "89000",
    "--min-semantic-edges",
    "1000000",
    "--expected-nodes",
    "102928",
    "--expected-edges",
    "2913960",
    "--expected-semantic-edges",
    "1683193",
    "--expected-harness-nodes",
    "207",
    "--expected-skills-sh-nodes",
    "89471",
    "--expected-skills-sh-catalog-entries",
    "89465",
    "--expected-skills-sh-converted",
    "89465",
    "--expected-skill-pages",
    "91464",
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


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    env: dict[str, str] | None = None


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


def changed_files(base_ref: str) -> list[str]:
    merge_base = _run_git(["merge-base", base_ref, "HEAD"], allow_failure=True)
    base = merge_base[0] if merge_base else base_ref
    paths = set(_run_git(["diff", "--name-only", base, "HEAD"], allow_failure=True))
    paths.update(_run_git(["diff", "--name-only"], allow_failure=True))
    paths.update(_run_git(["diff", "--cached", "--name-only"], allow_failure=True))
    paths.update(
        _run_git(["ls-files", "--others", "--exclude-standard"], allow_failure=True)
    )
    return sorted(path.replace("\\", "/") for path in paths)


def select_checks(
    *,
    base_ref: str,
    files: list[str],
    profile: str,
    python: str,
) -> tuple[list[Check], list[str]]:
    flags = classify_paths(files)
    checks: list[Check] = [
        Check("whitespace", ("git", "diff", "--check")),
        Check("repo stats", (python, "src/update_repo_stats.py", "--check")),
    ]
    notes = [
        "GitHub still runs Windows/macOS matrix jobs; local preflight covers the "
        "same contracts on this host."
    ]

    source_required = profile == "full" or (
        not flags["docs_only"] and not flags["graph_only"]
    )
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
                Check("ruff", (python, "-m", "ruff", "check", "src", "hooks", "scripts")),
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
            Check("docs strict build", (python, "-m", "mkdocs", "build", "--strict"))
        )

    if flags["graph_artifact_changed"]:
        checks.append(Check("graph artifact validation", (python, *GRAPH_VALIDATE_ARGS)))

    if source_required and flags["similarity_changed"]:
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

    if source_required and flags["browser_changed"]:
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

    if source_required:
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

    return checks, notes


def _run_no_test_policy_for_files(files: list[str]) -> int:
    result = evaluate_policy(files, (), {})
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
        choices=("pr", "full"),
        default="pr",
        help="pr mirrors required PR checks; full forces source gates for any change set",
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
    args = parser.parse_args(argv)

    if not shutil.which("git"):
        raise SystemExit("git is required for ci_preflight")

    files = changed_files(args.base)
    if args.internal_no_test_policy:
        return _run_no_test_policy_for_files(files)

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
