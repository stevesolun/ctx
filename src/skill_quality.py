#!/usr/bin/env python3
"""
skill_quality.py -- Post-install quality scorer + three-sink persistence.

Phase 3 of the skill-quality plan (see ``docs/roadmap/skill-quality.md``).

Flow:

  1. ``extract_signals_for_slug`` gathers the four signal inputs by
     reading telemetry events, re-parsing the on-disk skill/agent file,
     consulting the wiki graph, and pulling router-trace counts.
  2. ``compute_quality`` aggregates those ``SignalResult`` instances via
     a weighted sum, applies hard floors, and maps to an A/B/C/D/F grade.
  3. ``persist_quality`` mirrors the result to three on-disk sinks so every
     downstream consumer — Obsidian, machine-readable automations, the wiki
     UI — can see the same number.

Persistence sinks (Q3 in the plan doc):

  - Sidecar JSON — ``~/.claude/skill-quality/<slug>.json`` (canonical
    machine-readable form; source of truth for the graph writer).
  - Frontmatter — ``quality_score``, ``quality_grade``,
    ``quality_updated_at`` keys on the wiki entity page.
  - Wiki body — a ``## Quality`` section with the grade + breakdown
    rendered in Markdown.

  The knowledge-graph node attribute is a **separate consumer path**:
  ``wiki_graphify`` reads the sidecar JSON on its next build and attaches
  quality attributes to graph nodes. It is not a write path owned by this
  module.

CLI verbs:

  - ``recompute``  — recompute one or more slugs (--all / --slugs / --slug).
  - ``show``       — print the current score for a slug.
  - ``explain``    — print the signal breakdown + evidence for a slug.
  - ``list``       — list every known slug with its grade (piped to tools).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable

from quality_signals import (
    SignalResult,
    graph_signal,
    intake_signal,
    routing_signal,
    telemetry_signal,
)
from ctx.utils._fs_utils import atomic_write_text as _atomic_write
from ctx.core.wiki.wiki_utils import SAFE_NAME_RE as _SLUG_RE, parse_frontmatter_and_body

_logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Config (static defaults; user override via ctx_config.cfg)
# ────────────────────────────────────────────────────────────────────


# Default signal weights for skills. Sum must be 1.0. Telemetry gets the
# largest slice because it's the one signal that meaningfully changes
# after install; the other three are structural and move slowly.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "telemetry": 0.40,
    "intake": 0.20,
    "graph": 0.25,
    "routing": 0.15,
}

# Default signal weights for agents. Agents are invoked deliberately and
# rarely — a seldom-used agent isn't stale, it's specialized — so
# telemetry is a weaker quality signal for them than for skills. Graph
# connectedness and intake structure carry more of the weight instead.
_DEFAULT_AGENT_WEIGHTS: dict[str, float] = {
    "telemetry": 0.15,
    "intake": 0.30,
    "graph": 0.35,
    "routing": 0.20,
}

# Grade cutoffs. Score must meet or exceed the threshold for that grade.
_DEFAULT_GRADE_THRESHOLDS: dict[str, float] = {
    "A": 0.80,
    "B": 0.60,
    "C": 0.40,
}

# Hard-floor thresholds applied after the weighted sum.
_DEFAULT_STALE_THRESHOLD_DAYS: float = 30.0
_DEFAULT_RECENT_WINDOW_DAYS: float = 14.0
_DEFAULT_MIN_BODY_CHARS: int = 120


def _ensure_safe_slug(slug: str) -> str:
    """Reject slugs that could traverse out of the sidecar directory."""
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(f"invalid quality slug: {slug!r}")
    return slug


_WEIGHT_KEYS: frozenset[str] = frozenset(
    {"telemetry", "intake", "graph", "routing"}
)


def _validate_weight_vector(name: str, weights: Mapping[str, float]) -> None:
    if set(weights) != _WEIGHT_KEYS:
        raise ValueError(
            f"{name} must supply exactly: telemetry, intake, graph, routing"
        )
    total = sum(weights.values())
    if not 0.99 <= total <= 1.01:
        raise ValueError(f"{name} must sum to 1.0; got {total:.4f}")
    for k, v in weights.items():
        if v < 0:
            raise ValueError(f"{name} weight for {k!r} must be >= 0, got {v}")


@dataclass(frozen=True)
class QualityConfig:
    """All knobs used by the scorer. Frozen so tests cannot mutate by accident."""

    weights: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_WEIGHTS)
    )
    agent_weights: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_AGENT_WEIGHTS)
    )
    grade_thresholds: Mapping[str, float] = field(
        default_factory=lambda: dict(_DEFAULT_GRADE_THRESHOLDS)
    )
    stale_threshold_days: float = _DEFAULT_STALE_THRESHOLD_DAYS
    recent_window_days: float = _DEFAULT_RECENT_WINDOW_DAYS
    min_body_chars: int = _DEFAULT_MIN_BODY_CHARS

    def __post_init__(self) -> None:
        _validate_weight_vector("weights", self.weights)
        _validate_weight_vector("agent_weights", self.agent_weights)
        if set(self.grade_thresholds) != {"A", "B", "C"}:
            raise ValueError("grade_thresholds must supply A, B, C cutoffs")
        a = self.grade_thresholds["A"]
        b = self.grade_thresholds["B"]
        c = self.grade_thresholds["C"]
        if not 0.0 <= c <= b <= a <= 1.0:
            raise ValueError(
                "grade thresholds must satisfy 0 <= C <= B <= A <= 1"
            )
        if self.stale_threshold_days <= 0:
            raise ValueError("stale_threshold_days must be > 0")
        if self.recent_window_days <= 0:
            raise ValueError("recent_window_days must be > 0")
        if self.min_body_chars < 0:
            raise ValueError("min_body_chars must be >= 0")

    def weights_for(self, subject_type: str) -> Mapping[str, float]:
        """Pick the weight vector that applies to this subject_type."""
        if subject_type == "agent":
            return self.agent_weights
        return self.weights


# ────────────────────────────────────────────────────────────────────
# Result types
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QualityScore:
    """One skill's quality score snapshot — frozen for safe sharing."""

    slug: str
    subject_type: str                  # "skill" | "agent"
    raw_score: float                   # weighted sum before floors
    score: float                       # final, after floors + clamp
    grade: str                         # A / B / C / D / F
    hard_floor: str | None             # which floor fired, if any
    signals: Mapping[str, SignalResult] = field(default_factory=dict)
    weights: Mapping[str, float] = field(default_factory=dict)
    computed_at: str = ""              # ISO-8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "subject_type": self.subject_type,
            "raw_score": round(self.raw_score, 4),
            "score": round(self.score, 4),
            "grade": self.grade,
            "hard_floor": self.hard_floor,
            "signals": {
                name: {
                    "score": round(sig.score, 4),
                    "evidence": dict(sig.evidence),
                }
                for name, sig in self.signals.items()
            },
            "weights": dict(self.weights),
            "computed_at": self.computed_at,
        }


