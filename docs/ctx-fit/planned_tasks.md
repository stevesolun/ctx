# CTX Fit — Planned Tasks

Jira-style backlog. Companion to [`PRD.md`](PRD.md) (what and why),
[`CURRENT_STATE.md`](CURRENT_STATE.md) (what exists), and
[`CTX_AUDIT.md`](CTX_AUDIT.md) (keep/adapt/archive/delete).

**Status values:** `NOT_STARTED` · `IN_PROGRESS` · `BLOCKED` · `REVIEW` · `DONE`

**DONE requires:** implementation **+** tests **+** independent review **+**
evidence. Code existing is not DONE.

**Every task carries a Worker and a Reviewer.** For milestone-closing tasks the
reviewer set is Product + Architecture + Security + QA.

## Board summary

| Milestone | Tasks | Done | Status |
| --- | --- | --- | --- |
| M0 Audit | 4 | 2 | IN_PROGRESS |
| M1 Product skeleton | 2 | 2 | DONE |
| M2 Repository profile | 3 | 2 | IN_PROGRESS |
| M3 Readiness | 4 | 0 | NOT_STARTED |
| M4 Candidate selection | 4 | 0 | NOT_STARTED |
| M5 Experiment planning | 3 | 0 | NOT_STARTED |
| M6 Execution | 3 | 0 | BLOCKED (budget authorization) |
| M7 Verification | 2 | 0 | NOT_STARTED |
| M8 Recommendation | 4 | 0 | NOT_STARTED |
| M9 Apply | 3 | 0 | NOT_STARTED |
| M10 PR | 2 | 0 | NOT_STARTED |

---

## M0 — Audit

### FIT-001 · Current-state audit
- **User story:** As the team, we need to know what CTX actually contains so we build on evidence rather than assumption.
- **Priority:** P0 · **Depends on:** — · **Worker:** coordinator · **Reviewer:** architecture
- **Status:** `DONE`
- **Tests:** n/a · **Evidence:** `CURRENT_STATE.md`, every claim cited to `file:line`; two of the coordinator's own claims corrected by the five-lane audit.

### FIT-002 · Architecture gap analysis
- **User story:** As the team, we need each Fit workflow step mapped to exists/partial/absent with an extend-refactor-replace decision.
- **Priority:** P0 · **Depends on:** FIT-001 · **Worker:** coordinator · **Reviewer:** architecture
- **Status:** `DONE`
- **Evidence:** `ARCHITECTURE_GAP_ANALYSIS.md`; resolved the arm-model question (D8) and V1 scope (D9).

### FIT-003 · Quarry classification (`CTX_AUDIT.md`)
- **User story:** As the team, we need every subsystem classified KEEP/ADAPT/ARCHIVE/DELETE so we can delete fearlessly with evidence.
- **Priority:** P0 · **Depends on:** FIT-001 · **Worker:** 5-lane audit workflow · **Reviewer:** architecture + product
- **Status:** `DONE`
- **Tests:** n/a
- **Evidence:** `CTX_AUDIT.md` — 130 components classified (14 KEEP, 38 ADAPT, 36 ARCHIVE, 42 DELETE); public surface 44 → 2. Independent architecture review reproduced the 10,741-LOC dead-code finding from a broader entry-point seed and corrected the lane headline; corrections applied in §3 and §3a.

### FIT-004 · Decision log
- **User story:** As the team, we need the target architecture and its rationale recorded once so decisions are not relitigated.
- **Priority:** P0 · **Depends on:** FIT-003 · **Worker:** coordinator · **Reviewer:** architecture
- **Status:** `DONE`
- **Evidence:** `DECISIONS.md` ADR-001…013, including ADR-012 superseding D8 and the superseded-notice added to `MAP.md`.
- **Notes:** A separate `ARCHITECTURE_PROPOSAL.md` was deliberately **not** created — it would have been a third overlapping document. The target architecture lives in `CTX_AUDIT.md` §2 (the KEEP core) plus `DECISIONS.md`. The canonical domain model is specified in `PRD.md` and tracked as FIT-040/FIT-080.

### FIT-005 · M0 review-gate remediation
- **User story:** As the team, the milestone gate must actually be able to reject, and its findings must be fixed rather than noted.
- **Priority:** P0 · **Depends on:** FIT-003 · **Worker:** coordinator · **Reviewer:** product + architecture
- **Status:** `DONE`
- **Tests:** `src/tests/fit/test_fit_profile.py` — 14 passing, including two new regressions for the defects found.
- **Evidence:** Both reviewers returned `accept-with-conditions`; every blocking objection was fixed, not argued: the evaluability overclaim, non-deterministic JSON, the duplicate tracker, the contradictory execution decision, the overstated audit headline, and front-door discoverability.

