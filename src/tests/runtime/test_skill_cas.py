from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from ctx.engine.content import MaterialIdentity
from ctx.engine.engine import CtxEngine
from ctx.engine.installation import InstallExecutionBinding
from ctx.engine.planning_v3 import InstallPlanningAuthority
from ctx.engine.protocol import HostAction
from ctx.runtime.install_execution import (
    InstallDriverRegistration,
    InstallDriverRegistry,
    InstallDriverRequest,
    prepare_install_execution,
)
from ctx.runtime.skill_cas import (
    SkillCasBodySource,
    SkillCasDriverFactory,
    skill_cas_target_identity_digest,
)
from ctx.utils._fs_utils import ensure_secure_directory
from tests.engine import test_engine_install_coordinator as support


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the built-in skill CAS is disabled until its native Windows actuator ships",
)


BODY = "---\nname: remote-testing\ndescription: exact CAS test skill\n---\nUse tests first.\n"


class _Source(SkillCasBodySource):
    def __init__(self, body: str) -> None:
        self.body = body
        self.load_calls = 0
        self.claim_seen = False

    def load(self, request: InstallDriverRequest, material: MaterialIdentity) -> str:
        self.load_calls += 1
        self.claim_seen = request.action.kind == "InstallCapability"
        assert material.kind == "skill"
        return self.body


def _material(body: str = BODY) -> MaterialIdentity:
    encoded = body.encode("utf-8")
    return MaterialIdentity.create(
        capability_id="skill:remote-testing",
        kind="skill",
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        content_bytes=len(encoded),
    )


def _selection(body: str = BODY):
    material = _material(body)
    descriptor = support._descriptor(result_material=material)
    base = support._selection()
    return replace(
        base,
        presentation=replace(
            base.presentation,
            install_descriptor_digest=descriptor.descriptor_digest,
        ),
        authority=InstallPlanningAuthority(
            descriptor=descriptor,
            result_material=material,
        ),
    )


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    source_body: str = BODY,
):
    selection = _selection()
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    monkeypatch.setattr(support, "_selection", lambda: selection)
    engine, policy = support._engine(tmp_path, descriptor=authority.descriptor)
    action = support._pending_install(engine)
    skill_root = tmp_path / "ctx-private" / "skills"
    ensure_secure_directory(skill_root)
    source = _Source(source_body)
    binding = InstallExecutionBinding(
        driver_id=authority.descriptor.installer_id,
        driver_digest=action.payload["installer_digest"],  # type: ignore[arg-type]
        host_identity_digest=support._digest("host:skill-cas-test"),
        target_identity_digest=skill_cas_target_identity_digest(skill_root),
    )
    factory = SkillCasDriverFactory(
        skill_store_root=skill_root,
        body_source=source,
        expected_target_identity_digest=binding.target_identity_digest,
    )
    registry = InstallDriverRegistry(
        (
            InstallDriverRegistration(
                binding=binding,
                capability_kind="skill",
                factory=factory,
            ),
        )
    )
    handle = prepare_install_execution(
        engine=engine,
        action=action,
        selection=selection,
        descriptor=authority.descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy.policy_digest,
        registry=registry,
    )
    return engine, action, authority.result_material, source, skill_root, handle


def _claim_without_execution(
    engine: CtxEngine,
    action: HostAction,
    skill_root: Path,
) -> None:
    selection = support._selection()
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    policy_digest = snapshot.state.install_policy_snapshot_digest
    assert policy_digest is not None
    binding = InstallExecutionBinding(
        driver_id=authority.descriptor.installer_id,
        driver_digest=action.payload["installer_digest"],  # type: ignore[arg-type]
        host_identity_digest=support._digest("host:skill-cas-test"),
        target_identity_digest=skill_cas_target_identity_digest(skill_root),
    )
    engine.authorize_install(
        action,
        selection,
        authority.descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy_digest,
        execution_binding=binding,
    )


