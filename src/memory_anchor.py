#!/usr/bin/env python3
"""
memory_anchor.py -- Diff-aware memory anchoring for the auto-memory store.

Auto-memory notes live under:

    ~/.claude/projects/<slug>/memory/*.md

They accumulate backtick-wrapped file references that rot as the codebase
moves. This module scans each memory file, pulls out references that look
like file paths, and verifies whether each resolves under a repository
root. It then renders a dashboard and, under ``check --strict``, exits
non-zero when any reference is dead.

Reference shapes we recognize (inside backtick code spans only, to avoid
false positives on prose):

    `scan_repo.py`                — bare file with known extension
    `src/foo.py`                  — relative path with slash
    `src/foo.py:42`               — with optional line suffix
    `~/.claude/skills/foo/SKILL.md` — tilde-expanded absolute path

No prose is parsed. If a path isn't in backticks, it isn't a reference.

Usage:
    python src/memory_anchor.py scan
    python src/memory_anchor.py dashboard
    python src/memory_anchor.py check --strict   # exit 2 if dead refs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


# ── Paths & config defaults ────────────────────────────────────────────────


DEFAULT_MEMORY_ROOT = Path(os.path.expanduser("~/.claude/projects"))


KNOWN_EXTENSIONS = frozenset({
    ".py", ".md", ".json", ".yaml", ".yml", ".toml",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".c", ".h", ".cpp", ".hpp",
    ".sh", ".bash", ".zsh", ".fish",
    ".html", ".css", ".scss", ".sass",
    ".sql", ".txt", ".rst", ".ini", ".cfg",
})

# Inline code spans: backtick-delimited, not crossing newlines.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")

# Trailing ``:<line>`` to strip (keep drive letters like ``C:`` intact by
# requiring purely digit tail).
_LINE_TAIL = re.compile(r":(\d+)$")


# ── Data model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AnchorRef:
    raw: str           # exactly what the memory note wrote between backticks
    path: str          # normalised path sans line suffix
    line: int | None   # optional line number if suffix was present
    exists: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MemoryAnchorFile:
    memory_path: str
    refs: tuple[AnchorRef, ...]

    @property
    def live(self) -> tuple[AnchorRef, ...]:
        return tuple(r for r in self.refs if r.exists)

    @property
    def dead(self) -> tuple[AnchorRef, ...]:
        return tuple(r for r in self.refs if not r.exists)

    def to_dict(self) -> dict:
        return {
            "memory_path": self.memory_path,
            "refs": [r.to_dict() for r in self.refs],
            "live": [r.to_dict() for r in self.live],
            "dead": [r.to_dict() for r in self.dead],
        }


@dataclass(frozen=True)
class AnchorReport:
    generated_at: float
    repo_root: str
    memory_root: str
    files: tuple[MemoryAnchorFile, ...]

    @property
    def all_refs(self) -> tuple[AnchorRef, ...]:
        out: list[AnchorRef] = []
        for f in self.files:
            out.extend(f.refs)
        return tuple(out)

    @property
    def dead_count(self) -> int:
        return sum(1 for r in self.all_refs if not r.exists)

    @property
    def live_count(self) -> int:
        return sum(1 for r in self.all_refs if r.exists)

    @property
    def has_dead(self) -> bool:
        return self.dead_count > 0

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "repo_root": self.repo_root,
            "memory_root": self.memory_root,
            "files": [f.to_dict() for f in self.files],
            "totals": {
                "total": len(self.all_refs),
                "live": self.live_count,
                "dead": self.dead_count,
            },
        }


# ── Reference extraction ───────────────────────────────────────────────────


def _looks_like_path(token: str) -> bool:
    """
    Heuristic: token looks like a file path if it has a known extension OR
    contains a slash with a dotted last segment.

    Rejects obvious non-paths: shell-style flags, bare function names like
    ``add_skill()``, URLs, commit hashes.
    """
    t = token.strip()
    if not t or any(ch.isspace() for ch in t):
        return False
    if t.startswith(("-", "http://", "https://")):
        return False
    if t.endswith("()"):
        return False
    stripped = _LINE_TAIL.sub("", t)
    lower = stripped.lower()
    for ext in KNOWN_EXTENSIONS:
        if lower.endswith(ext):
            return True
    if "/" in stripped:
        tail = stripped.rsplit("/", 1)[-1]
        if "." in tail and not tail.startswith("."):
            return True
    return False


def _strip_line_suffix(token: str) -> tuple[str, int | None]:
    m = _LINE_TAIL.search(token)
    if not m:
        return token, None
    return token[: m.start()], int(m.group(1))


def extract_refs(text: str) -> list[tuple[str, str, int | None]]:
    """
    Return a list of (raw, path, line) triples for every backtick span that
    looks like a file path. Preserves document order; deduplicates within
    one document (same raw token scanned once).
    """
    seen: set[str] = set()
    out: list[tuple[str, str, int | None]] = []
    for match in _CODE_SPAN.finditer(text):
        raw = match.group(1).strip()
        if raw in seen:
            continue
        if not _looks_like_path(raw):
            continue
        seen.add(raw)
        path, line = _strip_line_suffix(raw)
        out.append((raw, path, line))
    return out


# ── Resolution ─────────────────────────────────────────────────────────────


def _resolve(path_str: str, repo_root: Path) -> bool:
    """
    A reference resolves if:
      - absolute path (or tilde-expanded) exists, OR
      - joining against repo_root exists, OR
      - joining against repo_root/src exists (common Python convention).
    """
    expanded = os.path.expanduser(path_str)
    p = Path(expanded)
    if p.is_absolute():
        return p.exists()
    candidates = [
        repo_root / expanded,
        repo_root / "src" / expanded,
    ]
    return any(c.exists() for c in candidates)


def scan_memory_file(memory_path: Path,
                     repo_root: Path) -> MemoryAnchorFile:
    try:
        text = memory_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return MemoryAnchorFile(memory_path=str(memory_path), refs=())
    refs: list[AnchorRef] = []
    for raw, path, line in extract_refs(text):
        refs.append(AnchorRef(
            raw=raw,
            path=path,
            line=line,
            exists=_resolve(path, repo_root),
        ))
    return MemoryAnchorFile(memory_path=str(memory_path), refs=tuple(refs))


def _iter_memory_files(memory_root: Path) -> list[Path]:
    if not memory_root.exists():
        return []
    return sorted(memory_root.rglob("*.md"))


# ── Report ─────────────────────────────────────────────────────────────────


def build_report(repo_root: Path,
                 memory_root: Path = DEFAULT_MEMORY_ROOT,
                 now: float | None = None) -> AnchorReport:
    files = tuple(
        scan_memory_file(mf, repo_root)
        for mf in _iter_memory_files(memory_root)
    )
    return AnchorReport(
        generated_at=now if now is not None else time.time(),
        repo_root=str(repo_root),
        memory_root=str(memory_root),
        files=files,
    )


def format_dashboard(report: AnchorReport) -> str:
    total = len(report.all_refs)
    header = (
        f"[memory-anchor] {len(report.files)} memory files   "
        f"refs={total}  live={report.live_count}  "
        f"dead={report.dead_count}"
    )
    lines = [header]
    dead_files = [f for f in report.files if f.dead]
    if not dead_files:
        lines.append("All references resolve.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Dead references:")
    for f in dead_files:
        lines.append(f"  {f.memory_path}")
        for ref in f.dead:
            if ref.line is not None:
                lines.append(f"    - `{ref.raw}` (path={ref.path}, "
                             f"line={ref.line})")
            else:
                lines.append(f"    - `{ref.raw}`")
    return "\n".join(lines)


# ── Repo-root detection ────────────────────────────────────────────────────


def _detect_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for parent in (cur, *cur.parents):
        if (parent / ".git").exists():
            return parent
    return cur


# ── CLI ────────────────────────────────────────────────────────────────────


def _build_cli_report(args: argparse.Namespace) -> AnchorReport:
    repo_root = Path(args.repo_root).resolve() if args.repo_root \
        else _detect_repo_root(Path.cwd())
    memory_root = Path(args.memory_root).expanduser() if args.memory_root \
        else DEFAULT_MEMORY_ROOT
    return build_report(repo_root=repo_root, memory_root=memory_root)


def cmd_scan(args: argparse.Namespace) -> int:
    print(json.dumps(_build_cli_report(args).to_dict(), indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    print(format_dashboard(_build_cli_report(args)))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    report = _build_cli_report(args)
    print(format_dashboard(report))
    if args.strict and report.has_dead:
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory_anchor")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--repo-root", default=None,
                        help="Repository root (default: auto-detect from cwd)")
        sp.add_argument("--memory-root", default=None,
                        help="Memory directory "
                             "(default: ~/.claude/projects)")

    sp = sub.add_parser("scan", help="Emit a JSON anchor report")
    _add_common(sp)
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("dashboard", help="Pretty dashboard to stdout")
    _add_common(sp)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("check",
                        help="Dashboard + nonzero exit on dead refs in --strict")
    _add_common(sp)
    sp.add_argument("--strict", action="store_true")
    sp.set_defaults(func=cmd_check)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
