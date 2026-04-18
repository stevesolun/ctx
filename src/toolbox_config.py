"""
toolbox_config.py -- Data model + loader for the pre/post dev toolbox feature.

Two-layer config:
  1. ~/.claude/toolboxes.json       (global, user-personal)
  2. <repo_root>/.toolbox.yaml      (per-repo, team-shared, overrides global)

A repo-local YAML ALWAYS wins on key collision. Missing files are non-fatal;
both layers are optional. Consumers receive a single merged ToolboxSet.

Design constraints:
- Immutable frozen dataclasses (CLAUDE.md rule: immutability).
- Schema version gate \u2014 unknown versions raise ValueError, not a warning.
- YAML support is optional; if PyYAML is missing, .toolbox.yaml is skipped
  with a single stderr notice (does not crash the CLI).
- Atomic writes for persistence (save_global).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]

    _HAS_YAML = True
except ImportError:  # pragma: no cover - exercised only in minimal envs
    _HAS_YAML = False


SCHEMA_VERSION = 1
VALID_ANALYSIS = frozenset({"diff", "full", "graph-blast", "dynamic"})
VALID_DEDUP_POLICY = frozenset({"fresh", "cached"})


@dataclass(frozen=True)
class Scope:
    projects: tuple[str, ...] = ("*",)
    signals: tuple[str, ...] = ()
    analysis: str = "dynamic"

    def __post_init__(self) -> None:
        if self.analysis not in VALID_ANALYSIS:
            raise ValueError(
                f"scope.analysis must be one of {sorted(VALID_ANALYSIS)}; got {self.analysis!r}"
            )


@dataclass(frozen=True)
class Trigger:
    slash: bool = True
    pre_commit: bool = False
    session_end: bool = False
    file_save: str | None = None  # glob pattern or None


@dataclass(frozen=True)
class Budget:
    max_tokens: int = 150_000
    max_seconds: int = 300

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            raise ValueError(f"budget.max_tokens must be positive; got {self.max_tokens}")
        if self.max_seconds <= 0:
            raise ValueError(f"budget.max_seconds must be positive; got {self.max_seconds}")


@dataclass(frozen=True)
class Dedup:
    window_seconds: int = 600
    policy: str = "fresh"

    def __post_init__(self) -> None:
        if self.policy not in VALID_DEDUP_POLICY:
            raise ValueError(
                f"dedup.policy must be one of {sorted(VALID_DEDUP_POLICY)}; got {self.policy!r}"
            )
        if self.window_seconds < 0:
            raise ValueError(f"dedup.window_seconds must be >= 0; got {self.window_seconds}")


@dataclass(frozen=True)
class Toolbox:
    name: str
    description: str = ""
    pre: tuple[str, ...] = ()
    post: tuple[str, ...] = ()
    scope: Scope = field(default_factory=Scope)
    trigger: Trigger = field(default_factory=Trigger)
    budget: Budget = field(default_factory=Budget)
    dedup: Dedup = field(default_factory=Dedup)
    guardrail: bool = False

    @staticmethod
    def from_dict(name: str, raw: dict[str, Any]) -> Toolbox:
        scope_raw = raw.get("scope", {}) or {}
        trigger_raw = raw.get("trigger", {}) or {}
        budget_raw = raw.get("budget", {}) or {}
        dedup_raw = raw.get("dedup", {}) or {}

        return Toolbox(
            name=name,
            description=str(raw.get("description", "")),
            pre=tuple(raw.get("pre", []) or []),
            post=tuple(raw.get("post", []) or []),
            scope=Scope(
                projects=tuple(scope_raw.get("projects", ("*",)) or ("*",)),
                signals=tuple(scope_raw.get("signals", ()) or ()),
                analysis=str(scope_raw.get("analysis", "dynamic")),
            ),
            trigger=Trigger(
                slash=bool(trigger_raw.get("slash", True)),
                pre_commit=bool(trigger_raw.get("pre_commit", False)),
                session_end=bool(trigger_raw.get("session_end", False)),
                file_save=trigger_raw.get("file_save"),
            ),
            budget=Budget(
                max_tokens=int(budget_raw.get("max_tokens", 150_000)),
                max_seconds=int(budget_raw.get("max_seconds", 300)),
            ),
            dedup=Dedup(
                window_seconds=int(dedup_raw.get("window_seconds", 600)),
                policy=str(dedup_raw.get("policy", "fresh")),
            ),
            guardrail=bool(raw.get("guardrail", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "pre": list(self.pre),
            "post": list(self.post),
            "scope": {
                "projects": list(self.scope.projects),
                "signals": list(self.scope.signals),
                "analysis": self.scope.analysis,
            },
            "trigger": {
                "slash": self.trigger.slash,
                "pre_commit": self.trigger.pre_commit,
                "session_end": self.trigger.session_end,
                "file_save": self.trigger.file_save,
            },
            "budget": {
                "max_tokens": self.budget.max_tokens,
                "max_seconds": self.budget.max_seconds,
            },
            "dedup": {
                "window_seconds": self.dedup.window_seconds,
                "policy": self.dedup.policy,
            },
            "guardrail": self.guardrail,
        }


@dataclass(frozen=True)
class ToolboxSet:
    toolboxes: dict[str, Toolbox]
    active: tuple[str, ...]
    version: int = SCHEMA_VERSION

    @staticmethod
    def empty() -> ToolboxSet:
        return ToolboxSet(toolboxes={}, active=())

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> ToolboxSet:
        version = int(raw.get("version", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported toolbox config version {version}; expected {SCHEMA_VERSION}"
            )
        tbs_raw = raw.get("toolboxes", {}) or {}
        if not isinstance(tbs_raw, dict):
            raise ValueError("'toolboxes' must be a mapping of name -> config")

        toolboxes = {
            name: Toolbox.from_dict(name, body)
            for name, body in tbs_raw.items()
        }
        active_raw = raw.get("active", []) or []
        active = tuple(a for a in active_raw if a in toolboxes)
        return ToolboxSet(toolboxes=toolboxes, active=active, version=version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "toolboxes": {name: tb.to_dict() for name, tb in self.toolboxes.items()},
            "active": list(self.active),
        }

    def with_toolbox(self, tb: Toolbox) -> ToolboxSet:
        new_tbs = dict(self.toolboxes)
        new_tbs[tb.name] = tb
        return replace(self, toolboxes=new_tbs)

    def without_toolbox(self, name: str) -> ToolboxSet:
        if name not in self.toolboxes:
            return self
        new_tbs = {k: v for k, v in self.toolboxes.items() if k != name}
        new_active = tuple(a for a in self.active if a != name)
        return replace(self, toolboxes=new_tbs, active=new_active)

    def activate(self, name: str) -> ToolboxSet:
        if name not in self.toolboxes:
            raise KeyError(f"No such toolbox: {name!r}")
        if name in self.active:
            return self
        return replace(self, active=self.active + (name,))

    def deactivate(self, name: str) -> ToolboxSet:
        if name not in self.active:
            return self
        return replace(self, active=tuple(a for a in self.active if a != name))


# ── Path resolution ─────────────────────────────────────────────────────────

def global_config_path() -> Path:
    """~/.claude/toolboxes.json \u2014 user-personal, global across all repos."""
    return Path(os.path.expanduser("~/.claude/toolboxes.json"))


def repo_config_path(repo_root: Path | None = None) -> Path:
    """<repo_root>/.toolbox.yaml \u2014 team-shared, checked into git."""
    root = repo_root if repo_root is not None else Path.cwd()
    return root / ".toolbox.yaml"


# ── Loading ─────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if not _HAS_YAML:
        print(
            f"[toolbox] PyYAML not installed; skipping {path}. "
            f"pip install pyyaml to enable per-repo config.",
            file=sys.stderr,
        )
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    return loaded or {}


def load_global(path: Path | None = None) -> ToolboxSet:
    raw = _load_json(path or global_config_path())
    if not raw:
        return ToolboxSet.empty()
    return ToolboxSet.from_dict(raw)


def load_repo(repo_root: Path | None = None) -> ToolboxSet:
    raw = _load_yaml(repo_config_path(repo_root))
    if not raw:
        return ToolboxSet.empty()
    return ToolboxSet.from_dict(raw)


def merged(repo_root: Path | None = None,
           global_path: Path | None = None) -> ToolboxSet:
    """
    Global + per-repo merge. Per-repo toolboxes override global toolboxes of
    the same name. Active list is the union (repo preferences take precedence).
    """
    g = load_global(global_path)
    r = load_repo(repo_root)

    merged_tbs = dict(g.toolboxes)
    merged_tbs.update(r.toolboxes)

    seen: set[str] = set()
    merged_active: list[str] = []
    for name in list(r.active) + list(g.active):
        if name in merged_tbs and name not in seen:
            merged_active.append(name)
            seen.add(name)

    return ToolboxSet(
        toolboxes=merged_tbs,
        active=tuple(merged_active),
        version=SCHEMA_VERSION,
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


def save_global(tset: ToolboxSet, path: Path | None = None) -> None:
    target = path or global_config_path()
    _atomic_write(target, json.dumps(tset.to_dict(), indent=2, sort_keys=False) + "\n")


def save_repo(tset: ToolboxSet, repo_root: Path | None = None) -> None:
    target = repo_config_path(repo_root)
    if not _HAS_YAML:
        raise RuntimeError(
            "PyYAML is required to write per-repo .toolbox.yaml; "
            "pip install pyyaml or edit the file manually."
        )
    _atomic_write(target, yaml.safe_dump(tset.to_dict(), sort_keys=False))
