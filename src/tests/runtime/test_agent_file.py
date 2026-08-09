from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from ctx.engine.content import MaterialIdentity
from ctx.engine.engine import CtxEngine
from ctx.engine.installation import InstallExecutionBinding, InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import (
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
)
from ctx.engine.protocol import HostAction
from ctx.runtime.agent_file import (
    AGENT_FILE_SCANNER_VERSION,
    AgentFileBodySource,
    AgentFileDriverFactory,
    agent_file_target_identity_digest,
)
from ctx.runtime.install_execution import (
    InstallDriverRegistration,
    InstallDriverRegistry,
    InstallDriverRequest,
    prepare_install_execution,
)
from ctx.utils._fs_utils import ensure_secure_directory
from tests.engine import test_engine_install_coordinator as support


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the agent-file actuator is disabled until its native Windows implementation ships",
)


BODY = "---\nname: reviewer\ndescription: exact agent test body\n---\nReview the current task.\n"
CAPABILITY_ID = "agent:reviewer"
MAX_CAPABILITY_ID = "agent:" + ("a" * 122)
YAML_UNSAFE_PLAIN_SCALARS = (
    "- unsafe",
    "? unsafe",
    ": unsafe",
    ",unsafe",
    "[unsafe]",
    "]unsafe",
    "{unsafe}",
    "}unsafe",
    "# unsafe",
    "&unsafe",
    "*unsafe",
    "!unsafe",
    "| unsafe",
    "> unsafe",
    "'unsafe'",
    '"unsafe"',
    "%unsafe",
    "@unsafe",
    "`unsafe",
    "safe: mapping",
    "safe # comment",
    "true",
    "null",
    "123",
    "1e3",
    "0x10",
    "2026-01-01",
)


class _Source(AgentFileBodySource):
    def __init__(self, body: str) -> None:
        self.body = body
        self.load_calls = 0
        self.claim_seen = False

    def load(self, request: InstallDriverRequest, material: MaterialIdentity) -> str:
        self.load_calls += 1
        self.claim_seen = request.action.kind == "InstallCapability"
        assert material.kind == "agent"
        return self.body


def _material(
    body: str = BODY,
    *,
    capability_id: str = CAPABILITY_ID,
) -> MaterialIdentity:
    encoded = body.encode("utf-8")
    return MaterialIdentity.create(
        capability_id=capability_id,
        kind="agent",
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        content_bytes=len(encoded),
    )


