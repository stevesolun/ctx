"""Host-neutral durable entry point for one managed CTX planning decision.

The service owns orchestration only.  Trusted host intake resolves an opaque
current-work reference to registry-issued planning inputs; callers never pass
paths, planners, graph handles, or installation authority.  Results expose the
authority-free committed plan and a deliberately small action summary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import NoReturn, Protocol, SupportsIndex

from ctx.core.install_consent_broker_store import (
    HumanDecisionVerifier,
    InstallConsentChallenge,
    InstallConsentChallengeRecord,
    SignedHumanDecisionAssertion,
)
from ctx.core.install_policy_store import (
    has_persisted_install_policy,
    hold_current_install_policy,
    load_current_install_policy,
)
from ctx.engine.benefit import NetBenefitPolicy
from ctx.engine.content import CapabilityMaterialPort
from ctx.engine.engine import EngineSnapshot
from ctx.engine.installation import (
    CapabilityInstallBundlePort,
    InstallConsentDirective,
    InstallConsentPolicy,
    InstallDecisionEvidenceQuery,
    InstallExecutionBinding,
    InteractiveInstallDecisionGuard,
    InteractiveInstallDecisionReservation,
    route_install_consent_request,
)
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, InstallPlanningAuthority
from ctx.engine.protocol import EngineEvent, HostAction, ScopeRef, Transition
from ctx.engine.replay import ReplayInput
from ctx.engine.state import CapabilityStateV3, CommittedPlanV3, EngineState
from ctx.engine.store import (
    EventIdCollision,
    InstallActionAlreadyClaimed,
    InstallActionClaimExpired,
    JournalRecord,
    RevisionConflict,
    SQLiteEngineStore,
    StreamId,
)
from ctx.runtime.agent_file import AgentFileRuntimeConfig
from ctx.runtime.composition import EngineComposition, open_managed_engine_composition
from ctx.runtime.install_consent_authenticators import (
    TrustedHumanDecisionVerifierRegistry,
    decode_signed_human_decision_assertion,
)
from ctx.runtime.install_consent_broker import (
    InstallConsentBrokerService,
    derive_install_consent_challenge,
    install_consent_selection_digest,
)
from ctx.runtime.install_execution import InstallExecutionReport
from ctx.runtime.managed_artifact_registry import (
    ManagedArtifactHandle,
    ManagedArtifactRegistry,
)
from ctx.runtime.managed_query import ManagedAdvanceResult, PreparedManagedQuery
from ctx.runtime.managed_query_store import (
    ManagedDesiredSetRecord,
    ManagedQueryRecord,
    ManagedQueryStore,
    ManagedQueryStoreConflict,
    ManagedQueryStoreNotFound,
)
from ctx.runtime.planning_v3 import AuthenticatedBenefitFactsPort
from ctx.runtime.skill_cas import SkillCasRuntimeConfig
from ctx.utils._file_lock import secure_file_lock


_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_WORK_REF_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}\Z")
_QUERY_REF_RE = re.compile(r"\Amqr_[0-9a-f]{64}\Z")
_DESIRED_SET_REF_RE = re.compile(r"\Amds_[0-9a-f]{64}\Z")
_CAPABILITY_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_MAX_ACTION_SUMMARIES = 16
_ACTION_FACTORY_TOKEN = object()
_CHALLENGE_FACTORY_TOKEN = object()
_CONSENT_RESULT_FACTORY_TOKEN = object()
_DESIRED_RESULT_FACTORY_TOKEN = object()
_RESULT_FACTORY_TOKEN = object()
_SERVICE_FACTORY_TOKEN = object()


def _stable_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


class ManagedQueryServiceError(RuntimeError):
    """Managed planning could not preserve its exact durable identity."""


class ManagedQuerySupersededError(ManagedQueryServiceError):
    """A later committed plan replaced the requested historical projection."""


class ManagedQueryHeadDriftError(ManagedQueryServiceError):
    """The authoritative plan or pending consent changed during publication."""


class ManagedDesiredSetConflictError(ManagedQueryServiceError):
    """A logical desired choice was already bound to different exact content."""


class ManagedDesiredSetBusyError(ManagedQueryServiceError):
    """Another desired choice owns the stream's one pending reservation."""


class ManagedDesiredSetSupersededError(ManagedQuerySupersededError):
    """A different durable desired choice already superseded this request."""


class ManagedQueryInputAuthority(Protocol):
    """Trusted adapter boundary from an opaque work reference to safe inputs."""

    def resolve(self, current_work_ref: str) -> ManagedQueryInput: ...


class ManagedQueryRequest:
    """The complete untrusted public request surface."""

    __slots__ = ("current_work_ref", "logical_query_id")

    current_work_ref: str
    logical_query_id: str

    def __init__(self, *, logical_query_id: str, current_work_ref: str) -> None:
        if not isinstance(logical_query_id, str) or _DIGEST_RE.fullmatch(logical_query_id) is None:
            raise ValueError("logical_query_id must be a lowercase SHA-256 digest")
        if (
            not isinstance(current_work_ref, str)
            or _WORK_REF_RE.fullmatch(current_work_ref) is None
        ):
            raise ValueError("current_work_ref must be a bounded opaque token")
        object.__setattr__(self, "logical_query_id", logical_query_id)
        object.__setattr__(self, "current_work_ref", current_work_ref)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed query requests are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed query requests are immutable")

    def __repr__(self) -> str:
        return f"ManagedQueryRequest(logical_query_id={self.logical_query_id!r})"


class ManagedDesiredSetRequest:
    """Untrusted request to reconcile one subset of an existing committed plan."""

    __slots__ = (
        "capability_ids",
        "expected_previous_desired_set_ref",
        "logical_choice_id",
        "query_ref",
    )

    query_ref: str
    logical_choice_id: str
    capability_ids: tuple[str, ...]
    expected_previous_desired_set_ref: str | None

    def __init__(
        self,
        *,
        query_ref: str,
        logical_choice_id: str,
        capability_ids: tuple[str, ...],
        expected_previous_desired_set_ref: str | None = None,
    ) -> None:
        if not isinstance(query_ref, str) or _QUERY_REF_RE.fullmatch(query_ref) is None:
            raise ValueError("query_ref must be an opaque managed-query reference")
        if (
            not isinstance(logical_choice_id, str)
            or _DIGEST_RE.fullmatch(logical_choice_id) is None
        ):
            raise ValueError("logical_choice_id must be a lowercase SHA-256 digest")
        if type(capability_ids) is not tuple:
            raise TypeError("capability_ids must be an exact tuple")
        if len(capability_ids) > 5:
            raise ValueError("capability_ids cannot contain more than five choices")
        if not all(
            isinstance(capability_id, str)
            and _CAPABILITY_ID_RE.fullmatch(capability_id) is not None
            for capability_id in capability_ids
        ):
            raise ValueError("capability_ids must contain canonical capability tokens")
        if len(set(capability_ids)) != len(capability_ids):
            raise ValueError("capability_ids must be unique")
        if expected_previous_desired_set_ref is not None and (
            not isinstance(expected_previous_desired_set_ref, str)
            or _DESIRED_SET_REF_RE.fullmatch(expected_previous_desired_set_ref) is None
        ):
            raise ValueError(
                "expected_previous_desired_set_ref must be an opaque desired-set reference or None"
            )
        object.__setattr__(self, "query_ref", query_ref)
        object.__setattr__(self, "logical_choice_id", logical_choice_id)
        object.__setattr__(self, "capability_ids", capability_ids)
        object.__setattr__(
            self,
            "expected_previous_desired_set_ref",
            expected_previous_desired_set_ref,
        )

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed desired-set requests are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed desired-set requests are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed desired-set requests cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed desired-set requests cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed desired-set requests cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed desired-set requests cannot be serialized")

    def __repr__(self) -> str:
        return (
            "ManagedDesiredSetRequest("
            f"query_ref={self.query_ref!r}, logical_choice_id={self.logical_choice_id!r})"
        )


class ManagedQueryInput:
    """Trusted intake value containing only registry and protocol values."""

    __slots__ = ("artifact", "decision_event", "session_started")

    artifact: ManagedArtifactHandle
    session_started: EngineEvent
    decision_event: EngineEvent

    def __init__(
        self,
        *,
        artifact: ManagedArtifactHandle,
        session_started: EngineEvent,
        decision_event: EngineEvent,
    ) -> None:
        if type(artifact) is not ManagedArtifactHandle:
            raise TypeError("artifact must be an exact ManagedArtifactHandle")
        if type(session_started) is not EngineEvent or type(decision_event) is not EngineEvent:
            raise TypeError("managed query input events must be exact EngineEvent values")
        object.__setattr__(self, "artifact", artifact)
        object.__setattr__(self, "session_started", session_started)
        object.__setattr__(self, "decision_event", decision_event)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed query inputs are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed query inputs are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed query inputs cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed query inputs cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed query inputs cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed query inputs cannot be serialized")

    def __repr__(self) -> str:
        return f"ManagedQueryInput(manifest_digest={self.artifact.manifest_digest!r})"


class ManagedActionSummary:
    """Bounded, prose-free projection of one committed host action."""

    __slots__ = ("action_id", "entity_id", "kind")

    action_id: str
    kind: str
    entity_id: str | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed action summaries are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        action: HostAction,
    ) -> ManagedActionSummary:
        if factory_token is not _ACTION_FACTORY_TOKEN:
            raise TypeError("managed action summaries are factory-issued only")
        if type(action) is not HostAction:
            raise TypeError("managed action summary requires an exact HostAction")
        instance = object.__new__(cls)
        object.__setattr__(instance, "action_id", action.action_id)
        object.__setattr__(instance, "kind", action.kind)
        object.__setattr__(instance, "entity_id", action.entity_id)
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed action summaries are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed action summaries are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed action summaries cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed action summaries cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed action summaries cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed action summaries cannot be serialized")

    def __eq__(self, other: object) -> bool:
        return type(other) is ManagedActionSummary and (
            self.action_id,
            self.kind,
            self.entity_id,
        ) == (other.action_id, other.kind, other.entity_id)

    def __repr__(self) -> str:
        return (
            "ManagedActionSummary("
            f"action_id={self.action_id!r}, kind={self.kind!r}, entity_id={self.entity_id!r})"
        )


class ManagedConsentChallengeProjection:
    """Minimal authority-free identity for one currently pending human decision."""

    __slots__ = (
        "audience",
        "capability_id",
        "challenge_digest",
        "expires_at",
        "kind",
    )

    audience: str
    capability_id: str
    challenge_digest: str
    expires_at: str
    kind: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed consent challenge projections are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        challenge: InstallConsentChallenge,
    ) -> ManagedConsentChallengeProjection:
        if factory_token is not _CHALLENGE_FACTORY_TOKEN:
            raise TypeError("managed consent challenge projections are factory-issued only")
        if type(challenge) is not InstallConsentChallenge:
            raise TypeError("challenge projection requires an exact InstallConsentChallenge")
        instance = object.__new__(cls)
        for name in cls.__slots__:
            object.__setattr__(instance, name, getattr(challenge, name))
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed consent challenge projections are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed consent challenge projections are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed consent challenge projections cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed consent challenge projections cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed consent challenge projections cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed consent challenge projections cannot be serialized")

    def __eq__(self, other: object) -> bool:
        return type(other) is ManagedConsentChallengeProjection and all(
            getattr(self, name) == getattr(other, name) for name in self.__slots__
        )

    def __repr__(self) -> str:
        return (
            "ManagedConsentChallengeProjection("
            f"challenge_digest={self.challenge_digest!r}, capability_id={self.capability_id!r}, "
            f"kind={self.kind!r}, "
            f"expires_at={self.expires_at!r})"
        )


class ManagedConsentResolutionResult:
    """Sealed authority-free result of one signed-consent continuation."""

    __slots__ = (
        "actions",
        "capability_id",
        "challenge_digest",
        "journal_record_digest",
        "journal_revision",
        "kind",
        "next_challenge",
        "outcome",
        "reason_code",
    )

    challenge_digest: str
    capability_id: str
    kind: str
    outcome: str
    reason_code: str
    next_challenge: ManagedConsentChallengeProjection | None
    actions: tuple[ManagedActionSummary, ...]
    journal_revision: int
    journal_record_digest: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed consent resolution results are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        challenge: InstallConsentChallenge,
        outcome: str,
        reason_code: str,
        next_challenge: ManagedConsentChallengeProjection | None,
        actions: tuple[ManagedActionSummary, ...],
        journal_revision: int,
        journal_record_digest: str,
    ) -> ManagedConsentResolutionResult:
        if factory_token is not _CONSENT_RESULT_FACTORY_TOKEN:
            raise TypeError("managed consent resolution results are factory-issued only")
        if outcome not in {
            "consent-required",
            "reauthentication-required",
            "denied",
            "installed-inactive",
            "install-failed",
            "install-indeterminate",
            "expired",
            "quarantined",
            "superseded",
        }:
            raise ManagedQueryServiceError("managed consent resolution outcome is invalid")
        if (
            not isinstance(reason_code, str)
            or _WORK_REF_RE.fullmatch(reason_code) is None
            or len(actions) > _MAX_ACTION_SUMMARIES
            or not all(type(action) is ManagedActionSummary for action in actions)
        ):
            raise ManagedQueryServiceError("managed consent resolution projection is invalid")
        if (outcome in {"consent-required", "reauthentication-required"}) != (
            next_challenge is not None
        ):
            raise ManagedQueryServiceError(
                "managed consent resolution next challenge is inconsistent"
            )
        if (
            next_challenge is not None
            and type(next_challenge) is not ManagedConsentChallengeProjection
        ):
            raise ManagedQueryServiceError("managed consent resolution challenge was substituted")
        if (
            outcome == "reauthentication-required"
            and next_challenge is not None
            and next_challenge.challenge_digest != challenge.challenge_digest
        ):
            raise ManagedQueryServiceError(
                "managed consent reauthentication changed challenge identity"
            )
        if next_challenge is not None and (
            next_challenge.capability_id != challenge.capability_id
            or next_challenge.kind != challenge.kind
        ):
            raise ManagedQueryServiceError(
                "managed consent next challenge changed capability identity"
            )
        if type(journal_revision) is not int or journal_revision < 1:
            raise ManagedQueryServiceError("managed consent journal revision is invalid")
        if _DIGEST_RE.fullmatch(journal_record_digest) is None:
            raise ManagedQueryServiceError("managed consent journal digest is invalid")
        instance = object.__new__(cls)
        for name, value in (
            ("challenge_digest", challenge.challenge_digest),
            ("capability_id", challenge.capability_id),
            ("kind", challenge.kind),
            ("outcome", outcome),
            ("reason_code", reason_code),
            ("next_challenge", next_challenge),
            ("actions", actions),
            ("journal_revision", journal_revision),
            ("journal_record_digest", journal_record_digest),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed consent resolution results are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed consent resolution results are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed consent resolution results cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed consent resolution results cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed consent resolution results cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed consent resolution results cannot be serialized")

    def __repr__(self) -> str:
        return (
            "ManagedConsentResolutionResult("
            f"challenge_digest={self.challenge_digest!r}, outcome={self.outcome!r})"
        )


class ManagedQueryServiceResult:
    """Sealed public projection of one exactly committed managed decision."""

    __slots__ = ("actions", "challenge", "prepared", "query_ref")

    query_ref: str
    prepared: PreparedManagedQuery
    actions: tuple[ManagedActionSummary, ...]
    challenge: ManagedConsentChallengeProjection | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed query service results are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        query_ref: str,
        prepared: PreparedManagedQuery,
        actions: tuple[ManagedActionSummary, ...],
        challenge: ManagedConsentChallengeProjection | None,
    ) -> ManagedQueryServiceResult:
        if factory_token is not _RESULT_FACTORY_TOKEN:
            raise TypeError("managed query service results are factory-issued only")
        if _QUERY_REF_RE.fullmatch(query_ref) is None:
            raise ManagedQueryServiceError("query result reference is invalid")
        if type(prepared) is not PreparedManagedQuery:
            raise TypeError("query result requires an exact PreparedManagedQuery")
        if len(actions) > _MAX_ACTION_SUMMARIES or not all(
            type(action) is ManagedActionSummary for action in actions
        ):
            raise ManagedQueryServiceError("query result action summary set is invalid")
        if challenge is not None and type(challenge) is not ManagedConsentChallengeProjection:
            raise ManagedQueryServiceError("query result consent challenge is invalid")
        instance = object.__new__(cls)
        object.__setattr__(instance, "query_ref", query_ref)
        object.__setattr__(instance, "prepared", prepared)
        object.__setattr__(instance, "actions", actions)
        object.__setattr__(instance, "challenge", challenge)
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed query service results are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed query service results are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed query service results cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed query service results cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed query service results cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed query service results cannot be serialized")

    def __repr__(self) -> str:
        return (
            "ManagedQueryServiceResult("
            f"query_ref={self.query_ref!r}, decision_digest={self.prepared.decision_digest!r})"
        )


