# ctx Agent-Reviewer Workbench

This plan adapts the Claude Science actor-critic pattern to ctx review work.
The source pattern is a coordinator agent that delegates to specialists, while
separate reviewer agents check evidence, traceability, calculations, and
artifact correctness:
<https://www.anthropic.com/news/claude-science-ai-workbench>.

This is an internal execution plan. It is intentionally kept under
`docs/plans/`, which `mkdocs.yml` excludes from public docs.

## Goal

Review ctx with paired agents that behave like production review teams:

1. An actor agent discovers, tests, or proposes the review artifact.
2. A critic agent independently checks the artifact against code, tests, docs,
   and user-visible behavior.
3. The CTO orchestrator merges only evidence-backed findings into the canonical
   trackers.
4. Fixes land as small commits. No rushed PRs. No long no-mistakes runs until
   the fix batch is ready.

## Non-Goals

- Do not create a PR during this review setup phase.
- Do not run no-mistakes as the default review loop.
- Do not replace `qa/bug_smoke_status.csv` or `qa/feature_status.csv`.
- Do not let specialist notes become new canonical trackers.
- Do not accept "tests passed" as evidence unless the behavior under review is
  directly demonstrated.

## CTX Review Workbench Team

### CTO Orchestrator

Owns scope, task graph, conflicts, and merge criteria.

Inputs:

- `qa/feature_status.csv`.
- `qa/bug_smoke_status.csv`.
- CI/no-mistakes evidence.
- Repo tree.

Output:

- One canonical review board with owner, status, evidence path, and blocker
  state, derived from the canonical CSVs. Do not create a third tracker file.

### Feature Mapper + Feature Reviewer

Mapper discovers every product capability: CLI, MCP server, dashboard,
telemetry, graph/wiki, skills, agents, harnesses, and Loopflow adapter.

Reviewer checks every discovered feature has a user story, expected behavior,
test evidence, and tracker row.

### Runtime/API/MCP Agent + Contract Reviewer

Agent reviews `src/ctx/api.py`, `src/ctx/mcp_server`, generic adapters, tool
router, and lifecycle flows.

Reviewer checks API schemas, backwards compatibility, permissions, error
behavior, and trace propagation.

### Telemetry/Enterprise Agent + Privacy Reviewer

Agent reviews OTel-style traces/metrics/logs, token usage KPIs, exporters,
retention, and local defaults.

Reviewer checks redaction, hashed error handling, salt boundaries, exporter
config safety, and enterprise runbook gaps.

### Dashboard/UX Agent + UX Critic

Agent walks dashboard pages and user flows.

Critic checks broken links, misleading states, graph/LLM-wiki/dashboard health,
visual regressions, and empty/error states.

### Graph/Wiki/Recommendation Agent + Evidence Reviewer

Agent reviews graph artifacts, wiki packs, skill/agent/MCP recommendations, and
subgroup selection behavior.

Reviewer checks recommendations are explainable, reproducible,
permission-aware, and backed by tests.

### QA/Test Gate Agent + Gate Critic

Agent runs local-fast, focused pytest, CI classifier, no-test policy,
no-mistakes, and PR checks.

Critic checks bottlenecks, flaky tests, overbroad gates, missing fixtures, and
whether evidence is real or just "tests passed."

### Security/Supply Chain Agent + Red-Team Reviewer

Agent scans install scripts, sync/HF paths, tokens/secrets, LFS/artifacts, and
external tool ingestion.

Reviewer tries adversarial cases: malicious skill/MCP metadata, path traversal,
unsafe exporters, and poisoned DSL/tool manifests.

### Docs/Runbook Agent + Docs Reviewer

Agent validates docs against code behavior.

Reviewer checks every doc claim has command/test/source evidence and flags stale
prose or unsupported marketing language.

### Loopflow/DSL Integration Agent + DSL Author Reviewer

Agent designs the Loopflow adapter surface: permissions, harnesses, skills,
MCPs, agents, recommendations, and agent loops.

Reviewer evaluates from the Loopflow owner's view: "Would I trust ctx as a
recommendation/runtime layer without losing control of my DSL?"

## Operating Loop

Each pair produces:

- finding
- evidence
- repro
- risk
- fix recommendation
- reviewer verdict

