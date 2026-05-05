"""ctx.adapters.generic.providers.base — provider-agnostic types + Protocol.

A ``ModelProvider`` wraps "call a language model, get a response." It
is the seam between the harness while-loop (ctx.adapters.generic.loop)
and any backing SDK (LiteLLM by default, direct provider SDKs later).

Concrete shape: every provider takes a normalised message list + tool
definitions and returns a normalised ``CompletionResponse``. The loop
never touches provider-specific JSON — that lives behind this layer.

Design notes:
  * **Dataclasses are frozen.** Once a response lands in the session
    log, it is immutable. Mutating a message in flight is a class of
    bug we would rather not catch by code review.
  * **Usage accounting lives in the response.** Each call reports
    input/output tokens and (when the provider supports it) cost in
    USD. The loop sums these into the session for cost-budget stops.
  * **`raw` dict on CompletionResponse.** Keeps the underlying SDK
    response so debugging doesn't require re-running the call. Not
    part of the stable public contract — subject to change per
    provider.
  * **No streaming here.** Streaming is a separate Protocol we will
    add in a later phase if users ask for incremental tool-call
    execution; for the v1 solo loop, full-response semantics are
    simpler and sufficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


MessageRole = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal[
    "stop",            # model decided it was done
    "tool_calls",      # model requested tool execution
    "length",          # hit max_tokens
    "content_filter",  # provider policy filter
    "other",           # catch-all for provider-specific reasons
]


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation requested by the model.

    ``id`` is the provider-assigned identifier the model uses to
    correlate a later tool-result message with this call. ``name``
    and ``arguments`` are already parsed — ``arguments`` is the
    parsed JSON dict the provider emitted, not the raw string.
    ``parse_error`` is set when the provider emitted an invalid tool
    argument payload; the loop must refuse to execute those calls.
    """

    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str = ""


@dataclass(frozen=True)
class Message:
    """Normalised message on the conversation."""

    role: MessageRole
    content: str = ""
    # Set on role="assistant" when the model requested tool execution.
    tool_calls: tuple[ToolCall, ...] = ()
    # Set on role="tool" (a tool-result message replying to a prior tool_call).
    tool_call_id: str | None = None
    # Tool name for role="tool" messages (some providers want it echoed back).
    name: str | None = None


@dataclass(frozen=True)
class ToolDefinition:
    """A tool the model may invoke.

    ``parameters`` is a JSON schema describing the argument shape;
    providers translate it into their own native tool-definition
    format inside the provider adapter.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    """Token counts + optional cost for a single provider call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True)
class CompletionResponse:
    """Normalised return value from ``ModelProvider.complete``."""

    content: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: FinishReason
    usage: Usage
    provider: str
    model: str
    # Opaque underlying SDK response — debugging aid, not stable API.
    raw: dict[str, Any] = field(default_factory=dict)


class ModelProvider(Protocol):
    """Protocol every provider implementation must satisfy.

    ``name`` is the lowercase short identifier surfaced in logs and
    session files (e.g. "litellm", "anthropic-direct", "ollama-http").
    It does NOT name the LLM — that's ``model`` on the response.
    """

    name: str

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResponse: ...
