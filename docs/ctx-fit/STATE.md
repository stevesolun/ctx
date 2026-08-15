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

- Updated: 2026-08-16 (Asia/Jerusalem)
- Active goal: review, harden, verify, and release CTX Fit 1.0.21
- Phase: remote review of the pinned-attestation CycloneDX compatibility repair
- Release decision: **DO NOT RELEASE YET**
- Branch: `codex/ctx-fit-sbom-attestation`
- Base commit: `2714f9be88a1c3a3b680e344b25a962829d18ca8`
- Active repair commit: `b3145c49`
- Last merged release candidate: `2714f9be88a1c3a3b680e344b25a962829d18ca8`
- Latest merged PR: `https://github.com/stevesolun/ctx/pull/270`
- Follow-up scope at checkpoint:
  - deterministic content-derived CycloneDX serial binding for the pinned
    attestation action, exact compatibility regressions, and this checkpoint
  - user-owned and out of scope: `.scratch/`
- Parallel execution: PR #270 is merged; exact-main Tests run `31912497654`
  and CodeQL run `31912497612` are green. Publish run `31913053842` proved the
  PURL/closure repair, build, package smokes, and graph paths, then failed
  closed because the pinned attestation action requires CycloneDX's optional
  `serialNumber`. The narrow content-derived serial repair is committed as
  `b3145c49`; independent format and recovery reviewers accepted it with no
  P0-P2 findings, and its committed fast gate plus all 19 PR-preflight lanes
  are green. Remote review/merge is next.

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
| Final verification and release | Attestation compatibility repair ready for remote review | Coordinator + release/SBOM reviewers | PR #270 merged as `2714f9be`; exact-main Tests and CodeQL succeeded. Recovered annotated tag object `bcd5632d` points to that exact commit. Publish run `31913053842` passed the repaired SBOM normalization/strict closure gate and the complete build job. Package and graph provenance attestations were created, but the SBOM attestation failed because pinned `actions/attest@f7c74d...` requires an optional top-level CycloneDX serial; release/PyPI jobs skipped, GitHub release is absent, and PyPI is still 404. Commit `b3145c49` derives a deterministic RFC-4122 UUIDv5 from canonical normalized BOM content, validates exact equality, proves pinned-action recognition, and has independent acceptance plus green committed fast and 19-lane PR-preflight gates. PR, fresh exact-main CI, audited partial-attestation cleanup, and another explicit unpublished-tag recovery remain. |

## Open release blockers

### P0 — release-stopping

None in the current working tree. PR #267's earlier CodeQL finding was repaired
with owner-only (`0600`) applied manifests, independently accepted, and remote
CodeQL is green on commit `2cc8667d`.

ADR-016 itself has independent current-tree acceptance: repository-native
verification is explicitly a non-adversarial trust boundary.

### P1 — must resolve or explicitly de-scope before release

One active blocker remains at the remote-release boundary. The PURL repair is merged and passed its exact
production gate, but cyclonedx-bom 7.3.0 also removes `serialNumber` under
reproducible output. CycloneDX 1.6 permits that omission; pinned
`actions/attest@f7c74d...` has a narrower parser requiring truthy `bomFormat`,
`specVersion`, and `serialNumber`. Run `31913053842` therefore failed only at
the SBOM attestation. The active repair binds a canonical UUIDv5 derived from
the normalized BOM content with the serial omitted, so byte-identical BOMs are
reproducible and materially different BOMs get different identities. It
refuses conflicts and requires exact recomputation after strict dependency
closure validation. Focused regressions and exact uploaded-artifact schema/
parser probes are green; commit `b3145c49` passed the committed fast gate and
all 19 PR-preflight lanes. Review/merge and a new exact-main Tests success
remain mandatory before controlled tag recovery.

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
| Commit `10e47d37`, 2026-08-14 | committed fast gate + clean detached PR preflight | All 11 fast lanes and all 19 preflight lanes passed; 8,769 tests, 5 skipped, 92.16% coverage; wheel `03cdaac7...`, sdist `1b488e9e...`, Twine green | Exact implementation commit evidence; this state-only follow-up needs proportional revalidation before push |
| Commit `6ec9d9b2`, 2026-08-14 | proportional clean release/docs/package revalidation | 180 release/workflow/docs tests, strict MkDocs, stats, reproducible build, and Twine passed; wheel `4d3e5e3c...`, sdist `798d9582...` | Exact pushed candidate evidence before remote Ubuntu execution |
| PR #267 / commit `6ec9d9b2`, 2026-08-15 | required Ubuntu live-prerequisite lane | 3 positive sandbox checks failed before child start with `bwrap: loopback: Failed RTM_NEWADDR`; 7 negative checks were not valid denial evidence because the child never started | Release-stopping remote evidence; directly drove the AppArmor profile, child-start sentinel, and operational-preflight repair |
| Accepted Ubuntu repair tree, 2026-08-15 | sandbox/provider/doctor/workflow focused + full Fit + independent refreeze + static/docs | Coordinator: 64 focused and 513 full Fit passed. Independent reviewer: 513 Fit, 99 focused, 75 adjacent, docs/tracker 36; Ruff, format, mypy, workflow parsing, strict MkDocs, stats, and diff checks all passed; no P0-P2 | Accepted uncommitted evidence; committed gates and exact-SHA remote Ubuntu run remain |
| Commit `c4aaa22e`, 2026-08-15 | committed fast gate + clean detached PR preflight | All 11 fast lanes and all 19 preflight lanes passed; 8,774 tests, 5 skipped, 92.16% coverage; wheel `e9770d99...`, sdist `a0d86319...`, Twine green | Exact local release evidence; remote Ubuntu still required |
| PR #267 / commit `c4aaa22e`, 2026-08-15 | required Ubuntu live-prerequisite lane | AppArmor profile loaded and every child started; 8 passed, while a private-root sibling write and private POSIX-shm creation contradicted the old denial assertions | Release-stopping policy/evidence mismatch; directly drove private-root remount and same-name shm isolation proof |
| Linux private-root repair tree, 2026-08-15 | sandbox/provider/doctor/workflow focused + static | 64 passed; Ruff, format, mypy, and diff check passed; independent review found no P0/P1 | Fresh local evidence; committed gates and exact-SHA Ubuntu remain |
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

