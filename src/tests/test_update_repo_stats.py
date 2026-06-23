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


def test_graph_contract_stats_use_preflight_exact_counts() -> None:
    stats = urs._read_graph_contract_stats()

    assert stats is not None
    assert stats["nodes"] == 79958
    assert stats["edges"] == 1778069
    assert stats["skills"] == 68494
    assert stats["agents"] == 467
    assert stats["mcps"] == 10790
    assert stats["harnesses"] == 207
    assert stats["skills_sh_entries"] == 67024
    assert stats["skills_sh_bodies"] == 67024
    assert stats["semantic_edges"] == 1088763
    assert stats["tag_edges"] == 474837
    assert stats["token_edges"] == 280275
    assert stats["harness_edges"] == 5063


def test_read_graph_stats_prefers_tarball_over_preflight_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = {
        "nodes": 10,
        "edges": 20,
        "skills": 3,
        "agents": 4,
        "mcps": 5,
        "harnesses": 6,
        "communities": 7,
    }
    tarball = {
        "nodes": 100,
        "edges": 200,
        "skills": 30,
        "agents": 40,
        "mcps": 50,
        "harnesses": 60,
        "communities": 70,
    }
    monkeypatch.setattr(urs, "_read_graph_contract_stats", lambda: contract)
    monkeypatch.setattr(urs, "_read_graph_from_tarball", lambda: tarball)

    assert urs.read_graph_stats() is tarball


def test_read_graph_stats_falls_back_to_preflight_contract_when_tarball_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = {
        "nodes": 10,
        "edges": 20,
        "skills": 3,
        "agents": 4,
        "mcps": 5,
        "harnesses": 6,
        "communities": 7,
    }
    monkeypatch.setattr(urs, "_read_graph_from_tarball", lambda: None)
    monkeypatch.setattr(urs, "_read_graph_contract_stats", lambda: contract)

    assert urs.read_graph_stats() is contract


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
    assert (
        "](https://github.com/stevesolun/ctx/actions/workflows/test.yml)"
        in patched
    )
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
        "| Total nodes | **1,111** |",
        "| Curated core nodes | **222** (11 skills + 22 agents + 33 MCP servers + 4 harnesses) |",
        "| Body-backed skill nodes | **889** hydrated installable skill entries |",
        "| Total edges | **2,222** |",
        "| Hydrated skill incident edges | **1,234** |",
        "| Hydrated skill semantic incident edges | **567** |",
        "| Edge sources (overlap-deduped) | semantic 123 - tag 456 - token 789 |",
        "| Cross-type edges (skill <-> agent) | ~12 |",
        "| Cross-type edges (skill <-> MCP) | ~34 |",
        "| Cross-type edges (agent <-> MCP) | ~5 |",
        "| Harness edges | **6** |",
    ])
    stats = {
        "nodes": 12345,
        "edges": 67890,
        "skills": 4000,
        "agents": 50,
        "mcps": 600,
        "harnesses": 7,
        "communities": 8,
        "skills_sh_entries": 3000,
        "skills_sh_bodies": 3000,
        "semantic_edges": 23456,
        "tag_edges": 12345,
        "token_edges": 6789,
        "hydrated_incident_edges": 45678,
        "hydrated_semantic_incident_edges": 2345,
        "cross_skill_agent_edges": 321,
        "cross_skill_mcp_edges": 654,
        "cross_agent_mcp_edges": 87,
        "harness_edges": 98,
    }
    patched = text
    for pattern, replacement in urs.build_replacements(stats, tests=None, converted=None):
        patched = pattern.sub(replacement, patched)

    assert "| Total nodes | **12,345** |" in patched
    assert "| Curated core nodes | **9,345** (1,000 skills + 50 agents + 600 MCP servers + 7 harnesses) |" in patched
    assert "| Body-backed skill nodes | **3,000** hydrated installable skill entries |" in patched
    assert "| Total edges | **67,890** |" in patched
    assert "semantic 23,456 - tag 12,345 - token 6,789" in patched
    assert "| Cross-type edges (skill <-> agent) | ~321 |" in patched
    assert "| Cross-type edges (skill <-> MCP) | ~654 |" in patched
    assert "| Cross-type edges (agent <-> MCP) | ~87 |" in patched
    assert "| Harness edges | **98** |" in patched


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


