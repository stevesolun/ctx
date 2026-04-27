# ctx Deep Review Report - 2026-04-26

## Scope

This report covers two related passes:

1. The remediation pass for the six review findings against ctx's recommendation system and generic harness.
2. A follow-up repo/user-flow review that re-ran the project through an A-Z first-user path and looked for remaining bugs.

The codebase under review is the Python package rooted at `C:\Steves_Files\Work\Research_and_Papers\ctx`.

## Phase 6 Update - 2026-04-27

The four follow-up review findings were addressed in Phase 6.

Fixed:

- `src/ctx_init.py` now invokes package module targets:
  - `python -m ctx.adapters.claude_code.inject_hooks`
  - `python -m ctx.core.wiki.wiki_graphify`
- `ctx-init` now returns non-zero when an explicitly requested hooks or graph step fails.
- Re-running `ctx-init` when starter toolboxes already exist now reports:
  - `[skip] toolboxes already seeded (use --force to overwrite)`
  instead of warning.
- `docs/knowledge-graph.md` now documents the shared
  `ctx.core.resolve.recommendations.recommend_by_tags` path used by
  public ctx tools, the standalone MCP server, Claude Code hooks, and
  `resolve_skills.resolve()`.
- `docs/skill-quality-install.md` now points graph troubleshooting at
  `ctx-wiki-graphify --graph-only`.

Phase 6 regressions added:

- exact hook package-module invocation
- exact graph package-module invocation
- non-zero return when requested hook install fails
- non-zero return when requested graph build fails
- idempotent toolbox re-run skip behavior

Phase 6 focused verification:

- `python -m pytest src\tests\test_ctx_init.py -q`
  - `10 passed in 1.07s`
- isolated `ctx-init --hooks`
  - exit 0
  - wrote isolated `settings.json`
  - printed `[ok] PostToolUse + Stop hooks injected`
- isolated `ctx-init --graph`
  - exit 0
  - printed `[skip] toolboxes already seeded`
  - built an empty graph successfully in the isolated home
- stale-doc search over the two edited docs for
  `resolve_by_seeds`, `graph neighbor`, `python -m wiki_graphify`,
  and `src/wiki_graphify.py`
  - no matches

Phase 6 full-suite/static verification:

- `python -m pytest -q`
  - `3148 passed, 8 skipped in 108.92s (0:01:48)`
- `python -m compileall -q src hooks scripts`
  - passed
- `python -m ruff check src\ctx_init.py src\tests\test_ctx_init.py --quiet`
  - passed
- `python -m mypy --ignore-missing-imports src\ctx_init.py`
  - `Success: no issues found in 1 source file`
- root `package.json` count:
  - `0`, so no project `npx tsc --noEmit` or `npx eslint . --quiet`
    command is configured at the repo root.
- repo-wide `python -m ruff check src hooks scripts --quiet`
  - still fails on pre-existing lint debt outside Phase 6, including
    `src\council_runner.py:61` (`E402`), unused imports in
    generic harness modules, and existing lint debt in test files.
- repo-wide `python -m mypy --ignore-missing-imports src\ctx src\scan_repo.py`
  - still fails on pre-existing type debt:
    `src\ctx\adapters\claude_code\skill_loader.py:96` and
    `src\ctx\adapters\claude_code\install\install_utils.py:223`.

## Product Intent Reconstructed

Observed from `README.md`, `docs/index.md`, `docs/knowledge-graph.md`, and `pyproject.toml`:

- ctx is a Claude Code ecosystem manager for skills, agents, and MCP servers.
- It scans a repository, detects stack signals, maps those signals into recommended skills/agents/MCPs, and keeps a Claude home wiki/manifest/audit trail current.
- It has three user-facing recommendation surfaces:
  - Claude Code hooks, especially `context_monitor.graph_suggest`.
  - Public/generic tools via `CtxCoreToolbox` and `ctx-mcp-server`.
  - Scan/resolve flow via `ctx-scan-repo --recommend`.
- It also contains a model-agnostic generic harness:
  - `ctx run`
  - `ctx resume`
  - `ctx sessions`
  - optional MCP routing
  - optional ctx-core tools
  - optional compaction/evaluation/planning.
- The intended first-user path is:
  - `pip install claude-ctx`
  - `ctx-init`
  - optionally `ctx-init --hooks`
  - optionally `ctx-init --graph`
  - `ctx-scan-repo --repo . --recommend`
  - inspect/manage with `ctx-monitor serve`
  - optionally use `ctx` harness or `ctx-mcp-server`.

## What The Original Review Revealed

### 1. Recommendation behavior was not one product

