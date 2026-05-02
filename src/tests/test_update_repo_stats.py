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
            ("entities/harnesses/good.md", b"# harness"),
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
        "harnesses": 1,
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


def test_tarball_stats_uses_report_when_graph_json_is_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(urs, "_MAX_TAR_JSON_BYTES", 8)
    _write_graph_tarball(
        tmp_path,
        [
            (
                "graphify-out/graph-report.md",
                b"# Graph Report\n\n> Nodes: 104078 | Edges: 2881027 | Communities: 50\n",
            ),
            ("graphify-out/graph.json", {"nodes": [{}], "edges": []}),
            ("entities/skills/good.md", b"# skill"),
            ("entities/agents/good.md", b"# agent"),
            ("entities/mcp-servers/a/good.md", b"# mcp"),
            ("entities/harnesses/good.md", b"# harness"),
        ],
    )

    assert urs._read_graph_from_tarball() == {
        "nodes": 104078,
        "edges": 2881027,
        "skills": 1,
        "agents": 1,
        "mcps": 1,
        "harnesses": 1,
        "communities": 50,
    }


def test_test_badge_is_labeled_collected_not_passing() -> None:
    text = "[![Tests](https://img.shields.io/badge/Tests-12_passing-brightgreen.svg)](#)"
    stats = {
        "nodes": None,
        "edges": None,
        "skills": None,
        "agents": None,
        "mcps": None,
        "harnesses": None,
        "communities": None,
    }
    patched = text
    for pattern, replacement in urs.build_replacements(stats, tests=34, converted=None):
        patched = pattern.sub(replacement, patched)

    assert "Tests-34_collected" in patched
    assert "_passing" not in patched


def test_harness_aware_readme_prose_is_updated() -> None:
    text = (
        "walks a **1,000 skills, 20 agents, 30 MCP servers, "
        "and 4 cataloged harnesses** graph"
    )
    stats = {
        "nodes": None,
        "edges": None,
        "skills": 92815,
        "agents": 464,
        "mcps": 10787,
        "harnesses": 13,
        "communities": None,
    }
    patched = text
    for pattern, replacement in urs.build_replacements(stats, tests=None, converted=None):
        patched = pattern.sub(replacement, patched)

    assert "**92,815 skills, 464 agents, 10,787 MCP servers, and 13 cataloged harnesses**" in patched


def test_read_test_count_prefers_project_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _collect(candidate: str) -> int | None:
        calls.append(candidate)
        return 34 if candidate == "python" else 30

    monkeypatch.setattr(urs.sys, "executable", "python3")
    monkeypatch.setattr(urs, "_pytest_collect", _collect)

    assert urs.read_test_count() == 34
    assert calls == ["python"]


def test_uncollected_importorskip_tests_are_added_to_collection_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    tests_dir = tmp_path / "src" / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_browser.py").write_text(
        "import pytest\n"
        "pytest.importorskip('playwright.sync_api')\n"
        "def test_one(): pass\n"
        "def test_two(): pass\n",
        encoding="utf-8",
    )
    (tests_dir / "test_present.py").write_text(
        "import pytest\n"
        "pytest.importorskip('already.available')\n"
        "def test_present(): pass\n",
        encoding="utf-8",
    )

    stdout = "src/tests/test_present.py::test_present\n1 test collected\n"
    assert urs._uncollected_importorskip_test_count(stdout) == 2
