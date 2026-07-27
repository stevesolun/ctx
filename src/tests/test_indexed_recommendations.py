from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import networkx as nx
import pytest

from ctx.adapters.generic.ctx_core_tools import (
    CtxCoreToolbox,
    _recommendation_index_is_fresh,
)
from ctx.adapters.generic.providers import ToolCall
from ctx.core.entity_types import entity_page_path
from ctx.core.graph.graph_packs import write_base_pack
from ctx.core.graph.graph_store import (
    build_graph_store_from_graph_dir,
)
from ctx.core.resolve.recommendations import (
    query_to_tags,
    recommend_by_tags,
    recommend_by_tags_indexed,
    resolve_recommendation_aliases_indexed,
)


def _build_world(
    tmp_path: Path,
    *,
    include_external_catalog_nodes: bool = True,
) -> tuple[Path, Path, Path, nx.Graph]:
    wiki = tmp_path / "wiki"
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph_path = graph_dir / "graph.json"
    index_path = graph_dir / "graph-store.sqlite3"

    graph = nx.Graph()
    graph.graph["ctx_graph_path"] = str(graph_path)
    if include_external_catalog_nodes:
        graph.graph["source_catalog_nodes"] = {"skills.sh": 1}
    graph.add_node(
        "skill:python-testing",
        label="python-testing",
        type="skill",
        tags=["python", "testing"],
        status="cataloged",
    )
    graph.add_node(
        "skill:python-planner",
        label="python-planner",
        type="skill",
        tags=["python", "planning", "testing"],
    )
    graph.add_node(
        "skill:javascript-testing",
        label="javascript-testing",
        type="skill",
        tags=["javascript", "testing"],
    )
    graph.add_node(
        "skill:go-helper",
        label="go-helper",
        type="skill",
        tags=["go"],
    )
    graph.add_node(
        "skill:remote-python",
        label="remote-python",
        type="skill",
        tags=["python", "testing"],
        status="available",
    )
    graph.add_node(
        "skill:hidden-python",
        label="hidden-python",
        type="skill",
        tags=["python", "testing"],
        never_load=True,
    )
    graph.add_node(
        "skill:alias-shell",
        label="runner-shell",
        name="pytest accelerator",
        type="skill",
        tags=["shell"],
    )
    graph.add_node(
        "skill:string-external",
        label="string-external-python",
        type="skill",
        tags=["python"],
        external="false",
    )
    graph.add_node(
        "agent:python-reviewer",
        label="python-reviewer",
        type="agent",
        tags=["python", "review", "testing"],
    )
    graph.add_node(
        "mcp-server:codex-cli",
        label="codex-cli",
        type="mcp-server",
        tags=["python", "testing"],
    )
    graph.add_node(
        "mcp-server:local-files",
        label="local-files",
        type="mcp-server",
        tags=["python", "files", "testing"],
    )
    graph.add_node(
        "skill:shared-skill",
        label="shared-tool",
        type="skill",
        tags=["shared"],
    )
    graph.add_node(
        "agent:shared-agent",
        label="shared-tool",
        type="agent",
        tags=["shared"],
    )
    graph.add_node(
        "external-skill:python-market",
        label="python-market",
        type="external-skill",
        tags=["python", "testing"],
        external=True,
    )
    graph.add_edges_from(
        [
            ("skill:python-testing", "agent:python-reviewer"),
            ("skill:python-testing", "mcp-server:local-files"),
            ("skill:python-testing", "skill:python-planner"),
            ("agent:python-reviewer", "mcp-server:local-files"),
            ("skill:javascript-testing", "mcp-server:codex-cli"),
            ("skill:shared-skill", "agent:shared-agent"),
        ]
    )
    graph_path.write_text(
        json.dumps(nx.node_link_data(graph, edges="edges")),
        encoding="utf-8",
    )
    build_graph_store_from_graph_dir(graph_dir, index_path)

    for slug in (
        "python-testing",
        "python-planner",
        "javascript-testing",
        "alias-shell",
        "string-external",
    ):
        body = wiki / "converted" / slug / "SKILL.md"
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text(f"# {slug}\n", encoding="utf-8")
    for entity_type, slug in (
        ("agent", "python-reviewer"),
        ("mcp-server", "codex-cli"),
        ("mcp-server", "local-files"),
    ):
        page = entity_page_path(wiki, entity_type, slug)
        assert page is not None
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(f"# {slug}\n", encoding="utf-8")
    return wiki, graph_path, index_path, graph


