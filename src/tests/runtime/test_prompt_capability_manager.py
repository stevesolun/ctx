from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ctx.core.install_policy_store import persist_install_policy
from ctx.core.install_consent_broker_store import (
    HumanDecisionVerifier,
    SQLiteInstallConsentBrokerStore,
    SignedHumanDecisionAssertion,
)
from ctx.engine.engine import CtxEngine
from ctx.engine.installation import InstallConsentPolicy
from ctx.engine.store import SQLiteEngineStore
from ctx.runtime.production_catalog import RELEASE_QUERY_CATALOG_ROOT_SHA256
from ctx.runtime.install_consent_broker import InstallConsentBrokerService
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_ID
from ctx.runtime.release_skill_dispatcher import ReleaseSkillDispatchResult
from ctx.runtime.release_skill_layout import (
    ReleaseSkillRuntimeLayout,
    open_release_skill_runtime_layout,
)


NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
TARGET_SHA256 = "c87c65b5b09f48e27c683fb5ada9d8bc377d6d72d7742ce7aac3c2d3d97ac441"
RELEVANT_PROMPT = "repair nested Python context manager state restoration"
BROKER_AUDIENCE = "ctx-managed-skill-consent-v1"
BROKER_KEY = b"manager-test-only-human-authenticator-key"


class _BrokerVerifier(HumanDecisionVerifier):
    def verify_signed_assertion(
        self,
        assertion: SignedHumanDecisionAssertion,
        *,
        signing_bytes: bytes,
    ) -> bool:
        return hmac.compare_digest(
            assertion.proof,
            hmac.digest(BROKER_KEY, signing_bytes, "sha256"),
        )


def _layout(tmp_path: Path) -> ReleaseSkillRuntimeLayout:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return open_release_skill_runtime_layout(
        state_root=tmp_path / "state",
        host_context_id="codex",
        native_session_id="native-session",
        workspace=workspace,
    )


def _broker(layout: ReleaseSkillRuntimeLayout) -> InstallConsentBrokerService:
    return InstallConsentBrokerService(
        store=SQLiteInstallConsentBrokerStore(
            layout.session_root / "install-consent-test.sqlite3",
            audience=BROKER_AUDIENCE,
        ),
        verifier=_BrokerVerifier(),
        workspace_identity_digest=layout.workspace_identity_digest,
        release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
        trusted_utc_now=lambda: NOW,
    )


