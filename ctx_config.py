"""
ctx_config.py -- Central configuration loader for the Alive Skill System.

Loads from (in priority order):
  1. ~/.claude/skill-system-config.json  (user's deployed config, highest priority)
  2. <script_dir>/config.json            (repo default config)

Usage:
    from ctx_config import cfg

    wiki_dir = cfg.wiki_dir
    max_skills = cfg.max_skills
"""

import json
import os
from pathlib import Path
from typing import Any


_SCRIPT_DIR = Path(__file__).parent
_DEFAULT_CONFIG = _SCRIPT_DIR / "config.json"
_USER_CONFIG = Path(os.path.expanduser("~/.claude/skill-system-config.json"))


def _load_raw() -> dict[str, Any]:
    """Load and merge default + user config."""
    raw: dict[str, Any] = {}

    if _DEFAULT_CONFIG.exists():
        try:
            raw = json.loads(_DEFAULT_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            pass

    if _USER_CONFIG.exists():
        try:
            user = json.loads(_USER_CONFIG.read_text(encoding="utf-8"))
            # Deep merge: user values override defaults
            _deep_merge(raw, user)
        except Exception:
            pass

    return raw


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in-place (recursive for nested dicts)."""
    for k, v in override.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _expand(value: str) -> str:
    """Expand ~ and env vars in path strings."""
    return os.path.expandvars(os.path.expanduser(value))


class Config:
    """Typed access to configuration values."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        paths = raw.get("paths", {})
        resolver = raw.get("resolver", {})
        monitor = raw.get("context_monitor", {})
        tracker = raw.get("usage_tracker", {})
        transformer = raw.get("skill_transformer", {})
        router = raw.get("skill_router", {})
        bsitter = raw.get("babysitter", {})

        # ── Paths ──────────────────────────────────────────────────────────
        self.claude_dir = Path(_expand(paths.get("claude_dir", "~/.claude")))
        self.wiki_dir = Path(_expand(paths.get("wiki_dir", "~/.claude/skill-wiki")))
        self.skills_dir = Path(_expand(paths.get("skills_dir", "~/.claude/skills")))
        self.agents_dir = Path(_expand(paths.get("agents_dir", "~/.claude/agents")))
        self.skill_manifest = Path(_expand(paths.get("skill_manifest", "~/.claude/skill-manifest.json")))
        self.intent_log = Path(_expand(paths.get("intent_log", "~/.claude/intent-log.jsonl")))
        self.pending_skills = Path(_expand(paths.get("pending_skills", "~/.claude/pending-skills.json")))
        self.skill_registry = Path(_expand(paths.get("skill_registry", "~/.claude/skill-registry.json")))
        self.stack_profile_tmp = Path(_expand(paths.get("stack_profile_tmp", "/tmp/skill-stack-profile.json")))
        self.catalog = Path(_expand(paths.get("catalog", "~/.claude/skill-wiki/catalog.md")))

        # ── Resolver ───────────────────────────────────────────────────────
        self.max_skills: int = resolver.get("max_skills", 15)
        self.intent_boost_per_signal: int = resolver.get("intent_boost_per_signal", 5)
        self.intent_boost_max: int = resolver.get("intent_boost_max", 15)
        self.staleness_penalty: int = resolver.get("staleness_penalty", -8)
        self.meta_skills: list[str] = resolver.get("meta_skills", ["skill-router", "file-reading"])

        # ── Context Monitor ────────────────────────────────────────────────
        self.unmatched_signal_threshold: int = monitor.get("unmatched_signal_threshold", 3)
        self.manifest_stale_minutes: int = monitor.get("manifest_stale_minutes", 60)

        # ── Usage Tracker ──────────────────────────────────────────────────
        self.stale_threshold_sessions: int = tracker.get("stale_threshold_sessions", 30)
        self.keep_log_days: int = tracker.get("keep_log_days", 5)

        # ── Skill Transformer ──────────────────────────────────────────────
        self.line_threshold: int = transformer.get("line_threshold", 180)
        self.max_stage_lines: int = transformer.get("max_stage_lines", 40)
        self.stage_count: int = transformer.get("stage_count", 5)

        # ── Skill Router ───────────────────────────────────────────────────
        self.manifest_stale_router_minutes: int = router.get("manifest_stale_minutes", 60)
        self.manifest_max_age_hours: int = router.get("manifest_max_age_hours", 24)

        # ── Extra Skill Dirs ───────────────────────────────────────────────
        self.extra_skill_dirs: list[Path] = [
            Path(_expand(d)) for d in raw.get("extra_skill_dirs", [])
        ]

        # ── Babysitter ─────────────────────────────────────────────────────
        self.babysitter_plugin_root: str = bsitter.get("plugin_root", "")
        self.babysitter_runs_dir: str = bsitter.get("runs_dir", ".a5c/runs")
        self.babysitter_sdk_version: str = bsitter.get("sdk_version", "latest")

    def get(self, key: str, default: Any = None) -> Any:
        """Raw key access (dot-separated: 'paths.wiki_dir')."""
        parts = key.split(".")
        node: Any = self._raw
        for p in parts:
            if isinstance(node, dict) and p in node:
                node = node[p]
            else:
                return default
        return node

    def all_skill_dirs(self) -> list[Path]:
        """Return all skill directories (primary + extra)."""
        dirs = [self.skills_dir, self.agents_dir] + self.extra_skill_dirs
        return [d for d in dirs if d.exists()]


# Singleton instance — import this
cfg = Config(_load_raw())


def reload() -> None:
    """Reload config from disk (useful if config changed during session)."""
    global cfg
    cfg = Config(_load_raw())
