"""Regression tests for P2-15: graph.json deserialization integrity."""

import json
import sys
import tempfile
from pathlib import Path

import networkx as nx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import resolve_graph


def _write_graph_file(tmp_path: Path, content: bytes | str) -> Path:
    p = tmp_path / "graph.json"
    if isinstance(content, str):
        p.write_text(content, encoding="utf-8")
    else:
        p.write_bytes(content)
    return p


class TestLoadGraphIntegrity:
    """load_graph must return an empty graph rather than raise on bad input."""

    def test_file_not_found_returns_empty_graph(self, tmp_path):
        missing = tmp_path / "nonexistent" / "graph.json"
        G = resolve_graph.load_graph(path=missing)
        assert isinstance(G, nx.Graph)
        assert G.number_of_nodes() == 0

    def test_invalid_encoding_returns_empty_graph(self, tmp_path):
        """File with non-UTF-8 bytes must not raise; returns empty graph."""
        p = _write_graph_file(tmp_path, b"\xff\xfe{bad encoding\x00")
        G = resolve_graph.load_graph(path=p)
        assert G.number_of_nodes() == 0

    def test_truncated_json_returns_empty_graph(self, tmp_path):
        p = _write_graph_file(tmp_path, '{"nodes": [{"id": "skill:foo"')
        G = resolve_graph.load_graph(path=p)
        assert G.number_of_nodes() == 0

    def test_missing_nodes_key_returns_empty_graph(self, tmp_path):
        # Has 'links' but not 'nodes'
        data = json.dumps({"links": []})
        p = _write_graph_file(tmp_path, data)
        G = resolve_graph.load_graph(path=p)
        assert G.number_of_nodes() == 0

    def test_missing_links_key_returns_empty_graph(self, tmp_path):
        # Has 'nodes' but not 'links'
        data = json.dumps({"nodes": []})
        p = _write_graph_file(tmp_path, data)
        G = resolve_graph.load_graph(path=p)
        assert G.number_of_nodes() == 0

    def test_wrong_schema_type_returns_empty_graph(self, tmp_path):
        # Root is a list, not a dict
        data = json.dumps([{"id": "skill:foo"}])
        p = _write_graph_file(tmp_path, data)
        G = resolve_graph.load_graph(path=p)
        assert G.number_of_nodes() == 0

    def test_null_json_returns_empty_graph(self, tmp_path):
        p = _write_graph_file(tmp_path, "null")
        G = resolve_graph.load_graph(path=p)
        assert G.number_of_nodes() == 0

    def test_valid_graph_loads_correctly(self, tmp_path):
        """A conforming graph.json must load with the correct node count."""
        # Use node_link_data to generate a valid fixture for the installed networkx version
        import networkx as nx
        from networkx.readwrite import node_link_data
        G_src = nx.Graph()
        G_src.add_node("skill:fastapi-pro", label="fastapi-pro", type="skill", tags=["python"])
        G_src.add_node("skill:docker-expert", label="docker-expert", type="skill", tags=["docker"])
        G_src.add_edge("skill:fastapi-pro", "skill:docker-expert", weight=1.0)
        data = node_link_data(G_src)
        p = _write_graph_file(tmp_path, json.dumps(data))
        G = resolve_graph.load_graph(path=p)
        assert G.number_of_nodes() == 2
        assert G.number_of_edges() == 1