def test_published_inventory_prose_uses_exact_graph_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "\n".join([
        "badge/Graph-79%2C958_nodes_/_1.8M_edges-red",
        "- **2.6M graph edges** across semantic similarity.",
        "with 91K+ skill pages, 460+ agents, 10K+ MCP servers, and 207 harnesses",
        "from the 91K+ skills, 460+ agents, and 10K+ MCP servers",
        "and the full ~439 MiB wiki tarball with **79,958 nodes / 1,778,069 edges / 52 Louvain communities**.",
    ])
    stats = {
        "nodes": 79958,
        "edges": 1778069,
        "skills": 68494,
        "agents": 467,
        "mcps": 10790,
        "harnesses": 207,
        "communities": 52,
    }
    monkeypatch.setattr(urs, "_full_wiki_tarball_mib", lambda: 314)

    patched = text
    for pattern, replacement in urs.build_replacements(stats, tests=None, converted=None):
        patched = pattern.sub(replacement, patched)

    assert "badge/Graph-79%2C958_nodes_/_1%2C778%2C069_edges-red" in patched
    assert "**1,778,069 graph edges**" in patched
    assert "with 68,494 skill pages, 467 agents, 10,790 MCP servers, and 207 harnesses" in patched
    assert "from the 68,494 skills, 467 agents, and 10,790 MCP servers" in patched
    assert "full ~314 MiB wiki tarball" in patched
    assert "1.8M_edges" not in patched
    assert "2.6M" not in patched
    assert "91K+" not in patched


def test_readme_entity_badges_are_updated() -> None:
    text = "\n".join([
        "[![Graph](https://img.shields.io/badge/Graph-1_nodes_/_2_edges-red.svg)](graph/)",
        "[![Skills](https://img.shields.io/badge/Skills-1-blue.svg)](graph/)",
        "[![Agents](https://img.shields.io/badge/Agents-2-purple.svg)](graph/)",
        "[![MCPs](https://img.shields.io/badge/MCPs-3-pink.svg)](graph/)",
        "[![Harnesses](https://img.shields.io/badge/Harnesses-4-orange.svg)](graph/)",
    ])
    stats = {
        "nodes": None,
        "edges": None,
        "skills": 12345,
        "agents": 6789,
        "mcps": 9012,
        "harnesses": 1234,
        "communities": None,
    }
    patched = text
    for pattern, replacement in urs.build_replacements(stats, tests=None, converted=None):
        patched = pattern.sub(replacement, patched)

    assert "badge/Skills-12%2C345-blue" in patched
    assert "badge/Agents-6%2C789-purple" in patched
    assert "badge/MCPs-9%2C012-pink" in patched
    assert "badge/Harnesses-1%2C234-orange" in patched
    assert "](https://stevesolun.github.io/ctx/knowledge-graph/)" in patched
    assert "](https://stevesolun.github.io/ctx/catalog/?type=skill)" in patched
    assert "](https://stevesolun.github.io/ctx/catalog/?type=agent)" in patched
    assert "](https://stevesolun.github.io/ctx/catalog/?type=mcp-server)" in patched
    assert "](https://stevesolun.github.io/ctx/catalog/?type=harness)" in patched
    assert "127.0.0.1" not in patched


def test_github_about_description_uses_entity_counts() -> None:
    stats = {
        "nodes": 123456,
        "edges": 789000,
        "skills": 4321,
        "agents": 56,
        "mcps": 789,
        "harnesses": 10,
        "communities": 52,
    }

    description = urs.build_github_about_description(stats)

    assert description.startswith("Recommendation layer for loading the right")
    assert "right development moment" in description
    assert "123,456-node LLM-wiki graph" in description
    assert "4,321 skills" in description
    assert "56 agents" in description
    assert "789 MCPs" in description
    assert "10 harnesses" in description