# ────────────────────────────────────────────────────────────────────
# Paths
# ────────────────────────────────────────────────────────────────────


def default_sidecar_dir() -> Path:
    """Directory where per-slug quality JSONs land.

    Honours ``quality.paths.sidecar_dir`` from ``ctx_config.cfg`` when
    set; falls back to ``~/.claude/skill-quality`` otherwise.
    """
    try:
        from ctx_config import cfg  # local import to avoid cost on test import
        raw = cfg.get("quality", {}) or {}
        paths = raw.get("paths", {}) if isinstance(raw, dict) else {}
        configured = paths.get("sidecar_dir") if isinstance(paths, dict) else None
        if isinstance(configured, str) and configured.strip():
            return Path(os.path.expanduser(configured))
    except Exception:  # noqa: BLE001 — config unavailable in some test contexts
        pass
    return Path(os.path.expanduser("~/.claude/skill-quality"))


def sidecar_path(slug: str, *, sidecar_dir: Path | None = None) -> Path:
    _ensure_safe_slug(slug)
    root = sidecar_dir if sidecar_dir is not None else default_sidecar_dir()
    return root / f"{slug}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ────────────────────────────────────────────────────────────────────
# Core scoring
# ────────────────────────────────────────────────────────────────────


def _grade_from_score(score: float, thresholds: Mapping[str, float]) -> str:
    """Map a numeric score to a letter grade A/B/C/D.

    Note: This function does **not** return F. Grade F is produced
    exclusively by the ``intake_fail`` hard-floor override in
    ``compute_quality()`` when the intake signal reports a structural
    failure.  A score of 0.0 returns D (the lowest score-derived grade).
    """
    if score >= thresholds["A"]:
        return "A"
    if score >= thresholds["B"]:
        return "B"
    if score >= thresholds["C"]:
        return "C"
    return "D"


