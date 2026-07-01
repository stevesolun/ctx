# Configuration

Toolbox config lives in two files:

| Layer | Path | Format | Scope |
|---|---|---|---|
| **Global** | `~/.claude/toolboxes.json` | JSON | Every repo on this machine |
| **Per-repo** | `.toolbox.yaml` (project root) | YAML | This repo only, overrides global |

Per-repo entries shadow global entries with the same name. Fields absent
from the per-repo file fall back to the global value.

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
        // Optional: restrict to these glob projects
        "projects": ["*"],
        // Optional: match intent signals
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

- `pre` — skills to load before work starts. Loaded into the session's
  skill manifest, unloaded when the session ends.
- `post` — agents to invoke after the trigger. Each runs in its own
  sub-agent context window.

Either list can be empty. A toolbox with only `pre` is a skill preloader;
one with only `post` is a review council.

### `scope.analysis`

Controls what files the council sees:

| Value | Behavior |
|---|---|
| `diff` | Only files with uncommitted changes. Cheapest, fastest. |
| `dynamic` | Diff by default; expands one graph hop for tiny diffs when graph edges are available; falls back to full when no diff exists. |
| `graph-blast` | Current diff plus one-hop graph expansion when a graph edge map is supplied; otherwise the changed set. |
| `full` | Every tracked file. Most thorough; expensive — reserve for security sweeps. |

### `budget`

Copied into the `RunPlan` as `budget_tokens` and `budget_seconds`.
Downstream council execution enforces those caps.

### `dedup`

`fresh` always builds a new plan. `cached` reuses a matching plan when its
deterministic `plan_hash` is still within `window_seconds`. Dedup state lives
at `~/.claude/toolbox-runs/<plan_hash>.json`.

### `trigger`

Multiple triggers are allowed — a `ship-it` toolbox typically enables
`slash`, `pre_commit`, and `session_end`. `file_save` is a glob string
such as `"**/*.md"`; use `null` to disable file-save matching.
`session-start` is not configured in the trigger map: any active toolbox
with a non-empty `pre` list can preload those skills at session start.

### `guardrail`

When `true` and the trigger is `pre_commit`, the hook reads
`<plan_hash>.verdict.json` after the council runs and exits `2` (blocks
the commit) if level is `HIGH` or `CRITICAL`. See
[Verdicts & guardrails](verdicts.md).

## Editing tools

```bash
# List all toolboxes, both layers merged
ctx-toolbox list

# Show resolved config for one toolbox
ctx-toolbox show ship-it

# Activate a starter preset
ctx-toolbox activate ship-it

# Export merged config
ctx-toolbox export > my-toolboxes.yaml

# Import from file
ctx-toolbox import my-toolboxes.yaml
```

## Validation

`toolbox_config` validates on read:

- `version` must equal `1`.
- `scope.analysis` must be one of `diff`, `dynamic`, `graph-blast`, `full`.
- `dedup.policy` must be one of `fresh`, `cached`.
- `budget.max_tokens` and `budget.max_seconds` must be positive ints.

Invalid entries raise `ValueError` with the offending key.
