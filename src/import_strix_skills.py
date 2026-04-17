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

sys.path.insert(0, str(Path(__file__).parent))
from ctx_config import cfg  # noqa: E402

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


def deploy_entry(entry: dict, manifest: dict, target_dir: Path, dry_run: bool) -> tuple[Path, bool]:
    source = IMPORT_ROOT / entry["source_path"]
    if not source.exists():
        raise FileNotFoundError(f"Source skill missing: {source}")

    dir_name = f"strix-{entry['category']}-{slugify(entry['name'])}"
    skill_dir = target_dir / dir_name
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
        print(f"  python src/wiki_batch_entities.py --all")
        print(f"  python src/wiki_graphify.py")


if __name__ == "__main__":
    main()
