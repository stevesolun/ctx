---
name: skill-router
description: "Repo-aware skill and plugin manager. Scans the user's active repository, identifies the tech stack, frameworks, and workflows in use, then loads ONLY the relevant skills, plugins, and MCP servers -- unloading everything else to keep the context clean. Maintains a persistent LLM Wiki catalog of all available skills, plugins, and marketplaces so decisions are informed and consistent across sessions. Use this skill whenever the user opens a project, switches repos, asks 'what skills do I need', mentions context bloat or slow responses, asks to manage/list/add/remove skills or plugins, or references their skill catalog/wiki. Also triggers on: 'scan my repo', 'what tools do I need for this project', 'clean up my skills', 'too many plugins loaded', 'optimize my context', or any repo-switch event."
---

# Skill Router

Scan a repo. Know what it needs. Load only that. Maintain a wiki of everything available.

## Problem

Every skill, plugin, and MCP server loaded into context costs tokens and attention.
Most projects need 3-8 skills out of 30+. Loading all of them:
- Wastes context window on irrelevant instructions
- Causes skill misfires (wrong skill triggers for a task)
- Slows response time
- Creates conflicting instructions between skills

## Architecture

```
skill-router/
├── SKILL.md                    # This file -- orchestration logic
├── references/
│   ├── stack-signatures.md     # File/config patterns mapped to stack identifiers
│   ├── skill-stack-matrix.md   # Which skills serve which stacks
│   └── marketplace-registry.md # Known marketplaces and how to query them
└── scripts/
    ├── scan_repo.py            # Repo scanner -- outputs stack profile JSON
    ├── resolve_skills.py       # Maps stack profile to skill set
    └── wiki_sync.py            # Syncs scan results into the wiki
```

The skill router has two halves:
1. **The Scanner** -- analyzes a repo and produces a stack profile
2. **The Wiki** -- persistent catalog of all available skills/plugins/marketplaces,
   maintained via the Karpathy LLM Wiki pattern

## Session Startup (CRITICAL -- do this every time)

When this skill activates, follow this sequence:

### Step 1: Orient from the Wiki

```bash
WIKI="${SKILL_ROUTER_WIKI:-$HOME/skill-wiki}"
if [ -d "$WIKI" ]; then
  # Existing wiki -- orient first
  cat "$WIKI/SCHEMA.md"
  cat "$WIKI/index.md"
  tail -30 "$WIKI/log.md"
else
  # No wiki yet -- will initialize after first scan
  echo "No skill wiki found. Will initialize on first scan."
fi
```

### Step 2: Scan the Active Repo

```bash
python -m scan_repo --repo "$REPO_PATH" --output .ctx/stack-profile.json
```

### Step 3: Resolve and Load

```bash
python -m ctx.core.resolve.resolve_skills \
  --profile .ctx/stack-profile.json \
  --wiki "$WIKI" \
  --available-skills "$HOME/.claude/skills" \
  --output .ctx/skill-manifest.json
```

### Step 4: Apply the Manifest

Load skills in the manifest. Unload everything else. Report what changed.

---

## The Scanner

### What It Detects

The scanner reads repo structure and files to produce a **stack profile** -- a JSON
document describing everything the project uses. Detection is evidence-based: every
claim maps to a file or pattern that proves it.

#### Detection Categories

**1. Languages**
- Primary language(s) by file count and LOC
- Evidence: file extensions, shebangs, `*.lock` files

**2. Frameworks & Libraries**
- Web frameworks (React, Vue, Angular, Next.js, FastAPI, Django, Express, etc.)
- ML/AI frameworks (PyTorch, TensorFlow, LangChain, LlamaIndex, CrewAI, etc.)
- Mobile (React Native, Flutter, Swift, Kotlin)
- Evidence: `package.json` deps, `requirements.txt`, `pyproject.toml`, `Cargo.toml`,
  `go.mod`, import statements in entry files

