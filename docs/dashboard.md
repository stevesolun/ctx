# Dashboard (`ctx-monitor`)

Local HTTP dashboard for ctx's currently supported live observables:
loaded skills, agents, and MCP servers; session timelines; the
knowledge graph; the LLM-wiki browser; quality grades + scores;
filterable audit logs; and a live event stream. Dashboard harness
exposure is not yet present.

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
is `Home → jump anywhere`. The three feature tabs new in v0.6.4 are
how you explore the dashboard-supported ctx corpus without ever touching
the CLI. The underlying ctx catalog can include harness pages and
recommendations, but `ctx-monitor` does not yet index, render, filter,
load, unload, or score harness entries.

### Browse the LLM wiki — `/wiki`

The wiki tab is a filterable card grid of **every dashboard-supported
entity page** under
`~/.claude/skill-wiki/entities/{skills,agents,mcp-servers}/`. MCP
server pages use the sharded layout
`entities/mcp-servers/<first-char-or-0-9>/<slug>.md`; the dashboard
routes `/wiki/<slug>` to the same shard convention. Harness pages may
exist under `entities/harnesses/`, but dashboard wiki exposure for
harnesses is not yet present. Each card shows:

- the slug (click to open `/wiki/<slug>`)
- the quality grade pill (A/B/C/D/F) when the entity has a sidecar,
  otherwise a `skill`, `agent`, or `mcp-server` type badge
- the frontmatter `description`
- up to 6 tags

The **left sidebar** has a text search that matches across slug,
description, and tags, plus skill/agent/MCP type checkboxes. Pair them to
answer questions like "show me all grade-B agents related to
testing" — check `agent`, type `testing` in the search box.

Dashboard-supported entity pages (`/wiki/<slug>`) render the full
markdown body, the frontmatter table on the right, and a quality banner
with deep links to `/skill/<slug>` (sidecar detail) and
`/graph?slug=<slug>` (1-hop neighborhood).

### Explore the knowledge graph — `/graph`

The graph tab is a cytoscape-rendered view over the dashboard-supported
skill/agent/MCP graph. The shipped graph bundle also contains Skills.sh
`external-skill` nodes and the graph build/recommendation APIs can be
harness-aware, but this dashboard view does not yet render harness or
external-skill nodes as first-class entities. When you arrive with no
slug selected, the page shows:

- a stats line with the total node + edge counts
- a **Popular seed slugs** panel — the 18 highest-degree entities
  rendered as clickable chips (skills in indigo, agents in amber).
  Click a chip to explore that entity's 1-hop neighborhood
- a search box — type any valid skill, agent, or MCP slug and press
  `explore` (or hit Enter)
- the cytoscape canvas itself, which activates as soon as you pick a
  seed

Inside the cytoscape view, node colors mean:

- **emerald** — the focus node you searched for
- **indigo** — skills
- **amber** — agents
- **red diamond** — MCP servers

Edge width encodes the `weight` attribute (count of shared tags), so
thicker lines = stronger semantic relationships. **Tap any node** to
navigate to that entity's wiki page. The type checkboxes hide or show
skills, agents, and MCP servers without reloading the graph. There is no
harness filter or harness node styling yet.

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
`ctx-skill-quality score --all`.

## Routes

### Top navigation

Every page shows the same nav bar. The nine tabs cover the
dashboard-supported observable surface of ctx:

```
Home · Loaded · Skills · Wiki · Graph · KPIs · Sessions · Logs · Live
```

### HTML views

Harness catalog entries are absent from these routes today; they remain
available through the CLI/API recommendation surfaces.

