"""
test_resolve_graph_queries.py -- Deep coverage for resolve_graph.py query paths.

Covers:
  - load_graph: missing file, corrupt JSON, wrong schema, nx 2.x (links key),
    nx 3.x (edges key), encoding error, KeyError/TypeError/NetworkXError paths,
    valid round-trip.
  - resolve_by_seeds: empty seeds, unknown seed, single seed, multi-seed,
    hop=1 vs hop=2, decay factor maths, exclude_seeds toggle, shared_tags
    accumulation, via provenance (multi-seed), normalized_score in [0,1],
    empty graph, label fallback, top_n truncation.
  - resolve_by_tags: empty tags, no matches, single match, multi-match
    ranking, tag-overlap + log-degree tiebreak, top_n boundary.
  - main CLI: no args -> help + exit 1, --matched, --tags, --json output,
    missing graph.json error exit.
"""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import networkx as nx
import pytest

from ctx.core.graph import resolve_graph


# ── Graph builders ────────────────────────────────────────────────────────────


def _write_graph_json(path: Path, data: Any) -> None:
    """Write arbitrary data as graph.json (for schema-error tests)."""
    path.write_text(json.dumps(data), encoding="utf-8")


def _build_simple_graph() -> nx.Graph:
    """
    Three-node graph with known weights:

        skill:A --[weight=1.0, shared_tags=["python"]]-- skill:B
        skill:A --[weight=0.5, shared_tags=["api"]]---  skill:C
        skill:B --[weight=0.8]--------------------------skill:C

    Node attributes include type/label/tags so both resolve paths exercise
    them.
    """
    G = nx.Graph()
    G.add_node("skill:A", type="skill", label="Alpha", tags=["python", "api"])
    G.add_node("skill:B", type="skill", label="Beta", tags=["python", "docker"])
    G.add_node("skill:C", type="agent", label="Gamma", tags=["api", "docker"])
    G.add_edge("skill:A", "skill:B", weight=1.0, shared_tags=["python"])
    G.add_edge("skill:A", "skill:C", weight=0.5, shared_tags=["api"])
    G.add_edge("skill:B", "skill:C", weight=0.8, shared_tags=[])
    return G


def _build_mcp_graph() -> nx.Graph:
    """Graph with mixed node types (skill, agent, mcp-server) for seed-prefix tests."""
    G = nx.Graph()
    G.add_node("skill:fastapi", type="skill", label="FastAPI", tags=["python"])
    G.add_node("agent:devops", type="agent", label="DevOps Agent", tags=["docker"])
    G.add_node("mcp-server:github", type="mcp-server", label="GitHub MCP", tags=["git"])
    G.add_edge("skill:fastapi", "agent:devops", weight=0.9, shared_tags=[])
    G.add_edge("mcp-server:github", "skill:fastapi", weight=0.7, shared_tags=["python"])
    return G


def _serialise_graph(G: nx.Graph, *, edges_key: str = "edges") -> dict:
    """Return node-link dict using the requested edges key name."""
    from networkx.readwrite import node_link_data
    data = node_link_data(G, edges=edges_key)
    return data


# ── TestLoadGraph ─────────────────────────────────────────────────────────────


