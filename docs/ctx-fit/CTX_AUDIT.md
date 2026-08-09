# CTX Audit — The Quarry

Milestone 0 deliverable. Every significant CTX subsystem classified
`KEEP` / `ADAPT` / `ARCHIVE` / `DELETE` against the CTX Fit product mandate.

Method: five parallel read-only classification lanes plus coordinator
verification. **130 components** classified. Every claim is anchored to a
`file:line` or an executed command. The governing question for each component
was: *if we were creating CTX Fit today from zero, would we deliberately design
this?*

Date: 2026-08-09. Baseline: ~156K source LOC, ~186K test LOC, 8,233 tests, 44
public console scripts, 19-lane preflight green.

## 1. Result

| Classification | Components |
| --- | --- |
| KEEP | 14 |
| ADAPT | 38 |
| ARCHIVE | 36 |
| DELETE | 42 |

**Public surface: 44 console scripts → 2** (`ctx`, `ctx-mcp-server`) — a 95
percent reduction. Only `ctx` is meant for humans; `ctx-mcp-server` survives
because its *name is a contract string* embedded in shipped artifacts and in
any MCP configuration `ctx fit --apply` writes.

Removable code, per lane, before de-duplicating overlap:

| Lane | Source LOC removable | Notes |
| --- | --- | --- |
| A — Public CLI | ~21,900 | includes the 12,205-LOC monitor package also counted in E |
| B — Graph/catalog | ~25,100 | production and curation, not consumption |
| C — Engine/runtime | see §3 — **two different claims, only one proven** | |
| D — Benchmark | ~21,100 gross | estimate revised; see §3a |
| E — Supporting | ~32,000 | plus ~29,000 test LOC |

Lanes overlap (the dashboard is counted in both A and E), so these do not sum
cleanly. The honest statement is: **on the order of 100K+ source LOC and 80K+
test LOC are removable** — well over half the repository — without losing any
capability CTX Fit needs. Precise de-duplicated figures are computed during the
deletion program, not asserted here.

## 2. The new core — everything classified KEEP

This is the entire surviving foundation. It is remarkably small, and it is the
most useful output of the audit.

| Component | LOC | Why it earns its place |
| --- | --- | --- |
| `ctx` console script (`ctx.cli.run:main`) | — | The one binary a human types. Already hosts `fit`. |
| `ctx-mcp-server` | — | Machine entrypoint; its name is a hard-coded contract in shipped assets and applied configs. |
| Distribution name `claude-ctx` | — | **Do not rename.** Users type `ctx`, never the distribution name; a rename costs adoption and buys nothing. |
| `src/ctx/engine/planner.py:46-593` | ~550 | `BoundedCapabilityPlanner` — literally the mandate's "candidate search-space reduction with explainable selection". Pure, no I/O. |
| `src/ctx/engine/benefit.py` + `capability_schema.py` | 1,695 | The cost/benefit policy that makes "verified work per dollar" possible, and stops a catalog scoring itself. |
| `src/ctx/engine/observation.py` | 274 | Maps repository + task to planner signals without leaking private prose into artifacts. |
| `runtime-availability.json` + `eligible_catalog.py` + `production_catalog.py` | 1,845 + 19KB | The real product asset: where a candidate is simultaneously identified, described, ranked, installable, and verifiable. |
| `src/ctx/runtime/workspace_identity.py` | 95 | Proves a trial ran where it claims. |
| Validity controls (`validate_evaluator_controls`) | 61 | The cheapest guard against a meaningless experiment — a task that already passes, or that no patch can satisfy. |
| Honest token accounting (`extract_token_usage`) | — | The reference implementation of "unknown stays unknown". |
| Deterministic provider bridge | 646 | Tests the pair executor without spending provider tokens. |
| `src/ctx/adapters/hook_config.py` | 298 | Locked, validated JSON merge — exactly what `--apply` needs to edit a user's settings safely. |
| `src/ctx/utils/` (`_fs_utils`, `_file_lock`, `_secret_scan`, `_safe_name`) | 1,222 | Highest value-per-LOC in the repository. Fit executes untrusted repository code and writes into user repositories. |
| `src/cosine_ranker.py` | 174 | Dependency-light, correct, the only part of the embedding stack worth keeping. |

