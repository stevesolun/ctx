from __future__ import annotations

import copy
import os
import pickle
import stat
from pathlib import Path

import pytest

import ctx.runtime.release_skill_layout as layout_module
from ctx.runtime.release_skill_layout import (
    ReleaseSkillRuntimeLayoutError,
    open_release_skill_runtime_layout,
    open_workspace_release_skill_runtime_layout,
)


def _open(tmp_path: Path, workspace: Path):
    return open_release_skill_runtime_layout(
        state_root=tmp_path / "state",
        host_context_id="codex",
        native_session_id="native/session secret",
        workspace=workspace,
    )


def _open_workspace_layout(
    tmp_path: Path,
    workspace: Path,
    *,
    state_name: str = "workspace-state",
    policy_name: str = "onboarding-policy",
):
    policy_root = tmp_path / policy_name
    policy_root.mkdir(mode=0o700, exist_ok=True)
    policy_root.chmod(0o700)
    return open_workspace_release_skill_runtime_layout(
        state_root=tmp_path / state_name,
        policy_store_root=policy_root,
        workspace=workspace,
    )


def test_workspace_layout_is_stable_across_native_host_sessions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_root = tmp_path / "onboarding-policy"
    policy_root.mkdir(mode=0o700)
    policy_root.chmod(0o700)

    before = open_workspace_release_skill_runtime_layout(
        state_root=tmp_path / "workspace-state",
        policy_store_root=policy_root,
        workspace=workspace,
    )
    open_release_skill_runtime_layout(
        state_root=tmp_path / "legacy-state",
        host_context_id="codex",
        native_session_id="codex-turn-1",
        workspace=workspace,
    )
    open_release_skill_runtime_layout(
        state_root=tmp_path / "legacy-state",
        host_context_id="claude-code",
        native_session_id="claude-turn-9",
        workspace=workspace,
    )
    after = open_workspace_release_skill_runtime_layout(
        state_root=tmp_path / "workspace-state",
        policy_store_root=policy_root,
        workspace=workspace,
    )

    assert after == before
    assert before.host_context_id == "ctx-workspace-management"
    assert before.session_id.startswith("workspace-session-")
    assert "codex" not in before.session_id
    assert "claude" not in before.session_id
    assert before.journal_path == after.journal_path
    assert before.benefit_audit_path == after.benefit_audit_path
    assert before.consent_broker_path == after.consent_broker_path
    assert before.skill_store_root == after.skill_store_root
    assert before.policy_store_root == policy_root
    assert not (before.managed_root / "install-policy-v1").exists()


def test_workspace_layout_does_not_reinterpret_or_migrate_v1_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "shared-state"
    legacy = open_release_skill_runtime_layout(
        state_root=state_root,
        host_context_id="codex",
        native_session_id="legacy-session",
        workspace=workspace,
    )
    policy_root = tmp_path / "onboarding-policy"
    policy_root.mkdir(mode=0o700)
    policy_root.chmod(0o700)

    workspace_layout = open_workspace_release_skill_runtime_layout(
        state_root=state_root,
        policy_store_root=policy_root,
        workspace=workspace,
    )

    assert legacy.managed_root == state_root / "managed-capabilities-v1"
    assert workspace_layout.managed_root == state_root / "managed-capabilities-v2"
    assert legacy.policy_store_root.exists()
    assert legacy.skill_store_root.exists()
    assert workspace_layout.policy_store_root == policy_root
    assert workspace_layout.skill_store_root != legacy.skill_store_root
    assert legacy.consent_broker_path is None
    assert workspace_layout.consent_broker_path is not None


