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
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from networkx.algorithms.community import greedy_modularity_communities

from wiki_utils import parse_frontmatter as _parse_fm

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki"))
SKILL_ENTITIES = WIKI_DIR / "entities" / "skills"
AGENT_ENTITIES = WIKI_DIR / "entities" / "agents"
# MCP entities are sharded by first character
# (entities/mcp-servers/<shard>/<slug>.md) — see McpRecord.entity_relpath.
# Iterate recursively so the shard layout is transparent here.
MCP_ENTITIES = WIKI_DIR / "entities" / "mcp-servers"
CONCEPTS_DIR = WIKI_DIR / "concepts"
GRAPH_OUT = WIKI_DIR / "graphify-out"
# Source of truth for per-node quality: sidecars produced by
# ``src/skill_quality.py``. Graph nodes get ``quality_score`` and
# ``quality_grade`` attached when a matching sidecar exists.
QUALITY_SIDECAR_DIR = Path(os.path.expanduser("~/.claude/skill-quality"))


def parse_frontmatter(filepath: Path) -> dict:
    """Parse YAML frontmatter from a markdown file, adding path metadata."""
    content = filepath.read_text(encoding="utf-8", errors="replace")
    result: dict = {"_path": str(filepath), "_stem": filepath.stem, "_content": content}
    result.update(_parse_fm(content))
    return result


# ────────────────────────────────────────────────────────────────────
# Entity-type dispatch — keeps skill/agent/mcp-server treatment in one
# place so the inject loop below stays readable as types grow.
# ────────────────────────────────────────────────────────────────────


def _mcp_shard(slug: str) -> str:
    """Return the shard segment for an MCP slug (matches McpRecord.entity_relpath)."""
    first = slug[0] if slug else ""
    return first if first.isalpha() else "0-9"


def _entity_page_path(entity_type: str, slug: str) -> Path | None:
    """Resolve (entity_type, slug) to its on-disk page path. None for unknown types."""
    if entity_type == "skill":
        return SKILL_ENTITIES / f"{slug}.md"
    if entity_type == "agent":
        return AGENT_ENTITIES / f"{slug}.md"
    if entity_type == "mcp-server":
        return MCP_ENTITIES / _mcp_shard(slug) / f"{slug}.md"
    return None


def _entity_wikilink(entity_type: str, slug: str) -> str | None:
    """Wikilink target for an entity. None for unknown types."""
    if entity_type == "skill":
        return f"[[entities/skills/{slug}]]"
    if entity_type == "agent":
        return f"[[entities/agents/{slug}]]"
    if entity_type == "mcp-server":
        return f"[[entities/mcp-servers/{_mcp_shard(slug)}/{slug}]]"
    return None


def _related_section_header(entity_type: str) -> str:
    """Section header under which graph-derived backlinks land."""
    return {
        "skill": "## Related Skills",
        "agent": "## Related Agents",
        "mcp-server": "## Related MCP Servers",
    }.get(entity_type, "## Related")


SLUG_STOP: frozenset[str] = frozenset({
    "a", "an", "the", "and", "of", "for", "to", "with",
    "skill", "agent", "expert", "pro", "core",
})


def _strip_frontmatter(content: str) -> str:
    """Return the markdown body with the YAML frontmatter removed."""
    parts = content.split("---", 2)
    return parts[2] if len(parts) >= 3 else content


