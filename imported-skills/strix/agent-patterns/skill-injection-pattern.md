---
name: skill-injection-pattern
description: Load up to N specialized skills into an agent at spawn time — dynamically tailor expertise per subtask
source: Distilled from Strix (https://github.com/usestrix/strix, Apache-2.0) — rev 15c95718
category: agent-architecture
---

# Skill Injection Pattern

Skills are specialized knowledge packages (markdown with YAML frontmatter). They are selected at agent-spawn time based on the subtask, then dynamically injected into the agent's system prompt. This is how a generic agent runtime becomes a domain expert on demand.

## Shape of a Skill

- Markdown document with YAML frontmatter `name` + `description` (the description is what the router matches against)
- Length budget: typically 150–250 lines (large enough for depth, small enough to combine several in one prompt)
- Content: advanced techniques, practical payloads/commands, validation methods, context-specific edge cases
- Self-contained — a skill should not require the agent to load other skills to understand it

## Selection Protocol

1. Orchestrator identifies the subtask (e.g. "test authentication mechanisms in API").
2. A router scores each available skill's description against the subtask.
3. Top N skills (Strix uses N=5) are selected — beyond that, prompt bloat degrades quality.
4. Selected skill bodies are concatenated into the agent's system prompt at construction time.

```python
# Shape of the spawn call
create_agent(
    task="Test authentication mechanisms in API",
    name="Auth Specialist",
    skills="authentication_jwt,business_logic",
)
```

## Skill Categories (useful split for a large library)

- **vulnerabilities/** — testing techniques for specific vuln classes (SQLi, XSS, IDOR, etc.)
- **frameworks/** — framework-specific patterns (FastAPI, Next.js, Django)
- **technologies/** — third-party platforms (Supabase, Firebase, payment gateways)
- **protocols/** — communication standards (GraphQL, WebSocket, OAuth)
- **tooling/** — CLI playbooks for specific tools (nmap, semgrep, sqlmap)
- **cloud/** — AWS/Azure/GCP/K8s testing patterns
- **reconnaissance/** — enumeration and mapping techniques
- **coordination/** — orchestration playbooks (how to work with other agents)
- **scan_modes/** — system-level mode definitions (quick/standard/deep)
- **custom/** — community-contributed / domain-specific

## Why This Beats Baking Knowledge into the Agent

- **Extensibility without retraining** — new skills are plain markdown files added to a directory; no code changes, no fine-tuning.
- **Context efficiency** — agents load only what they need; the same runtime handles wildly different domains.
- **Community contribution** — non-engineers (pentesters, auditors, domain experts) can contribute via PRs.
- **Version-controllable expertise** — skills are diffed, reviewed, and tested like any code artifact.

## Design Rules for Writing Skills

- Lead with the **YAML description** — it must be specific enough that the router can disambiguate.
- Include **practical examples** (payloads, commands, code snippets), not abstract principles.
- Include **validation methods** — how to confirm a finding and avoid false positives.
- Keep context-specific nuance (version, configuration, edge cases) in a clearly-labeled section.
- No external dependencies — if a skill references a tool, include the tool's invocation inline.
