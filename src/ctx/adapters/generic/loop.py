"""ctx.adapters.generic.loop — the solo-agent while-loop.

Wires the provider adapter (H1) + MCP router (H2) into a single
callable that drives a model against a task until it is done, a stop
condition fires, or the caller aborts.

Shape:

    provider  ←→  loop  ←→  mcp_router
                    │
                    ▼
               session events

The loop owns NO state beyond the live ``list[Message]`` — session
persistence (H4) and context compaction (H5) plug in as observer
hooks and mutation hooks respectively.

Stop conditions (deterministic, in priority order):
  1. Model returned no tool_calls and content != ''     → ``"completed"``
  2. Max iterations reached                              → ``"max_iterations"``
  3. Cumulative cost exceeded ``budget_usd``              → ``"cost_budget"``
  4. Total tokens exceeded ``budget_tokens``              → ``"token_budget"``
  5. Caller cancellation (``cancel_event`` set)           → ``"cancelled"``
  6. Provider returned finish_reason == 'content_filter' → ``"content_filter"``
  7. An MCP tool call raised a non-recoverable error     → ``"tool_error"``

The loop NEVER catches provider-level exceptions (import failures,
HTTP errors, auth errors) — those bubble to the caller so a bad
config fails loudly at call time instead of being silently swallowed
as a dead loop iteration.

Plan 001 Phase H3.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)
from ctx.adapters.generic.tools import McpRouter, McpServerError


_logger = logging.getLogger(__name__)


StopReason = Literal[
    "completed",
    "max_iterations",
    "cost_budget",
    "token_budget",
    "cancelled",
    "content_filter",
    "tool_error",
]


# ── Event hooks (for H4 session state + H5 context compaction) ───────────


class LoopObserver(Protocol):
    """Receives every event the loop emits as it runs.

    Default implementation is a no-op. H4 ships a JSONL-writing
    observer; H5 ships an in-place message-list compactor. Observers
    are stateless as far as the loop is concerned — any state they
    need lives on the observer itself.
    """

    def on_iteration_start(self, iteration: int, messages: list[Message]) -> None:
        ...

    def on_model_response(self, iteration: int, response: CompletionResponse) -> None:
        ...

    def on_tool_call(
        self, iteration: int, call: ToolCall, result: str, error: str | None,
    ) -> None:
        ...

    def on_stop(self, result: "LoopResult") -> None:
        ...


class _NullObserver:
    """Default observer — silently ignores everything."""

    def on_iteration_start(self, iteration: int, messages: list[Message]) -> None:
        pass

    def on_model_response(self, iteration: int, response: CompletionResponse) -> None:
        pass

    def on_tool_call(
        self, iteration: int, call: ToolCall, result: str, error: str | None,
    ) -> None:
        pass

    def on_stop(self, result: "LoopResult") -> None:
        pass


# ── Result ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LoopResult:
    """What ``run_loop`` returns when the loop terminates.

    ``stop_reason`` is the canonical tag the caller inspects to tell
    whether this was a normal completion or a guard-rail trip.
    ``final_message`` is the last model-produced message (empty string
    when termination was external). ``usage`` is the sum across every
    provider call.
    """

    stop_reason: StopReason
    final_message: str
    iterations: int
    usage: Usage
    messages: tuple[Message, ...]
    detail: str = ""


@dataclass
class _RunningTotals:
    """Mutable counter state threaded through the loop body."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost_usd is not None:
            self.cost_usd += usage.cost_usd

    def as_usage(self) -> Usage:
        # cost_usd=None when the provider never reported cost (ollama)
        # → caller can tell accumulated cost is unknown, not "0".
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd if self.cost_usd > 0 else None,
        )


# ── Main loop ──────────────────────────────────────────────────────────────


