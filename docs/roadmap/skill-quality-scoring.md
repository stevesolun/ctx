# Skill & Agent Quality Scoring — Plan

**Status:** PLAN. No code in this document. No files touched by this doc.
**Scope:** scoring, de-duplication, behavioural telemetry, auto-demote/archive, live dashboard.
**Author:** drafted 2026-04-18 in response to user request: *"not all skills/agent are built the same... eliminate the garbage."*

---

## 1. Problem statement

ctx currently has 1,725 skills, 443 agents, 2,168 graph nodes across 861 communities. The ingestion bar is **mechanical**: if a markdown file parses and passes `skill_health.py`'s drift check, it lands. Two blind spots follow:

1. **Intake blindness** — a new skill near-duplicate of an existing one is accepted and silently lowers retrieval quality (dilutes ranking, fragments backlinks, inflates graph noise).
2. **Outcome blindness** — once loaded, ctx does not measure whether a skill actually *helped*. A skill the user unloads, overrides, or ignores sits in the registry at equal rank to one that is genuinely useful.

The user's directive is to make both signals first-class: **score on intake, score on outcome, auto-demote the bottom of the distribution, show it all on a live local dashboard, and give the user a tailored recommender that proposes mute/archive/delete.**

This plan organises that work as six tracks. Each track has a verifiable deliverable. No track depends on externalising data — everything stays on the user's machine, git-backed, in line with ctx's single-user-local design.

---

## 2. Non-goals

- **Not** building a multi-tenant quality service. Single-user local only.
- **Not** auto-deleting anything without explicit user confirmation. Archive is reversible; delete is a CLI step the user runs on their own.
- **Not** assigning float confidence scores to individual claims inside a skill. Reject — see `llm-wiki-v2-analysis.md` §"Confidence scoring as a first-class field".
- **Not** a cross-user quality signal. Outside scope. If ever added, would be a new opt-in subsystem.

---

## 3. Track A — Intake similarity check (de-duplication on `skill_add`)

### Problem
`skill_add.py` and `batch_convert.py` accept new skills without asking *"do we already have this?"*. Duplicates dilute ranking, split backlinks, and are invisible in `skill-manifest.json` because names differ.

### Deliverable
`src/skill_similarity.py` — a module that, given a candidate SKILL.md body, returns the top-K nearest existing skills plus a decision hint (`distinct`, `near-duplicate`, `duplicate`).

### Approach
- **Embedding:** reuse whatever local embedding model ctx already runs (`ollama` or `sentence-transformers/all-MiniLM-L6-v2` if none). Single-batch, 384-dim, cached under `~/.claude/skills/_embeddings/<skill-id>.npy`.
- **Index:** in-memory `numpy.ndarray` stack plus a sidecar `skill_embedding_index.json` mapping row → skill-id. Rebuild on demand; no external vector DB.
- **Distance:** cosine. Thresholds (draft, tunable from CLI flag):
  - ≥ 0.93 → `duplicate` — block ingestion; CLI exits with the duplicate's name
  - 0.85 – 0.93 → `near-duplicate` — require `--force` or interactive confirm; emit diff of the two frontmatter blocks
  - < 0.85 → `distinct` — proceed
- **Signals beyond embedding:** also compare normalised `description`, `tags`, top-level H2 headings. A skill with the same description and tags but different body is still a duplicate.

### Integration point
`skill_add.add_skill()` runs similarity check **before** disk write. On `near-duplicate` without `--force`, the candidate is enqueued in `pending-skills.json` with reason `similar_to:<existing-id>` so the user reviews and resolves it, not the LLM.

### Verification
- Golden-set regression test under `tests/fixtures/similarity/`:
  - 30 pairs known-similar, 30 pairs known-distinct, plus 10 adversarial near-misses.
  - CI asserts precision ≥ 0.9, recall ≥ 0.9 on the similar set.
- Rebuilding the index over all existing skills must finish under 60 s on a laptop and must not allocate > 500 MB RSS.

---

## 4. Track B — Per-skill / per-agent KPIs and telemetry

### Problem
We do not measure what happens to a skill after it is suggested, loaded, or invoked. Without per-skill outcome data, the scoring model has nothing to score on.

