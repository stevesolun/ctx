# CTX Unified Capability Engine — Working State

This file is the canonical resume checkpoint for the long-running unified CTX
engine implementation. Read it together with
[`docs/plans/unified-capability-engine.md`](docs/plans/unified-capability-engine.md)
and `AGENTS.md` before continuing work.

The plan defines where the product is going. This file records where the work
actually is. Update this file whenever a slice is independently accepted, a
material risk is discovered, the critical path changes, or validation evidence
becomes stale.

## Checkpoint

- Updated: 2026-08-08 (Asia/Jerusalem)
- Branch: `codex/benchmark-runtime-fairness`
- Base commit observed: `3e24e397b07c73b948abfb3a374bee9963b995d9`
- Goal status: active; the complete product objective is **not achieved**
- Working tree: broadly dirty and intentionally uncommitted; preserve unrelated
  and user-owned changes
- Persistence status: this checkpoint exists on disk but is currently untracked
  by Git; it is not portable to another clone until intentionally committed
- Current critical path: receipts/activation → lifecycle → host cutover →
  product proof. The signed-consent-execution focused suite is now fully green
  (see the 2026-08-08 evidence and the reopened signed-slice status below); the
  head of the critical path has advanced to end-to-end managed
  receipts/activation.

## Product objective

Build one host-neutral, event-sourced CTX capability engine that observes the
work being done and maintains the smallest useful global set of zero to five
skills, agents, MCP servers, and harnesses across Codex, Claude Code, `ctx run`,
MCP, and generic harnesses.

The selected graph can be the CTX graph or a user/organization graph. A relevant
capability does not have to be installed already. When similarity and expected
benefit justify it, CTX may propose it and then install it under the user's
persisted per-kind policy:

- preapproved automatic installation; or
- exact confirmation for each installation.

The choice is independent for skills, agents, and MCP servers. Persistent
uninstall is never silent. Deactivation of temporary context and persistent
uninstall remain different operations.

## Milestone state

| Milestone | State | Current evidence or gap |
| --- | --- | --- |
| Canonical event protocol, reducer, journal, replay, and global 0–5 invariant | Accepted foundation | Deterministic engine and schema-v3 planning/runtime tests exist under `src/tests/engine/` and `src/tests/runtime/` |
| Durable consent authentication, recovery, trusted time, and policy snapshots | Accepted foundation | Consent broker/store and install-policy suites passed focused and independent review |
| Content-addressed artifact registry and production composition | Accepted foundation | Pathless managed artifacts and actuator-bound composition accepted after independent review |
| Managed query `prepare` and `reopen` | Accepted foundation | Exact retry, crash recovery, concurrency, privacy, and host-neutral projections covered |
| Safe managed consent challenge publication | Accepted foundation | Uses the composition-owned physical actuator registry; stale-head and target-drift failures are closed |
| Durable desired-set store | Accepted foundation | Authenticated 0–5 records, predecessor causation, cross-process reservation, and crash recovery accepted |
| Managed `set_desired` service | Accepted | Independent review found two P1 defects; both were repaired and re-reviewed with no open P0/P1 |
| Preapproved automatic install execution | Accepted | Deterministic grant, invariant pre-grant actuator binding, claim/outcome/receipt recovery, honest mixed outcomes, and stale-consent race recovery passed independent adversarial review with no open P0/P1 |
| Signed ask-each-time resolution and execution | Implemented; focused suite green; formal acceptance pending | All four previously-open P1 recovery classes now have implementing code and green covering tests (terminal consent expiry under target/policy drift, post-journal target drift, unclaimed post-grant action expiry, target-independent settled-denial recovery). A stale-clock regression that reddened five drift/refusal tests was repaired test-only on 2026-08-08 (see below). The full dirty-tree gate is now green (19/19 PR-preflight lanes). Remaining before formal acceptance: an independent adversarial re-review across the complete signed checklist, plus the still-unavailable native Windows and real interactive host evidence |
| Installation receipts and activation through the managed service | Partial: applied activation seam accepted; service continuation + recovery pending | Slice 2.1 (2026-08-08) added a composition-integrated applied-activation seam `EngineComposition.execute_activation` (new `activation_execution.py` mirroring `install_execution.py`), so callers stop reaching engine-private activation methods. `ManagedQueryService.activate`/`recover_activation` and failed/expired/indeterminate recovery (slice 2.2, needs a store `outcome`-column migration) are not yet wired |
| Usage evidence, opportunity-aware cooling, deactivation, and uninstall proposals | Partial foundation | Reducer vocabulary/primitives exist; complete managed product loop and host receipts are not integrated |
| Skills/agents/MCPs from CTX, user, and organization graph layers | Partial foundation | Catalog and planning primitives exist; all local layers and trusted external material installation are not complete |
| Codex, Claude Code, `ctx run`, MCP, and generic cutover | Pending | Adapter prototypes/facades exist, but they are not accepted as one complete managing-engine path |
| Multi-language observation and relevance gates | Pending | Claimed top-language analyzer corpus and preregistered relevance thresholds have not passed |
| Real-host/security/compatibility rollout gates | Pending | Focused security work exists; full clean-home, platform, upgrade, and canary evidence is absent |
| CTX versus no-CTX benefit proof | Pending | Benchmark infrastructure exists; official result remains 0/10 controls and 0/30 pairs, so no benefit claim is valid |

