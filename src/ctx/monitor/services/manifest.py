"""Manifest read/write helpers for ctx-monitor loaded entity state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import atomic_write_text
from ctx.utils._safe_name import is_safe_source_name


def default_manifest() -> dict[str, Any]:
    return {"load": [], "unload": [], "warnings": []}


def normalize_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return default_manifest()
    if not isinstance(manifest.get("load"), list):
        manifest["load"] = []
    if not isinstance(manifest.get("unload"), list):
        manifest["unload"] = []
    if not isinstance(manifest.get("warnings"), list):
        manifest["warnings"] = []
    return manifest


def save_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    atomic_write_text(manifest_path, json.dumps(manifest, indent=2) + "\n")


def read_skill_manifest_only(manifest_path: Path) -> dict[str, Any]:
    """Read the mutable skill manifest without synthetic harness rows."""
    if not manifest_path.exists():
        return default_manifest()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_manifest()
    return normalize_manifest(manifest)


def remove_loaded_manifest_entry(
    manifest_path: Path,
    slug: str,
    entity_type: str,
) -> list[dict[str, Any]]:
    """Remove loaded rows for one entity tuple and return removed rows."""
    with file_lock(manifest_path):
        manifest = read_skill_manifest_only(manifest_path)
        removed: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for entry in manifest.get("load", []):
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("entity_type") or "skill")
            if entry.get("skill") == slug and entry_type == entity_type:
                removed.append(entry)
            else:
                remaining.append(entry)
        if not removed:
            return []
        manifest["load"] = remaining
        unloaded = {
            (entry.get("skill"), str(entry.get("entity_type") or "skill"))
            for entry in manifest.get("unload", [])
            if isinstance(entry, dict)
        }
        preserved: dict[str, object] = {}
        for field in ("command", "json_config", "priority", "reason"):
            value = removed[0].get(field)
            if value not in (None, ""):
                preserved[field] = value
        if (slug, entity_type) not in unloaded:
            entry = {
                "skill": slug,
                "entity_type": entity_type,
                "source": removed[0].get("source") or "ctx-monitor",
            }
            entry.update(preserved)
            manifest.setdefault("unload", []).append(entry)
        elif preserved:
            for entry in manifest.get("unload", []):
                if not isinstance(entry, dict):
                    continue
                if (
                    entry.get("skill") == slug
                    and str(entry.get("entity_type") or "skill") == entity_type
                ):
                    for field, value in preserved.items():
                        entry.setdefault(field, value)
                    break
        save_manifest(manifest_path, manifest)
        return removed


def read_harness_install_rows(claude_dir: Path) -> list[dict[str, Any]]:
    """Return installed harness records as manifest-compatible load rows."""
    root = claude_dir / "harness-installs"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("status") != "installed":
            continue
        slug = str(data.get("slug") or path.stem).strip()
        if not slug or not is_safe_source_name(slug):
            continue
        rows.append(
            {
                "skill": slug,
                "entity_type": "harness",
                "source": "ctx-harness-install",
                "command": data.get("target") or data.get("repo_url") or "",
                "installed_at": data.get("installed_at", ""),
                "status": data.get("status", "installed"),
            }
        )
    return rows


def read_manifest(manifest_path: Path, claude_dir: Path) -> dict[str, Any]:
    """Return current loaded entities from the skill manifest plus harness installs."""
    manifest = read_skill_manifest_only(manifest_path)
    load_rows = manifest.setdefault("load", [])
    existing = {
        (str(row.get("entity_type") or "skill"), str(row.get("skill") or ""))
        for row in load_rows
        if isinstance(row, dict)
    }
    for row in read_harness_install_rows(claude_dir):
        key = ("harness", str(row.get("skill") or ""))
        if key not in existing:
            load_rows.append(row)
            existing.add(key)
    return manifest
