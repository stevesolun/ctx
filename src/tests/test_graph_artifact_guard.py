from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType


def _load_guard(repo_root: Path) -> ModuleType:
    script = repo_root / "scripts" / "graph_artifact_guard.py"
    spec = importlib.util.spec_from_file_location("graph_artifact_guard", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout


def test_graph_artifact_guard_parks_and_unparks_tracked_artifacts(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    guard = _load_guard(repo_root)

    repo = tmp_path / "repo"
    graph = repo / "graph"
    graph.mkdir(parents=True)
    _git(tmp_path, "init", "repo")
    _git(repo, "config", "user.email", "ctx@example.invalid")
    _git(repo, "config", "user.name", "ctx test")

    artifact = graph / "wiki-graph.tar.gz"
    artifact.write_bytes(b"fake graph artifact")
    _git(repo, "add", "graph/wiki-graph.tar.gz")
    _git(repo, "commit", "-m", "track graph artifact")

    assert guard.main(["--repo", str(repo), "park"]) == 0
    assert _git(repo, "ls-files", "-v", "graph/wiki-graph.tar.gz").startswith("S ")

    assert guard.main(["--repo", str(repo), "unpark"]) == 0
    assert not _git(repo, "ls-files", "-v", "graph/wiki-graph.tar.gz").startswith(
        "S "
    )
