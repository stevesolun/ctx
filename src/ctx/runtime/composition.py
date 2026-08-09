"""Production composition boundary for the host-neutral CTX engine.

This module owns construction and lifetime only. It does not observe a host,
choose consent, execute actions, call a provider, or change planner ordering.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import TYPE_CHECKING, NoReturn, SupportsIndex, cast

from ctx.core.install_policy_store import HeldInstallConsentPolicy, hold_current_install_policy
from ctx.core.resolve.engine_candidates import IndexedGraphCandidateSource
from ctx.engine.content import (
    CapabilityMaterialPort,
    ExposureAuthorizer,
    MaterialDescriptor,
    PreparedCapabilityContent,
)
from ctx.engine.benefit import BenefitSelectionResult, NetBenefitPolicy
from ctx.engine.benefit_audit_store import SQLiteBenefitAuditStore
from ctx.engine.engine import CtxEngine, EngineSnapshot
from ctx.engine.installation import (
    CapabilityInstallBundlePort,
    InstallAuthorizer,
    InstallExecutionBinding,
    InstallPlanDescriptor,
    InstallPlanningBundle,
    InteractiveInstallDecisionGuard,
    PreparedInstallPlan,
)
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import (
    CandidateAuthorityUnavailable,
    CandidateSource,
    CapabilityCandidate,
    CapabilitySelection,
)
from ctx.engine.planning_v3 import (
    AuthenticatedNetBenefitPlanner,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
)
from ctx.engine.protocol import EngineEvent, HostAction, ScopeRef, Transition
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION, INSTALLER_DIGEST
from ctx.engine.replay import DefaultReplayInputFactory, ObservationNormalizer
from ctx.engine.state import CommittedPlanV3
from ctx.engine.store import SQLiteEngineStore, StreamId
from ctx.runtime.planning_v3 import (
    AuthenticatedBenefitFactsPort,
    AuthenticatedReplayDecisionPlannerV3,
    CatalogLoadPlanningBundle,
)
from ctx.runtime.activation_execution import (
    ActivationExecutionReport,
    prepare_activation_execution,
)
from ctx.runtime.install_execution import (
    InstallDriverRegistry,
    InstallExecutionReport,
    prepare_install_execution,
)
from ctx.runtime.managed_query import (
    ManagedAdvanceResult,
    ManagedQueryError,
    PreparedManagedQuery,
    _create_managed_advance_result,
    _project_prepared_managed_query,
    _project_prepared_managed_query_snapshot,
)
from ctx.runtime.agent_file import AgentFileRuntimeConfig
from ctx.runtime.skill_cas import SkillCasRuntimeConfig

if TYPE_CHECKING:
    from ctx.runtime.managed_artifact_registry import (
        ManagedArtifactHandle,
        ManagedArtifactRegistry,
    )


# Kept as a package-level compatibility name while the public runtime surface
# moves atomically to authenticated schema-v3 planning.
DEFAULT_RUNTIME_PLANNER_VERSION = "ctx-authenticated-net-benefit-planner-v3"
_MALFORMED_AUTHORITY_OUTPUT = object()
_NONE_AUTHORITY_OUTPUT = object()
_RAISED_AUTHORITY_UNAVAILABLE_OUTPUT = object()
_ENGINE_COMPOSITION_FACTORY_TOKEN = object()
_MANAGED_SOURCE_FACTORY_TOKEN = object()


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _note_cleanup_failure(primary: BaseException, cleanup: BaseException) -> None:
    primary.add_note(f"CTX candidate-source cleanup also failed with {type(cleanup).__name__}")


class _PinnedMaterialPort:
    """Keep material lookups bound to the digest used by the catalog identity."""

    __slots__ = ("_cache", "_delegate", "material_snapshot_digest")

    def __init__(self, delegate: CapabilityMaterialPort) -> None:
        digest = getattr(delegate, "material_snapshot_digest", None)
        if (
            not callable(getattr(delegate, "describe", None))
            or not callable(getattr(delegate, "prepare", None))
            or not _is_sha256(digest)
        ):
            raise TypeError("material_port must implement the capability material contract")
        self._delegate = delegate
        self.material_snapshot_digest = cast(str, digest)
        self._cache: dict[tuple[str, str], object] = {}

    def _assert_current(self) -> None:
        if self._delegate.material_snapshot_digest != self.material_snapshot_digest:
            raise ValueError("material snapshot changed after composition")

    def _remember_output(self, key: tuple[str, str], classified: object) -> None:
        if key in self._cache and self._cache[key] != classified:
            raise ValueError("material descriptor changed within the pinned snapshot")
        self._cache[key] = classified

    def describe(self, capability_id: str, kind: str) -> MaterialDescriptor:
        key = (capability_id, kind)
        self._assert_current()
        try:
            descriptor = self._delegate.describe(capability_id, kind)
        except CandidateAuthorityUnavailable:
            self._assert_current()
            self._remember_output(key, _RAISED_AUTHORITY_UNAVAILABLE_OUTPUT)
            raise
        self._assert_current()
        classified: object = (
            descriptor
            if isinstance(descriptor, MaterialDescriptor)
            else (_NONE_AUTHORITY_OUTPUT if descriptor is None else _MALFORMED_AUTHORITY_OUTPUT)
        )
        self._remember_output(key, classified)
        if (
            not isinstance(descriptor, MaterialDescriptor)
            or descriptor.capability_id != capability_id
            or descriptor.kind != kind
            or descriptor.provenance_digest != self.material_snapshot_digest
        ):
            raise CandidateAuthorityUnavailable(
                "material descriptor does not match the pinned catalog"
            )
        return descriptor

    def prepare(
        self,
        action: HostAction,
        selection: CapabilitySelection,
        *,
        expected_catalog_snapshot_digest: str,
        authority: ExposureAuthorizer | None = None,
    ) -> PreparedCapabilityContent:
        self._assert_current()
        return self._delegate.prepare(
            action,
            selection,
            expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
            authority=authority,
        )


class _CatalogMaterialAuthority:
    """Join a pinned retrieval presentation to its exact pinned load descriptor."""

    __slots__ = (
        "_catalog_namespace_digest",
        "_catalog_snapshot_digest",
        "_material_port",
        "material_snapshot_digest",
    )

    def __init__(
        self,
        *,
        material_port: _PinnedMaterialPort,
        catalog_namespace_digest: str,
        catalog_snapshot_digest: str,
    ) -> None:
        self._material_port = material_port
        self._catalog_namespace_digest = catalog_namespace_digest
        self._catalog_snapshot_digest = catalog_snapshot_digest
        self.material_snapshot_digest = material_port.material_snapshot_digest

    def load_bundle(
        self,
        presentation: CapabilityCandidate,
    ) -> CatalogLoadPlanningBundle:
        descriptor = self._material_port.describe(
            presentation.capability_id,
            presentation.kind,
        )
        try:
            return CatalogLoadPlanningBundle(
                presentation=presentation,
                catalog_identity=CatalogCapabilityIdentity.create(
                    capability_id=presentation.capability_id,
                    kind=presentation.kind,
                    catalog_namespace_digest=self._catalog_namespace_digest,
                ),
                descriptor=descriptor,
                catalog_snapshot_digest=self._catalog_snapshot_digest,
                material_snapshot_digest=self.material_snapshot_digest,
            )
        except (TypeError, ValueError) as exc:
            raise CandidateAuthorityUnavailable("catalog load authority is invalid") from exc


class _PinnedInstallBundlePort:
    """Freeze complete install authority while exposing only metadata to retrieval."""

    __slots__ = ("_cache", "_delegate", "installation_snapshot_digest")

    def __init__(self, delegate: CapabilityInstallBundlePort) -> None:
        digest = getattr(delegate, "installation_snapshot_digest", None)
        if not callable(getattr(delegate, "describe_bundle", None)) or not _is_sha256(digest):
            raise TypeError("install_bundle_port must implement the install bundle contract")
        self._delegate = delegate
        self.installation_snapshot_digest = cast(str, digest)
        self._cache: dict[tuple[str, str], object] = {}

    def _assert_current(self) -> None:
        if self._delegate.installation_snapshot_digest != self.installation_snapshot_digest:
            raise ValueError("installation snapshot changed after composition")

    def _remember_output(self, key: tuple[str, str], classified: object) -> None:
        if key in self._cache and self._cache[key] != classified:
            raise ValueError("install bundle changed within the pinned snapshot")
        self._cache[key] = classified

    def describe_bundle(
        self,
        capability_id: str,
        kind: str,
    ) -> InstallPlanningBundle | None:
        key = (capability_id, kind)
        self._assert_current()
        try:
            bundle = self._delegate.describe_bundle(capability_id, kind)
        except CandidateAuthorityUnavailable:
            self._assert_current()
            self._remember_output(key, _RAISED_AUTHORITY_UNAVAILABLE_OUTPUT)
            raise
        self._assert_current()
        classified: object = (
            bundle
            if bundle is None or isinstance(bundle, InstallPlanningBundle)
            else _MALFORMED_AUTHORITY_OUTPUT
        )
        self._remember_output(key, classified)
        if bundle is not None and (
            not isinstance(bundle, InstallPlanningBundle)
            or bundle.descriptor.capability_id != capability_id
            or bundle.descriptor.kind != kind
            or bundle.descriptor.provenance_digest != self.installation_snapshot_digest
            or bundle.result_material.capability_id != capability_id
            or bundle.result_material.kind != kind
        ):
            raise CandidateAuthorityUnavailable("install bundle does not match the pinned catalog")
        return bundle

    def describe(self, capability_id: str, kind: str) -> InstallPlanDescriptor | None:
        """Descriptor-only view used by the retrieval adapter."""

        bundle = self.describe_bundle(capability_id, kind)
        return None if bundle is None else bundle.descriptor

    def prepare(
        self,
        _action: HostAction,
        _selection: CapabilitySelection,
        _descriptor: InstallPlanDescriptor,
        *,
        expected_catalog_snapshot_digest: str,
        expected_policy_digest: str,
        authority: InstallAuthorizer | None = None,
    ) -> PreparedInstallPlan:
        del expected_catalog_snapshot_digest, expected_policy_digest, authority
        raise RuntimeError("composition has no physical installation actuator")


class EngineComposition:
    """One engine and its pinned catalog resources.

    Construct instances with :func:`open_engine_composition` and close them
    explicitly or with ``with``. The journal store opens connections per
    operation; the pinned graph source is the resource owned by this handle.
    """

    __slots__ = (
        "_closed",
        "_engine",
        "_agent_file_runtime",
        "_install_driver_registry",
        "_lock",
        "_owner_pid",
        "_source",
        "_skill_cas_runtime",
        "catalog_snapshot_digest",
        "planner_version",
    )
    _closed: bool
    _engine: CtxEngine
    _agent_file_runtime: AgentFileRuntimeConfig | None
    _install_driver_registry: InstallDriverRegistry | None
    _lock: RLock
    _owner_pid: int
    _source: IndexedGraphCandidateSource
    _skill_cas_runtime: SkillCasRuntimeConfig | None
    catalog_snapshot_digest: str
    planner_version: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("engine compositions are opened by trusted production composition")

    @classmethod
    def _create(
        cls,
        *,
        factory_token: object,
        engine: CtxEngine,
        source: IndexedGraphCandidateSource,
        planner: AuthenticatedReplayDecisionPlannerV3,
        install_driver_registry: InstallDriverRegistry | None = None,
        skill_cas_runtime: SkillCasRuntimeConfig | None = None,
        agent_file_runtime: AgentFileRuntimeConfig | None = None,
    ) -> EngineComposition:
        if factory_token is not _ENGINE_COMPOSITION_FACTORY_TOKEN:
            raise TypeError("engine compositions are opened by trusted production composition")
        if type(engine) is not CtxEngine:
            raise TypeError("engine composition requires an exact CtxEngine")
        if type(source) is not IndexedGraphCandidateSource:
            raise TypeError("engine composition requires an exact indexed graph source")
        if type(planner) is not AuthenticatedReplayDecisionPlannerV3:
            raise TypeError("engine composition requires an exact authenticated planner")
        if source.catalog_snapshot_digest != planner.catalog_retrieval_snapshot_digest:
            raise ValueError("composition source and planner catalog snapshots do not match")
        instance = object.__new__(cls)
        object.__setattr__(instance, "_engine", engine)
        object.__setattr__(instance, "_install_driver_registry", install_driver_registry)
        object.__setattr__(instance, "_skill_cas_runtime", skill_cas_runtime)
        object.__setattr__(instance, "_agent_file_runtime", agent_file_runtime)
        object.__setattr__(instance, "catalog_snapshot_digest", planner.catalog_snapshot_digest)
        object.__setattr__(instance, "planner_version", planner.planner_version)
        object.__setattr__(instance, "_source", source)
        object.__setattr__(instance, "_closed", False)
        object.__setattr__(instance, "_lock", RLock())
        object.__setattr__(instance, "_owner_pid", os.getpid())
        return instance

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise AttributeError("engine composition authority is immutable")

    def __delattr__(self, _name: str) -> NoReturn:
        raise AttributeError("engine composition authority is immutable")

    def __copy__(self) -> NoReturn:
        raise TypeError("engine composition authority cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("engine composition authority cannot be copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("engine composition authority cannot be serialized")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("engine composition authority cannot be serialized")

    @property
    def closed(self) -> bool:
        """Whether the owned pinned candidate source has been closed."""

        self._assert_owner_process()
        with self._lock:
            return self._closed

    def _assert_owner_process(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("engine composition cannot be used from a forked process")

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("engine composition is closed")

    def process(self, event: EngineEvent) -> Transition:
        """Process one event while holding the composition lifetime lease."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            return self._engine.process(event)

    def snapshot(self, scope: ScopeRef) -> EngineSnapshot:
        """Return the authoritative stream snapshot while the composition is open."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            return self._engine.snapshot(scope)

    def prepare_managed_query(
        self,
        *,
        session_started: EngineEvent,
        intent_observed: EngineEvent,
    ) -> PreparedManagedQuery:
        """Commit one safe cross-type plan under the owned authority lifetime."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            if type(session_started) is not EngineEvent or type(intent_observed) is not EngineEvent:
                raise TypeError("managed planning events must be exact EngineEvent values")
            if session_started.kind != "SessionStarted" or session_started.expected_revision != 0:
                raise ManagedQueryError(
                    "managed query must begin with SessionStarted revision zero"
                )
            if intent_observed.kind != "IntentObserved" or intent_observed.expected_revision != 1:
                raise ManagedQueryError("managed query must plan with IntentObserved revision one")
            if session_started.scope != intent_observed.scope:
                raise ManagedQueryError("managed query planning events must share one exact scope")
            if session_started.correlation_id != intent_observed.correlation_id:
                raise ManagedQueryError(
                    "managed query planning events must share one plan identity"
                )
            for field_name in (
                "catalog_snapshot_digest",
                "planner_version",
                "semantic_index_digest",
                "work_signature",
            ):
                if getattr(session_started, field_name) != getattr(intent_observed, field_name):
                    raise ManagedQueryError(
                        f"managed query planning events disagree on {field_name}"
                    )
            initial = self._engine.snapshot(session_started.scope)
            if initial.revision != 0 or initial.state is not None:
                raise ManagedQueryError("managed query scope already contains authoritative state")
            started = self._engine.process(session_started)
            if started.scope != session_started.scope or started.to_revision != 1:
                raise ManagedQueryError("managed query session did not commit exactly revision one")
            planned = self._engine.process(intent_observed)
            snapshot = self._engine.snapshot(intent_observed.scope)
            state = snapshot.state
            committed = None if state is None else state.committed_plan
            if (
                planned.scope != intent_observed.scope
                or planned.to_revision != 2
                or snapshot.revision != 2
                or snapshot.record_digest is None
                or not isinstance(committed, CommittedPlanV3)
            ):
                raise ManagedQueryError("managed query did not commit one exact schema-v3 plan")
            if (
                committed.plan_id != intent_observed.correlation_id
                or committed.catalog_snapshot_id != intent_observed.catalog_snapshot_digest
            ):
                raise ManagedQueryError("committed managed plan lost its event identity")
            return _project_prepared_managed_query(
                committed=committed,
                journal_revision=snapshot.revision,
                journal_record_digest=snapshot.record_digest,
            )

    def reopen_managed_query(
        self,
        scope: ScopeRef,
        *,
        expected_plan_id: str | None = None,
    ) -> PreparedManagedQuery:
        """Project the latest committed managed plan without invoking its planner."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            if type(scope) is not ScopeRef:
                raise TypeError("managed query scope must be an exact ScopeRef")
            return self._project_managed_snapshot(
                self._engine.snapshot(scope),
                expected_plan_id=expected_plan_id,
            )

    def advance_managed_query(
        self,
        *,
        session_started: EngineEvent,
        planning_observed: EngineEvent,
    ) -> ManagedAdvanceResult:
        """Commit or replay one exact managed plan on a stable session stream."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            self._validate_managed_advance_events(session_started, planning_observed)
            initial = self._engine.snapshot(session_started.scope)
            if initial.revision <= 1 and planning_observed.expected_revision != 1:
                raise ManagedQueryError(
                    "an unplanned managed session requires IntentObserved revision one"
                )

            started = self._engine.process(session_started)
            if (
                started.event_id != session_started.event_id
                or started.scope != session_started.scope
                or started.from_revision != 0
                or started.to_revision != 1
                or StreamId.from_scope(started.scope) != initial.stream_id
            ):
                raise ManagedQueryError("managed query lost its exact session-start identity")

            current = self._engine.snapshot(planning_observed.scope)
            state = current.state
            if current.revision < 1 or state is None:
                raise ManagedQueryError("managed query session start is not authoritative")

            is_new_replacement = (
                current.revision >= 2 and planning_observed.expected_revision == current.revision
            )
            if is_new_replacement:
                if type(state.committed_plan) is not CommittedPlanV3:
                    raise ManagedQueryError(
                        "managed replacement requires an exact committed schema-v3 plan"
                    )
                if state.committed_plan.plan_id == planning_observed.correlation_id:
                    raise ManagedQueryError(
                        "managed replacement plan identity is already committed; "
                        "an exact retry must reuse its original revision"
                    )
                if state.session_status == "ended":
                    raise ManagedQueryError("managed query session has ended")
                if state.pending_effects or state.pending_consents:
                    raise ManagedQueryError(
                        "managed query must settle pending authority before replacement"
                    )

            planned = self._engine.process(planning_observed)
            if (
                planned.event_id != planning_observed.event_id
                or planned.scope != planning_observed.scope
                or planned.from_revision != planning_observed.expected_revision
                or planned.to_revision != planning_observed.expected_revision + 1
                or StreamId.from_scope(planned.scope) != current.stream_id
            ):
                raise ManagedQueryError("managed query lost its exact planning-event identity")
            return _create_managed_advance_result(
                prepared=self._project_managed_snapshot(
                    self._engine.snapshot(planning_observed.scope),
                    expected_plan_id=planning_observed.correlation_id,
                ),
                transition=planned,
            )

    def _project_managed_snapshot(
        self,
        snapshot: EngineSnapshot,
        *,
        expected_plan_id: str | None,
    ) -> PreparedManagedQuery:
        prepared = _project_prepared_managed_query_snapshot(
            snapshot=snapshot,
            expected_plan_id=expected_plan_id,
        )
        if prepared.planning_environment_digest != self.catalog_snapshot_digest:
            raise ManagedQueryError(
                "committed managed plan does not match this planning environment"
            )
        return prepared

    def _validate_managed_advance_events(
        self,
        session_started: EngineEvent,
        planning_observed: EngineEvent,
    ) -> None:
        if type(session_started) is not EngineEvent or type(planning_observed) is not EngineEvent:
            raise TypeError("managed planning events must be exact EngineEvent values")
        if session_started.kind != "SessionStarted" or session_started.expected_revision != 0:
            raise ManagedQueryError("managed advance requires SessionStarted revision zero")
        if planning_observed.expected_revision < 1 or (
            planning_observed.kind
            != (
                "IntentObserved"
                if planning_observed.expected_revision == 1
                else "DevelopmentObserved"
            )
        ):
            raise ManagedQueryError(
                "managed planning must use IntentObserved at revision one and "
                "DevelopmentObserved afterward"
            )
        if StreamId.from_scope(session_started.scope) != StreamId.from_scope(
            planning_observed.scope
        ):
            raise ManagedQueryError("managed planning events must share one stable stream")
        for event in (session_started, planning_observed):
            if event.catalog_snapshot_digest != self.catalog_snapshot_digest:
                raise ManagedQueryError(
                    "managed planning event does not match the planning environment"
                )
            if event.planner_version != self.planner_version:
                raise ManagedQueryError("managed planning event does not match the planner")
        for field_name in (
            "host_descriptor_digest",
            "semantic_model_digest",
            "semantic_index_digest",
        ):
            if getattr(session_started, field_name) != getattr(planning_observed, field_name):
                raise ManagedQueryError(f"managed planning events disagree on {field_name}")
        if planning_observed.expected_revision == 1 and (
            session_started.correlation_id != planning_observed.correlation_id
            or session_started.work_signature != planning_observed.work_signature
        ):
            raise ManagedQueryError(
                "initial managed planning must preserve its plan and work identity"
            )

    def authorize_exposure(
        self,
        action: HostAction,
        selection: CapabilitySelection | CapabilityPlanSelectionV3,
        *,
        expected_catalog_snapshot_digest: str,
    ) -> None:
        """Delegate exact exposure authorization only while the catalog is owned."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            self._engine.authorize_exposure(
                action,
                selection,
                expected_catalog_snapshot_digest=expected_catalog_snapshot_digest,
            )

    def execute_install(
        self,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
        *,
        expected_policy_digest: str,
    ) -> InstallExecutionReport:
        """Execute one trusted built-in install driver under composition ownership."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            registry = self._install_driver_registry
            if registry is None:
                raise RuntimeError("composition has no physical installation actuator")
            authority = selection.authority
            if not isinstance(authority, InstallPlanningAuthority):
                raise TypeError("selection has no install planning authority")
            handle = prepare_install_execution(
                engine=self._engine,
                action=action,
                selection=selection,
                descriptor=authority.descriptor,
                expected_catalog_snapshot_digest=self.catalog_snapshot_digest,
                expected_policy_digest=expected_policy_digest,
                registry=registry,
            )
            return handle.execute()

    def resolve_install_execution_binding(
        self,
        action: HostAction,
        selection: CapabilityPlanSelectionV3,
    ) -> InstallExecutionBinding:
        """Resolve an install binding without connecting to or invoking its driver."""

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            if type(action) is not HostAction or type(selection) is not CapabilityPlanSelectionV3:
                raise TypeError("install binding lookup requires exact action and selection values")
            authority = selection.authority
            if not isinstance(authority, InstallPlanningAuthority):
                raise TypeError("install binding lookup requires install planning authority")
            registry = self._install_driver_registry
            if registry is None:
                raise RuntimeError("composition has no physical installation actuator")
            presentation = selection.presentation
            if (
                action.kind != "InstallCapability"
                or action.entity_id != presentation.capability_id
                or action.source_digest != presentation.source_digest
                or action.catalog_snapshot_id != self.catalog_snapshot_digest
                or action.payload.get("catalog_identity") != selection.catalog_identity.to_dict()
                or action.payload.get("install_plan_descriptor") != authority.descriptor.to_dict()
                or action.payload.get("result_material") != authority.result_material.to_dict()
            ):
                raise RuntimeError("install action does not match its exact planned selection")
            registration = registry.resolve(action, authority.descriptor)
            current_registry = _install_driver_registry(
                skill_cas_runtime=self._skill_cas_runtime,
                agent_file_runtime=self._agent_file_runtime,
            )
            if current_registry is None:
                raise RuntimeError("composition physical installation actuator disappeared")
            current = current_registry.resolve(action, authority.descriptor)
            if current.binding != registration.binding:
                raise RuntimeError("composition physical installation actuator identity changed")
            return registration.binding

    def execute_activation(
        self,
        action: HostAction,
        *,
        execution_binding: InstallExecutionBinding,
        expected_host_descriptor_digest: str,
        verification_digest: str,
    ) -> ActivationExecutionReport:
        """Sequence one applied activation under composition ownership.

        Encapsulates the engine claim, one-shot verified outcome, and idempotent
        ``ActionApplied`` receipt for an installed-inactive capability so callers
        never reach the private ``_engine`` slot or engine-private outcome
        methods.  The caller supplies the independent physical ``verification
        digest`` (a generic activation driver registry does not exist yet); only
        the applied outcome is handled here, and re-entry after a settled
        activation is idempotent.
        """

        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            handle = prepare_activation_execution(
                engine=self._engine,
                action=action,
                execution_binding=execution_binding,
                expected_host_descriptor_digest=expected_host_descriptor_digest,
                verification_digest=verification_digest,
            )
            return handle.execute()

    def close(self) -> None:
        """Release the pinned graph snapshot; repeated closes are harmless."""

        self._assert_owner_process()
        with self._lock:
            if self._closed:
                return
            self._source.close()
            object.__setattr__(self, "_closed", True)

    def __enter__(self) -> EngineComposition:
        self._assert_owner_process()
        with self._lock:
            self._assert_open()
            return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:
            if _exc is None:
                raise
            _note_cleanup_failure(_exc, cleanup_error)