---

## M1 — Product skeleton · `DONE`

### FIT-010 · `ctx fit` command surface
- **User story:** As a developer, I run one obvious command and get an answer.
- **Priority:** P0 · **Worker:** coordinator · **Reviewer:** product
- **Status:** `DONE`
- **Tests:** `src/tests/fit/test_fit_profile.py` (CLI cases) · **Evidence:** `ctx --help` shows `{fit,run,resume,sessions}`; registered as a subcommand so the pinned console-script tuple is untouched; 168 CLI/packaging tests pass.

### FIT-011 · Safe-by-default behavior
- **User story:** As a developer, running `ctx fit` must never cost money or change files.
- **Priority:** P0 · **Worker:** coordinator · **Reviewer:** security
- **Status:** `DONE`
- **Tests:** dry-run test asserts no `$` figure is invented and states nothing was spent.

---

## M2 — Repository profile

### FIT-020 · Verification discovery
- **Status:** `DONE` · **Worker:** coordinator · **Reviewer:** QA
- **Evidence:** `src/ctx/fit/verification.py`; discovers test/lint/typecheck/build for Python, Node (lockfile-aware runner), Rust, Go, Make, each with source, confidence, and evidence; adversarial tests for malformed manifests and empty repositories.

### FIT-021 · Fit profile + existing AI config detection
- **Status:** `DONE` · **Worker:** coordinator · **Reviewer:** QA
- **Evidence:** `src/ctx/fit/profile.py`; versioned `ctx.fit.profile-v1`; detects `AGENTS.md`, `CLAUDE.md`, tool config, capability dirs; correctly found 26 skills in this repository.

### FIT-022 · Per-property provenance in the profile
- **User story:** As a user, I want to know *why* CTX believes something about my repository.
- **Priority:** P1 · **Depends on:** FIT-021 · **Worker:** TBD · **Reviewer:** QA
- **Status:** `NOT_STARTED`
- **Notes:** Verification commands already carry evidence; the stack half still relies on `scan_repo`'s shape. Normalize both.

---

## M3 — Readiness *(next build)*

### FIT-030 · Readiness dimension model
- **User story:** As an engineering leader, I want to know how suitable my repo is for AI agents and exactly what to fix.
- **Priority:** P0 · **Depends on:** FIT-021 · **Worker:** TBD · **Reviewer:** product + QA
- **Status:** `NOT_STARTED`
- **Notes:** Dimensions: verification, instructions, environment, CI, tool safety, architecture. Deterministic computation, versioned methodology, raw dimension scores always visible. An LLM must never invent the score.

### FIT-031 · Blockers and prioritized fixes
- **Priority:** P0 · **Depends on:** FIT-030 · **Reviewer:** product
- **Status:** `NOT_STARTED`
- **Notes:** Every recommendation must answer "how does this help an agent produce safer or more verifiable work?" If it cannot, the metric is deleted (anti-score-gaming).

### FIT-032 · Readiness rendering in `ctx fit`
- **Priority:** P0 · **Depends on:** FIT-030, FIT-031 · **Reviewer:** product
- **Status:** `NOT_STARTED`

### FIT-033 · Readiness adversarial tests
- **Priority:** P0 · **Reviewer:** QA
- **Status:** `NOT_STARTED`
- **Notes:** No tests; no instructions; monorepo; unknown language; huge repo; contradictory instructions.

---

## M4 — Candidate selection

### FIT-040 · `CandidateConfiguration` domain model
- **Priority:** P0 · **Depends on:** FIT-004 · **Reviewer:** architecture
- **Status:** `NOT_STARTED`
- **Notes:** id, provider, agent, model, reasoning level, instructions, skills, MCPs, tools, context policy, environment, configuration hash, selection reason. No important difference hidden in free-form prompt text.

### FIT-041 · Staged candidate search
- **Priority:** P0 · **Depends on:** FIT-040 · **Reviewer:** architecture
- **Status:** `NOT_STARTED`
- **Notes:** understand → compatibility filter → relevance filter (graph earns its place here) → cost filter → diversity selection. Never brute force.

### FIT-042 · Diversity policy
- **Priority:** P1 · **Depends on:** FIT-041 · **Reviewer:** product
- **Status:** `NOT_STARTED`
- **Notes:** Avoid three near-identical candidates. Target: best predicted quality, best predicted value, fast/simple, baseline, one exploratory.

