#!/usr/bin/env python3
"""
toolbox_hooks.py -- Trigger-point handlers for the toolbox system.

This module is the bridge between Claude Code hooks (session-start,
session-end, pre-commit, file-save) and the toolbox's run-plan machinery.

Each handler:
  1. Loads the merged toolbox config.
  2. Filters for toolboxes whose trigger matches the current event.
  3. Builds a run plan via council_runner for each matching toolbox.
  4. Emits a structured line to stdout that Claude Code (or a shell hook)
     can parse and act on.

The handlers are intentionally side-effect-light: they do not invoke agents
themselves. That's Claude Code's job \u2014 these handlers just say "here's what
should run next, and here's the plan file backing it."

Output format (one JSON object per line on stdout):
    {"trigger": "pre_commit", "toolbox": "ship-it", "plan_file": "...json",
     "agents": [...], "files": [...], "source": "fresh"|"cached"}

Return codes:
    0  normal (zero or more plans emitted)
    1  unrecoverable error (corrupt config, etc.)
    2  guardrail block (caller should abort the commit)

Intended callers:
    - .githooks/pre-commit          \u2192 toolbox_hooks.py pre-commit
    - Claude Code session-start     \u2192 toolbox_hooks.py session-start
    - Claude Code session-end       \u2192 toolbox_hooks.py session-end
    - PostToolUse hook for Write    \u2192 toolbox_hooks.py file-save --path X
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from council_runner import build_plan, persist_plan
    from toolbox_config import Toolbox, merged
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).parent))
    from council_runner import build_plan, persist_plan  # type: ignore[no-redef]
    from toolbox_config import Toolbox, merged  # type: ignore[no-redef]


VALID_TRIGGERS = frozenset({"session-start", "session-end", "pre-commit", "file-save"})


@dataclass(frozen=True)
class TriggerEmission:
    trigger: str
    toolbox: str
    plan_file: str
    agents: tuple[str, ...]
    files: tuple[str, ...]
    source: str
    guardrail: bool

    def to_line(self) -> str:
        return json.dumps({
            "trigger": self.trigger,
            "toolbox": self.toolbox,
            "plan_file": self.plan_file,
            "agents": list(self.agents),
            "files": list(self.files),
            "source": self.source,
            "guardrail": self.guardrail,
        })


def _trigger_matches(tb: Toolbox, event: str, file_path: str | None) -> bool:
    t = tb.trigger
    if event == "session-start":
        # Only pre skills are loaded at session start. A toolbox with a non-empty
        # pre list should activate on session-start.
        return bool(tb.pre)
    if event == "session-end":
        return t.session_end
    if event == "pre-commit":
        return t.pre_commit
    if event == "file-save":
        if not t.file_save or not file_path:
            return False
        # Normalize to forward slashes for cross-platform glob compatibility
        normalized = file_path.replace(os.sep, "/")
        return fnmatch.fnmatch(normalized, t.file_save)
    return False


def _select_toolboxes(event: str, file_path: str | None,
                      repo_root: Path | None) -> list[Toolbox]:
    tset = merged(repo_root=repo_root)
    active_set = set(tset.active)
    out: list[Toolbox] = []
    for name in tset.active:
        tb = tset.toolboxes.get(name)
        if tb is None:
            continue
        if _trigger_matches(tb, event, file_path):
            out.append(tb)
    return out


def run_trigger(event: str,
                file_path: str | None = None,
                repo_root: Path | None = None,
                stream=None) -> int:
    """
    Entry point for all trigger events. Returns process exit code.
    Emits one JSON line per matching toolbox.

    ``stream`` defaults to the current ``sys.stdout`` at call time (not at
    import time) so pytest's ``capsys`` and other stdout swaps are honored.
    """
    if stream is None:
        stream = sys.stdout
    if event not in VALID_TRIGGERS:
        print(f"Unknown trigger: {event!r}", file=sys.stderr)
        return 1

    try:
        matching = _select_toolboxes(event, file_path, repo_root)
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    guardrail_violation = False
    for tb in matching:
        try:
            plan = build_plan(
                toolbox_name=tb.name,
                repo_root=repo_root,
            )
        except KeyError:
            continue  # toolbox disappeared between selection and build

        plan_file = str(persist_plan(plan))
        emission = TriggerEmission(
            trigger=event,
            toolbox=tb.name,
            plan_file=plan_file,
            agents=plan.agents,
            files=plan.files,
            source=plan.source,
            guardrail=plan.guardrail,
        )
        print(emission.to_line(), file=stream)

        # For pre-commit + guardrail: record so caller can block.
        # The actual HIGH-finding check happens when Claude runs the council;
        # the emission itself is not a block. Guardrail=True merely flags the
        # toolbox as enforceable, so the wrapping hook knows to honor whatever
        # verdict file the council leaves behind (future phase).
        if event == "pre-commit" and tb.guardrail:
            verdict_file = Path(plan_file).with_suffix(".verdict.json")
            if verdict_file.exists():
                try:
                    verdict = json.loads(verdict_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    verdict = {}
                if verdict.get("level") in {"HIGH", "CRITICAL"}:
                    guardrail_violation = True

    return 2 if guardrail_violation else 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="toolbox_hooks")
    sub = p.add_subparsers(dest="event", required=True)

    for trig in sorted(VALID_TRIGGERS):
        sp = sub.add_parser(trig, help=f"{trig} trigger")
        sp.add_argument("--repo", help="Repo root (default: cwd)")
        if trig == "file-save":
            sp.add_argument("--path", required=True, help="Path to the saved file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    file_path = getattr(args, "path", None)
    repo_root = Path(args.repo) if getattr(args, "repo", None) else None
    return run_trigger(args.event, file_path=file_path, repo_root=repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
