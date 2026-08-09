"""Codex hook projection for one closed CTX query decision."""

from __future__ import annotations

from ctx.adapters.recommendation_presentation import render_committed_query_context
from ctx.runtime.query_decision import QueryDecisionValidationError


CodexQueryHookEnvelope = dict[str, dict[str, str]]


def render_committed_query_hook(
    decision: object,
) -> CodexQueryHookEnvelope | None:
    """Purely project a sealed decision into ``UserPromptSubmit`` context.

    This renderer is intentionally repeatable.  The executable hook handler,
    not this policy-free function, owns session-scoped one-use delivery.
    """

    try:
        context = render_committed_query_context(
            decision,
            expected_host_context_id="codex",
        )
    except QueryDecisionValidationError:
        return None
    if context is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


__all__ = ["CodexQueryHookEnvelope", "render_committed_query_hook"]
