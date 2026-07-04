from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

from ctx.monitor.services.graph_artifacts import (
    dashboard_index_matches_manifest,
    ensure_dashboard_graph_index,
)


def _write_dashboard_index(path: Path, *, export_id: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES(?,?)", ("export_id", json.dumps(export_id)))
        conn.execute("CREATE TABLE padding(payload BLOB NOT NULL)")
        conn.execute("INSERT INTO padding VALUES(?)", (b"x" * (1024 * 1024 + 17),))
        conn.commit()
    finally:
        conn.close()


def test_ensure_dashboard_graph_index_extracts_multichunk_archive_member(
    tmp_path: Path,
) -> None:
    wiki_dir = tmp_path / "skill-wiki"
    graph_dir = wiki_dir / "graphify-out"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph-export-manifest.json").write_text(
        json.dumps({"version": 1, "export_id": "export-test"}),
        encoding="utf-8",
    )

    seed = tmp_path / "dashboard-neighborhoods.sqlite3"
    _write_dashboard_index(seed, export_id="export-test")
    assert seed.stat().st_size > 1024 * 1024

    archive = tmp_path / "wiki-graph-runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(seed, arcname="./graphify-out/dashboard-neighborhoods.sqlite3")

    target = graph_dir / "dashboard-neighborhoods.sqlite3"
    result = ensure_dashboard_graph_index(
        target=target,
        manifest_export_id=lambda: "export-test",
        packaged_export_id=lambda: "export-test",
        archives=lambda: [archive],
        archive_export_id=lambda _archive: "export-test",
        index_matches_manifest=lambda path: dashboard_index_matches_manifest(path, wiki_dir),
        index_member="graphify-out/dashboard-neighborhoods.sqlite3",
    )

    assert result == target
    assert dashboard_index_matches_manifest(target, wiki_dir)
    assert target.read_bytes() == seed.read_bytes()
