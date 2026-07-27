"""LoopFlow and external agent-loop adapter for ctx recommendations."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys
import threading
from typing import Any

import ctx.api as ctx_api
from ctx.adapters.generic.ctx_core_tools import (
    _base_recommendation_row,
    _infer_no_api_keys_constraint,
    _is_local_loadable_skill_row,
    _recommendation_context_from_args,
    _recommendation_context_skip_reason,
)
from ctx.core.resolve.recommendations import query_to_tags, recommend_by_tags
from ctx.core.wiki.wiki_utils import validate_skill_name
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
_MCP_SCOPE_ENTITY_BY_GROUP = {"skills": "skill", "agents": "agent", "mcps": "mcp-server"}
_CAPABILITY_KEYS = ("skills", "agents", "mcps", "harnesses")
_ALL_CAPABILITY_GRANTS = frozenset(_CAPABILITY_KEYS)
_PROJECT_OWNED_RECOMMENDATION_SOURCE = "ctx-runtime-availability"
_READ_ONLY_MCP_TOOL_NAMES = frozenset(
    {
        "ctx__recommend_bundle",
        "ctx__graph_query",
        "ctx__recommend_related",
        "ctx__wiki_search",
        "ctx__wiki_get",
    }
)
_HARNESS_REQUIREMENT_FLAGS = {
    "runtime": "--harness-runtime",
    "autonomy": "--harness-autonomy",
    "tools": "--harness-tools",
    "verification": "--harness-verify",
    "privacy": "--harness-privacy",
    "attach_mode": "--harness-attach-mode",
    "api_key_env": "--api-key-env",
}
_LEASE_ENTITY_TO_GROUP = {
    "agent": "agents",
    "harness": "harnesses",
    "mcp-server": "mcps",
    "skill": "skills",
}
_LEASE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class ActivationLeaseActions:
    """Physical context changes required after one lease synchronization."""

    keep: tuple[str, ...] = ()
    load: tuple[str, ...] = ()
    use: tuple[str, ...] = ()
    unload: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "keep": list(self.keep),
            "load": list(self.load),
            "use": list(self.use),
            "unload": list(self.unload),
        }


class ActivationLeaseBusyError(RuntimeError):
    """A host transition is active and this operation must be retried."""


class ActivationLeaseRegistry:
    """Share host-owned context safely across LoopFlow and agent-loop leases."""

    def __init__(self) -> None:
        self._entities_by_lease: dict[str, set[str]] = {}
        self._leases_by_entity: dict[str, set[str]] = {}
        self._context_leases: set[str] = set()
        self._lock = threading.RLock()
        self._transition_lock = threading.Lock()
        self._callback_active = threading.Event()
        self._callback_local = threading.local()

    def sync(
        self,
        lease_id: str,
        *,
        desired: Iterable[str],
        permissions: set[str],
        apply: Callable[[ActivationLeaseActions], None],
        used: Iterable[str] = (),
        wait_for_transition: bool = False,
    ) -> ActivationLeaseActions:
        """Apply and commit one lease transition in deterministic order.

        ``desired`` is the complete context this loop intends to retain.
        ``used`` is the subset actually used during this synchronization.
        Entity IDs must be typed (for example ``skill:pytest``) so permission
        grants remain authoritative. ``apply`` must perform the returned host
        actions or raise; ownership changes commit only after it returns.
        Direct calls fail with ``ActivationLeaseBusyError`` during another host
        transition unless ``wait_for_transition`` is explicitly enabled.
        """

        owner = _validate_lease_id(lease_id)
        granted = _parse_permissions(list(permissions))
        desired_entities = _normalize_lease_entities(
            desired,
            permissions=granted,
            field="desired",
        )
        used_entities = _normalize_lease_entities(
            used,
            permissions=granted,
            field="used",
        )
        if not used_entities <= desired_entities:
            missing = ", ".join(sorted(used_entities - desired_entities))
            raise ValueError(f"used entities must also be desired: {missing}")

        self._acquire_transition(wait=wait_for_transition)
        try:
            with self._lock:
                entities_by_lease = {
                    lease: set(entities) for lease, entities in self._entities_by_lease.items()
                }
                leases_by_entity = {
                    entity: set(leases) for entity, leases in self._leases_by_entity.items()
                }
                previous = entities_by_lease.get(owner, set())
                acquired = desired_entities - previous
                released = previous - desired_entities
                load: set[str] = set()
                keep = set(desired_entities & previous)

                for entity_id in acquired:
                    owners = leases_by_entity.setdefault(entity_id, set())
                    if owners:
                        keep.add(entity_id)
                    else:
                        load.add(entity_id)
                    owners.add(owner)

                unload: set[str] = set()
                for entity_id in released:
                    owners = leases_by_entity[entity_id]
                    owners.discard(owner)
                    if owners:
                        keep.add(entity_id)
                    else:
                        unload.add(entity_id)
                        del leases_by_entity[entity_id]

                if desired_entities:
                    entities_by_lease[owner] = set(desired_entities)
                else:
                    entities_by_lease.pop(owner, None)

                actions = ActivationLeaseActions(
                    keep=tuple(sorted(keep)),
                    load=tuple(sorted(load)),
                    use=tuple(sorted(used_entities)),
                    unload=tuple(sorted(unload)),
                )
            self._apply_actions(actions, apply)
            with self._lock:
                self._entities_by_lease = entities_by_lease
                self._leases_by_entity = leases_by_entity
            return actions
        finally:
            self._transition_lock.release()

    def release(
        self,
        lease_id: str,
        *,
        apply: Callable[[ActivationLeaseActions], None],
        wait_for_transition: bool = False,
    ) -> ActivationLeaseActions:
        """Apply and commit a release; failed unloads remain retryable."""

        return self._release(
            lease_id,
            apply=apply,
            from_context=False,
            wait_for_transition=wait_for_transition,
        )

    def _release(
        self,
        lease_id: str,
        *,
        apply: Callable[[ActivationLeaseActions], None],
        from_context: bool,
        wait_for_transition: bool,
    ) -> ActivationLeaseActions:
        owner = _validate_lease_id(lease_id)
        self._acquire_transition(wait=wait_for_transition)
        try:
            with self._lock:
                if owner in self._context_leases and not from_context:
                    raise RuntimeError(f"lease {owner!r} is owned by an active context manager")
                entities_by_lease = {
                    lease: set(entities) for lease, entities in self._entities_by_lease.items()
                }
                leases_by_entity = {
                    entity: set(leases) for entity, leases in self._leases_by_entity.items()
                }
                released = entities_by_lease.pop(owner, set())
                keep: set[str] = set()
                unload: set[str] = set()
                for entity_id in released:
                    owners = leases_by_entity[entity_id]
                    owners.discard(owner)
                    if owners:
                        keep.add(entity_id)
                    else:
                        unload.add(entity_id)
                        del leases_by_entity[entity_id]
                actions = ActivationLeaseActions(
                    keep=tuple(sorted(keep)),
                    unload=tuple(sorted(unload)),
                )
            self._apply_actions(actions, apply)
            with self._lock:
                self._entities_by_lease = entities_by_lease
                self._leases_by_entity = leases_by_entity
            return actions
        finally:
            self._transition_lock.release()

    @contextmanager
    def lease(
        self,
        lease_id: str,
        *,
        desired: Iterable[str],
        permissions: set[str],
        apply: Callable[[ActivationLeaseActions], None],
        used: Iterable[str] | Callable[[], Iterable[str]] = (),
    ) -> Iterator[ActivationLeaseActions]:
        """Hold one lease, report observed use, and release on every exit path."""

        owner = _validate_lease_id(lease_id)
        desired_values = tuple(desired)
        permission_values = set(permissions)
        with self._lock:
            if owner in self._context_leases:
                raise ValueError(f"lease_id {owner!r} is already active")
            self._context_leases.add(owner)
        try:
            actions = self.sync(
                owner,
                desired=desired_values,
                permissions=permission_values,
                apply=apply,
                wait_for_transition=True,
            )
            failure: BaseException | None = None
            try:
                yield actions
            except BaseException as exc:
                failure = exc
                raise
            finally:
                cleanup_errors: list[BaseException] = []
                try:
                    used_values = tuple(used() if callable(used) else used)
                    if used_values:
                        self.sync(
                            owner,
                            desired=desired_values,
                            permissions=permission_values,
                            apply=apply,
                            used=used_values,
                            wait_for_transition=True,
                        )
                except BaseException as exc:
                    cleanup_errors.append(exc)
                try:
                    self._release(
                        owner,
                        apply=apply,
                        from_context=True,
                        wait_for_transition=True,
                    )
                except BaseException as exc:
                    cleanup_errors.append(exc)
                if cleanup_errors:
                    for error in cleanup_errors:
                        detail = f"activation lease cleanup failed: {type(error).__name__}: {error}"
                        if failure is not None:
                            failure.add_note(detail)
                        else:
                            cleanup_errors[0].add_note(detail)
                    if failure is None:
                        raise cleanup_errors[0]
        finally:
            with self._lock:
                self._context_leases.discard(owner)

    def active_context(self) -> tuple[str, ...]:
        """Return the context currently owned by at least one live lease."""

        with self._lock:
            return tuple(sorted(self._leases_by_entity))

    def _apply_actions(
        self,
        actions: ActivationLeaseActions,
        apply: Callable[[ActivationLeaseActions], None],
    ) -> None:
        self._callback_active.set()
        self._callback_local.active = True
        try:
            apply(actions)
        finally:
            self._callback_local.active = False
            self._callback_active.clear()

    def _acquire_transition(self, *, wait: bool) -> None:
        if bool(getattr(self._callback_local, "active", False)):
            raise ActivationLeaseBusyError(
                "activation lease callbacks must not invoke or wait on registry operations"
            )
        if wait:
            self._transition_lock.acquire()
            return
        if self._callback_active.is_set() or not self._transition_lock.acquire(blocking=False):
            raise ActivationLeaseBusyError("activation lease transition busy; retry the operation")


def _validate_lease_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("lease_id must be a string")
    lease_id = value.strip()
    if not _LEASE_ID_RE.fullmatch(lease_id):
        raise ValueError("lease_id must be 1-128 safe characters")
    return lease_id


def _normalize_lease_entities(
    values: Iterable[str],
    *,
    permissions: set[str],
    field: str,
) -> set[str]:
    entities: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise TypeError(f"{field} entities must be strings")
        entity_id = _selection_key(value)
        entity_type, separator, slug = entity_id.partition(":")
        group = _LEASE_ENTITY_TO_GROUP.get(entity_type)
        if not separator or not slug or group is None:
            raise ValueError(
                f"{field} entity {value!r} must be a typed skill, agent, mcp-server, or harness ID"
            )
        validate_skill_name(slug)
        if group not in permissions:
            raise ValueError(f"{field} entity {entity_id!r} is not granted by permissions")
        entities.add(entity_id)
    return entities


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


def _ordered_permissions(permissions: set[str]) -> list[str]:
    return [key for key in _CAPABILITY_KEYS if key in permissions]


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
    permission_grants: set[str] = set()
    for match in re.finditer(r"^\s*ctx\s+grants?\s*:\s*(.+)$", text, flags=re.MULTILINE):
        permission_grants.update(_parse_permissions([match.group(1)]))
    if permission_grants:
        fields["permissions"] = _ordered_permissions(permission_grants)
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


def _context_basename(value: str) -> str:
    stripped = value.strip().rstrip("/\\")
    if not stripped:
        return ""
    return re.split(r"[\\/]", stripped)[-1] or stripped


def _safe_context_refs(values: list[str]) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    for value in values:
        stripped = value.strip()
        if not stripped:
            continue
        items.append(
            {
                "basename": _context_basename(stripped),
                "path_hash": hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16],
            }
        )
    return {"count": len(items), "items": items}


def _safe_context_query_items(safe_context: dict[str, Any]) -> list[str]:
    items = safe_context.get("items", [])
    if not items:
        return []
    return [
        f"count={safe_context.get('count', len(items))}",
        *[f"basename={item['basename']} path_hash={item['path_hash']}" for item in items],
    ]


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
        "id",
        "name",
        "type",
        "tldr",
        "reason",
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
        "installable",
        "load_status",
        "source_path",
        "selected",
        "selection_state",
        "related_to",
    ):
        value = row.get(key)
        if value not in (None, "", [], {}):
            compact[key] = value
    return compact


def _is_loadable_skill_row(row: dict[str, Any]) -> bool:
    return _is_local_loadable_skill_row(row)


def _is_actionable_capability_row(row: dict[str, Any]) -> bool:
    if "installable" not in row and "load_status" not in row:
        return True
    if row.get("installable") is True:
        return True
    return bool(str(row.get("install_command") or "").strip())


def _selection_key(value: str) -> str:
    item = value.strip().lower()
    if item.startswith("mcp:"):
        return "mcp-server:" + item.split(":", 1)[1]
    return item


def _selection_keys(values: list[str]) -> set[str]:
    keys: set[str] = set()
    for value in values:
        item = value.strip()
        if item:
            keys.add(_selection_key(item))
    return keys


def _row_selection_keys(row: dict[str, Any], name: str) -> set[str]:
    return _selection_keys([str(row.get("id") or f"{row.get('type')}:{name}"), name])


def _loop_recommendation_context(
    query: str,
    *,
    no_api_keys: bool | None,
) -> dict[str, Any]:
    resolved_no_api_keys = (
        _infer_no_api_keys_constraint(query) if no_api_keys is None else no_api_keys
    )
    return _recommendation_context_from_args(
        query,
        {"no_api_keys": resolved_no_api_keys},
    )


def _group_bundle(
    rows: list[dict[str, Any]],
    *,
    permissions: set[str],
    excluded: set[str] | None = None,
    local_loadable_skills_only: bool = False,
    context: dict[str, Any] | None = None,
    top_k: int,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in ("skills", "agents", "mcps")}
    seen: set[tuple[str, str]] = set()
    excluded_keys = excluded or set()
    for row in rows:
        group = _ENTITY_TO_GROUP.get(str(row.get("type") or ""))
        name = str(row.get("name") or "").strip()
        if group is None or group not in permissions or not name:
            continue
        identity = (group, name)
        if identity in seen:
            continue
        if _row_selection_keys(row, name) & excluded_keys:
            continue
        if group == "skills" and local_loadable_skills_only and not _is_loadable_skill_row(row):
            continue
        if context is not None and _recommendation_context_skip_reason(row, context) is not None:
            continue
        seen.add(identity)
        if len(grouped[group]) < top_k:
            grouped[group].append(_compact_row(row))
    return grouped


def _filter_related_rows(
    rows: list[dict[str, Any]],
    *,
    permissions: set[str],
    excluded: set[str] | None = None,
    local_loadable_skills_only: bool = False,
    context: dict[str, Any] | None = None,
    top_k: int,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    excluded_keys = excluded or set()
    for row in rows:
        group = _ENTITY_TO_GROUP.get(str(row.get("type") or ""))
        name = str(row.get("name") or "").strip()
        if group is None or group not in permissions or not name:
            continue
        identity = (group, name)
        if identity in seen:
            continue
        if _row_selection_keys(row, name) & excluded_keys:
            continue
        if not _is_actionable_capability_row(row):
            continue
        if group == "skills" and local_loadable_skills_only and not _is_loadable_skill_row(row):
            continue
        if context is not None and _recommendation_context_skip_reason(row, context) is not None:
            continue
        seen.add(identity)
        filtered.append(_compact_row(row))
        if len(filtered) >= top_k:
            break
    return filtered


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
    if "mcps" not in permissions:
        return []
    tool_names = ctx_api.ctx_core_tool_names()
    if _ALL_CAPABILITY_GRANTS <= permissions:
        return tool_names
    return [name for name in tool_names if name in _READ_ONLY_MCP_TOOL_NAMES]


def _ctx_mcp_server_args(permissions: set[str], tool_names: list[str]) -> list[str]:
    if not tool_names or _ALL_CAPABILITY_GRANTS <= permissions:
        return []
    entity_types = [
        entity_type
        for group, entity_type in _MCP_SCOPE_ENTITY_BY_GROUP.items()
        if group in permissions
    ]
    return [
        "--allow-tools",
        ",".join(tool_names),
        "--entity-types",
        ",".join(entity_types),
    ]


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


def _capability_row(row: dict[str, Any], *, wiki_dir: Path | None) -> dict[str, Any]:
    enriched = _base_recommendation_row(row, wiki_dir=wiki_dir)
    row_id = str(row.get("id") or "").strip()
    if not row_id:
        entity_type = str(enriched.get("type") or "").strip()
        name = str(enriched.get("name") or "").strip()
        if entity_type and name:
            row_id = f"{entity_type}:{name}"
    if row_id:
        enriched["id"] = _selection_key(row_id)
    return enriched


def _project_owned_fallback_rows(
    graph: Any,
    *,
    query: str,
    entity_types: tuple[str, ...],
    wiki_dir: Path | None,
    recommendation_context: dict[str, Any],
) -> list[dict[str, Any]]:
    try:
        node_ids = [
            node_id
            for node_id, data in graph.nodes(data=True)
            if str(data.get("source") or "").strip() == _PROJECT_OWNED_RECOMMENDATION_SOURCE
            and str(data.get("type") or "") in entity_types
        ]
        if not node_ids:
            return []
        fallback_graph = graph.subgraph(node_ids).copy()
    except (AttributeError, TypeError):
        return []

    source_counts = dict(fallback_graph.graph.get("source_catalog_nodes") or {})
    source_counts["skills.sh"] = 1
    fallback_graph.graph["source_catalog_nodes"] = source_counts
    local_loadable_skills_only = bool(
        recommendation_context.get("local_code_task") or recommendation_context.get("no_api_keys")
    )
    rows: list[dict[str, Any]] = []
    for raw in recommend_by_tags(
        fallback_graph,
        query_to_tags(query),
        top_n=len(node_ids),
        query=query,
        entity_types=entity_types,
        min_normalized_score=0.0,
    ):
        row = _capability_row(raw, wiki_dir=wiki_dir)
        if (
            row.get("type") == "skill"
            and local_loadable_skills_only
            and not _is_loadable_skill_row(row)
        ):
            continue
        if _recommendation_context_skip_reason(row, recommendation_context) is not None:
            continue
        rows.append(row)
    return rows


def _recommend_capability_rows(
    query: str,
    *,
    permissions: set[str],
    top_k: int,
    no_api_keys: bool | None = None,
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

    wiki_dir = ctx_api.default_wiki_dir()
    raw_rows = recommend_by_tags(
        graph,
        tags,
        top_n=top_k * len(entity_types),
        query=query,
        entity_types=tuple(entity_types),
        min_normalized_score=cfg.recommendation_min_normalized_score,
        candidate_filter=lambda row: _is_actionable_capability_row(
            _capability_row(dict(row), wiki_dir=wiki_dir)
        ),
    )
    rows = [_capability_row(row, wiki_dir=wiki_dir) for row in raw_rows]
    recommendation_context = _loop_recommendation_context(
        query,
        no_api_keys=no_api_keys,
    )
    if any(
        bool(recommendation_context.get(key))
        for key in ("local_code_task", "no_api_keys", "language")
    ):
        rows.extend(
            _project_owned_fallback_rows(
                graph,
                query=query,
                entity_types=tuple(entity_types),
                wiki_dir=wiki_dir,
                recommendation_context=recommendation_context,
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
    selected: list[str] | None = None,
    rejected: list[str] | None = None,
    session_id: str | None = None,
    rejection_mode: str = "use",
    no_api_keys: bool | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Return a permissioned ctx adapter payload for a DSL or agent loop.

    Empty permissions fail closed. ``last_failure`` may influence ranking, but
    the raw failure text is omitted from the returned context and capability
    rows.
    """
    safe_top_k = max(1, min(int(top_k), 20))
    if rejection_mode not in {"use", "replace", "ignore"}:
        raise ValueError("rejection_mode must be one of ignore, replace, use")
    granted = permissions or set()
    context_paths = look_at or []
    safe_context = _safe_context_refs(context_paths)
    done_when_checks = [value.strip() for value in (done_when or []) if value.strip()]
    requirements, unknown_requirement_keys = _normalize_harness_requirements(
        harness_requirements or {}
    )
    public_query = _build_query(
        goal=goal,
        loop_name=loop_name,
        look_at=_safe_context_query_items(safe_context),
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
        model=None,
        model_provider=None,
    )

    capability_bundle: dict[str, list[dict[str, Any]]] = {
        "skills": [],
        "agents": [],
        "mcps": [],
        "harnesses": [],
    }
    selected_ids = [value.strip() for value in (selected or []) if value.strip()]
    rejected_ids = [value.strip() for value in (rejected or []) if value.strip()]
    if session_id is not None:
        rejected_ids = ctx_api.recommendation_rejections(
            rejected_ids,
            session_id=session_id,
            rejection_mode=rejection_mode,
        )
    excluded_ids = _selection_keys(selected_ids + rejected_ids)
    recommendation_context = _loop_recommendation_context(
        ranking_query,
        no_api_keys=no_api_keys,
    )
    local_loadable_skills_only = bool(
        recommendation_context.get("local_code_task") or recommendation_context.get("no_api_keys")
    )
    context_filters_active = any(
        bool(recommendation_context.get(key))
        for key in ("local_code_task", "no_api_keys", "language")
    )
    if granted.intersection({"skills", "agents", "mcps"}):
        fetch_top_k = safe_top_k
        if context_filters_active:
            fetch_top_k = 50
        elif excluded_ids or local_loadable_skills_only:
            fetch_top_k = min(50, safe_top_k + len(excluded_ids) + 5)
        if no_api_keys is None:
            rows = _recommend_capability_rows(
                ranking_query,
                permissions=granted,
                top_k=fetch_top_k,
            )
        else:
            rows = _recommend_capability_rows(
                ranking_query,
                permissions=granted,
                top_k=fetch_top_k,
                no_api_keys=no_api_keys,
            )
        capability_bundle.update(
            _group_bundle(
                rows,
                permissions=granted,
                excluded=excluded_ids,
                local_loadable_skills_only=local_loadable_skills_only,
                context=recommendation_context,
                top_k=safe_top_k,
            )
        )
    related_recommendations: list[dict[str, Any]] = []
    if selected_ids and granted.intersection({"skills", "agents", "mcps"}):
        related_kwargs: dict[str, Any] = {}
        if session_id is not None:
            related_kwargs = {
                "session_id": session_id,
                "rejection_mode": "ignore",
            }
        related_recommendations = _filter_related_rows(
            ctx_api.recommend_related(
                selected_ids,
                rejected=rejected_ids,
                top_n=50,
                **related_kwargs,
            ),
            permissions=granted,
            excluded=excluded_ids,
            local_loadable_skills_only=local_loadable_skills_only,
            context=recommendation_context,
            top_k=safe_top_k,
        )

    warnings: list[str] = []
    if unknown_requirement_keys:
        warnings.append(
            "ignored unknown harness requirement(s): " + ", ".join(unknown_requirement_keys)
        )
    should_recommend_harness = "harnesses" in granted and own_llm
    if "harnesses" in granted and not should_recommend_harness:
        warnings.append(
            "harnesses permission granted but --own-llm/user-owned model consent was not declared"
        )
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
        harness_top_k = min(50, safe_top_k + len(excluded_ids) + 5) if excluded_ids else safe_top_k
        for row in recommend_harnesses(
            harness_goal,
            top_k=harness_top_k,
            model_provider=model_provider,
            model=model,
        ):
            name = str(row.get("name") or "").strip()
            if not name or _row_selection_keys(row, name) & excluded_ids:
                continue
            capability_bundle["harnesses"].append(_compact_row(row))
            if len(capability_bundle["harnesses"]) >= safe_top_k:
                break

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
    mcp_server_args = _ctx_mcp_server_args(granted, mcp_server_tools)
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
            "look_at": safe_context,
            "done_when": done_when_checks,
            "last_failure_present": bool(last_failure),
            "query": public_query,
        },
        "mcp_server": {
            "name": "ctx",
            "command": mcp_server_command,
            "args": mcp_server_args,
            "tools": mcp_server_tools,
        },
        "capabilities": capability_bundle,
        "related_recommendations": related_recommendations,
        "selection": {
            "selected": selected_ids,
            "rejected": rejected_ids,
            "session_bound": session_id is not None,
            "rejection_mode": rejection_mode,
        },
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
        "--selected",
        action="append",
        default=[],
        help="Comma-separated selected ctx recommendation IDs or names.",
    )
    parser.add_argument(
        "--rejected",
        action="append",
        default=[],
        help="Comma-separated rejected ctx recommendation IDs or names.",
    )
    parser.add_argument(
        "--session-id",
        help="Optional host session id for recommendation rejection memory.",
    )
    parser.add_argument(
        "--rejection-mode",
        choices=("use", "replace", "ignore"),
        default="use",
        help="Use, replace, or ignore remembered rejections for this session.",
    )
    parser.add_argument(
        "--permissions",
        action="append",
        help=(
            "Comma-separated capability grants: skills, agents, mcps, harnesses. "
            "Overrides ctx grants in a .loop file."
        ),
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
    api_key_group = parser.add_mutually_exclusive_group()
    api_key_group.add_argument(
        "--no-api-keys",
        dest="no_api_keys",
        action="store_true",
        default=None,
        help="Force local/no-key recommendation filtering.",
    )
    api_key_group.add_argument(
        "--api-keys-available",
        dest="no_api_keys",
        action="store_false",
        default=None,
        help="Disable inferred no-key filtering when credentials are available.",
    )
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--compact", action="store_true", help="Print compact JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cli_permissions_provided = args.permissions is not None
    try:
        permissions = _parse_permissions(args.permissions) if cli_permissions_provided else set()
    except ValueError as exc:
        parser.error(str(exc))

    loop_fields: dict[str, Any] = {}
    if args.loop_file is not None:
        try:
            loop_fields = parse_loop_file(args.loop_file)
        except OSError as exc:
            parser.error(f"could not read --loop-file {args.loop_file}: {exc}")
        except ValueError as exc:
            parser.error(f"could not parse --loop-file {args.loop_file}: {exc}")
    if not cli_permissions_provided:
        permissions = set(loop_fields.get("permissions", []))
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
        selected=_split_csv(args.selected),
        rejected=_split_csv(args.rejected),
        session_id=args.session_id,
        rejection_mode=args.rejection_mode,
        no_api_keys=args.no_api_keys,
        top_k=args.top_k,
    )
    json.dump(payload, sys.stdout, indent=None if args.compact else 2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
