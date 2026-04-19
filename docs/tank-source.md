# Tank registry source

`skill_add` can ingest skills directly from the [Tank registry](https://tankpkg.dev) — a security-first package manager for AI agent skills with SHA-512 integrity verification and a 6-stage static scanner.

When a skill is pulled via Tank, ctx records the scan verdict, audit score, integrity hash, and publish date in the wiki entity page frontmatter. The quality layer (`skill_health`, `skill_quality`) can then use that provenance for rot detection and trust ranking — which it cannot do for skills dropped in manually.

## Install

```bash
pip install "claude-ctx[tank]"
```

This pulls [`tank-sdk`](https://pypi.org/project/tank-sdk/) (the pure-Python HTTP client, v0.14.0+). For faster integrity verification on downloads, also install the native extra on `tank-sdk` itself once it ships:

```bash
pip install "tank-sdk[native]"
```

> The `[native]` extra pulls `tank-core` which is published from the same monorepo release tag. If you see `tank-core` unavailable on PyPI, the macos-13 runner queue is still building the darwin-x86_64 wheel — the pure-Python install always works.

## Use

Pull the latest version of a skill:

```bash
python src/skill_add.py --tank @tank/nextjs
```

Pin a specific version:

```bash
python src/skill_add.py --tank @tank/nextjs@1.2.0
```

The skill is downloaded into a private temp directory, verified for integrity, installed under `~/.claude/skills/<slug>/`, and ingested into the wiki the same way local skills are.

## Authentication

For private or org-scoped skills, log in with the Tank CLI first:

```bash
tank login
```

ctx reads `~/.tank/config.json` (and `$TANK_TOKEN` as a fallback). You can also override per-call:

```bash
python src/skill_add.py --tank @myorg/internal --tank-token $TOKEN
```

## Self-hosted registries

```bash
python src/skill_add.py \
  --tank @internal/skill \
  --tank-registry https://tank.example.com
```

Or set the `TANK_REGISTRY_URL` env var.

## Name sanitization

Tank registry names contain `@` and `/` (e.g. `@tank/nextjs`). These are not valid characters in ctx's on-disk skill directory names (`SAFE_NAME_RE` = `^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,127}$`). The module maps them to a safe slug:

| Tank reference    | ctx slug         |
| ----------------- | ---------------- |
| `@tank/nextjs`    | `tank-nextjs`    |
| `@myorg/my.skill` | `myorg-my.skill` |
| `nextjs`          | `nextjs`         |

The full Tank name is preserved in the `tank_name` frontmatter field for exact provenance.

## Frontmatter

A Tank-sourced entity page gets additional fields:

```yaml
---
title: tank-nextjs
source: tank
tank_name: "@tank/nextjs"
tank_version: 1.2.0
tank_integrity: sha512-abcdef==
tank_scan_verdict: pass
tank_audit_score: 9.7
tank_audit_status: pass
tank_published_at: "2026-03-12T08:22:41Z"
---
```

These are additive — nothing in the existing entity page structure changes.

## Failure modes

`--tank` resolves to one of:

| Condition                                                | Error class                      | Exit |
| -------------------------------------------------------- | -------------------------------- | ---- |
| Invalid reference (empty, malformed)                     | `TankFetchError`                 | 1    |
| Skill or version not found                               | `tankpkg.TankNotFoundError`      | 1    |
| Auth required but no token                               | `tankpkg.TankAuthError`          | 1    |
| Network transport failure (after retries)                | `tankpkg.TankNetworkError`       | 1    |
| SHA-512 integrity mismatch on download                   | `tankpkg.TankIntegrityError`     | 1    |
| Intake gate rejects (structural/similarity/connectivity) | `intake_pipeline.IntakeRejected` | 1    |

The intake gate runs the same way for local and Tank-sourced skills, so Tank skills are subject to ctx's deduplication and quality checks too.