def _signed_assertion(
    challenge_digest: str,
    *,
    decision: str,
) -> SignedHumanDecisionAssertion:
    unsigned = SignedHumanDecisionAssertion(
        challenge_digest=challenge_digest,
        decision=decision,
        principal_digest=hashlib.sha256(b"authenticated-human").hexdigest(),
        authenticator_id="test-passkey",
        audience=BROKER_AUDIENCE,
        nonce=f"manager-human-{decision}-nonce",
        issued_at="2026-08-02T12:20:00Z",
        expires_at="2026-08-02T12:45:00Z",
        proof=b"unsigned",
    )
    return replace(
        unsigned,
        proof=hmac.digest(BROKER_KEY, unsigned.signing_bytes(), "sha256"),
    )


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_safe_default_returns_consent_without_install_or_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager

    layout = _layout(tmp_path)
    activation_calls = 0

    def forbidden_activation(*_args: object, **_kwargs: object) -> object:
        nonlocal activation_calls
        activation_calls += 1
        raise AssertionError("ask-each-time must not activate")

    monkeypatch.setattr(manager, "activate_installed_release_skill", forbidden_activation)

    outcome = manager.reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )

    assert outcome.status == "consent-required"
    assert len(outcome.consent_directives) == 1
    directive = outcome.consent_directives[0]
    assert directive.capability_id == RELEASE_INSTALL_SKILL_ID
    assert directive.requires_prompt
    assert directive.recommendation_only
    assert not directive.resumable
    assert not outcome.availability.has_activated_release_skill
    assert activation_calls == 0
    assert not (layout.skill_store_root / TARGET_SHA256).exists()
    assert not layout.journal_path.exists()
    assert not layout.benefit_audit_path.exists()
    assert len(outcome.management_epoch_digest) == 64
    with pytest.raises(FrozenInstanceError):
        outcome.status = "failed"  # type: ignore[misc]


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_brokered_ask_creates_one_durable_challenge_without_installing(
    tmp_path: Path,
) -> None:
    from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities

    layout = _layout(tmp_path)
    broker = _broker(layout)

    first = reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        consent_broker=broker,
        trusted_utc_now=lambda: NOW,
    )
    repeated = reconcile_prompt_capabilities(
        layout=layout,
        task="write an unrelated JavaScript button label",
        language="JavaScript",
        consent_broker=broker,
        trusted_utc_now=lambda: NOW + timedelta(minutes=5),
    )

    assert first.status == repeated.status == "consent-required"
    assert first.consent_directives == repeated.consent_directives == ()
    assert len(first.consent_challenges) == 1
    assert repeated.consent_challenges == first.consent_challenges
    challenge = first.consent_challenges[0]
    assert challenge.capability_id == RELEASE_INSTALL_SKILL_ID
    assert challenge.audience == BROKER_AUDIENCE
    assert challenge.release_root_digest == RELEASE_QUERY_CATALOG_ROOT_SHA256
    assert layout.journal_path.is_file()
    assert not (layout.skill_store_root / TARGET_SHA256).exists()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
