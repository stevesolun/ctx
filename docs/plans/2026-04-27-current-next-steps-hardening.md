# Current Next Steps Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the review report's current next steps into repeatable release-hardening checks and then close the remaining live-host, MCP, browser-security, crash-consistency, docs, and release-readiness gaps in small verified phases.

**Architecture:** Start by adding an out-of-band clean-host contract runner that installs the wheel into a fresh virtualenv and drives real console scripts under an isolated home. Subsequent phases add live MCP compatibility probes, monitor browser tests, crash-consistency stress tests, and repo/docs organization, each with at most five touched files and focused verification.

**Tech Stack:** Python 3.11+, pytest, Ruff, mypy, GitHub Actions, venv, pip wheel/build/twine packaging, ctx CLI entrypoints.

---

## Constraints

- Each implementation phase touches no more than five files.
- Every phase ends with focused tests, Ruff, mypy where applicable, and a commit.
- Do not broaden public CLI/API surface until the contract proves the behavior is worth supporting.
- Keep long-running or live-host checks outside the required fast PR matrix until runtime and flake rate are measured.
- Do not mutate real `HOME`, `USERPROFILE`, `APPDATA`, Claude config, or global Python environments.

## Expert Review Synthesis

### Clean-Host / A-Z Flow

Observation:
- Existing package smoke loads console scripts and checks help output, but it does not run `ctx-init`, scan a repo, invoke `ctx run`, or resume a session from a clean wheel install.

Decision:
- Add a standalone `scripts/clean_host_contract.py` runner plus manual/nightly workflow.
- Keep it out of `pyproject.toml` scripts for now; this is release infrastructure, not user API.

### MCP / Tooling

Observation:
- MCP subprocess env hardening exists, but live third-party MCP compatibility is not proven.
- CLI cannot yet express MCP env overlays or inherit-env exceptions.
- Tool policy is enforceable but lacks a preflight/dry-run UX.

Decision:
- Phase 1 proves local wheel and harness process boundaries.
- Later phases add live MCP pytest option, CLI env UX, and tool-policy preflight.

### Monitor / Browser Security

Observation:
- Token/origin/path hardening exists in unit tests, but browser-driven mutation/SSE behavior is not exercised.

Decision:
- Add browser or HTTP-client contract tests after the clean-host runner exists, so dashboard checks can reuse the same isolated-home pattern.

### Crash Consistency

Observation:
- Targeted rollback/session/wiki tests exist, but manifest read-modify-write locking, wiki atomic writes, process-level session concurrency, and kill-during-write coverage remain weak.

Decision:
- Add process-level lock/atomic primitive proof first, then manifest/wiki/session/restore hardening in separate phases.

### Repo Organization / Docs Debt

Observation:
- The repo intentionally still has flat modules plus package modules during migration. A broad reorg would be risky before release contracts exist.

Decision:
- Do not reorganize first. Add searchable docs/entrypoint audits and migrate flat modules only after clean-host and package contracts are green.

## Phase 1: Clean-Host Contract Harness

**Files:**
- Create: `scripts/clean_host_contract.py`
- Create: `src/tests/test_clean_host_contract.py`
- Create: `.github/workflows/clean-host-contract.yml`
- Create: `docs/harness/clean-host-contract.md`
- Create: `docs/plans/2026-04-27-current-next-steps-hardening.md`

**Steps:**

