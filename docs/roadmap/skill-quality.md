# Skill Quality — Plan

Living plan for the **skill quality scoring + lifecycle** initiative.
Keep this file terse and current. Detailed work lives in GH issues linked per phase.

## Vision

Every skill and agent in `~/.claude/skills` and `~/.claude/agents` carries a
continuous **quality score** that updates automatically as signals change.
Low-scoring entries are proposed for demotion, archival, or deletion via a
propose-and-confirm CLI. High-scoring entries earn priority in the router.
The goal: keep the corpus from decaying as it grows past 1,700 skills,
without asking the user to curate by hand.

## Principles

- **Evidence-weighted, not opinion-driven.** Score = weighted sum of four
  measurable signals. No manual grades.
- **Propose, don't mutate.** The lifecycle CLI prints proposed actions and
  waits for confirmation. `--auto` unlocks the non-destructive tiers; the
  **Delete** tier always requires human confirmation regardless of `--auto`.
- **Live on the knowledge graph.** The score is a node attribute on the
  wiki graph that changes in real time, not a static frontmatter number.
  Frontmatter + sidecar JSON + wiki page mirror it for auditability.
- **Asymmetric lifecycle.** Demotion is automatic; promotion back is
  deliberate, through a dedicated `--review-archived` CLI.
- **Config-driven thresholds.** Weights, score cutoffs, lifecycle cadence
  — all live under `config.json` so advanced users can tune without code
  changes.

## Signals (Q1)

| Signal | Source | Range | Intent |
|---|---|---|---|
| **telemetry** | `~/.claude/intent-log.jsonl` + `skill-manifest.json` | 0–1 | Did the skill get loaded? Used after load? How recently? |
| **intake warnings** | `IntakeDecision.findings` captured at install | 0–1 | Was this flagged as near-dup / thin body / orphan at install? |
| **graph connectivity** | `graph/wiki-graph.tar.gz` edges | 0–1 | How many incoming + outgoing wiki-links? Isolated nodes score low. |
| **routing hit rate** | router trace logs | 0–1 | When the router considered this skill, did it pick it? |

## Score formula (Q2)

```
raw = w_t * telemetry + w_i * intake + w_g * graph + w_r * routing
score = clamp(raw, 0, 1) — then apply hard floors

grade = A  if score >= 0.80
        B  if score >= 0.60
        C  if score >= 0.40
        D  otherwise

# Hard floors override the weighted score:
#   intake_fail → F (blocked at install; should not exist in corpus)
#   never_loaded AND age > stale_threshold_sessions → D regardless of graph/intake
```

Default weights: `w_t=0.40`, `w_i=0.20`, `w_g=0.25`, `w_r=0.15`.
All exposed under `config.json::quality`.

## Persistence (Q3)

Write score to **four sinks** on every compute:

1. **Knowledge-graph node attribute** — live, re-renders on wiki refresh.
   This is the source of truth; everything else is a mirror.
2. **Frontmatter** — `quality_score`, `quality_grade`, `quality_updated_at`
   on the converted wiki page.
3. **Sidecar JSON** — `~/.claude/skill-quality/<slug>.json` for machine
   consumption without parsing frontmatter.
4. **Wiki page** — a `## Quality` section with the current grade + signal
   breakdown for human auditability.

## Compute cadence (Q4)

All three triggers, each independently toggleable in `config.json::quality`:

- **Stop-hook piggyback** — on session end, recompute for skills touched
  during the session. Cheap, incremental, keeps the score fresh.
- **CLI** — `python src/skill_quality.py recompute [--all | --slug <name>]`
  for on-demand recomputation.
- **Cron / scheduled** — optional daily full-corpus recompute for
  telemetry drift. Off by default; opt-in via `quality.cron.enabled`.

## Action policy (Q5)

Every lifecycle action goes through **propose-and-confirm**:

```
$ ctx-lifecycle review
3 skills eligible for demotion (C → D):
  - old-fastapi-patterns: score 0.42, stale 45 sessions
  - ...
Proceed? [y/N]
```

- `--auto` flag promotes to auto-apply for Watch and Demote tiers only.
- Archive and Delete always require explicit confirmation.
- Delete additionally prints a **big warning + diff preview** and requires
  typing the skill name to confirm. No `--auto` override.

## Four-tier lifecycle (Q6)

| Tier | Entry condition | Action | Reversible? |
|---|---|---|---|
| **Watch** | grade drops to C | tag in frontmatter, surface in next review | yes, automatic |
| **Demote** | grade drops to D for 2+ consecutive recomputes | move from `skills/` to `skills/_demoted/` (router excludes) | yes, via review |
| **Archive** | demoted > archive_threshold_days | move to `skills/_archive/`, remove from graph | yes, via `--review-archived` |
| **Delete** | archived > delete_threshold_days AND `ctx-lifecycle purge` invoked | permanent delete after typed confirmation | NO |

