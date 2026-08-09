from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctx.core.install_consent_broker_store import (
    ConsentBrokerDecisionRejected,
    ConsentBrokerExpired,
    ConsentBrokerReplay,
    HumanDecisionVerifier,
    InstallConsentChallenge,
    SQLiteInstallConsentBrokerStore,
    SignedHumanDecisionAssertion,
)
from ctx.engine.installation import (
    CommittedInstallDecisionEvidence,
    CommittedInstallDecisionEvidenceProvider,
    InstallConsentDirective,
    InstallDecisionEvidenceLookup,
    InstallDecisionEvidenceQuery,
    InstallConsentPolicy,
    InstallExecutionBinding,
    InteractiveInstallDecisionReservation,
)
from ctx.engine.engine import CtxEngine
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, InstallPlanningAuthority
from ctx.engine.protocol import EngineEvent, HostAction
from ctx.engine.store import SQLiteEngineStore
from ctx.runtime.install_consent_broker import (
    AuthenticatedInstallConsent,
    InstallConsentBrokerBindingError,
    InstallConsentBrokerService,
    InstallConsentReconciliationReport,
    InstallConsentVerifierUnavailable,
    derive_install_consent_challenge,
    install_consent_selection_digest,
)
from tests.engine import test_engine_install_coordinator as install_support
from tests.engine import test_install_decision_evidence as evidence_support


NOW = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
AUDIENCE = "ctx-install-consent-v1"
WORKSPACE_DIGEST = hashlib.sha256(b"canonical-workspace").hexdigest()
RELEASE_ROOT_DIGEST = hashlib.sha256(b"canonical-release-root").hexdigest()
VERIFIER_KEY = b"runtime-service-test-only-authenticator-key"


class _CountingEvidenceProvider(CommittedInstallDecisionEvidenceProvider):
    def __init__(
        self,
        lookup_store: SQLiteEngineStore,
        revalidation_store: SQLiteEngineStore | None = None,
    ) -> None:
        self.lookup_store = lookup_store
        self.revalidation_store = revalidation_store or lookup_store
        self.revalidations = 0

    def inspect_install_decision(
        self,
        query: InstallDecisionEvidenceQuery,
    ):
        return self.lookup_store.inspect_install_decision(query)

    def revalidate_install_decision_evidence(
        self,
        evidence: CommittedInstallDecisionEvidence,
        *,
        query: InstallDecisionEvidenceQuery,
    ) -> CommittedInstallDecisionEvidence:
        self.revalidations += 1
        return self.revalidation_store.revalidate_install_decision_evidence(
            evidence,
            query=query,
        )


class _StaticEvidenceProvider(CommittedInstallDecisionEvidenceProvider):
    def __init__(self, status: str) -> None:
        self.status = status

    @contextmanager
    def inspect_install_decision(
        self,
        query: InstallDecisionEvidenceQuery,
    ) -> Iterator[InstallDecisionEvidenceLookup]:
        del query
        yield InstallDecisionEvidenceLookup(status=self.status)  # type: ignore[arg-type]

    def revalidate_install_decision_evidence(
        self,
        evidence: CommittedInstallDecisionEvidence,
        *,
        query: InstallDecisionEvidenceQuery,
    ) -> CommittedInstallDecisionEvidence:
        del evidence, query
        raise AssertionError("indeterminate evidence must never be revalidated")


class _TestOnlyAuthenticatorVerifier(HumanDecisionVerifier):
    """Authenticator stand-in scoped to this test module only."""

    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        return hmac.compare_digest(
            assertion.proof,
            hmac.digest(VERIFIER_KEY, signing_bytes, "sha256"),
        )


def _directive(
    action: HostAction,
    selection: CapabilityPlanSelectionV3,
    policy: InstallConsentPolicy,
) -> InstallConsentDirective:
    authority = selection.authority
    descriptor = authority.descriptor  # type: ignore[union-attr]
    material = authority.result_material  # type: ignore[union-attr]
    assert action.entity_id is not None
    assert action.source_digest is not None
    assert action.catalog_snapshot_id is not None
    assert action.plan_id is not None
    assert action.consent_id is not None
    return InstallConsentDirective(
        consent_id=action.consent_id,
        capability_id=action.entity_id,
        kind=selection.presentation.kind,
        source_digest=action.source_digest,
        catalog_snapshot_digest=action.catalog_snapshot_id,
        plan_id=action.plan_id,
        install_plan_digest=descriptor.plan_digest,
        descriptor_digest=descriptor.descriptor_digest,
        installer_id=descriptor.installer_id,
        provenance_digest=descriptor.provenance_digest,
        permission_expansion=descriptor.permission_expansion,
        credential_requirement=descriptor.credential_requirement,
        decision_basis="interactive",
        policy_snapshot_digest=policy.policy_digest,
        reason_code="per-install-consent-required",
        requested_action_id=action.action_id,
        requested_action_kind=action.kind,
        requested_action_content_digest=action.content_digest,
        requested_action_precondition_revision=action.precondition_revision,
        result_material_identity_digest=material.identity_digest,
    )


