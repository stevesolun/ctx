#!/usr/bin/env python3
"""
agent_install.py -- Install an agent from the wiki into the live agents directory.

Companion to ``skill_install.py``. The wiki source lives at
``<wiki>/converted-agents/<slug>.md`` (populated by ``agent_mirror``)
and the live target is ``~/.claude/agents/<slug>.md``.

Agents are single-file by convention: no pipeline references, no multi-
stage structure. The install is therefore a one-file copy plus:

  - manifest bump (``load`` list in ``~/.claude/skill-manifest.json``,
    reusing the same manifest that tracks skills — manifest entries
    carry an ``entity_type`` field so we can distinguish)
  - wiki entity ``status`` frontmatter flipped to ``installed``
  - telemetry ``load`` event emitted

Usage:
    ctx-agent-install --slug accessibility-expert
    ctx-agent-install --slugs "accessibility-expert,architect"
    ctx-agent-install --slug architect --force
    ctx-agent-install --slug architect --dry-run
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

from _fs_utils import atomic_write_text as _atomic_write_text
from ctx_config import cfg
from wiki_utils import validate_skill_name

_logger = logging.getLogger(__name__)

_SESSION_ID: str = uuid.uuid4().hex
MANIFEST_PATH = Path(os.path.expanduser("~/.claude/skill-manifest.json"))


@dataclass(frozen=True)
class InstallResult:
    slug: str
    status: str  # "installed" | "skipped-existing" | "not-in-wiki" | "failed"
    installed_path: str | None
    message: str = ""


# ── Wiki lookups ─────────────────────────────────────────────────────────────


def _entity_path(wiki_dir: Path, slug: str) -> Path:
    return wiki_dir / "entities" / "agents" / f"{slug}.md"


def _mirror_path(wiki_dir: Path, slug: str) -> Path:
    return wiki_dir / "converted-agents" / f"{slug}.md"


# ── Manifest + frontmatter updates ───────────────────────────────────────────


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"load": [], "unload": [], "warnings": []}
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"load": [], "unload": [], "warnings": []}
    data.setdefault("load", [])
    data.setdefault("unload", [])
    data.setdefault("warnings", [])
    return data


def _save_manifest(manifest: dict) -> None:
    _atomic_write_text(MANIFEST_PATH, json.dumps(manifest, indent=2))


def _record_in_manifest(slug: str, source: str = "ctx-agent-install") -> None:
    """Idempotent manifest update; mirrors skill_install._record_in_manifest."""
    manifest = _load_manifest()
    loaded = {(e.get("skill"), e.get("entity_type")) for e in manifest["load"]}
    if (slug, "agent") not in loaded:
        manifest["load"].append({
            "skill": slug,
            "entity_type": "agent",
            "source": source,
        })
    # Drop any unload entry for this agent so a reinstall clears the flag.
    manifest["unload"] = [
        e for e in manifest["unload"]
        if not (e.get("skill") == slug and e.get("entity_type", "skill") == "agent")
    ]
    _save_manifest(manifest)


def _bump_entity_status(wiki_dir: Path, slug: str) -> bool:
    """Flip entity card status to ``installed`` (or insert if absent)."""
    import re  # local: one-shot use

    entity = _entity_path(wiki_dir, slug)
    if not entity.is_file():
        return False
    text = entity.read_text(encoding="utf-8", errors="replace")
    new_text, count = re.subn(
        r"^status:\s*.+$", "status: installed", text, count=1, flags=re.MULTILINE
    )
    if count == 0:
        new_text = re.sub(r"(---\n)", r"\1status: installed\n", text, count=1)
    if new_text != text:
        _atomic_write_text(entity, new_text)
        return True
    return False


def _emit_install_event(slug: str) -> None:
    try:
        import skill_telemetry

        skill_telemetry.log_event("load", slug, _SESSION_ID)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("agent_install: telemetry write failed for %r: %s", slug, exc)


# ── Core install ─────────────────────────────────────────────────────────────


def install_agent(
    slug: str,
    *,
    wiki_dir: Path,
    agents_dir: Path,
    force: bool = False,
    dry_run: bool = False,
) -> InstallResult:
    """Install one agent from the wiki mirror into the live agents dir."""
    try:
        validate_skill_name(slug)
    except ValueError as exc:
        return InstallResult(
            slug=slug, status="failed", installed_path=None,
            message=f"invalid slug: {exc}",
        )

    source = _mirror_path(wiki_dir, slug)
    if not source.is_file():
        return InstallResult(
            slug=slug, status="not-in-wiki", installed_path=None,
            message=(
                f"no mirrored body at {source}. "
                "Run ctx-agent-mirror first to populate."
            ),
        )

    dest = agents_dir / f"{slug}.md"
    if dest.exists() and not force:
        if not dry_run:
            _record_in_manifest(slug)
            _bump_entity_status(wiki_dir, slug)
        return InstallResult(
            slug=slug, status="skipped-existing", installed_path=str(dest),
            message="already installed; pass --force to overwrite",
        )

    if dry_run:
        return InstallResult(
            slug=slug, status="installed", installed_path=str(dest),
            message="dry-run: no files written",
        )

    agents_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)

    _record_in_manifest(slug)
    _bump_entity_status(wiki_dir, slug)
    _emit_install_event(slug)

    return InstallResult(slug=slug, status="installed", installed_path=str(dest))


# ── CLI ──────────────────────────────────────────────────────────────────────


def _split_slugs(args: argparse.Namespace) -> list[str]:
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
        prog="ctx-agent-install",
        description=(
            "Install an agent from the wiki into ~/.claude/agents/. "
            "Source: <wiki>/converted-agents/<slug>.md (run "
            "ctx-agent-mirror once to populate that dir from the live "
            "agents if you haven't yet)."
        ),
    )
    parser.add_argument("slugs_positional", nargs="*", help="Slugs to install")
    parser.add_argument("--slug", help="Single agent slug")
    parser.add_argument("--slugs", help="Comma-separated slugs")
    parser.add_argument("--force", action="store_true", help="Overwrite existing agent")
    parser.add_argument("--dry-run", action="store_true", help="Print intent only")
    parser.add_argument(
        "--wiki-dir", default=str(cfg.wiki_dir),
        help="Wiki root (default: cfg.wiki_dir)",
    )
    parser.add_argument(
        "--agents-dir", default=str(cfg.agents_dir),
        help="Live agents dir (default: cfg.agents_dir)",
    )
    parser.add_argument("--json", action="store_true", help="Emit results as JSON")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    slugs = _split_slugs(args)
    if not slugs:
        parser.print_help()
        sys.exit(2)

    wiki_dir = Path(os.path.expanduser(args.wiki_dir))
    agents_dir = Path(os.path.expanduser(args.agents_dir))

    seen: set[str] = set()
    uniq: list[str] = []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            uniq.append(s)

    results: list[InstallResult] = []
    for slug in uniq:
        results.append(install_agent(
            slug, wiki_dir=wiki_dir, agents_dir=agents_dir,
            force=args.force, dry_run=args.dry_run,
        ))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        for r in results:
            tag = "[OK]" if r.status == "installed" else f"[{r.status.upper()}]"
            msg = f" -- {r.message}" if r.message else ""
            print(f"{tag} {r.slug}{msg}")

    failures = [r for r in results if r.status in ("failed", "not-in-wiki")]
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
