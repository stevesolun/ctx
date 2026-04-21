#!/usr/bin/env python3
"""
mcp_fetch.py -- Dispatcher for MCP catalog sources.

Emits one JSON record per line on stdout so the output pipes directly into
``ctx-mcp-add --from-stdin``.  The dispatcher knows nothing about specific
catalog shapes; it resolves a source by name from
:data:`mcp_sources.SOURCES`, calls its ``fetch`` method, and serialises
each yielded dict as JSONL.

Usage
-----
    ctx-mcp-fetch --source awesome-mcp [--limit N] [--refresh]
    ctx-mcp-fetch --source all [--limit N]
    ctx-mcp-fetch --list-sources

Downstream pipe
---------------
    ctx-mcp-fetch --source awesome-mcp --limit 5 | ctx-mcp-add --from-stdin
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterator

from mcp_sources import SOURCES


def _emit(records: Iterator[dict]) -> int:
    """Write *records* as JSONL to stdout.  Return the count emitted."""
    count = 0
    for rec in records:
        # ``separators`` kills the default ", " / ": " whitespace so each
        # line is compact; JSONL consumers treat each line as a standalone
        # document, so trailing whitespace is wasted bytes at batch scale.
        sys.stdout.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
        count += 1
    sys.stdout.flush()
    return count


def _run_one(
    source_name: str, *, limit: int | None, refresh: bool
) -> tuple[int, int]:
    """Fetch from a single named source.  Return ``(emitted, errors)``."""
    try:
        source = SOURCES[source_name]
    except KeyError:
        print(
            f"Error: unknown source {source_name!r}. "
            f"Known: {', '.join(sorted(SOURCES)) or '(none registered)'}",
            file=sys.stderr,
        )
        return 0, 1

    try:
        emitted = _emit(source.fetch(limit=limit, refresh=refresh))
    except Exception as exc:  # noqa: BLE001 — dispatcher must not leak tracebacks to pipes
        print(f"Error: source {source_name!r} failed: {exc}", file=sys.stderr)
        return 0, 1

    print(
        f"[{source_name}] emitted {emitted} record(s)",
        file=sys.stderr,
    )
    return emitted, 0


def _run_all(*, limit: int | None) -> tuple[int, int]:
    """Fetch from every registered source, summing emissions and errors.

    ``--limit`` applies *per source*.  Applying a single global cap would
    bias the output toward whichever source is iterated first, which
    defeats the point of listing them side-by-side.
    """
    total_emitted = 0
    total_errors = 0
    for name in sorted(SOURCES):
        emitted, errors = _run_one(name, limit=limit, refresh=False)
        total_emitted += emitted
        total_errors += errors
    return total_emitted, total_errors


def _list_sources() -> int:
    """Print each registered source and its homepage to stdout.  Return 0."""
    if not SOURCES:
        print("(no sources registered)")
        return 0
    for name in sorted(SOURCES):
        src = SOURCES[name]
        print(f"{name}\t{src.homepage}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-mcp-fetch",
        description=(
            "Fetch MCP server records from a registered catalog source "
            "and emit them as JSONL on stdout."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--source",
        metavar="NAME",
        help="Source name (e.g. 'awesome-mcp') or 'all' for every registered source",
    )
    group.add_argument(
        "--list-sources",
        action="store_true",
        help="List registered sources with their homepages and exit",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of records yielded per source (default: no cap)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass the local raw cache and fetch fresh upstream content",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help=(
            "Enable progress logging to stderr. -v for INFO (parse counts, "
            "page progress), -vv for DEBUG (per-entry skip reasons). Library "
            "modules emit via ``logging`` by default but are silent unless "
            "this flag wires up basicConfig."
        ),
    )
    return parser


def _configure_logging(verbosity: int) -> None:
    """Wire logging.basicConfig for CLI visibility.

    Phase 2.5 replaced print(stderr) in library code with logging calls
    which are silent by default. This helper lights them up on demand.
    stderr (not stdout) so JSONL pipe consumers stay clean.
    """
    if verbosity <= 0:
        return
    import logging  # noqa: PLC0415 — local import keeps cold-path cost off imports
    level = logging.DEBUG if verbosity >= 2 else logging.INFO
    logging.basicConfig(
        level=level,
        format="[%(name)s] %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    """Entry point for the ``ctx-mcp-fetch`` console script."""
    parser = _build_parser()
    args = parser.parse_args()
    _configure_logging(args.verbose)

    if args.list_sources:
        sys.exit(_list_sources())

    if args.limit is not None and args.limit <= 0:
        print("Error: --limit must be a positive integer.", file=sys.stderr)
        sys.exit(2)

    if args.source == "all":
        if args.refresh:
            # --refresh on 'all' would silently re-fetch every source; that is
            # almost never what the operator intends, so we refuse it rather
            # than surprise them with a long network burst.
            print(
                "Error: --refresh is not supported with --source all; "
                "refresh one source at a time.",
                file=sys.stderr,
            )
            sys.exit(2)
        _, errors = _run_all(limit=args.limit)
    else:
        _, errors = _run_one(args.source, limit=args.limit, refresh=args.refresh)

    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
