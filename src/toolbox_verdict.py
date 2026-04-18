#!/usr/bin/env python3
"""
toolbox_verdict.py -- Guardrail verdicts + retrospective surface.

A RunPlan produced by council_runner says *what agents should run*. A Verdict
says *what they found*. This module owns the verdict file format, the
"record a finding" API, and the retrospective / explainability CLI.

Storage:
    ~/.claude/toolbox-runs/<plan_hash>.verdict.json

The sibling location is intentional: toolbox_hooks already reads
``<plan>.verdict.json`` to decide whether pre-commit should block. Keeping
verdicts alongside plans means one ``history`` / ``purge`` sweep covers both.

Verdict level is the max-severity of any recorded finding. The pre-commit
hook blocks only on HIGH or CRITICAL (see toolbox_hooks.run_trigger).

Explainability: each finding carries evidence (file + optional line + free
text note). ``explain`` renders those verbatim so a human-in-the-loop can
trace the verdict back to source. Graph evidence is *not* re-derived here;
callers pass in the files they considered, typically drawn from
RunPlan.files (which already reflects graph-blast expansion).

CLI:
    toolbox_verdict.py record --plan-hash X --level HIGH --title Y \\
        --agent code-reviewer --evidence path/to.py:42 path/to.py:51 \\
        --rationale "race condition on shared counter"
    toolbox_verdict.py show --plan-hash X [--json]
    toolbox_verdict.py retro [--limit 10] [--level HIGH]
    toolbox_verdict.py explain --plan-hash X
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence


RUNS_DIR = Path(os.path.expanduser("~/.claude/toolbox-runs"))

LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_LEVEL_RANK = {level: i for i, level in enumerate(LEVELS)}
BLOCKING_LEVELS = frozenset({"HIGH", "CRITICAL"})


# \u2500\u2500 Data model \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


@dataclass(frozen=True)
class Evidence:
    file: str
    line: int | None = None
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict) -> Evidence:
        line_raw = raw.get("line")
        try:
            line = int(line_raw) if line_raw is not None else None
        except (TypeError, ValueError):
            line = None
        return Evidence(
            file=str(raw.get("file", "")),
            line=line,
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class Finding:
    id: str
    level: str
    title: str
    agent: str = ""
    evidence: tuple[Evidence, ...] = ()
    rationale: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @staticmethod
    def from_dict(raw: dict) -> Finding:
        evidence = tuple(
            Evidence.from_dict(e) for e in raw.get("evidence", []) or []
            if isinstance(e, dict)
        )
        level = str(raw.get("level", "LOW")).upper()
        if level not in _LEVEL_RANK:
            level = "LOW"
        return Finding(
            id=str(raw.get("id", "")),
            level=level,
            title=str(raw.get("title", "")),
            agent=str(raw.get("agent", "")),
            evidence=evidence,
            rationale=str(raw.get("rationale", "")),
            created_at=float(raw.get("created_at", 0) or 0),
        )


@dataclass(frozen=True)
class Verdict:
    plan_hash: str
    level: str
    summary: str
    findings: tuple[Finding, ...]
    created_at: float
    updated_at: float

    @property
    def blocks(self) -> bool:
        """True when this verdict should short-circuit the guardrail hook."""
        return self.level in BLOCKING_LEVELS

    def to_dict(self) -> dict:
        return {
            "plan_hash": self.plan_hash,
            "level": self.level,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(raw: dict) -> Verdict:
        findings = tuple(
            Finding.from_dict(f) for f in raw.get("findings", []) or []
            if isinstance(f, dict)
        )
        level = str(raw.get("level", "LOW")).upper()
        if level not in _LEVEL_RANK:
            level = _escalate_level(findings)
        created = float(raw.get("created_at", 0) or 0)
        updated = float(raw.get("updated_at", created) or created)
        return Verdict(
            plan_hash=str(raw.get("plan_hash", "")),
            level=level,
            summary=str(raw.get("summary", "")),
            findings=findings,
            created_at=created,
            updated_at=updated,
        )


# \u2500\u2500 Level arithmetic \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _max_level(a: str, b: str) -> str:
    return a if _LEVEL_RANK.get(a, 0) >= _LEVEL_RANK.get(b, 0) else b


def _escalate_level(findings: Sequence[Finding]) -> str:
    level = "LOW"
    for f in findings:
        level = _max_level(level, f.level)
    return level


def _summarise(findings: Sequence[Finding]) -> str:
    if not findings:
        return "No findings."
    counts: dict[str, int] = {lvl: 0 for lvl in LEVELS}
    for f in findings:
        counts[f.level] = counts.get(f.level, 0) + 1
    parts = [f"{counts[lvl]} {lvl.lower()}" for lvl in LEVELS if counts.get(lvl, 0)]
    return f"{len(findings)} finding(s): " + ", ".join(parts)


# \u2500\u2500 Storage \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def _runs_dir() -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNS_DIR


def verdict_path(plan_hash: str) -> Path:
    return _runs_dir() / f"{plan_hash}.verdict.json"


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


def load_verdict(plan_hash: str) -> Verdict | None:
    p = verdict_path(plan_hash)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return Verdict.from_dict(raw)


def save_verdict(verdict: Verdict) -> Path:
    target = verdict_path(verdict.plan_hash)
    _atomic_write(target, json.dumps(verdict.to_dict(), indent=2) + "\n")
    return target


# \u2500\u2500 Finding construction helpers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def parse_evidence(spec: str) -> Evidence:
    """
    Accept 'file', 'file:line', or 'file:line:note' forms. Empty specs yield
    an Evidence with empty file (callers filter those out).
    """
    if not spec:
        return Evidence(file="")
    # Windows paths can contain a drive-letter colon. Parse from the right
    # so the first two colons from the end are the delimiters.
    parts = spec.rsplit(":", 2)
    if len(parts) == 3 and parts[1].isdigit():
        file, line, note = parts
        return Evidence(file=file, line=int(line), note=note)
    parts2 = spec.rsplit(":", 1)
    if len(parts2) == 2 and parts2[1].isdigit():
        return Evidence(file=parts2[0], line=int(parts2[1]))
    return Evidence(file=spec)


def _finding_id(level: str, title: str, agent: str) -> str:
    payload = f"{level}|{agent}|{title}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def build_finding(
    level: str,
    title: str,
    agent: str = "",
    evidence: Iterable[str | Evidence] | None = None,
    rationale: str = "",
    finding_id: str | None = None,
    now: float | None = None,
) -> Finding:
    level_up = level.upper()
    if level_up not in _LEVEL_RANK:
        raise ValueError(
            f"level must be one of {list(LEVELS)}; got {level!r}"
        )
    parsed_evidence: list[Evidence] = []
    for item in evidence or ():
        if isinstance(item, Evidence):
            if item.file:
                parsed_evidence.append(item)
        else:
            ev = parse_evidence(str(item))
            if ev.file:
                parsed_evidence.append(ev)
    return Finding(
        id=finding_id or _finding_id(level_up, title, agent),
        level=level_up,
        title=title,
        agent=agent,
        evidence=tuple(parsed_evidence),
        rationale=rationale,
        created_at=now if now is not None else time.time(),
    )


# \u2500\u2500 Record / merge \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def record_finding(
    plan_hash: str,
    finding: Finding,
    now: float | None = None,
    persist: bool = True,
) -> Verdict:
    """
    Merge a finding into the verdict for ``plan_hash``. Findings with the
    same id replace the previous version (agents can refine). Returns the
    updated verdict; persists by default.
    """
    current_time = now if now is not None else time.time()
    existing = load_verdict(plan_hash)

    if existing is None:
        merged_findings = (finding,)
        created = current_time
    else:
        merged = {f.id: f for f in existing.findings}
        merged[finding.id] = finding
        merged_findings = tuple(merged.values())
        created = existing.created_at

    level = _escalate_level(merged_findings)
    summary = _summarise(merged_findings)

    verdict = Verdict(
        plan_hash=plan_hash,
        level=level,
        summary=summary,
        findings=merged_findings,
        created_at=created,
        updated_at=current_time,
    )
    if persist:
        save_verdict(verdict)
    return verdict


def clear_finding(plan_hash: str, finding_id: str,
                  now: float | None = None,
                  persist: bool = True) -> Verdict | None:
    existing = load_verdict(plan_hash)
    if existing is None:
        return None
    remaining = tuple(f for f in existing.findings if f.id != finding_id)
    if len(remaining) == len(existing.findings):
        return existing  # nothing to do
    current_time = now if now is not None else time.time()
    verdict = replace(
        existing,
        findings=remaining,
        level=_escalate_level(remaining),
        summary=_summarise(remaining),
        updated_at=current_time,
    )
    if persist:
        save_verdict(verdict)
    return verdict


# \u2500\u2500 Retrospective \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def iter_verdicts() -> Iterable[Verdict]:
    """Yield every verdict in RUNS_DIR, sorted by updated_at desc."""
    if not RUNS_DIR.exists():
        return
    entries: list[Verdict] = []
    for p in RUNS_DIR.glob("*.verdict.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        entries.append(Verdict.from_dict(raw))
    entries.sort(key=lambda v: v.updated_at, reverse=True)
    yield from entries


def recent_verdicts(limit: int = 10,
                    min_level: str | None = None) -> tuple[Verdict, ...]:
    out: list[Verdict] = []
    min_rank = _LEVEL_RANK.get((min_level or "LOW").upper(), 0)
    for v in iter_verdicts():
        if _LEVEL_RANK.get(v.level, 0) < min_rank:
            continue
        out.append(v)
        if len(out) >= limit:
            break
    return tuple(out)


# \u2500\u2500 Explainability renderer \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def explain(verdict: Verdict) -> str:
    lines = [
        f"[verdict] plan={verdict.plan_hash}  level={verdict.level}  "
        f"{verdict.summary}",
    ]
    for f in sorted(verdict.findings,
                    key=lambda x: (-_LEVEL_RANK.get(x.level, 0), x.title)):
        head = f"  - [{f.level}] {f.title}"
        if f.agent:
            head += f"  (agent: {f.agent})"
        lines.append(head)
        if f.rationale:
            lines.append(f"      why: {f.rationale}")
        for ev in f.evidence:
            loc = ev.file if ev.line is None else f"{ev.file}:{ev.line}"
            suffix = f" \u2014 {ev.note}" if ev.note else ""
            lines.append(f"      evidence: {loc}{suffix}")
    return "\n".join(lines)


def format_retro(verdicts: Sequence[Verdict]) -> str:
    if not verdicts:
        return "[retro] no verdicts yet."
    header = f"[retro] {len(verdicts)} recent verdict(s):"
    lines = [header]
    for v in verdicts:
        blocks = "BLOCK" if v.blocks else "ok"
        lines.append(
            f"  - {v.plan_hash}  {v.level:<8}  {blocks:<5}  {v.summary}"
        )
    return "\n".join(lines)


# \u2500\u2500 CLI \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def cmd_record(args: argparse.Namespace) -> int:
    try:
        finding = build_finding(
            level=args.level,
            title=args.title,
            agent=args.agent or "",
            evidence=args.evidence or (),
            rationale=args.rationale or "",
            finding_id=args.id,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    verdict = record_finding(args.plan_hash, finding)
    print(json.dumps(verdict.to_dict(), indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    verdict = load_verdict(args.plan_hash)
    if verdict is None:
        print(f"No verdict for plan {args.plan_hash}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        print(explain(verdict))
    return 0


def cmd_retro(args: argparse.Namespace) -> int:
    verdicts = recent_verdicts(limit=args.limit, min_level=args.min_level)
    if args.json:
        print(json.dumps([v.to_dict() for v in verdicts], indent=2))
    else:
        print(format_retro(verdicts))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    verdict = load_verdict(args.plan_hash)
    if verdict is None:
        print(f"No verdict for plan {args.plan_hash}", file=sys.stderr)
        return 1
    print(explain(verdict))
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    verdict = clear_finding(args.plan_hash, args.id)
    if verdict is None:
        print(f"No verdict for plan {args.plan_hash}", file=sys.stderr)
        return 1
    print(json.dumps(verdict.to_dict(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="toolbox_verdict")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("record", help="Record (or refine) a finding")
    sp.add_argument("--plan-hash", required=True)
    sp.add_argument("--level", required=True,
                    choices=[lvl for lvl in LEVELS])
    sp.add_argument("--title", required=True)
    sp.add_argument("--agent", help="Agent that produced the finding")
    sp.add_argument("--evidence", nargs="*",
                    help="Evidence specs: file[:line[:note]]")
    sp.add_argument("--rationale", help="Why this is a finding")
    sp.add_argument("--id",
                    help="Stable finding id (default: hash of level|agent|title)")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("show", help="Show a verdict")
    sp.add_argument("--plan-hash", required=True)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("retro", help="Recent verdicts, most recent first")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--min-level",
                    choices=[lvl for lvl in LEVELS])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_retro)

    sp = sub.add_parser("explain", help="Pretty-print evidence for a verdict")
    sp.add_argument("--plan-hash", required=True)
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("clear", help="Remove one finding from a verdict")
    sp.add_argument("--plan-hash", required=True)
    sp.add_argument("--id", required=True)
    sp.set_defaults(func=cmd_clear)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