def run_loop(
    *,
    provider: ModelProvider,
    system_prompt: str,
    task: str,
    router: McpRouter | None = None,
    extra_tools: list[ToolDefinition] | None = None,
    tool_executor: Callable[[ToolCall], str] | None = None,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    max_iterations: int = 25,
    budget_usd: float | None = None,
    budget_tokens: int | None = None,
    cancel_event: threading.Event | None = None,
    observer: LoopObserver | None = None,
    messages: list[Message] | None = None,
    compactor: Any | None = None,  # ctx.adapters.generic.compaction.ContextCompactor
) -> LoopResult:
    """Drive a solo agent loop until it terminates.

    Required args:
        provider         - any ModelProvider (H1)
        system_prompt    - framing instructions (injected as role='system')
        task             - the user's first turn (role='user')

    Tool surface (pick at least one for non-trivial tasks):
        router           - McpRouter from H2; tools namespaced '<server>__<tool>'
        extra_tools      - declared locally; dispatched via tool_executor
        tool_executor    - fallback dispatcher for tools not owned by router.
                           Called as tool_executor(ToolCall) → result string.
                           Raise ``McpServerError`` or ``RuntimeError`` for
                           non-recoverable failures.

    Safety limits:
        max_iterations   - hard cap on model calls (default 25)
        budget_usd       - stop when cumulative reported cost exceeds (optional)
        budget_tokens    - stop when input+output tokens exceed (optional)
        cancel_event     - caller sets to stop between iterations

    State seeding:
        messages         - if provided, appended to AFTER the synthesized
                           system + task messages. Lets H7's --resume path
                           hand replayed history back in.

    Returns a ``LoopResult`` the caller inspects for stop_reason + usage.
    """
    if max_iterations <= 0:
        raise ValueError(f"max_iterations must be >= 1 (got {max_iterations})")

    obs = observer or _NullObserver()
    totals = _RunningTotals()

    # Seed the conversation.
    conversation: list[Message] = []
    if system_prompt:
        conversation.append(Message(role="system", content=system_prompt))
    conversation.append(Message(role="user", content=task))
    if messages:
        conversation.extend(messages)

    # Build the tool-catalogue once. Router tools use "__" namespacing;
    # extra_tools are passed through verbatim. A caller-supplied extra
    # tool with a "__" in its name is allowed but risks colliding with
    # the router's namespace convention — log a warning.
    tools = list(_collect_tools(router, extra_tools))

    iteration = 0
    final_message = ""
    stop_reason: StopReason = "max_iterations"
    stop_detail = ""

    while iteration < max_iterations:
        iteration += 1

        if cancel_event is not None and cancel_event.is_set():
            stop_reason = "cancelled"
            stop_detail = "cancel_event was set"
            break

        obs.on_iteration_start(iteration, list(conversation))

        response = provider.complete(
            messages=list(conversation),
            tools=tools or None,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        totals.add(response.usage)
        obs.on_model_response(iteration, response)

        # Append the model's turn to the conversation BEFORE we act on
        # any tool calls — so if a tool call raises, the assistant
        # message is already in the log.
        conversation.append(
            Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )

        # Terminal content-filter trip takes priority over tool calls.
        if response.finish_reason == "content_filter":
            final_message = response.content
            stop_reason = "content_filter"
            stop_detail = "provider reported content_filter finish"
            break

        # No tool calls → the model answered in-line.
        if not response.tool_calls:
            final_message = response.content
            stop_reason = "completed"
            break

        # Execute every tool call. Errors end the loop (tool_error)
        # rather than loop forever trying to recover; an Evaluator
        # agent (H11) is where retry strategy will live.
        tool_error_occurred = False
        for call in response.tool_calls:
            result, error = _execute_tool(
                call,
                router=router,
                tool_executor=tool_executor,
            )
            obs.on_tool_call(iteration, call, result, error)
            conversation.append(
                Message(
                    role="tool",
                    content=result if error is None else f"ERROR: {error}",
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
            if error is not None:
                stop_reason = "tool_error"
                stop_detail = f"tool {call.name!r} failed: {error}"
                tool_error_occurred = True
                break
        if tool_error_occurred:
            break

        # Context compaction runs BEFORE budget checks so the summary
        # call's cost lands inside this iteration's budget window.
        # The compactor owns the should-compact decision + the
        # summary call + the in-place message-list swap.
        if compactor is not None and compactor.should_compact(conversation):
            try:
                new_conversation = compactor.compact(conversation, provider)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "compactor raised (%s); continuing with uncompacted "
                    "conversation — next provider call may hit context limit",
                    exc,
                )
            else:
                if new_conversation is not conversation:
                    # Accept whatever the compactor returned (list or
                    # any sequence). Replace in place.
                    conversation[:] = list(new_conversation)

        # Budget checks run AFTER the tool responses land in the
        # conversation — the caller sees the model's last pre-budget
        # action in the session log.
        if budget_usd is not None and totals.cost_usd > budget_usd:
            stop_reason = "cost_budget"
            stop_detail = (
                f"cumulative cost ${totals.cost_usd:.4f} exceeded budget "
                f"${budget_usd:.4f}"
            )
            break
        if budget_tokens is not None:
            total_tokens = totals.input_tokens + totals.output_tokens
            if total_tokens > budget_tokens:
                stop_reason = "token_budget"
                stop_detail = (
                    f"cumulative tokens {total_tokens} exceeded budget "
                    f"{budget_tokens}"
                )
                break

    else:
        # Fell out of while via hitting max_iterations without break.
        stop_reason = "max_iterations"
        stop_detail = f"hit iteration cap {max_iterations}"

    result = LoopResult(
        stop_reason=stop_reason,
        final_message=final_message,
        iterations=iteration,
        usage=totals.as_usage(),
        messages=tuple(conversation),
        detail=stop_detail,
    )
    obs.on_stop(result)
    return result


# ── Helpers ───────────────────────────────────────────────────────────────


def _collect_tools(
    router: McpRouter | None,
    extra_tools: list[ToolDefinition] | None,
) -> list[ToolDefinition]:
    """Merge router-provided + caller-provided tools into one flat list."""
    merged: list[ToolDefinition] = []
    if router is not None:
        merged.extend(router.list_tools())
    if extra_tools:
        merged.extend(extra_tools)
    return merged


def _execute_tool(
    call: ToolCall,
    *,
    router: McpRouter | None,
    tool_executor: Callable[[ToolCall], str] | None,
) -> tuple[str, str | None]:
    """Dispatch one tool call, returning ``(result, error)``.

    Priority order:
      1. Router ownership (if the tool name contains the router's
         separator and names a known server).
      2. Caller-supplied ``tool_executor`` (used for ctx-core tools
         the router doesn't host — e.g. recommend_bundle in H6).
      3. Neither → synthesized error string.

    Errors are returned as (partial_result, error_message) so the
    model still sees a turn on the conversation; the loop decides
    whether the error ends the run.
    """
    from ctx.adapters.generic.tools import TOOL_SEPARATOR  # noqa: PLC0415

    # Router path
    if router is not None and TOOL_SEPARATOR in call.name:
        server_name = call.name.split(TOOL_SEPARATOR, 1)[0]
        if server_name in router.server_names:
            try:
                return router.call(call.name, call.arguments), None
            except McpServerError as exc:
                return "", f"MCP: {exc}"
            except (ValueError, RuntimeError) as exc:
                return "", f"MCP-dispatch: {exc}"

    # Caller executor path
    if tool_executor is not None:
        try:
            return tool_executor(call), None
        except McpServerError as exc:
            return "", f"executor: {exc}"
        except (ValueError, RuntimeError) as exc:
            return "", f"executor: {exc}"
        except Exception as exc:  # noqa: BLE001
            return "", f"executor: unexpected {type(exc).__name__}: {exc}"

    # Unhandled
    return "", f"no dispatcher for tool {call.name!r}"
