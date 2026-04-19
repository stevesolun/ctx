#!/usr/bin/env python3
"""
kpi_dashboard.py -- Skill-quality KPI report generator.

Phase 4 of the skill-quality plan. Reads the persistence sinks the
scorer and lifecycle already write and emits a single Markdown digest
the user can commit, share, or watch in a file viewer:

  - ``~/.claude/skill-quality/<slug>.json``          (quality scores)
  - ``~/.claude/skill-quality/<slug>.lifecycle.json`` (lifecycle tier)
  - ``<skills_dir>/<slug>/SKILL.md``                 (category frontmatter)
  - ``<agents_dir>/<slug>.md``                       (category frontmatter)

Design notes:

  - Pure read-only. Never mutates sidecars or skill files.
  - All aggregation happens in pure functions returning dataclasses so
    the CLI output, JSON output, and tests see the same shape.
  - Missing category falls back to ``skill_category.infer_category`` on
    the skill's tags — keeps the report useful before backfill has run.
  - Archive candidates still appear in the report even when their
    quality sidecar was removed, because the lifecycle sidecar is the
    authoritative record for non-active tiers.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ctx_lifecycle import (
    LifecycleSources,
    STATE_ACTIVE,
    STATE_ARCHIVE,
    STATE_DEMOTE,
    STATE_WATCH,
    load_lifecycle_state,
)
from skill_category import CATEGORIES, infer_category, read_existing_category
from skill_quality import QualityScore, load_quality
from wiki_utils import parse_frontmatter_and_body

_logger = logging.getLogger(__name__)

_GRADES: tuple[str, ...] = ("A", "B", "C", "D", "F")
_UNCATEGORIZED = "uncategorized"
_LIFECYCLE_STATES: tuple[str, ...] = (
    STATE_ACTIVE, STATE_WATCH, STATE_DEMOTE, STATE_ARCHIVE,
)


# ────────────────────────────────────────────────────────────────────
# Aggregation types
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityRow:
    """One slug's dashboard-relevant facts, joined across sinks."""

    slug: str
    subject_type: str                     # "skill" | "agent"
    category: str                         # always a concrete string (never None)
    grade: str                            # "A"/"B"/"C"/"D"/"F" or "" if no score
    score: float                          # 0..1; 0.0 if no score
    hard_floor: str | None
    lifecycle_state: str                  # one of _LIFECYCLE_STATES
    consecutive_d_count: int
    computed_at: str                      # ISO-8601 or ""


@dataclass(frozen=True)
class DashboardSummary:
    """The full aggregation — serializable to JSON, renderable to Markdown."""

    generated_at: str
    total: int
    by_subject: dict[str, int] = field(default_factory=dict)
    grade_counts: dict[str, int] = field(default_factory=dict)
    lifecycle_counts: dict[str, int] = field(default_factory=dict)
    category_breakdown: list[dict[str, Any]] = field(default_factory=list)
    hard_floor_counts: dict[str, int] = field(default_factory=dict)
    low_quality_candidates: list[dict[str, Any]] = field(default_factory=list)
    archived: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total": self.total,
            "by_subject": dict(self.by_subject),
            "grade_counts": dict(self.grade_counts),
            "lifecycle_counts": dict(self.lifecycle_counts),
            "category_breakdown": [dict(c) for c in self.category_breakdown],
            "hard_floor_counts": dict(self.hard_floor_counts),
            "low_quality_candidates": [dict(c) for c in self.low_quality_candidates],
            "archived": [dict(a) for a in self.archived],
        }


# ────────────────────────────────────────────────────────────────────
# Category resolution
# ────────────────────────────────────────────────────────────────────


def _skill_source_path(slug: str, sources: LifecycleSources) -> Path | None:
    skill_path = sources.skills_dir / slug / "SKILL.md"
    if skill_path.is_file():
        return skill_path
    agent_path = sources.agents_dir / f"{slug}.md"
    if agent_path.is_file():
        return agent_path
    return None


def _resolve_category(slug: str, sources: LifecycleSources) -> str:
    """Read existing category, else infer from tags, else uncategorized."""
    path = _skill_source_path(slug, sources)
    if path is None:
        return _UNCATEGORIZED
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _UNCATEGORIZED
    existing = read_existing_category(raw)
    if existing in CATEGORIES:
        return existing
    fm, _ = parse_frontmatter_and_body(raw)
    tags_raw = fm.get("tags", []) if isinstance(fm, dict) else []
    if isinstance(tags_raw, list):
        tags: Iterable[str] = [t for t in tags_raw if isinstance(t, str)]
    elif isinstance(tags_raw, str):
        tags = [p.strip() for p in tags_raw.split(",") if p.strip()]
    else:
        tags = []
    inferred = infer_category(tags)
    return inferred or _UNCATEGORIZED


