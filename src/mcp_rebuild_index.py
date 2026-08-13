#!/usr/bin/env python3
"""
mcp_rebuild_index.py -- Rebuild the canonical-key sidecar index for MCP entities.

Usage
-----
    python -m mcp_rebuild_index [--wiki PATH] [--dry-run]

Reads MCP entity markdown from either:

- ``<wiki>/wiki-packs`` when modular wiki packs are active, or
- ``<wiki>/entities/mcp-servers/`` for an extracted/editable wiki tree.

It writes ``<wiki>/entities/mcp-servers/.canonical-index.json`` with a fresh
``github_url -> {slug, relpath}`` map. The sidecar is a cache; the merged wiki
page set remains authoritative.

Exit codes: 0 on success, 2 on missing wiki path, 1 on unexpected error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ctx_config import cfg
from mcp_canonical_index import rebuild_from_scan

_MCP_ENTITY_SUBDIR = "entities/mcp-servers"


def main() -> None:
    """Entry point for ``python -m mcp_rebuild_index``."""
    parser = argparse.ArgumentParser(
        prog="python -m mcp_rebuild_index",
        description=(
            "Rebuild the canonical-key sidecar index from existing MCP entity "
            "pages. Idempotent; safe to run repeatedly."
        ),
    )
    parser.add_argument(
        "--wiki",
        default=str(cfg.wiki_dir),
        help="Wiki root path (default: config wiki_dir)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report counts without writing the sidecar file",
    )
    args = parser.parse_args()

    wiki_path = Path(os.path.expanduser(args.wiki))
    mcp_dir = wiki_path / _MCP_ENTITY_SUBDIR
    packs_dir = wiki_path / "wiki-packs"

    if not mcp_dir.is_dir() and not packs_dir.is_dir():
        print(
            f"Error: MCP entity directory or wiki-packs do not exist under: {wiki_path}",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        _, indexed, skipped = rebuild_from_scan(mcp_dir, persist=not args.dry_run)
    except Exception as exc:  # noqa: BLE001 - surface any failure to operator.
        print(f"Error: rebuild failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(
            f"[dry-run] would index {indexed} entities, "
            f"skip {skipped} (no github_url or unreadable)."
        )
    else:
        print(
            f"Canonical index rebuilt: {indexed} entities indexed, "
            f"{skipped} skipped (no github_url)."
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