def _dispatch_policy_fixture(
    tmp_path: Path,
    *,
    backend: str,
    graph: nx.Graph,
    arguments: dict[str, Any],
    local_skills: tuple[str, ...] = (),
) -> tuple[dict[str, Any], CtxCoreToolbox]:
    wiki = tmp_path / "wiki"
    graph_dir = wiki / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph_path = graph_dir / "graph.json"
    index_path = graph_dir / "graph-store.sqlite3"
    graph.graph["ctx_graph_path"] = str(graph_path)
    graph.graph["source_catalog_nodes"] = {"skills.sh": 1}
    graph_path.write_text(
        json.dumps(nx.node_link_data(graph, edges="edges")),
        encoding="utf-8",
    )
    build_graph_store_from_graph_dir(graph_dir, index_path)
    if backend == "graph":
        index_path.unlink()
    for slug in local_skills:
        body = wiki / "converted" / slug / "SKILL.md"
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text(f"# {slug}\n", encoding="utf-8")

    toolbox = CtxCoreToolbox(
        wiki_dir=wiki,
        graph_path=graph_path,
        lifecycle_dir=tmp_path / "runtime",
        allowed_entity_types=["skill"],
    )
    payload = json.loads(
        toolbox.dispatch(
            ToolCall(
                id=f"policy-{backend}",
                name="ctx__recommend_bundle",
                arguments=arguments,
            )
        )
    )
    assert (toolbox._graph is None) is (backend == "indexed")
    return payload, toolbox


@pytest.mark.parametrize(
    ("query", "entity_types", "minimum"),
    [
        ("python testing", ("skill", "agent", "mcp-server"), 0.0),
        ("pytest accelerator", ("skill",), 0.0),
        ("python review", ("agent",), 0.2),
        ("test", ("skill", "agent"), 0.0),
    ],
)
def test_indexed_ranker_matches_networkx_contract(
    tmp_path: Path,
    query: str,
    entity_types: tuple[str, ...],
    minimum: float,
) -> None:
    _, _, index_path, graph = _build_world(tmp_path)
    tags = query_to_tags(query)

    expected = recommend_by_tags(
        graph,
        tags,
        top_n=20,
        query=query,
        entity_types=entity_types,
        min_normalized_score=minimum,
    )
    indexed = recommend_by_tags_indexed(
        index_path,
        tags,
        top_n=20,
        query=query,
        entity_types=entity_types,
        min_normalized_score=minimum,
    )

    assert indexed is not None
    actual, node_count = indexed
    assert node_count == graph.number_of_nodes()
    assert actual == expected


@pytest.mark.parametrize("backend", ["indexed", "graph"])
def test_policy_filters_before_normalized_score_threshold(
    tmp_path: Path,
    backend: str,
) -> None:
    graph = nx.Graph()
    graph.add_node(
        "skill:python-testing",
        label="python-testing",
        type="skill",
        tags=["python", "testing"],
        status="available",
        source_catalog="skill-index",
        install_command="ctx-skill-install python-testing",
    )
    graph.add_node(
        "skill:ctx-python-testing",
        label="ctx-python-testing",
        type="skill",
        tags=["python", "testing"],
        source="ctx-runtime-availability",
    )

    payload, _ = _dispatch_policy_fixture(
        tmp_path,
        backend=backend,
        graph=graph,
        arguments={
            "query": "python testing",
            "top_k": 1,
            "local_code_task": True,
            "no_api_keys": True,
            "language": "python",
        },
        local_skills=("ctx-python-testing",),
    )

    assert [row["id"] for row in payload["results"]] == ["skill:ctx-python-testing"]
    assert payload["results"][0]["normalized_score"] == 1.0