### Deliverable
`~/.claude/skills/_usage.jsonl` — an append-only event log, one line per event. Backed by `src/skill_telemetry.py` for writes and `src/skill_metrics.py` for reads.

### Event schema
Each line is a single JSON object. All timestamps are UTC ISO-8601. All IDs are stable skill/agent slugs.

```
{
  "ts": "2026-04-18T12:34:56.789Z",
  "session_id": "<uuid>",
  "kind": "suggested | loaded | unloaded | invoked | overridden | switched_from | switched_to | failed | approved_finding | rejected_finding",
  "subject_type": "skill | agent",
  "subject_id": "python-patterns",
  "context": {
    "trigger": "auto | slash | hook",
    "stack_hash": "<sha256 of detected stack>",
    "prev_subject_id": "…"   // for switched_*
  }
}
```

### KPIs derived from the event log

| KPI | Formula | Why it matters |
|---|---|---|
| `invocation_count` | count(`invoked`) over last 90 days | baseline attention |
| `acceptance_rate` | `loaded` / `suggested` | does the suggestion survive first contact |
| `retention_rate` | 1 − (`unloaded` within 5 min of `loaded` / `loaded`) | do users immediately reject it |
| `override_rate` | `overridden` / `loaded` | user ignored the skill's advice |
| `switch_away_rate` | `switched_from[this]` / `loaded` | active replacement = strong negative signal |
| `co_occurrence_lift` | `P(skill B loaded | skill A loaded)` / `P(skill B loaded)` | fuels "users who loaded A also loaded B" |
| `finding_precision` (agents only) | `approved_finding` / (`approved_finding` + `rejected_finding`) | does the agent produce signal or noise |
| `time_to_unload_p50` | median seconds between `loaded` and `unloaded` | velocity of disengagement |

All KPIs are **rolling-window** (7 / 30 / 90 day). The event log is truncated by `src/skill_telemetry.py --compact` keeping 90 days + per-skill aggregate rollups.

### Hooks required
- `skill_loader.load()` emits `loaded`; `skill_loader.unload()` emits `unloaded`.
- `resolve_skills.rank()` emits `suggested` for every item in its top-K.
- `skill_suggest.py` interactive switch path emits `switched_from` + `switched_to`.
- `toolbox_verdict.record_outcome()` emits `approved_finding` / `rejected_finding` keyed to the agent that produced the finding.

### Privacy boundary
Event log lives under `~/.claude/` only. Never shipped, never synced. User can `rm -rf` it without consequence beyond losing scoring history.

---

## 5. Track C — Blended scoring model

### Problem
Telemetry alone is noisy (small-sample bias, recency bias). Technical-health alone is not predictive (a lint-clean skill can still be useless). Need a blended score that survives both.

### Deliverable
`src/skill_score.py` — pure function `score(subject) → ScoreBundle(technical, behavioural, blended, decay, rationale)`.

### Composition

```
blended = 0.35 * technical + 0.55 * behavioural + 0.10 * recency

technical = weighted_mean({
  wikilink_resolve_rate: 0.20,    # Track C.1 from llm-wiki-v2-analysis.md
  bash_block_parse_rate: 0.15,
  file_path_resolve_rate: 0.15,
  frontmatter_valid: 0.15,
  graph_connectivity (log(edges+1) capped): 0.15,
  drift_free (from skill_health): 0.20,
})

behavioural = Bayesian-smoothed weighted_mean({
  acceptance_rate: 0.25,
  retention_rate: 0.25,
  finding_precision (agents only): 0.20,
  1 − override_rate: 0.15,
  1 − switch_away_rate: 0.15,
})

recency = exp(−days_since_last_invocation / 30)
```

- **Bayesian smoothing:** every rate is computed as `(successes + α) / (total + α + β)` with `α = β = 5` to stop a fresh skill with 1 of 1 loads from scoring 1.0.
- **Calibration set:** 20 hand-labelled skills (10 known-good, 10 known-bad) live under `tests/fixtures/scoring/` and drive a regression test. Target: Spearman ρ ≥ 0.7 between label and `blended`.