## Accepted slice: managed `set_desired`

### Implemented

- Sealed public request/result and typed busy, conflict, and superseded errors.
- Canonical zero-to-five subset in committed-plan order; no reranking.
- Full-digest, stream-stable ownership and source/kind-bound leases.
- Fresh trusted occurrence time and immutable policy snapshot binding.
- Reserve → exact journal process/recovery → durable mark → current-head action
  projection → consent publication.
- Empty selection, manual deferral, ask-each-time consent, and honest automatic
  policy deferral.
- Sequential choices with exact predecessor compare-and-swap.
- Crash recovery after reservation, engine commit, store mark, and result
  projection.
- One cross-process lifecycle lock shared by `prepare`, `reopen`, and
  `set_desired`.
- Pending desired work blocks replacement planning.
- Competing choices serialize; identical concurrent choices converge.

### Acceptance repairs

- Crash after durable reservation plus a policy change no longer wedges the
  stream: the authority-free desired event commits, reports policy drift, and
  an exact successor can reissue consent under current policy.
- Result status now consults durable `state.pending_effects`; an actionless
  evidence event cannot make a pending installation appear reconciled.

## Accepted slice: preapproved automatic installation

### Implemented

- Deterministic action-specific `UserDecision` under the current persisted
  per-kind preapproval policy; sensitive or credentialed installs still require
  interaction.
- Exact desired-plan membership, global serialized execution, and physical
  actuator binding resolved before authority is committed.
- Claim-once execution with durable outcome and receipt recovery across crashes
  after decision, claim, outcome, receipt, or response.
- Exact applied material lineage anchored to one journaled receipt; failed and
  indeterminate installs never report reconciliation.
- Canonical bounded failure projections and authority-free sealed results.
- Interactive grants cannot enter the automatic execution path.

### Acceptance repairs

- A committed automatic grant now cryptographically commits the exact physical
  binding through its deterministic event and reconstructs that commitment
  before any later claim. Restarting against a changed target fails before host
  mutation; restoring the original target recovers exactly once.
- Provider submission, tool call, turn start, and prompt-context receipt paths
  that consume an immediate-next-revision consent now re-run lifecycle
  reconciliation. Service fallback also refuses to call a desired absent
  install `reconciled` when lifecycle authority is missing.
- The desired-result factory rejects `reconciled` plus failed-capability
  contradictions.

## Latest evidence

Evidence is valid only for the files as they existed at the stated checkpoint.
Any later edit to the covered surface requires rerunning the affected check.

- 2026-08-08 dirty-tree gate campaign: the pre-existing debt that blocked the
  full gate was cleared. `python -m mypy src` now reports **Success: no issues
  found in 548 source files** (was 29 errors across five test-fixture files);
  Ruff check and `ruff format --check` pass across `src`, `hooks`, and
  `scripts` (575 files). Full local suite (`-m "not integration and not
  browser"`) went from **14 failed / 8216 passed** to **1 failed / 8229 passed /
  9 skipped**. Root causes fixed, not suppressed: an unquoted comma inside the
  ENGINE-002 `validation_status` field of `qa/feature_status.csv` corrupted
  `csv.DictReader` (a `None` restkey) and cascaded into six of the seven
  feature/user-story tracker failures; ten new untracked engine modules were
  attributed to ENGINE-001/ENGINE-002; the A/B live-coding-pair suite now
  substitutes the Codex-sandbox evaluator boundary (`verify_workspace`) with a
  deterministic double that keeps an evaluator-hash assertion, matching the
  sibling deterministic-bridge suites — no fairness, provenance, or
  serialization assertion was weakened; the stale README/docs test-inventory
  badge was refreshed (7,746 → 8,253) with `src/update_repo_stats.py`.
