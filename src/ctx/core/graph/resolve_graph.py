#!/usr/bin/env python3
"""
resolve_graph.py -- Walk the knowledge graph to discover related skills/agents.

Given a set of matched skill names (from resolve_skills.py), this walks the
graph 1-2 hops out to find strongly connected skills the user hasn't loaded yet.

Usage:
    python resolve_graph.py --matched fastapi-pro,docker-expert --top 10
    python resolve_graph.py --matched fastapi-pro --json
    python resolve_graph.py --tags python,api --top 15
"""

import argparse
import json
import logging
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import networkx as nx
from networkx.readwrite import node_link_graph

logger = logging.getLogger(__name__)

WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki"))
GRAPH_PATH = WIKI_DIR / "graphify-out" / "graph.json"

# A valid node-link graph dict must have "nodes" and either "links" or "edges"
# (networkx >= 3.0 uses "edges"; older versions used "links").
_EDGE_KEYS = frozenset({"links", "edges"})


def load_graph(path: Path | None = None) -> nx.Graph:
    """Load the knowledge graph from graph.json.

    Returns an empty graph on any parse or schema error rather than crashing.
    Callers that *require* a populated graph (e.g. CLI main) should check
    ``G.number_of_nodes() == 0`` and handle accordingly.
    """
    graph_path = path if path is not None else GRAPH_PATH
    if not graph_path.exists():
        logger.warning("graph.json not found at %s; returning empty graph", graph_path)
        return nx.Graph()
    try:
        with open(graph_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "nodes" not in data or not (_EDGE_KEYS & data.keys()):
            logger.warning(
                "graph.json missing required keys ('nodes' + one of %s); returning empty graph",
                sorted(_EDGE_KEYS),
            )
            return nx.Graph()
        # NetworkX 2.x wrote edges under "links"; NetworkX 3.x defaults to
        # "edges" and errors out when the schema doesn't match. Detect
        # which schema the file actually uses and pass it explicitly so
        # both old and new graph.json files round-trip cleanly.
        edges_key = "links" if "links" in data else "edges"
        return node_link_graph(data, edges=edges_key)
    except json.JSONDecodeError as exc:
        logger.warning("graph.json is not valid JSON (%s); returning empty graph", exc)
    except UnicodeDecodeError as exc:
        logger.warning("graph.json has invalid encoding (%s); returning empty graph", exc)
    except (KeyError, TypeError, nx.NetworkXError) as exc:
        logger.warning("graph.json failed to deserialize (%s); returning empty graph", exc)
    return nx.Graph()


def resolve_by_seeds(
    G: nx.Graph,
    seed_names: list[str],
    *,
    max_hops: int = 2,
    top_n: int = 10,
    exclude_seeds: bool = True,
) -> list[dict]:
    """Walk the graph from seed skills/agents/mcp-servers and rank neighbors.

    Returns a list of dicts: [{name, type, score, shared_tags, via}]
    """
    # Map plain names to graph node IDs. Tries skill, agent, and
    # mcp-server prefixes so a seed name that happens to match an
    # MCP slug (e.g. "github", "filesystem") can kick off a walk
    # through MCP territory too. Phase 5 additive change — skill and
    # agent seeds still work identically.
    seed_ids: set[str] = set()
    for name in seed_names:
        for prefix in ("skill:", "agent:", "mcp-server:"):
            nid = f"{prefix}{name}"
            if nid in G:
                seed_ids.add(nid)

    if not seed_ids:
        return []

    # Walk neighbors up to max_hops, accumulate scores
    scores: dict[str, float] = defaultdict(float)
    via: dict[str, list[str]] = defaultdict(list)
    shared_tags_map: dict[str, list[str]] = defaultdict(list)

    visited: set[str] = set(seed_ids)
    frontier = list(seed_ids)

    for hop in range(max_hops):
        next_frontier: list[str] = []
        decay = 1.0 / (hop + 1)  # 1.0 for hop 0, 0.5 for hop 1

        for nid in frontier:
            for neighbor in G.neighbors(nid):
                if exclude_seeds and neighbor in seed_ids:
                    continue

                edge_data = G[nid][neighbor]
                weight = edge_data.get("weight", 1) * decay
                scores[neighbor] += weight

                # Track provenance
                seed_label = nid.split(":", 1)[1]
                if seed_label not in via[neighbor]:
                    via[neighbor].append(seed_label)

                for tag in edge_data.get("shared_tags", []):
                    if tag not in shared_tags_map[neighbor]:
                        shared_tags_map[neighbor].append(tag)

                if neighbor not in visited:
                    visited.add(neighbor)
                    next_frontier.append(neighbor)

        frontier = next_frontier

    # Rank + normalise. Post-P2.5 we report both:
    #   score            — raw accumulated edge weight (backward-compat)
    #   normalized_score — score / max(score), in [0, 1] (new, preferred)
    # Pre-fix, callers used absolute ``score >= 1.5`` floors that
    # happened to match the OLD integer-weight graph but broke on the
    # v0.7 float-weight graph (single edge weight is now <=1.0, so a
    # 1.5 floor silently drops ALL single-seed hits). Normalised
    # percentile thresholds don't care about the underlying weight
    # scale.
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
    results: list[dict] = []
    max_score = max((s for _, s in ranked), default=0.0) or 1.0
    for nid, score in ranked:
        node_data = G.nodes.get(nid, {})
        entity_type = node_data.get("type", "skill")
        name = node_data.get("label", nid.split(":", 1)[-1])
        results.append({
            "name": name,
            "type": entity_type,
            "score": round(score, 2),
            "normalized_score": round(score / max_score, 4),
            "shared_tags": shared_tags_map.get(nid, [])[:8],
            "via": via.get(nid, [])[:4],
        })

    return results


def resolve_by_tags(
    G: nx.Graph,
    tags: list[str],
    *,
    top_n: int = 10,
) -> list[dict]:
    """Find skills/agents that match the given tags, ranked by tag overlap + degree."""
    scores: dict[str, float] = defaultdict(float)
    tag_set = set(tags)

    for nid, data in G.nodes(data=True):
        node_tags = set(data.get("tags", []))
        overlap = tag_set & node_tags
        if overlap:
            # Score = number of matching tags + log(degree) for tiebreaking
            scores[nid] = len(overlap) * 10 + math.log1p(G.degree(nid))

    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
    results: list[dict] = []
    for nid, score in ranked:
        node_data = G.nodes.get(nid, {})
        entity_type = node_data.get("type", "skill")
        name = node_data.get("label", nid.split(":", 1)[-1])
        matching_tags = list(tag_set & set(node_data.get("tags", [])))
        results.append({
            "name": name,
            "type": entity_type,
            "score": round(score, 2),
            "matching_tags": matching_tags,
        })

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Graph-based skill/agent discovery")
    parser.add_argument("--matched", help="Comma-separated seed skill names (from resolve_skills.py)")
    parser.add_argument("--tags", help="Comma-separated tags to search for")
    parser.add_argument("--top", type=int, default=10, help="Number of results (default 10)")
    parser.add_argument("--hops", type=int, default=2, help="Max graph hops (default 2)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if not args.matched and not args.tags:
        parser.print_help()
        sys.exit(1)

    G = load_graph()
    if G.number_of_nodes() == 0 and not GRAPH_PATH.exists():
        print(f"Error: {GRAPH_PATH} not found. Run wiki_graphify.py first.", file=sys.stderr)
        sys.exit(1)

    if args.matched:
        seeds = [s.strip() for s in args.matched.split(",")]
        results = resolve_by_seeds(G, seeds, max_hops=args.hops, top_n=args.top)
        mode = "graph-walk"
    else:
        tags = [t.strip() for t in args.tags.split(",")]
        results = resolve_by_tags(G, tags, top_n=args.top)
        mode = "tag-search"

    if args.json:
        print(json.dumps({"mode": mode, "results": results}, indent=2))
    else:
        print(f"\n{mode}: {len(results)} suggestions\n")
        for i, r in enumerate(results, 1):
            tags_str = ", ".join(r.get("shared_tags", r.get("matching_tags", [])))
            via_str = f" (via {', '.join(r['via'])})" if "via" in r else ""
            print(f"  {i:2d}. [{r['type']}] {r['name']}  score={r['score']}{via_str}")
            if tags_str:
                print(f"      tags: {tags_str}")


if __name__ == "__main__":
    main()