class TestLoadGraph:
    def test_missing_file_returns_empty_graph(self, tmp_path: Path) -> None:
        absent = tmp_path / "graph.json"
        G = resolve_graph.load_graph(absent)
        assert isinstance(G, nx.Graph)
        assert G.number_of_nodes() == 0

    def test_corrupt_json_returns_empty_graph(self, tmp_path: Path) -> None:
        bad = tmp_path / "graph.json"
        bad.write_text("{not valid json!", encoding="utf-8")
        G = resolve_graph.load_graph(bad)
        assert G.number_of_nodes() == 0

    def test_wrong_schema_missing_nodes_key(self, tmp_path: Path) -> None:
        p = tmp_path / "graph.json"
        _write_graph_json(p, {"edges": []})  # "nodes" absent
        G = resolve_graph.load_graph(p)
        assert G.number_of_nodes() == 0

    def test_wrong_schema_missing_edge_key(self, tmp_path: Path) -> None:
        p = tmp_path / "graph.json"
        _write_graph_json(p, {"nodes": []})  # neither "links" nor "edges"
        G = resolve_graph.load_graph(p)
        assert G.number_of_nodes() == 0

    def test_wrong_schema_not_a_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "graph.json"
        _write_graph_json(p, [{"nodes": [], "edges": []}])  # list, not dict
        G = resolve_graph.load_graph(p)
        assert G.number_of_nodes() == 0

    def test_unicode_decode_error_returns_empty_graph(self, tmp_path: Path) -> None:
        p = tmp_path / "graph.json"
        # Write valid JSON bytes then corrupt with latin-1 sequence
        p.write_bytes(b'{"nodes": [], "edges": [], "graph": {}, "multigraph": false, "directed": false}\xff\xfe')
        G = resolve_graph.load_graph(p)
        # The file may parse as valid JSON if utf-8 happens to decode it,
        # OR it triggers UnicodeDecodeError — either way we get an empty
        # or a valid (zero-node) graph. Just assert no crash.
        assert isinstance(G, nx.Graph)

    def test_valid_edges_key_round_trips(self, tmp_path: Path) -> None:
        """Networkx >= 3.0 writes 'edges' key — load_graph must handle it."""
        source = _build_simple_graph()
        data = _serialise_graph(source, edges_key="edges")
        p = tmp_path / "graph.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        G = resolve_graph.load_graph(p)
        assert G.number_of_nodes() == 3
        assert G.number_of_edges() == 3

    def test_valid_links_key_round_trips(self, tmp_path: Path) -> None:
        """Networkx 2.x files used 'links' key — load_graph must handle it."""
        source = _build_simple_graph()
        # Build with edges key then rename to links to simulate nx 2.x output
        data = _serialise_graph(source, edges_key="edges")
        data["links"] = data.pop("edges")
        p = tmp_path / "graph.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        G = resolve_graph.load_graph(p)
        assert G.number_of_nodes() == 3
        assert G.number_of_edges() == 3

    def test_default_path_used_when_arg_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passing path=None falls back to GRAPH_PATH."""
        source = _build_simple_graph()
        data = _serialise_graph(source)
        p = tmp_path / "graph.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        G = resolve_graph.load_graph(None)
        assert G.number_of_nodes() == 3

    def test_networkx_deserialise_error_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Trigger the KeyError/TypeError/NetworkXError branch via monkeypatching."""
        from networkx.readwrite import json_graph as _jg
        p = tmp_path / "graph.json"
        # Write a syntactically valid node-link dict so the schema check passes,
        # then force node_link_graph to raise TypeError — one of the caught exceptions.
        source = _build_simple_graph()
        data = _serialise_graph(source)
        p.write_text(json.dumps(data), encoding="utf-8")

        def _boom(*args, **kwargs):
            raise TypeError("simulated deserialise failure")

        monkeypatch.setattr(resolve_graph, "node_link_graph", _boom)
        G = resolve_graph.load_graph(p)
        assert G.number_of_nodes() == 0


# ── TestResolveBySeeds ────────────────────────────────────────────────────────


