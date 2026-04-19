# ctx

**Repo-aware skill routing, a pre/post-dev toolbox, a behavior miner, and
guardrail verdicts — all wired into Claude Code.**

ctx turns Claude Code from a single-shot assistant into a workflow with
memory. It watches which skills and agents you actually use, which files you
touch, which commits you make; it proposes bundles tailored to that pattern;
and it blocks pre-commit when the council it runs raises a HIGH or CRITICAL
finding.

## What this project ships

| Layer | Module | Purpose |
|---|---|---|
| **Skill router** | [`scan_repo.py`](https://github.com/stevesolun/ctx/blob/main/src/scan_repo.py) | Scan the active repo, detect stack, choose relevant skills/agents |
| **Toolbox config** | [`toolbox_config.py`](https://github.com/stevesolun/ctx/blob/main/src/toolbox_config.py) | Global + per-repo config merge (`~/.claude/toolboxes.json` + `.toolbox.yaml`) |
| **Toolbox CLI** | [`toolbox.py`](https://github.com/stevesolun/ctx/blob/main/src/toolbox.py) | `list`, `show`, `activate`, `init`, `export`, `import` |
| **Council runner** | [`council_runner.py`](https://github.com/stevesolun/ctx/blob/main/src/council_runner.py) | Token/time budgets, dedup policy, graph-informed scope |
| **Hooks** | [`toolbox_hooks.py`](https://github.com/stevesolun/ctx/blob/main/src/toolbox_hooks.py) | `session-start`, `pre-commit`, `session-end`, `file-save` |
| **Behavior miner** | [`behavior_miner.py`](https://github.com/stevesolun/ctx/blob/main/src/behavior_miner.py) | Co-invocation, cadence, file-type, commit-type signals |
| **Intent interview** | [`intent_interview.py`](https://github.com/stevesolun/ctx/blob/main/src/intent_interview.py) | State detection + interview flow |
| **Verdict guardrail** | [`toolbox_verdict.py`](https://github.com/stevesolun/ctx/blob/main/src/toolbox_verdict.py) | Record findings, escalate level, block on HIGH/CRITICAL |

## Quick links

- **[Toolbox overview](toolbox/index.md)** — what a toolbox is, how it's declared, how it runs.
- **[Starter toolboxes](toolbox/starters.md)** — `ship-it`, `security-sweep`, `refactor-safety`, `docs-review`, `fresh-repo-init`.
- **[Verdicts & guardrails](toolbox/verdicts.md)** — how the council blocks a bad commit with evidence.
- **[Skill router overview](skill-router/index.md)** — how the router picks skills from the active repo's stack.
- **[Skill health](skills-health.md)** — the four-signal quality score and where it surfaces.

## Install

```bash
git clone https://github.com/stevesolun/ctx.git
cd ctx
pip install -e .               # core
pip install -e ".[embeddings]" # optional: semantic embedding backend
pip install -e ".[dev]"        # optional: pytest, mypy, ruff
./install.sh python            # rules for your language (typescript / golang / swift / php)
```

## Releases

- **v0.5.0-rc1** — first open-source release candidate. MIT-licensed,
  CI-matrixed (Ubuntu + Windows × Python 3.11/3.12), 1,316 tests passing,
  installable via `pip install -e .`. Hardened against RCE/shell
  injection, path traversal, and race conditions in atomic writes. Full
  notes in [CHANGELOG.md](https://github.com/stevesolun/ctx/blob/main/CHANGELOG.md).

## Principles

- **Foundation first.** Data model, CLI, and starter bundles ship before any
  hook integration. Each phase is independently usable.
- **User-configurable everything.** Dedup policy, suggestion loudness,
  trigger set, council composition.
- **Evidence over opinion.** Suggestions cite real usage data plus
  knowledge-graph edges. No black-box prompts.
- **Token discipline.** Every council run honors `max_tokens` /
  `max_seconds` budgets.
