"""
test_wiki_graphify_security.py -- Security regression tests for wiki_graphify.

Covers the CRITICAL security-auditor finding (C-1): pickle.loads on
graphify-out/graph.pickle is an RCE primitive. Any process that can write
to graphify-out/ gets code execution under the user's UID on the next
regraphify.

The fix removes pickle entirely — load_prior_graph reads the existing
graph.json artifact via nx.node_link_graph. JSON loading has no code-
execution path. These tests pin that behavior so a future revert to
pickle fails loudly.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import networkx as nx
import pytest


# ────────────────────────────────────────────────────────────────────
# Shared fixtures — a graphify-out dir with a known good graph.json
# ────────────────────────────────────────────────────────────────────


@pytest.fixture()
def graphify_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point wiki_graphify.GRAPH_OUT at a temp dir.

    Every test that touches the load/export paths needs the module-level
    constant overridden so tests don't clobber the real wiki.
    """
    from ctx.core.wiki import wiki_graphify

    out = tmp_path / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(wiki_graphify, "GRAPH_OUT", out)
    return out


def _make_sample_graph() -> nx.Graph:
    """A small networkx graph with the same shape wiki_graphify produces."""
    G = nx.Graph()
    G.graph["semantic_build_floor"] = 0.5
    G.graph["semantic_min_cosine_default"] = 0.8
    G.add_node("skill:a", label="a", type="skill", tags=["python"])
    G.add_node("skill:b", label="b", type="skill", tags=["python"])
    G.add_node("mcp-server:c", label="c", type="mcp-server", tags=["official"])
    G.add_edge(
        "skill:a", "skill:b",
        semantic_sim=0.9, tag_sim=0.4, token_sim=0.0,
        final_weight=0.72, weight=0.72,
        shared_tags=["python"], shared_tokens=[],
    )
    G.add_edge(
        "skill:a", "mcp-server:c",
        semantic_sim=0.6, tag_sim=0.0, token_sim=0.0,
        final_weight=0.42, weight=0.42,
        shared_tags=[], shared_tokens=[],
    )
    return G


def _write_graph_json(out: Path, G: nx.Graph) -> Path:
    """Serialise *G* the same way export_graph does."""
    data = nx.node_link_data(G, edges="edges")
    path = out / "graph.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return path


# ────────────────────────────────────────────────────────────────────
# CRITICAL-C1: the pickle RCE primitive must be gone
# ────────────────────────────────────────────────────────────────────


class _PickleRCESentinel:
    """A pickle payload that would run arbitrary code during loads()."""

    def __reduce__(self):
        # On unpickle, this touches a sentinel file. A safe load MUST
        # NOT invoke __reduce__ — which means the pickle path must not
        # be read at all.
        marker = Path(_PickleRCESentinel._marker_path())  # type: ignore[attr-defined]
        return (_touch_file, (str(marker),))

    @classmethod
    def _marker_path(cls) -> str:
        return cls._marker  # type: ignore[attr-defined]


def _touch_file(path: str) -> bool:
    Path(path).write_text("pwned", encoding="utf-8")
    return True


def test_load_prior_graph_does_not_execute_pickle_payload(
    graphify_out: Path, tmp_path: Path,
) -> None:
    """Writing a malicious pickle next to graph.json must NOT execute it.

    This is the pinned regression for security-auditor finding C-1.
    If load_prior_graph ever starts reading graph.pickle again, this
    sentinel file will be created and the test fails.
    """
    from ctx.core.wiki import wiki_graphify

    marker = tmp_path / "rce-executed.sentinel"
    _PickleRCESentinel._marker = str(marker)  # type: ignore[attr-defined]

    # Write the RCE payload where the old code used to look.
    payload = pickle.dumps(_PickleRCESentinel(), protocol=pickle.HIGHEST_PROTOCOL)
    (graphify_out / "graph.pickle").write_bytes(payload)

    # Legitimate graph.json alongside — load_prior_graph must prefer it
    # (or return None if the JSON is missing; either way, no RCE).
    _write_graph_json(graphify_out, _make_sample_graph())

    result = wiki_graphify.load_prior_graph()

    assert not marker.is_file(), (
        "PICKLE RCE REGRESSED: load_prior_graph executed a pickle __reduce__"
    )
    # Valid JSON alongside → result should be the JSON-derived graph.
    assert result is not None
    assert result.number_of_nodes() == 3