def _fixture(
    tmp_path: Path,
) -> tuple[
    SQLiteInstallConsentBrokerStore,
    InstallConsentBrokerService,
    InstallConsentDirective,
    CapabilityPlanSelectionV3,
    HostAction,
    InstallExecutionBinding,
]:
    engine, policy = install_support._engine(tmp_path / "engine", decision=None)
    snapshot = engine.snapshot(install_support._scope())
    assert snapshot.state is not None and len(snapshot.state.pending_consents) == 1
    action = snapshot.state.pending_consents[0].install_action
    selection = install_support._selection()
    directive = _directive(action, selection, policy)
    binding = install_support._execution_binding()
    store = SQLiteInstallConsentBrokerStore(
        tmp_path / "broker" / "consent.sqlite3",
        audience=AUDIENCE,
    )
    service = InstallConsentBrokerService(
        store=store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )
    return store, service, directive, selection, action, binding


def _assertion(
    challenge_digest: str,
    *,
    decision: str = "granted",
    nonce: str = "runtime-nonce-1",
) -> SignedHumanDecisionAssertion:
    unsigned = SignedHumanDecisionAssertion(
        challenge_digest=challenge_digest,
        decision=decision,
        principal_digest=hashlib.sha256(b"authenticated-human").hexdigest(),
        authenticator_id="test-passkey",
        audience=AUDIENCE,
        nonce=nonce,
        issued_at="2026-08-01T12:29:00Z",
        expires_at="2026-08-01T12:45:00Z",
        proof=b"unsigned",
    )
    return replace(
        unsigned,
        proof=hmac.digest(VERIFIER_KEY, unsigned.signing_bytes(), "sha256"),
    )


def _reservation(
    authorization: AuthenticatedInstallConsent,
    *,
    event_id: str = "human-decision-event-1",
) -> InteractiveInstallDecisionReservation:
    challenge = authorization.challenge
    return InteractiveInstallDecisionReservation(
        scope=challenge.scope,
        event_id=event_id,
        event_content_digest=hashlib.sha256(event_id.encode()).hexdigest(),
        consent_id=challenge.challenge_id,
        decision=authorization.decision.decision,
        policy_snapshot_digest=challenge.policy_snapshot_digest,
        requested_action_id=challenge.requested_action_id,
        requested_action_kind=challenge.requested_action_kind,
        requested_action_content_digest=challenge.requested_action_content_digest,
        requested_action_precondition_revision=(challenge.requested_action_precondition_revision),
        install_expires_at=challenge.expires_at,
    )


def _persist_abandoned_reservation(
    store: SQLiteInstallConsentBrokerStore,
    authorization: AuthenticatedInstallConsent,
    reservation: InteractiveInstallDecisionReservation,
) -> None:
    store._reserve(
        authorization.decision,
        reservation,
        hashlib.sha256(b"abandoned-reservation-token").hexdigest(),
        now=lambda: NOW,
    )


