#!/usr/bin/env python3
"""
inject_hooks.py -- Inject CTX lifecycle hooks into ~/.claude/settings.json.

Merges new hook entries without overwriting existing ones.
Idempotent: safe to run multiple times.

Usage:
    python inject_hooks.py \
      --settings ~/.claude/settings.json \
      --ctx-dir /path/to/ctx
"""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

from ctx.adapters.hook_config import (
    load_json_object,
    merge_hook_events,
    remove_python_module_handlers,
    update_json_object_locked,
    write_json_object_atomic,
)


def load_settings(path: Path) -> dict:
    """Load existing settings.json without repairing malformed user data."""

    return load_json_object(path)


def make_hooks(ctx_dir: str) -> dict:
    """Return the hooks config block for this installation."""
    _ = ctx_dir  # Kept for CLI/API compatibility; commands now use modules.
    # Commands are quoted for the host OS so Python paths with spaces do not
    # break the hook shell/command runner.
    # Tool input is delivered by Claude Code on stdin as JSON; --from-stdin reads it
    # from there instead of interpolating $CLAUDE_TOOL_INPUT into argv (which would
    # allow shell injection via malicious tool-input blobs).
    monitor_cmd = _module_cmd("ctx.adapters.claude_code.hooks.context_monitor", "--from-stdin")
    tracker_cmd = _module_cmd("usage_tracker", "--sync")
    quality_cmd = _module_cmd(
        "ctx.adapters.claude_code.hooks.lifecycle_hooks",
        "quality-on-session-end",
    )
    # Skill-add detection: a Write/Edit to an installed SKILL.md refreshes
    # its catalog row.
    skill_add_cmd = _module_cmd("skill_add_detector", "--from-stdin")
    # Graph-based skill suggestion: surfaces pending-skills.json to Claude for user approval
    suggest_cmd = _module_cmd("ctx.adapters.claude_code.hooks.bundle_orchestrator")
    query_cmd = _module_cmd("ctx.adapters.claude_code.query_handler")
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
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": query_cmd,
                        "timeout": 10,
                    },
                ],
            },
        ],
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
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


_STALE_PATTERNS = ["context-monitor.py", "usage-tracker.py", "skill-transformer.py"]


def _validate_claude_handler(handler: dict[str, object], *, event_name: str) -> None:
    handler_type = handler.get("type")
    if handler_type not in {"command", "http", "mcp_tool", "prompt", "agent"}:
        raise ValueError(f"existing {event_name} handler type is invalid")
    required_fields = {
        "command": ("command",),
        "http": ("url",),
        "mcp_tool": ("server", "tool"),
        "prompt": ("prompt",),
        "agent": ("prompt",),
    }[str(handler_type)]
    for required_field in required_fields:
        required_value = handler.get(required_field)
        if not isinstance(required_value, str) or not required_value.strip():
            raise ValueError(f"existing {event_name} handler {required_field} is invalid")
    timeout = handler.get("timeout")
    if timeout is not None and (
        not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0
    ):
        raise ValueError(f"existing {event_name} handler timeout is invalid")
    if "async" in handler and not isinstance(handler["async"], bool):
        raise ValueError(f"existing {event_name} handler async flag is invalid")
    for boolean_field in ("asyncRewake", "once"):
        if boolean_field in handler and not isinstance(handler[boolean_field], bool):
            raise ValueError(f"existing {event_name} handler {boolean_field} is invalid")
    status = handler.get("statusMessage")
    if status is not None and not isinstance(status, str):
        raise ValueError(f"existing {event_name} handler status message is invalid")
    for string_field in ("if", "model"):
        value = handler.get(string_field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"existing {event_name} handler {string_field} is invalid")
    args = handler.get("args")
    if args is not None and (
        not isinstance(args, list) or not all(isinstance(value, str) for value in args)
    ):
        raise ValueError(f"existing {event_name} handler args are invalid")
    headers = handler.get("headers")
    if headers is not None and (
        not isinstance(headers, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
        )
    ):
        raise ValueError(f"existing {event_name} handler headers are invalid")
    allowed_env = handler.get("allowedEnvVars")
    if allowed_env is not None and (
        not isinstance(allowed_env, list)
        or not all(isinstance(value, str) for value in allowed_env)
    ):
        raise ValueError(f"existing {event_name} handler allowedEnvVars are invalid")
    mcp_input = handler.get("input")
    if mcp_input is not None and not isinstance(mcp_input, dict):
        raise ValueError(f"existing {event_name} handler input is invalid")


