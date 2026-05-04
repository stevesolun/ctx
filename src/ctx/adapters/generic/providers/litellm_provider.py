"""ctx.adapters.generic.providers.litellm_provider — LiteLLM-backed provider.

LiteLLM handles the N-provider translation problem so we do not have
to. Every major remote provider (OpenAI, Anthropic, Google, Mistral,
Cohere, Together, Fireworks, ...) plus every local inference server
that speaks an OpenAI-compatible HTTP API (Ollama, vLLM, LM Studio,
llama.cpp server) is reachable through a single ``litellm.completion()``
call — the wrapper below is therefore intentionally thin.

Tier-1 providers wired + tested in H1:

    * **openrouter/<model>** — remote aggregator that proxies to GPT,
      Claude, Gemini, MiniMax, Llama, DeepSeek, etc. Single API key
      via ``OPENROUTER_API_KEY``; model strings look like
      ``openrouter/anthropic/claude-opus-4.7`` or
      ``openrouter/minimax/minimax-m1``.
    * **ollama/<model>** — local inference. Model strings look like
      ``ollama/llama3.1:70b`` or ``ollama/qwen2.5-coder:32b``. No API
      key; the default base URL is ``http://localhost:11434``.

Direct provider SDKs (``anthropic/<model>``, ``openai/<model>``, etc.)
also work through LiteLLM when the matching env var is set, but we do
not consider them tier-1 because OpenRouter subsumes the most common
case (want ANY remote model, don't want to manage N keys).

Graceful-degradation principle: this module imports WITHOUT LiteLLM
installed. The dependency is deferred to ``complete()`` call time so
a user on an import path that never hits the harness does not need
the full LiteLLM dependency tree (litellm brings in a lot).
"""

from __future__ import annotations

import json
import os
from typing import Any

from ctx.adapters.generic.providers.base import (
    CompletionResponse,
    FinishReason,
    Message,
    ModelProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)


class LiteLLMProvider:
    """``ModelProvider`` impl backed by LiteLLM.

    Use ``get_provider(default_model=...)`` as the preferred
    constructor for CLI-driven flows — it reads env vars the caller
    usually cares about and applies the right defaults.
    """

    name: str = "litellm"

    def __init__(
        self,
        *,
        default_model: str,
        base_url: str | None = None,
        api_key_env: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not default_model:
            raise ValueError(
                "default_model is required; pass e.g. "
                "'openrouter/anthropic/claude-opus-4.7' or 'ollama/llama3.1'"
            )
        self._default_model = default_model
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._timeout = timeout

    # ── public API ─────────────────────────────────────────────────

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> CompletionResponse:
        """Call the model, return a normalised response.

        Raises ``RuntimeError`` with a clear diagnosis when LiteLLM
        is not importable — this keeps the provider usable as a
        library import even in environments without the harness
        dependency.
        """
        try:
            import litellm  # noqa: PLC0415 — deferred import
        except ImportError as exc:
            raise RuntimeError(
                "litellm is required for the generic harness provider. "
                "Install with: pip install 'claude-ctx[harness]' "
                "(or: pip install litellm)"
            ) from exc

        effective_model = model or self._default_model
        params: dict[str, Any] = {
            "model": effective_model,
            "messages": [_message_to_litellm(m) for m in messages],
            "temperature": temperature,
            "timeout": self._timeout,
        }
        if self._base_url is not None:
            params["api_base"] = self._base_url
        if self._api_key_env:
            key = os.environ.get(self._api_key_env)
            if key:
                params["api_key"] = key
        if tools:
            params["tools"] = [_tool_def_to_litellm(t) for t in tools]
        if max_tokens is not None:
            params["max_tokens"] = max_tokens

        raw = litellm.completion(**params)
        return _normalise_response(
            raw, provider=self.name, model=effective_model,
        )


# ── LiteLLM translation helpers ───────────────────────────────────


def _message_to_litellm(message: Message) -> dict[str, Any]:
    """Convert a ``Message`` into the OpenAI-shaped dict LiteLLM expects.

    LiteLLM normalises everything to the OpenAI chat-completion
    schema (``role``, ``content``, optional ``tool_calls``, etc.),
    so we emit that directly.
    """
    base: dict[str, Any] = {"role": message.role}
    if message.role == "assistant" and message.tool_calls:
        base["content"] = message.content or None
        base["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments),
                },
            }
            for tc in message.tool_calls
        ]
    elif message.role == "tool":
        base["content"] = message.content
        if message.tool_call_id:
            base["tool_call_id"] = message.tool_call_id
        if message.name:
            base["name"] = message.name
    else:
        base["content"] = message.content
    return base


