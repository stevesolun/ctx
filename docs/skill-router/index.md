# Skill router

The skill router decides which skills, agents, and MCP servers are useful for
the active repository and current development task. Harnesses are recommended in
the custom-model onboarding flow or loop adapters; after a harness is attached,
the same capped skills/agents/MCP recommendation layer can be used by that host.

## Problem

Every loaded skill, agent, and MCP server costs tokens, attention, and tool
surface area. Most sessions need a small top-scored bundle from the 68,494 skills, 467 agents, and 10,790 MCP servers in the shipped graph, not the whole inventory.
Loading too much:

- wastes context on irrelevant instructions,
- causes the wrong helper to trigger for a task,
- slows response time, and
- creates conflicting instructions between helpers.

## Architecture

```text
ctx/
|-- src/scan_repo.py                         # Repo scanner -> stack profile
|-- src/ctx/core/resolve/resolve_skills.py   # Profile -> load/unload manifest
|-- src/ctx/core/resolve/recommendations.py  # Shared recommendation engine
|-- src/ctx/adapters/                        # Host adapters + generic tools
`-- graph/wiki-graph-runtime.tar.gz          # Shipped graph/wiki runtime
```

## Flow

1. `ctx-scan-repo --repo . --recommend` scans the repository and produces stack signals.
2. The shared resolver scores graph/wiki entities by tags, categories, semantic
   edges, usage, quality, and configured gates.
3. The resolver returns a capped manifest: what to load, what to unload, and why.
4. The user confirms load/unload changes unless they configured automatic mode.
5. Usage and quality signals are recorded so future recommendations improve.

The same recommender is used by the CLI, MCP/library tools, Claude Code hooks,
LoopFlow/agent-loop adapter, and attached harness hosts. Entry points should
differ only in transport and confirmation UX, not in ranking logic.

## Reference Pages

- [Stack signatures](../stack-signatures.md) - file/config patterns used to
  identify stack signals.
- [Skill-stack matrix](../skill-stack-matrix.md) - stack-to-capability mapping
  used as scanner evidence.
- [Entity source registry](../marketplace-registry.md) - skill, GitHub,
  MCP, harness, and local sources plus update rules.
