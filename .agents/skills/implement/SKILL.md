---
name: implement
description: Implement work from a spec, ticket, or agreed conversation. Use explicitly when the user wants an existing plan carried through to working code.
---

# Implement planned work

Confirm the intended outcome and inspect the relevant code, tests, and
repository conventions. Resolve ambiguity only when it would materially change
the result; otherwise make a reasonable scoped assumption and proceed.

Implement the smallest coherent change that satisfies the source requirements.
Use test-first development when requested or when a focused failing test is the
clearest feedback loop. Avoid speculative abstractions and unrelated cleanup.

Validate the changed surface proportionally: start with focused evidence and
expand to broader checks when risk or repository contracts justify it. Use a
separate review when semantic risk remains or the user requests one.

Report what changed, what was verified, and any remaining uncertainty. Commit,
publish, or mutate tracker state only when requested or clearly included in the
active workflow.
