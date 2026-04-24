#!/usr/bin/env python3
"""
skill_install.py -- Install a skill from the wiki into the live skills directory.

Target UX: a new user clones the ``ctx`` repo, runs
``ctx-skill-install <slug>``, and the skill lands under
``~/.claude/skills/<slug>/`` where Claude Code auto-loads it. No git
clone per skill, no registry lookup — the wiki is the single source of
truth.

Source selection (in order):

  1. ``<wiki>/converted/<slug>/SKILL.md``          — canonical wiki body
  2. ``<wiki>/converted/<slug>/SKILL.md.original`` — pre-conversion backup

If neither exists, the wiki has only the entity card for this slug (short
skill never converted, no original snapshot) and the install fails with
a clear error rather than copying an empty shell.

If ``<wiki>/converted/<slug>/references/`` exists, the pipeline stages
are mirrored into ``~/.claude/skills/<slug>/references/`` so multi-stage
skills retain their structure.

This is the reverse of ``skill_unload.py``: it adds a ``load`` entry to
``~/.claude/skill-manifest.json``, bumps the wiki entity's ``status``
frontmatter to ``installed``, and emits a ``load`` telemetry event.

Usage:
    ctx-skill-install --slug accessibility-compliance
    ctx-skill-install --slugs "accessibility-compliance,python-testing"
    ctx-skill-install --slug fastapi-pro --prefer original
    ctx-skill-install --slug fastapi-pro --force   # overwrite existing local copy
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from ctx_config import cfg
from ctx.adapters.claude_code.install.install_utils import (
    bump_entity_status,
    emit_load_event,
    record_install,
)
from ctx.core.wiki.wiki_utils import validate_skill_name

_logger = logging.getLogger(__name__)

# Stable session ID so telemetry can correlate a multi-slug install call.
_SESSION_ID: str = uuid.uuid4().hex


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a single install. One per slug."""

    slug: str
    status: str  # "installed" | "skipped-existing" | "not-in-wiki" | "failed"
    installed_path: str | None
    source_variant: str | None  # "transformed" | "original" | None
    references_copied: int
    message: str = ""


# ── Wiki lookups ─────────────────────────────────────────────────────────────


def _entity_path(wiki_dir: Path, slug: str) -> Path:
    """Return the expected entity-card path for ``slug``."""
    return wiki_dir / "entities" / "skills" / f"{slug}.md"


def _converted_dir(wiki_dir: Path, slug: str) -> Path:
    """Return the expected converted-content dir for ``slug``."""
    return wiki_dir / "converted" / slug


def _pick_source(
    converted: Path, prefer: str
) -> tuple[Path | None, str | None]:
    """Pick the on-disk SKILL.md to install.

    Returns ``(path, variant)`` where variant is ``"transformed"`` for
    the canonical SKILL.md or ``"original"`` for the .original backup.
    Returns ``(None, None)`` when neither exists.
    """
    transformed = converted / "SKILL.md"
    original = converted / "SKILL.md.original"

    if prefer == "original" and original.is_file():
        return original, "original"
    if transformed.is_file():
        return transformed, "transformed"
    if original.is_file():
        return original, "original"
    return None, None


# ── Copy logic ───────────────────────────────────────────────────────────────


def _copy_references(src_dir: Path, dest_dir: Path) -> int:
    """Copy every .md in ``src_dir/references/`` to ``dest_dir/references/``.

    Returns the number of reference files copied. Skips silently when no
    references dir exists in the wiki.
    """
    src_refs = src_dir / "references"
    if not src_refs.is_dir():
        return 0
    dest_refs = dest_dir / "references"
    dest_refs.mkdir(parents=True, exist_ok=True)
    copied = 0
    for md_file in sorted(src_refs.glob("*.md")):
        dest = dest_refs / md_file.name
        shutil.copy2(md_file, dest)
        copied += 1
    return copied