- 2026-08-08 PR preflight (`python scripts/ci_preflight.py --profile pr`):
  **all 19 of 19 lanes pass; 0 failed, 0 skipped.** Lanes: whitespace, repo
  stats, no-test policy, `ruff format`, `ruff`, `mypy src`, `pip check`,
  unit-linux equivalent (**8230 passed, 9 skipped, 91.9 percent coverage**
  against a 40 percent floor), A–Z canary, contract compatibility, clean host
  contract, public docs tracker, docs strict build, telemetry enterprise,
  similarity precision/recall, browser monitor security, clean preflight dist,
  reproducible wheel build, and twine check. Reproducible artifacts:
  wheel `316961b9cc2aaf11393713dc62d2eea83dbf9d392e8a9e5ccd30cc6c83d3a72a`,
  sdist `3a1684e9c9f37a13fadbcd6daae5dda4974b766e2d5a7817df8fafb245bb8c18`.
  The clean-host lane additionally executed six Claude Code hook commands and
  one Codex hook command from a fresh wheel with zero failures, each returning
  the exact 750-byte `skill:ctx-python-testing` capability context.
  Caveat: the preflight process exit status is `0` even on a failing run, so
  lane results must always be read from its log, never from the exit code.
- 2026-08-08 manage-mode ask-prompt contract conflict resolved. Two tests
  encoded mutually exclusive contracts for the same
  `QueryDeliveryController(mode="manage").issue(...)` call:
  `test_query_hook_delivery.py::test_manage_mode_publishes_one_durable_resumable_challenge_without_installing`
  required a durable challenge, while
  `test_activated_skill_controller_integration.py::test_manage_repeated_ask_prompts_remain_authority_free_and_nonpersistent`
  required no journal at all. The second contract is unsatisfiable: because
  `ManagedQueryService.resolve_consent` authenticates a signed decision against
  a durable challenge record (`status_by_challenge_digest`), a resumable
  ask-each-time consent cannot exist without one persisted challenge. Empirical
  probing showed production behavior is already correct — two repeated ask
  prompts publish exactly **one** challenge, reuse it (byte-identical payloads,
  same `challenge_id`), keep the journal at **3** records, install nothing, leak
  no `consent_id`/`requested_action` into model-visible bytes, and persist no
  raw prompt text or absolute path in any durable byte. **No production change
  was required.** The stale test was rewritten as
  `test_manage_repeated_ask_prompts_reuse_one_authority_free_consent_challenge`
  asserting that strictly stronger contract, with the reasoning recorded in its
  docstring. Each new assertion was mutation-checked (perturbing the challenge
  count, journal count, or payload-reuse equality makes it fail) so the test is
  demonstrably non-vacuous.
