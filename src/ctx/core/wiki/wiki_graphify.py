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
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
from networkx.algorithms.community import (
    greedy_modularity_communities,  # legacy CNM — slow on 10K+ node graphs
    louvain_communities,
)

from ctx.core.entity_types import (
    RELATED_SECTION_FOR_ENTITY_TYPE,
    entity_page_path,
    entity_wikilink,
    mcp_shard,
)
from ctx.core.wiki.wiki_utils import parse_frontmatter as _parse_fm
from ctx.utils._fs_utils import safe_atomic_write_text

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
GRAPH_RELATED_START = "<!-- ctx-graph-related:start -->"
GRAPH_RELATED_END = "<!-- ctx-graph-related:end -->"
GRAPH_RELATED_BLOCK_RE = re.compile(
    rf"\n?{re.escape(GRAPH_RELATED_START)}\n.*?{re.escape(GRAPH_RELATED_END)}\n?",
    re.DOTALL,
)
ENTITY_WIKILINK_RE = re.compile(r"\[\[entities/(?P<section>[^/\]#|]+)/(?P<target>[^\]#|]+)")
CONCEPT_GENERATED_MARKER = "<!-- ctx-generated-community -->"

WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki"))
SKILL_ENTITIES = WIKI_DIR / "entities" / "skills"
AGENT_ENTITIES = WIKI_DIR / "entities" / "agents"
# MCP entities are sharded by first character
# (entities/mcp-servers/<shard>/<slug>.md) — see McpRecord.entity_relpath.
# Iterate recursively so the shard layout is transparent here.
MCP_ENTITIES = WIKI_DIR / "entities" / "mcp-servers"
HARNESS_ENTITIES = WIKI_DIR / "entities" / "harnesses"
CONCEPTS_DIR = WIKI_DIR / "concepts"
GRAPH_OUT = WIKI_DIR / "graphify-out"
# Source of truth for per-node quality: sidecars produced by
# ``src/skill_quality.py``. Graph nodes get ``quality_score`` and
# ``quality_grade`` attached when a matching sidecar exists.
QUALITY_SIDECAR_DIR = Path(os.path.expanduser("~/.claude/skill-quality"))
DEFAULT_WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki")).resolve()
DEFAULT_GRAPH_SEMANTIC_CACHE_DIR = (
    DEFAULT_WIKI_DIR / ".embedding-cache" / "graph"
).resolve()


def configure_wiki_dir(wiki_dir: Path) -> None:
    """Point graphify at a specific wiki root.

    The shipped tarball can be rebuilt in an isolated temp directory, so
    graphify must not be hard-wired to the operator's live
    ``~/.claude/skill-wiki``. Keep the derived entity/output paths in sync.
    """
    global WIKI_DIR, SKILL_ENTITIES, AGENT_ENTITIES, MCP_ENTITIES
    global HARNESS_ENTITIES, CONCEPTS_DIR, GRAPH_OUT

    WIKI_DIR = wiki_dir.expanduser().resolve()
    SKILL_ENTITIES = WIKI_DIR / "entities" / "skills"
    AGENT_ENTITIES = WIKI_DIR / "entities" / "agents"
    MCP_ENTITIES = WIKI_DIR / "entities" / "mcp-servers"
    HARNESS_ENTITIES = WIKI_DIR / "entities" / "harnesses"
    CONCEPTS_DIR = WIKI_DIR / "concepts"
    GRAPH_OUT = WIKI_DIR / "graphify-out"


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
    return mcp_shard(slug)


def _entity_page_path(entity_type: str, slug: str) -> Path | None:
    """Resolve (entity_type, slug) to its on-disk page path. None for unknown types."""
    wiki = WIKI_DIR
    if entity_type == "skill":
        wiki = SKILL_ENTITIES.parents[1]
    elif entity_type == "agent":
        wiki = AGENT_ENTITIES.parents[1]
    elif entity_type == "mcp-server":
        wiki = MCP_ENTITIES.parents[1]
    elif entity_type == "harness":
        wiki = HARNESS_ENTITIES.parents[1]
    else:
        return None
    return entity_page_path(wiki, entity_type, slug)


def _entity_wikilink(entity_type: str, slug: str) -> str | None:
    """Wikilink target for an entity. None for unknown types."""
    return entity_wikilink(entity_type, slug)


