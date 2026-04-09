#!/usr/bin/env python3
"""
wiki_orchestrator.py -- Master orchestrator and validator for the skill wiki.

Single entry point for all wiki maintenance: validation, full sync, single-skill
add, and quick status. Think of it as `make all` for ~/.claude/skill-wiki/.

Modes:
    --check   Read-only validation, health score 0-100
    --sync    Full 10-step ordered sync
    --add     Add/refresh one skill by path or name
    --status  Quick counts + last-sync date
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
STALE_DAYS = 90
SCHEMA_REQUIRED_SECTIONS = [
    "## Domain", "## Conventions", "## Tag Taxonomy",
    "## Page Thresholds", "## Update Policy",
]
_CATALOG_EXCLUDES = {
    "SCHEMA.md", "index.md", "log.md",
    "catalog.md", "versions-catalog.md", "converted-index.md",
}


# ---------------------------------------------------------------------------
# HealthReport
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
        out = [f"Health Score: {self.score}/100", ""]
        if self.skipped_modules:
            out.append("Skipped modules (not yet installed):")
            out.extend(f"  - {m}" for m in self.skipped_modules)
            out.append("")
        out.append("No issues found." if not self.warnings else "Issues:")
        out.extend(f"  {w}" for w in self.warnings)
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Dynamic import
# ---------------------------------------------------------------------------


def _try_import(name: str, report: HealthReport) -> Any | None:
    """Load a sibling .py script. Returns module or None; logs skips gracefully.

    Registers in sys.modules before exec so @dataclass decorators that inspect
    cls.__module__ resolve correctly.
    """
    path = SCRIPT_DIR / f"{name}.py"
    if not path.exists():
        report.skipped_modules.append(name)
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        report.skipped_modules.append(name)
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as exc:
        sys.modules.pop(name, None)
        report.skipped_modules.append(f"{name} (import error: {exc})")
        return None
    return mod


# ---------------------------------------------------------------------------
# Low-level wiki helpers
# ---------------------------------------------------------------------------


def _count_log_entries(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    return sum(1 for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
               if ln.startswith("## ["))


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end < 0:
        return {}
    fm: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def _entity_pages(wiki_dir: Path) -> list[Path]:
    d = wiki_dir / "entities" / "skills"
    return sorted(d.glob("*.md")) if d.exists() else []


def _all_wikilinks(wiki_dir: Path) -> set[str]:
    links: set[str] = set()
    for md in wiki_dir.rglob("*.md"):
        try:
            for m in re.finditer(r"\[\[([^\]]+)\]\]",
                                 md.read_text(encoding="utf-8", errors="replace")):
                links.add(m.group(1).strip())
        except Exception:
            pass
    return links


def _index_declared_count(wiki_dir: Path) -> int | None:
    p = wiki_dir / "index.md"
    if not p.exists():
        return None
    m = re.search(r"Total pages:\s*(\d+)", p.read_text(encoding="utf-8", errors="replace"))
    return int(m.group(1)) if m else None


def _actual_page_count(wiki_dir: Path) -> int:
    return sum(1 for p in wiki_dir.rglob("*.md") if p.name not in _CATALOG_EXCLUDES)


def _skill_names_on_disk() -> list[str]:
    names: list[str] = []
    for skill_dir in cfg.all_skill_dirs():
        for item in sorted(skill_dir.iterdir()):
            if item.is_dir() and (item / "SKILL.md").exists():
                names.append(item.name)
            elif item.is_file() and item.suffix == ".md":
                names.append(item.stem)
    return names


def _converted_names(wiki_dir: Path) -> list[str]:
    d = wiki_dir / "converted"
    return [x.name for x in sorted(d.iterdir()) if x.is_dir()] if d.exists() else []


def _is_stale(fm: dict[str, str]) -> bool:
    updated = fm.get("updated", "")
    if not updated:
        return False
    try:
        dt = datetime.strptime(updated, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days > STALE_DAYS
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------


def run_check(wiki_dir: Path, verbose: bool = False) -> HealthReport:  # noqa: ARG001
    """Read-only validation. Returns populated HealthReport."""
    r = HealthReport()

    # SCHEMA sections
    schema = wiki_dir / "SCHEMA.md"
    if not schema.exists():
        r.deduct(5, "SCHEMA.md missing")
        r.missing_schema_sections.extend(SCHEMA_REQUIRED_SECTIONS)
    else:
        text = schema.read_text(encoding="utf-8", errors="replace")
        for sec in SCHEMA_REQUIRED_SECTIONS:
            if sec not in text:
                r.missing_schema_sections.append(sec)
                r.deduct(5, f"SCHEMA.md missing section: {sec}")

    # Entity pages: frontmatter + stale check
    pages = _entity_pages(wiki_dir)
    entity_names: set[str] = set()
    for page in pages:
        name = page.stem
        entity_names.add(name)
        try:
            text = page.read_text(encoding="utf-8", errors="replace")
        except Exception:
            r.invalid_frontmatter.append(name)
            r.deduct(2, f"Cannot read entity page: {name}")
            continue
        fm = _parse_frontmatter(text)
        if not fm:
            r.invalid_frontmatter.append(name)
            r.deduct(2, f"No valid YAML frontmatter: {name}")
        elif _is_stale(fm):
            r.stale_pages.append(name)
            r.deduct(1, f"Stale page (>{STALE_DAYS}d): {name}")

    # Skills on disk missing entity pages
    for skill in _skill_names_on_disk():
        if skill not in entity_names:
            r.missing_entity_pages.append(skill)
            r.deduct(1, f"Missing entity page for skill: {skill}")

    # Converted pipelines missing entity pages or has_pipeline flag
    for name in _converted_names(wiki_dir):
        if name not in entity_names:
            r.missing_entity_pages.append(name)
            r.deduct(1, f"Missing entity page for converted pipeline: {name}")
        else:
            pp = wiki_dir / "entities" / "skills" / f"{name}.md"
            try:
                fm = _parse_frontmatter(pp.read_text(encoding="utf-8", errors="replace"))
                if fm.get("has_pipeline", "").lower() != "true":
                    r.warnings.append(f"  [warn] {name}: entity page missing has_pipeline: true")
            except Exception:
                pass

    # index.md count vs disk count
    declared = _index_declared_count(wiki_dir)
    actual = _actual_page_count(wiki_dir)
    if declared is None:
        r.warnings.append("  [warn] index.md missing or has no 'Total pages:' line")
    elif declared != actual:
        r.index_count_wrong = True
        r.deduct(10, f"index.md says {declared} pages but {actual} exist on disk")

    # log.md size
    if _count_log_entries(wiki_dir / "log.md") > 500:
        r.deduct(5, "log.md has >500 entries; consider archiving")

    # SKILL.md.original files (must not exist — originals stay untouched)
    for skill_dir in cfg.all_skill_dirs():
        for orig in skill_dir.rglob("SKILL.md.original"):
            r.leftover_originals.append(str(orig))
            r.deduct(1, f"SKILL.md.original present (original untouched): {orig}")

    # Wikilink resolution
    all_stems: set[str] = {p.stem for p in wiki_dir.rglob("*.md")}
    all_rel: set[str] = {
        str(p.relative_to(wiki_dir)).replace("\\", "/").removesuffix(".md")
        for p in wiki_dir.rglob("*.md")
    }
    wikilinks = _all_wikilinks(wiki_dir)
    for lnk in sorted(wikilinks):
        bare = lnk.split("|")[0].strip()
        if bare not in all_rel and bare not in all_stems:
            r.broken_wikilinks.append(bare)
            r.deduct(2, f"Broken wikilink: [[{bare}]]")

    # Orphan entity pages
    inbound: set[str] = set()
    for lnk in wikilinks:
        bare = lnk.split("|")[0].strip()
        inbound.add(bare)
        inbound.add(bare.split("/")[-1])
    for page in pages:
        rel = str(page.relative_to(wiki_dir)).replace("\\", "/").removesuffix(".md")
        if page.stem not in inbound and rel not in inbound:
            r.orphan_pages.append(page.stem)
            r.deduct(1, f"Orphan page (no inbound wikilinks): {page.stem}")

    # Delegate to wiki_lint if present
    lint = _try_import("wiki_lint", r)
    if lint and hasattr(lint, "run_lint"):
        try:
            for issue in lint.run_lint(wiki_dir) or []:
                r.warnings.append(f"  [lint] {issue}")
        except Exception as exc:
            r.warnings.append(f"  [lint] wiki_lint.run_lint raised: {exc}")

    return r


# ---------------------------------------------------------------------------
# --sync
# ---------------------------------------------------------------------------


def run_sync(wiki_dir: Path, verbose: bool = False) -> HealthReport:  # noqa: ARG001
    """Run all 10 maintenance steps in order. Returns final HealthReport."""
    acc = HealthReport()  # accumulates sync-phase warnings; merged into final check
    log = print

    # Step 1 — ensure wiki structure
    log("\nStep 1: Ensure wiki structure")
    ws = _try_import("wiki_sync", acc)
    if ws and hasattr(ws, "ensure_wiki"):
        ws.ensure_wiki(str(wiki_dir))
        log("  structure OK")

    # Step 2 — inventory skills
    log("\nStep 2: Scan skill directories")
    skill_names = _skill_names_on_disk()
    log(f"  {len(skill_names)} skills found")

    # Step 3 — auto-convert skills >threshold lines
    log("\nStep 3: Auto-convert skills >180 lines")
    bc = _try_import("batch_convert", acc)
    converted_count = 0
    if bc and hasattr(bc, "convert_skill"):
        for skill_dir in cfg.all_skill_dirs():
            for item in sorted(skill_dir.iterdir()):
                skill_md = item / "SKILL.md"
                if not item.is_dir() or not skill_md.exists():
                    continue
                try:
                    n = len(skill_md.read_text(encoding="utf-8", errors="replace").splitlines())
                except Exception:
                    continue
                if n > cfg.line_threshold:
                    out = wiki_dir / "converted" / item.name
                    out.mkdir(parents=True, exist_ok=True)
                    try:
                        bc.convert_skill(str(skill_md), str(out))
                        converted_count += 1
                    except Exception as exc:
                        acc.warnings.append(f"  [convert] {item.name}: {exc}")
        log(f"  {converted_count} converted/refreshed")
    else:
        log("  batch_convert unavailable — skipping")

    # Step 4 — upsert entity pages
    log("\nStep 4: Upsert entity pages")
    new_pages: list[str] = []
    if ws and hasattr(ws, "upsert_skill_page"):
        for skill_dir in cfg.all_skill_dirs():
            for item in sorted(skill_dir.iterdir()):
                if not (item.is_dir() and (item / "SKILL.md").exists()):
                    continue
                info = {"path": str(item / "SKILL.md"), "reason": "orchestrator sync"}
                try:
                    if ws.upsert_skill_page(str(wiki_dir), item.name, info):
                        new_pages.append(item.name)
                except Exception as exc:
                    acc.warnings.append(f"  [upsert] {item.name}: {exc}")
        log(f"  {len(new_pages)} new entity pages")

    # Step 5 — link entity pages to converted pipelines
    log("\nStep 5: Link entity pages to converted pipelines")
    lc = _try_import("link_conversions", acc)
    if lc and hasattr(lc, "link_all"):
        try:
            lc.link_all(str(wiki_dir))
            log("  link_conversions.link_all complete")
        except Exception as exc:
            acc.warnings.append(f"  [link_conversions] {exc}")
    else:
        log("  link_conversions unavailable — skipping")

    # Step 6 — rebuild catalog.md
    log("\nStep 6: Rebuild catalogs")
    cb = _try_import("catalog_builder", acc)
    if cb and hasattr(cb, "build_catalog"):
        try:
            stats = cb.build_catalog(
                wiki_dir=wiki_dir, skills_dir=cfg.skills_dir,
                agents_dir=cfg.agents_dir, extra_dirs=cfg.extra_skill_dirs,
            )
            if hasattr(cb, "update_wiki_index"):
                cb.update_wiki_index(wiki_dir, stats)
            log(f"  catalog.md: {stats.get('total', '?')} items")
        except Exception as exc:
            acc.warnings.append(f"  [catalog_builder] {exc}")
    # rebuild versions-catalog.md
    vc = _try_import("versions_catalog", acc)
    if vc and hasattr(vc, "find_dual_version_skills") and hasattr(vc, "build_versions_catalog"):
        try:
            all_dirs = [cfg.skills_dir, cfg.agents_dir] + cfg.extra_skill_dirs
            dual: list[dict] = []
            for d in all_dirs:
                dual.extend(vc.find_dual_version_skills(d))
            if dual:
                vc.build_versions_catalog(wiki_dir, dual)
                log(f"  versions-catalog.md: {len(dual)} dual-version skills")
            else:
                log("  versions-catalog.md: no dual-version skills")
        except Exception as exc:
            acc.warnings.append(f"  [versions_catalog] {exc}")

    # Step 7 — rebuild index.md page counts
    log("\nStep 7: Rebuild index.md page counts")
    if ws and hasattr(ws, "update_index"):
        try:
            ws.update_index(str(wiki_dir), new_pages)
        except Exception as exc:
            acc.warnings.append(f"  [update_index] {exc}")
    log(f"  {_actual_page_count(wiki_dir)} total pages on disk")

    # Step 8 — lint pass
    log("\nStep 8: Run lint")
    lint = _try_import("wiki_lint", acc)
    if lint and hasattr(lint, "run_lint"):
        try:
            issues = lint.run_lint(wiki_dir) or []
            for issue in issues:
                acc.warnings.append(f"  [lint] {issue}")
            log(f"  lint: {len(issues)} issues")
        except Exception as exc:
            acc.warnings.append(f"  [wiki_lint] {exc}")
    else:
        log("  wiki_lint unavailable — skipping")

    # Step 9 — append log entry
    log("\nStep 9: Append log entry")
    if ws and hasattr(ws, "append_log"):
        details = [
            f"Skills found: {len(skill_names)}",
            f"New entity pages: {len(new_pages)}",
            f"Skills auto-converted: {converted_count}",
            f"Total wiki pages: {_actual_page_count(wiki_dir)}",
            f"Skipped modules: {', '.join(acc.skipped_modules) or 'none'}",
            f"Warnings: {len(acc.warnings)}",
        ]
        try:
            ws.append_log(str(wiki_dir), "orchestrator-sync", "full-sync", details)
            log("  log.md updated")
        except Exception as exc:
            acc.warnings.append(f"  [append_log] {exc}")

    # Step 10 — final health score
    log("\nStep 10: Compute final health score")
    final = run_check(wiki_dir)
    for m in acc.skipped_modules:
        if m not in final.skipped_modules:
            final.skipped_modules.append(m)
    final.warnings = [w for w in acc.warnings if w not in final.warnings] + final.warnings
    return final


# ---------------------------------------------------------------------------
# --add
# ---------------------------------------------------------------------------


def run_add(wiki_dir: Path, skill_path_or_name: str) -> None:
    """Delegate to skill_add, or fall back to wiki_sync upsert."""
    r = HealthReport()
    sa = _try_import("skill_add", r)
    if sa and hasattr(sa, "add_skill"):
        try:
            sa.add_skill(skill_path_or_name, str(wiki_dir))
            print(f"Added skill via skill_add: {skill_path_or_name}")
            return
        except Exception as exc:
            print(f"skill_add.add_skill raised: {exc}", file=sys.stderr)

    ws = _try_import("wiki_sync", r)
    if not (ws and hasattr(ws, "upsert_skill_page")):
        print("Neither skill_add nor wiki_sync available — cannot add skill.", file=sys.stderr)
        sys.exit(1)

    skill_name = Path(skill_path_or_name).stem
    info = {"path": skill_path_or_name, "reason": "manually added via orchestrator"}
    is_new = ws.upsert_skill_page(str(wiki_dir), skill_name, info)
    ws.update_index(str(wiki_dir), [skill_name] if is_new else [])
    ws.append_log(str(wiki_dir), "add-skill", skill_name, [f"Path: {skill_path_or_name}"])
    print(f"Entity page {'created' if is_new else 'updated'}: {skill_name}")


# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------


def run_status(wiki_dir: Path) -> None:
    """Print quick stats without modifying anything."""
    if not wiki_dir.exists():
        print(f"Wiki not found at {wiki_dir}")
        sys.exit(1)

    pages = _entity_pages(wiki_dir)
    now = datetime.now(timezone.utc)
    stale = sum(
        1 for p in pages
        if _is_stale(_parse_frontmatter(
            p.read_text(encoding="utf-8", errors="replace")
        ))
    )

    log_path = wiki_dir / "log.md"
    last_sync = "never"
    if log_path.exists():
        for line in reversed(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
            m = re.match(r"## \[(\d{4}-\d{2}-\d{2})\] orchestrator-sync", line)
            if m:
                last_sync = m.group(1)
                break

    print(f"Wiki path:      {wiki_dir}")
    print(f"Skills on disk: {len(_skill_names_on_disk())}")
    print(f"Entity pages:   {len(pages)}")
    print(f"Converted:      {len(_converted_names(wiki_dir))}")
    print(f"Stale pages:    {stale}  (>{STALE_DAYS}d without update)")
    print(f"Last sync:      {last_sync}")
    print(f"Log entries:    {_count_log_entries(log_path)}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
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
    p.add_argument("--wiki", default=str(cfg.wiki_dir),
                   help=f"Wiki directory (default: {cfg.wiki_dir})")
    p.add_argument("--check", action="store_true", help="Read-only validation; health score 0-100")
    p.add_argument("--sync", action="store_true", help="Full ordered sync of all wiki components")
    p.add_argument("--add", metavar="PATH_OR_NAME", help="Add or refresh a single skill")
    p.add_argument("--status", action="store_true", help="Quick counts + last sync date")
    p.add_argument("--verbose", action="store_true", help="Extra detail in output")
    args = p.parse_args()

    wiki_dir = Path(args.wiki)
    modes = [args.check, args.sync, bool(args.add), args.status]
    if not any(modes):
        p.print_help()
        sys.exit(0)
    if sum(modes) > 1:
        p.error("Specify exactly one of --check, --sync, --add, --status")

    # Use UTF-8 stdout so emoji from lint issues render on Windows terminals.
    utf8_out = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)

    if args.status:
        run_status(wiki_dir)
    elif args.add:
        run_add(wiki_dir, args.add)
    elif args.check:
        if not wiki_dir.exists():
            print(f"Wiki not found at {wiki_dir}. Run --sync to initialise.", file=sys.stderr)
            sys.exit(1)
        report = run_check(wiki_dir, verbose=args.verbose)
        print(report.render(), file=utf8_out)
        sys.exit(0 if report.score >= 70 else 1)
    elif args.sync:
        report = run_sync(wiki_dir, verbose=args.verbose)
        print("\n" + report.render(), file=utf8_out)
        sys.exit(0 if report.score >= 70 else 1)


if __name__ == "__main__":
    main()
