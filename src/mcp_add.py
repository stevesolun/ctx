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

import yaml  # type: ignore[import-untyped]

from ctx.core.entity_update import build_update_review, render_update_review
from ctx_config import cfg
from intake_pipeline import IntakeRejected, check_intake, record_embedding
import mcp_canonical_index
from mcp_entity import McpRecord
from wiki_batch_entities import generate_mcp_page
from ctx.core.wiki.wiki_sync import append_log, ensure_wiki, update_index
from ctx.core.wiki.wiki_utils import validate_skill_name
from ctx.utils._fs_utils import reject_symlink_path, safe_atomic_write_text

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

    Frontmatter is rendered via ``yaml.safe_dump`` rather than f-string
    interpolation. Descriptions and slugs flow from untrusted README
    content in the awesome-mcp parser; a description containing
    ``\\n---\\n`` or ``\\nname: evil`` would otherwise corrupt the
    synthesized YAML before the intake gate sees it. Body sections are
    plain markdown with no parser-significant interpolation.
    """
    tags_line = " ".join(record.tags) if record.tags else "none"
    transports_line = " ".join(record.transports) if record.transports else "unknown"
    sources_line = " ".join(record.sources) if record.sources else "none"

    fm_yaml = yaml.safe_dump(
        {"name": record.slug, "description": record.description},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )

    return (
        "---\n"
        f"{fm_yaml}"
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


def _normalize_github_url(url: str | None) -> str | None:
    """Return the canonical lowercase GitHub URL, or None for non-GitHub inputs.

    Mirrors ``McpRecord.canonical_dedup_key`` so an existing-entity scan
    can match against new records on the same key. We strip trailing
    slashes and lowercase host+path because GitHub URLs are case-
    insensitive at the host but display case is preserved by the parser.
    """
    if not url:
        return None
    candidate = url.strip().rstrip("/").lower()
    if not candidate.startswith(("http://github.com/", "https://github.com/")):
        return None
    return candidate


def _scan_for_github_url(mcp_dir: Path, target: str) -> Path | None:
    """Walk every entity page looking for a canonical github_url match.

    O(n) fallback used when the canonical-index sidecar misses or
    returns a stale entry. ``target`` must already be normalized — this
    function does not re-normalize.
    """
    for page in mcp_dir.rglob("*.md"):
        # Skip sidecar files (``.canonical-index.json`` is not .md but
        # a future hidden .md would be caught here).
        if page.name.startswith("."):
            continue
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Fast path: grep the text for the URL before parsing YAML.
        if target not in text.lower():
            continue
        fm = _parse_frontmatter(text)
        if _normalize_github_url(fm.get("github_url")) == target:
            return page
    return None


def _find_existing_by_github_url(
    mcp_dir: Path, target_github_url: str | None
) -> Path | None:
    """Return the path of an existing entity page for ``target_github_url``.

    Phase 3.6 cross-source dedup: when awesome-mcp and pulsemcp both
    catalog the same upstream repo, their slugs differ, and the slug-
    based existence check in ``add_mcp`` would create two separate
    entities. This helper finds the existing entity by its canonical
    github_url so the second source can merge into it instead.

    Phase 6b: canonical-index sidecar turns the O(n) scan into O(1)
    lookup on the hot path. The index is a *cache*, not authoritative:

    - Hit + file exists  -> return the indexed path.
    - Hit + file missing -> stale entry; scan and repair (upsert the
      scan result, or remove the stale mapping if nothing matches).
    - Miss               -> scan once; on hit, repair by upserting.
    - No github_url      -> return None (indexing github-only URLs).
    """
    target = _normalize_github_url(target_github_url)
    if target is None or not mcp_dir.is_dir():
        return None

    index = mcp_canonical_index.load_index(mcp_dir)
    # Distinguish "true miss" (no entry) from "stale hit" (entry exists
    # but points at nothing/something else). lookup() collapses both to
    # None which hides the information we need for repair decisions.
    raw_entry = index["by_github_url"].get(target)
    cached = mcp_canonical_index.lookup(mcp_dir, target, index=index)
    if cached is not None:
        # Defensive confirm: the indexed file might have been manually
        # edited to point at a different repo. If the stored github_url
        # still matches the target, we trust it; otherwise fall through
        # to the scan path and treat this as a stale entry.
        try:
            fm = _parse_frontmatter(cached.read_text(encoding="utf-8", errors="replace"))
            if _normalize_github_url(fm.get("github_url")) == target:
                return cached
        except OSError:
            pass  # Scan-and-repair below handles this.

    # Miss or stale hit: fall back to the authoritative scan.
    hit = _scan_for_github_url(mcp_dir, target)
    if hit is not None:
        # Repair the index so the next call is O(1).
        try:
            relpath = hit.relative_to(mcp_dir).as_posix()
            slug = hit.stem
            mcp_canonical_index.upsert(
                mcp_dir, target, slug=slug, relpath=relpath, index=index
            )
        except (OSError, ValueError):
            # Index-repair failure must not block the dedup decision;
            # the next successful add will get another chance to repair.
            pass
        return hit

    # Nothing on disk anywhere. If the index had a stale entry, drop it
    # so future lookups don't keep paying the scan cost.
    if raw_entry is not None:
        try:
            mcp_canonical_index.remove(mcp_dir, target, index=index)
        except OSError:
            pass
    return None


def add_mcp(
    *,
    record: McpRecord,
    wiki_path: Path,
    dry_run: bool = False,
    review_existing: bool = False,
    update_existing: bool = False,
) -> dict[str, Any]:
    """Add (or merge sources for) one MCP record into the wiki catalog.

    Flow:
      1. Validate slug.
      2. Compute target path: wiki/entities/mcp-servers/<first-letter>/<slug>.md
      3. If target exists -> SKIP intake gate, run merge directly. The
         merge path is for known-good entities; running intake here would
         flag the re-fetched record as DUPLICATE against its own existing
         embedding (cosine 1.0 >= dup_threshold 0.93) and block source
         merging from ever happening. Existence is the source of truth.
      4. If target does NOT exist -> run intake gate, then write a new
         entity page via generate_mcp_page(). Intake's similarity check
         here is meaningful: it catches near-duplicates *between distinct
         slugs* in the same source (e.g. two records that differ only in
         creator-prefix capitalisation).
      5. record_embedding (non-fatal). Only on first creation; re-merge
         doesn't touch the cache because the existing vector is correct.
      6. update_index + append_log (only when is_new_page).

    Args:
        record: Populated McpRecord dataclass instance.
        wiki_path: Absolute path to the wiki root directory.
        dry_run: Compute everything but skip writes and embeddings.
        review_existing: Return an update review instead of mutating existing pages.
        update_existing: Apply an existing-page update after review.

    Returns:
        dict with keys: slug, is_new_page, merged_sources, path
    """
    validate_skill_name(record.slug)

    entity_rel = record.entity_relpath()  # e.g. "f/fetch-mcp.md"
    mcp_dir = wiki_path / _MCP_ENTITY_SUBDIR
    target_path = mcp_dir / entity_rel

    # Phase 3.6: cross-source dedup by canonical github_url before the
    # slug-based check. When awesome-mcp and pulsemcp both catalog the
    # same upstream repo, their slugs differ — we want to merge into
    # the existing entity, not create a second one at our slug path.
    # Only fires when the new record carries a github_url; pulsemcp
    # listing-page records currently have only homepage_url (Phase 6
    # detail-page enrichment will populate github_url so this dedup
    # path becomes meaningful for them too).
    canonical_match = _find_existing_by_github_url(mcp_dir, record.github_url)
    if canonical_match is not None and canonical_match != target_path:
        target_path = canonical_match

    reject_symlink_path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    is_new_page: bool
    merged_sources: list[str]
    decision = None  # Filled only on the new-record path; new entries
                     # carry their intake findings forward into the log.
    corpus_text = ""

    # Phase 1 of branching: compute the read-side state. No serialization
    # work happens here so dry-run cannot fail on a malformed existing
    # page — that's deferred to the write-gate below.
    if target_path.exists():
        # Existing entity → straight to merge. No intake call: the gate
        # would reject this as DUPLICATE against the cached embedding
        # of the original ingest, blocking the source-merge that's the
        # whole point of re-fetching. Phase 3b made this concrete.
        is_new_page = False
        existing_text = target_path.read_text(encoding="utf-8")
        existing_fm = _parse_frontmatter(existing_text)
        merged_sources = _merge_sources(existing_fm, record.sources)
        kept_description = _keep_longer_description(existing_fm, record)
    else:
        # New entity → intake gate applies. A DUPLICATE finding here
        # would mean a *different* slug has near-identical content,
        # which is a real signal we want to surface.
        corpus_text = _build_corpus_text(record)
        decision = check_intake(corpus_text, "mcp-servers")
        if not decision.allow:
            raise IntakeRejected(decision)

        is_new_page = True
        existing_text = ""
        existing_fm = {}
        merged_sources = sorted(record.sources)
        kept_description = record.description

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

    if review_existing and not is_new_page and not update_existing:
        review = build_update_review(
            entity_type="mcp-server",
            slug=record.slug,
            existing_text=existing_text,
            proposed_text=final_text,
        )
        return {
            "slug": record.slug,
            "is_new_page": False,
            "merged_sources": merged_sources,
            "path": str(target_path),
            "skipped": True,
            "update_required": True,
            "update_review": render_update_review(review),
        }

    if not dry_run:
        # Phase 2 of branching: render and write. Any YAML serialization
        # failure now is a real error, not a dry-run side-effect.
        safe_atomic_write_text(target_path, final_text, encoding="utf-8")

        # Phase 6b: keep the canonical sidecar index hot. Upsert on
        # every successful write so the first cross-source dedup after
        # this add is O(1) without needing a rebuild. Applies to both
        # new pages AND source merges — a merge can land a github_url
        # from the second source when the first source lacked one.
        # Index-write failures must not break the add; the next lookup
        # will scan-and-repair.
        canonical = _normalize_github_url(record.github_url)
        if canonical is not None:
            try:
                relpath = target_path.relative_to(mcp_dir).as_posix()
                mcp_canonical_index.upsert(
                    mcp_dir,
                    canonical,
                    slug=record.slug,
                    relpath=relpath,
                )
            except (OSError, ValueError) as exc:
                print(
                    f"Warning: failed to update canonical index for {record.slug}: {exc}",
                    file=sys.stderr,
                )

        # Embed + index + log only on first creation. Re-merging an
        # existing entity does not invalidate its embedding (content
        # is the same — sources are metadata, not corpus text), and
        # logging every re-merge would blow up log.md at scale.
        if is_new_page:
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

            update_index(str(wiki_path), [record.slug], subject_type="mcp-servers")

            log_details = [
                f"Slug: {record.slug}",
                f"Path: {target_path}",
                f"Sources: {', '.join(merged_sources) if merged_sources else 'none'}",
                f"Tags: {', '.join(record.tags) if record.tags else 'none'}",
                f"Transports: {', '.join(record.transports) if record.transports else 'unknown'}",
            ]
            # ``decision`` is set whenever ``is_new_page`` is True (intake
            # ran on the new-record path). The assert is a typing invariant,
            # not a runtime guard.
            assert decision is not None
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
        "skipped": False,
        "update_required": False,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def _process_batch(
    records: list[dict[str, Any]],
    wiki_path: Path,
    dry_run: bool,
    skip_existing: bool,
    update_existing: bool,
    mcp_entity_dir: Path,
) -> tuple[int, int, int, int, int]:
    """Process records. Returns (added, merged, reviewed, rejected, errors)."""
    added = merged = reviewed = rejected = errors = 0
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
            result = add_mcp(
                record=record,
                wiki_path=wiki_path,
                dry_run=dry_run,
                review_existing=True,
                update_existing=update_existing,
            )
            if result["is_new_page"]:
                added += 1
                status = "added"
            elif result.get("update_required"):
                reviewed += 1
                status = "update-review"
                if result.get("update_review"):
                    print(result["update_review"])
            else:
                merged += 1
                status = "updated" if update_existing else "merged"
            print(f"  [{i}/{total}] [{status}] {record.slug}")
        except IntakeRejected as exc:
            rejected += 1
            codes = ", ".join(f.code for f in exc.decision.failures) or "unknown"
            print(f"  [{i}/{total}] [rejected] {record.slug}: {codes}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — batch CLI must continue past one failure
            errors += 1
            print(f"  [{i}/{total}] ERROR: {record.slug}: {exc}", file=sys.stderr)

    return added, merged, reviewed, rejected, errors


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
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Apply the reviewed replacement when an MCP entity already exists",
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

    added, merged, reviewed, rejected, errors = _process_batch(
        records=raw_records,
        wiki_path=wiki_path,
        dry_run=args.dry_run,
        skip_existing=args.skip_existing,
        update_existing=args.update_existing,
        mcp_entity_dir=mcp_entity_dir,
    )

    dry_label = " (dry-run)" if args.dry_run else ""
    print(
        f"\nDone{dry_label}: {added} added, {merged} updated, "
        f"{reviewed} reviewed, {rejected} rejected, {errors} errors"
    )


if __name__ == "__main__":
    main()