class _PlanningDigestAuditStore:
    """Side-effect-free audit seam used only to recompute planner identity."""

    def store(self, _result: object) -> NoReturn:
        raise RuntimeError("planning digest preflight cannot store audit results")


class _LazySQLiteBenefitAuditStore:
    """Delay durable audit creation until a validated decision is persisted."""

    __slots__ = ("_delegate", "_lock", "_path")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._delegate: SQLiteBenefitAuditStore | None = None
        self._lock = RLock()

    def store(self, result: BenefitSelectionResult) -> str:
        with self._lock:
            delegate = self._delegate
            if delegate is None:
                delegate = SQLiteBenefitAuditStore(self._path)
                self._delegate = delegate
            return delegate.store(result)


def _install_driver_registry(
    *,
    skill_cas_runtime: SkillCasRuntimeConfig | None,
    agent_file_runtime: AgentFileRuntimeConfig | None,
) -> InstallDriverRegistry | None:
    registrations = []
    if skill_cas_runtime is not None:
        registrations.append(skill_cas_runtime.registration(driver_digest=INSTALLER_DIGEST))
    if agent_file_runtime is not None:
        registrations.append(agent_file_runtime.registration(driver_digest=INSTALLER_DIGEST))
    return InstallDriverRegistry(registrations) if registrations else None


