#!/usr/bin/env python3
"""
ctx_lifecycle.py -- Propose-and-confirm lifecycle CLI for the quality corpus.

Phase 4 of the skill-quality plan (see ``docs/roadmap/skill-quality.md``).

Four tiers, asymmetric by design:

    active ─C─► watch ─D×N─► demote ─Δt─► archive ─Δt + purge─► deleted

  - Watch   : grade C → tag in frontmatter, surface in next review (auto-ok).
  - Demote  : grade D for N consecutive recomputes → move to ``skills/_demoted/``.
              The router excludes ``_demoted/**`` and ``_archive/**`` via
              existing path-based filters.
  - Archive : demoted for > ``archive_threshold_days`` → move to
              ``skills/_archive/``, drop the sidecar but keep the tree so
              ``--review-archived`` can git-diff it.
  - Delete  : archived for > ``delete_threshold_days`` AND user types the
              slug at the prompt. No ``--auto`` override for delete.

Propose-and-confirm on every action. ``--auto`` unlocks Watch + Demote only.

Lifecycle state lives in a sidecar next to the quality sidecar so score
recomputes and lifecycle transitions evolve independently:

    ~/.claude/skill-quality/<slug>.lifecycle.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctx.utils._fs_utils import atomic_write_text as _atomic_write
from skill_quality import (
    QualityScore,
    default_sidecar_dir,
    load_quality,
    sidecar_path,
)
from ctx.core.wiki.wiki_utils import SAFE_NAME_RE as _SLUG_RE

_logger = logging.getLogger(__name__)


def _ensure_safe_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(f"invalid lifecycle slug: {slug!r}")
    return slug


# ────────────────────────────────────────────────────────────────────
# Config + state types
# ────────────────────────────────────────────────────────────────────


STATE_ACTIVE = "active"
STATE_WATCH = "watch"
STATE_DEMOTE = "demote"
STATE_ARCHIVE = "archive"

# Grades that keep the D-streak going. We deliberately include F so a
# broken skill drops through the pipeline at the same cadence as a stale
# one — both are "this should not be in the router" signals.
_NEGATIVE_GRADES = {"D", "F"}


@dataclass(frozen=True)
class LifecycleConfig:
    """All lifecycle knobs — frozen so tests cannot mutate by accident."""

    archive_threshold_days: float = 14.0
    delete_threshold_days: float = 60.0
    consecutive_d_to_demote: int = 2
    demoted_subdir: str = "_demoted"
    archive_subdir: str = "_archive"
    history_max: int = 20

    def __post_init__(self) -> None:
        if self.archive_threshold_days <= 0:
            raise ValueError("archive_threshold_days must be > 0")
        if self.delete_threshold_days <= 0:
            raise ValueError("delete_threshold_days must be > 0")
        if self.consecutive_d_to_demote < 1:
            raise ValueError("consecutive_d_to_demote must be >= 1")
        if self.history_max < 0:
            raise ValueError("history_max must be >= 0")
        if not self.demoted_subdir or "/" in self.demoted_subdir or "\\" in self.demoted_subdir:
            raise ValueError("demoted_subdir must be a single path segment")
        if not self.archive_subdir or "/" in self.archive_subdir or "\\" in self.archive_subdir:
            raise ValueError("archive_subdir must be a single path segment")


@dataclass(frozen=True)
class LifecycleSources:
    """Paths the lifecycle CLI reads from and writes to."""

    skills_dir: Path
    agents_dir: Path
    sidecar_dir: Path

    def demoted_dir(self, cfg: LifecycleConfig) -> Path:
        return self.skills_dir / cfg.demoted_subdir

    def archive_dir(self, cfg: LifecycleConfig) -> Path:
        return self.skills_dir / cfg.archive_subdir


@dataclass(frozen=True)
class LifecycleState:
    """One slug's lifecycle position, persisted to a sidecar."""

    slug: str
    subject_type: str
    state: str = STATE_ACTIVE
    state_since: str = ""                # ISO-8601 UTC; when state last changed
    consecutive_d_count: int = 0         # resets to 0 on any non-negative grade
    last_grade: str = ""
    last_seen_computed_at: str = ""      # most recent score.computed_at we saw
    history: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "subject_type": self.subject_type,
            "state": self.state,
            "state_since": self.state_since,
            "consecutive_d_count": self.consecutive_d_count,
            "last_grade": self.last_grade,
            "last_seen_computed_at": self.last_seen_computed_at,
            "history": [dict(e) for e in self.history],
        }


