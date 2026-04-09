# ctx — Alive Skill System

A runtime skill router for Claude Code that continuously scans your active project, loads only the skills relevant to your stack, and learns from session usage. It turns `~/.claude/agents/` and `~/.claude/skills/` from a flat pile into a context-aware, self-maintaining library.

---

## How It Works

```
Session start
  skill-router scans the project (scan_repo.py)
    -> resolves which skills match the stack (resolve_skills.py)
    -> loads <=15 highest-priority skills
    -> unloads stale ones

Every tool call
  context-monitor.py (PostToolUse hook)
    -> extracts intent signals (file extensions, framework keywords)
    -> writes to ~/.claude/intent-log.jsonl
    -> if >=3 new unmatched signals -> queues pending-skills.json

Session end
  usage-tracker.py (Stop hook)
    -> reads intent-log.jsonl
    -> updates use_count + last_used in wiki entity pages
    -> marks skills unseen >30 days as status: stale
    -> appends summary to skill-wiki/log.md
```

Skills over 180 lines are split into the micro-skill pipeline format (5-stage: scope -> plan -> build -> check -> deliver) so the router can load them incrementally.

---

## Prerequisites

- Python 3.10+
- Node.js 18+ (for the Babysitter SDK orchestrator)
- Claude Code CLI installed at `~/.claude/`
- `~/.claude/skills/` populated (global skill library)

---

## Installation

### Option A — Automated (Babysitter bootstrap)

```bash
# 1. Install SDK deps
cd ctx
npm install

# 2. Run bootstrap (pauses at two breakpoints for approval)
babysitter run:create   --process-id alive-skill-bootstrap   --entry "$(pwd)/.a5c/processes/alive-skill-bootstrap.js#process"   --inputs .a5c/inputs.json   --prompt "Bootstrap the alive skill system"   --harness claude-code   --plugin-root "~/.claude/plugins/cache/a5c-ai/babysitter/4.0.149"   --json
```

Bootstrap phases:

| Phase | What happens |
|-------|-------------|
| 1 | Init skill wiki at `~/.claude/skill-wiki/` |
| 2 | Catalog all 1,489 skills -> `catalog.md` |
| 3 | Deploy `skill-router` to `~/.claude/agents/skill-router/` |
| **[BREAKPOINT]** | Approve PostToolUse + Stop hook injection |
| 4 | Inject hooks into `~/.claude/settings.json` |
| 5 | Create `skill-registry.json` |
| 6 | Dry-run: count skills >180 lines |
| **[BREAKPOINT]** | Choose transform strategy |
| 7 | Transform skills to micro-skill pipeline format |
| 8 | Build dual-version sub-catalog (`versions-catalog.md`) |
| M1-M3 | Git commit each milestone |

### Option B — Manual

```bash
cd ctx

# Init wiki
python wiki_sync.py --init --wiki ~/.claude/skill-wiki

# Build catalog
python catalog_builder.py --wiki ~/.claude/skill-wiki

# Deploy skill-router
mkdir -p ~/.claude/agents/skill-router
cp -r skills/skill-router/. ~/.claude/agents/skill-router/

# Inject hooks
python inject_hooks.py --settings ~/.claude/settings.json --ctx-dir $(pwd)

# Transform long skills (optional but recommended)
PYTHONIOENCODING=utf-8 python skill-transformer.py --auto --scan ~/.claude/skills

# Build versions catalog
python versions_catalog.py --wiki ~/.claude/skill-wiki
```

---

## Configuration

### `config.json`

```json
{
  "claude_dir": "~/.claude",
  "wiki_path":  "~/.claude/skill-wiki",
  "intent_log": "~/.claude/intent-log.jsonl",
  "manifest":   "~/.claude/skill-manifest.json",
  "max_skills_loaded": 15,
  "stale_threshold_days": 30,
  "intent_signal_threshold": 3
}
```

### `.a5c/inputs.json`

```json
{
  "projectRoot":       "c:/path/to/ctx",
  "claudeDir":         "C:/Users/<you>/.claude",
  "wikiPath":          "C:/Users/<you>/.claude/skill-wiki",
  "transformStrategy": "all"
}
```

`transformStrategy`: `"all"` (skills >180 lines), `"large"` (>300 lines only), `"skip"`.

---

## Adding More Skill Repos

Edit `skill-registry.json` (deployed to `~/.claude/skill-registry.json`):