Total surviving core: **roughly 7,000 LOC** plus a 19KB catalog asset.

## 3. The hardest finding — the event-sourced capability engine

Lane C examined `src/ctx/engine/` (20,959 LOC) and `src/ctx/runtime/`
(28,875 LOC) — 49,834 source LOC backed by 47,301 test LOC and 1,652 tests,
roughly 23 percent of the whole suite. This is recent, heavily reviewed,
high-quality work.

**Corrected after independent architecture review.** An earlier draft of this
section headlined "~46,200 of 49,834 (93%) removable". That figure conflated
two different claims, and only one of them is proven. The correction matters
more than the original number, so it is stated first.

| Claim | Status | Figure |
| --- | --- | --- |
| **Proven dead** — reachable only from tests, no production consumer | **Verified twice, independently** | **10,741 LOC** |
| **Not needed by the future product** — reachable today, but serves a lifecycle CTX Fit does not have | Judgement, not reachability | remainder of the lane |

A second reviewer rebuilt the AST import graph from **all 45 console-script
entry points** — a broader seed set than the original audit used — and
deliberately did *not* suppress the `__init__` re-export hubs. It reproduced
the 10,741 figure exactly, and concluded the original audit **understated** the
dead-code finding.

But the same graph contradicts the 93 percent headline as written: with today's
entry points, **35,993 LOC of the lane are reachable** (20,821 of
`src/ctx/engine`, 15,172 of `src/ctx/runtime`), much of it through the `ctx`
binary itself. Removing that code is a *product decision* requiring real work,
not the deletion of unreachable code. Only the 10,741 LOC can be removed on
reachability evidence alone.

**Practical consequence.** The deletion program starts with the proven-dead
10,741 LOC, which is safe and mechanical. Everything beyond that is sequenced
behind the milestones that actually replace it, and is re-measured before each
step rather than asserted here.

### 3a. Two further review corrections

- **ADR-011's ~900 LOC extraction estimate is roughly 2x optimistic.** Seeding
  the five named mechanisms plus the `Scenario` model and computing the
  transitive in-file call closure yields 56 definitions, not the handful
  assumed. Budget accordingly.
- **ADR-011's delete list swallowed machinery the PRD depends on.** The
  ~21,100 LOC figure includes ~6,012 LOC of holdout and exposure-ledger code
  (`ctx_ab_holdout*.py`, `ctx_ab_exposure_ledger.py`) — the contamination
  controls the PRD's own risk table names as the mitigation for invalid tasks.
  That code is reclassified **ADAPT**, not DELETE.

The decisive evidence for the proven-dead cluster is not opinion. The auditor built an AST import graph over
all of `src/` (excluding tests) and computed reachability from every real entry
point — `ctx.cli.run`, `ctx.cli.fit`, `ctx.cli.recommend`,
`ctx.mcp_server.server`, the four hook adapters, and `ctx_init` — suppressing
the `__init__.py` re-export hubs that otherwise import everything. The result:

> `managed_query_service` (4,130), `managed_query_store` (2,263),
> `managed_artifact_registry` (1,615), `managed_query` (520), `composition`
> (1,140), `agent_file` (715), `activation_execution` (358) —
> **10,741 LOC reachable only from tests.**

Confirmed independently: grepping `src/` outside `src/tests/` and
`src/ctx/runtime/` for `open_managed_query_service`, `ManagedQueryService`,
`dispatch_release_skill_install`, or `activate_installed_release_skill` returns
**nothing**.

