#!/usr/bin/env python3
"""
mcp_enrich.py -- Phase 6f detail-page enrichment for MCP entities.

Walks every MCP entity file (``<wiki>/entities/mcp-servers/<shard>/<slug>.md``)
and calls the source's ``fetch_details(slug)`` to pull ``github_url``
and ``stars`` from the detail page. The scraped values are written
back into the entity's YAML frontmatter.

Why this exists (from Phase 6e close-out):
  - The listing pages scraped by Phase 6c (``mcp_ingest``) expose
    slug / name / description / classification but NOT a repo URL.
  - Without ``github_url``, the quality scorer's popularity and
    freshness signals stay neutral for every MCP, flattening the
    A/B/C/D distribution to 100% B+C.
  - ``ctx-mcp-install`` needs a URL to pass to ``claude mcp add``.
  - The knowledge graph has no way to link pulsemcp entries to the
    same repo surfaced elsewhere (awesome-mcp, mcp-get, ...).

Checkpoint: ``<wiki>/.enrich-checkpoint/<source>.json`` — same shape
as the Phase 6c ingest checkpoint. Slugs in ``processed`` skip on
resume. ``failures`` retry on the next run unless ``--skip-failures``.
All fetches go through the existing SSRF-hardened ``fetch_text`` in
``mcp_sources/base.py`` with the pulsemcp-level date-keyed cache, so
a second run over the same day is near-instant (cache hit) and a
re-run tomorrow refreshes everything naturally.

Usage:
    ctx-mcp-enrich --source pulsemcp                  # all entities
    ctx-mcp-enrich --source pulsemcp --limit 50       # first 50 only
    ctx-mcp-enrich --source pulsemcp --slug foo-bar   # single entity
    ctx-mcp-enrich --source pulsemcp --status         # checkpoint report
    ctx-mcp-enrich --source pulsemcp --reset          # delete checkpoint
    ctx-mcp-enrich --source pulsemcp --dry-run        # fetch but don't write
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ctx.utils._fs_utils import atomic_write_json, atomic_write_text
from ctx_config import cfg
from mcp_sources import SOURCES

_logger = logging.getLogger(__name__)

CHECKPOINT_SUBDIR = ".enrich-checkpoint"
CHECKPOINT_VERSION = 1
DEFAULT_FLUSH_EVERY = 10
DEFAULT_SLEEP_SECONDS = 0.5  # polite pacing between live fetches

_MCP_ENTITY_SUBDIR = Path("entities") / "mcp-servers"

# Line-break codepoints that Python's str.splitlines() treats as
# boundaries. The renderer neutralises all five so that a quoted
# scalar cannot be split into new frontmatter lines by the project's
# splitlines-based parser (_parse_entity_frontmatter). Strix
# vuln-0001 HIGH. \x85 is NEL,   is LINE SEPARATOR,   is
# PARAGRAPH SEPARATOR — written with \u escapes so editor autonorm
# cannot silently convert them to plain space (U+0020).
_LINE_SEP_TRANSLATE = str.maketrans({
    "\r": " ", "\n": " ",
    "\x85": " ", "\u2028": " ", "\u2029": " ",
})


# ── Graceful exit ────────────────────────────────────────────────────────────


class _GracefulExit:
    """Mirror of mcp_ingest._GracefulExit — see that module for the
    rationale (cooperative signal handling between records)."""

    def __init__(self) -> None:
        self.requested = False
        self._prev_int = signal.getsignal(signal.SIGINT)
        self._prev_term = signal.getsignal(signal.SIGTERM)

    def install(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        try:
            signal.signal(signal.SIGTERM, self._handle)
        except (ValueError, OSError):
            pass

    def uninstall(self) -> None:
        signal.signal(signal.SIGINT, self._prev_int)
        try:
            signal.signal(signal.SIGTERM, self._prev_term)
        except (ValueError, OSError):
            pass

    def _handle(self, signum: int, frame: object) -> None:  # noqa: ARG002
        self.requested = True


# ── Checkpoint ───────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _checkpoint_path(wiki_path: Path, source: str) -> Path:
    # Delegate to shared validator. Prior impl only rejected ``/``, ``\\``
    # and leading ``.``, which let Windows drive-relative names like
    # ``C:evil`` through — those resolve against drive C's CWD, not the
    # wiki, which an attacker could use to overwrite ~/.claude/settings.json
    # via ``--source "C:...\\settings"``. Security-auditor H-3, fixed here.
    from ctx.utils._safe_name import validate_source_name  # noqa: PLC0415
    validate_source_name(source, field="source")
    return wiki_path / CHECKPOINT_SUBDIR / f"{source}.json"


def _empty_checkpoint(source: str) -> dict:
    now = _now_iso()
    return {
        "version": CHECKPOINT_VERSION,
        "source": source,
        "started_at": now,
        "updated_at": now,
        "total_seen": 0,
        "processed": {},
        "failures": {},
    }


def load_checkpoint(wiki_path: Path, source: str) -> dict:
    """Tolerant load — any error resets to a fresh checkpoint.

    The authoritative state lives in the entity frontmatter itself;
    a lost or corrupt checkpoint just means the next run re-fetches
    the detail pages (which hit the cache, so cheap).
    """
    path = _checkpoint_path(wiki_path, source)
    if not path.is_file():
        return _empty_checkpoint(source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_checkpoint(source)
    if not isinstance(data, dict) or data.get("version") != CHECKPOINT_VERSION:
        return _empty_checkpoint(source)
    if data.get("source") != source:
        return _empty_checkpoint(source)
    processed = data.get("processed") or {}
    failures = data.get("failures") or {}
    if not isinstance(processed, dict) or not isinstance(failures, dict):
        return _empty_checkpoint(source)
    return {
        "version": CHECKPOINT_VERSION,
        "source": source,
        "started_at": str(data.get("started_at") or _now_iso()),
        "updated_at": str(data.get("updated_at") or _now_iso()),
        "total_seen": int(data.get("total_seen") or 0),
        "processed": processed,
        "failures": failures,
    }


def save_checkpoint(wiki_path: Path, checkpoint: dict) -> None:
    checkpoint["updated_at"] = _now_iso()
    atomic_write_json(_checkpoint_path(wiki_path, checkpoint["source"]), checkpoint)


# ── Entity discovery ─────────────────────────────────────────────────────────


def _iter_entities(wiki_path: Path) -> Iterable[Path]:
    """Yield every ``<wiki>/entities/mcp-servers/<shard>/<slug>.md`` file.

    Sort globally for determinism — otherwise a resume after a crash
    at entity #5,000 might skip ahead or rewind depending on platform
    shard-iteration order.
    """
    root = wiki_path / _MCP_ENTITY_SUBDIR
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.md"))


def _extract_slug_from_path(path: Path) -> str:
    """Entity files are named ``<wiki_slug>.md`` — stem is the WIKI slug.

    Note this is not necessarily the slug used by the upstream source
    (e.g. pulsemcp): during ingest, non-ASCII names can cause the
    wiki slug to get mangled (``ignfab-geoportail`` → ``g-oportail``)
    while the authoritative ``homepage_url`` preserves the real URL.
    Use ``_source_slug_from_entity`` when you need the slug to pass
    to ``source.fetch_details``.
    """
    return path.stem


# Map {source_name: regex that pulls the upstream slug out of a
# homepage_url}. Each regex has one named group ``slug``.
_SOURCE_SLUG_PATTERNS: dict[str, re.Pattern[str]] = {
    "pulsemcp": re.compile(
        r"^https?://(?:www\.)?pulsemcp\.com/servers/(?P<slug>[^/?#\s]+)"
    ),
}


def _source_slug_from_entity(
    entity_path: Path, source_name: str
) -> str | None:
    """Pull the upstream slug out of the entity's frontmatter.

    For pulsemcp, parses ``homepage_url``:
    ``https://www.pulsemcp.com/servers/<slug>`` → ``<slug>``.

    Returns ``None`` when the entity has no homepage_url, the URL
    doesn't match the expected shape, or the source isn't registered
    in ``_SOURCE_SLUG_PATTERNS``. The caller treats None as "skip
    with a clear failure message" rather than as an error — a
    partner like glama/mcp-get won't have pulsemcp URLs.
    """
    pattern = _SOURCE_SLUG_PATTERNS.get(source_name)
    if pattern is None:
        return None
    try:
        text = entity_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match is None:
        return None
    url_match = re.search(
        r"^homepage_url:[ \t]*(?P<url>.+)$",
        fm_match.group(1),
        flags=re.MULTILINE,
    )
    if url_match is None:
        return None
    url = url_match.group("url").strip().strip('"').strip("'")
    slug_match = pattern.match(url)
    return slug_match.group("slug") if slug_match else None


# ── Frontmatter update ───────────────────────────────────────────────────────


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _set_frontmatter_field(text: str, field: str, value: Any) -> str:
    """Replace or insert ``field: value`` in the YAML frontmatter.

    Lossy for complex YAML (lists, mappings) but we only touch simple
    scalar fields (``github_url``, ``stars``, ``updated``), and the
    frontmatter on these entity files is flat. Uses line-anchored
    regex so a ``github_url: null`` in the middle doesn't match a
    hypothetical ``sub_github_url`` key on a following line.
    """
    escaped = re.escape(field)
    rendered = _render_scalar(value)

    # Try to replace an existing key.
    pattern = rf"^{escaped}:[ \t]*.*$"
    repl = f"{field}: {rendered}"
    new_text, n = re.subn(pattern, repl, text, count=1, flags=re.MULTILINE)
    if n:
        return new_text

    # Key didn't exist — insert after the opening delimiter. We only
    # do this inside the frontmatter block to avoid polluting bodies.
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match is None:
        return text  # no frontmatter at all; skip rather than fabricate
    insert_at = fm_match.end(1)
    return text[:insert_at] + f"\n{field}: {rendered}" + text[insert_at:]


def _render_scalar(value: Any) -> str:
    """YAML-scalar rendering for the handful of types we write.

    ``None`` → ``null`` (matches the existing frontmatter convention
    for unset fields). Strings are double-quoted when they could be
    misparsed as another YAML type (start with ``-``, contain ``:``);
    github URLs are alnum+``/.-_`` so they fall in the safe-bare
    range, but we quote anyway for consistency.

    SECURITY NOTE: strings are stripped of ``\\n`` / ``\\r`` before
    rendering to prevent frontmatter injection. Security-auditor H-1:
    a Source implementation that returned a multi-line URL value
    (e.g. scraped from a poisoned detail page) would otherwise inject
    fake YAML keys like ``status: installed`` or ``install_cmd: ...``
    which the next ``ctx-mcp-install --force`` would dutifully pick
    up from the now-poisoned frontmatter. The executable allowlist
    (commit b79be55) catches most of the blast radius today, but the
    injection vector must be shut at the writer too.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # Neutralise ASCII line breaks (\r\n) AND Unicode line separators
        # (U+0085 NEL, U+2028 LS, U+2029 PS). Python's str.splitlines()
        # treats all five as line boundaries, so downstream splitlines-
        # based parsers (mcp_install._parse_entity_frontmatter,
        # wiki_utils) would otherwise see a quoted scalar as multiple
        # frontmatter lines — Strix vuln-0001 HIGH (CWE-116). Mirrors
        # install_utils._render_scalar so the two stay aligned.
        sanitised = value.translate(_LINE_SEP_TRANSLATE)
        # Conservative: quote on the full YAML 1.1 reserved-indicator set,
        # leading block indicators, or leading/trailing whitespace. The
        # unquoted path is reserved for simple alphanumeric-style values
        # that YAML's plain-scalar scanner parses unambiguously. Mirrors
        # install_utils._render_scalar — the two must stay aligned.
        yaml_structural = set(",[]{}:?#&*!|>%@`=\"'\\")
        needs_quote = (
            any(ch in sanitised for ch in yaml_structural)
            or (
                sanitised
                and (
                    sanitised[0] == "-"
                    or sanitised[0].isspace()
                    or sanitised[-1].isspace()
                )
            )
            or sanitised.startswith(("?", "[", "{"))
        )
        if needs_quote:
            escaped = sanitised.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return sanitised
    # Defensive fallback — stringify and quote.
    return f'"{str(value)}"'