def _reconciliation_fixture(
    tmp_path: Path,
) -> tuple[
    SQLiteEngineStore,
    CtxEngine,
    EngineEvent,
    InstallDecisionEvidenceQuery,
    SQLiteInstallConsentBrokerStore,
    InstallConsentBrokerService,
    InstallConsentChallenge,
    AuthenticatedInstallConsent,
    InstallExecutionBinding,
    InteractiveInstallDecisionReservation,
]:
    engine_store, engine, event, query = evidence_support._pending_decision(tmp_path / "journal")
    selection = install_support._selection()
    binding = install_support._execution_binding()
    challenge = InstallConsentChallenge(
        challenge_id=query.consent_id,
        audience=AUDIENCE,
        workspace_identity_digest=WORKSPACE_DIGEST,
        scope=query.scope,
        capability_id=selection.presentation.capability_id,
        kind=selection.presentation.kind,
        source_digest=selection.presentation.source_digest,
        catalog_snapshot_digest=hashlib.sha256(b"catalog").hexdigest(),
        plan_id="plan-reconciliation",
        install_plan_digest=selection.presentation.install_plan_digest,  # type: ignore[arg-type]
        descriptor_digest=selection.presentation.install_descriptor_digest,  # type: ignore[arg-type]
        execution_binding_digest=binding.binding_digest,
        selection_digest=hashlib.sha256(b"selection").hexdigest(),
        material_identity_digest=selection.authority.result_material.identity_digest,  # type: ignore[union-attr]
        requested_action_id=query.requested_action_id,
        requested_action_kind=query.requested_action_kind,
        requested_action_content_digest=query.requested_action_content_digest,
        requested_action_precondition_revision=(query.requested_action_precondition_revision),
        policy_snapshot_digest=query.policy_snapshot_digest,
        release_root_digest=RELEASE_ROOT_DIGEST,
        permission_expansion=False,
        credential_requirement=False,
        expires_at="2026-08-01T13:00:00Z",
    )
    broker_store = SQLiteInstallConsentBrokerStore(
        tmp_path / "broker" / "consent.sqlite3",
        audience=AUDIENCE,
    )
    service = InstallConsentBrokerService(
        store=broker_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        evidence_provider=engine_store,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )
    broker_store.create_challenge(challenge, now=NOW)
    authorization = service.authenticate(
        challenge,
        _assertion(challenge.challenge_digest),
    )
    reservation = InteractiveInstallDecisionReservation(
        scope=query.scope,
        event_id=query.event_id,
        event_content_digest=query.event_content_digest,
        consent_id=query.consent_id,
        decision=query.decision,
        policy_snapshot_digest=query.policy_snapshot_digest,
        requested_action_id=query.requested_action_id,
        requested_action_kind=query.requested_action_kind,
        requested_action_content_digest=query.requested_action_content_digest,
        requested_action_precondition_revision=(query.requested_action_precondition_revision),
        install_expires_at=challenge.expires_at,
    )
    return (
        engine_store,
        engine,
        event,
        query,
        broker_store,
        service,
        challenge,
        authorization,
        binding,
        reservation,
    )


def test_derives_exact_challenge_from_all_typed_authority(tmp_path: Path) -> None:
    _store, service, directive, selection, action, binding = _fixture(tmp_path)

    challenge = derive_install_consent_challenge(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        audience=AUDIENCE,
    )

    authority = selection.authority
    assert challenge.challenge_id == directive.consent_id
    assert challenge.workspace_identity_digest == WORKSPACE_DIGEST
    assert challenge.scope == action.scope
    assert challenge.capability_id == selection.presentation.capability_id
    assert challenge.catalog_snapshot_digest == directive.catalog_snapshot_digest
    assert challenge.descriptor_digest == authority.descriptor.descriptor_digest  # type: ignore[union-attr]
    assert challenge.material_identity_digest == authority.result_material.identity_digest  # type: ignore[union-attr]
    assert challenge.execution_binding_digest == binding.binding_digest
    assert challenge.selection_digest == install_consent_selection_digest(selection)
    assert challenge.audience == AUDIENCE
    assert challenge.release_root_digest == RELEASE_ROOT_DIGEST
    assert challenge.requested_action_content_digest == action.content_digest
    assert challenge.expires_at == action.expires_at
    assert service.audience == AUDIENCE
    with pytest.raises(AttributeError):
        service.audience = "other-audience"  # type: ignore[misc]


def test_full_selection_digest_binds_benefit_score_signals_and_reasons(tmp_path: Path) -> None:
    _store, _service, directive, selection, action, binding = _fixture(tmp_path)
    original = derive_install_consent_challenge(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        audience=AUDIENCE,
    )
    mutations = (
        replace(
            selection,
            benefit=replace(selection.benefit, marginal_net_benefit_u=599_999),
        ),
        replace(
            selection,
            presentation=replace(selection.presentation, normalized_score_ppm=899_999),
        ),
        replace(
            selection,
            presentation=replace(selection.presentation, matching_signals=("python",)),
        ),
        replace(
            selection,
            presentation=replace(selection.presentation, reason_codes=("other-reason",)),
        ),
    )

    for changed_selection in mutations:
        changed = derive_install_consent_challenge(
            directive=directive,
            selection=changed_selection,
            install_action=action,
            execution_binding=binding,
            workspace_identity_digest=WORKSPACE_DIGEST,
            release_root_digest=RELEASE_ROOT_DIGEST,
            audience=AUDIENCE,
        )
        assert changed.selection_digest != original.selection_digest
        assert changed.challenge_digest != original.challenge_digest


