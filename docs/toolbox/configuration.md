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
        // "diff" | "dynamic" | "full"
        "analysis": "dynamic",
        // Optional: restrict to these glob projects
        "projects": ["*"],
        // Optional: restrict to these file globs
        "files": ["src/**/*.py"]
      },

      "budget": {
        "max_tokens": 60000,
        "max_seconds": 180
      },

      "dedup": {
        // "fresh" = always re-run, "user-configurable" = skip
        // if same files already reviewed this session
        "policy": "user-configurable",
        "window_seconds": 3600
      },

      "trigger": {
        "slash": true,
        "session_start": false,
        "file_save": false,
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
| `dynamic` | Diff + import graph blast radius. Catches downstream regressions. |
| `full` | Every tracked file. Most thorough; expensive — reserve for security sweeps. |

### `budget`

Enforced by `council_runner`. When the plan would exceed `max_tokens`, the
runner truncates the file list; when time exceeds `max_seconds`, the
trigger exits 0 without running remaining agents.

### `dedup`

`fresh` always re-runs. `user-configurable` skips a council run when the
same file set was reviewed within `window_seconds`. Dedup state lives at
`~/.claude/toolbox-runs/<plan_hash>.json`.

### `trigger`

At least one trigger must be true. Multiple triggers are allowed — a
`ship-it` toolbox typically enables `slash`, `pre_commit`, and
`session_end`.

### `guardrail`

When `true` and the trigger is `pre_commit`, the hook reads
`<plan_hash>.verdict.json` after the council runs and exits `2` (blocks
the commit) if level is `HIGH` or `CRITICAL`. See
[Verdicts & guardrails](verdicts.md).

## Editing tools

```bash
# List all toolboxes, both layers merged
python src/toolbox.py list

# Show resolved config for one toolbox
python src/toolbox.py show ship-it

# Activate a starter preset
python src/toolbox.py activate ship-it

# Export merged config
python src/toolbox.py export > my-toolboxes.yaml

# Import from file
python src/toolbox.py import my-toolboxes.yaml
```

## Validation

`toolbox_config.load()` validates on read:

- `version` must equal `1`.
- Every toolbox needs at least one trigger.
- `scope.analysis` must be one of `diff`, `dynamic`, `full`.
- `budget.max_tokens` and `budget.max_seconds` must be positive ints.

Invalid entries raise `ValueError` with the offending key.
