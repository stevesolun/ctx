# M1 Spike — Friction Log and Rewrite Verdict

Purpose: the spike was commissioned to answer one question with evidence rather
than impression — **does the existing CTX architecture obstruct CTX Fit enough
to justify starting over?**

Method: build Milestone 1 (repository Fit profile, zero model spend) on top of
existing CTX and record every point of friction encountered.

Date: 2026-08-09.

## What was built

| Artifact | Purpose |
| --- | --- |
| `src/ctx/fit/verification.py` | Discovers how a repository verifies itself (test/lint/typecheck/build) with source, confidence, and evidence. Pure inspection; executes nothing. |
| `src/ctx/fit/profile.py` | Versioned `FitProfile` composing CTX's existing stack analysis with verification discovery, existing-AI-config inventory, and honestly-scoped optimization dimensions. |
| `src/ctx/cli/fit.py` | The `ctx fit` subcommand, with `--json` and `--dry-run`. |
| `src/tests/fit/test_fit_profile.py` | 11 tests including adversarial cases: no tests, malformed manifests, empty repository, missing path. |

Total: three new modules plus tests. **Two lines** of change to existing code
(a subparser registration and a dispatch branch in `src/ctx/cli/run.py`).

## Evidence

- `ctx fit .` on this repository correctly reports Python/JavaScript, all four
  verification commands with their exact `pyproject.toml` sources, both
  instruction files, the tool config, and 26 installed skills.
- `src/tests/fit` — **11 passed**.
- CLI, packaging, and Fit suites together — **168 passed**.
- `mypy src` — **no issues in 554 source files** (was 548; the six new files
  type cleanly).
- Ruff and formatting — clean.

## Friction encountered

Recorded honestly, including the parts that argue *against* the existing
architecture.

| # | Friction | Severity | Notes |
| --- | --- | --- | --- |
| F1 | `scan_repo` is a flat legacy module, so the new package layer imports across the layout split | **Low** | Resolved with a lazy import inside the function, which also keeps `ctx.fit` from depending on the legacy layout at import time. The migration is already documented as deliberate and incomplete. |
| F2 | `detect_stack` returns an untyped `dict` | **Low** | Fine at a boundary. `FitProfile` wraps it in a typed, versioned structure, so the untyped dict never leaks into Fit logic. |
| F3 | `build_system` detection is empty even for a repository with a declared build backend | **Low** | Not an architectural problem. Fit's own verification discovery covers it and is more precise, because it reads `[build-system]` directly. |
| F4 | No repository-native verification existed anywhere to reuse | **Medium** | The single largest genuinely-missing capability — but a rewrite would not have supplied it either. It had to be written regardless, and it was, in one self-contained module. |
| F5 | The umbrella CLI required editing a shared 2000-line module | **Very low** | Two lines. The subparser pattern was already there, and adding a subcommand needs no change to the console-script set pinned by `test_package_scaffold.py`. |

**No high-severity friction was encountered.** Nothing forced a workaround,
nothing required touching the engine, the graph, or the benchmark runner, and
no existing test needed modification.

## Verdict: evolve, do not rewrite

The spike is decisive, and the reasoning is arithmetic rather than aesthetic.

1. **The existing architecture did not obstruct the work.** Milestone 1 landed
   as three new files and two lines of change to existing code. A rewrite would
   have produced the same three files plus the obligation to rebuild everything
   else.
2. **The hard, missing capabilities are missing either way.** Verification
   discovery, task derivation, cost unification, and recommendation do not
   exist in CTX today. Starting over does not deliver them sooner; it only adds
   the cost of re-creating what already works.
3. **What a rewrite would destroy is the expensive part.** Counterbalanced
   execution order, workspace isolation, contamination controls, deterministic
   scenario verification, token accounting that refuses malformed usage, a
   nullable-cost discipline that never fakes a number, 8233 passing tests, and
   a green 19-lane gate. Experimental-fairness machinery is exactly the kind of
   code where a missed subtlety produces *confident wrong answers* — the worst
   possible defect in a product whose only value is trustworthy evidence.
4. **The instinct behind the rewrite question was still correct**, and it has
   been honored. The product surface is now genuinely simple: one command, no
   graph statistics, no entity taxonomy, no orchestration internals. CTX Fit
   depends on a deliberately short list and simply does not call the rest.
   Simplicity was achieved by *narrowing the surface*, not by deleting the
   foundation.

## Recommendation

Continue with evolution and a strict dependency diet. Reassess only if a later
milestone hits a genuinely high-severity friction — most plausibly in task
derivation (the hardest unsolved problem) or in unifying cost across the two
execution paths. Both are tracked as open questions rather than assumed
solved.

## Correction carried into the audit

The spike also confirmed a correction already applied to `CURRENT_STATE.md`:
CTX **does** have honest dollar accounting on the `ctx run` path
(nullable `cost_usd`, `attribution="unavailable"`, fail-closed `--budget-usd`),
and it **does** have per-execution budget controls. Only the A/B benchmark path
lacks cost, and only the *aggregate* campaign budget is missing. An earlier
draft of the audit overstated both gaps.