1. Push commit `b3145c49`, open the narrow serial-repair PR, and merge it only
   with required checks green.
2. Require a fresh successful exact-main Tests run. Archive and remove the
   transient Actions artifacts and deletable GitHub attestation records from
   run `31913053842`, recording that Rekor history remains. Reconfirm GitHub
   release absence and PyPI 404; then explicitly delete/recreate the annotated
   `v1.0.21` tag on the new exact tested main commit without a force-push.
3. Monitor production publish to completion, then verify PyPI, GitHub release
   assets/attestations, clean install, and Hugging Face synchronization before
   declaring the release complete.

## Checkpoint log

- 2026-08-16: Committed the independently accepted CycloneDX serial repair as
  `b3145c49`. Its committed fast gate passed 8,783 tests, 5 skips, 92.16%
  coverage, static checks, and clean-host/package contracts. The authoritative
  PR preflight then passed all 19 lanes, including the same 8,783-test unit
  equivalent, strict documentation, telemetry, similarity, browser security,
  clean-host installation, reproducible distribution build, and Twine.
  Reproducible artifact SHA-256 values are
  `40c8e88adb6ffcd6159ed30eddc25162db6e5e17bb6f0ac035ad2162857b70d7`
  for the wheel and
  `ce6fb0da869d8b3b5c6ff27327940f494b98f4c4efb34d466704983144368a6f`
  for the sdist. Only the user-owned `.scratch/` remains untracked; push and
  remote PR review are next.

- 2026-08-16: Merged PR #270 as exact main `2714f9be`; exact-main Tests run
  `31912497654` and CodeQL run `31912497612` succeeded. Explicitly deleted the
  still-unpublished old `v1.0.21` tag and created annotated object `bcd5632d`
  on that exact tested commit, without force-updating it. Publish run
  `31913053842` passed provenance, graph, stats, static/clean-host/canaries,
  reproducible build, distribution checks, wheel and all-runtime-extras smoke,
  deterministic PURL normalization, strict dependency closure, and artifact
  upload. Package/graph provenance attestations `40948323` and `40948329` were
  created, then the SBOM attestation failed closed: the valid CycloneDX 1.6
  file omitted optional `serialNumber`, while pinned `actions/attest` requires
  it. Release-assets and PyPI jobs skipped; GitHub release remains absent and
  PyPI 1.0.21 remains 404. The exact uploaded SBOM independently passes the
  official 1.6 schema. A failing-first repair now derives a content-addressed
  UUIDv5 serial, refuses conflicts, requires exact recomputation, and passes
  28 focused supply-chain tests plus the pinned action's three-field probe.
  Public inventory is now 8,780.

- 2026-08-16: Committed the accepted SBOM repair as `4ff0e890`. Its committed
  fast gate passed 8,779 tests, 5 skips, 92.16% coverage, static checks, and the
  clean-host contract. The authoritative PR preflight passed all 19 lanes:
  inventory/policy/static checks, the same 8,779-test unit equivalent, A-Z and
  compatibility canaries, clean-host installation, public docs contracts,
  strict MkDocs, telemetry, similarity, browser security, reproducible build,
  and Twine. Reproducible artifact digests were
  `748a0862e379a2dc0e8b8a671484225be588d80d9372e9412368377ac6cbfc32`
  for the wheel and
  `5c08024e5c88161ac7ed56f05edb4b7a26a91fc2861cc0188f0c98b96dac7a7e`
  for the sdist. Independent review accepted exact commit `4ff0e890` with no
  P0-P2 finding. Only the user-owned `.scratch/` remains untracked.

