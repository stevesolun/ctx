# CTX Fit — Architecture Gap Analysis

Milestone 0 exit document. It converts the [audit](CURRENT_STATE.md) into an
ordered build plan, and records an **extend / refactor / replace** decision for
every gap. Replacement requires written justification; none is proposed.

Read with [`MAP.md`](MAP.md) (open questions and fog) and
[`IMPLEMENTATION_TRACKER.md`](IMPLEMENTATION_TRACKER.md) (execution ledger).

Status: written 2026-08-09.

## 1. The central architectural question, resolved

**Question.** CTX Fit must compare N candidate *configurations*. The existing
experiment runner is a two-arm rig whose arms are hardcoded. How does an
arbitrary configuration become an experiment arm without forking a 522 KB
module that is currently protected by a green 19-lane gate?

**What the code actually does.**

- Arms are module constants: `TREATMENT_ARMS` and `OFFICIAL_TREATMENT_ARMS`
  (`scripts/ctx_ab_benchmark.py:71-72`), validated for exact equality
  (`:1120`).
- The literal pair `("baseline", "ctx-light")` appears **14 times**, and
  `arm == "baseline"` / `arm == "ctx-light"` appears **7 times**.
- The arm ultimately collapses to a boolean:
  `with_ctx = arm == "ctx-light"` (`:8760`), which feeds
  `production_ctx_command(..., with_ctx=with_ctx, ...)`.

So the arm is not a parameter. It is a pervasive binary literal meaning
*"is CTX enabled for this run"*. Generalizing it in place would mean editing
21 coupling sites in the largest module in the repository.

**The seam that already exists.** `write_ctx_fixture(scenario, home)`
(`scripts/ctx_ab_benchmark.py`, called at `:8752`) constructs a **per-run
isolated CTX home** from `scenario.context` — a list of entries of the form
`{type, slug, title, tags, body}` where `type` is one of `skill`, `agent`, or
`mcp-server`. It writes them into `~/.claude/skill-wiki/entities/...`, into
`converted/<slug>/SKILL.md`, into `converted-agents/<slug>.md`, and installs
skills into `.codex/skills/<slug>/SKILL.md` so the harness can actually load
them.

**Decision (D8).** A CTX Fit candidate configuration is expressed as a
`scenario.context` capability set, and each candidate is executed as its own
counterbalanced **baseline-versus-candidate pair** using the existing runner
unmodified. CTX Fit orchestrates *above* the runner rather than generalizing
inside it.

- **Classification: EXTEND.** No change to the arm model, no fork, no rewrite.
- Every existing fairness control is inherited intact: identical repository,
  commit, task bytes, model, tools, approvals, budgets; serial arms;
  counterbalanced execution order; deterministic verification.
- The 21 coupling sites are never touched, so the risk of regressing a green
  gate is near zero.

**The honest cost of this decision.** Candidates are compared *indirectly*,
each against a shared baseline, rather than head-to-head in one pair. That is a
legitimate design, but the report must say so, and cross-candidate differences
carry more uncertainty than each candidate's own baseline delta.

## 2. Which dimensions CTX can evaluate honestly today

The brief asks explicitly which dimensions can be evaluated honestly now, and
tells us not to assume all of them must be optimized in V1.

| Dimension | Evaluable in V1? | Mechanism and limit |
| --- | --- | --- |
| CTX capability set (skills, agents, MCP servers) | **Yes — strongest** | Varied through `scenario.context`; compared *within* a counterbalanced pair, which is the fairest comparison available. |
| Repository instructions / context strategy | **Yes** | Same mechanism: instruction text is a capability body delivered as prepared context. |
| Model | **Yes, weaker** | `--model` (`:11253`) is a *run-level* flag, not an arm-level one. Two models can only be compared across two pinned runs, not inside one pair, so environment pinning carries the fairness burden. |
| Model configuration | **Partial** | `--max-iterations`, `--max-tokens`, `--provider-timeout` exist as run-level flags; reasoning effort is fixed by the Codex runtime contract (`:1114-1134`). |
| Coding harness / agent | **No** | The rig is Codex-only. Making harness a variable is the single largest change available and is deliberately **out of scope for V1**. |
| Tool configuration | **Partial** | `PRODUCTION_CTX_TOOL_NAMES` is gated by `with_ctx`; finer tool control is not exposed. |

**Decision (D9).** V1 compares **CTX capability configurations within a single
harness**, optionally across pinned models. The report must state plainly that
harness was not varied. This satisfies the honesty requirement rather than
implying a comparison the experiment never made.

## 3. Gap-by-gap plan

Each row records the decision type. "Extend" means building on an existing
component without changing its contract.

