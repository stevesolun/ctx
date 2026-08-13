# Deepening

How to deepen a cluster of shallow modules safely, given its dependencies. Assumes the vocabulary in [SKILL.md](SKILL.md) — **module**, **interface**, **seam**, **adapter**.

## Dependency categories

When assessing a candidate for deepening, classify its dependencies. The category determines how the deepened module is tested across its seam.

### 1. In-process

Pure computation, in-memory state, no I/O. These are often the safest candidates
to consolidate and test through a new interface; no adapter is usually needed.

### 2. Local-substitutable

Dependencies that have local test stand-ins (PGLite for Postgres, in-memory
filesystem). Consolidation is attractive when the stand-in is representative
enough. Keep the seam internal unless callers genuinely need to select an
adapter.

### 3. Remote but owned (Ports & Adapters)

Your own services across a network boundary (microservices, internal APIs).
Consider a **port** at the seam when transport variation or isolation repays the
indirection. The deep module can own the policy while production and tests use
appropriate adapters.

Recommendation shape: *"Define a port at the seam, implement an HTTP adapter for production and an in-memory adapter for testing, so the logic sits in one deep module even though it's deployed across a network."*

### 4. True external (Mock)

Third-party services (Stripe, Twilio, etc.) you don't control. An injected port
and test adapter often keep vendor behavior outside the module's policy.

## Seam discipline

- **Require a concrete reason for a seam.** Multiple adapters are strong
  evidence, but ownership, policy, fault isolation, or an expensive dependency
  can also justify one.
- **Keep internal seams internal by default.** Expose them only when callers need
  the variation, not solely because tests use it.

## Testing strategy: replace, don't layer

- Remove superseded unit tests when interface-level tests provide equivalent or
  better confidence; retain focused tests that still catch distinct risks.
- Write new tests at the deepened module's interface. The **interface is the test surface**.
- Tests assert on observable outcomes through the interface, not internal state.
- Prefer tests that survive internal refactors and describe behavior rather than
  structure.