def _selection(
    body: str = BODY,
    *,
    capability_id: str = CAPABILITY_ID,
) -> CapabilityPlanSelectionV3:
    material = _material(body, capability_id=capability_id)
    slug = capability_id.split(":", 1)[1]
    descriptor = InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind="agent",
        installer_id="ctx-local-agent-file-v1",
        plan_digest=support.INSTALL_PLAN_DIGEST,
        provenance_digest=support._digest("agent-installation-snapshot"),
        result_material_identity_digest=material.identity_digest,
    )
    base = support._selection()
    return replace(
        base,
        presentation=CapabilityCandidate(
            capability_id=capability_id,
            kind="agent",
            name=slug,
            source_digest=support.SOURCE_DIGEST,
            normalized_score_ppm=900_000,
            matching_signals=("review", "testing"),
            reason_codes=("exact-tag-match",),
            actionability="install",
            install_descriptor_digest=descriptor.descriptor_digest,
            install_plan_digest=descriptor.plan_digest,
        ),
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=capability_id,
            kind="agent",
            catalog_namespace_digest=support._digest("catalog-namespace"),
        ),
        benefit=CapabilityBenefitProjection(
            tier="executable",
            individual_net_benefit_u=600_000,
            marginal_net_benefit_u=600_000,
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
    material_body: str | None = None,
    capability_id: str = CAPABILITY_ID,
):
    selection = _selection(
        source_body if material_body is None else material_body,
        capability_id=capability_id,
    )
    authority = selection.authority
    assert isinstance(authority, InstallPlanningAuthority)
    monkeypatch.setattr(support, "_selection", lambda: selection)
    engine, policy = support._engine(tmp_path, descriptor=authority.descriptor)
    action = support._pending_install(engine)
    agent_root = tmp_path / "ctx-private" / "inactive-agents"
    ensure_secure_directory(agent_root)
    source = _Source(source_body)
    binding = InstallExecutionBinding(
        driver_id=authority.descriptor.installer_id,
        driver_digest=action.payload["installer_digest"],  # type: ignore[arg-type]
        host_identity_digest=support._digest("host:agent-file-test"),
        target_identity_digest=agent_file_target_identity_digest(agent_root),
    )
    factory = AgentFileDriverFactory(
        inactive_agent_root=agent_root,
        body_source=source,
        expected_target_identity_digest=binding.target_identity_digest,
    )
    registry = InstallDriverRegistry(
        (
            InstallDriverRegistration(
                binding=binding,
                capability_kind="agent",
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
    return engine, action, authority.result_material, source, agent_root, handle


def _claim_without_execution(
    engine: CtxEngine,
    action: HostAction,
    agent_root: Path,
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
        host_identity_digest=support._digest("host:agent-file-test"),
        target_identity_digest=agent_file_target_identity_digest(agent_root),
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
    source: AgentFileBodySource,
    agent_root: Path,
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
        host_identity_digest=support._digest("host:agent-file-test"),
        target_identity_digest=agent_file_target_identity_digest(agent_root),
    )
    registry = InstallDriverRegistry(
        (
            InstallDriverRegistration(
                binding=binding,
                capability_kind="agent",
                factory=AgentFileDriverFactory(
                    inactive_agent_root=agent_root,
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


def _bounded_stage_path(
    agent_root: Path,
    material: MaterialIdentity,
    action_content_digest: str,
) -> Path:
    namespace = hashlib.sha256(
        json.dumps(
            {
                "capability_id": material.capability_id,
                "content_sha256": material.content_sha256,
                "format_scanner_version": AGENT_FILE_SCANNER_VERSION,
                "schema": "ctx.agent-file-stage-namespace-v1",
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return agent_root / f".ctx-agent-{namespace}-{action_content_digest}.pending"


def test_agent_body_is_claimed_then_published_to_one_inactive_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
    )

    assert source.load_calls == 0
    assert not engine.install_execution_status(action).claimed

    report = handle.execute()

    target = agent_root / "reviewer.md"
    assert report.outcome == "applied"
    assert report.settled
    assert source.load_calls == 1
    assert source.claim_seen
    assert target.read_text(encoding="utf-8") == BODY
    metadata = target.stat(follow_symlinks=False)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    snapshot = engine.snapshot(action.scope)
    assert snapshot.state is not None
    installed = snapshot.state.capability(CAPABILITY_ID)
    assert installed is not None
    assert installed.installation == "installed"
    assert installed.activation == "inactive"
    parsed = yaml.safe_load(target.read_text(encoding="utf-8").split("---", 2)[1])
    assert parsed == {"name": "reviewer", "description": "exact agent test body"}
    assert all(type(value) is str for value in parsed.values())


def test_maximum_length_agent_id_uses_a_bounded_recovery_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = MAX_CAPABILITY_ID.split(":", 1)[1]
    body = f"---\nname: {slug}\ndescription: maximum identifier\n---\nReview.\n"
    _engine, _action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=body,
        capability_id=MAX_CAPABILITY_ID,
    )

    report = handle.execute()

    target = agent_root / f"{slug}.md"
    assert report.outcome == "applied"
    assert report.settled
    assert source.load_calls == 1
    assert target.read_text(encoding="utf-8") == body
    assert len(target.name.encode("utf-8")) < 255
    assert not tuple(agent_root.glob(".ctx-agent-*.pending"))


def test_maximum_length_agent_id_recovers_a_bounded_crash_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = MAX_CAPABILITY_ID.split(":", 1)[1]
    body = f"---\nname: {slug}\ndescription: maximum identifier\n---\nReview.\n"
    engine, action, material, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=body,
        capability_id=MAX_CAPABILITY_ID,
    )
    _claim_without_execution(engine, action, agent_root)
    stage = _bounded_stage_path(agent_root, material, action.content_digest)
    stage.write_text(body, encoding="utf-8")
    stage.chmod(0o600)

    report = handle.execute()

    target = agent_root / f"{slug}.md"
    assert len(stage.name.encode("utf-8")) < 255
    assert report.outcome == "applied"
    assert report.settled
    assert not report.claim_was_new
    assert source.load_calls == 0
    assert target.read_text(encoding="utf-8") == body
    assert not stage.exists()


def test_wrong_source_bytes_cannot_create_an_agent_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=BODY + "wrong",
        material_body=BODY,
    )

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 1
    assert not (agent_root / "reviewer.md").exists()
    assert not engine.install_execution_status(action).outcome_recorded


def test_existing_claim_reconciles_valid_agent_without_loading_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
    )
    _claim_without_execution(engine, action, agent_root)
    target = agent_root / "reviewer.md"
    target.write_text(BODY, encoding="utf-8")
    target.chmod(0o600)

    def unavailable_body_source(
        _request: InstallDriverRequest,
        _material_identity: MaterialIdentity,
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


@pytest.mark.parametrize("crash_point", ["staged", "linked"])
def test_existing_claim_repairs_authenticated_agent_stage_without_reapply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    engine, action, material, source, agent_root, handle = _runtime(tmp_path, monkeypatch)
    _claim_without_execution(engine, action, agent_root)
    stage = _bounded_stage_path(agent_root, material, action.content_digest)
    target = agent_root / "reviewer.md"
    stage.write_text(BODY, encoding="utf-8")
    stage.chmod(0o600)
    if crash_point == "linked":
        os.link(stage, target)

    report = handle.execute()

    assert report.outcome == "applied"
    assert report.settled
    assert not report.claim_was_new
    assert source.load_calls == 0
    assert target.read_text(encoding="utf-8") == BODY
    assert target.stat(follow_symlinks=False).st_nlink == 1
    assert not stage.exists()


@pytest.mark.parametrize("hostile_kind", ["symlink", "hardlink"])
def test_linked_agent_target_is_never_followed_or_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hostile_kind: str,
) -> None:
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
    )
    outside = tmp_path / "outside-agent.md"
    outside.write_text(BODY, encoding="utf-8")
    outside.chmod(0o600)
    target = agent_root / "reviewer.md"
    if hostile_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 0
    assert outside.read_text(encoding="utf-8") == BODY
    assert not engine.install_execution_status(action).outcome_recorded


@pytest.mark.parametrize("failure_kind", ["stage-file", "directory"])
def test_fresh_handle_repairs_agent_durability_failure_without_reapply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
    )
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
    replacement = _replacement_handle(engine, action, source, agent_root)

    repaired = replacement.execute()

    assert repaired.outcome == "applied"
    assert repaired.settled
    assert not repaired.claim_was_new
    assert source.load_calls == 1
    target = agent_root / "reviewer.md"
    assert target.read_text(encoding="utf-8") == BODY
    assert target.stat(follow_symlinks=False).st_nlink == 1
    assert not tuple(agent_root.glob(".ctx-agent-*.pending"))


def test_agent_stage_flood_fails_closed_without_loading_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, source, agent_root, handle = _runtime(tmp_path, monkeypatch)
    for index in range(65):
        stage = _bounded_stage_path(agent_root, material, f"{index:064x}")
        stage.write_text(BODY, encoding="utf-8")
        stage.chmod(0o600)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 0
    assert not engine.install_execution_status(action).outcome_recorded


@pytest.mark.parametrize(
    "unsafe_body",
    [
        "---\nname: reviewer\ndescription: test\nhooks: post-tool\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\ncommand: python -c pass\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\nmcp-server: hidden\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\ntools: shell\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\npermissions: all\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\nscripts: setup.py\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\npath: /tmp/tool\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\ninclude: remote.md\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\nurl: https://example.invalid\n---\nInstructions.\n",
        (
            "---\nname: reviewer\ndescription: safe\u0085tools: Bash\u0085"
            "permissionMode: bypassPermissions\n---\nInstructions.\n"
        ),
        (
            "---\nname: reviewer\ndescription: safe\u2028tools: Bash\u2028"
            "permissionMode: bypassPermissions\n---\nInstructions.\n"
        ),
        (
            "---\nname: reviewer\ndescription: safe\u2029tools: Bash\u2029"
            "permissionMode: bypassPermissions\n---\nInstructions.\n"
        ),
        "---\nname: reviewer\nname: reviewer\ndescription: duplicate\n---\nInstructions.\n",
        "---\nname reviewer\ndescription: malformed\n---\nInstructions.\n",
        "---\nname: different\ndescription: mismatched\n---\nInstructions.\n",
        "---\nname: reviewer\ndescription: test\n---\nInstructions.\n---\nname: second\n",
        "---\nname: reviewer\ndescription: " + ("x" * 2_100) + "\n---\nInstructions.\n",
    ],
    ids=(
        "hook",
        "command",
        "embedded-mcp",
        "tool-grant",
        "permission-grant",
        "script",
        "path",
        "include",
        "external-reference",
        "unicode-line-separator-smuggling",
        "unicode-line-separator-u2028",
        "unicode-line-separator-u2029",
        "duplicate",
        "malformed",
        "name-mismatch",
        "multi-document",
        "oversized-frontmatter",
    ),
)
def test_executable_or_malformed_agent_frontmatter_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_body: str,
) -> None:
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=unsafe_body,
    )

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 1
    assert not (agent_root / "reviewer.md").exists()
    assert not tuple(agent_root.glob(".ctx-agent-*.pending"))
    status = engine.install_execution_status(action)
    assert status.claimed
    assert not status.outcome_recorded


@pytest.mark.parametrize("unsafe_scalar", YAML_UNSAFE_PLAIN_SCALARS)
def test_yaml_ambiguous_or_non_string_plain_scalar_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_scalar: str,
) -> None:
    unsafe = f"---\nname: reviewer\ndescription: {unsafe_scalar}\n---\nInstructions.\n"
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=unsafe,
    )

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 1
    assert not (agent_root / "reviewer.md").exists()
    assert not tuple(agent_root.glob(".ctx-agent-*.pending"))
    status = engine.install_execution_status(action)
    assert status.claimed
    assert not status.outcome_recorded


