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
import shlex
import sys
import tempfile
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
    _ = ctx_dir  # Kept for CLI/API compatibility; commands now use modules.
    # shlex.quote() ensures paths with spaces, $, or quotes don't break the shell command.
    # Tool input is delivered by Claude Code on stdin as JSON; --from-stdin reads it
    # from there instead of interpolating $CLAUDE_TOOL_INPUT into argv (which would
    # allow shell injection via malicious tool-input blobs).
    monitor_cmd = _module_cmd(
        "ctx.adapters.claude_code.hooks.context_monitor", "--from-stdin"
    )
    tracker_cmd = _module_cmd("usage_tracker", "--sync")
    quality_cmd = _module_cmd(
        "ctx.adapters.claude_code.hooks.lifecycle_hooks",
        "quality-on-session-end",
    )
    # Skill-add detection: when Write/Edit/Bash touches a SKILL.md path → register in wiki
    skill_add_cmd = _module_cmd("skill_add_detector", "--from-stdin")
    # Graph-based skill suggestion: surfaces pending-skills.json to Claude for user approval
    suggest_cmd = _module_cmd("ctx.adapters.claude_code.hooks.bundle_orchestrator")
    # Change-triggered backup: fires on every Edit/Write/MultiEdit, takes a
    # snapshot into ~/.claude/backups/ ONLY when tracked files actually
    # changed. SHA-gated so no-op edits don't create folders. Without this,
    # Claude-driven edits of ~/.claude/settings.json, agents/*, skills/*
    # have no rollback target.
    backup_cmd = _module_cmd(
        "ctx.adapters.claude_code.hooks.lifecycle_hooks",
        "backup-on-change",
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
            },
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [
                    {
                        "type": "command",
                        "command": backup_cmd,
                    },
                ],
            },
        ],
        # Stop hooks need the same {"hooks": [...]} wrapper as PostToolUse —
        # Claude Code's schema is consistent across events. The previous
        # flat form made the live-load verification agent discover that
        # quality_on_session_end.py never actually fires on session close
        # (only manually). This shape validates against the schema.
        "Stop": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": tracker_cmd,
                    },
                    {
                        "type": "command",
                        "command": quality_cmd,
                    },
                ],
            },
        ],
    }


# Old filenames that were renamed — remove stale hook entries referencing them
def _module_cmd(module: str, *args: str) -> str:
    """Return a hook command that targets an installed Python module."""
    parts = [sys.executable, "-m", module, *args]
    return " ".join(shlex.quote(part) for part in parts)


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


def write_settings_atomic(path: Path, data: dict) -> None:
    """Write settings.json atomically: tempfile + fsync + os.replace().

    On POSIX, os.replace() is a single syscall and is guaranteed atomic even
    under concurrent writes.  On Windows, os.replace() raises PermissionError
    if the destination is held open by another process/thread.  We retry a
    small number of times with a short back-off; after that we re-raise so
    callers know something is genuinely wrong.
    """
    import time

    content = json.dumps(data, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="settings.json.",
        dir=str(path.parent),
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        # Retry loop handles transient Windows PermissionError on os.replace().
        _last_exc: Exception | None = None
        for attempt in range(10):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as exc:
                _last_exc = exc
                time.sleep(0.01 * (attempt + 1))
        raise _last_exc  # type: ignore[misc]
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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

    write_settings_atomic(settings_path, updated)

    print(f"Hooks injected into {settings_path}")
    print("  PostToolUse: context_monitor + skill-add-detector + skill-suggest + backup_on_change")
    print("  Stop: usage_tracker + quality_on_session_end")


if __name__ == "__main__":
    main()
