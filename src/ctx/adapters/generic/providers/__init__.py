"""ctx.adapters.generic.providers — provider abstraction layer.

Public API:
  ModelProvider        - Protocol every backend satisfies
  CompletionResponse   - normalised call result
  Message, ToolCall, ToolDefinition, Usage - data types
  LiteLLMProvider      - the default backend (OpenRouter / Ollama / direct)
  get_provider         - factory with sensible defaults

Usage:

    from ctx.adapters.generic.providers import get_provider, Message

    provider = get_provider(
        default_model="openrouter/anthropic/claude-opus-4.7",
        api_key_env="OPENROUTER_API_KEY",
    )
    response = provider.complete([Message("user", "Hello")])
    print(response.content)

See ``base.py`` for Protocol + types, ``litellm_provider.py`` for the
default implementation. Plan 001 Phase H1.
"""

from __future__ import annotations

from ctx.adapters.generic.providers.base import (
    CompletionResponse,
    FinishReason,
    Message,
    MessageRole,
    ModelProvider,
    ToolCall,
    ToolDefinition,
    Usage,
)
from ctx.adapters.generic.providers.litellm_provider import LiteLLMProvider


def get_provider(
    *,
    default_model: str,
    base_url: str | None = None,
    api_key_env: str | None = None,
    timeout: float = 120.0,
) -> ModelProvider:
    """Return a configured ``ModelProvider`` instance.

    Currently always returns a ``LiteLLMProvider`` — the factory is a
    seam so tests (and, later, direct-SDK bypasses) can substitute
    without touching every call site.

    Conventional ``api_key_env`` by provider prefix:
      openrouter/... → OPENROUTER_API_KEY
      anthropic/...  → ANTHROPIC_API_KEY
      openai/...     → OPENAI_API_KEY
      gemini/...     → GEMINI_API_KEY
      mistral/...    → MISTRAL_API_KEY
      ollama/...     → no key (local)
    """
    return LiteLLMProvider(
        default_model=default_model,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout=timeout,
    )


__all__ = [
    "CompletionResponse",
    "FinishReason",
    "LiteLLMProvider",
    "Message",
    "MessageRole",
    "ModelProvider",
    "ToolCall",
    "ToolDefinition",
    "Usage",
    "get_provider",
]
