# Changelog

All notable changes to the `ctx` project will be documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

- No unreleased changes yet.

## [0.7.2] - 2026-05-04

### Added

- Added a persistent wiki ingest queue and worker so skill, agent, MCP,
  and harness wiki updates can be queued and replayed durably.
- Added a non-required pytest-xdist experiment workflow for measuring
  safe parallel test execution without changing the required CI gate.

### Changed

- Split PR CI from main CI: PRs now use a canonical Ubuntu coverage lane,
  while main keeps the broader OS/Python matrix.
- Added docs-only and graph-only PR fast paths guarded by the stable
  `CI required` aggregate check.
- Reused one built wheel across package-smoke OS jobs and cached
  Playwright browser setup for monitor security tests.
- Enabled active GitHub main protection that requires only the stable
  `CI required` check.
- Kept Hugging Face repo-card metadata out of the GitHub README while
  preserving valid metadata during Hugging Face sync.

### Fixed

- Made graph export, generated graph wikilinks, harness catalog writes,
  and harness install manifests safer against crash/truncation cases.
- Hardened the README harness stats updater so harness-aware counts stay
  aligned.

## [0.7.1] - 2026-05-02

### Fixed

- Ensured the shipped `graph/wiki-graph.tar.gz` omits all `.original`
  micro-skill backup files, not just newly generated `SKILL.md.original`
  files.
- Made the micro-skill line threshold strict, configurable through
  `skill_transformer.line_threshold`, and honored by both scan and
  single-file conversion modes.
- Made `ctx-monitor --host 0.0.0.0` read-only: non-loopback binds no
  longer expose the dashboard mutation token or accept load/unload POSTs.
- Hardened skill, agent, and MCP entity-page writes against pre-existing
  symlinked targets or parent directories.
- Fixed generated Claude Code hook commands on Windows Python paths with
  spaces.

### Changed

- Added macOS to the main CI matrix and expanded wheel smoke tests across
  Linux, Windows, and macOS.
- Added a native `install.ps1` wrapper and made `install.sh` use the host
  Python path separator.
- `v0.7.0` remains untouched because it is already published; the safe
  release tag for this patch line is `v0.7.1`.

## [0.7.0] - 2026-04-28

This release is the hardening and model-agnostic harness release. It
consolidates the MCP phase work that had accumulated under Unreleased,
then adds the full review remediation stack: shared recommendation
semantics, safer harness execution/resume/tooling, locked and durable
state writes, cleaner package/CI gates, and typed skill/agent/MCP
handling across the dashboard, resolver, manifest, and wiki.

### Security

- Hardened monitor mutations with same-origin and token checks.
- Locked manifest and wiki read-modify-write paths that can be hit by
  concurrent sessions or dashboard actions.
- Made MCP subprocess environment inheritance opt-in and validated
  live MCP `inherit_env` configs as strict booleans.
- Hardened install/archive/wiki paths against traversal, symlink, and
  unsafe extraction cases.
- Added an explicit approval/policy gate for model-driven tool calls.

### Harness

- Added the generic `ctx run`/`ctx resume`/`ctx sessions` harness and
  standalone `ctx-mcp-server` surface.
- Fixed resume ordering/tool restoration, terminal budget accounting,
  empty/truncated completion handling, compaction usage charging, and
  MCP request timeout behavior.
- Added opt-in live-host gates for Claude Code and MCP compatibility
  checks without spending quota or running third-party code by default.

### Recommendation And Wiki

- Unified recommendation behavior behind shared tag/entity logic across
  CLI, library, MCP, and harness surfaces.
- Made scan/resolve/manifests preserve typed skill, agent, and MCP
  entries.
- Made wiki sync write agents and MCP servers into their typed entity
  locations, including sharded MCP paths, instead of treating every
  manifest load as a skill.
- Added durable atomic writes for wiki/state files.

### CI And Release

- Added wheel/package smoke checks, version/tag alignment protections,
  clean-host contract coverage, and browser-monitor CI coverage.
- Updated package license metadata to the modern SPDX string form.
- Raised the local type gate to `python -m mypy src`.
- This release bumps package metadata from `0.6.4` to `0.7.0`; do not
  reuse the existing remote `v0.6.4` tag.

Detailed phase notes for the MCP work included in 0.7.0 are retained below.

## [0.7.0] — MCP Phase 6a — cheap wins before scale

Three small items from the Phase 2.5, 5, and 6 backlogs, bundled so
the scale work (6b–6f) starts from a clean base.

