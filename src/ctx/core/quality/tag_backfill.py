"""tag_backfill.py — propose tags for skills/agents with empty frontmatter.

Many imported skills (especially the auto-mirrored short ones) ship
with no ``tags:`` block. The recommender uses tag overlap as one of
three signals; an entity with zero tags is invisible to that signal.
This tool walks the wiki, finds entities with empty ``tags`` (or
missing the field entirely), and proposes a backfill set drawn from:

  1. **Slug tokens** — the entity's filename, hyphen-split, lowercased,
     filtered against a stopword list. ``python-fastapi-development``
     contributes ``python``, ``fastapi``, ``development``.

  2. **Detected keywords** — a small allowlist of well-known
     technologies / disciplines. We scan the entity body for these
     and add any that fire. ``react``, ``kubernetes``, ``security``,
     ``testing``, etc.

  3. **Existing-corpus tag overlap** — any tag already used elsewhere
     in the catalog that also appears as a slug-token of this entity
     is added with high confidence. This keeps backfilled tags
     consistent with the existing tag vocabulary instead of inventing
     new ones.

The tool is **propose-only** by default. It writes a report (YAML
patches per file) and never edits frontmatter unless explicitly
``--apply``ed. The report is reviewable; applied patches are reversible
because they only ever ADD tags, never remove or rewrite them.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_logger = logging.getLogger(__name__)

# Hyphen / underscore / slash splitter — same shape as the recommender's
# slug tokeniser so backfilled tags align with how the recommender will
# match them later.
_SLUG_TOKEN_RE = re.compile(r"[-_/]+")

# Slug-token stoplist: tokens that show up too often or too generically
# to be useful as tags. Keep this list intentionally short — every
# entry here is a tag we won't ever auto-add. Curated by hand from
# the most common tokens in the catalog.
_TOKEN_STOPLIST: frozenset[str] = frozenset({
    # generic
    "skill", "agent", "skills", "agents", "tool", "tools",
    "init", "setup", "config", "guide", "guidelines",
    "create", "creator", "builder", "build",
    "the", "and", "or", "for", "with", "of", "to", "as",
    "v1", "v2", "v3", "v4", "py", "ts", "js",
    # numeric / sequence prefixes used by some imports
    "00", "01", "02", "03", "04", "05", "10", "20",
})

# Domain keyword allowlist. If any of these appear in the entity body,
# they are added as tags. Keep this list short and high-signal — every
# entry here is something worth surfacing in recommendations.
_KEYWORD_TAGS: dict[str, list[str]] = {
    # languages
    "python":      ["python"],
    "typescript":  ["typescript"],
    "javascript":  ["javascript"],
    "rust":        ["rust"],
    "golang":      ["golang"],
    "java ":       ["java"],
    "kotlin":      ["kotlin"],
    "swift":       ["swift"],
    "c++":         ["cpp"],
    "c#":          ["csharp"],
    "ruby":        ["ruby"],
    "elixir":      ["elixir"],
    "scala":       ["scala"],
    "php":         ["php"],
    # frameworks
    "react":       ["react", "frontend"],
    "vue":         ["vue", "frontend"],
    "angular":     ["angular", "frontend"],
    "next.js":     ["nextjs", "frontend"],
    "nextjs":      ["nextjs", "frontend"],
    "svelte":      ["svelte", "frontend"],
    "fastapi":     ["fastapi", "api", "python"],
    "flask":       ["flask", "python"],
    "django":      ["django", "python"],
    "express":     ["express", "nodejs"],
    "rails":       ["rails", "ruby"],
    "spring":      ["spring", "java"],
    "laravel":     ["laravel", "php"],
    # data / ml
    "pandas":      ["pandas", "python", "data"],
    "numpy":       ["numpy", "python"],
    "pytorch":     ["pytorch", "ml"],
    "tensorflow":  ["tensorflow", "ml"],
    "huggingface": ["huggingface", "ml"],
    "embedding":   ["embeddings", "ml"],
    # infra
    "kubernetes":  ["kubernetes", "devops"],
    "docker":      ["docker", "devops"],
    "terraform":   ["terraform", "devops"],
    "ansible":     ["ansible", "devops"],
    "aws":         ["aws", "cloud"],
    "azure":       ["azure", "cloud"],
    "gcp ":        ["gcp", "cloud"],
    # disciplines
    "security":    ["security"],
    "vulnerab":    ["security"],
    "penetration": ["security"],
    "testing":     ["testing"],
    "tdd":         ["testing", "tdd"],
    "ci/cd":       ["cicd", "devops"],
    "performance": ["performance"],
    "monitoring":  ["observability"],
    "logging":     ["observability"],
    "observabil":  ["observability"],
    "graphql":     ["graphql", "api"],
    "rest api":    ["rest", "api"],
    "websocket":   ["websocket"],
    "grpc":        ["grpc", "api"],
    "mcp ":        ["mcp"],
    "claude":      ["claude"],
    "openai":      ["openai"],
    "anthropic":   ["anthropic"],
    "agent":       ["agents"],
}


# ── Data shapes ────────────────────────────────────────────────────────


@dataclass
class TagProposal:
    """Per-entity backfill proposal."""
    entity_type: str    # "skill" | "agent"
    slug: str
    path: Path          # canonical SKILL.md / agent .md path
    current_tags: list[str]
    proposed_add: list[str]  # tags to add (no duplicates with current)
    sources: dict[str, list[str]] = field(default_factory=dict)  # token | keyword | corpus → list of tags


@dataclass
class TagReport:
    """Aggregate output of one backfill scan."""
    total_entities_scanned: int
    entities_with_empty_tags: int
    proposals: list[TagProposal] = field(default_factory=list)


# ── Frontmatter parsing + writing ──────────────────────────────────────


_FM_OPEN = re.compile(r"^---\s*\n", re.MULTILINE)


def _split_frontmatter(text: str) -> tuple[str, str, str]:
    """Return ``(prefix, body_after, frontmatter_text)``.

    ``prefix`` is everything before the opening ``---`` (usually empty
    or an HTML comment from a strix/mattpocock import header).
    ``body_after`` is the markdown body after the closing ``---``.
    """
    if not text.lstrip().startswith("---"):
        return text, "", ""
    # First --- could be after an HTML comment (strix/mattpocock import header)
    open_match = _FM_OPEN.match(text)
    if not open_match:
        # find first ---
        first = text.find("---")
        if first < 0:
            return text, "", ""
        # we want everything before the line containing the leading ---
        prefix_end = text.rfind("\n", 0, first) + 1
        prefix = text[:prefix_end]
        rest = text[prefix_end:]
        if not rest.startswith("---"):
            return text, "", ""
        try:
            close = rest.index("\n---", 3)
        except ValueError:
            return text, "", ""
        fm = rest[3:close].strip("\n")
        body_after = rest[close + 4:].lstrip("\n")
        return prefix, body_after, fm
    prefix = ""
    rest = text
    try:
        close = rest.index("\n---", 3)
    except ValueError:
        return text, "", ""
    fm = rest[3:close].strip("\n")
    body_after = rest[close + 4:].lstrip("\n")
    return prefix, body_after, fm


def _parse_frontmatter_tags(fm_text: str) -> tuple[list[str], bool]:
    """Read the ``tags:`` block. Returns ``(tags, present)``.

    ``present`` is True if a ``tags:`` line was found at all (even if
    empty). False means the field is absent and we'll add it.
    """
    tags: list[str] = []
    present = False
    in_block = False
    for raw in fm_text.splitlines():
        if in_block:
            stripped = raw.strip()
            if stripped.startswith("- "):
                v = stripped[2:].strip().strip('"').strip("'")
                if v:
                    tags.append(v)
                continue
            else:
                in_block = False
        if raw.strip().startswith("tags:"):
            present = True
            after = raw.split(":", 1)[1].strip()
            if after.startswith("[") and after.endswith("]"):
                inner = after[1:-1]
                tags = [t.strip().strip('"').strip("'") for t in inner.split(",") if t.strip()]
            elif after:
                tags = [after.strip('"').strip("'")]
            else:
                in_block = True
    return tags, present


def _render_frontmatter_with_added_tags(
    fm_text: str, tags_to_add: list[str],
) -> str:
    """Return the frontmatter with the new tags merged in."""
    existing, present = _parse_frontmatter_tags(fm_text)
    merged = list(dict.fromkeys(existing + tags_to_add))
    new_block = "tags:\n" + "\n".join(f"  - {t}" for t in merged)

    if not present:
        # Append at end of frontmatter
        return fm_text.rstrip("\n") + "\n" + new_block

    # Replace existing tags block (single-line OR multi-line form)
    out_lines: list[str] = []
    skipping = False
    inserted = False
    for raw in fm_text.splitlines():
        if skipping:
            if raw.startswith(("  -", "\t-", "  ")) or raw.strip() == "":
                if raw.strip().startswith("- "):
                    continue  # drop list item
                if raw.strip() == "":
                    out_lines.append(raw)
                    continue
            skipping = False
        if raw.strip().startswith("tags:"):
            after = raw.split(":", 1)[1].strip()
            if after.startswith("[") and after.endswith("]"):
                # single-line form — replace this one line
                out_lines.append(new_block)
                inserted = True
                continue
            elif after:
                out_lines.append(new_block)
                inserted = True
                continue
            else:
                # block form — emit new block, skip following list items
                out_lines.append(new_block)
                inserted = True
                skipping = True
                continue
        out_lines.append(raw)
    if not inserted:
        out_lines.append(new_block)
    return "\n".join(out_lines)


# ── Discovery + proposal ──────────────────────────────────────────────


def _existing_tag_vocabulary(entities: Iterable[Path]) -> Counter:
    """Tally tag usage across the corpus so backfill prefers existing tags."""
    counter: Counter = Counter()
    for p in entities:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _, _, fm = _split_frontmatter(text)
        if not fm:
            continue
        tags, _ = _parse_frontmatter_tags(fm)
        for t in tags:
            counter[t.lower()] += 1
    return counter


def _slug_token_candidates(slug: str) -> list[str]:
    return [
        t for t in _SLUG_TOKEN_RE.split(slug.lower())
        if t and len(t) >= 3 and t not in _TOKEN_STOPLIST and not t.isdigit()
    ]


def _keyword_candidates(body: str) -> list[str]:
    body_lower = body.lower()
    out: list[str] = []
    for needle, tags in _KEYWORD_TAGS.items():
        if needle in body_lower:
            for t in tags:
                if t not in out:
                    out.append(t)
    return out


def _propose(
    path: Path, entity_type: str, slug: str,
    *, vocab: Counter, max_tags: int,
) -> TagProposal:
    text = path.read_text(encoding="utf-8", errors="replace")
    _, body_after, fm = _split_frontmatter(text)
    current, _ = _parse_frontmatter_tags(fm) if fm else ([], False)
    current_l = {t.lower() for t in current}

    sources: dict[str, list[str]] = {}

    # Slug tokens are the highest-signal candidates: they ARE the entity's
    # name. Always include them, even if they exhaust max_tags. Rare
    # slug-tokens (like 'fastapi' on 'python-fastapi-development') are
    # exactly the discriminating signal we want the recommender to see.
    tok = [t for t in _slug_token_candidates(slug) if t not in current_l]
    if tok:
        sources["slug_token"] = tok[:]

    # Body-keyword candidates fill remaining slots. Prefer keywords that
    # are already in the corpus vocabulary so backfills converge on the
    # existing tag style.
    kw = [t for t in _keyword_candidates(body_after) if t not in current_l]
    kw_sorted = sorted(kw, key=lambda t: (-vocab.get(t, 0), t))
    if kw_sorted:
        sources["body_keyword"] = kw[:]

    # Compose: slug tokens first (capped at max_tags), then body
    # keywords filling whatever's left. Dedupe while preserving order.
    proposed: list[str] = []
    for t in tok[:max_tags]:
        if t not in proposed:
            proposed.append(t)
    for t in kw_sorted:
        if len(proposed) >= max_tags:
            break
        if t not in proposed:
            proposed.append(t)

    return TagProposal(
        entity_type=entity_type,
        slug=slug,
        path=path,
        current_tags=current,
        proposed_add=proposed,
        sources=sources,
    )


def discover_empty_tag_entities(wiki_dir: Path) -> list[tuple[str, str, Path]]:
    """Return ``(entity_type, slug, source_path)`` for every entity whose
    canonical SKILL.md / agent file has empty or missing tags.

    The "canonical" path for skills is ``~/.claude/skills/<slug>/SKILL.md``
    when present (the deployed source), falling back to the entity card
    in the wiki. Same shape for agents.
    """
    out: list[tuple[str, str, Path]] = []
    skills_root = Path.home() / ".claude" / "skills"
    agents_root = Path.home() / ".claude" / "agents"

    # Skills: each <slug>/SKILL.md
    if skills_root.is_dir():
        for skill_dir in skills_root.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            _, _, fm = _split_frontmatter(text)
            if not fm:
                continue
            tags, present = _parse_frontmatter_tags(fm)
            if not tags:  # absent OR present-but-empty both qualify
                out.append(("skill", skill_dir.name, skill_md))

    # Agents: each <slug>.md
    if agents_root.is_dir():
        for agent_md in agents_root.glob("*.md"):
            try:
                text = agent_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            _, _, fm = _split_frontmatter(text)
            if not fm:
                continue
            tags, present = _parse_frontmatter_tags(fm)
            if not tags:
                out.append(("agent", agent_md.stem, agent_md))

    return out


def run_backfill(
    *, wiki_dir: Path, max_tags_per_entity: int = 6,
) -> TagReport:
    """Walk the catalog, propose backfills for empty-tag entities."""
    skills_root = Path.home() / ".claude" / "skills"
    agents_root = Path.home() / ".claude" / "agents"
    all_paths: list[Path] = []
    if skills_root.is_dir():
        all_paths.extend(skills_root.glob("*/SKILL.md"))
    if agents_root.is_dir():
        all_paths.extend(agents_root.glob("*.md"))
    vocab = _existing_tag_vocabulary(all_paths)

    targets = discover_empty_tag_entities(wiki_dir)
    proposals: list[TagProposal] = []
    for entity_type, slug, path in targets:
        prop = _propose(
            path, entity_type, slug, vocab=vocab,
            max_tags=max_tags_per_entity,
        )
        if prop.proposed_add:
            proposals.append(prop)

    return TagReport(
        total_entities_scanned=len(all_paths),
        entities_with_empty_tags=len(targets),
        proposals=proposals,
    )


def apply_proposals(proposals: list[TagProposal]) -> int:
    """Apply each proposal in-place. Returns the count of edited files."""
    edited = 0
    for p in proposals:
        if not p.proposed_add:
            continue
        try:
            text = p.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        prefix, body_after, fm = _split_frontmatter(text)
        if not fm:
            continue
        new_fm = _render_frontmatter_with_added_tags(fm, p.proposed_add)
        new_text = f"{prefix}---\n{new_fm}\n---\n{body_after}"
        try:
            p.path.write_text(new_text, encoding="utf-8")
            edited += 1
        except OSError as exc:
            _logger.warning("tag_backfill: failed to write %s: %s", p.path, exc)
    return edited


# ── Reporting ──────────────────────────────────────────────────────────


def render_markdown(report: TagReport, *, top_n: int = 200) -> str:
    out: list[str] = []
    out.append("# Tag Backfill Report")
    out.append("")
    out.append(f"- **Entities with empty/missing tags**: {report.entities_with_empty_tags:,}")
    out.append(f"- **Proposals generated**: {len(report.proposals):,}")
    if len(report.proposals) > top_n:
        out.append(f"- **Showing**: top {top_n} (full data in `tag-backfill.json`)")
    out.append("")
    if not report.proposals:
        out.append("✓ No empty-tag entities found. Catalog is fully tagged.")
        return "\n".join(out)
    out.append("## Proposals (apply with `ctx-tag-backfill --apply`)")
    out.append("")
    for prop in report.proposals[:top_n]:
        out.append(f"### {prop.entity_type}: `{prop.slug}`")
        out.append(f"- **Path**: `{prop.path}`")
        out.append(f"- **Current tags**: `{prop.current_tags}` (empty/missing)")
        out.append(f"- **Proposed**: `{prop.proposed_add}`")
        for src, vals in prop.sources.items():
            out.append(f"  - from {src}: `{vals}`")
        out.append("")
    return "\n".join(out)


def render_json(report: TagReport) -> str:
    return json.dumps({
        "total_entities_scanned": report.total_entities_scanned,
        "entities_with_empty_tags": report.entities_with_empty_tags,
        "proposals": [
            {
                "entity_type": p.entity_type,
                "slug": p.slug,
                "path": str(p.path),
                "current_tags": p.current_tags,
                "proposed_add": p.proposed_add,
                "sources": p.sources,
            }
            for p in report.proposals
        ],
    }, indent=2)


# ── CLI ───────────────────────────────────────────────────────────────


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Propose (or apply) tag backfills for skills/agents that "
            "ship with empty or missing tags: frontmatter."
        ),
    )
    parser.add_argument(
        "--wiki", type=Path,
        default=Path.home() / ".claude" / "skill-wiki",
        help="Wiki directory (default: ~/.claude/skill-wiki)",
    )
    parser.add_argument(
        "--max-tags", type=int, default=6,
        help="Max tags to add per entity (default: 6)",
    )
    parser.add_argument(
        "--report", type=Path, default=Path("graph") / "tag-backfill.md",
        help="Markdown report output path",
    )
    parser.add_argument(
        "--report-json", type=Path, default=Path("graph") / "tag-backfill.json",
        help="JSON report output path",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Edit frontmatter in place (default: report-only).",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    report = run_backfill(
        wiki_dir=args.wiki, max_tags_per_entity=args.max_tags,
    )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(report), encoding="utf-8")
    args.report_json.write_text(render_json(report), encoding="utf-8")

    if args.apply:
        edited = apply_proposals(report.proposals)
        print(
            f"tag_backfill: APPLIED {edited:,} files. "
            f"empty_tag_entities={report.entities_with_empty_tags:,} "
            f"proposals={len(report.proposals):,} "
            f"report={args.report}",
            flush=True,
        )
    else:
        print(
            f"tag_backfill: report-only. "
            f"empty_tag_entities={report.entities_with_empty_tags:,} "
            f"proposals={len(report.proposals):,} "
            f"report={args.report}  "
            f"(re-run with --apply to write)",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