```json
{
  "version": "1.0",
  "skill_dirs": [
    {"path": "~/.claude/skills",  "label": "global-skills", "enabled": true},
    {"path": "~/.claude/agents",  "label": "global-agents", "enabled": true},
    {"path": "/path/to/my/repo/skills", "label": "my-project", "enabled": true}
  ]
}
```

---

## Skill Versions

After transform, each converted skill has:

```
~/.claude/skills/<skill-name>/
  SKILL.md              <- orchestrator (~30 lines, loaded by router)
  SKILL.md.original     <- original preserved for audit
  check-gates.md
  failure-log.md
  original-hash.txt
  references/
    01-scope.md
    02-plan.md
    03-build.md
    04-check.md
    05-deliver.md
```

To revert a single skill: rename `SKILL.md.original` -> `SKILL.md`.
Full list of dual-version skills: `~/.claude/skill-wiki/versions-catalog.md`.

---

## Manual Operations

| Task | Command |
|------|---------|
| Re-scan project | `python scan_repo.py --repo . --output ~/.claude/skill-manifest.json` |
| Re-resolve skills | `python resolve_skills.py --profile /tmp/stack-profile.json --wiki ~/.claude/skill-wiki --output ~/.claude/skill-manifest.json` |
| Update wiki | `python wiki_sync.py --profile /tmp/stack-profile.json --manifest ~/.claude/skill-manifest.json --wiki ~/.claude/skill-wiki` |
| Run usage tracker | `python usage-tracker.py --sync` |
| Dry-run transform | `PYTHONIOENCODING=utf-8 python skill-transformer.py --dry-run --scan ~/.claude/skills` |
| Transform one skill | `PYTHONIOENCODING=utf-8 python skill-transformer.py --auto --file ~/.claude/skills/<name>/SKILL.md` |
| Rebuild catalog | `python catalog_builder.py --wiki ~/.claude/skill-wiki` |

---

## File Reference

| File | Role |
|------|------|
| `scan_repo.py` | Scans repo -> `stack-profile.json` (detected stacks + confidence) |
| `resolve_skills.py` | Scores skills against stack profile -> `skill-manifest.json` |
| `wiki_sync.py` | Creates/updates skill wiki pages, index, and log |
| `context-monitor.py` | PostToolUse hook: extracts intent signals, writes `intent-log.jsonl` |
| `usage-tracker.py` | Stop hook: updates wiki use counts, marks stale skills |
| `skill-transformer.py` | Splits long SKILL.md files into micro-skill pipeline format |
| `catalog_builder.py` | Builds `catalog.md` listing all 1,489 skills+agents |
| `versions_catalog.py` | Builds `versions-catalog.md` listing dual-version skills |
| `inject_hooks.py` | Merges PostToolUse + Stop hooks into `~/.claude/settings.json` |
| `ctx_config.py` | Config singleton (reads `config.json` + env overrides) |
| `skill-registry.json` | List of skill directories to watch (extensible) |
| `skills/skill-router/` | The skill-router micro-skill (deployed to `~/.claude/agents/`) |

---

## Wiki Structure

```
~/.claude/skill-wiki/
  SCHEMA.md             <- tag taxonomy, conventions, update policy
  index.md              <- all pages listed with one-line summaries
  log.md                <- append-only action log
  catalog.md            <- bulk listing of all 1,489 managed items
  versions-catalog.md   <- 750 dual-version skills
  raw/
    scans/              <- per-repo scan results (JSON)
    marketplace-dumps/
  entities/
    skills/             <- one .md per discovered skill
      <skill>.md        <- frontmatter: use_count, last_used, status, tags
```

---

## Troubleshooting

**`UnicodeEncodeError` on Windows**
```bash
PYTHONIOENCODING=utf-8 python skill-transformer.py ...
```

**`inject_hooks.py` fails without args**
```bash
python inject_hooks.py --settings ~/.claude/settings.json --ctx-dir $(pwd)
```

**`skill-transformer.py` needs `--scan` or `--file`**
```bash
python skill-transformer.py --dry-run --scan ~/.claude/skills
python skill-transformer.py --auto   --scan ~/.claude/skills
```

**Babysitter session bind fails**
```bash
babysitter session:init --session-id <CLAUDE_SESSION_ID>   --state-dir ~/.claude/plugins/cache/a5c-ai/babysitter/4.0.149/skills/babysit/state   --run-id <RUN_ID>
```

---

## License

MIT