### Security

- **``_fs_utils.atomic_write_{text,bytes,json}``** now ``chmod 0o600``
  the temp file before ``os.replace`` so the renamed inode lands
  owner-only. Phase 2.5 security-reviewer MEDIUM: ``tempfile.mkstemp``
  creates with 0o600 on POSIX but ``os.replace`` onto an existing
  file can inherit the destination's more-permissive mode. Explicit
  chmod closes that window. Windows ignores the bits (OSError swallowed).

### Added

- **``ctx-mcp-fetch -v / --verbose``** — wires up ``logging.basicConfig``
  so library-module ``_logger.info`` / ``_logger.debug`` calls
  (Phase 2.5 print→logging cleanup) surface on stderr. ``-v`` = INFO,
  ``-vv`` = DEBUG. Default silent so JSONL pipe consumers stay clean.
- **``ctx-scan-repo --recommend``** — after stack detection, runs
  ``resolve()`` and prints a three-section summary to stdout: Skills
  (from ``manifest["load"]``), Agents (filtered from load by type),
  MCP Servers (``manifest["mcp_servers"]`` from Phase 5). Default
  off — scan stays fast for callers who only want the profile.

### Tests

- 4 new ``TestVerboseFlag`` cases in ``test_mcp_fetch_cli.py``: no-op
  at verbosity 0, INFO at -v, DEBUG at -vv, argparser accepts the flag.
- 5 new tests in ``test_fs_utils_permissions.py`` (POSIX-only via
  ``pytest.mark.skipif``): 0o600 on text/bytes/json writes, and the
  critical regression for the Phase 2.5 finding — overwriting a 0o644
  file must pin the result to 0o600.
- Total: **1626 passed, 6 skipped** (was 1621 → +5 new tests + 4
  new platform-skips, 0 regressions).

### Live verification

```
$ ctx-mcp-fetch --source awesome-mcp --limit 2 -v 2>&1 >/dev/null
[mcp_sources.awesome_mcp] parsed 2023 entries, skipped 0
[awesome-mcp] emitted 2 record(s)

$ ctx-scan-repo --repo . --recommend
... 16 Skills / 0 MCP Servers (with helpful hint to populate) / 4 Notes
```

## [0.7.0] — MCP Phase 5 — cross-type recommendations

Closes the loop on MCP integration: the graph already contained MCP
nodes with cross-type edges (Phase 3c) and had per-MCP quality scores
(Phase 4); Phase 5 wires them into the recommender so a user scanning
a repo sees MCP suggestions alongside skill/agent recommendations.

### Changed

- **``resolve_skills.resolve``**: graph-walk hits with
  ``type=="mcp-server"`` now land in ``manifest["mcp_servers"]`` as
  ``{name, reason, score, via, shared_tags}`` entries. Previously they
  were silently filtered out because the skill-availability check
  (``name in available``) dropped them — ``available`` contains only
  installed SKILLS.
- **Noise floor per hit type**: skills remain at ``>= 1.5`` (calibrated
  for the 1789-node / 454k-edge dense skill graph where single-tag
  overlaps are noise). MCPs get ``>= 1.0`` since the corpus is sparse
  (42 today, projected ~12k in Phase 6) so a single matched edge is
  already a meaningful signal.
- **Graph-walk pool widened from top_n=12 to top_n=30**: without this,
  equal-score-1.0 skills filled all 12 slots before any MCPs could
  surface. The downstream noise floor + availability filter keep the
  final manifest tight regardless of pool size.
- **``resolve_graph.resolve_by_seeds``** now recognizes the
  ``mcp-server:`` node prefix in addition to ``skill:`` and ``agent:``
  so a seed name that matches an MCP slug (e.g. ``github``,
  ``filesystem``) can kick off a walk from MCP territory too.
