# CTX Fit — Live Working State

> **This is the canonical operational checkpoint for current CTX Fit work.**
>
> Read `AGENTS.md` and `docs/ctx-fit/DECISIONS.md` first. Then read this file
> before resuming a long-running task. The root `STATE.md` is frozen history
> from the superseded unified-capability-engine goal and is not live state.
>
> This file records where the work is. Code, tests, and accepted decisions
> remain the source of truth for what the product does and where it is going.

## Checkpoint

- Updated: 2026-08-14 (Asia/Jerusalem)
- Active goal: review, harden, verify, and release CTX Fit 1.0.21
- Phase: final committed verification of Linux CI remediation
- Release decision: **DO NOT RELEASE YET**
- Branch: `codex/ctx-fit-release-hardening`
- Base commit: `bd36bbea591495f8ef498a6818e7ec541fe78ebb`
- Committed release candidate: `2cc8667d53a7ca39af18e22e5d9c2727045f07ca`
- Community PR: `https://github.com/stevesolun/ctx/pull/267`
- Working tree at checkpoint:
  - reviewed Linux/provider/CI remediation is ready for a new commit
  - user-owned and out of scope: `.scratch/`
- Parallel execution: the Linux semantic-test, provider/pricing, and zero-spend
  Ubuntu CI lanes are complete. Independent integration review accepted the
  combined tree with no P0-P2 findings; the coordinator owns final gates.

## Product destination

CTX Fit must find the cheapest capability configuration that reliably completes
representative work in the user's repository, using repository-native
verification rather than an agent's self-report. It must present evidence, keep
"current setup" as a valid result when that claim is actually supported, and
produce a reviewable change that reproduces the winning configuration.

The current release is limited by ADR-007 to capability configuration within a
single harness. It must not claim to compare Codex, Claude Code, or other
harnesses when it did not run that experiment.

## Definition of done for 1.0.21

All of the following must be true before tagging or publishing:

1. A production trial can edit only its throwaway workspace with the intended
   tools, and an untrusted repository cannot read ambient secrets or mutate the
   host outside that workspace.
2. The evaluated agent cannot change the verification judge or any file outside
   the task's explicit editable set and still earn a verified result.
3. No public API or CLI path can spend without an executable plan, an explicit
   budget, and user authorization after a pre-spend preview.
4. Baseline, candidate, dry-run, recommendation, and apply/PR output describe
   the configuration that is actually evaluated. Applying a winner reproduces
   it rather than writing internal IDs only.
5. Incomplete or one-sided experiments return no verdict. CTX-imposed budget
   truncation is inconclusive and remains auditable.
6. Representative task derivation works for the supported language set, or the
   release surface states a narrower, truthful support contract.
7. Focused tests, static checks, the fast gate, PR preflight, package smoke, and
   the exact release commit's required CI checks are green on the final tree.
8. Public docs, changelog, package metadata, and GitHub release notes describe
   1.0.21 truthfully. The release tag points to the exact reviewed green commit.

## Work status

