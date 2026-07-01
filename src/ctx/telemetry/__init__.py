"""Enterprise-ready local telemetry primitives for ctx.

This module is the telemetry spine: a stable event envelope, a local
append-only JSONL spool, and optional best-effort exporters. Network export is
disabled by default and must be explicitly enabled in config.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import sys
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ctx.utils._file_lock import file_lock
from ctx.utils._secret_scan import redact_secret_text, secret_key_like

SCHEMA_VERSION = "ctx.telemetry.v1"
METRIC_SCHEMA_VERSION = "ctx.telemetry.metrics.v1"
EXPORT_STATUS_SCHEMA_VERSION = "ctx.telemetry.export_status.v1"
RETENTION_STATUS_SCHEMA_VERSION = "ctx.telemetry.retention_status.v1"
DEFAULT_TELEMETRY_PATH = Path(os.path.expanduser("~/.ctx/telemetry/events.jsonl"))
DEFAULT_EXPORT_PATH = Path(os.path.expanduser("~/.ctx/telemetry/exported-events.jsonl"))
DEFAULT_METRICS_PATH = Path(os.path.expanduser("~/.ctx/telemetry/metrics.jsonl"))
DEFAULT_METRICS_EXPORT_PATH = Path(os.path.expanduser("~/.ctx/telemetry/exported-metrics.jsonl"))
DEFAULT_OTLP_LOGS_ENDPOINT = "http://localhost:4318/v1/logs"
DEFAULT_OTLP_METRICS_ENDPOINT = "http://localhost:4318/v1/metrics"
DEFAULT_PRIVACY_MODE = "local_redacted"
DEFAULT_HASH_SALT_ENV = "CTX_TELEMETRY_HASH_SALT"
DEFAULT_HASH_SALT_PATH = Path(os.path.expanduser("~/.ctx/telemetry/hash-salt"))
_LOCAL_OTLP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DISABLED_MODES = frozenset({"disabled", "off", "none"})
ALLOWED_MODES = frozenset({DEFAULT_PRIVACY_MODE, *DISABLED_MODES})

_MAX_PAYLOAD_KEYS = 40
_MAX_PAYLOAD_VALUE_LEN = 1024
_MAX_PAYLOAD_DEPTH = 4
_OTEL_AGGREGATION_TEMPORALITY_DELTA = 1
_METRIC_INSTRUMENTS = frozenset({"counter", "histogram"})
_DEFAULT_HISTOGRAM_BOUNDS = (
    1.0,
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
    2500.0,
    5000.0,
    10000.0,
)
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_RAW_VALUE_KEYS = frozenset(
    {
        "command",
        "command_output",
        "cwd",
        "goal",
        "input",
        "model_response",
        "output",
        "path",
        "paths",
        "prompt",
        "query",
        "raw_input",
        "raw_prompt",
        "repo",
        "response",
        "stderr",
        "stdout",
        "task",
        "tool_args",
        "tool_input",
        "tool_output",
    }
)
_SCALAR_TYPES = (str, int, float, bool, type(None))


def _raw_value_key_like(normalized_key: str) -> bool:
    return normalized_key in _RAW_VALUE_KEYS or normalized_key.endswith(("_path", "_paths"))


@dataclass(frozen=True)
class TelemetryEvent:
    """One canonical ctx telemetry event."""

    schema_version: str
    event_id: str
    ts: str
    event_name: str
    source: str
    outcome: str = "ok"
    session_id: str | None = None
    session_hash: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    transport: str | None = None
    actor: str | None = None
    duration_ms: float | None = None
    error_kind: str | None = None
    privacy_mode: str = DEFAULT_PRIVACY_MODE
    repo_hash: str | None = None
    cwd_hash: str | None = None
    graph_export_id: str | None = None
    wiki_export_id: str | None = None
    ctx_version: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported telemetry schema: {self.schema_version!r}")
        if not self.event_id:
            raise ValueError("event_id must be non-empty")
        if not self.event_name or any(ch.isspace() for ch in self.event_name):
            raise ValueError("event_name must be a non-empty token")
        if not self.source:
            raise ValueError("source must be non-empty")
        if not self.outcome:
            raise ValueError("outcome must be non-empty")
        if self.privacy_mode not in ALLOWED_MODES:
            allowed = ", ".join(sorted(ALLOWED_MODES))
            raise ValueError(f"privacy_mode must be one of: {allowed}")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")
        if self.session_hash is not None and not self.session_hash.startswith("sha256:"):
            raise ValueError("session_hash must be a sha256 identifier")
        try:
            datetime.fromisoformat(self.ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"ts must be ISO-8601: {self.ts!r}") from exc
        _sanitize_payload(dict(self.payload), privacy_mode=self.privacy_mode)


@dataclass(frozen=True)
class TelemetryMetric:
    """One local metric point that can export as an OpenTelemetry metric."""

    schema_version: str
    metric_id: str
    ts: str
    name: str
    instrument: str
    value: float
    unit: str = "1"
    source: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    session_hash: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    privacy_mode: str = DEFAULT_PRIVACY_MODE
    ctx_version: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != METRIC_SCHEMA_VERSION:
            raise ValueError(f"unsupported metric schema: {self.schema_version!r}")
        if not self.metric_id:
            raise ValueError("metric_id must be non-empty")
        if not self.name or any(ch.isspace() for ch in self.name):
            raise ValueError("metric name must be a non-empty token")
        if self.instrument not in _METRIC_INSTRUMENTS:
            allowed = ", ".join(sorted(_METRIC_INSTRUMENTS))
            raise ValueError(f"metric instrument must be one of: {allowed}")
        if (
            not isinstance(self.value, (int, float))
            or not math.isfinite(float(self.value))
            or self.value < 0
        ):
            raise ValueError("metric value must be a finite non-negative number")
        if not self.unit:
            raise ValueError("metric unit must be non-empty")
        if self.privacy_mode not in ALLOWED_MODES:
            allowed = ", ".join(sorted(ALLOWED_MODES))
            raise ValueError(f"privacy_mode must be one of: {allowed}")
        if self.session_hash is not None and not self.session_hash.startswith("sha256:"):
            raise ValueError("session_hash must be a sha256 identifier")
        try:
            datetime.fromisoformat(self.ts.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"ts must be ISO-8601: {self.ts!r}") from exc
        _sanitize_payload(dict(self.attributes), privacy_mode=self.privacy_mode)


@dataclass(frozen=True)
class TelemetrySpan:
    """Active trace/span correlation context for nested ctx operations."""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None


@dataclass(frozen=True)
class ExportResult:
    """Summary of one telemetry export attempt."""

    attempted: int
    exported: int
    failed: int
    sink: str
    status: str = "noop"
    error_kind: str | None = None
    checkpoint_path: str | None = None
    last_event_id: str | None = None
    malformed_records: int = 0
    malformed_pending_records: int = 0
    malformed_first_line: int | None = None
    malformed_last_line: int | None = None
    status_path: str | None = None
    checkpoint_before_event_id: str | None = None
    checkpoint_after_event_id: str | None = None
    checkpoint_advanced: bool = False
    checkpoint_found: bool = False
    destination_hash: str | None = None
    last_success_at: str | None = None
    last_success_event_id: str | None = None


@dataclass(frozen=True)
class MetricExportResult:
    """Summary of one metric export attempt."""

    attempted: int
    exported: int
    failed: int
    sink: str
    status: str = "noop"
    error_kind: str | None = None
    checkpoint_path: str | None = None
    last_metric_id: str | None = None
    malformed_records: int = 0
    malformed_pending_records: int = 0
    malformed_first_line: int | None = None
    malformed_last_line: int | None = None
    status_path: str | None = None
    checkpoint_before_metric_id: str | None = None
    checkpoint_after_metric_id: str | None = None
    checkpoint_advanced: bool = False
    checkpoint_found: bool = False
    destination_hash: str | None = None
    last_success_at: str | None = None
    last_success_metric_id: str | None = None


@dataclass(frozen=True)
class TelemetryRetentionResult:
    """Summary of one telemetry retention plan or enforcement run."""

    signal: str
    path: str
    status: str
    dry_run: bool
    scanned_records: int
    retained_records: int
    dropped_records: int
    malformed_records: int
    malformed_dropped_records: int
    max_age_days: int | None = None
    max_records: int | None = None
    min_keep_records: int = 0
    cutoff_ts: str | None = None
    status_path: str | None = None


@dataclass(frozen=True)
class _SpoolRead:
    events: list[TelemetryEvent]
    event_line_numbers: dict[str, int]
    malformed_line_numbers: list[int]


@dataclass(frozen=True)
class _MetricSpoolRead:
    metrics: list[TelemetryMetric]
    metric_line_numbers: dict[str, int]
    malformed_line_numbers: list[int]


@dataclass(frozen=True)
class _PendingExport:
    events: list[TelemetryEvent]
    checkpoint_path: Path
    checkpoint_before_event_id: str | None
    checkpoint_found: bool
    malformed_total_records: int
    malformed_pending_records: int
    malformed_first_line: int | None
    malformed_last_line: int | None


@dataclass(frozen=True)
class _PendingMetricExport:
    metrics: list[TelemetryMetric]
    checkpoint_path: Path
    checkpoint_before_metric_id: str | None
    checkpoint_found: bool
    malformed_total_records: int
    malformed_pending_records: int
    malformed_first_line: int | None
    malformed_last_line: int | None


@dataclass(frozen=True)
class _RetentionRecord:
    index: int
    raw_line: str
    ts: datetime


_CURRENT_SPAN: ContextVar[TelemetrySpan | None] = ContextVar(
    "ctx_telemetry_span",
    default=None,
)


def hash_identifier(value: str, *, salt: str | bytes | None = None) -> str:
    """Return a stable non-reversible identifier for paths, repos, or queries."""

    raw = value.encode("utf-8")
    resolved_salt = salt if salt is not None else _resolve_hash_salt()
    if resolved_salt is None:
        digest = hashlib.sha256(b"ctx.telemetry.v1\x00" + raw).hexdigest()
    else:
        key = resolved_salt.encode("utf-8") if isinstance(resolved_salt, str) else resolved_salt
        digest = hmac.new(key, raw, hashlib.sha256).hexdigest()
    return f"sha256:{digest}"


def telemetry_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Return whether the effective telemetry config permits event capture."""

    try:
        settings = _settings(config)
    except ValueError:
        return False
    privacy_mode = str(settings["mode"])
    return bool(settings["enabled"]) and privacy_mode not in DISABLED_MODES


