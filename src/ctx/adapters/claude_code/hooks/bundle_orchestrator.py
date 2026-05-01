#!/usr/bin/env python3
"""
bundle_orchestrator.py -- PostToolUse hook: surface a cross-type bundle recommendation.

Replaces the older name ``skill_suggest.py`` which was a misnomer —
this hook produces a BUNDLE that may span any combination of the
three execution entity types tracked by the system:

  - skills   (converted long-form pipelines OR short single-file skills)
  - agents   (Claude Code agents)
  - mcp-servers (registered MCP servers the user has or could install)

The graph-based scorer in ``context_monitor.graph_suggest`` ranks all
three execution types uniformly against the current unmatched signals; whatever
scores highest wins. A bundle of 5 might be 3 skills + 1 agent + 1
MCP, or all-skills, or all-agents, or any mix. It's dynamic.

This module:
  - reads ``~/.claude/pending-skills.json`` (written by
    ``context_monitor.write_pending_skills`` when unmatched-signal
    cumulative threshold fires)
  - reads ``~/.claude/pending-unload.json`` (written by
    ``ctx_lifecycle`` when stale entities should be unloaded)
  - caps both lists at ``cfg.recommendation_top_k`` (default 5)
  - categorises the output by entity type so the user sees a
    structured "Skills / Agents / MCPs" recommendation rather than
    a flat list
  - emits a Claude-Code-hook JSON payload to stdout
  - marks the suggestion as shown so it isn't re-emitted on every
    PostToolUse in the same session

The user decides whether to approve the bundle. Nothing auto-loads.

Called from Claude Code PostToolUse hook:
    python bundle_orchestrator.py 2>/dev/null || true

Backward compatibility: ``skill_suggest.py`` stays on disk as a thin
shim that just calls ``bundle_orchestrator.main`` — existing
``~/.claude/settings.json`` hook configs keep working.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
PENDING_SKILLS = CLAUDE_DIR / "pending-skills.json"
PENDING_UNLOAD = CLAUDE_DIR / "pending-unload.json"
SHOWN_FLAG = CLAUDE_DIR / ".bundle-suggest-shown"

# Entity-type display ordering for the execution bundle. Harnesses are
# recommended through ctx-init / ctx-harness-install, not Claude Code hooks.
_TYPE_ORDER: tuple[str, ...] = ("skill", "agent", "mcp-server")
_TYPE_DISPLAY: dict[str, str] = {
    "skill": "Skills",
    "agent": "Agents",
    "mcp-server": "MCPs",
}
# Install CLI hint per type — surfaced in the message so the user
# knows how to act on each category.
_TYPE_INSTALL_CLI: dict[str, str] = {
    "skill": "ctx-skill-install <slug>",
    "agent": "ctx-agent-install <slug>",
    "mcp-server": "ctx-mcp-install <slug> --cmd '...'",
}


# ── Session-shown flag ───────────────────────────────────────────────────────


def already_shown_this_session() -> bool:
    """True when we already surfaced suggestions during this session.

    Guards against hook spam — every PostToolUse would re-emit the
    same JSON payload otherwise. The flag is invalidated when the
    pending-skills file is newer (a new batch of unmatched signals
    fired the threshold).
    """
    if not SHOWN_FLAG.exists():
        return False
    try:
        shown_data = json.loads(SHOWN_FLAG.read_text(encoding="utf-8"))
        shown_at = shown_data.get("shown_at", "")
        pending_at = ""
        if PENDING_SKILLS.exists():
            pending_data = json.loads(PENDING_SKILLS.read_text(encoding="utf-8"))
            pending_at = pending_data.get("generated_at", "")
        # Already shown if pending hasn't been refreshed since last show.
        return shown_at >= pending_at
    except Exception as exc:
        print(f"Warning: failed to check shown status: {exc}", file=sys.stderr)
        return False


def mark_shown() -> None:
    """Record that the bundle was shown so the next PostToolUse skips."""
    SHOWN_FLAG.write_text(
        json.dumps({"shown_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


# ── Bundle categorisation ────────────────────────────────────────────────────


def _top_k() -> int:
    """Resolve the bundle cap from config, defaulting to 5.

    Lazy-imports ctx_config so the hook stays importable in a
    minimal-env test where the full config chain isn't wired.
    """
    try:
        from ctx_config import cfg  # noqa: PLC0415
        k = int(cfg.recommendation_top_k)
    except Exception:
        k = 5
    return max(k, 1)


def categorise_bundle(
    suggestions: list[dict], *, top_k: int,
) -> dict[str, list[dict]]:
    """Group the top-``top_k`` suggestions by entity type.

    Input: a ranked list of dicts with ``name``, ``type``, ``score``,
    ``matching_tags`` fields (as produced by
    ``context_monitor.graph_suggest``).

    The bundle is a TOTAL top-K across all types — not top-K per
    type. A top-5 bundle might be {skills: 3, agents: 1, mcps: 1}
    or {skills: 5, agents: 0, mcps: 0}. The ordering of entries
    within each type preserves the graph score order.

    Returns a dict ``{entity_type: [entry, ...]}`` with entries for
    every type in ``_TYPE_ORDER``. Empty lists are included so the
    caller can render a consistent layout.
    """
    capped = suggestions[:top_k]
    grouped: dict[str, list[dict]] = {t: [] for t in _TYPE_ORDER}
    for entry in capped:
        etype = entry.get("type", "skill")
        if etype in grouped:
            grouped[etype].append(entry)
    return grouped


def render_bundle_message(
    graph_suggestions: list[dict],
    unmatched: list[str],
    unload_suggestions: list[dict],
    *,
    top_k: int,
) -> str:
    """Compose the user-facing recommendation message.

    Layout:
      - Leader line if any signals / bundle items exist
      - "Unmatched signals: ..." line for transparency
      - Bundle block grouped by type (Skills / Agents / MCPs), each
        with its install command hint
      - Unload block if the lifecycle flagged anything

    Types with zero entries in the bundle are OMITTED from output
    so the user doesn't see empty headers.
    """
    lines: list[str] = []

    if unmatched or graph_suggestions:
        lines.append("ctx detected stack signals not covered by your loaded entities.")
    if unmatched:
        lines.append(f"Unmatched signals: {', '.join(unmatched)}")

    if graph_suggestions:
        bundle = categorise_bundle(graph_suggestions, top_k=top_k)
        total = sum(len(v) for v in bundle.values())
        if total:
            lines.append("")
            lines.append(
                f"Suggested bundle (top {total} by graph score — "
                "skills, agents, and MCPs blend):"
            )
            for etype in _TYPE_ORDER:
                entries = bundle.get(etype, [])
                if not entries:
                    continue
                lines.append("")
                lines.append(f"  {_TYPE_DISPLAY[etype]}:")
                for s in entries:
                    tags = ", ".join(s.get("matching_tags", []))
                    score = s.get("score")
                    score_str = f" score={score}" if score is not None else ""
                    tag_str = f" (tags: {tags})" if tags else ""
                    lines.append(f"    - {s['name']}{score_str}{tag_str}")
                # Install-cli hint scoped to this type.
                lines.append(f"    install: {_TYPE_INSTALL_CLI[etype]}")
            lines.append("")
            lines.append(
                "A bundle can be any mix of the three types — whatever "
                "the graph ranks highest for your current signals wins."
            )

    if unload_suggestions:
        if lines:
            lines.append("")
        lines.append(
            "ctx detected entities (skills / agents / MCPs) that have "
            "been loaded but never used:"
        )
        for s in unload_suggestions[:top_k]:
            lines.append(f"  - {s['name']} ({s['reason']})")
        lines.append("")
        lines.append(
            "Tell the user: \"These entities have been loaded but "
            "unused. Want me to unload any of them?\""
        )

    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────


def _read_pending(path: Path, key: str) -> list[dict] | list[str]:
    """Load ``key`` from ``path``'s JSON. Tolerant: returns [] on any error."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        val = data.get(key, [])
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def main() -> None:
    if not PENDING_SKILLS.exists() and not PENDING_UNLOAD.exists():
        sys.exit(0)
    if already_shown_this_session():
        sys.exit(0)

    graph_suggestions = _read_pending(PENDING_SKILLS, "graph_suggestions")
    unmatched = _read_pending(PENDING_SKILLS, "unmatched_signals")
    unload_suggestions = _read_pending(PENDING_UNLOAD, "suggestions")

    # Defensive type narrowing — _read_pending says "list[dict] | list[str]"
    # but graph_suggestions/unload_suggestions should be list[dict].
    assert isinstance(graph_suggestions, list)
    assert isinstance(unmatched, list)
    assert isinstance(unload_suggestions, list)

    if not graph_suggestions and not unmatched and not unload_suggestions:
        sys.exit(0)

    message = render_bundle_message(
        graph_suggestions,  # type: ignore[arg-type]
        [str(u) for u in unmatched],
        unload_suggestions,  # type: ignore[arg-type]
        top_k=_top_k(),
    )
    if not message.strip():
        sys.exit(0)

    # Claude Code hook envelope.
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }
    print(json.dumps(output))
    mark_shown()


if __name__ == "__main__":
    main()