def test_load_prior_graph_ignores_pickle_when_only_pickle_exists(
    graphify_out: Path, tmp_path: Path,
) -> None:
    """Even without a graph.json, a stray pickle must not be loaded.

    Before the fix, load_prior_graph fell back to graph.pickle directly.
    After the fix, graph.pickle is never read — the absence of graph.json
    yields None (triggers a full rebuild) rather than executing pickle.
    """
    from ctx.core.wiki import wiki_graphify

    marker = tmp_path / "rce-pickle-only.sentinel"
    _PickleRCESentinel._marker = str(marker)  # type: ignore[attr-defined]

    (graphify_out / "graph.pickle").write_bytes(
        pickle.dumps(_PickleRCESentinel(), protocol=pickle.HIGHEST_PROTOCOL)
    )

    result = wiki_graphify.load_prior_graph()

    assert not marker.is_file(), "pickle was loaded despite being the only artifact"
    assert result is None


# ────────────────────────────────────────────────────────────────────
# Happy-path: JSON round-trip preserves nodes, edges, attrs, graph-meta
# ────────────────────────────────────────────────────────────────────


def test_load_prior_graph_roundtrip_preserves_nodes_and_edges(
    graphify_out: Path,
) -> None:
    """A graph exported to JSON and loaded back must be structurally equal."""
    from ctx.core.wiki import wiki_graphify

    original = _make_sample_graph()
    _write_graph_json(graphify_out, original)

    loaded = wiki_graphify.load_prior_graph()
    assert loaded is not None
    assert loaded.number_of_nodes() == original.number_of_nodes()
    assert loaded.number_of_edges() == original.number_of_edges()
    assert set(loaded.nodes()) == set(original.nodes())


def test_load_prior_graph_roundtrip_preserves_edge_attrs(
    graphify_out: Path,
) -> None:
    """Edge attributes (semantic_sim, tag_sim, etc.) survive JSON round-trip."""
    from ctx.core.wiki import wiki_graphify

    original = _make_sample_graph()
    _write_graph_json(graphify_out, original)

    loaded = wiki_graphify.load_prior_graph()
    assert loaded is not None

    attrs = loaded["skill:a"]["skill:b"]
    assert attrs["semantic_sim"] == pytest.approx(0.9)
    assert attrs["tag_sim"] == pytest.approx(0.4)
    assert attrs["token_sim"] == pytest.approx(0.0)
    assert attrs["final_weight"] == pytest.approx(0.72)
    assert attrs["shared_tags"] == ["python"]


def test_load_prior_graph_roundtrip_preserves_graph_level_metadata(
    graphify_out: Path,
) -> None:
    """Graph-level attrs (build_floor, min_cosine_default) survive the round-trip.

    The semantic_build_floor is used by filter_graph_by_min_cosine to refuse
    below-floor requests; if it got dropped during serialisation the filter
    would silently accept invalid thresholds.
    """
    from ctx.core.wiki import wiki_graphify

    original = _make_sample_graph()
    _write_graph_json(graphify_out, original)

    loaded = wiki_graphify.load_prior_graph()
    assert loaded is not None
    assert loaded.graph.get("semantic_build_floor") == pytest.approx(0.5)
    assert loaded.graph.get("semantic_min_cosine_default") == pytest.approx(0.8)


# ────────────────────────────────────────────────────────────────────
# Robustness: corrupt / malformed / missing JSON must not crash
# ────────────────────────────────────────────────────────────────────


def test_load_prior_graph_returns_none_on_missing_json(graphify_out: Path) -> None:
    from ctx.core.wiki import wiki_graphify

    # graphify_out is empty — no graph.json
    assert wiki_graphify.load_prior_graph() is None


def test_load_prior_graph_returns_none_on_malformed_json(graphify_out: Path) -> None:
    from ctx.core.wiki import wiki_graphify

    (graphify_out / "graph.json").write_text("not { valid json", encoding="utf-8")
    assert wiki_graphify.load_prior_graph() is None


def test_load_prior_graph_returns_none_on_wrong_schema(graphify_out: Path) -> None:
    """Valid JSON that doesn't match the node-link schema returns None.

    Pre-fix, an attacker could craft a JSON file that passed json.loads
    but confused a downstream consumer. Post-fix, the shape check rejects
    anything that isn't a networkx node-link document.
    """
    from ctx.core.wiki import wiki_graphify

    (graphify_out / "graph.json").write_text(
        json.dumps({"not_a_graph": "nope"}), encoding="utf-8",
    )
    assert wiki_graphify.load_prior_graph() is None


# ────────────────────────────────────────────────────────────────────
# The export path no longer writes graph.pickle
# ────────────────────────────────────────────────────────────────────


def test_export_graph_does_not_write_pickle(graphify_out: Path) -> None:
    """export_graph must stop writing graph.pickle — no future RCE primitive."""
    from ctx.core.wiki import wiki_graphify

    G = _make_sample_graph()
    wiki_graphify.export_graph(G, communities={})

    assert (graphify_out / "graph.json").is_file()
    assert not (graphify_out / "graph.pickle").exists(), (
        "export_graph wrote graph.pickle — re-introducing the RCE vector"
    )
