# Toolbox Feature — Plan

Living plan for the **pre/post dev toolbox + behavior-learning + docs** initiative.
Keep this file terse and current. Detailed work lives in GH issues linked per phase.

## Vision

Let the user declare **named bundles of skills/agents** that load *before* (`pre`) or
run *after* (`post`) a development task. Learn from the user's invocation patterns
over time and propose new bundles. Surface everything through slash commands, a
CLI, and a version-controlled config.

## Principles

- **Foundation first.** Ship data model + CLI + 5 starter toolboxes before any
  hook integration or learning. Each phase is independently usable.
- **User-configurable everything.** Dedup policy, suggestion loudness, trigger
  set, council composition — all settable per toolbox with sensible defaults.
- **Evidence over opinion.** Suggestions cite real usage data + knowledge-graph
  edges. No black-box "trust me" prompts.
- **Token discipline.** Every council run respects a declared
  `max_tokens` / `max_seconds` budget. No runaway cost.

## Data model (canonical)

```jsonc
// ~/.claude/toolboxes.json (global) OR .toolbox.yaml (per-repo, overrides global)
{
  "version": 1,
  "toolboxes": {
    "ship-it": {
      "description": "Professional council for end-of-feature review",
      "pre": [],
      "post": [
        "code-reviewer", "security-reviewer", "architect-review",
        "test-automator", "performance-engineer",
        "accessibility-tester", "docs-lookup"
      ],
      "scope": {
        "projects": ["*"],
        "signals": ["python", "typescript", "rust", "go"],
        "analysis": "dynamic"   // "diff" | "full" | "graph-blast" | "dynamic"
      },
      "trigger": {
        "slash": true,
        "pre_commit": true,
        "session_end": true,
        "file_save": null       // glob or null
      },
      "budget": { "max_tokens": 150000, "max_seconds": 300 },
      "dedup": { "window_seconds": 600, "policy": "fresh" },  // "fresh" | "cached"
      "guardrail": false        // true => block commit on HIGH findings
    }
  },
  "active": ["ship-it"]
}
```

## Phases

### Phase 1 — Foundation (data model, CLI, templates, tests)

Files: `src/toolbox.py`, `src/toolbox_config.py`, `src/tests/test_toolbox.py`,
`skills/toolbox/SKILL.md`, `docs/toolbox/templates/*.yaml`.

- [ ] `toolbox_config.py` — JSON/YAML loader with global + per-repo merge.
- [ ] `toolbox.py` — CLI: `list`, `show`, `activate`, `deactivate`, `init`, `export`, `import`, `validate`.
- [ ] 5 starter templates: `ship-it`, `security-sweep`, `refactor-safety`, `docs-review`, `fresh-repo-init`.
- [ ] Slash command wrappers under `skills/toolbox/` mapping to CLI.
- [ ] Regression tests (data model round-trip, CLI happy paths, config merge precedence).

### Phase 2 — Hook integration + council runner

- [ ] Extend existing pre-commit hook with optional council stage.
- [ ] `src/council_runner.py` — runs the post list with budget enforcement.
- [ ] Session-start / session-end / file-save hook handlers.
- [ ] Dedup cache at `~/.claude/toolbox-runs/<hash>.json`.
- [ ] Graph-informed blast radius: read `graph/wiki-graph.tar.gz` edges to
      compute transitively affected files for `"analysis": "graph-blast"`.

### Phase 3 — Behavior miner + suggestion surface

- [ ] `src/behavior_miner.py` — mines `~/.claude/intent-log.jsonl` +
      `~/.claude/skill-manifest.json` + `git log` for four signals:
      agent co-invocation, skill load/unload cadence, file-type → agent
      correlations, commit-message-type correlations.
- [ ] `~/.claude/user-profile.json` — acceptance rate, opted-out suggestions,
      cadence preferences.
- [ ] Suggestion surface: real-time digest (batched, not interrupt-style) +
      session-end digest. Cadence user-configurable.

### Phase 4 — Intent interview + guardrails + retrospective + explainability

- [ ] `src/intent_interview.py` — structured interview for blank repos and
      existing repos. Auto-prompt on empty-repo detection with one-click skip.
- [ ] Slash: `/toolbox init` and `/toolbox suggest`.
- [ ] Guardrail mode: when enabled per-toolbox, blocks the commit on HIGH findings.
- [ ] Session-end retrospective summarizing skills/agents used + council verdicts.
- [ ] Explainability: every suggestion includes graph evidence + log citations.

### Phase 5 — Documentation site (MkDocs Material)

- [ ] `mkdocs.yml` + Material theme + GH Pages deploy action.
- [ ] Pages: getting started, concepts, CLI reference, hook integration,
      starter toolboxes, behavior learning, FAQ.
- [ ] Keep the existing README as landing; site = deep docs.

### Phase 6 — Curation & self-healing add-ons

- [ ] Skill health dashboard: stale skills, never-used-after-load, high-cost-low-value.
- [ ] Self-healing catalog: nightly diff-scan backfills catalog + graph when
      skills/agents are added outside the wiki flow.
- [ ] Diff-aware memory anchoring: project memories auto-expire when the
      referenced code is no longer present.
- [ ] Community toolbox registry (read-only index repo).

## Open decisions (captured during interview)

- Council composition: **Full 7** = code-reviewer, security-reviewer,
  architect-review, test-automator, performance-engineer, accessibility-tester,
  docs-lookup. Overridable per toolbox.
- Triggers: all four (slash, pre-commit, session-end, file-save), configurable per toolbox.
- Dedup: fresh-by-default, toolbox-overridable.
- Intent interview: auto-prompt on empty repo with skip, plus `/toolbox init`, plus CLI wizard.
- Behavior signals: all four enabled.
- Loudness: user-configurable; default = session-end digest + batched real-time.
- Scope: global + per-repo, repo overrides global.
- Docs: MkDocs Material on GH Pages.
- Ship order: foundation first.

## Out of scope (v1)

- Hosted cloud-sync of user-profile (local-only).
- Non-Claude-Code integrations (VS Code extension, Cursor, etc.).
- Paid/premium toolbox tiers.

## Tracking

GH issues live under the `toolbox-v1` milestone. One issue per phase + one
per starter toolbox template. `plan.md` (this file) is updated each time a
phase completes.
