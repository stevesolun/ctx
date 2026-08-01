# ctx development benchmark

The public Click and Requests scenarios are controlled context-delivery checks.
They keep the model, task, timeout, evaluator, sandbox, and verification fixed.
Production-catalog runs use the shipped graph with a separately frozen private
scenario pack; controlled fixtures alone do not measure recommendation relevance.

| Arm | Selected context | Required use |
| --- | --- | --- |
| `baseline` | None | None |
| `ctx-light` | Highest-ranked matching skill | Skill body only |
| `ctx-full` | Skill, MCP, and reviewer agent | Exact MCP lookup plus completed and closed review |

`ctx-light` is the product-default treatment. Recommendations are candidates, not
automatic loads. `ctx-full` is an explicit escalation and positive control for
tasks that request independent review, cross sensitive boundaries, or fail focused
verification. A failed light verification escalates its next retry only after the
model completed cleanly and the focused verifier returned a normal test failure.
Timeout, containment, sandbox, and harness failures retry the same treatment.

## Run

```bash
.venv/bin/python scripts/ctx_ab_benchmark.py --list

# Fast protocol check without a model turn
.venv/bin/python scripts/ctx_ab_benchmark.py --arm all --dry-run \
  --cache-root /tmp/ctx-benchmark-cache --output /tmp/ctx-ab-preflight

# Default comparison: baseline versus ctx-light
.venv/bin/python scripts/ctx_ab_benchmark.py --arm both --trials 1 \
  --retries 1 --output /tmp/ctx-ab-live

# Counterbalanced adaptive evidence run
.venv/bin/python scripts/ctx_ab_benchmark.py --arm both --trials 6 \
  --retries 1 --output /tmp/ctx-ab-evidence

# Separate positive control; do not include it in the default KPI run
.venv/bin/python scripts/ctx_ab_benchmark.py --arm ctx-full --trials 1 \
  --retries 0 --output /tmp/ctx-ab-full-control
```

Six paired trials alternate baseline/light ordering. An explicit three-arm run
still covers every ordering once. Ordering is derived from the scenario ID, so
filtering to one scenario does not reset it.

## Official production-graph V2 campaign

Current status (2026-08-01): the framework is validated, but the official
campaign has not run (`0/10` controls, `0/30` pairs, `0/60` arms). CTX benefit
remains unproven; `not_beneficial` is a valid final result.

The pre-selection descriptive contract is frozen in
`descriptive-metrics-v1.json`. It reports completed shell-command executions,
failed executions, output bytes, repeated commands, the CTX
recommend/select/deliver/semantic-use funnel, lifecycle event counts, timeouts,
and nonzero agent/verifier outcomes. These metrics are descriptive and
non-confirmatory: they have no thresholds, do not change the primary
time/token/quality verdict, and do not support a causal claim on their own.

Every assigned arm remains in the descriptive population regardless of outcome.
Unknown or malformed evidence stays missing rather than becoming zero. Semantic
use is not inferred from context delivery. The public descriptive block is
withheld until all 60 unique final arm records are present and authenticated;
when released, it contains only whole-arm and whole-campaign aggregates, never
task-level identifiers, commands, outputs, errors, paths, patches, or entity
identifiers.

Run V2 only from a clean worktree at the merged `origin/main` revision. The
worktree, private scenario artifacts, and run output must be on a persistent
filesystem, not under `/tmp`, `/private/tmp`, or `/var/tmp`. Use an isolated
Python 3.12 environment and keep the authenticated Codex configuration unchanged
from protocol creation through final attestation.

