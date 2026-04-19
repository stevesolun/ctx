# Changelog

All notable changes to the `ctx` project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

[0.5.0]: https://github.com/stevesolun/ctx/releases/tag/v0.5.0
[0.5.0-rc1]: https://github.com/stevesolun/ctx/releases/tag/v0.5.0-rc1
