---
hide:
  - navigation
---

# ctx

**Skill & agent recommendation and management for Claude Code.** ctx
watches what you develop, walks a knowledge graph of **1,768 skills and
443 agents**, and recommends the right ones on the fly — you decide what
loads and unloads. Powered by a Karpathy-style LLM wiki with persistent
memory that gets smarter every session.

Claude Code skills and agents are powerful, but at scale they become
unmanageable: discovery is hard past 1,700+ skills, loading everything
wastes context, related skills have hidden dependencies, and stale skills
rot silently. ctx treats the library as a **knowledge graph with
persistent memory** instead of a flat directory — a 3-layer wiki at
`~/.claude/skill-wiki/`, a 642K-edge graph linking skills by shared tags
and stack signals, and session hooks that update the wiki automatically
as you work. Nothing loads without your approval.

!!! tip "Install in two commands"

    ```bash
    git clone https://github.com/stevesolun/ctx.git && cd ctx
    pip install -e .
    ```

    Optional extras: `pip install -e ".[embeddings]"` for the semantic
    embedding backend, `pip install -e ".[dev]"` for the test + lint
    toolchain. Then `./install.sh python` (or `typescript` / `golang` /
    `swift` / `php`) to sync the language rule set.

## Start here

<div class="grid cards" markdown>

-   **Toolbox**

    ---

    Curated councils of skills and agents that fire at session-start,
    file-save, pre-commit, and session-end. Blocks `git commit` on
    HIGH/CRITICAL findings. 5 starter toolboxes ship out of the box.

    [:octicons-arrow-right-24: Toolbox overview](toolbox/index.md) ·
    [Starter toolboxes](toolbox/starters.md) ·
    [Verdicts & guardrails](toolbox/verdicts.md)

-   **Skill router**

    ---

    Scans the active repo, detects the stack from file signatures,
    walks the skill-stack matrix, and loads exactly the skills that
    apply — no more, no fewer.

    [:octicons-arrow-right-24: Router overview](skill-router/index.md) ·
    [Stack signatures](stack-signatures.md) ·
    [Skill-stack matrix](skill-stack-matrix.md)

-   **Health & quality**

    ---

    Structural health checks (missing frontmatter, orphan manifest
    entries, line-count drift) plus the four-signal quality score
    (telemetry · intake · graph · routing) that grades every skill
    A/B/C/D/F.

    [:octicons-arrow-right-24: Skill health](skills-health.md) ·
    [Memory anchoring](memory-anchor.md) ·
    [Lifecycle dashboard](skill-lifecycle-and-dashboard.md)

-   **Releases**

    ---

    **v0.5.0-rc1** — first open-source release candidate. MIT, CI-matrixed
    (Ubuntu + Windows × Python 3.11/3.12), 1,316 tests passing. Hardened
    against RCE, path traversal, and atomic-write races.

    [:octicons-arrow-right-24: CHANGELOG](https://github.com/stevesolun/ctx/blob/main/CHANGELOG.md) ·
    [Repository](https://github.com/stevesolun/ctx)

</div>

## Principles

- **Foundation first.** Data model, CLI, and starter bundles ship before any
  hook integration. Each phase is independently usable.
- **User-configurable everything.** Dedup policy, suggestion loudness,
  trigger set, council composition.
- **Evidence over opinion.** Suggestions cite real usage data plus
  knowledge-graph edges. No black-box prompts.
- **Token discipline.** Every council run honors `max_tokens` /
  `max_seconds` budgets.