```bash
# Choose persistent absolute paths. RUN must not already exist.
SOURCE=/absolute/path/to/ctx
RUN=/absolute/non-temporary/path/to/ctx-ab-v2-run
git -C "$SOURCE" fetch origin
git -C "$SOURCE" worktree add --detach "$RUN" origin/main
cd "$RUN"
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
test -z "$(git status --porcelain=v1 --untracked-files=all)"

mkdir -p .gate
python3.12 -m venv --copies .gate/venv
PY="$RUN/.gate/venv/bin/python"
"$PY" -m pip install -e '.[dev]'
test "$("$PY" -c 'import sys; print(sys.version_info[:2])')" = "(3, 12)"
test -z "$(git status --porcelain=v1 --untracked-files=all)"

# Operator-supplied, revision-pinned inputs and authenticated runtimes.
CODEX=/absolute/path/to/codex
PARQUET=/absolute/path/to/test.parquet
DUCKDB_GZIP=/absolute/path/to/duckdb-cli.gz
SWEBENCH_CHECKOUT=/absolute/path/to/SWE-bench
# Must be a regular-file launcher from a virtual environment created with --copies.
SWEBENCH_PYTHON=/absolute/path/to/swebench-python
DOCKER_CLI=/absolute/path/to/docker
DOCKER_HOST=unix:///absolute/path/to/docker.sock
MODEL=gpt-5.5
MODEL_REASONING_EFFORT=high
MODEL_AUTO_COMPACT_TOKEN_LIMIT=200000

# Private historical evidence must cover every task previously shown to CTX,
# an LLM, or a benchmark arm. Repeat --selection/--evidence as needed.
HISTORY_SELECTION=/absolute/private/path/to/prior-selection.json
HISTORY_EVIDENCE=/absolute/private/path/to/prior-results.csv

PRIVATE="$RUN/.gate/ctx-ab-private/production-graph-holdout-v2"
FAILURES="$PRIVATE/failures"
MAT="$PRIVATE/materialized"
CACHE="$PRIVATE/runner-cache"
OUT="$RUN/.gate/ctx-ab-runs/production-graph-holdout-v2"
mkdir -p "$FAILURES" "$RUN/.gate/ctx-ab-runs"
chmod 700 "$RUN/.gate" "$RUN/.gate/ctx-ab-private" "$PRIVATE" "$FAILURES" \
  "$RUN/.gate/ctx-ab-runs"
test ! -e "$MAT"
test ! -e "$OUT"

# Every failure destination is single-use and remains absent on success. If a
# command fails, stop, preserve/classify its owner-only bundle, and use a fresh
# numbered destination only for an authorized identical-input reproduction.

"$PY" -m scripts.ctx_ab_exposure_ledger \
  --selection "$HISTORY_SELECTION" \
  --evidence "$HISTORY_EVIDENCE" \
  --output "$PRIVATE/exposure-ledger.json"

"$PY" -m scripts.ctx_ab_holdout_prepare protocol \
  --output "$PRIVATE/acquisition-protocol.json" \
  --exposure-ledger "$PRIVATE/exposure-ledger.json" \
  --codex "$CODEX" \
  --swebench-checkout "$SWEBENCH_CHECKOUT" \
  --swebench-python "$SWEBENCH_PYTHON" \
  --docker-cli "$DOCKER_CLI" \
  --docker-host "$DOCKER_HOST" \
  --failure-evidence-output "$FAILURES/protocol-001"
ACQ_SHA="$("$PY" -c \
  'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
  "$PRIVATE/acquisition-protocol.json")"

"$PY" -m scripts.ctx_ab_holdout_acquire \
  --protocol "$PRIVATE/acquisition-protocol.json" \
  --expected-acquisition-protocol-sha256 "$ACQ_SHA" \
  --parquet "$PARQUET" \
  --duckdb-gzip "$DUCKDB_GZIP" \
  --output "$PRIVATE/universe.jsonl"

"$PY" -m scripts.ctx_ab_holdout \
  --protocol "$PRIVATE/acquisition-protocol.json" \
  --expected-acquisition-protocol-sha256 "$ACQ_SHA" \
  --selection-jsonl "$PRIVATE/universe.jsonl" \
  --ledger "$PRIVATE/ledger.csv" \
  --exposure-ledger "$PRIVATE/exposure-ledger.json" \
  --selection "$PRIVATE/selection.json"

"$PY" -m scripts.ctx_ab_holdout_prepare sources \
  --protocol "$PRIVATE/acquisition-protocol.json" \
  --expected-acquisition-protocol-sha256 "$ACQ_SHA" \
  --exposure-ledger "$PRIVATE/exposure-ledger.json" \
  --rows "$PRIVATE/universe.jsonl" \
  --selection "$PRIVATE/selection.json" \
  --cache-root "$PRIVATE/source-cache" \
  --output "$PRIVATE/source-map.json" \
  --workers 4 \
  --failure-evidence-output "$FAILURES/sources-001"

# Materialization runs one red/reference verifier control for each of 10 tasks.
"$PY" -m scripts.ctx_ab_holdout_materialize \
  --protocol "$PRIVATE/acquisition-protocol.json" \
  --expected-acquisition-protocol-sha256 "$ACQ_SHA" \
  --exposure-ledger "$PRIVATE/exposure-ledger.json" \
  --rows "$PRIVATE/universe.jsonl" \
  --selection "$PRIVATE/selection.json" \
  --source-map "$PRIVATE/source-map.json" \
  --runtime-availability "$RUN/src/ctx/assets/runtime-availability.json" \
  --catalog-archive "$RUN/graph/wiki-graph-runtime.tar.gz" \
  --output "$MAT" \
  --failure-evidence-output "$FAILURES/materialize-001" \
  --swebench-checkout "$SWEBENCH_CHECKOUT" \
  --swebench-python "$SWEBENCH_PYTHON" \
  --docker-cli "$DOCKER_CLI" \
  --docker-host "$DOCKER_HOST"

"$PY" -m scripts.ctx_ab_holdout_prepare environment \
  --protocol "$PRIVATE/acquisition-protocol.json" \
  --expected-acquisition-protocol-sha256 "$ACQ_SHA" \
  --output "$PRIVATE/execution-environment.json" \
  --model "$MODEL" \
  --model-reasoning-effort "$MODEL_REASONING_EFFORT" \
  --model-auto-compact-token-limit "$MODEL_AUTO_COMPACT_TOKEN_LIMIT" \
  --agent-timeout-seconds 900 \
  --codex "$CODEX" \
  --python "$PY" \
  --swebench-checkout "$SWEBENCH_CHECKOUT" \
  --swebench-python "$SWEBENCH_PYTHON" \
  --docker-cli "$DOCKER_CLI" \
  --docker-host "$DOCKER_HOST" \
  --failure-evidence-output "$FAILURES/environment-001"

"$PY" -m scripts.ctx_ab_holdout_freeze \
  --protocol "$PRIVATE/acquisition-protocol.json" \
  --expected-acquisition-protocol-sha256 "$ACQ_SHA" \
  --exposure-ledger "$PRIVATE/exposure-ledger.json" \
  --selection "$PRIVATE/selection.json" \
  --scenario-pack "$MAT/scenario-pack.json" \
  --source-map "$PRIVATE/source-map.json" \
  --collision "$MAT/collision-attestation.json" \
  --reconstructed "$MAT/reconstructed-test-attestation.json" \
  --controls "$MAT/control-results.json" \
  --environment "$PRIVATE/execution-environment.json" \
  --schedule "$PRIVATE/execution-schedule.json" \
  --output "$PRIVATE/execution-protocol.json" \
  --failure-evidence-output "$FAILURES/freeze-001"
EXEC_SHA="$("$PY" -c \
  'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
  "$PRIVATE/execution-protocol.json")"

# The authenticated schedule runs 30 pairs as 60 sequential arms.
"$PY" -m scripts.ctx_ab_benchmark \
  --engine codex-production-catalog \
  --arm both \
  --model "$MODEL" \
  --codex "$CODEX" \
  --cache-root "$CACHE" \
  --output "$OUT" \
  --holdout-protocol "$PRIVATE/execution-protocol.json" \
  --holdout-protocol-sha256 "$EXEC_SHA" \
  --holdout-selection "$PRIVATE/selection.json" \
  --holdout-scenario-pack "$MAT/scenario-pack.json" \
  --holdout-collision "$MAT/collision-attestation.json" \
  --holdout-reconstructed "$MAT/reconstructed-test-attestation.json" \
  --holdout-controls "$MAT/control-results.json" \
  --holdout-environment "$PRIVATE/execution-environment.json" \
  --holdout-schedule "$PRIVATE/execution-schedule.json" \
  --holdout-source-map "$PRIVATE/source-map.json" \
  --swebench-dataset "$PRIVATE/universe.jsonl" \
  --swebench-checkout "$SWEBENCH_CHECKOUT" \
  --swebench-python "$SWEBENCH_PYTHON" \
  --docker-cli "$DOCKER_CLI" \
  --docker-host "$DOCKER_HOST" \
  --trials 3 \
  --retries 0 \
  --timeout 900
```

