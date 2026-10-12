---
hide:
  - navigation
---

# CTX — user-owned knowledge, relevant tools

**Bring your own skills, agents, and MCP records. CTX supplies the engine,
knowledge graph, and LLM-wiki structure—not a downloaded tool catalog.**

A new installation starts empty, local, and offline. While you work, a configured
Codex or Claude Code hook can ask the shared recommendation engine about your
current prompt. Other hosts can use the same engine through MCP, the CLI, or
Python. Each response contains at most five tool suggestions in total. No match
is a useful answer, not a reason to download a fallback library.

```bash
pip install --upgrade claude-ctx
ctx-init --model-mode skip
ctx recommend "review the Python API changes" --json
```

Requires CPython 3.11+ on Linux or macOS; use WSL2 on Windows.
This source contract is the bring-your-own-knowledge direction. A published
version badge does not mean these branch changes have already reached PyPI.

## Your knowledge, one engine

Use [entity onboarding](entity-onboarding.md) to add your own records and
[the graph guide](knowledge-graph.md) to connect and index them. The wiki stores
descriptions, provenance, tags, relationships, and reviewed installation metadata.
It is not an automatic permission grant or a guarantee of tool quality.

`CTX_WIKI_DIR` selects an absolute wiki directory shared by CLI, MCP, and native
hooks. Without an override, the compatibility default is
`~/.claude/skill-wiki/`; the path name does not make the engine Claude-only.
An existing user's knowledge is preserved during setup, not replaced with CTX data.

| Surface | Start here |
| --- | --- |
| Current task | `ctx recommend "describe your task" --json` |
| Repository scan | `ctx-scan-repo --repo . --recommend` |
| Claude Code events | `ctx-init --hooks --model-mode skip` |
| Codex events | `ctx-init --codex-hooks --model-mode skip` |
| MCP-aware host | `ctx-mcp-server` |
| Local dashboard | `python -m ctx_monitor serve` |

Native hooks are event-driven and suggestion-only. No separate paid LLM or
polling daemon is needed. The host controls event delivery, visible presentation,
tool use, and approval. Hook subprocess checks are not proof of actual
interoperability with every host release. Follow the
[attachment and acceptance guide](harness/attaching-to-hosts.md).

## Consent and limits

CTX suggests tools. Install them through your chosen host/workflow only after
explicit approval; the recommendation hooks do not perform installations.
Previously saved per-kind preapproval preferences do not authorize execution
through suggestion-only hooks. Unload and uninstall require separate approval.
Similarity and graph metadata never authorize execution.

The engine can rank and suppress candidates based on supplied context, but
delivery is not evidence of use. Hosts must report actual use and implement any
approved lifecycle action. The consuming host must enforce its own token,
time, credential, filesystem, and execution boundaries.

CTX aims to reduce unnecessary context. It promises no measured token, time,
quality, or cost savings without an actual controlled comparison.

## Explore

- [Your catalog](catalog.md): starts empty; inspect records you supply.
- [Knowledge graph](knowledge-graph.md): schema, relationships, building and querying.
- [Entity onboarding](entity-onboarding.md): author/import records and review updates.
- [Host integration](harness/attaching-to-hosts.md): hooks, MCP, Python, and loop adapters.
- [Dashboard](dashboard.md): local catalog, graph and activity views.
- [Telemetry](telemetry.md) and [threat model](threat-model.md): privacy and boundaries.
- [Toolbox](toolbox/index.md): optional user-configured plans and triggers.
- [Historical dataset publication](huggingface-publish.md): retired, not a setup prerequisite.

## Optional: CTX Fit

CTX Fit compares configurations within one coding-agent harness; it does not compare hosts. The selected test command is the verification authority. No paid live-provider trial qualifies this new product direction. Repository-native verification does not prove deliberately hostile code cannot deceive its own test runner.

Verification dependencies must be already available in the repository or a
supported offline environment. Final verification uses an isolated home without
network access; the user's package caches are not a supported dependency source.