- 2026-08-08 slice 2.2a (expired/unclaimed activation retirement): closes the
  expiry half of the P1 activation recovery gap with **no durable schema
  change**. Before this slice an `ActionExpired` on a pending activation was
  refused by `_resolve_activation_receipt_guard` before any commit, so a
  never-claimed activation whose window lapsed wedged the stream permanently.
  Changes: `ActivationActionClaimGuard` gains an in-memory
  `mode: Literal["applied", "expired"]` with an optional outcome digest
  (mirroring `InstallActionClaimGuard`); `_validate_activation_claim_guard`
  returns `None` for expired after proving inside the commit transaction that
  **no durable claim exists** (an existing claim raises
  `ActivationActionAlreadyClaimed`, because a claim means a driver may already
  have mutated the host); `commit` skips activation settlement for expired,
  exactly mirroring the install branch; `_activation_receipt_claim_guard` now
  returns the `PendingEffect` so the resolver can read `effect` — the match
  itself stays keyed on action kind, because narrowing it to `activate` would
  let a `rollback-activate` receipt commit with no claim and no verified
  outcome; new `_exact_unclaimed_activation_action_expiry` and
  `_assert_activation_action_has_expired` mirror the install expiry helpers and
  are asserted both before reduce and again immediately before commit;
  `process_activation_receipt` now rejects any non-applied guard at the door.
  The reducer change was **mandatory and initially missed**: an expired
  activation previously fell through to `_fail_receipt_v3`, which adds the
  capability to `blocked_capability_ids` and would have converted a hard wedge
  into a permanent soft wedge, since blocked ids survive re-planning. The
  retirement branch now covers `pending.effect in {"install", "activate"}` and
  re-requests a displaced `rollback_capability_id` so a rollback-held
  capability is never stranded.
  Replay determinism is preserved: an expired-activation event could never be
  committed before this slice (the guard raised at `_process` before
  `_prepare_reduce_commit`), so no existing journal can contain one and no
  existing replay changes. The durable-digest hazard was avoided deliberately —
  `mode` never enters the settlement `values` dicts, so every settlement row
  already on disk still revalidates.
  Evidence: `src/tests/runtime/test_release_skill_lifecycle.py` **27 passed**,
  including three new forgery bars (premature trusted clock, tampered payload,
  and settled/completed action) plus a retirement that asserts zero
  `engine_activation_outcomes` and zero `engine_activation_claim_settlements`
  rows. Runtime, engine, and core suites: **1687 passed, 4 skipped**. Repo-wide
  `mypy src` reports no issues in 548 files; Ruff and formatting pass.
  The obsolete `test_generic_engine_process_cannot_forge_activation_expiry`
  asserted that a well-formed, genuinely expired, never-claimed retirement must
  be refused — that assertion *was* the wedge, so it was replaced by the three
  targeted forgery bars above rather than deleted.
  Full PR preflight re-run after this slice: **all 19 of 19 lanes pass, 0 failed,
  0 skipped**, with the unit lane at **8233 passed, 9 skipped**. The repo stats
  lane failed once first because the new tests changed the inventory count; it
  was refreshed with `src/update_repo_stats.py`.
- 2026-08-08 slice 2.2a independent adversarial review: **accepted, no blocking
  issues.** Every attack fell closed under executed probes, not inspection
  alone: six two-thread claim-versus-retire races on one journal (the claim won
  every time; retirement always lost with `ActivationActionAlreadyClaimed`); a
  claim forced to land after every engine-side check and immediately before
  commit (still refused, journal unchanged); a claim attempted from inside the
  retirement's open `BEGIN IMMEDIATE` (blocked for the full window, so SQLite
  genuinely serializes the two transactions); re-claiming an already-retired
  action (refused by the journal anchor on a fresh head, and by
  `RevisionConflict` on a stale head, so no host mutation can be authorized
  after retirement); `rollback-activate` retirement (double-blocked by the
  resolver and the expiry helper); supplied-guard injection on the expired path
  (refused three ways); and payload rebinding across foreign action id, wrong
  action kind, wrong content digest, wrong precondition revision, wrong reason,
  and foreign scope (all refused). Review follow-ups applied: the
  legitimate-retirement test now pins `trusted_utc_now` to `AFTER_EXPIRY`
  instead of depending on the machine's wall clock, and was renamed to
  `test_expired_unclaimed_activation_is_retired_without_host_authority` because
  the old name contradicted its body; a mutation check confirms it still fails
  on a pre-expiry clock. The reducer retirement branch now also requires the
  exact schema-v3 activation payload, so the reducer and both engine gates
  agree rather than relying on a distant state-validation invariant.
  Recorded non-blocking observations, not defects in this slice: retirement
  writes no "already expired once" marker, so a host that never claims can loop
  request/expire/retire without backoff (availability, not authority); and the
  "no durable claim implies no host mutation" invariant rests on side tables
  that are not hash-chained against row deletion, so an attacker with write
  access to the 0700 SQLite store could delete both the claim and outcome rows
  and retire a real mutation. The install expired arm has the identical shape,
  so this is pre-existing design; it deserves an explicit threat-model line or
  a journal-anchored retirement tombstone if store write access is in scope.
- 2026-08-08 slice 2.1 (applied activation seam): new
  `src/ctx/runtime/activation_execution.py` (applied path mirroring
  `install_execution.py`) + `EngineComposition.execute_activation` +
  `runtime/__init__` exports. The lifecycle reach-in test
  (`test_managed_query.py::test_managed_development_preserves_exact_lifecycle_transition_on_retry`)
  now drives activation through the public seam (no `composition._engine` /
  `engine._record_activation_outcome` / `_issue_activation_outcome_permit`
  reaches) and asserts idempotent re-entry. Three added focused tests cover the
  crash-recovery/race branches an independent reviewer required:
  `test_execute_activation_recovers_a_recorded_but_unsettled_outcome`
  (crash-after-outcome), `test_execute_activation_recovers_after_a_claim_without_outcome`
  (crash-after-claim), and `test_execute_activation_tolerates_a_lost_claim_race`
  (the `ActivationActionAlreadyClaimed` catch). Runtime suite `893 passed, 4
  skipped` under `pytest -n 12` (one unrelated pre-existing failure,
  `test_manage_repeated_ask_prompts_...`, proven independent by reverting the
  slice and reproduced identically — flagged as a separate task). Ruff, format,
  and strict mypy pass on `activation_execution.py` and `composition.py`.
  Independent 3-agent review: two `approve-with-nonblocking`; the third required
  the three crash/race tests above, which were then added and pass. A formal
  re-review confirmation of that third lane and the full dirty-tree gate remain
  outstanding.
