from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import stat
import threading
from contextlib import AbstractContextManager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ctx.core.install_consent_broker_store as broker_store_module
from ctx.core.install_consent_broker_store import (
    ConsentBrokerCapacityExceeded,
    ConsentBrokerCorruption,
    ConsentBrokerDecisionRejected,
    ConsentBrokerExpired,
    ConsentBrokerReplay,
    HumanDecisionVerifier,
    InstallConsentChallenge,
    SQLiteInstallConsentBrokerStore,
    SignedHumanDecisionAssertion,
    VerifiedHumanDecision,
)
from ctx.engine.installation import InteractiveInstallDecisionReservation
from ctx.engine.protocol import ScopeRef
from ctx.utils._file_lock import secure_file_lock


NOW = datetime(2035, 1, 2, 3, 4, 5, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
SECRET = b"deterministic-test-only-verifier-key"


class _TestOnlyHmacVerifier(HumanDecisionVerifier):
    """Deterministic test verifier; production composition cannot import it."""

    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        expected = hmac.digest(SECRET, signing_bytes, "sha256")
        return hmac.compare_digest(assertion.proof, expected)


class _TestOnlyTruthyTextVerifier:
    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        del assertion, signing_bytes
        return "yes, install it"  # type: ignore[return-value]


def _scope(**changes: str | None) -> ScopeRef:
    values: dict[str, str | None] = {
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "repository_id": "repository-1",
        "session_id": "session-1",
        "exposure_id": "exposure-1",
        "host_context_id": "host-1",
        "parent_exposure_id": None,
    }
    values.update(changes)
    return ScopeRef(**values)  # type: ignore[arg-type]


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _challenge(**changes: object) -> InstallConsentChallenge:
    values: dict[str, object] = {
        "challenge_id": "consent-1",
        "audience": "ctx-install-consent-v1",
        "workspace_identity_digest": _digest("workspace"),
        "scope": _scope(),
        "capability_id": "skill:example",
        "kind": "skill",
        "source_digest": _digest("source"),
        "catalog_snapshot_digest": _digest("catalog"),
        "plan_id": "plan-1",
        "install_plan_digest": _digest("install-plan"),
        "descriptor_digest": _digest("descriptor"),
        "execution_binding_digest": _digest("execution-binding"),
        "selection_digest": _digest("selection"),
        "material_identity_digest": _digest("material"),
        "requested_action_id": "action-1",
        "requested_action_kind": "InstallCapability",
        "requested_action_content_digest": _digest("action-content"),
        "requested_action_precondition_revision": 3,
        "policy_snapshot_digest": _digest("policy"),
        "release_root_digest": _digest("release-root"),
        "permission_expansion": False,
        "credential_requirement": False,
        "expires_at": "2035-01-02T03:09:05Z",
    }
    values.update(changes)
    return InstallConsentChallenge(**values)  # type: ignore[arg-type]


def _unsigned_assertion(
    challenge: InstallConsentChallenge,
    *,
    decision: str = "granted",
    nonce: str = "nonce-1",
    expires_at: str = "2035-01-02T03:08:05Z",
    proof: bytes = b"placeholder",
) -> SignedHumanDecisionAssertion:
    return SignedHumanDecisionAssertion(
        challenge_digest=challenge.challenge_digest,
        decision=decision,
        principal_digest=_digest("principal"),
        authenticator_id="test-authenticator",
        audience="ctx-install-consent-v1",
        nonce=nonce,
        issued_at="2035-01-02T03:03:05Z",
        expires_at=expires_at,
        proof=proof,
    )


def _assertion(
    challenge: InstallConsentChallenge,
    *,
    decision: str = "granted",
    nonce: str = "nonce-1",
    expires_at: str = "2035-01-02T03:08:05Z",
) -> SignedHumanDecisionAssertion:
    unsigned = _unsigned_assertion(
        challenge,
        decision=decision,
        nonce=nonce,
        expires_at=expires_at,
    )
    return replace(unsigned, proof=hmac.digest(SECRET, unsigned.signing_bytes(), "sha256"))


def _reservation(
    challenge: InstallConsentChallenge,
    *,
    decision: str = "granted",
    event_id: str = "decision-event-1",
    event_content_digest: str | None = None,
    **changes: object,
) -> InteractiveInstallDecisionReservation:
    values: dict[str, object] = {
        "scope": challenge.scope,
        "event_id": event_id,
        "event_content_digest": event_content_digest or _digest(event_id),
        "consent_id": challenge.challenge_id,
        "decision": decision,
        "policy_snapshot_digest": challenge.policy_snapshot_digest,
        "requested_action_id": challenge.requested_action_id,
        "requested_action_kind": challenge.requested_action_kind,
        "requested_action_content_digest": challenge.requested_action_content_digest,
        "requested_action_precondition_revision": (
            challenge.requested_action_precondition_revision
        ),
        "install_expires_at": challenge.expires_at,
    }
    values.update(changes)
    return InteractiveInstallDecisionReservation(**values)  # type: ignore[arg-type]


def _store(tmp_path: Path, *, capacity: int = 32) -> SQLiteInstallConsentBrokerStore:
    return SQLiteInstallConsentBrokerStore(
        tmp_path / "private" / "consent.sqlite3",
        audience="ctx-install-consent-v1",
        capacity=capacity,
    )


def _ready(
    store: SQLiteInstallConsentBrokerStore,
    challenge: InstallConsentChallenge,
    *,
    decision: str = "granted",
) -> VerifiedHumanDecision:
    store.create_challenge(challenge, now=NOW)
    verified = store.verify_human_decision(
        challenge.challenge_id,
        _assertion(challenge, decision=decision),
        _TestOnlyHmacVerifier(),
        now=NOW,
    )
    store.mark_decision_ready(verified, now=NOW)
    return verified


def test_challenge_is_immutable_and_digest_binds_every_authority_field() -> None:
    challenge = _challenge()
    with pytest.raises(FrozenInstanceError):
        challenge.capability_id = "changed"  # type: ignore[misc]

    mutations: dict[str, object] = {
        "challenge_id": "consent-2",
        "audience": "other-audience",
        "workspace_identity_digest": _digest("other-workspace"),
        "scope": _scope(host_context_id="host-2"),
        "capability_id": "skill:other",
        "source_digest": _digest("other-source"),
        "catalog_snapshot_digest": _digest("other-catalog"),
        "plan_id": "plan-2",
        "install_plan_digest": _digest("other-install-plan"),
        "descriptor_digest": _digest("other-descriptor"),
        "execution_binding_digest": _digest("other-execution-binding"),
        "selection_digest": _digest("other-selection"),
        "material_identity_digest": _digest("other-material"),
        "requested_action_id": "action-2",
        "requested_action_content_digest": _digest("other-action"),
        "requested_action_precondition_revision": 4,
        "policy_snapshot_digest": _digest("other-policy"),
        "release_root_digest": _digest("other-root"),
        "permission_expansion": True,
        "credential_requirement": True,
        "expires_at": "2035-01-02T03:10:05Z",
    }
    for field_name, value in mutations.items():
        changed = replace(challenge, **{field_name: value})  # type: ignore[arg-type]
        assert changed.challenge_digest != challenge.challenge_digest, field_name
    changed_kind = replace(challenge, capability_id="agent:other", kind="agent")
    assert changed_kind.challenge_digest != challenge.challenge_digest


def test_canonical_fractional_timestamps_strip_trailing_zeroes() -> None:
    challenge = _challenge(expires_at="2035-01-02T03:09:05.12345Z")
    assertion = SignedHumanDecisionAssertion(
        challenge_digest=challenge.challenge_digest,
        decision="granted",
        principal_digest=_digest("principal"),
        authenticator_id="test-authenticator",
        audience="ctx-install-consent-v1",
        nonce="nonce-fractional",
        issued_at="2035-01-02T03:03:05.12345Z",
        expires_at="2035-01-02T03:08:05.12345Z",
        proof=b"proof",
    )

    assert challenge.expires_at.endswith(".12345Z")
    assert b".12345Z" in assertion.signing_bytes()
    with pytest.raises(ValueError, match="canonical UTC"):
        _challenge(expires_at="2035-01-02T03:09:05.123450Z")


def test_public_challenge_id_and_plain_values_are_not_bearer_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)

    with pytest.raises(TypeError, match="VerifiedHumanDecision"):
        store.mark_decision_ready(challenge.challenge_id, now=NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="VerifiedHumanDecision"):
        store.interactive_guard(challenge.challenge_id, now=lambda: NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        VerifiedHumanDecision()  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="SignedHumanDecisionAssertion"):
        store.verify_human_decision(
            challenge.challenge_id,
            "yes, install it",  # type: ignore[arg-type]
            _TestOnlyHmacVerifier(),
            now=NOW,
        )
    with pytest.raises(ConsentBrokerDecisionRejected, match="signature"):
        store.verify_human_decision(
            challenge.challenge_id,
            _assertion(challenge),
            _TestOnlyTruthyTextVerifier(),
            now=NOW,
        )
    assert store.get(challenge.challenge_id, now=NOW).state == "pending"


