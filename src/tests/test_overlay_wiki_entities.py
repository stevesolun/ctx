from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.overlay_wiki_entities import _entity_page, _skill_replacements, overlay_entities

ROOT = Path(__file__).resolve().parents[2]


def _add_text(tf: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = 0
    tf.addfile(info, __import__("io").BytesIO(data))


def _read_json(tf: tarfile.TarFile, name: str) -> dict:
    member = tf.getmember(name)
    f = tf.extractfile(member)
    assert f is not None
    return json.loads(f.read().decode("utf-8"))


def test_overlay_entities_preserves_existing_graph_and_adds_selected_pages(tmp_path: Path) -> None:
    source_wiki = tmp_path / "wiki"
    (source_wiki / "graphify-out").mkdir(parents=True)
    (source_wiki / "entities" / "skills").mkdir(parents=True)
    (source_wiki / "entities" / "harnesses").mkdir(parents=True)
    skills_root = tmp_path / "skills"
    (skills_root / "new-skill" / "references").mkdir(parents=True)
    (source_wiki / "entities" / "skills" / "new-skill.md").write_text("# New skill\n")
    (source_wiki / "entities" / "harnesses" / "new-harness.md").write_text("# Harness\n")
    body = skills_root / "new-skill" / "SKILL.md"
    body.write_text("# body\n", encoding="utf-8")
    (skills_root / "new-skill" / "references" / "guide.md").write_text(
        "# guide\n", encoding="utf-8"
    )
    source_graph = {
        "graph": {"export_id": "source"},
        "nodes": [
            {"id": "skill:old", "label": "old", "type": "skill"},
            {"id": "skill:new-skill", "label": "new", "type": "skill"},
            {"id": "harness:new-harness", "label": "harness", "type": "harness"},
        ],
        "edges": [
            {"source": "skill:old", "target": "skill:new-skill", "weight": 0.5},
            {"source": "skill:old", "target": "harness:new-harness", "weight": 0.7},
        ],
    }
    (source_wiki / "graphify-out" / "graph.json").write_text(json.dumps(source_graph))

    tarball = tmp_path / "wiki-graph.tar.gz"
    graph = {
        "graph": {"export_id": "old"},
        "nodes": [{"id": "skill:old", "label": "old", "type": "skill"}],
        "edges": [{"source": "skill:old", "target": "skill:other", "weight": 0.1}],
    }
    communities = {
        "export_id": "old",
        "communities": {"0": {"label": "Core", "members": ["skill:old"]}},
        "total_communities": 1,
        "generated": "old",
    }
    manifest = {
        "version": 1,
        "export_id": "old",
        "artifacts": {
            "graph": "graph.json",
            "delta": "graph-delta.json",
            "communities": "communities.json",
            "report": "graph-report.md",
        },
        "counts": {"nodes": 1, "edges": 1, "communities": 1},
    }
    with tarfile.open(tarball, "w:gz") as tf:
        _add_text(tf, "./graphify-out/graph.json", json.dumps(graph))
        _add_text(tf, "./graphify-out/communities.json", json.dumps(communities))
        _add_text(
            tf, "./graphify-out/graph-delta.json", json.dumps({"version": 1, "export_id": "old"})
        )
        _add_text(tf, "./graphify-out/graph-report.md", "> Export ID: old\n")
        _add_text(tf, "./graphify-out/graph-export-manifest.json", json.dumps(manifest))
        _add_text(tf, "./keep.md", "keep")

    root_communities = tmp_path / "communities.json"
    stats = overlay_entities(
        source_wiki=source_wiki,
        tarball=tarball,
        entity_ids=["skill:new-skill", "harness:new-harness"],
        skills_root=skills_root,
        root_communities=root_communities,
        now=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )

    assert stats.node_count == 3
    assert stats.edge_count == 3
    assert stats.added_nodes == 2
    assert stats.added_edges == 2
    with tarfile.open(tarball, "r:gz") as tf:
        graph_out = _read_json(tf, "./graphify-out/graph.json")
        manifest_out = _read_json(tf, "./graphify-out/graph-export-manifest.json")
        communities_out = _read_json(tf, "./graphify-out/communities.json")
        assert graph_out["graph"]["export_id"] == stats.export_id
        assert manifest_out["export_id"] == stats.export_id
        assert communities_out["export_id"] == stats.export_id
        assert tf.extractfile("./keep.md") is not None
        assert tf.extractfile("./entities/skills/new-skill.md") is not None
        assert tf.extractfile("./entities/harnesses/new-harness.md") is not None
        assert tf.extractfile("./converted/new-skill/SKILL.md") is not None
        assert tf.extractfile("./converted/new-skill/references/guide.md") is not None
        index_member = tf.extractfile("./graphify-out/dashboard-neighborhoods.sqlite3")
        assert index_member is not None
        index_path = tmp_path / "dashboard-neighborhoods.sqlite3"
        index_path.write_bytes(index_member.read())
    with sqlite3.connect(index_path) as conn:
        assert conn.execute(
            "SELECT node_id FROM slug_index WHERE slug = ?",
            ("new-skill",),
        ).fetchone() == ("skill:new-skill",)
    assert json.loads(root_communities.read_text())["export_id"] == stats.export_id


def test_script_direct_invocation_help_works() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "overlay_wiki_entities.py"), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert proc.returncode == 0
    assert "Overlay explicit local wiki entities" in proc.stdout


def test_overlay_rejects_symlinked_entity_page(tmp_path: Path) -> None:
    source_wiki = tmp_path / "wiki"
    (source_wiki / "entities" / "skills").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = source_wiki / "entities" / "skills" / "new-skill.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="symlinked path"):
        _entity_page(source_wiki, "skill", "new-skill")


def test_overlay_rejects_symlinked_skill_reference(tmp_path: Path) -> None:
    source_wiki = tmp_path / "wiki"
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "new-skill"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# body\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = skill_dir / "references" / "leak.md"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="symlinked path"):
        _skill_replacements(source_wiki, "new-skill", skills_root=skills_root)
