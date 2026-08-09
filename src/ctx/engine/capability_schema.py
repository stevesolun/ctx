"""Shared closed limits for capability plans and their host projection."""

from __future__ import annotations

import re


MAX_SELECTED_CAPABILITIES = 5
MAX_MATCHING_SIGNALS = 32
MAX_REASON_CODES = 16
MAX_CANONICAL_TOKEN_CHARS = 128
SHA256_HEX_CHARS = 64
MAX_HOST_CONTEXT_CHARS = 8_192

CAPABILITY_KINDS = frozenset({"skill", "agent", "mcp-server", "harness"})
PRESENTED_ACTIONABILITY_STATES = frozenset({"load", "install", "manual"})

_CAPABILITY_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._@-]{0,127}\Z")


def validate_capability_identity(
    capability_id: object,
    kind: object,
) -> tuple[str, str]:
    """Return one authoritative ``kind:name`` identity or reject it.

    Capability names cannot contain another colon, so callers cannot accept a
    prefix match while interpreting a different nested identity.
    """

    if not isinstance(capability_id, str) or not isinstance(kind, str):
        raise ValueError("kind and capability_id must form one canonical identity")
    prefix, separator, name = capability_id.partition(":")
    if (
        kind not in CAPABILITY_KINDS
        or len(capability_id) > MAX_CANONICAL_TOKEN_CHARS
        or separator != ":"
        or prefix != kind
        or _CAPABILITY_NAME_RE.fullmatch(name) is None
    ):
        raise ValueError("kind and capability_id must form one canonical identity")
    return capability_id, kind


__all__ = [
    "CAPABILITY_KINDS",
    "MAX_CANONICAL_TOKEN_CHARS",
    "MAX_HOST_CONTEXT_CHARS",
    "MAX_MATCHING_SIGNALS",
    "MAX_REASON_CODES",
    "MAX_SELECTED_CAPABILITIES",
    "PRESENTED_ACTIONABILITY_STATES",
    "SHA256_HEX_CHARS",
    "validate_capability_identity",
]