def apply_enrichment(
    entity_path: Path, enrichment: dict, *, dry_run: bool
) -> dict:
    """Write ``enrichment`` fields into the entity's frontmatter.

    Returns a diff dict ``{field: (old, new)}`` describing what
    changed, or an empty dict when nothing needed updating. Updates
    ``updated`` with today's ISO date only when at least one other
    field changed — a no-op run shouldn't bump the mtime.
    """
    if not enrichment:
        return {}

    text = entity_path.read_text(encoding="utf-8", errors="replace")
    fm_match = _FRONTMATTER_RE.match(text)
    if fm_match is None:
        return {}

    diff: dict[str, tuple[Any, Any]] = {}
    for field, new_val in enrichment.items():
        # Parse current value cheaply with a line grep rather than
        # pulling a yaml dependency for three scalar reads.
        pat = re.compile(rf"^{re.escape(field)}:[ \t]*(?P<val>.*)$", re.MULTILINE)
        cur_match = pat.search(fm_match.group(0))
        current: Any = None
        if cur_match is not None:
            raw = cur_match.group("val").strip()
            if raw in ("", "null", "~"):
                current = None
            elif raw.isdigit():
                current = int(raw)
            else:
                current = raw.strip('"').strip("'")
        if current != new_val:
            diff[field] = (current, new_val)
            text = _set_frontmatter_field(text, field, new_val)

    if diff and not dry_run:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        text = _set_frontmatter_field(text, "updated", today)
        atomic_write_text(entity_path, text)
    return diff


