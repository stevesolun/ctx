#!/usr/bin/env python3
"""
inject_hooks.py -- Inject PostToolUse and Stop hooks into ~/.claude/settings.json.

Merges new hook entries without overwriting existing ones.
Idempotent: safe to run multiple times.

Usage:
    python inject_hooks.py \
      --settings ~/.claude/settings.json \
      --ctx-dir /path/to/ctx
"""

import argparse
import json
import os
import sys
from pathlib import Path


def load_settings(path: Path) -> dict:
    """Load existing settings.json or return empty dict."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"Warning: {path} is invalid JSON, starting fresh", file=sys.stderr)
    return {}


def make_hooks(ctx_dir: str) -> dict:
    """Return the hooks config block for this installation."""
    # The hooks system runs in the user's shell; 2>/dev/null || true ensures
    # silent failure if the script is missing or python3 is unavailable
    monitor_cmd = (
        f'python3 "{ctx_dir}/context_monitor.py" '
        f'--tool "$CLAUDE_TOOL_NAME" --input "$CLAUDE_TOOL_INPUT" 2>/dev/null || true'
    )
    tracker_cmd = (
        f'python3 "{ctx_dir}/usage_tracker.py" --sync 2>/dev/null || true'
    )
    # Skill-add detection: when Write/Edit/Bash touches a SKILL.md path → register in wiki
    skill_add_cmd = (
        f'python3 "{ctx_dir}/skill_add_detector.py" '
        f'--tool "$CLAUDE_TOOL_NAME" --input "$CLAUDE_TOOL_INPUT" 2>/dev/null || true'
    )
    # Graph-based skill suggestion: surfaces pending-skills.json to Claude for user approval
    suggest_cmd = (
        f'python3 "{ctx_dir}/skill_suggest.py" 2>/dev/null || true'
    )

    return {
        "PostToolUse": [
            {
                "matcher": ".*",
                "hooks": [
                    {
                        "type": "command",
                        "command": monitor_cmd,
                    },
                    {
                        "type": "command",
                        "command": skill_add_cmd,
                    },
                    {
                        "type": "command",
                        "command": suggest_cmd,
                    },
                ],
            }
        ],
        "Stop": [
            {
                "type": "command",
                "command": tracker_cmd,
            }
        ],
    }


# Old filenames that were renamed — remove stale hook entries referencing them
_STALE_PATTERNS = ["context-monitor.py", "usage-tracker.py", "skill-transformer.py"]


def _remove_stale_hooks(settings: dict) -> dict:
    """Remove hook entries that reference renamed/deleted scripts."""
    hooks = settings.get("hooks", {})
    for event_name, entries in list(hooks.items()):
        cleaned = []
        for entry in entries:
            if isinstance(entry, dict):
                cmd = entry.get("command", "")
                sub_hooks = entry.get("hooks", [])
                if any(pat in cmd for pat in _STALE_PATTERNS):
                    continue
                if sub_hooks:
                    sub_hooks = [h for h in sub_hooks
                                 if not any(pat in h.get("command", "") for pat in _STALE_PATTERNS)]
                    if not sub_hooks:
                        continue
                    entry["hooks"] = sub_hooks
            cleaned.append(entry)
        hooks[event_name] = cleaned
    return settings


def merge_hooks(existing: dict, new_hooks: dict) -> dict:
    """Merge new hooks into existing settings without duplicating entries."""
    if "hooks" not in existing:
        existing["hooks"] = {}

    for event_name, new_entries in new_hooks.items():
        if event_name not in existing["hooks"]:
            existing["hooks"][event_name] = new_entries
            continue

        existing_list = existing["hooks"][event_name]

        # Deduplicate by command string (for both list-of-matchers and list-of-hooks formats)
        existing_commands: set[str] = set()
        for entry in existing_list:
            if isinstance(entry, dict):
                if "command" in entry:
                    existing_commands.add(entry["command"])
                for hook in entry.get("hooks", []):
                    if "command" in hook:
                        existing_commands.add(hook["command"])

        for new_entry in new_entries:
            if isinstance(new_entry, dict):
                new_cmd = new_entry.get("command", "")
                new_hooks_list = new_entry.get("hooks", [])

                # Check if any command in this entry already exists
                new_cmds = {new_cmd} if new_cmd else {h.get("command", "") for h in new_hooks_list}
                if not new_cmds.intersection(existing_commands):
                    existing_list.append(new_entry)

    return existing


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject hooks into settings.json")
    parser.add_argument("--settings", required=True, help="Path to settings.json")
    parser.add_argument("--ctx-dir", required=True, help="Path to the ctx/ directory")
    args = parser.parse_args()

    settings_path = Path(args.settings)
    ctx_dir = os.path.abspath(args.ctx_dir)

    settings = load_settings(settings_path)
    settings = _remove_stale_hooks(settings)
    new_hooks = make_hooks(ctx_dir)
    updated = merge_hooks(settings, new_hooks)

    # Write back with pretty formatting
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")

    print(f"Hooks injected into {settings_path}")
    print(f"  PostToolUse: context_monitor + skill-add-detector + skill-suggest")
    print(f"  Stop: usage_tracker")


if __name__ == "__main__":
    main()
