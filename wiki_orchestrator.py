#!/usr/bin/env python3
"""
wiki_orchestrator.py -- Master orchestrator and validator for the skill wiki.

Single entry point that runs all maintenance operations in the correct order,
validates results, and reports a 0-100 health score.

Usage:
    python wiki_orchestrator.py --check         # Read-only validation + health score
    python wiki_orchestrator.py --sync          # Full ordered sync of all components
    python wiki_orchestrator.py --add <path>    # Add a single new skill
    python wiki_orchestrator.py --status        # Quick summary stats
"""

import argparse
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ctx_config import cfg

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

SCRIPT_DIR = Path(__file__).parent

# Required sections that SCHEMA.md must contain.
SCHEMA_REQUIRED_SECTIONS = [
    "## Domain",
    "## Conventions",
    "## Tag Taxonomy",
    "## Page Thresholds",
    "## Update Policy",
]

# Entity pages with updated dates older than this are stale.
STALE_DAYS = 90


# ---------------------------------------------------------------------------
# Result / reporting types
# ---------------------------------------------------------------------------


@dataclass
class HealthReport:
    """Accumulated findings from a validation run."""

    score: int = 100
    orphan_pages: list[str] = field(default_factory=list)
    broken_wikilinks: list[str] = field(default_factory=list)
    missing_entity_pages: list[str] = field(default_factory=list)
    missing_schema_sections: list[str] = field(default_factory=list)
    stale_pages: list[str] = field(default_factory=list)
    invalid_frontmatter: list[str] = field(default_factory=list)
    leftover_originals: list[str] = field(default_factory=list)
    index_count_wrong: bool = False
    skipped_modules: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def deduct(self, points: int, reason: str) -> None:
        self.score = max(0, self.score - points)
        self.warnings.append(f"[-{points}] {reason}")

    def render(self) -> str:
        lines = [
            f"Health Score: {self.score}/100",
            "",
        ]
        if self.skipped_modules:
            lines.append("Skipped modules (not yet installed):")
            lines.extend(f"  - {m}" for m in self.skipped_modules)
            lines.append("")
        if not self.warnings:
            lines.append("No issues found.")
        else:
            lines.append("Issues:")
            lines.extend(f"  {w}" for w in self.warnings)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dynamic import helpers
# ---------------------------------------------------------------------------


