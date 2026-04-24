"""
test_patch_graph.py -- Coverage for wiki_graphify.patch_graph.

patch_graph is the incremental-update core: given a prior graph + a
set of affected nodes + target edges, it mutates the prior graph
in-place so it matches the new target state while preserving edges
between unaffected node pairs.

A bug here produces silent gaps (missing edges), silent stale
edges (wrong weights lingering), or full-rebuild performance
regressions.
"""

from __future__ import annotations

from typing import Any

import networkx as nx
import pytest

from ctx.core.wiki.wiki_graphify import patch_graph


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_prior(
    nodes: list[tuple[str, dict[str, Any]]] | None = None,
    edges: list[tuple[str, str, dict[str, Any]]] | None = None,
) -> nx.Graph:
    G = nx.Graph()
    for nid, attrs in (nodes or []):
        G.add_node(nid, **attrs)
    for u, v, attrs in (edges or []):
        G.add_edge(u, v, **attrs)
    return G


def _info(label: str, type_: str = "skill", tags: list[str] | None = None) -> dict:
    return {"label": label, "type": type_, "tags": list(tags or [])}


# ── Node-level delta ─────────────────────────────────────────────────────────


class TestNodeDelta:
    def test_new_node_added_with_attrs(self) -> None:
        prior = _make_prior()
        current = {"skill:a": _info("a", "skill", ["py"])}
        patch_graph(
            prior,
            current_node_info=current,
            target_edges={},
            affected_node_ids=set(),
        )
        assert "skill:a" in prior
        assert prior.nodes["skill:a"]["label"] == "a"
        assert prior.nodes["skill:a"]["type"] == "skill"
        assert prior.nodes["skill:a"]["tags"] == ["py"]

    def test_removed_node_dropped(self) -> None:
        prior = _make_prior([("skill:gone", _info("gone"))])
        patch_graph(
            prior,
            current_node_info={},
            target_edges={},
            affected_node_ids=set(),
        )
        assert "skill:gone" not in prior

    def test_existing_node_attrs_refreshed(self) -> None:
        """Tags may have changed between runs — patch must refresh."""
        prior = _make_prior(
            [("skill:a", {"label": "a", "type": "skill", "tags": ["old"]})]
        )
        current = {"skill:a": _info("a", "skill", ["new"])}
        patch_graph(
            prior,
            current_node_info=current,
            target_edges={},
            affected_node_ids=set(),
        )
        assert prior.nodes["skill:a"]["tags"] == ["new"]

    def test_label_defaults_from_nid_suffix(self) -> None:
        prior = _make_prior()
        patch_graph(
            prior,
            current_node_info={"skill:fastapi-pro": {}},
            target_edges={},
            affected_node_ids=set(),
        )
        assert prior.nodes["skill:fastapi-pro"]["label"] == "fastapi-pro"

    def test_type_preserved_when_info_omits_it(self) -> None:
        """Refresh on existing node should preserve the prior type if new info has none."""
        prior = _make_prior(
            [("skill:a", {"label": "a", "type": "skill", "tags": []})]
        )
        patch_graph(
            prior,
            current_node_info={"skill:a": {"label": "a", "tags": []}},
            target_edges={},
            affected_node_ids=set(),
        )
        assert prior.nodes["skill:a"]["type"] == "skill"

    def test_tags_stored_as_fresh_list(self) -> None:
        """Mutating the info afterwards must not leak into the graph."""
        tags = ["py"]
        prior = _make_prior()
        patch_graph(
            prior,
            current_node_info={"skill:a": _info("a", "skill", tags)},
            target_edges={},
            affected_node_ids=set(),
        )
        tags.append("leak")
        assert "leak" not in prior.nodes["skill:a"]["tags"]


# ── Edge-level delta ─────────────────────────────────────────────────────────


