---
name: skill-router
description: Alive skill router — reads the current project's stack and loads/unloads skills dynamically. Invoke at session start or when project context changes.
type: meta
priority: 99
always_load: true
---

# skill-router

Reads `~/.claude/skill-manifest.json` and `~/.claude/pending-skills.json`.
Executes the 5-stage pipeline. **No skipping stages.**

## Pipeline

```
01-scope → 02-plan → 03-build → 04-check → 05-deliver
```

## Invoke When

- Session starts (automatic via using-superpowers pattern)
- User switches projects or opens a new repo
- `pending-skills.json` exists (mid-session signals detected)
- User asks "what skills are loaded?" or "update my skills"

## Stage Files

All stage files are in `references/`. Read and follow each in order.

1. **[01-scope](references/01-scope.md)** — Is a scan needed?
2. **[02-plan](references/02-plan.md)** — Run the scanner
3. **[03-build](references/03-build.md)** — Resolve the manifest
4. **[04-check](references/04-check.md)** — Validate before loading
5. **[05-deliver](references/05-deliver.md)** — Present to user

See [check-gates.md](check-gates.md) for validation questions.
