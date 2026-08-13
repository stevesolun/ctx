# CTX Fit — Current State Audit (snapshot of 2026-08-09)

> **This is a dated snapshot, not a description of the repository today.** It
> records what CTX contained on 2026-08-09, before CTX Fit was built, and it
> has not been updated since. Many rows below marked **Absent** or `MISSING`
> have shipped: readiness, candidate generation, experiment planning, execution,
> verification states, recommendation, apply, and pull-request preparation all
> live in `src/ctx/fit/` with tests in `src/tests/fit/`. Row 14 in particular —
> "Prepare a GitHub PR" — shipped as *preparation only*: `ctx fit --pr` prints
> a PR body and a suggested branch name. CTX Fit runs no git *write* commands: it reads history to derive tasks, but creates no branch, commits nothing, pushes nothing and never merges, so
> it creates no branch, commits nothing and never merges. Read the code and
> `git log --oneline -- src/ctx/fit` for the current state; read this document
> for the reasoning that shaped it.

Milestone 0 deliverable. This document recorded what CTX already had, what had
to change, and what was genuinely absent, so that CTX Fit was built as an
evolution of CTX rather than a second parallel implementation.

- Audited: 2026-08-09
- Audited tree: the current working tree (broadly untracked and in-flight; see
  the repository-root `STATE.md`)
- Verification baseline at audit time: the full 19-lane PR preflight
  (`python scripts/ci_preflight.py --profile pr`) passes with **8233 tests** and
  **91.9 percent** coverage, and `mypy src` is clean across 548 files.

Every classification below is one of `REUSE_AS_IS`, `MODIFY`, `MISSING`,
`TECH_DEBT`, `DO_NOT_REWRITE`, or `OBSOLETE`.

## 1. Executive summary

CTX already contains a large fraction of what CTX Fit needs, but it is pointed
at a different question.

- **CTX today answers:** *does CTX help at all?* — a two-arm experiment
  (`baseline` versus `ctx-light`) whose only treatment difference is whether
  CTX-prepared context is appended to the prompt.
- **CTX Fit must answer:** *which configuration, out of a small set of
  meaningfully different configurations, is best for this repository?*

The single most important structural finding is that **the experiment harness is
a fixed two-arm A/B rig, not a configuration optimizer**. Arms are a module-level
constant and are validated for exact equality, and the treatment delta is
literally defined as a prompt suffix. Making the arm a first-class
*configuration* object is the central architectural change of this pivot.

The second most important finding is that **the experiment machinery around that
rig is genuinely good and must not be rewritten**: isolation, counterbalanced
execution order, deterministic repository-native verification, regression
verification, contamination controls, and honest token accounting all already
exist and are exercised by the test suite.

The third finding concerns cost, and it is split. **The `ctx run` executor
already reports honest dollar cost**, sourced from LiteLLM
(`src/ctx/adapters/generic/providers/litellm_provider.py:313`,
`cost_usd=float(cost) if cost is not None else None`) and surfaced as a
nullable value with an explicit `attribution="unavailable"` marker
(`src/ctx/cli/run.py:1776-1801`, `:3196-3223`). **The A/B benchmark path has no
dollar cost at all** — it accounts tokens only. So Verified Work per Dollar has
a trustworthy numerator (verification), a trustworthy token foundation, and a
usable dollar source on one of the two execution paths; what is missing is a
single cost record that spans both and preserves unknown-ness.

A fourth finding materially changes the product surface: **an umbrella `ctx`
command with subcommands already exists** (`ctx = "ctx.cli.run:main"`,
`pyproject.toml:89`; subparsers `{run,resume,sessions}` at
`src/ctx/cli/run.py:1857`). `ctx fit` should be a subcommand there rather than
a 45th console script — which also avoids touching the console-script tuple
pinned by `src/tests/test_package_scaffold.py:52`.

## 2. Fit workflow coverage

Mapping the intended CTX Fit workflow onto what exists today.

