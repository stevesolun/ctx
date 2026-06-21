"""Config read/write helpers for ctx-monitor."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import atomic_write_text


CONFIG_REMOVE = object()


def read_default_config_raw() -> dict[str, Any]:
    try:
        from ctx_config import _read_default_config  # type: ignore

        raw = _read_default_config()
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        try:
            raw = json.loads(files("ctx").joinpath("config.json").read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


def read_user_config_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> None:
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            deep_merge_config(base[key], value)
        else:
            base[key] = value


def config_value(raw: dict[str, Any], path: str, default: Any = None) -> Any:
    current: Any = raw
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def set_config_value(raw: dict[str, Any], path: str, value: Any) -> None:
    current = raw
    parts = path.split(".")
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def delete_config_value(raw: dict[str, Any], path: str) -> None:
    current = raw
    parts = path.split(".")
    parents: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        parents.append((current, part))
        current = child
    current.pop(parts[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)


def config_field_specs() -> tuple[dict[str, Any], ...]:
    return (
        {"group": "Knowledge", "path": "knowledge.mode", "type": "choice", "choices": ("shipped", "local", "enriched"), "required": True, "label": "Knowledge source mode", "help": "shipped uses ctx's packaged graph/wiki, local stays private, enriched starts from shipped knowledge and adds your own.", "example": "enriched"},
        {"group": "Recommendation", "path": "resolver.recommendation_top_k", "type": "int", "min": 1, "max": 5, "required": True, "label": "Max mixed recommendations", "help": "Hard cap for the combined skills/agents/MCP recommendation bundle.", "example": 5},
        {"group": "Recommendation", "path": "resolver.recommendation_min_normalized_score", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Minimum recommendation score", "help": "Drops weak skill/agent/MCP matches instead of recommending at all cost.", "example": 0.30},
        {"group": "Recommendation", "path": "resolver.max_skills", "type": "int", "min": 1, "max": 50, "label": "Resolver hard skill ceiling", "help": "Maximum load candidates considered by a resolver call.", "example": 15},
        {"group": "Harness", "path": "harness.recommendation_min_fit_score", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Minimum harness fit score", "help": "Custom/API/local model users only see harnesses at or above this fit floor.", "example": 0.85},
        {"group": "Harness", "path": "harness.recommendation_min_normalized_score", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Harness normalized score floor", "help": "Compatibility display floor for older configs.", "example": 0.85},
        {"group": "Micro-skills", "path": "skill_transformer.line_threshold", "type": "int", "min": 1, "max": 2000, "required": True, "label": "Micro-skill line threshold", "help": "Any SKILL.md above this many lines triggers the micro-skills conversion gate.", "example": 180},
        {"group": "Micro-skills", "path": "skill_transformer.max_stage_lines", "type": "int", "min": 1, "max": 300, "label": "Max staged reference lines", "help": "Target maximum lines for each generated reference stage.", "example": 40},
        {"group": "Micro-skills", "path": "skill_transformer.stage_count", "type": "int", "min": 1, "max": 20, "label": "Stage count", "help": "Target number of staged references for long skills.", "example": 5},
        {"group": "Graph", "path": "graph.min_edge_weight", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Minimum final edge weight", "help": "Edges below this blended score are dropped from graph.json during rebuild.", "example": 0.03},
        {"group": "Graph", "path": "graph.edge_weights.semantic", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Semantic edge weight", "help": "Semantic portion of the blended edge score. Semantic/tags/slug tokens should sum to 1.", "example": 0.70},
        {"group": "Graph", "path": "graph.edge_weights.tags", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Tag edge weight", "help": "Tag-overlap portion of the blended edge score.", "example": 0.15},
        {"group": "Graph", "path": "graph.edge_weights.slug_tokens", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Slug-token edge weight", "help": "Slug-token overlap portion of the blended edge score.", "example": 0.15},
        {"group": "Graph", "path": "graph.semantic.top_k", "type": "int", "min": 1, "max": 200, "label": "Semantic neighbors per entity", "help": "Maximum nearest semantic neighbors retained per entity during graph build.", "example": 20},
        {"group": "Graph", "path": "graph.semantic.build_floor", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Semantic build floor", "help": "Low inclusion bar used when graph embeddings are rebuilt.", "example": 0.50},
        {"group": "Graph", "path": "graph.semantic.min_cosine", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "required": True, "label": "Semantic display floor", "help": "Read-time semantic filter. Raising this is stricter without forcing a rebuild.", "example": 0.80},
        {"group": "Graph", "path": "graph.tag_edges.dense_tag_threshold", "type": "int", "min": 1, "max": 10000, "label": "Dense tag cutoff", "help": "Tags shared by more than this many entities do not create broad noisy cliques.", "example": 500},
        {"group": "Graph", "path": "graph.token_edges.dense_token_threshold", "type": "int", "min": 1, "max": 10000, "label": "Dense slug-token cutoff", "help": "Slug words shared by too many entities are ignored as edge creators.", "example": 30},
        {"group": "Intake", "path": "intake.enabled", "type": "bool", "required": True, "label": "Intake quality gate", "help": "Runs duplicate/near-duplicate and body-quality checks when entities are added or updated.", "example": True},
        {"group": "Intake", "path": "intake.dup_threshold", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Duplicate threshold", "help": "Similarity at or above this is treated as a duplicate.", "example": 0.93},
        {"group": "Intake", "path": "intake.near_dup_threshold", "type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "label": "Near-duplicate threshold", "help": "Similarity at or above this asks the user to update/merge instead of blindly adding.", "example": 0.80},
        {"group": "Paths", "path": "paths.wiki_dir", "type": "str", "required": True, "label": "Wiki directory", "help": "Runtime llm-wiki directory used by dashboard, graph, and recommendation flows.", "example": "~/.claude/skill-wiki"},
        {"group": "Paths", "path": "paths.skills_dir", "type": "str", "required": True, "label": "Skills directory", "help": "Installed local skills directory.", "example": "~/.claude/skills"},
        {"group": "Paths", "path": "paths.agents_dir", "type": "str", "required": True, "label": "Agents directory", "help": "Installed local agents directory.", "example": "~/.claude/agents"},
    )


def coerce_config_value(spec: dict[str, Any], raw_value: Any) -> Any:
    if raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == ""):
        return CONFIG_REMOVE
    kind = spec.get("type", "str")
    if kind == "bool":
        if isinstance(raw_value, bool):
            return raw_value
        text = str(raw_value).strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
        raise ValueError(f"{spec['path']} must be true or false")
    if kind == "int":
        if isinstance(raw_value, bool):
            raise ValueError(f"{spec['path']} must be an integer")
        value: int | float = int(raw_value)
    elif kind == "float":
        if isinstance(raw_value, bool):
            raise ValueError(f"{spec['path']} must be a number")
        value = float(raw_value)
    elif kind == "choice":
        choice_value = str(raw_value).strip()
        if choice_value not in spec.get("choices", ()):
            raise ValueError(f"{spec['path']} must be one of {spec.get('choices')}")
        return choice_value
    else:
        text_value = str(raw_value).strip()
        return text_value if text_value else CONFIG_REMOVE
    if "min" in spec and value < spec["min"]:
        raise ValueError(f"{spec['path']} must be >= {spec['min']}")
    if "max" in spec and value > spec["max"]:
        raise ValueError(f"{spec['path']} must be <= {spec['max']}")
    return value


def effective_config_payload(user_config_path: Path) -> dict[str, Any]:
    defaults = read_default_config_raw()
    user = read_user_config_raw(user_config_path)
    effective = json.loads(json.dumps(defaults))
    deep_merge_config(effective, user)
    return {
        "defaults": defaults,
        "user": user,
        "effective": effective,
        "path": str(user_config_path),
    }


def save_config_updates(updates: dict[str, Any], user_config_path: Path) -> dict[str, Any]:
    specs = {spec["path"]: spec for spec in config_field_specs()}
    unknown = sorted(set(updates) - set(specs))
    if unknown:
        return {"ok": False, "detail": f"unknown config keys: {', '.join(unknown)}"}
    user_config = read_user_config_raw(user_config_path)
    try:
        for path, raw_value in updates.items():
            value = coerce_config_value(specs[path], raw_value)
            if value is CONFIG_REMOVE:
                delete_config_value(user_config, path)
            else:
                set_config_value(user_config, path, value)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "detail": str(exc)}
    with file_lock(user_config_path):
        atomic_write_text(
            user_config_path,
            json.dumps(user_config, indent=2, sort_keys=True) + "\n",
        )
    return {"ok": True, "detail": f"saved {len(updates)} config keys"}