def sanitize_payload(
    payload: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Sanitize a payload using the effective telemetry privacy settings."""

    settings = _settings(config)
    return _sanitize_payload(
        dict(payload),
        privacy_mode=str(settings["mode"]),
        hash_salt=settings["hash_salt"],
        max_keys=int(settings["max_payload_keys"]),
        max_value_len=int(settings["max_payload_value_chars"]),
    )


def ensure_private_event_file(path: Path) -> None:
    """Create or tighten a local event file to owner read/write only."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)
    try:
        os.chmod(path.parent, _PRIVATE_DIR_MODE)
    except OSError:
        pass
    if path.exists():
        try:
            os.chmod(path, _PRIVATE_FILE_MODE)
        except OSError:
            pass
        return
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, _PRIVATE_FILE_MODE)
    os.close(fd)
    try:
        os.chmod(path, _PRIVATE_FILE_MODE)
    except OSError:
        pass


def current_telemetry_span() -> TelemetrySpan | None:
    """Return the active telemetry span for this context, if any."""

    return _CURRENT_SPAN.get()


@contextmanager
def telemetry_span(
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> Iterator[TelemetrySpan]:
    """Start a nested telemetry span using OpenTelemetry-compatible IDs."""

    parent = _CURRENT_SPAN.get()
    span = TelemetrySpan(
        trace_id=trace_id or (parent.trace_id if parent is not None else uuid.uuid4().hex),
        span_id=span_id or secrets.token_hex(8),
        parent_span_id=parent.span_id if parent is not None else None,
    )
    token = _CURRENT_SPAN.set(span)
    try:
        yield span
    finally:
        _CURRENT_SPAN.reset(token)


def record_event(
    event_name: str,
    *,
    source: str,
    payload: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    parent_span_id: str | None = None,
    transport: str | None = None,
    actor: str | None = None,
    outcome: str = "ok",
    duration_ms: float | None = None,
    error_kind: str | None = None,
    repo: str | None = None,
    cwd: str | None = None,
    graph_export_id: str | None = None,
    wiki_export_id: str | None = None,
    ctx_version: str | None = None,
    path: Path | None = None,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> TelemetryEvent | None:
    """Append one telemetry event unless telemetry is disabled.

    Returns the event that was written, or ``None`` when the effective config
    disables telemetry. I/O errors are deliberately best-effort so telemetry
    cannot break product workflows.
    """

    try:
        settings = _settings(config)
    except ValueError as exc:
        print(f"ctx telemetry: invalid config ({exc})", file=sys.stderr)
        return None
    privacy_mode = str(settings["mode"])
    if not settings["enabled"] or privacy_mode in DISABLED_MODES:
        return None

    hash_salt = settings["hash_salt"]
    sanitized_payload = _sanitize_payload(
        dict(payload or {}),
        privacy_mode=privacy_mode,
        hash_salt=hash_salt,
        max_keys=int(settings["max_payload_keys"]),
        max_value_len=int(settings["max_payload_value_chars"]),
    )
    active_span = _CURRENT_SPAN.get()
    if active_span is not None and trace_id is None and span_id is None:
        resolved_trace_id = active_span.trace_id
        resolved_span_id = active_span.span_id
        resolved_parent_span_id = (
            parent_span_id if parent_span_id is not None else active_span.parent_span_id
        )
    else:
        resolved_trace_id = trace_id or uuid.uuid4().hex
        resolved_span_id = span_id or secrets.token_hex(8)
        resolved_parent_span_id = parent_span_id
    event = TelemetryEvent(
        schema_version=SCHEMA_VERSION,
        event_id=uuid.uuid4().hex,
        ts=_now_iso(),
        event_name=event_name,
        source=source,
        outcome=outcome,
        session_id=session_id,
        session_hash=hash_identifier(session_id, salt=hash_salt) if session_id else None,
        trace_id=resolved_trace_id,
        span_id=resolved_span_id,
        parent_span_id=resolved_parent_span_id,
        transport=transport,
        actor=actor,
        duration_ms=duration_ms,
        error_kind=error_kind,
        privacy_mode=privacy_mode,
        repo_hash=hash_identifier(repo, salt=hash_salt) if repo else None,
        cwd_hash=hash_identifier(cwd, salt=hash_salt) if cwd else None,
        graph_export_id=graph_export_id,
        wiki_export_id=wiki_export_id,
        ctx_version=ctx_version if ctx_version is not None else _ctx_version(),
        payload=sanitized_payload,
    )

    target = _resolve_path(path or Path(str(settings["path"])), trusted_root=trusted_root)
    try:
        ensure_private_event_file(target)
        line = json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":")) + "\n"
        with file_lock(target):
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:  # noqa: BLE001 - telemetry must not break callers.
        print(f"ctx telemetry: failed to write event ({type(exc).__name__})", file=sys.stderr)
        return None
    if settings["export_enabled"]:
        started_at = _now_iso()
        result = _export_events([event], settings=settings, trusted_root=trusted_root)
        checkpoint_path = _export_checkpoint_path(
            settings,
            source_path=target,
            trusted_root=trusted_root,
        )
        checkpoint_before_event_id = _read_export_checkpoint(
            checkpoint_path,
            settings=settings,
            source_path=target,
        )
        checkpoint_after_event_id = checkpoint_before_event_id
        checkpoint_advanced = False
        last_success_at = None
        last_success_event_id = None
        if result.failed == 0 and result.exported:
            _write_export_checkpoint(
                settings,
                source_path=target,
                event=event,
                trusted_root=trusted_root,
            )
            checkpoint_after_event_id = event.event_id
            checkpoint_advanced = checkpoint_after_event_id != checkpoint_before_event_id
            last_success_at = _now_iso()
            last_success_event_id = event.event_id
        status_path = _export_status_path(
            settings,
            source_path=target,
            trusted_root=trusted_root,
        )
        status = "failed" if result.failed else ("ok" if result.exported else "noop")
        final_result = ExportResult(
            attempted=result.attempted,
            exported=result.exported,
            failed=result.failed,
            sink=result.sink,
            status=status,
            error_kind=result.error_kind,
            checkpoint_path=str(checkpoint_path),
            last_event_id=checkpoint_after_event_id,
            status_path=str(status_path),
            checkpoint_before_event_id=checkpoint_before_event_id,
            checkpoint_after_event_id=checkpoint_after_event_id,
            checkpoint_advanced=checkpoint_advanced,
            checkpoint_found=checkpoint_before_event_id is not None,
            destination_hash=_export_destination_hash(settings),
            last_success_at=last_success_at,
            last_success_event_id=last_success_event_id,
        )
        _write_export_status(
            settings,
            source_path=target,
            result=final_result,
            trusted_root=trusted_root,
            include_exported=False,
            started_at=started_at,
        )
    return event


