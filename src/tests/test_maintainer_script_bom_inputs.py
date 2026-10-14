from __future__ import annotations

import json
import sqlite3
import sys
import zlib
from pathlib import Path

import pytest

from scripts import audit_backup, build_dashboard_graph_index, tune_similarity_thresholds
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


def test_audit_backup_reports_shipped_candidate_and_tree_coverage_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claude_home = tmp_path / "home" / ".claude"
    (claude_home / "agents").mkdir(parents=True)
    (claude_home / "reports").mkdir()
    backed_config = claude_home / "skill-system-config.json"
    backed_config.write_text('{"ok": true}', encoding="utf-8")
    missing_candidates = [
        claude_home / "intent-log.jsonl",
        claude_home / "CLAUDE.md",
        claude_home / "claude.json",
    ]
    for path in missing_candidates:
        path.write_text(f"source:{path.name}\n", encoding="utf-8")
    agent = claude_home / "agents" / "reviewer.md"
    agent.write_text("# Reviewer\n", encoding="utf-8")
    report = claude_home / "reports" / "audit.json"
    report.write_text('{"status": "ok"}', encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    manifest = {
        "entries": [
            {"source": str(backed_config), "dest": backed_config.name, "size": 12},
            {"source": str(agent), "dest": "agents/reviewer.md", "size": 11},
            {"source": str(report), "dest": "reports/audit.json", "size": 16},
        ]
    }
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    monkeypatch.setattr(audit_backup, "CLAUDE_HOME", claude_home)
    monkeypatch.setattr(sys, "argv", ["audit_backup.py", str(snapshot)])

    assert audit_backup.main() == 0

    output = capsys.readouterr().out
    assert "entries:   3" in output
    coverage_rows = {tuple(line.split()) for line in output.splitlines() if line.strip()}
    assert ("agents", "1", "files") in coverage_rows
    assert ("reports", "1", "files") in coverage_rows
    assert f"OK:      {backed_config}" in output
    for path in missing_candidates:
        assert f"MISSING: {path}" in output
    assert f"absent:  {claude_home / 'skill-registry.json'}" in output
    assert f"absent:  {claude_home / '.claude.json'}" in output
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file()
    }
    assert after == before


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


def test_dashboard_graph_index_replace_failure_preserves_prior_index_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_json = tmp_path / "graph.json"
    output = tmp_path / "dashboard.sqlite3"
    graph_json.write_text(
        json.dumps(
            {
                "graph": {"export_id": "original"},
                "nodes": [{"id": "skill:original", "type": "skill"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    build_dashboard_index(graph_json, output)
    original = output.read_bytes()

    graph_json.write_text(
        json.dumps(
            {
                "graph": {"export_id": "replacement"},
                "nodes": [{"id": "skill:replacement", "type": "skill"}],
                "edges": [],
            }
        ),
        encoding="utf-8",
    )
    replace_calls: list[tuple[Path, Path]] = []

    def interrupt_replace(source: Path, target: Path) -> None:
        replace_calls.append((source, target))
        assert source.is_file()
        assert target == output
        raise OSError("forced interruption before index replacement")

    monkeypatch.setattr(build_dashboard_graph_index.os, "replace", interrupt_replace)

    with pytest.raises(OSError, match="forced interruption before index replacement"):
        build_dashboard_graph_index.build_dashboard_index(graph_json, output)

    assert len(replace_calls) == 1
    assert output.read_bytes() == original
    with sqlite3.connect(output) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='export_id'",
        ).fetchone() == ('"original"',)
        assert conn.execute("SELECT id FROM nodes").fetchall() == [("skill:original",)]
    assert not list(tmp_path.glob(f".{output.name}.*.tmp"))