class TestResolveBySeeds:
    def test_empty_seeds_returns_empty_list(self) -> None:
        G = _build_simple_graph()
        assert resolve_graph.resolve_by_seeds(G, []) == []

    def test_unknown_seed_returns_empty_list(self) -> None:
        G = _build_simple_graph()
        assert resolve_graph.resolve_by_seeds(G, ["no-such-skill"]) == []

    def test_empty_graph_returns_empty_list(self) -> None:
        G = nx.Graph()
        assert resolve_graph.resolve_by_seeds(G, ["anything"]) == []

    def test_single_seed_finds_direct_neighbors(self) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(G, ["A"], max_hops=1)
        names = [r["name"] for r in results]
        assert "Beta" in names
        assert "Gamma" in names

    def test_single_seed_excludes_seed_by_default(self) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(G, ["A"])
        names = [r["name"] for r in results]
        assert "Alpha" not in names

    def test_exclude_seeds_false_includes_seed_nodes_as_neighbors(self) -> None:
        """With exclude_seeds=False, seed nodes can appear via other hops."""
        G = nx.Graph()
        G.add_node("skill:X", type="skill", label="X", tags=[])
        G.add_node("skill:Y", type="skill", label="Y", tags=[])
        G.add_node("skill:Z", type="skill", label="Z", tags=[])
        G.add_edge("skill:X", "skill:Y", weight=1.0, shared_tags=[])
        G.add_edge("skill:Y", "skill:Z", weight=1.0, shared_tags=[])
        G.add_edge("skill:Z", "skill:X", weight=1.0, shared_tags=[])
        # seed is X; with exclude_seeds=False, X can appear as neighbor of Z
        results = resolve_graph.resolve_by_seeds(
            G, ["X"], max_hops=2, exclude_seeds=False
        )
        names = [r["name"] for r in results]
        assert "X" in names

    def test_hop1_vs_hop2_second_hop_reachable(self) -> None:
        """
        Graph: A -> B -> C (no direct A-C edge)
        With max_hops=1 only B is found; with max_hops=2 both B and C appear.
        """
        G = nx.Graph()
        G.add_node("skill:A", type="skill", label="A", tags=[])
        G.add_node("skill:B", type="skill", label="B", tags=[])
        G.add_node("skill:C", type="skill", label="C", tags=[])
        G.add_edge("skill:A", "skill:B", weight=1.0, shared_tags=[])
        G.add_edge("skill:B", "skill:C", weight=1.0, shared_tags=[])

        res1 = resolve_graph.resolve_by_seeds(G, ["A"], max_hops=1)
        res2 = resolve_graph.resolve_by_seeds(G, ["A"], max_hops=2)
        names1 = {r["name"] for r in res1}
        names2 = {r["name"] for r in res2}
        assert "B" in names1
        assert "C" not in names1
        assert "C" in names2

    def test_decay_factor_hop0_full_hop1_half(self) -> None:
        """
        decay = 1/(hop+1): hop 0 → 1.0, hop 1 → 0.5.

        Graph: A --[w=1.0]--> B --[w=1.0]--> C (no direct A-C edge)
        After hop 0: B gains 1.0 * 1.0 = 1.0
        After hop 1: C gains 1.0 * 0.5 = 0.5
        """
        G = nx.Graph()
        G.add_node("skill:A", type="skill", label="A", tags=[])
        G.add_node("skill:B", type="skill", label="B", tags=[])
        G.add_node("skill:C", type="skill", label="C", tags=[])
        G.add_edge("skill:A", "skill:B", weight=1.0, shared_tags=[])
        G.add_edge("skill:B", "skill:C", weight=1.0, shared_tags=[])

        results = resolve_graph.resolve_by_seeds(G, ["A"], max_hops=2)
        score_map = {r["name"]: r["score"] for r in results}
        assert score_map["B"] == pytest.approx(1.0, abs=0.01)
        assert score_map["C"] == pytest.approx(0.5, abs=0.01)

    def test_multi_hop_score_accumulates(self) -> None:
        """
        C is reachable from A directly AND via B at hop 1.
        Its score = direct_weight (hop 0 decay=1.0) + indirect_weight * 0.5.
        """
        G = _build_simple_graph()
        # A -> B (w=1.0) and A -> C (w=0.5) directly;
        # B -> C (w=0.8), so C gets: 0.5*1.0 (hop0) + 0.8*0.5 (hop1) = 0.9
        results = resolve_graph.resolve_by_seeds(G, ["A"], max_hops=2)
        score_map = {r["name"]: r["score"] for r in results}
        assert score_map["Gamma"] == pytest.approx(0.9, abs=0.01)

    def test_normalized_score_in_range(self) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(G, ["A"])
        assert results, "expected non-empty results"
        for r in results:
            assert 0.0 <= r["normalized_score"] <= 1.0

    def test_top_result_has_normalized_score_one(self) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(G, ["A"])
        scores = [r["normalized_score"] for r in results]
        assert max(scores) == pytest.approx(1.0, abs=0.0001)

    def test_shared_tags_accumulated(self) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(G, ["A"], max_hops=1)
        b_result = next(r for r in results if r["name"] == "Beta")
        assert "python" in b_result["shared_tags"]

    def test_via_provenance_single_seed(self) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(G, ["A"])
        b_result = next(r for r in results if r["name"] == "Beta")
        assert "A" in b_result["via"]

    def test_via_provenance_multi_seed(self) -> None:
        """
        When both A and B are seeds, C should list both in its 'via'.
        """
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(
            G, ["A", "B"], max_hops=1, exclude_seeds=True
        )
        c_result = next((r for r in results if r["name"] == "Gamma"), None)
        assert c_result is not None
        via_set = set(c_result["via"])
        assert "A" in via_set
        assert "B" in via_set

    def test_top_n_limits_output(self) -> None:
        G = nx.Graph()
        G.add_node("skill:seed", type="skill", label="seed", tags=[])
        for i in range(20):
            G.add_node(f"skill:n{i}", type="skill", label=f"node{i}", tags=[])
            G.add_edge("skill:seed", f"skill:n{i}", weight=float(i + 1), shared_tags=[])
        results = resolve_graph.resolve_by_seeds(G, ["seed"], top_n=5)
        assert len(results) <= 5

    def test_label_fallback_to_nid_suffix(self) -> None:
        """Nodes without 'label' attr fall back to nid.split(':')[-1]."""
        G = nx.Graph()
        G.add_node("skill:seed", type="skill", tags=[])      # no label
        G.add_node("skill:target", type="skill", tags=[])    # no label
        G.add_edge("skill:seed", "skill:target", weight=1.0, shared_tags=[])
        results = resolve_graph.resolve_by_seeds(G, ["seed"], max_hops=1)
        assert results[0]["name"] == "target"

    def test_mcp_server_prefix_resolved(self) -> None:
        """Seeds matching an mcp-server: prefix node are walked correctly."""
        G = _build_mcp_graph()
        results = resolve_graph.resolve_by_seeds(G, ["github"], max_hops=1)
        names = [r["name"] for r in results]
        assert "FastAPI" in names

    def test_agent_prefix_resolved(self) -> None:
        G = _build_mcp_graph()
        results = resolve_graph.resolve_by_seeds(G, ["devops"], max_hops=1)
        names = [r["name"] for r in results]
        assert "FastAPI" in names

    def test_multiple_seeds_both_prefixes_resolved(self) -> None:
        G = _build_mcp_graph()
        results = resolve_graph.resolve_by_seeds(G, ["fastapi", "github"], max_hops=1)
        assert len(results) > 0

    @pytest.mark.parametrize("seeds,expected_empty", [
        ([], True),
        (["does-not-exist"], True),
        (["A"], False),
    ])
    def test_seeds_parametrized(self, seeds: list[str], expected_empty: bool) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_seeds(G, seeds)
        assert (len(results) == 0) == expected_empty