def test_bad_or_misbinding_signed_assertions_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)

    bad_proof = _unsigned_assertion(challenge, proof=b"not-a-valid-proof")
    with pytest.raises(ConsentBrokerDecisionRejected, match="signature"):
        store.verify_human_decision(
            challenge.challenge_id, bad_proof, _TestOnlyHmacVerifier(), now=NOW
        )

    wrong_audience = replace(_assertion(challenge), audience="other-audience")
    with pytest.raises(ConsentBrokerDecisionRejected, match="audience"):
        store.verify_human_decision(
            challenge.challenge_id, wrong_audience, _TestOnlyHmacVerifier(), now=NOW
        )

    wrong_challenge = replace(_assertion(challenge), challenge_digest=_digest("wrong"))
    with pytest.raises(ConsentBrokerDecisionRejected, match="challenge"):
        store.verify_human_decision(
            challenge.challenge_id, wrong_challenge, _TestOnlyHmacVerifier(), now=NOW
        )
    assert store.get(challenge.challenge_id, now=NOW).state == "pending"


def test_exact_lifecycle_reserves_and_settles_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    verified = _ready(store, challenge)
    reservation = _reservation(challenge)
    guard = store.interactive_guard(verified, now=lambda: NOW)

    with guard(reservation):
        record = store.get(challenge.challenge_id, now=NOW)
        assert record.state == "reserved"
        assert record.reservation_event_content_digest == reservation.event_content_digest

    record = store.get(challenge.challenge_id, now=NOW)
    assert record.state == "settled"
    assert record.decision == "granted"
    assert record.principal_digest == _digest("principal")
    with pytest.raises(ConsentBrokerReplay):
        with guard(reservation):
            pass


