# Strix Skill Import — Attribution & Usage

This directory contains security-testing skills and agent-architecture patterns
sourced from [Strix](https://github.com/usestrix/strix), an open-source
multi-agent cybersecurity penetration testing tool.

## Provenance

| Field | Value |
|---|---|
| Upstream repo | https://github.com/usestrix/strix |
| Revision | `15c95718e600897a2a532a613a1c8fa6b712b144` |
| Revision date | 2026-04-13 |
| Upstream license | Apache License 2.0 (see `LICENSE`) |
| Imported on | 2026-04-17 |

## What's in here

- `skills/` — **38 upstream skill markdown files** copied verbatim, organized in
  their original category tree (vulnerabilities, tooling, frameworks, etc.).
- `agent-patterns/` — **5 agent-architecture notes** distilled from the Strix
  codebase (orchestrator/worker, skill-injection, shared-wiki-memory,
  sandboxed-tool-runtime, scan-mode-as-skill). These are original
  documentation, not copies — they describe reusable patterns observed in the
  Strix architecture.
- `MANIFEST.json` — machine-readable index with name, description, category,
  and line count for every entry.
- `build_manifest.py` — regenerator for `MANIFEST.json`.
- `LICENSE` — Apache 2.0 license text from the upstream repo.

## License compliance

Per Apache-2.0 §4:
- Upstream `LICENSE` is preserved alongside the imported files.
- No `NOTICE` file exists in upstream — none required to reproduce.
- Files have not been modified. Any downstream modification should add a
  prominent notice describing the change, per §4(b).

## Why these were imported

Strix's skill library is one of the highest-quality open catalogues of
pentesting knowledge available as structured markdown with YAML frontmatter.
Its format is **directly compatible** with this project's wiki/knowledge-graph
ingestion pipeline, which already consumes markdown-with-frontmatter for
skills and agents.

The agent-patterns notes capture *design* insights — how Strix structures a
multi-agent security runtime — that apply well beyond security:
orchestrator/worker decomposition, skill injection, shared wiki memory,
sandboxed tool execution, and scan-mode-as-skill are general multi-agent
architecture patterns.

## How to integrate

These files are **staged** in the repo but not yet deployed to
`~/.claude/skills/`. Two ways to consume them:

### Option A — Feed them to the knowledge graph directly

Use the wiki/graph builders with `--extra-dirs` to include this tree in the
scan without installing the skills globally. Requires a minor patch to
`catalog_builder.py` if not already supported.

### Option B — Install as global skills

Run `python src/import_strix_skills.py --install` (see that script for
options). It creates one directory per Strix skill under `~/.claude/skills/`
with the naming convention `strix-<category>-<slug>/SKILL.md` and prepends an
attribution header to each file so provenance stays visible inline.

## Which skills are highest value for general use

Not every Strix skill applies outside security testing. These generalise:

| Skill | Why it's broadly useful |
|---|---|
| `coordination/root_agent` | Multi-agent orchestration template — read first if designing any agent swarm |
| `coordination/source_aware_whitebox` | White-box coordination pattern applicable to any code-review agent |
| `scan_modes/{quick,standard,deep}` | Template for tiered operational modes |
| `custom/source_aware_sast` | Concrete SAST playbook — semgrep + ast-grep + gitleaks + trivy |
| `tooling/semgrep` | Semgrep playbook — universally useful for code review |
| `tooling/nmap` | Network recon — infra/ops workflows |
| All `agent-patterns/*` | Pure architecture patterns — 100% domain-agnostic |

Security-specific skills (XSS, SQLi, IDOR, etc.) remain valuable for any
project that touches web application code review.

## Limitations

- **Skills only, not the runtime** — Strix's agent runtime (the actual
  Python engine, Docker sandbox, tool-server) is not imported. Those
  components are design-referenced in `agent-patterns/` but not copied.
- **No fine-tuning or prompts-as-code** — these are human-readable skill
  documents, not prompt-chains or LangChain templates.
- **Source-aware tooling assumed to exist** — SAST skills assume `semgrep`,
  `ast-grep`, `gitleaks`, `trufflehog`, and `trivy` are installed on the
  agent's runtime path.
