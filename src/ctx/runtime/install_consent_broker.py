"""Host-neutral runtime composition for authenticated install consent.

This service joins already-authenticated planner, action, driver, workspace,
and release identities into one durable consent challenge.  It never accepts
prompt text as authority and it does not replace the engine's independent,
one-shot install claim for the same execution binding.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from ctx.core.install_consent_broker_store import (
    ConsentBrokerChallengeNotFound,
    ConsentBrokerReconciliationReport,
    ConsentBrokerReplay,
    HumanDecisionVerifier,
    InstallConsentChallenge,
    InstallConsentChallengeRecord,
    SQLiteInstallConsentBrokerStore,
    SignedHumanDecisionAssertion,
    VerifiedHumanDecision,
)
from ctx.engine.installation import (
    CommittedInstallDecisionEvidenceProvider,
    InstallConsentDirective,
    InstallDecisionEvidenceQuery,
    InstallExecutionBinding,
    InteractiveInstallDecisionGuard,
    InteractiveInstallDecisionReservation,
)
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, InstallPlanningAuthority
from ctx.engine.protocol import INSTALL_ACTION_PAYLOAD_SCHEMA_V3, HostAction


_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SELECTION_BINDING_SCHEMA = "ctx.install-consent-selection-binding-v1"


class InstallConsentBrokerBindingError(ValueError):
    """Typed runtime authorities do not describe one exact install."""


class InstallConsentVerifierUnavailable(RuntimeError):
    """Trusted composition did not supply a human-decision verifier."""


class InstallConsentEvidenceProviderUnavailable(RuntimeError):
    """Trusted composition did not bind a canonical engine evidence provider."""


InstallConsentReconciliationReport = ConsentBrokerReconciliationReport


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def install_consent_selection_digest(selection: CapabilityPlanSelectionV3) -> str:
    """Digest the complete validated selection row without persisting its contents."""

    if not isinstance(selection, CapabilityPlanSelectionV3):
        raise TypeError("selection must be a CapabilityPlanSelectionV3")
    payload = json.dumps(
        {
            "schema": _SELECTION_BINDING_SCHEMA,
            "selection": selection.to_mapping(),
        },
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def derive_install_consent_challenge(
    *,
    directive: InstallConsentDirective,
    selection: CapabilityPlanSelectionV3,
    install_action: HostAction,
    execution_binding: InstallExecutionBinding,
    workspace_identity_digest: str,
    release_root_digest: str,
    audience: str,
) -> InstallConsentChallenge:
    """Derive one challenge only after all typed identities agree exactly."""

    if not isinstance(directive, InstallConsentDirective):
        raise TypeError("directive must be an InstallConsentDirective")
    if not isinstance(selection, CapabilityPlanSelectionV3):
        raise TypeError("selection must be a CapabilityPlanSelectionV3")
    if not isinstance(install_action, HostAction):
        raise TypeError("install_action must be a HostAction")
    if not isinstance(execution_binding, InstallExecutionBinding):
        raise TypeError("execution_binding must be an InstallExecutionBinding")
    workspace_identity_digest = _digest(workspace_identity_digest, "workspace_identity_digest")
    release_root_digest = _digest(release_root_digest, "release_root_digest")
    if not directive.requires_prompt:
        raise InstallConsentBrokerBindingError(
            "interactive consent broker requires an interactive directive"
        )
    authority = selection.authority
    if not isinstance(authority, InstallPlanningAuthority):
        raise InstallConsentBrokerBindingError(
            "interactive install consent requires exact install planning authority"
        )
    descriptor = authority.descriptor
    material = authority.result_material
    presentation = selection.presentation

    directive_identity: tuple[object, ...] = (
        directive.capability_id,
        directive.kind,
        directive.source_digest,
        directive.install_plan_digest,
        directive.descriptor_digest,
        directive.installer_id,
        directive.provenance_digest,
        directive.permission_expansion,
        directive.credential_requirement,
        directive.result_material_identity_digest,
    )
    selection_identity: tuple[object, ...] = (
        presentation.capability_id,
        presentation.kind,
        presentation.source_digest,
        descriptor.plan_digest,
        descriptor.descriptor_digest,
        descriptor.installer_id,
        descriptor.provenance_digest,
        descriptor.permission_expansion,
        descriptor.credential_requirement,
        material.identity_digest,
    )
    if directive_identity != selection_identity:
        raise InstallConsentBrokerBindingError(
            "directive does not match the exact selection, descriptor, and material"
        )
    expected_reason = (
        "permission-expansion-requires-consent"
        if descriptor.permission_expansion
        else (
            "credentials-require-consent"
            if descriptor.credential_requirement
            else "per-install-consent-required"
        )
    )
    if directive.reason_code != expected_reason:
        raise InstallConsentBrokerBindingError(
            "directive does not carry the exact interactive consent reason"
        )

    action_identity: tuple[object, ...] = (
        install_action.kind,
        install_action.entity_id,
        install_action.source_digest,
        install_action.catalog_snapshot_id,
        install_action.plan_id,
        install_action.consent_id,
        install_action.action_id,
        install_action.content_digest,
        install_action.precondition_revision,
    )
    directive_action_identity: tuple[object, ...] = (
        directive.requested_action_kind,
        directive.capability_id,
        directive.source_digest,
        directive.catalog_snapshot_digest,
        directive.plan_id,
        directive.consent_id,
        directive.requested_action_id,
        directive.requested_action_content_digest,
        directive.requested_action_precondition_revision,
    )
    if action_identity != directive_action_identity or install_action.expires_at is None:
        raise InstallConsentBrokerBindingError(
            "directive does not match the exact pending install action"
        )
    if install_action.required_host_feature != "installation":
        raise InstallConsentBrokerBindingError(
            "pending action is not an exact installation host effect"
        )

    if (
        execution_binding.driver_id != descriptor.installer_id
        or execution_binding.driver_digest != install_action.payload.get("installer_digest")
    ):
        raise InstallConsentBrokerBindingError(
            "execution driver does not match the exact install descriptor and action"
        )

    expected_action_payload: dict[str, object] = {
        "capability_kind": presentation.kind,
        "catalog_identity": selection.catalog_identity.to_dict(),
        "install_plan_descriptor": descriptor.to_dict(),
        "installer_digest": execution_binding.driver_digest,
        "policy_snapshot_digest": directive.policy_snapshot_digest,
        "result_material": material.to_dict(),
        "schema": INSTALL_ACTION_PAYLOAD_SCHEMA_V3,
    }
    if _plain_mapping(install_action.payload) != expected_action_payload:
        raise InstallConsentBrokerBindingError(
            "pending action payload does not match the exact install authority"
        )
    return InstallConsentChallenge(
        challenge_id=directive.consent_id,
        audience=audience,
        workspace_identity_digest=workspace_identity_digest,
        scope=install_action.scope,
        capability_id=presentation.capability_id,
        kind=presentation.kind,
        source_digest=presentation.source_digest,
        catalog_snapshot_digest=directive.catalog_snapshot_digest,
        plan_id=directive.plan_id,
        install_plan_digest=descriptor.plan_digest,
        descriptor_digest=descriptor.descriptor_digest,
        execution_binding_digest=execution_binding.binding_digest,
        selection_digest=install_consent_selection_digest(selection),
        material_identity_digest=material.identity_digest,
        requested_action_id=install_action.action_id,
        requested_action_kind=install_action.kind,
        requested_action_content_digest=install_action.content_digest,
        requested_action_precondition_revision=install_action.precondition_revision,
        policy_snapshot_digest=directive.policy_snapshot_digest,
        release_root_digest=release_root_digest,
        permission_expansion=descriptor.permission_expansion,
        credential_requirement=descriptor.credential_requirement,
        expires_at=install_action.expires_at,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedInstallConsent:
    """Live, non-serializable human authority for one exact challenge."""

    challenge: InstallConsentChallenge
    decision: VerifiedHumanDecision = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.challenge, InstallConsentChallenge):
            raise TypeError("challenge must be an InstallConsentChallenge")
        if not isinstance(self.decision, VerifiedHumanDecision):
            raise TypeError("decision must be a VerifiedHumanDecision")
        if (
            self.decision.challenge_id != self.challenge.challenge_id
            or self.decision.challenge_digest != self.challenge.challenge_digest
        ):
            raise InstallConsentBrokerBindingError(
                "verified decision does not match the exact install challenge"
            )


class InstallConsentBrokerService:
    """Compose a durable broker store with one trusted authenticator port.

    A prepare-only service may omit the verifier so an untrusted host hook can
    durably publish a challenge.  Such a service cannot authenticate an
    assertion.  A verifier is injected once by trusted runtime composition for
    continuation.  Individual calls cannot replace it with prompt text, a model
    callback, or a per-request verifier.  The returned interactive guard remains
    independent from the engine's later install-action claim, which must
    revalidate the same ``InstallExecutionBinding`` again.
    """

    __slots__ = (
        "_release_root_digest",
        "_evidence_provider",
        "_store",
        "_trusted_utc_now",
        "_verifier",
        "_workspace_identity_digest",
    )
    _release_root_digest: str
    _evidence_provider: CommittedInstallDecisionEvidenceProvider | None
    _store: SQLiteInstallConsentBrokerStore
    _trusted_utc_now: Callable[[], datetime]
    _verifier: HumanDecisionVerifier | None
    _workspace_identity_digest: str

    def __init__(
        self,
        *,
        store: SQLiteInstallConsentBrokerStore,
        verifier: HumanDecisionVerifier | None,
        evidence_provider: CommittedInstallDecisionEvidenceProvider | None = None,
        workspace_identity_digest: str,
        release_root_digest: str,
        trusted_utc_now: Callable[[], datetime],
    ) -> None:
        if not isinstance(store, SQLiteInstallConsentBrokerStore):
            raise TypeError("store must be a SQLiteInstallConsentBrokerStore")
        if verifier is not None and not isinstance(verifier, HumanDecisionVerifier):
            raise TypeError(
                "verifier must implement the trusted HumanDecisionVerifier port or be None"
            )
        if evidence_provider is not None and not isinstance(
            evidence_provider, CommittedInstallDecisionEvidenceProvider
        ):
            raise TypeError(
                "evidence_provider must implement "
                "CommittedInstallDecisionEvidenceProvider or be None"
            )
        if not callable(trusted_utc_now):
            raise TypeError("trusted_utc_now must be callable")
        object.__setattr__(self, "_store", store)
        object.__setattr__(self, "_verifier", verifier)
        object.__setattr__(self, "_evidence_provider", evidence_provider)
        object.__setattr__(
            self,
            "_workspace_identity_digest",
            _digest(workspace_identity_digest, "workspace_identity_digest"),
        )
        object.__setattr__(
            self,
            "_release_root_digest",
            _digest(release_root_digest, "release_root_digest"),
        )
        object.__setattr__(self, "_trusted_utc_now", trusted_utc_now)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("install consent broker service is immutable")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("install consent broker service is immutable")

    @property
    def audience(self) -> str:
        """Return the audience durably bound to this broker service."""

        return self._store.audience

    def prepare(
        self,
        *,
        directive: InstallConsentDirective,
        selection: CapabilityPlanSelectionV3,
        install_action: HostAction,
        execution_binding: InstallExecutionBinding,
    ) -> InstallConsentChallenge:
        """Derive and durably record one exact pending challenge."""

        challenge = derive_install_consent_challenge(
            directive=directive,
            selection=selection,
            install_action=install_action,
            execution_binding=execution_binding,
            workspace_identity_digest=self._workspace_identity_digest,
            release_root_digest=self._release_root_digest,
            audience=self._store.audience,
        )
        now = self._trusted_utc_now()
        self._store.create_challenge(challenge, now=now)
        return challenge

    def with_verifier(self, verifier: HumanDecisionVerifier) -> InstallConsentBrokerService:
        """Return the same durable broker bound to one registry-selected verifier."""

        if not isinstance(verifier, HumanDecisionVerifier):
            raise TypeError("verifier must implement the trusted HumanDecisionVerifier port")
        return InstallConsentBrokerService(
            store=self._store,
            verifier=verifier,
            evidence_provider=self._evidence_provider,
            workspace_identity_digest=self._workspace_identity_digest,
            release_root_digest=self._release_root_digest,
            trusted_utc_now=self._trusted_utc_now,
        )

    def with_evidence_provider(
        self,
        provider: CommittedInstallDecisionEvidenceProvider,
    ) -> InstallConsentBrokerService:
        """Return the same durable broker bound to its owner-held journal."""

        if not isinstance(provider, CommittedInstallDecisionEvidenceProvider):
            raise TypeError("provider must implement CommittedInstallDecisionEvidenceProvider")
        return InstallConsentBrokerService(
            store=self._store,
            verifier=self._verifier,
            evidence_provider=provider,
            workspace_identity_digest=self._workspace_identity_digest,
            release_root_digest=self._release_root_digest,
            trusted_utc_now=self._trusted_utc_now,
        )

    def authenticate(
        self,
        challenge: InstallConsentChallenge,
        assertion: SignedHumanDecisionAssertion,
    ) -> AuthenticatedInstallConsent:
        """Authenticate signed human claims and make the decision ready once."""

        if not isinstance(challenge, InstallConsentChallenge):
            raise TypeError("challenge must be an InstallConsentChallenge")
        if not isinstance(assertion, SignedHumanDecisionAssertion):
            raise TypeError("assertion must be a SignedHumanDecisionAssertion")
        if self._verifier is None:
            raise InstallConsentVerifierUnavailable(
                "install consent authentication requires a trusted verifier"
            )
        self._require_service_challenge(challenge)
        now = self._trusted_utc_now()
        before = self._store.get(challenge.challenge_id, now=now)
        if before.challenge != challenge:
            raise InstallConsentBrokerBindingError(
                "submitted challenge does not match the exact persisted challenge"
            )
        decision = self._store.verify_human_decision(
            challenge.challenge_id,
            assertion,
            self._verifier,
            now=now,
        )
        if before.state in {"pending", "reauthentication-required"}:
            try:
                self._store.mark_decision_ready(decision, now=now)
            except ConsentBrokerReplay:
                # Another same-user process may have won the transition after
                # verification.  Accept only its byte-equivalent ready record;
                # the core guard rechecks this process-bound decision again.
                concurrent = self._store.get(challenge.challenge_id, now=now)
                if not _record_matches_decision(concurrent, decision):
                    raise
        elif before.state != "decision-ready":
            raise ConsentBrokerReplay(f"install consent record is already {before.state}")
        after = self._store.get(challenge.challenge_id, now=now)
        if not _record_matches_decision(after, decision):
            raise InstallConsentBrokerBindingError(
                "authenticated decision does not match the decision-ready record"
            )
        return AuthenticatedInstallConsent(challenge=challenge, decision=decision)

    def interactive_guard(
        self,
        authorization: AuthenticatedInstallConsent,
        *,
        execution_binding: InstallExecutionBinding,
    ) -> InteractiveInstallDecisionGuard:
        """Return an engine guard after independently rechecking driver binding."""

        if not isinstance(authorization, AuthenticatedInstallConsent):
            raise TypeError("authorization must be an AuthenticatedInstallConsent")
        if not isinstance(execution_binding, InstallExecutionBinding):
            raise TypeError("execution_binding must be an InstallExecutionBinding")
        self._require_service_challenge(authorization.challenge)
        if execution_binding.binding_digest != authorization.challenge.execution_binding_digest:
            raise InstallConsentBrokerBindingError(
                "post-consent execution binding does not match the exact challenge"
            )
        return self._store.interactive_guard(
            authorization.decision,
            now=self._trusted_utc_now,
        )

    def status(self, challenge_id: str) -> InstallConsentChallengeRecord:
        """Return privacy-safe status without producing authority."""

        return self._store.get(challenge_id, now=self._trusted_utc_now())

    def status_by_challenge_digest(
        self,
        challenge_digest: str,
    ) -> InstallConsentChallengeRecord:
        """Return one exact privacy-safe challenge status by assertion digest."""

        record = self._store.get_by_challenge_digest(
            challenge_digest,
            expected_workspace_identity_digest=self._workspace_identity_digest,
            expected_release_root_digest=self._release_root_digest,
            now=self._trusted_utc_now(),
        )
        self._require_service_challenge(record.challenge)
        return record

    def reconcile_install_decision(
        self,
        *,
        query: InstallDecisionEvidenceQuery,
        reservation: InteractiveInstallDecisionReservation,
    ) -> InstallConsentReconciliationReport:
        """Reconcile a crash state against one authoritative engine store."""

        if self._evidence_provider is None:
            raise InstallConsentEvidenceProviderUnavailable(
                "install consent reconciliation requires the canonical evidence provider"
            )
        before = self._store.inspect_record(query.consent_id)
        self._require_service_challenge(before.challenge)
        report = self._store.reconcile_install_decision(
            provider=self._evidence_provider,
            query=query,
            reservation=reservation,
            now=self._trusted_utc_now(),
        )
        self._require_service_challenge(report.record.challenge)
        return report

    def _require_service_challenge(self, challenge: InstallConsentChallenge) -> None:
        if (
            challenge.workspace_identity_digest != self._workspace_identity_digest
            or challenge.release_root_digest != self._release_root_digest
            or challenge.audience != self._store.audience
        ):
            raise InstallConsentBrokerBindingError(
                "challenge does not match this workspace and release root"
            )


def _plain_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Thaw the known JSON payload enough for exact typed identity comparison."""

    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[key] = _plain_mapping(item)
        elif isinstance(item, tuple):
            result[key] = [
                _plain_mapping(member) if isinstance(member, Mapping) else member for member in item
            ]
        else:
            result[key] = item
    return result


def _record_matches_decision(
    record: InstallConsentChallengeRecord,
    decision: VerifiedHumanDecision,
) -> bool:
    return bool(
        record.state == "decision-ready"
        and record.challenge.challenge_id == decision.challenge_id
        and record.challenge.challenge_digest == decision.challenge_digest
        and record.decision == decision.decision
        and record.principal_digest == decision.principal_digest
        and record.authenticator_id == decision.authenticator_id
        and record.audience == decision.audience
        and record.assertion_nonce_digest == decision.assertion_nonce_digest
        and record.decision_issued_at == decision.issued_at
        and record.decision_expires_at == decision.expires_at
    )


__all__ = [
    "AuthenticatedInstallConsent",
    "ConsentBrokerChallengeNotFound",
    "InstallConsentBrokerBindingError",
    "InstallConsentBrokerService",
    "InstallConsentEvidenceProviderUnavailable",
    "InstallConsentReconciliationReport",
    "InstallConsentVerifierUnavailable",
    "derive_install_consent_challenge",
    "install_consent_selection_digest",
]
