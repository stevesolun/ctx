#!/usr/bin/env python3
"""
skill_mirror.py -- Mirror locally-installed short skills into the wiki.

Context: `<wiki>/converted/<slug>/SKILL.md` is the canonical source that
`ctx-skill-install` reads from. Long skills (> cfg.line_threshold lines,
default 180) go through `batch_convert` into a `converted/<slug>/` with
SKILL.md + references/*.md. Short skills skip the pipeline — their
content lives ONLY at `~/.claude/skills/<slug>/SKILL.md` on the
maintainer's machine, NOT in the wiki. That breaks portability:
  - The shipped `graph/wiki-graph.tar.gz` lists them in `entities/skills/`
    but the install CLI returns `not-in-wiki` because there's no
    `converted/<slug>/SKILL.md`.
  - A fresh-clone user can browse them in the catalog but can't install.

This module scans `~/.claude/skills/<slug>/SKILL.md` and, for every slug
that has no `<wiki>/converted/<slug>/` dir yet, writes a minimal
`converted/<slug>/SKILL.md` containing the local body verbatim. Long
skills with existing `converted/` are left untouched.

The CLI is a one-shot admin operation. `ctx-skill-install` stays
exactly as-is — its `_pick_source` already reads `converted/<slug>/
SKILL.md` which is now populated for every skill.

Usage:
    ctx-skill-mirror                     # mirror everything missing
    ctx-skill-mirror --slug foo          # mirror one slug
    ctx-skill-mirror --force             # overwrite even when converted/
                                         # exists (for re-sync after a
                                         # local edit)
    ctx-skill-mirror --dry-run           # report without writing
    ctx-skill-mirror --prune             # delete mirrors whose local
                                         # source has disappeared
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text
from ctx_config import cfg
from ctx.core.wiki.wiki_utils import validate_skill_name

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MirrorResult:
    """One outcome per slug."""

    slug: str
    status: str  # "mirrored" | "unchanged" | "skipped-existing-pipeline"
                 # | "skipped-too-long" | "skipped-invalid"
                 # | "pruned" | "not-found"
    source_path: str | None
    dest_path: str | None
    body_lines: int | None = None
    message: str = ""


# ── Local scan ───────────────────────────────────────────────────────────────


def _iter_local_skill_dirs(skills_dir: Path) -> list[Path]:
    """Every `<skills_dir>/<slug>/` containing a SKILL.md. Sorted for stable logs."""
    if not skills_dir.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        if (child / "SKILL.md").is_file():
            out.append(child)
    return out


def _line_count(text: str) -> int:
    """Count lines in text. Trailing newline doesn't add an extra line."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


# ── Per-slug mirror ──────────────────────────────────────────────────────────


def _converted_dir(wiki_dir: Path, slug: str) -> Path:
    return wiki_dir / "converted" / slug