def compute_quality(
    *,
    slug: str,
    subject_type: str,
    signals: Mapping[str, SignalResult],
    config: QualityConfig | None = None,
    computed_at: str | None = None,
) -> QualityScore:
    """Aggregate signals → score → grade, applying hard floors.

    Hard floors override the weighted grade:
      - Any intake ``hard_fail`` → grade F (entity currently violates
        the structural gate; remediate the file or unlist it). Applies
        to both skills and agents.
      - ``never_loaded`` on a **skill** → grade D, regardless of
        graph/intake strength. Evidence lives in the telemetry signal;
        we just check the flag here. This floor does NOT apply to
        agents — agents are invoked via the Agent tool and do not emit
        load events, so a zero telemetry signal is the expected steady
        state for them rather than a staleness signal.

    Weight vector is selected per subject_type: skills use
    ``config.weights`` (telemetry-heavy), agents use
    ``config.agent_weights`` (structure-heavy).
    """
    _ensure_safe_slug(slug)
    if subject_type not in ("skill", "agent"):
        raise ValueError(f"subject_type must be 'skill' or 'agent': {subject_type!r}")
    cfg = config or QualityConfig()
    weights = cfg.weights_for(subject_type)

    required = {"telemetry", "intake", "graph", "routing"}
    if set(signals) != required:
        missing = required - set(signals)
        extra = set(signals) - required
        raise ValueError(
            f"signals keys mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
        )

    raw = sum(weights[name] * signals[name].score for name in required)
    score = max(0.0, min(1.0, raw))

    hard_floor: str | None = None
    intake_evidence = signals["intake"].evidence or {}
    if intake_evidence.get("hard_fail"):
        hard_floor = "intake_fail"
        grade = "F"
    elif subject_type == "skill":
        telemetry_evidence = signals["telemetry"].evidence or {}
        never_loaded = bool(telemetry_evidence.get("never_loaded"))
        # Without telemetry we cannot tell stale from never-seen. Treat
        # "never_loaded" as prima facie stale when the skill exists —
        # the scorer's job is to push low-signal entries toward review.
        if never_loaded:
            hard_floor = "never_loaded_stale"
            grade = _grade_from_score(score, cfg.grade_thresholds)
            # Floor to at most D when it would otherwise have graded higher.
            if grade in ("A", "B", "C"):
                grade = "D"
        else:
            grade = _grade_from_score(score, cfg.grade_thresholds)
    else:
        # Agent: no load-event stream exists, so the never_loaded flag
        # on the telemetry signal is not a quality signal. Grade
        # straight from the weighted sum (which already weights
        # telemetry lightly for agents).
        grade = _grade_from_score(score, cfg.grade_thresholds)

    return QualityScore(
        slug=slug,
        subject_type=subject_type,
        raw_score=raw,
        score=score,
        grade=grade,
        hard_floor=hard_floor,
        signals=dict(signals),
        weights=dict(weights),
        computed_at=computed_at or _now_iso(),
    )


# ────────────────────────────────────────────────────────────────────
# Signal extraction (the impure layer)
# ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SignalSources:
    """Paths + already-loaded structures the extractor needs."""

    skills_dir: Path
    agents_dir: Path
    wiki_dir: Path
    events_path: Path
    router_trace_path: Path | None = None
    # ``graph_index`` may be supplied pre-built by the caller (the stop
    # hook does this so every slug scored in the same tick shares one
    # graph walk). When None, ``extract_signals_for_slug`` falls back to
    # a cheap structural walk of entity frontmatter.
    graph_index: Mapping[str, Mapping[str, Any]] | None = None
    # ``events_index`` may be supplied pre-built by ``recompute_all`` so
    # that a full --all recompute reads the JSONL once instead of once
    # per slug.  Keys are slug strings; values are lists of raw event
    # dicts.  When None, ``_compute_telemetry_inputs`` scans the file
    # directly (preserving the single-slug O(M) behaviour).
    events_index: Mapping[str, list[dict[str, Any]]] | None = None


def _read_skill_source(slug: str, sources: SignalSources) -> tuple[str, str]:
    """Return (subject_type, raw_md). Raises FileNotFoundError if neither exists.

    Skills live at ``<skills_dir>/<slug>/SKILL.md``; agents live at
    ``<agents_dir>/<slug>.md``. We treat skills as the default because
    they're the larger corpus; Phase 5 will add proper agent parity.
    """
    skill_path = sources.skills_dir / slug / "SKILL.md"
    if skill_path.is_file():
        return "skill", skill_path.read_text(encoding="utf-8", errors="replace")
    # Agents may live in nested subdirectories (e.g. agents/design/foo.md).
    # Try exact flat path first (O(1)), then fall back to an rglob scan.
    agent_path = sources.agents_dir / f"{slug}.md"
    if agent_path.is_file():
        return "agent", agent_path.read_text(encoding="utf-8", errors="replace")
    if sources.agents_dir.is_dir():
        matches = [
            p for p in sources.agents_dir.rglob(f"{slug}.md")
            if p.is_file()
        ]
        if len(matches) > 1:
            raise FileNotFoundError(
                f"ambiguous agent slug {slug!r}: found {len(matches)} files "
                f"under {sources.agents_dir}: {[str(m) for m in matches]}"
            )
        if len(matches) == 1:
            return "agent", matches[0].read_text(encoding="utf-8", errors="replace")
    raise FileNotFoundError(
        f"no skill or agent file found for slug {slug!r} under "
        f"{sources.skills_dir} or {sources.agents_dir}"
    )


def _iter_events_for_slug(slug: str, events_path: Path) -> Iterable[dict[str, Any]]:
    """Yield raw event dicts for one slug, skipping malformed lines."""
    if not events_path.is_file():
        return
    try:
        fh = events_path.open(encoding="utf-8")
    except OSError:
        return
    with fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("skill") == slug:
                yield obj


