"""ctx_init.py -- One-shot ``ctx-init`` command to bootstrap ~/.claude/ for ctx.

Replaces the legacy ``install.sh`` flow for users who installed via
``pip install claude-ctx``. The goal is a single command that, run
once after installation, produces a working environment:

    $ pip install claude-ctx
    $ ctx-init

What it does:

  1. Ensures ``~/.claude`` + standard subdirectories exist
     (``skills/``, ``agents/``, ``skill-wiki/``, ``skill-quality/``,
     ``backups/``).
  2. Copies the shipped starter config if ``skill-system-config.json``
     is missing (otherwise leaves the user's config alone).
  3. Seeds the starter toolboxes via ``ctx-toolbox init`` if the
     global toolboxes file is empty.
  4. Optionally: injects PostToolUse + Stop hooks via
     ``ctx-install-hooks``. Skipped unless ``--hooks`` is passed so
     the user has to opt in to modifying ``~/.claude/settings.json``.
  5. Optionally: runs the initial graph/wiki build if missing.
     Skipped unless ``--graph`` is passed — building graph from 2k+
     skills is a multi-minute operation and not everyone wants it.

Idempotent: re-running only writes what's missing. Never overwrites
a user's config or hook settings without an explicit ``--force`` flag.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


# ─── Directory layout ───────────────────────────────────────────────────────


_STANDARD_SUBDIRS = (
    "skills",
    "agents",
    "skill-wiki",
    "skill-wiki/entities",
    "skill-wiki/entities/skills",
    "skill-wiki/entities/agents",
    "skill-wiki/concepts",
    "skill-wiki/converted",
    "skill-wiki/graphify-out",
    "skill-quality",
    "backups",
)


def _claude_dir() -> Path:
    return Path(os.path.expanduser("~/.claude"))


def ensure_directories(root: Path | None = None) -> list[Path]:
    """Create standard subdirectories. Returns the list of paths created."""
    claude = root if root is not None else _claude_dir()
    claude.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for sub in _STANDARD_SUBDIRS:
        p = claude / sub
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(p)
    return created


# ─── Config seeding ─────────────────────────────────────────────────────────


_STARTER_USER_CONFIG = """{
  "_comment": "User-level overrides for ctx (claude-ctx) defaults. Edit me.",
  "_config_path": "~/.claude/skill-system-config.json"
}
"""


def seed_user_config(claude: Path, *, force: bool = False) -> Path | None:
    """Write a stub ``skill-system-config.json`` if missing. Returns path if written."""
    target = claude / "skill-system-config.json"
    if target.exists() and not force:
        return None
    target.write_text(_STARTER_USER_CONFIG, encoding="utf-8")
    return target


# ─── Toolbox seeding ────────────────────────────────────────────────────────


def seed_toolboxes(*, force: bool = False) -> int:
    """Invoke ``toolbox init`` to drop the 5 starter templates.

    Returns 0 on success, non-zero on failure. Safe to call when the
    global config already has toolboxes — ``toolbox init`` refuses to
    overwrite without ``--force``.
    """
    cmd = [sys.executable, "-m", "toolbox", "init"]
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


# ─── Hook injection (opt-in) ────────────────────────────────────────────────


def install_hooks(*, ctx_src_dir: Path, settings_path: Path | None = None) -> int:
    """Run ``inject_hooks.main()`` to wire PostToolUse + Stop hooks."""
    target_settings = settings_path or (_claude_dir() / "settings.json")
    cmd = [
        sys.executable, "-m", "ctx.adapters.claude_code.inject_hooks",
        "--settings", str(target_settings),
        "--ctx-dir", str(ctx_src_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


def _resolve_ctx_src_dir() -> Path:
    """Best-guess the directory containing the runtime modules.

    When installed via pip, the modules land in ``<sitepackages>/``. The
    inject_hooks template writes absolute python3 ... paths into the hook
    commands, so we pass in the site-packages directory that *this* file
    lives in.
    """
    return Path(__file__).resolve().parent


# ─── Graph build (opt-in, slow) ─────────────────────────────────────────────


def build_graph() -> int:
    """Run ``wiki_graphify`` to rebuild the knowledge graph."""
    result = subprocess.run(
        [sys.executable, "-m", "ctx.core.wiki.wiki_graphify"],
        capture_output=True, text=True, check=False,
    )
    if result.stdout.strip():
        print(result.stdout.rstrip())
    if result.stderr.strip():
        print(result.stderr.rstrip(), file=sys.stderr)
    return result.returncode


# ─── CLI ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ctx-init",
        description="Bootstrap ~/.claude/ for ctx (claude-ctx).",
    )
    parser.add_argument(
        "--hooks", action="store_true",
        help="Inject PostToolUse + Stop hooks into ~/.claude/settings.json",
    )
    parser.add_argument(
        "--graph", action="store_true",
        help="Rebuild the knowledge graph after setup (slow: >1 minute)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing config files if present",
    )
    args = parser.parse_args(argv)

    claude = _claude_dir()
    print(f"ctx-init: setting up {claude}")

    created = ensure_directories(claude)
    if created:
        print(f"  [ok] created {len(created)} subdirectories")
    else:
        print("  [ok] all standard subdirectories exist")

    seeded_config = seed_user_config(claude, force=args.force)
    if seeded_config:
        print(f"  [ok] wrote {seeded_config.name}")
    else:
        print("  [skip] skill-system-config.json already present (use --force to overwrite)")

    toolbox_rc = seed_toolboxes(force=args.force)
    final_rc = 0
    if toolbox_rc == 0:
        print("  [ok] toolboxes seeded")
    else:
        print(f"  [warn] toolbox init returned {toolbox_rc} — inspect above", file=sys.stderr)

    if args.hooks:
        rc = install_hooks(ctx_src_dir=_resolve_ctx_src_dir())
        if rc == 0:
            print("  [ok] PostToolUse + Stop hooks injected")
        else:
            print(f"  [warn] hook injection returned {rc}", file=sys.stderr)
            final_rc = rc
    else:
        print("  [skip] hook injection (pass --hooks to enable)")

    if args.graph:
        rc = build_graph()
        if rc == 0:
            print("  [ok] knowledge graph rebuilt")
        else:
            print(f"  [warn] graph build returned {rc}", file=sys.stderr)
            if final_rc == 0:
                final_rc = rc
    else:
        print("  [skip] graph build (pass --graph to rebuild)")

    print("\nctx-init: done. Next steps:")
    print("  - ctx-toolbox list                 # see starter toolboxes")
    print("  - ctx-skill-health dashboard       # baseline health scan")
    print("  - ctx-monitor serve                # local dashboard at :8765")
    if not args.hooks:
        print("  - ctx-init --hooks                 # wire live observation")
    if not args.graph:
        print("  - ctx-init --graph                 # build knowledge graph")
    return final_rc


if __name__ == "__main__":
    sys.exit(main())