def test_denial_uses_the_same_one_shot_lifecycle(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    verified = _ready(store, challenge, decision="denied")

    with store.interactive_guard(verified, now=lambda: NOW)(
        _reservation(challenge, decision="denied")
    ):
        pass

    assert store.get(challenge.challenge_id, now=NOW).state == "settled"


def test_exception_releases_exact_reservation_for_safe_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    verified = _ready(store, challenge)
    reservation = _reservation(challenge)
    guard = store.interactive_guard(verified, now=lambda: NOW)

    with pytest.raises(RuntimeError, match="journal failed"):
        with guard(reservation):
            assert store.get(challenge.challenge_id, now=NOW).state == "reserved"
            raise RuntimeError("journal failed")
    assert store.get(challenge.challenge_id, now=NOW).state == "decision-ready"

    with guard(reservation):
        pass
    assert store.get(challenge.challenge_id, now=NOW).state == "settled"


def test_concurrent_or_nested_reservation_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    verified = _ready(store, challenge)
    reservation = _reservation(challenge)
    guard = store.interactive_guard(verified, now=lambda: NOW)

    first: AbstractContextManager[None] = guard(reservation)
    first.__enter__()
    try:
        with pytest.raises(ConsentBrokerReplay, match="reserved"):
            with guard(reservation):
                pass
    finally:
        first.__exit__(RuntimeError, RuntimeError("abort"), None)


def test_recovery_never_reopens_or_expires_a_held_or_settled_reservation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    verified = _ready(store, challenge)
    reservation = _reservation(challenge)
    current = NOW
    manager = store.interactive_guard(verified, now=lambda: current)(reservation)
    manager.__enter__()

    current = LATER
    report = store.recover(now=current)
    assert report.expired == 0
    assert report.retained_reserved == 1
    assert store.get(challenge.challenge_id, now=current).state == "reserved"
    manager.__exit__(None, None, None)
    assert store.get(challenge.challenge_id, now=current).state == "settled"

    assert store.recover(now=current).expired == 0
    with pytest.raises(ConsentBrokerReplay, match="settled"):
        store.verify_human_decision(
            challenge.challenge_id,
            _assertion(challenge),
            _TestOnlyHmacVerifier(),
            now=current,
        )
    assert store.get(challenge.challenge_id, now=current).state == "settled"


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("scope", _scope(session_id="other-session")),
        ("consent_id", "consent-2"),
        ("decision", "denied"),
        ("policy_snapshot_digest", _digest("other-policy")),
        ("requested_action_id", "other-action"),
        ("requested_action_content_digest", _digest("other-content")),
        ("requested_action_precondition_revision", 4),
        ("install_expires_at", "2035-01-02T03:10:05Z"),
    ],
)
def test_reservation_must_match_the_exact_challenge(
    tmp_path: Path, field_name: str, replacement: object
) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    verified = _ready(store, challenge)
    reservation = _reservation(
        challenge,
        **{field_name: replacement},  # type: ignore[arg-type]
    )

    with pytest.raises(ConsentBrokerDecisionRejected, match="reservation"):
        with store.interactive_guard(verified, now=lambda: NOW)(reservation):
            pass
    assert store.get(challenge.challenge_id, now=NOW).state == "decision-ready"


