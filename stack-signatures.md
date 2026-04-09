# Stack Signatures Reference

> Maps file patterns and config markers to stack identifiers.
> The scanner uses this to classify what a repo contains.
> Organized by detection category. Each entry: pattern -> stack identifier + confidence.

## Table of Contents
1. [Languages](#languages)
2. [Web Frameworks](#web-frameworks)
3. [AI/ML Frameworks](#aiml-frameworks)
4. [Infrastructure](#infrastructure)
5. [Data & Storage](#data--storage)
6. [Testing](#testing)
7. [Build & Package](#build--package)
8. [Documentation](#documentation)
9. [AI/Agent Tooling](#aiagent-tooling)

---

## Languages

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `*.py` + `pyproject.toml` | python | 1.0 | Check `python_requires` for version |
| `*.py` + `requirements.txt` | python | 0.95 | Older pattern, still common |
| `*.py` + `Pipfile` | python | 0.95 | |
| `*.py` + `poetry.lock` | python | 1.0 | |
| `*.ts` + `tsconfig.json` | typescript | 1.0 | |
| `*.js` + `package.json` | javascript | 0.9 | Could be TS compiled |
| `*.rs` + `Cargo.toml` | rust | 1.0 | |
| `*.go` + `go.mod` | go | 1.0 | |
| `*.java` + `pom.xml` | java | 1.0 | Maven |
| `*.java` + `build.gradle` | java | 1.0 | Gradle |
| `*.kt` + `build.gradle.kts` | kotlin | 1.0 | |
| `*.rb` + `Gemfile` | ruby | 1.0 | |
| `*.swift` + `Package.swift` | swift | 1.0 | |
| `*.cs` + `*.csproj` | csharp | 1.0 | |
| `*.php` + `composer.json` | php | 1.0 | |

## Web Frameworks

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `next.config.*` | nextjs | 1.0 | Check for app/ vs pages/ |
| `nuxt.config.*` | nuxt | 1.0 | |
| `angular.json` | angular | 1.0 | |
| `svelte.config.*` | svelte | 1.0 | |
| `vite.config.*` + react in deps | react | 0.95 | Confirm via package.json |
| `package.json` has `"react"` | react | 0.9 | Check version for 18 vs 19 |
| `package.json` has `"vue"` | vue | 0.9 | |
| `package.json` has `"express"` | express | 0.95 | |
| `package.json` has `"fastify"` | fastify | 0.95 | |
| pyproject/req has `fastapi` | fastapi | 0.99 | |
| pyproject/req has `django` | django | 0.99 | Check for DRF too |
| pyproject/req has `flask` | flask | 0.95 | |
| `Gemfile` has `rails` | rails | 1.0 | |
| `go.mod` has `gin-gonic` | gin | 0.95 | |
| `Cargo.toml` has `actix-web` | actix | 0.95 | |
| `Cargo.toml` has `axum` | axum | 0.95 | |

## AI/ML Frameworks

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| deps has `torch` or `pytorch` | pytorch | 0.95 | |
| deps has `tensorflow` | tensorflow | 0.95 | |
| deps has `transformers` | huggingface | 0.9 | |
| deps has `langchain` | langchain | 0.95 | Check core vs community |
| deps has `llama-index` | llamaindex | 0.95 | |
| deps has `crewai` | crewai | 0.95 | |
| deps has `autogen` | autogen | 0.95 | |
| deps has `semantic-kernel` | semantic-kernel | 0.95 | |
| deps has `openai` | openai-sdk | 0.8 | Could be indirect |
| deps has `anthropic` | anthropic-sdk | 0.8 | |
| deps has `dspy` | dspy | 0.95 | |
| `*.ipynb` files present | jupyter | 0.85 | |

## Infrastructure

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `Dockerfile` | docker | 1.0 | |
| `docker-compose.yml` | docker-compose | 1.0 | |
| `.github/workflows/*.yml` | github-actions | 1.0 | |
| `.gitlab-ci.yml` | gitlab-ci | 1.0 | |
| `Jenkinsfile` | jenkins | 1.0 | |
| `.circleci/config.yml` | circleci | 1.0 | |
| `*.tf` files | terraform | 1.0 | |
| `pulumi.*` or `Pulumi.yaml` | pulumi | 1.0 | |
| `cdk.json` | aws-cdk | 1.0 | |
| `template.yaml` (SAM) | aws-sam | 0.9 | Disambiguate from other templates |
| `serverless.yml` | serverless | 1.0 | |
| `k8s/` or `kubernetes/` dir | kubernetes | 0.95 | |
| `helm/` or `Chart.yaml` | helm | 1.0 | |
| `kustomization.yaml` | kustomize | 1.0 | |
| `ansible/` or `playbook.yml` | ansible | 0.9 | |
| `fly.toml` | fly-io | 1.0 | |
| `vercel.json` | vercel | 1.0 | |
| `netlify.toml` | netlify | 1.0 | |
| `render.yaml` | render | 1.0 | |
| `railway.json` | railway | 1.0 | |

## Data & Storage

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `alembic/` or `alembic.ini` | sqlalchemy | 0.95 | |
| `prisma/schema.prisma` | prisma | 1.0 | |
| deps has `typeorm` | typeorm | 0.95 | |
| deps has `drizzle-orm` | drizzle | 0.95 | |
| deps has `sequelize` | sequelize | 0.95 | |
| `migrations/` + Django | django-orm | 0.9 | |
| deps has `redis` or `ioredis` | redis | 0.85 | |
| deps has `kafka` or `confluent-kafka` | kafka | 0.9 | |
| deps has `celery` | celery | 0.95 | |
| `dags/` directory | airflow | 0.9 | |
| `dbt_project.yml` | dbt | 1.0 | |
| `*.sql` migration files | sql | 0.7 | Generic |

## Testing

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `pytest.ini` or `conftest.py` | pytest | 1.0 | |
| `jest.config.*` | jest | 1.0 | |
| `vitest.config.*` | vitest | 1.0 | |
| `cypress.config.*` or `cypress/` | cypress | 1.0 | |
| `playwright.config.*` | playwright | 1.0 | |
| `.mocharc.*` | mocha | 1.0 | |

## Build & Package

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `webpack.config.*` | webpack | 1.0 | |
| `vite.config.*` | vite | 1.0 | |
| `esbuild.*` in scripts | esbuild | 0.8 | |
| `turbo.json` | turborepo | 1.0 | |
| `nx.json` | nx | 1.0 | |
| `lerna.json` | lerna | 1.0 | |
| `pnpm-workspace.yaml` | pnpm-workspace | 1.0 | |
| `yarn.lock` + `workspaces` in pkg.json | yarn-workspace | 0.95 | |

## Documentation

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `mkdocs.yml` | mkdocs | 1.0 | |
| `docusaurus.config.*` | docusaurus | 1.0 | |
| `conf.py` + `index.rst` | sphinx | 0.95 | |
| `.vitepress/` | vitepress | 1.0 | |
| `openapi.yaml` or `swagger.yaml` | openapi | 0.95 | |
| `*.graphql` or `schema.graphql` | graphql | 0.9 | |

## AI/Agent Tooling

| Pattern | Stack ID | Confidence | Notes |
|---------|----------|------------|-------|
| `mcp.json` or `.mcp/` | mcp | 1.0 | |
| `CLAUDE.md` | claude-code | 0.95 | |
| `.cursorrules` | cursor | 0.9 | |
| `.windsurfrules` | windsurf | 0.9 | |
| `prompts/` directory | prompt-management | 0.7 | |
| `.env` with `*_API_KEY` | api-keys | 0.6 | Names only, never values |
