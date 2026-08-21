from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPH_ARCHIVES = (
    "graph/wiki-graph.tar.gz",
    "graph/wiki-graph-runtime.tar.gz",
)


def test_graph_archives_are_release_assets_not_git_lfs_files() -> None:
    manifest = json.loads(
        (REPO_ROOT / "graph" / "release-artifacts.json").read_text(encoding="utf-8")
    )

    hydrated_paths = {
        artifact["path"] for artifact in manifest["artifacts"] if artifact["hydrate"] is True
    }
    assert hydrated_paths == set(GRAPH_ARCHIVES)

    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "filter=lfs" not in attributes

    if not (REPO_ROOT / ".git").exists():
        return
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", *GRAPH_ARCHIVES],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode != 0
