# ctx — Alive Skill System Agent

An autonomous agent that uses a Karpathy LLM wiki as ground truth to manage 1,489 skills for Claude Code. It scans your project, loads only what is relevant, converts long skills into structured micro-skill pipelines, and learns from session usage — without manual intervention.

---

## What Is This

ctx is not a collection of scripts. It is an agent with persistent memory.

The core idea comes from Andrej Karpathy's LLM wiki pattern: instead of re-loading everything from scratch each session, an LLM maintains a wiki it can read, write, and query. The wiki becomes the agent's long-term memory.

ctx applies that pattern to skill management:

- A Karpathy 3-layer wiki at `~/.claude/skill-wiki/` is the single source of truth
- Every skill has an entity page tracking use count, last used date, tags, and status
- PostToolUse and Stop hooks update the wiki automatically during each Claude Code session
- Skills over 180 lines are converted to a gated 5-stage micro-skill pipeline so the router can load them incrementally
- The skill-router agent runs at session start, reads the wiki, and loads only the skills that match your current project's stack

The result: your skill library gets smarter every session. High-signal skills surface. Stale ones are flagged. New ones self-ingest.

---

## How It Works

```
Session start
  skill-router reads stack-profile.json + wiki entity pages
    -> scan_repo.py detects stacks (Python, Docker, FastAPI, etc.) with confidence scores
    -> resolve_skills.py scores each skill against the detected stack
    -> loads <=15 highest-priority skills into context
    -> unloads stale ones (unseen >30 days)

Mid-session (every tool call)
  context-monitor.py (PostToolUse hook)
    -> extracts intent signals from file extensions and framework keywords
    -> writes signals to ~/.claude/intent-log.jsonl
    -> if >=3 new unmatched signals -> queues skill candidates in pending-skills.json

Session end
  usage-tracker.py (Stop hook)
    -> reads intent-log.jsonl
    -> updates use_count + last_used in wiki entity pages
    -> marks skills unseen >30 days as status: stale
    -> appends session summary to skill-wiki/log.md

Maintenance (on-demand)
  wiki_orchestrator.py --check   -> health score 0-100 across all subsystems
  wiki_orchestrator.py --sync    -> full maintenance pass (lint + link + catalog rebuild)
  wiki_lint.py                   -> find orphans, broken links, stale entity pages
  wiki_query.py                  -> query wiki with citations, filter by tag, show stats
```

---

## Quick Start

```bash
# 1. Clone and enter
git clone https://github.com/stevesolun/ctx && cd ctx

# 2. Deploy everything
bash install.sh

# 3. Check health
python wiki_orchestrator.py --check
```

---

## Installation

### Option A — Automated (install.sh)

```bash
bash install.sh
```

The install script runs these phases in order:

| Phase | What happens |
|-------|-------------|
| 1 | Init skill wiki at `~/.claude/skill-wiki/` |
| 2 | Catalog all 1,489 skills -> `catalog.md` |
| 3 | Deploy `skill-router` to `~/.claude/agents/skill-router/` |
| 4 | Inject PostToolUse + Stop hooks into `~/.claude/settings.json` |
| 5 | Create `skill-registry.json` at `~/.claude/skill-registry.json` |
| 6 | Dry-run: count skills >180 lines eligible for conversion |
| 7 | Convert eligible skills to micro-skill pipeline format |
| 8 | Build dual-version sub-catalog (`versions-catalog.md`) |

### Option B — Manual (step by step)

```bash
cd ctx

# Init wiki (creates 3-layer directory structure + SCHEMA.md + index.md)
python wiki_sync.py --init --wiki ~/.claude/skill-wiki

# Build catalog
python catalog_builder.py --wiki ~/.claude/skill-wiki

# Deploy skill-router agent
mkdir -p ~/.claude/agents/skill-router
cp -r skills/skill-router/. ~/.claude/agents/skill-router/

# Inject hooks into Claude Code settings
python inject_hooks.py --settings ~/.claude/settings.json --ctx-dir $(pwd)

# Convert long skills to micro-skill pipeline format (dry-run first)
PYTHONIOENCODING=utf-8 python batch_convert.py --scan ~/.claude/skills --dry-run
PYTHONIOENCODING=utf-8 python batch_convert.py --scan ~/.claude/skills --auto

# Link wiki entity pages to converted pipelines
python link_conversions.py --wiki ~/.claude/skill-wiki

# Build versions catalog (tracks both original + pipeline versions)
python versions_catalog.py --wiki ~/.claude/skill-wiki
```