| Workstream | State | Owner | Current evidence / next condition |
| --- | --- | --- | --- |
| Merge the reviewed CTX Fit base | Complete | Coordinator | PR #266 merged; this hardening branch starts at `bd36bbea` |
| Independent product review | Complete | Product reviewer | Verdict: do not release; 4 P0 and 7 P1 findings recorded below |
| Production agent editing surface | Complete, independently accepted | Coordinator + independent reviewer | Production trials expose only a workspace-rooted filesystem MCP through the shared sandbox, use a scrubbed environment, deliver exact candidate material, and now refuse missing harness dependencies before trial setup; the integrated 508-test Fit suite and Linux/provider refreeze are green |
| Repository sandbox and secret isolation | Complete, independently accepted | Sandbox writer + coordinator + independent reviewer | Exact executable/symlink paths cannot expose sibling trees, trusted runtime subtrees remain usable, repository setup/verification stays network-disabled, and provider authority remains separate. Final macOS refreeze passed 84 focused tests plus real child/installed-CTX compatibility and static checks with no P0-P2 findings |
| Verification-judge integrity | Complete, independently accepted | Coordinator + architecture reviewer | The forgeable Python witness was removed. ADR-016 defines the repository command as an explicit, non-adversarial trust boundary; exact Python/JS/TS/Go/Rust/Make commands run unchanged, verification writes are confined to one trial workspace, and the assumption appears before spend and in every result. Independent review found no P0-P2 |
| Spend authorization and preview | Complete, independently accepted | Coordinator + independent reviewer | Immutable digest-bound plans, human pre-spend preview, JSON plan-only behavior, strict simulator identity, exact caps, and honest observed over-cap accounting passed 106 focused tests plus independent adversarial review |
| Fair campaign completion | Complete, independently accepted | Coordinator + independent reviewer | Exact candidate/task/trial/floor identity, no partial verdicts, strict numeric and simulation handling, and full-precision selection have no remaining reviewer findings |
| Truthful current baseline | Complete, independently accepted | Coordinator + independent reviewer | Simple installed skills are exact content-addressed material; invalid/complex skills and unreproducible agent/MCP/tool configurations abstain. Baseline drift is checked before authorization, before spend, and after the campaign; 222 focused tests and static checks passed in independent refreeze |
| Reproducible apply/PR result | Complete, independently accepted | Apply writer + independent reviewer | Sidecar-only `.ctx/fit-configuration.json`; immutable exact materials, instruction preimage checks, CAS, symlink refusal, transactional rollback, and PR staging passed 91 focused tests plus Ruff/mypy and independent review |
| Multi-language task derivation | Complete, aggregate green | Multi-language writer + coordinator | Python, JavaScript, TypeScript, Go, and Rust paired source/test history contract; 446-test Fit aggregate passed |
| Doctor/runtime truth | Complete, independently accepted | Doctor writer + coordinator + independent reviewer | Live selection and credential forwarding are bound to the exact selected model; unused or mismatched keys cannot authorize live execution, exact-model pricing and invalid sidecars fail closed, and independent refreeze passed 156 focused tests plus static/read-only checks |
| Applied winner activation | Complete, independently accepted | Activation writer + coordinator + independent reviewer | Strict repository-root loader, nested-sidecar refusal, hash/model validation, one-use exact context, subdirectory activation, and explicit model-conflict handling passed independent refreeze as part of the 222-check baseline/activation lane |
| ADR-015 stop attribution | Complete, independently accepted | Coordinator + spend/fairness reviewer | Structured stop reason/log fields flow provider → live runner → serialized result, budget-capped trials are inconclusive, and the accepted spend/fairness lane plus 446-test Fit aggregate are green |
| Release metadata and publish guard | Complete, independently accepted | Metadata writer + publish writer/reviewer + coordinator | 1.0.21 metadata and changelog are current; exact-main/exact-successful-Tests production guard and changelog-backed notes have no P0/P1 review findings; P2 credential/doc hardening is integrated |
| Final verification and release | PR open; repaired tree independently accepted | Coordinator + expert writers/reviewer | The original Linux failures are repaired without weakening fail-closed behavior. A new required zero-spend Ubuntu lane installs `[harness]` plus Bubblewrap, runs ten real isolation checks, and constructs but never invokes the live driver. Full Fit is 508 passed; workflow contracts are 137 passed. Commit, committed gates, and remote Ubuntu evidence remain |

## Open release blockers

### P0 — release-stopping

None in the current working tree. PR #267's earlier CodeQL finding was repaired
with owner-only (`0600`) applied manifests, independently accepted, and remote
CodeQL is green on commit `2cc8667d`.

ADR-016 itself has independent current-tree acceptance: repository-native
verification is explicitly a non-adversarial trust boundary.

### P1 — must resolve or explicitly de-scope before release

None in the reviewed working tree. PR #267's earlier `unit-linux` failure is
addressed by host-independent semantic tests, deterministic default-provider
and release-verified default-price resolution, pre-trial refusal when the
optional harness is absent, and a separate required Ubuntu lane that exercises
the real Bubblewrap and `[harness]` prerequisites without model credentials.
That new required lane must pass remotely on the exact committed SHA before the
release is eligible.

The release also deliberately discloses that qualification did not include a
paid provider call or a complete provider-plus-filesystem-MCP launch on this
host (which lacks `npx`). Missing `npx` fails closed. These are evidence limits,
not claims the release makes.