def test_v1_layout_keeps_legacy_namespace_and_host_session_partition(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    codex = open_release_skill_runtime_layout(
        state_root=tmp_path / "legacy-state",
        host_context_id="codex",
        native_session_id="codex-session",
        workspace=workspace,
    )
    claude = open_release_skill_runtime_layout(
        state_root=tmp_path / "legacy-state",
        host_context_id="claude-code",
        native_session_id="claude-session",
        workspace=workspace,
    )

    assert codex.managed_root == codex.state_root / "managed-capabilities-v1"
    assert codex.policy_store_root == codex.managed_root / "install-policy-v1"
    assert codex.skill_store_root == codex.managed_root / "skill-cas-v1"
    assert codex.session_root != claude.session_root
    assert codex.journal_path == codex.session_root / "engine.sqlite3"
    assert codex.benefit_audit_path == codex.session_root / "benefit.sqlite3"
    assert codex.consent_broker_path is None
    assert claude.consent_broker_path is None


def test_workspace_layout_keeps_management_and_host_exposure_separate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = _open_workspace_layout(tmp_path, workspace)

    assert layout.managed_root == layout.state_root / "managed-capabilities-v2"
    assert layout.session_root.parent.name == "workspaces"
    assert layout.session_root.name == layout.workspace_identity_digest
    assert layout.skill_store_root == layout.session_root / "skill-cas-v1"
    assert layout.consent_broker_path == layout.session_root / "install-consent-v1.sqlite3"
    assert not layout.consent_broker_path.exists()
    assert not any(part.startswith("host-") for part in layout.session_root.parts)
    assert "codex" not in os.fspath(layout.session_root)
    assert "claude" not in os.fspath(layout.session_root)

    request = layout.install_request(
        task="manage one useful capability",
        language="Python",
        occurred_at="2026-08-02T12:00:00Z",
    )
    assert request.host_context_id == "ctx-workspace-management"
    assert request.host_identity_digest == layout.host_identity_digest
    assert request.session_id == layout.session_id
    assert request.policy_store_root == layout.policy_store_root


def test_workspace_consent_broker_path_is_shared_but_outside_user_policy_and_cas_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _open_workspace_layout(tmp_path, workspace)
    second = open_workspace_release_skill_runtime_layout(
        state_root=first.state_root,
        policy_store_root=first.policy_store_root,
        workspace=workspace,
    )
    assert first.consent_broker_path is not None

    assert second.consent_broker_path == first.consent_broker_path
    assert first.consent_broker_path.parent == first.session_root
    assert first.consent_broker_path not in workspace.parents
    assert workspace not in first.consent_broker_path.parents
    assert first.policy_store_root not in first.consent_broker_path.parents
    assert first.skill_store_root not in first.consent_broker_path.parents
    assert not first.consent_broker_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-mode contract")
def test_workspace_layout_accepts_only_private_single_link_broker_database(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = _open_workspace_layout(tmp_path, workspace)
    assert layout.consent_broker_path is not None
    layout.consent_broker_path.touch(mode=0o600)
    layout.consent_broker_path.chmod(0o600)

    reopened = _open_workspace_layout(tmp_path, workspace)
    assert reopened.consent_broker_path == layout.consent_broker_path
    reopened.assert_current()

    layout.consent_broker_path.chmod(0o644)
    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="unsafe|owner-private"):
        reopened.assert_current()


@pytest.mark.skipif(os.name == "nt", reason="POSIX inode contract")
@pytest.mark.parametrize("hazard", ["symlink", "directory", "hardlink"])
def test_workspace_layout_rejects_broker_path_alias_hazards(
    tmp_path: Path,
    hazard: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = _open_workspace_layout(tmp_path, workspace)
    assert layout.consent_broker_path is not None
    if hazard == "symlink":
        layout.consent_broker_path.symlink_to(layout.policy_store_root, target_is_directory=True)
    elif hazard == "directory":
        layout.consent_broker_path.mkdir(mode=0o700)
    else:
        layout.journal_path.touch(mode=0o600)
        layout.journal_path.chmod(0o600)
        os.link(layout.journal_path, layout.consent_broker_path)

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="unsafe|distinct|regular|overlap"):
        _open_workspace_layout(tmp_path, workspace)


def test_workspace_layout_rejects_broker_path_escape_or_cas_nesting(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = _open_workspace_layout(tmp_path, workspace)
    assert layout.consent_broker_path is not None

    object.__setattr__(
        layout,
        "consent_broker_path",
        layout.skill_store_root / "install-consent-v1.sqlite3",
    )
    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="changed|unsafe"):
        layout.assert_current()


def test_workspace_layout_isolated_per_workspace_but_shares_explicit_policy(
    tmp_path: Path,
) -> None:
    first_workspace = tmp_path / "first-workspace"
    second_workspace = tmp_path / "second-workspace"
    first_workspace.mkdir()
    second_workspace.mkdir()
    policy_root = tmp_path / "onboarding-policy"
    policy_root.mkdir(mode=0o700)
    policy_root.chmod(0o700)

    first = open_workspace_release_skill_runtime_layout(
        state_root=tmp_path / "workspace-state",
        policy_store_root=policy_root,
        workspace=first_workspace,
    )
    second = open_workspace_release_skill_runtime_layout(
        state_root=tmp_path / "workspace-state",
        policy_store_root=policy_root,
        workspace=second_workspace,
    )

    assert first.session_id != second.session_id
    assert first.journal_path != second.journal_path
    assert first.benefit_audit_path != second.benefit_audit_path
    assert first.skill_store_root != second.skill_store_root
    assert first.policy_store_root == second.policy_store_root == policy_root


def test_workspace_layout_requires_absolute_input_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_root = tmp_path / "policy"
    policy_root.mkdir(mode=0o700)
    policy_root.chmod(0o700)

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="absolute"):
        open_workspace_release_skill_runtime_layout(
            state_root=Path("relative-state"),
            policy_store_root=policy_root,
            workspace=workspace,
        )
    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="absolute"):
        open_workspace_release_skill_runtime_layout(
            state_root=tmp_path / "state",
            policy_store_root=Path("relative-policy"),
            workspace=workspace,
        )

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="absolute"):
        open_workspace_release_skill_runtime_layout(
            state_root=tmp_path / "state",
            policy_store_root=policy_root,
            workspace=Path("workspace"),
        )