| Route | What it shows |
|---|---|
| `/` | Home: six stat cards (loaded, sidecars, wiki entities, graph nodes, audit events, sessions), grade distribution pills, recent sessions table, recent audit events |
| `/loaded` | **Currently-loaded skills, agents, and MCP servers** from `~/.claude/skill-manifest.json` with per-row **unload** buttons + a text-input to load a new skill slug |
| `/skills` | Every sidecar as a filterable **card grid**: left sidebar (search by slug, grade checkboxes, skill/agent toggle, hide-floored), card shows grade pill + raw score + links to sidecar/wiki/graph |
| `/skill/<slug>` | Full sidecar breakdown: four-signal score (telemetry · intake · graph · routing), hard-floor reason, computed_at timestamp, per-skill audit timeline |
| `/wiki` | **Wiki entity index** — card grid of every dashboard-supported page under `~/.claude/skill-wiki/entities/{skills,agents,mcp-servers}/`, including sharded MCP server pages. Left sidebar: text search (slug · description · tag), skill/agent/MCP checkboxes. Harness pages are not indexed yet. |
| `/wiki/<slug>` | Dashboard-supported wiki entity page rendered: markdown body + full frontmatter table + grade banner + deep links to sidecar and graph-neighborhood views |
| `/graph` | **Graph explorer landing page** — node/edge count header, a "Popular seed slugs" block (18 highest-degree skill/agent/MCP entities as clickable chips), search box for any skill/agent/MCP slug, and the cytoscape canvas. Clicking a seed chip navigates to `/graph?slug=<slug>` |
| `/graph?slug=<slug>` | **Cytoscape-rendered** 1-hop neighborhood around the target skill/agent/MCP slug. Node colors: emerald=focus, indigo=skill, amber=agent, red diamond=MCP server. Edge width maps to shared-tag count. Tap any node → navigate to that entity's wiki page. Type and tag filters run client-side; no harness filter or styling exists yet. |
| `/kpi` | **KPI dashboard** — total entity count with subject breakdown, grade distribution pills, two-column tables for grade counts and lifecycle tiers (active · watch · demote · archive), hard-floor reasons with counts, **By category** table (count · avg score · A/B/C/D/F mix per category), **Top demotion candidates** (active/watch entries graded D or F, sorted by consecutive-D streak desc then score asc), and the **Archived** list. Same shape as `python -m kpi_dashboard render` but HTML |
| `/sessions` | Index of every session (audit + skill-events), first/last seen, counts of skills loaded/unloaded/agents/lifecycle transitions |
| `/session/<id>` | Per-session audit timeline showing the load → score_updated → unload triad with timestamps |
| `/logs` | Last 500 audit events in a filterable table (client-side filter on event name, subject, session id) |
| `/events` | Live SSE stream of new audit events |

### JSON API

| Route | Returns |
|---|---|
| `GET /api/sessions.json` | All sessions with aggregated counts |
| `GET /api/manifest.json` | Raw `skill-manifest.json` passthrough |
| `GET /api/skill/<slug>.json` | Raw sidecar for one slug |
| `GET /api/graph/<slug>.json?hops=1&limit=40` | Dashboard-shaped skill/agent/MCP `{nodes, edges, center}`; `hops` ∈ [1, 3], `limit` ∈ [5, 150]. Harness graph exposure is not yet present here. |
| `GET /api/kpi.json` | `DashboardSummary` passthrough — `{total, by_subject, grade_counts, lifecycle_counts, category_breakdown, hard_floor_counts, low_quality_candidates, archived, generated_at}`. Returns `{total: 0, detail: "no sidecars yet"}` when the quality directory is empty |
| `GET /api/events.stream` | Server-sent events tail of `~/.claude/ctx-audit.jsonl` |

### Mutation endpoints

Both POST endpoints enforce same-origin (browser tab open on another
origin can't forge a request), require the per-process
`X-CTX-Monitor-Token` injected into the dashboard page, and reject any
slug failing the shared safe-name validator. That validator blocks path
separators, Windows drive-relative strings, malformed names, and Windows
reserved device names such as `con.txt` and `nul.`. There is no harness
load/unload mutation endpoint yet.

| Route | Body | Calls |
|---|---|---|
| `POST /api/load` | `{"slug": "..."}` | `skill_loader.load_skill(slug)` |
| `POST /api/unload` | `{"slug": "...", "entity_type": "skill"}` | `skill_unload.unload_from_session([slug])` |
| `POST /api/unload` | `{"slug": "...", "entity_type": "mcp-server"}` | `mcp_install.uninstall_mcp(slug, force=True)` |

Both emit a matching `skill.loaded` / `skill.unloaded` audit row
with `actor=user, meta.via="ctx-monitor"` so the dashboard-driven
action is visible in the session timeline.

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
| **Wiki entities** | Count of dashboard-supported wiki pages (skills + agents + MCP servers; no harness pages yet) |
| **Knowledge graph** | Dashboard-supported skill/agent/MCP node count + edge count from `graphify-out/graph.json` |
| **Audit events** | Line count of `~/.claude/ctx-audit.jsonl` |
| **Sessions** | Unique session IDs seen across audit + events |
| **Grade pills** | A / B / C / D / F counts across all sidecars, colored |

### On `/skills`

Every card shows:

- **grade** — A / B / C / D / F pill (A=green, F=red)
- **raw score** — float in [0, 1] before the hard-floor override
- **subject_type** — skill vs agent
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
  a skill, agent, or MCP slug to a file path (`/wiki/<slug>`,
  `/graph?slug=<slug>`, `/api/graph/<slug>.json`), the slug is
  validated through the shared
  safe-name helper — no path traversal, no absolute paths, no UNC
  shares, no Windows reserved device names.

## Stopping

Ctrl+C in the terminal. Request handling is threaded for local dashboard
responsiveness, and shutdown signals any open SSE workers. The monitor is
still not suitable for shared/production serving.
