# Your knowledge graph

CTX supplies graph/wiki infrastructure, not a catalog of tools. Fresh setup
contains no skills, agents, MCP servers, descriptions or executable tool bodies.
Users and organizations author or import their own knowledge.

`empty-graph.json` is a valid zero-node NetworkX node-link graph. It documents
the structural empty state; it is not a seed catalog. The richer graph, wiki
and pack validation contracts remain in `src/ctx/core/graph/` and
`src/ctx/core/wiki/`. Setup uses `ensure_wiki()` to create the wiki schema,
index, log and entity directories without tool content.

The user workspace can contain:

```text
skill-wiki/
  SCHEMA.md
  index.md
  log.md
  entities/
    skills/
    agents/
    mcp-servers/
    harnesses/
  concepts/
  graphify-out/
    graph.json
```

Only user-provided records appear in recommendations. Empty knowledge or no
relevant match produces no suggestions. Descriptions, tags, explicit links,
source/install metadata and optional local embeddings support relevance;
metadata and similarity never grant installation or execution authority.

Use the [knowledge guide](../docs/knowledge-graph.md) and
[entity onboarding](../docs/entity-onboarding.md) for authoring and import.
Graph construction, overlays, immutable packs, validation and compaction remain
available for user-owned datasets. Generated corpus artifacts belong in the
user's storage, not this source repository or the Python distribution.

## Historical artifacts

Older releases included a CTX-maintained corpus and a five-asset manifest.
They are historical, not a fallback source for new installs or CI. The current
product does not hydrate or republish them. Removing them from the current
tree does not rewrite Git history or purge GitHub's historical LFS storage.
