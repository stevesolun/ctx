# ctx Deep Review Report - 2026-04-26

## Scope

This report covers two related passes:

1. The remediation pass for the six review findings against ctx's recommendation system and generic harness.
2. A follow-up repo/user-flow review that re-ran the project through an A-Z first-user path and looked for remaining bugs.

The codebase under review is the Python package rooted at `C:\Steves_Files\Work\Research_and_Papers\ctx`.

## Current-State Deep Audit Update - 2026-04-27

This section supersedes any earlier "fixed" status claims where the
current checked-out tree contradicts them. The working tree was re-read
and verified after the parallel review lanes completed.

### Static Gate Remediation

The repo-wide static-analysis caveat has been fixed in the current
working tree.

Observed before remediation:

- `python -m ruff check src hooks scripts --quiet --statistics`
  - `61 F401`, `18 E741`, `11 F841`, `5 F541`, `3 E402`, `2 E702`
- `python -m mypy --ignore-missing-imports src\ctx src\scan_repo.py`
  - `11 errors in 7 files`

What changed:

- Removed unused imports and unused locals across production and test
  files.
- Renamed ambiguous one-letter JSONL loop variables in tests.
- Split semicolon-compressed setup statements.
- Added narrow type annotations for dictionaries and path lists.
- Fixed the semantic-cache sentinel typing in
  `ctx.core.resolve.recommendations`.
- Fixed the `run_loop()` local `result` variable collision that made
  mypy infer the function return as `str`.
- Replaced the `str.maketrans(dict[str, str])` call in install scalar
  rendering with the typed two-string form.

Fresh verification after the persisted changes:

- `python -m ruff check src hooks scripts --quiet`
  - exit 0
- `python -m mypy --ignore-missing-imports src\ctx src\scan_repo.py`
  - `Success: no issues found in 58 source files`
- `python -m compileall -q src hooks scripts`
  - exit 0
- `python -m pytest -q`
  - `3169 passed, 7 skipped in 381.47s (0:06:21)`

### Critical Correction

The earlier Phase 6 section below says several ctx-init and
recommendation-doc issues were fixed. The current checked-out tree does
not match that claim.

Current observations:

- `src/ctx_init.py` still runs `python -m inject_hooks`.
- `src/ctx_init.py` still runs `python -m wiki_graphify`.
- `docs/knowledge-graph.md` still names `src/wiki_graphify.py`.
- `docs/knowledge-graph.md` still describes
  `resolve_by_seeds(G, matched_slugs)` and `graph neighbor` reasons.
- `docs/skill-quality-install.md` still recommends
  `python -m wiki_graphify --graph-only`.
- `ctx.adapters.claude_code.hooks.context_monitor.graph_suggest()` still
  implements a local scorer instead of calling `recommend_by_tags()`.
- `resolve_skills.py` still imports and calls `resolve_by_seeds()`.

This means the architecture is still partially split. Public ctx tools
use the shared ranker, but Claude Code hook recommendations and resolver
graph augmentation remain separate behavior surfaces.

### Executive Verdict

The project is materially better than a prototype, but it is not yet
release-hard from a CTO/security/reliability perspective.

The strongest parts:

- Broad behavioral test suite: 3,169 passing tests in the current run.
- Ruff and mypy can now pass repo-wide.
- The generic harness has real abstractions for providers, sessions,
  observers, tools, compaction, planning, and evaluation.
- Security hardening exists in several places: slug validation,
  path-containment checks, YAML scalar escaping, and atomic write helpers.

The release blockers:

- Setup entrypoints are still broken or stale in current source/docs.
- Automatic hook recommendation behavior remains split from the shared
  recommendation engine.
- MCP subprocesses inherit the full parent environment, including secrets.
- Dashboard APIs can expose or mutate local state if bound off localhost.
- Backup code has path/symlink escape risks that contradict its safety
  promise.
- Packaging tests do not verify the built wheel, and the wheel likely
  omits runtime config files.
- CI publish is not gated by the same verification now proven to matter.

### P0/P1 Findings

#### P0 - `ctx-init` Optional Steps Still Call Removed Modules

Evidence:

- `src/ctx_init.py` invokes `sys.executable, "-m", "inject_hooks"`.
- `src/ctx_init.py` invokes `sys.executable, "-m", "wiki_graphify"`.
- The package module paths are
  `ctx.adapters.claude_code.inject_hooks` and
  `ctx.core.wiki.wiki_graphify`.

Impact:

- `ctx-init --hooks` and `ctx-init --graph` can fail while users think
  initialization succeeded.
- This breaks the first-user A-Z flow and makes hook/graph setup
  unreliable.

Required fix:

- Change the invoked modules to package paths.
- Propagate non-zero return codes for explicitly requested optional
  steps.
- Add tests asserting exact module names and non-zero failure behavior.

