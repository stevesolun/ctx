#!/usr/bin/env python3
"""
skill_add.py -- Add new skills with automatic micro-skill conversion and wiki ingestion.

Usage:
    # Single skill
    python skill_add.py --skill-path /path/to/SKILL.md --name my-skill \
        --wiki ~/.claude/skill-wiki --skills-dir ~/.claude/skills

    # Batch from directory
    python skill_add.py --scan-dir /path/to/new-skills/ \
        --wiki ~/.claude/skill-wiki --skills-dir ~/.claude/skills
"""

import argparse
import os
import re
import sys
import yaml  # type: ignore[import-untyped]
from datetime import datetime, timezone
from pathlib import Path

from batch_convert import convert_skill
from ctx.core.entity_update import build_update_review, render_update_review
from ctx_config import cfg
from intake_pipeline import IntakeRejected, check_intake, record_embedding
from ctx.adapters.claude_code.install.install_utils import safe_copy_file
from ctx.core.wiki.wiki_queue import enqueue_entity_upsert
from ctx.core.wiki.wiki_sync import append_log, ensure_wiki, update_index
from ctx.core.wiki.wiki_utils import validate_skill_name
from ctx.utils._fs_utils import reject_symlink_path, safe_atomic_write_text

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Tag inference ─────────────────────────────────────────────────────────────

def infer_tags(name: str, content: str) -> list[str]:
    """Infer taxonomy tags from skill name and file content."""
    combined = f"{name} {content}".lower()
    found = [tag for tag in cfg.all_tags if re.search(rf"\b{re.escape(tag)}\b", combined)]
    return found if found else ["uncategorized"]


# ── Skills-dir helpers ────────────────────────────────────────────────────────

def install_skill(source: Path, skills_dir: Path, name: str) -> Path:
    """Copy SKILL.md into skills_dir/<name>/SKILL.md. Returns the installed path."""
    dest_dir = skills_dir / name
    dest = dest_dir / "SKILL.md"
    safe_copy_file(source, dest, dest_root=skills_dir)
    return dest


# ── Conversion ────────────────────────────────────────────────────────────────

def maybe_convert(
    installed_path: Path,
    name: str,
    converted_root: Path,
    line_count: int,
) -> tuple[bool, Path | None]:
    """Convert skill to micro-skill pipeline if >180 lines.

    Args:
        installed_path: Path to the installed SKILL.md (original, never touched).
        name: Skill name.
        converted_root: ~/.claude/skill-wiki/converted/
        line_count: Pre-computed line count of the source file.

    Returns:
        (was_converted, output_dir | None)
    """
    if line_count <= cfg.line_threshold:
        return False, None

    output_dir = converted_root / name
    output_dir.mkdir(parents=True, exist_ok=True)

    # batch_convert.convert_skill operates on the source path and writes to output_dir
    result = convert_skill(installed_path, output_dir)

    if result.get("status") == "converted":
        return True, output_dir

    return False, None


# ── Wiki entity page ──────────────────────────────────────────────────────────

def build_entity_page(
    *,
    name: str,
    tags: list[str],
    line_count: int,
    has_pipeline: bool,
    original_path: Path,
    related: list[str],
    scan_sources: list[str],
) -> str:
    """Render the full entity page markdown for a skill."""
    pipeline_path_str = (
        f"converted/{name}/" if has_pipeline else "null"
    )

    fm_dict: dict = {
        "title": name,
        "created": TODAY,
        "updated": TODAY,
        "type": "skill",
        "status": "installed",
        "tags": tags,
        "source": "local",
        "original_path": str(original_path),
        "original_lines": line_count,
        "has_pipeline": has_pipeline,
        "pipeline_path": pipeline_path_str,
        "always_load": False,
        "never_load": False,
        "last_used": TODAY,
        "use_count": 0,
        "avg_session_rating": None,
        "notes": "",
    }
    if scan_sources:
        fm_dict["sources"] = scan_sources

    frontmatter_body = yaml.safe_dump(fm_dict, default_flow_style=False, allow_unicode=True, sort_keys=False)
    frontmatter_block = f"---\n{frontmatter_body}---"

    related_links = "\n".join(f"- [[entities/skills/{r}]]" for r in related[:6])
    if not related_links:
        related_links = "<!-- No related skills found yet -->"

    pipeline_note = (
        f"Pipeline converted to `{pipeline_path_str}` (original: {line_count} lines)."
        if has_pipeline
        else f"Skill is {line_count} lines — under the {cfg.line_threshold}-line threshold, no pipeline generated."
    )

    return frontmatter_block + f"""

# {name}

## Overview

{pipeline_note}

## Tags

{', '.join(f'`{t}`' for t in tags)}

## Related Skills

{related_links}

## Usage History

| Date | Action | Notes |
|------|--------|-------|
| {TODAY} | Added | Ingested via skill_add.py |
"""


