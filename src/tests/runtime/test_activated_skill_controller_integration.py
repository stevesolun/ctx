from __future__ import annotations

import io
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
import ctx.runtime.query_delivery as query_delivery_module

from ctx.core.install_policy_store import persist_install_policy
from ctx.engine.installation import InstallConsentPolicy
from ctx.runtime.query_decision import QueryHostDescriptor
from ctx.runtime.query_delivery import QueryDeliveryController, SensitiveQueryInput
from ctx.runtime.release_skill_dispatcher import dispatch_release_skill_install
from ctx.runtime.release_skill_layout import open_release_skill_runtime_layout
from ctx.runtime.release_skill_layout import open_workspace_release_skill_runtime_layout
from ctx.runtime.release_skill_lifecycle import activate_installed_release_skill


NOW = datetime(2026, 8, 2, 12, 30, tzinfo=UTC)
OCCURRED_AT = "2026-08-02T12:00:00Z"
STATE_PROTOCOLS_SHA256 = "c87c65b5b09f48e27c683fb5ada9d8bc377d6d72d7742ce7aac3c2d3d97ac441"
FIRST_ASK_PROMPT = "repair nested Python context manager state restoration"
SECOND_ASK_PROMPT = "fix Python state restoration for nested context managers"


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_manage_first_prompt_auto_installs_loads_and_cross_host_reuses_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "shared-state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        policy_root,
    )
    outputs: list[bytes] = []

    for host, native_session_id, logical_prompt_id in (
        (QueryHostDescriptor.codex(), "codex-native", "codex-turn"),
        (QueryHostDescriptor.claude_code(), "claude-native", "claude-turn"),
    ):
        report = QueryDeliveryController(
            host=host,
            mode="manage",
            state_root=state_root,
            environment={"CTX_INSTALL_POLICY_ROOT": str(policy_root)},
        ).issue(
            SensitiveQueryInput(
                native_session_id=native_session_id,
                logical_prompt_id=logical_prompt_id,
                workspace=workspace,
                prompt="repair nested Python context manager state restoration",
                language="Python",
            )
        )

        assert report.status == "issued"
        assert report.emission_permit is not None
        output = io.BytesIO()
        report.emission_permit.emit_once(output)
        outputs.append(output.getvalue())

    assert all(b"# ctx Python State and Protocols" in output for output in outputs)
    installed = tuple(state_root.rglob(STATE_PROTOCOLS_SHA256))
    assert len(installed) == 1
    assert installed[0].is_file()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_concurrent_cross_host_manage_prompts_apply_one_workspace_install(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "shared-state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        policy_root,
    )

    def issue(item: tuple[QueryHostDescriptor, str, str]) -> bytes:
        host, native_session_id, logical_prompt_id = item
        report = QueryDeliveryController(
            host=host,
            mode="manage",
            state_root=state_root,
            environment={"CTX_INSTALL_POLICY_ROOT": str(policy_root)},
            # These two hosts contend for one lock by design. The production
            # default (2s) fails closed rather than stalling a prompt hook,
            # which is correct for a user but makes this assertion a race
            # against machine load. The subject here is that both hosts
            # converge on one install, not how fast a lock is acquired.
            lock_timeout_seconds=10.0,
        ).issue(
            SensitiveQueryInput(
                native_session_id=native_session_id,
                logical_prompt_id=logical_prompt_id,
                workspace=workspace,
                prompt="repair nested Python context manager state restoration",
                language="Python",
            )
        )
        assert report.status == "issued"
        assert report.emission_permit is not None
        output = io.BytesIO()
        report.emission_permit.emit_once(output)
        return output.getvalue()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = tuple(
            pool.map(
                issue,
                (
                    (QueryHostDescriptor.codex(), "codex-native", "codex-turn"),
                    (
                        QueryHostDescriptor.claude_code(),
                        "claude-native",
                        "claude-turn",
                    ),
                ),
            )
        )

    assert all(b"# ctx Python State and Protocols" in output for output in outputs)
    assert len(tuple(state_root.rglob(STATE_PROTOCOLS_SHA256))) == 1


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_activate_mode_never_auto_installs_from_persisted_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        policy_root,
    )

    report = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="activate",
        state_root=state_root,
        environment={"CTX_INSTALL_POLICY_ROOT": str(policy_root)},
    ).issue(
        SensitiveQueryInput(
            native_session_id="native-session",
            logical_prompt_id="relevant-turn",
            workspace=workspace,
            prompt="repair nested Python context manager state restoration",
            language="Python",
        )
    )

    assert report.status == "abstained"
    assert report.emission_permit is None
    assert not tuple(state_root.rglob(STATE_PROTOCOLS_SHA256))


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_manage_ask_each_time_emits_only_an_authority_free_recommendation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="ask-each-time"),
        policy_root,
    )

    report = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="manage",
        state_root=state_root,
        environment={"CTX_INSTALL_POLICY_ROOT": str(policy_root)},
    ).issue(
        SensitiveQueryInput(
            native_session_id="native-session",
            logical_prompt_id="consent-turn",
            workspace=workspace,
            prompt="repair nested Python context manager state restoration",
            language="Python",
        )
    )

    assert report.status == "issued"
    assert report.emission_permit is not None
    output = io.BytesIO()
    report.emission_permit.emit_once(output)
    rendered = output.getvalue()
    assert b"skill:ctx-python-state-protocols" in rendered
    assert b"approval=required" in rendered
    assert b"no installation performed" in rendered.lower()
    assert b"# ctx Python State and Protocols" not in rendered
    assert not tuple(state_root.rglob(STATE_PROTOCOLS_SHA256))


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_manage_irrelevant_then_relevant_prompt_uses_fresh_management_stream(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        policy_root,
    )
    layout = open_workspace_release_skill_runtime_layout(
        state_root=state_root,
        policy_store_root=policy_root,
        workspace=workspace,
    )
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="manage",
        state_root=state_root,
        environment={"CTX_INSTALL_POLICY_ROOT": str(policy_root)},
    )

    irrelevant = controller.issue(
        SensitiveQueryInput(
            native_session_id="native-session",
            logical_prompt_id="irrelevant-turn",
            workspace=workspace,
            prompt="write a JavaScript button label",
            language="JavaScript",
        )
    )

    assert irrelevant.status == "abstained"
    assert irrelevant.emission_permit is None
    assert not layout.journal_path.exists()
    assert not layout.benefit_audit_path.exists()
    assert not (layout.skill_store_root / STATE_PROTOCOLS_SHA256).exists()

    relevant = controller.issue(
        SensitiveQueryInput(
            native_session_id="native-session",
            logical_prompt_id="relevant-turn",
            workspace=workspace,
            prompt="repair nested Python context manager state restoration",
            language="Python",
        )
    )

    assert relevant.status == "issued"
    assert relevant.emission_permit is not None
    output = io.BytesIO()
    relevant.emission_permit.emit_once(output)
    assert b"# ctx Python State and Protocols" in output.getvalue()
    assert (layout.skill_store_root / STATE_PROTOCOLS_SHA256).is_file()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_manage_repeated_ask_prompts_reuse_one_authority_free_consent_challenge(
    tmp_path: Path,
) -> None:
    """Repeated ask-each-time prompts must accumulate no new authority.

    An earlier revision of this test asserted that no journal or benefit audit
    existed at all.  That contract is unsatisfiable together with the accepted
    ask-each-time design: `ManagedQueryService.resolve_consent` authenticates a
    signed human decision against a *durable* challenge record
    (`status_by_challenge_digest`), so a resumable ask cannot exist without one
    persisted challenge.  The property that actually protects the user is that
    an untrusted prompt hook creates that authority exactly once, reuses it for
    later prompts, never installs anything, never leaks consent authority into
    model-visible bytes, and never persists raw prompt text or absolute paths.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="ask-each-time"),
        policy_root,
    )
    layout = open_workspace_release_skill_runtime_layout(
        state_root=state_root,
        policy_store_root=policy_root,
        workspace=workspace,
    )
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="manage",
        state_root=state_root,
        environment={"CTX_INSTALL_POLICY_ROOT": str(policy_root)},
    )
    outputs: list[bytes] = []

    for logical_prompt_id, prompt in (
        ("first-turn", FIRST_ASK_PROMPT),
        ("second-turn", SECOND_ASK_PROMPT),
    ):
        report = controller.issue(
            SensitiveQueryInput(
                native_session_id="native-session",
                logical_prompt_id=logical_prompt_id,
                workspace=workspace,
                prompt=prompt,
                language="Python",
            )
        )
        assert report.status == "issued"
        assert report.emission_permit is not None
        output = io.BytesIO()
        report.emission_permit.emit_once(output)
        outputs.append(output.getvalue())

    assert all(b"approval=required" in output for output in outputs)
    assert all(b"no installation performed" in output.lower() for output in outputs)
    assert all(b"consent_id=" not in output for output in outputs)
    assert all(b"requested_action" not in output for output in outputs)
    # Authority is created exactly once and then reused: the second prompt
    # neither publishes a second challenge nor advances the journal.
    assert outputs[0] == outputs[1]
    assert layout.consent_broker_path is not None
    with sqlite3.connect(layout.consent_broker_path) as connection:
        assert connection.execute("SELECT count(*) FROM consent_challenges").fetchone() == (1,)
    with sqlite3.connect(layout.journal_path) as connection:
        journal_records = connection.execute("SELECT count(*) FROM engine_journal").fetchone()[0]
    assert journal_records == 3
    # Nothing is installed and no host material is published by an ask.
    assert not (layout.skill_store_root / STATE_PROTOCOLS_SHA256).exists()
    # No durable byte carries raw prompt text or an absolute workspace path.
    durable = b"".join(
        path.read_bytes()
        for path in sorted(state_root.rglob("*"))
        if path.is_file() and path.stat().st_size
    )
    for secret in (FIRST_ASK_PROMPT.encode(), SECOND_ASK_PROMPT.encode(), str(workspace).encode()):
        assert secret not in durable


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_manage_reconciliation_failure_is_not_committed_as_abstention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    policy_root = tmp_path / "onboarding-policy"
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        policy_root,
    )
    attempts = 0

    def failed_reconciliation(**_kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("private failure detail")

    monkeypatch.setattr(
        query_delivery_module,
        "reconcile_prompt_capabilities",
        failed_reconciliation,
    )
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="manage",
        state_root=state_root,
        environment={"CTX_INSTALL_POLICY_ROOT": str(policy_root)},
    )
    request = SensitiveQueryInput(
        native_session_id="native-session",
        logical_prompt_id="failure-turn",
        workspace=workspace,
        prompt="repair nested Python context manager state restoration",
        language="Python",
    )

    assert controller.issue(request).status == "failed"
    assert attempts == 3
    assert controller.issue(request).status == "failed"
    assert attempts == 6


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
@pytest.mark.parametrize(
    "host",
    (QueryHostDescriptor.codex(), QueryHostDescriptor.claude_code()),
)
def test_later_relevant_host_prompt_routes_activated_release_skill_from_cas(
    tmp_path: Path,
    host: QueryHostDescriptor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / f"{host.host_context_id}-state"
    native_session_id = f"{host.host_context_id}-native-session"
    layout = open_release_skill_runtime_layout(
        state_root=state_root,
        host_context_id=host.host_context_id,
        native_session_id=native_session_id,
        workspace=workspace,
    )
    request = layout.install_request(
        task="repair nested Python context manager state restoration",
        language="Python",
        occurred_at=OCCURRED_AT,
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    assert (
        dispatch_release_skill_install(
            request,
            trusted_utc_now=lambda: NOW,
        ).status
        == "installed"
    )
    activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)
    availability_opens = 0
    original_open = query_delivery_module.open_activated_skill_query_availability

    def observed_open(**kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal availability_opens
        availability_opens += 1
        return original_open(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        query_delivery_module,
        "open_activated_skill_query_availability",
        observed_open,
    )

    controller = QueryDeliveryController(
        host=host,
        mode="activate",
        state_root=state_root,
        environment={},
    )
    report = controller.issue(
        SensitiveQueryInput(
            native_session_id=native_session_id,
            logical_prompt_id="later-relevant-turn",
            workspace=workspace,
            prompt="repair nested Python context manager state restoration",
            language="Python",
        )
    )

    assert report.status == "issued"
    assert availability_opens == 1
    assert report.emission_permit is not None
    output = io.BytesIO()
    report.emission_permit.emit_once(output)
    assert b"# ctx Python State and Protocols" in output.getvalue()
    assert b"# ctx Python Testing" not in output.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_irrelevant_prompt_does_not_expose_activated_release_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    native_session_id = "native-session"
    layout = open_release_skill_runtime_layout(
        state_root=state_root,
        host_context_id="codex",
        native_session_id=native_session_id,
        workspace=workspace,
    )
    request = layout.install_request(
        task="repair nested Python context manager state restoration",
        language="Python",
        occurred_at=OCCURRED_AT,
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    assert (
        dispatch_release_skill_install(
            request,
            trusted_utc_now=lambda: NOW,
        ).status
        == "installed"
    )
    activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    report = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="activate",
        state_root=state_root,
        environment={},
    ).issue(
        SensitiveQueryInput(
            native_session_id=native_session_id,
            logical_prompt_id="irrelevant-turn",
            workspace=workspace,
            prompt="write a JavaScript button label",
            language="JavaScript",
        )
    )

    assert report.status == "abstained"
    assert report.emission_permit is None


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_activation_epoch_preserves_prompt_terminal_and_new_turn_replans(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    native_session_id = "native-session"
    prompt = "repair nested Python context manager state restoration"
    sensitive = SensitiveQueryInput(
        native_session_id=native_session_id,
        logical_prompt_id="same-logical-turn",
        workspace=workspace,
        prompt=prompt,
        language="Python",
    )
    before = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="activate",
        state_root=state_root,
        environment={},
    )

    assert before.issue(sensitive).status == "abstained"
    assert before.issue(sensitive).status == "already-terminal"

    layout = open_release_skill_runtime_layout(
        state_root=state_root,
        host_context_id="codex",
        native_session_id=native_session_id,
        workspace=workspace,
    )
    request = layout.install_request(
        task=prompt,
        language="Python",
        occurred_at=OCCURRED_AT,
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    assert (
        dispatch_release_skill_install(
            request,
            trusted_utc_now=lambda: NOW,
        ).status
        == "installed"
    )
    activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)

    after = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="activate",
        state_root=state_root,
        environment={},
    )
    assert after.issue(sensitive).status == "already-terminal"

    next_turn = SensitiveQueryInput(
        native_session_id=native_session_id,
        logical_prompt_id="next-logical-turn",
        workspace=workspace,
        prompt=prompt,
        language="Python",
    )
    reopened = after.issue(next_turn)

    assert reopened.status == "issued"
    assert after.issue(next_turn).status == "already-terminal"


@pytest.mark.skipif(os.name == "nt", reason="managed skill CAS is POSIX-only")
def test_tampered_activated_cas_fails_bundle_without_packaged_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    native_session_id = "native-session"
    task = "repair nested Python context manager state restoration and fix pytest tests"
    layout = open_release_skill_runtime_layout(
        state_root=state_root,
        host_context_id="codex",
        native_session_id=native_session_id,
        workspace=workspace,
    )
    request = layout.install_request(
        task=task,
        language="Python",
        occurred_at=OCCURRED_AT,
    )
    persist_install_policy(
        InstallConsentPolicy(skill_mode="preapproved-auto"),
        request.policy_store_root,
    )
    assert dispatch_release_skill_install(request, trusted_utc_now=lambda: NOW).status == (
        "installed"
    )
    activate_installed_release_skill(request, trusted_utc_now=lambda: NOW)
    target = next(
        path
        for path in request.skill_store_root.iterdir()
        if path.is_file() and len(path.name) == 64
    )
    original_open = query_delivery_module.open_activated_skill_query_availability

    def open_then_tamper(**kwargs: object):  # type: ignore[no-untyped-def]
        availability = original_open(**kwargs)  # type: ignore[arg-type]
        target.write_bytes(b"x" * target.stat().st_size)
        return availability

    monkeypatch.setattr(
        query_delivery_module,
        "open_activated_skill_query_availability",
        open_then_tamper,
    )

    report = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="activate",
        state_root=state_root,
        environment={},
    ).issue(
        SensitiveQueryInput(
            native_session_id=native_session_id,
            logical_prompt_id="tampered-cas-turn",
            workspace=workspace,
            prompt=task,
            language="Python",
        )
    )

    assert report.status == "failed"
    assert report.emission_permit is None
