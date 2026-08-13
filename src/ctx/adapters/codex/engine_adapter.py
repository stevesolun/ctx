"""Codex projections for committed recommendations and prepared content."""

from __future__ import annotations

from ctx.adapters.recommendation_presentation import (
    render_loaded_capability_context,
    render_present_bundle_context,
)
from ctx.engine.content import PreparedCapabilityContent
from ctx.engine.protocol import Transition


def render_recommendation_context(transition: Transition) -> str | None:
    """Return committed CTX recommendations as bounded Codex context."""

    return render_present_bundle_context(transition)


def render_prepared_context(prepared: PreparedCapabilityContent) -> str:
    """Return one action-authorized capability as lower-authority Codex context."""

    return render_loaded_capability_context((prepared,))


__all__ = ["render_prepared_context", "render_recommendation_context"]
