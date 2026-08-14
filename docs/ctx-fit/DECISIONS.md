# CTX Fit — Decision Log

Architectural decision records. Each decision is recorded once, with its
evidence. Do not reopen a settled decision without new evidence; record the new
evidence here instead.

Status values: `ACCEPTED` · `SUPERSEDED` · `PROPOSED`

---

## ADR-016 — The repository verifier is an explicit trust boundary · `ACCEPTED`

**Context.** ADR-005 makes the repository's own command authoritative, but did
not state the limit of that authority. A Python-only launcher attempted to prove
that the verifier reached normal completion with a secret in-process witness.
Editable source defeated it twice: once by forging the traceback filename the
launcher trusted, and once by reading the secret from the launcher's live frame.
Both programs exited before the assertion and still produced `verified`.

**Decision.** CTX Fit executes the selected repository command unchanged and
uses its exit status for Python, JavaScript/TypeScript, Go, and Rust. A trial is
verified only after the original commit passes, reverting the source makes the
same command fail, the agent changes only declared source paths, protected tests
remain byte-for-byte unchanged, and the same command exits zero after the
change.

This is evidence for normal, non-adversarial development. CTX confines trial
code to a throwaway workspace and protects the judge's files, but it does not
claim that code under test cannot deliberately terminate, skip, or deceive the
test runner whose process it inhabits. A universal same-process completion
proof is not available: any secret or visible negative control needed by that
process can also be observed or influenced by code executing inside it.

**Consequences.**

- The trust assumption is displayed in the dry run and exact pre-spend plan,
  serialized in plan warnings, and retained in every recommendation's
  limitations.
- A Python-only wrapper must not replace or reject a repository-native command.
  Framework-specific adapters may strengthen future evidence, but they cannot
  be described as a universal adversarial proof.
- Host isolation and judge-file integrity remain hard boundaries. Repository
  verification can write only inside its individual throwaway workspace, not
  the shared campaign environment or the user's machine.

---

## ADR-015 — A trial stopped by a CTX-imposed bound is inconclusive, except the iteration cap · `ACCEPTED`

**Context.** The verdict branch in `src/ctx/fit/live_runner.py` reads only the
test exit code: anything non-zero becomes `outcome="failed"`, which
`counts_toward_reliability`. The harness's `stop_reason`, carried on
`AgentOutcome.detail` (`providers.py:338-339`), is never consulted there. So a
trial CTX itself cut short at the $2.00 per-trial budget cap
(`providers.py:44`) or at 25 iterations (`providers.py:40`) is recorded as the
candidate failing. The 900-second subprocess timeout is **not** in that set:
`subprocess.TimeoutExpired` is a `SubprocessError`, so the driver returns
`completed=False` with no tokens and no cost, and `live_runner`'s
spent-nothing guard already records `infrastructure-failure` with
`counts_toward_reliability False`.

**Decision.** The verdict is attributed by what stopped the run, in three parts:

1. **Budget caps are inconclusive.** A trial cut off by the per-trial
   budget cap — the one remaining case, since timeouts and provider errors
   already record `infrastructure-failure`
   did not test the candidate; it tested a bound CTX imposed on itself. It is
   recorded `inconclusive` and does not count toward reliability.
2. **The iteration cap is a real candidate failure.** An agent that burned its
   iteration allowance and real tokens without finishing has been shown to fail
   the task. That stays `failed` and counts.
3. **Every outcome keeps its `stop_reason` and its logs**, whatever the verdict,
   so any attribution can be re-derived and audited rather than taken on trust.

**Rationale.** Reliability is a constraint, not a metric (ADR-014), so a
candidate excluded by the floor is excluded absolutely — with the default floor
of 1.0, one mis-attributed trial disqualifies it and adaptive stopping abandons
the rest. Blaming a candidate for CTX's own ceiling therefore corrupts the one
gate the recommendation rests on. But "spending nothing while failing to finish
is the signature of a harness fault; burning tokens and still not finishing is a
real candidate failure" is already this module's rule, and it is correct. The
line is drawn at whether the bound truncated a productive run (budget, wall
clock) or recorded an unproductive one (iterations exhausted).