def test_catalog_or_authority_selection_substitution_is_rejected(tmp_path: Path) -> None:
    _store, _service, directive, selection, action, binding = _fixture(tmp_path)
    changed_catalog = replace(
        selection,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=selection.presentation.capability_id,
            kind=selection.presentation.kind,
            catalog_namespace_digest=hashlib.sha256(b"other-namespace").hexdigest(),
        ),
    )
    descriptor = install_support._descriptor(credential_requirement=True)
    changed_authority = replace(
        selection,
        presentation=replace(
            selection.presentation,
            install_descriptor_digest=descriptor.descriptor_digest,
        ),
        authority=InstallPlanningAuthority(
            descriptor=descriptor,
            result_material=selection.authority.result_material,  # type: ignore[union-attr]
        ),
    )

    with pytest.raises(InstallConsentBrokerBindingError, match="payload"):
        derive_install_consent_challenge(
            directive=directive,
            selection=changed_catalog,
            install_action=action,
            execution_binding=binding,
            workspace_identity_digest=WORKSPACE_DIGEST,
            release_root_digest=RELEASE_ROOT_DIGEST,
            audience=AUDIENCE,
        )
    with pytest.raises(InstallConsentBrokerBindingError, match="exact"):
        derive_install_consent_challenge(
            directive=directive,
            selection=changed_authority,
            install_action=action,
            execution_binding=binding,
            workspace_identity_digest=WORKSPACE_DIGEST,
            release_root_digest=RELEASE_ROOT_DIGEST,
            audience=AUDIENCE,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"source_digest": hashlib.sha256(b"other-source").hexdigest()},
        {"catalog_snapshot_digest": hashlib.sha256(b"other-catalog").hexdigest()},
        {"plan_id": "other-plan"},
        {"install_plan_digest": hashlib.sha256(b"other-plan").hexdigest()},
        {"descriptor_digest": hashlib.sha256(b"other-descriptor").hexdigest()},
        {"policy_snapshot_digest": hashlib.sha256(b"other-policy").hexdigest()},
        {"requested_action_content_digest": hashlib.sha256(b"other-action").hexdigest()},
        {"permission_expansion": True},
        {"credential_requirement": True},
        {"reason_code": "other-reason"},
    ],
)
def test_directive_substitution_fails_before_persistence(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)

    with pytest.raises(InstallConsentBrokerBindingError, match="exact"):
        service.prepare(
            directive=replace(directive, **mutation),  # type: ignore[arg-type]
            selection=selection,
            install_action=action,
            execution_binding=binding,
        )
    with pytest.raises(KeyError):
        store.get(directive.consent_id, now=NOW)


def test_action_or_driver_substitution_fails_before_persistence(tmp_path: Path) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    changed_action = replace(action, expires_at="2026-08-01T13:30:00Z")
    changed_driver = InstallExecutionBinding(
        driver_id=binding.driver_id,
        driver_digest=hashlib.sha256(b"other-driver").hexdigest(),
        host_identity_digest=binding.host_identity_digest,
        target_identity_digest=binding.target_identity_digest,
    )

    with pytest.raises(InstallConsentBrokerBindingError, match="action"):
        service.prepare(
            directive=directive,
            selection=selection,
            install_action=changed_action,
            execution_binding=binding,
        )
    with pytest.raises(InstallConsentBrokerBindingError, match="driver"):
        service.prepare(
            directive=directive,
            selection=selection,
            install_action=action,
            execution_binding=changed_driver,
        )
    with pytest.raises(KeyError):
        store.get(directive.consent_id, now=NOW)


def test_prepare_is_idempotent_but_binding_substitution_collides(tmp_path: Path) -> None:
    _store, service, directive, selection, action, binding = _fixture(tmp_path)
    first = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    second = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    assert second == first

    substituted = InstallExecutionBinding(
        driver_id=binding.driver_id,
        driver_digest=binding.driver_digest,
        host_identity_digest=binding.host_identity_digest,
        target_identity_digest=hashlib.sha256(b"other-target").hexdigest(),
    )
    with pytest.raises(ConsentBrokerReplay, match="different challenge"):
        service.prepare(
            directive=directive,
            selection=selection,
            install_action=action,
            execution_binding=substituted,
        )