def _related_section_header(entity_type: str) -> str:
    """Section header under which graph-derived backlinks land."""
    return RELATED_SECTION_FOR_ENTITY_TYPE.get(entity_type, "## Related")


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

      - **skill**: when a micro-skill conversion preserved
        ``SKILL.md.original``, use that full source body for semantic
        embedding. This keeps graph quality high without crawling every
        generated shard file. Otherwise use the converted ``SKILL.md``
        plus ``references/*.md`` stages when present. Falls back to the
        entity card body when no converted dir exists.
      - **agent**: ``<wiki>/converted-agents/<slug>.md`` holds the full
        Claude Code agent prompt (populated by ``ctx-agent-mirror``).
        Falls back to the entity card body when the mirror hasn't
        run for a particular slug.
      - **mcp-server** and **harness**: entity cards are the only body we have; the
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
        converted_dir = WIKI_DIR / "converted" / slug
        original_md = converted_dir / "SKILL.md.original"
        skill_md = converted_dir / "SKILL.md"
        if original_md.is_file():
            try:
                rich_body = original_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                rich_body = ""
        if skill_md.is_file() and not rich_body:
            try:
                rich_body = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                rich_body = ""
            refs_dir = converted_dir / "references"
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


def _pair(n1: str, n2: str) -> tuple[str, str]:
    return (n1, n2) if n1 <= n2 else (n2, n1)


def _source_keys(meta: dict) -> list[str]:
    """High-specificity source keys that can justify an edge."""
    keys: list[str] = []
    for field in (
        "repo_url",
        "repository",
        "source_url",
        "homepage",
        "detail_url",
        "package_url",
    ):
        raw = meta.get(field)
        if not isinstance(raw, str):
            continue
        value = raw.strip().lower().rstrip("/")
        if value:
            keys.append(f"{field}:{value}")
    return sorted(set(keys))


def _direct_link_targets(content: str) -> set[str]:
    """Return graph node IDs referenced by entity wikilinks in markdown."""
    out: set[str] = set()
    section_to_type = {
        "skills": "skill",
        "agents": "agent",
        "mcp-servers": "mcp-server",
        "harnesses": "harness",
    }
    for match in ENTITY_WIKILINK_RE.finditer(content):
        entity_type = section_to_type.get(match.group("section"))
        if entity_type is None:
            continue
        slug = Path(match.group("target")).stem
        if slug:
            out.add(f"{entity_type}:{slug}")
    return out


def _effective_semantic_cache_dir(configured_cache_dir: Path) -> Path:
    """Keep custom ``--wiki-dir`` graph builds out of the live cache by default."""
    configured = configured_cache_dir.expanduser()
    try:
        configured_resolved = configured.resolve()
    except OSError:
        configured_resolved = configured
    if WIKI_DIR != DEFAULT_WIKI_DIR and configured_resolved == DEFAULT_GRAPH_SEMANTIC_CACHE_DIR:
        return WIKI_DIR / ".embedding-cache" / "graph"
    return configured


def _type_affinity_score(left: str, right: str) -> float:
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


def _mean_present(*values: float | None) -> float:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else 0.0


def _quality_usage_signals(sidecar_dir: Path) -> dict[str, dict[str, float | None]]:
    signals: dict[str, dict[str, float | None]] = {}
    if not sidecar_dir.is_dir():
        return signals
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
            if not slug:
                continue
            subject_type = data.get("subject_type", "skill")
            if root == mcp_subdir and subject_type == "skill":
                subject_type = "mcp-server"
            node_id = f"{subject_type}:{slug}"
            quality = _float_or_none(data.get("score"))
            usage = _float_or_none(data.get("usage_score"))
            raw_signals = data.get("signals")
            if usage is None and isinstance(raw_signals, dict):
                telemetry = raw_signals.get("telemetry")
                popularity = raw_signals.get("popularity")
                if isinstance(telemetry, dict):
                    usage = _float_or_none(telemetry.get("score"))
                elif isinstance(popularity, dict):
                    usage = _float_or_none(popularity.get("score"))
            signals[node_id] = {"quality": quality, "usage": usage}
    return signals


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _adamic_adar_scores(
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
        for i, n1 in enumerate(neighbors):
            for n2 in neighbors[i + 1:]:
                pair = _pair(n1, n2)
                if pair in pair_lookup:
                    scores[pair] += contribution
    return {pair: min(score, 1.0) for pair, score in scores.items()}


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


def build_graph(
    *, incremental: bool = True,
    persist_semantic_cache: bool = True,
    _affected_out: set[str] | None = None,
) -> tuple[nx.Graph, dict[str, dict]]:
    """Build a networkx graph from all entity pages.

    Nodes = entity pages (skills + agents + mcp-servers).

    Edges come from independent signals whose per-edge contributions
    are blended into a single ``final_weight``:

      - **Semantic similarity** (default blend 0.70) — cosine between
        sentence-embedding vectors of each entity's name+description+
        slug+tags. Captures context-level affinity that tags miss
        (e.g. a python-linter MCP ↔ a code-reviewer agent).
      - **Shared tags** (default 0.15) — explicit ``tags:`` frontmatter
        overlap, saturating at ``shared_tag_saturation`` overlaps.
      - **Shared slug tokens** (default 0.15) — matching >=3-char
        non-stopword tokens from the slug (``atlassian-admin`` ↔
        ``atlassian-cloud`` share the ``atlassian`` token).

      - **Source overlap** - shared repo/source/homepage/detail URLs.
      - **Direct wikilinks** - explicit links to another entity page.

    Existing base edges can receive explainable boosts from direct links,
    source overlap, Adamic-Adar shared-neighbor structure, type affinity,
    usage telemetry, and quality scores. Boost-only evidence never creates
    an edge; it only strengthens an edge justified by a base signal.

    Weights and thresholds are configurable via ``cfg.graph.*`` in
    config.json (see the ``graph`` section comments for the full
    rationale). Setting ``semantic=0.0`` reverts to the pre-7.1
    tag+token-only behaviour.

    Each edge carries the raw signal values, ``final_weight``, ``weight``
    (alias of final_weight for backward compat with Obsidian graph view +
    downstream consumers that still read ``weight``), concrete shared-key
    lists, ``edge_reasons``, and weighted ``score_components``.
    """
    from ctx_config import cfg as _cfg  # noqa: PLC0415 — local to avoid
    # a config read at module-import time (tests patch cfg).
    from ctx.core.graph import semantic_edges as _sem  # noqa: PLC0415

    G = nx.Graph()
    entities: dict[str, dict] = {}

    sources: list[tuple[Path, str, str]] = [
        (SKILL_ENTITIES, "skill", "*.md"),
        (AGENT_ENTITIES, "agent", "*.md"),
        (MCP_ENTITIES, "mcp-server", "**/*.md"),
        (HARNESS_ENTITIES, "harness", "*.md"),
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
    source_index: dict[str, list[str]] = defaultdict(list)
    direct_pairs: set[tuple[str, str]] = set()
    for nid, data in G.nodes(data=True):
        for tag in data.get("tags", []):
            if tag and tag != "uncategorized":
                tag_index[str(tag)].append(nid)
        slug = data.get("label", nid.split(":", 1)[-1])
        for token in _slug_tokens(slug):
            token_index[token].append(nid)
        meta = entities.get(nid, {})
        source_keys = _source_keys(meta)
        direct_targets = sorted(
            target
            for target in _direct_link_targets(str(meta.get("_content", "")))
            if target in G and target != nid
        )
        data["source_keys"] = source_keys
        data["direct_targets"] = direct_targets
        for source_key in source_keys:
            source_index[source_key].append(nid)
        for target in direct_targets:
            direct_pairs.add(_pair(nid, target))

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
    source_counts, source_shared = _pairs_from_index(
        source_index,
        dense_threshold=_cfg.graph_dense_source_threshold,
        saturation=1,
    )

    # ── Phase 3: semantic edges (expensive) ──────────────────────────
    # ``build_floor`` governs inclusion in graph.json; ``min_cosine`` is
    # the query-time filter applied by consumers. Materialising everything
    # down to the floor lets operators A/B different display thresholds
    # without regraphifying — the slow path only runs when the floor
    # itself, top_k, or the embedding text changes.
    semantic_affected: set[str] = set()
    if _cfg.graph_edge_weight_semantic > 0.0:
        sem_pairs = _sem.compute_semantic_edges(
            embed_nodes,
            top_k=_cfg.graph_semantic_top_k,
            min_cosine=_cfg.graph_semantic_build_floor,
            batch_size=_cfg.graph_semantic_batch_size,
            cache_dir=_effective_semantic_cache_dir(_cfg.graph_semantic_cache_dir),
            backend=_cfg.intake_backend,
            model=_cfg.intake_model,
            incremental=incremental,
            persist_cache=persist_semantic_cache,
            affected_out=semantic_affected,
        )
    else:
        sem_pairs = {}

    # ── Phase 4: union all pairs and blend weights per pair ──────────
    tag_sat = max(_cfg.graph_shared_tag_saturation, 1)
    tok_sat = max(_cfg.graph_shared_token_saturation, 1)
    w_sem = _cfg.graph_edge_weight_semantic
    w_tag = _cfg.graph_edge_weight_tags
    w_tok = _cfg.graph_edge_weight_tokens
    w_direct = _cfg.graph_edge_boost_direct_link
    w_source = _cfg.graph_edge_boost_source_overlap
    w_adamic = _cfg.graph_edge_boost_adamic_adar
    w_type = _cfg.graph_edge_boost_type_affinity
    w_usage = _cfg.graph_edge_boost_usage
    w_quality = _cfg.graph_edge_boost_quality

    all_pairs: set[tuple[str, str]] = (
        set(sem_pairs)
        | set(tag_counts)
        | set(token_counts)
        | set(source_counts)
        | direct_pairs
    )
    quality_usage = _quality_usage_signals(QUALITY_SIDECAR_DIR)
    for nid, data in G.nodes(data=True):
        signals = quality_usage.get(nid, {})
        quality = signals.get("quality")
        usage = signals.get("usage")
        data["quality_signal"] = round(quality, 4) if quality is not None else None
        data["usage_signal"] = round(usage, 4) if usage is not None else None
    adamic_scores = _adamic_adar_scores(list(G.nodes), all_pairs)

    # Materialise the target edge set as ``{pair: attr_dict}`` — both
    # the full-build and patch paths consume this same dict, which
    # keeps the blend formula in one place and makes the patch path
    # a pure data op.
    target_edges: dict[tuple[str, str], dict] = {}
    for pair in all_pairs:
        n1, n2 = pair
        sem = sem_pairs.get(pair, 0.0)
        tag = min(tag_counts.get(pair, 0) / tag_sat, 1.0)
        tok = min(token_counts.get(pair, 0) / tok_sat, 1.0)
        direct = 1.0 if pair in direct_pairs else 0.0
        source = min(source_counts.get(pair, 0), 1.0)
        type_affinity = _type_affinity_score(
            str(G.nodes[n1].get("type", "")),
            str(G.nodes[n2].get("type", "")),
        )
        quality = _mean_present(
            quality_usage.get(n1, {}).get("quality"),
            quality_usage.get(n2, {}).get("quality"),
        )
        usage = _mean_present(
            quality_usage.get(n1, {}).get("usage"),
            quality_usage.get(n2, {}).get("usage"),
        )
        adamic = adamic_scores.get(pair, 0.0)
        components = {
            "semantic": w_sem * sem,
            "tags": w_tag * tag,
            "slug_tokens": w_tok * tok,
            "direct_link": w_direct * direct,
            "source_overlap": w_source * source,
            "adamic_adar": w_adamic * adamic,
            "type_affinity": w_type * type_affinity,
            "usage": w_usage * usage,
            "quality": w_quality * quality,
        }
        final = min(sum(components.values()), 1.0)
        if final <= 0.0:
            # Useless edge — only happens when every signal dropped to
            # zero or landed below its floor. Skip materialisation.
            continue
        reasons: list[str] = []
        if sem > 0.0:
            reasons.append("semantic")
        if tag > 0.0:
            reasons.append("tags")
        if tok > 0.0:
            reasons.append("slug-tokens")
        if direct > 0.0:
            reasons.append("direct-link")
        if source > 0.0:
            reasons.append("source-overlap")
        if adamic > 0.0:
            reasons.append("adamic-adar")
        if type_affinity > 0.0:
            reasons.append("type-affinity")
        if usage > 0.0:
            reasons.append("usage")
        if quality > 0.0:
            reasons.append("quality")
        target_edges[pair] = {
            "semantic_sim": round(sem, 4),
            "tag_sim": round(tag, 4),
            "token_sim": round(tok, 4),
            "direct_link": round(direct, 4),
            "source_overlap": round(source, 4),
            "adamic_adar": round(adamic, 4),
            "type_affinity": round(type_affinity, 4),
            "usage_score": round(usage, 4),
            "quality_score": round(quality, 4),
            "final_weight": round(final, 4),
            "weight": round(final, 4),
            "shared_tags": tag_shared.get(pair, []),
            "shared_tokens": token_shared.get(pair, []),
            "shared_sources": source_shared.get(pair, []),
            "edge_reasons": reasons,
            "score_components": {
                key: round(value, 4)
                for key, value in components.items()
                if value > 0.0
            },
        }

    # Decide: full build or patch an existing graph? Patching requires
    # a compatible graph.json on disk AND an incremental run. Anything
    # else falls through to a clean rebuild.
    prior_graph = load_prior_graph() if incremental else None

    # ── Patch-path safety: detect "edge generation parameters changed"
    # since the prior build (e.g. semantic backend went from
    # unavailable → available between runs). The patch path's
    # affected-nodes detector only catches *content* changes; it does
    # NOT catch the case where the same content needs different edges
    # because a signal source was just enabled/disabled. Without this
    # guard, a freshly-computed semantic_sim never lands on edges that
    # the patch path leaves "untouched", and the published graph
    # silently ships with semantic_sim=0 everywhere.
    #
    # The check: if we just computed semantic pairs (len(sem_pairs) > 0)
    # but the prior graph has zero edges with semantic_sim > 0, the
    # prior was built without semantic. Force a full rebuild — the
    # patch path cannot reconcile this without rebuilding every edge.
    if prior_graph is not None and len(sem_pairs) > 0:
        prior_with_sem = sum(
            1 for _, _, d in prior_graph.edges(data=True)
            if d.get("semantic_sim", 0.0) > 0
        )
        if prior_with_sem == 0:
            print(
                "graphify: prior graph has 0 semantic edges but current run "
                f"computed {len(sem_pairs):,} semantic pairs — forcing full "
                "rebuild (patch path cannot reconcile signal-source change).",
                flush=True,
            )
            prior_graph = None
    current_node_info: dict[str, dict] = {
        nid: {
            "label": data.get("label", nid.split(":", 1)[-1]),
            "type": data.get("type", ""),
            "tags": list(data.get("tags", []) or []),
            "source_keys": list(data.get("source_keys", []) or []),
            "direct_targets": list(data.get("direct_targets", []) or []),
            "quality_signal": data.get("quality_signal"),
            "usage_signal": data.get("usage_signal"),
        }
        for nid, data in G.nodes(data=True)
    }
    if prior_graph is not None and incremental:
        affected_nodes = set(semantic_affected)
        affected_nodes.update(
            _metadata_affected_nodes(
                prior_graph=prior_graph,
                current_node_info=current_node_info,
            )
        )
        patched = patch_graph(
            prior_graph,
            current_node_info=current_node_info,
            target_edges=target_edges,
            affected_node_ids=affected_nodes,
        )
        # Patching mutates the prior; swap it in for G so downstream
        # sees the patched graph.
        G = patched
        if _affected_out is not None:
            _affected_out.update(affected_nodes)
    else:
        # Full build path — add every target edge to the fresh graph.
        for pair, attrs in target_edges.items():
            n1, n2 = pair
            G.add_edge(n1, n2, **attrs)
        # Full rebuilds have no per-entity delta; leave _affected_out empty.

    _attach_quality_attrs(G, QUALITY_SIDECAR_DIR)

    # Record the build-time floor on the graph so consumers that
    # inherit a stale graph.json can sanity-check their filter
    # request (filtering below the floor is meaningless — those
    # edges were never materialised).
    G.graph["semantic_build_floor"] = round(_cfg.graph_semantic_build_floor, 4)
    G.graph["semantic_min_cosine_default"] = round(_cfg.graph_semantic_min_cosine, 4)

    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    print(
        f"Edge sources: semantic={len(sem_pairs)}, tag_pairs={len(tag_counts)}, "
        f"token_pairs={len(token_counts)}, source_pairs={len(source_counts)}, "
        f"direct_pairs={len(direct_pairs)} "
        f"(blend: sem={w_sem}, tag={w_tag}, tok={w_tok}; "
        f"boosts: direct={w_direct}, source={w_source}, "
        f"adamic={w_adamic}, type={w_type}, usage={w_usage}, quality={w_quality})"
    )
    print(f"Tag index: {len(tag_index)} unique tags; token index: {len(token_index)} tokens")
    return G, entities