External release settings remain a P2 operational risk: observed `main` and
the `pypi` environment have no server-side protection rules. The workflow now
fails closed unless the tag is the exact current `main` head with a successful
exact-SHA Tests run, but repository settings should still add reviewer/tag
protection after this release.

## Verification ledger

Evidence is valid only for the tree named in the row. Any edit to a covered
surface makes that row stale for release purposes.

| Tree / date | Check | Result | Release use |
| --- | --- | --- | --- |
| Pre-hardening tree, 2026-08-13 | `src/tests/fit` | 307 passed | Baseline only; stale after current edits |
| Pre-hardening tree, 2026-08-13 | package/surface/clean-host focused contracts | 62 passed | Baseline only; stale after current edits |
| First ADR-015 edit, 2026-08-13 | focused budget-stop tests | Passed | Partial behavior evidence only |
| First ADR-015 edit, 2026-08-13 | Ruff on changed ADR-015 files | Passed | Partial static evidence only |
| Current hardening tree, 2026-08-13 | focused judge integrity, budget-cap, log-retention, and ambient-secret selector | 6 passed | Local behavior evidence; aggregate and independent review still required |
| Current hardening tree, 2026-08-13 | Python compile + Ruff on sandbox/live-runner/execution surfaces | Passed | Local static evidence; provider integration is not complete |
| Current hardening tree, 2026-08-13 | direct sandbox adversarial tests | 3 passed | Proves this macOS host denies sibling write and ambient-temp read while allowing workspace writes |
| Current hardening tree, 2026-08-13 | provider boundary/translation suite | 20 passed | Shared boundary invocation, least-authority environment, tool surface, and structured result translation |
| Current hardening tree, 2026-08-13 | environment reuse integration + focused security selector | 7 passed | Dependency setup network split and workspace re-aiming are green on this host |
| Spend/fairness writer tree, 2026-08-13 | owned focused tests | 89 passed | Writer evidence only; independent review pending |
| Multi-language writer tree, 2026-08-13 | task derivation focused tests | 23 passed | Writer evidence only; aggregate pending |
| Exact-apply draft, 2026-08-13 | focused apply/candidate tests | 63 passed | Insufficient: independent reviewer rejected after 4 adversarial preservation/correctness probes failed |
| Spend/fairness draft, 2026-08-13 | focused reviewed files | 73 passed, 16 failed on shared candidate-fixture drift | Independent reviewer rejected with six adversarial classes; repair active |
| Later exact-apply draft, 2026-08-13 | focused apply/candidate tests + static checks | 87 passed; Ruff, format, mypy, diff check passed | Independent reviewer still rejected AGENTS mutation; sidecar-only repair active |
| Accepted sidecar-only apply, 2026-08-13 | focused apply/candidate tests + static checks + independent refreeze | 91 passed; Ruff, format, mypy, diff check passed; reviewer accepted | Valid lane evidence; aggregate still required |
| Integrated exact baseline/activation tree, 2026-08-13 | candidate/profile/experiment/provider/live/activation/harness CLI selector | 282 passed; source static checks passed before later spend edits | Strong local integration evidence; stale after spend changes and independent security refreeze active |
| Current spend integration tree, 2026-08-13 | execution/recommend/experiment/budget/Fit CLI selector | 100 passed; Ruff, format, mypy, diff check passed | Fresh writer/coordinator evidence; independent final refreeze active |
| Accepted spend/fairness tree, 2026-08-13 | execution/recommend/experiment/budget/Fit CLI selector + adversarial probes | 106 passed; Ruff, format, mypy, diff check passed; reviewer accepted | Accepted semantic lane evidence |
| Current verifier-witness tree, 2026-08-13 | `src/tests/fit/test_live_runner.py` + source static | 33 passed; Ruff, format, mypy, diff check passed | Writer/coordinator evidence; independent refreeze active |
| Rejected verifier-witness tree, 2026-08-14 | independent adversarial probes + focused regression selector | Existing 33 tests/static green, but 2 new attacks earn `verified`; selector is 2 passed/2 failed | Release-stopping red evidence; proves the previous witness is not an authority boundary |
| Current integrated Fit tree, 2026-08-13 | full `src/tests/fit` suite | 440 passed in 80.12s | Fresh integrated behavior evidence; later code edits invalidate covered surfaces |
| Truthful-dimensions repair, 2026-08-14 | profile + dry-run focused behavior/static | 17 passed; Ruff, format, mypy passed | The experiment now claims only skill-capability variation; aggregate still required |
| ADR-016 verifier-boundary repair, 2026-08-14 | live runner + plan/result/disclosure focused behavior | 58 passed after one prose-regression correction; live runner alone 32 passed; Ruff/format/mypy/diff passed | Exact native commands and workspace-only write boundary implemented; independent refreeze active |
| Accepted ADR-016 tree, 2026-08-14 | independent verifier/profile/task/sandbox/release-surface refreeze | 186 focused checks passed; real editable-install and campaign-reuse paths passed; Ruff/format/mypy/diff passed | Independent reviewer accepted with no P0-P2 |
| Current full non-integration tree, 2026-08-14 | repository-wide parallel pytest excluding browser/integration | 8,706 passed, 5 skipped, 1 tracker-attribution failure | Implementation evidence green; the sole governance failure named the two new Fit modules and was repaired immediately afterward |
| Tracker-repaired tree, 2026-08-14 | feature/bug/dashboard/toolbox tracker contracts | 36 passed | Canonical FIT-001 now attributes `applied_configuration.py` and `sandbox.py`; repository-wide pytest rerun still required |
| Tracker-repaired pre-refreeze tree, 2026-08-14 | repository-wide parallel pytest excluding browser/integration | 8,707 passed, 5 skipped | Full behavior evidence was green before the independently found baseline and read-boundary P0 repairs; stale for the final release tree |
| First-use baseline red/green slice, 2026-08-14 | exact installed repository skill appears in baseline material/context | Failed against empty-control implementation, then passed after exact materialization; candidate module 33 passed | Focused writer evidence only; unsafe/unreproducible layouts, aggregate, static, and independent refreeze remain |
| Applied-model activation repair, 2026-08-14 | applied/profile CLI selectors + static | 32 applied tests and 34 combined checks passed; Ruff, format, mypy passed | Writer evidence only; aggregate and independent refreeze remain |
| Accepted baseline/activation tree, 2026-08-14 | candidate/applied/provider/experiment/budget/apply/CLI selectors + static | 222 passed; Ruff, format, mypy, diff check passed; reviewer accepted | Exact current baseline, drift guards, repository-root activation, model binding, and safe-read availability have no P0/P1 findings |
| Current integration tree, 2026-08-14 | baseline/activation/apply/experiment/discovery/live-runner/sandbox/provider selector | 364 passed | Fresh integrated behavior evidence after the baseline and sandbox repairs |
| Current integration tree, 2026-08-14 | full `src/tests/fit` suite | 478 passed in 69.95s | Fresh Fit behavior evidence; final release still requires repository-wide and static gates |
| Accepted doctor/runtime-truth tree, 2026-08-14 | model-aware CLI/doctor/provider/budget/applied/experiment/profile tests + static/read-only probe | 156 passed; Ruff, format, mypy, diff check passed; reviewer accepted | Exact selected-model credential and pricing truth has no remaining material finding |
| Post credential/process repair tree, 2026-08-14 | critical sandbox/live/provider/doctor/budget/activation/experiment selector + static | 196 passed; Ruff, format, mypy, diff check passed | Fresh coordinator evidence, superseded for sandbox release use by the later exact-path refreeze finding |
| Post credential/process repair tree, 2026-08-14 | full `src/tests/fit` suite | 497 passed in 79.89s | Fresh integration evidence, but sandbox exact-path repair will require rerun |
| Accepted exact-path sandbox tree, 2026-08-14 | sandbox/live-runner/provider focused behavior + real macOS compatibility + static | 17 sandbox, 41 live-runner, and 26 provider tests passed; Ruff, format, mypy, diff check passed; reviewer accepted | Exact-file/runtime-subtree authority split and provider separation have no P0-P2 findings; Linux remains structural and no paid model call occurred |
| Current settled Fit tree, 2026-08-14 | full `src/tests/fit` plus public surface truth | 522 passed in 71.20s | Fresh settled behavior evidence; repository-wide, packaging, and committed-history gates remain |
| Base release audit, 2026-08-13 | release-contract suite | 201 passed | Baseline only; docs/code edits require rerun |
| Base `bd36bbea`, 2026-08-13 | reproducible wheel/sdist, manifest, and Twine checks | Passed twice | Baseline only; final-tree artifacts will have different hashes |
| Base `bd36bbea`, 2026-08-13 | GitHub Tests run `31703885499` | Completed successfully | Proves the merged base only; final tag SHA needs a fresh green run |
| Final uncommitted tree, 2026-08-14 | CI-shaped full non-integration suite | 8,761 passed, 5 skipped, 15 deprecation warnings in 262.33s | Green exact-tree behavior evidence. A prior parallel attempt had one deterministic-bridge request-count miss; the isolated test passed three times and this complete rerun passed |
| Final uncommitted tree, 2026-08-14 | Ruff check/format, mypy, strict MkDocs, trackers/release/package surfaces, repo stats, diff integrity | Ruff green across `src hooks scripts`; 615 files formatted; mypy green on 585 files; MkDocs strict green; 135 focused contracts passed; stats and diff checks green | Required uncommitted static/documentation/release evidence complete |
| Commit `0264cede`, 2026-08-14 | `scripts/no_mistakes_run.sh fast --allow-dirty` | All 11 lanes passed in 356.89s; committed-head-only report | Valid for `0264cede`; superseded after the CodeQL repair is amended |
| Commit `0264cede`, 2026-08-14 | `python scripts/ci_preflight.py --profile pr` from a clean detached worktree | All 19 lanes passed; 8,761 tests, 5 skipped, 92.15% coverage; reproducible artifacts and Twine green | Valid for `0264cede`; superseded after the CodeQL repair is amended |
| PR #267 / commit `0264cede`, 2026-08-14 | GitHub PR checks | Product/build/clean-host/static/docs/CodeQL-Python lanes green; aggregate CodeQL rejected one high world-readable-manifest alert | Release-stopping remote evidence; local repair active |
| CodeQL permission repair working tree, 2026-08-14 | exact new-manifest mode regression, full apply/Fit/surface suites, docs/stats, Ruff/format/mypy, independent probes | Regression failed at `0644`; 64 apply tests, 523 Fit/surface tests, docs/stats, and static checks passed with `0600`; reviewer verified create/modify/rollback under `umask 000` and accepted with no P0-P2 | Accepted repair evidence; new committed and remote gates required |
| Commit `2cc8667d`, 2026-08-14 | committed fast gate + clean detached PR preflight | All 11 fast lanes and all 19 preflight lanes passed; 8,762 tests, 5 skipped, 92.15% coverage; wheel `9986b8c6...`, sdist `099825fd...`, Twine green | Exact macOS/local release evidence; remote Linux still required |
| PR #267 / commit `2cc8667d`, 2026-08-14 | GitHub CodeQL and Tests | CodeQL aggregate plus 15 specialized checks passed; `unit-linux` failed 8 cases (2 missing Bubblewrap, 6 missing optional harness metadata/pricing), causing aggregate CI failure | Release-stopping Linux environment-contract evidence; parallel repair active |
| Reviewed Linux/provider remediation tree, 2026-08-14 | full Fit + Linux/provider/CLI + no-LiteLLM + workflow/CI/docs contracts | 508 Fit passed; 146 targeted passed; no-LiteLLM 72 passed/16 expected skips; workflow/CI 137 passed; docs tracker 36 passed; Ruff/format/mypy/YAML/embedded-Python/diff green | Independent integration reviewer accepted with no P0-P2; remote Ubuntu lane remains the authoritative Linux execution evidence |
| Exact tag target | required GitHub CI and package/publish smoke | Not run | Required |