def _validate_claude_settings(settings: dict[str, object]) -> None:
    raw_events = settings.get("hooks", {})
    if not isinstance(raw_events, dict):
        raise ValueError("hook configuration 'hooks' field must be an object")
    for event_name, raw_groups in raw_events.items():
        if not isinstance(raw_groups, list):
            raise ValueError(f"existing {event_name} hooks must be a list")
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                raise ValueError(f"existing {event_name} matcher group is invalid")
            matcher = raw_group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                raise ValueError(f"existing {event_name} matcher is invalid")
            if "hooks" not in raw_group:
                _validate_claude_handler(raw_group, event_name=str(event_name))
                continue
            raw_handlers = raw_group["hooks"]
            if not isinstance(raw_handlers, list):
                raise ValueError(f"existing {event_name} handler list is invalid")
            for raw_handler in raw_handlers:
                if not isinstance(raw_handler, dict):
                    raise ValueError(f"existing {event_name} handler is invalid")
                _validate_claude_handler(raw_handler, event_name=str(event_name))


def _remove_stale_hooks(settings: dict) -> dict:
    """Remove hook entries that reference renamed/deleted scripts."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hook configuration 'hooks' field must be an object")
    for event_name, entries in list(hooks.items()):
        if not isinstance(entries, list):
            raise ValueError(f"existing {event_name} hooks must be a list")
        cleaned = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"existing {event_name} matcher group is invalid")
            cmd = entry.get("command", "")
            if not isinstance(cmd, str):
                raise ValueError(f"existing {event_name} command is invalid")
            sub_hooks = entry.get("hooks", [])
            if not isinstance(sub_hooks, list) or not all(
                isinstance(hook, dict) for hook in sub_hooks
            ):
                raise ValueError(f"existing {event_name} handler list is invalid")
            if any(pat in cmd for pat in _STALE_PATTERNS):
                continue
            if sub_hooks:
                for hook in sub_hooks:
                    command = hook.get("command", "")
                    if not isinstance(command, str):
                        raise ValueError(f"existing {event_name} handler command is invalid")
                sub_hooks = [
                    hook
                    for hook in sub_hooks
                    if not any(pattern in hook.get("command", "") for pattern in _STALE_PATTERNS)
                ]
                if not sub_hooks:
                    continue
                entry["hooks"] = sub_hooks
            cleaned.append(entry)
        hooks[event_name] = cleaned
    return settings


def merge_hooks(existing: dict, new_hooks: dict) -> dict:
    """Merge new hooks into existing settings without duplicating entries."""
    return merge_hook_events(existing, new_hooks)


def write_settings_atomic(path: Path, data: dict) -> None:
    """Write settings.json atomically: tempfile + fsync + os.replace().

    On POSIX, os.replace() is a single syscall and is guaranteed atomic even
    under concurrent writes.  On Windows, os.replace() raises PermissionError
    if the destination is held open by another process/thread.  We retry a
    small number of times with a short back-off; after that we re-raise so
    callers know something is genuinely wrong.
    """
    write_json_object_atomic(path, data)


def install_hooks_file(settings_path: Path, ctx_dir: str) -> dict:
    """Install all CTX Claude hooks under one locked read-modify-write."""

    def update(settings: dict[str, object]) -> dict[str, object]:
        remove_python_module_handlers(
            settings,
            event_name="UserPromptSubmit",
            module="ctx.adapters.claude_code.query_handler",
        )
        _validate_claude_settings(settings)
        cleaned = _remove_stale_hooks(settings)
        return merge_hooks(cleaned, make_hooks(ctx_dir))

    return update_json_object_locked(settings_path, update)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject hooks into settings.json")
    parser.add_argument("--settings", required=True, help="Path to settings.json")
    parser.add_argument("--ctx-dir", required=True, help="Path to the ctx/ directory")
    args = parser.parse_args()

    settings_path = Path(args.settings)
    ctx_dir = os.path.abspath(args.ctx_dir)

    install_hooks_file(settings_path, ctx_dir)

    print(f"Hooks injected into {settings_path}")
    print("  UserPromptSubmit: CTX capability engine")
    print("  PostToolUse: context_monitor + skill-add-detector + skill-suggest + backup_on_change")
    print("  Stop: usage_tracker + quality_on_session_end")


if __name__ == "__main__":
    main()
