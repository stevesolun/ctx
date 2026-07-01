# Council runner

[`src/council_runner.py`](https://github.com/stevesolun/ctx/blob/main/src/council_runner.py)
is the planner that turns a toolbox declaration into a concrete `RunPlan`
the hook system can execute.

## Responsibilities

1. **Resolve the toolbox** — merge global + per-repo config.
2. **Compute scope** — use explicit files, the current diff, the full repo,
   or one-hop graph expansion when a graph edge map is supplied.
3. **Carry budget caps** — copy the toolbox token and second caps into the
   plan so downstream execution can enforce them.
4. **Honor dedup** — reuse a cached plan when the toolbox policy is `cached`
   and the matching hash is still inside `dedup.window_seconds`.
5. **Persist** — write the plan to
   `~/.claude/toolbox-runs/<plan_hash>.json` for downstream reads.

## RunPlan

```python
@dataclass(frozen=True)
class RunPlan:
    toolbox: str
    agents: tuple[str, ...]
    files: tuple[str, ...]
    scope_mode: str
    budget_tokens: int
    budget_seconds: int
    guardrail: bool
    created_at: float
    plan_hash: str
    source: str           # "fresh" | "cached"
```

The `plan_hash` is deterministic from the toolbox, agents, file set, and
effective scope mode, which lets dedup work without any additional state.

## CLI

```bash
# Build and persist a plan for the named toolbox
python -m council_runner plan --toolbox ship-it

# Build without persisting (useful for inspection)
python -m council_runner plan --toolbox ship-it --dry-run

# List recent plans
python -m council_runner history --limit 10

# Delete stale plans
python -m council_runner purge --older-than-days 30
```

## Budget estimation

The runner records `budget.max_tokens` and `budget.max_seconds` in the plan.
The downstream council execution is responsible for enforcing those caps.

## Dedup window

Dedup looks up the deterministic `plan_hash`. If the toolbox policy is
`cached` and the existing plan is still inside `dedup.window_seconds`, the
runner returns that cached plan instead of writing a fresh one.

## Graph-blast expansion

When callers provide a graph edge map, `graph-blast` expands changed files by
one hop. Without a graph map, graph-blast falls back to the changed set so the
CLI remains deterministic and cheap.

## Related

- [Hooks & triggers](hooks.md) — how a plan gets executed.
- [Verdicts & guardrails](verdicts.md) — what the council leaves behind.