No paid real-provider evaluation has been authorized or run during this
hardening pass. A simulated or injected driver is not evidence of live provider
quality. Any paid canary requires a separately stated budget and consent.
TestPyPI does not yet contain a `claude-ctx` project, so its first Trusted
Publisher path is also unproven. Do not consume the 1.0.21 TestPyPI filename on
anything except the final release candidate because published filenames cannot
be overwritten.

## Execution and review loops

### Resume loop

1. Read `AGENTS.md`, `docs/ctx-fit/DECISIONS.md`, and this file.
2. Inspect the actual branch, commit, working tree, and active agents. Preserve
   user-owned files and do not assume this checkpoint is newer than Git.
3. Reconcile this file with code and test evidence. If they disagree, code,
   tests, and accepted decisions win; update this file immediately.
4. Take the highest-severity unblocked item. Do not duplicate an active writer's
   files.
5. Update this checkpoint after a meaningful repair, new blocker, verification
   result, handoff, commit, merge, or release action.

### Implementation loop

1. Reproduce the defect with the smallest failing test or deterministic probe.
2. Make the smallest coherent repair within one owned surface.
3. Run the focused test and applicable static checks on the actual changed
   tree.
4. Send the repair through an independent semantic review when it affects
   security, spend, recommendation validity, apply behavior, or release flow.
