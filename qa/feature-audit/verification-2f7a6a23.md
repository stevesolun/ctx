# Audit verification checkpoint — 2026-09-30

Implementation commit: `2f7a6a23d7d6f80acc54c7c09543048367da5bc9`.
This is an evidence record, not a second feature tracker. Canonical status is
`qa/feature_status.csv`. **The gate is not green and the audit is not complete.**

## Committed fast gate

Command: `PYTEST_ADDOPTS=-ra scripts/no_mistakes_run.sh fast --allow-dirty --jobs 4`.
The only untracked work at selection was the preserved user-owned `.scratch/`.
Each lane checked the exact commit in a detached, isolated checkout.

Ten lanes passed: cheap policy/stats, static checks, canary, compatibility,
clean-host, documentation, telemetry, similarity, browser, and packaging.
The unit lane finished **8,976 passed, 5 skipped, 1 failed** in 302.47 seconds;
coverage was 92.00%. Total unit-lane time including hydration was 369.2 seconds.

Failure:
`src/tests/test_ctx_ab_deterministic_pair.py::test_deterministic_bridge_pair_uses_current_delivery_and_exact_provider_delta`.
The context arm produced zero provider records, so the exact-one-request guard
correctly refused the result. Its retained child output reports
`stop_reason=provider_timeout`, `detail=provider call timed out after 5.000s`.
The child stderr was empty. This is not a passing benchmark and is not yet
attributed to either a timing-only fixture or a production defect. A focused
diagnosis is in progress; no retry has been used to erase this failure.

Later diagnosis traced the timer through `_complete_provider` into the deferred
LiteLLM import. In 32 fresh-process imports, the writer measured a 4.086-second
median and 4.626-second maximum before any HTTP request. The successful frozen
baseline itself spent 3.326 seconds in the provider call. A five-second bound
therefore conflates cold dependency startup with loopback-provider performance.
The proposed repair changes only the real adapter test to an explicitly approved
15-second provider bound; its 30-second subprocess bound and exact-request,
delivery, usage, cost, and drift assertions are retained. All other contract
fixtures retain five seconds. This later test delta is outside `2f7a6a23` and
requires independent acceptance and final committed verification.

Independent review subsequently accepted the fixture-only repair with no
findings: the exact regression passed in 8.48 seconds and all 23 tests in its
file passed. The reviewer confirmed both arms bind the same timeout into the
approval digest and retain the exact guards. Final committed gates are still
required; this does not relabel the earlier failed run.

Five skips: one real-user global wiki-schema opt-in and four native Windows
contract cases. macOS/Linux is the supported platform surface.

Other gate results include 19 browser tests, 31 documentation tracker tests,
24 canaries, 57 compatibility tests, full Ruff/format/mypy, and real clean wheel
installation plus init/scan/harness/monitor checks. Reproducible package SHA-256:

- Wheel: `01f3565f87493108bdde27f58414f2a035b361f7a5d3c73a82d976296a960aa0`
- Sdist: `f3d520888d034d5451ea87f2812d5b79dc0ae341185701781d4f30f52e99b029`

Twine passed. No package or release was published. The raw gate log and summary
are local operational outputs (`/tmp/ctx-feature-audit-fast.log` and
`.gate/local-fast.json`); this compact record survives their cleanup.

## Additional exact-source evidence

- All five integration tests passed in 33.97 seconds. The command selected
  `src/tests -m integration --run-live-mcp --live-mcp-config <trusted-config>`.
  The explicitly trusted server was this repository's isolated CTX MCP server,
  read-only `ctx__wiki_get` allowlist, empty isolated home, no inherited secrets.
  This is not third-party-host or paid-model evidence.
- All four public-document browser tests passed. These use the production
  catalog stylesheet and assert actual layout, not only the `hidden` attribute.
- Exact manifest hydration plus the real deep validator passed the released
  full/runtime pair: 79,958 nodes, 1,778,069 edges, 1,088,763 semantic edges,
  68,494 skills, 467 agents, 10,790 MCPs, 207 harnesses, and 111,652 tar members.
- Hugging Face's real five-asset pre-upload validator and local repo-card
  renderer passed. No API client, upload, or publication was invoked.
- The retained dashboard reproducer passed all 24 HTTP smoke checks. Frozen
  timings: graph cold 1.190728s (limit 5s), graph warm 0.001915s (limit 1s),
  10,000-sidecar KPI cold 0.994893s and warm 0.033647s (limit 3s), page warm
  0.032624s. This is bounded synthetic KPI evidence, not full-catalog latency.
- GitHub About readback matches the current generator. Traffic for September
  15–28: 303 views from 102 unique visitors; 698 clones from 139 unique cloners.

## Final documentation rendering and later prose-only repair

Strict MkDocs passed. All 29 public navigation pages were opened in the browser;
each rendered its expected heading and content. A local HTML link/anchor check
validated 3,407 references across those pages with no missing target.
The built catalog's skill filter showed four cards and gave all eleven excluded
cards zero layout height. All four deployed README badge URLs were reachable,
but the deployed site still displayed all fifteen cards: the repair is local
and has not been deployed.

Rendering exposed additional stale homepage claims. A later, separately tested
documentation delta removes the old Node/npx Fit prerequisite and clarifies
host-owned observation, installation, council execution, and budget enforcement.
The failing-first homepage regression and all 21 surface-truth tests pass.
This delta is **not** part of commit `2f7a6a23`; its final commit and gate evidence
must be recorded before claiming final-tree completion.

The exact router scanner example, `ctx fit --dry-run`, and `python -m ctx --help`
also passed on an isolated Python/pytest repository with no model or network.
The scanner wrote only its expected temporary-home profile; repository bytes
and Git status were unchanged. The other two commands changed neither repository
nor temporary-home file hashes.

## Remaining

Resolve the deterministic-pair failure, commit the final documentation/evidence
delta, run the authoritative PR preflight, and stamp canonical rows only from
their actual acceptance evidence. Real SkillSpector, approved OCR/model calls,
external host/service/collector operation, required reviewer-rule changes, and
new remote deployment/publication remain explicitly unverified or require owner
authority. Do not substitute the local gate for those external outcomes.
