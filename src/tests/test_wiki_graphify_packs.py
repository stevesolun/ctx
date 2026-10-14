"""Rebuilding an imported wiki must retain its authoritative packed pages."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tarfile

import networkx as nx
import pytest

import ctx_config
import ctx_init
from ctx.core.graph import semantic_edges
from ctx.core.wiki import wiki_graphify as wg
from ctx.core.wiki.wiki_packs import (
    WikiPackManifestError,
    load_merged_wiki_pages,
    write_wiki_base_pack,
    write_wiki_overlay_pack,
)
from ctx.core.wiki.wiki_query import load_all_pages


def _page(title: str, kind: str) -> str:
    return f"---\ntitle: {title}\ntype: {kind}\ntags: [testing]\n---\n{title}\n"


@pytest.fixture
def packed_import(tmp_path, monkeypatch):
    original_wiki = wg.WIKI_DIR
    source = tmp_path / "source"
    source.mkdir()
    wg.configure_wiki_dir(source)
    monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "quality")
    monkeypatch.setattr(ctx_config.cfg, "graph_edge_weight_semantic", 0.0)
    graph = nx.Graph()
    for kind, slug in (("skill", "review"), ("agent", "tester"), ("mcp-server", "tools")):
        graph.add_node(f"{kind}:{slug}", type=kind, label=slug, tags=["testing"])
    export_id = wg.export_graph(graph, {})
    (source / "index.md").write_text("# My knowledge\n")
    with sqlite3.connect(source / "graphify-out" / "dashboard-neighborhoods.sqlite3") as db:
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        db.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [("export_id", json.dumps(export_id)), ("nodes_count", "0")],
        )
        db.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE slug_index(slug TEXT)")
        db.execute("CREATE TABLE neighbors(payload BLOB)")
    pages = {
        "index.md": "# My knowledge\n",
        "entities/skills/review.md": _page("Old review", "skill"),
        "entities/agents/tester.md": _page("User tester", "agent"),
        "entities/mcp-servers/t/tools.md": _page("User tools", "mcp-server"),
        "entities/skills/retired.md": _page("Retired", "skill"),
    }
    write_wiki_base_pack(
        pack_dir=source / "wiki-packs" / "base-user",
        pack_id="base-user",
        base_export_id=export_id,
        pages=pages,
    )
    pages["entities/skills/review.md"] = _page("Updated user review", "skill")
    pages.pop("entities/skills/retired.md")
    write_wiki_overlay_pack(
        pack_dir=source / "wiki-packs" / "overlay-user",
        pack_id="overlay-user",
        base_export_id=export_id,
        parent_export_id=export_id,
        pages={"entities/skills/review.md": pages["entities/skills/review.md"]},
        tombstones=["entities/skills/retired.md"],
    )
    archive = tmp_path / "user-knowledge.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                output.add(path, arcname=path.relative_to(source).as_posix())
    root = tmp_path / "destination"
    assert (
        ctx_init.build_graph(
            root,
            graph_url=archive.as_uri(),
            graph_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
            install_mode="full",
        )
        == 0
    )
    wiki = root / "skill-wiki"
    assert not list((wiki / "entities").rglob("*.md"))
    try:
        yield wiki, pages
    finally:
        wg.configure_wiki_dir(original_wiki)


@pytest.mark.parametrize("graph_only", [True, False])
def test_imported_packed_records_survive_repeated_rebuilds(packed_import, monkeypatch, graph_only):
    wiki, expected_pages = packed_import
    # Loose copies must neither replace a packed overlay nor resurrect a tombstone.
    for slug in ("review", "retired"):
        (wiki / "entities" / "skills" / f"{slug}.md").write_text(_page("Stale", "skill"))
    args = ["ctx-wiki-graphify", "--wiki-dir", str(wiki), "--full"]
    if graph_only:
        args.append("--graph-only")
    monkeypatch.setattr(sys, "argv", args)
    for _ in range(2):
        wg.main()
        graph = wg.load_prior_graph()
        assert graph is not None
        assert set(graph) == {"skill:review", "agent:tester", "mcp-server:tools"}
        pages = load_merged_wiki_pages(wiki / "wiki-packs")
        assert "entities/skills/retired.md" not in pages
        for path, text in expected_pages.items():
            assert text in pages[path]
        assert {(page.entity_type, page.name) for page in load_all_pages(wiki)} == {
            ("skill", "review"),
            ("agent", "tester"),
            ("mcp-server", "tools"),
        }


@pytest.mark.parametrize("corruption", ["checksum", "missing-base"])
def test_corrupt_active_pack_aborts_before_replacing_graph_or_pages(
    packed_import, monkeypatch, corruption
):
    wiki, _ = packed_import
    packed_file = wiki / "wiki-packs" / "base-user" / "pages.jsonl"
    if corruption == "checksum":
        packed_file.write_text(packed_file.read_text() + "corrupt\n")
    else:
        for manifest in (wiki / "wiki-packs").rglob("wiki-pack-manifest.json"):
            manifest.unlink()
    before = {p: p.read_bytes() for p in wiki.rglob("*") if p.is_file()}
    monkeypatch.setattr(sys, "argv", ["ctx-wiki-graphify", "--wiki-dir", str(wiki), "--graph-only"])
    with pytest.raises(WikiPackManifestError, match="checksum|base pack"):
        wg.main()
    assert {p: p.read_bytes() for p in wiki.rglob("*") if p.is_file()} == before


@pytest.fixture
def semantic_wiki(tmp_path, monkeypatch):
    original_wiki = wg.WIKI_DIR
    wiki = tmp_path / "wiki"
    wg.configure_wiki_dir(wiki)
    monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "quality")
    monkeypatch.setattr(ctx_config.cfg, "graph_edge_weight_semantic", 1.0)
    captured: list[dict[str, str]] = []

    def capture(nodes, **_kwargs):
        captured.append({node.node_id: node.text for node in nodes})
        return {}

    monkeypatch.setattr(semantic_edges, "compute_semantic_edges", capture)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ctx-wiki-graphify", "--wiki-dir", str(wiki), "--graph-only", "--full"],
    )
    try:
        yield wiki, captured
    finally:
        wg.configure_wiki_dir(original_wiki)


def test_rich_embedding_inputs_survive_loose_to_packed_rebuilds(semantic_wiki):
    wiki, captured = semantic_wiki
    pages = {
        "entities/skills/review.md": _page("Review card", "skill"),
        "entities/agents/tester.md": _page("Tester card", "agent"),
        "converted/review/SKILL.md": "Full skill instructions\n",
        "converted/review/references/02-check.md": "Second reference\n",
        "converted/review/references/01-plan.md": "First reference\n",
        "converted/review/references/nested/unused.md": "Nested reference excluded\n",
        "converted-agents/tester.md": "Complete agent prompt\n",
    }
    for relpath, text in pages.items():
        path = wiki / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    wg.main()
    expected = captured[0]
    skill_text = expected["skill:review"]
    assert "Full skill instructions" in skill_text
    assert skill_text.index("First reference") < skill_text.index("Second reference")
    assert "Nested reference excluded" not in expected["skill:review"]
    assert "Complete agent prompt" in expected["agent:tester"]
    for relpath in pages:
        (wiki / relpath).write_text("Stale loose content\n")

    for _ in range(2):
        wg.main()
        assert captured[-1] == expected
        assert load_merged_wiki_pages(wiki / "wiki-packs") == pages


def test_packed_rich_sources_honor_overlays_and_tombstones(semantic_wiki):
    wiki, captured = semantic_wiki
    pages = {
        "entities/skills/review.md": _page("Review card", "skill"),
        "entities/skills/card-only.md": _page("Skill fallback card", "skill"),
        "entities/agents/tester.md": _page("Tester card", "agent"),
        "entities/agents/card-only.md": _page("Agent fallback card", "agent"),
        "converted/review/SKILL.md": "Old skill instructions\n",
        "converted/review/references/keep.md": "Old reference\n",
        "converted/review/references/retired.md": "Retired reference\n",
        "converted/card-only/SKILL.md": "Retired skill instructions\n",
        "converted/card-only/references/orphan.md": "Orphaned reference\n",
        "converted-agents/tester.md": "Old agent prompt\n",
        "converted-agents/card-only.md": "Retired agent prompt\n",
    }
    write_wiki_base_pack(
        pack_dir=wiki / "wiki-packs" / "base-user",
        pack_id="base-user",
        base_export_id="user-export",
        pages=pages,
    )
    updates = {
        "converted/review/SKILL.md": "Updated skill instructions\n",
        "converted/review/references/keep.md": "Updated reference\n",
        "converted-agents/tester.md": "Updated agent prompt\n",
    }
    tombstones = [
        "converted/review/references/retired.md",
        "converted/card-only/SKILL.md",
        "converted-agents/card-only.md",
    ]
    write_wiki_overlay_pack(
        pack_dir=wiki / "wiki-packs" / "overlay-user",
        pack_id="overlay-user",
        base_export_id="user-export",
        parent_export_id="user-export",
        pages=updates,
        tombstones=tombstones,
    )
    for relpath, text in pages.items():
        path = wiki / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (wiki / "converted" / "review" / "SKILL.md.original").write_text("Loose original backup\n")
    expected_pages = pages | updates
    for relpath in tombstones:
        expected_pages.pop(relpath)

    for _ in range(2):
        wg.main()
        inputs = captured[-1]
        assert "Updated skill instructions" in inputs["skill:review"]
        assert "Updated reference" in inputs["skill:review"]
        assert "Updated agent prompt" in inputs["agent:tester"]
        assert "Skill fallback card" in inputs["skill:card-only"]
        assert "Agent fallback card" in inputs["agent:card-only"]
        assert not any(
            value in text
            for text in inputs.values()
            for value in ("Old ", "Retired ", "Orphaned reference", "Loose original backup")
        )
        assert load_merged_wiki_pages(wiki / "wiki-packs") == expected_pages
    assert captured[0] == captured[1]
