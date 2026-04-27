# ctx Deep Code Review - 2026-04-27

This is the second-opinion, no-sugar-coating review requested after the Claude Code Opus 4.7 findings and the follow-up fixes/static cleanup.

Short answer to "are you sure you did not miss anything?": no. A serious reviewer cannot guarantee full absence of missed bugs in a 733-file repository, especially with a long-running remediation branch and multiple integration surfaces. What this review does provide is a much higher-confidence risk map: nine parallel expert passes plus local re-checks, focused on product architecture, recommendation consistency, harness/runtime behavior, security, data-loss paths, packaging/release, tests, and A-Z user flows.

The "CTO" and "devil advocate" language below means simulated review lenses, not actual company executives. The standard used here is what a senior architecture/release/security review would block before a public release.

## Executive Verdict

ctx has a coherent product idea: a cross-host context/recommendation layer that turns repository/tool signals into skill, agent, and MCP recommendations, then exposes that through Claude Code hooks, a generic harness, a Python API, and an MCP server.

At the start of this review, the codebase was not release-safe. The core problem was not one bug. The product had several partially-migrated architectures active at once:

- Bootstrap called removed flat modules while package entrypoints had moved to canonical package paths.
- Claude Code hooks used their own local recommendation scorer while public/MCP/harness surfaces used a different recommender.
- The generic harness had runtime contract breaks around budget enforcement, evaluator revision ordering, compaction persistence, MCP timeouts, tool approval, and exit semantics.
- Security boundaries were too weak for a system that installs hooks, copies wiki content into live Claude directories, starts MCP subprocesses, exposes a dashboard, and lets models call tools.
- Persistent state writes were not consistently locked or rollback-safe.
- Test coverage was broad but often mocked the exact boundary that breaks in real user flows.
- CI did not gate lint/type/package smoke/release alignment strongly enough.

That recommendation drove the remediation phases below. Do not cut a user-facing release from this branch without the final branch verification and clean wheel gates described in the implementation status.

## Scope And Method

Observed repo state:

- Working directory: `C:\Steves_Files\Work\Research_and_Papers\ctx`
- Tracked files: 733
- Primary language: Python 3.11+
- Key surfaces reviewed: `ctx-init`, install scripts, Claude Code hooks, recommendation engine, graph builder/resolver, generic harness, MCP router/server, monitor dashboard, install/uninstall flows, backup/restore, quality tools, tests, GitHub workflows, docs.
- `rg` was unusable in this shell: `rg.exe` returned "Access is denied". I used scoped PowerShell reads/searches and direct file reads.

Agent coverage:

- OpenAI CTO lens: product architecture, public API, recommendation consistency, harness contracts.
- Anthropic CTO lens: safety, human-in-the-loop, hooks, MCP trust boundaries, prompt/tool risk.
- Google CTO/SRE lens: reliability, determinism, CI, release, packaging, concurrency, portability.
- Devil advocate security team: local/LAN attack paths, symlink/tar poisoning, subprocess execution.
- Devil advocate data-loss team: restore/session/wiki/manifest/cache corruption paths.
- Algorithms team: graph, semantic edges, resolver ranking, dedup, tag backfill, scanner heuristics.
- Harness/runtime team: loop, budgets, evaluator, resume, compaction, MCP routing.
- Release/packaging/CI team: wheel/install/tag/version workflows.
- Test/A-Z team: mocked boundaries, integration gaps, asserted-broken behavior.

Verification commands observed during this review wave or the immediately preceding static cleanup:

- `python -m ruff check src hooks scripts --quiet` passed.
- `python -m mypy --ignore-missing-imports src\ctx src\scan_repo.py` passed for the narrowed package set.
- `python -m compileall -q src hooks scripts` passed.
- `python -m pytest -q` previously passed: `3169 passed, 7 skipped in 381.47s`.
- `python -m pip check` currently fails: `litellm 1.83.14 has requirement click==8.1.8, but you have click 8.3.3`.
- `PYTHONPATH=src python -m inject_hooks --help` fails: `No module named inject_hooks`.
- `PYTHONPATH=src python -m wiki_graphify --help` fails: `No module named wiki_graphify`.
- `PYTHONPATH=src python -m ctx.adapters.claude_code.inject_hooks --help` responds.
- `PYTHONPATH=src python -m ctx.core.wiki.wiki_graphify --help` responds.

## What Was Done Before This Report

Earlier in this workstream, the repo was cleaned to satisfy static checks across many Python files and tests. That cleanup is now part of the remediation branch history rather than an unstaged dirty worktree. Those changes made Ruff, narrowed mypy, compileall, and the full pytest suite pass at that point.

Earlier report work added a current-state audit section to `docs/reports/ctx-review-2026-04-26.md`. That report correctly showed that several previous "fixed" claims were not true in then-current source:

- `src/ctx_init.py` still calls `python -m inject_hooks`.
- `src/ctx_init.py` still calls `python -m wiki_graphify`.
- `docs/knowledge-graph.md` still documents the old `resolve_by_seeds` architecture.
- `docs/skill-quality-install.md` still recommends `python -m wiki_graphify --graph-only`.
- `context_monitor.graph_suggest()` still implements a local recommendation scorer.
- `resolve_skills.py` still uses `resolve_by_seeds()` and normalized scores incorrectly.

This report originally documented the current risk surface and recommended remediation order. The branch now contains follow-up remediation for the recommendation, bootstrap, harness, security, data-loss, packaging, and CI phases; the blocker list below is retained as review evidence, and the implementation status is tracked here so future readers do not mistake old evidence for current source state.

## Post-Review Implementation Status

### Phase 1: Bootstrap and hook install

Status: implemented in this worktree.

What changed:

- `ctx-init --hooks` now uses the packaged Claude Code hook injector module instead of removed flat `inject_hooks`.
- `ctx-init --graph` now uses the packaged graphify module instead of removed flat `wiki_graphify`.
- Explicit hook or graph setup failure now returns non-zero instead of silently completing setup.
- Generated Claude Code hook commands now use packaged module entrypoints and the new packaged lifecycle hook module.

Verification observed:

- Focused bootstrap/hook tests passed: `23 passed`.
- Full suite after Phase 1 passed: `3176 passed, 7 skipped`.

### Phase 2: Recommendation surface convergence

Status: implemented in this worktree.

What changed:

- `recommend_by_tags()` now emits `normalized_score`.
- Claude Code `context_monitor.graph_suggest()` now routes through the shared graph loader and `recommend_by_tags()` instead of its own local scorer.
- Generic harness `ctx__recommend_bundle` now forwards `normalized_score` and delegates free-text tokenization to the shared recommendation tokenizer.
- Resolver graph hits now separate raw score from normalized rank score, scale priority from normalized rank, and carry `entity_type`/`type` on load entries.
- A golden cross-surface test now asserts that direct core, Claude hook, generic harness toolbox, and public Python API recommendations return the same ordered `(name, type)` rows for the same graph and query.

Verification observed:

- Red-first golden tests failed on missing `normalized_score` and missing `entity_type`.
- Focused recommendation/harness/public API/context-monitor/resolver tests passed.
- Full suite after Phase 2 passed: `3178 passed, 7 skipped`.

### Phase 3: Scan recommendation output

Status: implemented in this worktree.

What changed:

- `ctx-scan-repo --recommend` now separates skills and agents using `entity_type` with `type` fallback.
- The skills count no longer includes agent load entries.
- The agents section is always rendered, including an explicit empty state.
- MCP output now shows raw `score` and, when present, `normalized_score`.

Verification observed:

- Red-first scan test failed because current output counted an agent in `-- Skills (2) --` and did not print normalized MCP score.
- Focused scan test suite passed after the fix: `59 passed`.
- Focused Phase 2/3 regression suite passed: `61 passed`.
- Static checks passed: ruff produced no output; mypy reported `Success: no issues found in 59 source files`; compileall completed.
- A monolithic `python -m pytest -q` run timed out after 15 minutes and was stopped. The same test set then passed in two alphabetical shards: `1752 passed, 6 skipped` and `1427 passed, 1 skipped`.

### Phase 4: Terminal response budget enforcement

Status: implemented in this worktree.

What changed:

- `run_loop()` now checks accumulated cost/token budgets before a no-tool provider response can be classified as `completed`, `length`, `empty_response`, or `provider_other`.
- Budget stop detail construction is centralized in `_budget_stop_reason()` so terminal and post-tool budget handling stay consistent.
- Existing post-tool budget behavior is preserved: budget checks still run after tool responses and compaction usage are recorded.

Verification observed:

- Red-first budget tests failed as expected: terminal high-cost and high-token responses returned `completed`.
- Focused budget suite passed after the fix: `4 passed`.
- Focused harness/compaction suite passed: `58 passed`.
- Broader generic harness suite passed: `235 passed`.
- Static checks: `python -m ruff check src` reported `All checks passed!`; `python -m compileall -q src` completed; touched-file mypy with `MYPYPATH=src --namespace-packages --explicit-package-bases` reported `Success: no issues found in 2 source files`.
- Full-repo `python -m mypy src` is not a passing configured check in this repository; it reported 506 pre-existing errors across legacy modules/tests, mostly missing stubs/untyped import boundaries plus unrelated existing type errors.

### Phase 5: Configured mypy gate

Status: implemented in this worktree.

What changed:

- Added a `[tool.mypy]` configuration so `python -m mypy src` is now a deterministic project check for the actively packaged `src/ctx` tree.
- The mypy gate uses the repo's `src` import layout and excludes legacy flat modules plus tests, which are not currently typed as a coherent package boundary.
- Fixed the one real `src/ctx` type error exposed by that boundary: graph recommendation scores in `resolve_skills.py` now coerce optional/malformed numeric values through `_float_or_default()` instead of passing `None` into `float()`.

Verification observed:

- Before this phase, raw `python -m mypy src` reported 506 errors. With explicit `src` layout and missing-import suppression, the unresolved inventory dropped to 72 legacy/test errors; checking `src/ctx` alone exposed one real package error.
- After the fix, `python -m mypy src` reported `Success: no issues found in 58 source files`.
- Focused resolver/recommendation tests passed: `43 passed`.
- Static check passed on touched files: `python -m ruff check pyproject.toml src\ctx\core\resolve\resolve_skills.py` reported `All checks passed!`.

Original remaining type debt outside the configured package gate:

- 72 errors remain if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Largest buckets: `arg-type` 25, `var-annotated` 9, `operator` 8, `misc` 8, `valid-type` 5, `assignment` 5.
- Largest files: `src/tests/test_skill_install.py` 8, `src/tests/test_mcp_canonical_index.py` 7, `src/tests/test_skill_add.py` 7, `src/tests/test_fuzz_yaml_rendering.py` 7, `src/tests/test_harness_planner.py` 5.

### Phase 6: Legacy/test mypy debt slice 1

Status: implemented in this worktree.

What changed:

- Fixed 22 force-check mypy errors across four high-yield test files: `test_skill_install.py`, `test_agent_install.py`, `test_skill_quality_list.py`, and `test_mcp_canonical_index.py`.
- Test helpers now return concrete `argparse.Namespace` / callable types instead of `object`.
- The MCP canonical index lazy importer now advertises the callable it returns instead of `object`.

Verification observed:

- Red targeted mypy check initially reported 22 errors across those four files.
- After the fix, the same four-file force-check reported `Success: no issues found in 4 source files`.
- Focused tests passed: `73 passed, 1 skipped`.
- Static checks passed: `python -m ruff check ...` reported `All checks passed!`; `python -m mypy src` reported `Success: no issues found in 58 source files`; `python -m compileall -q src` completed.

Remaining legacy/test type debt after Phase 6:

- 49 errors remain if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Largest buckets: `arg-type` 11, `misc` 8, `var-annotated` 7, `valid-type` 5, `import-untyped` 5, `assignment` 5, `func-returns-value` 2.
- Largest files: `src/tests/test_skill_add.py` 7, `src/tests/test_fuzz_yaml_rendering.py` 7, `src/tests/test_harness_planner.py` 5, `src/ctx_monitor.py` 3, `src/tests/test_ctx_init.py` 2, `src/catalog_builder.py` 2, `src/batch_convert.py` 2.

### Phase 7: Legacy/test mypy debt slice 2

Status: implemented in this worktree.

What changed:

- Fixed 21 force-check mypy errors across `test_skill_add.py`, `test_fuzz_yaml_rendering.py`, `test_harness_planner.py`, and `test_ctx_init.py`.
- Stubbed module restoration in `test_skill_add.py` now records real module types.
- Planner JSON extraction tests now assert non-`None` parse results before destructuring.
- YAML fuzz tests now use real `Path` annotations and typed Hypothesis category filters.
- `ctx_init` subprocess monkeypatches now use typed helper functions rather than lambdas relying on `list.append()` returning falsey `None`.

Verification observed:

- Red targeted mypy check initially reported 21 errors across those four files.
- After the fix, the same four-file force-check reported `Success: no issues found in 4 source files`.
- Focused tests passed: `111 passed`.
- Static checks passed: `python -m ruff check ...` reported `All checks passed!`; `python -m mypy src` reported `Success: no issues found in 58 source files`; `python -m compileall -q src` completed.

Remaining legacy/test type debt after Phase 7:

- 28 errors remain if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Largest buckets: `var-annotated` 7, `import-untyped` 4, `arg-type` 4, `assignment` 4, `misc` 3.
- Largest files: `src/ctx_monitor.py` 3, `src/tests/test_backup_watchdog.py` 2, `src/catalog_builder.py` 2, `src/batch_convert.py` 2, `src/tests/test_kpi_dashboard.py` 2, `src/tests/test_recommendations.py` 2.

### Phase 8: Legacy/test mypy debt slice 3

Status: implemented in this worktree.

What changed:

