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
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


# ─── Directory layout ───────────────────────────────────────────────────────


_STANDARD_SUBDIRS = (
    "skills",
    "agents",
    "skill-wiki",
    "skill-wiki/entities",
    "skill-wiki/entities/skills",
    "skill-wiki/entities/agents",
    "skill-wiki/entities/mcp-servers",
    "skill-wiki/entities/harnesses",
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


# ─── Model onboarding ───────────────────────────────────────────────────────


_MODEL_PROFILE_NAME = "ctx-model-profile.json"

_PROVIDER_KEY_ENV: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "together": "TOGETHER_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "",
}


def _model_provider_prefix(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else model


def _resolve_api_key_env(
    explicit: str | None,
    model: str | None,
    provider: str | None,
) -> str | None:
    if explicit is not None:
        return explicit or None
    prefix = provider or (_model_provider_prefix(model) if model else "")
    env_name = _PROVIDER_KEY_ENV.get(prefix, "")
    return env_name or None


def write_model_profile(
    claude: Path,
    profile: dict[str, Any],
    *,
    force: bool = False,
) -> Path | None:
    """Write the user's ctx model/onboarding profile if allowed."""
    target = claude / _MODEL_PROFILE_NAME
    if target.exists() and not force:
        return None
    target.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return target


def recommend_harnesses(goal: str, *, top_k: int = 5) -> list[dict[str, Any]]:
    """Return harness recommendations from the shared recommendation API."""
    if not goal.strip():
        return []
    try:
        from ctx.api import recommend_bundle  # noqa: PLC0415

        results = recommend_bundle(goal, top_k=top_k * 3)
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [warn] harness recommendation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return []
    return [row for row in results if row.get("type") == "harness"][:top_k]


def validate_model_connection(
    *,
    model: str,
    api_key_env: str | None,
    base_url: str | None,
) -> int:
    """Make one tiny provider call when the user explicitly asks."""
    try:
        from ctx.adapters.generic.providers import Message, get_provider  # noqa: PLC0415

        client = get_provider(
            default_model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout=30.0,
        )
        client.complete(
            [Message(role="user", content="Reply exactly: ctx-ok")],
            model=model,
            temperature=0.0,
            max_tokens=8,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [warn] model validation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    return 0


def _prompt_model_mode() -> str:
    answer = input(
        "Use Claude Code or a custom model with ctx? "
        "[claude-code/custom/skip] "
    ).strip().lower()
    return answer or "claude-code"


def run_model_onboarding(args: argparse.Namespace, claude: Path) -> int:
    """Record model choice and print harness recommendations."""
    mode = args.model_mode
    if mode is None and sys.stdin.isatty():
        mode = _prompt_model_mode()
    if mode is None or mode == "skip":
        print("  [skip] model onboarding (pass --model-mode to configure)")
        return 0
    if mode not in {"claude-code", "custom"}:
        print(f"  [warn] unknown model mode: {mode}", file=sys.stderr)
        return 1

    goal = args.goal or ""
    if mode == "custom" and not args.model:
        print("  [warn] --model-mode custom requires --model", file=sys.stderr)
        return 1

    provider = args.model_provider or (
        _model_provider_prefix(args.model) if args.model else None
    )
    api_key_env = _resolve_api_key_env(args.api_key_env, args.model, provider)
    profile: dict[str, Any] = {
        "mode": mode,
        "provider": provider,
        "model": args.model,
        "api_key_env": api_key_env,
        "base_url": args.base_url,
        "goal": goal,
    }
    written = write_model_profile(claude, profile, force=args.force)
    if written:
        print(f"  [ok] wrote {written.name}")
    else:
        print(f"  [skip] {_MODEL_PROFILE_NAME} already present (use --force)")

    rc = 0
    if mode == "custom" and api_key_env and not os.environ.get(api_key_env):
        print(f"  [warn] set {api_key_env} before running ctx with this model")
    if mode == "custom" and args.validate_model:
        rc = validate_model_connection(
            model=args.model,
            api_key_env=api_key_env,
            base_url=args.base_url,
        )
        if rc == 0:
            print("  [ok] model connection validated")

    recommendation_query = " ".join(
        part for part in [goal, provider or "", args.model or "", "harness"]
        if part
    )
    harnesses = recommend_harnesses(recommendation_query)
    if harnesses:
        print("  [ok] recommended harnesses:")
        for row in harnesses:
            score = float(row.get("score") or 0.0)
            print(f"       - {row.get('name')} ({score:.2f})")
    elif goal or mode == "custom":
        print("  [info] no harness recommendations matched yet")
    return rc


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
    parser.add_argument(
        "--model-mode",
        choices=("claude-code", "custom", "skip"),
        help="Record whether this install uses Claude Code or a custom model",
    )
    parser.add_argument("--model-provider", help="Custom model provider prefix")
    parser.add_argument("--model", help="Custom model slug, e.g. openai/gpt-5.5")
    parser.add_argument(
        "--api-key-env",
        help="Environment variable that stores the custom provider API key",
    )
    parser.add_argument("--base-url", help="Custom provider base URL")
    parser.add_argument(
        "--goal",
        help="What the user wants to build; used for harness recommendations",
    )
    parser.add_argument(
        "--validate-model",
        action="store_true",
        help="Make one tiny provider call to validate the custom model connection",
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

    rc = run_model_onboarding(args, claude)
    if rc != 0 and final_rc == 0:
        final_rc = rc

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
