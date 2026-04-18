# /toolbox init

Run the intent interview to bootstrap this repo's toolbox set.

## What it does

Drives `src/intent_interview.py init` to:

1. Detect the repo's state (git? commits? languages? existing toolbox config?).
2. Load the current behaviour profile from `~/.claude/user-profile.json`.
3. Ask up to three questions:
   - which starter toolboxes to activate (ship-it, security-sweep,
     refactor-safety, docs-review, fresh-repo-init)
   - which behaviour-miner suggestions to accept (if any exist)
   - default analysis mode for new toolboxes
4. Persist the chosen toolboxes to `~/.claude/toolboxes.json` when `--apply`
   is passed.

The interview can be skipped at any prompt by typing `skip`.

## Usage

```bash
# Default: interactive flow, dry run (no write to global config)
python src/intent_interview.py init

# Preset flow (no prompts)
python src/intent_interview.py init --preset blank --apply
python src/intent_interview.py init --preset existing --apply
python src/intent_interview.py init --preset docs-heavy --apply
python src/intent_interview.py init --preset security-first --apply

# Fully structured (for CI or scripted setup)
python src/intent_interview.py init \
  --non-interactive \
  --starters ship-it,security-sweep \
  --suggestions 1,2 \
  --analysis dynamic \
  --apply

# Just detect state without running the interview
python src/intent_interview.py detect
```

## Exit codes

- `0` — success, JSON payload printed to stdout
- non-zero — unrecoverable error (unknown preset, malformed args)

## Related

- `src/toolbox.py` — CLI for managing the toolbox set directly.
- `src/behavior_miner.py` — produces the suggestions this command surfaces.
- `src/toolbox_hooks.py` — the bridge between Claude Code hooks and the runner.