**3. Infrastructure & DevOps**
- Containerization: Dockerfile, docker-compose.yml, .containerignore
- CI/CD: `.github/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/`
- IaC: Terraform (`*.tf`), Pulumi, CDK, CloudFormation, Ansible
- Cloud: AWS (SAM, CDK, `.aws/`), GCP, Azure config files
- K8s: `k8s/`, `helm/`, `kustomization.yaml`
- Evidence: config files, directory names

**4. Data & Storage**
- Databases: migrations dir, ORM configs (Prisma, SQLAlchemy, TypeORM, Drizzle)
- Message queues: Kafka, RabbitMQ, Redis configs
- Data pipelines: Airflow DAGs, dbt, Spark configs
- Evidence: connection strings (redacted), migration files, schema files

**5. Documentation & Content**
- Docs generators: MkDocs, Docusaurus, Sphinx, VitePress
- Content: markdown collections, MDX, RST
- API specs: OpenAPI/Swagger YAML/JSON, GraphQL schemas
- Evidence: config files, directory structure

**6. Testing & Quality**
- Test frameworks: pytest, Jest, Vitest, Cypress, Playwright
- Linting: ESLint, Prettier, Ruff, Black, Clippy
- Type checking: TypeScript config, mypy, pyright
- Evidence: config files, test directories

**7. AI/Agent Tooling**
- MCP servers: `mcp.json`, `.mcp/`, server configs
- Agent frameworks: LangGraph, CrewAI, AutoGen, Semantic Kernel
- Prompt management: prompt files, template dirs
- Model configs: `.env` with API keys (names only, never values), model references
- Evidence: config files, import patterns

**8. Build & Package**
- Build tools: Webpack, Vite, esbuild, Turbopack, Bazel
- Package managers: npm, yarn, pnpm, pip, poetry, cargo, go modules
- Monorepo tools: Nx, Turborepo, Lerna, workspace configs
- Evidence: config files, lock files

### Stack Profile Schema

```json
{
  "repo_path": "/absolute/path",
  "scanned_at": "ISO-8601",
  "languages": [
    {
      "name": "python",
      "confidence": 0.95,
      "evidence": ["pyproject.toml", "87 .py files", "poetry.lock"],
      "version_hint": ">=3.11 (pyproject.toml python_requires)"
    }
  ],
  "frameworks": [
    {
      "name": "fastapi",
      "category": "web",
      "confidence": 0.99,
      "evidence": ["pyproject.toml dependency", "main.py imports FastAPI"]
    }
  ],
  "infrastructure": [
    {
      "name": "docker",
      "confidence": 1.0,
      "evidence": ["Dockerfile", "docker-compose.yml"]
    }
  ],
  "data_stores": [],
  "testing": [],
  "ai_tooling": [],
  "build_system": [],
  "docs": [],
  "project_type": "api-service",
  "monorepo": false,
  "workspace_packages": [],
  "custom_signals": {}
}
```

### Scanning Rules

1. **Never read file contents unless necessary.** Start with directory listing and
   filenames. Only open files when you need to disambiguate (e.g., is this React or
   Preact? Check the import in the entry file).

2. **Confidence scoring:**
   - 1.0 = definitive (lock file, explicit config)
   - 0.8-0.99 = strong (dependency listed, config present)
   - 0.5-0.79 = probable (file patterns match, no explicit config)
   - <0.5 = speculative (mention in README, commented-out code) -- do not include

3. **Depth limits:**
   - Directory tree: 3 levels deep max for initial scan
   - `node_modules/`, `.git/`, `__pycache__/`, `venv/`, `.venv/`: skip entirely
   - For monorepos, scan each workspace package as a sub-profile

4. **Performance budget:** The scan should complete in under 10 seconds for repos up
   to 10K files. Use `find` with exclusions, not recursive `ls`.

5. **Version detection:** Extract version constraints from config files when available.
   This helps select skill variants (e.g., React 18 vs React 19 patterns differ).

---

## The Resolver

The resolver takes a stack profile and produces a **skill manifest** -- the exact set
of skills, plugins, and MCP servers to load.

### Resolution Algorithm