The private exposure ledger must include every known historical task exposure.
Its digest is authenticated by the acquisition protocol, checked before
selection, source preparation, materialization, and freeze, and never publishes
the underlying task IDs. The acquisition protocol SHA-256 must be passed
unchanged through acquisition, selection, source preparation, materialization,
environment capture, and execution freeze. The execution protocol then
authenticates that predecessor digest, the source map and bundles, and every
other frozen execution input. Before accepting an execution protocol, the
runner reconstructs the complete canonical acquisition protocol, validates the
pinned V1-derived design, and compares its canonical digest with the
authenticated predecessor digest. Verdict generation uses the authenticated
snapshots of every frozen input and fails closed if any on-disk input changes
after attestation.

Preparation, materialization, and freeze require an explicit owner-only failure
destination. A failure atomically publishes `failure.json` plus a SHA-256 file
manifest without printing private details. Materialization additionally moves
the exact in-progress control worktree and every already-retained verifier
artifact into that bundle before temporary-directory cleanup. Existing failure
destinations are never overwritten; a successful command creates no bundle.

A completed campaign has exactly 60 arms and 30 complete pairs,
`experiment_valid=true`, no unresolved incidents, and an honest
`product_benefit_verdict` of `beneficial` or `not_beneficial`. `beneficial`
additionally requires preserved quality, exact evidence, verified CTX delivery
in all 10 repositories, an uncached-token benefit in at least 9, an overall
median uncached-token ratio at most 0.85, an overall time ratio at most 1.10,
and the preregistered one-sided repository test at `p <= 0.05`. A valid
`not_beneficial` result is a final result and must not be rerun or hidden.