class ManagedDesiredSetResult:
    """Sealed authority-free projection of one durable desired-set reconciliation."""

    __slots__ = (
        "actions",
        "capability_ids",
        "challenge",
        "decision_digest",
        "deferred_capability_ids",
        "desired_set_ref",
        "failed_capability_ids",
        "journal_record_digest",
        "journal_revision",
        "logical_choice_id",
        "plan_id",
        "query_ref",
        "reason_code",
        "status",
    )

    query_ref: str
    desired_set_ref: str
    logical_choice_id: str
    capability_ids: tuple[str, ...]
    deferred_capability_ids: tuple[str, ...]
    failed_capability_ids: tuple[str, ...]
    status: str
    reason_code: str | None
    plan_id: str
    decision_digest: str
    journal_revision: int
    journal_record_digest: str
    actions: tuple[ManagedActionSummary, ...]
    challenge: ManagedConsentChallengeProjection | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed desired-set results are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        record: ManagedDesiredSetRecord,
        deferred_capability_ids: tuple[str, ...],
        failed_capability_ids: tuple[str, ...],
        status: str,
        reason_code: str | None,
        actions: tuple[ManagedActionSummary, ...],
        challenge: ManagedConsentChallengeProjection | None,
    ) -> ManagedDesiredSetResult:
        if factory_token is not _DESIRED_RESULT_FACTORY_TOKEN:
            raise TypeError("managed desired-set results are factory-issued only")
        if type(record) is not ManagedDesiredSetRecord or not record.committed:
            raise TypeError("managed desired-set result requires an exact committed record")
        if status not in {
            "reconciled",
            "consent-required",
            "effect-pending",
            "manual-deferred",
            "lifecycle-deferred",
        }:
            raise ManagedQueryServiceError("managed desired-set result status is invalid")
        deferred = status in {"manual-deferred", "lifecycle-deferred"}
        if deferred != (reason_code is not None):
            raise ManagedQueryServiceError("managed desired-set deferral reason is invalid")
        if (status == "consent-required") != (challenge is not None):
            raise ManagedQueryServiceError(
                "managed desired-set consent status and challenge are inconsistent"
            )
        if deferred and challenge is not None:
            raise ManagedQueryServiceError(
                "managed desired-set deferred result cannot carry a consent challenge"
            )
        if type(deferred_capability_ids) is not tuple:
            raise ManagedQueryServiceError("managed desired-set deferred subset is invalid")
        canonical_deferred = tuple(
            capability_id
            for capability_id in record.capability_ids
            if capability_id in set(deferred_capability_ids)
        )
        if (
            deferred_capability_ids != canonical_deferred
            or len(deferred_capability_ids) != len(set(deferred_capability_ids))
            or deferred != bool(deferred_capability_ids)
        ):
            raise ManagedQueryServiceError("managed desired-set deferred subset is invalid")
        if type(failed_capability_ids) is not tuple:
            raise ManagedQueryServiceError("managed desired-set failed subset is invalid")
        canonical_failed = tuple(
            capability_id
            for capability_id in record.capability_ids
            if capability_id in set(failed_capability_ids)
        )
        if (
            failed_capability_ids != canonical_failed
            or len(failed_capability_ids) != len(set(failed_capability_ids))
            or len(failed_capability_ids) > 5
            or (status == "reconciled" and bool(failed_capability_ids))
        ):
            raise ManagedQueryServiceError("managed desired-set failed subset is invalid")
        if len(actions) > _MAX_ACTION_SUMMARIES or not all(
            type(action) is ManagedActionSummary for action in actions
        ):
            raise ManagedQueryServiceError("managed desired-set actions are invalid")
        if challenge is not None and type(challenge) is not ManagedConsentChallengeProjection:
            raise ManagedQueryServiceError("managed desired-set challenge is invalid")
        if challenge is not None:
            requests = tuple(
                action
                for action in actions
                if action.kind == "RequestConsent" and action.entity_id == challenge.capability_id
            )
            if len(requests) != 1:
                raise ManagedQueryServiceError(
                    "managed desired-set challenge has no exact action summary"
                )
        if (
            record.journal_revision is None
            or record.journal_record_digest is None
            or _DESIRED_SET_REF_RE.fullmatch(record.desired_set_ref) is None
        ):
            raise ManagedQueryServiceError("managed desired-set durable binding is incomplete")
        instance = object.__new__(cls)
        for name, value in (
            ("query_ref", record.query_ref),
            ("desired_set_ref", record.desired_set_ref),
            ("logical_choice_id", record.logical_choice_id),
            ("capability_ids", record.capability_ids),
            ("deferred_capability_ids", deferred_capability_ids),
            ("failed_capability_ids", failed_capability_ids),
            ("status", status),
            ("reason_code", reason_code),
            ("plan_id", record.plan_id),
            ("decision_digest", record.decision_digest),
            ("journal_revision", record.journal_revision),
            ("journal_record_digest", record.journal_record_digest),
            ("actions", actions),
            ("challenge", challenge),
        ):
            object.__setattr__(instance, name, value)
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed desired-set results are immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed desired-set results are immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed desired-set results cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed desired-set results cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed desired-set results cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed desired-set results cannot be serialized")

    def __repr__(self) -> str:
        return (
            "ManagedDesiredSetResult("
            f"desired_set_ref={self.desired_set_ref!r}, status={self.status!r})"
        )


@dataclass(frozen=True, slots=True)
class _ManagedConsentContext:
    """Exact journal, policy, and actuator facts for one broker challenge."""

    parent: ManagedQueryRecord
    desired: ManagedDesiredSetRecord
    consent_head: JournalRecord
    current: EngineSnapshot
    request: HostAction
    install_action: HostAction
    selection: CapabilityPlanSelectionV3
    directive: InstallConsentDirective
    binding: InstallExecutionBinding | None


