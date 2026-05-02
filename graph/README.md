# Knowledge Graph

Pre-built knowledge graph of **104,079 nodes** and **2,960,215 edges** across **53 communities** (Louvain). The curated core is **13,233 nodes** (1,969 curated skills + 464 agents + 10,787 MCP servers + 13 cataloged harnesses). The Skills.sh catalog contributes **90,846 first-class `skill` nodes**, **90,846 skill entity pages under `entities/skills/skills-sh-*.md`**, and **89,461 hydrated installable Skills.sh `SKILL.md` files** under `converted/skills-sh-*/`, with the **28,611** long entries converted to gated micro-skill orchestrators. Edges are blended from three signals: semantic cosine (**1,707,435** edges, default weight 0.70), explicit `tags:` overlap (**920,686** candidate pairs, weight 0.15), and sparse slug-token overlap (**442,556** candidate pairs, weight 0.15). Skills.sh is full-body semantic: **1,525,295** Skills.sh-incident edges have non-zero `semantic_sim`, including **1,437,138** Skills.sh-to-Skills.sh semantic edges. Rebuild with `python -m ctx.core.wiki.wiki_graphify`, add harnesses with `ctx-harness-add`, then refresh the Skills.sh catalog with `python src/import_skills_sh_catalog.py --from-api-union <raw.json> --update-wiki-tar`.

Runtime recommendation is intentionally split into two paths: execution
surfaces recommend only skills, agents, and MCP servers; custom/API/local model
onboarding recommends harnesses from the same graph catalog with the higher
harness match floor in `config.json`.

> **2026-04-30.** Completed the Skills.sh full-body semantic regraph. That build had **2,881,027** edges, including **1,626,632** semantic edges. Skills.sh nodes had **1,451,838** non-zero semantic incident edges. The slug-token dense threshold was tightened from 500 to 30 because hydrated Skills.sh slugs created 13M+ low-signal token-only pairs and made export fail; semantic top-K and explicit tags are now the primary large-scale signals.

> **2026-05-01.** Converted the hydrated Skills.sh corpus to micro-skill form. All **89,461** hydrated Skills.sh `SKILL.md` files are now under the 180-line loader threshold; **28,611** long bodies were split into gated pipeline stages for loading. The full-body semantic graph was rebuilt from preserved source material before packaging, but `SKILL.md.original` backups are not shipped in `wiki-graph.tar.gz`. Generated stage/reference markdown is bounded to 40 lines and raw PHP openers are defanged in generated markdown. The graph now has **2,960,189** edges, including **1,707,435** semantic edges.

