from __future__ import annotations

import hashlib
import json
from pathlib import Path

import networkx as nx
import pytest

from ctx.core.graph.graph_packs import (
    discover_pack_manifests,
    load_merged_pack_graph,
    write_base_pack,
    write_overlay_pack,
)
from ctx.core.graph.graph_store import graph_store_is_fresh, search_nodes
from ctx.core.wiki import pack_compaction
from ctx.core.wiki.pack_compaction import (
    PackCompactionError,
    compact_active_pack_sets,
    pack_compaction_status,
    promote_staged_pack_sets,
)
from ctx.core.wiki.wiki_packs import (
    discover_wiki_pack_manifests,
    load_merged_wiki_pages,
    write_wiki_base_pack,
    write_wiki_overlay_pack,
)


def test_compact_active_pack_sets_stages_graph_and_wiki_without_mutating_active(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    graph_packs, wiki_packs = _write_active_pack_sets(wiki)
    staging_dir = tmp_path / "staged-compaction"

    result = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=staging_dir,
    )

    assert result.graph_manifest.pack_id == "base-export-2"
    assert result.wiki_manifest.pack_id == "base-export-2"
    assert [entry.manifest.pack_id for entry in discover_pack_manifests(graph_packs)] == [
        "base-export-1",
        "overlay-new",
    ]
    assert [entry.manifest.pack_id for entry in discover_wiki_pack_manifests(wiki_packs)] == [
        "base-export-1",
        "overlay-new",
    ]
    compacted_graph = load_merged_pack_graph(staging_dir / "graph-packs")
    assert "skill:old" not in compacted_graph
    assert compacted_graph.has_edge("skill:new", "skill:keep")
    assert load_merged_wiki_pages(staging_dir / "wiki-packs") == {
        "entities/skills/keep.md": "# Keep\n",
        "entities/skills/new.md": "# New\n",
    }
    manifest = json.loads((staging_dir / "pack-compaction-manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["operation"] == "pack-compaction-stage"
    assert manifest["base_export_id"] == "export-2"
    assert manifest["staged_graph_packs_dir"] == str(staging_dir / "graph-packs")
    assert manifest["staged_wiki_packs_dir"] == str(staging_dir / "wiki-packs")
    assert manifest["graph"]["pack_id"] == "base-export-2"
    assert manifest["wiki"]["pack_id"] == "base-export-2"


def test_compact_active_pack_sets_rejects_graph_wiki_mismatch_before_return(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    graph_packs, wiki_packs = _write_active_pack_sets(wiki)
    staging_dir = tmp_path / "staged-compaction"
    write_overlay_pack(
        pack_dir=graph_packs / "overlay-missing-wiki",
        pack_id="overlay-missing-wiki",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-1",
        model_id="model-1",
        nodes=[{"id": "skill:missing-wiki", "type": "skill", "label": "missing wiki"}],
        edges=[{"source": "skill:missing-wiki", "target": "skill:keep", "weight": 0.8}],
        tombstones=[],
    )

    with pytest.raises(PackCompactionError, match="missing wiki pages"):
        compact_active_pack_sets(
            wiki_path=wiki,
            base_export_id="export-2",
            staging_dir=staging_dir,
        )

    assert not staging_dir.exists()
    assert [entry.manifest.pack_id for entry in discover_pack_manifests(graph_packs)] == [
        "base-export-1",
        "overlay-new",
        "overlay-missing-wiki",
    ]
    assert [entry.manifest.pack_id for entry in discover_wiki_pack_manifests(wiki_packs)] == [
        "base-export-1",
        "overlay-new",
    ]


def test_pack_compaction_cli_emits_json_for_staged_pack_sets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staging_dir = tmp_path / "staged-compaction"

    result = pack_compaction.main(
        [
            "compact",
            "--wiki-path",
            str(wiki),
            "--base-export-id",
            "export-2",
            "--staging-dir",
            str(staging_dir),
            "--json",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["graph"]["pack_id"] == "base-export-2"
    assert payload["wiki"]["pack_id"] == "base-export-2"
    assert payload["staged_graph_packs_dir"] == str(staging_dir / "graph-packs")
    assert payload["staged_wiki_packs_dir"] == str(staging_dir / "wiki-packs")
    assert "skill:old" not in load_merged_pack_graph(staging_dir / "graph-packs")
    assert "entities/skills/old.md" not in load_merged_wiki_pages(staging_dir / "wiki-packs")


def test_pack_compaction_status_reports_overlay_threshold(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)

    default_status = pack_compaction_status(wiki_path=wiki)
    assert default_status["overlay_threshold"] == 25
    assert default_status["needs_compaction"] is False

    status = pack_compaction_status(
        wiki_path=wiki,
        overlay_threshold=1,
    )

    assert status["base_export_id"] == "export-1"
    assert status["graph_overlay_count"] == 1
    assert status["wiki_overlay_count"] == 1
    assert status["max_overlay_count"] == 1
    assert status["overlay_threshold"] == 1
    assert status["needs_compaction"] is True
    assert status["can_compact_now"] is True
    assert status["graph_pack_ids"] == ["base-export-1", "overlay-new"]
    assert status["wiki_pack_ids"] == ["base-export-1", "overlay-new"]
    validation = status["validation"]
    assert isinstance(validation, dict)
    assert validation["graph_nodes"] == 2
    assert validation["wiki_pages"] == 2


def test_pack_compaction_cli_status_emits_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)

    rc = pack_compaction.main(
        [
            "status",
            "--wiki-path",
            str(wiki),
            "--overlay-threshold",
            "2",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["base_export_id"] == "export-1"
    assert payload["max_overlay_count"] == 1
    assert payload["overlay_threshold"] == 2
    assert payload["needs_compaction"] is False
    assert payload["can_compact_now"] is True


def test_promote_staged_pack_sets_replaces_graph_and_wiki_with_backups(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )

    result = promote_staged_pack_sets(
        wiki_path=wiki,
        staged_graph_packs_dir=staged.staged_graph_packs_dir,
        staged_wiki_packs_dir=staged.staged_wiki_packs_dir,
        graph_backup_packs_dir=tmp_path / "graph-packs.backup",
        wiki_backup_packs_dir=tmp_path / "wiki-packs.backup",
    )

    active_graph = load_merged_pack_graph(wiki / "graphify-out" / "packs")
    assert "skill:old" not in active_graph
    assert active_graph.has_edge("skill:new", "skill:keep")
    assert load_merged_wiki_pages(wiki / "wiki-packs") == {
        "entities/skills/keep.md": "# Keep\n",
        "entities/skills/new.md": "# New\n",
    }
    backup_graph = load_merged_pack_graph(tmp_path / "graph-packs.backup")
    assert "skill:old" not in backup_graph
    assert backup_graph.has_edge("skill:new", "skill:keep")
    assert [
        entry.manifest.pack_id for entry in discover_pack_manifests(tmp_path / "graph-packs.backup")
    ] == [
        "base-export-1",
        "overlay-new",
    ]
    assert load_merged_wiki_pages(tmp_path / "wiki-packs.backup") == {
        "entities/skills/keep.md": "# Keep\n",
        "entities/skills/new.md": "# New\n",
    }
    assert [
        entry.manifest.pack_id
        for entry in discover_wiki_pack_manifests(tmp_path / "wiki-packs.backup")
    ] == [
        "base-export-1",
        "overlay-new",
    ]
    assert result.graph.promoted_pack_ids == ["base-export-2"]
    assert result.wiki.promoted_pack_ids == ["base-export-2"]
    store_path = wiki / "graphify-out" / "graph-store.sqlite3"
    assert result.graph_store == {"rebuilt": True, "nodes": 2, "edges": 1}
    assert graph_store_is_fresh(store_path, wiki / "graphify-out") is True
    assert [row["id"] for row in search_nodes(store_path, "new")] == ["skill:new"]


def test_pack_compaction_cli_promotes_staged_pack_sets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )

    rc = pack_compaction.main(
        [
            "promote",
            "--wiki-path",
            str(wiki),
            "--staged-graph-packs-dir",
            str(staged.staged_graph_packs_dir),
            "--staged-wiki-packs-dir",
            str(staged.staged_wiki_packs_dir),
            "--graph-backup-packs-dir",
            str(tmp_path / "graph-packs.backup"),
            "--wiki-backup-packs-dir",
            str(tmp_path / "wiki-packs.backup"),
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["graph"]["promoted_pack_ids"] == ["base-export-2"]
    assert payload["wiki"]["promoted_pack_ids"] == ["base-export-2"]
    assert "skill:old" not in load_merged_pack_graph(wiki / "graphify-out" / "packs")
    assert "entities/skills/old.md" not in load_merged_wiki_pages(wiki / "wiki-packs")


def test_pack_compaction_cli_compact_promote_one_shot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staging_dir = tmp_path / "staged-one-shot"
    store_path = tmp_path / "graph-store.sqlite3"

    rc = pack_compaction.main(
        [
            "compact-promote",
            "--wiki-path",
            str(wiki),
            "--base-export-id",
            "export-2",
            "--staging-dir",
            str(staging_dir),
            "--graph-backup-packs-dir",
            str(tmp_path / "graph-packs.backup"),
            "--wiki-backup-packs-dir",
            str(tmp_path / "wiki-packs.backup"),
            "--graph-store-db",
            str(store_path),
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["compaction"]["base_export_id"] == "export-2"
    assert payload["promotion"]["graph"]["promoted_pack_ids"] == ["base-export-2"]
    assert payload["promotion"]["wiki"]["promoted_pack_ids"] == ["base-export-2"]
    assert payload["promotion"]["graph_store"] == {"rebuilt": True, "nodes": 2, "edges": 1}
    assert "skill:old" not in load_merged_pack_graph(wiki / "graphify-out" / "packs")
    assert "entities/skills/old.md" not in load_merged_wiki_pages(wiki / "wiki-packs")
    assert graph_store_is_fresh(store_path, wiki / "graphify-out") is True


def test_pack_compaction_cli_validates_active_pack_sets(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)

    rc = pack_compaction.main(
        [
            "validate",
            "--wiki-path",
            str(wiki),
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["graph_nodes"] == 2
    assert payload["graph_edges"] == 1
    assert payload["wiki_pages"] == 2
    assert payload["graph_pack_ids"] == ["base-export-1", "overlay-new"]
    assert payload["base_export_id"] == "export-1"
    assert payload["missing_wiki_pages"] == 0
    assert payload["orphan_wiki_pages"] == 0
    assert payload["stale_wiki_links"] == 0


def test_pack_compaction_cli_validates_staged_pack_sets_with_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )

    rc = pack_compaction.main(
        [
            "validate",
            "--staged-graph-packs-dir",
            str(staged.staged_graph_packs_dir),
            "--staged-wiki-packs-dir",
            str(staged.staged_wiki_packs_dir),
            "--require-compaction-manifest",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["graph_nodes"] == 2
    assert payload["graph_edges"] == 1
    assert payload["wiki_pages"] == 2
    assert payload["graph_pack_ids"] == ["base-export-2"]
    assert payload["base_export_id"] == "export-2"


def test_pack_compaction_validate_rejects_stale_entity_wikilinks(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    write_wiki_overlay_pack(
        pack_dir=wiki / "wiki-packs" / "overlay-z-stale-link",
        pack_id="overlay-z-stale-link",
        base_export_id="export-1",
        parent_export_id="export-1",
        pages={"entities/skills/new.md": "# New\n\n[[entities/skills/missing]]\n"},
        tombstones=[],
    )

    rc = pack_compaction.main(
        [
            "validate",
            "--wiki-path",
            str(wiki),
            "--json",
        ]
    )

    assert rc == 1
    assert "stale wiki links: 1" in capsys.readouterr().err


def test_promote_staged_pack_sets_rejects_invalid_staged_wiki_before_graph_swap(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )
    pages_path = staged.staged_wiki_packs_dir / "base-export-2" / "pages.jsonl"
    pages_path.write_text('{"path":"entities/skills/new.md","text":"tampered","sha256":"bad"}\n')

    with pytest.raises(PackCompactionError, match="checksum mismatch"):
        promote_staged_pack_sets(
            wiki_path=wiki,
            staged_graph_packs_dir=staged.staged_graph_packs_dir,
            staged_wiki_packs_dir=staged.staged_wiki_packs_dir,
        )

    assert [
        entry.manifest.pack_id for entry in discover_pack_manifests(wiki / "graphify-out" / "packs")
    ] == [
        "base-export-1",
        "overlay-new",
    ]
    assert [
        entry.manifest.pack_id for entry in discover_wiki_pack_manifests(wiki / "wiki-packs")
    ] == [
        "base-export-1",
        "overlay-new",
    ]


def test_promote_staged_pack_sets_rejects_graph_wiki_entity_mismatch(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )
    staged_wiki_base = staged.staged_wiki_packs_dir / "base-export-2"
    pages_path = staged_wiki_base / "pages.jsonl"
    page_rows = [
        json.loads(line)
        for line in pages_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    page_rows = [row for row in page_rows if row["path"] != "entities/skills/new.md"]
    pages_path.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in page_rows),
        encoding="utf-8",
    )
    wiki_manifest_path = staged_wiki_base / "wiki-pack-manifest.json"
    wiki_manifest = json.loads(wiki_manifest_path.read_text(encoding="utf-8"))
    wiki_manifest["page_count"] = len(page_rows)
    wiki_manifest["checksums"]["pages.jsonl"] = _sha256_file(pages_path)
    wiki_manifest_path.write_text(
        json.dumps(wiki_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    compaction_manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    compaction_manifest["wiki"] = wiki_manifest
    staged.manifest_path.write_text(
        json.dumps(compaction_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(PackCompactionError, match="missing wiki pages"):
        promote_staged_pack_sets(
            wiki_path=wiki,
            staged_graph_packs_dir=staged.staged_graph_packs_dir,
            staged_wiki_packs_dir=staged.staged_wiki_packs_dir,
        )

    assert [
        entry.manifest.pack_id for entry in discover_pack_manifests(wiki / "graphify-out" / "packs")
    ] == [
        "base-export-1",
        "overlay-new",
    ]


def test_promote_staged_pack_sets_rejects_missing_compaction_manifest(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )
    staged.manifest_path.unlink()

    with pytest.raises(PackCompactionError, match="pack-compaction-manifest.json"):
        promote_staged_pack_sets(
            wiki_path=wiki,
            staged_graph_packs_dir=staged.staged_graph_packs_dir,
            staged_wiki_packs_dir=staged.staged_wiki_packs_dir,
        )

    assert [
        entry.manifest.pack_id for entry in discover_pack_manifests(wiki / "graphify-out" / "packs")
    ] == [
        "base-export-1",
        "overlay-new",
    ]


def test_promote_staged_pack_sets_rejects_manifest_dir_mismatch(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    manifest["staged_wiki_packs_dir"] = str(tmp_path / "other-wiki-packs")
    staged.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PackCompactionError, match="staged_wiki_packs_dir"):
        promote_staged_pack_sets(
            wiki_path=wiki,
            staged_graph_packs_dir=staged.staged_graph_packs_dir,
            staged_wiki_packs_dir=staged.staged_wiki_packs_dir,
        )


def test_promote_staged_pack_sets_rejects_manifest_graph_section_drift(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )
    manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
    manifest["graph"]["edge_count"] = 999
    staged.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(PackCompactionError, match="graph manifest"):
        promote_staged_pack_sets(
            wiki_path=wiki,
            staged_graph_packs_dir=staged.staged_graph_packs_dir,
            staged_wiki_packs_dir=staged.staged_wiki_packs_dir,
        )


def test_promote_staged_pack_sets_rejects_unrecorded_staged_wiki_overlay(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    _write_active_pack_sets(wiki)
    staged = compact_active_pack_sets(
        wiki_path=wiki,
        base_export_id="export-2",
        staging_dir=tmp_path / "staged-compaction",
    )
    write_wiki_overlay_pack(
        pack_dir=staged.staged_wiki_packs_dir / "overlay-extra",
        pack_id="overlay-extra",
        base_export_id="export-2",
        parent_export_id="export-2",
        pages={"entities/skills/extra.md": "# Extra\n"},
        tombstones=[],
    )

    with pytest.raises(PackCompactionError, match="exactly one base pack"):
        promote_staged_pack_sets(
            wiki_path=wiki,
            staged_graph_packs_dir=staged.staged_graph_packs_dir,
            staged_wiki_packs_dir=staged.staged_wiki_packs_dir,
        )


def _write_active_pack_sets(wiki: Path) -> tuple[Path, Path]:
    graph_packs = wiki / "graphify-out" / "packs"
    wiki_packs = wiki / "wiki-packs"
    graph = nx.Graph()
    graph.add_node("skill:old", type="skill", label="old")
    graph.add_node("skill:keep", type="skill", label="keep")
    graph.add_edge("skill:old", "skill:keep", weight=0.2)
    write_base_pack(
        pack_dir=graph_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        config_hash="config-1",
        model_id="model-1",
        graph=graph,
    )
    write_overlay_pack(
        pack_dir=graph_packs / "overlay-new",
        pack_id="overlay-new",
        base_export_id="export-1",
        parent_export_id="export-1",
        config_hash="config-1",
        model_id="model-1",
        nodes=[{"id": "skill:new", "type": "skill", "label": "new"}],
        edges=[{"source": "skill:new", "target": "skill:keep", "weight": 0.9}],
        tombstones=[{"node_id": "skill:old"}],
    )
    write_wiki_base_pack(
        pack_dir=wiki_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="export-1",
        pages={
            "entities/skills/old.md": "# Old\n",
            "entities/skills/keep.md": "# Keep\n",
        },
    )
    write_wiki_overlay_pack(
        pack_dir=wiki_packs / "overlay-new",
        pack_id="overlay-new",
        base_export_id="export-1",
        parent_export_id="export-1",
        pages={"entities/skills/new.md": "# New\n"},
        tombstones=["entities/skills/old.md"],
    )
    return graph_packs, wiki_packs


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