@pytest.mark.parametrize("decision", ("granted", "denied"))
def test_brokered_signed_continuation_resolves_without_using_new_prompt_as_authority(
    tmp_path: Path,
    decision: str,
) -> None:
    from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities

    layout = _layout(tmp_path)
    broker = _broker(layout)
    prepared = reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        consent_broker=broker,
        trusted_utc_now=lambda: NOW,
    )
    assert len(prepared.consent_challenges) == 1
    challenge = prepared.consent_challenges[0]

    resolved = reconcile_prompt_capabilities(
        layout=layout,
        task="write an unrelated JavaScript button label",
        language="JavaScript",
        consent_broker=broker,
        decision_assertion=_signed_assertion(
            challenge.challenge_digest,
            decision=decision,
        ),
        trusted_utc_now=lambda: NOW + timedelta(minutes=5),
    )

    assert resolved.consent_directives == ()
    assert resolved.consent_challenges == ()
    assert resolved.status == ("available" if decision == "granted" else "denied")
    assert (layout.skill_store_root / TARGET_SHA256).exists() is (decision == "granted")
    broker_record = broker.status(challenge.challenge_id)
    assert broker_record.state == "settled"
    assert broker_record.decision == decision


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_persisted_auto_installs_activates_and_reopens_verified_availability(
    tmp_path: Path,
) -> None:
    from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )
    trusted_clock_calls = 0

    def trusted_clock() -> datetime:
        nonlocal trusted_clock_calls
        trusted_clock_calls += 1
        return NOW

    outcome = reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=trusted_clock,
    )

    assert outcome.status == "available"
    assert outcome.consent_directives == ()
    assert outcome.failure_code is None
    assert outcome.availability.has_activated_release_skill
    assert trusted_clock_calls == 1
    installed = layout.skill_store_root / TARGET_SHA256
    assert installed.is_file()
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == TARGET_SHA256


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_existing_active_skill_is_inspected_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )
    first = manager.reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )
    assert first.status == "available"

    def forbidden_dispatch(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("active availability must bypass installation dispatch")

    monkeypatch.setattr(manager, "dispatch_release_skill_install", forbidden_dispatch)

    second = manager.reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )

    assert second.status == "available"
    assert second.availability.has_activated_release_skill
    assert second.consent_directives == ()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_irrelevant_prompt_never_physically_installs(
    tmp_path: Path,
) -> None:
    from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )

    outcome = reconcile_prompt_capabilities(
        layout=layout,
        task="write a JavaScript button label",
        language="JavaScript",
        trusted_utc_now=lambda: NOW,
    )

    assert outcome.status == "abstained"
    assert outcome.consent_directives == ()
    assert not outcome.availability.has_activated_release_skill
    assert not (layout.skill_store_root / TARGET_SHA256).exists()
    assert not layout.journal_path.exists()
    assert not layout.benefit_audit_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_irrelevant_then_relevant_auto_prompt_uses_fresh_canonical_stream(
    tmp_path: Path,
) -> None:
    from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )

    irrelevant = reconcile_prompt_capabilities(
        layout=layout,
        task="write a JavaScript button label",
        language="JavaScript",
        trusted_utc_now=lambda: NOW,
    )
    assert irrelevant.status == "abstained"
    assert not layout.journal_path.exists()
    assert not layout.benefit_audit_path.exists()

    relevant = reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )

    assert relevant.status == "available"
    assert relevant.availability.has_activated_release_skill
    assert (layout.skill_store_root / TARGET_SHA256).is_file()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_auto_install_supports_protocol_canonical_subsecond_clock(
    tmp_path: Path,
) -> None:
    from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )
    lossy_now = NOW.replace(microsecond=123450)

    outcome = reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: lossy_now,
    )

    assert outcome.status == "available"
    assert outcome.availability.has_activated_release_skill


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_repeated_ask_prompts_are_nonresumable_and_do_not_poison_management_stream(
    tmp_path: Path,
) -> None:
    from ctx.runtime.prompt_capability_manager import reconcile_prompt_capabilities

    layout = _layout(tmp_path)

    first = reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )
    first_files = tuple(
        sorted(
            path.relative_to(layout.state_root)
            for path in layout.state_root.rglob("*")
            if path.is_file()
        )
    )
    second = reconcile_prompt_capabilities(
        layout=layout,
        task="fix Python state restoration for nested context managers",
        language="Python",
        trusted_utc_now=lambda: NOW,
    )

    assert first.status == second.status == "consent-required"
    for outcome in (first, second):
        assert len(outcome.consent_directives) == 1
        directive = outcome.consent_directives[0]
        assert directive.recommendation_only
        assert not directive.resumable
        assert not hasattr(directive, "requested_action_id")
    assert not layout.journal_path.exists()
    assert not layout.benefit_audit_path.exists()
    assert not (layout.skill_store_root / TARGET_SHA256).exists()
    durable = tuple(path for path in layout.state_root.rglob("*") if path.is_file())
    assert tuple(sorted(path.relative_to(layout.state_root) for path in durable)) == first_files
    assert set(first_files) == {
        Path("managed-capabilities-v1/.install-policy-v1.policy-current.lock"),
        Path(
            "managed-capabilities-v1/prompt-reconciliation-locks-v1/"
            f"workspace-{layout.workspace_identity_digest}.reconcile.lock"
        ),
    }
    for path in durable:
        body = path.read_bytes()
        assert RELEVANT_PROMPT.encode("utf-8") not in body
        assert os.fsencode(layout._workspace) not in body  # noqa: SLF001


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_workspace_reconciliation_serializes_codex_and_claude_first_prompts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    layouts = tuple(
        open_release_skill_runtime_layout(
            state_root=state_root,
            host_context_id=host,
            native_session_id=f"{host}-session",
            workspace=workspace,
        )
        for host in ("codex", "claude-code")
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layouts[0].policy_store_root,
    )
    original_dispatch = manager.dispatch_release_skill_install
    count_lock = threading.Lock()
    active_dispatches = 0
    maximum_dispatches = 0

    def observed_dispatch(*args: object, **kwargs: object) -> ReleaseSkillDispatchResult:
        nonlocal active_dispatches, maximum_dispatches
        with count_lock:
            active_dispatches += 1
            maximum_dispatches = max(maximum_dispatches, active_dispatches)
        try:
            time.sleep(0.1)
            return original_dispatch(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            with count_lock:
                active_dispatches -= 1

    monkeypatch.setattr(manager, "dispatch_release_skill_install", observed_dispatch)

    def reconcile(layout: ReleaseSkillRuntimeLayout):
        return manager.reconcile_prompt_capabilities(
            layout=layout,
            task=RELEVANT_PROMPT,
            language="Python",
            trusted_utc_now=lambda: NOW,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reconcile, layouts))

    assert tuple(outcome.status for outcome in outcomes) == ("available", "available")
    assert all(outcome.availability.has_activated_release_skill for outcome in outcomes)
    assert maximum_dispatches == 1
    installed = layouts[0].skill_store_root / TARGET_SHA256
    assert installed.is_file()
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == TARGET_SHA256


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
@pytest.mark.parametrize("install_status", ("failed", "indeterminate"))
def test_failed_or_indeterminate_install_never_exposes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    install_status: str,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )
    activation_calls = 0

    def failed_dispatch(*_args: object, **_kwargs: object) -> ReleaseSkillDispatchResult:
        return ReleaseSkillDispatchResult(
            status=install_status,  # type: ignore[arg-type]
            capability_id=RELEASE_INSTALL_SKILL_ID,
            release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
        )

    def forbidden_activation(*_args: object, **_kwargs: object) -> object:
        nonlocal activation_calls
        activation_calls += 1
        raise AssertionError("unsettled install must not activate")

    monkeypatch.setattr(manager, "dispatch_release_skill_install", failed_dispatch)
    monkeypatch.setattr(manager, "activate_installed_release_skill", forbidden_activation)

    outcome = manager.reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )

    assert outcome.status == "failed"
    assert outcome.failure_code == f"install-{install_status}"
    assert outcome.consent_directives == ()
    assert not outcome.availability.has_activated_release_skill
    assert activation_calls == 0


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_activation_failure_returns_sanitized_closed_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )

    def installed_dispatch(*_args: object, **_kwargs: object) -> ReleaseSkillDispatchResult:
        return ReleaseSkillDispatchResult(
            status="installed",
            capability_id=RELEASE_INSTALL_SKILL_ID,
            release_root_digest=RELEASE_QUERY_CATALOG_ROOT_SHA256,
            install_action_content_digest="1" * 64,
            install_receipt_content_digest="2" * 64,
            installed_lineage_digest="3" * 64,
            activation_action_content_digest="4" * 64,
        )

    def failed_activation(*_args: object, **_kwargs: object) -> object:
        raise OSError(f"sensitive path: {layout.skill_store_root}")

    monkeypatch.setattr(manager, "dispatch_release_skill_install", installed_dispatch)
    monkeypatch.setattr(manager, "activate_installed_release_skill", failed_activation)

    outcome = manager.reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )

    assert outcome.status == "failed"
    assert outcome.failure_code == "activation-failed"
    assert str(layout.skill_store_root) not in repr(outcome)
    assert not outcome.availability.has_activated_release_skill


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
@pytest.mark.parametrize("boundary", ("before", "after"))
def test_reconciliation_recovers_an_activation_interruption_without_reinstalling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )
    original_activate = manager.activate_installed_release_skill
    calls = 0

    def interrupt_once(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            if boundary == "after":
                original_activate(*args, **kwargs)  # type: ignore[arg-type]
            raise OSError("injected activation interruption")
        return original_activate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(manager, "activate_installed_release_skill", interrupt_once)

    first = manager.reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )
    second = manager.reconcile_prompt_capabilities(
        layout=layout,
        task="fix Python state restoration for nested context managers",
        language="Python",
        trusted_utc_now=lambda: NOW + timedelta(minutes=5),
    )

    assert first.status == "failed"
    assert first.failure_code == "activation-failed"
    assert second.status == "available"
    assert second.availability.has_activated_release_skill
    installed = layout.skill_store_root / TARGET_SHA256
    assert installed.is_file()
    assert tuple(layout.skill_store_root.glob(TARGET_SHA256)) == (installed,)


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
@pytest.mark.parametrize("boundary", ("decision", "outcome", "receipt"))
def test_reconciliation_recovers_durable_install_before_probing_a_new_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    from ctx.runtime import prompt_capability_manager as manager
    from ctx.runtime import release_material

    layout = _layout(tmp_path)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        layout.policy_store_root,
    )
    original_load = release_material.ReleasePinnedSkillMaterialSource.load
    material_loads = 0

    def counted_load(
        source: object,
        driver_request: object,
        material: object,
        install_body: bytes,
    ) -> str:
        nonlocal material_loads
        material_loads += 1
        return original_load(source, driver_request, material, install_body)  # type: ignore[arg-type]

    monkeypatch.setattr(
        release_material.ReleasePinnedSkillMaterialSource,
        "load",
        counted_load,
    )
    original_process = CtxEngine.process
    original_outcome = SQLiteEngineStore.record_install_outcome
    original_receipt = CtxEngine.process_install_receipt

    if boundary == "decision":

        def interrupt_after_decision(engine: CtxEngine, event: object):
            transition = original_process(engine, event)  # type: ignore[arg-type]
            if event.kind == "UserDecision":  # type: ignore[attr-defined]
                raise RuntimeError("injected interruption after durable decision")
            return transition

        monkeypatch.setattr(CtxEngine, "process", interrupt_after_decision)
    elif boundary == "outcome":

        def interrupt_after_outcome(
            store: SQLiteEngineStore,
            *args: object,
            **kwargs: object,
        ):
            original_outcome(store, *args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected interruption after durable outcome")

        monkeypatch.setattr(
            SQLiteEngineStore,
            "record_install_outcome",
            interrupt_after_outcome,
        )
    else:

        def interrupt_after_receipt(
            engine: CtxEngine,
            *args: object,
            **kwargs: object,
        ):
            original_receipt(engine, *args, **kwargs)  # type: ignore[arg-type]
            raise RuntimeError("injected interruption after durable receipt")

        monkeypatch.setattr(
            CtxEngine,
            "process_install_receipt",
            interrupt_after_receipt,
        )

    first = manager.reconcile_prompt_capabilities(
        layout=layout,
        task=RELEVANT_PROMPT,
        language="Python",
        trusted_utc_now=lambda: NOW,
    )
    assert first.status == "failed"
    assert first.failure_code == "dispatch-failed"

    monkeypatch.setattr(CtxEngine, "process", original_process)
    monkeypatch.setattr(SQLiteEngineStore, "record_install_outcome", original_outcome)
    monkeypatch.setattr(CtxEngine, "process_install_receipt", original_receipt)
    persist_install_policy(
        InstallConsentPolicy(skill_mode="ask-each-time"),
        layout.policy_store_root,
    )
    second = manager.reconcile_prompt_capabilities(
        layout=layout,
        task="write an unrelated JavaScript button label",
        language="JavaScript",
        trusted_utc_now=lambda: NOW + timedelta(minutes=5),
    )
    third = manager.reconcile_prompt_capabilities(
        layout=layout,
        task="rename an unrelated CSS class",
        language="CSS",
        trusted_utc_now=lambda: NOW + timedelta(minutes=10),
    )

    assert second.status == "available"
    assert second.availability.has_activated_release_skill
    assert third.status == "available"
    assert third.availability.has_activated_release_skill
    assert material_loads == 1
    installed = layout.skill_store_root / TARGET_SHA256
    assert tuple(layout.skill_store_root.glob(TARGET_SHA256)) == (installed,)
