from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ctx.runtime.activated_skill_exposure as exposure_module
from ctx.core.install_policy_store import persist_install_policy
from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, PreparedCapabilityContent
from ctx.engine.engine import (
    CtxEngine,
    CtxEngineError,
    _PromptContextMaterialPermit,
    _PromptContextMaterialRoutePermit,
)
from ctx.engine.installation import InstallConsentPolicy
from ctx.engine.planning_v3 import CapabilityPlanSelectionV3, LoadPlanningAuthority
from ctx.engine.protocol import (
    PROMPT_CONTEXT_ACTION_PAYLOAD_SCHEMA_V1,
    PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
    HostAction,
    ScopeRef,
)
from ctx.engine.reducer import INSTALLATION_REDUCER_VERSION
from ctx.engine.replay import DefaultReplayInputFactory
from ctx.engine.state import CapabilityStateV3
from ctx.engine.store import SQLiteEngineStore, StreamId
from ctx.runtime.activated_skill_exposure import (
    ActivatedSkillExposureError,
    ActivatedSkillExposurePreparation,
    prepare_activated_skill_exposure,
)
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.production_catalog import open_release_pinned_query_catalog
from ctx.runtime.release_skill_dispatcher import (
    ReleaseSkillInstallRequest,
    _scope,
    dispatch_release_skill_install,
)
from ctx.runtime.release_skill_lifecycle import activate_installed_release_skill


NOW = "2026-08-02T12:30:00Z"
TRUSTED_NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)


def _digest(value: object) -> str:
    encoded = (
        value.encode("utf-8")
        if isinstance(value, str)
        else json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    return hashlib.sha256(encoded).hexdigest()


def _request(tmp_path: Path) -> ReleaseSkillInstallRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    skill_root = tmp_path / "skills"
    skill_root.mkdir(mode=0o700)
    request = ReleaseSkillInstallRequest(
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
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    installed = dispatch_release_skill_install(
        request,
        trusted_utc_now=lambda: TRUSTED_NOW,
    )
    assert installed.status == "installed"
    return request


def _activated_preparation(
    tmp_path: Path,
) -> tuple[ReleaseSkillInstallRequest, ActivatedSkillExposurePreparation]:
    request = _request(tmp_path)
    activation = activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: TRUSTED_NOW,
    )
    return request, prepare_activated_skill_exposure(
        request=request,
        activation_evidence=activation,
    )


def _selection_and_action(
    request: ReleaseSkillInstallRequest,
    *,
    catalog_planning_authority: bool = False,
) -> tuple[CapabilityPlanSelectionV3, HostAction]:
    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )
    snapshot = engine.snapshot(_scope(request))
    assert snapshot.state is not None
    capability = snapshot.state.capability(RELEASE_INSTALL_SKILL_ID)
    assert isinstance(capability, CapabilityStateV3)
    authorized = capability.current_authorized_material
    assert authorized is not None
    original = capability.selection.selection
    presentation = replace(
        original.presentation,
        actionability="load",
        install_descriptor_digest=None,
        install_plan_digest=None,
    )
    planning_material = authorized
    if catalog_planning_authority:
        planning_material = AuthorizedMaterial.from_catalog(
            catalog_identity_digest=original.catalog_identity.identity_digest,
            descriptor=MaterialDescriptor.create(
                capability_id=capability.capability_id,
                kind=capability.kind,
                actionability="load",
                content_sha256=capability.material_identity.content_sha256,
                content_bytes=capability.material_identity.content_bytes,
                estimated_tokens=max(
                    1,
                    (capability.material_identity.content_bytes + 3) // 4,
                ),
                provenance_digest=_digest("reviewed-load-material-snapshot"),
                material_identity_digest=capability.material_identity.identity_digest,
            ),
        )
    selection = CapabilityPlanSelectionV3(
        presentation=presentation,
        catalog_identity=original.catalog_identity,
        benefit=original.benefit,
        authority=LoadPlanningAuthority(material=planning_material),
    )
    row = {
        "authorized_material": planning_material.to_dict(),
        "capability_id": capability.capability_id,
        "capability_kind": capability.kind,
        "catalog_identity": capability.catalog_identity.to_dict(),
        "material_identity": capability.material_identity.to_dict(),
        "source_digest": presentation.source_digest,
    }
    plan_digest = _digest("logical-prompt-plan")
    presentation_action_id = "logical-prompt-presentation"
    presentation_action_content_digest = _digest("logical-prompt-presentation")
    source_digest = _digest(
        {
            "capabilities": [row],
            "execution_intent": "activate",
            "plan_digest": plan_digest,
            "presentation_action_content_digest": presentation_action_content_digest,
            "presentation_action_id": presentation_action_id,
            "schema": "ctx.prompt-context-bundle-v1",
        }
    )
    scope = ScopeRef(
        tenant_id="local",
        workspace_id=_digest(str(request.workspace.resolve())),
        repository_id=_digest(str(request.workspace.resolve())),
        session_id="later-logical-prompt",
        exposure_id="later-logical-prompt-exposure",
        host_context_id="host-neutral-test",
    )
    action = HostAction(
        action_id="later-logical-prompt-context",
        kind="PreparePromptContext",
        scope=scope,
        precondition_revision=2,
        payload={
            "capabilities": (row,),
            "execution_intent": "activate",
            "plan_digest": plan_digest,
            "presentation_action_content_digest": presentation_action_content_digest,
            "presentation_action_id": presentation_action_id,
            "schema": PROMPT_CONTEXT_ACTION_PAYLOAD_SCHEMA_V1,
        },
        source_digest=source_digest,
        plan_id=plan_digest,
        catalog_snapshot_id=capability.catalog_snapshot_id,
        lease_id="later-logical-prompt-lease",
        expires_at="2099-08-02T12:30:00Z",
        required_host_feature="prompt-context",
        verification={
            "receipt_required": True,
            "expected_state": "prompt-context-prepared",
            "receipt_schema": PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
        },
        rollback={
            "kind": "discard-prompt-context",
            "exposure_id": scope.exposure_id,
        },
    )
    return selection, action