### FIT-043 · Selection provenance
- **Priority:** P0 · **Depends on:** FIT-041 · **Reviewer:** product
- **Status:** `NOT_STARTED`
- **Notes:** Every candidate answers "why did CTX believe this was worth testing?"

---

## M5 — Experiment planning

### FIT-050 · Task derivation from the repository *(hardest task in the plan)*
- **Priority:** P0 · **Depends on:** FIT-021 · **Reviewer:** QA + architecture
- **Status:** `NOT_STARTED`
- **Notes:** Provenance mandatory; generated tasks labelled; historical solutions must not leak. `red_failure_contains` is the validity gate — a task that does not start red is not a task. Start with the narrowest defensible source and expand only with evidence.

### FIT-051 · Experiment plan object
- **Priority:** P0 · **Depends on:** FIT-040, FIT-050 · **Reviewer:** architecture
- **Status:** `NOT_STARTED`

### FIT-052 · `--dry-run` with cost estimate and budget gate
- **Priority:** P0 · **Depends on:** FIT-051 · **Reviewer:** security + product
- **Status:** `NOT_STARTED`
- **Notes:** Must show candidates, tasks, max executions, estimated cost, providers involved, files that may change. Fails safely when a budget would be exceeded.

---

## M6 — Execution · `BLOCKED`

### FIT-060 · Execution model and isolation
- **Priority:** P0 · **Depends on:** FIT-051 · **Reviewer:** security
- **Status:** `BLOCKED` — requires an authorized budget before any provider spend.
- **Notes:** execution_id, fit_run_id, candidate_id, task_id, commit, environment, timings, status, provider, model, tooling, cost, artifacts. Executions must not contaminate each other.

### FIT-061 · Cost record with completeness
- **Priority:** P0 · **Reviewer:** QA
- **Status:** `NOT_STARTED` *(can be built and unit-tested without spend)*
- **Notes:** States `priced_exact` / `priced_partial` / `unpriced` / `unpriceable`; total is `None` unless exact; folding takes the worst state. Adversarial test: a configuration must never appear cheaper because usage was missing.

### FIT-062 · Repetition and nondeterminism
- **Priority:** P1 · **Depends on:** FIT-060 · **Reviewer:** QA
- **Status:** `NOT_STARTED`
- **Notes:** Reliability as "2/3 verified"; retries always visible; never silently retry until success.

---

## M7 — Verification

### FIT-070 · Verification execution and hierarchy
- **Priority:** P0 · **Depends on:** FIT-020, FIT-060 · **Reviewer:** security + QA
- **Status:** `NOT_STARTED`
- **Notes:** deterministic tests > build/type/lint > acceptance scripts > structured evaluator > reviewer agent > self-report (never sufficient alone).

### FIT-071 · Flaky and infrastructure-failure handling
- **Priority:** P0 · **Depends on:** FIT-070 · **Reviewer:** QA
- **Status:** `NOT_STARTED`
- **Notes:** Distinguish `verification_pass` / `verification_fail` / `infrastructure_failure` / `flaky` / `inconclusive`. Never blame a candidate for known repository nondeterminism.

---

## M8 — Recommendation

### FIT-080 · `FitResult` canonical object
- **Priority:** P0 · **Reviewer:** architecture
- **Status:** `NOT_STARTED`
- **Notes:** Versioned and machine-readable. Every interface derives from it; no interface re-implements business logic.

### FIT-081 · Deterministic recommendation policy
- **Priority:** P0 · **Depends on:** FIT-080 · **Reviewer:** architecture + product
- **Status:** `NOT_STARTED`
- **Notes:** Threshold exclusion → critical-failure exclusion → Pareto frontier → documented default choice. An LLM may explain, never decide.

### FIT-082 · Confidence model
- **Priority:** P0 · **Depends on:** FIT-080 · **Reviewer:** QA
- **Status:** `NOT_STARTED`
- **Notes:** LOW/MEDIUM/HIGH from task count, diversity, repetitions, verification strength, cost completeness, consistency. No false precision.

### FIT-083 · No-improvement result
- **Priority:** P0 · **Depends on:** FIT-081 · **Reviewer:** product
- **Status:** `NOT_STARTED`
- **Notes:** "Keep current configuration" is a first-class success. Test that the product can return it.

---

## M9 — Apply

### FIT-090 · Artifact generation · **FIT-091** · Preview and apply · **FIT-092** · Rollback
- **Priority:** P0/P0/P1 · **Depends on:** FIT-080 · **Reviewer:** security + product
- **Status:** `NOT_STARTED`
- **Notes:** Show every change before applying; never silently modify; every artifact traceable to the recommendation; `--rollback <run-id>` or explicit Git instructions.

---