def record_counter(
    name: str,
    *,
    value: float = 1.0,
    unit: str = "1",
    attributes: Mapping[str, Any] | None = None,
    source: str | None = None,
    session_id: str | None = None,
    path: Path | None = None,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> TelemetryMetric | None:
    """Append one OpenTelemetry-style counter point to the metric spool."""

    return _record_metric(
        name,
        instrument="counter",
        value=value,
        unit=unit,
        attributes=attributes,
        source=source,
        session_id=session_id,
        path=path,
        trusted_root=trusted_root,
        config=config,
    )


def record_histogram(
    name: str,
    *,
    value: float,
    unit: str = "ms",
    attributes: Mapping[str, Any] | None = None,
    source: str | None = None,
    session_id: str | None = None,
    path: Path | None = None,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
) -> TelemetryMetric | None:
    """Append one OpenTelemetry-style histogram observation to the metric spool."""

    return _record_metric(
        name,
        instrument="histogram",
        value=value,
        unit=unit,
        attributes=attributes,
        source=source,
        session_id=session_id,
        path=path,
        trusted_root=trusted_root,
        config=config,
    )


def exception_payload(
    exc: BaseException,
    *,
    config: Mapping[str, Any] | None = None,
    hash_salt: str | bytes | None = None,
    stack_limit: int = 50,
) -> dict[str, Any]:
    """Return privacy-safe exception attributes with hashed message and stack."""

    settings = _settings(config)
    resolved_salt = hash_salt if hash_salt is not None else settings["hash_salt"]
    exc_type = _exception_type(exc)
    message = str(exc)
    stack = "".join(
        traceback.format_exception(
            type(exc),
            exc,
            exc.__traceback__,
            limit=stack_limit,
        )
    )
    message_hash = hash_identifier(message, salt=resolved_salt) if message else None
    stack_hash = hash_identifier(stack, salt=resolved_salt) if stack else None
    fingerprint = hash_identifier(
        "|".join(part for part in (exc_type, message_hash, stack_hash) if part),
        salt=resolved_salt,
    )
    payload: dict[str, Any] = {
        "error.type": type(exc).__name__,
        "ctx.exception.type": exc_type,
        "ctx.exception.fingerprint": fingerprint,
        "ctx.exception.frame_count": len(traceback.extract_tb(exc.__traceback__)),
        "ctx.exception.chain_depth": _exception_chain_depth(exc),
    }
    if message_hash is not None:
        payload["ctx.exception.message_hash"] = message_hash
    if stack_hash is not None:
        payload["ctx.exception.stack_hash"] = stack_hash
    if exc.__cause__ is not None:
        payload["ctx.exception.cause.type"] = _exception_type(exc.__cause__)
    if exc.__context__ is not None:
        payload["ctx.exception.context.type"] = _exception_type(exc.__context__)
    return payload


def record_exception(
    event_name: str,
    *,
    source: str,
    exc: BaseException,
    payload: Mapping[str, Any] | None = None,
    escaped: bool = True,
    **record_event_kwargs: Any,
) -> TelemetryEvent | None:
    """Record one privacy-safe exception event."""

    config = record_event_kwargs.get("config")
    event_payload = dict(payload or {})
    try:
        event_payload.update(exception_payload(exc, config=config))
    except ValueError as config_exc:
        print(f"ctx telemetry: invalid exception config ({config_exc})", file=sys.stderr)
        return None
    event_payload["ctx.exception.escaped"] = escaped
    outcome = str(record_event_kwargs.pop("outcome", "error"))
    error_kind = record_event_kwargs.pop("error_kind", type(exc).__name__)
    return record_event(
        event_name,
        source=source,
        payload=event_payload,
        outcome=outcome,
        error_kind=error_kind,
        **record_event_kwargs,
    )


def _exception_type(exc: BaseException) -> str:
    cls = type(exc)
    return f"{cls.__module__}.{cls.__qualname__}"


def _exception_chain_depth(exc: BaseException) -> int:
    depth = 0
    seen: set[int] = set()
    current = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        depth += 1
        current = current.__cause__ or current.__context__
    return depth


def _record_metric(
    name: str,
    *,
    instrument: str,
    value: float,
    unit: str,
    attributes: Mapping[str, Any] | None,
    source: str | None,
    session_id: str | None,
    path: Path | None,
    trusted_root: Path | None,
    config: Mapping[str, Any] | None,
) -> TelemetryMetric | None:
    try:
        settings = _metric_settings(config)
    except ValueError as exc:
        print(f"ctx telemetry: invalid metric config ({exc})", file=sys.stderr)
        return None
    privacy_mode = str(settings["mode"])
    if not settings["metrics_enabled"] or privacy_mode in DISABLED_MODES:
        return None
    active_span = _CURRENT_SPAN.get()
    sanitized_attributes = _sanitize_payload(
        dict(attributes or {}),
        privacy_mode=privacy_mode,
        hash_salt=settings["hash_salt"],
        max_keys=int(settings["max_payload_keys"]),
        max_value_len=int(settings["max_payload_value_chars"]),
    )
    metric = TelemetryMetric(
        schema_version=METRIC_SCHEMA_VERSION,
        metric_id=uuid.uuid4().hex,
        ts=_now_iso(),
        name=name,
        instrument=instrument,
        value=float(value),
        unit=unit,
        source=source,
        attributes=sanitized_attributes,
        session_hash=hash_identifier(session_id, salt=settings["hash_salt"])
        if session_id
        else None,
        trace_id=active_span.trace_id if active_span is not None else None,
        span_id=active_span.span_id if active_span is not None else None,
        privacy_mode=privacy_mode,
        ctx_version=_ctx_version(),
    )
    target = _resolve_path(path or Path(str(settings["metrics_path"])), trusted_root=trusted_root)
    try:
        ensure_private_event_file(target)
        line = json.dumps(asdict(metric), ensure_ascii=False, separators=(",", ":")) + "\n"
        with file_lock(target):
            with target.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:  # noqa: BLE001 - metrics must not break callers.
        print(f"ctx telemetry: failed to write metric ({type(exc).__name__})", file=sys.stderr)
        return None
    if settings["metric_export_enabled"]:
        _export_recorded_metric(
            metric, settings=settings, source_path=target, trusted_root=trusted_root
        )
    return metric


def read_metrics(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
) -> Iterator[TelemetryMetric]:
    """Yield well-formed metric points from the local metric spool."""

    spool = _read_metrics_with_malformed(path, trusted_root=trusted_root)
    yield from spool.metrics


def _read_metrics_with_malformed(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
) -> _MetricSpoolRead:
    target = _resolve_path(path or DEFAULT_METRICS_PATH, trusted_root=trusted_root)
    metrics: list[TelemetryMetric] = []
    metric_line_numbers: dict[str, int] = {}
    malformed_line_numbers: list[int] = []
    try:
        fh = target.open(encoding="utf-8")
    except FileNotFoundError:
        return _MetricSpoolRead(metrics, metric_line_numbers, malformed_line_numbers)
    with fh:
        for line_number, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                metric = TelemetryMetric(**json.loads(line))
                metrics.append(metric)
                metric_line_numbers[metric.metric_id] = line_number
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                malformed_line_numbers.append(line_number)
                msg = (
                    "ctx telemetry: skipping malformed metric at line "
                    f"{line_number} ({type(exc).__name__})"
                )
                print(msg, file=sys.stderr)
    return _MetricSpoolRead(metrics, metric_line_numbers, malformed_line_numbers)


def read_events(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
) -> Iterator[TelemetryEvent]:
    """Yield well-formed telemetry events from the local spool."""

    spool = _read_events_with_malformed(path, trusted_root=trusted_root)
    yield from spool.events


def _read_events_with_malformed(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
) -> _SpoolRead:
    """Return well-formed events and the number of malformed records skipped."""

    target = _resolve_path(path or DEFAULT_TELEMETRY_PATH, trusted_root=trusted_root)
    events: list[TelemetryEvent] = []
    event_line_numbers: dict[str, int] = {}
    malformed_line_numbers: list[int] = []
    try:
        fh = target.open(encoding="utf-8")
    except FileNotFoundError:
        return _SpoolRead(events, event_line_numbers, malformed_line_numbers)
    with fh:
        for line_number, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                event = TelemetryEvent(**json.loads(line))
                events.append(event)
                event_line_numbers[event.event_id] = line_number
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                malformed_line_numbers.append(line_number)
                msg = (
                    "ctx telemetry: skipping malformed event at line "
                    f"{line_number} ({type(exc).__name__})"
                )
                print(msg, file=sys.stderr)
    return _SpoolRead(events, event_line_numbers, malformed_line_numbers)


def export_events(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
    include_exported: bool = False,
) -> ExportResult:
    """Export locally spooled events using the configured enterprise sink."""

    settings = _settings(config)
    if not settings["enabled"] or not settings["export_enabled"]:
        return ExportResult(
            attempted=0,
            exported=0,
            failed=0,
            sink=str(settings["export_sink"]),
            status="noop",
        )
    source_path = path or Path(str(settings["path"]))
    started_at = _now_iso()
    pending = _events_pending_export(
        source_path,
        settings=settings,
        trusted_root=trusted_root,
        include_exported=include_exported,
    )
    result = _export_events(pending.events, settings=settings, trusted_root=trusted_root)
    checkpoint_after_event_id = pending.checkpoint_before_event_id
    checkpoint_advanced = False
    last_success_at = None
    last_success_event_id = None
    last_event = pending.events[-1] if pending.events else None
    if result.failed == 0 and result.exported and last_event is not None:
        last_success_at = _now_iso()
        last_success_event_id = last_event.event_id
    if (
        result.failed == 0
        and result.exported
        and last_event is not None
        and pending.malformed_pending_records == 0
    ):
        _write_export_checkpoint(
            settings,
            source_path=source_path,
            event=last_event,
            trusted_root=trusted_root,
        )
        checkpoint_after_event_id = last_event.event_id
        checkpoint_advanced = checkpoint_after_event_id != pending.checkpoint_before_event_id
    status_path = _export_status_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    status = _export_status(
        result,
        pending=pending,
    )
    final_result = ExportResult(
        attempted=result.attempted,
        exported=result.exported,
        failed=result.failed,
        sink=result.sink,
        status=status,
        error_kind=result.error_kind,
        checkpoint_path=str(pending.checkpoint_path),
        last_event_id=checkpoint_after_event_id,
        malformed_records=pending.malformed_total_records,
        malformed_pending_records=pending.malformed_pending_records,
        malformed_first_line=pending.malformed_first_line,
        malformed_last_line=pending.malformed_last_line,
        status_path=str(status_path),
        checkpoint_before_event_id=pending.checkpoint_before_event_id,
        checkpoint_after_event_id=checkpoint_after_event_id,
        checkpoint_advanced=checkpoint_advanced,
        checkpoint_found=pending.checkpoint_found,
        destination_hash=_export_destination_hash(settings),
        last_success_at=last_success_at,
        last_success_event_id=last_success_event_id,
    )
    _write_export_status(
        settings,
        source_path=source_path,
        result=final_result,
        trusted_root=trusted_root,
        include_exported=include_exported,
        started_at=started_at,
    )
    return final_result


def export_metrics(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
    include_exported: bool = False,
) -> MetricExportResult:
    """Export locally spooled metrics using the configured enterprise sink."""

    settings = _metric_settings(config)
    if not settings["metrics_enabled"] or not settings["metric_export_enabled"]:
        return MetricExportResult(
            attempted=0,
            exported=0,
            failed=0,
            sink=str(settings["metric_export_sink"]),
            status="noop",
        )
    source_path = path or Path(str(settings["metrics_path"]))
    started_at = _now_iso()
    pending = _metrics_pending_export(
        source_path,
        settings=settings,
        trusted_root=trusted_root,
        include_exported=include_exported,
    )
    result = _export_metrics(pending.metrics, settings=settings, trusted_root=trusted_root)
    checkpoint_after_metric_id = pending.checkpoint_before_metric_id
    checkpoint_advanced = False
    last_success_at = None
    last_success_metric_id = None
    last_metric = pending.metrics[-1] if pending.metrics else None
    if result.failed == 0 and result.exported and last_metric is not None:
        last_success_at = _now_iso()
        last_success_metric_id = last_metric.metric_id
    if (
        result.failed == 0
        and result.exported
        and last_metric is not None
        and pending.malformed_pending_records == 0
    ):
        _write_metric_export_checkpoint(
            settings,
            source_path=source_path,
            metric=last_metric,
            trusted_root=trusted_root,
        )
        checkpoint_after_metric_id = last_metric.metric_id
        checkpoint_advanced = checkpoint_after_metric_id != pending.checkpoint_before_metric_id
    status_path = _metric_export_status_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    status = _metric_export_status(result, pending=pending)
    final_result = MetricExportResult(
        attempted=result.attempted,
        exported=result.exported,
        failed=result.failed,
        sink=result.sink,
        status=status,
        error_kind=result.error_kind,
        checkpoint_path=str(pending.checkpoint_path),
        last_metric_id=checkpoint_after_metric_id,
        malformed_records=pending.malformed_total_records,
        malformed_pending_records=pending.malformed_pending_records,
        malformed_first_line=pending.malformed_first_line,
        malformed_last_line=pending.malformed_last_line,
        status_path=str(status_path),
        checkpoint_before_metric_id=pending.checkpoint_before_metric_id,
        checkpoint_after_metric_id=checkpoint_after_metric_id,
        checkpoint_advanced=checkpoint_advanced,
        checkpoint_found=pending.checkpoint_found,
        destination_hash=_metric_export_destination_hash(settings),
        last_success_at=last_success_at,
        last_success_metric_id=last_success_metric_id,
    )
    _write_metric_export_status(
        settings,
        source_path=source_path,
        result=final_result,
        trusted_root=trusted_root,
        include_exported=include_exported,
        started_at=started_at,
    )
    return final_result


def preview_metrics_export(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
    include_exported: bool = False,
) -> MetricExportResult:
    """Return the metric export count without writing to the sink or checkpoint."""

    settings = _metric_settings(config)
    if not settings["metrics_enabled"] or not settings["metric_export_enabled"]:
        return MetricExportResult(
            attempted=0,
            exported=0,
            failed=0,
            sink=str(settings["metric_export_sink"]),
            status="noop",
        )
    source_path = path or Path(str(settings["metrics_path"]))
    pending = _metrics_pending_export(
        source_path,
        settings=settings,
        trusted_root=trusted_root,
        include_exported=include_exported,
    )
    last_metric_id = (
        pending.metrics[-1].metric_id if pending.metrics else pending.checkpoint_before_metric_id
    )
    status_path = _metric_export_status_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    preview_result = MetricExportResult(
        attempted=len(pending.metrics),
        exported=len(pending.metrics),
        failed=0,
        sink=str(settings["metric_export_sink"]),
    )
    status = _metric_export_status(preview_result, pending=pending)
    return MetricExportResult(
        attempted=len(pending.metrics),
        exported=0,
        failed=0,
        sink=str(settings["metric_export_sink"]),
        status=status,
        checkpoint_path=str(pending.checkpoint_path),
        last_metric_id=last_metric_id,
        malformed_records=pending.malformed_total_records,
        malformed_pending_records=pending.malformed_pending_records,
        malformed_first_line=pending.malformed_first_line,
        malformed_last_line=pending.malformed_last_line,
        status_path=str(status_path),
        checkpoint_before_metric_id=pending.checkpoint_before_metric_id,
        checkpoint_after_metric_id=pending.checkpoint_before_metric_id,
        checkpoint_advanced=False,
        checkpoint_found=pending.checkpoint_found,
        destination_hash=_metric_export_destination_hash(settings),
    )


def preview_export(
    path: Path | None = None,
    *,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
    include_exported: bool = False,
) -> ExportResult:
    """Return the export count without writing to the sink or checkpoint."""

    settings = _settings(config)
    if not settings["enabled"] or not settings["export_enabled"]:
        return ExportResult(
            attempted=0,
            exported=0,
            failed=0,
            sink=str(settings["export_sink"]),
            status="noop",
        )
    source_path = path or Path(str(settings["path"]))
    pending = _events_pending_export(
        source_path,
        settings=settings,
        trusted_root=trusted_root,
        include_exported=include_exported,
    )
    last_event_id = (
        pending.events[-1].event_id if pending.events else pending.checkpoint_before_event_id
    )
    status_path = _export_status_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    preview_result = ExportResult(
        attempted=len(pending.events),
        exported=len(pending.events),
        failed=0,
        sink=str(settings["export_sink"]),
    )
    status = _export_status(preview_result, pending=pending)
    return ExportResult(
        attempted=len(pending.events),
        exported=0,
        failed=0,
        sink=str(settings["export_sink"]),
        status=status,
        checkpoint_path=str(pending.checkpoint_path),
        last_event_id=last_event_id,
        malformed_records=pending.malformed_total_records,
        malformed_pending_records=pending.malformed_pending_records,
        malformed_first_line=pending.malformed_first_line,
        malformed_last_line=pending.malformed_last_line,
        status_path=str(status_path),
        checkpoint_before_event_id=pending.checkpoint_before_event_id,
        checkpoint_after_event_id=pending.checkpoint_before_event_id,
        checkpoint_advanced=False,
        checkpoint_found=pending.checkpoint_found,
        destination_hash=_export_destination_hash(settings),
    )


def plan_telemetry_retention(
    *,
    signal: str = "all",
    event_path: Path | None = None,
    metrics_path: Path | None = None,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
    drop_malformed: bool | None = None,
) -> list[TelemetryRetentionResult]:
    """Return the retention changes that would be applied without writing files."""

    return _telemetry_retention_results(
        signal=signal,
        event_path=event_path,
        metrics_path=metrics_path,
        trusted_root=trusted_root,
        config=config,
        drop_malformed=drop_malformed,
        dry_run=True,
    )


def enforce_telemetry_retention(
    *,
    signal: str = "all",
    event_path: Path | None = None,
    metrics_path: Path | None = None,
    trusted_root: Path | None = None,
    config: Mapping[str, Any] | None = None,
    drop_malformed: bool | None = None,
    dry_run: bool = False,
) -> list[TelemetryRetentionResult]:
    """Apply the configured telemetry retention policy to local spools."""

    return _telemetry_retention_results(
        signal=signal,
        event_path=event_path,
        metrics_path=metrics_path,
        trusted_root=trusted_root,
        config=config,
        drop_malformed=drop_malformed,
        dry_run=dry_run,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ctx_version() -> str | None:
    try:
        return package_version("claude-ctx")
    except PackageNotFoundError:
        try:
            from ctx import __version__
        except (ImportError, AttributeError):
            return None
        return __version__ or None


def _resolve_path(path: Path, *, trusted_root: Path | None = None) -> Path:
    raw = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if ".." in raw.parts:
        raise ValueError(f"path escapes its parent directory: {raw}")
    if trusted_root is not None:
        root = trusted_root.resolve()
        resolved = raw.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise ValueError(f"path escapes trusted root {root}: {path}") from None
    return raw


def _settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(config or _config_get("telemetry", {}) or {})
    limits = raw.get("limits") if isinstance(raw.get("limits"), Mapping) else {}
    raw_privacy = raw.get("privacy")
    privacy: Mapping[str, Any] = raw_privacy if isinstance(raw_privacy, Mapping) else {}
    raw_export = raw.get("export")
    export: Mapping[str, Any] = raw_export if isinstance(raw_export, Mapping) else {}
    raw_otlp = export.get("otlp")
    otlp: Mapping[str, Any] = raw_otlp if isinstance(raw_otlp, Mapping) else {}
    otlp_allowed_hosts = _otlp_allowed_hosts(_mapping_get(otlp, "allowed_hosts", ()))
    export_enabled = bool(_mapping_get(export, "enabled", False))
    export_sink = str(_mapping_get(export, "sink", "otlp_http"))
    otlp_endpoint = _otlp_endpoint(str(_mapping_get(otlp, "endpoint", "")))
    if export_enabled and export_sink == "otlp_http":
        otlp_endpoint = _validate_otlp_endpoint(
            otlp_endpoint,
            allowed_hosts=otlp_allowed_hosts,
        )
    hash_salt_env = str(_mapping_get(privacy, "hash_salt_env", DEFAULT_HASH_SALT_ENV)).strip()
    hash_salt_path = str(_mapping_get(privacy, "hash_salt_path", DEFAULT_HASH_SALT_PATH))
    mode = str(raw.get("mode", DEFAULT_PRIVACY_MODE)).strip().lower()
    if mode not in ALLOWED_MODES:
        allowed = ", ".join(sorted(ALLOWED_MODES))
        raise ValueError(f"telemetry.mode must be one of: {allowed}")
    return {
        "enabled": bool(raw.get("enabled", True)),
        "mode": mode,
        "path": str(raw.get("path", DEFAULT_TELEMETRY_PATH)),
        "hash_salt": _resolve_hash_salt(privacy),
        "hash_salt_env": hash_salt_env or DEFAULT_HASH_SALT_ENV,
        "hash_salt_path": hash_salt_path,
        "export_enabled": export_enabled,
        "export_sink": export_sink,
        "export_path": str(_mapping_get(export, "path", DEFAULT_EXPORT_PATH)),
        "export_checkpoint_path": str(_mapping_get(export, "checkpoint_path", "")),
        "export_status_path": str(_mapping_get(export, "status_path", "")),
        "otlp_endpoint": otlp_endpoint,
        "otlp_allowed_hosts": sorted(otlp_allowed_hosts),
        "otlp_headers": _mapping_get(otlp, "headers", {}),
        "otlp_timeout_seconds": float(_mapping_get(otlp, "timeout_seconds", 5.0)),
        "otlp_service_name": str(_mapping_get(otlp, "service_name", "ctx")),
        "otlp_service_namespace": str(_mapping_get(otlp, "service_namespace", "ctx")),
        "otlp_deployment_environment": str(_mapping_get(otlp, "deployment_environment", "local")),
        "max_payload_keys": int(
            _mapping_get(limits, "max_payload_keys", _MAX_PAYLOAD_KEYS),
        ),
        "max_payload_value_chars": int(
            _mapping_get(limits, "max_payload_value_chars", _MAX_PAYLOAD_VALUE_LEN),
        ),
    }


def _metric_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(config or _config_get("telemetry", {}) or {})
    metrics_present = "metrics" in raw
    raw_metrics = raw.get("metrics")
    metrics: Mapping[str, Any] = raw_metrics if isinstance(raw_metrics, Mapping) else {}
    limits = raw.get("limits") if isinstance(raw.get("limits"), Mapping) else {}
    raw_privacy = raw.get("privacy")
    privacy: Mapping[str, Any] = raw_privacy if isinstance(raw_privacy, Mapping) else {}
    raw_export = metrics.get("export")
    export: Mapping[str, Any] = raw_export if isinstance(raw_export, Mapping) else {}
    raw_otlp = export.get("otlp")
    otlp: Mapping[str, Any] = raw_otlp if isinstance(raw_otlp, Mapping) else {}
    otlp_allowed_hosts = _otlp_allowed_hosts(_mapping_get(otlp, "allowed_hosts", ()))
    export_enabled = bool(_mapping_get(export, "enabled", False))
    export_sink = str(_mapping_get(export, "sink", "otlp_http"))
    otlp_endpoint = _otlp_metrics_endpoint(str(_mapping_get(otlp, "endpoint", "")))
    if export_enabled and export_sink == "otlp_http":
        otlp_endpoint = _validate_otlp_endpoint(
            otlp_endpoint,
            allowed_hosts=otlp_allowed_hosts,
        )
    mode = str(raw.get("mode", DEFAULT_PRIVACY_MODE)).strip().lower()
    if mode not in ALLOWED_MODES:
        allowed = ", ".join(sorted(ALLOWED_MODES))
        raise ValueError(f"telemetry.mode must be one of: {allowed}")
    default_enabled = bool(raw.get("enabled", True)) if metrics_present else False
    return {
        "metrics_enabled": bool(_mapping_get(metrics, "enabled", default_enabled)),
        "mode": mode,
        "metrics_path": str(_mapping_get(metrics, "path", DEFAULT_METRICS_PATH)),
        "hash_salt": _resolve_hash_salt(privacy),
        "metric_export_enabled": export_enabled,
        "metric_export_sink": export_sink,
        "metric_export_path": str(_mapping_get(export, "path", DEFAULT_METRICS_EXPORT_PATH)),
        "metric_export_checkpoint_path": str(_mapping_get(export, "checkpoint_path", "")),
        "metric_export_status_path": str(_mapping_get(export, "status_path", "")),
        "otlp_endpoint": otlp_endpoint,
        "otlp_allowed_hosts": sorted(otlp_allowed_hosts),
        "otlp_headers": _mapping_get(otlp, "headers", {}),
        "otlp_timeout_seconds": float(_mapping_get(otlp, "timeout_seconds", 5.0)),
        "otlp_service_name": str(_mapping_get(otlp, "service_name", "ctx")),
        "otlp_service_namespace": str(_mapping_get(otlp, "service_namespace", "ctx")),
        "otlp_deployment_environment": str(_mapping_get(otlp, "deployment_environment", "local")),
        "histogram_bounds": _histogram_bounds(metrics),
        "max_payload_keys": int(
            _mapping_get(limits, "max_payload_keys", _MAX_PAYLOAD_KEYS),
        ),
        "max_payload_value_chars": int(
            _mapping_get(limits, "max_payload_value_chars", _MAX_PAYLOAD_VALUE_LEN),
        ),
    }


def _retention_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(config or _config_get("telemetry", {}) or {})
    raw_retention = raw.get("retention")
    retention: Mapping[str, Any] = raw_retention if isinstance(raw_retention, Mapping) else {}
    events = retention.get("events")
    metrics = retention.get("metrics")
    return {
        "enabled": bool(_mapping_get(retention, "enabled", True)),
        "status_path": str(
            _mapping_get(
                retention,
                "status_path",
                "~/.ctx/telemetry/retention-status.json",
            )
        ),
        "drop_malformed": bool(_mapping_get(retention, "drop_malformed", False)),
        "min_keep_records": max(0, int(_mapping_get(retention, "min_keep_records", 1000))),
        "events": events if isinstance(events, Mapping) else {},
        "metrics": metrics if isinstance(metrics, Mapping) else {},
    }


def _retention_policy(
    settings: Mapping[str, Any],
    signal: str,
) -> tuple[int | None, int | None]:
    signal_policy = settings.get(signal)
    policy: Mapping[str, Any] = signal_policy if isinstance(signal_policy, Mapping) else {}
    raw_max_age_days = _mapping_get(policy, "max_age_days", None)
    raw_max_records = _mapping_get(policy, "max_records", None)
    max_age_days = max(0, int(raw_max_age_days)) if raw_max_age_days not in (None, "") else None
    max_records = max(0, int(raw_max_records)) if raw_max_records not in (None, "") else None
    return max_age_days, max_records


def _telemetry_retention_results(
    *,
    signal: str,
    event_path: Path | None,
    metrics_path: Path | None,
    trusted_root: Path | None,
    config: Mapping[str, Any] | None,
    drop_malformed: bool | None,
    dry_run: bool,
) -> list[TelemetryRetentionResult]:
    settings = _retention_settings(config)
    signal_names = _retention_signal_names(signal)
    event_settings = _settings(config)
    metric_settings = _metric_settings(config)
    status_path = _resolve_path(
        Path(str(settings["status_path"])),
        trusted_root=trusted_root,
    )
    results: list[TelemetryRetentionResult] = []
    for signal_name in signal_names:
        if signal_name == "events":
            source_path = event_path or Path(str(event_settings["path"]))
        else:
            source_path = metrics_path or Path(str(metric_settings["metrics_path"]))
        result = _retention_result_for_signal(
            signal=signal_name,
            source_path=source_path,
            settings=settings,
            trusted_root=trusted_root,
            drop_malformed=drop_malformed,
            dry_run=dry_run,
            status_path=status_path,
        )
        results.append(result)
    if not dry_run:
        _write_retention_status(status_path, results)
    return results


def _retention_signal_names(signal: str) -> tuple[str, ...]:
    normalized = signal.strip().lower()
    if normalized == "all":
        return ("events", "metrics")
    if normalized in {"event", "events"}:
        return ("events",)
    if normalized in {"metric", "metrics"}:
        return ("metrics",)
    raise ValueError("telemetry retention signal must be one of: all, events, metrics")


def _retention_result_for_signal(
    *,
    signal: str,
    source_path: Path,
    settings: Mapping[str, Any],
    trusted_root: Path | None,
    drop_malformed: bool | None,
    dry_run: bool,
    status_path: Path,
) -> TelemetryRetentionResult:
    path = _resolve_path(source_path, trusted_root=trusted_root)
    max_age_days, max_records = _retention_policy(settings, signal)
    min_keep_records = int(settings["min_keep_records"])
    effective_drop_malformed = (
        bool(settings["drop_malformed"]) if drop_malformed is None else drop_malformed
    )
    if not settings["enabled"]:
        return TelemetryRetentionResult(
            signal=signal,
            path=str(path),
            status="disabled",
            dry_run=dry_run,
            scanned_records=0,
            retained_records=0,
            dropped_records=0,
            malformed_records=0,
            malformed_dropped_records=0,
            max_age_days=max_age_days,
            max_records=max_records,
            min_keep_records=min_keep_records,
            status_path=str(status_path),
        )

    records, malformed_lines = _read_retention_records(path, signal=signal)
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max_age_days)
        if max_age_days is not None and max_age_days > 0
        else None
    )
    kept_records, dropped_records = _apply_retention_policy(
        records,
        cutoff=cutoff,
        max_records=max_records,
        min_keep_records=min_keep_records,
    )
    kept_by_index = {record.index: record for record in kept_records}
    dropped_count = len(dropped_records)
    malformed_dropped = len(malformed_lines) if effective_drop_malformed else 0
    retained_count = len(kept_records)
    status = "noop"
    if dropped_count or malformed_dropped:
        status = "planned" if dry_run else "pruned"
    if not dry_run and (dropped_count or malformed_dropped):
        _rewrite_retention_file(
            path,
            records=records,
            kept_by_index=kept_by_index,
            malformed_lines=malformed_lines,
            drop_malformed=effective_drop_malformed,
        )
    return TelemetryRetentionResult(
        signal=signal,
        path=str(path),
        status=status,
        dry_run=dry_run,
        scanned_records=len(records),
        retained_records=retained_count,
        dropped_records=dropped_count,
        malformed_records=len(malformed_lines),
        malformed_dropped_records=malformed_dropped,
        max_age_days=max_age_days,
        max_records=max_records,
        min_keep_records=min_keep_records,
        cutoff_ts=(
            cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if cutoff is not None
            else None
        ),
        status_path=str(status_path),
    )


