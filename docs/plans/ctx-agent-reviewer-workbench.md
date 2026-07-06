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
  state.

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

## Review Pairs

### 1. Feature Mapper Pair

Actor: Feature Mapper.

- Discover user-visible features across CLI, API, MCP, dashboard, telemetry,
  graph/wiki, skill management, agent management, harnesses, Loopflow adapter,
  importers, sync jobs, and packaging.
- Map each feature to current tests and docs.
- Propose missing user stories.

Critic: Feature Coverage Reviewer.

- Check whether each proposed feature is real in code.
- Check whether the expected behavior is based on code, not wishful docs.
- Reject duplicate or vague feature rows.

Primary outputs:

- `qa/feature_status.csv` updates.
- Missing-story findings for `qa/bug_smoke_status.csv`.

### 2. Runtime/API/MCP Pair

Actor: Runtime/API/MCP Reviewer.

- Review `src/ctx/api.py`, `src/ctx/mcp_server/`, generic adapters, runtime
  lifecycle, tool routing, and permission enforcement.
- Exercise API and MCP boundaries from a user perspective.

Critic: Contract Reviewer.

- Check schema stability, error payloads, backwards compatibility, permission
  semantics, and trace propagation.
- Verify that behavior is test-backed.

Primary outputs:

- Contract findings.
- Permission or trace-propagation gaps.
- Focused tests for API/MCP regressions.

### 3. Telemetry/Enterprise Pair

Actor: Telemetry Enterprise Reviewer.

- Review traces, metrics, logs, events, token usage KPIs, exporters, retention,
  and local privacy defaults.
- Check skill, MCP, and agent lifecycle token attribution.

Critic: Privacy and Governance Reviewer.

- Check redaction, hashed errors, salt boundaries, nested trace propagation,
  exporter opt-in safety, and enterprise docs.
- Treat secrets, host paths, and user prompts as sensitive by default.

Primary outputs:

- Telemetry privacy findings.
- Enterprise-readiness gaps.
- Runbook/doc mismatches.

### 4. Dashboard/UX Pair

Actor: Dashboard Flow Reviewer.

- Walk dashboard pages and main flows: home, docs, graph, loaded entities,
  recommendations, skills, ops, config, wiki, and telemetry surfaces.
- Capture browser evidence for visual or interaction findings.

Critic: UX Critic.

- Check copy accuracy, empty states, broken links, stale counters, layout
  regressions, inaccessible controls, and misleading success/error states.
- Reject UX findings without screenshot or rendered artifact evidence when a
  visual surface exists.

Primary outputs:

- UX findings in `qa/bug_smoke_status.csv`.
- Browser reproduction steps.

### 5. Graph/Wiki/Recommendation Pair

Actor: Graph and Recommendation Reviewer.

- Review graph artifacts, wiki packs, semantic edges, entity overlays,
  recommendation surfaces, subgroup selection, and Loopflow recommendation
  behavior.
- Check explainability for skills, agents, MCPs, harnesses, and ctx MCP tools.

Critic: Evidence Reviewer.

- Verify every recommendation can be traced to code, graph data, registry data,
  or test fixtures.
- Check stale graph artifact handling and LFS hydration expectations.

Primary outputs:

- Recommendation correctness findings.
- Graph artifact or stale-doc findings.

### 6. QA/Gate Pair

Actor: QA Gate Reviewer.

- Run focused tests, `scripts/no_mistakes_run.sh fast`, CI classifier checks,
  no-test policy checks, and local gate timing summaries.
- Identify slow, flaky, redundant, or missing gates.

Critic: Gate Critic.

- Check whether the test evidence actually proves the reviewed behavior.
- Separate local CPU bottlenecks from GitHub-hosted runner delays and
  no-mistakes agent overhead.

Primary outputs:

- Gate efficiency findings.
- Test coverage gaps.
- Local-fast timing reports.

### 7. Security/Supply-Chain Pair

Actor: Security Reviewer.

- Review install scripts, Hugging Face sync, Git LFS artifacts, external skill
  imports, MCP ingestion, exporters, and generated artifacts.
- Look for secret leaks, path traversal, unsafe subprocess usage, and poisoned
  metadata risks.

Critic: Red-Team Reviewer.

- Try adversarial skill, MCP, agent, DSL, and artifact inputs.
- Check whether failures are explicit, safe, and recoverable.

Primary outputs:

- Security findings.
- Repro inputs.
- Minimal hardening recommendations.

### 8. Docs/Runbook Pair

Actor: Docs Validator.

- Compare docs to code behavior for install, CLI usage, dashboard, telemetry,
  Loopflow, graph artifacts, HF sync, and gates.

Critic: Evidence Editor.

- Reject unsupported claims, stale prose, and docs that do not tell the user how
  to verify behavior.
- Check whether docs require public nav changes or should remain internal.

Primary outputs:

- Docs drift findings.
- Concrete command examples.

### 9. Loopflow/DSL Pair

Actor: DSL Adapter Reviewer.

- Review the Loopflow adapter and generic DSL host story.
- Check permissions for skills, harnesses, MCPs, agents, ctx MCP server tools,
  recommendations, and agent loops.

Critic: DSL Author Reviewer.

- Evaluate from the Loopflow author's perspective: does ctx add useful
  recommendation and runtime guidance without stealing control from the DSL?
- Check whether users with their own LLM can still receive ctx recommendations.

Primary outputs:

- Adapter UX findings.
- Permission model findings.
- Demo gaps.

## First Wave

Run these pairs first because they give the most leverage:

1. Feature Mapper Pair.
2. QA/Gate Pair.
3. Runtime/API/MCP Pair.
4. Telemetry/Enterprise Pair.
5. Loopflow/DSL Pair.

First-wave success criteria:

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
