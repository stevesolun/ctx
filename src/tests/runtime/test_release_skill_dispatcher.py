from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import ctx.runtime.production_catalog as production_catalog_module

from ctx.core.install_policy_store import persist_install_policy
from ctx.core.install_consent_broker_store import (
    HumanDecisionVerifier,
    SQLiteInstallConsentBrokerStore,
    SignedHumanDecisionAssertion,
)
from ctx.engine.engine import CtxEngine
from ctx.engine.installation import (
    InstallConsentPolicy,
    InstallExecutionBinding,
    InteractiveInstallDecisionReservation,
)
from ctx.engine.store import SQLiteEngineStore, StreamId
from ctx.runtime.install_execution import InstallDriverRequest
from ctx.runtime.install_consent_broker import (
    AuthenticatedInstallConsent,
    InstallConsentBrokerService,
)
from ctx.runtime.production_catalog import ReleaseCatalogError, open_release_pinned_query_catalog
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE
from ctx.runtime.release_skill_dispatcher import (
    ReleaseSkillDispatchError,
    ReleaseSkillInstallRequest,
    dispatch_release_skill_install,
    inspect_release_skill_recovery_status,
    probe_release_skill_install_relevance,
)
from ctx.runtime.workspace_identity import capture_workspace_identity
from tests.engine import test_engine_install_coordinator as install_support


TARGET_ID = "skill:ctx-python-state-protocols"
TARGET_SHA256 = "c87c65b5b09f48e27c683fb5ada9d8bc377d6d72d7742ce7aac3c2d3d97ac441"
BEFORE_EXPIRY = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
AFTER_EXPIRY = datetime(2026, 8, 2, 14, 0, tzinfo=UTC)
BROKER_AUDIENCE = "ctx-release-skill-consent-v1"
BROKER_KEY = b"dispatcher-test-only-human-authenticator-key"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(tmp_path: Path) -> ReleaseSkillInstallRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    skill_root = tmp_path / "skills"
    skill_root.mkdir(mode=0o700)
    if os.name != "nt":
        state_root.chmod(0o700)
        skill_root.chmod(0o700)
    return ReleaseSkillInstallRequest(
        host_context_id="host-neutral-test",
        host_identity_digest=_digest("host-neutral-test"),
        task="Repair nested Python context-manager state restoration",
        language="Python",
        session_id="release-install-session",
        workspace=workspace,
        journal_path=state_root / "engine.sqlite3",
        benefit_audit_path=state_root / "benefit.sqlite3",
        policy_store_root=state_root / "install-policy",
        skill_store_root=skill_root,
        occurred_at="2026-08-02T12:00:00Z",
    )


class _BrokerVerifier(HumanDecisionVerifier):
    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        return hmac.compare_digest(
            assertion.proof,
            hmac.digest(BROKER_KEY, signing_bytes, "sha256"),
        )


def _broker(
    request: ReleaseSkillInstallRequest,
    *,
    trusted_now: datetime = BEFORE_EXPIRY,
) -> tuple[SQLiteInstallConsentBrokerStore, InstallConsentBrokerService]:
    store = SQLiteInstallConsentBrokerStore(
        request.journal_path.parent / "consent-broker.sqlite3",
        audience=BROKER_AUDIENCE,
    )
    service = InstallConsentBrokerService(
        store=store,
        verifier=_BrokerVerifier(),
        evidence_provider=SQLiteEngineStore(request.journal_path),
        workspace_identity_digest=capture_workspace_identity(request.workspace).digest,
        release_root_digest=production_catalog_module.RELEASE_QUERY_CATALOG_ROOT_SHA256,
        trusted_utc_now=lambda: trusted_now,
    )
    return store, service


def _signed_assertion(
    challenge_digest: str,
    *,
    decision: str = "granted",
    nonce: str = "dispatcher-human-nonce-1",
) -> SignedHumanDecisionAssertion:
    unsigned = SignedHumanDecisionAssertion(
        challenge_digest=challenge_digest,
        decision=decision,
        principal_digest=_digest("authenticated-human"),
        authenticator_id="test-passkey",
        audience=BROKER_AUDIENCE,
        nonce=nonce,
        issued_at="2026-08-02T12:20:00Z",
        expires_at="2026-08-02T12:45:00Z",
        proof=b"unsigned",
    )
    return replace(
        unsigned,
        proof=hmac.digest(BROKER_KEY, unsigned.signing_bytes(), "sha256"),
    )


