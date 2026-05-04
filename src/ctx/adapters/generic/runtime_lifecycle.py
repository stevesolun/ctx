"""Host-neutral runtime lifecycle logging for generic ctx integrations."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctx.core.entity_types import RECOMMENDABLE_ENTITY_TYPES
from ctx.core.wiki.wiki_utils import validate_skill_name
from ctx.utils._fs_utils import reject_symlink_path


_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ENTITY_TYPES = set(RECOMMENDABLE_ENTITY_TYPES)


@dataclass(frozen=True)
class RuntimeLifecycleStore:
    """Append-only lifecycle event store for custom/API/local harnesses."""

    root: Path | None = None

    def record_dev_event(
        self,
        *,
        session_id: str,
        event_type: str,
        host: str | None = None,
        cwd: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="dev_event",
            session_id=session_id,
            event_type=event_type or "generic",
            host=host,
            cwd=cwd,
            payload=payload or {},
        )

    def load_entity(
        self,
        *,
        session_id: str,
        entity_type: str,
        slug: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="load_requested",
            session_id=session_id,
            entity_type=entity_type,
            slug=slug,
            reason=reason,
        )

    def mark_entity_used(
        self,
        *,
        session_id: str,
        entity_type: str,
        slug: str,
        evidence: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="used",
            session_id=session_id,
            entity_type=entity_type,
            slug=slug,
            evidence=evidence,
        )

    def unload_entity(
        self,
        *,
        session_id: str,
        entity_type: str,
        slug: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="unload_requested",
            session_id=session_id,
            entity_type=entity_type,
            slug=slug,
            reason=reason,
        )

    def end_session(
        self,
        *,
        session_id: str,
        status: str | None = None,
        summary: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="session_end",
            session_id=session_id,
            status=status or "ended",
            summary=summary,
        )

    def _record(self, **event: Any) -> dict[str, Any]:
        session_id = _validate_session_id(str(event.get("session_id") or ""))
        entity_type = event.get("entity_type")
        slug = event.get("slug")
        if entity_type is not None:
            event["entity_type"] = _validate_entity_type(str(entity_type))
        if slug is not None:
            event["slug"] = _validate_slug(str(slug))
        event["session_id"] = session_id
        event["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        path = self.events_path
        reject_symlink_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        return {"ok": True, "event": event, "events_path": str(path)}

    @property
    def events_path(self) -> Path:
        root = self.root
        if root is None:
            root = Path(
                os.environ.get("CTX_RUNTIME_LIFECYCLE_DIR", "~/.ctx/runtime")
            ).expanduser()
        return root / "events.jsonl"


def _validate_session_id(raw: str) -> str:
    value = raw.strip()
    if not value or not _SESSION_RE.match(value):
        raise ValueError("session_id must be 1-128 safe characters")
    return value


def _validate_entity_type(raw: str) -> str:
    value = raw.strip()
    if value not in _ENTITY_TYPES:
        raise ValueError(
            "entity_type must be one of " + ", ".join(sorted(_ENTITY_TYPES))
        )
    return value


def _validate_slug(raw: str) -> str:
    value = raw.strip()
    validate_skill_name(value)
    return value
