#!/usr/bin/env python3
"""
mcp_sources/base.py -- Shared primitives for MCP catalog source modules.

Each concrete source (awesome-mcp, pulsemcp, ...) lives in its own sibling
module and exposes a ``SOURCE`` object that satisfies the :class:`Source`
protocol.  This module provides only the plumbing: the protocol shape, a
small cache-path helper wired to the wiki layout, and a tightly constrained
URL fetcher that refuses to touch anything outside :data:`ALLOWED_HOSTS`.

Phase-2a deliberately avoids a third-party HTTP client.  ``urllib.request``
from the standard library is used with redirects disabled so a malicious
listing page cannot pivot a harvest run into an unrelated host.  When
``pulsemcp`` is added in Phase-2b, ``httpx`` becomes the transport for that
source only; this module stays stdlib-only.

This module must not print.  Callers (CLIs and batch scripts) own user
output; library code stays quiet so it can compose cleanly under pipes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

from _fs_utils import atomic_write_text
# Import the module rather than `cfg` so that ctx_config.reload()
# (used by test_config.py) doesn't leave us holding a stale reference.
import ctx_config as _ctx_config

__all__ = [
    "ALLOWED_HOSTS",
    "Source",
    "cache_path",
    "fetch_text",
    "read_cache",
    "write_cache",
]


# ── Source protocol ───────────────────────────────────────────────────────────


@runtime_checkable
class Source(Protocol):
    """One MCP catalog source.

    Implementations live in ``mcp_sources/<name>.py`` and expose a
    module-level ``SOURCE`` attribute that satisfies this protocol.  The
    registry in :mod:`mcp_sources.__init__` imports each module eagerly and
    maps its ``name`` into ``SOURCES`` so the CLI dispatcher can resolve
    ``--source <name>`` without reflection.
    """

    name: str
    """Canonical identifier, e.g. ``"awesome-mcp"`` or ``"pulsemcp"``."""

    homepage: str
    """Human-readable URL for logs and docs."""

    def fetch(
        self, *, limit: int | None = None, refresh: bool = False
    ) -> Iterator[dict]:
        """Yield raw record dicts suitable for ``McpRecord.from_dict``.

        Args:
            limit: Maximum number of records to yield.  ``None`` yields
                everything the source exposes.
            refresh: When ``True``, bypass the local raw cache and fetch
                fresh upstream content before parsing.
        """
        ...


# ── Raw cache ─────────────────────────────────────────────────────────────────

# Wiki layout convention documented in ``docs/marketplace-registry.md``.
# Each source gets its own subdirectory so dumps from different harvesters
# never collide, and the cache as a whole stays under the wiki root so it
# inherits the wiki's backup and lifecycle policies.
_CACHE_SUBDIR = ("raw", "marketplace-dumps")


def _validate_cache_component(value: str, *, label: str) -> str:
    """Reject path-traversal attempts in cache path components.

    ``Path.joinpath`` does not strip ``..`` segments, so a malicious or
    buggy caller passing ``"../../etc"`` for *source_name* or *basename*
    would resolve outside the wiki root. We constrain both components
    to plain filenames: no path separators, no leading dot, non-empty.
    """
    if not value:
        raise ValueError(f"{label} must be non-empty")
    if "/" in value or "\\" in value or value.startswith(".") or value in {".", ".."}:
        raise ValueError(
            f"{label} {value!r} contains path separators or leading dot; refusing to join"
        )
    return value


def cache_path(source_name: str, basename: str) -> Path:
    """Return the on-disk path for a cached artifact belonging to *source_name*.

    The caller chooses *basename* (for example ``"README-2026-04-20.md"`` or
    ``"listing-page-1.json"``); this helper only joins it with the conventional
    cache root.  The file is not created here -- cache writes go through
    :func:`write_cache`.

    Both *source_name* and *basename* are validated as plain filenames
    (no path separators, no leading dot) so a hostile caller cannot
    write outside the cache root.
    """
    _validate_cache_component(source_name, label="source_name")
    _validate_cache_component(basename, label="basename")
    return _ctx_config.cfg.wiki_dir.joinpath(*_CACHE_SUBDIR, source_name, basename)


def read_cache(source_name: str, basename: str) -> str | None:
    """Return cached text for *(source_name, basename)*, or ``None`` when missing."""
    path = cache_path(source_name, basename)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_cache(source_name: str, basename: str, content: str) -> Path:
    """Atomically write *content* to the cache slot for *(source_name, basename)*.

    Delegates to :func:`_fs_utils.atomic_write_text` so the cache cannot end
    up half-written on a crash or on Windows AV-induced rename races.
    """
    path = cache_path(source_name, basename)
    atomic_write_text(path, content)
    return path


# ── Safe URL fetch (SSRF-constrained) ─────────────────────────────────────────

# Phase-2a sources must only reach these hosts.  The allowlist is a frozen set
# so it is cheap to check on every request and trivially auditable.  Adding a
# new host is an explicit code change -- no config knob, no environment
# override -- because the whole point of the list is that the surface area of
# the fetcher stays small and reviewable.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "raw.githubusercontent.com",
        "github.com",
        "api.github.com",
        "www.pulsemcp.com",
        "pulsemcp.com",
    }
)


# Hard ceiling on a single response body. The README we currently fetch
# is ~600 KB; pulsemcp listing pages are smaller. 10 MB leaves comfortable
# headroom while bounding worst-case memory if a host streams indefinitely.
MAX_RESPONSE_BYTES: int = 10 * 1024 * 1024


class _DisallowedHostError(ValueError):
    """Raised when a URL's host is not in :data:`ALLOWED_HOSTS`."""