5. Record the result and residual risk here; a writer's self-report alone is
   never verification.

### Parallel dispatch rules

- The coordinator owns scope, dependency decisions, integration, state, and the
  final release decision.
- Writers receive disjoint files and bounded outcomes. Shared files have one
  owner at a time.
- Reviewers do not silently repair the code they are judging.
- Expensive gates wait until focused failures are closed, so time and compute
  are not wasted proving a known-bad tree is bad.

### Release loop

1. Close every P0 and resolve or truthfully de-scope every P1.
2. Rerun focused tests, then Fit, full non-integration, Ruff, and mypy.
3. Commit the reviewed tree and run the fast and PR-preflight gates against that
   exact history.
4. Open and review the hardening PR. Merge only with required checks green.
5. Verify the resulting `main` SHA and package metadata; tag that exact SHA.
6. Publish through the tag workflow, verify PyPI and GitHub release artifacts,
   and record digests and links here.

## Immediate next actions

1. Commit the independently accepted Linux/provider/CI remediation.
2. Run the committed fast gate and clean PR preflight against that exact SHA.
3. Push PR #267 and require `unit-linux`, the new zero-spend live-prerequisite
   Ubuntu lane, CodeQL, and every aggregate check to be green.
4. Merge only after review, then build, tag, and publish from the exact green
   `main` SHA.

