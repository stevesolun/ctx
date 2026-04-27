#!/usr/bin/env python3
"""import_mattpocock_skills.py -- Deploy mattpocock/skills into ~/.claude/skills.

Reads imported-skills/mattpocock/MANIFEST.json. Each entry creates a directory
named ``mattpocock-<slug>``, copies SKILL.md (with attribution header
prepended), and copies any support files (ADR-FORMAT.md, deep-modules.md,
scripts/, etc.) verbatim.

Idempotent. Safe to re-run.

Usage:
    python src/import_mattpocock_skills.py --dry-run
    python src/import_mattpocock_skills.py --install
    python src/import_mattpocock_skills.py --install --target ./custom-skills-dir
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from ctx_config import cfg

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORT_ROOT = REPO_ROOT / "imported-skills" / "mattpocock"
MANIFEST_PATH = IMPORT_ROOT / "MANIFEST.json"

_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        print(f"Manifest not found: {MANIFEST_PATH}", file=sys.stderr)
        print("Run: python imported-skills/mattpocock/build_manifest.py", file=sys.stderr)
        sys.exit(1)
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _validate(field: str, value: object, *, regex: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: expected non-empty string, got {type(value).__name__}")
    if regex is not None and not regex.match(value):
        raise ValueError(f"{field}: {value!r} failed strict format check")
    return value


def _resolve_within(root: Path, candidate_rel: str, *, field: str) -> Path:
    if ".." in Path(candidate_rel).parts or candidate_rel.startswith(("/", "\\")):
        raise ValueError(f"{field}: path traversal denied in {candidate_rel!r}")
    resolved = (root / candidate_rel).resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"{field}: {candidate_rel!r} resolves outside import root") from exc
    return resolved


def render_attribution_header(manifest: dict) -> str:
    return (
        f"<!-- mattpocock-import: upstream={manifest['upstream']} "
        f"rev={manifest['upstream_revision'][:12]} "
        f"license={manifest['license']} -->\n"
    )


def deploy_entry(entry: dict, manifest: dict, target_dir: Path, dry_run: bool) -> tuple[Path, bool, list[Path]]:
    slug = _validate("slug", entry.get("slug"), regex=_SAFE_SLUG_RE)
    source_path_raw = _validate("source_path", entry.get("source_path"))
    source = _resolve_within(IMPORT_ROOT, source_path_raw, field="source_path")
    if not source.exists():
        raise FileNotFoundError(f"Source skill missing: {source}")
    source_dir = source.parent

    skill_dir = target_dir / f"mattpocock-{slug}"
    dest_resolved = skill_dir.resolve()
    target_resolved = target_dir.resolve()
    try:
        dest_resolved.relative_to(target_resolved)
    except ValueError as exc:
        raise ValueError(f"skill dir {skill_dir} resolves outside target_dir") from exc

    dest_skill = skill_dir / "SKILL.md"
    header = render_attribution_header(manifest)
    body = source.read_text(encoding="utf-8")
    if body.startswith("<!-- mattpocock-import:"):
        body = body.split("-->", 1)[1].lstrip("\n")
    content = header + body

    changed = True
    if dest_skill.exists():
        existing = dest_skill.read_text(encoding="utf-8")
        changed = existing != content

    support_paths: list[Path] = []
    for rel in entry.get("support_files", []):
        # Validate each support file rel-path against the source dir.
        sp = _resolve_within(source_dir, rel, field="support_files")
        if sp.is_file():
            support_paths.append(sp)

    if not dry_run:
        if changed:
            skill_dir.mkdir(parents=True, exist_ok=True)
            dest_skill.write_text(content, encoding="utf-8")
        for sp in support_paths:
            rel = sp.relative_to(source_dir)
            dest_support = skill_dir / rel
            dest_support.parent.mkdir(parents=True, exist_ok=True)
            if not dest_support.exists() or dest_support.read_bytes() != sp.read_bytes():
                shutil.copy2(sp, dest_support)
                changed = True

    return dest_skill, changed, support_paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--target",
        default=str(cfg.skills_dir),
        help=f"Target skills dir (default: {cfg.skills_dir})",
    )
    args = parser.parse_args()
    if not args.install and not args.dry_run:
        parser.error("Pass either --install or --dry-run")

    manifest = load_manifest()
    target_dir = Path(args.target).expanduser()
    if args.install and not target_dir.exists():
        print(f"Creating target dir: {target_dir}")
        target_dir.mkdir(parents=True, exist_ok=True)

    new_or_updated = 0
    unchanged = 0
    for entry in manifest["entries"]:
        dest, changed, support = deploy_entry(entry, manifest, target_dir, dry_run=args.dry_run)
        marker = "UPD" if changed else "   "
        if changed:
            new_or_updated += 1
        else:
            unchanged += 1
        suffix = f"  (+{len(support)} support)" if support else ""
        print(f"  [{marker}] {dest.relative_to(target_dir.parent)}{suffix}")

    mode = "dry-run" if args.dry_run else "install"
    print()
    print(f"Mode: {mode}  target: {target_dir}")
    print(f"Entries: {len(manifest['entries'])}  new/updated: {new_or_updated}  unchanged: {unchanged}")
    if args.install:
        print()
        print("Next steps:")
        print(f"  ctx-catalog-builder --wiki {cfg.wiki_dir} --skills-dir {target_dir} \\")
        print(f"      --agents-dir {cfg.agents_dir}")
        print("  ctx-wiki-batch-entities --all")
        print("  ctx-wiki-graphify")


if __name__ == "__main__":
    main()
