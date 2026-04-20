#!/usr/bin/env python3
"""
mcp_entity.py -- Frozen ``McpRecord`` dataclass for the MCP-server catalog.

``ctx`` already indexes skills and agents as first-class entities in the
knowledge graph. MCP servers are the third subject type; this module is
the Phase-1 schema they are normalised into before they ever touch disk.

Design notes
------------
* The record is ``frozen=True`` with ``tuple`` collections so the same
  instance can be safely shared across the fetcher, the dedup layer, and
  the graph builder without defensive copies at every hop.
* ``from_dict`` is the sole entry point for raw fetcher payloads. It is
  tolerant of garbage (unknown transports, messy slugs, short-ref GitHub
  URLs) and fails loudly only when the slug is unrecoverable — that is
  the one field downstream code cannot repair.
* No I/O. The module is pure so it can be unit-tested without a network,
  a filesystem, or the rest of the ``ctx`` runtime wiring.

See ``src/wiki_utils.SAFE_NAME_RE`` for the legacy skill-name pattern;
MCP slugs enforce the stricter Tier-2 contract (lowercase + hyphens
only) used by ``skill_add_detector.validate_user_supplied_slug``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "ALLOWED_TRANSPORTS",
    "MCP_SLUG_RE",
    "McpRecord",
    "canonicalize_github_url",
    "normalize_slug",
]

# Tier-2 slug contract: lowercase, hyphens only, no leading/trailing
# hyphen, no consecutive hyphens. Mirrors the wiki's stricter hook-side
# validator so MCP entries are safe to use as filesystem paths and
# graph node ids without further escaping.
MCP_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Subset of the MCP spec's transport tags we consider meaningful. Any
# fetcher input outside this set is silently dropped — see
# ``_normalize_transports``.
ALLOWED_TRANSPORTS: frozenset[str] = frozenset(
    {"stdio", "http", "sse", "websocket"}
)

# GitHub URL matcher. Accepts http/https, optional ``www.``, trailing
# ``.git`` / slash, and captures org + repo for canonical reassembly.
# Host + scheme are matched case-insensitively (RFC 3986 §3.1 / §3.2.2);
# the org/repo path is preserved verbatim so display case round-trips.
_GITHUB_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<org>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*?)"
    r"(?:\.git)?/?$",
    re.IGNORECASE,
)

# Short-ref matcher (``Org/Repo``) — only considered when the input
# contains no scheme and exactly one ``/``. Kept separate from the URL
# matcher so a stray ``/`` in free-text URLs doesn't falsely match.
_GITHUB_SHORT_REF_RE = re.compile(
    r"^(?P<org>[A-Za-z0-9][A-Za-z0-9._-]*)/"
    r"(?P<repo>[A-Za-z0-9][A-Za-z0-9._-]*?)"
    r"(?:\.git)?/?$"
)

_DESCRIPTION_MAX = 300
_DESCRIPTION_TRUNCATE_AT = 297
_DESCRIPTION_FALLBACK = "No description available."


def normalize_slug(raw: str) -> str:
    """Normalize a free-text name/slug to ``[a-z0-9]+(-[a-z0-9]+)*``.

    Lowercases, collapses any run of non-alphanumeric characters to a
    single hyphen, and strips leading/trailing hyphens. Raises
    ``ValueError`` if nothing usable survives — downstream code cannot
    invent a slug, so failing here is the correct behaviour.
    """
    if not isinstance(raw, str):
        raise ValueError(f"slug must be str, got {type(raw).__name__}")
    lowered = raw.strip().lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not collapsed:
        raise ValueError(f"slug normalization produced empty string from {raw!r}")
    # The collapse already guarantees the pattern, but re-assert so any
    # future regex change fails loudly rather than silently accepting
    # malformed output.
    if not MCP_SLUG_RE.match(collapsed):
        raise ValueError(f"slug {collapsed!r} does not match {MCP_SLUG_RE.pattern}")
    return collapsed


def canonicalize_github_url(raw: str | None) -> str | None:
    """Canonicalize a GitHub URL to ``https://github.com/<org>/<repo>``.

    Accepts full URLs (``https://github.com/Org/Repo``), scheme-less
    variants (``github.com/Org/Repo.git``), and short refs
    (``Org/Repo``). Returns ``None`` when the input is ``None``, empty,
    or not recognisable as a GitHub reference.

    Case in the org/repo path is preserved — GitHub is case-insensitive
    for routing but users expect display case to round-trip. Dedup is
    handled separately by :meth:`McpRecord.canonical_dedup_key`.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    m = _GITHUB_URL_RE.match(candidate)
    if m is None and "://" not in candidate and candidate.count("/") == 1:
        m = _GITHUB_SHORT_REF_RE.match(candidate)
    if m is None:
        return None

    return f"https://github.com/{m.group('org')}/{m.group('repo')}"


def _normalize_tags(raw: object) -> tuple[str, ...]:
    """Dedupe, lowercase, sort. Fall back to ``('uncategorized',)`` if empty."""
    if not raw:
        return ("uncategorized",)
    if isinstance(raw, str):
        # Tolerate a single comma-separated string from scruffier fetchers.
        items: list[str] = [p for p in (s.strip() for s in raw.split(",")) if p]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(t).strip() for t in raw if str(t).strip()]
    else:
        return ("uncategorized",)
    cleaned = {t.lower() for t in items if t}
    if not cleaned:
        return ("uncategorized",)
    return tuple(sorted(cleaned))


