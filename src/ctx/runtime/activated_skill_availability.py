"""Query-scoped availability overlay for the activated release skill.

The overlay changes only which reviewed actionability variant may enter the
global planner.  It never appends a capability after selection, and installed
bytes remain behind the one-shot activation/CAS permit until the shared query
engine has authorized the complete logical-prompt bundle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from ctx.engine.content import PreparedCapabilityContent
from ctx.engine.engine import _PromptContextMaterialPermit
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3
from ctx.engine.protocol import HostAction
from ctx.runtime.activated_skill_exposure import (
    ActivatedSkillExposureError,
    ActivatedSkillExposurePreparation,
    prepare_activated_skill_exposure,
)
from ctx.runtime.authenticated_benefit import capability_presentation_digest
from ctx.runtime.benefit_closure import (
    EligibleCatalogClaim,
    QueryCapabilityEligibility,
    eligible_catalog_claim_digest,
)
from ctx.runtime.production_catalog import RELEASE_QUERY_CATALOG_ROOT_SHA256
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.release_skill_layout import ReleaseSkillRuntimeLayout
from ctx.runtime.release_skill_lifecycle import (
    ReleaseSkillActivationError,
    inspect_activated_release_skill,
)

if TYPE_CHECKING:
    from ctx.runtime.production_catalog import ReleasePinnedQueryCatalog


_FACTORY_TOKEN = object()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True, repr=False)
class ActivatedSkillQueryAvailability:
    """Immutable availability facts and an optional one-shot CAS route."""

    activation_epoch_digest: str
    host_policy_snapshot_digest: str
    _preparation: ActivatedSkillExposurePreparation | None = field(
        repr=False,
        compare=False,
    )
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _FACTORY_TOKEN:
            raise TypeError("activated skill availability is factory-issued only")
        if self.host_policy_snapshot_digest != self.activation_epoch_digest:
            raise ValueError("availability epoch and host policy snapshot must match")

    @property
    def has_activated_release_skill(self) -> bool:
        return self._preparation is not None

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        available = presentation.actionability in {"load", "manual"}
        if presentation.capability_id == RELEASE_INSTALL_SKILL_ID:
            available = presentation.actionability == "load" and self.has_activated_release_skill
        return QueryCapabilityEligibility(
            presentation_digest=capability_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=eligible_catalog_claim_digest(claim),
            available=available,
            permissions_allowed=available,
            credentials_available=available,
        )

    def prepare_prompt_context(
        self,
        *,
        catalog: ReleasePinnedQueryCatalog,
        action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        expected_catalog_snapshot_digest: str,
        authority: _PromptContextMaterialPermit,
    ) -> tuple[PreparedCapabilityContent, ...]:
        """Route the activated selection directly to CAS under one bundle permit."""

        installed = tuple(
            selection
            for selection in selections
            if selection.presentation.capability_id == RELEASE_INSTALL_SKILL_ID
        )
        preparation = self._preparation
        if len(installed) > 1 or (installed and preparation is None):
            raise ActivatedSkillExposureError(
                "activated skill selection has no exact query availability"
            )
        return catalog.prepare_prompt_context(
            action,
            selections,
            expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
            authority=authority,
            external_material_source=(
                None if not installed or preparation is None else preparation.material_permit
            ),
        )

    def __repr__(self) -> str:
        return (
            "ActivatedSkillQueryAvailability("
            f"activation_epoch_digest={self.activation_epoch_digest!r}, "
            f"has_activated_release_skill={self.has_activated_release_skill!r})"
        )


def open_activated_skill_query_availability(
    *,
    layout: ReleaseSkillRuntimeLayout,
    task: str,
    language: str,
    occurred_at: str,
    trusted_utc_now: Callable[[], datetime] | None = None,
) -> ActivatedSkillQueryAvailability:
    """Open one fail-closed query availability snapshot from canonical state."""

    if not isinstance(layout, ReleaseSkillRuntimeLayout):
        raise TypeError("layout must be a ReleaseSkillRuntimeLayout")
    if trusted_utc_now is not None and not callable(trusted_utc_now):
        raise TypeError("trusted_utc_now must be callable or None")
    layout.assert_current()
    request = layout.install_request(
        task=task,
        language=language,
        occurred_at=occurred_at,
    )
    preparation: ActivatedSkillExposurePreparation | None = None
    try:
        evidence = inspect_activated_release_skill(request)
        preparation = prepare_activated_skill_exposure(
            request=request,
            activation_evidence=evidence,
        )
    except (ReleaseSkillActivationError, ActivatedSkillExposureError):
        preparation = None
    epoch = _digest(
        {
            "activation_preparation_digest": (
                None if preparation is None else preparation.preparation_digest
            ),
            "host_identity_digest": layout.host_identity_digest,
            "release_root_digest": RELEASE_QUERY_CATALOG_ROOT_SHA256,
            "schema": "ctx.activated-skill-query-availability-v1",
            "session_digest": _digest(layout.session_id),
            "workspace_identity_digest": layout.workspace_identity_digest,
        }
    )
    return ActivatedSkillQueryAvailability(
        activation_epoch_digest=epoch,
        host_policy_snapshot_digest=epoch,
        _preparation=preparation,
        _token=_FACTORY_TOKEN,
    )


__all__ = [
    "ActivatedSkillQueryAvailability",
    "open_activated_skill_query_availability",
]
