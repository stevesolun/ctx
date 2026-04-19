# Changelog

All notable changes to the `ctx` project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

[0.5.0-rc1]: https://github.com/stevesolun/ctx/releases/tag/v0.5.0-rc1
