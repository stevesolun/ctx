"""Host-neutral reconciliation of one prompt with managed release capabilities.

This is the narrow persistent-management seam above the existing release-skill
dispatcher and activation verifier.  It never prepares or exposes capability
content.  Callers receive only an immutable availability snapshot, safe consent
metadata, and digest-only lifecycle status.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ctx.core.install_consent_broker_store import SignedHumanDecisionAssertion
from ctx.core.install_policy_store import load_current_install_policy
from ctx.runtime.activated_skill_availability import (
    ActivatedSkillQueryAvailability,
    open_activated_skill_query_availability,
)
from ctx.runtime.production_catalog import RELEASE_QUERY_CATALOG_ROOT_SHA256
from ctx.runtime.install_consent_broker import InstallConsentBrokerService
from ctx.runtime.release_skill_dispatcher import (
    ReleaseSkillConsentChallengeProjection,
    ReleaseSkillDispatchError,
    dispatch_release_skill_install,
    inspect_release_skill_recovery_status,
    probe_release_skill_install_relevance,
)
from ctx.runtime.release_skill_layout import ReleaseSkillRuntimeLayout
from ctx.runtime.release_skill_lifecycle import (
    ReleaseSkillActivationError,
    activate_installed_release_skill,
)
from ctx.utils._file_lock import secure_file_lock
from ctx.utils._fs_utils import ensure_secure_directory


ManagedPromptStatus = Literal[
    "available",
    "abstained",
    "consent-required",
    "denied",
    "failed",
]

_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_FAILURE_CODES = frozenset(
    {
        "activation-failed",
        "availability-unverified",
        "dispatch-failed",
        "install-denied",
        "install-failed",
        "install-indeterminate",
        "install-result-invalid",
        "policy-unavailable",
        "recovery-inspection-failed",
        "relevance-probe-failed",
    }
)
_RECONCILIATION_LOCK_DIRECTORY = "prompt-reconciliation-locks-v1"
_RECONCILIATION_LOCK_TIMEOUT_SECONDS = 30.0


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedConsentDirective:
    """Non-resumable recommendation to seek separate authenticated consent.

    This is deliberately not an engine ``InstallConsentDirective``: no durable
    consent request exists, and no field can be submitted as a user decision.
    """

    capability_id: str
    kind: str
    policy_snapshot_digest: str
    release_root_digest: str
    planning_environment_digest: str
    reason_code: str = "ask-each-time-recommendation"
    recommendation_only: bool = True
    resumable: bool = False

    def __post_init__(self) -> None:
        if self.capability_id != "skill:ctx-python-state-protocols" or self.kind != "skill":
            raise ValueError("managed consent recommendation has the wrong capability")
        for field_name in (
            "policy_snapshot_digest",
            "release_root_digest",
            "planning_environment_digest",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
        if self.release_root_digest != RELEASE_QUERY_CATALOG_ROOT_SHA256:
            raise ValueError("managed consent recommendation has the wrong release root")
        if self.reason_code != "ask-each-time-recommendation":
            raise ValueError("managed consent recommendation reason is invalid")
        if self.recommendation_only is not True or self.resumable is not False:
            raise ValueError("managed consent recommendation cannot carry decision authority")

    @property
    def requires_prompt(self) -> bool:
        return True


def _directive_identity(directive: ManagedConsentDirective) -> dict[str, object]:
    return {
        "capability_id": directive.capability_id,
        "kind": directive.kind,
        "planning_environment_digest": directive.planning_environment_digest,
        "policy_snapshot_digest": directive.policy_snapshot_digest,
        "reason_code": directive.reason_code,
        "recommendation_only": directive.recommendation_only,
        "release_root_digest": directive.release_root_digest,
        "resumable": directive.resumable,
    }


def _challenge_identity(
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


def _management_epoch_digest(
    *,
    status: ManagedPromptStatus,
    availability: ActivatedSkillQueryAvailability,
    consent_directives: tuple[ManagedConsentDirective, ...],
    consent_challenges: tuple[ReleaseSkillConsentChallengeProjection, ...],
    failure_code: str | None,
) -> str:
    return _digest(
        {
            "availability_epoch_digest": availability.activation_epoch_digest,
            "consent_directives": [
                _directive_identity(directive) for directive in consent_directives
            ],
            "consent_challenges": [
                _challenge_identity(challenge) for challenge in consent_challenges
            ],
            "failure_code": failure_code,
            "release_root_digest": RELEASE_QUERY_CATALOG_ROOT_SHA256,
            "schema": "ctx.managed-prompt-outcome-v2",
            "status": status,
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedPromptOutcome:
    """Authority-free result of reconciling one prompt with managed state.

    ``availability`` can route verified material only when a separate exact
    engine bundle permit is supplied.  The outcome itself carries no host
    action, execution handle, content, path, command, credential, or decision
    authority.
    """

    status: ManagedPromptStatus
    availability: ActivatedSkillQueryAvailability
    consent_directives: tuple[ManagedConsentDirective, ...]
    management_epoch_digest: str
    consent_challenges: tuple[ReleaseSkillConsentChallengeProjection, ...] = ()
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {
            "available",
            "abstained",
            "consent-required",
            "denied",
            "failed",
        }:
            raise ValueError("managed prompt status is invalid")
        if type(self.availability) is not ActivatedSkillQueryAvailability:
            raise TypeError("availability must be an exact activated-skill snapshot")
        if not isinstance(self.consent_directives, tuple) or not all(
            isinstance(item, ManagedConsentDirective) for item in self.consent_directives
        ):
            raise TypeError("consent_directives must be an immutable directive tuple")
        if not 0 <= len(self.consent_directives) <= 5:
            raise ValueError("consent_directives exceeds the global capability bound")
        if not isinstance(self.consent_challenges, tuple) or not all(
            type(item) is ReleaseSkillConsentChallengeProjection for item in self.consent_challenges
        ):
            raise TypeError("consent_challenges must be an immutable challenge tuple")
        if not 0 <= len(self.consent_challenges) <= 5:
            raise ValueError("consent_challenges exceeds the global capability bound")
        if len(self.consent_directives) + len(self.consent_challenges) > 5:
            raise ValueError("managed outcome exceeds the global capability bound")
        capability_ids = tuple(item.capability_id for item in self.consent_directives)
        challenge_capability_ids = tuple(item.capability_id for item in self.consent_challenges)
        all_capability_ids = capability_ids + challenge_capability_ids
        if len(all_capability_ids) != len(set(all_capability_ids)):
            raise ValueError("managed outcome contains duplicate consent identities")
        if self.status == "available":
            if not self.availability.has_activated_release_skill:
                raise ValueError("available outcome requires verified active availability")
            if self.consent_directives or self.consent_challenges or self.failure_code is not None:
                raise ValueError("available outcome cannot carry consent or failure metadata")
        elif self.status == "consent-required":
            if self.availability.has_activated_release_skill:
                raise ValueError("active availability cannot require installation consent")
            if (
                bool(self.consent_directives) == bool(self.consent_challenges)
                or not all(item.requires_prompt for item in self.consent_directives)
                or self.failure_code is not None
            ):
                raise ValueError(
                    "consent-required outcome needs exactly one consent representation"
                )
        elif self.status == "abstained":
            if (
                self.availability.has_activated_release_skill
                or self.consent_directives
                or self.consent_challenges
                or self.failure_code is not None
            ):
                raise ValueError("abstained outcome cannot expose managed lifecycle state")
        elif self.status == "denied":
            if (
                self.availability.has_activated_release_skill
                or self.consent_directives
                or self.consent_challenges
                or self.failure_code is not None
            ):
                raise ValueError("denied outcome must remain inactive and authority-free")
        elif (
            self.availability.has_activated_release_skill
            or self.consent_directives
            or self.consent_challenges
            or self.failure_code not in _FAILURE_CODES
        ):
            raise ValueError("failed outcome must remain closed with a safe failure code")
        if (
            not isinstance(self.management_epoch_digest, str)
            or _DIGEST_RE.fullmatch(self.management_epoch_digest) is None
            or self.management_epoch_digest
            != _management_epoch_digest(
                status=self.status,
                availability=self.availability,
                consent_directives=self.consent_directives,
                consent_challenges=self.consent_challenges,
                failure_code=self.failure_code,
            )
        ):
            raise ValueError("management_epoch_digest does not match the outcome")


def _outcome(
    *,
    status: ManagedPromptStatus,
    availability: ActivatedSkillQueryAvailability,
    consent_directives: tuple[ManagedConsentDirective, ...] = (),
    consent_challenges: tuple[ReleaseSkillConsentChallengeProjection, ...] = (),
    failure_code: str | None = None,
) -> ManagedPromptOutcome:
    return ManagedPromptOutcome(
        status=status,
        availability=availability,
        consent_directives=consent_directives,
        consent_challenges=consent_challenges,
        failure_code=failure_code,
        management_epoch_digest=_management_epoch_digest(
            status=status,
            availability=availability,
            consent_directives=consent_directives,
            consent_challenges=consent_challenges,
            failure_code=failure_code,
        ),
    )


def _trusted_timestamp(source: Callable[[], datetime] | None) -> datetime:
    try:
        value = datetime.now(UTC) if source is None else source()
    except Exception:
        raise RuntimeError("trusted UTC clock failed") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RuntimeError("trusted UTC clock failed")
    return value.astimezone(UTC)


def _occurred_at(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _reconciliation_lock_target(layout: ReleaseSkillRuntimeLayout):
    layout.assert_current()
    lock_root = layout.managed_root / _RECONCILIATION_LOCK_DIRECTORY
    ensure_secure_directory(lock_root)
    return lock_root / f"workspace-{layout.workspace_identity_digest}.reconcile"


def reconcile_prompt_capabilities(
    *,
    layout: ReleaseSkillRuntimeLayout,
    task: str,
    language: str,
    consent_broker: InstallConsentBrokerService | None = None,
    decision_assertion: SignedHumanDecisionAssertion | None = None,
    trusted_utc_now: Callable[[], datetime] | None = None,
) -> ManagedPromptOutcome:
    """Reconcile one current prompt through the reviewed release-skill path.

    Existing active state is inspected before any dispatch.  An absent relevant
    skill may be installed only through the existing consent, claim, receipt,
    and activation boundaries.  Any incomplete transition returns the original
    non-active availability snapshot.
    """

    if not isinstance(layout, ReleaseSkillRuntimeLayout):
        raise TypeError("layout must be a ReleaseSkillRuntimeLayout")
    if not isinstance(task, str) or not isinstance(language, str):
        raise TypeError("task and language must be strings")
    if consent_broker is not None and type(consent_broker) is not InstallConsentBrokerService:
        raise TypeError("consent_broker must be an InstallConsentBrokerService or None")
    if (
        decision_assertion is not None
        and type(decision_assertion) is not SignedHumanDecisionAssertion
    ):
        raise TypeError("decision_assertion must be a SignedHumanDecisionAssertion or None")
    if consent_broker is None and decision_assertion is not None:
        raise ValueError("decision_assertion requires a consent broker")
    if trusted_utc_now is not None and not callable(trusted_utc_now):
        raise TypeError("trusted_utc_now must be callable or None")

    with secure_file_lock(
        _reconciliation_lock_target(layout),
        timeout=_RECONCILIATION_LOCK_TIMEOUT_SECONDS,
    ):
        trusted_now = _trusted_timestamp(trusted_utc_now)
        occurred_at = _occurred_at(trusted_now)

        def fixed_clock() -> datetime:
            return trusted_now

        initial = open_activated_skill_query_availability(
            layout=layout,
            task=task,
            language=language,
            occurred_at=occurred_at,
        )
        if initial.has_activated_release_skill:
            if decision_assertion is not None:
                raise ValueError("active capability cannot accept a consent assertion")
            return _outcome(status="available", availability=initial)

        request = layout.install_request(
            task=task,
            language=language,
            occurred_at=occurred_at,
        )
        try:
            recovery = inspect_release_skill_recovery_status(request)
        except (ReleaseSkillDispatchError, OSError, RuntimeError, ValueError):
            return _outcome(
                status="failed",
                availability=initial,
                failure_code="recovery-inspection-failed",
            )
        if not recovery.requires_recovery:
            if decision_assertion is not None:
                return _outcome(
                    status="failed",
                    availability=initial,
                    failure_code="install-result-invalid",
                )
            try:
                probe = probe_release_skill_install_relevance(request)
            except (ReleaseSkillDispatchError, OSError, RuntimeError, ValueError):
                return _outcome(
                    status="failed",
                    availability=initial,
                    failure_code="relevance-probe-failed",
                )
            if not probe.relevant:
                return _outcome(status="abstained", availability=initial)
            try:
                policy = load_current_install_policy(layout.policy_store_root)
            except (OSError, RuntimeError, ValueError):
                return _outcome(
                    status="failed",
                    availability=initial,
                    failure_code="policy-unavailable",
                )
            if policy.skill_mode == "ask-each-time" and consent_broker is None:
                return _outcome(
                    status="consent-required",
                    availability=initial,
                    consent_directives=(
                        ManagedConsentDirective(
                            capability_id=probe.capability_id or "",
                            kind="skill",
                            policy_snapshot_digest=policy.policy_digest,
                            release_root_digest=probe.release_root_digest,
                            planning_environment_digest=probe.planning_environment_digest,
                        ),
                    ),
                )

        try:
            dispatched = dispatch_release_skill_install(
                request,
                consent_broker=consent_broker,
                decision_assertion=decision_assertion,
                trusted_utc_now=fixed_clock,
            )
        except (ReleaseSkillDispatchError, OSError, RuntimeError, ValueError):
            return _outcome(
                status="failed",
                availability=initial,
                failure_code="dispatch-failed",
            )

        if dispatched.status in {"failed", "indeterminate"}:
            return _outcome(
                status="failed",
                availability=initial,
                failure_code=f"install-{dispatched.status}",
            )
        if dispatched.status == "consent-required":
            if dispatched.challenge is None or consent_broker is None:
                return _outcome(
                    status="failed",
                    availability=initial,
                    failure_code="install-result-invalid",
                )
            return _outcome(
                status="consent-required",
                availability=initial,
                consent_challenges=(dispatched.challenge,),
            )
        if dispatched.status == "denied":
            return _outcome(status="denied", availability=initial)
        if dispatched.status != "installed":
            return _outcome(
                status="failed",
                availability=initial,
                failure_code="install-result-invalid",
            )

        try:
            activate_installed_release_skill(
                request,
                trusted_utc_now=fixed_clock,
            )
        except (ReleaseSkillActivationError, OSError, RuntimeError, ValueError):
            return _outcome(
                status="failed",
                availability=initial,
                failure_code="activation-failed",
            )
        try:
            reopened = open_activated_skill_query_availability(
                layout=layout,
                task=task,
                language=language,
                occurred_at=occurred_at,
            )
        except Exception:
            return _outcome(
                status="failed",
                availability=initial,
                failure_code="availability-unverified",
            )
        if not reopened.has_activated_release_skill:
            return _outcome(
                status="failed",
                availability=initial,
                failure_code="availability-unverified",
            )
        return _outcome(status="available", availability=reopened)


__all__ = [
    "ManagedConsentDirective",
    "ManagedPromptOutcome",
    "ManagedPromptStatus",
    "reconcile_prompt_capabilities",
]
