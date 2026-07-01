# Behavior miner

[`src/behavior_miner.py`](https://github.com/stevesolun/ctx/blob/main/src/behavior_miner.py)
watches your invocation patterns and proposes toolbox tweaks grounded in
real evidence.

## What it collects

Four signal families, each with `MIN_EVIDENCE = 3` before a suggestion
can surface:

| Signal | Source | Example suggestion |
|---|---|---|
| **Co-invocation** | Pairs of agents invoked in the same session | "You ran `code-reviewer` + `security-reviewer` together 4 times — consider a bundle." |
| **Skill cadence** | Skill load frequency over time | "`python-patterns` loaded every session — promote to `pre`." |
| **File-type** | File extensions of work-in-progress | "60% of your diffs touch `.tf` files — consider a Terraform toolbox." |
| **Commit-type** | Conventional Commit parsing | "8 of your last 10 commits are `fix:` — consider a pre-commit test toolbox." |

## User profile

Signals aggregate into `~/.claude/user-profile.json` as a `BehaviorProfile`:

```jsonc
{
  "total_intent_events": 87,
  "total_commits": 10,
  "co_invocation_pairs": [{"a": "python", "b": "pytest", "count": 4}],
  "skill_cadence": [["python-patterns", 12]],
  "file_types": [["py", 87], ["md", 31]],
  "commit_types": [["fix", 8], ["feat", 2]],
  "suggestions": [
    {
      "kind": "co-invocation",
      "rationale": "Signals 'python' and 'pytest' co-occurred 4x.",
      "evidence": 4,
      "proposed": {"name": "python-pytest-bundle"}
    }
  ],
  "generated_at": 1713456789
}
```

## Digest

On `session-end`, the hook calls `format_digest(profile)` and prints
anything new. Example output:

```
[toolbox] 2 suggestion(s):
  - python-pytest-bundle (co-invocation, 4x): Signals 'python' and 'pytest' co-occurred 4x.
  - python-patterns-default (skill-cadence, 12x): Skill 'python-patterns' was loaded/unloaded 12x.
```

Suggestions are never applied automatically. The digest is advisory; apply
changes through the normal `ctx-toolbox` commands after review.

## CLI

```bash
# Build the full JSON profile without saving it
python -m behavior_miner profile

# Build and persist ~/.claude/user-profile.json
python -m behavior_miner profile --save

# Print a short digest, optionally persisting the underlying profile
python -m behavior_miner suggest --limit 5
python -m behavior_miner suggest --save
```

## Privacy

All signal data stays in `~/.claude/`. Nothing is sent over the network.
The miner never reads file contents — only names, extensions, and commit
message prefixes.

## Related

- [Intent interview](intent-interview.md) — surfaces miner suggestions
  during the `toolbox init` flow.
