#!/usr/bin/env python3
"""
skill_unload.py -- Unload skills/agents from the current session or permanently suppress them.

Usage:
    python skill_unload.py --name fastapi-pro              # Unload from current session
    python skill_unload.py --name fastapi-pro --permanent   # Set never_load: true in wiki
    python skill_unload.py --names "fastapi-pro,docker-expert"
    python skill_unload.py --stale                          # Unload all stale skills
    python skill_unload.py --list-loaded                    # Show currently loaded skills
    python skill_unload.py --list-never                     # Show permanently suppressed skills
    python skill_unload.py --restore fastapi-pro            # Remove never_load flag
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from ctx.core.graph.graph_packs import (
    GraphPackManifestError,
    discover_pack_manifests,
    load_merged_pack_graph,
    write_overlay_pack,
)
from ctx.core.wiki import wiki_queue
from ctx.core.wiki.wiki_packs import (
    WikiPackManifestError,
    load_merged_wiki_pages,
    write_active_wiki_overlay_pack,
)
from ctx.core.wiki.wiki_utils import validate_skill_name
from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import atomic_write_text as _atomic_write_text
from ctx_config import cfg

CLAUDE_DIR = cfg.claude_dir
MANIFEST_PATH = cfg.skill_manifest
PENDING_UNLOAD = CLAUDE_DIR / "pending-unload.json"
WIKI_DIR = cfg.wiki_dir
SKILL_ENTITIES = WIKI_DIR / "entities" / "skills"
AGENT_ENTITIES = WIKI_DIR / "entities" / "agents"


@dataclass(frozen=True)
class EntityPageRef:
    name: str
    subject_type: str
    path: Path
    relpath: str
    content: str


def _graph_node_id_for_subject_type(name: str, subject_type: str) -> str | None:
    if subject_type == "skills":
        return f"skill:{name}"
    if subject_type == "agents":
        return f"agent:{name}"
    return None


def _sync_graph_never_load_for_entity(ref: EntityPageRef, value: bool) -> bool:
    """Best-effort mirror of never_load into graph artifacts for merged wiki entities."""
    node_id = _graph_node_id_for_subject_type(ref.name, ref.subject_type)
    return _sync_graph_never_load_for_node(node_id, value)


def _sync_graph_never_load_for_node(node_id: str | None, value: bool) -> bool:
    """Best-effort mirror of never_load into graph artifacts for immediate filtering."""
    if node_id is None:
        return False
    legacy_changed = _sync_graph_json_never_load(node_id, value)
    pack_changed = _sync_graph_pack_never_load(node_id, value)
    changed = legacy_changed or pack_changed
    if changed:
        _queue_graph_store_refresh(node_id, value)
    return changed


def _queue_graph_store_refresh(node_id: str, value: bool) -> None:
    """Queue a hot graph-store rebuild after graph metadata changes."""
    try:
        wiki_queue.enqueue_maintenance_job(
            WIKI_DIR,
            kind=wiki_queue.GRAPH_STORE_REFRESH_JOB,
            payload={
                "reason": "never_load",
                "node_id": node_id,
                "never_load": value,
            },
            source="skill_unload",
        )
    except Exception as exc:  # noqa: BLE001 - refresh is best-effort for CLI UX.
        print(f"Warning: failed to queue graph store refresh: {exc}", file=sys.stderr)


def _sync_graph_json_never_load(node_id: str, value: bool) -> bool:
    graph_json = WIKI_DIR / "graphify-out" / "graph.json"
    if not graph_json.is_file():
        return False
    try:
        payload = json.loads(graph_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list):
        return False
    changed = False
    for node in nodes:
        if not isinstance(node, dict) or node.get("id") != node_id:
            continue
        if bool(node.get("never_load")) != value:
            node["never_load"] = value
            changed = True
        break
    if not changed:
        return False
    _atomic_write_text(graph_json, json.dumps(payload, indent=2))
    return True


def _sync_graph_pack_never_load(node_id: str, value: bool) -> bool:
    packs_dir = WIKI_DIR / "graphify-out" / "packs"
    try:
        entries = discover_pack_manifests(packs_dir)
        if not entries:
            return False
        graph = load_merged_pack_graph(packs_dir)
        if node_id not in graph or bool(graph.nodes[node_id].get("never_load")) == value:
            return False
        base = entries[0].manifest
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        digest = sha256(f"{node_id}:{value}".encode("utf-8")).hexdigest()[:12]
        stem = node_id.replace(":", "-")
        pack_id = f"overlay-{timestamp}-{stem}-never-load-{digest}"
        for suffix in ["", *[f"-{index}" for index in range(1, 1000)]]:
            candidate = f"{pack_id}{suffix}"
            pack_dir = packs_dir / candidate
            if pack_dir.exists():
                continue
            write_overlay_pack(
                pack_dir=pack_dir,
                pack_id=candidate,
                base_export_id=base.base_export_id,
                parent_export_id=base.base_export_id,
                config_hash=base.config_hash,
                model_id=base.model_id,
                nodes=[{"id": node_id, "never_load": value}],
                edges=[],
                tombstones=[],
            )
            return True
    except (GraphPackManifestError, OSError):
        return False
    return False


def _sanitize_yaml_value(value: str) -> str:
    """Strip newlines/CRs so a value can't inject extra YAML keys."""
    return value.replace("\r", " ").replace("\n", " ").strip()