def _query_engine(request: ReleaseSkillInstallRequest) -> CtxEngine:
    return CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )


def _route_authority(
    request: ReleaseSkillInstallRequest,
    action: HostAction,
    selections: tuple[CapabilityPlanSelectionV3, ...],
    *,
    expected_catalog_snapshot_digest: str | None = None,
) -> _PromptContextMaterialRoutePermit:
    catalog_digest = expected_catalog_snapshot_digest or action.catalog_snapshot_id or ""
    authority = _query_engine(request)._issue_prompt_context_material_permit(  # noqa: SLF001
        action,
        selections,
        expected_catalog_snapshot_digest=catalog_digest,
    )
    routes = authority._consume_and_issue_routes(  # noqa: SLF001
        action=action,
        selections=selections,
        expected_catalog_snapshot_digest=catalog_digest,
        external_capability_ids=frozenset({RELEASE_INSTALL_SKILL_ID}),
    )
    return routes[RELEASE_INSTALL_SKILL_ID]


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_prepares_one_authority_bound_material_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)
    calls: list[tuple[str, tuple[str, ...], str]] = []

    def authorize(
        _engine: CtxEngine,
        prompt_action: HostAction,
        selections: tuple[CapabilityPlanSelectionV3, ...],
        *,
        expected_catalog_snapshot_digest: str,
    ) -> None:
        calls.append(
            (
                prompt_action.content_digest,
                tuple(item.presentation.capability_id for item in selections),
                expected_catalog_snapshot_digest,
            )
        )

    monkeypatch.setattr(CtxEngine, "authorize_prompt_context", authorize)
    route_authority = _route_authority(request, action, (selection,))
    lifecycle_store = SQLiteEngineStore(request.journal_path)
    lifecycle_stream = StreamId.from_scope(_scope(request))
    records_before = tuple(lifecycle_store.records(lifecycle_stream))

    content = preparation.material_permit.prepare_prompt_context_once(
        action=action,
        selection=selection,
        expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
        route_authority=route_authority,
    )

    assert isinstance(content, PreparedCapabilityContent)
    assert content.capability_id == RELEASE_INSTALL_SKILL_ID
    assert hashlib.sha256(content.content.encode("utf-8")).hexdigest() == content.content_sha256
    assert len(content.content.encode("utf-8")) == content.content_bytes
    assert tuple(lifecycle_store.records(lifecycle_stream)) == records_before
    assert calls == [
        (
            action.content_digest,
            (RELEASE_INSTALL_SKILL_ID,),
            action.catalog_snapshot_id,
        )
    ]
    with pytest.raises(ActivatedSkillExposureError, match="already consumed"):
        preparation.material_permit.prepare_prompt_context_once(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            route_authority=route_authority,
        )


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_routes_reviewed_catalog_selection_to_exact_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(
        request,
        catalog_planning_authority=True,
    )
    assert isinstance(selection.authority, LoadPlanningAuthority)
    selected_material = selection.authority.material
    assert selected_material.origin == "catalog"

    monkeypatch.setattr(
        CtxEngine,
        "authorize_prompt_context",
        lambda _engine, _action, _selections, **_kwargs: None,
    )

    content = preparation.material_permit.prepare_prompt_context_once(
        action=action,
        selection=selection,
        expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
        route_authority=_route_authority(request, action, (selection,)),
    )

    assert content.capability_id == RELEASE_INSTALL_SKILL_ID
    assert content.content_sha256 == request.skill_store_root.joinpath(content.content_sha256).name


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_authorizes_before_reading_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)

    def forbidden_rederivation(**_kwargs: object) -> object:
        raise AssertionError("material was read before route authorization")

    monkeypatch.setattr(
        exposure_module,
        "_rederive_under_material_lock",
        forbidden_rederivation,
    )
    monkeypatch.setattr(
        CtxEngine,
        "authorize_prompt_context",
        lambda *_args, **_kwargs: None,
    )
    route_authority = _route_authority(
        request,
        action,
        (selection,),
        expected_catalog_snapshot_digest="a" * 64,
    )

    with pytest.raises(ActivatedSkillExposureError, match="material route rejected"):
        preparation.material_permit.prepare_prompt_context_once(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            route_authority=route_authority,
        )


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_permit_is_atomic_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)
    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()

    monkeypatch.setattr(
        CtxEngine,
        "authorize_prompt_context",
        lambda *_args, **_kwargs: None,
    )
    route_authority = _route_authority(request, action, (selection,))
    original_rederive = exposure_module._rederive_under_material_lock

    def blocked_rederive(**kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(action.content_digest)
        entered.set()
        assert release.wait(timeout=10)
        return original_rederive(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(exposure_module, "_rederive_under_material_lock", blocked_rederive)

    def consume() -> PreparedCapabilityContent:
        return preparation.material_permit.prepare_prompt_context_once(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            route_authority=route_authority,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(consume)
        assert entered.wait(timeout=10)
        second = pool.submit(consume)
        with pytest.raises(ActivatedSkillExposureError, match="already consumed"):
            second.result(timeout=10)
        release.set()
        prepared = first.result(timeout=10)

    assert prepared.capability_id == RELEASE_INSTALL_SKILL_ID
    assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_is_digest_only_and_non_serializable(tmp_path: Path) -> None:
    request, preparation = _activated_preparation(tmp_path)

    assert {field.name for field in fields(preparation)} == {
        "activation_evidence_digest",
        "installed_lineage_digest",
        "material_identity_digest",
        "material_permit",
        "preparation_digest",
        "skill_cas_root_identity_digest",
    }
    rendered = repr(preparation)
    assert str(request.journal_path) not in rendered
    assert str(request.skill_store_root) not in rendered
    assert "SKILL.md" not in rendered
    assert not hasattr(preparation.material_permit, "emit")
    with pytest.raises(TypeError):
        copy.copy(preparation.material_permit)
    with pytest.raises(TypeError):
        pickle.dumps(preparation.material_permit)


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_rejects_arbitrary_callable_authority(
    tmp_path: Path,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)

    class FakeAuthority:
        def authorize_prompt_context(self, *_args: object, **_kwargs: object) -> None:
            return None

    with pytest.raises(TypeError, match="engine-issued material route"):
        preparation.material_permit.prepare_prompt_context_once(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            route_authority=FakeAuthority(),  # type: ignore[arg-type]
        )


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_release_catalog_rejects_callable_authority_and_external_source(
    tmp_path: Path,
) -> None:
    request, _preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request, catalog_planning_authority=True)
    catalog = open_release_pinned_query_catalog()

    class FakeAuthority:
        def authorize_prompt_context(self, *_args: object, **_kwargs: object) -> None:
            return None

    with pytest.raises(TypeError, match="engine-issued material authority"):
        catalog.prepare_prompt_context(
            action,
            (selection,),
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            authority=FakeAuthority(),  # type: ignore[arg-type]
        )
    forged = object.__new__(_PromptContextMaterialPermit)
    with pytest.raises(TypeError, match="exact activated-skill permit"):
        catalog.prepare_prompt_context(
            action,
            (selection,),
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            authority=forged,
            external_material_source=lambda: None,
        )
    catalog.close()


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_engine_bundle_permit_cannot_be_replayed_through_reopened_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)
    selections = (selection,)
    catalog_digest = action.catalog_snapshot_id or ""
    monkeypatch.setattr(
        CtxEngine,
        "authorize_prompt_context",
        lambda *_args, **_kwargs: None,
    )
    authority = _query_engine(request)._issue_prompt_context_material_permit(  # noqa: SLF001
        action,
        selections,
        expected_catalog_snapshot_digest=catalog_digest,
    )
    authority._consume_and_issue_routes(  # noqa: SLF001
        action=action,
        selections=selections,
        expected_catalog_snapshot_digest=catalog_digest,
        external_capability_ids=frozenset({RELEASE_INSTALL_SKILL_ID}),
    )
    reopened = open_release_pinned_query_catalog()
    reopened.close()

    with pytest.raises(CtxEngineError, match="already consumed"):
        authority._consume_and_issue_routes(  # noqa: SLF001
            action=action,
            selections=selections,
            expected_catalog_snapshot_digest=catalog_digest,
            external_capability_ids=frozenset({RELEASE_INSTALL_SKILL_ID}),
        )


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_does_not_recreate_missing_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)
    monkeypatch.setattr(
        CtxEngine,
        "authorize_prompt_context",
        lambda *_args, **_kwargs: None,
    )
    route_authority = _route_authority(request, action, (selection,))
    request.journal_path.unlink()
    state_after_unlink = frozenset(request.journal_path.parent.iterdir())

    with pytest.raises(ActivatedSkillExposureError, match="could not be rederived"):
        preparation.material_permit.prepare_prompt_context_once(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            route_authority=route_authority,
        )

    assert not request.journal_path.exists()
    assert frozenset(request.journal_path.parent.iterdir()) == state_after_unlink


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_sanitizes_unexpected_consumption_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)
    secret = f"{request.journal_path}:token-secret"
    monkeypatch.setattr(
        CtxEngine,
        "authorize_prompt_context",
        lambda *_args, **_kwargs: None,
    )

    def fail(**_kwargs: object) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(exposure_module, "_rederive_under_material_lock", fail)
    with pytest.raises(ActivatedSkillExposureError) as captured:
        preparation.material_permit.prepare_prompt_context_once(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            route_authority=_route_authority(request, action, (selection,)),
        )

    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_reverifies_cas_at_permit_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, preparation = _activated_preparation(tmp_path)
    selection, action = _selection_and_action(request)
    monkeypatch.setattr(
        CtxEngine,
        "authorize_prompt_context",
        lambda *_args, **_kwargs: None,
    )
    route_authority = _route_authority(request, action, (selection,))
    assert isinstance(selection.authority, LoadPlanningAuthority)
    material = selection.authority.material
    assert material.installed_material_lineage is not None
    engine = CtxEngine(
        store=SQLiteEngineStore(request.journal_path),
        replay_factory=DefaultReplayInputFactory(reducer_version=INSTALLATION_REDUCER_VERSION),
    )
    snapshot = engine.snapshot(_scope(request))
    assert snapshot.state is not None
    capability = snapshot.state.capability(RELEASE_INSTALL_SKILL_ID)
    assert isinstance(capability, CapabilityStateV3)
    target = request.skill_store_root / capability.material_identity.content_sha256
    target.write_bytes(b"x" * capability.material_identity.content_bytes)

    with pytest.raises(ActivatedSkillExposureError, match="exact installed UTF-8 material"):
        preparation.material_permit.prepare_prompt_context_once(
            action=action,
            selection=selection,
            expected_catalog_snapshot_digest=action.catalog_snapshot_id or "",
            route_authority=route_authority,
        )


@pytest.mark.skipif(os.name == "nt", reason="installed skill CAS is POSIX-only")
def test_activated_skill_exposure_rejects_nonmatching_activation_evidence(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    request = _request(first)
    other_request = _request(second)
    activate_installed_release_skill(
        request,
        trusted_utc_now=lambda: TRUSTED_NOW,
    )
    other_activation = activate_installed_release_skill(
        other_request,
        trusted_utc_now=lambda: TRUSTED_NOW,
    )

    with pytest.raises(ActivatedSkillExposureError, match="activation evidence"):
        prepare_activated_skill_exposure(
            request=request,
            activation_evidence=other_activation,
        )
