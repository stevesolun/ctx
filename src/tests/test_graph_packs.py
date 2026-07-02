from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

from ctx.core.graph.graph_packs import (
    GraphPackManifest,
    GraphPackManifestError,
    build_pack_manifest,
    compact_graph_packs,
    discover_pack_manifests,
    load_merged_pack_graph,
    promote_graph_pack_set,
    read_pack_manifest,
    sha256_file,
    main,
    write_base_pack,
    write_overlay_pack,
    write_pack_manifest,
)


def test_base_pack_manifest_round_trips_with_file_checksums(tmp_path: Path) -> None:
    pack_dir = tmp_path / "graph" / "packs" / "base-export-1"
    pack_dir.mkdir(parents=True)
    (pack_dir / "graph.json").write_text('{"nodes":[],"edges":[]}\n', encoding="utf-8")
    (pack_dir / "communities.json").write_text('{"communities":[]}\n', encoding="utf-8")

    manifest = build_pack_manifest(
        pack_dir=pack_dir,
        pack_id="base-export-1",
        pack_type="base",
        base_export_id="export-1",
        parent_export_id=None,
        config_hash="config-sha",
        model_id="bge-small-en-v1.5",
        node_count=0,
        edge_count=0,
        artifact_paths=["graph.json", "communities.json"],
    )

    assert manifest.checksums["graph.json"] == sha256_file(pack_dir / "graph.json")

    manifest_path = pack_dir / "graph-pack-manifest.json"
    write_pack_manifest(manifest_path, manifest)

    assert read_pack_manifest(manifest_path) == manifest
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["schema_version"] == 1


def test_overlay_pack_manifest_requires_parent_export_id() -> None:
    with pytest.raises(GraphPackManifestError, match="parent_export_id"):
        GraphPackManifest.from_mapping(
            {
                "schema_version": 1,
                "pack_id": "overlay-1",
                "pack_type": "overlay",
                "base_export_id": "export-1",
                "parent_export_id": None,
                "config_hash": "config-sha",
                "model_id": "bge-small-en-v1.5",
                "node_count": 1,
                "edge_count": 2,
                "tombstone_count": 0,
                "checksums": {"entity-overlays.jsonl": "a" * 64},
            }
        )


def test_manifest_rejects_unsafe_artifact_paths(tmp_path: Path) -> None:
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    (tmp_path / "graph.json").write_text("{}", encoding="utf-8")

    with pytest.raises(GraphPackManifestError, match="unsafe"):
        build_pack_manifest(
            pack_dir=pack_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=0,
            edge_count=0,
            artifact_paths=["../graph.json"],
        )


def test_manifest_rejects_bad_checksum_shape() -> None:
    payload = {
        "schema_version": 1,
        "pack_id": "base-export-1",
        "pack_type": "base",
        "base_export_id": "export-1",
        "parent_export_id": None,
        "config_hash": "config-sha",
        "model_id": "bge-small-en-v1.5",
        "node_count": 0,
        "edge_count": 0,
        "tombstone_count": 0,
        "checksums": {"graph.json": "not-a-digest"},
    }

    with pytest.raises(GraphPackManifestError, match="SHA-256"):
        GraphPackManifest.from_mapping(payload)


def test_discover_pack_manifests_orders_base_then_overlays(tmp_path: Path) -> None:
    packs_dir = tmp_path / "graph" / "packs"
    base_dir = packs_dir / "base-export-1"
    overlay_dir = packs_dir / "overlay-review-skill"
    base_dir.mkdir(parents=True)
    overlay_dir.mkdir()
    (base_dir / "graph.json").write_text('{"nodes":[],"edges":[]}\n', encoding="utf-8")
    (overlay_dir / "nodes.jsonl").write_text('{"id":"skill:review"}\n', encoding="utf-8")
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=0,
            edge_count=0,
            artifact_paths=["graph.json"],
        ),
    )
    write_pack_manifest(
        overlay_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=overlay_dir,
            pack_id="overlay-review-skill",
            pack_type="overlay",
            base_export_id="export-1",
            parent_export_id="export-1",
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=1,
            edge_count=0,
            artifact_paths=["nodes.jsonl"],
            tombstone_count=2,
        ),
    )

    discovered = discover_pack_manifests(packs_dir)

    assert [entry.manifest.pack_id for entry in discovered] == [
        "base-export-1",
        "overlay-review-skill",
    ]
    assert discovered[1].manifest.tombstone_count == 2


