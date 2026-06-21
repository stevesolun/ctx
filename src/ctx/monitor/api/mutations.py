"""Mutation route payloads for ctx-monitor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MutationApiResponse:
    payload: dict[str, Any]
    status: int = 200


@dataclass(frozen=True)
class MutationApiDeps:
    perform_load: Callable[[str, str, dict[str, str]], tuple[bool, str]]
    perform_unload: Callable[[str, str], tuple[bool, str]]
    save_config_updates: Callable[[dict[str, Any]], dict[str, Any]]
    upsert_wiki_entity: Callable[[dict[str, Any]], tuple[bool, str]]
    delete_wiki_entity: Callable[[str, str], tuple[bool, str]]


def handle_mutation_route(
    name: str,
    body: Mapping[str, Any],
    deps: MutationApiDeps,
) -> MutationApiResponse | None:
    if name == "api_load":
        slug = str(body.get("slug", "")).strip()
        entity_type = str(body.get("entity_type", "skill")).strip() or "skill"
        kwargs: dict[str, str] = {}
        command = body.get("command")
        json_config = body.get("json_config")
        if isinstance(command, str) and command:
            kwargs["command"] = command
        if isinstance(json_config, str) and json_config:
            kwargs["json_config"] = json_config
        ok, detail = deps.perform_load(slug, entity_type, kwargs)
        return MutationApiResponse({"ok": ok, "detail": detail}, status=200 if ok else 400)
    if name == "api_unload":
        slug = str(body.get("slug", "")).strip()
        entity_type = str(body.get("entity_type", "skill")).strip() or "skill"
        ok, detail = deps.perform_unload(slug, entity_type)
        return MutationApiResponse({"ok": ok, "detail": detail}, status=200 if ok else 400)
    if name == "api_config":
        updates = body.get("updates", {})
        if not isinstance(updates, dict):
            return MutationApiResponse(
                {"ok": False, "detail": "updates must be an object"},
                status=400,
            )
        result = deps.save_config_updates(updates)
        return MutationApiResponse(result, status=200 if result.get("ok") else 400)
    if name == "api_entity_upsert":
        ok, detail = deps.upsert_wiki_entity(dict(body))
        return MutationApiResponse({"ok": ok, "detail": detail}, status=200 if ok else 400)
    if name == "api_entity_delete":
        slug = str(body.get("slug", "")).strip()
        entity_type = str(body.get("entity_type", "skill")).strip() or "skill"
        ok, detail = deps.delete_wiki_entity(slug, entity_type)
        return MutationApiResponse({"ok": ok, "detail": detail}, status=200 if ok else 400)
    return None