def _load_full_body(meta: dict, slug: str, entity_type: str) -> str:
    """Load the canonical content source for semantic embedding.

    Different entity types live in different places in the wiki:

      - **skill**: the pipeline-converted ``<wiki>/converted/<slug>/SKILL.md``
        is the canonical wiki body. We also concatenate the
        ``references/*.md`` pipeline stages when present so a skill
        whose logic is split across 5 stages still embeds the union
        of all five. Falls back to the entity card body when no
        converted dir exists (short skills <180 lines that skip the
        pipeline).
      - **agent**: ``<wiki>/converted-agents/<slug>.md`` holds the full
        Claude Code agent prompt (populated by ``ctx-agent-mirror``).
        Falls back to the entity card body when the mirror hasn't
        run for a particular slug.
      - **mcp-server**: entity cards are the only body we have; the
        pulsemcp description + tags + related links inside the
        card are the full context. Phase 6f.B detail-enrichment
        could later pull the GitHub README here, but that's out of
        scope for this pass.

    The returned text is always non-empty — falls through to the
    slug (hyphen-split) if every richer source was blank.
    """
    entity_body = _strip_frontmatter(meta.get("_content", ""))

    rich_body = ""
    if entity_type == "skill":
        skill_md = WIKI_DIR / "converted" / slug / "SKILL.md"
        if skill_md.is_file():
            try:
                rich_body = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                rich_body = ""
            refs_dir = WIKI_DIR / "converted" / slug / "references"
            if refs_dir.is_dir():
                parts: list[str] = [rich_body] if rich_body else []
                for ref in sorted(refs_dir.glob("*.md")):
                    try:
                        parts.append(ref.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        continue
                rich_body = "\n\n".join(p for p in parts if p.strip())
    elif entity_type == "agent":
        agent_md = WIKI_DIR / "converted-agents" / f"{slug}.md"
        if agent_md.is_file():
            try:
                rich_body = agent_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                rich_body = ""

    body = rich_body or entity_body

    # Prepend a tiny header (name + tags) so it rides along even when
    # body content is noisy. Short, canonical, high-signal.
    header_pieces: list[str] = []
    for key in ("name", "title"):
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            header_pieces.append(v.strip())
            break
    desc = meta.get("description")
    if isinstance(desc, str) and desc.strip():
        header_pieces.append(desc.strip())
    header_pieces.append(slug.replace("-", " "))
    tags = meta.get("tags") or []
    if isinstance(tags, list) and tags:
        header_pieces.append(" ".join(str(t) for t in tags if t))

    head = " | ".join(p for p in header_pieces if p)
    combined = f"{head}\n\n{body}" if body else head
    return combined.strip() or slug.replace("-", " ")


def _slug_tokens(slug: str) -> list[str]:
    """Return the >=3-char non-stopword tokens from a slug."""
    return [
        t for t in slug.lower().split("-")
        if len(t) >= 3 and t not in SLUG_STOP
    ]


def _pairs_from_index(
    index: dict[str, list[str]],
    *,
    dense_threshold: int,
    saturation: int,
) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], list[str]]]:
    """Turn a ``{key: [node_ids]}`` index into per-pair overlap counts.

    Buckets with more than ``dense_threshold`` nodes are skipped —
    they'd produce O(N^2) noise edges of weight 1 (e.g. a "python"
    tag on 1,000 entities ⇒ 499,500 near-meaningless pairs).

    Returns ``(counts, shared_keys)``:
      - ``counts[pair]`` is the number of shared keys in that pair
      - ``shared_keys[pair]`` is the concrete list of shared keys,
        for edge attribution in the rendered graph.
    """
    counts: dict[tuple[str, str], int] = defaultdict(int)
    shared: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key, node_ids in index.items():
        if len(node_ids) > dense_threshold:
            continue
        sorted_ids = sorted(node_ids)
        for i, n1 in enumerate(sorted_ids):
            for n2 in sorted_ids[i + 1:]:
                pair = (n1, n2)
                counts[pair] += 1
                shared[pair].append(key)
    return counts, shared


