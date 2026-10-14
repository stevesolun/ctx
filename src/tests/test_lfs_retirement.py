from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPH_INFRASTRUCTURE = {"graph/README.md", "graph/empty-graph.json"}


def test_empty_graph_infrastructure_replaces_the_owned_release_catalog() -> None:
    """ADR-017 retains the graph structure, not the retired corpus or manifest."""
    empty_graph = json.loads((REPO_ROOT / "graph/empty-graph.json").read_text(encoding="utf-8"))
    assert empty_graph == {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [],
        "links": [],
    }
    assert (REPO_ROOT / "graph/README.md").is_file()


def test_current_graph_and_corpus_have_no_git_lfs_storage_dependency() -> None:
    """Check the current index only; retirement must not rewrite Git history."""

    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "filter=lfs" not in attributes

    if not (REPO_ROOT / ".git").exists():
        return
    # Ignored operator archives are not distributed source and are outside the
    # deletion boundary. Package-content checks separately forbid shipping them.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "graph", "imported-skills"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert set(tracked.stdout.rstrip("\0").split("\0")) == GRAPH_INFRASTRUCTURE
