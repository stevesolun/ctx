# Harness Recommendations And Entity Onboarding Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add first-class harness recommendation support and document a repeatable way for users to add MCP servers, skills, agents, and harnesses to the ctx knowledge graph.

**Architecture:** Treat `harness` as a fourth recommendable entity type, not as a special-case note or markdown-only convention. The existing split between wiki pages, graph nodes, resolver manifests, generic harness tools, dashboard APIs, and docs must be extended together so all recommendation surfaces stay identical. Start with catalog/recommend-only harnesses; do not auto-install or execute harness setup commands until a later policy-gated installer exists.

**Tech Stack:** Python 3.11+, pytest, Ruff, mypy, NetworkX node-link graph, ctx wiki pages, ctx graphify, ctx resolver, ctx generic tools, Markdown docs.

---

## Sources Reviewed

- `https://github.com/earthtojake/text-to-cad`
  - Useful as a real harness exemplar. It is a source-controlled CAD workflow with bundled CAD/URDF skills, explicit regeneration commands, a local viewer, STEP/STL/DXF/GLB/URDF outputs, and stable `@cad[...]` geometry references.
  - License: MIT in the cloned repository.
- `https://github.com/eugeniughelbur/obsidian-second-brain`
  - Useful as a skill/onboarding exemplar and wiki-pattern reference. It provides one skill, 31 commands, role presets, an AI-first vault schema, background/scheduled agents, and `mcp-obsidian` fallback guidance.
  - License: MIT in the cloned repository.
- `https://github.com/thedotmack/claude-mem`
  - Useful as an architecture reference, not as code to copy. The valuable ideas are progressive disclosure, lifecycle hook observation, privacy exclusion tags, citations to stored observations, claim-confirm queues, and explicit hook degradation rules.
  - License: AGPL-3.0 in `package.json` and `LICENSE`; do not copy implementation code into ctx without a separate license decision.

## Decision

Yes, ctx can and should support harness recommendations.

Definition for ctx:

> A harness is a runnable or source-controlled workflow environment that gives an agent a domain-specific execution loop, validation loop, viewer, or lifecycle integration. It may contain skills, MCP servers, agents, commands, scripts, viewers, datasets, or project templates, but the harness itself is the higher-level operating surface.

Examples:

- `text-to-cad`: harness for CAD/URDF generation and local review.
- Future user-provided harnesses: evaluation rigs, browser-use environments, benchmark runners, simulation loops, data-analysis notebooks, local app test harnesses.

Non-goals for the first implementation:

- No automatic `git clone`, `npm install`, `pip install`, or shell execution from harness recommendations.
- No copying external repository code into ctx.
- No separate recommendation engine. Harnesses must flow through the same shared ranking path as skills, agents, and MCP servers.

## Entity Schema

Harness wiki page path:

```text
~/.claude/skill-wiki/entities/harnesses/<slug>.md
```

Minimum frontmatter:

```yaml
---
title: text-to-cad
created: 2026-04-28
updated: 2026-04-28
type: harness
status: cataloged
tags: [cad, urdf, geometry, robotics, viewer, validation]
source: github
homepage: https://github.com/earthtojake/text-to-cad
license: MIT
install_risk: medium
execution_policy: user-confirmed
setup_commands:
  - git clone https://github.com/earthtojake/text-to-cad.git
  - python3.11 -m venv .venv
  - ./.venv/bin/pip install -r requirements-cad.txt
  - cd viewer && npm install
recommended_for:
  - CAD model generation
  - URDF robot description generation
  - geometry inspection and snapshot review
related_skills: [cad, urdf]
related_mcp_servers: []
---
```

Body template:

```markdown
# text-to-cad

## Overview
Open-source local harness for agent-driven CAD and URDF workflows.

## Capabilities
- Generate source-controlled CAD models.
- Export STEP, STL, DXF, GLB, topology, and URDF artifacts.
- Inspect outputs in a local viewer.
- Use stable geometry references for follow-up edits.

## Use When
- The task asks for 3D parts, assemblies, fixtures, robot geometry, or URDF.
- The agent needs a regenerate-review-commit workflow rather than one-off code.

## Safety Notes
- Do not run setup commands without user confirmation.
- Treat generated CAD artifacts as outputs; edit source generators first.
```

## Phase 1: Central Entity Type Model

**Files:**
- Create: `src/ctx/core/entity_types.py`
- Create: `src/tests/test_entity_types.py`

**Step 1: Write tests for all supported types**

Cover:

- `skill -> entities/skills/<slug>.md`
- `agent -> entities/agents/<slug>.md`
- `mcp-server -> entities/mcp-servers/<shard>/<slug>.md`
- `harness -> entities/harnesses/<slug>.md`
- invalid type raises a clear `ValueError`
- plural subject names round-trip to singular entity types

Run:

```powershell
python -m pytest src\tests\test_entity_types.py -q
```

Expected initial result: fail because `ctx.core.entity_types` does not exist.

**Step 2: Implement `entity_types.py`**

Provide:

- `SUPPORTED_ENTITY_TYPES`
- `SUBJECT_FOR_ENTITY_TYPE`
- `ENTITY_TYPE_FOR_SUBJECT`
- `entity_page_relpath(entity_type, slug)`
- `entity_wikilink(entity_type, slug)`
- `related_section_header(entity_type)`
- `validate_entity_type(entity_type)`

Keep the MCP sharding function here so `wiki_sync`, `wiki_query`, and `wiki_graphify` do not each carry their own copy.

**Step 3: Verify**

```powershell
python -m pytest src\tests\test_entity_types.py -q
python -m ruff check src\ctx\core\entity_types.py src\tests\test_entity_types.py
python -m mypy src\ctx\core\entity_types.py src\tests\test_entity_types.py
```

**Step 4: Commit**

```powershell
git add src\ctx\core\entity_types.py src\tests\test_entity_types.py
git commit -m "feat: add shared entity type model"
```

Stop for review before Phase 2.

## Phase 2: Wiki Read/Write Support For Harness Pages

**Files:**
- Modify: `src/ctx/core/wiki/wiki_sync.py`
- Modify: `src/ctx/core/wiki/wiki_query.py`
- Modify: `src/tests/test_wiki_sync.py`
- Modify: `src/tests/test_query.py`
- Modify: `docs/knowledge-graph.md`

**Steps:**

1. Replace local subject/type maps in `wiki_sync.py` with `ctx.core.entity_types`.
2. Add `entities/harnesses` to `ensure_wiki()`.
3. Route manifest entries with `entity_type: harness` to `entities/harnesses/<slug>.md`.
4. Add `## Harnesses` to index creation and update logic.
5. Update `wiki_query.load_all_pages()` so search includes harness pages.
6. Add tests proving harness pages are created, indexed, and returned by wiki search.
7. Update `docs/knowledge-graph.md` count/type wording from three types to four type-capable schema, while keeping current released counts unchanged until harness catalog data exists.

Verification:

```powershell
python -m pytest src\tests\test_wiki_sync.py src\tests\test_query.py -q
python -m ruff check src\ctx\core\wiki\wiki_sync.py src\ctx\core\wiki\wiki_query.py src\tests\test_wiki_sync.py src\tests\test_query.py
python -m mypy src\ctx\core\wiki\wiki_sync.py src\ctx\core\wiki\wiki_query.py src\tests\test_wiki_sync.py src\tests\test_query.py
```

Stop for review before Phase 3.

## Phase 3: Graphify And Recommendation Surface Support

**Files:**
- Modify: `src/ctx/core/wiki/wiki_graphify.py`
- Modify: `src/ctx/adapters/generic/ctx_core_tools.py`
- Modify: `src/tests/test_wiki_graphify_mcp.py`
- Modify: `src/tests/test_recommendations.py`
- Modify: `src/tests/test_recommendation_surfaces_golden.py`