def write_entity_page(wiki_path: Path, name: str, content: str) -> bool:
    """Write entity page. Returns True if newly created."""
    page = wiki_path / "entities" / "skills" / f"{name}.md"
    reject_symlink_path(page)
    is_new = not page.exists()
    safe_atomic_write_text(page, content, encoding="utf-8")
    return is_new


# ── Wikilink backfill ─────────────────────────────────────────────────────────

def find_related_skills(wiki_path: Path, name: str, tags: list[str]) -> list[str]:
    """Scan existing entity pages for skills that share at least one tag."""
    skills_dir = wiki_path / "entities" / "skills"
    related: list[str] = []
    tag_set = set(tags) - {"uncategorized"}

    for page in sorted(skills_dir.glob("*.md")):
        if page.stem == name:
            continue
        content = page.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^tags:\s*\[([^\]]*)\]", content, re.MULTILINE)
        if not m:
            continue
        page_tags = {t.strip() for t in m.group(1).split(",")}
        if tag_set & page_tags:
            related.append(page.stem)

    return related


def _add_backlink(wiki_path: Path, target_name: str, source_name: str) -> None:
    """Add a [[wikilink]] from target page back to source if not already present."""
    page = wiki_path / "entities" / "skills" / f"{target_name}.md"
    reject_symlink_path(page)
    if not page.exists():
        return
    content = page.read_text(encoding="utf-8", errors="replace")
    link = f"[[entities/skills/{source_name}]]"
    if link in content:
        return
    # Append under Related Skills section if present, else end of file
    if "## Related Skills" in content:
        content = content.replace(
            "## Related Skills\n",
            f"## Related Skills\n- {link}\n",
            1,
        )
    else:
        content = content.rstrip() + f"\n\n- {link}\n"
    safe_atomic_write_text(page, content, encoding="utf-8")


def wire_backlinks(wiki_path: Path, name: str, related: list[str]) -> None:
    """Bidirectionally add wikilinks between name and each related skill."""
    for target in related:
        _add_backlink(wiki_path, target, name)


# ── Scan-source detection ─────────────────────────────────────────────────────

def detect_scan_sources(wiki_path: Path, name: str) -> list[str]:
    """Return filenames in raw/scans/ that reference this skill name."""
    scans_dir = wiki_path / "raw" / "scans"
    if not scans_dir.exists():
        return []
    sources: list[str] = []
    for scan in sorted(scans_dir.glob("*.json")):
        try:
            text = scan.read_text(encoding="utf-8", errors="replace")
            if name in text:
                sources.append(scan.name)
        except OSError:
            pass
    return sources


# ── Core orchestration ────────────────────────────────────────────────────────

