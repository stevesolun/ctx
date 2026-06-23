from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from ctx.core.wiki.wiki_packs import load_merged_wiki_pages
from scripts.pack_full_wiki_tar import repack_full_wiki_tar


def _add_text(tf: tarfile.TarFile, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    tf.addfile(info, io.BytesIO(payload))


def test_repack_full_wiki_tar_moves_high_fanout_pages_into_wiki_pack(
    tmp_path: Path,
) -> None:
    source = tmp_path / "wiki-graph.tar.gz"
    target = tmp_path / "wiki-graph-packed.tar.gz"
    with tarfile.open(source, "w:gz") as tf:
        _add_text(tf, "index.md", "# Wiki\n")
        _add_text(tf, "entities/skills/current.md", "# Current Skill\n")
        _add_text(tf, "entities/skills/empty.md", "")
        _add_text(tf, "entities/agents/reviewer.md", "# Reviewer Agent\n")
        _add_text(tf, "entities/mcp-servers/github.md", "# GitHub MCP\n")
        _add_text(tf, "entities/harnesses/langgraph.md", "# LangGraph Harness\n")
        _add_text(tf, "concepts/empty.md", "")
        _add_text(
            tf,
            "graphify-out/graph-export-manifest.json",
            json.dumps({"export_id": "test-export"}),
        )
        _add_text(tf, "graphify-out/graph-report.md", "# Graph Report\n")
        _add_text(tf, "graphify-out/graph.json", json.dumps({"nodes": [], "edges": []}))
        _add_text(tf, "external-catalogs/skills-sh/catalog.json", json.dumps({"skills": []}))

    stats = repack_full_wiki_tar(source, target)

    assert stats.removed_expanded_markdown_pages == 5
    assert stats.packed_pages == 8
    with tarfile.open(target, "r:gz") as tf:
        names = {member.name for member in tf.getmembers()}
        tf.extractall(tmp_path / "extracted")

    assert "entities/skills/current.md" not in names
    assert "entities/skills/empty.md" not in names
    assert "entities/agents/reviewer.md" not in names
    assert "entities/mcp-servers/github.md" not in names
    assert "entities/harnesses/langgraph.md" in names
    assert "concepts/empty.md" not in names
    assert "graphify-out/graph-report.md" in names
    assert "wiki-packs/base-test-export/wiki-pack-manifest.json" in names
    assert "wiki-packs/base-test-export/pages.jsonl" in names

    pages = load_merged_wiki_pages(tmp_path / "extracted" / "wiki-packs")
    assert pages["entities/skills/current.md"] == "# Current Skill\n"
    assert pages["entities/skills/empty.md"] == "<!-- empty markdown page -->\n"
    assert pages["entities/agents/reviewer.md"] == "# Reviewer Agent\n"
    assert pages["entities/mcp-servers/github.md"] == "# GitHub MCP\n"
    assert pages["concepts/empty.md"] == "<!-- empty markdown page -->\n"

    second_target = tmp_path / "wiki-graph-packed-again.tar.gz"
    repack_full_wiki_tar(target, second_target)
    with tarfile.open(second_target, "r:gz") as tf:
        second_names = {member.name for member in tf.getmembers()}
        tf.extractall(tmp_path / "extracted-again")
    assert "graphify-out/graph-report.md" in second_names
    repacked_pages = load_merged_wiki_pages(tmp_path / "extracted-again" / "wiki-packs")
    assert repacked_pages["entities/skills/current.md"] == "# Current Skill\n"
    assert repacked_pages["entities/agents/reviewer.md"] == "# Reviewer Agent\n"
    assert repacked_pages["entities/mcp-servers/github.md"] == "# GitHub MCP\n"