**Prerequisites:**

- Python 3.10+
- Claude Code CLI installed (provides `~/.claude/`)
- `~/.claude/skills/` populated with your global skill library

---

## Configuration

All paths, thresholds, and numeric limits live in `config.json`. Nothing is hardcoded in the scripts.

```json
{
  "claude_dir":               "~/.claude",
  "wiki_path":                "~/.claude/skill-wiki",
  "skills_dir":               "~/.claude/skills",
  "agents_dir":               "~/.claude/agents",
  "intent_log":               "~/.claude/intent-log.jsonl",
  "pending_skills":           "~/.claude/pending-skills.json",
  "manifest":                 "~/.claude/skill-manifest.json",
  "skill_registry":           "~/.claude/skill-registry.json",
  "catalog_path":             "~/.claude/skill-wiki/catalog.md",
  "versions_catalog_path":    "~/.claude/skill-wiki/versions-catalog.md",
  "max_skills_loaded":        15,
  "stale_threshold_days":     30,
  "intent_signal_threshold":  3,
  "convert_line_threshold":   180,
  "health_score_warn":        70,
  "health_score_critical":    50
}
```

| Key | What it controls |
|-----|-----------------|
| `claude_dir` | Root of Claude Code installation |
| `wiki_path` | Karpathy wiki vault location |
| `skills_dir` | Global skill library scanned at session start |
| `max_skills_loaded` | Max skills loaded into context per session |
| `stale_threshold_days` | Days of inactivity before a skill is flagged stale |
| `intent_signal_threshold` | Unmatched signals needed to queue a new skill candidate |
| `convert_line_threshold` | Skills over this line count get converted to pipeline format |
| `health_score_warn` | Orchestrator warns below this health score |
| `health_score_critical` | Orchestrator alerts critical below this score |

`ctx_config.py` is the config singleton — all scripts import from it. Environment variable overrides are supported for CI use.

---

## Usage Examples

### Health check

```bash
python wiki_orchestrator.py --check
```

Runs all subsystem checks and outputs a health score from 0-100. Checks include: wiki integrity, entity page completeness, catalog freshness, hook injection status, stale skill count, and broken internal links.

### Full maintenance sync

```bash
python wiki_orchestrator.py --sync
```

Runs lint, relinks converted pipelines to entity pages, rebuilds catalog and versions catalog, and updates the health score. Safe to run any time.

### Query the wiki

```bash
# Natural language query with citations
python wiki_query.py --query "docker skills"

# Filter by tag
python wiki_query.py --tag python

# Show usage statistics
python wiki_query.py --tag python --stats
```

### Lint the wiki

```bash
python wiki_lint.py --wiki ~/.claude/skill-wiki
```

Finds orphaned entity pages (no matching skill file), broken internal links, and entity pages that have not been updated in over 30 days.

### Add a new skill

```bash
python skill_add.py --skill-path /path/to/MY-SKILL.md --name my-skill
```

This copies the skill to `~/.claude/skills/my-skill/SKILL.md`, checks line count, auto-converts if over the threshold, creates a wiki entity page, links the entity to the converted pipeline if applicable, and updates the catalog.

### Check what is eligible for conversion (dry-run)

```bash
PYTHONIOENCODING=utf-8 python batch_convert.py --scan ~/.claude/skills --dry-run
```

### Run conversion on all eligible skills

```bash
PYTHONIOENCODING=utf-8 python batch_convert.py --scan ~/.claude/skills --auto
```

---

## Adding New Skills

`skill_add.py` is the single entry point for adding skills to the system.

```bash
python skill_add.py --skill-path /path/to/SKILL.md --name my-skill [--tags python,api]
```

What it does internally:

1. Copies the skill file to `~/.claude/skills/my-skill/SKILL.md`
2. Counts lines — if over `convert_line_threshold` (default 180), invokes the micro-skills pipeline conversion
3. Creates a wiki entity page at `~/.claude/skill-wiki/entities/skills/my-skill.md` with frontmatter: `use_count: 0`, `last_used: null`, `status: active`, and the supplied tags
4. If converted, calls `link_conversions.py` to write a `pipeline_path` reference into the entity page
5. Regenerates `catalog.md` and `versions-catalog.md`

