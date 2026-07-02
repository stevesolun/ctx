"""SQLite operational store for merged ctx graph reads.

The JSON/pack graph remains the source artifact. This module materializes a
small local SQLite store for fast node search and neighborhood lookups.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import networkx as nx

SCHEMA_VERSION = 1


def build_graph_store(
    db_path: Path,
    graph: nx.Graph,
    *,
    extra_metadata: Mapping[str, str] | None = None,
) -> None:
    """Materialize *graph* into a SQLite store at *db_path*."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(
            """
            DROP TABLE IF EXISTS metadata;
            DROP TABLE IF EXISTS nodes;
            DROP TABLE IF EXISTS edges;
            CREATE TABLE metadata (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE nodes (
              id TEXT PRIMARY KEY,
              type TEXT,
              label TEXT,
              title TEXT,
              tags_json TEXT NOT NULL,
              attrs_json TEXT NOT NULL,
              search_text TEXT NOT NULL
            );
            CREATE TABLE edges (
              source TEXT NOT NULL,
              target TEXT NOT NULL,
              weight REAL NOT NULL DEFAULT 0.0,
              attrs_json TEXT NOT NULL,
              PRIMARY KEY (source, target)
            );
            CREATE INDEX idx_nodes_type ON nodes(type);
            CREATE INDEX idx_nodes_search_text ON nodes(search_text);
            CREATE INDEX idx_edges_source ON edges(source);
            CREATE INDEX idx_edges_target ON edges(target);
            """
        )
        conn.executemany(
            "INSERT INTO metadata(key, value) VALUES(:key, :value)",
            _metadata_rows(graph, extra_metadata=extra_metadata),
        )
        conn.executemany(
            """
            INSERT INTO nodes(id, type, label, title, tags_json, attrs_json, search_text)
            VALUES(:id, :type, :label, :title, :tags_json, :attrs_json, :search_text)
            """,
            (_node_row(node_id, attrs) for node_id, attrs in graph.nodes(data=True)),
        )
        conn.executemany(
            """
            INSERT INTO edges(source, target, weight, attrs_json)
            VALUES(:source, :target, :weight, :attrs_json)
            """,
            (_edge_row(source, target, attrs) for source, target, attrs in graph.edges(data=True)),
        )


def build_graph_store_from_graph_dir(
    graph_dir: Path,
    db_path: Path,
    *,
    apply_runtime_filter: bool = True,
) -> dict[str, int]:
    """Build a SQLite store from a graphify-out directory.

    ``resolve_graph.load_graph`` is the single source of truth for graph
    loading. It prefers active graph packs beside ``graph.json`` and falls
    back to the legacy monolithic ``graph.json`` only when packs are absent.
    """
    from ctx.core.graph.resolve_graph import load_graph  # noqa: PLC0415

    source_metadata = _graph_dir_source_metadata(graph_dir)
    if source_metadata.get("ctx_graph_store_source") == "missing":
        raise ValueError("source graph is missing")

    graph = load_graph(
        graph_dir / "graph.json",
        apply_runtime_filter=apply_runtime_filter,
    )
    build_graph_store(
        db_path,
        graph,
        extra_metadata=source_metadata,
    )
    return graph_store_stats(db_path)


def ensure_graph_store(
    graph_dir: Path,
    db_path: Path,
    *,
    apply_runtime_filter: bool = True,
) -> dict[str, bool | int]:
    """Reuse a fresh SQLite store or rebuild it from the graph directory."""
    if graph_store_is_fresh(db_path, graph_dir):
        return {"rebuilt": False, **graph_store_stats(db_path)}
    stats = build_graph_store_from_graph_dir(
        graph_dir,
        db_path,
        apply_runtime_filter=apply_runtime_filter,
    )
    return {"rebuilt": True, **stats}


def graph_store_stats(db_path: Path) -> dict[str, int]:
    """Return node/edge counts for an existing graph store."""
    with _connect(db_path) as conn:
        return {
            "nodes": int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]),
            "edges": int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]),
        }


