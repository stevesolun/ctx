# ctx repository notes

ctx is a Python 3.11+ CLI and library for recommending bounded sets of skills,
agents, MCP servers, and harnesses.

## Repository gotchas

- The project is migrating from legacy flat modules to the `ctx` package. Both
  layouts are intentional until the migration phase removes the old one.
- Integration, browser, graph, platform, and release checks have different
  dependencies and costs. Select checks from the changed surface; validation,
  platform, and package-migration contracts live in `CONTRIBUTING.md`.

## On-demand workflows

- Prefer existing scripts, schemas, tests, and batched tools for deterministic
  work. Use an LLM only where semantic judgment adds value.
- For nontrivial work with independent lanes, load
  `.claude/skills/ctx-dispatch/SKILL.md` and act as dispatcher: spawn a
  right-sized swarm of relevant expert subagents in parallel, keep synthesis
  with one coordinator, and add independent reviewer or architecture/CTO
  passes when material risk warrants them.
- For material changes or reviews, load
  `.claude/skills/ctx-verify/SKILL.md`.
