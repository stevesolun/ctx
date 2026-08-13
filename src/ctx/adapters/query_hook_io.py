"""Shared fail-soft I/O primitives for native ``UserPromptSubmit`` adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import BinaryIO, Protocol

from ctx.runtime.query_decision import QueryHostDescriptor
from ctx.runtime.query_delivery import QueryDeliveryController, SensitiveQueryInput


MAX_STDIN_BYTES = 512 * 1024
_MODE_ENV = "CTX_ENGINE_MODE"
_STATE_ROOT_ENV = "CTX_QUERY_DELIVERY_ROOT"
_MODES = frozenset({"activate", "legacy", "manage", "shadow", "recommend"})


class QueryController(Protocol):
    def issue(self, request: SensitiveQueryInput) -> object: ...


ControllerFactory = Callable[[Mapping[str, str]], QueryController]


def read_bounded_json_object(stdin: BinaryIO) -> dict[str, object] | None:
    raw = stdin.read(MAX_STDIN_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_STDIN_BYTES:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def bounded_text(
    value: object,
    *,
    maximum_bytes: int,
    allow_empty: bool = False,
) -> str | None:
    if type(value) is not str or (not allow_empty and not value):
        return None
    try:
        if len(value.encode("utf-8")) > maximum_bytes:
            return None
    except UnicodeEncodeError:
        return None
    return value


def existing_workspace(value: object) -> Path | None:
    raw = bounded_text(value, maximum_bytes=4_096)
    if raw is None or "\x00" in raw:
        return None
    path = Path(raw)
    try:
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            return None
    except OSError:
        return None
    return path


def open_query_delivery_controller(
    *,
    host: QueryHostDescriptor,
    environment: Mapping[str, str],
) -> QueryDeliveryController:
    configured_mode = environment.get(_MODE_ENV, "")
    mode = configured_mode.strip().lower() if isinstance(configured_mode, str) else ""
    if mode not in _MODES:
        mode = "legacy"
    configured_root = environment.get(_STATE_ROOT_ENV, "")
    if isinstance(configured_root, str) and configured_root:
        state_root = Path(configured_root)
        if not state_root.is_absolute():
            raise ValueError("CTX query delivery root must be absolute")
    else:
        state_root = Path.home() / ".ctx" / "query-delivery-v1"
    return QueryDeliveryController(
        host=host,
        mode=mode,
        state_root=state_root,
        environment=environment,
    )


def emit_report(report: object, stdout: BinaryIO) -> None:
    permit = getattr(report, "emission_permit", None)
    if permit is not None:
        permit.emit_once(stdout)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


__all__ = [
    "ControllerFactory",
    "MAX_STDIN_BYTES",
    "QueryController",
    "bounded_text",
    "emit_report",
    "existing_workspace",
    "open_query_delivery_controller",
    "read_bounded_json_object",
]
