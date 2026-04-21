#!/usr/bin/env python3
"""
mcp_rebuild_index.py -- Rebuild the canonical-key sidecar index from disk.

Usage
-----
    ctx-mcp-rebuild-index [--wiki PATH] [--dry-run]

Reads every ``*.md`` under ``<wiki>/entities/mcp-servers/``, parses its
YAML frontmatter, and writes
``<wiki>/entities/mcp-servers/.canonical-index.json`` with a fresh
``github_url -> {slug, relpath}`` map.

Intended to be run:

- Once, to backfill the sidecar for the entities that existed before
  Phase 6b (the ``add_mcp`` hot-path upsert only covers records added
  after the feature landed).
- Any time the index is suspected stale (manual edits, restored from
  backup, cross-wiki merge). The normal scan-and-repair fallback in
  ``_find_existing_by_github_url`` handles one-off drift, but a full
  rebuild is cheap (~1 s at 15k entities) and gives a clean baseline.

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
    """Entry point for ``ctx-mcp-rebuild-index``."""
    parser = argparse.ArgumentParser(
        prog="ctx-mcp-rebuild-index",
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

    if not mcp_dir.is_dir():
        print(
            f"Error: MCP entity directory does not exist: {mcp_dir}",
            file=sys.stderr,
        )
        sys.exit(2)

    if args.dry_run:
        # Dry-run uses the same traversal but discards the write. Easiest
        # way is to call the real rebuild, then overwrite the file back
        # — but that's still a write. Instead, walk inline and count.
        indexed = 0
        skipped = 0
        for page in mcp_dir.rglob("*.md"):
            if page.name.startswith("."):
                skipped += 1
                continue
            # Lazy import to match the module pattern.
            from mcp_add import _normalize_github_url, _parse_frontmatter  # noqa: PLC0415
            try:
                text = page.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped += 1
                continue
            fm = _parse_frontmatter(text)
            if _normalize_github_url(fm.get("github_url")) is None:
                skipped += 1
            else:
                indexed += 1
        print(
            f"[dry-run] would index {indexed} entities, "
            f"skip {skipped} (no github_url or unreadable)."
        )
        sys.exit(0)

    try:
        _, indexed, skipped = rebuild_from_scan(mcp_dir)
    except Exception as exc:  # noqa: BLE001 — surface any failure to operator
        print(f"Error: rebuild failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Canonical index rebuilt: {indexed} entities indexed, "
        f"{skipped} skipped (no github_url)."
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
