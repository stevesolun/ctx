#!/usr/bin/env python3
"""
council_runner.py -- Plan and dispatch a post-toolbox council run.

This module does *not* invoke agents itself. It produces an audit-ready
"run plan" that a Claude Code session (or CI wrapper) uses to dispatch the
agents listed in the toolbox. The runner is responsible for:

  1. Resolving the toolbox (merged global + per-repo config).
  2. Computing scope: diff-only, full-repo, graph-blast, or dynamic.
  3. Honoring dedup: skip if a matching plan ran recently ("cached" policy),
     or always produce a new plan ("fresh" policy).
  4. Enforcing budget caps on the produced plan.
  5. Persisting the plan + outcome under ~/.claude/toolbox-runs/<hash>.json
     so that dedup can detect prior identical runs.

Why a plan-not-exec design:
- Agent invocation requires Claude Code's agent subsystem, not a subprocess.
- A plan file is inspectable, cache-friendly, and CI-replayable.
- Separates "what should run" (deterministic, testable) from "run it"
  (stateful, session-scoped).

CLI:
  python council_runner.py plan --toolbox ship-it [--files a.py b.py]
  python council_runner.py history [--toolbox ship-it] [--limit 10]
  python council_runner.py purge [--older-than-days 30]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

try:
    from toolbox_config import Toolbox, merged
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from toolbox_config import Toolbox, merged  # type: ignore[no-redef]


RUNS_DIR = Path(os.path.expanduser("~/.claude/toolbox-runs"))


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunPlan:
    toolbox: str
    agents: tuple[str, ...]
    files: tuple[str, ...]
    scope_mode: str
    budget_tokens: int
    budget_seconds: int
    guardrail: bool
    created_at: float
    plan_hash: str
    source: str  # "fresh" | "cached"

    def to_dict(self) -> dict:
        return asdict(self)


# ── Scope computation ───────────────────────────────────────────────────────


def _git_diff_files(repo_root: Path, base: str = "HEAD") -> list[str]:
    """Return files changed vs ``base``, relative to repo_root. Empty on error."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "diff", "--name-only", base],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _all_tracked_files(repo_root: Path) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), "ls-files"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _graph_blast_files(
    changed: Iterable[str],
    graph_edges: dict[str, set[str]] | None = None,
) -> list[str]:
    """
    Expand the change set by one hop via graph edges (file -> related files).
    If no graph is available, falls back to the changed set verbatim.
    """
    changed_set = set(changed)
    if not graph_edges:
        return sorted(changed_set)
    expanded = set(changed_set)
    for src in changed_set:
        expanded.update(graph_edges.get(src, set()))
    return sorted(expanded)


def resolve_scope(
    tb: Toolbox,
    repo_root: Path,
    explicit_files: list[str] | None = None,
    graph_edges: dict[str, set[str]] | None = None,
) -> tuple[list[str], str]:
    """
    Returns (files_to_review, effective_mode).
    explicit_files short-circuits everything and forces diff-mode semantics.
    """
    if explicit_files:
        return sorted(set(explicit_files)), "explicit"

    mode = tb.scope.analysis
    if mode == "full":
        return _all_tracked_files(repo_root), "full"

    diff = _git_diff_files(repo_root)
    if mode == "diff":
        return diff, "diff"
    if mode == "graph-blast":
        return _graph_blast_files(diff, graph_edges), "graph-blast"

    # "dynamic": diff by default, escalate to graph-blast if diff is tiny,
    # escalate to full if diff is empty (initial commit / no HEAD).
    if not diff:
        return _all_tracked_files(repo_root), "dynamic:full"
    if len(diff) <= 3 and graph_edges:
        return _graph_blast_files(diff, graph_edges), "dynamic:graph-blast"
    return diff, "dynamic:diff"


# ── Plan hashing + dedup cache ──────────────────────────────────────────────