Bare `ctx fit` is free, local and read-only. It invokes no model, spends
nothing, and on a plain run issues no `git` commands at all. It prints the
repository profile: detected languages, the AI coding setup already in place,
the verification commands the repository declares for itself, an agent-readiness
score with its component breakdown, and the highest-impact improvements.

In the current source, discovery includes C/C++ source and test evidence and
root Maven projects (`pom.xml`). Maven commands use an executable `./mvnw` when present,
otherwise `mvn`: `test` for tests and `package -DskipTests` for builds. Discovery
does not establish evaluation support: historical task derivation remains
limited to Python, JavaScript, TypeScript, Go, and Rust. A Java or C/C++ profile
and discovered test command alone do not supply representative Fit tasks.

| Command | What it costs | What it touches |
| --- | --- | --- |
| `ctx fit` | nothing; no model call | reads the working tree |
| `ctx fit --json` | nothing; no model call | reads the working tree, prints the profile as JSON |
| `ctx fit --catalog my-fit.json --dry-run` | nothing; no model call | reads your candidate catalog and runs read-only git queries (`log`, `show --name-only`, `ls-tree`, `rev-parse`) to derive representative tasks, then prints the experiment plan and a cost estimate |
| `ctx fit --catalog my-fit.json --test --budget N` | up to `N` dollars | runs candidates and verifies each trial with the repository's own test command |
| `ctx fit --catalog my-fit.json --test --budget N --apply --yes` | up to `N` dollars for evaluation | writes the winning configuration into your working tree; the write runs no git command, though evaluation does |
| `ctx fit --catalog my-fit.json --test --budget N --pr --yes` | up to `N` dollars for evaluation | creates a branch, commits, **pushes to your remote**, and opens a pull request through `gh` |

`--dry-run` reads history to derive tasks and writes nothing — not to the
repository, not to the index, not to any ref.

Fit has no default candidate catalog. Supply a local JSON file with the skill
material you want to compare; without it, planning reports no candidates and
evaluation abstains. `--catalog` alone also produces a free plan. For example,
save this as `my-fit.json`:

```json
{
  "schema": "ctx.fit.catalog-v1",
  "entries": [
    {
      "id": "skill:python-testing",
      "type": "skill",
      "signals": ["python", "test"],
      "content": "---\nname: python-testing\n---\n\nCheck edge cases and run the repository tests.\n"
    }
  ]
}
```

Each entry contains exactly these four fields. Skill names and signals use
bounded canonical lowercase tokens; the skill frontmatter name must match the
ID. Content is inline UTF-8, with no fetched URLs or referenced file reads.
Catalogs are limited to 512 entries and 2 MiB, with each skill and each complete
candidate subject to the 16 KiB context limit. Fit currently evaluates skills
as task context; the shared advisory recommender separately supports skills,
agents, and MCP servers. Plans bind the exact supplied bytes, preserve the
current setup as the baseline, and keep unresolved costs unknown. Supplying
catalog data does not authorize evaluation, installation, or activation.

Spending requires two explicit flags. `--budget` must be a finite, non-negative
number of US dollars; negative values, NaN, and infinity exit with an error
before profiling. Zero is valid input and authorizes no spending.
`--test` alone will not spend: without `--budget` CTX Fit only plans.
Run `ctx doctor` to see whether a real evaluation can run where you are.
A real evaluation needs
`pip install "claude-ctx[harness]"`, a matching provider credential, and
Bubblewrap on Linux. The current source uses CTX's bundled filesystem MCP;
it does not require Node.js or `npx` for Fit's workspace tools. The base install
can profile, plan, and simulate. Without a matching
provider credential, `--test` runs in simulation, which proves the pipeline but
never your repository. With a credential but a missing live prerequisite, CTX
refuses the run before trial setup. A simulated result is refused as evidence
for `--apply` and `--pr`.

### Ubuntu 24.04 Bubblewrap prerequisite

