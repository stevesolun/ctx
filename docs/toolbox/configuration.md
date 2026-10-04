# Configuration

Toolbox config lives in two files:

| Layer | Path | Format | Scope |
|---|---|---|---|
| **Global** | `~/.claude/toolboxes.json` | JSON | Every repo on this machine |
| **Per-repo** | `.toolbox.yaml` (project root) | YAML | This repo only, overrides global |

Per-repo entries replace global toolbox entries with the same name as a whole.
Omitted fields use the schema defaults, not the matching global field values.
The active-name lists are combined, with per-repo names first.

## Schema

```jsonc
{
  "version": 1,
  "toolboxes": {
    "<name>": {
      "description": "human-readable purpose",

      // Skills to load before the trigger fires
      "pre": ["python-patterns", "docs-lookup"],

      // Agents to run after
      "post": [
        "code-reviewer",
        "security-reviewer",
        "architect-review"
      ],

      "scope": {
        // "diff" | "dynamic" | "graph-blast" | "full"
        "analysis": "dynamic",
        // Host metadata: optional project globs (not enforced by the planner)
        "projects": ["*"],
        // Host metadata: optional intent signals (not enforced by the planner)
        "signals": ["python"]
      },

      "budget": {
        "max_tokens": 60000,
        "max_seconds": 180
      },

      "dedup": {
        // "fresh" = always re-run, "cached" = reuse a matching
        // plan within window_seconds
        "policy": "cached",
        "window_seconds": 3600
      },

      "trigger": {
        "slash": true,
        // Optional file-save glob; null disables file-save
        "file_save": null,
        "pre_commit": true,
        "session_end": false
      },

      // If true, HIGH/CRITICAL verdicts block pre-commit
      "guardrail": true
    }
  }
}
```

## Field reference

### `pre` and `post`

- `pre` — requested skills for a host integration to load before work starts.
  A non-empty list makes an active toolbox match `session-start`; the shipped
  hook does not itself load or unload those skills.
- `post` — agents named in the emitted plan. A host integration must invoke
  them and choose their execution contexts.

Either list can be empty. These are declarations, not evidence that a preload
or review has executed.

### `scope.analysis`

Controls what files the council sees:

| Value | Behavior |
|---|---|
| `diff` | Only files with uncommitted changes. Cheapest, fastest. |
| `dynamic` | Diff by default; expands one graph hop for tiny diffs when graph edges are available; falls back to full when no diff exists. |
| `graph-blast` | Current diff plus one-hop graph expansion when a graph edge map is supplied; otherwise the changed set. |
| `full` | Every tracked file. Most thorough; expensive — reserve for security sweeps. |

`scope.projects` and `scope.signals` are retained configuration metadata. The
shipped planner and trigger matcher do not apply those fields as filters;
a host integration must interpret them if it needs that behavior.

### `budget`

Copied into the `RunPlan` as `budget_tokens` and `budget_seconds`.
A downstream executor must enforce those caps. The planner and hook emitter
do not execute agents or enforce runtime spending/time limits.

### `dedup`

`fresh` always builds a new plan. `cached` reuses a matching plan when its
deterministic `plan_hash` is still within `window_seconds`. Dedup state lives
at `~/.claude/toolbox-runs/<plan_hash>.json`.

### `trigger`

Multiple triggers are allowed — a `ship-it` toolbox typically enables
`slash`, `pre_commit`, and `session_end`. `file_save` is a glob string
such as `"**/*.md"`; use `null` to disable file-save matching.
`session-start` is not configured in the trigger map: any active toolbox
with a non-empty `pre` list matches that event. See [`pre` and `post`](#pre-and-post)
for the host's execution responsibilities.

### `guardrail`

When `true` and the trigger is `pre_commit`, the hook reads
an already existing `<plan_hash>.verdict.json` and exits `2` if its level is
`HIGH` or `CRITICAL`. It does not wait for a new council result. Blocking a
commit requires a Git hook or host wrapper that propagates that exit code. See
[Verdicts & guardrails](verdicts.md).

## Editing tools

```bash
# List all toolboxes, both layers merged
python -m toolbox list

# Show resolved config for one toolbox
python -m toolbox show ship-it

# Activate a starter preset
python -m toolbox activate ship-it

# Export one resolved toolbox
python -m toolbox export ship-it > my-toolboxes.yaml

# Import from file
python -m toolbox import my-toolboxes.yaml
```

## Validation

`toolbox_config` validates on read:

- `version` must equal `1`.
- `scope.analysis` must be one of `diff`, `dynamic`, `graph-blast`, `full`.
- `dedup.policy` must be one of `fresh`, `cached`.
- `budget.max_tokens` and `budget.max_seconds` must be positive ints.

Invalid entries raise `ValueError` with the offending key.