**Evidence.** FITBUG-040, confirmed by execution. A stand-in harness returned
`stop_reason="cost_budget"` having spent the full $2.00 without finishing; the
trial was recorded `outcome="failed"`, `cost 2.0`,
`counts_toward_reliability True`. A genuine provider failure is **not**
affected: `providers.py`'s "harness could not run" `OSError`/`SubprocessError`
spends nothing, so the spent-nothing guard already routes it to
`infrastructure-failure` and it is excluded from reliability. Only the two caps
above reach the `failed` branch.

**Consequences.**

- **`live_runner` must read `agent.detail`, not just the exit code.** This is
  delivered as ARCH-5 in `ARCHITECTURE_CANDIDATES.md`.
- **The existing test is preserved, not weakened.**
  `test_an_agent_that_burned_tokens_without_finishing_is_a_real_failure`
  (`src/tests/fit/test_live_runner.py:511`) asserts the `max_iterations` case is
  `failed`; part 2 above is what keeps it green.
- **The reason must be read, not the `completed` flag.** A `cost_budget` stop
  arrives as `completed=True` with the stop reason smuggled into `detail`, so an
  implementation that branches on `completed` gets this case wrong.
- **Inconclusive trials must stay visible.** Because ADR-014 requires the
  confidence model to report how many trials backed a result, an attribution
  that removes a trial from the reliability count cannot also remove it from the
  record.

---

## ADR-014 — The objective is "cheapest that reliably works" · `ACCEPTED`

**Product definition (authoritative).**

> Connect your GitHub repository. CTX Fit finds the cheapest AI coding setup
> that reliably works on your codebase, then opens a PR containing the winning
> configuration.
>
> Headline: *Find the cheapest AI coding setup that actually works on your repo.*

**Decision.** The winner is chosen by a lexicographic rule, not a weighted score
and not a general Pareto search:

1. **Filter on reliability.** A candidate qualifies only if its verified
   success rate across repeated trials meets the reliability floor. A
   configuration that sometimes works has not been shown to work.
2. **Minimize attributable cost** among the qualifying candidates.
3. **Tie-break toward simplicity** — fewer capabilities, less context — because
   a simpler configuration is cheaper to maintain and less likely to drift.

**Why this replaces the previous multi-objective framing.** The earlier PRD
proposed a Pareto frontier with a documented default. That left the actual
choice under-specified, and "recommendation scoring becomes arbitrary" is one of
this project's own stop conditions. A lexicographic rule is deterministic,
explainable in one sentence, and needs no weights to defend. The Pareto view
survives as a *presentation* (`BEST QUALITY` / `FASTEST` alongside
`RECOMMENDED`), never as the selection mechanism.

**Consequences.**

- **Reliability is a constraint, not a metric.** Repeated trials move from
  "when budget permits" to required for any recommendation, and the confidence
  model must report how many trials backed the result.
- **Cost is the objective, so cost completeness is load-bearing.** A candidate
  whose cost is `priced_partial` or `unpriced` cannot be declared cheapest;
  it is reported as unranked rather than silently winning (ADR-004).
- **"Keep your current setup" is the expected answer whenever no candidate
  clears the floor more cheaply.** This is a success, not a failure.
- **The PR is the terminal deliverable.** M10 is no longer optional polish; the
  product promise is not met until a reviewable PR exists.
- **The `lean` candidate role becomes central.** Testing whether fewer
  capabilities suffice is the most direct route to "cheapest that works", not a
  curiosity.

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

ADR-016 states the authority boundary: repository-native verification is
evidence for ordinary development, not an adversarial proof about code running
inside the verifier itself.

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

**1.0.21 implementation note.** The current bounded experiment varies skill
capabilities only. Repository instructions, the selected model, and the single
harness are held constant across all arms; agents and MCP servers are not yet
attached as candidate material. The profile and dry run must report this
narrower implemented surface, even though the ADR permits later expansion
within one harness.

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
