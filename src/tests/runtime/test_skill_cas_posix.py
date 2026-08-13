from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import ctx.runtime._skill_cas_posix as cas_posix
import ctx.runtime.agent_file as agent_file
from ctx.runtime._skill_cas_posix import (
    FileIdentity,
    SkillCasFilesystemConflict,
    SkillCasFilesystemUnsupported,
    open_skill_cas_directory,
    skill_cas_root_identity,
)
from tests.runtime.test_skill_cas import BODY, _claim_without_execution, _runtime


_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="descriptor-relative skill CAS primitives are POSIX-only",
)


def _private_root(tmp_path: Path) -> Path:
    root = tmp_path / "skill-cas"
    root.mkdir(mode=0o700)
    if os.name != "nt":
        root.chmod(0o700)
    return root


def _write_private(path: Path, body: bytes) -> None:
    path.write_bytes(body)
    if os.name != "nt":
        path.chmod(0o600)


@_POSIX_ONLY
def test_pinned_root_rename_never_touches_replacement_and_revalidation_fails(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    expected = skill_cas_root_identity(root)
    body = BODY.encode("utf-8")

    with open_skill_cas_directory(expected) as directory:
        displaced = tmp_path / "displaced-skill-cas"
        root.rename(displaced)
        root.mkdir(mode=0o700)
        root.chmod(0o700)

        directory.create_exact("pinned-write", body)

        assert (displaced / "pinned-write").read_bytes() == body
        assert not (root / "pinned-write").exists()
        with pytest.raises(
            SkillCasFilesystemConflict,
            match="path no longer names the pinned root",
        ):
            directory.revalidate_root()


@_POSIX_ONLY
def test_inspection_rejects_post_read_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_root(tmp_path)
    name = "authenticated-child"
    body = BODY.encode("utf-8")
    _write_private(root / name, body)
    expected = skill_cas_root_identity(root)
    original_stat = os.stat
    matching_stat_calls = 0

    with open_skill_cas_directory(expected) as directory:

        def swap_on_post_read_stat(
            path: str | bytes | int | Path,
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            nonlocal matching_stat_calls
            if path == name and dir_fd is not None and follow_symlinks is False:
                matching_stat_calls += 1
                if matching_stat_calls == 2:
                    os.unlink(name, dir_fd=dir_fd)
                    replacement = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    try:
                        os.write(replacement, body)
                        os.fchmod(replacement, 0o600)
                    finally:
                        os.close(replacement)
            return original_stat(
                path,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )

        with monkeypatch.context() as scoped:
            scoped.setattr(cas_posix.os, "stat", swap_on_post_read_stat)
            inspection = directory.inspect_exact_utf8(
                name,
                expected_sha256=hashlib.sha256(body).hexdigest(),
                expected_bytes=len(body),
                allowed_links=frozenset({1}),
            )

    assert matching_stat_calls == 2
    assert inspection.state == "conflict"
    assert (root / name).read_bytes() == body


@_POSIX_ONLY
def test_authenticated_read_returns_exact_bytes_and_rejects_digest_mismatch(
    tmp_path: Path,
) -> None:
    root = _private_root(tmp_path)
    name = "authenticated-content"
    body = BODY.encode("utf-8")
    _write_private(root / name, body)
    expected = skill_cas_root_identity(root)

    with open_skill_cas_directory(expected) as directory:
        assert (
            directory.read_exact_utf8_bytes(
                name,
                expected_sha256=hashlib.sha256(body).hexdigest(),
                expected_bytes=len(body),
                allowed_links=frozenset({1}),
            )
            == body
        )
        with pytest.raises(SkillCasFilesystemConflict, match="not exact"):
            directory.read_exact_utf8_bytes(
                name,
                expected_sha256=hashlib.sha256(b"different").hexdigest(),
                expected_bytes=len(body),
                allowed_links=frozenset({1}),
            )


@_POSIX_ONLY
def test_authenticated_read_rejects_post_read_identity_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _private_root(tmp_path)
    name = "swapped-content"
    body = BODY.encode("utf-8")
    _write_private(root / name, body)
    expected = skill_cas_root_identity(root)
    original_stat = os.stat
    matching_stat_calls = 0

    with open_skill_cas_directory(expected) as directory:

        def swap_on_post_read_stat(
            path: str | bytes | int | Path,
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            nonlocal matching_stat_calls
            if path == name and dir_fd is not None and follow_symlinks is False:
                matching_stat_calls += 1
                if matching_stat_calls == 2:
                    os.unlink(name, dir_fd=dir_fd)
                    replacement = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=dir_fd,
                    )
                    try:
                        os.write(replacement, body)
                        os.fchmod(replacement, 0o600)
                    finally:
                        os.close(replacement)
            return original_stat(
                path,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )

        with monkeypatch.context() as scoped:
            scoped.setattr(cas_posix.os, "stat", swap_on_post_read_stat)
            with pytest.raises(SkillCasFilesystemConflict, match="not exact"):
                directory.read_exact_utf8_bytes(
                    name,
                    expected_sha256=hashlib.sha256(body).hexdigest(),
                    expected_bytes=len(body),
                    allowed_links=frozenset({1}),
                )

    assert matching_stat_calls == 2
    assert (root / name).read_bytes() == body


@_POSIX_ONLY
def test_identity_guarded_unlink_refuses_swapped_inode(tmp_path: Path) -> None:
    root = _private_root(tmp_path)
    name = "cleanup-candidate"
    body = BODY.encode("utf-8")
    expected = skill_cas_root_identity(root)

    with open_skill_cas_directory(expected) as directory:
        original_identity = directory.create_exact(name, body)
        (root / name).rename(root / "parked-original")
        _write_private(root / name, body)
        replacement_identity = FileIdentity.from_stat((root / name).stat(follow_symlinks=False))
        assert replacement_identity != original_identity

        with pytest.raises(
            SkillCasFilesystemConflict,
            match="child changed before cleanup",
        ):
            directory.unlink_child_if_identity(
                name,
                expected=original_identity,
                allowed_links=frozenset({1}),
            )

        assert (root / name).read_bytes() == body


@_POSIX_ONLY
def test_driver_rejects_unaccounted_hardlink_to_exact_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    target = skill_root / material.content_sha256
    _write_private(target, BODY.encode("utf-8"))
    os.link(target, skill_root / "unaccounted-alias")

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert not engine.install_execution_status(action).outcome_recorded
    assert target.stat(follow_symlinks=False).st_nlink == 2


@_POSIX_ONLY
def test_recovery_chooses_one_of_multiple_exact_cross_action_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    _claim_without_execution(engine, action, skill_root)
    stages = [
        skill_root / f".ctx-skill-{material.content_sha256}-{'1' * 64}.pending",
        skill_root / f".ctx-skill-{material.content_sha256}-{'2' * 64}.pending",
    ]
    for stage in stages:
        _write_private(stage, BODY.encode("utf-8"))

    report = handle.execute()

    assert report.outcome == "applied"
    assert report.settled
    assert source.load_calls == 0
    assert (skill_root / material.content_sha256).read_text(encoding="utf-8") == BODY
    assert all(not stage.exists() for stage in stages)


@_POSIX_ONLY
def test_recovery_accounts_for_foreign_action_stage_linked_to_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    _claim_without_execution(engine, action, skill_root)
    stage = skill_root / f".ctx-skill-{material.content_sha256}-{'3' * 64}.pending"
    target = skill_root / material.content_sha256
    _write_private(stage, BODY.encode("utf-8"))
    os.link(stage, target)

    report = handle.execute()

    assert report.outcome == "applied"
    assert report.settled
    assert source.load_calls == 0
    assert target.stat(follow_symlinks=False).st_nlink == 1
    assert not stage.exists()


@_POSIX_ONLY
def test_too_many_recovery_stages_fail_closed_without_body_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    for index in range(65):
        stage = skill_root / (f".ctx-skill-{material.content_sha256}-{index:064x}.pending")
        _write_private(stage, BODY.encode("utf-8"))

    report = handle.execute()

    assert report.outcome == "indeterminate"
    assert not report.settled
    assert source.load_calls == 0
    assert not engine.install_execution_status(action).outcome_recorded


@_POSIX_ONLY
def test_unprotected_ancestor_is_rejected(tmp_path: Path) -> None:
    unsafe_parent = tmp_path / "public-parent"
    unsafe_parent.mkdir(mode=0o700)
    unsafe_parent.chmod(0o777)
    root = unsafe_parent / "skill-cas"
    root.mkdir(mode=0o700)
    root.chmod(0o700)

    with pytest.raises(SkillCasFilesystemConflict, match="unsafe rename"):
        skill_cas_root_identity(root)


@_POSIX_ONLY
def test_true_publication_collision_reconciles_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, action, material, _source, skill_root, handle = _runtime(tmp_path, monkeypatch)
    original_link = cas_posix.PinnedSkillCasDirectory.link_child_exclusive
    injected = False

    def collide_once(
        directory: cas_posix.PinnedSkillCasDirectory,
        source: str,
        destination: str,
        *,
        expected_source: FileIdentity,
    ) -> bool:
        nonlocal injected
        if not injected:
            injected = True
            os.link(skill_root / source, skill_root / destination, follow_symlinks=False)
            return False
        return original_link(
            directory,
            source,
            destination,
            expected_source=expected_source,
        )

    monkeypatch.setattr(
        cas_posix.PinnedSkillCasDirectory,
        "link_child_exclusive",
        collide_once,
    )

    report = handle.execute()

    target = skill_root / material.content_sha256
    assert injected
    assert report.outcome == "applied"
    assert report.settled
    assert target.read_text(encoding="utf-8") == BODY
    assert target.stat(follow_symlinks=False).st_nlink == 1


def test_windows_platform_fails_closed_before_filesystem_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_secure_directory(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Windows fail-closed check touched the filesystem")

    with monkeypatch.context() as scoped:
        scoped.setattr(cas_posix.os, "name", "nt")
        scoped.setattr(cas_posix, "secure_directory", unexpected_secure_directory)
        with pytest.raises(
            SkillCasFilesystemUnsupported,
            match="native Windows skill installation is not enabled",
        ):
            skill_cas_root_identity(tmp_path)


def test_agent_file_windows_platform_fails_closed_before_filesystem_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_secure_directory(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Windows fail-closed check touched the filesystem")

    with monkeypatch.context() as scoped:
        scoped.setattr(cas_posix.os, "name", "nt")
        scoped.setattr(cas_posix, "secure_directory", unexpected_secure_directory)
        with pytest.raises(agent_file.AgentFileError, match="unavailable or unsupported"):
            agent_file.agent_file_target_identity_digest(tmp_path)
