# Dashboard (`ctx-monitor`)

Local HTTP dashboard for ctx's currently supported live observables:
loaded skills, agents, MCP servers, and installed harness records; session timelines; the
knowledge graph; the LLM-wiki browser; quality grades + scores;
durable queue state; graph/wiki artifact versions; filterable audit
logs; a live event stream; and cataloged harness wiki/graph browsing.

```bash
ctx-monitor serve              # http://127.0.0.1:8765
ctx-monitor serve --port 8888  # custom port
ctx-monitor serve --host 0.0.0.0 --port 8888  # LAN-visible (explicit opt-in)
```

Zero Python dependencies added by the dashboard. Everything runs on
stdlib `http.server`, using daemon request threads so a live
`/api/events.stream` client cannot block normal dashboard or JSON API
requests. Cytoscape.js is loaded from a CDN on the `/graph` route only.

## Usage

Every page in the dashboard has the same top nav, so getting around
is `Home -> jump anywhere`. The dashboard indexes skills, agents, MCP
servers, and harness pages in wiki/graph views. Harness install, update,
load/unload, and quality scoring remain CLI/API workflows.

### Check queue and artifact state - `/status`

The status tab shows the durable wiki/graph maintenance queue and the
generated graph/wiki artifacts that ctx can ship or consume. It reports:

- queue DB availability and job counts by state (`pending`, `running`,
  `succeeded`, `failed`)
- the 20 most recent queue jobs with kind, attempts, source, worker, and
  last error
- artifact presence and byte size for generated
  `~/.claude/skill-wiki/graphify-out/{graph.json,graph-delta.json,communities.json}`
  plus `wiki-graph.tar.gz` and `skills-sh-catalog.json.gz` from
  `~/.claude/graph/` when installed there, falling back to the repo `graph/`
  directory during source checkouts
- artifact promotion metadata, including the latest promoted hash when
  the crash-safe promotion path has recorded it

### Browse the LLM wiki — `/wiki`

The wiki tab is a filterable card grid over a bounded dashboard sample:
up to 500 pages per dashboard-supported entity type under
`~/.claude/skill-wiki/entities/{skills,agents,mcp-servers,harnesses}/`.
MCP server pages use the sharded layout
`entities/mcp-servers/<first-char-or-0-9>/<slug>.md`; the dashboard
routes `/wiki/<slug>` to the same shard convention. Harness pages use
the flat `entities/harnesses/<slug>.md` layout. Each card shows:

- the slug (click to open `/wiki/<slug>?type=<entity>`)
- the quality grade pill (A/B/C/D/F) when the entity has a sidecar,
  otherwise a `skill`, `agent`, `mcp-server`, or `harness` type badge
- the frontmatter `description`
- up to 6 tags

The **left sidebar** has a text search over the visible sample that
matches slug, description, and tags, plus skill/agent/MCP/harness type
checkboxes. Pair them to
answer questions like "show me all grade-B agents related to
testing" — check `agent`, type `testing` in the search box.

Dashboard-supported entity pages (`/wiki/<slug>?type=<entity>`) render the full
markdown body, the frontmatter table on the right, and a quality banner
with deep links to `/skill/<slug>` (sidecar detail) and
`/graph?slug=<slug>&type=<entity>` (1-hop neighborhood).

### Explore the knowledge graph — `/graph`

The graph tab is a cytoscape-rendered view over the dashboard-supported
skill/agent/MCP/harness graph. The shipped graph bundle also contains
remote-cataloged Skills.sh `skill` nodes. Harness nodes are browsable
and filterable here; install/update actions remain in `ctx-harness-install`.
When you arrive with no
slug selected, the page shows:

- a stats line with the total node + edge counts
- a **Popular seed slugs** panel — the 18 highest-degree entities
  rendered as clickable chips (skills in indigo, agents in amber).
  Click a chip to explore that entity's 1-hop neighborhood
- a search box — type any valid skill, agent, MCP, or harness slug and press
  `explore` (or hit Enter)
- the cytoscape canvas itself, which activates as soon as you pick a
  seed

Inside the cytoscape view, node colors mean:

