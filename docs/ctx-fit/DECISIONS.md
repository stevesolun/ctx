# CTX Fit — Decision Log

Architectural decision records. Each decision is recorded once, with its
evidence. Do not reopen a settled decision without new evidence; record the new
evidence here instead.

Status values: `ACCEPTED` · `SUPERSEDED` · `PROPOSED`

---

## ADR-001 — CTX Fit becomes the primary product surface · `ACCEPTED`

**Context.** CTX had 44 public console scripts and required architectural
explanation before its value was apparent.

**Decision.** `ctx fit` becomes the product. The public surface collapses to
`ctx` (human) and `ctx-mcp-server` (machine contract name). Remaining useful
commands move under `ctx advanced`; the rest are archived or deleted.

**Consequences.** `README.md` and the docs hierarchy are rewritten;
`test_package_scaffold.py:126-149` changes because it asserts `run`/`resume`
appear in top-level help.

---

## ADR-002 — Treat existing CTX as a quarry, not a constraint · `ACCEPTED`

**Decision.** Extract proven IP; do not preserve module boundaries, command
structure, UX, or historical abstractions. The governing test for every
component is: *would we deliberately design this from zero?*

**Evidence.** `CTX_AUDIT.md` — 130 components classified; 78 marked
ARCHIVE or DELETE.

---

## ADR-003 — Recommendation is deterministic, never LLM-selected · `ACCEPTED`

**Decision.** The winner is chosen by documented policy: exclude below
verification threshold, exclude critical failures, build the Pareto frontier,
select the default by written rule. An LLM may *explain* a result; it may never
*decide* one.

**Rationale.** The product's only asset is trustworthy evidence. A model
choosing the winner would make the recommendation unauditable and
unreproducible.

---

## ADR-004 — Unknown cost stays unknown · `ACCEPTED`

**Decision.** Cost records carry an explicit completeness state
(`priced_exact` / `priced_partial` / `unpriced` / `unpriceable`). A total is
emitted only when exact. Folding two records takes the worse state. Incomplete
records are never compared as if complete.

**Evidence.** CTX already implements this discipline for tokens
(`ctx_ab_benchmark.py:4392-4410` refuses malformed usage) and for dollars
(`ctx run` emits nullable `cost_usd` with `attribution="unavailable"`). The
failure mode being prevented is a configuration appearing cheaper merely
because its usage data was missing.

---

## ADR-005 — Verification is repository-native · `ACCEPTED`

**Decision.** Success is judged by the repository's own tests, build, type
check, and lint. Self-reported completion never counts as verified. States
`attempted` / `completed` / `verified` / `failed` / `inconclusive` stay
distinct, and `flaky` / `infrastructure_failure` are represented rather than
retried away.

**Consequence.** A repository with no deterministic verification is honestly
reported as not Fit-evaluable rather than given a confident guess.

---

## ADR-006 — The knowledge graph is an internal optimizer, never UX · `ACCEPTED`

**Decision.** The graph answers exactly one question — *which 2–5 candidates are
worth evaluating?* Graph size, node counts, and entity taxonomy never appear in
ordinary output. Graph **consumption** is kept and shrunk to an extracted
ranking module; graph **production and curation** (~21,650 LOC) is archived as
not part of the product.

---

## ADR-007 — V1 varies capability configuration within one harness · `ACCEPTED`

**Decision.** V1 compares CTX capability sets and repository instructions
within a single harness, optionally across pinned models. Harness comparison is
out of scope and the report must say so.

**Evidence.** The execution rig is Codex-only; `--model` is a run-level flag;
reasoning effort is fixed by the Codex runtime contract. Claiming a harness
comparison the experiment never ran would violate the honesty rules.

---

## ADR-008 — Deletion requires a committed tree · `ACCEPTED`

**Decision.** No ARCHIVE or DELETE executes while the working tree is
untracked. The tree is committed to a branch first, so every removal is
recoverable.

**Rationale.** The engineering success metric is code removed, which makes
recoverability a safety precondition rather than a nicety.

---

## ADR-009 — Do not rename the distribution · `ACCEPTED`

**Decision.** Keep `claude-ctx` on PyPI. The product is named CTX Fit; the
distribution name is not user-facing after install, because users type `ctx`.

**Rationale.** A rename costs existing adoption, documentation links, and
release automation, and buys nothing the product promise needs.

---

## ADR-010 — The event-sourced capability lifecycle is out of scope · `ACCEPTED`

**Decision.** The consent-gated durable install/activation lifecycle
(`managed_query_service`, `managed_query_store`, `managed_artifact_registry`,
`composition`, `activation_execution`, the SQLite journal, reducer, replay, and
store) is removed from the CTX Fit product. The bounded planner, benefit
policy, observation normalization, and workspace identity are extracted and
kept.

**Evidence.** An AST import graph from every real entry point found
**10,741 LOC reachable only from tests**, confirmed by grep: no production
consumer exists outside `src/tests/` and `src/ctx/runtime/` itself.

**Note.** This work is recent, well tested, and was correct against the
*previous* goal. The goal changed. CTX Fit evaluates configurations in
throwaway workspaces and emits config files for a human to apply; it needs no
durable consent ledger. Retaining it because it was expensive to build is the
sunk-cost reasoning the mandate forbids.

---

## ADR-011 — Extract the experiment engine; do not wrap the benchmark script · `ACCEPTED`

**Decision.** Extract workspace isolation, counterbalancing, usage extraction,
verification execution, and validity controls into
`src/ctx/core/execution.py` (~450 LOC) and `src/ctx/fit/experiment.py`
(~450 LOC). Delete ~21,100 LOC of `ctx_ab_*` scripts. Keep the deterministic
provider bridge as a test support module.

**Consequences.** Net ≈ −38,000 LOC including tests. The extracted code is
importable, testable, and arm-agnostic, unlike the current script.

---

## ADR-012 — Supersedes D8 (black-box benchmark reuse) · `ACCEPTED`

**Supersedes.** `ARCHITECTURE_GAP_ANALYSIS.md` D8, which decided CTX Fit would
call `scripts/ctx_ab_benchmark.py` unmodified as a pairwise primitive.

**Why it changed.** D8 was correct under the *evolution* mandate, where leaving
a 522KB module untouched was the dominant concern. Under the rewrite mandate,
extracting ~900 LOC of real mechanism and deleting ~21,100 LOC is better than
preserving a script that cannot be imported and hardcodes its arms in 21
places. The mandate explicitly prefers deletion over wrapper chains.

**What survives from D8.** The insight that a candidate configuration already
has a representation (`scenario.context`: skills, agents, MCP servers) remains
valid and carries into the extracted design.

---

## ADR-013 — `ctx fit` is safe, free, and read-only by default · `ACCEPTED`

**Decision.** Bare `ctx fit` performs no model execution and modifies nothing.
Spend requires `--test` plus an explicit `--budget`, with a dry run and a
confirmation showing candidates, tasks, executions, and estimated cost.

**Consequence.** Milestones 1–5 are buildable and useful with zero provider
authorization; only Milestone 6 onward requires it.