# ────────────────────────────────────────────────────────────────────
# Row building
# ────────────────────────────────────────────────────────────────────


def _iter_quality_slugs(sidecar_dir: Path) -> list[str]:
    if not sidecar_dir.is_dir():
        return []
    out: list[str] = []
    for path in sorted(sidecar_dir.glob("*.json")):
        name = path.name
        if name.endswith(".lifecycle.json"):
            continue
        # Skip internal state files (dotfiles like .hook-state.json) —
        # they share the sidecar directory but are not entity slugs and
        # fail the strict slug validator downstream.
        if name.startswith("."):
            continue
        out.append(path.stem)
    return out


def _iter_lifecycle_slugs(sidecar_dir: Path) -> list[str]:
    if not sidecar_dir.is_dir():
        return []
    suffix = ".lifecycle.json"
    return sorted(p.name[: -len(suffix)] for p in sidecar_dir.glob(f"*{suffix}"))


def _build_row(
    slug: str,
    *,
    score: QualityScore | None,
    lifecycle_state: str,
    consecutive_d_count: int,
    sources: LifecycleSources,
) -> EntityRow:
    subject = score.subject_type if score is not None else _guess_subject(slug, sources)
    return EntityRow(
        slug=slug,
        subject_type=subject,
        category=_resolve_category(slug, sources),
        grade=(score.grade if score is not None else ""),
        score=(score.score if score is not None else 0.0),
        hard_floor=(score.hard_floor if score is not None else None),
        lifecycle_state=lifecycle_state,
        consecutive_d_count=consecutive_d_count,
        computed_at=(score.computed_at if score is not None else ""),
    )


def _guess_subject(slug: str, sources: LifecycleSources) -> str:
    """Used only when no quality sidecar exists (archived-and-cleared case)."""
    if (sources.skills_dir / slug / "SKILL.md").is_file():
        return "skill"
    if (sources.agents_dir / f"{slug}.md").is_file():
        return "agent"
    return "skill"


def collect_rows(
    *, sources: LifecycleSources,
) -> list[EntityRow]:
    """Walk both sinks and return one row per known slug (union)."""
    quality_slugs = set(_iter_quality_slugs(sources.sidecar_dir))
    lifecycle_slugs = set(_iter_lifecycle_slugs(sources.sidecar_dir))
    all_slugs = sorted(quality_slugs | lifecycle_slugs)
    rows: list[EntityRow] = []
    for slug in all_slugs:
        try:
            score = load_quality(slug, sidecar_dir=sources.sidecar_dir)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            _logger.warning("kpi_dashboard: skipping %s: %s", slug, exc)
            score = None
        lc = load_lifecycle_state(slug, sidecar_dir=sources.sidecar_dir)
        if lc is not None:
            state = lc.state
            streak = lc.consecutive_d_count
        else:
            state = STATE_ACTIVE
            streak = 0
        rows.append(
            _build_row(
                slug,
                score=score,
                lifecycle_state=state,
                consecutive_d_count=streak,
                sources=sources,
            )
        )
    return rows


# ────────────────────────────────────────────────────────────────────
# Aggregation
# ────────────────────────────────────────────────────────────────────


def _grade_key(grade: str) -> str:
    """Normalize blank grades to 'F' for counting — no score ≈ worst signal."""
    return grade if grade in _GRADES else "F"


