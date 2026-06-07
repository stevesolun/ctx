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

    stats = urs._read_graph_from_tarball()
    assert stats is not None
    assert {
        key: stats[key]
        for key in ("nodes", "edges", "skills", "agents", "mcps", "harnesses", "communities")
    } == {
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


def test_docs_landing_test_count_is_updated() -> None:
    text = "CI-matrixed, 3,617 tests collected. Ships console scripts."
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
    for pattern, replacement in urs.build_docs_replacements(
        stats=stats,
        tests=3619,
        converted=None,
    ):
        patched = pattern.sub(replacement, patched)

    assert "3,619 tests collected" in patched
    assert "3,617 tests collected" not in patched


def test_knowledge_graph_counts_are_updated() -> None:
    text = "\n".join([
        "| Total nodes | **102,925** |",
        "| Curated core nodes | **13,460** (1,998 skills + 467 agents + 10,788 MCP servers + 207 harnesses) |",
        "| Body-backed skill nodes | **89,463** hydrated installable skill entries |",
        "| Total edges | **2,913,930** |",
        "| Hydrated skill incident edges | **2,605,000** |",
        "| Hydrated skill semantic incident edges | **1,500,000** |",
        "| Edge sources (overlap-deduped) | semantic 1,683,163 - tag 897,754 - token 433,245 |",
        "| Cross-type edges (skill <-> agent) | ~67K |",
        "| Cross-type edges (skill <-> MCP) | ~41K |",
        "| Cross-type edges (agent <-> MCP) | ~223 |",
        "| Harness edges | **6,571** |",
    ])
    stats = {
        "nodes": 102928,
        "edges": 2913960,
        "skills": 91464,
        "agents": 467,
        "mcps": 10790,
        "harnesses": 207,
        "communities": 52,
        "skills_sh_entries": 89465,
        "skills_sh_bodies": 89465,
        "semantic_edges": 1683193,
        "tag_edges": 897784,
        "token_edges": 433245,
        "hydrated_incident_edges": 2605721,
        "hydrated_semantic_incident_edges": 1500648,
        "cross_skill_agent_edges": 66799,
        "cross_skill_mcp_edges": 41521,
        "cross_agent_mcp_edges": 229,
        "harness_edges": 6576,
    }
    patched = text
    for pattern, replacement in urs.build_replacements(stats, tests=None, converted=None):
        patched = pattern.sub(replacement, patched)

    assert "| Total nodes | **102,928** |" in patched
    assert "| Curated core nodes | **13,463** (1,999 skills + 467 agents + 10,790 MCP servers + 207 harnesses) |" in patched
    assert "| Body-backed skill nodes | **89,465** hydrated installable skill entries |" in patched
    assert "| Total edges | **2,913,960** |" in patched
    assert "semantic 1,683,193 - tag 897,784 - token 433,245" in patched
    assert "| Cross-type edges (skill <-> agent) | ~66,799 |" in patched
    assert "| Cross-type edges (skill <-> MCP) | ~41,521 |" in patched
    assert "| Cross-type edges (agent <-> MCP) | ~229 |" in patched
    assert "| Harness edges | **6,576** |" in patched


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

    assert "**92,815 skills, 464 agents, 10,787 MCP servers, and 13 harnesses**" in patched


def test_readme_entity_badges_are_updated() -> None:
    text = "\n".join([
        "[![Skills](https://img.shields.io/badge/Skills-1-blue.svg)](graph/)",
        "[![Agents](https://img.shields.io/badge/Agents-2-purple.svg)](graph/)",
        "[![MCPs](https://img.shields.io/badge/MCPs-3-pink.svg)](graph/)",
        "[![Harnesses](https://img.shields.io/badge/Harnesses-4-orange.svg)](graph/)",
    ])
    stats = {
        "nodes": None,
        "edges": None,
        "skills": 91464,
        "agents": 467,
        "mcps": 10790,
        "harnesses": 207,
        "communities": None,
    }
    patched = text
    for pattern, replacement in urs.build_replacements(stats, tests=None, converted=None):
        patched = pattern.sub(replacement, patched)

    assert "badge/Skills-91%2C464-blue" in patched
    assert "badge/Agents-467-purple" in patched
    assert "badge/MCPs-10%2C790-pink" in patched
    assert "badge/Harnesses-207-orange" in patched


def test_patch_readme_checks_knowledge_graph_doc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readme = tmp_path / "README.md"
    docs = tmp_path / "docs"
    docs.mkdir()
    docs_index = docs / "index.md"
    docs_knowledge = docs / "knowledge-graph.md"
    readme.write_text("Graph has 10 nodes and 20 edges\n", encoding="utf-8")
    docs_index.write_text("3 tests collected\n", encoding="utf-8")
    docs_knowledge.write_text("| Total nodes | **10** |\n", encoding="utf-8")
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(urs, "README", readme)
    monkeypatch.setattr(urs, "DOCS_INDEX", docs_index)
    monkeypatch.setattr(urs, "DOCS_KNOWLEDGE_GRAPH", docs_knowledge)
    monkeypatch.setattr(
        urs,
        "read_graph_stats",
        lambda: {
            "nodes": 11,
            "edges": 21,
            "skills": None,
            "agents": None,
            "mcps": None,
            "harnesses": None,
            "communities": 1,
        },
    )
    monkeypatch.setattr(urs, "read_test_count", lambda: None)
    monkeypatch.setattr(urs, "read_converted_count", lambda: None)

    assert urs.patch_readme(check_only=True) == 1


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