- 2026-08-08 signed-slice test-clock repair: five red tests in
  `src/tests/runtime/test_managed_query_service.py`
  (`test_desired_retry_refuses_interactive_effect_without_signed_execution_path`,
  `test_head_drift_is_rejected_before_broker_authentication`,
  `test_service_rejects_decision_committed_during_registry_binding`,
  `test_service_rejects_decision_committed_during_broker_publication`,
  `test_service_rejects_decision_committed_during_broker_status`) were failing
  with `install decision has expired according to trusted clock`. Root cause was
  test-only: the two test-side sites that commit a `UserDecision`
  (`_commit_consent_decision` and the inline composition in
  `test_desired_retry_...`) opened `open_managed_engine_composition` without
  threading the frozen clock, so the engine's decision-time expiry guard
  (`engine.py:772` → `_assert_install_decision_not_expired`, `engine.py:978`)
  defaulted to real wall-time and correctly deemed the frozen-dated
  (`2026-08-03T13:00Z`) pending install action expired. Fix: expose
  `_Setup.clock` and thread `trusted_utc_now=setup.clock` into both sites (four
  lines, one test file). Production is unaffected — every production
  `open_managed_engine_composition` caller
  (`managed_query_service.py:1361/2433/3661`) already threads
  `self._trusted_utc_now`, and both production `UserDecision` commits
  (`managed_query_service.py:1867/3260`) run inside that clock-threaded service.
- 2026-08-08 focused signed-slice suite: `215 passed in 54.81s` across
  `test_managed_query_service.py`, `test_install_consent_broker.py`,
  `test_install_consent_continuation.py`, `test_install_consent_authenticators.py`,
  `test_install_consent_challenge_lookup.py`, and
  `test_install_consent_broker_store.py` (was `210 passed, 5 failed`). Broader
  STATE.md aggregate suite: `348 passed in 63.62s`. Ruff and format clean on the
  touched test file; mypy adds no new errors on the changed lines (the file
  carries 14 pre-existing non-strict `arg-type` errors from fake-port fixtures,
  unrelated to this change).
- 2026-08-08 independent review board (4-agent workflow; 3 completed, 1 agent
  aborted on a structured-output retry cap): the completed fix reviewer returned
  `test-only-correct` with the guard confirmed as correct fail-closed production
  behavior; the aborted skeptic's angle (no production `UserDecision` path lacks
  a trusted clock) was closed by direct grep. Two P1 auditors found all four
  signed-slice recovery classes **closed** with green named tests: class A
  `test_terminal_broker_expiry_durably_retires_pending_consent_and_allows_next_choice`,
  class B `test_target_replacement_after_decision_commit_never_reaches_claim_or_driver`,
  class C `test_signed_grant_expired_before_claim_is_retired_once_and_can_be_chosen_again`,
  class D `test_service_signed_denial_never_invokes_install_driver` (plus the
  `[denied]` continuation). None of these four were among the five reddened
  tests, so the recovery logic was already green at baseline — the prior
  "four open P1 recovery classes" status was stale. This is focused-green plus
  independent review, not a formal slice acceptance: a full adversarial
  acceptance re-review across the entire signed checklist and the full dirty-tree
  gate are still required.
- 2026-08-03 coordinator aggregate: `297 passed in 28.46s` across managed query,
  desired/query stores, composition, consent lookup/broker, and broker store.
- 2026-08-03 coordinator static checks: Ruff passed; five focused files already
  formatted; strict mypy reported no issues in three production files.
- 2026-08-03 writer checkpoint: complete managed-query service suite `51 passed`;
  Ruff, formatting, and strict service mypy passed before the final adversarial
  additions.