### Surfacing
- `ranker_cli score <skill>` prints the full bundle + rationale.
- Every `suggested` event includes the current blended score so ranking is auditable after the fact.
- `skill_health.py --scores` adds a column per skill.

---

## 6. Track D — Auto-demote, archive, and tailored recommender

### Problem
User's ask: *"I am using skill A and it's complete shit... I am switching it or our recommendation system detects it and switches."* The scoring model must translate into action.

### State machine

```
active ──(score < 0.35 for ≥ 3 sessions) ──► demotion_candidate
active ──(switch_away_rate > 0.6 within 7d) ──► demotion_candidate

demotion_candidate ──(user approves) ──► muted
demotion_candidate ──(score recovers > 0.55) ──► active
demotion_candidate ──(stale > 30d) ──► archive_candidate

muted  ──(user approves) ──► archived
archived ──(user runs `skill_registry --restore`) ──► active
archived ──(user runs `skill_registry --delete`) ──► deleted  (snapshot taken first)
```

- **`muted`:** excluded from `resolve_skills.rank()` but SKILL.md stays in place. Reversible with zero I/O.
- **`archived`:** moved to `~/.claude/skills/_archive/<date>/<skill-id>/`. Excluded from graph rebuilds. `backup_mirror.py create` runs first automatically so the user has one-command restore.
- **`deleted`:** only reachable from `archived`, only via explicit CLI, only after a fresh snapshot. ctx itself never deletes.

### Tailored recommender (per-user)

A generic "this skill is bad" gate is a blunt instrument. The user wants tailored suggestions — different users extract different value from the same skill.

- `src/skill_recommender.py` builds a **personal baseline** from the last 90 days of events: stack signatures, accepted skills, rejected skills, co-occurrence clusters the user prefers.
- When a skill's blended score drops but the personal baseline shows the user *does* use it, the recommender **suppresses the auto-demote** and surfaces the skill as `user_specific_keeper` on the dashboard.
- When a skill scores fine globally but the user personally always switches away from it, the recommender proposes muting only for this user — stored as `~/.claude/skills/_overrides.json`, not in the shared registry.

### Invariants
- No skill is ever deleted without a prior snapshot. Enforced by `skill_registry.delete()` calling `backup_mirror.create_snapshot()` and verifying the manifest before the `rmtree`.
- Every transition writes a line to `~/.claude/skills/_transitions.jsonl` so the history is auditable and reversible.
- The user can disable the whole system with `~/.claude/settings.json` → `"skill_scoring": "off"` and ctx behaves exactly as it does today.

---

## 7. Track E — Live local dashboard

### Problem
A JSONL event log and a CLI are not enough. The user wants a live view of usage, the knowledge graph, the LLM wiki, and performance. Local only, realtime, on the user's machine.

### Deliverable
`src/dashboard/` — a FastAPI + server-sent-events backend plus a single-file HTML/JS frontend.

- **Launch:** `ctx dashboard` starts `uvicorn` on `127.0.0.1:<port>` bound to loopback, opens the browser. No auth because bind is loopback; refuse to start if `--host` is anything else.
- **Live data source:** the process tails `~/.claude/skills/_usage.jsonl` with `watchdog` and pushes deltas via SSE. No polling.
- **Sections:**
  1. **Usage** — top-N skills / agents by invocation, retention, switch-away; sparklines for the last 24 h / 7 d; red-flag list of `demotion_candidate` entries with one-click "mute" / "archive" buttons that POST to the local API.
  2. **Knowledge graph** — pre-rendered static graph from `wiki_visualize.py` + a live overlay of nodes touched in the current session (highlighted). Clicking a node opens the SKILL.md in the user's editor via a `vscode://` URL.
  3. **LLM wiki** — table of wiki pages with freshness (`git log -1`), backlink count, orphan flag, drift status.
  4. **Performance** — `ranker_cli` p50/p95/p99 latency over the last 1k calls; memory RSS of long-running `batch_convert` runs; `skill_health` last-run timestamp and red/green.
- **Stop condition:** CLI `ctx dashboard --stop` cleanly terminates; `Ctrl-C` also works because it's a foreground uvicorn by default.

