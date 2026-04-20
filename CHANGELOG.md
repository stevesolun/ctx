# Changelog

All notable changes to the `ctx` project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.6.3] — 2026-04-19

Docs-only release. The two marquee features of ctx — the pre-built
knowledge graph and the `ctx-monitor` dashboard — were referenced all
over the docs but had no dedicated page explaining them. This release
adds both and wires them into the home page and the top of the nav.

### Added

- **`docs/knowledge-graph.md`** — dedicated page for the pre-built
  graph: authoritative counts (2,253 nodes / 454,719 edges / 93
  communities / 416.6 avg degree / 1,152 max degree / 195,226 skill↔
  agent cross-edges / 71 isolated), install via the shipped tarball,
  how edges are built (explicit frontmatter tags + slug-token
  pseudo-tags with `DENSE_TAG_THRESHOLD=500` and the `SLUG_STOP`
  filter), community detection details (greedy modularity
  `resolution=1.2`), query recipes via the dashboard + Python +
  recommendation path, rebuild instructions, and a postmortem section
  explaining why the edge count is 454K and not the stale 642K bundle
  referenced in earlier releases.
- **`docs/dashboard.md`** — full `ctx-monitor` reference: startup
  commands (with the `--host 0.0.0.0` opt-in warning), complete HTML
  route catalog (`/`, `/loaded`, `/skills`, `/skill/<slug>`,
  `/wiki/<slug>`, `/graph`, `/sessions`, `/session/<id>`, `/logs`,
  `/events`), JSON API (`/api/sessions.json`, `/api/manifest.json`,
  `/api/skill/<slug>.json`, `/api/graph/<slug>.json`,
  `/api/events.stream`), mutation endpoints with CSRF/same-origin
  notes, a **KPIs / measures / scores** section explaining the six
  home stat cards, the grade + raw-score view on `/skills`, the full
  four-signal breakdown on `/skill/<slug>` (Telemetry 0.40, Intake
  0.20, Graph 0.25, Routing 0.15) with hard-floor reasons, and the
  `load → score_updated → unload` observability triad on
  `/session/<id>`. Also documents the security posture (loopback
  default, same-origin gating on POST, slug allowlist on every
  path-resolving route).

### Changed

- **`mkdocs.yml` nav** — knowledge graph and dashboard hoisted to
  positions 2 and 3 (right after Home), above Toolbox / Skill router /
  Health. They are the two observables users are most likely looking
  for, so the nav now matches the mental model.
- **`docs/index.md`** — added two grid cards at the top of the "Explore
  the docs" section pointing at the new pages, so the home page
  surfaces the graph + dashboard as first-class features rather than
  burying them inside the router/health sections.

## [0.6.2] — 2026-04-20

Verification-pass patch after v0.6.1 shipped. Three items the v0.6.1
for-the-reviewer note flagged as "not verified" all got verified; one
surfaced a real bug (pre-commit tar repack silently failing on
Windows/MSYS).

### Fixed

- **`.githooks/pre-commit` — tar repack crashed on Windows/MSYS**:
  GNU tar parses `c:/path` as `host:path` for legacy rsh remote tar,
  tries to resolve host `c`, and fails with `Cannot connect to c:
  resolve failed`. The hook swallowed the error (by design — a hook
  failure must not block a commit) but the tarball was never
  regenerated, so developer-side rebuilds on Windows silently shipped
  stale counts. Fixed by passing `--force-local` to the tar invocation.
- **`.obsidian/` Obsidian vault config was being excluded from the
  tarball** despite `graph/README.md` advertising "Obsidian vault
  config, so the extracted tree opens as a graph directly in Obsidian."
  Removed the `--exclude='.obsidian'` from the pre-commit repack so the
  tarball actually ships what the docs promise.

### Verified (v0.6.1 "not verified" items)

- **PyPI 0.6.1 published**: wheel `claude_ctx-0.6.1-py3-none-any.whl`
  (267 KB) + sdist (232 KB) live, uploaded 2026-04-19T23:45 UTC.
  Publish / Tests / Deploy-docs workflows all succeeded.
