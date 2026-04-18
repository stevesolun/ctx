# Skill router

The skill router decides which skills, plugins, and MCP servers load into
a session based on the active repository. The full router spec lives in
[`docs/SKILL.md`](https://github.com/stevesolun/ctx/blob/main/docs/SKILL.md);
this page summarizes the parts most relevant to the docs site.

## Problem

Every skill, plugin, and MCP server loaded into context costs tokens and
attention. Most projects need 3–8 skills out of 30+. Loading all of them:

- wastes the context window on irrelevant instructions,
- causes skill misfires (wrong skill triggers for a task),
- slows response time, and
- creates conflicting instructions between skills.

## Architecture

```
skill-router/
├── SKILL.md                    # Orchestration logic
├── references/
│   ├── stack-signatures.md     # File/config → stack id
│   ├── skill-stack-matrix.md   # Which skills serve which stacks
│   └── marketplace-registry.md # Known marketplaces
└── scripts/
    ├── scan_repo.py            # Scanner → stack profile JSON
    ├── resolve_skills.py       # Stack → skill set
    └── skill_loader.py         # Load/unload skills into session
```

## Flow

1. Repo opens (or Claude detects a `cd`).
2. `scan_repo.py` produces a stack profile.
3. `resolve_skills.py` maps the profile to a skill set using the
   [skill-stack matrix](../skill-stack-matrix.md).
4. `skill_loader.py` loads selected skills, unloads anything not in the
   set, and records the choice in the LLM Wiki catalog.

## Reference pages

- [Stack signatures](../stack-signatures.md) — the file/config patterns
  the scanner uses to identify stacks.
- [Skill-stack matrix](../skill-stack-matrix.md) — the mapping from stack
  identifiers to skill sets.
- [Marketplace registry](../marketplace-registry.md) — known skill
  marketplaces and query patterns.