def _read_retention_records(
    path: Path,
    *,
    signal: str,
) -> tuple[list[_RetentionRecord], list[tuple[int, str]]]:
    records: list[_RetentionRecord] = []
    malformed_lines: list[tuple[int, str]] = []
    try:
        fh = path.open(encoding="utf-8")
    except FileNotFoundError:
        return records, malformed_lines
    with fh:
        for index, raw in enumerate(fh):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if signal == "events":
                    record_ts = TelemetryEvent(**payload).ts
                else:
                    record_ts = TelemetryMetric(**payload).ts
                records.append(
                    _RetentionRecord(
                        index=index,
                        raw_line=line + "\n",
                        ts=_parse_telemetry_ts(record_ts),
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                malformed_lines.append((index, line + "\n"))
                msg = (
                    f"ctx telemetry: skipping malformed {signal} retention record "
                    f"at line {index + 1} ({type(exc).__name__})"
                )
                print(msg, file=sys.stderr)
    return records, malformed_lines


def _apply_retention_policy(
    records: list[_RetentionRecord],
    *,
    cutoff: datetime | None,
    max_records: int | None,
    min_keep_records: int,
) -> tuple[list[_RetentionRecord], list[_RetentionRecord]]:
    if not records:
        return [], []
    protected_start = max(0, len(records) - min_keep_records)
    kept: list[_RetentionRecord] = []
    dropped: list[_RetentionRecord] = []
    for position, record in enumerate(records):
        protected = position >= protected_start
        if cutoff is not None and record.ts < cutoff and not protected:
            dropped.append(record)
        else:
            kept.append(record)
    effective_max_records = (
        max(max_records, min_keep_records) if max_records is not None and max_records > 0 else None
    )
    if effective_max_records is not None and len(kept) > effective_max_records:
        overflow = len(kept) - effective_max_records
        dropped.extend(kept[:overflow])
        kept = kept[overflow:]
    return kept, dropped


def _rewrite_retention_file(
    path: Path,
    *,
    records: list[_RetentionRecord],
    kept_by_index: Mapping[int, _RetentionRecord],
    malformed_lines: list[tuple[int, str]],
    drop_malformed: bool,
) -> None:
    lines: list[tuple[int, str]] = [
        (record.index, record.raw_line) for record in records if record.index in kept_by_index
    ]
    if not drop_malformed:
        lines.extend(malformed_lines)
    lines.sort(key=lambda item: item[0])
    ensure_private_event_file(path)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for _, line in lines:
                fh.write(line)
        os.replace(temp_path, path)
        try:
            os.chmod(path, _PRIVATE_FILE_MODE)
        except OSError:
            pass
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _write_retention_status(
    status_path: Path,
    results: list[TelemetryRetentionResult],
) -> None:
    payload = {
        "schema_version": RETENTION_STATUS_SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "updated_at": _now_iso(),
        "results": [asdict(result) for result in results],
    }
    try:
        ensure_private_event_file(status_path)
        with file_lock(status_path):
            _write_private_json(status_path, payload)
    except Exception as exc:  # noqa: BLE001 - retention status must not break pruning.
        print(
            f"ctx telemetry: failed to write retention status ({type(exc).__name__})",
            file=sys.stderr,
        )


def _parse_telemetry_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _histogram_bounds(metrics: Mapping[str, Any]) -> tuple[float, ...]:
    raw = _mapping_get(metrics, "histogram_bounds", _DEFAULT_HISTOGRAM_BOUNDS)
    if not isinstance(raw, (list, tuple)):
        return _DEFAULT_HISTOGRAM_BOUNDS
    bounds = tuple(sorted({float(value) for value in raw if float(value) >= 0}))
    return bounds or _DEFAULT_HISTOGRAM_BOUNDS


def _config_get(key: str, default: Any) -> Any:
    try:
        from ctx_config import cfg  # noqa: PLC0415

        return cfg.get(key, default)
    except Exception:  # noqa: BLE001 - config is optional for import-time safety.
        return default


def _mapping_get(mapping: object, key: str, default: Any) -> Any:
    if isinstance(mapping, Mapping):
        return mapping.get(key, default)
    return default


def _resolve_hash_salt(privacy: Mapping[str, Any] | None = None) -> str | bytes | None:
    effective_privacy = privacy
    if effective_privacy is None:
        raw = _config_get("telemetry", {}) or {}
        raw_privacy = _mapping_get(raw, "privacy", {})
        effective_privacy = raw_privacy if isinstance(raw_privacy, Mapping) else {}

    default_env_value = os.environ.get(DEFAULT_HASH_SALT_ENV)
    if default_env_value:
        return default_env_value

    hash_salt_env = str(
        _mapping_get(effective_privacy, "hash_salt_env", DEFAULT_HASH_SALT_ENV)
    ).strip()
    if hash_salt_env and hash_salt_env != DEFAULT_HASH_SALT_ENV:
        env_value = os.environ.get(hash_salt_env)
        if env_value:
            return env_value

    configured = _mapping_get(effective_privacy, "hash_salt", "")
    if isinstance(configured, bytes):
        return configured or None
    configured_text = str(configured)
    if configured_text:
        return configured_text

    configured_path = _mapping_get(effective_privacy, "hash_salt_path", None)
    if not configured_path:
        return None
    salt_path = Path(str(configured_path))
    try:
        return _read_or_create_hash_salt(salt_path)
    except OSError:
        return None


def _read_or_create_hash_salt(path: Path) -> str:
    target = _resolve_path(path)
    ensure_private_event_file(target)
    with file_lock(target):
        existing = target.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        generated = secrets.token_urlsafe(32)
        target.write_text(generated + "\n", encoding="utf-8")
        try:
            os.chmod(target, _PRIVATE_FILE_MODE)
        except OSError:
            pass
        return generated


def _otlp_endpoint(configured: str) -> str:
    logs_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
    if logs_endpoint:
        return logs_endpoint
    base_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if base_endpoint:
        return base_endpoint.rstrip("/") + "/v1/logs"
    return configured or DEFAULT_OTLP_LOGS_ENDPOINT


def _otlp_metrics_endpoint(configured: str) -> str:
    metrics_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT")
    if metrics_endpoint:
        return metrics_endpoint
    base_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if base_endpoint:
        return base_endpoint.rstrip("/") + "/v1/metrics"
    return configured or DEFAULT_OTLP_METRICS_ENDPOINT


def _otlp_allowed_hosts(raw: object) -> frozenset[str]:
    if raw in (None, ""):
        return frozenset()
    if isinstance(raw, str):
        values: object = [raw]
    else:
        values = raw
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("telemetry.export.otlp.allowed_hosts must be a list of hosts")
    hosts = {_normalize_otlp_host(str(value)) for value in values}
    hosts.discard("")
    return frozenset(hosts)


def _validate_otlp_endpoint(endpoint: str, *, allowed_hosts: frozenset[str]) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("telemetry.export.otlp.endpoint must use http or https")
    if not parsed.hostname:
        raise ValueError("telemetry.export.otlp.endpoint must include a host")
    if parsed.username or parsed.password:
        raise ValueError("telemetry.export.otlp.endpoint must not include userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("telemetry.export.otlp.endpoint must not include query or fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("telemetry.export.otlp.endpoint has an invalid port") from exc

    host = _normalize_otlp_host(parsed.hostname)
    if _is_loopback_host(host):
        return endpoint

    if _is_forbidden_otlp_ip(host):
        raise ValueError("telemetry.export.otlp.endpoint host is not allowed")
    if parsed.scheme != "https":
        raise ValueError("remote telemetry.export.otlp.endpoint must use https")
    if host not in allowed_hosts:
        raise ValueError(
            "remote telemetry.export.otlp.endpoint host must be listed in "
            "telemetry.export.otlp.allowed_hosts"
        )
    return endpoint


def _normalize_otlp_host(host: str) -> str:
    text = host.strip().lower().strip("[]").rstrip(".")
    if "/" in text:
        parsed = urlparse(text if "://" in text else f"//{text}")
        text = parsed.hostname or ""
    if text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text


def _is_loopback_host(host: str) -> bool:
    if host in _LOCAL_OTLP_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_forbidden_otlp_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_link_local
        or address.is_multicast
        or address.is_private
        or address.is_reserved
        or address.is_unspecified
    )


def _export_events(
    events: list[TelemetryEvent],
    *,
    settings: Mapping[str, Any],
    trusted_root: Path | None,
) -> ExportResult:
    sink = str(settings["export_sink"])
    if not events:
        return ExportResult(attempted=0, exported=0, failed=0, sink=sink)
    try:
        if sink == "local_jsonl":
            _export_local_jsonl(events, Path(str(settings["export_path"])), trusted_root)
        elif sink == "otlp_http":
            _post_otlp_http(_otlp_logs_payload(events, settings), settings)
        else:
            return ExportResult(
                attempted=len(events),
                exported=0,
                failed=len(events),
                sink=sink,
                error_kind="unsupported_sink",
            )
    except Exception as exc:  # noqa: BLE001 - exporters are best effort.
        print(f"ctx telemetry: export failed ({type(exc).__name__})", file=sys.stderr)
        return ExportResult(
            attempted=len(events),
            exported=0,
            failed=len(events),
            sink=sink,
            error_kind=type(exc).__name__,
        )
    return ExportResult(attempted=len(events), exported=len(events), failed=0, sink=sink)


def _export_recorded_metric(
    metric: TelemetryMetric,
    *,
    settings: Mapping[str, Any],
    source_path: Path,
    trusted_root: Path | None,
) -> None:
    started_at = _now_iso()
    result = _export_metrics([metric], settings=settings, trusted_root=trusted_root)
    checkpoint_path = _metric_export_checkpoint_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    checkpoint_before_metric_id = _read_metric_export_checkpoint(
        checkpoint_path,
        settings=settings,
        source_path=source_path,
    )
    checkpoint_after_metric_id = checkpoint_before_metric_id
    checkpoint_advanced = False
    last_success_at = None
    last_success_metric_id = None
    if result.failed == 0 and result.exported:
        _write_metric_export_checkpoint(
            settings,
            source_path=source_path,
            metric=metric,
            trusted_root=trusted_root,
        )
        checkpoint_after_metric_id = metric.metric_id
        checkpoint_advanced = checkpoint_after_metric_id != checkpoint_before_metric_id
        last_success_at = _now_iso()
        last_success_metric_id = metric.metric_id
    status_path = _metric_export_status_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    status = "failed" if result.failed else ("ok" if result.exported else "noop")
    final_result = MetricExportResult(
        attempted=result.attempted,
        exported=result.exported,
        failed=result.failed,
        sink=result.sink,
        status=status,
        error_kind=result.error_kind,
        checkpoint_path=str(checkpoint_path),
        last_metric_id=checkpoint_after_metric_id,
        status_path=str(status_path),
        checkpoint_before_metric_id=checkpoint_before_metric_id,
        checkpoint_after_metric_id=checkpoint_after_metric_id,
        checkpoint_advanced=checkpoint_advanced,
        checkpoint_found=checkpoint_before_metric_id is not None,
        destination_hash=_metric_export_destination_hash(settings),
        last_success_at=last_success_at,
        last_success_metric_id=last_success_metric_id,
    )
    _write_metric_export_status(
        settings,
        source_path=source_path,
        result=final_result,
        trusted_root=trusted_root,
        include_exported=False,
        started_at=started_at,
    )


def _export_metrics(
    metrics: list[TelemetryMetric],
    *,
    settings: Mapping[str, Any],
    trusted_root: Path | None,
) -> MetricExportResult:
    sink = str(settings["metric_export_sink"])
    if not metrics:
        return MetricExportResult(attempted=0, exported=0, failed=0, sink=sink)
    try:
        if sink == "local_jsonl":
            _export_local_metrics_jsonl(
                metrics,
                Path(str(settings["metric_export_path"])),
                trusted_root,
            )
        elif sink == "otlp_http":
            _post_otlp_http(_otlp_metrics_payload(metrics, settings), settings)
        else:
            return MetricExportResult(
                attempted=len(metrics),
                exported=0,
                failed=len(metrics),
                sink=sink,
                error_kind="unsupported_sink",
            )
    except Exception as exc:  # noqa: BLE001 - exporters are best effort.
        print(f"ctx telemetry: metric export failed ({type(exc).__name__})", file=sys.stderr)
        return MetricExportResult(
            attempted=len(metrics),
            exported=0,
            failed=len(metrics),
            sink=sink,
            error_kind=type(exc).__name__,
        )
    return MetricExportResult(
        attempted=len(metrics),
        exported=len(metrics),
        failed=0,
        sink=sink,
    )


def _events_pending_export(
    source_path: Path,
    *,
    settings: Mapping[str, Any],
    trusted_root: Path | None,
    include_exported: bool,
) -> _PendingExport:
    spool = _read_events_with_malformed(
        source_path,
        trusted_root=trusted_root,
    )
    checkpoint_path = _export_checkpoint_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    checkpoint_event_id = None
    checkpoint_found = False
    pending_events = spool.events
    pending_start_line = 1
    if not include_exported:
        checkpoint_event_id = _read_export_checkpoint(
            checkpoint_path,
            settings=settings,
            source_path=source_path,
        )
    if checkpoint_event_id is not None:
        for index, event in enumerate(spool.events):
            if event.event_id == checkpoint_event_id:
                checkpoint_found = True
                pending_events = spool.events[index + 1 :]
                pending_start_line = spool.event_line_numbers.get(event.event_id, 0) + 1
                break

    pending_malformed_lines = [
        line_number
        for line_number in spool.malformed_line_numbers
        if line_number >= pending_start_line
    ]
    return _PendingExport(
        events=pending_events,
        checkpoint_path=checkpoint_path,
        checkpoint_before_event_id=checkpoint_event_id,
        checkpoint_found=checkpoint_found,
        malformed_total_records=len(spool.malformed_line_numbers),
        malformed_pending_records=len(pending_malformed_lines),
        malformed_first_line=pending_malformed_lines[0] if pending_malformed_lines else None,
        malformed_last_line=pending_malformed_lines[-1] if pending_malformed_lines else None,
    )


def _metrics_pending_export(
    source_path: Path,
    *,
    settings: Mapping[str, Any],
    trusted_root: Path | None,
    include_exported: bool,
) -> _PendingMetricExport:
    spool = _read_metrics_with_malformed(
        source_path,
        trusted_root=trusted_root,
    )
    checkpoint_path = _metric_export_checkpoint_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    checkpoint_metric_id = None
    checkpoint_found = False
    pending_metrics = spool.metrics
    pending_start_line = 1
    if not include_exported:
        checkpoint_metric_id = _read_metric_export_checkpoint(
            checkpoint_path,
            settings=settings,
            source_path=source_path,
        )
    if checkpoint_metric_id is not None:
        for index, metric in enumerate(spool.metrics):
            if metric.metric_id == checkpoint_metric_id:
                checkpoint_found = True
                pending_metrics = spool.metrics[index + 1 :]
                pending_start_line = spool.metric_line_numbers.get(metric.metric_id, 0) + 1
                break

    pending_malformed_lines = [
        line_number
        for line_number in spool.malformed_line_numbers
        if line_number >= pending_start_line
    ]
    return _PendingMetricExport(
        metrics=pending_metrics,
        checkpoint_path=checkpoint_path,
        checkpoint_before_metric_id=checkpoint_metric_id,
        checkpoint_found=checkpoint_found,
        malformed_total_records=len(spool.malformed_line_numbers),
        malformed_pending_records=len(pending_malformed_lines),
        malformed_first_line=pending_malformed_lines[0] if pending_malformed_lines else None,
        malformed_last_line=pending_malformed_lines[-1] if pending_malformed_lines else None,
    )


def _metric_export_checkpoint_path(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    trusted_root: Path | None,
) -> Path:
    configured = str(settings.get("metric_export_checkpoint_path") or "")
    raw = Path(configured) if configured else Path(str(source_path) + ".export-checkpoint.json")
    return _resolve_path(raw, trusted_root=trusted_root)


def _metric_export_status_path(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    trusted_root: Path | None,
) -> Path:
    configured = str(settings.get("metric_export_status_path") or "")
    raw = Path(configured) if configured else Path(str(source_path) + ".export-status.json")
    return _resolve_path(raw, trusted_root=trusted_root)


def _read_metric_export_checkpoint(
    path: Path,
    *,
    settings: Mapping[str, Any],
    source_path: Path,
) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, Mapping):
        return None
    expected_source_hash = hash_identifier(str(source_path), salt=settings.get("hash_salt"))
    if payload.get("source_path_hash") != expected_source_hash:
        return None
    if payload.get("sink") != str(settings["metric_export_sink"]):
        return None
    if payload.get("destination_hash") != _metric_export_destination_hash(settings):
        return None
    value = payload.get("last_metric_id")
    return str(value) if value else None


def _write_metric_export_checkpoint(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    metric: TelemetryMetric,
    trusted_root: Path | None,
) -> None:
    checkpoint_path = _metric_export_checkpoint_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    payload = {
        "schema_version": METRIC_SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "sink": str(settings["metric_export_sink"]),
        "destination_hash": _metric_export_destination_hash(settings),
        "source_path_hash": hash_identifier(str(source_path), salt=settings.get("hash_salt")),
        "last_metric_id": metric.metric_id,
        "last_metric_ts": metric.ts,
    }
    ensure_private_event_file(checkpoint_path)
    with file_lock(checkpoint_path):
        _write_private_json(checkpoint_path, payload)


def _write_metric_export_status(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    result: MetricExportResult,
    trusted_root: Path | None,
    include_exported: bool,
    started_at: str | None = None,
) -> None:
    status_path = _metric_export_status_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    payload = {
        "schema_version": METRIC_SCHEMA_VERSION + ".export_status",
        "metric_schema_version": METRIC_SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "status": result.status,
        "started_at": started_at,
        "updated_at": _now_iso(),
        "finished_at": _now_iso(),
        "sink": result.sink,
        "destination_hash": result.destination_hash,
        "source_path_hash": hash_identifier(str(source_path), salt=settings.get("hash_salt")),
        "attempted": result.attempted,
        "exported": result.exported,
        "failed": result.failed,
        "error_kind": result.error_kind,
        "checkpoint_path": result.checkpoint_path,
        "checkpoint_before_metric_id": result.checkpoint_before_metric_id,
        "checkpoint_after_metric_id": result.checkpoint_after_metric_id,
        "checkpoint_advanced": result.checkpoint_advanced,
        "checkpoint_found": result.checkpoint_found,
        "last_metric_id": result.last_metric_id,
        "malformed_records": result.malformed_records,
        "malformed_total_records": result.malformed_records,
        "malformed_pending_records": result.malformed_pending_records,
        "malformed_first_line": result.malformed_first_line,
        "malformed_last_line": result.malformed_last_line,
        "last_success_at": result.last_success_at,
        "last_success_metric_id": result.last_success_metric_id,
        "include_exported": include_exported,
    }
    try:
        ensure_private_event_file(status_path)
        with file_lock(status_path):
            _write_private_json(status_path, payload)
    except Exception as exc:  # noqa: BLE001 - metric status must not break callers.
        print(
            f"ctx telemetry: failed to write metric export status ({type(exc).__name__})",
            file=sys.stderr,
        )


def _export_checkpoint_path(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    trusted_root: Path | None,
) -> Path:
    configured = str(settings.get("export_checkpoint_path") or "")
    raw = Path(configured) if configured else Path(str(source_path) + ".export-checkpoint.json")
    return _resolve_path(raw, trusted_root=trusted_root)


def _export_status_path(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    trusted_root: Path | None,
) -> Path:
    configured = str(settings.get("export_status_path") or "")
    raw = Path(configured) if configured else Path(str(source_path) + ".export-status.json")
    return _resolve_path(raw, trusted_root=trusted_root)


def _read_export_checkpoint(
    path: Path,
    *,
    settings: Mapping[str, Any],
    source_path: Path,
) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, Mapping):
        return None
    expected_source_hash = hash_identifier(str(source_path), salt=settings.get("hash_salt"))
    if payload.get("source_path_hash") != expected_source_hash:
        return None
    if payload.get("sink") != str(settings["export_sink"]):
        return None
    if payload.get("destination_hash") != _export_destination_hash(settings):
        return None
    value = payload.get("last_event_id")
    return str(value) if value else None


