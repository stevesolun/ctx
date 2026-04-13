#!/usr/bin/env python3
"""
wiki_graphify.py -- Build a knowledge graph from wiki entity pages, detect
communities, generate concept pages, and inject wikilinks.

Uses networkx for graph construction and greedy_modularity_communities for
community detection (no external Leiden dependency needed).

Usage:
    python wiki_graphify.py                    # Full run: graph + communities + inject
    python wiki_graphify.py --graph-only       # Build graph and export JSON only
    python wiki_graphify.py --dry-run          # Preview changes without writing
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from networkx.algorithms.community import greedy_modularity_communities

sys.path.insert(0, str(Path(__file__).parent))
from wiki_utils import parse_frontmatter as _parse_fm  # noqa: E402

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki"))
SKILL_ENTITIES = WIKI_DIR / "entities" / "skills"
AGENT_ENTITIES = WIKI_DIR / "entities" / "agents"
CONCEPTS_DIR = WIKI_DIR / "concepts"
GRAPH_OUT = WIKI_DIR / "graphify-out"


def parse_frontmatter(filepath: Path) -> dict:
    """Parse YAML frontmatter from a markdown file, adding path metadata."""
    content = filepath.read_text(encoding="utf-8", errors="replace")
    result: dict = {"_path": str(filepath), "_stem": filepath.stem, "_content": content}
    result.update(_parse_fm(content))
    return result


def build_graph() -> tuple[nx.Graph, dict[str, dict]]:
    """Build a networkx graph from all entity pages.

    Nodes = entity pages (skills + agents).
    Edges = shared tags (weighted by number of shared tags).
    """
    G = nx.Graph()
    entities: dict[str, dict] = {}

    # Collect all entities
    for entity_dir, entity_type in [(SKILL_ENTITIES, "skill"), (AGENT_ENTITIES, "agent")]:
        if not entity_dir.exists():
            continue
        for page in sorted(entity_dir.glob("*.md")):
            meta = parse_frontmatter(page)
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            node_id = f"{entity_type}:{page.stem}"
            G.add_node(node_id, label=page.stem, type=entity_type, tags=tags)
            entities[node_id] = meta

    # Build tag->nodes index for edge creation
    tag_index: dict[str, list[str]] = defaultdict(list)
    for nid, data in G.nodes(data=True):
        for tag in data.get("tags", []):
            if tag != "uncategorized":
                tag_index[tag].append(nid)

    # Create edges between nodes sharing tags
    edge_count = 0
    for tag, nodes in tag_index.items():
        for i, n1 in enumerate(nodes):
            for n2 in nodes[i + 1:]:
                if G.has_edge(n1, n2):
                    G[n1][n2]["weight"] += 1
                    G[n1][n2]["shared_tags"].append(tag)
                else:
                    G.add_edge(n1, n2, weight=1, shared_tags=[tag])
                    edge_count += 1

    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    print(f"Tag index: {len(tag_index)} unique tags")
    return G, entities


def detect_communities(G: nx.Graph) -> dict[int, list[str]]:
    """Run greedy modularity community detection."""
    if G.number_of_nodes() == 0:
        return {}

    # Filter to connected components for better detection
    communities_gen = greedy_modularity_communities(G, weight="weight", resolution=1.2)
    communities: dict[int, list[str]] = {}
    for i, community in enumerate(communities_gen):
        communities[i] = sorted(community)

    print(f"Communities: {len(communities)} detected")
    for cid, members in sorted(communities.items(), key=lambda x: -len(x[1]))[:10]:
        print(f"  Community {cid}: {len(members)} members")
    return communities


def label_community(G: nx.Graph, members: list[str]) -> str:
    """Generate a human-readable label for a community based on dominant tags."""
    tag_counts: dict[str, int] = defaultdict(int)
    for nid in members:
        for tag in G.nodes[nid].get("tags", []):
            if tag != "uncategorized":
                tag_counts[tag] += 1

    if not tag_counts:
        return "Miscellaneous"

    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:3]
    return " + ".join(t[0].title() for t in top_tags)


def generate_concept_pages(
    G: nx.Graph,
    communities: dict[int, list[str]],
    dry_run: bool = False,
) -> list[str]:
    """Generate concept pages for each community."""
    CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    for cid, members in sorted(communities.items(), key=lambda x: -len(x[1])):
        if len(members) < 3:
            continue  # Skip tiny communities

        label = label_community(G, members)
        safe_name = label.lower().replace(" + ", "-").replace(" ", "-")
        filename = f"community-{safe_name}.md"

        # Top members by degree
        top_members = sorted(members, key=lambda n: G.degree(n), reverse=True)[:20]
        member_links = "\n".join(
            f"- [[entities/{'skills' if 'skill:' in m else 'agents'}/{m.split(':', 1)[1]}]]"
            for m in top_members
        )
        remaining = len(members) - len(top_members)

        # Cross-community connections
        cross: dict[str, int] = defaultdict(int)
        for nid in members:
            for neighbor in G.neighbors(nid):
                if neighbor not in members:
                    # Find which community neighbor belongs to
                    for other_cid, other_members in communities.items():
                        if other_cid != cid and neighbor in other_members:
                            other_label = label_community(G, other_members)
                            cross[other_label] += 1
                            break

        cross_links = "\n".join(
            f"- {lbl} ({cnt} connections)"
            for lbl, cnt in sorted(cross.items(), key=lambda x: -x[1])[:8]
        )

        page = f"""---
