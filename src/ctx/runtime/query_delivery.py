"""Host-neutral, durable at-most-once issuance for query-only host hooks.

The engine commits a closed query decision before this boundary.  This module
coordinates cooperating hook processes, renders one exact host envelope,
purges bounded transient decision storage, burns a digest-only terminal before
stdout may be written, and returns a process-bound one-shot emission permit. It
never claims that a host consumed the issued bytes; neither Codex nor Claude
Code provides a delivery acknowledgement.
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
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias

from ctx.core.install_policy_store import default_install_policy_root
from ctx.engine.capability_schema import MAX_HOST_CONTEXT_CHARS
from ctx.engine.installation import InstallConsentDirective
from ctx.runtime._query_attempt_posix import (
    QueryAttemptPool,
    QueryAttemptSlot,
    QueryAttemptStorageError,
    query_attempt_pool_supported,
    validate_query_state_root_parent,
)
from ctx.runtime.query_decision import (
    CommittedQueryDecision,
    QueryDecisionFailure,
    QueryDecisionResult,
    QueryHostDescriptor,
    accept_query_decision,
    prepare_query_decision,
    render_query_decision_context,
)
from ctx.runtime.prepared_query_delivery import (
    PreparedQueryDelivery,
    accept_prepared_query_delivery,
)
from ctx.runtime.activated_skill_availability import (
    ActivatedSkillQueryAvailability,
    open_activated_skill_query_availability,
)
from ctx.runtime.query_session import prepare_query_delivery
from ctx.runtime.prompt_capability_manager import (
    ManagedConsentDirective,
    reconcile_prompt_capabilities,
)
from ctx.runtime.install_consent_continuation import (
    open_prepare_only_managed_install_consent_broker,
)
from ctx.runtime.release_skill_dispatcher import ReleaseSkillConsentChallengeProjection
from ctx.runtime.release_skill_layout import (
    open_release_skill_runtime_layout,
    open_workspace_release_skill_runtime_layout,
)
from ctx.runtime.workspace_identity import WorkspaceIdentity, capture_workspace_identity
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import (
    ensure_secure_directory,
    reject_symlink_path,
    secure_directory,
)


QueryDeliveryStatus: TypeAlias = Literal[
    "legacy",
    "already-terminal",
    "failed",
    "abstained",
    "shadow-ready",
    "shadow-abstained",
    "issued",
]
QueryDecisionFactory: TypeAlias = Callable[
    ...,
    PreparedQueryDelivery | QueryDecisionResult,
]

_MODES: Final = frozenset({"activate", "legacy", "manage", "shadow", "recommend"})
_FORCE_LEGACY_ENV: Final = "CTX_FORCE_LEGACY"
_INSTALL_POLICY_ROOT_ENV: Final = "CTX_INSTALL_POLICY_ROOT"
_TRUTHY: Final = frozenset({"1", "true", "yes", "on"})
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_PRIVATE_FILE_MODE: Final = 0o600
_BUSY_TIMEOUT_MS: Final = 2_000
_DEFAULT_LOCK_TIMEOUT_SECONDS: Final = 2.0
_RUNTIME_ATTEMPTS: Final = 3
_MAX_TERMINAL_ROWS: Final = 65_536
_MAX_NATIVE_ID_BYTES: Final = 4_096
_MAX_PROMPT_BYTES: Final = 256 * 1024
_MAX_LANGUAGE_BYTES: Final = 256
_INSTALLATION_KEY_NAME: Final = "query-delivery-installation-key-v1"
_LEDGER_NAME: Final = "query-delivery-v1.sqlite3"
_LOCK_DIRECTORY_NAME: Final = "query-delivery-locks-v1"
_ATTEMPT_DIRECTORY_NAME: Final = "query-delivery-attempts-v1"
_TERMINAL_SCHEMA_ID: Final = "ctx.query-delivery-terminal-v1"
_KEY_BINDING_SCHEMA_ID: Final = "ctx.query-delivery-key-binding-v1"
_SQLITE_SIDECAR_SUFFIXES: Final = ("-wal", "-shm", "-journal")
_TERMINAL_KINDS: Final = frozenset(
    {"failed", "abstained", "shadow-ready", "shadow-abstained", "issued"}
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_delivery_terminals (
    delivery_key_digest      TEXT PRIMARY KEY NOT NULL,
    terminal_kind_digest     TEXT NOT NULL,
    terminal_result_digest   TEXT NOT NULL,
    record_digest            TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS query_delivery_metadata (
    binding_id_digest                 TEXT PRIMARY KEY NOT NULL,
    installation_key_binding_digest  TEXT NOT NULL,
    record_digest                     TEXT NOT NULL
) WITHOUT ROWID;
"""
_TABLE_SQLS = tuple(
    statement.strip().replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE", 1)
    for statement in _SCHEMA.strip().split(";")
    if statement.strip()
)
_EXPECTED_COLUMNS = {
    "delivery_key_digest": ("TEXT", 1, 1),
    "terminal_kind_digest": ("TEXT", 1, 0),
    "terminal_result_digest": ("TEXT", 1, 0),
    "record_digest": ("TEXT", 1, 0),
}
_EXPECTED_METADATA_COLUMNS = {
    "binding_id_digest": ("TEXT", 1, 1),
    "installation_key_binding_digest": ("TEXT", 1, 0),
    "record_digest": ("TEXT", 1, 0),
}


class QueryDeliveryError(RuntimeError):
    """Base class for closed delivery-boundary failures."""


class QueryDeliveryCorruption(QueryDeliveryError):
    """The digest-only delivery ledger violates its closed contract."""


class QueryDeliveryCapacityReached(QueryDeliveryError):
    """The bounded digest-only delivery ledger cannot accept another key."""