def graph_store_metadata(db_path: Path) -> dict[str, str]:
    """Return metadata recorded when the graph store was materialized."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}


def graph_store_is_fresh(db_path: Path, graph_dir: Path) -> bool:
    """Return whether *db_path* still reflects *graph_dir* sources."""
    if not db_path.is_file():
        return False
    try:
        stored = graph_store_metadata(db_path)
        current = _graph_dir_source_metadata(graph_dir)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return False
    if current.get("ctx_graph_store_source") == "missing":
        return False
    return all(stored.get(key) == value for key, value in current.items())


def validate_graph_store(db_path: Path, graph_dir: Path) -> dict[str, object]:
    """Validate a SQLite store against its recorded source graph directory."""
    errors: list[str] = []
    if not db_path.is_file():
        return {
            "ok": False,
            "fresh": False,
            "nodes": 0,
            "edges": 0,
            "errors": ["graph store is missing"],
        }

    try:
        stats = graph_store_stats(db_path)
        metadata = graph_store_metadata(db_path)
    except sqlite3.DatabaseError as exc:
        return {
            "ok": False,
            "fresh": False,
            "nodes": 0,
            "edges": 0,
            "errors": [f"graph store is unreadable: {exc}"],
        }

    if metadata.get("schema_version") != str(SCHEMA_VERSION):
        errors.append("schema_version is not supported")
    _validate_count_metadata(metadata, stats, "node_count", "nodes", errors)
    _validate_count_metadata(metadata, stats, "edge_count", "edges", errors)
    source_missing = _source_graph_is_missing(graph_dir)
    fresh = graph_store_is_fresh(db_path, graph_dir)
    if source_missing:
        errors.append("source graph is missing")
    elif not fresh:
        errors.append("source fingerprint is stale")
    return {
        "ok": not errors,
        "fresh": fresh,
        "nodes": stats["nodes"],
        "edges": stats["edges"],
        "errors": errors,
    }


def search_nodes(db_path: Path, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    """Search nodes by id, label, title, type, or tags."""
    term = query.strip().lower()
    if not term or limit <= 0:
        return []
    like = f"%{term}%"
    prefix = f"{term}%"
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, type, label, title, tags_json
            FROM nodes
            WHERE search_text LIKE ?
            ORDER BY
              CASE
                WHEN lower(id) = ? OR lower(label) = ? THEN 0
                WHEN lower(id) LIKE ? OR lower(label) LIKE ? THEN 1
                WHEN lower(title) LIKE ? THEN 2
                ELSE 3
              END,
              id
            LIMIT ?
            """,
            (like, term, term, prefix, prefix, like, limit),
        ).fetchall()
    return [_node_result(row) for row in rows]


