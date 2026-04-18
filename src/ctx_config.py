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
import sys
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
        except Exception as exc:
            print(f"Warning: failed to load default config: {exc}", file=sys.stderr)

    if _USER_CONFIG.exists():
        try:
            user = json.loads(_USER_CONFIG.read_text(encoding="utf-8"))
            # Deep merge: user values override defaults
            _deep_merge(raw, user)
        except Exception as exc:
            print(f"Warning: failed to load user config: {exc}", file=sys.stderr)

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
        intake = raw.get("intake", {})
        intake_emb = intake.get("embedding", {}) if isinstance(intake, dict) else {}
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

        # ── Tag Taxonomy ──────────────────────────────────────────────────
        self.all_tags: list[str] = raw.get("tags", [
            "python", "javascript", "typescript", "rust", "go", "java", "ruby", "swift", "kotlin",
            "react", "vue", "angular", "nextjs", "fastapi", "django", "express", "flask",
            "docker", "kubernetes", "terraform", "ci-cd", "aws", "gcp", "azure",
            "sql", "nosql", "redis", "kafka", "spark", "dbt", "airflow",
            "llm", "agents", "mcp", "langchain", "embeddings", "fine-tuning", "rag",
            "testing", "linting", "typing", "security", "performance",
            "documentation", "api-spec", "markdown", "diagrams",
            "comparison", "decision", "pattern", "troubleshooting",
            "marketplace", "registry", "versioning", "compatibility",
        ])

        # ── Intake Gate ────────────────────────────────────────────────────
        # Phase 2 similarity/structure gate for skill_add / agent_add.
        # When disabled, callers should skip the gate entirely — the
        # thresholds here only apply when ``intake_enabled`` is True.
        self.intake_enabled: bool = bool(intake.get("enabled", True))
        self.intake_dup_threshold: float = float(intake.get("dup_threshold", 0.93))
        self.intake_near_dup_threshold: float = float(
            intake.get("near_dup_threshold", 0.85)
        )
        self.intake_min_neighbors: int = int(intake.get("min_neighbors", 0))
        self.intake_min_neighbor_score: float = float(
            intake.get("min_neighbor_score", 0.30)
        )
        self.intake_min_body_chars: int = int(intake.get("min_body_chars", 120))
        self.intake_cache_root: Path = Path(
            _expand(intake.get("cache_root", "~/.claude/skills/_embeddings"))
        )
        self.intake_backend: str = str(
            intake_emb.get("backend", "sentence-transformers")
        )
        # ``None``-valued keys flow through unchanged so downstream
        # factories can distinguish "use backend default" (None) from
        # "forced empty string" (never).
        model = intake_emb.get("model")
        self.intake_model: str | None = model if isinstance(model, str) else None
        base_url = intake_emb.get("base_url")
        self.intake_base_url: str | None = (
            base_url if isinstance(base_url, str) else None
        )
        self.intake_allow_remote: bool = bool(intake_emb.get("allow_remote", False))

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

    def build_intake_config(self) -> Any:
        """Construct an ``intake_gate.IntakeConfig`` from these settings.

        Lazy-imported so ``ctx_config`` stays free of the numpy / embedding
        dependency graph when callers don't need the intake gate.
        """
        from intake_gate import IntakeConfig  # noqa: PLC0415
        return IntakeConfig(
            dup_threshold=self.intake_dup_threshold,
            near_dup_threshold=self.intake_near_dup_threshold,
            min_neighbors=self.intake_min_neighbors,
            min_neighbor_score=self.intake_min_neighbor_score,
            min_body_chars=self.intake_min_body_chars,
        )

    def build_intake_embedder(self) -> Any:
        """Construct the configured embedding backend.

        Lazy-imported for the same reason as ``build_intake_config``.
        Callers pay the heavy-model cost only when they ask for it.
        """
        from embedding_backend import get_embedder  # noqa: PLC0415
        return get_embedder(
            backend=self.intake_backend,
            model=self.intake_model,
            base_url=self.intake_base_url,
            allow_remote=self.intake_allow_remote,
        )


# Singleton instance — import this
cfg = Config(_load_raw())


def reload() -> None:
    """Reload config from disk (useful if config changed during session)."""
    global cfg
    cfg = Config(_load_raw())