def _open_composition_from_indexed_source(
    *,
    source: IndexedGraphCandidateSource,
    journal_path: Path,
    observation_normalizer: ObservationNormalizer,
    benefit_facts_port: AuthenticatedBenefitFactsPort,
    net_benefit_policy: NetBenefitPolicy,
    catalog_namespace_digest: str,
    benefit_audit_path: Path,
    pinned_material_port: _PinnedMaterialPort | None,
    pinned_install_bundle_port: _PinnedInstallBundlePort | None,
    planner_version: str,
    policy_store_root: Path | None,
    interactive_install_decision_guard: InteractiveInstallDecisionGuard | None,
    trusted_utc_now: Callable[[], datetime] | None,
    install_driver_registry: InstallDriverRegistry | None,
    skill_cas_runtime: SkillCasRuntimeConfig | None,
    agent_file_runtime: AgentFileRuntimeConfig | None,
    expected_catalog_retrieval_digest: str | None = None,
    expected_planning_environment_digest: str | None = None,
) -> EngineComposition:
    """Assemble the single production engine around one already-owned source."""

    try:
        if (
            expected_catalog_retrieval_digest is not None
            and source.catalog_snapshot_digest != expected_catalog_retrieval_digest
        ):
            raise ValueError("managed catalog retrieval digest does not match the handle")
        catalog_material_authority = (
            None
            if pinned_material_port is None
            else _CatalogMaterialAuthority(
                material_port=pinned_material_port,
                catalog_namespace_digest=catalog_namespace_digest,
                catalog_snapshot_digest=source.catalog_snapshot_digest,
            )
        )

        def decision_planner(
            audit_store: object,
        ) -> AuthenticatedReplayDecisionPlannerV3:
            return AuthenticatedReplayDecisionPlannerV3(
                planner=AuthenticatedNetBenefitPlanner(
                    policy=net_benefit_policy,
                    audit_store=audit_store,  # type: ignore[arg-type]
                ),
                source=cast(CandidateSource, source),
                benefit_facts_port=benefit_facts_port,
                material_port=catalog_material_authority,
                install_bundle_port=pinned_install_bundle_port,
                planner_version=planner_version,
                catalog_namespace_digest=catalog_namespace_digest,
            )

        if expected_planning_environment_digest is not None:
            preflight = decision_planner(_PlanningDigestAuditStore())
            if preflight.catalog_snapshot_digest != expected_planning_environment_digest:
                raise ValueError("managed planning environment does not match the handle")

        audit_store = (
            _LazySQLiteBenefitAuditStore(benefit_audit_path)
            if expected_planning_environment_digest is not None
            else SQLiteBenefitAuditStore(benefit_audit_path)
        )
        planner = decision_planner(audit_store)
        if (
            expected_planning_environment_digest is not None
            and planner.catalog_snapshot_digest != expected_planning_environment_digest
        ):
            raise ValueError("managed planning environment changed during composition")
        replay_factory = DefaultReplayInputFactory(
            observation_normalizer=observation_normalizer,
            decision_planner=planner,
            reducer_version=INSTALLATION_REDUCER_VERSION,
        )

        def current_policy_guard(
            expected_policy_digest: str,
        ) -> AbstractContextManager[HeldInstallConsentPolicy]:
            return hold_current_install_policy(
                expected_policy_digest,
                root=policy_store_root,
            )

        install_descriptor_loader = None
        if pinned_install_bundle_port is not None:

            def load_install_descriptor(
                capability_id: str,
                kind: str,
            ) -> InstallPlanDescriptor | None:
                bundle = pinned_install_bundle_port.describe_bundle(capability_id, kind)
                return None if bundle is None else bundle.descriptor

            install_descriptor_loader = load_install_descriptor

        engine = CtxEngine(
            store=SQLiteEngineStore(journal_path),
            replay_factory=replay_factory,
            install_policy_guard=current_policy_guard,
            interactive_install_decision_guard=interactive_install_decision_guard,
            install_descriptor_loader=install_descriptor_loader,
            trusted_utc_now=trusted_utc_now,
        )
        return EngineComposition._create(
            factory_token=_ENGINE_COMPOSITION_FACTORY_TOKEN,
            engine=engine,
            source=source,
            planner=planner,
            install_driver_registry=install_driver_registry,
            skill_cas_runtime=skill_cas_runtime,
            agent_file_runtime=agent_file_runtime,
        )
    except BaseException as error:
        try:
            source.close()
        except BaseException as cleanup_error:
            _note_cleanup_failure(error, cleanup_error)
        raise


