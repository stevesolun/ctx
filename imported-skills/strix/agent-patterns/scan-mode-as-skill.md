---
name: scan-mode-as-skill
description: Operational modes (quick/standard/deep) should be first-class skills, not hard-coded branching — each mode defines its own phases, priorities, and completion criteria
source: Distilled from Strix (https://github.com/usestrix/strix, Apache-2.0) — rev 15c95718
category: agent-architecture
---

# Scan-Mode-as-Skill

Rather than hard-coding `if mode == "quick": do_x() else: do_y()` in the agent runtime, treat each operational mode as its own skill that the root agent loads at session start. Each mode skill encodes the phases, priorities, and exit criteria for that mode.

## The Three Canonical Modes

**Quick** — time-boxed rapid assessment
- Prioritize breadth over depth.
- Focus on recent changes (git diffs, modified files) — most likely to contain fresh bugs.
- Load existing wiki notes instead of remapping from scratch.
- Run fast static triage scoped to changed paths.
- One of each essential pass: `semgrep`, `ast-grep` (or `tree-sitter`), secrets, `trivy fs` — scoped.
- Use case: CI/CD check on a PR.

**Standard** — balanced systematic assessment
- Full attack surface mapping, but not exhaustive depth on every surface.
- Understand the application before exploiting it.
- Complete authentication/authorization review.
- All major input vectors tested with primary techniques.
- Use case: routine security review, release gate.

**Deep** — exhaustive assessment
- Maximum coverage, maximum depth. Finding what others miss is the goal.
- Multi-phase: exhaustive recon → business logic deep dive → comprehensive attack surface → vulnerability chaining → persistent testing → comprehensive reporting.
- Agents decompose hierarchically: component → feature → vulnerability, then scale horizontally.
- Use case: thorough audit, adversarial assessment, post-incident deep dive.

## Why This Beats Hard-Coding Modes

- **Transparent** — users can read exactly what "deep" means in markdown, not reverse-engineer control flow.
- **Extensible** — add `pr_review`, `red_team`, `compliance_scan` modes by dropping in new skill files.
- **Tunable per domain** — an org can fork the skill to reflect its own priorities without forking the engine.
- **Reusable pattern** — the same pattern works for any system that has "modes of operation": deployment modes, migration modes, refactor modes.

## Anatomy of a Mode Skill

- **Phase 1..N** — ordered steps with clear enter/exit criteria
- **Whitebox vs blackbox variants** — most modes apply differently depending on input
- **Agent strategy** — how to decompose work at this depth (what workers to spawn, how to parallelize)
- **Completion criteria** — what "done" looks like for this mode
- **Mindset guidance** — one paragraph setting the attitude (relentless vs. fast vs. thorough)

## Portability Beyond Security

The scan-mode-as-skill pattern maps cleanly onto any multi-step agent workflow where depth/time/rigor is a knob:
- **Code review**: quick (syntax + obvious bugs) / standard (+ architecture) / deep (+ perf + security + maintainability)
- **Refactoring**: quick (extract method) / standard (restructure module) / deep (cross-module redesign)
- **Documentation**: quick (API signatures) / standard (+ usage examples) / deep (+ architectural context + migration notes)

Each mode becomes a first-class, versioned, diffable artifact.