**Steps:**

1. Replace local path/link/header helpers in `wiki_graphify.py` with `ctx.core.entity_types`.
2. Include `entities/harnesses/*.md` as graph nodes with `type: harness`.
3. Include harness nodes in semantic, tag, and slug-token edge generation.
4. Allow `ctx__recommend_bundle`, `ctx__graph_query`, `ctx__wiki_search`, and `ctx__wiki_get` to return/read `harness`.
5. Extend golden recommendation tests to include a harness node and prove direct recommender, generic ctx tool, and resolver-facing graph paths preserve order, type, and normalized score.

Verification:

```powershell
python -m pytest src\tests\test_wiki_graphify_mcp.py src\tests\test_recommendations.py src\tests\test_recommendation_surfaces_golden.py -q
python -m ruff check src\ctx\core\wiki\wiki_graphify.py src\ctx\adapters\generic\ctx_core_tools.py src\tests\test_wiki_graphify_mcp.py src\tests\test_recommendations.py src\tests\test_recommendation_surfaces_golden.py
python -m mypy src\ctx\core\wiki\wiki_graphify.py src\ctx\adapters\generic\ctx_core_tools.py src\tests\test_wiki_graphify_mcp.py src\tests\test_recommendations.py src\tests\test_recommendation_surfaces_golden.py
```

Stop for review before Phase 4.

## Phase 4: Resolver And Scan Flow Support

**Files:**
- Modify: `src/ctx/core/resolve/resolve_skills.py`
- Modify: `src/scan_repo.py`
- Modify: `src/tests/test_resolve_skills.py`
- Modify: `src/tests/test_scan_repo.py`
- Modify: `src/tests/test_recommendation_surfaces_golden.py`

**Steps:**

1. Add a manifest bucket for `harnesses` or preserve them as `load` entries with `entity_type: harness`; choose one contract and test it.
2. Recommended default: catalog-only `manifest["harnesses"]`, mirroring current MCP semantics. A harness is not loaded into Claude context directly.
3. Make scan rendering show `-- Harnesses (N) --` with install/setup caution.
4. Ensure free-text recommender and scan resolver still share the same ranker for graph-based harness hits.
5. Add tests for empty/non-empty harness recommendations and mixed skill/agent/MCP/harness output.

Verification:

```powershell
python -m pytest src\tests\test_resolve_skills.py src\tests\test_scan_repo.py src\tests\test_recommendation_surfaces_golden.py -q
python -m ruff check src\ctx\core\resolve\resolve_skills.py src\scan_repo.py src\tests\test_resolve_skills.py src\tests\test_scan_repo.py src\tests\test_recommendation_surfaces_golden.py
python -m mypy src\ctx\core\resolve\resolve_skills.py src\scan_repo.py src\tests\test_resolve_skills.py src\tests\test_scan_repo.py src\tests\test_recommendation_surfaces_golden.py
```

Stop for review before Phase 5.

## Phase 5: Harness Catalog CLI And Seed Entries

**Files:**
- Create: `src/harness_add.py`
- Modify: `pyproject.toml`
- Create: `src/tests/test_harness_add.py`
- Modify: `docs/knowledge-graph.md`
- Modify: `docs/index.md`

**Steps:**

1. Add `ctx-harness-add` with:
   - `--from-json`
   - `--from-jsonl`
   - `--from-stdin`
   - `--wiki PATH`
   - `--dry-run`
   - `--skip-existing`
2. Accept records with:
   - `slug`
   - `name`
   - `description`
   - `homepage`
   - `license`
   - `tags`
   - `setup_commands`
   - `capabilities`
   - `related_skills`
   - `related_mcp_servers`
   - `execution_policy`
3. Reject unsafe slugs, YAML injection, missing homepage, and non-list setup command fields.
4. Seed a local example for `text-to-cad` only if explicitly approved. If approved, use metadata only and link to upstream; do not vendor its code.

