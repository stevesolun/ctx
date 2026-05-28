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
    overlay = graph / "entity-overlays.jsonl"
    artifact.write_bytes(b"fake graph artifact")
    overlay.write_text('{"nodes":[],"edges":[]}\n', encoding="utf-8")
    _git(repo, "add", "graph/wiki-graph.tar.gz", "graph/entity-overlays.jsonl")
    _git(repo, "commit", "-m", "track graph artifact")

    assert guard.main(["--repo", str(repo), "park"]) == 0
    assert _git(repo, "ls-files", "-v", "graph/wiki-graph.tar.gz").startswith("S ")
    assert _git(repo, "ls-files", "-v", "graph/entity-overlays.jsonl").startswith("S ")

    assert guard.main(["--repo", str(repo), "unpark"]) == 0
    assert not _git(repo, "ls-files", "-v", "graph/wiki-graph.tar.gz").startswith(
        "S "
    )
    assert not _git(repo, "ls-files", "-v", "graph/entity-overlays.jsonl").startswith(
        "S "
    )


def test_graph_artifact_guard_removes_only_stale_graph_files(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    guard = _load_guard(repo_root)

    repo = tmp_path / "repo"
    graph = repo / "graph"
    graph.mkdir(parents=True)
    _git(tmp_path, "init", "repo")

    artifact = graph / "wiki-graph.tar.gz"
    staged = graph / "wiki-graph.tar.gz.staged"
    partial = graph / "wiki-graph-runtime.tar.gz.partial"
    outside = repo / "wiki-graph.tar.gz.staged"
    artifact.write_bytes(b"real graph artifact")
    staged.write_bytes(b"interrupted staged artifact")
    partial.write_bytes(b"interrupted partial artifact")
    outside.write_bytes(b"not under graph")

    assert guard.main(["--repo", str(repo), "clean-stale"]) == 0

    assert artifact.exists()
    assert not staged.exists()
    assert not partial.exists()
    assert outside.exists()


def test_graph_artifact_guard_prune_is_lfs_only_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    guard = _load_guard(repo_root)
    calls: list[tuple[str, ...]] = []

    def fake_run_git(
        _repo: Path,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(guard, "_run_git", fake_run_git)

    guard._prune(tmp_path, include_lfs=True, include_git_prune=False)

    assert ("lfs", "prune", "--verbose") in calls
    assert not any(call[:1] == ("prune",) for call in calls)


def test_graph_artifact_guard_prune_requires_explicit_git_prune(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    guard = _load_guard(repo_root)
    calls: list[tuple[str, ...]] = []

    def fake_run_git(
        _repo: Path,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(guard, "_run_git", fake_run_git)

    guard._prune(tmp_path, include_lfs=False, include_git_prune=True)

    assert ("prune", "--expire=now", "--verbose") in calls
    assert not any(call[:2] == ("lfs", "prune") for call in calls)
