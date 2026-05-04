# Knowledge graph

A pre-built weighted graph of skills, agents, MCP servers, and cataloged
harnesses in the ctx ecosystem, shipped as `graph/wiki-graph.tar.gz`.
The on-disk JSON and `resolve_graph` Python API are harness-aware, including
plain-slug graph walks from `harness:<slug>` nodes. `ctx-monitor`
exposes skill/agent/MCP/harness wiki and graph views; harness install,
update, load/unload, and quality scoring remain CLI/API workflows.

## What's in it

Authoritative numbers from the shipped tarball. The curated-core snapshot
is **13,233 nodes** (1,969 curated skills + 464 agents + 10,787 MCP servers
+ 13 harnesses). Harness pages under `entities/harnesses/` are ingested into
local rebuilds and the separate harness-catalog recommendation path. The
tarball also carries **89,463 body-backed Skills.sh `skill` nodes**,
matching skill pages under `entities/skills/skills-sh-*.md`. **89,463**
hydrated Skills.sh bodies are shipped as installable `SKILL.md` files under
`converted/skills-sh-*/`; the **28,611** entries over the configured line
limit were converted to gated micro-skill orchestrators. Full original bodies
are used during graph rebuilds for semantic similarity, but
`SKILL.md.original` backups are omitted from the shipped tarball.

| | Count |
|---|---:|
| Total nodes | **102,696** |
| Curated core nodes | **13,233** (1,969 skills + 464 agents + 10,787 MCP servers + 13 harnesses) |
| Remote-cataloged Skills.sh skill nodes | **89,463** (`skill`, `status=remote-cataloged`, body-backed) |
| Total edges | **2,900,834** |
| Skills.sh incident edges | **2,605,971** |
| Skills.sh semantic incident edges | **1,500,685** |
| Communities | **52** (Louvain) |
| Edge sources (overlap-deduped) | semantic 1,682,825 - tag 891,684 - token 433,074 |
| Cross-type edges (skill <-> agent) | ~65K |
| Cross-type edges (skill <-> MCP) | ~41K |
| Cross-type edges (agent <-> MCP) | ~223 |
| Harness edges | **3,289** |
| Skills.sh catalog | **89,463** observed body-backed entries (`external-catalogs/skills-sh/catalog.json` + `entities/skills/skills-sh-*.md`) |

## Install

Extract the tarball into your `~/.claude/skill-wiki/` to get a
ready-to-query graph plus every shipped skill/agent/MCP entity page,
cataloged harness pages when present, remote-cataloged Skills.sh skill
pages, concept pages, and converted micro-skill pipelines. The extracted
tree also includes the Skills.sh catalog JSON used by the shared
recommender:

```bash
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

The extracted tree also opens directly as an Obsidian vault — the
`.obsidian/` config ships inside the tarball — so you can use
Obsidian's native graph view if you prefer it to the web dashboard.

## How edges are built

Edges are built and explained by the `ctx-wiki-graphify` console script
(`ctx.core.wiki.wiki_graphify`). A pair must first have at least one base
signal:

1. **Semantic cosine** — when the embedding backend is available, entity
   text is embedded and semantic neighbors above the configured build floor
   contribute weighted edges.
2. **Explicit frontmatter tags** — each entity page's YAML `tags:`
   list contributes edges between every pair of entities that share
   a tag. Popular tags capped at 500 nodes to avoid noise-floor
   "everything connects to everything" mega-buckets like `typescript`
   or `frontend`.
3. **Slug-token pseudo-tags** — each hyphenated slug contributes its
   tokens as implicit tags. `fastapi-pro` contributes `fastapi`;
   `python-patterns` contributes `python` and `patterns`. A stop-word
   filter drops generic tokens like `skill`, `agent`, `pro`, `expert`,
   `core` so they don't over-connect the graph.
4. **Source overlap** — pages with the same high-specificity source URL,
   repository URL, homepage, detail URL, or package URL can connect even
   when their tags differ. Dense source buckets are skipped.
5. **Direct wikilinks** — explicit entity links such as
   `[[entities/agents/code-reviewer]]` create a direct graph edge.

Edge `weight` is the final blended strength. Semantic, tag, and token
weights form the base blend from `config.json`; source overlap and direct
links add configured boosts. Existing edges can also receive explainable
ranking boosts from Adamic-Adar shared-neighbor structure, type affinity,
usage telemetry, and quality scores. Those boost-only signals do not create
edges by themselves.

Edge metadata keeps the ingredients explainable: `semantic_sim`,
`shared_tags`, `shared_tokens`, `shared_sources`, `direct_link`,
`adamic_adar`, `type_affinity`, `usage_score`, `quality_score`,
`edge_reasons`, and `score_components`. Hydrated Skills.sh records use their
full source bodies during graph rebuilds, so long converted entries keep
full-body similarity even though the shipped installable `SKILL.md` files are
short gated loaders. The raw `SKILL.md.original` backups are build inputs, not
tarball members.

## Communities

After edges are built, `wiki_graphify` runs NetworkX's Louvain
community detection (`resolution=1.2`, `seed=42` for determinism).
The result is **52 communities** ranging from single-member isolated
specialists to several thousand members in broad clusters like
`Community + Official + AI`. Each community also gets an auto-generated
`concepts/<community>.md` wiki page summarizing its members and top
shared tags.

The legacy CNM ("greedy modularity") algorithm is still available
behind `CTX_GRAPH_COMMUNITY=cnm` — it's deterministic but O(n²) on
dense graphs and hangs on the live 13K-node dataset (~50min run was
killed on 2026-04-27 inside the priority-queue siftup). Louvain is
the default because it finishes in seconds and produces equivalent
quality clusters for the recommendation use case.

## Querying the graph

### Via the dashboard

```bash
ctx-monitor serve              # http://127.0.0.1:8765
```

Then open `/graph?slug=<entity-slug>&type=<entity-type>` for a
cytoscape neighborhood view, or
`/api/graph/<slug>.json?type=<entity-type>&hops=1&limit=40` for the
dashboard-shaped JSON. The `type` query is optional for unique slugs and
recommended for duplicate slugs such as `langgraph`. See the
[dashboard reference](dashboard.md) for the full route catalogue.

### Via Python

```python
import json
from pathlib import Path
from networkx.readwrite import node_link_graph

