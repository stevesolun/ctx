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
import sqlite3
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
from ctx.utils._fs_utils import reject_symlink_path
from ctx.utils._secret_scan import redact_secret_text


_logger = logging.getLogger(__name__)
_REJECTION_INDEX_VERSION = 3
_REJECTION_HEAD_SEED = hashlib.sha256(b"").hexdigest()
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


class _InvalidRejectionIndex(RuntimeError):
    """The derived rejection index cannot be trusted or upgraded in place."""


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
        if not path.exists():
            return []
        if not path.is_file():
            raise ValueError(f"runtime lifecycle events must be a regular file: {path}")
        _prepare_private_lifecycle_lock(path)
        with file_lock(path):
            _repair_jsonl_tail(path)
            connection = self._open_rejection_index_unlocked()
            try:
                return _rejection_index_lookup(
                    connection,
                    events_path=path,
                    session_id=session_id,
                )
            finally:
                connection.close()

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
        _prepare_private_lifecycle_lock(path)
        recorded_event: dict[str, Any] | None = None
        normalised = supplied
        with file_lock(path):
            _repair_jsonl_tail(path)
            connection = self._open_rejection_index_unlocked()
            try:
                stored = _rejection_index_lookup(
                    connection,
                    events_path=path,
                    session_id=session_id,
                )
                normalised = (
                    _deduplicate_recommendation_ids(stored + supplied) if merge else supplied
                )
                if stored != normalised:
                    recorded = self._record(
                        _lock_held=True,
                        _emit_telemetry=False,
                        _index_connection=connection,
                        action="recommendation_rejections",
                        session_id=session_id,
                        rejected=normalised,
                    )
                    recorded_event = recorded["event"]
            finally:
                connection.close()
        if recorded_event is not None:
            _record_runtime_lifecycle_telemetry(recorded_event)
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
                if action == "load_applied" and current["load_status"] != "applied":
                    current["load_status"] = "applied"
                    current["applied_at"] = event.get("created_at")
                    current["applied_at_epoch"] = float(event.get("created_at_epoch") or 0)
                    current["dev_event_epoch"] = latest_dev_event_epoch
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
            and (min_age == 0 or now - float(entry.get("applied_at_epoch") or 0) >= min_age)
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

    def _record(
        self,
        *,
        _lock_held: bool = False,
        _emit_telemetry: bool = True,
        _index_connection: sqlite3.Connection | None = None,
        **event: Any,
    ) -> dict[str, Any]:
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

        if _lock_held:
            self._append_lifecycle_event_unlocked(
                event,
                connection=_index_connection,
            )
        else:
            reject_symlink_path(path)
            _prepare_private_lifecycle_lock(path)
            with file_lock(path):
                self._append_lifecycle_event_unlocked(event)
        if _emit_telemetry:
            _record_runtime_lifecycle_telemetry(event)
        return {"ok": True, "event": event, "recorded": True}

    def _append_lifecycle_event_unlocked(
        self,
        event: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        path = self.events_path
        owns_connection = connection is None
        if owns_connection:
            _repair_jsonl_tail(path)
            connection = self._open_rejection_index_unlocked(verify_content=False)
        assert connection is not None
        try:
            payload = _append_jsonl_event(path, event)
            try:
                _update_rejection_index_after_append(
                    connection,
                    events_path=path,
                    event=event,
                    payload=payload,
                )
            except Exception:
                _logger.warning(
                    "ctx runtime lifecycle: discarded stale rejection index after "
                    "canonical event append",
                    exc_info=True,
                )
                connection.close()
                try:
                    _discard_rejection_index(self.recommendation_index_path)
                except Exception:
                    _logger.warning(
                        "ctx runtime lifecycle: could not discard stale rejection index; "
                        "the next lookup will rebuild it",
                        exc_info=True,
                    )
        finally:
            if owns_connection:
                connection.close()

    def _events_for_session(self, session_id: str) -> list[dict[str, Any]]:
        path = self.events_path
        reject_symlink_path(path)
        if not path.is_file():
            return []
        _prepare_private_lifecycle_lock(path)
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

    def _open_rejection_index_unlocked(
        self,
        *,
        verify_content: bool = True,
    ) -> sqlite3.Connection:
        events_path = self.events_path
        return _open_rejection_index(
            events_path=events_path,
            index_path=self.recommendation_index_path,
            legacy_checkpoint_path=self._legacy_recommendation_checkpoint_path,
            verify_content=verify_content,
        )

    @property
    def events_path(self) -> Path:
        root = self.root
        if root is None:
            root = Path(os.environ.get("CTX_RUNTIME_LIFECYCLE_DIR", "~/.ctx/runtime")).expanduser()
        return root / "events.jsonl"

    @property
    def recommendation_index_path(self) -> Path:
        return self.events_path.with_name("recommendation-rejections.sqlite3")

    @property
    def recommendation_checkpoint_path(self) -> Path:
        """Deprecated compatibility alias for the derived SQLite index path."""
        return self.recommendation_index_path

    @property
    def _legacy_recommendation_checkpoint_path(self) -> Path:
        return self.events_path.with_name("recommendation-rejections.json")


def _append_jsonl_event(path: Path, event: dict[str, Any]) -> bytes:
    reject_symlink_path(path)
    ensure_private_event_file(path)
    payload = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return payload


def _prepare_private_lifecycle_lock(path: Path) -> None:
    lock_path = path.with_suffix(path.suffix + ".lock")
    reject_symlink_path(lock_path)
    ensure_private_event_file(lock_path)


def _repair_jsonl_tail(path: Path) -> None:
    """Remove a crash-partial final record before the next durable append."""
    reject_symlink_path(path)
    if not path.is_file():
        return
    with path.open("r+b") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return
        handle.seek(-1, os.SEEK_END)
        if handle.read(1) == b"\n":
            return

        cursor = size
        truncate_at = 0
        while cursor > 0:
            chunk_size = min(cursor, 64 * 1024)
            cursor -= chunk_size
            handle.seek(cursor)
            chunk = handle.read(chunk_size)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                truncate_at = cursor + newline + 1
                break
        handle.truncate(truncate_at)
        handle.flush()
        os.fsync(handle.fileno())


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


def _open_rejection_index(
    *,
    events_path: Path,
    index_path: Path,
    legacy_checkpoint_path: Path,
    verify_content: bool,
) -> sqlite3.Connection:
    reject_symlink_path(events_path)
    _reject_rejection_index_symlinks(index_path)
    reject_symlink_path(legacy_checkpoint_path)

    for attempt in range(2):
        connection: sqlite3.Connection | None = None
        created = not index_path.exists()
        try:
            _ensure_private_sqlite_file(index_path)
            connection = sqlite3.connect(
                str(index_path),
                timeout=0,
                isolation_level=None,
            )
            _configure_rejection_index(connection)
            if created:
                _create_rejection_index_schema(connection)
            elif not _rejection_index_schema_current(connection):
                raise _InvalidRejectionIndex("unsupported rejection index schema")

            metadata = _read_rejection_index_metadata(connection)
            if metadata is None or not _rejection_index_matches_events(
                metadata,
                events_path,
                verify_content=verify_content,
            ):
                _rebuild_rejection_index(connection, events_path=events_path)
            _remove_legacy_rejection_checkpoint(legacy_checkpoint_path)
            _tighten_rejection_index_files(index_path)
            return connection
        except (sqlite3.DatabaseError, _InvalidRejectionIndex):
            if connection is not None:
                connection.close()
            if attempt:
                raise
            _logger.warning("ctx runtime lifecycle: rebuilding malformed rejection index")
            _discard_rejection_index(index_path)
    raise AssertionError("rejection index recovery loop exhausted")


def _ensure_private_sqlite_file(path: Path) -> None:
    reject_symlink_path(path)
    if path.exists() and not path.is_file():
        raise ValueError(f"rejection index must be a regular file: {path}")
    ensure_private_event_file(path)


def _reject_rejection_index_symlinks(path: Path) -> None:
    for candidate in _rejection_index_files(path):
        reject_symlink_path(candidate)


def _rejection_index_files(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(f"{path}-journal"),
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
    )


def _tighten_rejection_index_files(path: Path) -> None:
    for candidate in _rejection_index_files(path):
        if not candidate.exists():
            continue
        reject_symlink_path(candidate)
        try:
            os.chmod(candidate, 0o600)
        except OSError:
            pass


def _discard_rejection_index(path: Path) -> None:
    for candidate in reversed(_rejection_index_files(path)):
        reject_symlink_path(candidate)
        if not candidate.exists():
            continue
        if not candidate.is_file():
            raise ValueError(f"rejection index state must be a regular file: {candidate}")
        candidate.unlink()


def _remove_legacy_rejection_checkpoint(path: Path) -> None:
    reject_symlink_path(path)
    if not path.exists():
        return
    if not path.is_file():
        raise ValueError(f"legacy rejection checkpoint must be a regular file: {path}")
    path.unlink()


def _configure_rejection_index(connection: sqlite3.Connection) -> None:
    journal_mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
    if not journal_mode or str(journal_mode[0]).lower() != "delete":
        raise _InvalidRejectionIndex("rejection index must use DELETE journal mode")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA trusted_schema=OFF")


def _create_rejection_index_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE metadata (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            version INTEGER NOT NULL,
            event_dev INTEGER NOT NULL,
            event_ino INTEGER NOT NULL,
            event_size INTEGER NOT NULL,
            event_mtime_ns INTEGER NOT NULL,
            event_ctime_ns INTEGER NOT NULL,
            event_head TEXT NOT NULL,
            checksum TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            rejected_json TEXT NOT NULL,
            checksum TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )
    connection.execute(f"PRAGMA user_version={_REJECTION_INDEX_VERSION}")


def _rejection_index_schema_current(connection: sqlite3.Connection) -> bool:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    if not version_row or int(version_row[0]) != _REJECTION_INDEX_VERSION:
        return False
    metadata_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(metadata)").fetchall()
    }
    session_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
    }
    return metadata_columns == {
        "singleton",
        "version",
        "event_dev",
        "event_ino",
        "event_size",
        "event_mtime_ns",
        "event_ctime_ns",
        "event_head",
        "checksum",
    } and session_columns == {"session_id", "rejected_json", "checksum"}