| # | Fit step | Today | Decision | Plan |
| --- | --- | --- | --- | --- |
| 1–2 | Inspect repository, characteristics | Partial: `scan_repo.detect_stack()` | **Extend** | Wrap the existing stack profile in a normalized, versioned Fit profile. Add only what is missing: verification commands and existing AI configuration. |
| 3 | Verification mechanisms | Absent | **New (small)** | Derive test/lint/typecheck/build commands from the repository; validate cheaply before trusting; degrade honestly when absent. |
| 4 | Representative tasks | Absent for arbitrary repos | **Extend** | Reuse the scenario contract (`verify`, `regression_verify`, `red_failure_contains`, `allowed_changes`, withheld `reference_patch`). The new work is *deriving* those fields from a repository, not inventing a task format. |
| 5 | Baseline | Exists | **Reuse as-is** | The `baseline` arm is already the no-CTX control. |
| 6 | Candidate generation | Absent as configurations | **Extend** | CTX's bounded 0–5 selection and benefit closure produce the capability set; the set is rendered into `scenario.context`. Provenance comes from the planner's committed reasons. |
| 7 | Controlled trials | Exists | **Reuse as-is** | One pair per candidate, orchestrated by CTX Fit. |
| 8 | Verify each trial | Exists | **Reuse as-is** | Scenario verification already refuses "the agent said done". |
| 9 | Cost/performance evidence | Partial | **Extend + new** | Reuse the token normalizer (`:4392-4410`); add a versioned price table and an unknown-preserving cost record. |
| 10 | Compare candidates | Two-arm only | **New** | Aggregate per-candidate baseline deltas; multi-objective / Pareto presentation. |
| 11–12 | Select and explain | Absent | **New** | Recommendation, explanation, and a Low/Medium/High confidence model. |
| 13 | Configuration artifacts | Absent | **Extend** | Reuse existing installers/writers; the new part is emitting them as a reviewable experiment output. |
| 14 | GitHub PR | Absent | **New (last)** | Branch and PR preparation; never auto-merge. |

Nothing in this table is classified **replace**.

## 4. Where CTX Fit lives

**Decision (D10).** New code lives in the package layout under
`src/ctx/fit/`, not in the flat legacy modules. The flat/`src/ctx/` split is
documented in `pyproject.toml` as an incomplete migration; adding to the flat
side would deepen known debt.

**Decision (D10a) — `ctx fit` is a subcommand of the existing `ctx` command,
not a 45th console script.** An umbrella CLI already exists
(`ctx = "ctx.cli.run:main"`, `pyproject.toml:89`) with subparsers
`{run,resume,sessions}` (`src/ctx/cli/run.py:1857`). Registering a `fit`
subparser requires no change to the console-script set that
`src/tests/test_package_scaffold.py:52` pins exactly, so the packaging contract
stays green by construction. It also matches the intended `ctx fit .` naming
without inventing a new convention.

**Decision (D10b) — `ctx fit` reuses `ctx run` as the per-arm executor.**
`ctx run` already provides per-execution budget caps (`--budget-usd`,
`--budget-tokens`, `src/ctx/cli/run.py:1965`, `:1971`), iteration and timeout
bounds, `--json` machine-readable output, and honest nullable cost. CTX Fit
supplies the *aggregate* budget and the repository-native verification that
`ctx run` lacks, rather than building a second agent loop.

Proposed internal boundary, mirroring the engine's existing style of a small
stable surface over a larger implementation:

```text
src/ctx/fit/
  profile.py       normalized repository Fit profile (extends scan_repo)
  verification.py  discovery and validation of repository-native checks
  tasks.py         task representation, provenance, contamination controls
  candidates.py    bounded candidate configurations + selection provenance
  cost.py          versioned price table and unknown-preserving cost records
  experiment.py    orchestration over the existing A/B runner (no fork)
  result.py        versioned, machine-readable Fit result schema
  recommend.py     multi-objective comparison, explanation, confidence
  cli.py           the `ctx fit` surface
```

Per the brief's API requirement, the CLI, dashboard, and any future automation
must call the same internal API; business logic never lives in an interface.

## 5. Spend, budget, and dry run

The repository has never authorized a cost-bearing provider campaign, and the
brief requires both a dry-run mode and budget controls. These reconcile
cleanly:

**Decision (D11).** Spend is authorized *per run by the user*, through an
explicit budget, and never implicitly.

- Milestones 1 and 2 (profile, candidate generation) perform **no provider
  execution at all** and can proceed with no authorization.
- Milestone 3 onward requires an explicit budget. The pre-flight estimate, the
  fail-safe-before-spend check, and `--dry-run` are therefore not optional
  extras — they are the mechanism by which spend is authorized.
- Unknown cost stays unknown. A candidate must never appear cheaper because its
  usage data was missing; this is called out in the brief as a required
  adversarial test.

## 6. Risks carried forward

1. **Indirect candidate comparison** (from D8). Mitigate by reporting each
   candidate's own baseline delta and by never claiming a head-to-head result
   the design did not produce.
2. **Task validity** remains the hardest problem. `red_failure_contains` is the
   right primitive: a task that does not start red is not a valid task.
3. **Cost honesty** — the dollar layer must inherit the token layer's refusal
   to accept malformed input.
4. **Verification isolation** currently depends on a Codex-managed macOS
   sandbox; degradation must be honest, never silent.
5. **Unstable base** — most of the engine and runtime is still untracked and
   in-flight.

## 7. Milestone 0 exit

All three Milestone 0 deliverables now exist: `CURRENT_STATE.md`,
`ARCHITECTURE_GAP_ANALYSIS.md`, and `IMPLEMENTATION_TRACKER.md`. Decisions
D8–D11 close map questions Q1, Q4, Q8, and Q11.

The next milestone is **M1 Fit Profile**, chosen because it delivers visible
user value, requires no provider spend, and is a pure extension of existing
repository intelligence.
