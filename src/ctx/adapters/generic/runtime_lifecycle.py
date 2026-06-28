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
from ctx.telemetry import (
    ensure_private_event_file,
    hash_identifier,
    record_event,
    sanitize_payload,
    telemetry_span,
    telemetry_enabled,
)
from ctx.utils._fs_utils import reject_symlink_path


_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ENTITY_TYPES = set(RECOMMENDABLE_ENTITY_TYPES)
_VALIDATION_STATUSES = {"passed", "failed", "skipped", "error"}
_ESCALATION_STATUSES = {"open", "resolved", "ignored"}
_SECURITY_SCAN_STATUSES = {
    "passed",
    "findings",
    "missing",
    "error",
    "skipped",
    "not_provided",
}


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
        security_scan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entity_type = _validate_entity_type(entity_type)
        slug = _validate_slug(slug)
        return self._record(
            action="load_requested",
            session_id=session_id,
            entity_type=entity_type,
            slug=slug,
            reason=reason,
            security_scan=_security_scan_state(
                security_scan,
                entity_type=entity_type,
                slug=slug,
            ),
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

    def record_validation(
        self,
        *,
        session_id: str,
        check_name: str,
        status: str,
        command: str | None = None,
        summary: str | None = None,
        entity_type: str | None = None,
        slug: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="validation",
            session_id=session_id,
            check_name=_validate_nonempty(check_name, "check_name"),
            status=_validate_choice(status, _VALIDATION_STATUSES, "status"),
            command=command,
            summary=summary,
            entity_type=entity_type,
            slug=slug,
            payload=payload or {},
        )

    def record_escalation(
        self,
        *,
        session_id: str,
        trigger: str,
        reason: str,
        severity: str | None = None,
        status: str | None = None,
        entity_type: str | None = None,
        slug: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="escalation",
            session_id=session_id,
            trigger=_validate_nonempty(trigger, "trigger"),
            reason=_validate_nonempty(reason, "reason"),
            severity=severity or "blocking",
            status=_validate_choice(status or "open", _ESCALATION_STATUSES, "status"),
            entity_type=entity_type,
            slug=slug,
            payload=payload or {},
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

    def session_state(
        self,
        *,
        session_id: str,
        min_unused_seconds: float = 0,
    ) -> dict[str, Any]:
        session_id = _validate_session_id(session_id)
        loaded: dict[tuple[str, str], dict[str, Any]] = {}
        unloaded: list[dict[str, Any]] = []
        validations: list[dict[str, Any]] = []
        escalations: list[dict[str, Any]] = []
        min_age = max(0.0, float(min_unused_seconds))
        now = time.time()
        latest_dev_event_epoch: float | None = None

        for event in self._events_for_session(session_id):
            action = event.get("action")
            if action == "dev_event":
                latest_dev_event_epoch = float(event.get("created_at_epoch") or 0)
                continue
            if action == "validation":
                validations.append(_validation_state(event))
                continue
            if action == "escalation":
                escalations.append(_escalation_state(event))
                continue
            key = (str(event.get("entity_type") or ""), str(event.get("slug") or ""))
            if not key[0] or not key[1]:
                continue
            if action == "load_requested":
                loaded[key] = {
                    "entity_type": key[0],
                    "slug": key[1],
                    "loaded_at": event.get("created_at"),
                    "loaded_at_epoch": float(event.get("created_at_epoch") or 0),
                    "reason": event.get("reason"),
                    "security_scan": event.get("security_scan"),
                    "used": False,
                    "use_count": 0,
                    "last_used_at": None,
                    "evidence": [],
                    "dev_event_epoch": latest_dev_event_epoch,
                }
            elif action == "used" and key in loaded:
                loaded[key]["used"] = True
                loaded[key]["use_count"] = int(loaded[key]["use_count"]) + 1
                loaded[key]["last_used_at"] = event.get("created_at")
                if event.get("evidence"):
                    loaded[key]["evidence"].append(event["evidence"])
            elif action == "unload_requested":
                current = loaded.pop(key, None)
                unloaded.append({
                    "entity_type": key[0],
                    "slug": key[1],
                    "unloaded_at": event.get("created_at"),
                    "reason": event.get("reason"),
                    "was_loaded": current is not None,
                    "was_used": bool(current and current.get("used")),
                })

        loaded_entries = list(loaded.values())
        unload_candidates = [
            entry for entry in loaded_entries
            if not entry["used"]
            and _loaded_before_latest_dev_event(entry, latest_dev_event_epoch)
            and (min_age == 0 or now - float(entry.get("loaded_at_epoch") or 0) >= min_age)
        ]
        return {
            "ok": True,
            "session_id": session_id,
            "loaded": loaded_entries,
            "used": [entry for entry in loaded_entries if entry["used"]],
            "unload_candidates": unload_candidates,
            "unloaded": unloaded,
            "validations": validations,
            "escalations": escalations,
            "latest_validation_status": (
                str(validations[-1]["status"]) if validations else None
            ),
            "open_escalations": [
                event for event in escalations if event["status"] == "open"
            ],
        }

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
        event["created_at_epoch"] = time.time()
        if not telemetry_enabled():
            return {
                "ok": True,
                "events_path": str(self.events_path),
                "recorded": False,
            }
        event = _sanitize_lifecycle_event(event)
        path = self.events_path
        reject_symlink_path(path)
        ensure_private_event_file(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
        _record_runtime_lifecycle_telemetry(event)
        return {"ok": True, "event": event, "events_path": str(path), "recorded": True}

    def _events_for_session(self, session_id: str) -> list[dict[str, Any]]:
        path = self.events_path
        reject_symlink_path(path)
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("session_id") == session_id:
                events.append(event)
        return events

    @property
    def events_path(self) -> Path:
        root = self.root
        if root is None:
            root = Path(
                os.environ.get("CTX_RUNTIME_LIFECYCLE_DIR", "~/.ctx/runtime")
            ).expanduser()
        return root / "events.jsonl"


def _sanitize_lifecycle_event(event: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(event)
    payload = redacted.get("payload")
    if isinstance(payload, dict):
        redacted["payload"] = sanitize_payload(payload)
    cwd = redacted.pop("cwd", None)
    if isinstance(cwd, str) and cwd:
        redacted["cwd_hash"] = hash_identifier(cwd)
    return redacted


def _validate_session_id(raw: str) -> str:
    value = raw.strip()
    if not value or not _SESSION_RE.match(value):
        raise ValueError("session_id must be 1-128 safe characters")
    return value


def _record_runtime_lifecycle_telemetry(event: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "ctx.lifecycle.action": str(event.get("action") or ""),
        "ctx.payload.present": bool(event.get("payload")),
        "otel.status_code": "OK",
    }
    entity_type = event.get("entity_type")
    if isinstance(entity_type, str) and entity_type:
        payload["ctx.entity.type"] = entity_type
    slug = event.get("slug")
    if isinstance(slug, str) and slug:
        payload["ctx.slug.hash"] = hash_identifier(slug)
    status = event.get("status")
    if isinstance(status, str) and status:
        payload["ctx.status"] = status
    security_scan = event.get("security_scan")
    if isinstance(security_scan, dict):
        payload["ctx.security_scan.status"] = str(security_scan.get("status") or "")
    try:
        with telemetry_span():
            record_event(
                "ctx.runtime_lifecycle.record",
                source="ctx-runtime-lifecycle",
                transport="local-jsonl",
                session_id=str(event.get("session_id") or "") or None,
                outcome="ok",
                payload=payload,
            )
    except Exception:  # noqa: BLE001 - lifecycle writes must not depend on telemetry.
        pass


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


def _validate_nonempty(raw: str, field: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError(f"{field} must be non-empty")
    return value


def _validate_choice(raw: str, allowed: set[str], field: str) -> str:
    value = raw.strip().lower()
    if value not in allowed:
        raise ValueError(f"{field} must be one of {', '.join(sorted(allowed))}")
    return value


def _validation_state(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "check_name": event.get("check_name"),
        "status": event.get("status"),
        "command": event.get("command"),
        "summary": event.get("summary"),
        "entity_type": event.get("entity_type"),
        "slug": event.get("slug"),
        "payload": event.get("payload") or {},
    }


def _security_scan_state(
    raw: dict[str, Any] | None,
    *,
    entity_type: str,
    slug: str,
) -> dict[str, Any] | None:
    if raw is None:
        if entity_type != "skill":
            return None
        return {
            "status": "not_provided",
            "scanner": "skillspector",
            "required": False,
            "summary": (
                "No SkillSpector scan proof was provided by the host for this "
                "skill load."
            ),
            "recommended_command": f"ctx-skill-install {slug} --security-scan-required",
        }

    status = _validate_choice(
        str(raw.get("status") or ""),
        _SECURITY_SCAN_STATUSES,
        "security_scan.status",
    )
    state: dict[str, Any] = {
        "status": status,
        "scanner": str(raw.get("scanner") or "skillspector"),
        "required": bool(raw.get("required", False)),
    }
    for key in ("command", "exit_code", "output", "summary"):
        if key in raw:
            state[key] = raw[key]
    return state


def _escalation_state(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "trigger": event.get("trigger"),
        "reason": event.get("reason"),
        "severity": event.get("severity"),
        "status": event.get("status"),
        "entity_type": event.get("entity_type"),
        "slug": event.get("slug"),
        "payload": event.get("payload") or {},
    }


def _loaded_before_latest_dev_event(
    entry: dict[str, Any],
    latest_dev_event_epoch: float | None,
) -> bool:
    if latest_dev_event_epoch is None:
        return True
    loaded_window = entry.get("dev_event_epoch")
    if loaded_window is None:
        return True
    return float(loaded_window) < latest_dev_event_epoch
