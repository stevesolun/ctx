#!/usr/bin/env python3
"""Build the compact dashboard graph-neighborhood SQLite index."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import zlib
from pathlib import Path
from typing import Any


def _graph_edges(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("links") if "links" in data else data.get("edges", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def build_dashboard_index(graph_json: Path, output: Path, *, top_k: int = 40) -> None:
    data = json.loads(graph_json.read_text(encoding="utf-8-sig"))
    nodes_raw = [item for item in data.get("nodes", []) if isinstance(item, dict)]
    edges_raw = _graph_edges(data)
    nodes: dict[str, dict[str, Any]] = {}
    slug_rows: list[tuple[str, str, str]] = []
    neighbors: dict[str, list[dict[str, Any]]] = {}

    for node in nodes_raw:
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            continue
        slug = node_id.split(":", 1)[-1]
        node_type = str(node.get("type") or node_id.split(":", 1)[0])
        raw_tags = node.get("tags")
        tags = raw_tags if isinstance(raw_tags, list) else []
        nodes[node_id] = {
            "id": node_id,
            "label": node.get("label") or slug,
            "type": node_type,
            "tags": tags[:8],
            "description": node.get("description") or "",
            "quality_score": node.get("quality_score"),
            "usage_score": node.get("usage_score"),
            "degree": 0,
        }
        slug_rows.append((slug, node_type, node_id))
        neighbors[node_id] = []

    for edge in edges_raw:
        source = edge.get("source")
        target = edge.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        if source not in nodes or target not in nodes:
            continue
        weight = float(edge.get("weight", 1.0) or 0.0)
        row = {
            "target": target,
            "weight": weight,
            "shared_tags": (edge.get("shared_tags") or [])[:4],
            "reasons": (edge.get("reasons") or edge.get("edge_reasons") or [])[:4],
            "semantic": edge.get("semantic"),
            "tag_sim": edge.get("tag_sim"),
            "slug_token_sim": edge.get("slug_token_sim"),
            "source_overlap": edge.get("source_overlap"),
        }
        neighbors[source].append(row)
        reverse = dict(row)
        reverse["target"] = source
        neighbors[target].append(reverse)

    for node_id, rows in neighbors.items():
        rows.sort(key=lambda row: -float(row.get("weight") or 0.0))
        nodes[node_id]["degree"] = len(rows)
        del rows[top_k:]

    output.parent.mkdir(parents=True, exist_ok=True)
    build_path = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if build_path.exists():
        build_path.unlink()
    try:
        conn = sqlite3.connect(build_path)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute(
                "CREATE TABLE nodes("
                "id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,description TEXT,"
                "quality_score REAL,usage_score REAL,degree INTEGER)"
            )
            conn.execute(
                "CREATE TABLE slug_index("
                "slug TEXT,type TEXT,node_id TEXT,PRIMARY KEY(slug,type,node_id))"
            )
            conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
            meta = {
                "version": 1,
                "export_id": data.get("graph", {}).get("export_id"),
                "nodes_count": len(nodes),
                "edges_count": len(edges_raw),
                "max_degree": max((int(node["degree"]) for node in nodes.values()), default=1),
                "top_k": top_k,
            }
            conn.executemany(
                "INSERT INTO meta(key,value) VALUES(?,?)",
                [(key, json.dumps(value)) for key, value in meta.items()],
            )
            conn.executemany(
                "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        node["id"],
                        node["label"],
                        node["type"],
                        json.dumps(node["tags"], separators=(",", ":")),
                        node["description"],
                        node["quality_score"],
                        node["usage_score"],
                        node["degree"],
                    )
                    for node in nodes.values()
                ],
            )
            conn.executemany("INSERT OR IGNORE INTO slug_index VALUES(?,?,?)", slug_rows)
            neighbor_rows = []
            for source, rows in neighbors.items():
                slim = [
                    {key: value for key, value in row.items() if value not in (None, [], "")}
                    for row in rows
                ]
                payload = zlib.compress(
                    json.dumps(slim, separators=(",", ":")).encode("utf-8"),
                    level=6,
                )
                neighbor_rows.append((source, payload))
                if len(neighbor_rows) >= 10_000:
                    conn.executemany("INSERT INTO neighbors VALUES(?,?)", neighbor_rows)
                    neighbor_rows.clear()
            if neighbor_rows:
                conn.executemany("INSERT INTO neighbors VALUES(?,?)", neighbor_rows)
            conn.execute("CREATE INDEX idx_slug_index_slug ON slug_index(slug)")
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        os.replace(build_path, output)
    finally:
        if build_path.exists():
            build_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--graph-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=40)
    args = parser.parse_args()
    build_dashboard_index(args.graph_json, args.output, top_k=args.top_k)


if __name__ == "__main__":
    main()