Transitions happen at review time, never silently. Cadence + thresholds
in `config.json::quality.lifecycle`.

## Promotion back (Q7)

Asymmetric by design — automatic demotion, deliberate promotion:

- `ctx-lifecycle --review-archived` — prints archived skills with their
  last score and a preview of what changed since archival (git diff
  against the archive point).
- Promotion restores to `skills/` and recomputes score immediately.
- No automatic promotion from Archive; the user must invoke the CLI.

## KPI categories (Q8)

Skills/agents carry both:

- **Tags** (free-form, existing): `python`, `testing`, `aws`, ...
- **Category** (new, closed set): one of `framework`, `language`, `tool`,
  `pattern`, `workflow`, `meta`.

Dashboard shows:

- Score distribution by category (are `framework` skills healthier than
  `pattern` skills?)
- Trend: percentage of corpus in each grade over time
- Top-10 demotion candidates
- Archived-but-restorable count (leading indicator of user regret)

## Phases

### Phase 3 — Post-install scoring module ← next

Files: `src/skill_quality.py`, `src/quality_signals.py`,
`src/tests/test_skill_quality.py`, `src/config.json` (new `quality`
section), `src/ctx_config.py` (extend for `quality_*` fields).

- [ ] `quality_signals.py` — four signal extractors, each returning a
      normalized 0–1 float + raw evidence. Deterministic given inputs.
- [ ] `skill_quality.py` — score aggregation, hard floors, grade mapping,
      four-sink persistence. Includes CLI `recompute` / `show` / `explain`.
- [ ] Stop-hook integration — recompute only for skills touched this
      session (incremental path).
- [ ] Knowledge-graph node attribute writer — extends `graph/build_wiki_graph.py`
      to emit `quality_score`, `quality_grade` on each skill node.
- [ ] `config.json::quality` section with weights, thresholds, cadence toggles.
- [ ] Tests: signal extractors are deterministic; hard floors fire correctly;
      stop-hook incremental path matches full recompute on touched slugs.

### Phase 4 — Lifecycle CLI + KPI dashboard

Files: `src/ctx_lifecycle.py`, `src/kpi_dashboard.py`,
`src/tests/test_ctx_lifecycle.py`, `docs/quality/dashboard.md`.

- [ ] `ctx_lifecycle.py` — `review`, `demote`, `archive`, `purge`,
      `--review-archived`, `--auto`. Propose-and-confirm on every action;
      typed confirmation for purge.
- [ ] Tier transitions: Watch → Demote → Archive → Delete, reading
      `config.json::quality.lifecycle` thresholds.
- [ ] `kpi_dashboard.py` — markdown report generator with distribution
      tables and trend charts (sparkline per category).
- [ ] Add `category:` field to skill frontmatter schema + backfill script
      that infers category from existing tags where unambiguous.
- [ ] Archive recovery preview: git-diff between archive point and HEAD
      for the archived skill's source file.

### Phase 5 — Agent parity

Files: `src/skill_telemetry.py` (add subject_type discriminator),
`src/usage_tracker.py` (extend to agent pages), `src/agent_quality.py`.

- [ ] `skill_telemetry.py` — subject-type discriminator so the same
      module tracks both skills and agents without collision.
- [ ] `usage_tracker.py` — extend to agent entity pages in the wiki.
- [ ] Short interview to confirm agent-specific signal weights (likely
      different — agents are invoked less often but more deliberately).
- [ ] Reuse Phase 3 + 4 machinery for agents; single quality backbone.

## Open decisions (captured during interview)

- Signals: telemetry + intake warnings + graph connectivity + routing hit rate.
- Scoring: weighted sum + hard floors → A/B/C/D grade.
- Persistence: frontmatter + sidecar JSON + wiki page + **live KG node attribute**.
- Cadence: Stop-hook + CLI + cron, all config-toggleable.
- Policy: propose-and-confirm with `--auto` for Watch/Demote only; Delete always human-gated.
- Lifecycle: four tiers Watch → Demote → Archive → Delete.
- Promotion: asymmetric; dedicated `--review-archived` CLI.
- KPIs: both `tags:` (free-form) and `category:` (closed set).
- Order: M2.10 (done) → this plan doc (done) → Phase 3 → Phase 4 → agents.

## Out of scope (v1)

- Cross-machine score sync (local-only).
- ML-based scoring (start deterministic; revisit if signals prove noisy).
- Community quality leaderboard.
- Rewriting / auto-fixing low-scoring skills (we only demote/archive).

## Tracking

GH issues live under the `skill-quality-v1` milestone. One issue per phase.
This file is updated each time a phase completes.