| # | Fit step | Status | Where it lives today |
| --- | --- | --- | --- |
| 1 | Inspect repository | **Partial** | `src/scan_repo.py` `detect_stack()` |
| 2 | Determine repository characteristics | **Partial** | same; missing verification-command and AI-config inventory |
| 3 | Determine available verification mechanisms | **Absent** | verification exists per benchmark scenario, never derived from a repository |
| 4 | Identify representative tasks | **Absent for arbitrary repos** | `benchmarks/ctx_ab/scenarios.yaml` holds two hand-written tasks for external repos |
| 5 | Establish baseline | **Exists** | `baseline` arm, counterbalanced against treatment |
| 6 | Generate bounded candidate configurations | **Absent as configurations** | bounded 0–5 *capability* selection exists; configuration-level candidates do not |
| 7 | Execute controlled trials | **Exists** | `scripts/ctx_ab_benchmark.py` |
| 8 | Verify each trial | **Exists** | scenario `verify` / `regression_verify` |
| 9 | Collect cost/performance evidence | **Partial** | tokens and latency yes; dollars no |
| 10 | Compare candidates | **Partial** | two-arm comparison only |
| 11 | Select/recommend configuration | **Absent** | no configuration-level recommendation |
| 12 | Explain recommendation | **Absent** | evidence exists; no explanation layer |
| 13 | Generate configuration artifacts | **Absent** | installers exist, but not as an experiment output |
| 14 | Prepare a GitHub PR | **Absent** | no PR-preparation path |

## 3. Reuse — do not rewrite

These are working, tested, and directly relevant. Rebuilding any of them would
be the failure mode the pivot brief explicitly warns about.

| Component | Class | Evidence | CTX Fit use |
| --- | --- | --- | --- |
| Paired A/B experiment runner | `DO_NOT_REWRITE` | `scripts/ctx_ab_benchmark.py` (~522 KB) | Becomes the execution engine for Fit trials once arms are parameterized. |
| Execution-order counterbalancing | `DO_NOT_REWRITE` | all six permutations enumerated, `scripts/ctx_ab_benchmark.py:155-160` | Satisfies the experimental-fairness requirement directly. |
| Deterministic repository-native verification | `DO_NOT_REWRITE` | scenario `verify`, `expected_test_count`, `regression_verify`, `red_failure_contains`, `allowed_changes` in `benchmarks/ctx_ab/scenarios.yaml` | This is exactly the "never trust the agent saying done" rule, already implemented. `red_failure_contains` additionally proves a task starts failing, which is a real validity control. |
| Contamination controls | `REUSE_AS_IS` | `reference_patch` held outside the agent's view; `scripts/ctx_ab_holdout*.py`; `benchmarks/ctx_ab/holdout-protocol-v1.json` | Directly reusable for task-provenance and leakage safeguards. |
| Honest token accounting | `DO_NOT_REWRITE` | `scripts/ctx_ab_benchmark.py:4392-4410` — requires `input_tokens`, `cached_input_tokens`, `output_tokens`; derives `uncached_input_tokens`; validates `cached <= input` | The token foundation for Verified Work per Dollar. It already refuses malformed usage rather than defaulting to zero. |
| Repository stack profiling | `MODIFY` | `src/scan_repo.py:264` `detect_stack()` returning languages, frameworks, testing, monorepo, CI, `CLAUDE.md`, plus PEP 621/Poetry and `package.json` dependency extraction | Extend into the Fit profile. Do not write a new analyzer. |
| Bounded 0–5 capability selection and benefit closure | `REUSE_AS_IS` | `src/ctx/engine/benefit.py`, `src/ctx/runtime/benefit_closure.py`, `eligible_catalog.py` | This is the search-space reducer that keeps candidate generation non-combinatorial — the answer to the combinatorial-cost stop condition. |
| Event-sourced engine, provenance, isolation | `DO_NOT_REWRITE` | `src/ctx/engine/`, `src/ctx/runtime/composition.py`; catalog/planning-environment digests | Supplies reproducibility and provenance for Fit runs. |
| Telemetry with redaction and opt-in export | `REUSE_AS_IS` | `ctx-telemetry-export`, `ctx-telemetry-retention`; `~/.ctx/telemetry/{events,metrics}.jsonl`; `local_redacted` default | Preserves the privacy posture the brief requires. |
| Dashboard / monitor | `MODIFY` | `python -m ctx_monitor serve`, `src/ctx_monitor.py`, `src/kpi_dashboard.py` | Add a Fit view; do not rebuild the dashboard. |
| 19-lane preflight gate | `REUSE_AS_IS` | `scripts/ci_preflight.py --profile pr` | New Fit code must keep this green; it already enforces docs-strict, tracker, packaging, and typing lanes. |