#### P0 - Hook Installer Emits Dead/Non-Portable Commands

Evidence:

- `inject_hooks.make_hooks()` still emits direct script-style commands
  such as `python3 ...context_monitor.py` and shell fragments like
  `2>/dev/null || true`.
- Current hook modules live under
  `ctx.adapters.claude_code.hooks`, not flat package root scripts.

Impact:

- Installed Claude Code hooks can silently do nothing.
- The POSIX-shell command shape is fragile on Windows.
- `|| true` masks broken hook execution.

Required fix:

- Generate package-module commands with the current interpreter:
  `python -m ctx.adapters.claude_code.hooks.context_monitor` and
  package-module bundle hook equivalents.
- Avoid shell-specific redirection in JSON hook commands where possible.
- Stop swallowing hook failures during explicit install/test modes.

#### P1 - Recommendation Unification Is Incomplete

Evidence:

- `context_monitor.graph_suggest()` locally scores labels, tag overlap,
  and degree.
- Public ctx tools use
  `ctx.core.resolve.recommendations.recommend_by_tags()`.
- `resolve_skills.py` imports and calls `resolve_by_seeds()`.

Impact:

- Recommendations differ by entrypoint.
- A user can see different rankings or different empty/non-empty results
  from hooks, public tools, and scan/resolve.
- Documentation currently promises more consistency than the code
  delivers.

Required fix:

- Make hook suggestions call the shared ranker.
- Decide whether resolver is a tag recommender or a graph-neighbor loader.
  If it is a recommender, use `recommend_by_tags()`. If it remains a
  loader, update docs and product claims.
- Add parity tests that monkeypatch or fixture the shared ranker and prove
  hook/public/scan paths agree.

#### P1 - MCP Servers Inherit All Parent Secrets

Evidence:

- `McpClient` builds subprocess environment from `os.environ.copy()`.
- Configured MCP server commands receive unrelated credentials such as
  API keys, cloud credentials, GitHub tokens, and `SSH_AUTH_SOCK`.

Impact:

- Any MCP process, including third-party `npx` servers, can exfiltrate
  secrets without model-visible output.
- This is the highest-risk security issue in the generic harness.

Required fix:

- Default to a minimal environment.
- Allow explicit env pass-through by name.
- Redact secrets in logs and metadata.
- Add tests proving fake secrets are not visible to child processes unless
  explicitly allowed.

#### P1 - Backup Can Copy Files Outside The Intended Claude Home

Evidence:

- `backup_config.py` promises credential files are never copied.
- Configurable `top_files` are joined under `CLAUDE_HOME` without a
  resolved containment check.
- Backup tree roots can start on a symlink/junction target even with
  `os.walk(..., followlinks=False)`.

Impact:

- A malicious or corrupted backup config can snapshot credentials such as
  SSH keys or cloud credentials into Claude backup directories.
- Symlink/junction starting roots can bypass the "never follows symlinks"
  safety statement.

Required fix:

- Validate every configured file/root by `resolve(strict=False)` against
  the intended home.
- Reject symlink/junction roots.
- Enforce an explicit denylist for credential-like names even after
  resolution.
- Add traversal and symlink-root tests.

#### P1 - Dashboard Entity APIs Have Path/Exposure Risks

Evidence:

- Skill sidecar lookup builds `_sidecar_dir() / f"{slug}.json"` from
  route input without slug validation.
- `ctx-monitor --host 0.0.0.0` exposes unauthenticated read and mutation
  APIs.
- The HTTP server is single-threaded, and the live SSE endpoint can hold
  the only request thread indefinitely.

Impact:

- LAN clients can read local ctx state or trigger mutations if the
  dashboard is bound off localhost.
- Path traversal can read JSON files adjacent to the sidecar directory.
- Opening the live events page can stall the rest of the dashboard.

Required fix:

- Validate slugs for all API routes.
- Default-bind localhost and require an explicit unsafe flag/token for
  non-localhost binding.
- Use `ThreadingHTTPServer` or move SSE to a separate worker path.

#### P1 - Packaging Artifact Is Not Verified

Evidence:

- `pyproject.toml` declares package data for `config.json` and
  `skill-registry.json`, but those files live beside flat modules, not
  inside a package.
- `ctx_config.py` expects `config.json` next to `ctx_config.py`.
- `ctx.__version__` reports `0.1.0-alpha` while package metadata reports
  `0.6.4`.
- The publish workflow builds and publishes without running tests, ruff,
  mypy, `pip check`, `twine check`, or wheel smoke tests.

Impact:

- The installed artifact can differ from the tested source tree.
- Runtime config can be missing after install.
- Support/debug output can report the wrong version.
- PyPI tags can ship unverified artifacts.

Required fix:

- Move config data into a package or configure setuptools data for
  py-modules correctly.