def test_expiry_is_terminal_but_ready_decisions_can_be_reverified_after_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private" / "consent.sqlite3"
    store = SQLiteInstallConsentBrokerStore(path, audience="ctx-install-consent-v1")
    challenge = _challenge()
    verified = _ready(store, challenge)

    reopened = SQLiteInstallConsentBrokerStore(path, audience="ctx-install-consent-v1")
    reverified = reopened.verify_human_decision(
        challenge.challenge_id,
        _assertion(challenge, nonce="nonce-2"),
        _TestOnlyHmacVerifier(),
        now=NOW,
    )
    with reopened.interactive_guard(reverified, now=lambda: NOW)(_reservation(challenge)):
        pass
    assert reopened.get(challenge.challenge_id, now=NOW).state == "settled"

    expired = _challenge(challenge_id="consent-expired")
    store.create_challenge(expired, now=NOW)
    report = store.recover(now=LATER)
    assert report.expired == 1
    assert store.get(expired.challenge_id, now=LATER).state == "expired"
    with pytest.raises(ConsentBrokerExpired):
        store.verify_human_decision(
            expired.challenge_id,
            _assertion(expired),
            _TestOnlyHmacVerifier(),
            now=LATER,
        )
    # The old process-bound value cannot authorize through a reopened store.
    with pytest.raises(ConsentBrokerDecisionRejected, match="process-bound"):
        reopened.interactive_guard(verified, now=lambda: NOW)


def test_expired_assertion_and_post_expiry_creation_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)
    assertion = _assertion(challenge, expires_at="2035-01-02T03:04:05Z")
    with pytest.raises(ConsentBrokerExpired, match="assertion"):
        store.verify_human_decision(
            challenge.challenge_id,
            assertion,
            _TestOnlyHmacVerifier(),
            now=NOW,
        )

    with pytest.raises(ConsentBrokerExpired, match="challenge"):
        store.create_challenge(
            _challenge(challenge_id="already-expired", expires_at="2035-01-02T03:04:05Z"),
            now=NOW,
        )


def test_failed_expired_attempt_persists_terminal_expiry_across_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)

    with pytest.raises(ConsentBrokerExpired):
        store.verify_human_decision(
            challenge.challenge_id,
            _assertion(challenge),
            _TestOnlyHmacVerifier(),
            now=LATER,
        )

    reopened = SQLiteInstallConsentBrokerStore(store.path, audience="ctx-install-consent-v1")
    assert reopened.get(challenge.challenge_id, now=LATER).state == "expired"


def test_late_idempotent_prepare_persists_expiry_against_regressed_service_clock(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)
    late_service = SQLiteInstallConsentBrokerStore(
        store.path,
        audience="ctx-install-consent-v1",
    )

    with pytest.raises(ConsentBrokerExpired):
        late_service.create_challenge(challenge, now=LATER)

    regressed_service = SQLiteInstallConsentBrokerStore(
        store.path,
        audience="ctx-install-consent-v1",
    )
    assert regressed_service.get(challenge.challenge_id, now=NOW).state == "expired"
    with pytest.raises(ConsentBrokerExpired):
        regressed_service.verify_human_decision(
            challenge.challenge_id,
            _assertion(challenge),
            _TestOnlyHmacVerifier(),
            now=NOW,
        )