def add_skill(
    *,
    source_path: Path,
    name: str,
    wiki_path: Path,
    skills_dir: Path,
    review_existing: bool = False,
    update_existing: bool = False,
) -> dict:
    """Add a single skill: install, convert if needed, ingest into wiki.

    Returns a result dict with keys: name, installed, converted, is_new_page.
    """
    validate_skill_name(name)

    # Reject oversized files before reading into memory
    file_size = source_path.stat().st_size
    if file_size > 1_048_576:  # 1 MB
        raise ValueError(
            f"SKILL.md too large ({file_size:,} bytes). Max 1 MB. "
            f"Split the skill or trim content before ingestion."
        )

    content = source_path.read_text(encoding="utf-8", errors="replace")
    line_count = len(content.splitlines())

    installed_path = skills_dir / name / "SKILL.md"
    entity_page = wiki_path / "entities" / "skills" / f"{name}.md"
    existing_path = (
        installed_path
        if installed_path.exists()
        else entity_page if entity_page.exists() else None
    )
    has_existing = existing_path is not None

    if review_existing and has_existing and not update_existing:
        assert existing_path is not None
        existing_text = existing_path.read_text(encoding="utf-8", errors="replace")
        review = build_update_review(
            entity_type="skill",
            slug=name,
            existing_text=existing_text,
            proposed_text=content,
        )
        return {
            "name": name,
            "installed": str(installed_path),
            "converted": False,
            "is_new_page": False,
            "skipped": True,
            "update_required": True,
            "update_review": render_update_review(review),
        }

    if not has_existing:
        # Intake gate: reject broken/duplicate candidates before we touch
        # skills-dir. Existing updates bypass similarity intake because
        # they compare against their own cached embedding.
        decision = check_intake(content, "skills")
        if not decision.allow:
            raise IntakeRejected(decision)

    tags = infer_tags(name, content)

    # 1. Install original into skills-dir (never modified after this)
    installed_path = install_skill(source_path, skills_dir, name)

    # Record the candidate's embedding so future intake checks can
    # rank against it. Failure here is non-fatal — the install already
    # succeeded and a missing vector only weakens the next check, it
    # doesn't corrupt anything.
    try:
        record_embedding(subject_id=name, raw_md=content, subject_type="skills")
    except Exception as exc:  # noqa: BLE001 — cache failure must not break install
        print(
            f"Warning: failed to record intake embedding for {name}: {exc}",
            file=sys.stderr,
        )

    # 2. Convert if above threshold
    converted_root = wiki_path / "converted"
    converted, pipeline_path = maybe_convert(installed_path, name, converted_root, line_count)

    # 3. Detect related skills and scan sources (before writing new page)
    related = find_related_skills(wiki_path, name, tags)
    scan_sources = detect_scan_sources(wiki_path, name)

    # Ensure at least 2 wikilinks (pad with first two related even if no tag match)
    all_entity_pages = sorted(
        (p.stem for p in (wiki_path / "entities" / "skills").glob("*.md") if p.stem != name)
    )
    while len(related) < 2 and len(all_entity_pages) > len(related):
        candidate = all_entity_pages[len(related)]
        if candidate not in related:
            related.append(candidate)

    # 4. Write entity page
    page_content = build_entity_page(
        name=name,
        tags=tags,
        line_count=line_count,
        has_pipeline=converted,
        original_path=installed_path,
        related=related,
        scan_sources=scan_sources,
    )
    is_new = write_entity_page(wiki_path, name, page_content)

    # 5. Bidirectional wikilinks
    wire_backlinks(wiki_path, name, related)

    # 6. Index + log
    if is_new:
        update_index(str(wiki_path), [name])

    log_details = [
        f"Source: {source_path}",
        f"Installed: {installed_path}",
        f"Lines: {line_count}",
        f"Tags: {', '.join(tags)}",
        f"Converted: {converted}",
        f"Related: {', '.join(related) if related else 'none'}",
    ]
    if converted and pipeline_path:
        log_details.append(f"Pipeline: {pipeline_path}")
    append_log(str(wiki_path), "add-skill", name, log_details)
    queue_job = enqueue_entity_upsert(
        wiki_path,
        entity_type="skill",
        slug=name,
        entity_path=wiki_path / "entities" / "skills" / f"{name}.md",
        content=page_content,
        action="created" if is_new else "updated",
        source="skill_add",
    )

    # Append to the unified audit log so post-hoc investigations can
    # reconstruct every add/convert/install event without mining
    # per-subsystem log files. See src/ctx_audit_log.py.
    try:
        from ctx_audit_log import log_skill_event
        log_skill_event(
            "skill.added" if is_new else "skill.installed",
            name,
            actor="cli",
            meta={
                "source": str(source_path),
                "installed": str(installed_path),
                "lines": line_count,
                "converted": converted,
                "tags": tags,
                "related": related,
            },
        )
        if converted:
            log_skill_event(
                "skill.converted", name, actor="cli",
                meta={"pipeline": str(pipeline_path)},
            )
    except Exception:  # noqa: BLE001 — audit is best-effort
        pass

    return {
        "name": name,
        "installed": str(installed_path),
        "converted": converted,
        "is_new_page": is_new,
        "skipped": False,
        "update_required": False,
        "queued_job_id": queue_job.id,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Add new skills with wiki ingestion")
    parser.add_argument("--skill-path", help="Path to a single SKILL.md to add")
    parser.add_argument("--name", help="Skill name (required with --skill-path)")
    parser.add_argument("--scan-dir", help="Directory of skills to batch-add (each subdir with SKILL.md)")
    parser.add_argument("--skip-existing", action="store_true", help="Skip skills already installed (prevents overwrites)")
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Apply the reviewed replacement when a skill already exists",
    )
    parser.add_argument("--wiki", default=str(cfg.wiki_dir), help="Wiki path")
    parser.add_argument("--skills-dir", default=str(cfg.skills_dir), help="Skills install path")
    args = parser.parse_args()

    wiki_path = Path(os.path.expanduser(args.wiki))
    skills_dir = Path(os.path.expanduser(args.skills_dir))

    ensure_wiki(str(wiki_path))
    skills_dir.mkdir(parents=True, exist_ok=True)

    if args.skill_path and args.scan_dir:
        print("Error: use --skill-path or --scan-dir, not both.", file=sys.stderr)
        sys.exit(1)

    if not args.skill_path and not args.scan_dir:
        print("Error: --skill-path or --scan-dir is required.", file=sys.stderr)
        sys.exit(1)

    # Build the list of (source_path, name) pairs to process
    candidates: list[tuple[Path, str]] = []

    if args.skill_path:
        if not args.name:
            print("Error: --name is required with --skill-path.", file=sys.stderr)
            sys.exit(1)
        source = Path(os.path.expanduser(args.skill_path))
        if not source.exists():
            print(f"Error: {source} does not exist.", file=sys.stderr)
            sys.exit(1)
        candidates.append((source, args.name))

    if args.scan_dir:
        scan_root = Path(os.path.expanduser(args.scan_dir))
        if not scan_root.exists():
            print(f"Error: {scan_root} does not exist.", file=sys.stderr)
            sys.exit(1)
        for skill_md in sorted(scan_root.rglob("SKILL.md")):
            skill_name = skill_md.parent.name
            candidates.append((skill_md, skill_name))

        if not candidates:
            print(f"No SKILL.md files found under {scan_root}.", file=sys.stderr)
            sys.exit(0)

    added = updated = converted = skipped = errors = 0
    total = len(candidates)
    for i, (source_path, name) in enumerate(candidates, 1):
        # Skip if already installed and --skip-existing is set
        if args.skip_existing and (skills_dir / name / "SKILL.md").exists():
            skipped += 1
            if skipped <= 5 or skipped % 100 == 0:
                print(f"  [{i}/{total}] [skipped] {name}")
            continue
        try:
            result = add_skill(
                source_path=source_path,
                name=name,
                wiki_path=wiki_path,
                skills_dir=skills_dir,
                review_existing=True,
                update_existing=args.update_existing,
            )
            if result.get("skipped"):
                skipped += 1
                if result.get("update_review"):
                    print(result["update_review"])
                print(f"  [{i}/{total}] [update-review] {name}")
                continue
            if result["is_new_page"]:
                added += 1
            else:
                updated += 1
            if result["converted"]:
                converted += 1
            status = (
                "updated"
                if not result["is_new_page"]
                else "converted" if result["converted"] else "installed"
            )
            print(f"  [{i}/{total}] [{status}] {name}")
        except Exception as exc:
            errors += 1
            print(f"  [{i}/{total}] ERROR: {name}: {exc}", file=sys.stderr)

    print(
        f"\nDone: {added} added, {updated} updated, {converted} converted, "
        f"{skipped} skipped, {errors} errors"
    )


if __name__ == "__main__":
    main()
