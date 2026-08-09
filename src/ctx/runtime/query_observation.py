"""One-use typed observation handoff for query-only runtime planning.

This registry accepts only an already-sanitized :class:`WorkObservation`. It
never accepts source prompts, code, diffs, repository paths, or other raw host
material. Registered values remain process-local until exact digest-bound
resolution, explicit discard, or close.
"""

from __future__ import annotations

import hmac
import secrets
import threading
from dataclasses import dataclass

from ctx.engine.planner import WorkObservation
from ctx.engine.replay import ObservationReference, StructuredSurrogate
from ctx.engine.state import EngineState


DEFAULT_MAX_PENDING_QUERY_OBSERVATIONS = 64
_OPAQUE_ID_ATTEMPTS = 8
_VALIDATION_DIGEST = "0" * 64


class QueryObservationRegistryError(RuntimeError):
    """Base error for the process-local query observation boundary."""


class QueryObservationCapacityExceeded(QueryObservationRegistryError):
    """The bounded registry cannot accept another observation."""


class QueryObservationRegistryClosed(QueryObservationRegistryError):
    """Registration was attempted after the registry was closed."""


class QueryObservationUnavailable(QueryObservationRegistryError):
    """An exact unconsumed observation reference is not available."""


@dataclass(frozen=True, slots=True)
class _RegisteredObservation:
    observation: WorkObservation
    content_digest: str


def _current_work_surrogate(observation: WorkObservation) -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": observation.signals,
            "languages": observation.languages,
            "baseline_capability_ids": observation.baseline_capability_ids,
            "active_capability_ids": observation.active_capability_ids,
            "rejected_capability_ids": observation.rejected_capability_ids,
            "requested_limit": observation.requested_limit,
        },
    )


class QueryObservationRegistry:
    """Bounded, lock-safe one-use registry implementing ObservationNormalizer."""

    __slots__ = ("_closed", "_lock", "_max_pending", "_pending", "_provider_id")

    def __init__(
        self,
        *,
        provider_id: str = "ctx-query-observation-v1",
        max_pending: int = DEFAULT_MAX_PENDING_QUERY_OBSERVATIONS,
    ) -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be an integer >= 1")
        try:
            validated = ObservationReference(
                provider_id=provider_id,
                opaque_id="provider-validation",
                content_digest=_VALIDATION_DIGEST,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("provider_id must be an opaque safe token") from exc
        self._provider_id = validated.provider_id
        self._max_pending = max_pending
        self._pending: dict[str, _RegisteredObservation] = {}
        self._closed = False
        self._lock = threading.Lock()

    def register(self, observation: WorkObservation) -> ObservationReference:
        """Register one canonical observation and return its opaque exact reference."""

        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        surrogate = _current_work_surrogate(observation)
        entry = _RegisteredObservation(
            observation=observation,
            content_digest=surrogate.value_digest,
        )
        with self._lock:
            if self._closed:
                raise QueryObservationRegistryClosed("query observation registry is closed")
            if len(self._pending) >= self._max_pending:
                raise QueryObservationCapacityExceeded(
                    "query observation registry reached its bounded capacity"
                )
            opaque_id = self._new_opaque_id_locked()
            self._pending[opaque_id] = entry
        return ObservationReference(
            provider_id=self._provider_id,
            opaque_id=opaque_id,
            content_digest=entry.content_digest,
        )

    def __call__(
        self,
        reference: ObservationReference,
        state: EngineState | None,
    ) -> StructuredSurrogate:
        """Resolve and atomically consume one exact registered observation."""

        del state
        if not isinstance(reference, ObservationReference):
            raise TypeError("reference must be an ObservationReference")
        with self._lock:
            if self._closed or reference.provider_id != self._provider_id:
                raise QueryObservationUnavailable("query observation reference is unavailable")
            entry = self._pending.get(reference.opaque_id)
            if entry is None or not hmac.compare_digest(
                reference.content_digest,
                entry.content_digest,
            ):
                raise QueryObservationUnavailable("query observation reference is unavailable")
            surrogate = _current_work_surrogate(entry.observation)
            if not hmac.compare_digest(surrogate.value_digest, entry.content_digest):
                raise QueryObservationUnavailable("query observation reference is unavailable")
            del self._pending[reference.opaque_id]
            return surrogate

    def discard(self, reference: ObservationReference) -> bool:
        """Discard one exact pending reference; return whether it was removed."""

        if not isinstance(reference, ObservationReference):
            raise TypeError("reference must be an ObservationReference")
        with self._lock:
            if self._closed or reference.provider_id != self._provider_id:
                return False
            entry = self._pending.get(reference.opaque_id)
            if entry is None or not hmac.compare_digest(
                reference.content_digest,
                entry.content_digest,
            ):
                return False
            del self._pending[reference.opaque_id]
            return True

    def close(self) -> None:
        """Clear all pending observations and permanently close the registry."""

        with self._lock:
            self._pending.clear()
            self._closed = True

    def _new_opaque_id_locked(self) -> str:
        for _ in range(_OPAQUE_ID_ATTEMPTS):
            opaque_id = secrets.token_hex(16)
            if opaque_id not in self._pending:
                return opaque_id
        raise QueryObservationRegistryError("could not allocate an opaque observation reference")


__all__ = [
    "DEFAULT_MAX_PENDING_QUERY_OBSERVATIONS",
    "QueryObservationCapacityExceeded",
    "QueryObservationRegistry",
    "QueryObservationRegistryClosed",
    "QueryObservationRegistryError",
    "QueryObservationUnavailable",
]
