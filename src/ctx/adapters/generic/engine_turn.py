"""One-use lower-authority presentation for an already committed engine bundle."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Callable, cast

from ctx.adapters.generic.loop import (
    DEFAULT_MAX_EPHEMERAL_CONTEXT_BYTES,
    TurnActivation,
    TurnAuthorization,
    TurnController,
    TurnPreparation,
)
from ctx.adapters.generic.providers import Message, ToolCall, ToolDefinition, Usage


def _validated_context(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("recommendation_context must be a string or None")
    if not value.strip():
        raise ValueError("recommendation_context must not be blank")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("recommendation_context must be valid UTF-8") from None
    if len(encoded) > DEFAULT_MAX_EPHEMERAL_CONTEXT_BYTES:
        raise ValueError("recommendation_context exceeds the ephemeral context byte bound")
    return value


class EngineTurnController:
    """Present one prevalidated recommendation without performing engine work."""

    __slots__ = (
        "_consumed",
        "_delegate",
        "_lock",
        "_pending",
        "_pending_open_count",
        "_recommendation_context",
    )

    def __init__(
        self,
        *,
        recommendation_context: str | None,
        delegate: TurnController | None,
    ) -> None:
        self._recommendation_context = _validated_context(recommendation_context)
        self._delegate = delegate
        self._consumed = False
        self._pending: tuple[int, int] | None = None
        self._pending_open_count = 0
        self._lock = threading.Lock()

    def prepare_turn(
        self,
        iteration: int,
        messages: tuple[Message, ...],
        base_tools: tuple[ToolDefinition, ...],
        *,
        deadline_monotonic: float | None,
        cancel_event: threading.Event | None,
    ) -> TurnPreparation:
        delegate = self._delegate
        if delegate is None:
            preparation = TurnPreparation(capability_epoch=iteration)
        else:
            preparation = delegate.prepare_turn(
                iteration,
                messages,
                base_tools,
                deadline_monotonic=deadline_monotonic,
                cancel_event=cancel_event,
            )
        if not isinstance(preparation, TurnPreparation):
            raise TypeError("delegate must return TurnPreparation")

        with self._lock:
            context = self._recommendation_context
            if context is None or self._consumed:
                return preparation
            key = (iteration, preparation.capability_epoch)
            if self._pending is not None:
                if self._pending == key:
                    self._pending_open_count += 1
                return preparation
            self._pending = key
            self._pending_open_count = 1
        return replace(
            preparation,
            ephemeral_user_context=(*preparation.ephemeral_user_context, context),
        )

    def activate_turn(
        self,
        iteration: int,
        capability_epoch: int,
    ) -> TurnActivation | Usage | None:
        hook = self._optional_delegate_hook("activate_turn")
        return None if hook is None else hook(iteration, capability_epoch)

    def authorize_tool_call(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
    ) -> TurnAuthorization | None:
        delegate = self._delegate
        if delegate is None:
            return None
        return delegate.authorize_tool_call(iteration, capability_epoch, call)

    def on_provider_request(self, iteration: int, capability_epoch: int) -> None:
        hook = self._optional_delegate_hook("on_provider_request")
        if hook is not None:
            hook(iteration, capability_epoch)
        with self._lock:
            if self._pending == (iteration, capability_epoch):
                self._pending = None
                self._pending_open_count = 0
                self._consumed = True

    def on_tool_result(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> Usage | None:
        delegate = self._delegate
        if delegate is None:
            return None
        return delegate.on_tool_result(
            iteration,
            capability_epoch,
            call,
            result,
            error,
        )

    def close_turn(
        self,
        iteration: int,
        capability_epoch: int,
        outcome: str,
    ) -> Usage | None:
        try:
            delegate = self._delegate
            if delegate is None:
                return None
            return delegate.close_turn(iteration, capability_epoch, outcome)
        finally:
            with self._lock:
                if self._pending == (iteration, capability_epoch):
                    self._pending_open_count -= 1
                    if self._pending_open_count == 0:
                        self._pending = None

    def _optional_delegate_hook(self, name: str) -> Callable[..., Any] | None:
        delegate = self._delegate
        if delegate is None:
            return None
        hook = getattr(delegate, name, None)
        if hook is None:
            return None
        if not callable(hook):
            raise TypeError(f"delegate {name} must be callable")
        return cast(Callable[..., Any], hook)


__all__ = ["EngineTurnController"]
