---
name: ctx-dispatch
description: Coordinate nontrivial work in the ctx repository when a task has independent investigation, implementation, or test lanes; compute-heavy checks; or repetitive changes suited to scripts or codemods. Use for decomposition and concurrency decisions, not simple edits or questions.
---

# Dispatch ctx work

1. Identify the requested outcome, dependencies, and independently executable
   lanes. Keep simple work local.
2. Reserve subagents for bounded semantic work that benefits from isolated
   context.
3. Run lanes concurrently only when this reduces elapsed time without duplicate
   work or competing writes. Give each writer disjoint ownership; keep shared
   files with one owner.
4. Inspect CPU, memory, and disk pressure only before genuinely compute-heavy
   work, then size concurrency to the available capacity.
5. Keep scope, dependency resolution, and synthesis with one coordinator. Merge
   evidence from every required lane before reporting completion.
