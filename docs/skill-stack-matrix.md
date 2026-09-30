# Skill-Stack Matrix

The active legacy resolver mapping is
`src/ctx/core/resolve/stack_skill_map.py::STACK_SKILL_MAP`, pinned by
`src/tests/test_stack_skill_map.py`. `resolve_skills.py` applies that mapping,
its small `PRIORITY_BASE` table (default `5`), and four conflict pairs:
Flask/FastAPI, Flask/Django, Jest/Vitest, and Webpack/Vite.

The larger matrix below is a **design catalog**, not executable configuration.
In particular, the resolver has no `Required` field, does not evaluate the
companion rules below, and does not make the listed meta skills permanently
loaded. A row is current only when its stack key and skill list appear in
`STACK_SKILL_MAP`; add it there and add a behavior test before relying on it.

## Table of Contents
1. [Matrix Format](#matrix-format)
2. [Design catalog](#design-catalog)
3. [Proposed companion rules](#proposed-companion-rules)
4. [Conflict catalog](#conflict-catalog)

---

## Matrix Format

Each entry:
- **Stack IDs**: which detected stacks trigger this skill
- **Skill**: skill/entity slug in the graph/wiki catalog
- **Priority Base**: starting priority before signal boosts
- **Required**: must-load if stack detected, vs nice-to-have
- **Companions**: skills that should co-load
- **Conflicts**: skills that should not co-load

## Design catalog

### Document Creation Skills

| Skill | Stack IDs | Priority | Required | Path Pattern |
|-------|-----------|----------|----------|--------------|
| docx | (any -- triggered by user request) | 2 | no | graph/wiki or local skill path |
| pdf | (any -- triggered by user request) | 2 | no | graph/wiki or local skill path |
| pptx | (any -- triggered by user request) | 2 | no | graph/wiki or local skill path |
| xlsx | (any -- triggered by user request) | 2 | no | graph/wiki or local skill path |

> Note: document skills are demand-loaded, not stack-loaded. They activate on user
> request ("make a presentation") not on repo content. The router keeps them in a
> "standby" pool -- not loaded into context, but available for instant load.

### Frontend Skills

| Skill | Stack IDs | Priority | Required |
|-------|-----------|----------|----------|
| frontend-design | react, vue, angular, svelte, nextjs, nuxt, html, css | 8 | yes |
| react | react, nextjs | 7 | yes |
| vue | vue, nuxt | 7 | yes |
| angular | angular | 7 | yes |
| svelte | svelte | 7 | yes |
| tailwind | tailwindcss | 5 | no |
| css-modules | css-modules | 4 | no |

### Backend Skills

| Skill | Stack IDs | Priority | Required |
|-------|-----------|----------|----------|
| fastapi | fastapi | 8 | yes |
| django | django | 8 | yes |
| flask | flask | 7 | yes |
| express | express | 8 | yes |
| nestjs | nestjs | 8 | yes |
| rails | rails | 8 | yes |
| gin | gin | 7 | yes |
| actix | actix | 7 | yes |

### Data Skills

| Skill | Stack IDs | Priority | Required |
|-------|-----------|----------|----------|
| sqlalchemy | sqlalchemy, alembic | 6 | yes |
| prisma | prisma | 6 | yes |
| typeorm | typeorm | 6 | yes |
| drizzle | drizzle | 6 | yes |
| redis | redis | 4 | no |
| kafka | kafka | 5 | no |
| dbt | dbt | 6 | yes |

### Infrastructure Skills

| Skill | Stack IDs | Priority | Required |
|-------|-----------|----------|----------|
| docker | docker, docker-compose | 6 | yes |
| kubernetes | kubernetes, helm, kustomize | 6 | yes |
| terraform | terraform | 7 | yes |
| github-actions | github-actions | 5 | yes |
| gitlab-ci | gitlab-ci | 5 | yes |
| aws | aws-cdk, aws-sam | 7 | yes |
| vercel | vercel | 4 | no |

### AI/Agent Skills

| Skill | Stack IDs | Priority | Required |
|-------|-----------|----------|----------|
| langchain | langchain | 7 | yes |
| llamaindex | llamaindex | 7 | yes |
| mcp-dev | mcp | 7 | yes |
| pytorch | pytorch | 6 | yes |
| huggingface | huggingface | 6 | yes |
| openai-sdk | openai-sdk | 5 | no |
| anthropic-sdk | anthropic-sdk | 5 | no |

### Quality Skills

| Skill | Stack IDs | Priority | Required |
|-------|-----------|----------|----------|
| pytest | pytest | 5 | yes |
| jest | jest, vitest | 5 | yes |
| cypress | cypress | 4 | no |
| playwright | playwright | 4 | no |
| eslint | eslint | 3 | no |
| ruff | ruff | 3 | no |

### Documentation Skills

| Skill | Stack IDs | Priority | Required |
|-------|-----------|----------|----------|
| openapi | openapi | 5 | yes |
| graphql | graphql | 5 | yes |
| mkdocs | mkdocs | 4 | no |
| docusaurus | docusaurus | 4 | no |

### Proposed meta skills

| Skill | Stack IDs | Priority | Required | Notes |
|-------|-----------|----------|----------|-------|
| skill-router | * | 99 | yes | This skill -- always loaded |
| file-reading | * | 50 | yes | Core capability |
| skill-creator | * | 10 | no | Standby pool |
| product-self-knowledge | * | 10 | no | Standby pool |

---

## Proposed companion rules

These relationships are not implemented by the legacy resolver. They record
possible future behavior only:

| Primary Skill | Companion | Condition |
|---------------|-----------|-----------|
| fastapi | sqlalchemy | DB migrations detected |
| fastapi | openapi | OpenAPI spec file exists |
| django | django-orm | (always with django) |
| react | tailwind | tailwind.config.* exists |
| docker | docker-compose | docker-compose.yml exists |
| kubernetes | helm | Chart.yaml exists |
| terraform | aws | provider "aws" in *.tf |
| langchain | openai-sdk | openai in deps |
| pytest | coverage | .coveragerc or coverage config exists |

## Conflict catalog

Only the first four pairs are implemented by `resolve_skills.py`. The package
manager and monorepo rows are design notes, not current conflict behavior:

| Skill A | Skill B | Resolution |
|---------|---------|------------|
| flask | fastapi | Highest resolver priority wins; alphabetical tie-break |
| flask | django | Highest resolver priority wins; alphabetical tie-break |
| jest | vitest | Highest resolver priority wins; alphabetical tie-break |
| webpack | vite | Highest resolver priority wins; alphabetical tie-break |
| npm | yarn | Check lock file |
| npm | pnpm | Check lock file |
| yarn | pnpm | Check lock file |
| react | vue | Both can coexist in monorepo |
| sqlalchemy | prisma | Both can coexist if different services |

> Proposed behavior only: a future package-aware resolver could retain otherwise
> conflicting skills for different monorepo packages. The current resolver is
> not package-aware and always applies the four implemented conflict pairs.
