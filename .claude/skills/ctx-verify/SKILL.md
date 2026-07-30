---
name: ctx-verify
description: Select proportional validation for changes or reviews in the ctx repository. Use before handing off material code, workflow, migration, documentation, security, packaging, or release changes; before a PR; or when asked whether a change is adequately tested. Do not use for pure questions with no changed artifact.
---

# Verify ctx changes

1. Inspect the requested behavior and changed surface. Read only the relevant
   section of `CONTRIBUTING.md` before selecting checks.
2. Run the smallest focused tests or deterministic checks that exercise the
   changed behavior first.
3. Escalate evidence in proportion to risk:
   - Small, localized change: focused tests, schemas, or diff checks.
   - Material repository change: `scripts/no_mistakes_run.sh fast`.
   - PR-level, contract, workflow, migration, packaging, security, or release
     change: run the fast gate, then
     `python scripts/ci_preflight.py --profile pr`.
   - Documentation navigation or linked public-asset change: follow the tracker
     contract in `CONTRIBUTING.md`; do not require tracker changes for ordinary
     prose-only edits.
   - Windows-sensitive change: follow the native-CI contract in
     `CONTRIBUTING.md`; local checks do not replace required Windows evidence.
4. Treat the fast gate as committed-history evidence. When the working tree is
   dirty, pair it with focused checks against the actual uncommitted changes.
5. If a gate cannot find its trusted pytest, Ruff, and mypy environment, report
   the environment failure. Do not silently install dependencies or weaken the
   trust check.
6. Use an independent validator only when semantic risk remains after
   deterministic checks, especially for material security, migration, or
   release work.
7. Report each check, its result, and any omitted or unavailable evidence.

## Rubric

Mark each applicable criterion `pass`, `fail`, or `not verified`:

- **Intent:** The artifact satisfies the request without unrelated scope.
- **Behavior:** Fresh evidence covers the changed success path and relevant
  failure or regression paths.
- **Static integrity:** Applicable lint, formatting, and type checks pass.
- **Repository contracts:** Migration, changed-file, documentation, packaging,
  and platform requirements remain satisfied where relevant.
- **Evidence quality:** Results apply to the actual changed surface and are not
  stale or inapplicable.
- **Handoff:** Skipped checks and residual risks are stated plainly.

Do not claim completion while an applicable criterion fails. Treat missing
required evidence as `not verified`, not as a pass.