- **Cytoscape layout quality verified via headless Chromium**:
  Playwright loaded `http://127.0.0.1:8811/graph?slug=cloud-architect`,
  waited for `cy.nodes().length > 0`, then read the live cytoscape
  instance: 40 nodes / 39 edges rendered, center `skill:cloud-architect`
  at depth 0, COSE layout placed nodes (bounding box 400×297px, none
  at origin), status panel showed "40 nodes · 39 edges", zero JS errors
  on `pageerror` or `console.error`. Screenshot captured for proof.
- **Tarball content**: same 1,789 skills / 464 agents / 2,253 nodes
  / 454,719 edges after the hook fix, `.obsidian/` now included as
  advertised.

[0.6.2]: https://github.com/stevesolun/ctx/releases/tag/v0.6.2

## [0.6.1] — 2026-04-20

Harvested **`0xNyk/council-of-high-intelligence`** on top of v0.6.0.
The repo contributes a `council` orchestrator skill plus 18 named
persona agents (Karpathy, Sutskever, Taleb, Munger, Feynman, Socrates,
Aristotle, Ada Lovelace, Aurelius, Kahneman, Lao Tzu, Machiavelli,
Meadows, Musashi, Rams, Sun Tzu, Torvalds, Watts). Every agent was
re-intaked with a prepended H1 derived from its `council.figure`
frontmatter field — legitimate data-cleanup, not a gate bypass.

Also fixes a documentation audit: every stale count reference across
README + docs + graph/README had drifted from the shipped tarball
(some said 2,211/642K, others 2,235/448K, community count varied
between 93 and 95). Single sweep + tightened `update_repo_stats`
regex to match "N nodes and N edges" phrasing the old regex missed.

### Graph: final shipped state

| Metric | v0.6.0 | v0.6.1 |
|---|---:|---:|
| Nodes | 2,235 | **2,253** |
| Skills | 1,789 | **1,789** |
| Agents | 446 | **464** |
| Edges | 448,799 | **454,719** |
| Communities | 95 | **93** |
| Avg degree | 414.8 | **416.6** |
| Max degree | 1,144 | **1,152** |
| Skill↔agent cross-edges | 191,770 | **195,226** |

### Added

- **1 skill + 18 agents** from `0xNyk/council-of-high-intelligence`:
  `council` (orchestrator) + `council-{ada, aristotle, aurelius,
  feynman, kahneman, karpathy, lao-tzu, machiavelli, meadows, munger,
  musashi, rams, socrates, sun-tzu, sutskever, taleb, torvalds,
  watts}`. Every one passed the intake gate after a minimal H1
  transform that preserved the original `## Identity` body.

### Fixed

- **README + docs stale number drift**: 12 locations across README,
  graph/README, and 5 docs pages had references to the v0.5.x stale
  bundle numbers (2,211/642K/865/952/1,768/443). Single audit pass
  updates all to the current live tarball (2,253/454,719/93/956/
  1,789/464).