The repo claimed one recommendation system, but code had multiple independent implementations:

- `src/ctx/adapters/generic/ctx_core_tools.py` tokenized free-text queries and called graph recommendation logic itself.
- `src/ctx/adapters/claude_code/hooks/context_monitor.py` had a separate graph scorer.
- `src/ctx/core/resolve/resolve_skills.py` used installed skill names as graph seeds and walked graph neighbors.
- `src/scan_repo.py` rendered resolver output with additional assumptions about skill/agent shape.

Impact: a FastAPI project could get different rankings, different entity types, or no results depending on whether the user entered through MCP, library/harness tools, Claude Code hooks, or `ctx-scan-repo`.

### 2. Agent recommendations were structurally unreachable in scan flow

`resolve_skills.resolve()` discovered installed `SKILL.md` files and dropped non-MCP graph hits unless the name existed in that installed skill map. Agent graph hits were not installed skills, so the resolver could not emit them.

At the same time, `scan_repo._print_recommendations()` expected agent entries in `manifest["load"]` where `type == "agent"`. Those entries could not be produced by the resolver before the fix.

Impact: the scan UI had an Agents section that was effectively dead for graph-derived agents.

### 3. Resume was not a true continuation

`ctx resume` reconstructed provider and transcript but not the tool surface:

- MCP router was not restarted.
- ctx-core tools were not recreated.
- previous tool executor wiring was absent.
- the new follow-up task was prepended before prior transcript in the loop seed order.

Impact: a resumed harness session saw a different world from the original session and could not call tools that were available before.

### 4. MCP request timeout was not actually enforced

`McpClient.call()` computed deadlines, but the read path used blocking `stdout.readline()`.

Impact: if an MCP server accepted a request and then stayed silent, the harness could hang indefinitely despite `request_timeout`.

### 5. Provider empty/truncated output was incorrectly successful

`run_loop()` marked any response with no tool calls as `completed`.

Impact: empty content, `finish_reason="length"`, and `finish_reason="other"` without tool calls could all be reported as successful runs.

### 6. Compaction usage was not counted

`TokenBudgetCompactor._summarise_middle()` performed an extra provider call but returned only text. `run_loop()` never added that response's usage to totals.

Impact: long sessions could underreport tokens and cost, weakening budget enforcement and audit accuracy.

## What Was Changed And Why

### Shared Recommendation Engine

Added `src/ctx/core/resolve/recommendations.py`.

The new module provides:

- `query_to_tags(query: str) -> list[str]`
- `recommend_by_tags(graph, tags, top_n=...) -> list[dict]`

The shared ranker scores by:

- entity name match: strong signal
- tag overlap: medium signal
- graph degree: small tiebreaker

Why: the same project context should produce the same ordering and entity shape across MCP, harness, hooks, and scan. Centralizing this eliminated duplicated scoring logic.

Files changed:

- `src/ctx/core/resolve/recommendations.py`
- `src/ctx/adapters/generic/ctx_core_tools.py`
- `src/ctx/adapters/claude_code/hooks/context_monitor.py`
- `src/ctx/core/resolve/resolve_skills.py`

Validation:

- `src/tests/test_harness_ctx_core.py` verifies free-text query can match entity name.
- `src/tests/test_context_monitor.py` verifies hook suggestions equal `recommend_by_tags`.
- `src/tests/test_resolve_skills.py` verifies resolver uses detected stack tags with `_recommend_by_tags`.

### Resolver And Scan Agent Path

Changed `src/ctx/core/resolve/resolve_skills.py`:

- graph recommendations now use detected stack tags instead of installed skill seeds
- graph runs even if no seed skill was installed
- graph-backed agents are admitted without requiring a local `SKILL.md`
- skills still require local availability
- `manifest["load"]` entries now carry `type` and `entity_type`
- graph-derived load entries preserve `score` and `matching_tags`

Changed `src/scan_repo.py`:

- splits `manifest["load"]` into skills and agents before rendering
- legacy entries with no type still render as skills

Why: scan recommendations can now surface agent graph hits and present them in the intended section without corrupting skill counts.

Validation:

- `src/tests/test_resolve_skills.py::TestResolveSharedGraphRecommendations`
- `src/tests/test_scan_repo.py::TestPrintRecommendations`
- A-Z isolated scan showed:
  - `-- Skills (1) -- fastapi-pro`
  - `-- Agents (1) -- api-reviewer`

### Harness Resume

Changed `src/ctx/cli/run.py`:

- added `_mcp_configs_from_metadata()`
- resume recreates MCP router from session metadata
- resume recreates ctx-core tools unless disabled in metadata
- resume restores session parameters such as temperature, max iterations, and budgets
- resume starts/stops router around the loop

