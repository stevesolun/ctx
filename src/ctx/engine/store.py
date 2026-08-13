"""Authoritative SQLite journal for the unified CTX capability engine.

The store owns durability, optimistic revision checks, event-id idempotency,
and rebuildable projection bytes.  It deliberately knows nothing about reducer
or planner semantics: replay inputs, transitions, and states cross this seam as
canonical JSON strings.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Self

from ctx.engine.protocol import (
    INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    HostAction,
    PrivacyLabel,
    ScopeRef,
    Transition,
)
from ctx.utils._fs_utils import ensure_secure_directory, reject_symlink_path

if TYPE_CHECKING:
    from ctx.engine.installation import (
        CommittedInstallDecisionEvidence,
        InstallDecisionEvidenceLookup,
        InstallDecisionEvidenceQuery,
    )


_PRIVATE_FILE_MODE = 0o600
_BUSY_TIMEOUT_MS = 30_000
_SQLITE_SIDECAR_SECURE_ATTEMPTS = 16

_STREAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_streams (
    stream_key          TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    workspace_id        TEXT NOT NULL,
    repository_id       TEXT NOT NULL,
    session_id          TEXT NOT NULL,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    state_json          TEXT NOT NULL,
    state_digest        TEXT NOT NULL,
    head_record_digest  TEXT NOT NULL
);
"""

_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_journal (
    event_id                TEXT PRIMARY KEY,
    stream_key              TEXT NOT NULL,
    revision                INTEGER NOT NULL CHECK (revision >= 1),
    event_content_digest    TEXT NOT NULL,
    replay_json             TEXT NOT NULL,
    replay_digest           TEXT NOT NULL,
    transition_json         TEXT NOT NULL,
    transition_digest       TEXT NOT NULL,
    result_state_json       TEXT NOT NULL,
    result_state_digest     TEXT NOT NULL,
    previous_record_digest  TEXT,
    record_digest           TEXT NOT NULL,
    privacy_classification  TEXT NOT NULL,
    retention_class         TEXT NOT NULL,
    reducer_version         TEXT NOT NULL,
    UNIQUE (stream_key, revision)
);
"""

_JOURNAL_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_engine_journal_stream_revision
    ON engine_journal(stream_key, revision);
"""

_INSTALL_CLAIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_install_claims (
    stream_key                  TEXT NOT NULL,
    action_id                   TEXT NOT NULL,
    action_content_digest       TEXT NOT NULL,
    action_kind                 TEXT NOT NULL,
    precondition_revision       INTEGER NOT NULL CHECK (precondition_revision >= 1),
    action_json                 TEXT NOT NULL,
    issuing_record_digest       TEXT NOT NULL,
    claimed_head_revision       INTEGER NOT NULL CHECK (claimed_head_revision >= 1),
    claimed_head_record_digest  TEXT NOT NULL,
    claimed_head_state_digest   TEXT NOT NULL,
    action_expires_at           TEXT NOT NULL,
    claimed_at                  TEXT NOT NULL,
    authorization_digest        TEXT NOT NULL,
    execution_binding_json      TEXT NOT NULL,
    execution_binding_digest    TEXT NOT NULL,
    claim_digest                TEXT NOT NULL,
    PRIMARY KEY (stream_key, action_id)
);
"""

_INSTALL_OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_install_outcomes (
    stream_key                         TEXT NOT NULL,
    action_id                          TEXT NOT NULL,
    action_content_digest              TEXT NOT NULL,
    claim_digest                       TEXT NOT NULL,
    execution_binding_digest           TEXT NOT NULL,
    outcome                            TEXT NOT NULL CHECK (outcome IN ('applied', 'failed')),
    observed_material_identity_digest  TEXT,
    verification_digest                TEXT NOT NULL,
    observed_at                        TEXT NOT NULL,
    outcome_digest                     TEXT NOT NULL,
    PRIMARY KEY (stream_key, action_id)
);
"""

_INSTALL_SETTLEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_install_claim_settlements (
    stream_key                    TEXT NOT NULL,
    action_id                     TEXT NOT NULL,
    action_content_digest         TEXT NOT NULL,
    claim_digest                  TEXT NOT NULL,
    outcome                       TEXT NOT NULL CHECK (outcome IN ('applied', 'failed')),
    receipt_event_id              TEXT NOT NULL,
    receipt_event_content_digest  TEXT NOT NULL,
    receipt_record_digest         TEXT NOT NULL,
    settlement_digest             TEXT NOT NULL,
    PRIMARY KEY (stream_key, action_id)
);
"""

_ACTIVATION_CLAIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_activation_claims (
    stream_key                  TEXT NOT NULL,
    action_id                   TEXT NOT NULL,
    action_content_digest       TEXT NOT NULL,
    precondition_revision       INTEGER NOT NULL CHECK (precondition_revision >= 1),
    action_json                 TEXT NOT NULL,
    issuing_record_digest       TEXT NOT NULL,
    claimed_head_revision       INTEGER NOT NULL CHECK (claimed_head_revision >= 1),
    claimed_head_record_digest  TEXT NOT NULL,
    claimed_head_state_digest   TEXT NOT NULL,
    action_expires_at           TEXT NOT NULL,
    claimed_at                  TEXT NOT NULL,
    authorization_digest        TEXT NOT NULL,
    execution_binding_json      TEXT NOT NULL,
    execution_binding_digest    TEXT NOT NULL,
    claim_digest                TEXT NOT NULL,
    PRIMARY KEY (stream_key, action_id)
);
"""

_ACTIVATION_OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_activation_outcomes (
    stream_key                         TEXT NOT NULL,
    action_id                          TEXT NOT NULL,
    action_content_digest              TEXT NOT NULL,
    claim_digest                       TEXT NOT NULL,
    execution_binding_digest           TEXT NOT NULL,
    observed_material_identity_digest  TEXT NOT NULL,
    verification_digest                TEXT NOT NULL,
    observed_at                        TEXT NOT NULL,
    outcome_digest                     TEXT NOT NULL,
    PRIMARY KEY (stream_key, action_id)
);
"""

_ACTIVATION_SETTLEMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_activation_claim_settlements (
    stream_key                    TEXT NOT NULL,
    action_id                     TEXT NOT NULL,
    action_content_digest         TEXT NOT NULL,
    claim_digest                  TEXT NOT NULL,
    outcome_digest                TEXT NOT NULL,
    receipt_event_id              TEXT NOT NULL,
    receipt_event_content_digest  TEXT NOT NULL,
    receipt_record_digest         TEXT NOT NULL,
    settlement_digest             TEXT NOT NULL,
    PRIMARY KEY (stream_key, action_id)
);
"""

_SCHEMA = (
    _STREAM_SCHEMA
    + _JOURNAL_SCHEMA
    + _JOURNAL_INDEX_SCHEMA
    + _INSTALL_CLAIM_SCHEMA
    + _INSTALL_OUTCOME_SCHEMA
    + _INSTALL_SETTLEMENT_SCHEMA
    + _ACTIVATION_CLAIM_SCHEMA
    + _ACTIVATION_OUTCOME_SCHEMA
    + _ACTIVATION_SETTLEMENT_SCHEMA
)
_EXACT_SCHEMA_OBJECTS = {
    ("index", "idx_engine_journal_stream_revision"): (
        "engine_journal",
        _JOURNAL_INDEX_SCHEMA,
    ),
    ("table", "engine_activation_claim_settlements"): (
        "engine_activation_claim_settlements",
        _ACTIVATION_SETTLEMENT_SCHEMA,
    ),
    ("table", "engine_activation_claims"): (
        "engine_activation_claims",
        _ACTIVATION_CLAIM_SCHEMA,
    ),
    ("table", "engine_activation_outcomes"): (
        "engine_activation_outcomes",
        _ACTIVATION_OUTCOME_SCHEMA,
    ),
    ("table", "engine_install_claim_settlements"): (
        "engine_install_claim_settlements",
        _INSTALL_SETTLEMENT_SCHEMA,
    ),
    ("table", "engine_install_claims"): (
        "engine_install_claims",
        _INSTALL_CLAIM_SCHEMA,
    ),
    ("table", "engine_install_outcomes"): (
        "engine_install_outcomes",
        _INSTALL_OUTCOME_SCHEMA,
    ),
    ("table", "engine_journal"): ("engine_journal", _JOURNAL_SCHEMA),
    ("table", "engine_streams"): ("engine_streams", _STREAM_SCHEMA),
}
_JOURNAL_COLUMNS = """
event_id, stream_key, revision, event_content_digest,
replay_json, replay_digest, transition_json, transition_digest,
result_state_json, result_state_digest, previous_record_digest,
record_digest, privacy_classification, retention_class, reducer_version
"""
_PROJECTION_SIGNATURE = {
    "stream_key": ("TEXT", 0, 1),
    "tenant_id": ("TEXT", 1, 0),
    "workspace_id": ("TEXT", 1, 0),
    "repository_id": ("TEXT", 1, 0),
    "session_id": ("TEXT", 1, 0),
    "revision": ("INTEGER", 1, 0),
    "state_json": ("TEXT", 1, 0),
    "state_digest": ("TEXT", 1, 0),
    "head_record_digest": ("TEXT", 1, 0),
}
_INSTALL_CLAIM_SIGNATURE = {
    "stream_key": ("TEXT", 1, 1),
    "action_id": ("TEXT", 1, 2),
    "action_content_digest": ("TEXT", 1, 0),
    "action_kind": ("TEXT", 1, 0),
    "precondition_revision": ("INTEGER", 1, 0),
    "action_json": ("TEXT", 1, 0),
    "issuing_record_digest": ("TEXT", 1, 0),
    "claimed_head_revision": ("INTEGER", 1, 0),
    "claimed_head_record_digest": ("TEXT", 1, 0),
    "claimed_head_state_digest": ("TEXT", 1, 0),
    "action_expires_at": ("TEXT", 1, 0),
    "claimed_at": ("TEXT", 1, 0),
    "authorization_digest": ("TEXT", 1, 0),
    "execution_binding_json": ("TEXT", 1, 0),
    "execution_binding_digest": ("TEXT", 1, 0),
    "claim_digest": ("TEXT", 1, 0),
}
_INSTALL_OUTCOME_SIGNATURE = {
    "stream_key": ("TEXT", 1, 1),
    "action_id": ("TEXT", 1, 2),
    "action_content_digest": ("TEXT", 1, 0),
    "claim_digest": ("TEXT", 1, 0),
    "execution_binding_digest": ("TEXT", 1, 0),
    "outcome": ("TEXT", 1, 0),
    "observed_material_identity_digest": ("TEXT", 0, 0),
    "verification_digest": ("TEXT", 1, 0),
    "observed_at": ("TEXT", 1, 0),
    "outcome_digest": ("TEXT", 1, 0),
}
_INSTALL_SETTLEMENT_SIGNATURE = {
    "stream_key": ("TEXT", 1, 1),
    "action_id": ("TEXT", 1, 2),
    "action_content_digest": ("TEXT", 1, 0),
    "claim_digest": ("TEXT", 1, 0),
    "outcome": ("TEXT", 1, 0),
    "receipt_event_id": ("TEXT", 1, 0),
    "receipt_event_content_digest": ("TEXT", 1, 0),
    "receipt_record_digest": ("TEXT", 1, 0),
    "settlement_digest": ("TEXT", 1, 0),
}
_ACTIVATION_CLAIM_SIGNATURE = {
    "stream_key": ("TEXT", 1, 1),
    "action_id": ("TEXT", 1, 2),
    "action_content_digest": ("TEXT", 1, 0),
    "precondition_revision": ("INTEGER", 1, 0),
    "action_json": ("TEXT", 1, 0),
    "issuing_record_digest": ("TEXT", 1, 0),
    "claimed_head_revision": ("INTEGER", 1, 0),
    "claimed_head_record_digest": ("TEXT", 1, 0),
    "claimed_head_state_digest": ("TEXT", 1, 0),
    "action_expires_at": ("TEXT", 1, 0),
    "claimed_at": ("TEXT", 1, 0),
    "authorization_digest": ("TEXT", 1, 0),
    "execution_binding_json": ("TEXT", 1, 0),
    "execution_binding_digest": ("TEXT", 1, 0),
    "claim_digest": ("TEXT", 1, 0),
}
_ACTIVATION_OUTCOME_SIGNATURE = {
    "stream_key": ("TEXT", 1, 1),
    "action_id": ("TEXT", 1, 2),
    "action_content_digest": ("TEXT", 1, 0),
    "claim_digest": ("TEXT", 1, 0),
    "execution_binding_digest": ("TEXT", 1, 0),
    "observed_material_identity_digest": ("TEXT", 1, 0),
    "verification_digest": ("TEXT", 1, 0),
    "observed_at": ("TEXT", 1, 0),
    "outcome_digest": ("TEXT", 1, 0),
}
_ACTIVATION_SETTLEMENT_SIGNATURE = {
    "stream_key": ("TEXT", 1, 1),
    "action_id": ("TEXT", 1, 2),
    "action_content_digest": ("TEXT", 1, 0),
    "claim_digest": ("TEXT", 1, 0),
    "outcome_digest": ("TEXT", 1, 0),
    "receipt_event_id": ("TEXT", 1, 0),
    "receipt_event_content_digest": ("TEXT", 1, 0),
    "receipt_record_digest": ("TEXT", 1, 0),
    "settlement_digest": ("TEXT", 1, 0),
}


class EngineStoreError(RuntimeError):
    """Base class for authoritative journal failures."""


class RevisionConflict(EngineStoreError):
    """The stream changed after the caller read its head."""

    def __init__(self, *, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"expected revision {expected}, found {actual}")


class EventIdCollision(EngineStoreError):
    """An event id is already bound to different canonical content."""

    def __init__(self, *, event_id: str, stored_digest: str, submitted_digest: str) -> None:
        self.event_id = event_id
        self.stored_digest = stored_digest
        self.submitted_digest = submitted_digest
        super().__init__(f"event id {event_id!r} is already bound to different content")


class JournalCorruption(EngineStoreError):
    """Persisted journal or projection bytes fail their integrity contract."""


class StoreBusy(EngineStoreError):
    """SQLite could not acquire its bounded write lock."""


class InstallActionAlreadyClaimed(EngineStoreError):
    """An install action's one-use authority has already been burned."""


class InstallActionClaimExpired(EngineStoreError):
    """An unclaimed install action reached its trusted expiry boundary."""


class InstallActionClaimRequired(EngineStoreError):
    """A receipt attempted to settle install work without a durable claim."""


class InstallActionClaimSettled(EngineStoreError):
    """An install claim already has an immutable terminal settlement."""


class InstallExecutionOutcomeRequired(EngineStoreError):
    """A driver-managed receipt or outcome has no exact durable predecessor."""


class InstallExecutionOutcomeConflict(EngineStoreError):
    """One claimed action is already bound to a different verified outcome."""


class ActivationActionAlreadyClaimed(EngineStoreError):
    """An activation action already has a durable verifier claim."""


class ActivationActionClaimExpired(EngineStoreError):
    """An unclaimed activation action expired before verifier claim."""


class ActivationExecutionOutcomeRequired(EngineStoreError):
    """An activation receipt has no durable verified outcome."""


class ActivationExecutionOutcomeConflict(EngineStoreError):
    """An activation action is bound to a different verifier outcome."""


class ActivationActionClaimSettled(EngineStoreError):
    """An activation claim already has a terminal receipt settlement."""


