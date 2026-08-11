"""Load/unload lifecycle helpers for ctx dashboard entities."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ctx import dashboard_entities
from ctx.monitor.services import audit as audit_service
from ctx.monitor.services import manifest as manifest_service
from ctx.monitor.services import wiki as wiki_service


def entity_runtime_deps(
    *,
    wiki_dir: Callable[[], Path],
    claude_dir: Callable[[], Path],
    audit_log_path: Callable[[], Path],
    manifest_path: Callable[[], Path],
) -> dashboard_entities.EntityRuntimeDeps:
    return dashboard_entities.EntityRuntimeDeps(
        is_safe_slug=wiki_service.is_safe_slug,
        normalize_entity_type=wiki_service.normalize_entity_type,
        wiki_dir=wiki_dir,
        claude_dir=claude_dir,
        log_dashboard_entity_event=lambda entity_type, action, slug: (
            audit_service.log_dashboard_entity_event(
                audit_log_path(),
                entity_type,
                action,
                slug,
            )
        ),
        remove_loaded_manifest_entry=lambda slug, entity_type: (
            manifest_service.remove_loaded_manifest_entry(
                manifest_path(),
                slug,
                entity_type,
            )
        ),
    )


def perform_load(
    slug: str,
    entity_type: str = "skill",
    *,
    command: str | None = None,
    json_config: str | None = None,
    wiki_dir: Callable[[], Path],
    claude_dir: Callable[[], Path],
    audit_log_path: Callable[[], Path],
    manifest_path: Callable[[], Path],
) -> tuple[bool, str]:
    return dashboard_entities.perform_load(
        slug,
        entity_type,
        command=command,
        json_config=json_config,
        deps=entity_runtime_deps(
            wiki_dir=wiki_dir,
            claude_dir=claude_dir,
            audit_log_path=audit_log_path,
            manifest_path=manifest_path,
        ),
    )


def perform_unload(
    slug: str,
    entity_type: str = "skill",
    *,
    wiki_dir: Callable[[], Path],
    claude_dir: Callable[[], Path],
    audit_log_path: Callable[[], Path],
    manifest_path: Callable[[], Path],
) -> tuple[bool, str]:
    return dashboard_entities.perform_unload(
        slug,
        entity_type,
        deps=entity_runtime_deps(
            wiki_dir=wiki_dir,
            claude_dir=claude_dir,
            audit_log_path=audit_log_path,
            manifest_path=manifest_path,
        ),
    )