Changed `src/ctx/adapters/generic/loop.py`:

- added `append_task_after_messages`
- when true, replayed messages stay first and the follow-up is appended after history
- existing leading system message is preserved rather than duplicated

Why: resume should be a continuation, not a new task shoved before the transcript.

Validation:

- `src/tests/test_harness_cli_run.py::TestResumeCommand::test_resume_replays_history_before_follow_up_and_keeps_ctx_tools`
- `src/tests/test_harness_loop.py::TestResumePath::test_task_can_be_appended_after_replayed_messages`
- A-Z harness run/resume both exited 0 with `stop_reason="completed"`.

### MCP Timeout

Changed `src/ctx/adapters/generic/tools/mcp_router.py`:

- added background stdout drain thread
- added queue-backed `_read_frame(deadline)`
- `call()` now raises `McpServerError` on queue timeout
- malformed frames are still dropped and reading continues

Changed `src/tests/fixtures/fake_mcp_server.py`:

- added `FAKE_MCP_HANG_ON_TOOL=1`

Why: a timeout must govern the blocking read itself, not only code around it.

Validation:

- `src/tests/test_mcp_router.py::TestClientRobustness::test_silent_server_call_respects_request_timeout`
- focused MCP suite passed.

### Empty/Truncated Provider Completion

Changed `src/ctx/adapters/generic/loop.py`:

- added stop reasons:
  - `length`
  - `empty_response`
  - `provider_other`
- no-tool-call responses only become `completed` when content is non-empty and finish reason is normal.

Why: a run that has no usable output is not a successful agent completion.

Validation:

- `src/tests/test_harness_loop.py::TestCompletion::test_length_finish_reason_is_not_completed`
- `src/tests/test_harness_loop.py::TestCompletion::test_empty_stop_response_is_not_completed`

### Compaction Usage Accounting

Changed `src/ctx/adapters/generic/compaction.py`:

- `CompactionResult` now carries `usage`
- `TokenBudgetCompactor.compact_with_usage()` returns new messages and summary-call usage
- `compact_now()` uses `compact_with_usage()` when available

Changed `src/ctx/adapters/generic/loop.py`:

- when compactor has `compact_with_usage`, loop adds summary usage to running totals

Why: summary calls are provider calls and must count toward budgets.

Validation:

- `src/tests/test_harness_loop.py::TestUsage::test_compaction_usage_counts_toward_total`

## A-Z User Flow Verification

This was run in an isolated temp home:

`C:\Users\solun\AppData\Local\Temp\ctx-az-32b58a7f491f40cf804c7a2ec5af6031\home`

The test set `USERPROFILE` and process `HOME` before launching Python. `os.path.expanduser("~")` printed that isolated path.

### Flow Steps And Outcomes

1. `python -m ctx_init`
   - Exit: 0
   - Created 11 subdirectories.
   - Wrote `skill-system-config.json`.
   - Seeded 5 starter toolboxes.

2. `python -m ctx_init --hooks`
   - Exit: 0
   - But stderr contained: `No module named inject_hooks`.
   - This is a real bug: the hook install failed but the command still returned success.

3. `python -m ctx_init --graph`
   - Exit: 0
   - But stderr contained: `No module named wiki_graphify`.
   - This is a real bug: graph build failed but the command still returned success.

4. `python -m toolbox list`
   - Exit: 0
   - Listed starter toolboxes: `docs-review`, `fresh-repo-init`, `refactor-safety`, `security-sweep`, `ship-it`.

5. `python -m scan_repo --repo <sample> --output <profile> --recommend`
   - Exit: 0
   - Detected 5 stack elements.
   - Classified project as `api-service`.
   - Rendered `fastapi-pro` under Skills.
   - Rendered `api-reviewer` under Agents.
   - This confirms the repaired scan agent flow works in a realistic sample repo.

6. `python -m ctx.mcp_server.server` JSON-RPC interaction
   - Exit: 0
   - `initialize` returned server name `ctx-wiki`.
   - `tools/list` returned:
     - `ctx__recommend_bundle`
     - `ctx__graph_query`
     - `ctx__wiki_search`
     - `ctx__wiki_get`
   - `tools/call ctx__recommend_bundle` returned both:
     - `fastapi-pro` skill
     - `api-reviewer` agent

7. `python -m ctx_monitor serve --port 18766`
   - HTTP GET `/` returned status 200.
   - Home page contained `Currently loaded`.