- 2026-08-03 coordinator frozen-surface check: complete managed-query service
  suite `56 passed in 13.10s`; Ruff passed; three files were already formatted;
  strict service mypy reported no issues.
- 2026-08-03 coordinator frozen aggregate: `302 passed in 30.96s` across the
  complete managed query, desired/query store, composition, consent
  lookup/broker, and broker-store surface.
- 2026-08-03 independent repaired-surface aggregate: `304 passed in 30.46s`;
  Ruff passed; seven files were already formatted; strict mypy reported no
  issues in three production files. Independent verdict: accepted, no open
  P0/P1.
- 2026-08-03 coordinator post-repair checks: complete service suite `58 passed`;
  service plus desired/query stores `132 passed`; Ruff, formatting, strict
  service mypy, and diff integrity passed.
- 2026-08-03 automatic-execution writer freeze: complete service suite
  `70 passed in 22.24s`; Ruff and formatting passed; strict service mypy passed;
  two-file diff integrity passed.
- 2026-08-03 coordinator automatic-execution aggregate: `316 passed in
  37.53s` across the managed service, desired/query stores, composition,
  challenge lookup, consent broker, and broker store at service hash
  `ac1736c824872008206db1fe70c92ce57fdf91efe2bcf51692c52e3837c815db`.
- 2026-08-03 coordinator lower execution aggregate: `129 passed in 14.31s`
  across install execution/continuation, engine claim/coordinator/outcome, claim
  store, and installation-contract suites. Ruff, formatting, strict service
  mypy, diff integrity, and frozen-file hash recheck also passed.
- 2026-08-03 independent automatic-execution review: **rejected** with two P1
  reproductions despite `260 passed in 36.80s` and green Ruff, formatting,
  eight-file mypy, and diff integrity. A restart could substitute the physical
  binding after grant, and an intervening observation could lose pending
  consent then falsely report reconciliation. Both are under repair.
- 2026-08-03 automatic-execution repair freeze: writer focused service/reducer
  aggregate `94 passed in 27.18s`; Ruff, formatting, strict mypy on both
  production files, and diff integrity passed.
- 2026-08-03 coordinator repaired-surface aggregate: `340 passed in 44.11s`
  across the managed service/store/composition/broker surface plus schema-v3
  reducer regressions; lower install execution aggregate `129 passed in
  15.71s`; Ruff, formatting, two-file strict mypy, diff integrity, and all four
  frozen hashes passed.
- 2026-08-03 independent automatic-execution re-review: **accepted**, no P0/P1;
  `341 passed in 43.83s`; Ruff, formatting, nine-source mypy, and diff integrity
  passed. Exact changed-target restart failed before mutation and original-target
  restoration recovered once; provider/tool/turn races each refreshed exactly
  one consent and installed exactly once on retry.
- 2026-08-03 signed-resolution broker audit: decision-expired/live-challenge
  state now moves to fresh-nonce reauthentication rather than terminal expiry;
  broker/store/lookup aggregate `90 passed in 6.33s`, with Ruff and strict store
  mypy green. This repair remains part of the not-yet-accepted signed slice.
- 2026-08-03 coordinator first signed-slice aggregate: **rejected**, `3 failed,
  404 passed in 59.76s`. Eager evidence-provider binding created the engine
  journal during service construction, violating no-mutation-before-use and
  breaking path-collision validation. Lazy binding repair is active.
- 2026-08-03 coordinator second signed freeze: `409 passed in 52.86s` across
  managed service, broker/authenticator/lookup/continuation, executor,
  composition, query stores, install claims/outcomes, reducer, and policy;
  lower install aggregate `129 passed in 13.20s`. Ruff, formatting, strict mypy
  on four production files, diff integrity, and seven frozen hashes passed.
  Independent re-review remains active.
- 2026-08-03 reviewer broader preservation pass exposed one stale protocol
  registry expectation (`352 passed, 1 failed`): production already declared
  `InstallConsentExpired`, while the exact registry test omitted it. The test
  contract was repaired; focused protocol suite `123 passed` with Ruff,
  formatting, and diff integrity green. Broader reviewer rerun is pending.
- 2026-08-03 live signed-repair checkpoint: authority-free terminal consent
  expiry compiles and passes strict mypy plus its focused expiry test. It is not
  accepted or frozen. The independent reviewer still rejects the slice with no
  P0 and four P1 recovery classes: terminal consent expiry under target/policy
  drift, post-journal target drift, unclaimed post-grant action expiry, and
  target-independent settled-denial recovery.