This must be stated plainly, because it includes work completed earlier in this
same session: the signed-consent repair, the `composition.execute_activation`
seam, and the expired-activation retirement slice all live inside that
test-only cluster.

That work was not *wrong*. It was correct engineering against the previous goal
— a durable, consent-gated capability lifecycle for managing a user's installed
tools. The **goal changed**. CTX Fit evaluates configurations in isolated
throwaway workspaces and emits configuration files for a human to apply; it
needs no durable consent ledger, no activation lifecycle, and no crash-recovery
journal. Retaining 46K LOC because it was expensive to build is precisely the
sunk-cost reasoning the mandate forbids.

What survives from the lane is the genuinely reusable intelligence: the bounded
planner, the benefit policy, observation normalization, and workspace identity —
about 2,600 LOC of the 49,834.

## 4. Lane summaries

**Lane A — Public CLI.** 44 scripts → 2. `ctx fit` should become the default
when no subcommand is given; `run`/`resume`/`sessions` move under
`ctx advanced`. Note a real dependency: `test_package_scaffold.py:126-149`
asserts `run` and `resume` appear in top-level `ctx --help`, so that test
changes with the restructure. `README.md:31-33` currently teaches
`ctx-init --graph` and `ctx-scan-repo --recommend` and must be rewritten.

**Lane B — Graph and catalog.** The decisive split is **consumption** (needed
at Fit time to rank candidates, ~4,649 LOC of which 550–800 survives as an
extracted ranking module) versus **production/curation** (building and
maintaining the corpus, ~21,650 LOC — arguably not part of the product at all).
The graph becomes an invisible optimizer answering only "which 2–5 candidates
are worth evaluating?"

**Lane C — Engine/runtime.** See §3.

**Lane D — Benchmark.** The valuable mechanisms are extracted rather than
wrapped: workspace isolation, counterbalancing, usage extraction, verification
execution, and the validity controls move into a new
`src/ctx/core/execution.py` (~450 LOC) plus `src/ctx/fit/experiment.py`
(~450 LOC), replacing ~21,100 LOC of scripts. Net ≈ **−38,000 LOC**. This
reverses the earlier "call the 522KB script as a black box" decision (D8) —
see §6.

**Lane E — Supporting.** The dashboard (14,323 LOC) is archived, not
redesigned. Host installers largely die with the old product; only
`hook_config.py` survives for `--apply`. Telemetry keeps a small product-signal
set. `src/ctx/utils/` is the highest value-per-LOC asset in the lane.

## 5. Deletion is gated

The tree is **largely untracked**, so deletions are not currently recoverable
through Git. No ARCHIVE or DELETE action executes until the working tree is
committed to a branch. This is recorded as a hard precondition on the deletion
program (FIT-900) rather than a preference.

Additional standing rules for every removal: identify dependents, inspect
tests, docs, CLI/API usage and integration points, confirm no reusable logic is
lost, and keep the 19-lane gate green after each step.

## 6. Decision superseded

`ARCHITECTURE_GAP_ANALYSIS.md` **D8** decided that CTX Fit would call
`scripts/ctx_ab_benchmark.py` as an unmodified black-box pairwise primitive.
That was the correct decision under the *evolution* mandate, where not touching
a 522KB module was the dominant concern.

Under the rewrite mandate it is superseded: extracting ~900 LOC of genuinely
valuable mechanism and deleting ~21,100 LOC beats preserving a script that
cannot be imported and that hardcodes its arms in 21 places. The mandate
explicitly prefers deletion over wrapper chains. **D8 is replaced by D12**,
recorded in `DECISIONS.md`.

## 7. What this buys

- A product a new developer understands in ten seconds, with one command.
- A core of roughly 7,000 LOC instead of 156,000.
- Every remaining line traceable to the product promise.
- Substantially less to maintain, test, and explain.

The audit's most useful output is not the deletion list. It is the KEEP list in
§2: the proof that a genuinely small, coherent product can be built from parts
CTX already owns and has already tested.