def _write_export_checkpoint(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    event: TelemetryEvent,
    trusted_root: Path | None,
) -> None:
    checkpoint_path = _export_checkpoint_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "sink": str(settings["export_sink"]),
        "destination_hash": _export_destination_hash(settings),
        "source_path_hash": hash_identifier(str(source_path), salt=settings.get("hash_salt")),
        "last_event_id": event.event_id,
        "last_event_ts": event.ts,
    }
    ensure_private_event_file(checkpoint_path)
    with file_lock(checkpoint_path):
        _write_private_json(checkpoint_path, payload)


def _write_export_status(
    settings: Mapping[str, Any],
    *,
    source_path: Path,
    result: ExportResult,
    trusted_root: Path | None,
    include_exported: bool,
    started_at: str | None = None,
) -> None:
    status_path = _export_status_path(
        settings,
        source_path=source_path,
        trusted_root=trusted_root,
    )
    payload = {
        "schema_version": EXPORT_STATUS_SCHEMA_VERSION,
        "event_schema_version": SCHEMA_VERSION,
        "run_id": uuid.uuid4().hex,
        "status": result.status,
        "started_at": started_at,
        "updated_at": _now_iso(),
        "finished_at": _now_iso(),
        "sink": result.sink,
        "destination_hash": result.destination_hash,
        "source_path_hash": hash_identifier(str(source_path), salt=settings.get("hash_salt")),
        "attempted": result.attempted,
        "exported": result.exported,
        "failed": result.failed,
        "error_kind": result.error_kind,
        "checkpoint_path": result.checkpoint_path,
        "checkpoint_before_event_id": result.checkpoint_before_event_id,
        "checkpoint_after_event_id": result.checkpoint_after_event_id,
        "checkpoint_advanced": result.checkpoint_advanced,
        "checkpoint_found": result.checkpoint_found,
        "last_event_id": result.last_event_id,
        "malformed_records": result.malformed_records,
        "malformed_total_records": result.malformed_records,
        "malformed_pending_records": result.malformed_pending_records,
        "malformed_first_line": result.malformed_first_line,
        "malformed_last_line": result.malformed_last_line,
        "last_success_at": result.last_success_at,
        "last_success_event_id": result.last_success_event_id,
        "include_exported": include_exported,
    }
    try:
        ensure_private_event_file(status_path)
        with file_lock(status_path):
            _write_private_json(status_path, payload)
    except Exception as exc:  # noqa: BLE001 - telemetry status must not break callers.
        print(
            f"ctx telemetry: failed to write export status ({type(exc).__name__})",
            file=sys.stderr,
        )


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    ensure_private_event_file(path)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
        os.replace(temp_path, path)
        try:
            os.chmod(path, _PRIVATE_FILE_MODE)
        except OSError:
            pass
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _metric_export_destination_hash(settings: Mapping[str, Any]) -> str:
    sink = str(settings["metric_export_sink"])
    if sink == "local_jsonl":
        destination = str(settings.get("metric_export_path") or "")
    elif sink == "otlp_http":
        destination = str(settings.get("otlp_endpoint") or "")
    else:
        destination = sink
    return hash_identifier(f"metrics:{sink}:{destination}", salt=settings.get("hash_salt"))