def _entity_subjects(entity_type: str | None = None) -> list[str]:
    if entity_type == "skill":
        return ["skills"]
    if entity_type == "agent":
        return ["agents"]
    return ["skills", "agents"]


def _entity_dir(subject_type: str) -> Path:
    if subject_type == "skills":
        return SKILL_ENTITIES
    if subject_type == "agents":
        return AGENT_ENTITIES
    raise ValueError(f"unknown subject_type: {subject_type}")


def _entity_relpath(subject_type: str, name: str) -> str:
    return f"entities/{subject_type}/{name}.md"


def _iter_entity_page_refs(*, entity_type: str | None = None) -> list[EntityPageRef]:
    packs_dir = WIKI_DIR / "wiki-packs"
    subjects = set(_entity_subjects(entity_type))
    if packs_dir.is_dir():
        refs: list[EntityPageRef] = []
        try:
            pages = load_merged_wiki_pages(packs_dir)
        except (WikiPackManifestError, OSError) as exc:
            print(f"Warning: failed to read wiki packs: {exc}", file=sys.stderr)
            pages = {}
        for relpath, content in sorted(pages.items()):
            path = Path(relpath)
            if (
                len(path.parts) == 3
                and path.parts[0] == "entities"
                and path.parts[1] in subjects
                and path.suffix == ".md"
            ):
                refs.append(
                    EntityPageRef(
                        name=path.stem,
                        subject_type=path.parts[1],
                        path=WIKI_DIR / relpath,
                        relpath=relpath,
                        content=content,
                    )
                )
        return refs

    legacy_refs: list[EntityPageRef] = []
    for subject_type in _entity_subjects(entity_type):
        entity_dir = _entity_dir(subject_type)
        if not entity_dir.exists():
            continue
        for page in sorted(entity_dir.glob("*.md")):
            try:
                content = page.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"Warning: entity page read error for {page.stem}: {exc}", file=sys.stderr)
                continue
            legacy_refs.append(
                EntityPageRef(
                    name=page.stem,
                    subject_type=subject_type,
                    path=page,
                    relpath=_entity_relpath(subject_type, page.stem),
                    content=content,
                )
            )
    return legacy_refs


def _find_entity_page_ref(name: str, *, entity_type: str | None = None) -> EntityPageRef | None:
    try:
        validate_skill_name(name)
    except ValueError:
        return None
    for ref in _iter_entity_page_refs(entity_type=entity_type):
        if ref.name == name:
            return ref
    return None


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"load": [], "unload": [], "warnings": []}


def save_manifest(manifest: dict) -> None:
    _atomic_write_text(MANIFEST_PATH, json.dumps(manifest, indent=2))