## M10 — PR *(terminal deliverable, not optional)*

### FIT-100 · Branch and PR preparation
- **Priority:** **P0** · **Depends on:** FIT-090 · **Reviewer:** security + product
- **Status:** `NOT_STARTED`
- **Notes:** ADR-014 makes the PR part of the product promise, not polish: "…then opens a PR containing the winning configuration." Priority raised from P1. The PR contains evidence, not marketing, and is never auto-merged.

### FIT-101 · GitHub-first entry
- **User story:** As a user, I connect a GitHub repository rather than only running CTX inside a local checkout.
- **Priority:** P1 · **Depends on:** FIT-100 · **Reviewer:** security + product
- **Status:** `NOT_STARTED`
- **Notes:** The promise opens with "Connect your GitHub repository." Local-first stays fully supported (ADR: local-first is not crippled); this adds the hosted-repo entry path. Credential handling is a security-review gate.

---

## Objective-function tasks *(from ADR-014)*

### FIT-110 · Reliability as a selection constraint
- **User story:** As a user, I need "works" to mean "worked every time we tried", not "worked once".
- **Priority:** P0 · **Depends on:** FIT-062 · **Reviewer:** QA
- **Status:** `NOT_STARTED`
- **Notes:** Repeated trials move from "when budget permits" to required for any recommendation. Defines and documents the reliability floor; a candidate below it is excluded before cost is even considered.

### FIT-111 · Lexicographic winner selection
- **User story:** As a user, I want to know exactly why one configuration won, in one sentence.
- **Priority:** P0 · **Depends on:** FIT-110, FIT-061 · **Reviewer:** architecture + product
- **Status:** `NOT_STARTED`
- **Notes:** Replaces the earlier Pareto-selection sketch (see FIT-081). Filter by reliability → minimize attributable cost → tie-break toward the simpler configuration. Candidates with incomplete cost are reported unranked rather than winning by having less data. Pareto survives as presentation only.

---

## Cross-cutting

### FIT-900 · Deletion program
- **Priority:** P1 · **Depends on:** FIT-003 · **Reviewer:** architecture + QA
- **Status:** `DONE (surface)` — command surface and console scripts landed; LOC deletion still open
- **Notes:** Execute the ARCHIVE/DELETE classifications with evidence. Success is measured in public commands and LOC removed while the gate stays green.

**Delivered.** The advertised command surface is now `{fit, doctor, advanced}`. `run`, `resume`, and `sessions` still work and are additionally reachable as `ctx advanced <command>`; they are hidden from help rather than removed, so existing scripts do not break. Bare `ctx` runs the product. Console scripts went from **45 to 7**. `src/tests/fit/test_cli_surface.py` pins all of this so the surface cannot quietly regrow.

**Not delivered: the target of 2 console scripts.** Five of the remaining seven are load-bearing and cannot be dropped by editing `pyproject.toml` alone:

| Script | Why it stays |
| --- | --- |
| `ctx` | The product. |
| `ctx-init` | First-run install path. |
| `ctx-mcp-server` | MCP hosts spawn it by name; renaming it breaks configured clients. |
| `ctx-scan-repo` | Invoked by CI lanes. |
| `ctx-source-registry` | Pinned by `src/tests/test_threat_model_docs.py:190` as a threat-model control. |
| `ctx-telemetry-export` | Invoked by CI lanes. |
| `ctx-telemetry-retention` | Invoked by CI lanes. |

Reaching 2 requires migrating each call site to `python -m`, updating the clean-host contract, and amending the threat-model test — a separate change with its own review, not a line in this one.

**Also fixed here.** `test_concurrent_cross_host_manage_prompts_apply_one_workspace_install` was flaking under full-suite load. Root cause confirmed by reproduction, not inference: two hosts contend for one lock, and the production default `lock_timeout_seconds=2.0` fails closed. Forcing `0.01` reproduces the exact gate signature (`status='failed'`, no permit). The production default is correct — a prompt hook must not stall — so the test now passes `lock_timeout_seconds=10.0`, matching the existing convention in `test_query_hook_delivery.py`.

### FIT-901 · Dogfood CTX Fit on CTX
- **Priority:** P1 · **Reviewer:** product
- **Status:** `NOT_STARTED`
- **Notes:** Mandatory, but must not be the only benchmark. Maintain development / calibration / held-out repository sets to prevent self-optimization.

### FIT-902 · README and docs rewrite
- **Priority:** P1 · **Depends on:** FIT-032 · **Reviewer:** product
- **Status:** `NOT_STARTED`
- **Notes:** Sell the outcome before the machinery; must pass the ten-second test.