def test_existing_exact_hash_with_unsafe_format_is_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = "---\nname: reviewer\ndescription: test\ntools: shell\n---\nInstructions.\n"
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=unsafe,
    )
    target = agent_root / "reviewer.md"
    target.write_text(unsafe, encoding="utf-8")
    target.chmod(0o600)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 0
    assert target.read_text(encoding="utf-8") == unsafe
    assert not engine.install_execution_status(action).outcome_recorded


@pytest.mark.parametrize("unsafe_scalar", ["safe: mapping", "safe # comment", "true"])
def test_existing_exact_hash_with_yaml_ambiguous_scalar_is_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_scalar: str,
) -> None:
    unsafe = f"---\nname: reviewer\ndescription: {unsafe_scalar}\n---\nInstructions.\n"
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=unsafe,
    )
    target = agent_root / "reviewer.md"
    target.write_text(unsafe, encoding="utf-8")
    target.chmod(0o600)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 0
    assert target.read_text(encoding="utf-8") == unsafe
    assert not engine.install_execution_status(action).outcome_recorded


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_existing_exact_hash_with_unicode_separator_smuggling_is_not_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    separator: str,
) -> None:
    unsafe = (
        f"---\nname: reviewer\ndescription: safe{separator}tools: Bash{separator}"
        "permissionMode: bypassPermissions\n---\nInstructions.\n"
    )
    engine, action, _material_identity, source, agent_root, handle = _runtime(
        tmp_path,
        monkeypatch,
        source_body=unsafe,
    )
    target = agent_root / "reviewer.md"
    target.write_text(unsafe, encoding="utf-8")
    target.chmod(0o600)

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 0
    assert target.read_text(encoding="utf-8") == unsafe
    assert not engine.install_execution_status(action).outcome_recorded
