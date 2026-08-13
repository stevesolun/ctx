"""Safely register the CTX ``UserPromptSubmit`` handler with Codex."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ctx.adapters.hook_config import (
    merge_hook_events,
    remove_python_module_handlers,
    update_json_object_locked,
)


def _module_cmd(module: str) -> str:
    parts = [sys.executable, "-m", module]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return " ".join(shlex.quote(part) for part in parts)


def default_hooks_path(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    configured = values.get("CODEX_HOME", "")
    root = Path(configured) if configured else Path.home() / ".codex"
    if not root.is_absolute():
        raise ValueError("CODEX_HOME must be an absolute path")
    return root / "hooks.json"


def make_query_hooks() -> dict[str, object]:
    """Return the exact current Codex hook group without trust state."""

    return {
        "UserPromptSubmit": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _module_cmd("ctx.adapters.codex.hook_handler"),
                        "timeout": 10,
                        "additionalContextLimit": 0,
                    }
                ]
            }
        ]
    }


def _validate_optional_nonnegative_int(value: object, *, field: str) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise ValueError(f"Codex hook {field} must be a non-negative integer")


def _validate_codex_hooks_file(existing: dict[str, object]) -> None:
    """Match Codex's current ``HooksFile``/command-handler data contract."""

    if set(existing) - {"description", "hooks"}:
        raise ValueError("Codex hooks.json contains unsupported root fields")
    description = existing.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("Codex hooks.json description must be a string")
    raw_events = existing.get("hooks", {})
    if not isinstance(raw_events, dict):
        raise ValueError("Codex hooks.json hooks must be an object")
    for event_name, raw_groups in raw_events.items():
        if not isinstance(raw_groups, list):
            raise ValueError(f"Codex {event_name} hook groups must be a list")
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                raise ValueError(f"Codex {event_name} hook group must be an object")
            matcher = raw_group.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                raise ValueError(f"Codex {event_name} matcher must be a string")
            raw_handlers = raw_group.get("hooks", [])
            if not isinstance(raw_handlers, list):
                raise ValueError(f"Codex {event_name} handlers must be a list")
            for raw_handler in raw_handlers:
                if not isinstance(raw_handler, dict):
                    raise ValueError(f"Codex {event_name} handler must be an object")
                handler_type = raw_handler.get("type")
                if handler_type not in {"command", "prompt", "agent"}:
                    raise ValueError(f"Codex {event_name} handler type is invalid")
                if handler_type != "command":
                    continue
                if not isinstance(raw_handler.get("command"), str):
                    raise ValueError(f"Codex {event_name} command is required")
                command_windows = raw_handler.get(
                    "commandWindows",
                    raw_handler.get("command_windows"),
                )
                if command_windows is not None and not isinstance(command_windows, str):
                    raise ValueError(f"Codex {event_name} commandWindows must be a string")
                _validate_optional_nonnegative_int(
                    raw_handler.get("timeout"),
                    field="timeout",
                )
                _validate_optional_nonnegative_int(
                    raw_handler.get("additionalContextLimit"),
                    field="additionalContextLimit",
                )
                if "async" in raw_handler and not isinstance(raw_handler["async"], bool):
                    raise ValueError(f"Codex {event_name} async must be a boolean")
                status = raw_handler.get("statusMessage")
                if status is not None and not isinstance(status, str):
                    raise ValueError(f"Codex {event_name} statusMessage must be a string")


def install_query_hook(path: Path) -> dict[str, object]:
    """Merge the CTX command under one complete locked update."""

    def update(existing: dict[str, object]) -> dict[str, object]:
        remove_python_module_handlers(
            existing,
            event_name="UserPromptSubmit",
            module="ctx.adapters.codex.hook_handler",
        )
        _validate_codex_hooks_file(existing)
        return merge_hook_events(existing, make_query_hooks())

    return update_json_object_locked(
        path,
        update,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Register the CTX Codex prompt hook")
    parser.add_argument(
        "--hooks-path",
        type=Path,
        default=None,
        help="Override the Codex hooks.json path",
    )
    args = parser.parse_args(argv)
    try:
        path = default_hooks_path() if args.hooks_path is None else args.hooks_path
        if not path.is_absolute():
            raise ValueError("Codex hooks path must be absolute")
        install_query_hook(path)
    except Exception as error:
        print(f"CTX Codex hook registration failed: {error}", file=sys.stderr)
        return 1
    print(f"CTX Codex hook registered in {path}")
    print("Review and approve the exact command in Codex /hooks before use.")
    print("Managed policy or disabled hooks may still suppress this registration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["default_hooks_path", "install_query_hook", "main", "make_query_hooks"]
