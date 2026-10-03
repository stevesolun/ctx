# Audit isolation repair — 2026-10-04

Review-phase worktree starting HEAD:
`03cc9e791059aa9dae8792ce6ff5dd0c300ac520`, existing delivery branch
`codex/full-feature-audit-delivery`, draft PR #286.
`qa/feature_status.csv` remains the only status tracker.

## Findings and repair

Both findings were confirmed from source before editing. They are fixture
ownership defects, not defects in production telemetry or monitor behavior.

- R1 / `AUDIT-20261004-SAFE-EXAMPLE-SALT-ISOLATION`: the safe helper's
  temporary HOME and salt environment were supplied only to subprocesses.
  Its two in-process `record_event` calls now share an explicit
  `privacy.hash_salt=fixture-salt` configuration. No production salt precedence,
  checkpoint behavior, export semantics, or redaction logic changed.
- R2 / `AUDIT-20261004-BROWSER-HISTORY-ISOLATION`: the shared browser fixture
  now binds `runtime_lifecycle_path` to its temporary `runtime/events.jsonl`.
  Existing per-test overrides, browser assertions, and timeouts remain intact.

The telemetry regression runs the actual helper, restores the real recorder
past the suite's autouse no-op, removes the ambient salt, and configures a
synthetic caller-home salt sentinel. Both filesystem salt creation and identity
reading fail the test if invoked. The synthetic caller home must remain empty.
The browser regression installs a temporary default-history sentinel before
loading the shared fixture, seeds distinct fixture history, and observes the
real home-page reader. It requires the fixture failure count, fixture lifecycle
read, no sentinel read, and all observed reads beneath the fixture directory.

An independent reader traced both changes and tests. Their one test-assertion
finding was corrected before execution: home also reads its fixture audit log,
so the regression permits those reads while enforcing their fixture boundary.
No unresolved source finding remains from that bounded inspection. This is not
human approval of the outer fix-review.

## Focused verification

Used the already trusted venv selected by
`CTX_NO_MISTAKES_PYTHON_BIN_RESOLVED`, with `PYTHONPATH` pointing to this
worktree's `src` and root. Test/command HOME was isolated under this worktree's
`.gate/review-isolation/home`; Playwright used the existing browser cache.
No environment installation, upgrades, or other checkout source edits occurred.
Browser execution set `CI=1`, so missing Chromium would fail instead of skip.

All intended source edits preceded verification. To retain red/green evidence
without reverting working source, the single final verification batch first ran
the new tests in temporary copies: R1 used the exact HEAD helper; R2 used the
new browser test with only the new shared-fixture binding removed. These
pre-fix controls ran after the edit batch and before the repaired-tree tests.
They are not a claim that tests ran before editing the worktree.

Retained red output excerpts (temporary prefixes abbreviated):

```text
FAILED test_safe_examples_never_access_default_home_salt
Failed: safe examples accessed filesystem salt: <temporary>/caller-home/.ctx/telemetry/hash-salt
FAILED test_home_uses_fixture_lifecycle_history
AssertionError at assert sentinel not in read_paths
2 failed in 27.83s
```

The repaired selection ran `python -m pytest -q --no-cov` with:

- `src/tests/test_enterprise_telemetry.py::test_safe_examples_never_access_default_home_salt`
- Six cases in `src/tests/test_ctx_monitor_browser.py`: fixture history,
  navigation drag/reset, dark dashboard contrast, mobile home/activity/wiki,
  empty runtime/recommendation outcomes, and populated sessions/KPI/runtime.
- `src/tests/test_feature_user_story_tracker.py`.

```text
....................                                                     [100%]
20 passed in 9.22s
```

The separate actual command
`python qa/feature-audit/safe_examples.py` exited zero:

```json
{
  "attach_edges": 2,
  "compacted_graph_nodes": 3,
  "compacted_wiki_pages": 3,
  "external_calls": 0,
  "graphify": "Graph: 2 nodes, 1 edges",
  "index_nodes": 2,
  "telemetry_exported": 2,
  "telemetry_preview_attempted": 2,
  "telemetry_replayed": 2
}
```

Changed-file Ruff lint passed. Initial format checking requested a lambda layout
adjustment in the new telemetry test; Ruff applied only that formatting change.
The affected regression was rerun on the final bytes:

```text
.                                                                        [100%]
1 passed in 31.22s
```

Final `ruff check` passed and `ruff format --check` reported
`3 files already formatted` for the helper and both changed test modules.
No full test suite, full lint suite, committed gate, preflight, pipeline-control,
push, PR/issue mutation, merge, release, or paid evaluation was performed.

Raw red/green, standalone helper, initial/final static, and formatting-regression
logs plus exact command arrays are retained under `.gate/review-isolation/`.
`results.json` also records final source hashes and original-contract checks.
These ignored local files supplement this portable report.

## Frozen source and tracker boundary

| File | SHA-256 |
| --- | --- |
| `qa/feature-audit/safe_examples.py` | `fd18ead81d17029a39224f02c8de44ca414e846270923aafa6c2649bfd906727` |
| `src/tests/test_enterprise_telemetry.py` | `b792562ae25a2057af9213a34e56226b7157785e5e9ff8e38f493b4eefec8b5e` |
| `src/tests/test_ctx_monitor_browser.py` | `b6f86212ab547b481a19ce813e8faabe8b87122cb7bc5e12a3adb4946ef3f1a9` |

The HEAD helper previously catalogued in `SHA256SUMS` was
`642040a9f9dc76e7c4c4deecb70a5e12458cee6ea75efb1394c937efc1a6e753`.
The manifest now identifies the repaired helper and the corrected historical
safe-example note. Historical command results remain intact; the old
no-real-home-write claim is explicitly qualified.

All 318 original acceptance contracts and every historical bug ID were compared
against HEAD and preserved. DASH-001, DOC-NAV-003, DOC-NAV-007 and LANE-D-036
are Needs Validation; their former formal acceptance fields are retained
verbatim in notes. Focused fixture evidence does not renew complete dashboard
or documentation acceptance. Existing pending whole-audit, deployment,
publication, paid OCR, and independent-human governance rows remain pending.

Two new nonparametrized tests increase the documented inventory from 9,450 to
9,452. This is the verified source delta from the supplied baseline inventory;
full live recollection belongs to the outer executor, not this focused review.
The supplied clean-03cc9e79 fast pass is historical after these edits. Source is
frozen for explicit fix-review; after approval and commit the outer executor
must run a fresh committed fast gate before authoritative PR preflight. No
automatic approval or nested pipeline was used.
