from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ctx.core.install_consent_broker_store import (
    HumanDecisionVerifier,
    SignedHumanDecisionAssertion,
)
from ctx.runtime.install_consent_authenticators import (
    HumanDecisionVerifierRegistration,
    TrustedHumanDecisionVerifierRegistry,
    encode_signed_human_decision_assertion,
)
from ctx.runtime.install_consent_continuation import (
    MANAGED_INSTALL_CONSENT_AUDIENCE,
    ManagedInstallConsentContinuationService,
    open_managed_install_consent_continuation,
    open_prepare_only_managed_install_consent_broker,
)
from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities
from ctx.runtime.release_skill_layout import open_workspace_release_skill_runtime_layout


NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
KEY = b"continuation-test-only-human-authenticator-key"
PRINCIPAL_DIGEST = hashlib.sha256(b"authenticated-human").hexdigest()
TARGET_SHA256 = "c87c65b5b09f48e27c683fb5ada9d8bc377d6d72d7742ce7aac3c2d3d97ac441"


class _Verifier(HumanDecisionVerifier):
    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        return hmac.compare_digest(
            assertion.proof,
            hmac.digest(KEY, signing_bytes, "sha256"),
        )


def _layout(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return open_workspace_release_skill_runtime_layout(
        state_root=tmp_path / "state",
        policy_store_root=tmp_path / "policy",
        workspace=workspace,
    )


def _registry() -> TrustedHumanDecisionVerifierRegistry:
    return TrustedHumanDecisionVerifierRegistry(
        (
            HumanDecisionVerifierRegistration(
                audience=MANAGED_INSTALL_CONSENT_AUDIENCE,
                authenticator_id="test-passkey",
                principal_digest=PRINCIPAL_DIGEST,
                verifier=_Verifier(),
            ),
        )
    )


def _assertion(challenge_digest: str, decision: str) -> bytes:
    unsigned = SignedHumanDecisionAssertion(
        challenge_digest=challenge_digest,
        decision=decision,
        principal_digest=PRINCIPAL_DIGEST,
        authenticator_id="test-passkey",
        audience=MANAGED_INSTALL_CONSENT_AUDIENCE,
        nonce=f"continuation-{decision}-nonce",
        issued_at="2026-08-02T12:31:00Z",
        expires_at="2026-08-02T12:45:00Z",
        proof=b"unsigned",
    )
    return encode_signed_human_decision_assertion(
        replace(
            unsigned,
            proof=hmac.digest(KEY, unsigned.signing_bytes(), "sha256"),
        )
    )


def _continuation(
    layout: object,
    *,
    trusted_utc_now=lambda: NOW + timedelta(minutes=2),
) -> ManagedInstallConsentContinuationService:
    return open_managed_install_consent_continuation(
        layout=layout,  # type: ignore[arg-type]
        verifier_registry=_registry(),
        trusted_utc_now=trusted_utc_now,
    )


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
@pytest.mark.parametrize("decision", ("granted", "denied"))
def test_signed_continuation_resolves_durable_challenge_without_prompt_authority(
    tmp_path: Path,
    decision: str,
) -> None:
    layout = _layout(tmp_path)
    prepared = reconcile_prompt_capabilities(
        layout=layout,
        task="repair nested Python context manager state restoration",
        language="Python",
        consent_broker=open_prepare_only_managed_install_consent_broker(
            layout=layout,
            trusted_utc_now=lambda: NOW,
        ),
        trusted_utc_now=lambda: NOW,
    )
    assert prepared.status == "consent-required"
    challenge = prepared.consent_challenges[0]

    continuation = _continuation(layout)
    resolved = continuation.resolve(_assertion(challenge.challenge_digest, decision))

    assert resolved.status == ("available" if decision == "granted" else "denied")
    assert resolved.consent_challenges == ()
    assert (layout.skill_store_root / TARGET_SHA256).exists() is (decision == "granted")


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_continuation_rejects_prompt_text_and_unknown_authenticator(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    continuation = _continuation(layout, trusted_utc_now=lambda: NOW)
    with pytest.raises(ValueError):
        continuation.resolve(b"yes, install it")

    prepared = reconcile_prompt_capabilities(
        layout=layout,
        task="repair nested Python context manager state restoration",
        language="Python",
        consent_broker=open_prepare_only_managed_install_consent_broker(
            layout=layout,
            trusted_utc_now=lambda: NOW,
        ),
        trusted_utc_now=lambda: NOW,
    )
    challenge = prepared.consent_challenges[0]
    decoded = SignedHumanDecisionAssertion(
        challenge_digest=challenge.challenge_digest,
        decision="granted",
        principal_digest=hashlib.sha256(b"unknown-human").hexdigest(),
        authenticator_id="unknown-passkey",
        audience=MANAGED_INSTALL_CONSENT_AUDIENCE,
        nonce="unknown-authenticator-nonce",
        issued_at="2026-08-02T12:31:00Z",
        expires_at="2026-08-02T12:45:00Z",
        proof=b"signed-elsewhere",
    )
    with pytest.raises(LookupError, match="registered"):
        continuation.resolve(encode_signed_human_decision_assertion(decoded))
    assert not (layout.skill_store_root / TARGET_SHA256).exists()
    with pytest.raises(AttributeError, match="immutable"):
        continuation._verifier_registry = _registry()  # type: ignore[misc]


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_continuation_cannot_revive_expired_assertion_with_a_regressing_clock(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    prepared = reconcile_prompt_capabilities(
        layout=layout,
        task="repair nested Python context manager state restoration",
        language="Python",
        consent_broker=open_prepare_only_managed_install_consent_broker(
            layout=layout,
            trusted_utc_now=lambda: NOW,
        ),
        trusted_utc_now=lambda: NOW,
    )
    challenge = prepared.consent_challenges[0]
    clock_calls = 0

    def regressing_clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return (
            datetime(2026, 8, 2, 12, 46, tzinfo=UTC)
            if clock_calls == 1
            else datetime(2026, 8, 2, 12, 32, tzinfo=UTC)
        )

    continuation = _continuation(layout, trusted_utc_now=regressing_clock)
    assertion = _assertion(challenge.challenge_digest, "granted")

    first = continuation.resolve(assertion)
    second = continuation.resolve(assertion)

    assert first.status == "failed"
    assert second.status == "failed"
    assert not (layout.skill_store_root / TARGET_SHA256).exists()
    assert clock_calls == 2


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_continuation_samples_trusted_time_only_after_reconciliation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager

    layout = _layout(tmp_path)
    prepared = reconcile_prompt_capabilities(
        layout=layout,
        task="repair nested Python context manager state restoration",
        language="Python",
        consent_broker=open_prepare_only_managed_install_consent_broker(
            layout=layout,
            trusted_utc_now=lambda: NOW,
        ),
        trusted_utc_now=lambda: NOW,
    )
    challenge = prepared.consent_challenges[0]
    original_lock = manager.secure_file_lock
    reconciliation_lock_held = False

    @contextmanager
    def observed_lock(*args: object, **kwargs: object) -> Iterator[object]:
        nonlocal reconciliation_lock_held
        with original_lock(*args, **kwargs) as held:  # type: ignore[arg-type]
            reconciliation_lock_held = True
            try:
                yield held
            finally:
                reconciliation_lock_held = False

    def guarded_clock() -> datetime:
        assert reconciliation_lock_held
        return NOW + timedelta(minutes=2)

    monkeypatch.setattr(manager, "secure_file_lock", observed_lock)
    resolved = _continuation(layout, trusted_utc_now=guarded_clock).resolve(
        _assertion(challenge.challenge_digest, "denied")
    )

    assert resolved.status == "denied"
