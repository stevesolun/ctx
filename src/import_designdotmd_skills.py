#!/usr/bin/env python3
"""import_designdotmd_skills.py -- Deploy designdotmd.directory designs as skills.

Reads imported-skills/designdotmd/MANIFEST.json. Each entry creates
``~/.claude/skills/designdotmd-<slug>/SKILL.md`` with:

  * The upstream YAML frontmatter (name, description, colors, typography,
    spacing, components, etc.) preserved verbatim.
  * A ``tags:`` field injected if missing (the upstream .md doesn't carry
    tags, but the listing API does — they're loaded from MANIFEST.json).
  * An attribution HTML comment prepended above the frontmatter so the
    upstream URL is visible inline.

Idempotent. Re-running updates existing deployments in place.

Usage:
    python src/import_designdotmd_skills.py --dry-run
    python src/import_designdotmd_skills.py --install
    python src/import_designdotmd_skills.py --install --target ./custom-skills-dir
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from ctx_config import cfg

REPO_ROOT = Path(__file__).resolve().parent.parent
IMPORT_ROOT = REPO_ROOT / "imported-skills" / "designdotmd"
MANIFEST_PATH = IMPORT_ROOT / "MANIFEST.json"

_SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        print(f"Manifest not found: {MANIFEST_PATH}", file=sys.stderr)
        print("Run: python imported-skills/designdotmd/build_manifest.py", file=sys.stderr)
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


def _render_attribution(manifest: dict, entry: dict) -> str:
    return (
        f"<!-- designdotmd-import: upstream={manifest['upstream']} "
        f"id={entry['slug']} fetched={manifest['fetched_on']} "
        f"author={entry.get('author', '?')} -->\n"
    )


_FM_OPEN_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _has_top_level_tags(fm_text: str) -> bool:
    """True if the frontmatter has a ``tags:`` line at the top level
    (not nested under typography or other keys).

    The upstream design files are richly indented YAML; we only need to
    avoid double-injecting the tags block. Top-level keys are unindented.
    """
    for raw in fm_text.splitlines():
        if raw.startswith("tags:") or raw.startswith("tags ") or raw == "tags":
            return True
    return False


def _inject_tags(text: str, tags: list[str]) -> str:
    """Insert a ``tags: [...]`` line into the YAML frontmatter.

    Inserts after the ``description:`` line when present (keeps the
    common name/description/tags ordering Claude Code's skill loader
    expects); otherwise appends just before the closing ``---``.

    No-op when the frontmatter already has top-level tags.
    """
    if not tags:
        return text
    m = _FM_OPEN_RE.match(text)
    if not m:
        return text
    fm = m.group(1)
    if _has_top_level_tags(fm):
        return text

    tags_line = "tags: [" + ", ".join(tags) + "]"
    lines = fm.splitlines()
    insert_at = None
    for i, raw in enumerate(lines):
        if raw.startswith("description:") or raw.startswith("description "):
            insert_at = i + 1
            break
    if insert_at is None:
        # No description? insert at end of frontmatter
        insert_at = len(lines)
    new_lines = lines[:insert_at] + [tags_line] + lines[insert_at:]
    new_fm = "\n".join(new_lines)
    return text[:m.start(1)] + new_fm + text[m.end(1):]


def deploy_entry(
    entry: dict, manifest: dict, target_dir: Path, dry_run: bool,
) -> tuple[Path, bool]:
    slug = _validate("slug", entry.get("slug"), regex=_SAFE_SLUG_RE)
    source_rel = _validate("source_path", entry.get("source_path"))
    source = _resolve_within(IMPORT_ROOT, source_rel, field="source_path")
    if not source.exists():
        raise FileNotFoundError(f"Source design missing: {source}")

    tags = entry.get("tags", []) or []
    if not isinstance(tags, list):
        raise ValueError(f"{slug}: tags must be a list, got {type(tags).__name__}")
    tags = [str(t).strip().lower() for t in tags if str(t).strip()]

    skill_dir = target_dir / f"designdotmd-{slug}"
    dest_resolved = skill_dir.resolve()
    target_resolved = target_dir.resolve()
    try:
        dest_resolved.relative_to(target_resolved)
    except ValueError as exc:
        raise ValueError(f"skill dir {skill_dir} resolves outside target_dir") from exc

    dest_skill = skill_dir / "SKILL.md"
    body = source.read_text(encoding="utf-8")
    # Strip a prior attribution header if present so the rewrite is idempotent.
    if body.startswith("<!-- designdotmd-import:"):
        body = body.split("-->", 1)[1].lstrip("\n")
    body = _inject_tags(body, tags)
    content = _render_attribution(manifest, entry) + body

    changed = True
    if dest_skill.exists():
        changed = dest_skill.read_text(encoding="utf-8") != content

    if not dry_run and changed:
        skill_dir.mkdir(parents=True, exist_ok=True)
        dest_skill.write_text(content, encoding="utf-8")

    return dest_skill, changed


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
        dest, changed = deploy_entry(entry, manifest, target_dir, dry_run=args.dry_run)
        marker = "UPD" if changed else "   "
        if changed:
            new_or_updated += 1
        else:
            unchanged += 1
        # Quiet mode after first 5 to avoid 156-line output noise
        if changed and (new_or_updated <= 5 or new_or_updated == len(manifest["entries"])):
            print(f"  [{marker}] {dest.relative_to(target_dir.parent)}")
        elif new_or_updated == 6:
            print(f"  ... ({len(manifest['entries']) - 5} more) ...")

    mode = "dry-run" if args.dry_run else "install"
    print()
    print(f"Mode: {mode}  target: {target_dir}")
    print(f"Entries: {len(manifest['entries'])}  new/updated: {new_or_updated}  unchanged: {unchanged}")
    if args.install:
        print()
        print("Next steps:")
        print("  python src/catalog_builder.py")
        print("  python src/wiki_batch_entities.py --all")
        print("  python -m ctx.core.wiki.wiki_graphify")


if __name__ == "__main__":
    main()
