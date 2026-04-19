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

Signals aggregate into `~/.claude/user-profile.json`:

```jsonc
{
  "version": 1,
  "updated_at": 1713456789,
  "signals": {
    "co_invocation": {
      "code-reviewer|security-reviewer": 4,
      "architect-review|test-automator": 3
    },
    "skill_cadence": {
      "python-patterns": {"loads": 12, "sessions": 12}
    },
    "file_types": {"py": 87, "md": 31, "tf": 0},
    "commit_types": {"fix": 8, "feat": 2}
  },
  "suggestions": [
    {
      "id": "bundle:reviewers-pair",
      "rationale": "4 co-invocations of code-reviewer + security-reviewer",
      "evidence_count": 4
    }
  ]
}
```

## Digest

On `session-end`, the hook calls `format_digest(profile)` and prints
anything new. Example output:

```
[behavior-miner] 2 suggestions:
  - bundle:reviewers-pair  (4 co-invocations)
    → add to 'review' toolbox:
       ctx-toolbox add review --post code-reviewer,security-reviewer
  - promote:python-patterns  (loaded in 12/12 sessions)
    → promote to 'pre' in your default toolbox
```

Suggestions are never applied automatically. The user runs the command,
or accepts the suggestion via `toolbox init --accept <id>`.

## CLI

```bash
# Rebuild the profile from scratch (scans ~/.claude/history/)
python -m behavior_miner build

# Show current suggestions
python -m behavior_miner show

# Print digest (same output as session-end hook)
python -m behavior_miner digest

# Drop a suggestion (noise reduction)
python -m behavior_miner dismiss bundle:reviewers-pair
```

## Privacy

All signal data stays in `~/.claude/`. Nothing is sent over the network.
The miner never reads file contents — only names, extensions, and commit
message prefixes.

## Related

- [Intent interview](intent-interview.md) — surfaces miner suggestions
  during the `toolbox init` flow.