def test_prepare_only_service_creates_challenge_but_cannot_authenticate(
    tmp_path: Path,
) -> None:
    store, _service, directive, selection, action, binding = _fixture(tmp_path)
    service = InstallConsentBrokerService(
        store=store,
        verifier=None,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )

    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )

    assert store.get(challenge.challenge_id, now=NOW).state == "pending"
    with pytest.raises(InstallConsentVerifierUnavailable, match="trusted verifier"):
        service.authenticate(challenge, _assertion(challenge.challenge_digest))
    assert store.get(challenge.challenge_id, now=NOW).state == "pending"


def test_authenticated_grant_exposes_engine_guard_and_settles(tmp_path: Path) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    authorization = service.authenticate(challenge, _assertion(challenge.challenge_digest))
    guard = service.interactive_guard(
        authorization,
        execution_binding=binding,
    )

    with guard(_reservation(authorization)):
        assert store.get(challenge.challenge_id, now=NOW).state == "reserved"
    assert store.get(challenge.challenge_id, now=NOW).state == "settled"


def test_prompt_text_or_bad_proof_can_never_create_runtime_authority(tmp_path: Path) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )

    with pytest.raises(TypeError, match="SignedHumanDecisionAssertion"):
        service.authenticate(challenge, "yes, install it")  # type: ignore[arg-type]
    with pytest.raises(AttributeError, match="immutable"):
        service._verifier = _TestOnlyAuthenticatorVerifier()  # type: ignore[misc]
    bad = replace(_assertion(challenge.challenge_digest), proof=b"model-said-yes")
    with pytest.raises(ConsentBrokerDecisionRejected, match="verified"):
        service.authenticate(challenge, bad)
    assert store.get(challenge.challenge_id, now=NOW).state == "pending"


def test_post_consent_execution_binding_is_revalidated_independently(tmp_path: Path) -> None:
    _store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    authorization = service.authenticate(challenge, _assertion(challenge.challenge_digest))
    substituted = InstallExecutionBinding(
        driver_id=binding.driver_id,
        driver_digest=binding.driver_digest,
        host_identity_digest=binding.host_identity_digest,
        target_identity_digest=hashlib.sha256(b"substituted-target").hexdigest(),
    )

    with pytest.raises(InstallConsentBrokerBindingError, match="execution binding"):
        service.interactive_guard(authorization, execution_binding=substituted)


def test_exception_releases_and_denial_uses_same_settlement_flow(tmp_path: Path) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    authorization = service.authenticate(
        challenge,
        _assertion(challenge.challenge_digest, decision="denied"),
    )
    manager = service.interactive_guard(
        authorization,
        execution_binding=binding,
    )(_reservation(authorization))

    with pytest.raises(RuntimeError, match="engine failed"):
        with manager:
            raise RuntimeError("engine failed")
    assert store.get(challenge.challenge_id, now=NOW).state == "decision-ready"

    with service.interactive_guard(
        authorization,
        execution_binding=binding,
    )(_reservation(authorization)):
        pass
    assert store.get(challenge.challenge_id, now=NOW).state == "settled"


def test_ready_decision_is_reauthenticated_after_service_restart(tmp_path: Path) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    old_authorization = service.authenticate(
        challenge,
        _assertion(challenge.challenge_digest),
    )
    reopened_store = SQLiteInstallConsentBrokerStore(store.path, audience=AUDIENCE)
    reopened = InstallConsentBrokerService(
        store=reopened_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )

    recovered = reopened.authenticate(
        challenge,
        _assertion(challenge.challenge_digest, nonce="runtime-nonce-2"),
    )
    with reopened.interactive_guard(recovered, execution_binding=binding)(_reservation(recovered)):
        pass
    assert reopened_store.get(challenge.challenge_id, now=NOW).state == "settled"

    with pytest.raises(ConsentBrokerDecisionRejected, match="process-bound"):
        reopened.interactive_guard(old_authorization, execution_binding=binding)


def test_valid_expired_assertion_cannot_revive_under_regressed_service_clock(
    tmp_path: Path,
) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    assertion = _assertion(challenge.challenge_digest)
    forward_service = InstallConsentBrokerService(
        store=store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 12, 46, tzinfo=UTC),
    )

    with pytest.raises(ConsentBrokerExpired, match="assertion"):
        forward_service.authenticate(challenge, assertion)

    regressed_service = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(store.path, audience=AUDIENCE),
        verifier=_TestOnlyAuthenticatorVerifier(),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 12, 32, tzinfo=UTC),
    )
    with pytest.raises(ConsentBrokerExpired, match="assertion"):
        regressed_service.authenticate(challenge, assertion)
    assert regressed_service.status(challenge.challenge_id).state == "pending"