```
1. For each detected stack element (language, framework, infra, etc.):
   a. Look up in skill-stack-matrix.md which skills serve this element
   b. Check the wiki for any user-configured overrides or preferences
   c. Add to candidate set with priority score

2. Deduplicate:
   - If two skills cover the same capability, prefer the more specific one
   - Example: generic "python" skill vs "fastapi" skill -- keep fastapi, drop generic python

3. Check for required companions:
   - Some skills require others (e.g., "docker" skill needs "dockerfile-lint" if Dockerfile exists)
   - Read companion rules from skill-stack-matrix.md

4. Check for conflicts:
   - Some skills conflict (e.g., two different CSS-in-JS skills)
   - Resolve by: user preference (wiki) > specificity > recency

5. Apply user overrides:
   - Wiki pages in entities/ may have "always_load: true" or "never_load: true" flags
   - These override the algorithm

6. Produce the manifest
```

### Skill Manifest Schema

```json
{
  "generated_at": "ISO-8601",
  "repo_path": "/absolute/path",
  "profile_hash": "sha256 of stack profile",
  "load": [
    {
      "skill": "fastapi",
      "path": "/mnt/skills/public/fastapi/SKILL.md",
      "reason": "FastAPI detected in pyproject.toml dependencies",
      "priority": 1
    }
  ],
  "unload": [
    {
      "skill": "react",
      "reason": "No frontend framework detected in repo"
    }
  ],
  "mcp_servers": [
    {
      "name": "github",
      "url": "https://github.mcp.example.com",
      "reason": ".github/ directory with workflows detected"
    }
  ],
  "plugins": [],
  "warnings": [
    "Detected Terraform but no terraform skill is installed. Consider adding one."
  ],
  "suggestions": [
    {
      "skill": "openapi-generator",
      "reason": "OpenAPI spec found at api/openapi.yaml but no API generation skill loaded",
      "install_from": "marketplace:anthropic/openapi-gen"
    }
  ]
}
```

### Priority Scoring

Skills are ordered by priority so the most relevant instructions appear first in context:

| Signal | Priority Boost |
|---|---|
| Framework detected with confidence >= 0.9 | +10 |
| User marked "always_load" in wiki | +20 |
| Skill used in last 3 sessions (from wiki log) | +5 |
| Skill covers primary language | +8 |
| Skill covers secondary tooling (linting, testing) | +3 |
| Skill is generic/fallback | +1 |

---

## The Wiki (Persistent Catalog)

The skill router maintains a wiki following the Karpathy LLM Wiki pattern. This is
the router's long-term memory -- it tracks what's available, what's been used, and
what the user prefers.

### Wiki Location

Default: `~/skill-wiki` (configurable via `skills.config.wiki.path`)

### Wiki Structure

```
skill-wiki/
├── SCHEMA.md               # Conventions for this wiki domain
├── index.md                 # Catalog of all pages
├── log.md                   # Action log (scans, loads, installs)
├── raw/                     # Layer 1: Immutable source data
│   ├── scans/               # Historical stack profile JSONs
│   └── marketplace-dumps/   # Cached marketplace listings
├── entities/                # Layer 2: One page per skill/plugin/MCP server
│   ├── skills/
│   ├── plugins/
│   └── mcp-servers/
├── concepts/                # Layer 2: Stack patterns, best practices
├── comparisons/             # Layer 2: Skill-vs-skill analyses
└── queries/                 # Layer 2: Resolved decision records
```

### SCHEMA.md for the Skill Wiki

