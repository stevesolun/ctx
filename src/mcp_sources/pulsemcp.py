"""src/mcp_sources/pulsemcp.py -- Source for pulsemcp.com Sub-Registry API.

Cursor-paginated JSON API. Requires PULSEMCP_API_KEY and PULSEMCP_TENANT_ID
environment variables.

Rate limits: 200/min, 5000/hr, 10000/day. On 429 this module raises
immediately rather than sleeping — Phase 6 will add retry logic.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import date
from urllib.error import HTTPError
from urllib.parse import quote as _url_quote

from mcp_sources.base import Source, fetch_text, read_cache, write_cache

__all__ = ["SOURCE"]

API_BASE = "https://www.pulsemcp.com/api/v0.1"
_DEFAULT_PAGE_SIZE = 100  # maximum allowed by the pulsemcp API

# Mapping from package registry name to canonical language tag.
_REGISTRY_LANG: dict[str, str] = {
    "npm": "typescript",
    "pypi": "python",
    "cargo": "rust",
    "go": "go",
    "rubygems": "ruby",
    "nuget": "csharp",
    "maven": "java",
    "packagist": "php",
}


# ── Credential helpers ────────────────────────────────────────────────────────


class _MissingPulsemcpCredentialsError(RuntimeError):
    """Raised when PULSEMCP_API_KEY or PULSEMCP_TENANT_ID are unset."""


class _InvalidPulsemcpCredentialError(ValueError):
    """Raised when a credential value contains characters that would inject
    HTTP headers (CR, LF, or colon).

    ``urllib.request.Request`` does not sanitize header values on Python
    3.11/3.12/3.13 — a credential value containing ``\\r\\nX-Other: ...``
    would inject an arbitrary header on every request. We reject such
    values up front rather than rely on the transport.
    """


def _credentials() -> dict[str, str]:
    """Return auth headers built from environment variables.

    Both ``PULSEMCP_API_KEY`` and ``PULSEMCP_TENANT_ID`` must be set.
    Obtain them from https://www.pulsemcp.com/settings/api-keys.

    Values are validated against header-injection: a value containing
    CR, LF, or colon characters would be passed verbatim into the HTTP
    request line by ``urllib`` and could be used to inject arbitrary
    headers. We refuse such values explicitly.

    Raises:
        _MissingPulsemcpCredentialsError: Either variable is absent or empty.
        _InvalidPulsemcpCredentialError: A value contains CR/LF/colon.
    """
    api_key = os.environ.get("PULSEMCP_API_KEY", "").strip()
    tenant_id = os.environ.get("PULSEMCP_TENANT_ID", "").strip()

    missing = []
    if not api_key:
        missing.append("PULSEMCP_API_KEY")
    if not tenant_id:
        missing.append("PULSEMCP_TENANT_ID")

    if missing:
        raise _MissingPulsemcpCredentialsError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Obtain API credentials from https://www.pulsemcp.com/settings/api-keys "
            "and set them before running the pulsemcp source."
        )

    # Header injection guard. Values are never echoed in the error
    # message — an attacker who can set the env can already see them,
    # but tracebacks routed elsewhere (Sentry, CI logs) should not.
    for label, value in (
        ("PULSEMCP_API_KEY", api_key),
        ("PULSEMCP_TENANT_ID", tenant_id),
    ):
        if any(ch in value for ch in ("\r", "\n", ":")):
            raise _InvalidPulsemcpCredentialError(
                f"{label} contains CR, LF, or colon — refused to prevent "
                "HTTP header injection"
            )

    return {
        "X-API-Key": api_key,
        "X-Tenant-ID": tenant_id,
    }


# ── Record mapping ────────────────────────────────────────────────────────────


def _infer_language(packages: object) -> str | None:
    """Infer language from the first recognized package registry name."""
    if not isinstance(packages, list):
        return None
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        registry = pkg.get("registry_name")
        if isinstance(registry, str) and registry.lower() in _REGISTRY_LANG:
            return _REGISTRY_LANG[registry.lower()]
    return None


def _infer_transports(packages: object) -> list[str]:
    """Extract transport values from ``--transport`` package arguments."""
    if not isinstance(packages, list):
        return []
    found: list[str] = []
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        args = pkg.get("package_arguments")
        if not isinstance(args, list):
            continue
        # Arguments are often dicts with "name" / "value" keys, or plain strings.
        it = iter(args)
        for arg in it:
            if isinstance(arg, dict):
                name = arg.get("name", "")
                value = arg.get("value")
                if name == "--transport" and isinstance(value, str) and value:
                    found.append(value)
            elif isinstance(arg, str) and arg == "--transport":
                # Positional-style: next element is the value
                try:
                    nxt = next(it)
                    if isinstance(nxt, str) and nxt:
                        found.append(nxt)
                except StopIteration:
                    pass
    return found


def _to_record(server_obj: dict, meta: dict) -> dict | None:
    """Map one pulsemcp API entry to a McpRecord-compatible raw dict.

    Returns ``None`` when the entry is too malformed to be useful (missing
    name and no fallback). Logs a one-line warning to stderr per skipped
    entry so the caller can diagnose upstream data quality without crashing.

    Args:
        server_obj: The ``server`` sub-object from one pulsemcp list entry.
        meta: The ``_meta`` sub-object from the same entry.

    Returns:
        Raw dict accepted by ``McpRecord.from_dict``, or ``None``.
    """
    name: str | None = server_obj.get("name")
    if not isinstance(name, str) or not name.strip():
        # Don't echo the full server_obj — upstream payloads may grow to
        # contain arbitrary data we don't want surfacing in stderr/logs.
        # The name field's absence is itself the diagnostic.
        print(
            "[pulsemcp] skip: entry missing required 'name' field",
            file=sys.stderr,
        )
        return None

    description: str | None = server_obj.get("description")

    # Classify repository URL
    github_url: str | None = None
    homepage_url: str | None = None
    repo = server_obj.get("repository")
    if isinstance(repo, dict):
        url = repo.get("url")
        source = repo.get("source", "")
        if isinstance(url, str) and url.strip():
            if isinstance(source, str) and source.lower() == "github":
                github_url = url.strip()
            else:
                homepage_url = url.strip()

    # Timestamps and official flag from _meta
    last_commit_at: str | None = None
    tags: list[str] = []

    server_meta = meta.get("com.pulsemcp/server")
    if isinstance(server_meta, dict) and server_meta.get("isOfficial") is True:
        tags.append("official")

    version_meta = meta.get("com.pulsemcp/server-version")
    if isinstance(version_meta, dict):
        published = version_meta.get("publishedAt")
        if isinstance(published, str) and published.strip():
            last_commit_at = published.strip()

    packages = server_obj.get("packages")
    language = _infer_language(packages)
    transports = _infer_transports(packages)

    record: dict = {
        "name": name.strip(),
        "sources": ["pulsemcp"],
        "tags": tags if tags else ["uncategorized"],
    }
    if description:
        record["description"] = description
    if github_url:
        record["github_url"] = github_url
    if homepage_url:
        record["homepage_url"] = homepage_url
    if last_commit_at:
        record["last_commit_at"] = last_commit_at
    if language:
        record["language"] = language
    if transports:
        record["transports"] = transports

    return record


# ── Pagination ────────────────────────────────────────────────────────────────


def _build_url(cursor: str | None, page_size: int) -> str:
    """Build a paginated API URL.

    The opaque cursor from upstream is URL-encoded with ``safe=''`` to
    prevent query-string smuggling: a cursor containing ``&`` would
    otherwise inject extra parameters, and ``#`` would silently truncate
    the URL at the fragment boundary.
    """
    url = f"{API_BASE}/servers?limit={page_size}"
    if cursor:
        url += f"&cursor={_url_quote(cursor, safe='')}"
    return url


def _fetch_page(
    cursor: str | None,
    *,
    page_size: int,
    page_index: int,
    auth_headers: dict[str, str],
    refresh: bool,
) -> dict:
    """Fetch one page from the pulsemcp API, with caching.

    Cache key: ``raw/marketplace-dumps/pulsemcp/<date>/page-<n>.json``.
    The cursor is not stable across days, so the date-scoped directory
    naturally expires stale cursors without manual cleanup.

    Args:
        cursor: Opaque pagination cursor, or ``None`` for the first page.
        page_size: Number of records to request per page.
        page_index: Zero-based page counter (used for the cache filename).
        auth_headers: Dict with ``X-API-Key`` and ``X-Tenant-ID``.
        refresh: When ``True``, skip cache and re-fetch from network.

    Returns:
        Parsed JSON dict from the API response.

    Raises:
        RuntimeError: API returned HTTP 429 (rate limited).
        urllib.error.HTTPError: Any other non-2xx response.
    """
    today = date.today().isoformat()
    # Nest under a date subdirectory. cache_path only accepts a plain basename
    # (no path separators), so we encode the date into the basename itself.
    basename = f"{today}--page-{page_index:04d}.json"
    source_name = "pulsemcp"

    cached: str | None = None
    if not refresh:
        cached = read_cache(source_name, basename)

    if cached is not None:
        return json.loads(cached)  # type: ignore[no-any-return]

    url = _build_url(cursor, page_size)
    try:
        raw_text = fetch_text(url, headers=auth_headers)
    except HTTPError as exc:
        if exc.code == 429:
            retry_after = exc.headers.get("Retry-After", "unknown") if exc.headers else "unknown"
            raise RuntimeError(
                f"pulsemcp API rate-limited (HTTP 429). "
                f"Retry-After: {retry_after}. "
                f"Limits: 200/min, 5000/hr, 10000/day."
            ) from exc
        raise

    write_cache(source_name, basename, raw_text)
    return json.loads(raw_text)  # type: ignore[no-any-return]


# ── Source class ──────────────────────────────────────────────────────────────


class _PulsemcpSource:
    name = "pulsemcp"
    homepage = "https://www.pulsemcp.com/servers"

    def fetch(self, *, limit: int | None = None, refresh: bool = False) -> Iterator[dict]:
        """Walk pulsemcp pages until exhausted or *limit* records yielded.

        Credentials are read from ``PULSEMCP_API_KEY`` and
        ``PULSEMCP_TENANT_ID`` environment variables; a descriptive error
        is raised if either is absent.

        Args:
            limit: Maximum records to yield. ``None`` yields all available.
            refresh: Bypass the local raw cache and re-fetch from network.

        Yields:
            Raw dicts suitable for ``McpRecord.from_dict()``.

        Raises:
            _MissingPulsemcpCredentialsError: Env vars not set.
            RuntimeError: HTTP 429 rate-limit hit.
            urllib.error.HTTPError: Other non-2xx API response.
        """
        auth_headers = _credentials()  # raises early if env vars missing

        yielded = 0
        cursor: str | None = None
        page_index = 0

        while True:
            if limit is not None and yielded >= limit:
                break

            page = _fetch_page(
                cursor,
                page_size=_DEFAULT_PAGE_SIZE,
                page_index=page_index,
                auth_headers=auth_headers,
                refresh=refresh,
            )

            servers = page.get("servers", [])
            if not isinstance(servers, list):
                print(
                    "[pulsemcp] unexpected 'servers' shape on page "
                    f"{page_index}; stopping.",
                    file=sys.stderr,
                )
                break

            for entry in servers:
                if limit is not None and yielded >= limit:
                    return

                if not isinstance(entry, dict):
                    print(
                        f"[pulsemcp] skip: non-dict entry on page {page_index}",
                        file=sys.stderr,
                    )
                    continue

                server_obj = entry.get("server", {})
                meta = entry.get("_meta", {})
                if not isinstance(server_obj, dict):
                    server_obj = {}
                if not isinstance(meta, dict):
                    meta = {}

                record = _to_record(server_obj, meta)
                if record is None:
                    continue

                yield record
                yielded += 1

            # Advance cursor; stop when absent or empty
            metadata = page.get("metadata", {})
            next_cursor = metadata.get("nextCursor") if isinstance(metadata, dict) else None
            if not next_cursor:
                break

            cursor = next_cursor
            page_index += 1


SOURCE: Source = _PulsemcpSource()
