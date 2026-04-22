#!/usr/bin/env python3
"""
agent_mirror.py -- Mirror locally-installed agent bodies into the wiki.

Context: ``entities/agents/<slug>.md`` in the wiki is a metadata card —
title, tags, quality, backlinks — but NOT the agent's prompt body.
The live body lives at ``~/.claude/agents/<slug>.md`` which is absent
from the wiki. That breaks portability: a fresh user cloning the repo
has the card but no agent to install.

This module scans ``~/.claude/agents/*.md``, keeps files that expose a
Claude-Code agent frontmatter (``name:`` + ``description:``), and
writes each body verbatim to ``<wiki>/converted-agents/<slug>.md`` —
a parallel structure to ``<wiki>/converted/<slug>/`` which already
holds the canonical skill bodies.

The CLI is a one-shot admin operation. ``ctx-agent-install`` consumes
the mirrored files at install time.

Usage:
    ctx-agent-mirror                     # mirror everything that changed
    ctx-agent-mirror --slug X            # mirror one slug
    ctx-agent-mirror --force             # overwrite even unchanged bodies
    ctx-agent-mirror --prune             # delete mirrored bodies whose
                                         # live source has disappeared
    ctx-agent-mirror --dry-run           # report without writing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from _fs_utils import atomic_write_text as _atomic_write_text
from ctx_config import cfg
from wiki_utils import validate_skill_name

_logger = logging.getLogger(__name__)

# Files under ~/.claude/agents/ that look like agents but aren't.
# Skill-router ships a skill under ~/.claude/agents/skill-router/*.md with
# stage files (01-scope, 02-plan, BUILDER, REVIEWER, SKILL). Those must
# not be treated as agents by name — they happen to live under the
# agents dir for routing convenience but are pipeline fragments.
_PIPELINE_NAMES: frozenset[str] = frozenset({
    "01-scope", "02-plan", "03-build", "03a-build", "03b-build",
    "04-check", "05-deliver",
    "BUILDER", "REVIEWER", "SKILL", "QUICKSTART", "EXECUTIVE-BRIEF",
})


@dataclass(frozen=True)
class MirrorResult:
    """One outcome per slug."""

    slug: str
    status: str  # "mirrored" | "unchanged" | "skipped-no-frontmatter"
                 # | "skipped-pipeline-fragment" | "pruned" | "not-found"
    source_path: str | None
    dest_path: str | None
    bytes_copied: int = 0
    message: str = ""


# ── Frontmatter sniff ────────────────────────────────────────────────────────


def _has_agent_frontmatter(text: str) -> bool:
    """Return True when *text* opens with a Claude-Code agent frontmatter.

    Minimum viable signal: a ``---`` delimited block at the top whose
    keys include both ``name`` and ``description``. We deliberately
    don't require ``model`` — some agents inherit it implicitly.
    """
    if not text.startswith("---"):
        return False
    # Find the closing delimiter after the first line.
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    head = parts[1]
    # Cheap key check — avoids pulling yaml for a boolean decision.
    has_name = False
    has_desc = False
    for line in head.splitlines():
        if line.startswith("name:"):
            has_name = True
        elif line.startswith("description:"):
            has_desc = True
        if has_name and has_desc:
            return True
    return False


def _looks_like_pipeline_fragment(path: Path) -> bool:
    """True for files like ``BUILDER.md`` that ship inside the agents
    dir for routing convenience but aren't actually agents.

    We check the filename stem against a small allowlist of known
    pipeline fragment names rather than inferring from frontmatter —
    some fragments legitimately have their own frontmatter and we
    don't want them to slip through.
    """
    return path.stem in _PIPELINE_NAMES


# ── Source iteration ─────────────────────────────────────────────────────────


def _iter_agent_files(agents_dir: Path) -> list[Path]:
    """Return every .md file directly inside ``agents_dir`` (non-recursive).

    We intentionally skip nested subdirs because those are skill-router
    pipelines and other routing constructs, not top-level agents. A
    top-level ``~/.claude/agents/<slug>.md`` is the one-agent-per-file
    convention Claude Code enforces.
    """
    if not agents_dir.is_dir():
        return []
    return sorted(p for p in agents_dir.glob("*.md") if p.is_file())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Per-slug mirror ──────────────────────────────────────────────────────────


def mirror_one(
    slug: str,
    *,
    agents_dir: Path,
    wiki_dir: Path,
    force: bool = False,
    dry_run: bool = False,
) -> MirrorResult:
    """Mirror one live agent body into the wiki's converted-agents dir.

    Returns an ``unchanged`` result when the destination already has
    identical content. ``force=True`` rewrites regardless.
    """
    try:
        validate_skill_name(slug)
    except ValueError as exc:
        return MirrorResult(
            slug=slug, status="skipped-no-frontmatter",
            source_path=None, dest_path=None,
            message=f"invalid slug: {exc}",
        )

    source = agents_dir / f"{slug}.md"
    if not source.is_file():
        return MirrorResult(
            slug=slug, status="not-found", source_path=None, dest_path=None,
            message=f"no live agent file at {source}",
        )

    if _looks_like_pipeline_fragment(source):
        return MirrorResult(
            slug=slug, status="skipped-pipeline-fragment",
            source_path=str(source), dest_path=None,
            message="pipeline fragment, not an agent",
        )

    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return MirrorResult(
            slug=slug, status="skipped-no-frontmatter",
            source_path=str(source), dest_path=None,
            message=f"read failed: {exc}",
        )

    if not _has_agent_frontmatter(text):
        return MirrorResult(
            slug=slug, status="skipped-no-frontmatter",
            source_path=str(source), dest_path=None,
            message="file missing name:/description: frontmatter",
        )

    dest_dir = wiki_dir / "converted-agents"
    dest = dest_dir / f"{slug}.md"

    # Change detection via content hash — avoids rewriting 444 files
    # every run when only a handful changed.
    if dest.is_file() and not force:
        try:
            existing = dest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
        if _sha256(existing) == _sha256(text):
            return MirrorResult(
                slug=slug, status="unchanged",
                source_path=str(source), dest_path=str(dest),
                bytes_copied=0,
            )

    if dry_run:
        return MirrorResult(
            slug=slug, status="mirrored",
            source_path=str(source), dest_path=str(dest),
            bytes_copied=len(text.encode("utf-8")),
            message="dry-run: no files written",
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(dest, text)
    return MirrorResult(
        slug=slug, status="mirrored",
        source_path=str(source), dest_path=str(dest),
        bytes_copied=len(text.encode("utf-8")),
    )


# ── Bulk mirror ──────────────────────────────────────────────────────────────


def mirror_all(
    *,
    agents_dir: Path,
    wiki_dir: Path,
    force: bool = False,
    dry_run: bool = False,
) -> list[MirrorResult]:
    """Mirror every eligible agent body. Orders output for stable logs."""
    results: list[MirrorResult] = []
    for path in _iter_agent_files(agents_dir):
        slug = path.stem
        # Fast short-circuit before the expensive read when we can tell
        # from the filename alone that we should skip.
        if _looks_like_pipeline_fragment(path):
            results.append(MirrorResult(
                slug=slug, status="skipped-pipeline-fragment",
                source_path=str(path), dest_path=None,
                message="pipeline fragment, not an agent",
            ))
            continue
        results.append(mirror_one(
            slug, agents_dir=agents_dir, wiki_dir=wiki_dir,
            force=force, dry_run=dry_run,
        ))
    return results


def prune_orphans(
    *,
    agents_dir: Path,
    wiki_dir: Path,
    dry_run: bool = False,
) -> list[MirrorResult]:
    """Delete ``converted-agents/<slug>.md`` whose live source vanished.

    A user who uninstalls an agent locally and runs ``--prune`` gets
    the wiki mirror cleaned up too. Without this, stale mirrored
    bodies would quietly linger and become installable again.
    """
    mirror_dir = wiki_dir / "converted-agents"
    if not mirror_dir.is_dir():
        return []
    results: list[MirrorResult] = []
    for dest in sorted(mirror_dir.glob("*.md")):
        slug = dest.stem
        source = agents_dir / f"{slug}.md"
        if source.is_file():
            continue  # Still present upstream, skip.
        if dry_run:
            results.append(MirrorResult(
                slug=slug, status="pruned",
                source_path=None, dest_path=str(dest),
                message="dry-run: would delete",
            ))
            continue
        try:
            dest.unlink()
            results.append(MirrorResult(
                slug=slug, status="pruned",
                source_path=None, dest_path=str(dest),
            ))
        except OSError as exc:
            results.append(MirrorResult(
                slug=slug, status="skipped-no-frontmatter",
                source_path=None, dest_path=str(dest),
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
        prog="ctx-agent-mirror",
        description=(
            "Mirror agent bodies from ~/.claude/agents/ into "
            "<wiki>/converted-agents/<slug>.md. Unblocks ctx-agent-install "
            "by giving the wiki the actual agent prompt, not just the card."
        ),
    )
    parser.add_argument("--slug", help="Mirror a single slug")
    parser.add_argument(
        "--force", action="store_true",
        help="Rewrite mirrored files even when content hash is unchanged",
    )
    parser.add_argument(
        "--prune", action="store_true",
        help="Delete mirrored bodies whose live agent file vanished",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen without writing or deleting",
    )
    parser.add_argument(
        "--agents-dir", default=str(cfg.agents_dir),
        help="Live agents dir (default: cfg.agents_dir)",
    )
    parser.add_argument(
        "--wiki-dir", default=str(cfg.wiki_dir),
        help="Wiki root (default: cfg.wiki_dir)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit results as JSON for automation",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    agents_dir = Path(os.path.expanduser(args.agents_dir))
    wiki_dir = Path(os.path.expanduser(args.wiki_dir))

    results: list[MirrorResult] = []
    if args.slug:
        results.append(mirror_one(
            args.slug, agents_dir=agents_dir, wiki_dir=wiki_dir,
            force=args.force, dry_run=args.dry_run,
        ))
    else:
        results.extend(mirror_all(
            agents_dir=agents_dir, wiki_dir=wiki_dir,
            force=args.force, dry_run=args.dry_run,
        ))
        if args.prune:
            results.extend(prune_orphans(
                agents_dir=agents_dir, wiki_dir=wiki_dir,
                dry_run=args.dry_run,
            ))

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2))
    else:
        counts = _summarize(results)
        print("Mirror summary:")
        for status in sorted(counts):
            print(f"  {status}: {counts[status]}")
        # Show non-success outcomes explicitly so problems don't hide.
        for r in results:
            if r.status not in ("mirrored", "unchanged"):
                print(f"  [{r.status}] {r.slug}: {r.message}")

    # Exit 0 unless something unexpected happened.
    hard_failures = [
        r for r in results
        if r.status in ("skipped-no-frontmatter",) and r.message.startswith(("read failed", "unlink failed"))
    ]
    sys.exit(1 if hard_failures else 0)


if __name__ == "__main__":
    main()