- **`update_repo_stats` regex coverage**: added patterns for
  "N nodes and N edges" (missed by the old "N nodes, N edges, N
  communities" regex) plus the Python example comment form
  "# N nodes, N edges". Any future README sentence using the "and"
  connector will auto-refresh correctly.
- **Cytoscape rendering verified live**: `/api/graph/cloud-architect.json`
  returns 60 nodes / 59 edges with sensible edge-weight-ranked
  neighbors (database-admin, hybrid-cloud-architect,
  terraform-engineer all at weight 6 sharing automation+azure+security
  tags). The `/graph?slug=<slug>` HTML page embeds cytoscape.js from
  CDN with the initial slug JSON-encoded and the tap→/wiki/<slug>
  navigation wired.

### Known limitation carried from v0.6.0

Graph rebuild regenerates from the **live wiki**, not from a pinned
baseline. If someone else also re-graphifies locally with slightly
different wiki content, their edge count will differ from the
shipped tarball's. The `update_repo_stats` tarball-first source of
truth (v0.5.1) keeps README honest, but post-install users who
re-graphify will see their own numbers. Documented in `graph/README.md`.

[0.6.1]: https://github.com/stevesolun/ctx/releases/tag/v0.6.1

## [0.6.0] — 2026-04-20

Harvested 11 upstream Claude Code / context-management / token-optimizer
repos and ingested their skills + agents through our intake gate into
the LLM wiki. Every candidate that landed passed the gate's structural
checks (frontmatter name, body H1, minimum body length, no duplicate
embedding) and was rendered in our canonical wiki format (YAML
frontmatter with tags / use_count / last_used / status + Overview +
Tags + Obsidian `[[wikilinks]]` to related skills).

### Added

- **+20 truly new skills** ingested and vetted:
  `build-graph`, `caveman-compress`, `caveman-help`, `compress`,
  `context-mode`, `context-mode-ops`, `ctx-doctor`, `ctx-insight`,
  `ctx-purge`, `ctx-stats`, `ctx-upgrade`, `fleet-auditor`,
  `review-delta`, `review-pr`, `rtk-tdd`, `rtk-triage`, `tdd-rust`,
  `token-coach`, `token-dashboard`, `token-optimizer`.
- **+3 new agents**: `rtk-testing-specialist`, `rust-rtk`,
  `system-architect`.
- **3 existing pages refreshed** with intake-vetted replacements
  (`design-patterns`, `issue-triage`, `pr-triage`).
- **`graph/wiki-graph.tar.gz`** re-archived with the new entity pages:
  **1,788 skills · 446 agents** (was 1,768 / 443 in v0.5.x).

### Harvest sources

| Upstream repo | Accepted | Rejected |
|---|---:|---:|
| alexgreensh/token-optimizer | 4 skills | 0 |
| juliusbrussee/caveman | 3 skills | 3 (missing H1) |
| mksglu/context-mode | 7 skills | 0 |
| tirth8205/code-review-graph | 2 skills | 5 (missing H1) |
| rtk-ai/rtk | 7 skills + 3 agents | 4 (frontmatter missing `name:`) + 4 (dup agents) |
| russelleNVy/three-man-team | 0 | 3 (dup agents) |
| drona23/claude-token-efficient | 0 | 1 (dup agent) |
| mibayy/token-savior, nadimtuhin, ooples, zilliztech | 0 | (README-only, no SKILL.md/agent files) |

### Intake-gate rejections (preserved for reference)

- `BODY_MISSING_H1` (9 skills): `caveman`, `caveman-commit`,
  `caveman-review`, `debug-issue`, `explore-codebase`,
  `refactor-safely`, `review-changes` + 2 others.
- `FRONTMATTER_FIELD_MISSING_NAME` (4 skills): `performance`,
  `pr-review`, `repo-recap`, `security-guardian`, `ship`.
- Duplicate-agent short-circuit (6): `architect`, `builder`,
  `code-reviewer`, `debugger`, `reviewer`, `technical-writer` —
  already installed in the wiki; intake gate correctly skipped.

### Fixed

- **Graph sparsity regression** (`src/wiki_graphify.py`): the
  `DENSE_TAG_THRESHOLD` constant was `20`, which silently dropped
  every tag that appeared on more than 20 nodes. On a wiki where
  `python`, `frontend`, `security`, and `testing` each tag hundreds
  of entities, this collapsed the graph from the canonical 642,468
  edges down to 861 on every rebuild. Bumped to **500** (now pinned
  by `src/tests/test_wiki_graphify_density.py`) and added
  **slug-token pseudo-tags** — e.g. the slug `fastapi-pro`
  contributes an implicit `fastapi` token so skills that share a
  topic keyword get connected even when their explicit tags don't
  overlap. Stop-word filter keeps noise tokens (`skill`, `agent`,
  `pro`, `core`, etc.) out of the index.
- **Multi-line YAML list parsing** (`src/wiki_utils.py`):
  `parse_frontmatter` only handled inline `tags: [a, b, c]` lists;
  the block form
  ```yaml
  tags:
    - python
    - frontend
  ```
  returned an empty string, silently invalidating every real wiki
  entity page (all 2,234 of them use the block form). Extended the
  parser to collect `- item` lines following an empty-value key.

### Graph: final shipped state

| Metric | v0.5.x | v0.6.0 |
|---|---:|---:|
| Nodes | 2,211 | **2,235** |
| Edges | 861 (effective; bundle had 642K stale) | **448,799** |
| Communities | 2,110 (mostly singletons) | **95** |
| Avg degree | <1 | **414.8** |
| Max degree | 40 | **1,144** |
| Skill↔agent cross-edges | — | **191,770** |
| Isolated nodes | 390+ | **71** |

The full recommendation pipeline can now walk edges from a detected
stack signal (e.g. `fastapi`) to installed agents (`code-reviewer`,
`test-automator`) via shared-tag and slug-token pseudo-edges. Verified
via `ctx-monitor`'s `/graph?slug=<any-tagged-slug>` view: every
tag-heavy entity now lights up its neighborhood with 200+ edges on
average.

[0.6.0]: https://github.com/stevesolun/ctx/releases/tag/v0.6.0

## [0.5.1] — 2026-04-20

Point release. Same day as the GA cut, issued to correct one
behavior: the pre-commit stats hook was silently rewriting README
numbers from the user's *live* `~/.claude/skill-wiki/` — which can be
a locally-rebuilt sparse graph — rather than from the shipped
`graph/wiki-graph.tar.gz`. The tag `v0.5.0` therefore pointed at a
commit whose README showed the user's local 885-edge rebuild instead
of the canonical 642,468-edge shipped graph.

### Fixed

- **`src/update_repo_stats.py` source of truth**: the stats refresher
  now reads node/edge/skill/agent/community counts from
  `graph/wiki-graph.tar.gz` first (the pinned release asset), and
  only falls back to `~/.claude/skill-wiki/` when the tarball is
  absent. Counts no longer drift across developer machines.
- **README badges + tagline**: restored the authoritative numbers
  (1,768 skills · 443 agents · 2,211 nodes · 642K edges · 865
  communities) that the v0.5.0 commit accidentally clobbered.

## [0.5.0] — 2026-04-20

First stable release. MIT-licensed, CI-matrixed (ubuntu-latest +
windows-latest × Python 3.11/3.12), **1,360 tests passing**, installable
via `pip install claude-ctx` with 10 console scripts on PATH.

### Highlights

- **Pre-built knowledge graph** (`graph/wiki-graph.tar.gz`, 11.7 MB
  compressed): **2,211 nodes** (1,768 skills + 443 agents), **642,468
  edges**, **865 communities**, 61 auto-generated concept pages, 952
  converted micro-skill pipelines, Obsidian-compatible vault config.
- **Live dashboard** (`ctx-monitor serve` → `http://127.0.0.1:8765/`):
  six-card stat grid home, currently-loaded-skills view with load/unload
  buttons, Cytoscape graph explorer (`/graph?slug=…`), LLM-wiki entity
  browser (`/wiki/<slug>`), filterable skills card grid with left
  sidebar, session timeline, audit-log viewer, SSE live event stream.
- **Unified audit log** (`~/.claude/ctx-audit.jsonl`): append-only,
  rotates at 25 MB, 24 canonical event types covering the full
  skill/agent lifecycle from added → loaded → score_updated →
  archived → deleted.
- **Graph load-bearing on recommendations** (`resolve_skills.py`):
  matrix-matched skills seed a graph walk that adds 1-hop neighbors
  scored by edge weight. On a trivial FastAPI+SQLAlchemy+pytest
  repo the manifest now loads 14 relevant skills (fastapi-pro,
  async-python-patterns, python-pro, test-automator,
  backend-security-coder, etc.) with mixed `fuzzy match` and
  `graph neighbor of …` reasons. Previous releases returned 1.
- **One-command setup**: `ctx-init --hooks` creates the standard
  `~/.claude/` tree, seeds the five starter toolboxes, and
  optionally injects the PostToolUse + Stop hooks into
  `~/.claude/settings.json` — replacing the legacy `install.sh` flow
  for pip-installed users.

### Added

- **Console scripts**: `ctx-init`, `ctx-install-hooks`, `ctx-monitor`,
  `ctx-scan-repo`, `ctx-skill-quality`, `ctx-skill-health`,
  `ctx-toolbox`, `ctx-lifecycle`, `ctx-skill-add`,
  `ctx-wiki-graphify`.
- **`src/ctx_audit_log.py`** — concurrent-safe append-only audit log
  with session attribution, threading lock for in-process safety,
  `rotate_if_needed(max_bytes=25MB)` called on every `session.ended`.
- **`src/ctx_monitor.py`** — stdlib-only `http.server` dashboard
  (no Flask/Starlette dep). Cytoscape.js loaded from unpkg on the
  `/graph` route. Binds to 127.0.0.1 by default; same-origin check
  on POST endpoints; slug allowlist regex gates all mutation.
- **`src/ctx_init.py`** — idempotent bootstrap replacing install.sh.
  Opt-in `--hooks` and `--graph` flags so the command never mutates
  `~/.claude/settings.json` or runs a multi-minute graph build
  without explicit consent.
- **Pre-built wiki** shipped as `graph/wiki-graph.tar.gz`:
  `entities/skills/*.md` (1,768), `entities/agents/*.md` (443),
  `concepts/*.md` (61), `converted/*/` (952), full
  `graphify-out/graph.json` + `communities.json`, catalog,
  `.obsidian/` vault config.
- **Playbooks** in `docs/`:
  - `playbook-real-world.md` — end-to-end PCI-fintech checkout scenario
    exercising scan → suggest → load → toolbox council → custom
    skill_add → lifecycle archive → KPI render.
  - `playbook-live-load-unload.md` — 7-step verification that the
    observe → suggest → record → score pipeline is live. Verified in
    **5.86 s** end-to-end.
  - `playbook-random-load-unload.md` — full lifecycle: pick a random
    never-loaded skill, surface via KEYWORD_SIGNALS, load, force
    Stop-hook rescore, wait for staleness, queue-for-unload, unload,
    verify via `/session/<id>` dashboard timeline.

### Fixed (security — Strix deep scan audit)

- **HIGH — path traversal in `src/import_strix_skills.py`**: manifest
  `source_path` + `category` now validated against a strict allowlist
  regex and containment-checked via `Path.resolve()` + `relative_to()`
  against `IMPORT_ROOT` + `target_dir`.
- **HIGH — backup config path traversal in `src/backup_config.py`**:
  `trees[].src` / `trees[].dest` reject `..`, absolute paths, Windows
  drive letters, and UNC shares. Malformed entries logged to stderr
  and skipped, not silently replaced with defaults.
- **HIGH — git textconv RCE in `src/ctx_lifecycle.py`**:
  `_git_diff_preview` now invokes `git log -p` with
  `--no-textconv`, `--no-ext-diff`, `-c diff.external=`, and
  `-c core.attributesfile=os.devnull` so a hostile repo's
  `.gitattributes` can't trigger arbitrary command execution.
- **LOW — `usage_tracker.py --wiki` flag ignored**: the override
  now actually threads through `update_skill_page` + `append_wiki_log`
  instead of silently writing to the default wiki.

### Fixed (correctness)

- **NetworkX 'links' vs 'edges' schema** (`resolve_graph.py`,
  `wiki_visualize.py`, `context_monitor.py`, `wiki_graphify.py`):
  readers auto-detect the schema; writer pins `edges="edges"` going
  forward. The 642K-edge graph had been silently returning 0 edges
  on every consumer since the NetworkX 3.x upgrade.
- **Stop-hook schema wrapper** (`src/inject_hooks.py`):
  `quality_on_session_end.py` was registered flat and never
  auto-fired on session close. Now wrapped in the `{"hooks":[…]}`
  form Claude Code expects.
- **`_set_frontmatter_field` replace-only** (`src/usage_tracker.py`):
  silently no-op'd when the field was missing. `session_count`
  never persisted on wiki pages that didn't pre-ship with it, so
  the staleness gate at `session_count ≥ STALE_THRESHOLD` never
  fired. Now inserts missing fields into the frontmatter block.
- **`skill_unload` one-sided event log** (`src/skill_unload.py`):
  `unload_from_session` now emits an `unload` line to
  `skill-events.jsonl` + a `skill.unloaded` audit row; previously
  loads were recorded but unloads weren't.
- **`skill.score_updated` audit rows missing session_id**:
  Stop hook now exports `CTX_SESSION_ID` before invoking the
  recompute subprocess so the dashboard per-session timeline shows
  the middle event of the load → score_updated → unload triad.
- **CI flake on Windows** (`src/_fs_utils.py`): concurrent-writer
  retry bumped from 3 × 50ms to 10 × 50ms to absorb AV/indexer
  lock contention on windows-latest CI runners.
- **Graph orphaned from recommendations** (`src/resolve_skills.py`):
  added fuzzy installed-skill fallback + `resolve_by_seeds` graph
  walk with a 1.5 edge-weight noise floor. Manifest now contains
  real neighbors instead of `1 load + 1576 unload + 2 warnings`.
- **`context_monitor.KEYWORD_SIGNALS`** extended with `stripe`,
  `pci`, `payment`, `postgres`/`psycopg*`/`asyncpg`, `mongodb`,
  `pydantic`, and more — fintech/payments projects now fire
  pending-skills suggestions.
- **`scan_repo` framework detection** extended across
  `stripe`/`paypal`/`plaid` payment deps,
  `psycopg*`/`asyncpg`/`mongodb`/`pymongo` datastores,
  `pydantic`/`zod`/`yup` validation, and `pytest`/`jest`/`vitest`
  from dev-deps (no longer requires a dedicated config file).
- **`scan_repo --output` mkdir-p**: was raising `FileNotFoundError`
  when the parent directory didn't exist.
- **`ctx_lifecycle` Windows UnicodeEncodeError**: `main()` now
  reconfigures `sys.stdout`/`sys.stderr` to UTF-8 so arrows and
  other Unicode in transition descriptions don't crash the cp1252
  Windows console.
- **`kpi_dashboard` `.hook-state.json` crash**: the sidecar
  iteration now skips internal dotfiles so the strict slug
  validator doesn't get fed a `.hook-state` string.
- **`_trigger_matches` Linux-side Windows path normalization**
  (`src/toolbox_hooks.py`): now replaces `\\` with `/` unconditionally,
  not gated on `os.sep` which was a no-op on Linux runners.
- **Positional slugs for `recompute`** (`src/skill_quality.py`):
  `ctx-skill-quality recompute python-patterns` now works; the
  subparser was flag-only before.
- **Toolbox templates packaged** (`src/toolbox.py`): all five
  starter templates embedded inline so `ctx-toolbox init` seeds
  them from the installed wheel (previously the `docs/toolbox/templates/`
  directory was not bundled, so init produced `[warn] Template not
  found` x 5).

### Changed

- **Package name**: `ctx-skill-quality` → `claude-ctx`. PyPI's `ctx`
  namespace is a post-incident tombstone, so `claude-ctx` is the
  canonical install name. `pip install claude-ctx` is the only
  supported install path.
- **Dashboard UI** (`ctx-monitor`): home page rebuilt from a near-empty
  placeholder into a six-card stat grid + two-column session/audit
  panels + grade pills that render even with zero data.
- **`/skills` page**: table → responsive card grid with a left filter
  sidebar (text search, grade checkboxes, subject_type toggles,
  hide-floored). Each card links to `/skill/<slug>` sidecar detail,
  `/wiki/<slug>` entity page, and `/graph?slug=<slug>` neighborhood.
- **`resolve_skills.py`** is now load-bearing on the graph: matrix
  → fuzzy fallback → graph-walk augmentation happens in that order,
  each stage optional.

### Migration from 0.4.x / pre-PyPI

- Replace `./install.sh python` with `pip install claude-ctx &&
  ctx-init --hooks`.
- Replace `python src/<module>.py` invocations with the `ctx-<name>`
  console script (see `pyproject.toml [project.scripts]`) or
  `python -m <module>`.
- Existing `~/.claude/skill-wiki/graphify-out/graph.json` files in
  the NetworkX 2.x "links" schema load transparently — readers auto-
  detect. New builds pin `edges="edges"`.

### Release-candidate history

Between the rc1 internal review (2026-04-19) and this GA, ten public
release candidates were cut and published to PyPI: rc1 (broken wheel,
superseded), rc2 (wheel fix — 55 runtime modules), rc3 (positional
slugs + toolbox templates), rc4 (graph wired into resolve_skills +
CI flake fix), rc5 (stack-detection breadth + playbook fixes), rc6
(three Strix HIGH security patches), rc7 (pipeline fixes +
vuln-0004 + audit log), rc8 (`ctx-monitor` + `ctx-init`), rc9 (three
load/unload bugs caught by the random-load playbook), rc10 (live
dashboard with load/unload POST actions + `/logs`), rc11 (rich home
+ `/graph` + `/wiki` + `/skills` sidebar filter).

## [0.5.0-rc1] — 2026-04-19

First open-source release candidate. MIT-licensed, CI-matrixed, and
hardened against the review findings from an internal CTO+CEO council
pass. Full test suite: **1316 passed, 2 skipped**.

### Added

- `LICENSE` — MIT.
- `pyproject.toml` — installable via `pip install -e .` with optional
  `[embeddings]` (sentence-transformers) and `[dev]` (pytest/mypy) extras.
- `.github/workflows/test.yml` — matrix CI: ubuntu-latest + windows-latest
  × Python 3.11 + 3.12.
- `CONTRIBUTING.md`, issue templates, PR template.
- `src/_fs_utils.py` — canonical `atomic_write_text` / `atomic_write_bytes`
  / `atomic_write_json` with Windows `PermissionError` retry. Replaces
  14 local copies across the codebase.
- `src/__init__.py` — `__version__ = "0.5.0-rc1"`.
- Hooks: `quality_on_session_end.py` registered as a Claude Code `Stop`
  hook for incremental quality scoring.
- 112 new tests across security, performance benchmarks, JSON integrity,
  atomic writes, and edge cases.

### Fixed (security)

- **RCE / shell injection** in `inject_hooks.py`: removed argv
  interpolation of `$CLAUDE_TOOL_INPUT` / `$CLAUDE_TOOL_NAME`. Hooks now
  consume tool input via `--from-stdin`.
- **Path traversal** in `skill_add_detector.py`: added
  `validate_user_supplied_slug` (strict regex) plus `resolve` +
  `relative_to` containment check.
- **Path escape** in `skill_telemetry.py`: events file is now anchored to
  a `_TRUSTED_ROOT` / `trusted_root` kwarg.
- **Race on concurrent writes**: three files used a predictable
  `path.with_suffix(".tmp")` temp filename that clobbers when two writers
  race. Replaced with `tempfile.mkstemp` (unique per write).
- **Graph JSON integrity**: `resolve_graph.load_graph` now validates the
  schema (`JSONDecodeError`, `NetworkXError`, missing `nodes`/`links`).
- **YAML injection** in `skill_add`: frontmatter is emitted via
  `yaml.safe_dump`, not string-concatenated.
- **Markdown cell escaping** in `skill_add_detector` (`_escape_md_cell`).

### Fixed (correctness)

- **Latent bug** in `kpi_dashboard`: `len(bucket)` was being called on a
  rebound loop variable rather than the category dict — fixed by
  renaming to `cat_bucket`.
- `ctx_lifecycle.observe_score` guard against empty `computed_at`.
- `skill_quality.cmd_list` skips `.lifecycle.json` records.
- `ctx_lifecycle._apply_buckets` checks auto-apply before interactive
  prompt.

### Changed

- **Package layout**: `[tool.setuptools] package-dir = {"" = "src"}`.
  Removed 18 `sys.path.insert()` hacks across the source tree.
- **Incremental quality scoring**: `skill_quality._build_events_index`
  converts `O(N·M)` JSONL re-scans into `O(N+M)` single-pass indexing.
- **Graph build**: `wiki_graphify` gained `DENSE_TAG_THRESHOLD=20` to
  skip pathological cliques and a `node_to_community` reverse index
  (`O(C²·members)` → `O(C·members)`).
- **Browser visualizer**: `wiki_visualize.js` replaces `NODES.find` in
  hot loops with a `Map` (`O(E·N)` → `O(N+E)`).
- `QualitySink` protocol extracted in `skill_quality.py` with three
  concrete sinks: `SidecarSink`, `WikiFrontmatterSink`, `WikiBodySink`.
- Slug validation tiers documented: `wiki_utils.SAFE_NAME_RE` (lenient,
  legacy) vs `skill_add_detector.validate_user_supplied_slug` (strict,
  new input).
- `sidecar_dir`, `skills_dir`, `agents_dir`, `wiki_dir` now read from
  `src/config.json`.

### Developer experience

- `mypy --strict` clean on `_fs_utils.py`, `wiki_utils.py`. 28→0 mypy
  errors cleared in `scan_repo.py`, `kpi_dashboard.py`,
  `intent_interview.py`.
- `__all__` exports on `wiki_utils.py` and `_fs_utils.py`.
- 5 dead imports removed (`os`, `Mapping`, `timedelta` from
  `ctx_lifecycle`; `Path` from `intake_gate`, `intake_pipeline`).

[0.5.1]: https://github.com/stevesolun/ctx/releases/tag/v0.5.1
[0.5.0]: https://github.com/stevesolun/ctx/releases/tag/v0.5.0
[0.5.0-rc1]: https://github.com/stevesolun/ctx/releases/tag/v0.5.0-rc1