class TestEdgeDelta:
    def test_edges_between_unaffected_pairs_preserved(self) -> None:
        """The core invariant: unaffected pairs keep their prior edges untouched."""
        prior = _make_prior(
            [("skill:a", _info("a")), ("skill:b", _info("b"))],
            [("skill:a", "skill:b", {"weight": 0.8, "semantic_sim": 0.8})],
        )
        current = {
            "skill:a": _info("a"),
            "skill:b": _info("b"),
        }
        patch_graph(
            prior,
            current_node_info=current,
            target_edges={},
            affected_node_ids=set(),
        )
        # Both nodes unaffected → edge should survive.
        assert prior.has_edge("skill:a", "skill:b")
        assert prior["skill:a"]["skill:b"]["weight"] == 0.8

    def test_edge_incident_on_affected_node_refreshed(self) -> None:
        prior = _make_prior(
            [("skill:a", _info("a")), ("skill:b", _info("b"))],
            [("skill:a", "skill:b", {"weight": 0.8})],
        )
        current = {
            "skill:a": _info("a"),
            "skill:b": _info("b"),
        }
        target = {("skill:a", "skill:b"): {"weight": 0.5, "semantic_sim": 0.5}}
        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids={"skill:a"},
        )
        assert prior["skill:a"]["skill:b"]["weight"] == 0.5
        assert prior["skill:a"]["skill:b"]["semantic_sim"] == 0.5

    def test_edge_to_removed_node_disappears(self) -> None:
        prior = _make_prior(
            [("skill:a", _info("a")), ("skill:gone", _info("gone"))],
            [("skill:a", "skill:gone", {"weight": 0.5})],
        )
        patch_graph(
            prior,
            current_node_info={"skill:a": _info("a")},
            target_edges={},
            affected_node_ids=set(),
        )
        # Removing node also cleans up its edges (NetworkX semantics).
        assert not prior.has_edge("skill:a", "skill:gone")
        assert "skill:gone" not in prior

    def test_new_edge_between_new_nodes(self) -> None:
        prior = _make_prior()
        current = {
            "skill:a": _info("a"),
            "skill:b": _info("b"),
        }
        target = {("skill:a", "skill:b"): {"weight": 0.7}}
        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids=set(),
        )
        assert prior.has_edge("skill:a", "skill:b")
        assert prior["skill:a"]["skill:b"]["weight"] == 0.7

    def test_target_edge_with_missing_endpoint_skipped(self) -> None:
        """Target edges referencing a node absent from current_node_info must not materialize."""
        prior = _make_prior()
        current = {"skill:a": _info("a")}
        target = {("skill:a", "skill:ghost"): {"weight": 0.5}}
        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids={"skill:a"},
        )
        assert not prior.has_edge("skill:a", "skill:ghost")

    def test_stale_edge_on_affected_node_removed(self) -> None:
        """Affected node loses all incident edges that aren't re-added by target_edges."""
        prior = _make_prior(
            [
                ("skill:a", _info("a")),
                ("skill:b", _info("b")),
                ("skill:c", _info("c")),
            ],
            [
                ("skill:a", "skill:b", {"weight": 0.5}),
                ("skill:a", "skill:c", {"weight": 0.5}),
            ],
        )
        current = {
            "skill:a": _info("a"),
            "skill:b": _info("b"),
            "skill:c": _info("c"),
        }
        # Only a-b is in the new target; a-c should be dropped.
        target = {("skill:a", "skill:b"): {"weight": 0.6}}
        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids={"skill:a"},
        )
        assert prior.has_edge("skill:a", "skill:b")
        assert prior["skill:a"]["skill:b"]["weight"] == 0.6
        assert not prior.has_edge("skill:a", "skill:c")


# ── Affected set semantics ───────────────────────────────────────────────────