def _metadata_affected_nodes(
    *,
    prior_graph: nx.Graph,
    current_node_info: dict[str, dict],
) -> set[str]:
    """Return nodes whose graph-driving metadata changed.

    Semantic body changes are reported by ``compute_semantic_edges``.
    Tag, type, label, source keys, direct links, and quality/usage
    sidecar signals all affect blended edge weights, so the patch path
    must refresh those incident edges as well.
    """
    affected: set[str] = set()
    for nid, info in current_node_info.items():
        if nid not in prior_graph:
            affected.add(nid)
            continue
        prior = prior_graph.nodes[nid]
        if prior.get("label") != info.get("label"):
            affected.add(nid)
            continue
        if prior.get("type") != info.get("type"):
            affected.add(nid)
            continue
        if set(prior.get("tags", []) or []) != set(info.get("tags", []) or []):
            affected.add(nid)
            continue
        if set(prior.get("source_keys", []) or []) != set(
            info.get("source_keys", []) or []
        ):
            affected.add(nid)
            continue
        if set(prior.get("direct_targets", []) or []) != set(
            info.get("direct_targets", []) or []
        ):
            affected.add(nid)
            continue
        if prior.get("quality_signal") != info.get("quality_signal"):
            affected.add(nid)
            continue
        if prior.get("usage_signal") != info.get("usage_signal"):
            affected.add(nid)
    return affected


