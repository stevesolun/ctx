# ctx development benchmark

This benchmark runs the same small feature task against pinned Click and Requests
commits. The model, task, timeout, evaluator, sandbox, and verification stay fixed.
It is a controlled context-delivery benchmark: scenario-local fixtures isolate the
effect of selecting and using context. It does not measure production-catalog
recommendation relevance; that requires a separate real-catalog benchmark.

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

## Evidence

`summary.json` and `summary.csv` record end-to-end time, phase time, verification,
exact terminal-turn tokens when Codex emits them, and separate `recommended_ids`,
`selected_ids`, and `used_ids`. `performance.json` records paired baseline/light
ratios and enforces the declared limits when at least six paired trials are run.
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