def _export_destination_hash(settings: Mapping[str, Any]) -> str:
    sink = str(settings["export_sink"])
    if sink == "local_jsonl":
        destination = str(settings.get("export_path") or "")
    elif sink == "otlp_http":
        destination = str(settings.get("otlp_endpoint") or "")
    else:
        destination = sink
    return hash_identifier(f"{sink}:{destination}", salt=settings.get("hash_salt"))


def _metric_export_status(result: MetricExportResult, *, pending: _PendingMetricExport) -> str:
    checkpoint_anomaly = (
        pending.checkpoint_before_metric_id is not None and not pending.checkpoint_found
    )
    if result.failed:
        return "failed"
    if pending.malformed_pending_records or checkpoint_anomaly:
        return "degraded"
    if result.attempted == 0:
        return "noop"
    return "ok"


def _export_status(result: ExportResult, *, pending: _PendingExport) -> str:
    checkpoint_anomaly = (
        pending.checkpoint_before_event_id is not None and not pending.checkpoint_found
    )
    if result.failed:
        return "failed"
    if pending.malformed_pending_records or checkpoint_anomaly:
        return "degraded"
    if result.attempted == 0:
        return "noop"
    return "ok"


def _export_local_metrics_jsonl(
    metrics: list[TelemetryMetric],
    path: Path,
    trusted_root: Path | None,
) -> None:
    target = _resolve_path(path, trusted_root=trusted_root)
    ensure_private_event_file(target)
    with file_lock(target):
        with target.open("a", encoding="utf-8") as fh:
            for metric in metrics:
                fh.write(json.dumps(asdict(metric), ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")


def _export_local_jsonl(
    events: list[TelemetryEvent],
    path: Path,
    trusted_root: Path | None,
) -> None:
    target = _resolve_path(path, trusted_root=trusted_root)
    ensure_private_event_file(target)
    with file_lock(target):
        with target.open("a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(asdict(event), ensure_ascii=False, separators=(",", ":")))
                fh.write("\n")


def _post_otlp_http(payload: Mapping[str, Any], settings: Mapping[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    configured_headers = settings.get("otlp_headers")
    if isinstance(configured_headers, Mapping):
        headers.update({str(key): str(value) for key, value in configured_headers.items()})
    request = Request(str(settings["otlp_endpoint"]), data=body, headers=headers, method="POST")
    opener = build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=float(settings["otlp_timeout_seconds"])) as response:
            status = int(getattr(response, "status", response.getcode()))
            if status != 200:
                raise RuntimeError(f"OTLP HTTP export failed with status {status}")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"OTLP HTTP export failed: {exc}") from exc


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _otlp_logs_payload(events: list[TelemetryEvent], settings: Mapping[str, Any]) -> dict[str, Any]:
    resource_attributes = {
        "service.name": str(settings["otlp_service_name"]),
        "service.namespace": str(settings["otlp_service_namespace"]),
        "deployment.environment": str(settings["otlp_deployment_environment"]),
    }
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _otlp_attributes(resource_attributes)},
                "scopeLogs": [
                    {
                        "scope": {"name": "ctx.telemetry", "version": SCHEMA_VERSION},
                        "logRecords": [
                            _otlp_log_record(event, settings=settings) for event in events
                        ],
                    }
                ],
            }
        ]
    }


