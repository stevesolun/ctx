from __future__ import annotations

import json
import sqlite3
import sys
import zlib
from pathlib import Path

import pytest

from scripts import audit_backup, tune_similarity_thresholds
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


def test_audit_backup_help_does_not_require_snapshot(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(sys, "argv", ["audit_backup.py", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        audit_backup.main()

    assert exc_info.value.code == 0

    output = capsys.readouterr().out
    assert "usage: python scripts/audit_backup.py [SNAPSHOT]" in output
    assert "manifest.json" not in output


def test_tune_similarity_thresholds_help_does_not_load_embedder(capsys) -> None:
    assert tune_similarity_thresholds.main(["--help"]) == 0

    output = capsys.readouterr().out
    assert "usage: python scripts/tune_similarity_thresholds.py" in output
    assert "without loading the embedding model" in output


def test_tune_similarity_thresholds_reports_f1() -> None:
    precision, recall, f1, tp, fn, fp = tune_similarity_thresholds._precision_recall_f1(
        near_scores=[("near-1", 0.9), ("near-2", 0.4)],
        negative_scores=[("distinct-1", 0.8), ("distinct-2", 0.3)],
        threshold=0.5,
    )

    assert precision == 0.5
    assert recall == 0.5
    assert f1 == 0.5
    assert (tp, fn, fp) == (1, 1, 1)


def test_dashboard_graph_index_accepts_bom_graph_json(tmp_path: Path) -> None:
    graph_json = tmp_path / "graph.json"
    output = tmp_path / "dashboard.sqlite3"
    graph_json.write_text(
        json.dumps(
            {
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
            }
        ),
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