def _normalize_transports(raw: object) -> tuple[str, ...]:
    """Filter to ``ALLOWED_TRANSPORTS``, lowercase, dedupe, sort."""
    if not raw:
        return ()
    if isinstance(raw, str):
        items: list[str] = [p for p in (s.strip() for s in raw.split(",")) if p]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(t).strip() for t in raw if str(t).strip()]
    else:
        return ()
    kept = {t.lower() for t in items if t.lower() in ALLOWED_TRANSPORTS}
    return tuple(sorted(kept))


def _normalize_description(raw: object) -> str:
    """Trim, fall back to placeholder when empty, truncate to 300 chars."""
    if not isinstance(raw, str):
        return _DESCRIPTION_FALLBACK
    trimmed = raw.strip()
    if not trimmed:
        return _DESCRIPTION_FALLBACK
    if len(trimmed) > _DESCRIPTION_MAX:
        return trimmed[:_DESCRIPTION_TRUNCATE_AT] + "..."
    return trimmed


def _normalize_sources(raw: object) -> tuple[str, ...]:
    """Dedupe + sort sources. Empty input yields an empty tuple."""
    if not raw:
        return ()
    if isinstance(raw, str):
        items: list[str] = [p for p in (s.strip() for s in raw.split(",")) if p]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        return ()
    cleaned = {s for s in items if s}
    return tuple(sorted(cleaned))


