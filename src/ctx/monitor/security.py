"""HTTP trust and token helpers for the ctx dashboard."""

from __future__ import annotations

import ipaddress
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit


MAX_POST_BODY_BYTES = 64 * 1024
READ_TOKEN_COOKIE = "ctx_monitor_read_token"


def _canonical_host_name(hostname: str) -> str | None:
    value = hostname.lower()
    if not value or value.endswith(".."):
        return None
    if value.endswith("."):
        value = value[:-1]
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return value


def _canonical_http_authority(authority: str) -> tuple[str, int] | None:
    value = authority.strip()
    if not value or value != authority or any(char.isspace() for char in value):
        return None
    if any(char in value for char in "/?#\\"):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port
    except ValueError:
        return None
    if (
        not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.hostname
        or value.endswith(":")
    ):
        return None
    hostname = _canonical_host_name(parsed.hostname)
    return (hostname, 80 if port is None else port) if hostname is not None else None


def origin_matches_http_host(origin: str, host_header: str) -> bool:
    """Return whether an Origin is the exact HTTP origin named by Host."""
    if not origin or origin != origin.strip() or any(char.isspace() for char in origin):
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    scheme_delimiter = origin.find(":")
    raw_authority = (
        origin[scheme_delimiter + 3 :]
        if scheme_delimiter >= 0 and origin[scheme_delimiter + 1 : scheme_delimiter + 3] == "//"
        else ""
    )
    if (
        parsed.scheme.lower() != "http"
        or not parsed.netloc
        or not raw_authority
        or any(char in raw_authority for char in "/?#\\")
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        return False
    request_authority = _canonical_http_authority(host_header)
    origin_authority = _canonical_http_authority(parsed.netloc)
    return request_authority is not None and origin_authority == request_authority


def host_allows_mutations(host: str) -> bool:
    normalized = (host or "").strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    normalized = _canonical_host_name(normalized) or ""
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def request_host_name(host_header: str) -> str:
    """Return a hostname only when the complete HTTP Host authority is valid."""
    authority = _canonical_http_authority(host_header or "")
    return authority[0] if authority is not None else ""


def origin_host_name(origin: str) -> str:
    """Return the legacy normalized hostname; do not use for origin authorization."""
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return _canonical_host_name(parsed.hostname) or ""


def read_token_cookie(cookie_header: str) -> str:
    if not cookie_header:
        return ""
    try:
        cookie = SimpleCookie()
        cookie.load(cookie_header)
    except CookieError:
        return ""
    morsel = cookie.get(READ_TOKEN_COOKIE)
    return morsel.value if morsel is not None else ""
