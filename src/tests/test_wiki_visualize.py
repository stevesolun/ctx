"""
test_wiki_visualize.py -- Regression tests for XSS hardening in wiki_visualize.

Covers the three Strix-validated XSS sinks:
  1. CLI-derived ``--title`` interpolated into ``<title>`` and ``<div id="title">``
  2. Untrusted graph tag names interpolated into filter-button HTML
  3. Untrusted node labels embedded in a raw ``<script>`` block via ``json.dumps``
     (``</script>`` breakout)

Tests build HTML via ``build_html_with_filters`` with attacker-controlled inputs
and assert that the payload never appears in an executable form.
"""

from __future__ import annotations

import builtins
import importlib
import json
import sys
from pathlib import Path

import networkx as nx
import pytest

_SRC = Path(__file__).resolve().parent.parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture()
def graph_with_hostile_labels() -> tuple[nx.Graph, dict]:
    G = nx.Graph()
    G.add_node(
        "skill:evil",
        label="</script><script>window.__pwn=1</script>",
        type="skill",
        tags=['"><img src=x onerror="window.__tagpwn=1">'],
    )
    G.add_node("skill:benign", label="benign", type="skill", tags=["safe"])
    G.add_edge("skill:evil", "skill:benign", weight=1)
    pos = {"skill:evil": (0.0, 0.0), "skill:benign": (1.0, 1.0)}
    return G, pos


def test_html_visualizer_imports_without_optional_plotly(monkeypatch):
    previous = sys.modules.pop("wiki_visualize", None)
    real_import = builtins.__import__

    def block_plotly(name, *args, **kwargs):
        if name == "plotly" or name.startswith("plotly."):
            raise ImportError("plotly intentionally unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", block_plotly)
    try:
        wv = importlib.import_module("wiki_visualize")
        G = nx.Graph()
        G.add_node("skill:a", label="alpha", type="skill", tags=["python"])
        pos = {"skill:a": (0.0, 0.0)}

        html = wv.build_html_with_filters(G, pos, title="Knowledge Graph")

        assert "<title>Knowledge Graph</title>" in html
        assert "alpha" in html
    finally:
        sys.modules.pop("wiki_visualize", None)
        if previous is not None:
            sys.modules["wiki_visualize"] = previous


def test_title_is_html_escaped(graph_with_hostile_labels):
    import wiki_visualize as wv

    G, pos = graph_with_hostile_labels
    payload = '<img src=x onerror="window.__title_pwn=1">'
    html = wv.build_html_with_filters(G, pos, title=payload)
    assert payload not in html, "raw title payload must not appear"
    assert "&lt;img src=x onerror=" in html, "title must be HTML-escaped"
    # onerror= may appear inside safe contexts (e.g. inside escaped strings), but
    # not as a live attribute on a real <img> tag.
    assert '<img src=x onerror="window.__title_pwn=1">' not in html


def test_tag_names_are_html_escaped(graph_with_hostile_labels):
    import wiki_visualize as wv

    G, pos = graph_with_hostile_labels
    html = wv.build_html_with_filters(G, pos, title="safe")
    # Tag name payload should be escaped in both the attribute and the element text
    assert '"><img src=x onerror="window.__tagpwn=1">' not in html
    # Escaped form must appear (& becomes &amp; after escape)
    assert "&quot;&gt;&lt;img src=x onerror=" in html


def test_script_breakout_via_node_label_is_neutralized(graph_with_hostile_labels):
    import wiki_visualize as wv

    G, pos = graph_with_hostile_labels
    html = wv.build_html_with_filters(G, pos, title="safe")
    # The literal closing tag must not survive anywhere in the embedded NODES
    # JSON, because any </script> inside a raw <script> block ends the block.
    # Count occurrences outside actual </script> close tags: there should be
    # exactly one </script> (the end of our embedded-data script).
    close_tags = html.count("</script>")
    # Page has 2 real </script> tags: one after the plotly CDN <script>, one
    # closing the inline data+render block. Any further </script> = breakout.
    assert close_tags <= 2, f"unexpected </script> sequences: {close_tags}"
    # Escaped form should appear instead
    assert r"<\/script>" in html or "&lt;/script&gt;" in html