def _parse_event_ts(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compute_telemetry_inputs(
    slug: str,
    events_path: Path,
    *,
    now: datetime,
    recent_window_days: float,
    events_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Walk the event stream once and derive telemetry inputs.

    When ``events_override`` is provided (a pre-filtered list of events
    for this slug), it is used directly instead of scanning
    ``events_path``.  This allows callers that already built a full
    events index to avoid redundant O(M) file scans per slug.
    """
    load_count = 0
    recent_load_count = 0
    last_load_at: datetime | None = None
    recent_cutoff = now.timestamp() - recent_window_days * 86400.0

    event_iter: Iterable[dict[str, Any]] = (
        events_override
        if events_override is not None
        else _iter_events_for_slug(slug, events_path)
    )
    for obj in event_iter:
        if obj.get("event") != "load":
            continue
        ts = _parse_event_ts(str(obj.get("timestamp", "")))
        if ts is None:
            continue
        load_count += 1
        if ts.timestamp() >= recent_cutoff:
            recent_load_count += 1
        if last_load_at is None or ts > last_load_at:
            last_load_at = ts

    last_load_age_days: float | None
    if last_load_at is None:
        last_load_age_days = None
    else:
        delta = (now - last_load_at).total_seconds()
        last_load_age_days = max(0.0, delta / 86400.0)

    return {
        "load_count": load_count,
        "recent_load_count": recent_load_count,
        "last_load_age_days": last_load_age_days,
    }


def _compute_routing_inputs(
    slug: str, trace_path: Path | None
) -> dict[str, int]:
    """Count ``considered`` and ``picked`` events for one slug.

    Trace format (JSONL): ``{"skill": "...", "considered": true,
    "picked": true | false, "timestamp": "..."}``. Missing file → zeros,
    which pushes the routing signal into its neutral-prior branch.
    """
    if trace_path is None or not trace_path.is_file():
        return {"considered": 0, "picked": 0}
    considered = 0
    picked = 0
    try:
        with trace_path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("skill") != slug:
                    continue
                if obj.get("considered"):
                    considered += 1
                if obj.get("picked"):
                    picked += 1
    except OSError:
        return {"considered": 0, "picked": 0}
    # Router may log picks without an explicit ``considered`` marker.
    # A pick implies consideration, so raise the floor accordingly.
    considered = max(considered, picked)
    return {"considered": considered, "picked": picked}


def _compute_graph_inputs(
    slug: str,
    subject_type: str,
    graph_index: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Read degree + average edge weight from a pre-built graph index.

    When ``graph_index`` is None the caller is running with no graph
    data available (first-run bootstrap, tests, or a wiki that has not
    yet been graphified). Return zeros — the signal's isolated-node
    branch keeps its score at 0 in that case, which is accurate: until
    the graph exists, nothing is connected.
    """
    if graph_index is None:
        return {"degree": 0, "avg_edge_weight": 0.0}
    key = f"{subject_type}:{slug}"
    node = graph_index.get(key)
    if not isinstance(node, Mapping):
        return {"degree": 0, "avg_edge_weight": 0.0}
    degree = int(node.get("degree", 0))
    avg_weight = float(node.get("avg_edge_weight", 1.0)) if degree > 0 else 0.0
    return {"degree": degree, "avg_edge_weight": avg_weight}


def extract_signals_for_slug(
    slug: str,
    *,
    sources: SignalSources,
    config: QualityConfig | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, SignalResult]]:
    """Compute all four signals for one slug. Returns (subject_type, signals)."""
    _ensure_safe_slug(slug)
    cfg = config or QualityConfig()
    ts = now or datetime.now(timezone.utc)

    subject_type, raw_md = _read_skill_source(slug, sources)
    fm, body = parse_frontmatter_and_body(raw_md)
    has_fm_block = raw_md.lstrip().startswith("---")

    events_override = (
        list(sources.events_index.get(slug, []))
        if sources.events_index is not None
        else None
    )
    tel_inputs = _compute_telemetry_inputs(
        slug,
        sources.events_path,
        now=ts,
        recent_window_days=cfg.recent_window_days,
        events_override=events_override,
    )
    tel = telemetry_signal(
        load_count=tel_inputs["load_count"],
        recent_load_count=tel_inputs["recent_load_count"],
        last_load_age_days=tel_inputs["last_load_age_days"],
        stale_threshold_days=cfg.stale_threshold_days,
    )

    intake = intake_signal(
        raw_md,
        frontmatter=fm,
        has_frontmatter_block=has_fm_block,
        body=body,
        min_body_chars=cfg.min_body_chars,
    )

    graph_inputs = _compute_graph_inputs(slug, subject_type, sources.graph_index)
    graph = graph_signal(
        degree=graph_inputs["degree"],
        avg_edge_weight=graph_inputs["avg_edge_weight"],
    )

    routing_inputs = _compute_routing_inputs(slug, sources.router_trace_path)
    routing = routing_signal(
        considered=routing_inputs["considered"],
        picked=routing_inputs["picked"],
    )

    return subject_type, {
        "telemetry": tel,
        "intake": intake,
        "graph": graph,
        "routing": routing,
    }


# ────────────────────────────────────────────────────────────────────
# Persistence (three sinks)
# ────────────────────────────────────────────────────────────────────


_QUALITY_SECTION_HEADER = "## Quality"
_QUALITY_SECTION_BEGIN = "<!-- quality:begin -->"
_QUALITY_SECTION_END = "<!-- quality:end -->"


def _render_quality_section(score: QualityScore) -> str:
    """Build the ``## Quality`` block injected into the wiki entity page."""
    lines: list[str] = [
        _QUALITY_SECTION_BEGIN,
        _QUALITY_SECTION_HEADER,
        "",
        f"- **Grade:** {score.grade}",
        f"- **Score:** {score.score:.2f} "
        f"(raw {score.raw_score:.2f})",
        f"- **Computed:** {score.computed_at}",
    ]
    if score.hard_floor:
        lines.append(f"- **Hard floor:** `{score.hard_floor}`")
    lines.append("")
    lines.append("| Signal | Score | Weight |")
    lines.append("| --- | --- | --- |")
    for name in ("telemetry", "intake", "graph", "routing"):
        sig = score.signals[name]
        w = score.weights.get(name, 0.0)
        lines.append(f"| {name} | {sig.score:.2f} | {w:.2f} |")
    lines.append("")
    lines.append(_QUALITY_SECTION_END)
    return "\n".join(lines)


_QUALITY_BLOCK_RE = re.compile(
    re.escape(_QUALITY_SECTION_BEGIN)
    + r".*?"
    + re.escape(_QUALITY_SECTION_END),
    re.DOTALL,
)


def _inject_quality_section(body: str, block: str) -> str:
    """Replace any existing ``## Quality`` block, else append."""
    if _QUALITY_BLOCK_RE.search(body):
        return _QUALITY_BLOCK_RE.sub(block, body, count=1)
    sep = "" if body.endswith("\n") else "\n"
    return body + sep + "\n" + block + "\n"


def _update_frontmatter_quality(raw_md: str, score: QualityScore) -> str:
    """Update ``quality_*`` keys in the frontmatter; preserve other keys.

    Keeps the edit surgical: we don't re-emit the whole frontmatter with
    a YAML library because that would normalize quoting/ordering and
    blow up diffs every time the score changes.
    """
    if not raw_md.startswith("---"):
        return raw_md
    end_idx = raw_md.find("\n---", 3)
    if end_idx == -1:
        return raw_md
    fm_block = raw_md[3 : end_idx + 1]
    after_fm = raw_md[end_idx + 4 :]

    pairs: list[str] = [
        f"quality_score: {score.score:.4f}",
        f"quality_grade: {score.grade}",
        f"quality_updated_at: {score.computed_at}",
    ]
    if score.hard_floor:
        pairs.append(f"quality_hard_floor: {score.hard_floor}")
    else:
        # Explicitly clear the key if it existed before — prevents a
        # stale floor label from sticking around after the condition
        # clears.
        pairs.append("quality_hard_floor: ")

    lines = fm_block.splitlines()
    kept: list[str] = [
        ln for ln in lines if not ln.lstrip().startswith(
            ("quality_score:", "quality_grade:", "quality_updated_at:",
             "quality_hard_floor:")
        )
    ]
    # Drop trailing blanks from the kept block, then re-append our pairs.
    while kept and not kept[-1].strip():
        kept.pop()
    new_fm = "\n".join(kept + pairs)

    return "---" + "\n" + new_fm + "\n---" + after_fm


@runtime_checkable
class QualitySink(Protocol):
    """Write a ``QualityScore`` to one persistence destination.

    Each concrete sink is responsible for exactly one storage target:
    the sidecar JSON file, the wiki entity frontmatter, or the wiki body
    section.  ``persist_quality`` iterates over the active list of sinks.
    """

    def write(self, score: QualityScore) -> Path | None:
        """Persist ``score`` and return the path written, or ``None`` if skipped."""
        ...


class SidecarSink:
    """Sink 1 — ``~/.claude/skill-quality/<slug>.json`` (canonical machine form)."""

    def __init__(self, sidecar_dir: Path | None = None) -> None:
        self._sidecar_dir = sidecar_dir

    def write(self, score: QualityScore) -> Path | None:
        path = sidecar_path(score.slug, sidecar_dir=self._sidecar_dir)
        _atomic_write(
            path,
            json.dumps(score.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
        )
        return path


class WikiFrontmatterSink:
    """Sink 2 — ``quality_*`` keys in the wiki entity page frontmatter."""

    def __init__(self, wiki_dir: Path) -> None:
        self._wiki_dir = wiki_dir

    def _entity_path(self, score: QualityScore) -> Path:
        entity_subdir = "skills" if score.subject_type == "skill" else "agents"
        return self._wiki_dir / "entities" / entity_subdir / f"{score.slug}.md"

    def write(self, score: QualityScore) -> Path | None:
        entity_path = self._entity_path(score)
        if not entity_path.is_file():
            _logger.info(
                "skill_quality: no wiki page at %s; frontmatter sink skipped",
                entity_path,
            )
            return None
        raw = entity_path.read_text(encoding="utf-8", errors="replace")
        updated = _update_frontmatter_quality(raw, score)
        _atomic_write(entity_path, updated)
        return entity_path


class WikiBodySink:
    """Sink 3 — ``## Quality`` section in the wiki entity page body."""

    def __init__(self, wiki_dir: Path) -> None:
        self._wiki_dir = wiki_dir

    def _entity_path(self, score: QualityScore) -> Path:
        entity_subdir = "skills" if score.subject_type == "skill" else "agents"
        return self._wiki_dir / "entities" / entity_subdir / f"{score.slug}.md"

    def write(self, score: QualityScore) -> Path | None:
        entity_path = self._entity_path(score)
        if not entity_path.is_file():
            return None
        raw = entity_path.read_text(encoding="utf-8", errors="replace")
        fm_end = raw.find("\n---", 3)
        if fm_end == -1:
            header, body = "", raw
        else:
            header = raw[: fm_end + 4]
            body = raw[fm_end + 4 :]
        new_body = _inject_quality_section(body, _render_quality_section(score))
        _atomic_write(entity_path, header + new_body)
        return entity_path


def persist_quality(
    score: QualityScore,
    *,
    sources: SignalSources,
    sidecar_dir: Path | None = None,
    update_frontmatter: bool = True,
) -> dict[str, Path]:
    """Write the quality result to the three on-disk sinks.

    The knowledge-graph node-attribute is a separate consumer path:
    ``wiki_graphify`` reads the sidecar JSON that this function produced
    on its next build.

    Returns a mapping of sink-name → Path that was written, for the CLI
    to report back to the user.
    """
    written: dict[str, Path] = {}

    sidecar_result = SidecarSink(sidecar_dir).write(score)
    if sidecar_result is not None:
        written["sidecar"] = sidecar_result

    if not update_frontmatter:
        return written

    fm_result = WikiFrontmatterSink(sources.wiki_dir).write(score)
    if fm_result is not None:
        written["frontmatter"] = fm_result

    body_result = WikiBodySink(sources.wiki_dir).write(score)
    if body_result is not None:
        written["wiki_body"] = body_result

    # Best-effort audit: one line per score refresh so postmortem
    # scripts can trace why a sidecar changed at a specific instant.
    # Session attribution comes from ``CTX_SESSION_ID`` in the env —
    # quality_on_session_end.py exports this before invoking recompute
    # so the monitor's per-session timeline can filter by session_id.
    try:
        from ctx_audit_log import log  # local import, no CLI dep
        subject_type = "agent" if score.subject_type == "agent" else "skill"
        event = f"{subject_type}.score_updated"
        log(
            event, subject_type=subject_type, subject=score.slug,
            actor="hook",
            session_id=os.environ.get("CTX_SESSION_ID"),
            meta={
                "grade": score.grade,
                "raw_score": round(score.raw_score, 4),
                "hard_floor": score.hard_floor,
            },
        )
    except Exception:  # noqa: BLE001 — audit must never break scoring
        pass

    return written


def load_quality(
    slug: str, *, sidecar_dir: Path | None = None
) -> QualityScore | None:
    """Read back a previously-persisted ``QualityScore`` from disk.

    Returns ``None`` if no sidecar exists. Partial / corrupt files raise
    ``json.JSONDecodeError`` or ``ValueError`` — the caller decides
    whether to skip or re-compute.
    """
    path = sidecar_path(slug, sidecar_dir=sidecar_dir)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    signals: dict[str, SignalResult] = {}
    for name, payload in data.get("signals", {}).items():
        signals[name] = SignalResult(
            score=float(payload.get("score", 0.0)),
            evidence=dict(payload.get("evidence", {})),
        )
    return QualityScore(
        slug=data["slug"],
        subject_type=data.get("subject_type", "skill"),
        raw_score=float(data.get("raw_score", 0.0)),
        score=float(data.get("score", 0.0)),
        grade=data.get("grade", "D"),
        hard_floor=data.get("hard_floor"),
        signals=signals,
        weights=dict(data.get("weights", {})),
        computed_at=data.get("computed_at", ""),
    )


# ────────────────────────────────────────────────────────────────────
# High-level orchestration
# ────────────────────────────────────────────────────────────────────


def recompute_slug(
    slug: str,
    *,
    sources: SignalSources,
    config: QualityConfig | None = None,
    now: datetime | None = None,
    sidecar_dir: Path | None = None,
    update_frontmatter: bool = True,
) -> QualityScore:
    """End-to-end recompute: extract signals → compute → persist."""
    subject_type, signals = extract_signals_for_slug(
        slug, sources=sources, config=config, now=now
    )
    score = compute_quality(
        slug=slug,
        subject_type=subject_type,
        signals=signals,
        config=config,
        computed_at=(now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    )
    persist_quality(
        score,
        sources=sources,
        sidecar_dir=sidecar_dir,
        update_frontmatter=update_frontmatter,
    )
    return score


def discover_slugs(sources: SignalSources) -> list[tuple[str, str]]:
    """Enumerate every (subject_type, slug) on disk, deduped, sorted."""
    out: dict[str, str] = {}
    if sources.skills_dir.is_dir():
        for entry in sorted(sources.skills_dir.iterdir()):
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                slug = entry.name
                if _SLUG_RE.match(slug):
                    out[slug] = "skill"
    if sources.agents_dir.is_dir():
        for entry in sorted(sources.agents_dir.glob("*.md")):
            slug = entry.stem
            if _SLUG_RE.match(slug) and slug not in out:
                out[slug] = "agent"
    return [(subject, slug) for slug, subject in out.items()]


_EVENTS_INDEX_SIZE_THRESHOLD = 100 * 1024 * 1024  # 100 MB


def _build_events_index(
    events_path: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Read ``events_path`` once and return a ``{slug: [events]}`` map.

    For a JSONL file under 100 MB the entire file is read into memory so
    that ``recompute_all`` can hand each slug its pre-filtered event list
    instead of re-scanning the file N times.

    For files over 100 MB the same line-by-line approach is used but with
    a ``defaultdict`` to keep peak memory proportional to distinct slugs
    rather than total file size.
    """
    from collections import defaultdict

    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not events_path.is_file():
        return {}
    try:
        with events_path.open(encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    slug = obj.get("skill")
                    if isinstance(slug, str) and slug:
                        index[slug].append(obj)
    except OSError:
        return {}
    return dict(index)


def recompute_all(
    *,
    sources: SignalSources,
    config: QualityConfig | None = None,
    now: datetime | None = None,
    sidecar_dir: Path | None = None,
    update_frontmatter: bool = True,
) -> tuple[list[QualityScore], list[tuple[str, Exception]]]:
    """Recompute every discovered slug, reading the events JSONL once.

    Returns ``(successes, failures)`` where failures is a list of
    ``(slug, exception)`` pairs.  The JSONL is read once and the
    resulting ``events_index`` is injected into ``sources`` so each
    per-slug call to ``_compute_telemetry_inputs`` reads from memory
    rather than re-scanning the file.
    """
    slug_pairs = discover_slugs(sources)
    events_index = _build_events_index(sources.events_path)
    # Inject the pre-built index into a new SignalSources instance.
    from dataclasses import replace as _dc_replace
    indexed_sources = _dc_replace(sources, events_index=events_index)

    successes: list[QualityScore] = []
    failures: list[tuple[str, Exception]] = []
    for _subject_type, slug in slug_pairs:
        try:
            score = recompute_slug(
                slug,
                sources=indexed_sources,
                config=config,
                now=now,
                sidecar_dir=sidecar_dir,
                update_frontmatter=update_frontmatter,
            )
            successes.append(score)
        except (FileNotFoundError, ValueError, OSError) as exc:
            failures.append((slug, exc))
    return successes, failures


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────


def _build_sources_from_config() -> SignalSources:
    """Construct SignalSources from ``ctx_config.cfg`` for CLI invocations."""
    from ctx_config import cfg  # local import: avoid cost on unit-test import
    quality_raw = cfg.get("quality", {}) or {}
    paths = quality_raw.get("paths", {}) if isinstance(quality_raw, dict) else {}
    trace_path_raw = paths.get("router_trace") if isinstance(paths, dict) else None
    trace_path = (
        Path(os.path.expanduser(trace_path_raw))
        if isinstance(trace_path_raw, str) and trace_path_raw
        else None
    )
    events_path = Path(os.path.expanduser("~/.claude/skill-events.jsonl"))
    return SignalSources(
        skills_dir=cfg.skills_dir,
        agents_dir=cfg.agents_dir,
        wiki_dir=cfg.wiki_dir,
        events_path=events_path,
        router_trace_path=trace_path,
    )


def _config_from_cfg() -> QualityConfig:
    """Build QualityConfig from ``ctx_config.cfg``'s ``quality`` block."""
    from ctx_config import cfg
    quality_raw = cfg.get("quality", {}) or {}
    if not isinstance(quality_raw, dict):
        return QualityConfig()
    weights = quality_raw.get("weights")
    agent_weights = quality_raw.get("agent_weights")
    thresholds = quality_raw.get("grade_thresholds")
    stale = quality_raw.get("stale_threshold_days")
    recent = quality_raw.get("recent_window_days")
    min_body = quality_raw.get("min_body_chars")

    kwargs: dict[str, Any] = {}
    if isinstance(weights, dict) and weights:
        kwargs["weights"] = {k: float(v) for k, v in weights.items()}
    if isinstance(agent_weights, dict) and agent_weights:
        kwargs["agent_weights"] = {k: float(v) for k, v in agent_weights.items()}
    if isinstance(thresholds, dict) and thresholds:
        kwargs["grade_thresholds"] = {k: float(v) for k, v in thresholds.items()}
    if isinstance(stale, (int, float)):
        kwargs["stale_threshold_days"] = float(stale)
    if isinstance(recent, (int, float)):
        kwargs["recent_window_days"] = float(recent)
    if isinstance(min_body, int):
        kwargs["min_body_chars"] = min_body
    return QualityConfig(**kwargs)


def cmd_recompute(args: argparse.Namespace) -> int:
    sources = _build_sources_from_config()
    cfg = _config_from_cfg()

    results: list[dict[str, Any]] = []
    failures = 0

    if args.all:
        # O(M) single JSONL scan shared across all N slugs.
        scores, errs = recompute_all(sources=sources, config=cfg)
        results = [s.to_dict() for s in scores]
        failures = len(errs)
        for slug, exc in errs:
            print(f"[recompute] {slug}: {exc}", file=sys.stderr)
    else:
        slugs: list[str]
        if args.slugs_positional:
            slugs = list(args.slugs_positional)
        elif args.slugs:
            slugs = [s for s in args.slugs.split(",") if s.strip()]
        elif args.slug:
            slugs = [args.slug]
        else:
            print("recompute: pass one or more SLUG positionals, --all, "
                  "--slugs, or --slug", file=sys.stderr)
            return 2

        for slug in slugs:
            try:
                score = recompute_slug(slug, sources=sources, config=cfg)
                results.append(score.to_dict())
            except (FileNotFoundError, ValueError, OSError) as exc:
                failures += 1
                print(f"[recompute] {slug}: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps({"count": len(results), "failures": failures,
                          "results": results}, indent=2))
    else:
        for r in results:
            print(f"{r['grade']}  {r['slug']:<40} score={r['score']:.2f}"
                  + (f"  floor={r['hard_floor']}" if r['hard_floor'] else ""))
        print(f"{len(results)} recomputed, {failures} failed", file=sys.stderr)
    return 0 if failures == 0 else 1


def cmd_show(args: argparse.Namespace) -> int:
    loaded = load_quality(args.slug)
    if loaded is None:
        print(f"no sidecar for {args.slug!r} (run recompute first)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(loaded.to_dict(), indent=2))
    else:
        print(f"{loaded.slug} ({loaded.subject_type})")
        print(f"  grade: {loaded.grade}")
        print(f"  score: {loaded.score:.2f} (raw {loaded.raw_score:.2f})")
        print(f"  floor: {loaded.hard_floor or '—'}")
        print(f"  computed: {loaded.computed_at}")
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    loaded = load_quality(args.slug)
    if loaded is None:
        print(f"no sidecar for {args.slug!r} (run recompute first)", file=sys.stderr)
        return 1
    print(f"{loaded.slug} ({loaded.subject_type}) — grade {loaded.grade}")
    print(f"  raw={loaded.raw_score:.4f}  score={loaded.score:.4f}"
          f"  floor={loaded.hard_floor or '—'}")
    print("")
    for name in ("telemetry", "intake", "graph", "routing"):
        sig = loaded.signals.get(name)
        w = loaded.weights.get(name, 0.0)
        if sig is None:
            print(f"  {name}: MISSING")
            continue
        print(f"  {name}: score={sig.score:.2f} weight={w:.2f}")
        for k, v in sig.evidence.items():
            print(f"    {k}: {v}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    sidecar_dir = default_sidecar_dir()
    if not sidecar_dir.is_dir():
        print("no quality data yet (run recompute --all)", file=sys.stderr)
        return 0

    rows: list[dict[str, Any]] = []
    for p in sorted(sidecar_dir.glob("*.json")):
        # Skip lifecycle sidecars written by ctx_lifecycle — they use the
        # pattern <slug>.lifecycle.json and lack a quality_grade field.
        if p.name.endswith(".lifecycle.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if "quality_grade" not in data and "grade" not in data:
            # Defensive: skip any sidecar that lacks both grade fields.
            continue
        rows.append(data)

    if args.grade:
        rows = [r for r in rows if r.get("grade") == args.grade]

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in sorted(rows, key=lambda x: (x.get("grade", "Z"), x.get("slug", ""))):
            print(f"{r.get('grade', '?')}  {r.get('slug', '?'):<40} "
                  f"score={float(r.get('score', 0)):.2f}")
        print(f"{len(rows)} entries", file=sys.stderr)
    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skill_quality",
        description="Score + persist quality for installed skills and agents.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("recompute", help="Recompute quality for one or more slugs")
    r.add_argument("slugs_positional", nargs="*", metavar="SLUG",
                   help="one or more slugs to recompute (positional)")
    r.add_argument("--all", action="store_true", help="recompute every installed slug")
    r.add_argument("--slug", help="recompute a single slug")
    r.add_argument("--slugs", help="comma-separated list of slugs")
    r.add_argument("--json", action="store_true", help="emit JSON result")
    r.set_defaults(func=cmd_recompute)

    s = sub.add_parser("show", help="Show the persisted score for a slug")
    s.add_argument("slug")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    e = sub.add_parser("explain", help="Print signal breakdown + evidence")
    e.add_argument("slug")
    e.set_defaults(func=cmd_explain)

    ls = sub.add_parser("list", help="List every slug with its grade")
    ls.add_argument("--grade", help="filter by grade (A/B/C/D/F)")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_argparser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "QualityConfig",
    "QualityScore",
    "QualitySink",
    "SidecarSink",
    "SignalSources",
    "WikiBodySink",
    "WikiFrontmatterSink",
    "compute_quality",
    "default_sidecar_dir",
    "discover_slugs",
    "extract_signals_for_slug",
    "load_quality",
    "main",
    "persist_quality",
    "recompute_all",
    "recompute_slug",
    "sidecar_path",
]
