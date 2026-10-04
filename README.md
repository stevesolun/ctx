# ctx

[![CI](https://github.com/stevesolun/ctx/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![Tests](https://img.shields.io/badge/Tests-9452_inventory-blue.svg)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)

**Find the cheapest AI coding setup that actually works on your repo.**

CTX Fit profiles your repository, evaluates a bounded set of AI coding configurations against real
tasks from its history, and produces the winner as a reviewable change. “Keep your current setup” is a valid result.

The rule is deterministic and lexicographic: discard candidates below the reliability floor, minimize
attributable cost, then prefer the simpler configuration. An LLM may explain a result; it never chooses one.

## Install

Requires CPython 3.11+ on Linux or macOS. Use WSL2 on Windows.

```bash
pip install --upgrade claude-ctx
cd /path/to/repository
ctx fit
```

Install `claude-ctx[harness]` for real model-backed evaluations. `ctx doctor` checks credentials
and runtime prerequisites without contacting a model or spending money.

## CLI Reference

| Task | Command |
| --- | --- |
| Profile a repository (`ctx` is the same as `ctx fit`) | `ctx fit` |
| Preview an evaluation | `ctx fit --dry-run` |
| Evaluate candidates | `ctx fit --test --budget 10` |
| Evaluate and write the winner locally | `ctx fit --test --budget 10 --apply --yes` |
| Evaluate and open a pull request with the winner | `ctx fit --test --budget 10 --pr --yes` |

Bare `ctx fit` is free, local, and read-only. It runs no model, spends nothing, and issues no git commands.
`--dry-run` adds read-only history queries. Spending needs `--test` plus `--budget`; simulation cannot authorize writes.

> **Release scope for 1.0.21.** CTX Fit compares configurations within one coding-agent harness;
> it does not compare Codex with Claude Code. The selected test command is the verification authority.
> Python campaign dependencies must be available without downloading them; for other supported
> ecosystems, the runtime must be usable, with verification dependencies already available in the repository.
> Final verification uses an isolated home and runs without network access. This does not prove
> deliberately hostile code cannot deceive its own test runner.
> Release qualification did not include a paid live-provider trial, so inspect `ctx doctor` and
> the dry run before spending.

### `--apply` writes the working tree

Include `--apply` in the same `ctx fit --test --budget N` invocation; prior evaluations are not loaded.
`--yes` authorizes the evaluation and resulting write. Without it, a winning result is only previewed.
The write itself runs no git command, although evaluation uses read-only history queries.

For `modify: .ctx/fit-configuration.json` or `create: .ctx/fit-configuration.json`, check tracking with
`git status` before undoing a write: version control cannot recover an untracked file.
See the [write, review, and recovery guide](https://stevesolun.github.io/ctx/#-apply-and-pr-are-different-writes)
for the sidecar contract and recovery steps.

**`--pr` writes to a remote.** It uses the same evaluation and `--yes` requirements and runs read-only probes including
`git status`, remote checks, and `gh auth status`. It refuses before writing if `gh` is not installed
or not logged in, or if the tree and remote are unsuitable. It then performs:

```text
git checkout -b ctx-fit/<timestamp>
git add -- <paths>
git commit -m "<pull request title>"
git push --set-upstream origin ctx-fit/<timestamp>
gh pr create --title "<pull request title>" --body-file -
```

It creates a branch, commits, pushes, and opens a pull request. It never merges.

## Recommendations and private knowledge

The older recommendation surface still ships. It can use the CTX graph, your own graph, or an
enriched combination to suggest at most five relevant skills, agents, and MCP servers:

```bash
ctx-init --graph --model-mode skip
ctx-scan-repo --repo . --recommend
```

During `ctx-init`, choose install consent per kind: ask each time or preapprove automatic installation.
Preapproval does not authorize unload. See [entity onboarding](https://stevesolun.github.io/ctx/entity-onboarding/),
the [knowledge graph guide](https://stevesolun.github.io/ctx/knowledge-graph/), and [host integration](https://stevesolun.github.io/ctx/harness/attaching-to-hosts/).

### Shipped graph inventory

[![Skills](https://img.shields.io/badge/Skills-68%2C494-blue.svg)](https://stevesolun.github.io/ctx/catalog/?type=skill)
[![Agents](https://img.shields.io/badge/Agents-467-purple.svg)](https://stevesolun.github.io/ctx/catalog/?type=agent)
[![MCPs](https://img.shields.io/badge/MCPs-10%2C790-pink.svg)](https://stevesolun.github.io/ctx/catalog/?type=mcp-server)
[![Harnesses](https://img.shields.io/badge/Harnesses-207-orange.svg)](https://stevesolun.github.io/ctx/catalog/?type=harness)

The release publishes a **79,958-node** graph with **68,494 skill entity pages**, **467 agents**,
**10,790 MCP servers**, and **207 harnesses** as verified, manifest-bound assets rather than Git or
Git LFS objects.

Every clean install includes the project-owned, MIT-licensed, no-key fallbacks `ctx-python-testing`,
`ctx-python-state-protocols`, `ctx-python-input-boundaries`, `ctx-python-api-compatibility`, `ctx-javascript-testing`,
`ctx-rust-patterns`, `ctx-typescript`, `ctx-python-reviewer`, and `ctx-core`. The installer preserves unrelated skill,
agent, and MCP content, refreshes runtime-managed harness pages, and fails closed on unexpected reserved paths.

## Example user stories

`CLI-002` covers bounded repository recommendations, `CLI-026` reviewed harness installation, and
`API-011` validated local entity management. Current status lives only in
[`qa/feature_status.csv`](qa/feature_status.csv).

See the [full documentation](https://stevesolun.github.io/ctx/) for evaluation semantics, platform setup, privacy and telemetry, configuration, and APIs.

## License

MIT. See [LICENSE](LICENSE).
