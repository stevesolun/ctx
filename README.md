# CTX — bring your own tools

[![CI](https://github.com/stevesolun/ctx/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)

**The right few tools for the work you are doing now. Your knowledge, your choice.**

CTX provides a local knowledge graph, an LLM-wiki structure, and one shared recommendation
engine for skills, agents, and MCP servers. You or your organization supply the records and tools.
CTX suggests up to five relevant options across all three kinds—or none when nothing fits.

**The default installation is empty and offline.** No downloaded skill library, agent pack,
MCP catalog, corpus, or executable tool bodies are bundled. CTX does not maintain a public tool catalog
for you. Adding a record is not the same as installing or running the tool it describes.

## Install

Requires CPython 3.11+ on Linux or macOS. Use WSL2 on Windows.

```bash
pip install --upgrade claude-ctx
ctx-init --model-mode skip
ctx recommend "review the Python API changes" --json
```

An empty result is expected until you add your own knowledge. Set `CTX_WIKI_DIR` to an absolute
directory to select the same private wiki for the CLI, MCP server, and native hooks.
See [entity onboarding](https://stevesolun.github.io/ctx/entity-onboarding/) for authoring/import
and the [knowledge graph guide](https://stevesolun.github.io/ctx/knowledge-graph/) for indexing.

## How it works during your work

1. You add descriptions, tags, connections, and reviewed installation metadata for tools you trust.
2. A connected host supplies the current task or prompt. The shared engine matches that context
   against your graph/wiki and returns a mixed list of at most five useful candidates.
3. You or the host decide what to use. Recommendations include relevance and availability;
   they are not permission to install, execute, activate, or unload a tool.

Use `ctx-init --hooks --model-mode skip` for Claude Code hooks, or
`ctx-init --codex-hooks --model-mode skip` for Codex hooks. The adapters run on host events:
there is no separate paid LLM or polling daemon. Host configuration, trust approval, and sometimes
a restart are required. Hooks provide advisory context; whether a suggestion is shown to you or
used by an agent depends on the host. Installing the Python package alone does not observe your work.

Other MCP hosts can start `ctx-mcp-server`; Python integrations can call `recommend_bundle`.
All paths use the same user-owned knowledge. See the
[host integration guide](https://stevesolun.github.io/ctx/harness/attaching-to-hosts/) for setup,
read-only permissions, and the distinction between local adapter tests and actual host acceptance.

## CLI Reference

| Task | Command |
| --- | --- |
| Show setup and command guidance | `ctx` |
| Suggest tools for a task | `ctx recommend "describe your task" --json` |
| Scan a repository for recommendations | `ctx-scan-repo --repo . --recommend` |
| Run the local knowledge dashboard | `python -m ctx_monitor serve` |
| Profile a repository with the optional evaluator | `ctx fit` |
| Check optional evaluation prerequisites | `ctx doctor` |
| Run the optional agent harness | `ctx advanced run --help` |

## Safety and honest limits

CTX suggests tools. Install them through your chosen host/workflow only after
explicit approval; the recommendation hooks do not perform installations.
Previously saved per-kind preapproval preferences do not authorize execution through the
current suggestion-only prompt path. Tool descriptions,
similarity scores, and installation strings are untrusted data—not execution authority.
Unload and uninstall require separate permission.

CTX cannot infer that an agent used knowledge merely because it received it. Host-reported use
and lifecycle support vary; CTX does not claim universal automatic loading or unloading.
It aims to reduce unnecessary context, time, and cost, but **no token, time, quality, or cost
savings are guaranteed**. Compare real task outcomes before making those claims.

## Optional: CTX Fit

CTX Fit is a separate, explicitly invoked evaluator. It profiles a repository and can compare
capability configurations using repository-native verification. It rejects candidates below the
reliability floor, minimizes attributable cost, then prefers simplicity. An LLM does not choose
the winner. With an empty catalog, no capability change is a valid result.

Bare `ctx fit` is free, local, and read-only. Spending needs `--test --budget N`.
Install `claude-ctx[harness]` only if you want real model-backed evaluations.

> Fit compares configurations within one coding-agent harness, not Codex against Claude Code.
> The selected test command is the verification authority. Verification dependencies must be
> already available in the repository or supported offline environment. Final verification uses
> an isolated home without network access. This does not prove deliberately hostile code cannot
> deceive its own test runner. No paid live-provider trial qualifies this new product direction.

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

## Project status

The source contract is changing from a shipped catalog to user-owned infrastructure.
A version badge reports the published package; it does not prove that an unreleased branch is
already on PyPI. Current acceptance and remaining external checks live in
[`qa/feature_status.csv`](qa/feature_status.csv).

See the [documentation](https://stevesolun.github.io/ctx/) for private knowledge, host setup,
privacy, telemetry, and the optional evaluation utility.

## License

MIT. See [LICENSE](LICENSE).