> **2026-05-02.** Added [GitNexus](https://github.com/abhigyanpatwari/GitNexus) as a first-class cataloged MCP server entity, linked to its Skills.sh GitNexus skill pages and related architecture/refactoring agents. Node count: 104,078 -> **104,079**. Edge count: 2,960,189 -> **2,960,215**. The GitNexus node has 26 incident cross-type edges and an MCP quality score of 0.8997 (grade A). Its PolyForm Noncommercial license is recorded in frontmatter, so install decisions remain explicit.

> **2026-04-29.** Expanded the harness catalog to 13 first-class `harness` nodes/pages: LangGraph, CrewAI, AutoGen, Google ADK, Semantic Kernel, Mastra, Pydantic AI, Haystack, OpenAI Agents SDK, LiteLLM, Langfuse, AgentOps, and [`text-to-cad`](https://github.com/earthtojake/text-to-cad). Node count: 104,066 -> **104,078**. Edge count: 1,031,011 -> **1,033,253**. Harness incident edges now total 2,700: 2,411 curated-core edges plus 289 remote-cataloged Skills.sh metadata edges.

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

> **Edge-count history.** v0.5.x shipped a stale `graph.json` with 642K edges from a build path that no longer existed; the live rebuild silently produced only 861 edges because `DENSE_TAG_THRESHOLD=20` dropped every tag with more than 20 nodes. v0.6.0 fixed the threshold and added slug-token pseudo-tags, reaching a 454K-edge graph. v0.7 ingested 10,786 MCP servers from pulsemcp, added sentence-embedding semantic edges with a configurable `build_floor=0.50` / `min_cosine=0.80` split, wired the alive-loop cumulative-threshold trigger, and shipped install/uninstall CLIs. The curated-core rebuild added 21 mattpocock skills + 156 designdotmd designs, fixed the patch-path bug, and switched to Louvain, bringing the graph to 963K curated edges. The Skills.sh catalog/harness passes brought the graph to 1.03M edges. The full-body Skills.sh semantic regraph reached **2.88M** edges; the micro-skill conversion rebuild now ships **2.96M** edges with semantic top-K as the dominant large-scale signal.

## Files

| File | Size | Contents |
|------|------|----------|
| `wiki-graph.tar.gz` | ~336 MiB | **Full wiki** - entity cards, 91,234 converted skill bodies, 430 mirrored agent bodies, 104K-node / 3.0M-edge knowledge graph, concept pages, catalog, 13 cataloged harnesses, and first-class hydrated Skills.sh installable pages |
| `skills-sh-catalog.json.gz` | ~11.3 MiB | Compressed Skills.sh catalog (90,846 observed entries, install commands, detail URLs, inferred tags, overlap metadata) |
| `communities.json` | ~6.6 MiB | 53 detected communities (Louvain) with labels + member lists |
| `viz-overview.html` / `.png` | — | Plotly-rendered overview of the full graph |
| `viz-python.html` | — | Python-skills sub-view |
| `viz-security.html` / `.png` | — | Security-skills sub-view |
| `viz-ai-agents.html` | — | AI agents sub-view |
| `sample-top60.html` | — | Top-60-by-degree nodes, interactive |

### What's inside `wiki-graph.tar.gz`

- `entities/skills/` - **92,815** skill entity pages: 1,969 curated ctx skills plus 90,846 remote-cataloged Skills.sh pages under the `skills-sh-` prefix
- `entities/agents/` — **464** agent entity pages
- `entities/mcp-servers/<shard>/` — **10,787** MCP entity pages (sharded by first-char to keep dirs scannable)
- `entities/harnesses/` - **13** harness entity pages
- `concepts/` - community concept pages generated from the current Louvain labels
- `converted/` - **91,234** skill bodies ready for `ctx-skill-install`, including **89,461** hydrated Skills.sh `SKILL.md` files. Long entries over the configured loader threshold are gated micro-skill orchestrators; no `SKILL.md.original` backups are shipped
- `converted-agents/` — **430** agent bodies ready for `ctx-agent-install`
- `graphify-out/graph.json` - full knowledge graph (104,079 nodes, 2,960,215 edges), including the curated core, cataloged harnesses, and full-body semantic Skills.sh skill nodes
- `graphify-out/communities.json` - community detection results (53 communities, Louvain)
- `external-catalogs/skills-sh/catalog.json` — Skills.sh catalog (90,846 observed entries; site reported 90,991 during the clean refresh), including graph node IDs, entity paths, install commands, duplicate hints, and quality signals
- `external-catalogs/skills-sh/summary.json` and `README.md` — fetch/coverage/overlap metadata for the catalog
- `catalog.md` — bulk listing of skills / agents / MCPs
- `SCHEMA.md`, `index.md`, `log.md` — wiki infrastructure
- `.obsidian/` — Obsidian vault config, so the extracted tree opens as a graph directly in Obsidian

Excluded to keep the tarball reviewable (all regenerable on first local run): `raw/` (pulsemcp HTML cache, ~700MB), `.embedding-cache/` (sentence-transformer vectors + top-K state, hundreds of MB), `.ingest-checkpoint/`, `.enrich-checkpoint/`, `graphify-out/graph-delta.json`, and micro-skill `SKILL.md.original` backups.

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
- Installable content for every curated short/long skill, 89,461 hydrated Skills.sh `SKILL.md` files, and every mirrored agent (`ctx-skill-install`, `ctx-agent-install`)
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

# 104,079 nodes, 2,960,215 edges
print(G.number_of_nodes(), G.number_of_edges())

# Find entities related to "fastapi"
fastapi = "skill:fastapi-pro"
neighbors = sorted(G.neighbors(fastapi),
                   key=lambda n: G[fastapi][n]["weight"], reverse=True)[:10]
for n in neighbors:
    edge = G[fastapi][n]
    print(f"  {G.nodes[n]['label']} (weight={edge['weight']})")
```

Or just use the dashboard:

```bash
ctx-monitor serve
# then open http://127.0.0.1:8765/graph?slug=fastapi-pro
```

### Open in Obsidian

The extracted wiki is an Obsidian-compatible vault. Entity pages use `[[wikilinks]]` for cross-references. Open the directory in Obsidian and use the graph view to explore visually.

## Rebuild

After adding or changing skills, agents, MCP entities, or harnesses:

```bash
python src/wiki_batch_entities.py --all          # skills/agents only; MCPs/harnesses already write entity pages
python -m ctx.core.wiki.wiki_graphify            # rebuild graph + communities; --full to force semantic top-K
ctx-dedup-check --threshold 0.85                 # pre-ship dedup gate (flag-only, NEVER drops)
ctx-tag-backfill                                 # report-only by default; --apply to write
python src/render_graph_viz.py                   # refresh graph/*.html and overview PNG snapshots
```

`nashsu/llm_wiki` was reviewed as a design reference for source traceability,
ingest queues, graph insights, and budgeted retrieval. Its GPLv3 license is
not compatible with copying code into this MIT repo, so ctx should adopt only
independently implemented ideas.

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

Then re-archive. The pre-commit hook only reminds you when the tracked graph
artifacts may be stale; graph archives are refreshed by explicit commands so
the exclusion policy is visible and repeatable:

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
    --exclude='*.original' \
    .
```

The exclusions keep the tarball under GitHub's 100MB file limit (raw/ alone is ~700MB of regenerable pulsemcp HTML). `--force-local` tells MSYS `tar` on Windows not to parse `c:` as a remote host. Non-Windows users can drop that flag.
