#!/usr/bin/env python3
"""
toolbox.py -- CLI for the pre/post dev toolbox feature.

Commands:
  list              List available toolboxes (merged global + per-repo).
  show <name>       Print one toolbox as JSON.
  activate <name>   Add to the active list (global config).
  deactivate <name> Remove from the active list (global config).
  init              Seed global config with the 5 starter templates.
  export <name>     Print a toolbox as standalone YAML for sharing.
  import <path>     Read a YAML file and add as a new toolbox (global).
  validate [path]   Validate a config file (defaults to both layers).

Exit codes:
  0  success
  1  user error (unknown toolbox, missing file, etc.)
  2  schema/validation error

Writes are atomic (tempfile + os.replace). Global config lives at
~/.claude/toolboxes.json; per-repo config at <cwd>/.toolbox.yaml.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from toolbox_config import (
    SCHEMA_VERSION,
    Toolbox,
    ToolboxSet,
    _HAS_YAML,
    _load_json,
    _load_yaml,
    global_config_path,
    load_global,
    load_repo,
    merged,
    repo_config_path,
    save_global,
)

# Location of the starter templates in the dev source tree. When the
# package is installed via pip, this path does NOT exist — we fall back
# to the inlined ``_EMBEDDED_TEMPLATES`` constant below so ``ctx-toolbox
# init`` works out of the box from PyPI without any data-file packaging.
TEMPLATES_DIR = Path(__file__).parent.parent / "docs" / "toolbox" / "templates"

# Starter templates embedded as a constant so the installed wheel is
# self-contained. Kept in sync with docs/toolbox/templates/*.json via
# ``python -m update_repo_stats`` or a manual re-paste when the JSON
# files change — the dev-tree JSON files remain the authoring source.
_EMBEDDED_TEMPLATES: dict[str, dict] = {
    "docs-review": {
        "description": "Documentation pass: accuracy, completeness, clarity, and API parity",
        "pre": ["docs-lookup"],
        "post": ["technical-writer", "docs-architect", "api-documenter", "tutorial-engineer"],
        "scope": {"projects": ["*"], "signals": ["documentation"], "analysis": "diff"},
        "trigger": {"slash": True, "pre_commit": False, "session_end": False, "file_save": "**/*.md"},
        "budget": {"max_tokens": 120000, "max_seconds": 240},
        "dedup": {"window_seconds": 300, "policy": "cached"},
        "guardrail": False,
    },
    "fresh-repo-init": {
        "description": "New-repo bootstrap: run the intent interview, scaffold plan, pick initial toolbox",
        "pre": [],
        "post": ["planner", "architect", "tdd-guide"],
        "scope": {"projects": ["*"], "signals": [], "analysis": "diff"},
        "trigger": {"slash": True, "pre_commit": False, "session_end": False, "file_save": None},
        "budget": {"max_tokens": 100000, "max_seconds": 300},
        "dedup": {"window_seconds": 0, "policy": "fresh"},
        "guardrail": False,
    },
    "refactor-safety": {
        "description": "Graph-informed refactor review with regression and dead-code checks",
        "pre": ["architect-review", "refactor-cleaner"],
        "post": ["architect-review", "refactor-cleaner", "code-reviewer", "test-automator", "dependency-manager"],
        "scope": {"projects": ["*"], "signals": [], "analysis": "graph-blast"},
        "trigger": {"slash": True, "pre_commit": False, "session_end": True, "file_save": None},
        "budget": {"max_tokens": 180000, "max_seconds": 360},
        "dedup": {"window_seconds": 900, "policy": "cached"},
        "guardrail": False,
    },
    "security-sweep": {
        "description": "Full-repo security audit with blocking guardrail on HIGH findings",
        "pre": [],
        "post": ["security-reviewer", "security-auditor", "penetration-tester", "compliance-auditor", "threat-detection-engineer"],
        "scope": {"projects": ["*"], "signals": ["security", "auth", "crypto"], "analysis": "full"},
        "trigger": {"slash": True, "pre_commit": True, "session_end": False, "file_save": "**/auth/**"},
        "budget": {"max_tokens": 300000, "max_seconds": 600},
        "dedup": {"window_seconds": 0, "policy": "fresh"},
        "guardrail": True,
    },
    "ship-it": {
        "description": "Professional council of 7 experts for end-of-feature review",
        "pre": [],
        "post": ["code-reviewer", "security-reviewer", "architect-review", "test-automator", "performance-engineer", "accessibility-tester", "docs-lookup"],
        "scope": {"projects": ["*"], "signals": ["python", "typescript", "rust", "go", "java"], "analysis": "dynamic"},
        "trigger": {"slash": True, "pre_commit": True, "session_end": True, "file_save": None},
        "budget": {"max_tokens": 200000, "max_seconds": 420},
        "dedup": {"window_seconds": 600, "policy": "fresh"},
        "guardrail": False,
    },
}


def _print_err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_template(name: str) -> dict:
    # Prefer the on-disk file in the dev source tree (so edits to the JSON
    # are picked up immediately), then fall back to the embedded copy so
    # installed wheels work without the docs/ subtree.
    path = TEMPLATES_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if name in _EMBEDDED_TEMPLATES:
        return dict(_EMBEDDED_TEMPLATES[name])
    raise FileNotFoundError(f"Template not found: {name}")


def cmd_list(args: argparse.Namespace) -> int:
    tset = merged()
    if not tset.toolboxes:
        print("(no toolboxes configured; run `toolbox.py init` to seed starters)")
        return 0
    active = set(tset.active)
    widest = max((len(n) for n in tset.toolboxes), default=8)
    print(f"{'NAME'.ljust(widest)}  ACTIVE  PRE  POST  DESCRIPTION")
    for name, tb in sorted(tset.toolboxes.items()):
        flag = " yes " if name in active else "  -  "
        print(
            f"{name.ljust(widest)}  {flag}   {len(tb.pre):3d}  "
            f"{len(tb.post):4d}  {tb.description}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    tset = merged()
    tb = tset.toolboxes.get(args.name)
    if tb is None:
        _print_err(f"No such toolbox: {args.name}")
        return 1
    payload = {"name": tb.name, **tb.to_dict()}
    print(json.dumps(payload, indent=2))
    return 0


def _seed_if_empty() -> ToolboxSet:
    tset = load_global()
    if tset.toolboxes:
        return tset
    starters = ["ship-it", "security-sweep", "refactor-safety",
                "docs-review", "fresh-repo-init"]
    for name in starters:
        try:
            raw = _load_template(name)
        except FileNotFoundError as exc:
            _print_err(f"[warn] {exc}")
            continue
        tset = tset.with_toolbox(Toolbox.from_dict(name, raw))
    return tset


def cmd_init(args: argparse.Namespace) -> int:
    tset = load_global()
    if tset.toolboxes and not args.force:
        _print_err(
            f"Global config already has {len(tset.toolboxes)} toolbox(es). "
            f"Use --force to overwrite."
        )
        return 1
    tset = ToolboxSet.empty() if args.force else tset
    starters = ["ship-it", "security-sweep", "refactor-safety",
                "docs-review", "fresh-repo-init"]
    added: list[str] = []
    for name in starters:
        try:
            raw = _load_template(name)
        except FileNotFoundError as exc:
            _print_err(f"[warn] {exc}")
            continue
        tset = tset.with_toolbox(Toolbox.from_dict(name, raw))
        added.append(name)
    save_global(tset)
    print(f"Seeded {len(added)} starter toolbox(es): {', '.join(added)}")
    print(f"Config written to {global_config_path()}")
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    tset = load_global()
    if args.name not in tset.toolboxes:
        _print_err(f"No such toolbox in global config: {args.name}")
        return 1
    tset = tset.activate(args.name)
    save_global(tset)
    print(f"Activated: {args.name}")
    return 0


def cmd_deactivate(args: argparse.Namespace) -> int:
    tset = load_global()
    tset = tset.deactivate(args.name)
    save_global(tset)
    print(f"Deactivated: {args.name}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    tset = merged()
    tb = tset.toolboxes.get(args.name)
    if tb is None:
        _print_err(f"No such toolbox: {args.name}")
        return 1
    if not _HAS_YAML:
        # JSON fallback \u2014 still shareable, still round-trippable via --import
        print(json.dumps({"name": tb.name, **tb.to_dict()}, indent=2))
        return 0
    import yaml  # type: ignore[import-untyped]
    payload = {"version": SCHEMA_VERSION,
               "toolboxes": {tb.name: tb.to_dict()}}
    print(yaml.safe_dump(payload, sort_keys=False), end="")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        _print_err(f"File not found: {path}")
        return 1
    if path.suffix.lower() in {".yaml", ".yml"}:
        if not _HAS_YAML:
            _print_err("PyYAML is required to import .yaml files; pip install pyyaml.")
            return 1
        import yaml  # type: ignore[import-untyped]
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        raw = json.loads(path.read_text(encoding="utf-8"))

    tbs_raw = raw.get("toolboxes") or {}
    if not tbs_raw:
        _print_err("Import file has no 'toolboxes' section.")
        return 2

    tset = load_global()
    added: list[str] = []
    for name, body in tbs_raw.items():
        if name in tset.toolboxes and not args.force:
            _print_err(f"Skip {name}: already exists (use --force to overwrite).")
            continue
        tset = tset.with_toolbox(Toolbox.from_dict(name, body))
        added.append(name)
    save_global(tset)
    print(f"Imported {len(added)} toolbox(es): {', '.join(added) or '(none)'}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    if args.path:
        path = Path(args.path)
        if not path.exists():
            _print_err(f"File not found: {path}")
            return 1
        try:
            if path.suffix.lower() in {".yaml", ".yml"}:
                raw = _load_yaml(path)
            else:
                raw = _load_json(path)
            tset = ToolboxSet.from_dict(raw) if raw else ToolboxSet.empty()
        except ValueError as exc:
            _print_err(f"INVALID: {exc}")
            return 2
        print(f"OK: {len(tset.toolboxes)} toolbox(es), {len(tset.active)} active.")
        return 0

    errors: list[str] = []
    try:
        g = load_global()
        print(f"global ({global_config_path()}): "
              f"{len(g.toolboxes)} toolbox(es)")
    except ValueError as exc:
        errors.append(f"global: {exc}")
    try:
        r = load_repo()
        repo_p = repo_config_path()
        print(f"repo   ({repo_p}): {len(r.toolboxes)} toolbox(es) "
              f"({'exists' if repo_p.exists() else 'absent'})")
    except ValueError as exc:
        errors.append(f"repo: {exc}")
    if errors:
        for e in errors:
            _print_err(f"INVALID: {e}")
        return 2
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Fire a toolbox trigger event — thin alias to toolbox_hooks.run_trigger.

    Exists because the README / docs / playbook all reference
    ``ctx-toolbox run --event pre-commit``, but the trigger runner lives
    in ``toolbox_hooks.py``. Keeping a user-facing ``run`` subcommand
    here means the top-level ``ctx-toolbox`` CLI is the single entry
    point for every toolbox operation.
    """
    from toolbox_hooks import run_trigger  # local import — avoids circular
    file_path = args.file_path or None
    repo_root = Path(args.repo).resolve() if args.repo else None
    return run_trigger(args.event, file_path=file_path, repo_root=repo_root)


