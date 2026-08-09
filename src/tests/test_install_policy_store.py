from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from ctx.core.install_policy_store import (
    MAX_POLICY_FILE_BYTES,
    default_install_policy_root,
    has_persisted_install_policy,
    hold_current_install_policy,
    load_current_install_policy,
    persist_install_policy,
)
from ctx.engine.installation import InstallConsentPolicy


def _root(tmp_path: Path) -> Path:
    return tmp_path / "policy-store"


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    path.chmod(0o600)


def test_default_root_is_user_local_and_not_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    root = default_install_policy_root()

    assert root == tmp_path / ".ctx" / "install-policy"
    assert not root.exists()


def test_missing_store_returns_safe_default_without_creating_files(tmp_path: Path) -> None:
    root = _root(tmp_path)

    assert load_current_install_policy(root) == InstallConsentPolicy.safe_default()
    assert not has_persisted_install_policy(root)
    assert not root.exists()

    with pytest.raises(TypeError, match="InstallConsentPolicy"):
        persist_install_policy({"skill_mode": "preapproved-auto"}, root)  # type: ignore[arg-type]


def test_persist_round_trips_canonical_policy_and_private_modes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    policy = InstallConsentPolicy(
        skill_mode="preapproved-auto",
        agent_mode="ask-each-time",
        mcp_server_mode="preapproved-auto",
    )

    digest = persist_install_policy(policy, root)

    assert digest == policy.policy_digest
    assert has_persisted_install_policy(root)
    assert load_current_install_policy(root) == policy
    expected = (
        json.dumps(
            policy.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    assert (root / "current.json").read_bytes() == expected
    assert (root / "snapshots" / f"{digest}.json").read_bytes() == expected
    if os.name != "nt":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "snapshots").stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "current.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((root / "snapshots" / f"{digest}.json").stat().st_mode) == 0o600


def test_changing_current_never_mutates_a_prior_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    first = InstallConsentPolicy(skill_mode="preapproved-auto")
    second = InstallConsentPolicy(agent_mode="preapproved-auto")
    persist_install_policy(first, root)
    snapshot = root / "snapshots" / f"{first.policy_digest}.json"
    before = snapshot.read_bytes()
    before_stat = snapshot.stat()

    persist_install_policy(second, root)

    assert load_current_install_policy(root) == second
    assert snapshot.read_bytes() == before
    assert snapshot.stat().st_ino == before_stat.st_ino
    assert (root / "snapshots" / f"{second.policy_digest}.json").is_file()


def test_policy_guard_serializes_current_change_and_rejects_stale_digest(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    automatic = InstallConsentPolicy(skill_mode="preapproved-auto")
    ask = InstallConsentPolicy.safe_default()
    persist_install_policy(automatic, root)
    started = threading.Event()
    finished = threading.Event()

    def change_policy() -> None:
        started.set()
        persist_install_policy(ask, root)
        finished.set()

    with hold_current_install_policy(automatic.policy_digest, root) as held:
        assert held.policy == automatic
        held.assert_current()
        thread = threading.Thread(target=change_policy)
        thread.start()
        assert started.wait(timeout=1)
        time.sleep(0.05)
        assert not finished.is_set()
    thread.join(timeout=1)
    assert finished.is_set()
    assert load_current_install_policy(root) == ask

    with pytest.raises(ValueError, match="changed"):
        with hold_current_install_policy(automatic.policy_digest, root):
            raise AssertionError("stale policy guard must not yield")


def test_policy_guard_detects_root_replacement_and_stable_lock_blocks_persist(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    moved = tmp_path / "moved-policy-store"
    automatic = InstallConsentPolicy(skill_mode="preapproved-auto")
    ask = InstallConsentPolicy.safe_default()
    persist_install_policy(automatic, root)
    started = threading.Event()
    finished = threading.Event()

    def replace_policy() -> None:
        started.set()
        persist_install_policy(ask, root)
        finished.set()

    with hold_current_install_policy(automatic.policy_digest, root) as held:
        root.rename(moved)
        thread = threading.Thread(target=replace_policy)
        thread.start()
        assert started.wait(timeout=1)
        time.sleep(0.05)
        assert not finished.is_set()
        with pytest.raises(ValueError, match="identity changed"):
            held.assert_current()
    thread.join(timeout=1)
    assert finished.is_set()
    assert load_current_install_policy(root) == ask


def test_repeated_persist_does_not_replace_existing_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    persist_install_policy(policy, root)
    snapshot = root / "snapshots" / f"{policy.policy_digest}.json"
    before = snapshot.stat()

    persist_install_policy(policy, root)

    assert snapshot.stat().st_ino == before.st_ino


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            b'{"agent_mode":"ask-each-time","agent_mode":"preapproved-auto"}\n',
            "duplicate",
        ),
        (b"\xff\xfe", "UTF-8"),
        (b"{" + b" " * MAX_POLICY_FILE_BYTES + b"}", "size"),
    ],
)
def test_load_rejects_duplicate_keys_invalid_utf8_and_oversize(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    root = _root(tmp_path)
    _write_private(root / "current.json", payload)
    (root / "snapshots").mkdir(mode=0o700)
    root.chmod(0o700)

    with pytest.raises(ValueError, match=message):
        load_current_install_policy(root)


def test_load_rejects_noncanonical_json_digest_tamper_and_schema_change(tmp_path: Path) -> None:
    root = _root(tmp_path)
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    persist_install_policy(policy, root)

    pretty = json.dumps(policy.to_dict(), indent=2).encode()
    _write_private(root / "current.json", pretty)
    with pytest.raises(ValueError, match="canonical"):
        load_current_install_policy(root)

    tampered = policy.to_dict()
    tampered["skill_mode"] = "ask-each-time"
    _write_private(
        root / "current.json",
        (json.dumps(tampered, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )
    with pytest.raises(ValueError, match="policy_digest"):
        load_current_install_policy(root)

    wrong_schema = policy.to_dict()
    wrong_schema["schema"] = "ctx.install-consent-policy-v999"
    _write_private(
        root / "current.json",
        (json.dumps(wrong_schema, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )
    with pytest.raises(ValueError, match="schema"):
        load_current_install_policy(root)


def test_current_requires_matching_content_addressed_snapshot(tmp_path: Path) -> None:
    root = _root(tmp_path)
    policy = InstallConsentPolicy(skill_mode="preapproved-auto")
    persist_install_policy(policy, root)
    snapshot = root / "snapshots" / f"{policy.policy_digest}.json"
    snapshot.unlink()

    with pytest.raises(ValueError, match="snapshot"):
        load_current_install_policy(root)


def test_load_rejects_symlink_nonregular_and_writable_files(tmp_path: Path) -> None:
    root = _root(tmp_path)
    policy = InstallConsentPolicy()
    persist_install_policy(policy, root)
    current = root / "current.json"
    target = root / "outside.json"
    target.write_bytes(current.read_bytes())
    target.chmod(0o600)
    current.unlink()
    current.symlink_to(target)
    with pytest.raises(ValueError, match="symlink|regular"):
        load_current_install_policy(root)

    current.unlink()
    current.mkdir()
    with pytest.raises(ValueError, match="regular"):
        load_current_install_policy(root)

    current.rmdir()
    _write_private(current, target.read_bytes())
    if os.name != "nt":
        current.chmod(0o620)
        with pytest.raises(ValueError, match="group or world writable"):
            load_current_install_policy(root)


def test_store_rejects_symlinked_or_writable_managed_directories(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        persist_install_policy(InstallConsentPolicy(), linked)
    with pytest.raises(ValueError, match="directory|symlink"):
        load_current_install_policy(linked)
    assert (tmp_path / ".linked.policy-current.lock").is_file()
    assert not (real / "current.json.lock").exists()

    root = _root(tmp_path)
    root.mkdir(mode=0o700)
    if os.name != "nt":
        root.chmod(0o770)
        with pytest.raises(ValueError, match="group or world writable"):
            persist_install_policy(InstallConsentPolicy(), root)


def test_store_rejects_symlinked_companion_lock_without_touching_target(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    persist_install_policy(InstallConsentPolicy(), root)
    lock_path = root.parent / f".{root.name}.policy-current.lock"
    lock_path.unlink()
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"unchanged")
    lock_path.symlink_to(outside)

    with pytest.raises(ValueError, match="lock file"):
        load_current_install_policy(root)

    assert outside.read_bytes() == b"unchanged"


def test_store_rejects_filesystem_root_and_relaxed_managed_modes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        persist_install_policy(InstallConsentPolicy(), Path(tmp_path.anchor))

    if os.name != "nt":
        root = _root(tmp_path)
        policy = InstallConsentPolicy()
        persist_install_policy(policy, root)
        (root / "current.json").chmod(0o640)
        with pytest.raises(ValueError, match="0600"):
            load_current_install_policy(root)

        (root / "current.json").chmod(0o600)
        (root / "snapshots").chmod(0o750)
        with pytest.raises(ValueError, match="0700"):
            load_current_install_policy(root)


def test_unknown_fields_cannot_persist_commands_credentials_or_paths(tmp_path: Path) -> None:
    root = _root(tmp_path)
    policy = InstallConsentPolicy()
    persist_install_policy(policy, root)
    raw = policy.to_dict()
    raw["command"] = "pip install package"
    _write_private(
        root / "current.json",
        (json.dumps(raw, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )

    with pytest.raises(ValueError, match="unknown fields"):
        load_current_install_policy(root)
    persisted = (root / "snapshots" / f"{policy.policy_digest}.json").read_text()
    assert "command" not in persisted
    assert "credential" not in persisted
    assert str(tmp_path) not in persisted