def aggregate(
    rows: list[EntityRow], *, now: datetime | None = None, top_n: int = 10,
) -> DashboardSummary:
    now = now or datetime.now(timezone.utc)

    by_subject: dict[str, int] = {}
    grade_counts: dict[str, int] = {g: 0 for g in _GRADES}
    lifecycle_counts: dict[str, int] = {s: 0 for s in _LIFECYCLE_STATES}
    hard_floor_counts: dict[str, int] = {}

    category_buckets: dict[str, list[EntityRow]] = {c: [] for c in CATEGORIES}
    category_buckets[_UNCATEGORIZED] = []

    for r in rows:
        by_subject[r.subject_type] = by_subject.get(r.subject_type, 0) + 1
        grade_counts[_grade_key(r.grade)] += 1
        lifecycle_counts[r.lifecycle_state] = (
            lifecycle_counts.get(r.lifecycle_state, 0) + 1
        )
        if r.hard_floor:
            hard_floor_counts[r.hard_floor] = (
                hard_floor_counts.get(r.hard_floor, 0) + 1
            )
        bucket = r.category if r.category in category_buckets else _UNCATEGORIZED
        category_buckets[bucket].append(r)

    category_breakdown: list[dict[str, Any]] = []
    for cat, cat_bucket in category_buckets.items():
        if not cat_bucket:
            continue
        scored = [r for r in cat_bucket if r.grade in _GRADES]
        avg_score = (
            sum(r.score for r in scored) / len(scored) if scored else 0.0
        )
        mix = {g: 0 for g in _GRADES}
        for r in cat_bucket:
            mix[_grade_key(r.grade)] += 1
        category_breakdown.append(
            {
                "category": cat,
                "count": len(cat_bucket),
                "avg_score": round(avg_score, 4),
                "grade_mix": mix,
            }
        )
    # Canonical order: taxonomy first, then uncategorized.
    _rank = {c: i for i, c in enumerate(CATEGORIES)}
    _rank[_UNCATEGORIZED] = len(CATEGORIES)
    category_breakdown.sort(key=lambda c: _rank.get(c["category"], 999))

    # Low-quality candidates: D/F grade, sorted by (streak desc, score asc).
    candidates = [
        r for r in rows
        if _grade_key(r.grade) in ("D", "F")
        and r.lifecycle_state in (STATE_ACTIVE, STATE_WATCH)
    ]
    candidates.sort(key=lambda r: (-r.consecutive_d_count, r.score))
    low_quality = [
        {
            "slug": r.slug,
            "subject_type": r.subject_type,
            "category": r.category,
            "grade": r.grade or "F",
            "score": round(r.score, 4),
            "lifecycle_state": r.lifecycle_state,
            "consecutive_d_count": r.consecutive_d_count,
            "hard_floor": r.hard_floor,
        }
        for r in candidates[: max(0, top_n)]
    ]

    archived = [
        {
            "slug": r.slug,
            "subject_type": r.subject_type,
            "category": r.category,
            "last_grade": r.grade or "",
            "computed_at": r.computed_at,
        }
        for r in rows if r.lifecycle_state == STATE_ARCHIVE
    ]

    return DashboardSummary(
        generated_at=now.isoformat(timespec="seconds"),
        total=len(rows),
        by_subject=by_subject,
        grade_counts=grade_counts,
        lifecycle_counts=lifecycle_counts,
        category_breakdown=category_breakdown,
        hard_floor_counts=hard_floor_counts,
        low_quality_candidates=low_quality,
        archived=archived,
    )


# ────────────────────────────────────────────────────────────────────
# Markdown rendering
# ────────────────────────────────────────────────────────────────────


def _pct(n: int, total: int) -> str:
    if total <= 0:
        return "—"
    return f"{(100.0 * n / total):.1f}%"


def _render_grade_row(grade: str, count: int, total: int) -> str:
    return f"| {grade} | {count} | {_pct(count, total)} |"


