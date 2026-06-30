---
name: skill-router
description: "Repo-aware recommendation manager for ctx. Scans the active repository, identifies stack and workflow signals, recommends a capped set of skills, agents, and MCP servers, and unloads helpers that no longer match the current work after user confirmation. Harnesses are recommended by the custom-model onboarding flow or loop adapters and then attach to the same recommendation layer."
---

# Skill Router

Scan a repo. Know what the current work needs. Recommend only that. Keep the
wiki and graph as the durable catalog behind the decision.

## Scope

The router manages runtime recommendations for:

- skills
- agents
- MCP servers

Harnesses are separate catalog entities. They are recommended when a user wants
to run ctx with a non-Claude-Code host, local model, API model, or external loop
adapter. Once attached, the harness calls the same skills/agents/MCP
recommendation engine.

## Problem

Every loaded helper costs tokens, attention, and operational surface area. Most
sessions need a small top-scored bundle from the shipped graph, not every entity
ctx knows about. Loading too much:

- wastes context on irrelevant instructions,
- causes misfires when a helper matches the wrong task,
- slows the agent loop, and
- creates conflicting instructions.

## Architecture

```text
ctx/
|-- src/scan_repo.py                         # Repo scanner -> stack profile
|-- src/ctx/core/resolve/resolve_skills.py   # Profile -> load/unload manifest
|-- src/ctx/core/resolve/recommendations.py  # Shared scoring/ranking engine
|-- src/ctx/adapters/                        # Host adapters + generic tools
|-- src/harness_install.py                   # Custom-model harness install flow
`-- graph/wiki-graph-runtime.tar.gz          # Shipped graph/wiki runtime
```

The router has three halves:

1. **Scanner** - analyzes a repo and produces stack/task evidence.
2. **Resolver** - scores graph/wiki entities and emits a capped manifest.
3. **Wiki/graph** - persistent catalog of entities, usage, quality, and links.

## Startup Flow

1. Read the shipped graph/wiki metadata and local user overrides.
2. Run `ctx-scan-repo --repo . --recommend` for the active repo.
3. Resolve a load/unload manifest with the shared recommendation engine.
4. Present changes with reasons and require confirmation unless the user enabled
   automatic mode.
5. Record usage/quality changes after the user accepts or rejects suggestions.

## Scanner

The scanner reads repo structure and files to produce a stack profile. Detection
is evidence-based: every claim should map to a file, dependency, config value, or
import pattern.

### Detection Categories

- **Languages** - file extensions, shebangs, lock files.
- **Frameworks and libraries** - package manifests, imports, config files.
- **Infrastructure and DevOps** - Docker, CI/CD, IaC, cloud, Kubernetes.
- **Data and storage** - databases, migrations, queues, pipelines.
- **Documentation and content** - MkDocs, Docusaurus, Sphinx, API specs.
- **Testing and quality** - pytest, Jest, Playwright, Ruff, mypy, TypeScript.
- **AI and agent tooling** - MCP configs, LangGraph, CrewAI, prompt dirs, model
  config names.
- **Build and package** - package managers, build tools, monorepos.

### Scanning Rules

1. Start with filenames and config files. Read source only when needed to
   disambiguate.
2. Do not include speculative signals below the configured confidence floor.
3. Skip generated/vendor directories such as `.git`, `node_modules`,
   `__pycache__`, `venv`, and `.venv`.
4. Keep initial scans bounded for large repos.
5. Redact secret values; only record secret/key names as evidence.

## Resolver

The resolver produces a manifest containing the exact helper set to load or
unload.

```json
{
  "generated_at": "ISO-8601",
  "repo_path": "/absolute/path",
  "profile_hash": "sha256 of stack profile",
  "load": [
    {
      "name": "fastapi",
      "type": "skill",
      "reason": "FastAPI detected in pyproject.toml dependencies",
      "score": 0.94
    }
  ],
  "unload": [
    {
      "name": "react",
      "type": "skill",
      "reason": "No active frontend signal in the current repo window"
    }
  ],
  "suggestions": [
    {
      "name": "github",
      "type": "mcp",
      "reason": ".github/workflows exists and the repo uses GitHub Actions",
      "install_command": "ctx-mcp-add ..."
    }
  ],
  "warnings": []
}
```

### Ranking Signals

Candidates can be scored by:

- tag/category/subcategory match
- semantic similarity edge weight
- direct wiki links
- source overlap
- type affinity
- usage score
- quality/security score
- user overrides (`always_load`, `never_load`)
- configured caps and minimum score gates

Recommendations are capped. ctx should not recommend at all costs; below-threshold
candidates should be withheld and the gap should be explained.

## Wiki And Graph Contract

The wiki follows a Karpathy-style durable memory pattern: schema, purpose, index,
log, lint, review items, and entity pages that are machine-readable.

```text
llm-wiki/
|-- SCHEMA.md
|-- index.md
|-- log.md
|-- raw/
|   |-- scans/
|   `-- external-catalogs/
|-- entities/
|   |-- skills/
|   |-- agents/
|   |-- mcp-servers/
|   `-- harnesses/
|-- concepts/
|-- comparisons/
`-- queries/
```

