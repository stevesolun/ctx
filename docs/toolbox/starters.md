# Starter toolboxes

Five presets ship in `docs/toolbox/templates/`. `toolbox init` activates
them into `~/.claude/toolboxes.json`; you can then override any field per-repo
in `.toolbox.yaml`.

## ship-it

> **Professional council of 7 experts for end-of-feature review.**

Runs `code-reviewer`, `security-reviewer`, `architect-review`,
`test-automator`, `performance-engineer`, `accessibility-tester`, and
`docs-lookup` against the diff + graph blast radius.

- **Triggers**: slash, pre-commit, session-end.
- **Scope**: `dynamic` — diff plus imports of changed modules.
- **Budget**: 200 k tokens / 420 seconds.
- **Guardrail**: on (HIGH/CRITICAL blocks pre-commit).

Best for: shipping a feature branch. The council covers correctness,
security, architecture, testing, performance, accessibility, and docs in
one pass.

## security-sweep

> **Full-repo security audit with blocking guardrail on HIGH findings.**

Runs `security-reviewer`, `backend-security-coder`, `frontend-security-coder`,
and `compliance-auditor` against the entire repo.

- **Triggers**: slash, pre-commit.
- **Scope**: `full` — every tracked file.
- **Budget**: 300 k tokens / 600 seconds.
- **Guardrail**: on.

Best for: periodic audits, pre-release sweeps, compliance checkpoints.
Expensive; not a per-commit hook.

## refactor-safety

> **Graph-informed refactor review with regression and dead-code checks.**

Runs `architect-review`, `refactor-cleaner`, `test-automator`, and
`code-reviewer` against diff + graph blast.

- **Triggers**: slash, pre-commit.
- **Scope**: `dynamic`.
- **Budget**: 120 k tokens / 300 seconds.
- **Guardrail**: off — flags issues without blocking.

Best for: mid-refactor checkpoints. Catches orphaned code, downstream
breakage, missing test updates.

## docs-review

> **Documentation pass: accuracy, completeness, clarity, and API parity.**

Runs `docs-lookup`, `technical-writer`, and `code-reviewer` against docs
diffs.

- **Triggers**: slash, pre-commit.
- **Scope**: `diff`.
- **Budget**: 60 k tokens / 180 seconds.
- **Guardrail**: off.

Best for: docs-heavy branches and README updates.

## fresh-repo-init

> **New-repo bootstrap: run the intent interview, scaffold plan, pick initial toolbox.**

Invokes `intent_interview` in interactive mode, then activates whichever
starters the user selects.

- **Triggers**: slash only.
- **Scope**: `full` (fresh scan of a new repo).
- **Budget**: small — this bundle is just an orchestrator.

Best for: `git init` followed by `toolbox init`.

## Activation

```bash
# Pick starters interactively
python -m intent_interview init

# Non-interactive preset
python -m intent_interview init --preset existing --apply
python -m intent_interview init --preset docs-heavy --apply
python -m intent_interview init --preset security-first --apply

# Activate a specific starter directly
ctx-toolbox activate ship-it
```

See [Intent interview](intent-interview.md) for the full flow.
