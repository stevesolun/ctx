---
name: shared-wiki-memory-pattern
description: Multi-agent systems need a shared, append-only memory — one canonical note per entity, updated by every agent that touches it
source: Distilled from Strix (https://github.com/usestrix/strix, Apache-2.0) — rev 15c95718
category: agent-architecture
---

# Shared Wiki Memory Pattern

When a swarm of agents explores the same target, the fastest way to waste cycles is to let each agent re-derive the same mental model. The wiki-memory pattern enforces a single canonical note per entity (e.g. per repository, per service, per user flow) that every participating agent reads first and updates before finishing.

## The Protocol

1. **At task start** — every agent MUST call `list_notes(category="wiki")` and `get_note(note_id=...)` to load the canonical note for its scope. This is non-optional.
2. **If no note exists** — the first agent to arrive creates it with `create_note(category="wiki")`. Subsequent arrivals find and reuse it.
3. **During work** — agents extend the note with new findings via `update_note`. Never create a second note for the same entity.
4. **Before finishing** — every agent appends a short delta update (what did I find, what still needs validation) before calling `agent_finish`.

## Note Anatomy (per repository)

Recommended sections, kept in this order so readers get oriented fast:

- **Architecture overview** — high-level shape: modules, entry points, deployment model
- **Entrypoints and routing** — how external traffic / invocations reach code
- **AuthN/AuthZ model** — identity flows, session handling, access checks
- **High-risk sinks and trust boundaries** — where untrusted data meets sensitive operations
- **Static scanner summary** — semgrep/ast-grep/secrets/trivy output, deduplicated
- **Dynamic validation follow-ups** — static findings that still need PoC

## Why One Note Per Entity

- **Convergence over divergence** — N agents produce one coherent story, not N conflicting ones.
- **Fast onboarding for new workers** — a worker spawned 20 minutes into a run reads the current wiki note in seconds and skips rediscovery.
- **Deduplication by construction** — if two agents are about to record the same finding, the second sees the first's entry and adapts.
- **Auditability** — the note's revision history is the assessment's reasoning log.

## Discipline Required

- Resist the temptation to create a parallel note. If the existing note is wrong, *update* it.
- Keep entries evidence-driven. A wiki bloated with speculation loses its value.
- Prefer bounded, query-driven entries over whole-repo dumps. "Here are the 4 SQL sinks we found" beats "here is grep output for every call to `execute`."

## When It Backfires

- Without concurrency discipline, updates can clobber each other (last writer wins). Either serialize updates, use append-only semantics, or use a CRDT-ish merge.
- If the note grows without compaction, it becomes context-poison for any agent that reads it. Periodically summarize and archive older sections.
- If agents skip the read-first step, the pattern provides zero benefit. Enforce it in the orchestration skill / system prompt, not as a polite suggestion.
