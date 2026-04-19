"""Fetch skills from the Tank registry (https://tankpkg.dev).

Wraps ``tankpkg.TankClient`` to turn a registry reference like
``@tank/nextjs@1.2.0`` into a local SKILL.md path plus a metadata dict
that ``skill_add`` can merge into its wiki frontmatter.

Tank is a security-first package manager for AI agent skills with a
SHA-512 integrity-verified download pipeline and a 6-stage static
scanner. When a skill is pulled via this module, the scan verdict,
audit score, integrity hash, and publish date are preserved as
provenance in the generated wiki entity page so the quality layer can
use them for rot detection and trust ranking.

Install the optional extra to use this module::

    pip install "claude-ctx[tank]"

or directly::

    pip install tank-sdk
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TankFetchError(RuntimeError):
    """Raised when a Tank-sourced fetch fails for a reason specific to this
    module (bad reference, missing SKILL.md, unsupported registry response).

    Network / auth / not-found errors bubble up as ``tankpkg.Tank*Error``
    subclasses so callers can distinguish transport failures from
    intake-level problems.
    """


@dataclass(frozen=True)
class _Ref:
    name: str
    version: str | None


_REF_SPLIT = re.compile(r"^(?P<name>@?[^@]+)(?:@(?P<version>.+))?$")


def parse_ref(ref: str) -> _Ref:
    """Split ``@scope/name@version`` references safely.

    Accepts all of: ``name``, ``name@1.2.3``, ``@scope/name``,
    ``@scope/name@1.2.3``. Rejects leading/trailing whitespace and empty
    names.
    """
    ref = ref.strip()
    if not ref:
        raise TankFetchError("empty Tank reference")
    match = _REF_SPLIT.match(ref)
    if not match:
        raise TankFetchError(f"invalid Tank reference: {ref!r}")
    name = match.group("name").strip()
    version = match.group("version")
    if version is not None:
        version = version.strip()
        if not version:
            version = None
    if not name:
        raise TankFetchError(f"missing skill name in Tank reference: {ref!r}")
    if name.endswith("/") or "//" in name or name.startswith("@/") or name in {"@", "/"}:
        raise TankFetchError(f"invalid skill name in Tank reference: {ref!r}")
    return _Ref(name=name, version=version)


_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9._-]")
_LEADING_NON_ALNUM = re.compile(r"^[^a-zA-Z0-9]+")
_SAFE_NAME_MAX_LEN = 128


def sanitize_slug(name: str) -> str:
    """Turn a Tank registry name into a ctx-safe slug.

    Output is guaranteed to match ``wiki_utils.SAFE_NAME_RE``
    (``^[a-zA-Z0-9][a-zA-Z0-9_.\\-]{0,127}$``). The transform is:

      1. Drop a single leading ``@`` (npm-style scope marker).
      2. Replace every run of unsafe characters with a single ``-``.
      3. Strip leading non-alnum characters — ``SAFE_NAME_RE`` requires
         the first byte to be alnum.
      4. Strip trailing ``-``.
      5. Truncate to 128 characters.

    Idempotent: ``sanitize_slug(sanitize_slug(x)) == sanitize_slug(x)``
    for any ``x`` that produces a valid slug.
    """
    candidate = name.removeprefix("@")
    candidate = _UNSAFE_CHARS.sub("-", candidate)
    candidate = _LEADING_NON_ALNUM.sub("", candidate)
    candidate = candidate.rstrip("-")
    candidate = candidate[:_SAFE_NAME_MAX_LEN]
    if not candidate:
        raise TankFetchError(f"cannot derive a safe slug from {name!r}")
    return candidate


def fetch_from_tank(
    ref: str,
    *,
    registry_url: str | None = None,
    token: str | None = None,
    work_dir: str | Path | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> tuple[Path, dict]:
    """Download a skill from Tank and return a local SKILL.md path.

    Args:
        ref: Tank reference like ``@tank/nextjs`` or ``@tank/nextjs@1.2.0``.
            When version is omitted, Tank's latest is used.
        registry_url: Override the registry (defaults to the value in
            ``~/.tank/config.json`` or ``https://www.tankpkg.dev``).
        token: Override the auth token (defaults to ``~/.tank/config.json``
            or the ``TANK_TOKEN`` env var).
        work_dir: Directory to write the SKILL.md into. If omitted, a
            private temp directory is created via ``tempfile.mkdtemp``;
            the caller is responsible for cleanup (``add_skill`` copies
            the file into ``skills_dir``, so leaking the temp file is
            cheap).
        client_factory: Test hook. A zero-arg callable that returns a
            ``TankClient``-compatible context manager. Production callers
            never pass this.

    Returns:
        A tuple ``(skill_md_path, metadata)``. The metadata dict carries
        provenance from Tank that ``skill_add`` merges into the wiki
        frontmatter::

            {
                "source": "tank",
                "tank_name": "@tank/nextjs",
                "tank_version": "1.2.0",
                "tank_integrity": "sha512-...",
                "tank_scan_verdict": "pass" | "fail" | "pending" | None,
                "tank_audit_score": 9.8,
                "tank_published_at": "2026-03-12T08:22:41Z",
                "tank_slug": "tank-nextjs",
            }

    Raises:
        TankFetchError: invalid reference or missing SKILL.md.
        tankpkg.TankNotFoundError: skill or version does not exist.
        tankpkg.TankAuthError: private skill, no valid token.
        tankpkg.TankNetworkError: transport failure after retries.
        tankpkg.TankIntegrityError: SHA-512 mismatch on download.
    """
    parsed = parse_ref(ref)
    slug = sanitize_slug(parsed.name)

    client_cm = _build_client(registry_url=registry_url, token=token, factory=client_factory)

    with client_cm as client:
        skill = client.read_skill(parsed.name, parsed.version)
        detail = client.version_detail(parsed.name, skill.version)

    if not skill.content:
        raise TankFetchError(f"Tank returned empty SKILL.md for {parsed.name}@{skill.version}")

    dest_dir = _resolve_work_dir(work_dir)
    skill_md_path = dest_dir / "SKILL.md"
    skill_md_path.write_text(skill.content, encoding="utf-8")

    metadata = {
        "source": "tank",
        "tank_name": parsed.name,
        "tank_version": skill.version,
        "tank_integrity": detail.integrity or None,
        "tank_scan_verdict": detail.scan_verdict,
        "tank_audit_score": detail.audit_score,
        "tank_audit_status": detail.audit_status or None,
        "tank_published_at": detail.published_at or None,
        "tank_slug": slug,
        "_tank_cleanup_dir": str(dest_dir) if work_dir is None else None,
    }
    return skill_md_path, metadata


def _build_client(
    *,
    registry_url: str | None,
    token: str | None,
    factory: Callable[[], Any] | None,
) -> Any:
    if factory is not None:
        return factory()
    try:
        from tankpkg import TankClient
    except ImportError as exc:
        raise TankFetchError("tank-sdk is not installed. Run: pip install tank-sdk") from exc
    return TankClient(registry_url=registry_url, token=token)


def _resolve_work_dir(work_dir: str | Path | None) -> Path:
    if work_dir is None:
        return Path(tempfile.mkdtemp(prefix="ctx-tank-"))
    path = Path(work_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path