def _read_rejection_index_metadata(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT version, event_dev, event_ino, event_size, event_mtime_ns,
               event_ctime_ns, event_head, checksum
        FROM metadata
        WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        return None
    metadata = {
        "version": row[0],
        "event_dev": row[1],
        "event_ino": row[2],
        "event_size": row[3],
        "event_mtime_ns": row[4],
        "event_ctime_ns": row[5],
        "event_head": row[6],
        "checksum": row[7],
    }
    if (
        metadata["version"] != _REJECTION_INDEX_VERSION
        or any(
            isinstance(metadata[field], bool) or not isinstance(metadata[field], int)
            for field in (
                "event_dev",
                "event_ino",
                "event_size",
                "event_mtime_ns",
                "event_ctime_ns",
            )
        )
        or not _valid_sha256(metadata["event_head"])
        or metadata["checksum"] != _rejection_metadata_checksum(metadata)
    ):
        raise _InvalidRejectionIndex("invalid rejection index metadata")
    return metadata


def _rejection_index_matches_events(
    metadata: dict[str, Any],
    path: Path,
    *,
    verify_content: bool,
) -> bool:
    event_stat = path.stat() if path.is_file() else None
    expected = _event_stat_metadata(path, event_stat=event_stat)
    stat_matches = all(
        metadata[field] == expected[field]
        for field in (
            "event_dev",
            "event_ino",
            "event_size",
            "event_mtime_ns",
            "event_ctime_ns",
        )
    )
    return stat_matches and (
        not verify_content or metadata["event_head"] == _event_stream_head(path)
    )


def _rebuild_rejection_index(
    connection: sqlite3.Connection,
    *,
    events_path: Path,
) -> None:
    state, event_head = _scan_rejection_events(events_path)
    metadata = _event_stat_metadata(
        events_path,
        event_stat=events_path.stat() if events_path.is_file() else None,
        event_head=event_head,
    )
    rows = [_rejection_session_row(session_id, rejected) for session_id, rejected in state.items()]
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM sessions")
        connection.execute("DELETE FROM metadata")
        connection.executemany(
            "INSERT INTO sessions(session_id, rejected_json, checksum) VALUES (?, ?, ?)",
            rows,
        )
        _write_rejection_index_metadata(connection, metadata)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _rejection_index_lookup(
    connection: sqlite3.Connection,
    *,
    events_path: Path,
    session_id: str,
) -> list[str]:
    row = connection.execute(
        "SELECT rejected_json, checksum FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return []
    try:
        rejected = _decode_rejection_session_row(session_id, row)
    except (TypeError, ValueError, json.JSONDecodeError):
        _logger.warning("ctx runtime lifecycle: rebuilding malformed rejection index")
        _rebuild_rejection_index(connection, events_path=events_path)
        row = connection.execute(
            "SELECT rejected_json, checksum FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return []
        rejected = _decode_rejection_session_row(session_id, row)
    return rejected


def _update_rejection_index_after_append(
    connection: sqlite3.Connection,
    *,
    events_path: Path,
    event: dict[str, Any],
    payload: bytes,
) -> None:
    metadata = _read_rejection_index_metadata(connection)
    if metadata is None:
        raise _InvalidRejectionIndex("rejection index metadata is missing")
    event_stat = events_path.stat()
    if int(event_stat.st_size) != int(metadata["event_size"]) + len(payload):
        raise _InvalidRejectionIndex("event log changed during append")
    event_head = _advance_event_head(str(metadata["event_head"]), payload)
    updated_metadata = _event_stat_metadata(
        events_path,
        event_stat=event_stat,
        event_head=event_head,
    )

    action = event.get("action")
    session_id: str | None = None
    rejected: list[str] | None = None
    if action == "recommendation_rejections":
        session_id = _validate_session_id(str(event.get("session_id") or ""))
        rejected = _validated_rejection_snapshot(
            event.get("rejected"),
            session_id=session_id,
        )
        if rejected is None:
            raise ValueError("invalid recommendation rejection event")

    try:
        connection.execute("BEGIN IMMEDIATE")
        if session_id is not None and rejected is not None:
            if rejected:
                connection.execute(
                    """
                    INSERT INTO sessions(session_id, rejected_json, checksum)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        rejected_json = excluded.rejected_json,
                        checksum = excluded.checksum
                    """,
                    _rejection_session_row(session_id, rejected),
                )
            else:
                connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
        connection.execute("DELETE FROM metadata")
        _write_rejection_index_metadata(connection, updated_metadata)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _write_rejection_index_metadata(
    connection: sqlite3.Connection,
    metadata: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO metadata(
            singleton, version, event_dev, event_ino, event_size,
            event_mtime_ns, event_ctime_ns, event_head, checksum
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            metadata["version"],
            metadata["event_dev"],
            metadata["event_ino"],
            metadata["event_size"],
            metadata["event_mtime_ns"],
            metadata["event_ctime_ns"],
            metadata["event_head"],
            metadata["checksum"],
        ),
    )


def _scan_rejection_events(path: Path) -> tuple[dict[str, list[str]], str]:
    state: dict[str, list[str]] = {}
    event_head = _REJECTION_HEAD_SEED
    if not path.is_file():
        return state, event_head
    with path.open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                break
            event_head = _advance_event_head(event_head, line)
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
    return state, event_head


def _rejection_session_row(
    session_id: str,
    rejected: list[str],
) -> tuple[str, str, str]:
    rejected_json = json.dumps(rejected, separators=(",", ":"))
    checksum = hashlib.sha256(f"{session_id}\0{rejected_json}".encode("utf-8")).hexdigest()
    return session_id, rejected_json, checksum


def _decode_rejection_session_row(
    session_id: str,
    row: tuple[Any, ...],
) -> list[str]:
    rejected_json, checksum = row
    if not isinstance(rejected_json, str) or not isinstance(checksum, str):
        raise ValueError("invalid rejection index session row")
    expected = hashlib.sha256(f"{session_id}\0{rejected_json}".encode("utf-8")).hexdigest()
    if checksum != expected:
        raise ValueError("rejection index session checksum changed")
    raw = json.loads(rejected_json)
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise ValueError("invalid rejection index session snapshot")
    return _deduplicate_recommendation_ids(raw)


def _event_stat_metadata(
    path: Path,
    *,
    event_stat: os.stat_result | None,
    event_head: str = _REJECTION_HEAD_SEED,
) -> dict[str, Any]:
    event_dev, event_ino = _event_file_id(event_stat)
    event_size = int(event_stat.st_size) if event_stat is not None else 0
    metadata: dict[str, Any] = {
        "version": _REJECTION_INDEX_VERSION,
        "event_dev": event_dev,
        "event_ino": event_ino,
        "event_size": event_size,
        "event_mtime_ns": _stat_time_ns(event_stat, "st_mtime_ns"),
        "event_ctime_ns": _stat_time_ns(event_stat, "st_ctime_ns"),
        "event_head": event_head,
    }
    metadata["checksum"] = _rejection_metadata_checksum(metadata)
    return metadata


def _event_file_id(event_stat: os.stat_result | None) -> tuple[int, int]:
    if event_stat is None:
        return 0, 0
    return (
        _stable_sqlite_integer(event_stat.st_dev),
        _stable_sqlite_integer(event_stat.st_ino),
    )


def _stable_sqlite_integer(value: int) -> int:
    """Fit platform file identifiers into SQLite's signed 64-bit integer."""
    normalized = int(value)
    if -(1 << 63) <= normalized < (1 << 63):
        return normalized
    digest = hashlib.sha256(str(normalized).encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _stat_time_ns(event_stat: os.stat_result | None, field: str) -> int:
    if event_stat is None:
        return 0
    value = getattr(event_stat, field, None)
    if isinstance(value, int):
        return value
    seconds = getattr(event_stat, field.removesuffix("_ns"))
    return int(float(seconds) * 1_000_000_000)


def _rejection_metadata_checksum(payload: dict[str, Any]) -> str:
    canonical = {key: value for key, value in payload.items() if key != "checksum"}
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _advance_event_head(previous: str, payload: bytes) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + payload).hexdigest()


def _valid_sha256(raw: Any) -> bool:
    return (
        isinstance(raw, str)
        and len(raw) == 64
        and all(character in "0123456789abcdef" for character in raw)
    )


def _event_stream_head(path: Path) -> str:
    event_head = _REJECTION_HEAD_SEED
    if not path.is_file():
        return event_head
    with path.open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                break
            event_head = _advance_event_head(event_head, line)
    return event_head


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
            "recommended_command": f"python -m ctx.adapters.claude_code.install.skill_install {slug} --security-scan-required",
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