class TestAffectedSet:
    def test_affected_union_new_nodes(self) -> None:
        """New nodes join the affected set implicitly — their edges must be evaluated."""
        prior = _make_prior(
            [("skill:a", _info("a"))],
            [],
        )
        current = {
            "skill:a": _info("a"),
            "skill:b": _info("b"),
        }
        # Only b is new; caller did NOT include it in affected_node_ids.
        target = {("skill:a", "skill:b"): {"weight": 0.9}}
        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids=set(),  # empty
        )
        # Still materializes because new nodes are auto-affected.
        assert prior.has_edge("skill:a", "skill:b")

    def test_affected_union_removed_nodes(self) -> None:
        """Removed nodes' edges are dropped even when caller omits them from affected."""
        prior = _make_prior(
            [("skill:a", _info("a")), ("skill:gone", _info("gone"))],
            [("skill:a", "skill:gone", {"weight": 0.5})],
        )
        patch_graph(
            prior,
            current_node_info={"skill:a": _info("a")},
            target_edges={},
            affected_node_ids=set(),  # empty — but gone is removed
        )
        assert "skill:gone" not in prior

    def test_affected_node_not_in_prior_tolerated(self) -> None:
        """Affected set may list a node not yet in prior — patch should not crash."""
        prior = _make_prior()
        patch_graph(
            prior,
            current_node_info={"skill:a": _info("a")},
            target_edges={},
            affected_node_ids={"skill:a", "skill:does-not-exist-yet"},
        )
        assert "skill:a" in prior
        assert "skill:does-not-exist-yet" not in prior


# ── Return value ─────────────────────────────────────────────────────────────


class TestReturnValue:
    def test_returns_same_instance(self) -> None:
        """Caller expects the same graph object they passed in."""
        prior = _make_prior()
        out = patch_graph(
            prior,
            current_node_info={},
            target_edges={},
            affected_node_ids=set(),
        )
        assert out is prior


# ── Integration: full cycle ─────────────────────────────────────────────────


class TestFullCycle:
    def test_no_op_when_nothing_changes(self) -> None:
        """Idempotent: same inputs twice should leave the graph identical."""
        prior = _make_prior(
            [("skill:a", _info("a", tags=["py"])),
             ("skill:b", _info("b", tags=["py"]))],
            [("skill:a", "skill:b", {"weight": 0.5})],
        )
        current = {
            "skill:a": _info("a", tags=["py"]),
            "skill:b": _info("b", tags=["py"]),
        }
        target = {("skill:a", "skill:b"): {"weight": 0.5}}

        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids=set(),
        )
        snapshot = (
            set(prior.nodes()),
            sorted(prior.edges()),
            prior["skill:a"]["skill:b"]["weight"],
        )
        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids=set(),
        )
        assert (
            set(prior.nodes()),
            sorted(prior.edges()),
            prior["skill:a"]["skill:b"]["weight"],
        ) == snapshot

    def test_prints_summary(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Operational summary line must be emitted."""
        prior = _make_prior()
        patch_graph(
            prior,
            current_node_info={"skill:a": _info("a")},
            target_edges={},
            affected_node_ids=set(),
        )
        out = capsys.readouterr().out
        assert "patch_graph:" in out
        assert "added=1 nodes" in out

    def test_combined_add_remove_update(self) -> None:
        """Stress: one node removed, one added, one updated, edges refreshed."""
        prior = _make_prior(
            [
                ("skill:a", _info("a", tags=["old"])),
                ("skill:b", _info("b")),
                ("skill:gone", _info("gone")),
            ],
            [
                ("skill:a", "skill:b", {"weight": 0.3}),
                ("skill:a", "skill:gone", {"weight": 0.4}),
            ],
        )
        current = {
            "skill:a": _info("a", tags=["fresh"]),
            "skill:b": _info("b"),
            "skill:new": _info("new"),
        }
        target = {
            ("skill:a", "skill:b"): {"weight": 0.7},
            ("skill:b", "skill:new"): {"weight": 0.6},
        }
        patch_graph(
            prior,
            current_node_info=current,
            target_edges=target,
            affected_node_ids={"skill:a"},
        )
        assert "skill:gone" not in prior
        assert "skill:new" in prior
        assert prior.nodes["skill:a"]["tags"] == ["fresh"]
        assert prior["skill:a"]["skill:b"]["weight"] == 0.7
        assert prior.has_edge("skill:b", "skill:new")
        assert prior["skill:b"]["skill:new"]["weight"] == 0.6
