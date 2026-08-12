# ctx

[![CI](https://github.com/stevesolun/ctx/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![Tests](https://img.shields.io/badge/Tests-8524_inventory-blue.svg)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)

**Find the cheapest AI coding setup that actually works on your repo.**

CTX Fit analyzes your repository, tests promising AI coding configurations
against real tasks in it, and writes the winning configuration into your
working tree. It picks the *cheapest* setup that *reliably* works —
reliability is a requirement, not a tie-break — and if nothing beats what you
already have, it says so.

> **CTX Fit is not released yet.** The published `claude-ctx` package
> ([v1.0.20](https://pypi.org/project/claude-ctx/)) contains none of it —
> `pip install claude-ctx` gets you the older recommendation surface described
> further down, not `ctx fit`. To use Fit today you need a source checkout.

```bash
git clone https://github.com/stevesolun/ctx && cd ctx
pip install -e .
cd /path/to/my-project
ctx fit
```

Bare `ctx fit` is free, local and read-only: it runs no model and spends
nothing. Example output from a real run, abridged:

```text
Languages:  python, javascript

Current AI coding setup
  Instructions:  AGENTS.md, CLAUDE.md
  Tool config:   .claude/settings.local.json
  Installed skills: 26

How this repository verifies itself
  test       python -m pytest -q
             from pyproject.toml [tool.pytest] (high confidence)
  typecheck  python -m mypy src
             from pyproject.toml [tool.mypy] (high confidence)
  lint       python -m ruff check .
             from pyproject.toml [tool.ruff] (high confidence)

AI agent readiness
  83/100
    Verification           30/30
    Instructions           12/20
    Environment             6/15
    CI enforcement         15/15
    Tool safety            10/10
    Context tractability   10/10

Highest-impact improvements
  1. Commit a dependency lockfile. (+9)
     no dependency lockfile is committed
  2. State the exact test command in the instruction file. (+8)
     AGENTS.md, CLAUDE.md never mention how to verify a change

This repository can be evaluated: it has deterministic tests, so a candidate
configuration can be judged on evidence rather than on an agent's own claim.
```

Requires Python 3.11 or newer. Add `--json` for machine-readable output, or
`--dry-run` to see what a full evaluation would involve.

Beyond the free profile, `ctx fit --test --budget N` evaluates candidate
configurations against real tasks taken from the repository's own history, and
`ctx fit --apply` writes the winning configuration, showing every change before
it writes. Run `ctx doctor` to see whether a real evaluation can run here;
without provider credentials `--test` runs in simulation, which proves the
pipeline but not your repository.

`ctx fit --pr` prints a suggested branch name and a pull-request body to
stdout. It creates no branch, commits nothing, pushes nothing, and opens no
pull request — that is yours to do.

**Release status:** [v1.0.20](https://github.com/stevesolun/ctx/releases/tag/v1.0.20)
is the current GitHub and [PyPI](https://pypi.org/project/claude-ctx/) release;
this source tree declares `1.0.21` for unreleased work.

## Install

Requires CPython 3.11 or newer. Linux and macOS are the tested host platforms;
other POSIX systems are best-effort. Native Windows and PowerShell are not
supported. On a Windows machine, run ctx inside WSL2 as a Linux installation.

```bash
pip install claude-ctx
```

This installs release `1.0.20`, which ships the recommendation surface below.
Its `ctx` command is the agent-loop harness only (`ctx run`, `ctx resume`,
`ctx sessions`); `ctx fit`, `ctx doctor` and `ctx advanced` exist only in a
source checkout.

## Recommendation surface (existing)

From the repository you want to analyze, install the runtime graph and request
recommendations:

```bash
ctx-init --graph --model-mode skip
ctx-scan-repo --repo . --recommend
```

`ctx-init --graph` uses the bundled runtime artifact in a source checkout or
downloads the matching release asset for a package install. The full packed
wiki is optional; see the [knowledge graph guide](https://stevesolun.github.io/ctx/knowledge-graph/).

Every clean graph install seeds nine project-owned, MIT-licensed, no-key
fallbacks: `ctx-python-testing`, `ctx-python-state-protocols`,
`ctx-python-input-boundaries`, `ctx-python-api-compatibility`,
`ctx-javascript-testing`, `ctx-rust-patterns`, `ctx-typescript`, the
`ctx-python-reviewer` agent, and the local `ctx-core` MCP server.
ctx preserves unrelated skill, agent, MCP, and converted-skill content.
Runtime-managed harness pages are refreshed from the installed artifact.
Installation fails closed if a reserved `ctx-*` identity, body, overlay, or
parent path is unexpected.

## Privacy And Telemetry

These controls are available in release `1.0.20` and the current source tree.
Telemetry is enabled by default in `local_redacted` mode. Events are written to
`~/.ctx/telemetry/events.jsonl`, metrics are written to
`~/.ctx/telemetry/metrics.jsonl`, and raw prompts and queries are removed or
hashed. Continuous log, trace, and metric exporters are disabled by default.

A network export requires an explicit `ctx-telemetry-export` command or an
operator-enabled exporter configuration. Local JSONL may retain a raw
`session_id` for compatibility, so treat the spool as sensitive. Review the
[enterprise telemetry guide](https://stevesolun.github.io/ctx/telemetry/) before
enabling export.

```bash
ctx-telemetry-export --dry-run --json
```

The dry run inspects the local spool without exporting it.

## CLI Reference

| Task | CLI | Guide |
| --- | --- | --- |
| Analyze a repository and get a recommendation | `ctx` | [Entity onboarding](https://stevesolun.github.io/ctx/entity-onboarding/) |
| Check why a real evaluation cannot run yet | `ctx doctor` | [Entity onboarding](https://stevesolun.github.io/ctx/entity-onboarding/) |
| Initialize ctx and install graph data | `ctx-init` | [Knowledge graph](https://stevesolun.github.io/ctx/knowledge-graph/) |
| Scan a repository | `ctx-scan-repo` | [Entity onboarding](https://stevesolun.github.io/ctx/entity-onboarding/) |
| Connect an MCP, Python, or CLI host | `ctx-mcp-server`, `ctx advanced run` | [Host integration](https://stevesolun.github.io/ctx/harness/attaching-to-hosts/) |
| Inspect the local runtime | `python -m ctx_monitor serve` | [Dashboard](https://stevesolun.github.io/ctx/dashboard/) |
| Review or export telemetry | `ctx-telemetry-export`, `ctx-telemetry-retention` | [Telemetry](https://stevesolun.github.io/ctx/telemetry/) |

This table describes the source tree. The `ctx-*` scripts are also in release
`1.0.20`; the `ctx` subcommands other than `run`, `resume` and `sessions` are
not.

The agent-loop harness (`run`, `resume`, `sessions`) is still there and still
supported; it now lives under `ctx advanced` so the top-level help stays about
the product. Maintenance utilities that used to be console scripts are reached
with `python -m` — for example `python -m ctx.cli.recommend` or
`python -m ctx.core.quality.dedup_check`.

See the [full documentation](https://stevesolun.github.io/ctx/) for configuration,
APIs, entity lifecycle, and operational details.

## Example user stories

| Tracker ID | User outcome |
| --- | --- |
| `CLI-002` | Scan a repository and receive a bounded skill, agent, and MCP recommendation set. |
| `CLI-026` | Review a custom-model harness recommendation with `python -m harness_install --dry-run` before installation. |
| `API-011` | Manage local entities through the dashboard's validated API. |

<details>
<summary>Tracking sources</summary>

Release readiness is tracked in [`qa/feature_status.csv`](qa/feature_status.csv).
The [`docs/qa/feature-user-story-status.csv`](docs/qa/feature-user-story-status.csv),
[`docs/qa/dashboard-user-story-status.csv`](docs/qa/dashboard-user-story-status.csv),
and [`qa/tool-selection-token-history/tracker.csv`](qa/tool-selection-token-history/tracker.csv)
files are supporting detail ledgers.

</details>

## Test Signal

The inventory badge reports pytest collection, not a blanket passing claim.
The CI badge links to the change-classified GitHub Actions workflow; individual
jobs run the lanes required for a change.

<details>
<summary>Shipped graph inventory</summary>

[![Skills](https://img.shields.io/badge/Skills-68%2C494-blue.svg)](https://stevesolun.github.io/ctx/catalog/?type=skill)
[![Agents](https://img.shields.io/badge/Agents-467-purple.svg)](https://stevesolun.github.io/ctx/catalog/?type=agent)
[![MCPs](https://img.shields.io/badge/MCPs-10%2C790-pink.svg)](https://stevesolun.github.io/ctx/catalog/?type=mcp-server)
[![Harnesses](https://img.shields.io/badge/Harnesses-207-orange.svg)](https://stevesolun.github.io/ctx/catalog/?type=harness)

The shipped artifact contract is a **79,958-node** graph covering
**68,494 skill entity pages**, **467 agents**, **10,790 MCP servers**, and
**207 harnesses**.

</details>

## License

MIT. See [LICENSE](LICENSE).