```markdown
# Skill Wiki Schema

## Domain
Catalog and management of all available skills, plugins, MCP servers, and
marketplace sources for the agent development environment. Tracks what exists,
what's been used, what works well, and user preferences.

## Conventions
- File names: lowercase, hyphens, no spaces
- Every page starts with YAML frontmatter
- Use [[wikilinks]] between pages (min 2 outbound per page)
- Bump `updated` on every change
- Every new page goes in index.md
- Every action appends to log.md

## Frontmatter for Entity Pages (Skills/Plugins/MCP)

  ```yaml
  ---
  title: Skill Name
  created: YYYY-MM-DD
  updated: YYYY-MM-DD
  type: skill | plugin | mcp-server | marketplace
  status: installed | available | deprecated | broken
  tags: [from taxonomy]
  source: local | marketplace-name | github-url
  path: /mnt/skills/public/skill-name/SKILL.md
  stacks: [python, fastapi, docker]
  always_load: false
  never_load: false
  last_used: YYYY-MM-DD
  use_count: 0
  avg_session_rating: null
  notes: ""
  ---
  ```

## Tag Taxonomy
- Stack: python, javascript, typescript, rust, go, java, ruby, swift, kotlin
- Framework: react, vue, angular, nextjs, fastapi, django, express, flask
- Infra: docker, kubernetes, terraform, ci-cd, aws, gcp, azure
- Data: sql, nosql, redis, kafka, spark, dbt, airflow
- AI: llm, agents, mcp, langchain, embeddings, fine-tuning, rag
- Quality: testing, linting, typing, security, performance
- Docs: documentation, api-spec, markdown, diagrams
- Meta: comparison, decision, pattern, troubleshooting
- Management: marketplace, registry, versioning, compatibility

## Page Thresholds
- Create a page when a skill/plugin/MCP server is discovered (installed or available)
- Update usage/configuration metadata when the local user changes preferences
- When a new version or replacement content is found, emit an update review
  first; do not replace the entity by default
- Archive when deprecated or superseded with a note pointing to the replacement

## Update Policy
- New version of a skill, agent, MCP server, or harness: compare the existing
  entity/local asset with the proposed replacement, list benefits and risks,
  and require the explicit update flag before replacing content
- Skill conflict discovered: create a comparison page, update both entity pages
- User preference expressed: update entity frontmatter (always_load/never_load)
```

### Entity Page Template (Skill)

```markdown
---
title: FastAPI Skill
created: 2026-04-08
updated: 2026-04-08
type: skill
status: installed
tags: [python, fastapi, web]
source: local
path: /mnt/skills/public/fastapi/SKILL.md
stacks: [python, fastapi]
always_load: false
never_load: false
last_used: 2026-04-07
use_count: 12
avg_session_rating: 4.5
notes: "Works well for API scaffolding. Occasionally suggests Pydantic v1 patterns."
---

# FastAPI Skill

## Overview
Generates FastAPI applications, routes, middleware, and deployment configs.

## Capabilities
- Scaffold new FastAPI projects
- Generate route handlers with Pydantic models
- Add middleware (CORS, auth, rate limiting)
- Generate OpenAPI spec customizations
- Docker + uvicorn deployment configs

## Stack Affinity
Primary: [[python]], [[fastapi]]
Secondary: [[docker]], [[openapi]]
Companions: [[pydantic-skill]] (recommended), [[sqlalchemy-skill]] (if DB detected)
Conflicts: [[flask-skill]] (overlapping web framework)

## Usage History
| Date | Repo | Outcome |
|------|------|---------|
| 2026-04-07 | /home/user/api-project | Generated 12 routes, good |
| 2026-03-29 | /home/user/microservice | Scaffold + Docker, good |

## Known Issues
- Suggests `from pydantic import BaseModel` without checking if v2 `model_validator` is needed
- Does not handle GraphQL integration (use [[graphql-skill]] instead)

## Sources
- [[raw/marketplace-dumps/anthropic-marketplace-2026-04.md]]
```

### Marketplace Integration

The wiki tracks marketplace sources so the router can suggest skills the user
doesn't have yet.

#### Marketplace Entity Page

```markdown
---
title: Anthropic Marketplace
created: 2026-04-08
updated: 2026-04-08
type: marketplace
status: active
tags: [marketplace, registry]
url: https://marketplace.anthropic.com/skills
refresh_interval_days: 7
last_refreshed: 2026-04-08
---

# Anthropic Marketplace

## Overview
Official skill marketplace maintained by Anthropic.

## How to Query
- API: GET /api/v1/skills?stack=python&category=web
- CLI: `hermes marketplace search --query "fastapi"`

## Cached Listings
See [[raw/marketplace-dumps/anthropic-marketplace-2026-04.md]]

## Install Command
`hermes skill install marketplace:anthropic/<skill-name>`
```