def _replacement_handle(
    engine: CtxEngine,
    action: HostAction,
    source: SkillCasBodySource,
    skill_root: Path,
):
    selection = support._selection()
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    policy_digest = snapshot.state.install_policy_snapshot_digest
    assert policy_digest is not None
    binding = InstallExecutionBinding(
        driver_id=authority.descriptor.installer_id,
        driver_digest=action.payload["installer_digest"],  # type: ignore[arg-type]
        host_identity_digest=support._digest("host:skill-cas-test"),
        target_identity_digest=skill_cas_target_identity_digest(skill_root),
    )
    registry = InstallDriverRegistry(
        (
            InstallDriverRegistration(
                binding=binding,
                capability_kind="skill",
                factory=SkillCasDriverFactory(
                    skill_store_root=skill_root,
                    body_source=source,
                    expected_target_identity_digest=binding.target_identity_digest,
                ),
            ),
        )
    )
    return prepare_install_execution(
        engine=engine,
        action=action,
        selection=selection,
        descriptor=authority.descriptor,
        expected_catalog_snapshot_digest=support.CATALOG_DIGEST,
        expected_policy_digest=policy_digest,
        registry=registry,
    )


def test_skill_body_is_loaded_only_after_claim_and_published_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, source, skill_root, handle = _runtime(tmp_path, monkeypatch)

    assert source.load_calls == 0
    assert not engine.install_execution_status(action).claimed

    report = handle.execute()

    target = skill_root / material.content_sha256
    assert report.outcome == "applied"
    assert report.settled
    assert source.load_calls == 1
    assert source.claim_seen
    assert target.read_text(encoding="utf-8") == BODY
    metadata = target.stat(follow_symlinks=False)
    if os.name != "nt":
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_nlink == 1


def test_wrong_source_bytes_fail_before_filesystem_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, source, skill_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body="wrong body",
    )

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 1
    assert not (skill_root / material.content_sha256).exists()
    status = engine.install_execution_status(action)
    assert status.claimed and not status.outcome_recorded