Example record:

```json
{
  "slug": "text-to-cad",
  "name": "Text to CAD",
  "description": "Open-source local harness for agent-driven CAD and URDF generation.",
  "homepage": "https://github.com/earthtojake/text-to-cad",
  "license": "MIT",
  "tags": ["cad", "urdf", "geometry", "robotics", "viewer", "validation"],
  "setup_commands": [
    "git clone https://github.com/earthtojake/text-to-cad.git",
    "python3.11 -m venv .venv",
    "./.venv/bin/pip install -r requirements-cad.txt",
    "cd viewer && npm install"
  ],
  "capabilities": ["step-export", "stl-export", "dxf-export", "glb-viewer", "urdf"],
  "related_skills": ["cad", "urdf"],
  "related_mcp_servers": [],
  "execution_policy": "user-confirmed"
}
```

Verification:

```powershell
python -m pytest src\tests\test_harness_add.py -q
python -m ruff check src\harness_add.py src\tests\test_harness_add.py
python -m mypy src\harness_add.py src\tests\test_harness_add.py
```

Stop for review before Phase 6.

## Phase 6: User-Facing Entity Onboarding Documentation

**Files:**
- Create: `docs/entity-onboarding.md`
- Modify: `docs/index.md`
- Modify: `docs/knowledge-graph.md`
- Modify: `docs/marketplace-registry.md`
- Modify: `docs/reports/ctx-million-dollar-review-2026-04-27.md`

**Required documentation sections:**

1. Add a skill:
   - Required file layout.
   - Minimal `SKILL.md` frontmatter.
   - Install/mirror/graphify commands.
   - Example with `cad` skill.
2. Add an agent:
   - Required `~/.claude/agents/<slug>.md` layout.
   - Mirror/install commands.
   - Example with `code-reviewer`.
3. Add an MCP server:
   - JSON/JSONL record shape.
   - `ctx-mcp-add` commands.
   - `ctx-mcp-install` confirmation step.
   - Example with a fake local MCP record that does not execute `npx`.
4. Add a harness:
   - JSON/JSONL record shape.
   - `ctx-harness-add` commands.
   - Graph rebuild and recommendation check.
   - Example with `text-to-cad` metadata only.
5. Verify recommendation parity:
   - `ctx-scan-repo --recommend`
   - Python API `ctx.recommend_bundle(...)`
   - generic harness `ctx__recommend_bundle`
   - dashboard/wiki search
6. Safety policy:
   - Cataloging never runs setup commands.
   - Install/execute paths require explicit user confirmation.
   - External repo code is not copied unless license and vendoring are explicitly approved.

Verification:

```powershell
python -m pytest src\tests\test_package_scaffold.py src\tests\test_public_api.py -q
python -m ruff check docs
python -m mypy src
```

Stop for review before Phase 7.

## Phase 7: Release Verification

Run the release gates after all approved phases:

```powershell
python scripts\clean_host_contract.py --fast
python -m ruff check .
python -m mypy src
python -m compileall -q src hooks scripts
git diff --check
python -m pytest -q
python -m build --outdir <temp>
python -m twine check <temp>\*
```

Only after those pass:

```powershell
git switch main
git merge --ff-only codex/crash-consistency-and-harness-plan
git tag -a v0.7.0 -m "v0.7.0"
git push origin main
git push origin v0.7.0
```

## Open Approval Questions

1. Should `harness` recommendations be catalog-only in v0.7.0, or should they have an install command in the first release?
2. Should `text-to-cad` be seeded as a harness entity immediately, or should `ctx-harness-add` ship first and accept user-provided records later?
3. Should `obsidian-second-brain` be cataloged as a skill recommendation seed now, or only referenced in docs as an onboarding pattern?
4. Should `claude-mem` be cataloged as an external plugin/memory system despite current first-class entity types not including `plugin`, or left as architecture reference until plugin recommendation support is formalized?
