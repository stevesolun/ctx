# Knowledge Graph Artifacts

This directory ships the pre-built ctx LLM-wiki and knowledge graph.

Current snapshot:

- **102,696 graph nodes**
- **2,900,834 graph edges**
- **52 Louvain communities**
- **91,432 skill entity pages**: 1,969 curated/imported skills plus 89,463 body-backed Skills.sh skills
- **464 agent pages**
- **10,787 MCP server pages**
- **13 harness pages**
- **89,463 hydrated Skills.sh `SKILL.md` bodies**
- **28,612 long Skills.sh bodies converted through the micro-skill gate**

The runtime recommendation paths use this graph in two ways:

- Development recommendations return skills, agents, and MCP servers only.
- Custom/API/local model onboarding recommends harnesses from the harness catalog using the higher harness fit floor in `src/config.json`.

## Files

| File | Contents |
|---|---|
| `wiki-graph.tar.gz` | Full LLM-wiki: entity pages, converted skill bodies, mirrored agent bodies, concept pages, `graphify-out/graph.json`, communities, external catalogs, and Obsidian metadata |
| `skills-sh-catalog.json.gz` | Compressed Skills.sh catalog for the 89,463 body-backed entries shipped in the wiki |
| `communities.json` | Current Louvain community export |
| `viz-overview.html` / `.png` | Plotly overview of the graph |
| `viz-python.html` | Python-focused graph view |
| `viz-security.html` / `.png` | Security-focused graph view |
| `viz-ai-agents.html` | AI-agent-focused graph view |
| `sample-top60.html` | Interactive top-degree sample |

## What Is Inside `wiki-graph.tar.gz`

- `entities/skills/` - all skill entity pages, including `skills-sh-*` pages
- `entities/agents/` - agent entity pages
- `entities/mcp-servers/<shard>/` - sharded MCP server entity pages
- `entities/harnesses/` - harness entity pages
- `converted/` - installable skill bodies for curated and Skills.sh skills
- `converted-agents/` - mirrored agent bodies
- `concepts/` - community concept pages
- `external-catalogs/skills-sh/` - Skills.sh catalog, summary, and coverage metadata
- `graphify-out/graph.json` - NetworkX node-link graph
- `graphify-out/communities.json` - community export
- `SCHEMA.md`, `index.md`, `log.md`, `catalog.md` - wiki contract and indexes
- `.obsidian/` - vault metadata for local graph browsing

`SKILL.md.original` backups and transient `.lock` files are not shipped. Local
micro-skill conversion may keep `.original` files for traceability, but the
packaged tarball excludes them so users do not ingest raw long bodies after
conversion.

## Extract

```bash
mkdir -p ~/.claude/skill-wiki
tar xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

On Windows with Git Bash or MSYS tar, use `--force-local`:

```bash
tar --force-local xzf graph/wiki-graph.tar.gz -C ~/.claude/skill-wiki/
```

## Validate

```bash
python src/validate_graph_artifacts.py --deep
python src/update_repo_stats.py --check
```

For release-count validation, pin the exact snapshot numbers:

```bash
python src/validate_graph_artifacts.py --deep \
  --expected-nodes 102696 \
  --expected-edges 2900834 \
  --expected-semantic-edges 1682825 \
  --expected-harness-nodes 13 \
  --expected-skills-sh-nodes 89463 \
  --expected-skills-sh-catalog-entries 89463 \
  --expected-skills-sh-converted 89463 \
  --expected-skill-pages 91432 \
  --expected-agent-pages 464 \
  --expected-mcp-pages 10787 \
  --expected-harness-pages 13
```

Manual sanity checks:

```bash
tar -tzf graph/wiki-graph.tar.gz | grep 'graphify-out/graph.json'
tar -tzf graph/wiki-graph.tar.gz | grep 'external-catalogs/skills-sh/catalog.json'
tar -tzf graph/wiki-graph.tar.gz | grep 'SKILL.md.original' && exit 1 || true
tar -tzf graph/wiki-graph.tar.gz | grep '\.lock$' && exit 1 || true
```

Windows PowerShell equivalent for the exclusion checks:

```powershell
tar -tzf graph/wiki-graph.tar.gz | Select-String 'SKILL.md.original'
tar -tzf graph/wiki-graph.tar.gz | Select-String '\.lock$'
```

The PowerShell commands should print nothing.

## Rebuild

After adding or updating skills, agents, MCP servers, or harnesses:

```bash
ctx-wiki-graphify
python src/validate_graph_artifacts.py --deep
python src/update_repo_stats.py --check
```

After a full wiki refresh, repack with the same exclusion contract:

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
    --exclude='*.lock' \
    .
```

The excluded paths are regenerable local caches or trace files. They are omitted
to keep the release artifact small and to prevent raw long skill bodies from
being loaded by users.

## Implementation Notes

The graph is built by `ctx.core.wiki.wiki_graphify` and the `ctx-wiki-graphify`
console script. Edges blend semantic similarity, explicit tag overlap,
slug-token overlap, source overlap, direct links, quality, usage, type affinity,
and graph-structure signals where available.

`nashsu/llm_wiki` was reviewed for design ideas around persistent wiki
contracts, queues, retrieval, and graph maintenance. ctx does not vendor that
code in this MIT repository.
