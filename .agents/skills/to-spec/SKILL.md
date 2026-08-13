---
name: to-spec
description: Synthesize an implementation-ready spec from the current conversation and relevant repository context. Use explicitly when the user wants agreed work captured as a spec or PRD.
---

# Create a spec

Capture the decisions already made. Resolve only gaps that would materially
change the intended behavior, scope, or feasibility; do not reopen settled
questions for the sake of completing a template.

Inspect the relevant implementation, tests, domain language, and architectural
decisions when they improve fidelity. Keep facts observed in the repository
distinct from decisions inferred from the conversation.

Describe:

- the user-visible problem and desired outcome;
- behavior, edge cases, and acceptance criteria;
- constraints and decisions that implementation must preserve;
- relevant interfaces or test seams when they are already known; and
- explicit exclusions and unresolved questions.

Prefer durable behavioral contracts over file paths and implementation recipes.
Include code or a type shape only when it records a decision more precisely than
prose.

Use the [spec format](SPEC-FORMAT.md) when a structured document helps. Return,
save, or publish the spec in the destination the user requested. Treat tracker
publication and label changes as separate mutations requiring clear scope.
