"""Durable, host-neutral broker for authenticated interactive install consent.

The public challenge identifier is deliberately only a lookup key.  Authority
crosses this boundary solely as a process-bound :class:`VerifiedHumanDecision`
created after a trusted composition-supplied verifier authenticates an exact,
signed assertion.  Prompt text, model output, credentials, executable material,
raw proofs, and absolute workspace/release paths are never persistence inputs.
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Final, Protocol, runtime_checkable

from ctx.engine.capability_schema import validate_capability_identity
from ctx.engine.installation import (
    INSTALLABLE_CAPABILITY_KINDS,
    CommittedInstallDecisionEvidenceProvider,
    InstallDecisionEvidenceLookup,
    InstallDecisionEvidenceQuery,
    InstallDecisionEvidenceRejected,
    InteractiveInstallDecisionGuard,
    InteractiveInstallDecisionReservation,
)
from ctx.engine.protocol import ScopeRef
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import ensure_secure_directory, reject_symlink_path


_PRIVATE_FILE_MODE: Final = 0o600
_PRIVATE_DIRECTORY_MODE: Final = 0o700
_BUSY_TIMEOUT_MS: Final = 30_000
_DEFAULT_CAPACITY: Final = 4_096
_MAX_CAPACITY: Final = 65_536
_MAX_CHALLENGE_BYTES: Final = 16_384
_MAX_PROOF_BYTES: Final = 8_192
_MAX_DATABASE_BYTES: Final = 32 * 1024 * 1024
_MAX_TEXT_BYTES: Final = 256
_MAX_ID_BYTES: Final = 128
_SCHEMA_VERSION: Final = 4
_CHALLENGE_SCHEMA: Final = "ctx.install-consent-challenge-v2"
_ASSERTION_SCHEMA: Final = "ctx.signed-human-install-decision-v1"
_STORE_IDENTITY_SCHEMA: Final = "ctx.install-consent-broker-identity-v1"
_NONCE_RECORD_SCHEMA: Final = "ctx.install-consent-assertion-nonce-v1"
_NONCE_HISTORY_SCHEMA: Final = "ctx.install-consent-assertion-nonce-history-v1"
_NONCE_HISTORY_FACTOR: Final = 8
_STATES: Final = frozenset(
    {
        "pending",
        "decision-ready",
        "reauthentication-required",
        "reserved",
        "settled",
        "expired",
    }
)
_DECISIONS: Final = frozenset({"granted", "denied"})
_DIGEST_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_SQLITE_SIDECARS: Final = ("-wal", "-shm", "-journal")

_SCHEMA = """
CREATE TABLE consent_broker_identity (
    singleton                           INTEGER PRIMARY KEY NOT NULL CHECK(singleton = 1),
    audience                            TEXT NOT NULL,
    trusted_time_high_water             TEXT,
    identity_digest                     TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE consent_challenges (
    challenge_id                       TEXT PRIMARY KEY NOT NULL,
    challenge_digest                   TEXT NOT NULL,
    challenge_json                     BLOB NOT NULL,
    challenge_byte_length              INTEGER NOT NULL,
    state                              TEXT NOT NULL,
    created_at                         TEXT NOT NULL,
    decision                           TEXT,
    principal_digest                   TEXT,
    authenticator_id                   TEXT,
    audience                           TEXT,
    assertion_nonce_digest             TEXT,
    assertion_nonce_history_count      INTEGER NOT NULL,
    assertion_nonce_history_digest     TEXT NOT NULL,
    decision_issued_at                 TEXT,
    decision_expires_at                TEXT,
    decision_recorded_at               TEXT,
    reservation_event_id               TEXT,
    reservation_event_content_digest   TEXT,
    reservation_token_digest           TEXT,
    reserved_at                        TEXT,
    settled_at                         TEXT,
    expired_at                         TEXT,
    record_digest                      TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE consent_assertion_nonces (
    challenge_id                       TEXT NOT NULL,
    sequence                           INTEGER NOT NULL,
    nonce_digest                       TEXT NOT NULL UNIQUE,
    recorded_at                        TEXT NOT NULL,
    previous_record_digest             TEXT NOT NULL,
    record_digest                      TEXT NOT NULL,
    PRIMARY KEY (challenge_id, sequence),
    FOREIGN KEY (challenge_id) REFERENCES consent_challenges(challenge_id)
) WITHOUT ROWID;
"""

_EXPECTED_COLUMNS: Final = {
    "challenge_id": ("TEXT", 1, 1),
    "challenge_digest": ("TEXT", 1, 0),
    "challenge_json": ("BLOB", 1, 0),
    "challenge_byte_length": ("INTEGER", 1, 0),
    "state": ("TEXT", 1, 0),
    "created_at": ("TEXT", 1, 0),
    "decision": ("TEXT", 0, 0),
    "principal_digest": ("TEXT", 0, 0),
    "authenticator_id": ("TEXT", 0, 0),
    "audience": ("TEXT", 0, 0),
    "assertion_nonce_digest": ("TEXT", 0, 0),
    "assertion_nonce_history_count": ("INTEGER", 1, 0),
    "assertion_nonce_history_digest": ("TEXT", 1, 0),
    "decision_issued_at": ("TEXT", 0, 0),
    "decision_expires_at": ("TEXT", 0, 0),
    "decision_recorded_at": ("TEXT", 0, 0),
    "reservation_event_id": ("TEXT", 0, 0),
    "reservation_event_content_digest": ("TEXT", 0, 0),
    "reservation_token_digest": ("TEXT", 0, 0),
    "reserved_at": ("TEXT", 0, 0),
    "settled_at": ("TEXT", 0, 0),
    "expired_at": ("TEXT", 0, 0),
    "record_digest": ("TEXT", 1, 0),
}
_EXPECTED_IDENTITY_COLUMNS: Final = {
    "singleton": ("INTEGER", 1, 1),
    "audience": ("TEXT", 1, 0),
    "trusted_time_high_water": ("TEXT", 0, 0),
    "identity_digest": ("TEXT", 1, 0),
}
_EXPECTED_NONCE_COLUMNS: Final = {
    "challenge_id": ("TEXT", 1, 1),
    "sequence": ("INTEGER", 1, 2),
    "nonce_digest": ("TEXT", 1, 0),
    "recorded_at": ("TEXT", 1, 0),
    "previous_record_digest": ("TEXT", 1, 0),
    "record_digest": ("TEXT", 1, 0),
}
_ROW_COLUMNS: Final = tuple(_EXPECTED_COLUMNS)
_ROW_WITHOUT_DIGEST: Final = tuple(name for name in _ROW_COLUMNS if name != "record_digest")
_SELECT_ONE: Final = (
    f"SELECT {', '.join(_ROW_COLUMNS)} FROM consent_challenges WHERE challenge_id = ?"
)


class ConsentBrokerError(RuntimeError):
    """Base class for consent-broker failures."""


class ConsentBrokerUnavailable(ConsentBrokerError):
    """The secure filesystem or SQLite store is operationally unavailable."""


class ConsentBrokerCorruption(ConsentBrokerError):
    """Persisted schema or content violates the broker's integrity contract."""


class ConsentBrokerChallengeNotFound(ConsentBrokerError):
    """No authenticated durable challenge matches an exact public digest."""


class ConsentBrokerCapacityExceeded(ConsentBrokerError):
    """The bounded durable challenge capacity has been exhausted."""


class ConsentBrokerDecisionRejected(ConsentBrokerError):
    """An assertion, verifier, decision, or reservation failed closed."""


class ConsentBrokerReplay(ConsentBrokerDecisionRejected):
    """A public ID, decision, or reservation was replayed or concurrently reused."""


class ConsentBrokerExpired(ConsentBrokerDecisionRejected):
    """A challenge or authenticated decision has expired."""


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _store_identity_digest(audience: str, trusted_time_high_water: str | None) -> str:
    return _canonical_digest(
        {
            "audience": audience,
            "schema": _STORE_IDENTITY_SCHEMA,
            "trusted_time_high_water": trusted_time_high_water,
        }
    )


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _token(value: object, name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded canonical token")
    return value


def _bounded_text(value: object, name: str, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} must be non-empty text of at most {max_bytes} bytes")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean")
    return value


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"{name} must be a canonical UTC timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or _format_timestamp(parsed) != value:
        raise ValueError(f"{name} must be a canonical UTC timestamp")
    return parsed.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trusted time must be timezone-aware")
    normalized = value.astimezone(UTC)
    result = normalized.strftime("%Y-%m-%dT%H:%M:%S")
    if normalized.microsecond:
        result += f".{normalized.microsecond:06d}".rstrip("0")
    return result + "Z"


def _trusted_now(value: datetime) -> tuple[datetime, str]:
    encoded = _format_timestamp(value)
    return _parse_timestamp(encoded, "trusted time"), encoded


def _effective_trusted_now(
    connection: sqlite3.Connection,
    value: datetime,
) -> tuple[datetime, str]:
    submitted, _submitted_text = _trusted_now(value)
    row = connection.execute(
        "SELECT trusted_time_high_water FROM consent_broker_identity WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise ConsentBrokerCorruption("consent broker identity record is missing")
    persisted = row["trusted_time_high_water"]
    if persisted is None:
        return submitted, _format_timestamp(submitted)
    try:
        floor = _parse_timestamp(persisted, "trusted_time_high_water")
    except (TypeError, ValueError) as exc:
        raise ConsentBrokerCorruption("consent broker trusted-time high-water is invalid") from exc
    effective = max(submitted, floor)
    return effective, _format_timestamp(effective)


def _advance_trusted_time_high_water(
    connection: sqlite3.Connection,
    value: datetime,
    *,
    audience: str,
) -> tuple[datetime, str]:
    submitted, submitted_text = _trusted_now(value)
    effective, effective_text = _effective_trusted_now(connection, submitted)
    if effective == submitted:
        row = connection.execute(
            "SELECT trusted_time_high_water FROM consent_broker_identity WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise ConsentBrokerCorruption("consent broker identity record is missing")
        persisted = row["trusted_time_high_water"]
        if persisted is None or _parse_timestamp(persisted, "trusted_time_high_water") < submitted:
            connection.execute(
                "UPDATE consent_broker_identity "
                "SET trusted_time_high_water = ?, identity_digest = ? WHERE singleton = 1",
                (
                    submitted_text,
                    _store_identity_digest(audience, submitted_text),
                ),
            )
    return effective, effective_text


def _validate_scope(scope: ScopeRef) -> None:
    if not isinstance(scope, ScopeRef):
        raise TypeError("scope must be a ScopeRef")
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


def _mark_reauthentication_required(values: dict[str, object]) -> None:
    """Clear reservation authority while retaining the exact human identity."""

    values.update(
        {
            "state": "reauthentication-required",
            "reservation_event_id": None,
            "reservation_event_content_digest": None,
            "reservation_token_digest": None,
            "reserved_at": None,
            "expired_at": None,
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallConsentChallenge:
    """Exact immutable identity shown to a human before an install decision."""

    challenge_id: str
    audience: str
    workspace_identity_digest: str
    scope: ScopeRef
    capability_id: str
    kind: str
    source_digest: str
    catalog_snapshot_digest: str
    plan_id: str
    install_plan_digest: str
    descriptor_digest: str
    execution_binding_digest: str
    selection_digest: str
    material_identity_digest: str
    requested_action_id: str
    requested_action_kind: str
    requested_action_content_digest: str
    requested_action_precondition_revision: int
    policy_snapshot_digest: str
    release_root_digest: str
    permission_expansion: bool
    credential_requirement: bool
    expires_at: str
    challenge_digest: str = field(init=False)

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "capability_id",
            "audience",
            "catalog_snapshot_digest",
            "challenge_digest",
            "challenge_id",
            "credential_requirement",
            "descriptor_digest",
            "execution_binding_digest",
            "expires_at",
            "install_plan_digest",
            "kind",
            "material_identity_digest",
            "permission_expansion",
            "plan_id",
            "policy_snapshot_digest",
            "release_root_digest",
            "requested_action_content_digest",
            "requested_action_id",
            "requested_action_kind",
            "requested_action_precondition_revision",
            "schema",
            "selection_digest",
            "scope",
            "source_digest",
            "workspace_identity_digest",
        }
    )

    def __post_init__(self) -> None:
        _token(self.challenge_id, "challenge_id")
        _token(self.audience, "audience")
        _digest(self.workspace_identity_digest, "workspace_identity_digest")
        _validate_scope(self.scope)
        capability_id, kind = validate_capability_identity(self.capability_id, self.kind)
        if kind not in INSTALLABLE_CAPABILITY_KINDS:
            raise ValueError("kind is not an installable capability kind")
        if len(capability_id.encode("utf-8")) > _MAX_TEXT_BYTES:
            raise ValueError("capability_id exceeds its bounded size")
        for name in (
            "source_digest",
            "catalog_snapshot_digest",
            "install_plan_digest",
            "descriptor_digest",
            "execution_binding_digest",
            "selection_digest",
            "material_identity_digest",
            "requested_action_content_digest",
            "policy_snapshot_digest",
            "release_root_digest",
        ):
            _digest(getattr(self, name), name)
        for name in ("plan_id", "requested_action_id"):
            _token(getattr(self, name), name)
        _bounded_text(self.requested_action_kind, "requested_action_kind", max_bytes=_MAX_ID_BYTES)
        if self.requested_action_kind != "InstallCapability":
            raise ValueError("requested_action_kind must be InstallCapability")
        if (
            type(self.requested_action_precondition_revision) is not int
            or self.requested_action_precondition_revision < 1
        ):
            raise ValueError("requested_action_precondition_revision must be positive")
        _boolean(self.permission_expansion, "permission_expansion")
        _boolean(self.credential_requirement, "credential_requirement")
        _parse_timestamp(self.expires_at, "expires_at")
        object.__setattr__(self, "challenge_digest", _canonical_digest(self._digest_mapping()))
        if len(self.to_json().encode("utf-8")) > _MAX_CHALLENGE_BYTES:
            raise ValueError("install consent challenge exceeds its bounded size")

    def _digest_mapping(self) -> dict[str, object]:
        return {
            "audience": self.audience,
            "capability_id": self.capability_id,
            "catalog_snapshot_digest": self.catalog_snapshot_digest,
            "challenge_id": self.challenge_id,
            "credential_requirement": self.credential_requirement,
            "descriptor_digest": self.descriptor_digest,
            "execution_binding_digest": self.execution_binding_digest,
            "expires_at": self.expires_at,
            "install_plan_digest": self.install_plan_digest,
            "kind": self.kind,
            "material_identity_digest": self.material_identity_digest,
            "permission_expansion": self.permission_expansion,
            "plan_id": self.plan_id,
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "release_root_digest": self.release_root_digest,
            "requested_action_content_digest": self.requested_action_content_digest,
            "requested_action_id": self.requested_action_id,
            "requested_action_kind": self.requested_action_kind,
            "requested_action_precondition_revision": self.requested_action_precondition_revision,
            "schema": _CHALLENGE_SCHEMA,
            "selection_digest": self.selection_digest,
            "scope": self.scope.to_dict(),
            "source_digest": self.source_digest,
            "workspace_identity_digest": self.workspace_identity_digest,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._digest_mapping(), "challenge_digest": self.challenge_digest}

    def to_json(self) -> str:
        return _canonical_bytes(self.to_dict()).decode("ascii")

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> InstallConsentChallenge:
        if set(value) != cls._FIELDS:
            raise ValueError("install consent challenge fields are not exact")
        if value.get("schema") != _CHALLENGE_SCHEMA:
            raise ValueError("install consent challenge schema is unsupported")
        scope_value = value.get("scope")
        if not isinstance(scope_value, Mapping):
            raise ValueError("install consent challenge scope is invalid")
        challenge = cls(
            challenge_id=value["challenge_id"],  # type: ignore[arg-type]
            audience=value["audience"],  # type: ignore[arg-type]
            workspace_identity_digest=value["workspace_identity_digest"],  # type: ignore[arg-type]
            scope=ScopeRef.from_dict(scope_value),
            capability_id=value["capability_id"],  # type: ignore[arg-type]
            kind=value["kind"],  # type: ignore[arg-type]
            source_digest=value["source_digest"],  # type: ignore[arg-type]
            catalog_snapshot_digest=value["catalog_snapshot_digest"],  # type: ignore[arg-type]
            plan_id=value["plan_id"],  # type: ignore[arg-type]
            install_plan_digest=value["install_plan_digest"],  # type: ignore[arg-type]
            descriptor_digest=value["descriptor_digest"],  # type: ignore[arg-type]
            execution_binding_digest=value["execution_binding_digest"],  # type: ignore[arg-type]
            selection_digest=value["selection_digest"],  # type: ignore[arg-type]
            material_identity_digest=value["material_identity_digest"],  # type: ignore[arg-type]
            requested_action_id=value["requested_action_id"],  # type: ignore[arg-type]
            requested_action_kind=value["requested_action_kind"],  # type: ignore[arg-type]
            requested_action_content_digest=value["requested_action_content_digest"],  # type: ignore[arg-type]
            requested_action_precondition_revision=value["requested_action_precondition_revision"],  # type: ignore[arg-type]
            policy_snapshot_digest=value["policy_snapshot_digest"],  # type: ignore[arg-type]
            release_root_digest=value["release_root_digest"],  # type: ignore[arg-type]
            permission_expansion=value["permission_expansion"],  # type: ignore[arg-type]
            credential_requirement=value["credential_requirement"],  # type: ignore[arg-type]
            expires_at=value["expires_at"],  # type: ignore[arg-type]
        )
        if challenge.challenge_digest != value.get("challenge_digest"):
            raise ValueError("install consent challenge digest does not match its fields")
        return challenge


@dataclass(frozen=True, slots=True, kw_only=True)
class SignedHumanDecisionAssertion:
    """Bounded signed assertion submitted to a trusted verifier port."""

    challenge_digest: str
    decision: str
    principal_digest: str
    authenticator_id: str
    audience: str
    nonce: str
    issued_at: str
    expires_at: str
    proof: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _digest(self.challenge_digest, "challenge_digest")
        if self.decision not in _DECISIONS:
            raise ValueError("decision must be granted or denied")
        _digest(self.principal_digest, "principal_digest")
        _token(self.authenticator_id, "authenticator_id")
        _token(self.audience, "audience")
        _bounded_text(self.nonce, "nonce", max_bytes=_MAX_TEXT_BYTES)
        issued = _parse_timestamp(self.issued_at, "issued_at")
        expires = _parse_timestamp(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("assertion expires_at must be after issued_at")
        if not isinstance(self.proof, bytes) or not 1 <= len(self.proof) <= _MAX_PROOF_BYTES:
            raise ValueError(f"proof must be 1..{_MAX_PROOF_BYTES} bytes")
        if len(self.signing_bytes()) > _MAX_CHALLENGE_BYTES:
            raise ValueError("signed assertion exceeds its bounded size")

    def signing_bytes(self) -> bytes:
        """Return exact canonical claims bytes; proof is intentionally excluded."""

        return _canonical_bytes(
            {
                "audience": self.audience,
                "authenticator_id": self.authenticator_id,
                "challenge_digest": self.challenge_digest,
                "decision": self.decision,
                "expires_at": self.expires_at,
                "issued_at": self.issued_at,
                "nonce": self.nonce,
                "principal_digest": self.principal_digest,
                "schema": _ASSERTION_SCHEMA,
            }
        )


@runtime_checkable
class HumanDecisionVerifier(Protocol):
    """Trusted-composition port for authenticating a signed human assertion.

    Implementations must verify the proof over ``signing_bytes`` and enforce
    authenticator-specific nonce/replay policy.  This port is never satisfied by
    prompt text, model output, the public challenge ID, or a truthy non-boolean.
    Production composition must inject an authenticator-backed implementation;
    this module intentionally provides none.
    """

    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool: ...


@dataclass(frozen=True, slots=True, init=False)
class VerifiedHumanDecision:
    """Opaque, non-serializable authority issued by one live broker instance."""

    challenge_id: str
    challenge_digest: str
    decision: str
    principal_digest: str
    authenticator_id: str
    audience: str
    assertion_nonce_digest: str
    issued_at: str
    expires_at: str
    _issuer_identity: object = field(repr=False, compare=False)
    _seal: str = field(repr=False, compare=False)

    def __init__(self) -> None:
        raise TypeError("VerifiedHumanDecision can only be issued by a consent broker")


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallConsentChallengeRecord:
    """Privacy-safe durable status for one challenge."""

    challenge: InstallConsentChallenge
    state: str
    created_at: str
    decision: str | None
    principal_digest: str | None
    authenticator_id: str | None
    audience: str | None
    assertion_nonce_digest: str | None
    decision_issued_at: str | None
    decision_expires_at: str | None
    decision_recorded_at: str | None
    reservation_event_id: str | None
    reservation_event_content_digest: str | None
    reserved_at: str | None
    settled_at: str | None
    expired_at: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsentBrokerRecoveryReport:
    """Result of a bounded fail-closed expiry/recovery sweep."""

    expired: int
    retained_reserved: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ConsentBrokerReconciliationReport:
    """Fail-closed result of reconciling broker state with engine truth."""

    outcome: str
    journal_status: str
    record: InstallConsentChallengeRecord

    def __post_init__(self) -> None:
        if self.outcome not in {
            "settled",
            "reauthentication-required",
            "expired",
            "quarantined",
        }:
            raise ValueError("consent broker reconciliation outcome is invalid")
        if self.journal_status not in {
            "committed",
            "absent-at-expected-head",
            "head-advanced",
            "event-collision",
            "corrupt",
            "unavailable",
        }:
            raise ValueError("consent broker journal status is invalid")


class SQLiteInstallConsentBrokerStore:
    """Owner-private SQLite lifecycle for exact interactive install decisions."""

    def __init__(
        self,
        path: Path,
        *,
        audience: str,
        capacity: int = _DEFAULT_CAPACITY,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("consent broker database path must be absolute")
        self.audience = _token(audience, "audience")
        if type(capacity) is not int or not 1 <= capacity <= _MAX_CAPACITY:
            raise ValueError(f"capacity must be an integer in 1..{_MAX_CAPACITY}")
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be a positive integer")
        self._capacity = capacity
        self._busy_timeout_ms = busy_timeout_ms
        self.__issuer_identity = object()
        self.__issuer_key = secrets.token_bytes(32)
        self._prepare_parent()
        with secure_file_lock(self.path, timeout=self._busy_timeout_ms / 1000):
            created = self._prepare_path()
            with self._connect(initialize=created):
                pass
            self.__bound_metadata = _require_private_file(self.path)

    def create_challenge(self, challenge: InstallConsentChallenge, *, now: datetime) -> None:
        """Persist a pending challenge idempotently by its exact digest."""

        if not isinstance(challenge, InstallConsentChallenge):
            raise TypeError("challenge must be an InstallConsentChallenge")
        if challenge.audience != self.audience:
            raise ConsentBrokerDecisionRejected(
                "install consent challenge audience does not match this broker"
            )
        payload = challenge.to_json().encode("ascii")
        if len(payload) > _MAX_CHALLENGE_BYTES:
            raise ValueError("install consent challenge exceeds its bounded size")
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_text = _effective_trusted_now(connection, now)
                existing = connection.execute(_SELECT_ONE, (challenge.challenge_id,)).fetchone()
                if existing is not None:
                    values = self._validated_row(existing)
                    if values["challenge_digest"] != challenge.challenge_digest:
                        raise ConsentBrokerReplay(
                            "public challenge ID is already bound to a different challenge"
                        )
                    persisted = _challenge_from_values(values)
                    self._assert_challenge_live(
                        values,
                        persisted,
                        current,
                        current_text,
                        connection,
                    )
                    connection.execute("COMMIT")
                    return
                if current >= _parse_timestamp(challenge.expires_at, "challenge.expires_at"):
                    raise ConsentBrokerExpired("install consent challenge is already expired")
                count = connection.execute("SELECT count(*) FROM consent_challenges").fetchone()[0]
                if type(count) is not int or count < 0:
                    raise ConsentBrokerCorruption("challenge count is invalid")
                if count >= self._capacity:
                    raise ConsentBrokerCapacityExceeded(
                        "install consent broker reached its bounded capacity"
                    )
                values = _empty_row_values(
                    challenge=challenge,
                    challenge_json=payload,
                    created_at=current_text,
                )
                self._insert_row(connection, values)
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        self._require_bounded_database()

    def get(self, challenge_id: str, *, now: datetime) -> InstallConsentChallengeRecord:
        """Read and fully validate one record without granting any authority."""

        _token(challenge_id, "challenge_id")
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_text = _effective_trusted_now(connection, now)
                row = connection.execute(_SELECT_ONE, (challenge_id,)).fetchone()
                if row is None:
                    raise KeyError(challenge_id)
                values = self._validated_row(row)
                challenge = _challenge_from_values(values)
                self._expire_if_due(values, challenge, current, current_text, connection)
                connection.execute("COMMIT")
                return _record_from_values(values)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def get_by_challenge_digest(
        self,
        challenge_digest: str,
        *,
        expected_workspace_identity_digest: str,
        expected_release_root_digest: str,
        now: datetime,
    ) -> InstallConsentChallengeRecord:
        """Find one exact authenticated challenge by its public digest.

        A signed human assertion carries the challenge digest, not the public
        challenge ID.  The durable store therefore performs a bounded scan of
        every challenge row, authenticates all rows before comparing any
        digest, and accepts exactly one match.  The result is the same
        privacy-safe status projection returned by :meth:`get`; it carries no
        process-bound decision or reservation authority.
        """

        _digest(challenge_digest, "challenge_digest")
        _digest(expected_workspace_identity_digest, "expected_workspace_identity_digest")
        _digest(expected_release_root_digest, "expected_release_root_digest")
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_text = _effective_trusted_now(connection, now)
                rows = connection.execute(
                    f"SELECT {', '.join(_ROW_COLUMNS)} FROM consent_challenges "
                    "ORDER BY challenge_id LIMIT ?",
                    (_MAX_CAPACITY + 1,),
                ).fetchall()
                if len(rows) > _MAX_CAPACITY:
                    raise ConsentBrokerCorruption(
                        "consent broker challenge count exceeds its absolute bound"
                    )
                # Deliberately keep validation and comparison as separate
                # phases: a valid early match must never mask corruption in an
                # unrelated later row.
                authenticated = tuple(self._validated_row(row) for row in rows)
                matches = tuple(
                    values
                    for values in authenticated
                    if hmac.compare_digest(
                        _required_digest_value(
                            values["challenge_digest"],
                            "challenge_digest",
                        ),
                        challenge_digest,
                    )
                )
                if not matches:
                    raise ConsentBrokerChallengeNotFound(
                        "install consent challenge digest was not found"
                    )
                if len(matches) != 1:
                    raise ConsentBrokerCorruption(
                        "challenge digest matches multiple authenticated challenges"
                    )
                values = matches[0]
                challenge = _challenge_from_values(values)
                if not hmac.compare_digest(
                    challenge.workspace_identity_digest,
                    expected_workspace_identity_digest,
                ) or not hmac.compare_digest(
                    challenge.release_root_digest,
                    expected_release_root_digest,
                ):
                    # A digest for another workspace or release is not an
                    # enumerable object in this service's authority domain.
                    # Raise before expiry/high-water mutations can commit.
                    raise ConsentBrokerChallengeNotFound(
                        "install consent challenge digest was not found"
                    )
                self._expire_if_due(values, challenge, current, current_text, connection)
                connection.execute("COMMIT")
                return _record_from_values(values)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def inspect_record(self, challenge_id: str) -> InstallConsentChallengeRecord:
        """Read and validate one record without applying time-based transitions."""

        _token(challenge_id, "challenge_id")
        with self._locked_connection() as connection:
            connection.execute("BEGIN")
            try:
                row = connection.execute(_SELECT_ONE, (challenge_id,)).fetchone()
                if row is None:
                    raise KeyError(challenge_id)
                values = self._validated_row(row)
                connection.execute("COMMIT")
                return _record_from_values(values)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def verify_human_decision(
        self,
        challenge_id: str,
        assertion: SignedHumanDecisionAssertion,
        verifier: HumanDecisionVerifier,
        *,
        now: datetime,
    ) -> VerifiedHumanDecision:
        """Authenticate exact signed claims and issue process-bound authority."""

        _token(challenge_id, "challenge_id")
        if not isinstance(assertion, SignedHumanDecisionAssertion):
            raise TypeError("assertion must be a SignedHumanDecisionAssertion")
        if not isinstance(verifier, HumanDecisionVerifier):
            raise TypeError("verifier must implement the trusted HumanDecisionVerifier port")
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_text = _effective_trusted_now(connection, now)
                row = connection.execute(_SELECT_ONE, (challenge_id,)).fetchone()
                if row is None:
                    raise KeyError(challenge_id)
                values = self._validated_row(row)
                challenge = _challenge_from_values(values)
                state = values["state"]
                if state in {"reserved", "settled"}:
                    raise ConsentBrokerReplay(f"install consent decision is already {state}")
                self._assert_challenge_live(values, challenge, current, current_text, connection)
                # A decision TTL may expire while its enclosing challenge is
                # still live.  ``_assert_challenge_live`` durably moves that
                # case to reauthentication-required, so all matching and nonce
                # rules below must use the post-transition state.
                state = values["state"]
                if assertion.challenge_digest != challenge.challenge_digest:
                    raise ConsentBrokerDecisionRejected(
                        "signed assertion does not bind the exact challenge digest"
                    )
                if assertion.audience != self.audience:
                    raise ConsentBrokerDecisionRejected(
                        "signed assertion audience does not match this broker"
                    )
                issued = _parse_timestamp(assertion.issued_at, "assertion.issued_at")
                expires = _parse_timestamp(assertion.expires_at, "assertion.expires_at")
                if issued > current:
                    raise ConsentBrokerDecisionRejected("signed assertion was issued in the future")
                if expires > _parse_timestamp(challenge.expires_at, "challenge.expires_at"):
                    raise ConsentBrokerDecisionRejected(
                        "signed assertion outlives the exact install challenge"
                    )
                submitted_nonce_digest = hashlib.sha256(assertion.nonce.encode("utf-8")).hexdigest()
                if state == "decision-ready":
                    _require_reverification_matches(values, assertion)
                    nonce_digest = _required_digest_value(
                        values["assertion_nonce_digest"], "assertion_nonce_digest"
                    )
                    issued_at = _required_text_value(values["decision_issued_at"], "issued_at")
                    expires_at = _required_text_value(
                        values["decision_expires_at"], "decision_expires_at"
                    )
                else:
                    nonce_digest = submitted_nonce_digest
                    if (
                        state == "reauthentication-required"
                        and nonce_digest == values["assertion_nonce_digest"]
                    ):
                        raise ConsentBrokerDecisionRejected(
                            "reconciliation requires a fresh nonce in the signed assertion"
                        )
                    if state == "reauthentication-required":
                        _require_reverification_matches(values, assertion)
                    issued_at = assertion.issued_at
                    expires_at = assertion.expires_at
                nonce_owner = connection.execute(
                    "SELECT challenge_id FROM consent_assertion_nonces WHERE nonce_digest = ?",
                    (submitted_nonce_digest,),
                ).fetchone()
                if nonce_owner is not None:
                    raise ConsentBrokerReplay("verified human decision nonce was already recorded")
                try:
                    verified = verifier.verify_signed_assertion(
                        assertion,
                        signing_bytes=assertion.signing_bytes(),
                    )
                except Exception:
                    raise ConsentBrokerDecisionRejected(
                        "signed human decision verifier rejected the assertion"
                    ) from None
                if type(verified) is not bool or not verified:
                    raise ConsentBrokerDecisionRejected(
                        "signed human decision signature was not verified"
                    )
                current, current_text = _advance_trusted_time_high_water(
                    connection,
                    now,
                    audience=self.audience,
                )
                if current >= expires:
                    connection.execute("COMMIT")
                    raise ConsentBrokerExpired("signed assertion has expired")
                if state == "decision-ready":
                    self._record_assertion_nonce(
                        connection,
                        values=values,
                        challenge_id=challenge.challenge_id,
                        nonce_digest=submitted_nonce_digest,
                        recorded_at=current_text,
                    )
                    self._update_row(connection, values)
                decision = self._issue_verified_decision(
                    challenge_id=challenge.challenge_id,
                    challenge_digest=challenge.challenge_digest,
                    decision=assertion.decision,
                    principal_digest=assertion.principal_digest,
                    authenticator_id=assertion.authenticator_id,
                    audience=assertion.audience,
                    assertion_nonce_digest=nonce_digest,
                    issued_at=issued_at,
                    expires_at=expires_at,
                )
                connection.execute("COMMIT")
                return decision
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def mark_decision_ready(
        self,
        decision: VerifiedHumanDecision,
        *,
        now: datetime,
    ) -> InstallConsentChallengeRecord:
        """Atomically move pending or reauthenticated consent to decision-ready."""

        self._require_issued_decision(decision)
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_text = _effective_trusted_now(connection, now)
                row = connection.execute(_SELECT_ONE, (decision.challenge_id,)).fetchone()
                if row is None:
                    raise ConsentBrokerDecisionRejected("verified decision challenge is unknown")
                values = self._validated_row(row)
                challenge = _challenge_from_values(values)
                if values["state"] not in {"pending", "reauthentication-required"}:
                    raise ConsentBrokerReplay(
                        f"install consent record is already {values['state']}"
                    )
                self._assert_challenge_live(values, challenge, current, current_text, connection)
                if challenge.challenge_digest != decision.challenge_digest:
                    raise ConsentBrokerDecisionRejected(
                        "verified decision does not bind the persisted challenge"
                    )
                if current >= _parse_timestamp(decision.expires_at, "decision.expires_at"):
                    raise ConsentBrokerExpired("verified human decision has expired")
                self._record_assertion_nonce(
                    connection,
                    values=values,
                    challenge_id=decision.challenge_id,
                    nonce_digest=decision.assertion_nonce_digest,
                    recorded_at=current_text,
                )
                values.update(
                    {
                        "state": "decision-ready",
                        "decision": decision.decision,
                        "principal_digest": decision.principal_digest,
                        "authenticator_id": decision.authenticator_id,
                        "audience": decision.audience,
                        "assertion_nonce_digest": decision.assertion_nonce_digest,
                        "decision_issued_at": decision.issued_at,
                        "decision_expires_at": decision.expires_at,
                        "decision_recorded_at": current_text,
                    }
                )
                self._update_row(connection, values)
                connection.execute("COMMIT")
                return _record_from_values(values)
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def interactive_guard(
        self,
        decision: VerifiedHumanDecision,
        *,
        now: Callable[[], datetime],
    ) -> InteractiveInstallDecisionGuard:
        """Return the engine-compatible exact reserve/release/settle guard."""

        self._require_issued_decision(decision)
        if not callable(now):
            raise TypeError("now must be a trusted clock callable")

        def guard(
            reservation: InteractiveInstallDecisionReservation,
        ) -> AbstractContextManager[None]:
            if not isinstance(reservation, InteractiveInstallDecisionReservation):
                raise TypeError("reservation must be an InteractiveInstallDecisionReservation")
            return self._reservation_context(decision, reservation, now)

        return guard

    def reconcile_install_decision(
        self,
        *,
        provider: CommittedInstallDecisionEvidenceProvider,
        query: InstallDecisionEvidenceQuery,
        reservation: InteractiveInstallDecisionReservation,
        now: datetime,
    ) -> ConsentBrokerReconciliationReport:
        """Reconcile one crash state using only owner-held journal evidence.

        Exact absence is acted on while the engine provider still excludes a
        concurrent writer.  Positive evidence is revalidated by the issuing
        provider before the broker settles.  Indeterminate evidence never
        changes durable broker state.
        """

        if not isinstance(provider, CommittedInstallDecisionEvidenceProvider):
            raise TypeError("provider must implement CommittedInstallDecisionEvidenceProvider")
        if not isinstance(query, InstallDecisionEvidenceQuery):
            raise TypeError("query must be an InstallDecisionEvidenceQuery")
        if not isinstance(reservation, InteractiveInstallDecisionReservation):
            raise TypeError("reservation must be an InteractiveInstallDecisionReservation")
        with secure_file_lock(
            self._reservation_lease_target(query.consent_id),
            timeout=self._busy_timeout_ms / 1000,
        ):
            return self._reconcile_install_decision_under_lease(
                provider=provider,
                query=query,
                reservation=reservation,
                now=now,
            )

    def _reconcile_install_decision_under_lease(
        self,
        *,
        provider: CommittedInstallDecisionEvidenceProvider,
        query: InstallDecisionEvidenceQuery,
        reservation: InteractiveInstallDecisionReservation,
        now: datetime,
    ) -> ConsentBrokerReconciliationReport:
        initial = self.get(query.consent_id, now=now)
        _require_exact_reconciliation(initial, query, reservation)
        committed_lookup: InstallDecisionEvidenceLookup | None = None
        with provider.inspect_install_decision(query) as lookup:
            if not isinstance(lookup, InstallDecisionEvidenceLookup):
                raise ConsentBrokerDecisionRejected(
                    "install decision evidence provider returned an invalid result"
                )
            if lookup.status == "absent-at-expected-head":
                return self._reconcile_absent_install_decision(
                    query=query,
                    reservation=reservation,
                    now=now,
                )
            if lookup.status != "committed":
                record = self.get(query.consent_id, now=now)
                _require_exact_reconciliation(record, query, reservation)
                return ConsentBrokerReconciliationReport(
                    outcome="quarantined",
                    journal_status=lookup.status,
                    record=record,
                )
            committed_lookup = lookup

        assert committed_lookup is not None
        evidence = committed_lookup.evidence
        if evidence is None:
            raise ConsentBrokerDecisionRejected(
                "committed install decision evidence is missing opaque authority"
            )
        try:
            provider.revalidate_install_decision_evidence(evidence, query=query)
        except InstallDecisionEvidenceRejected:
            raise ConsentBrokerDecisionRejected(
                "committed install decision evidence failed issuing-store revalidation"
            ) from None
        return self._reconcile_committed_install_decision(
            query=query,
            reservation=reservation,
            now=now,
        )

    def recover(self, *, now: datetime) -> ConsentBrokerRecoveryReport:
        """Expire stale records; never make an abandoned reservation replayable.

        An exception from a live context returns ``reserved`` to
        ``decision-ready`` immediately.  This generic sweep retains every
        process-abandoned reservation.  Only ``reconcile_install_decision`` may
        reopen one, and only while the engine store proves exact absence at the
        expected head.
        """

        expired_count = 0
        retained_reserved = 0
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_text = _effective_trusted_now(connection, now)
                rows = connection.execute(
                    f"SELECT {', '.join(_ROW_COLUMNS)} FROM consent_challenges "
                    "WHERE state IN "
                    "('pending', 'decision-ready', 'reauthentication-required', "
                    "'reserved', 'expired')"
                ).fetchall()
                for row in rows:
                    values = self._validated_row(row)
                    challenge = _challenge_from_values(values)
                    challenge_expired = current >= _parse_timestamp(
                        challenge.expires_at, "challenge.expires_at"
                    )
                    decision_expired = values["state"] in {
                        "decision-ready",
                        "reauthentication-required",
                    } and current >= _parse_timestamp(
                        _required_text_value(values["decision_expires_at"], "decision_expires_at"),
                        "decision_expires_at",
                    )
                    if values["state"] == "reserved":
                        retained_reserved += 1
                    elif (
                        values["state"] == "expired"
                        and not challenge_expired
                        and values["decision"] is not None
                    ):
                        _mark_reauthentication_required(values)
                        self._update_row(connection, values)
                    elif challenge_expired:
                        if values["state"] != "expired":
                            values["state"] = "expired"
                            values["expired_at"] = current_text
                            values["reservation_token_digest"] = None
                            self._update_row(connection, values)
                            expired_count += 1
                    elif decision_expired:
                        _mark_reauthentication_required(values)
                        self._update_row(connection, values)
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return ConsentBrokerRecoveryReport(
            expired=expired_count,
            retained_reserved=retained_reserved,
        )

    def _reconcile_absent_install_decision(
        self,
        *,
        query: InstallDecisionEvidenceQuery,
        reservation: InteractiveInstallDecisionReservation,
        now: datetime,
    ) -> ConsentBrokerReconciliationReport:
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current, current_text = _effective_trusted_now(connection, now)
                row = connection.execute(_SELECT_ONE, (query.consent_id,)).fetchone()
                if row is None:
                    raise ConsentBrokerDecisionRejected(
                        "install decision evidence challenge is unknown"
                    )
                values = self._validated_row(row)
                record = _record_from_values(values)
                _require_exact_reconciliation(record, query, reservation)
                state = values["state"]
                if state == "settled":
                    raise ConsentBrokerDecisionRejected(
                        "absence cannot reopen a settled install decision"
                    )
                if state == "pending":
                    raise ConsentBrokerDecisionRejected(
                        "absence cannot reconcile unauthenticated install consent"
                    )
                if state == "reserved":
                    _require_persisted_reservation(values, reservation)
                elif state not in {
                    "decision-ready",
                    "reauthentication-required",
                    "expired",
                }:
                    raise ConsentBrokerDecisionRejected(
                        "install consent state cannot be reconciled from absence"
                    )
                challenge = record.challenge
                challenge_expired = current >= _parse_timestamp(
                    challenge.expires_at, "challenge.expires_at"
                )
                if challenge_expired:
                    values.update(
                        {
                            "state": "expired",
                            "reservation_token_digest": None,
                            "expired_at": values["expired_at"] or current_text,
                        }
                    )
                    outcome = "expired"
                else:
                    _mark_reauthentication_required(values)
                    outcome = "reauthentication-required"
                self._update_row(connection, values)
                connection.execute("COMMIT")
                return ConsentBrokerReconciliationReport(
                    outcome=outcome,
                    journal_status="absent-at-expected-head",
                    record=_record_from_values(values),
                )
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _reconcile_committed_install_decision(
        self,
        *,
        query: InstallDecisionEvidenceQuery,
        reservation: InteractiveInstallDecisionReservation,
        now: datetime,
    ) -> ConsentBrokerReconciliationReport:
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _current, current_text = _effective_trusted_now(connection, now)
                row = connection.execute(_SELECT_ONE, (query.consent_id,)).fetchone()
                if row is None:
                    raise ConsentBrokerDecisionRejected(
                        "committed install decision challenge is unknown"
                    )
                values = self._validated_row(row)
                record = _record_from_values(values)
                _require_exact_reconciliation(record, query, reservation)
                state = values["state"]
                if state == "pending":
                    raise ConsentBrokerDecisionRejected(
                        "committed decision cannot settle unauthenticated install consent"
                    )
                if state in {"reserved", "settled"}:
                    _require_persisted_reservation(values, reservation)
                elif state not in {
                    "decision-ready",
                    "reauthentication-required",
                    "expired",
                }:
                    raise ConsentBrokerDecisionRejected(
                        "install consent state cannot accept committed evidence"
                    )
                if state != "settled":
                    values.update(
                        {
                            "state": "settled",
                            "reservation_event_id": reservation.event_id,
                            "reservation_event_content_digest": (reservation.event_content_digest),
                            "reservation_token_digest": None,
                            "reserved_at": values["reserved_at"] or current_text,
                            "settled_at": current_text,
                            "expired_at": None,
                        }
                    )
                    self._update_row(connection, values)
                connection.execute("COMMIT")
                return ConsentBrokerReconciliationReport(
                    outcome="settled",
                    journal_status="committed",
                    record=_record_from_values(values),
                )
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @contextmanager
    def _reservation_context(
        self,
        decision: VerifiedHumanDecision,
        reservation: InteractiveInstallDecisionReservation,
        now: Callable[[], datetime],
    ) -> Iterator[None]:
        token = secrets.token_bytes(32)
        token_digest = hashlib.sha256(token).hexdigest()
        if self.inspect_record(decision.challenge_id).state == "reserved":
            raise ConsentBrokerReplay("install consent decision is already reserved")
        with secure_file_lock(
            self._reservation_lease_target(decision.challenge_id),
            timeout=self._busy_timeout_ms / 1000,
        ):
            self._reserve(decision, reservation, token_digest, now=now)
            try:
                yield
            except BaseException:
                self._release(decision, reservation, token_digest, now=now)
                raise
            else:
                self._settle(decision, reservation, token_digest, now=now)

    def _reservation_lease_target(self, challenge_id: str) -> Path:
        _token(challenge_id, "challenge_id")
        identity = hashlib.sha256(f"{self.path.name}\x00{challenge_id}".encode("utf-8")).hexdigest()
        return self.path.parent / f".ctx-consent-reservation-{identity}"

    def _reserve(
        self,
        decision: VerifiedHumanDecision,
        reservation: InteractiveInstallDecisionReservation,
        token_digest: str,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._require_issued_decision(decision)
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                values = self._row_for_decision(connection, decision)
                challenge = _challenge_from_values(values)
                if values["state"] == "reserved":
                    raise ConsentBrokerReplay("install consent decision is already reserved")
                if values["state"] != "decision-ready":
                    raise ConsentBrokerReplay(
                        f"install consent decision is already {values['state']}"
                    )
                self._require_ready_matches_decision(values, decision)
                _require_exact_reservation(challenge, decision, reservation)
                current, current_text = _effective_trusted_now(connection, now())
                self._assert_challenge_live(values, challenge, current, current_text, connection)
                values.update(
                    {
                        "state": "reserved",
                        "reservation_event_id": reservation.event_id,
                        "reservation_event_content_digest": reservation.event_content_digest,
                        "reservation_token_digest": token_digest,
                        "reserved_at": current_text,
                    }
                )
                self._update_row(connection, values)
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _release(
        self,
        decision: VerifiedHumanDecision,
        reservation: InteractiveInstallDecisionReservation,
        token_digest: str,
        *,
        now: Callable[[], datetime],
    ) -> None:
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                values = self._row_for_decision(connection, decision)
                _require_held_reservation(values, reservation, token_digest)
                _effective_trusted_now(connection, now())
                values.update(
                    {
                        "state": "decision-ready",
                        "reservation_event_id": None,
                        "reservation_event_content_digest": None,
                        "reservation_token_digest": None,
                        "reserved_at": None,
                    }
                )
                self._update_row(connection, values)
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _settle(
        self,
        decision: VerifiedHumanDecision,
        reservation: InteractiveInstallDecisionReservation,
        token_digest: str,
        *,
        now: Callable[[], datetime],
    ) -> None:
        with self._locked_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                values = self._row_for_decision(connection, decision)
                _require_held_reservation(values, reservation, token_digest)
                _, current_text = _effective_trusted_now(connection, now())
                values.update(
                    {
                        "state": "settled",
                        "reservation_token_digest": None,
                        "settled_at": current_text,
                    }
                )
                self._update_row(connection, values)
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _row_for_decision(
        self,
        connection: sqlite3.Connection,
        decision: VerifiedHumanDecision,
    ) -> dict[str, object]:
        self._require_issued_decision(decision)
        row = connection.execute(_SELECT_ONE, (decision.challenge_id,)).fetchone()
        if row is None:
            raise ConsentBrokerDecisionRejected("verified decision challenge is unknown")
        values = self._validated_row(row)
        if values["challenge_digest"] != decision.challenge_digest:
            raise ConsentBrokerDecisionRejected(
                "verified decision does not bind the persisted challenge"
            )
        return values

    def _require_ready_matches_decision(
        self,
        values: Mapping[str, object],
        decision: VerifiedHumanDecision,
    ) -> None:
        expected: dict[str, object] = {
            "decision": decision.decision,
            "principal_digest": decision.principal_digest,
            "authenticator_id": decision.authenticator_id,
            "audience": decision.audience,
            "assertion_nonce_digest": decision.assertion_nonce_digest,
            "decision_issued_at": decision.issued_at,
            "decision_expires_at": decision.expires_at,
        }
        if any(values[name] != value for name, value in expected.items()):
            raise ConsentBrokerDecisionRejected(
                "process-bound decision does not match the decision-ready record"
            )

    def _assert_challenge_live(
        self,
        values: dict[str, object],
        challenge: InstallConsentChallenge,
        current: datetime,
        current_text: str,
        connection: sqlite3.Connection,
    ) -> None:
        self._expire_if_due(
            values,
            challenge,
            current,
            current_text,
            connection,
        )
        if values["state"] != "expired":
            return
        # Expiry is a terminal security transition.  Persist it before raising
        # so the caller's failure path cannot roll it back into reusable state.
        connection.execute("COMMIT")
        raise ConsentBrokerExpired("install consent challenge or decision has expired")

    def _expire_if_due(
        self,
        values: dict[str, object],
        challenge: InstallConsentChallenge,
        current: datetime,
        current_text: str,
        connection: sqlite3.Connection,
    ) -> str | None:
        """Persist one eligible TTL transition and return its resulting state."""

        if values["state"] == "expired":
            if (
                current < _parse_timestamp(challenge.expires_at, "challenge.expires_at")
                and values["decision"] is not None
            ):
                _mark_reauthentication_required(values)
                self._update_row(connection, values)
                return "reauthentication-required"
            return None
        if values["state"] not in {
            "pending",
            "decision-ready",
            "reauthentication-required",
        }:
            return None
        challenge_expired = current >= _parse_timestamp(
            challenge.expires_at, "challenge.expires_at"
        )
        decision_expired = values["state"] in {
            "decision-ready",
            "reauthentication-required",
        } and current >= _parse_timestamp(
            _required_text_value(values["decision_expires_at"], "decision_expires_at"),
            "decision_expires_at",
        )
        if not challenge_expired and not decision_expired:
            return None
        if challenge_expired:
            values["state"] = "expired"
            values["expired_at"] = current_text
            values["reservation_token_digest"] = None
            resulting_state = "expired"
        else:
            _mark_reauthentication_required(values)
            resulting_state = "reauthentication-required"
        self._update_row(connection, values)
        return resulting_state

    def _issue_verified_decision(self, **claims: str) -> VerifiedHumanDecision:
        seal = self._decision_seal(claims)
        decision = object.__new__(VerifiedHumanDecision)
        for name, value in claims.items():
            object.__setattr__(decision, name, value)
        object.__setattr__(decision, "_issuer_identity", self.__issuer_identity)
        object.__setattr__(decision, "_seal", seal)
        return decision

    def _decision_seal(self, claims: Mapping[str, str]) -> str:
        return hmac.new(self.__issuer_key, _canonical_bytes(claims), hashlib.sha256).hexdigest()

    def _require_issued_decision(self, decision: object) -> None:
        if not isinstance(decision, VerifiedHumanDecision):
            raise TypeError("decision must be an opaque VerifiedHumanDecision")
        claims = _verified_claims(decision)
        if decision._issuer_identity is not self.__issuer_identity or not hmac.compare_digest(
            decision._seal,
            self._decision_seal(claims),
        ):
            raise ConsentBrokerDecisionRejected(
                "VerifiedHumanDecision is not process-bound to this broker instance"
            )

    def _insert_row(self, connection: sqlite3.Connection, values: dict[str, object]) -> None:
        values["record_digest"] = _record_digest(values)
        placeholders = ", ".join("?" for _ in _ROW_COLUMNS)
        connection.execute(
            f"INSERT INTO consent_challenges ({', '.join(_ROW_COLUMNS)}) VALUES ({placeholders})",
            tuple(values[name] for name in _ROW_COLUMNS),
        )

    def _record_assertion_nonce(
        self,
        connection: sqlite3.Connection,
        *,
        values: dict[str, object],
        challenge_id: str,
        nonce_digest: str,
        recorded_at: str,
    ) -> None:
        existing = connection.execute(
            "SELECT challenge_id FROM consent_assertion_nonces WHERE nonce_digest = ?",
            (nonce_digest,),
        ).fetchone()
        if existing is not None:
            raise ConsentBrokerReplay("verified human decision nonce was already recorded")
        count = connection.execute("SELECT count(*) FROM consent_assertion_nonces").fetchone()[0]
        if type(count) is not int or count < 0:
            raise ConsentBrokerCorruption("assertion nonce history count is invalid")
        if count >= self._capacity * _NONCE_HISTORY_FACTOR:
            raise ConsentBrokerCapacityExceeded(
                "install consent assertion nonce history reached its bounded capacity"
            )
        history_count = values["assertion_nonce_history_count"]
        history_digest = values["assertion_nonce_history_digest"]
        if type(history_count) is not int or history_count < 0:
            raise ConsentBrokerCorruption("assertion nonce history count is invalid")
        history_digest = _required_digest_value(history_digest, "nonce history digest")
        sequence = history_count + 1
        record_digest = _nonce_record_digest(
            challenge_id=challenge_id,
            sequence=sequence,
            nonce_digest=nonce_digest,
            recorded_at=recorded_at,
            previous_record_digest=history_digest,
        )
        connection.execute(
            "INSERT INTO consent_assertion_nonces "
            "(challenge_id, sequence, nonce_digest, recorded_at, "
            "previous_record_digest, record_digest) VALUES (?, ?, ?, ?, ?, ?)",
            (
                challenge_id,
                sequence,
                nonce_digest,
                recorded_at,
                history_digest,
                record_digest,
            ),
        )
        values["assertion_nonce_history_count"] = sequence
        values["assertion_nonce_history_digest"] = record_digest

    def _update_row(self, connection: sqlite3.Connection, values: dict[str, object]) -> None:
        values["record_digest"] = _record_digest(values)
        assignments = ", ".join(f"{name} = ?" for name in _ROW_COLUMNS if name != "challenge_id")
        connection.execute(
            f"UPDATE consent_challenges SET {assignments} WHERE challenge_id = ?",
            tuple(values[name] for name in _ROW_COLUMNS if name != "challenge_id")
            + (values["challenge_id"],),
        )

    def _validated_row(self, row: sqlite3.Row) -> dict[str, object]:
        values = {name: row[name] for name in _ROW_COLUMNS}
        payload = _blob_bytes(values["challenge_json"])
        byte_length = values["challenge_byte_length"]
        if (
            type(byte_length) is not int
            or byte_length != len(payload)
            or not 1 <= len(payload) <= _MAX_CHALLENGE_BYTES
        ):
            raise ConsentBrokerCorruption("persisted challenge byte length is invalid")
        values["challenge_json"] = payload
        challenge = _decode_challenge(payload)
        if challenge.challenge_id != values["challenge_id"]:
            raise ConsentBrokerCorruption("persisted challenge ID binding is invalid")
        if challenge.challenge_digest != values["challenge_digest"]:
            raise ConsentBrokerCorruption("persisted challenge digest binding is invalid")
        supplied_digest = values["record_digest"]
        if not isinstance(supplied_digest, str) or not hmac.compare_digest(
            supplied_digest, _record_digest(values)
        ):
            raise ConsentBrokerCorruption("persisted consent record digest is invalid")
        try:
            _validate_state(values)
        except (TypeError, ValueError) as exc:
            raise ConsentBrokerCorruption("persisted consent lifecycle fields are invalid") from exc
        return values

    def _prepare_parent(self) -> None:
        reject_symlink_path(self.path)
        ensure_secure_directory(self.path.parent)
        reject_symlink_path(self.path)
        _require_private_directory(self.path.parent)

    def _prepare_path(self) -> bool:
        reject_symlink_path(self.path)
        if self.path.exists():
            _require_private_file(self.path)
            return False
        if any(os.path.lexists(Path(f"{self.path}{suffix}")) for suffix in _SQLITE_SIDECARS):
            raise ConsentBrokerCorruption(
                "new consent broker database has pre-existing SQLite sidecars"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags, _PRIVATE_FILE_MODE)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        finally:
            os.close(descriptor)
        _require_private_file(self.path)
        return True

    @contextmanager
    def _locked_connection(self) -> Iterator[sqlite3.Connection]:
        with secure_file_lock(self.path, timeout=self._busy_timeout_ms / 1000):
            self._assert_bound_path()
            with self._connect() as connection:
                yield connection
            self._assert_bound_path()

    @contextmanager
    def _connect(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        reject_symlink_path(self.path)
        _require_private_directory(self.path.parent)
        before = _require_private_file(self.path)
        sidecars_before = self._authenticate_sidecars()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self._busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            if initialize:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO consent_broker_identity "
                    "(singleton, audience, trusted_time_high_water, identity_digest) "
                    "VALUES (1, ?, NULL, ?)",
                    (self.audience, _store_identity_digest(self.audience, None)),
                )
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            _require_schema(connection, audience=self.audience)
            after = _require_private_file(self.path)
            if not os.path.samestat(before, after):
                raise ConsentBrokerCorruption("consent broker database changed while opening")
            sidecars_after = self._authenticate_sidecars()
            for suffix in sidecars_before.keys() & sidecars_after.keys():
                if not os.path.samestat(sidecars_before[suffix], sidecars_after[suffix]):
                    raise ConsentBrokerCorruption(
                        "consent broker SQLite sidecar changed while opening"
                    )
            self._require_bounded_database()
            yield connection
        except ConsentBrokerError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ConsentBrokerCorruption("consent broker database is unreadable") from exc
        except OSError as exc:
            raise ConsentBrokerUnavailable("consent broker filesystem is unavailable") from exc
        finally:
            if connection is not None:
                connection.close()
            self._authenticate_sidecars()

    def _assert_bound_path(self) -> None:
        current = _require_private_file(self.path)
        if not os.path.samestat(current, self.__bound_metadata):
            raise ConsentBrokerCorruption(
                "consent broker database changed since its authenticated path binding"
            )

    def _authenticate_sidecars(self) -> dict[str, os.stat_result]:
        authenticated: dict[str, os.stat_result] = {}
        for suffix in _SQLITE_SIDECARS:
            candidate = Path(f"{self.path}{suffix}")
            if not os.path.lexists(candidate):
                continue
            reject_symlink_path(candidate)
            metadata = candidate.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise ValueError("consent broker SQLite sidecar must be an unlinked regular file")
            if os.name != "nt" and metadata.st_uid != os.geteuid():
                raise ValueError("consent broker SQLite sidecar is not owned by the current user")
            if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE:
                raise ValueError("consent broker SQLite sidecar must be owner-private (0600)")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags)
            try:
                opened = os.fstat(descriptor)
                current = candidate.stat(follow_symlinks=False)
                if (
                    not os.path.samestat(metadata, opened)
                    or not os.path.samestat(opened, current)
                    or opened.st_nlink != 1
                ):
                    raise ConsentBrokerCorruption(
                        "consent broker SQLite sidecar changed while authenticating"
                    )
            finally:
                os.close(descriptor)
            authenticated[suffix] = metadata
        return authenticated

    def _require_bounded_database(self) -> None:
        total = 0
        for candidate in (self.path, *(Path(f"{self.path}{s}") for s in _SQLITE_SIDECARS)):
            try:
                metadata = candidate.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            total += metadata.st_size
        if total > _MAX_DATABASE_BYTES:
            raise ConsentBrokerCapacityExceeded("consent broker database exceeds its size bound")


def _empty_row_values(
    *,
    challenge: InstallConsentChallenge,
    challenge_json: bytes,
    created_at: str,
) -> dict[str, object]:
    return {
        "challenge_id": challenge.challenge_id,
        "challenge_digest": challenge.challenge_digest,
        "challenge_json": challenge_json,
        "challenge_byte_length": len(challenge_json),
        "state": "pending",
        "created_at": created_at,
        "decision": None,
        "principal_digest": None,
        "authenticator_id": None,
        "audience": None,
        "assertion_nonce_digest": None,
        "assertion_nonce_history_count": 0,
        "assertion_nonce_history_digest": _nonce_history_genesis(challenge.challenge_id),
        "decision_issued_at": None,
        "decision_expires_at": None,
        "decision_recorded_at": None,
        "reservation_event_id": None,
        "reservation_event_content_digest": None,
        "reservation_token_digest": None,
        "reserved_at": None,
        "settled_at": None,
        "expired_at": None,
        "record_digest": "",
    }


def _record_digest(values: Mapping[str, object]) -> str:
    mapping: dict[str, object] = {}
    for name in _ROW_WITHOUT_DIGEST:
        value = values[name]
        if isinstance(value, (bytes, bytearray, memoryview)):
            mapping[name] = bytes(value).decode("ascii")
        else:
            mapping[name] = value
    return _canonical_digest(mapping)


def _nonce_record_digest(
    *,
    challenge_id: str,
    sequence: int,
    nonce_digest: str,
    recorded_at: str,
    previous_record_digest: str,
) -> str:
    return _canonical_digest(
        {
            "challenge_id": challenge_id,
            "nonce_digest": nonce_digest,
            "previous_record_digest": previous_record_digest,
            "recorded_at": recorded_at,
            "schema": _NONCE_RECORD_SCHEMA,
            "sequence": sequence,
        }
    )


def _nonce_history_genesis(challenge_id: str) -> str:
    return _canonical_digest(
        {
            "challenge_id": challenge_id,
            "schema": _NONCE_HISTORY_SCHEMA,
        }
    )


def _blob_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    raise ConsentBrokerCorruption("persisted challenge is not a byte string")


def _decode_challenge(payload: bytes) -> InstallConsentChallenge:
    try:
        decoded = json.loads(payload)
        if not isinstance(decoded, Mapping):
            raise ValueError("challenge JSON must encode an object")
        challenge = InstallConsentChallenge.from_dict(decoded)
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise ConsentBrokerCorruption("persisted install consent challenge is invalid") from exc
    if challenge.to_json().encode("ascii") != payload:
        raise ConsentBrokerCorruption("persisted install consent challenge is not canonical")
    return challenge


def _challenge_from_values(values: Mapping[str, object]) -> InstallConsentChallenge:
    return _decode_challenge(_blob_bytes(values["challenge_json"]))


def _validate_state(values: Mapping[str, object]) -> None:
    state = values["state"]
    if state not in _STATES:
        raise ConsentBrokerCorruption("persisted consent lifecycle state is invalid")
    _parse_timestamp(values["created_at"], "created_at")
    history_count = values["assertion_nonce_history_count"]
    if type(history_count) is not int or not 0 <= history_count <= (
        _MAX_CAPACITY * _NONCE_HISTORY_FACTOR
    ):
        raise ConsentBrokerCorruption("persisted assertion nonce history count is invalid")
    history_digest = _digest(
        values["assertion_nonce_history_digest"],
        "assertion_nonce_history_digest",
    )
    decision_fields = (
        "decision",
        "principal_digest",
        "authenticator_id",
        "audience",
        "assertion_nonce_digest",
        "decision_issued_at",
        "decision_expires_at",
        "decision_recorded_at",
    )
    reservation_fields = (
        "reservation_event_id",
        "reservation_event_content_digest",
        "reserved_at",
    )
    if state == "pending":
        if history_count != 0 or history_digest != _nonce_history_genesis(
            _required_text_value(values["challenge_id"], "challenge_id")
        ):
            raise ConsentBrokerCorruption("pending consent record has nonce history")
        if any(
            values[name] is not None
            for name in (*decision_fields, *reservation_fields, "reservation_token_digest")
        ):
            raise ConsentBrokerCorruption("pending consent record carries decision authority")
    elif (
        state
        in {
            "decision-ready",
            "reauthentication-required",
            "reserved",
            "settled",
            "expired",
        }
        and values["decision"] is not None
    ):
        if values["decision"] not in _DECISIONS:
            raise ConsentBrokerCorruption("persisted decision is invalid")
        _digest(values["principal_digest"], "principal_digest")
        _token(values["authenticator_id"], "authenticator_id")
        _token(values["audience"], "audience")
        _digest(values["assertion_nonce_digest"], "assertion_nonce_digest")
        _parse_timestamp(values["decision_issued_at"], "decision_issued_at")
        _parse_timestamp(values["decision_expires_at"], "decision_expires_at")
        _parse_timestamp(values["decision_recorded_at"], "decision_recorded_at")
        if history_count < 1:
            raise ConsentBrokerCorruption("persisted decision has no nonce history")
    elif state != "expired":
        raise ConsentBrokerCorruption("consent record is missing decision authority")
    if state in {"decision-ready", "reauthentication-required"} and any(
        values[name] is not None for name in (*reservation_fields, "reservation_token_digest")
    ):
        raise ConsentBrokerCorruption("ready consent record carries a reservation")
    if state in {"reserved", "settled"}:
        _token(values["reservation_event_id"], "reservation_event_id")
        _digest(values["reservation_event_content_digest"], "reservation_event_content_digest")
        _parse_timestamp(values["reserved_at"], "reserved_at")
        if state == "reserved":
            _digest(values["reservation_token_digest"], "reservation_token_digest")
        elif values["reservation_token_digest"] is not None:
            raise ConsentBrokerCorruption("settled consent record retains reservation authority")
    if state == "settled":
        _parse_timestamp(values["settled_at"], "settled_at")
    elif values["settled_at"] is not None:
        raise ConsentBrokerCorruption("unsettled consent record has a settled timestamp")
    if state == "expired":
        _parse_timestamp(values["expired_at"], "expired_at")
        if values["reservation_token_digest"] is not None:
            raise ConsentBrokerCorruption("expired consent record retains reservation authority")
        expired_reservation = tuple(values[name] for name in reservation_fields)
        if any(value is not None for value in expired_reservation):
            if any(value is None for value in expired_reservation):
                raise ConsentBrokerCorruption(
                    "expired consent record has a partial reservation identity"
                )
            _token(values["reservation_event_id"], "reservation_event_id")
            _digest(
                values["reservation_event_content_digest"],
                "reservation_event_content_digest",
            )
            _parse_timestamp(values["reserved_at"], "reserved_at")
    elif values["expired_at"] is not None:
        raise ConsentBrokerCorruption("live consent record has an expiry tombstone")


def _record_from_values(values: Mapping[str, object]) -> InstallConsentChallengeRecord:
    return InstallConsentChallengeRecord(
        challenge=_challenge_from_values(values),
        state=_required_text_value(values["state"], "state"),
        created_at=_required_text_value(values["created_at"], "created_at"),
        decision=_optional_text_value(values["decision"]),
        principal_digest=_optional_text_value(values["principal_digest"]),
        authenticator_id=_optional_text_value(values["authenticator_id"]),
        audience=_optional_text_value(values["audience"]),
        assertion_nonce_digest=_optional_text_value(values["assertion_nonce_digest"]),
        decision_issued_at=_optional_text_value(values["decision_issued_at"]),
        decision_expires_at=_optional_text_value(values["decision_expires_at"]),
        decision_recorded_at=_optional_text_value(values["decision_recorded_at"]),
        reservation_event_id=_optional_text_value(values["reservation_event_id"]),
        reservation_event_content_digest=_optional_text_value(
            values["reservation_event_content_digest"]
        ),
        reserved_at=_optional_text_value(values["reserved_at"]),
        settled_at=_optional_text_value(values["settled_at"]),
        expired_at=_optional_text_value(values["expired_at"]),
    )


def _verified_claims(decision: VerifiedHumanDecision) -> dict[str, str]:
    return {
        "assertion_nonce_digest": decision.assertion_nonce_digest,
        "audience": decision.audience,
        "authenticator_id": decision.authenticator_id,
        "challenge_digest": decision.challenge_digest,
        "challenge_id": decision.challenge_id,
        "decision": decision.decision,
        "expires_at": decision.expires_at,
        "issued_at": decision.issued_at,
        "principal_digest": decision.principal_digest,
    }


def _require_reverification_matches(
    values: Mapping[str, object], assertion: SignedHumanDecisionAssertion
) -> None:
    expected = {
        "decision": assertion.decision,
        "principal_digest": assertion.principal_digest,
        "authenticator_id": assertion.authenticator_id,
        "audience": assertion.audience,
    }
    if any(values[name] != value for name, value in expected.items()):
        raise ConsentBrokerDecisionRejected(
            "reverified assertion does not match the decision-ready record"
        )


def _require_exact_reservation(
    challenge: InstallConsentChallenge,
    decision: VerifiedHumanDecision,
    reservation: InteractiveInstallDecisionReservation,
) -> None:
    expected: tuple[tuple[object, object], ...] = (
        (reservation.scope, challenge.scope),
        (reservation.consent_id, challenge.challenge_id),
        (reservation.decision, decision.decision),
        (reservation.policy_snapshot_digest, challenge.policy_snapshot_digest),
        (reservation.requested_action_id, challenge.requested_action_id),
        (reservation.requested_action_kind, challenge.requested_action_kind),
        (
            reservation.requested_action_content_digest,
            challenge.requested_action_content_digest,
        ),
        (
            reservation.requested_action_precondition_revision,
            challenge.requested_action_precondition_revision,
        ),
        (reservation.install_expires_at, challenge.expires_at),
    )
    if any(actual != wanted for actual, wanted in expected):
        raise ConsentBrokerDecisionRejected(
            "interactive reservation does not match the exact install challenge"
        )


def _require_exact_reconciliation(
    record: InstallConsentChallengeRecord,
    query: InstallDecisionEvidenceQuery,
    reservation: InteractiveInstallDecisionReservation,
) -> None:
    challenge = record.challenge
    if query.decision_basis != "interactive":
        raise ConsentBrokerDecisionRejected(
            "install consent reconciliation requires an interactive decision"
        )
    expected: tuple[tuple[object, object], ...] = (
        (query.scope, challenge.scope),
        (query.consent_id, challenge.challenge_id),
        (query.decision, record.decision),
        (query.policy_snapshot_digest, challenge.policy_snapshot_digest),
        (query.requested_action_id, challenge.requested_action_id),
        (query.requested_action_kind, challenge.requested_action_kind),
        (
            query.requested_action_content_digest,
            challenge.requested_action_content_digest,
        ),
        (
            query.requested_action_precondition_revision,
            challenge.requested_action_precondition_revision,
        ),
        (reservation.scope, query.scope),
        (reservation.consent_id, query.consent_id),
        (reservation.decision, query.decision),
        (reservation.policy_snapshot_digest, query.policy_snapshot_digest),
        (reservation.requested_action_id, query.requested_action_id),
        (reservation.requested_action_kind, query.requested_action_kind),
        (
            reservation.requested_action_content_digest,
            query.requested_action_content_digest,
        ),
        (
            reservation.requested_action_precondition_revision,
            query.requested_action_precondition_revision,
        ),
        (reservation.install_expires_at, challenge.expires_at),
        (reservation.event_id, query.event_id),
        (reservation.event_content_digest, query.event_content_digest),
    )
    if any(actual != wanted for actual, wanted in expected):
        raise ConsentBrokerDecisionRejected(
            "journal evidence does not match the exact broker decision and reservation"
        )


def _require_persisted_reservation(
    values: Mapping[str, object],
    reservation: InteractiveInstallDecisionReservation,
) -> None:
    if (
        values["reservation_event_id"] != reservation.event_id
        or values["reservation_event_content_digest"] != reservation.event_content_digest
    ):
        raise ConsentBrokerDecisionRejected(
            "journal evidence does not match the persisted broker reservation"
        )


def _require_held_reservation(
    values: Mapping[str, object],
    reservation: InteractiveInstallDecisionReservation,
    token_digest: str,
) -> None:
    if values["state"] != "reserved":
        raise ConsentBrokerReplay("interactive decision is no longer reserved")
    if (
        values["reservation_event_id"] != reservation.event_id
        or values["reservation_event_content_digest"] != reservation.event_content_digest
        or values["reservation_token_digest"] != token_digest
    ):
        raise ConsentBrokerDecisionRejected("interactive reservation ownership changed")


def _required_text_value(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConsentBrokerCorruption(f"persisted {name} is invalid")
    return value


def _required_digest_value(value: object, name: str) -> str:
    try:
        return _digest(value, name)
    except ValueError as exc:
        raise ConsentBrokerCorruption(f"persisted {name} is invalid") from exc


def _optional_text_value(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConsentBrokerCorruption("persisted optional text is invalid")
    return value


def _require_schema(connection: sqlite3.Connection, *, audience: str) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != _SCHEMA_VERSION:
        raise ConsentBrokerCorruption("consent broker schema version is invalid")
    objects = connection.execute(
        """
        SELECT type, name, tbl_name
          FROM sqlite_master
         WHERE name NOT LIKE 'sqlite_%'
         ORDER BY type, name
        """
    ).fetchall()
    if [(str(row["type"]), str(row["name"]), str(row["tbl_name"])) for row in objects] != [
        ("table", "consent_assertion_nonces", "consent_assertion_nonces"),
        ("table", "consent_broker_identity", "consent_broker_identity"),
        ("table", "consent_challenges", "consent_challenges"),
    ]:
        raise ConsentBrokerCorruption("consent broker schema objects are invalid")
    rows = connection.execute("PRAGMA table_info(consent_challenges)").fetchall()
    signature = {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in rows
    }
    if signature != _EXPECTED_COLUMNS:
        raise ConsentBrokerCorruption("consent broker schema columns are invalid")
    identity_rows = connection.execute("PRAGMA table_info(consent_broker_identity)").fetchall()
    identity_signature = {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in identity_rows
    }
    if identity_signature != _EXPECTED_IDENTITY_COLUMNS:
        raise ConsentBrokerCorruption("consent broker identity schema is invalid")
    nonce_rows = connection.execute("PRAGMA table_info(consent_assertion_nonces)").fetchall()
    nonce_signature = {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in nonce_rows
    }
    if nonce_signature != _EXPECTED_NONCE_COLUMNS:
        raise ConsentBrokerCorruption("consent broker nonce history schema is invalid")
    identities = connection.execute(
        "SELECT singleton, audience, trusted_time_high_water, identity_digest "
        "FROM consent_broker_identity"
    ).fetchall()
    if len(identities) != 1 or int(identities[0]["singleton"]) != 1:
        raise ConsentBrokerCorruption("consent broker identity record is invalid")
    stored_audience = identities[0]["audience"]
    trusted_time_high_water = identities[0]["trusted_time_high_water"]
    stored_digest = identities[0]["identity_digest"]
    if not isinstance(stored_audience, str) or not isinstance(stored_digest, str):
        raise ConsentBrokerCorruption("consent broker identity fields are invalid")
    if trusted_time_high_water is not None:
        try:
            trusted_time_high_water = _format_timestamp(
                _parse_timestamp(trusted_time_high_water, "trusted_time_high_water")
            )
        except (TypeError, ValueError) as exc:
            raise ConsentBrokerCorruption(
                "consent broker trusted-time high-water is invalid"
            ) from exc
    if not hmac.compare_digest(
        stored_digest,
        _store_identity_digest(stored_audience, trusted_time_high_water),
    ):
        raise ConsentBrokerCorruption("consent broker identity digest is invalid")
    if stored_audience != audience:
        raise ConsentBrokerDecisionRejected(
            "consent broker audience does not match its durable identity"
        )
    nonces = connection.execute(
        "SELECT challenge_id, sequence, nonce_digest, recorded_at, "
        "previous_record_digest, record_digest "
        "FROM consent_assertion_nonces ORDER BY challenge_id, sequence"
    ).fetchall()
    if len(nonces) > _MAX_CAPACITY * _NONCE_HISTORY_FACTOR:
        raise ConsentBrokerCorruption("consent broker nonce history exceeds its absolute bound")
    anchors = connection.execute(
        "SELECT challenge_id, assertion_nonce_history_count, "
        "assertion_nonce_history_digest FROM consent_challenges ORDER BY challenge_id"
    ).fetchall()
    chains: dict[str, tuple[int, str]] = {}
    try:
        for anchor in anchors:
            challenge_id = _token(anchor["challenge_id"], "nonce.challenge_id")
            count = anchor["assertion_nonce_history_count"]
            if type(count) is not int or not 0 <= count <= (_MAX_CAPACITY * _NONCE_HISTORY_FACTOR):
                raise ValueError("nonce history count is invalid")
            _digest(anchor["assertion_nonce_history_digest"], "nonce.history_digest")
            chains[challenge_id] = (0, _nonce_history_genesis(challenge_id))
        for nonce in nonces:
            challenge_id = _token(nonce["challenge_id"], "nonce.challenge_id")
            if challenge_id not in chains:
                raise ValueError("nonce history challenge is unknown")
            previous_sequence, previous_digest = chains[challenge_id]
            sequence = nonce["sequence"]
            if type(sequence) is not int or sequence != previous_sequence + 1:
                raise ValueError("nonce history sequence is invalid")
            nonce_digest = _digest(nonce["nonce_digest"], "nonce.nonce_digest")
            recorded_at = _format_timestamp(
                _parse_timestamp(nonce["recorded_at"], "nonce.recorded_at")
            )
            persisted_previous = _digest(
                nonce["previous_record_digest"],
                "nonce.previous_record_digest",
            )
            if persisted_previous != previous_digest:
                raise ValueError("nonce history chain predecessor is invalid")
            expected_digest = _nonce_record_digest(
                challenge_id=challenge_id,
                sequence=sequence,
                nonce_digest=nonce_digest,
                recorded_at=recorded_at,
                previous_record_digest=previous_digest,
            )
            if not isinstance(nonce["record_digest"], str) or not hmac.compare_digest(
                nonce["record_digest"], expected_digest
            ):
                raise ValueError("nonce history record digest is invalid")
            chains[challenge_id] = (sequence, expected_digest)
        for anchor in anchors:
            challenge_id = str(anchor["challenge_id"])
            if chains[challenge_id] != (
                anchor["assertion_nonce_history_count"],
                anchor["assertion_nonce_history_digest"],
            ):
                raise ValueError("nonce history anchor is invalid")
    except (TypeError, ValueError) as exc:
        raise ConsentBrokerCorruption("persisted assertion nonce history is invalid") from exc
    missing_current = connection.execute(
        """
        SELECT challenge_id
          FROM consent_challenges AS challenge
         WHERE challenge.assertion_nonce_digest IS NOT NULL
           AND NOT EXISTS (
               SELECT 1
                 FROM consent_assertion_nonces AS nonce
                WHERE nonce.challenge_id = challenge.challenge_id
                  AND nonce.nonce_digest = challenge.assertion_nonce_digest
           )
        LIMIT 1
        """
    ).fetchone()
    if missing_current is not None:
        raise ConsentBrokerCorruption(
            "current assertion nonce is missing from immutable nonce history"
        )


def _require_private_directory(path: Path) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("consent broker parent must be a real directory")
    if os.name != "nt":
        if metadata.st_uid != os.geteuid():
            raise ValueError("consent broker parent must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE:
            raise ValueError("consent broker parent must be owner-private (0700)")
    return metadata


def _require_private_file(path: Path) -> os.stat_result:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError("consent broker database must be an unlinked regular file")
    if os.name != "nt":
        if metadata.st_uid != os.geteuid():
            raise ValueError("consent broker database must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE:
            raise ValueError("consent broker database must be owner-private (0600)")
    return metadata


__all__ = [
    "ConsentBrokerCapacityExceeded",
    "ConsentBrokerChallengeNotFound",
    "ConsentBrokerCorruption",
    "ConsentBrokerDecisionRejected",
    "ConsentBrokerError",
    "ConsentBrokerExpired",
    "ConsentBrokerReconciliationReport",
    "ConsentBrokerRecoveryReport",
    "ConsentBrokerReplay",
    "ConsentBrokerUnavailable",
    "HumanDecisionVerifier",
    "InstallConsentChallenge",
    "InstallConsentChallengeRecord",
    "SQLiteInstallConsentBrokerStore",
    "SignedHumanDecisionAssertion",
    "VerifiedHumanDecision",
]
