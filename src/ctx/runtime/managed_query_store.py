"""Private durable index for exact managed-query registration and completion.

This store is a crash-safe bridge for the managed-query service. It never
plans, opens artifacts, carries executable authority, or accepts a caller-made
public reference. Persisted registrations contain only canonical protocol
events and digest bindings that already crossed the trusted intake boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, NoReturn, SupportsIndex, cast

from ctx.engine.capability_schema import CAPABILITY_KINDS, validate_capability_identity
from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.store import StreamId
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import ensure_secure_directory, reject_symlink_path


_SCHEMA_VERSION: Final = 2
_REGISTRATION_SCHEMA: Final = "ctx.managed-query-registration-v1"
_ROW_SCHEMA: Final = "ctx.managed-query-store-row-v1"
_DESIRED_SET_SCHEMA: Final = "ctx.managed-desired-set-registration-v1"
_DESIRED_SET_ROW_SCHEMA: Final = "ctx.managed-desired-set-row-v1"
_QUERY_REF_DOMAIN: Final = b"ctx.managed-query-ref-v1\x00"
_ROW_HMAC_DOMAIN: Final = b"ctx.managed-query-row-v1\x00"
_DESIRED_SET_REF_DOMAIN: Final = b"ctx.managed-desired-set-ref-v1\x00"
_DESIRED_SET_ROW_HMAC_DOMAIN: Final = b"ctx.managed-desired-set-row-v1\x00"
_DESIRED_SET_STREAM_DOMAIN: Final = b"ctx.managed-desired-set-stream-v1\x00"
_KEY_CHECK_DOMAIN: Final = b"ctx.managed-query-installation-key-v1\x00"
_OBSERVATION_PROVIDER_ID: Final = "ctx-managed-artifact-observation-v1"
_PRIVATE_DIRECTORY_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600
_BUSY_TIMEOUT_MS: Final = 30_000
_MAX_EVENT_BYTES: Final = 16 * 1024
_MAX_REGISTRATION_BYTES: Final = 36 * 1024
_MAX_DESIRED_SET_EVENT_BYTES: Final = 24 * 1024
_MAX_CAPABILITY_IDS_BYTES: Final = 1_024
_MAX_DATABASE_BYTES: Final = 64 * 1024 * 1024
_MAX_ROWS: Final = 4_096
_MAX_DESIRED_SET_ROWS: Final = 16_384
_MAX_SQLITE_INTEGER: Final = (1 << 63) - 1
_SQLITE_SIDECARS: Final = ("-wal", "-shm", "-journal")
_DIGEST_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_QUERY_REF_RE: Final = re.compile(r"\Amqr_[0-9a-f]{64}\Z")
_DESIRED_SET_REF_RE: Final = re.compile(r"\Amds_[0-9a-f]{64}\Z")
_FACTORY_TOKEN = object()
_RECORD_FACTORY_TOKEN = object()
_DESIRED_SET_RECORD_FACTORY_TOKEN = object()

_SCHEMA = """
CREATE TABLE managed_query_store_identity (
    singleton       INTEGER PRIMARY KEY NOT NULL CHECK(singleton = 1),
    key_check       TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE managed_queries (
    logical_query_id        TEXT PRIMARY KEY NOT NULL,
    query_ref               TEXT NOT NULL UNIQUE,
    registration_json       BLOB NOT NULL,
    registration_bytes      INTEGER NOT NULL,
    plan_id                 TEXT,
    decision_digest         TEXT,
    journal_revision        INTEGER,
    journal_record_digest   TEXT,
    row_hmac                TEXT NOT NULL
) WITHOUT ROWID;

CREATE INDEX idx_managed_queries_plan_id ON managed_queries(plan_id);

CREATE TABLE managed_desired_sets (
    query_ref                   TEXT NOT NULL,
    logical_choice_id           TEXT NOT NULL,
    desired_set_ref             TEXT NOT NULL,
    plan_id                     TEXT NOT NULL,
    decision_digest             TEXT NOT NULL,
    stream_identity_digest      TEXT NOT NULL,
    expected_revision           INTEGER NOT NULL,
    event_id                    TEXT NOT NULL,
    event_json                  BLOB NOT NULL,
    event_bytes                 INTEGER NOT NULL,
    capability_ids_json         BLOB NOT NULL,
    capability_ids_bytes        INTEGER NOT NULL,
    journal_revision            INTEGER,
    journal_record_digest       TEXT,
    transition_digest           TEXT,
    row_hmac                    TEXT NOT NULL,
    PRIMARY KEY (query_ref, logical_choice_id)
) WITHOUT ROWID;

CREATE UNIQUE INDEX idx_managed_desired_sets_ref
    ON managed_desired_sets(desired_set_ref);
CREATE UNIQUE INDEX idx_managed_desired_sets_stream_revision
    ON managed_desired_sets(stream_identity_digest, expected_revision);
CREATE INDEX idx_managed_desired_sets_query_revision
    ON managed_desired_sets(query_ref, expected_revision);
CREATE INDEX idx_managed_desired_sets_stream_state
    ON managed_desired_sets(stream_identity_digest, journal_revision);
"""

_EXPECTED_OBJECTS: Final = [
    ("index", "idx_managed_desired_sets_query_revision", "managed_desired_sets"),
    ("index", "idx_managed_desired_sets_ref", "managed_desired_sets"),
    ("index", "idx_managed_desired_sets_stream_revision", "managed_desired_sets"),
    ("index", "idx_managed_desired_sets_stream_state", "managed_desired_sets"),
    ("index", "idx_managed_queries_plan_id", "managed_queries"),
    ("table", "managed_desired_sets", "managed_desired_sets"),
    ("table", "managed_queries", "managed_queries"),
    ("table", "managed_query_store_identity", "managed_query_store_identity"),
]
_EXPECTED_IDENTITY_COLUMNS: Final = {
    "singleton": ("INTEGER", 1, 1),
    "key_check": ("TEXT", 1, 0),
}
_EXPECTED_QUERY_COLUMNS: Final = {
    "logical_query_id": ("TEXT", 1, 1),
    "query_ref": ("TEXT", 1, 0),
    "registration_json": ("BLOB", 1, 0),
    "registration_bytes": ("INTEGER", 1, 0),
    "plan_id": ("TEXT", 0, 0),
    "decision_digest": ("TEXT", 0, 0),
    "journal_revision": ("INTEGER", 0, 0),
    "journal_record_digest": ("TEXT", 0, 0),
    "row_hmac": ("TEXT", 1, 0),
}
_EXPECTED_DESIRED_SET_COLUMNS: Final = {
    "query_ref": ("TEXT", 1, 1),
    "logical_choice_id": ("TEXT", 1, 2),
    "desired_set_ref": ("TEXT", 1, 0),
    "plan_id": ("TEXT", 1, 0),
    "decision_digest": ("TEXT", 1, 0),
    "stream_identity_digest": ("TEXT", 1, 0),
    "expected_revision": ("INTEGER", 1, 0),
    "event_id": ("TEXT", 1, 0),
    "event_json": ("BLOB", 1, 0),
    "event_bytes": ("INTEGER", 1, 0),
    "capability_ids_json": ("BLOB", 1, 0),
    "capability_ids_bytes": ("INTEGER", 1, 0),
    "journal_revision": ("INTEGER", 0, 0),
    "journal_record_digest": ("TEXT", 0, 0),
    "transition_digest": ("TEXT", 0, 0),
    "row_hmac": ("TEXT", 1, 0),
}
_ROW_COLUMNS: Final = tuple(_EXPECTED_QUERY_COLUMNS)
_SELECT_COLUMNS: Final = ", ".join(_ROW_COLUMNS)
_DESIRED_SET_ROW_COLUMNS: Final = tuple(_EXPECTED_DESIRED_SET_COLUMNS)
_DESIRED_SET_SELECT_COLUMNS: Final = ", ".join(_DESIRED_SET_ROW_COLUMNS)


class ManagedQueryStoreError(RuntimeError):
    """Base class for durable managed-query index failures."""


class ManagedQueryStoreUnavailable(ManagedQueryStoreError):
    """The owner-private filesystem or SQLite store is unavailable."""


class ManagedQueryStoreCorruption(ManagedQueryStoreError):
    """Persisted schema, bytes, authentication, or lifecycle is invalid."""


class ManagedQueryStoreConflict(ManagedQueryStoreError):
    """A logical identity or completion binding was substituted."""


class ManagedQueryStoreCapacityExceeded(ManagedQueryStoreError):
    """The bounded durable store cannot accept another query."""


class ManagedQueryStoreNotFound(ManagedQueryStoreError):
    """No exact authenticated managed-query record exists."""


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManagedQueryStoreCorruption("managed query JSON contains duplicate fields")
        result[key] = value
    return result


def _decode_canonical_mapping(payload: bytes) -> dict[str, object]:
    if not 1 <= len(payload) <= _MAX_REGISTRATION_BYTES:
        raise ManagedQueryStoreCorruption("managed query registration size is invalid")
    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ManagedQueryStoreCorruption("managed query JSON is non-finite")
            ),
        )
    except ManagedQueryStoreCorruption:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ManagedQueryStoreCorruption("managed query registration JSON is invalid") from exc
    if not isinstance(decoded, dict) or _canonical_bytes(decoded) != payload:
        raise ManagedQueryStoreCorruption("managed query registration JSON is noncanonical")
    return cast(dict[str, object], decoded)


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded canonical token")
    return value


def _query_ref(value: object) -> str:
    if not isinstance(value, str) or _QUERY_REF_RE.fullmatch(value) is None:
        raise ValueError("query_ref must be an opaque managed-query reference")
    return value


def _desired_set_ref(value: object) -> str:
    if not isinstance(value, str) or _DESIRED_SET_REF_RE.fullmatch(value) is None:
        raise ValueError("desired_set_ref must be an opaque managed desired-set reference")
    return value


def _capability_ids(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise TypeError("capability_ids must be an exact tuple")
    if len(value) > 5:
        raise ValueError("capability_ids cannot contain more than five choices")
    result = tuple(_token(item, "capability_id") for item in value)
    if len(set(result)) != len(result):
        raise ValueError("capability_ids must be unique")
    return result


def _capability_ids_bytes(value: tuple[str, ...]) -> bytes:
    payload = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(payload) > _MAX_CAPABILITY_IDS_BYTES:
        raise ValueError("capability_ids exceed their persistent byte bound")
    return payload


def _decode_capability_ids(payload: bytes) -> tuple[str, ...]:
    if not 2 <= len(payload) <= _MAX_CAPABILITY_IDS_BYTES:
        raise ManagedQueryStoreCorruption("desired-set capability IDs size is invalid")
    try:
        decoded = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ManagedQueryStoreCorruption("desired-set capability IDs are non-finite")
            ),
        )
    except ManagedQueryStoreCorruption:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ManagedQueryStoreCorruption("desired-set capability IDs JSON is invalid") from exc
    if not isinstance(decoded, list):
        raise ManagedQueryStoreCorruption("desired-set capability IDs must be an array")
    try:
        exact = _capability_ids(tuple(decoded))
    except (TypeError, ValueError) as exc:
        raise ManagedQueryStoreCorruption("desired-set capability IDs are invalid") from exc
    if _capability_ids_bytes(exact) != payload:
        raise ManagedQueryStoreCorruption("desired-set capability IDs JSON is noncanonical")
    return exact


def _stream_identity_digest(scope: ScopeRef) -> str:
    stream_key = StreamId.from_scope(scope).key.encode("utf-8")
    return hashlib.sha256(_DESIRED_SET_STREAM_DOMAIN + stream_key).hexdigest()


def _safe_scope(scope: object) -> ScopeRef:
    if type(scope) is not ScopeRef:
        raise TypeError("managed query scope must be an exact ScopeRef")
    for name in (
        "tenant_id",
        "workspace_id",
        "repository_id",
        "session_id",
        "exposure_id",
        "host_context_id",
    ):
        _token(getattr(scope, name), f"scope.{name}")
    if scope.parent_exposure_id is not None:
        _token(scope.parent_exposure_id, "scope.parent_exposure_id")
    return scope


def _safe_event_tokens(event: EngineEvent, label: str) -> None:
    for name in (
        "event_id",
        "correlation_id",
        "causation_id",
        "engine_version",
        "planner_version",
        "policy_version",
    ):
        _token(getattr(event, name), f"{label}.{name}")
    for name in (
        "host_descriptor_digest",
        "catalog_snapshot_digest",
        "semantic_model_digest",
        "semantic_index_digest",
        "work_signature",
    ):
        _digest(getattr(event, name), f"{label}.{name}")
    if type(event.random_seed) is not int:
        raise ValueError(f"{label}.random_seed must be an integer")


def _event_payload(event: EngineEvent) -> dict[str, object]:
    payload = event.to_dict()["payload"]
    if not isinstance(payload, dict):
        raise ValueError("managed query event payload is invalid")
    return cast(dict[str, object], payload)


def _validated_registration(
    *,
    logical_query_id: object,
    session_started: object,
    decision_event: object,
    artifact_manifest_digest: object,
    planning_environment_digest: object,
) -> dict[str, object]:
    logical_id = _digest(logical_query_id, "logical_query_id")
    manifest_digest = _digest(artifact_manifest_digest, "artifact_manifest_digest")
    environment_digest = _digest(
        planning_environment_digest,
        "planning_environment_digest",
    )
    if type(session_started) is not EngineEvent or type(decision_event) is not EngineEvent:
        raise TypeError("managed query events must be exact EngineEvent values")
    _safe_scope(session_started.scope)
    _safe_scope(decision_event.scope)
    _safe_event_tokens(session_started, "session_started")
    _safe_event_tokens(decision_event, "decision_event")
    if session_started.kind != "SessionStarted" or session_started.expected_revision != 0:
        raise ValueError("managed query must begin with SessionStarted revision zero")
    if decision_event.kind not in {"IntentObserved", "DevelopmentObserved"}:
        raise ValueError("managed query decision event kind is unsupported")
    if not 1 <= decision_event.expected_revision < _MAX_SQLITE_INTEGER:
        raise ValueError("managed query decision revision must follow session start")
    if decision_event.kind != (
        "IntentObserved" if decision_event.expected_revision == 1 else "DevelopmentObserved"
    ):
        raise ValueError("managed query decision kind does not match its stream revision")
    if _event_payload(session_started) != {"host_level": "managing"}:
        raise ValueError("managed query session payload is not the exact managing envelope")
    decision_payload = _event_payload(decision_event)
    if set(decision_payload) != {"observation_ref"}:
        raise ValueError("managed query planning payload must contain only observation_ref")
    observation_ref = decision_payload["observation_ref"]
    if not isinstance(observation_ref, dict) or set(observation_ref) != {
        "provider_id",
        "opaque_id",
        "content_digest",
    }:
        raise ValueError("managed query observation reference is invalid")
    if (
        observation_ref["provider_id"] != _OBSERVATION_PROVIDER_ID
        or observation_ref["opaque_id"] != f"manifest-{manifest_digest}"
    ):
        raise ValueError("managed query observation reference is not artifact-bound")
    _digest(observation_ref["content_digest"], "observation_ref.content_digest")
    if session_started.event_id == decision_event.event_id:
        raise ValueError("managed query event identity relationships are invalid")
    for name in (
        "planner_version",
        "host_descriptor_digest",
        "catalog_snapshot_digest",
        "semantic_model_digest",
        "semantic_index_digest",
    ):
        if getattr(session_started, name) != getattr(decision_event, name):
            raise ValueError(f"managed query events disagree on {name}")
    if decision_event.kind == "IntentObserved":
        if (
            session_started.scope != decision_event.scope
            or session_started.correlation_id != decision_event.correlation_id
            or decision_event.causation_id != session_started.event_id
        ):
            raise ValueError("managed query initial event identity relationships are invalid")
        for name in (
            "engine_version",
            "policy_version",
            "work_signature",
            "random_seed",
        ):
            if getattr(session_started, name) != getattr(decision_event, name):
                raise ValueError(f"managed query initial events disagree on {name}")
    elif StreamId.from_scope(session_started.scope) != StreamId.from_scope(decision_event.scope):
        raise ValueError("managed query development events must share one stable stream")
    if session_started.catalog_snapshot_digest != environment_digest:
        raise ValueError("managed query planning environment binding is invalid")
    started_json = session_started.to_json()
    decision_json = decision_event.to_json()
    if (
        len(started_json.encode("ascii")) > _MAX_EVENT_BYTES
        or len(decision_json.encode("ascii")) > _MAX_EVENT_BYTES
    ):
        raise ValueError("managed query event exceeds its persistent byte bound")
    mapping: dict[str, object] = {
        "artifact_manifest_digest": manifest_digest,
        "decision_event": decision_event.to_dict(),
        "logical_query_id": logical_id,
        "planning_environment_digest": environment_digest,
        "schema": _REGISTRATION_SCHEMA,
        "session_started": session_started.to_dict(),
    }
    if len(_canonical_bytes(mapping)) > _MAX_REGISTRATION_BYTES:
        raise ValueError("managed query registration exceeds its persistent byte bound")
    return mapping


def _registration_from_mapping(
    mapping: Mapping[str, object],
) -> tuple[dict[str, object], EngineEvent, EngineEvent]:
    if (
        set(mapping)
        != {
            "artifact_manifest_digest",
            "decision_event",
            "logical_query_id",
            "planning_environment_digest",
            "schema",
            "session_started",
        }
        or mapping.get("schema") != _REGISTRATION_SCHEMA
    ):
        raise ManagedQueryStoreCorruption("managed query registration fields are invalid")
    started_value = mapping.get("session_started")
    decision_value = mapping.get("decision_event")
    if not isinstance(started_value, Mapping) or not isinstance(decision_value, Mapping):
        raise ManagedQueryStoreCorruption("managed query persisted events are invalid")
    try:
        started = EngineEvent.from_dict(started_value)
        decision = EngineEvent.from_dict(decision_value)
        validated = _validated_registration(
            logical_query_id=mapping.get("logical_query_id"),
            session_started=started,
            decision_event=decision,
            artifact_manifest_digest=mapping.get("artifact_manifest_digest"),
            planning_environment_digest=mapping.get("planning_environment_digest"),
        )
    except (TypeError, ValueError) as exc:
        raise ManagedQueryStoreCorruption(
            "managed query persisted registration is invalid"
        ) from exc
    if validated != mapping:
        raise ManagedQueryStoreCorruption("managed query persisted registration is not exact")
    return validated, started, decision


def _derive_query_ref(key: bytes, registration: bytes) -> str:
    return "mqr_" + hmac.digest(key, _QUERY_REF_DOMAIN + registration, "sha256").hex()


def _row_hmac(
    key: bytes,
    *,
    query_ref: str,
    registration: Mapping[str, object],
    plan_id: str | None,
    decision_digest: str | None,
    journal_revision: int | None,
    journal_record_digest: str | None,
) -> str:
    body = _canonical_bytes(
        {
            "decision_digest": decision_digest,
            "journal_record_digest": journal_record_digest,
            "journal_revision": journal_revision,
            "plan_id": plan_id,
            "query_ref": query_ref,
            "registration": registration,
            "schema": _ROW_SCHEMA,
        }
    )
    return hmac.digest(key, _ROW_HMAC_DOMAIN + body, "sha256").hex()


def _validated_desired_set_registration(
    *,
    parent: ManagedQueryRecord,
    logical_choice_id: object,
    capability_ids: object,
    event: object,
) -> tuple[dict[str, object], bytes, bytes]:
    if type(parent) is not ManagedQueryRecord or not parent.planned:
        raise ManagedQueryStoreConflict("desired-set parent query is not planned")
    exact_choice_id = _digest(logical_choice_id, "logical_choice_id")
    exact_capability_ids = _capability_ids(capability_ids)
    if type(event) is not EngineEvent:
        raise TypeError("desired-set event must be an exact EngineEvent")
    _safe_scope(event.scope)
    _safe_event_tokens(event, "desired_set_event")
    if event.kind != "ReassessmentRequested":
        raise ValueError("desired-set event must be ReassessmentRequested")
    if (
        type(event.expected_revision) is not int
        or parent.journal_revision is None
        or not parent.journal_revision <= event.expected_revision < _MAX_SQLITE_INTEGER
    ):
        raise ValueError("desired-set event revision cannot precede its planned parent")
    if event.scope != parent.decision_event.scope:
        raise ManagedQueryStoreConflict("desired-set scope does not match its parent query")
    if event.correlation_id != parent.plan_id:
        raise ManagedQueryStoreConflict("desired-set plan does not match its parent query")
    parent_event = parent.decision_event
    if any(
        getattr(event, field_name) != getattr(parent_event, field_name)
        for field_name in (
            "engine_version",
            "planner_version",
            "policy_version",
            "host_descriptor_digest",
            "catalog_snapshot_digest",
            "semantic_model_digest",
            "semantic_index_digest",
            "work_signature",
            "random_seed",
            "privacy",
        )
    ):
        raise ManagedQueryStoreConflict(
            "desired-set replay envelope does not match its parent query"
        )
    payload = _event_payload(event)
    if set(payload) != {
        "desired_capabilities",
        "owner_id",
        "policy_snapshot_digest",
    }:
        raise ValueError("desired-set event payload fields are invalid")
    _token(payload["owner_id"], "desired_set_event.owner_id")
    _digest(payload["policy_snapshot_digest"], "desired_set_event.policy_snapshot_digest")
    raw_rows = payload["desired_capabilities"]
    if not isinstance(raw_rows, list):
        raise ValueError("desired-set event capabilities must be an array")
    observed_ids: list[str] = []
    for index, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, Mapping) or set(raw_row) != {
            "actionability",
            "capability_id",
            "install_descriptor_digest",
            "install_plan_digest",
            "kind",
            "lease_id",
            "source_digest",
        }:
            raise ValueError(f"desired-set capability row {index} fields are invalid")
        capability_id = _token(raw_row["capability_id"], f"capability_ids[{index}]")
        kind = _token(raw_row["kind"], f"desired_capabilities[{index}].kind")
        if kind not in CAPABILITY_KINDS:
            raise ValueError(f"desired-set capability row {index} kind is invalid")
        try:
            validate_capability_identity(capability_id, kind)
        except ValueError as exc:
            raise ValueError(
                f"desired-set capability row {index} typed identity is invalid"
            ) from exc
        _token(raw_row["lease_id"], f"desired_capabilities[{index}].lease_id")
        _digest(raw_row["source_digest"], f"desired_capabilities[{index}].source_digest")
        actionability = _token(
            raw_row["actionability"],
            f"desired_capabilities[{index}].actionability",
        )
        if actionability not in {"load", "install", "manual"}:
            raise ValueError(f"desired-set capability row {index} actionability is invalid")
        descriptor_digest = raw_row["install_descriptor_digest"]
        plan_digest = raw_row["install_plan_digest"]
        if actionability == "install":
            _digest(
                descriptor_digest,
                f"desired_capabilities[{index}].install_descriptor_digest",
            )
            _digest(plan_digest, f"desired_capabilities[{index}].install_plan_digest")
        elif descriptor_digest is not None or plan_digest is not None:
            raise ValueError(f"desired-set capability row {index} has unexpected install identity")
        observed_ids.append(capability_id)
    if tuple(observed_ids) != exact_capability_ids:
        raise ManagedQueryStoreConflict(
            "desired-set capability IDs do not match the exact event choice"
        )
    event_bytes = event.to_json().encode("utf-8")
    if not 1 <= len(event_bytes) <= _MAX_DESIRED_SET_EVENT_BYTES:
        raise ValueError("desired-set event exceeds its persistent byte bound")
    ids_bytes = _capability_ids_bytes(exact_capability_ids)
    if parent.plan_id is None or parent.decision_digest is None:
        raise ManagedQueryStoreCorruption("planned parent query lacks its decision binding")
    registration: dict[str, object] = {
        "capability_ids": list(exact_capability_ids),
        "decision_digest": parent.decision_digest,
        "event_content_digest": event.content_digest,
        "logical_choice_id": exact_choice_id,
        "plan_id": parent.plan_id,
        "query_ref": parent.query_ref,
        "schema": _DESIRED_SET_SCHEMA,
        "stream_identity_digest": _stream_identity_digest(event.scope),
    }
    return registration, event_bytes, ids_bytes


def _derive_desired_set_ref(key: bytes, registration: Mapping[str, object]) -> str:
    return (
        "mds_"
        + hmac.digest(
            key,
            _DESIRED_SET_REF_DOMAIN + _canonical_bytes(registration),
            "sha256",
        ).hex()
    )


def _desired_set_row_hmac(
    key: bytes,
    *,
    desired_set_ref: str,
    registration: Mapping[str, object],
    event_bytes: bytes,
    capability_ids_bytes: bytes,
    journal_revision: int | None,
    journal_record_digest: str | None,
    transition_digest: str | None,
) -> str:
    body = _canonical_bytes(
        {
            "capability_ids_sha256": hashlib.sha256(capability_ids_bytes).hexdigest(),
            "desired_set_ref": desired_set_ref,
            "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "journal_record_digest": journal_record_digest,
            "journal_revision": journal_revision,
            "registration": registration,
            "schema": _DESIRED_SET_ROW_SCHEMA,
            "transition_digest": transition_digest,
        }
    )
    return hmac.digest(key, _DESIRED_SET_ROW_HMAC_DOMAIN + body, "sha256").hex()


class ManagedQueryRecord:
    """Immutable, authority-free projection of one authenticated store row."""

    __slots__ = (
        "artifact_manifest_digest",
        "decision_digest",
        "decision_event",
        "journal_record_digest",
        "journal_revision",
        "logical_query_id",
        "plan_id",
        "planning_environment_digest",
        "query_ref",
        "session_started",
    )
    query_ref: str
    logical_query_id: str
    session_started: EngineEvent
    decision_event: EngineEvent
    artifact_manifest_digest: str
    planning_environment_digest: str
    plan_id: str | None
    decision_digest: str | None
    journal_revision: int | None
    journal_record_digest: str | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed query records are store-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        query_ref: str,
        logical_query_id: str,
        session_started: EngineEvent,
        decision_event: EngineEvent,
        artifact_manifest_digest: str,
        planning_environment_digest: str,
        plan_id: str | None,
        decision_digest: str | None,
        journal_revision: int | None,
        journal_record_digest: str | None,
    ) -> ManagedQueryRecord:
        if factory_token is not _RECORD_FACTORY_TOKEN:
            raise TypeError("managed query records are store-issued only")
        instance = object.__new__(cls)
        for name, value in (
            ("query_ref", query_ref),
            ("logical_query_id", logical_query_id),
            ("session_started", session_started),
            ("decision_event", decision_event),
            ("artifact_manifest_digest", artifact_manifest_digest),
            ("planning_environment_digest", planning_environment_digest),
            ("plan_id", plan_id),
            ("decision_digest", decision_digest),
            ("journal_revision", journal_revision),
            ("journal_record_digest", journal_record_digest),
        ):
            object.__setattr__(instance, name, value)
        return instance

    @property
    def planned(self) -> bool:
        return self.plan_id is not None

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed query records are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed query records are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed query records cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed query records cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed query records cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed query records cannot be serialized")

    def __eq__(self, other: object) -> bool:
        return type(other) is ManagedQueryRecord and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def __repr__(self) -> str:
        state = "planned" if self.planned else "registered"
        return f"ManagedQueryRecord(query_ref={self.query_ref!r}, state={state!r})"


class ManagedDesiredSetRecord:
    """Immutable, authority-free projection of one desired-set reservation."""

    __slots__ = (
        "capability_ids",
        "decision_digest",
        "desired_set_ref",
        "event",
        "journal_record_digest",
        "journal_revision",
        "logical_choice_id",
        "plan_id",
        "query_ref",
        "stream_identity_digest",
        "transition_digest",
    )
    query_ref: str
    logical_choice_id: str
    desired_set_ref: str
    plan_id: str
    decision_digest: str
    stream_identity_digest: str
    capability_ids: tuple[str, ...]
    event: EngineEvent
    journal_revision: int | None
    journal_record_digest: str | None
    transition_digest: str | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed desired-set records are store-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        query_ref: str,
        logical_choice_id: str,
        desired_set_ref: str,
        plan_id: str,
        decision_digest: str,
        stream_identity_digest: str,
        capability_ids: tuple[str, ...],
        event: EngineEvent,
        journal_revision: int | None,
        journal_record_digest: str | None,
        transition_digest: str | None,
    ) -> ManagedDesiredSetRecord:
        if factory_token is not _DESIRED_SET_RECORD_FACTORY_TOKEN:
            raise TypeError("managed desired-set records are store-issued only")
        instance = object.__new__(cls)
        for name, value in (
            ("query_ref", query_ref),
            ("logical_choice_id", logical_choice_id),
            ("desired_set_ref", desired_set_ref),
            ("plan_id", plan_id),
            ("decision_digest", decision_digest),
            ("stream_identity_digest", stream_identity_digest),
            ("capability_ids", capability_ids),
            ("event", event),
            ("journal_revision", journal_revision),
            ("journal_record_digest", journal_record_digest),
            ("transition_digest", transition_digest),
        ):
            object.__setattr__(instance, name, value)
        return instance

    @property
    def committed(self) -> bool:
        return self.journal_revision is not None

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed desired-set records are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed desired-set records are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed desired-set records cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed desired-set records cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed desired-set records cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed desired-set records cannot be serialized")

    def __eq__(self, other: object) -> bool:
        return type(other) is ManagedDesiredSetRecord and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def __repr__(self) -> str:
        state = "committed" if self.committed else "reserved"
        return f"ManagedDesiredSetRecord(desired_set_ref={self.desired_set_ref!r}, state={state!r})"


class ManagedQueryStore:
    """Factory-issued owner of one authenticated managed-query SQLite index."""

    __slots__ = (
        "_bound_identity",
        "_installation_hmac_key",
        "_path",
    )
    _path: Path
    _installation_hmac_key: bytes
    _bound_identity: tuple[int, int]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed query stores are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        path: Path,
        installation_hmac_key: bytes,
        bound_identity: tuple[int, int],
    ) -> ManagedQueryStore:
        if factory_token is not _FACTORY_TOKEN:
            raise TypeError("managed query stores are factory-issued only")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_path", path)
        object.__setattr__(instance, "_installation_hmac_key", installation_hmac_key)
        object.__setattr__(instance, "_bound_identity", bound_identity)
        return instance

    def register(
        self,
        *,
        logical_query_id: str,
        session_started: EngineEvent,
        decision_event: EngineEvent,
        artifact_manifest_digest: str,
        planning_environment_digest: str,
    ) -> ManagedQueryRecord:
        registration = _validated_registration(
            logical_query_id=logical_query_id,
            session_started=session_started,
            decision_event=decision_event,
            artifact_manifest_digest=artifact_manifest_digest,
            planning_environment_digest=planning_environment_digest,
        )
        _, _, exact_decision = _registration_from_mapping(registration)
        payload = _canonical_bytes(registration)
        query_ref = _derive_query_ref(self._installation_hmac_key, payload)
        row_auth = _row_hmac(
            self._installation_hmac_key,
            query_ref=query_ref,
            registration=registration,
            plan_id=None,
            decision_digest=None,
            journal_revision=None,
            journal_record_digest=None,
        )
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validated_all_desired_set_rows(connection)
                existing = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE logical_query_id = ?",
                    (logical_query_id,),
                ).fetchone()
                if existing is not None:
                    record = self._validated_row(existing)
                    existing_payload = _canonical_bytes(_registration_mapping_from_record(record))
                    if not hmac.compare_digest(existing_payload, payload):
                        raise ManagedQueryStoreConflict(
                            "logical query identity is already bound to different content"
                        )
                    connection.execute("COMMIT")
                    return record
                count = connection.execute("SELECT count(*) FROM managed_queries").fetchone()[0]
                if type(count) is not int or not 0 <= count <= _MAX_ROWS:
                    raise ManagedQueryStoreCorruption("managed query row count is invalid")
                if count == _MAX_ROWS:
                    raise ManagedQueryStoreCapacityExceeded(
                        "managed query store reached its bounded capacity"
                    )
                rows = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM managed_queries"
                ).fetchall()
                if len(rows) != count:
                    raise ManagedQueryStoreCorruption(
                        "managed query row count changed during registration"
                    )
                for row in rows:
                    other = self._validated_row(row)
                    if (
                        other.decision_event.scope == exact_decision.scope
                        and other.decision_event.correlation_id == exact_decision.correlation_id
                    ):
                        raise ManagedQueryStoreConflict(
                            "scope and plan identity already bind another managed query"
                        )
                collision = connection.execute(
                    "SELECT logical_query_id FROM managed_queries WHERE query_ref = ?",
                    (query_ref,),
                ).fetchone()
                if collision is not None:
                    raise ManagedQueryStoreConflict("derived managed query reference collided")
                connection.execute(
                    "INSERT INTO managed_queries "
                    "(logical_query_id, query_ref, registration_json, registration_bytes, "
                    "plan_id, decision_digest, journal_revision, journal_record_digest, row_hmac) "
                    "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                    (logical_query_id, query_ref, payload, len(payload), row_auth),
                )
                row = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE query_ref = ?",
                    (query_ref,),
                ).fetchone()
                if row is None:
                    raise ManagedQueryStoreCorruption("managed query insert was not durable")
                record = self._validated_row(row)
                _require_connection_size_bound(connection)
                connection.execute("COMMIT")
                return record
            except sqlite3.DatabaseError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if _sqlite_capacity_error(exc):
                    raise ManagedQueryStoreCapacityExceeded(
                        "managed query database reached its size bound"
                    ) from None
                raise
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def load(self, query_ref: str) -> ManagedQueryRecord:
        exact_ref = _query_ref(query_ref)
        with self._locked_connection() as connection:
            row = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE query_ref = ?",
                (exact_ref,),
            ).fetchone()
            if row is None:
                raise ManagedQueryStoreNotFound("managed query record is unavailable")
            return self._validated_row(row)

    def mark_planned(
        self,
        query_ref: str,
        *,
        plan_id: str,
        decision_digest: str,
        journal_revision: int,
        journal_record_digest: str,
    ) -> ManagedQueryRecord:
        exact_ref = _query_ref(query_ref)
        exact_plan_id = _token(plan_id, "plan_id")
        exact_decision_digest = _digest(decision_digest, "decision_digest")
        exact_record_digest = _digest(journal_record_digest, "journal_record_digest")
        if type(journal_revision) is not int or not 2 <= journal_revision <= _MAX_SQLITE_INTEGER:
            raise ValueError("journal_revision must be a bounded integer at least two")
        desired = (
            exact_plan_id,
            exact_decision_digest,
            journal_revision,
            exact_record_digest,
        )
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validated_all_desired_set_rows(connection)
                row = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE query_ref = ?",
                    (exact_ref,),
                ).fetchone()
                if row is None:
                    raise ManagedQueryStoreNotFound("managed query record is unavailable")
                record = self._validated_row(row)
                if exact_plan_id != record.decision_event.correlation_id:
                    raise ManagedQueryStoreConflict(
                        "plan identity does not match the managed decision event"
                    )
                if journal_revision != record.decision_event.expected_revision + 1:
                    raise ManagedQueryStoreConflict(
                        "journal revision does not match the managed decision event"
                    )
                current = (
                    record.plan_id,
                    record.decision_digest,
                    record.journal_revision,
                    record.journal_record_digest,
                )
                if record.planned:
                    if current != desired:
                        raise ManagedQueryStoreConflict(
                            "managed query completion is already bound differently"
                        )
                    connection.execute("COMMIT")
                    return record
                matches = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE plan_id = ?",
                    (exact_plan_id,),
                ).fetchall()
                for match in matches:
                    other = self._validated_row(match)
                    if other.decision_event.scope == record.decision_event.scope:
                        raise ManagedQueryStoreConflict(
                            "scope and plan identity already bind another managed query"
                        )
                registration = _registration_mapping_from_record(record)
                row_auth = _row_hmac(
                    self._installation_hmac_key,
                    query_ref=exact_ref,
                    registration=registration,
                    plan_id=exact_plan_id,
                    decision_digest=exact_decision_digest,
                    journal_revision=journal_revision,
                    journal_record_digest=exact_record_digest,
                )
                connection.execute(
                    "UPDATE managed_queries SET plan_id = ?, decision_digest = ?, "
                    "journal_revision = ?, journal_record_digest = ?, row_hmac = ? "
                    "WHERE query_ref = ?",
                    (*desired, row_auth, exact_ref),
                )
                updated = connection.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE query_ref = ?",
                    (exact_ref,),
                ).fetchone()
                if updated is None:
                    raise ManagedQueryStoreCorruption("managed query completion disappeared")
                result = self._validated_row(updated)
                _require_connection_size_bound(connection)
                connection.execute("COMMIT")
                return result
            except sqlite3.DatabaseError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if _sqlite_capacity_error(exc):
                    raise ManagedQueryStoreCapacityExceeded(
                        "managed query database reached its size bound"
                    ) from None
                raise
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def load_by_scope_and_plan(self, scope: ScopeRef, plan_id: str) -> ManagedQueryRecord:
        exact_scope = _safe_scope(scope)
        exact_plan_id = _token(plan_id, "plan_id")
        with self._locked_connection() as connection:
            rows = connection.execute(
                f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE plan_id = ?",
                (exact_plan_id,),
            ).fetchall()
            matches = [
                record
                for row in rows
                if (record := self._validated_row(row)).decision_event.scope == exact_scope
            ]
        if not matches:
            raise ManagedQueryStoreNotFound("managed query plan record is unavailable")
        if len(matches) != 1:
            raise ManagedQueryStoreCorruption("managed query plan lookup is ambiguous")
        return matches[0]

    def _validated_all_desired_set_rows(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[ManagedDesiredSetRecord, ...]:
        count = connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone()[0]
        if type(count) is not int or not 0 <= count <= _MAX_DESIRED_SET_ROWS:
            raise ManagedQueryStoreCorruption("desired-set row count is invalid")
        rows = connection.execute(
            f"SELECT {_DESIRED_SET_SELECT_COLUMNS} FROM managed_desired_sets"
        ).fetchall()
        if len(rows) != count:
            raise ManagedQueryStoreCorruption(
                "desired-set row count changed during authenticated scan"
            )
        parents: dict[str, ManagedQueryRecord] = {}
        records: list[ManagedDesiredSetRecord] = []
        for row in rows:
            row_query_ref = row["query_ref"]
            if not isinstance(row_query_ref, str):
                raise ManagedQueryStoreCorruption("desired-set parent query reference is invalid")
            parent = parents.get(row_query_ref)
            if parent is None:
                parent = self._load_parent_by_ref(
                    connection,
                    row_query_ref,
                    persisted_binding=True,
                )
                parents[row_query_ref] = parent
            records.append(self._validated_desired_set_row(row, parent=parent))

        streams: dict[tuple[str, str], list[ManagedDesiredSetRecord]] = {}
        for record in records:
            streams.setdefault(
                (record.stream_identity_digest, record.query_ref),
                [],
            ).append(record)
        for stream_records in streams.values():
            ordered = sorted(stream_records, key=lambda item: item.event.expected_revision)
            previous: ManagedDesiredSetRecord | None = None
            for record in ordered:
                parent = parents[record.query_ref]
                expected_causation = (
                    parent.decision_event.event_id if previous is None else previous.event.event_id
                )
                if record.event.causation_id != expected_causation:
                    raise ManagedQueryStoreCorruption(
                        "desired-set persisted causation chain is invalid"
                    )
                if previous is not None and not previous.committed:
                    raise ManagedQueryStoreCorruption(
                        "desired-set persisted sequence follows an uncommitted choice"
                    )
                previous = record
        return tuple(records)

    def reserve_desired_set(
        self,
        *,
        query_ref: str,
        logical_choice_id: str,
        capability_ids: tuple[str, ...],
        event: EngineEvent,
    ) -> ManagedDesiredSetRecord:
        """Atomically reserve one exact desired-set event for a planned query."""

        exact_query_ref = _query_ref(query_ref)
        exact_choice_id = _digest(logical_choice_id, "logical_choice_id")
        exact_capability_ids = _capability_ids(capability_ids)
        if type(event) is not EngineEvent:
            raise TypeError("desired-set event must be an exact EngineEvent")
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                parent = self._load_parent_by_ref(connection, exact_query_ref)
                registration, event_bytes, capability_ids_bytes = (
                    _validated_desired_set_registration(
                        parent=parent,
                        logical_choice_id=exact_choice_id,
                        capability_ids=exact_capability_ids,
                        event=event,
                    )
                )
                desired_set_ref = _derive_desired_set_ref(
                    self._installation_hmac_key,
                    registration,
                )
                records = self._validated_all_desired_set_rows(connection)
                existing_record = next(
                    (
                        record
                        for record in records
                        if record.query_ref == exact_query_ref
                        and record.logical_choice_id == exact_choice_id
                    ),
                    None,
                )
                if existing_record is not None:
                    if (
                        existing_record.desired_set_ref != desired_set_ref
                        or existing_record.capability_ids != exact_capability_ids
                        or existing_record.event.to_json().encode("utf-8") != event_bytes
                    ):
                        raise ManagedQueryStoreConflict(
                            "desired-set choice identity is already bound to different content"
                        )
                    connection.execute("COMMIT")
                    return existing_record
                stream_digest = cast(str, registration["stream_identity_digest"])
                latest_committed_revision: int | None = None
                same_query_records = tuple(
                    record
                    for record in records
                    if record.stream_identity_digest == stream_digest
                    and record.query_ref == exact_query_ref
                )
                previous_event_id = (
                    parent.decision_event.event_id
                    if not same_query_records
                    else max(
                        same_query_records,
                        key=lambda item: item.event.expected_revision,
                    ).event.event_id
                )
                for other in records:
                    if other.stream_identity_digest != stream_digest:
                        continue
                    if other.event.expected_revision == event.expected_revision:
                        raise ManagedQueryStoreConflict(
                            "desired-set stream revision is already reserved"
                        )
                    if not other.committed:
                        raise ManagedQueryStoreConflict(
                            "desired-set stream has a pending reservation"
                        )
                    if (
                        latest_committed_revision is None
                        or other.event.expected_revision > latest_committed_revision
                    ):
                        latest_committed_revision = other.event.expected_revision
                if event.causation_id != previous_event_id:
                    raise ManagedQueryStoreConflict(
                        "desired-set event causation does not follow the committed stream"
                    )
                if (
                    latest_committed_revision is not None
                    and event.expected_revision <= latest_committed_revision
                ):
                    raise ManagedQueryStoreConflict(
                        "desired-set revision does not follow the latest committed reservation"
                    )
                if len(records) == _MAX_DESIRED_SET_ROWS:
                    raise ManagedQueryStoreCapacityExceeded(
                        "managed desired-set store reached its bounded capacity"
                    )
                collision = connection.execute(
                    "SELECT query_ref FROM managed_desired_sets WHERE desired_set_ref = ?",
                    (desired_set_ref,),
                ).fetchone()
                if collision is not None:
                    raise ManagedQueryStoreConflict("derived desired-set reference collided")
                row_auth = _desired_set_row_hmac(
                    self._installation_hmac_key,
                    desired_set_ref=desired_set_ref,
                    registration=registration,
                    event_bytes=event_bytes,
                    capability_ids_bytes=capability_ids_bytes,
                    journal_revision=None,
                    journal_record_digest=None,
                    transition_digest=None,
                )
                connection.execute(
                    "INSERT INTO managed_desired_sets ("
                    "query_ref, logical_choice_id, desired_set_ref, plan_id, decision_digest, "
                    "stream_identity_digest, expected_revision, event_id, event_json, event_bytes, "
                    "capability_ids_json, capability_ids_bytes, journal_revision, "
                    "journal_record_digest, transition_digest, row_hmac"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
                    (
                        exact_query_ref,
                        exact_choice_id,
                        desired_set_ref,
                        parent.plan_id,
                        parent.decision_digest,
                        stream_digest,
                        event.expected_revision,
                        event.event_id,
                        event_bytes,
                        len(event_bytes),
                        capability_ids_bytes,
                        len(capability_ids_bytes),
                        row_auth,
                    ),
                )
                inserted = connection.execute(
                    f"SELECT {_DESIRED_SET_SELECT_COLUMNS} FROM managed_desired_sets "
                    "WHERE desired_set_ref = ?",
                    (desired_set_ref,),
                ).fetchone()
                if inserted is None:
                    raise ManagedQueryStoreCorruption("desired-set reservation was not durable")
                result = self._validated_desired_set_row(inserted, parent=parent)
                _require_connection_size_bound(connection)
                connection.execute("COMMIT")
                return result
            except sqlite3.DatabaseError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if _sqlite_capacity_error(exc):
                    raise ManagedQueryStoreCapacityExceeded(
                        "managed query database reached its size bound"
                    ) from None
                raise
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def load_desired_set(self, desired_set_ref: str) -> ManagedDesiredSetRecord:
        exact_ref = _desired_set_ref(desired_set_ref)
        with self._locked_connection() as connection:
            records = self._validated_all_desired_set_rows(connection)
            record = next(
                (item for item in records if item.desired_set_ref == exact_ref),
                None,
            )
            if record is None:
                raise ManagedQueryStoreNotFound("managed desired-set record is unavailable")
            return record

    def load_latest_desired_set(self, query_ref: str) -> ManagedDesiredSetRecord:
        exact_query_ref = _query_ref(query_ref)
        with self._locked_connection() as connection:
            self._load_parent_by_ref(connection, exact_query_ref)
            records = tuple(
                record
                for record in self._validated_all_desired_set_rows(connection)
                if record.query_ref == exact_query_ref
            )
        if not records:
            raise ManagedQueryStoreNotFound("managed desired-set record is unavailable")
        latest_revision = max(record.event.expected_revision for record in records)
        latest = tuple(
            record for record in records if record.event.expected_revision == latest_revision
        )
        if len(latest) != 1:
            raise ManagedQueryStoreCorruption("managed desired-set latest lookup is ambiguous")
        return latest[0]

    def load_pending_desired_set(self, scope: ScopeRef) -> ManagedDesiredSetRecord:
        exact_scope = _safe_scope(scope)
        stream_digest = _stream_identity_digest(exact_scope)
        with self._locked_connection() as connection:
            records = [
                record
                for record in self._validated_all_desired_set_rows(connection)
                if record.stream_identity_digest == stream_digest
            ]
        pending = tuple(record for record in records if not record.committed)
        if not pending:
            raise ManagedQueryStoreNotFound("managed desired-set pending record is unavailable")
        if len(pending) != 1:
            raise ManagedQueryStoreCorruption("managed desired-set pending lookup is ambiguous")
        return pending[0]

    def mark_desired_set_committed(
        self,
        desired_set_ref: str,
        *,
        journal_revision: int,
        journal_record_digest: str,
        transition_digest: str,
    ) -> ManagedDesiredSetRecord:
        exact_ref = _desired_set_ref(desired_set_ref)
        exact_record_digest = _digest(journal_record_digest, "journal_record_digest")
        exact_transition_digest = _digest(transition_digest, "transition_digest")
        if type(journal_revision) is not int or not 1 <= journal_revision <= _MAX_SQLITE_INTEGER:
            raise ValueError("journal_revision must be a bounded positive integer")
        desired = (journal_revision, exact_record_digest, exact_transition_digest)
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                records = self._validated_all_desired_set_rows(connection)
                record = next(
                    (item for item in records if item.desired_set_ref == exact_ref),
                    None,
                )
                if record is None:
                    raise ManagedQueryStoreNotFound("managed desired-set record is unavailable")
                parent = self._load_parent_by_ref(
                    connection,
                    record.query_ref,
                    persisted_binding=True,
                )
                if journal_revision != record.event.expected_revision + 1:
                    raise ManagedQueryStoreConflict(
                        "desired-set journal revision does not match its exact event"
                    )
                current = (
                    record.journal_revision,
                    record.journal_record_digest,
                    record.transition_digest,
                )
                if record.committed:
                    if current != desired:
                        raise ManagedQueryStoreConflict(
                            "desired-set commit is already bound differently"
                        )
                    connection.execute("COMMIT")
                    return record
                registration, event_bytes, capability_ids_bytes = (
                    _validated_desired_set_registration(
                        parent=parent,
                        logical_choice_id=record.logical_choice_id,
                        capability_ids=record.capability_ids,
                        event=record.event,
                    )
                )
                row_auth = _desired_set_row_hmac(
                    self._installation_hmac_key,
                    desired_set_ref=exact_ref,
                    registration=registration,
                    event_bytes=event_bytes,
                    capability_ids_bytes=capability_ids_bytes,
                    journal_revision=journal_revision,
                    journal_record_digest=exact_record_digest,
                    transition_digest=exact_transition_digest,
                )
                connection.execute(
                    "UPDATE managed_desired_sets SET journal_revision = ?, "
                    "journal_record_digest = ?, transition_digest = ?, row_hmac = ? "
                    "WHERE desired_set_ref = ?",
                    (*desired, row_auth, exact_ref),
                )
                updated = connection.execute(
                    f"SELECT {_DESIRED_SET_SELECT_COLUMNS} FROM managed_desired_sets "
                    "WHERE desired_set_ref = ?",
                    (exact_ref,),
                ).fetchone()
                if updated is None:
                    raise ManagedQueryStoreCorruption("desired-set commit disappeared")
                result = self._validated_desired_set_row(updated, parent=parent)
                _require_connection_size_bound(connection)
                connection.execute("COMMIT")
                return result
            except sqlite3.DatabaseError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                if _sqlite_capacity_error(exc):
                    raise ManagedQueryStoreCapacityExceeded(
                        "managed query database reached its size bound"
                    ) from None
                raise
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _load_parent_by_ref(
        self,
        connection: sqlite3.Connection,
        query_ref: object,
        *,
        persisted_binding: bool = False,
    ) -> ManagedQueryRecord:
        try:
            exact_ref = _query_ref(query_ref)
        except ValueError as exc:
            raise ManagedQueryStoreCorruption(
                "desired-set parent query reference is invalid"
            ) from exc
        row = connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM managed_queries WHERE query_ref = ?",
            (exact_ref,),
        ).fetchone()
        if row is None:
            if persisted_binding:
                raise ManagedQueryStoreCorruption(
                    "desired-set persisted parent query is unavailable"
                )
            raise ManagedQueryStoreNotFound("desired-set parent query is unavailable")
        return self._validated_row(row)

    def _validated_desired_set_row(
        self,
        row: sqlite3.Row,
        *,
        parent: ManagedQueryRecord,
    ) -> ManagedDesiredSetRecord:
        values = {name: row[name] for name in _DESIRED_SET_ROW_COLUMNS}
        event_value = values["event_json"]
        ids_value = values["capability_ids_json"]
        if not isinstance(event_value, (bytes, bytearray, memoryview)) or not isinstance(
            ids_value,
            (bytes, bytearray, memoryview),
        ):
            raise ManagedQueryStoreCorruption("desired-set persisted bytes are invalid")
        event_bytes = bytes(event_value)
        capability_ids_bytes = bytes(ids_value)
        event_length = values["event_bytes"]
        ids_length = values["capability_ids_bytes"]
        if (
            type(event_length) is not int
            or event_length != len(event_bytes)
            or not 1 <= event_length <= _MAX_DESIRED_SET_EVENT_BYTES
            or type(ids_length) is not int
            or ids_length != len(capability_ids_bytes)
        ):
            raise ManagedQueryStoreCorruption("desired-set persisted byte lengths are invalid")
        try:
            event = EngineEvent.from_json(event_bytes)
        except (TypeError, ValueError) as exc:
            raise ManagedQueryStoreCorruption("desired-set persisted event is invalid") from exc
        if event.to_json().encode("utf-8") != event_bytes:
            raise ManagedQueryStoreCorruption("desired-set persisted event is noncanonical")
        capability_ids = _decode_capability_ids(capability_ids_bytes)
        logical_choice_id = values["logical_choice_id"]
        try:
            registration, exact_event_bytes, exact_ids_bytes = _validated_desired_set_registration(
                parent=parent,
                logical_choice_id=logical_choice_id,
                capability_ids=capability_ids,
                event=event,
            )
        except (ManagedQueryStoreConflict, TypeError, ValueError) as exc:
            raise ManagedQueryStoreCorruption(
                "desired-set persisted registration is invalid"
            ) from exc
        desired_set_ref = values["desired_set_ref"]
        expected_ref = _derive_desired_set_ref(self._installation_hmac_key, registration)
        if not isinstance(desired_set_ref, str) or not hmac.compare_digest(
            desired_set_ref,
            expected_ref,
        ):
            raise ManagedQueryStoreCorruption("desired-set reference authentication failed")
        expected_redundant = (
            parent.query_ref,
            cast(str, registration["plan_id"]),
            cast(str, registration["decision_digest"]),
            cast(str, registration["stream_identity_digest"]),
            event.expected_revision,
            event.event_id,
        )
        observed_redundant = (
            values["query_ref"],
            values["plan_id"],
            values["decision_digest"],
            values["stream_identity_digest"],
            values["expected_revision"],
            values["event_id"],
        )
        if observed_redundant != expected_redundant:
            raise ManagedQueryStoreCorruption("desired-set redundant binding is invalid")
        if exact_event_bytes != event_bytes or exact_ids_bytes != capability_ids_bytes:
            raise ManagedQueryStoreCorruption("desired-set persisted bytes are not exact")
        lifecycle = (
            values["journal_revision"],
            values["journal_record_digest"],
            values["transition_digest"],
        )
        if any(value is None for value in lifecycle) and any(
            value is not None for value in lifecycle
        ):
            raise ManagedQueryStoreCorruption("desired-set commit is partial")
        journal_revision: int | None = None
        journal_record_digest: str | None = None
        transition_digest: str | None = None
        if all(value is not None for value in lifecycle):
            if type(lifecycle[0]) is not int or lifecycle[0] != event.expected_revision + 1:
                raise ManagedQueryStoreCorruption("desired-set committed revision is invalid")
            journal_revision = lifecycle[0]
            try:
                journal_record_digest = _digest(
                    lifecycle[1],
                    "journal_record_digest",
                )
                transition_digest = _digest(lifecycle[2], "transition_digest")
            except ValueError as exc:
                raise ManagedQueryStoreCorruption("desired-set commit digest is invalid") from exc
        supplied_hmac = values["row_hmac"]
        expected_hmac = _desired_set_row_hmac(
            self._installation_hmac_key,
            desired_set_ref=desired_set_ref,
            registration=registration,
            event_bytes=event_bytes,
            capability_ids_bytes=capability_ids_bytes,
            journal_revision=journal_revision,
            journal_record_digest=journal_record_digest,
            transition_digest=transition_digest,
        )
        if not isinstance(supplied_hmac, str) or not hmac.compare_digest(
            supplied_hmac,
            expected_hmac,
        ):
            raise ManagedQueryStoreCorruption("desired-set row authentication failed")
        return ManagedDesiredSetRecord._create(
            factory_token=_DESIRED_SET_RECORD_FACTORY_TOKEN,
            query_ref=parent.query_ref,
            logical_choice_id=cast(str, logical_choice_id),
            desired_set_ref=desired_set_ref,
            plan_id=cast(str, registration["plan_id"]),
            decision_digest=cast(str, registration["decision_digest"]),
            stream_identity_digest=cast(str, registration["stream_identity_digest"]),
            capability_ids=capability_ids,
            event=event,
            journal_revision=journal_revision,
            journal_record_digest=journal_record_digest,
            transition_digest=transition_digest,
        )

    def _validated_row(self, row: sqlite3.Row) -> ManagedQueryRecord:
        values = {name: row[name] for name in _ROW_COLUMNS}
        payload_value = values["registration_json"]
        if not isinstance(payload_value, (bytes, bytearray, memoryview)):
            raise ManagedQueryStoreCorruption("managed query registration storage is invalid")
        payload = bytes(payload_value)
        byte_length = values["registration_bytes"]
        if type(byte_length) is not int or byte_length != len(payload):
            raise ManagedQueryStoreCorruption("managed query registration length is invalid")
        registration = _decode_canonical_mapping(payload)
        registration, started, decision = _registration_from_mapping(registration)
        logical_query_id = registration["logical_query_id"]
        query_ref = values["query_ref"]
        if values["logical_query_id"] != logical_query_id:
            raise ManagedQueryStoreCorruption("managed query logical identity is invalid")
        if not isinstance(query_ref, str) or not hmac.compare_digest(
            query_ref,
            _derive_query_ref(self._installation_hmac_key, payload),
        ):
            raise ManagedQueryStoreCorruption("managed query reference authentication failed")
        lifecycle = (
            values["plan_id"],
            values["decision_digest"],
            values["journal_revision"],
            values["journal_record_digest"],
        )
        if any(value is None for value in lifecycle) and any(
            value is not None for value in lifecycle
        ):
            raise ManagedQueryStoreCorruption("managed query completion is partial")
        plan_id: str | None = None
        decision_digest: str | None = None
        journal_revision: int | None = None
        journal_record_digest: str | None = None
        if all(value is not None for value in lifecycle):
            try:
                plan_id = _token(lifecycle[0], "plan_id")
                decision_digest = _digest(lifecycle[1], "decision_digest")
                journal_record_digest = _digest(lifecycle[3], "journal_record_digest")
            except ValueError as exc:
                raise ManagedQueryStoreCorruption(
                    "managed query completion binding is invalid"
                ) from exc
            journal_revision = lifecycle[2] if type(lifecycle[2]) is int else None
            if (
                plan_id != decision.correlation_id
                or journal_revision is None
                or journal_revision < 2
                or journal_revision != decision.expected_revision + 1
            ):
                raise ManagedQueryStoreCorruption(
                    "managed query completion relationship is invalid"
                )
        supplied_hmac = values["row_hmac"]
        expected_hmac = _row_hmac(
            self._installation_hmac_key,
            query_ref=query_ref,
            registration=registration,
            plan_id=plan_id,
            decision_digest=decision_digest,
            journal_revision=journal_revision,
            journal_record_digest=journal_record_digest,
        )
        if not isinstance(supplied_hmac, str) or not hmac.compare_digest(
            supplied_hmac,
            expected_hmac,
        ):
            raise ManagedQueryStoreCorruption("managed query row authentication failed")
        return ManagedQueryRecord._create(
            factory_token=_RECORD_FACTORY_TOKEN,
            query_ref=query_ref,
            logical_query_id=cast(str, logical_query_id),
            session_started=started,
            decision_event=decision,
            artifact_manifest_digest=cast(str, registration["artifact_manifest_digest"]),
            planning_environment_digest=cast(
                str,
                registration["planning_environment_digest"],
            ),
            plan_id=plan_id,
            decision_digest=decision_digest,
            journal_revision=journal_revision,
            journal_record_digest=journal_record_digest,
        )

    @contextmanager
    def _locked_connection(self) -> Iterator[sqlite3.Connection]:
        with secure_file_lock(self._path, timeout=_BUSY_TIMEOUT_MS / 1000):
            self._assert_bound_path()
            with _connect(
                self._path,
                self._installation_hmac_key,
                initialize=False,
            ) as connection:
                yield connection
            self._assert_bound_path()

    def _assert_bound_path(self) -> None:
        current = _require_private_file(self._path)
        if (current.st_dev, current.st_ino) != self._bound_identity:
            raise ManagedQueryStoreCorruption(
                "managed query database changed since its authenticated binding"
            )

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed query stores are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed query stores are immutable")

    def __repr__(self) -> str:
        return "ManagedQueryStore(schema='ctx.managed-query-store-v2')"

    def __copy__(self) -> NoReturn:
        raise TypeError("managed query stores cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed query stores cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed query stores cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed query stores cannot be serialized")


def _registration_mapping_from_record(record: ManagedQueryRecord) -> dict[str, object]:
    return {
        "artifact_manifest_digest": record.artifact_manifest_digest,
        "decision_event": record.decision_event.to_dict(),
        "logical_query_id": record.logical_query_id,
        "planning_environment_digest": record.planning_environment_digest,
        "schema": _REGISTRATION_SCHEMA,
        "session_started": record.session_started.to_dict(),
    }


def _key_check(key: bytes) -> str:
    return hmac.digest(key, _KEY_CHECK_DOMAIN, "sha256").hex()


def _require_private_directory(path: Path) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ManagedQueryStoreUnavailable("managed query parent is unavailable")
    if os.name != "nt" and (
        metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise ManagedQueryStoreUnavailable("managed query parent is not owner-private")
    return metadata


def _ensure_durable_secure_directory_tree(path: Path) -> None:
    if os.name == "nt":
        ensure_secure_directory(path)
        return
    current = Path(path.anchor)
    missing: list[Path] = []
    for part in path.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            missing.append(current)
    ensure_secure_directory(path)
    for directory in missing:
        parent_fd = os.open(
            directory.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _require_private_file(
    path: Path,
    *,
    allowed_links: frozenset[int] = frozenset({1}),
) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink not in allowed_links
    ):
        raise ManagedQueryStoreCorruption("managed query database type is invalid")
    if os.name != "nt" and (
        metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE
    ):
        raise ManagedQueryStoreCorruption("managed query database is not owner-private")
    return metadata


def _authenticate_sidecars_unchecked(path: Path) -> dict[str, os.stat_result]:
    authenticated: dict[str, os.stat_result] = {}
    for suffix in _SQLITE_SIDECARS:
        candidate = Path(f"{path}{suffix}")
        if not os.path.lexists(candidate):
            continue
        reject_symlink_path(candidate)
        metadata = candidate.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or (
                os.name != "nt"
                and (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE
                )
            )
        ):
            raise ManagedQueryStoreCorruption("managed query SQLite sidecar is unsafe")
        descriptor = os.open(
            candidate,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            opened = os.fstat(descriptor)
            current = candidate.stat(follow_symlinks=False)
            if not os.path.samestat(metadata, opened) or not os.path.samestat(opened, current):
                raise ManagedQueryStoreCorruption(
                    "managed query SQLite sidecar changed during authentication"
                )
        finally:
            os.close(descriptor)
        authenticated[suffix] = metadata
    return authenticated


def _authenticate_sidecars(path: Path) -> dict[str, os.stat_result]:
    try:
        return _authenticate_sidecars_unchecked(path)
    except ManagedQueryStoreError:
        raise
    except (OSError, ValueError):
        raise ManagedQueryStoreCorruption(
            "managed query SQLite sidecar authentication failed"
        ) from None


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _initialization_stage_pattern(path: Path) -> re.Pattern[str]:
    name_digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()
    return re.compile(rf"\A\.ctx-managed-query-init-{name_digest}-[0-9a-f]{{32}}\.stage\Z")


def _new_initialization_stage(path: Path) -> Path:
    name_digest = hashlib.sha256(path.name.encode("utf-8")).hexdigest()
    return path.parent / (f".ctx-managed-query-init-{name_digest}-{secrets.token_hex(16)}.stage")


def _remove_initialization_stage(stage: Path) -> None:
    for suffix in _SQLITE_SIDECARS:
        sidecar = Path(f"{stage}{suffix}")
        if not os.path.lexists(sidecar):
            continue
        _authenticate_sidecars(stage)
        sidecar.unlink()
    if os.path.lexists(stage):
        _require_private_file(stage, allowed_links=frozenset({1, 2}))
        stage.unlink()
    _fsync_directory(stage.parent)


def _reconcile_initialization_stages(path: Path) -> None:
    pattern = _initialization_stage_pattern(path)
    try:
        names = os.listdir(path.parent)
    except OSError as exc:
        raise ManagedQueryStoreUnavailable(
            "managed query initialization state is unavailable"
        ) from exc
    stages = tuple(sorted(path.parent / name for name in names if pattern.fullmatch(name)))
    for stage in stages:
        stage_metadata = _require_private_file(
            stage,
            allowed_links=frozenset({1, 2}),
        )
        if stage_metadata.st_nlink == 2:
            if not os.path.lexists(path):
                raise ManagedQueryStoreCorruption(
                    "managed query initialization link has no final database"
                )
            final_metadata = _require_private_file(
                path,
                allowed_links=frozenset({2}),
            )
            if not os.path.samestat(stage_metadata, final_metadata):
                raise ManagedQueryStoreCorruption("managed query initialization link is ambiguous")
        _remove_initialization_stage(stage)


def _create_initialization_stage(stage: Path) -> None:
    descriptor = os.open(
        stage,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        _PRIVATE_FILE_MODE,
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _require_private_file(stage)
    _fsync_directory(stage.parent)


def _publish_initialized_database(path: Path, key: bytes) -> None:
    if any(os.path.lexists(Path(f"{path}{suffix}")) for suffix in _SQLITE_SIDECARS):
        raise ManagedQueryStoreCorruption(
            "new managed query database has pre-existing SQLite sidecars"
        )
    stage = _new_initialization_stage(path)
    _create_initialization_stage(stage)
    try:
        with _connect(stage, key, initialize=True):
            pass
        descriptor = os.open(
            stage,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.link(stage, path, follow_symlinks=False)
        _fsync_directory(path.parent)
        stage_metadata = _require_private_file(
            stage,
            allowed_links=frozenset({2}),
        )
        final_metadata = _require_private_file(
            path,
            allowed_links=frozenset({2}),
        )
        if not os.path.samestat(stage_metadata, final_metadata):
            raise ManagedQueryStoreCorruption("managed query initialization publication changed")
        _remove_initialization_stage(stage)
    except BaseException:
        if os.path.lexists(stage):
            _remove_initialization_stage(stage)
        raise


def _schema_signature(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, tuple[str, int, int]]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in rows
    }


def _require_schema(connection: sqlite3.Connection, key: bytes) -> None:
    if connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
        raise ManagedQueryStoreCorruption("managed query schema version is invalid")
    check = connection.execute("PRAGMA quick_check").fetchone()
    if check is None or str(check[0]).lower() != "ok":
        raise ManagedQueryStoreCorruption("managed query database integrity check failed")
    objects = connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    observed = [(str(row["type"]), str(row["name"]), str(row["tbl_name"])) for row in objects]
    # SQLite's implicit unique index is intentionally authenticated as part of
    # the exact schema because query_ref must remain collision-free.
    if observed != _EXPECTED_OBJECTS:
        raise ManagedQueryStoreCorruption("managed query schema objects are invalid")
    if (
        _schema_signature(connection, "managed_query_store_identity") != _EXPECTED_IDENTITY_COLUMNS
        or _schema_signature(connection, "managed_queries") != _EXPECTED_QUERY_COLUMNS
        or _schema_signature(connection, "managed_desired_sets") != _EXPECTED_DESIRED_SET_COLUMNS
    ):
        raise ManagedQueryStoreCorruption("managed query schema columns are invalid")
    indexes = connection.execute("PRAGMA index_list(managed_queries)").fetchall()
    index_signature = {
        str(row["name"]): (int(row["unique"]), str(row["origin"])) for row in indexes
    }
    if index_signature != {
        "idx_managed_queries_plan_id": (0, "c"),
        "sqlite_autoindex_managed_queries_1": (1, "pk"),
        "sqlite_autoindex_managed_queries_2": (1, "u"),
    }:
        raise ManagedQueryStoreCorruption("managed query schema indexes are invalid")
    for index_name, expected_column in (
        ("idx_managed_queries_plan_id", "plan_id"),
        ("sqlite_autoindex_managed_queries_1", "logical_query_id"),
        ("sqlite_autoindex_managed_queries_2", "query_ref"),
    ):
        columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
        if [str(row["name"]) for row in columns] != [expected_column]:
            raise ManagedQueryStoreCorruption("managed query schema index columns are invalid")
    desired_indexes = connection.execute("PRAGMA index_list(managed_desired_sets)").fetchall()
    desired_index_signature = {
        str(row["name"]): (int(row["unique"]), str(row["origin"])) for row in desired_indexes
    }
    if desired_index_signature != {
        "idx_managed_desired_sets_query_revision": (0, "c"),
        "idx_managed_desired_sets_ref": (1, "c"),
        "idx_managed_desired_sets_stream_revision": (1, "c"),
        "idx_managed_desired_sets_stream_state": (0, "c"),
        "sqlite_autoindex_managed_desired_sets_1": (1, "pk"),
    }:
        raise ManagedQueryStoreCorruption("managed desired-set schema indexes are invalid")
    desired_index_columns = {
        "idx_managed_desired_sets_query_revision": ["query_ref", "expected_revision"],
        "idx_managed_desired_sets_ref": ["desired_set_ref"],
        "idx_managed_desired_sets_stream_revision": [
            "stream_identity_digest",
            "expected_revision",
        ],
        "idx_managed_desired_sets_stream_state": [
            "stream_identity_digest",
            "journal_revision",
        ],
        "sqlite_autoindex_managed_desired_sets_1": [
            "query_ref",
            "logical_choice_id",
        ],
    }
    for index_name, expected_columns in desired_index_columns.items():
        columns = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
        if [str(row["name"]) for row in columns] != expected_columns:
            raise ManagedQueryStoreCorruption(
                "managed desired-set schema index columns are invalid"
            )
    identities = connection.execute(
        "SELECT singleton, key_check FROM managed_query_store_identity"
    ).fetchall()
    if (
        len(identities) != 1
        or identities[0]["singleton"] != 1
        or not isinstance(identities[0]["key_check"], str)
        or not hmac.compare_digest(identities[0]["key_check"], _key_check(key))
    ):
        raise ManagedQueryStoreCorruption(
            "managed query installation key does not authenticate the store"
        )
    count = connection.execute("SELECT count(*) FROM managed_queries").fetchone()[0]
    if type(count) is not int or not 0 <= count <= _MAX_ROWS:
        raise ManagedQueryStoreCorruption("managed query store exceeds its row bound")
    desired_count = connection.execute("SELECT count(*) FROM managed_desired_sets").fetchone()[0]
    if type(desired_count) is not int or not 0 <= desired_count <= _MAX_DESIRED_SET_ROWS:
        raise ManagedQueryStoreCorruption("managed desired-set store exceeds its row bound")


def _connection_page_geometry(connection: sqlite3.Connection) -> tuple[int, int]:
    page_size_row = connection.execute("PRAGMA page_size").fetchone()
    page_count_row = connection.execute("PRAGMA page_count").fetchone()
    page_size = page_size_row[0] if page_size_row is not None else None
    page_count = page_count_row[0] if page_count_row is not None else None
    if (
        type(page_size) is not int
        or page_size <= 0
        or type(page_count) is not int
        or page_count < 0
    ):
        raise ManagedQueryStoreCorruption("managed query database page geometry is invalid")
    return page_size, page_count


def _require_connection_size_bound(connection: sqlite3.Connection) -> None:
    page_size, page_count = _connection_page_geometry(connection)
    if page_count > _MAX_DATABASE_BYTES // page_size:
        raise ManagedQueryStoreCapacityExceeded("managed query database exceeds its size bound")


def _configure_connection_size_bound(connection: sqlite3.Connection) -> None:
    page_size, page_count = _connection_page_geometry(connection)
    maximum_pages = _MAX_DATABASE_BYTES // page_size
    if maximum_pages < 1 or page_count > maximum_pages:
        raise ManagedQueryStoreCapacityExceeded("managed query database exceeds its size bound")
    applied_row = connection.execute(f"PRAGMA max_page_count = {maximum_pages}").fetchone()
    applied = applied_row[0] if applied_row is not None else None
    if type(applied) is not int or applied != maximum_pages:
        raise ManagedQueryStoreCorruption("managed query database page bound was not applied")
    _require_connection_size_bound(connection)


def _sqlite_capacity_error(exc: sqlite3.DatabaseError) -> bool:
    return (
        getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL or "full" in str(exc).lower()
    )


@contextmanager
def _connect(
    path: Path,
    key: bytes,
    *,
    initialize: bool,
) -> Iterator[sqlite3.Connection]:
    reject_symlink_path(path)
    _require_private_directory(path.parent)
    before = _require_private_file(path)
    sidecars_before = _authenticate_sidecars(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        if initialize:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT INTO managed_query_store_identity (singleton, key_check) VALUES (1, ?)",
                (_key_check(key),),
            )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        _require_schema(connection, key)
        _configure_connection_size_bound(connection)
        after = _require_private_file(path)
        if not os.path.samestat(before, after):
            raise ManagedQueryStoreCorruption("managed query database changed while opening")
        sidecars_after = _authenticate_sidecars(path)
        for suffix in sidecars_before.keys() & sidecars_after.keys():
            if not os.path.samestat(sidecars_before[suffix], sidecars_after[suffix]):
                raise ManagedQueryStoreCorruption(
                    "managed query SQLite sidecar changed while opening"
                )
        total = sum(
            candidate.stat(follow_symlinks=False).st_size
            for candidate in (path, *(Path(f"{path}{suffix}") for suffix in _SQLITE_SIDECARS))
            if os.path.lexists(candidate)
        )
        if total > _MAX_DATABASE_BYTES:
            raise ManagedQueryStoreCapacityExceeded("managed query database exceeds its size bound")
        yield connection
        _require_connection_size_bound(connection)
    except ManagedQueryStoreError:
        raise
    except sqlite3.DatabaseError as exc:
        raise ManagedQueryStoreCorruption("managed query database is unreadable") from exc
    except OSError as exc:
        raise ManagedQueryStoreUnavailable("managed query filesystem is unavailable") from exc
    finally:
        if connection is not None:
            connection.close()
        _authenticate_sidecars(path)


def open_managed_query_store(
    *,
    path: Path,
    installation_hmac_key: bytes,
) -> ManagedQueryStore:
    """Open one owner-private store bound to an exact installation HMAC key."""

    if not isinstance(path, Path):
        raise TypeError("path must be a Path")
    if not path.is_absolute():
        raise ValueError("managed query database path must be absolute")
    if type(installation_hmac_key) is not bytes or len(installation_hmac_key) != 32:
        raise ValueError("installation_hmac_key must be exactly 32 bytes")
    exact_path = Path(os.path.abspath(path))
    try:
        reject_symlink_path(exact_path)
        _ensure_durable_secure_directory_tree(exact_path.parent)
        reject_symlink_path(exact_path)
        _require_private_directory(exact_path.parent)
        with secure_file_lock(exact_path, timeout=_BUSY_TIMEOUT_MS / 1000):
            _reconcile_initialization_stages(exact_path)
            if not os.path.lexists(exact_path):
                _publish_initialized_database(exact_path, installation_hmac_key)
            with _connect(exact_path, installation_hmac_key, initialize=False):
                pass
            metadata = _require_private_file(exact_path)
    except ManagedQueryStoreError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ManagedQueryStoreUnavailable(
            "managed query store could not be opened securely"
        ) from exc
    return ManagedQueryStore._create(
        factory_token=_FACTORY_TOKEN,
        path=exact_path,
        installation_hmac_key=bytes(installation_hmac_key),
        bound_identity=(metadata.st_dev, metadata.st_ino),
    )


__all__ = [
    "ManagedDesiredSetRecord",
    "ManagedQueryRecord",
    "ManagedQueryStore",
    "ManagedQueryStoreCapacityExceeded",
    "ManagedQueryStoreConflict",
    "ManagedQueryStoreCorruption",
    "ManagedQueryStoreError",
    "ManagedQueryStoreNotFound",
    "ManagedQueryStoreUnavailable",
    "open_managed_query_store",
]