def open_engine_composition(
    *,
    graph_artifact_path: Path,
    graph_artifact_sha256: str,
    journal_path: Path,
    observation_normalizer: ObservationNormalizer,
    benefit_facts_port: AuthenticatedBenefitFactsPort,
    net_benefit_policy: NetBenefitPolicy,
    catalog_namespace_digest: str,
    benefit_audit_path: Path,
    material_port: CapabilityMaterialPort | None = None,
    install_bundle_port: CapabilityInstallBundlePort | None = None,
    planner_version: str = DEFAULT_RUNTIME_PLANNER_VERSION,
    policy_store_root: Path | None = None,
    interactive_install_decision_guard: InteractiveInstallDecisionGuard | None = None,
    trusted_utc_now: Callable[[], datetime] | None = None,
    skill_cas_runtime: SkillCasRuntimeConfig | None = None,
    agent_file_runtime: AgentFileRuntimeConfig | None = None,
) -> EngineComposition:
    """Build one schema-v3 engine over authenticated, explicitly supplied facts.

    The source is opened before the journal is created, so an invalid graph or
    port cannot create authoritative engine state. Any later construction
    failure closes the source before propagating the original error.
    """

    if not isinstance(graph_artifact_path, Path):
        raise TypeError("graph_artifact_path must be a Path")
    if not isinstance(journal_path, Path):
        raise TypeError("journal_path must be a Path")
    if not isinstance(benefit_audit_path, Path):
        raise TypeError("benefit_audit_path must be a Path")
    if not callable(observation_normalizer):
        raise TypeError("observation_normalizer must be callable")
    if policy_store_root is not None and not isinstance(policy_store_root, Path):
        raise TypeError("policy_store_root must be a Path or None")
    if interactive_install_decision_guard is not None and not callable(
        interactive_install_decision_guard
    ):
        raise TypeError("interactive_install_decision_guard must be callable or None")
    if trusted_utc_now is not None and not callable(trusted_utc_now):
        raise TypeError("trusted_utc_now must be callable or None")
    if skill_cas_runtime is not None and not isinstance(skill_cas_runtime, SkillCasRuntimeConfig):
        raise TypeError("skill_cas_runtime must be a SkillCasRuntimeConfig or None")
    if agent_file_runtime is not None and not isinstance(
        agent_file_runtime,
        AgentFileRuntimeConfig,
    ):
        raise TypeError("agent_file_runtime must be an AgentFileRuntimeConfig or None")

    # Validate and pin physical-driver configuration before opening catalog,
    # audit, or journal resources.  An invalid/replaced install target must not
    # leave authoritative engine state behind as a construction side effect.
    install_driver_registry = _install_driver_registry(
        skill_cas_runtime=skill_cas_runtime,
        agent_file_runtime=agent_file_runtime,
    )
    pinned_material_port = None if material_port is None else _PinnedMaterialPort(material_port)
    pinned_install_bundle_port = (
        None if install_bundle_port is None else _PinnedInstallBundlePort(install_bundle_port)
    )
    source = IndexedGraphCandidateSource(
        graph_artifact_path,
        graph_artifact_sha256,
        install_plan_port=pinned_install_bundle_port,
        material_port=pinned_material_port,
    )
    return _open_composition_from_indexed_source(
        source=source,
        journal_path=journal_path,
        observation_normalizer=observation_normalizer,
        benefit_facts_port=benefit_facts_port,
        net_benefit_policy=net_benefit_policy,
        catalog_namespace_digest=catalog_namespace_digest,
        benefit_audit_path=benefit_audit_path,
        pinned_material_port=pinned_material_port,
        pinned_install_bundle_port=pinned_install_bundle_port,
        planner_version=planner_version,
        policy_store_root=policy_store_root,
        interactive_install_decision_guard=interactive_install_decision_guard,
        trusted_utc_now=trusted_utc_now,
        install_driver_registry=install_driver_registry,
        skill_cas_runtime=skill_cas_runtime,
        agent_file_runtime=agent_file_runtime,
    )