- **emerald** — the focus node you searched for
- **indigo** — skills
- **amber** — agents
- **red diamond** — MCP servers
- **green hexagon** — harnesses

Edge width encodes the blended graph `weight` attribute, combining semantic
similarity, explicit tag overlap, and slug-token overlap where available.
Thicker lines = stronger relationships. **Tap any node** to
navigate to that entity's wiki page. The type checkboxes hide or show
skills, agents, MCP servers, and harnesses without reloading the graph.

### Read the quality KPIs — `/kpi`

The KPI tab is the browser equivalent of `python -m kpi_dashboard
render`. It aggregates the quality + lifecycle sidecars under
`~/.claude/skill-quality/` into a single page with six tables:

1. **Header banner** — total entity count, subject breakdown, grade
   pill counts, link to the raw `/api/kpi.json` payload, link back to
   `/skills`.
2. **Grade distribution** — A/B/C/D/F count and share.
3. **Lifecycle tiers** — counts for `active`, `watch`, `demote`,
   `archive`.
4. **Hard floors active** — which override reasons are currently
   pinning entities to F (`never_loaded_stale`, `intake_fail`, etc.)
   and how many entities each one catches.
5. **By category** — per-category count, average score, and full
   A/B/C/D/F mix. This is the row most useful for "where are my D/F
   skills concentrated?"
6. **Top demotion candidates** — up to 25 active-or-watch entities
   graded D/F, sorted by consecutive-D streak desc then raw score
   asc. Click a slug to jump to its sidecar.
7. **Archived** — slugs currently in the archive tier, with their
   last-known grade.

If the quality sidecar directory is empty (no scoring has happened
yet), the page shows a helpful empty-state pointing at
`ctx-skill-quality recompute --all`.

## Routes

### Top navigation

Every page shows the same nav bar. The ten tabs cover the
dashboard-supported observable surface of ctx:

```
Home · Loaded · Skills · Wiki · Graph · Status · KPIs · Sessions · Logs · Live
```

### HTML views

Harness catalog entries are visible in wiki and graph routes. `/loaded` shows
installed harness records from `~/.claude/harness-installs/*.json`, not the
full catalog. Harness installation, update, uninstall, and quality scoring
remain CLI/API workflows.
Dashboard POST actions are available only from loopback clients and require the
per-process monitor token injected into the rendered page.

| Route | What it shows |
|---|---|
| `/` | Home: six stat cards (loaded, sidecars, wiki entities, graph nodes, audit events, sessions), grade distribution pills, recent sessions table, recent audit events |
| `/loaded` | **Currently-loaded skills, agents, MCP servers, and installed harness records** from `~/.claude/skill-manifest.json` plus `~/.claude/harness-installs/*.json`; skill/agent/MCP rows expose supported live actions |
| `/skills` | Every sidecar as a filterable **card grid**: left sidebar (search by slug, grade checkboxes, skill/agent/MCP toggle, hide-floored), card shows grade pill + raw score + links to sidecar/wiki/graph |
| `/skill/<slug>` | Full sidecar breakdown: four-signal score (telemetry · intake · graph · routing), hard-floor reason, computed_at timestamp, per-skill audit timeline |
| `/wiki` | **Wiki entity index** - bounded card-grid sample of up to 500 pages per dashboard-supported entity type under `~/.claude/skill-wiki/entities/{skills,agents,mcp-servers,harnesses}/`, including sharded MCP server pages and flat harness pages. Left sidebar: text search over the visible sample (slug, description, tag), skill/agent/MCP/harness checkboxes. |
| `/wiki/<slug>?type=<entity>` | Dashboard-supported wiki entity page rendered: markdown body + full frontmatter table + grade banner + deep links to sidecar and graph-neighborhood views. The optional `type` query disambiguates duplicate slugs such as `langgraph`. |
| `/graph` | **Graph explorer landing page** - node/edge count header, a "Popular seed slugs" block (18 highest-degree skill/agent/MCP/harness entities as clickable chips), search box for any skill/agent/MCP/harness slug, and the cytoscape canvas. Clicking a seed chip navigates to `/graph?slug=<slug>&type=<entity>`. |
| `/graph?slug=<slug>&type=<entity>` | **Cytoscape-rendered** 1-hop neighborhood around the target skill/agent/MCP/harness slug. Node colors: emerald=focus, indigo=skill, amber=agent, red diamond=MCP server, green hexagon=harness. Edge width maps to blended graph weight. Tap any node to navigate to that entity's typed wiki page. Type and tag filters run client-side. |
| `/status` | Durable queue and artifact status: job counts by state, recent queue jobs, graph/wiki artifact sizes, and crash-safe promotion metadata. |
| `/kpi` | **KPI dashboard** — total entity count with subject breakdown, grade distribution pills, two-column tables for grade counts and lifecycle tiers (active · watch · demote · archive), hard-floor reasons with counts, **By category** table (count · avg score · A/B/C/D/F mix per category), **Top demotion candidates** (active/watch entries graded D or F, sorted by consecutive-D streak desc then score asc), and the **Archived** list. Same shape as `python -m kpi_dashboard render` but HTML |
| `/sessions` | Index of every session (audit + skill-events), first/last seen, counts of skills loaded/unloaded, agents loaded/unloaded, MCPs loaded/unloaded, and lifecycle transitions |
| `/session/<id>` | Per-session audit timeline showing the load → score_updated → unload triad with timestamps |
| `/logs` | Last 500 audit events in a filterable table (client-side filter on event name, subject, session id) |
| `/events` | Live SSE stream of new audit events |