def test_patch_readme_checks_docs_and_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readme = tmp_path / "README.md"
    docs = tmp_path / "docs"
    docs.mkdir()
    docs_index = docs / "index.md"
    docs_knowledge = docs / "knowledge-graph.md"
    docs_catalog = docs / "catalog.md"
    readme.write_text("Graph has 10 nodes and 20 edges\n", encoding="utf-8")
    docs_index.write_text("3 tests collected\n", encoding="utf-8")
    docs_knowledge.write_text("| Total nodes | **10** |\n", encoding="utf-8")
    docs_catalog.write_text(
        "\n".join([
            '<div id="ctx-catalog-grid" class="ctx-catalog-grid">',
            "<!-- ctx-catalog:begin -->",
            '<article class="ctx-catalog-card" data-type="skill">',
            '<p class="ctx-catalog-muted">1 entities</p>',
            "</article>",
            "<!-- ctx-catalog:end -->",
            "</div>",
        ]),
        encoding="utf-8",
    )
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(urs, "README", readme)
    monkeypatch.setattr(urs, "DOCS_INDEX", docs_index)
    monkeypatch.setattr(urs, "DOCS_KNOWLEDGE_GRAPH", docs_knowledge)
    monkeypatch.setattr(urs, "DOCS_CATALOG", docs_catalog)
    monkeypatch.setattr(
        urs,
        "read_graph_stats",
        lambda: {
            "nodes": 123,
            "edges": 456,
            "skills": 1234,
            "agents": 56,
            "mcps": 7890,
            "harnesses": 12,
            "communities": 3,
        },
    )
    monkeypatch.setattr(urs, "read_test_count", lambda **_kwargs: None)
    monkeypatch.setattr(urs, "read_converted_count", lambda: None)

    assert urs.patch_readme(check_only=True) == 1
    assert urs.patch_readme(check_only=False) == 0

    patched = docs_catalog.read_text(encoding="utf-8")
    assert ">1,234 entities</p>" in patched
    assert ">56 entities</p>" in patched
    assert ">7,890 entities</p>" in patched
    assert ">12 entities</p>" in patched
    assert "./?type=harness&q=tool+access" in patched


def test_read_test_count_prefers_project_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _collect(candidate: str) -> int | None:
        calls.append(candidate)
        return 34 if candidate == "python" else 30

    monkeypatch.setattr(urs.sys, "executable", "python3")
    monkeypatch.setattr(urs, "_pytest_collect", _collect)
    monkeypatch.setenv("CTX_UPDATE_REPO_STATS_LIVE_TESTS", "1")

    assert urs.read_test_count() == 34
    assert calls == ["python"]


def test_read_test_count_uses_checked_in_count_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readme = tmp_path / "README.md"
    docs_index = tmp_path / "docs" / "index.md"
    docs_index.parent.mkdir()
    readme.write_text(
        "[![Tests](https://img.shields.io/badge/Tests-3981_collected-brightgreen.svg)](#)",
        encoding="utf-8",
    )
    docs_index.write_text("3,981 tests collected", encoding="utf-8")
    monkeypatch.setattr(urs, "README", readme)
    monkeypatch.setattr(urs, "DOCS_INDEX", docs_index)
    monkeypatch.setattr(
        urs,
        "_pytest_collect",
        lambda _candidate: pytest.fail("default test count should not collect"),
    )

    assert urs.read_test_count() == 3981


def test_read_test_count_live_mode_ignores_checked_in_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readme = tmp_path / "README.md"
    docs_index = tmp_path / "docs" / "index.md"
    docs_index.parent.mkdir()
    readme.write_text(
        "[![Tests](https://img.shields.io/badge/Tests-3981_collected-brightgreen.svg)](#)",
        encoding="utf-8",
    )
    docs_index.write_text("3,981 tests collected", encoding="utf-8")
    monkeypatch.setattr(urs, "README", readme)
    monkeypatch.setattr(urs, "DOCS_INDEX", docs_index)
    monkeypatch.setattr(urs, "_pytest_collect", lambda _candidate: 3982)

    assert urs.read_test_count(live=True) == 3982


