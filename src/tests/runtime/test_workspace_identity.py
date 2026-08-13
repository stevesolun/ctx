from __future__ import annotations

import copy
import os
import pickle
from pathlib import Path

import pytest

from ctx.runtime.query_decision import QueryHostDescriptor
from ctx.runtime.query_delivery import SensitiveQueryInput, _derive_invocation_ref
from ctx.runtime.query_session import _scope as query_scope
from ctx.runtime.release_skill_dispatcher import (
    ReleaseSkillInstallRequest,
    _scope as install_scope,
)
from ctx.runtime.workspace_identity import (
    WorkspaceIdentityError,
    capture_workspace_identity,
)


def test_workspace_identity_is_alias_stable_and_path_private(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = capture_workspace_identity(workspace)
    second = capture_workspace_identity(workspace / "child" / "..")

    assert first == second
    assert len(first.digest) == 64
    assert str(workspace) not in repr(first)
    with pytest.raises(TypeError):
        copy.copy(first)
    with pytest.raises(TypeError):
        pickle.dumps(first)


def test_workspace_identity_rejects_symlink_ancestry(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)

    with pytest.raises(WorkspaceIdentityError, match="symlinks"):
        capture_workspace_identity(alias)


def test_workspace_identity_detects_delete_and_recreate(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = capture_workspace_identity(workspace)

    _before_ino = workspace.stat().st_ino
    workspace.rmdir()
    workspace.mkdir()
    if workspace.stat().st_ino == _before_ino:
        pytest.skip("filesystem reused the freed inode; recreation is not observable via stat")

    with pytest.raises(WorkspaceIdentityError, match="changed"):
        identity.assert_current()
    assert capture_workspace_identity(workspace).digest != identity.digest


def test_workspace_identity_detects_rename(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = capture_workspace_identity(workspace)
    workspace.rename(tmp_path / "moved")

    with pytest.raises(WorkspaceIdentityError, match="changed"):
        identity.assert_current()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link semantics")
def test_workspace_identity_requires_a_directory(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_text("not a workspace", encoding="utf-8")

    with pytest.raises(WorkspaceIdentityError, match="directory"):
        capture_workspace_identity(regular)


def test_install_query_and_delivery_share_one_workspace_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = capture_workspace_identity(workspace)
    request = ReleaseSkillInstallRequest(
        host_context_id="codex",
        host_identity_digest="1" * 64,
        task="repair state restoration",
        language="Python",
        session_id="native-session",
        workspace=workspace,
        journal_path=tmp_path / "engine.sqlite3",
        benefit_audit_path=tmp_path / "benefit.sqlite3",
        policy_store_root=tmp_path / "policy",
        skill_store_root=tmp_path / "skills",
        occurred_at="2026-08-02T12:00:00Z",
    )
    host = QueryHostDescriptor.codex("activate")
    sensitive = SensitiveQueryInput(
        native_session_id=request.session_id,
        logical_prompt_id="turn-2",
        workspace=workspace,
        prompt=request.task,
        language=request.language,
    )

    assert install_scope(request).workspace_id == f"workspace-{identity.digest}"
    assert (
        query_scope(
            host=host,
            session_id=request.session_id,
            workspace=workspace,
        ).workspace_id
        == f"workspace-{identity.digest}"
    )
    assert (
        _derive_invocation_ref(
            key=b"k" * 32,
            host=host,
            request=sensitive,
        ).workspace_digest
        == identity.digest
    )
