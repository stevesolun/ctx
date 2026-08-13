"""One-shot installed-skill material for the shared logical-prompt pipeline.

This module stops at :class:`PreparedCapabilityContent`.  It neither writes a
host response nor records provider exposure; the existing prepared-delivery
factory and shared query-delivery ledger remain the only downstream emission
path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from ctx.engine.content import (
    MAX_PREPARED_CONTENT_TOKENS,
    AuthorizedMaterial,
    MaterialIdentity,
    PreparedCapabilityContent,
)
from ctx.engine.engine import CtxEngine, _PromptContextMaterialRoutePermit
from ctx.engine.installation import InstallExecutionBinding
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, LoadPlanningAuthority
from ctx.engine.protocol import HostAction
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION
from ctx.engine.replay import DefaultReplayInputFactory
from ctx.engine.state import CapabilityStateV3
from ctx.engine.store import SQLiteEngineStore, StreamId
from ctx.runtime._skill_cas_posix import (
    RootIdentity,
    SkillCasFilesystemError,
    open_skill_cas_directory,
    skill_cas_root_identity,
)
from ctx.runtime.release_skill_dispatcher import (
    ReleaseSkillInstallRequest,
    _release_skill_host_descriptor_digest,
    _scope,
)
from ctx.runtime.release_skill_lifecycle import (
    _ACTIVATION_VERIFIER_DIGEST,
    _ACTIVATION_VERIFIER_ID,
    _require_existing_journal,
    ReleaseSkillActivationError,
    ReleaseSkillActivationEvidence,
    _exact_activation_action,
    _exact_activation_receipt,
    _exact_install_action,
    _verify_install_target_binding,
)
from ctx.utils._file_lock import secure_file_lock


_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_FACTORY_TOKEN = object()


class ActivatedSkillExposureError(RuntimeError):
    """Activated installed material cannot be prepared without changing authority."""


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


def _required_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ActivatedSkillExposureError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivatedSkillExposurePreparation:
    """Digest-only preparation evidence plus one process-local material permit."""

    activation_evidence_digest: str
    installed_lineage_digest: str
    material_identity_digest: str
    skill_cas_root_identity_digest: str
    preparation_digest: str
    material_permit: ActivatedSkillMaterialPermit = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for field_name in (
            "activation_evidence_digest",
            "installed_lineage_digest",
            "material_identity_digest",
            "skill_cas_root_identity_digest",
            "preparation_digest",
        ):
            _required_digest(getattr(self, field_name), field_name)
        if type(self.material_permit) is not ActivatedSkillMaterialPermit:
            raise ActivatedSkillExposureError(
                "material_permit must be an exact activated-skill permit"
            )
        expected = _preparation_digest(
            activation_evidence_digest=self.activation_evidence_digest,
            installed_lineage_digest=self.installed_lineage_digest,
            material_identity_digest=self.material_identity_digest,
            skill_cas_root_identity_digest=self.skill_cas_root_identity_digest,
            permit_digest=self.material_permit.permit_digest,
        )
        if self.preparation_digest != expected:
            raise ActivatedSkillExposureError("preparation digest does not match its authority")


class ActivatedSkillMaterialPermit:
    """Process-bound one-shot bridge into PreparedCapabilityContent.

    Returning prepared content is not evidence of host or provider use.  The
    caller must still build a journal-receipted PreparedQueryDelivery and pass
    it through QueryDeliveryController's shared terminal ledger.
    """

    __slots__ = (
        "_activation_evidence",
        "_authorized_material",
        "_consumed",
        "_lock",
        "_material_identity",
        "_permit_digest",
        "_pid",
        "_request",
    )

    def __init__(
        self,
        *,
        request: ReleaseSkillInstallRequest,
        activation_evidence: ReleaseSkillActivationEvidence,
        authorized_material: AuthorizedMaterial,
        material_identity: MaterialIdentity,
        permit_digest: str,
        _token: object,
    ) -> None:
        if _token is not _FACTORY_TOKEN:
            raise TypeError("activated skill material permits are factory-issued only")
        self._request = request
        self._activation_evidence = activation_evidence
        self._authorized_material = authorized_material
        if not isinstance(material_identity, MaterialIdentity):
            raise TypeError("material_identity must be a MaterialIdentity")
        self._material_identity = material_identity
        self._permit_digest = _required_digest(permit_digest, "permit_digest")
        self._pid = os.getpid()
        self._lock = Lock()
        self._consumed = False

    @property
    def permit_digest(self) -> str:
        return self._permit_digest

    def prepare_prompt_context_once(
        self,
        *,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
        selections: tuple[CapabilityPlanSelectionV3, ...] | None = None,
        expected_catalog_snapshot_digest: str,
        route_authority: _PromptContextMaterialRoutePermit,
    ) -> PreparedCapabilityContent:
        """Prepare one exact body for a journal-authorized logical prompt."""

        if not isinstance(action, HostAction):
            raise TypeError("action must be a HostAction")
        if not isinstance(selection, CapabilityPlanSelectionV3):
            raise TypeError("selection must be a CapabilityPlanSelectionV3")
        bundle = (selection,) if selections is None else selections
        if (
            not isinstance(bundle, tuple)
            or not 1 <= len(bundle) <= 5
            or not all(isinstance(item, CapabilityPlanSelectionV3) for item in bundle)
            or sum(item == selection for item in bundle) != 1
        ):
            raise TypeError("selections must contain the exact installed selection once")
        catalog_snapshot_digest = _required_digest(
            expected_catalog_snapshot_digest,
            "expected_catalog_snapshot_digest",
        )
        if type(route_authority) is not _PromptContextMaterialRoutePermit:
            raise TypeError("route_authority must be an engine-issued material route")
        with self._lock:
            if os.getpid() != self._pid:
                raise ActivatedSkillExposureError("material permit belongs to another process")
            if self._consumed:
                raise ActivatedSkillExposureError("material permit is already consumed")
            self._consumed = True

        self._assert_prompt_binding(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=catalog_snapshot_digest,
        )
        try:
            route_authority._consume(
                action=action,
                selection=selection,
                selections=bundle,
                expected_catalog_snapshot_digest=catalog_snapshot_digest,
            )
        except Exception:
            raise ActivatedSkillExposureError(
                "logical prompt material route rejected installed material"
            ) from None

        try:
            capability, _root_identity, content = _rederive_under_material_lock(
                request=self._request,
                activation_evidence=self._activation_evidence,
                read_content=True,
            )
        except ActivatedSkillExposureError:
            raise
        except SkillCasFilesystemError:
            raise ActivatedSkillExposureError(
                "skill CAS does not contain exact installed UTF-8 material"
            ) from None
        except Exception:
            raise ActivatedSkillExposureError(
                "activated skill authority could not be rederived"
            ) from None
        if content is None or (
            capability.material_identity != self._material_identity
            or capability.current_authorized_material != self._authorized_material
        ):
            raise ActivatedSkillExposureError(
                "activated skill material changed before prompt preparation"
            )
        try:
            decoded = content.decode("utf-8", errors="strict")
        except UnicodeError as exc:  # Defensive; authenticated CAS read already checked UTF-8.
            raise ActivatedSkillExposureError(
                "skill CAS does not contain exact installed UTF-8 material"
            ) from exc
        estimated_tokens = min(
            MAX_PREPARED_CONTENT_TOKENS,
            max(1, (capability.material_identity.content_bytes + 3) // 4),
        )
        return PreparedCapabilityContent(
            capability_id=capability.capability_id,
            source_digest=selection.presentation.source_digest,
            catalog_snapshot_digest=catalog_snapshot_digest,
            action_id=action.action_id,
            lease_id=action.lease_id or "",
            content=decoded,
            content_sha256=capability.material_identity.content_sha256,
            content_bytes=capability.material_identity.content_bytes,
            estimated_tokens=estimated_tokens,
        )

    def _assert_prompt_binding(
        self,
        *,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
        expected_catalog_snapshot_digest: str,
    ) -> None:
        planning_authority = selection.authority
        presentation = selection.presentation
        material = (
            planning_authority.material
            if isinstance(planning_authority, LoadPlanningAuthority)
            else None
        )
        rows = action.payload.get("capabilities")
        matching_rows = (
            tuple(
                row
                for row in rows
                if isinstance(row, Mapping)
                and row.get("capability_id") == presentation.capability_id
                and row.get("source_digest") == presentation.source_digest
            )
            if isinstance(rows, tuple)
            else ()
        )
        row = matching_rows[0] if len(matching_rows) == 1 else None
        row_material = row.get("material_identity") if isinstance(row, Mapping) else None
        material_matches = self._planning_material_matches(material)
        row_authorized_material = None if material is None else material.to_dict()
        if (
            action.kind != "PreparePromptContext"
            or action.catalog_snapshot_id != expected_catalog_snapshot_digest
            or action.lease_id is None
            or action.payload.get("execution_intent") not in {"activate", "experiment"}
            or not isinstance(row, Mapping)
            or not isinstance(row_material, Mapping)
            or presentation.capability_id != self._authorized_material.capability_id
            or presentation.kind != "skill"
            or presentation.actionability != "load"
            or not material_matches
            or selection.catalog_identity.identity_digest
            != self._authorized_material.catalog_identity_digest
            or row.get("authorized_material") != row_authorized_material
            or row.get("capability_id") != presentation.capability_id
            or row.get("capability_kind") != presentation.kind
            or row.get("catalog_identity") != selection.catalog_identity.to_dict()
            or row_material != self._material_identity.to_dict()
            or row.get("source_digest") != presentation.source_digest
        ):
            raise ActivatedSkillExposureError(
                "logical prompt action does not bind exact installed material"
            )

    def _planning_material_matches(self, material: AuthorizedMaterial | None) -> bool:
        """Accept the installed proof or its exact reviewed catalog realization."""

        if material == self._authorized_material:
            return True
        if not isinstance(material, AuthorizedMaterial) or material.origin != "catalog":
            return False
        descriptor = material.catalog_material_descriptor
        return bool(
            descriptor is not None
            and (
                material.capability_id,
                material.kind,
                material.catalog_identity_digest,
                material.material_identity_digest,
                descriptor.content_sha256,
                descriptor.content_bytes,
            )
            == (
                self._authorized_material.capability_id,
                self._authorized_material.kind,
                self._authorized_material.catalog_identity_digest,
                self._material_identity.identity_digest,
                self._material_identity.content_sha256,
                self._material_identity.content_bytes,
            )
        )

    def __repr__(self) -> str:
        return f"ActivatedSkillMaterialPermit(permit_digest={self._permit_digest!r})"

    def __copy__(self) -> ActivatedSkillMaterialPermit:
        raise TypeError("ActivatedSkillMaterialPermit cannot be copied")

    def __deepcopy__(self, _memo: object) -> ActivatedSkillMaterialPermit:
        raise TypeError("ActivatedSkillMaterialPermit cannot be copied")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("ActivatedSkillMaterialPermit cannot be serialized")


def prepare_activated_skill_exposure(
    *,
    request: ReleaseSkillInstallRequest,
    activation_evidence: ReleaseSkillActivationEvidence,
) -> ActivatedSkillExposurePreparation:
    """Re-derive active installed authority and issue one material permit."""

    if not isinstance(request, ReleaseSkillInstallRequest):
        raise TypeError("request must be a ReleaseSkillInstallRequest")
    if not isinstance(activation_evidence, ReleaseSkillActivationEvidence):
        raise TypeError("activation_evidence must be ReleaseSkillActivationEvidence")
    try:
        capability, root_identity, _content = _rederive_under_material_lock(
            request=request,
            activation_evidence=activation_evidence,
            read_content=False,
        )
        authorized = capability.current_authorized_material
        lineage = capability.installed_lineage
        if (
            not isinstance(authorized, AuthorizedMaterial)
            or authorized.origin != "installed"
            or lineage is None
            or authorized.installed_material_lineage != lineage
        ):
            raise ActivatedSkillExposureError(
                "activated skill lacks exact installed material authority"
            )
        permit_digest = _digest(
            {
                "activation_evidence_digest": activation_evidence.evidence_digest,
                "installed_lineage_digest": lineage.lineage_digest,
                "material_identity_digest": capability.material_identity.identity_digest,
                "schema": "ctx.activated-skill-material-permit-v1",
                "skill_cas_root_identity_digest": root_identity.digest,
            }
        )
        permit = ActivatedSkillMaterialPermit(
            request=request,
            activation_evidence=activation_evidence,
            authorized_material=authorized,
            material_identity=capability.material_identity,
            permit_digest=permit_digest,
            _token=_FACTORY_TOKEN,
        )
        preparation_digest = _preparation_digest(
            activation_evidence_digest=activation_evidence.evidence_digest,
            installed_lineage_digest=lineage.lineage_digest,
            material_identity_digest=capability.material_identity.identity_digest,
            skill_cas_root_identity_digest=root_identity.digest,
            permit_digest=permit_digest,
        )
        return ActivatedSkillExposurePreparation(
            activation_evidence_digest=activation_evidence.evidence_digest,
            installed_lineage_digest=lineage.lineage_digest,
            material_identity_digest=capability.material_identity.identity_digest,
            skill_cas_root_identity_digest=root_identity.digest,
            preparation_digest=preparation_digest,
            material_permit=permit,
        )
    except ActivatedSkillExposureError:
        raise
    except Exception:
        raise ActivatedSkillExposureError(
            "activated skill authority could not be rederived"
        ) from None


def _rederive_under_material_lock(
    *,
    request: ReleaseSkillInstallRequest,
    activation_evidence: ReleaseSkillActivationEvidence,
    read_content: bool,
) -> tuple[CapabilityStateV3, RootIdentity, bytes | None]:
    """Re-open journal authority and CAS under their shared material lock."""

    try:
        _require_existing_journal(request.journal_path)
    except ReleaseSkillActivationError:
        raise ActivatedSkillExposureError(
            "activated skill authority could not be rederived"
        ) from None
    store = SQLiteEngineStore.open_read_only(request.journal_path)
    engine = CtxEngine(
        store=store,
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )
    scope = _scope(request)
    snapshot = engine.snapshot(scope)
    capability = (
        None
        if snapshot.state is None
        else snapshot.state.capability(activation_evidence.capability_id)
    )
    if not isinstance(capability, CapabilityStateV3):
        raise ActivatedSkillExposureError("activated skill is absent from exact journal")
    root_identity = skill_cas_root_identity(request.skill_store_root)
    lock_target = Path(root_identity.canonical_root) / capability.material_identity.content_sha256
    with secure_file_lock(lock_target, timeout=30.0):
        active, verified_revision, verified_record_digest = _verify_active_journal_locked(
            request=request,
            activation_evidence=activation_evidence,
            engine=engine,
            store=store,
            stream_id=StreamId.from_scope(scope),
            root_identity=root_identity,
            locked_material_digest=capability.material_identity.content_sha256,
        )
        current_root = skill_cas_root_identity(Path(root_identity.canonical_root))
        if current_root != root_identity:
            raise ActivatedSkillExposureError("skill CAS root changed during preparation")
        with open_skill_cas_directory(root_identity) as directory:
            if read_content:
                content = directory.read_exact_utf8_bytes(
                    active.material_identity.content_sha256,
                    expected_sha256=active.material_identity.content_sha256,
                    expected_bytes=active.material_identity.content_bytes,
                    allowed_links=frozenset({1}),
                )
            else:
                inspection = directory.inspect_exact_utf8(
                    active.material_identity.content_sha256,
                    expected_sha256=active.material_identity.content_sha256,
                    expected_bytes=active.material_identity.content_bytes,
                    allowed_links=frozenset({1}),
                )
                if inspection.state != "exact" or inspection.identity is None:
                    raise ActivatedSkillExposureError(
                        "skill CAS does not contain exact installed UTF-8 material"
                    )
                content = None
            directory.revalidate_root()
        final = engine.snapshot(scope)
        final_capability = (
            None
            if final.state is None
            else final.state.capability(activation_evidence.capability_id)
        )
        if (
            final.revision != verified_revision
            or final.record_digest != verified_record_digest
            or final_capability != active
            or not isinstance(final_capability, CapabilityStateV3)
            or final_capability.activation != "active"
        ):
            raise ActivatedSkillExposureError(
                "activated skill changed before material exposure linearized"
            )
        return active, root_identity, content


def _verify_active_journal_locked(
    *,
    request: ReleaseSkillInstallRequest,
    activation_evidence: ReleaseSkillActivationEvidence,
    engine: CtxEngine,
    store: SQLiteEngineStore,
    stream_id: StreamId,
    root_identity: RootIdentity,
    locked_material_digest: str,
) -> tuple[CapabilityStateV3, int, str]:
    """Read-only proof of the exact settled activation at one journal head."""

    for _ in range(3):
        snapshot = engine.snapshot(_scope(request))
        records = tuple(store.records(stream_id))
        if (
            len(records) != snapshot.revision
            or not records
            or records[-1].record_digest != snapshot.record_digest
        ):
            continue
        state = snapshot.state
        if state is None:
            raise ActivatedSkillExposureError("activated skill is absent from exact journal")
        capability = state.capability(activation_evidence.capability_id)
        if not isinstance(capability, CapabilityStateV3):
            raise ActivatedSkillExposureError("activated skill is absent from exact journal")
        lineage = capability.installed_lineage
        if (
            capability.installation != "installed"
            or capability.activation != "active"
            or lineage is None
            or capability.material_identity.content_sha256 != locked_material_digest
            or state.host_descriptor_digest != _release_skill_host_descriptor_digest(request)
        ):
            raise ActivatedSkillExposureError(
                "activated skill is not current exact journal authority"
            )
        try:
            install_action = _exact_install_action(records, capability)
            _verify_install_target_binding(
                request=request,
                engine=engine,
                root_identity=root_identity,
                capability=capability,
                install_action=install_action,
            )
            activation_action = _exact_activation_action(records, capability)
            execution_binding = InstallExecutionBinding(
                driver_id=_ACTIVATION_VERIFIER_ID,
                driver_digest=_ACTIVATION_VERIFIER_DIGEST,
                host_identity_digest=request.host_identity_digest,
                target_identity_digest=root_identity.digest,
            )
            status = engine.activation_execution_status(activation_action)
            if (
                not status.claimed
                or not status.outcome_recorded
                or not status.settled
                or status.execution_binding_digest != execution_binding.binding_digest
                or status.observed_at is None
                or status.outcome_digest is None
            ):
                raise ActivatedSkillExposureError(
                    "active skill lacks exact settled activation authority"
                )
            receipt = _exact_activation_receipt(
                records=records,
                action=activation_action,
                observed_at=status.observed_at,
                outcome_digest=status.outcome_digest,
            )
        except ReleaseSkillActivationError as exc:
            raise ActivatedSkillExposureError(str(exc)) from exc
        if (
            activation_action.content_digest != activation_evidence.activation_action_content_digest
            or receipt.event_content_digest != activation_evidence.activation_receipt_content_digest
            or receipt.record_digest != activation_evidence.activation_record_digest
            or lineage.lineage_digest != activation_evidence.installed_lineage_digest
            or capability.material_identity.identity_digest
            != activation_evidence.material_identity_digest
            or root_identity.digest != activation_evidence.skill_cas_root_identity_digest
        ):
            raise ActivatedSkillExposureError(
                "activation evidence does not match exact journal authority"
            )
        if snapshot.record_digest is None:
            raise ActivatedSkillExposureError("activated skill journal has no exact head")
        return capability, snapshot.revision, snapshot.record_digest
    raise ActivatedSkillExposureError("activated skill journal remained contended")


def _preparation_digest(
    *,
    activation_evidence_digest: str,
    installed_lineage_digest: str,
    material_identity_digest: str,
    skill_cas_root_identity_digest: str,
    permit_digest: str,
) -> str:
    return _digest(
        {
            "activation_evidence_digest": activation_evidence_digest,
            "installed_lineage_digest": installed_lineage_digest,
            "material_identity_digest": material_identity_digest,
            "permit_digest": permit_digest,
            "schema": "ctx.activated-skill-exposure-preparation-v1",
            "skill_cas_root_identity_digest": skill_cas_root_identity_digest,
        }
    )


__all__ = [
    "ActivatedSkillExposureError",
    "ActivatedSkillExposurePreparation",
    "ActivatedSkillMaterialPermit",
    "prepare_activated_skill_exposure",
]
