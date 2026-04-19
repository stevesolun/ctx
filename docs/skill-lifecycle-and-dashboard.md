# Skill lifecycle & KPI dashboard — install & operations

One page on running the Phase 4 lifecycle CLI, the category backfill,
and the KPI dashboard. Prerequisite: Phase 3 scorer is installed and
has written at least one sidecar. See
[skill-quality-install.md](./skill-quality-install.md).

## What it does

After the scorer has labeled everything, these three tools turn the
labels into action:

| Tool | CLI | Purpose |
| ---- | --- | ------- |
| `ctx_lifecycle.py`  | `review`, `demote`, `archive`, `purge`, `review-archived` | Move D/F-grade skills through `active → watch → demote → archive → deleted`. |
| `skill_category.py` | `backfill`, `infer`                                       | Write the closed-set `category:` field into skill/agent frontmatter. |
| `kpi_dashboard.py`  | `render`, `summary`                                       | Emit a single Markdown dashboard joined across all quality sinks. |

Asymmetric gates: downward transitions are automatic from a D-streak;
upward transitions are deliberate. Archive needs aging (`active` for
14 days in `_demoted`); delete needs a typed-slug confirmation even
under `--auto`.

## Category taxonomy

Closed set: `framework`, `language`, `tool`, `pattern`, `workflow`,
`meta`. The dashboard groups scores by category so you can see, e.g.,
that your `framework`-tagged skills average a B but your
`workflow`-tagged skills are mostly D — which tells you where to focus
curation.

Inference is precedence-ordered: `python + django` → `language`
(language wins over framework). The backfill **never overwrites** an
existing non-empty value — human edits win.

Run once after the scorer has seeded the sidecars:

```bash
python src/skill_category.py backfill --dry-run
python src/skill_category.py backfill            # apply
```

Unresolved slugs (no tag matched the taxonomy) are listed for manual
curation.

## Lifecycle CLI

Four verbs. All are propose-and-confirm by default; `--auto` unlocks
only the safe tiers (Watch + Demote).

```bash
# List every pending transition; no writes.
python src/ctx_lifecycle.py review --dry-run

# Apply all Watch/Demote transitions without prompting.
python src/ctx_lifecycle.py review --auto

# Archive a specific slug. Requires the demoted aging threshold to have passed.
python src/ctx_lifecycle.py archive <slug>

# Delete archived slugs that exceeded the delete threshold.
# Requires typed-slug confirmation per entry even with --auto.
python src/ctx_lifecycle.py purge

# List archived slugs with optional diffs, or restore one.
python src/ctx_lifecycle.py review-archived --show-diff
python src/ctx_lifecycle.py review-archived --restore <slug>
```

Filesystem moves: demote → `<skills_dir>/_demoted/<slug>/`, archive →
`<skills_dir>/_archive/<slug>/`. The scanner skips directories starting
with `_`, so demoted/archived skills no longer show up to the router.

Lifecycle state is persisted in a sibling sidecar at
`~/.claude/skill-quality/<slug>.lifecycle.json`. Each transition is
folded into the `history` array (capped at `history_max`) so you can
audit how a slug ended up where it did.

### The D-streak

A skill needs `consecutive_d_to_demote` consecutive D-or-F grades to
trigger a demote proposal. Any A/B/C grade resets the streak to 0. The
default is 2 — one bad session is a blip, two in a row is a trend.

## KPI dashboard

Pure read-only. Walks both quality sidecars and lifecycle sidecars,
joins them against `category:` in frontmatter (with inference
fallback), and emits Markdown or JSON.

```bash
# Dump Markdown to stdout.
python src/kpi_dashboard.py render

# Persist to a file — good target for the cron / pre-push hook.
python src/kpi_dashboard.py render --out ~/.claude/skill-quality/kpi.md

# Machine-readable.
python src/kpi_dashboard.py render --json --out kpi.json

# Terse one-screen summary.
python src/kpi_dashboard.py summary
```

### What's in the report

- **Grade distribution** — A/B/C/D/F counts + percentages. Blank grade
  (no score yet) rolls up to F so it surfaces in the "needs
  attention" bucket.
- **Lifecycle tiers** — counts across active/watch/demote/archive.
- **Hard floors active** — how many slugs are failing intake or
  never-loaded-stale.
- **By category** — per-category count, average score of
  scored entries, and a mini A/B/C/D/F mix table.
- **Top demotion candidates** — up to `--limit N` (default 10) active
  or watch-tier entries sorted by (D-streak desc, score asc). These
  are the first slugs `ctx_lifecycle review --auto` will act on.
- **Archived (restorable)** — every slug currently in the archive
  tier; still recoverable via `review-archived --restore <slug>` until
  `purge` deletes them.

## Configuration

All knobs live under `quality` in `src/config.json`:

```json
{
  "quality": {
    "lifecycle": {
      "archive_threshold_days": 14.0,
      "delete_threshold_days": 60.0,
      "consecutive_d_to_demote": 2,
      "demoted_subdir": "_demoted",
      "archive_subdir": "_archive",
      "history_max": 20
    },
    "dashboard": {
      "default_top_n": 10,
      "report_path": "~/.claude/skill-quality/kpi.md"
    }
  }
}
```

Tighten `consecutive_d_to_demote` to 1 for aggressive pruning, or
loosen `archive_threshold_days` if you want a longer grace window
before a demoted skill gets archived.

## Operational cadence

A reasonable default rhythm, given the defaults above:

- **Every session** — scorer hook runs automatically on session end.
- **Weekly** — `ctx_lifecycle review --auto` to sweep Watch/Demote.
- **Weekly** — `kpi_dashboard render --out …/kpi.md` for the digest.
- **Monthly** — `ctx_lifecycle review` (no `--auto`) to surface
  archive-ready demoted skills for manual approval.
- **Quarterly** — `ctx_lifecycle purge` with typed-slug confirmation
  to actually remove archived skills past the delete threshold.

## Troubleshooting

- **"unresolved" in backfill output** — the skill's tags don't match
  the taxonomy. Either add a matching tag (e.g. `python`) or set
  `category:` manually in frontmatter.
- **Dashboard shows skills as F with no score** — they have a
  lifecycle sidecar but no quality sidecar. That's intentional:
  archived slugs whose quality sidecar was cleaned up still appear
  in the tier and archive sections so you can restore them.
- **`review --auto` refuses to archive or delete** — by design. Those
  tiers require human approval.