@pytest.mark.parametrize("backend", ["indexed", "graph"])
def test_language_policy_backfills_beyond_wrong_language_candidates(
    tmp_path: Path,
    backend: str,
) -> None:
    graph = nx.Graph()
    for index in range(60):
        slug = f"aaa-rust-testing-{index:02d}"
        graph.add_node(
            f"skill:{slug}",
            label=slug,
            type="skill",
            tags=["rust", "testing"],
        )
    graph.add_node(
        "skill:zzz-python-testing",
        label="zzz-python-testing",
        type="skill",
        tags=["python", "testing"],
    )

    payload, _ = _dispatch_policy_fixture(
        tmp_path,
        backend=backend,
        graph=graph,
        arguments={
            "query": "testing",
            "top_k": 1,
            "language": "python",
            "include_unavailable": True,
        },
    )

    assert [row["id"] for row in payload["results"]] == ["skill:zzz-python-testing"]
    assert payload["results"][0]["normalized_score"] == 1.0


@pytest.mark.parametrize("backend", ["indexed", "graph"])
def test_selection_policy_backfills_without_fixed_candidate_cap(
    tmp_path: Path,
    backend: str,
) -> None:
    graph = nx.Graph()
    selected: list[str] = []
    for index in range(60):
        slug = f"aaa-testing-{index:02d}"
        entity_id = f"skill:{slug}"
        selected.append(entity_id)
        graph.add_node(entity_id, label=slug, type="skill", tags=["testing"])
    graph.add_node(
        "skill:zzz-testing",
        label="zzz-testing",
        type="skill",
        tags=["testing"],
    )

    payload, _ = _dispatch_policy_fixture(
        tmp_path,
        backend=backend,
        graph=graph,
        arguments={
            "query": "testing",
            "top_k": 1,
            "selected": selected,
            "include_unavailable": True,
        },
    )

    assert [row["id"] for row in payload["results"]] == ["skill:zzz-testing"]
    assert payload["results"][0]["normalized_score"] == 1.0


@pytest.mark.parametrize("backend", ["indexed", "graph"])
def test_natural_python_testing_reaches_local_runtime_skill(
    tmp_path: Path,
    backend: str,
) -> None:
    graph = nx.Graph()
    for index in range(60):
        slug = f"aaa-python-testing-{index:02d}"
        graph.add_node(
            f"skill:{slug}",
            label=slug,
            type="skill",
            tags=["python", "testing"],
            status="available",
            source_catalog="skill-index",
            install_command=f"ctx-skill-install {slug}",
        )
    graph.add_node(
        "skill:ctx-python-testing",
        label="ctx-python-testing",
        type="skill",
        tags=["ctx", "local", "no-api-key", "python", "testing"],
        source="ctx-runtime-availability",
    )

    payload, _ = _dispatch_policy_fixture(
        tmp_path,
        backend=backend,
        graph=graph,
        arguments={
            "query": "python testing",
            "top_k": 1,
            "local_code_task": True,
            "no_api_keys": True,
            "language": "python",
        },
        local_skills=("ctx-python-testing",),
    )

    assert [row["id"] for row in payload["results"]] == ["skill:ctx-python-testing"]
    assert payload["results"][0]["installable"] is True


def test_indexed_aliases_match_graph_identity_rules(tmp_path: Path) -> None:
    _, _, index_path, graph = _build_world(tmp_path)

    indexed = resolve_recommendation_aliases_indexed(
        index_path,
        typed_ids=["SKILL:PYTHON-TESTING"],
        bare_labels=["python-reviewer", "shared-tool"],
        allowed_entity_types=("skill", "agent", "mcp-server", "harness"),
    )

    assert indexed is not None
    aliases, node_count = indexed
    assert node_count == graph.number_of_nodes()
    assert aliases["skill:python-testing"] == "skill:python-testing"
    assert aliases["python-reviewer"] == "agent:python-reviewer"
    assert "shared-tool" not in aliases


def test_indexed_ranker_matches_short_direct_signals(tmp_path: Path) -> None:
    _, _, index_path, graph = _build_world(tmp_path)

    expected = recommend_by_tags(graph, ["go"], top_n=10, query="go")
    indexed = recommend_by_tags_indexed(index_path, ["go"], top_n=10, query="go")

    assert indexed is not None
    assert indexed[0] == expected


