---
name: domain-modeling
description: Clarify domain concepts, language, boundaries, and durable architectural decisions. Use when terminology is ambiguous, the user is shaping a domain model, or a design depends on resolving what concepts mean.
---

# Model the domain

Build a precise shared understanding of the concepts that matter to the current
decision. Read existing glossaries, context maps, ADRs, code, and tests when
they provide relevant evidence.

## Sharpen the model

- Identify overloaded terms, hidden distinctions, and conflicting definitions.
- Use concrete scenarios and edge cases to test whether concepts and boundaries
  hold.
- Compare the stated model with behavior in the code and surface material
  contradictions.
- Propose clear language when ambiguity is blocking progress, while respecting
  established repository terminology that remains accurate.

Ask the user to resolve a term only when their intent cannot be inferred safely
and the distinction affects the outcome.

## Record durable knowledge proportionally

Update a glossary or context map when the user requests it or when a resolved
term is durable, project-specific, and useful beyond the current conversation.
Follow an existing repository format first; otherwise use the
[context format](CONTEXT-FORMAT.md) as a lightweight starting point.

Record an ADR only when a decision is costly to reverse, surprising without its
context, and based on a meaningful tradeoff. Follow existing ADR conventions or
use the [ADR format](ADR-FORMAT.md). Do not create artifacts merely to complete
the workflow.

Keep domain definitions separate from implementation plans. Report unresolved
ambiguity and artifact changes explicitly.