def cmd_status(args: argparse.Namespace) -> int:
    """Print a compact status summary — active toolboxes + config paths."""
    tset = merged()
    active = sorted(tset.active)
    print(f"Global config: {global_config_path()}")
    print(f"Repo config:   {repo_config_path()}")
    print(f"Toolboxes:     {len(tset.toolboxes)} total, {len(active)} active")
    if active:
        print(f"Active:        {', '.join(active)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="toolbox", description=__doc__.splitlines()[1])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List available toolboxes").set_defaults(func=cmd_list)
    sub.add_parser("status", help="Show active toolboxes + config paths").set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "run",
        help="Fire a toolbox trigger event (session-start / session-end / pre-commit / file-save / slash)",
    )
    sp.add_argument("--event", required=True,
                    choices=["session-start", "session-end", "pre-commit",
                             "file-save", "slash"],
                    help="Trigger event to fire")
    sp.add_argument("--file-path", default=None,
                    help="File path for file-save events")
    sp.add_argument("--repo", default=None,
                    help="Repo root (defaults to current working dir)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("show", help="Show one toolbox")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("init", help="Seed global config with starter templates")
    sp.add_argument("--force", action="store_true", help="Overwrite existing config")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("activate", help="Mark a toolbox active")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_activate)

    sp = sub.add_parser("deactivate", help="Unmark a toolbox")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_deactivate)

    sp = sub.add_parser("export", help="Print a toolbox as YAML/JSON for sharing")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="Import a toolbox from YAML/JSON")
    sp.add_argument("path")
    sp.add_argument("--force", action="store_true", help="Overwrite on name collision")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("validate", help="Validate config(s)")
    sp.add_argument("path", nargs="?", help="Optional file path; default validates both layers")
    sp.set_defaults(func=cmd_validate)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
