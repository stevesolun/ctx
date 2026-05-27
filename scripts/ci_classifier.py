"""Classify changed paths for CI workflow decisions."""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
from typing import Iterable

OUTPUT_NAMES = (
    "browser_changed",
    "ci_changed",
    "docs_changed",
    "docs_only",
    "graph_artifact_changed",
    "graph_changed",
    "graph_only",
    "package_changed",
    "similarity_changed",
    "source_changed",
)

DOCS_PATTERNS = (
    "*.md",
    "docs/**",
    "graph/README.md",
    "LICENSE",
    "mkdocs.yml",
    "requirements-docs.txt",
)
GRAPH_ARTIFACT_PATTERNS = (
    "graph/communities.json",
    "graph/entity-overlays.jsonl",
    "graph/skills-sh-catalog.json.gz",
    "graph/wiki-graph-runtime.tar.gz",
    "graph/wiki-graph.tar.gz",
    "graph/*.html",
)
BROWSER_PATTERNS = (
    ".github/workflows/test.yml",
    "dashboard/**",
    "pyproject.toml",
    "src/**/browser/**",
    "src/**/monitor/**",
    "src/ctx_monitor.py",
    "src/ctx/utils/_safe_name.py",
    "src/tests/test_ctx_monitor_browser.py",
)
PACKAGE_PATTERNS = (
    "MANIFEST.in",
    "pyproject.toml",
    "src/*.py",
    "src/ctx/**",
)
SOURCE_PATTERNS = (
    "hooks/**",
    "pyproject.toml",
    "scripts/**",
    "src/**",
)
SIMILARITY_PATTERNS = (
    ".github/workflows/test.yml",
    "graph/entity-overlays.jsonl",
    "pyproject.toml",
    "src/corpus_cache.py",
    "src/config.json",
    "src/cosine_ranker.py",
    "src/ctx/adapters/claude_code/hooks/context_monitor.py",
    "src/ctx/adapters/generic/ctx_core_tools.py",
    "src/ctx_config.py",
    "src/ctx/core/graph/**",
    "src/ctx/core/resolve/**",
    "src/ctx/core/wiki/wiki_graphify.py",
    "src/embedding_backend.py",
    "src/intake_gate.py",
    "src/tests/test_similarity_precision_recall.py",
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _normalize_path(path: str) -> str:
    return path.strip().lstrip("\ufeff").replace("\\", "/")


def _is_graph_artifact_path(path: str) -> bool:
    if _matches(path, GRAPH_ARTIFACT_PATTERNS):
        return True
    return _matches(path, ("graph/**",)) and path != "graph/README.md"


def classify_paths(paths: Iterable[str]) -> dict[str, bool]:
    files = [
        normalized
        for path in paths
        if (normalized := _normalize_path(path))
    ]
    ci_changed = any(_matches(path, (".github/workflows/**",)) for path in files)
    docs_changed = any(_matches(path, DOCS_PATTERNS) for path in files)
    graph_artifact_changed = any(_is_graph_artifact_path(path) for path in files)
    graph_only = bool(files) and all(_matches(path, ("graph/**",)) for path in files)
    return {
        "browser_changed": ci_changed
        or any(_matches(path, BROWSER_PATTERNS) for path in files),
        "ci_changed": ci_changed,
        "docs_changed": docs_changed,
        "docs_only": bool(files) and all(_matches(path, DOCS_PATTERNS) for path in files),
        "graph_artifact_changed": graph_artifact_changed,
        "graph_changed": any(_matches(path, ("graph/**",)) for path in files),
        "graph_only": graph_only,
        "package_changed": ci_changed
        or any(_matches(path, PACKAGE_PATTERNS) for path in files),
        "similarity_changed": ci_changed
        or any(_matches(path, SIMILARITY_PATTERNS) for path in files),
        "source_changed": ci_changed
        or any(_matches(path, SOURCE_PATTERNS) for path in files),
    }


def write_github_outputs(flags: dict[str, bool], output_path: Path) -> None:
    with output_path.open("a", encoding="utf-8") as output:
        for name in OUTPUT_NAMES:
            output.write(f"{name}={str(flags[name]).lower()}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("changed_files", type=Path)
    args = parser.parse_args(argv)

    files = args.changed_files.read_text(encoding="utf-8").splitlines()
    flags = classify_paths(files)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        write_github_outputs(flags, Path(github_output))

    print("Changed files:")
    for path in [path for line in files if (path := _normalize_path(line))]:
        print(f"  {path}")
    print("Classification:")
    for name in OUTPUT_NAMES:
        print(f"  {name}={str(flags[name]).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