Entity pages use YAML frontmatter with at least:

```yaml
title: Entity Name
type: skill | agent | mcp-server | harness | external-catalog
status: installed | available | deprecated | broken
tags: []
source: local | shipped | github | curated
source_url: ""
path: ""
quality_score: null
usage_score: 0
last_used: null
always_load: false
never_load: false
```

## Entity Sources

Entity sources include shipped skill/MCP/harness sources, GitHub
entity repositories, and local user assets. The public reference page is
[`marketplace-registry.md`](marketplace-registry.md), kept under that filename
for backwards-compatible links.

Source rules:

1. Search the shipped graph/wiki first.
2. Use additional sources only when the local graph is missing or stale.
3. Deduplicate before adding.
4. If an entity exists, emit an update review instead of replacing it.
5. Run security checks before promotion.
6. Rebuild and validate graph/wiki artifacts before shipping.

## Micro-Skill Gate

Every added or updated skill must pass the configured line-count gate before it
is packed into the shipped wiki. The default threshold is 180 lines, read from
ctx config. Skills above the threshold are converted into a short orchestrator
plus staged reference files. Local `.original` backups may be preserved for
traceability, but packaged runtime archives must omit those backups.

## Core Operations

### Full Scan

Triggers: repo open, repo switch, `ctx-scan-repo --repo . --recommend`, or explicit user
request.

1. Read wiki orientation and local overrides.
2. Scan the target repo.
3. Save the scan result.
4. Resolve load/unload recommendations.
5. Present reasons, scores, and install/update commands.
6. Apply only after confirmation unless configured otherwise.
7. Record usage and decisions.

### Incremental Scan

Triggers: changed config files, new dependencies, new MCP config, new tests, new
infrastructure files, or user task change.

If a helper is no longer useful, ctx should suggest unloading it and ask for
confirmation. If the user asks to skip unload prompts, ctx should respect that
preference.

### Manual Override

- "Always load the docker skill" -> set `always_load: true`.
- "Never load the react skill" -> set `never_load: true`.
- "Load the terraform skill for this session" -> temporary load.
- "What is loaded?" -> show current manifest with reasons.

### Discovery

When the user asks what exists for a task:

1. Search graph/wiki entity pages by tags and text.
2. Use `find-skills` and configured sources for remote freshness when needed.
3. Show status, score, source, and risk notes.
4. Suggest install/update commands, not silent installs.
5. Log the query.

## Maintenance Checks

- **Stale installed helpers** - used rarely or not used recently.
- **Ghost helpers** - installed status but missing local path.
- **Orphan local helpers** - present locally but missing entity page.
- **Skill index freshness** - shipped skill snapshot older than policy.
- **Conflicts** - overlapping helpers both marked `always_load`.
- **Usage cold spots** - low usage/quality score candidates for unload review.
- **Wiki lint** - broken links, missing frontmatter, index drift.

## Reporting

After a scan, report only actionable details:

```text
Skill Router Report - repo-name
Scanned: YYYY-MM-DD HH:MM

Loaded:
1. fastapi skill - score 0.94 - pyproject.toml dependency
2. github MCP - score 0.91 - GitHub Actions workflows detected

Unload candidates:
1. react skill - no active frontend signal

Suggestions:
1. openapi-generator skill - OpenAPI spec found

Warnings:
None
```

## Configuration

Router behavior is controlled by ctx config and user overrides. Important knobs:

```yaml
recommendations:
  max_total: 5
  min_score: 0.85
micro_skills:
  max_lines: 180
router:
  auto_scan: true
  auto_apply: false
  ask_before_unload: true
```

## Pitfalls

- Never load all helpers just in case.
- Never replace an existing entity without an update review.
- Never execute external repo scripts while cataloging.
- Never treat conversation history as the source of workflow state.
- Never ship graph/wiki artifacts before validation passes.
- Respect `never_load` and user rejection history.