def test_invalid_expired_assertion_cannot_advance_durable_time_floor(
    tmp_path: Path,
) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    valid = _assertion(challenge.challenge_digest)
    invalid = replace(valid, proof=b"invalid-proof")
    forward_service = InstallConsentBrokerService(
        store=store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 12, 46, tzinfo=UTC),
    )

    with pytest.raises(ConsentBrokerDecisionRejected, match="verified"):
        forward_service.authenticate(challenge, invalid)

    regressed_service = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(store.path, audience=AUDIENCE),
        verifier=_TestOnlyAuthenticatorVerifier(),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 12, 32, tzinfo=UTC),
    )
    accepted = regressed_service.authenticate(challenge, valid)
    assert accepted.decision.decision == "granted"


def test_automatic_policy_directive_is_not_interactive_broker_input(tmp_path: Path) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    automatic = replace(directive, decision_basis="preapproved-policy")

    with pytest.raises(InstallConsentBrokerBindingError, match="interactive"):
        service.prepare(
            directive=automatic,
            selection=selection,
            install_action=action,
            execution_binding=binding,
        )
    with pytest.raises(KeyError):
        store.get(directive.consent_id, now=NOW)


def test_runtime_service_persists_no_proof_prompt_or_absolute_path(tmp_path: Path) -> None:
    store, service, directive, selection, action, binding = _fixture(tmp_path)
    challenge = service.prepare(
        directive=directive,
        selection=selection,
        install_action=action,
        execution_binding=binding,
    )
    assertion = _assertion(challenge.challenge_digest)
    service.authenticate(challenge, assertion)

    persisted = store.path.read_bytes()
    assert assertion.proof not in persisted
    assert b"yes, install it" not in persisted
    assert b"/Users/example/private/workspace" not in persisted
    assert b"normalized_score_ppm" not in persisted
    assert b"matching_signals" not in persisted
    assert b"reason_codes" not in persisted
    assert b"marginal_net_benefit_u" not in persisted
    assert binding.binding_digest.encode() in persisted
    assert challenge.selection_digest.encode() in persisted