- Fixed 8 force-check mypy errors across `catalog_builder.py`, `batch_convert.py`, `test_recommendations.py`, and `test_backup_watchdog.py`.
- Added concrete list/dict annotations where local inference fell back to ambiguous empty containers.
- Replaced `max(..., key=dict.get)` with a lambda that always returns an `int`, avoiding an optional comparator result.
- Replaced watchdog test lambdas with typed fake snapshot functions.

Verification observed:

- Red targeted mypy check initially reported 8 errors across those four files.
- After the fix, the same four-file force-check reported `Success: no issues found in 4 source files`.
- Focused tests passed: `24 passed`.
- Static checks passed: `python -m ruff check ...` reported `All checks passed!`; `python -m mypy src` reported `Success: no issues found in 58 source files`; `python -m compileall -q src` completed.

Remaining legacy/test type debt after Phase 8:

- 20 errors remain if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Largest buckets: `import-untyped` 4, `assignment` 4, `arg-type` 3, `var-annotated` 2.
- Largest files: `src/ctx_monitor.py` 3 and `src/tests/test_kpi_dashboard.py` 2; the rest are single-file/single-error cleanups.

### Phase 9: Legacy/test mypy debt slice 4

Status: implemented in this worktree.

What changed:

- Fixed 7 force-check mypy errors across `ctx_monitor.py`, `test_kpi_dashboard.py`, `versions_catalog.py`, and `toolbox_verdict.py`.
- The monitor sidecar floor rendering now stringifies optional values before HTML escaping.
- The monitor dashboard load helper keeps its heavy dynamic import but suppresses the one known dynamic attribute lookup at file scope.
- Version catalog and toolbox verdict paths now declare tuple/list shapes where mypy could not infer them.
- KPI dashboard import-retention sentinels no longer reuse `_` for different imported class types.

Verification observed:

- Red targeted mypy check initially reported 7 errors across those four files.
- After the fix, the same four-file force-check reported `Success: no issues found in 4 source files`.
- Focused tests passed: `72 passed`.
- Static checks passed: `python -m ruff check ...` reported `All checks passed!`; `python -m mypy src` reported `Success: no issues found in 58 source files`; `python -m compileall -q src` completed.

Remaining legacy/test type debt after Phase 9:

- 13 errors remain if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Remaining buckets: `import-untyped` 4, `arg-type` 3, `assignment` 2, plus four singletons (`union-attr`, `str-bytes-safe`, `var-annotated`, `list-item`).
- Remaining files each have one error: `ctx_lifecycle.py`, `skill_add.py`, `skill_quality.py`, `test_mcp_server.py`, `backup_watchdog.py`, `test_skill_add_yaml_escape.py`, `mcp_add.py`, `test_litellm_provider.py`, `test_mcp_router.py`, `test_toolbox_verdict.py`, `ctx_audit_log.py`, `usage_tracker.py`, and `wiki_batch_entities.py`.

### Phase 10: Legacy/test mypy debt slice 5

Status: implemented in this worktree.

What changed:

- Fixed the four remaining PyYAML import-stub errors in `skill_add.py`, `mcp_add.py`, `wiki_batch_entities.py`, and `test_skill_add_yaml_escape.py`.
- `test_skill_add_yaml_escape.py` now imports `build_entity_page` lazily, which prevents it from pre-loading `skill_add` before `test_skill_add.py` can install its dependency stubs.

Verification observed:

- Red targeted mypy check initially reported 4 errors across those four files.
- After the fix, the same four-file force-check reported `Success: no issues found in 4 source files`.
- Focused tests passed: `55 passed`.
- Static checks passed: `python -m ruff check ...` reported `All checks passed!`; `python -m mypy src` reported `Success: no issues found in 58 source files`; `python -m compileall -q src` completed.

Remaining legacy/test type debt after Phase 10:

- 9 errors remain if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Remaining buckets: `arg-type` 3, `assignment` 2, and one each of `union-attr`, `str-bytes-safe`, `list-item`, `var-annotated`.
- Remaining files: `ctx_lifecycle.py`, `skill_quality.py`, `test_mcp_server.py`, `backup_watchdog.py`, `ctx_audit_log.py`, `test_mcp_router.py`, `test_toolbox_verdict.py`, `usage_tracker.py`, and `test_litellm_provider.py`.

### Phase 11: Legacy/test mypy debt slice 6

Status: implemented in this worktree.

What changed:

- Fixed 4 force-check mypy errors across `ctx_lifecycle.py`, `skill_quality.py`, `backup_watchdog.py`, and `usage_tracker.py`.
- `ctx_lifecycle.py` and `skill_quality.py` now narrow audit-log `subject_type` values to the `Literal["skill", "agent"]` contract before calling `ctx_audit_log.log`.
- `backup_watchdog.py` now types saved signal handlers with the concrete `signal.signal` handler shape instead of round-tripping them as `object`.
- `usage_tracker.py` replaced the untyped regex replacement lambda with a named `re.Match[str]` callback, removing the bytes-safety false positive.

Verification observed:

- Red targeted mypy check initially reported 4 errors across those four files.
- After the fix, the same four-file force-check reported `Success: no issues found in 4 source files`.
- Focused tests passed: `94 passed`.
- Static checks passed: `python -m ruff check ...` reported `All checks passed!`; `python -m mypy src` reported `Success: no issues found in 58 source files`; `python -m compileall -q src` completed.
- One parallel `python -m mypy src` run hit a transient mypy internal error while another mypy process was writing cache state. Two subsequent single-process reruns, including `--show-traceback`, passed.

Remaining legacy/test type debt after Phase 11:

- 5 errors remain if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Remaining buckets: `assignment` 2, plus one each of `union-attr`, `list-item`, and `var-annotated`.
- Remaining files: `test_toolbox_verdict.py`, `test_mcp_router.py`, `test_litellm_provider.py`, `ctx_audit_log.py`, and `test_mcp_server.py`.

### Phase 12: Legacy/test mypy debt slice 7

Status: implemented in this worktree.

What changed:

- Fixed 4 of the 5 remaining force-check mypy errors across `ctx_audit_log.py`, `mcp_router.py`, `test_toolbox_verdict.py`, and `test_litellm_provider.py`.
- `ctx_audit_log.py` now declares audit records as `dict[str, Any]`, matching the optional `meta` object it already writes at runtime.
- `_flatten_content` now accepts `list[Any]`, matching the existing implementation and test that intentionally tolerate malformed/non-dict MCP content blocks.
- `test_toolbox_verdict.py` asserts the precondition returned by `load_verdict` before comparing it, and `test_litellm_provider.py` annotates the intentionally empty response fixture.

Verification observed:

- Red force-check inventory after Phase 11 reported 5 errors.
- Targeted force-check over the four changed sites plus `test_mcp_router.py` reported `Success: no issues found in 5 source files`.
- Focused tests passed after correcting the test filename: `136 passed`.
- Static checks passed: `python -m ruff check ...` reported `All checks passed!`; `python -m mypy src` reported `Success: no issues found in 58 source files`; `python -m compileall -q src` completed.

Remaining legacy/test type debt after Phase 12:

- 1 error remains if legacy flat modules and tests are force-checked with `--ignore-missing-imports`.
- Remaining file: `test_mcp_server.py`.
- Remaining bucket: `assignment`.

### Phase 13: Full mypy gate

Status: implemented in this worktree.

What changed:

- Fixed the final force-check mypy error in `test_mcp_server.py` by annotating JSON-RPC fixture frames as `dict[str, Any]`.
- Removed the temporary `[tool.mypy].exclude` entries from `pyproject.toml`; `python -m mypy src` now checks the package, legacy flat modules, and tests together.

Verification observed:

- `python -m mypy src` reported `Success: no issues found in 234 source files`.
- The old force-check command with `--config-file NUL --namespace-packages --explicit-package-bases --ignore-missing-imports src` reported `Success: no issues found in 234 source files`.
- `python -m ruff check pyproject.toml src\tests\test_mcp_server.py` reported `All checks passed!`.
- `python -m pytest src\tests\test_mcp_server.py -q` reported `32 passed`.
- `python -m compileall -q src` completed.

Remaining legacy/test type debt after Phase 13:

- 0 force-check mypy errors remain under the same gate that initially exposed the legacy/test wall.
- The caveat is no longer "hidden behind 506 errors"; the configured project gate now enforces the full `src` tree.

### Phase 14: MCP timeout and environment hardening

Status: implemented in this worktree.

What changed:

- `McpClient` no longer blocks directly on `stdout.readline()` in the request path. Stdout is drained by a background thread into a queue, and request timeout now applies to waiting for the next frame.
- `McpServerConfig` no longer inherits the full parent environment by default. Child processes receive a small process-plumbing allowlist plus explicit `env` overlays; full inheritance now requires `inherit_env=True`.
- The fake MCP server and router tests now reproduce a server that accepts `tools/call` but never answers, and they verify default secret non-inheritance, explicit env overlays, and opt-in legacy inheritance.

Verification observed:

- `python -m pytest src\tests\test_mcp_router.py -q` reported `41 passed`.
- `python -m ruff check src\ctx\adapters\generic\tools\mcp_router.py src\tests\test_mcp_router.py src\tests\fixtures\fake_mcp_server.py` reported `All checks passed!`.
- `python -m mypy src\ctx\adapters\generic\tools\mcp_router.py src\tests\test_mcp_router.py src\tests\fixtures\fake_mcp_server.py` reported `Success: no issues found in 3 source files`.

### Phase 15: Resume MCP execution gate

Status: implemented in this worktree.

What changed:

- `ctx resume` no longer starts MCP servers reconstructed from mutable session metadata by default.
- Added `--restore-session-mcp` as an explicit opt-in. When used, the CLI prints the restored MCP server names and argv before starting them.
- CLI regression tests now prove that a session containing an executable MCP command is skipped by default and restored only with the explicit flag.

Verification observed:

- `python -m pytest src\tests\test_harness_cli_run.py -q` reported `37 passed`.
- `python -m ruff check src\ctx\cli\run.py src\tests\test_harness_cli_run.py` reported `All checks passed!`.
- `python -m mypy src\ctx\cli\run.py src\tests\test_harness_cli_run.py` reported `Success: no issues found in 2 source files`.

### Phase 16: Session-id overwrite safety

Status: implemented in this worktree.

What changed:

- `SessionStore.create()` now opens new sessions exclusively by default instead of truncating an existing JSONL file.
- Added an explicit `overwrite=True` API path and `ctx run --overwrite-session` CLI flag for intentional replacement.
- `ctx run` now creates the session file before starting MCP subprocesses, so a rejected session-id reuse cannot leak a child process.

Verification observed:

- `python -m pytest src\tests\test_harness_state.py src\tests\test_harness_cli_run.py -q` reported `89 passed`.
- `python -m ruff check src\ctx\adapters\generic\state.py src\ctx\cli\run.py src\tests\test_harness_state.py src\tests\test_harness_cli_run.py` reported `All checks passed!`.
- `python -m mypy src\ctx\adapters\generic\state.py src\ctx\cli\run.py src\tests\test_harness_state.py src\tests\test_harness_cli_run.py` reported `Success: no issues found in 4 source files`.

### Phase 17: Restore rollback safety

Status: implemented in this worktree.

What changed:

- `promote_archived()` now rolls the filesystem move back if lifecycle sidecar persistence fails after restoring an archived skill or agent to the active location.
- Added a regression test that forces `save_lifecycle_state()` to fail and verifies the archived source is restored and the active destination is absent.

Verification observed:

- `python -m pytest src\tests\test_ctx_lifecycle.py -q` reported `40 passed`.
- `python -m ruff check src\ctx_lifecycle.py src\tests\test_ctx_lifecycle.py` reported `All checks passed!`.
- `python -m mypy src\ctx_lifecycle.py src\tests\test_ctx_lifecycle.py` reported `Success: no issues found in 2 source files`.

### Phase 18: Monitor mutation and path boundary

Status: implemented in this worktree.

What changed:

- `ctx-monitor` now generates a per-process mutation token and injects it into dashboard POST requests.
- `/api/load` and `/api/unload` require `X-CTX-Monitor-Token` in addition to the existing same-origin check and JSON body requirement.
- Mutation denial paths now consume the request body before responding, avoiding connection resets on Windows clients.
- Sidecar reads now apply the same safe-slug boundary used by wiki, graph, load, and unload paths.

Verification observed:

- `python -m pytest src\tests\test_ctx_monitor.py -q` reported `37 passed`.
- `python -m ruff check src\ctx_monitor.py src\tests\test_ctx_monitor.py` reported `All checks passed!`.
- `python -m mypy src\ctx_monitor.py src\tests\test_ctx_monitor.py` reported `Success: no issues found in 2 source files`.

### Phase 19: Claude install symlink hardening

Status: implemented in this worktree.

What changed:

- Added shared `safe_copy_file()` install utility that refuses symlinked sources, symlinked destination roots/parents, destination symlink files, and destination paths escaping the configured install root.
- `ctx-skill-install` now uses that helper for `SKILL.md` and reference copies and rejects symlinked converted wiki directories.
- `ctx-agent-install` now uses the same helper for agent body installation.
- Regression tests cover both skill destination directory symlink write-through and agent destination file symlink overwrite attempts.

Verification observed:

- `python -m pytest src\tests\test_skill_install.py -q` reported `31 passed`.
- `python -m ruff check src\ctx\adapters\claude_code\install\install_utils.py src\ctx\adapters\claude_code\install\skill_install.py src\ctx\adapters\claude_code\install\agent_install.py src\tests\test_skill_install.py` reported `All checks passed!`.
- `python -m mypy src\ctx\adapters\claude_code\install\install_utils.py src\ctx\adapters\claude_code\install\skill_install.py src\ctx\adapters\claude_code\install\agent_install.py src\tests\test_skill_install.py` reported `Success: no issues found in 4 source files`.