def test_benign_render_still_works():
    import wiki_visualize as wv

    G = nx.Graph()
    G.add_node("skill:a", label="alpha", type="skill", tags=["python"])
    G.add_node("skill:b", label="beta", type="skill", tags=["python"])
    G.add_edge("skill:a", "skill:b", weight=2)
    pos = {"skill:a": (0.0, 0.0), "skill:b": (1.0, 1.0)}
    html = wv.build_html_with_filters(G, pos, title="Knowledge Graph")
    assert "<title>Knowledge Graph</title>" in html
    assert "alpha" in html and "beta" in html


def test_default_min_weight_preserves_fractional_semantic_edges():
    import wiki_visualize as wv

    G = nx.Graph()
    G.add_node("skill:a", label="alpha", type="skill", tags=[])
    G.add_node("skill:b", label="beta", type="skill", tags=[])
    G.add_edge("skill:a", "skill:b", weight=0.42)

    sub = wv.extract_subgraph(G, seeds=["alpha"], hops=1)

    assert sub.number_of_edges() == 1


def test_visualizer_renders_mcp_and_harness_type_filters():
    import wiki_visualize as wv

    G = nx.Graph()
    G.add_node("mcp-server:filesystem", label="filesystem", type="mcp-server", tags=[])
    G.add_node("harness:text-to-cad", label="text-to-cad", type="harness", tags=[])
    G.add_edge("mcp-server:filesystem", "harness:text-to-cad", weight=1)
    pos = {
        "mcp-server:filesystem": (0.0, 0.0),
        "harness:text-to-cad": (1.0, 1.0),
    }

    html = wv.build_html_with_filters(G, pos)

    assert 'data-type="mcp-server"' in html
    assert 'data-type="harness"' in html
    assert '"mcp-server": "#06b6d4"' in html
    assert '"harness": "#22c55e"' in html


def test_visualizer_can_load_explicit_graph_and_communities(tmp_path):
    import wiki_visualize as wv

    graph_path = tmp_path / "graph.json"
    communities_path = tmp_path / "communities.json"
    graph_path.write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "graph": {"export_id": "ctx-graph-test-2-1"},
                "nodes": [
                    {"id": "skill:a", "label": "alpha", "type": "skill", "tags": ["python"]},
                    {"id": "harness:b", "label": "beta", "type": "harness", "tags": ["agent"]},
                ],
                "edges": [{"source": "skill:a", "target": "harness:b", "weight": 0.9}],
            }
        ),
        encoding="utf-8",
    )
    communities_path.write_text(
        json.dumps(
            {
                "communities": {"7": {"members": ["skill:a", "harness:b"]}},
            }
        ),
        encoding="utf-8",
    )

    G = wv.load_graph(graph_path)
    communities = wv.load_communities(communities_path)

    assert G.graph["export_id"] == "ctx-graph-test-2-1"
    assert G.number_of_nodes() == 2
    assert communities["communities"]["7"]["members"] == ["skill:a", "harness:b"]


def test_visualizer_embeds_export_metadata_for_preview_freshness():
    import wiki_visualize as wv

    G = nx.Graph(export_id="ctx-graph-test-2-1")
    G.add_node("skill:a", label="alpha", type="skill", tags=["python"])
    G.add_node("harness:b", label="beta", type="harness", tags=["agent"])
    G.add_edge("skill:a", "harness:b", weight=0.9)
    pos = {"skill:a": (0.0, 0.0), "harness:b": (1.0, 1.0)}

    html = wv.build_html_with_filters(
        G,
        pos,
        title="Knowledge Graph",
        metadata={"export_id": "ctx-graph-test-2-1", "nodes": 2, "edges": 1},
    )

    assert '<meta name="ctx-graph-export-id" content="ctx-graph-test-2-1">' in html
    assert '"export_id": "ctx-graph-test-2-1"' in html
    assert '"nodes": 2' in html
    assert '"edges": 1' in html
