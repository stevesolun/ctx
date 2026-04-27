from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import update_repo_stats as urs  # noqa: E402


def _add_bytes(tf: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    tf.addfile(info, io.BytesIO(body))


def _add_json(tf: tarfile.TarFile, name: str, body: object) -> None:
    _add_bytes(tf, name, json.dumps(body).encode("utf-8"))


def _write_graph_tarball(root: Path, entries: list[tuple[str, object | bytes]]) -> None:
    graph_dir = root / "graph"
    graph_dir.mkdir()
    with tarfile.open(graph_dir / "wiki-graph.tar.gz", "w:gz") as tf:
        for name, body in entries:
            if isinstance(body, bytes):
                _add_bytes(tf, name, body)
            else:
                _add_json(tf, name, body)


def test_tarball_stats_only_trust_safe_regular_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    _write_graph_tarball(
        tmp_path,
        [
            ("./graphify-out/graph.json", {"nodes": [{}, {}], "edges": [{}, {}, {}]}),
            ("./graphify-out/communities.json", {"total_communities": 4}),
            ("./entities/skills/good.md", b"# skill"),
            ("entities/agents/good.md", b"# agent"),
            ("entities/mcp-servers/a/good.md", b"# mcp"),
            ("shadow/entities/skills/ignored.md", b"# ignored"),
            ("entities/skills/../ignored.md", b"# ignored"),
        ],
    )

    assert urs._read_graph_from_tarball() == {
        "nodes": 2,
        "edges": 3,
        "skills": 1,
        "agents": 1,
        "mcps": 1,
        "communities": 4,
    }


def test_tarball_stats_reject_suffix_impersonation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    _write_graph_tarball(
        tmp_path,
        [
            ("evil/graphify-out/graph.json", {"nodes": [{}], "edges": []}),
            ("entities/skills/good.md", b"# skill"),
        ],
    )

    assert urs._read_graph_from_tarball() is None


def test_tarball_stats_reject_non_regular_json_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    graph_dir = tmp_path / "graph"
    graph_dir.mkdir()
    with tarfile.open(graph_dir / "wiki-graph.tar.gz", "w:gz") as tf:
        info = tarfile.TarInfo("graphify-out/graph.json")
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        _add_bytes(tf, "entities/skills/good.md", b"# skill")

    assert urs._read_graph_from_tarball() is None


def test_tarball_stats_reject_oversized_json_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(urs, "_MAX_TAR_JSON_BYTES", 8)
    _write_graph_tarball(
        tmp_path,
        [
            ("graphify-out/graph.json", {"nodes": [{}], "edges": []}),
            ("entities/skills/good.md", b"# skill"),
        ],
    )

    assert urs._read_graph_from_tarball() is None