def test_workspace_layout_canonicalizes_absolute_lexical_aliases(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_root = tmp_path / "policy"
    policy_root.mkdir(mode=0o700)
    policy_root.chmod(0o700)
    lexical_parent = tmp_path / "lexical-parent"
    lexical_parent.mkdir()

    lexical = open_workspace_release_skill_runtime_layout(
        state_root=lexical_parent / ".." / "state",
        policy_store_root=lexical_parent / ".." / "policy",
        workspace=lexical_parent / ".." / "workspace",
    )
    canonical = open_workspace_release_skill_runtime_layout(
        state_root=tmp_path / "state",
        policy_store_root=policy_root,
        workspace=workspace,
    )

    assert lexical == canonical
    assert lexical.state_root == tmp_path / "state"
    assert lexical.policy_store_root == policy_root


@pytest.mark.parametrize(
    ("alias_kind", "expected_error"),
    (
        ("state", "distinct"),
        ("managed", "overlap"),
        ("workspace", "overlap"),
        ("skill", "overlap"),
    ),
)
def test_workspace_layout_rejects_policy_root_aliases(
    tmp_path: Path,
    alias_kind: str,
    expected_error: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    policy_root = tmp_path / "policy"
    policy_root.mkdir(mode=0o700)
    policy_root.chmod(0o700)
    baseline = open_workspace_release_skill_runtime_layout(
        state_root=state_root,
        policy_store_root=policy_root,
        workspace=workspace,
    )
    aliases = {
        "state": baseline.state_root,
        "managed": baseline.managed_root,
        "workspace": baseline.session_root,
        "skill": baseline.skill_store_root,
    }

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match=expected_error):
        open_workspace_release_skill_runtime_layout(
            state_root=state_root,
            policy_store_root=aliases[alias_kind],
            workspace=workspace,
        )


def test_workspace_layout_rejects_state_or_policy_alias_of_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    workspace.chmod(0o700)
    policy_root = tmp_path / "policy"
    policy_root.mkdir(mode=0o700)
    policy_root.chmod(0o700)

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="distinct"):
        open_workspace_release_skill_runtime_layout(
            state_root=workspace,
            policy_store_root=policy_root,
            workspace=workspace,
        )
    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="distinct"):
        open_workspace_release_skill_runtime_layout(
            state_root=tmp_path / "state",
            policy_store_root=workspace,
            workspace=workspace,
        )


@pytest.mark.parametrize(
    ("descendant", "ancestor"),
    (
        ("state", "workspace"),
        ("policy", "workspace"),
        ("workspace", "state"),
        ("workspace", "policy"),
        ("policy", "state"),
        ("state", "policy"),
    ),
)
def test_workspace_layout_rejects_nested_caller_roots(
    tmp_path: Path,
    descendant: str,
    ancestor: str,
) -> None:
    roots = {
        "state": tmp_path / "state",
        "policy": tmp_path / "policy",
        "workspace": tmp_path / "workspace",
    }
    roots[descendant] = roots[ancestor] / f"nested-{descendant}"
    for path in sorted(set(roots.values()), key=lambda item: len(item.parts)):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="overlap"):
        open_workspace_release_skill_runtime_layout(
            state_root=roots["state"],
            policy_store_root=roots["policy"],
            workspace=roots["workspace"],
        )

    assert not (roots["state"] / "managed-capabilities-v2").exists()


def test_workspace_layout_rejects_physical_aliases_before_creating_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    state_root.chmod(0o700)
    policy_alias = tmp_path / "policy-alias"
    policy_alias.symlink_to(state_root, target_is_directory=True)

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="distinct"):
        open_workspace_release_skill_runtime_layout(
            state_root=state_root,
            policy_store_root=policy_alias,
            workspace=workspace,
        )

    assert not (state_root / "managed-capabilities-v2").exists()


def test_workspace_layout_rejects_symlinked_policy_alias(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_policy = tmp_path / "real-policy"
    real_policy.mkdir(mode=0o700)
    real_policy.chmod(0o700)
    policy_alias = tmp_path / "policy-alias"
    policy_alias.symlink_to(real_policy, target_is_directory=True)

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="unsafe"):
        open_workspace_release_skill_runtime_layout(
            state_root=tmp_path / "state",
            policy_store_root=policy_alias,
            workspace=workspace,
        )


