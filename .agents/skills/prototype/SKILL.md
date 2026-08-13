---
name: prototype
description: Build a throwaway prototype to answer a design question. Use when the user wants to sanity-check whether a state model or logic feels right, or explore what a UI should look like.
---

# Prototype

A prototype is disposable code that answers a specific design question. Let the
question, repository, and cheapest credible feedback loop determine its shape.

## Pick a branch

Infer the branch from the request and surrounding code:

- **Logic or state-model uncertainty** → read [LOGIC.md](LOGIC.md).
- **Visual or interaction uncertainty** → read [UI.md](UI.md).

If both are involved, prototype the riskiest uncertainty first. State a material
assumption instead of blocking when the choice can be reversed cheaply.

## Shared guidance

- Mark the artifact clearly as a prototype and follow existing project structure.
- Keep it runnable with a simple documented command or URL.
- Prefer in-memory or stubbed dependencies unless persistence or integration is
  the question being tested.
- Add only the error handling, tests, and structure needed for trustworthy
  feedback. Avoid production polish and speculative flexibility.
- Make the relevant state, alternatives, or outcomes easy to inspect.
- Stop when the evidence answers the question.

Report the answer, how to exercise the prototype, and its limitations. Do not
promote, commit, branch, publish, or preserve the artifact unless the user asks.
When preservation is useful, record the question and conclusion alongside it.