Do not resume into a nonempty output directory. Infrastructure, identity,
containment, or interruption failures require a new output campaign. After any
outcome-informed code or harness fix, increment the committed
`PROTOCOL_GENERATION`, merge that change, and create a fresh protocol, seed,
selection, controls, environment, schedule, and run from the new merged
revision. Generation `N` selects stable candidate slot `N - 1` independently
within each repository, so later generations must be task-ID-disjoint from
earlier generations. If any of the ten repositories lacks a fresh eligible
candidate, selection fails closed and a new pinned universe must be
preregistered. Never reuse the observed V2 selection for a confirmatory claim.
The runner also keeps a host-wide one-shot claim under
`~/.ctx/benchmark-state/`, keyed by the canonical repository URL. Immediately
before model work it atomically consumes an order-independent identity derived
from the frozen task/repository assignments, then records the exact selection
and execution-protocol identities as secondary fail-closed indexes. Reordering
or re-freezing the same assignments cannot permit another protocol, concurrent
run, clone, or output directory to replay them.

## Evidence

`summary.json` and `summary.csv` record end-to-end time, phase time, verification,
exact terminal-turn tokens when Codex emits them, and separate `recommended_ids`,
`selected_ids`, and `used_ids`. `performance.json` records paired baseline/light
ratios and enforces the declared limits when at least six paired trials are run.
Product-level evidence first collapses trials to scenario medians, then scenarios
to repository medians. Repeated trials never count as independent repositories.
The frozen scenario-to-repository map must match every attempt, including retries.
`environment.json` records the run configuration, schedule, code and scenario
hashes, dependency inventory, and whether the ctx worktree was clean.

Only selected entities receive lifecycle load and unload events. A light skill is
marked used only after a model-turn event. Full MCP use requires the exact tool,
arguments, successful result, and fixture body. Full agent use requires the
selected review marker plus matching spawn, completed wait, and close events. Any
agent attempt makes child-token completeness unknown. Required-tool and retry
failures are written to `incidents.csv`; successful retries resolve their earlier
correlated rows, and unresolved incidents fail the run.

The evaluator test is created before each arm and hash-checked afterward. Red and
reference controls prove that it rejects the missing feature and accepts a known
correct patch. Verification runs in the Codex-managed macOS sandbox with network
disabled, an environment allowlist, resource limits, and descendant cleanup. The
runner also verifies each controlled recommendation is installable and resolves to
a real scenario-local source file before selection.

Token totals are exact copies of the terminal parent `turn.completed.usage` record.
Full-arm child-agent token completeness is unknown, so full-arm totals must not be
presented as complete team usage or attributed to individual tools.

## Pilot diagnosis

The original forced-full pilots passed but measured the wrong default policy:

| Scenario | Baseline | Forced full | Time ratio | Reported token ratio |
| --- | ---: | ---: | ---: | ---: |
| Click | 46.82s / 195,592 | 142.15s / 437,734 | 3.04x | 2.24x |
| Requests | 43.96s / 134,483 | 137.31s / 310,584 | 3.12x | 2.31x |

Recommendation and setup added only 0.27 to 0.47 seconds. The remaining
full-treatment model phase added 93.06 to 94.88 seconds. Both traces forced a
reviewer, retried an initially failed reviewer spawn, and called the MCP; repeated
cached context accounted for 93 to 99 percent of the extra input tokens. Child
timing and tokens were not exposed, so the reviewer's exact share cannot be
isolated. These baseline-first single trials diagnose the old treatment; they are
not a ctx-light performance claim.

For the two trivial scenarios, ctx-light is acceptable only if first-attempt quality
is unchanged and its median paired time and reported-token ratios are at most 1.10.
The six-trial command fails when this performance gate fails. Smaller runs remain
diagnostic and still write paired ratios. Raw model logs can contain repository
source and local artifact paths; review them before sharing.

The generic product-pilot verdict estimates the intent-to-treat effect of
assigning the ctx-light policy; it is not the official V2 claim. An assignment
may deliver context or produce a verified policy abstention, so the verdict does
not attribute every repository's result to delivered context. Generic pilot
eligibility requires at least one verified context delivery overall, six
scenarios across five independent repositories, six trials per scenario,
preserved quality, and no repository above the 1.10 non-regression limit. A
repository supports benefit only when time or uncached tokens improve by at
least 15% while the other stays within 10%. The exact one-sided support test
must pass at `p <= 0.05`; otherwise the product verdict is `not_beneficial` or
`insufficient_cross_repo_evidence`.