def load_prior_graph() -> nx.Graph | None:
    """Load the previous run's graph from ``graph.json``, or None on any issue.

    The canonical on-disk artifact is ``graph.json`` (node-link format).
    ``patch_graph`` uses the loaded graph as the starting point for an
    incremental update; callers that can't load (missing file, corrupt
    JSON, wrong schema, first run) just build from scratch instead.

    SECURITY NOTE: earlier revisions of this function read a
    ``graph.pickle`` sidecar via ``pickle.loads``, which is an RCE
    primitive — a poisoned pickle executes during deserialisation,
    before any type check. Security-auditor finding C-1. The pickle
    path is now permanently removed; a stale ``graph.pickle`` on disk
    is ignored by reads and deleted by the next export. Do not
    reintroduce it — use JSON or a checksummed
    binary format (msgpack) if the JSON parse cost ever matters.
    """
    path = GRAPH_OUT / "graph.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"wiki_graphify: prior graph.json unreadable ({exc}); full rebuild",
            flush=True,
        )
        return None
    # Shape check: networkx node-link documents have a 'nodes' list and
    # an 'edges' (or legacy 'links') list. Anything else is either a
    # garbage file someone dropped in graphify-out/ or an unrelated
    # JSON blob — reject rather than feed to node_link_graph.
    if not isinstance(data, dict):
        return None
    if "nodes" not in data or not isinstance(data["nodes"], list):
        return None
    if "edges" not in data and "links" not in data:
        return None
    try:
        # ``edges="edges"`` matches what export_graph writes. networkx
        # auto-falls-back to the legacy ``links`` key internally when
        # reading, but being explicit here pins the contract.
        graph = nx.node_link_graph(data, edges="edges")
    except (KeyError, TypeError, ValueError) as exc:
        print(
            f"wiki_graphify: prior graph.json rejected by node_link_graph "
            f"({exc}); full rebuild",
            flush=True,
        )
        return None
    if not isinstance(graph, nx.Graph):
        return None
    return graph