raw = json.loads(
    Path("~/.claude/skill-wiki/graphify-out/graph.json").expanduser().read_text()
)
edges_key = "links" if "links" in raw else "edges"
G = node_link_graph(raw, edges=edges_key)

# 102,696 nodes, 2,900,834 edges
print(G.number_of_nodes(), G.number_of_edges())

# Find entities related to 'fastapi-pro' by edge weight
seed = "skill:fastapi-pro"
neighbors = sorted(
    G.neighbors(seed),
    key=lambda n: G[seed][n]["weight"],
    reverse=True,
)[:10]
for n in neighbors:
    shared = G[seed][n].get("shared_tags", [])
    print(f"  w={G[seed][n]['weight']:>2}  {G.nodes[n]['label']:<40}  {shared[:3]}")
```

The node-link JSON schema's edges key is auto-detected (legacy
NetworkX 2.x used `"links"`; current versions default to `"edges"`).
The helper `resolve_graph.load_graph()` does this for you.

### Via recommendation paths

The graph backs two recommendation paths:

- Execution recommendation surfaces (`ctx.recommend_bundle`, MCP
  `ctx__recommend_bundle`, generic harness tools, Claude Code hook
  suggestions, and repo-scan advisory output) share
  `ctx.core.resolve.recommendations.recommend_by_tags` for skills,
  agents, and MCP servers. That engine ranks candidates by
  slug-token matches, tag overlap, graph degree, and semantic-cache
  signals when available. Skills.sh results are `skill` nodes with
  `source_catalog=skills.sh`, `detail_url`, `install_command`, duplicate
  hints, gated micro-skill loaders when over the line threshold, and
  quality/security metadata. If an older
  extracted wiki has the Skills.sh catalog JSON but no graph nodes for
  those records, the same recommender falls back to the catalog file.
- Harness recommendations are a separate catalog path for custom/API/local
  model onboarding (`ctx-init --model-mode custom ...`) and
  `ctx-harness-install`. They use the same graph catalog filtered to
  `harness` nodes and the higher harness match floor from `config.json`.
- Repository scans still start from stack detections, then turn that profile
  into the same tag/query bundle used by the execution recommender. If a
  shipped graph is unavailable, scan output falls back to the legacy installed
  skill resolver so a plain profile scan remains useful. Harnesses are
  intentionally not emitted from repo scans or Claude Code hook bundles.

This split is intentional: execution surfaces need identical ranking and a
small top-K, while harness choice changes the model runtime itself and belongs
in an explicit onboarding/install flow.

### LLM-wiki design references

ctx follows Karpathy's LLM-wiki pattern. We also reviewed
[`nashsu/llm_wiki`](https://github.com/nashsu/llm_wiki) as a design reference
for source traceability, persistent ingest queues, graph insights, and
budgeted token/vector/graph retrieval. That repository is GPLv3, while ctx is
MIT, so ctx can use those ideas as product inspiration but must not copy or
vendor its code or assets.

## Rebuilding

After you add a skill, agent, MCP server, or harness entity page:

```bash
ctx-wiki-graphify          # rebuild entity graph + communities
```

The pre-commit hook (`.githooks/pre-commit`) re-runs this
automatically when `skills/` or `agents/` are staged, and repacks
the tarball on disk so `README.md` numbers never drift. Run
`ctx-wiki-graphify` directly for MCP server or harness catalog changes
if your hook config does not include those paths.

## Edge-count history

| Version | Edges | Note |
|---|---|---|
| v0.5.x | 642K (stale) / 861 (live) | Bundle had stale 642K; live rebuild silently produced 861 because `DENSE_TAG_THRESHOLD=20` dropped every popular tag. |
| v0.6.0 | 454,719 | Threshold raised to 500, multi-line YAML lists parsed, slug-token pseudo-tags added. |
| v0.7.x | 847,207 | Pulsemcp ingest added 10,786 MCP server nodes; sentence-embedding semantic edges added. |
| 2026-04-27 graph rebuild pass | **963,068** | +21 mattpocock skills, +156 designdotmd designs (+106,702 edges); patch-path bug fixed (graphify now forces full rebuild when prior graph has 0 semantic edges but current run computed semantic pairs); community detection switched from CNM to Louvain. |
| 2026-04-29 Skills.sh remote-cataloged pass | **1,030,831** | +90,846 first-class `skill` nodes, +90,846 skill pages, and +67,519 sparse duplicate/tag metadata edges to the curated graph. Full-body semantic edges are intentionally deferred to the hydration pass. |
| 2026-04-29 text-to-cad harness pass | **1,031,011** | +1 first-class `harness` node, +1 harness page, and +224 explainable harness edges, including 44 remote-cataloged Skills.sh edges. |
| 2026-04-29 curated harness catalog pass | **1,033,253** | +12 first-class `harness` nodes/pages for LangGraph, CrewAI, AutoGen, Google ADK, Semantic Kernel, Mastra, Pydantic AI, Haystack, OpenAI Agents SDK, LiteLLM, Langfuse, and AgentOps; harness incident edges now total 2,700. |
| 2026-04-30 Skills.sh semantic hydration pass | **2,881,027** | +full-body semantic edges for hydrated Skills.sh records; semantic top-K became the dominant large-scale signal. |
| 2026-05-01 Skills.sh micro-skill pass | **2,960,189** | Enforced the <=180-line loader threshold across 89,461 hydrated Skills.sh `SKILL.md` files, converted 28,611 long bodies into gated micro-skill orchestrators, used full originals for semantic graphing, excluded `.original` backups from the shipped tarball, bounded generated stage/reference files to 40 lines, and rebuilt the graph. |
| 2026-05-02 GitNexus MCP pass | **2,960,215** | Added GitNexus as a cataloged MCP server entity with 26 cross-type edges to its Skills.sh skill pages and related architecture/refactoring agents; semantic edge count unchanged. |
| 2026-05-04 v0.7.3 artifact refresh | **2,960,215** | Hydrated one recoverable Skills.sh command-injection-testing body, raising hydrated Skills.sh `SKILL.md` files to 89,463; generated micro-skill markdown now defangs high-risk command-injection payloads before packaging. Graph topology unchanged. |
| 2026-05-04 body-backed Skills.sh prune | **2,900,834** | Removed 1,383 Skills.sh records that had no packaged `SKILL.md` body and no parseable Skills.sh prose body. Remaining Skills.sh catalog entries, graph nodes, entity pages, and converted `SKILL.md` bodies are all **89,463**. |

The full audit history lives in `CHANGELOG.md`. The current build is
fully reproducible from the wiki content.

## Pre-ship gates

Two advisory gates run before the tarball is repackaged. Both produce
review reports and never auto-modify the catalog.

- **`ctx-dedup-check`** — flags entity pairs (skill ↔ skill, skill ↔
  agent, skill ↔ MCP, agent ↔ agent, agent ↔ MCP, MCP ↔ MCP) at or
  above 0.85 cosine similarity. Incremental: keeps a `dedup-state.json`
  next to the embedding cache, so follow-up runs only re-check pairs
  involving entities whose content changed. Allowlist support via
  `.dedup-allowlist.txt`. The current snapshot has 15,976 findings,
  most of which are within-MCP near-duplicates (multiple wrappers
  around the same upstream service).
- **`ctx-tag-backfill`** — finds skills/agents with empty `tags:`
  frontmatter and proposes a backfill drawn from slug tokens, body
  keywords, and the existing tag vocabulary. Report-only by default;
  pass `--apply` to write. Backfills are additive only.