class _BinaryWriteStream(Protocol):
    def write(self, payload: bytes) -> int | None: ...

    def flush(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class SensitiveQueryInput:
    """Raw host input whose values are valid only for this process invocation."""

    native_session_id: str
    logical_prompt_id: str
    workspace: Path
    prompt: str
    language: str = ""
    _workspace_identity: WorkspaceIdentity = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _bounded_text(self.native_session_id, "native_session_id", _MAX_NATIVE_ID_BYTES)
        _bounded_text(self.logical_prompt_id, "logical_prompt_id", _MAX_NATIVE_ID_BYTES)
        _bounded_text(self.prompt, "prompt", _MAX_PROMPT_BYTES, allow_empty=True)
        _bounded_text(self.language, "language", _MAX_LANGUAGE_BYTES, allow_empty=True)
        if not isinstance(self.workspace, Path):
            raise TypeError("workspace must be a Path")
        object.__setattr__(
            self,
            "_workspace_identity",
            capture_workspace_identity(self.workspace),
        )

    def __repr__(self) -> str:
        return "SensitiveQueryInput(<redacted>)"

    def __copy__(self) -> SensitiveQueryInput:
        raise TypeError("SensitiveQueryInput cannot be copied")

    def __deepcopy__(self, _memo: object) -> SensitiveQueryInput:
        raise TypeError("SensitiveQueryInput cannot be copied")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("SensitiveQueryInput cannot be serialized")


@dataclass(frozen=True, slots=True)
class _QueryInvocationRef:
    delivery_key_digest: str
    legacy_delivery_key_digest: str
    invocation_digest: str
    workspace_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "delivery_key_digest",
            "legacy_delivery_key_digest",
            "invocation_digest",
            "workspace_digest",
        ):
            _require_digest(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class _PreparedTerminal:
    kind: str
    result_digest: str
    payload: bytes | None = None

    def __post_init__(self) -> None:
        if self.kind not in _TERMINAL_KINDS:
            raise ValueError("prepared terminal kind is invalid")
        _require_digest(self.result_digest, "result_digest")
        if (self.kind == "issued") != isinstance(self.payload, bytes):
            raise ValueError("only an issued terminal carries a payload")


class QueryEmissionPermit:
    """One process-bound authority to attempt one exact stdout emission."""

    __slots__ = ("_consumed", "_emit_lock", "_payload", "_payload_digest", "_pid")

    def __init__(self, payload: bytes, *, _token: object) -> None:
        if _token is not _PERMIT_TOKEN:
            raise TypeError("emission permits are issued only by the delivery controller")
        self._payload = bytes(payload)
        self._payload_digest = hashlib.sha256(self._payload).hexdigest()
        self._pid = os.getpid()
        self._consumed = False
        self._emit_lock = threading.Lock()

    @property
    def payload_digest(self) -> str:
        return self._payload_digest

    def emit_once(self, stream: _BinaryWriteStream) -> None:
        write = getattr(stream, "write", None)
        flush = getattr(stream, "flush", None)
        if not callable(write) or not callable(flush):
            raise TypeError("stream must provide binary write and flush")
        if os.getpid() != self._pid:
            raise RuntimeError("emission permit belongs to a different process")
        with self._emit_lock:
            if os.getpid() != self._pid:
                raise RuntimeError("emission permit belongs to a different process")
            if self._consumed:
                raise RuntimeError("emission permit is already consumed")
            self._consumed = True
        written = write(self._payload)
        if written is not None and written != len(self._payload):
            raise RuntimeError("emission stream accepted only part of the payload")
        flush()

    def __repr__(self) -> str:
        return f"QueryEmissionPermit(payload_digest={self._payload_digest!r})"

    def __copy__(self) -> QueryEmissionPermit:
        raise TypeError("QueryEmissionPermit cannot be copied")

    def __deepcopy__(self, _memo: object) -> QueryEmissionPermit:
        raise TypeError("QueryEmissionPermit cannot be copied")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("QueryEmissionPermit cannot be serialized")


_PERMIT_TOKEN = object()


@dataclass(frozen=True, slots=True)
class QueryDeliveryReport:
    status: QueryDeliveryStatus
    emission_permit: QueryEmissionPermit | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            "legacy",
            "already-terminal",
            "failed",
            "abstained",
            "shadow-ready",
            "shadow-abstained",
            "issued",
        }:
            raise ValueError("query delivery status is invalid")
        if (self.status == "issued") != isinstance(
            self.emission_permit,
            QueryEmissionPermit,
        ):
            raise ValueError("only an issued report may carry an emission permit")