### Phase 20: Wiki sync symlink hardening

Status: implemented in this worktree.

What changed:

- `wiki_sync` now refuses to write through symlinked wiki roots, required directories, seed files, raw scan outputs, entity pages, index, log, usage, and stale-marker paths.
- Regression tests cover a symlinked wiki root and a symlinked `index.md` seed file.

Verification observed:

- `python -m pytest src\tests\test_wiki_sync.py -q` reported `111 passed`.
- `python -m ruff check src\ctx\core\wiki\wiki_sync.py src\tests\test_wiki_sync.py` reported `All checks passed!`.
- `python -m mypy src\ctx\core\wiki\wiki_sync.py src\tests\test_wiki_sync.py` reported `Success: no issues found in 2 source files`.

### Phase 21: Source-add symlink hardening

Status: implemented in this worktree.

What changed:

- Flat `skill_add.install_skill()` and `agent_add.install_agent()` now use the shared guarded copy helper rather than raw `shutil.copy2`.
- Regression tests cover symlinked source rejection and agent destination symlink overwrite rejection.

Verification observed:

- `python -m pytest src\tests\test_skill_add.py -q` reported `51 passed`.
- `python -m ruff check src\skill_add.py src\agent_add.py src\tests\test_skill_add.py` reported `All checks passed!`.
- `python -m mypy src\skill_add.py src\agent_add.py src\tests\test_skill_add.py` reported `Success: no issues found in 3 source files`.

### Phase 22: Tar member hardening

Status: implemented in this worktree.

What changed:

- `update_repo_stats` now accepts only exact normalized `graphify-out/graph.json` and `graphify-out/communities.json` archive members instead of suffix matches.
- Archive names are normalized and rejected if they are absolute, Windows-drive absolute, empty, dotted, or contain `..` path components.
- Entity counts only include safe regular Markdown files under the expected `entities/skills/`, `entities/agents/`, and `entities/mcp-servers/` prefixes.
- JSON archive members must be regular files and must stay under an explicit uncompressed size cap.
- Regression tests cover safe archive reads, suffix impersonation, non-regular JSON members, and oversized JSON members.

Verification observed:

- `python -m pytest src\tests\test_update_repo_stats.py -q` reported `4 passed`.
- `python -m ruff check src\update_repo_stats.py src\tests\test_update_repo_stats.py` reported `All checks passed!`.
- `python -m compileall -q src\update_repo_stats.py src\tests\test_update_repo_stats.py` completed.
- `python -m mypy src\update_repo_stats.py src\tests\test_update_repo_stats.py` reported `Success: no issues found in 2 source files`.
- `python -m mypy src` reported `Success: no issues found in 235 source files`.

### Phase 23: CI and release quality gates

Status: implemented in this worktree.

What changed:

- The main test workflow now runs Ruff, full configured mypy, and `pip check` before pytest in the OS/Python matrix.
- The test workflow now includes a clean wheel smoke job that builds distributions, runs `twine check`, installs the wheel into a fresh virtualenv, runs `pip check`, validates `ctx.__version__` against installed metadata, loads all `ctx` console script entrypoints, and runs representative CLI help commands.
- The no-test-no-merge workflow's documented `no-tests-needed` label exemption is now implemented instead of only mentioned in the error text.
- The publish workflow now validates that a pushed release tag matches `pyproject.toml` version using normalized Python package versions.
- The publish workflow now runs `twine check` and the same clean wheel install/entrypoint smoke before uploading publishable artifacts.
- `ctx.__version__` now matches the current package version, and the new smoke gates fail if it drifts from installed metadata.

Verification observed:

- `python -m ruff check src hooks scripts` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 235 source files`.
- `python -m compileall -q src\ctx\__init__.py` completed.
- Workflow YAML parse probe reported `workflow yaml parsed`.
- `PYTHONPATH=src python -c "import ctx; ..."` reported source `ctx.__version__` as `0.6.4`.
- Clean dev virtualenv `pip install ".[dev]"` followed by `python -m pip check` reported `No broken requirements found`.
- Clean wheel virtualenv smoke reported `twine check` passed for the wheel, `pip check` reported `No broken requirements found`, `loaded 27 ctx console scripts from wheel 0.6.4`, and representative CLI help commands returned success.
- The shared review environment still reports the pre-existing global `litellm`/`click` conflict on `python -m pip check`; the new gates were verified in clean virtualenvs.

### Phase 24: Harness tool policy hook

Status: implemented in this worktree; CLI wiring follows in Phase 25.

What changed:

- `run_loop()` now accepts a pre-dispatch `tool_policy` callback for every model-requested tool call.
- A policy callback returns `None` to allow the call or a denial reason string to block it before router/executor dispatch.
- Policy callback exceptions fail closed and stop the loop with `tool_denied`.
- `LoopResult.stop_reason` now distinguishes `tool_denied` from actual `tool_error`.
- `run_with_evaluation()` now forwards the same policy hook into generator rounds, so evaluator/planner mode cannot bypass the loop policy.
- Regression tests cover policy denial before executor invocation and fail-closed policy exceptions.

Verification observed:

- `python -m pytest src\tests\test_harness_loop.py src\tests\test_harness_evaluator.py -q` reported `88 passed`.
- `python -m ruff check src\ctx\adapters\generic\loop.py src\ctx\adapters\generic\evaluator.py src\tests\test_harness_loop.py` reported `All checks passed!`.
- `python -m compileall -q src\ctx\adapters\generic\loop.py src\ctx\adapters\generic\evaluator.py src\tests\test_harness_loop.py` completed.
- `python -m mypy src\ctx\adapters\generic\loop.py src\ctx\adapters\generic\evaluator.py src\tests\test_harness_loop.py` reported `Success: no issues found in 3 source files`.
- `python -m mypy src` reported `Success: no issues found in 235 source files`.

### Phase 25: CLI tool policy wiring

Status: implemented in this worktree.

What changed:

- `ctx run` and `ctx resume` now accept repeatable `--allow-tool PATTERN` and `--deny-tool PATTERN` options.
- Tool patterns use exact/glob matching against model-visible tool names such as `ctx__wiki_get` or `filesystem__read_file`.
- Deny patterns override allow patterns. If allow patterns are present, calls that match no allow pattern are blocked before execution.
- New sessions persist the effective tool policy in session metadata.
- Resume inherits recorded tool policy from the session and can add stricter allow/deny patterns for the resumed turn.
- CLI exit code `2` now covers both execution errors and policy-denied tool calls.
- Focused CLI tests cover direct policy matching, metadata persistence, run-time denial, and resume-time inherited denial.

Verification observed:

- `python -m pytest src\tests\test_harness_cli_run.py -q` reported `44 passed`.
- `python -m ruff check src\ctx\cli\run.py src\tests\test_harness_cli_run.py` reported `All checks passed!`.
- `python -m compileall -q src\ctx\cli\run.py src\tests\test_harness_cli_run.py` completed.
- `python -m mypy src\ctx\cli\run.py src\tests\test_harness_cli_run.py` reported `Success: no issues found in 2 source files`.
- `python -m mypy src` reported `Success: no issues found in 235 source files`.

### Phase 26: Final report reconciliation

Status: implemented in this worktree.

What changed:

- This report now clearly separates original review evidence from current remediation status.
- Stale "current source" and "dirty worktree" wording was replaced with branch/history wording.
- Remaining caveats were narrowed to live-host and exhaustive integration checks that were not proven locally.
- The blocker summary now treats fixed items as fixed evidence, not as still-open defects.

Verification observed:

- Docs-only edit; final branch verification is recorded in the merge handoff rather than claimed here.

### Phase 27: MCP subprocess test environment

Status: implemented in this worktree.

What changed:

- Full-suite verification exposed that the MCP subprocess round-trip tests invoked `python -m ctx.mcp_server.server` without an explicit source-tree `PYTHONPATH`.
- This failure was caused by the intentional Phase 14 security hardening that stopped MCP subprocesses from inheriting the full parent environment.
- The tests now pass only the required `src/` path through the explicit MCP env overlay, preserving the production no-secret-inheritance behavior.

Verification observed:

- `python -m pytest src\tests\test_mcp_server.py -q` reported `32 passed`.
- `python -m ruff check src\tests\test_mcp_server.py` reported `All checks passed!`.
- `python -m mypy src\tests\test_mcp_server.py` reported `Success: no issues found in 1 source file`.
- `python -m compileall -q src\tests\test_mcp_server.py` completed.

### Phase 28: Final branch verification

Status: completed before merge.

Verification observed:

- `python -m ruff check src hooks scripts` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 235 source files`.
- `python -m compileall -q src hooks scripts` completed.
- `python -m pytest -q` reported `3212 passed, 7 skipped in 431.94s`.

### Phase 29: Clean-host contract harness

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- Added `scripts/clean_host_contract.py`, which builds the current wheel, installs it into a fresh virtualenv, redirects user/config/cache state to a temp root, creates a tiny repo, and runs the installed console scripts through the core A-Z path.
- Added a GitHub Actions workflow for manual/weekly clean-host contract execution.
- Added focused unit coverage for the contract runner's environment isolation, path safety, fake LiteLLM module, venv script resolution, and command sequencing.
- Added `docs/harness/clean-host-contract.md` and a phase plan for the remaining release-hardening work.

Verification observed:

- `python -m pytest src\tests\test_clean_host_contract.py -q` reported `6 passed`.
- `python -m ruff check src hooks scripts` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 236 source files`.
- `python -m compileall -q src hooks scripts` completed.
- `python scripts\clean_host_contract.py --fast` built and installed `claude_ctx-0.6.4-py3-none-any.whl`, ran `ctx-init --hooks`, `ctx-scan-repo --recommend`, `ctx run`, `ctx resume`, and a denied-tool policy run, then reported `clean-host contract passed`.
- `python -m pytest -q` reported `3218 passed, 7 skipped in 448.61s`.

### Phase 30: Monitor SSE concurrency and route safety

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- `ctx-monitor` now serves through a daemon-threaded `ThreadingHTTPServer` wrapper instead of a single-request `HTTPServer`, so one open `/api/events.stream` client cannot block `/api/sessions.json` or other dashboard routes.
- Monitor shutdown now signals open SSE workers through a per-server event instead of relying only on daemon-thread cleanup.
- Monitor slug validation now delegates to the shared safe-name validator, including Windows reserved-name rejection such as `con.txt`, `nul.`, and `LPT9.ini`.
- `/wiki/<slug>` now resolves sharded MCP wiki pages using the canonical `entities/mcp-servers/<first-char-or-0-9>/<slug>.md` route.
- `/graph?slug=<slug>` and `/api/graph/<slug>.json` now support MCP graph nodes with the `mcp-server:` prefix.
- `docs/dashboard.md` now documents threaded SSE behavior, three-type wiki/graph routing, MCP unload routing, token-gated mutations, and shared slug safety.

Verification observed:

- Red-first monitor regression run initially reported `2 failed, 1 passed`; failures were the missing threaded server factory and missing sharded MCP wiki route.
- Additional red-first checks showed the missing SSE shutdown signal and missing MCP graph focus support.
- `python -m pytest src\tests\test_ctx_monitor.py::test_monitor_sse_stream_does_not_block_json_requests src\tests\test_ctx_monitor.py::test_monitor_shutdown_signals_open_sse_workers src\tests\test_ctx_monitor.py::test_graph_neighborhood_supports_mcp_nodes src\tests\test_ctx_monitor.py::test_monitor_slug_validator_rejects_windows_reserved_names src\tests\test_ctx_monitor_3type.py::TestWikiIndexEntries::test_wiki_entity_path_resolves_sharded_mcp_pages -q` reported `8 passed`.
- `python -m pytest src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_3type.py src\tests\test_safe_name.py -q` reported `142 passed, 1 skipped`.
- `python -m ruff check src\ctx_monitor.py src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_3type.py` reported `All checks passed!`.
- `python -m mypy src\ctx_monitor.py src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_3type.py` reported `Success: no issues found in 3 source files`.
- `python -m mypy src` reported `Success: no issues found in 236 source files`.
- `python scripts\clean_host_contract.py --fast` reported `clean-host contract passed`.
- `python -m pytest -q` reported `3229 passed, 7 skipped in 403.51s`.

### Phase 31: Wiki search three-type coverage

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- `ctx.core.wiki.wiki_query.load_all_pages()` now loads all three wiki entity types: flat skill pages, flat agent pages, and recursively sharded MCP server pages.
- Loaded wiki pages now carry `entity_type`, canonical `wikilink`, and `description` metadata, so search results are no longer anonymous slugs.
- MCP page loading validates both the slug and canonical shard path, including numeric `0-9` shards, and skips unsafe or mis-sharded files.
- Keyword search now scores slug, title, description, tags, and body. Installed/use-count boosts now apply only after an actual query-field match, so widening the corpus does not leak unrelated installed pages into results.
- `ctx__wiki_search` now returns `entity_type`, `wikilink`, and `description` for each hit.
- `ctx__wiki_get` now accepts an optional `entity_type` argument from search results, which disambiguates duplicate skill/agent/MCP slugs instead of silently resolving by hardcoded candidate order.

Verification observed:

- Red-first query tests initially failed because loaded pages had no `entity_type`, no canonical `wikilink`, and agent/MCP pages were invisible.
- Red-first follow-up tests initially failed because title/description-only pages did not score and typed `ctx__wiki_get` did not disambiguate duplicate slugs.
- `python -m pytest src\tests\test_query.py src\tests\test_harness_ctx_core.py src\tests\test_public_api.py src\tests\test_mcp_server.py -q` reported `113 passed`.
- `python -m ruff check src\ctx\core\wiki\wiki_query.py src\ctx\adapters\generic\ctx_core_tools.py src\tests\test_query.py src\tests\test_harness_ctx_core.py` reported `All checks passed!`.
- `python -m mypy src\ctx\core\wiki\wiki_query.py src\ctx\adapters\generic\ctx_core_tools.py src\tests\test_query.py src\tests\test_harness_ctx_core.py` reported `Success: no issues found in 4 source files`.
- `python -m ruff check src hooks scripts` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 236 source files`.
- `python -m compileall -q src hooks scripts` completed.
- `python scripts\clean_host_contract.py --fast` built and installed `claude_ctx-0.6.4-py3-none-any.whl`, ran the installed A-Z clean-host contract, and reported `clean-host contract passed`.
- `python -m pytest -q` reported `3235 passed, 7 skipped in 449.20s`.