def patch_graph(
    prior: nx.Graph,
    *,
    current_node_info: dict[str, dict],
    target_edges: dict[tuple[str, str], dict],
    affected_node_ids: set[str],
) -> nx.Graph:
    """Mutate ``prior`` in-place to match the new graph state.

    Args:
        prior: the previous run's graph (from ``load_prior_graph``).
        current_node_info: ``{node_id: {label, type, tags}}`` for every
            node that should be in the new graph.
        target_edges: ``{(n1, n2): attr_dict}`` for the blended edge
            set produced by ``build_graph`` this run. Keys are
            canonicalised with ``n1 < n2``.
        affected_node_ids: nodes whose incident edges need to be
            refreshed (changed + new + contamination from Option B's
            partition). Edges where BOTH endpoints are outside this
            set are left untouched — they're guaranteed identical
            to the prior run.

    Returns the patched graph (same instance as ``prior``).
    """
    # Node-level delta.
    prior_ids = set(prior.nodes())
    current_ids = set(current_node_info.keys())
    removed_nodes = prior_ids - current_ids
    new_nodes = current_ids - prior_ids

    for nid in removed_nodes:
        prior.remove_node(nid)
    for nid in new_nodes:
        info = current_node_info[nid]
        prior.add_node(
            nid,
            label=info.get("label", nid.split(":", 1)[-1]),
            type=info.get("type", ""),
            tags=list(info.get("tags", [])),
            source_keys=list(info.get("source_keys", [])),
            direct_targets=list(info.get("direct_targets", [])),
            quality_signal=info.get("quality_signal"),
            usage_signal=info.get("usage_signal"),
        )
    # Refresh attrs on existing nodes (tags may have changed).
    for nid in current_ids & prior_ids:
        info = current_node_info[nid]
        prior.nodes[nid]["label"] = info.get("label", nid.split(":", 1)[-1])
        prior.nodes[nid]["type"] = info.get("type", prior.nodes[nid].get("type", ""))
        prior.nodes[nid]["tags"] = list(info.get("tags", []))
        prior.nodes[nid]["source_keys"] = list(info.get("source_keys", []))
        prior.nodes[nid]["direct_targets"] = list(info.get("direct_targets", []))
        prior.nodes[nid]["quality_signal"] = info.get("quality_signal")
        prior.nodes[nid]["usage_signal"] = info.get("usage_signal")

    # Edge delta — only touch edges incident on affected nodes.
    # Pairs where both endpoints are unaffected are guaranteed to
    # have the same blended weight (same semantic_sim from cache,
    # same tag/token overlap) and we skip them.
    affected = affected_node_ids | new_nodes | removed_nodes

    # Remove outdated edges incident on affected nodes. We collect
    # first, mutate after — avoids "dictionary changed size during
    # iteration" on Graph.edges view.
    to_remove: list[tuple[str, str]] = []
    for nid in affected:
        if nid not in prior:
            continue
        for neighbor in list(prior.neighbors(nid)):
            to_remove.append((nid, neighbor))
    for u, v in to_remove:
        if prior.has_edge(u, v):
            prior.remove_edge(u, v)

    # Re-add all target edges where at least one endpoint is affected.
    # Also re-add edges between unaffected-and-in-target pairs to
    # repair any drift — but that's a no-op because those edges
    # survived the remove step and are identical to the prior.
    added = 0
    for (n1, n2), attrs in target_edges.items():
        if n1 not in prior or n2 not in prior:
            continue
        if n1 in affected or n2 in affected:
            prior.add_edge(n1, n2, **attrs)
            added += 1

    print(
        f"patch_graph: removed={len(removed_nodes)} nodes + {len(to_remove)} edges; "
        f"added={len(new_nodes)} nodes + {added} edges; "
        f"untouched={prior.number_of_edges() - added} edges",
        flush=True,
    )
    return prior


