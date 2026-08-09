"""Strict, locked JSON updates shared by CTX host-hook installers."""

from __future__ import annotations

import json
import os
import shlex
import stat
from copy import deepcopy
from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import (
    ensure_secure_directory,
    reject_symlink_path,
    safe_atomic_write_text,
)


JsonObject: TypeAlias = dict[str, object]
_MAX_HOOK_CONFIG_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> JsonObject:
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"hook configuration contains duplicate key {key!r}")
        result[key] = value
    return result


def _validate_hook_shape(value: JsonObject) -> None:
    """Reject malformed host hook structures without rewriting user data."""

    raw_events = value.get("hooks")
    if raw_events is None:
        return
    if not isinstance(raw_events, dict):
        raise ValueError("hook configuration 'hooks' field must be an object")
    for event_name, raw_groups in raw_events.items():
        if not isinstance(event_name, str) or not isinstance(raw_groups, list):
            raise ValueError("hook configuration event entries must be lists")
        for raw_group in raw_groups:
            if not isinstance(raw_group, dict):
                raise ValueError(f"existing {event_name} matcher group is invalid")
            command = raw_group.get("command")
            if command is not None and not isinstance(command, str):
                raise ValueError(f"existing {event_name} command is invalid")
            if "hooks" not in raw_group:
                continue
            raw_handlers = raw_group["hooks"]
            if not isinstance(raw_handlers, list):
                raise ValueError(f"existing {event_name} handler list is invalid")
            for raw_handler in raw_handlers:
                if not isinstance(raw_handler, dict):
                    raise ValueError(f"existing {event_name} handler is invalid")
                handler_command = raw_handler.get("command")
                if handler_command is not None and not isinstance(handler_command, str):
                    raise ValueError(f"existing {event_name} handler command is invalid")