def _otlp_metrics_payload(
    metrics: list[TelemetryMetric],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    resource_attributes = {
        "service.name": str(settings["otlp_service_name"]),
        "service.namespace": str(settings["otlp_service_namespace"]),
        "deployment.environment": str(settings["otlp_deployment_environment"]),
    }
    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": _otlp_attributes(resource_attributes)},
                "scopeMetrics": [
                    {
                        "scope": {"name": "ctx.telemetry", "version": METRIC_SCHEMA_VERSION},
                        "metrics": [
                            _otlp_metric_record(metric, settings=settings) for metric in metrics
                        ],
                    }
                ],
            }
        ]
    }


def _otlp_metric_record(
    metric: TelemetryMetric,
    *,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    data_point = _otlp_metric_data_point(metric)
    record: dict[str, Any] = {
        "name": metric.name,
        "unit": metric.unit,
    }
    if metric.instrument == "counter":
        record["sum"] = {
            "aggregationTemporality": _OTEL_AGGREGATION_TEMPORALITY_DELTA,
            "isMonotonic": True,
            "dataPoints": [data_point],
        }
        return record

    bounds = tuple(settings.get("histogram_bounds") or _DEFAULT_HISTOGRAM_BOUNDS)
    record["histogram"] = {
        "aggregationTemporality": _OTEL_AGGREGATION_TEMPORALITY_DELTA,
        "dataPoints": [_otlp_histogram_data_point(metric, bounds=bounds)],
    }
    return record


def _otlp_metric_data_point(metric: TelemetryMetric) -> dict[str, Any]:
    observed_nanos = _iso_to_unix_nanos(metric.ts)
    data_point: dict[str, Any] = {
        "timeUnixNano": str(observed_nanos),
        "attributes": _otlp_metric_attributes(metric),
    }
    if float(metric.value).is_integer():
        data_point["asInt"] = str(int(metric.value))
    else:
        data_point["asDouble"] = metric.value
    return data_point


def _otlp_histogram_data_point(
    metric: TelemetryMetric,
    *,
    bounds: tuple[float, ...],
) -> dict[str, Any]:
    observed_nanos = _iso_to_unix_nanos(metric.ts)
    bucket_counts = [0 for _ in range(len(bounds) + 1)]
    bucket_index = 0
    for index, bound in enumerate(bounds):
        if metric.value <= bound:
            bucket_index = index
            break
    else:
        bucket_index = len(bounds)
    bucket_counts[bucket_index] = 1
    return {
        "timeUnixNano": str(observed_nanos),
        "count": "1",
        "sum": metric.value,
        "min": metric.value,
        "max": metric.value,
        "bucketCounts": [str(count) for count in bucket_counts],
        "explicitBounds": list(bounds),
        "attributes": _otlp_metric_attributes(metric),
    }


def _otlp_metric_attributes(metric: TelemetryMetric) -> list[dict[str, Any]]:
    attributes = {
        "ctx.metric_id": metric.metric_id,
        "ctx.schema_version": metric.schema_version,
        "ctx.instrument": metric.instrument,
        "ctx.privacy_mode": metric.privacy_mode,
        **{f"ctx.metric.{key}": value for key, value in metric.attributes.items()},
    }
    optional = {
        "ctx.source": metric.source,
        "ctx.session.hash": metric.session_hash,
        "ctx.trace_id": metric.trace_id,
        "ctx.span_id": metric.span_id,
        "ctx.version": metric.ctx_version,
    }
    attributes.update({key: value for key, value in optional.items() if value is not None})
    return _otlp_attributes(attributes)


def _otlp_log_record(event: TelemetryEvent, *, settings: Mapping[str, Any]) -> dict[str, Any]:
    observed_nanos = _iso_to_unix_nanos(event.ts)
    session_hash = event.session_hash
    if session_hash is None and event.session_id:
        session_hash = hash_identifier(event.session_id, salt=settings["hash_salt"])
    attributes = {
        "event.name": event.event_name,
        "ctx.schema_version": event.schema_version,
        "ctx.event_id": event.event_id,
        "ctx.source": event.source,
        "ctx.outcome": event.outcome,
        "ctx.privacy_mode": event.privacy_mode,
        **{f"ctx.payload.{key}": value for key, value in event.payload.items()},
    }
    optional = {
        "ctx.session.hash": session_hash,
        "ctx.trace_id": event.trace_id,
        "ctx.span_id": event.span_id,
        "ctx.parent_span_id": event.parent_span_id,
        "ctx.transport": event.transport,
        "ctx.actor": event.actor,
        "ctx.duration_ms": event.duration_ms,
        "error.type": event.error_kind,
        "ctx.repo_hash": event.repo_hash,
        "ctx.cwd_hash": event.cwd_hash,
        "ctx.graph_export_id": event.graph_export_id,
        "ctx.wiki_export_id": event.wiki_export_id,
        "ctx.version": event.ctx_version,
    }
    attributes.update({key: value for key, value in optional.items() if value is not None})
    record = {
        "timeUnixNano": str(observed_nanos),
        "observedTimeUnixNano": str(observed_nanos),
        "severityText": "ERROR" if event.outcome == "error" else "INFO",
        "body": {"stringValue": event.event_name},
        "attributes": _otlp_attributes(attributes),
    }
    if event.trace_id:
        record["traceId"] = event.trace_id
    if event.span_id:
        record["spanId"] = event.span_id
    return record


def _iso_to_unix_nanos(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1_000_000_000)


def _otlp_attributes(attributes: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"key": key, "value": _otlp_value(value)} for key, value in sorted(attributes.items())]


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"stringValue": ""}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, sort_keys=True, default=str)}


