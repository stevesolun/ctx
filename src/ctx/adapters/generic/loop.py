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
  3. Cumulative cost exhausted ``budget_usd``              → ``"cost_budget"``
  4. Total tokens exhausted ``budget_tokens``              → ``"token_budget"``
  5. Caller cancellation (``cancel_event`` set)           → ``"cancelled"``
  6. Provider returned finish_reason == 'content_filter' → ``"content_filter"``
  7. Tool policy denied a model-requested call           -> ``"tool_denied"``
  8. A tool call raised a non-recoverable error          -> ``"tool_error"``

The loop NEVER catches provider-level exceptions (import failures,
HTTP errors, auth errors) — those bubble to the caller so a bad
config fails loudly at call time instead of being silently swallowed
as a dead loop iteration.

Raw ``ctx__wiki_get`` tool calls and results are request-scoped: a
completed pair is available to one subsequent provider request and is
then removed. The loop does not guess whether ordinary model-authored
assistant text quotes or summarizes that result; such text remains
normal conversation history.

Plan 001 Phase H3.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Protocol

from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ModelProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)
from ctx.adapters.generic.tools import McpRouter, McpServerError, TOOL_SEPARATOR
from ctx.utils._secret_scan import redact_secret_text


_logger = logging.getLogger(__name__)


StopReason = Literal[
    "completed",
    "length",  # provider truncated (finish_reason=length)
    "empty_response",  # no content + no tool calls
    "provider_other",  # finish_reason='other' with no usable output
    "max_iterations",
    "cost_budget",
    "token_budget",
    "cancelled",
    "content_filter",
    "tool_denied",
    "tool_error",
    "controller_error",
    "observer_error",
    "provider_error",
    "provider_timeout",
]

ToolPolicy = Callable[[ToolCall], str | None]
DEFAULT_MAX_EPHEMERAL_CONTEXT_BYTES = 16_384
DEFAULT_MAX_TURN_TOOLS = 32
DEFAULT_MAX_TURN_SCHEMA_BYTES = 65_536
DEFAULT_TURN_PREPARE_TIMEOUT = 1.0
_EPHEMERAL_USER_CONTEXT_BOUNDARY = "\n\n--- current user request ---\n"
EPHEMERAL_WIKI_TOOL_NAME = "ctx__wiki_get"


@dataclass(frozen=True)
class TurnPreparation:
    """Request-only context and tools for one provider turn.

    ``ephemeral_context`` is trusted system-level context inserted after the
    canonical system message. ``ephemeral_user_context`` is lower-authority
    reference material inserted immediately before the current user request.
    Neither input is appended directly to session history; provider responses
    are persisted normally and may independently repeat reference text.
    ``tools=None`` keeps the loop's base catalogue; an empty tuple exposes no
    tools. ``capability_epoch`` identifies the immutable snapshot that
    authorizes calls returned by that provider response.
    """

    ephemeral_context: tuple[str, ...] = ()
    tools: tuple[ToolDefinition, ...] | None = None
    capability_epoch: int = 0
    usage: Usage = field(default_factory=Usage)
    ephemeral_user_context: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnActivation:
    """Resources discovered after a turn passes its cheap safety gates."""

    tools: tuple[ToolDefinition, ...] | None = None
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class TurnAuthorization:
    """Host authorization decision plus any activation-model usage."""

    denial: str | None = None
    usage: Usage = field(default_factory=Usage)


