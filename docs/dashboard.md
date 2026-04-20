# Dashboard (`ctx-monitor`)

Local HTTP dashboard for every live observable in ctx: currently-
loaded skills, session timelines, the knowledge graph, the LLM-wiki
browser, quality grades + scores, filterable audit logs, and a live
event stream.

```bash
ctx-monitor serve              # http://127.0.0.1:8765
ctx-monitor serve --port 8888  # custom port
ctx-monitor serve --host 0.0.0.0 --port 8888  # LAN-visible (explicit opt-in)
```

Zero Python dependencies added by the dashboard. Everything runs on
stdlib `http.server`. Cytoscape.js is loaded from a CDN on the
`/graph` route only.

## Routes

### HTML views

| Route | What it shows |
|---|---|
| `/` | Home: six stat cards (loaded, sidecars, wiki entities, graph nodes, audit events, sessions), grade distribution pills, recent sessions table, recent audit events |
| `/loaded` | **Currently-loaded skills** from `~/.claude/skill-manifest.json` with per-row **unload** buttons + a text-input to load a new slug |
| `/skills` | Every sidecar as a filterable **card grid**: left sidebar (search by slug, grade checkboxes, skill/agent toggle, hide-floored), card shows grade pill + raw score + links to sidecar/wiki/graph |
| `/skill/<slug>` | Full sidecar breakdown: four-signal score (telemetry · intake · graph · routing), hard-floor reason, computed_at timestamp, per-skill audit timeline |
| `/wiki/<slug>` | Wiki entity page rendered: markdown body + full frontmatter table + grade banner + deep links to sidecar and graph-neighborhood views |
| `/graph?slug=<slug>` | **Cytoscape-rendered** 1-hop neighborhood around the target slug. Node colors: emerald=focus, indigo=skill, amber=agent. Edge width maps to shared-tag count. Tap any node → navigate to that entity's wiki page. Toggle to filter agents only |
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
| `GET /api/graph/<slug>.json?hops=1&limit=40` | Cytoscape-shaped `{nodes, edges, center}`; `hops` ∈ [1, 3], `limit` ∈ [5, 150] |
| `GET /api/events.stream` | Server-sent events tail of `~/.claude/ctx-audit.jsonl` |

### Mutation endpoints

Both POST endpoints enforce same-origin (browser tab open on another
origin can't forge a request) and reject any slug failing
`^[a-z0-9][a-z0-9_.-]{0,127}$`.

| Route | Body | Calls |
|---|---|---|
| `POST /api/load` | `{"slug": "..."}` | `skill_loader.load_skill(slug)` |
| `POST /api/unload` | `{"slug": "..."}` | `skill_unload.unload_from_session([slug])` |

Both emit a matching `skill.loaded` / `skill.unloaded` audit row
with `actor=user, meta.via="ctx-monitor"` so the dashboard-driven
action is visible in the session timeline.

## KPIs, measures, scores

The dashboard surfaces every quality signal ctx computes. Nothing
is aggregated-only — you can always drill from a headline number to
the raw sidecar that produced it.

### On the home page

| Card | What it means |
|---|---|
| **Currently loaded** | Count of entries in `skill-manifest.json[load]`. Clicking the card drills to `/loaded` |
| **Sidecars** | Total sidecars in `~/.claude/skill-quality/` |
| **Wiki entities** | Count of wiki pages (skills + agents) |
| **Knowledge graph** | Node count + edge count from `graphify-out/graph.json` |
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
  a slug to a file path (`/wiki/<slug>`, `/graph?slug=<slug>`,
  `/api/graph/<slug>.json`), the slug is validated against
  `^[a-z0-9][a-z0-9_.-]{0,127}$` — no path traversal, no absolute
  paths, no UNC shares.

## Stopping

Ctrl+C in the terminal. The server is single-threaded (enough for
local dev); not suitable for shared/production serving.
