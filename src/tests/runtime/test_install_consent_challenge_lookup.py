from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ctx.core.install_consent_broker_store as broker_store_module
from ctx.core.install_consent_broker_store import (
    ConsentBrokerChallengeNotFound,
    ConsentBrokerCorruption,
    InstallConsentChallenge,
    InstallConsentChallengeRecord,
    SQLiteInstallConsentBrokerStore,
)
from ctx.engine.protocol import ScopeRef
from ctx.runtime.install_consent_broker import InstallConsentBrokerService


NOW = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)
AUDIENCE = "ctx-install-consent-v1"
WORKSPACE_DIGEST = hashlib.sha256(b"canonical-workspace").hexdigest()
RELEASE_ROOT_DIGEST = hashlib.sha256(b"canonical-release-root").hexdigest()


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _challenge(challenge_id: str) -> InstallConsentChallenge:
    return InstallConsentChallenge(
        challenge_id=challenge_id,
        audience=AUDIENCE,
        workspace_identity_digest=WORKSPACE_DIGEST,
        scope=ScopeRef(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            repository_id="repository-1",
            session_id="session-1",
            exposure_id="exposure-1",
            host_context_id="host-1",
        ),
        capability_id=f"skill:{challenge_id}",
        kind="skill",
        source_digest=_digest(f"source:{challenge_id}"),
        catalog_snapshot_digest=_digest("catalog"),
        plan_id="plan-1",
        install_plan_digest=_digest(f"install-plan:{challenge_id}"),
        descriptor_digest=_digest(f"descriptor:{challenge_id}"),
        execution_binding_digest=_digest(f"binding:{challenge_id}"),
        selection_digest=_digest(f"selection:{challenge_id}"),
        material_identity_digest=_digest(f"material:{challenge_id}"),
        requested_action_id=f"action-{challenge_id}",
        requested_action_kind="InstallCapability",
        requested_action_content_digest=_digest(f"action:{challenge_id}"),
        requested_action_precondition_revision=1,
        policy_snapshot_digest=_digest("policy"),
        release_root_digest=RELEASE_ROOT_DIGEST,
        permission_expansion=False,
        credential_requirement=False,
        expires_at="2026-08-01T13:00:00Z",
    )


def _store(tmp_path: Path) -> SQLiteInstallConsentBrokerStore:
    return SQLiteInstallConsentBrokerStore(
        tmp_path / "broker" / "consent.sqlite3",
        audience=AUDIENCE,
    )


def _service(store: SQLiteInstallConsentBrokerStore) -> InstallConsentBrokerService:
    return InstallConsentBrokerService(
        store=store,
        verifier=None,
        workspace_identity_digest=WORKSPACE_DIGEST,
        release_root_digest=RELEASE_ROOT_DIGEST,
        trusted_utc_now=lambda: NOW,
    )


def _lookup(
    store: SQLiteInstallConsentBrokerStore,
    challenge_digest: str,
    *,
    now: datetime = NOW,
) -> InstallConsentChallengeRecord:
    return store.get_by_challenge_digest(
        challenge_digest,
        expected_workspace_identity_digest=WORKSPACE_DIGEST,
        expected_release_root_digest=RELEASE_ROOT_DIGEST,
        now=now,
    )


def test_digest_lookup_returns_only_the_exact_privacy_safe_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    wanted = _challenge("consent-wanted")
    other = _challenge("consent-other")
    store.create_challenge(wanted, now=NOW)
    store.create_challenge(other, now=NOW)

    record = _lookup(store, wanted.challenge_digest)
    service_record = _service(store).status_by_challenge_digest(wanted.challenge_digest)

    assert type(record) is InstallConsentChallengeRecord
    assert record == service_record
    assert record.challenge == wanted
    assert record.state == "pending"
    assert str(store.path) not in repr(record)
    assert "proof" not in repr(record).lower()


