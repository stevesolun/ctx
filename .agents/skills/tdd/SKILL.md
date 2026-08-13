---
name: tdd
description: Apply test-driven development with small, behavior-focused feedback loops. Use when the user asks to build or fix something test-first, mentions red-green-refactor, or wants integration tests.
---

# Test-Driven Development

Use small red → green → refactor cycles when a test-first feedback loop is
practical. Treat repository conventions, existing tests, and the requested
behavior as authoritative.

## Loop

1. Identify one observable behavior and the seam through which callers see it.
2. Write the smallest meaningful test and confirm that it fails for the expected
   reason.
3. Add only enough implementation to make the test pass.
4. Refactor when doing so improves clarity or design; keep the suite green.
5. Repeat with the next vertical slice.

Infer seams from public interfaces and nearby tests. Ask the user only when
choosing a seam would materially change the product contract, cost, or scope.

## Judgment

- Prefer behavior-focused tests that survive internal refactors.
- Derive expected results from a specification, worked example, or other
  independent source rather than duplicating the implementation.
- Mock external or nondeterministic boundaries when useful; follow established
  repository patterns where they provide an adequate feedback loop.
- Prefer vertical slices over a speculative batch of tests. Batch closely
  related mechanical cases when that is clearer and faster.
- If a failing-first cycle is impractical—for example in legacy code with no
  usable seam—state the limitation and establish the nearest reliable
  characterization or integration test.

Read [tests.md](tests.md) for examples and [mocking.md](mocking.md) when deciding
whether and where to use test doubles.
