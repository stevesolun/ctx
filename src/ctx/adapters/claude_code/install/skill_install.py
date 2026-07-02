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
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from ctx_config import cfg
from ctx.adapters.claude_code.install.install_utils import (
    bump_entity_status,
    emit_load_event,
    record_install,
    safe_copy_file,
)
from ctx.adapters.claude_code.install.skillspector_scan import SkillSpectorResult
from ctx.adapters.claude_code.install.skillspector_scan import run_skillspector_scan
from ctx.core.wiki.wiki_utils import validate_skill_name

_logger = logging.getLogger(__name__)

# Stable session ID so telemetry can correlate a multi-slug install call.
_SESSION_ID: str = uuid.uuid4().hex


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a single install. One per slug."""

    slug: str
    status: str  # "installed" | "would-install" | "skipped-existing"
    # | "not-in-wiki" | "failed"
    installed_path: str | None
    source_variant: str | None  # "transformed" | "original" | None
    references_copied: int
    message: str = ""
    security_scan: SkillSpectorResult | None = None


# ── Wiki lookups ─────────────────────────────────────────────────────────────


def _entity_path(wiki_dir: Path, slug: str) -> Path:
    """Return the expected entity-card path for ``slug``."""
    return wiki_dir / "entities" / "skills" / f"{slug}.md"


def _converted_dir(wiki_dir: Path, slug: str) -> Path:
    """Return the expected converted-content dir for ``slug``."""
    return wiki_dir / "converted" / slug


def _pick_source(converted: Path, prefer: str) -> tuple[Path | None, str | None]:
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


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def _ensure_micro_converted(
    converted: Path,
    source: Path,
    variant: str,
) -> tuple[Path | None, str | None, str]:
    """Return a source safe to install, converting long raw bodies first."""
    if source.is_symlink():
        return None, None, f"unsafe symlinked wiki source at {source}"
    lines = _line_count(source)
    if lines <= cfg.line_threshold:
        return source, variant, ""
    try:
        from batch_convert import convert_skill

        result = convert_skill(source, output_dir=converted)
    except Exception as exc:  # noqa: BLE001 - install should return a structured failure.
        return None, None, f"micro-skill conversion failed: {exc}"
    if result.get("status") != "converted":
        return (
            None,
            None,
            (
                "micro-skill conversion did not complete: "
                f"{result.get('reason') or result.get('status')}"
            ),
        )
    transformed = converted / "SKILL.md"
    if not transformed.is_file():
        return None, None, "micro-skill conversion produced no SKILL.md"
    return transformed, "transformed", ""


# ── Copy logic ───────────────────────────────────────────────────────────────


_BUNDLE_DIR_NAMES = ("references", "reference", "resources", "scripts", "assets")


def _iter_bundle_files(src_dir: Path) -> list[tuple[Path, Path]]:
    """Return ``(source, relative_destination)`` pairs for skill bundle files."""
    files: list[tuple[Path, Path]] = []
    for dirname in _BUNDLE_DIR_NAMES:
        bundle_dir = src_dir / dirname
        if not bundle_dir.is_dir():
            continue
        for source in sorted(path for path in bundle_dir.rglob("*") if path.is_file()):
            files.append((source, source.relative_to(src_dir)))
    return files


def _find_symlink_in_tree(root: Path) -> Path | None:
    """Return the first symlink under root, including root itself."""
    candidates = [root]
    try:
        candidates.extend(root.rglob("*"))
    except OSError:
        return root
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                return candidate
        except OSError:
            return candidate
    return None


def _copy_bundle_files(src_dir: Path, dest_dir: Path) -> int:
    """Copy bundled references/resources/scripts/assets into an install dir."""
    copied = 0
    for source, relative in _iter_bundle_files(src_dir):
        safe_copy_file(source, dest_dir / relative, dest_root=dest_dir)
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
    security_scan: bool = False,
    security_scan_required: bool = False,
    security_scan_use_llm: bool = False,
    security_scan_command: list[str] | None = None,
    skillspector_bin: str | None = None,
    security_scan_timeout: int = 120,
) -> InstallResult:
    """Install one skill from the wiki into the live skills directory.

    The install is:

      1. Validated (slug passes ``validate_skill_name``).
      2. Sourced from the wiki (``converted/<slug>/SKILL.md``, with
         ``.original`` as fallback).
      3. Copied to ``<skills_dir>/<slug>/SKILL.md`` plus bundled resources.
      4. Mirrored into the skill manifest and the wiki entity's status
         frontmatter.

    ``dry_run=True`` skips the copy + state updates; everything else is
    evaluated so the caller sees what would happen.
    """
    try:
        validate_skill_name(slug)
    except ValueError as exc:
        return InstallResult(
            slug=slug,
            status="failed",
            installed_path=None,
            source_variant=None,
            references_copied=0,
            message=f"invalid slug: {exc}",
        )

    converted = _converted_dir(wiki_dir, slug)
    if converted.is_symlink():
        return InstallResult(
            slug=slug,
            status="failed",
            installed_path=None,
            source_variant=None,
            references_copied=0,
            message=f"unsafe symlinked wiki content at {converted}",
        )
    if not converted.is_dir():
        return InstallResult(
            slug=slug,
            status="not-in-wiki",
            installed_path=None,
            source_variant=None,
            references_copied=0,
            message=f"no wiki content at {converted}",
        )

    source, variant = _pick_source(converted, prefer)
    if source is None:
        return InstallResult(
            slug=slug,
            status="not-in-wiki",
            installed_path=None,
            source_variant=None,
            references_copied=0,
            message="wiki has no SKILL.md or SKILL.md.original",
        )
    assert variant is not None
    unsafe_symlink = _find_symlink_in_tree(converted)
    if unsafe_symlink is not None:
        return InstallResult(
            slug=slug,
            status="failed",
            installed_path=None,
            source_variant=variant,
            references_copied=0,
            message=f"unsafe symlinked wiki bundle at {unsafe_symlink}",
        )

    dest_dir = skills_dir / slug
    dest = dest_dir / "SKILL.md"

    if dest.exists() and not force:
        scan_result = None
        if security_scan:
            scan_result = run_skillspector_scan(
                converted,
                command=security_scan_command,
                binary=skillspector_bin,
                use_llm=security_scan_use_llm,
                timeout_seconds=security_scan_timeout,
            )
            if security_scan_required and scan_result.status != "passed":
                return InstallResult(
                    slug=slug,
                    status="failed",
                    installed_path=None,
                    source_variant=variant,
                    references_copied=0,
                    message=(f"SkillSpector security scan did not pass: {scan_result.status}"),
                    security_scan=scan_result,
                )
        # Already installed. Still refresh manifest/status so an earlier
        # install that didn't record into manifest gets reconciled.
        if not dry_run:
            record_install(
                slug,
                entity_type="skill",
                source="ctx-skill-install",
            )
            bump_entity_status(_entity_path(wiki_dir, slug), status="installed")
        return InstallResult(
            slug=slug,
            status="skipped-existing",
            installed_path=str(dest),
            source_variant=variant,
            references_copied=0,
            message=(
                "already installed; pass --force to overwrite"
                if scan_result is None
                else (
                    "already installed; pass --force to overwrite; "
                    f"SkillSpector: {scan_result.status}"
                )
            ),
            security_scan=scan_result,
        )

    if dry_run:
        refs_count = len(_iter_bundle_files(converted))
        message = "dry-run: no files written"
        try:
            if _line_count(source) > cfg.line_threshold:
                message = "dry-run: would micro-convert before install"
        except OSError:
            pass
        scan_result = None
        if security_scan:
            scan_result = run_skillspector_scan(
                converted,
                command=security_scan_command,
                binary=skillspector_bin,
                use_llm=security_scan_use_llm,
                timeout_seconds=security_scan_timeout,
            )
            message = f"{message}; SkillSpector: {scan_result.status}"
        return InstallResult(
            slug=slug,
            status="would-install",
            installed_path=str(dest),
            source_variant=variant,
            references_copied=refs_count,
            message=message,
            security_scan=scan_result,
        )

    source, variant, conversion_error = _ensure_micro_converted(
        converted,
        source,
        variant,
    )
    if source is None:
        return InstallResult(
            slug=slug,
            status="failed",
            installed_path=None,
            source_variant=variant,
            references_copied=0,
            message=conversion_error,
        )

    scan_result = None
    if security_scan:
        scan_result = run_skillspector_scan(
            converted,
            command=security_scan_command,
            binary=skillspector_bin,
            use_llm=security_scan_use_llm,
            timeout_seconds=security_scan_timeout,
        )
        if security_scan_required and scan_result.status != "passed":
            return InstallResult(
                slug=slug,
                status="failed",
                installed_path=None,
                source_variant=variant,
                references_copied=0,
                message=f"SkillSpector security scan did not pass: {scan_result.status}",
                security_scan=scan_result,
            )

    try:
        safe_copy_file(source, dest, dest_root=skills_dir)
        refs_copied = _copy_bundle_files(converted, dest_dir)
    except (OSError, ValueError) as exc:
        return InstallResult(
            slug=slug,
            status="failed",
            installed_path=None,
            source_variant=variant,
            references_copied=0,
            message=str(exc),
        )

    record_install(slug, entity_type="skill", source="ctx-skill-install")
    bump_entity_status(_entity_path(wiki_dir, slug), status="installed")
    emit_load_event(slug, _SESSION_ID)

    return InstallResult(
        slug=slug,
        status="installed",
        installed_path=str(dest),
        source_variant=variant,
        references_copied=refs_copied,
        message=(f"SkillSpector: {scan_result.status}" if scan_result is not None else ""),
        security_scan=scan_result,
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
    parser.add_argument(
        "--security-scan",
        action="store_true",
        help="Run SkillSpector before install and include its report in output",
    )
    parser.add_argument(
        "--security-scan-required",
        action="store_true",
        help="Fail the install unless SkillSpector exits cleanly",
    )
    parser.add_argument(
        "--security-scan-llm",
        action="store_true",
        help="Allow SkillSpector LLM analysis instead of static-only --no-llm",
    )
    parser.add_argument(
        "--skillspector-bin",
        help=(
            "SkillSpector executable. Defaults to CTX_SKILLSPECTOR_BIN or 'skillspector' on PATH."
        ),
    )
    parser.add_argument(
        "--security-scan-timeout",
        type=int,
        default=120,
        help="SkillSpector timeout in seconds (default: 120)",
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
            security_scan=args.security_scan or args.security_scan_required,
            security_scan_required=args.security_scan_required,
            security_scan_use_llm=args.security_scan_llm,
            skillspector_bin=args.skillspector_bin,
            security_scan_timeout=args.security_scan_timeout,
        )
        results.append(result)

    if args.json:
        payload = [
            {
                "slug": r.slug,
                "status": r.status,
                "installed_path": r.installed_path,
                "source_variant": r.source_variant,
                "references_copied": r.references_copied,
                "message": r.message,
                "security_scan": (
                    r.security_scan.to_json() if r.security_scan is not None else None
                ),
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
            if r.security_scan is not None:
                print("  SkillSpector report:")
                if r.security_scan.output:
                    for line in r.security_scan.output.splitlines():
                        print(f"    {line}")
                else:
                    print("    <no output>")

    # Exit 1 if any install actually failed (not-in-wiki or hard error).
    # Skipped-existing is NOT a failure — idempotent reruns should exit 0.
    failures = [r for r in results if r.status in ("failed", "not-in-wiki")]
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