### Phase 32: Clean-host fake Claude hook smoke

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- The clean-host contract now writes a deterministic fake Claude Code host that reads the isolated `settings.json` generated by `ctx-init --hooks`.
- The fake host executes generated PostToolUse and Stop hook command strings from the installed wheel with representative stdin payloads, without calling Anthropic APIs.
- The contract now fails if any generated hook command exits non-zero or if the expected hook modules are absent from the generated settings.
- The main `Tests` workflow now runs `python scripts/clean_host_contract.py --fast` on push and pull request, while the standalone clean-host workflow remains available for manual and weekly scheduled runs.
- `docs/harness/clean-host-contract.md` now distinguishes fake-host hook execution coverage from the remaining manual live-Claude-Code host gate.

Verification observed:

- Red-first local clean-host run failed when the fake host used `shell=True` on Windows: generated hook commands are POSIX-quoted, and `cmd.exe` rejected the single-quoted Python path. The fake host was corrected to parse the generated command with `shlex.split()` and execute argv directly, which is deterministic for the generated module commands on Windows and Linux.
- `claude --version` reported `2.1.119 (Claude Code)`. A bounded real `claude -p` probe is not a reliable automated release check because it can consume quota and, in discovery, hit `error_max_budget_usd` before producing a deterministic response.
- `python -m pytest src\tests\test_clean_host_contract.py -q` reported `8 passed`.
- `python -m ruff check scripts\clean_host_contract.py src\tests\test_clean_host_contract.py` reported `All checks passed!`.
- `python -m mypy scripts\clean_host_contract.py src\tests\test_clean_host_contract.py` reported `Success: no issues found in 2 source files`.
- `python scripts\clean_host_contract.py --fast` built and installed `claude_ctx-0.6.4-py3-none-any.whl`, executed five generated hook commands through the fake host with `failed: 0`, ran the rest of the installed clean-host contract, and reported `clean-host contract passed`.
- Workflow YAML parsing for `.github/workflows/test.yml` and `.github/workflows/clean-host-contract.yml` succeeded.
- `python -m ruff check src hooks scripts` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 236 source files`.
- `python -m compileall -q src hooks scripts` completed successfully.
- `git diff --check` reported no whitespace errors.
- `python -m pytest -q` reported `3237 passed, 7 skipped in 433.15s`.

### Phase 33: Post-migration docs audit cleanup

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- `docs/knowledge-graph.md` no longer names the removed flat `src/wiki_graphify.py` path as the graph build implementation.
- The knowledge-graph recommendation section now distinguishes the shared free-text recommendation engine from repository scan resolution instead of describing the old seed-walk path as the whole product behavior.
- `docs/skill-quality-install.md` now points graph rebuild troubleshooting at the supported `ctx-wiki-graphify` console script.
- `docs/roadmap/skill-quality.md` now points on-demand quality recomputation at `ctx-skill-quality` instead of `python src/skill_quality.py`.

Verification observed:

- Targeted stale-doc search across the three edited docs found no remaining `python -m wiki_graphify`, `python -m inject_hooks`, `src/wiki_graphify.py`, `resolve_by_seeds`, current-behavior `graph neighbor`, or `python src/skill_quality.py` references.
- `python -m ruff check .` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 236 source files`.
- `git diff --check` reported no whitespace errors.
- `python -m pytest -q` reported `3237 passed, 7 skipped in 440.79s`.

### Phase 34: Locked manifest transactions

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- `record_install()` and `record_uninstall()` now serialize the whole manifest read-modify-write transaction with `file_lock(MANIFEST_PATH)`.
- Atomic writes are still used for the final save, but the lock now prevents lost updates between concurrent installer processes.
- `src/tests/test_install_utils.py` now has subprocess race regressions for concurrent distinct installs and concurrent distinct uninstalls. The workers intentionally slow the save point to expose the original bug deterministically.

Verification observed:

- Red test: `python -m pytest src\tests\test_install_utils.py -q -k "parallel_installs or parallel_uninstalls"` failed before the production change. Concurrent installs collapsed to one loaded entry, and concurrent uninstalls left seven loaded entries behind.
- Green test: the same command reported `2 passed, 59 deselected`.
- `python -m pytest src\tests\test_install_utils.py src\tests\test_skill_install.py src\tests\test_agent_install.py src\tests\test_mcp_install.py -q` reported `194 passed`.
- `python -m ruff check src\ctx\adapters\claude_code\install\install_utils.py src\tests\test_install_utils.py` reported `All checks passed!`.
- `python -m mypy src\ctx\adapters\claude_code\install\install_utils.py src\tests\test_install_utils.py` reported `Success: no issues found in 2 source files`.
- `python -m ruff check .` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 236 source files`.
- `python -m compileall -q src hooks scripts` completed successfully.
- `git diff --check` reported no whitespace errors.
- `python -m pytest -q` reported `3239 passed, 7 skipped in 392.77s`.

### Phase 35: Opt-in live MCP compatibility gate

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- Added `--run-live-mcp` and repeatable `--live-mcp-config PATH` pytest options.
- Added `src/tests/test_mcp_live_compat.py`, an integration-marked gate that is skipped by default and runs only when explicitly enabled with trusted local config JSON.
- The live gate builds `McpServerConfig` with argv-form `command` / `args`, explicit env overlay, optional env inheritance, startup/request timeouts, expected tool assertions, and one optional safe probe call.
- `docs/harness/attaching-to-hosts.md` now documents the trust boundary, config shape, argv/no-shell behavior, env inheritance caveat, and `${tmp_path}` placeholder for safe filesystem probes.

Verification observed:

- Red test: `python -m pytest src\tests\test_mcp_live_compat.py -q --run-live-mcp` failed before registering the pytest option with `unrecognized arguments: --run-live-mcp`.
- `python -m pytest src\tests\test_mcp_live_compat.py -q` reported `1 skipped`.
- `python -m ruff check src\tests\conftest.py src\tests\test_mcp_live_compat.py` reported `All checks passed!`.
- `python -m mypy src\tests\conftest.py src\tests\test_mcp_live_compat.py` reported `Success: no issues found in 2 source files`.
- First fake-config live run exposed a Windows UTF-8 BOM parsing issue from PowerShell-authored JSON; the config reader now uses `utf-8-sig`.
- `python -m pytest src\tests\test_mcp_live_compat.py -q --run-live-mcp --live-mcp-config <fake-mcp-config>` reported `1 passed` against the repo's fake MCP subprocess.
- `python -m pytest src\tests\test_mcp_router.py src\tests\test_harness_cli_run.py -q` reported `85 passed`.
- `python -m ruff check .` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 237 source files`.
- `python -m compileall -q src hooks scripts` completed successfully.
- `git diff --check` reported no whitespace errors.
- `python -m pytest -q` reported `3239 passed, 8 skipped in 419.62s`.

### Phase 36: Browser-driven monitor security coverage

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- Added a `browser` optional dependency extra for Python Playwright and registered a `browser` pytest marker.
- Added `src/tests/test_ctx_monitor_browser.py`, which starts a real local `ctx-monitor` server and drives Chromium through Playwright.
- Browser coverage now proves `/loaded` injects a token that controls mutation requests, missing-token browser POSTs fail, cross-origin browser POSTs cannot mutate state, traversal slugs are rejected through browser-executed fetch, and two live `EventSource` streams do not block a concurrent JSON request.

Verification observed:

- `python -m pytest src\tests\test_ctx_monitor_browser.py -q` reported `4 passed`.
- `python -m ruff check pyproject.toml src\tests\test_ctx_monitor_browser.py` reported `All checks passed!`.
- `python -m mypy src\tests\test_ctx_monitor_browser.py` reported `Success: no issues found in 1 source file`.
- `python -m pytest src\tests\test_ctx_monitor_browser.py src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_3type.py -q` reported `71 passed`.
- `python -m ruff check pyproject.toml src\tests\test_ctx_monitor_browser.py src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_3type.py` reported `All checks passed!`.
- `python -m mypy src\tests\test_ctx_monitor_browser.py src\ctx_monitor.py` reported `Success: no issues found in 2 source files`.
- `python -m ruff check .` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 238 source files`.
- `python -m compileall -q src hooks scripts` completed successfully.
- `git diff --check` reported no whitespace errors.
- `python -m pytest -q` reported `3243 passed, 8 skipped in 410.96s`.

### Phase 37: Opt-in live Claude host gate

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- Extended `scripts/clean_host_contract.py` with `--run-live-claude`, `--live-claude-max-budget-usd`, and `--claude-bin`.
- The live path is unreachable unless `CTX_LIVE_CLAUDE_ACK=uses_quota` is present, and the budget must be greater than zero and no more than 1 USD.
- The live path runs non-spending `claude --version` and `claude auth status` preflights before constructing the quota-consuming `claude -p` command.
- The live prompt is budget-capped, streamed with hook events, uses an argv command list, disables session persistence, and allows only `Bash(python --version)`.
- Hook execution proof no longer depends on model stdout: the contract appends temporary PostToolUse and Stop sentinel hooks to the isolated `settings.json` and requires both records in `live-claude-hooks.jsonl` under the temp root.
- The live host environment is narrowed to platform plumbing plus explicit provider/auth variables while keeping home/config/cache redirected to the temp root.
- The default fake-host path strips caller `PYTHONPATH`, so a developer shell cannot accidentally mask wheel packaging problems by importing source-tree modules.
- `docs/harness/clean-host-contract.md` now documents the manual live-host command, auth assumptions, budget acknowledgement, and sentinel artifact.

Verification observed:

- Red-first `python -m pytest src\tests\test_clean_host_contract.py -q` failed before implementation because the new live-gate symbols did not exist.
- `claude --help` showed local support for `--settings`, `--setting-sources`, `--output-format stream-json`, `--include-hook-events`, and `--max-budget-usd`.
- `python -m pytest src\tests\test_clean_host_contract.py -q` reported `14 passed`.
- `python -m ruff check scripts\clean_host_contract.py src\tests\test_clean_host_contract.py` reported `All checks passed!`.
- `python -m mypy scripts\clean_host_contract.py src\tests\test_clean_host_contract.py` reported `Success: no issues found in 2 source files`.
- `python scripts\clean_host_contract.py --fast` built and installed `claude_ctx-0.6.4-py3-none-any.whl`, executed five generated hook commands through the fake host with `failed: 0`, ran the rest of the installed clean-host contract, and reported `clean-host contract passed`.
- `python -m ruff check .` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 238 source files`.
- `python -m compileall -q src hooks scripts` completed successfully.
- `git diff --check` reported no whitespace errors.
- `python -m pytest -q` reported `3249 passed, 8 skipped in 456.87s`.

### Phase 38: Durable wiki and atomic write hardening

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- `atomic_write_text` and `atomic_write_bytes` now flush and `fsync` temp-file contents before replace, then best-effort `fsync` the parent directory after replace.
- `wiki_sync` now routes seed-file writes, raw scan writes, skill page create/update, index updates, log appends, usage updates, and stale marking through the shared atomic writers.
- Wiki read-modify-write paths now use `file_lock` for the target file so concurrent writers that use the ctx helpers do not clobber each other.
- Log appends are now implemented as locked read-plus-atomic-replace instead of direct append, preserving whole-file atomic visibility.
- Added red-first regressions for temp/parent fsync ordering and for preserving original wiki files when atomic writes fail.

Verification observed:

