# ctx

[![CI](https://github.com/stevesolun/ctx/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![Tests](https://img.shields.io/badge/Tests-5691_inventory-blue.svg)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)

ctx is a Python CLI and library that recommends a small, relevant set of
skills, agents, and MCP servers for a repository or task. It can use your
organization's local/private knowledge or the shipped graph. Harness selection
for custom, API, and local models is a separate workflow. Recommendations do
not install or load tools without an operator action or approval.

**Release status:** [v1.0.20](https://github.com/stevesolun/ctx/releases/tag/v1.0.20)
is the current GitHub and [PyPI](https://pypi.org/project/claude-ctx/) release;
this source tree declares `1.0.21` for unreleased work.

## Install

Requires Python 3.11 or newer.

```bash
pip install claude-ctx
```

## Quickstart

From the repository you want to analyze, install the runtime graph and request
recommendations:

```bash
ctx-init --graph --model-mode skip
ctx-scan-repo --repo . --recommend
```

`ctx-init --graph` uses the bundled runtime artifact in a source checkout or
downloads the matching release asset for a package install. The full packed
wiki is optional; see the [knowledge graph guide](https://stevesolun.github.io/ctx/knowledge-graph/).

Every clean graph install seeds six project-owned, MIT-licensed, no-key
fallbacks: `ctx-python-testing`, `ctx-javascript-testing`,
`ctx-rust-patterns`, `ctx-typescript`, the `ctx-python-reviewer` agent, and the
local `ctx-core` MCP server. ctx preserves unrelated skill, agent, MCP, and
converted-skill content; runtime-managed harness pages are refreshed from the
installed artifact. Installation fails closed if a reserved `ctx-*` identity,
body, overlay, or parent path is unexpected.

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
| Initialize ctx and install graph data | `ctx-init` | [Knowledge graph](https://stevesolun.github.io/ctx/knowledge-graph/) |
| Scan a repository and get recommendations | `ctx-scan-repo`, `ctx-recommend` | [Entity onboarding](https://stevesolun.github.io/ctx/entity-onboarding/) |
| Connect an MCP, Python, or CLI host | `ctx-mcp-server`, `ctx` | [Host integration](https://stevesolun.github.io/ctx/harness/attaching-to-hosts/) |
| Inspect the local runtime | `ctx-monitor serve` | [Dashboard](https://stevesolun.github.io/ctx/dashboard/) |
| Review or export telemetry | `ctx-telemetry-export`, `ctx-telemetry-retention` | [Telemetry](https://stevesolun.github.io/ctx/telemetry/) |

See the [full documentation](https://stevesolun.github.io/ctx/) for configuration,
APIs, entity lifecycle, and operational details.

## Example user stories

| Tracker ID | User outcome |
| --- | --- |
| `CLI-002` | Scan a repository and receive a bounded skill, agent, and MCP recommendation set. |
| `CLI-026` | Review a custom-model harness recommendation with `ctx-harness-install --dry-run` before installation. |
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
