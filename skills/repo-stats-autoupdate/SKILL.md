---
name: repo-stats-autoupdate
description: Keeps README badge + inline counts in sync with the real number of skills, agents, graph nodes/edges, communities, converted pipelines, and pytest tests. Runs automatically on every commit via a git pre-commit hook. Use when the README drifts from reality or before publishing a release.
type: maintenance
priority: 30
always_load: false
---

# repo-stats-autoupdate

## What this skill does

Reads the **authoritative sources** for the ctx repo's key numbers and patches `README.md` in place so badges and inline counts never drift:

| Number | Source of truth |
|---|---|
| Skills | `~/.claude/skill-wiki/graphify-out/graph.json` — count nodes where `type == "skill"` |
| Agents | same file — count nodes where `type == "agent"` |
| Graph nodes | `len(graph["nodes"])` |
| Graph edges | `len(graph["edges"])`, formatted as `642K` / `1.2M` |
| Communities | `graph/communities.json` → `total_communities` |
| Converted pipelines | `~/.claude/skill-wiki/converted/` subdir count |
| Tests | `pytest --collect-only -q` from `src/` |

Fields that can't be resolved (e.g. wiki not deployed) are left untouched — the updater never blocks a commit.

## How to use it

**One-time install (per clone):**
```bash
git config core.hooksPath .githooks
```

After that, every `git commit` runs `.githooks/pre-commit` which calls the updater and re-stages `README.md` if any number drifted.

**Manual run:**
```bash
python src/update_repo_stats.py          # patch README
python src/update_repo_stats.py --check  # exit 1 if stale (for CI)
```

## When NOT to use it

- When you've deliberately phrased a number descriptively (e.g. "over 1,700 skills") — the regex patterns only match exact digits, so prose phrasings are safe, but double-check after big imports.
- When the wiki isn't deployed locally. The updater degrades gracefully: it prints a warning and skips fields it can't resolve. It does **not** invent numbers.

## Known gaps (intentionally out of scope)

- **Does not rebuild the graph or wiki.** Rebuilding `graph/wiki-graph.tar.gz` takes minutes and churns a 159 MB tree — too heavy for per-commit. Run `python src/wiki_graphify.py` and repack the tarball manually when the skill catalog changes materially.
- **Does not verify graph integrity.** If `graphify-out/graph.json` is corrupt, you'll get junk numbers. Run the wiki health check (`python src/wiki_orchestrator.py --check`) separately.

## Related files

- [src/update_repo_stats.py](../../src/update_repo_stats.py) — the worker
- [.githooks/pre-commit](../../.githooks/pre-commit) — the per-commit trigger
- `README.md` — the target being kept in sync