Then CTO merges them into:

- canonical tracker updates
- prioritized fix batches
- local-fast/no-mistakes validation plan
- PR-ready changelog

## Operating Principles

- Keep one coordinator. The CTO owns scope, conflicts, ordering, and commit
  boundaries.
- Keep agents paired. Every actor output gets an independent critic pass before
  it becomes a finding.
- Keep evidence portable. Findings must include file paths, commands, observed
  output, screenshots when visual, or generated artifacts when behavior is not
  visible in logs.
- Keep trackers canonical. Feature coverage updates go to
  `qa/feature_status.csv`; bug, smoke, UX, and garbage-code findings go to
  `qa/bug_smoke_status.csv`.
- Keep fixes separate. Review findings first, fix batches second.
- Keep validation cheap until ready. Use focused tests and `local-fast`; reserve
  no-mistakes and PR CI for the final integrated batch.

## Canonical Tracker Hygiene

Before any agent or CTO step writes tracker rows, validate the canonical CSVs:

- Parse `qa/feature_status.csv` as CSV and require unique non-empty
  `feature_id` values.
- Parse `qa/bug_smoke_status.csv` as CSV and require unique non-empty
  `finding_id` values.
- Check proposed new row keys against both files before writing; collisions
  must become `needs-cto-review`, not silent overwrites.
- Check stale status/evidence markers before writing: historical phrases such
  as `pending no-mistakes`, bare `TODO`, obsolete blocker text, or retest claims
  without matching `last_verified_at` and evidence.
- If a stale row is found, preserve the old text in review notes, add current
  evidence, and update status only after the critic verifies the behavior.
- Treat the "canonical review board" as a derived view over these CSVs with
  owner, status, evidence path, and blocker state. Do not create a separate
  board file unless the user explicitly approves it.

## CTO Orchestrator

The CTO orchestrator owns the shared review board and makes final calls.

Inputs:

- Current branch and diff.
- `qa/feature_status.csv`.
- `qa/bug_smoke_status.csv`.
- `docs/qa/feature-user-story-status.csv`.
- `docs/qa/dashboard-user-story-status.csv`.
- Local-fast timing evidence in `.gate/local-fast.json` when available.
- CI/no-mistakes evidence only when intentionally run.

Outputs:

- Updated canonical trackers.
- A prioritized fix queue.
- Small commit boundaries.
- A final "ready for big PR" report.

Decision rules:

- If actor and critic disagree, the finding stays `needs-cto-review`.
- If the critic cannot reproduce, the finding stays `needs-evidence`.
- If a behavior needs product judgment, the finding is `human-decision`.
- If a fix would touch more than 5 files, split it into phases before coding.

## Evidence Contract

Every pair returns this shape:

```yaml
pair: string
surface: string
status: pass | finding | blocked | human-decision
finding:
  summary: string
  category: feature-gap | bug | ux | security | docs | gate | architecture | none
evidence:
  files: []
  commands: []
  artifacts: []
  observed_output: []
risk:
  severity: critical | high | medium | low | info
  blast_radius: string
repro:
  steps: []
fix_recommendation:
  scope: string
  suggested_files: []
  validation: []
reviewer_verdict:
  status: accepted | needs-evidence | duplicate | human-decision | invalid
  notes: string
tracker_update:
  file: qa/feature_status.csv | qa/bug_smoke_status.csv | none
  row_key: string
```

The CTO may reject any result without concrete evidence.

## Detailed Pair Contracts

The canonical team roster is `CTX Review Workbench Team` above. This section
adds execution contracts only, so agents do not maintain a second divergent
team list.