title: "{label}"
created: {TODAY}
updated: {TODAY}
type: concept
community_id: {cid}
member_count: {len(members)}
tags: [{', '.join(t for t, _ in sorted(defaultdict(int, {t: c for m in members for t, c in [(tag, 1) for tag in G.nodes[m].get('tags', [])] if t != 'uncategorized'}).items(), key=lambda x: -x[1])[:5])}]
---

# {label}

> Auto-generated community of {len(members)} related skills and agents.

## Key Members

{member_links}
{f'*... and {remaining} more*' if remaining > 0 else ''}

## Cross-Community Connections

{cross_links if cross_links else '*No strong cross-community connections*'}

---

*Generated by wiki_graphify.py via community detection. See [[graphify-out/graph-report]] for full graph.*
"""
        if dry_run:
            print(f"  [DRY RUN] Would create: concepts/{filename}")
        else:
            (CONCEPTS_DIR / filename).write_text(page, encoding="utf-8")
        created.append(filename)

    print(f"Concept pages: {len(created)} created")
    return created


def inject_community_links(
    G: nx.Graph,
    communities: dict[int, list[str]],
    dry_run: bool = False,
) -> int:
    """Inject community membership and top-N neighbor wikilinks into entity frontmatter."""
    updated = 0

    # Build node->community mapping
    node_community: dict[str, int] = {}
    for cid, members in communities.items():
        for nid in members:
            node_community[nid] = cid

    for nid, data in G.nodes(data=True):
        entity_type = data.get("type", "skill")
        name = data.get("label", nid.split(":", 1)[-1])
        entity_dir = SKILL_ENTITIES if entity_type == "skill" else AGENT_ENTITIES
        page_path = entity_dir / f"{name}.md"

        if not page_path.exists():
            continue

        content = page_path.read_text(encoding="utf-8", errors="replace")

        # Find top neighbors by edge weight
        neighbors = sorted(
            G.neighbors(nid),
            key=lambda n: G[nid][n].get("weight", 1),
            reverse=True,
        )[:6]

        new_links: list[str] = []
        for neighbor in neighbors:
            n_type = G.nodes[neighbor].get("type", "skill")
            n_name = G.nodes[neighbor].get("label", neighbor.split(":", 1)[-1])
            link = f"[[entities/{'skills' if n_type == 'skill' else 'agents'}/{n_name}]]"
            if link not in content:
                new_links.append(f"- {link}")

        if not new_links:
            continue

        # Inject under ## Related Skills/Agents section
        section_header = "## Related Skills" if entity_type == "skill" else "## Related Agents"
        if section_header in content:
            insert_text = "\n".join(new_links)
            content = content.replace(
                section_header + "\n",
                section_header + "\n" + insert_text + "\n",
                1,
            )
        else:
            content = content.rstrip() + f"\n\n{section_header}\n" + "\n".join(new_links) + "\n"

        if not dry_run:
            page_path.write_text(content, encoding="utf-8")
        updated += 1

    print(f"Entity pages updated with graph-based wikilinks: {updated}")
    return updated


def export_graph(G: nx.Graph, communities: dict[int, list[str]]) -> None:
    """Export graph as JSON and generate a report."""
    GRAPH_OUT.mkdir(parents=True, exist_ok=True)

    # Export graph as node-link JSON
    graph_data = nx.node_link_data(G)
    (GRAPH_OUT / "graph.json").write_text(
        json.dumps(graph_data, indent=2, default=str),
        encoding="utf-8",
    )

    # Community labels
    labels = {}
    for cid, members in communities.items():
        labels[cid] = label_community(G, members)

    (GRAPH_OUT / "communities.json").write_text(
        json.dumps({
            "communities": {str(cid): {"label": labels[cid], "members": members}
                           for cid, members in communities.items()},
            "total_communities": len(communities),
            "generated": TODAY,
        }, indent=2),
        encoding="utf-8",
    )

    # God nodes (highest degree)
    god_nodes = sorted(G.nodes(), key=lambda n: G.degree(n), reverse=True)[:20]
    report_lines = [
        "# Graph Report",
        "",
        f"> Generated: {TODAY}",
        f"> Nodes: {G.number_of_nodes()} | Edges: {G.number_of_edges()} | Communities: {len(communities)}",
        "",
        "## God Nodes (Most Connected)",
        "",
    ]
    for nid in god_nodes:
        d = G.nodes[nid]
        report_lines.append(f"- **{d.get('label', nid)}** ({G.degree(nid)} connections) — {d.get('type', '?')}")

    report_lines += ["", "## Communities (by size)", ""]
    for cid, members in sorted(communities.items(), key=lambda x: -len(x[1])):
        report_lines.append(f"- **{labels[cid]}** — {len(members)} members")

    (GRAPH_OUT / "graph-report.md").write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Graph exported to {GRAPH_OUT}/")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build knowledge graph from wiki entities")
    parser.add_argument("--graph-only", action="store_true", help="Build graph and export only")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    G, entities = build_graph()
    communities = detect_communities(G)
    export_graph(G, communities)

    if args.graph_only:
        return

    generate_concept_pages(G, communities, args.dry_run)
    inject_community_links(G, communities, args.dry_run)

    print("\nDone. Open wiki in Obsidian to see the graph visualization.")


if __name__ == "__main__":
    main()
