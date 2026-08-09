# Milestone 0 — Audit · Milestone Report

Date: 2026-08-09 · Branch `ctx-fit/m0-baseline` · Baseline commit `e8af9554`

**Gate outcome: ACCEPTED WITH CONDITIONS — all conditions met.**

Both reviewers returned `accept-with-conditions`. Every blocking objection was
fixed rather than argued. This report is for a reader who does not know CTX
internals.

## What a user can do now that they could not before

Point one command at any repository and get, free, read-only, in under a
second:

- the repository's own test, lint, type-check and build commands, each with the
  exact manifest stanza that justified it and a coarse confidence;
- an inventory of the AI coding setup already present (`AGENTS.md`,
  `CLAUDE.md`, tool config, installed skills);
- a plain verdict on whether the repository can be evaluated at all, and if
  not, what to fix first.

```bash
pip install claude-ctx && cd my-project && ctx fit
```

## Why that is valuable

It replaces a manual pass over `pyproject.toml`, `package.json`, `go.mod` and
`Makefile` — and, more importantly, it is honest. The previous surface
(`ctx-scan-repo --recommend`) inferred relevance from keyword overlap and wrote
into the user's home directory. `ctx fit` writes nothing, guesses nothing, and
cites the file and stanza behind every claim.

## What the gate caught, and what changed

The reviews found real defects. They are listed because the process working is
the point of this milestone.

| Finding | Severity | Resolution |
| --- | --- | --- |
| **Evaluability overclaim.** A repository with a pytest stanza and zero test files was reported as "can be evaluated: it has deterministic tests". A declared runner is intent, not evidence. | **Blocking** — this was the one sentence the product's credibility rests on | `has_deterministic_verification` now requires a declared command **and** observed test material. The repository is told exactly why it is not evaluable and what to do. Two regression tests added. |
| **Non-deterministic output.** `--json` embedded a wall-clock timestamp and an absolute path from the legacy scanner, so two consecutive runs of a "versioned, reproducible" schema differed. | **Blocking** | Volatile fields stripped; a test now asserts byte-identical serialization across runs. |
| **Audit headline overstated.** "93% of the engine lane is removable" conflated *not reachable* with *not needed*. Only 10,741 LOC is proven dead; 35,993 LOC is reachable today. | **Blocking** | `CTX_AUDIT.md` §3 rewritten to separate the two claims. The deletion program starts with the proven-dead cluster only. |
| **Three contradictory execution decisions live at once** across `MAP.md`, `ARCHITECTURE_GAP_ANALYSIS.md` and `DECISIONS.md`. | **Blocking** | `DECISIONS.md` declared authoritative; superseded notices added to `MAP.md`. |
| **Two contradictory task ledgers.** | **Blocking** | `IMPLEMENTATION_TRACKER.md` retired; `planned_tasks.md` is the single board. |
| **Undiscoverable at the front door.** `ctx fit` appeared nowhere in the README; `ctx --help` still described a "model-agnostic harness". | **Blocking** | README now leads with the product and real output; parser description rewritten. |
| **Extraction estimate ~2x optimistic; delete list swallowed the contamination controls the PRD depends on.** | Correction | Estimate revised; ~6,012 LOC of holdout/exposure-ledger code reclassified ADAPT. |
| CTX-internal jargon in user output; a permanent `[no] coding-harness` row. | Simplification | Collapsed to one plain sentence; full detail kept in `--json`. |

## The most consequential finding

An AST import graph from every real entry point proved that **10,741 LOC of the
event-sourced capability lifecycle has no production consumer** — it is
reachable only from its own tests. An independent second review reproduced this
from a broader seed set and concluded the original audit *understated* it.

That cluster includes work completed earlier in the same session. It was
correct engineering against the previous goal; the goal changed. Recorded as
ADR-010 rather than quietly protected.

## What evidence proves this works

- 14 tests in `src/tests/fit/`, including adversarial cases: no tests, declared
  runner with no tests, malformed `pyproject.toml` and `package.json`, empty
  repository, missing path, Node lockfile runner selection.
- `mypy` clean across 554 source files; Ruff and formatting clean.
- The full 19-lane PR preflight was green at the baseline commit: 8,233 tests,
  91.9 percent coverage.
- The reviewer's exact repro of the overclaim now produces the correct refusal.

## What still prevents charging money

The product currently answers *"can this repository be evaluated, and what
should I fix?"* It does not yet answer *"which setup should I use?"* — there is
no readiness score, no candidate set, no experiment, and no recommendation.
Roughly the first third of the first clause of the promise is implemented.

## Next smallest step that increases usefulness

**M3 — AI Agent Readiness.** It converts the profile from information into a
prioritized decision, needs no provider spend, and is the last milestone before
candidate generation. The design is ready.