def test_discover_pack_manifests_rejects_overlay_parent_mismatch(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    base_dir = packs_dir / "base-export-1"
    overlay_dir = packs_dir / "overlay-stale"
    base_dir.mkdir(parents=True)
    overlay_dir.mkdir()
    (base_dir / "graph.json").write_text("{}", encoding="utf-8")
    (overlay_dir / "nodes.jsonl").write_text("{}", encoding="utf-8")
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=0,
            edge_count=0,
            artifact_paths=["graph.json"],
        ),
    )
    write_pack_manifest(
        overlay_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=overlay_dir,
            pack_id="overlay-stale",
            pack_type="overlay",
            base_export_id="export-1",
            parent_export_id="old-export",
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=1,
            edge_count=0,
            artifact_paths=["nodes.jsonl"],
        ),
    )

    with pytest.raises(GraphPackManifestError, match="parent_export_id"):
        discover_pack_manifests(packs_dir)


def test_discover_pack_manifests_rejects_checksum_drift(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    base_dir = packs_dir / "base-export-1"
    base_dir.mkdir(parents=True)
    graph_path = base_dir / "graph.json"
    graph_path.write_text("{}", encoding="utf-8")
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=0,
            edge_count=0,
            artifact_paths=["graph.json"],
        ),
    )
    graph_path.write_text('{"changed":true}', encoding="utf-8")

    with pytest.raises(GraphPackManifestError, match="checksum mismatch"):
        discover_pack_manifests(packs_dir)


def test_load_merged_pack_graph_applies_overlay_nodes_and_edges(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    base_dir = packs_dir / "base-export-1"
    overlay_dir = packs_dir / "overlay-review"
    base_dir.mkdir(parents=True)
    overlay_dir.mkdir()
    (base_dir / "graph.json").write_text(
        json.dumps(
            {
                "graph": {"export_id": "export-1"},
                "nodes": [
                    {"id": "skill:python", "type": "skill", "tags": ["python"]},
                    {"id": "mcp-server:github", "type": "mcp-server", "tags": ["github"]},
                ],
                "edges": [
                    {"source": "skill:python", "target": "mcp-server:github", "weight": 0.4},
                ],
            }
        ),
        encoding="utf-8",
    )
    (overlay_dir / "nodes.jsonl").write_text(
        json.dumps({"id": "skill:review", "type": "skill", "tags": ["review"]}) + "\n",
        encoding="utf-8",
    )
    (overlay_dir / "edges.jsonl").write_text(
        json.dumps(
            {
                "source": "skill:review",
                "target": "mcp-server:github",
                "weight": 0.8,
                "provenance": "overlay-test",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=2,
            edge_count=1,
            artifact_paths=["graph.json"],
        ),
    )
    write_pack_manifest(
        overlay_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=overlay_dir,
            pack_id="overlay-review",
            pack_type="overlay",
            base_export_id="export-1",
            parent_export_id="export-1",
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=1,
            edge_count=1,
            artifact_paths=["nodes.jsonl", "edges.jsonl"],
        ),
    )

    graph = load_merged_pack_graph(packs_dir)

    assert graph.number_of_nodes() == 3
    assert graph.has_edge("skill:review", "mcp-server:github")
    assert graph.edges["skill:review", "mcp-server:github"]["weight"] == 0.8
    assert graph.graph["ctx_pack_ids"] == ["base-export-1", "overlay-review"]


def test_load_merged_pack_graph_applies_overlays_by_created_at(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    base_graph = nx.Graph()
    base_graph.add_node("skill:docs", title="base", type="skill")
    write_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        graph=base_graph,
    )
    write_overlay_pack(
        pack_dir=packs_dir / "overlay-z-old",
        pack_id="overlay-z-old",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        nodes=[{"id": "skill:docs", "title": "old"}],
        edges=[],
        tombstones=[],
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_overlay_pack(
        pack_dir=packs_dir / "overlay-a-new",
        pack_id="overlay-a-new",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        nodes=[{"id": "skill:docs", "title": "new"}],
        edges=[],
        tombstones=[],
        created_at="2026-01-02T00:00:00+00:00",
    )

    graph = load_merged_pack_graph(packs_dir)

    assert graph.nodes["skill:docs"]["title"] == "new"
    assert graph.graph["ctx_pack_ids"] == [
        "base-export-1",
        "overlay-z-old",
        "overlay-a-new",
    ]


def test_load_merged_pack_graph_applies_tombstones(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    base_dir = packs_dir / "base-export-1"
    overlay_dir = packs_dir / "overlay-delete"
    base_dir.mkdir(parents=True)
    overlay_dir.mkdir()
    (base_dir / "graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "skill:python"},
                    {"id": "skill:old"},
                ],
                "edges": [
                    {"source": "skill:python", "target": "skill:old", "weight": 0.4},
                ],
            }
        ),
        encoding="utf-8",
    )
    (overlay_dir / "tombstones.jsonl").write_text(
        json.dumps({"node_id": "skill:old", "reason": "deleted"}) + "\n",
        encoding="utf-8",
    )
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=2,
            edge_count=1,
            artifact_paths=["graph.json"],
        ),
    )
    write_pack_manifest(
        overlay_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=overlay_dir,
            pack_id="overlay-delete",
            pack_type="overlay",
            base_export_id="export-1",
            parent_export_id="export-1",
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=0,
            edge_count=0,
            artifact_paths=["tombstones.jsonl"],
            tombstone_count=1,
        ),
    )

    graph = load_merged_pack_graph(packs_dir)

    assert "skill:old" not in graph
    assert graph.number_of_edges() == 0


