# Mattpocock Skills Import — Attribution & Usage

This directory mirrors Matt Pocock's personal `.claude/` skill set — opinionated
agent skills covering TDD, domain modelling, codebase architecture review,
github triage, and meta-workflows for working with Claude Code.

## Provenance

| Field | Value |
|---|---|
| Upstream repo | https://github.com/mattpocock/skills |
| Revision | `90ea8eec03d4ae8f43427aaf6fe4722653561a42` |
| Revision date | 2026-04-26 |
| Upstream license | MIT (see `LICENSE`) |
| Imported on | 2026-04-27 |
| Skill count | 21 |

## What's in here

Each top-level directory is one skill, with `SKILL.md` as the entry point and
optional supporting `.md` / `.sh` files alongside it (e.g. `tdd/deep-modules.md`,
`domain-model/ADR-FORMAT.md`, `git-guardrails-claude-code/scripts/block-dangerous-git.sh`).

| Skill | Purpose |
|---|---|
| `tdd` | Red-green-refactor TDD discipline (with deep-modules / mocking / refactoring sidecars) |
| `qa` | Interactive QA conversation that files GitHub issues using project domain language |
| `caveman` | Ultra-compressed communication mode (~75% token reduction) |
| `domain-model` | Stress-test plans against existing domain model + ADRs (with `ADR-FORMAT.md` + `CONTEXT-FORMAT.md`) |
| `ubiquitous-language` | DDD-style shared vocabulary discipline |
| `design-an-interface` | Generate multiple radically different API designs via parallel sub-agents |
| `improve-codebase-architecture` | Architecture review playbook (with `DEEPENING.md`, `INTERFACE-DESIGN.md`, `LANGUAGE.md` sidecars) |
| `github-triage` | Triage GitHub issues with agent-brief + out-of-scope guardrails |
| `triage-issue` | Single-issue triage workflow |
| `to-issues` | Convert plans/notes into well-formed issues |
| `to-prd` | Convert sketches into a product requirements document |
| `request-refactor-plan` | Plan a refactor before touching code |
| `migrate-to-shoehorn` | Migration playbook to the `shoehorn` library |
| `setup-pre-commit` | Pre-commit hook bootstrap |
| `scaffold-exercises` | Scaffold programming exercises |
| `git-guardrails-claude-code` | Block dangerous git ops in Claude Code (with hook script) |
| `obsidian-vault` | Obsidian vault management workflow |
| `edit-article` | Editing pass for article drafts |
| `grill-me` | Adversarial questioning to stress-test a plan |
| `write-a-skill` | Meta: how to write a skill |
| `zoom-out` | Force a higher-altitude review of current work |

## License compliance

Per the MIT license:
- Upstream `LICENSE` text is preserved alongside the imported files.
- Files are imported verbatim. The deployed copies (under `~/.claude/skills/`) prepend
  an HTML-comment attribution header before the original `---` frontmatter so
  provenance is visible inline; the original content below is unmodified.

## How to integrate

Skills are staged in this directory and **not** deployed to `~/.claude/skills/`
until you run the importer:

```bash
python imported-skills/mattpocock/build_manifest.py    # rebuild MANIFEST.json
python src/import_mattpocock_skills.py --dry-run       # preview
python src/import_mattpocock_skills.py --install       # deploy as mattpocock-<slug>
```

Each skill lands as `~/.claude/skills/mattpocock-<slug>/` with all its support
files copied alongside `SKILL.md`. Directory namespacing prevents collisions
with same-named skills already in the wiki (e.g. existing `tdd-orchestrator`
agent + `python-testing` skill coexist with `mattpocock-tdd`).

After install, refresh the wiki + graph:

```bash
python src/catalog_builder.py
python src/wiki_batch_entities.py --all
python -m ctx.core.wiki.wiki_graphify
```

## Why this set

mattpocock's skills are short, opinionated, and prose-style — closer to
checklists or playbooks than reference manuals. They complement the larger
catalogue (which leans dense + comprehensive) by providing crisp,
single-purpose workflows for everyday engineering tasks.

The `tdd`, `domain-model`, `ubiquitous-language`, and
`improve-codebase-architecture` set in particular form a cohesive DDD-leaning
toolkit. The `caveman`, `grill-me`, `zoom-out` set are useful behavioural
modes for steering a Claude Code session.

## Limitations

- **Frontmatter format** — uses YAML frontmatter with `name:` + `description:`;
  some entries use `disable-model-invocation: true` (Claude Code reads this).
  The importer preserves these fields as-is.
- **Tool assumptions** — `git-guardrails-claude-code` ships a `block-dangerous-git.sh`
  hook that expects POSIX `bash` on PATH; on Windows it requires Git-Bash or
  WSL. The hook is copied but not wired into your Claude Code settings — wire
  manually if you want it active.
- **Opinionated** — these reflect one engineer's workflow. Treat them as
  starting points; nothing here is universally correct (e.g. `grill-me`'s
  adversarial style isn't right for every team).
