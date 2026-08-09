"""Trusted continuation boundary for durable managed install consent.

Untrusted host hooks may create and publish a durable challenge through the
prepare-only broker.  Resolving that challenge requires canonical signed bytes
and an immutable verifier registry supplied by trusted composition.  This API
accepts no prompt text, plain decision, challenge override, or per-request
verifier.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Lock
from typing import Final, NoReturn, SupportsIndex

from ctx.core.install_consent_broker_store import SQLiteInstallConsentBrokerStore
from ctx.runtime.install_consent_authenticators import (
    TrustedHumanDecisionVerifierRegistry,
    decode_signed_human_decision_assertion,
)
from ctx.runtime.install_consent_broker import InstallConsentBrokerService
from ctx.runtime.production_catalog import RELEASE_QUERY_CATALOG_ROOT_SHA256
from ctx.runtime.prompt_capability_manager import (
    ManagedPromptOutcome,
    reconcile_prompt_capabilities,
)
from ctx.runtime.release_skill_layout import ReleaseSkillRuntimeLayout


MANAGED_INSTALL_CONSENT_AUDIENCE: Final = "ctx-managed-install-consent-v1"
_CONTINUATION_FACTORY_TOKEN: Final = object()


def _require_layout(layout: ReleaseSkillRuntimeLayout) -> ReleaseSkillRuntimeLayout:
    if type(layout) is not ReleaseSkillRuntimeLayout:
        raise TypeError("layout must be an exact ReleaseSkillRuntimeLayout")
    layout.assert_current()
    if layout.consent_broker_path is None:
        raise ValueError("layout does not provide a durable consent broker path")
    return layout


class _PinnedTrustedClock:
    """Sample once on first use, which the manager performs under its lock."""

    __slots__ = ("_lock", "_source", "_value")

    def __init__(self, source: Callable[[], datetime]) -> None:
        if not callable(source):
            raise TypeError("trusted_utc_now must be callable")
        self._source = source
        self._value: datetime | None = None
        self._lock = Lock()

    def __call__(self) -> datetime:
        with self._lock:
            if self._value is None:
                try:
                    sampled = self._source()
                except Exception:
                    raise RuntimeError("trusted UTC clock failed") from None
                if (
                    not isinstance(sampled, datetime)
                    or sampled.tzinfo is None
                    or sampled.utcoffset() is None
                ):
                    raise RuntimeError("trusted UTC clock failed")
                self._value = sampled.astimezone(UTC)
            return self._value


class _TrustedClockFloor:
    """Issue per-request clocks without allowing trusted time to regress."""

    __slots__ = ("_floor", "_lock", "_source")

    def __init__(self, source: Callable[[], datetime]) -> None:
        if not callable(source):
            raise TypeError("trusted_utc_now must be callable")
        self._source = source
        self._floor: datetime | None = None
        self._lock = Lock()

    def pin_request(self) -> _PinnedTrustedClock:
        """Create a lazy request clock; construction never samples the source."""

        return _PinnedTrustedClock(self._sample_non_regressing)

    def _sample_non_regressing(self) -> datetime:
        with self._lock:
            try:
                sampled = self._source()
            except Exception:
                raise RuntimeError("trusted UTC clock failed") from None
            if (
                not isinstance(sampled, datetime)
                or sampled.tzinfo is None
                or sampled.utcoffset() is None
            ):
                raise RuntimeError("trusted UTC clock failed")
            normalized = sampled.astimezone(UTC)
            if self._floor is None or normalized > self._floor:
                self._floor = normalized
            return self._floor


def open_prepare_only_managed_install_consent_broker(
    *,
    layout: ReleaseSkillRuntimeLayout,
    trusted_utc_now: Callable[[], datetime],
) -> InstallConsentBrokerService:
    """Open the fixed-audience broker without assertion-verification authority."""

    current = _require_layout(layout)
    fixed_clock = _PinnedTrustedClock(trusted_utc_now)
    assert current.consent_broker_path is not None
    return InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(
            current.consent_broker_path,
            audience=MANAGED_INSTALL_CONSENT_AUDIENCE,
        ),
        verifier=None,
        workspace_identity_digest=current.workspace_identity_digest,
        release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
        trusted_utc_now=fixed_clock,
    )


class ManagedInstallConsentContinuationService:
    """Immutable trusted composition that accepts signed assertion bytes only."""

    __slots__ = ("_clock_floor", "_layout", "_pid", "_verifier_registry")
    _clock_floor: _TrustedClockFloor
    _layout: ReleaseSkillRuntimeLayout
    _pid: int
    _verifier_registry: TrustedHumanDecisionVerifierRegistry

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "managed install consent continuation services are opened by trusted composition"
        )

    @classmethod
    def _create(
        cls,
        *,
        token: object,
        layout: ReleaseSkillRuntimeLayout,
        verifier_registry: TrustedHumanDecisionVerifierRegistry,
        trusted_utc_now: Callable[[], datetime],
    ) -> ManagedInstallConsentContinuationService:
        if token is not _CONTINUATION_FACTORY_TOKEN:
            raise TypeError(
                "managed install consent continuation services are opened by trusted composition"
            )
        instance = object.__new__(cls)
        object.__setattr__(instance, "_layout", layout)
        object.__setattr__(instance, "_verifier_registry", verifier_registry)
        object.__setattr__(instance, "_clock_floor", _TrustedClockFloor(trusted_utc_now))
        object.__setattr__(instance, "_pid", os.getpid())
        return instance

    def resolve(self, assertion_payload: bytes) -> ManagedPromptOutcome:
        """Authenticate and resolve the exact pending workspace challenge."""

        if os.getpid() != self._pid:
            raise RuntimeError("install consent continuation cannot cross a process boundary")
        current = _require_layout(self._layout)
        assertion = decode_signed_human_decision_assertion(assertion_payload)
        if assertion.audience != MANAGED_INSTALL_CONSENT_AUDIENCE:
            raise ValueError("signed assertion has the wrong managed-install audience")
        verifier = self._verifier_registry.resolve(assertion)
        fixed_clock = self._clock_floor.pin_request()
        assert current.consent_broker_path is not None
        broker = InstallConsentBrokerService(
            store=SQLiteInstallConsentBrokerStore(
                current.consent_broker_path,
                audience=MANAGED_INSTALL_CONSENT_AUDIENCE,
            ),
            verifier=verifier,
            workspace_identity_digest=current.workspace_identity_digest,
            release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
            trusted_utc_now=fixed_clock,
        )
        return reconcile_prompt_capabilities(
            layout=current,
            task="",
            language="",
            consent_broker=broker,
            decision_assertion=assertion,
            trusted_utc_now=fixed_clock,
        )

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("managed install consent continuation service is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("managed install consent continuation service is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("managed install consent continuation service cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("managed install consent continuation service cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("managed install consent continuation service cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("managed install consent continuation service cannot be serialized")

    def __repr__(self) -> str:
        return "<managed-install-consent-continuation-service>"


def open_managed_install_consent_continuation(
    *,
    layout: ReleaseSkillRuntimeLayout,
    verifier_registry: TrustedHumanDecisionVerifierRegistry,
    trusted_utc_now: Callable[[], datetime],
) -> ManagedInstallConsentContinuationService:
    """Capture trusted verifier composition before handling assertion requests."""

    current = _require_layout(layout)
    if type(verifier_registry) is not TrustedHumanDecisionVerifierRegistry:
        raise TypeError("verifier_registry must be an exact TrustedHumanDecisionVerifierRegistry")
    if not callable(trusted_utc_now):
        raise TypeError("trusted_utc_now must be callable")
    return ManagedInstallConsentContinuationService._create(
        token=_CONTINUATION_FACTORY_TOKEN,
        layout=current,
        verifier_registry=verifier_registry,
        trusted_utc_now=trusted_utc_now,
    )


__all__ = [
    "MANAGED_INSTALL_CONSENT_AUDIENCE",
    "ManagedInstallConsentContinuationService",
    "open_managed_install_consent_continuation",
    "open_prepare_only_managed_install_consent_broker",
]