- 2026-08-16: Merged PR #269 as exact main `c1d8405b`; main Tests run
  `31910183189`, CodeQL, docs, and Hugging Face sync all succeeded. Created
  annotated `v1.0.21` tag object `2514da44` on that commit. Production publish
  run `31910688624` passed provenance, graph, stats, static, clean-host,
  canaries, reproducible build/package checks, wheel smoke, and all-runtime-
  extras smoke, then failed closed at SBOM validation: cyclonedx-bom 7.3.0
  omits the release root PURL. Zero workflow artifacts were
  uploaded; attest/release-assets/PyPI jobs skipped; GitHub release is absent;
  PyPI 1.0.21 remains 404. A failing-first generator-shaped regression now
  drives deterministic binding of the root and optional matching installed
  component without changing graph references; conflicting identities and
  duplicates fail closed. A fresh wheel-only all-runtime-extras environment
  reproduced the production root-only shape: both pinned-generator outputs
  passed strict closure validation after binding and remained byte-identical.
  The integrated release/workflow selector passed 190 tests, and the generated
  public inventory is now 8,776.

- 2026-08-16: Committed the deterministic inventory repair as `ca2b5799`.
  Its committed fast gate passed all lanes, including 8,775 tests, 5 skips,
  92.16% coverage, static checks, clean-host, and package contracts. The clean
  PR preflight passed all 16 lanes; reproducible wheel SHA-256 is `07bbd80a...`,
  reproducible sdist SHA-256 is `5f224abd...`, and Twine passed. Independent
  refreeze repeated 206 release/docs/workflow tests and proved the normalized
  8,772 inventory in both full-extra and fresh clean `[dev]` environments, with
  no P0-P2 findings. Only remote review/merge and new exact-main evidence remain
  before tagging.

- 2026-08-16: PR #267 merged as exact main `7cf8fb62`. Its main-push Tests run
  `31908356448`, CodeQL, Linux/macOS matrices, package/build lanes, and required
  zero-spend Ubuntu sandbox job all succeeded. Published and independently
  re-downloaded the validated graph cache prerelease
  `graph-artifacts-v1.0.21-7cf8fb62`; full/runtime/catalog assets match their
  committed size and SHA-256 contracts. Hugging Face retry hydrated from that
  cache, then exposed a release-stat environment dependency: 8,795 tests with
  optional extras versus 8,771 under clean `[dev]`. A failing-first regression
  now drives AST-based module-level `importorskip` normalization; focused tests
  and static checks pass, and the added regression makes the invariant public
  count 8,772. No `v1.0.21` tag, GitHub release, or PyPI version exists yet.

- 2026-08-15: Commit `c4aaa22e` passed all 11 committed fast lanes and all 19
  clean detached PR-preflight lanes, including 8,774 tests, 5 skips, 92.16%
  coverage, reproducible wheel `e9770d99...`, reproducible sdist `a0d86319...`,
  and Twine. Required Ubuntu job `94903820786` then started every Bubblewrap
  child and passed 8 of 10 adversarial checks. The two failures exposed an
  evidence-model distinction: writes beside the workspace and POSIX shm were
  private to Bubblewrap's new tmpfs/dev mounts, not host mutations. The active
  repair enforces the stronger host-backed single-writable-tree policy with a
  non-recursive root remount and proves Linux shm isolation by using the same
  name inside and outside the sandbox; macOS denial remains unchanged. Focused
  behavior/static checks and independent review are green; commit, gates, and
  exact-SHA Ubuntu remain.

- 2026-08-15: Required Ubuntu run `31840406705` reached the real Bubblewrap
  boundary and failed with AppArmor denying loopback setup. Three positive
  checks failed; seven negative tests had not proved their child started. The
  accepted repair loads the packaged capability-stripping
  `bwrap-userns-restrict` profile without disabling the global restriction,
  adds an exact child-start sentinel to isolation tests, and makes doctor/live
  setup execute a bounded no-network canary before any campaign. Public Ubuntu
  instructions preserve existing administrator policy and disclose that the
  profile applies to all `/usr/bin/bwrap` callers. Coordinator evidence is 64
  focused and 513 full Fit tests; independent refreeze repeated 513 Fit, 99
  focused, and 75 adjacent tests plus static/docs checks with no P0-P2. Exact
  committed gates and remote Ubuntu evidence remain.

- 2026-08-14: Committed Linux/provider/CI remediation as `10e47d37`. Its
  committed-history fast gate passed all 11 lanes in 323.45 seconds; the clean
  detached PR preflight passed all 19 lanes, including 8,769 tests, 5 skips,
  92.16% coverage, reproducible wheel `03cdaac7...`, reproducible sdist
  `1b488e9e...`, and Twine. Recording that result creates this state-only
  follow-up; proportionate exact-SHA checks and remote required CI remain.

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