1. Add `scripts/clean_host_contract.py`.
   - Build a wheel into a temp directory with `pip wheel --no-deps`.
   - Create a fresh venv.
   - Install the wheel.
   - Set isolated `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, and `PIP_CACHE_DIR`.
   - Write a tiny FastAPI-like repo.
   - Write a process-local fake `litellm.py`.
   - Run `ctx-init --hooks`, `ctx-scan-repo --recommend`, `ctx run`, `ctx resume`, and a denied-tool policy run.
   - Assert all expected user-state writes live under the temp root.

2. Add `src/tests/test_clean_host_contract.py`.
   - Unit-test env isolation.
   - Unit-test venv script path resolution on Windows and POSIX.
   - Unit-test fake LiteLLM module content.
   - Unit-test command sequencing without building/installing wheels.

3. Add `.github/workflows/clean-host-contract.yml`.
   - Trigger on `workflow_dispatch` and a weekly schedule.
   - Upgrade pip.
   - Run `python scripts/clean_host_contract.py --fast`.

4. Add `docs/harness/clean-host-contract.md`.
   - State what the contract proves.
   - State what it skips.
   - Document local command and CI workflow.

5. Verify Phase 1.
   - `python -m pytest src\tests\test_clean_host_contract.py -q`
   - `python -m ruff check scripts\clean_host_contract.py src\tests\test_clean_host_contract.py`
   - `python -m mypy scripts\clean_host_contract.py src\tests\test_clean_host_contract.py`
   - `python scripts\clean_host_contract.py --fast`
   - `python -m ruff check src hooks scripts`
   - `python -m mypy src`

**Stop point:** Commit Phase 1 and wait for explicit Phase 2 approval.

## Phase 2: Monitor SSE Concurrency And Route Safety

**Goal:** Fix the highest-priority live-server bug from the monitor/browser review: one open SSE stream can monopolize the stdlib `HTTPServer` and block normal dashboard/API requests.

**Candidate files:**
- Modify: `src/ctx_monitor.py`
- Modify: `src/tests/test_ctx_monitor.py`
- Modify: `src/tests/test_ctx_monitor_3type.py`
- Modify: `docs/dashboard.md`
- Modify: `docs/reports/ctx-million-dollar-review-2026-04-27.md`

**Steps:**
- Replace the monitor's single-threaded server with a `ThreadingHTTPServer` variant using daemon request threads.
- Add a regression where an open `/api/events.stream` request does not block `/api/sessions.json`.
- Route monitor slug validation through the shared safe-name validator, including Windows reserved-name regressions.
- Make `_wiki_entity_path()` resolve sharded MCP wiki pages listed by `_wiki_index_entries()`.
- Document the SSE concurrency and slug-routing contract.

**Verification:**
- `python -m pytest src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_3type.py src\tests\test_safe_name.py -q`
- `python -m ruff check src\ctx_monitor.py src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_3type.py`
- `python -m mypy src`
- `python scripts\clean_host_contract.py --fast`

## Phase 3: Operational Stale References And ctx-init Idempotence

**Goal:** Remove setup noise and stale moved-module references from operational scripts and user-facing docs.

**Candidate files:**
- Modify: `install.sh`
- Modify: `.githooks/pre-commit`
- Modify: `src/ctx_init.py`
- Modify: `src/tests/test_ctx_init.py`
- Modify: `docs/skill-quality-install.md`

**Steps:**
- Replace stale `src/wiki_sync.py`, `src/inject_hooks.py`, and `src/wiki_graphify.py` references with packaged module paths or console scripts.
- Make expected starter toolbox existence a clean skip, not a warning.
- Add a `ctx-init` re-run regression test.
- Search docs/scripts for `python -m inject_hooks`, `python -m wiki_graphify`, `resolve_by_seeds`, and current-behavior "graph neighbor" wording.
- Update remaining stale docs in later docs-only slices if the five-file cap is reached.

**Verification:**
- `bash -n install.sh`
- `bash -n .githooks/pre-commit`
- `python -m pytest src\tests\test_ctx_init.py -q`
- `python -m ruff check src\ctx_init.py src\tests\test_ctx_init.py`
- `python -m mypy src\ctx_init.py src\tests\test_ctx_init.py`
- `python scripts\clean_host_contract.py --fast`

## Phase 4: Crash Consistency Primitives

**Goal:** Prove file-lock and atomic-write behavior across processes before applying it to higher-level stores.

**Candidate files:**
- Modify: `src/ctx/utils/_fs_utils.py`
- Modify: `src/ctx/utils/_file_lock.py`
- Modify: `src/tests/test_fs_utils.py`
- Create or modify: `src/tests/test_file_lock.py`

**Steps:**
- Add supported fsync behavior for temp files and parent directories.
- Add process-level file-lock serialization tests.
- Add subprocess kill-before-replace tests.
- Document platform limitations.

**Verification:**
- `python -m pytest src\tests\test_fs_utils.py src\tests\test_file_lock.py -q`
- `python -m ruff check src\ctx\utils src\tests\test_fs_utils.py src\tests\test_file_lock.py`
- `python -m mypy src\ctx\utils`

## Phase 5: Live MCP Compatibility Probe

**Goal:** Prove real third-party stdio MCP startup/list/call behavior without making it required on every PR.

**Candidate files:**
- Modify: `src/tests/conftest.py`
- Create: `src/tests/test_mcp_live_compat.py`
- Modify: `src/ctx/adapters/generic/tools/mcp_router.py`
- Modify: `.github/workflows/clean-host-contract.yml`
- Modify: `docs/harness/attaching-to-hosts.md`

**Steps:**
- Add `--run-live-mcp` pytest option.
- Skip live MCP tests unless explicitly enabled.
- Validate startup, `tools/list`, timeout diagnostics, stderr tail, explicit env overlays, and inherit-env exceptions.
- Document trusted local MCP setup.

**Verification:**
- `python -m pytest src\tests\test_mcp_live_compat.py -m "not integration" -q`
- `python -m pytest src\tests\test_mcp_live_compat.py -m integration --run-live-mcp -q`
- `python -m ruff check src\ctx\adapters\generic\tools\mcp_router.py src\tests\test_mcp_live_compat.py src\tests\conftest.py`
- `python -m mypy src\ctx\adapters\generic\tools\mcp_router.py src\tests\test_mcp_live_compat.py src\tests\conftest.py`

## Phase 6: MCP CLI Env UX And Tool Policy Preflight

**Goal:** Make env hardening usable and tool policy auditable before a model attempts calls.

**Candidate files:**
- Create: `src/ctx/adapters/generic/tool_policy.py`
- Modify: `src/ctx/cli/run.py`
- Modify: `src/ctx/adapters/generic/loop.py`
- Modify: `src/tests/test_harness_cli_run.py`
- Modify: `src/tests/test_harness_loop.py`

**Steps:**
- Move allow/deny parsing into a shared policy module.
- Add tool catalog preflight output.
- Add `--dry-run-tools`.
- Add explicit MCP env/inherit controls or config-file support.
- Preserve deny-overrides-allow semantics.

**Verification:**
- `python -m pytest src\tests\test_harness_cli_run.py src\tests\test_harness_loop.py -q`
- `python -m ruff check src\ctx\adapters\generic\tool_policy.py src\ctx\cli\run.py src\ctx\adapters\generic\loop.py src\tests\test_harness_cli_run.py src\tests\test_harness_loop.py`
- `python -m mypy src\ctx\adapters\generic\tool_policy.py src\ctx\cli\run.py src\ctx\adapters\generic\loop.py src\tests\test_harness_cli_run.py src\tests\test_harness_loop.py`

## Phase 7: Monitor Browser Security Contract

**Goal:** Verify real dashboard requests, token/origin rejection, sidecar path rejection, and browser-observed SSE behavior after Phase 2 fixes server concurrency.

**Candidate files:**
- Create: `src/tests/test_ctx_monitor_browser_contract.py`
- Modify: `src/tests/test_ctx_monitor.py`
- Modify: `src/ctx_monitor.py`
- Modify: `.github/workflows/clean-host-contract.yml`
- Modify: `docs/dashboard.md`

**Steps:**
- Start `ctx-monitor` in an isolated temp home.
- Fetch dashboard HTML and extract or configure the mutation token.
- Assert missing-token mutations fail.
- Assert same-origin/token mutations succeed.
- Assert traversal-style sidecar paths fail.
- Open concurrent SSE clients while issuing load/unload mutations.

**Verification:**
- `python -m pytest src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_browser_contract.py -q`
- `python -m ruff check src\ctx_monitor.py src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_browser_contract.py`
- `python -m mypy src\ctx_monitor.py src\tests\test_ctx_monitor.py src\tests\test_ctx_monitor_browser_contract.py`

## Phase 8: Manifest, Session, Wiki, And Restore Hardening

**Goal:** Apply proven primitives to the highest-risk read-modify-write stores.

**Candidate sub-phases:**
- Manifest locking: `install_utils.py`, skill/agent install tests.
- Session writer concurrency: `state.py`, `test_harness_state.py`.
- Wiki atomic locked writes: `wiki_sync.py`, `test_wiki_sync.py`.
- Restore transaction gaps: `backup_mirror.py`, `ctx_lifecycle.py`, matching tests.

**Verification:**
- Focused pytest per store.
- `python -m ruff check src hooks scripts`
- `python -m mypy src`
- `python -m pytest -q`

## Phase 9: Repo Organization And Release Readiness

**Goal:** Reduce flat/package split confusion only after contracts protect behavior.

**Steps:**
- Generate a flat-module migration inventory.
- Move one low-risk flat module batch per phase.
- Keep console scripts stable.
- Update docs and changelog.
- Dry-run tag/version/publish workflow.

**Verification:**
- `python scripts\clean_host_contract.py --fast`
- `python -m pytest -q`
- `python -m ruff check src hooks scripts`
- `python -m mypy src`
- `python -m build`
- `python -m twine check dist/*`