def _optional_lower(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip().lower()
    return trimmed or None


def _optional_str(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    return trimmed or None


def _optional_author_type(raw: object) -> str | None:
    val = _optional_lower(raw)
    if val in {"org", "user"}:
        return val
    return None


def _optional_int(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        # ``bool`` is an ``int`` subclass; treat it as not-a-count.
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            try:
                return int(stripped)
            except ValueError:  # pragma: no cover — isdigit() guarded
                return None
    return None


@dataclass(frozen=True)
class McpRecord:
    """Normalized MCP-server record built from a fetcher payload.

    All collection fields are ``tuple`` to preserve the ``frozen=True``
    contract. ``raw`` is retained for debugging and is deep-copied on
    construction so callers cannot mutate the stored payload after the
    fact; it is never written to the entity page frontmatter.
    """

    # Required
    slug: str
    name: str
    description: str
    sources: tuple[str, ...]
    # Provenance
    github_url: str | None
    homepage_url: str | None
    # Classification
    tags: tuple[str, ...]
    transports: tuple[str, ...]
    language: str | None
    license: str | None
    # Authorship
    author: str | None
    author_type: str | None
    # Metrics — enriched later; None at initial fetch.
    stars: int | None
    last_commit_at: str | None
    # Raw payload — kept for debugging, never serialized to entity body.
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> McpRecord:
        """Build an ``McpRecord`` from a fetcher's raw dict.

        Normalizes slug (required), description, sources, tags, and
        transports; canonicalizes the GitHub URL; coerces optional
        string fields. Raises ``ValueError`` if the slug cannot be
        recovered from either ``slug`` or ``name``.
        """
        if not isinstance(data, dict):
            raise ValueError(f"from_dict expected dict, got {type(data).__name__}")

        # Prefer an explicit slug; fall back to name. Both are fed
        # through the same normalizer so the canonical form is
        # identical regardless of which field the fetcher populated.
        raw_slug = data.get("slug") or data.get("name") or ""
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            raise ValueError("McpRecord requires non-empty 'slug' or 'name'")
        slug = normalize_slug(raw_slug)

        raw_name = data.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        else:
            # Fetcher didn't supply a display name — use the slug so the
            # record is still renderable.
            name = slug

        description = _normalize_description(data.get("description"))
        sources = _normalize_sources(data.get("sources"))

        github_url = canonicalize_github_url(
            data.get("github_url") if isinstance(data.get("github_url"), str) else None
        )
        homepage_url = _optional_str(data.get("homepage_url"))

        tags = _normalize_tags(data.get("tags"))
        transports = _normalize_transports(data.get("transports"))
        language = _optional_lower(data.get("language"))
        license_ = _optional_str(data.get("license"))

        author = _optional_str(data.get("author"))
        author_type = _optional_author_type(data.get("author_type"))

        stars = _optional_int(data.get("stars"))
        last_commit_at = _optional_str(data.get("last_commit_at"))

        raw_payload = data.get("raw")
        if isinstance(raw_payload, dict):
            raw_copy: dict[str, Any] = copy.deepcopy(raw_payload)
        else:
            # No explicit ``raw`` key: snapshot the entire input so
            # debugging has the original fetcher output available.
            raw_copy = copy.deepcopy(data)

        return cls(
            slug=slug,
            name=name,
            description=description,
            sources=sources,
            github_url=github_url,
            homepage_url=homepage_url,
            tags=tags,
            transports=transports,
            language=language,
            license=license_,
            author=author,
            author_type=author_type,
            stars=stars,
            last_commit_at=last_commit_at,
            raw=raw_copy,
        )

    def to_frontmatter(self) -> dict[str, Any]:
        """Return a dict ready for YAML frontmatter serialization.

        Excludes ``raw``. Includes ``type: mcp-server`` and ``created`` /
        ``updated`` placeholders (both ``None``) so the caller — which
        owns wall-clock time — can fill them without re-shaping the
        dict. Lists are emitted as ``list`` (YAML-friendly) rather than
        the internal ``tuple`` representation.
        """
        return {
            "type": "mcp-server",
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "sources": list(self.sources),
            "github_url": self.github_url,
            "homepage_url": self.homepage_url,
            "tags": list(self.tags),
            "transports": list(self.transports),
            "language": self.language,
            "license": self.license,
            "author": self.author,
            "author_type": self.author_type,
            "stars": self.stars,
            "last_commit_at": self.last_commit_at,
            "created": None,
            "updated": None,
        }

    def entity_relpath(self) -> Path:
        """Return ``Path('<shard>/<slug>.md')``.

        ``shard`` is ``slug[0]`` for alphabetic slugs and the literal
        ``'0-9'`` when the slug starts with a digit. Sharding keeps the
        entity directory from growing into a single multi-thousand-file
        mess as the MCP catalog fills in.
        """
        first = self.slug[0]
        shard = "0-9" if first.isdigit() else first
        return Path(shard) / f"{self.slug}.md"

    def canonical_dedup_key(self) -> str:
        """Return the key used to detect duplicates across sources.

        When a GitHub URL is present we strip trailing slashes and
        lowercase the entire URL so ``https://github.com/Org/Repo`` and
        ``https://GitHub.com/org/repo/`` collapse to the same key.
        Otherwise we fall back to ``'slug:' + self.slug`` so records
        without a repo can still be deduplicated by their normalized
        slug.
        """
        if self.github_url:
            return self.github_url.rstrip("/").lower()
        return f"slug:{self.slug}"