def _set_frontmatter_field_text(content: str, field: str, value: str) -> tuple[str, bool]:
    safe_value = _sanitize_yaml_value(value)
    escaped_field = re.escape(field)
    pattern = rf"^{escaped_field}:\s*.+$"
    replacement = f"{field}: {safe_value}"
    new_content, count = re.subn(pattern, replacement, content, count=1, flags=re.MULTILINE)
    if count == 0:
        # Field doesn't exist; add it after the opening frontmatter delimiter.
        new_content = re.sub(r"(---\n)", rf"\1{field}: {safe_value}\n", content, count=1)
    return new_content, new_content != content


def set_frontmatter_field(filepath: Path, field: str, value: str) -> bool:
    """Set a YAML frontmatter field in a wiki entity page. Returns True if changed.

    Hardened: ``field`` is re-escaped before regex use (prevents ReDoS and
    regex-injection via caller-controlled field names); ``value`` is sanitized
    to strip newlines that would inject extra YAML keys.
    """
    if not filepath.exists():
        return False
    content = filepath.read_text(encoding="utf-8", errors="replace")
    new_content, changed = _set_frontmatter_field_text(content, field, value)
    if changed:
        _atomic_write_text(filepath, new_content)
        return True
    return False


def _set_entity_frontmatter_field(ref: EntityPageRef, field: str, value: str) -> bool:
    new_content, changed = _set_frontmatter_field_text(ref.content, field, value)
    if not changed:
        return False
    if ref.path.exists():
        _atomic_write_text(ref.path, new_content)
    try:
        write_active_wiki_overlay_pack(
            packs_dir=WIKI_DIR / "wiki-packs",
            pages={ref.relpath: new_content},
            tombstones=[],
        )
    except (WikiPackManifestError, OSError) as exc:
        print(f"Warning: failed to mirror entity update into wiki pack: {exc}", file=sys.stderr)
    return True


def find_entity_page(name: str, entity_type: str | None = None) -> Path | None:
    """Find entity page for a skill or agent by name.

    Hardened against path traversal (CWE-22): name is validated against
    ``SAFE_NAME_RE`` before any filesystem access.
    """
    try:
        validate_skill_name(name)
    except ValueError:
        return None
    for subject_type in _entity_subjects(entity_type):
        page = _entity_dir(subject_type) / f"{name}.md"
        if page.exists():
            return page
    return None


def clear_pending_unload(names: list[str]) -> None:
    """Remove unloaded skills from pending-unload.json."""
    if not PENDING_UNLOAD.exists():
        return
    try:
        data = json.loads(PENDING_UNLOAD.read_text(encoding="utf-8"))
        data["suggestions"] = [s for s in data.get("suggestions", []) if s["name"] not in names]
        _atomic_write_text(PENDING_UNLOAD, json.dumps(data, indent=2))
    except Exception as exc:
        print(f"Warning: failed to clear pending unload: {exc}", file=sys.stderr)