def mirror_one(
    slug: str,
    *,
    skills_dir: Path,
    wiki_dir: Path,
    line_threshold: int,
    force: bool = False,
    dry_run: bool = False,
) -> MirrorResult:
    """Mirror one local short skill's SKILL.md into the wiki.

    Behaviour:
      - `<wiki>/converted/<slug>/` already exists AND not force: return
        `skipped-existing-pipeline`. The long-skill pipeline owns this
        dir; we must not overwrite its SKILL.md with the raw body.
      - Local skill body exceeds `line_threshold`: return
        `skipped-too-long`. Those belong in the pipeline, not here.
      - Slug fails `validate_skill_name`: return `skipped-invalid`.
      - `~/.claude/skills/<slug>/SKILL.md` missing: return `not-found`.
      - Otherwise: write `<wiki>/converted/<slug>/SKILL.md` with the
        body verbatim. Return `mirrored` (or `unchanged` when the
        destination already matches byte-for-byte and force is off).
    """
    try:
        validate_skill_name(slug)
    except ValueError as exc:
        return MirrorResult(
            slug=slug, status="skipped-invalid",
            source_path=None, dest_path=None,
            message=f"invalid slug: {exc}",
        )

    source = skills_dir / slug / "SKILL.md"
    if not source.is_file():
        return MirrorResult(
            slug=slug, status="not-found",
            source_path=None, dest_path=None,
            message=f"no local skill at {source}",
        )

    try:
        body = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return MirrorResult(
            slug=slug, status="skipped-invalid",
            source_path=str(source), dest_path=None,
            message=f"read failed: {exc}",
        )
    lines = _line_count(body)

    if lines > line_threshold:
        # Long skills go through batch_convert. Refusing them here
        # prevents a mirror from clobbering a legit pipeline.
        return MirrorResult(
            slug=slug, status="skipped-too-long",
            source_path=str(source), dest_path=None, body_lines=lines,
            message=(
                f"{lines} lines > line_threshold={line_threshold}; "
                "use ctx-skill-add / batch_convert for long skills"
            ),
        )

    dest_dir = _converted_dir(wiki_dir, slug)
    dest = dest_dir / "SKILL.md"

    # Existing converted/<slug>/ dir usually means the long-skill pipeline
    # produced it. Don't overwrite without --force: an accidental mirror
    # call must never replace a pipeline-converted SKILL.md with raw body.
    if dest_dir.is_dir() and not force:
        if dest.is_file():
            try:
                existing = dest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                existing = ""
            if existing == body:
                return MirrorResult(
                    slug=slug, status="unchanged",
                    source_path=str(source), dest_path=str(dest),
                    body_lines=lines,
                )
        return MirrorResult(
            slug=slug, status="skipped-existing-pipeline",
            source_path=str(source), dest_path=str(dest), body_lines=lines,
            message="converted/<slug>/ already exists; pass --force to overwrite",
        )

    if dry_run:
        return MirrorResult(
            slug=slug, status="mirrored",
            source_path=str(source), dest_path=str(dest), body_lines=lines,
            message="dry-run: no files written",
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(dest, body)
    return MirrorResult(
        slug=slug, status="mirrored",
        source_path=str(source), dest_path=str(dest), body_lines=lines,
    )


# ── Bulk mirror ──────────────────────────────────────────────────────────────


def mirror_all(
    *,
    skills_dir: Path,
    wiki_dir: Path,
    line_threshold: int,
    force: bool = False,
    dry_run: bool = False,
) -> list[MirrorResult]:
    """Mirror every short local skill that has no wiki converted/ dir."""
    results: list[MirrorResult] = []
    for path in _iter_local_skill_dirs(skills_dir):
        slug = path.name
        results.append(mirror_one(
            slug, skills_dir=skills_dir, wiki_dir=wiki_dir,
            line_threshold=line_threshold, force=force, dry_run=dry_run,
        ))
    return results


def prune_orphans(
    *,
    skills_dir: Path,
    wiki_dir: Path,
    dry_run: bool = False,
) -> list[MirrorResult]:
    """Delete short-skill mirror dirs whose local source vanished.

    ONLY deletes mirror dirs that match the short-skill shape — a
    single ``SKILL.md`` and nothing else. Long-skill pipeline dirs
    (SKILL.md + references/ + the rest) are left alone even if the
    local skill has been uninstalled — those dirs carry converted
    content derived from the original, not a raw mirror.
    """
    mirror_root = wiki_dir / "converted"
    if not mirror_root.is_dir():
        return []
    results: list[MirrorResult] = []
    for conv_dir in sorted(mirror_root.iterdir()):
        if not conv_dir.is_dir():
            continue
        slug = conv_dir.name
        local_src = skills_dir / slug / "SKILL.md"
        if local_src.is_file():
            continue  # still present upstream
        # Don't touch long-skill pipelines — they have references/ or
        # other siblings beyond SKILL.md.
        contents = {p.name for p in conv_dir.iterdir()}
        if contents and contents != {"SKILL.md"}:
            continue
        if dry_run:
            results.append(MirrorResult(
                slug=slug, status="pruned",
                source_path=None, dest_path=str(conv_dir),
                message="dry-run: would delete",
            ))
            continue
        try:
            for f in conv_dir.iterdir():
                f.unlink()
            conv_dir.rmdir()
            results.append(MirrorResult(
                slug=slug, status="pruned",
                source_path=None, dest_path=str(conv_dir),
            ))
        except OSError as exc:
            results.append(MirrorResult(
                slug=slug, status="skipped-invalid",
                source_path=None, dest_path=str(conv_dir),
                message=f"unlink failed: {exc}",
            ))
    return results


# ── CLI ──────────────────────────────────────────────────────────────────────


def _summarize(results: list[MirrorResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-skill-mirror",
        description=(
            "Mirror short skills (<line_threshold lines) from "
            "~/.claude/skills/ into <wiki>/converted/<slug>/SKILL.md "
            "so ctx-skill-install finds them after a fresh tarball "
            "extract. Companion to ctx-agent-mirror."
        ),
    )
    parser.add_argument("--slug", help="Mirror a single slug")
    parser.add_argument(
        "--force", action="store_true",
        help="Rewrite mirrored files even when converted/<slug>/ exists",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="Delete short-skill mirror dirs whose local source vanished",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen without writing",
    )
    parser.add_argument(
        "--skills-dir", default=str(cfg.skills_dir),
        help="Live skills dir (default: cfg.skills_dir)",
    )
    parser.add_argument(
        "--wiki-dir", default=str(cfg.wiki_dir),
        help="Wiki root (default: cfg.wiki_dir)",
    )
    parser.add_argument(
        "--line-threshold", type=int, default=cfg.line_threshold,
        help=(
            f"Max lines for a skill to qualify as short "
            f"(default {cfg.line_threshold}; skills above this belong "
            "in the batch_convert pipeline, not here)"
        ),
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit results as JSON for automation",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    skills_dir = Path(os.path.expanduser(args.skills_dir))
    wiki_dir = Path(os.path.expanduser(args.wiki_dir))

    results: list[MirrorResult] = []
    if args.slug:
        results.append(mirror_one(
            args.slug, skills_dir=skills_dir, wiki_dir=wiki_dir,
            line_threshold=args.line_threshold,
            force=args.force, dry_run=args.dry_run,
        ))
    else:
        results.extend(mirror_all(
            skills_dir=skills_dir, wiki_dir=wiki_dir,
            line_threshold=args.line_threshold,
            force=args.force, dry_run=args.dry_run,
        ))
        if args.prune:
            results.extend(prune_orphans(
                skills_dir=skills_dir, wiki_dir=wiki_dir,
                dry_run=args.dry_run,
            ))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        counts = _summarize(results)
        print("Skill-mirror summary:")
        for status in sorted(counts):
            print(f"  {status}: {counts[status]}")
        # Surface unexpected outcomes explicitly.
        for r in results:
            if r.status not in ("mirrored", "unchanged", "skipped-too-long",
                                "skipped-existing-pipeline"):
                print(f"  [{r.status}] {r.slug}: {r.message}")

    hard_failures = [
        r for r in results
        if r.status == "skipped-invalid"
        and r.message.startswith(("read failed", "unlink failed"))
    ]
    sys.exit(1 if hard_failures else 0)


if __name__ == "__main__":
    main()