class TurnController(Protocol):
    """Host-owned control plane for dynamic context and capabilities.

    Preparation must be side-effect free and cooperatively observe its
    monotonic deadline and cancellation event. Resource activation belongs in
    the optional ``activate_turn`` hook, authorization, or tool execution;
    unload belongs in ``close_turn``. Hooks that perform model work must return
    its usage for budgets and telemetry.
    """

    def prepare_turn(
        self,
        iteration: int,
        messages: tuple[Message, ...],
        base_tools: tuple[ToolDefinition, ...],
        *,
        deadline_monotonic: float | None,
        cancel_event: threading.Event | None,
    ) -> TurnPreparation: ...

    def authorize_tool_call(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
    ) -> TurnAuthorization | None: ...

    def on_tool_result(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> Usage | None: ...

    def close_turn(
        self,
        iteration: int,
        capability_epoch: int,
        outcome: str,
    ) -> Usage | None: ...


# ── Event hooks (for H4 session state + H5 context compaction) ───────────


class LoopObserver(Protocol):
    """Receives every event the loop emits as it runs.

    Default implementation is a no-op. H4 ships a JSONL-writing
    observer; H5 ships an in-place message-list compactor. Observers
    are stateless as far as the loop is concerned — any state they
    need lives on the observer itself.
    """

    def on_iteration_start(self, iteration: int, messages: list[Message]) -> None: ...

    def on_model_response(self, iteration: int, response: CompletionResponse) -> None: ...

    def on_tool_call(
        self,
        iteration: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> None: ...

    def on_stop(self, result: "LoopResult") -> None: ...


class _NullObserver:
    """Default observer — silently ignores everything."""

    def on_iteration_start(self, iteration: int, messages: list[Message]) -> None:
        pass

    def on_model_response(self, iteration: int, response: CompletionResponse) -> None:
        pass

    def on_tool_call(
        self,
        iteration: int,
        call: ToolCall,
        result: str,
        error: str | None,
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
    when termination was external). ``usage`` is the sum across all
    provider, preparation, controller-hook, and compaction calls.
    """

    stop_reason: StopReason
    final_message: str
    iterations: int
    usage: Usage
    messages: tuple[Message, ...]
    detail: str = ""


@dataclass(frozen=True)
class ProviderFailure:
    """Correlate a provider exception with its persisted terminal result."""

    exception: Exception
    result: LoopResult


@dataclass(frozen=True)
class _TurnStep:
    stop_reason: StopReason | None = None
    detail: str = ""
    final_message: str = ""


@dataclass
class _RunningTotals:
    """Mutable counter state threaded through the loop body."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached_input_tokens: int = 0
    usage_sources: int = 0
    tokens_reported: bool = True
    cost_reported: bool = True
    cached_input_reported: bool = True

    def add(self, usage: Usage, *, provider_call: bool = False) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if usage.cost_usd is not None:
            self.cost_usd += usage.cost_usd
        if usage.cached_input_tokens is not None:
            self.cached_input_tokens += usage.cached_input_tokens
        observed_usage = provider_call or any(
            (
                usage.input_tokens,
                usage.output_tokens,
                usage.cost_usd is not None,
                usage.cached_input_tokens is not None,
                not usage.tokens_reported,
            )
        )
        if observed_usage:
            self.usage_sources += 1
            self.tokens_reported = self.tokens_reported and usage.tokens_reported
            self.cost_reported = self.cost_reported and usage.cost_usd is not None
            self.cached_input_reported = (
                self.cached_input_reported and usage.cached_input_tokens is not None
            )

    def as_usage(self) -> Usage:
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=(self.cost_usd if self.usage_sources > 0 and self.cost_reported else None),
            cached_input_tokens=(
                self.cached_input_tokens
                if self.usage_sources > 0 and self.cached_input_reported
                else None
            ),
            tokens_reported=self.tokens_reported,
        )


def _budget_stop_reason(
    totals: _RunningTotals,
    *,
    budget_usd: float | None,
    budget_tokens: int | None,
    before_call: bool = False,
) -> tuple[StopReason | None, str]:
    if budget_usd is not None and totals.usage_sources > 0 and not totals.cost_reported:
        return (
            "cost_budget",
            "provider cost usage unavailable; cannot enforce USD budget",
        )
    cost_limit_hit = budget_usd is not None and (
        totals.cost_usd >= budget_usd if before_call else totals.cost_usd > budget_usd
    )
    if cost_limit_hit:
        return (
            "cost_budget",
            f"cumulative cost ${totals.cost_usd:.4f} "
            f"{'exhausted' if before_call else 'exceeded'} budget ${budget_usd:.4f}",
        )
    if budget_tokens is not None:
        if totals.usage_sources > 0 and not totals.tokens_reported:
            return (
                "token_budget",
                "provider token usage unavailable; cannot enforce token budget",
            )
        total_tokens = totals.input_tokens + totals.output_tokens
        token_limit_hit = (
            total_tokens >= budget_tokens if before_call else total_tokens > budget_tokens
        )
        if token_limit_hit:
            return (
                "token_budget",
                f"cumulative tokens {total_tokens} "
                f"{'exhausted' if before_call else 'exceeded'} budget {budget_tokens}",
            )
    return None, ""


# ── Main loop ──────────────────────────────────────────────────────────────


def run_loop(
    *,
    provider: ModelProvider,
    system_prompt: str,
    task: str,
    router: McpRouter | None = None,
    extra_tools: list[ToolDefinition] | None = None,
    tool_executor: Callable[[ToolCall], str] | None = None,
    tool_policy: ToolPolicy | None = None,
    turn_controller: TurnController | None = None,
    turn_prepare_timeout: float | None = DEFAULT_TURN_PREPARE_TIMEOUT,
    max_ephemeral_context_bytes: int = DEFAULT_MAX_EPHEMERAL_CONTEXT_BYTES,
    max_turn_tools: int = DEFAULT_MAX_TURN_TOOLS,
    max_turn_schema_bytes: int = DEFAULT_MAX_TURN_SCHEMA_BYTES,
    model: str | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    provider_timeout: float | None = None,
    max_iterations: int = 25,
    budget_usd: float | None = None,
    budget_tokens: int | None = None,
    initial_usage: Usage | None = None,
    cancel_event: threading.Event | None = None,
    observer: LoopObserver | None = None,
    messages: list[Message] | None = None,
    compactor: Any | None = None,  # ctx.adapters.generic.compaction.ContextCompactor
    append_task_after_messages: bool = False,
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
        tool_policy      - optional pre-dispatch policy. Return ``None`` to
                           allow a call, or a denial reason string to block it.
        turn_controller  - optional host control plane that supplies bounded
                           request-only context and a per-turn capability
                           snapshot. Its input is not directly persisted.
        turn_prepare_timeout - cooperative deadline for side-effect-free preparation

    Safety limits:
        max_iterations   - hard cap on model calls (default 25)
        budget_usd       - stop before another call would exceed reported cost (optional)
        budget_tokens    - stop before another call would exceed reported tokens (optional)
        cancel_event     - caller sets to stop between iterations
        max_ephemeral_context_bytes - request-only context byte ceiling
        max_turn_tools   - dynamic capability count ceiling
        max_turn_schema_bytes - serialized dynamic schema byte ceiling

    State seeding:
        messages         - if provided, appended to AFTER the synthesized
                           system + task messages. Lets H7's --resume path
                           hand replayed history back in.

    Returns a ``LoopResult`` the caller inspects for stop_reason + usage.
    """
    if max_iterations <= 0:
        raise ValueError(f"max_iterations must be >= 1 (got {max_iterations})")
    if provider_timeout is not None and provider_timeout <= 0:
        raise ValueError("provider_timeout must be > 0 when set")
    if turn_prepare_timeout is not None:
        if (
            isinstance(turn_prepare_timeout, bool)
            or not isinstance(turn_prepare_timeout, (int, float))
            or not math.isfinite(turn_prepare_timeout)
            or turn_prepare_timeout <= 0
        ):
            raise ValueError("turn_prepare_timeout must be a positive finite number or None")
    for name, value in (
        ("max_ephemeral_context_bytes", max_ephemeral_context_bytes),
        ("max_turn_tools", max_turn_tools),
        ("max_turn_schema_bytes", max_turn_schema_bytes),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")

    obs = observer or _NullObserver()
    totals = _RunningTotals()
    if initial_usage is not None:
        totals.add(initial_usage, provider_call=True)

    # Seed the conversation.
    # Two ordering modes:
    #   default                          : [system?] + task + messages
    #   append_task_after_messages=True  : [system?] + messages + task
    # The latter is what `ctx resume` wants: the replayed transcript
    # leads, the follow-up task is appended at the end. If the replayed
    # messages already begin with a system message, don't duplicate the
    # caller-supplied one. Codex review fix #3.
    conversation: list[Message] = []
    has_replayed_system = bool(messages and messages[0].role == "system" and messages[0].content)
    if append_task_after_messages:
        if system_prompt and not has_replayed_system:
            conversation.append(Message(role="system", content=system_prompt))
        if messages:
            conversation.extend(messages)
        conversation.append(Message(role="user", content=task))
    else:
        if system_prompt:
            conversation.append(Message(role="system", content=system_prompt))
        conversation.append(Message(role="user", content=task))
        if messages:
            conversation.extend(messages)

    # Build the base catalogue once. A host turn controller may publish a
    # smaller or larger immutable snapshot immediately before each provider
    # call without mutating the canonical conversation.
    base_tools = tuple(_collect_tools(router, extra_tools))

    iteration = 0
    final_message = ""
    stop_reason: StopReason = "max_iterations"
    stop_detail = ""

    while iteration < max_iterations:
        budget_stop, budget_detail = _budget_stop_reason(
            totals,
            budget_usd=budget_usd,
            budget_tokens=budget_tokens,
            before_call=True,
        )
        if budget_stop is not None:
            stop_reason = budget_stop
            stop_detail = budget_detail
            break
        iteration += 1

        if cancel_event is not None and cancel_event.is_set():
            stop_reason = "cancelled"
            stop_detail = "cancel_event was set"
            break

        _prune_malformed_ephemeral_wiki_context(conversation, observer=obs)
        obs.on_iteration_start(iteration, list(conversation))
        try:
            preparation = _prepare_turn(
                turn_controller,
                iteration=iteration,
                conversation=conversation,
                base_tools=base_tools,
                timeout=turn_prepare_timeout,
                cancel_event=cancel_event,
                totals=totals,
            )
        except InterruptedError as exc:
            stop_reason = "cancelled"
            stop_detail = str(exc)
            break
        except (TypeError, ValueError, RuntimeError) as exc:
            stop_reason = "controller_error"
            stop_detail = str(exc)
            break
        step = _run_prepared_turn(
            iteration=iteration,
            max_iterations=max_iterations,
            preparation=preparation,
            turn_controller=turn_controller,
            provider=provider,
            conversation=conversation,
            base_tools=base_tools,
            totals=totals,
            router=router,
            tool_executor=tool_executor,
            tool_policy=tool_policy,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            provider_timeout=provider_timeout,
            budget_usd=budget_usd,
            budget_tokens=budget_tokens,
            cancel_event=cancel_event,
            observer=obs,
            compactor=compactor,
            max_context_bytes=max_ephemeral_context_bytes,
            max_tools=max_turn_tools,
            max_schema_bytes=max_turn_schema_bytes,
            prior_final_message=final_message,
        )
        if step.stop_reason is None:
            continue
        stop_reason = step.stop_reason
        stop_detail = step.detail
        final_message = step.final_message
        break

    _prune_all_ephemeral_wiki_context(conversation, observer=obs)
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


def _run_prepared_turn(
    *,
    iteration: int,
    max_iterations: int,
    preparation: TurnPreparation,
    turn_controller: TurnController | None,
    provider: ModelProvider,
    conversation: list[Message],
    base_tools: tuple[ToolDefinition, ...],
    totals: _RunningTotals,
    router: McpRouter | None,
    tool_executor: Callable[[ToolCall], str] | None,
    tool_policy: ToolPolicy | None,
    model: str | None,
    temperature: float,
    max_tokens: int | None,
    provider_timeout: float | None,
    budget_usd: float | None,
    budget_tokens: int | None,
    cancel_event: threading.Event | None,
    observer: LoopObserver,
    compactor: Any | None,
    max_context_bytes: int,
    max_tools: int,
    max_schema_bytes: int,
    prior_final_message: str,
) -> _TurnStep:
    """Execute one prepared turn and close its capability lease exactly once."""
    step: _TurnStep | None = None
    failure: BaseException | None = None
    provider_failure: Exception | None = None
    close_error: str | None = None

    try:
        try:
            while True:
                budget_stop, budget_detail = _budget_stop_reason(
                    totals,
                    budget_usd=budget_usd,
                    budget_tokens=budget_tokens,
                    before_call=True,
                )
                if budget_stop is not None:
                    step = _TurnStep(budget_stop, budget_detail)
                    break

                request_tools = base_tools if preparation.tools is None else preparation.tools
                if turn_controller is not None:
                    try:
                        request_tools = _validate_turn_payload(
                            (
                                *preparation.ephemeral_context,
                                *preparation.ephemeral_user_context,
                            ),
                            request_tools,
                            max_context_bytes=max_context_bytes,
                            max_tools=max_tools,
                            max_schema_bytes=max_schema_bytes,
                        )
                    except (TypeError, ValueError, OverflowError) as exc:
                        step = _TurnStep(
                            "controller_error",
                            f"turn controller payload rejected: {exc}",
                        )
                        break

                if cancel_event is not None and cancel_event.is_set():
                    step = _TurnStep("cancelled", "cancel_event was set after preparation")
                    break

                activation, activation_error = _activate_turn(
                    turn_controller,
                    iteration=iteration,
                    preparation=preparation,
                )
                totals.add(activation.usage)
                if activation_error is not None:
                    step = _TurnStep("controller_error", activation_error)
                    break
                if activation.tools is not None:
                    try:
                        request_tools = _validate_turn_payload(
                            (
                                *preparation.ephemeral_context,
                                *preparation.ephemeral_user_context,
                            ),
                            activation.tools,
                            max_context_bytes=max_context_bytes,
                            max_tools=max_tools,
                            max_schema_bytes=max_schema_bytes,
                        )
                    except (TypeError, ValueError, OverflowError) as exc:
                        step = _TurnStep(
                            "controller_error",
                            f"turn activation payload rejected: {exc}",
                        )
                        break
                budget_stop, budget_detail = _budget_stop_reason(
                    totals,
                    budget_usd=budget_usd,
                    budget_tokens=budget_tokens,
                    before_call=True,
                )
                if budget_stop is not None:
                    step = _TurnStep(budget_stop, budget_detail)
                    break
                if cancel_event is not None and cancel_event.is_set():
                    step = _TurnStep("cancelled", "cancel_event was set during activation")
                    break

                request_messages = _messages_for_turn(
                    conversation,
                    preparation.ephemeral_context,
                    preparation.ephemeral_user_context,
                )
                advertised_tool_names = frozenset(tool.name for tool in request_tools)
                enforce_advertised_tools = turn_controller is not None or bool(base_tools)

                provider_request_error = _notify_provider_request(
                    turn_controller,
                    iteration=iteration,
                    preparation=preparation,
                )
                if provider_request_error is not None:
                    step = _TurnStep("controller_error", provider_request_error)
                    break

                consumed_wiki_call_ids = _completed_ephemeral_wiki_call_ids(conversation)
                try:
                    response = _complete_provider(
                        provider,
                        messages=request_messages,
                        tools=list(request_tools) or None,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        provider_timeout=provider_timeout,
                    )
                except TimeoutError as exc:
                    totals.add(Usage(tokens_reported=False), provider_call=True)
                    step = _TurnStep("provider_timeout", str(exc))
                    break
                except Exception as exc:
                    totals.add(Usage(tokens_reported=False), provider_call=True)
                    if _is_provider_timeout_exception(exc):
                        step = _TurnStep("provider_timeout", f"provider timed out: {exc}")
                        break
                    provider_failure = exc
                    raise
                finally:
                    _prune_ephemeral_wiki_context(
                        conversation,
                        remove_ids=consumed_wiki_call_ids,
                        observer=observer,
                    )

                totals.add(response.usage, provider_call=True)
                tool_call_id_error = _tool_call_id_error(
                    response.tool_calls,
                    retained_ids=_retained_tool_call_ids(conversation),
                )
                if tool_call_id_error is not None:
                    step = _TurnStep("provider_error", tool_call_id_error)
                    break
                observer.on_model_response(iteration, response)
                conversation.append(
                    Message(
                        role="assistant",
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )

                if response.finish_reason == "content_filter":
                    step = _TurnStep(
                        "content_filter",
                        "provider reported content_filter finish",
                        response.content,
                    )
                    break
                if response.finish_reason == "length":
                    step = _TurnStep(
                        "length",
                        "provider truncated response (finish_reason=length)",
                        response.content or "",
                    )
                    break

                if response.tool_calls:
                    budget_stop, budget_detail = _budget_stop_reason(
                        totals,
                        budget_usd=budget_usd,
                        budget_tokens=budget_tokens,
                        before_call=True,
                    )
                    if budget_stop is not None:
                        step = _TurnStep(budget_stop, budget_detail)
                        break
                else:
                    final_message = response.content or ""
                    budget_stop, budget_detail = _budget_stop_reason(
                        totals,
                        budget_usd=budget_usd,
                        budget_tokens=budget_tokens,
                    )
                    if budget_stop is not None:
                        step = _TurnStep(budget_stop, budget_detail, final_message)
                        break
                    finish = (response.finish_reason or "").lower()
                    if not final_message.strip():
                        step = _TurnStep(
                            "empty_response",
                            "empty content with no tool calls "
                            f"(finish_reason={finish or 'unset'!r})",
                        )
                    elif finish in ("stop", "end_turn", ""):
                        step = _TurnStep("completed", final_message=final_message)
                    else:
                        step = _TurnStep(
                            "provider_other",
                            f"unexpected finish_reason={finish!r} with no tool calls",
                            final_message,
                        )
                    break

                for call in response.tool_calls:
                    denial: str | None
                    error: str | None
                    parse_error = getattr(call, "parse_error", "")
                    if parse_error:
                        denial = None
                        tool_result, error = "", f"invalid tool call arguments: {parse_error}"
                    else:
                        denial = _check_tool_policy(call, tool_policy)
                        if denial is None:
                            denial, authorization_usage = _check_turn_authorization(
                                call,
                                preparation=preparation,
                                advertised_tool_names=advertised_tool_names,
                                enforce_advertised_tools=enforce_advertised_tools,
                                turn_controller=turn_controller,
                                iteration=iteration,
                            )
                            if authorization_usage is not None:
                                totals.add(authorization_usage)
                            budget_stop, budget_detail = _budget_stop_reason(
                                totals,
                                budget_usd=budget_usd,
                                budget_tokens=budget_tokens,
                                before_call=True,
                            )
                            if budget_stop is not None:
                                step = _TurnStep(budget_stop, budget_detail)
                                break
                        if denial is None:
                            tool_result, error = _execute_tool(
                                call,
                                router=router,
                                tool_executor=tool_executor,
                            )
                        else:
                            tool_result, error = "", f"policy: {denial}"

                    controller_usage, controller_error = _notify_turn_controller(
                        turn_controller,
                        iteration=iteration,
                        preparation=preparation,
                        call=call,
                        result=tool_result,
                        error=error,
                    )
                    if controller_usage is not None:
                        totals.add(controller_usage)
                    conversation.append(
                        Message(
                            role="tool",
                            content=tool_result if error is None else f"ERROR: {error}",
                            tool_call_id=call.id,
                            name=call.name,
                        )
                    )
                    observer_error: str | None = None
                    try:
                        observer.on_tool_call(iteration, call, tool_result, error)
                    except Exception as exc:  # noqa: BLE001
                        observer_error = f"observer raised {type(exc).__name__}: {exc}"

                    if error is not None:
                        reason: StopReason = "tool_error" if denial is None else "tool_denied"
                        action = "failed" if denial is None else "denied"
                        detail = (
                            f"tool {call.name!r} {action}: {error if denial is None else denial}"
                        )
                        if controller_error is not None:
                            detail += f"; additionally {controller_error}"
                        if observer_error is not None:
                            detail += f"; additionally {observer_error}"
                        step = _TurnStep(reason, detail)
                        break
                    if controller_error is not None:
                        detail = f"tool {call.name!r} executed successfully; {controller_error}"
                        if observer_error is not None:
                            detail += f"; additionally {observer_error}"
                        step = _TurnStep(
                            "controller_error",
                            detail,
                        )
                        break
                    if observer_error is not None:
                        step = _TurnStep(
                            "observer_error",
                            f"tool {call.name!r} executed successfully; {observer_error}",
                        )
                        break
                    budget_stop, budget_detail = _budget_stop_reason(
                        totals,
                        budget_usd=budget_usd,
                        budget_tokens=budget_tokens,
                        before_call=True,
                    )
                    if budget_stop is not None:
                        step = _TurnStep(budget_stop, budget_detail)
                        break
                if step is not None:
                    break

                if _should_compact_conversation(conversation, compactor):
                    assert compactor is not None
                    budget_stop, budget_detail = _budget_stop_reason(
                        totals,
                        budget_usd=budget_usd,
                        budget_tokens=budget_tokens,
                        before_call=True,
                    )
                    if budget_stop is not None:
                        step = _TurnStep(budget_stop, budget_detail)
                        break
                    try:
                        if hasattr(compactor, "compact_with_usage"):
                            cresult = compactor.compact_with_usage(conversation, provider)
                            new_conversation = cresult.new_messages
                            totals.add(cresult.usage, provider_call=True)
                        else:
                            new_conversation = compactor.compact(conversation, provider)
                            totals.add(Usage(tokens_reported=False), provider_call=True)
                    except Exception as exc:  # noqa: BLE001
                        totals.add(Usage(tokens_reported=False), provider_call=True)
                        _logger.warning(
                            "compactor raised (%s); continuing with uncompacted "
                            "conversation — next provider call may hit context limit",
                            exc,
                        )
                    else:
                        if new_conversation is not conversation:
                            conversation[:] = list(new_conversation)

                budget_stop, budget_detail = _budget_stop_reason(
                    totals,
                    budget_usd=budget_usd,
                    budget_tokens=budget_tokens,
                    before_call=True,
                )
                if budget_stop is not None:
                    step = _TurnStep(budget_stop, budget_detail)
                elif iteration >= max_iterations:
                    step = _TurnStep(
                        "max_iterations",
                        f"hit iteration cap {max_iterations}",
                    )
                else:
                    step = _TurnStep()
                break
        except BaseException as exc:  # cleanup must also run for cancellation signals
            failure = exc
    finally:
        if failure is not None:
            outcome = "provider_error" if provider_failure is not None else type(failure).__name__
        else:
            outcome = step.stop_reason if step and step.stop_reason is not None else "continue"
        close_usage, close_error = _close_turn(
            turn_controller,
            iteration=iteration,
            preparation=preparation,
            outcome=outcome,
        )
        if close_usage is not None:
            totals.add(close_usage)

    if failure is not None:
        _prune_all_ephemeral_wiki_context(conversation, observer=observer)
        if provider_failure is not None:
            detail = redact_secret_text(
                f"provider raised {type(provider_failure).__name__}: {provider_failure}"
            )
            if close_error is not None:
                detail = redact_secret_text(f"{detail}; {close_error}")
            result = LoopResult(
                stop_reason="provider_error",
                final_message=prior_final_message,
                iterations=iteration,
                usage=totals.as_usage(),
                messages=tuple(conversation),
                detail=detail,
            )
            observer.on_stop(result)
            provider_failure_hook = getattr(observer, "on_provider_failure", None)
            if callable(provider_failure_hook):
                provider_failure_hook(ProviderFailure(provider_failure, result))
        if close_error is not None:
            raise RuntimeError(f"{failure}; {close_error}") from failure
        raise failure

    if step is None:
        raise RuntimeError("prepared turn ended without a result")
    if close_error is not None:
        original_outcome = step.stop_reason or "continue"
        return _TurnStep(
            "controller_error",
            f"turn ended as {original_outcome}; {close_error}",
            step.final_message,
        )
    close_budget_stop, close_budget_detail = _budget_stop_reason(
        totals,
        budget_usd=budget_usd,
        budget_tokens=budget_tokens,
    )
    if close_budget_stop is not None and step.stop_reason in (
        None,
        "completed",
        "max_iterations",
        "cost_budget",
        "token_budget",
    ):
        return _TurnStep(close_budget_stop, close_budget_detail, step.final_message)
    return step


# ── Helpers ───────────────────────────────────────────────────────────────


def _prepare_turn(
    turn_controller: TurnController | None,
    *,
    iteration: int,
    conversation: list[Message],
    base_tools: tuple[ToolDefinition, ...],
    timeout: float | None,
    cancel_event: threading.Event | None,
    totals: _RunningTotals,
) -> TurnPreparation:
    if turn_controller is None:
        return TurnPreparation()
    try:
        preparation, deadline = _call_turn_preparer(
            turn_controller,
            iteration=iteration,
            conversation=conversation,
            base_tools=base_tools,
            timeout=timeout,
            cancel_event=cancel_event,
        )
    except InterruptedError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"turn controller preparation failed: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        if not isinstance(preparation, TurnPreparation):
            raise TypeError("turn controller must return TurnPreparation")
        _validate_usage(preparation.usage, source="turn preparation")
        totals.add(preparation.usage)
        if isinstance(preparation.capability_epoch, bool) or not isinstance(
            preparation.capability_epoch, int
        ):
            raise TypeError("capability_epoch must be an integer")
        if preparation.capability_epoch < 0:
            raise ValueError("capability_epoch must be >= 0")
        if not isinstance(preparation.ephemeral_context, tuple):
            raise TypeError("ephemeral_context must be a tuple of strings")
        if not isinstance(preparation.ephemeral_user_context, tuple):
            raise TypeError("ephemeral_user_context must be a tuple of strings")
        if preparation.tools is not None and not isinstance(preparation.tools, tuple):
            raise TypeError("turn tools must be a tuple or None")
    except (TypeError, ValueError) as exc:
        epoch = getattr(preparation, "capability_epoch", 0)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            epoch = 0
        close_usage, close_error = _close_turn(
            turn_controller,
            iteration=iteration,
            preparation=TurnPreparation(capability_epoch=epoch),
            outcome="preparation_rejected",
        )
        if close_usage is not None:
            totals.add(close_usage)
        if close_error is not None:
            raise RuntimeError(f"{exc}; {close_error}") from exc
        raise
    if deadline is not None and time.monotonic() > deadline:
        close_usage, close_error = _close_turn(
            turn_controller,
            iteration=iteration,
            preparation=preparation,
            outcome="preparation_timeout",
        )
        if close_usage is not None:
            totals.add(close_usage)
        error = RuntimeError(f"turn controller preparation exceeded {timeout:.3f}s deadline")
        if close_error is not None:
            raise RuntimeError(f"{error}; {close_error}") from error
        raise error
    return TurnPreparation(
        ephemeral_context=preparation.ephemeral_context,
        ephemeral_user_context=preparation.ephemeral_user_context,
        tools=preparation.tools,
        capability_epoch=preparation.capability_epoch,
        usage=preparation.usage,
    )


def _call_turn_preparer(
    turn_controller: TurnController,
    *,
    iteration: int,
    conversation: list[Message],
    base_tools: tuple[ToolDefinition, ...],
    timeout: float | None,
    cancel_event: threading.Event | None,
) -> tuple[TurnPreparation, float | None]:
    deadline = None if timeout is None else time.monotonic() + timeout
    preparation = turn_controller.prepare_turn(
        iteration,
        tuple(conversation),
        base_tools,
        deadline_monotonic=deadline,
        cancel_event=cancel_event,
    )
    return preparation, deadline


def _messages_for_turn(
    conversation: list[Message],
    ephemeral_context: tuple[str, ...],
    ephemeral_user_context: tuple[str, ...],
) -> list[Message]:
    messages = list(conversation)
    if ephemeral_context:
        insert_at = 1 if messages and messages[0].role == "system" else 0
        messages.insert(
            insert_at,
            Message(role="system", content="\n\n".join(ephemeral_context)),
        )
    if ephemeral_user_context:
        user_index = next(
            (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"),
            len(messages),
        )
        reference = "\n\n".join(ephemeral_user_context)
        if user_index == len(messages):
            messages.append(Message(role="user", content=reference))
        else:
            current = messages[user_index]
            messages[user_index] = replace(
                current,
                content=(reference + _EPHEMERAL_USER_CONTEXT_BOUNDARY + current.content),
            )
    return messages


def _completed_ephemeral_wiki_call_ids(messages: list[Message]) -> frozenset[str]:
    call_counts: dict[str, int] = {}
    result_counts: dict[str, int] = {}
    wiki_call_ids: set[str] = set()
    wiki_result_ids: set[str] = set()

    for message in messages:
        if message.role == "assistant":
            for call in message.tool_calls:
                if not isinstance(call.id, str) or not call.id.strip():
                    continue
                call_counts[call.id] = call_counts.get(call.id, 0) + 1
                if call.name == EPHEMERAL_WIKI_TOOL_NAME:
                    wiki_call_ids.add(call.id)
        elif (
            message.role == "tool"
            and isinstance(message.tool_call_id, str)
            and message.tool_call_id.strip()
        ):
            call_id = message.tool_call_id
            result_counts[call_id] = result_counts.get(call_id, 0) + 1
            if message.name == EPHEMERAL_WIKI_TOOL_NAME:
                wiki_result_ids.add(call_id)

    return frozenset(
        call_id
        for call_id in wiki_call_ids.intersection(wiki_result_ids)
        if call_counts.get(call_id) == 1 and result_counts.get(call_id) == 1
    )


def _retained_tool_call_ids(messages: list[Message]) -> frozenset[str]:
    retained: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            if isinstance(call.id, str) and call.id.strip():
                retained.add(call.id)
    return frozenset(retained)


def _tool_call_id_error(
    tool_calls: tuple[ToolCall, ...],
    *,
    retained_ids: frozenset[str],
) -> str | None:
    seen: set[str] = set()
    for call in tool_calls:
        if not isinstance(call.id, str) or not call.id.strip():
            return f"provider returned blank tool-call id for {call.name!r}"
        if call.id in seen:
            return f"provider returned duplicate tool-call id {call.id!r}"
        if call.id in retained_ids:
            return f"provider reused retained tool-call id {call.id!r}"
        seen.add(call.id)
    return None


def _has_raw_ephemeral_wiki_result(messages: list[Message]) -> bool:
    wiki_call_ids = {
        call.id
        for message in messages
        for call in message.tool_calls
        if call.name == EPHEMERAL_WIKI_TOOL_NAME and isinstance(call.id, str)
    }
    return any(
        message.name == EPHEMERAL_WIKI_TOOL_NAME
        or (isinstance(message.tool_call_id, str) and message.tool_call_id in wiki_call_ids)
        for message in messages
    )


def _should_compact_conversation(
    messages: list[Message],
    compactor: Any | None,
) -> bool:
    if compactor is None or _has_raw_ephemeral_wiki_result(messages):
        return False
    return bool(compactor.should_compact(messages))


def _prune_malformed_ephemeral_wiki_context(
    conversation: list[Message],
    *,
    observer: LoopObserver | None = None,
) -> None:
    _prune_ephemeral_wiki_context(
        conversation,
        keep_ids=_completed_ephemeral_wiki_call_ids(conversation),
        observer=observer,
    )


def _prune_all_ephemeral_wiki_context(
    conversation: list[Message],
    *,
    observer: LoopObserver | None = None,
) -> None:
    _prune_ephemeral_wiki_context(
        conversation,
        observer=observer,
    )


def _prune_ephemeral_wiki_context(
    conversation: list[Message],
    *,
    remove_ids: frozenset[str] | None = None,
    keep_ids: frozenset[str] | None = None,
    observer: LoopObserver | None = None,
) -> None:
    if remove_ids is not None and keep_ids is not None:
        raise ValueError("remove_ids and keep_ids are mutually exclusive")
    if remove_ids is not None and not remove_ids:
        return

    def should_remove(call_id: object) -> bool:
        if remove_ids is not None:
            return isinstance(call_id, str) and call_id in remove_ids
        if keep_ids is not None:
            return not isinstance(call_id, str) or call_id not in keep_ids
        return True

    wiki_call_ids = {
        call.id
        for message in conversation
        for call in message.tool_calls
        if call.name == EPHEMERAL_WIKI_TOOL_NAME and isinstance(call.id, str)
    }
    retained: list[Message] = []
    pruned_ids: set[str] = set()
    for message in conversation:
        if message.tool_calls:
            kept_calls: list[ToolCall] = []
            for call in message.tool_calls:
                if call.name == EPHEMERAL_WIKI_TOOL_NAME and should_remove(call.id):
                    if isinstance(call.id, str):
                        pruned_ids.add(call.id)
                    continue
                kept_calls.append(call)
            tool_calls = tuple(kept_calls)
            if tool_calls != message.tool_calls:
                if tool_calls or message.content:
                    retained.append(replace(message, tool_calls=tool_calls))
                continue
        if (
            message.name == EPHEMERAL_WIKI_TOOL_NAME
            or (isinstance(message.tool_call_id, str) and message.tool_call_id in wiki_call_ids)
        ) and should_remove(message.tool_call_id):
            if isinstance(message.tool_call_id, str):
                pruned_ids.add(message.tool_call_id)
            continue
        retained.append(message)

    removed_message_count = len(conversation) - len(retained)
    conversation[:] = retained
    hook = getattr(observer, "on_ephemeral_context_pruned", None)
    if removed_message_count and callable(hook):
        try:
            hook(frozenset(pruned_ids), removed_message_count)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("ephemeral-context observer hook raised: %s", exc)


def _validate_tool_catalogue(
    tools: list[ToolDefinition] | tuple[ToolDefinition, ...],
) -> None:
    seen: set[str] = set()
    for tool in tools:
        if not isinstance(tool, ToolDefinition):
            raise TypeError("tool catalogue entries must be ToolDefinition instances")
        if not isinstance(tool.name, str) or not tool.name.strip():
            raise ValueError("tool names must be non-empty strings")
        if not isinstance(tool.description, str):
            raise TypeError("tool descriptions must be strings")
        if not isinstance(tool.parameters, dict):
            raise TypeError("tool parameters must be JSON-schema objects")
        if tool.name in seen:
            raise ValueError(f"duplicate tool name exposed to provider: {tool.name}")
        seen.add(tool.name)


def _validate_turn_payload(
    context: tuple[str, ...],
    tools: tuple[ToolDefinition, ...],
    *,
    max_context_bytes: int,
    max_tools: int,
    max_schema_bytes: int,
) -> tuple[ToolDefinition, ...]:
    for item in context:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("ephemeral context entries must be non-empty strings")
    context_bytes = len("\n\n".join(context).encode("utf-8"))
    if context_bytes > max_context_bytes:
        raise ValueError(
            f"ephemeral context is {context_bytes} bytes; limit is {max_context_bytes}"
        )
    if len(tools) > max_tools:
        raise ValueError(f"turn exposes {len(tools)} tools; limit is {max_tools}")
    _validate_tool_catalogue(tools)
    try:
        encoded = json.dumps(
            [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in tools
            ],
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"turn tool schemas are not JSON serializable: {exc}") from exc
    schema_bytes = len(encoded.encode("utf-8"))
    if schema_bytes > max_schema_bytes:
        raise ValueError(f"turn tool schemas are {schema_bytes} bytes; limit is {max_schema_bytes}")
    return tuple(
        ToolDefinition(
            name=item["name"],
            description=item["description"],
            parameters=item["parameters"],
        )
        for item in payload
    )


def _validate_usage(usage: Usage, *, source: str) -> None:
    if not isinstance(usage, Usage):
        raise TypeError(f"{source} usage must be Usage")
    for name, value in (
        ("input_tokens", usage.input_tokens),
        ("output_tokens", usage.output_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{source} {name} must be a non-negative integer")
    if usage.cached_input_tokens is not None:
        cached = usage.cached_input_tokens
        if isinstance(cached, bool) or not isinstance(cached, int) or cached < 0:
            raise ValueError(f"{source} cached_input_tokens must be a non-negative integer")
    if not isinstance(usage.tokens_reported, bool):
        raise ValueError(f"{source} tokens_reported must be a boolean")
    if usage.cost_usd is not None:
        cost = usage.cost_usd
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ValueError(f"{source} cost_usd must be a non-negative finite number")
        try:
            finite = math.isfinite(cost)
        except (TypeError, OverflowError) as exc:
            raise ValueError(f"{source} cost_usd must be a non-negative finite number") from exc
        if not finite or cost < 0:
            raise ValueError(f"{source} cost_usd must be a non-negative finite number")


def _check_turn_authorization(
    call: ToolCall,
    *,
    preparation: TurnPreparation,
    advertised_tool_names: frozenset[str],
    enforce_advertised_tools: bool,
    turn_controller: TurnController | None,
    iteration: int,
) -> tuple[str | None, Usage | None]:
    if enforce_advertised_tools and call.name not in advertised_tool_names:
        return (
            f"capability epoch {preparation.capability_epoch} did not advertise tool {call.name!r}",
            None,
        )
    if turn_controller is None:
        return None, None
    try:
        authorization = turn_controller.authorize_tool_call(
            iteration,
            preparation.capability_epoch,
            call,
        )
    except Exception as exc:  # noqa: BLE001
        return f"turn controller raised {type(exc).__name__}: {exc}", None
    if authorization is None:
        return None, None
    if not isinstance(authorization, TurnAuthorization):
        return "turn controller authorization must return TurnAuthorization or None", None
    try:
        _validate_usage(authorization.usage, source="turn controller authorization")
    except (TypeError, ValueError) as exc:
        return str(exc), None
    if authorization.denial is None:
        return None, authorization.usage
    if not isinstance(authorization.denial, str):
        return "turn controller authorization denial must be a string or None", authorization.usage
    return authorization.denial or "denied by turn controller", authorization.usage


def _notify_turn_controller(
    turn_controller: TurnController | None,
    *,
    iteration: int,
    preparation: TurnPreparation,
    call: ToolCall,
    result: str,
    error: str | None,
) -> tuple[Usage | None, str | None]:
    if turn_controller is None:
        return None, None
    try:
        usage = turn_controller.on_tool_result(
            iteration,
            preparation.capability_epoch,
            call,
            result,
            error,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"turn controller result hook raised {type(exc).__name__}: {exc}"
    if usage is not None:
        try:
            _validate_usage(usage, source="turn controller result hook")
        except (TypeError, ValueError) as exc:
            return None, str(exc)
    return usage, None


def _activate_turn(
    turn_controller: TurnController | None,
    *,
    iteration: int,
    preparation: TurnPreparation,
) -> tuple[TurnActivation, str | None]:
    if turn_controller is None:
        return TurnActivation(), None
    try:
        hook = getattr(turn_controller, "activate_turn", None)
    except Exception as exc:  # noqa: BLE001
        return TurnActivation(), (
            f"turn controller activation lookup raised {type(exc).__name__}: {exc}"
        )
    if hook is None:
        return TurnActivation(), None
    if not callable(hook):
        return TurnActivation(), "turn controller activate_turn must be callable"
    try:
        result = hook(iteration, preparation.capability_epoch)
    except Exception as exc:  # noqa: BLE001
        return TurnActivation(), f"turn controller activation raised {type(exc).__name__}: {exc}"
    if result is None:
        activation = TurnActivation()
    elif isinstance(result, Usage):
        activation = TurnActivation(usage=result)
    elif isinstance(result, TurnActivation):
        activation = result
    else:
        return TurnActivation(), "turn controller activate_turn returned an unsupported value"
    try:
        _validate_usage(activation.usage, source="turn controller activation")
    except (TypeError, ValueError) as exc:
        return TurnActivation(), str(exc)
    return activation, None


def _notify_provider_request(
    turn_controller: TurnController | None,
    *,
    iteration: int,
    preparation: TurnPreparation,
) -> str | None:
    if turn_controller is None:
        return None
    try:
        hook = getattr(turn_controller, "on_provider_request", None)
    except Exception as exc:  # noqa: BLE001
        return f"turn controller provider-request lookup raised {type(exc).__name__}: {exc}"
    if hook is None:
        return None
    if not callable(hook):
        return "turn controller on_provider_request must be callable"
    try:
        hook(iteration, preparation.capability_epoch)
    except Exception as exc:  # noqa: BLE001
        return f"turn controller provider-request hook raised {type(exc).__name__}: {exc}"
    return None


def _close_turn(
    turn_controller: TurnController | None,
    *,
    iteration: int,
    preparation: TurnPreparation,
    outcome: str,
) -> tuple[Usage | None, str | None]:
    if turn_controller is None:
        return None, None
    try:
        usage = turn_controller.close_turn(
            iteration,
            preparation.capability_epoch,
            outcome,
        )
    except BaseException as exc:  # cleanup failures must not replace the original exit
        return None, f"turn controller close hook raised {type(exc).__name__}: {exc}"
    if usage is not None:
        try:
            _validate_usage(usage, source="turn controller close hook")
        except (TypeError, ValueError) as exc:
            return None, str(exc)
    return usage, None


def _complete_provider(
    provider: ModelProvider,
    *,
    messages: list[Message],
    tools: list[ToolDefinition] | None,
    model: str | None,
    temperature: float,
    max_tokens: int | None,
    provider_timeout: float | None,
) -> CompletionResponse:
    if provider_timeout is None:
        return provider.complete(
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    results: queue.Queue[CompletionResponse | BaseException] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            results.put(
                provider.complete(
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            )
        except BaseException as exc:  # noqa: BLE001
            results.put(exc)

    thread = threading.Thread(
        target=_target,
        name=f"ctx-provider-call-{getattr(provider, 'name', 'provider')}",
        daemon=True,
    )
    thread.start()
    try:
        outcome = results.get(timeout=provider_timeout)
    except queue.Empty as exc:
        raise TimeoutError(f"provider call timed out after {provider_timeout:.3f}s") from exc
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


def _is_provider_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    cls = type(exc)
    class_name = cls.__name__.lower()
    module_name = cls.__module__.lower()
    if "timeout" in class_name:
        return True
    timeout_modules = ("httpx", "requests", "litellm", "openai", "anthropic")
    return "timeout" in str(exc).lower() and any(name in module_name for name in timeout_modules)


def _collect_tools(
    router: McpRouter | None,
    extra_tools: list[ToolDefinition] | None,
) -> list[ToolDefinition]:
    """Merge router-provided + caller-provided tools into one flat list."""
    router_tools = list(router.list_tools()) if router is not None else []
    caller_tools = list(extra_tools or [])
    if router is not None and caller_tools:
        reserved_prefixes = {
            tool.name.split(TOOL_SEPARATOR, 1)[0]
            for tool in caller_tools
            if TOOL_SEPARATOR in tool.name
        }
        configured_names = getattr(router, "configured_server_names", None)
        router_names = router.server_names if configured_names is None else configured_names
        conflicts = sorted(reserved_prefixes & set(router_names))
        if conflicts:
            raise ValueError(
                "MCP server name conflicts with caller tool namespace: " + ", ".join(conflicts)
            )

    merged = [*router_tools, *caller_tools]
    _validate_tool_catalogue(merged)
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


def _check_tool_policy(
    call: ToolCall,
    tool_policy: ToolPolicy | None,
) -> str | None:
    """Return a denial reason when policy blocks ``call``.

    Policy failures fail closed. A policy hook is part of the trust
    boundary; if it raises, the model should not get the tool call.
    """
    if tool_policy is None:
        return None
    try:
        denial = tool_policy(call)
    except Exception as exc:  # noqa: BLE001
        return f"policy raised {type(exc).__name__}: {exc}"
    if denial is None:
        return None
    return str(denial) or "denied"
