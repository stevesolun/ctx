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
  4. In a terminal, guides first-time users through hooks, graph build,
     model profile, and harness recommendation setup. Automation can
     keep the non-interactive path by passing explicit flags such as
     ``--model-mode skip``; ``--wizard`` forces the prompts.
  5. Optionally: injects PostToolUse + Stop hooks via
     ``ctx-install-hooks``. Skipped unless the wizard or ``--hooks`` asks
     for it, so the user has to opt in to modifying
     ``~/.claude/settings.json``.
  6. Optionally: runs the initial graph/wiki build if missing.
     Skipped unless the wizard or ``--graph`` asks for it — building
     graph from 2k+ skills is a multi-minute operation and not everyone
     wants it.

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


def recommend_harnesses(
    goal: str,
    *,
    top_k: int = 5,
    model_provider: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Return high-confidence harness catalog recommendations."""
    if not goal.strip():
        return []
    try:
        from ctx.core.resolve.recommendations import (  # noqa: PLC0415
            query_to_tags,
            recommend_by_tags,
        )
        from ctx_config import cfg  # noqa: PLC0415

        graph = _load_recommendation_graph()
        if graph.number_of_nodes() == 0:
            return []
        limit = max(1, min(int(top_k), cfg.recommendation_top_k))
        candidate_limit = max(limit * 4, 25)
        results = recommend_by_tags(
            graph,
            query_to_tags(goal),
            top_n=candidate_limit,
            query=goal,
            entity_types=("harness",),
            min_normalized_score=0.0,
        )
        results = [
            row for row in results
            if _harness_supports_provider(
                graph,
                str(row.get("name") or ""),
                model_provider,
                model=model,
            )
        ]
        installed = _installed_harness_slugs(cfg.claude_dir / "harness-installs")
        if installed:
            results = [
                row for row in results
                if str(row.get("name") or "") not in installed
            ]
        threshold = cfg.harness_recommendation_min_normalized_score
        for row in results:
            score = float(row.get("score") or 0.0)
            row.setdefault("normalized_score", round(min(max(score, 0.0), 1.0), 4))
        if results:
            results = [
                row for row in results
                if float(row.get("normalized_score") or 0.0) >= threshold
            ]
    except Exception as exc:  # noqa: BLE001
        print(
            f"  [warn] harness recommendation failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return []
    return results[:limit]


def _installed_harness_slugs(manifest_dir: Path) -> set[str]:
    """Return harness slugs with an active install manifest."""
    if not manifest_dir.exists():
        return set()
    slugs: set[str] = set()
    for path in manifest_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupt manifests should not break onboarding.
            continue
        if str(data.get("status") or "installed") != "installed":
            continue
        slug = str(data.get("slug") or path.stem).strip()
        if slug:
            slugs.add(slug)
    return slugs


def _harness_supports_provider(
    graph: Any,
    slug: str,
    model_provider: str | None,
    *,
    model: str | None = None,
) -> bool:
    """Return true when a harness is compatible with the requested provider."""
    requested = _provider_match_candidates(model_provider, model)
    if not requested:
        return True
    providers = _harness_model_providers_from_graph(graph, slug)
    if not providers:
        providers = _harness_model_providers_from_wiki(slug)
    if not providers:
        return True
    if providers.intersection({"model-agnostic", "any", "all", "litellm"}):
        return True
    return bool(requested & providers)


def _provider_match_candidates(
    model_provider: str | None,
    model: str | None,
) -> set[str]:
    providers = {
        candidate for candidate in (
            _normalise_model_provider(model_provider),
            _normalise_model_provider(_model_provider_prefix(model or "")),
        ) if candidate
    }
    parts = [part for part in (model or "").split("/") if part]
    if parts and _normalise_model_provider(parts[0]) in {"openrouter", "litellm"}:
        providers.update(
            _normalise_model_provider(part)
            for part in parts[1:2]
            if _normalise_model_provider(part)
        )
    return providers


def _normalise_model_provider(value: str | None) -> str:
    provider = (value or "").strip().lower()
    if not provider:
        return ""
    aliases = {
        "azure": "azure-openai",
        "azure_openai": "azure-openai",
        "googleai": "google",
        "gemini": "google",
        "local": "ollama",
        "model_agnostic": "model-agnostic",
        "model agnostic": "model-agnostic",
    }
    return aliases.get(provider, provider)


def _normalise_model_providers(raw: object) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        values = [str(item) for item in raw]
    else:
        return set()
    return {
        provider for value in values
        if (provider := _normalise_model_provider(value))
    }


def _harness_model_providers_from_graph(graph: Any, slug: str) -> set[str]:
    for _node_id, data in graph.nodes(data=True):
        if str(data.get("type")) != "harness":
            continue
        if str(data.get("label") or "") != slug:
            continue
        return _normalise_model_providers(data.get("model_providers"))
    return set()


def _harness_model_providers_from_wiki(slug: str) -> set[str]:
    try:
        from ctx.core.entity_types import entity_page_path  # noqa: PLC0415
        from ctx.core.wiki.wiki_utils import parse_frontmatter_and_body  # noqa: PLC0415
        from ctx_config import cfg  # noqa: PLC0415

        path = entity_page_path(cfg.wiki_dir, "harness", slug)
        if path is None or not path.is_file():
            return set()
        fm, _body = parse_frontmatter_and_body(
            path.read_text(encoding="utf-8", errors="replace"),
        )
        return _normalise_model_providers(fm.get("model_providers"))
    except Exception:
        return set()


def _load_recommendation_graph() -> Any:
    """Load the ctx knowledge graph for harness onboarding."""
    from ctx.core.graph.resolve_graph import load_graph  # noqa: PLC0415

    return load_graph()


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


def _prompt_yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{prompt} [{suffix}] ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("  Please answer yes or no.")


def _prompt_text(prompt: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def _prompt_model_mode(default: str = "claude-code") -> str:
    while True:
        answer = input(
            "Use Claude Code or a custom model with ctx? "
            f"[{default}; choices: claude-code/custom/skip] "
        ).strip().lower()
        mode = answer or default
        if mode in {"claude-code", "custom", "skip"}:
            return mode
        print("  Please choose claude-code, custom, or skip.")


def _stdio_is_interactive() -> bool:
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def _should_run_wizard(
    args: argparse.Namespace,
    raw_argv: list[str],
) -> bool:
    return bool(args.wizard or (not raw_argv and _stdio_is_interactive()))


def run_wizard(args: argparse.Namespace) -> None:
    """Prompt for first-run choices and mutate parsed args in place."""
    print("ctx-init wizard:")
    args.hooks = _prompt_yes_no(
        "Install Claude Code observation hooks now?",
        default=args.hooks,
    )
    args.graph = _prompt_yes_no(
        "Build the knowledge graph now? This can take a while.",
        default=args.graph,
    )

    args.model_mode = _prompt_model_mode(args.model_mode or "claude-code")
    if args.model_mode == "skip":
        return

    if args.model_mode == "custom":
        args.model = _prompt_text(
            "Model slug, e.g. openai/gpt-5.5 or ollama/llama3.1",
            default=args.model,
        )
        provider_default = args.model_provider or (
            _model_provider_prefix(args.model) if args.model else None
        )
        args.model_provider = _prompt_text(
            "Provider prefix",
            default=provider_default,
        ) or None
        api_key_default = _resolve_api_key_env(
            args.api_key_env,
            args.model,
            args.model_provider,
        )
        args.api_key_env = _prompt_text(
            "API key environment variable (blank for local/no key)",
            default=api_key_default,
        )
        args.base_url = _prompt_text(
            "Provider base URL (blank for default)",
            default=args.base_url,
        ) or None

    args.goal = _prompt_text(
        "What do you want ctx to help you build or maintain?",
        default=args.goal,
    )
    if args.model_mode == "custom":
        args.validate_model = _prompt_yes_no(
            "Validate the model with one tiny provider call now?",
            default=args.validate_model,
        )


def run_model_onboarding(args: argparse.Namespace, claude: Path) -> int:
    """Record model choice and print harness recommendations."""
    mode = args.model_mode
    if mode is None or mode == "skip":
        print("  [skip] model onboarding (run ctx-init --wizard to configure)")
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

    if mode != "custom":
        return rc

    recommendation_query = " ".join(
        part for part in [goal, provider or "", args.model or "", "harness"]
        if part
    )
    harnesses = recommend_harnesses(
        recommendation_query,
        model_provider=provider,
        model=args.model,
    )
    if harnesses:
        print("  [ok] recommended harnesses:")
        for row in harnesses:
            norm = float(row.get("normalized_score") or 0.0)
            name = row.get("name")
            print(f"       - {name} (match {norm:.2f})")
            print(f"         install: ctx-harness-install {name} --dry-run")
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
        "--wizard",
        action="store_true",
        help=(
            "Prompt for hooks, graph build, model profile, and harness "
            "recommendation setup. Plain ctx-init does this automatically "
            "when run in an interactive terminal."
        ),
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
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    args = parser.parse_args(raw_argv)
    if _should_run_wizard(args, raw_argv):
        run_wizard(args)

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
