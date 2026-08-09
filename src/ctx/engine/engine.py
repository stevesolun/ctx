"""Coordinator for privacy-safe, durable CTX engine transitions."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from threading import Lock
from typing import Literal, NoReturn, Protocol, SupportsIndex, TypeAlias, cast
from weakref import WeakKeyDictionary

from ctx.engine.protocol import (
    INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    INSTALL_RECEIPT_SCHEMA_V3,
    MATERIAL_ACTION_PAYLOAD_SCHEMA_V3,
    EngineEvent,
    HostAction,
    ScopeRef,
    Transition,
)
from ctx.engine.installation import (
    HeldInstallConsentPolicyAuthority,
    InstallConsentPolicy,
    InstallExecutionBinding,
    InstallPlanDescriptor,
    InteractiveInstallDecisionGuard,
    InteractiveInstallDecisionReservation,
    activation_action_authorization_digest,
    install_action_authorization_digest,
    route_install_authorization,
)
from ctx.engine.planner import CapabilitySelection
from ctx.engine.planning_v3 import (
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
    LoadPlanningAuthority,
)
from ctx.engine.reducer import (
    INSTALLATION_REDUCER_VERSION,
    PLANNING_REDUCER_VERSION,
    PROMPT_CONTEXT_REDUCER_VERSION,
    reduce_replay_v1,
    reduce_replay_v2,
    reduce_replay_v3,
    reduce_replay_v4,
)
from ctx.engine.replay import (
    DEFAULT_REDUCER_VERSION,
    DefaultReplayInputFactory,
    PreflightReplayInput,
    ReplayError,
    ReplayInput,
)
from ctx.engine.state import CapabilityStateV3, CommittedPlanV3, EngineState, PendingEffect
from ctx.engine.store import (
    ActivationActionClaimGuard,
    ActivationActionClaimRequest,
    ActivationExecutionOutcomeConflict,
    ActivationExecutionOutcomeRecord,
    ActivationExecutionOutcomeRequest,
    ActivationExecutionOutcomeRequired,
    ActivationExecutionStatus,
    CommitResult,
    InstallActionClaimGuard,
    InstallActionClaimRequest,
    InstallExecutionOutcomeConflict,
    InstallExecutionOutcomeRecord,
    InstallExecutionOutcomeRequest,
    InstallExecutionOutcomeRequired,
    InstallExecutionStatus,
    JournalRecord,
    RevisionConflict,
    StoredHead,
    StreamId,
)


ReducerFn: TypeAlias = Callable[
    [EngineState | None, ReplayInput],
    tuple[EngineState, Transition],
]
_SNAPSHOT_ATTEMPTS = 3


def _system_utc_now() -> datetime:
    return datetime.now(UTC)


class _ReplayFactory(Protocol):
    def preflight(self, event: EngineEvent) -> PreflightReplayInput: ...

    def prepare(
        self,
        preflight: PreflightReplayInput,
        state: EngineState | None,
        *,
        decision_surrogate: None = None,
    ) -> ReplayInput: ...


class _JournalStore(Protocol):
    def load_head(self, stream_id: StreamId) -> StoredHead: ...

    def cached_transition(
        self,
        stream_id: StreamId,
        event_id: str,
        event_content_digest: str,
    ) -> Transition | None: ...

    def records(
        self,
        stream_id: StreamId,
        *,
        after_revision: int = 0,
    ) -> Iterator[JournalRecord]: ...

    def repair_projection(
        self,
        stream_id: StreamId,
        *,
        at_revision: int,
        state_json: str,
        record_digest: str,
    ) -> bool: ...

    def commit(
        self,
        *,
        expected_revision: int,
        record: JournalRecord,
    ) -> CommitResult: ...


class _InstallClaimStore(Protocol):
    """Additive managing-mode store surface; legacy stores remain readable."""

    def claim_install(
        self,
        request: InstallActionClaimRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> object: ...

    def record_install_outcome(
        self,
        request: InstallExecutionOutcomeRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> InstallExecutionOutcomeRecord: ...

    def install_execution_status(
        self,
        stream_id: StreamId,
        action_id: str,
    ) -> InstallExecutionStatus: ...

    def commit(
        self,
        *,
        expected_revision: int,
        record: JournalRecord,
        install_claim_guard: InstallActionClaimGuard | None = None,
    ) -> CommitResult: ...


class _ActivationClaimStore(Protocol):
    def claim_activation(
        self,
        request: ActivationActionClaimRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> object: ...

    def record_activation_outcome(
        self,
        request: ActivationExecutionOutcomeRequest,
        *,
        trusted_utc_now: Callable[[], datetime],
    ) -> ActivationExecutionOutcomeRecord: ...

    def activation_execution_status(
        self,
        stream_id: StreamId,
        action_id: str,
    ) -> ActivationExecutionStatus: ...

    def commit(
        self,
        *,
        expected_revision: int,
        record: JournalRecord,
        activation_claim_guard: ActivationActionClaimGuard | None = None,
    ) -> CommitResult: ...


class CtxEngineError(RuntimeError):
    """Base class for coordinator-owned failures."""


class UnsupportedReducerVersionError(CtxEngineError):
    """A journal or prepared replay names no exact registered reducer."""

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__("no exact reducer is registered for the requested version")


class ReplayDivergenceError(CtxEngineError):
    """Stored replay and deterministic reducer output disagree."""

    def __init__(self, *, stream_id: StreamId, revision: int, component: str) -> None:
        self.stream_id = stream_id
        self.revision = revision
        self.component = component
        super().__init__(f"deterministic replay diverged at revision {revision}: {component}")


class SnapshotContentionError(CtxEngineError):
    """A stable journal cursor was not observed within the bounded retry limit."""

    def __init__(self, stream_id: StreamId) -> None:
        self.stream_id = stream_id
        super().__init__("journal head kept changing while materializing the snapshot")


@dataclass(frozen=True, slots=True, kw_only=True)
class EngineSnapshot:
    stream_id: StreamId
    revision: int
    state: EngineState | None
    record_digest: str | None
    projection_repaired: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, StreamId):
            raise TypeError("stream_id must be a StreamId")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a non-negative integer")
        if self.revision == 0:
            if self.state is not None or self.record_digest is not None:
                raise ValueError("empty snapshots cannot contain state or a record digest")
        elif (
            not isinstance(self.state, EngineState)
            or self.state.revision != self.revision
            or self.record_digest is None
        ):
            raise ValueError("non-empty snapshot state must match its revision and record")


class _InstallOutcomePermit:
    """Process-bound one-use authority held only by the install coordinator."""

    __slots__ = (
        "_action_content_digest",
        "_binding_digest",
        "_consumed",
        "_engine_identity",
        "_lock",
        "_pid",
    )

    def __init__(
        self,
        *,
        engine_identity: int,
        action_content_digest: str,
        binding_digest: str,
    ) -> None:
        self._engine_identity = engine_identity
        self._action_content_digest = action_content_digest
        self._binding_digest = binding_digest
        self._pid = os.getpid()
        self._consumed = False
        self._lock = Lock()

    def _consume(
        self,
        *,
        engine_identity: int,
        action_content_digest: str,
        binding_digest: str,
    ) -> None:
        with self._lock:
            if os.getpid() != self._pid:
                raise CtxEngineError("install outcome authority cannot cross a process boundary")
            if self._consumed:
                raise CtxEngineError("install outcome authority has already been consumed")
            self._consumed = True
            if (
                self._engine_identity != engine_identity
                or self._action_content_digest != action_content_digest
                or self._binding_digest != binding_digest
            ):
                raise CtxEngineError("install outcome authority does not match this execution")

    def __copy__(self) -> object:
        raise TypeError("install outcome authority cannot be copied")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("install outcome authority cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("install outcome authority cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("install outcome authority cannot be serialized")

    def __repr__(self) -> str:
        return "<install-outcome-authority>"


_PROMPT_CONTEXT_MATERIAL_PERMIT_TOKEN = object()
_PROMPT_CONTEXT_MATERIAL_PERMITS: WeakKeyDictionary[object, tuple[object, ...]] = (
    WeakKeyDictionary()
)
_PROMPT_CONTEXT_MATERIAL_ROUTES: WeakKeyDictionary[object, tuple[object, ...]] = WeakKeyDictionary()
_PROMPT_CONTEXT_MATERIAL_PERMITS_LOCK = Lock()


class _PromptContextMaterialRoutePermit:
    """One-use authority for one externally routed selection in a bundle."""

    __slots__ = ("__weakref__",)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("prompt context material routes are engine-issued only")

    @classmethod
    def _create(
        cls,
        *,
        action_json: str,
        selection: CapabilityPlanSelectionV3,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        expected_catalog_snapshot_digest: str,
        token: object,
    ) -> _PromptContextMaterialRoutePermit:
        if token is not _PROMPT_CONTEXT_MATERIAL_PERMIT_TOKEN:
            raise TypeError("prompt context material routes are engine-issued only")
        instance = object.__new__(cls)
        with _PROMPT_CONTEXT_MATERIAL_PERMITS_LOCK:
            _PROMPT_CONTEXT_MATERIAL_ROUTES[instance] = (
                os.getpid(),
                True,
                action_json,
                selection,
                selections,
                expected_catalog_snapshot_digest,
            )
        return instance

    def _consume(
        self,
        *,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        expected_catalog_snapshot_digest: str,
    ) -> None:
        with _PROMPT_CONTEXT_MATERIAL_PERMITS_LOCK:
            record = _PROMPT_CONTEXT_MATERIAL_ROUTES.get(self)
            if record is None:
                raise CtxEngineError("prompt context material route was not engine-issued")
            pid, available, action_json, expected_selection, expected_selections, catalog = record
            _PROMPT_CONTEXT_MATERIAL_ROUTES[self] = (
                pid,
                False,
                action_json,
                expected_selection,
                expected_selections,
                catalog,
            )
        if os.getpid() != pid:
            raise CtxEngineError("prompt context material route cannot cross a process boundary")
        if available is not True:
            raise CtxEngineError("prompt context material route was already consumed")
        if (
            action.to_json() != action_json
            or selection != expected_selection
            or selections != expected_selections
            or expected_catalog_snapshot_digest != catalog
        ):
            raise CtxEngineError("prompt context material route was substituted")

    def __copy__(self) -> object:
        raise TypeError("prompt context material route cannot be copied")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("prompt context material route cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("prompt context material route cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("prompt context material route cannot be serialized")

    def __repr__(self) -> str:
        return "<prompt-context-material-route>"


class _PromptContextMaterialPermit:
    """Process-bound one-use authority for one exact prompt-context bundle."""

    __slots__ = ("__weakref__",)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("prompt context material permits are engine-issued only")

    @classmethod
    def _create(
        cls,
        *,
        action_json: str,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        expected_catalog_snapshot_digest: str,
        token: object,
    ) -> _PromptContextMaterialPermit:
        if token is not _PROMPT_CONTEXT_MATERIAL_PERMIT_TOKEN:
            raise TypeError("prompt context material permits are engine-issued only")
        instance = object.__new__(cls)
        with _PROMPT_CONTEXT_MATERIAL_PERMITS_LOCK:
            _PROMPT_CONTEXT_MATERIAL_PERMITS[instance] = (
                os.getpid(),
                True,
                action_json,
                selections,
                expected_catalog_snapshot_digest,
            )
        return instance

    def _consume_and_issue_routes(
        self,
        *,
        action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        expected_catalog_snapshot_digest: str,
        external_capability_ids: frozenset[str],
    ) -> dict[str, _PromptContextMaterialRoutePermit]:
        if not isinstance(external_capability_ids, frozenset) or not all(
            isinstance(value, str) for value in external_capability_ids
        ):
            raise TypeError("external capability ids must be an exact frozenset of strings")
        with _PROMPT_CONTEXT_MATERIAL_PERMITS_LOCK:
            record = _PROMPT_CONTEXT_MATERIAL_PERMITS.get(self)
            if record is None:
                raise CtxEngineError("prompt context material authority was not engine-issued")
            pid, available, action_json, expected_selections, catalog = record
            _PROMPT_CONTEXT_MATERIAL_PERMITS[self] = (
                pid,
                False,
                action_json,
                expected_selections,
                catalog,
            )
        if os.getpid() != pid:
            raise CtxEngineError(
                "prompt context material authority cannot cross a process boundary"
            )
        if available is not True:
            raise CtxEngineError("prompt context material authority was already consumed")
        if (
            action.to_json() != action_json
            or selections != expected_selections
            or expected_catalog_snapshot_digest != catalog
        ):
            raise CtxEngineError("prompt context material authority was substituted")
        selected_by_id = {
            selection.presentation.capability_id: selection for selection in selections
        }
        if len(selected_by_id) != len(selections) or not external_capability_ids.issubset(
            selected_by_id
        ):
            raise CtxEngineError("external material routes changed the authorized bundle")
        return {
            capability_id: _PromptContextMaterialRoutePermit._create(
                action_json=action_json,
                selection=selected_by_id[capability_id],
                selections=selections,
                expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
                token=_PROMPT_CONTEXT_MATERIAL_PERMIT_TOKEN,
            )
            for capability_id in external_capability_ids
        }

    def __copy__(self) -> object:
        raise TypeError("prompt context material authority cannot be copied")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("prompt context material authority cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("prompt context material authority cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("prompt context material authority cannot be serialized")

    def __repr__(self) -> str:
        return "<prompt-context-material-authority>"


_PROMPT_CONTEXT_PERMIT_TOKEN = object()
_PROMPT_CONTEXT_PERMITS: WeakKeyDictionary[object, tuple[object, ...]] = WeakKeyDictionary()
_PROMPT_CONTEXT_PERMITS_LOCK = Lock()


class _PromptContextReceiptPermit:
    """Process-bound one-use proof of an exact revision-three receipt."""

    __slots__ = ("__weakref__",)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("prompt context receipt permits are engine-issued only")

    @classmethod
    def _create(
        cls,
        *,
        action_id: str,
        action_content_digest: str,
        receipt_event_content_digest: str,
        issuing_record_digest: str,
        final_record_digest: str,
        expires_at: str,
        token: object,
    ) -> _PromptContextReceiptPermit:
        if token is not _PROMPT_CONTEXT_PERMIT_TOKEN:
            raise TypeError("prompt context receipt permits are engine-issued only")
        instance = object.__new__(cls)
        with _PROMPT_CONTEXT_PERMITS_LOCK:
            _PROMPT_CONTEXT_PERMITS[instance] = (
                os.getpid(),
                True,
                action_id,
                action_content_digest,
                receipt_event_content_digest,
                issuing_record_digest,
                final_record_digest,
                expires_at,
            )
        return instance

    def _consume(
        self,
        *,
        action_id: str,
        action_content_digest: str,
        receipt_event_content_digest: str,
        issuing_record_digest: str,
        expires_at: str,
    ) -> tuple[int, str]:
        with _PROMPT_CONTEXT_PERMITS_LOCK:
            record = _PROMPT_CONTEXT_PERMITS.get(self)
            if record is None:
                raise CtxEngineError("prompt context receipt authority was not engine-issued")
            (
                pid,
                available,
                expected_action_id,
                expected_action_digest,
                expected_receipt_digest,
                expected_issuing_digest,
                final_record_digest,
                expected_expires_at,
            ) = record
            if os.getpid() != pid:
                raise CtxEngineError(
                    "prompt context receipt authority cannot cross a process boundary"
                )
            if available is not True:
                raise CtxEngineError("prompt context receipt authority was already consumed")
            if (
                expected_action_id != action_id
                or expected_action_digest != action_content_digest
                or expected_receipt_digest != receipt_event_content_digest
                or expected_issuing_digest != issuing_record_digest
                or expected_expires_at != expires_at
                or not isinstance(final_record_digest, str)
            ):
                raise CtxEngineError("prompt context receipt authority was substituted")
            _PROMPT_CONTEXT_PERMITS[self] = (
                pid,
                False,
                expected_action_id,
                expected_action_digest,
                expected_receipt_digest,
                expected_issuing_digest,
                final_record_digest,
                expected_expires_at,
            )
            return 3, final_record_digest

    def __copy__(self) -> object:
        raise TypeError("prompt context receipt authority cannot be copied")

    def __deepcopy__(self, _memo: object) -> object:
        raise TypeError("prompt context receipt authority cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("prompt context receipt authority cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("prompt context receipt authority cannot be serialized")

    def __repr__(self) -> str:
        return "<prompt-context-receipt-authority>"


class ReducerRegistry:
    """Immutable exact-version lookup for deterministic historical reducers."""

    def __init__(self, versions: Mapping[str, ReducerFn]) -> None:
        copied = dict(versions)
        if not copied:
            raise ValueError("at least one reducer version is required")
        for version, reducer_fn in copied.items():
            if not isinstance(version, str) or not version or version != version.strip():
                raise ValueError("reducer versions must be non-empty trimmed strings")
            if not callable(reducer_fn):
                raise TypeError("registered reducers must be callable")
        self._versions: Mapping[str, ReducerFn] = MappingProxyType(copied)

    @classmethod
    def default(cls) -> ReducerRegistry:
        return cls(
            {
                DEFAULT_REDUCER_VERSION: reduce_replay_v1,
                PLANNING_REDUCER_VERSION: reduce_replay_v2,
                INSTALLATION_REDUCER_VERSION: reduce_replay_v3,
                PROMPT_CONTEXT_REDUCER_VERSION: reduce_replay_v4,
            }
        )

    @property
    def versions(self) -> Mapping[str, ReducerFn]:
        return self._versions

    def resolve(self, exact_version: str) -> ReducerFn:
        reducer_fn = self._versions.get(exact_version)
        if reducer_fn is None:
            raise UnsupportedReducerVersionError(exact_version)
        return reducer_fn


class CtxEngine:
    """Deep coordinator: validate, replay, reduce, commit, then return actions."""

    def __init__(
        self,
        *,
        store: _JournalStore,
        replay_factory: _ReplayFactory | None = None,
        reducers: ReducerRegistry | None = None,
        install_policy_guard: (
            Callable[
                [str],
                AbstractContextManager[HeldInstallConsentPolicyAuthority],
            ]
            | None
        ) = None,
        interactive_install_decision_guard: InteractiveInstallDecisionGuard | None = None,
        install_descriptor_loader: (
            Callable[[str, str], InstallPlanDescriptor | None] | None
        ) = None,
        trusted_utc_now: Callable[[], datetime] | None = None,
    ) -> None:
        if interactive_install_decision_guard is not None and not callable(
            interactive_install_decision_guard
        ):
            raise TypeError("interactive_install_decision_guard must be callable or None")
        if trusted_utc_now is not None and not callable(trusted_utc_now):
            raise TypeError("trusted_utc_now must be callable or None")
        self._store = store
        self._replay_factory = replay_factory or DefaultReplayInputFactory()
        self._reducers = reducers or ReducerRegistry.default()
        self._install_policy_guard = install_policy_guard
        self._interactive_install_decision_guard = interactive_install_decision_guard
        self._install_descriptor_loader = install_descriptor_loader
        self._trusted_utc_now = trusted_utc_now or _system_utc_now

    def process(self, event: EngineEvent) -> Transition:
        return self._process(
            event,
            supplied_install_guard=None,
            supplied_activation_guard=None,
        )

    def process_install_receipt(
        self,
        event: EngineEvent,
        guard: InstallActionClaimGuard,
    ) -> Transition:
        """Commit a driver-managed receipt backed by one verified outcome."""

        if not isinstance(guard, InstallActionClaimGuard):
            raise TypeError("guard must be an InstallActionClaimGuard")
        if guard.mode == "expired" or guard.execution_outcome_digest is None:
            raise CtxEngineError("install receipt requires a verified execution outcome")
        self._assert_install_guard_matches_event(event, guard)
        return self._process(
            event,
            supplied_install_guard=guard,
            supplied_activation_guard=None,
        )

    def process_activation_receipt(
        self,
        event: EngineEvent,
        guard: ActivationActionClaimGuard,
    ) -> Transition:
        """Commit an activation receipt backed by one verified host outcome."""

        if not isinstance(guard, ActivationActionClaimGuard):
            raise TypeError("guard must be an ActivationActionClaimGuard")
        if guard.mode != "applied" or guard.execution_outcome_digest is None:
            raise CtxEngineError("activation receipt requires a verified execution outcome")
        self._assert_activation_guard_matches_event(event, guard)
        return self._process(
            event,
            supplied_install_guard=None,
            supplied_activation_guard=guard,
        )

    def _process(
        self,
        event: EngineEvent,
        *,
        supplied_install_guard: InstallActionClaimGuard | None,
        supplied_activation_guard: ActivationActionClaimGuard | None,
    ) -> Transition:
        preflight = self._replay_factory.preflight(event)
        stream_id = StreamId.from_scope(event.scope)
        cached = self._cached(event, stream_id)
        if cached is not None:
            return cached

        head = self._store.load_head(stream_id)
        if head.revision != event.expected_revision:
            cached = self._cached(event, stream_id)
            if cached is not None:
                return cached
            raise RevisionConflict(expected=event.expected_revision, actual=head.revision)

        snapshot = self._snapshot(stream_id)
        if snapshot.revision != event.expected_revision:
            cached = self._cached(event, stream_id)
            if cached is not None:
                return cached
            raise RevisionConflict(expected=event.expected_revision, actual=snapshot.revision)

        install_action = self._exact_pending_install_decision(event, snapshot.state)
        expiring_install_action = self._exact_pending_install_expiry(
            event,
            snapshot.state,
        )
        expiring_unclaimed_install_action = self._exact_unclaimed_install_action_expiry(
            event,
            snapshot.state,
        )
        derived_install_guard = self._install_receipt_claim_guard(event, snapshot.state)
        install_claim_guard = self._resolve_install_receipt_guard(
            derived=derived_install_guard,
            supplied=supplied_install_guard,
        )
        activation_claim_guard = self._resolve_activation_receipt_guard(
            event=event,
            derived=self._activation_receipt_claim_guard(event, snapshot.state),
            supplied=supplied_activation_guard,
        )
        expiring_unclaimed_activation_action = self._exact_unclaimed_activation_action_expiry(
            event,
            snapshot.state,
        )
        if install_action is not None:
            self._assert_install_decision_not_expired(install_action)
        if expiring_install_action is not None:
            self._assert_install_consent_has_expired(expiring_install_action)
        if expiring_unclaimed_install_action is not None:
            self._assert_install_action_has_expired(expiring_unclaimed_install_action)
        if expiring_unclaimed_activation_action is not None:
            self._assert_activation_action_has_expired(expiring_unclaimed_activation_action)

        with self._interactive_install_decision_context(event, install_action):
            with self._current_install_policy_context(event) as current_policy_authority:
                self._assert_preapproved_install_decision(
                    event,
                    snapshot.state,
                    current_policy_authority.policy
                    if current_policy_authority is not None
                    else None,
                )
                return self._prepare_reduce_commit(
                    event=event,
                    stream_id=stream_id,
                    preflight=preflight,
                    snapshot=snapshot,
                    current_policy_authority=current_policy_authority,
                    install_action=install_action,
                    expiring_install_action=expiring_install_action,
                    expiring_unclaimed_install_action=expiring_unclaimed_install_action,
                    expiring_unclaimed_activation_action=(expiring_unclaimed_activation_action),
                    install_claim_guard=install_claim_guard,
                    activation_claim_guard=activation_claim_guard,
                )

    def _prepare_reduce_commit(
        self,
        *,
        event: EngineEvent,
        stream_id: StreamId,
        preflight: PreflightReplayInput,
        snapshot: EngineSnapshot,
        current_policy_authority: HeldInstallConsentPolicyAuthority | None,
        install_action: HostAction | None,
        expiring_install_action: HostAction | None,
        expiring_unclaimed_install_action: HostAction | None,
        expiring_unclaimed_activation_action: HostAction | None,
        install_claim_guard: InstallActionClaimGuard | None,
        activation_claim_guard: ActivationActionClaimGuard | None,
    ) -> Transition:
        """Prepare, reduce, and commit while external authorities remain held."""

        replay = self._replay_factory.prepare(
            preflight,
            snapshot.state,
            decision_surrogate=None,
        )
        self._assert_prepared_binding(event, stream_id, preflight, replay)
        reducer_fn = self._reducers.resolve(replay.reducer_version)
        next_state, transition = reducer_fn(snapshot.state, replay)
        state_json, transition_json = self._validate_reduction(
            stream_id=stream_id,
            event_id=event.event_id,
            expected_revision=event.expected_revision,
            state=next_state,
            transition=transition,
        )
        record = JournalRecord(
            stream_id=stream_id,
            revision=next_state.revision,
            event_id=event.event_id,
            event_content_digest=event.content_digest,
            replay_json=replay.to_json(),
            transition_json=transition_json,
            result_state_json=state_json,
            privacy_classification=replay.reducer_event.privacy.classification,
            retention_class=replay.reducer_event.privacy.retention,
            reducer_version=replay.reducer_version,
        )
        try:
            replay.assert_record_binding(record)
        except ReplayError:
            self._diverged(stream_id, record.revision, "record-binding")
        if current_policy_authority is not None:
            try:
                current_policy_authority.assert_current()
            except Exception:
                raise CtxEngineError(
                    "preapproved install policy is no longer authoritative"
                ) from None
        if install_action is not None:
            self._assert_install_decision_not_expired(install_action)
        if expiring_install_action is not None:
            self._assert_install_consent_has_expired(expiring_install_action)
        if expiring_unclaimed_install_action is not None:
            self._assert_install_action_has_expired(expiring_unclaimed_install_action)
        if expiring_unclaimed_activation_action is not None:
            self._assert_activation_action_has_expired(expiring_unclaimed_activation_action)
        if install_claim_guard is None and activation_claim_guard is None:
            committed = self._store.commit(
                expected_revision=event.expected_revision,
                record=record,
            )
        elif install_claim_guard is not None:
            committed = self._install_claim_store().commit(
                expected_revision=event.expected_revision,
                record=record,
                install_claim_guard=install_claim_guard,
            )
        else:
            assert activation_claim_guard is not None
            committed = self._activation_claim_store().commit(
                expected_revision=event.expected_revision,
                record=record,
                activation_claim_guard=activation_claim_guard,
            )
        self._assert_commit_result(
            stream_id=stream_id,
            computed=record,
            transition=transition,
            committed=committed,
        )
        return committed.transition

    def _current_install_policy_context(
        self,
        event: EngineEvent,
    ) -> AbstractContextManager[HeldInstallConsentPolicyAuthority | None]:
        if (
            event.kind != "UserDecision"
            or event.payload.get("decision") != "granted"
            or event.payload.get("decision_basis") != "preapproved-policy"
        ):
            return nullcontext(None)
        expected_digest = event.payload.get("policy_snapshot_digest")
        if not isinstance(expected_digest, str) or self._install_policy_guard is None:
            raise CtxEngineError(
                "preapproved install requires an authoritative current-policy guard"
            )
        try:
            manager = self._install_policy_guard(expected_digest)
        except Exception:
            raise CtxEngineError("preapproved install policy guard is unavailable") from None
        return self._entered_install_policy_context(manager)

    @staticmethod
    @contextmanager
    def _entered_install_policy_context(
        manager: AbstractContextManager[HeldInstallConsentPolicyAuthority],
    ) -> Iterator[HeldInstallConsentPolicyAuthority]:
        """Normalize guard-entry failure without swallowing engine-body errors."""

        try:
            authority = manager.__enter__()
        except Exception:
            raise CtxEngineError("preapproved install policy guard is unavailable") from None
        try:
            valid_authority = isinstance(
                authority, HeldInstallConsentPolicyAuthority
            ) and isinstance(authority.policy, InstallConsentPolicy)
        except Exception:
            valid_authority = False
        if not valid_authority:
            error = TypeError("policy guard returned no held authority")
            try:
                manager.__exit__(TypeError, error, error.__traceback__)
            finally:
                raise CtxEngineError("preapproved install policy guard is unavailable") from None
        try:
            yield authority
        except BaseException as error:
            manager.__exit__(type(error), error, error.__traceback__)
            raise
        else:
            manager.__exit__(None, None, None)

    def _exact_pending_install_decision(
        self,
        event: EngineEvent,
        state: EngineState | None,
    ) -> HostAction | None:
        if event.kind != "UserDecision" or event.payload.get("decision_basis") not in {
            "interactive",
            "preapproved-policy",
        }:
            return None
        if (
            event.payload.get("decision_basis") == "preapproved-policy"
            and event.payload.get("decision") != "granted"
        ):
            raise CtxEngineError("preapproved install policy decisions may only grant")
        consent_id = event.payload.get("consent_id")
        pending = (
            next(
                (item for item in state.pending_consents if item.consent_id == consent_id),
                None,
            )
            if state is not None
            else None
        )
        if pending is None:
            raise CtxEngineError("install decision does not match an exact pending consent")
        action = pending.install_action
        expected_identity = {
            "requested_action_id": action.action_id,
            "requested_action_kind": action.kind,
            "requested_action_content_digest": action.content_digest,
            "requested_action_precondition_revision": action.precondition_revision,
            "policy_snapshot_digest": action.payload.get("policy_snapshot_digest"),
        }
        if event.scope != action.scope or any(
            event.payload.get(key) != value for key, value in expected_identity.items()
        ):
            raise CtxEngineError("install decision does not match an exact pending consent")
        return action

    def _assert_install_decision_not_expired(self, action: HostAction) -> None:
        try:
            now = self._trusted_utc_now()
        except Exception:
            raise CtxEngineError("trusted install decision clock is unavailable") from None
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise CtxEngineError("trusted install decision clock is unavailable")
        try:
            expires_at = datetime.fromisoformat((action.expires_at or "").replace("Z", "+00:00"))
        except ValueError:
            raise CtxEngineError("pending install has no valid expiry") from None
        if now.astimezone(UTC) >= expires_at.astimezone(UTC):
            raise CtxEngineError("install decision has expired according to trusted clock")

    def _exact_pending_install_expiry(
        self,
        event: EngineEvent,
        state: EngineState | None,
    ) -> HostAction | None:
        if event.kind != "InstallConsentExpired":
            return None
        consent_id = event.payload.get("consent_id")
        pending = (
            next(
                (item for item in state.pending_consents if item.consent_id == consent_id),
                None,
            )
            if state is not None
            else None
        )
        if pending is None:
            raise CtxEngineError("install consent expiry does not match an exact pending consent")
        action = pending.install_action
        expected_identity = {
            "policy_snapshot_digest": action.payload.get("policy_snapshot_digest"),
            "requested_action_id": action.action_id,
            "requested_action_kind": action.kind,
            "requested_action_content_digest": action.content_digest,
            "requested_action_precondition_revision": action.precondition_revision,
            "install_expires_at": action.expires_at,
        }
        if event.scope != action.scope or any(
            event.payload.get(key) != value for key, value in expected_identity.items()
        ):
            raise CtxEngineError("install consent expiry does not match an exact pending consent")
        return action

    def _assert_install_consent_has_expired(self, action: HostAction) -> None:
        try:
            now = self._trusted_utc_now()
        except Exception:
            raise CtxEngineError("trusted install consent clock is unavailable") from None
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise CtxEngineError("trusted install consent clock is unavailable")
        try:
            expires_at = datetime.fromisoformat((action.expires_at or "").replace("Z", "+00:00"))
        except ValueError:
            raise CtxEngineError("pending install consent has no valid expiry") from None
        if now.astimezone(UTC) < expires_at.astimezone(UTC):
            raise CtxEngineError("install consent has not expired according to trusted clock")

    @staticmethod
    def _exact_unclaimed_install_action_expiry(
        event: EngineEvent,
        state: EngineState | None,
    ) -> HostAction | None:
        """Bind ActionExpired to one exact pending schema-v3 install action."""

        if event.kind != "ActionExpired" or state is None:
            return None
        action_id = event.payload.get("action_id")
        pending = next(
            (
                item
                for item in state.pending_effects
                if item.effect == "install" and item.action.action_id == action_id
            ),
            None,
        )
        if pending is None:
            return None
        action = pending.action
        if (
            action.kind != "InstallCapability"
            or action.payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            return None
        expected_identity = {
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "reason": "expired",
        }
        if event.scope != action.scope or dict(event.payload) != expected_identity:
            raise CtxEngineError("install action expiry does not match exact pending authority")
        return action

    @staticmethod
    def _exact_unclaimed_activation_action_expiry(
        event: EngineEvent,
        state: EngineState | None,
    ) -> HostAction | None:
        """Bind ActionExpired to one exact pending schema-v3 activation action."""

        if event.kind != "ActionExpired" or state is None:
            return None
        action_id = event.payload.get("action_id")
        pending = next(
            (
                item
                for item in state.pending_effects
                if item.effect == "activate" and item.action.action_id == action_id
            ),
            None,
        )
        if pending is None:
            return None
        action = pending.action
        if (
            action.kind != "ActivateCapability"
            or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            return None
        expected_identity = {
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "reason": "expired",
        }
        if event.scope != action.scope or dict(event.payload) != expected_identity:
            raise CtxEngineError("activation action expiry does not match exact pending authority")
        return action

    def _assert_activation_action_has_expired(self, action: HostAction) -> None:
        try:
            now = self._trusted_utc_now()
        except Exception:
            raise CtxEngineError("trusted activation action clock is unavailable") from None
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise CtxEngineError("trusted activation action clock is unavailable")
        try:
            expires_at = datetime.fromisoformat((action.expires_at or "").replace("Z", "+00:00"))
        except ValueError:
            raise CtxEngineError("pending activation action has no valid expiry") from None
        if now.astimezone(UTC) < expires_at.astimezone(UTC):
            raise CtxEngineError("activation action has not expired according to trusted clock")

    def _assert_install_action_has_expired(self, action: HostAction) -> None:
        try:
            now = self._trusted_utc_now()
        except Exception:
            raise CtxEngineError("trusted install action clock is unavailable") from None
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise CtxEngineError("trusted install action clock is unavailable")
        try:
            expires_at = datetime.fromisoformat((action.expires_at or "").replace("Z", "+00:00"))
        except ValueError:
            raise CtxEngineError("pending install action has no valid expiry") from None
        if now.astimezone(UTC) < expires_at.astimezone(UTC):
            raise CtxEngineError("install action has not expired according to trusted clock")

    def _interactive_install_decision_context(
        self,
        event: EngineEvent,
        action: HostAction | None,
    ) -> AbstractContextManager[None]:
        if event.kind != "UserDecision" or event.payload.get("decision_basis") != "interactive":
            return nullcontext()
        if action is None or action.expires_at is None:
            raise CtxEngineError("interactive install decision has no exact pending action")
        guard = self._interactive_install_decision_guard
        if guard is None:
            raise CtxEngineError(
                "interactive install decision requires a host-authenticated one-shot guard"
            )
        reservation = InteractiveInstallDecisionReservation(
            scope=event.scope,
            event_id=event.event_id,
            event_content_digest=event.content_digest,
            consent_id=event.payload["consent_id"],
            decision=event.payload["decision"],
            policy_snapshot_digest=event.payload["policy_snapshot_digest"],
            requested_action_id=event.payload["requested_action_id"],
            requested_action_kind=event.payload["requested_action_kind"],
            requested_action_content_digest=event.payload["requested_action_content_digest"],
            requested_action_precondition_revision=event.payload[
                "requested_action_precondition_revision"
            ],
            install_expires_at=action.expires_at,
        )
        try:
            manager = guard(reservation)
        except Exception:
            raise CtxEngineError("interactive install decision guard is unavailable") from None
        return self._entered_interactive_install_decision_context(manager)

    @staticmethod
    @contextmanager
    def _entered_interactive_install_decision_context(
        manager: AbstractContextManager[None],
    ) -> Iterator[None]:
        """Hold an external one-shot reservation across the journal commit."""

        try:
            manager.__enter__()
        except Exception:
            raise CtxEngineError("interactive install decision guard is unavailable") from None
        try:
            yield
        except BaseException as error:
            try:
                manager.__exit__(type(error), error, error.__traceback__)
            except Exception:
                raise CtxEngineError("interactive install decision guard cleanup failed") from error
            raise
        else:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                raise CtxEngineError(
                    "interactive install decision guard settlement failed"
                ) from None

    def _assert_preapproved_install_decision(
        self,
        event: EngineEvent,
        state: EngineState | None,
        current_policy: InstallConsentPolicy | None,
    ) -> None:
        if (
            event.kind != "UserDecision"
            or event.payload.get("decision") != "granted"
            or event.payload.get("decision_basis") != "preapproved-policy"
        ):
            return
        if (
            state is None
            or not isinstance(current_policy, InstallConsentPolicy)
            or self._install_descriptor_loader is None
        ):
            raise CtxEngineError(
                "preapproved install requires authoritative policy and descriptor data"
            )
        consent_id = event.payload.get("consent_id")
        pending = next(
            (item for item in state.pending_consents if item.consent_id == consent_id),
            None,
        )
        if pending is None:
            raise CtxEngineError("preapproved install does not match a pending consent")
        action = pending.install_action
        capability_id = action.entity_id
        if capability_id is None:
            raise CtxEngineError("preapproved install does not identify a capability")
        capability = state.capability(capability_id)
        if capability is None:
            raise CtxEngineError("preapproved install references an unknown capability")
        try:
            descriptor = self._install_descriptor_loader(
                capability.capability_id,
                capability.kind,
            )
        except Exception:
            raise CtxEngineError("preapproved install authority lookup failed") from None
        if not isinstance(descriptor, InstallPlanDescriptor):
            raise CtxEngineError("preapproved install authority is unavailable")
        decision = route_install_authorization(current_policy, descriptor)
        common_authorized = (
            decision.is_preapproved
            and decision.policy_snapshot_digest == event.payload.get("policy_snapshot_digest")
            and action.payload.get("policy_snapshot_digest") == current_policy.policy_digest
            and descriptor.capability_id == capability.capability_id
            and descriptor.kind == capability.kind
        )
        if isinstance(capability, CapabilityStateV3):
            authority = capability.selection.authority
            authorized = bool(
                common_authorized
                and isinstance(authority, InstallPlanningAuthority)
                and descriptor == authority.descriptor
                and descriptor.matches_result_material(capability.material_identity)
                and action.payload.get("catalog_identity") == capability.catalog_identity.to_dict()
                and action.payload.get("result_material") == capability.material_identity.to_dict()
                and action.payload.get("install_plan_descriptor") == descriptor.to_dict()
            )
        else:
            authorized = bool(
                common_authorized
                and action.payload.get("install_descriptor_digest") == descriptor.descriptor_digest
                and action.payload.get("install_plan_digest") == descriptor.plan_digest
                and descriptor.descriptor_digest == capability.install_descriptor_digest
                and descriptor.plan_digest == capability.install_plan_digest
            )
        if not authorized:
            raise CtxEngineError("preapproved install is not authorized by current policy")

    def snapshot(self, scope: ScopeRef) -> EngineSnapshot:
        return self._snapshot(StreamId.from_scope(scope))

    def authorize_install(
        self,
        action: HostAction,
        selection: CapabilitySelection | CapabilityPlanSelectionV3,
        descriptor: InstallPlanDescriptor,
        *,
        expected_catalog_snapshot_digest: str,
        expected_policy_digest: str,
        execution_binding: InstallExecutionBinding,
    ) -> None:
        """Atomically consume authority for one exact pending schema-v3 install.

        No executable plan or bearer token crosses this boundary.  A successful
        return means only that the journal-backed one-use claim was durably
        burned; a second or concurrent call is rejected by the store.
        """

        if not isinstance(action, HostAction) or not isinstance(
            selection,
            (CapabilitySelection, CapabilityPlanSelectionV3),
        ):
            raise TypeError("install authorization requires typed action and selection values")
        if not isinstance(descriptor, InstallPlanDescriptor):
            raise TypeError("install authorization requires a typed plan descriptor")
        if not isinstance(execution_binding, InstallExecutionBinding):
            raise TypeError("install authorization requires a typed execution binding")
        if not isinstance(selection, CapabilityPlanSelectionV3):
            raise CtxEngineError("legacy selections cannot authorize physical installation")
        if not self._is_sha256(expected_catalog_snapshot_digest) or not self._is_sha256(
            expected_policy_digest
        ):
            raise CtxEngineError("install authorization requires exact snapshot digests")

        presentation = selection.presentation
        authority = selection.authority
        if (
            presentation.kind not in {"skill", "agent", "mcp-server"}
            or presentation.actionability != "install"
            or not isinstance(authority, InstallPlanningAuthority)
            or descriptor.schema_version != 2
            or descriptor != authority.descriptor
            or not descriptor.matches_result_material(authority.result_material)
            or execution_binding.driver_id != descriptor.installer_id
            or execution_binding.driver_digest != action.payload.get("installer_digest")
        ):
            raise CtxEngineError("selection has no schema-v3 physical install authority")

        stream_id = StreamId.from_scope(action.scope)
        snapshot = self._snapshot(stream_id)
        state = snapshot.state
        if state is None or snapshot.record_digest is None:
            raise CtxEngineError("install authorization has no authoritative session")
        pending = tuple(
            item
            for item in state.pending_effects
            if item.effect == "install"
            and item.action.action_id == action.action_id
            and item.action.content_digest == action.content_digest
        )
        capability = state.capability(presentation.capability_id)
        committed_plan = state.committed_plan
        expected_payload = {
            "schema": INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
            "capability_kind": presentation.kind,
            "catalog_identity": selection.catalog_identity.to_dict(),
            "result_material": authority.result_material.to_dict(),
            "install_plan_descriptor": descriptor.to_dict(),
            "installer_digest": action.payload.get("installer_digest"),
            "policy_snapshot_digest": expected_policy_digest,
        }
        expected_verification = {
            "receipt_required": True,
            "expected_state": "installed",
            "receipt_schema": INSTALL_RECEIPT_SCHEMA_V3,
        }
        expected_rollback = {
            "kind": "UninstallCapability",
            "installer_id": descriptor.installer_id,
        }
        if (
            len(pending) != 1
            or pending[0].action.to_json() != action.to_json()
            or not isinstance(capability, CapabilityStateV3)
            or capability.installation != "absent"
            or capability.activation != "inactive"
            or capability.current_authorized_material is not None
            or capability.selection.selection != selection
            or capability.material_identity != authority.result_material
            or capability.plan_id != action.plan_id
            or capability.catalog_snapshot_id != expected_catalog_snapshot_digest
            or not isinstance(committed_plan, CommittedPlanV3)
            or committed_plan.plan_id != action.plan_id
            or committed_plan.catalog_snapshot_id != expected_catalog_snapshot_digest
            or not any(row.selection == selection for row in committed_plan.capabilities)
            or state.install_policy_snapshot_digest != expected_policy_digest
            or action.kind != "InstallCapability"
            or action.entity_id != presentation.capability_id
            or action.source_digest != presentation.source_digest
            or action.catalog_snapshot_id != expected_catalog_snapshot_digest
            or action.consent_id is None
            or action.expires_at is None
            or action.required_host_feature != "installation"
            or dict(action.payload) != expected_payload
            or not self._is_sha256(action.payload.get("installer_digest"))
            or dict(action.verification) != expected_verification
            or dict(action.rollback) != expected_rollback
        ):
            raise CtxEngineError("install action is not the exact pending journal authority")

        action_json = action.to_json()
        authorization_digest = install_action_authorization_digest(
            action=action,
            selection=selection,
            descriptor=descriptor,
            catalog_snapshot_digest=expected_catalog_snapshot_digest,
            policy_snapshot_digest=expected_policy_digest,
        )
        request = InstallActionClaimRequest(
            stream_id=stream_id,
            expected_revision=snapshot.revision,
            expected_head_record_digest=snapshot.record_digest,
            action_json=action_json,
            authorization_digest=authorization_digest,
            execution_binding_json=execution_binding.to_json(),
        )
        self._install_claim_store().claim_install(
            request,
            trusted_utc_now=self._trusted_utc_now,
        )

    def authorize_activation(
        self,
        action: HostAction,
        *,
        execution_binding: InstallExecutionBinding,
        expected_host_descriptor_digest: str,
    ) -> None:
        """Atomically claim one exact pending schema-v3 activation."""

        if not isinstance(action, HostAction):
            raise TypeError("action must be a HostAction")
        if not isinstance(execution_binding, InstallExecutionBinding):
            raise TypeError("execution_binding must be an InstallExecutionBinding")
        if not self._is_sha256(expected_host_descriptor_digest):
            raise CtxEngineError("activation requires an exact host descriptor digest")
        stream_id = StreamId.from_scope(action.scope)
        snapshot = self._snapshot(stream_id)
        state = snapshot.state
        if state is None or snapshot.record_digest is None:
            raise CtxEngineError("activation authorization has no authoritative session")
        pending = tuple(
            item
            for item in state.pending_effects
            if item.effect == "activate"
            and item.action.action_id == action.action_id
            and item.action.content_digest == action.content_digest
        )
        capability = state.capability(action.entity_id or "")
        current = (
            None
            if not isinstance(capability, CapabilityStateV3)
            else capability.current_authorized_material
        )
        if (
            len(pending) != 1
            or pending[0].action.to_json() != action.to_json()
            or not isinstance(capability, CapabilityStateV3)
            or capability.installation != "installed"
            or capability.activation != "inactive"
            or current is None
            or state.host_descriptor_digest != expected_host_descriptor_digest
            or action.kind != "ActivateCapability"
            or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
            or action.payload.get("capability_kind") != capability.kind
            or action.payload.get("catalog_identity") != capability.catalog_identity.to_dict()
            or action.payload.get("material_identity") != capability.material_identity.to_dict()
            or action.payload.get("authorized_material") != current.to_dict()
            or action.required_host_feature != "activation"
            or action.expires_at is None
        ):
            raise CtxEngineError("activation action is not exact pending journal authority")
        authorization_digest = activation_action_authorization_digest(
            action=action,
            execution_binding=execution_binding,
            host_descriptor_digest=expected_host_descriptor_digest,
        )
        self._activation_claim_store().claim_activation(
            ActivationActionClaimRequest(
                stream_id=stream_id,
                expected_revision=snapshot.revision,
                expected_head_record_digest=snapshot.record_digest,
                action_json=action.to_json(),
                authorization_digest=authorization_digest,
                execution_binding_json=execution_binding.to_json(),
            ),
            trusted_utc_now=self._trusted_utc_now,
        )

    def _issue_install_outcome_permit(
        self,
        action: HostAction,
        execution_binding: InstallExecutionBinding,
    ) -> _InstallOutcomePermit:
        """Issue non-exported authority after the durable claim is observable."""

        status = self.install_execution_status(action)
        if not status.claimed:
            raise InstallExecutionOutcomeRequired(
                f"install action {action.action_id!r} has no durable execution claim"
            )
        if status.execution_binding_digest != execution_binding.binding_digest:
            raise CtxEngineError("install execution binding does not match the durable claim")
        if status.settled or status.outcome_recorded:
            raise CtxEngineError("install execution cannot mint outcome authority")
        return _InstallOutcomePermit(
            engine_identity=id(self),
            action_content_digest=action.content_digest,
            binding_digest=execution_binding.binding_digest,
        )

    def _record_install_outcome(
        self,
        action: HostAction,
        *,
        execution_binding: InstallExecutionBinding,
        execution_authority: _InstallOutcomePermit,
        outcome: Literal["applied", "failed"],
        observed_material_identity_digest: str | None,
        verification_digest: str,
    ) -> InstallActionClaimGuard:
        """Persist an independently verified physical observation for settlement."""

        if not isinstance(action, HostAction):
            raise TypeError("action must be a HostAction")
        if not isinstance(execution_binding, InstallExecutionBinding):
            raise TypeError("execution_binding must be an InstallExecutionBinding")
        if not isinstance(execution_authority, _InstallOutcomePermit):
            raise TypeError("execution_authority must be a coordinator-issued permit")
        execution_authority._consume(
            engine_identity=id(self),
            action_content_digest=action.content_digest,
            binding_digest=execution_binding.binding_digest,
        )
        if outcome not in {"applied", "failed"}:
            raise ValueError("outcome must be applied or failed")
        if not self._is_sha256(verification_digest):
            raise CtxEngineError("verification digest must be an exact SHA-256 digest")
        if observed_material_identity_digest is not None and not self._is_sha256(
            observed_material_identity_digest
        ):
            raise CtxEngineError("observed material identity must be an exact SHA-256 digest")
        if (
            action.kind != "InstallCapability"
            or action.payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            raise CtxEngineError("execution outcome requires an exact schema-v3 install action")
        result_material = action.payload.get("result_material")
        expected_material = (
            result_material.get("identity_digest") if isinstance(result_material, Mapping) else None
        )
        if outcome == "applied" and observed_material_identity_digest != expected_material:
            raise CtxEngineError("applied outcome does not match authorized result material")
        if outcome == "failed" and observed_material_identity_digest is not None:
            raise CtxEngineError("failed outcome requires verified material absence")
        request = InstallExecutionOutcomeRequest(
            stream_id=StreamId.from_scope(action.scope),
            action_json=action.to_json(),
            execution_binding_digest=execution_binding.binding_digest,
            outcome=outcome,
            observed_material_identity_digest=observed_material_identity_digest,
            verification_digest=verification_digest,
        )
        store = self._install_claim_store()
        if not callable(getattr(store, "record_install_outcome", None)):
            raise CtxEngineError("engine store does not support verified install outcomes")
        try:
            record = store.record_install_outcome(
                request,
                trusted_utc_now=self._trusted_utc_now,
            )
        except InstallExecutionOutcomeConflict as exc:
            raise CtxEngineError(str(exc)) from None
        return record.settlement_guard

    def _issue_activation_outcome_permit(
        self,
        action: HostAction,
        execution_binding: InstallExecutionBinding,
    ) -> _InstallOutcomePermit:
        status = self.activation_execution_status(action)
        if not status.claimed:
            raise ActivationExecutionOutcomeRequired(
                f"activation action {action.action_id!r} has no durable claim"
            )
        if status.execution_binding_digest != execution_binding.binding_digest:
            raise CtxEngineError("activation verifier binding does not match durable claim")
        if status.settled or status.outcome_recorded:
            raise CtxEngineError("activation cannot mint duplicate outcome authority")
        return _InstallOutcomePermit(
            engine_identity=id(self),
            action_content_digest=action.content_digest,
            binding_digest=execution_binding.binding_digest,
        )

    def _record_activation_outcome(
        self,
        action: HostAction,
        *,
        execution_binding: InstallExecutionBinding,
        execution_authority: _InstallOutcomePermit,
        observed_material_identity_digest: str,
        verification_digest: str,
    ) -> ActivationActionClaimGuard:
        if (
            not isinstance(action, HostAction)
            or action.kind != "ActivateCapability"
            or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            raise CtxEngineError("activation outcome requires exact schema-v3 authority")
        if not isinstance(execution_binding, InstallExecutionBinding):
            raise TypeError("execution_binding must be an InstallExecutionBinding")
        if not isinstance(execution_authority, _InstallOutcomePermit):
            raise TypeError("execution_authority must be engine-issued")
        execution_authority._consume(
            engine_identity=id(self),
            action_content_digest=action.content_digest,
            binding_digest=execution_binding.binding_digest,
        )
        for value, field_name in (
            (observed_material_identity_digest, "observed_material_identity_digest"),
            (verification_digest, "verification_digest"),
        ):
            if not self._is_sha256(value):
                raise CtxEngineError(f"{field_name} must be an exact SHA-256 digest")
        material = action.payload.get("material_identity")
        expected_material = (
            material.get("identity_digest") if isinstance(material, Mapping) else None
        )
        if observed_material_identity_digest != expected_material:
            raise CtxEngineError("activation outcome does not match authorized material")
        try:
            record = self._activation_claim_store().record_activation_outcome(
                ActivationExecutionOutcomeRequest(
                    stream_id=StreamId.from_scope(action.scope),
                    action_json=action.to_json(),
                    execution_binding_digest=execution_binding.binding_digest,
                    observed_material_identity_digest=observed_material_identity_digest,
                    verification_digest=verification_digest,
                ),
                trusted_utc_now=self._trusted_utc_now,
            )
        except ActivationExecutionOutcomeConflict as exc:
            raise CtxEngineError(str(exc)) from None
        return record.settlement_guard

    def install_execution_status(self, action: HostAction) -> InstallExecutionStatus:
        """Return restart-safe audit state without recreating execution authority."""

        if not isinstance(action, HostAction):
            raise TypeError("action must be a HostAction")
        if (
            action.kind != "InstallCapability"
            or action.payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            raise CtxEngineError("execution status requires an exact schema-v3 install action")
        store = self._install_claim_store()
        if not callable(getattr(store, "install_execution_status", None)):
            raise CtxEngineError("engine store does not support install execution status")
        return store.install_execution_status(
            StreamId.from_scope(action.scope),
            action.action_id,
        )

    def activation_execution_status(self, action: HostAction) -> ActivationExecutionStatus:
        """Return restart-safe activation claim and outcome state."""

        if not isinstance(action, HostAction):
            raise TypeError("action must be a HostAction")
        if (
            action.kind != "ActivateCapability"
            or action.payload.get("schema") != MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            raise CtxEngineError("execution status requires an exact schema-v3 activation action")
        store = self._activation_claim_store()
        return store.activation_execution_status(
            StreamId.from_scope(action.scope),
            action.action_id,
        )

    def _install_execution_lock_target(self, action: HostAction) -> Path:
        """Resolve the one canonical lock domain owned by the durable journal."""

        if not isinstance(action, HostAction):
            raise TypeError("action must be a HostAction")
        resolver = getattr(self._store, "install_execution_lock_target", None)
        if not callable(resolver):
            raise CtxEngineError("engine store has no canonical install lock domain")
        target = resolver(StreamId.from_scope(action.scope), action.action_id)
        if not isinstance(target, Path):
            raise CtxEngineError("engine store returned an invalid install lock target")
        return target

    def _install_claim_store(self) -> _InstallClaimStore:
        """Require the additive one-use authority surface only in manage paths."""

        if not callable(getattr(self._store, "claim_install", None)):
            raise CtxEngineError("engine store does not support one-use install claims")
        return cast(_InstallClaimStore, self._store)

    def _activation_claim_store(self) -> _ActivationClaimStore:
        """Require the additive activation verifier authority surface."""

        if not callable(getattr(self._store, "claim_activation", None)):
            raise CtxEngineError("engine store does not support activation claims")
        return cast(_ActivationClaimStore, self._store)

    @staticmethod
    def _install_receipt_claim_guard(
        event: EngineEvent,
        state: EngineState | None,
    ) -> InstallActionClaimGuard | None:
        mode = {
            "ActionApplied": "applied",
            "ActionFailed": "failed",
            "ActionExpired": "expired",
        }.get(event.kind)
        if mode is None or state is None:
            return None
        action_id = event.payload.get("action_id")
        pending = next(
            (
                item
                for item in state.pending_effects
                if item.effect == "install" and item.action.action_id == action_id
            ),
            None,
        )
        if pending is None:
            return None
        action = pending.action
        capability = state.capability(action.entity_id or "")
        if (
            not isinstance(capability, CapabilityStateV3)
            or action.payload.get("schema") != INSTALL_ACTION_PAYLOAD_SCHEMA_V3
        ):
            return None
        return InstallActionClaimGuard(
            action_id=action.action_id,
            action_content_digest=action.content_digest,
            mode=mode,  # type: ignore[arg-type]
        )

    @staticmethod
    def _resolve_install_receipt_guard(
        *,
        derived: InstallActionClaimGuard | None,
        supplied: InstallActionClaimGuard | None,
    ) -> InstallActionClaimGuard | None:
        if derived is None:
            if supplied is not None:
                raise CtxEngineError("install execution outcome does not match a pending receipt")
            return None
        if derived.mode == "expired":
            if supplied is not None:
                raise CtxEngineError("expired install actions cannot use execution outcomes")
            return derived
        if supplied is None:
            raise InstallExecutionOutcomeRequired(
                f"install action {derived.action_id!r} has no verified execution outcome"
            )
        if (
            supplied.action_id != derived.action_id
            or supplied.action_content_digest != derived.action_content_digest
            or supplied.mode != derived.mode
            or supplied.execution_outcome_digest is None
        ):
            raise CtxEngineError("install receipt does not match its execution outcome")
        return supplied

    @staticmethod
    def _assert_install_guard_matches_event(
        event: EngineEvent,
        guard: InstallActionClaimGuard,
    ) -> None:
        expected_kind = "ActionApplied" if guard.mode == "applied" else "ActionFailed"
        if (
            event.kind != expected_kind
            or event.payload.get("action_id") != guard.action_id
            or event.payload.get("action_content_digest") != guard.action_content_digest
        ):
            raise CtxEngineError("install receipt does not match its execution outcome")

    @staticmethod
    def _activation_receipt_claim_guard(
        event: EngineEvent,
        state: EngineState | None,
    ) -> PendingEffect | None:
        # The match stays keyed on the action kind, never on the pending effect:
        # narrowing it to `activate` would make a `rollback-activate` receipt
        # derive nothing and commit with no durable claim and no verified
        # outcome.  The effect is inspected only by the resolver below.
        if event.kind not in {"ActionApplied", "ActionFailed", "ActionExpired"} or state is None:
            return None
        action_id = event.payload.get("action_id")
        pending = next(
            (
                item
                for item in state.pending_effects
                if item.action.action_id == action_id
                and item.action.kind == "ActivateCapability"
                and item.action.payload.get("schema") == MATERIAL_ACTION_PAYLOAD_SCHEMA_V3
            ),
            None,
        )
        if pending is None:
            return None
        return pending

    @staticmethod
    def _resolve_activation_receipt_guard(
        *,
        event: EngineEvent,
        derived: PendingEffect | None,
        supplied: ActivationActionClaimGuard | None,
    ) -> ActivationActionClaimGuard | None:
        if derived is None:
            if supplied is not None:
                raise CtxEngineError(
                    "activation execution outcome does not match a pending receipt"
                )
            return None
        action = derived.action
        if event.kind == "ActionExpired" and derived.effect == "activate":
            # Retiring a never-claimed activation grants no physical authority,
            # so it carries no execution outcome.  The durable "no claim exists"
            # proof is enforced by the store inside the commit transaction.
            if supplied is not None:
                raise CtxEngineError("expired activation actions cannot use execution outcomes")
            return ActivationActionClaimGuard(
                action_id=action.action_id,
                action_content_digest=action.content_digest,
                mode="expired",
            )
        if supplied is None:
            raise ActivationExecutionOutcomeRequired(
                f"activation action {action.action_id!r} has no verified execution outcome"
            )
        if (
            supplied.action_id != action.action_id
            or supplied.action_content_digest != action.content_digest
            or supplied.mode != "applied"
        ):
            raise CtxEngineError("activation receipt does not match its execution outcome")
        return supplied

    @staticmethod
    def _assert_activation_guard_matches_event(
        event: EngineEvent,
        guard: ActivationActionClaimGuard,
    ) -> None:
        if (
            event.kind != "ActionApplied"
            or event.payload.get("action_kind") != "ActivateCapability"
            or event.payload.get("action_id") != guard.action_id
            or event.payload.get("action_content_digest") != guard.action_content_digest
        ):
            raise CtxEngineError("activation receipt does not match its execution outcome")

    def authorize_prompt_context(
        self,
        action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        *,
        expected_catalog_snapshot_digest: str,
    ) -> None:
        """Prove one pending bundle action before raw prompt context is prepared."""

        if (
            not isinstance(action, HostAction)
            or not isinstance(selections, tuple)
            or not 1 <= len(selections) <= 5
            or not all(isinstance(item, CapabilityPlanSelectionV3) for item in selections)
        ):
            raise TypeError("prompt context authorization requires an exact typed bundle")
        if (
            action.kind != "PreparePromptContext"
            or action.entity_id is not None
            or action.catalog_snapshot_id != expected_catalog_snapshot_digest
        ):
            raise CtxEngineError("prompt context authorization does not match the requested bundle")
        stream_id = StreamId.from_scope(action.scope)
        snapshot = self._snapshot(stream_id)
        state = snapshot.state
        if state is None or snapshot.record_digest is None:
            raise CtxEngineError("prompt context authorization has no authoritative session")
        pending = tuple(
            item
            for item in state.pending_effects
            if item.effect == "prompt-context"
            and item.action.action_id == action.action_id
            and item.action.content_digest == action.content_digest
        )
        committed_plan = state.committed_plan
        expected_intent = {
            "prompt-context-activate": "activate",
            "prompt-context-experiment": "experiment",
        }.get(state.host_level)
        if (
            len(pending) != 1
            or pending[0].action.to_json() != action.to_json()
            or not isinstance(committed_plan, CommittedPlanV3)
            or committed_plan.status != "ready"
            or committed_plan.plan_id != action.plan_id
            or committed_plan.catalog_snapshot_id != expected_catalog_snapshot_digest
            or action.precondition_revision != snapshot.revision
            or expected_intent is None
            or action.payload.get("execution_intent") != expected_intent
            or action.payload.get("plan_digest") != committed_plan.decision_digest
        ):
            raise CtxEngineError("prompt context action is not exact pending journal authority")
        committed_loads = tuple(
            row.selection
            for row in committed_plan.capabilities
            if isinstance(row.authority, LoadPlanningAuthority)
        )
        if selections != committed_loads:
            raise CtxEngineError("prompt context bundle changed the committed load selections")
        expected_rows: list[dict[str, object]] = []
        for selection in selections:
            capability = state.capability(selection.presentation.capability_id)
            if not isinstance(capability, CapabilityStateV3):
                raise CtxEngineError("prompt context selection lacks runtime authority")
            current_material = capability.current_authorized_material
            if (
                capability.selection.selection != selection
                or capability.installation != "installed"
                or capability.activation != "inactive"
                or capability.leases
                or current_material is None
            ):
                raise CtxEngineError("prompt context selection changed runtime state")
            expected_rows.append(
                {
                    "authorized_material": current_material.to_dict(),
                    "capability_id": capability.capability_id,
                    "capability_kind": capability.kind,
                    "catalog_identity": capability.catalog_identity.to_dict(),
                    "material_identity": capability.material_identity.to_dict(),
                    "source_digest": capability.source_digest,
                }
            )
        if tuple(action.payload.get("capabilities", ())) != tuple(expected_rows):
            raise CtxEngineError("prompt context action changed its exact material rows")
        presentation_id = action.payload.get("presentation_action_id")
        presentation_digest = action.payload.get("presentation_action_content_digest")
        bound = False
        for record in self._store.records(stream_id):
            if record.revision != snapshot.revision:
                continue
            transition = Transition.from_json(record.transition_json)
            presentation = next(
                (
                    candidate
                    for candidate in transition.actions
                    if candidate.kind == "PresentBundle"
                    and candidate.action_id == presentation_id
                    and candidate.content_digest == presentation_digest
                ),
                None,
            )
            prompt = next(
                (
                    candidate
                    for candidate in transition.actions
                    if candidate.kind == "PreparePromptContext"
                    and candidate.content_digest == action.content_digest
                ),
                None,
            )
            bound = presentation is not None and prompt is not None
        if not bound:
            raise CtxEngineError("prompt context action lost its presentation binding")
        try:
            expires_at = datetime.fromisoformat((action.expires_at or "").replace("Z", "+00:00"))
            now = self._trusted_utc_now()
        except Exception:
            raise CtxEngineError("prompt context authority has no trusted expiry") from None
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.astimezone(UTC) >= expires_at.astimezone(UTC)
        ):
            raise CtxEngineError("prompt context authority has expired")

    def _issue_prompt_context_material_permit(
        self,
        action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        *,
        expected_catalog_snapshot_digest: str,
    ) -> _PromptContextMaterialPermit:
        """Authorize once and return an unforgeable one-use bundle permit."""

        self.authorize_prompt_context(
            action,
            selections,
            expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
        )
        return _PromptContextMaterialPermit._create(
            action_json=action.to_json(),
            selections=selections,
            expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
            token=_PROMPT_CONTEXT_MATERIAL_PERMIT_TOKEN,
        )

    def _issue_prompt_context_receipt_permit(
        self,
        action: HostAction,
        receipt_event: EngineEvent,
    ) -> _PromptContextReceiptPermit:
        """Prove that one exact prompt-context receipt is the revision-three head."""

        if not isinstance(action, HostAction) or not isinstance(receipt_event, EngineEvent):
            raise TypeError("prompt context receipt proof requires typed protocol values")
        if (
            action.kind != "PreparePromptContext"
            or receipt_event.kind != "ActionApplied"
            or receipt_event.scope != action.scope
            or receipt_event.expected_revision != 2
            or receipt_event.payload.get("action_id") != action.action_id
            or receipt_event.payload.get("action_kind") != action.kind
            or receipt_event.payload.get("action_content_digest") != action.content_digest
            or receipt_event.payload.get("action_precondition_revision")
            != action.precondition_revision
            or receipt_event.payload.get("verification", {}).get("schema")
            != "ctx.prompt-context-receipt-v1"
            or action.expires_at is None
        ):
            raise CtxEngineError("prompt context receipt does not match its exact action")
        stream_id = StreamId.from_scope(action.scope)
        snapshot = self._snapshot(stream_id)
        if snapshot.revision != 3 or snapshot.record_digest is None or snapshot.state is None:
            raise CtxEngineError("prompt context receipt is not the revision-three journal head")
        records = tuple(self._store.records(stream_id))
        issuing = next((record for record in records if record.revision == 2), None)
        final = next((record for record in records if record.revision == 3), None)
        if issuing is None or final is None or len(records) != 3:
            raise CtxEngineError("prompt context receipt journal history is incomplete")
        issuing_transition = Transition.from_json(issuing.transition_json)
        journal_action = next(
            (
                candidate
                for candidate in issuing_transition.actions
                if candidate.action_id == action.action_id
                and candidate.content_digest == action.content_digest
            ),
            None,
        )
        if (
            journal_action is None
            or journal_action.to_json() != action.to_json()
            or final.event_id != receipt_event.event_id
            or final.event_content_digest != receipt_event.content_digest
            or final.record_digest != snapshot.record_digest
            or snapshot.state.pending_effects
        ):
            raise CtxEngineError("prompt context receipt lost its authoritative journal binding")
        return _PromptContextReceiptPermit._create(
            action_id=action.action_id,
            action_content_digest=action.content_digest,
            receipt_event_content_digest=receipt_event.content_digest,
            issuing_record_digest=issuing.record_digest,
            final_record_digest=final.record_digest,
            expires_at=action.expires_at,
            token=_PROMPT_CONTEXT_PERMIT_TOKEN,
        )

    def authorize_exposure(
        self,
        action: HostAction,
        selection: CapabilitySelection | CapabilityPlanSelectionV3,
        *,
        expected_catalog_snapshot_digest: str,
    ) -> None:
        """Prove an exposure action and selection against the authoritative journal."""

        if not isinstance(action, HostAction) or not isinstance(
            selection,
            (CapabilitySelection, CapabilityPlanSelectionV3),
        ):
            raise TypeError("exposure authorization requires typed action and selection values")
        presentation = (
            selection.presentation
            if isinstance(selection, CapabilityPlanSelectionV3)
            else selection
        )
        if (
            action.kind != "PrepareExposure"
            or action.entity_id != presentation.capability_id
            or action.source_digest != presentation.source_digest
            or action.catalog_snapshot_id != expected_catalog_snapshot_digest
        ):
            raise CtxEngineError("exposure authorization does not match the requested material")
        stream_id = StreamId.from_scope(action.scope)
        snapshot = self._snapshot(stream_id)
        state = snapshot.state
        if state is None:
            raise CtxEngineError("exposure authorization has no authoritative session")
        pending = tuple(
            item
            for item in state.pending_effects
            if item.effect == "prepare"
            and item.action.action_id == action.action_id
            and item.action.content_digest == action.content_digest
        )
        capability = state.capability(presentation.capability_id)
        if (
            len(pending) != 1
            or pending[0].action.to_json() != action.to_json()
            or capability is None
            or capability.activation != "active"
            or not capability.desired
            or capability.source_digest != presentation.source_digest
            or capability.catalog_snapshot_id != expected_catalog_snapshot_digest
            or capability.plan_id != action.plan_id
            or capability.activation_lease_id != action.lease_id
        ):
            raise CtxEngineError("exposure action is not the exact pending journal action")

        if isinstance(selection, CapabilityPlanSelectionV3):
            if not isinstance(capability, CapabilityStateV3):
                raise CtxEngineError("schema-v3 exposure lacks schema-v3 runtime authority")
            committed_plan = state.committed_plan
            current_material = capability.current_authorized_material
            if (
                not isinstance(committed_plan, CommittedPlanV3)
                or committed_plan.plan_id != action.plan_id
                or capability.selection.selection != selection
                or not any(row.selection == selection for row in committed_plan.capabilities)
                or current_material is None
                or action.payload.get("catalog_identity") != capability.catalog_identity.to_dict()
                or action.payload.get("material_identity") != capability.material_identity.to_dict()
                or action.payload.get("authorized_material") != current_material.to_dict()
            ):
                raise CtxEngineError("exposure selection is not committed in the journal")
            return

        committed_plan = state.committed_plan
        decision_digest = (
            committed_plan.decision_digest
            if committed_plan is not None and committed_plan.plan_id == action.plan_id
            else action.plan_id
        )
        if decision_digest is None or not self._selection_is_committed(
            stream_id,
            selection,
            decision_digest=decision_digest,
        ):
            raise CtxEngineError("exposure selection is not committed in the journal")

    def _selection_is_committed(
        self,
        stream_id: StreamId,
        selection: CapabilitySelection,
        *,
        decision_digest: str,
    ) -> bool:
        for record in self._store.records(stream_id):
            transition = Transition.from_json(record.transition_json)
            for bundle in (
                candidate for candidate in transition.actions if candidate.kind == "PresentBundle"
            ):
                if bundle.payload.get("plan_digest") != decision_digest:
                    continue
                if any(
                    self._selection_matches_row(raw, selection)
                    for raw in bundle.payload.get("capabilities", ())
                ):
                    return True
        return False

    @staticmethod
    def _selection_matches_row(raw: object, selection: CapabilitySelection) -> bool:
        if not isinstance(raw, Mapping):
            return False
        install_descriptor_digest = raw.get("install_descriptor_digest")
        install_plan_digest = raw.get("install_plan_digest")
        return bool(
            raw.get("capability_id") == selection.capability_id
            and raw.get("kind") == selection.kind
            and raw.get("name") == selection.name
            and raw.get("catalog_entry_digest") == selection.source_digest
            and raw.get("normalized_score_ppm") == selection.normalized_score_ppm
            and tuple(raw.get("matching_signals", ())) == selection.matching_signals
            and tuple(raw.get("reason_codes", ())) == selection.reason_codes
            and raw.get("actionability") == selection.actionability
            and (
                install_descriptor_digest == selection.install_descriptor_digest
                if "install_descriptor_digest" in raw
                else selection.install_descriptor_digest is None
            )
            and (
                install_plan_digest == selection.install_plan_digest
                if "install_plan_digest" in raw
                else selection.install_plan_digest is None
            )
        )

    @staticmethod
    def _is_sha256(value: object) -> bool:
        return bool(
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def _snapshot(self, stream_id: StreamId) -> EngineSnapshot:
        for _ in range(_SNAPSHOT_ATTEMPTS):
            before = self._store.load_head(stream_id)
            records = tuple(self._store.records(stream_id))
            after = self._store.load_head(stream_id)
            if not self._stable_cursor(before, after, records):
                continue
            if not records:
                if not before.projection_valid:
                    self._diverged(stream_id, 0, "projection")
                return EngineSnapshot(
                    stream_id=stream_id,
                    revision=0,
                    state=None,
                    record_digest=None,
                )
            state = self._replay_records(stream_id, records)
            state_json = state.to_json()
            if before.state_json != state_json:
                self._diverged(stream_id, before.revision, "head")
            if before.projection_valid:
                return EngineSnapshot(
                    stream_id=stream_id,
                    revision=before.revision,
                    state=state,
                    record_digest=before.record_digest,
                )
            assert before.record_digest is not None
            repaired = self._store.repair_projection(
                stream_id,
                at_revision=before.revision,
                state_json=state_json,
                record_digest=before.record_digest,
            )
            if not repaired:
                continue
            confirmed = self._store.load_head(stream_id)
            if (
                confirmed.revision == before.revision
                and confirmed.record_digest == before.record_digest
                and confirmed.projection_valid
            ):
                return EngineSnapshot(
                    stream_id=stream_id,
                    revision=before.revision,
                    state=state,
                    record_digest=before.record_digest,
                    projection_repaired=True,
                )
        raise SnapshotContentionError(stream_id)

    def _replay_records(
        self,
        stream_id: StreamId,
        records: tuple[JournalRecord, ...],
    ) -> EngineState:
        state: EngineState | None = None
        for record in records:
            try:
                replay = ReplayInput.from_json(record.replay_json)
                replay.assert_record_binding(record)
            except ReplayError:
                self._diverged(stream_id, record.revision, "record-binding")
            reducer_fn = self._reducers.resolve(replay.reducer_version)
            try:
                next_state, transition = reducer_fn(state, replay)
                state_json, transition_json = self._validate_reduction(
                    stream_id=stream_id,
                    event_id=record.event_id,
                    expected_revision=record.revision - 1,
                    state=next_state,
                    transition=transition,
                )
            except ReplayDivergenceError:
                raise
            except Exception:
                self._diverged(stream_id, record.revision, "reducer")
            if transition_json != record.transition_json:
                self._diverged(stream_id, record.revision, "transition")
            if state_json != record.result_state_json:
                self._diverged(stream_id, record.revision, "state")
            state = next_state
        assert state is not None
        return state

    def _cached(self, event: EngineEvent, stream_id: StreamId) -> Transition | None:
        return self._store.cached_transition(
            stream_id,
            event.event_id,
            event.content_digest,
        )

    @staticmethod
    def _stable_cursor(
        before: StoredHead,
        after: StoredHead,
        records: tuple[JournalRecord, ...],
    ) -> bool:
        if before.revision != after.revision or before.record_digest != after.record_digest:
            return False
        if before.revision == 0:
            return not records and before.record_digest is None
        return bool(
            len(records) == before.revision
            and records[-1].revision == before.revision
            and records[-1].record_digest == before.record_digest
        )

    @staticmethod
    def _assert_prepared_binding(
        event: EngineEvent,
        stream_id: StreamId,
        preflight: PreflightReplayInput,
        replay: ReplayInput,
    ) -> None:
        reducer_event = replay.reducer_event
        if (
            replay.source_event_content_digest != preflight.source_event_content_digest
            or reducer_event.to_json() != preflight.reducer_event.to_json()
            or replay.source_event_content_digest != event.content_digest
            or reducer_event.event_id != event.event_id
            or reducer_event.expected_revision != event.expected_revision
            or StreamId.from_scope(reducer_event.scope) != stream_id
            or reducer_event.privacy != event.privacy
        ):
            raise ReplayDivergenceError(
                stream_id=stream_id,
                revision=event.expected_revision + 1,
                component="prepared-input",
            )

    @staticmethod
    def _validate_reduction(
        *,
        stream_id: StreamId,
        event_id: str,
        expected_revision: int,
        state: EngineState,
        transition: Transition,
    ) -> tuple[str, str]:
        revision = expected_revision + 1
        if not isinstance(state, EngineState) or not isinstance(transition, Transition):
            raise ReplayDivergenceError(
                stream_id=stream_id,
                revision=revision,
                component="reducer-output",
            )
        if (
            state.revision != revision
            or StreamId.from_scope(state.scope) != stream_id
            or transition.event_id != event_id
            or transition.from_revision != expected_revision
            or transition.to_revision != revision
            or StreamId.from_scope(transition.scope) != stream_id
        ):
            raise ReplayDivergenceError(
                stream_id=stream_id,
                revision=revision,
                component="reducer-output",
            )
        return state.to_json(), transition.to_json()

    @staticmethod
    def _assert_commit_result(
        *,
        stream_id: StreamId,
        computed: JournalRecord,
        transition: Transition,
        committed: CommitResult,
    ) -> None:
        stored = committed.record
        if (
            committed.revision != computed.revision
            or stored.stream_id != computed.stream_id
            or stored.revision != computed.revision
            or committed.transition.to_json() != transition.to_json()
            or stored.event_id != computed.event_id
            or stored.event_content_digest != computed.event_content_digest
            or stored.replay_json != computed.replay_json
            or stored.replay_digest != computed.replay_digest
            or stored.transition_json != computed.transition_json
            or stored.transition_digest != computed.transition_digest
            or stored.result_state_json != computed.result_state_json
            or stored.result_state_digest != computed.result_state_digest
            or stored.privacy_classification != computed.privacy_classification
            or stored.retention_class != computed.retention_class
            or stored.reducer_version != computed.reducer_version
            or not stored.record_digest
        ):
            raise ReplayDivergenceError(
                stream_id=stream_id,
                revision=computed.revision,
                component="commit-result",
            )

    @staticmethod
    def _diverged(stream_id: StreamId, revision: int, component: str) -> None:
        raise ReplayDivergenceError(
            stream_id=stream_id,
            revision=revision,
            component=component,
        ) from None


__all__ = [
    "CtxEngine",
    "CtxEngineError",
    "EngineSnapshot",
    "ReducerFn",
    "ReducerRegistry",
    "ReplayDivergenceError",
    "SnapshotContentionError",
    "UnsupportedReducerVersionError",
]