- Red-first `python -m pytest src\tests\test_fs_utils.py src\tests\test_wiki_sync.py -q` failed in the expected places: missing temp fsync, missing parent fsync, non-atomic `save_scan`, non-atomic index update, non-atomic log append, and non-atomic usage write.
- `python -m pytest src\tests\test_fs_utils.py src\tests\test_wiki_sync.py -q` reported `131 passed`.
- `python -m ruff check src\ctx\utils\_fs_utils.py src\ctx\core\wiki\wiki_sync.py src\tests\test_fs_utils.py src\tests\test_wiki_sync.py` reported `All checks passed!`.
- `python -m mypy src\ctx\utils\_fs_utils.py src\ctx\core\wiki\wiki_sync.py src\tests\test_fs_utils.py src\tests\test_wiki_sync.py` reported `Success: no issues found in 4 source files`.
- `python -m pytest src\tests\test_harness_state.py src\tests\test_ctx_lifecycle.py src\tests\test_backup_mirror.py -q` reported `133 passed, 1 skipped`.
- `python scripts\clean_host_contract.py --fast` built and installed `claude_ctx-0.6.4-py3-none-any.whl`, executed the installed clean-host flow, and reported `clean-host contract passed`.
- `python -m ruff check .` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 238 source files`.
- `python -m compileall -q src hooks scripts` completed successfully.
- `git diff --check` reported no whitespace errors.
- `python -m pytest -q` reported `3254 passed, 8 skipped in 436.91s`.

### Phase 39: Dashboard manifest transaction hardening

Status: implemented in branch `codex/current-next-steps-hardening`, commit `e558a5e`.

What changed:

- `skill_loader.update_manifest()` now wraps the whole dashboard load read-dedup-write sequence in `file_lock(MANIFEST_PATH)`.
- `skill_unload.unload_from_session()` now wraps the whole dashboard unload read-filter-write sequence in `file_lock(MANIFEST_PATH)`.
- Dashboard unload now forwards `entity_type` to `skill_unload`, so unloading an agent no longer removes a same-slug skill from mixed-type manifests.
- `unload_from_session()` keeps backward-compatible slug-wide behavior when called without `entity_type`, preserving existing CLI semantics while allowing typed dashboard calls.
- Added subprocess race regressions for concurrent dashboard loads and unloads, with intentionally delayed save points to expose the previous lost-update bug.
- Added a same-slug skill/agent regression for dashboard agent unload.

Verification observed:

- Red-first `python -m pytest src\tests\test_skill_loader.py -q` failed before implementation with two failures: concurrent dashboard loads left only one surviving entry, and concurrent dashboard unloads left most entries loaded.
- After implementation, `python -m pytest src\tests\test_skill_loader.py src\tests\test_skill_unload.py src\tests\test_ctx_monitor_3type.py -q` reported `62 passed`.
- `python -m ruff check src\ctx\adapters\claude_code\skill_loader.py src\ctx\adapters\claude_code\install\skill_unload.py src\ctx_monitor.py src\tests\test_skill_loader.py src\tests\test_ctx_monitor_3type.py` reported `All checks passed!`.
- `python -m mypy src\ctx\adapters\claude_code\skill_loader.py src\ctx\adapters\claude_code\install\skill_unload.py src\ctx_monitor.py src\tests\test_skill_loader.py src\tests\test_ctx_monitor_3type.py` reported `Success: no issues found in 5 source files` with existing notes about unchecked bodies in untyped test functions.
- `git diff --check` reported no whitespace errors, only existing CRLF conversion warnings.

### Phase 40: Typed wiki sync for mixed manifests

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- `wiki_sync.ensure_wiki()` now creates `entities/agents/` alongside skills, plugins, and MCP server roots.
- Manifest entries are routed by `entity_type`: `skill` -> `entities/skills`, `agent` -> `entities/agents`, `mcp-server` -> sharded `entities/mcp-servers/<shard>/`.
- `upsert_skill_page()` remains backward compatible for old callers, but now accepts `subject_type` for typed entity pages and writes the correct frontmatter `type`.
- MCP wiki pages use `command` as the path-like frontmatter value when a file path is not present.
- Index updates are grouped by subject type, so skill, agent, plugin, and MCP links go into their own sections.
- The sync log and console summary now say entities rather than skills when counting mixed manifests.

Verification observed:

- Red-first `python -m pytest src\tests\test_wiki_sync.py::TestMain::test_full_sync_routes_mixed_manifest_entries_by_entity_type -q` failed before implementation because the agent page was not created under `entities/agents/`.
- After implementation, `python -m pytest src\tests\test_wiki_sync.py -q` reported `116 passed`.
- `python -m ruff check src\ctx\core\wiki\wiki_sync.py src\tests\test_wiki_sync.py` reported `All checks passed!`.
- `python -m mypy src\ctx\core\wiki\wiki_sync.py src\tests\test_wiki_sync.py` reported `Success: no issues found in 2 source files` with the existing note about unchecked bodies in an untyped function.

### Phase 41: Strict live MCP `inherit_env` parsing

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- The opt-in live MCP compatibility harness now treats `inherit_env` as a strict optional JSON boolean.
- Omitted `inherit_env` still defaults to `False`.
- Literal JSON `false` and `true` are accepted.
- Strings, numbers, and null are rejected instead of being coerced with Python truthiness.

Verification observed:

- Red-first `python -m pytest src\tests\test_mcp_live_compat.py -q` failed before implementation because `"false"`, `"true"`, `0`, `1`, and `null` did not raise.
- After implementation, `python -m pytest src\tests\test_mcp_live_compat.py -q` reported `8 passed, 1 skipped`.
- `python -m ruff check src\tests\test_mcp_live_compat.py` reported `All checks passed!`.
- `python -m mypy src\tests\test_mcp_live_compat.py` reported `Success: no issues found in 1 source file`.

### Phase 42: Browser CI and SSE race hardening

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- The main GitHub Actions test matrix now runs `pytest -m "not browser"` explicitly, so browser ownership is no longer hidden behind import skips.
- Added a dedicated Ubuntu `browser-security` job that installs `.[dev,browser]`, installs Playwright Chromium with system dependencies, and runs `src/tests/test_ctx_monitor_browser.py` under the `browser` marker.
- The browser SSE test now creates the audit file before opening streams and waits for both EventSource connections to fire `onopen` before writing the single audit event.
- Removed the fixed sleep that could let slow CI connect after the audit write and then tail from EOF.

Verification observed:

- `python -m pytest -q --no-cov src\tests\test_ctx_monitor_browser.py -rs` reported `4 passed`.
- `python -m pytest -q --no-cov src\tests\test_ctx_monitor.py::test_monitor_sse_stream_does_not_block_json_requests src\tests\test_ctx_monitor.py::test_monitor_shutdown_signals_open_sse_workers -rs` reported `2 passed`.
- `python -m ruff check src\tests\test_ctx_monitor_browser.py` reported `All checks passed!`.
- `python -m mypy src\tests\test_ctx_monitor_browser.py` reported `Success: no issues found in 1 source file`.
- `python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/test.yml').read_text()); print('workflow yaml parsed')"` reported `workflow yaml parsed`.

### Phase 43: Release metadata and changelog readiness

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- Bumped `pyproject.toml` package metadata from `0.6.4` to `0.7.0`.
- Bumped `ctx.__version__` from `0.6.4` to `0.7.0`.
- Added a fresh empty `[Unreleased]` bucket to `CHANGELOG.md`.
- Added a consolidated `0.7.0` changelog summary covering the hardening, harness, recommendation/wiki, CI, and release-gate work.
- Converted the old top-level `[Unreleased]` MCP phase headings to `0.7.0` material so the changelog no longer advertises those historical phase notes as unreleased.

Verification observed:

- A version parity probe printed `0.7.0 0.7.0` after reading both `pyproject.toml` and `src/ctx/__init__.py`.
- `python -m pytest src\tests\test_package_scaffold.py src\tests\test_public_api.py -q` reported `51 passed`.
- `python -m ruff check src\ctx\__init__.py` reported `All checks passed!`.
- `python -m mypy src\ctx\__init__.py` reported `Success: no issues found in 1 source file`.
- `Select-String -Path 'CHANGELOG.md' -Pattern '^## \[Unreleased\]' | Measure-Object | Select-Object -ExpandProperty Count` reported `1`.
- `git diff --check` reported no whitespace errors, only existing CRLF conversion warnings.

### Phase 44: Modern package license metadata

Status: implemented in branch `codex/current-next-steps-hardening`.

What changed:

- Changed `pyproject.toml` from deprecated `license = { text = "MIT" }` to the SPDX string form `license = "MIT"`.
- Raised the build backend floor from `setuptools>=42` to `setuptools>=77`, matching the Setuptools version family that supports the modern license field.
- Added the package metadata cleanup to the `0.7.0` changelog.

Verification observed:

- `python -m pytest src\tests\test_package_scaffold.py src\tests\test_public_api.py -q` reported `51 passed`.
- A clean package build captured stdout/stderr, found no `SetuptoolsDeprecationWarning` or `project.license` warning, and then `python -m twine check <temp>\*` reported `PASSED` for both `claude_ctx-0.7.0` artifacts.

### Final verification after Phase 43

Status: completed on branch `codex/current-next-steps-hardening`.

Observed:

- `git status --short --branch` reported `## codex/current-next-steps-hardening`.
- `python scripts\clean_host_contract.py --fast` built `claude_ctx-0.7.0-py3-none-any.whl`, installed it into an isolated venv, executed five generated hook commands through the fake host with `failed: 0`, exercised `ctx run`, `ctx resume`, and denied-tool policy, and ended with `clean-host contract passed`.
- `python -m ruff check .` reported `All checks passed!`.
- `python -m mypy src` reported `Success: no issues found in 238 source files` with existing notes about unchecked bodies in untyped functions.
- `python -m compileall -q src hooks scripts` completed successfully.
- `git diff --check` completed successfully.
- `python -m pytest -q` reported `3266 passed, 8 skipped in 419.08s (0:06:59)`.
- Package dry-run in a temporary dist/venv completed successfully:
  - `python -m build --outdir <temp>` built `claude_ctx-0.7.0.tar.gz` and `claude_ctx-0.7.0-py3-none-any.whl`.
  - `python -m twine check <temp>\*` reported `PASSED` for both artifacts.
  - Fresh venv wheel install succeeded.
  - `python -m pip check` in the fresh venv reported `No broken requirements found.`
  - Entry point probe reported `loaded 27 ctx console scripts from wheel 0.7.0`.
  - Installed `ctx-init --help`, `ctx-scan-repo --help`, `ctx-wiki-graphify --help`, and `ctx --help` all executed.

Follow-up:

- Phase 44 removed the Setuptools `project.license` deprecation warning observed in this dry-run.

## Blocker Summary

P0/P1 blockers I would not ship over. Items 1-14 now have direct remediation implemented in the current branch. Item 15 is mitigated by clean wheel/entrypoint smoke, targeted CLI policy tests, and the MCP subprocess source-tree round-trip regression fix in Phase 27, while live third-party host execution remains an out-of-scope integration caveat. The list is retained to show the original review basis and keep the risk map auditable. The mypy caveat has been resolved in phases: Phase 5 defined the package gate, Phases 6-12 reduced the force-checked legacy/test debt from 72 to 1 error, and Phase 13 moved the configured gate to the full `src` tree with zero mypy errors.

1. `ctx-init --hooks/--graph` invokes removed modules and still exits success. Fixed in Phase 1.
2. Installed Claude hooks point at files not shipped in the wheel and use non-portable shell commands. Fixed in Phase 1.
3. Recommendation ranking is still split between the shared recommender and Claude Code hooks. Fixed in Phase 2.
4. Harness budget caps are bypassed on terminal model responses. Fixed in Phase 4; retained as original review evidence.
5. MCP request timeouts can hang forever on blocking stdout reads. Fixed in Phase 14.
6. MCP subprocesses inherit all parent secrets by default. Fixed in Phase 14.
7. Model tool calls execute without an approval/policy gate. Fixed across library/evaluator paths in Phase 24 and CLI run/resume paths in Phase 25.
8. Resume trusts session metadata as executable MCP config. Fixed in Phase 15.
9. Session ID reuse truncates existing JSONL transcripts. Fixed in Phase 16.
10. Restore overwrites live state without a rollback snapshot. Fixed in Phase 17.
11. Monitor dashboard has unauthenticated mutation endpoints and path traversal risks. Fixed in Phase 18.
12. Wiki/install flows follow symlinks from wiki content into live Claude directories. Fixed across install copy paths in Phase 19 and wiki write paths in Phase 20.
13. Tar extraction and source install paths are stale/unsafe. Source install paths fixed in Phase 21; tar member hardening fixed in Phase 22.
14. CI/release can publish a tag without tests/package smoke/version alignment. Fixed in Phase 23.
15. Tests mocked the exact command boundaries that were originally broken. Mitigated by the clean wheel/entrypoint smoke in Phase 23 and CLI policy regression tests in Phase 25; live third-party MCP host execution remains out of scope for local CI.

## Product Intent As Understood

ctx is trying to be a model-agnostic "context operating layer":

- Maintain a skill/agent/MCP wiki and graph.
- Detect a repository or tool-use context.
- Recommend a bundle of skills, agents, and MCP servers.
- Surface that bundle consistently through:
  - Claude Code hooks and dashboard.
  - Generic `ctx run` harness.
  - Python library API.
  - Standalone MCP server.
- Track use, quality, lifecycle, and backups.

The product promise only works if three invariants hold:

1. Same context produces the same recommendations across every surface.
2. Harness execution is resumable, bounded, observable, and safe.
3. Installed/user-state mutations are reversible, locked, and auditable.

The original reviewed source violated all three invariants. Phases 1-44 closed the original P0/P1 blocker list plus the newly surfaced wiki type-sync blocker and the first release-hardening slices in the current branch, with final local verification now green. The remaining caveats are live-host execution, live third-party MCP validation, exhaustive process-kill crash-consistency scenarios, and tagging only after merge.

## P0 Findings

### P0-1: Bootstrap invokes missing modules and exits success

Evidence:

- `src/ctx_init.py:117-130` builds `cmd = [sys.executable, "-m", "inject_hooks", ...]`.
- `src/ctx_init.py:147-157` builds `[sys.executable, "-m", "wiki_graphify"]`.
- `pyproject.toml:34` defines `ctx-install-hooks = "ctx.adapters.claude_code.inject_hooks:main"`.
- `pyproject.toml:55` defines `ctx-wiki-graphify = "ctx.core.wiki.wiki_graphify:main"`.
- Probe: `PYTHONPATH=src python -m inject_hooks --help` fails.
- Probe: `PYTHONPATH=src python -m wiki_graphify --help` fails.

Impact:

A fresh user can run `ctx-init --hooks --graph`, see warnings mixed into setup output, and still get process exit 0. The hooks and graph are not actually installed/built. That breaks the top of the A-Z user journey.

Why this happened:

The codebase is mid-migration from flat scripts to package modules. Entrypoints were updated, but internal subprocess calls and docs were not.

Required fix:

- Replace removed flat module calls with canonical package modules or console scripts.
- Make explicitly requested optional setup steps fail non-zero when they fail.
- Add clean-install integration tests that do not mock `subprocess.run`.

Recommended tests:

- Temp HOME + wheel install + `ctx-init --hooks`, assert generated hook settings exist.
- Temp HOME + wheel install + `ctx-init --graph`, assert graph command starts or produces expected dry-run output.
- `ctx-init --hooks` with forced subprocess failure returns non-zero.