def test_load_merged_pack_graph_rejects_base_count_drift(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    base_dir = packs_dir / "base-export-1"
    base_dir.mkdir(parents=True)
    (base_dir / "graph.json").write_text(
        json.dumps(
            {
                "nodes": [{"id": "skill:python"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=2,
            edge_count=0,
            artifact_paths=["graph.json"],
        ),
    )

    with pytest.raises(GraphPackManifestError, match="node_count mismatch"):
        load_merged_pack_graph(packs_dir)


def test_load_merged_pack_graph_rejects_overlay_count_drift(tmp_path: Path) -> None:
    packs_dir = tmp_path / "packs"
    base_graph = nx.Graph()
    base_graph.add_node("skill:python")
    write_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="bge-small-en-v1.5",
        graph=base_graph,
    )
    overlay_dir = packs_dir / "overlay-review"
    overlay_dir.mkdir()
    (overlay_dir / "nodes.jsonl").write_text(
        json.dumps({"id": "skill:review"}) + "\n",
        encoding="utf-8",
    )
    write_pack_manifest(
        overlay_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=overlay_dir,
            pack_id="overlay-review",
            pack_type="overlay",
            base_export_id="export-1",
            parent_export_id="export-1",
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            node_count=2,
            edge_count=0,
            artifact_paths=["nodes.jsonl"],
        ),
    )

    with pytest.raises(GraphPackManifestError, match="node_count mismatch"):
        load_merged_pack_graph(packs_dir)


def test_write_overlay_pack_creates_jsonl_artifacts_and_manifest(tmp_path: Path) -> None:
    pack_dir = tmp_path / "packs" / "overlay-review"

    manifest = write_overlay_pack(
        pack_dir=pack_dir,
        pack_id="overlay-review",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-sha",
        model_id="bge-small-en-v1.5",
        nodes=[{"id": "skill:review", "type": "skill"}],
        edges=[{"source": "skill:review", "target": "skill:python", "weight": 0.7}],
        tombstones=[{"node_id": "skill:old"}],
    )

    assert manifest.pack_type == "overlay"
    assert manifest.node_count == 1
    assert manifest.edge_count == 1
    assert manifest.tombstone_count == 1
    assert (pack_dir / "nodes.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert (pack_dir / "edges.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert (pack_dir / "tombstones.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert read_pack_manifest(pack_dir / "graph-pack-manifest.json") == manifest


def test_write_overlay_pack_rejects_empty_pack(tmp_path: Path) -> None:
    with pytest.raises(GraphPackManifestError, match="empty overlay pack"):
        write_overlay_pack(
            pack_dir=tmp_path / "overlay-empty",
            pack_id="overlay-empty",
            base_export_id="export-1",
            parent_export_id="export-1",
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            nodes=[],
            edges=[],
            tombstones=[],
        )


def test_write_overlay_pack_rejects_existing_manifest(tmp_path: Path) -> None:
    pack_dir = tmp_path / "packs" / "overlay-review"
    write_overlay_pack(
        pack_dir=pack_dir,
        pack_id="overlay-review",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-sha",
        model_id="bge-small-en-v1.5",
        nodes=[{"id": "skill:review"}],
        edges=[],
        tombstones=[],
    )

    with pytest.raises(GraphPackManifestError, match="already exists"):
        write_overlay_pack(
            pack_dir=pack_dir,
            pack_id="overlay-review",
            base_export_id="export-1",
            parent_export_id="export-1",
            config_hash="config-sha",
            model_id="bge-small-en-v1.5",
            nodes=[{"id": "skill:changed"}],
            edges=[],
            tombstones=[],
        )


def test_write_base_pack_creates_graph_json_and_manifest(tmp_path: Path) -> None:
    graph = nx.Graph()
    graph.add_node("skill:python", type="skill")
    graph.add_node("agent:review", type="agent")
    graph.add_edge("skill:python", "agent:review", weight=0.6)

    manifest = write_base_pack(
        pack_dir=tmp_path / "base-export-2",
        pack_id="base-export-2",
        base_export_id="export-2",
        config_hash="config-sha",
        model_id="model-a",
        graph=graph,
    )

    payload = json.loads((tmp_path / "base-export-2" / "graph.json").read_text(encoding="utf-8"))
    assert payload["graph"]["export_id"] == "export-2"
    assert "edges" in payload
    assert manifest.pack_type == "base"
    assert manifest.node_count == 2
    assert manifest.edge_count == 1
    assert read_pack_manifest(tmp_path / "base-export-2" / "graph-pack-manifest.json") == manifest


def test_pack_writers_reject_unsafe_pack_id_before_payload_write(tmp_path: Path) -> None:
    graph = nx.Graph()
    graph.add_node("skill:python", type="skill")

    with pytest.raises(GraphPackManifestError, match="pack_id is unsafe"):
        write_base_pack(
            pack_dir=tmp_path / "base-bad",
            pack_id="../base-bad",
            base_export_id="export-2",
            config_hash="config-sha",
            model_id="model-a",
            graph=graph,
        )
    assert not (tmp_path / "base-bad" / "graph.json").exists()

    with pytest.raises(GraphPackManifestError, match="pack_id is unsafe"):
        write_overlay_pack(
            pack_dir=tmp_path / "overlay-bad",
            pack_id="../overlay-bad",
            base_export_id="export-1",
            parent_export_id="export-1",
            config_hash="config-sha",
            model_id="model-a",
            nodes=[{"id": "skill:review"}],
            edges=[],
            tombstones=[],
        )
    assert not (tmp_path / "overlay-bad" / "nodes.jsonl").exists()


def test_compact_graph_packs_writes_staged_base_without_mutating_active_packs(
    tmp_path: Path,
) -> None:
    active_packs = tmp_path / "active" / "packs"
    base_dir = active_packs / "base-export-1"
    overlay_dir = active_packs / "overlay-review"
    base_dir.mkdir(parents=True)
    overlay_dir.mkdir()
    (base_dir / "graph.json").write_text(
        json.dumps(
            {
                "graph": {"export_id": "export-1"},
                "nodes": [
                    {"id": "skill:python", "type": "skill"},
                    {"id": "agent:review", "type": "agent"},
                ],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="model-a",
            node_count=2,
            edge_count=0,
            artifact_paths=["graph.json"],
        ),
    )
    write_overlay_pack(
        pack_dir=overlay_dir,
        pack_id="overlay-review",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        nodes=[{"id": "skill:review", "type": "skill"}],
        edges=[{"source": "skill:review", "target": "agent:review", "weight": 0.8}],
        tombstones=[{"node_id": "skill:python"}],
    )

    staged_pack = tmp_path / "staged" / "base-export-2"
    manifest = compact_graph_packs(
        packs_dir=active_packs,
        compacted_pack_dir=staged_pack,
        base_export_id="export-2",
    )

    assert manifest.pack_type == "base"
    assert manifest.base_export_id == "export-2"
    assert [entry.manifest.pack_id for entry in discover_pack_manifests(active_packs)] == [
        "base-export-1",
        "overlay-review",
    ]
    staged_packs_root = tmp_path / "staged"
    compacted = load_merged_pack_graph(staged_packs_root)
    assert "skill:python" not in compacted
    assert compacted.has_edge("skill:review", "agent:review")
    assert compacted.graph["export_id"] == "export-2"
    assert compacted.graph["ctx_compacted_from_base_export_id"] == "export-1"
    assert compacted.graph["ctx_compacted_overlay_count"] == 1


def test_main_compact_writes_json_report_and_staged_pack(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    active_packs = tmp_path / "active" / "packs"
    base_dir = active_packs / "base-export-1"
    overlay_dir = active_packs / "overlay-review"
    base_dir.mkdir(parents=True)
    (base_dir / "graph.json").write_text(
        json.dumps(
            {
                "graph": {"export_id": "export-1"},
                "nodes": [{"id": "skill:python"}, {"id": "skill:review"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    write_pack_manifest(
        base_dir / "graph-pack-manifest.json",
        build_pack_manifest(
            pack_dir=base_dir,
            pack_id="base-export-1",
            pack_type="base",
            base_export_id="export-1",
            parent_export_id=None,
            config_hash="config-sha",
            model_id="model-a",
            node_count=2,
            edge_count=0,
            artifact_paths=["graph.json"],
        ),
    )
    write_overlay_pack(
        pack_dir=overlay_dir,
        pack_id="overlay-review",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        nodes=[],
        edges=[{"source": "skill:review", "target": "skill:python", "weight": 0.8}],
        tombstones=[],
    )
    staged_pack = tmp_path / "staged" / "base-export-2"

    rc = main(
        [
            "compact",
            "--packs-dir",
            str(active_packs),
            "--staged-pack-dir",
            str(staged_pack),
            "--base-export-id",
            "export-2",
            "--json",
        ]
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["pack_id"] == "base-export-2"
    assert output["base_export_id"] == "export-2"
    graph = load_merged_pack_graph(tmp_path / "staged")
    assert graph.has_edge("skill:review", "skill:python")


def test_promote_graph_pack_set_replaces_active_and_writes_rollback_metadata(
    tmp_path: Path,
) -> None:
    active_packs = tmp_path / "graph" / "packs"
    old_graph = nx.Graph()
    old_graph.add_node("skill:old")
    write_base_pack(
        pack_dir=active_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        graph=old_graph,
    )
    write_overlay_pack(
        pack_dir=active_packs / "overlay-review",
        pack_id="overlay-review",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        nodes=[{"id": "skill:review"}],
        edges=[{"source": "skill:old", "target": "skill:review", "weight": 0.8}],
        tombstones=[],
    )

    staged_packs = tmp_path / "staged-packs"
    new_graph = nx.Graph()
    new_graph.add_node("skill:new")
    write_base_pack(
        pack_dir=staged_packs / "base-export-2",
        pack_id="base-export-2",
        base_export_id="export-2",
        config_hash="config-sha",
        model_id="model-a",
        graph=new_graph,
    )
    backup_packs = tmp_path / "graph" / "packs.rollback"

    result = promote_graph_pack_set(
        staged_packs_dir=staged_packs,
        active_packs_dir=active_packs,
        backup_packs_dir=backup_packs,
    )

    assert result.promoted_pack_ids == ["base-export-2"]
    assert result.replaced_pack_ids == ["base-export-1", "overlay-review"]
    assert not staged_packs.exists()
    assert [entry.manifest.pack_id for entry in discover_pack_manifests(active_packs)] == [
        "base-export-2",
    ]
    assert [entry.manifest.pack_id for entry in discover_pack_manifests(backup_packs)] == [
        "base-export-1",
        "overlay-review",
    ]
    promoted_graph = load_merged_pack_graph(active_packs)
    assert list(promoted_graph.nodes) == ["skill:new"]
    rollback_metadata = json.loads(result.rollback_metadata_path.read_text(encoding="utf-8"))
    assert rollback_metadata["backup_packs_dir"] == str(backup_packs)
    assert rollback_metadata["promoted_pack_ids"] == ["base-export-2"]
    assert rollback_metadata["replaced_pack_ids"] == ["base-export-1", "overlay-review"]


def test_main_promote_writes_json_report_and_uses_default_backup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    active_packs = tmp_path / "graph" / "packs"
    old_graph = nx.Graph()
    old_graph.add_node("skill:old")
    write_base_pack(
        pack_dir=active_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-sha",
        model_id="model-a",
        graph=old_graph,
    )
    staged_packs = tmp_path / "staged-packs"
    new_graph = nx.Graph()
    new_graph.add_node("skill:new")
    write_base_pack(
        pack_dir=staged_packs / "base-export-2",
        pack_id="base-export-2",
        base_export_id="export-2",
        config_hash="config-sha",
        model_id="model-a",
        graph=new_graph,
    )

    rc = main(
        [
            "promote",
            "--staged-packs-dir",
            str(staged_packs),
            "--active-packs-dir",
            str(active_packs),
            "--json",
        ]
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["promoted_pack_ids"] == ["base-export-2"]
    assert output["replaced_pack_ids"] == ["base-export-1"]
    assert output["backup_packs_dir"] == str(tmp_path / "graph" / "packs.rollback")
    assert [entry.manifest.pack_id for entry in discover_pack_manifests(active_packs)] == [
        "base-export-2",
    ]