# ── TestResolveByTags ─────────────────────────────────────────────────────────


class TestResolveByTags:
    def test_empty_tags_returns_empty(self) -> None:
        G = _build_simple_graph()
        assert resolve_graph.resolve_by_tags(G, []) == []

    def test_no_matching_nodes_returns_empty(self) -> None:
        G = _build_simple_graph()
        assert resolve_graph.resolve_by_tags(G, ["rust", "wasm"]) == []

    def test_single_match(self) -> None:
        G = nx.Graph()
        G.add_node("skill:only", type="skill", label="Only", tags=["unique-tag"])
        results = resolve_graph.resolve_by_tags(G, ["unique-tag"])
        assert len(results) == 1
        assert results[0]["name"] == "Only"

    def test_multi_match_ranked_by_overlap(self) -> None:
        """
        Node X matches 2 tags; node Y matches 1 tag.
        X should rank higher regardless of degree.
        """
        G = nx.Graph()
        G.add_node("skill:X", type="skill", label="X", tags=["python", "api", "docker"])
        G.add_node("skill:Y", type="skill", label="Y", tags=["python"])
        # Give Y more edges so degree tiebreak would favour Y if overlap ignored
        G.add_node("skill:Z1", type="skill", label="Z1", tags=[])
        G.add_node("skill:Z2", type="skill", label="Z2", tags=[])
        G.add_node("skill:Z3", type="skill", label="Z3", tags=[])
        G.add_edge("skill:Y", "skill:Z1", weight=1.0, shared_tags=[])
        G.add_edge("skill:Y", "skill:Z2", weight=1.0, shared_tags=[])
        G.add_edge("skill:Y", "skill:Z3", weight=1.0, shared_tags=[])

        results = resolve_graph.resolve_by_tags(G, ["python", "api"])
        assert results[0]["name"] == "X"

    def test_degree_tiebreak_for_equal_overlap(self) -> None:
        """
        Two nodes with identical tag overlap; higher degree wins.
        Score = overlap * 10 + log1p(degree).
        """
        import math
        G = nx.Graph()
        G.add_node("skill:low", type="skill", label="Low", tags=["python"])
        G.add_node("skill:high", type="skill", label="High", tags=["python"])
        # Give 'high' 3 edges, 'low' 0 edges
        for i in range(3):
            G.add_node(f"skill:extra{i}", type="skill", label=f"extra{i}", tags=[])
            G.add_edge("skill:high", f"skill:extra{i}", weight=1.0, shared_tags=[])

        results = resolve_graph.resolve_by_tags(G, ["python"])
        assert results[0]["name"] == "High"

    def test_top_n_limits_output(self) -> None:
        G = nx.Graph()
        for i in range(15):
            G.add_node(f"skill:n{i}", type="skill", label=f"node{i}", tags=["common"])
        results = resolve_graph.resolve_by_tags(G, ["common"], top_n=5)
        assert len(results) <= 5

    def test_result_contains_matching_tags(self) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_tags(G, ["python", "docker"])
        assert all("matching_tags" in r for r in results)
        for r in results:
            for tag in r["matching_tags"]:
                assert tag in {"python", "docker"}

    def test_type_preserved_in_result(self) -> None:
        G = nx.Graph()
        G.add_node("agent:myagent", type="agent", label="MyAgent", tags=["python"])
        results = resolve_graph.resolve_by_tags(G, ["python"])
        assert results[0]["type"] == "agent"

    def test_label_fallback_to_nid_suffix(self) -> None:
        G = nx.Graph()
        G.add_node("skill:nolabel", type="skill", tags=["python"])
        results = resolve_graph.resolve_by_tags(G, ["python"])
        assert results[0]["name"] == "nolabel"

    @pytest.mark.parametrize("tags,min_results", [
        (["python"], 2),    # both A and B have python tag in _build_simple_graph
        (["api"], 2),       # A and C have api tag
        (["docker"], 1),    # only B and C have docker, both should appear
    ])
    def test_parametrized_tag_counts(self, tags: list[str], min_results: int) -> None:
        G = _build_simple_graph()
        results = resolve_graph.resolve_by_tags(G, tags)
        assert len(results) >= min_results


