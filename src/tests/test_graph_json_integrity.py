"""Regression tests for graph.json deserialization integrity."""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
from networkx.readwrite import node_link_data

from ctx.core.graph import resolve_graph


def _write_graph_file(tmp_path: Path, content: bytes | str) -> Path:
    path = tmp_path / "graph.json"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


class TestLoadGraphIntegrity:
    """load_graph must return an empty graph rather than raise on bad input."""

    def test_file_not_found_returns_empty_graph(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent" / "graph.json"

        graph = resolve_graph.load_graph(path=missing)

        assert isinstance(graph, nx.Graph)
        assert graph.number_of_nodes() == 0

    def test_invalid_encoding_returns_empty_graph(self, tmp_path: Path) -> None:
        path = _write_graph_file(tmp_path, b"\xff\xfe{bad encoding\x00")

        graph = resolve_graph.load_graph(path=path)

        assert graph.number_of_nodes() == 0

    def test_truncated_json_returns_empty_graph(self, tmp_path: Path) -> None:
        path = _write_graph_file(tmp_path, '{"nodes": [{"id": "skill:foo"')

        graph = resolve_graph.load_graph(path=path)

        assert graph.number_of_nodes() == 0

    def test_missing_nodes_key_returns_empty_graph(self, tmp_path: Path) -> None:
        path = _write_graph_file(tmp_path, json.dumps({"links": []}))

        graph = resolve_graph.load_graph(path=path)

        assert graph.number_of_nodes() == 0

    def test_missing_links_key_returns_empty_graph(self, tmp_path: Path) -> None:
        path = _write_graph_file(tmp_path, json.dumps({"nodes": []}))

        graph = resolve_graph.load_graph(path=path)

        assert graph.number_of_nodes() == 0

    def test_wrong_schema_type_returns_empty_graph(self, tmp_path: Path) -> None:
        path = _write_graph_file(tmp_path, json.dumps([{"id": "skill:foo"}]))

        graph = resolve_graph.load_graph(path=path)

        assert graph.number_of_nodes() == 0

    def test_null_json_returns_empty_graph(self, tmp_path: Path) -> None:
        path = _write_graph_file(tmp_path, "null")

        graph = resolve_graph.load_graph(path=path)

        assert graph.number_of_nodes() == 0

    def test_valid_graph_loads_correctly(self, tmp_path: Path) -> None:
        source_graph = nx.Graph()
        source_graph.add_node(
            "skill:fastapi-pro",
            label="fastapi-pro",
            type="skill",
            tags=["python"],
        )
        source_graph.add_node(
            "skill:docker-expert",
            label="docker-expert",
            type="skill",
            tags=["docker"],
        )
        source_graph.add_edge("skill:fastapi-pro", "skill:docker-expert", weight=1.0)
        path = _write_graph_file(tmp_path, json.dumps(node_link_data(source_graph)))

        graph = resolve_graph.load_graph(path=path)

        assert graph.number_of_nodes() == 2
        assert graph.number_of_edges() == 1