## 4. Must change

| Component | Class | Evidence | Required change |
| --- | --- | --- | --- |
| **Fixed arm model** | `MODIFY` | `TREATMENT_ARMS = ("baseline", "ctx-light", "ctx-full")` and `OFFICIAL_TREATMENT_ARMS = ("baseline", "ctx-light")` at `scripts/ctx_ab_benchmark.py:71-72`; exact-equality validation at `:1120` (`if arms != list(OFFICIAL_TREATMENT_ARMS): raise`) | An arm must become a *configuration* (harness, model, model settings, instructions, skills, agents, MCP servers, tool config, context strategy). This is the central change of the pivot. |
| **Single-dimension treatment delta** | `MODIFY` | `"context_delta": "exact suffix: two LF bytes plus accepted prepared context"`, `scripts/ctx_ab_benchmark.py:5214` | Today the only variable is prompt-context injection. Fit must vary at least model and harness, and must record which dimensions actually differed per comparison. |
| **Codex-only host** | `MODIFY` | `--codex` argument and the Codex runtime contract at `scripts/ctx_ab_benchmark.py:1114-1134` | Fit compares harnesses, so at least one more host must be executable, or harness comparison must be honestly declared out of scope for V1. |
| **Task source** | `MODIFY` | `benchmarks/ctx_ab/scenarios.yaml` contains exactly two scenarios — `click-echo-json` (`pallets/click`) and `requests-json-or` (`psf/requests`) — both hand-written against external repositories | Fit needs tasks derived from the *target* repository, with recorded provenance and generated/historical labelling. |
| **`python -m ctx.cli.recommend` intent surface** | `MODIFY` | `python -m ctx.cli.recommend` takes free-text intent and returns ≤5 capabilities | Fit is repository-driven, not intent-driven; reuse the resolver beneath it rather than the CLI shape. |

## 5. Missing

| Capability | Class | Note |
| --- | --- | --- |
| Unified cost record across both execution paths | `MISSING` | Split source of truth: `ctx run` reports a nullable LiteLLM `cost_usd` with `attribution="unavailable"` (`src/ctx/cli/run.py:1776-1801`), while the A/B benchmark accounts tokens only and has no `usd`/`cost` field at all. CTX Fit needs one versioned cost record spanning both, preserving unknown-ness. |
| Aggregate (campaign-level) budget | `MISSING` | Per-execution budget already exists — `--budget-usd` and `--budget-tokens` (`src/ctx/cli/run.py:1965`, `:1971`). What is absent is a budget across all candidates, tasks, and executions in one Fit run, plus a fail-before-spend pre-flight. |
| Repository-native verification in any packaged CLI | `MISSING` | The only packaged executor's `--evaluator` is an LLM judge (`src/ctx/adapters/generic/evaluator.py`); file/git access is via npx-launched MCP presets (`src/ctx/cli/run.py:893-910`). Deterministic verification exists only inside the benchmark's scenario contract. This is the largest single gap against the "never trust the agent saying done" rule. |
| `--dry-run` on the execution path | `MISSING` | `ctx run` has none, but the repository-native pattern exists in `python -m harness_install --dry-run` (`src/harness_install.py:1108`) and `python -m mcp_add --dry-run`. `ctx fit --dry-run` must therefore gate *before* invoking execution rather than delegating a flag. |
| Repository-derived task generation | `MISSING` | Including provenance labelling and leakage safeguards for historical tasks. |
| Verification-mechanism discovery | `MISSING` | Deriving a repository's own test/lint/typecheck/build commands, as opposed to a scenario declaring them. |
| Configuration-level candidate generation with provenance | `MISSING` | Each candidate must explain why it was selected. |
| Multi-objective comparison / Pareto presentation | `MISSING` | Two-arm significance logic exists; N-way multi-objective selection does not. |
| Recommendation, explanation, and confidence model | `MISSING` | Including the Low/Medium/High confidence scale. |
| Fit run identity and result schema | `MISSING` | A versioned, machine-readable result record. |
| Budget controls in dollars | `MISSING` | `--max-tokens`, `--trials`, `--timeout` exist; a dollar budget and a fail-safe-before-spend path do not. |
| Configuration artifact generation and PR preparation | `MISSING` | Installers exist but are not wired as experiment outputs. |
| `ctx fit` command surface | `MISSING` | Closest existing surfaces are `ctx-scan-repo --recommend` and `python -m ctx.cli.recommend`. |