class ManagedQueryService:
    """Process-bound orchestrator for durable managed-query preparation."""

    __slots__ = (
        "_agent_file_runtime",
        "_benefit_audit_path",
        "_benefit_facts_port",
        "_closed",
        "_consent_broker",
        "_input_authority",
        "_install_bundle_port",
        "_interactive_install_decision_guard",
        "_journal_path",
        "_lifecycle_lock_path",
        "_lock",
        "_material_port",
        "_net_benefit_policy",
        "_owner_pid",
        "_policy_store_root",
        "_query_store",
        "_registry",
        "_skill_cas_runtime",
        "_trusted_utc_now",
        "_verifier_registry",
    )
    _agent_file_runtime: AgentFileRuntimeConfig | None
    _benefit_audit_path: Path
    _benefit_facts_port: AuthenticatedBenefitFactsPort
    _closed: bool
    _consent_broker: InstallConsentBrokerService | None
    _input_authority: ManagedQueryInputAuthority
    _install_bundle_port: CapabilityInstallBundlePort
    _interactive_install_decision_guard: InteractiveInstallDecisionGuard | None
    _journal_path: Path
    _lifecycle_lock_path: Path
    _lock: RLock
    _material_port: CapabilityMaterialPort
    _net_benefit_policy: NetBenefitPolicy
    _owner_pid: int
    _policy_store_root: Path | None
    _query_store: ManagedQueryStore
    _registry: ManagedArtifactRegistry
    _skill_cas_runtime: SkillCasRuntimeConfig | None
    _trusted_utc_now: Callable[[], datetime] | None
    _verifier_registry: TrustedHumanDecisionVerifierRegistry | None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("managed query services are factory-issued only")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        registry: ManagedArtifactRegistry,
        query_store: ManagedQueryStore,
        journal_path: Path,
        lifecycle_lock_path: Path,
        benefit_audit_path: Path,
        benefit_facts_port: AuthenticatedBenefitFactsPort,
        net_benefit_policy: NetBenefitPolicy,
        material_port: CapabilityMaterialPort,
        install_bundle_port: CapabilityInstallBundlePort,
        input_authority: ManagedQueryInputAuthority,
        consent_broker: InstallConsentBrokerService | None,
        policy_store_root: Path | None,
        interactive_install_decision_guard: InteractiveInstallDecisionGuard | None,
        trusted_utc_now: Callable[[], datetime] | None,
        verifier_registry: TrustedHumanDecisionVerifierRegistry | None,
        skill_cas_runtime: SkillCasRuntimeConfig | None,
        agent_file_runtime: AgentFileRuntimeConfig | None,
    ) -> ManagedQueryService:
        if factory_token is not _SERVICE_FACTORY_TOKEN:
            raise TypeError("managed query services are factory-issued only")
        instance = object.__new__(cls)
        for name, value in (
            ("_registry", registry),
            ("_query_store", query_store),
            ("_journal_path", journal_path),
            ("_lifecycle_lock_path", lifecycle_lock_path),
            ("_benefit_audit_path", benefit_audit_path),
            ("_benefit_facts_port", benefit_facts_port),
            ("_net_benefit_policy", net_benefit_policy),
            ("_material_port", material_port),
            ("_install_bundle_port", install_bundle_port),
            ("_input_authority", input_authority),
            ("_consent_broker", consent_broker),
            ("_policy_store_root", policy_store_root),
            ("_interactive_install_decision_guard", interactive_install_decision_guard),
            ("_trusted_utc_now", trusted_utc_now),
            ("_verifier_registry", verifier_registry),
            ("_skill_cas_runtime", skill_cas_runtime),
            ("_agent_file_runtime", agent_file_runtime),
        ):
            object.__setattr__(instance, name, value)
        object.__setattr__(instance, "_closed", False)
        object.__setattr__(instance, "_lock", RLock())
        object.__setattr__(instance, "_owner_pid", os.getpid())
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed query service authority is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed query service authority is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed query service authority cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed query service authority cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed query service authority cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed query service authority cannot be serialized")

    def _assert_owner_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("managed query service cannot be used from a forked process")

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("managed query service is closed")

    @property
    def closed(self) -> bool:
        self._assert_owner_process()
        with self._lock:
            return self._closed

    def prepare(self, request: ManagedQueryRequest) -> ManagedQueryServiceResult:
        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            if type(request) is not ManagedQueryRequest:
                raise TypeError("request must be an exact ManagedQueryRequest")
            with secure_file_lock(self._lifecycle_lock_path):
                return self._prepare_locked(request)

    def _prepare_locked(self, request: ManagedQueryRequest) -> ManagedQueryServiceResult:
        supplied = self._input_authority.resolve(request.current_work_ref)
        self._validate_input(supplied)
        try:
            pending_desired = self._query_store.load_pending_desired_set(
                supplied.decision_event.scope
            )
        except ManagedQueryStoreNotFound:
            pending_desired = None
        if pending_desired is not None:
            raise ManagedDesiredSetBusyError(
                "a pending desired choice must be recovered before managed planning"
            )
        artifact = supplied.artifact
        plan_id = supplied.decision_event.correlation_id
        if plan_id is None:
            raise ManagedQueryServiceError("managed decision has no plan identity")
        try:
            existing_plan = self._query_store.load_by_scope_and_plan(
                supplied.decision_event.scope,
                plan_id,
            )
        except ManagedQueryStoreNotFound:
            pass
        else:
            if existing_plan.logical_query_id != request.logical_query_id:
                raise ManagedQueryStoreConflict(
                    "managed query scope and plan already bind another logical identity"
                )
        record = self._query_store.register(
            logical_query_id=request.logical_query_id,
            session_started=supplied.session_started,
            decision_event=supplied.decision_event,
            artifact_manifest_digest=artifact.manifest_digest,
            planning_environment_digest=artifact.planning_environment_digest,
        )
        self._validate_record(record, supplied)
        return self._advance_record(record, artifact)

    def reopen(self, opaque_query_ref: str) -> ManagedQueryServiceResult:
        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            if (
                not isinstance(opaque_query_ref, str)
                or _QUERY_REF_RE.fullmatch(opaque_query_ref) is None
            ):
                raise ValueError("opaque_query_ref must be an opaque managed-query reference")
            with secure_file_lock(self._lifecycle_lock_path):
                return self._reopen_locked(opaque_query_ref)

    def _reopen_locked(self, opaque_query_ref: str) -> ManagedQueryServiceResult:
        record = self._query_store.load(opaque_query_ref)
        artifact = self._registry.reopen(
            manifest_digest=record.artifact_manifest_digest,
            planning_environment_digest=record.planning_environment_digest,
        )
        value = ManagedQueryInput(
            artifact=artifact,
            session_started=record.session_started,
            decision_event=record.decision_event,
        )
        self._validate_input(value)
        self._validate_record(record, value)
        return self._advance_record(record, artifact)

    def set_desired(self, request: ManagedDesiredSetRequest) -> ManagedDesiredSetResult:
        """Durably reconcile one exact subset of an already committed plan."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            if type(request) is not ManagedDesiredSetRequest:
                raise TypeError("request must be an exact ManagedDesiredSetRequest")
            with secure_file_lock(self._lifecycle_lock_path):
                return self._set_desired_locked(request)

    def resolve_consent(self, assertion_payload: bytes) -> ManagedConsentResolutionResult:
        """Authenticate one fresh signed decision and continue its exact install."""

        self._assert_owner_process()
        if type(assertion_payload) is not bytes:
            raise TypeError("assertion_payload must be canonical signed assertion bytes")
        assertion = decode_signed_human_decision_assertion(assertion_payload)
        base_broker = self._require_consent_broker()
        if assertion.audience != base_broker.audience:
            raise ManagedQueryServiceError(
                "signed assertion audience does not match managed consent"
            )
        verifier_registry = self._verifier_registry
        if verifier_registry is None:
            raise ManagedQueryServiceError(
                "signed managed consent requires a captured trusted verifier registry"
            )
        verifier = verifier_registry.resolve(assertion)
        broker = self._consent_broker_for_verifier(base_broker, verifier)
        with self._lock:
            self._assert_open()
            with secure_file_lock(self._lifecycle_lock_path):
                record = broker.status_by_challenge_digest(assertion.challenge_digest)
                if record.state not in {"pending", "reauthentication-required"}:
                    raise ManagedQueryServiceError(
                        "signed consent is not awaiting fresh authentication; recover it first"
                    )
                return self._resolve_consent_locked(broker, record, assertion)

    def recover_consent(self, challenge_digest: str) -> ManagedConsentResolutionResult:
        """Reconcile durable decision evidence without authenticating a human."""

        self._assert_owner_process()
        if not isinstance(challenge_digest, str) or _DIGEST_RE.fullmatch(challenge_digest) is None:
            raise ValueError("challenge_digest must be a lowercase SHA-256 digest")
        with self._lock:
            self._assert_open()
            with secure_file_lock(self._lifecycle_lock_path):
                broker = self._require_consent_broker()
                record = broker.status_by_challenge_digest(challenge_digest)
                return self._recover_consent_locked(record)

    def _require_consent_broker(self) -> InstallConsentBrokerService:
        broker = self._consent_broker
        if broker is None:
            raise ManagedQueryServiceError(
                "managed consent continuation requires captured broker authorities"
            )
        return broker

    @staticmethod
    def _consent_broker_for_verifier(
        broker: InstallConsentBrokerService,
        verifier: HumanDecisionVerifier,
    ) -> InstallConsentBrokerService:
        if type(broker) is not InstallConsentBrokerService:
            raise TypeError("managed consent broker was substituted")
        return broker.with_verifier(verifier)

    def _consent_broker_for_recovery(
        self,
        broker: InstallConsentBrokerService,
    ) -> InstallConsentBrokerService:
        if type(broker) is not InstallConsentBrokerService:
            raise TypeError("managed consent broker was substituted")
        provider = broker._evidence_provider  # noqa: SLF001 - trusted lazy composition.
        if provider is not None:
            return broker
        return broker.with_evidence_provider(SQLiteEngineStore(self._journal_path))

    def _resolve_consent_locked(
        self,
        broker: InstallConsentBrokerService,
        broker_record: InstallConsentChallengeRecord,
        assertion: SignedHumanDecisionAssertion,
    ) -> ManagedConsentResolutionResult:
        challenge = broker_record.challenge
        parent, desired, artifact = self._consent_owner(challenge)
        if not has_persisted_install_policy(self._policy_store_root):
            raise ManagedQueryServiceError(
                "signed managed consent requires an explicitly persisted install policy"
            )
        policy = load_current_install_policy(self._policy_store_root)
        if policy.policy_digest != challenge.policy_snapshot_digest:
            raise ManagedQueryHeadDriftError(
                "install policy changed after the consent challenge was published"
            )
        active_guard: list[InteractiveInstallDecisionGuard | None] = [None]

        def guarded(
            reservation: InteractiveInstallDecisionReservation,
        ) -> AbstractContextManager[None]:
            guard = active_guard[0]
            if guard is None:
                raise ManagedQueryServiceError(
                    "interactive decision guard was used before authentication"
                )
            return guard(reservation)

        with hold_current_install_policy(
            challenge.policy_snapshot_digest,
            root=self._policy_store_root,
        ) as held_policy:
            with self._open_consent_composition(
                artifact,
                interactive_guard=guarded,
            ) as composition:
                context = self._managed_consent_context(
                    composition,
                    challenge=challenge,
                    parent=parent,
                    desired=desired,
                    policy=held_policy.policy,
                    require_pending_head=True,
                )
                held_policy.assert_current()
                authorization = broker.authenticate(challenge, assertion)
                refreshed = self._managed_consent_context(
                    composition,
                    challenge=challenge,
                    parent=parent,
                    desired=desired,
                    policy=held_policy.policy,
                    require_pending_head=True,
                )
                if refreshed != context:
                    raise ManagedQueryHeadDriftError(
                        "managed consent authority changed after human authentication"
                    )
                held_policy.assert_current()
                context = refreshed
                binding = self._require_context_binding(context)
                active_guard[0] = broker.interactive_guard(
                    authorization,
                    execution_binding=binding,
                )
                ready = broker.status(challenge.challenge_id)
                event = self._interactive_consent_event(context, ready)
                transition = composition.process(event)
                self._validate_interactive_decision_transition(
                    transition,
                    event=event,
                    install_action=context.install_action,
                    decision=ready.decision,
                )
                held_policy.assert_current()
                settled = broker.status(challenge.challenge_id)
                if settled.state != "settled":
                    raise ManagedQueryServiceError(
                        "interactive consent commit did not settle its one-shot broker authority"
                    )
                return self._finish_settled_consent(
                    composition,
                    context=context,
                    broker_record=settled,
                    event=event,
                )

    def _recover_consent_locked(
        self,
        broker_record: InstallConsentChallengeRecord,
    ) -> ManagedConsentResolutionResult:
        challenge = broker_record.challenge
        parent, desired, artifact = self._consent_owner(challenge)
        authority_free = broker_record.state != "pending"
        policy = None if authority_free else load_current_install_policy(self._policy_store_root)
        with self._open_consent_composition(
            artifact,
            include_actuators=not authority_free,
        ) as composition:
            try:
                context = self._managed_consent_context(
                    composition,
                    challenge=challenge,
                    parent=parent,
                    desired=desired,
                    policy=policy,
                    require_pending_head=False,
                    require_current_policy=(broker_record.state == "pending"),
                    resolve_execution_binding=not authority_free,
                )
            except ManagedQuerySupersededError:
                return self._consent_resolution_result(
                    composition.snapshot(challenge.scope),
                    challenge=challenge,
                    outcome="superseded",
                    reason_code="desired-install-authority-was-replaced",
                    next_challenge=None,
                )
            if broker_record.state == "pending":
                self._require_pending_consent_head(context)
                return self._consent_resolution_result(
                    context.current,
                    challenge=challenge,
                    outcome="consent-required",
                    reason_code="fresh-human-decision-required",
                    next_challenge=self._challenge_projection(challenge),
                )
            if broker_record.decision is None:
                if broker_record.state == "expired":
                    return self._expire_managed_consent(
                        composition,
                        context=context,
                        challenge=challenge,
                    )
                raise ManagedQueryServiceError(
                    "recoverable consent state has no authenticated decision identity"
                )
            event = self._interactive_consent_event(context, broker_record)
            reservation = self._interactive_reservation(
                event=event,
                directive=context.directive,
                install_action=context.install_action,
            )
            query = self._install_decision_evidence_query(
                consent_head=context.consent_head,
                reservation=reservation,
            )
            recovery_broker = self._consent_broker_for_recovery(self._require_consent_broker())
            reconciliation = recovery_broker.reconcile_install_decision(
                query=query,
                reservation=reservation,
            )
            if reconciliation.outcome == "quarantined":
                return self._consent_resolution_result(
                    composition.snapshot(challenge.scope),
                    challenge=challenge,
                    outcome="quarantined",
                    reason_code=f"journal-{reconciliation.journal_status}",
                    next_challenge=None,
                )
            if reconciliation.outcome == "expired":
                return self._expire_managed_consent(
                    composition,
                    context=context,
                    challenge=challenge,
                )
            if reconciliation.outcome == "reauthentication-required":
                self._require_pending_consent_head(context)
                return self._consent_resolution_result(
                    composition.snapshot(challenge.scope),
                    challenge=challenge,
                    outcome="reauthentication-required",
                    reason_code="fresh-human-reauthentication-required",
                    next_challenge=self._challenge_projection(challenge),
                )
            if reconciliation.outcome != "settled":
                raise ManagedQueryServiceError("consent reconciliation returned an invalid state")
            if context.binding is None and reconciliation.record.decision == "granted":
                retired = self._retire_expired_unclaimed_install(
                    composition,
                    context=context,
                    challenge=challenge,
                )
                if retired is not None:
                    return retired
                current = composition.snapshot(challenge.scope)
                if not self._has_exact_pending_install_effect(
                    current,
                    context.install_action,
                ):
                    return self._finish_settled_consent(
                        composition,
                        context=context,
                        broker_record=reconciliation.record,
                        event=event,
                    )
                with self._open_consent_composition(artifact) as live_composition:
                    live_context = self._managed_consent_context(
                        live_composition,
                        challenge=challenge,
                        parent=parent,
                        desired=desired,
                        policy=None,
                        require_pending_head=False,
                        require_current_policy=False,
                    )
                    return self._finish_settled_consent(
                        live_composition,
                        context=live_context,
                        broker_record=reconciliation.record,
                        event=event,
                    )
            return self._finish_settled_consent(
                composition,
                context=context,
                broker_record=reconciliation.record,
                event=event,
            )

    def _consent_owner(
        self,
        challenge: InstallConsentChallenge,
    ) -> tuple[ManagedQueryRecord, ManagedDesiredSetRecord, ManagedArtifactHandle]:
        try:
            parent = self._query_store.load_by_scope_and_plan(
                challenge.scope,
                challenge.plan_id,
            )
            desired = self._query_store.load_latest_desired_set(parent.query_ref)
        except ManagedQueryStoreNotFound as exc:
            raise ManagedQueryServiceError(
                "install consent has no exact managed query owner"
            ) from exc
        if not parent.planned or parent.plan_id != challenge.plan_id:
            raise ManagedQueryServiceError(
                "install consent managed query owner is not exactly planned"
            )
        if (
            not desired.committed
            or desired.query_ref != parent.query_ref
            or desired.plan_id != challenge.plan_id
        ):
            raise ManagedQuerySupersededError(
                "install consent no longer belongs to the current desired plan"
            )
        artifact = self._registry.reopen(
            manifest_digest=parent.artifact_manifest_digest,
            planning_environment_digest=parent.planning_environment_digest,
        )
        return parent, desired, artifact

    def _open_consent_composition(
        self,
        artifact: ManagedArtifactHandle,
        *,
        interactive_guard: InteractiveInstallDecisionGuard | None = None,
        include_actuators: bool = True,
    ) -> EngineComposition:
        return open_managed_engine_composition(
            registry=self._registry,
            artifact=artifact,
            journal_path=self._journal_path,
            benefit_audit_path=self._benefit_audit_path,
            benefit_facts_port=self._benefit_facts_port,
            net_benefit_policy=self._net_benefit_policy,
            material_port=self._material_port,
            install_bundle_port=self._install_bundle_port,
            policy_store_root=self._policy_store_root,
            interactive_install_decision_guard=interactive_guard,
            trusted_utc_now=self._trusted_utc_now,
            skill_cas_runtime=self._skill_cas_runtime if include_actuators else None,
            agent_file_runtime=self._agent_file_runtime if include_actuators else None,
        )

    def _managed_consent_context(
        self,
        composition: EngineComposition,
        *,
        challenge: InstallConsentChallenge,
        parent: ManagedQueryRecord,
        desired: ManagedDesiredSetRecord,
        policy: InstallConsentPolicy | None,
        require_pending_head: bool,
        require_current_policy: bool = True,
        resolve_execution_binding: bool = True,
    ) -> _ManagedConsentContext:
        current = composition.snapshot(challenge.scope)
        committed = self._exact_parent_plan(current, parent)
        if challenge.capability_id not in desired.capability_ids:
            raise ManagedQuerySupersededError(
                "install consent capability is no longer in the desired subset"
            )
        consent_head = self._journal_record_at_revision(
            challenge.scope,
            challenge.requested_action_precondition_revision - 1,
        )
        historical = EngineState.from_json(consent_head.result_state_json)
        historical_plan = historical.committed_plan
        if (
            historical.scope != challenge.scope
            or not isinstance(historical_plan, CommittedPlanV3)
            or historical_plan.plan_id != challenge.plan_id
            or committed.plan_id != challenge.plan_id
            or len(historical.pending_consents) != 1
        ):
            raise ManagedQueryHeadDriftError(
                "install consent historical head no longer binds the managed plan"
            )
        pending = historical.pending_consents[0]
        install_action = pending.install_action
        if (
            pending.consent_id != challenge.challenge_id
            or install_action.consent_id != challenge.challenge_id
            or install_action.action_id != challenge.requested_action_id
            or install_action.content_digest != challenge.requested_action_content_digest
            or install_action.precondition_revision
            != challenge.requested_action_precondition_revision
        ):
            raise ManagedQueryHeadDriftError(
                "install consent historical action identity was substituted"
            )
        capability = historical.capability(challenge.capability_id)
        if type(capability) is not CapabilityStateV3:
            raise ManagedQueryServiceError(
                "install consent historical capability authority is unavailable"
            )
        selection = capability.selection.selection
        authority = selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            raise ManagedQueryServiceError(
                "install consent historical selection has no install authority"
            )
        request = self._consent_request_from_record(
            consent_head,
            install_action=install_action,
        )
        directive = self._interactive_directive_from_challenge(
            challenge,
            selection=selection,
            install_action=install_action,
        )
        if require_current_policy:
            if policy is None:
                raise ManagedQueryServiceError("current install policy authority is unavailable")
            if policy.policy_digest != challenge.policy_snapshot_digest:
                raise ManagedQueryHeadDriftError(
                    "install policy changed after consent challenge publication"
                )
            routed = route_install_consent_request(
                request,
                selection,
                authority.descriptor,
                policy,
            )
            if routed != directive:
                raise ManagedQueryHeadDriftError(
                    "current install policy no longer produces the exact consent directive"
                )
        self._validate_historical_consent_challenge(
            challenge,
            directive=directive,
            selection=selection,
            install_action=install_action,
        )
        binding: InstallExecutionBinding | None = None
        if resolve_execution_binding:
            binding = composition.resolve_install_execution_binding(install_action, selection)
            self._validate_install_binding(
                binding,
                install_action=install_action,
                selection=selection,
            )
            expected_challenge = derive_install_consent_challenge(
                directive=directive,
                selection=selection,
                install_action=install_action,
                execution_binding=binding,
                workspace_identity_digest=challenge.workspace_identity_digest,
                release_root_digest=challenge.release_root_digest,
                audience=challenge.audience,
            )
            if expected_challenge != challenge:
                raise ManagedQueryHeadDriftError(
                    "current install target or historical authority no longer matches consent"
                )
        context = _ManagedConsentContext(
            parent=parent,
            desired=desired,
            consent_head=consent_head,
            current=current,
            request=request,
            install_action=install_action,
            selection=selection,
            directive=directive,
            binding=binding,
        )
        if require_pending_head:
            self._require_pending_consent_head(context)
        return context

    @staticmethod
    def _interactive_directive_from_challenge(
        challenge: InstallConsentChallenge,
        *,
        selection: CapabilityPlanSelectionV3,
        install_action: HostAction,
    ) -> InstallConsentDirective:
        authority = selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            raise ManagedQueryServiceError("interactive consent lost install authority")
        descriptor = authority.descriptor
        if descriptor.permission_expansion:
            reason_code = "permission-expansion-requires-consent"
        elif descriptor.credential_requirement:
            reason_code = "credentials-require-consent"
        else:
            reason_code = "per-install-consent-required"
        return InstallConsentDirective(
            consent_id=challenge.challenge_id,
            capability_id=selection.presentation.capability_id,
            kind=selection.presentation.kind,
            source_digest=selection.presentation.source_digest,
            catalog_snapshot_digest=install_action.catalog_snapshot_id or "",
            plan_id=install_action.plan_id or "",
            install_plan_digest=descriptor.plan_digest,
            descriptor_digest=descriptor.descriptor_digest,
            installer_id=descriptor.installer_id,
            provenance_digest=descriptor.provenance_digest,
            permission_expansion=descriptor.permission_expansion,
            credential_requirement=descriptor.credential_requirement,
            decision_basis="interactive",
            policy_snapshot_digest=challenge.policy_snapshot_digest,
            reason_code=reason_code,
            requested_action_id=install_action.action_id,
            requested_action_kind=install_action.kind,
            requested_action_content_digest=install_action.content_digest,
            requested_action_precondition_revision=install_action.precondition_revision,
            result_material_identity_digest=authority.result_material.identity_digest,
        )

    @staticmethod
    def _validate_historical_consent_challenge(
        challenge: InstallConsentChallenge,
        *,
        directive: InstallConsentDirective,
        selection: CapabilityPlanSelectionV3,
        install_action: HostAction,
    ) -> None:
        authority = selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            raise ManagedQueryServiceError("historical consent lost install authority")
        expected = (
            directive.consent_id,
            directive.capability_id,
            directive.kind,
            directive.source_digest,
            directive.catalog_snapshot_digest,
            directive.plan_id,
            directive.install_plan_digest,
            directive.descriptor_digest,
            directive.policy_snapshot_digest,
            directive.requested_action_id,
            directive.requested_action_kind,
            directive.requested_action_content_digest,
            directive.requested_action_precondition_revision,
            directive.result_material_identity_digest,
            install_consent_selection_digest(selection),
            install_action.expires_at,
        )
        actual = (
            challenge.challenge_id,
            challenge.capability_id,
            challenge.kind,
            challenge.source_digest,
            challenge.catalog_snapshot_digest,
            challenge.plan_id,
            challenge.install_plan_digest,
            challenge.descriptor_digest,
            challenge.policy_snapshot_digest,
            challenge.requested_action_id,
            challenge.requested_action_kind,
            challenge.requested_action_content_digest,
            challenge.requested_action_precondition_revision,
            challenge.material_identity_digest,
            challenge.selection_digest,
            challenge.expires_at,
        )
        if expected != actual:
            raise ManagedQueryHeadDriftError(
                "historical consent challenge no longer matches its exact journal authority"
            )

    @staticmethod
    def _require_pending_consent_head(context: _ManagedConsentContext) -> None:
        state = context.current.state
        if (
            state is None
            or context.current.revision != context.consent_head.revision
            or context.current.record_digest != context.consent_head.record_digest
            or len(state.pending_consents) != 1
            or state.pending_consents[0].install_action != context.install_action
        ):
            raise ManagedQueryHeadDriftError(
                "install consent is not the exact current pending journal head"
            )

    @staticmethod
    def _require_context_binding(
        context: _ManagedConsentContext,
    ) -> InstallExecutionBinding:
        binding = context.binding
        if type(binding) is not InstallExecutionBinding:
            raise ManagedQueryServiceError(
                "managed consent operation requires a current execution binding"
            )
        return binding

    def _expire_managed_consent(
        self,
        composition: EngineComposition,
        *,
        context: _ManagedConsentContext,
        challenge: InstallConsentChallenge,
    ) -> ManagedConsentResolutionResult:
        """Commit one exact machine expiry or classify later authority safely."""

        event = self._install_consent_expired_event(context, challenge=challenge)
        current = composition.snapshot(challenge.scope)
        if (
            current.revision == context.consent_head.revision
            and current.record_digest == context.consent_head.record_digest
        ):
            current_context = _ManagedConsentContext(
                parent=context.parent,
                desired=context.desired,
                consent_head=context.consent_head,
                current=current,
                request=context.request,
                install_action=context.install_action,
                selection=context.selection,
                directive=context.directive,
                binding=context.binding,
            )
            self._require_pending_consent_head(current_context)
            try:
                transition = composition.process(event)
            except EventIdCollision:
                return self._consent_resolution_result(
                    composition.snapshot(challenge.scope),
                    challenge=challenge,
                    outcome="quarantined",
                    reason_code="journal-event-collision",
                    next_challenge=None,
                )
            except RevisionConflict:
                current = composition.snapshot(challenge.scope)
            else:
                self._validate_install_consent_expiry_transition(
                    transition,
                    event=event,
                    install_action=context.install_action,
                )
                current = composition.snapshot(challenge.scope)

        expiry_revision = event.expected_revision + 1
        if current.revision >= expiry_revision:
            expiry_record = self._journal_record_at_revision(
                challenge.scope,
                expiry_revision,
            )
            committed = ReplayInput.from_json(expiry_record.replay_json).reducer_event
            if (
                committed.to_json() == event.to_json()
                and expiry_record.event_id == event.event_id
                and expiry_record.event_content_digest == event.content_digest
            ):
                transition = Transition.from_json(expiry_record.transition_json)
                self._validate_install_consent_expiry_transition(
                    transition,
                    event=event,
                    install_action=context.install_action,
                )
                current = composition.snapshot(challenge.scope)
                if self._has_exact_pending_install(current, context.install_action):
                    raise ManagedQueryServiceError(
                        "committed consent expiry retained its exact pending authority"
                    )
                return self._consent_resolution_result(
                    current,
                    challenge=challenge,
                    outcome="expired",
                    reason_code="install-consent-challenge-expired",
                    next_challenge=None,
                )
            if self._has_exact_pending_install(current, context.install_action):
                return self._consent_resolution_result(
                    current,
                    challenge=challenge,
                    outcome="quarantined",
                    reason_code="expired-consent-head-is-ambiguous",
                    next_challenge=None,
                )
            return self._consent_resolution_result(
                current,
                challenge=challenge,
                outcome="superseded",
                reason_code="expired-consent-authority-was-refreshed",
                next_challenge=None,
            )
        if current.revision == context.consent_head.revision:
            return self._consent_resolution_result(
                current,
                challenge=challenge,
                outcome="quarantined",
                reason_code="expired-consent-head-is-ambiguous",
                next_challenge=None,
            )
        raise ManagedQueryServiceError("managed consent expiry did not reach a durable outcome")

    @staticmethod
    def _has_exact_pending_install(
        snapshot: EngineSnapshot,
        install_action: HostAction,
    ) -> bool:
        state = snapshot.state
        return bool(
            state is not None
            and any(
                pending.consent_id == install_action.consent_id
                and pending.install_action == install_action
                for pending in state.pending_consents
            )
        )

    @staticmethod
    def _install_consent_expired_event(
        context: _ManagedConsentContext,
        *,
        challenge: InstallConsentChallenge,
    ) -> EngineEvent:
        install_action = context.install_action
        if install_action.expires_at != challenge.expires_at:
            raise ManagedQueryServiceError(
                "broker expiry does not match the exact pending install expiry"
            )
        payload: dict[str, str | int] = {
            "consent_id": challenge.challenge_id,
            "install_expires_at": challenge.expires_at,
            "policy_snapshot_digest": challenge.policy_snapshot_digest,
            "requested_action_id": challenge.requested_action_id,
            "requested_action_kind": challenge.requested_action_kind,
            "requested_action_content_digest": challenge.requested_action_content_digest,
            "requested_action_precondition_revision": (
                challenge.requested_action_precondition_revision
            ),
        }
        digest = _stable_digest(
            {
                "challenge_digest": challenge.challenge_digest,
                "consent_head_event_id": context.consent_head.event_id,
                "consent_head_record_digest": context.consent_head.record_digest,
                "consent_head_revision": context.consent_head.revision,
                "payload": payload,
                "schema": "ctx.managed-install-consent-expiry.v1",
            }
        )
        source = context.desired.event
        return EngineEvent(
            protocol_version=source.protocol_version,
            event_id=f"ctx-install-consent-expired:{digest}",
            kind="InstallConsentExpired",
            scope=source.scope,
            expected_revision=context.consent_head.revision,
            occurred_at=challenge.expires_at,
            payload=payload,
            privacy=source.privacy,
            correlation_id=source.correlation_id,
            causation_id=context.request.action_id,
            engine_version=source.engine_version,
            planner_version=source.planner_version,
            policy_version=source.policy_version,
            host_descriptor_digest=source.host_descriptor_digest,
            catalog_snapshot_digest=source.catalog_snapshot_digest,
            semantic_model_digest=source.semantic_model_digest,
            semantic_index_digest=source.semantic_index_digest,
            work_signature=source.work_signature,
            random_seed=source.random_seed,
        )

    @staticmethod
    def _validate_install_consent_expiry_transition(
        transition: Transition,
        *,
        event: EngineEvent,
        install_action: HostAction,
    ) -> None:
        if (
            type(transition) is not Transition
            or transition.event_id != event.event_id
            or transition.scope != event.scope
            or transition.from_revision != event.expected_revision
            or transition.to_revision != event.expected_revision + 1
            or transition.actions
            or not any(
                diagnostic.get("code") == "install_consent_expired"
                and diagnostic.get("consent_id") == install_action.consent_id
                and diagnostic.get("capability_id") == install_action.entity_id
                for diagnostic in transition.diagnostics
            )
        ):
            raise ManagedQueryServiceError(
                "managed install consent expiry transition was substituted"
            )

    def _interactive_consent_event(
        self,
        context: _ManagedConsentContext,
        broker_record: InstallConsentChallengeRecord,
    ) -> EngineEvent:
        decision = broker_record.decision
        if (
            decision not in {"granted", "denied"}
            or broker_record.principal_digest is None
            or broker_record.authenticator_id is None
            or broker_record.audience is None
            or broker_record.assertion_nonce_digest is None
            or broker_record.decision_issued_at is None
            or broker_record.decision_expires_at is None
            or broker_record.challenge.challenge_id != context.directive.consent_id
        ):
            raise ManagedQueryServiceError(
                "authenticated consent record lacks its exact durable decision identity"
            )
        payload = context.directive.decision_payload(decision)
        source = context.desired.event
        digest = _stable_digest(
            {
                "assertion_nonce_digest": broker_record.assertion_nonce_digest,
                "audience": broker_record.audience,
                "authenticator_id": broker_record.authenticator_id,
                "binding_digest": broker_record.challenge.execution_binding_digest,
                "challenge_digest": broker_record.challenge.challenge_digest,
                "decision_expires_at": broker_record.decision_expires_at,
                "decision_issued_at": broker_record.decision_issued_at,
                "desired_event_content_digest": source.content_digest,
                "desired_event_id": source.event_id,
                "desired_journal_record_digest": context.desired.journal_record_digest,
                "desired_journal_revision": context.desired.journal_revision,
                "desired_set_ref": context.desired.desired_set_ref,
                "head_event_id": context.consent_head.event_id,
                "head_record_digest": context.consent_head.record_digest,
                "head_revision": context.consent_head.revision,
                "install_action_content_digest": context.install_action.content_digest,
                "install_action_id": context.install_action.action_id,
                "payload": payload,
                "principal_digest": broker_record.principal_digest,
                "request_action_content_digest": context.request.content_digest,
                "request_action_id": context.request.action_id,
                "schema": "ctx.managed-interactive-install-decision.v1",
            }
        )
        return EngineEvent(
            protocol_version=source.protocol_version,
            event_id=f"ctx-interactive-install:{digest}",
            kind="UserDecision",
            scope=source.scope,
            expected_revision=context.consent_head.revision,
            occurred_at=broker_record.decision_issued_at,
            payload=payload,
            privacy=source.privacy,
            correlation_id=source.correlation_id,
            causation_id=context.request.action_id,
            engine_version=source.engine_version,
            planner_version=source.planner_version,
            policy_version=source.policy_version,
            host_descriptor_digest=source.host_descriptor_digest,
            catalog_snapshot_digest=source.catalog_snapshot_digest,
            semantic_model_digest=source.semantic_model_digest,
            semantic_index_digest=source.semantic_index_digest,
            work_signature=source.work_signature,
            random_seed=source.random_seed,
        )

    @staticmethod
    def _interactive_reservation(
        *,
        event: EngineEvent,
        directive: InstallConsentDirective,
        install_action: HostAction,
    ) -> InteractiveInstallDecisionReservation:
        decision = event.payload.get("decision")
        if (
            decision not in {"granted", "denied"}
            or dict(event.payload) != directive.decision_payload(decision)
            or event.expected_revision + 1 != install_action.precondition_revision
            or install_action.expires_at is None
        ):
            raise ManagedQueryServiceError(
                "interactive decision lost its exact broker reservation binding"
            )
        return InteractiveInstallDecisionReservation(
            scope=event.scope,
            event_id=event.event_id,
            event_content_digest=event.content_digest,
            consent_id=directive.consent_id,
            decision=decision,
            policy_snapshot_digest=directive.policy_snapshot_digest,
            requested_action_id=directive.requested_action_id,
            requested_action_kind=directive.requested_action_kind,
            requested_action_content_digest=directive.requested_action_content_digest,
            requested_action_precondition_revision=(
                directive.requested_action_precondition_revision
            ),
            install_expires_at=install_action.expires_at,
        )

    @staticmethod
    def _install_decision_evidence_query(
        *,
        consent_head: JournalRecord,
        reservation: InteractiveInstallDecisionReservation,
    ) -> InstallDecisionEvidenceQuery:
        if (
            consent_head.stream_id != StreamId.from_scope(reservation.scope)
            or consent_head.revision != reservation.requested_action_precondition_revision - 1
        ):
            raise ManagedQueryServiceError(
                "interactive decision evidence lost its exact consent head"
            )
        return InstallDecisionEvidenceQuery(
            scope=reservation.scope,
            consent_id=reservation.consent_id,
            decision=reservation.decision,
            decision_basis="interactive",
            policy_snapshot_digest=reservation.policy_snapshot_digest,
            requested_action_id=reservation.requested_action_id,
            requested_action_kind=reservation.requested_action_kind,
            requested_action_content_digest=reservation.requested_action_content_digest,
            requested_action_precondition_revision=(
                reservation.requested_action_precondition_revision
            ),
            event_id=reservation.event_id,
            event_content_digest=reservation.event_content_digest,
            expected_head_revision=consent_head.revision,
            expected_head_record_digest=consent_head.record_digest,
        )

    @staticmethod
    def _validate_interactive_decision_transition(
        transition: Transition,
        *,
        event: EngineEvent,
        install_action: HostAction,
        decision: str | None,
    ) -> None:
        expected_actions = (install_action,) if decision == "granted" else ()
        if (
            type(transition) is not Transition
            or transition.event_id != event.event_id
            or transition.scope != event.scope
            or transition.from_revision != event.expected_revision
            or transition.to_revision != event.expected_revision + 1
            or transition.actions != expected_actions
        ):
            raise ManagedQueryServiceError(
                "interactive consent decision transition was substituted"
            )

    def _finish_settled_consent(
        self,
        composition: EngineComposition,
        *,
        context: _ManagedConsentContext,
        broker_record: InstallConsentChallengeRecord,
        event: EngineEvent,
    ) -> ManagedConsentResolutionResult:
        challenge = broker_record.challenge
        reservation = self._interactive_reservation(
            event=event,
            directive=context.directive,
            install_action=context.install_action,
        )
        if (
            broker_record.state != "settled"
            or broker_record.decision != event.payload.get("decision")
            or broker_record.reservation_event_id != reservation.event_id
            or broker_record.reservation_event_content_digest != reservation.event_content_digest
        ):
            raise ManagedQueryServiceError(
                "settled broker consent does not match the committed decision"
            )
        decision_record = self._journal_record_at_revision(
            challenge.scope,
            context.install_action.precondition_revision,
        )
        committed_event = ReplayInput.from_json(decision_record.replay_json).reducer_event
        if (
            committed_event.to_json() != event.to_json()
            or decision_record.event_id != event.event_id
            or decision_record.event_content_digest != event.content_digest
        ):
            raise ManagedQueryServiceError(
                "settled broker consent has no exact committed journal event"
            )
        current = composition.snapshot(challenge.scope)
        self._exact_parent_plan(current, context.parent)
        if broker_record.decision == "denied":
            state = current.state
            if state is None or any(
                pending.action == context.install_action for pending in state.pending_effects
            ):
                raise ManagedQueryServiceError(
                    "denied install consent retained physical execution authority"
                )
            return self._consent_resolution_result(
                current,
                challenge=challenge,
                outcome="denied",
                reason_code="human-denied-install",
                next_challenge=None,
            )

        state = current.state
        if state is None:
            raise ManagedQueryServiceError("settled install state is unavailable")
        pending = tuple(
            item
            for item in state.pending_effects
            if item.effect == "install" and item.action == context.install_action
        )
        report: InstallExecutionReport | None = None
        if pending:
            if len(pending) != 1 or len(state.pending_effects) != 1:
                raise ManagedQueryServiceError(
                    "interactive physical installation is not globally serialized"
                )
            retired = self._retire_expired_unclaimed_install(
                composition,
                context=context,
                challenge=challenge,
            )
            if retired is not None:
                return retired
            binding = self._require_context_binding(context)
            self._validate_install_binding(
                binding,
                install_action=context.install_action,
                selection=context.selection,
            )
            fresh_binding = composition.resolve_install_execution_binding(
                context.install_action,
                context.selection,
            )
            if (
                fresh_binding != binding
                or fresh_binding.binding_digest != challenge.execution_binding_digest
            ):
                raise ManagedQueryHeadDriftError(
                    "interactive install target changed after decision commit"
                )
            try:
                report = composition.execute_install(
                    context.install_action,
                    context.selection,
                    expected_policy_digest=context.directive.policy_snapshot_digest,
                )
            except InstallActionClaimExpired:
                retired = self._retire_expired_unclaimed_install(
                    composition,
                    context=context,
                    challenge=challenge,
                )
                if retired is not None:
                    return retired
                raise
            if type(report) is not InstallExecutionReport:
                raise TypeError("managed composition returned an invalid install report")
            if report.execution_binding_digest != binding.binding_digest:
                raise ManagedQueryServiceError(
                    "interactive install execution substituted its captured binding"
                )
            current = composition.snapshot(challenge.scope)
            self._validate_install_execution_result(
                current,
                action=context.install_action,
                selection=context.selection,
                report=report,
            )
            if report.outcome == "indeterminate":
                return self._consent_resolution_result(
                    current,
                    challenge=challenge,
                    outcome="install-indeterminate",
                    reason_code="physical-install-outcome-indeterminate",
                    next_challenge=None,
                )
        return self._settled_install_result(
            current,
            challenge=challenge,
            context=context,
            report=report,
        )

    def _retire_expired_unclaimed_install(
        self,
        composition: EngineComposition,
        *,
        context: _ManagedConsentContext,
        challenge: InstallConsentChallenge,
    ) -> ManagedConsentResolutionResult | None:
        """Retire an exact granted action only when it expired before any claim."""

        event = self._install_action_expired_event(context, challenge=challenge)
        current = composition.snapshot(challenge.scope)
        expiry_revision = event.expected_revision + 1

        if current.revision >= expiry_revision:
            expiry_record = self._journal_record_at_revision(
                challenge.scope,
                expiry_revision,
            )
            committed = ReplayInput.from_json(expiry_record.replay_json).reducer_event
            if (
                committed.to_json() == event.to_json()
                and expiry_record.event_id == event.event_id
                and expiry_record.event_content_digest == event.content_digest
            ):
                transition = Transition.from_json(expiry_record.transition_json)
                self._validate_install_action_expiry_transition(
                    transition,
                    event=event,
                    install_action=context.install_action,
                )
                current = composition.snapshot(challenge.scope)
                if self._has_exact_pending_install_effect(current, context.install_action):
                    raise ManagedQueryServiceError(
                        "committed install action expiry retained physical authority"
                    )
                return self._consent_resolution_result(
                    current,
                    challenge=challenge,
                    outcome="expired",
                    reason_code="install-approval-expired-before-claim",
                    next_challenge=None,
                )

        status = SQLiteEngineStore(self._journal_path).install_execution_status(
            StreamId.from_scope(challenge.scope),
            context.install_action.action_id,
        )
        if status.claimed or not self._install_action_has_expired(context.install_action):
            return None
        if (
            current.revision != event.expected_revision
            or current.record_digest
            != self._journal_record_at_revision(
                challenge.scope,
                event.expected_revision,
            ).record_digest
        ):
            if self._has_exact_pending_install_effect(current, context.install_action):
                return self._consent_resolution_result(
                    current,
                    challenge=challenge,
                    outcome="quarantined",
                    reason_code="expired-install-head-is-ambiguous",
                    next_challenge=None,
                )
            return None
        try:
            transition = composition.process(event)
        except InstallActionAlreadyClaimed:
            return None
        except EventIdCollision:
            return self._consent_resolution_result(
                composition.snapshot(challenge.scope),
                challenge=challenge,
                outcome="quarantined",
                reason_code="journal-event-collision",
                next_challenge=None,
            )
        except RevisionConflict:
            current = composition.snapshot(challenge.scope)
            if self._has_exact_pending_install_effect(current, context.install_action):
                return self._consent_resolution_result(
                    current,
                    challenge=challenge,
                    outcome="quarantined",
                    reason_code="expired-install-head-is-ambiguous",
                    next_challenge=None,
                )
            return self._retire_expired_unclaimed_install(
                composition,
                context=context,
                challenge=challenge,
            )
        self._validate_install_action_expiry_transition(
            transition,
            event=event,
            install_action=context.install_action,
        )
        return self._retire_expired_unclaimed_install(
            composition,
            context=context,
            challenge=challenge,
        )

    def _install_action_has_expired(self, action: HostAction) -> bool:
        try:
            now = (
                self._trusted_utc_now() if self._trusted_utc_now is not None else datetime.now(UTC)
            )
        except Exception:
            raise ManagedQueryServiceError("trusted install action clock is unavailable") from None
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ManagedQueryServiceError("trusted install action clock is unavailable")
        try:
            expires_at = datetime.fromisoformat((action.expires_at or "").replace("Z", "+00:00"))
        except ValueError:
            raise ManagedQueryServiceError("managed install action has no valid expiry") from None
        return now.astimezone(UTC) >= expires_at.astimezone(UTC)

    @staticmethod
    def _has_exact_pending_install_effect(
        snapshot: EngineSnapshot,
        install_action: HostAction,
    ) -> bool:
        state = snapshot.state
        return bool(
            state is not None
            and any(
                pending.effect == "install" and pending.action == install_action
                for pending in state.pending_effects
            )
        )

    @staticmethod
    def _install_action_expired_event(
        context: _ManagedConsentContext,
        *,
        challenge: InstallConsentChallenge,
    ) -> EngineEvent:
        action = context.install_action
        if action.expires_at != challenge.expires_at:
            raise ManagedQueryServiceError(
                "settled install expiry lost its exact challenge binding"
            )
        payload: dict[str, str | int] = {
            "action_id": action.action_id,
            "action_kind": action.kind,
            "action_content_digest": action.content_digest,
            "action_precondition_revision": action.precondition_revision,
            "reason": "expired",
        }
        digest = _stable_digest(
            {
                "challenge_digest": challenge.challenge_digest,
                "decision_event_id": context.desired.event.event_id,
                "install_action_content_digest": action.content_digest,
                "payload": payload,
                "schema": "ctx.managed-install-action-expiry.v1",
            }
        )
        source = context.desired.event
        return EngineEvent(
            protocol_version=source.protocol_version,
            event_id=f"ctx-install-action-expired:{digest}",
            kind="ActionExpired",
            scope=source.scope,
            expected_revision=action.precondition_revision,
            occurred_at=action.expires_at or challenge.expires_at,
            payload=payload,
            privacy=source.privacy,
            correlation_id=source.correlation_id,
            causation_id=action.action_id,
            engine_version=source.engine_version,
            planner_version=source.planner_version,
            policy_version=source.policy_version,
            host_descriptor_digest=source.host_descriptor_digest,
            catalog_snapshot_digest=source.catalog_snapshot_digest,
            semantic_model_digest=source.semantic_model_digest,
            semantic_index_digest=source.semantic_index_digest,
            work_signature=source.work_signature,
            random_seed=source.random_seed,
        )

    @staticmethod
    def _validate_install_action_expiry_transition(
        transition: Transition,
        *,
        event: EngineEvent,
        install_action: HostAction,
    ) -> None:
        if (
            type(transition) is not Transition
            or transition.event_id != event.event_id
            or transition.scope != event.scope
            or transition.from_revision != event.expected_revision
            or transition.to_revision != event.expected_revision + 1
            or install_action in transition.actions
        ):
            raise ManagedQueryServiceError("install action expiry transition was substituted")

    def _settled_install_result(
        self,
        snapshot: EngineSnapshot,
        *,
        challenge: InstallConsentChallenge,
        context: _ManagedConsentContext,
        report: InstallExecutionReport | None,
    ) -> ManagedConsentResolutionResult:
        state = snapshot.state
        capability = None if state is None else state.capability(challenge.capability_id)
        authority = context.selection.authority
        if (
            state is None
            or type(capability) is not CapabilityStateV3
            or not isinstance(authority, InstallPlanningAuthority)
        ):
            raise ManagedQueryServiceError("settled install capability state is unavailable")
        if capability.installation == "installed" and capability.activation == "inactive":
            return self._consent_resolution_result(
                snapshot,
                challenge=challenge,
                outcome="installed-inactive",
                reason_code="physical-install-applied",
                next_challenge=None,
            )
        if capability.installation == "installed":
            return self._consent_resolution_result(
                snapshot,
                challenge=challenge,
                outcome="quarantined",
                reason_code="installed-capability-is-already-active",
                next_challenge=None,
            )
        if (
            authority.descriptor.descriptor_digest in state.blocked_install_descriptor_digests
            and capability.installation == "absent"
        ):
            return self._consent_resolution_result(
                snapshot,
                challenge=challenge,
                outcome="install-failed",
                reason_code="physical-install-failed",
                next_challenge=None,
            )
        if report is None:
            raise ManagedQueryServiceError(
                "settled grant has neither pending nor terminal install evidence"
            )
        raise ManagedQueryServiceError("interactive install outcome is not terminal")

    def _consent_resolution_result(
        self,
        snapshot: EngineSnapshot,
        *,
        challenge: InstallConsentChallenge,
        outcome: str,
        reason_code: str,
        next_challenge: ManagedConsentChallengeProjection | None,
    ) -> ManagedConsentResolutionResult:
        head = self._authoritative_head_record(snapshot)
        return ManagedConsentResolutionResult._create(
            factory_token=_CONSENT_RESULT_FACTORY_TOKEN,
            challenge=challenge,
            outcome=outcome,
            reason_code=reason_code,
            next_challenge=next_challenge,
            actions=self._current_head_action_summaries(snapshot),
            journal_revision=head.revision,
            journal_record_digest=head.record_digest,
        )

    @staticmethod
    def _challenge_projection(
        challenge: InstallConsentChallenge,
    ) -> ManagedConsentChallengeProjection:
        return ManagedConsentChallengeProjection._create(
            factory_token=_CHALLENGE_FACTORY_TOKEN,
            challenge=challenge,
        )

    def _set_desired_locked(
        self,
        request: ManagedDesiredSetRequest,
    ) -> ManagedDesiredSetResult:
        parent = self._query_store.load(request.query_ref)
        if not parent.planned or parent.plan_id is None or parent.decision_digest is None:
            raise ManagedDesiredSetConflictError(
                "managed desired set requires an exactly planned parent query"
            )
        artifact = self._registry.reopen(
            manifest_digest=parent.artifact_manifest_digest,
            planning_environment_digest=parent.planning_environment_digest,
        )
        latest: ManagedDesiredSetRecord | None
        try:
            latest = self._query_store.load_latest_desired_set(parent.query_ref)
        except ManagedQueryStoreNotFound:
            latest = None
        if latest is None:
            if request.expected_previous_desired_set_ref is not None:
                raise ManagedDesiredSetSupersededError(
                    "desired choice expected a previous record that is unavailable"
                )
        elif latest.logical_choice_id == request.logical_choice_id:
            self._validate_retry_predecessor(parent, latest, request)
        else:
            if not latest.committed:
                raise ManagedDesiredSetBusyError(
                    "another managed desired choice is pending on this stream"
                )
            if request.expected_previous_desired_set_ref != latest.desired_set_ref:
                raise ManagedDesiredSetSupersededError(
                    "desired choice does not name the exact latest committed predecessor"
                )
        try:
            pending = self._query_store.load_pending_desired_set(parent.decision_event.scope)
        except ManagedQueryStoreNotFound:
            pending = None
        if pending is not None and (
            latest is None
            or pending.desired_set_ref != latest.desired_set_ref
            or pending.logical_choice_id != request.logical_choice_id
        ):
            raise ManagedDesiredSetBusyError(
                "another managed desired choice is pending on this stream"
            )

        with open_managed_engine_composition(
            registry=self._registry,
            artifact=artifact,
            journal_path=self._journal_path,
            benefit_audit_path=self._benefit_audit_path,
            benefit_facts_port=self._benefit_facts_port,
            net_benefit_policy=self._net_benefit_policy,
            material_port=self._material_port,
            install_bundle_port=self._install_bundle_port,
            policy_store_root=self._policy_store_root,
            interactive_install_decision_guard=self._interactive_install_decision_guard,
            trusted_utc_now=self._trusted_utc_now,
            skill_cas_runtime=self._skill_cas_runtime,
            agent_file_runtime=self._agent_file_runtime,
        ) as composition:
            snapshot = composition.snapshot(parent.decision_event.scope)
            committed = self._exact_parent_plan(snapshot, parent)
            capability_ids = self._canonical_desired_subset(
                committed,
                request.capability_ids,
            )
            if latest is not None and latest.logical_choice_id == request.logical_choice_id:
                if latest.capability_ids != capability_ids:
                    raise ManagedDesiredSetConflictError(
                        "logical desired choice is already bound to another subset"
                    )
                reserved = latest
            else:
                state = snapshot.state
                if state is None:
                    raise ManagedDesiredSetBusyError(
                        "managed stream has an unresolved lifecycle effect"
                    )
                pending_policy_drift = self._pending_desired_policy_drift(snapshot)
                if state.pending_effects or (state.pending_consents and not pending_policy_drift):
                    raise ManagedDesiredSetBusyError(
                        "managed stream has an unresolved lifecycle effect"
                    )
                reserved = self._reserve_desired_set(
                    parent=parent,
                    snapshot=snapshot,
                    committed=committed,
                    logical_choice_id=request.logical_choice_id,
                    capability_ids=capability_ids,
                    previous=latest,
                )
            transition, journal_record = self._recover_or_process_desired_event(
                composition,
                reserved.event,
            )
            marked = self._query_store.mark_desired_set_committed(
                reserved.desired_set_ref,
                journal_revision=journal_record.revision,
                journal_record_digest=journal_record.record_digest,
                transition_digest=journal_record.transition_digest,
            )
            current = composition.snapshot(parent.decision_event.scope)
            self._exact_parent_plan(current, parent)
            policy_drift = self._pending_desired_policy_drift(current)
            indeterminate_ids: tuple[str, ...] = ()
            if not policy_drift:
                current, indeterminate_ids = self._advance_automatic_installs(
                    composition,
                    current,
                    desired_record=marked,
                )
                self._exact_parent_plan(current, parent)
                policy_drift = self._pending_desired_policy_drift(current)
            challenge = (
                None
                if policy_drift or indeterminate_ids
                else self._publish_current_consent(
                    composition,
                    current,
                    expected_plan_id=parent.plan_id,
                )
            )
            actions = self._current_head_action_summaries(current)
            failed_ids = self._failed_desired_install_ids(
                current,
                committed=committed,
                capability_ids=marked.capability_ids,
            )
            status, reason_code, deferred_ids = self._desired_status(
                current,
                committed=committed,
                capability_ids=marked.capability_ids,
                actions=actions,
                challenge=challenge,
                policy_drift=policy_drift,
                indeterminate_capability_ids=indeterminate_ids,
                failed_capability_ids=failed_ids,
            )
        return ManagedDesiredSetResult._create(
            factory_token=_DESIRED_RESULT_FACTORY_TOKEN,
            record=marked,
            deferred_capability_ids=deferred_ids,
            failed_capability_ids=failed_ids,
            status=status,
            reason_code=reason_code,
            actions=actions,
            challenge=challenge,
        )

    def _validate_retry_predecessor(
        self,
        parent: ManagedQueryRecord,
        latest: ManagedDesiredSetRecord,
        request: ManagedDesiredSetRequest,
    ) -> None:
        expected_ref = request.expected_previous_desired_set_ref
        if latest.event.causation_id == parent.decision_event.event_id:
            if expected_ref is not None:
                raise ManagedDesiredSetSupersededError(
                    "first desired choice cannot name a predecessor"
                )
            return
        if expected_ref is None:
            raise ManagedDesiredSetSupersededError(
                "later desired choice must name its exact predecessor"
            )
        try:
            previous = self._query_store.load_desired_set(expected_ref)
        except ManagedQueryStoreNotFound as exc:
            raise ManagedDesiredSetSupersededError(
                "desired choice predecessor is unavailable"
            ) from exc
        if (
            not previous.committed
            or previous.query_ref != parent.query_ref
            or previous.event.event_id != latest.event.causation_id
        ):
            raise ManagedDesiredSetSupersededError(
                "desired choice predecessor no longer matches its durable causation"
            )

    @staticmethod
    def _exact_parent_plan(
        snapshot: EngineSnapshot,
        parent: ManagedQueryRecord,
    ) -> CommittedPlanV3:
        state = snapshot.state
        committed = None if state is None else state.committed_plan
        if (
            type(committed) is not CommittedPlanV3
            or committed.plan_id != parent.plan_id
            or committed.decision_digest != parent.decision_digest
            or committed.catalog_snapshot_id != parent.planning_environment_digest
        ):
            raise ManagedDesiredSetSupersededError(
                "managed desired set parent plan is no longer authoritative"
            )
        return committed

    @staticmethod
    def _canonical_desired_subset(
        committed: CommittedPlanV3,
        requested_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        planned_ids = tuple(item.capability_id for item in committed.capabilities)
        unknown = set(requested_ids).difference(planned_ids)
        if unknown:
            raise ManagedDesiredSetConflictError(
                "desired capability subset is not contained in the committed plan"
            )
        requested = set(requested_ids)
        return tuple(capability_id for capability_id in planned_ids if capability_id in requested)

    def _reserve_desired_set(
        self,
        *,
        parent: ManagedQueryRecord,
        snapshot: EngineSnapshot,
        committed: CommittedPlanV3,
        logical_choice_id: str,
        capability_ids: tuple[str, ...],
        previous: ManagedDesiredSetRecord | None,
    ) -> ManagedDesiredSetRecord:
        policy = load_current_install_policy(self._policy_store_root)
        owner_digest = _stable_digest(
            {
                "schema": "ctx.managed-desired-owner.v1",
                "stream": StreamId.from_scope(parent.decision_event.scope).key,
            }
        )
        owner_id = f"ctx-desired:{owner_digest}"
        by_id = {item.capability_id: item for item in committed.capabilities}
        desired_rows = []
        for capability_id in capability_ids:
            capability = by_id[capability_id]
            lease_digest = _stable_digest(
                {
                    "capability_id": capability_id,
                    "kind": capability.kind,
                    "owner_id": owner_id,
                    "schema": "ctx.managed-desired-lease.v1",
                    "source_digest": capability.source_digest,
                }
            )
            desired_rows.append(
                {
                    "actionability": capability.actionability,
                    "capability_id": capability.capability_id,
                    "install_descriptor_digest": capability.install_descriptor_digest,
                    "install_plan_digest": capability.install_plan_digest,
                    "kind": capability.kind,
                    "lease_id": f"ctx-lease:{lease_digest}",
                    "source_digest": capability.source_digest,
                }
            )
        event_digest = _stable_digest(
            {
                "capability_ids": capability_ids,
                "logical_choice_id": logical_choice_id,
                "plan_id": parent.plan_id,
                "policy_snapshot_digest": policy.policy_digest,
                "query_ref": parent.query_ref,
                "revision": snapshot.revision,
                "schema": "ctx.managed-desired-event.v1",
            }
        )
        event = EngineEvent(
            event_id=f"ctx-desired:{event_digest}",
            kind="ReassessmentRequested",
            scope=parent.decision_event.scope,
            expected_revision=snapshot.revision,
            occurred_at=self._desired_occurred_at(),
            payload={
                "desired_capabilities": desired_rows,
                "owner_id": owner_id,
                "policy_snapshot_digest": policy.policy_digest,
            },
            privacy=parent.decision_event.privacy,
            correlation_id=parent.plan_id,
            causation_id=(
                parent.decision_event.event_id if previous is None else previous.event.event_id
            ),
            engine_version=parent.decision_event.engine_version,
            planner_version=parent.decision_event.planner_version,
            policy_version=parent.decision_event.policy_version,
            host_descriptor_digest=parent.decision_event.host_descriptor_digest,
            catalog_snapshot_digest=parent.decision_event.catalog_snapshot_digest,
            semantic_model_digest=parent.decision_event.semantic_model_digest,
            semantic_index_digest=parent.decision_event.semantic_index_digest,
            work_signature=parent.decision_event.work_signature,
            random_seed=parent.decision_event.random_seed,
        )
        try:
            return self._query_store.reserve_desired_set(
                query_ref=parent.query_ref,
                logical_choice_id=logical_choice_id,
                capability_ids=capability_ids,
                event=event,
            )
        except ManagedQueryStoreConflict as exc:
            try:
                winner = self._query_store.load_latest_desired_set(parent.query_ref)
            except ManagedQueryStoreNotFound:
                raise ManagedDesiredSetBusyError(
                    "managed desired-set reservation lost a concurrent stream race"
                ) from exc
            expected_causation = (
                parent.decision_event.event_id if previous is None else previous.event.event_id
            )
            if (
                winner.logical_choice_id == logical_choice_id
                and winner.capability_ids == capability_ids
                and winner.event.causation_id == expected_causation
            ):
                return winner
            raise ManagedDesiredSetBusyError(
                "managed desired-set reservation lost a concurrent stream race"
            ) from exc

    def _desired_occurred_at(self) -> str:
        current = (
            self._trusted_utc_now() if self._trusted_utc_now is not None else datetime.now(UTC)
        )
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ManagedQueryServiceError("trusted desired-set clock must return aware datetime")
        return current.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _process_desired_event(
        self,
        composition: EngineComposition,
        event: EngineEvent,
    ) -> Transition:
        # A desired event selects capabilities but authorizes no physical
        # installation.  Its immutable policy snapshot must therefore remain
        # recoverable after a crash even when the user selects a newer policy.
        # The committed stale snapshot is reported as drift below, and an exact
        # successor reassessment clears/reissues the pending consent under the
        # current policy before any automatic grant can be committed.
        transition = composition.process(event)
        if (
            transition.event_id != event.event_id
            or transition.scope != event.scope
            or transition.from_revision != event.expected_revision
            or transition.to_revision != event.expected_revision + 1
            or len(transition.actions) > _MAX_ACTION_SUMMARIES
        ):
            raise ManagedQueryServiceError("managed desired-set transition was substituted")
        return transition

    def _recover_or_process_desired_event(
        self,
        composition: EngineComposition,
        event: EngineEvent,
    ) -> tuple[Transition, JournalRecord]:
        store = SQLiteEngineStore(self._journal_path)
        stream_id = StreamId.from_scope(event.scope)
        revision = event.expected_revision + 1
        committed = tuple(
            record
            for record in store.records(
                stream_id,
                after_revision=event.expected_revision,
            )
            if record.revision == revision
        )
        if committed:
            if len(committed) != 1:
                raise ManagedQueryServiceError("managed desired-set journal revision is ambiguous")
            record = committed[0]
            if (
                record.event_id != event.event_id
                or record.event_content_digest != event.content_digest
            ):
                raise ManagedDesiredSetConflictError(
                    "managed desired-set journal revision was committed by another event"
                )
            transition = Transition.from_json(record.transition_json)
            self._validate_desired_transition(transition, event)
            self._validate_desired_journal_record(record, transition, event)
            return transition, record
        head = store.load_head(stream_id)
        if head.revision != event.expected_revision:
            raise ManagedDesiredSetConflictError(
                "managed desired-set journal head advanced before its reserved event"
            )
        transition = self._process_desired_event(composition, event)
        return transition, self._exact_journal_record(transition, event)

    @staticmethod
    def _validate_desired_transition(
        transition: Transition,
        event: EngineEvent,
    ) -> None:
        if (
            transition.event_id != event.event_id
            or transition.scope != event.scope
            or transition.from_revision != event.expected_revision
            or transition.to_revision != event.expected_revision + 1
            or len(transition.actions) > _MAX_ACTION_SUMMARIES
        ):
            raise ManagedQueryServiceError("managed desired-set transition was substituted")

    @staticmethod
    def _validate_desired_journal_record(
        record: JournalRecord,
        transition: Transition,
        event: EngineEvent,
    ) -> None:
        if (
            record.event_id != transition.event_id
            or record.event_content_digest != event.content_digest
            or record.transition_json != transition.to_json()
            or record.transition_digest
            != hashlib.sha256(transition.to_json().encode("utf-8")).hexdigest()
        ):
            raise ManagedQueryServiceError("managed desired-set journal record was substituted")

    def _exact_journal_record(
        self,
        transition: Transition,
        event: EngineEvent,
    ) -> JournalRecord:
        matches = tuple(
            record
            for record in SQLiteEngineStore(self._journal_path).records(
                StreamId.from_scope(transition.scope),
                after_revision=transition.to_revision - 1,
            )
            if record.revision == transition.to_revision
        )
        if len(matches) != 1:
            raise ManagedQueryServiceError("managed desired-set journal record is unavailable")
        record = matches[0]
        self._validate_desired_journal_record(record, transition, event)
        return record

    def _current_head_action_summaries(
        self,
        snapshot: EngineSnapshot,
    ) -> tuple[ManagedActionSummary, ...]:
        if snapshot.state is None:
            raise ManagedQueryServiceError("managed desired-set current state is unavailable")
        record = self._authoritative_head_record(snapshot)
        transition = Transition.from_json(record.transition_json)
        if (
            transition.scope != snapshot.state.scope
            or transition.to_revision != snapshot.revision
            or len(transition.actions) > _MAX_ACTION_SUMMARIES
        ):
            raise ManagedQueryServiceError("managed desired-set current actions exceed their bound")
        return tuple(
            ManagedActionSummary._create(factory_token=_ACTION_FACTORY_TOKEN, action=action)
            for action in transition.actions
        )

    def _advance_automatic_installs(
        self,
        composition: EngineComposition,
        snapshot: EngineSnapshot,
        *,
        desired_record: ManagedDesiredSetRecord,
    ) -> tuple[EngineSnapshot, tuple[str, ...]]:
        """Grant and settle at most one serialized install per desired capability."""

        if not desired_record.committed:
            raise ManagedQueryServiceError("automatic installation requires a committed choice")
        current = snapshot
        for _attempt in range(len(desired_record.capability_ids)):
            state = current.state
            if state is None:
                raise ManagedQueryServiceError("automatic installation state is unavailable")
            install_action: HostAction | None = None
            selection: CapabilityPlanSelectionV3 | None = None
            binding: InstallExecutionBinding | None = None

            if state.pending_effects:
                install_effects = tuple(
                    pending for pending in state.pending_effects if pending.effect == "install"
                )
                if not install_effects:
                    break
                if len(state.pending_effects) != 1 or len(install_effects) != 1:
                    raise ManagedQueryServiceError(
                        "managed physical installation must remain globally serialized"
                    )
                install_action = install_effects[0].action
                selection = self._desired_install_selection(
                    current,
                    install_action=install_action,
                    desired_record=desired_record,
                )
                retired = self._retire_expired_automatic_install(
                    composition,
                    current=current,
                    install_action=install_action,
                    desired_record=desired_record,
                )
                if retired is not None:
                    current = retired
                    break
                binding = composition.resolve_install_execution_binding(
                    install_action,
                    selection,
                )
            elif state.pending_consents:
                if len(state.pending_consents) != 1:
                    raise ManagedQueryServiceError(
                        "managed automatic consent must remain globally serialized"
                    )
                pending = state.pending_consents[0]
                install_action = pending.install_action
                selection = self._desired_install_selection(
                    current,
                    install_action=install_action,
                    desired_record=desired_record,
                )
                request = self._current_consent_request(
                    scope=state.scope,
                    revision=current.revision,
                    consent_id=pending.consent_id,
                    install_action=install_action,
                )
                authority = selection.authority
                if not isinstance(authority, InstallPlanningAuthority):
                    raise ManagedQueryServiceError(
                        "managed automatic consent has no install planning authority"
                    )
                policy = load_current_install_policy(self._policy_store_root)
                if request.payload.get("policy_snapshot_digest") != policy.policy_digest:
                    break
                directive = route_install_consent_request(
                    request,
                    selection,
                    authority.descriptor,
                    policy,
                )
                if directive.requires_prompt:
                    break
                if not has_persisted_install_policy(self._policy_store_root):
                    raise ManagedQueryServiceError(
                        "automatic installation requires an explicitly persisted policy"
                    )
                binding = composition.resolve_install_execution_binding(
                    install_action,
                    selection,
                )
                self._validate_install_binding(
                    binding,
                    install_action=install_action,
                    selection=selection,
                )
                self._require_unchanged_consent_head(
                    composition,
                    expected_snapshot=current,
                    install_action=install_action,
                )
                decision = self._automatic_install_decision_event(
                    desired_record=desired_record,
                    snapshot=current,
                    request=request,
                    install_action=install_action,
                    binding=binding,
                    payload=directive.automatic_grant_payload(),
                )
                granted = composition.process(decision)
                self._validate_automatic_grant(
                    granted,
                    event=decision,
                    install_action=install_action,
                )
                current = composition.snapshot(install_action.scope)
                if (
                    current.revision != granted.to_revision
                    or current.record_digest is None
                    or current.state is None
                ):
                    raise ManagedQueryHeadDriftError(
                        "managed automatic grant is not the authoritative current head"
                    )
            else:
                break

            if install_action is None or selection is None or binding is None:
                raise AssertionError("automatic installation authority was not resolved")
            self._validate_install_binding(
                binding,
                install_action=install_action,
                selection=selection,
            )
            self._validate_committed_automatic_binding(
                desired_record=desired_record,
                install_action=install_action,
                binding=binding,
            )
            policy_digest = install_action.payload.get("policy_snapshot_digest")
            if not isinstance(policy_digest, str) or _DIGEST_RE.fullmatch(policy_digest) is None:
                raise ManagedQueryServiceError("managed install action has no exact policy digest")
            try:
                report = composition.execute_install(
                    install_action,
                    selection,
                    expected_policy_digest=policy_digest,
                )
            except InstallActionClaimExpired:
                retired = self._retire_expired_automatic_install(
                    composition,
                    current=composition.snapshot(install_action.scope),
                    install_action=install_action,
                    desired_record=desired_record,
                )
                if retired is not None:
                    current = retired
                    break
                raise
            if type(report) is not InstallExecutionReport:
                raise TypeError("managed composition must return an exact InstallExecutionReport")
            if report.execution_binding_digest != binding.binding_digest:
                raise ManagedQueryServiceError(
                    "managed install execution report substituted its physical binding"
                )
            current = composition.snapshot(install_action.scope)
            self._validate_install_execution_result(
                current,
                action=install_action,
                selection=selection,
                report=report,
            )
            if report.outcome == "indeterminate":
                capability_id = install_action.entity_id
                if capability_id is None:
                    raise ManagedQueryServiceError(
                        "indeterminate managed install lost its capability identity"
                    )
                return current, (capability_id,)
        return current, ()

    def _retire_expired_automatic_install(
        self,
        composition: EngineComposition,
        *,
        current: EngineSnapshot,
        install_action: HostAction,
        desired_record: ManagedDesiredSetRecord,
    ) -> EngineSnapshot | None:
        """Retire one stale preapproved action without touching its driver."""

        event = self._automatic_install_action_expired_event(
            desired_record=desired_record,
            install_action=install_action,
        )
        expiry_revision = event.expected_revision + 1
        if current.revision >= expiry_revision:
            record = self._journal_record_at_revision(
                install_action.scope,
                expiry_revision,
            )
            committed = ReplayInput.from_json(record.replay_json).reducer_event
            if (
                committed.to_json() == event.to_json()
                and record.event_id == event.event_id
                and record.event_content_digest == event.content_digest
            ):
                self._validate_install_action_expiry_transition(
                    Transition.from_json(record.transition_json),
                    event=event,
                    install_action=install_action,
                )
                recovered = composition.snapshot(install_action.scope)
                if self._has_exact_pending_install_effect(recovered, install_action):
                    raise ManagedQueryServiceError(
                        "committed automatic install expiry retained physical authority"
                    )
                return recovered
        status = SQLiteEngineStore(self._journal_path).install_execution_status(
            StreamId.from_scope(install_action.scope),
            install_action.action_id,
        )
        if status.claimed or not self._install_action_has_expired(install_action):
            return None
        if current.revision != event.expected_revision:
            raise ManagedQueryHeadDriftError(
                "expired automatic install no longer owns the exact journal head"
            )
        try:
            transition = composition.process(event)
        except InstallActionAlreadyClaimed:
            return None
        except RevisionConflict:
            recovered = composition.snapshot(install_action.scope)
            return self._retire_expired_automatic_install(
                composition,
                current=recovered,
                install_action=install_action,
                desired_record=desired_record,
            )
        self._validate_install_action_expiry_transition(
            transition,
            event=event,
            install_action=install_action,
        )
        return self._retire_expired_automatic_install(
            composition,
            current=composition.snapshot(install_action.scope),
            install_action=install_action,
            desired_record=desired_record,
        )

    @staticmethod
    def _automatic_install_action_expired_event(
        *,
        desired_record: ManagedDesiredSetRecord,
        install_action: HostAction,
    ) -> EngineEvent:
        if install_action.expires_at is None:
            raise ManagedQueryServiceError("automatic install action has no exact expiry")
        payload: dict[str, str | int] = {
            "action_id": install_action.action_id,
            "action_kind": install_action.kind,
            "action_content_digest": install_action.content_digest,
            "action_precondition_revision": install_action.precondition_revision,
            "reason": "expired",
        }
        digest = _stable_digest(
            {
                "desired_set_ref": desired_record.desired_set_ref,
                "install_action_content_digest": install_action.content_digest,
                "payload": payload,
                "schema": "ctx.managed-automatic-install-action-expiry.v1",
            }
        )
        source = desired_record.event
        return EngineEvent(
            protocol_version=source.protocol_version,
            event_id=f"ctx-automatic-install-action-expired:{digest}",
            kind="ActionExpired",
            scope=source.scope,
            expected_revision=install_action.precondition_revision,
            occurred_at=install_action.expires_at,
            payload=payload,
            privacy=source.privacy,
            correlation_id=source.correlation_id,
            causation_id=install_action.action_id,
            engine_version=source.engine_version,
            planner_version=source.planner_version,
            policy_version=source.policy_version,
            host_descriptor_digest=source.host_descriptor_digest,
            catalog_snapshot_digest=source.catalog_snapshot_digest,
            semantic_model_digest=source.semantic_model_digest,
            semantic_index_digest=source.semantic_index_digest,
            work_signature=source.work_signature,
            random_seed=source.random_seed,
        )

    def _desired_install_selection(
        self,
        snapshot: EngineSnapshot,
        *,
        install_action: HostAction,
        desired_record: ManagedDesiredSetRecord,
    ) -> CapabilityPlanSelectionV3:
        state = snapshot.state
        capability_id = install_action.entity_id
        capability = (
            None if state is None or capability_id is None else state.capability(capability_id)
        )
        if (
            type(capability) is not CapabilityStateV3
            or capability_id not in desired_record.capability_ids
            or capability.plan_id != desired_record.plan_id
            or install_action.kind != "InstallCapability"
            or install_action.scope != desired_record.event.scope
            or install_action.entity_id != capability.capability_id
            or install_action.source_digest != capability.source_digest
        ):
            raise ManagedQueryServiceError(
                "managed physical install is not an exact member of the desired plan"
            )
        selection = capability.selection.selection
        if type(selection) is not CapabilityPlanSelectionV3:
            raise ManagedQueryServiceError("managed physical install selection was substituted")
        return selection

    @staticmethod
    def _validate_install_binding(
        binding: InstallExecutionBinding,
        *,
        install_action: HostAction,
        selection: CapabilityPlanSelectionV3,
    ) -> None:
        authority = selection.authority
        if (
            type(binding) is not InstallExecutionBinding
            or not isinstance(authority, InstallPlanningAuthority)
            or binding.driver_id != authority.descriptor.installer_id
            or binding.driver_digest != install_action.payload.get("installer_digest")
        ):
            raise ManagedQueryServiceError(
                "managed install execution binding does not match its journal authority"
            )

    def _automatic_install_decision_event(
        self,
        *,
        desired_record: ManagedDesiredSetRecord,
        snapshot: EngineSnapshot,
        request: HostAction,
        install_action: HostAction,
        binding: InstallExecutionBinding,
        payload: dict[str, str | int] | None,
    ) -> EngineEvent:
        if payload is None:
            raise ManagedQueryServiceError("automatic install routing returned no exact grant")
        if (
            desired_record.journal_revision is None
            or desired_record.journal_record_digest is None
            or snapshot.record_digest is None
        ):
            raise ManagedQueryServiceError("automatic install lacks durable desired authority")
        head = self._authoritative_head_record(snapshot)
        return self._automatic_install_decision_event_from_head(
            desired_record=desired_record,
            head=head,
            request=request,
            install_action=install_action,
            binding=binding,
            payload=payload,
        )

    def _automatic_install_decision_event_from_head(
        self,
        *,
        desired_record: ManagedDesiredSetRecord,
        head: JournalRecord,
        request: HostAction,
        install_action: HostAction,
        binding: InstallExecutionBinding,
        payload: dict[str, str | int] | None,
    ) -> EngineEvent:
        if payload is None:
            raise ManagedQueryServiceError("automatic install routing returned no exact grant")
        if (
            desired_record.journal_revision is None
            or desired_record.journal_record_digest is None
            or head.stream_id != StreamId.from_scope(desired_record.event.scope)
            or request.scope != desired_record.event.scope
            or install_action.scope != desired_record.event.scope
        ):
            raise ManagedQueryServiceError("automatic install lacks durable desired authority")
        digest = _stable_digest(
            {
                "binding_digest": binding.binding_digest,
                "desired_event_content_digest": desired_record.event.content_digest,
                "desired_event_id": desired_record.event.event_id,
                "desired_journal_record_digest": desired_record.journal_record_digest,
                "desired_journal_revision": desired_record.journal_revision,
                "desired_set_ref": desired_record.desired_set_ref,
                "head_event_id": head.event_id,
                "head_record_digest": head.record_digest,
                "head_revision": head.revision,
                "install_action_content_digest": install_action.content_digest,
                "install_action_id": install_action.action_id,
                "install_action_precondition_revision": install_action.precondition_revision,
                "payload": payload,
                "request_action_content_digest": request.content_digest,
                "request_action_id": request.action_id,
                "schema": "ctx.managed-auto-install-decision.v1",
            }
        )
        source = desired_record.event
        return EngineEvent(
            protocol_version=source.protocol_version,
            event_id=f"ctx-auto-install:{digest}",
            kind="UserDecision",
            scope=source.scope,
            expected_revision=head.revision,
            occurred_at=source.occurred_at,
            payload=payload,
            privacy=source.privacy,
            correlation_id=source.correlation_id,
            causation_id=request.action_id,
            engine_version=source.engine_version,
            planner_version=source.planner_version,
            policy_version=source.policy_version,
            host_descriptor_digest=source.host_descriptor_digest,
            catalog_snapshot_digest=source.catalog_snapshot_digest,
            semantic_model_digest=source.semantic_model_digest,
            semantic_index_digest=source.semantic_index_digest,
            work_signature=source.work_signature,
            random_seed=source.random_seed,
        )

    def _validate_committed_automatic_binding(
        self,
        *,
        desired_record: ManagedDesiredSetRecord,
        install_action: HostAction,
        binding: InstallExecutionBinding,
    ) -> None:
        grant_record = self._journal_record_at_revision(
            install_action.scope,
            install_action.precondition_revision,
        )
        grant = ReplayInput.from_json(grant_record.replay_json).reducer_event
        if grant.kind != "UserDecision":
            raise ManagedQueryServiceError(
                "managed pending install has no exact committed decision"
            )
        if grant.payload.get("decision_basis") != "preapproved-policy":
            raise ManagedQueryServiceError(
                "managed automatic execution cannot consume an interactive install decision"
            )
        consent_head = self._journal_record_at_revision(
            install_action.scope,
            install_action.precondition_revision - 1,
        )
        request = self._consent_request_from_record(
            consent_head,
            install_action=install_action,
        )
        payload = dict(grant.payload)
        reproduced = self._automatic_install_decision_event_from_head(
            desired_record=desired_record,
            head=consent_head,
            request=request,
            install_action=install_action,
            binding=binding,
            payload=payload,
        )
        if (
            reproduced.to_json() != grant.to_json()
            or grant_record.event_id != grant.event_id
            or grant_record.event_content_digest != grant.content_digest
        ):
            raise ManagedQueryServiceError(
                "committed automatic install binding no longer matches the captured actuator"
            )

    def _journal_record_at_revision(
        self,
        scope: ScopeRef,
        revision: int,
    ) -> JournalRecord:
        if type(revision) is not int or revision < 1:
            raise ManagedQueryServiceError("managed install journal revision is invalid")
        matches = tuple(
            record
            for record in SQLiteEngineStore(self._journal_path).records(
                StreamId.from_scope(scope),
                after_revision=revision - 1,
            )
            if record.revision == revision
        )
        if len(matches) != 1:
            raise ManagedQueryServiceError("managed install journal authority is unavailable")
        return matches[0]

    @staticmethod
    def _consent_request_from_record(
        record: JournalRecord,
        *,
        install_action: HostAction,
    ) -> HostAction:
        transition = Transition.from_json(record.transition_json)
        matches = tuple(
            action
            for action in transition.actions
            if action.kind == "RequestConsent"
            and action.consent_id == install_action.consent_id
            and action.entity_id == install_action.entity_id
            and action.payload.get("requested_action_id") == install_action.action_id
            and action.payload.get("requested_action_content_digest")
            == install_action.content_digest
            and action.payload.get("requested_action_precondition_revision")
            == install_action.precondition_revision
        )
        if (
            len(matches) != 1
            or transition.to_revision != record.revision
            or install_action.precondition_revision != record.revision + 1
        ):
            raise ManagedQueryServiceError(
                "committed automatic install has no exact consent request"
            )
        return matches[0]

    @staticmethod
    def _validate_automatic_grant(
        transition: Transition,
        *,
        event: EngineEvent,
        install_action: HostAction,
    ) -> None:
        if (
            type(transition) is not Transition
            or transition.event_id != event.event_id
            or transition.scope != event.scope
            or transition.from_revision != event.expected_revision
            or transition.to_revision != event.expected_revision + 1
            or transition.actions != (install_action,)
        ):
            raise ManagedQueryServiceError("managed automatic grant was substituted")

    def _validate_install_execution_result(
        self,
        snapshot: EngineSnapshot,
        *,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
        report: InstallExecutionReport,
    ) -> None:
        state = snapshot.state
        capability = None if state is None else state.capability(action.entity_id or "")
        authority = selection.authority
        if (
            state is None
            or type(capability) is not CapabilityStateV3
            or not isinstance(authority, InstallPlanningAuthority)
        ):
            raise ManagedQueryServiceError("managed install result lost its planned capability")
        pending = tuple(
            item
            for item in state.pending_effects
            if item.effect == "install" and item.action == action
        )
        if report.outcome == "indeterminate":
            if report.settled or len(pending) != 1:
                raise ManagedQueryServiceError(
                    "indeterminate managed install is not durably pending"
                )
            return
        if not report.settled:
            raise ManagedQueryServiceError("managed install outcome is not durably settled")
        if report.outcome == "applied":
            current = capability.current_authorized_material
            lineage = None if current is None else current.installed_material_lineage
            if (
                capability.installation != "installed"
                or current is None
                or current.material_identity_digest != authority.result_material.identity_digest
                or lineage is None
                or lineage.install_action_content_digest != action.content_digest
                or lineage.material_identity_digest != authority.result_material.identity_digest
                or lineage.origin_install_descriptor_digest
                != authority.descriptor.descriptor_digest
                or pending
            ):
                raise ManagedQueryServiceError(
                    "applied managed install has no exact receipt-backed state"
                )
            receipts = tuple(
                record
                for record in SQLiteEngineStore(self._journal_path).records(snapshot.stream_id)
                if record.event_content_digest == lineage.install_receipt_content_digest
            )
            if len(receipts) != 1:
                raise ManagedQueryServiceError(
                    "applied managed install receipt lineage is not journal-anchored"
                )
            receipt = ReplayInput.from_json(receipts[0].replay_json).reducer_event
            if (
                receipt.kind != "ActionApplied"
                or receipt.payload.get("action_id") != action.action_id
                or receipt.payload.get("action_content_digest") != action.content_digest
                or receipt.payload.get("action_precondition_revision")
                != action.precondition_revision
            ):
                raise ManagedQueryServiceError(
                    "applied managed install receipt lineage was substituted"
                )
            return
        if (
            report.outcome != "failed"
            or capability.installation != "absent"
            or authority.descriptor.descriptor_digest
            not in state.blocked_install_descriptor_digests
            or pending
        ):
            raise ManagedQueryServiceError("failed managed install has no exact durable state")

    def _authoritative_head_record(self, snapshot: EngineSnapshot) -> JournalRecord:
        records = tuple(
            SQLiteEngineStore(self._journal_path).records(
                snapshot.stream_id,
                after_revision=max(0, snapshot.revision - 1),
            )
        )
        if len(records) != 1 or records[0].revision != snapshot.revision:
            raise ManagedQueryServiceError("managed desired-set current head is unavailable")
        if records[0].record_digest != snapshot.record_digest:
            raise ManagedQueryServiceError(
                "managed desired-set current head digest does not match its snapshot"
            )
        return records[0]

    def _pending_desired_policy_drift(self, snapshot: EngineSnapshot) -> bool:
        state = snapshot.state
        if state is None or not state.pending_consents:
            return False
        if len(state.pending_consents) != 1:
            raise ManagedQueryServiceError("managed desired-set consent is not serialized")
        expected = state.pending_consents[0].install_action.payload.get("policy_snapshot_digest")
        current = load_current_install_policy(self._policy_store_root)
        return expected != current.policy_digest

    @staticmethod
    def _failed_desired_install_ids(
        snapshot: EngineSnapshot,
        *,
        committed: CommittedPlanV3,
        capability_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        state = snapshot.state
        if state is None:
            raise ManagedQueryServiceError("managed desired-set state is unavailable")
        requested = set(capability_ids)
        blocked = set(state.blocked_install_descriptor_digests)
        failed: list[str] = []
        for planned in committed.capabilities:
            if planned.capability_id not in requested:
                continue
            authority = planned.selection.authority
            capability = state.capability(planned.capability_id)
            if (
                isinstance(authority, InstallPlanningAuthority)
                and type(capability) is CapabilityStateV3
                and capability.installation == "absent"
                and authority.descriptor.descriptor_digest in blocked
            ):
                failed.append(planned.capability_id)
        return tuple(failed)

    @staticmethod
    def _desired_status(
        snapshot: EngineSnapshot,
        *,
        committed: CommittedPlanV3,
        capability_ids: tuple[str, ...],
        actions: tuple[ManagedActionSummary, ...],
        challenge: ManagedConsentChallengeProjection | None,
        policy_drift: bool,
        indeterminate_capability_ids: tuple[str, ...],
        failed_capability_ids: tuple[str, ...],
    ) -> tuple[str, str | None, tuple[str, ...]]:
        if indeterminate_capability_ids:
            return (
                "lifecycle-deferred",
                "automatic-install-indeterminate",
                indeterminate_capability_ids,
            )
        if challenge is not None:
            return "consent-required", None, ()
        state = snapshot.state
        if state is None:
            raise ManagedQueryServiceError("managed desired-set state is unavailable")
        if policy_drift:
            deferred = tuple(
                pending.install_action.entity_id
                for pending in state.pending_consents
                if pending.install_action.entity_id in capability_ids
            )
            return (
                "lifecycle-deferred",
                "install-policy-changed-after-desired-commit",
                tuple(capability_id for capability_id in deferred if capability_id is not None),
            )
        manual = {
            item.capability_id for item in committed.capabilities if item.actionability == "manual"
        }
        deferred_manual = tuple(
            capability_id for capability_id in capability_ids if capability_id in manual
        )
        if deferred_manual:
            return "manual-deferred", "manual-capability-requires-user-action", deferred_manual
        if state.pending_effects or actions:
            return "effect-pending", None, ()
        if failed_capability_ids:
            return (
                "lifecycle-deferred",
                "automatic-install-failed",
                failed_capability_ids,
            )
        if state.pending_consents:
            raise ManagedQueryServiceError(
                "managed desired-set pending consent has no prompt or automatic route"
            )
        requested = set(capability_ids)
        blocked = set(state.blocked_install_descriptor_digests)
        missing: list[str] = []
        for planned in committed.capabilities:
            authority = planned.selection.authority
            capability = state.capability(planned.capability_id)
            if (
                planned.capability_id in requested
                and isinstance(authority, InstallPlanningAuthority)
                and authority.descriptor.descriptor_digest not in blocked
                and type(capability) is CapabilityStateV3
                and capability.desired
                and capability.installation == "absent"
            ):
                missing.append(planned.capability_id)
        missing_install_authority = tuple(missing)
        if missing_install_authority:
            return (
                "lifecycle-deferred",
                "install-lifecycle-authority-missing",
                missing_install_authority,
            )
        return "reconciled", None, ()

    def _validate_input(self, value: object) -> None:
        if type(value) is not ManagedQueryInput:
            raise TypeError("input authority must return an exact ManagedQueryInput")
        artifact = value.artifact
        if (
            artifact._registry_token is not self._registry._registry_token
            or artifact._pid != os.getpid()
        ):
            raise ManagedQueryServiceError(
                "managed query artifact was not issued by this service registry"
            )
        if (
            self._benefit_facts_port.benefit_facts_snapshot_digest
            != artifact.benefit_facts_snapshot_digest
            or self._net_benefit_policy.policy_digest != artifact.benefit_policy_snapshot_digest
            or getattr(self._material_port, "material_snapshot_digest", None)
            != artifact.material_snapshot_digest
            or getattr(self._install_bundle_port, "installation_snapshot_digest", None)
            != artifact.installation_snapshot_digest
        ):
            raise ValueError("managed planning authority snapshot does not match its artifact")
        started = value.session_started
        decision = value.decision_event
        if started.kind != "SessionStarted" or started.expected_revision != 0:
            raise ManagedQueryServiceError("managed query input must start at revision zero")
        expected_kind = (
            "IntentObserved" if decision.expected_revision == 1 else "DevelopmentObserved"
        )
        if decision.expected_revision < 1 or decision.kind != expected_kind:
            raise ManagedQueryServiceError("managed query input has an invalid decision revision")
        if (
            started.catalog_snapshot_digest != artifact.planning_environment_digest
            or decision.catalog_snapshot_digest != artifact.planning_environment_digest
            or started.planner_version != artifact.planning_schema_version
            or decision.planner_version != artifact.planning_schema_version
        ):
            raise ManagedQueryServiceError("managed query input does not match its artifact")
        reference = artifact.observation_reference
        expected_reference = {
            "content_digest": reference.content_digest,
            "opaque_id": reference.opaque_id,
            "provider_id": reference.provider_id,
        }
        if dict(decision.payload) != {"observation_ref": expected_reference}:
            raise ManagedQueryServiceError("managed query input observation is not artifact-bound")

    def _validate_record(self, record: object, value: ManagedQueryInput) -> None:
        if type(record) is not ManagedQueryRecord:
            raise TypeError("query store must return an exact ManagedQueryRecord")
        if (
            record.session_started != value.session_started
            or record.decision_event != value.decision_event
            or record.artifact_manifest_digest != value.artifact.manifest_digest
            or record.planning_environment_digest != value.artifact.planning_environment_digest
        ):
            raise ManagedQueryServiceError(
                "managed query store returned a substituted registration"
            )

    def _advance_record(
        self,
        record: ManagedQueryRecord,
        artifact: ManagedArtifactHandle,
    ) -> ManagedQueryServiceResult:
        with open_managed_engine_composition(
            registry=self._registry,
            artifact=artifact,
            journal_path=self._journal_path,
            benefit_audit_path=self._benefit_audit_path,
            benefit_facts_port=self._benefit_facts_port,
            net_benefit_policy=self._net_benefit_policy,
            material_port=self._material_port,
            install_bundle_port=self._install_bundle_port,
            policy_store_root=self._policy_store_root,
            interactive_install_decision_guard=self._interactive_install_decision_guard,
            trusted_utc_now=self._trusted_utc_now,
            skill_cas_runtime=self._skill_cas_runtime,
            agent_file_runtime=self._agent_file_runtime,
        ) as composition:
            if record.planned:
                current = composition.reopen_managed_query(record.decision_event.scope)
                if current.plan_id != record.plan_id:
                    raise ManagedQuerySupersededError(
                        "managed query was superseded by a later committed plan"
                    )
            advanced = composition.advance_managed_query(
                session_started=record.session_started,
                planning_observed=record.decision_event,
            )
            self._validate_advance(record, advanced)
            challenge = self._publish_current_consent(
                composition,
                composition.snapshot(record.decision_event.scope),
                expected_plan_id=advanced.prepared.plan_id,
            )
        prepared = advanced.prepared
        if record.planned:
            if (
                record.plan_id is None
                or record.decision_digest is None
                or record.journal_revision is None
                or record.journal_record_digest is None
            ):
                raise ManagedQueryServiceError("planned managed query is only partially bound")
            plan_id = record.plan_id
            decision_digest = record.decision_digest
            journal_revision = record.journal_revision
            journal_record_digest = record.journal_record_digest
        else:
            if prepared.journal_revision != advanced.transition.to_revision:
                raise ManagedQueryServiceError(
                    "managed planning head is not the committed decision"
                )
            plan_id = prepared.plan_id
            decision_digest = prepared.decision_digest
            journal_revision = prepared.journal_revision
            journal_record_digest = prepared.journal_record_digest
        marked = self._query_store.mark_planned(
            record.query_ref,
            plan_id=plan_id,
            decision_digest=decision_digest,
            journal_revision=journal_revision,
            journal_record_digest=journal_record_digest,
        )
        if (
            not marked.planned
            or marked.plan_id != prepared.plan_id
            or marked.decision_digest != prepared.decision_digest
        ):
            raise ManagedQueryServiceError("managed query completion binding was substituted")
        summaries = tuple(
            ManagedActionSummary._create(factory_token=_ACTION_FACTORY_TOKEN, action=action)
            for action in advanced.transition.actions
        )
        return ManagedQueryServiceResult._create(
            factory_token=_RESULT_FACTORY_TOKEN,
            query_ref=record.query_ref,
            prepared=prepared,
            actions=summaries,
            challenge=challenge,
        )

    def _publish_current_consent(
        self,
        composition: EngineComposition,
        snapshot: EngineSnapshot,
        *,
        expected_plan_id: str,
    ) -> ManagedConsentChallengeProjection | None:
        if (
            type(composition) is not EngineComposition
            or type(snapshot) is not EngineSnapshot
            or snapshot.state is None
            or snapshot.record_digest is None
        ):
            raise ManagedQueryServiceError("managed consent requires an exact current snapshot")
        state = snapshot.state
        committed = state.committed_plan
        if not isinstance(committed, CommittedPlanV3) or committed.plan_id != expected_plan_id:
            raise ManagedQueryHeadDriftError("managed plan changed before consent publication")
        if not state.pending_consents:
            return None
        if len(state.pending_consents) != 1:
            raise ManagedQueryServiceError("managed consent must remain globally serialized")
        pending = state.pending_consents[0]
        install_action = pending.install_action
        request = self._current_consent_request(
            scope=state.scope,
            revision=snapshot.revision,
            consent_id=pending.consent_id,
            install_action=install_action,
        )
        capability = state.capability(install_action.entity_id or "")
        if type(capability) is not CapabilityStateV3:
            raise ManagedQueryServiceError(
                "managed consent is not bound to an exact schema-v3 capability"
            )
        selection = capability.selection.selection
        authority = selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            raise ManagedQueryServiceError("managed consent has no install planning authority")
        policy = load_current_install_policy(self._policy_store_root)
        if request.payload.get("policy_snapshot_digest") != policy.policy_digest:
            raise ManagedQueryServiceError("managed consent policy snapshot is no longer exact")

        if has_persisted_install_policy(self._policy_store_root):
            with hold_current_install_policy(
                policy.policy_digest,
                root=self._policy_store_root,
            ) as held_policy:
                projection = self._prepare_consent_challenge(
                    composition=composition,
                    expected_snapshot=snapshot,
                    request=request,
                    install_action=install_action,
                    selection=selection,
                    policy=held_policy.policy,
                )
                held_policy.assert_current()
                return projection
        return self._prepare_consent_challenge(
            composition=composition,
            expected_snapshot=snapshot,
            request=request,
            install_action=install_action,
            selection=selection,
            policy=policy,
        )

    def _current_consent_request(
        self,
        *,
        scope: ScopeRef,
        revision: int,
        consent_id: str,
        install_action: HostAction,
    ) -> HostAction:
        if install_action.scope != scope or install_action.consent_id != consent_id:
            raise ManagedQueryServiceError("pending consent install identity is inconsistent")
        records = tuple(
            SQLiteEngineStore(self._journal_path).records(
                StreamId.from_scope(install_action.scope),
                after_revision=max(0, revision - 1),
            )
        )
        if len(records) != 1 or records[0].revision != revision:
            raise ManagedQueryServiceError(
                "managed consent request is not the authoritative head transition"
            )
        transition = Transition.from_json(records[0].transition_json)
        matches = tuple(
            action
            for action in transition.actions
            if action.kind == "RequestConsent"
            and action.consent_id == consent_id
            and action.entity_id == install_action.entity_id
            and action.payload.get("requested_action_id") == install_action.action_id
            and action.payload.get("requested_action_content_digest")
            == install_action.content_digest
            and action.payload.get("requested_action_precondition_revision")
            == install_action.precondition_revision
        )
        if len(matches) != 1:
            raise ManagedQueryServiceError(
                "managed consent request is missing from the authoritative head transition"
            )
        return matches[0]

    def _prepare_consent_challenge(
        self,
        *,
        composition: EngineComposition,
        expected_snapshot: EngineSnapshot,
        request: HostAction,
        install_action: HostAction,
        selection: CapabilityPlanSelectionV3,
        policy: InstallConsentPolicy,
    ) -> ManagedConsentChallengeProjection | None:
        authority = selection.authority
        if not isinstance(authority, InstallPlanningAuthority):
            raise ManagedQueryServiceError("managed consent lost its install authority")
        directive = route_install_consent_request(
            request,
            selection,
            authority.descriptor,
            policy,
        )
        if not directive.requires_prompt:
            return None
        broker = self._consent_broker
        if broker is None:
            raise ManagedQueryServiceError(
                "interactive managed consent requires captured publication authorities"
            )
        binding = composition.resolve_install_execution_binding(install_action, selection)
        if type(binding) is not InstallExecutionBinding:
            raise TypeError("composition must return InstallExecutionBinding")
        if (
            binding.driver_id != authority.descriptor.installer_id
            or binding.driver_digest != install_action.payload.get("installer_digest")
        ):
            raise ManagedQueryServiceError(
                "managed consent execution binding does not match its install action"
            )
        self._require_unchanged_consent_head(
            composition,
            expected_snapshot=expected_snapshot,
            install_action=install_action,
        )
        challenge = broker.prepare(
            directive=directive,
            selection=selection,
            install_action=install_action,
            execution_binding=binding,
        )
        self._require_unchanged_consent_head(
            composition,
            expected_snapshot=expected_snapshot,
            install_action=install_action,
        )
        status = broker.status(challenge.challenge_id)
        if status.challenge != challenge:
            raise ManagedQueryServiceError("managed consent broker substituted its challenge")
        self._require_unchanged_consent_head(
            composition,
            expected_snapshot=expected_snapshot,
            install_action=install_action,
        )
        if status.state != "pending":
            return None
        return ManagedConsentChallengeProjection._create(
            factory_token=_CHALLENGE_FACTORY_TOKEN,
            challenge=challenge,
        )

    def _require_unchanged_consent_head(
        self,
        composition: EngineComposition,
        *,
        expected_snapshot: EngineSnapshot,
        install_action: HostAction,
    ) -> None:
        current = composition.snapshot(install_action.scope)
        expected_state = expected_snapshot.state
        current_state = current.state
        if (
            expected_state is None
            or current_state is None
            or current.revision != expected_snapshot.revision
            or current.record_digest != expected_snapshot.record_digest
            or current_state.committed_plan != expected_state.committed_plan
            or current_state.pending_consents != expected_state.pending_consents
            or len(current_state.pending_consents) != 1
            or current_state.pending_consents[0].install_action != install_action
        ):
            raise ManagedQueryHeadDriftError(
                "managed consent head changed during challenge publication; "
                "any published broker row is stale and cannot authorize current state"
            )

    def _validate_advance(
        self,
        record: ManagedQueryRecord,
        advanced: object,
    ) -> None:
        if type(advanced) is not ManagedAdvanceResult:
            raise TypeError("managed composition must return an exact ManagedAdvanceResult")
        prepared = advanced.prepared
        transition = advanced.transition
        if type(prepared) is not PreparedManagedQuery or type(transition) is not Transition:
            raise ManagedQueryServiceError("managed composition returned an invalid projection")
        if (
            transition.event_id != record.decision_event.event_id
            or transition.scope != record.decision_event.scope
            or transition.from_revision != record.decision_event.expected_revision
            or transition.to_revision != record.decision_event.expected_revision + 1
            or prepared.plan_id != record.decision_event.correlation_id
            or prepared.planning_environment_digest != record.planning_environment_digest
        ):
            raise ManagedQueryServiceError(
                "managed composition lost the registered decision identity"
            )
        if len(transition.actions) > _MAX_ACTION_SUMMARIES:
            raise ManagedQueryServiceError("managed transition exceeds the action summary bound")
        for action in transition.actions:
            if (
                type(action) is not HostAction
                or action.scope != transition.scope
                or action.precondition_revision != transition.to_revision
            ):
                raise ManagedQueryServiceError("managed transition contains a substituted action")
        if record.planned and (
            prepared.plan_id != record.plan_id or prepared.decision_digest != record.decision_digest
        ):
            raise ManagedQueryServiceError("managed replay disagrees with its durable completion")

    def close(self) -> None:
        self._assert_owner_process()
        with self._lock:
            object.__setattr__(self, "_closed", True)

    def __enter__(self) -> ManagedQueryService:
        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def open_managed_query_service(
    *,
    registry: ManagedArtifactRegistry,
    query_store: ManagedQueryStore,
    journal_path: Path,
    benefit_audit_path: Path,
    benefit_facts_port: AuthenticatedBenefitFactsPort,
    net_benefit_policy: NetBenefitPolicy,
    material_port: CapabilityMaterialPort,
    install_bundle_port: CapabilityInstallBundlePort,
    input_authority: ManagedQueryInputAuthority,
    consent_broker: InstallConsentBrokerService | None = None,
    policy_store_root: Path | None = None,
    interactive_install_decision_guard: InteractiveInstallDecisionGuard | None = None,
    trusted_utc_now: Callable[[], datetime] | None = None,
    verifier_registry: TrustedHumanDecisionVerifierRegistry | None = None,
    skill_cas_runtime: SkillCasRuntimeConfig | None = None,
    agent_file_runtime: AgentFileRuntimeConfig | None = None,
) -> ManagedQueryService:
    """Capture one exact set of planning authorities behind an opaque API."""

    if type(registry) is not ManagedArtifactRegistry:
        raise TypeError("registry must be an exact ManagedArtifactRegistry")
    if type(query_store) is not ManagedQueryStore:
        raise TypeError("query_store must be an exact ManagedQueryStore")
    if not isinstance(journal_path, Path):
        raise TypeError("journal_path must be a Path")
    if not journal_path.is_absolute():
        raise ValueError("journal_path must be an absolute Path")
    if not isinstance(benefit_audit_path, Path):
        raise TypeError("benefit_audit_path must be a Path")
    if not benefit_audit_path.is_absolute():
        raise ValueError("benefit_audit_path must be an absolute Path")
    store_path = query_store._path  # noqa: SLF001 - exact owned-store collision check.
    owned_paths = {journal_path, benefit_audit_path, store_path}
    if len(owned_paths) != 3:
        raise ValueError("query store, journal, and benefit audit paths must be distinct")
    if (
        not callable(getattr(benefit_facts_port, "benefit_candidate", None))
        or _DIGEST_RE.fullmatch(getattr(benefit_facts_port, "benefit_facts_snapshot_digest", ""))
        is None
    ):
        raise TypeError("benefit_facts_port must implement the authenticated facts contract")
    if not isinstance(net_benefit_policy, NetBenefitPolicy):
        raise TypeError("net_benefit_policy must be a NetBenefitPolicy")
    if (
        not callable(getattr(material_port, "describe", None))
        or not callable(getattr(material_port, "prepare", None))
        or _DIGEST_RE.fullmatch(getattr(material_port, "material_snapshot_digest", "")) is None
    ):
        raise TypeError("material_port must implement the capability material contract")
    if (
        not callable(getattr(install_bundle_port, "describe_bundle", None))
        or not callable(getattr(install_bundle_port, "describe", None))
        or not callable(getattr(install_bundle_port, "prepare", None))
        or _DIGEST_RE.fullmatch(getattr(install_bundle_port, "installation_snapshot_digest", ""))
        is None
    ):
        raise TypeError("install_bundle_port must implement the installation bundle contract")
    if not callable(getattr(input_authority, "resolve", None)):
        raise TypeError("input_authority must implement the managed input contract")
    if consent_broker is not None:
        if type(consent_broker) is not InstallConsentBrokerService:
            raise TypeError("consent_broker must be an InstallConsentBrokerService or None")
        consent_path = consent_broker._store.path  # noqa: SLF001 - collision check only.
        if consent_path in owned_paths:
            raise ValueError("managed query and consent persistence paths must be distinct")
        evidence_provider = consent_broker._evidence_provider  # noqa: SLF001
        if evidence_provider is not None and (
            type(evidence_provider) is not SQLiteEngineStore
            or evidence_provider.path != journal_path
        ):
            raise ValueError(
                "managed consent broker evidence provider must own the managed journal"
            )
    if policy_store_root is not None:
        if not isinstance(policy_store_root, Path):
            raise TypeError("policy_store_root must be a Path or None")
        if not policy_store_root.is_absolute():
            raise ValueError("policy_store_root must be an absolute Path or None")
    if interactive_install_decision_guard is not None and not callable(
        interactive_install_decision_guard
    ):
        raise TypeError("interactive_install_decision_guard must be callable or None")
    if trusted_utc_now is not None and not callable(trusted_utc_now):
        raise TypeError("trusted_utc_now must be callable or None")
    if (
        verifier_registry is not None
        and type(verifier_registry) is not TrustedHumanDecisionVerifierRegistry
    ):
        raise TypeError("verifier_registry must be a TrustedHumanDecisionVerifierRegistry or None")
    if skill_cas_runtime is not None and not isinstance(skill_cas_runtime, SkillCasRuntimeConfig):
        raise TypeError("skill_cas_runtime must be a SkillCasRuntimeConfig or None")
    if agent_file_runtime is not None and not isinstance(
        agent_file_runtime, AgentFileRuntimeConfig
    ):
        raise TypeError("agent_file_runtime must be an AgentFileRuntimeConfig or None")
    return ManagedQueryService._create(
        factory_token=_SERVICE_FACTORY_TOKEN,
        registry=registry,
        query_store=query_store,
        journal_path=journal_path,
        lifecycle_lock_path=(store_path.parent / f".{store_path.name}.managed-lifecycle"),
        benefit_audit_path=benefit_audit_path,
        benefit_facts_port=benefit_facts_port,
        net_benefit_policy=net_benefit_policy,
        material_port=material_port,
        install_bundle_port=install_bundle_port,
        input_authority=input_authority,
        consent_broker=consent_broker,
        policy_store_root=policy_store_root,
        interactive_install_decision_guard=interactive_install_decision_guard,
        trusted_utc_now=trusted_utc_now,
        verifier_registry=verifier_registry,
        skill_cas_runtime=skill_cas_runtime,
        agent_file_runtime=agent_file_runtime,
    )


__all__ = [
    "ManagedActionSummary",
    "ManagedConsentChallengeProjection",
    "ManagedConsentResolutionResult",
    "ManagedDesiredSetBusyError",
    "ManagedDesiredSetConflictError",
    "ManagedDesiredSetRequest",
    "ManagedDesiredSetResult",
    "ManagedDesiredSetSupersededError",
    "ManagedQueryHeadDriftError",
    "ManagedQueryInput",
    "ManagedQueryInputAuthority",
    "ManagedQueryRequest",
    "ManagedQueryService",
    "ManagedQueryServiceError",
    "ManagedQueryServiceResult",
    "ManagedQuerySupersededError",
    "open_managed_query_service",
]