def test_trusted_time_high_water_is_durable_and_identity_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)
    after_assertion_expiry = NOW + timedelta(minutes=4, seconds=30)

    with pytest.raises(ConsentBrokerExpired, match="assertion"):
        store.verify_human_decision(
            challenge.challenge_id,
            _assertion(challenge),
            _TestOnlyHmacVerifier(),
            now=after_assertion_expiry,
        )

    reopened = SQLiteInstallConsentBrokerStore(
        store.path,
        audience="ctx-install-consent-v1",
    )
    with pytest.raises(ConsentBrokerExpired, match="assertion"):
        reopened.verify_human_decision(
            challenge.challenge_id,
            _assertion(challenge),
            _TestOnlyHmacVerifier(),
            now=NOW,
        )

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE consent_broker_identity SET trusted_time_high_water = ?",
            ("2035-01-02T03:04:05Z",),
        )
    with pytest.raises(ConsentBrokerCorruption, match="identity digest"):
        SQLiteInstallConsentBrokerStore(
            store.path,
            audience="ctx-install-consent-v1",
        )


def test_status_read_expires_challenge_but_requires_fresh_auth_for_decision_ttl(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    pending = _challenge(challenge_id="pending-expiry")
    store.create_challenge(pending, now=NOW)

    assert store.get(pending.challenge_id, now=LATER).state == "expired"
    reopened = SQLiteInstallConsentBrokerStore(store.path, audience=pending.audience)
    assert reopened.get(pending.challenge_id, now=LATER).state == "expired"

    ready = _challenge(challenge_id="decision-expiry", expires_at="2035-01-02T03:12:05Z")
    verified = _ready(store, ready)
    decision_expiry = datetime(2035, 1, 2, 3, 8, 5, tzinfo=UTC)
    reauthentication = store.get(ready.challenge_id, now=decision_expiry)
    assert reauthentication.state == "reauthentication-required"
    assert reauthentication.decision == verified.decision
    assert reauthentication.expired_at is None

    with pytest.raises(ConsentBrokerDecisionRejected, match="fresh nonce"):
        store.verify_human_decision(
            ready.challenge_id,
            _assertion(ready),
            _TestOnlyHmacVerifier(),
            now=decision_expiry,
        )
    refreshed = store.verify_human_decision(
        ready.challenge_id,
        _assertion(
            ready,
            nonce="decision-expiry-fresh-nonce",
            expires_at="2035-01-02T03:11:05Z",
        ),
        _TestOnlyHmacVerifier(),
        now=decision_expiry,
    )
    assert store.mark_decision_ready(refreshed, now=decision_expiry).state == "decision-ready"


def test_recovery_sweep_keeps_live_challenge_reauthenticatable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge(
        challenge_id="sweep-decision-expiry",
        expires_at="2035-01-02T03:12:05Z",
    )
    _ready(store, challenge)

    report = store.recover(now=datetime(2035, 1, 2, 3, 8, 5, tzinfo=UTC))

    assert report.expired == 0
    record = store.inspect_record(challenge.challenge_id)
    assert record.state == "reauthentication-required"
    assert record.decision == "granted"
    assert record.expired_at is None


def test_legacy_decision_expiry_tombstone_is_migrated_while_challenge_is_live(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    challenge = _challenge(
        challenge_id="legacy-decision-expiry",
        expires_at="2035-01-02T03:12:05Z",
    )
    _ready(store, challenge)
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM consent_challenges WHERE challenge_id = ?",
            (challenge.challenge_id,),
        ).fetchone()
        assert row is not None
        values = dict(row)
        values["state"] = "expired"
        values["expired_at"] = "2035-01-02T03:08:05Z"
        values["record_digest"] = broker_store_module._record_digest(values)
        connection.execute(
            "UPDATE consent_challenges SET state = ?, expired_at = ?, record_digest = ? "
            "WHERE challenge_id = ?",
            (
                values["state"],
                values["expired_at"],
                values["record_digest"],
                challenge.challenge_id,
            ),
        )

    report = store.recover(now=datetime(2035, 1, 2, 3, 8, 5, tzinfo=UTC))

    assert report.expired == 0
    migrated = store.inspect_record(challenge.challenge_id)
    assert migrated.state == "reauthentication-required"
    assert migrated.expired_at is None
    assert migrated.decision == "granted"


def test_reserve_samples_clock_only_after_lock_and_expires_durably(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    verified = _ready(store, challenge)
    reservation = _reservation(challenge)
    current = [NOW]
    attempted = threading.Event()
    clock_called = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def trusted_clock() -> datetime:
        clock_called.set()
        return current[0]

    manager = store.interactive_guard(verified, now=trusted_clock)(reservation)

    def enter_guard() -> None:
        attempted.set()
        try:
            manager.__enter__()
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    with secure_file_lock(store.path):
        worker = threading.Thread(target=enter_guard, daemon=True)
        worker.start()
        assert attempted.wait(2)
        assert not clock_called.wait(0.2)
        current[0] = LATER

    assert finished.wait(5)
    worker.join(timeout=1)
    assert len(failures) == 1
    assert isinstance(failures[0], ConsentBrokerExpired)
    assert store.get(challenge.challenge_id, now=LATER).state == "expired"


def test_assertion_nonce_cannot_be_reused_across_challenges(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _challenge(challenge_id="consent-1")
    second = _challenge(challenge_id="consent-2")
    store.create_challenge(first, now=NOW)
    store.create_challenge(second, now=NOW)
    first_decision = store.verify_human_decision(
        first.challenge_id,
        _assertion(first, nonce="shared-nonce"),
        _TestOnlyHmacVerifier(),
        now=NOW,
    )
    second_decision = store.verify_human_decision(
        second.challenge_id,
        _assertion(second, nonce="shared-nonce"),
        _TestOnlyHmacVerifier(),
        now=NOW,
    )
    store.mark_decision_ready(first_decision, now=NOW)

    with pytest.raises(ConsentBrokerReplay, match="nonce"):
        store.mark_decision_ready(second_decision, now=NOW)
    assert store.get(second.challenge_id, now=NOW).state == "pending"


def test_store_is_owner_private_bounded_and_never_persists_sensitive_input(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, capacity=1)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)
    assertion = _assertion(challenge)
    verified = store.verify_human_decision(
        challenge.challenge_id,
        assertion,
        _TestOnlyHmacVerifier(),
        now=NOW,
    )
    store.mark_decision_ready(verified, now=NOW)

    with pytest.raises(ConsentBrokerCapacityExceeded):
        store.create_challenge(_challenge(challenge_id="consent-2"), now=NOW)

    path = store.path
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    persisted = path.read_bytes()
    assert assertion.proof not in persisted
    assert assertion.nonce.encode() not in persisted
    assert b"a secret prompt body" not in persisted
    assert b"/Users/example/private/workspace" not in persisted
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(consent_challenges)")}
    assert not {"prompt", "body", "credential", "proof", "absolute_path"} & columns


def test_same_challenge_is_idempotent_but_id_collision_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)
    store.create_challenge(challenge, now=NOW)

    with pytest.raises(ConsentBrokerReplay, match="different challenge"):
        store.create_challenge(
            _challenge(source_digest=_digest("different-source")),
            now=NOW,
        )


def test_corrupt_schema_and_corrupt_row_are_detected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)

    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE consent_challenges SET challenge_digest = ? WHERE challenge_id = ?",
            (_digest("tampered"), challenge.challenge_id),
        )
    with pytest.raises(ConsentBrokerCorruption, match="digest"):
        store.get(challenge.challenge_id, now=NOW)

    other_path = tmp_path / "other" / "consent.sqlite3"
    other_path.parent.mkdir(mode=0o700)
    with sqlite3.connect(other_path) as connection:
        connection.execute("CREATE TABLE attacker(value TEXT)")
    if os.name != "nt":
        other_path.chmod(0o600)
    with pytest.raises(ConsentBrokerCorruption, match="schema"):
        SQLiteInstallConsentBrokerStore(other_path, audience="ctx-install-consent-v1")