class QueryDeliveryController:
    """One host-specific controller over the shared query engine and ledger."""

    def __init__(
        self,
        *,
        host: QueryHostDescriptor,
        mode: str,
        state_root: Path,
        environment: Mapping[str, str] | None = None,
        lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if type(host) is not QueryHostDescriptor:
            raise TypeError("host must be an exact QueryHostDescriptor")
        if host not in {QueryHostDescriptor.codex(), QueryHostDescriptor.claude_code()}:
            raise ValueError("query hook delivery supports only Codex and Claude Code")
        if type(mode) is not str or mode not in _MODES:
            raise ValueError("mode must be legacy, shadow, recommend, activate, or manage")
        if not isinstance(state_root, Path):
            raise TypeError("state_root must be a Path")
        if environment is not None and not isinstance(environment, Mapping):
            raise TypeError("environment must be a mapping")
        if type(lock_timeout_seconds) not in {int, float} or lock_timeout_seconds <= 0:
            raise ValueError("lock_timeout_seconds must be positive")
        self._host = QueryHostDescriptor._for(
            host.host_context_id,
            "activate" if mode in {"activate", "manage"} else "recommend",
        )
        self._mode = mode
        self._state_root = Path(os.path.abspath(state_root))
        self._decision_factory: QueryDecisionFactory = (
            prepare_query_delivery if mode in {"activate", "manage"} else prepare_query_decision
        )
        self._managed_availability_enabled = mode in {"activate", "manage"}
        self._environment = os.environ if environment is None else environment
        self._lock_timeout_seconds = float(lock_timeout_seconds)
        self._initialization_lock = threading.Lock()
        self._installation_key: bytes | None = None
        self._ledger: _SQLiteQueryDeliveryLedger | None = None
        self._attempt_pool: QueryAttemptPool | None = None

    @classmethod
    def _for_testing(
        cls,
        *,
        host: QueryHostDescriptor,
        mode: str,
        state_root: Path,
        decision_factory: QueryDecisionFactory,
        environment: Mapping[str, str] | None = None,
        lock_timeout_seconds: float = _DEFAULT_LOCK_TIMEOUT_SECONDS,
    ) -> QueryDeliveryController:
        """Construct with a fake engine boundary for focused failure tests only."""

        if not callable(decision_factory):
            raise TypeError("decision_factory must be callable")
        controller = cls(
            host=host,
            mode=mode,
            state_root=state_root,
            environment=environment,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        controller._decision_factory = decision_factory
        controller._managed_availability_enabled = False
        return controller

    def issue(self, request: SensitiveQueryInput) -> QueryDeliveryReport:
        if type(request) is not SensitiveQueryInput:
            raise TypeError("request must be an exact SensitiveQueryInput")
        if self._force_legacy() or self._mode == "legacy":
            return QueryDeliveryReport(status="legacy")
        if not query_attempt_pool_supported():
            return QueryDeliveryReport(status="failed")
        for attempt in range(_RUNTIME_ATTEMPTS):
            try:
                key, ledger, attempt_pool = self._runtime()
                slot_reference = _derive_invocation_ref(
                    key=key,
                    host=self._host,
                    request=request,
                )
                with attempt_pool.acquire(slot_reference.delivery_key_digest) as attempt_slot:
                    if ledger.contains(slot_reference.delivery_key_digest) or ledger.contains(
                        slot_reference.legacy_delivery_key_digest
                    ):
                        return QueryDeliveryReport(status="already-terminal")
                    (
                        availability,
                        availability_epoch_digest,
                        consent_directives,
                        consent_challenges,
                    ) = self._managed_availability(request)
                    reference = _derive_invocation_ref(
                        key=key,
                        host=self._host,
                        request=request,
                        availability_epoch_digest=availability_epoch_digest,
                    )
                    if (
                        reference.delivery_key_digest != slot_reference.delivery_key_digest
                        or reference.legacy_delivery_key_digest
                        != slot_reference.legacy_delivery_key_digest
                    ):
                        raise QueryDeliveryError("availability changed the logical prompt slot")
                    return self._issue_under_lock(
                        request=request,
                        reference=reference,
                        availability=availability,
                        consent_directives=consent_directives,
                        consent_challenges=consent_challenges,
                        ledger=ledger,
                        attempt_slot=attempt_slot,
                    )
            except QueryDeliveryCapacityReached:
                return QueryDeliveryReport(status="failed")
            except Exception:
                if attempt + 1 == _RUNTIME_ATTEMPTS:
                    return QueryDeliveryReport(status="failed")
        raise AssertionError("query delivery retry loop exhausted unexpectedly")

    def _issue_under_lock(
        self,
        *,
        request: SensitiveQueryInput,
        reference: _QueryInvocationRef,
        availability: ActivatedSkillQueryAvailability | None,
        consent_directives: tuple[ManagedConsentDirective, ...],
        consent_challenges: tuple[ReleaseSkillConsentChallengeProjection, ...],
        ledger: _SQLiteQueryDeliveryLedger,
        attempt_slot: QueryAttemptSlot,
    ) -> QueryDeliveryReport:
        attempt_id = secrets.token_hex(16)
        try:
            with attempt_slot.transient_directory() as attempt_directory:
                prepared = self._prepare_terminal(
                    request=request,
                    reference=reference,
                    availability=availability,
                    consent_directives=consent_directives,
                    consent_challenges=consent_challenges,
                    attempt_id=attempt_id,
                    attempt_directory=attempt_directory,
                )
        except QueryAttemptStorageError:
            raise
        except Exception:
            prepared = _PreparedTerminal(
                kind="failed",
                result_digest=_terminal_result_digest(
                    kind="failed",
                    reference=reference,
                    values={"failure_code_digest": _text_digest("closed-boundary-failed")},
                ),
            )
        return self._commit_prepared_terminal(
            ledger=ledger,
            reference=reference,
            prepared=prepared,
        )

    def _prepare_terminal(
        self,
        *,
        request: SensitiveQueryInput,
        reference: _QueryInvocationRef,
        availability: ActivatedSkillQueryAvailability | None,
        consent_directives: tuple[ManagedConsentDirective, ...],
        consent_challenges: tuple[ReleaseSkillConsentChallengeProjection, ...],
        attempt_id: str,
        attempt_directory: Path,
    ) -> _PreparedTerminal:
        if consent_directives and consent_challenges:
            raise QueryDeliveryError("managed consent has multiple representations")
        if consent_challenges:
            if self._mode != "manage":
                raise QueryDeliveryError("install consent is available only in manage mode")
            payload = _project_managed_install_challenge_context_envelope(consent_challenges)
            return _PreparedTerminal(
                kind="issued",
                result_digest=_terminal_result_digest(
                    kind="issued",
                    reference=reference,
                    values={
                        "challenges_digest": _digest_managed_install_challenges(consent_challenges),
                        "envelope_digest": hashlib.sha256(payload).hexdigest(),
                    },
                ),
                payload=payload,
            )
        if consent_directives:
            if self._mode != "manage":
                raise QueryDeliveryError("install consent is available only in manage mode")
            payload = _project_managed_install_recommendation_context_envelope(consent_directives)
            return _PreparedTerminal(
                kind="issued",
                result_digest=_terminal_result_digest(
                    kind="issued",
                    reference=reference,
                    values={
                        "recommendations_digest": _digest_managed_install_recommendations(
                            consent_directives
                        ),
                        "envelope_digest": hashlib.sha256(payload).hexdigest(),
                    },
                ),
                payload=payload,
            )
        decision_kwargs: dict[str, object] = {
            "host": self._host,
            "task": request.prompt,
            "language": request.language,
            "session_id": f"query-{attempt_id}",
            "workspace": request.workspace,
            "journal_path": attempt_directory / "engine.sqlite3",
            "benefit_audit_path": attempt_directory / "benefit.sqlite3",
            "host_invocation_digest": reference.invocation_digest,
        }
        if self._managed_availability_enabled:
            decision_kwargs["managed_availability"] = availability
        result = self._decision_factory(
            **decision_kwargs,
        )
        if self._mode in {"activate", "manage"}:
            if isinstance(result, PreparedQueryDelivery):
                prepared = accept_prepared_query_delivery(result, host=self._host)
                decision = prepared.decision
                if decision.host_invocation_digest != reference.invocation_digest:
                    raise QueryDeliveryError("prepared receipt does not bind this invocation")
                payload = _project_exact_context_envelope(prepared.context)
                return _PreparedTerminal(
                    kind="issued",
                    result_digest=_terminal_result_digest(
                        kind="issued",
                        reference=reference,
                        values={
                            "receipt_digest": decision.receipt_digest,
                            "prepared_delivery_digest": prepared.delivery_digest,
                            "receipt_event_content_digest": (prepared.receipt_event_content_digest),
                            "context_sha256": prepared.context_sha256,
                            "envelope_digest": hashlib.sha256(payload).hexdigest(),
                        },
                    ),
                    payload=payload,
                )
            accepted = accept_query_decision(result, host=self._host)
            if isinstance(accepted, QueryDecisionFailure):
                return _PreparedTerminal(
                    kind="failed",
                    result_digest=_terminal_result_digest(
                        kind="failed",
                        reference=reference,
                        values={"failure_code_digest": _text_digest(accepted.failure_code)},
                    ),
                )
            if (
                accepted.host_invocation_digest != reference.invocation_digest
                or accepted.capabilities
            ):
                raise QueryDeliveryError("activate result is not an exact abstention")
            return _PreparedTerminal(
                kind="abstained",
                result_digest=_terminal_result_digest(
                    kind="abstained",
                    reference=reference,
                    values={"receipt_digest": accepted.receipt_digest},
                ),
            )
        accepted = accept_query_decision(result, host=self._host)
        if isinstance(accepted, QueryDecisionFailure):
            return _PreparedTerminal(
                kind="failed",
                result_digest=_terminal_result_digest(
                    kind="failed",
                    reference=reference,
                    values={"failure_code_digest": _text_digest(accepted.failure_code)},
                ),
            )
        if accepted.host_invocation_digest != reference.invocation_digest:
            raise QueryDeliveryError("query receipt does not bind this invocation")
        if not accepted.capabilities:
            kind = "shadow-abstained" if self._mode == "shadow" else "abstained"
            return _PreparedTerminal(
                kind=kind,
                result_digest=_terminal_result_digest(
                    kind=kind,
                    reference=reference,
                    values={"receipt_digest": accepted.receipt_digest},
                ),
            )
        if self._mode == "shadow":
            return _PreparedTerminal(
                kind="shadow-ready",
                result_digest=_terminal_result_digest(
                    kind="shadow-ready",
                    reference=reference,
                    values={"receipt_digest": accepted.receipt_digest},
                ),
            )
        payload = _project_exact_envelope(decision=accepted, host=self._host)
        return _PreparedTerminal(
            kind="issued",
            result_digest=_terminal_result_digest(
                kind="issued",
                reference=reference,
                values={
                    "receipt_digest": accepted.receipt_digest,
                    "presentation_action_content_digest": (
                        accepted.presentation_action_content_digest
                    ),
                    "envelope_digest": hashlib.sha256(payload).hexdigest(),
                },
            ),
            payload=payload,
        )

    def _commit_prepared_terminal(
        self,
        *,
        ledger: _SQLiteQueryDeliveryLedger,
        reference: _QueryInvocationRef,
        prepared: _PreparedTerminal,
    ) -> QueryDeliveryReport:
        if prepared.kind != "issued":
            return self._commit_terminal(
                ledger=ledger,
                reference=reference,
                terminal_kind=prepared.kind,
                terminal_result_digest=prepared.result_digest,
            )
        assert prepared.payload is not None
        inserted = ledger.commit(
            delivery_key_digest=reference.delivery_key_digest,
            terminal_kind="issued",
            terminal_result_digest=prepared.result_digest,
        )
        if not inserted:
            return QueryDeliveryReport(status="already-terminal")
        return QueryDeliveryReport(
            status="issued",
            emission_permit=QueryEmissionPermit(prepared.payload, _token=_PERMIT_TOKEN),
        )

    @staticmethod
    def _commit_terminal(
        *,
        ledger: _SQLiteQueryDeliveryLedger,
        reference: _QueryInvocationRef,
        terminal_kind: str,
        terminal_result_digest: str,
    ) -> QueryDeliveryReport:
        inserted = ledger.commit(
            delivery_key_digest=reference.delivery_key_digest,
            terminal_kind=terminal_kind,
            terminal_result_digest=terminal_result_digest,
        )
        if not inserted:
            return QueryDeliveryReport(status="already-terminal")
        if terminal_kind not in {
            "failed",
            "abstained",
            "shadow-ready",
            "shadow-abstained",
        }:
            raise QueryDeliveryError("terminal kind has no report status")
        return QueryDeliveryReport(status=terminal_kind)  # type: ignore[arg-type]

    def _runtime(
        self,
    ) -> tuple[bytes, _SQLiteQueryDeliveryLedger, QueryAttemptPool]:
        with self._initialization_lock:
            if self._ledger is None:
                validate_query_state_root_parent(self._state_root)
                ensure_secure_directory(self._state_root)
                _require_private_directory(self._state_root)
                lock_root = self._state_root / _LOCK_DIRECTORY_NAME
                attempt_root = self._state_root / _ATTEMPT_DIRECTORY_NAME
                ensure_secure_directory(lock_root)
                ensure_secure_directory(attempt_root)
                _require_private_directory(lock_root)
                _require_private_directory(attempt_root)
                attempt_pool = QueryAttemptPool(
                    root=attempt_root,
                    lock_root=lock_root,
                    timeout=self._lock_timeout_seconds,
                )
                installation_key = _load_or_create_installation_key(
                    self._state_root,
                    timeout=self._lock_timeout_seconds,
                )
                ledger = _SQLiteQueryDeliveryLedger(
                    self._state_root / _LEDGER_NAME,
                    installation_key=installation_key,
                    busy_timeout_ms=max(1, int(self._lock_timeout_seconds * 1000)),
                )
                self._installation_key = installation_key
                self._ledger = ledger
                self._attempt_pool = attempt_pool
            assert self._ledger is not None
            assert self._installation_key is not None
            assert self._attempt_pool is not None
            return (
                self._installation_key,
                self._ledger,
                self._attempt_pool,
            )

    def _force_legacy(self) -> bool:
        raw = self._environment.get(_FORCE_LEGACY_ENV, "")
        return isinstance(raw, str) and raw.strip().lower() in _TRUTHY

    def _managed_availability(
        self,
        request: SensitiveQueryInput,
    ) -> tuple[
        ActivatedSkillQueryAvailability | None,
        str | None,
        tuple[ManagedConsentDirective, ...],
        tuple[ReleaseSkillConsentChallengeProjection, ...],
    ]:
        if not self._managed_availability_enabled:
            return None, None, (), ()
        try:
            if self._mode == "manage":
                layout = open_workspace_release_skill_runtime_layout(
                    state_root=self._state_root,
                    policy_store_root=self._install_policy_root(),
                    workspace=request.workspace,
                )
                consent_broker = open_prepare_only_managed_install_consent_broker(
                    layout=layout,
                    trusted_utc_now=lambda: datetime.now(UTC),
                )
                outcome = reconcile_prompt_capabilities(
                    layout=layout,
                    task=request.prompt,
                    language=request.language,
                    consent_broker=consent_broker,
                )
                if outcome.status == "failed":
                    raise QueryDeliveryError("managed capability reconciliation failed")
                return (
                    outcome.availability,
                    outcome.management_epoch_digest,
                    outcome.consent_directives,
                    outcome.consent_challenges,
                )
            layout = open_release_skill_runtime_layout(
                state_root=self._state_root,
                host_context_id=self._host.host_context_id,
                native_session_id=request.native_session_id,
                workspace=request.workspace,
            )
            availability = open_activated_skill_query_availability(
                layout=layout,
                task=request.prompt,
                language=request.language,
                occurred_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )
            return availability, availability.activation_epoch_digest, (), ()
        except Exception:
            if self._mode == "manage":
                raise QueryDeliveryError("managed capability reconciliation failed") from None
            return (
                None,
                _text_digest(
                    "\0".join(
                        (
                            "ctx-managed-availability-unavailable-v1",
                            self._host.host_context_id,
                            request._workspace_identity.digest,
                            _text_digest(request.native_session_id),
                        )
                    )
                ),
                (),
                (),
            )

    def _install_policy_root(self) -> Path:
        configured = self._environment.get(_INSTALL_POLICY_ROOT_ENV, "")
        if configured:
            if not isinstance(configured, str):
                raise QueryDeliveryError("install policy root is invalid")
            root = Path(configured)
            if not root.is_absolute():
                raise QueryDeliveryError("install policy root must be absolute")
            return root
        return default_install_policy_root()


class _SQLiteQueryDeliveryLedger:
    """Append-only, digest-only terminal ledger for cooperating hook processes."""

    def __init__(
        self,
        path: Path,
        *,
        installation_key: bytes,
        busy_timeout_ms: int = _BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(os.path.abspath(path))
        if type(installation_key) is not bytes or len(installation_key) != 32:
            raise ValueError("installation_key must be exactly 32 bytes")
        self._installation_key = installation_key
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be an integer >= 1")
        self._busy_timeout_ms = busy_timeout_ms
        reject_symlink_path(self.path)
        ensure_secure_directory(self.path.parent)
        _require_private_directory(self.path.parent)
        with secure_file_lock(self.path, timeout=busy_timeout_ms / 1000):
            created_metadata = self._prepare_path()
            try:
                with self._connect(initialize=created_metadata is not None):
                    pass
            except Exception:
                if created_metadata is not None:
                    _cleanup_failed_initialization(
                        self.path,
                        created_metadata,
                        cleanup_sidecars=True,
                    )
                raise

    def contains(self, delivery_key_digest: str) -> bool:
        _require_digest(delivery_key_digest, "delivery_key_digest")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT delivery_key_digest, terminal_kind_digest,
                       terminal_result_digest, record_digest
                  FROM query_delivery_terminals
                 WHERE delivery_key_digest = ?
                """,
                (delivery_key_digest,),
            ).fetchone()
        if row is None:
            return False
        _validate_terminal_row(row)
        return True

    def commit(
        self,
        *,
        delivery_key_digest: str,
        terminal_kind: str,
        terminal_result_digest: str,
    ) -> bool:
        _require_digest(delivery_key_digest, "delivery_key_digest")
        if terminal_kind not in _TERMINAL_KINDS:
            raise ValueError("terminal_kind is invalid")
        _require_digest(terminal_result_digest, "terminal_result_digest")
        terminal_kind_digest = _text_digest(f"ctx.query-delivery-kind-v1:{terminal_kind}")
        record_digest = _record_digest(
            delivery_key_digest=delivery_key_digest,
            terminal_kind_digest=terminal_kind_digest,
            terminal_result_digest=terminal_result_digest,
        )
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT delivery_key_digest, terminal_kind_digest,
                           terminal_result_digest, record_digest
                      FROM query_delivery_terminals
                     WHERE delivery_key_digest = ?
                    """,
                    (delivery_key_digest,),
                ).fetchone()
                if row is not None:
                    _validate_terminal_row(row)
                    if (
                        str(row["terminal_kind_digest"]) != terminal_kind_digest
                        or str(row["terminal_result_digest"]) != terminal_result_digest
                        or str(row["record_digest"]) != record_digest
                    ):
                        raise QueryDeliveryCorruption(
                            "delivery key is already bound to a different terminal"
                        )
                    connection.execute("COMMIT")
                    return False
                count_row = connection.execute(
                    "SELECT COUNT(*) AS terminal_count FROM query_delivery_terminals"
                ).fetchone()
                if count_row is None or int(count_row["terminal_count"]) >= _MAX_TERMINAL_ROWS:
                    raise QueryDeliveryCapacityReached(
                        "query delivery terminal capacity is exhausted"
                    )
                connection.execute(
                    """
                    INSERT INTO query_delivery_terminals (
                        delivery_key_digest, terminal_kind_digest,
                        terminal_result_digest, record_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        delivery_key_digest,
                        terminal_kind_digest,
                        terminal_result_digest,
                        record_digest,
                    ),
                )
                inserted = connection.execute(
                    """
                    SELECT delivery_key_digest, terminal_kind_digest,
                           terminal_result_digest, record_digest
                      FROM query_delivery_terminals
                     WHERE delivery_key_digest = ?
                    """,
                    (delivery_key_digest,),
                ).fetchone()
                if inserted is None:
                    raise QueryDeliveryCorruption("terminal insert was not preserved")
                _validate_terminal_row(inserted)
                if str(inserted["record_digest"]) != record_digest:
                    raise QueryDeliveryCorruption("terminal insert changed before commit")
                connection.execute("COMMIT")
                return True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _prepare_path(self) -> os.stat_result | None:
        created_metadata: os.stat_result | None = None
        try:
            reject_symlink_path(self.path)
            if self.path.exists():
                _require_private_file(self.path)
                return None
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, _PRIVATE_FILE_MODE)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, _PRIVATE_FILE_MODE)
                created_metadata = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            _require_no_sqlite_sidecars(self.path)
            _require_private_file(self.path)
            return created_metadata
        except Exception:
            if created_metadata is not None:
                _cleanup_failed_initialization(
                    self.path,
                    created_metadata,
                    cleanup_sidecars=False,
                )
            raise

    @contextmanager
    def _connect(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        reject_symlink_path(self.path)
        _require_private_directory(self.path.parent)
        _require_private_file(self.path)
        _secure_sqlite_files(self.path)
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
            if initialize:
                connection.executescript(_SCHEMA)
            _require_ledger_schema(connection)
            _require_key_binding(
                connection,
                key=self._installation_key,
                initialize=initialize,
            )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            _secure_sqlite_files(self.path)
            yield connection
        except QueryDeliveryError:
            raise
        except sqlite3.DatabaseError as exc:
            raise QueryDeliveryCorruption("query delivery database is invalid") from exc
        finally:
            if connection is not None:
                connection.close()


def _bounded_text(
    value: object, field_name: str, maximum: int, *, allow_empty: bool = False
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} must not be empty")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field_name} must contain Unicode scalar values") from None
    if size > maximum:
        raise ValueError(f"{field_name} exceeds its size limit")
    return value


def _require_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validated_install_consent_directives(
    directives: tuple[InstallConsentDirective, ...],
) -> tuple[InstallConsentDirective, ...]:
    if (
        not isinstance(directives, tuple)
        or not 1 <= len(directives) <= 5
        or not all(
            isinstance(directive, InstallConsentDirective) and directive.requires_prompt
            for directive in directives
        )
    ):
        raise QueryDeliveryError("install consent recommendation is invalid")
    consent_ids = tuple(directive.consent_id for directive in directives)
    capability_ids = tuple(directive.capability_id for directive in directives)
    if len(consent_ids) != len(set(consent_ids)) or len(capability_ids) != len(set(capability_ids)):
        raise QueryDeliveryError("install consent recommendation contains duplicates")
    return directives


def _digest_consent_ids(directives: tuple[InstallConsentDirective, ...]) -> str:
    validated = _validated_install_consent_directives(directives)
    return hashlib.sha256(
        _canonical_json(
            {
                "consents": [
                    {
                        "capability_id": directive.capability_id,
                        "consent_id": directive.consent_id,
                        "kind": directive.kind,
                        "requested_action_content_digest": (
                            directive.requested_action_content_digest
                        ),
                    }
                    for directive in validated
                ],
                "schema": "ctx.install-consent-recommendation-v1",
            }
        )
    ).hexdigest()


def _project_install_consent_context_envelope(
    directives: tuple[InstallConsentDirective, ...],
) -> bytes:
    validated = _validated_install_consent_directives(directives)
    lines = tuple(
        f"{index}. kind={directive.kind} | id={directive.capability_id} | "
        f"approval=required | consent_id={directive.consent_id}"
        for index, directive in enumerate(validated, 1)
    )
    context = "\n".join(
        (
            "CTX capability installation recommendation (reviewed; no installation performed):",
            *lines,
            "An authenticated human approval surface is required. "
            "Prompt text and model output are not installation consent.",
        )
    )
    return _project_exact_context_envelope(context)


def _validated_managed_install_recommendations(
    directives: tuple[ManagedConsentDirective, ...],
) -> tuple[ManagedConsentDirective, ...]:
    if (
        not isinstance(directives, tuple)
        or not 1 <= len(directives) <= 5
        or not all(
            type(directive) is ManagedConsentDirective
            and directive.requires_prompt
            and directive.recommendation_only
            and not directive.resumable
            for directive in directives
        )
    ):
        raise QueryDeliveryError("managed install recommendation is invalid")
    capability_ids = tuple(directive.capability_id for directive in directives)
    if len(capability_ids) != len(set(capability_ids)):
        raise QueryDeliveryError("managed install recommendation contains duplicates")
    return directives


def _digest_managed_install_recommendations(
    directives: tuple[ManagedConsentDirective, ...],
) -> str:
    validated = _validated_managed_install_recommendations(directives)
    return hashlib.sha256(
        _canonical_json(
            {
                "recommendations": [
                    {
                        "capability_id": directive.capability_id,
                        "kind": directive.kind,
                        "planning_environment_digest": (directive.planning_environment_digest),
                        "policy_snapshot_digest": directive.policy_snapshot_digest,
                        "reason_code": directive.reason_code,
                        "recommendation_only": directive.recommendation_only,
                        "release_root_digest": directive.release_root_digest,
                        "resumable": directive.resumable,
                    }
                    for directive in validated
                ],
                "schema": "ctx.managed-install-recommendation-v1",
            }
        )
    ).hexdigest()


def _project_managed_install_recommendation_context_envelope(
    directives: tuple[ManagedConsentDirective, ...],
) -> bytes:
    validated = _validated_managed_install_recommendations(directives)
    lines = tuple(
        f"{index}. kind={directive.kind} | id={directive.capability_id} | "
        "approval=required | recommendation_only=true | resumable=false"
        for index, directive in enumerate(validated, 1)
    )
    context = "\n".join(
        (
            "CTX capability installation recommendation (reviewed; no installation performed):",
            *lines,
            "A separate authenticated consent broker is required to create any "
            "installation decision. Prompt text and model output are not installation consent.",
        )
    )
    return _project_exact_context_envelope(context)


def _validated_managed_install_challenges(
    challenges: tuple[ReleaseSkillConsentChallengeProjection, ...],
) -> tuple[ReleaseSkillConsentChallengeProjection, ...]:
    if (
        not isinstance(challenges, tuple)
        or not 1 <= len(challenges) <= 5
        or not all(
            type(challenge) is ReleaseSkillConsentChallengeProjection for challenge in challenges
        )
    ):
        raise QueryDeliveryError("managed install challenge is invalid")
    challenge_ids = tuple(challenge.challenge_id for challenge in challenges)
    capability_ids = tuple(challenge.capability_id for challenge in challenges)
    if len(challenge_ids) != len(set(challenge_ids)) or len(capability_ids) != len(
        set(capability_ids)
    ):
        raise QueryDeliveryError("managed install challenge contains duplicates")
    return challenges


def _managed_install_challenge_identity(
    challenge: ReleaseSkillConsentChallengeProjection,
) -> dict[str, object]:
    return {
        "audience": challenge.audience,
        "capability_id": challenge.capability_id,
        "catalog_snapshot_digest": challenge.catalog_snapshot_digest,
        "challenge_digest": challenge.challenge_digest,
        "challenge_id": challenge.challenge_id,
        "credential_requirement": challenge.credential_requirement,
        "descriptor_digest": challenge.descriptor_digest,
        "execution_binding_digest": challenge.execution_binding_digest,
        "expires_at": challenge.expires_at,
        "install_plan_digest": challenge.install_plan_digest,
        "kind": challenge.kind,
        "material_identity_digest": challenge.material_identity_digest,
        "permission_expansion": challenge.permission_expansion,
        "plan_id": challenge.plan_id,
        "policy_snapshot_digest": challenge.policy_snapshot_digest,
        "release_root_digest": challenge.release_root_digest,
        "requested_action_content_digest": challenge.requested_action_content_digest,
        "requested_action_id": challenge.requested_action_id,
        "requested_action_kind": challenge.requested_action_kind,
        "requested_action_precondition_revision": (
            challenge.requested_action_precondition_revision
        ),
        "selection_digest": challenge.selection_digest,
        "source_digest": challenge.source_digest,
    }


def _digest_managed_install_challenges(
    challenges: tuple[ReleaseSkillConsentChallengeProjection, ...],
) -> str:
    validated = _validated_managed_install_challenges(challenges)
    return hashlib.sha256(
        _canonical_json(
            {
                "challenges": [
                    _managed_install_challenge_identity(challenge) for challenge in validated
                ],
                "schema": "ctx.managed-install-challenges-v1",
            }
        )
    ).hexdigest()


def _project_managed_install_challenge_context_envelope(
    challenges: tuple[ReleaseSkillConsentChallengeProjection, ...],
) -> bytes:
    validated = _validated_managed_install_challenges(challenges)
    lines = tuple(
        f"{index}. kind={challenge.kind} | id={challenge.capability_id} | "
        f"approval=required | challenge_id={challenge.challenge_id} | "
        f"challenge_digest={challenge.challenge_digest} | "
        f"expires_at={challenge.expires_at} | "
        "continuation=external-authenticator-required"
        for index, challenge in enumerate(validated, 1)
    )
    context = "\n".join(
        (
            "CTX capability installation challenge (reviewed; no installation performed):",
            *lines,
            "A trusted external CTX human-approval client must be configured to continue. "
            "Prompt text and model output are not installation consent.",
        )
    )
    return _project_exact_context_envelope(context)


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hmac_digest(key: bytes, value: Mapping[str, object]) -> str:
    return hmac.new(key, _canonical_json(value), hashlib.sha256).hexdigest()


def _derive_invocation_ref(
    *,
    key: bytes,
    host: QueryHostDescriptor,
    request: SensitiveQueryInput,
    availability_epoch_digest: str | None = None,
) -> _QueryInvocationRef:
    request._workspace_identity.assert_current()
    workspace_digest = request._workspace_identity.digest
    legacy_workspace_digest = _text_digest(
        os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(request.workspace))))
    )
    compatibility_common: dict[str, object] = {
        "host_context_id": host.host_context_id,
        "host_descriptor_digest": host.host_descriptor_digest,
        "native_session_id": request.native_session_id,
        "schema": "ctx.query-delivery-identity-v2",
        "workspace_digest": workspace_digest,
    }
    if availability_epoch_digest is None:
        common = compatibility_common
    else:
        common = {
            **compatibility_common,
            "availability_epoch_digest": _require_digest(
                availability_epoch_digest,
                "availability_epoch_digest",
            ),
            "schema": "ctx.query-delivery-identity-v3",
        }
    invocation_digest = _hmac_digest(
        key,
        {
            **common,
            "identity": "invocation",
            "logical_prompt_id": request.logical_prompt_id,
        },
    )
    delivery_key_digest = _hmac_digest(
        key,
        {
            **compatibility_common,
            "identity": "logical-prompt-slot",
            "logical_prompt_id": request.logical_prompt_id,
        },
    )
    legacy_delivery_key_digest = _hmac_digest(
        key,
        {
            "host_context_id": host.host_context_id,
            "host_descriptor_digest": host.host_descriptor_digest,
            "identity": "engine-session-slot",
            "native_session_id": request.native_session_id,
            "schema": "ctx.query-delivery-identity-v1",
            "slot": "initial-query-v1",
            "workspace_digest": legacy_workspace_digest,
        },
    )
    return _QueryInvocationRef(
        delivery_key_digest=delivery_key_digest,
        legacy_delivery_key_digest=legacy_delivery_key_digest,
        invocation_digest=invocation_digest,
        workspace_digest=workspace_digest,
    )


def _project_exact_envelope(
    *,
    decision: CommittedQueryDecision,
    host: QueryHostDescriptor,
) -> bytes:
    context = render_query_decision_context(decision, host=host)
    if context is None:
        raise QueryDeliveryError("sealed decision has no bounded hook context")
    return _project_exact_context_envelope(context)


def _project_exact_context_envelope(context: str) -> bytes:
    if not isinstance(context, str) or not context or len(context) > MAX_HOST_CONTEXT_CHARS:
        raise QueryDeliveryError("sealed decision has no bounded hook context")
    canonical = {
        "hookSpecificOutput": {
            "additionalContext": context,
            "hookEventName": "UserPromptSubmit",
        }
    }
    return _canonical_json(canonical) + b"\n"


def _terminal_result_digest(
    *,
    kind: str,
    reference: _QueryInvocationRef,
    values: Mapping[str, object],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "delivery_key_digest": reference.delivery_key_digest,
                "invocation_digest": reference.invocation_digest,
                "kind": kind,
                "schema": "ctx.query-delivery-result-v1",
                "values": dict(values),
            }
        )
    ).hexdigest()


def _record_digest(
    *,
    delivery_key_digest: str,
    terminal_kind_digest: str,
    terminal_result_digest: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "delivery_key_digest": delivery_key_digest,
                "schema": _TERMINAL_SCHEMA_ID,
                "terminal_kind_digest": terminal_kind_digest,
                "terminal_result_digest": terminal_result_digest,
            }
        )
    ).hexdigest()


def _load_or_create_installation_key(root: Path, *, timeout: float) -> bytes:
    target = root / _INSTALLATION_KEY_NAME
    ledger = root / _LEDGER_NAME
    with secure_file_lock(target, timeout=timeout):
        with secure_directory(root, create=False) as directory:
            if directory.exists(_INSTALLATION_KEY_NAME):
                encoded = directory.read_text(_INSTALLATION_KEY_NAME, encoding="ascii")
            else:
                if any(
                    os.path.lexists(candidate)
                    for candidate in (
                        ledger,
                        *(Path(f"{ledger}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES),
                    )
                ):
                    raise QueryDeliveryCorruption(
                        "query delivery installation key is missing for existing state"
                    )
                encoded = secrets.token_hex(32)
                directory.atomic_write_text(_INSTALLATION_KEY_NAME, encoded, encoding="ascii")
    _require_private_file(target)
    if _SHA256_RE.fullmatch(encoded) is None:
        raise QueryDeliveryCorruption("query delivery installation key is invalid")
    return bytes.fromhex(encoded)


def _validate_terminal_row(row: sqlite3.Row) -> None:
    delivery_key_digest = _require_digest(row["delivery_key_digest"], "delivery_key_digest")
    terminal_kind_digest = _require_digest(row["terminal_kind_digest"], "terminal_kind_digest")
    terminal_result_digest = _require_digest(
        row["terminal_result_digest"],
        "terminal_result_digest",
    )
    record_digest = _require_digest(row["record_digest"], "record_digest")
    expected = _record_digest(
        delivery_key_digest=delivery_key_digest,
        terminal_kind_digest=terminal_kind_digest,
        terminal_result_digest=terminal_result_digest,
    )
    if record_digest != expected:
        raise QueryDeliveryCorruption("query delivery terminal digest is invalid")


def _require_ledger_schema(connection: sqlite3.Connection) -> None:
    objects = connection.execute(
        """
        SELECT type, name, tbl_name, sql
          FROM sqlite_master
         WHERE name NOT LIKE 'sqlite_%'
         ORDER BY type, name
        """
    ).fetchall()
    expected_objects = (
        (
            "table",
            "query_delivery_metadata",
            "query_delivery_metadata",
            _TABLE_SQLS[1],
        ),
        (
            "table",
            "query_delivery_terminals",
            "query_delivery_terminals",
            _TABLE_SQLS[0],
        ),
    )
    actual_objects = tuple(
        (
            str(row["type"]),
            str(row["name"]),
            str(row["tbl_name"]),
            str(row["sql"]),
        )
        for row in objects
    )
    if actual_objects != expected_objects:
        raise QueryDeliveryCorruption("query delivery database objects are invalid")
    columns = connection.execute("PRAGMA table_info(query_delivery_terminals)").fetchall()
    signature = {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in columns
    }
    if signature != _EXPECTED_COLUMNS:
        raise QueryDeliveryCorruption("query delivery database schema is invalid")
    metadata_columns = connection.execute("PRAGMA table_info(query_delivery_metadata)").fetchall()
    metadata_signature = {
        str(row["name"]): (str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in metadata_columns
    }
    if metadata_signature != _EXPECTED_METADATA_COLUMNS:
        raise QueryDeliveryCorruption("query delivery metadata schema is invalid")


def _require_key_binding(
    connection: sqlite3.Connection,
    *,
    key: bytes,
    initialize: bool,
) -> None:
    binding_id_digest = _text_digest("ctx.query-delivery-key-binding-id-v1")
    key_binding_digest = _hmac_digest(
        key,
        {
            "binding": "installation-key",
            "schema": _KEY_BINDING_SCHEMA_ID,
        },
    )
    record_digest = hashlib.sha256(
        _canonical_json(
            {
                "binding_id_digest": binding_id_digest,
                "installation_key_binding_digest": key_binding_digest,
                "schema": _KEY_BINDING_SCHEMA_ID,
            }
        )
    ).hexdigest()
    if initialize:
        connection.execute(
            """
            INSERT INTO query_delivery_metadata (
                binding_id_digest, installation_key_binding_digest, record_digest
            ) VALUES (?, ?, ?)
            """,
            (binding_id_digest, key_binding_digest, record_digest),
        )
    rows = connection.execute(
        """
        SELECT binding_id_digest, installation_key_binding_digest, record_digest
          FROM query_delivery_metadata
        """
    ).fetchall()
    if len(rows) != 1:
        raise QueryDeliveryCorruption("query delivery key binding is missing or duplicated")
    row = rows[0]
    actual = tuple(
        _require_digest(row[field_name], field_name)
        for field_name in (
            "binding_id_digest",
            "installation_key_binding_digest",
            "record_digest",
        )
    )
    if not hmac.compare_digest(
        "\x00".join(actual),
        "\x00".join((binding_id_digest, key_binding_digest, record_digest)),
    ):
        raise QueryDeliveryCorruption("query delivery installation key changed")


def _require_private_directory(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("query delivery parent must be a real directory")
    if os.name == "nt":
        return
    if metadata.st_uid != os.geteuid():
        raise ValueError("query delivery parent must be owned by the current user")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o700 != 0o700:
        raise ValueError("query delivery parent must be owner-private")


def _require_private_file(path: Path) -> None:
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("query delivery file must be regular")
    if metadata.st_nlink != 1:
        raise ValueError("query delivery file must not be hard-linked")
    if os.name == "nt":
        return
    if metadata.st_uid != os.geteuid():
        raise ValueError("query delivery file must be owned by the current user")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077 or mode & 0o600 != 0o600:
        raise ValueError("query delivery file must be owner-private")


def _secure_sqlite_files(path: Path) -> None:
    _require_private_file(path)
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        candidate = Path(f"{path}{suffix}")
        _secure_optional_sqlite_sidecar(candidate)


def _secure_optional_sqlite_sidecar(path: Path) -> None:
    """Authenticate a SQLite sidecar while tolerating normal disappearance."""

    for attempt in range(_RUNTIME_ATTEMPTS):
        if not os.path.lexists(path):
            return
        try:
            reject_symlink_path(path)
            _require_private_file(path)
            os.chmod(path, _PRIVATE_FILE_MODE)
            _require_private_file(path)
            return
        except FileNotFoundError:
            if not os.path.lexists(path):
                return
            if attempt + 1 == _RUNTIME_ATTEMPTS:
                raise QueryDeliveryCorruption(
                    "query delivery SQLite sidecar changed during authentication"
                )
    raise AssertionError("SQLite sidecar authentication loop exhausted unexpectedly")


def _require_no_sqlite_sidecars(path: Path) -> None:
    if any(os.path.lexists(Path(f"{path}{suffix}")) for suffix in _SQLITE_SIDECAR_SUFFIXES):
        raise QueryDeliveryCorruption(
            "new query delivery database has pre-existing SQLite sidecars"
        )


def _cleanup_failed_initialization(
    path: Path,
    expected: os.stat_result,
    *,
    cleanup_sidecars: bool,
) -> None:
    """Remove only the exact database inode created by this initialization."""

    with secure_directory(path.parent, create=False) as directory:
        directory_fd = directory._directory_fd
        if directory_fd is None:  # pragma: no cover - Windows only
            try:
                current = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            if not os.path.samestat(expected, current):
                raise QueryDeliveryCorruption(
                    "new query delivery database changed during failed initialization"
                )
            if cleanup_sidecars:
                for suffix in _SQLITE_SIDECAR_SUFFIXES:
                    candidate = Path(f"{path}{suffix}")
                    if os.path.lexists(candidate):
                        _require_private_file(candidate)
                        candidate.unlink()
            path.unlink()
            return

        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not os.path.samestat(expected, current):
            raise QueryDeliveryCorruption(
                "new query delivery database changed during failed initialization"
            )
        if cleanup_sidecars:
            for suffix in _SQLITE_SIDECAR_SUFFIXES:
                name = f"{path.name}{suffix}"
                try:
                    sidecar = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(sidecar.st_mode)
                    or sidecar.st_nlink != 1
                    or (os.name != "nt" and sidecar.st_uid != os.geteuid())
                ):
                    raise QueryDeliveryCorruption(
                        "failed query delivery initialization left an unsafe SQLite sidecar"
                    )
                os.unlink(name, dir_fd=directory_fd)
        os.unlink(path.name, dir_fd=directory_fd)


__all__ = [
    "QueryDeliveryController",
    "QueryDeliveryReport",
    "QueryDeliveryStatus",
    "QueryEmissionPermit",
    "SensitiveQueryInput",
]