def load_json_object(path: Path) -> JsonObject:
    """Load one bounded JSON object without repairing malformed user data."""

    if not isinstance(path, Path):
        raise TypeError("hook configuration path must be a Path")
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("hook configuration must be a regular non-symlink file")
    if before.st_size > _MAX_HOOK_CONFIG_BYTES:
        raise ValueError("hook configuration exceeds its size bound")
    reject_symlink_path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not os.path.samestat(before, opened)
            or not os.path.samestat(opened, current)
        ):
            raise ValueError("hook configuration changed while opening")
        chunks: list[bytes] = []
        remaining = _MAX_HOOK_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if len(raw) > _MAX_HOOK_CONFIG_BYTES:
            raise ValueError("hook configuration exceeds its size bound")
        if (
            not os.path.samestat(opened, after)
            or not os.path.samestat(after, current)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or len(raw) != after.st_size
        ):
            raise ValueError("hook configuration changed while reading")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("hook configuration is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("hook configuration root must be an object")
    _validate_hook_shape(value)
    return value


def write_json_object_atomic(path: Path, data: JsonObject) -> None:
    """Atomically replace one JSON object with owner-private permissions."""

    if not isinstance(path, Path) or not isinstance(data, dict):
        raise TypeError("atomic hook write requires a Path and JSON object")
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        metadata = None
    if metadata is not None and (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1):
        raise ValueError("hook configuration cannot be a symlink or hardlink")
    content = json.dumps(data, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
    if len(content.encode("utf-8")) > _MAX_HOOK_CONFIG_BYTES:
        raise ValueError("updated hook configuration exceeds its size bound")
    safe_atomic_write_text(path, content, encoding="utf-8")


def update_json_object_locked(
    path: Path,
    updater: Callable[[JsonObject], JsonObject],
) -> JsonObject:
    """Serialize the complete read-modify-write cycle across CTX processes."""

    if not isinstance(path, Path) or not callable(updater):
        raise TypeError("locked hook update requires a Path and updater")
    ensure_secure_directory(path.parent)
    with file_lock(path):
        existing = load_json_object(path)
        original = deepcopy(existing)
        updated = updater(existing)
        if not isinstance(updated, dict):
            raise TypeError("hook configuration updater must return an object")
        if updated != original:
            write_json_object_atomic(path, updated)
        return updated


def merge_hook_events(existing: JsonObject, new_hooks: JsonObject) -> JsonObject:
    """Merge command handlers by command identity while preserving user data."""

    _validate_hook_shape(existing)
    _validate_hook_shape({"hooks": new_hooks})
    hooks = existing.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hook configuration 'hooks' field must be an object")
    for event_name, raw_new_entries in new_hooks.items():
        if not isinstance(raw_new_entries, list):
            raise ValueError("new hook event entries must be a list")
        if event_name not in hooks:
            hooks[event_name] = raw_new_entries
            continue
        existing_list = hooks[event_name]
        if not isinstance(existing_list, list):
            raise ValueError(f"existing {event_name} hooks must be a list")
        existing_commands: set[str] = set()
        for entry in existing_list:
            if not isinstance(entry, dict):
                raise ValueError(f"existing {event_name} matcher group is invalid")
            command = entry.get("command")
            if isinstance(command, str):
                existing_commands.add(command)
            sub_hooks = entry.get("hooks", [])
            if not isinstance(sub_hooks, list):
                raise ValueError(f"existing {event_name} handler list is invalid")
            for hook in sub_hooks:
                if not isinstance(hook, dict):
                    raise ValueError(f"existing {event_name} handler is invalid")
                if isinstance(hook.get("command"), str):
                    existing_commands.add(hook["command"])
        for new_entry in raw_new_entries:
            if not isinstance(new_entry, dict):
                raise ValueError("new hook matcher group must be an object")
            new_command = new_entry.get("command")
            raw_handlers = new_entry.get("hooks", [])
            if raw_handlers:
                if not isinstance(raw_handlers, list):
                    raise ValueError("new hook handler list is invalid")
                missing = [
                    handler
                    for handler in raw_handlers
                    if isinstance(handler, dict)
                    and isinstance(handler.get("command"), str)
                    and handler["command"] not in existing_commands
                ]
                if not missing:
                    continue
                matcher = new_entry.get("matcher")
                target = next(
                    (
                        entry
                        for entry in existing_list
                        if isinstance(entry, dict)
                        and entry.get("matcher") == matcher
                        and isinstance(entry.get("hooks"), list)
                    ),
                    None,
                )
                if target is None:
                    target = dict(new_entry)
                    target["hooks"] = missing
                    existing_list.append(target)
                else:
                    target["hooks"].extend(missing)
                existing_commands.update(handler["command"] for handler in missing)
            elif isinstance(new_command, str) and new_command not in existing_commands:
                existing_list.append(new_entry)
                existing_commands.add(new_command)
    return existing


def invokes_python_module(command: object, module: str) -> bool:
    """Recognize a generated ``python -m module`` command across host OSes."""

    if not isinstance(command, str) or not isinstance(module, str) or not module:
        return False
    for posix in (True, False):
        try:
            parts = shlex.split(command, posix=posix)
        except ValueError:
            continue
        normalized = [part.strip('"') for part in parts]
        if len(normalized) == 3 and normalized[1:] == ["-m", module]:
            return True
    return False


def remove_python_module_handlers(
    existing: JsonObject,
    *,
    event_name: str,
    module: str,
) -> JsonObject:
    """Remove prior generated handlers so interpreter/config changes replace them."""

    _validate_hook_shape(existing)
    raw_events = existing.get("hooks")
    if raw_events is None:
        return existing
    assert isinstance(raw_events, dict)
    raw_groups = raw_events.get(event_name)
    if raw_groups is None:
        return existing
    assert isinstance(raw_groups, list)
    retained_groups: list[object] = []
    for raw_group in raw_groups:
        assert isinstance(raw_group, dict)
        if invokes_python_module(raw_group.get("command"), module):
            continue
        raw_handlers = raw_group.get("hooks")
        if raw_handlers is None:
            retained_groups.append(raw_group)
            continue
        assert isinstance(raw_handlers, list)
        retained_handlers = [
            handler
            for handler in raw_handlers
            if isinstance(handler, dict)
            and not invokes_python_module(handler.get("command"), module)
        ]
        if retained_handlers:
            raw_group["hooks"] = retained_handlers
            retained_groups.append(raw_group)
    raw_events[event_name] = retained_groups
    return existing


__all__ = [
    "JsonObject",
    "load_json_object",
    "merge_hook_events",
    "remove_python_module_handlers",
    "update_json_object_locked",
    "write_json_object_atomic",
]
