"""Policy-free Claude Code projections for committed recommendations."""

from __future__ import annotations

from ctx.adapters.recommendation_presentation import (
    render_committed_query_context,
    render_present_bundle_context,
)
from ctx.engine.protocol import Transition
from ctx.runtime.query_decision import QueryDecisionValidationError


ClaudeHookEnvelope = dict[str, dict[str, str]]


def render_committed_query_hook(decision: object) -> ClaudeHookEnvelope | None:
    """Purely project one closed decision beside its submitted prompt.

    This renderer is intentionally repeatable.  The executable hook handler,
    not this policy-free function, owns session-scoped one-use delivery.
    """

    try:
        context = render_committed_query_context(
            decision,
            expected_host_context_id="claude-code",
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


def render_recommendation_hook(transition: Transition) -> ClaudeHookEnvelope | None:
    """Wrap committed CTX recommendations in Claude Code's proven hook shape."""

    context = render_present_bundle_context(transition)
    if context is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }


__all__ = [
    "ClaudeHookEnvelope",
    "render_committed_query_hook",
    "render_recommendation_hook",
]
