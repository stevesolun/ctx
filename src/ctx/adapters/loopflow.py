"""LoopFlow and external agent-loop adapter for ctx recommendations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any

import ctx.api as ctx_api
from ctx.core.resolve.recommendations import query_to_tags, recommend_by_tags
from ctx_init import _harness_requirements_text, recommend_harnesses


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
_GROUP_TO_ENTITY = {"skills": "skill", "agents": "agent", "mcps": "mcp-server"}
_CAPABILITY_KEYS = ("skills", "agents", "mcps", "harnesses")
_ALL_CAPABILITY_GRANTS = frozenset(_CAPABILITY_KEYS)
_HARNESS_REQUIREMENT_FLAGS = {
    "runtime": "--harness-runtime",
    "autonomy": "--harness-autonomy",
    "tools": "--harness-tools",
    "verification": "--harness-verify",
    "privacy": "--harness-privacy",
    "attach_mode": "--harness-attach-mode",
    "api_key_env": "--api-key-env",
}


def _split_csv(values: list[str] | None) -> list[str]:
    if not values:
        return []
    parts: list[str] = []
    for value in values:
        parts.extend(piece.strip() for piece in value.split(",") if piece.strip())
    return parts


def _parse_permissions(values: list[str] | None) -> set[str]:
    raw = _split_csv(values)
    permissions: set[str] = set()
    for value in raw:
        normalized = _PERMISSION_ALIASES.get(value.strip().lower())
        if normalized is None:
            raise ValueError(
                f"unknown permission {value!r}; expected one of skills, agents, mcps, harnesses"
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


def _done_when_text(done_when: list[str]) -> str:
    checks = [value.strip() for value in done_when if value.strip()]
    if not checks:
        return ""
    return "done when: " + ", ".join(checks)


def _build_query(
    *,
    goal: str,
    loop_name: str,
    look_at: list[str],
    done_when: list[str],
    last_failure: str,
    loop_kind: str,
    model: str | None,
    model_provider: str | None,
) -> str:
    parts = [goal, loop_name, loop_kind]
    if look_at:
        parts.append("context: " + ", ".join(look_at))
    if done_when_text := _done_when_text(done_when):
        parts.append(done_when_text)
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
        "source_catalog",
        "status",
        "source",
        "skill_id",
        "installs",
        "detail_url",
        "install_command",
        "category",
        "invoke_command",
        "security_review",
    ):
        value = row.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact


def _is_loadable_skill_row(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    source_catalog = str(row.get("source_catalog") or "").strip().lower()
    install_command = str(row.get("install_command") or "").strip()
    if status in {"available", "remote-cataloged"}:
        return False
    if source_catalog == "skill-index" or install_command:
        return False
    return True


def _group_bundle(
    rows: list[dict[str, Any]],
    *,
    permissions: set[str],
    top_k: int,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in ("skills", "agents", "mcps")}
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
    parts = ["ctx-harness-install", "--dry-run"]
    if goal:
        parts.append(f"--goal={goal}")
    if model_provider:
        parts.append(f"--model-provider={model_provider}")
    if model:
        parts.append(f"--model={model}")
    for key, value in requirements.items():
        if value:
            parts.append(f"{_HARNESS_REQUIREMENT_FLAGS[key]}={value}")
    parts.extend(["--", str(harnesses[0]["name"])])
    return shlex.join(parts)


def _ctx_mcp_tool_names(permissions: set[str]) -> list[str]:
    if not _ALL_CAPABILITY_GRANTS <= permissions:
        return []
    return ctx_api.ctx_core_tool_names()


def _normalize_harness_requirements(
    requirements: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    known: dict[str, str] = {}
    unknown: list[str] = []
    for key, value in requirements.items():
        if key in _HARNESS_REQUIREMENT_FLAGS:
            known[key] = value
        else:
            unknown.append(str(key))
    return known, sorted(unknown)


def _recommendation_graph() -> Any:
    return ctx_api.recommendation_graph()


def _recommend_capability_rows(
    query: str,
    *,
    permissions: set[str],
    top_k: int,
) -> list[dict[str, Any]]:
    entity_types = [
        entity_type for group, entity_type in _GROUP_TO_ENTITY.items() if group in permissions
    ]
    if not entity_types:
        return []
    tags = query_to_tags(query)
    if not tags:
        return []
    graph = _recommendation_graph()
    if graph.number_of_nodes() == 0:
        return []
    from ctx_config import cfg  # noqa: PLC0415

    rows: list[dict[str, Any]] = []
    for entity_type in entity_types:
        rows.extend(
            recommend_by_tags(
                graph,
                tags,
                top_n=top_k,
                query=query,
                entity_types=(entity_type,),
                min_normalized_score=cfg.recommendation_min_normalized_score,
            )
        )
    return rows


def recommend_for_loop(
    *,
    goal: str,
    loop_name: str = "",
    loop_kind: str = "loopflow",
    look_at: list[str] | None = None,
    done_when: list[str] | None = None,
    last_failure: str = "",
    permissions: set[str] | None = None,
    own_llm: bool = False,
    model_provider: str | None = None,
    model: str | None = None,
    harness_requirements: dict[str, str] | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Return a permissioned ctx adapter payload for a DSL or agent loop.

    Empty permissions fail closed. ``last_failure`` may influence ranking, but
    the raw failure text is omitted from the returned context and capability
    rows.
    """
    safe_top_k = max(1, min(int(top_k), 20))
    granted = permissions or set()
    context_paths = look_at or []
    done_when_checks = [value.strip() for value in (done_when or []) if value.strip()]
    requirements, unknown_requirement_keys = _normalize_harness_requirements(
        harness_requirements or {}
    )
    public_query = _build_query(
        goal=goal,
        loop_name=loop_name,
        look_at=context_paths,
        done_when=done_when_checks,
        last_failure="",
        loop_kind=loop_kind,
        model=model,
        model_provider=model_provider,
    )
    ranking_query = _build_query(
        goal=goal,
        loop_name=loop_name,
        look_at=context_paths,
        done_when=done_when_checks,
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
        rows = _recommend_capability_rows(
            ranking_query,
            permissions=granted,
            top_k=safe_top_k,
        )
        capability_bundle.update(_group_bundle(rows, permissions=granted, top_k=safe_top_k))

    warnings: list[str] = []
    if unknown_requirement_keys:
        warnings.append(
            "ignored unknown harness requirement(s): " + ", ".join(unknown_requirement_keys)
        )
    should_recommend_harness = "harnesses" in granted and (
        own_llm or bool(model_provider) or bool(model)
    )
    if "harnesses" in granted and not should_recommend_harness:
        warnings.append("harnesses permission granted but no user-owned LLM/model was declared")
    if should_recommend_harness:
        harness_query_parts = [
            goal or ranking_query,
            _done_when_text(done_when_checks),
            _harness_requirements_text(requirements),
            model_provider or "",
            model or "",
        ]
        if any(harness_query_parts):
            harness_query_parts.append("harness")
        harness_goal = " ".join(part for part in harness_query_parts if part)
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
    skill_names: list[str] = []
    for row in capability_bundle["skills"]:
        if len(skill_names) >= 3:
            break
        name = str(row.get("name") or "").strip()
        if name and _is_loadable_skill_row(row):
            skill_names.append(name)
    if skill_names:
        use_skills = "use skills: " + ", ".join(skill_names)
    mcp_server_tools = _ctx_mcp_tool_names(granted)
    use_tools = 'use tools from the "ctx" server' if mcp_server_tools else None
    mcp_server_command = "ctx-mcp-server" if mcp_server_tools else None

    return {
        "version": "ctx.loop_adapter.v1",
        "adapter": loop_kind,
        "permissions": {key: key in granted for key in _CAPABILITY_KEYS},
        "warnings": warnings,
        "context": {
            "goal": goal,
            "loop_name": loop_name,
            "look_at": context_paths,
            "done_when": done_when_checks,
            "last_failure_present": bool(last_failure),
            "query": public_query,
        },
        "mcp_server": {
            "name": "ctx",
            "command": mcp_server_command,
            "tools": mcp_server_tools,
        },
        "capabilities": capability_bundle,
        "loopflow": {
            "use_tools": use_tools,
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
    parser.add_argument("--done-when", action="append", default=[], help="Verification/check hint.")
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
    parser.add_argument("--api-key-env", default="")
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
        try:
            loop_fields = parse_loop_file(args.loop_file)
        except OSError as exc:
            parser.error(f"could not read --loop-file {args.loop_file}: {exc}")
    goal = args.goal or str(loop_fields.get("goal") or "")
    if not goal:
        parser.error("--goal or a loop file with goal: is required")
    loop_name = args.loop_name or str(loop_fields.get("name") or "")
    look_at = [*loop_fields.get("look_at", []), *_split_csv(args.look_at)]
    done_when = [
        *[str(value) for value in loop_fields.get("done_when", [])],
        *[str(value) for value in args.done_when],
    ]
    try:
        last_failure = args.last_failure or _read_text_file(args.last_failure_file)
    except OSError as exc:
        parser.error(f"could not read --last-failure-file {args.last_failure_file}: {exc}")
    requirements = {
        "runtime": args.harness_runtime,
        "autonomy": args.harness_autonomy,
        "tools": args.harness_tools,
        "verification": args.harness_verify,
        "privacy": args.harness_privacy,
        "attach_mode": args.harness_attach_mode,
        "api_key_env": args.api_key_env,
    }
    payload = recommend_for_loop(
        goal=goal,
        loop_name=loop_name,
        loop_kind=args.loop_kind,
        look_at=look_at,
        done_when=done_when,
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
