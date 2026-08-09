# CTX Fit — Product Requirements Document

Status: draft v1, 2026-08-09. Owner: coordinator. Supersedes the earlier
"evolution, preserve the surface" framing.

## 1. One sentence

> Give it your repository. It tells you how ready the repo is for AI coding,
> tests the most promising AI coding setups, and shows you what actually works
> best.

## 2. The problem

Teams adopting AI coding agents choose their setup — agent, model,
instructions, skills, MCP servers — from generic benchmarks, blog posts, and
opinion. None of that is evidence about *their* repository. Meanwhile CTX
itself has become a system that requires explanation before it can be
appreciated: 44 public commands, a 79,958-node graph, an entity taxonomy, and
a benchmark research framework.

Both problems have the same fix: put the complexity behind a single answer.

## 3. Users

| User | Wants | Success looks like |
| --- | --- | --- |
| Developer adopting AI coding tools | To stop guessing which setup to use | Runs one command, gets a defensible answer and the config to apply it |
| Engineering leader | Evidence before standardizing a stack for a team | A report with verified task outcomes and real cost, not vendor claims |
| Platform/DevEx engineer | To know whether a repo is even ready for agents | A readiness assessment with prioritized, actionable fixes |

## 4. The product experience

Three levels, increasing in cost and commitment. Level 1 must be safe, fast,
read-only, and free.

```text
ctx fit            understand   — readiness + what to improve      (free)
ctx fit --test     verify       — controlled experiments           (costs money, gated)
ctx fit --apply    act          — generate the configuration       (reviewable)
```

Supporting flags: `--explain`, `--budget N`, `--local`, `--dry-run`,
`--rollback <run-id>`, `--pr`, `--verbose`. Diagnostics live in `ctx doctor`.
Anything else must justify its existence.

## 5. Scope

### In scope for V1

- Repository profile with per-property provenance.
- AI agent readiness assessment with dimensions, evidence, blockers, and
  prioritized fixes.
- Bounded candidate generation (2–5) with an explicit selection reason each.
- Controlled execution against a mandatory baseline, isolated per execution.
- Repository-native verification with distinct `attempted / completed /
  verified / failed / inconclusive` states.
- Honest cost accounting where unknown stays unknown.
- Deterministic recommendation policy plus a Pareto view.
- Artifact generation with preview, apply, and rollback.

### Explicitly out of scope for V1

Simultaneous optimization of every dimension; MCP experimental optimization
(observe and recommend only); organization management, multi-tenant SaaS,
billing, SSO, RBAC; marketplaces, leaderboards, social features; a generic
agent platform, workflow builder, or second knowledge graph; CTX Continuous.

## 6. Non-negotiable product rules

These are correctness requirements, not preferences. Each maps to a test.

1. **Verification is repository-native.** An agent's self-report never counts
   as success.
2. **Unknown cost stays unknown.** A configuration must never look cheaper
   because its usage data was missing.
3. **A baseline is mandatory.** No improvement claim without one, run under
   identical conditions.
4. **No fabricated numbers.** Every figure shown is measured, or labelled
   estimate/inference/unknown.
5. **"No improvement found" is a valid, first-class result.** The product never
   invents a recommendation to justify its own name.
6. **Recommendation is deterministic.** An LLM may *explain* a result; it may
   never *decide* it.
7. **Nothing expensive happens by surprise.** Spend requires an explicit budget
   and confirmation, with a dry run available.
8. **Honest scope language.** "Strongest among the configurations tested in this
   experiment", never "the best AI setup for this repository".
9. **Graceful degradation.** Missing credentials, network, tests, or graph
   reduce capability; they never produce a fatal error where partial value was
   possible.
10. **The graph is invisible.** It is an internal optimizer, never the UX.

## 7. Primary metric

**Verified Work per Dollar** — verified engineering outcome over attributable
execution cost. Never collapsed into a single scalar that hides quality, cost,
latency, reliability, retries, or confidence. Where candidates trade off, a
Pareto view (`BEST QUALITY` / `BEST VALUE` / `FASTEST` / `RECOMMENDED`) is
preferred to a fake total ordering.

## 8. Milestones

| M | Name | Outcome | Cost to user |
| --- | --- | --- | --- |
| M0 | Audit | Quarry classified; architecture chosen | none |
| M1 | Product skeleton | `ctx fit` exists and owns the product surface | none |
| M2 | Repository profile | Stack, verification, existing AI config | none |
| M3 | Readiness | Scored dimensions, blockers, prioritized fixes | none |
| M4 | Candidate selection | 2–5 explained candidates | none |
| M5 | Experiment planning | Plan + `--dry-run` + cost estimate | none |
| M6 | Execution | Isolated, reproducible runs with usage | money |
| M7 | Verification | Structured evidence per candidate/task | money |
| M8 | Recommendation | First complete Fit result | money |
| M9 | Apply | Generate config, preview, rollback | none |
| M10 | PR | Branch and PR with evidence | none |

**M1 and M2 are already implemented** (`src/ctx/fit/`, `src/ctx/cli/fit.py`,
11 tests). M3 is the next build.

## 9. Definition of done

Per task: implementation **+** tests **+** independent review **+** evidence.
Code existing is not done.

Per milestone: the four reviews in §11 all pass, including Product, which may
reject a technically complete milestone that does not improve the outcome.

V1 overall: a developer installs, runs `ctx fit` in a real repository, gets
useful readiness output; runs `ctx fit --test --budget 10` and receives an
honest recommendation with verified evidence and real cost; optionally runs
`ctx fit --apply` to generate the configuration.

## 10. Success criteria

Product: time to first useful result; fit completion rate; test-mode adoption;
apply rate; repeat use; whether users trust the recommendation.

Engineering: **public commands removed** (from 44 toward ~1); modules and LOC
removed; duplicate paths eliminated; experiment reproducibility; provider
adapter reliability. A successful rebuild ends with substantially *less* code.

## 11. Review model

Every milestone passes four independent reviews. Reviewers are expected to
reject; review is not ceremonial.

| Role | Asks |
| --- | --- |
| **Product Manager** | Did this make the product simpler, or merely add capability? Can a new user understand it? Does it produce a decision rather than more information? |
| **Architecture (CTO)** | Did we reuse valuable CTX code, avoid duplication, avoid accidental abstraction? Can the architecture be smaller? Are we building a second CTX? |
| **Security** | Credentials, repository boundaries, command execution, workspace isolation, artifact storage, telemetry, cleanup. The engine executes code; treat it accordingly. |
| **QA** | Test quality and coverage, integration across the full path, adversarial cases, regression, flakiness, reproducibility. |

Milestone reports are stored under `docs/ctx-fit/milestones/`.

## 12. Risks

| Risk | Mitigation |
| --- | --- |
| Task derivation is the hardest unsolved problem; invalid tasks produce confident nonsense | `red_failure_contains` — a task that does not start red is not a task. Start with the narrowest defensible source. |
| Combinatorial evaluation cost | Bounded candidate generation is the whole point; hard budget enforced before spend. |
| Building a second CTX alongside the first | Architecture review has explicit authority to reject; prefer deletion over wrapper chains. |
| Cost dishonesty via missing usage | Completeness is a first-class field; incomplete records are never compared as if complete. |
| Optimizing our own benchmark instead of real outcomes | Held-out repositories never used for tuning. |
| Deleting something load-bearing | Evidence required before removal: dependents, tests, docs, CLI/API usage, integration points. |
