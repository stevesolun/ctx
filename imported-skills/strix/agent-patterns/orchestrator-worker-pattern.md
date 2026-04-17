---
name: orchestrator-worker-pattern
description: Hierarchical agent pattern — a root orchestrator decomposes work and spawns specialized workers with focused context
source: Distilled from Strix (https://github.com/usestrix/strix, Apache-2.0) — rev 15c95718
category: agent-architecture
---

# Orchestrator/Worker Agent Pattern

The root agent owns scope decomposition and aggregation. Specialized workers own narrow, parallelizable tasks. This pattern is what keeps multi-agent systems from drowning in context.

## Roles

**Root / Orchestrator**
- Decomposes the target into discrete, parallelizable tasks
- Decides *when* to spawn workers, not just at t=0 — spawn continues throughout execution as new findings emerge
- Holds the global view; workers hold local views
- Aggregates and de-duplicates worker output
- Never performs primitive operations itself (no direct file reads, no direct tool calls on the target)

**Specialized Worker**
- One specific, measurable objective per worker
- Narrow capability scope — loaded with only the skills relevant to its objective (e.g. `authentication_jwt`, `idor`)
- Emits structured findings back to orchestrator
- Terminates on success, explicit failure, or when its scope is invalidated

## Coordination Principles

1. **Task Independence** — parallel > sequential. Design each worker so it does not block on another worker's output.
2. **Clear Objectives** — vague goals cause scope creep and duplicated work. Every worker's system prompt should answer "what does done look like?"
3. **Avoid Duplication** — before spawning, the orchestrator must check whether an existing worker already covers the scope.
4. **Hierarchical Delegation** — workers can spawn sub-workers when their scope expands (e.g. a discovery worker finds an endpoint → spawns a validator worker → spawns a report worker).
5. **Minimum Message Passing** — message passing is reserved for critical handoffs (request/answer pairs). Prefer batched updates to routine status pings. Every message is context a worker has to re-read.
6. **Resource Efficiency** — terminate workers when objectives are met or made irrelevant by new findings.

## Completion Protocol

When all workers report done:
1. Collect and deduplicate findings (different workers frequently observe the same root cause from different angles).
2. Assess overall posture / coverage — what wasn't tested, and why.
3. Compile executive summary with prioritized recommendations.
4. Invoke a final reporting tool (or agent) to produce the deliverable.

## Why It Works

- Each worker has a **fresh context window** tuned to its narrow task — they see only the skills and tools they need.
- The orchestrator never fills its own context with raw tool output — workers pre-digest.
- Parallelism is the primary speedup lever; this architecture is the enabling structure.

## When NOT to Use

- Single-file edits or one-shot queries — the overhead of spawning swamps the benefit.
- Strongly sequential workflows where each step depends on the last's full output — use a pipeline, not a swarm.
- When workers would all load the same large skills — the context savings evaporate.