8. `python -m ctx.cli.run run --model ollama/fake ...`
   - Exit: 0
   - Used a fake local `litellm` module.
   - Returned JSON with `stop_reason="completed"` and `final_message="fake final answer"`.

9. `python -m ctx.cli.run resume az-flow ...`
   - Exit: 0
   - Returned JSON with `stop_reason="completed"`.

10. `python -m ctx.cli.run sessions`
    - Exit: 0
    - Listed `az-flow`.

### A-Z Side Note

An earlier attempt used PowerShell's reserved `$HOME` variable incorrectly. That caused the test to use `C:\Users\solun\.claude`. I restored the two overwritten backed-up skill files from `C:\Users\solun\.claude\backups\20260419T103608.992918Z` and removed the generated `file-reading` stub. The corrected isolated run above is the one to trust.

## Fresh Verification Evidence

### Passing

- `python -m pytest -q`
  - `3145 passed, 8 skipped in 95.89s`
- `python -m compileall -q src hooks scripts`
  - exit 0, no output
- `python -m pip wheel . --no-deps -w <temp>`
  - built `claude_ctx-0.6.4-py3-none-any.whl`
- Script target import probe
  - `script_import_failures=[]`
  - `missing_modules=[]`
  - `missing_packages=[]`
- Focused touched-file checks from the remediation pass:
  - focused regression suite: `293 passed`
  - touched-file ruff: exit 0
  - touched-source mypy: `Success: no issues found in 9 source files`

### Failing / Existing Debt

`python -m ruff check src hooks scripts --quiet` exits 1 with repo-wide lint debt. Examples:

- `src/council_runner.py:61` E402 import not at top
- `src/ctx/adapters/generic/contract.py` unused imports
- `src/ctx_monitor.py:54` unused `datetime`, `timezone`
- many test files with unused imports, ambiguous `l`, or unused variables

`python -m mypy --ignore-missing-imports src\ctx src\scan_repo.py` exits 1 with:

- `src\ctx\adapters\claude_code\skill_loader.py:96`: missing annotation for `manifest`
- `src\ctx\adapters\claude_code\install\install_utils.py:223`: `str.maketrans` dict type mismatch

These failures were not introduced by the remediation patch, but they are real repo quality debt.

## New Findings From The Follow-Up Review

### Finding A - Fixed in Phase 6: `ctx-init --hooks` and `ctx-init --graph` invoked removed module names and still returned success

File: `src/ctx_init.py`

Evidence:

- `install_hooks()` runs `python -m inject_hooks` at lines 120-123.
- `build_graph()` runs `python -m wiki_graphify` at lines 149-150.
- Neither `src/inject_hooks.py` nor `src/wiki_graphify.py` exists.
- `pyproject.toml` registers the canonical targets as:
  - `ctx.adapters.claude_code.inject_hooks:main`
  - `ctx.core.wiki.wiki_graphify:main`
- `main()` prints warnings on non-zero subcommand return codes but still returns 0 at line 229.

A-Z reproduction:

- `python -m ctx_init --hooks`
  - stderr: `No module named inject_hooks`
  - command exit: 0
- `python -m ctx_init --graph`
  - stderr: `No module named wiki_graphify`
  - command exit: 0

Impact:

Users following README's first-run setup can believe hooks or graph were installed when they were not.

Recommended fix:

- Replace the stale module invocations with canonical package modules:
  - `python -m ctx.adapters.claude_code.inject_hooks`
  - `python -m ctx.core.wiki.wiki_graphify`
- Track subcommand failures and return non-zero if an explicitly requested optional step fails.
- Add tests for `ctx_init.install_hooks`, `ctx_init.build_graph`, and `ctx_init.main(["--hooks"])` / `main(["--graph"])` failure propagation.

Status: fixed in Phase 6.

Priority: P1.

### Finding B - Fixed in Phase 6: `ctx-init` was not cleanly idempotent for toolboxes

File: `src/ctx_init.py`

Evidence:

- The module docstring says re-running only writes what is missing.
- `seed_toolboxes()` always runs `python -m toolbox init`.
- When toolboxes already exist, `toolbox init` returns 1 and prints `Global config already has 5 toolbox(es). Use --force to overwrite.`
- `ctx-init` surfaces that as `[warn] toolbox init returned 1`.

A-Z reproduction:

- first `ctx-init`: seeds 5 starter toolboxes
- second `ctx-init --hooks` and `ctx-init --graph`: both print toolbox warnings before the requested optional step

Impact:

Normal re-runs look partially broken even when the existing toolbox state is expected. That makes it harder for users to notice the real hook/graph failures above.

Recommended fix:

