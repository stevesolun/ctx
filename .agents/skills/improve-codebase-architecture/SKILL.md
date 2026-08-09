---
name: improve-codebase-architecture
description: Analyze a codebase for high-leverage architectural improvements and explain evidence, tradeoffs, and next steps. Use explicitly for architecture audits or deep-module refactoring opportunities.
---

# Improve Codebase Architecture

Look for architectural friction that a focused structural change could reduce.
Favor improvements that shrink interfaces, concentrate related complexity, and
make important behavior easier to test and understand.

## Scope the investigation

Follow the user's named subsystem or pain point. Otherwise use recent changes,
repeated maintenance friction, test difficulty, or dependency structure to
select a useful area. Read relevant domain language and ADRs when present so
recommendations respect intentional boundaries.

Use the `codebase-design` skill when its deep-module vocabulary will sharpen the
analysis. Explore independently in parallel only when the repository is large
enough to benefit and the lanes do not duplicate work.

## Find evidence

Consider:

- interfaces that expose nearly as much complexity as their implementations;
- behavior spread across modules that change together;
- dependency seams that leak internal details;
- testing that bypasses the real call path because no stable seam exists; and
- repeated wrappers or indirection that add little leverage.

Treat these as prompts for investigation, not violations. Apply the deletion
test where useful: would removing a layer concentrate complexity behind a
better interface, or merely move it elsewhere?

## Present candidates

For each material candidate, cite the affected code and explain:

- the observed friction;
- the structural change being considered;
- expected benefits and tradeoffs;
- evidence that the seam is real rather than hypothetical;
- test impact and migration risk; and
- conflicts with existing decisions.

Rank recommendations by evidence and likely leverage. Keep speculative ideas
clearly labeled and avoid proposing a wide refactor solely for aesthetic
consistency.

Use prose, a compact table, or diagrams according to what best communicates the
relationships. When a visual HTML artifact is useful and authorized, load the
[HTML report guide](HTML-REPORT.md).

Explore a selected candidate further only when the user requests it or the
current task includes design. Update domain records or ADRs only when those
artifacts are in scope and the decision is durable enough to justify them.