def _tool_def_to_litellm(tool: ToolDefinition) -> dict[str, Any]:
    """Convert a ``ToolDefinition`` into LiteLLM's OpenAI-shaped tool spec."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _normalise_response(
    raw: Any, *, provider: str, model: str,
) -> CompletionResponse:
    """Convert a LiteLLM response object into ``CompletionResponse``.

    LiteLLM returns an OpenAI-shaped object (a Pydantic model), with
    the standard ``choices[0].message``, ``choices[0].finish_reason``,
    and ``usage`` fields. We normalise into our own types so the loop
    never has to care about the underlying shape.
    """
    # Dict-shaped responses are simpler to reason about + test; coerce
    # pydantic-shaped responses to dicts via their .model_dump() if
    # available. LiteLLM's response is Pydantic for real calls and
    # often just a dict in tests.
    if hasattr(raw, "model_dump"):
        raw_dict: dict[str, Any] = raw.model_dump()
    elif isinstance(raw, dict):
        raw_dict = raw
    else:
        raw_dict = {"_repr": repr(raw)}

    choices = raw_dict.get("choices") or []
    if not choices:
        return CompletionResponse(
            content="",
            tool_calls=(),
            finish_reason="other",
            usage=_extract_usage(raw_dict),
            provider=provider,
            model=model,
            raw=raw_dict,
        )
    first = choices[0]
    message = first.get("message") or {}
    content = message.get("content") or ""
    tool_calls_raw = message.get("tool_calls") or []
    tool_calls = tuple(_parse_tool_call(tc) for tc in tool_calls_raw)
    finish = _normalise_finish_reason(first.get("finish_reason"))
    return CompletionResponse(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=_extract_usage(raw_dict),
        provider=provider,
        model=model,
        raw=raw_dict,
    )


def _parse_tool_call(tc_raw: dict[str, Any]) -> ToolCall:
    """Parse one OpenAI-shaped tool call (from LiteLLM's normalised output).

    The ``arguments`` field is a JSON *string* per the OpenAI schema.
    Malformed JSON is preserved as ``parse_error`` so the loop can
    refuse execution. Treating a truncated argument payload as ``{}``
    can turn a partial provider response into a real tool invocation.
    """
    func = tc_raw.get("function") or {}
    args_raw = func.get("arguments") or "{}"
    parse_error = ""
    if isinstance(args_raw, str):
        try:
            arguments = json.loads(args_raw) if args_raw else {}
        except json.JSONDecodeError as exc:
            arguments = {}
            parse_error = f"invalid JSON arguments: {exc.msg}"
    elif isinstance(args_raw, dict):
        arguments = args_raw
    else:
        arguments = {}
        parse_error = f"arguments must be JSON object/string, got {type(args_raw).__name__}"
    return ToolCall(
        id=tc_raw.get("id") or "",
        name=func.get("name") or "",
        arguments=arguments,
        parse_error=parse_error,
    )


def _extract_usage(raw_dict: dict[str, Any]) -> Usage:
    """Pull the token counts out of a LiteLLM response dict.

    Different providers report usage under slightly different keys;
    LiteLLM normalises them into the OpenAI schema
    (``prompt_tokens`` + ``completion_tokens``). Cost is not always
    reported — pass through ``None`` when absent so the loop's
    budget tracker can decide how to handle unknown cost.
    """
    usage = raw_dict.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    # LiteLLM sometimes attaches ``response_cost`` at top level, not
    # under usage. Check both without requiring it.
    cost = raw_dict.get("response_cost")
    if cost is None:
        cost = usage.get("cost")
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=float(cost) if cost is not None else None,
    )


_FINISH_ALIASES: dict[str, FinishReason] = {
    "stop": "stop",
    "end_turn": "stop",           # Anthropic
    "function_call": "tool_calls", # OpenAI legacy
    "tool_calls": "tool_calls",
    "tool_use": "tool_calls",     # Anthropic
    "length": "length",
    "max_tokens": "length",       # Anthropic
    "content_filter": "content_filter",
}


def _normalise_finish_reason(raw: Any) -> FinishReason:
    """Map a provider finish reason to one of our 5 canonical values."""
    if not isinstance(raw, str):
        return "other"
    return _FINISH_ALIASES.get(raw, "other")


# Keep a reference to the Protocol so static type checkers can verify
# LiteLLMProvider satisfies the interface.
_: type[ModelProvider] = LiteLLMProvider