- Full current dirty-working-tree gate: **run and green** on 2026-08-08.
- PR preflight: **run and green — all 19 of 19 lanes pass** on the current dirty
  working tree (see the 2026-08-08 evidence above).
- Native Windows evidence: **not run**.
- Official benefit experiment: **not run**; do not claim CTX superiority.

## Known risks and explicit non-claims

- P1 migration gap: direct writers outside `ManagedQueryService` do not all
  cooperate with its lifecycle lock and must be cut over before one-writer
  authority can be claimed globally.
- Signed-slice P1 recovery classes — status updated 2026-08-08: the four
  recovery classes previously tracked here as open (terminal consent expiry
  under target/policy drift, post-journal target drift, unclaimed post-grant
  action expiry, and target-independent settled-denial recovery) now have
  implementing code and green named covering tests, confirmed by an independent
  P1 audit and a focused-suite rerun (see the 2026-08-08 evidence above). The
  earlier signed-construction (eager `SQLiteEngineStore` before first use) and
  post-authentication-race (pre-auth binding reuse) repairs are exercised by the
  now-green focused signed suite. These are closed against focused evidence and
  independent review, but the slice is **not** formally accepted: a full
  adversarial acceptance re-review across the entire signed checklist and the
  full dirty-tree gate are still required, and one review-board skeptic aborted
  on a tooling retry cap rather than delivering a verdict, so the second
  independent fix-verdict is still outstanding.
- P1 capability gap: no generic physical MCP installation actuator exists.
- P1 platform gap: built-in skill and agent actuators remain POSIX-oriented.
- P1 material gap: user/organization graph material needs a trusted,
  claim-bound content source before generic installation.
- P1 lifecycle gap (partially closed 2026-08-08): the reducer emits
  `ActivateCapability` after verified installation. Slice 2.1 added a
  composition-integrated applied-activation seam
  (`EngineComposition.execute_activation` in `activation_execution.py`) so the
  lifecycle reach-in test no longer reaches engine-private activation methods.
  Still open: the public `ManagedQueryService.activate`/`recover_activation`
  continuation (slice 2.2) so hosts, not just the composition, can drive
  activation.
- P1 recovery gap: durable activation claims/settlement currently cover applied
  outcomes only; failed, expired, or indeterminate host activation can still
  wedge the stream. Closing this needs a store `outcome`-column migration on
  `engine_activation_outcomes`/`engine_activation_claim_settlements`, an
  `ActivationActionClaimGuard.mode`, a failed path in
  `_record_activation_outcome`, and relaxing the `ActionApplied`-only activation
  receipt assertion — scoped as slice 2.2 in
  `.scratch/managed-activation-slice/DESIGN.md`.
- P1 authority gap: deactivation `ActionApplied` receipts are not yet guarded by
  an exact durable host outcome claim.
- P1 evidence gap: exposure and invocation are distinct, but no typed
  opportunity/utility window is fed into authenticated benefit planning;
  current planning facts keep `opportunity_observable=False`.
- The prior P2 desired-result factory hardening gap is repaired and accepted.
- The presence of Codex/Claude adapter files does not prove real-host parity.
- Passing focused tests does not prove the complete repository, release,
  language, or product-benefit gates.

## Active work lanes

Keep only independent work parallel. A writer owns each shared file.

| Lane | Ownership | State | Completion condition |
| --- | --- | --- | --- |
| Managed desired-set writer | `managed_query_service.py`, runtime exports, service tests | Complete | Accepted after two review-driven repairs |
| Coordinator integration | Plan, state, aggregate checks, risk triage | Active | Drive signed resolution while preserving accepted automatic-execution invariants |
| Automatic-execution design | Read-only execution/receipt/recovery analysis | Complete | Reuses current engine/executor; no schema, store, broker, or request expansion required |
| Automatic-execution writer | Managed service, reducer, and focused tests | Complete and accepted | Cross-restart binding and stale-consent races repaired |
| Automatic-execution reviewer | Read-only service/engine/executor security and recovery review | Complete; accepted repaired freeze | No open P0/P1; independent adversarial reproductions closed |
| Independent desired-set reviewer | Read-only correctness/security/concurrency review | Complete | Accepted with no open P0/P1; prior P2 hardening item is now repaired |
| Signed-resolution design | Read-only broker/decision/execution continuation analysis | Complete | Implementation-ready safe public API and crash-recovery contract delivered to the active writer |
| Signed-resolution broker audit | Read-only broker/store/continuation review | Complete | Reauthentication repair verified; remaining generic resolver seams delivered to writer |
| Signed-resolution writer | Managed service, consent continuation, runtime exports, and focused tests | Complete (focused-green) | All four recovery classes implemented with green covering tests; the 2026-08-08 residual was a test-only stale-clock fix, not a writer gap |
| Signed-resolution reviewer | Read-only authentication/recovery/security acceptance review | Focused-green + partial acceptance; second fix-verdict outstanding | 2026-08-08 board found the four recovery classes closed with green named tests and the clock fix test-only-correct; one skeptic aborted on a tooling cap. The full dirty-tree gate has since run green (19/19 lanes). Remaining: obtain the second independent fix-verdict and run the full signed acceptance checklist before formal acceptance |
| Activation/lifecycle design | Read-only reducer/composition/service analysis | Complete | Implementation-ready API/event flow and current P0/P1 lifecycle defects recorded |

