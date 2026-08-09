# CTX Fit — Wayfinding Map

A low-resolution map of the CTX Fit effort: what is settled, what is a precise
open question, what is still fog, and what is deliberately out of scope. This
is an **index**, not a specification. Detailed evidence lives with the decision
or question that owns it.

Companion documents: [`CURRENT_STATE.md`](CURRENT_STATE.md) (audited facts),
[`IMPLEMENTATION_TRACKER.md`](IMPLEMENTATION_TRACKER.md) (execution ledger).

Status: charted 2026-08-09, after the Milestone 0 audit.

## Destination

A developer or engineering leader points CTX at a repository and receives a
decision they can act on:

> Here is the baseline we measured, the small set of configurations we tested
> and why we chose them, the verification evidence, what each cost, the best
> configuration **among those tested**, the limitations of the experiment, and
> the exact configuration changes required to adopt it.

The route is complete when that output is produced truthfully for a real
repository, with "no configuration beat the baseline" available as a valid
result.

## Decisions

Settled by the Milestone 0 audit. Each links to its evidence.

- **D1 — CTX Fit is a layer inside CTX, not a fork or a rewrite.** Rejecting a
  second parallel implementation is an explicit architecture constraint.
> **Superseded notice.** D2 and D8 below were settled under the earlier
> *evolution* mandate. The mandate changed to a rebuild, and both are now
> superseded by **ADR-011 / ADR-012** in [`DECISIONS.md`](DECISIONS.md):
> the experiment mechanisms are *extracted* into `ctx/core/execution.py` and
> `ctx/fit/experiment.py` rather than wrapped, and the benchmark scripts are
> deleted apart from the contamination controls and the deterministic bridge.
> `DECISIONS.md` is the authoritative record; D2 and D8 are retained here only
> as history.

- **D2 — (SUPERSEDED) The paired A/B runner is extended, never rewritten.**
  `scripts/ctx_ab_benchmark.py` already provides isolation, counterbalanced
  execution order, deterministic verification, and contamination controls.
  ([`CURRENT_STATE.md` §3](CURRENT_STATE.md))
- **D3 — The Fit repository profile extends `scan_repo.detect_stack()`.** A new
  analyzer would duplicate working repository intelligence.
- **D4 — CTX's bounded 0–5 selection and benefit closure are the search-space
  reducer.** This is the standing answer to combinatorial benchmark cost.
- **D5 — Token accounting is reused as-is; the dollar layer is new.** The
  existing usage normalizer refuses malformed usage rather than defaulting to
  zero, and that discipline must be inherited.
- **D6 — Implementation is gated on Milestone 0.** Satisfied by
  `CURRENT_STATE.md`.
- **D7 — The experiment's arm model is the central thing that must change.**
  Arms are a validated module constant and the treatment delta is a prompt
  suffix, so today the rig varies exactly one dimension.
- **D8 — (SUPERSEDED by ADR-012; the candidate-representation insight survives)**
  A candidate configuration is a `scenario.context` capability set, and each
  candidate runs as its own baseline-versus-candidate pair. Resolves Q1.
  `write_ctx_fixture` already materializes a per-run isolated CTX home from
  `scenario.context` (skills, agents, MCP servers) and installs skills into the
  harness, so CTX Fit orchestrates *above* the runner and changes none of its
  21 arm-coupling sites. Cost of the decision: candidates are compared
  indirectly, each against a shared baseline.
  ([`ARCHITECTURE_GAP_ANALYSIS.md` §1](ARCHITECTURE_GAP_ANALYSIS.md))
- **D9 — V1 varies CTX capability configuration within one harness, optionally
  across pinned models; harness comparison is out of scope.** Resolves Q4 from
  evidence rather than preference: the rig is Codex-only, `--model` is a
  run-level flag, and reasoning effort is fixed by the Codex runtime contract.
  The report must state plainly that harness was not varied.
- **D10 — CTX Fit lives in `src/ctx/fit/`,** the package layout, never the flat
  legacy modules. Resolves the placement half of Q8.
- **D11 — Spend is authorized per run by an explicit budget, never
  implicitly.** Resolves Q11. M1 and M2 perform no provider execution; from M3
  the pre-flight estimate, fail-safe-before-spend check, and `--dry-run` are
  the mechanism by which spend is authorized.

## Open questions

Precise enough to investigate now. Owner `coordinator` means a shared surface
that must be serialized; `research` means an independent read-only lane.

| ID | Question | Owner | Depends on | State |
| --- | --- | --- | --- | --- |
| Q1 | How does an arbitrary configuration become an experiment arm without forking the 522 KB runner? | coordinator | — | **Resolved → D8** |
| Q2 | Where does a price table come from, how is it versioned, and how does cost stay honestly unknown when usage is missing? | research | — | Open; research lane in flight |
| Q3 | How can valid, non-contaminated, verifiable tasks be derived from an arbitrary repository? | research | — | Open; research lane in flight. The hardest question. |
| Q4 | Is harness comparison in scope for V1? | coordinator | Q1 | **Resolved → D9** (single harness, from evidence) |
| Q5 | How are a repository's own verification commands discovered and trusted? | research | — | Open; research lane in flight |
| Q6 | What confidence statement is defensible at small task counts and single trials? | research | Q3 | Open |
| Q7 | What multi-objective / Pareto selection rule is used, and how is it explained? | research | Q2, Q6 | Open |
| Q8 | Where does `ctx fit` live, and what is the versioned result schema? | coordinator | — | **Placement resolved → D10**; result schema still open |
| Q9 | Verification depends on a Codex-managed macOS sandbox. What happens without it? | research | — | Open; research lane in flight |
| Q10 | How is a dollar budget enforced *before* spend, and how does the run fail safely? | coordinator | Q2 | Open; mechanism decided in D11, thresholds not yet designed |
| Q11 | Are cost-bearing provider runs authorized, and under what budget? | coordinator | — | **Resolved → D11** (per-run explicit budget; M1–M2 need none) |

## Fog

In scope, but not yet stateable as a precise question.

- **Continuous re-evaluation** (repository changed, new model, pricing changed).
  Revisit once the result schema exists (Q8).
- **Dashboard Fit view.** Depends on the result schema (Q8); extend the existing
  monitor rather than rebuilding it.
- **Configuration artifact generation and PR preparation.** Depends on knowing
  what a winning configuration actually contains (Q1, Q4).
- **Which dimensions are honestly comparable.** Depends on Q1 and Q4; the
  answer determines what the product may claim.
- **Dogfood corpus.** Development, calibration, and held-out repositories, with
  leakage prevention. Depends on Q3.

## Out of scope

Explicitly excluded from the current destination.

- Organization management, multi-tenant SaaS, billing, subscriptions,
  enterprise SSO, elaborate RBAC.
- Marketplace, public leaderboards, social features.
- Large visualization redesign or an arbitrary workflow builder.
- A generic agent platform, or a second knowledge-graph implementation.
- Recurring hosted evaluation infrastructure before the one-time Fit workflow
  works reliably.

## Frontier

Open, unblocked, and useful to advance now.

- **Concurrent research lanes (independent, read-only):** Q2, Q3, Q5, Q9.
- **Coordinator lane (shared surfaces, serialized):** Q1, then Q8.
- **Blocked on user judgment:** Q4 and Q11. Q11 blocks Milestones 3 and later
  outright; Milestones 1 and 2 need no provider spend and can proceed.

## Handoff condition

Wayfinding ends and specification begins when Q1, Q3, Q4, and Q11 are settled.
At that point the route to a first usable Fit report is execution rather than
uncertainty reduction, and the tracker can carry real tasks.