@dataclass(frozen=True, slots=True, kw_only=True)
class StreamId:
    """Revision authority for one current-work session.

    Exposure and host-context identifiers are intentionally absent: parent and
    child agents share one globally bounded belt within the session.
    """

    tenant_id: str
    workspace_id: str
    repository_id: str
    session_id: str

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "workspace_id", "repository_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field_name} must be a non-empty trimmed string")

    @classmethod
    def from_scope(cls, scope: ScopeRef) -> StreamId:
        if not isinstance(scope, ScopeRef):
            raise TypeError("scope must be a ScopeRef")
        return cls(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            repository_id=scope.repository_id,
            session_id=scope.session_id,
        )

    @property
    def key(self) -> str:
        return _canonical_json(
            {
                "repository_id": self.repository_id,
                "session_id": self.session_id,
                "tenant_id": self.tenant_id,
                "workspace_id": self.workspace_id,
            }
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class JournalRecord:
    """One committed replay input, deterministic output, and state projection."""

    stream_id: StreamId
    revision: int
    event_id: str
    event_content_digest: str
    replay_json: str
    transition_json: str
    result_state_json: str
    privacy_classification: str
    retention_class: str
    reducer_version: str
    replay_digest: str = ""
    transition_digest: str = ""
    result_state_digest: str = ""
    previous_record_digest: str | None = None
    record_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be an integer >= 1")
        for field_name in (
            "event_id",
            "privacy_classification",
            "retention_class",
            "reducer_version",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field_name} must be a non-empty trimmed string")
        PrivacyLabel(
            classification=self.privacy_classification,
            retention=self.retention_class,
        )
        _require_sha256(self.event_content_digest, "event_content_digest")
        for field_name in ("replay_json", "transition_json", "result_state_json"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _canonical_json_text(value) != value:
                raise ValueError(f"{field_name} must be canonical JSON")
        transition = Transition.from_json(self.transition_json)
        if transition.event_id != self.event_id or transition.to_revision != self.revision:
            raise ValueError("transition must match the record event id and revision")
        if StreamId.from_scope(transition.scope) != self.stream_id:
            raise ValueError("transition scope must belong to the record stream")
        for field_name, source in (
            ("replay_digest", self.replay_json),
            ("transition_digest", self.transition_json),
            ("result_state_digest", self.result_state_json),
        ):
            digest = _sha256(source)
            supplied = getattr(self, field_name)
            if supplied and supplied != digest:
                raise ValueError(f"{field_name} does not match canonical content")
            object.__setattr__(self, field_name, digest)
        if self.previous_record_digest is not None:
            _require_sha256(self.previous_record_digest, "previous_record_digest")
        if self.record_digest:
            _require_sha256(self.record_digest, "record_digest")

    def bind_chain(self, previous_record_digest: str | None) -> JournalRecord:
        if previous_record_digest is not None:
            _require_sha256(previous_record_digest, "previous_record_digest")
        bound = replace(
            self,
            previous_record_digest=previous_record_digest,
            record_digest="",
        )
        return replace(bound, record_digest=_sha256(_record_chain_json(bound)))


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredHead:
    stream_id: StreamId
    revision: int
    state_json: str | None
    state_digest: str | None
    projection_valid: bool
    record_digest: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CommitResult:
    committed: bool
    revision: int
    transition: Transition
    record: JournalRecord


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallActionClaimRequest:
    stream_id: StreamId
    expected_revision: int
    expected_head_record_digest: str
    action_json: str
    authorization_digest: str
    execution_binding_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("expected_revision must be an integer >= 1")
        _require_sha256(self.expected_head_record_digest, "expected_head_record_digest")
        if (
            not isinstance(self.action_json, str)
            or _canonical_json_text(self.action_json) != self.action_json
        ):
            raise ValueError("action_json must be canonical JSON")
        _require_sha256(self.authorization_digest, "authorization_digest")
        from ctx.engine.installation import InstallExecutionBinding

        InstallExecutionBinding.from_json(self.execution_binding_json)


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallActionClaimRecord:
    stream_id: StreamId
    action_id: str
    action_content_digest: str
    action_kind: str
    precondition_revision: int
    action_json: str
    issuing_record_digest: str
    claimed_head_revision: int
    claimed_head_record_digest: str
    claimed_head_state_digest: str
    action_expires_at: str
    claimed_at: str
    authorization_digest: str
    execution_binding_json: str
    execution_binding_digest: str
    claim_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if (
            not isinstance(self.action_id, str)
            or not self.action_id
            or self.action_id != self.action_id.strip()
        ):
            raise ValueError("action_id must be a non-empty trimmed string")
        for field_name in (
            "action_content_digest",
            "issuing_record_digest",
            "claimed_head_record_digest",
            "claimed_head_state_digest",
            "authorization_digest",
            "execution_binding_digest",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if self.action_kind != "InstallCapability":
            raise ValueError("action_kind must be InstallCapability")
        if type(self.precondition_revision) is not int or self.precondition_revision < 1:
            raise ValueError("precondition_revision must be an integer >= 1")
        if type(self.claimed_head_revision) is not int or self.claimed_head_revision < 1:
            raise ValueError("claimed_head_revision must be an integer >= 1")
        if self.claimed_head_revision < self.precondition_revision:
            raise ValueError("claimed head cannot precede the issuing revision")
        if _canonical_json_text(self.action_json) != self.action_json:
            raise ValueError("action_json must be canonical JSON")
        action = HostAction.from_json(self.action_json)
        if (
            action.action_id != self.action_id
            or action.content_digest != self.action_content_digest
        ):
            raise ValueError("claim action identity does not match action_json")
        if (
            action.kind != self.action_kind
            or action.precondition_revision != self.precondition_revision
        ):
            raise ValueError("claim action binding does not match action_json")
        if action.expires_at != self.action_expires_at:
            raise ValueError("claim action expiry does not match action_json")
        expires_at = _parse_utc_timestamp(self.action_expires_at, "action_expires_at")
        claimed_at = _parse_utc_timestamp(self.claimed_at, "claimed_at")
        if claimed_at >= expires_at:
            raise ValueError("claimed_at must precede action_expires_at")
        from ctx.engine.installation import InstallExecutionBinding

        binding = InstallExecutionBinding.from_json(self.execution_binding_json)
        if binding.binding_digest != self.execution_binding_digest:
            raise ValueError("execution binding digest does not match its canonical JSON")
        expected = _sha256(_install_claim_json(self))
        if self.claim_digest and self.claim_digest != expected:
            raise ValueError("claim_digest does not match claim content")
        object.__setattr__(self, "claim_digest", expected)


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallActionClaimGuard:
    action_id: str
    action_content_digest: str
    mode: Literal["applied", "failed", "expired"]
    execution_outcome_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.action_id, str)
            or not self.action_id
            or self.action_id != self.action_id.strip()
        ):
            raise ValueError("action_id must be a non-empty trimmed string")
        _require_sha256(self.action_content_digest, "action_content_digest")
        if self.mode not in {"applied", "failed", "expired"}:
            raise ValueError("mode must be applied, failed, or expired")
        if self.execution_outcome_digest is not None:
            _require_sha256(self.execution_outcome_digest, "execution_outcome_digest")
        if self.mode == "expired" and self.execution_outcome_digest is not None:
            raise ValueError("expired actions cannot carry an execution outcome")


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallExecutionOutcomeRequest:
    stream_id: StreamId
    action_json: str
    execution_binding_digest: str
    outcome: Literal["applied", "failed"]
    observed_material_identity_digest: str | None
    verification_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if _canonical_json_text(self.action_json) != self.action_json:
            raise ValueError("action_json must be canonical JSON")
        _require_sha256(self.execution_binding_digest, "execution_binding_digest")
        if self.outcome not in {"applied", "failed"}:
            raise ValueError("outcome must be applied or failed")
        if self.observed_material_identity_digest is not None:
            _require_sha256(
                self.observed_material_identity_digest,
                "observed_material_identity_digest",
            )
        if self.outcome == "applied" and self.observed_material_identity_digest is None:
            raise ValueError("applied outcome requires an observed material identity")
        if self.outcome == "failed" and self.observed_material_identity_digest is not None:
            raise ValueError("failed outcome requires verified material absence")
        _require_sha256(self.verification_digest, "verification_digest")


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallExecutionOutcomeRecord:
    stream_id: StreamId
    action_id: str
    action_content_digest: str
    claim_digest: str
    execution_binding_digest: str
    outcome: Literal["applied", "failed"]
    observed_material_identity_digest: str | None
    verification_digest: str
    observed_at: str
    outcome_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be non-empty text")
        for field_name in (
            "action_content_digest",
            "claim_digest",
            "execution_binding_digest",
            "verification_digest",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if self.outcome not in {"applied", "failed"}:
            raise ValueError("outcome must be applied or failed")
        if self.observed_material_identity_digest is not None:
            _require_sha256(
                self.observed_material_identity_digest,
                "observed_material_identity_digest",
            )
        if self.outcome == "applied" and self.observed_material_identity_digest is None:
            raise ValueError("applied outcome requires an observed material identity")
        if self.outcome == "failed" and self.observed_material_identity_digest is not None:
            raise ValueError("failed outcome requires verified material absence")
        _parse_utc_timestamp(self.observed_at, "observed_at")
        expected = _sha256(_install_outcome_json(self))
        if self.outcome_digest and self.outcome_digest != expected:
            raise ValueError("outcome_digest does not match outcome content")
        object.__setattr__(self, "outcome_digest", expected)

    @property
    def settlement_guard(self) -> InstallActionClaimGuard:
        return InstallActionClaimGuard(
            action_id=self.action_id,
            action_content_digest=self.action_content_digest,
            mode=self.outcome,
            execution_outcome_digest=self.outcome_digest,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallExecutionStatus:
    claimed: bool
    outcome_recorded: bool
    settled: bool
    execution_binding_digest: str | None
    outcome: Literal["applied", "failed"] | None
    outcome_digest: str | None
    observed_at: str | None

    def __post_init__(self) -> None:
        if not self.claimed and (
            self.outcome_recorded
            or self.settled
            or self.execution_binding_digest is not None
            or self.outcome is not None
            or self.outcome_digest is not None
            or self.observed_at is not None
        ):
            raise ValueError("unclaimed execution cannot have durable execution state")
        if self.execution_binding_digest is not None:
            _require_sha256(self.execution_binding_digest, "execution_binding_digest")
        if self.outcome_recorded != (self.outcome is not None):
            raise ValueError("outcome state is inconsistent")
        if self.outcome_recorded != (self.outcome_digest is not None):
            raise ValueError("outcome digest state is inconsistent")
        if self.outcome_recorded != (self.observed_at is not None):
            raise ValueError("outcome observation time state is inconsistent")
        if self.outcome_digest is not None:
            _require_sha256(self.outcome_digest, "outcome_digest")
        if self.observed_at is not None:
            _parse_utc_timestamp(self.observed_at, "observed_at")
        if self.settled and not self.outcome_recorded:
            raise ValueError("settled execution requires a verified outcome")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationActionClaimRequest:
    stream_id: StreamId
    expected_revision: int
    expected_head_record_digest: str
    action_json: str
    authorization_digest: str
    execution_binding_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("expected_revision must be an integer >= 1")
        _require_sha256(self.expected_head_record_digest, "expected_head_record_digest")
        if _canonical_json_text(self.action_json) != self.action_json:
            raise ValueError("action_json must be canonical JSON")
        action = HostAction.from_json(self.action_json)
        if (
            action.kind != "ActivateCapability"
            or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            raise ValueError("activation claim requires an exact schema-v3 action")
        _require_sha256(self.authorization_digest, "authorization_digest")
        from ctx.engine.installation import InstallExecutionBinding

        InstallExecutionBinding.from_json(self.execution_binding_json)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationActionClaimRecord:
    stream_id: StreamId
    action_id: str
    action_content_digest: str
    precondition_revision: int
    action_json: str
    issuing_record_digest: str
    claimed_head_revision: int
    claimed_head_record_digest: str
    claimed_head_state_digest: str
    action_expires_at: str
    claimed_at: str
    authorization_digest: str
    execution_binding_json: str
    execution_binding_digest: str
    claim_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be non-empty text")
        for field_name in (
            "action_content_digest",
            "issuing_record_digest",
            "claimed_head_record_digest",
            "claimed_head_state_digest",
            "authorization_digest",
            "execution_binding_digest",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if type(self.precondition_revision) is not int or self.precondition_revision < 1:
            raise ValueError("precondition_revision must be an integer >= 1")
        if type(self.claimed_head_revision) is not int or self.claimed_head_revision < 1:
            raise ValueError("claimed_head_revision must be an integer >= 1")
        if self.claimed_head_revision < self.precondition_revision:
            raise ValueError("claimed head cannot precede activation issuance")
        action = HostAction.from_json(self.action_json)
        if (
            action.kind != "ActivateCapability"
            or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
            or action.action_id != self.action_id
            or action.content_digest != self.action_content_digest
            or action.precondition_revision != self.precondition_revision
            or action.expires_at != self.action_expires_at
        ):
            raise ValueError("activation claim does not match action_json")
        claimed_at = _parse_utc_timestamp(self.claimed_at, "claimed_at")
        expires_at = _parse_utc_timestamp(self.action_expires_at, "action_expires_at")
        if claimed_at >= expires_at:
            raise ValueError("activation claim must precede action expiry")
        from ctx.engine.installation import InstallExecutionBinding

        binding = InstallExecutionBinding.from_json(self.execution_binding_json)
        if binding.binding_digest != self.execution_binding_digest:
            raise ValueError("activation execution binding digest does not match")
        expected = _sha256(_activation_claim_json(self))
        if self.claim_digest and self.claim_digest != expected:
            raise ValueError("activation claim_digest does not match claim content")
        object.__setattr__(self, "claim_digest", expected)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationExecutionOutcomeRequest:
    stream_id: StreamId
    action_json: str
    execution_binding_digest: str
    observed_material_identity_digest: str
    verification_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if _canonical_json_text(self.action_json) != self.action_json:
            raise ValueError("action_json must be canonical JSON")
        action = HostAction.from_json(self.action_json)
        if action.kind != "ActivateCapability":
            raise ValueError("activation outcome requires ActivateCapability")
        for field_name in (
            "execution_binding_digest",
            "observed_material_identity_digest",
            "verification_digest",
        ):
            _require_sha256(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationExecutionOutcomeRecord:
    stream_id: StreamId
    action_id: str
    action_content_digest: str
    claim_digest: str
    execution_binding_digest: str
    observed_material_identity_digest: str
    verification_digest: str
    observed_at: str
    outcome_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be non-empty text")
        for field_name in (
            "action_content_digest",
            "claim_digest",
            "execution_binding_digest",
            "observed_material_identity_digest",
            "verification_digest",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        _parse_utc_timestamp(self.observed_at, "observed_at")
        expected = _sha256(_activation_outcome_json(self))
        if self.outcome_digest and self.outcome_digest != expected:
            raise ValueError("activation outcome_digest does not match outcome content")
        object.__setattr__(self, "outcome_digest", expected)

    @property
    def settlement_guard(self) -> ActivationActionClaimGuard:
        return ActivationActionClaimGuard(
            action_id=self.action_id,
            action_content_digest=self.action_content_digest,
            mode="applied",
            execution_outcome_digest=self.outcome_digest,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationActionClaimGuard:
    """In-memory activation receipt authority; never persisted as a row.

    ``applied`` carries the exact durable execution outcome that settles the
    claim.  ``expired`` retires one never-claimed activation action and
    therefore carries no outcome: no host mutation happened, so there is
    nothing to verify and no settlement row is written.  A ``failed`` mode is
    deliberately absent until the activation outcome tables can record it.
    """

    action_id: str
    action_content_digest: str
    mode: Literal["applied", "expired"]
    execution_outcome_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be non-empty text")
        _require_sha256(self.action_content_digest, "action_content_digest")
        if self.mode not in {"applied", "expired"}:
            raise ValueError("mode must be applied or expired")
        if self.execution_outcome_digest is not None:
            _require_sha256(self.execution_outcome_digest, "execution_outcome_digest")
        if self.mode == "expired" and self.execution_outcome_digest is not None:
            raise ValueError("expired activation actions cannot carry an execution outcome")
        if self.mode == "applied" and self.execution_outcome_digest is None:
            raise ValueError("applied activation receipts require an execution outcome")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationExecutionStatus:
    claimed: bool
    outcome_recorded: bool
    settled: bool
    execution_binding_digest: str | None
    outcome_digest: str | None
    observed_at: str | None

    def __post_init__(self) -> None:
        if not self.claimed and any(
            value is not None
            for value in (
                self.execution_binding_digest,
                self.outcome_digest,
                self.observed_at,
            )
        ):
            raise ValueError("unclaimed activation cannot have execution state")
        if self.outcome_recorded != (self.outcome_digest is not None):
            raise ValueError("activation outcome state is inconsistent")
        if self.outcome_recorded != (self.observed_at is not None):
            raise ValueError("activation observation state is inconsistent")
        if self.settled and not self.outcome_recorded:
            raise ValueError("settled activation requires a verified outcome")
        if self.execution_binding_digest is not None:
            _require_sha256(self.execution_binding_digest, "execution_binding_digest")
        if self.outcome_digest is not None:
            _require_sha256(self.outcome_digest, "outcome_digest")
        if self.observed_at is not None:
            _parse_utc_timestamp(self.observed_at, "observed_at")


class SQLiteEngineStore:
    """Durable multi-process engine journal and current-state projection."""

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
        _read_only: bool = False,
    ) -> None:
        self.path = Path(os.path.abspath(Path(path)))
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be an integer >= 1")
        if type(_read_only) is not bool:
            raise TypeError("_read_only must be a bool")
        self._busy_timeout_ms = busy_timeout_ms
        self._read_only = _read_only
        self.__evidence_issuer_identity = object()
        self.__evidence_key = secrets.token_bytes(32)
        self.__issued_install_decision_evidence: weakref.WeakValueDictionary[
            int, CommittedInstallDecisionEvidence
        ] = weakref.WeakValueDictionary()
        if self._read_only:
            self._validate_existing_path()
        else:
            self._prepare_path()
        self._install_lock_root = self.path.parent / "install-execution-locks"
        if not self._read_only:
            ensure_secure_directory(self._install_lock_root)
        with self._connect():
            pass

    @classmethod
    def open_read_only(
        cls,
        path: Path,
        *,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> Self:
        """Open an existing exact-schema journal without creating or migrating it."""

        return cls(path, busy_timeout_ms=busy_timeout_ms, _read_only=True)

    def install_execution_lock_target(
        self,
        stream_id: StreamId,
        action_id: str,
    ) -> Path:
        """Return the canonical cooperating-process lock for one journal action."""

        if not isinstance(stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if not isinstance(action_id, str) or not action_id or action_id != action_id.strip():
            raise ValueError("action_id must be a non-empty trimmed string")
        lock_name = _sha256(
            _canonical_json(
                {
                    "action_id": action_id,
                    "schema": "ctx.install-execution-lock-v1",
                    "stream_key": stream_id.key,
                }
            )
        )
        return self._install_lock_root / lock_name

    @contextmanager
    def inspect_install_decision(
        self,
        query: InstallDecisionEvidenceQuery,
    ) -> Iterator[InstallDecisionEvidenceLookup]:
        """Hold an authoritative journal snapshot while classifying one decision.

        A writable store uses ``BEGIN IMMEDIATE`` so an absent-at-head result
        remains true until the caller leaves this context.  This prevents a
        broker from releasing a reservation concurrently with the journal
        commit it is trying to disprove.
        """

        from ctx.engine.installation import (
            InstallDecisionEvidenceLookup,
            InstallDecisionEvidenceQuery,
        )

        if not isinstance(query, InstallDecisionEvidenceQuery):
            raise TypeError("query must be an InstallDecisionEvidenceQuery")
        delivered = False
        try:
            with self._install_decision_evidence_transaction(query) as result:
                delivered = True
                yield result
        except JournalCorruption:
            if delivered:
                raise
            yield InstallDecisionEvidenceLookup(status="corrupt")
        except (EngineStoreError, OSError, sqlite3.Error):
            if delivered:
                raise
            yield InstallDecisionEvidenceLookup(status="unavailable")

    def revalidate_install_decision_evidence(
        self,
        evidence: CommittedInstallDecisionEvidence,
        *,
        query: InstallDecisionEvidenceQuery,
    ) -> CommittedInstallDecisionEvidence:
        """Revalidate opaque evidence through the live store that issued it."""

        from ctx.engine.installation import (
            InstallDecisionEvidenceQuery,
            InstallDecisionEvidenceRejected,
        )

        if not isinstance(query, InstallDecisionEvidenceQuery):
            raise TypeError("query must be an InstallDecisionEvidenceQuery")
        self._require_issued_install_decision_evidence(evidence)
        if not _install_decision_evidence_matches_query(evidence, query):
            raise InstallDecisionEvidenceRejected(
                "committed install decision evidence does not match the exact query"
            )
        with self.inspect_install_decision(query) as lookup:
            current = lookup.evidence
            if lookup.status != "committed" or current is None:
                raise InstallDecisionEvidenceRejected(
                    "committed install decision evidence is no longer journal-valid"
                )
            if _install_decision_evidence_claims(current) != _install_decision_evidence_claims(
                evidence
            ):
                raise InstallDecisionEvidenceRejected(
                    "committed install decision evidence changed during revalidation"
                )
        return evidence

    @contextmanager
    def _install_decision_evidence_transaction(
        self,
        query: InstallDecisionEvidenceQuery,
    ) -> Iterator[InstallDecisionEvidenceLookup]:
        from ctx.engine.installation import InstallDecisionEvidenceLookup

        with self._connect() as connection:
            try:
                connection.execute("BEGIN" if self._read_only else "BEGIN IMMEDIATE")
                result = self._inspect_install_decision(connection, query)
                if self._read_only and result.status == "absent-at-expected-head":
                    # A WAL read snapshot cannot exclude a concurrent writer.
                    # Exact positive commits are immutable, but absence needs
                    # the write-reserving lease held by a writable store.
                    result = InstallDecisionEvidenceLookup(status="unavailable")
                yield result
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _inspect_install_decision(
        self,
        connection: sqlite3.Connection,
        query: InstallDecisionEvidenceQuery,
    ) -> InstallDecisionEvidenceLookup:
        from ctx.engine.installation import InstallDecisionEvidenceLookup

        stream_id = StreamId.from_scope(query.scope)
        journal = self._validated_install_decision_evidence_journal(connection, stream_id)
        head = None if not journal else journal[-1]
        head_revision = 0 if head is None else head.revision
        head_digest = None if head is None else head.record_digest
        identity_row = connection.execute(
            "SELECT stream_key, event_content_digest FROM engine_journal WHERE event_id = ?",
            (query.event_id,),
        ).fetchone()
        if identity_row is not None:
            identity_stream_key = identity_row["stream_key"]
            if identity_stream_key != stream_id.key:
                try:
                    collision_stream = _stream_id_from_key(identity_stream_key)
                except (TypeError, ValueError) as exc:
                    raise JournalCorruption("colliding event stream identity is invalid") from exc
                collision_journal = self._validated_install_decision_evidence_journal(
                    connection,
                    collision_stream,
                )
                collision_record = next(
                    (item for item in collision_journal if item.event_id == query.event_id),
                    None,
                )
                if (
                    collision_record is None
                    or collision_record.event_content_digest != identity_row["event_content_digest"]
                ):
                    raise JournalCorruption("colliding event identity is not journal-anchored")
                return InstallDecisionEvidenceLookup(
                    status="event-collision",
                    observed_head_revision=head_revision,
                    observed_head_record_digest=head_digest,
                )
            if identity_row["event_content_digest"] != query.event_content_digest:
                return InstallDecisionEvidenceLookup(
                    status="event-collision",
                    observed_head_revision=head_revision,
                    observed_head_record_digest=head_digest,
                )
            record = next((item for item in journal if item.event_id == query.event_id), None)
            if record is None:
                raise JournalCorruption("decision event is missing from its bound journal stream")
            if not _install_decision_record_matches_query(record, query):
                return InstallDecisionEvidenceLookup(
                    status="event-collision",
                    observed_head_revision=head_revision,
                    observed_head_record_digest=head_digest,
                )
            evidence = self._issue_install_decision_evidence(query, record)
            return InstallDecisionEvidenceLookup(
                status="committed",
                evidence=evidence,
                observed_head_revision=head_revision,
                observed_head_record_digest=head_digest,
            )
        if (
            head_revision == query.expected_head_revision
            and head_digest == query.expected_head_record_digest
        ):
            return InstallDecisionEvidenceLookup(
                status="absent-at-expected-head",
                observed_head_revision=head_revision,
                observed_head_record_digest=head_digest,
            )
        return InstallDecisionEvidenceLookup(
            status="head-advanced",
            observed_head_revision=head_revision,
            observed_head_record_digest=head_digest,
        )

    @staticmethod
    def _validated_install_decision_evidence_journal(
        connection: sqlite3.Connection,
        stream_id: StreamId,
    ) -> list[JournalRecord]:
        from ctx.engine.replay import ReplayInput
        from ctx.engine.state import EngineState

        journal = SQLiteEngineStore._validated_journal(connection, stream_id)
        for record in journal:
            try:
                replay = ReplayInput.from_json(record.replay_json)
                replay.assert_record_binding(record)
                transition = Transition.from_json(record.transition_json)
                state = EngineState.from_json(record.result_state_json)
            except Exception as exc:
                raise JournalCorruption(
                    f"journal replay or state is invalid at revision {record.revision}"
                ) from exc
            if (
                transition.event_id != replay.reducer_event.event_id
                or transition.scope != replay.reducer_event.scope
                or transition.from_revision != record.revision - 1
                or transition.to_revision != record.revision
                or state.revision != record.revision
                or StreamId.from_scope(state.scope) != stream_id
            ):
                raise JournalCorruption(
                    f"journal replay, transition, and state chain diverge at revision {record.revision}"
                )
        if journal and not _projection_is_valid(connection, journal[-1]):
            raise JournalCorruption("engine projection does not match the validated journal head")
        if not journal:
            unexpected_projection = connection.execute(
                "SELECT 1 FROM engine_streams WHERE stream_key = ?",
                (stream_id.key,),
            ).fetchone()
            if unexpected_projection is not None:
                raise JournalCorruption("engine projection exists without a journal chain")
        return journal

    def _issue_install_decision_evidence(
        self,
        query: InstallDecisionEvidenceQuery,
        record: JournalRecord,
    ) -> CommittedInstallDecisionEvidence:
        from ctx.engine.installation import (
            CommittedInstallDecisionEvidence,
            _INSTALL_DECISION_EVIDENCE_SERIALIZATION_GUARD,
        )

        evidence = object.__new__(CommittedInstallDecisionEvidence)
        claims: dict[str, object] = {
            "committed_revision": record.revision,
            "consent_id": query.consent_id,
            "decision": query.decision,
            "decision_basis": query.decision_basis,
            "event_content_digest": record.event_content_digest,
            "event_id": record.event_id,
            "policy_snapshot_digest": query.policy_snapshot_digest,
            "previous_record_digest": record.previous_record_digest,
            "record_digest": record.record_digest,
            "requested_action_content_digest": query.requested_action_content_digest,
            "requested_action_id": query.requested_action_id,
            "requested_action_kind": query.requested_action_kind,
            "requested_action_precondition_revision": (
                query.requested_action_precondition_revision
            ),
            "result_state_digest": record.result_state_digest,
            "scope": query.scope,
            "stream_identity_digest": query.stream_identity_digest,
        }
        for name, value in claims.items():
            object.__setattr__(evidence, name, value)
        object.__setattr__(
            evidence,
            "_serialization_guard",
            _INSTALL_DECISION_EVIDENCE_SERIALIZATION_GUARD,
        )
        object.__setattr__(evidence, "_issuer_identity", self.__evidence_issuer_identity)
        seal = hmac.digest(
            self.__evidence_key,
            _canonical_json(_install_decision_evidence_claims(evidence)).encode("utf-8"),
            "sha256",
        ).hex()
        object.__setattr__(evidence, "_seal", seal)
        self.__issued_install_decision_evidence[id(evidence)] = evidence
        return evidence

    def _require_issued_install_decision_evidence(
        self,
        evidence: CommittedInstallDecisionEvidence,
    ) -> None:
        from ctx.engine.installation import (
            CommittedInstallDecisionEvidence,
            InstallDecisionEvidenceRejected,
        )

        if not isinstance(evidence, CommittedInstallDecisionEvidence):
            raise InstallDecisionEvidenceRejected(
                "committed install decision evidence must be opaque store authority"
            )
        if evidence._issuer_identity is not self.__evidence_issuer_identity:
            raise InstallDecisionEvidenceRejected(
                "committed install decision evidence came from another store process"
            )
        if self.__issued_install_decision_evidence.get(id(evidence)) is not evidence:
            raise InstallDecisionEvidenceRejected(
                "committed install decision evidence was not issued as this exact object"
            )
        expected = hmac.digest(
            self.__evidence_key,
            _canonical_json(_install_decision_evidence_claims(evidence)).encode("utf-8"),
            "sha256",
        ).hex()
        if not hmac.compare_digest(evidence._seal, expected):
            raise InstallDecisionEvidenceRejected(
                "committed install decision evidence seal is invalid"
            )

    def load_head(self, stream_id: StreamId) -> StoredHead:
        with self._connect() as connection:
            connection.execute("BEGIN")
            journal = self._validated_journal(connection, stream_id)
            if journal:
                projection_valid = _projection_is_valid(connection, journal[-1])
            else:
                projection_valid = (
                    connection.execute(
                        "SELECT 1 FROM engine_streams WHERE stream_key = ?",
                        (stream_id.key,),
                    ).fetchone()
                    is None
                )
            connection.execute("COMMIT")
        if not journal:
            return StoredHead(
                stream_id=stream_id,
                revision=0,
                state_json=None,
                state_digest=None,
                projection_valid=projection_valid,
                record_digest=None,
            )
        head = journal[-1]
        return StoredHead(
            stream_id=stream_id,
            revision=head.revision,
            state_json=head.result_state_json,
            state_digest=head.result_state_digest,
            projection_valid=projection_valid,
            record_digest=head.record_digest,
        )

    def cached_transition(
        self,
        stream_id: StreamId,
        event_id: str,
        event_content_digest: str,
    ) -> Transition | None:
        """Return an exact prior result or reject event-id content reuse."""

        if not isinstance(stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
            raise ValueError("event_id must be a non-empty trimmed string")
        _require_sha256(event_content_digest, "event_content_digest")
        with self._connect() as connection:
            cached = self._cached_commit_result(
                connection,
                stream_id=stream_id,
                event_id=event_id,
                event_content_digest=event_content_digest,
            )
        return None if cached is None else cached.transition

    def claim_install(
        self,
        request: InstallActionClaimRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> InstallActionClaimRecord:
        """Atomically burn the authority to execute one exact v3 install action."""

        if not isinstance(request, InstallActionClaimRequest):
            raise TypeError("request must be an InstallActionClaimRequest")
        if not callable(trusted_utc_now):
            raise TypeError("trusted_utc_now must be callable")
        try:
            action = HostAction.from_json(request.action_json)
        except Exception as exc:
            raise ValueError("action_json must encode a valid HostAction") from exc
        if (
            action.kind != "InstallCapability"
            or action.payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            raise ValueError("only exact v3 InstallCapability actions may be claimed")
        if StreamId.from_scope(action.scope) != request.stream_id:
            raise ValueError("action scope does not belong to the requested stream")
        from ctx.engine.installation import InstallExecutionBinding, InstallPlanDescriptor

        binding = InstallExecutionBinding.from_json(request.execution_binding_json)
        descriptor_value = action.payload.get("install_plan_descriptor")
        if not isinstance(descriptor_value, Mapping):
            raise ValueError("install action has no typed plan descriptor")
        descriptor = InstallPlanDescriptor.from_dict(descriptor_value)
        if (
            binding.driver_id != descriptor.installer_id
            or binding.driver_digest != action.payload.get("installer_digest")
        ):
            raise ValueError("execution driver does not match the install descriptor")

        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                journal = self._validated_journal(connection, request.stream_id)
                _validated_install_authority_rows(connection)
                prior = _load_claim(connection, request.stream_id, action.action_id)
                if prior is not None:
                    if (
                        prior.action_json != request.action_json
                        or prior.action_content_digest != action.content_digest
                        or prior.authorization_digest != request.authorization_digest
                        or prior.execution_binding_json != request.execution_binding_json
                    ):
                        raise JournalCorruption(
                            "install action id is bound to different persisted claim authority"
                        )
                    raise InstallActionAlreadyClaimed(
                        f"install action {action.action_id!r} has already been claimed"
                    )
                actual = 0 if not journal else journal[-1].revision
                if actual != request.expected_revision:
                    raise RevisionConflict(expected=request.expected_revision, actual=actual)
                if not journal or journal[-1].record_digest != request.expected_head_record_digest:
                    raise RevisionConflict(expected=request.expected_revision, actual=actual)
                issuing = next(
                    (item for item in journal if item.revision == action.precondition_revision),
                    None,
                )
                if issuing is None:
                    raise JournalCorruption("install action issuing revision is absent")
                issuing_transition = Transition.from_json(issuing.transition_json)
                matches = [
                    candidate
                    for candidate in issuing_transition.actions
                    if candidate.action_id == action.action_id
                    and candidate.to_json() == request.action_json
                ]
                if len(matches) != 1:
                    raise JournalCorruption(
                        "install action is not byte-equal to one action emitted by its issuing record"
                    )
                try:
                    now = trusted_utc_now()
                except Exception:
                    raise EngineStoreError("trusted UTC clock failed") from None
                claimed_at = _canonical_utc_timestamp(now, "trusted_utc_now")
                expires_at = action.expires_at
                if expires_at is None:
                    raise JournalCorruption("persisted install action has no expiry")
                if now >= _parse_utc_timestamp(expires_at, "action.expires_at"):
                    raise InstallActionClaimExpired(
                        f"install action {action.action_id!r} expired before it was claimed"
                    )
                head = journal[-1]
                claim = InstallActionClaimRecord(
                    stream_id=request.stream_id,
                    action_id=action.action_id,
                    action_content_digest=action.content_digest,
                    action_kind=action.kind,
                    precondition_revision=action.precondition_revision,
                    action_json=request.action_json,
                    issuing_record_digest=issuing.record_digest,
                    claimed_head_revision=head.revision,
                    claimed_head_record_digest=head.record_digest,
                    claimed_head_state_digest=head.result_state_digest,
                    action_expires_at=expires_at,
                    claimed_at=claimed_at,
                    authorization_digest=request.authorization_digest,
                    execution_binding_json=request.execution_binding_json,
                    execution_binding_digest=binding.binding_digest,
                )
                _insert_claim(connection, claim)
                persisted = _load_claim(connection, request.stream_id, action.action_id)
                if persisted != claim:
                    raise JournalCorruption("inserted install claim was not durably preserved")
                _validated_install_authority_rows(connection)
                connection.execute("COMMIT")
                return claim
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def record_install_outcome(
        self,
        request: InstallExecutionOutcomeRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> InstallExecutionOutcomeRecord:
        """Durably bind one verified host observation to a claimed install."""

        if not isinstance(request, InstallExecutionOutcomeRequest):
            raise TypeError("request must be an InstallExecutionOutcomeRequest")
        if not callable(trusted_utc_now):
            raise TypeError("trusted_utc_now must be callable")
        action = HostAction.from_json(request.action_json)
        if StreamId.from_scope(action.scope) != request.stream_id:
            raise ValueError("action scope does not belong to the requested stream")
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _validated_install_authority_rows(connection)
                claim = _load_claim(connection, request.stream_id, action.action_id)
                if claim is None:
                    raise InstallExecutionOutcomeRequired(
                        f"install action {action.action_id!r} has no durable execution claim"
                    )
                if (
                    claim.action_json != request.action_json
                    or claim.action_content_digest != action.content_digest
                ):
                    raise JournalCorruption("execution outcome does not match claimed action")
                if claim.execution_binding_digest != request.execution_binding_digest:
                    raise InstallExecutionOutcomeConflict(
                        "execution outcome binding does not match the claimed driver and target"
                    )
                if _load_settlement(connection, request.stream_id, action.action_id) is not None:
                    raise InstallActionClaimSettled(
                        f"install action {action.action_id!r} already has a terminal settlement"
                    )
                expected_material = _action_result_material_identity_digest(action)
                if request.outcome == "applied" and (
                    request.observed_material_identity_digest != expected_material
                ):
                    raise InstallExecutionOutcomeConflict(
                        "applied execution outcome does not match authorized result material"
                    )
                prior = _load_install_outcome(connection, request.stream_id, action.action_id)
                if prior is not None:
                    if (
                        prior.action_content_digest == action.content_digest
                        and prior.execution_binding_digest == request.execution_binding_digest
                        and prior.outcome == request.outcome
                        and prior.observed_material_identity_digest
                        == request.observed_material_identity_digest
                        and prior.verification_digest == request.verification_digest
                    ):
                        connection.execute("COMMIT")
                        return prior
                    raise InstallExecutionOutcomeConflict(
                        f"install action {action.action_id!r} already has a different outcome"
                    )
                try:
                    now = trusted_utc_now()
                except Exception:
                    raise EngineStoreError("trusted UTC clock failed") from None
                observed_at = _canonical_utc_timestamp(now, "trusted_utc_now")
                if now < _parse_utc_timestamp(claim.claimed_at, "claim.claimed_at"):
                    raise EngineStoreError("trusted UTC clock moved before the install claim")
                outcome = InstallExecutionOutcomeRecord(
                    stream_id=request.stream_id,
                    action_id=action.action_id,
                    action_content_digest=action.content_digest,
                    claim_digest=claim.claim_digest,
                    execution_binding_digest=request.execution_binding_digest,
                    outcome=request.outcome,
                    observed_material_identity_digest=request.observed_material_identity_digest,
                    verification_digest=request.verification_digest,
                    observed_at=observed_at,
                )
                _insert_install_outcome(connection, outcome)
                persisted = _load_install_outcome(connection, request.stream_id, action.action_id)
                if persisted != outcome:
                    raise JournalCorruption("inserted execution outcome was not durably preserved")
                _validated_install_authority_rows(connection)
                connection.execute("COMMIT")
                return outcome
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def install_execution_status(
        self,
        stream_id: StreamId,
        action_id: str,
    ) -> InstallExecutionStatus:
        """Return non-executable durable execution and reconciliation state."""

        if not isinstance(stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if not isinstance(action_id, str) or not action_id.strip():
            raise ValueError("action_id must be non-empty text")
        with self._connect() as connection:
            connection.execute("BEGIN")
            _validated_install_authority_rows(connection)
            claim = _load_claim(connection, stream_id, action_id)
            outcome = _load_install_outcome(connection, stream_id, action_id)
            settlement = _load_settlement(connection, stream_id, action_id)
            connection.execute("COMMIT")
        return InstallExecutionStatus(
            claimed=claim is not None,
            outcome_recorded=outcome is not None,
            settled=settlement is not None,
            execution_binding_digest=(None if claim is None else claim.execution_binding_digest),
            outcome=None if outcome is None else outcome.outcome,
            outcome_digest=None if outcome is None else outcome.outcome_digest,
            observed_at=None if outcome is None else outcome.observed_at,
        )

    def claim_activation(
        self,
        request: ActivationActionClaimRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> ActivationActionClaimRecord:
        """Atomically bind one pending activation to an exact verifier target."""

        if not isinstance(request, ActivationActionClaimRequest):
            raise TypeError("request must be an ActivationActionClaimRequest")
        if not callable(trusted_utc_now):
            raise TypeError("trusted_utc_now must be callable")
        action = HostAction.from_json(request.action_json)
        from ctx.engine.installation import InstallExecutionBinding

        binding = InstallExecutionBinding.from_json(request.execution_binding_json)
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                journal = self._validated_journal(connection, request.stream_id)
                _validated_activation_authority_rows(connection)
                prior = _load_activation_claim(connection, request.stream_id, action.action_id)
                if prior is not None:
                    if (
                        prior.action_json != request.action_json
                        or prior.authorization_digest != request.authorization_digest
                        or prior.execution_binding_digest != binding.binding_digest
                    ):
                        raise JournalCorruption(
                            "activation action is claimed by different verifier authority"
                        )
                    raise ActivationActionAlreadyClaimed(
                        f"activation action {action.action_id!r} is already claimed"
                    )
                actual = 0 if not journal else journal[-1].revision
                if actual != request.expected_revision:
                    raise RevisionConflict(expected=request.expected_revision, actual=actual)
                if not journal or journal[-1].record_digest != request.expected_head_record_digest:
                    raise RevisionConflict(expected=request.expected_revision, actual=actual)
                issuing = next(
                    (row for row in journal if row.revision == action.precondition_revision),
                    None,
                )
                if issuing is None or not any(
                    candidate.to_json() == action.to_json()
                    for candidate in Transition.from_json(issuing.transition_json).actions
                ):
                    raise JournalCorruption("activation action is not exactly journal-emitted")
                head = journal[-1]
                try:
                    now = trusted_utc_now()
                except Exception:
                    raise EngineStoreError("trusted UTC clock failed") from None
                claimed_at = _canonical_utc_timestamp(now, "trusted_utc_now")
                installed_at = _activation_install_observed_at(
                    connection,
                    stream_id=request.stream_id,
                    state_json=head.result_state_json,
                    action=action,
                )
                if now < installed_at:
                    raise EngineStoreError("trusted UTC clock moved before installation")
                if action.expires_at is None:
                    raise JournalCorruption("activation action has no expiry")
                if now >= _parse_utc_timestamp(action.expires_at, "action.expires_at"):
                    raise ActivationActionClaimExpired(
                        f"activation action {action.action_id!r} expired before claim"
                    )
                claim = ActivationActionClaimRecord(
                    stream_id=request.stream_id,
                    action_id=action.action_id,
                    action_content_digest=action.content_digest,
                    precondition_revision=action.precondition_revision,
                    action_json=action.to_json(),
                    issuing_record_digest=issuing.record_digest,
                    claimed_head_revision=head.revision,
                    claimed_head_record_digest=head.record_digest,
                    claimed_head_state_digest=head.result_state_digest,
                    action_expires_at=action.expires_at,
                    claimed_at=claimed_at,
                    authorization_digest=request.authorization_digest,
                    execution_binding_json=binding.to_json(),
                    execution_binding_digest=binding.binding_digest,
                )
                _insert_activation_claim(connection, claim)
                if _load_activation_claim(connection, request.stream_id, action.action_id) != claim:
                    raise JournalCorruption("activation claim was not durably preserved")
                _validated_activation_authority_rows(connection)
                connection.execute("COMMIT")
                return claim
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def record_activation_outcome(
        self,
        request: ActivationExecutionOutcomeRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> ActivationExecutionOutcomeRecord:
        """Persist exact verified material identity at actual activation time."""

        if not isinstance(request, ActivationExecutionOutcomeRequest):
            raise TypeError("request must be an ActivationExecutionOutcomeRequest")
        if not callable(trusted_utc_now):
            raise TypeError("trusted_utc_now must be callable")
        action = HostAction.from_json(request.action_json)
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                _validated_activation_authority_rows(connection)
                claim = _load_activation_claim(connection, request.stream_id, action.action_id)
                if claim is None:
                    raise ActivationExecutionOutcomeRequired(
                        f"activation action {action.action_id!r} has no durable claim"
                    )
                if (
                    claim.action_json != action.to_json()
                    or claim.execution_binding_digest != request.execution_binding_digest
                ):
                    raise ActivationExecutionOutcomeConflict(
                        "activation outcome does not match its durable claim"
                    )
                if (
                    _load_activation_settlement(connection, request.stream_id, action.action_id)
                    is not None
                ):
                    raise ActivationActionClaimSettled(
                        f"activation action {action.action_id!r} is already settled"
                    )
                expected_material = _activation_material_identity_digest(action)
                if request.observed_material_identity_digest != expected_material:
                    raise ActivationExecutionOutcomeConflict(
                        "activation outcome does not match authorized material"
                    )
                prior = _load_activation_outcome(connection, request.stream_id, action.action_id)
                if prior is not None:
                    if (
                        prior.action_content_digest == action.content_digest
                        and prior.execution_binding_digest == request.execution_binding_digest
                        and prior.observed_material_identity_digest
                        == request.observed_material_identity_digest
                        and prior.verification_digest == request.verification_digest
                    ):
                        connection.execute("COMMIT")
                        return prior
                    raise ActivationExecutionOutcomeConflict(
                        "activation action already has a different verified outcome"
                    )
                try:
                    now = trusted_utc_now()
                except Exception:
                    raise EngineStoreError("trusted UTC clock failed") from None
                observed_at = _canonical_utc_timestamp(now, "trusted_utc_now")
                if now < _parse_utc_timestamp(claim.claimed_at, "claim.claimed_at"):
                    raise EngineStoreError("trusted UTC clock moved before activation claim")
                if now >= _parse_utc_timestamp(claim.action_expires_at, "claim.action_expires_at"):
                    raise ActivationActionClaimExpired(
                        f"activation action {action.action_id!r} expired before observation"
                    )
                outcome = ActivationExecutionOutcomeRecord(
                    stream_id=request.stream_id,
                    action_id=action.action_id,
                    action_content_digest=action.content_digest,
                    claim_digest=claim.claim_digest,
                    execution_binding_digest=request.execution_binding_digest,
                    observed_material_identity_digest=request.observed_material_identity_digest,
                    verification_digest=request.verification_digest,
                    observed_at=observed_at,
                )
                _insert_activation_outcome(connection, outcome)
                if (
                    _load_activation_outcome(connection, request.stream_id, action.action_id)
                    != outcome
                ):
                    raise JournalCorruption("activation outcome was not durably preserved")
                _validated_activation_authority_rows(connection)
                connection.execute("COMMIT")
                return outcome
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def activation_execution_status(
        self,
        stream_id: StreamId,
        action_id: str,
    ) -> ActivationExecutionStatus:
        with self._connect() as connection:
            connection.execute("BEGIN")
            _validated_activation_authority_rows(connection)
            claim = _load_activation_claim(connection, stream_id, action_id)
            outcome = _load_activation_outcome(connection, stream_id, action_id)
            settlement = _load_activation_settlement(connection, stream_id, action_id)
            connection.execute("COMMIT")
        return ActivationExecutionStatus(
            claimed=claim is not None,
            outcome_recorded=outcome is not None,
            settled=settlement is not None,
            execution_binding_digest=(None if claim is None else claim.execution_binding_digest),
            outcome_digest=None if outcome is None else outcome.outcome_digest,
            observed_at=None if outcome is None else outcome.observed_at,
        )

    def commit(
        self,
        *,
        expected_revision: int,
        record: JournalRecord,
        install_claim_guard: InstallActionClaimGuard | None = None,
        activation_claim_guard: ActivationActionClaimGuard | None = None,
    ) -> CommitResult:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        if record.revision != expected_revision + 1:
            raise ValueError("record revision must equal expected_revision + 1")
        transition = Transition.from_json(record.transition_json)
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cached = self._cached_commit_result(
                    connection,
                    stream_id=record.stream_id,
                    event_id=record.event_id,
                    event_content_digest=record.event_content_digest,
                )
                if cached is not None:
                    connection.execute("COMMIT")
                    return cached
                journal = self._validated_journal(connection, record.stream_id)
                claim: InstallActionClaimRecord | None = None
                activation_claim: ActivationActionClaimRecord | None = None
                if install_claim_guard is not None and activation_claim_guard is not None:
                    raise ValueError("one receipt cannot settle install and activation claims")
                if install_claim_guard is not None:
                    if not isinstance(install_claim_guard, InstallActionClaimGuard):
                        raise TypeError("install_claim_guard must be an InstallActionClaimGuard")
                    _validated_install_authority_rows(connection)
                    claim = _validate_install_claim_guard(
                        connection,
                        stream_id=record.stream_id,
                        guard=install_claim_guard,
                    )
                if activation_claim_guard is not None:
                    _validated_activation_authority_rows(connection)
                    activation_claim = _validate_activation_claim_guard(
                        connection,
                        stream_id=record.stream_id,
                        guard=activation_claim_guard,
                    )
                actual = 0 if not journal else journal[-1].revision
                if actual != expected_revision:
                    raise RevisionConflict(expected=expected_revision, actual=actual)
                previous_digest = None if not journal else journal[-1].record_digest
                bound = record.bind_chain(previous_digest)
                _ensure_projection_schema(connection)
                self._write_projection(connection, bound)
                self._insert_record(connection, bound)
                if install_claim_guard is not None and install_claim_guard.mode != "expired":
                    assert claim is not None
                    self._insert_install_settlement(
                        connection,
                        claim=claim,
                        guard=install_claim_guard,
                        receipt=bound,
                    )
                    self._assert_install_settlement_preserved(
                        connection,
                        claim=claim,
                        guard=install_claim_guard,
                        receipt=bound,
                    )
                    _validated_install_authority_rows(connection)
                if activation_claim_guard is not None and activation_claim_guard.mode != "expired":
                    assert activation_claim is not None
                    self._insert_activation_settlement(
                        connection,
                        claim=activation_claim,
                        guard=activation_claim_guard,
                        receipt=bound,
                    )
                    self._assert_activation_settlement_preserved(
                        connection,
                        claim=activation_claim,
                        guard=activation_claim_guard,
                        receipt=bound,
                    )
                    _validated_activation_authority_rows(connection)
                connection.execute("COMMIT")
                return CommitResult(
                    committed=True,
                    revision=bound.revision,
                    transition=transition,
                    record=bound,
                )
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _cached_commit_result(
        connection: sqlite3.Connection,
        *,
        stream_id: StreamId,
        event_id: str,
        event_content_digest: str,
    ) -> CommitResult | None:
        try:
            row = connection.execute(
                """
                SELECT stream_key, event_content_digest
                  FROM engine_journal
                 WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise
            raise JournalCorruption("persisted event identity cannot be reconstructed") from exc
        if row is None:
            return None
        stored_digest = str(row["event_content_digest"])
        if str(row["stream_key"]) != stream_id.key:
            raise EventIdCollision(
                event_id=event_id,
                stored_digest=stored_digest,
                submitted_digest=event_content_digest,
            )
        journal = SQLiteEngineStore._validated_journal(connection, stream_id)
        record = next((item for item in journal if item.event_id == event_id), None)
        if record is None:
            raise JournalCorruption(f"event {event_id!r} is missing from its journal stream")
        if stored_digest != event_content_digest:
            raise EventIdCollision(
                event_id=event_id,
                stored_digest=stored_digest,
                submitted_digest=event_content_digest,
            )
        return CommitResult(
            committed=False,
            revision=record.revision,
            transition=Transition.from_json(record.transition_json),
            record=record,
        )

    def records(
        self,
        stream_id: StreamId,
        *,
        after_revision: int = 0,
    ) -> Iterator[JournalRecord]:
        if type(after_revision) is not int or after_revision < 0:
            raise ValueError("after_revision must be a non-negative integer")
        with self._connect() as connection:
            connection.execute("BEGIN")
            records = self._validated_journal(connection, stream_id)
            connection.execute("COMMIT")
        return iter(record for record in records if record.revision > after_revision)

    def repair_projection(
        self,
        stream_id: StreamId,
        *,
        at_revision: int,
        state_json: str,
        record_digest: str,
    ) -> bool:
        """CAS-repair the rebuildable head projection without altering history."""

        if type(at_revision) is not int or at_revision < 1:
            raise ValueError("at_revision must be an integer >= 1")
        if _canonical_json_text(state_json) != state_json:
            raise ValueError("state_json must be canonical JSON")
        _require_sha256(record_digest, "record_digest")
        state_digest = _sha256(state_json)
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                journal = self._validated_journal(connection, stream_id)
                if not journal or journal[-1].revision != at_revision:
                    connection.execute("ROLLBACK")
                    return False
                head = journal[-1]
                if head.record_digest != record_digest:
                    connection.execute("ROLLBACK")
                    return False
                if head.result_state_digest != state_digest:
                    raise JournalCorruption(
                        "rebuilt projection does not match the committed result-state digest"
                    )
                _ensure_projection_schema(connection)
                connection.execute(
                    """
                    INSERT INTO engine_streams (
                        stream_key, tenant_id, workspace_id, repository_id, session_id,
                        revision, state_json, state_digest, head_record_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(stream_key) DO UPDATE SET
                        tenant_id = excluded.tenant_id,
                        workspace_id = excluded.workspace_id,
                        repository_id = excluded.repository_id,
                        session_id = excluded.session_id,
                        revision = excluded.revision,
                        state_json = excluded.state_json,
                        state_digest = excluded.state_digest,
                        head_record_digest = excluded.head_record_digest
                    """,
                    (
                        stream_id.key,
                        stream_id.tenant_id,
                        stream_id.workspace_id,
                        stream_id.repository_id,
                        stream_id.session_id,
                        at_revision,
                        state_json,
                        state_digest,
                        record_digest,
                    ),
                )
                connection.execute("COMMIT")
                return True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _prepare_path(self) -> None:
        """Prepare a dedicated private parent without mutating caller directories.

        A private, owned parent excludes pathname replacement by other OS users.
        A malicious process running as the same user can still race path-based
        SQLite access; callers needing protection from that actor need process or
        account isolation rather than filesystem mode bits.
        """

        reject_symlink_path(self.path)
        ensure_secure_directory(self.path.parent)
        reject_symlink_path(self.path)
        _require_private_directory(self.path.parent)
        created = False
        if not self.path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, _PRIVATE_FILE_MODE)
            created = True
            try:
                os.close(descriptor)
                os.chmod(self.path, _PRIVATE_FILE_MODE)
            except Exception:
                try:
                    self.path.unlink()
                except OSError:
                    pass
                raise
        try:
            _require_private_file(self.path)
        except Exception:
            if created:
                try:
                    self.path.unlink()
                except OSError:
                    pass
            raise
        if os.name == "nt":  # POSIX modes do not model Windows ACLs.
            os.chmod(self.path, _PRIVATE_FILE_MODE)

    def _validate_existing_path(self) -> None:
        """Validate a read-only database path without creating or hardening it."""

        reject_symlink_path(self.path)
        _require_private_directory(self.path.parent)
        _require_private_file(self.path)
        _require_secure_sqlite_files(self.path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        reject_symlink_path(self.path)
        _require_private_directory(self.path.parent)
        _require_private_file(self.path)
        if self._read_only:
            _require_secure_sqlite_files(self.path)
        else:
            _secure_sqlite_files(self.path)
        connection: sqlite3.Connection | None = None
        try:
            if self._read_only:
                connection = sqlite3.connect(
                    f"{self.path.as_uri()}?mode=ro",
                    timeout=self._busy_timeout_ms / 1000,
                    isolation_level=None,
                    uri=True,
                )
            else:
                connection = sqlite3.connect(
                    self.path,
                    timeout=self._busy_timeout_ms / 1000,
                    isolation_level=None,
                )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            if self._read_only:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA query_only = ON")
                _require_exact_store_schema(connection)
            else:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.executescript(_SCHEMA)
                _remove_projection_foreign_key(connection)
            _validated_install_authority_rows(connection)
            _validated_activation_authority_rows(connection)
            if self._read_only:
                _require_secure_sqlite_files(self.path)
            else:
                _secure_sqlite_files(self.path)
            yield connection
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StoreBusy(str(exc)) from exc
            raise EngineStoreError(f"engine database operation failed: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise JournalCorruption(f"engine database is unreadable: {self.path}") from exc
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _write_projection(connection: sqlite3.Connection, record: JournalRecord) -> None:
        connection.execute(
            """
            INSERT INTO engine_streams (
                stream_key, tenant_id, workspace_id, repository_id, session_id,
                revision, state_json, state_digest, head_record_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stream_key) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                workspace_id = excluded.workspace_id,
                repository_id = excluded.repository_id,
                session_id = excluded.session_id,
                revision = excluded.revision,
                state_json = excluded.state_json,
                state_digest = excluded.state_digest,
                head_record_digest = excluded.head_record_digest
            """,
            (
                record.stream_id.key,
                record.stream_id.tenant_id,
                record.stream_id.workspace_id,
                record.stream_id.repository_id,
                record.stream_id.session_id,
                record.revision,
                record.result_state_json,
                record.result_state_digest,
                record.record_digest,
            ),
        )

    @staticmethod
    def _validated_journal(
        connection: sqlite3.Connection,
        stream_id: StreamId,
    ) -> list[JournalRecord]:
        try:
            rows = connection.execute(
                """
                SELECT * FROM engine_journal
                 WHERE stream_key = ?
                 ORDER BY revision
                """,
                (stream_id.key,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise
            raise JournalCorruption("persisted journal text cannot be reconstructed") from exc
        expected_revision = 1
        previous_digest: str | None = None
        records: list[JournalRecord] = []
        for row in rows:
            record = _row_to_record(row, stream_id)
            if record.revision != expected_revision:
                raise JournalCorruption(
                    f"journal revision gap: expected {expected_revision}, found {record.revision}"
                )
            if record.previous_record_digest != previous_digest:
                raise JournalCorruption(f"journal chain mismatch at revision {record.revision}")
            records.append(record)
            previous_digest = record.record_digest
            expected_revision += 1
        return records

    @staticmethod
    def _insert_record(connection: sqlite3.Connection, record: JournalRecord) -> None:
        connection.execute(
            """
            INSERT INTO engine_journal (
                event_id, stream_key, revision, event_content_digest,
                replay_json, replay_digest, transition_json, transition_digest,
                result_state_json, result_state_digest, previous_record_digest,
                record_digest, privacy_classification, retention_class,
                reducer_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.event_id,
                record.stream_id.key,
                record.revision,
                record.event_content_digest,
                record.replay_json,
                record.replay_digest,
                record.transition_json,
                record.transition_digest,
                record.result_state_json,
                record.result_state_digest,
                record.previous_record_digest,
                record.record_digest,
                record.privacy_classification,
                record.retention_class,
                record.reducer_version,
            ),
        )

    @staticmethod
    def _insert_install_settlement(
        connection: sqlite3.Connection,
        *,
        claim: InstallActionClaimRecord,
        guard: InstallActionClaimGuard,
        receipt: JournalRecord,
    ) -> None:
        values = {
            "action_content_digest": claim.action_content_digest,
            "action_id": claim.action_id,
            "claim_digest": claim.claim_digest,
            "outcome": guard.mode,
            "receipt_event_content_digest": receipt.event_content_digest,
            "receipt_event_id": receipt.event_id,
            "receipt_record_digest": receipt.record_digest,
            "stream_key": claim.stream_id.key,
        }
        settlement_digest = _sha256(_canonical_json(values))
        connection.execute(
            """
            INSERT INTO engine_install_claim_settlements (
                stream_key, action_id, action_content_digest, claim_digest, outcome,
                receipt_event_id, receipt_event_content_digest, receipt_record_digest,
                settlement_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim.stream_id.key,
                claim.action_id,
                claim.action_content_digest,
                claim.claim_digest,
                guard.mode,
                receipt.event_id,
                receipt.event_content_digest,
                receipt.record_digest,
                settlement_digest,
            ),
        )

    @staticmethod
    def _assert_install_settlement_preserved(
        connection: sqlite3.Connection,
        *,
        claim: InstallActionClaimRecord,
        guard: InstallActionClaimGuard,
        receipt: JournalRecord,
    ) -> None:
        values = {
            "action_content_digest": claim.action_content_digest,
            "action_id": claim.action_id,
            "claim_digest": claim.claim_digest,
            "outcome": guard.mode,
            "receipt_event_content_digest": receipt.event_content_digest,
            "receipt_event_id": receipt.event_id,
            "receipt_record_digest": receipt.record_digest,
            "stream_key": claim.stream_id.key,
        }
        expected = (
            claim.stream_id.key,
            claim.action_id,
            claim.action_content_digest,
            claim.claim_digest,
            guard.mode,
            receipt.event_id,
            receipt.event_content_digest,
            receipt.record_digest,
            _sha256(_canonical_json(values)),
        )
        row = connection.execute(
            """
            SELECT stream_key, action_id, action_content_digest, claim_digest, outcome,
                   receipt_event_id, receipt_event_content_digest, receipt_record_digest,
                   settlement_digest
              FROM engine_install_claim_settlements
             WHERE stream_key = ? AND action_id = ?
            """,
            (claim.stream_id.key, claim.action_id),
        ).fetchone()
        if row is None or tuple(row) != expected:
            raise JournalCorruption("inserted install settlement was not durably preserved")

    @staticmethod
    def _insert_activation_settlement(
        connection: sqlite3.Connection,
        *,
        claim: ActivationActionClaimRecord,
        guard: ActivationActionClaimGuard,
        receipt: JournalRecord,
    ) -> None:
        # Only an applied receipt reaches settlement; an expired retirement
        # writes no row.  The digest inputs below must stay byte-identical to
        # every settlement already on disk, so `mode` never enters `values`.
        if guard.mode != "applied" or guard.execution_outcome_digest is None:
            raise ActivationExecutionOutcomeRequired(
                f"activation action {guard.action_id!r} has no verified execution outcome"
            )
        values = {
            "action_content_digest": claim.action_content_digest,
            "action_id": claim.action_id,
            "claim_digest": claim.claim_digest,
            "outcome_digest": guard.execution_outcome_digest,
            "receipt_event_content_digest": receipt.event_content_digest,
            "receipt_event_id": receipt.event_id,
            "receipt_record_digest": receipt.record_digest,
            "stream_key": claim.stream_id.key,
        }
        connection.execute(
            """
            INSERT INTO engine_activation_claim_settlements (
                stream_key, action_id, action_content_digest, claim_digest,
                outcome_digest, receipt_event_id, receipt_event_content_digest,
                receipt_record_digest, settlement_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                claim.stream_id.key,
                claim.action_id,
                claim.action_content_digest,
                claim.claim_digest,
                guard.execution_outcome_digest,
                receipt.event_id,
                receipt.event_content_digest,
                receipt.record_digest,
                _sha256(_canonical_json(values)),
            ),
        )

    @staticmethod
    def _assert_activation_settlement_preserved(
        connection: sqlite3.Connection,
        *,
        claim: ActivationActionClaimRecord,
        guard: ActivationActionClaimGuard,
        receipt: JournalRecord,
    ) -> None:
        row = _load_activation_settlement(connection, claim.stream_id, claim.action_id)
        if row is None:
            raise JournalCorruption("activation settlement was not durably preserved")
        if guard.mode != "applied" or guard.execution_outcome_digest is None:
            raise ActivationExecutionOutcomeRequired(
                f"activation action {guard.action_id!r} has no verified execution outcome"
            )
        values = {
            "action_content_digest": claim.action_content_digest,
            "action_id": claim.action_id,
            "claim_digest": claim.claim_digest,
            "outcome_digest": guard.execution_outcome_digest,
            "receipt_event_content_digest": receipt.event_content_digest,
            "receipt_event_id": receipt.event_id,
            "receipt_record_digest": receipt.record_digest,
            "stream_key": claim.stream_id.key,
        }
        expected = (
            claim.stream_id.key,
            claim.action_id,
            claim.action_content_digest,
            claim.claim_digest,
            guard.execution_outcome_digest,
            receipt.event_id,
            receipt.event_content_digest,
            receipt.record_digest,
            _sha256(_canonical_json(values)),
        )
        if tuple(row) != expected:
            raise JournalCorruption("activation settlement was not durably preserved")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _install_decision_record_matches_query(
    record: JournalRecord,
    query: InstallDecisionEvidenceQuery,
) -> bool:
    from ctx.engine.replay import ReplayInput

    replay = ReplayInput.from_json(record.replay_json)
    replay.assert_record_binding(record)
    event = replay.reducer_event
    expected_payload: dict[str, object] = {
        "consent_id": query.consent_id,
        "decision": query.decision,
        "decision_basis": query.decision_basis,
        "policy_snapshot_digest": query.policy_snapshot_digest,
        "requested_action_content_digest": query.requested_action_content_digest,
        "requested_action_id": query.requested_action_id,
        "requested_action_kind": query.requested_action_kind,
        "requested_action_precondition_revision": (query.requested_action_precondition_revision),
    }
    return bool(
        event.kind == "UserDecision"
        and event.scope == query.scope
        and event.expected_revision == query.expected_head_revision
        and dict(event.payload) == expected_payload
        and record.event_id == query.event_id
        and record.event_content_digest == query.event_content_digest
        and record.revision == query.requested_action_precondition_revision
        and record.previous_record_digest == query.expected_head_record_digest
    )


def _install_decision_evidence_claims(
    evidence: CommittedInstallDecisionEvidence,
) -> dict[str, object]:
    return {
        "committed_revision": evidence.committed_revision,
        "consent_id": evidence.consent_id,
        "decision": evidence.decision,
        "decision_basis": evidence.decision_basis,
        "event_content_digest": evidence.event_content_digest,
        "event_id": evidence.event_id,
        "policy_snapshot_digest": evidence.policy_snapshot_digest,
        "previous_record_digest": evidence.previous_record_digest,
        "record_digest": evidence.record_digest,
        "requested_action_content_digest": evidence.requested_action_content_digest,
        "requested_action_id": evidence.requested_action_id,
        "requested_action_kind": evidence.requested_action_kind,
        "requested_action_precondition_revision": (evidence.requested_action_precondition_revision),
        "result_state_digest": evidence.result_state_digest,
        "scope": evidence.scope.to_dict(),
        "schema": "ctx.committed-install-decision-evidence-v1",
        "stream_identity_digest": evidence.stream_identity_digest,
    }


def _install_decision_evidence_matches_query(
    evidence: CommittedInstallDecisionEvidence,
    query: InstallDecisionEvidenceQuery,
) -> bool:
    return bool(
        evidence.scope == query.scope
        and evidence.stream_identity_digest == query.stream_identity_digest
        and evidence.consent_id == query.consent_id
        and evidence.decision == query.decision
        and evidence.decision_basis == query.decision_basis
        and evidence.policy_snapshot_digest == query.policy_snapshot_digest
        and evidence.requested_action_id == query.requested_action_id
        and evidence.requested_action_kind == query.requested_action_kind
        and evidence.requested_action_content_digest == query.requested_action_content_digest
        and evidence.requested_action_precondition_revision
        == query.requested_action_precondition_revision
        and evidence.event_id == query.event_id
        and evidence.event_content_digest == query.event_content_digest
        and evidence.committed_revision == query.requested_action_precondition_revision
        and evidence.previous_record_digest == query.expected_head_record_digest
    )


def _canonical_json_text(value: str) -> str:
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("value must be valid JSON") from exc
    return _canonical_json(decoded)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_sha256(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _record_chain_json(record: JournalRecord) -> str:
    return _canonical_json(
        {
            "event_content_digest": record.event_content_digest,
            "event_id": record.event_id,
            "previous_record_digest": record.previous_record_digest,
            "privacy_classification": record.privacy_classification,
            "reducer_version": record.reducer_version,
            "replay_digest": record.replay_digest,
            "retention_class": record.retention_class,
            "result_state_digest": record.result_state_digest,
            "revision": record.revision,
            "stream_key": record.stream_id.key,
            "transition_digest": record.transition_digest,
        }
    )


def _install_claim_json(claim: InstallActionClaimRecord) -> str:
    return _canonical_json(
        {
            "action_content_digest": claim.action_content_digest,
            "action_expires_at": claim.action_expires_at,
            "action_id": claim.action_id,
            "action_json": claim.action_json,
            "action_kind": claim.action_kind,
            "authorization_digest": claim.authorization_digest,
            "claimed_at": claim.claimed_at,
            "claimed_head_record_digest": claim.claimed_head_record_digest,
            "claimed_head_revision": claim.claimed_head_revision,
            "claimed_head_state_digest": claim.claimed_head_state_digest,
            "issuing_record_digest": claim.issuing_record_digest,
            "execution_binding_digest": claim.execution_binding_digest,
            "execution_binding_json": claim.execution_binding_json,
            "precondition_revision": claim.precondition_revision,
            "stream_key": claim.stream_id.key,
        }
    )


def _install_outcome_json(outcome: InstallExecutionOutcomeRecord) -> str:
    return _canonical_json(
        {
            "action_content_digest": outcome.action_content_digest,
            "action_id": outcome.action_id,
            "claim_digest": outcome.claim_digest,
            "execution_binding_digest": outcome.execution_binding_digest,
            "observed_at": outcome.observed_at,
            "observed_material_identity_digest": (outcome.observed_material_identity_digest),
            "outcome": outcome.outcome,
            "stream_key": outcome.stream_id.key,
            "verification_digest": outcome.verification_digest,
        }
    )


def _activation_claim_json(claim: ActivationActionClaimRecord) -> str:
    return _canonical_json(
        {
            "action_content_digest": claim.action_content_digest,
            "action_expires_at": claim.action_expires_at,
            "action_id": claim.action_id,
            "action_json": claim.action_json,
            "authorization_digest": claim.authorization_digest,
            "claimed_at": claim.claimed_at,
            "claimed_head_record_digest": claim.claimed_head_record_digest,
            "claimed_head_revision": claim.claimed_head_revision,
            "claimed_head_state_digest": claim.claimed_head_state_digest,
            "execution_binding_digest": claim.execution_binding_digest,
            "execution_binding_json": claim.execution_binding_json,
            "issuing_record_digest": claim.issuing_record_digest,
            "precondition_revision": claim.precondition_revision,
            "stream_key": claim.stream_id.key,
        }
    )


def _activation_outcome_json(outcome: ActivationExecutionOutcomeRecord) -> str:
    return _canonical_json(
        {
            "action_content_digest": outcome.action_content_digest,
            "action_id": outcome.action_id,
            "claim_digest": outcome.claim_digest,
            "execution_binding_digest": outcome.execution_binding_digest,
            "observed_at": outcome.observed_at,
            "observed_material_identity_digest": outcome.observed_material_identity_digest,
            "stream_key": outcome.stream_id.key,
            "verification_digest": outcome.verification_digest,
        }
    )


def _canonical_utc_timestamp(value: datetime, field_name: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must return an aware UTC datetime")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field_name} must return an aware UTC datetime")
    normalized = value.astimezone(timezone.utc)
    result = normalized.strftime("%Y-%m-%dT%H:%M:%S")
    if normalized.microsecond:
        result += f".{normalized.microsecond:06d}".rstrip("0")
    return result + "Z"


def _parse_utc_timestamp(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an RFC 3339 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field_name} must be an RFC 3339 UTC timestamp")
    if _canonical_utc_timestamp(parsed, field_name) != value:
        raise ValueError(f"{field_name} must be a canonical RFC 3339 UTC timestamp")
    return parsed


def _stream_id_from_key(value: object) -> StreamId:
    if not isinstance(value, str) or _canonical_json_text(value) != value:
        raise ValueError("stream_key must be canonical JSON")
    decoded = json.loads(value)
    if not isinstance(decoded, dict) or set(decoded) != {
        "tenant_id",
        "workspace_id",
        "repository_id",
        "session_id",
    }:
        raise ValueError("stream_key has an invalid identity schema")
    stream_id = StreamId(
        tenant_id=decoded["tenant_id"],
        workspace_id=decoded["workspace_id"],
        repository_id=decoded["repository_id"],
        session_id=decoded["session_id"],
    )
    if stream_id.key != value:
        raise ValueError("stream_key identity is not canonical")
    return stream_id


def _claim_row_to_record(row: sqlite3.Row) -> InstallActionClaimRecord:
    try:
        return InstallActionClaimRecord(
            stream_id=_stream_id_from_key(row["stream_key"]),
            action_id=row["action_id"],
            action_content_digest=row["action_content_digest"],
            action_kind=row["action_kind"],
            precondition_revision=row["precondition_revision"],
            action_json=row["action_json"],
            issuing_record_digest=row["issuing_record_digest"],
            claimed_head_revision=row["claimed_head_revision"],
            claimed_head_record_digest=row["claimed_head_record_digest"],
            claimed_head_state_digest=row["claimed_head_state_digest"],
            action_expires_at=row["action_expires_at"],
            claimed_at=row["claimed_at"],
            authorization_digest=row["authorization_digest"],
            execution_binding_json=row["execution_binding_json"],
            execution_binding_digest=row["execution_binding_digest"],
            claim_digest=row["claim_digest"],
        )
    except Exception as exc:
        raise JournalCorruption("persisted install claim is invalid") from exc


def _load_claim(
    connection: sqlite3.Connection,
    stream_id: StreamId,
    action_id: str,
) -> InstallActionClaimRecord | None:
    row = connection.execute(
        """
        SELECT * FROM engine_install_claims
         WHERE stream_key = ? AND action_id = ?
        """,
        (stream_id.key, action_id),
    ).fetchone()
    return None if row is None else _claim_row_to_record(row)


def _insert_claim(connection: sqlite3.Connection, claim: InstallActionClaimRecord) -> None:
    connection.execute(
        """
        INSERT INTO engine_install_claims (
            stream_key, action_id, action_content_digest, action_kind,
            precondition_revision, action_json, issuing_record_digest,
            claimed_head_revision, claimed_head_record_digest,
            claimed_head_state_digest, action_expires_at, claimed_at,
            authorization_digest, execution_binding_json,
            execution_binding_digest, claim_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim.stream_id.key,
            claim.action_id,
            claim.action_content_digest,
            claim.action_kind,
            claim.precondition_revision,
            claim.action_json,
            claim.issuing_record_digest,
            claim.claimed_head_revision,
            claim.claimed_head_record_digest,
            claim.claimed_head_state_digest,
            claim.action_expires_at,
            claim.claimed_at,
            claim.authorization_digest,
            claim.execution_binding_json,
            claim.execution_binding_digest,
            claim.claim_digest,
        ),
    )


def _action_result_material_identity_digest(action: HostAction) -> str:
    from ctx.engine.content import MaterialIdentity

    raw = action.payload.get("result_material")
    if not isinstance(raw, Mapping):
        raise JournalCorruption("install action has no typed result material")
    try:
        return MaterialIdentity.from_dict(raw).identity_digest
    except Exception as exc:
        raise JournalCorruption("install action result material is invalid") from exc


def _outcome_row_to_record(row: sqlite3.Row) -> InstallExecutionOutcomeRecord:
    try:
        return InstallExecutionOutcomeRecord(
            stream_id=_stream_id_from_key(row["stream_key"]),
            action_id=row["action_id"],
            action_content_digest=row["action_content_digest"],
            claim_digest=row["claim_digest"],
            execution_binding_digest=row["execution_binding_digest"],
            outcome=row["outcome"],
            observed_material_identity_digest=row["observed_material_identity_digest"],
            verification_digest=row["verification_digest"],
            observed_at=row["observed_at"],
            outcome_digest=row["outcome_digest"],
        )
    except Exception as exc:
        raise JournalCorruption("persisted install execution outcome is invalid") from exc


def _load_install_outcome(
    connection: sqlite3.Connection,
    stream_id: StreamId,
    action_id: str,
) -> InstallExecutionOutcomeRecord | None:
    row = connection.execute(
        """
        SELECT * FROM engine_install_outcomes
         WHERE stream_key = ? AND action_id = ?
        """,
        (stream_id.key, action_id),
    ).fetchone()
    return None if row is None else _outcome_row_to_record(row)


def _insert_install_outcome(
    connection: sqlite3.Connection,
    outcome: InstallExecutionOutcomeRecord,
) -> None:
    connection.execute(
        """
        INSERT INTO engine_install_outcomes (
            stream_key, action_id, action_content_digest, claim_digest,
            execution_binding_digest, outcome,
            observed_material_identity_digest, verification_digest,
            observed_at, outcome_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            outcome.stream_id.key,
            outcome.action_id,
            outcome.action_content_digest,
            outcome.claim_digest,
            outcome.execution_binding_digest,
            outcome.outcome,
            outcome.observed_material_identity_digest,
            outcome.verification_digest,
            outcome.observed_at,
            outcome.outcome_digest,
        ),
    )


def _load_settlement(
    connection: sqlite3.Connection,
    stream_id: StreamId,
    action_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM engine_install_claim_settlements
         WHERE stream_key = ? AND action_id = ?
        """,
        (stream_id.key, action_id),
    ).fetchone()


def _activation_claim_row_to_record(row: sqlite3.Row) -> ActivationActionClaimRecord:
    try:
        return ActivationActionClaimRecord(
            stream_id=_stream_id_from_key(row["stream_key"]),
            action_id=row["action_id"],
            action_content_digest=row["action_content_digest"],
            precondition_revision=row["precondition_revision"],
            action_json=row["action_json"],
            issuing_record_digest=row["issuing_record_digest"],
            claimed_head_revision=row["claimed_head_revision"],
            claimed_head_record_digest=row["claimed_head_record_digest"],
            claimed_head_state_digest=row["claimed_head_state_digest"],
            action_expires_at=row["action_expires_at"],
            claimed_at=row["claimed_at"],
            authorization_digest=row["authorization_digest"],
            execution_binding_json=row["execution_binding_json"],
            execution_binding_digest=row["execution_binding_digest"],
            claim_digest=row["claim_digest"],
        )
    except Exception as exc:
        raise JournalCorruption("persisted activation claim is invalid") from exc


def _load_activation_claim(
    connection: sqlite3.Connection,
    stream_id: StreamId,
    action_id: str,
) -> ActivationActionClaimRecord | None:
    row = connection.execute(
        """
        SELECT * FROM engine_activation_claims
         WHERE stream_key = ? AND action_id = ?
        """,
        (stream_id.key, action_id),
    ).fetchone()
    return None if row is None else _activation_claim_row_to_record(row)


def _insert_activation_claim(
    connection: sqlite3.Connection,
    claim: ActivationActionClaimRecord,
) -> None:
    connection.execute(
        """
        INSERT INTO engine_activation_claims (
            stream_key, action_id, action_content_digest, precondition_revision,
            action_json, issuing_record_digest, claimed_head_revision,
            claimed_head_record_digest, claimed_head_state_digest,
            action_expires_at, claimed_at, authorization_digest,
            execution_binding_json, execution_binding_digest, claim_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim.stream_id.key,
            claim.action_id,
            claim.action_content_digest,
            claim.precondition_revision,
            claim.action_json,
            claim.issuing_record_digest,
            claim.claimed_head_revision,
            claim.claimed_head_record_digest,
            claim.claimed_head_state_digest,
            claim.action_expires_at,
            claim.claimed_at,
            claim.authorization_digest,
            claim.execution_binding_json,
            claim.execution_binding_digest,
            claim.claim_digest,
        ),
    )


def _activation_outcome_row_to_record(
    row: sqlite3.Row,
) -> ActivationExecutionOutcomeRecord:
    try:
        return ActivationExecutionOutcomeRecord(
            stream_id=_stream_id_from_key(row["stream_key"]),
            action_id=row["action_id"],
            action_content_digest=row["action_content_digest"],
            claim_digest=row["claim_digest"],
            execution_binding_digest=row["execution_binding_digest"],
            observed_material_identity_digest=row["observed_material_identity_digest"],
            verification_digest=row["verification_digest"],
            observed_at=row["observed_at"],
            outcome_digest=row["outcome_digest"],
        )
    except Exception as exc:
        raise JournalCorruption("persisted activation outcome is invalid") from exc


def _load_activation_outcome(
    connection: sqlite3.Connection,
    stream_id: StreamId,
    action_id: str,
) -> ActivationExecutionOutcomeRecord | None:
    row = connection.execute(
        """
        SELECT * FROM engine_activation_outcomes
         WHERE stream_key = ? AND action_id = ?
        """,
        (stream_id.key, action_id),
    ).fetchone()
    return None if row is None else _activation_outcome_row_to_record(row)


def _insert_activation_outcome(
    connection: sqlite3.Connection,
    outcome: ActivationExecutionOutcomeRecord,
) -> None:
    connection.execute(
        """
        INSERT INTO engine_activation_outcomes (
            stream_key, action_id, action_content_digest, claim_digest,
            execution_binding_digest, observed_material_identity_digest,
            verification_digest, observed_at, outcome_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            outcome.stream_id.key,
            outcome.action_id,
            outcome.action_content_digest,
            outcome.claim_digest,
            outcome.execution_binding_digest,
            outcome.observed_material_identity_digest,
            outcome.verification_digest,
            outcome.observed_at,
            outcome.outcome_digest,
        ),
    )


def _load_activation_settlement(
    connection: sqlite3.Connection,
    stream_id: StreamId,
    action_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT stream_key, action_id, action_content_digest, claim_digest,
               outcome_digest, receipt_event_id, receipt_event_content_digest,
               receipt_record_digest, settlement_digest
          FROM engine_activation_claim_settlements
         WHERE stream_key = ? AND action_id = ?
        """,
        (stream_id.key, action_id),
    ).fetchone()


def _activation_material_identity_digest(action: HostAction) -> str:
    from ctx.engine.content import MaterialIdentity

    raw = action.payload.get("material_identity")
    if not isinstance(raw, Mapping):
        raise JournalCorruption("activation action has no material identity")
    try:
        return MaterialIdentity.from_dict(raw).identity_digest
    except Exception as exc:
        raise JournalCorruption("activation action material identity is invalid") from exc


def _activation_install_observed_at(
    connection: sqlite3.Connection,
    *,
    stream_id: StreamId,
    state_json: str,
    action: HostAction,
) -> datetime:
    """Derive the activation lower clock bound from settled install authority."""

    from ctx.engine.state import CapabilityStateV3, EngineState

    state = EngineState.from_json(state_json)
    capability = state.capability(action.entity_id or "")
    if not isinstance(capability, CapabilityStateV3) or capability.installed_lineage is None:
        raise JournalCorruption("activation action has no installed material lineage")
    lineage = capability.installed_lineage
    rows = connection.execute(
        """
        SELECT action_id FROM engine_install_claims
         WHERE stream_key = ? AND action_content_digest = ?
        """,
        (stream_id.key, lineage.install_action_content_digest),
    ).fetchall()
    if len(rows) != 1:
        raise JournalCorruption("activation action has no one exact install claim")
    install_action_id = rows[0]["action_id"]
    if not isinstance(install_action_id, str):
        raise JournalCorruption("activation install claim identity is invalid")
    claim = _load_claim(connection, stream_id, install_action_id)
    outcome = _load_install_outcome(connection, stream_id, install_action_id)
    settlement = _load_settlement(connection, stream_id, install_action_id)
    if (
        claim is None
        or outcome is None
        or settlement is None
        or outcome.outcome != "applied"
        or settlement["outcome"] != "applied"
        or settlement["receipt_event_content_digest"] != lineage.install_receipt_content_digest
    ):
        raise JournalCorruption("activation action lacks a settled install observation")
    return _parse_utc_timestamp(outcome.observed_at, "install_outcome.observed_at")


def _validate_activation_claim_guard(
    connection: sqlite3.Connection,
    *,
    stream_id: StreamId,
    guard: ActivationActionClaimGuard,
) -> ActivationActionClaimRecord | None:
    if _load_activation_settlement(connection, stream_id, guard.action_id) is not None:
        raise ActivationActionClaimSettled(
            f"activation action {guard.action_id!r} is already settled"
        )
    claim = _load_activation_claim(connection, stream_id, guard.action_id)
    if guard.mode == "expired":
        # Retirement is authorized only while no durable claim exists: an
        # existing claim means a driver may already have mutated the host, so
        # the action must settle through its verified outcome instead.  This
        # test runs inside the caller's BEGIN IMMEDIATE transaction and is the
        # authoritative "no claim implies no host mutation" check.
        if claim is not None:
            raise ActivationActionAlreadyClaimed(
                f"activation action {guard.action_id!r} is already claimed"
            )
        return None
    outcome = _load_activation_outcome(connection, stream_id, guard.action_id)
    if claim is None or outcome is None:
        raise ActivationExecutionOutcomeRequired(
            f"activation action {guard.action_id!r} has no verified outcome"
        )
    if (
        claim.action_content_digest != guard.action_content_digest
        or outcome.claim_digest != claim.claim_digest
        or outcome.execution_binding_digest != claim.execution_binding_digest
        or outcome.outcome_digest != guard.execution_outcome_digest
    ):
        raise ActivationExecutionOutcomeConflict(
            "activation receipt guard does not match durable outcome"
        )
    return claim


def _validate_install_claim_guard(
    connection: sqlite3.Connection,
    *,
    stream_id: StreamId,
    guard: InstallActionClaimGuard,
) -> InstallActionClaimRecord | None:
    settlement = connection.execute(
        """
        SELECT 1 FROM engine_install_claim_settlements
         WHERE stream_key = ? AND action_id = ?
        """,
        (stream_id.key, guard.action_id),
    ).fetchone()
    if settlement is not None:
        raise InstallActionClaimSettled(
            f"install action {guard.action_id!r} already has a terminal settlement"
        )
    claim = _load_claim(connection, stream_id, guard.action_id)
    if guard.mode == "expired":
        if claim is not None:
            raise InstallActionAlreadyClaimed(
                f"claimed install action {guard.action_id!r} cannot expire as unclaimed"
            )
        return None
    if claim is None:
        raise InstallActionClaimRequired(
            f"install action {guard.action_id!r} must be claimed before receipt commit"
        )
    if claim.action_content_digest != guard.action_content_digest:
        raise JournalCorruption("install receipt guard does not match claimed action content")
    outcome = _load_install_outcome(connection, stream_id, guard.action_id)
    if outcome is None or guard.execution_outcome_digest is None:
        raise InstallExecutionOutcomeRequired(
            f"install action {guard.action_id!r} has no verified execution outcome"
        )
    if (
        outcome.action_content_digest != claim.action_content_digest
        or outcome.claim_digest != claim.claim_digest
        or outcome.execution_binding_digest != claim.execution_binding_digest
        or outcome.outcome != guard.mode
        or outcome.outcome_digest != guard.execution_outcome_digest
    ):
        raise InstallExecutionOutcomeConflict(
            "install receipt guard does not match the durable execution outcome"
        )
    return claim


def _validated_install_authority_rows(connection: sqlite3.Connection) -> None:
    _require_exact_table_schema(
        connection,
        table_name="engine_install_claims",
        expected=_INSTALL_CLAIM_SIGNATURE,
        expected_sql=_INSTALL_CLAIM_SCHEMA,
    )
    _require_exact_table_schema(
        connection,
        table_name="engine_install_outcomes",
        expected=_INSTALL_OUTCOME_SIGNATURE,
        expected_sql=_INSTALL_OUTCOME_SCHEMA,
    )
    _require_exact_table_schema(
        connection,
        table_name="engine_install_claim_settlements",
        expected=_INSTALL_SETTLEMENT_SIGNATURE,
        expected_sql=_INSTALL_SETTLEMENT_SCHEMA,
    )
    try:
        claims = {
            (claim.stream_id.key, claim.action_id): claim
            for claim in (
                _claim_row_to_record(row)
                for row in connection.execute("SELECT * FROM engine_install_claims").fetchall()
            )
        }
        journals: dict[str, list[JournalRecord]] = {}
        for claim in claims.values():
            journal = journals.setdefault(
                claim.stream_id.key,
                SQLiteEngineStore._validated_journal(connection, claim.stream_id),
            )
            _validate_claim_journal_anchor(claim, journal)
        outcomes = {
            (outcome.stream_id.key, outcome.action_id): outcome
            for outcome in (
                _outcome_row_to_record(row)
                for row in connection.execute("SELECT * FROM engine_install_outcomes").fetchall()
            )
        }
        for key, outcome in outcomes.items():
            outcome_claim = claims.get(key)
            if outcome_claim is None:
                raise ValueError("execution outcome has no matching claim")
            if (
                outcome.action_content_digest != outcome_claim.action_content_digest
                or outcome.claim_digest != outcome_claim.claim_digest
                or outcome.execution_binding_digest != outcome_claim.execution_binding_digest
                or _parse_utc_timestamp(outcome.observed_at, "outcome.observed_at")
                < _parse_utc_timestamp(outcome_claim.claimed_at, "claim.claimed_at")
            ):
                raise ValueError("execution outcome does not match its install claim")
            expected_material = _action_result_material_identity_digest(
                HostAction.from_json(outcome_claim.action_json)
            )
            if outcome.outcome == "applied" and (
                outcome.observed_material_identity_digest != expected_material
            ):
                raise ValueError("applied execution outcome has wrong material identity")
        for row in connection.execute("SELECT * FROM engine_install_claim_settlements").fetchall():
            stream_id = _stream_id_from_key(row["stream_key"])
            action_id = row["action_id"]
            if not isinstance(action_id, str) or not action_id or action_id != action_id.strip():
                raise ValueError("settlement action_id is invalid")
            settlement_claim = claims.get((stream_id.key, action_id))
            if settlement_claim is None:
                raise ValueError("settlement has no matching claim")
            for field_name in (
                "action_content_digest",
                "claim_digest",
                "receipt_event_content_digest",
                "receipt_record_digest",
                "settlement_digest",
            ):
                _require_sha256(row[field_name], field_name)
            if row["action_content_digest"] != settlement_claim.action_content_digest:
                raise ValueError("settlement action digest does not match claim")
            if row["claim_digest"] != settlement_claim.claim_digest:
                raise ValueError("settlement claim digest does not match claim")
            if row["outcome"] not in {"applied", "failed"}:
                raise ValueError("settlement outcome is invalid")
            settlement_outcome = outcomes.get((stream_id.key, action_id))
            if settlement_outcome is None:
                raise ValueError("settlement has no verified execution outcome")
            if (
                settlement_outcome.claim_digest != settlement_claim.claim_digest
                or settlement_outcome.outcome != row["outcome"]
            ):
                raise ValueError("settlement does not match its execution outcome")
            receipt_event_id = row["receipt_event_id"]
            if (
                not isinstance(receipt_event_id, str)
                or not receipt_event_id
                or receipt_event_id != receipt_event_id.strip()
            ):
                raise ValueError("settlement receipt_event_id is invalid")
            values = {
                "action_content_digest": row["action_content_digest"],
                "action_id": action_id,
                "claim_digest": row["claim_digest"],
                "outcome": row["outcome"],
                "receipt_event_content_digest": row["receipt_event_content_digest"],
                "receipt_event_id": receipt_event_id,
                "receipt_record_digest": row["receipt_record_digest"],
                "stream_key": stream_id.key,
            }
            if row["settlement_digest"] != _sha256(_canonical_json(values)):
                raise ValueError("settlement digest does not match settlement content")
            journal = journals.setdefault(
                stream_id.key,
                SQLiteEngineStore._validated_journal(connection, stream_id),
            )
            _validate_settlement_journal_anchor(row, settlement_claim, journal)
    except JournalCorruption:
        raise
    except Exception as exc:
        raise JournalCorruption("persisted install claim authority is invalid") from exc


def _validated_activation_authority_rows(connection: sqlite3.Connection) -> None:
    for table_name, expected, expected_sql in (
        (
            "engine_activation_claims",
            _ACTIVATION_CLAIM_SIGNATURE,
            _ACTIVATION_CLAIM_SCHEMA,
        ),
        (
            "engine_activation_outcomes",
            _ACTIVATION_OUTCOME_SIGNATURE,
            _ACTIVATION_OUTCOME_SCHEMA,
        ),
        (
            "engine_activation_claim_settlements",
            _ACTIVATION_SETTLEMENT_SIGNATURE,
            _ACTIVATION_SETTLEMENT_SCHEMA,
        ),
    ):
        _require_exact_table_schema(
            connection,
            table_name=table_name,
            expected=expected,
            expected_sql=expected_sql,
        )
    try:
        claims = {
            (claim.stream_id.key, claim.action_id): claim
            for claim in (
                _activation_claim_row_to_record(row)
                for row in connection.execute("SELECT * FROM engine_activation_claims").fetchall()
            )
        }
        journals: dict[str, list[JournalRecord]] = {}
        for claim in claims.values():
            journal = journals.setdefault(
                claim.stream_id.key,
                SQLiteEngineStore._validated_journal(connection, claim.stream_id),
            )
            _validate_activation_claim_journal_anchor(connection, claim, journal)
        outcomes = {
            (outcome.stream_id.key, outcome.action_id): outcome
            for outcome in (
                _activation_outcome_row_to_record(row)
                for row in connection.execute("SELECT * FROM engine_activation_outcomes").fetchall()
            )
        }
        for key, outcome in outcomes.items():
            outcome_claim = claims.get(key)
            if outcome_claim is None:
                raise ValueError("activation outcome has no claim")
            action = HostAction.from_json(outcome_claim.action_json)
            if (
                outcome.action_content_digest != outcome_claim.action_content_digest
                or outcome.claim_digest != outcome_claim.claim_digest
                or outcome.execution_binding_digest != outcome_claim.execution_binding_digest
                or outcome.observed_material_identity_digest
                != _activation_material_identity_digest(action)
                or _parse_utc_timestamp(outcome.observed_at, "outcome.observed_at")
                < _parse_utc_timestamp(outcome_claim.claimed_at, "claim.claimed_at")
                or _parse_utc_timestamp(outcome.observed_at, "outcome.observed_at")
                >= _parse_utc_timestamp(
                    outcome_claim.action_expires_at,
                    "claim.action_expires_at",
                )
            ):
                raise ValueError("activation outcome does not match claim")
        for row in connection.execute(
            "SELECT * FROM engine_activation_claim_settlements"
        ).fetchall():
            stream_id = _stream_id_from_key(row["stream_key"])
            key = (stream_id.key, row["action_id"])
            settlement_claim = claims.get(key)
            settlement_outcome = outcomes.get(key)
            if settlement_claim is None or settlement_outcome is None:
                raise ValueError("activation settlement lacks claim or outcome")
            values = {
                "action_content_digest": row["action_content_digest"],
                "action_id": row["action_id"],
                "claim_digest": row["claim_digest"],
                "outcome_digest": row["outcome_digest"],
                "receipt_event_content_digest": row["receipt_event_content_digest"],
                "receipt_event_id": row["receipt_event_id"],
                "receipt_record_digest": row["receipt_record_digest"],
                "stream_key": stream_id.key,
            }
            if (
                row["action_content_digest"] != settlement_claim.action_content_digest
                or row["claim_digest"] != settlement_claim.claim_digest
                or row["outcome_digest"] != settlement_outcome.outcome_digest
                or row["settlement_digest"] != _sha256(_canonical_json(values))
            ):
                raise ValueError("activation settlement does not match authority")
            journal = journals.setdefault(
                stream_id.key,
                SQLiteEngineStore._validated_journal(connection, stream_id),
            )
            _validate_activation_settlement_journal_anchor(
                row,
                settlement_claim,
                settlement_outcome,
                journal,
            )
    except JournalCorruption:
        raise
    except Exception as exc:
        raise JournalCorruption("persisted activation authority is invalid") from exc


def _validate_claim_journal_anchor(
    claim: InstallActionClaimRecord,
    journal: list[JournalRecord],
) -> None:
    from ctx.engine.installation import (
        InstallExecutionBinding,
        install_action_authorization_digest,
    )
    from ctx.engine.planning_v3 import InstallPlanningAuthority
    from ctx.engine.state import CapabilityStateV3, CommittedPlanV3, EngineState

    issuing = next(
        (record for record in journal if record.revision == claim.precondition_revision),
        None,
    )
    claimed_head = next(
        (record for record in journal if record.revision == claim.claimed_head_revision),
        None,
    )
    if issuing is None or issuing.record_digest != claim.issuing_record_digest:
        raise JournalCorruption("install claim issuing record is not journal-anchored")
    if (
        claimed_head is None
        or claimed_head.record_digest != claim.claimed_head_record_digest
        or claimed_head.result_state_digest != claim.claimed_head_state_digest
    ):
        raise JournalCorruption("install claim head is not journal-anchored")
    action = HostAction.from_json(claim.action_json)
    emitted = [
        candidate
        for candidate in Transition.from_json(issuing.transition_json).actions
        if candidate.action_id == claim.action_id and candidate.to_json() == claim.action_json
    ]
    if len(emitted) != 1 or action.content_digest != claim.action_content_digest:
        raise JournalCorruption("install claim action is not exactly journal-emitted")
    state = EngineState.from_json(claimed_head.result_state_json)
    pending = [
        item
        for item in state.pending_effects
        if item.effect == "install"
        and item.action.action_id == claim.action_id
        and item.action.to_json() == claim.action_json
    ]
    capability = state.capability(action.entity_id or "")
    committed_plan = state.committed_plan
    if (
        len(pending) != 1
        or not isinstance(capability, CapabilityStateV3)
        or capability.installation != "absent"
        or capability.activation != "inactive"
        or capability.current_authorized_material is not None
        or not isinstance(committed_plan, CommittedPlanV3)
        or action.payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3
    ):
        raise JournalCorruption("install claim head has no exact pending schema-v3 install")
    selection = capability.selection.selection
    authority = selection.authority
    if not isinstance(authority, InstallPlanningAuthority):
        raise JournalCorruption("install claim selection has no install authority")
    descriptor = authority.descriptor
    execution_binding = InstallExecutionBinding.from_json(claim.execution_binding_json)
    catalog_snapshot_digest = capability.catalog_snapshot_id
    policy_snapshot_digest = state.install_policy_snapshot_digest
    if (
        policy_snapshot_digest is None
        or capability.material_identity != authority.result_material
        or capability.plan_id != action.plan_id
        or committed_plan.plan_id != action.plan_id
        or committed_plan.catalog_snapshot_id != catalog_snapshot_digest
        or not any(row.selection == selection for row in committed_plan.capabilities)
        or action.catalog_snapshot_id != catalog_snapshot_digest
        or action.payload.get("policy_snapshot_digest") != policy_snapshot_digest
        or action.payload.get("catalog_identity") != selection.catalog_identity.to_dict()
        or action.payload.get("result_material") != authority.result_material.to_dict()
        or action.payload.get("install_plan_descriptor") != descriptor.to_dict()
        or execution_binding.driver_id != descriptor.installer_id
        or execution_binding.driver_digest != action.payload.get("installer_digest")
    ):
        raise JournalCorruption("install claim authority is not exact journaled authority")
    expected_authorization = install_action_authorization_digest(
        action=action,
        selection=selection,
        descriptor=descriptor,
        catalog_snapshot_digest=catalog_snapshot_digest,
        policy_snapshot_digest=policy_snapshot_digest,
    )
    if claim.authorization_digest != expected_authorization:
        raise JournalCorruption("install claim authorization digest is not journal-derived")


def _validate_activation_claim_journal_anchor(
    connection: sqlite3.Connection,
    claim: ActivationActionClaimRecord,
    journal: list[JournalRecord],
) -> None:
    from ctx.engine.installation import (
        InstallExecutionBinding,
        activation_action_authorization_digest,
    )
    from ctx.engine.state import CapabilityStateV3, EngineState

    issuing = next(
        (record for record in journal if record.revision == claim.precondition_revision),
        None,
    )
    claimed_head = next(
        (record for record in journal if record.revision == claim.claimed_head_revision),
        None,
    )
    if issuing is None or issuing.record_digest != claim.issuing_record_digest:
        raise JournalCorruption("activation claim issuing record is not anchored")
    if (
        claimed_head is None
        or claimed_head.record_digest != claim.claimed_head_record_digest
        or claimed_head.result_state_digest != claim.claimed_head_state_digest
    ):
        raise JournalCorruption("activation claim head is not anchored")
    action = HostAction.from_json(claim.action_json)
    emitted = tuple(
        candidate
        for candidate in Transition.from_json(issuing.transition_json).actions
        if candidate.action_id == claim.action_id and candidate.to_json() == claim.action_json
    )
    state = EngineState.from_json(claimed_head.result_state_json)
    capability = state.capability(action.entity_id or "")
    current = (
        None
        if not isinstance(capability, CapabilityStateV3)
        else capability.current_authorized_material
    )
    pending = tuple(
        item
        for item in state.pending_effects
        if item.effect == "activate" and item.action.to_json() == action.to_json()
    )
    if (
        len(emitted) != 1
        or len(pending) != 1
        or not isinstance(capability, CapabilityStateV3)
        or capability.installation != "installed"
        or capability.activation != "inactive"
        or current is None
        or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
        or action.payload.get("capability_kind") != capability.kind
        or action.payload.get("catalog_identity") != capability.catalog_identity.to_dict()
        or action.payload.get("material_identity") != capability.material_identity.to_dict()
        or action.payload.get("authorized_material") != current.to_dict()
        or action.required_host_feature != "activation"
        or state.host_descriptor_digest is None
    ):
        raise JournalCorruption("activation claim lacks exact pending journal authority")
    binding = InstallExecutionBinding.from_json(claim.execution_binding_json)
    expected = activation_action_authorization_digest(
        action=action,
        execution_binding=binding,
        host_descriptor_digest=state.host_descriptor_digest,
    )
    if claim.authorization_digest != expected:
        raise JournalCorruption("activation authorization digest is not journal-derived")
    installed_at = _activation_install_observed_at(
        connection,
        stream_id=claim.stream_id,
        state_json=claimed_head.result_state_json,
        action=action,
    )
    if _parse_utc_timestamp(claim.claimed_at, "claim.claimed_at") < installed_at:
        raise JournalCorruption("activation claim predates settled installation")


def _validate_activation_settlement_journal_anchor(
    row: sqlite3.Row,
    claim: ActivationActionClaimRecord,
    outcome: ActivationExecutionOutcomeRecord,
    journal: list[JournalRecord],
) -> None:
    from ctx.engine.replay import ReplayInput

    receipt = next(
        (record for record in journal if record.event_id == row["receipt_event_id"]),
        None,
    )
    if (
        receipt is None
        or receipt.event_content_digest != row["receipt_event_content_digest"]
        or receipt.record_digest != row["receipt_record_digest"]
    ):
        raise JournalCorruption("activation settlement receipt is not journal-anchored")
    replay = ReplayInput.from_json(receipt.replay_json)
    replay.assert_record_binding(receipt)
    event = replay.reducer_event
    if (
        event.kind != "ActionApplied"
        or event.payload.get("action_id") != claim.action_id
        or event.payload.get("action_kind") != "ActivateCapability"
        or event.payload.get("action_content_digest") != claim.action_content_digest
        or event.payload.get("action_precondition_revision") != claim.precondition_revision
        or event.occurred_at != outcome.observed_at
    ):
        raise JournalCorruption("activation settlement receipt is not exact")


def _validate_settlement_journal_anchor(
    row: sqlite3.Row,
    claim: InstallActionClaimRecord,
    journal: list[JournalRecord],
) -> None:
    from ctx.engine.replay import ReplayInput

    receipt = next(
        (record for record in journal if record.event_id == row["receipt_event_id"]),
        None,
    )
    if (
        receipt is None
        or receipt.event_content_digest != row["receipt_event_content_digest"]
        or receipt.record_digest != row["receipt_record_digest"]
    ):
        raise JournalCorruption("install settlement receipt is not journal-anchored")
    replay = ReplayInput.from_json(receipt.replay_json)
    replay.assert_record_binding(receipt)
    event = replay.reducer_event
    expected_kind = "ActionApplied" if row["outcome"] == "applied" else "ActionFailed"
    if (
        event.kind != expected_kind
        or event.payload.get("action_id") != claim.action_id
        or event.payload.get("action_kind") != claim.action_kind
        or event.payload.get("action_content_digest") != claim.action_content_digest
        or event.payload.get("action_precondition_revision") != claim.precondition_revision
    ):
        raise JournalCorruption("install settlement does not match its exact reducer receipt")


def _require_exact_table_schema(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    expected: dict[str, tuple[str, int, int]],
    expected_sql: str,
) -> None:
    try:
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.Error as exc:
        raise JournalCorruption(f"cannot inspect security table {table_name}") from exc
    signature = {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in rows
    }
    if signature != expected:
        raise JournalCorruption(f"security table {table_name} has an invalid schema")
    schema_row = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name = ?",
        (table_name,),
    ).fetchone()
    if (
        schema_row is None
        or schema_row["type"] != "table"
        or _normalized_security_sql(schema_row["sql"])
        != _normalized_security_sql(expected_sql.replace("IF NOT EXISTS ", ""))
    ):
        raise JournalCorruption(f"security table {table_name} definition is invalid")
    indexes = connection.execute(f"PRAGMA index_list({table_name})").fetchall()
    if len(indexes) != 1:
        raise JournalCorruption(f"security table {table_name} has unexpected indexes")
    index = indexes[0]
    if int(index["unique"]) != 1 or str(index["origin"]) != "pk" or int(index["partial"]) != 0:
        raise JournalCorruption(f"security table {table_name} primary index is invalid")
    index_columns = connection.execute(f"PRAGMA index_info({index['name']})").fetchall()
    if [str(row["name"]) for row in index_columns] != ["stream_key", "action_id"]:
        raise JournalCorruption(f"security table {table_name} primary index is invalid")
    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
        (table_name,),
    ).fetchall()
    if triggers:
        raise JournalCorruption(f"security table {table_name} has unexpected triggers")


def _require_exact_store_schema(connection: sqlite3.Connection) -> None:
    """Reject absent, legacy, or augmented schemas without attempting a repair."""

    try:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
              FROM sqlite_master
             WHERE name NOT LIKE 'sqlite_%'
             ORDER BY type, name
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise JournalCorruption("engine database schema cannot be inspected") from exc
    actual: dict[tuple[str, str], tuple[str, object]] = {}
    for row in rows:
        key = (str(row["type"]), str(row["name"]))
        if key in actual:
            raise JournalCorruption("engine database schema contains duplicate objects")
        actual[key] = (str(row["tbl_name"]), row["sql"])
    if actual.keys() != _EXACT_SCHEMA_OBJECTS.keys():
        raise JournalCorruption("engine database does not have the exact required schema")
    for key, (expected_table, expected_sql) in _EXACT_SCHEMA_OBJECTS.items():
        actual_table, actual_sql = actual[key]
        if actual_table != expected_table or _normalized_security_sql(
            actual_sql
        ) != _normalized_security_sql(expected_sql.replace("IF NOT EXISTS ", "")):
            raise JournalCorruption("engine database does not have the exact required schema")


def _normalized_security_sql(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().rstrip(";").split())


def _projection_is_valid(connection: sqlite3.Connection, head: JournalRecord) -> bool:
    if not _projection_schema_is_valid(connection):
        return False
    try:
        row = connection.execute(
            """
            SELECT (
                tenant_id = ? AND workspace_id = ? AND repository_id = ? AND session_id = ?
                AND revision = ? AND state_json = ? AND state_digest = ?
                AND head_record_digest = ?
            ) AS projection_valid
              FROM engine_streams
             WHERE stream_key = ?
            """,
            (
                head.stream_id.tenant_id,
                head.stream_id.workspace_id,
                head.stream_id.repository_id,
                head.stream_id.session_id,
                head.revision,
                head.result_state_json,
                head.result_state_digest,
                head.record_digest,
                head.stream_id.key,
            ),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if _is_busy_error(exc):
            raise
        return False
    return row is not None and row["projection_valid"] == 1


def _row_to_record(row: sqlite3.Row, stream_id: StreamId) -> JournalRecord:
    try:
        record = JournalRecord(
            stream_id=stream_id,
            revision=int(row["revision"]),
            event_id=str(row["event_id"]),
            event_content_digest=str(row["event_content_digest"]),
            replay_json=str(row["replay_json"]),
            transition_json=str(row["transition_json"]),
            result_state_json=str(row["result_state_json"]),
            privacy_classification=str(row["privacy_classification"]),
            retention_class=str(row["retention_class"]),
            reducer_version=str(row["reducer_version"]),
            replay_digest=str(row["replay_digest"]),
            transition_digest=str(row["transition_digest"]),
            result_state_digest=str(row["result_state_digest"]),
            previous_record_digest=row["previous_record_digest"],
            record_digest=str(row["record_digest"]),
        )
        if record.record_digest != _sha256(_record_chain_json(record)):
            raise JournalCorruption(f"record digest mismatch at revision {record.revision}")
        return record
    except JournalCorruption:
        raise
    except Exception as exc:
        raise JournalCorruption("persisted journal record is invalid") from exc


def _require_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"engine database parent must be a real directory: {path}")
    if os.name == "nt":
        return
    if metadata.st_uid != os.geteuid():
        raise ValueError(f"engine database parent must be owned by the current user: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o700 != 0o700:
        raise ValueError(f"engine database parent must be owner-private (0700): {path}")


def _require_private_file(path: Path) -> None:
    metadata = _require_owned_regular_file(path)
    if os.name == "nt":
        return
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o600 != 0o600:
        raise ValueError(f"engine database file must be owner-private (0600): {path}")


def _secure_sqlite_files(path: Path) -> None:
    _require_private_file(path)
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        _secure_transient_sqlite_file(candidate)


def _require_secure_sqlite_files(path: Path) -> None:
    """Validate SQLite files for a read-only open without chmod or creation."""

    _require_private_file(path)
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            _require_private_file(candidate)
        except FileNotFoundError:
            pass


def _secure_transient_sqlite_file(path: Path) -> None:
    """Secure one WAL sidecar while tolerating SQLite removing it concurrently."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    for _ in range(_SQLITE_SIDECAR_SECURE_ATTEMPTS):
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError(f"engine database file must be a regular file: {path}") from None
            raise
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or stat.S_ISLNK(opened.st_mode):
                raise ValueError(f"engine database file must be a regular file: {path}")
            if os.name != "nt" and opened.st_uid != os.geteuid():
                raise ValueError(f"engine database file must be owned by the current user: {path}")
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            try:
                current = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            if not os.path.samestat(opened, current):
                continue
            if os.name != "nt" and stat.S_IMODE(os.fstat(descriptor).st_mode) != _PRIVATE_FILE_MODE:
                raise ValueError(f"engine database file must be owner-private (0600): {path}")
            return
        finally:
            os.close(descriptor)
    raise StoreBusy("engine database sidecar remained unstable during concurrent access")


def _require_owned_regular_file(path: Path) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"engine database file must be a regular file: {path}")
    if os.name != "nt" and metadata.st_uid != os.geteuid():
        raise ValueError(f"engine database file must be owned by the current user: {path}")
    return metadata


def _ensure_projection_schema(connection: sqlite3.Connection) -> None:
    if _projection_schema_is_valid(connection):
        return
    connection.execute("DROP TABLE IF EXISTS engine_streams")
    connection.execute(_STREAM_SCHEMA)


def _projection_schema_is_valid(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("PRAGMA table_info(engine_streams)").fetchall()
    signature = {
        str(row["name"]): (
            str(row["type"]).upper(),
            int(row["notnull"]),
            int(row["pk"]),
        )
        for row in rows
    }
    return signature == _PROJECTION_SIGNATURE


def _is_busy_error(exc: sqlite3.Error) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _remove_projection_foreign_key(connection: sqlite3.Connection) -> None:
    if not connection.execute("PRAGMA foreign_key_list(engine_journal)").fetchall():
        return
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE engine_journal RENAME TO engine_journal_legacy_projection_fk"
        )
        connection.execute(_JOURNAL_SCHEMA)
        connection.execute(
            f"""
            INSERT INTO engine_journal ({_JOURNAL_COLUMNS})
            SELECT {_JOURNAL_COLUMNS}
              FROM engine_journal_legacy_projection_fk
            """
        )
        connection.execute("DROP TABLE engine_journal_legacy_projection_fk")
        connection.execute(_JOURNAL_INDEX_SCHEMA)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


__all__ = [
    "ActivationActionAlreadyClaimed",
    "ActivationActionClaimExpired",
    "ActivationActionClaimGuard",
    "ActivationActionClaimRecord",
    "ActivationActionClaimRequest",
    "ActivationActionClaimSettled",
    "ActivationExecutionOutcomeConflict",
    "ActivationExecutionOutcomeRecord",
    "ActivationExecutionOutcomeRequest",
    "ActivationExecutionOutcomeRequired",
    "ActivationExecutionStatus",
    "CommitResult",
    "EngineStoreError",
    "EventIdCollision",
    "InstallActionAlreadyClaimed",
    "InstallActionClaimExpired",
    "InstallActionClaimGuard",
    "InstallActionClaimRecord",
    "InstallActionClaimRequest",
    "InstallActionClaimRequired",
    "InstallActionClaimSettled",
    "InstallExecutionOutcomeConflict",
    "InstallExecutionOutcomeRecord",
    "InstallExecutionOutcomeRequest",
    "InstallExecutionOutcomeRequired",
    "InstallExecutionStatus",
    "JournalCorruption",
    "JournalRecord",
    "RevisionConflict",
    "SQLiteEngineStore",
    "StoreBusy",
    "StoredHead",
    "StreamId",
]
