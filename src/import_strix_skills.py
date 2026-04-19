#!/usr/bin/env python3
"""import_strix_skills.py -- Deploy imported Strix skills into ~/.claude/skills.

Reads imported-skills/strix/MANIFEST.json and creates one skill directory per
entry in `cfg.skills_dir`, following the naming convention:

    <skills_dir>/strix-<category>-<slug>/SKILL.md

Each deployed SKILL.md prepends an attribution header so provenance remains
visible inline when the skill is loaded.

This script is idempotent. Re-running updates existing deployments in place.

Usage:
    python src/import_strix_skills.py --dry-run        # preview
    python src/import_strix_skills.py --install        # deploy to ~/.claude/skills
    python src/import_strix_skills.py --install \\
        --target ./custom-skills-dir                   # deploy elsewhere
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ctx_config import cfg

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORT_ROOT = REPO_ROOT / "imported-skills" / "strix"
MANIFEST_PATH = IMPORT_ROOT / "MANIFEST.json"

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return SLUG_RE.sub("-", name.lower()).strip("-")


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        print(f"Manifest not found: {MANIFEST_PATH}", file=sys.stderr)
        print("Run: python imported-skills/strix/build_manifest.py", file=sys.stderr)
        sys.exit(1)
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def render_attribution_header(entry: dict, manifest: dict) -> str:
    return (
        f"<!-- strix-import: upstream={manifest['upstream']} "
        f"rev={manifest['upstream_revision'][:12]} "
        f"license={manifest['license']} category={entry['category']} -->\n"
    )


_SAFE_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _validate_manifest_field(field: str, value: object, *, regex: re.Pattern[str] | None = None) -> str:
    """Reject manifest values that could escape the intended trust boundary."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field}: expected non-empty string, got {type(value).__name__}")
    if regex is not None and not regex.match(value):
        raise ValueError(f"{field}: {value!r} failed strict format check")
    return value


def _resolve_within(root: Path, candidate_rel: str, *, field: str) -> Path:
    """Join ``candidate_rel`` onto ``root`` and fail hard if the result escapes root.

    Strix finding vuln-0001 (Path Traversal in Strix Skill Import): the
    manifest's ``source_path`` was concatenated directly onto IMPORT_ROOT,
    so a crafted value like ``../../etc/passwd`` would be happily read
    and re-written into the target skills tree. Resolve both sides and
    enforce ``relative_to`` containment before we touch the filesystem.
    """
    if ".." in Path(candidate_rel).parts or candidate_rel.startswith(("/", "\\")):
        raise ValueError(f"{field}: path traversal denied in {candidate_rel!r}")
    resolved = (root / candidate_rel).resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"{field}: {candidate_rel!r} resolves outside import root"
        ) from exc
    return resolved


def deploy_entry(entry: dict, manifest: dict, target_dir: Path, dry_run: bool) -> tuple[Path, bool]:
    # Manifest fields are untrusted input (the repo's imported-skills/
    # MANIFEST.json is checked-in today, but the path from parsing to
    # filesystem write must still be defensible). Validate category
    # against a strict allowlist, contain source_path inside IMPORT_ROOT.
    category = _validate_manifest_field("category", entry.get("category"), regex=_SAFE_CATEGORY_RE)
    source_path_raw = _validate_manifest_field("source_path", entry.get("source_path"))
    source = _resolve_within(IMPORT_ROOT, source_path_raw, field="source_path")

    if not source.exists():
        raise FileNotFoundError(f"Source skill missing: {source}")

    dir_name = f"strix-{category}-{slugify(entry['name'])}"
    skill_dir = target_dir / dir_name
    # Same containment check on the destination — dir_name is built from
    # validated inputs but slugify() on entry['name'] is defensive too.
    dest_resolved = skill_dir.resolve()
    target_resolved = target_dir.resolve()
    try:
        dest_resolved.relative_to(target_resolved)
    except ValueError as exc:
        raise ValueError(
            f"skill dir {skill_dir} resolves outside target_dir"
        ) from exc
    dest = skill_dir / "SKILL.md"

    header = render_attribution_header(entry, manifest)
    body = source.read_text(encoding="utf-8")
    if body.startswith("<!-- strix-import:"):
        body = body.split("-->", 1)[1].lstrip("\n")
    content = header + body

    changed = True
    if dest.exists():
        existing = dest.read_text(encoding="utf-8")
        changed = existing != content

    if not dry_run and changed:
        skill_dir.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")

    return dest, changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="Write to target dir")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
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

    created = updated = unchanged = 0
    for entry in manifest["entries"]:
        dest, changed = deploy_entry(entry, manifest, target_dir, dry_run=args.dry_run)
        if changed:
            if dest.exists() and not args.dry_run:
                updated += 1
                marker = "UPD"
            else:
                created += 1
                marker = "NEW"
        else:
            unchanged += 1
            marker = "   "
        print(f"  [{marker}] {dest.relative_to(target_dir.parent)}")

    mode = "dry-run" if args.dry_run else "install"
    print()
    print(f"Mode: {mode}  target: {target_dir}")
    print(f"Entries: {len(manifest['entries'])}  new/updated: {created + updated}  unchanged: {unchanged}")

    if args.install:
        print()
        print("Next steps:")
        print(f"  python src/catalog_builder.py --wiki {cfg.wiki_dir} --skills-dir {target_dir} \\")
        print(f"      --agents-dir {cfg.agents_dir}")
        print("  python src/wiki_batch_entities.py --all")
        print("  python src/wiki_graphify.py")


if __name__ == "__main__":
    main()