## Checkpoint log

- 2026-08-14: Repaired all eight `unit-linux` failures without weakening
  production boundaries. Semantic live-runner tests now inject their executor;
  the default model's provider and official release-verified price resolve
  without optional LiteLLM; missing `[harness]` refuses before trial setup; and
  a new required zero-spend Ubuntu lane installs `[harness]` plus Bubblewrap,
  runs ten real adversarial sandbox checks, and constructs without invoking the
  live driver. Full Fit passed 508 tests, workflow/CI contracts passed 137, and
  independent integration review accepted with no P0-P2. Generated inventory
  is current. Release remains stopped for commit, committed gates, and remote
  Ubuntu evidence.

- 2026-08-14: Security commit `2cc8667d` passed all 11 committed fast lanes and
  all 19 clean PR-preflight lanes; both remote CodeQL checks and 15 specialized
  PR checks passed. Remote `unit-linux` then failed eight environment-contract
  cases: two reached a missing Bubblewrap boundary and six expected optional
  LiteLLM harness metadata/pricing absent from `[dev]`. The required CI
  aggregate consequently failed. Dispatched non-overlapping Linux-sandbox and
  provider/pricing expert repair lanes; release remains stopped.

- 2026-08-14: Committed release candidate `0264cede` passed all 11 committed
  fast-gate lanes and all 19 clean PR-preflight lanes, including 8,761 tests,
  reproducible package construction, Twine, clean-host, docs, and static checks.
  Opened community PR #267. Remote CodeQL then found one high-severity
  world-readable applied-manifest permission issue. Captured a failing-first
  regression and repaired new manifest creation from `0644` to owner-only
  `0600`; 64 apply tests, 523 Fit/surface checks, docs/stat checks, and focused
  static checks pass. An independent reviewer accepted create/modify/rollback
  behavior with no P0-P2 findings. Release is stopped pending a new commit,
  repeated committed gates, and green remote CodeQL.

