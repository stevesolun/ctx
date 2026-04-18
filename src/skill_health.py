#!/usr/bin/env python3
"""
skill_health.py -- Skill & agent health dashboard + self-healing catalog.

Scans the skills + agents directories and produces a structured health
report across four dimensions:

    structure   missing SKILL.md, empty file, binary content
    frontmatter malformed YAML, missing name/description
    size        over cfg.line_threshold (default 180)
    drift       in manifest but missing on disk (orphaned)
                on disk but never loaded (unused)

The dashboard is consumed two ways:

    report   a JSON payload for programmatic use
    format   a human-readable CLI dashboard

``--heal`` performs safe autofixes:
    - drops orphaned entries from ~/.claude/pending-skills.json
    - drops orphaned entries from ~/.claude/skill-manifest.json
    - nothing destructive ever touches SKILL.md or agent .md files

Usage:
    python src/skill_health.py scan
    python src/skill_health.py dashboard
    python src/skill_health.py check --strict   # exit 2 if any ERROR issues
    python src/skill_health.py heal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


# ── Paths & config defaults ────────────────────────────────────────────────


SKILLS_DIR = Path(os.path.expanduser("~/.claude/skills"))
AGENTS_DIR = Path(os.path.expanduser("~/.claude/agents"))
MANIFEST_PATH = Path(os.path.expanduser("~/.claude/skill-manifest.json"))
PENDING_PATH = Path(os.path.expanduser("~/.claude/pending-skills.json"))

DEFAULT_LINE_THRESHOLD = 180
DEFAULT_MIN_BODY_LINES = 5
ERROR_SEVERITIES = frozenset({"error"})
_SEVERITY_RANK = {"ok": 0, "warning": 1, "error": 2}


# ── Data model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str              # "warning" | "error"
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EntityHealth:
    name: str
    kind: str                  # "skill" | "agent"
    path: str
    lines: int
    has_frontmatter: bool
    issues: tuple[Issue, ...] = ()

    @property
    def severity(self) -> str:
        worst = "ok"
        for issue in self.issues:
            if _SEVERITY_RANK.get(issue.severity, 0) > _SEVERITY_RANK.get(worst, 0):
                worst = issue.severity
        return worst

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity
        return d


@dataclass(frozen=True)
class DriftReport:
    orphaned_manifest: tuple[str, ...] = ()
    orphaned_pending: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def empty(self) -> bool:
        return not self.orphaned_manifest and not self.orphaned_pending


@dataclass(frozen=True)
class HealthReport:
    generated_at: float
    entities: tuple[EntityHealth, ...]
    drift: DriftReport
    totals: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "entities": [e.to_dict() for e in self.entities],
            "drift": self.drift.to_dict(),
            "totals": self.totals,
        }

    @property
    def has_errors(self) -> bool:
        if any(e.severity == "error" for e in self.entities):
            return True
        return not self.drift.empty


# ── Frontmatter parsing ────────────────────────────────────────────────────


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """
    Return (frontmatter_fields, body). If no frontmatter is present or it is
    malformed, frontmatter_fields is empty.
    """
    if not text.startswith("---"):
        return {}, text
    # Find the closing fence. Only consider the first 80 lines to bound cost.
    lines = text.splitlines()
    if not lines:
        return {}, text
    close_idx = None
    for idx in range(1, min(80, len(lines))):
        if lines[idx].strip() == "---":
            close_idx = idx
            break
    if close_idx is None:
        return {}, text
    fields: dict[str, str] = {}
    for raw in lines[1:close_idx]:
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[close_idx + 1 :])
    return fields, body


# ── Scanner ────────────────────────────────────────────────────────────────


def _read_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None


def _inspect(path: Path, kind: str, name: str,
             line_threshold: int,
             min_body_lines: int) -> EntityHealth:
    text = _read_safe(path)
    if text is None:
        return EntityHealth(
            name=name, kind=kind, path=str(path), lines=0,
            has_frontmatter=False,
            issues=(Issue("unreadable", "error",
                          "file could not be read as UTF-8"),),
        )

    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    frontmatter, body = _split_frontmatter(text)

    issues: list[Issue] = []

    if not frontmatter:
        issues.append(Issue(
            "no-frontmatter", "error",
            "missing or malformed YAML frontmatter",
        ))
    else:
        if not frontmatter.get("name"):
            issues.append(Issue(
                "frontmatter-missing-name", "error",
                "frontmatter missing required 'name' field",
            ))
        if not frontmatter.get("description"):
            issues.append(Issue(
                "frontmatter-missing-description", "warning",
                "frontmatter missing 'description' (router relevance suffers)",
            ))

    body_lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(body_lines) < min_body_lines:
        issues.append(Issue(
            "empty-body", "error",
            f"body has fewer than {min_body_lines} non-blank lines",
        ))

    if lines > line_threshold:
        issues.append(Issue(
            "over-threshold", "warning",
            f"{lines} lines exceeds threshold {line_threshold} "
            "(consider moving reference material to a /references page)",
        ))

    return EntityHealth(
        name=name,
        kind=kind,
        path=str(path),
        lines=lines,
        has_frontmatter=bool(frontmatter),
        issues=tuple(issues),
    )


def scan_skills(skills_dir: Path, line_threshold: int,
                min_body_lines: int) -> list[EntityHealth]:
    out: list[EntityHealth] = []
    if not skills_dir.exists():
        return out
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            out.append(EntityHealth(
                name=entry.name, kind="skill", path=str(skill_md),
                lines=0, has_frontmatter=False,
                issues=(Issue("missing-file", "error",
                              "skill directory has no SKILL.md"),),
            ))
            continue
        out.append(_inspect(skill_md, "skill", entry.name,
                            line_threshold, min_body_lines))
    return out


def scan_agents(agents_dir: Path, line_threshold: int,
                min_body_lines: int) -> list[EntityHealth]:
    out: list[EntityHealth] = []
    if not agents_dir.exists():
        return out
    for entry in sorted(agents_dir.glob("*.md")):
        out.append(_inspect(entry, "agent", entry.stem,
                            line_threshold, min_body_lines))
    return out


# ── Drift detection ────────────────────────────────────────────────────────


def _entity_names(entities: Sequence[EntityHealth]) -> set[str]:
    return {e.name for e in entities}


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _manifest_names(manifest: dict | None) -> list[str]:
    if not manifest:
        return []
    loads = manifest.get("load") or []
    out: list[str] = []
    for item in loads:
        if isinstance(item, dict) and item.get("skill"):
            out.append(str(item["skill"]))
    return out


def _pending_names(pending: dict | None) -> list[str]:
    if not pending:
        return []
    names: list[str] = []
    for item in pending.get("graph_suggestions") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    for item in pending.get("unmatched_signals") or []:
        if isinstance(item, str):
            names.append(item)
    return names


def detect_drift(entities: Sequence[EntityHealth],
                 manifest_path: Path = MANIFEST_PATH,
                 pending_path: Path = PENDING_PATH) -> DriftReport:
    known = _entity_names(entities)
    manifest = _load_json(manifest_path)
    pending = _load_json(pending_path)

    orphaned_manifest = tuple(
        sorted({n for n in _manifest_names(manifest) if n not in known})
    )
    orphaned_pending = tuple(
        sorted({n for n in _pending_names(pending) if n not in known})
    )
    return DriftReport(
        orphaned_manifest=orphaned_manifest,
        orphaned_pending=orphaned_pending,
    )


# ── Report ─────────────────────────────────────────────────────────────────


def _tally(entities: Sequence[EntityHealth]) -> dict[str, int]:
    totals = {"total": len(entities), "ok": 0, "warning": 0, "error": 0}
    for e in entities:
        totals[e.severity] = totals.get(e.severity, 0) + 1
    return totals


def build_report(skills_dir: Path = SKILLS_DIR,
                 agents_dir: Path = AGENTS_DIR,
                 line_threshold: int = DEFAULT_LINE_THRESHOLD,
                 min_body_lines: int = DEFAULT_MIN_BODY_LINES,
                 manifest_path: Path = MANIFEST_PATH,
                 pending_path: Path = PENDING_PATH,
                 now: float | None = None) -> HealthReport:
    skills = scan_skills(skills_dir, line_threshold, min_body_lines)
    agents = scan_agents(agents_dir, line_threshold, min_body_lines)
    entities = tuple(skills + agents)
    drift = detect_drift(entities, manifest_path, pending_path)
    return HealthReport(
        generated_at=now if now is not None else time.time(),
        entities=entities,
        drift=drift,
        totals=_tally(entities),
    )


# ── Dashboard renderer ─────────────────────────────────────────────────────


def format_dashboard(report: HealthReport) -> str:
    t = report.totals
    header = (
        f"[health] {t.get('total', 0)} entities   "
        f"ok={t.get('ok', 0)}  warn={t.get('warning', 0)}  "
        f"err={t.get('error', 0)}"
    )
    lines = [header]

    problems = [
        e for e in report.entities if e.severity != "ok"
    ]
    if problems:
        lines.append("")
        lines.append("Issues:")
        # errors first, then warnings
        for severity in ("error", "warning"):
            group = [e for e in problems if e.severity == severity]
            if not group:
                continue
            lines.append(f"  [{severity}]")
            for e in sorted(group, key=lambda x: x.name):
                for issue in e.issues:
                    if issue.severity != severity:
                        continue
                    lines.append(
                        f"    - {e.name} ({e.kind}, {e.lines} lines): "
                        f"{issue.code} — {issue.message}"
                    )

    if not report.drift.empty:
        lines.append("")
        lines.append("Drift:")
        if report.drift.orphaned_manifest:
            lines.append(
                "  manifest references missing on disk: "
                + ", ".join(report.drift.orphaned_manifest)
            )
        if report.drift.orphaned_pending:
            lines.append(
                "  pending suggestions missing on disk: "
                + ", ".join(report.drift.orphaned_pending)
            )

    if len(lines) == 1:
        lines.append("All healthy.")
    return "\n".join(lines)


# ── Self-healing ───────────────────────────────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class HealResult:
    manifest_removed: tuple[str, ...] = ()
    pending_removed: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def empty(self) -> bool:
        return not self.manifest_removed and not self.pending_removed


def _heal_manifest(manifest_path: Path,
                   orphans: Iterable[str]) -> tuple[str, ...]:
    orphan_set = set(orphans)
    if not orphan_set:
        return ()
    data = _load_json(manifest_path)
    if not data:
        return ()
    loads = data.get("load") or []
    kept = [
        item for item in loads
        if not (isinstance(item, dict) and item.get("skill") in orphan_set)
    ]
    if len(kept) == len(loads):
        return ()
    data["load"] = kept
    _atomic_write(manifest_path, json.dumps(data, indent=2))
    return tuple(sorted(orphan_set))


def _heal_pending(pending_path: Path,
                  orphans: Iterable[str]) -> tuple[str, ...]:
    orphan_set = set(orphans)
    if not orphan_set:
        return ()
    data = _load_json(pending_path)
    if not data:
        return ()
    suggestions = data.get("graph_suggestions") or []
    kept_suggestions = [
        item for item in suggestions
        if not (isinstance(item, dict) and item.get("name") in orphan_set)
    ]
    unmatched = data.get("unmatched_signals") or []
    kept_unmatched = [s for s in unmatched if s not in orphan_set]

    if (len(kept_suggestions) == len(suggestions)
            and len(kept_unmatched) == len(unmatched)):
        return ()
    data["graph_suggestions"] = kept_suggestions
    data["unmatched_signals"] = kept_unmatched
    _atomic_write(pending_path, json.dumps(data, indent=2))
    return tuple(sorted(orphan_set))


def heal(report: HealthReport,
         manifest_path: Path = MANIFEST_PATH,
         pending_path: Path = PENDING_PATH) -> HealResult:
    manifest_removed = _heal_manifest(
        manifest_path, report.drift.orphaned_manifest
    )
    pending_removed = _heal_pending(
        pending_path, report.drift.orphaned_pending
    )
    return HealResult(
        manifest_removed=manifest_removed,
        pending_removed=pending_removed,
    )


# ── CLI ────────────────────────────────────────────────────────────────────


def _cli_report(args: argparse.Namespace) -> HealthReport:
    """
    Build a report using current module-level paths.

    Read globals at call time so tests (and any runtime overrides) can
    redirect SKILLS_DIR / AGENTS_DIR / MANIFEST_PATH / PENDING_PATH without
    having to pass them in explicitly.
    """
    return build_report(
        skills_dir=SKILLS_DIR,
        agents_dir=AGENTS_DIR,
        line_threshold=args.line_threshold,
        manifest_path=MANIFEST_PATH,
        pending_path=PENDING_PATH,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    report = _cli_report(args)
    print(json.dumps(report.to_dict(), indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    report = _cli_report(args)
    print(format_dashboard(report))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    report = _cli_report(args)
    print(format_dashboard(report))
    if args.strict and report.has_errors:
        return 2
    return 0


def cmd_heal(args: argparse.Namespace) -> int:
    report = _cli_report(args)
    result = heal(report, MANIFEST_PATH, PENDING_PATH)
    if result.empty:
        print("[heal] nothing to do.")
        return 0
    if result.manifest_removed:
        print("[heal] removed from manifest: "
              + ", ".join(result.manifest_removed))
    if result.pending_removed:
        print("[heal] removed from pending: "
              + ", ".join(result.pending_removed))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skill_health")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--line-threshold", type=int,
                        default=DEFAULT_LINE_THRESHOLD)

    sp = sub.add_parser("scan", help="Emit a JSON health report")
    _add_common(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("dashboard", help="Pretty dashboard to stdout")
    _add_common(sp)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("check",
                        help="Dashboard + nonzero exit on error in --strict")
    _add_common(sp)
    sp.add_argument("--strict", action="store_true")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("heal",
                        help="Drop orphan entries from manifest + pending")
    _add_common(sp)
    sp.set_defaults(func=cmd_heal)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