def _sanitize_payload(
    payload: Mapping[str, Any],
    *,
    privacy_mode: str,
    hash_salt: str | bytes | None = None,
    max_keys: int = _MAX_PAYLOAD_KEYS,
    max_value_len: int = _MAX_PAYLOAD_VALUE_LEN,
    depth: int = 0,
) -> dict[str, Any]:
    if depth >= _MAX_PAYLOAD_DEPTH:
        return {"_truncated": repr(type(payload).__name__)}
    if len(payload) > max_keys:
        raise ValueError(f"payload has {len(payload)} keys; max {max_keys}")
    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            raise TypeError(f"payload key must be str: {key!r}")
        if secret_key_like(key):
            sanitized[key] = "[redacted]"
            continue
        normalized = key.lower().replace("-", "_").replace(".", "_")
        if privacy_mode == DEFAULT_PRIVACY_MODE and _raw_value_key_like(normalized):
            if value is not None:
                sanitized[f"{key}_hash"] = hash_identifier(str(value), salt=hash_salt)
            continue
        sanitized[key] = _sanitize_value(
            value,
            privacy_mode=privacy_mode,
            hash_salt=hash_salt,
            max_value_len=max_value_len,
            depth=depth + 1,
        )
    return sanitized


def _sanitize_value(
    value: Any,
    *,
    privacy_mode: str,
    hash_salt: str | bytes | None,
    max_value_len: int,
    depth: int,
) -> Any:
    if isinstance(value, _SCALAR_TYPES):
        if isinstance(value, str):
            text = redact_secret_text(value)
            if len(text) > max_value_len:
                return text[:max_value_len] + "...[truncated]"
            return text
        return value
    if depth >= _MAX_PAYLOAD_DEPTH:
        return repr(type(value).__name__)
    if isinstance(value, Mapping):
        return _sanitize_payload(
            value,
            privacy_mode=privacy_mode,
            hash_salt=hash_salt,
            max_value_len=max_value_len,
            depth=depth,
        )
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_value(
                item,
                privacy_mode=privacy_mode,
                hash_salt=hash_salt,
                max_value_len=max_value_len,
                depth=depth + 1,
            )
            for item in value[:max_value_len]
        ]
    return repr(value)