def _build_delta(G: nx.Graph, delta_nodes: set[str]) -> dict:
    """Return a JSON-serializable dict of nodes + edges touching ``delta_nodes``.

    When ``delta_nodes`` is empty, returns a shell with ``"full_rebuild": true``
    so consumers know the whole ``graph.json`` is fresh.
    """
    if not delta_nodes:
        return {
            "version": 1,
            "full_rebuild": True,
            "generated": TODAY,
            "node_count": G.number_of_nodes(),
            "edge_count": G.number_of_edges(),
        }
    nodes_out: list[dict] = []
    edges_out: list[dict] = []
    for nid in delta_nodes:
        if nid not in G:
            continue
        attrs = dict(G.nodes[nid])
        nodes_out.append({"id": nid, **attrs})
        for neighbor in G.neighbors(nid):
            # Canonicalise so a neighbor listing doesn't produce
            # duplicates when both endpoints are in delta_nodes.
            u, v = (nid, neighbor) if nid < neighbor else (neighbor, nid)
            edge_attrs = dict(G[u][v])
            edges_out.append({"source": u, "target": v, **edge_attrs})
    # De-dup edges (a pair where both endpoints are in delta_nodes
    # would appear twice otherwise).
    seen: set[tuple[str, str]] = set()
    deduped_edges: list[dict] = []
    for e in edges_out:
        key = (e["source"], e["target"])
        if key in seen:
            continue
        seen.add(key)
        deduped_edges.append(e)
    return {
        "version": 1,
        "full_rebuild": False,
        "generated": TODAY,
        "delta_node_count": len(nodes_out),
        "delta_edge_count": len(deduped_edges),
        "graph_node_count": G.number_of_nodes(),
        "graph_edge_count": G.number_of_edges(),
        "nodes": nodes_out,
        "edges": deduped_edges,
    }


def _remove_stale_pickle_artifact() -> None:
    """Delete the removed graph.pickle sidecar if an older run left it behind."""
    path = GRAPH_OUT / "graph.pickle"
    if not path.exists() and not path.is_symlink():
        return
    try:
        path.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"stale graph pickle artifact could not be removed: {path}",
        ) from exc