def _try_import(module_name: str, report: HealthReport) -> Any | None:
    """Import a sibling script by name. Returns module or None if missing."""
    candidate = SCRIPT_DIR / f"{module_name}.py"
    if not candidate.exists():
        report.skipped_modules.append(module_name)
        return None
    spec = importlib.util.spec_from_file_location(module_name, candidate)
    if spec is None or spec.loader is None:
        report.skipped_modules.append(module_name)
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        report.skipped_modules.append(f"{module_name} (import error: {exc})")
        return None
    return mod


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _count_log_entries(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    return sum(1 for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith("## ["))


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter key/value pairs (flat strings only)."""
    fm: dict[str, str] = {}
    if not text.startswith("---"):
        return fm
    end = text.find("---", 3)
    if end < 0:
        return fm
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip()
    return fm


def _collect_all_entity_pages(wiki_dir: Path) -> list[Path]:
    skills_dir = wiki_dir / "entities" / "skills"
    if not skills_dir.exists():
        return []
    return sorted(skills_dir.glob("*.md"))


def _collect_all_wikilinks(wiki_dir: Path) -> set[str]:
    """Return all [[target]] link targets found across the wiki."""
    links: set[str] = set()
    for md in wiki_dir.rglob("*.md"):
        try:
            for m in re.finditer(r"\[\[([^\]]+)\]\]", md.read_text(encoding="utf-8", errors="replace")):
                links.add(m.group(1).strip())
        except Exception:
            pass
    return links


def _index_page_count(wiki_dir: Path) -> int | None:
    """Read the 'Total pages: N' value from index.md. Returns None if missing."""
    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        return None
    m = re.search(r"Total pages:\s*(\d+)", index_path.read_text(encoding="utf-8", errors="replace"))
    return int(m.group(1)) if m else None


def _actual_page_count(wiki_dir: Path) -> int:
    """Count all .md files under the wiki (excluding SCHEMA, index, log, catalogs)."""
    exclude = {"SCHEMA.md", "index.md", "log.md", "catalog.md", "versions-catalog.md", "converted-index.md"}
    return sum(1 for p in wiki_dir.rglob("*.md") if p.name not in exclude)


def _skill_names_from_disk(report: HealthReport) -> list[str]:
    """Return skill names from all skill dirs according to config."""
    names: list[str] = []
    for skill_dir in cfg.all_skill_dirs():
        for item in sorted(skill_dir.iterdir()):
            if item.is_dir() and (item / "SKILL.md").exists():
                names.append(item.name)
            elif item.is_file() and item.suffix == ".md":
                names.append(item.stem)
    return names


def _converted_skill_names(wiki_dir: Path) -> list[str]:
    """Return names of skills with a converted/ pipeline directory."""
    converted_dir = wiki_dir / "converted"
    if not converted_dir.exists():
        return []
    return [d.name for d in sorted(converted_dir.iterdir()) if d.is_dir()]


# ---------------------------------------------------------------------------
# --check : validation pass
# ---------------------------------------------------------------------------


def run_check(wiki_dir: Path, verbose: bool = False) -> HealthReport:
    """Read-only validation. Returns populated HealthReport."""
    report = HealthReport()

    # 1. SCHEMA.md existence and required sections
    schema_path = wiki_dir / "SCHEMA.md"
    if not schema_path.exists():
        report.deduct(5, "SCHEMA.md missing entirely")
        report.missing_schema_sections.extend(SCHEMA_REQUIRED_SECTIONS)
    else:
        schema_text = schema_path.read_text(encoding="utf-8", errors="replace")
        for section in SCHEMA_REQUIRED_SECTIONS:
            if section not in schema_text:
                report.missing_schema_sections.append(section)
                report.deduct(5, f"SCHEMA.md missing section: {section}")

    # 2. Entity page frontmatter validity
    entity_pages = _collect_all_entity_pages(wiki_dir)
    entity_names: set[str] = set()
    for page in entity_pages:
        name = page.stem
        entity_names.add(name)
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except Exception:
            report.invalid_frontmatter.append(name)
            report.deduct(2, f"Cannot read entity page: {name}")
            continue

        fm = _parse_frontmatter(text)
        if not fm:
            report.invalid_frontmatter.append(name)
            report.deduct(2, f"No valid YAML frontmatter: {name}")
            continue

        # Stale check
        updated_str = fm.get("updated", "")
        if updated_str:
            try:
                updated_dt = datetime.strptime(updated_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                if (now - updated_dt).days > STALE_DAYS:
                    report.stale_pages.append(name)
                    report.deduct(1, f"Stale page (>{STALE_DAYS}d): {name}")
            except ValueError:
                pass

    # 3. Every skill on disk has an entity page
    skill_names = _skill_names_from_disk(report)
    for skill in skill_names:
        if skill not in entity_names:
            report.missing_entity_pages.append(skill)
            report.deduct(1, f"Missing entity page for skill: {skill}")

    # 4. Converted pipelines have entity pages with has_pipeline: true
    converted_names = _converted_skill_names(wiki_dir)
    for name in converted_names:
        if name not in entity_names:
            report.missing_entity_pages.append(name)
            report.deduct(1, f"Missing entity page for converted pipeline: {name}")
        else:
            page_path = wiki_dir / "entities" / "skills" / f"{name}.md"
            try:
                fm = _parse_frontmatter(page_path.read_text(encoding="utf-8", errors="replace"))
                if fm.get("has_pipeline", "").lower() != "true":
                    report.warnings.append(f"  [warn] Entity page for {name} missing has_pipeline: true")
            except Exception:
                pass

    # 5. index.md page count matches actual count
    indexed = _index_page_count(wiki_dir)
    actual = _actual_page_count(wiki_dir)
    if indexed is not None and indexed != actual:
        report.index_count_wrong = True
        report.deduct(10, f"index.md says {indexed} pages but {actual} exist on disk")
    elif indexed is None:
        report.warnings.append("  [warn] index.md missing or has no 'Total pages:' line")

    # 6. log.md entry count
    log_count = _count_log_entries(wiki_dir / "log.md")
    if log_count > 500:
        report.deduct(5, f"log.md has {log_count} entries (>500); consider archiving")

    # 7. No SKILL.md.original files in skill dirs (originals must stay untouched)
    for skill_dir in cfg.all_skill_dirs():
        for original in skill_dir.rglob("SKILL.md.original"):
            report.leftover_originals.append(str(original))
            report.deduct(1, f"SKILL.md.original present (original must be untouched): {original}")

    # 8. Broken wikilinks — link target doesn't resolve to any wiki page
    all_md_stems: set[str] = {p.stem for p in wiki_dir.rglob("*.md")}
    all_md_paths: set[str] = {
        str(p.relative_to(wiki_dir)).replace("\\", "/").removesuffix(".md")
        for p in wiki_dir.rglob("*.md")
    }
    all_wikilinks = _collect_all_wikilinks(wiki_dir)
    for link in sorted(all_wikilinks):
        bare = link.split("|")[0].strip()  # handle [[target|alias]]
        # Match by full relative path or bare stem
        if bare not in all_md_paths and bare not in all_md_stems:
            report.broken_wikilinks.append(bare)
            report.deduct(2, f"Broken wikilink: [[{bare}]]")

    # 9. Orphan pages (no inbound wikilink pointing to them) — light check on entity pages
    inbound_targets: set[str] = set()
    for link in all_wikilinks:
        bare = link.split("|")[0].strip()
        inbound_targets.add(bare)
        # Also match by stem alone
        inbound_targets.add(bare.split("/")[-1])

    for page in entity_pages:
        page_stem = page.stem
        rel = str(page.relative_to(wiki_dir)).replace("\\", "/").removesuffix(".md")
        if page_stem not in inbound_targets and rel not in inbound_targets:
            report.orphan_pages.append(page_stem)
            report.deduct(1, f"Orphan page (no inbound wikilinks): {page_stem}")

    # 10. Delegate to wiki_lint if available
    lint_mod = _try_import("wiki_lint", report)
    if lint_mod is not None and hasattr(lint_mod, "run_lint"):
        try:
            lint_issues = lint_mod.run_lint(wiki_dir)
            for issue in lint_issues or []:
                report.warnings.append(f"  [lint] {issue}")
        except Exception as exc:
            report.warnings.append(f"  [lint] wiki_lint.run_lint raised: {exc}")

    report.score = max(0, report.score)
    return report


# ---------------------------------------------------------------------------
# --sync : full ordered sync
# ---------------------------------------------------------------------------


def run_sync(wiki_dir: Path, verbose: bool = False) -> HealthReport:
    """Run all maintenance steps in order. Returns final HealthReport."""
    report = HealthReport()
    _log = print  # simple immediate output

    def step(n: int, label: str) -> None:
        _log(f"\nStep {n}: {label}")

    # Step 1 — ensure wiki structure
    step(1, "Ensure wiki structure")
    ws_mod = _try_import("wiki_sync", report)
    if ws_mod and hasattr(ws_mod, "ensure_wiki"):
        ws_mod.ensure_wiki(str(wiki_dir))
        _log("  wiki structure OK")
    else:
        _log("  wiki_sync.ensure_wiki not available — skipping")

    # Step 2 — scan skill dirs for new/changed skills
    step(2, "Scan skill directories")
    skill_names = _skill_names_from_disk(report)
    _log(f"  {len(skill_names)} skills found across all dirs")

    # Step 3 — auto-convert skills >180 lines via batch_convert
    step(3, "Auto-convert skills >180 lines")
    bc_mod = _try_import("batch_convert", report)
    converted_count = 0
    if bc_mod and hasattr(bc_mod, "convert_skill"):
        for skill_dir in cfg.all_skill_dirs():
            for item in sorted(skill_dir.iterdir()):
                if not item.is_dir():
                    continue
                skill_md = item / "SKILL.md"
                if not skill_md.exists():
                    continue
                try:
                    lines = len(skill_md.read_text(encoding="utf-8", errors="replace").splitlines())
                except Exception:
                    continue
                if lines > cfg.line_threshold:
                    out_dir = wiki_dir / "converted" / item.name
                    out_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        bc_mod.convert_skill(str(skill_md), str(out_dir))
                        converted_count += 1
                    except Exception as exc:
                        report.warnings.append(f"  [convert] {item.name}: {exc}")
        _log(f"  {converted_count} skills converted/refreshed")
    else:
        _log("  batch_convert not available — skipping auto-convert")

    # Step 4 — upsert entity pages for all skills
    step(4, "Upsert entity pages")
    new_pages: list[str] = []
    if ws_mod and hasattr(ws_mod, "upsert_skill_page"):
        for skill_dir in cfg.all_skill_dirs():
            for item in sorted(skill_dir.iterdir()):
                if item.is_dir() and (item / "SKILL.md").exists():
                    skill_info = {"path": str(item / "SKILL.md"), "reason": "orchestrator sync"}
                    try:
                        is_new = ws_mod.upsert_skill_page(str(wiki_dir), item.name, skill_info)
                        if is_new:
                            new_pages.append(item.name)
                    except Exception as exc:
                        report.warnings.append(f"  [upsert] {item.name}: {exc}")
        _log(f"  {len(new_pages)} new entity pages created")
    else:
        _log("  wiki_sync.upsert_skill_page not available — skipping")

    # Step 5 — link entity pages to converted pipelines
    step(5, "Link entity pages to converted pipelines")
    lc_mod = _try_import("link_conversions", report)
    if lc_mod and hasattr(lc_mod, "link_all"):
        try:
            lc_mod.link_all(str(wiki_dir))
            _log("  link_conversions.link_all complete")
        except Exception as exc:
            report.warnings.append(f"  [link_conversions] {exc}")
    else:
        _log("  link_conversions not available — skipping")

    # Step 6 — rebuild catalog.md and versions-catalog.md
    step(6, "Rebuild catalogs")
    cb_mod = _try_import("catalog_builder", report)
    if cb_mod and hasattr(cb_mod, "build_catalog"):
        try:
            stats = cb_mod.build_catalog(
                wiki_dir=wiki_dir,
                skills_dir=cfg.skills_dir,
                agents_dir=cfg.agents_dir,
                extra_dirs=cfg.extra_skill_dirs,
            )
            if hasattr(cb_mod, "update_wiki_index"):
                cb_mod.update_wiki_index(wiki_dir, stats)
            _log(f"  catalog.md: {stats.get('total', '?')} items")
        except Exception as exc:
            report.warnings.append(f"  [catalog_builder] {exc}")
    else:
        _log("  catalog_builder not available — skipping")

    vc_mod = _try_import("versions_catalog", report)
    if vc_mod and hasattr(vc_mod, "find_dual_version_skills") and hasattr(vc_mod, "build_versions_catalog"):
        try:
            all_dirs = [cfg.skills_dir, cfg.agents_dir] + cfg.extra_skill_dirs
            dual_skills: list[dict] = []
            for d in all_dirs:
                dual_skills.extend(vc_mod.find_dual_version_skills(d))
            if dual_skills:
                catalog_path = vc_mod.build_versions_catalog(wiki_dir, dual_skills)
                _log(f"  versions-catalog.md: {len(dual_skills)} dual-version skills")
            else:
                _log("  versions-catalog.md: no dual-version skills found")
        except Exception as exc:
            report.warnings.append(f"  [versions_catalog] {exc}")
    else:
        _log("  versions_catalog not available — skipping")

    # Step 7 — rebuild index.md with accurate page counts
    step(7, "Rebuild index.md page counts")
    if ws_mod and hasattr(ws_mod, "update_index"):
        try:
            ws_mod.update_index(str(wiki_dir), new_pages)
        except Exception as exc:
            report.warnings.append(f"  [update_index] {exc}")
    actual = _actual_page_count(wiki_dir)
    _log(f"  {actual} total pages on disk")

    # Step 8 — run lint pass
    step(8, "Run lint")
    lint_mod = _try_import("wiki_lint", report)
    if lint_mod and hasattr(lint_mod, "run_lint"):
        try:
            lint_issues = lint_mod.run_lint(wiki_dir) or []
            for issue in lint_issues:
                report.warnings.append(f"  [lint] {issue}")
            _log(f"  lint: {len(lint_issues)} issues")
        except Exception as exc:
            report.warnings.append(f"  [wiki_lint] {exc}")
    else:
        _log("  wiki_lint not available — skipping lint")

    # Step 9 — append sync summary to log.md
    step(9, "Append log entry")
    if ws_mod and hasattr(ws_mod, "append_log"):
        details = [
            f"Skills found: {len(skill_names)}",
            f"New entity pages: {len(new_pages)}",
            f"Skills auto-converted: {converted_count}",
            f"Total wiki pages: {actual}",
            f"Skipped modules: {', '.join(report.skipped_modules) or 'none'}",
            f"Warnings: {len(report.warnings)}",
        ]
        try:
            ws_mod.append_log(str(wiki_dir), "orchestrator-sync", "full-sync", details)
            _log("  log.md updated")
        except Exception as exc:
            report.warnings.append(f"  [append_log] {exc}")

    # Step 10 — final health check
    step(10, "Compute final health score")
    final_report = run_check(wiki_dir, verbose=verbose)
    # Merge skipped modules accumulated during sync
    for m in report.skipped_modules:
        if m not in final_report.skipped_modules:
            final_report.skipped_modules.append(m)
    # Prepend sync warnings
    final_report.warnings = [w for w in report.warnings if w not in final_report.warnings] + final_report.warnings
    return final_report


# ---------------------------------------------------------------------------
# --add : single skill
# ---------------------------------------------------------------------------


def run_add(wiki_dir: Path, skill_path_or_name: str) -> None:
    """Delegate to skill_add logic if available, else do a minimal upsert."""
    report = HealthReport()
    sa_mod = _try_import("skill_add", report)

    if sa_mod and hasattr(sa_mod, "add_skill"):
        try:
            sa_mod.add_skill(skill_path_or_name, str(wiki_dir))
            print(f"Added skill via skill_add: {skill_path_or_name}")
            return
        except Exception as exc:
            print(f"skill_add.add_skill raised: {exc}", file=sys.stderr)

    # Minimal fallback using wiki_sync
    ws_mod = _try_import("wiki_sync", report)
    if ws_mod and hasattr(ws_mod, "upsert_skill_page"):
        skill_name = Path(skill_path_or_name).stem
        skill_info = {"path": skill_path_or_name, "reason": "manually added via orchestrator"}
        is_new = ws_mod.upsert_skill_page(str(wiki_dir), skill_name, skill_info)
        ws_mod.update_index(str(wiki_dir), [skill_name] if is_new else [])
        ws_mod.append_log(str(wiki_dir), "add-skill", skill_name, [f"Path: {skill_path_or_name}"])
        action = "created" if is_new else "updated"
        print(f"Entity page {action}: {skill_name}")
    else:
        print("Neither skill_add nor wiki_sync available — cannot add skill.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# --status : quick summary
# ---------------------------------------------------------------------------


def run_status(wiki_dir: Path) -> None:
    """Print a quick summary without modifying anything."""
    if not wiki_dir.exists():
        print(f"Wiki not found at {wiki_dir}")
        sys.exit(1)

    entity_pages = _collect_all_entity_pages(wiki_dir)
    converted_names = _converted_skill_names(wiki_dir)
    skill_names = _skill_names_from_disk(HealthReport())

    # Count stale pages
    stale_count = 0
    now = datetime.now(timezone.utc)
    for page in entity_pages:
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
            fm = _parse_frontmatter(text)
            updated_str = fm.get("updated", "")
            if updated_str:
                updated_dt = datetime.strptime(updated_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (now - updated_dt).days > STALE_DAYS:
                    stale_count += 1
        except Exception:
            pass

    # Last sync date from log
    last_sync = "never"
    log_path = wiki_dir / "log.md"
    if log_path.exists():
        for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            m = re.match(r"## \[(\d{4}-\d{2}-\d{2})\] orchestrator-sync", line)
            if m:
                last_sync = m.group(1)
                break

    print(f"Wiki path:      {wiki_dir}")
    print(f"Skills on disk: {len(skill_names)}")
    print(f"Entity pages:   {len(entity_pages)}")
    print(f"Converted:      {len(converted_names)}")
    print(f"Stale pages:    {stale_count}  (>{STALE_DAYS}d without update)")
    print(f"Last sync:      {last_sync}")
    print(f"Log entries:    {_count_log_entries(log_path)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Master orchestrator and validator for the skill wiki.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python wiki_orchestrator.py --check\n"
            "  python wiki_orchestrator.py --sync\n"
            "  python wiki_orchestrator.py --add ~/.claude/skills/my-new-skill\n"
            "  python wiki_orchestrator.py --status\n"
        ),
    )
    parser.add_argument(
        "--wiki",
        default=str(cfg.wiki_dir),
        help=f"Wiki directory (default: {cfg.wiki_dir})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Read-only validation; prints health score 0-100",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Full ordered sync of all wiki components",
    )
    parser.add_argument(
        "--add",
        metavar="PATH_OR_NAME",
        help="Add or refresh a single skill by path or name",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Quick summary: counts, last sync date",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include extra detail in output",
    )
    args = parser.parse_args()

    wiki_dir = Path(args.wiki)

    # Require exactly one mode
    modes = [args.check, args.sync, bool(args.add), args.status]
    if sum(modes) == 0:
        parser.print_help()
        sys.exit(0)
    if sum(modes) > 1:
        parser.error("Specify exactly one of --check, --sync, --add, --status")

    if args.status:
        run_status(wiki_dir)
        return

    if args.add:
        run_add(wiki_dir, args.add)
        return

    if args.check:
        if not wiki_dir.exists():
            print(f"Wiki not found at {wiki_dir}. Run --sync to initialise.", file=sys.stderr)
            sys.exit(1)
        report = run_check(wiki_dir, verbose=args.verbose)
        print(report.render())
        sys.exit(0 if report.score >= 70 else 1)

    if args.sync:
        report = run_sync(wiki_dir, verbose=args.verbose)
        print("\n" + report.render())
        sys.exit(0 if report.score >= 70 else 1)


if __name__ == "__main__":
    main()