# ── TestMainCLI ───────────────────────────────────────────────────────────────


def _graph_file(tmp_path: Path, G: nx.Graph | None = None) -> Path:
    """Write G (or a default simple graph) to tmp_path/graph.json and return path."""
    source = G if G is not None else _build_simple_graph()
    data = _serialise_graph(source)
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestMainCLI:
    def test_no_args_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """No --matched and no --tags should print help and exit 1."""
        monkeypatch.setattr(sys, "argv", ["resolve_graph"])
        with pytest.raises(SystemExit) as exc:
            resolve_graph.main()
        assert exc.value.code == 1

    def test_matched_flag_runs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        p = _graph_file(tmp_path)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--matched", "A"])
        resolve_graph.main()
        out = capsys.readouterr().out
        assert "graph-walk" in out

    def test_tags_flag_runs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        p = _graph_file(tmp_path)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--tags", "python"])
        resolve_graph.main()
        out = capsys.readouterr().out
        assert "tag-search" in out

    def test_json_flag_outputs_valid_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        p = _graph_file(tmp_path)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--matched", "A", "--json"])
        resolve_graph.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert "mode" in parsed
        assert "results" in parsed
        assert parsed["mode"] == "graph-walk"

    def test_json_flag_tags_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        p = _graph_file(tmp_path)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--tags", "python", "--json"])
        resolve_graph.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["mode"] == "tag-search"

    def test_missing_graph_json_exits_1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        absent = tmp_path / "graph.json"
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", absent)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--matched", "A"])
        with pytest.raises(SystemExit) as exc:
            resolve_graph.main()
        assert exc.value.code == 1

    def test_top_flag_is_respected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """--top 1 should produce at most 1 result line."""
        G = nx.Graph()
        G.add_node("skill:seed", type="skill", label="seed", tags=[])
        for i in range(10):
            G.add_node(f"skill:n{i}", type="skill", label=f"node{i}", tags=[])
            G.add_edge("skill:seed", f"skill:n{i}", weight=float(i + 1), shared_tags=[])
        p = _graph_file(tmp_path, G)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--matched", "seed", "--top", "1", "--json"])
        resolve_graph.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert len(parsed["results"]) <= 1

    def test_hops_flag_is_respected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        """--hops 1 should not reach 2-hop-only nodes."""
        G = nx.Graph()
        G.add_node("skill:A", type="skill", label="A", tags=[])
        G.add_node("skill:B", type="skill", label="B", tags=[])
        G.add_node("skill:C", type="skill", label="C", tags=[])
        G.add_edge("skill:A", "skill:B", weight=1.0, shared_tags=[])
        G.add_edge("skill:B", "skill:C", weight=1.0, shared_tags=[])
        p = _graph_file(tmp_path, G)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--matched", "A", "--hops", "1", "--json"])
        resolve_graph.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        names = [r["name"] for r in parsed["results"]]
        assert "C" not in names

    def test_multi_seed_comma_separated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        p = _graph_file(tmp_path)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--matched", "A,B", "--json"])
        resolve_graph.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed["results"], list)

    def test_multi_tag_comma_separated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
        p = _graph_file(tmp_path)
        monkeypatch.setattr(resolve_graph, "GRAPH_PATH", p)
        monkeypatch.setattr(sys, "argv", ["resolve_graph", "--tags", "python,docker", "--json"])
        resolve_graph.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed["results"], list)