### P0-2: Installed hooks point at unshipped/moved files

Evidence:

- `src/ctx/adapters/claude_code/inject_hooks.py:40-64` generates:
  - `python3 {ctx_dir}/context_monitor.py`
  - `python3 {ctx_dir}/usage_tracker.py`
  - `python3 {ctx_dir}/../hooks/quality_on_session_end.py`
  - `python3 {ctx_dir}/skill_add_detector.py`
  - `python3 {ctx_dir}/skill_suggest.py`
  - `python3 {ctx_dir}/../hooks/backup_on_change.py`
- `src/ctx_init.py:133-141` resolves `ctx_src_dir` to the directory containing `ctx_init.py`, i.e. the package/source layout after install.
- `pyproject.toml:172+` packages the `ctx` tree, not the repo-root `hooks/` directory.

Impact:

On a wheel/PyPI install, injected Claude Code hooks can point to files that do not exist. Errors are also suppressed with `2>/dev/null || true`, so the failure can be invisible.

Why this is severe:

The Claude Code hook path is the flagship product experience. If it is broken, recommendations, backups, quality updates, and usage tracking can all silently fail.

Required fix:

- Generate hooks using `sys.executable -m ctx.adapters.claude_code.hooks.<module>` style commands where possible.
- Package or relocate hook scripts that must remain external.
- Stop swallowing all hook errors; write structured diagnostics to an audit log.
- Add cross-platform hook command smoke tests.

### P0-3: Harness budget limits are bypassed by final answers

Current status:

Implemented in Phase 4. `run_loop()` now checks `_budget_stop_reason()` inside the terminal no-tool response path before any successful completion classification, and the same helper is reused for post-tool/compaction budget checks.

Evidence:

- `src/ctx/adapters/generic/loop.py:271` adds provider usage.
- `src/ctx/adapters/generic/loop.py:297-316` breaks immediately when the response has no tool calls.
- `src/ctx/adapters/generic/loop.py:376-391` performs budget checks only after tool execution and compaction.
- Runtime reviewer probe observed `stop_reason=completed` with usage above `budget_tokens` and `budget_usd`.

Impact:

A single expensive final provider response can exceed budget and still be reported as completed. This breaks cost safety and any automation using `ctx run` as a bounded executor.

Required fix:

- Check budget immediately after every provider or compactor usage update, before any terminal break.
- Distinguish "completed but over budget" from "budget stop" explicitly.

Recommended tests:

- First provider response has no tool calls, large usage, `budget_tokens=1`: expect `token_budget`.
- First provider response has no tool calls, high cost, `budget_usd=0.01`: expect `cost_budget`.

## P1 Findings

### P1-1: Recommendation surfaces still diverge

Evidence:

- Shared recommender lives in `src/ctx/core/resolve/recommendations.py`.
- Public toolbox uses it at `src/ctx/adapters/generic/ctx_core_tools.py:271-286`.
- Claude Code hook has a separate scorer at `src/ctx/adapters/claude_code/hooks/context_monitor.py:225-271`.
- Hook coverage also treats loaded skill names by exact match only at `context_monitor.py:162-188`.

Impact:

The same user intent can produce different recommendations depending on entrypoint:

- MCP/Python/harness path: `recommend_by_tags()`.
- Claude Code hook path: local graph scorer with substring, tag overlap, degree tiebreak.
- Resolver path: `resolve_by_seeds()` plus static stack matrix.

This violates the product contract that recommendations are consistent across MCP, library, harness, and hooks.

Required fix:

- Create one recommendation service API.
- Delete hook-local scoring.
- Route hook, scan, resolver, MCP, and Python API through the same engine.
- Add cross-surface golden tests: same synthetic graph + same query/signals produce same ranked names and types.

### P1-2: Graph recommendation priority collapses normalized scores

Evidence:

- `src/ctx/core/resolve/resolve_skills.py:264-271` uses `normalized_score` when present.
- `src/ctx/core/resolve/resolve_skills.py:306-310` computes `priority = 3 + min(int(score), 12)` and confidence as `0.6 + score / 20.0`.

Impact:

If `score` is normalized in `[0, 1]`, `int(score)` is almost always `0`. High-quality graph hits get priority 3 and confidence around 0.6. Under `max_skills`, strong graph recommendations can lose to weak static detections.

Required fix:

- Separate threshold score from ranking score from priority score.
- Use calibrated priority bands, raw rank, or percentile.
- Add cap-competition tests where a high normalized graph hit must survive over lower-confidence static detections.

### P1-3: Agent recommendations remain structurally weak in resolver flow

Evidence:

- `resolve_skills.py:303-305` uses the skill/agent path only if `name in available`.
- The available map is derived from installed skill files, not a first-class agent catalog in the same way.
- `resolve_skills.py:291-301` special-cases MCPs into `manifest["mcp_servers"]`; agents do not get an equivalent explicit manifest bucket/type path.

Impact:

Agent recommendations can be present in hook bundles but not reliably emitted by resolver/scan flows. The result is not just ranking divergence; whole entity types are inconsistently reachable.

Required fix:

- Make recommendation results typed: `skill`, `agent`, `mcp-server`.
- Make scan/resolve manifests typed.
- Add tests showing scan -> resolve emits an agent recommendation when the graph recommends an agent.

### P1-4: Long-lived recommenders cache stale graph data forever

Evidence:

- `src/ctx/adapters/generic/ctx_core_tools.py:407-413` loads graph once and returns `self._graph` forever.
- The public API and MCP server can keep long-lived toolbox instances.

Impact:

After `ctx-wiki-graphify`, dedup cleanup, tag backfill, or wiki changes, `ctx__recommend_bundle` and `ctx__graph_query` can serve stale recommendations until process restart.

Required fix:

- Track graph file mtime/hash and invalidate cache.
- Expose explicit refresh.
- Add test: modify graph.json after first query, assert second query sees new node without process restart.

### P1-5: Query-time semantic min-cosine is documented but not applied

Evidence:

- `filter_graph_by_min_cosine()` exists and documents query-time filtering.
- `ctx_core_tools.py:264-276` loads raw graph and passes it directly to `recommend_by_tags()`.
- Resolver also uses raw graph walks.

Impact:

Operators can configure stricter semantic thresholds, but recommendation surfaces may continue using lower build-floor semantic edges.

Required fix:

- Apply query-time edge filtering in all graph consumers.
- Add test graph with low-cosine semantic-only edge; configure higher query threshold; assert it is excluded.

### P1-6: MCP request timeout can hang forever

Evidence:

- `src/ctx/adapters/generic/tools/mcp_router.py:270-287` computes deadline before reading.
- `src/ctx/adapters/generic/tools/mcp_router.py:335-338` calls blocking `stdout.readline()` with no timeout.

Impact:

An MCP server that accepts a request and never responds can wedge the harness indefinitely. Budget and cancellation cannot interrupt a thread blocked in `readline()`.

Required fix:

- Read frames using a worker thread/queue with timeout, nonblocking IO, or async subprocess.
- On timeout, terminate/restart the MCP process.
- Add fake MCP test that initializes then stalls on `tools/list`.

### P1-7: MCP subprocesses inherit all parent secrets

Evidence:

- `mcp_router.py:119-128` uses `env = os.environ.copy(); env.update(config.env)` and passes that to `subprocess.Popen`.

Impact:

Third-party MCP servers inherit API keys, GitHub tokens, and other local secrets from the parent process unless the user has manually scrubbed the environment.

Required fix:

- Default to a minimal environment.
- Add explicit env allowlist.
- Redact env in logs/session metadata.
- Test with sentinel parent secret and fake MCP that prints env.

### P1-8: Model tool calls execute without human approval

Evidence:

- `src/ctx/adapters/generic/loop.py:318-336` executes every returned tool call immediately.
- There is no approval callback, policy object, dry-run gate, or dangerous-tool classification.

Impact:

Once filesystem, git, shell-like MCPs, or arbitrary MCP tools are attached, model output can directly mutate local state. This is not acceptable for a generic long-running harness.

Required fix:

- Add a tool policy layer before execution.
- Default to approval for mutating tools.
- Provide explicit `--unsafe-auto-approve-tools` or equivalent.
- Log approval decisions.

### P1-9: Resume trusts session metadata as executable MCP config

Evidence:

- `src/ctx/cli/run.py:705-737` reconstructs MCP router from session metadata on resume.
- Session JSONL is local user-writable state.

Impact:

A tampered session file can turn `ctx resume` into arbitrary subprocess launch.

Required fix:

- Treat session tool metadata as untrusted.
- Require explicit re-approval for MCP configs on resume.
- Store signed/trusted session metadata or only references to named trusted configs.

### P1-10: Resume loses provider connection settings

Evidence:

- Fresh runs accept `--base-url` and `--api-key-env`.
- `src/ctx/cli/run.py:697-699` reconstructs provider on resume with only model and inferred API key env.

Impact:

Resuming a custom OpenAI-compatible endpoint, local vLLM/Ollama-compatible gateway, or nonstandard key environment can silently switch backend or fail auth.

Required fix:

- Persist provider connection metadata.
- Rehydrate it on resume.
- Treat connection metadata as sensitive/trusted config.

### P1-11: Evaluator revision rounds corrupt conversation order

Evidence:

- Runtime reviewer found `run_with_evaluation()` passes prior messages to a new `run_loop()`.
- `run_loop()` defaults to building `[system, task, prior messages]` unless `append_task_after_messages=True`.

Impact:

Revision prompts can appear before the answer they are meant to revise, and a second system message can appear mid-conversation. Evaluator-guided improvement is therefore not reliably evaluating/revising the intended transcript.

Required fix:

- For evaluator revision rounds, append the revision task after existing messages.
- Avoid duplicating system messages mid-thread.
- Add transcript-order tests.

### P1-12: Compacted state is not persisted for resume

Evidence:

- `loop.py:353-371` replaces `conversation[:]` with compacted messages in memory.
- The JSONL observer records model/tool messages as they happen, but there is no compaction event that rewrites the replay state.
- Runtime reviewer probe observed live result compacted but `load_session()` replay un-compacted.

Impact:

Long sessions compact in memory but resume from the old un-compacted transcript. This defeats context-management guarantees and can resurrect large/obsolete content.

Required fix:

- Persist compaction events or checkpointed conversation snapshots.
- Make `load_session()` replay compaction events.
- Add observer + compactor + load_session integration test.

### P1-13: Session ID reuse truncates prior transcripts

Evidence:

- `src/ctx/cli/run.py:341-343` exposes `--session-id`.
- `src/ctx/adapters/generic/state.py:200-216` opens fresh sessions with mode `"w"`.

Impact:

Running with an existing pinned session ID destroys prior JSONL history.

Required fix:

- Use exclusive create for new sessions.
- Add explicit `--overwrite-session` if needed.
- Lock around create/append/resume operations.

### P1-14: Restore overwrites live state without rollback point

Evidence:

- `src/backup_mirror.py:542-575` verifies a snapshot, then copies over live targets.
- There is no automatic pre-restore snapshot, global restore lock, or rollback manifest.

Impact:

A wrong snapshot or mid-restore crash can leave the live Claude tree mixed and difficult to recover.

Required fix:

- Take automatic pre-restore snapshot.
- Restore under lock.
- Write transaction/rollback manifest.
- Add crash simulation tests.

### P1-15: Manifest read-modify-write paths bypass file locks

Evidence:

- `src/ctx/adapters/claude_code/install/install_utils.py:98-120` loads manifest, mutates, and saves.
- Other code has a `file_lock()` pattern, but this path does not use it.

Impact:

Concurrent load/unload/health hooks can lose each other's entries.

Required fix:

- Wrap all manifest mutations in one shared locked transaction helper.
- Add concurrent install/uninstall tests.

### P1-16: Wiki sync overwrites pages without serialization

Evidence:

- `src/ctx/core/wiki/wiki_sync.py:214-235` reads existing page text, edits fields, writes text back.
- No file lock or compare-and-swap.

Impact:

Concurrent sync/catalog/quality/manual edits can drop counters, status, quality fields, links, or log entries.

Required fix:

- Use locked per-page updates.
- Prefer structured frontmatter parsing and targeted updates.
- Add parallel writer test.

### P1-17: Monitor SSE endpoint can monopolize the server

Evidence:

- `src/ctx_monitor.py:1804-1807` uses single-threaded `HTTPServer`.
- `src/ctx_monitor.py:1778-1796` loops forever for SSE clients.

Impact:

One open `/api/events.stream` connection can block later dashboard/API requests.

Required fix:

- Use `ThreadingHTTPServer`.
- Add client caps and cleanup.
- Test concurrent SSE + normal GET.

### P1-18: Dashboard path traversal can disclose JSON outside sidecar directory

Evidence:

- `src/ctx_monitor.py:150-155` builds `_sidecar_dir() / f"{slug}.json"`.
- `src/ctx_monitor.py:1661-1667` passes path segment from `/api/skill/<slug>.json` directly to `_load_sidecar()`.

Impact:

If path normalization allows traversal through URL path segments, local/LAN clients can request JSON outside the sidecar directory.

Required fix:

- Decode and validate slug with `_SAFE_SLUG_RE`.
- Resolve path and enforce containment.
- Add traversal tests for `/api/skill/../...`.

### P1-19: Dashboard mutation endpoints are unauthenticated

Evidence:

- `src/ctx_monitor.py:1608-1616` accepts no-Origin POSTs.
- `src/ctx_monitor.py:1532-1560` exposes load/unload operations.
- `src/ctx_monitor.py:1827-1829` allows binding to `0.0.0.0`.

Impact:

If exposed beyond localhost, any LAN client can POST with no Origin and trigger load/unload operations.

Required fix:

- Require a random session token for mutation endpoints.
- Deny mutation endpoints unless bound to loopback unless explicit unsafe flag is set.
- Add tests for no-Origin LAN-style requests.

### P1-20: Wiki reads and installers follow symlinks

Evidence:

- `src/ctx/adapters/generic/ctx_core_tools.py:381-383` reads first matching wiki file.
- `src/ctx_monitor.py:958-960` resolves wiki entity path and renders it.
- `src/ctx/adapters/claude_code/install/skill_install.py:206-208` copies `SKILL.md`.
- `src/ctx/adapters/claude_code/install/agent_install.py:117-118` copies mirrored agent file.

Impact:

A poisoned wiki containing symlinks can cause secret files to be read through tools/dashboard or copied into live Claude skills/agents.

Required fix:

- Reject symlinks in wiki entity/body/reference sources.
- Enforce resolved-path containment.
- Sanitize tar extraction.
- Add symlink poisoning tests.

### P1-21: Installer extracts tarball without member validation

Evidence:

- `install.sh:48-52` runs `tar xzf "$WIKI_ARCHIVE" -C "$WIKI_DIR/"`.
- `.githooks/pre-commit:129-132` repacks wiki tarball from local wiki content.

Impact:

Malicious tar members or symlinks can poison the wiki tree.

Required fix:

- Validate tar members before extraction.
- Reject absolute paths, `..`, symlinks, hardlinks, devices.
- Extract into temp dir, validate, then swap.

## P2 Findings

### P2-1: CLI exits success on incomplete or blocked runs

Evidence:

- `src/ctx/cli/run.py:845-851` returns non-zero only for `tool_error`.

Impact:

`max_iterations`, `length`, `empty_response`, budget stops, content filter, cancellation, and provider anomalies can all exit 0. Automation may treat incomplete runs as successful.

Required fix:

- Map non-completed stop reasons to non-zero exit codes unless an explicit `--allow-incomplete-exit-zero` is set.

### P2-2: CLI discards evaluator/planner total cost accounting

Evidence:

- Runtime reviewer found `run_with_evaluation()` computes `total_usage`, but `_cmd_run` emits only final loop usage.

Impact:

Evaluator/planner mode underreports token and cost usage.

Required fix:

- Emit both final usage and total orchestration usage.
- Enforce budgets against total usage.

### P2-3: Evaluator accepts failed criteria as pass

Evidence:

- Evaluator parsing coerces `bool(item.get("passed"))`, so `"false"` becomes true.
- Top-level pass verdict can override failed criteria.

Impact:

Evaluator mode can approve outputs that explicitly failed criteria.

Required fix:

- Strictly parse booleans.
- If any criterion fails, downgrade top-level pass.
- Add malformed/inconsistent evaluator JSON tests.

### P2-4: Untrusted graph metadata is injected into Claude context

Evidence:

- `bundle_orchestrator` renders suggestion names, scores, and tags into additional context.
- These values originate from wiki/graph metadata.

Impact:

Malicious wiki metadata can become prompt-injection text.

Required fix:

- Escape and quote metadata.
- Label it as untrusted data.
- Add malicious label/tag tests.

### P2-5: Hook failures are intentionally hidden

Evidence:

- Generated hook commands use `2>/dev/null || true`.

Impact:

Broken hooks look successful. This is especially damaging now that hook paths are stale.

Required fix:

- Log failures to `~/.claude/ctx-hook-errors.jsonl` or equivalent.
- Keep Claude hook non-blocking, but do not erase diagnostics.

### P2-6: Backup scope omits important persistent state

Evidence:

- `src/config.json:130-143` backs up top files plus `agents` and `skills`.
- It omits wiki, graph outputs, quality sidecars, audit logs, skill events, and `~/.ctx/sessions`.

Impact:

Backup/restore cannot recover key ctx state stores.

Required fix:

- Define backup profiles: user Claude state, ctx operational state, all.
- Include sessions/audit/wiki/quality outputs or explicitly document exclusions.

### P2-7: Quality hook marks failures as processed

Evidence:

- `hooks/quality_on_session_end.py:193-194` invokes recompute then writes state unconditionally.

Impact:

If recompute fails, `last_run_at` still advances. Failed slugs may be skipped until another event.

Required fix:

- Only advance state for successful recompute.
- Persist failed slugs for retry.

### P2-8: Graph cache uses a shared temp filename without a lock

Evidence:

- `src/ctx/core/graph/semantic_edges.py:309-317` always writes `embeddings.tmp.npz`.

Impact:

Concurrent graph builds in the same cache dir can race and leave mismatched cache/state.

Required fix:

- Use unique temp files and a cache lock.
- Atomically update embeddings and top-k state together.

### P2-9: Atomic writes are not crash-durable

Evidence:

- `src/ctx/utils/_fs_utils.py:52-55` writes, closes, chmods, and replaces, but does not fsync file or directory.

Impact:

This gives atomic visibility but not durable crash consistency. Power loss can still lose just-replaced files.

Required fix:

- Add durable write option for manifests/session-critical state.
- fsync temp file before replace and parent dir after replace where supported.

### P2-10: Incremental graph patch misses changed existing nodes

Evidence:

- `src/ctx/core/wiki/wiki_graphify.py:497-514` affected set includes new nodes and nodes with removed neighbors.
- Comment says content-hash changes are marked, but implementation returns before doing that.

Impact:

Edited tags/body on existing entities can leave stale incident edges until a full rebuild.

Required fix:

- Thread changed IDs from semantic-edge state or compute content hashes before state refresh.
- Add incremental graph test: edit existing node tags/body, assert incident edges update.

### P2-11: Semantic top-K state is not true per-row top-K

Evidence:

- `src/ctx/core/graph/semantic_edges.py:700-706` rebuilds per-node top-K from canonical undirected pairs, appending pairs to both endpoints.

Impact:

Incremental reuse can differ from full rebuild behavior, especially when only one endpoint would rank a pair.

Required fix:

- Persist true row-wise top-K from the matrix computation.
- Add incremental vs full rebuild equivalence tests.

### P2-12: Dedup incremental state misses body/content changes

Evidence:

- `src/ctx/core/quality/dedup_check.py:490-506` only optimizes when some IDs changed.
- Agent reported state hash uses node ID, description, tags, but not embedded content/body.

Impact:

Body-only changes can be treated as unchanged. No-op runs can still pay full pairwise cost.

Required fix:

- Include embedded text/content hash in dedup state.
- Carry forward prior findings on true no-op runs.

### P2-13: Tag backfill ignores requested wiki root

Evidence:

- `src/ctx/core/quality/tag_backfill.py:370-431` accepts `wiki_dir`, but scans `Path.home() / ".claude" / "skills"` and `agents`.

Impact:

Running against a test or alternate wiki can affect/report the user's live home catalog instead of the supplied wiki.

Required fix:

- Derive source roots from `wiki_dir` or explicit CLI roots.
- Add temp wiki test ensuring home is untouched.

### P2-14: Conflict resolution and graph ties are nondeterministic

Evidence:

- `resolve_skills.py:339-365` uses set intersection and `max()` with only priority as key.
- Equal priorities can depend on hash/set order.

Impact:

Manifests/recommendation order can flip between runs or Python hash seeds.

Required fix:

- Use deterministic tiebreakers: priority, confidence, slug.
- Add `PYTHONHASHSEED` reproducibility tests.

### P2-15: Public wiki API fallback is split-brain

Evidence:

- `ctx.api.default_wiki_dir()` promises fallback to `~/.claude/skill-wiki`.
- `ctx_core_tools.py:427-434` returns `None` if `ctx_config` cannot be imported.

Impact:

Library callers can see `default_wiki_dir()` resolve while `wiki_search()`/`wiki_get()` return empty/error through the default toolbox.

Required fix:

- Share one fallback resolver across API and toolbox.

### P2-16: Source install script still calls removed flat scripts

Evidence:

- `install.sh` calls stale flat paths such as `src/wiki_sync.py`, `src/inject_hooks.py`, and `src/wiki_graphify.py` according to release reviewer.
- Local read confirmed stale direct `src/inject_hooks.py` path at `install.sh:95-97`.

Impact:

Source install can fail or install broken hooks.

Required fix:

- Rewrite install.sh to use console scripts or package module paths.
- Add source install smoke test on Linux.

### P2-17: Publish workflow is not release-safe

Evidence:

- `.github/workflows/publish.yml` publishes on `v*` tags.
- Release reviewer found it builds/upload without running the full test/lint/package smoke/version preflight.
- `.github/workflows/test.yml` runs tests on push/PR to main, not necessarily tag publish.

Impact:

A bad tag can publish a broken package or mismatched version.

Required fix:

- Publish workflow should depend on test/lint/type/package smoke.
- Compare tag version to `pyproject.toml`.
- Run `twine check`, `pip check`, and wheel console entrypoint import smoke.

### P2-18: Runtime version metadata is inconsistent

Evidence:

- `pyproject.toml:7` says `0.6.4`.
- `src/ctx/__init__.py:36` says `__version__ = "0.1.0-alpha"`.
- Release reviewer found MCP server reports `0.1.0`.

Impact:

Support/debug output and MCP client metadata are misleading. Version/tag release checks cannot be trusted.

Required fix:

- Source version from package metadata.
- Add test: `ctx.__version__ == importlib.metadata.version("claude-ctx")`.
- Add MCP initialize version test.

### P2-19: CI does not enforce type/lint/package policy strongly enough

Evidence:

- `.github/workflows/test.yml:30-42` installs dev deps and runs pytest/coverage.
- It does not run Ruff, mypy, wheel build, pip check, or entrypoint smoke.
- `python -m pip check` currently fails for local env dependency conflict.

Impact:

Regressions that static checks catch can still merge/publish.

Required fix:

- Add CI jobs:
  - `ruff check . --no-cache`
  - `ruff format --check`
  - narrowed mypy or explicit typed target policy
  - build wheel/sdist
  - install wheel in clean venv
  - smoke every console entrypoint with `--help`
  - `pip check`

### P2-20: Tests mock the broken ctx-init boundary

Evidence:

- Test/A-Z reviewer found `src/tests/test_ctx_init.py:56-70` mocks `subprocess.run`.
- These tests pass while real `python -m inject_hooks` and `python -m wiki_graphify` fail.

Impact:

The test suite can pass while the real bootstrap is broken.

Required fix:

- Add integration tests without subprocess mocking.
- Keep unit tests for command construction, but also assert constructed modules are importable.

### P2-21: Resume missing-session test asserts crash behavior

Evidence:

- Test/A-Z reviewer found `src/tests/test_harness_cli_run.py:479-486` expects `FileNotFoundError`.
- Probe observed CLI resume missing session exits with traceback.

Impact:

User typo produces Python traceback instead of clean CLI error.

Required fix:

- Catch missing session in CLI.
- Return exit 1 with concise message.
- Replace test expectation.

### P2-22: A-Z alive-loop excludes MCP approval

Evidence:

- `src/tests/test_alive_loop_e2e.py:39-45` explicitly excludes MCP install.
- MCP install tests mock subprocess.

Impact:

The "A-Z" flow does not prove a recommended MCP can be approved and installed.

Required fix:

- Add fake `claude` executable on PATH.
- Extend A-Z to approve MCP recommendation and assert command shape/status/manifest.

### P2-23: Scan-to-recommend handoff is not tested

Evidence:

- Test/A-Z reviewer found scan tests stop after scan/detect.
- Resolver tests use synthetic profiles/available skills.

Impact:

`ctx-scan-repo` can emit a stack but no installable recommendation path may work.

Required fix:

- Temp FastAPI repo -> scan -> resolve -> install recommended skill -> assert manifest/filesystem state.

### P2-24: MCP quality tests skip product import failure

Evidence:

- Test/A-Z reviewer found `src/tests/test_mcp_quality.py:25-37` catches `ImportError` and skips suite.

Impact:

A broken shipped console script can be reported as skipped.

Required fix:

- Do not skip product import failure.
- Only skip optional external/network dependencies.

### P2-25: Docs still describe old graph/recommendation architecture

Evidence:

- `docs/knowledge-graph.md:122-126` says resolver seeds `resolve_by_seeds(G, matched_slugs)` and uses `edge weight >= 1.5`.
- `docs/skill-quality-install.md:170-171` tells users to run `python -m wiki_graphify --graph-only`.

Impact:

Docs lead users and future agents back toward stale architecture and broken commands.

Required fix:

- Update docs after code is fixed.
- Prefer docs that name console scripts: `ctx-wiki-graphify`.

## P3 Findings And Quality Debt

### P3-1: Repo scan monorepo detection depends on filesystem order

Evidence:

- Algorithms reviewer found `scan_repo.py:220-226` overwrites `pkg_json` for every package.json encountered and checks only the last one.

Impact:

Monorepo detection can flip depending on traversal order.

Required fix:

- Treat root `package.json` as authoritative for workspace detection.
- Sort traversal or explicitly select root.

### P3-2: Graph quality score ignores current edge scale

Evidence:

- `quality_signals.py` edge strength bonus only applies above 1.0 while current normalized weights are effectively `[0, 1]`.

Impact:

Quality scoring becomes mostly degree-based, favoring generic highly-connected nodes.

Required fix:

- Recalibrate quality score to current edge weight scale.

### P3-3: MCP source merges can lose concurrent additions

Evidence:

- `src/mcp_add.py:299-338` reads page, merges `sources`, and writes whole page.

Impact:

Parallel ingests for the same entity can drop source additions.

Required fix:

- Lock or transactional update per entity.

## A-Z User Flow Assessment

The intended A-Z flow currently fails or is insufficiently proven at multiple handoffs:

1. Install package:
   - Risk: wheel/source install can contain stale hook/script paths.
   - Missing proof: clean wheel smoke for all console scripts.