def test_committed_evidence_settles_reserved_consent_idempotently(tmp_path: Path) -> None:
    (
        engine_store,
        engine,
        event,
        query,
        broker_store,
        service,
        challenge,
        authorization,
        binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    _persist_abandoned_reservation(broker_store, authorization, reservation)
    assert broker_store.get(challenge.challenge_id, now=NOW).state == "reserved"
    engine.process(event)
    provider = _CountingEvidenceProvider(engine_store)
    service = InstallConsentBrokerService(
        store=broker_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        evidence_provider=provider,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )

    first = service.reconcile_install_decision(
        query=query,
        reservation=reservation,
    )
    second = service.reconcile_install_decision(
        query=query,
        reservation=reservation,
    )

    assert isinstance(first, InstallConsentReconciliationReport)
    assert first.outcome == second.outcome == "settled"
    assert first.journal_status == second.journal_status == "committed"
    assert second.record.state == "settled"
    assert provider.revalidations == 2


def test_committed_evidence_settles_decision_ready_crash_state(tmp_path: Path) -> None:
    (
        engine_store,
        engine,
        event,
        query,
        broker_store,
        service,
        challenge,
        _authorization,
        _binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    engine.process(event)

    report = service.reconcile_install_decision(
        query=query,
        reservation=reservation,
    )

    assert report.outcome == "settled"
    assert broker_store.get(challenge.challenge_id, now=NOW).state == "settled"


def test_exact_absence_requires_fresh_signed_assertion_after_reserved_crash(
    tmp_path: Path,
) -> None:
    (
        engine_store,
        _engine,
        _event,
        query,
        broker_store,
        service,
        challenge,
        authorization,
        binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    _persist_abandoned_reservation(broker_store, authorization, reservation)

    report = service.reconcile_install_decision(
        query=query,
        reservation=reservation,
    )

    assert report.outcome == "reauthentication-required"
    assert report.journal_status == "absent-at-expected-head"
    assert report.record.state == "reauthentication-required"
    with pytest.raises(ConsentBrokerDecisionRejected, match="fresh nonce"):
        service.authenticate(challenge, _assertion(challenge.challenge_digest))
    refreshed = service.authenticate(
        challenge,
        _assertion(challenge.challenge_digest, nonce="runtime-nonce-2"),
    )
    assert refreshed.decision.assertion_nonce_digest != (
        authorization.decision.assertion_nonce_digest
    )
    assert broker_store.get(challenge.challenge_id, now=NOW).state == "decision-ready"


@pytest.mark.parametrize(
    "journal_status",
    ["head-advanced", "event-collision", "corrupt", "unavailable"],
)
def test_indeterminate_evidence_quarantines_reserved_consent_without_mutation(
    tmp_path: Path,
    journal_status: str,
) -> None:
    (
        _engine_store,
        _engine,
        _event,
        query,
        broker_store,
        service,
        challenge,
        authorization,
        binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    _persist_abandoned_reservation(broker_store, authorization, reservation)
    before = broker_store.get(challenge.challenge_id, now=NOW)
    service = InstallConsentBrokerService(
        store=broker_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        evidence_provider=_StaticEvidenceProvider(journal_status),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )

    report = service.reconcile_install_decision(
        query=query,
        reservation=reservation,
    )

    assert report.outcome == "quarantined"
    assert report.journal_status == journal_status
    assert report.record == before
    assert broker_store.get(challenge.challenge_id, now=NOW) == before


def test_cross_store_evidence_revalidation_fails_closed(tmp_path: Path) -> None:
    (
        engine_store,
        engine,
        event,
        query,
        broker_store,
        service,
        challenge,
        authorization,
        binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    _persist_abandoned_reservation(broker_store, authorization, reservation)
    engine.process(event)
    other_store = SQLiteEngineStore(tmp_path / "other-journal" / "journal.sqlite3")
    before = broker_store.get(challenge.challenge_id, now=NOW)
    service = InstallConsentBrokerService(
        store=broker_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        evidence_provider=_CountingEvidenceProvider(engine_store, other_store),
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )

    with pytest.raises(ConsentBrokerDecisionRejected, match="revalidation"):
        service.reconcile_install_decision(
            query=query,
            reservation=reservation,
        )
    assert broker_store.get(challenge.challenge_id, now=NOW) == before


@pytest.mark.parametrize("mutation", ["scope", "action", "event", "decision"])
def test_mismatched_reconciliation_identity_is_rejected_without_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    (
        engine_store,
        _engine,
        _event,
        query,
        broker_store,
        service,
        challenge,
        _authorization,
        _binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    if mutation == "scope":
        changed_query = replace(
            query,
            scope=replace(query.scope, exposure_id="other-exposure"),
        )
    elif mutation == "action":
        changed_query = replace(query, requested_action_id="other-action")
    elif mutation == "event":
        changed_query = replace(query, event_id="other-event")
    else:
        changed_query = replace(query, decision="denied")
    before = broker_store.get(challenge.challenge_id, now=NOW)

    with pytest.raises(ConsentBrokerDecisionRejected, match="exact broker decision"):
        service.reconcile_install_decision(
            query=changed_query,
            reservation=reservation,
        )
    assert broker_store.get(challenge.challenge_id, now=NOW) == before


def test_absent_evidence_expires_reserved_consent_at_boundary(tmp_path: Path) -> None:
    (
        engine_store,
        _engine,
        _event,
        query,
        broker_store,
        _original_service,
        challenge,
        authorization,
        binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    service = InstallConsentBrokerService(
        store=broker_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        evidence_provider=engine_store,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
    )
    _persist_abandoned_reservation(broker_store, authorization, reservation)

    report = service.reconcile_install_decision(
        query=query,
        reservation=reservation,
    )

    assert report.outcome == "expired"
    assert report.record.state == "expired"
    assert (
        broker_store.get(
            challenge.challenge_id,
            now=datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
        ).state
        == "expired"
    )


def test_exact_commit_settles_authenticated_decision_after_expiry(tmp_path: Path) -> None:
    (
        engine_store,
        engine,
        event,
        query,
        broker_store,
        _service,
        challenge,
        _authorization,
        _binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    engine.process(event)
    service = InstallConsentBrokerService(
        store=broker_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        evidence_provider=engine_store,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
    )

    report = service.reconcile_install_decision(
        query=query,
        reservation=reservation,
    )

    assert report.outcome == "settled"
    assert report.journal_status == "committed"
    assert report.record.state == "settled"
    assert (
        broker_store.get(
            challenge.challenge_id,
            now=datetime(2026, 8, 1, 13, 0, tzinfo=UTC),
        ).state
        == "settled"
    )


def test_live_reservation_lease_serializes_reconciliation_before_engine_lookup(
    tmp_path: Path,
) -> None:
    (
        _engine_store,
        engine,
        event,
        query,
        broker_store,
        service,
        challenge,
        authorization,
        binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    manager = service.interactive_guard(
        authorization,
        execution_binding=binding,
    )(reservation)
    manager.__enter__()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.reconcile_install_decision,
            query=query,
            reservation=reservation,
        )
        with pytest.raises(TimeoutError):
            future.result(timeout=0.2)
        assert broker_store.inspect_record(challenge.challenge_id).state == "reserved"
        engine.process(event)
        manager.__exit__(None, None, None)
        report = future.result(timeout=5)

    assert report.outcome == "settled"
    assert report.journal_status == "committed"
    assert broker_store.inspect_record(challenge.challenge_id).state == "settled"


def test_historical_signed_nonce_cannot_replay_after_two_reconciliation_cycles(
    tmp_path: Path,
) -> None:
    (
        _engine_store,
        _engine,
        _event,
        query,
        broker_store,
        service,
        challenge,
        _authorization,
        _binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    assertion_a = _assertion(challenge.challenge_digest)

    first = service.reconcile_install_decision(query=query, reservation=reservation)
    assert first.outcome == "reauthentication-required"
    service.authenticate(
        challenge,
        _assertion(challenge.challenge_digest, nonce="runtime-nonce-2"),
    )
    second = service.reconcile_install_decision(query=query, reservation=reservation)
    assert second.outcome == "reauthentication-required"

    with pytest.raises(ConsentBrokerReplay, match="nonce was already recorded"):
        service.authenticate(challenge, assertion_a)
    assert broker_store.inspect_record(challenge.challenge_id).state == (
        "reauthentication-required"
    )


def test_wrong_service_identity_is_rejected_before_reconciliation_mutation(
    tmp_path: Path,
) -> None:
    (
        engine_store,
        _engine,
        _event,
        query,
        broker_store,
        _service,
        challenge,
        _authorization,
        _binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    wrong_service = InstallConsentBrokerService(
        store=broker_store,
        verifier=_TestOnlyAuthenticatorVerifier(),
        evidence_provider=engine_store,
        workspace_identity_digest=hashlib.sha256(b"wrong-workspace").hexdigest(),
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )
    before = broker_store.inspect_record(challenge.challenge_id)

    with pytest.raises(InstallConsentBrokerBindingError, match="workspace"):
        wrong_service.reconcile_install_decision(
            query=query,
            reservation=reservation,
        )
    assert broker_store.inspect_record(challenge.challenge_id) == before


def test_canonical_evidence_provider_is_frozen_and_has_no_per_call_override(
    tmp_path: Path,
) -> None:
    (
        _engine_store,
        _engine,
        _event,
        query,
        broker_store,
        service,
        challenge,
        _authorization,
        _binding,
        reservation,
    ) = _reconciliation_fixture(tmp_path)
    wrong_provider = _StaticEvidenceProvider("committed")
    before = broker_store.inspect_record(challenge.challenge_id)

    with pytest.raises(AttributeError, match="immutable"):
        service._evidence_provider = wrong_provider  # type: ignore[misc]
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        service.reconcile_install_decision(
            provider=wrong_provider,  # type: ignore[call-arg]
            query=query,
            reservation=reservation,
        )
    assert broker_store.inspect_record(challenge.challenge_id) == before


@pytest.mark.parametrize("provider", [True, {"committed": True}])
def test_reconciliation_rejects_caller_maps_and_booleans_without_mutation(
    tmp_path: Path,
    provider: object,
) -> None:
    (
        _engine_store,
        _engine,
        _event,
        _query,
        broker_store,
        service,
        challenge,
        _authorization,
        _binding,
        _reservation,
    ) = _reconciliation_fixture(tmp_path)
    before = broker_store.get(challenge.challenge_id, now=NOW)

    with pytest.raises(TypeError, match="CommittedInstallDecisionEvidenceProvider"):
        InstallConsentBrokerService(
            store=broker_store,
            verifier=_TestOnlyAuthenticatorVerifier(),
            evidence_provider=provider,  # type: ignore[arg-type]
            workspace_identity_digest=WORKSPACE_DIGEST,
            release_root_digest=RELEASE_ROOT_DIGEST,
            trusted_utc_now=lambda: NOW,
        )
    assert broker_store.get(challenge.challenge_id, now=NOW) == before