- Derive `ctx.__version__` from package metadata or keep it synchronized.
- Add wheel-build, wheel-inspection, smoke-install, entrypoint, and
  package-data tests.
- Gate publish on the same checks used locally.

### P2 Findings

#### P2 - Model-Selected MCP Tools Execute Without Policy Confirmation

The harness sends attached MCP tools to the model and dispatches returned
tool calls directly. With filesystem, Git, browser, or GitHub MCPs
attached, prompt injection in a hostile repository can steer the model
into sensitive reads or mutations.

Required fix: introduce a policy layer for dangerous tools, per-server
allow/deny scopes, and optional user confirmation for side-effecting or
secret-bearing tools.

#### P2 - `--cmd-json` Bypasses MCP Install Command Policy

The MCP installer validates command allowlists for `--cmd`, but raw
`--cmd-json` is passed to `claude mcp add-json` after JSON parsing.

Required fix: parse `--cmd-json` into the same normalized command model
and apply the same executable/argument policy.

#### P2 - Graph Incremental Patch Can Miss Changed Existing Nodes

Graph patch recovery does not reliably compare prior and current content
hashes for existing nodes before state overwrite, so semantic incident
edges can remain stale after a page body/tag change.

Required fix: compute affected IDs from the semantic edge pass before
saving new state, or compare against the prior top-K state explicitly.

#### P2 - Semantic `min_cosine` Is Not Enforced By Consumers

Graph build exposes/configures semantic cosine thresholds, but consumers
load raw graphs and walk all edges.

Required fix: centralize graph loading through a filtered
recommendation-graph loader or have `resolve_graph.load_graph()` apply the
configured minimum by default.

#### P2 - Graphify Dry-Run Still Writes Artifacts

The `--dry-run` mode is documented as preview-only, but graph export
happens before mutation dry-run handling.

Required fix: make dry-run skip artifact writes or write only to an
explicit temp/output path.

#### P2 - Repo Scan Treats Optional Extras As Active Stack

`read_toml_deps()` folds every optional dependency group into active
dependencies. For this repo, optional `torch` can classify the project as
ML even if the core package does not require ML.

Required fix: distinguish active dependencies from optional extras and
label extras separately in the profile.

#### P2 - Monitor Cannot Open MCP Wiki Cards It Lists

The index can list recursive MCP pages, but entity detail/graph lookup
paths only search skills and agents in several routes.

Required fix: include `entities/mcp-servers` in detail and neighborhood
lookup, and disambiguate entity type in URLs.

#### P2 - Wiki Writers Use Non-Atomic Read-Modify-Write Paths

Several wiki/index/catalog/conversion paths still call `write_text()`
directly even though the repo has atomic write helpers.

Required fix: put wiki mutations under a per-wiki lock and use
`atomic_write_text()` for entity pages, `index.md`, `catalog.md`, and
conversion outputs.

#### P2 - Skill Quality CLI Does Not Load Graph Inputs

The quality score has a graph signal, but the normal CLI path constructs
`SignalSources` without graph index data, so graph connectivity scores can
be zeroed during normal recomputation.

Required fix: load skill/agent graph index data the way MCP quality does
and add recompute tests covering non-zero graph inputs.

### P3 Findings

- Graph JSON edge order can be nondeterministic because set iteration is
  exported without canonical sorting.
- `ctx__wiki_get` accepts only a slug and can return the wrong entity when
  skill/agent/MCP slugs collide.
- Configured/custom wiki paths are inconsistently honored; some modules
  still hard-code `~/.claude/skill-wiki`.
- `install.sh` references stale flat source paths.
- Windows defaults such as `/tmp/stack-profile.json` are not ergonomic.
- README and docs still drift from actual command behavior.

### Recommended Remediation Order

1. **Release blocker batch**:
   - Fix `ctx-init` package module targets and exit codes.
   - Fix hook installer commands.
   - Fix MCP environment isolation.
   - Fix dashboard slug validation and non-localhost exposure.
   - Add wheel smoke tests and publish gating.

2. **Recommendation consistency batch**:
   - Route Claude Code hook suggestions through `recommend_by_tags()`.
   - Decide and document resolver semantics.
   - Add parity tests across hook, public tools, MCP, and scan/resolve.

3. **Graph correctness batch**:
   - Enforce semantic min-cosine on consumer graph loads.
   - Fix incremental semantic affected-node detection.
   - Make dry-run non-mutating.
   - Canonically sort graph export.

4. **Data safety batch**:
   - Harden backup containment.
   - Make wiki writes atomic and locked.
   - Fix sidecar traversal and entity type disambiguation.

5. **Packaging/CI batch**:
   - Include runtime config files in the wheel.
   - Synchronize `ctx.__version__`.
   - Add `ruff`, `mypy`, `pip check`, `twine check`, wheel install, and
     entrypoint smoke tests to CI and publish workflows.

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