## 6. Technical debt affecting CTX Fit

- **The tree is broadly untracked and in-flight.** Most of `src/ctx/engine/`,
  `src/ctx/runtime/`, and their tests are new and uncommitted. CTX Fit work will
  land on top of an unstable base until that is resolved.
- **Two module layouts coexist.** Flat `src/*.py` modules (`scan_repo.py`,
  `ctx_monitor.py`, `toolbox.py`) sit beside the `src/ctx/` package, and console
  scripts point at both. `pyproject.toml` documents this as a deliberate,
  incomplete migration. New Fit code should live in the package layout.
- **No product-benefit result has ever been produced.** `STATE.md` states the
  official result remains 0/10 controls and 0/30 pairs and that no benefit claim
  is valid. The A/B rig is proven for transport and accounting, not for outcome.
- **The deterministic bridge pair is deliberately claim-ineligible** — constant
  output provider, no OS-level sibling isolation, both arms fail the evaluator.
  It must never be cited as Fit evidence.
- **Verification depends on a Codex-managed macOS sandbox.** When that binary is
  absent, `verify_workspace` fails; the unit suite substitutes a deterministic
  double. Fit must degrade honestly rather than silently skip verification.
- **Activation recovery is incomplete.** Failed and indeterminate host
  activation still cannot be recorded durably (blocked on a store schema
  migration). Fit should not depend on that path in V1.

## 7. Architectural risks

1. **Arm generalization could fork the benchmark.** Parameterizing arms touches
   a 522 KB module with strict validation. The risk is an accidental second
   experiment implementation. Mitigation: extend the existing runner behind a
   configuration abstraction; do not copy it.
2. **Combinatorial cost.** Configuration space is multiplicative. Mitigation is
   already in the codebase: CTX's bounded selection and benefit closure must be
   the search-space reducer, with a hard dollar budget enforced before spend.
3. **Task validity.** A repository-derived task that is trivial, ambiguous, or
   already solved produces a confident but meaningless verdict. The existing
   `red_failure_contains` control is the right starting primitive.
4. **Cost honesty.** The most dangerous failure mode named in the brief is a
   configuration appearing cheaper only because usage data is missing. The
   token layer already refuses malformed usage; the dollar layer must inherit
   that discipline.
5. **Privacy boundary.** Fit executes real tasks against private repositories
   using external providers. The existing fail-closed and redaction behavior
   must be preserved and explicitly documented for the Fit path.
6. **Honesty of claims.** With two scenarios and single trials, almost no
   statistically meaningful claim is available. Reporting must be scoped to
   "best among the configurations evaluated in this experiment", and
   "no configuration beat baseline" must remain a first-class valid outcome.

## 8. Obsolete

Nothing is currently recommended for removal. The deterministic bridge is not
obsolete — it is valid transport-and-accounting evidence — but it is
permanently ineligible as product-benefit evidence and is labelled as such in
`STATE.md`.

## 9. Audit method

Findings were produced by a five-lane parallel read-only audit (CLI/API,
repository understanding, benchmark infrastructure, cost/telemetry, and
surfaces/tests/docs) together with direct coordinator inspection. Every claim
in sections 3–5 is anchored to a cited `file:line` or to a command executed
against this tree. Where a capability was not observed, it is recorded as
missing rather than assumed.