@dataclass(frozen=True)
class Proposal:
    """One proposed state transition. The CLI asks the user before applying."""

    slug: str
    subject_type: str
    current_state: str
    target_state: str
    reason: str
    requires_typed_confirmation: bool = False  # True for Delete only
    auto_safe: bool = True                     # False for Archive + Delete

    def describe(self) -> str:
        return f"{self.current_state} → {self.target_state}  {self.slug}  ({self.reason})"


# ────────────────────────────────────────────────────────────────────
# State sidecar persistence
# ────────────────────────────────────────────────────────────────────


def lifecycle_sidecar_path(slug: str, *, sidecar_dir: Path | None = None) -> Path:
    _ensure_safe_slug(slug)
    root = sidecar_dir if sidecar_dir is not None else default_sidecar_dir()
    return root / f"{slug}.lifecycle.json"


def save_lifecycle_state(
    state: LifecycleState, *, sidecar_dir: Path | None = None
) -> Path:
    path = lifecycle_sidecar_path(state.slug, sidecar_dir=sidecar_dir)
    _atomic_write(
        path,
        json.dumps(state.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
    )
    return path


def load_lifecycle_state(
    slug: str, *, sidecar_dir: Path | None = None
) -> LifecycleState | None:
    path = lifecycle_sidecar_path(slug, sidecar_dir=sidecar_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    history_raw = data.get("history", [])
    history = tuple(
        dict(e) for e in history_raw if isinstance(e, dict)
    )
    return LifecycleState(
        slug=data.get("slug", slug),
        subject_type=data.get("subject_type", "skill"),
        state=data.get("state", STATE_ACTIVE),
        state_since=data.get("state_since", ""),
        consecutive_d_count=int(data.get("consecutive_d_count", 0)),
        last_grade=data.get("last_grade", ""),
        last_seen_computed_at=data.get("last_seen_computed_at", ""),
        history=history,
    )


def _ensure_state(
    slug: str, subject_type: str, *, sidecar_dir: Path | None = None
) -> LifecycleState:
    existing = load_lifecycle_state(slug, sidecar_dir=sidecar_dir)
    if existing is not None:
        return existing
    return LifecycleState(slug=slug, subject_type=subject_type)


# ────────────────────────────────────────────────────────────────────
# Pure state transitions
# ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _append_history(
    state: LifecycleState,
    *,
    event: str,
    note: str,
    cfg: LifecycleConfig,
    at: str | None = None,
) -> tuple[dict[str, Any], ...]:
    entry = {"at": at or _now_iso(), "event": event, "note": note}
    trimmed = (state.history + (entry,))[-cfg.history_max:]
    return trimmed


def observe_score(
    state: LifecycleState,
    score: QualityScore,
    *,
    cfg: LifecycleConfig | None = None,
) -> LifecycleState:
    """Fold a fresh ``QualityScore`` into ``state``.

    Updates the D-streak and last-grade fields only. Does *not* move the
    state tier itself — ``classify_transition`` + ``apply_proposal`` do
    that, so this step is safe to call on every recompute.

    Idempotent: if ``score.computed_at <= state.last_seen_computed_at``
    we return ``state`` unchanged. That way the stop hook can fold
    repeatedly without double-counting.
    """
    cfg = cfg or LifecycleConfig()
    new_ts = _parse_iso(score.computed_at)
    # Guard: treat a missing/empty computed_at as a no-op.  Without a
    # parseable timestamp we cannot determine ordering, so incrementing
    # the streak would silently corrupt demotion state.
    if new_ts is None:
        return state
    seen_ts = _parse_iso(state.last_seen_computed_at)
    if seen_ts is not None and new_ts <= seen_ts:
        return state

    if score.grade in _NEGATIVE_GRADES:
        new_streak = state.consecutive_d_count + 1
    else:
        new_streak = 0

    return replace(
        state,
        consecutive_d_count=new_streak,
        last_grade=score.grade,
        last_seen_computed_at=score.computed_at or _now_iso(),
    )


def classify_transition(
    state: LifecycleState,
    score: QualityScore | None,
    *,
    cfg: LifecycleConfig | None = None,
    now: datetime | None = None,
    include_delete: bool = False,
) -> Proposal | None:
    """Return the single next transition proposal for ``state``, if any.

    ``include_delete=True`` is required for the Delete tier to surface —
    the ``purge`` subcommand sets it, ``review`` does not. This keeps the
    destructive action out of the default review flow.
    """
    cfg = cfg or LifecycleConfig()
    ts_now = now or datetime.now(timezone.utc)

    if state.state == STATE_ARCHIVE:
        since = _parse_iso(state.state_since)
        if include_delete and since is not None:
            age = (ts_now - since).total_seconds() / 86400.0
            if age >= cfg.delete_threshold_days:
                return Proposal(
                    slug=state.slug,
                    subject_type=state.subject_type,
                    current_state=STATE_ARCHIVE,
                    target_state="deleted",
                    reason=f"archived {age:.1f}d ago (>{cfg.delete_threshold_days:.0f}d)",
                    requires_typed_confirmation=True,
                    auto_safe=False,
                )
        return None

    if state.state == STATE_DEMOTE:
        since = _parse_iso(state.state_since)
        if since is not None:
            age = (ts_now - since).total_seconds() / 86400.0
            if age >= cfg.archive_threshold_days:
                return Proposal(
                    slug=state.slug,
                    subject_type=state.subject_type,
                    current_state=STATE_DEMOTE,
                    target_state=STATE_ARCHIVE,
                    reason=f"demoted {age:.1f}d ago (>{cfg.archive_threshold_days:.0f}d)",
                    auto_safe=False,
                )
        return None

    # active or watch: look at latest grade
    if score is None:
        return None

    if (
        score.grade in _NEGATIVE_GRADES
        and state.consecutive_d_count >= cfg.consecutive_d_to_demote
    ):
        return Proposal(
            slug=state.slug,
            subject_type=state.subject_type,
            current_state=state.state,
            target_state=STATE_DEMOTE,
            reason=(
                f"grade {score.grade} for {state.consecutive_d_count} consecutive "
                f"recomputes (threshold {cfg.consecutive_d_to_demote})"
            ),
        )

    if score.grade == "C" and state.state != STATE_WATCH:
        return Proposal(
            slug=state.slug,
            subject_type=state.subject_type,
            current_state=state.state,
            target_state=STATE_WATCH,
            reason=f"grade dropped to C (score {score.score:.2f})",
        )

    return None


# ────────────────────────────────────────────────────────────────────
# Filesystem side-effects
# ────────────────────────────────────────────────────────────────────


def _resolve_entity_root(
    slug: str, subject_type: str, sources: LifecycleSources
) -> Path | None:
    """Return the canonical active location of the slug's source dir/file."""
    _ensure_safe_slug(slug)
    if subject_type == "skill":
        candidate = sources.skills_dir / slug
        if candidate.is_dir():
            return candidate
    else:
        candidate = sources.agents_dir / f"{slug}.md"
        if candidate.is_file():
            return candidate
    return None


def _safe_mv(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst``. Refuses to overwrite an existing target."""
    if not src.exists():
        raise FileNotFoundError(f"lifecycle mv: source missing: {src}")
    if dst.exists():
        raise FileExistsError(f"lifecycle mv: target exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def apply_proposal(
    proposal: Proposal,
    state: LifecycleState,
    *,
    sources: LifecycleSources,
    cfg: LifecycleConfig | None = None,
    now: datetime | None = None,
) -> LifecycleState:
    """Execute the filesystem move + state-sidecar update for one proposal.

    Returns the new ``LifecycleState``. Caller is responsible for saving
    it via ``save_lifecycle_state``. This split lets tests verify pure
    state evolution without writing to disk.
    """
    cfg = cfg or LifecycleConfig()
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    if proposal.target_state == STATE_WATCH:
        # No filesystem move for watch — the frontmatter flag is enough.
        return replace(
            state,
            state=STATE_WATCH,
            state_since=ts,
            history=_append_history(
                state, event="watch", note=proposal.reason, cfg=cfg, at=ts
            ),
        )

    if proposal.target_state == STATE_DEMOTE:
        entity = _resolve_entity_root(proposal.slug, proposal.subject_type, sources)
        if entity is None:
            # Already demoted or archived elsewhere — just advance state.
            _logger.warning(
                "demote: entity not at active location for %s; "
                "advancing state only", proposal.slug
            )
        else:
            target = sources.demoted_dir(cfg) / entity.name
            _safe_mv(entity, target)
        return replace(
            state,
            state=STATE_DEMOTE,
            state_since=ts,
            history=_append_history(
                state, event="demote", note=proposal.reason, cfg=cfg, at=ts
            ),
        )

    if proposal.target_state == STATE_ARCHIVE:
        demoted_root = sources.demoted_dir(cfg)
        # Entity name preserves the original folder/file name (SKILL.md
        # lives inside the folder, so name is the slug dir).
        if proposal.subject_type == "skill":
            src = demoted_root / proposal.slug
        else:
            src = demoted_root / f"{proposal.slug}.md"
        if not src.exists():
            _logger.warning(
                "archive: source missing under %s; advancing state only", src
            )
        else:
            target = sources.archive_dir(cfg) / src.name
            _safe_mv(src, target)
        return replace(
            state,
            state=STATE_ARCHIVE,
            state_since=ts,
            history=_append_history(
                state, event="archive", note=proposal.reason, cfg=cfg, at=ts
            ),
        )

    if proposal.target_state == "deleted":
        archive_root = sources.archive_dir(cfg)
        if proposal.subject_type == "skill":
            src = archive_root / proposal.slug
        else:
            src = archive_root / f"{proposal.slug}.md"
        if src.exists():
            if src.is_dir():
                shutil.rmtree(src)
            else:
                src.unlink()
        # Drop the quality + lifecycle sidecars too.
        for path in (
            sidecar_path(proposal.slug, sidecar_dir=sources.sidecar_dir),
            lifecycle_sidecar_path(proposal.slug, sidecar_dir=sources.sidecar_dir),
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        # Return a tombstone state so callers still see the history.
        return replace(
            state,
            state="deleted",
            state_since=ts,
            history=_append_history(
                state, event="delete", note=proposal.reason, cfg=cfg, at=ts
            ),
        )

    raise ValueError(f"unknown target state: {proposal.target_state!r}")


def promote_archived(
    slug: str,
    *,
    sources: LifecycleSources,
    cfg: LifecycleConfig | None = None,
    now: datetime | None = None,
) -> LifecycleState:
    """Move a slug from archive back to the active skills dir.

    Asymmetric by design — only the ``--review-archived`` flow calls
    this. Raises ``FileNotFoundError`` if the archived copy is missing.
    """
    _ensure_safe_slug(slug)
    cfg = cfg or LifecycleConfig()
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    state = _ensure_state(slug, "skill", sidecar_dir=sources.sidecar_dir)
    archive_root = sources.archive_dir(cfg)
    if state.subject_type == "skill":
        src = archive_root / slug
        dst = sources.skills_dir / slug
    else:
        src = archive_root / f"{slug}.md"
        dst = sources.agents_dir / f"{slug}.md"
    _safe_mv(src, dst)
    new_state = replace(
        state,
        state=STATE_ACTIVE,
        state_since=ts,
        consecutive_d_count=0,
        history=_append_history(
            state, event="promote", note="restored from archive", cfg=cfg, at=ts
        ),
    )
    save_lifecycle_state(new_state, sidecar_dir=sources.sidecar_dir)
    return new_state


# ────────────────────────────────────────────────────────────────────
# Discovery
# ────────────────────────────────────────────────────────────────────


def _iter_sidecars(sidecar_dir: Path) -> list[Path]:
    if not sidecar_dir.is_dir():
        return []
    return [
        p for p in sorted(sidecar_dir.glob("*.json"))
        # Dotfiles (e.g. .hook-state.json) are internal state, not slugs;
        # lifecycle sidecars have their own iterator.
        if not p.name.startswith(".")
        and not p.name.endswith(".lifecycle.json")
    ]


def _iter_lifecycle_sidecars(sidecar_dir: Path) -> list[Path]:
    if not sidecar_dir.is_dir():
        return []
    return sorted(sidecar_dir.glob("*.lifecycle.json"))


def plan_review(
    *,
    sources: LifecycleSources,
    cfg: LifecycleConfig | None = None,
    now: datetime | None = None,
    include_delete: bool = False,
) -> tuple[list[Proposal], dict[str, LifecycleState]]:
    """Walk all known quality sidecars and classify pending transitions.

    Observes the latest score into each state first so the D-streak is
    up to date, then classifies. Returns (proposals, observed_states).
    The caller decides whether to persist the observed states (``review``
    does after confirmation; dry-run callers can discard).
    """
    cfg = cfg or LifecycleConfig()
    proposals: list[Proposal] = []
    observed: dict[str, LifecycleState] = {}
    for path in _iter_sidecars(sources.sidecar_dir):
        slug = path.stem
        if not _SLUG_RE.match(slug):
            continue
        score = load_quality(slug, sidecar_dir=sources.sidecar_dir)
        if score is None:
            continue
        state = _ensure_state(slug, score.subject_type, sidecar_dir=sources.sidecar_dir)
        state = observe_score(state, score, cfg=cfg)
        observed[slug] = state
        proposal = classify_transition(
            state, score, cfg=cfg, now=now, include_delete=include_delete
        )
        if proposal is not None:
            proposals.append(proposal)
    # Also walk lifecycle sidecars for slugs already past the active
    # tier — these might have no quality sidecar anymore (e.g. deleted
    # quality sidecar under archive) but still need archive→delete eval.
    for path in _iter_lifecycle_sidecars(sources.sidecar_dir):
        slug = path.name[: -len(".lifecycle.json")]
        if slug in observed:
            continue
        if not _SLUG_RE.match(slug):
            continue
        lc_state: LifecycleState | None = load_lifecycle_state(
            slug, sidecar_dir=sources.sidecar_dir
        )
        if lc_state is None:
            continue
        observed[slug] = lc_state
        proposal = classify_transition(
            lc_state, None, cfg=cfg, now=now, include_delete=include_delete
        )
        if proposal is not None:
            proposals.append(proposal)
    return proposals, observed


# ────────────────────────────────────────────────────────────────────
# User-facing CLI (interactive confirmation)
# ────────────────────────────────────────────────────────────────────


def _prompt_yes_no(question: str, *, default_yes: bool = False) -> bool:
    suffix = " [Y/n] " if default_yes else " [y/N] "
    try:
        resp = input(question + suffix).strip().lower()
    except EOFError:
        return False
    if not resp:
        return default_yes
    return resp in ("y", "yes")


def _prompt_typed(expected: str, question: str) -> bool:
    try:
        resp = input(question).strip()
    except EOFError:
        return False
    return resp == expected


def _partition(
    proposals: list[Proposal],
) -> dict[str, list[Proposal]]:
    buckets: dict[str, list[Proposal]] = {
        STATE_WATCH: [], STATE_DEMOTE: [], STATE_ARCHIVE: [], "deleted": [],
    }
    for p in proposals:
        buckets.setdefault(p.target_state, []).append(p)
    return buckets


def _git_diff_preview(path: Path, *, max_lines: int = 40) -> str:
    """Best-effort ``git log -p`` snippet for an archived file or dir.

    We call git from inside the repo root so the relative path resolves.
    Return value is purely informational; absence of git or a history
    for this path is not an error.

    Strix finding vuln-0003 (Git Textconv RCE): ``git log -p`` on
    repository-controlled content honors ``.gitattributes`` +
    ``diff.<name>.textconv`` hooks, which can execute arbitrary commands
    set by a malicious repo author. Disarm by:
      - ``--no-textconv``  — skip diff.textconv drivers entirely
      - ``--no-ext-diff``  — skip user-configured external diff tools
      - ``-c diff.external=`` — belt-and-braces env-reset
      - ``-c core.attributesfile=/dev/null`` — ignore any user-level
        gitattributes that might pull in a textconv indirectly
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-c", "diff.external=",
                "-c", "core.attributesfile=" + (os.devnull or "/dev/null"),
                "log",
                "-p",
                "--no-textconv",
                "--no-ext-diff",
                "--max-count=1",
                "--",
                str(path),
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    out = proc.stdout.strip()
    if not out:
        return ""
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… ({len(out.splitlines()) - max_lines} more lines)"]
    return "\n".join(lines)


def _build_sources() -> LifecycleSources:
    from ctx_config import cfg as app_cfg
    return LifecycleSources(
        skills_dir=app_cfg.skills_dir,
        agents_dir=app_cfg.agents_dir,
        sidecar_dir=default_sidecar_dir(),
    )


def _build_config() -> LifecycleConfig:
    from ctx_config import cfg as app_cfg
    raw = app_cfg.get("quality", {}) or {}
    lc = raw.get("lifecycle", {}) if isinstance(raw, dict) else {}
    kwargs: dict[str, Any] = {}
    if isinstance(lc, dict):
        if isinstance(lc.get("archive_threshold_days"), (int, float)):
            kwargs["archive_threshold_days"] = float(lc["archive_threshold_days"])
        if isinstance(lc.get("delete_threshold_days"), (int, float)):
            kwargs["delete_threshold_days"] = float(lc["delete_threshold_days"])
        if isinstance(lc.get("consecutive_d_to_demote"), int):
            kwargs["consecutive_d_to_demote"] = int(lc["consecutive_d_to_demote"])
        if isinstance(lc.get("demoted_subdir"), str):
            kwargs["demoted_subdir"] = lc["demoted_subdir"]
        if isinstance(lc.get("archive_subdir"), str):
            kwargs["archive_subdir"] = lc["archive_subdir"]
        if isinstance(lc.get("history_max"), int):
            kwargs["history_max"] = lc["history_max"]
    return LifecycleConfig(**kwargs)


def cmd_review(args: argparse.Namespace) -> int:
    sources = _build_sources()
    cfg = _build_config()
    proposals, observed = plan_review(
        sources=sources, cfg=cfg, include_delete=False
    )

    # Persist observed states regardless — folding the fresh D-streak
    # keeps the counter correct even if the user declines transitions.
    for state in observed.values():
        save_lifecycle_state(state, sidecar_dir=sources.sidecar_dir)

    if args.json:
        print(json.dumps(
            {
                "proposals": [p.__dict__ for p in proposals],
                "state_count": len(observed),
            },
            indent=2,
            default=str,
        ))
        return 0

    if not proposals:
        print("Nothing to propose. Corpus is healthy.")
        return 0

    buckets = _partition(proposals)
    for tier in (STATE_WATCH, STATE_DEMOTE, STATE_ARCHIVE):
        items = buckets.get(tier, [])
        if not items:
            continue
        print(f"\n# {tier.upper()} ({len(items)})")
        for p in items:
            print(f"  - {p.describe()}")

    if args.dry_run:
        print("\n(dry-run: no changes applied)")
        return 0

    applied = _apply_buckets(
        buckets, observed,
        sources=sources, cfg=cfg, auto=args.auto,
    )
    print(f"\nApplied {applied} transition(s).")
    return 0


def _apply_buckets(
    buckets: dict[str, list[Proposal]],
    states: dict[str, LifecycleState],
    *,
    sources: LifecycleSources,
    cfg: LifecycleConfig,
    auto: bool,
) -> int:
    applied = 0
    # Watch + Demote are auto-safe; Archive requires explicit y/N.
    for tier in (STATE_WATCH, STATE_DEMOTE):
        items = buckets.get(tier, [])
        if not items:
            continue
        if not auto:
            if not _prompt_yes_no(f"Apply {len(items)} {tier} transition(s)?"):
                continue
        for p in items:
            applied += _apply_one(p, states, sources=sources, cfg=cfg)

    for p in buckets.get(STATE_ARCHIVE, []):
        print(f"\nArchive candidate: {p.describe()}")
        if auto:
            # Under --auto, archive always requires explicit confirmation.
            # Log the candidate and defer — the user can run `review`
            # interactively to action it.
            _logger.info(
                "auto mode: skipping archive prompt for %s; "
                "run `review` interactively to apply",
                p.slug,
            )
            print("  (auto: deferred — run review interactively to archive)")
            continue
        if not _prompt_yes_no("  Archive this entry?", default_yes=False):
            continue
        applied += _apply_one(p, states, sources=sources, cfg=cfg)
    return applied


def _apply_one(
    proposal: Proposal,
    states: dict[str, LifecycleState],
    *,
    sources: LifecycleSources,
    cfg: LifecycleConfig,
) -> int:
    state = states.get(proposal.slug)
    if state is None:
        state = _ensure_state(
            proposal.slug, proposal.subject_type, sidecar_dir=sources.sidecar_dir
        )
    try:
        new_state = apply_proposal(proposal, state, sources=sources, cfg=cfg)
    except (FileNotFoundError, FileExistsError, OSError) as exc:
        print(f"  ! failed: {proposal.slug}: {exc}", file=sys.stderr)
        return 0
    save_lifecycle_state(new_state, sidecar_dir=sources.sidecar_dir)
    states[proposal.slug] = new_state
    print(f"  ✓ {proposal.target_state}: {proposal.slug}")

    # Unified audit log entry. target_state is one of
    # active / watch / demote / archive / delete — map to the
    # {skill,agent}.<verb> event vocabulary. Restore is the special
    # case where target is active coming from archive.
    try:
        from ctx_audit_log import log
        verb_map = {
            "active": "restored" if proposal.current_state == "archive" else "added",
            "watch": "watched",
            "demote": "demoted",
            "archive": "archived",
            "delete": "deleted",
        }
        verb = verb_map.get(proposal.target_state)
        if verb:
            subject_type = proposal.subject_type if proposal.subject_type in ("skill", "agent") else "skill"
            log(
                f"{subject_type}.{verb}",
                subject_type=subject_type,
                subject=proposal.slug,
                actor="lifecycle",
                meta={
                    "from": proposal.current_state,
                    "to": proposal.target_state,
                    "reason": proposal.reason,
                },
            )
    except Exception:  # noqa: BLE001 — audit best-effort
        pass

    return 1


def cmd_demote(args: argparse.Namespace) -> int:
    sources = _build_sources()
    cfg = _build_config()
    slug = _ensure_safe_slug(args.slug)
    score = load_quality(slug, sidecar_dir=sources.sidecar_dir)
    state = _ensure_state(
        slug, score.subject_type if score else "skill",
        sidecar_dir=sources.sidecar_dir,
    )
    proposal = Proposal(
        slug=slug,
        subject_type=state.subject_type,
        current_state=state.state,
        target_state=STATE_DEMOTE,
        reason="explicit demote via CLI",
    )
    if not args.force and not _prompt_yes_no(f"Demote {slug}?"):
        print("Aborted.")
        return 1
    return 0 if _apply_one(
        proposal, {slug: state}, sources=sources, cfg=cfg
    ) else 1


def cmd_archive(args: argparse.Namespace) -> int:
    sources = _build_sources()
    cfg = _build_config()
    slug = _ensure_safe_slug(args.slug)
    state = load_lifecycle_state(slug, sidecar_dir=sources.sidecar_dir)
    if state is None or state.state != STATE_DEMOTE:
        print(f"{slug}: cannot archive — not in demote state "
              f"(current={state.state if state else 'active'})", file=sys.stderr)
        return 1
    proposal = Proposal(
        slug=slug,
        subject_type=state.subject_type,
        current_state=STATE_DEMOTE,
        target_state=STATE_ARCHIVE,
        reason="explicit archive via CLI",
        auto_safe=False,
    )
    if not args.force and not _prompt_yes_no(f"Archive {slug}?"):
        print("Aborted.")
        return 1
    return 0 if _apply_one(
        proposal, {slug: state}, sources=sources, cfg=cfg
    ) else 1


def cmd_purge(args: argparse.Namespace) -> int:
    sources = _build_sources()
    cfg = _build_config()
    proposals, observed = plan_review(
        sources=sources, cfg=cfg, include_delete=True
    )
    delete_candidates = [p for p in proposals if p.target_state == "deleted"]
    if not delete_candidates:
        print("Nothing to purge.")
        return 0

    print(f"\n{len(delete_candidates)} archived entries are purge-eligible:")
    for p in delete_candidates:
        print(f"  - {p.describe()}")
    print("\n*** DELETE IS PERMANENT. Type the exact slug to confirm each. ***")

    applied = 0
    for p in delete_candidates:
        if not _prompt_typed(p.slug, f"Type {p.slug!r} to delete: "):
            print(f"  skipped: {p.slug}")
            continue
        applied += _apply_one(p, observed, sources=sources, cfg=cfg)
    print(f"\nPurged {applied} entries.")
    return 0


def cmd_review_archived(args: argparse.Namespace) -> int:
    sources = _build_sources()
    cfg = _build_config()
    archived: list[LifecycleState] = []
    for path in _iter_lifecycle_sidecars(sources.sidecar_dir):
        slug = path.name[: -len(".lifecycle.json")]
        state = load_lifecycle_state(slug, sidecar_dir=sources.sidecar_dir)
        if state is not None and state.state == STATE_ARCHIVE:
            archived.append(state)

    if not archived:
        print("No archived entries to review.")
        return 0

    if args.json:
        print(json.dumps([s.to_dict() for s in archived], indent=2))
        return 0

    print(f"{len(archived)} archived entries:\n")
    for state in archived:
        if state.subject_type == "skill":
            path = sources.archive_dir(cfg) / state.slug
        else:
            path = sources.archive_dir(cfg) / f"{state.slug}.md"
        print(f"  {state.slug}  (archived {state.state_since})")
        if args.show_diff:
            snippet = _git_diff_preview(path)
            if snippet:
                indented = "\n".join("      " + ln for ln in snippet.splitlines())
                print(indented)
        if args.restore and _prompt_yes_no(f"  Restore {state.slug}?"):
            try:
                promote_archived(state.slug, sources=sources, cfg=cfg)
                print(f"  ✓ restored: {state.slug}")
            except (FileNotFoundError, FileExistsError, OSError) as exc:
                print(f"  ! restore failed: {exc}", file=sys.stderr)
    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ctx_lifecycle",
        description="Propose-and-confirm lifecycle CLI for the skill corpus.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("review", help="Classify transitions for the whole corpus")
    r.add_argument("--auto", action="store_true",
                   help="auto-apply watch + demote without prompting")
    r.add_argument("--dry-run", action="store_true",
                   help="print proposals but do not apply anything")
    r.add_argument("--json", action="store_true", help="emit JSON")
    r.set_defaults(func=cmd_review)

    d = sub.add_parser("demote", help="Demote one slug explicitly")
    d.add_argument("slug")
    d.add_argument("--force", action="store_true", help="skip the confirmation prompt")
    d.set_defaults(func=cmd_demote)

    a = sub.add_parser("archive", help="Archive one already-demoted slug")
    a.add_argument("slug")
    a.add_argument("--force", action="store_true", help="skip the confirmation prompt")
    a.set_defaults(func=cmd_archive)

    pu = sub.add_parser("purge", help="Delete archived entries past the threshold")
    pu.set_defaults(func=cmd_purge)

    ra = sub.add_parser("review-archived",
                        help="List archived entries with optional diff + restore")
    ra.add_argument("--show-diff", action="store_true",
                    help="show git-log preview for each archived entry")
    ra.add_argument("--restore", action="store_true",
                    help="prompt for restore after each entry")
    ra.add_argument("--json", action="store_true")
    ra.set_defaults(func=cmd_review_archived)

    return p


def main(argv: list[str] | None = None) -> int:
    # Force UTF-8 on stdout/stderr so the Unicode arrows in
    # ``TransitionPlan.describe()`` don't crash on Windows' default
    # cp1252 console. No-op on POSIX. We use errors='replace' as a
    # belt-and-braces fallback for any legacy-codepage edge case.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass  # stream may be a pipe that doesn't support reconfigure
    parser = build_argparser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "LifecycleConfig",
    "LifecycleSources",
    "LifecycleState",
    "Proposal",
    "STATE_ACTIVE",
    "STATE_ARCHIVE",
    "STATE_DEMOTE",
    "STATE_WATCH",
    "apply_proposal",
    "classify_transition",
    "lifecycle_sidecar_path",
    "load_lifecycle_state",
    "main",
    "observe_score",
    "plan_review",
    "promote_archived",
    "save_lifecycle_state",
]