class _ResponseTooLargeError(ValueError):
    """Raised when a response body exceeds :data:`MAX_RESPONSE_BYTES`."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every 3xx redirect.

    The default :class:`urllib.request.HTTPRedirectHandler` will happily chase
    redirects to arbitrary hosts, which completely defeats the allowlist
    check we perform on the *original* URL.  Refusing all redirects is
    simpler than re-validating each hop and still covers every legitimate
    upstream we actually use -- GitHub's raw content and API endpoints
    return 200 directly on canonical URLs.  Callers who hit a redirect
    should surface the error and fix the URL.
    """

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        raise HTTPError(
            req.full_url,
            code,
            f"redirect to {newurl!r} refused (fetch_text does not follow redirects)",
            headers,  # type: ignore[arg-type]
            fp,  # type: ignore[arg-type]
        )


def _validate_host(url: str) -> str:
    """Return the URL's host if it's in :data:`ALLOWED_HOSTS`; raise otherwise.

    https-only by policy. All allowlisted hosts serve TLS; admitting plain
    http would expose Phase 4's GitHub token header to DNS-poisoning and
    captive-portal MITM.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise _DisallowedHostError(
            f"unsupported URL scheme {parsed.scheme!r} (https-only): {url!r}"
        )
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise _DisallowedHostError(
            f"host {host!r} is not in the allowlist; refusing to fetch {url!r}"
        )
    return host


def _build_opener() -> OpenerDirector:
    """Build an opener that refuses redirects."""
    return build_opener(_NoRedirectHandler())


def fetch_text(
    url: str,
    *,
    timeout: float = 30.0,
    user_agent: str = "ctx-mcp-fetch/0.1",
    headers: dict[str, str] | None = None,
) -> str:
    """GET *url* and return its UTF-8 body.

    The host must be in :data:`ALLOWED_HOSTS` or a :class:`ValueError` is
    raised before any network I/O.  Redirects are hard-disabled -- a 3xx
    response raises :class:`urllib.error.HTTPError` rather than silently
    pivoting to another host.

    Args:
        url: Absolute ``http(s)://`` URL.
        timeout: Per-request timeout in seconds.
        user_agent: ``User-Agent`` header value.  A descriptive default is
            used because some GitHub endpoints reject empty or stdlib-default
            agents.
        headers: Optional extra headers merged on top of the ``User-Agent``
            default.  Caller is responsible for not exposing secrets in logs;
            this function does not log header values.

    Raises:
        ValueError: URL host or scheme is not allowed.
        urllib.error.HTTPError: Non-2xx response (including refused redirect).
        urllib.error.URLError: Network-level failure (DNS, connection, TLS).
        TimeoutError: Request exceeded *timeout* seconds.
    """
    _validate_host(url)

    final_headers: dict[str, str] = {"User-Agent": user_agent}
    if headers:
        final_headers.update(headers)
    request = Request(url, headers=final_headers)
    opener = _build_opener()

    with opener.open(request, timeout=timeout) as response:
        status = getattr(response, "status", None)
        if status is None:  # Python <3.9 shims; defensive, never hit on 3.11+
            status = response.getcode()
        if not (200 <= int(status) < 300):
            raise HTTPError(
                url,
                int(status),
                f"non-2xx response: {status}",
                response.headers,  # type: ignore[arg-type]
                None,
            )
        # Hard cap on response body size. The awesome-mcp README is
        # ~600 KB today; pulsemcp listing pages are smaller. 10 MB
        # leaves comfortable headroom while preventing a malicious or
        # misbehaving allowlisted host from streaming arbitrary memory.
        # We read MAX+1 bytes so we can detect overflow rather than
        # silently truncate.
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLargeError(
                f"response body exceeded {MAX_RESPONSE_BYTES:,} bytes for {url!r}"
            )

    # Charset handling: fall back to UTF-8 if the server omits a charset or
    # advertises something we can't decode.  The sources we target are all
    # UTF-8 in practice; a bad charset hint should not silently corrupt
    # downstream parsing.
    return raw.decode("utf-8", errors="replace")