def build_graph() -> tuple[nx.Graph, dict[str, dict]]:
    """Build a networkx graph from all entity pages.

    Nodes = entity pages (skills + agents + mcp-servers).

    Edges come from three independent signals whose per-edge
    contributions are blended into a single ``final_weight``:

      - **Semantic similarity** (default blend 0.70) — cosine between
        sentence-embedding vectors of each entity's name+description+
        slug+tags. Captures context-level affinity that tags miss
        (e.g. a python-linter MCP ↔ a code-reviewer agent).
      - **Shared tags** (default 0.15) — explicit ``tags:`` frontmatter
        overlap, saturating at ``shared_tag_saturation`` overlaps.
      - **Shared slug tokens** (default 0.15) — matching >=3-char
        non-stopword tokens from the slug (``atlassian-admin`` ↔
        ``atlassian-cloud`` share the ``atlassian`` token).

    Weights and thresholds are configurable via ``cfg.graph.*`` in
    config.json (see the ``graph`` section comments for the full
    rationale). Setting ``semantic=0.0`` reverts to the pre-7.1
    tag+token-only behaviour.

    Each edge carries: ``semantic_sim``, ``tag_sim``, ``token_sim``,
    ``final_weight``, ``weight`` (alias of final_weight for backward
    compat with Obsidian graph view + downstream consumers that still
    read ``weight``), and ``shared_tags`` / ``shared_tokens`` lists
    for explainability.
    """
    from ctx_config import cfg as _cfg  # noqa: PLC0415 — local to avoid
    # a config read at module-import time (tests patch cfg).
    import semantic_edges as _sem  # noqa: PLC0415

    G = nx.Graph()
    entities: dict[str, dict] = {}

    sources: list[tuple[Path, str, str]] = [
        (SKILL_ENTITIES, "skill", "*.md"),
        (AGENT_ENTITIES, "agent", "*.md"),
        (MCP_ENTITIES, "mcp-server", "**/*.md"),
    ]

    # ── Phase 1: discover nodes + collect embedding texts ─────────────
    embed_nodes: list[_sem.SemanticNode] = []
    for entity_dir, entity_type, glob_pattern in sources:
        if not entity_dir.exists():
            continue
        if "**" in glob_pattern:
            pages = sorted(entity_dir.rglob("*.md"))
        else:
            pages = sorted(entity_dir.glob(glob_pattern))
        for page in pages:
            meta = parse_frontmatter(page)
            tags = meta.get("tags", [])
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            slug = page.stem
            node_id = f"{entity_type}:{slug}"
            G.add_node(node_id, label=slug, type=entity_type, tags=tags)
            entities[node_id] = meta
            embed_nodes.append(_sem.SemanticNode(
                node_id=node_id,
                text=_load_full_body(meta, slug, entity_type),
            ))

    # ── Phase 2: tag + slug-token indices (cheap) ────────────────────
    tag_index: dict[str, list[str]] = defaultdict(list)
    token_index: dict[str, list[str]] = defaultdict(list)
    for nid, data in G.nodes(data=True):
        for tag in data.get("tags", []):
            if tag and tag != "uncategorized":
                tag_index[str(tag)].append(nid)
        slug = data.get("label", nid.split(":", 1)[-1])
        for token in _slug_tokens(slug):
            token_index[token].append(nid)

    tag_counts, tag_shared = _pairs_from_index(
        tag_index,
        dense_threshold=_cfg.graph_dense_tag_threshold,
        saturation=_cfg.graph_shared_tag_saturation,
    )
    token_counts, token_shared = _pairs_from_index(
        token_index,
        dense_threshold=_cfg.graph_dense_token_threshold,
        saturation=_cfg.graph_shared_token_saturation,
    )

    # ── Phase 3: semantic edges (expensive) ──────────────────────────
    if _cfg.graph_edge_weight_semantic > 0.0:
        sem_pairs = _sem.compute_semantic_edges(
            embed_nodes,
            top_k=_cfg.graph_semantic_top_k,
            min_cosine=_cfg.graph_semantic_min_cosine,
            batch_size=_cfg.graph_semantic_batch_size,
            cache_dir=_cfg.graph_semantic_cache_dir,
            backend=_cfg.intake_backend,
            model=_cfg.intake_model,
        )
    else:
        sem_pairs = {}

    # ── Phase 4: union all pairs and blend weights per pair ──────────
    tag_sat = max(_cfg.graph_shared_tag_saturation, 1)
    tok_sat = max(_cfg.graph_shared_token_saturation, 1)
    w_sem = _cfg.graph_edge_weight_semantic
    w_tag = _cfg.graph_edge_weight_tags
    w_tok = _cfg.graph_edge_weight_tokens

    all_pairs: set[tuple[str, str]] = (
        set(sem_pairs) | set(tag_counts) | set(token_counts)
    )
    for pair in all_pairs:
        n1, n2 = pair
        sem = sem_pairs.get(pair, 0.0)
        tag = min(tag_counts.get(pair, 0) / tag_sat, 1.0)
        tok = min(token_counts.get(pair, 0) / tok_sat, 1.0)
        final = w_sem * sem + w_tag * tag + w_tok * tok
        # An edge that ends up exactly 0.0 is useless — happens only
        # when every weight either drops to zero or the signals all
        # landed below their floors. Skip it.
        if final <= 0.0:
            continue
        G.add_edge(
            n1, n2,
            semantic_sim=round(sem, 4),
            tag_sim=round(tag, 4),
            token_sim=round(tok, 4),
            final_weight=round(final, 4),
            weight=round(final, 4),
            shared_tags=tag_shared.get(pair, []),
            shared_tokens=token_shared.get(pair, []),
        )

    _attach_quality_attrs(G, QUALITY_SIDECAR_DIR)

    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    print(
        f"Edge sources: semantic={len(sem_pairs)}, tag_pairs={len(tag_counts)}, "
        f"token_pairs={len(token_counts)} "
        f"(blend: sem={w_sem}, tag={w_tag}, tok={w_tok})"
    )
    print(f"Tag index: {len(tag_index)} unique tags; token index: {len(token_index)} tokens")
    return G, entities


