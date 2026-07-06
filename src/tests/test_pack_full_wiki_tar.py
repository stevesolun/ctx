from __future__ import annotations

import io
import json
import tarfile
from hashlib import sha256
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
        _add_text(tf, "catalog.md", "| skill | `C:\\Users\\steves\\ctx\\SKILL.md` |\n")
        _add_text(tf, "converted-index.md", "`/Users/steves/ctx/converted`\n")
        _add_text(tf, "log.md", "local export: /Users/steves/ctx C:\\Users\\steves\\ctx\n")
        _add_text(tf, "versions-catalog.md", "`C:\\Users\\steves\\ctx\\versions`\n")
        _add_text(
            tf,
            "entities/skills/current.md",
            "# Current Skill\nSource: /Users/steves/ctx\nLinux: /home/steves/ctx\n",
        )
        _add_text(tf, "entities/skills/empty.md", "")
        _add_text(tf, "entities/agents/reviewer.md", "# Reviewer Agent\n")
        _add_text(tf, "entities/mcp-servers/github.md", "# GitHub MCP\n")
        _add_text(
            tf,
            "entities/harnesses/langgraph.md",
            "# LangGraph Harness\nRun C:\\Users\\steves\\ctx\\harness.py\n",
        )
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

    assert stats.removed_expanded_markdown_pages == 9
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
    assert "catalog.md" not in names
    assert "converted-index.md" not in names
    assert "log.md" not in names
    assert "versions-catalog.md" not in names
    assert "graphify-out/graph-report.md" in names
    assert "wiki-packs/base-test-export/wiki-pack-manifest.json" in names
    assert "wiki-packs/base-test-export/pages.jsonl" in names
    harness_text = (tmp_path / "extracted" / "entities" / "harnesses" / "langgraph.md").read_text(
        encoding="utf-8"
    )
    assert "C:\\Users\\steves" not in harness_text
    assert "<host-user-path>" in harness_text

    pages = load_merged_wiki_pages(tmp_path / "extracted" / "wiki-packs")
    assert pages["entities/skills/current.md"] == (
        "# Current Skill\nSource: <host-user-path>\nLinux: <host-user-path>\n"
    )
    assert pages["entities/skills/empty.md"] == "<!-- empty markdown page -->\n"
    assert pages["entities/agents/reviewer.md"] == "# Reviewer Agent\n"
    assert pages["entities/mcp-servers/github.md"] == "# GitHub MCP\n"
    assert pages["concepts/empty.md"] == "<!-- empty markdown page -->\n"
    assert "catalog.md" not in pages
    assert "converted-index.md" not in pages
    assert "log.md" not in pages
    assert "versions-catalog.md" not in pages

    second_target = tmp_path / "wiki-graph-packed-again.tar.gz"
    repack_full_wiki_tar(target, second_target)
    with tarfile.open(second_target, "r:gz") as tf:
        second_names = {member.name for member in tf.getmembers()}
        tf.extractall(tmp_path / "extracted-again")
    assert "graphify-out/graph-report.md" in second_names
    repacked_pages = load_merged_wiki_pages(tmp_path / "extracted-again" / "wiki-packs")
    assert (
        repacked_pages["entities/skills/current.md"]
        == "# Current Skill\nSource: <host-user-path>\nLinux: <host-user-path>\n"
    )
    assert repacked_pages["entities/agents/reviewer.md"] == "# Reviewer Agent\n"
    assert repacked_pages["entities/mcp-servers/github.md"] == "# GitHub MCP\n"
    assert "catalog.md" not in repacked_pages


def test_repack_full_wiki_tar_redacts_file_uri_host_paths(tmp_path: Path) -> None:
    source = tmp_path / "wiki-graph.tar.gz"
    target = tmp_path / "wiki-graph-packed.tar.gz"
    with tarfile.open(source, "w:gz") as tf:
        _add_text(
            tf,
            "entities/skills/current.md",
            "macOS: file:///Users/steves/ctx\nLinux: file:///home/steves/ctx\n",
        )
        _add_text(
            tf,
            "graphify-out/graph-export-manifest.json",
            json.dumps({"export_id": "test-export"}),
        )

    repack_full_wiki_tar(source, target)

    with tarfile.open(target, "r:gz") as tf:
        tf.extractall(tmp_path / "extracted")
    pages = load_merged_wiki_pages(tmp_path / "extracted" / "wiki-packs")
    assert pages["entities/skills/current.md"] == (
        "macOS: <host-user-path>\nLinux: <host-user-path>\n"
    )


def test_repack_full_wiki_tar_preserves_graph_pack_checksums(tmp_path: Path) -> None:
    source = tmp_path / "wiki-graph.tar.gz"
    target = tmp_path / "wiki-graph-packed.tar.gz"
    graph_pack_payload = (
        '{"graph":{"export_id":"test-export"},"nodes":[{"id":"/Users/steves/private"}],'
        '"edges":[]}\n'
    )
    graph_pack_manifest = {
        "schema_version": 1,
        "pack_id": "base-test-export",
        "pack_type": "base",
        "base_export_id": "test-export",
        "parent_export_id": None,
        "config_hash": "config-sha",
        "model_id": "test-model",
        "node_count": 1,
        "edge_count": 0,
        "tombstone_count": 0,
        "checksums": {"graph.json": sha256(graph_pack_payload.encode("utf-8")).hexdigest()},
    }
    with tarfile.open(source, "w:gz") as tf:
        _add_text(
            tf,
            "graphify-out/graph-export-manifest.json",
            json.dumps({"export_id": "test-export"}),
        )
        _add_text(
            tf,
            "graphify-out/graph.json",
            '{"nodes":[{"id":"/Users/steves/private"}],"edges":[]}\n',
        )
        _add_text(
            tf,
            "graphify-out/packs/base-test-export/graph-pack-manifest.json",
            json.dumps(graph_pack_manifest),
        )
        _add_text(tf, "graphify-out/packs/base-test-export/graph.json", graph_pack_payload)

    repack_full_wiki_tar(source, target)

    with tarfile.open(target, "r:gz") as tf:
        top_graph = tf.extractfile("graphify-out/graph.json")
        pack_graph = tf.extractfile("graphify-out/packs/base-test-export/graph.json")
        pack_manifest = tf.extractfile(
            "graphify-out/packs/base-test-export/graph-pack-manifest.json"
        )
        assert top_graph is not None
        assert pack_graph is not None
        assert pack_manifest is not None
        top_graph_text = top_graph.read().decode("utf-8")
        pack_graph_text = pack_graph.read().decode("utf-8")
        manifest = json.loads(pack_manifest.read().decode("utf-8"))

    assert "/Users/steves/private" not in top_graph_text
    assert "/Users/steves/private" in pack_graph_text
    assert (
        manifest["checksums"]["graph.json"] == sha256(pack_graph_text.encode("utf-8")).hexdigest()
    )
