"""Host-neutral runtime lifecycle logging for generic ctx integrations.

Runtime lifecycle events are local operational records, but user- or
host-provided free text is still privacy-sensitive. The store redacts secrets
and local paths from top-level lifecycle text, nested payloads, source context,
and security-scan details before appending events.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
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
    record_counter,
    record_event,
    record_histogram,
    sanitize_payload,
    telemetry_span,
    telemetry_enabled,
)
from ctx.utils._file_lock import file_lock
from ctx.utils._fs_utils import reject_symlink_path, safe_atomic_write_text
from ctx.utils._secret_scan import redact_secret_text


_logger = logging.getLogger(__name__)
_REJECTION_CHECKPOINT_VERSION = 1
_REJECTION_CHECKPOINT_ANCHOR_BYTES = 4096
_SESSION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ENTITY_TYPES = set(RECOMMENDABLE_ENTITY_TYPES)
_VALIDATION_STATUSES = {"passed", "failed", "skipped", "error"}
_ESCALATION_STATUSES = {"open", "resolved", "ignored"}
_SELECTION_SOURCES = {"user", "system", "host", "unknown"}
_TOKEN_ATTRIBUTIONS = {"exact", "estimated", "unavailable"}
_TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "total_tokens",
)
_TOKEN_USAGE_METADATA_FIELDS = ("attribution_reason", "model", "provider")
_LEGACY_ATTRIBUTION_REASON = "legacy token usage without attribution; treated as estimated"
_INCONSISTENT_TOTAL_REASON = "inconsistent total token usage; treated as estimated"
_MALFORMED_REPORTED_REASON = "invalid tokens_reported value; treated as estimated"
_UNREPORTED_EXACT_REASON = "exact token usage was not fully reported; treated as estimated"
_INCOMPLETE_EXACT_REASON = "incomplete exact token usage; treated as unavailable"
_LIFECYCLE_SANITIZER_CONFIG = {"enabled": True, "mode": "local_redacted"}
_LIFECYCLE_FREE_TEXT_FIELDS = ("reason", "evidence", "command", "summary", "trigger", "status")
_PATH_SEGMENT_RE = r"[^/\s'\"`<>|:;,\)\]]+"
_UNIX_ABSOLUTE_PATH_RE = re.compile(rf"(?<![\w./-])/(?:{_PATH_SEGMENT_RE}/)+{_PATH_SEGMENT_RE}")
_TILDE_PATH_RE = re.compile(rf"(?<![\w./-])~/(?:{_PATH_SEGMENT_RE}/)*{_PATH_SEGMENT_RE}")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w./-])[A-Za-z]:\\(?:[^\\\s'\"`<>|:;,\)\]]+\\)*"
    r"[^\\\s'\"`<>|:;,\)\]]+"
)
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
    """Append-only, privacy-redacted event store for custom/API/local harnesses."""

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
        selected: bool | None = None,
        selection_source: str | None = None,
        source_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entity_type = _validate_entity_type(entity_type)
        slug = _validate_slug(slug)
        source = _validate_choice(
            selection_source or "unknown",
            _SELECTION_SOURCES,
            "selection_source",
        )
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
            selected=False if selected is None else bool(selected),
            selection_source=source,
            source_context=source_context or {},
        )

    def mark_entity_used(
        self,
        *,
        session_id: str,
        entity_type: str,
        slug: str,
        evidence: str | None = None,
        token_usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = self._record(
            action="used",
            session_id=session_id,
            entity_type=entity_type,
            slug=slug,
            evidence=evidence,
            token_usage=_token_usage_state(token_usage),
        )
        return event

    def mark_entity_loaded(
        self,
        *,
        session_id: str,
        entity_type: str,
        slug: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="load_applied",
            session_id=session_id,
            entity_type=entity_type,
            slug=slug,
            reason=reason,
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

    def mark_entity_unloaded(
        self,
        *,
        session_id: str,
        entity_type: str,
        slug: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return self._record(
            action="unload_applied",
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

    def recommendation_rejections(self, *, session_id: str) -> list[str]:
        """Return the latest canonical recommendation rejection set."""
        session_id = _validate_session_id(session_id)
        path = self.events_path
        reject_symlink_path(path)
        if not path.is_file():
            return []
        with file_lock(path):
            state = self._rejection_checkpoint_unlocked()
        return list(state.get(session_id, []))

    def remember_recommendation_rejections(
        self,
        *,
        session_id: str,
        rejected: list[str],
        merge: bool = False,
    ) -> list[str]:
        """Atomically persist a complete or merged canonical rejection snapshot."""
        session_id = _validate_session_id(session_id)
        supplied = _deduplicate_recommendation_ids(rejected)
        path = self.events_path
        reject_symlink_path(path)
        with file_lock(path):
            state = self._rejection_checkpoint_unlocked()
            stored = list(state.get(session_id, []))
            normalised = _deduplicate_recommendation_ids(stored + supplied) if merge else supplied
            if stored != normalised:
                self._record(
                    _lock_held=True,
                    action="recommendation_rejections",
                    session_id=session_id,
                    rejected=normalised,
                )
                if normalised:
                    state[session_id] = normalised
                else:
                    state.pop(session_id, None)
                self._write_rejection_checkpoint_unlocked(state)
        return normalised

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
        rejected_recommendations: list[str] = []
        min_age = max(0.0, float(min_unused_seconds))
        now = time.time()
        latest_dev_event_epoch: float | None = None

        for event in self._events_for_session(session_id):
            action = event.get("action")
            if action == "recommendation_rejections":
                snapshot = _validated_rejection_snapshot(
                    event.get("rejected"),
                    session_id=session_id,
                )
                if snapshot is not None:
                    rejected_recommendations = snapshot
                continue
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
            if action in {"load_requested", "load_applied"}:
                current = loaded.get(key)
                if current is None:
                    current = {
                        "entity_type": key[0],
                        "slug": key[1],
                        "loaded_at": event.get("created_at"),
                        "loaded_at_epoch": float(event.get("created_at_epoch") or 0),
                        "reason": event.get("reason"),
                        "security_scan": event.get("security_scan"),
                        "selected": bool(event.get("selected", False)),
                        "selection_source": event.get("selection_source") or "unknown",
                        "source_context": event.get("source_context") or {},
                        "used": False,
                        "use_count": 0,
                        "last_used_at": None,
                        "evidence": [],
                        "dev_event_epoch": latest_dev_event_epoch,
                        "token_usage": _empty_token_usage_summary(),
                        "load_status": "requested",
                        "applied_at": None,
                        "applied_at_epoch": None,
                    }
                    loaded[key] = current
                elif action == "load_requested":
                    current["reason"] = event.get("reason") or current["reason"]
                    current["security_scan"] = (
                        event.get("security_scan") or current["security_scan"]
                    )
                    current["selected"] = bool(event.get("selected", current["selected"]))
                    current["selection_source"] = (
                        event.get("selection_source") or current["selection_source"]
                    )
                    current["source_context"] = (
                        event.get("source_context") or current["source_context"]
                    )
                if action == "load_applied":
                    current["load_status"] = "applied"
                    current["applied_at"] = event.get("created_at")
                    current["applied_at_epoch"] = float(event.get("created_at_epoch") or 0)
            elif action == "used" and key in loaded:
                loaded[key]["used"] = True
                loaded[key]["use_count"] = int(loaded[key]["use_count"]) + 1
                loaded[key]["last_used_at"] = event.get("created_at")
                if event.get("evidence"):
                    loaded[key]["evidence"].append(event["evidence"])
                token_usage = normalize_historical_token_usage(event.get("token_usage"))
                _merge_token_usage(loaded[key]["token_usage"], token_usage)
            elif action == "unload_requested":
                current = loaded.get(key)
                pending = next(
                    (
                        entry
                        for entry in reversed(unloaded)
                        if entry["entity_type"] == key[0]
                        and entry["slug"] == key[1]
                        and entry["unload_status"] == "requested"
                    ),
                    None,
                )
                if pending is None:
                    unloaded.append(
                        {
                            "entity_type": key[0],
                            "slug": key[1],
                            "unloaded_at": event.get("created_at"),
                            "reason": event.get("reason"),
                            "was_loaded": bool(current and current.get("load_status") == "applied"),
                            "was_used": bool(current and current.get("used")),
                            "unload_status": "requested",
                        }
                    )
                else:
                    pending["unloaded_at"] = event.get("created_at")
                    pending["reason"] = event.get("reason") or pending["reason"]
                    pending["was_loaded"] = bool(
                        pending["was_loaded"]
                        or (current and current.get("load_status") == "applied")
                    )
                    pending["was_used"] = bool(
                        pending["was_used"] or (current and current.get("used"))
                    )
            elif action == "unload_applied":
                current = loaded.pop(key, None)
                pending = next(
                    (
                        entry
                        for entry in reversed(unloaded)
                        if entry["entity_type"] == key[0]
                        and entry["slug"] == key[1]
                        and entry["unload_status"] == "requested"
                    ),
                    None,
                )
                if pending is not None:
                    pending["unloaded_at"] = event.get("created_at")
                    pending["reason"] = event.get("reason") or pending["reason"]
                    pending["was_loaded"] = bool(
                        pending["was_loaded"]
                        or (current and current.get("load_status") == "applied")
                    )
                    pending["was_used"] = bool(
                        pending["was_used"] or (current and current.get("used"))
                    )
                    pending["unload_status"] = "applied"
                else:
                    unloaded.append(
                        {
                            "entity_type": key[0],
                            "slug": key[1],
                            "unloaded_at": event.get("created_at"),
                            "reason": event.get("reason"),
                            "was_loaded": bool(current and current.get("load_status") == "applied"),
                            "was_used": bool(current and current.get("used")),
                            "unload_status": "applied",
                        }
                    )

        loaded_entries = [
            entry for entry in loaded.values() if entry.get("load_status") == "applied"
        ]
        requested_entries = [
            entry for entry in loaded.values() if entry.get("load_status") == "requested"
        ]
        unload_candidates = [
            entry
            for entry in loaded_entries
            if not entry["used"]
            and _loaded_before_latest_dev_event(entry, latest_dev_event_epoch)
            and (min_age == 0 or now - float(entry.get("loaded_at_epoch") or 0) >= min_age)
        ]
        return {
            "ok": True,
            "session_id": session_id,
            "loaded": loaded_entries,
            "requested": requested_entries,
            "used": [entry for entry in loaded_entries if entry["used"]],
            "unload_candidates": unload_candidates,
            "unloaded": unloaded,
            "rejected_recommendations": rejected_recommendations,
            "validations": validations,
            "escalations": escalations,
            "latest_validation_status": (str(validations[-1]["status"]) if validations else None),
            "open_escalations": [event for event in escalations if event["status"] == "open"],
        }

    def _record(self, *, _lock_held: bool = False, **event: Any) -> dict[str, Any]:
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
        event = _sanitize_lifecycle_event(event)
        path = self.events_path

        def append() -> None:
            reject_symlink_path(path)
            ensure_private_event_file(path)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, sort_keys=True) + "\n")

        if _lock_held:
            append()
        else:
            reject_symlink_path(path)
            with file_lock(path):
                append()
        _record_runtime_lifecycle_telemetry(event)
        return {"ok": True, "event": event, "recorded": True}

    def _events_for_session(self, session_id: str) -> list[dict[str, Any]]:
        path = self.events_path
        reject_symlink_path(path)
        if not path.is_file():
            return []
        with file_lock(path):
            return self._events_for_session_unlocked(session_id)

    def _events_for_session_unlocked(self, session_id: str) -> list[dict[str, Any]]:
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

    def _rejection_checkpoint_unlocked(self) -> dict[str, list[str]]:
        events_path = self.events_path
        checkpoint_path = self.recommendation_checkpoint_path
        reject_symlink_path(events_path)
        reject_symlink_path(checkpoint_path)
        events_stat = events_path.stat() if events_path.is_file() else None
        events_size = int(events_stat.st_size) if events_stat is not None else 0
        state: dict[str, list[str]] = {}
        offset = 0
        checkpoint_valid = False

        if checkpoint_path.is_file():
            try:
                raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("version") != _REJECTION_CHECKPOINT_VERSION:
                    raise ValueError("unsupported checkpoint")
                raw_offset = raw.get("offset")
                if isinstance(raw_offset, bool) or not isinstance(raw_offset, int):
                    raise ValueError("invalid checkpoint offset")
                if raw_offset < 0 or raw_offset > events_size:
                    raise ValueError("checkpoint offset outside event log")
                expected_file_id = _event_file_id(events_stat)
                if raw.get("event_file_id") != expected_file_id:
                    raise ValueError("event log identity changed")
                if raw.get("anchor") != _event_log_anchor(events_path, raw_offset):
                    raise ValueError("event log checkpoint anchor changed")
                state = _validate_rejection_checkpoint_sessions(raw.get("sessions"))
                offset = raw_offset
                checkpoint_valid = True
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                _logger.warning("ctx runtime lifecycle: rebuilding malformed rejection checkpoint")

        updated_offset = _scan_rejection_events(
            events_path,
            offset=offset,
            state=state,
        )
        if not checkpoint_valid or updated_offset != offset:
            self._write_rejection_checkpoint_unlocked(state, offset=updated_offset)
        return state

    def _write_rejection_checkpoint_unlocked(
        self,
        state: dict[str, list[str]],
        *,
        offset: int | None = None,
    ) -> None:
        events_path = self.events_path
        checkpoint_path = self.recommendation_checkpoint_path
        reject_symlink_path(events_path)
        reject_symlink_path(checkpoint_path)
        events_stat = events_path.stat() if events_path.is_file() else None
        resolved_offset = (
            int(events_stat.st_size) if offset is None and events_stat else offset or 0
        )
        payload = {
            "version": _REJECTION_CHECKPOINT_VERSION,
            "event_file_id": _event_file_id(events_stat),
            "offset": resolved_offset,
            "anchor": _event_log_anchor(events_path, resolved_offset),
            "sessions": state,
        }
        safe_atomic_write_text(
            checkpoint_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        )

    @property
    def events_path(self) -> Path:
        root = self.root
        if root is None:
            root = Path(os.environ.get("CTX_RUNTIME_LIFECYCLE_DIR", "~/.ctx/runtime")).expanduser()
        return root / "events.jsonl"

    @property
    def recommendation_checkpoint_path(self) -> Path:
        return self.events_path.with_name("recommendation-rejections.json")


def _sanitize_lifecycle_event(event: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(event)
    payload = redacted.get("payload")
    if isinstance(payload, dict):
        redacted["payload"] = sanitize_payload(payload, config=_LIFECYCLE_SANITIZER_CONFIG)
    token_usage = redacted.get("token_usage")
    if isinstance(token_usage, dict):
        metadata = {
            field: token_usage[field]
            for field in ("attribution_reason", "model", "provider")
            if field in token_usage
        }
        redacted_usage = dict(token_usage)
        redacted_usage.update(
            sanitize_payload(
                metadata,
                config=_LIFECYCLE_SANITIZER_CONFIG,
            )
        )
        redacted["token_usage"] = redacted_usage
    source_context = redacted.get("source_context")
    if isinstance(source_context, dict):
        redacted["source_context"] = sanitize_payload(
            source_context,
            config=_LIFECYCLE_SANITIZER_CONFIG,
        )
    security_scan = redacted.get("security_scan")
    if isinstance(security_scan, dict):
        redacted["security_scan"] = _sanitize_security_scan(security_scan)
    for field in _LIFECYCLE_FREE_TEXT_FIELDS:
        value = redacted.get(field)
        if isinstance(value, str):
            redacted[field] = _sanitize_free_text(value)
    cwd = redacted.pop("cwd", None)
    if isinstance(cwd, str) and cwd:
        redacted["cwd_hash"] = hash_identifier(cwd)
    return redacted


def _sanitize_free_text(text: str) -> str:
    return _redact_path_text(redact_secret_text(text))


def _sanitize_security_scan(raw: Any) -> Any:
    if isinstance(raw, str):
        return _sanitize_free_text(raw)
    if isinstance(raw, list):
        return [_sanitize_security_scan(item) for item in raw]
    if isinstance(raw, tuple):
        return [_sanitize_security_scan(item) for item in raw]
    if isinstance(raw, dict):
        return {str(key): _sanitize_security_scan(value) for key, value in raw.items()}
    return raw


def _redact_path_text(text: str) -> str:
    redacted = text
    for pattern in (_UNIX_ABSOLUTE_PATH_RE, _TILDE_PATH_RE, _WINDOWS_ABSOLUTE_PATH_RE):
        redacted = pattern.sub("[redacted-path]", redacted)
    return redacted


def _validate_session_id(raw: str) -> str:
    value = raw.strip()
    if not value or not _SESSION_RE.match(value):
        raise ValueError("session_id must be 1-128 safe characters")
    return value


def _record_runtime_lifecycle_telemetry(event: dict[str, Any]) -> None:
    token_usage = event.get("token_usage")
    usage_attribution: str | None = None
    if isinstance(token_usage, dict):
        usage_attribution = str(token_usage.get("attribution") or "unavailable")
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
    selection_source = event.get("selection_source")
    if isinstance(selection_source, str) and selection_source:
        payload["ctx.selection.source"] = selection_source
    selected = event.get("selected")
    if isinstance(selected, bool):
        payload["ctx.selection.selected"] = selected
    security_scan = event.get("security_scan")
    if isinstance(security_scan, dict):
        payload["ctx.security_scan.status"] = str(security_scan.get("status") or "")
    if usage_attribution is not None:
        payload["ctx.usage.attribution"] = usage_attribution
        for usage_key in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            usage_value = token_usage.get(usage_key) if isinstance(token_usage, dict) else None
            payload[f"ctx.usage.{usage_key}"] = (
                usage_value if isinstance(usage_value, int) else None
            )
        tokens_reported = (
            token_usage.get("tokens_reported") if isinstance(token_usage, dict) else None
        )
        if isinstance(tokens_reported, bool):
            payload["ctx.usage.tokens_reported"] = tokens_reported
        cost_value = token_usage.get("cost_usd") if isinstance(token_usage, dict) else None
        payload["ctx.usage.cost_usd"] = (
            float(cost_value) if isinstance(cost_value, (int, float)) else None
        )
    try:
        with telemetry_span():
            if isinstance(token_usage, dict) and usage_attribution is not None:
                _record_token_usage_metrics(event, token_usage=token_usage)
            if not telemetry_enabled():
                return
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


def _record_token_usage_metrics(
    event: dict[str, Any],
    *,
    token_usage: dict[str, Any],
) -> None:
    attribution = str(token_usage.get("attribution") or "unavailable")
    attrs: dict[str, Any] = {
        "ctx.lifecycle.action": str(event.get("action") or ""),
        "ctx.usage.attribution": attribution,
    }
    entity_type = event.get("entity_type")
    if isinstance(entity_type, str) and entity_type:
        attrs["ctx.entity.type"] = entity_type
    tokens_reported = token_usage.get("tokens_reported")
    if isinstance(tokens_reported, bool):
        attrs["ctx.usage.tokens_reported"] = tokens_reported
    session_id = str(event.get("session_id") or "") or None
    try:
        record_counter(
            "ctx.tool_usage.records",
            value=1,
            unit="1",
            attributes=attrs,
            source="ctx-runtime-lifecycle",
            session_id=session_id,
        )
        metric_names = {
            "input_tokens": "ctx.tool_usage.input_tokens",
            "cached_input_tokens": "ctx.tool_usage.cached_input_tokens",
            "cache_write_input_tokens": "ctx.tool_usage.cache_write_input_tokens",
            "uncached_input_tokens": "ctx.tool_usage.uncached_input_tokens",
            "output_tokens": "ctx.tool_usage.output_tokens",
            "total_tokens": "ctx.tool_usage.tokens",
        }
        for usage_key, metric_name in metric_names.items():
            value = token_usage.get(usage_key)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            record_counter(
                metric_name,
                value=value,
                unit="tokens",
                attributes=attrs,
                source="ctx-runtime-lifecycle",
                session_id=session_id,
            )
        total_tokens = token_usage.get("total_tokens")
        if isinstance(total_tokens, bool) or not isinstance(total_tokens, int):
            return
        record_histogram(
            "ctx.tool_usage.tokens_per_record",
            value=total_tokens,
            unit="tokens",
            attributes=attrs,
            source="ctx-runtime-lifecycle",
            session_id=session_id,
        )
    except Exception:  # noqa: BLE001 - metrics must not break lifecycle writes.
        pass


def _validate_entity_type(raw: str) -> str:
    value = raw.strip()
    if value not in _ENTITY_TYPES:
        raise ValueError("entity_type must be one of " + ", ".join(sorted(_ENTITY_TYPES)))
    return value


def _validate_slug(raw: str) -> str:
    value = raw.strip()
    validate_skill_name(value)
    return value


def _validate_recommendation_id(raw: str) -> str:
    value = raw.strip()
    if ":" not in value:
        raise ValueError("recommendation rejection must use a canonical type:slug id")
    raw_type, raw_slug = value.split(":", 1)
    return f"{_validate_entity_type(raw_type)}:{_validate_slug(raw_slug)}"


def _deduplicate_recommendation_ids(values: list[str]) -> list[str]:
    normalised: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _validate_recommendation_id(raw)
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        normalised.append(value)
    return normalised


def _validated_rejection_snapshot(raw: Any, *, session_id: str) -> list[str] | None:
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        _log_malformed_rejection_snapshot(session_id)
        return None
    try:
        return _deduplicate_recommendation_ids(raw)
    except ValueError:
        _log_malformed_rejection_snapshot(session_id)
        return None


def _log_malformed_rejection_snapshot(session_id: str) -> None:
    _logger.warning(
        "ctx runtime lifecycle: skipped malformed recommendation rejection snapshot for session %s",
        hash_identifier(session_id),
    )


def _validate_rejection_checkpoint_sessions(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError("checkpoint sessions must be an object")
    sessions: dict[str, list[str]] = {}
    for raw_session_id, raw_snapshot in raw.items():
        if not isinstance(raw_session_id, str):
            raise ValueError("checkpoint session id must be a string")
        session_id = _validate_session_id(raw_session_id)
        if not isinstance(raw_snapshot, list) or any(
            not isinstance(value, str) for value in raw_snapshot
        ):
            raise ValueError("checkpoint rejection snapshot is malformed")
        sessions[session_id] = _deduplicate_recommendation_ids(raw_snapshot)
    return sessions


def _scan_rejection_events(
    path: Path,
    *,
    offset: int,
    state: dict[str, list[str]],
) -> int:
    if not path.is_file():
        return 0
    consumed = offset
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                break
            consumed += len(line)
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict) or event.get("action") != "recommendation_rejections":
                continue
            raw_session_id = event.get("session_id")
            if not isinstance(raw_session_id, str):
                continue
            try:
                session_id = _validate_session_id(raw_session_id)
            except ValueError:
                continue
            snapshot = _validated_rejection_snapshot(
                event.get("rejected"),
                session_id=session_id,
            )
            if snapshot is None:
                continue
            if snapshot:
                state[session_id] = snapshot
            else:
                state.pop(session_id, None)
    return consumed


def _event_file_id(event_stat: os.stat_result | None) -> list[int]:
    if event_stat is None:
        return [0, 0]
    return [int(event_stat.st_dev), int(event_stat.st_ino)]


def _event_log_anchor(path: Path, offset: int) -> str:
    if offset <= 0 or not path.is_file():
        return hashlib.sha256(b"").hexdigest()
    start = max(0, offset - _REJECTION_CHECKPOINT_ANCHOR_BYTES)
    with path.open("rb") as handle:
        handle.seek(start)
        payload = handle.read(offset - start)
    return hashlib.sha256(payload).hexdigest()


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


def _token_usage_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not raw:
        raw = {
            "attribution": "unavailable",
            "attribution_reason": "host did not provide per-tool token usage",
        }
    attribution = _validate_choice(
        str(raw.get("attribution") or "unavailable"),
        _TOKEN_ATTRIBUTIONS,
        "token_usage.attribution",
    )
    input_tokens = _nonnegative_int(raw.get("input_tokens"), "token_usage.input_tokens")
    cached_input_tokens = _nonnegative_int(
        raw.get("cached_input_tokens"),
        "token_usage.cached_input_tokens",
    )
    cache_write_input_tokens = _nonnegative_int(
        raw.get("cache_write_input_tokens"),
        "token_usage.cache_write_input_tokens",
    )
    uncached_input_tokens = _nonnegative_int(
        raw.get("uncached_input_tokens"),
        "token_usage.uncached_input_tokens",
    )
    output_tokens = _nonnegative_int(raw.get("output_tokens"), "token_usage.output_tokens")
    total_tokens = _nonnegative_int(raw.get("total_tokens"), "token_usage.total_tokens")
    if input_tokens is not None and output_tokens is not None:
        expected_total_tokens = input_tokens + output_tokens
        if total_tokens is not None and total_tokens != expected_total_tokens:
            raise ValueError("token_usage.total_tokens must equal input_tokens + output_tokens")
        if total_tokens is None:
            total_tokens = expected_total_tokens
    cost_usd = _nonnegative_float(raw.get("cost_usd"), "token_usage.cost_usd")
    if input_tokens is not None:
        if cached_input_tokens is not None and cached_input_tokens > input_tokens:
            raise ValueError("token_usage.cached_input_tokens cannot exceed input_tokens")
        if cache_write_input_tokens is not None and cache_write_input_tokens > input_tokens:
            raise ValueError("token_usage.cache_write_input_tokens cannot exceed input_tokens")
        if uncached_input_tokens is not None and uncached_input_tokens > input_tokens:
            raise ValueError("token_usage.uncached_input_tokens cannot exceed input_tokens")
        if (
            cached_input_tokens is not None
            and uncached_input_tokens is not None
            and cached_input_tokens + uncached_input_tokens != input_tokens
        ):
            raise ValueError(
                "token_usage.cached_input_tokens + uncached_input_tokens must equal input_tokens"
            )
    tokens_reported_raw = raw.get("tokens_reported")
    if "tokens_reported" in raw:
        if not isinstance(tokens_reported_raw, bool):
            raise ValueError("token_usage.tokens_reported must be a boolean")
        tokens_reported = tokens_reported_raw
    else:
        tokens_reported = input_tokens is not None and output_tokens is not None
    if tokens_reported and (input_tokens is None or output_tokens is None):
        raise ValueError("token_usage.tokens_reported=true requires input_tokens and output_tokens")
    if attribution == "exact" and (
        input_tokens is None or output_tokens is None or tokens_reported is not True
    ):
        raise ValueError(
            "token_usage.attribution=exact requires input_tokens, output_tokens, "
            "and tokens_reported=true"
        )
    if attribution == "unavailable":
        input_tokens = None
        cached_input_tokens = None
        cache_write_input_tokens = None
        uncached_input_tokens = None
        output_tokens = None
        total_tokens = None
        tokens_reported = False
        cost_usd = None
    state = {
        "attribution": attribution,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "tokens_reported": tokens_reported,
        "cost_usd": cost_usd,
        "attribution_reason": str(raw.get("attribution_reason") or "").strip() or None,
        "model": str(raw.get("model") or "").strip() or None,
        "provider": str(raw.get("provider") or "").strip() or None,
    }
    return state


def normalize_historical_token_usage(raw: Any) -> dict[str, Any]:
    """Tolerantly normalize persisted usage for lifecycle and monitor readers."""

    usage = raw if isinstance(raw, dict) else {}
    metadata = _historical_token_usage_metadata(usage)
    input_tokens = _historical_int_value(usage.get("input_tokens"))
    cached_raw = (
        usage.get("cached_input_tokens")
        if "cached_input_tokens" in usage
        else usage.get("cache_read_input_tokens")
    )
    cached_input_tokens = _historical_int_value(cached_raw)
    cache_write_input_tokens = _historical_int_value(usage.get("cache_write_input_tokens"))
    uncached_input_tokens = _historical_int_value(usage.get("uncached_input_tokens"))
    cache_fields_valid = True
    if cached_raw is not None and cached_input_tokens is None:
        cache_fields_valid = False
    if usage.get("cache_write_input_tokens") is not None and cache_write_input_tokens is None:
        cache_fields_valid = False
    if usage.get("uncached_input_tokens") is not None and uncached_input_tokens is None:
        cache_fields_valid = False
    if input_tokens is None and any(
        value is not None
        for value in (cached_input_tokens, cache_write_input_tokens, uncached_input_tokens)
    ):
        cached_input_tokens = None
        cache_write_input_tokens = None
        uncached_input_tokens = None
        cache_fields_valid = False
    elif input_tokens is not None:
        if cached_input_tokens is not None and cached_input_tokens > input_tokens:
            cached_input_tokens = None
            uncached_input_tokens = None
            cache_fields_valid = False
        if cache_write_input_tokens is not None and cache_write_input_tokens > input_tokens:
            cache_write_input_tokens = None
            cache_fields_valid = False
        if uncached_input_tokens is not None and uncached_input_tokens > input_tokens:
            cached_input_tokens = None
            uncached_input_tokens = None
            cache_fields_valid = False
        if (
            cached_input_tokens is not None
            and uncached_input_tokens is not None
            and cached_input_tokens + uncached_input_tokens != input_tokens
        ):
            cached_input_tokens = None
            uncached_input_tokens = None
            cache_fields_valid = False
    if (
        "uncached_input_tokens" not in usage
        and input_tokens is not None
        and cached_input_tokens is not None
    ):
        uncached_input_tokens = input_tokens - cached_input_tokens

    output_tokens = _historical_int_value(usage.get("output_tokens"))
    total_tokens_raw = usage.get("total_tokens")
    total_tokens = _historical_int_value(total_tokens_raw)
    total_tokens_supplied = total_tokens_raw is not None and total_tokens_raw != ""
    expected_total_tokens: int | None = None
    if input_tokens is not None and output_tokens is not None:
        expected_total_tokens = input_tokens + output_tokens
    complete_token_counts = expected_total_tokens is not None
    total_tokens_contradictory = bool(
        complete_token_counts and total_tokens_supplied and total_tokens != expected_total_tokens
    )
    if complete_token_counts and not total_tokens_supplied:
        total_tokens = expected_total_tokens

    raw_attribution = usage.get("attribution")
    attribution_missing = raw_attribution is None or (
        isinstance(raw_attribution, str) and not raw_attribution.strip()
    )
    if attribution_missing and complete_token_counts and cache_fields_valid:
        attribution = "estimated"
        metadata["attribution_reason"] = _LEGACY_ATTRIBUTION_REASON
    else:
        attribution = (
            raw_attribution.strip().lower() if isinstance(raw_attribution, str) else "unavailable"
        )
        if attribution not in _TOKEN_ATTRIBUTIONS:
            attribution = "unavailable"
    if total_tokens_contradictory and attribution != "unavailable":
        total_tokens = expected_total_tokens
        if attribution == "exact":
            attribution = "estimated"
            metadata["attribution_reason"] = _INCONSISTENT_TOTAL_REASON

    reported_present = "tokens_reported" in usage
    reported_raw = usage.get("tokens_reported")
    reported_malformed = reported_present and not isinstance(reported_raw, bool)
    if not reported_present:
        tokens_reported = complete_token_counts
    elif isinstance(reported_raw, bool):
        tokens_reported = reported_raw
    else:
        tokens_reported = False
    if not complete_token_counts or not cache_fields_valid or total_tokens_contradictory:
        tokens_reported = False

    if attribution == "exact" and not tokens_reported:
        if complete_token_counts:
            attribution = "estimated"
            metadata["attribution_reason"] = (
                _MALFORMED_REPORTED_REASON if reported_malformed else _UNREPORTED_EXACT_REASON
            )
        else:
            attribution = "unavailable"
            metadata["attribution_reason"] = _INCOMPLETE_EXACT_REASON
    if attribution == "unavailable":
        return {
            "attribution": attribution,
            **{key: None for key in _TOKEN_USAGE_FIELDS},
            "tokens_reported": False,
            "cost_usd": None,
            **metadata,
        }
    return {
        "attribution": attribution,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "uncached_input_tokens": uncached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "tokens_reported": tokens_reported,
        "cost_usd": _historical_float_value(usage.get("cost_usd")),
        **metadata,
    }


def _historical_int_value(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _historical_float_value(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _historical_token_usage_metadata(usage: dict[str, Any]) -> dict[str, str | None]:
    metadata = {
        key: value.strip() if isinstance(value, str) and value.strip() else None
        for key in _TOKEN_USAGE_METADATA_FIELDS
        if (value := usage.get(key)) is not None
    }
    sanitized = sanitize_payload(metadata, config=_LIFECYCLE_SANITIZER_CONFIG)
    return {
        key: value if isinstance((value := sanitized.get(key)), str) else None
        for key in _TOKEN_USAGE_METADATA_FIELDS
    }


def _empty_token_usage_summary() -> dict[str, Any]:
    return {
        "records": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "tokens_reported": True,
        "cost_usd": 0.0,
        "by_attribution": {key: 0 for key in sorted(_TOKEN_ATTRIBUTIONS)},
    }


def _merge_token_usage(summary: dict[str, Any], usage: dict[str, Any]) -> None:
    summary["records"] = int(summary.get("records") or 0) + 1
    attribution = str(usage.get("attribution") or "unavailable")
    by_attribution = summary.setdefault(
        "by_attribution",
        {key: 0 for key in sorted(_TOKEN_ATTRIBUTIONS)},
    )
    by_attribution[attribution] = int(by_attribution.get(attribution) or 0) + 1
    for key in _TOKEN_USAGE_FIELDS:
        value = usage.get(key)
        current = summary.get(key)
        if current is None or isinstance(value, bool) or not isinstance(value, int):
            summary[key] = None
        else:
            summary[key] = int(current) + value
    summary["tokens_reported"] = bool(
        summary.get("tokens_reported") and usage.get("tokens_reported") is True
    )
    cost = usage.get("cost_usd")
    current_cost = summary.get("cost_usd")
    if current_cost is None or isinstance(cost, bool) or not isinstance(cost, (int, float)):
        summary["cost_usd"] = None
    else:
        summary["cost_usd"] = round(float(current_cost) + float(cost), 8)


def _nonnegative_int(raw: Any, field: str) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _nonnegative_float(raw: Any, field: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative number") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return value


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
            "summary": ("No SkillSpector scan proof was provided by the host for this skill load."),
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