#### Marketplace Refresh

When the router detects a stack element with no matching installed skill:
1. Check marketplace entity pages for `last_refreshed`
2. If stale (> `refresh_interval_days`), re-query the marketplace
3. Save new listing dump to `raw/marketplace-dumps/`
4. Create entity pages for newly discovered skills; existing pages require an
   update review and explicit update flag before replacement
5. Include in the manifest's `suggestions` array

---

## Core Operations

### 1. Full Scan (repo switch or first run)

Triggers: user opens a new project, says "scan my repo", switches working directory

```
① Read wiki orientation (SCHEMA, index, recent log)
② Run scan_repo.py on the target repo
③ Save scan result to raw/scans/scan-YYYY-MM-DD-reponame.json
④ Run resolve_skills.py with the profile + wiki
⑤ Present the manifest to the user:
   - "Loading: [list with reasons]"
   - "Unloading: [list]"
   - "Suggestions: [skills you don't have but might want]"
   - "Warnings: [gaps detected]"
⑥ On user confirmation, apply the manifest
⑦ Update wiki:
   - Bump last_used and use_count on loaded skill entity pages
   - Create entity pages for any newly discovered skills
   - Append to log.md
⑧ Update index.md if new pages were created
```

### 2. Incremental Scan (file changes during session)

Triggers: user creates a new config file, adds a dependency, installs a package

The router watches for signals that the stack changed mid-session:
- New `Dockerfile` created -> check if docker skill is loaded
- `package.json` modified -> re-scan dependencies
- New `.github/workflows/` file -> check CI/CD skills
- New `*.tf` files -> check terraform skill

For incremental scans:
```
① Re-scan only the changed area (single file or directory)
② Diff against the current manifest
③ If new skills needed: "I noticed you added [X]. Want me to load the [Y] skill?"
④ If skills can be unloaded: "You removed [X]. I can unload the [Y] skill to free context."
⑤ Apply changes on confirmation
⑥ Log the incremental update
```

### 3. Manual Override

Users can force-load or force-unload skills:

- "Always load the docker skill" -> set `always_load: true` in wiki entity page
- "Never load the react skill" -> set `never_load: true` in wiki entity page
- "Load the terraform skill for this session" -> temporary load, no wiki change
- "What skills am I running?" -> show current manifest with reasons

### 4. Skill Discovery

When the user asks "what skills exist for X" or "is there a skill for Y":

```
① Search wiki entity pages for matching tags/stacks
② If found: show the entity page summary, status, and rating
③ If not found: query marketplace entity pages
④ If marketplace has it: suggest installation with command
⑤ If nowhere: note the gap, suggest creating a custom skill
⑥ Log the query
```

### 5. Wiki Maintenance (Lint)

Runs the standard LLM Wiki lint plus skill-specific checks:

- **Stale skills**: entity pages with `last_used` > 90 days
- **Ghost skills**: entity pages with `status: installed` but path doesn't exist
- **Orphan skills**: installed skills with no entity page in the wiki
- **Marketplace staleness**: marketplaces not refreshed within their interval
- **Conflict detection**: skills with overlapping `stacks` that are both `always_load`
- **Usage cold spots**: skills with `use_count: 0` after 30+ days -- suggest removal
- Standard wiki lint: orphan pages, broken links, index completeness, frontmatter validation

---

## Integration with Karpathy LLM Wiki

This skill extends the LLM Wiki pattern. If the user also has a general-purpose
knowledge wiki (separate from the skill wiki), the two coexist:

- **Skill wiki** (`~/skill-wiki`): managed by skill-router, tracks tooling
- **Knowledge wiki** (`~/wiki`): managed by llm-wiki skill, tracks domain knowledge

Cross-references between wikis use full paths: `[[~/wiki/concepts/rag.md|RAG]]`
rather than bare wikilinks (which resolve within the same wiki).

