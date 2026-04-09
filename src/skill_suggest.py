#!/usr/bin/env python3
"""
skill_suggest.py -- PostToolUse hook: surface graph-based skill suggestions to the user.

Reads pending-skills.json (written by context-monitor.py when unmatched signals
are detected). If suggestions exist and haven't been shown this session, outputs
a hookSpecificOutput message that Claude presents to the user as a recommendation.

The user decides whether to load any suggested skill. Nothing is auto-loaded.

Called by Claude Code PostToolUse hook:
    python skill_suggest.py 2>/dev/null || true

Output format (JSON to stdout, consumed by Claude Code hook system):
    {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.path.expanduser("~/.claude"))
PENDING_SKILLS = CLAUDE_DIR / "pending-skills.json"
PENDING_UNLOAD = CLAUDE_DIR / "pending-unload.json"
SHOWN_FLAG = CLAUDE_DIR / ".skill-suggest-shown"


def already_shown_this_session() -> bool:
    """Check if we already showed suggestions in this session (avoid spam)."""
    if not SHOWN_FLAG.exists():
        return False
    try:
        shown_data = json.loads(SHOWN_FLAG.read_text(encoding="utf-8"))
        shown_at = shown_data.get("shown_at", "")
        pending_at = ""
        if PENDING_SKILLS.exists():
            pending_data = json.loads(PENDING_SKILLS.read_text(encoding="utf-8"))
            pending_at = pending_data.get("generated_at", "")
        # Already shown if the pending file hasn't been updated since last show
        return shown_at >= pending_at
    except Exception:
        return False


def mark_shown() -> None:
    """Mark that suggestions were shown so we don't repeat."""
    SHOWN_FLAG.write_text(
        json.dumps({"shown_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def main() -> None:
    if not PENDING_SKILLS.exists():
        sys.exit(0)

    if already_shown_this_session():
        sys.exit(0)

    try:
        pending = json.loads(PENDING_SKILLS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    graph_suggestions = pending.get("graph_suggestions", [])
    unmatched = pending.get("unmatched_signals", [])

    # Also check for unload suggestions
    unload_suggestions: list[dict] = []
    if PENDING_UNLOAD.exists():
        try:
            unload_data = json.loads(PENDING_UNLOAD.read_text(encoding="utf-8"))
            unload_suggestions = unload_data.get("suggestions", [])
        except (json.JSONDecodeError, OSError):
            pass

    if not graph_suggestions and not unmatched and not unload_suggestions:
        sys.exit(0)

    # Build the recommendation message
    lines: list[str] = []

    if unmatched or graph_suggestions:
        lines.append("ctx detected stack signals not covered by your loaded skills.")

    if unmatched:
        lines.append(f"Unmatched signals: {', '.join(unmatched)}")

    if graph_suggestions:
        lines.append("")
        lines.append("Suggested skills/agents from the knowledge graph:")
        for s in graph_suggestions[:5]:
            tags = ", ".join(s.get("matching_tags", []))
            lines.append(f"  - {s['name']} [{s['type']}] (tags: {tags})")
        lines.append("")
        lines.append(
            "To load any of these, tell the user: "
            "\"ctx detected you might benefit from these skills: [list]. "
            "Want me to load any of them?\""
        )

    if unload_suggestions:
        if lines:
            lines.append("")
        lines.append("ctx detected skills/agents that have been loaded but never used:")
        for s in unload_suggestions[:5]:
            lines.append(f"  - {s['name']} ({s['reason']})")
        lines.append("")
        lines.append(
            "Tell the user: \"These skills have been loaded but unused. "
            "Want me to unload any of them?\""
        )

    message = "\n".join(lines)

    # Output in Claude Code hook format
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
