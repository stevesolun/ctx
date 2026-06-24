from __future__ import annotations

import json
import sqlite3
import sys
import zlib
from pathlib import Path

from scripts import audit_backup
from scripts.build_dashboard_graph_index import build_dashboard_index


def test_audit_backup_accepts_bom_manifest(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    claude_home = tmp_path / "home" / ".claude"
    claude_home.mkdir(parents=True)
    config = claude_home / "skill-system-config.json"
    config.write_text('{"ok": true}', encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    manifest = {
        "entries": [
            {
                "source": str(config),
                "dest": "skill-system-config.json",
                "size": config.stat().st_size,
            }
        ]
    }
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8-sig")

    monkeypatch.setattr(audit_backup, "CLAUDE_HOME", claude_home)
    monkeypatch.setattr(sys, "argv", ["audit_backup.py", str(snapshot)])

    assert audit_backup.main() == 0
    output = capsys.readouterr().out
    assert "entries:   1" in output
    assert "OK:" in output


def test_dashboard_graph_index_accepts_bom_graph_json(tmp_path: Path) -> None:
    graph_json = tmp_path / "graph.json"
    output = tmp_path / "dashboard.sqlite3"
    graph_json.write_text(
        json.dumps({
            "graph": {"export_id": "fixture"},
            "nodes": [
                {
                    "id": "skill:alpha",
                    "label": "Alpha",
                    "type": "skill",
                    "tags": ["python", "test"],
                    "quality_score": 0.9,
                },
                {
                    "id": "mcp-server:github",
                    "label": "GitHub",
                    "type": "mcp-server",
                    "tags": ["github"],
                },
            ],
            "links": [
                {
                    "source": "skill:alpha",
                    "target": "mcp-server:github",
                    "weight": 0.77,
                    "shared_tags": ["github"],
                    "reasons": ["fixture"],
                }
            ],
        }),
        encoding="utf-8-sig",
    )

    build_dashboard_index(graph_json, output, top_k=5)

    conn = sqlite3.connect(output)
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='nodes_count'",
        ).fetchone() == ("2",)
        payload = conn.execute(
            "SELECT payload FROM neighbors WHERE source='skill:alpha'",
        ).fetchone()[0]
    finally:
        conn.close()
    neighbors = json.loads(zlib.decompress(payload).decode("utf-8"))
    assert neighbors[0]["target"] == "mcp-server:github"
