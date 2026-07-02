"""Shared graph edge scoring primitives.

The full batch builder and incremental attach must agree on these helpers so
semantic, tag, token, and structural signals do not drift between entrypoints.
"""

from __future__ import annotations

from collections import defaultdict
import math

import networkx as nx

SLUG_STOP: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "of",
        "for",
        "to",
        "with",
        "skill",
        "agent",
        "expert",
        "pro",
        "core",
    }
)


def slug_tokens(slug: str, *, stop_words: frozenset[str] = SLUG_STOP) -> list[str]:
    """Return >=3-char non-stopword slug tokens."""
    return [
        token for token in slug.lower().split("-") if len(token) >= 3 and token not in stop_words
    ]


def canonical_pair(n1: str, n2: str) -> tuple[str, str]:
    return (n1, n2) if n1 <= n2 else (n2, n1)


def type_affinity_score(left: str, right: str) -> float:
    if left == right:
        return 0.35
    pair = frozenset((left, right))
    if pair == frozenset(("skill", "agent")):
        return 1.0
    if pair == frozenset(("skill", "mcp-server")):
        return 0.9
    if pair == frozenset(("skill", "harness")):
        return 0.75
    if pair == frozenset(("agent", "mcp-server")):
        return 0.65
    if pair == frozenset(("agent", "harness")):
        return 0.7
    if pair == frozenset(("mcp-server", "harness")):
        return 0.6
    return 0.4


def adamic_adar_scores(
    nodes: list[str],
    pairs: set[tuple[str, str]],
) -> dict[tuple[str, str], float]:
    base = nx.Graph()
    base.add_nodes_from(nodes)
    base.add_edges_from(pairs)
    pair_lookup = set(pairs)
    scores: dict[tuple[str, str], float] = defaultdict(float)
    max_common_degree = 200
    for common in base.nodes:
        neighbors = sorted(base.neighbors(common))
        degree = len(neighbors)
        if degree < 2 or degree > max_common_degree:
            continue
        contribution = 1.0 / math.log(degree)
        for index, n1 in enumerate(neighbors):
            for n2 in neighbors[index + 1 :]:
                pair = canonical_pair(n1, n2)
                if pair in pair_lookup:
                    scores[pair] += contribution
    return {pair: min(score, 1.0) for pair, score in scores.items()}


def pairs_from_index(
    index: dict[str, list[str]],
    *,
    dense_threshold: int,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], list[str]]]:
    """Turn a ``{key: [node_ids]}`` index into per-pair overlap counts."""
    counts: dict[tuple[str, str], int] = defaultdict(int)
    shared: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key, node_ids in index.items():
        if len(node_ids) > dense_threshold:
            continue
        sorted_ids = sorted(node_ids)
        for offset, n1 in enumerate(sorted_ids):
            for n2 in sorted_ids[offset + 1 :]:
                pair = (n1, n2)
                counts[pair] += 1
                shared[pair].append(key)
    return counts, shared
