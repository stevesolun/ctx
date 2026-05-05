# Skill quality — install & operations

One page on running the quality scorer: install the Stop hook, seed the
sidecars, and verify the data flows into the wiki and the knowledge graph.

## What it does

Every installed skill and agent gets a continuous quality score in
`[0.0, 1.0]` plus an A/B/C/D/F letter grade, derived from four signals:

| Signal    | Weight | What it measures                                      |
| --------- | -----: | ----------------------------------------------------- |
| telemetry |   0.40 | Load count, recency, freshness in `skill-events.jsonl`|
| intake    |   0.20 | Live re-run of the six install-time structural checks |
| graph     |   0.25 | Degree + average edge weight in the wiki graph        |
| routing   |   0.15 | Router hit-rate (neutral prior below 3 observations)  |

Two hard floors override the weighted sum:

- **`intake_fail`** — any structural check is currently failing → grade **F**.
  Grade F is **only** produced by this hard floor; it is never returned by
  the score-to-grade mapping function alone.  A score of 0.0 maps to **D**.
- **`never_loaded_stale`** — no load events ever → grade capped at **D**.

The score is mirrored to three on-disk sinks so every consumer sees the
same number:

1. `~/.claude/skill-quality/<slug>.json` — canonical machine-readable form.
2. Wiki entity frontmatter — `quality_score`, `quality_grade`, `quality_updated_at`.
3. Wiki body — a `## Quality` block between `<!-- quality:begin -->` markers.

The knowledge-graph node attribute is a **separate consumer path**, not a
write path from `persist_quality`: `wiki_graphify` reads the sidecar JSON
produced by sink 1 and attaches `quality_score` / `quality_grade` to each
node on its next build.

## Register the Stop hook

The hook runs once per session-end. It reads `skill-events.jsonl` since
its last run, collects every slug that appeared, and calls
`skill_quality.py recompute --slugs <comma-list>` — so scoring is
incremental (touched skills only), not a full 2,000-page sweep.

Edit `~/.claude/settings.json` and add, replacing `<REPO>` with the
absolute path to this checkout (use forward slashes on Windows):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python <REPO>/hooks/quality_on_session_end.py"
          }
        ]
      }
    ]
  }
}
```

The hook always exits 0: a scoring error will not block session
shutdown.

## Seed the sidecars (first run only)

Run once after install so every installed skill has a baseline score:

```bash
ctx-skill-quality recompute --all
```

This walks `~/.claude/skills/*/SKILL.md` and `~/.claude/agents/*.md`,
scores each, and writes the three on-disk sinks. Expect ~15–30s depending on
corpus size and disk.

## CLI reference

```bash
# Full recompute (use sparingly; the Stop hook handles incrementals).
ctx-skill-quality recompute --all

# One slug.
ctx-skill-quality recompute --slug python-testing

# Show the most recent score.
ctx-skill-quality show python-testing

# Signal-by-signal breakdown with evidence.
ctx-skill-quality explain python-testing

# List every slug with its grade, filtered.
ctx-skill-quality list --grade D
```

All verbs accept `--json` for piping into other tools.

## Graph integration

`wiki_graphify.py` reads the sidecar directory automatically and
attaches `quality_score` and `quality_grade` to every matching node. The
Obsidian graph view can then color nodes by grade — configure the
`quality_grade` property in Obsidian's graph settings.

Nodes without a sidecar get `quality_score: null` and `quality_grade:
null` so downstream consumers can always read the attribute safely.

## Configuration

All knobs live in `src/config.json` under the top-level `quality` key:

```json
{
  "quality": {
    "weights": {
      "telemetry": 0.40, "intake": 0.20, "graph": 0.25, "routing": 0.15
    },
    "agent_weights": {
      "telemetry": 0.15, "intake": 0.30, "graph": 0.35, "routing": 0.20
    },
    "grade_thresholds": {"A": 0.80, "B": 0.60, "C": 0.40},
    "stale_threshold_days": 30.0,
    "recent_window_days": 14.0,
    "min_body_chars": 120,
    "paths": {
      "sidecar_dir": "~/.claude/skill-quality",
      "router_trace": "~/.claude/router-trace.jsonl"
    }
  }
}
```

`ctx_config.Config` exposes this through `cfg.get("quality", {})`. User
overrides in `~/.claude/skill-system-config.json` deep-merge over the
repo defaults, so you can pin only the keys you want to change.

Both weight vectors must sum to 1.0 (±0.01) and grade thresholds must
satisfy `0 ≤ C ≤ B ≤ A ≤ 1` — `QualityConfig.__post_init__` will raise
on bad values, catching typos before they pollute sidecars.

### Why two weight vectors

Skills and agents differ in how they're invoked. Skills are loaded
automatically by the router, so **telemetry** (load counts, recency) is
the strongest post-install quality signal — hence 0.40 weight.

Agents are invoked via the Agent tool, deliberately and rarely. A
seldom-used agent isn't stale, it's specialized. The agent weights
shift mass onto **graph connectedness** (0.35) and **intake structure**
(0.30) so agents aren't penalized for having an empty telemetry stream.
The `never_loaded_stale` hard floor, which caps skills at D when they
have zero load events, does **not** apply to agents for the same
reason.

## Troubleshooting

- **Every skill grades D.** Telemetry hasn't accumulated enough load
  events yet. This is expected on a fresh install; the stop-hook will
  pick up real usage over the next few sessions.
- **A recently-edited skill now grades F.** Open the sidecar and look
  at `signals.intake.evidence.checks` — one of the six structural
  checks is failing. Fix the file and rerun `recompute --slug <name>`.
- **Wiki page has two `## Quality` sections.** Shouldn't happen —
  `persist_quality` is idempotent via the HTML-comment markers. If it
  does, delete both blocks and rerun `recompute`; the first pass will
  re-emit exactly one.
- **Graph view shows no color.** Run `ctx-wiki-graphify
  --graph-only` to rebuild; it reads sidecars fresh on every build.