### Security
- Bind to `127.0.0.1` only. Refuse any other `--host`.
- All mutating endpoints (mute, archive, restore) require a one-time CSRF token written to `~/.claude/dashboard.token` on startup and posted as a header. Prevents a malicious local page from CSRF-ing the dashboard.
- No analytics, no outbound HTTP, no CDN assets — all JS/CSS served from the wheel.

---

## 8. Track F — Eliminate existing "garbage" (one-time sweep)

Run the full pipeline retroactively once Tracks A–D land:

1. `skill_similarity --rebuild-index` over current 1,725 skills, report clusters at ≥ 0.93 cosine.
2. `skill_score --all` blended scores; write `docs/reports/skill_scores_baseline.csv`.
3. Present the bottom 10% as `demotion_candidate`, sorted by score × invocation_count (descending — noisy + rarely used first).
4. User reviews the list on the dashboard and confirms per-skill: mute, archive, or keep.
5. Snapshot before any archive/delete. No automatic removal.

Expected outcome (rough): some single-digit percent of the registry gets archived; another chunk gets merged into existing skills once the similarity report exposes duplicates.

---

## 9. Dependencies and sequencing

```
Track B (telemetry events)  ──┐
                              ├──► Track C (scoring)  ──┐
Track A (similarity)          │                         ├──► Track D (auto-demote + recommender)
                              │                         │
                              └──► Track E (dashboard) ◄┘
                                                         │
                                                         ▼
                                                   Track F (one-time sweep)
```

- **Phase 1 (lowest risk, highest info):** Track B. No behaviour change, just logging. Ship first.
- **Phase 2:** Track A. Gates intake, blocks new duplicates immediately.
- **Phase 3:** Track C. Read-only scores, surfaced in CLI. Still no enforcement.
- **Phase 4:** Track E. Dashboard reads everything Phase 1–3 produced.
- **Phase 5:** Track D. Only after scoring is calibrated and dashboard is live — otherwise auto-demote will misfire on bad data.
- **Phase 6:** Track F. One-time sweep using the now-calibrated machinery.

Each phase is independently shippable, independently reversible, and each one delivers user-visible value even if the subsequent phases never land.

---

## 10. Success criteria

- **Intake:** no two skills with cosine ≥ 0.93 can coexist in the registry after Phase 2.
- **Measurement:** every `suggested` → `loaded` → `unloaded` chain appears in `_usage.jsonl` within 1 s of the event.
- **Scoring:** Spearman ρ ≥ 0.7 between the calibration labels and `blended` on the 20-skill golden set.
- **Auto-demote:** zero false-positive demotions on the calibration set; zero deletions without a preceding snapshot (enforced by assertion).
- **Dashboard:** cold start to first render ≤ 2 s on a laptop; SSE latency for a new event ≤ 500 ms.
- **Sweep:** after Phase 6, the registry's mean blended score rises by ≥ 10 points with no loss of skills the user actually invoked in the last 90 days.

---

## 11. Explicit non-commitments

- **Not** pinning any LLM for the embedding backend. Whatever ctx already has.
- **Not** adopting a graph-DB. In-memory numpy is fast enough for 2k nodes.
- **Not** committing to a timeline. Plan is ordered by dependency, not calendar.
- **Not** surfacing scores to any agent's context window — scores inform *ranking*, not the skill's own text. Prevents prompt-injection-style gaming of the score.

---

## 12. Open questions (for the user, before Phase 1)

1. **Embedding backend.** Ollama (`nomic-embed-text`) if already running, else sentence-transformers. Preference?
2. **Dashboard port.** Pick a default (e.g., `17845` — unregistered in IANA) or let the OS assign?
3. **Thresholds.** Are `0.93 / 0.85` for similarity and `0.35 / 0.55` for demotion in the right neighbourhood, or should Phase 2 ship with a calibration pass that sets them from the current registry's distribution?
4. **Archive location.** `~/.claude/skills/_archive/` keeps it local. Alternative: per-repo `.ctx/skills_archive/` so the archive travels with a project. The user's single-user model suggests the former.

Answering these unlocks Phase 1 implementation.