def render_markdown(summary: DashboardSummary) -> str:
    """Render a Markdown digest — one file, commit-friendly."""
    out: list[str] = []
    out.append("# Skill Quality KPI Dashboard")
    out.append("")
    out.append(f"_Generated: {summary.generated_at}_")
    out.append("")
    out.append(f"**Total entities:** {summary.total}")
    if summary.by_subject:
        parts = [
            f"{subject}: {count}"
            for subject, count in sorted(summary.by_subject.items())
        ]
        out.append(f"**By subject:** {'  ·  '.join(parts)}")
    out.append("")

    # Grade distribution
    out.append("## Grade distribution")
    out.append("")
    out.append("| Grade | Count | Share |")
    out.append("| ----- | ----: | ----: |")
    for g in _GRADES:
        out.append(_render_grade_row(g, summary.grade_counts.get(g, 0), summary.total))
    out.append("")

    # Lifecycle
    out.append("## Lifecycle tiers")
    out.append("")
    out.append("| State | Count |")
    out.append("| ----- | ----: |")
    for s in _LIFECYCLE_STATES:
        out.append(f"| {s} | {summary.lifecycle_counts.get(s, 0)} |")
    out.append("")

    # Hard floors
    if summary.hard_floor_counts:
        out.append("## Hard floors active")
        out.append("")
        out.append("| Reason | Count |")
        out.append("| ------ | ----: |")
        for reason, count in sorted(
            summary.hard_floor_counts.items(), key=lambda kv: (-kv[1], kv[0]),
        ):
            out.append(f"| {reason} | {count} |")
        out.append("")

    # Category breakdown
    out.append("## By category")
    out.append("")
    out.append("| Category | Count | Avg score | A | B | C | D | F |")
    out.append("| -------- | ----: | --------: | -: | -: | -: | -: | -: |")
    for entry in summary.category_breakdown:
        mix = entry["grade_mix"]
        out.append(
            "| {cat} | {count} | {avg:.3f} | {a} | {b} | {c} | {d} | {f} |".format(
                cat=entry["category"],
                count=entry["count"],
                avg=entry["avg_score"],
                a=mix.get("A", 0), b=mix.get("B", 0), c=mix.get("C", 0),
                d=mix.get("D", 0), f=mix.get("F", 0),
            )
        )
    out.append("")

    # Low-quality candidates
    out.append("## Top demotion candidates")
    out.append("")
    if not summary.low_quality_candidates:
        out.append("_No active D/F-grade entries — corpus is healthy._")
    else:
        out.append(
            "| Slug | Subject | Category | Grade | Score | State | D-streak | Hard floor |"
        )
        out.append(
            "| ---- | ------- | -------- | :---: | ----: | ----- | -------: | ---------- |"
        )
        for c in summary.low_quality_candidates:
            out.append(
                "| {slug} | {subj} | {cat} | {grade} | {score:.3f} | {state} | {streak} | {floor} |".format(
                    slug=c["slug"],
                    subj=c["subject_type"],
                    cat=c["category"],
                    grade=c["grade"],
                    score=c["score"],
                    state=c["lifecycle_state"],
                    streak=c["consecutive_d_count"],
                    floor=c.get("hard_floor") or "—",
                )
            )
    out.append("")

    # Archived
    out.append("## Archived (restorable)")
    out.append("")
    if not summary.archived:
        out.append("_None._")
    else:
        out.append("| Slug | Subject | Category | Last grade | Computed at |")
        out.append("| ---- | ------- | -------- | :--------: | ----------- |")
        for a in summary.archived:
            out.append(
                "| {slug} | {subj} | {cat} | {grade} | {at} |".format(
                    slug=a["slug"],
                    subj=a["subject_type"],
                    cat=a["category"],
                    grade=a["last_grade"] or "—",
                    at=a["computed_at"] or "—",
                )
            )
    out.append("")

    return "\n".join(out) + "\n"


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def _build_sources_from_config() -> LifecycleSources:
    from ctx_config import cfg
    from skill_quality import default_sidecar_dir
    return LifecycleSources(
        skills_dir=cfg.skills_dir,
        agents_dir=cfg.agents_dir,
        sidecar_dir=default_sidecar_dir(),
    )


def generate(
    *, sources: LifecycleSources, top_n: int = 10, now: datetime | None = None,
) -> DashboardSummary:
    rows = collect_rows(sources=sources)
    return aggregate(rows, now=now, top_n=top_n)


def cmd_render(args: argparse.Namespace) -> int:
    sources = _build_sources_from_config()
    summary = generate(sources=sources, top_n=args.limit)
    if args.json:
        payload = json.dumps(summary.to_dict(), indent=2, sort_keys=True)
        if args.out:
            Path(args.out).write_text(payload, encoding="utf-8")
        else:
            print(payload)
        return 0
    md = render_markdown(summary)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(md)
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    sources = _build_sources_from_config()
    summary = generate(sources=sources, top_n=0)
    print(f"Total: {summary.total}")
    for g in _GRADES:
        print(f"  {g}: {summary.grade_counts.get(g, 0)}")
    print("Lifecycle:")
    for s in _LIFECYCLE_STATES:
        print(f"  {s}: {summary.lifecycle_counts.get(s, 0)}")
    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kpi_dashboard",
        description="Render the skill-quality KPI dashboard.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="Render Markdown or JSON dashboard")
    r.add_argument("--out", help="Write to this path instead of stdout")
    r.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")
    r.add_argument("--limit", type=int, default=10,
                   help="Max rows in the demotion-candidates section")
    r.set_defaults(func=cmd_render)

    s = sub.add_parser("summary", help="Print a terse one-screen summary")
    s.set_defaults(func=cmd_summary)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DashboardSummary",
    "EntityRow",
    "aggregate",
    "collect_rows",
    "generate",
    "main",
    "render_markdown",
]
