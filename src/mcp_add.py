#!/usr/bin/env python3
"""
mcp_add.py -- Add MCP server records to the wiki catalog with intake gate.

Records are catalog-only (no local install). Sources are merged idempotently
so the same record from two different harvesters yields one page with both
sources listed.

Usage
-----
    # Single record from JSON file
    ctx-mcp-add --from-json /path/to/record.json

    # JSONL from file (one JSON object per line)
    ctx-mcp-add --from-jsonl /path/to/records.jsonl

    # JSONL from stdin
    ctx-mcp-add --from-stdin

    [--dry-run] [--wiki PATH] [--skip-existing]
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ctx_config import cfg
from intake_pipeline import IntakeRejected, check_intake, record_embedding
from mcp_entity import McpRecord
from wiki_batch_entities import generate_mcp_page
from wiki_sync import append_log, ensure_wiki, update_index
from wiki_utils import validate_skill_name

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Relative root for MCP server entity pages inside the wiki
_MCP_ENTITY_SUBDIR = "entities/mcp-servers"


def _build_corpus_text(record: McpRecord) -> str:
    """Build a SKILL.md-shaped corpus blob for the intake gate + embedding.

    The intake gate's structural check expects YAML frontmatter with
    ``name`` + ``description`` plus an H1 and H2 in the body — the
    same shape skills and agents present. We synthesize a minimal
    page from the McpRecord so the same gate that polices skills and
    agents also polices MCPs without the gate needing per-type logic.

    The synthesized text is used only for intake (gate + embedding
    cache); the actual entity page that lands on disk is the richer
    output of ``generate_mcp_page``.
    """
    tags_line = " ".join(record.tags) if record.tags else "none"
    transports_line = " ".join(record.transports) if record.transports else "unknown"
    sources_line = " ".join(record.sources) if record.sources else "none"

    return (
        "---\n"
        f"name: {record.slug}\n"
        f"description: {record.description}\n"
        "---\n\n"
        f"# {record.name}\n\n"
        "## Overview\n\n"
        f"{record.description}\n\n"
        "## Tags\n\n"
        f"{tags_line}\n\n"
        "## Transports\n\n"
        f"{transports_line}\n\n"
        "## Sources\n\n"
        f"{sources_line}\n"
    )


def _merge_sources(existing_fm: dict[str, Any], new_sources: tuple[str, ...]) -> list[str]:
    """Return a sorted, deduplicated union of existing and new sources."""
    existing: list[str] = existing_fm.get("sources", []) or []
    merged = sorted(set(existing) | set(new_sources))
    return merged


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Extract YAML frontmatter from a markdown page. Returns {} on failure."""
    match = re.match(r"^---\n(.*?\n)---\n", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
        return data if isinstance(data, dict) else {}
    except yaml.YAMLError:
        return {}


def _keep_longer_description(existing_fm: dict[str, Any], record: McpRecord) -> str:
    """Return whichever description is longer — existing page's or the record's."""
    existing_desc: str = existing_fm.get("description", "") or ""
    return existing_desc if len(existing_desc) >= len(record.description) else record.description


def _rewrite_frontmatter(page_text: str, new_fm: dict[str, Any]) -> str:
    """Replace the YAML frontmatter block in *page_text* with *new_fm*."""
    body_match = re.match(r"^---\n.*?\n---\n(.*)", page_text, re.DOTALL)
    body = body_match.group(1) if body_match else page_text
    fm_str = yaml.safe_dump(new_fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{fm_str}---\n{body}"


def add_mcp(
    *,
    record: McpRecord,
    wiki_path: Path,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Add (or merge sources for) one MCP record into the wiki catalog.

    Flow:
      1. Validate slug.
      2. Run intake gate.
      3. Compute target path: wiki/entities/mcp-servers/<first-letter>/<slug>.md
      4. If target exists: read frontmatter, merge sources (dedup + sort),
         keep longer description, rewrite page. is_new_page = False.
         Otherwise: generate fresh page via generate_mcp_page(). is_new_page = True.
      5. record_embedding (non-fatal).
      6. update_index + append_log (only when is_new_page).

    Args:
        record: Populated McpRecord dataclass instance.
        wiki_path: Absolute path to the wiki root directory.
        dry_run: Compute everything but skip writes and embeddings.

    Returns:
        dict with keys: slug, is_new_page, merged_sources, path
    """
    validate_skill_name(record.slug)

    corpus_text = _build_corpus_text(record)

    decision = check_intake(corpus_text, "mcp-servers")
    if not decision.allow:
        raise IntakeRejected(decision)

    entity_rel = record.entity_relpath()  # e.g. "f/fetch-mcp.md"
    target_path = wiki_path / _MCP_ENTITY_SUBDIR / entity_rel
    target_path.parent.mkdir(parents=True, exist_ok=True)

    is_new_page: bool
    merged_sources: list[str]

    # Phase 1 of branching: compute the read-side state. No serialization
    # work happens here so dry-run cannot fail on a malformed existing
    # page — that's deferred to the write-gate below.
    if target_path.exists():
        is_new_page = False
        existing_text = target_path.read_text(encoding="utf-8")
        existing_fm = _parse_frontmatter(existing_text)
        merged_sources = _merge_sources(existing_fm, record.sources)
        kept_description = _keep_longer_description(existing_fm, record)
    else:
        is_new_page = True
        existing_text = ""
        existing_fm = {}
        merged_sources = sorted(record.sources)
        kept_description = record.description

    if not dry_run:
        # Phase 2 of branching: render and write. Any YAML serialization
        # failure now is a real error, not a dry-run side-effect.
        if is_new_page:
            final_text = generate_mcp_page(record)
        else:
            updated_fm = {
                **existing_fm,
                "sources": merged_sources,
                "description": kept_description,
                "updated": TODAY,
            }
            final_text = _rewrite_frontmatter(existing_text, updated_fm)

        target_path.write_text(final_text, encoding="utf-8")

        try:
            record_embedding(
                subject_id=record.slug,
                raw_md=corpus_text,
                subject_type="mcp-servers",
            )
        except Exception as exc:  # noqa: BLE001 — cache failure must not break catalog
            print(
                f"Warning: failed to record intake embedding for {record.slug}: {exc}",
                file=sys.stderr,
            )

        # Index + log only on first creation. Source-merge re-harvests
        # would otherwise produce one log line per existing record per
        # batch run, blowing up log.md at scale.
        if is_new_page:
            update_index(str(wiki_path), [record.slug], subject_type="mcp-servers")

            log_details = [
                f"Slug: {record.slug}",
                f"Path: {target_path}",
                f"Sources: {', '.join(merged_sources) if merged_sources else 'none'}",
                f"Tags: {', '.join(record.tags) if record.tags else 'none'}",
                f"Transports: {', '.join(record.transports) if record.transports else 'unknown'}",
            ]
            warnings = decision.warnings
            if warnings:
                log_details.append(
                    "Warnings: " + "; ".join(f"{w.code}:{w.message}" for w in warnings)
                )
            append_log(str(wiki_path), "add-mcp", record.slug, log_details)

    return {
        "slug": record.slug,
        "is_new_page": is_new_page,
        "merged_sources": merged_sources,
        "path": str(target_path),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def _process_batch(
    records: list[dict[str, Any]],
    wiki_path: Path,
    dry_run: bool,
    skip_existing: bool,
    mcp_entity_dir: Path,
) -> tuple[int, int, int, int]:
    """Process a batch of raw dicts. Returns (added, merged, rejected, errors)."""
    added = merged = rejected = errors = 0
    total = len(records)

    for i, raw in enumerate(records, 1):
        slug = raw.get("slug", "<unknown>")
        try:
            record = McpRecord.from_dict(raw)
        except Exception as exc:  # noqa: BLE001 — batch CLI must not crash on one bad record
            errors += 1
            print(f"  [{i}/{total}] ERROR: {slug}: {exc}", file=sys.stderr)
            continue

        entity_rel = record.entity_relpath()
        target_path = mcp_entity_dir / entity_rel

        if skip_existing and target_path.exists():
            merged += 1
            print(f"  [{i}/{total}] [skipped] {record.slug}")
            continue

        try:
            result = add_mcp(record=record, wiki_path=wiki_path, dry_run=dry_run)
            if result["is_new_page"]:
                added += 1
                status = "added"
            else:
                merged += 1
                status = "merged"
            print(f"  [{i}/{total}] [{status}] {record.slug}")
        except IntakeRejected as exc:
            rejected += 1
            codes = ", ".join(f.code for f in exc.decision.failures) or "unknown"
            print(f"  [{i}/{total}] [rejected] {record.slug}: {codes}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — batch CLI must continue past one failure
            errors += 1
            print(f"  [{i}/{total}] ERROR: {record.slug}: {exc}", file=sys.stderr)

    return added, merged, rejected, errors


def main() -> None:
    """Entry point for the ctx-mcp-add CLI."""
    parser = argparse.ArgumentParser(
        description="Add MCP server records to the wiki catalog"
    )

    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--from-json",
        metavar="PATH",
        help="Single record as a JSON object file",
    )
    source_group.add_argument(
        "--from-jsonl",
        metavar="PATH",
        help="Batch of records as a JSONL file (one object per line)",
    )
    source_group.add_argument(
        "--from-stdin",
        action="store_true",
        help="Read JSONL records from stdin (one object per line)",
    )

    parser.add_argument("--dry-run", action="store_true", help="Parse and validate but skip writes")
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki root path")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip records whose entity page already exists (no source merge)",
    )
    args = parser.parse_args()

    wiki_path = Path(os.path.expanduser(args.wiki))
    ensure_wiki(str(wiki_path))
    mcp_entity_dir = wiki_path / _MCP_ENTITY_SUBDIR

    raw_records: list[dict[str, Any]] = []

    if args.from_json:
        json_path = Path(os.path.expanduser(args.from_json))
        if not json_path.exists():
            print(f"Error: {json_path} does not exist.", file=sys.stderr)
            sys.exit(1)
        try:
            raw_records = [json.loads(json_path.read_text(encoding="utf-8"))]
        except json.JSONDecodeError as exc:
            print(f"Error: failed to parse JSON: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.from_jsonl:
        jsonl_path = Path(os.path.expanduser(args.from_jsonl))
        if not jsonl_path.exists():
            print(f"Error: {jsonl_path} does not exist.", file=sys.stderr)
            sys.exit(1)
        for lineno, line in enumerate(
            jsonl_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = line.strip()
            if not line:
                continue
            try:
                raw_records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"Warning: line {lineno} skipped (bad JSON): {exc}", file=sys.stderr)

    elif args.from_stdin:
        for lineno, line in enumerate(sys.stdin, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw_records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"Warning: line {lineno} skipped (bad JSON): {exc}", file=sys.stderr)

    if not raw_records:
        print("No records to process.", file=sys.stderr)
        sys.exit(0)

    added, merged, rejected, errors = _process_batch(
        records=raw_records,
        wiki_path=wiki_path,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        mcp_entity_dir=mcp_entity_dir,
    )

    dry_label = " (dry-run)" if args.dry_run else ""
    print(
        f"\nDone{dry_label}: {added} added, {merged} merged, "
        f"{rejected} rejected, {errors} errors"
    )


if __name__ == "__main__":
    main()