The skill-router's wiki follows all LLM Wiki conventions:
- Three-layer architecture (raw / entities-concepts / schema)
- Frontmatter on every page
- Tag taxonomy in SCHEMA.md
- Append-only log with rotation
- Lint for consistency
- Obsidian-compatible wikilinks

The key extension is the **entity frontmatter** -- skill/plugin/MCP pages carry
operational metadata (status, path, stacks, always_load, use_count) that the
resolver reads programmatically. This is what makes the wiki active rather than
passive -- it doesn't just store knowledge, it drives loading decisions.

---

## Reporting

After every scan, the router produces a concise report:

```
## Skill Router Report -- [repo-name]
Scanned: YYYY-MM-DD HH:MM

### Stack Profile
- Languages: Python 3.11, TypeScript 5.4
- Frameworks: FastAPI, React 18
- Infra: Docker, GitHub Actions
- Data: PostgreSQL (SQLAlchemy), Redis
- AI: LangChain, MCP (2 servers configured)

### Loaded (6 skills)
1. fastapi (confidence: 0.99) -- pyproject.toml
2. react (confidence: 0.95) -- package.json
3. docker (confidence: 1.0) -- Dockerfile
4. sqlalchemy (confidence: 0.9) -- alembic/
5. langchain (confidence: 0.85) -- imports in agent.py
6. github-actions (confidence: 1.0) -- .github/workflows/

### Unloaded (24 skills)
[collapsed list]

### Suggestions
- openapi-generator: OpenAPI spec found but no generation skill
- redis-skill: Redis connection in docker-compose but no Redis skill

### Warnings
- No testing skill loaded but pytest.ini exists -- add pytest skill?
```

---

## Handling Edge Cases

**Monorepos**: Scan each workspace package separately. Produce a merged manifest
that includes skills for all packages, with per-package annotations.

**Empty repos**: Report "No stack detected. This looks like a new project."
Ask what the user plans to build, then suggest a starter skill set.

**Conflicting signals**: If the repo has both `requirements.txt` AND `package.json`,
it's a polyglot project. Load skills for both stacks. Note: confidence drops if
files look abandoned (empty, very old timestamps).

**Skill not found**: If the resolver identifies a need but no skill exists for it,
log a gap in the wiki and include in `warnings`. Suggest marketplace search or
custom skill creation.

**User disagrees with scan**: "No, I don't use React anymore, that's legacy code."
Mark react skill as `never_load` in wiki, note the reason. The scan still sees the
files but the override takes precedence.

---

## Configuration

In `~/.hermes/config.yaml` (or equivalent agent config):

```yaml
skills:
  config:
    skill-router:
      wiki_path: ~/skill-wiki
      auto_scan: true          # Scan on repo switch
      auto_load: false         # Require confirmation before loading
      scan_depth: 3            # Directory depth for initial scan
      marketplace_refresh: 7   # Days between marketplace cache refresh
      max_loaded_skills: 15    # Hard cap on simultaneous skills
      incremental_watch: true  # Monitor file changes mid-session
      report_verbosity: normal # minimal | normal | verbose
```

---

## Pitfalls

- **Never skip wiki orientation.** Reading SCHEMA + index + log before acting prevents
  duplicates and missed context. This is the #1 cause of wiki degradation.
- **Never load all skills "just in case."** The whole point is selective loading.
  If the user needs something unexpected, incremental scan catches it.
- **Never modify raw/ files.** Scan results and marketplace dumps are immutable records.
- **Always confirm before loading/unloading.** Unless `auto_load: true` is configured.
- **Don't over-scan.** Reading every file in a 50K-file monorepo is wasteful. Use
  directory structure and config files first, open source files only to disambiguate.
- **Keep entity pages current.** A stale wiki is worse than no wiki -- it makes wrong
  loading decisions. Run lint monthly.
- **Respect `never_load`.** User overrides are sacrosanct. Don't re-suggest skills
  the user has explicitly rejected (unless they ask).
- **Log everything.** The log is how the router learns patterns across sessions.
  "Last 3 times this repo was opened, the user also loaded X" is valuable signal.