def test_store_and_challenge_are_durably_bound_to_one_audience(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)

    with pytest.raises(ConsentBrokerDecisionRejected, match="audience"):
        store.create_challenge(replace(challenge, audience="other-audience"), now=NOW)
    with pytest.raises(ConsentBrokerDecisionRejected, match="audience"):
        SQLiteInstallConsentBrokerStore(store.path, audience="other-audience")

    reopened = SQLiteInstallConsentBrokerStore(store.path, audience=challenge.audience)
    assert reopened.get(challenge.challenge_id, now=NOW).challenge == challenge


def test_bounded_challenge_and_assertion_fields_fail_before_persistence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="capability_id"):
        _challenge(capability_id="s" * 300)
    with pytest.raises(ValueError, match="proof"):
        _unsigned_assertion(_challenge(), proof=b"p" * 8193)
    with pytest.raises(ValueError, match="nonce"):
        replace(_unsigned_assertion(_challenge()), nonce="n" * 257)
    with pytest.raises(ValueError, match="audience"):
        SQLiteInstallConsentBrokerStore(
            tmp_path / "private" / "consent.sqlite3",
            audience="a" * 129,
        )


def test_relative_database_path_is_rejected_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        SQLiteInstallConsentBrokerStore(
            Path("relative/consent.sqlite3"), audience="ctx-install-consent-v1"
        )
    assert not (tmp_path / "relative").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem integrity contract")