def test_patch_readme_check_uses_live_test_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readme = tmp_path / "README.md"
    docs = tmp_path / "docs"
    docs.mkdir()
    docs_index = docs / "index.md"
    docs_knowledge = docs / "knowledge-graph.md"
    docs_catalog = docs / "catalog.md"
    readme.write_text(
        "[![Tests](https://img.shields.io/badge/Tests-3981_collected-brightgreen.svg)](#)",
        encoding="utf-8",
    )
    docs_index.write_text("3,981 tests collected", encoding="utf-8")
    docs_knowledge.write_text("", encoding="utf-8")
    docs_catalog.write_text("", encoding="utf-8")
    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(urs, "README", readme)
    monkeypatch.setattr(urs, "DOCS_INDEX", docs_index)
    monkeypatch.setattr(urs, "DOCS_KNOWLEDGE_GRAPH", docs_knowledge)
    monkeypatch.setattr(urs, "DOCS_CATALOG", docs_catalog)
    monkeypatch.setattr(
        urs,
        "read_graph_stats",
        lambda: {
            "nodes": None,
            "edges": None,
            "skills": None,
            "agents": None,
            "mcps": None,
            "harnesses": None,
            "communities": None,
        },
    )
    monkeypatch.setattr(urs, "_pytest_collect", lambda _candidate: 3982)
    monkeypatch.setattr(urs, "read_converted_count", lambda: None)

    assert urs.patch_readme(check_only=True) == 1


def test_patch_readme_update_uses_live_test_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readme = tmp_path / "README.md"
    docs = tmp_path / "docs"
    docs.mkdir()
    docs_index = docs / "index.md"
    docs_knowledge = docs / "knowledge-graph.md"
    docs_catalog = docs / "catalog.md"
    readme.write_text(
        "[![Tests](https://img.shields.io/badge/Tests-3981_collected-brightgreen.svg)](#)",
        encoding="utf-8",
    )
    docs_index.write_text("3,981 tests collected", encoding="utf-8")
    docs_knowledge.write_text("", encoding="utf-8")
    docs_catalog.write_text("", encoding="utf-8")
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(urs, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(urs, "README", readme)
    monkeypatch.setattr(urs, "DOCS_INDEX", docs_index)
    monkeypatch.setattr(urs, "DOCS_KNOWLEDGE_GRAPH", docs_knowledge)
    monkeypatch.setattr(urs, "DOCS_CATALOG", docs_catalog)
    monkeypatch.setattr(
        urs,
        "read_graph_stats",
        lambda: {
            "nodes": None,
            "edges": None,
            "skills": None,
            "agents": None,
            "mcps": None,
            "harnesses": None,
            "communities": None,
        },
    )

    def fake_read_test_count(**kwargs: object) -> int:
        calls.append(kwargs)
        return 3982

    monkeypatch.setattr(urs, "read_test_count", fake_read_test_count)
    monkeypatch.setattr(urs, "read_converted_count", lambda: None)

    assert urs.patch_readme(check_only=False) == 0
    assert calls == [{"live": True}]
    assert "Tests-3982_collected" in readme.read_text(encoding="utf-8")
    assert "3,982 tests collected" in docs_index.read_text(encoding="utf-8")


def test_pytest_collect_uses_inprocess_no_cache_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class FakePytest:
        @staticmethod
        def main(args: list[str]) -> int:
            calls.append(args)
            print("3980 tests collected")
            return 0

    monkeypatch.setitem(sys.modules, "pytest", FakePytest)
    monkeypatch.setattr(urs, "_uncollected_importorskip_test_count", lambda _stdout: 0)
    monkeypatch.setattr(
        urs.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("pytest collection should not spawn"),
    )

    assert urs._pytest_collect("python") == 3980
    assert calls == [[
        "tests/",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
    ]]


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