def test_indexed_ranker_matches_external_only_token_score(tmp_path: Path) -> None:
    _, _, index_path, graph = _build_world(tmp_path)
    tags = query_to_tags("market")

    expected = recommend_by_tags(
        graph,
        tags,
        top_n=10,
        query="market",
        entity_types=("external-skill",),
    )
    indexed = recommend_by_tags_indexed(
        index_path,
        tags,
        top_n=10,
        query="market",
        entity_types=("external-skill",),
    )

    assert indexed is not None
    assert indexed[0] == expected


def test_signal_limit_keeps_large_direct_queries_indexed(tmp_path: Path) -> None:
    _, _, index_path, graph = _build_world(tmp_path)
    tags = ["python", *(f"signal-{index}" for index in range(1_100))]

    expected = recommend_by_tags(graph, tags, top_n=10, query="python")
    indexed = recommend_by_tags_indexed(index_path, tags, top_n=10, query="python")

    assert len(query_to_tags(" ".join(tags))) == 64
    assert indexed is not None
    assert indexed[0] == expected


def test_toolbox_rejects_oversized_query_without_loading_graph(tmp_path: Path) -> None:
    wiki, graph_path, _, _ = _build_world(tmp_path)
    toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=graph_path)
    query = " ".join(f"signal-{index}" for index in range(1_100))

    payload = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="oversized",
                name="ctx__recommend_bundle",
                arguments={"query": query},
            )
        )
    )

    assert payload["error"] == "query is too long; maximum length is 4096 characters"
    assert payload["results"] == []
    assert toolbox._graph is None


def test_indexed_policy_does_not_guess_ambiguous_bare_active_context(
    tmp_path: Path,
) -> None:
    wiki, graph_path, _, _ = _build_world(tmp_path)
    toolbox = CtxCoreToolbox(
        wiki_dir=wiki,
        graph_path=graph_path,
        allowed_entity_types=["skill"],
    )

    payload = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="ambiguous",
                name="ctx__recommend_bundle",
                arguments={
                    "query": "shared",
                    "include_unavailable": True,
                    "active_context": [
                        {
                            "id": "shared-tool",
                            "load_status": "active",
                            "stale": True,
                        }
                    ],
                },
            )
        )
    )

    assert payload["context_policy"]["keep"] == [
        "mcp-server:codex-cli",
        "shared-tool",
    ]
    assert payload["context_policy"]["unload"] == []
    assert payload["context_policy"]["replace"] == []


def test_toolbox_indexed_path_preserves_filters_and_rejection_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki, graph_path, _, _ = _build_world(tmp_path)
    toolbox = CtxCoreToolbox(
        wiki_dir=wiki,
        graph_path=graph_path,
        lifecycle_dir=tmp_path / "runtime",
        recommendation_session_id="indexed-session",
    )

    def fail_graph_load() -> Any:
        raise AssertionError("indexed recommendation unexpectedly loaded graph.json")

    monkeypatch.setattr(toolbox, "_ensure_graph", fail_graph_load)
    arguments = {
        "query": "python testing",
        "selected": ["mcp-server:local-files"],
        "rejected": ["python-reviewer"],
        "local_code_task": True,
        "no_api_keys": True,
        "language": "python",
    }
    first = json.loads(
        toolbox.dispatch(
            ToolCall(id="indexed-1", name="ctx__recommend_bundle", arguments=arguments)
        )
    )
    second = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="indexed-2",
                name="ctx__recommend_bundle",
                arguments={**arguments, "rejected": []},
            )
        )
    )

    assert [row["id"] for row in first["results"]] == ["skill:python-testing"]
    assert [row["id"] for row in second["results"]] == ["skill:python-testing"]
    assert first["selection"]["rejected"] == ["agent:python-reviewer", "python-reviewer"]
    assert first["context_policy"]["baseline"] == ["mcp-server:codex-cli"]
    assert first["context_policy"]["load"] == ["skill:python-testing"]
    assert toolbox._graph is None