def filter_graph_by_min_cosine(G: nx.Graph, min_cosine: float) -> nx.Graph:
    """Return a subgraph view with semantic edges filtered at query time.

    Keeps an edge when ANY of the three signals justifies it:
      - ``semantic_sim`` >= ``min_cosine``, OR
      - ``tag_sim > 0`` (any shared explicit tag), OR
      - ``token_sim > 0`` (any shared slug token)

    This means tag/token-only connections survive any semantic
    threshold — only edges whose sole reason-to-exist is a below-
    threshold cosine disappear. That matches user intent: "show me
    only strong semantic links" shouldn't hide edges that are
    validated by explicit categorical overlap.

    Downstream consumers (resolve_graph, visualize, recommenders)
    should call this with ``cfg.graph_semantic_min_cosine`` at the
    top of their pipeline, then work with the filtered copy.

    Refuses to filter below the graph's build floor — that request
    would return a graph that doesn't contain edges it pretends to
    include at that threshold. The caller either regraphifies with
    a lower build_floor or raises the requested min_cosine.
    """
    build_floor = float(G.graph.get("semantic_build_floor", 0.0))
    if min_cosine < build_floor:
        raise ValueError(
            f"min_cosine ({min_cosine}) is below the graph's build_floor "
            f"({build_floor}); regraphify with a lower floor or raise "
            "min_cosine."
        )
    sub = nx.Graph()
    sub.graph.update(G.graph)
    sub.add_nodes_from(G.nodes(data=True))
    for n1, n2, attrs in G.edges(data=True):
        sem = float(attrs.get("semantic_sim", 0.0))
        tag = float(attrs.get("tag_sim", 0.0))
        tok = float(attrs.get("token_sim", 0.0))
        if sem >= min_cosine or tag > 0.0 or tok > 0.0:
            sub.add_edge(n1, n2, **attrs)
    return sub


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
    """Run community detection.

    Uses Louvain by default — it's near-linear in edge count and finishes
    on a 13K-node / 800K-edge graph in seconds. Falls back to the slower
    greedy-modularity (CNM) algorithm only if the env var
    ``CTX_GRAPH_COMMUNITY=cnm`` is set, since CNM is O(n²) on dense
    graphs and hangs on this dataset (~50min and counting was observed
    on 2026-04-27).
    """
    if G.number_of_nodes() == 0:
        return {}

    algo = os.environ.get("CTX_GRAPH_COMMUNITY", "louvain").lower()
    if algo == "cnm":
        communities_iter = greedy_modularity_communities(
            G, weight="weight", resolution=1.2,
        )
    else:
        # Louvain returns list[set[node]] directly. Resolution=1.2 to
        # match the CNM resolution we shipped previously, so cluster
        # granularity is comparable.
        # seed=42 fixed so output is reproducible across runs.
        communities_iter = louvain_communities(
            G, weight="weight", resolution=1.2, seed=42,
        )

    communities: dict[int, list[str]] = {}
    for i, community in enumerate(communities_iter):
        communities[i] = sorted(community)

    print(f"Communities: {len(communities)} detected (algo={algo})")
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


def _community_tags(G: nx.Graph, members: list[str], *, limit: int = 5) -> list[str]:
    counts: Counter[str] = Counter()
    for nid in members:
        counts.update(
            str(tag)
            for tag in G.nodes[nid].get("tags", [])
            if tag and tag != "uncategorized"
        )
    return [tag for tag, _count in counts.most_common(limit)]


def _reconcile_generated_concept_pages(
    wanted_filenames: set[str],
    *,
    dry_run: bool,
) -> int:
    if not CONCEPTS_DIR.is_dir():
        return 0
    removed = 0
    for page in CONCEPTS_DIR.glob("community-*.md"):
        if page.name in wanted_filenames:
            continue
        try:
            content = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        generated = (
            CONCEPT_GENERATED_MARKER in content
            or "*Generated by wiki_graphify.py" in content
        )
        if not generated:
            continue
        if dry_run:
            print(f"  [DRY RUN] Would remove stale: concepts/{page.name}")
        else:
            page.unlink()
        removed += 1
    return removed


