"""Enforce that product/CI contract changes include test changes."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable

RELEASE_METADATA_FILES = {
    "CHANGELOG.md",
    "pyproject.toml",
    "src/__init__.py",
    "src/ctx/__init__.py",
}
VERSION_LINE_RE = re.compile(r'version = "\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]*)?"')
INIT_VERSION_LINE_RE = re.compile(
    r'__version__ = "\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]*)?"'
)


@dataclass(frozen=True)
class PolicyResult:
    passed: bool
    message: str
    contract_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()


def is_contract_file(path: str) -> bool:
    return (
        (path.startswith("src/") and path.endswith((".py", ".json")))
        or path.startswith("scripts/ci_")
        or path == "scripts/clean_host_contract.py"
        or path == "pyproject.toml"
        or path == ".github/workflows/test.yml"
    ) and not path.startswith("src/tests/")


def is_test_file(path: str) -> bool:
    return path.startswith("src/tests/") and path.endswith((".py", ".json"))


def is_release_metadata_only(
    changed_files: Iterable[str],
    diffs_by_file: dict[str, str],
) -> bool:
    files = tuple(path.strip().replace("\\", "/") for path in changed_files if path)
    if not files or any(path not in RELEASE_METADATA_FILES for path in files):
        return False

    for path in files:
        if path == "CHANGELOG.md":
            continue
        expected = VERSION_LINE_RE if path == "pyproject.toml" else INIT_VERSION_LINE_RE
        for line in diffs_by_file.get(path, "").splitlines():
            if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue
            if not expected.fullmatch(line[1:].strip()):
                return False
    return True


def evaluate_policy(
    changed_files: Iterable[str],
    labels: Iterable[str],
    diffs_by_file: dict[str, str],
) -> PolicyResult:
    files = tuple(path.strip().replace("\\", "/") for path in changed_files if path)
    contract = tuple(path for path in files if is_contract_file(path))
    tests = tuple(path for path in files if is_test_file(path))
    if not contract:
        return PolicyResult(True, "No product or CI/package contract changes.")
    if tests:
        return PolicyResult(True, "Policy satisfied.", contract, tests)
    if "no-tests-needed" in set(labels):
        return PolicyResult(True, "Policy exempted by no-tests-needed label.", contract)
    if is_release_metadata_only(files, diffs_by_file):
        return PolicyResult(True, "Policy exempted for release metadata-only changes.", contract)
    return PolicyResult(False, "Contract files changed without accompanying tests.", contract)


def _git_lines(*args: str) -> tuple[str, ...]:
    proc = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())


def _git_text(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return proc.stdout


def _changed_files(base: str, head: str) -> tuple[str, ...]:
    return _git_lines("diff", "--name-only", base, head)


def _diffs_by_file(base: str, head: str, files: Iterable[str]) -> dict[str, str]:
    return {
        path: _git_text("diff", "--unified=0", base, head, "--", path)
        for path in files
    }


def _parse_labels(raw: str) -> tuple[str, ...]:
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(labels, list):
        return ()
    return tuple(label for label in labels if isinstance(label, str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--labels-json", default="[]")
    args = parser.parse_args(argv)

    files = _changed_files(args.base, args.head)
    result = evaluate_policy(
        files,
        _parse_labels(args.labels_json),
        _diffs_by_file(args.base, args.head, files),
    )
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


if __name__ == "__main__":
    raise SystemExit(main())