def test_normalized_workspace_spelling_reuses_the_same_committed_consent(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    first = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    alias = replace(request, workspace=request.workspace / "not-created" / "..")

    second = dispatch_release_skill_install(alias, trusted_utc_now=lambda: BEFORE_EXPIRY)

    assert first.status == second.status == "consent-required"
    assert first.consent == second.consent


def test_dispatcher_rejects_final_workspace_symlink(tmp_path: Path) -> None:
    request = _request(tmp_path)
    alias = tmp_path / "workspace-alias"
    try:
        alias.symlink_to(request.workspace, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ValueError, match="symlink"):
        replace(request, workspace=alias)


def test_dispatcher_rejects_workspace_below_symlinked_parent(tmp_path: Path) -> None:
    request = _request(tmp_path)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    nested_workspace = real_parent / "workspace"
    nested_workspace.mkdir()
    alias_parent = tmp_path / "alias-parent"
    try:
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")

    with pytest.raises(ValueError, match="symlink"):
        replace(request, workspace=alias_parent / "workspace")


def test_darwin_system_alias_reuses_workspace_identity(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("Darwin system alias contract")
    request = _request(tmp_path)
    canonical = request.workspace
    alias: Path | None = None
    for canonical_root, alias_root in (
        (Path("/private/tmp"), Path("/tmp")),
        (Path("/private/var"), Path("/var")),
    ):
        try:
            alias = alias_root / canonical.relative_to(canonical_root)
        except ValueError:
            continue
        break
    if alias is None:
        pytest.skip("workspace is not below a verified Darwin system alias")

    first = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    second = dispatch_release_skill_install(
        replace(request, workspace=alias),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert first.status == second.status == "consent-required"
    assert first.consent == second.consent


def test_dispatcher_rejects_workspace_identity_change_before_retry(tmp_path: Path) -> None:
    request = _request(tmp_path)
    original_workspace = request.workspace
    original_workspace.rename(tmp_path / "retired-workspace")
    original_workspace.mkdir()

    with pytest.raises(ReleaseSkillDispatchError, match="workspace identity"):
        dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    assert not request.journal_path.exists()
    assert not request.benefit_audit_path.exists()


def test_dispatcher_rejects_normalized_same_state_path(tmp_path: Path) -> None:
    request = _request(tmp_path)
    shared = request.journal_path.parent / "same.sqlite3"

    with pytest.raises(ValueError, match="distinct"):
        replace(
            request,
            journal_path=shared.parent / "not-created" / ".." / shared.name,
            benefit_audit_path=shared,
        )


@pytest.mark.skipif(os.name == "nt", reason="release skill catalog is POSIX-only")
def test_relevance_probe_is_pure_and_does_not_consume_management_stream(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    irrelevant = probe_release_skill_install_relevance(
        replace(
            request,
            task="write a JavaScript button label",
            language="JavaScript",
        )
    )
    relevant = probe_release_skill_install_relevance(request)

    assert not irrelevant.relevant
    assert relevant.relevant
    assert relevant.capability_id == TARGET_ID
    assert irrelevant.release_root_digest == relevant.release_root_digest
    assert not request.journal_path.exists()
    assert not request.benefit_audit_path.exists()
    assert not (request.skill_store_root / TARGET_SHA256).exists()


def test_recovery_status_absent_journal_is_pure_no_stream(tmp_path: Path) -> None:
    request = _request(tmp_path)

    status = inspect_release_skill_recovery_status(request)

    assert not status.requires_recovery
    assert status.phase == "no-stream"
    assert status.revision == 0
    assert not request.journal_path.exists()
    assert not request.benefit_audit_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill catalog is POSIX-only")
def test_recovery_status_treats_committed_abstention_as_terminal(tmp_path: Path) -> None:
    request = replace(
        _request(tmp_path),
        task="write a JavaScript button label",
        language="JavaScript",
    )
    dispatched = dispatch_release_skill_install(
        request,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    status = inspect_release_skill_recovery_status(request)

    assert dispatched.status == "abstained"
    assert not status.requires_recovery
    assert status.phase == "abstained"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_safe_default_asks_without_reading_or_installing_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    resource_reads: list[str] = []
    original_read = production_catalog_module._read_resource

    def observed_read(name: str, *, maximum_bytes: int) -> bytes:
        resource_reads.append(name)
        return original_read(name, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(production_catalog_module, "_read_resource", observed_read)

    result = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    assert result.status == "consent-required"
    assert result.capability_id == TARGET_ID
    assert result.consent is not None and result.consent.requires_prompt
    assert result.challenge is None
    assert result.install_action_content_digest is None
    assert result.install_receipt_content_digest is None
    assert result.installed_lineage_digest is None
    assert result.activation_action_content_digest is None
    assert not (request.skill_store_root / TARGET_SHA256).exists()
    assert RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE not in resource_reads


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_prepare_returns_only_safe_challenge_without_material_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    from ctx.runtime import release_material

    def forbidden_load(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("material was read before authenticated consent")

    monkeypatch.setattr(
        release_material.ReleasePinnedSkillMaterialSource,
        "load",
        forbidden_load,
    )
    result = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    repeated = dispatch_release_skill_install(
        replace(
            request,
            task="write an unrelated JavaScript button label",
            language="JavaScript",
            occurred_at="2026-08-02T12:05:00Z",
        ),
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert result.status == "consent-required"
    assert result.consent is not None and result.consent.requires_prompt
    assert result.challenge is not None
    assert result.challenge.challenge_id == result.consent.consent_id
    assert result.challenge.audience == BROKER_AUDIENCE
    assert len(result.challenge.selection_digest) == 64
    assert result.challenge.policy_snapshot_digest == result.consent.policy_snapshot_digest
    assert result.challenge.requested_action_content_digest == (
        result.consent.requested_action_content_digest
    )
    assert repeated.challenge == result.challenge
    assert not (request.skill_store_root / TARGET_SHA256).exists()
    encoded = json.dumps(asdict(result), sort_keys=True)
    rendered = repr(result)
    for forbidden in (
        "HostAction",
        "CapabilityPlanSelectionV3",
        "InstallExecutionBinding",
        "VerifiedHumanDecision",
        str(request.workspace),
        BROKER_KEY.hex(),
    ):
        assert forbidden not in encoded
        assert forbidden not in rendered


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_authenticated_grant_installs_exactly_once(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None
    assertion = _signed_assertion(prepared.challenge.challenge_digest)

    installed = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:05:00Z"),
        consent_broker=broker,
        decision_assertion=assertion,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    repeated = dispatch_release_skill_install(
        replace(
            request,
            task="a later prompt must not rewrite committed consent",
            occurred_at="2026-08-02T12:10:00Z",
        ),
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert installed.status == repeated.status == "installed"
    assert installed.challenge is repeated.challenge is None
    assert (
        broker.status(prepared.challenge.challenge_id).challenge.challenge_digest
        == prepared.challenge.challenge_digest
    )
    assert tuple(request.skill_store_root.glob(TARGET_SHA256)) == (
        request.skill_store_root / TARGET_SHA256,
    )


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_authenticated_denial_never_claims_install(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None

    denied = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:05:00Z"),
        consent_broker=broker,
        decision_assertion=_signed_assertion(
            prepared.challenge.challenge_digest,
            decision="denied",
        ),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert denied.status == "denied"
    assert denied.install_action_content_digest is None
    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_dispatcher_exposes_no_unauthenticated_interactive_authority() -> None:
    parameters = inspect.signature(dispatch_release_skill_install).parameters

    assert "interactive_decision" not in parameters
    assert "interactive_install_decision_guard" not in parameters


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_restart_reauthenticates_then_resumes_exact_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None
    original_commit = SQLiteEngineStore.commit

    def interrupt_decision_commit(
        _engine_store: SQLiteEngineStore,
        **_values: object,
    ) -> object:
        raise RuntimeError("injected broker decision interruption")

    monkeypatch.setattr(SQLiteEngineStore, "commit", interrupt_decision_commit)
    with pytest.raises(RuntimeError, match="broker decision interruption"):
        dispatch_release_skill_install(
            replace(request, occurred_at="2026-08-02T12:05:00Z"),
            consent_broker=broker,
            decision_assertion=_signed_assertion(prepared.challenge.challenge_digest),
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )
    monkeypatch.setattr(SQLiteEngineStore, "commit", original_commit)

    reopened = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(store.path, audience=BROKER_AUDIENCE),
        verifier=_BrokerVerifier(),
        evidence_provider=SQLiteEngineStore(request.journal_path),
        workspace_identity_digest=capture_workspace_identity(request.workspace).digest,
        release_root_digest=production_catalog_module.RELEASE_QUERY_CATALOG_ROOT_SHA256,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    awaiting_reauthentication = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:10:00Z"),
        consent_broker=reopened,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert awaiting_reauthentication.status == "consent-required"
    assert awaiting_reauthentication.challenge == prepared.challenge
    assert reopened.status(prepared.challenge.challenge_id).state == ("reauthentication-required")

    resumed = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:15:00Z"),
        consent_broker=reopened,
        decision_assertion=_signed_assertion(
            prepared.challenge.challenge_digest,
            nonce="dispatcher-human-nonce-2",
        ),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert resumed.status == "installed"
    assert (request.skill_store_root / TARGET_SHA256).is_file()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_restart_reconciles_abandoned_reservation_then_requires_fresh_assertion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None

    original_interactive_guard = InstallConsentBrokerService.interactive_guard

    def interrupt_after_reservation(
        _service: InstallConsentBrokerService,
        authorization: AuthenticatedInstallConsent,
        *,
        execution_binding: InstallExecutionBinding,
    ):
        del execution_binding

        def guarded(reservation: InteractiveInstallDecisionReservation):
            @contextmanager
            def abandon_before_commit() -> Iterator[None]:
                store._reserve(
                    authorization.decision,
                    reservation,
                    _digest("abandoned-reservation-token"),
                    now=lambda: BEFORE_EXPIRY,
                )
                raise RuntimeError("injected process loss after broker reservation")
                yield

            return abandon_before_commit()

        return guarded

    monkeypatch.setattr(
        InstallConsentBrokerService,
        "interactive_guard",
        interrupt_after_reservation,
    )
    with pytest.raises(ReleaseSkillDispatchError, match="guard is unavailable"):
        dispatch_release_skill_install(
            replace(request, occurred_at="2026-08-02T12:05:00Z"),
            consent_broker=broker,
            decision_assertion=_signed_assertion(prepared.challenge.challenge_digest),
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )
    assert broker.status(prepared.challenge.challenge_id).state == "reserved"
    assert inspect_release_skill_recovery_status(request).phase == "pending-consent"
    monkeypatch.setattr(
        InstallConsentBrokerService,
        "interactive_guard",
        original_interactive_guard,
    )

    reopened = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(store.path, audience=BROKER_AUDIENCE),
        verifier=_BrokerVerifier(),
        evidence_provider=SQLiteEngineStore(request.journal_path),
        workspace_identity_digest=capture_workspace_identity(request.workspace).digest,
        release_root_digest=production_catalog_module.RELEASE_QUERY_CATALOG_ROOT_SHA256,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    awaiting_fresh_assertion = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:10:00Z"),
        consent_broker=reopened,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert awaiting_fresh_assertion.status == "consent-required"
    assert reopened.status(prepared.challenge.challenge_id).state == ("reauthentication-required")

    resumed = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:15:00Z"),
        consent_broker=reopened,
        decision_assertion=_signed_assertion(
            prepared.challenge.challenge_digest,
            nonce="dispatcher-human-nonce-2",
        ),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert resumed.status == "installed"
    assert reopened.status(prepared.challenge.challenge_id).state == "settled"
    assert (request.skill_store_root / TARGET_SHA256).is_file()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_after_commit_recovery_reconciles_and_resumes_exact_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None
    original_interactive_guard = InstallConsentBrokerService.interactive_guard

    def interrupt_guard_settlement(
        service: InstallConsentBrokerService,
        authorization: object,
        *,
        execution_binding: InstallExecutionBinding,
    ):
        broker_guard = original_interactive_guard(
            service,
            authorization,  # type: ignore[arg-type]
            execution_binding=execution_binding,
        )

        def guarded(reservation: InteractiveInstallDecisionReservation):
            @contextmanager
            def interrupt_after_commit() -> Iterator[None]:
                with broker_guard(reservation):
                    yield
                    raise RuntimeError("injected broker settlement interruption")

            return interrupt_after_commit()

        return guarded

    monkeypatch.setattr(
        InstallConsentBrokerService,
        "interactive_guard",
        interrupt_guard_settlement,
    )
    with pytest.raises(ReleaseSkillDispatchError, match="settlement failed"):
        dispatch_release_skill_install(
            replace(request, occurred_at="2026-08-02T12:05:00Z"),
            consent_broker=broker,
            decision_assertion=_signed_assertion(prepared.challenge.challenge_digest),
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )
    assert broker.status(prepared.challenge.challenge_id).state == "decision-ready"
    assert inspect_release_skill_recovery_status(request).phase == "decision-committed"
    assert not (request.skill_store_root / TARGET_SHA256).exists()

    reopened = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(store.path, audience=BROKER_AUDIENCE),
        verifier=_BrokerVerifier(),
        evidence_provider=SQLiteEngineStore(request.journal_path),
        workspace_identity_digest=capture_workspace_identity(request.workspace).digest,
        release_root_digest=production_catalog_module.RELEASE_QUERY_CATALOG_ROOT_SHA256,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    resumed = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:10:00Z"),
        consent_broker=reopened,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert resumed.status == "installed"
    assert reopened.status(prepared.challenge.challenge_id).state == "settled"
    assert (request.skill_store_root / TARGET_SHA256).is_file()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
@pytest.mark.parametrize(
    "substitution",
    ("action", "policy", "binding", "selection", "audience"),
)
def test_broker_dispatch_rejects_authority_substitution_before_material_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    substitution: str,
) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    original_prepare = InstallConsentBrokerService.prepare

    def substituted_prepare(service: InstallConsentBrokerService, **values: object):
        if substitution == "action":
            values["install_action"] = replace(  # type: ignore[call-overload,type-var]
                values["install_action"],  # type: ignore[arg-type]
                expires_at="2026-08-02T12:59:00Z",
            )
        elif substitution == "policy":
            values["directive"] = replace(  # type: ignore[call-overload,type-var]
                values["directive"],  # type: ignore[arg-type]
                policy_snapshot_digest=_digest("substituted-policy"),
            )
        elif substitution == "binding":
            binding = values["execution_binding"]
            assert isinstance(binding, InstallExecutionBinding)
            values["execution_binding"] = InstallExecutionBinding(
                driver_id=binding.driver_id,
                driver_digest=binding.driver_digest,
                host_identity_digest=binding.host_identity_digest,
                target_identity_digest=_digest("substituted-target"),
            )
        challenge = original_prepare(service, **values)  # type: ignore[arg-type]
        if substitution == "selection":
            return replace(challenge, selection_digest=_digest("substituted-selection"))
        if substitution == "audience":
            return replace(challenge, audience="substituted-audience")
        return challenge

    monkeypatch.setattr(InstallConsentBrokerService, "prepare", substituted_prepare)
    with pytest.raises(ReleaseSkillDispatchError):
        dispatch_release_skill_install(
            request,
            consent_broker=broker,
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )
    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_rejects_substituted_assertion_before_material_read(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None
    substituted = _signed_assertion(_digest("other-challenge"))

    with pytest.raises(ReleaseSkillDispatchError):
        dispatch_release_skill_install(
            replace(request, occurred_at="2026-08-02T12:05:00Z"),
            consent_broker=broker,
            decision_assertion=substituted,
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )
    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_broker_expiry_fails_closed_before_install(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request, trusted_now=AFTER_EXPIRY)

    with pytest.raises(ReleaseSkillDispatchError, match="expired"):
        dispatch_release_skill_install(
            request,
            consent_broker=broker,
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )
    assert not (request.skill_store_root / TARGET_SHA256).exists()


def test_release_catalog_rejects_direct_body_read_before_durable_claim(
    tmp_path: Path,
) -> None:
    engine, _policy = install_support._engine(tmp_path)
    action = install_support._pending_install(engine)
    descriptor = install_support._descriptor()
    binding = InstallExecutionBinding(
        driver_id=descriptor.installer_id,
        driver_digest=action.payload["installer_digest"],  # type: ignore[arg-type]
        host_identity_digest=install_support._digest("unclaimed-host"),
        target_identity_digest=install_support._digest("unclaimed-target"),
    )
    driver_request = InstallDriverRequest(
        action=action,
        descriptor=descriptor,
        binding=binding,
    )
    catalog = open_release_pinned_query_catalog()
    try:
        with pytest.raises(ReleaseCatalogError, match="durable claim"):
            catalog._load_install_skill_body(  # noqa: SLF001 - security boundary regression.
                engine,
                driver_request,
                install_support._material(),
            )
    finally:
        catalog.close()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_persisted_preapproval_installs_then_returns_lineage_bound_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    persist_install_policy(policy, request.policy_store_root)
    observed_claims: list[bool] = []

    from ctx.runtime import release_material

    original = release_material.ReleasePinnedSkillMaterialSource.load

    def checked_load(
        source: object,
        driver_request: object,
        material: object,
        install_body: bytes,
    ) -> str:
        action = driver_request.action  # type: ignore[attr-defined]
        status = SQLiteEngineStore(request.journal_path).install_execution_status(
            StreamId.from_scope(action.scope),
            action.action_id,
        )
        observed_claims.append(status.claimed)
        return original(source, driver_request, material, install_body)  # type: ignore[arg-type]

    monkeypatch.setattr(
        release_material.ReleasePinnedSkillMaterialSource,
        "load",
        checked_load,
    )

    result = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    assert result.status == "installed"
    assert observed_claims == [True]
    assert result.consent is not None and not result.consent.requires_prompt
    assert result.install_action_content_digest is not None
    assert result.install_receipt_content_digest is not None
    assert result.installed_lineage_digest is not None
    assert result.activation_action_content_digest is not None
    assert not hasattr(result, "activation_action")
    installed = request.skill_store_root / TARGET_SHA256
    assert installed.exists()
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == TARGET_SHA256


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_authenticated_interactive_denial_never_claims_or_exposes(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None

    result = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:05:00Z"),
        consent_broker=broker,
        decision_assertion=_signed_assertion(
            prepared.challenge.challenge_digest,
            decision="denied",
        ),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert result.status == "denied"
    record = broker.status(prepared.challenge.challenge_id)
    assert record.state == "settled"
    assert record.decision == "denied"
    assert result.activation_action_content_digest is None
    assert result.installed_lineage_digest is None
    assert not (request.skill_store_root / TARGET_SHA256).exists()
    recovery = inspect_release_skill_recovery_status(request)
    assert not recovery.requires_recovery
    assert recovery.phase == "terminal-denied"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_authenticated_interactive_grant_installs_and_returns_activation_eligibility(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    prepared = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert prepared.challenge is not None

    result = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:05:00Z"),
        consent_broker=broker,
        decision_assertion=_signed_assertion(prepared.challenge.challenge_digest),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert result.status == "installed"
    assert result.consent is not None and result.consent.requires_prompt
    record = broker.status(prepared.challenge.challenge_id)
    assert record.state == "settled"
    assert record.decision == "granted"
    assert record.challenge.challenge_id == result.consent.consent_id
    assert record.challenge.policy_snapshot_digest == result.consent.policy_snapshot_digest
    assert record.challenge.requested_action_id == result.consent.requested_action_id
    assert (
        record.challenge.requested_action_content_digest
        == result.consent.requested_action_content_digest
        == result.install_action_content_digest
    )
    assert result.install_receipt_content_digest is not None
    assert result.installed_lineage_digest is not None
    assert result.activation_action_content_digest is not None
    installed = request.skill_store_root / TARGET_SHA256
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == TARGET_SHA256


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_policy_change_between_prompt_and_decision_fails_closed(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _store, broker = _broker(request)
    first = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert first.status == "consent-required"
    assert first.challenge is not None
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )

    with pytest.raises(ReleaseSkillDispatchError, match="policy"):
        dispatch_release_skill_install(
            request,
            consent_broker=broker,
            decision_assertion=_signed_assertion(first.challenge.challenge_digest),
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )

    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
@pytest.mark.parametrize("interrupt_after", ("SessionStarted", "IntentObserved"))
def test_policy_change_before_consent_request_uses_the_current_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after: str,
) -> None:
    request = _request(tmp_path)
    automatic = InstallConsentPolicy(skill_mode="preapproved-auto")
    interactive = InstallConsentPolicy(skill_mode="ask-each-time")
    persist_install_policy(automatic, request.policy_store_root)
    original_process = CtxEngine.process

    def interrupt_after_commit(engine: CtxEngine, event: object):
        transition = original_process(engine, event)  # type: ignore[arg-type]
        if event.kind == interrupt_after:  # type: ignore[attr-defined]
            raise RuntimeError("injected interruption before consent request")
        return transition

    monkeypatch.setattr(CtxEngine, "process", interrupt_after_commit)
    with pytest.raises(RuntimeError, match="before consent request"):
        dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    monkeypatch.setattr(CtxEngine, "process", original_process)
    persist_install_policy(interactive, request.policy_store_root)
    result = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:05:00Z"),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert result.status == "consent-required"
    assert result.consent is not None
    assert result.challenge is None
    assert result.consent.policy_snapshot_digest == interactive.policy_digest
    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_policy_change_with_pending_auto_consent_fails_closed_without_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    original_process = CtxEngine.process

    def interrupt_after_request(engine: CtxEngine, event: object):
        transition = original_process(engine, event)  # type: ignore[arg-type]
        if event.kind == "ReassessmentRequested":  # type: ignore[attr-defined]
            raise RuntimeError("injected interruption with pending consent")
        return transition

    monkeypatch.setattr(CtxEngine, "process", interrupt_after_request)
    with pytest.raises(RuntimeError, match="pending consent"):
        dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    monkeypatch.setattr(CtxEngine, "process", original_process)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="ask-each-time"),
        request.policy_store_root,
    )
    with pytest.raises(ReleaseSkillDispatchError, match="policy"):
        dispatch_release_skill_install(
            replace(request, occurred_at="2026-08-02T12:05:00Z"),
            trusted_utc_now=lambda: BEFORE_EXPIRY,
        )

    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_expired_interactive_grant_fails_before_install_claim(tmp_path: Path) -> None:
    request = _request(tmp_path)
    store, broker = _broker(request)
    first = dispatch_release_skill_install(
        request,
        consent_broker=broker,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert first.status == "consent-required"
    assert first.challenge is not None
    expired_broker = InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(store.path, audience=BROKER_AUDIENCE),
        verifier=_BrokerVerifier(),
        evidence_provider=SQLiteEngineStore(request.journal_path),
        workspace_identity_digest=capture_workspace_identity(request.workspace).digest,
        release_root_digest=production_catalog_module.RELEASE_QUERY_CATALOG_ROOT_SHA256,
        trusted_utc_now=lambda: AFTER_EXPIRY,
    )

    with pytest.raises(ReleaseSkillDispatchError, match="expired"):
        dispatch_release_skill_install(
            request,
            consent_broker=expired_broker,
            decision_assertion=_signed_assertion(first.challenge.challenge_digest),
            trusted_utc_now=lambda: AFTER_EXPIRY,
        )

    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_successful_retry_is_idempotent_and_does_not_read_content_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    from ctx.runtime import release_material

    original = release_material.ReleasePinnedSkillMaterialSource.load
    calls = 0

    def counted(
        source: object,
        driver_request: object,
        material: object,
        install_body: bytes,
    ) -> str:
        nonlocal calls
        calls += 1
        return original(source, driver_request, material, install_body)  # type: ignore[arg-type]

    monkeypatch.setattr(release_material.ReleasePinnedSkillMaterialSource, "load", counted)

    first = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    second = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    assert first.status == second.status == "installed"
    assert first.install_action_content_digest == second.install_action_content_digest
    assert first.install_receipt_content_digest == second.install_receipt_content_digest
    assert first.installed_lineage_digest == second.installed_lineage_digest
    assert calls == 1


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
@pytest.mark.parametrize(
    ("interrupt_after", "committed_revision"),
    (
        ("SessionStarted", 1),
        ("IntentObserved", 2),
        ("ReassessmentRequested", 3),
        ("UserDecision", 4),
    ),
)
def test_retry_resumes_each_committed_decision_prefix_without_replaying_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt_after: str,
    committed_revision: int,
) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    original_process = CtxEngine.process
    interrupted = False

    def interrupt_after_commit(engine: CtxEngine, event: object):
        nonlocal interrupted
        transition = original_process(engine, event)  # type: ignore[arg-type]
        if not interrupted and event.kind == interrupt_after:  # type: ignore[attr-defined]
            interrupted = True
            raise RuntimeError("injected process interruption")
        return transition

    monkeypatch.setattr(CtxEngine, "process", interrupt_after_commit)
    with pytest.raises(RuntimeError, match="injected process interruption"):
        dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    assert interrupted
    recovery = inspect_release_skill_recovery_status(request)
    expected_recovery = {
        "SessionStarted": (False, "no-lifecycle"),
        "IntentObserved": (True, "planned"),
        "ReassessmentRequested": (True, "pending-consent"),
        "UserDecision": (True, "decision-committed"),
    }[interrupt_after]
    assert (recovery.requires_recovery, recovery.phase) == expected_recovery

    monkeypatch.setattr(CtxEngine, "process", original_process)
    retry = replace(
        request,
        task=(
            "Debug Python exception-safe state restoration"
            if committed_revision == 1
            else "write an unrelated JavaScript button label"
        ),
        language=request.language if committed_revision == 1 else "JavaScript",
        occurred_at="2026-08-02T12:05:00Z",
    )
    result = dispatch_release_skill_install(retry, trusted_utc_now=lambda: BEFORE_EXPIRY)

    assert result.status == "installed"
    installed = request.skill_store_root / TARGET_SHA256
    assert installed.is_file()
    assert tuple(request.skill_store_root.glob(TARGET_SHA256)) == (installed,)


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_repeated_ask_recovers_exact_consent_without_replaying_prefix(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    first = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    second = dispatch_release_skill_install(
        replace(
            request,
            task="write an unrelated JavaScript button label",
            language="JavaScript",
            occurred_at="2026-08-02T12:05:00Z",
        ),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert first.status == second.status == "consent-required"
    assert first.consent == second.consent
    assert not (request.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_retry_after_durable_claim_never_reapplies_unstarted_physical_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    original_claim = SQLiteEngineStore.claim_install
    interrupted = False

    def interrupt_after_claim(store: SQLiteEngineStore, *args: object, **kwargs: object):
        nonlocal interrupted
        claim = original_claim(store, *args, **kwargs)  # type: ignore[arg-type]
        if not interrupted:
            interrupted = True
            raise RuntimeError("injected interruption after durable claim")
        return claim

    monkeypatch.setattr(SQLiteEngineStore, "claim_install", interrupt_after_claim)
    with pytest.raises(RuntimeError, match="after durable claim"):
        dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    claimed = inspect_release_skill_recovery_status(request)
    assert claimed.requires_recovery
    assert claimed.phase == "install-claimed"

    monkeypatch.setattr(SQLiteEngineStore, "claim_install", original_claim)
    result = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:05:00Z"),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert result.status == "failed"
    assert not (request.skill_store_root / TARGET_SHA256).exists()
    failed = inspect_release_skill_recovery_status(request)
    assert not failed.requires_recovery
    assert failed.phase == "terminal-failed"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_committed_auto_decision_survives_later_policy_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    from ctx.runtime import release_material

    original_load = release_material.ReleasePinnedSkillMaterialSource.load
    loads = 0

    def counted_load(
        source: object,
        driver_request: object,
        material: object,
        install_body: bytes,
    ) -> str:
        nonlocal loads
        loads += 1
        return original_load(source, driver_request, material, install_body)  # type: ignore[arg-type]

    monkeypatch.setattr(
        release_material.ReleasePinnedSkillMaterialSource,
        "load",
        counted_load,
    )
    original_process = CtxEngine.process

    def interrupt_after_decision(engine: CtxEngine, event: object):
        transition = original_process(engine, event)  # type: ignore[arg-type]
        if event.kind == "UserDecision":  # type: ignore[attr-defined]
            raise RuntimeError("injected interruption after committed decision")
        return transition

    monkeypatch.setattr(CtxEngine, "process", interrupt_after_decision)
    with pytest.raises(RuntimeError, match="committed decision"):
        dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    monkeypatch.setattr(CtxEngine, "process", original_process)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="ask-each-time"),
        request.policy_store_root,
    )
    result = dispatch_release_skill_install(
        replace(request, occurred_at="2026-08-02T12:05:00Z"),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert result.status == "installed"
    assert result.consent is not None and not result.consent.requires_prompt
    assert loads == 1
    assert (request.skill_store_root / TARGET_SHA256).is_file()


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
@pytest.mark.parametrize("boundary", ("outcome", "receipt"))
def test_retry_after_install_settlement_boundary_does_not_reapply_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    from ctx.runtime import release_material

    original_load = release_material.ReleasePinnedSkillMaterialSource.load
    loads = 0

    def counted_load(
        source: object,
        driver_request: object,
        material: object,
        install_body: bytes,
    ) -> str:
        nonlocal loads
        loads += 1
        return original_load(source, driver_request, material, install_body)  # type: ignore[arg-type]

    monkeypatch.setattr(
        release_material.ReleasePinnedSkillMaterialSource,
        "load",
        counted_load,
    )
    original_outcome = SQLiteEngineStore.record_install_outcome
    original_receipt = CtxEngine.process_install_receipt
    if boundary == "outcome":

        def interrupt_after_outcome(
            store: SQLiteEngineStore,
            *args: object,
            **kwargs: object,
        ):
            original_outcome(store, *args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected interruption after durable outcome")

        monkeypatch.setattr(
            SQLiteEngineStore,
            "record_install_outcome",
            interrupt_after_outcome,
        )
    else:

        def interrupt_after_receipt(
            engine: CtxEngine,
            *args: object,
            **kwargs: object,
        ):
            original_receipt(engine, *args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected interruption after durable receipt")

        monkeypatch.setattr(
            CtxEngine,
            "process_install_receipt",
            interrupt_after_receipt,
        )

    with pytest.raises(RuntimeError, match=f"after durable {boundary}"):
        dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    interrupted = inspect_release_skill_recovery_status(request)
    assert interrupted.requires_recovery
    assert interrupted.phase == (
        "install-outcome-recorded" if boundary == "outcome" else "installed-inactive"
    )

    if boundary == "outcome":
        monkeypatch.setattr(
            SQLiteEngineStore,
            "record_install_outcome",
            original_outcome,
        )
    else:
        monkeypatch.setattr(CtxEngine, "process_install_receipt", original_receipt)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="ask-each-time"),
        request.policy_store_root,
    )
    result = dispatch_release_skill_install(
        replace(
            request,
            task="write an unrelated JavaScript button label",
            language="JavaScript",
            occurred_at="2026-08-02T12:05:00Z",
        ),
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )

    assert result.status == "installed"
    assert result.consent is not None and not result.consent.requires_prompt
    assert loads == 1
    installed = request.skill_store_root / TARGET_SHA256
    assert tuple(request.skill_store_root.glob(TARGET_SHA256)) == (installed,)


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_recovery_status_is_terminal_after_verified_activation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    installed = dispatch_release_skill_install(
        request,
        trusted_utc_now=lambda: BEFORE_EXPIRY,
    )
    assert installed.status == "installed"
    inactive = inspect_release_skill_recovery_status(request)
    assert inactive.requires_recovery
    assert inactive.phase == "installed-inactive"

    from ctx.runtime.release_skill_lifecycle import activate_installed_release_skill

    activate_installed_release_skill(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    active = inspect_release_skill_recovery_status(request)

    assert not active.requires_recovery
    assert active.phase == "active"


@pytest.mark.skipif(os.name == "nt", reason="release skill CAS is POSIX-only")
def test_failed_body_read_recovers_without_reapplying_unclaimed_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    from ctx.runtime import release_material

    original = release_material.ReleasePinnedSkillMaterialSource.load
    calls = 0

    def fail_once(
        source: object,
        driver_request: object,
        material: object,
        install_body: bytes,
    ) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected release material interruption")
        return original(source, driver_request, material, install_body)  # type: ignore[arg-type]

    monkeypatch.setattr(release_material.ReleasePinnedSkillMaterialSource, "load", fail_once)

    first = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)
    second = dispatch_release_skill_install(request, trusted_utc_now=lambda: BEFORE_EXPIRY)

    assert first.status == "indeterminate"
    assert second.status == "failed"
    assert calls == 1
    assert second.activation_action_content_digest is None
    assert second.installed_lineage_digest is None
    assert not (request.skill_store_root / TARGET_SHA256).exists()
