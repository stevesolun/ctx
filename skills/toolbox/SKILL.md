---
name: toolbox
description: Pre/post dev toolbox — named bundles of skills/agents loaded before development work and councils of experts invoked after. Run /toolbox or the toolbox.py CLI to list, activate, initialize, export, import, and validate toolboxes. Invoke at the start or end of a dev session, when setting up a new repo, or when sharing toolboxes with a team.
type: feature
priority: 50
always_load: false
---

# toolbox

Named bundles of skills/agents that run **before** (`pre`) or **after** (`post`)
a development task. Learn patterns from past work and propose new bundles.
Invoke slash, pre-commit, session-end, or file-save.

## Quick reference

| Task                                       | Command                                 |
|--------------------------------------------|-----------------------------------------|
| Seed 5 starter toolboxes                   | `python src/toolbox.py init`            |
| List available toolboxes                   | `python src/toolbox.py list`            |
| Inspect one                                | `python src/toolbox.py show ship-it`    |
| Activate (global)                          | `python src/toolbox.py activate ship-it`|
| Export for sharing                         | `python src/toolbox.py export ship-it`  |
| Import a shared toolbox                    | `python src/toolbox.py import file.yaml`|
| Validate config                            | `python src/toolbox.py validate`        |

## Starter templates

| Name              | When to use                                                     |
|-------------------|-----------------------------------------------------------------|
| ship-it           | End-of-feature: 7-expert council on the change set              |
| security-sweep    | Security audit with guardrail blocking HIGH findings            |
| refactor-safety   | Refactors with graph-informed blast radius                       |
| docs-review       | When touching Markdown or API docs                              |
| fresh-repo-init   | Blank repo: run intent interview and scaffold                    |

## Data model

Global: `~/.claude/toolboxes.json`.
Per-repo (overrides global): `<repo_root>/.toolbox.yaml`.

Each toolbox declares:
- `pre` — skills/agents to load before work starts
- `post` — agents that form the council after work ends
- `scope.analysis` — `diff` | `full` | `graph-blast` | `dynamic`
- `trigger` — `slash`, `pre_commit`, `session_end`, or `file_save` glob
- `budget.max_tokens` / `budget.max_seconds` — hard stop on runaway cost
- `dedup.policy` — `fresh` (always re-run) or `cached` (reuse within window)
- `guardrail` — if true, block commit on HIGH findings

## Invoke when

- User says "I'm done with this feature" or commits a feature branch.
- User opens a fresh repo (triggers `fresh-repo-init` suggestion).
- User touches security-sensitive paths (triggers `security-sweep` file_save).
- User asks "what toolboxes do I have?" or "run the council".

## Reasoning

See `docs/roadmap/toolbox.md` for the full design rationale, open decisions,
and phase rollout.