- Teach `ctx-init` to inspect the toolbox config path and skip seeding when starters already exist, or teach `toolbox init` to return a distinguishable "already exists" status.
- Treat "already seeded" as `[skip]`, not `[warn]`.

Status: fixed in Phase 6.

Priority: P2.

### Finding C - Fixed in Phase 6: Documentation described the old recommendation resolver

File: `docs/knowledge-graph.md`

Evidence:

- Lines 116-118 still say the recommendation path seeds `resolve_by_seeds(G, matched_slugs)` and emits `reason="graph neighbor of <slug> via shared tags [...]"`.
- The current fixed implementation uses `recommend_by_tags()` over detected stack tags and reasons like `graph match for fastapi`.

Impact:

Future contributors could reintroduce the same architecture split because the docs describe the pre-fix behavior as canonical.

Recommended fix:

- Update `docs/knowledge-graph.md` to describe `ctx.core.resolve.recommendations.recommend_by_tags`.
- State explicitly that public ctx tools, hooks, resolver, scan, and MCP server share this ranker.

Status: fixed in Phase 6.

Priority: P2.

### Finding D - Fixed in Phase 6: Documentation pointed users at invalid module commands

File: `docs/skill-quality-install.md`

Evidence:

- Lines 170-171 tell users to run `python -m wiki_graphify --graph-only`.
- There is no flat `wiki_graphify` module.
- The registered CLI is `ctx-wiki-graphify`; the package module is `ctx.core.wiki.wiki_graphify`.

Impact:

Troubleshooting instructions fail exactly when a user is already trying to recover a broken graph view.

Recommended fix:

- Replace `python -m wiki_graphify --graph-only` with `ctx-wiki-graphify --graph-only` if that flag exists, or the correct supported graph rebuild command.

Status: fixed in Phase 6.

Priority: P2.

### Finding E - README/docs operational numbers are stale

Files:

- `README.md`
- `docs/index.md`
- `docs/knowledge-graph.md`

Evidence:

- `README.md` badge says `Tests-1807_passing`.
- Fresh pytest reports `3145 passed, 8 skipped`.
- README and docs disagree on graph/entity counts:
  - README says 13,041 nodes / 847K edges.
  - docs index says 2,253 nodes / 454,719 edges in several places.

Impact:

This is not a runtime bug, but it undermines trust and makes it hard to tell which shipped graph is authoritative.

Recommended fix:

- Decide the authoritative graph artifact and update README/docs/changelog references.
- Consider adding a docs consistency check around test badge and graph metadata if those are intentionally maintained.

Priority: P3.

### Finding F - Repo-wide lint/type gates are not enforceable yet

Files:

- many; see ruff/mypy output above

Evidence:

- Full ruff exits 1.
- Full source mypy exits 1.
- Pytest and compileall pass.

Impact:

The project has good behavioral coverage but cannot currently use ruff/mypy as release-blocking gates without either narrowing scope or paying down backlog.

Recommended fix:

- Run a separate cleanup phase for static quality debt.
- Use a targeted allowlist/changed-file gate until repo-wide gates are clean.

Priority: P2 for maintainability, P3 for runtime risk.

## Current Risk Assessment

High confidence fixed:

- Original six reviewed defects.
- Recommendation surface convergence for public tools, hooks, resolver, scan, and MCP.
- Scan agent rendering.
- Resume ordering/tool restoration.
- MCP timeout enforcement.
- Empty/truncated response stop reasons.
- Compaction usage accounting.

High confidence still broken:

- `ctx-init --hooks`.
- `ctx-init --graph`.
- `ctx-init` idempotent toolbox messaging.
- docs that describe old resolver/module names.

No evidence of breakage in the A-Z happy path for:

- baseline `ctx-init`
- starter toolbox listing
- scan/recommend
- MCP server tool listing/calls
- dashboard home page
- harness run/resume/session listing

## Recommended Next Phase

Phase 6 should be a small setup/docs fix pass touching no more than 5 files:

1. `src/ctx_init.py`
   - call canonical modules
   - return non-zero when requested optional steps fail
   - make toolbox "already exists" idempotent
2. `src/tests/test_ctx_init.py` or equivalent
   - add regressions for hooks/graph module invocation and failure propagation
3. `docs/knowledge-graph.md`
   - update recommender architecture
4. `docs/skill-quality-install.md`
   - replace stale graph rebuild command
5. `README.md` or a follow-up docs metadata file
   - reconcile public counts/test badge if desired

Suggested verification:

- `python -m pytest src\tests\test_ctx_init.py -q`
- isolated A-Z flow again
- `python -m pytest -q`
- touched-file ruff/mypy
- `python -m compileall -q src hooks scripts`