Ubuntu 24.04 restricts unprivileged user namespaces. Finding `bwrap` on `PATH`
is therefore insufficient: CTX must be able to start the same network-disabled
namespace used for repository commands. Install and load Ubuntu's packaged,
scoped `bwrap-userns-restrict` AppArmor profile for `/usr/bin/bwrap`:

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

Keep Ubuntu's global unprivileged-user-namespace restriction enabled. CTX uses
the targeted Bubblewrap profile instead of weakening that system-wide security
boundary. The profile is administrator-visible host policy for every
`/usr/bin/bwrap` caller, not a CTX-private setting; the commands above preserve
an existing local profile rather than overwriting it. On Linux, `ctx doctor`
runs a bounded `/bin/true` probe inside the same no-network namespace; it
executes no repository code and calls no model.
See [Ubuntu's AppArmor user-namespace guidance](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/#apparmor-unprivileged-user-namespace-restrictions)
and the [packaged Bubblewrap profile](https://gitlab.com/apparmor/apparmor/-/blob/master/profiles/apparmor/profiles/extras/bwrap-userns-restrict).

### `--apply` and `--pr` are different writes

Include `--apply` or `--pr` in the same
`ctx fit --catalog my-fit.json --test --budget N` invocation.
CTX Fit does not load evidence from prior evaluations, so either write flag
alone is refused. `--yes` authorizes both the displayed evaluation plan and
its resulting write. Without `--yes`, evaluation can be confirmed interactively,
but a winning result only previews the write; there is no second confirmation
prompt. Rerunning with `--yes` also reruns the evaluation and can spend again.

`--apply` writes files into your working tree on the current branch, after
printing every proposed change. The write itself runs no git command: nothing
is staged, committed, or pushed. Deriving tasks for the evaluation uses the
same read-only queries `--dry-run` uses.

Each proposed change names the file and whether CTX Fit is *creating* or
*modifying* it. Today every plan contains exactly one CTX-owned artifact,
`.ctx/fit-configuration.json`. The sidecar records the pinned model plus the
exact instruction and capability bytes that were evaluated, with their hashes;
ordinary `ctx run` invocations validate and activate that configuration.

| It printed | State after the write | Review with | Undo with |
| --- | --- | --- | --- |
| `modify: .ctx/fit-configuration.json` | existing sidecar replaced after a compare-and-swap check | `git diff -- .ctx/fit-configuration.json` when tracked; otherwise inspect the file directly | restore the tracked file from version control, or restore your saved copy if it was untracked |
| `create: .ctx/fit-configuration.json` | a previously absent sidecar is written; it may be untracked or a tracked file restored after deletion | `git status --short --untracked-files=all` and inspect the file directly | delete it if untracked; restore it from version control if tracked |

!!! warning "Version control cannot restore an untracked sidecar"

    CTX Fit does not rewrite `AGENTS.md`, `CLAUDE.md`, or other user-authored
    instruction files. Their evaluated bytes are embedded in the sidecar
    instead. If an existing untracked sidecar matters to you, save a copy
    before authorizing the write; version-control restore commands cannot
    recover an untracked file.

**`--pr` writes to a remote.** It creates a branch, commits the winning
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
if the working tree has changes CTX Fit did not write (untracked files
included — they would be carried onto the new branch), if `gh` is not installed
or not logged in, if the branch already exists, or if there is no remote to
push to. Each refusal names which one it was, exits non-zero, and leaves the
tree untouched. If a command fails partway, CTX Fit reports which one and how
many ran, and how to get back to the branch you were on; the files it had
already written stay in your working tree.


## Development and evidence

Use the [contribution guide](https://github.com/stevesolun/ctx/blob/main/CONTRIBUTING.md)
for proportional validation. The canonical acceptance board is
[`qa/feature_status.csv`](https://github.com/stevesolun/ctx/blob/main/qa/feature_status.csv).
Old releases and audit results remain historical evidence; they do not certify
the new empty-install or user-owned-catalog behavior.