def test_existing_exact_object_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    target = skill_root / material.content_sha256
    target.write_text(BODY, encoding="utf-8")
    stale_stage = skill_root / (
        f".ctx-skill-{material.content_sha256}-{action.content_digest}.pending"
    )
    stale_stage.write_text(BODY, encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
        stale_stage.chmod(0o600)

    before = target.stat(follow_symlinks=False)
    report = handle.execute()
    after = target.stat(follow_symlinks=False)

    assert report.outcome == "applied"
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert not stale_stage.exists()


@pytest.mark.parametrize("crash_point", ["staged", "linked"])
def test_reconciliation_repairs_fully_authenticated_crash_stage_without_reapply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    _claim_without_execution(engine, action, skill_root)
    stage = skill_root / (f".ctx-skill-{material.content_sha256}-{action.content_digest}.pending")
    target = skill_root / material.content_sha256
    stage.write_text(BODY, encoding="utf-8")
    if os.name != "nt":
        stage.chmod(0o600)
    if crash_point == "linked":
        os.link(stage, target)

    report = handle.execute()

    assert report.outcome == "applied"
    assert report.settled
    assert not report.claim_was_new
    assert target.read_text(encoding="utf-8") == BODY
    assert not stage.exists()
    if os.name != "nt":
        assert target.stat(follow_symlinks=False).st_nlink == 1


def test_partial_crash_stage_is_removed_and_settled_as_verified_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    _claim_without_execution(engine, action, skill_root)
    stage = skill_root / (f".ctx-skill-{material.content_sha256}-{action.content_digest}.pending")
    stage.write_text("partial", encoding="utf-8")
    if os.name != "nt":
        stage.chmod(0o600)

    report = handle.execute()

    assert report.outcome == "failed"
    assert report.settled
    assert not stage.exists()
    assert not (skill_root / material.content_sha256).exists()
    status = engine.install_execution_status(action)
    assert status.outcome == "failed"


def test_wrong_existing_object_is_never_replaced_or_settled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    target = skill_root / material.content_sha256
    target.write_text("hostile-existing-object", encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
    before = target.stat(follow_symlinks=False)

    report = handle.execute()

    after = target.stat(follow_symlinks=False)
    assert report.outcome == "indeterminate"
    assert not report.settled
    assert target.read_text(encoding="utf-8") == "hostile-existing-object"
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert not engine.install_execution_status(action).outcome_recorded


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and hardlink contract")
@pytest.mark.parametrize("hostile_kind", ["symlink", "hardlink"])
def test_linked_target_is_never_followed_or_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostile_kind: str,
) -> None:
    engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.write_text(BODY, encoding="utf-8")
    outside.chmod(0o600)
    target = skill_root / material.content_sha256
    if hostile_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert outside.read_text(encoding="utf-8") == BODY
    assert not engine.install_execution_status(action).outcome_recorded


def test_stage_file_fsync_failure_cannot_settle_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, _material_identity, _source, _skill_root, handle = _runtime(
        tmp_path,
        monkeypatch,
    )
    original_fsync = os.fsync

    def fail_regular_file_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            raise OSError("injected stage-file fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_regular_file_fsync)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    status = engine.install_execution_status(action)
    assert status.claimed
    assert not status.outcome_recorded


def test_directory_fsync_failure_cannot_settle_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, _material_identity, _source, _skill_root, handle = _runtime(
        tmp_path,
        monkeypatch,
    )
    original_fsync = os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            raise OSError("injected CAS-directory fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    status = engine.install_execution_status(action)
    assert status.claimed
    assert not status.outcome_recorded


@pytest.mark.parametrize("failure_kind", ["stage-file", "directory"])
def test_fresh_handle_durably_repairs_sync_failure_without_reapplying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    engine, action, material, source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    original_fsync = os.fsync
    failed = False
    directory_fsync_calls = 0

    def fail_selected_fsync_once(descriptor: int) -> None:
        nonlocal directory_fsync_calls, failed
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            directory_fsync_calls += 1
        selected = (failure_kind == "stage-file" and stat.S_ISREG(metadata.st_mode)) or (
            failure_kind == "directory" and directory_fsync_calls == 3
        )
        if selected and not failed:
            failed = True
            raise OSError("injected one-shot durability failure")
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_selected_fsync_once)
    first = handle.execute()
    monkeypatch.setattr(os, "fsync", original_fsync)

    assert failed
    assert first.outcome == "indeterminate"
    assert not first.settled
    assert engine.install_execution_status(action).claimed
    replacement = _replacement_handle(engine, action, source, skill_root)

    repaired = replacement.execute()

    assert repaired.outcome == "applied"
    assert repaired.settled
    assert not repaired.claim_was_new
    assert source.load_calls == 1
    target = skill_root / material.content_sha256
    assert target.read_text(encoding="utf-8") == BODY
    assert target.stat(follow_symlinks=False).st_nlink == 1
    assert not tuple(skill_root.glob(f".ctx-skill-{material.content_sha256}-*.pending"))


def test_existing_claim_reconciles_exact_target_without_body_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    _claim_without_execution(engine, action, skill_root)
    target = skill_root / material.content_sha256
    target.write_text(BODY, encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)

    def unavailable_body_source(
        _request: InstallDriverRequest,
        _material: MaterialIdentity,
    ) -> str:
        source.load_calls += 1
        raise OSError("injected body-source outage")

    monkeypatch.setattr(source, "load", unavailable_body_source)

    report = handle.execute()

    assert report.outcome == "applied"
    assert report.settled
    assert not report.claim_was_new
    assert source.load_calls == 0
    assert target.read_text(encoding="utf-8") == BODY


def test_existing_claim_cleans_own_orphan_after_other_action_publishes_same_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    _claim_without_execution(engine, action, skill_root)
    orphan = skill_root / (f".ctx-skill-{material.content_sha256}-{action.content_digest}.pending")
    orphan.write_text(BODY, encoding="utf-8")
    target = skill_root / material.content_sha256
    target.write_text(BODY, encoding="utf-8")
    if os.name != "nt":
        orphan.chmod(0o600)
        target.chmod(0o600)

    report = handle.execute()

    assert report.outcome == "applied"
    assert report.settled
    assert not report.claim_was_new
    assert target.read_text(encoding="utf-8") == BODY
    assert not orphan.exists()
