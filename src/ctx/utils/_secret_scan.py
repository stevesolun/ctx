"""Shared secret-shape detection for catalog and install inputs."""

from __future__ import annotations

import os
import re

SECRET_KEY_MARKERS: tuple[str, ...] = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "access_key",
    "refresh_token",
    "client_secret",
    "authorization",
    "bearer",
)

SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|"
    r"PRIVATE_KEY|CREDENTIAL|ACCESS_KEY|CLIENT_SECRET|AUTHORIZATION|BEARER)"
    r"[A-Za-z0-9_]*)=([^\s'\";]+)",
    re.IGNORECASE,
)

TOKEN_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
)


def secret_key_like(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower())
    compact = normalized.replace("_", "")
    return any(marker in normalized or marker in compact for marker in SECRET_KEY_MARKERS)


def placeholder_secret_value(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return True
    if stripped.startswith("$"):
        return True
    if stripped.startswith("%") and stripped.endswith("%") and len(stripped) > 2:
        return True
    if stripped.startswith("<") and stripped.endswith(">"):
        return True
    lower = stripped.lower()
    return lower in {
        "changeme",
        "change-me",
        "placeholder",
        "redacted",
        "example",
    }


def _value_has_token_pattern(value: str) -> bool:
    if placeholder_secret_value(value):
        return False
    return any(pattern.search(value) for pattern in TOKEN_VALUE_PATTERNS)


def find_inline_secret(obj: object, *, path: str = "") -> str | None:
    """Return the first path that appears to contain an inline secret."""
    if isinstance(obj, dict):
        for raw_key, value in obj.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if secret_key_like(key):
                if isinstance(value, str):
                    if not placeholder_secret_value(value):
                        return child_path
                elif value is not None:
                    return child_path
            nested = find_inline_secret(value, path=child_path)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            nested = find_inline_secret(value, path=f"{path}[{index}]")
            if nested is not None:
                return nested
    elif isinstance(obj, str) and _value_has_token_pattern(obj):
        return path or "<value>"
    return None


def find_inline_secret_arg(tokens: list[str]) -> str | None:
    for token in tokens:
        assignment = SECRET_ASSIGNMENT_RE.search(token)
        if assignment and not placeholder_secret_value(assignment.group(2)):
            return assignment.group(1)
        if token.startswith("--") and "=" in token:
            key, value = token.split("=", 1)
            if secret_key_like(key) and not placeholder_secret_value(value):
                return key

    for index, token in enumerate(tokens[:-1]):
        if not token.startswith("-"):
            continue
        key = token.lstrip("-").replace("-", "_")
        value = tokens[index + 1]
        if (
            secret_key_like(key)
            and value
            and not value.startswith("-")
            and not placeholder_secret_value(value)
        ):
            return token

    for token in tokens:
        for pattern in TOKEN_VALUE_PATTERNS:
            if pattern.search(token):
                return "[secret-value]"
    return None


def redact_secret_text(text: str) -> str:
    if not text:
        return text
    redacted = SECRET_ASSIGNMENT_RE.sub(r"\1=[redacted]", text)
    for pattern in TOKEN_VALUE_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    secret_env_values = sorted(
        (
            value
            for key, value in os.environ.items()
            if value and len(value) >= 6 and secret_key_like(key)
        ),
        key=len,
        reverse=True,
    )
    for value in secret_env_values:
        redacted = redacted.replace(value, "[redacted]")
    return redacted
