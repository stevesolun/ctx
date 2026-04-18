# Hooks & triggers

[`src/toolbox_hooks.py`](https://github.com/stevesolun/ctx/blob/main/src/toolbox_hooks.py)
is the bridge between Claude Code's hook system and the toolbox runner.
It listens for four events plus one explicit slash command.

## Event model

| Event | Fires on | Typical toolbox |
|---|---|---|
| `session-start` | New Claude Code session | Skill preloaders, intent suggestions |
| `file-save` | File written to disk | Linters, quick reviewers |
| `pre-commit` | `git commit` before write | Guardrail councils (`ship-it`, `security-sweep`) |
| `session-end` | Session closes | Digest, behavior miner, retro |
| `slash:/toolbox run <name>` | User-initiated | Anything |

Each trigger in a toolbox's `trigger` map enables that toolbox on that
event. Events with no matching toolbox emit nothing.

## Emission format

One JSON line per matching toolbox, on stdout:

```jsonc
{
  "trigger": "pre-commit",
  "toolbox": "ship-it",
  "plan_file": "/Users/steve/.claude/toolbox-runs/abc123.json",
  "agents": ["code-reviewer", "security-reviewer", "architect-review"],
  "files": ["src/toolbox_verdict.py", "src/tests/test_toolbox_verdict.py"],
  "source": "pre-commit",
  "guardrail": true
}
```

Claude Code's hook handler reads these lines and dispatches each agent
against the listed files.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success; zero or more toolboxes emitted |
| `1` | Unknown trigger or config error |
| `2` | `pre-commit` + `guardrail=true` + verdict level is HIGH/CRITICAL |

The `2` exit from `pre-commit` is what actually blocks `git commit`.

## Installation

The install script wires hooks into `.githooks/` and `~/.claude/hooks.json`:

```bash
./install.sh python
```

Manual wire-up:

```bash
# .githooks/pre-commit
#!/bin/sh
python src/toolbox_hooks.py run --event pre-commit
```

Then `git config core.hooksPath .githooks`.

## file-save path matching

`file-save` triggers honor `scope.files` globs. Without a `--path` arg
the event matches nothing (there's no file to test). This is intentional:
file-save toolboxes must be path-scoped.

## session-end digest

On `session-end`, the hook also calls
[`behavior_miner.build_profile`](behavior-miner.md), saves the updated
profile, and prints any new suggestions. This is informational only —
the digest never blocks and never changes the return code.

## Reference

- [Council runner](council-runner.md) — how plans are built.
- [Verdicts & guardrails](verdicts.md) — how blocking is decided.