def open_managed_engine_composition(
    *,
    registry: ManagedArtifactRegistry,
    artifact: ManagedArtifactHandle,
    journal_path: Path,
    benefit_audit_path: Path,
    benefit_facts_port: AuthenticatedBenefitFactsPort,
    net_benefit_policy: NetBenefitPolicy,
    material_port: CapabilityMaterialPort,
    install_bundle_port: CapabilityInstallBundlePort,
    policy_store_root: Path | None = None,
    interactive_install_decision_guard: InteractiveInstallDecisionGuard | None = None,
    trusted_utc_now: Callable[[], datetime] | None = None,
    skill_cas_runtime: SkillCasRuntimeConfig | None = None,
    agent_file_runtime: AgentFileRuntimeConfig | None = None,
) -> EngineComposition:
    """Open one engine from a registry-issued, path-free planning handle.

    The registry owns durable content-addressed inputs.  The returned exact
    :class:`EngineComposition` owns the authenticated graph snapshot copied
    from them and retains every pinned authority for its complete lifetime.
    """

    from ctx.runtime.managed_artifact_registry import (
        ManagedArtifactHandle,
        ManagedArtifactRegistry,
    )

    if type(registry) is not ManagedArtifactRegistry:
        raise TypeError("registry must be an exact ManagedArtifactRegistry")
    if type(artifact) is not ManagedArtifactHandle:
        raise TypeError("artifact must be an exact ManagedArtifactHandle")
    if not isinstance(journal_path, Path):
        raise TypeError("journal_path must be a Path")
    if not isinstance(benefit_audit_path, Path):
        raise TypeError("benefit_audit_path must be a Path")
    if policy_store_root is not None and not isinstance(policy_store_root, Path):
        raise TypeError("policy_store_root must be a Path or None")
    if interactive_install_decision_guard is not None and not callable(
        interactive_install_decision_guard
    ):
        raise TypeError("interactive_install_decision_guard must be callable or None")
    if trusted_utc_now is not None and not callable(trusted_utc_now):
        raise TypeError("trusted_utc_now must be callable or None")
    if skill_cas_runtime is not None and not isinstance(skill_cas_runtime, SkillCasRuntimeConfig):
        raise TypeError("skill_cas_runtime must be a SkillCasRuntimeConfig or None")
    if agent_file_runtime is not None and not isinstance(
        agent_file_runtime,
        AgentFileRuntimeConfig,
    ):
        raise TypeError("agent_file_runtime must be an AgentFileRuntimeConfig or None")
    if not callable(getattr(benefit_facts_port, "benefit_candidate", None)) or not _is_sha256(
        getattr(benefit_facts_port, "benefit_facts_snapshot_digest", None)
    ):
        raise TypeError("benefit_facts_port must implement the authenticated facts contract")
    if not isinstance(net_benefit_policy, NetBenefitPolicy):
        raise TypeError("net_benefit_policy must be a NetBenefitPolicy")

    pinned_material_port = _PinnedMaterialPort(material_port)
    pinned_install_bundle_port = _PinnedInstallBundlePort(install_bundle_port)
    if benefit_facts_port.benefit_facts_snapshot_digest != artifact.benefit_facts_snapshot_digest:
        raise ValueError("managed benefit facts snapshot does not match the handle")
    if net_benefit_policy.policy_digest != artifact.benefit_policy_snapshot_digest:
        raise ValueError("managed benefit policy snapshot does not match the handle")
    if pinned_material_port.material_snapshot_digest != artifact.material_snapshot_digest:
        raise ValueError("managed material snapshot does not match the handle")
    if (
        pinned_install_bundle_port.installation_snapshot_digest
        != artifact.installation_snapshot_digest
    ):
        raise ValueError("managed installation snapshot does not match the handle")

    # Physical driver validation remains ahead of catalog and output-store
    # construction, matching the legacy production boundary.
    install_driver_registry = _install_driver_registry(
        skill_cas_runtime=skill_cas_runtime,
        agent_file_runtime=agent_file_runtime,
    )
    source = registry._open_indexed_source_for_composition(
        artifact,
        factory_token=_MANAGED_SOURCE_FACTORY_TOKEN,
        material_port=pinned_material_port,
        install_plan_port=pinned_install_bundle_port,
    )
    return _open_composition_from_indexed_source(
        source=source,
        journal_path=journal_path,
        observation_normalizer=artifact,
        benefit_facts_port=benefit_facts_port,
        net_benefit_policy=net_benefit_policy,
        catalog_namespace_digest=artifact.catalog_namespace_digest,
        benefit_audit_path=benefit_audit_path,
        pinned_material_port=pinned_material_port,
        pinned_install_bundle_port=pinned_install_bundle_port,
        planner_version=artifact.planning_schema_version,
        policy_store_root=policy_store_root,
        interactive_install_decision_guard=interactive_install_decision_guard,
        trusted_utc_now=trusted_utc_now,
        install_driver_registry=install_driver_registry,
        skill_cas_runtime=skill_cas_runtime,
        agent_file_runtime=agent_file_runtime,
        expected_catalog_retrieval_digest=artifact.catalog_retrieval_digest,
        expected_planning_environment_digest=artifact.planning_environment_digest,
    )


__all__ = [
    "DEFAULT_RUNTIME_PLANNER_VERSION",
    "EngineComposition",
    "open_engine_composition",
    "open_managed_engine_composition",
]