To remove a skill: delete the directory from `~/.claude/skills/` and run `python wiki_lint.py --wiki ~/.claude/skill-wiki` to clean up the orphaned entity page.

---

## Micro-Skills Pipeline

Skills over 180 lines are too large to load in full context. ctx converts them to the stevesolun/micro-skills 5-stage gated pipeline format.

Each stage is a separate file with a YES/NO quality gate before the next stage executes. The orchestrator SKILL.md is ~30 lines and is what the skill-router loads. Full stage content is read on demand.

### Converted skill directory structure

```
~/.claude/skills/<skill-name>/
  SKILL.md              # orchestrator (~30 lines, loaded by router)
  SKILL.md.original     # original file, preserved for audit and revert
  check-gates.md        # gate definitions for each stage transition
  failure-log.md        # append-only log of gate failures
  original-hash.txt     # SHA of original for change detection
  references/
    01-scope.md         # Stage 1: define scope and constraints
    02-plan.md          # Stage 2: produce implementation plan
    03-build.md         # Stage 3: execute the build
    04-check.md         # Stage 4: verify quality gates pass
    05-deliver.md       # Stage 5: package and deliver output
```

To revert a single skill to its original form:

```bash
cp ~/.claude/skills/<name>/SKILL.md.original ~/.claude/skills/<name>/SKILL.md
```

Full list of dual-version skills: `~/.claude/skill-wiki/versions-catalog.md`

---

## Wiki Structure

The wiki uses Karpathy's 3-layer architecture: raw data, structured wiki pages, and a schema layer. It is an Obsidian-compatible vault with `.obsidian/` config included.

```
~/.claude/skill-wiki/
  SCHEMA.md                    # tag taxonomy, conventions, update policy
  index.md                     # all entity pages with one-line summaries
  log.md                       # append-only action log (written by Stop hook)
  catalog.md                   # bulk listing of all 1,489 managed skills + agents
  versions-catalog.md          # 760 dual-version skills (original + pipeline)
  .obsidian/                   # Obsidian vault config (graph, plugins, settings)
  raw/
    scans/                     # per-repo scan results (JSON)
    marketplace-dumps/         # upstream skill registry snapshots
  entities/
    skills/                    # one .md per discovered skill
      <skill-name>.md          # frontmatter: use_count, last_used, status, tags
                               # body: description, pipeline_path (if converted)
  converted/
    <skill-name>/              # 760 converted pipelines
      SKILL.md                 # orchestrator
      references/              # 5 stage files
      check-gates.md
      failure-log.md
```

Entity page frontmatter example:

```yaml
---
name: python-patterns
status: active
use_count: 14
last_used: 2026-03-28
tags: [python, patterns, async]
pipeline_path: converted/python-patterns/SKILL.md
---
```

Open `~/.claude/skill-wiki/` as a vault in Obsidian to get graph view, tag explorer, and backlink navigation across all 1,489 skills.

---

## File Reference

| File | Role |
|------|------|
| `scan_repo.py` | Scans a repo, detects tech stacks with confidence scores -> `stack-profile.json` |
| `resolve_skills.py` | Scores all skills against a stack profile, outputs ranked `skill-manifest.json` |
| `wiki_sync.py` | Creates/updates wiki entity pages, index.md, and log.md |
| `context-monitor.py` | PostToolUse hook: extracts intent signals, writes `intent-log.jsonl` |
| `usage-tracker.py` | Stop hook: updates wiki use counts, marks stale skills, appends to log.md |
| `batch_convert.py` | Scans skills dir, converts all files over threshold to micro-skill pipeline format |
| `skill_add.py` | Adds a new skill: copy + auto-convert + wiki entity page + catalog update |
| `wiki_lint.py` | Finds orphaned pages, broken links, and stale entity pages in the wiki |
| `wiki_query.py` | Queries the wiki with citations; supports tag filtering and usage stats |
| `wiki_orchestrator.py` | Master orchestrator: health score 0-100, runs all maintenance operations |
| `link_conversions.py` | Links wiki entity pages to their converted pipeline directories |
| `catalog_builder.py` | Builds `catalog.md` listing all managed skills and agents |
| `versions_catalog.py` | Builds `versions-catalog.md` listing all dual-version skills |
| `inject_hooks.py` | Merges PostToolUse + Stop hooks into `~/.claude/settings.json` |
| `ctx_config.py` | Config singleton: reads `config.json`, exposes all paths and thresholds |
| `config.json` | All configurable values: paths, thresholds, numeric limits |
| `skill-registry.json` | List of skill directories to scan (add project-local dirs here) |
| `skills/skill-router/` | The skill-router micro-skill, deployed to `~/.claude/agents/` |
| `install.sh` | End-to-end deployment script |

