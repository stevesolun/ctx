# Council runner

[`src/council_runner.py`](https://github.com/stevesolun/ctx/blob/main/src/council_runner.py)
is the planner that turns a toolbox declaration into a concrete `RunPlan`
the hook system can execute.

## Responsibilities

1. **Resolve the toolbox** — merge global + per-repo config.
2. **Compute scope** — walk the current diff or full repo, honoring
   `scope.analysis` and optional `scope.files` globs.
3. **Graph-blast expansion** — for `dynamic` scope, add every file that
   imports a changed module (via the knowledge graph edge map).
4. **Enforce budget** — drop files until the plan fits within
   `budget.max_tokens` (estimated by line count × heuristic).
5. **Honor dedup** — skip if the same file set was run within
   `dedup.window_seconds` and policy is `user-configurable`.
6. **Persist** — write the plan to
   `~/.claude/toolbox-runs/<plan_hash>.json` for downstream reads.

## RunPlan

```python
@dataclass(frozen=True)
class RunPlan:
    plan_hash: str
    toolbox: str
    agents: tuple[str, ...]
    files: tuple[str, ...]
    source: str           # "slash" | "pre-commit" | ...
    guardrail: bool
    budget: Budget
    created_at: float
```

The `plan_hash` is deterministic (sha256 of `toolbox|sorted(files)|agents`),
which lets dedup work across triggers without any additional state.

## CLI

```bash
# Build and persist a plan for the named toolbox
python -m council_runner build ship-it

# Build without persisting (useful for inspection)
python -m council_runner build ship-it --dry-run

# Show a previously persisted plan
python -m council_runner show <plan_hash>

# List recent plans
python -m council_runner list --limit 10
```

## Budget estimation

Token estimates are intentionally rough. The runner assumes ~4 tokens per
line of source, then sorts files by recency (newest first) and greedily
takes until `max_tokens` is reached. If a single file exceeds the budget,
the plan is truncated rather than dropped — the council still runs on a
partial view.

This cheap estimate is fine because the council itself enforces its own
budgets; `council_runner`'s job is just to stay in the right ballpark.

## Dedup window

Dedup compares the sorted file list, not the plan hash — that way a
toolbox and its re-run with a newer budget still dedup correctly.

## Graph-blast expansion

For `dynamic` scope, `council_runner` reads the graph edge map produced
by `scan_repo.py` and walks imports one hop out from each changed file.
It stops at one hop to keep scope bounded; deep graph walks are reserved
for explicit `full` mode.

## Related

- [Hooks & triggers](hooks.md) — how a plan gets executed.
- [Verdicts & guardrails](verdicts.md) — what the council leaves behind.