def _hash_plan(toolbox: str, agents: tuple[str, ...],
               files: tuple[str, ...], scope_mode: str) -> str:
    payload = json.dumps(
        {"toolbox": toolbox, "agents": list(agents),
         "files": list(files), "scope_mode": scope_mode},
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _runs_dir() -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return RUNS_DIR


def _find_cached_plan(plan_hash: str, window_seconds: int,
                      now: float | None = None) -> RunPlan | None:
    target = _runs_dir() / f"{plan_hash}.json"
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if window_seconds > 0:
        current = now if now is not None else time.time()
        age = current - float(raw.get("created_at", 0))
        if age > window_seconds:
            return None
    return _plan_from_dict(raw, source="cached")


def _plan_from_dict(raw: dict, source: str) -> RunPlan:
    return RunPlan(
        toolbox=str(raw["toolbox"]),
        agents=tuple(raw.get("agents", ())),
        files=tuple(raw.get("files", ())),
        scope_mode=str(raw.get("scope_mode", "unknown")),
        budget_tokens=int(raw.get("budget_tokens", 0)),
        budget_seconds=int(raw.get("budget_seconds", 0)),
        guardrail=bool(raw.get("guardrail", False)),
        created_at=float(raw.get("created_at", 0)),
        plan_hash=str(raw.get("plan_hash", "")),
        source=source,
    )


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


def persist_plan(plan: RunPlan) -> Path:
    target = _runs_dir() / f"{plan.plan_hash}.json"
    _atomic_write(target, json.dumps(plan.to_dict(), indent=2) + "\n")
    return target


# ── Plan construction ───────────────────────────────────────────────────────


def build_plan(
    toolbox_name: str,
    repo_root: Path | None = None,
    explicit_files: list[str] | None = None,
    graph_edges: dict[str, set[str]] | None = None,
    now: float | None = None,
) -> RunPlan:
    """
    Resolve the named toolbox, compute scope, honor dedup, return a RunPlan.
    Raises KeyError if the toolbox is unknown.
    """
    tset = merged(repo_root=repo_root)
    if toolbox_name not in tset.toolboxes:
        raise KeyError(f"Unknown toolbox: {toolbox_name!r}")
    tb = tset.toolboxes[toolbox_name]

    root = repo_root or Path.cwd()
    files, effective_mode = resolve_scope(
        tb, root, explicit_files=explicit_files, graph_edges=graph_edges,
    )
    files_tuple = tuple(files)
    agents = tuple(tb.post)
    plan_hash = _hash_plan(tb.name, agents, files_tuple, effective_mode)

    timestamp = now if now is not None else time.time()

    if tb.dedup.policy == "cached":
        cached = _find_cached_plan(plan_hash, tb.dedup.window_seconds,
                                   now=timestamp)
        if cached is not None:
            return cached

    return RunPlan(
        toolbox=tb.name,
        agents=agents,
        files=files_tuple,
        scope_mode=effective_mode,
        budget_tokens=tb.budget.max_tokens,
        budget_seconds=tb.budget.max_seconds,
        guardrail=tb.guardrail,
        created_at=timestamp,
        plan_hash=plan_hash,
        source="fresh",
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def cmd_plan(args: argparse.Namespace) -> int:
    try:
        plan = build_plan(
            toolbox_name=args.toolbox,
            repo_root=Path(args.repo) if args.repo else None,
            explicit_files=args.files or None,
        )
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if plan.source == "fresh" and not args.dry_run:
        persist_plan(plan)

    print(json.dumps(plan.to_dict(), indent=2))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    rd = _runs_dir()
    entries: list[dict] = []
    for p in rd.glob("*.json"):
        try:
            entries.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    if args.toolbox:
        entries = [e for e in entries if e.get("toolbox") == args.toolbox]
    entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    entries = entries[: args.limit]
    print(json.dumps(entries, indent=2))
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    rd = _runs_dir()
    cutoff = time.time() - (args.older_than_days * 86400)
    removed = 0
    for p in rd.glob("*.json"):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Corrupt file \u2014 remove
            p.unlink(missing_ok=True)
            removed += 1
            continue
        if float(raw.get("created_at", 0)) < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    print(f"Purged {removed} run(s) older than {args.older_than_days}d")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="council_runner")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="Build (or look up cached) a run plan")
    sp.add_argument("--toolbox", required=True)
    sp.add_argument("--files", nargs="*", help="Override scope with explicit files")
    sp.add_argument("--repo", help="Repo root (default: cwd)")
    sp.add_argument("--dry-run", action="store_true",
                    help="Do not persist the plan to ~/.claude/toolbox-runs/")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("history", help="List recent run plans")
    sp.add_argument("--toolbox", help="Filter by toolbox name")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("purge", help="Delete stale run plans")
    sp.add_argument("--older-than-days", type=int, default=30)
    sp.set_defaults(func=cmd_purge)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