def test_workspace_layout_detects_policy_alias_after_open(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = _open_workspace_layout(tmp_path, workspace)
    layout.policy_store_root.rmdir()
    layout.policy_store_root.symlink_to(layout.skill_store_root, target_is_directory=True)

    with pytest.raises(ReleaseSkillRuntimeLayoutError):
        layout.assert_current()


@pytest.mark.parametrize("public_root", ["state", "policy"])
def test_workspace_layout_rejects_non_private_roots(
    tmp_path: Path,
    public_root: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    policy_root = tmp_path / "policy"
    state_root.mkdir(mode=0o700)
    policy_root.mkdir(mode=0o700)
    state_root.chmod(0o755 if public_root == "state" else 0o700)
    policy_root.chmod(0o755 if public_root == "policy" else 0o700)

    with pytest.raises(
        ReleaseSkillRuntimeLayoutError,
        match="unsafe|owner-private",
    ):
        open_workspace_release_skill_runtime_layout(
            state_root=state_root,
            policy_store_root=policy_root,
            workspace=workspace,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-mode contract")
def test_workspace_layout_roots_are_absolute_distinct_and_owner_private(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = _open_workspace_layout(tmp_path, workspace)
    roots = (
        layout.state_root,
        layout.managed_root,
        layout.session_root,
        layout.skill_store_root,
        layout.policy_store_root,
    )

    assert all(root.is_absolute() for root in roots)
    assert len(set(roots)) == len(roots)
    assert len({(root.stat().st_dev, root.stat().st_ino) for root in roots}) == len(roots)
    assert all(stat.S_IMODE(root.stat(follow_symlinks=False).st_mode) == 0o700 for root in roots)


def test_workspace_layout_fails_closed_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy_root = tmp_path / "policy"
    policy_root.mkdir()
    monkeypatch.setattr(layout_module.os, "name", "nt")

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="not available on Windows"):
        open_workspace_release_skill_runtime_layout(
            state_root=tmp_path / "state",
            policy_store_root=policy_root,
            workspace=workspace,
        )


def test_release_skill_layout_is_restart_stable_and_path_private(tmp_path: Path) -> None:
    workspace = tmp_path / "customer-secret-workspace"
    workspace.mkdir()

    first = _open(tmp_path, workspace)
    second = _open(tmp_path, workspace)

    assert first == second
    assert first.workspace_identity_digest == second.workspace_identity_digest
    assert first.session_id == second.session_id
    assert "native/session secret" not in os.fspath(first.session_root)
    assert workspace.name not in os.fspath(first.session_root)
    assert str(workspace) not in repr(first)
    with pytest.raises(TypeError):
        copy.copy(first)
    with pytest.raises(TypeError):
        pickle.dumps(first)


@pytest.mark.skipif(os.name == "nt", reason="POSIX owner-mode contract")
def test_release_skill_layout_creates_only_owner_private_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    layout = _open(tmp_path, workspace)

    for directory in (
        layout.state_root,
        layout.managed_root,
        layout.session_root,
        layout.skill_store_root,
        layout.policy_store_root,
    ):
        assert stat.S_IMODE(directory.stat(follow_symlinks=False).st_mode) == 0o700


def test_release_skill_layout_request_uses_only_derived_stable_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    layout = _open(tmp_path, workspace)

    request = layout.install_request(
        task="repair state restoration",
        language="Python",
        occurred_at="2026-08-02T12:00:00Z",
    )

    assert request.workspace == workspace
    assert request.session_id == layout.session_id
    assert request.journal_path == layout.journal_path
    assert request.benefit_audit_path == layout.benefit_audit_path
    assert request.skill_store_root == layout.skill_store_root
    assert request.policy_store_root == layout.policy_store_root
    assert request.host_identity_digest == layout.host_identity_digest


def test_release_skill_layout_does_not_inherit_recreated_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    first = _open(tmp_path, workspace)
    workspace.rmdir()
    workspace.mkdir()

    second = _open(tmp_path, workspace)

    assert second.workspace_identity_digest != first.workspace_identity_digest
    assert second.session_root != first.session_root
    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="changed"):
        first.assert_current()


def test_release_skill_layout_rejects_symlink_state_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    alias = tmp_path / "state-alias"
    alias.symlink_to(real_state, target_is_directory=True)

    with pytest.raises(ReleaseSkillRuntimeLayoutError, match="unsafe"):
        open_release_skill_runtime_layout(
            state_root=alias,
            host_context_id="codex",
            native_session_id="session",
            workspace=workspace,
        )