### JSON API

| Route | Returns |
|---|---|
| `GET /api/sessions.json` | All sessions with aggregated counts |
| `GET /api/manifest.json` | Raw `skill-manifest.json` passthrough |
| `GET /api/status.json` | `{queue, artifacts}` payload: durable queue counts/recent jobs plus graph/wiki artifact file status and promotion metadata |
| `GET /api/skill/<slug>.json` | Raw sidecar for one slug |
| `GET /api/graph/<slug>.json?type=<entity>&hops=1&limit=40` | Dashboard-shaped skill/agent/MCP/harness `{nodes, edges, center}`; `type` is optional but recommended for duplicate slugs, `hops` is [1, 3], `limit` is [5, 150]. |
| `GET /api/kpi.json` | `DashboardSummary` passthrough — `{total, by_subject, grade_counts, lifecycle_counts, category_breakdown, hard_floor_counts, low_quality_candidates, archived, generated_at}`. Returns `{total: 0, detail: "no sidecars yet"}` when the quality directory is empty |
| `GET /api/events.stream` | Server-sent events tail of `~/.claude/ctx-audit.jsonl` |

### Mutation endpoints

Dashboard GET views are read-only. Both POST endpoints enforce same-origin (browser tab open on another
origin can't forge a request), require the per-process
`X-CTX-Monitor-Token` injected into the dashboard page, and reject any
slug failing the shared safe-name validator. That validator blocks path
separators, Windows drive-relative strings, malformed names, and Windows
reserved device names such as `con.txt` and `nul.`. There is no harness
load/unload mutation endpoint yet.

| Route | Body | Calls |
|---|---|---|
| `POST /api/load` | `{"slug": "...", "entity_type": "skill"}` | `skill_install.install_skill(slug)` |
| `POST /api/load` | `{"slug": "...", "entity_type": "agent"}` | `agent_install.install_agent(slug)` |
| `POST /api/load` | `{"slug": "...", "entity_type": "mcp-server"}` | `mcp_install.install_mcp(slug, command?, json_config?, auto=True)` |
| `POST /api/unload` | `{"slug": "...", "entity_type": "skill"}` | `skill_unload.unload_from_session([slug])` |
| `POST /api/unload` | `{"slug": "...", "entity_type": "agent"}` | remove the agent row from `skill-manifest.json` and append an unload row |
| `POST /api/unload` | `{"slug": "...", "entity_type": "mcp-server"}` | `mcp_install.uninstall_mcp(slug, wiki_dir=...)` |

Harness load/unload POSTs are rejected with the exact
`ctx-harness-install ... --dry-run` command to run instead. Skill rows emit
`skill.loaded` / `skill.unloaded`, agent rows emit `agent.loaded` /
`agent.unloaded`, and MCP rows emit `toolbox.triggered` with
`meta.entity_type="mcp-server"` and `meta.action` set to `loaded` or
`unloaded`. All dashboard-driven rows use `actor=user` and
`meta.via="ctx-monitor"` so they appear in the session timeline.

## KPIs, measures, scores

The dashboard surfaces every quality signal ctx currently computes for
sidecar-backed skills, agents, and MCP servers. Harness scoring is not
yet exposed in the dashboard. Nothing is aggregated-only — you can
always drill from a headline number to the raw sidecar that produced it.

### On the home page

| Card | What it means |
|---|---|
| **Currently loaded** | Count of entries in `skill-manifest.json[load]`. Clicking the card drills to `/loaded` |
| **Sidecars** | Total sidecars in `~/.claude/skill-quality/` |
| **Wiki entities** | Count of dashboard-supported wiki pages (skills + agents + MCP servers + harnesses) |
| **Knowledge graph** | Dashboard-supported skill/agent/MCP/harness node count + edge count from `graphify-out/graph.json` |
| **Audit events** | Line count of `~/.claude/ctx-audit.jsonl` |
| **Sessions** | Unique session IDs seen across audit + events |
| **Grade pills** | A / B / C / D / F counts across all sidecars, colored |

### On `/skills`

Every card shows:

- **grade** — A / B / C / D / F pill (A=green, F=red)
- **raw score** — float in [0, 1] before the hard-floor override
- **subject_type** — skill, agent, or mcp-server
- **hard floor reason** — `never_loaded_stale`, `intake_fail`, etc.
  when the floor is active

Cards sorted by `(grade, -raw_score)` so high-scoring A's come first.

### On `/skill/<slug>`

The full four-signal breakdown from the sidecar:

| Signal | Weight (default) | What it measures |
|---|---:|---|
| **Telemetry** | 0.40 | Load frequency + recency from `skill-events.jsonl`. Rewards skills that are actually used. |
| **Intake** | 0.20 | Structural health: frontmatter fields present, H1 present, minimum body length, description length. Zero if `intake_fail` floor is active. |
| **Graph** | 0.25 | Connectivity in the knowledge graph: degree, average edge weight, community size |
| **Routing** | 0.15 | Router hit rate from `~/.claude/router-trace.jsonl`: how often this skill was among the top-K recommendations when surfaced |

The final score is `sum(weight[i] * signal[i])`. A hard floor
(`never_loaded_stale`, `intake_fail`) can override the score to
force an F grade regardless of other signals.

The skill detail page also shows the audit timeline for this slug
specifically: every `skill.loaded`, `skill.unloaded`,
`skill.score_updated` row with its session_id, so you can trace
exactly why the score changed when it did.

### On `/session/<id>`

The per-session view lets you watch a skill's lifecycle inside one
session:

```
skill.loaded        fastapi-pro       session-abc  @ 10:23:05
skill.score_updated fastapi-pro       session-abc  @ 10:31:47   grade C->B
skill.unloaded      fastapi-pro       session-abc  @ 11:04:02
```

The `load → score_updated → unload` triad is the canonical
observability proof that ctx's telemetry pipeline is live.

## Security

- **Binds to 127.0.0.1 by default**. Use `--host 0.0.0.0` only if
  you actually want LAN-visible. No authentication; the server is
  intended for a local developer's own machine.
- **Same-origin gating on mutation**. Any POST with an `Origin`
  header that doesn't match `Host` returns 403. Curl and direct
  tool calls are allowed (no Origin header at all).
- **Slug allowlist on all paths**. Anywhere the dashboard resolves
  a skill, agent, MCP, or harness slug to a file path (`/wiki/<slug>`,
  `/graph?slug=<slug>&type=<entity>`, `/api/graph/<slug>.json`), the slug is
  validated through the shared
  safe-name helper — no path traversal, no absolute paths, no UNC
  shares, no Windows reserved device names.

## Stopping

Ctrl+C in the terminal. Request handling is threaded for local dashboard
responsiveness, and shutdown signals any open SSE workers. The monitor is
still not suitable for shared/production serving.