def unload_from_session(
    names: list[str],
    *,
    entity_type: str | None = None,
) -> list[str]:
    """Remove skills/agents from the current session manifest.

    Emits one ``unload`` line per removed skill to
    ``~/.claude/skill-events.jsonl`` and one ``skill.unloaded`` record
    to the unified audit log. The random-load-unload playbook caught
    that loads flowed through skill-events.jsonl but unloads didn't,
    making the two halves of the lifecycle asymmetric at the ground-
    truth log level. Fix both at the single choke point.
    """
    import uuid

    names_set = set(names)
    with file_lock(MANIFEST_PATH):
        manifest = load_manifest()
        removed: list[str] = []
        removed_entries: list[dict] = []
        remaining = []
        for entry in manifest.get("load", []):
            entry_type = entry.get("entity_type", "skill")
            if entry["skill"] in names_set and (entity_type is None or entry_type == entity_type):
                removed.append(entry["skill"])
                removed_entries.append(dict(entry))
                manifest.setdefault("unload", []).append(entry)
            else:
                remaining.append(entry)
        manifest["load"] = remaining
        save_manifest(manifest)

    # Emit ground-truth unload events. Best-effort — a disk failure on
    # the event log must not block the manifest save above.
    if removed:
        events_path = CLAUDE_DIR / "skill-events.jsonl"
        session_id = os.environ.get("CTX_SESSION_ID") or f"unload-{uuid.uuid4().hex[:8]}"
        try:
            import skill_telemetry

            for entry in removed_entries:
                slug = str(entry.get("skill") or "")
                skill_telemetry.log_event(
                    "unload",
                    slug,
                    session_id,
                    meta={"source": "skill_unload"},
                    entity_type=str(entry.get("entity_type") or "skill"),
                    path=events_path,
                    trusted_root=CLAUDE_DIR,
                )
        except Exception as exc:  # noqa: BLE001 - event log is best-effort.
            print(f"Warning: failed to log unload events: {exc}", file=sys.stderr)
        try:
            from ctx_audit_log import log_skill_event

            for entry in removed_entries:
                slug = str(entry.get("skill") or "")
                entry_type = str(entry.get("entity_type") or "skill")
                log_skill_event(
                    f"{entry_type}.unloaded",
                    slug,
                    actor="cli",
                    session_id=session_id,
                    meta={"via": "skill_unload"},
                )
        except Exception:  # noqa: BLE001 — audit best-effort
            pass

    return removed


def set_never_load(names: list[str], *, entity_type: str | None = None) -> list[str]:
    """Set never_load: true in wiki entity pages."""
    updated: list[str] = []
    for name in names:
        page = _find_entity_page_ref(name, entity_type=entity_type)
        if page:
            changed = _set_entity_frontmatter_field(page, "never_load", "true")
            graph_changed = _sync_graph_never_load_for_entity(page, True)
        else:
            changed = graph_changed = False
        if page and (changed or graph_changed):
            updated.append(name)
            print(f"  {name}: never_load set to true")
        elif page:
            print(f"  {name}: already set to never_load")
        else:
            print(f"  {name}: entity page not found", file=sys.stderr)
    return updated


def restore_load(names: list[str], *, entity_type: str | None = None) -> list[str]:
    """Remove never_load flag from wiki entity pages."""
    restored: list[str] = []
    for name in names:
        page = _find_entity_page_ref(name, entity_type=entity_type)
        if page:
            changed = _set_entity_frontmatter_field(page, "never_load", "false")
            graph_changed = _sync_graph_never_load_for_entity(page, False)
        else:
            changed = graph_changed = False
        if page and (changed or graph_changed):
            restored.append(name)
            print(f"  {name}: never_load removed, skill can be recommended again")
        elif page:
            print(f"  {name}: was not suppressed")
        else:
            print(f"  {name}: entity page not found", file=sys.stderr)
    return restored


def get_stale_skills(*, entity_type: str | None = None) -> list[str]:
    """Find all skills with status: stale in their entity pages."""
    stale: list[str] = []
    for page in _iter_entity_page_refs(entity_type=entity_type):
        if re.search(r"^status:\s*stale", page.content, re.MULTILINE):
            stale.append(page.name)
    return stale


def list_loaded(*, entity_type: str | None = None) -> None:
    """Show currently loaded skills/agents."""
    manifest = load_manifest()
    loaded = [
        entry
        for entry in manifest.get("load", [])
        if entity_type is None or entry.get("entity_type", "skill") == entity_type
    ]
    if not loaded:
        print("No skills/agents currently loaded in this session.")
        return
    print(f"Currently loaded ({len(loaded)}):\n")
    for entry in loaded:
        source = entry.get("source", "unknown")
        print(f"  - {entry['skill']}  (source: {source})")


def list_never_load(*, entity_type: str | None = None) -> None:
    """Show permanently suppressed skills/agents."""
    suppressed: list[str] = []
    for page in _iter_entity_page_refs(entity_type=entity_type):
        if re.search(r"^never_load:\s*true", page.content, re.MULTILINE):
            suppressed.append(page.name)
    if not suppressed:
        print("No skills/agents are permanently suppressed.")
        return
    print(f"Permanently suppressed ({len(suppressed)}):\n")
    for name in sorted(suppressed):
        print(f"  - {name}")
    print("\nTo restore: python src/skill_unload.py --restore <name>")


