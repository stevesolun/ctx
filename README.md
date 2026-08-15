# ctx

[![CI](https://github.com/stevesolun/ctx/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![Tests](https://img.shields.io/badge/Tests-8780_inventory-blue.svg)](https://github.com/stevesolun/ctx/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-ctx.svg)](https://pypi.org/project/claude-ctx/)

**Find the cheapest AI coding setup that actually works on your repo.**

CTX Fit analyzes your repository, tests promising AI coding configurations
against real tasks in it, and produces the winning configuration as a
reviewable change — in your working tree with `--apply`, or as a pull request
with `--pr`. It picks the *cheapest* setup that *reliably* works — reliability
is a requirement, not a tie-break — and if nothing beats what you already have,
it says so.

The winner is chosen by a fixed rule, not a score: discard every candidate
below the reliability floor, then minimize attributable cost, then break ties
toward the simpler configuration. An LLM may explain a result; it never
decides one.

> **Release scope (1.0.21).** CTX Fit compares capability configurations within
> one coding-agent harness; it does not compare Codex, Claude Code, or other
> harnesses against one another. It recognizes and can run repository-native
> verification commands for Python, JavaScript/TypeScript, Go, Rust, and Make,
> and treats the selected test command as the verification authority. For an
> installable Python project, CTX Fit builds a campaign environment and installs
> it without network access; its build backend and dependencies must already be
> available without downloading them. In the other ecosystems, verification is
> supported only when the runtime is usable
> from the host `PATH` under an isolated home and the verification dependencies
> are already available in the repository. Final verification uses that
> isolated home and runs without network access, so a user's package caches
> are not a supported dependency source. This is evidence for normal
> development; it does not prove that deliberately hostile code cannot deceive
> its own test runner. Release qualification did not include a paid
> live-provider trial, so inspect `ctx doctor` and the dry run before authorizing
> spend.

```bash
pip install --upgrade claude-ctx
cd /path/to/my-project
ctx fit
```

Bare `ctx fit` is free, local and read-only: it runs no model, spends nothing,
and issues no `git` commands at all. Example output, abridged from a real run
against this repository:

```text
Repository: /path/to/ctx
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
  build      python -m build
             from pyproject.toml [build-system] (medium confidence)

AI agent readiness
  91/100
    Verification           30/30
    Instructions           20/20
    Environment             6/15
    CI enforcement         15/15
    Tool safety            10/10
    Context tractability   10/10

Highest-impact improvements
  1. Commit a dependency lockfile. (+9)
     no dependency lockfile is committed

This repository has the static evidence needed to plan an evaluation: it
declares deterministic tests. Whether those tests can execute is checked only
inside the campaign.
```

Requires Python 3.11 or newer. Add `--json` for machine-readable output, or
`--dry-run` to see what a full evaluation would involve. `--dry-run` does read
your history — it runs read-only git queries (`log`, `show --name-only`,
`ls-tree`, `rev-parse`) to derive representative tasks — and writes nothing:
not to the repository, not to the index, not to any ref.

Beyond the free profile, `ctx fit --test --budget N` evaluates candidate
configurations against those tasks. Spending needs both flags: `--test`
without `--budget` only plans. Run `ctx doctor` to see whether a real
evaluation can run here. A real evaluation needs
`pip install "claude-ctx[harness]"`, Node.js with `npx` for the
workspace-filesystem MCP, a matching provider credential, and Bubblewrap on
Linux; the base install can profile, plan, and simulate. Without a matching
provider credential, `--test` runs in simulation, which proves the pipeline but
not your repository. With a credential but a missing live prerequisite, CTX
refuses the run before trial setup. A simulated result is refused as evidence
for `--apply` and `--pr`.

#### Ubuntu 24.04: enable Bubblewrap's packaged AppArmor profile

Ubuntu 24.04 restricts unprivileged user namespaces, and merely installing
`bwrap` does not prove it can start the network-disabled namespace CTX uses for
repository commands. Install and load Ubuntu's packaged, scoped
`bwrap-userns-restrict` profile for `/usr/bin/bwrap`:

```bash
sudo apt update
sudo apt install bubblewrap apparmor-profiles apparmor-utils
if [ ! -e /etc/apparmor.d/bwrap-userns-restrict ]; then
  sudo install -m 0644 \
    /usr/share/apparmor/extra-profiles/bwrap-userns-restrict \
    /etc/apparmor.d/bwrap-userns-restrict
fi
sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict
ctx doctor
```

Keep Ubuntu's global unprivileged-user-namespace restriction enabled; CTX uses
the targeted Bubblewrap profile instead of weakening that system-wide security
boundary. The profile is administrator-visible host policy for every
`/usr/bin/bwrap` caller, not a CTX-private setting; the commands above preserve
an existing local profile rather than overwriting it. `ctx doctor` proves this
path with a bounded `/bin/true` probe in the same no-network namespace. It
executes no repository code and calls no model.
See [Ubuntu's AppArmor user-namespace guidance](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/#apparmor-unprivileged-user-namespace-restrictions)
and the [packaged Bubblewrap profile](https://gitlab.com/apparmor/apparmor/-/blob/master/profiles/apparmor/profiles/extras/bwrap-userns-restrict).

### `--apply` and `--pr` write different things

`ctx fit --apply` writes the winning configuration into your working tree, on
whatever branch you are standing on. It prints every proposed change first and
stops there unless you pass `--yes`. **The write itself runs no git command**:
nothing is staged, committed, or pushed. Getting to it does run git — `--apply`
is refused without evidence from `ctx fit --test --budget N`, and deriving the
tasks for that evaluation uses the same read-only queries `--dry-run` uses.

Each proposed change names the file and whether CTX Fit is *creating* or
*modifying* it. Today every plan contains exactly one CTX-owned artifact,
`.ctx/fit-configuration.json`. The sidecar records the pinned model plus the
exact instruction and capability bytes that were evaluated, with their hashes;
ordinary `ctx run` invocations validate and activate that configuration.

| It printed | State after the write | Review with | Undo with |
| --- | --- | --- | --- |
| `modify: .ctx/fit-configuration.json` | existing sidecar replaced after a compare-and-swap check | `git diff -- .ctx/fit-configuration.json` when tracked; otherwise inspect the file directly | restore the tracked file from version control, or restore your saved copy if it was untracked |
| `create: .ctx/fit-configuration.json` | new and **untracked** until you add it | `git status --short --untracked-files=all` and inspect the file directly | delete `.ctx/fit-configuration.json` |

CTX Fit does not rewrite `AGENTS.md`, `CLAUDE.md`, or other user-authored
instruction files. Their evaluated bytes are embedded in the sidecar instead.
If an existing untracked sidecar matters to you, save a copy before confirming
the write; version-control restore commands cannot recover an untracked file.

**`ctx fit --pr` writes to a remote.** It creates a branch, commits the winning
configuration, pushes it to `origin`, and opens a pull request through the
GitHub CLI. Before running anything it prints the pull-request body, the files
it will write, and the exact command sequence:

```text
git checkout -b ctx-fit/<timestamp>
git add -- <paths>
git commit -m "<pull request title>"
git push --set-upstream origin ctx-fit/<timestamp>
gh pr create --title "<pull request title>" --body-file -
```

Without `--yes` it stops there and changes nothing. With `--yes` it writes those
files into the working tree and then runs those five commands, in that order and
no others. Before any of them runs, the gate described below runs read-only
probes — `git rev-parse`, `git status`, `git remote get-url`, and `gh auth
status` — which is what lets every refusal leave the repository exactly as it
found it. CTX Fit never merges.

`--pr` refuses before touching anything if you are not inside a git repository,
if the working tree has changes CTX Fit did not write (including untracked
files — they would be carried onto the new branch), if `gh` is not installed or
not logged in, if the branch already exists, or if there is no remote to push
to. Each refusal says which one it was, exits non-zero, and leaves the tree
untouched. If a command fails partway, CTX Fit reports which one and how many
ran, and how to get back to the branch you were on; the files it had already
written stay in your working tree.

**Release:** [v1.0.21](https://github.com/stevesolun/ctx/releases/tag/v1.0.21)
is the CTX Fit release. The distribution remains
[`claude-ctx`](https://pypi.org/project/claude-ctx/); the installed command is
`ctx`.

## Install

Requires CPython 3.11 or newer. Linux and macOS are the tested host platforms;
other POSIX systems are best-effort. Native Windows and PowerShell are not
supported. On a Windows machine, run ctx inside WSL2 as a Linux installation.

```bash
pip install claude-ctx
```

For real model-backed evaluations, install `claude-ctx[harness]` instead and
make Node.js plus `npx` available. Linux hosts also need Bubblewrap.
`ctx doctor` checks these prerequisites without contacting a model or spending
money.

Version `1.0.21` ships `ctx fit` as the primary command, plus `ctx doctor` and
`ctx advanced`. The established agent-loop spellings (`ctx run`, `ctx resume`,
`ctx sessions`) remain supported.

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

These controls are available in release `1.0.21`.
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
| Profile a repository for AI coding readiness | `ctx` (same as `ctx fit`) | this README |
| Evaluate candidate configurations and pick a winner | `ctx fit --test --budget N` | this README |
| Write the winner into the working tree | `ctx fit --apply` | this README |
| Open a pull request with the winner | `ctx fit --pr` | this README |
| Diagnose whether a real evaluation can run here | `ctx doctor` | this README |
| Initialize the recommendation surface and install graph data | `ctx-init` | [Knowledge graph](https://stevesolun.github.io/ctx/knowledge-graph/) |
| Scan a repository for skill/agent/MCP recommendations | `ctx-scan-repo` | [Entity onboarding](https://stevesolun.github.io/ctx/entity-onboarding/) |
| Connect an MCP, Python, or CLI host | `ctx-mcp-server`, `ctx advanced run` | [Host integration](https://stevesolun.github.io/ctx/harness/attaching-to-hosts/) |
| Inspect the local recommendation runtime | `python -m ctx_monitor serve` | [Dashboard](https://stevesolun.github.io/ctx/dashboard/) |
| Review or export telemetry | `ctx-telemetry-export`, `ctx-telemetry-retention` | [Telemetry](https://stevesolun.github.io/ctx/telemetry/) |

This table describes release `1.0.21`. Bare `ctx` with no arguments runs the Fit
profile, which is why it is not listed under the recommendation surface.

The agent-loop harness (`run`, `resume`, `sessions`) is still there and still
supported. It moved under `ctx advanced` so the top-level help stays about the
product, but the original spellings keep working: `ctx run ...` and
`ctx advanced run ...` are the same command, and `ctx run --help` still prints
the harness options. Only `ctx --help` changed — it advertises `fit`, `doctor`
and `advanced`. Maintenance utilities that used to be console scripts are
reached with `python -m` — for example `python -m ctx.cli.recommend` or
`python -m ctx.core.quality.dedup_check`.

See the [full documentation](https://stevesolun.github.io/ctx/) for configuration,
APIs, entity lifecycle, and operational details.

## Example user stories

| Tracker ID | User outcome |
| --- | --- |
| `CLI-002` | Scan a repository and receive a bounded skill, agent, and MCP recommendation set. |
| `CLI-026` | Review a custom-model harness recommendation with `python -m harness_install <slug> --dry-run` before installation. The slug is required: `--dry-run` on its own exits 2. |
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
