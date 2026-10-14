# Behavior miner

[`src/behavior_miner.py`](https://github.com/stevesolun/ctx/blob/main/src/behavior_miner.py)
watches your invocation patterns and proposes toolbox tweaks grounded in
real evidence.

## What it collects

Four signal families, each with `MIN_EVIDENCE = 3` before a suggestion
can surface:

| Signal | Source | Example suggestion |
|---|---|---|
| **Co-invocation** | Pairs of signal strings in the same intent-log event | "Signals `python` and `pytest` co-occurred 4 times — consider a bundle." |
| **Skill cadence** | Load and unload entries in the skill manifest | "`python-patterns` has 12 load/unload entries — consider a `pre` declaration." |
| **File-type** | Frequency of intent-log signal strings, despite the historical field name | "The `terraform` signal appeared in 8 tool uses — consider a scoped toolbox." |
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
changes through the normal `python -m toolbox` commands after review.

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
The miner does not read repository source-file bodies. It reads local intent
signals, manifest load/unload entries and Git commit subjects; it makes no
network requests.

## Related

- [Intent interview](intent-interview.md) — surfaces miner suggestions
  during the `python -m intent_interview init` flow.