- **``wiki_graphify._attach_quality_attrs``** scans both
  ``~/.claude/skill-quality/*.json`` (skills + agents) and
  ``~/.claude/skill-quality/mcp/*.json`` (MCPs). Phase 4 put MCP
  sidecars in the ``mcp/`` subdir for isolation; without this Phase 5
  change, MCP graph nodes would never pick up their quality scores
  for Obsidian-graph coloring etc.

### Live verification

```
Synthetic GitHub Actions repo → resolve()
  load: 2 skills
  mcp_servers: 4 MCPs surfaced:
    github-mcp-server-awesome-variant   score=1.00   shared=['_t:github']
    github                              score=1.00   shared=['_t:github']
    tadas-github-a2asearch-mcp          score=1.00   shared=['_t:github']
    data-everything-mcp-server-templates score=1.00  shared=['_t:templates']
```

Each MCP recommendation carries a reason string and the shared graph
tags that produced it. Cross-type edges are the mechanism proven in
Phase 3c; Phase 5 is the presentation layer.

### Tests

- 5 new ``TestResolveMcpRecommendations`` cases in
  ``test_resolve_skills.py``: MCP lands in correct bucket (not load),
  reason+score preserved, dedup on repeat hits, noise floor respected,
  mixed skill+mcp hits route correctly.
- 3 new cases in ``test_wiki_graphify_quality.py``: mcp/ subdir
  loaded, missing ``subject_type`` field inferred from subdir,
  backward compat when mcp/ doesn't exist.
- Total: **1621 passed, 2 skipped** (was 1613 → +8 new, 0 regressions).

### Not yet done (Phase 5.5 or Phase 6)

- The ``scan_repo`` CLI doesn't yet print MCP recommendations to the
  terminal. The manifest is populated correctly but only consumers
  that read it (monitor, hooks) see MCPs. Minor UX gap.
- ``## Related MCP Servers`` section header on MCP entity pages can
  show skills as neighbors (accurate links, misleading header). Same
  neighbors-list works; just cosmetic. Defer to when all entity pages
  are regenerated.
- Data sparsity: the 42 MCPs in the wiki today are github/aggregator/
  playwright-themed. A random Python+JS repo may surface 0 MCP
  recommendations because no topical overlap exists. Phase 6 full
  ingest (~12k MCPs) will fix this organically.

## [0.7.0] — MCP Phase 4 — six-signal quality scorer

Adds the MCP-specific quality scorer with the six-signal model designed
in the Phase 4 interview: popularity / freshness / structural / graph /
trust / runtime. Config-overridable weights (default sum 1.0 with
popularity-heavy distribution per the locked decision). Reuses
``SignalResult`` from the existing skill quality system; otherwise
stands as a parallel module to preserve the working skill scorer
contract.

### Added

- **`mcp_quality_signals`** (NEW, ~370 lines): six pure-function signal
  extractors, all returning ``SignalResult`` with bounded score [0,1]
  and rich evidence dicts.
  - ``popularity_signal``: log-scaled GitHub stars, neutral 0.5 when
    star data missing
  - ``freshness_signal``: exponential decay with 90-day half-life on
    days-since-last-commit, neutral when missing
  - ``structural_signal``: 5 binary checks (description / repo_url /
    tags / transports / language) each contributing 1/5
  - ``graph_signal``: degree + cross-type-degree weighted score
  - ``trust_signal``: official-or-org tag, license presence, author
    presence
  - ``runtime_signal``: invocation telemetry placeholder (neutral 0.5
    until Phase 5+ runtime instrumentation lands)
- **`mcp_quality`** (NEW, ~1000 lines): orchestrator + ``ctx-mcp-quality``
  CLI mirroring the skill quality scorer pattern.
  - ``McpQualityConfig`` frozen dataclass with weight + threshold
    validation (sum-to-1.0, monotone A>=B>=C, etc.)
  - ``McpQualityScore`` frozen dataclass with ``to_dict`` for the
    sidecar JSON sink
  - ``compute_quality`` pure: weighted sum + grade map. Grade F band
    added for very low scores (<0.20) since MCP scorer has no hard-floor
    mechanism today
  - ``extract_signals_for_slug`` reads entity from
    ``entities/mcp-servers/<shard>/<slug>.md``, builds graph degrees
    from optional pre-loaded ``graph_index``
  - ``load_graph_index`` parses ``graphify-out/graph.json`` into
    {node_id: {degree, cross_type_degree}} index for cheap per-slug
    lookups
  - ``persist_quality`` writes three sinks atomically: sidecar JSON at
    ``~/.claude/skill-quality/mcp/<slug>.json`` (note the ``mcp/``
    subdir for clean separation from skill scores), entity-page
    frontmatter (``quality_score``/``quality_grade``/``quality_updated_at``),
    body block between ``<!-- quality:begin -->`` markers
  - CLI verbs: ``recompute --slug X | --all``, ``show <slug>``,
    ``explain <slug>``, ``list``. ``--wiki-dir PATH`` accepted on
    every verb so users can target a non-default wiki without exporting
    env vars.
- **``mcp_quality`` block in `config.json`**: weights + thresholds +
  saturation knobs + sidecar path. User can override via
  ``~/.claude/skill-system-config.json``.
- **`pyproject.toml`**: registers ``mcp_quality`` and
  ``mcp_quality_signals`` modules, plus the ``ctx-mcp-quality`` console
  script.

### Live verification

```
$ ctx-mcp-quality recompute --all
... (41 MCPs scored in 1.9 seconds)

$ ctx-mcp-quality list | sort -k2 | head -5
atlassian-cloud      B  score=0.61
github               B  score=0.61
ariekogan-ateam-mcp  B  score=0.62
arikusi-deepseek-mcp-server  B  score=0.62
playwright-browser-automation  B  score=0.67

Grade distribution: 16 B (40%), 25 C (60%) across 41 entities.
```

`playwright-browser-automation` top scorer (graph=1.00 from 104 cross-
type neighbors, trust=1.00 from official+org+license, structural=0.80
from 4/5 fields). Popularity/freshness/runtime all neutral 0.5 pending
Phase 6 detail-page enrichment.

### Tests

- 38 cases in ``test_mcp_quality_signals.py``: per-signal happy/edge,
  monotonicity, saturation, negative-input ValueError raises
- 30 cases in ``test_mcp_quality.py``: config validation, compute
  formula, signal extraction with/without graph_index, persist 3-sink
  idempotency, CLI for all 4 verbs (--help, recompute, show, list)
- Total: **1613 passed, 2 skipped** (was 1545 → +68 new, 0 regressions)

### Limitations

- Popularity, freshness, and runtime signals all return neutral 0.5
  until Phase 6 enrichment lands the missing fields (stars, last
  commit age, invocation telemetry). The infrastructure is in place;
  signals will become discriminating automatically once data arrives.
- Sidecar JSONs land at ``~/.claude/skill-quality/mcp/<slug>.json`` —
  ``wiki_graphify._attach_quality_attrs`` currently scans only the
  parent ``~/.claude/skill-quality/`` dir, so MCP scores don't yet
  decorate graph nodes. Phase 5 (recommender wiring) will update
  ``wiki_graphify`` to look in the ``mcp/`` subdir too.

## [0.7.0] — MCP Phase 3.6 — cross-source canonical-key dedup

Phase 3c surfaced that two MCP catalog sources slugify the same upstream
repo differently — awesome-mcp slugifies the README name, pulsemcp uses
the URL path — so the slug-based existence check in ``add_mcp`` would
create two separate entities for the same repo. This phase adds a
github_url-based canonical-key lookup that runs **before** the slug
check so the second source merges into the first entity rather than
duplicating.

### Added

- ``mcp_add._normalize_github_url(url)`` — lowercases host+path, strips
  trailing ``/``. Returns None for non-GitHub URLs. Mirrors
  ``McpRecord.canonical_dedup_key()`` so an existing-entity scan can
  match against new records on the same key.
- ``mcp_add._find_existing_by_github_url(mcp_dir, target)`` —
  scan-on-demand lookup across all entity files. Substring-greps the
  raw text first to avoid parsing 12k YAML frontmatters when the
  answer is almost always None, then confirms via frontmatter parse.

### Changed

- ``add_mcp`` runs the canonical-key lookup before the slug existence
  check. When a match is found at a different path than the new
  record's slug-based path, ``target_path`` is rewritten to the
  existing entity's path so the merge fires there.

### Limitations (will be addressed in Phase 6)

- Pulsemcp listing-page records (Phase 2b.5) carry only
  ``homepage_url: pulsemcp.com/servers/<slug>``, not ``github_url``.
  Cross-source dedup against awesome-mcp records won't fire for
  pulsemcp until Phase 6 detail-page enrichment populates the
  github_url field. The infrastructure is in place; only the data
  is missing.
- Scan cost is O(n) per add. Acceptable at the current ~40 entity
  scale and tolerable up to ~1k. At Phase 6's projected ~12k+ scale
  this needs a sidecar index (canonical_key → entity_relpath).

### Tests

- 7 new ``TestCrossSourceCanonicalKeyDedup`` cases covering
  normalize_github_url canonicalization, missing-dir tolerance,
  cross-source merge into existing path, no-github_url fallback,
  and non-collision proof for entities with mismatched URL fields.
- Total: 1545 passed, 2 skipped (was 1538 → +7 new, 0 regressions).

### Live verification

```
$ ctx-mcp-add --from-jsonl /tmp/cross_source_test.jsonl
[1/2] [added] github-mcp-server-awesome-variant
[2/2] [merged] example-org-cross-source-test
Done: 1 added, 1 merged, 0 rejected, 0 errors
```

Both records carried github_url=``https://github.com/example-org/cross-source-test-repo``
under different slugs and from different sources. Result: 1 entity
file (not 2), sources field merged to ``[awesome-mcp, pulsemcp]``.
Wiki entity count went 40 → 41 (not 42), proving the dedup blocked
the duplicate.

## [0.7.0] — MCP Phase 2.5 — reviewer cleanup

Rolls up the deferred python-reviewer + security-reviewer findings
from Phase 2a/2b/2b.5 that didn't block merge but were worth a
focused cleanup pass.

### Changed

- **`mcp_sources.awesome_mcp`**: replaced two `print(..., file=sys.stderr)`
  diagnostic calls with `logging.getLogger(__name__)` calls (per house
  rule: library code must not print). Removed the dead
  `if TYPE_CHECKING: pass` block and the dead `try/except ImportError`
  fallback that was needed only during the parallel-write Phase 2a.
- **`mcp_fetch.py`**: inlined the one-line `_iter_records` wrapper —
  it added no error handling beyond the existing `except` block in
  `_run_one`. Removed the now-unused `Source` import.
- **`mcp_sources/base.py`**: removed the `#!/usr/bin/env python3`
  shebang. `base.py` is a library, not an entry point — the shebang
  was a leftover from the Foundation Engineer's template.

### Deferred (still pending)

- `_fs_utils.atomic_write_text` `chmod 0o600` (security MEDIUM): touches
  a shared utility used by 14+ modules; warrants its own focused
  commit with broader testing rather than rolling into this cleanup.
- `_parse_readme` lazy-yield refactor (Python LOW): current 1.2 MB
  peak for the awesome-mcp README is fine.
- `@dataclass(frozen=True)` for Source classes (Python LOW): style
  only, no behavior change.
- TypedDict for JSON shapes in old pulsemcp.py: the API mode that
  used those shapes was removed in 2b.5.

### Verification

- 1535 passed, 2 skipped (no regressions vs. Phase 2b.5)
- ruff + mypy clean on all touched files
- Live `ctx-mcp-fetch --source awesome-mcp --limit 2` and
  `ctx-mcp-fetch --source pulsemcp --limit 2` both produce valid
  JSONL with progress emitted to stderr (no longer pollutes stdout)

## [0.7.0] — MCP Phase 2b.5 — pulsemcp switched to public HTML scraping

The pulsemcp.com Sub-Registry API requires per-account credentials
that aren't broadly available. Phase 2b.5 swaps the auth-gated JSON
client for a stdlib HTML scraper of the public listing pages
(`https://www.pulsemcp.com/servers?page=N`), making the source
usable by anyone without contacting PulseMCP for an API key.

### Changed

- **`mcp_sources.pulsemcp` rewritten** for HTML scraping mode. Walks
  pages 1..310 (current total: ~12,975 servers / 42 per page = 310
  pages). Cards are delineated by `data-test-id="mcp-server-card-<slug>"`
  attributes — content-addressed so a frontend restyle of class names
  doesn't silently break the parser. Per-card extraction uses
  `html.parser.HTMLParser` (stdlib) for robustness against malformed
  markup.
- **Per-card data**: slug, name, creator (author), description,
  classification (official / community / reference → tag). Detail-page
  enrichment (github_url, language, transports) deferred to Phase 6.
- **No credentials required**. The PULSEMCP_API_KEY / PULSEMCP_TENANT_ID
  env vars are no longer read; the credential-injection guard, cursor
  URL-encoding guard, and 429 handling all dropped along with the API
  client. Attack surface for the source is now strictly the HTML
  parser.

### Removed

- `src/tests/fixtures/pulsemcp_page1.json` and `pulsemcp_page2.json`
  (API-mode fixtures). Replaced with `pulsemcp_listing_excerpt.html`
  — a 3-card real-HTML snippet captured from page 1 of the live site.
- `_credentials`, `_MissingPulsemcpCredentialsError`,
  `_InvalidPulsemcpCredentialError`, `_build_url`,
  `_to_record(server_obj, meta)` API-shape mapper. Replaced with
  HTML-shape `_to_record(card_html)`, `_split_cards`, `_parse_listing`,
  and `_CardTextExtractor`.

### Tests

- 20 new tests in `test_mcp_sources_pulsemcp.py` covering split,
  parse, mapping, and pagination paths against the real-HTML fixture.
- Total: **1,535 passed, 2 skipped** (was 1,546 → -11 net because
  the 31 API-mode tests were swapped for 20 HTML tests; 0 regressions
  in any unrelated suite).

### Live verification

```
$ ctx-mcp-fetch --source pulsemcp --limit 5 | ctx-mcp-add --from-stdin
[1/5] [added] playwright-browser-automation
[2/5] [added] duckdb
[3/5] [added] excel-file-manipulation
[4/5] [added] office-word
[5/5] [added] context7-documentation-database
Done: 5 added, 0 merged, 0 rejected, 0 errors
```

Sharded paths verified: `entities/mcp-servers/{c,d,e,o,p}/<slug>.md`.
Cleaned up before commit.

## [0.7.0] — MCP Phase 2b — pulsemcp source

Adds the second catalog source: `www.pulsemcp.com` Sub-Registry API
(~12,975 servers as of today). Uses the official JSON API
(`/api/v0.1/servers`) with cursor-based pagination, gated by API
credentials (`PULSEMCP_API_KEY` + `PULSEMCP_TENANT_ID` env vars).

### Added

- **`mcp_sources.pulsemcp`** — Source implementation against the
  Generic MCP Registry API spec with PulseMCP `_meta` extensions.
  Maps `repository.source == "github"` to `github_url`, infers
  `language` from `packages[].registry_name` (`npm` → typescript,
  `pypi` → python, `cargo` → rust, etc.), and tags entries with
  `_meta["com.pulsemcp/server"].isOfficial == true` as `"official"`.
  Caches raw page JSON under `~/.claude/skill-wiki/raw/marketplace-dumps/pulsemcp/<date>--page-NNNN.json`.
- **`fetch_text(headers=...)`** in `mcp_sources.base` — optional
  per-call header dict, merged on top of the User-Agent default.
  Existing callers unchanged.

### Security

- **CRLF header-injection guard** in `_credentials()`: rejects API key
  or tenant ID values containing `\r`, `\n`, or `:`. Python's
  `urllib.request.Request` does NOT sanitize header values on 3.11+,
  so a malicious env-var value would otherwise inject arbitrary
  headers into every authenticated request. Error message does not
  echo the rejected value (no secret leakage).
- **Cursor URL-encoding** in `_build_url`: opaque cursors from the
  upstream API are wrapped with `urllib.parse.quote(safe='')` to
  prevent query-string smuggling (a cursor containing `&` would
  inject extra parameters; `#` would silently truncate).
- **Stripped diagnostic output**: malformed-entry warnings no longer
  echo the full server object repr to stderr — they emit only the
  fact that `name` was missing, in case upstream payloads ever
  contain sensitive fields.

### Tests

- **31 new tests** in `test_mcp_sources_pulsemcp.py`:
  - 11 `_to_record` mapping tests (happy path, missing fields, github
    vs gitlab, official tag, language inference, round-trip through
    `McpRecord.from_dict`)
  - 4 credential-handling tests
  - 4 NEW credential-injection regression tests (CR, LF, colon, secret
    not echoed in error)
  - 3 NEW cursor URL-encoding regression tests
  - 5 pagination tests (single page, two pages with cursor, limit
    short-circuits second-page fetch, limit spans pages, 429 raises)
  - 4 SOURCE singleton tests
  - Tests use an autouse `_isolate_wiki` fixture so cache writes don't
    pollute the user's real `~/.claude/skill-wiki/`.
- **Total**: 1,546 passed, 2 skipped (was 1,515 → +31 net, 0 regressions).
- **`pytestmark skipif` removed** — the module now ships, so a broken
  import should fail the suite rather than silently skip.

### Live verification

```
$ ctx-mcp-fetch --list-sources
awesome-mcp     https://github.com/punkpeye/awesome-mcp-servers
pulsemcp        https://www.pulsemcp.com/servers

$ ctx-mcp-fetch --source pulsemcp --limit 1
Error: source 'pulsemcp' failed: Missing required environment
variable(s): PULSEMCP_API_KEY, PULSEMCP_TENANT_ID. Obtain API
credentials from https://www.pulsemcp.com/settings/api-keys and
set them before running the pulsemcp source.
```

Authenticated end-to-end fetch was NOT verified live — the test author
does not have PulseMCP API credentials. The fetch path is exercised
through the recorded JSON fixtures (`pulsemcp_page1.json`,
`pulsemcp_page2.json`) covering both single-page and multi-page cursor
pagination. Users with credentials can run
`PULSEMCP_API_KEY=... PULSEMCP_TENANT_ID=... ctx-mcp-fetch --source pulsemcp --limit 5 | ctx-mcp-add --from-stdin`
to confirm.

### Reviewer findings deferred (Phase 2.5 cleanup)

- File permission tightening on cache writes (Sec MEDIUM): `atomic_write_text` doesn't `chmod 0o600` — affects all sources, not just pulsemcp
- HTTPError body sanitization in non-429 re-raise path (Sec MEDIUM)
- TypedDict for JSON shapes in `pulsemcp.py` (Python MEDIUM)
- `@dataclass(frozen=True)` for `_PulsemcpSource` (Python LOW)
- Consolidate `noqa: no-any-return` suppressions (Python LOW)

## [0.7.0] — MCP Phase 2a — fetcher + first source

Adds the `Source` protocol, an SSRF-hardened HTTP fetcher, and the
first real catalog source: `github.com/punkpeye/awesome-mcp-servers`.
Live verification fetched 2,023 entries from the actual README; the
end-to-end pipe `ctx-mcp-fetch --source awesome-mcp --limit 5 |
ctx-mcp-add --from-stdin` ingested 5 records into the wiki, all of
which entered the knowledge graph and clustered together via shared
tags (e.g. all five `aggregator`-tagged MCPs formed a clique).

### Added

- **`mcp_sources.base`** — `Source` Protocol (`fetch(*, limit, refresh)
  → Iterator[dict]`), `cache_path` / `read_cache` / `write_cache`
  helpers under `~/.claude/skill-wiki/raw/marketplace-dumps/<source>/`,
  and an SSRF-defended `fetch_text(url)` HTTP client.
- **`mcp_sources.awesome_mcp`** — parses the punkpeye README into
  raw record dicts. Walks `## Server Implementations` → `### <Section>`
  → `- [name](url) - description` hierarchy. Tags inferred from
  section header (emoji-stripped). Language inferred from per-line
  emoji flags. ~2,000 records on the live README.
- **`mcp_fetch.py` + `ctx-mcp-fetch` CLI** — JSONL stream to stdout,
  progress to stderr (clean to pipe into `ctx-mcp-add --from-stdin`).
  `--list-sources`, `--source <name>`, `--limit N`, `--refresh`.

### Security

- **`fetch_text` defense in depth**: HTTPS-only, allowlist of 5 hosts
  (`raw.githubusercontent.com`, `github.com`, `api.github.com`,
  `pulsemcp.com`, `www.pulsemcp.com`), no redirects (3xx raises),
  10 MB response body cap (raises `_ResponseTooLargeError` rather
  than silently truncating).
- **`cache_path` path-traversal guard** — both `source_name` and
  `basename` validated as plain filenames (no `/`, `\`, leading dot).
- **`mcp_add._build_corpus_text` YAML safety** — frontmatter now
  rendered via `yaml.safe_dump` rather than f-string interpolation
  so a malicious description can't escape the YAML scalar.

### Fixed

- **Reload safety** in `mcp_sources.base`: imports the `ctx_config`
  module rather than `cfg` directly, so `ctx_config.reload()` (used
  by tests) doesn't leave a stale singleton reference.

### Changed

- `pyproject.toml` — adds `mcp_fetch` to `py-modules`, `mcp_sources`
  to `packages`, and registers the `ctx-mcp-fetch` console script.

### Tests

- 49 new tests across `test_mcp_sources_base.py` (24, including 4
  path-traversal regressions and 1 response-size cap regression),
  `test_mcp_sources_awesome.py` (12), `test_mcp_fetch_cli.py` (10),
  plus 3 new `TestCorpusTextStructure` cases pinning the YAML safety
  in `mcp_add`. Total: 1,515 / 1,517 passed (2 pre-existing skips,
  0 regressions).

## [0.7.0] — MCP Phase 1 — foundation

First-class **MCP server** entity type alongside skills and agents.
Phase 1 ships the data model, intake hooks, and ingest CLI; no fetcher
yet (Phase 2) and no quality scoring yet (Phase 4).

### Added

- **`mcp_entity.McpRecord`** — frozen dataclass capturing one MCP
  server: slug, description, sources, github URL, tags, transports,
  language, license, author, stars, last commit. Normalizes slug to
  `[a-z0-9-]+`, canonicalizes GitHub URLs, filters transports to the
  known subset, deduplicates and sorts tags. Provides `from_dict`,
  `to_frontmatter`, `entity_relpath` (sharded `<first-letter>/<slug>.md`,
  `0-9/` for digit-leading slugs), and `canonical_dedup_key` (github URL
  > slug fallback).
- **`mcp_add.add_mcp` + `ctx-mcp-add` CLI** — orchestrator that
  installs one MCP record into `~/.claude/skill-wiki/entities/mcp-servers/`
  via the existing intake gate. Idempotent: re-adding the same record
  is a no-op; re-adding from a different source merges sources into one
  page. CLI flags: `--from-json`, `--from-jsonl`, `--from-stdin`,
  `--dry-run`, `--skip-existing`, `--wiki`. Non-fatal embedding failure
  matches the agent_add convention.
- **`generate_mcp_page`** in `wiki_batch_entities.py` — renderer that
  matches the existing skill / agent page layout, with sections for
  Sources (links to GitHub + homepage), Tags, Transports, and a
  placeholder Related block for graph backlinks.
- **`mcp-servers` subject type** in `intake_pipeline._SUBJECT_TYPES` —
  MCPs get their own embedding cache namespace, isolated from skills
  and agents.

### Changed

- `pyproject.toml` — registers `mcp_add` and `mcp_entity` modules and
  the `ctx-mcp-add` console script.

### Notes

- Storage is sharded from day one (`entities/mcp-servers/<first-letter>/<slug>.md`)
  to keep listings fast at the projected ~12k+ scale.
- Quality scoring, fetchers, and cross-type recommendations land in
  Phases 2-5. Phase 1 is infra only.
- `wiki_sync.update_index` writes the `## Skills` section header for
  every subject type (pre-existing limitation also affecting agents).
  Subject-aware index updates deferred to a follow-up cleanup.

## [0.6.4] — 2026-04-20

Dashboard-tab release. v0.6.3 added docs for the graph and the KPI
pipeline; v0.6.4 exposes both (plus a proper wiki browser) as
top-level navigation tabs in `ctx-monitor`, so the docs pages match
what's actually reachable in the UI.

### Added

- **`/kpi` HTML route** — renders `kpi_dashboard.generate()` as a
  browser view. Six sections: grade distribution, lifecycle tiers
  (active/watch/demote/archive), hard-floor reasons, by-category
  A/B/C/D/F mix, top-25 demotion candidates (active/watch entries
  graded D/F, sorted by D-streak desc then score asc), and the
  archived list. Each demotion-candidate slug is a link to
  `/skill/<slug>`. Empty-state page points at `ctx-skill-quality
  score --all` when the sidecar dir is empty.
- **`/api/kpi.json`** — JSON passthrough of the `DashboardSummary`
  dataclass for scripting. Same shape as `python -m kpi_dashboard
  render --json`.
- **`/wiki` index route** — card grid of every entity page under
  `~/.claude/skill-wiki/entities/{skills,agents}/`. Left sidebar:
  text search (slug · description · tag), skill/agent checkboxes,
  live "N of M match" counter. Each card shows the slug, quality
  grade pill (when a sidecar exists), description, and tag preview.
  Slug allowlist (`^[a-z0-9][a-z0-9_.-]{0,127}$`) applied to every
  file glob to keep path-traversal bugs out of the index.
- **`/graph` landing-page seeds** — when no slug is selected, the
  graph page now shows a "Popular seed slugs" panel with the 18
  highest-degree entities as clickable chips, plus a stats line with
  node + edge counts. First-time visitors no longer land on a blank
  cytoscape canvas with nothing to click.
- **Wiki + KPI tabs in the top nav** — every page now shows
  `Home · Loaded · Skills · Wiki · Graph · KPIs · Sessions · Logs ·
  Live`.

### Changed

- **`ctx_monitor.py` module docstring** — updated the route
  catalogue to the full 15 routes (was 8). New dev-reference table
  matches what the server actually exposes.
- **`docs/dashboard.md`** — added a Usage section with three
  walkthroughs (Browse the LLM wiki, Explore the knowledge graph,
  Read the quality KPIs). Route tables extended with `/wiki`,
  `/kpi`, `/api/kpi.json` rows.

### Tests

- +9 tests in `src/tests/test_ctx_monitor.py` covering the new
  routes (empty + populated states), the slug-allowlist gate on the
  wiki index, the seed-chip panel on `/graph` landing, and nav-bar
  tab presence. Full suite: 1,372 passing, 2 skipped.

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