---

## Skill Versions

Every converted skill exists in two forms simultaneously:

| Location | Content | Purpose |
|----------|---------|---------|
| `~/.claude/skills/<name>/SKILL.md` | Micro-skill orchestrator (~30 lines) | Loaded by skill-router at session start |
| `~/.claude/skills/<name>/SKILL.md.original` | Original unmodified file | Audit trail, revert source |
| `~/.claude/skill-wiki/converted/<name>/` | Full pipeline (5 stage files) | On-demand stage loading during execution |
| `~/.claude/skill-wiki/entities/skills/<name>.md` | Wiki entity page | Usage tracking, tagging, status |

The original is never deleted or modified. Revert any skill by copying `.original` over `SKILL.md`.

---

## Adding More Skill Repositories

Edit `~/.claude/skill-registry.json` to add project-local or team skill directories:

```json
{
  "version": "1.0",
  "skill_dirs": [
    {"path": "~/.claude/skills",            "label": "global-skills", "enabled": true},
    {"path": "~/.claude/agents",            "label": "global-agents", "enabled": true},
    {"path": "/path/to/my/repo/skills",     "label": "my-project",   "enabled": true},
    {"path": "/path/to/team/shared-skills", "label": "team-shared",  "enabled": true}
  ]
}
```

All enabled directories are scanned by `scan_repo.py` and their skills appear in `catalog.md`.

---

## Troubleshooting

**UnicodeEncodeError on Windows when running batch_convert.py**

```bash
PYTHONIOENCODING=utf-8 python batch_convert.py --scan ~/.claude/skills --auto
```

Set this permanently in your shell profile to avoid repeating it:

```bash
export PYTHONIOENCODING=utf-8
```

**inject_hooks.py exits with "missing required arguments"**

Both `--settings` and `--ctx-dir` are required. `--ctx-dir` must be the absolute path to this repo so hook scripts can be referenced by absolute path in `settings.json`:

```bash
python inject_hooks.py --settings ~/.claude/settings.json --ctx-dir $(pwd)
```

**wiki_orchestrator.py reports health score below 70**

Run `--sync` to attempt automatic repair:

```bash
python wiki_orchestrator.py --sync
```

If the health score stays below 70, run lint to see specific issues:

```bash
python wiki_lint.py --wiki ~/.claude/skill-wiki
```

**Entity pages exist but no matching skill files (orphans after deleting skills)**

```bash
python wiki_lint.py --wiki ~/.claude/skill-wiki --fix-orphans
```

**batch_convert.py finds 0 eligible skills**

Check that `convert_line_threshold` in `config.json` is set correctly (default 180) and that the `skills_dir` path resolves to your actual skill library. Run with `--dry-run` to confirm file discovery:

```bash
python batch_convert.py --scan ~/.claude/skills --dry-run
```

**Hook not firing during Claude Code sessions**

Verify hooks are present in `~/.claude/settings.json`:

```bash
python inject_hooks.py --settings ~/.claude/settings.json --ctx-dir $(pwd) --check
```

---

## Credits

- Inspired by [Andrej Karpathy's LLM wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the concept of LLMs maintaining persistent, compounding knowledge wikis that grow more useful over time
- Micro-skill pipeline format from [stevesolun/micro-skills](https://github.com/stevesolun/micro-skills) — gated 5-stage pipeline (scope -> plan -> build -> check -> deliver) with YES/NO quality gates between stages
- LLM wiki skill implementation adapted from [NousResearch/hermes-agent](https://raw.githubusercontent.com/NousResearch/hermes-agent/refs/heads/main/skills/research/llm-wiki/SKILL.md)
- Extended wiki lifecycle concepts from [rohitg00's LLM wiki v2](https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2)

---

## License

MIT