- 2026-08-13: Created this live state file after confirming that root
  `STATE.md` is intentionally frozen history. Recorded the merged base,
  independent release verdict, active parallel lanes, blockers, evidence
  freshness, required loops, and release stop condition.
- 2026-08-13: Reconciled the checkpoint with the shared worktree and active
  agents. Rejected the production driver's completion claim after a direct
  `codex sandbox :workspace` probe wrote to a sibling temporary directory.
  Added a stricter shared process boundary and recorded 6 focused security and
  audit regressions plus compile/Ruff evidence; provider integration and
  independent review remain open.
- 2026-08-13: Replaced the escaped production-driver wrapper with the shared
  host boundary. Direct adversarial probes now deny sibling writes and ambient
  temporary-secret reads; provider and environment-reuse focused suites are
  green. Separated dependency-setup network authority from network-disabled
  verification. Received spend/fairness and five-language task lanes for
  independent integration review; exact apply materialization remains active.
- 2026-08-13: Independent apply review rejected the first exact-material draft
  despite 63 green focused tests. Added red repair requirements for ambiguous
  marker refusal, stale-preview compare-and-swap, byte-preserving newlines,
  canonical material identity, complete treatment manifests, provider use of
  those exact bytes, and an actual run-time consumer of the applied sidecar.
- 2026-08-13: Independent spend/fairness review rejected the first repair.
  Began sealing authorization to the canonical executable plan, restricting
  simulation to the built-in runner, refusing invalid/over-cap costs, making
  JSON plan-only, expanding the human preview, and requiring an exact
  baseline-versus-challenger field with every declared trial slot complete.
- 2026-08-13: Reconciled this checkpoint against the live branch and agent
  roster. Recorded the completed five-language task lane, the active strict
  applied-configuration consumer, and the independent rejection of AGENTS
  mutation because it changes the winner's instruction preimage. The apply
  repair is now sidecar-only; activation and integrated exact-byte evidence are
  still open. No tag, package publication, or paid provider evaluation has
  occurred.
- 2026-08-13: Integrated the accepted sidecar-only apply result and strict
  ordinary-run activation. Added an exact applied baseline, a controlled-trial
  suppression flag to prevent an existing sidecar contaminating every Fit arm,
  and one canonical renderer for trial and applied instruction/capability
  bytes. A 282-test integration selector was green before later spend edits.
- 2026-08-13: Reopened spend/fairness after independent adversarial refreeze
  found unsigned reliability, duplicate identity, exact-cap, unknown-cost,
  precision, and simulator-subclass defects. Bound floor/task/report structure
  into execution and recommendation, removed report-digest relabelling, and
  started reconciling strict fixtures. Current selector: 83 passed / 15 failed.
  Two independent reviewers are now active in parallel; no full gate, tag,
  publication, or paid provider call has been attempted.
- 2026-08-13: Reconciled strict comparison and sidecar-only CLI fixtures and
  closed the reviewer repros for exact caps, unknown/invalid/over-cap cost,
  plan/task/floor binding, bool slot identity, simulation identity, and
  full-precision selection. Focused spend selector is now 100 passed with
  Ruff/format/mypy/diff checks green; independent final refreeze is active.
- 2026-08-13: Independent activation/security review found a new P0: editable
  source could terminate the repository verifier successfully before tests
  completed and receive `verified`. A separate writer now owns only the live
  runner and its focused tests; the coordinator continues release integration
  in parallel. Release remains stopped.
- 2026-08-13: Spend/fairness received final independent ACCEPT after 106 focused
  tests and direct adversarial probes; no findings remain in that lane. The
  integrated Fit suite is now 440 passed. The verifier completion-witness
  repair is 33 tests/static green and under independent refreeze; non-Python
  verification currently fails closed pending an honest adapter design.
- 2026-08-14: Completed a tracker-repaired repository-wide checkpoint with
  8,707 passed and 5 skipped, then correctly marked it stale when independent
  refreeze found two new P0s: the first-use baseline omitted installed
  capabilities, and the sandbox exposed ambient host reads/local sockets. A
  failing-first baseline regression now passes with exact simple repository
  skill material; unreproducible current agents/MCP/tool layouts abstain. The
  applied-model P1 repair is focused-green, while sandbox and multi-language
  dependency-scope writers continue in parallel. Release remains stopped.