def test_external_catalog_rejection_persists_and_suppresses_next_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki, graph_path, index_path, _ = _build_world(
        tmp_path,
        include_external_catalog_nodes=False,
    )
    catalog_path = wiki / "external-catalogs" / "skills-sh" / "catalog.json"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "id": "code-with-beto-skills-local-build",
                        "name": "local-build",
                        "skill_id": "local-build",
                        "source": "code-with-beto/skills",
                        "tags": ["local", "build"],
                        "installs": 100,
                    },
                    {
                        "id": "../unsafe",
                        "name": "unsafe",
                        "tags": ["local", "build"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    ranked = recommend_by_tags_indexed(
        index_path,
        query_to_tags("local build"),
        top_n=5,
        query="local build",
        entity_types=("skill",),
        external_catalog_path=catalog_path,
    )
    assert ranked is not None
    assert ranked[0][0]["name"] == "code-with-beto-skills-local-build"

    def fail_graph_load() -> Any:
        raise AssertionError("indexed recommendation unexpectedly loaded graph.json")

    def toolbox() -> CtxCoreToolbox:
        instance = CtxCoreToolbox(
            wiki_dir=wiki,
            graph_path=graph_path,
            lifecycle_dir=tmp_path / "runtime",
            recommendation_session_id="external-catalog-session",
            allowed_entity_types=["skill"],
        )
        monkeypatch.setattr(instance, "_ensure_graph", fail_graph_load)
        return instance

    external_id = "skill:code-with-beto-skills-local-build"
    first_toolbox = toolbox()
    first = json.loads(
        first_toolbox.dispatch(
            ToolCall(
                id="catalog-reject",
                name="ctx__recommend_bundle",
                arguments={
                    "query": "local build",
                    "include_unavailable": True,
                    "rejected": [
                        external_id,
                        "skill:not-in-catalog",
                        "skill:../unsafe",
                    ],
                    "rejection_mode": "replace",
                },
            )
        )
    )
    second_toolbox = toolbox()
    second = json.loads(
        second_toolbox.dispatch(
            ToolCall(
                id="catalog-next",
                name="ctx__recommend_bundle",
                arguments={
                    "query": "local build",
                    "include_unavailable": True,
                },
            )
        )
    )

    assert external_id not in {row["id"] for row in first["results"]}
    assert second["selection"]["rejected"] == [external_id]
    assert external_id not in {row["id"] for row in second["results"]}
    assert first_toolbox._graph is None
    assert second_toolbox._graph is None


def test_semantic_and_stale_index_requests_fall_back_to_networkx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki, graph_path, index_path, graph = _build_world(tmp_path)
    toolbox = CtxCoreToolbox(wiki_dir=wiki, graph_path=graph_path)
    calls = 0

    def ensure_graph() -> nx.Graph:
        nonlocal calls
        calls += 1
        return graph

    monkeypatch.setattr(toolbox, "_ensure_graph", ensure_graph)
    semantic = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="semantic",
                name="ctx__recommend_bundle",
                arguments={"query": "python testing", "use_semantic_query": True},
            )
        )
    )
    assert semantic["results"]
    assert calls == 1

    graph_path.write_text(
        graph_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    regular = json.loads(
        toolbox.dispatch(
            ToolCall(
                id="stale",
                name="ctx__recommend_bundle",
                arguments={"query": "python testing"},
            )
        )
    )
    assert regular["results"]
    assert calls == 2


def test_index_freshness_uses_pack_manifest_fingerprint_without_recursive_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_dir = tmp_path / "graphify-out"
    pack = graph_dir / "packs" / "base"
    nested = pack / "unrelated" / "deep"
    nested.mkdir(parents=True)
    graph_path = graph_dir / "graph.json"
    index_path = graph_dir / "graph-store.sqlite3"
    graph = nx.Graph()
    graph.add_node("skill:base", label="base", type="skill", tags=["base"])
    write_base_pack(
        pack_dir=pack,
        pack_id="base",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="bge-small-en-v1.5",
        graph=graph,
    )
    build_graph_store_from_graph_dir(graph_dir, index_path)
    (nested / "large-unrelated.bin").write_bytes(b"x")

    def fail_recursive_scan(self: Path, pattern: str) -> Any:
        raise AssertionError(f"unexpected recursive scan of {self} with {pattern}")

    monkeypatch.setattr(Path, "rglob", fail_recursive_scan)

    assert _recommendation_index_is_fresh(index_path, graph_path) is True
    manifest = pack / "graph-pack-manifest.json"
    original_stat = manifest.stat()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["config_hash"] = "changed-sha"
    manifest.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.utime(
        manifest,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    assert _recommendation_index_is_fresh(index_path, graph_path) is False


def test_index_freshness_rejects_graph_tamper_with_preserved_mtime(
    tmp_path: Path,
) -> None:
    _, graph_path, index_path, _ = _build_world(tmp_path)
    original_stat = graph_path.stat()
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    payload["tampered"] = True
    graph_path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(
        graph_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert _recommendation_index_is_fresh(index_path, graph_path) is False


def test_index_freshness_rejects_overlay_tamper_with_preserved_mtime(
    tmp_path: Path,
) -> None:
    _, graph_path, index_path, _ = _build_world(tmp_path)
    overlay_path = graph_path.with_name("entity-overlays.jsonl")
    overlay_path.write_text(
        json.dumps({"overlay_id": "overlay-aa", "nodes": [], "edges": []}) + "\n",
        encoding="utf-8",
    )
    build_graph_store_from_graph_dir(graph_path.parent, index_path)
    original_stat = overlay_path.stat()
    overlay_path.write_text(
        json.dumps({"overlay_id": "overlay-bb", "nodes": [], "edges": []}) + "\n",
        encoding="utf-8",
    )
    os.utime(
        overlay_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    assert _recommendation_index_is_fresh(index_path, graph_path) is False


def test_indexed_reader_observes_committed_wal_changes(tmp_path: Path) -> None:
    _, _, index_path, _ = _build_world(tmp_path)
    writer = sqlite3.connect(index_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            """
            UPDATE nodes
            SET label = ?,
                tags_json = ?,
                attrs_json = ?,
                search_text = ?
            WHERE id = ?
            """,
            (
                "wal-visible",
                json.dumps(["wal-visible"]),
                json.dumps(
                    {
                        "label": "wal-visible",
                        "type": "skill",
                        "tags": ["wal-visible"],
                    }
                ),
                "wal-visible skill",
                "skill:go-helper",
            ),
        )
        writer.commit()
        assert Path(f"{index_path}-wal").is_file()

        indexed = recommend_by_tags_indexed(
            index_path,
            ["wal-visible"],
            top_n=5,
            query="wal-visible",
            entity_types=("skill",),
        )
    finally:
        writer.close()

    assert indexed is not None
    assert [row["name"] for row in indexed[0]] == ["wal-visible"]


def test_clean_subprocess_uses_index_without_reading_large_graph(tmp_path: Path) -> None:
    wiki, graph_path, index_path, _ = _build_world(tmp_path)
    graph_path.write_text(
        graph_path.read_text(encoding="utf-8") + (" " * 4_000_000),
        encoding="utf-8",
    )
    build_graph_store_from_graph_dir(graph_path.parent, index_path)
    home = tmp_path / "home"
    home.mkdir()
    code = f"""
import json
from pathlib import Path
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox
from ctx.adapters.generic.providers import ToolCall

toolbox = CtxCoreToolbox(
    wiki_dir=Path({str(wiki)!r}),
    graph_path=Path({str(graph_path)!r}),
    lifecycle_dir=Path({str(tmp_path / "runtime-subprocess")!r}),
)
payload = json.loads(toolbox.dispatch(ToolCall(
    id="fresh",
    name="ctx__recommend_bundle",
    arguments={{"query": "python testing", "include_unavailable": True}},
)))
print(json.dumps({{
    "graph_loaded": toolbox._graph is not None,
    "ids": [row["id"] for row in payload["results"]],
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "HOME": str(home)},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    payload = json.loads(completed.stdout)

    assert payload["graph_loaded"] is False
    assert payload["ids"][0] == "skill:python-testing"