def install_skill(
    slug: str,
    *,
    wiki_dir: Path,
    skills_dir: Path,
    prefer: str = "transformed",
    force: bool = False,
    dry_run: bool = False,
) -> InstallResult:
    """Install one skill from the wiki into the live skills directory.

    The install is:

      1. Validated (slug passes ``validate_skill_name``).
      2. Sourced from the wiki (``converted/<slug>/SKILL.md``, with
         ``.original`` as fallback).
      3. Copied to ``<skills_dir>/<slug>/SKILL.md`` plus any references.
      4. Mirrored into the skill manifest and the wiki entity's status
         frontmatter.

    ``dry_run=True`` skips the copy + state updates; everything else is
    evaluated so the caller sees what would happen.
    """
    try:
        validate_skill_name(slug)
    except ValueError as exc:
        return InstallResult(
            slug=slug, status="failed", installed_path=None,
            source_variant=None, references_copied=0,
            message=f"invalid slug: {exc}",
        )

    converted = _converted_dir(wiki_dir, slug)
    if not converted.is_dir():
        return InstallResult(
            slug=slug, status="not-in-wiki", installed_path=None,
            source_variant=None, references_copied=0,
            message=f"no wiki content at {converted}",
        )

    source, variant = _pick_source(converted, prefer)
    if source is None:
        return InstallResult(
            slug=slug, status="not-in-wiki", installed_path=None,
            source_variant=None, references_copied=0,
            message="wiki has no SKILL.md or SKILL.md.original",
        )

    dest_dir = skills_dir / slug
    dest = dest_dir / "SKILL.md"

    if dest.exists() and not force:
        # Already installed. Still refresh manifest/status so an earlier
        # install that didn't record into manifest gets reconciled.
        if not dry_run:
            record_install(
                slug, entity_type="skill", source="ctx-skill-install",
            )
            bump_entity_status(_entity_path(wiki_dir, slug), status="installed")
        return InstallResult(
            slug=slug, status="skipped-existing",
            installed_path=str(dest), source_variant=variant,
            references_copied=0,
            message="already installed; pass --force to overwrite",
        )

    if dry_run:
        refs_count = 0
        refs_dir = converted / "references"
        if refs_dir.is_dir():
            refs_count = sum(1 for _ in refs_dir.glob("*.md"))
        return InstallResult(
            slug=slug, status="installed", installed_path=str(dest),
            source_variant=variant, references_copied=refs_count,
            message="dry-run: no files written",
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    refs_copied = _copy_references(converted, dest_dir)

    record_install(slug, entity_type="skill", source="ctx-skill-install")
    bump_entity_status(_entity_path(wiki_dir, slug), status="installed")
    emit_load_event(slug, _SESSION_ID)

    return InstallResult(
        slug=slug, status="installed", installed_path=str(dest),
        source_variant=variant, references_copied=refs_copied,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _split_slugs(args: argparse.Namespace) -> list[str]:
    """Collect slugs from --slug/--slugs/--all-from-manifest/positional."""
    out: list[str] = []
    if args.slug:
        out.append(args.slug)
    if args.slugs:
        out.extend(s.strip() for s in args.slugs.split(",") if s.strip())
    if args.slugs_positional:
        out.extend(args.slugs_positional)
    return out


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctx-skill-install",
        description=(
            "Install a skill from the wiki into ~/.claude/skills/. "
            "Source: <wiki>/converted/<slug>/SKILL.md (or SKILL.md.original "
            "when --prefer original). "
            "Also updates the skill manifest and the wiki entity status."
        ),
    )
    parser.add_argument("slugs_positional", nargs="*", help="Slugs to install (positional)")
    parser.add_argument("--slug", help="Single skill slug")
    parser.add_argument("--slugs", help="Comma-separated slugs")
    parser.add_argument(
        "--prefer",
        choices=("transformed", "original"),
        default="transformed",
        help="Which variant to install when both exist (default: transformed)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing installed SKILL.md at the target path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without writing any files",
    )
    parser.add_argument(
        "--wiki-dir",
        default=str(cfg.wiki_dir),
        help="Wiki root (default: ctx_config.cfg.wiki_dir)",
    )
    parser.add_argument(
        "--skills-dir",
        default=str(cfg.skills_dir),
        help="Live skills dir (default: ctx_config.cfg.skills_dir)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON (useful for automation/UI integration)",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    slugs = _split_slugs(args)
    if not slugs:
        parser.print_help()
        sys.exit(2)

    wiki_dir = Path(os.path.expanduser(args.wiki_dir))
    skills_dir = Path(os.path.expanduser(args.skills_dir))

    # De-dup while preserving order so --slug fastapi-pro --slugs "fastapi-pro,x"
    # doesn't double-install fastapi-pro.
    seen: set[str] = set()
    uniq_slugs: list[str] = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            uniq_slugs.append(s)

    results: list[InstallResult] = []
    for slug in uniq_slugs:
        result = install_skill(
            slug,
            wiki_dir=wiki_dir,
            skills_dir=skills_dir,
            prefer=args.prefer,
            force=args.force,
            dry_run=args.dry_run,
        )
        results.append(result)

    if args.json:
        payload = [
            {
                "slug": r.slug, "status": r.status,
                "installed_path": r.installed_path,
                "source_variant": r.source_variant,
                "references_copied": r.references_copied,
                "message": r.message,
            }
            for r in results
        ]
        print(json.dumps(payload, indent=2))
    else:
        for r in results:
            tag = "[OK]" if r.status == "installed" else f"[{r.status.upper()}]"
            extra = f" refs={r.references_copied}" if r.references_copied else ""
            variant = f" ({r.source_variant})" if r.source_variant else ""
            msg = f" -- {r.message}" if r.message else ""
            print(f"{tag} {r.slug}{variant}{extra}{msg}")

    # Exit 1 if any install actually failed (not-in-wiki or hard error).
    # Skipped-existing is NOT a failure — idempotent reruns should exit 0.
    failures = [r for r in results if r.status in ("failed", "not-in-wiki")]
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