| Canonical pair | First local review focus | Primary tracker target |
| --- | --- | --- |
| QA/Test Gate Agent + Gate Critic | Run `scripts/no_mistakes_run.sh fast`, focused pytest, CI classifier checks, no-test policy checks, and local gate timing summaries. Identify slow, flaky, redundant, or missing local gates. Separate local CPU bottlenecks from GitHub-hosted runner delays and no-mistakes agent overhead. | `qa/bug_smoke_status.csv` for gate findings |
| Feature Mapper + Feature Reviewer | Discover user-visible features, map each to current tests/docs, and reject vague or duplicate rows. | `qa/feature_status.csv`; missing-story findings in `qa/bug_smoke_status.csv` |
| Runtime/API/MCP Agent + Contract Reviewer | Exercise API and MCP boundaries from a user perspective; check schema stability, error payloads, backwards compatibility, permissions, and trace propagation. | `qa/bug_smoke_status.csv` for contract gaps |
| Telemetry/Enterprise Agent + Privacy Reviewer | Verify traces, metrics, logs, events, token usage KPIs, exporters, retention, local defaults, redaction, hashed errors, salt boundaries, and exporter opt-in safety. | `qa/bug_smoke_status.csv` for telemetry/privacy gaps |
| Dashboard/UX Agent + UX Critic | Walk dashboard pages and capture browser evidence for visual or interaction findings. Reject visual UX findings without screenshot or rendered artifact evidence. | `qa/bug_smoke_status.csv` for UX findings |
| Graph/Wiki/Recommendation Agent + Evidence Reviewer | Verify graph artifacts, wiki packs, recommendations, subgroup selection, explainability, permission awareness, and LFS hydration expectations. | `qa/bug_smoke_status.csv` for graph/recommendation findings |
| Security/Supply Chain Agent + Red-Team Reviewer | Review install scripts, Hugging Face sync, Git LFS artifacts, external imports, MCP ingestion, exporters, generated artifacts, and adversarial skill/MCP/DSL inputs. | `qa/bug_smoke_status.csv` for security findings |
| Docs/Runbook Agent + Docs Reviewer | Compare docs to code behavior and reject unsupported claims, stale prose, or instructions without command/test/source evidence. | `qa/bug_smoke_status.csv` for docs drift |
| Loopflow/DSL Integration Agent + DSL Author Reviewer | Check permissions, harnesses, skills, MCPs, agents, recommendations, and agent loops from the DSL author's perspective. | `qa/bug_smoke_status.csv` for adapter UX/permission findings |

## First Wave

Run these pairs first because local-fast is the review bottleneck reducer and
tracker hygiene must be clean before broader feature mapping:

1. QA/Test Gate Agent + Gate Critic.
2. Feature Mapper + Feature Reviewer.
3. Runtime/API/MCP Agent + Contract Reviewer.
4. Telemetry/Enterprise Agent + Privacy Reviewer.
5. Loopflow/DSL Integration Agent + DSL Author Reviewer.

First-wave success criteria:

- Local-fast lanes are run first and `.gate/local-fast.json` is inspected.
- Canonical tracker IDs are checked for collisions before any row update.
- Stale tracker status/evidence text is identified before any row update.
- Every reviewed surface has a row or explicit deferral in canonical trackers.
- Every finding has repro evidence.
- Every fix candidate has a scoped validation command.
- No PR is opened.
- No no-mistakes run is started.
- At least one commit records the review plan and tracker updates.

## Subagent Prompt Template

Use this template for each actor:

```text
You are the ACTOR for <pair name>. Work only in ctx. Inspect the named surface.
Return findings using docs/plans/ctx-agent-reviewer-workbench.md Evidence
Contract. Do not fix code. Do not create trackers. Use existing canonical
trackers only. Include exact files, commands, and observed behavior.
```

Use this template for each critic:

```text
You are the CRITIC for <pair name>. Review the actor output. Verify against
code, docs, tests, and artifacts. Reject weak evidence. Mark each item as
accepted, needs-evidence, duplicate, human-decision, or invalid. Do not fix
code. Do not create trackers.
```

Use this template for the CTO:

```text
Merge actor and critic outputs. Update only canonical trackers. Split accepted
fixes into small commits. Keep no-mistakes and PRs out of scope until the user
approves the final integrated validation phase.
```

## Commit Policy

- Commit review infrastructure separately from bug fixes.
- Commit tracker updates separately from code fixes.
- Commit each fix batch by surface.
- Do not open PRs until the user asks.
- Do not run no-mistakes until the user asks or the final integrated batch is
  ready.

## Fast Validation

Use these checks for review-plan-only commits:

```bash
python -m mkdocs build --strict
scripts/no_mistakes_run.sh fast --lane cheap --lane docs
```

Use broader local validation only when tracker updates or code fixes justify it.