2. Run `ctx-init`:
   - Current failure: `--hooks` and `--graph` call missing modules.
   - Current bad behavior: warnings but exit 0.

3. Generate graph:
   - Risk: incremental graph misses changed existing nodes.
   - Risk: semantic top-K incremental state can diverge from full rebuild.

4. Scan repo:
   - Risk: scan output is not proven to drive resolver/install.
   - Risk: monorepo detection order dependence.

5. Recommend bundle:
   - Current failure: multiple recommenders disagree.
   - Risk: graph priority collapse suppresses good recommendations.
   - Risk: stale graph cache.

6. Present bundle in Claude Code:
   - Current failure: hook commands may not run after install.
   - Risk: untrusted graph metadata injected into prompt context.

7. Approve/load skill/agent:
   - Risk: exact-match coverage means loaded `fastapi-pro` may not satisfy `fastapi` signal.
   - Risk: manifest writes can race.

8. Approve/install MCP:
   - Missing proof: A-Z explicitly excludes MCP install.
   - Risk: MCP commands inherit secrets and are installed via permissive launcher commands.

9. Run generic harness:
   - Current failure: budget caps skipped on final response.
   - Risk: no approval gate for tool calls.
   - Risk: MCP timeout hangs forever.

10. Resume harness:
   - Risk: provider settings lost.
   - Risk: MCP config from session metadata is executable.
   - Risk: compaction not persisted.

11. Monitor dashboard:
   - Risk: single SSE client blocks server.
   - Risk: unauthenticated mutations if exposed.
   - Risk: path traversal/symlink reads.

12. Backup/restore:
   - Risk: backup omits major ctx state.
   - Risk: restore overwrites live state without rollback transaction.

Conclusion: the current tests show many modules can work in isolation, but they do not yet prove the complete product loop works from a clean install.

## Why The Current Test Suite Missed This

The suite is large, but several tests are shaped around implementation internals rather than user-observable contracts.

Patterns observed:

- Mocking subprocess calls that should be smoke-tested for real importability.
- Synthetic graph tests that do not enforce cross-surface recommendation equality.
- A-Z test explicitly excludes MCP install.
- Tests for resume missing session assert `FileNotFoundError` rather than user-friendly CLI behavior.
- Product import failures are skipped in at least one suite.
- CI requires tests but not enough package/release smoke gates.
- Full test pass does not equal clean install pass.

The strongest next testing investment is not more unit tests. It is a small set of clean-env contract tests:

- Build wheel.
- Install into clean venv.
- Temp HOME.
- Run `ctx-init --hooks --graph`.
- Run `ctx-scan-repo` on a temp FastAPI repo.
- Resolve/recommend a skill, agent, and MCP through every surface.
- Approve/install skill, agent, MCP with fake Claude CLI where needed.
- Run `ctx run` with fake provider + fake MCP.
- Resume same session with compaction and custom provider settings.
- Start monitor and test concurrent SSE plus mutation auth.

## Recommended Remediation Plan

Historical note: this was the remediation plan generated from the original review findings. Phases 1-28 above record the implementation and verification work that followed it. It is retained as audit evidence, not as the current backlog.

Follow the user's phase rule: no phase should touch more than five files.

### Phase 1: Bootstrap and hook install blockers

Goal: clean install can run hooks/graph setup or fail loudly.

Touch no more than:

- `src/ctx_init.py`
- `src/ctx/adapters/claude_code/inject_hooks.py`
- `install.sh`
- `src/tests/test_ctx_init.py`
- one new wheel/entrypoint smoke test

Success criteria:

- `PYTHONPATH=src python -m ctx.adapters.claude_code.inject_hooks --help` passes.
- `PYTHONPATH=src python -m ctx.core.wiki.wiki_graphify --help` passes.
- `ctx-init --hooks` no longer references removed flat modules.
- Hook commands point to packaged module paths or packaged scripts.
- Explicit hook/graph failure returns non-zero.

### Phase 2: Single recommendation engine

Goal: one ranking contract across hook, public API, MCP, resolver, and harness.

Touch no more than:

- `src/ctx/adapters/claude_code/hooks/context_monitor.py`
- `src/ctx/core/resolve/recommendations.py`
- `src/ctx/core/resolve/resolve_skills.py`
- `src/ctx/adapters/generic/ctx_core_tools.py`
- focused recommendation tests

Success criteria:

- Hook-local graph scorer removed.
- Normalized score priority fixed.
- Agent/MCP/skill results are typed consistently.
- Cross-surface golden test passes.

### Phase 3: Harness safety and runtime correctness

Goal: bounded, resumable, safe harness behavior.

Touch no more than:

- `src/ctx/adapters/generic/loop.py`
- `src/ctx/adapters/generic/evaluator.py`
- `src/ctx/cli/run.py`
- `src/ctx/adapters/generic/state.py`
- harness tests

Success criteria:

- Budgets apply to terminal responses.
- Evaluator revision order fixed.
- Total usage emitted for evaluator/planner mode.
- Missing session is clean CLI error.
- Resume preserves provider settings.

### Phase 4: MCP process boundary

Goal: no indefinite hangs and no default secret leakage.

Touch no more than:

- `src/ctx/adapters/generic/tools/mcp_router.py`
- `src/ctx/cli/run.py`
- `src/ctx/adapters/claude_code/install/mcp_install.py`
- MCP router/install tests
- docs for trust model

Success criteria:

- Request timeout test with silent server passes.
- MCP env is minimal/allowlisted.
- Resume requires re-approval or trusted config for MCP command execution.

### Phase 5: State safety

Goal: prevent avoidable data loss.

Touch no more than:

- `src/ctx/adapters/generic/state.py`
- `src/backup_mirror.py`
- `src/ctx/adapters/claude_code/install/install_utils.py`
- `src/ctx/core/wiki/wiki_sync.py`
- state/backup/wiki tests

Success criteria:

- Session create is exclusive.
- Restore creates rollback point and uses lock.
- Manifest/wiki updates are locked.

### Phase 6: Security hardening

Goal: defend dashboard/wiki/install boundaries.

Touch no more than:

- `src/ctx_monitor.py`
- `src/ctx/adapters/claude_code/install/skill_install.py`
- `src/ctx/adapters/claude_code/install/agent_install.py`
- tar extraction/install helper
- security tests

Success criteria:

- Slug/path containment enforced.
- Symlinks rejected for wiki entity/body/reference install/read paths.
- Dashboard mutations require token.
- `0.0.0.0` mutation exposure requires explicit unsafe flag.

### Phase 7: Graph and quality correctness

Goal: incremental results match full rebuild and alternate wiki roots stay isolated.

Touch no more than:

- `src/ctx/core/wiki/wiki_graphify.py`
- `src/ctx/core/graph/semantic_edges.py`
- `src/ctx/core/quality/dedup_check.py`
- `src/ctx/core/quality/tag_backfill.py`
- graph/quality tests

Success criteria:

- Incremental graph update matches full rebuild for edited existing nodes.
- Top-K state is row-wise correct.
- Dedup state includes body/content hash.
- Tag backfill honors supplied wiki root.

### Phase 8: Release and CI

Goal: impossible to publish broken package by accident.

Touch no more than:

- `.github/workflows/test.yml`
- `.github/workflows/publish.yml`
- `pyproject.toml`
- `src/ctx/__init__.py`
- release smoke tests

Success criteria:

- Ruff/mypy/package smoke in CI.
- Tag version equals package version.
- `ctx.__version__` equals installed metadata.
- Wheel entrypoints all smoke.
- `pip check` gate is addressed.

## Current Next Steps

The original P0/P1 remediation work is complete in this branch, and the parallel Phase 39 review's newly surfaced wiki P1 was fixed in Phase 40. The remaining work is release-hardening and live-integration validation.

1. Run a clean-machine A-Z host flow against a real Claude Code installation:
   - The opt-in live Claude host gate exists as of Phase 37.
   - Install from the built wheel into a clean virtualenv.
   - Use an isolated temporary home/config directory.
   - Run `ctx-init --hooks --graph`.
   - Run `CTX_LIVE_CLAUDE_ACK=uses_quota python scripts/clean_host_contract.py --fast --run-live-claude --live-claude-max-budget-usd 0.05`.
   - Confirm generated hooks execute in the host by inspecting the sentinel records written under the temp root.
   - Validate real Claude Code's Windows/Linux hook command execution semantics on at least one trusted Windows host and one trusted Linux host.
   - Run `ctx-scan-repo --recommend` on a small real repo and verify the same bundle through CLI, Python API, MCP, and Claude Code hook surfaces.
   - Keep this manual and quota-acknowledged: discovery showed `claude -p` can hit `error_max_budget_usd`, and this branch now requires an explicit environment acknowledgement plus a small `--max-budget-usd` cap before any live model call.

2. Validate live third-party MCP behavior:
   - The opt-in live MCP compatibility gate exists as of Phase 35.
   - Test at least one trusted real third-party MCP server for startup, `tools/list`, `tools/call`, timeout, stderr diagnostics, and env allowlist behavior.
   - Verify `ctx run --allow-tool/--deny-tool` UX when real model-visible tool names are involved.
   - Document any compatibility exceptions that require `inherit_env=True`.
   - The live MCP config parser now treats `inherit_env` as a strict boolean as of Phase 41.
   - Do not run `npx` or any third-party MCP command by default in CI.

3. Stress crash consistency and concurrent writers:
   - Kill processes during restore, wiki sync, manifest updates, and session writes.
   - Verify rollback snapshots, atomic temp-file replacement, and lock behavior.
   - Add regression tests for partial-write and parallel-writer cases not already covered.
   - Manifest install/uninstall lost-update coverage was fixed in Phase 34, active dashboard load/unload lost-update coverage was fixed in Phase 39, and wiki atomic write/lock coverage was hardened in Phase 38.
   - Remaining crash-consistency work should focus on process-kill restore/session interruption cases and any writer that still bypasses the shared atomic helpers.

4. Tag only after merge:
   - The 0.7.0 package dry-run passed locally.
   - After branch review/merge, tag the merge commit as `v0.7.0`.
   - Do not reuse the existing `v0.6.4` tag.

## Residual Uncertainty

Things I am not claiming:

- I am not claiming every bug in the repo has been found.
- I am not claiming every agent finding has a reproduction test yet.
- I am not claiming every legacy documentation sentence in the repo has been re-audited after every remediation phase.
- I am not claiming live Claude Code or third-party MCP hosts were exercised end to end on a real user machine.
- I am not claiming exhaustive crash-consistency coverage beyond the targeted rollback/session/wiki cases that were tested.

Things I am confident about:

- The original bootstrap missing-module bug was real and reproduced.
- The original hook path/package-layout mismatch was real from source inspection.
- The original recommendation split was real from source inspection.
- The original harness budget bypass was real from code path and agent probe.
- The original MCP timeout issue was real from blocking `readline()`.
- The security/data-loss issues were high enough risk to block release until covered by tests or fixed, and the remediation phases added focused regression coverage for the fixed paths.

## Appendix: Evidence Index

High-risk files called out repeatedly:

- `src/ctx_init.py`
- `src/ctx/adapters/claude_code/inject_hooks.py`
- `src/ctx/adapters/claude_code/hooks/context_monitor.py`
- `src/ctx/adapters/claude_code/hooks/bundle_orchestrator.py`
- `src/ctx/adapters/claude_code/install/install_utils.py`
- `src/ctx/adapters/claude_code/install/skill_install.py`
- `src/ctx/adapters/claude_code/install/agent_install.py`
- `src/ctx/adapters/claude_code/install/mcp_install.py`
- `src/ctx/adapters/generic/loop.py`
- `src/ctx/adapters/generic/evaluator.py`
- `src/ctx/adapters/generic/state.py`
- `src/ctx/adapters/generic/ctx_core_tools.py`
- `src/ctx/adapters/generic/tools/mcp_router.py`
- `src/ctx/cli/run.py`
- `src/ctx/core/resolve/recommendations.py`
- `src/ctx/core/resolve/resolve_skills.py`
- `src/ctx/core/wiki/wiki_graphify.py`
- `src/ctx/core/wiki/wiki_sync.py`
- `src/ctx/core/graph/semantic_edges.py`
- `src/ctx/core/quality/dedup_check.py`
- `src/ctx/core/quality/tag_backfill.py`
- `src/ctx/utils/_fs_utils.py`
- `src/ctx_monitor.py`
- `src/backup_mirror.py`
- `src/config.json`
- `install.sh`
- `.githooks/pre-commit`
- `.github/workflows/test.yml`
- `.github/workflows/publish.yml`
- `pyproject.toml`
- `src/ctx/__init__.py`
- `docs/knowledge-graph.md`
- `docs/skill-quality-install.md`
- `src/tests/test_ctx_init.py`
- `src/tests/test_alive_loop_e2e.py`
- `src/tests/test_harness_cli_run.py`
- `src/tests/test_scan_repo.py`
- `src/tests/test_mcp_quality.py`

## Final Reviewer Note

Observed:

- Nine specialized agents were used because the user explicitly requested sub-agent review.
- I re-checked the highest-risk paths locally with scoped file reads and command probes.
- Code and CI fixes landed across the phased commits referenced above, and this report was updated to keep historical findings separate from current branch state.

Inferred:

- The highest-risk architecture split around recommendations and harness tool policy has been converged in code; remaining convergence risk is in live host behavior, future migrations, and integration surfaces not fully reproducible in local unit tests.
- The branch is much closer to a release candidate than the original reviewed source, but it should still be treated as requiring CI and clean-install validation before a public release.

Not verified:

- Live Claude Code hook execution.
- Live MCP host behavior against a real third-party MCP.
- Browser-driven dashboard exploit reproduction.
- Exhaustive crash-consistency tests beyond the targeted rollback/session/wiki cases.
