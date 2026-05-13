from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable

DEFAULT_ARTIFACTS = (
    "graph/wiki-graph.tar.gz",
    "graph/wiki-graph-runtime.tar.gz",
    "graph/skills-sh-catalog.json.gz",
)


def _run_git(
    repo: Path,
    args: Iterable[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def _repo_root(path: Path) -> Path:
    result = _run_git(path, ["rev-parse", "--show-toplevel"], capture=True)
    return Path(result.stdout.strip()).resolve()


def _tracked_artifacts(repo: Path, artifacts: Iterable[str]) -> list[str]:
    tracked: list[str] = []
    for artifact in artifacts:
        result = _run_git(
            repo,
            ["ls-files", "--error-unmatch", artifact],
            check=False,
            capture=True,
        )
        if result.returncode == 0:
            tracked.append(artifact)
    return tracked


def _set_skip_worktree(repo: Path, artifacts: list[str], *, enabled: bool) -> None:
    if not artifacts:
        print("No tracked graph artifacts matched.")
        return
    flag = "--skip-worktree" if enabled else "--no-skip-worktree"
    _run_git(repo, ["update-index", flag, *artifacts])
    action = "parked" if enabled else "unparked"
    for artifact in artifacts:
        print(f"{action}: {artifact}")


def _print_status(repo: Path, artifacts: list[str]) -> None:
    if not artifacts:
        print("No tracked graph artifacts matched.")
        return
    result = _run_git(repo, ["ls-files", "-v", *artifacts], capture=True)
    for line in result.stdout.splitlines():
        state = "parked" if line.startswith("S ") else "active"
        print(f"{state}: {line[2:]}")
    print()
    sys.stdout.flush()
    _run_git(repo, ["count-objects", "-vH"])


def _prune(repo: Path, *, include_lfs: bool) -> None:
    _run_git(repo, ["prune", "--expire=now", "--verbose"])
    if include_lfs:
        result = _run_git(repo, ["lfs", "prune", "--verbose"], check=False)
        if result.returncode != 0:
            raise SystemExit(result.returncode)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Park or unpark heavyweight generated graph artifacts so background "
            "Git integrations do not repeatedly LFS-clean them while they are dirty."
        )
    )
    parser.add_argument(
        "command",
        choices=("status", "park", "unpark", "prune"),
        help=(
            "status shows skip-worktree state; park hides generated archives from "
            "normal Git status/stage scans; unpark re-enables release staging; "
            "prune removes unreachable local Git/LFS objects."
        ),
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Repository path. Defaults to the current directory.",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        dest="artifacts",
        help=(
            "Artifact path to manage. May be passed more than once. Defaults to "
            "the shipped graph tarballs and Skills.sh catalog gzip."
        ),
    )
    parser.add_argument(
        "--skip-lfs",
        action="store_true",
        help="For prune only: skip git lfs prune.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = _repo_root(args.repo)
    artifacts = _tracked_artifacts(repo, args.artifacts or DEFAULT_ARTIFACTS)

    if args.command == "status":
        _print_status(repo, artifacts)
    elif args.command == "park":
        _set_skip_worktree(repo, artifacts, enabled=True)
    elif args.command == "unpark":
        _set_skip_worktree(repo, artifacts, enabled=False)
    elif args.command == "prune":
        _prune(repo, include_lfs=not args.skip_lfs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