# ── Core loop ────────────────────────────────────────────────────────────────


def enrich_entities(
    entity_paths: list[Path],
    *,
    source_name: str,
    wiki_path: Path,
    checkpoint: dict,
    refresh: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    skip_failures: bool = False,
    flush_every: int = DEFAULT_FLUSH_EVERY,
    sleep_seconds: float = DEFAULT_SLEEP_SECONDS,
    graceful: _GracefulExit | None = None,
    report_progress: bool = True,
) -> dict:
    """Enrich each entity via ``source.fetch_details(slug)``.

    Returns the same checkpoint (mutated).
    """
    source = SOURCES.get(source_name)
    if source is None:
        raise ValueError(
            f"unknown source {source_name!r}; known: {sorted(SOURCES)}"
        )
    if not hasattr(source, "fetch_details"):
        raise NotImplementedError(
            f"source {source_name!r} does not implement fetch_details()"
        )

    processed = checkpoint["processed"]
    failures = checkpoint["failures"]

    attempted = enriched = unchanged = failed = skipped = 0
    for path in entity_paths:
        if limit is not None and attempted >= limit:
            break
        if graceful and graceful.requested:
            break

        wiki_slug = _extract_slug_from_path(path)
        if wiki_slug in processed:
            skipped += 1
            continue
        if wiki_slug in failures and skip_failures:
            skipped += 1
            continue

        attempted += 1
        checkpoint["total_seen"] += 1

        source_slug = _source_slug_from_entity(path, source_name)
        if source_slug is None:
            # Entity has no homepage_url for this source (e.g. ingested
            # from a different source). Record a skip so we don't
            # retry; it's not a failure.
            processed[wiki_slug] = {
                "result": "no-source-url",
                "at": _now_iso(),
                "fields": [],
            }
            if report_progress:
                print(
                    f"  [{attempted}] [no-source-url] {wiki_slug}",
                    flush=True,
                )
            if attempted % flush_every == 0:
                save_checkpoint(wiki_path, checkpoint)
            continue

        try:
            enrichment = source.fetch_details(source_slug, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 — batch must continue
            failed += 1
            failures[wiki_slug] = {
                "error": f"{type(exc).__name__}: {exc}",
                "at": _now_iso(),
                "source_slug": source_slug,
            }
            if report_progress:
                print(
                    f"  [{attempted}] [FAIL] {wiki_slug} "
                    f"(source={source_slug}): {type(exc).__name__}",
                    flush=True,
                )
            if attempted % flush_every == 0:
                save_checkpoint(wiki_path, checkpoint)
            continue

        try:
            diff = apply_enrichment(path, enrichment, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures[wiki_slug] = {
                "error": f"apply: {type(exc).__name__}: {exc}",
                "at": _now_iso(),
                "source_slug": source_slug,
            }
            if report_progress:
                print(
                    f"  [{attempted}] [APPLY-FAIL] {wiki_slug}: {exc}",
                    flush=True,
                )
            continue

        if diff:
            enriched += 1
            outcome = "enriched"
            failures.pop(wiki_slug, None)
        elif enrichment:
            unchanged += 1
            outcome = "unchanged"
        else:
            unchanged += 1
            outcome = "no-repo"

        processed[wiki_slug] = {
            "result": outcome,
            "at": _now_iso(),
            "fields": list(enrichment.keys()) if enrichment else [],
            "source_slug": source_slug,
        }

        if report_progress:
            fields = ",".join(enrichment.keys()) if enrichment else "none"
            print(
                f"  [{attempted}] [{outcome}] {wiki_slug} "
                f"(source={source_slug}, fields={fields})",
                flush=True,
            )

        if attempted % flush_every == 0:
            save_checkpoint(wiki_path, checkpoint)

        # Polite pacing — only between LIVE fetches, not cache hits.
        # We can't cheaply tell from here whether fetch_text hit cache,
        # so use a short default (0.5s) that's fine either way.
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    save_checkpoint(wiki_path, checkpoint)
    if report_progress:
        tail = " (interrupted)" if graceful and graceful.requested else ""
        print(
            f"\nEnrich summary{tail}: attempted={attempted}, enriched={enriched}, "
            f"unchanged/no-repo={unchanged}, failed={failed}, skipped_resume={skipped}",
            flush=True,
        )
    return checkpoint


# ── CLI ──────────────────────────────────────────────────────────────────────


def _print_status(wiki_path: Path, source: str) -> None:
    cp = load_checkpoint(wiki_path, source)
    print(f"source:       {cp['source']}")
    print(f"started_at:   {cp['started_at']}")
    print(f"updated_at:   {cp['updated_at']}")
    print(f"total_seen:   {cp['total_seen']}")
    print(f"processed:    {len(cp['processed'])}")
    print(f"failures:     {len(cp['failures'])}")
    # Breakdown by outcome.
    outcomes: dict[str, int] = {}
    for entry in cp["processed"].values():
        result = str(entry.get("result", "?"))
        outcomes[result] = outcomes.get(result, 0) + 1
    if outcomes:
        print("\nProcessed breakdown:")
        for k in sorted(outcomes):
            print(f"  {k}: {outcomes[k]}")
    if cp["failures"]:
        print("\nLast 5 failures:")
        for slug, info in list(cp["failures"].items())[-5:]:
            print(f"  {slug}: {info['error'][:120]}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-mcp-enrich",
        description=(
            "Enrich MCP entity frontmatter from per-source detail pages. "
            "Currently supports --source pulsemcp (github_url + stars)."
        ),
    )
    parser.add_argument("--source", required=True, help="Source name (e.g. pulsemcp)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--slug", help="Enrich one slug only (dry-run candidate)")
    group.add_argument(
        "--status", action="store_true",
        help="Print checkpoint summary and exit",
    )
    group.add_argument(
        "--reset", action="store_true",
        help="Delete the checkpoint for --source before starting",
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap the number of entities attempted this run")
    parser.add_argument("--refresh", action="store_true",
                        help="Bypass the raw detail-page cache")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch but do not write any frontmatter")
    parser.add_argument(
        "--skip-failures", action="store_true",
        help="Do not retry slugs already present in checkpoint.failures",
    )
    parser.add_argument(
        "--flush-every", type=int, default=DEFAULT_FLUSH_EVERY,
        help=f"Checkpoint flush cadence (default {DEFAULT_FLUSH_EVERY})",
    )
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP_SECONDS,
        help=f"Seconds between fetches (default {DEFAULT_SLEEP_SECONDS})",
    )
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki root")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-entity progress lines")
    return parser


def _force_utf8_stdio() -> None:
    """Mirror of mcp_fetch/mcp_ingest helper — Windows cp1252 crashes
    on CJK/emoji in pulsemcp descriptions, so force UTF-8 at entry."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main() -> None:
    _force_utf8_stdio()
    parser = _build_parser()
    args = parser.parse_args()

    wiki_path = Path(os.path.expanduser(args.wiki))

    if args.status:
        _print_status(wiki_path, args.source)
        sys.exit(0)

    if args.reset:
        try:
            _checkpoint_path(wiki_path, args.source).unlink()
            print(f"Reset checkpoint for {args.source!r}.", file=sys.stderr)
        except FileNotFoundError:
            pass

    checkpoint = load_checkpoint(wiki_path, args.source)

    if args.slug:
        # Single-slug path: bypass the discovery loop, build a one-file list.
        root = wiki_path / _MCP_ENTITY_SUBDIR
        # Shard lookup mirrors McpRecord.entity_relpath.
        shard = args.slug[0] if args.slug and args.slug[0].isalpha() else "0-9"
        entity_paths = [root / shard / f"{args.slug}.md"]
        if not entity_paths[0].is_file():
            print(
                f"Error: no entity at {entity_paths[0]} — has it been ingested?",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        entity_paths = list(_iter_entities(wiki_path))
        if not entity_paths:
            print("No MCP entities found — run ctx-mcp-ingest first.", file=sys.stderr)
            sys.exit(1)

    graceful = _GracefulExit()
    graceful.install()
    try:
        enrich_entities(
            entity_paths,
            source_name=args.source,
            wiki_path=wiki_path,
            checkpoint=checkpoint,
            refresh=args.refresh,
            dry_run=args.dry_run,
            limit=args.limit,
            skip_failures=args.skip_failures,
            flush_every=args.flush_every,
            sleep_seconds=args.sleep,
            graceful=graceful,
            report_progress=not args.quiet,
        )
    finally:
        graceful.uninstall()

    sys.exit(1 if checkpoint["failures"] else 0)


if __name__ == "__main__":
    main()