## Standing execution loop

For every material slice:

1. **Resume:** read `AGENTS.md`, the standing plan, and this file; inspect the
   current worktree and active lanes.
2. **Choose:** take the next item on the critical path; do not redefine the
   product around the easiest passing subset.
3. **Dispatch:** parallelize only bounded independent work; assign one writer per
   shared surface and keep architecture/integration with the coordinator.
4. **Implement:** add the smallest coherent end-to-end behavior plus focused
   success, failure, concurrency, privacy, and crash-recovery tests.
5. **Verify:** run focused tests first, then applicable aggregate/static checks.
   Stop on failure, diagnose, repair, and rerun.
6. **Review:** use an independent read-only reviewer for security, concurrency,
   state transitions, host mutations, migrations, or other material risk.
7. **Accept:** mark a slice accepted only when deterministic evidence and review
   agree. A worker's green report is evidence, not proof.
8. **Persist:** update this file with accepted behavior, exact commands/results,
   new risks, changed critical path, and next work before ending the run.

## Resume procedure

Start with:

```bash
git status --short
git branch --show-current
git rev-parse HEAD
sed -n '1,260p' AGENTS.md
sed -n '1,280p' STATE.md
sed -n '1,220p' docs/plans/unified-capability-engine.md
```

Then inspect the files and tests named by the first in-progress milestone. Do
not rely on a conversation summary when the worktree or this state file says
otherwise.

For the current slice, the focused verification command is:

```bash
.venv/bin/python -m pytest -q \
  src/tests/runtime/test_managed_query_service.py \
  src/tests/runtime/test_managed_query_desired_set_store.py \
  src/tests/runtime/test_managed_query_store.py \
  src/tests/runtime/test_managed_query.py \
  src/tests/runtime/test_composition.py \
  src/tests/runtime/test_install_consent_challenge_lookup.py \
  src/tests/runtime/test_install_consent_broker.py \
  src/tests/core/test_install_consent_broker_store.py
```

After the slice is frozen, run focused Ruff, formatting, and strict mypy; then
select broader gates according to `CONTRIBUTING.md` and the changed surface.

## Next actions

1. Formally accept the signed slice: the full dirty-tree PR preflight is green
   (19/19 lanes, 2026-08-08), so only the review side remains — obtain the
   still-outstanding second independent fix-verdict and run the full signed
   acceptance checklist. Accept only if independent review reports no open
   P0/P1. Native Windows and real interactive host evidence stay separate,
   currently unavailable gates and must not be implied by the local green run.
2. Continue managed activation. Slice 2.1 (applied `EngineComposition.execute_activation`
   seam) is done and reviewed. Next is slice 2.2: the store `outcome`-column
   migration on the activation outcome/settlement tables, `ActivationActionClaimGuard.mode`,
   a failed path in `_record_activation_outcome`, relaxing the `ActionApplied`-only
   activation receipt assertion, the failed/indeterminate driver observation, and
   the public `ManagedQueryService.activate`/`recover_activation` continuation
   (mirroring `resolve_consent`/`recover_consent`) with a generic non-release-skill
   activation verifier. Full plan in `.scratch/managed-activation-slice/DESIGN.md`.
3. Guard deactivation `ActionApplied` receipts with an exact durable host
   outcome claim (the open authority gap), then wire opportunity-aware cooling,
   deactivation, and uninstall proposals into the managed loop.
4. Continue through graph layers, relevance, real-host, compatibility, and
   controlled benefit proof without skipping gates.