def test_symlinked_or_hardlinked_database_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    real_db = real / "real.sqlite3"
    SQLiteInstallConsentBrokerStore(real_db, audience="ctx-install-consent-v1")

    symlink_parent = tmp_path / "symlink-parent"
    symlink_parent.symlink_to(real, target_is_directory=True)
    with pytest.raises((ValueError, OSError), match="symlink|real directory|authenticated"):
        SQLiteInstallConsentBrokerStore(
            symlink_parent / "other.sqlite3", audience="ctx-install-consent-v1"
        )

    link_root = tmp_path / "link-root"
    link_root.mkdir(mode=0o700)
    hardlink = link_root / "linked.sqlite3"
    os.link(real_db, hardlink)
    with pytest.raises((ValueError, OSError), match="link|regular file|authenticated"):
        SQLiteInstallConsentBrokerStore(hardlink, audience="ctx-install-consent-v1")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_insecure_existing_parent_or_database_mode_is_rejected(tmp_path: Path) -> None:
    insecure_root = tmp_path / "insecure"
    insecure_root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="owner-private"):
        SQLiteInstallConsentBrokerStore(
            insecure_root / "consent.sqlite3", audience="ctx-install-consent-v1"
        )

    secure_root = tmp_path / "secure"
    path = secure_root / "consent.sqlite3"
    SQLiteInstallConsentBrokerStore(path, audience="ctx-install-consent-v1")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-private"):
        SQLiteInstallConsentBrokerStore(path, audience="ctx-install-consent-v1")


@pytest.mark.skipif(os.name == "nt", reason="POSIX sidecar inode contract")
@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_hardlinked_sqlite_sidecar_is_rejected_before_open_without_mutation(
    tmp_path: Path,
    suffix: str,
) -> None:
    store = _store(tmp_path)
    target = store.path.parent / f"attacker{suffix}"
    target.write_bytes(b"attacker-owned-sidecar-material")
    target.chmod(0o644)
    sidecar = Path(f"{store.path}{suffix}")
    os.link(target, sidecar)
    before = target.stat(follow_symlinks=False)

    with pytest.raises((ValueError, ConsentBrokerCorruption), match="sidecar|link"):
        SQLiteInstallConsentBrokerStore(store.path, audience=store.audience)

    after = target.stat(follow_symlinks=False)
    assert target.read_bytes() == b"attacker-owned-sidecar-material"
    assert sidecar.exists()
    assert os.path.samestat(before, after)
    assert after.st_nlink == 2
    assert stat.S_IMODE(after.st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX inode authentication contract")
def test_database_path_replacement_is_detected_on_next_authenticated_open(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    challenge = _challenge()
    store.create_challenge(challenge, now=NOW)
    replacement = store.path.with_name("replacement.sqlite3")
    SQLiteInstallConsentBrokerStore(replacement, audience="ctx-install-consent-v1")
    original = store.path.with_name("original.sqlite3")
    store.path.rename(original)
    replacement.rename(store.path)

    with pytest.raises((ConsentBrokerCorruption, ValueError), match="changed|binding|digest"):
        store.get(challenge.challenge_id, now=NOW)