def generate_concept_pages(
    G: nx.Graph,
    communities: dict[int, list[str]],
    dry_run: bool = False,
) -> list[str]:
    """Generate concept pages for each community."""
    if not dry_run:
        CONCEPTS_DIR.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    wanted_filenames: set[str] = set()

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
        safe_name = re.sub(r"[^a-z0-9._-]+", "-", safe_name).strip("-") or f"{cid}"
        filename = f"community-{safe_name}.md"
        wanted_filenames.add(filename)

        # Top members by degree.
        #
        # Prior impl used ``'skills' if 'skill:' in m else 'agents'`` which
        # silently routed every ``mcp-server:`` node into ``entities/agents/``,
        # producing a sea of broken wikilinks once the MCP catalog was
        # ingested. Reuse _entity_wikilink which knows all three types
        # and the MCP shard layout.
        top_members = sorted(members, key=lambda n: G.degree(n), reverse=True)[:20]
        member_link_lines: list[str] = []
        for m in top_members:
            entity_type, _sep, slug = m.partition(":")
            link = _entity_wikilink(entity_type, slug)
            member_link_lines.append(f"- {link}" if link else f"- {m}")
        member_links = "\n".join(member_link_lines)
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
        tags = _community_tags(G, members)

        page = f"""---
title: "{label}"
created: {TODAY}
updated: {TODAY}
type: concept
community_id: {cid}
member_count: {len(members)}
tags: [{', '.join(tags)}]
---

{CONCEPT_GENERATED_MARKER}

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
            safe_atomic_write_text(CONCEPTS_DIR / filename, page, encoding="utf-8")
        created.append(filename)

    removed = _reconcile_generated_concept_pages(wanted_filenames, dry_run=dry_run)
    action = "would create" if dry_run else "created"
    print(f"Concept pages: {len(created)} {action}, {removed} stale removed")
    return created


def _remove_graph_related_block(content: str) -> tuple[str, bool]:
    stripped, count = GRAPH_RELATED_BLOCK_RE.subn("\n", content)
    return re.sub(r"\n{3,}", "\n\n", stripped), bool(count)


def _render_graph_related_block(new_links: list[str]) -> str:
    return "\n".join([GRAPH_RELATED_START, *new_links, GRAPH_RELATED_END])


def inject_community_links(
    G: nx.Graph,
    communities: dict[int, list[str]],
    dry_run: bool = False,
) -> int:
    """Refresh graph-generated top-N neighbor wikilinks on entity pages."""
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

        original_content = page_path.read_text(encoding="utf-8", errors="replace")
        content, had_generated_block = _remove_graph_related_block(original_content)

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

        if not new_links and not had_generated_block:
            continue

        # Refresh only ctx's generated block. Manual links in the same
        # section stay untouched and prevent duplicates.
        section_header = _related_section_header(entity_type)
        insert_text = _render_graph_related_block(new_links) if new_links else ""
        if new_links and section_header in content:
            content = content.replace(
                section_header + "\n",
                section_header + "\n" + insert_text + "\n",
                1,
            )
        elif new_links:
            content = content.rstrip() + f"\n\n{section_header}\n" + insert_text + "\n"

        if content == original_content:
            continue

        if not dry_run:
            safe_atomic_write_text(page_path, content, encoding="utf-8")
        updated += 1

    print(f"Entity pages updated with graph-based wikilinks: {updated}")
    return updated


def export_graph(
    G: nx.Graph,
    communities: dict[int, list[str]],
    *,
    delta_nodes: set[str] | None = None,
) -> None:
    """Export graph as JSON and remove obsolete binary sidecars.

    ``delta_nodes``, when provided, is the set of node IDs that the
    incremental path touched — we also write a ``graph-delta.json``
    containing only those nodes and their incident edges so downstream
    consumers can ingest just the change rather than re-read 130MB.
    """
    GRAPH_OUT.mkdir(parents=True, exist_ok=True)
    _remove_stale_pickle_artifact()

    # Export graph as node-link JSON. Pin the edges key so readers
    # (resolve_graph, wiki_visualize) can rely on it regardless of the
    # networkx version that wrote it — default changed from "links" in
    # <3.0 to "edges" in >=3.0, which silently broke every consumer.
    graph_data = nx.node_link_data(G, edges="edges")
    safe_atomic_write_text(
        GRAPH_OUT / "graph.json",
        json.dumps(graph_data, indent=2, default=str),
        encoding="utf-8",
    )

    # No binary sidecar. An earlier revision wrote ``graph.pickle`` next
    # to this JSON for faster incremental loads, but pickle.loads is an
    # RCE primitive — security-auditor finding C-1 (fixed). The JSON
    # parse overhead (~3s at 13k nodes / 850k edges) is acceptable for
    # the once-per-regraphify load path. ``load_prior_graph`` reads
    # graph.json directly.

    # Delta export — only the nodes touched this run + their incident
    # edges. Written unconditionally (empty when the full run produced
    # the graph from scratch) so downstream consumers can always
    # stat a single file to detect changes.
    delta = _build_delta(G, delta_nodes or set())
    safe_atomic_write_text(
        GRAPH_OUT / "graph-delta.json",
        json.dumps(delta, indent=2, default=str),
        encoding="utf-8",
    )

    # Community labels
    labels = {}
    for cid, members in communities.items():
        labels[cid] = label_community(G, members)

    safe_atomic_write_text(
        GRAPH_OUT / "communities.json",
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

    safe_atomic_write_text(
        GRAPH_OUT / "graph-report.md",
        "\n".join(report_lines),
        encoding="utf-8",
    )
    print(f"Graph exported to {GRAPH_OUT}/")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build knowledge graph from wiki entities")
    parser.add_argument(
        "--wiki-dir",
        type=Path,
        default=None,
        help="Wiki root to graphify (default: ~/.claude/skill-wiki)",
    )
    parser.add_argument("--graph-only", action="store_true", help="Build graph and export only")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    # Incremental vs full: incremental reuses the prior run's per-node
    # top-K where the entity text hasn't changed. Full forces a top-K
    # recompute for every node (but still honors the embedding cache,
    # so the embedding pass is only expensive on first run or after a
    # model/text change).
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--incremental", dest="incremental", action="store_true",
        default=True,
        help="Reuse prior top-K for unchanged nodes (default)",
    )
    mode.add_argument(
        "--full", dest="incremental", action="store_false",
        help="Force a full top-K recompute for every node",
    )
    args = parser.parse_args()

    if args.wiki_dir is not None:
        configure_wiki_dir(args.wiki_dir)

    affected: set[str] = set()
    G, entities = build_graph(
        incremental=args.incremental,
        persist_semantic_cache=not args.dry_run,
        _affected_out=affected,
    )
    communities = detect_communities(G)
    if args.dry_run:
        print(f"  [DRY RUN] Would export graph artifacts to {GRAPH_OUT}/")
    else:
        export_graph(G, communities, delta_nodes=affected)

    if args.graph_only:
        return

    generate_concept_pages(G, communities, args.dry_run)
    inject_community_links(G, communities, args.dry_run)

    print("\nDone. Open wiki in Obsidian to see the graph visualization.")


if __name__ == "__main__":
    main()
