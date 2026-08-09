# CTX Fit — Implementation Tracker

Canonical task ledger for the CTX Fit product layer. Read this together with
[`CURRENT_STATE.md`](CURRENT_STATE.md) (what exists today) and
[`ARCHITECTURE_GAP_ANALYSIS.md`](ARCHITECTURE_GAP_ANALYSIS.md) (what must
change). The repository-wide engineering checkpoint remains `STATE.md` at the
repository root; this file does not replace it.

## Rules

- Allowed states: `NOT_STARTED`, `IN_PROGRESS`, `BLOCKED`, `REVIEW`, `DONE`.
- A task is **not** `DONE` because code exists. `DONE` requires
  **implementation + tests + independent review + evidence**.
- Every substantial task has a **worker** role and a separate **reviewer**
  role. The reviewer is expected to reject weak work; review is not ceremonial.
- Evidence means an exact command and its result, or a cited artifact — not a
  claim.
- Order of preference for every gap: **extend → refactor → replace**.
  Replacement requires written justification in the Notes column.

## Milestone map

Milestones follow the agreed progression. Each milestone ends with three
independent reviews — Product Manager, CTO/Architecture, and CEO/Commercial —
recorded under [`milestones/`](milestones/).

| Milestone | Outcome | State |
| --- | --- | --- |
| M0 Audit | Understand existing CTX; no product implementation | IN_PROGRESS |
| M1 Fit Profile | `ctx fit .` understands a repository and emits a structured profile | NOT_STARTED |
| M2 Candidate Generation | Bounded, explained candidate configuration set | NOT_STARTED |
| M3 Controlled Execution | Isolated, reproducible candidate execution with cost/latency evidence | NOT_STARTED |
| M4 Verification | Repository-native verification with attempted/completed/verified/failed/inconclusive states | NOT_STARTED |
| M5 Recommendation | First complete CTX Fit report; the product becomes usable | NOT_STARTED |
| M6 Apply | Reviewable generation of repository configuration changes | NOT_STARTED |
| M7 GitHub PR | Prepared branch and PR as the final artifact | NOT_STARTED |

## Tasks

### Milestone 0 — Audit

| Field | FIT-000 |
| --- | --- |
| **Description** | Audit the existing CTX codebase across CLI/API, repository understanding, benchmark/experiment infrastructure, cost/telemetry, and dashboard/tests/docs. |
| **Why** | The product spec gates all implementation on this audit. CTX Fit is an evolution, not a rewrite, so reusable capability must be identified before anything is built. |
| **Dependencies** | none |
| **Owner role** | Coordinator + five parallel read-only auditors |
| **Reviewer role** | Coordinator synthesis, cross-checked against independently verified findings |
| **Status** | IN_PROGRESS |
| **Tests** | n/a (read-only audit) |
| **Evidence** | Five-lane audit workflow plus coordinator scans; findings cited by `file:line` in `CURRENT_STATE.md` |
| **Notes** | Deliverables: `CURRENT_STATE.md`, `ARCHITECTURE_GAP_ANALYSIS.md`, `IMPLEMENTATION_TRACKER.md`. |

| Field | FIT-001 |
| --- | --- |
| **Description** | Write `docs/ctx-fit/CURRENT_STATE.md` classifying every relevant component as REUSE_AS_IS / MODIFY / MISSING / TECH_DEBT / DO_NOT_REWRITE / OBSOLETE. |
| **Why** | Prevents rewriting working infrastructure and prevents assuming capability that does not exist. |
| **Dependencies** | FIT-000 |
| **Owner role** | Coordinator |
| **Reviewer role** | Independent reviewer to challenge any REUSE_AS_IS or MISSING claim |
| **Status** | IN_PROGRESS |
| **Tests** | n/a |
| **Evidence** | pending |
| **Notes** | Must state honestly what has never been run. |

| Field | FIT-002 |
| --- | --- |
| **Description** | Write `docs/ctx-fit/ARCHITECTURE_GAP_ANALYSIS.md` mapping all 14 Fit workflow steps to exists / partial / absent with an extend-refactor-replace decision each. |
| **Why** | Turns the audit into an ordered build plan and forces justification before any replacement. |
| **Dependencies** | FIT-001 |
| **Owner role** | Coordinator |
| **Reviewer role** | CTO/architecture review before M1 starts |
| **Status** | NOT_STARTED |
| **Tests** | n/a |
| **Evidence** | pending |
| **Notes** | Must resolve the central question of how arbitrary candidate configurations become experiment arms. |

### Milestone 1 — Fit Profile (planned, not started)

| Field | FIT-010 |
| --- | --- |
| **Description** | Normalized repository Fit profile: languages, frameworks, package managers, size, monorepo layout, tests, linters, type checkers, build commands, CI, existing AI configuration (`AGENTS.md`, `CLAUDE.md`, MCP config, installed skills), and the repository's available verification mechanisms. |
| **Why** | Every later step — task selection, candidate generation, verification — depends on a trustworthy profile. It is also the first thing the user sees. |
| **Dependencies** | FIT-002 |
| **Owner role** | worker TBD |
| **Reviewer role** | reviewer TBD |
| **Status** | NOT_STARTED |
| **Tests** | unit: detection per characteristic; adversarial: empty repo, no tests, dirty tree, monorepo, unknown language |
| **Evidence** | pending |
| **Notes** | Extend the existing `scan_repo` stack profile rather than writing a new analyzer. The Fit-specific additions are the verification-mechanism inventory and the existing-AI-configuration inventory. |

| Field | FIT-011 |
| --- | --- |
| **Description** | `ctx fit .` command surface with `--dry-run` and a profile-only mode that runs no model calls. |
| **Why** | Delivers the product's front door and guarantees the spec's no-surprise-cost rule from the very first milestone. |
| **Dependencies** | FIT-010 |
| **Owner role** | worker TBD |
| **Reviewer role** | reviewer TBD |
| **Status** | NOT_STARTED |
| **Tests** | unit + CLI contract tests; must not regress existing console scripts |
| **Evidence** | pending |
| **Notes** | Naming to be confirmed against repository console-script conventions. |

### Milestones 2–7

Tasks are deliberately not enumerated yet. They will be written from
`ARCHITECTURE_GAP_ANALYSIS.md` so that each one traces to a real gap rather
than to a guess. Adding speculative tasks now would violate the
do-not-overbuild rule.

## Stop conditions

Work halts and the architecture is reconsidered — with the reason documented
here — if any of the following becomes true:

- implementation would duplicate a major existing CTX subsystem;
- benchmark cost becomes combinatorial;
- results cannot be verified meaningfully;
- recommendation scoring becomes arbitrary;
- missing usage data is being treated as zero cost;
- candidate generation cannot explain why a configuration was selected;
- the system starts optimizing its own benchmark instead of real outcomes;
- existing CTX behavior begins regressing;
- the work drifts into a generic orchestration framework instead of CTX Fit.
