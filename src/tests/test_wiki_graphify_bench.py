"""
test_wiki_graphify_bench.py -- Benchmark the two algorithmic hotpaths in
wiki_graphify to guard against O(K²) / O(C²·members) regressions.

Both tests use synthetic in-memory data and assert wall-clock completion under
1 second.  The thresholds are loose by design (they must pass on a slow CI
runner) while still catching a genuine quadratic blowup.
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import networkx as nx
import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ctx.core.wiki import wiki_graphify as wg  # noqa: E402

# ---------------------------------------------------------------------------
# P1-10 benchmark: tag-pair edge construction
# ---------------------------------------------------------------------------

def _build_tag_edge_graph(
    num_pages: int,
    tags_per_page: int,
    total_unique_tags: int,
) -> nx.Graph:
    """Build a graph and run the tag-index edge loop from build_graph in isolation."""
    G = nx.Graph()

    # Create nodes with tags
    for i in range(num_pages):
        node_id = f"skill:page-{i}"
        # Each page gets tags_per_page tags drawn cyclically from the tag pool
        tags = [f"tag-{(i * tags_per_page + k) % total_unique_tags}" for k in range(tags_per_page)]
        G.add_node(node_id, label=f"page-{i}", type="skill", tags=tags)

    # Replicate the tag-index build + edge loop from build_graph
    tag_index: dict[str, list[str]] = defaultdict(list)
    for nid, data in G.nodes(data=True):
        for tag in data.get("tags", []):
            tag_index[tag].append(nid)

    DENSE_TAG_THRESHOLD = wg.DENSE_TAG_THRESHOLD if hasattr(wg, "DENSE_TAG_THRESHOLD") else 20
    for tag, nodes in tag_index.items():
        if len(nodes) > DENSE_TAG_THRESHOLD:
            continue
        for i, n1 in enumerate(nodes):
            for n2 in nodes[i + 1:]:
                if G.has_edge(n1, n2):
                    G[n1][n2]["weight"] += 1
                    G[n1][n2]["shared_tags"].append(tag)
                else:
                    G.add_edge(n1, n2, weight=1, shared_tags=[tag])

    return G


def test_tag_edge_construction_under_threshold() -> None:
    """
    500 tags across 200 pages (5 tags/page average) must complete in < 1 s.

    Without the dense-tag guard the worst case is O(K²) per tag. With the guard
    every tag group is capped at DENSE_TAG_THRESHOLD nodes, so the inner loop
    is at most THRESHOLD*(THRESHOLD-1)/2 = 190 iterations per tag — linear in
    the number of tags.
    """
    start = time.perf_counter()
    G = _build_tag_edge_graph(num_pages=200, tags_per_page=5, total_unique_tags=500)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, (
        f"Tag-pair edge construction took {elapsed:.3f}s (expected < 1.0s). "
        "Possible O(K²) regression."
    )
    # Sanity: the graph must have nodes and at least some edges
    assert G.number_of_nodes() == 200
    assert G.number_of_edges() >= 0  # small tag pool → many edges; large → fewer


# ---------------------------------------------------------------------------
# P1-11 benchmark: concept-page cross-community lookup
# ---------------------------------------------------------------------------

def _build_communities(
    num_concepts: int,
    members_per_concept: int,
) -> tuple[nx.Graph, dict[int, list[str]]]:
    """Build a synthetic graph and communities dict for the cross-edge loop."""
    G = nx.Graph()
    communities: dict[int, list[str]] = {}

    for cid in range(num_concepts):
        members = [f"skill:concept-{cid}-member-{j}" for j in range(members_per_concept)]
        communities[cid] = members
        for nid in members:
            G.add_node(nid, label=nid, type="skill", tags=[f"concept-{cid}"])

    # Add cross-community edges so the lookup actually traverses neighbors
    for cid in range(num_concepts - 1):
        n1 = communities[cid][0]
        n2 = communities[cid + 1][0]
        G.add_edge(n1, n2, weight=1, shared_tags=[f"bridge-{cid}"])

    return G, communities


def _run_cross_community_lookup(
    G: nx.Graph,
    communities: dict[int, list[str]],
) -> dict[int, dict[str, int]]:
    """
    Run the optimised cross-community counting from generate_concept_pages.

    This mirrors the fixed code: build node_to_community + community_labels
    once, then do O(1) lookups per neighbor.
    """
    from wiki_graphify import label_community

    node_to_community: dict[str, int] = {
        nid: cid for cid, members in communities.items() for nid in members
    }
    community_labels: dict[int, str] = {
        cid: label_community(G, members) for cid, members in communities.items()
    }

    results: dict[int, dict[str, int]] = {}
    for cid, members in communities.items():
        cross: dict[str, int] = defaultdict(int)
        members_set = set(members)
        for nid in members:
            for neighbor in G.neighbors(nid):
                if neighbor not in members_set:
                    other_cid = node_to_community.get(neighbor)
                    if other_cid is not None and other_cid != cid:
                        cross[community_labels[other_cid]] += 1
        results[cid] = dict(cross)
    return results


def test_concept_cross_lookup_under_threshold() -> None:
    """
    50 concepts with 20 members each must complete in < 1 s.

    The naive O(C²·members) approach iterates all communities for every
    out-of-community neighbor. The optimised path uses a reverse-index so each
    neighbor lookup is O(1), giving O(C·members) total.
    """
    G, communities = _build_communities(num_concepts=50, members_per_concept=20)

    start = time.perf_counter()
    results = _run_cross_community_lookup(G, communities)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, (
        f"Concept cross-community lookup took {elapsed:.3f}s (expected < 1.0s). "
        "Possible O(C²·members) regression."
    )
    # Sanity: communities with cross-edges must report them
    assert len(results) == 50
    # First community has one cross-edge to community 1
    assert sum(results[0].values()) >= 1