def main(argv: list[str] | None = None, *, default_entity_type: str | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Unload skills/agents from session or suppress permanently"
    )
    # ``--slug`` is an alias for ``--name`` so docs/playbooks that say
    # "slug" (the canonical vocabulary everywhere else in ctx) work
    # without the user remembering that this one CLI uses "name".
    parser.add_argument("--name", "--slug", dest="name", help="Skill or agent slug to unload")
    parser.add_argument("--names", help="Comma-separated slugs to unload")
    parser.add_argument(
        "--session-id",
        dest="session_id",
        help="Override CTX_SESSION_ID env for the emitted "
        "unload events (default: env var or synthetic id)",
    )
    parser.add_argument(
        "--permanent", action="store_true", help="Set never_load: true (won't be recommended again)"
    )
    parser.add_argument(
        "--entity-type",
        choices=("skill", "agent"),
        default=default_entity_type,
        help="Limit the operation to one entity type",
    )
    parser.add_argument("--stale", action="store_true", help="Unload all stale skills")
    parser.add_argument("--restore", help="Remove never_load flag from a skill/agent")
    parser.add_argument("--list-loaded", action="store_true", help="Show currently loaded skills")
    parser.add_argument(
        "--list-never", action="store_true", help="Show permanently suppressed skills"
    )
    args = parser.parse_args(argv)
    entity_type = args.entity_type

    # Propagate --session-id into the env so unload_from_session() and
    # its audit-log call both tag the emitted events with the caller's
    # chosen session id.
    if args.session_id:
        os.environ["CTX_SESSION_ID"] = args.session_id

    if args.list_loaded:
        list_loaded(entity_type=entity_type)
        return

    if args.list_never:
        list_never_load(entity_type=entity_type)
        return

    if args.restore:
        raw_names = [n.strip() for n in args.restore.split(",")]
        valid: list[str] = []
        for n in raw_names:
            try:
                valid.append(validate_skill_name(n))
            except ValueError as exc:
                print(f"Skipping invalid name {n!r}: {exc}", file=sys.stderr)
        if valid:
            restore_load(valid, entity_type=entity_type)
        return

    names: list[str] = []
    if args.name:
        names.append(args.name)
    if args.names:
        names.extend(n.strip() for n in args.names.split(","))
    if args.stale:
        stale = get_stale_skills(entity_type=entity_type)
        print(f"Found {len(stale)} stale skills")
        names.extend(stale)

    if not names:
        parser.print_help()
        sys.exit(1)

    # Reject any name that doesn't match the allowlist before we touch the FS.
    safe_names: list[str] = []
    for n in names:
        try:
            safe_names.append(validate_skill_name(n))
        except ValueError as exc:
            print(f"Skipping invalid name {n!r}: {exc}", file=sys.stderr)
    names = safe_names
    if not names:
        sys.exit(1)

    # Unload from current session manifest
    removed = unload_from_session(names, entity_type=entity_type)
    if removed:
        print(f"Unloaded from session: {', '.join(removed)}")

    # Mark as stale in wiki (so they drop in priority next session)
    not_removed = [n for n in names if n not in removed]
    if not_removed:
        for name in not_removed:
            page = _find_entity_page_ref(name, entity_type=entity_type)
            if page:
                _set_entity_frontmatter_field(page, "status", "stale")
                print(f"  {name}: marked stale (lower priority next session)")

    # Always clear from pending-unload
    clear_pending_unload(names)

    # Permanently suppress if requested
    if args.permanent:
        print("Setting never_load: true (will not be recommended again):")
        set_never_load(names, entity_type=entity_type)


def skill_main() -> None:
    main(default_entity_type="skill")


def agent_main() -> None:
    main(default_entity_type="agent")


if __name__ == "__main__":
    main()