def _attach_quality_attrs(G: nx.Graph, sidecar_dir: Path) -> int:
    """Decorate graph nodes with quality score + grade from sidecar JSONs.

    The sidecar directory is the source of truth for quality data; this
    function just mirrors ``quality_score`` and ``quality_grade`` onto
    matching graph nodes so downstream consumers (graph export, Obsidian
    graph view coloring) see one consistent number. Nodes without a
    sidecar keep their default values so the attribute is always present.

    Scans two roots:
      - ``<sidecar_dir>/*.json`` — skill + agent scores from
        ``skill_quality.persist_quality``
      - ``<sidecar_dir>/mcp/*.json`` — MCP scores from
        ``mcp_quality.persist_quality`` (Phase 4 separated MCP scores
        into a subdir so the MCP scorer's different signal-set
        doesn't pollute the flat skill/agent layout).
    """
    attached = 0
    # Default attrs so callers can always read the key without KeyError.
    for nid in G.nodes():
        G.nodes[nid].setdefault("quality_score", None)
        G.nodes[nid].setdefault("quality_grade", None)

    if not sidecar_dir.is_dir():
        return 0

    # Two roots to scan. The ``subject_type`` field in each sidecar
    # determines the graph node prefix — ``skill:`` or ``agent:`` for
    # the flat dir, ``mcp-server:`` for the mcp/ subdir. We don't infer
    # the prefix from the directory because the subject_type is the
    # canonical source of truth and some historical sidecars may have
    # landed in the wrong place.
    roots: list[Path] = [sidecar_dir]
    mcp_subdir = sidecar_dir / "mcp"
    if mcp_subdir.is_dir():
        roots.append(mcp_subdir)

    for root in roots:
        for path in root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            slug = data.get("slug")
            subject_type = data.get("subject_type", "skill")
            if not slug:
                continue
            # MCP sidecars written by Phase 4 don't carry a
            # ``subject_type`` field (the McpQualityScore shape omits
            # it since the subdir is the type discriminator). When a
            # sidecar lives under mcp/ we treat it as mcp-server.
            if root == mcp_subdir and subject_type == "skill":
                subject_type = "mcp-server"
            node_id = f"{subject_type}:{slug}"
            if node_id not in G:
                continue
            G.nodes[node_id]["quality_score"] = float(data.get("score", 0.0))
            G.nodes[node_id]["quality_grade"] = data.get("grade")
            attached += 1
    return attached


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

    # Reverse index: O(1) community lookup per neighbor instead of O(C) linear
    # scan through communities.items(). Reduces cross-edge loop from O(C²·members)
    # to O(C·members).
    node_to_community: dict[str, int] = {
        nid: cid for cid, members in communities.items() for nid in members
    }
    # Pre-compute community labels once to avoid redundant label_community calls
    community_labels: dict[int, str] = {
        cid: label_community(G, members) for cid, members in communities.items()
    }

    for cid, members in sorted(communities.items(), key=lambda x: -len(x[1])):
        if len(members) < 3:
            continue  # Skip tiny communities

        label = community_labels[cid]
        safe_name = label.lower().replace(" + ", "-").replace(" ", "-")
        filename = f"community-{safe_name}.md"

        # Top members by degree
        top_members = sorted(members, key=lambda n: G.degree(n), reverse=True)[:20]
        member_links = "\n".join(
            f"- [[entities/{'skills' if 'skill:' in m else 'agents'}/{m.split(':', 1)[1]}]]"
            for m in top_members
        )
        remaining = len(members) - len(top_members)

        # Cross-community connections (O(neighbors) via reverse-index lookup)
        cross: dict[str, int] = defaultdict(int)
        members_set = set(members)
        for nid in members:
            for neighbor in G.neighbors(nid):
                if neighbor not in members_set:
                    other_cid = node_to_community.get(neighbor)
                    if other_cid is not None and other_cid != cid:
                        cross[community_labels[other_cid]] += 1

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
        page_path = _entity_page_path(entity_type, name)

        if page_path is None or not page_path.exists():
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
            link = _entity_wikilink(n_type, n_name)
            if link is None or link in content:
                continue
            new_links.append(f"- {link}")

        if not new_links:
            continue

        # Inject under the type-appropriate ## Related section header.
        section_header = _related_section_header(entity_type)
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

    # Export graph as node-link JSON. Pin the edges key so readers
    # (resolve_graph, wiki_visualize) can rely on it regardless of the
    # networkx version that wrote it — default changed from "links" in
    # <3.0 to "edges" in >=3.0, which silently broke every consumer.
    graph_data = nx.node_link_data(G, edges="edges")
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
