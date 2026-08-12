# ctx repository notes

## The product

CTX Fit finds the cheapest AI coding setup that reliably works on a given
repository, and produces the winning configuration as a reviewable change.

The winner is chosen by a lexicographic rule, not a score: filter candidates on
a reliability floor, then minimize attributable cost, then tie-break toward
simplicity. "Keep your current setup" is a valid and expected answer.

`ctx` is the human command and `ctx fit` is the product; older harness commands
live under `ctx advanced`. `ctx-mcp-server` is the machine contract name. A few
maintenance console scripts (`ctx-init`, `ctx-scan-repo`, `ctx-source-registry`,
`ctx-telemetry-*`) still ship — ADR-001's collapse to two entry points is the
destination, not yet the state.

```bash
ctx fit                      # free, local, read-only: profile + readiness
ctx fit --dry-run            # what a full evaluation would involve
ctx fit --test --budget 10   # evaluate candidates; spending requires both flags
ctx fit --apply              # write the winning configuration
ctx fit --pr                 # print a PR body and branch name; commits nothing
```

## Where the code lives

- `src/ctx/cli/fit.py` — the `ctx fit` subcommand: argument surface, human and
  `--json` rendering, apply/PR handling. Registered from `src/ctx/cli/run.py`,
  which is the `ctx` console-script entry point (`ctx = "ctx.cli.run:main"`).
- `src/ctx/fit/` — the engine, one concern per module:
  `profile.py` (normalized repository profile), `verification.py` (discovering
  the repository's own test/typecheck/lint/build commands), `readiness.py`,
  `tasks.py` (representative tasks derived from repository history),
  `candidates.py` and `release_catalog.py` (bounded candidate generation),
  `experiment.py` (resolve, plan, run), `execution.py` (trial execution with
  adaptive reliability stopping), `live_runner.py` and `providers.py` (driving a
  real coding agent), `recommend.py` (choosing a winner, or refusing to),
  `apply.py` (recommendation → reviewable repository changes and a PR body).
- `src/tests/fit/` — the Fit test suite.

## How to verify a change

Run the smallest thing that exercises what you changed, then escalate.

The first four need only `pip install -e ".[dev]"`. The last two are the full
gate and need the environment CONTRIBUTING.md documents — `.[dev]` alone is
missing mkdocs and twine, and the docs lane fires on any `*.md` change, so both
will die with `ModuleNotFoundError` in a `[dev]`-only checkout.

```bash
python -m pytest -q --no-cov src/tests/fit    # focused: the Fit suite
python -m pytest -q -m 'not integration'      # full suite minus model/network
ruff check src hooks scripts                  # lint
mypy src/                                     # types
scripts/no_mistakes_run.sh fast               # local gate, committed history
python scripts/ci_preflight.py --profile pr   # authoritative multi-lane gate
```

The `integration` marker is opt-in only by convention — nothing deselects it
automatically, so pass `-m 'not integration'` yourself if you want the fast
suite.

`CONTRIBUTING.md` is the contract for which gate a given change needs, plus the
documentation-tracker, packaging, and platform rules. Read the relevant section
before choosing checks rather than running everything.

Never treat an agent's self-report as verification — that rule is the product's
core premise and it applies to work on the product too.

## Settled decisions — do not re-litigate

`docs/ctx-fit/DECISIONS.md` is the decision log. Each ADR is recorded once with
its evidence; reopening one requires new evidence, recorded there. The ones that
most often get argued from first principles again:

- **ADR-014** — the objective is "cheapest that reliably works", by the
  lexicographic rule above. Not a weighted score, not a Pareto search.
- **ADR-003** — recommendation is deterministic. An LLM may *explain* a result;
  it may never *decide* one.
- **ADR-004** — unknown cost stays unknown. Cost records carry an explicit
  completeness state, folding takes the worse state, and an incomplete record is
  never compared as if complete.
- **ADR-005** — verification is repository-native. `attempted` / `completed` /
  `verified` / `failed` / `inconclusive` stay distinct; `flaky` and
  `infrastructure_failure` are represented, not retried away.
- **ADR-013** — bare `ctx fit` is safe, free, and read-only. Spend requires
  `--test` plus an explicit `--budget`.
- **ADR-007** — V1 varies capability configuration within one harness. Harness
  comparison is out of scope and the report must say so.
- **ADR-010** — the event-sourced consent/activation lifecycle is out of scope
  for CTX Fit. It still exists under `src/ctx/runtime/`; it is not the product.
- **ADR-009** — the PyPI distribution stays `claude-ctx`. The product is named
  CTX Fit; users type `ctx`.

Where a planning document under `docs/ctx-fit/` and the code disagree, the code
and its tests are the ground truth.

## Repository gotchas

- The project is migrating from legacy flat modules (`src/*.py`) to the `ctx`
  package (`src/ctx/`). Both layouts are intentional until the migration phase
  removes the old one. `pyproject.toml` is the authoritative inventory and
  `src/tests/test_package_scaffold.py` pins the import and distribution surface.
- Integration, browser, graph, platform, and release checks have different
  dependencies and costs. Select checks from the changed surface.
- Python 3.11+ on Linux and macOS. Native Windows and PowerShell are not
  supported; use WSL2.

## Superseded history

`STATE.md` and `docs/plans/unified-capability-engine.md` describe the *previous*
goal — a unified event-sourced capability engine — which was superseded by the
CTX Fit pivot (ADR-001, ADR-002). They are kept as historical records. Do not
resume work from them and do not treat `STATE.md` as live state.

## On-demand workflows

- Prefer existing scripts, schemas, tests, and batched tools for deterministic
  work. Use an LLM only where semantic judgment adds value.
- For nontrivial work with independent lanes, load
  `.claude/skills/ctx-dispatch/SKILL.md` and act as dispatcher: spawn a
  right-sized swarm of relevant expert subagents in parallel, keep synthesis
  with one coordinator, and add independent reviewer or architecture/CTO
  passes when material risk warrants them.
- For material changes or reviews, load
  `.claude/skills/ctx-verify/SKILL.md`.
