# Starter toolboxes

The five JSON files in `docs/toolbox/templates/` are optional configuration
examples, not installed presets or supplied agents. `python -m toolbox init`
creates an empty configuration and preserves existing user definitions.
It adds and activates no tools.

Adapt an example to tools you actually own, wrap it under a `toolboxes` mapping
in your own version-1 configuration, and import that file explicitly. Only then
use `python -m toolbox activate NAME` for an existing definition. Per-repository
definitions in `.toolbox.yaml` replace the complete same-name global toolbox,
not individual fields.

The agent lists below describe intended host execution. The shipped planner
only emits plans; it does not invoke those agents or enforce their budgets.

## ship-it

> **Professional council of 7 experts for end-of-feature review.**

The example references `code-reviewer`, `security-reviewer`, `architect-review`,
`test-automator`, `performance-engineer`, `accessibility-tester`, and
`docs-lookup` against the dynamic review scope.

- **Triggers**: slash, pre-commit, session-end.
- **Scope**: `dynamic` — diff by default, graph-blast for tiny diffs with
  graph edges, full repo when no diff exists.
- **Budget**: 200 k tokens / 420 seconds.
- **Guardrail**: off.

Best for: shipping a feature branch. The council covers correctness,
security, architecture, testing, performance, accessibility, and docs in
one pass.

## security-sweep

> **Full-repo security audit with blocking guardrail on HIGH findings.**

The example references `security-reviewer`, `security-auditor`, `penetration-tester`,
`compliance-auditor`, and `threat-detection-engineer` against the entire repo.

- **Triggers**: slash, pre-commit, file-save on `**/auth/**`.
- **Scope**: `full` — every tracked file.
- **Budget**: 300 k tokens / 600 seconds.
- **Guardrail**: on.

Best for: periodic audits, pre-release sweeps, compliance checkpoints.
Expensive; not a per-commit hook.

## refactor-safety

> **Graph-informed refactor review with regression and dead-code checks.**

The example references `architect-review`, `refactor-cleaner`, `code-reviewer`,
`test-automator`, and `dependency-manager` against graph-blast scope.

- **Triggers**: slash, session-end.
- **Scope**: `graph-blast`.
- **Budget**: 180 k tokens / 360 seconds.
- **Guardrail**: off — flags issues without blocking.

Best for: mid-refactor checkpoints. Catches orphaned code, downstream
breakage, missing test updates.

## docs-review

> **Documentation pass: accuracy, completeness, clarity, and API parity.**

The example references `technical-writer`, `docs-architect`, `api-documenter`, and
`tutorial-engineer` against docs diffs.

- **Triggers**: slash, file-save on `**/*.md`.
- **Scope**: `diff`.
- **Budget**: 120 k tokens / 240 seconds.
- **Guardrail**: off.

Best for: docs-heavy branches and README updates.

## fresh-repo-init

> **New-repo bootstrap: run the intent interview, scaffold plan, pick initial toolbox.**

Declares an intent-interview bootstrap task for a host to invoke. To perform
the interview directly, run `python -m intent_interview init`; activation
requires `--apply`.

- **Triggers**: slash only.
- **Scope**: `diff`.
- **Budget**: 100 k tokens / 300 seconds.

Best for: bootstrapping a newly initialized repository.

## Activation

```bash
# Create an empty configuration; then import your reviewed definitions
python -m toolbox init
python -m toolbox import ./my-toolboxes.yaml

# Only if your imported configuration defines ship-it
python -m toolbox activate ship-it
```

The historical [Intent interview](intent-interview.md) presets reference these
names; they do not supply missing tool definitions or capabilities. Review and
create the named definitions yourself before expecting activation.