def test_digest_lookup_has_a_typed_exact_not_found_result(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create_challenge(_challenge("consent-existing"), now=NOW)

    with pytest.raises(ConsentBrokerChallengeNotFound, match="challenge digest"):
        _lookup(store, _digest("absent"))


def test_digest_lookup_authenticates_every_row_before_comparison(tmp_path: Path) -> None:
    store = _store(tmp_path)
    wanted = _challenge("consent-a-wanted")
    unrelated = _challenge("consent-z-corrupt")
    store.create_challenge(wanted, now=NOW)
    store.create_challenge(unrelated, now=NOW)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE consent_challenges SET record_digest = ? WHERE challenge_id = ?",
            (_digest("forged-record"), unrelated.challenge_id),
        )

    with pytest.raises(ConsentBrokerCorruption, match="record digest"):
        _lookup(store, wanted.challenge_digest)


def test_digest_lookup_fails_closed_on_an_authenticated_digest_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision_digest = "f" * 64
    monkeypatch.setattr(
        broker_store_module,
        "_canonical_digest",
        lambda _value: collision_digest,
    )
    store = _store(tmp_path)
    store.create_challenge(_challenge("consent-collision-1"), now=NOW)
    store.create_challenge(_challenge("consent-collision-2"), now=NOW)

    with pytest.raises(ConsentBrokerCorruption, match="multiple authenticated challenges"):
        _lookup(store, collision_digest)


def test_digest_lookup_fails_closed_when_the_absolute_scan_bound_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    first = _challenge("consent-bound-1")
    store.create_challenge(first, now=NOW)
    store.create_challenge(_challenge("consent-bound-2"), now=NOW)
    monkeypatch.setattr(broker_store_module, "_MAX_CAPACITY", 1)

    with pytest.raises(ConsentBrokerCorruption, match="absolute bound"):
        _lookup(store, first.challenge_digest)


def test_foreign_service_cannot_enumerate_or_expire_a_challenge(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge("consent-foreign-service")
    store.create_challenge(challenge, now=NOW)
    foreign = InstallConsentBrokerService(
        store=store,
        verifier=None,
        workspace_identity_digest=_digest("foreign-workspace"),
        release_root_digest=_digest("foreign-release"),
        trusted_utc_now=lambda: datetime(2026, 8, 1, 14, 0, tzinfo=UTC),
    )

    with pytest.raises(ConsentBrokerChallengeNotFound, match="challenge digest"):
        foreign.status_by_challenge_digest(challenge.challenge_digest)

    assert _lookup(store, challenge.challenge_digest).state == "pending"


@pytest.mark.skipif(os.name == "nt", reason="POSIX abrupt-exit assertion")
def test_digest_lookup_survives_cross_process_reopen_and_abrupt_exit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    challenge = _challenge("consent-cross-process")
    store.create_challenge(challenge, now=NOW)
    script = "\n".join(
        (
            "import os, sys",
            "from datetime import datetime",
            "from pathlib import Path",
            "from ctx.core.install_consent_broker_store import SQLiteInstallConsentBrokerStore",
            "store = SQLiteInstallConsentBrokerStore(Path(sys.argv[1]), audience=sys.argv[2])",
            "record = store.get_by_challenge_digest(",
            "    sys.argv[3],",
            "    expected_workspace_identity_digest=sys.argv[4],",
            "    expected_release_root_digest=sys.argv[5],",
            "    now=datetime.fromisoformat(sys.argv[6]),",
            ")",
            "assert record.challenge.challenge_id == sys.argv[7]",
            "os._exit(23)",
        )
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store.path),
            AUDIENCE,
            challenge.challenge_digest,
            WORKSPACE_DIGEST,
            RELEASE_ROOT_DIGEST,
            NOW.isoformat(),
            challenge.challenge_id,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 23, completed.stderr
    reopened = SQLiteInstallConsentBrokerStore(store.path, audience=AUDIENCE)
    assert _lookup(reopened, challenge.challenge_digest).challenge == challenge