def load_neighborhood(
    db_path: Path, node_id: str, *, limit: int = 50
) -> dict[str, list[dict[str, Any]]]:
    """Return a 1-hop neighborhood centered on *node_id*."""
    if limit <= 0:
        limit = 1
    with _connect(db_path) as conn:
        center = conn.execute(
            "SELECT id, type, label, title, tags_json FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if center is None:
            return {"nodes": [], "edges": []}
        edge_rows = conn.execute(
            """
            SELECT source, target, weight, attrs_json
            FROM edges
            WHERE source = ? OR target = ?
            ORDER BY weight DESC, source, target
            LIMIT ?
            """,
            (node_id, node_id, limit),
        ).fetchall()
        neighbor_ids = {
            row["target"] if row["source"] == node_id else row["source"] for row in edge_rows
        }
        nodes = [_node_result(center)]
        if neighbor_ids:
            placeholders = ",".join("?" for _ in neighbor_ids)
            nodes.extend(
                _node_result(row)
                for row in conn.execute(
                    f"SELECT id, type, label, title, tags_json FROM nodes WHERE id IN ({placeholders})",
                    tuple(sorted(neighbor_ids)),
                ).fetchall()
            )
    edges = [_edge_result(row, center_id=node_id) for row in edge_rows]
    return {"nodes": nodes, "edges": edges}


def main(argv: list[str] | None = None) -> int:
    """CLI for materializing a graph directory into the SQLite store."""
    parser = argparse.ArgumentParser(
        prog="python -m ctx.core.graph.graph_store",
        description="Build and inspect the ctx SQLite graph operational store.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser(
        "build",
        help="Build a SQLite store from graphify-out packs or graph.json.",
    )
    build.add_argument("--graph-dir", required=True, help="Path to graphify-out")
    build.add_argument("--db", required=True, help="Destination SQLite database")
    build.add_argument(
        "--no-runtime-filter",
        action="store_true",
        help="Preserve all stored edges instead of applying runtime graph filters.",
    )
    validate = sub.add_parser(
        "validate",
        help="Validate a SQLite store against graphify-out sources.",
    )
    validate.add_argument("--graph-dir", required=True, help="Path to graphify-out")
    validate.add_argument("--db", required=True, help="SQLite database to validate")
    search = sub.add_parser(
        "search",
        help="Search a built SQLite graph store.",
    )
    search.add_argument("--db", required=True, help="SQLite database to query")
    search.add_argument("--graph-dir", help="Require the store to be fresh for this graphify-out")
    search.add_argument("--query", required=True, help="Search text")
    search.add_argument("--limit", type=int, default=20, help="Maximum rows to return")
    neighborhood = sub.add_parser(
        "neighborhood",
        help="Read a 1-hop neighborhood from a built SQLite graph store.",
    )
    neighborhood.add_argument("--db", required=True, help="SQLite database to query")
    neighborhood.add_argument(
        "--graph-dir", help="Require the store to be fresh for this graphify-out"
    )
    neighborhood.add_argument("--node-id", required=True, help="Center node id")
    neighborhood.add_argument("--limit", type=int, default=50, help="Maximum edges to return")

    args = parser.parse_args(argv)
    if args.command == "build":
        try:
            stats = build_graph_store_from_graph_dir(
                Path(args.graph_dir),
                Path(args.db),
                apply_runtime_filter=not args.no_runtime_filter,
            )
        except ValueError as exc:
            print(json.dumps({"error": str(exc), "ok": False}, sort_keys=True))
            return 1
        print(json.dumps(stats, sort_keys=True))
        return 0
    if args.command == "validate":
        report = validate_graph_store(Path(args.db), Path(args.graph_dir))
        print(json.dumps(report, sort_keys=True))
        return 0 if report["ok"] else 1
    if args.command == "search":
        db_path = Path(args.db)
        if args.graph_dir:
            report = validate_graph_store(db_path, Path(args.graph_dir))
            if not report["ok"]:
                print(json.dumps(report, sort_keys=True))
                return 1
        rows = search_nodes(db_path, args.query, limit=args.limit)
        print(json.dumps({"results": rows}, sort_keys=True))
        return 0
    if args.command == "neighborhood":
        db_path = Path(args.db)
        if args.graph_dir:
            report = validate_graph_store(db_path, Path(args.graph_dir))
            if not report["ok"]:
                print(json.dumps(report, sort_keys=True))
                return 1
        neighborhood_payload = load_neighborhood(db_path, args.node_id, limit=args.limit)
        print(json.dumps(neighborhood_payload, sort_keys=True))
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _metadata_rows(
    graph: nx.Graph,
    *,
    extra_metadata: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    metadata = {
        "schema_version": str(SCHEMA_VERSION),
        "node_count": str(graph.number_of_nodes()),
        "edge_count": str(graph.number_of_edges()),
    }
    for key, value in sorted(graph.graph.items()):
        if value is None:
            continue
        metadata[str(key)] = _metadata_value(value)
    if extra_metadata:
        metadata.update(extra_metadata)
    return [{"key": key, "value": value} for key, value in sorted(metadata.items())]


def _graph_dir_source_metadata(graph_dir: Path) -> dict[str, str]:
    from ctx.core.graph.graph_packs import (  # noqa: PLC0415
        discover_pack_manifests,
        sha256_file,
    )

    overlay_metadata = _entity_overlay_source_metadata(graph_dir)
    packs_dir = graph_dir / "packs"
    if packs_dir.is_dir():
        entries = discover_pack_manifests(packs_dir)
        if entries:
            pack_ids = [entry.manifest.pack_id for entry in entries]
            pack_payload = [entry.manifest.to_mapping() for entry in entries]
            return {
                "ctx_graph_store_source": "packs",
                "ctx_graph_store_fingerprint": _fingerprint_payload(pack_payload),
                "ctx_graph_store_pack_ids": json.dumps(pack_ids, sort_keys=True),
                **overlay_metadata,
            }

    graph_json = graph_dir / "graph.json"
    if graph_json.is_file():
        return {
            "ctx_graph_store_source": "graph.json",
            "ctx_graph_store_fingerprint": sha256_file(graph_json),
            **overlay_metadata,
        }
    return {
        "ctx_graph_store_source": "missing",
        "ctx_graph_store_fingerprint": "",
        **overlay_metadata,
    }


def _entity_overlay_source_metadata(graph_dir: Path) -> dict[str, str]:
    from ctx.core.graph.graph_packs import sha256_file  # noqa: PLC0415

    overlay_path = graph_dir / "entity-overlays.jsonl"
    if not overlay_path.is_file():
        return {
            "ctx_graph_store_entity_overlay": "absent",
            "ctx_graph_store_entity_overlay_fingerprint": "",
        }
    return {
        "ctx_graph_store_entity_overlay": "present",
        "ctx_graph_store_entity_overlay_fingerprint": sha256_file(overlay_path),
    }


def _source_graph_is_missing(graph_dir: Path) -> bool:
    try:
        return _graph_dir_source_metadata(graph_dir).get("ctx_graph_store_source") == "missing"
    except (OSError, ValueError):
        return False


def _fingerprint_payload(payload: object) -> str:
    encoded = json.dumps(
        _jsonable(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _metadata_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return json.dumps(_jsonable(value), sort_keys=True, default=str)


def _validate_count_metadata(
    metadata: Mapping[str, str],
    stats: Mapping[str, int],
    metadata_key: str,
    stats_key: str,
    errors: list[str],
) -> None:
    raw_value = metadata.get(metadata_key)
    if raw_value is None:
        errors.append(f"metadata {metadata_key} is missing")
        return
    try:
        value = int(raw_value)
    except ValueError:
        errors.append(f"metadata {metadata_key} is not an integer")
        return
    actual = stats[stats_key]
    if value != actual:
        errors.append(f"metadata {metadata_key} {value} != actual {actual}")


def _node_row(node_id: str, attrs: dict[str, Any]) -> dict[str, Any]:
    label = _optional_str(attrs.get("label")) or node_id.split(":", 1)[-1]
    title = _optional_str(attrs.get("title")) or label
    entity_type = _optional_str(attrs.get("type"))
    tags = _string_list(attrs.get("tags"))
    search_text = " ".join([node_id, label, title, entity_type or "", *tags]).lower()
    return {
        "id": node_id,
        "type": entity_type,
        "label": label,
        "title": title,
        "tags_json": json.dumps(tags, sort_keys=True),
        "attrs_json": json.dumps(_jsonable(attrs), sort_keys=True),
        "search_text": search_text,
    }


def _edge_row(source: str, target: str, attrs: dict[str, Any]) -> dict[str, Any]:
    weight = attrs.get("final_weight", attrs.get("weight", 0.0))
    try:
        numeric_weight = float(weight)
    except (TypeError, ValueError):
        numeric_weight = 0.0
    return {
        "source": source,
        "target": target,
        "weight": numeric_weight,
        "attrs_json": json.dumps(_jsonable(attrs), sort_keys=True),
    }


def _node_result(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "label": row["label"],
        "title": row["title"],
        "tags": json.loads(row["tags_json"]),
    }


def _edge_result(row: sqlite3.Row, *, center_id: str) -> dict[str, Any]:
    source = row["source"]
    target = row["target"]
    if target == center_id:
        source, target = target, source
    attrs = json.loads(row["attrs_json"])
    return {
        "source": source,
        "target": target,
        "weight": row["weight"],
        "attrs": attrs,
    }


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _jsonable(value: object) -> object:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
