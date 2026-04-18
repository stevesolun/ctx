#!/usr/bin/env python3
"""
behavior_miner.py -- Mine user behaviour signals for toolbox suggestions.

Reads three local sources and produces a BehaviorProfile that powers the
"you keep doing X manually — want to encapsulate it as a toolbox?" surface:

    ~/.claude/intent-log.jsonl       -- per-tool signal hits (file types, langs)
    ~/.claude/skill-manifest.json    -- skill load/unload cadence
    git log                          -- commit-type patterns by convention

Four signal families (all four enabled per spec):

  1. AGENT CO-INVOCATION
     Intent-log doesn't record Agent tool calls directly, so we use co-occurring
     signals as a proxy: signals appearing together in the same event are
     treated as a co-invocation pair. Top pairs = candidate council bundles.

  2. SKILL CADENCE
     From skill-manifest: how often each skill is loaded or unloaded. High
     cadence on a pre-work skill = candidate for a `pre:` slot on a toolbox.

  3. FILE-TYPE SIGNALS
     Aggregate intent-log signals by language/framework token. Dominant
     file types hint at which scope.signals the user's default toolbox
     should target.

  4. COMMIT-TYPE SIGNALS
     Parse Conventional Commit prefixes (feat / fix / refactor / docs /
     test / chore / perf / ci / security) from git log. A high rate of
     one type suggests a toolbox tuned to that workflow (e.g. "docs-review"
     if docs commits dominate).

The profile is a *snapshot*. The intent is: run this on session-end (or
on demand via `python behavior_miner.py profile`), persist to
~/.claude/user-profile.json, and let the suggestion surface show deltas.

Suggestion logic is intentionally conservative: a signal must have at
least MIN_EVIDENCE occurrences before it nominates a toolbox. This
keeps the suggestion feed from noise-spamming on a fresh install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Iterable


INTENT_LOG = Path(os.path.expanduser("~/.claude/intent-log.jsonl"))
SKILL_MANIFEST = Path(os.path.expanduser("~/.claude/skill-manifest.json"))
USER_PROFILE = Path(os.path.expanduser("~/.claude/user-profile.json"))

MIN_EVIDENCE = 3          # minimum hits before a signal nominates a toolbox
MAX_PAIRS = 10            # cap on stored co-invocation pairs
MAX_SKILLS = 20           # cap on stored skill cadence entries
MAX_FILE_TYPES = 20       # cap on stored file-type entries

# Conventional Commit types we recognize. Anything else lands in "other".
COMMIT_TYPES = frozenset(
    {"feat", "fix", "refactor", "docs", "test", "chore",
     "perf", "ci", "security", "style", "build"}
)
_COMMIT_PREFIX_RE = re.compile(r"^(?P<type>[a-z]+)(\([^)]+\))?(!)?:\s", re.IGNORECASE)


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CoInvocationPair:
    a: str
    b: str
    count: int


@dataclass(frozen=True)
class Suggestion:
    kind: str          # "co-invocation" | "skill-cadence" | "file-type" | "commit-type"
    rationale: str     # human-readable justification
    evidence: int      # count backing this suggestion
    proposed: dict     # proposed Toolbox dict (name + key fields)


@dataclass(frozen=True)
class BehaviorProfile:
    total_intent_events: int
    total_commits: int
    co_invocation_pairs: tuple[CoInvocationPair, ...]
    skill_cadence: tuple[tuple[str, int], ...]       # (skill_name, count)
    file_types: tuple[tuple[str, int], ...]          # (token, count)
    commit_types: tuple[tuple[str, int], ...]        # (type, count)
    suggestions: tuple[Suggestion, ...]
    generated_at: float

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict() turns frozen dataclasses into dicts already; suggestions
        # come back as dicts too, which is what we want for JSON.
        return d


# ── Source readers ──────────────────────────────────────────────────────────


def _iter_intent_events(path: Path | None = None) -> Iterable[dict]:
    """Yield each JSONL row from the intent log. Silent on missing/corrupt."""
    p = path or INTENT_LOG
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _read_skill_manifest(path: Path | None = None) -> dict:
    p = path or SKILL_MANIFEST
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError:
        return {}


def _git_commit_types(repo_root: Path, limit: int = 500) -> list[str]:
    """
    Return commit-type tokens for up to ``limit`` recent commits.
    Anything that doesn't match the Conventional Commit prefix lands as
    "other". Silent on missing git / not-a-repo.
    """
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "log",
             f"--max-count={limit}", "--pretty=format:%s"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    types: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _COMMIT_PREFIX_RE.match(line)
        if m:
            t = m.group("type").lower()
            types.append(t if t in COMMIT_TYPES else "other")
        else:
            types.append("other")
    return types


# ── Signal extraction ───────────────────────────────────────────────────────


def _extract_co_invocation(events: Iterable[dict]) -> Counter:
    """
    Count unordered pairs of signals that co-occur in the same event.
    Pairs like ("docker","python") are emitted once per event, sorted so
    the Counter keys are deterministic regardless of input order.
    """
    pairs: Counter = Counter()
    for ev in events:
        sigs = sorted({s for s in ev.get("signals", []) if isinstance(s, str)})
        if len(sigs) < 2:
            continue
        for a, b in combinations(sigs, 2):
            pairs[(a, b)] += 1
    return pairs


def _extract_file_types(events: Iterable[dict]) -> Counter:
    """Flatten all signal strings into a frequency counter."""
    c: Counter = Counter()
    for ev in events:
        for s in ev.get("signals", []) or []:
            if isinstance(s, str) and s:
                c[s] += 1
    return c


def _extract_skill_cadence(manifest: dict) -> Counter:
    c: Counter = Counter()
    for entry in manifest.get("load", []) or []:
        name = entry.get("skill") if isinstance(entry, dict) else None
        if name:
            c[name] += 1
    for entry in manifest.get("unload", []) or []:
        name = entry.get("skill") if isinstance(entry, dict) else None
        if name:
            c[name] += 1
    return c


# ── Suggestion logic ────────────────────────────────────────────────────────


def _suggest_from_co_invocation(pairs: Counter) -> list[Suggestion]:
    out: list[Suggestion] = []
    for (a, b), count in pairs.most_common(5):
        if count < MIN_EVIDENCE:
            break
        out.append(Suggestion(
            kind="co-invocation",
            rationale=(
                f"Signals {a!r} and {b!r} co-occurred {count}x. "
                "Consider a toolbox that always loads both domains together."
            ),
            evidence=count,
            proposed={
                "name": f"{a}-{b}-bundle",
                "description": f"Auto-suggested bundle for {a} + {b} work",
                "pre": [],
                "post": [],
                "scope": {"signals": [a, b], "analysis": "dynamic"},
            },
        ))
    return out


def _suggest_from_commit_types(types: Counter) -> list[Suggestion]:
    out: list[Suggestion] = []
    total = sum(types.values())
    if total == 0:
        return out
    for t, count in types.most_common(3):
        if t in {"other", "chore"}:
            continue
        if count < MIN_EVIDENCE:
            break
        share = count / total
        if share < 0.15:  # ignore long-tail types
            continue
        out.append(Suggestion(
            kind="commit-type",
            rationale=(
                f"{count} of last {total} commits were {t!r} "
                f"({share:.0%}). A tuned {t!r} toolbox may cut review time."
            ),
            evidence=count,
            proposed={
                "name": f"{t}-review",
                "description": f"Auto-suggested council for {t} commits",
                "pre": [],
                "post": [],
                "scope": {"analysis": "diff"},
                "trigger": {"pre_commit": True},
            },
        ))
    return out


def _suggest_from_skill_cadence(cadence: Counter) -> list[Suggestion]:
    out: list[Suggestion] = []
    for skill, count in cadence.most_common(5):
        if count < MIN_EVIDENCE:
            break
        out.append(Suggestion(
            kind="skill-cadence",
            rationale=(
                f"Skill {skill!r} was loaded/unloaded {count}x. "
                "Pinning it to a toolbox's pre list avoids repeated toggling."
            ),
            evidence=count,
            proposed={
                "name": f"{skill}-default",
                "description": f"Auto-suggested pre-load for {skill}",
                "pre": [skill],
                "post": [],
                "scope": {"analysis": "dynamic"},
            },
        ))
    return out


def _suggest_from_file_types(file_types: Counter) -> list[Suggestion]:
    out: list[Suggestion] = []
    for token, count in file_types.most_common(3):
        if count < MIN_EVIDENCE:
            break
        out.append(Suggestion(
            kind="file-type",
            rationale=(
                f"Signal {token!r} hit {count}x across tool uses. "
                "A toolbox scoped to this signal could cover most sessions."
            ),
            evidence=count,
            proposed={
                "name": f"{token}-default",
                "description": f"Auto-suggested default for {token} work",
                "pre": [],
                "post": [],
                "scope": {"signals": [token], "analysis": "dynamic"},
            },
        ))
    return out


# ── Profile assembly ────────────────────────────────────────────────────────


def build_profile(
    repo_root: Path | None = None,
    intent_log: Path | None = None,
    skill_manifest: Path | None = None,
    now: float | None = None,
) -> BehaviorProfile:
    import time as _time  # local import so callers can monkeypatch cheaply

    events = list(_iter_intent_events(intent_log))
    manifest = _read_skill_manifest(skill_manifest)
    commit_types = _git_commit_types(repo_root or Path.cwd())

    pairs = _extract_co_invocation(events)
    file_types = _extract_file_types(events)
    cadence = _extract_skill_cadence(manifest)
    commit_counter = Counter(commit_types)

    co_list = tuple(
        CoInvocationPair(a=a, b=b, count=n)
        for (a, b), n in pairs.most_common(MAX_PAIRS)
    )
    skill_list = tuple(cadence.most_common(MAX_SKILLS))
    file_list = tuple(file_types.most_common(MAX_FILE_TYPES))
    commit_list = tuple(commit_counter.most_common())

    suggestions: list[Suggestion] = []
    suggestions.extend(_suggest_from_co_invocation(pairs))
    suggestions.extend(_suggest_from_commit_types(commit_counter))
    suggestions.extend(_suggest_from_skill_cadence(cadence))
    suggestions.extend(_suggest_from_file_types(file_types))
    # Stable order: highest evidence first, then kind for ties.
    suggestions.sort(key=lambda s: (-s.evidence, s.kind))

    return BehaviorProfile(
        total_intent_events=len(events),
        total_commits=len(commit_types),
        co_invocation_pairs=co_list,
        skill_cadence=skill_list,
        file_types=file_list,
        commit_types=commit_list,
        suggestions=tuple(suggestions),
        generated_at=(now if now is not None else _time.time()),
    )


# ── Persistence ─────────────────────────────────────────────────────────────


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


def save_profile(profile: BehaviorProfile, path: Path | None = None) -> Path:
    target = path or USER_PROFILE
    _atomic_write(target, json.dumps(profile.to_dict(), indent=2) + "\n")
    return target


def load_profile(path: Path | None = None) -> BehaviorProfile | None:
    """Return the persisted profile, or None if unavailable/corrupt."""
    p = path or USER_PROFILE
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    try:
        return BehaviorProfile(
            total_intent_events=int(raw.get("total_intent_events", 0)),
            total_commits=int(raw.get("total_commits", 0)),
            co_invocation_pairs=tuple(
                CoInvocationPair(a=d["a"], b=d["b"], count=int(d["count"]))
                for d in raw.get("co_invocation_pairs", []) or []
                if isinstance(d, dict) and "a" in d and "b" in d
            ),
            skill_cadence=tuple(
                (str(item[0]), int(item[1]))
                for item in raw.get("skill_cadence", []) or []
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            file_types=tuple(
                (str(item[0]), int(item[1]))
                for item in raw.get("file_types", []) or []
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            commit_types=tuple(
                (str(item[0]), int(item[1]))
                for item in raw.get("commit_types", []) or []
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            suggestions=tuple(
                Suggestion(
                    kind=str(d["kind"]),
                    rationale=str(d["rationale"]),
                    evidence=int(d["evidence"]),
                    proposed=dict(d.get("proposed", {}) or {}),
                )
                for d in raw.get("suggestions", []) or []
                if isinstance(d, dict) and "kind" in d
            ),
            generated_at=float(raw.get("generated_at", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ── Digest rendering ────────────────────────────────────────────────────────


def format_digest(profile: BehaviorProfile, limit: int = 5) -> str:
    """
    Short, human-readable digest for the real-time suggestion surface.
    Designed to fit in a status line or a session-end summary block.
    """
    if not profile.suggestions:
        if profile.total_intent_events == 0 and profile.total_commits == 0:
            return "[toolbox] no behaviour yet — keep working, suggestions will appear."
        return "[toolbox] no new suggestions."

    lines = [f"[toolbox] {len(profile.suggestions)} suggestion(s):"]
    for s in profile.suggestions[:limit]:
        name = s.proposed.get("name", "?")
        lines.append(f"  - {name} ({s.kind}, {s.evidence}x): {s.rationale}")
    if len(profile.suggestions) > limit:
        lines.append(f"  ...and {len(profile.suggestions) - limit} more.")
    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────────────


def cmd_profile(args: argparse.Namespace) -> int:
    profile = build_profile(
        repo_root=Path(args.repo) if args.repo else None,
    )
    if args.save:
        save_profile(profile)
    print(json.dumps(profile.to_dict(), indent=2))
    return 0


def cmd_suggest(args: argparse.Namespace) -> int:
    profile = build_profile(
        repo_root=Path(args.repo) if args.repo else None,
    )
    if args.save:
        save_profile(profile)
    print(format_digest(profile, limit=args.limit))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="behavior_miner")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("profile", help="Mine behaviour and emit full JSON profile")
    sp.add_argument("--repo", help="Repo root for commit-type mining (default: cwd)")
    sp.add_argument("--save", action="store_true",
                    help="Persist to ~/.claude/user-profile.json")
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("suggest",
                        help="Print a short toolbox-suggestion digest")
    sp.add_argument("--repo", help="Repo root (default: cwd)")
    sp.add_argument("--save", action="store_true",
                    help="Also persist the underlying profile")
    sp.add_argument("--limit", type=int, default=5,
                    help="Max suggestions to show (default: 5)")
    sp.set_defaults(func=cmd_suggest)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
