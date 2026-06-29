"""LoopFlow and external agent-loop adapter for ctx recommendations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

from ctx.api import recommend_bundle
from ctx_init import recommend_harnesses


_PERMISSION_ALIASES = {
    "agent": "agents",
    "agents": "agents",
    "harness": "harnesses",
    "harnesses": "harnesses",
    "mcp": "mcps",
    "mcps": "mcps",
    "mcp-server": "mcps",
    "mcp-servers": "mcps",
    "skill": "skills",
    "skills": "skills",
}
_ENTITY_TO_GROUP = {"agent": "agents", "mcp-server": "mcps", "skill": "skills"}
_CAPABILITY_KEYS = ("skills", "agents", "mcps", "harnesses")
_HARNESS_REQUIREMENT_FLAGS = {
    "runtime": "--harness-runtime",
    "autonomy": "--harness-autonomy",
    "tools": "--harness-tools",
    "verification": "--harness-verify",
    "privacy": "--harness-privacy",
    "attach_mode": "--harness-attach-mode",
}


def _split_csv(values: list[str] | None) -> list[str]:
    if not values:
        return []
    parts: list[str] = []
    for value in values:
        parts.extend(piece.strip() for piece in value.split(",") if piece.strip())
    return parts


def _parse_permissions(values: list[str] | None) -> set[str]:
    raw = _split_csv(values) or ["skills", "agents", "mcps"]
    permissions: set[str] = set()
    for value in raw:
        normalized = _PERMISSION_ALIASES.get(value.strip().lower())
        if normalized is None:
            raise ValueError(
                f"unknown permission {value!r}; expected one of "
                "skills, agents, mcps, harnesses"
            )
        permissions.add(normalized)
    return permissions


def parse_loop_file(path: Path) -> dict[str, Any]:
    """Extract the LoopFlow fields ctx needs from a .loop file.

    This is intentionally a permissive subset parser. LoopFlow remains the
    source of truth for execution; ctx only needs goal/context/check hints.
    """
    text = path.read_text(encoding="utf-8")
    fields: dict[str, Any] = {"source": str(path)}
    if match := re.search(r'^\s*loop\s+"([^"]+)"\s*:', text, flags=re.MULTILINE):
        fields["name"] = match.group(1).strip()
    if match := re.search(r"^\s*goal\s*:\s*(.+)$", text, flags=re.MULTILINE):
        fields["goal"] = match.group(1).strip()
    look_at: list[str] = []
    for match in re.finditer(r"^\s*(?:look at|in)\s*:\s*(.+)$", text, flags=re.MULTILINE):
        look_at.extend(piece.strip() for piece in match.group(1).split(",") if piece.strip())
    if look_at:
        fields["look_at"] = look_at
    done_when = [
        match.group(1).strip()
        for match in re.finditer(r"^\s*done when\s+(.+)$", text, flags=re.MULTILINE)
    ]
    if done_when:
        fields["done_when"] = done_when
    return fields


def _read_text_file(path: Path | None) -> str:
    if path is None:
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _build_query(
    *,
    goal: str,
    loop_name: str,
    look_at: list[str],
    last_failure: str,
    loop_kind: str,
    model: str | None,
    model_provider: str | None,
) -> str:
    parts = [goal, loop_name, loop_kind]
    if look_at:
        parts.append("context: " + ", ".join(look_at))
    if last_failure:
        parts.append("last failure: " + last_failure[:2000])
    if model or model_provider:
        parts.append("model: " + " ".join(part for part in (model_provider, model) if part))
    return " ".join(part for part in parts if part).strip()


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in (
        "name",
        "type",
        "score",
        "normalized_score",
        "fit_score",
        "reliability_score",
        "fit_reason",
        "reliability_reason",
        "matching_tags",
        "shared_tags",
        "tags",
    ):
        value = row.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact


def _group_bundle(
    rows: list[dict[str, Any]],
    *,
    permissions: set[str],
    top_k: int,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("skills", "agents", "mcps")
    }
    seen: set[tuple[str, str]] = set()
    for row in rows:
        group = _ENTITY_TO_GROUP.get(str(row.get("type") or ""))
        name = str(row.get("name") or "").strip()
        if group is None or group not in permissions or not name:
            continue
        identity = (group, name)
        if identity in seen:
            continue
        seen.add(identity)
        if len(grouped[group]) < top_k:
            grouped[group].append(_compact_row(row))
    return grouped


def _harness_command(
    harnesses: list[dict[str, Any]],
    *,
    goal: str,
    model_provider: str | None,
    model: str | None,
    requirements: dict[str, str],
) -> str | None:
    if not harnesses:
        return None
    parts = ["ctx-harness-install", str(harnesses[0]["name"]), "--dry-run"]
    if goal:
        parts.extend(["--goal", goal])
    if model_provider:
        parts.extend(["--model-provider", model_provider])
    if model:
        parts.extend(["--model", model])
    for key, value in requirements.items():
        if value:
            parts.extend([_HARNESS_REQUIREMENT_FLAGS[key], value])
    return shlex.join(parts)


def recommend_for_loop(
    *,
    goal: str,
    loop_name: str = "",
    loop_kind: str = "loopflow",
    look_at: list[str] | None = None,
    last_failure: str = "",
    permissions: set[str] | None = None,
    own_llm: bool = False,
    model_provider: str | None = None,
    model: str | None = None,
    harness_requirements: dict[str, str] | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Return a permissioned ctx adapter payload for a DSL or agent loop."""
    safe_top_k = max(1, min(int(top_k), 20))
    granted = permissions if permissions is not None else {"skills", "agents", "mcps"}
    context_paths = look_at or []
    requirements = harness_requirements or {}
    query = _build_query(
        goal=goal,
        loop_name=loop_name,
        look_at=context_paths,
        last_failure=last_failure,
        loop_kind=loop_kind,
        model=model,
        model_provider=model_provider,
    )

    capability_bundle: dict[str, list[dict[str, Any]]] = {
        "skills": [],
        "agents": [],
        "mcps": [],
        "harnesses": [],
    }
    if granted.intersection({"skills", "agents", "mcps"}):
        rows = recommend_bundle(query, top_k=max(safe_top_k * 4, safe_top_k))
        capability_bundle.update(
            _group_bundle(rows, permissions=granted, top_k=safe_top_k)
        )

    warnings: list[str] = []
    should_recommend_harness = "harnesses" in granted and (
        own_llm or bool(model_provider) or bool(model)
    )
    if "harnesses" in granted and not should_recommend_harness:
        warnings.append(
            "harnesses permission granted but no user-owned LLM/model was declared"
        )
    if should_recommend_harness:
        harness_goal = " ".join([goal, *requirements.values()]).strip() or query
        capability_bundle["harnesses"] = [
            _compact_row(row)
            for row in recommend_harnesses(
                harness_goal,
                top_k=safe_top_k,
                model_provider=model_provider,
                model=model,
            )
        ]

    use_skills = None
    if "skills" in granted:
        skill_names = [row["name"] for row in capability_bundle["skills"][:3]]
        use_skills = "use skills: ctx-recommend"
        if skill_names:
            use_skills += ", " + ", ".join(str(name) for name in skill_names)

    return {
        "version": "ctx.loop_adapter.v1",
        "adapter": loop_kind,
        "permissions": {key: key in granted for key in _CAPABILITY_KEYS},
        "warnings": warnings,
        "context": {
            "goal": goal,
            "loop_name": loop_name,
            "look_at": context_paths,
            "last_failure_present": bool(last_failure),
            "query": query,
        },
        "mcp_server": {
            "name": "ctx",
            "command": "ctx-mcp-server",
            "tools": [
                "ctx__recommend_bundle",
                "ctx__graph_query",
                "ctx__wiki_search",
                "ctx__wiki_get",
            ],
        },
        "capabilities": capability_bundle,
        "loopflow": {
            "use_tools": 'use tools from the "ctx" server',
            "use_skills": use_skills,
            "before_plan": "Call python -m ctx.adapters.loopflow before planning and inject this JSON as read-only context.",
            "harness_rule": "Only load harnesses when the loop runs on a user-owned/API/local LLM.",
        },
        "agent_loop": {
            "before_plan": "Call recommend_for_loop() or python -m ctx.adapters.loopflow with task, context, and last failure.",
            "before_act": "Load only the granted capability groups from capabilities.*.",
            "on_failure": "Pass the latest failure back as last_failure before the next plan.",
            "harness_install": _harness_command(
                capability_bundle["harnesses"],
                goal=goal,
                model_provider=model_provider,
                model=model,
                requirements=requirements,
            ),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit ctx recommendations for LoopFlow DSL files and agent loops."
    )
    parser.add_argument("--loop-file", type=Path, help="Optional .loop file to read.")
    parser.add_argument("--loop-name", default="", help="Loop name when no .loop file is used.")
    parser.add_argument("--goal", default="", help="Loop goal or agent-loop task.")
    parser.add_argument("--look-at", action="append", default=[], help="Context path or phrase.")
    parser.add_argument("--last-failure", default="", help="Previous failure text.")
    parser.add_argument("--last-failure-file", type=Path, help="Read previous failure from a file.")
    parser.add_argument(
        "--permissions",
        action="append",
        help="Comma-separated capability grants: skills, agents, mcps, harnesses.",
    )
    parser.add_argument(
        "--loop-kind",
        choices=("loopflow", "agent-loop"),
        default="loopflow",
        help="Shape hints for the consuming loop.",
    )
    parser.add_argument("--own-llm", action="store_true", help="Enable harness recommendations.")
    parser.add_argument("--model-provider", help="Provider for user-owned/API/local model.")
    parser.add_argument("--model", help="Model name for harness matching.")
    parser.add_argument("--harness-runtime", default="")
    parser.add_argument("--harness-autonomy", default="")
    parser.add_argument("--harness-tools", default="")
    parser.add_argument("--harness-verify", default="")
    parser.add_argument("--harness-privacy", default="")
    parser.add_argument("--harness-attach-mode", default="")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--compact", action="store_true", help="Print compact JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        permissions = _parse_permissions(args.permissions)
    except ValueError as exc:
        parser.error(str(exc))

    loop_fields: dict[str, Any] = {}
    if args.loop_file is not None:
        loop_fields = parse_loop_file(args.loop_file)
    goal = args.goal or str(loop_fields.get("goal") or "")
    if not goal:
        parser.error("--goal or a loop file with goal: is required")
    loop_name = args.loop_name or str(loop_fields.get("name") or "")
    look_at = [*loop_fields.get("look_at", []), *_split_csv(args.look_at)]
    last_failure = args.last_failure or _read_text_file(args.last_failure_file)
    requirements = {
        "runtime": args.harness_runtime,
        "autonomy": args.harness_autonomy,
        "tools": args.harness_tools,
        "verification": args.harness_verify,
        "privacy": args.harness_privacy,
        "attach_mode": args.harness_attach_mode,
    }
    payload = recommend_for_loop(
        goal=goal,
        loop_name=loop_name,
        loop_kind=args.loop_kind,
        look_at=look_at,
        last_failure=last_failure,
        permissions=permissions,
        own_llm=args.own_llm,
        model_provider=args.model_provider,
        model=args.model,
        harness_requirements={key: value for key, value in requirements.items() if value},
        top_k=args.top_k,
    )
    json.dump(payload, sys.stdout, indent=None if args.compact else 2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
