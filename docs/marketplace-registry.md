# Entity Source Registry

ctx keeps entity sources separate from install state. A source entry can be
searched and recommended without being installed, and duplicate/update paths must
show what would change before replacing an existing entity.

## Registered Sources

### Shipped Graph And LLM-Wiki

```yaml
name: ctx-shipped-graph
type: compressed-runtime
paths:
  graph: graph/wiki-graph-runtime.tar.gz
  skills: graph/skills-sh-catalog.json.gz
refresh: release-time
priority: 1
```

The shipped runtime is the first source for recommendations. It contains the
first-class skills, agents, MCP servers, harnesses, graph edges, quality scores,
usage fields, and wiki pages that ctx can use offline.

### User Local Assets

```yaml
name: user-local
type: filesystem
paths:
  skills: ~/.claude/skills
  agents: ~/.claude/agents
  mcp: project/user MCP config files
  harnesses: ~/.ctx/harnesses
refresh: always current
priority: 2
```

Local assets override shipped suggestions when names collide, but updates
still require an explicit review if replacement content is proposed.

### Skill Index

```yaml
name: shipped-skills
type: shipped-index
shipped_entries: 67024
local_index: graph/skills-sh-catalog.json.gz
hydrated_wiki: external-catalogs/<source>/catalog.json
refresh: on-demand
priority: 3
```

Skill index entries are stored as first-class skill entities in the graph/wiki.
Hydrated `SKILL.md` bodies pass through the micro-skill gate before they are
packed into the shipped runtime.

### GitHub Entity Repositories

```yaml
name: github-entity-repos
type: git
entrypoints:
  skills: python -m skill_add
  agents: python -m agent_add
  mcp: python -m mcp_add
  harnesses: python -m harness_add
refresh: on-demand
priority: 4
```

GitHub stars, forks, releases, and update timestamps can be stored as source
metadata for a candidate entity. The ctx repository's own star count is not
hard-coded in docs because it changes continuously; read it from GitHub when
needed.

### MCP And Harness Sources

```yaml
name: mcp-and-harness-sources
type: curated-source
entrypoints:
  mcp: python -m mcp_fetch, python -m mcp_add
  harnesses: python -m harness_add, python -m harness_install
refresh: on-demand
priority: 5
```

MCP servers and harnesses are recorded as entities with install guidance,
permission notes, compatibility tags, and quality/security review status.

## Query Protocol

When a user asks for help or the scanner detects a stack/task gap:

1. Search the local graph/wiki first.
2. Search the shipped skill index and, when needed, the `find-skills`
   helper for fresher remote results.
3. Search local user assets and configured entity repositories.
4. Deduplicate by slug, source URL, canonical name, tags, and semantic overlap.
5. Score candidates with the shared recommendation engine.
6. Present at most the configured cap with reasons, quality/usage notes, and
   install/update commands.
7. If an existing entity would be replaced, show the update review: benefits,
   drawbacks, changed files, security posture, and rollback path.

## Update Rules

Entity updates are intentionally explicit:

- `python -m skill_add`, `python -m agent_add`, `python -m mcp_add`, and `python -m harness_add` create
  new entities when no duplicate exists.
- If a duplicate exists, the command emits an update review and refuses to
  replace content unless the user passes the update flag.
- New or updated skills go through the micro-skill line-count gate from config.
- Security/cyber checks run before entity content is promoted.
- Graph/wiki artifacts are rebuilt, validated, packed, and atomically promoted.

## Security Notes

- Never auto-install without user confirmation.
- Always show the source URL, entity type, install command, and permissions.
- Reject missing `SKILL.md` bodies for skill imports unless the source is only a
  catalog pointer.
- Never execute repository scripts during cataloging without explicit user
  approval.
- Warn on network, filesystem, shell, credential, or system-level permissions.
- Preserve last-good graph/wiki artifacts so a failed refresh cannot ship a
  corrupt runtime.
