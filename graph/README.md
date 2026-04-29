# Knowledge Graph

Pre-built knowledge graph of **104,066 nodes** and **1,031,011 edges**. The curated core is **13,220 nodes** (1,969 curated skills + 464 agents + 10,786 MCP servers + 1 cataloged harness) with **963,492 edges** across **22 communities** (Louvain). The Skills.sh catalog adds **90,846 first-class remote-cataloged `skill` nodes**, **90,846 skill pages under `entities/skills/skills-sh-*.md`**, and **67,519 sparse metadata edges** to curated entities. Curated-core edges are blended from three signals: semantic cosine (210,248 edges, default weight 0.70), explicit `tags:` overlap (597,017 edges, weight 0.15), and slug-token overlap (314,945 edges, weight 0.15). The current Skills.sh pass is first-class by node type, but not yet full-body semantic: Skills.sh edges still have `semantic_sim=0.0` until the hydration + full regraphify phase fetches upstream SKILL.md bodies. Rebuild the curated core with `python -m ctx.core.wiki.wiki_graphify`, add harnesses with `ctx-harness-add`, then refresh the Skills.sh catalog with `python src/import_skills_sh_catalog.py --from-api-union <raw.json> --update-wiki-tar`.

> **2026-04-29.** Added the cataloged [`text-to-cad`](https://github.com/earthtojake/text-to-cad) harness as a first-class `harness` node with an entity page under `entities/harnesses/text-to-cad.md`. Node count: 104,065 -> **104,066**. Edge count: 1,030,831 -> **1,031,011**. This single-harness pass adds 224 explainable edges: 180 curated-core edges plus 44 remote-cataloged Skills.sh edges. It does not add full-body Skills.sh semantic edges; those require the later hydration + graphify pass.

> **2026-04-29.** Added the curated `find-skills` workflow and mirrored it into `converted/find-skills/SKILL.md`, so fresh clones can install it from the shipped wiki. Curated node count: 13,218 -> **13,219**. Curated edge count: 963,068 -> **963,312**. The tarball now also carries Skills.sh catalog coverage as **90,846 remote-cataloged `skill` nodes**, matching skill pages under `entities/skills/skills-sh-*.md`, and `external-catalogs/skills-sh/catalog.json`.

> **2026-04-27.** Two imports landed this day:
> - **[mattpocock/skills](https://github.com/mattpocock/skills)** — 21 opinionated behavior skills (TDD, domain-model, ubiquitous-language, github-triage, plus 17 more) under the `mattpocock-` prefix.
> - **[designdotmd.directory](https://designdotmd.directory)** — 156 DESIGN.md visual-identity files (color tokens, typography, spacing, component tokens, rationale) under the `designdotmd-` prefix. These are *reference designs* (data the agent reads when asked to build a UI), not behavior skills.
>
> Node count: 13,041 → **13,218** (+177 = 21 mattpocock + 156 designdotmd). Edge count: 847,207 → **963,068** (+106,702 from designdotmd-driven tag/token overlap with the existing catalog).

> **Bugs fixed in this release:**
>
> - *Patch-path edge silence.* `wiki_graphify`'s incremental path used to keep stale edges when the semantic backend went from unavailable → available between runs (no node content changed, so the affected-set was empty, so freshly-computed semantic pairs never landed). Fixed: the build now detects "prior graph has 0 semantic edges but current run computed semantic pairs" and forces a full rebuild. Regression test added in `test_wiki_graphify_density.py`.
> - *CNM community-detection hang.* The legacy CNM (greedy modularity) algorithm took 50+ minutes on the 13K-node graph stuck in `_siftup`. Fixed: switched default to **Louvain** (`networkx.algorithms.community.louvain_communities`) with deterministic seed=42. CNM still available behind `CTX_GRAPH_COMMUNITY=cnm` for legacy parity.

> **Edge-count history.** v0.5.x shipped a stale `graph.json` with 642K edges from a build path that no longer existed; the live rebuild silently produced only 861 edges because `DENSE_TAG_THRESHOLD=20` dropped every tag with more than 20 nodes. v0.6.0 fixed the threshold + added slug-token pseudo-tags → 454K-edge graph. v0.7 ingested 10,786 MCP servers from pulsemcp (93% enriched with github_url + stars), added sentence-embedding semantic edges with a configurable `build_floor=0.50` / `min_cosine=0.80` split, wired the alive-loop cumulative-threshold trigger, and shipped install/uninstall CLIs for all three entity types → 847K edges. The curated-core rebuild adds 21 mattpocock skills + 156 designdotmd designs, fixes the patch-path bug, and switches to Louvain → **963K curated edges**. The current Skills.sh remote-cataloged pass adds 67,519 sparse metadata edges, and the `text-to-cad` harness pass brings the tarball to **1,031,011 total edges**; the next hydration pass must fetch SKILL.md bodies and run the normal semantic graph builder across those nodes.

## Files

| File | Size | Contents |
|------|------|----------|
| `wiki-graph.tar.gz` | ~46 MB | **Full wiki** - entity cards, 1,773 converted skill bodies, 430 mirrored agent bodies, 104K-node knowledge graph, concept pages, catalog, one cataloged harness, and first-class remote-cataloged Skills.sh skill pages |
| `skills-sh-catalog.json.gz` | ~4.1 MB | Compressed Skills.sh catalog (90,846 observed entries, install commands, detail URLs, inferred tags, overlap metadata) |
| `communities.json` | ~500 KB | 22 detected communities (Louvain) with labels + member lists |
| `viz-overview.html` / `.png` | — | Plotly-rendered overview of the full graph |
| `viz-python.html` | — | Python-skills sub-view |
| `viz-security.html` / `.png` | — | Security-skills sub-view |
| `viz-ai-agents.html` | — | AI agents sub-view |
| `sample-top60.html` | — | Top-60-by-degree nodes, interactive |

### What's inside `wiki-graph.tar.gz`

- `entities/skills/` - **92,815** skill entity pages: 1,969 curated ctx skills plus 90,846 remote-cataloged Skills.sh pages under the `skills-sh-` prefix
- `entities/agents/` — **464** agent entity pages
- `entities/mcp-servers/<shard>/` — **10,786** MCP entity pages (sharded by first-char to keep dirs scannable)
- `entities/harnesses/` - **1** harness entity page (`text-to-cad`)
- `concepts/` - community concept pages generated from the current Louvain labels
- `converted/` - **1,773** skill bodies ready for `ctx-skill-install`, including `converted/find-skills/SKILL.md`
- `converted-agents/` — **430** agent bodies ready for `ctx-agent-install`
- `graphify-out/graph.json` - full knowledge graph (104,066 nodes, 1,031,011 edges), including the curated core, cataloged harnesses, and remote-cataloged Skills.sh skill nodes
- `graphify-out/communities.json` - community detection results (22 communities, Louvain; top 5 cover 62.2% of nodes)
- `external-catalogs/skills-sh/catalog.json` — Skills.sh catalog (90,846 observed entries; site reported 90,991 during the clean refresh), including graph node IDs, entity paths, install commands, duplicate hints, and quality signals
- `external-catalogs/skills-sh/summary.json` and `README.md` — fetch/coverage/overlap metadata for the catalog
- `catalog.md` — bulk listing of skills / agents / MCPs
- `SCHEMA.md`, `index.md`, `log.md` — wiki infrastructure
- `.obsidian/` — Obsidian vault config, so the extracted tree opens as a graph directly in Obsidian

Excluded to keep the tarball under GitHub's 100MB file limit (all regenerable on first local run): `raw/` (pulsemcp HTML cache, ~700MB), `.embedding-cache/` (sentence-transformer vectors, ~66MB), `.ingest-checkpoint/`, `.enrich-checkpoint/`, `graphify-out/graph-delta.json`.

## Usage

### Extract the wiki

```bash
# Extract to ~/.claude/skill-wiki/
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

> **Windows / Git-Bash / MSYS:** pass `--force-local` so `tar` doesn't parse `c:` as a remote host: `tar --force-local xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/`.

This gives you:
- Every curated entity (skill / agent / MCP / harness) plus every remote-cataloged Skills.sh skill page browsable as frontmatter-rich markdown
- Installable content for every short and long skill + every agent (`ctx-skill-install`, `ctx-agent-install`)
- The full knowledge graph (`graphify-out/graph.json`) and community detection (`communities.json`)
- An Obsidian vault — open the extracted dir in Obsidian and the graph view renders directly

### Load the graph in Python

```python
import json
from pathlib import Path
from networkx.readwrite import node_link_graph

raw = json.loads(Path("~/.claude/skill-wiki/graphify-out/graph.json").expanduser().read_text())
# Auto-detect the NetworkX 2.x "links" vs 3.x "edges" schema.
# v0.6.x graphs used "links"; v0.7+ uses "edges".
edges_key = "links" if "links" in raw else "edges"
G = node_link_graph(raw, edges=edges_key)

# 104,066 nodes, 1,031,011 edges
print(G.number_of_nodes(), G.number_of_edges())

# Find skills related to "fastapi"
fastapi = "skill:fastapi-pro"
neighbors = sorted(G.neighbors(fastapi),
                   key=lambda n: G[fastapi][n]["weight"], reverse=True)[:10]
for n in neighbors:
    print(f"  {G.nodes[n]['label']} (weight={G[fastapi][n]['weight']})")
```

Or just use the dashboard:

```bash
ctx-monitor serve
# then open http://127.0.0.1:8765/graph?slug=fastapi-pro
```

### Open in Obsidian

The extracted wiki is an Obsidian-compatible vault. Entity pages use `[[wikilinks]]` for cross-references. Open the directory in Obsidian and use the graph view to explore visually.

## Rebuild

After adding new skills, agents, MCP entities, or harnesses (or changing the wiki):

```bash
python src/wiki_batch_entities.py --all          # regenerate entity cards
python -m ctx.core.wiki.wiki_graphify            # incremental by default; --full to force
ctx-dedup-check --threshold 0.85                 # pre-ship dedup gate (flag-only, NEVER drops)
ctx-tag-backfill                                 # report-only by default; --apply to write
```

### Pre-ship gates

Two gates run before the tarball is repackaged. Both are advisory: they
generate local review reports and never auto-modify the catalog. Those
reports are intentionally ignored by git because they can include local
filesystem paths and human review notes.

**`ctx-dedup-check`** — finds entity pairs (skill ↔ skill, skill ↔ agent,
skill ↔ MCP, agent ↔ agent, agent ↔ MCP, MCP ↔ MCP) with cosine
similarity ≥ 0.85. Emits ignored local files under `graph/`, including a
top-results markdown report and gzipped JSON sidecar. Incremental: a
`dedup-state.json` next to the embeddings cache means follow-up runs only
re-check pairs involving entities whose content changed. Pairs that are
legitimately distinct can be added to
`.dedup-allowlist.txt` to suppress them in future reports without
silencing the underlying detection.

**`ctx-tag-backfill`** — finds skills/agents with empty or missing
`tags:` frontmatter and proposes a backfill set drawn from the slug
tokens, body keywords, and the existing tag vocabulary. Report-only by
default; pass `--apply` to write. The generated markdown and JSON reports
are ignored by git. Backfills are additive: the gate never removes or
rewrites existing tags.

Then re-archive. The pre-commit hook does this automatically when the wiki drifts beyond the tarball; the manual command below mirrors its exclusions:

```bash
cd ~/.claude/skill-wiki
tar --force-local -czf /path/to/ctx/graph/wiki-graph.tar.gz \
    --exclude='.trash' \
    --exclude='__pycache__' \
    --exclude='./raw' \
    --exclude='./.embedding-cache' \
    --exclude='./.ingest-checkpoint' \
    --exclude='./.enrich-checkpoint' \
    --exclude='./graphify-out/graph-delta.json' \
    --exclude='./graphify-out/graph.pickle' \
    .
```

The exclusions keep the tarball under GitHub's 100MB file limit (raw/ alone is ~700MB of regenerable pulsemcp HTML). `--force-local` tells MSYS `tar` on Windows not to parse `c:` as a remote host. Non-Windows users can drop that flag.
