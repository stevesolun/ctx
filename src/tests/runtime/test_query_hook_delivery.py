from __future__ import annotations

import copy
import hashlib
import hmac
import io
import json
import multiprocessing
import os
import pickle
import sqlite3
import sys
import tempfile
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import ctx.runtime._query_attempt_posix as attempt_storage
import ctx.runtime.query_delivery as query_delivery_runtime
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import (
    BenefitAuditReference,
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    ManualPlanningAuthority,
)
from ctx.engine.protocol import HostAction, ScopeRef, Transition
from ctx.engine.state import CommittedPlanV3, PlanCapabilityV3
from ctx.runtime.query_decision import (
    CommittedQueryDecision,
    QueryDecisionFailure,
    QueryHostDescriptor,
    _commit_query_decision,
    prepare_query_decision,
)
from ctx.runtime.query_delivery import (
    QueryDeliveryCorruption,
    QueryDeliveryController,
    SensitiveQueryInput,
    _SQLiteQueryDeliveryLedger,
)


pytestmark = pytest.mark.skipif(
    not attempt_storage.query_attempt_pool_supported(),
    reason="POSIX descriptor-relative query attempt contracts",
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _attempt_children(state_root: Path) -> tuple[Path, ...]:
    root = state_root / "query-delivery-attempts-v1"
    return () if not root.exists() else tuple(root.iterdir())


def _request(tmp_path: Path, *, logical_prompt_id: str = "turn-1") -> SensitiveQueryInput:
    workspace = tmp_path / "private-client-repository"
    workspace.mkdir(parents=True, exist_ok=True)
    return SensitiveQueryInput(
        native_session_id="session-secret-name",
        logical_prompt_id=logical_prompt_id,
        workspace=workspace,
        prompt="Fix sk-live-secret authentication in private-client-repository",
        language="python",
    )


def _audit(*, candidates: int) -> BenefitAuditReference:
    return BenefitAuditReference(
        result_schema_id="ctx.benefit-result-v1",
        result_digest=_digest(f"benefit-result:{candidates}"),
        policy_schema_id="ctx.benefit-policy-v1",
        policy_digest=_digest("benefit-policy"),
        selection_algorithm_id="ctx.benefit-selection-v1",
        calibration_digest=_digest("calibration"),
        requested_limit=5,
        candidate_pool_count=candidates,
        search_evaluation_count=candidates,
    )


def _reviewed_decision(
    host: QueryHostDescriptor,
    *,
    host_invocation_digest: str,
) -> CommittedQueryDecision:
    scope = ScopeRef(
        tenant_id="local",
        workspace_id="workspace-test",
        repository_id="repository-test",
        session_id="query-test-attempt",
        exposure_id="exposure-test",
        host_context_id=host.host_context_id,
    )
    candidate = CapabilityCandidate(
        capability_id="skill:python-tdd",
        kind="skill",
        name="python-tdd",
        source_digest=_digest("catalog:python-tdd"),
        normalized_score_ppm=900_000,
        matching_signals=("python", "testing"),
        reason_codes=("signal-match",),
        actionability="manual",
    )
    selection = CapabilityPlanSelectionV3(
        presentation=candidate,
        catalog_identity=CatalogCapabilityIdentity.create(
            capability_id=candidate.capability_id,
            kind=candidate.kind,
            catalog_namespace_digest=_digest("catalog-namespace"),
        ),
        benefit=CapabilityBenefitProjection(
            tier="advisory",
            individual_net_benefit_u=900_000,
            marginal_net_benefit_u=900_000,
        ),
        authority=ManualPlanningAuthority(),
    )
    plan = CommittedPlanV3(
        plan_id="query-delivery-plan",
        catalog_snapshot_id=_digest("catalog-snapshot"),
        decision_digest=_digest("query-delivery-decision"),
        status="ready",
        abstention_code=None,
        benefit_audit=_audit(candidates=1),
        capabilities=(PlanCapabilityV3(selection=selection),),
    )
    action = HostAction(
        action_id="query-delivery-present",
        kind="PresentBundle",
        scope=scope,
        precondition_revision=2,
        payload={
            "plan_digest": plan.decision_digest,
            "capabilities": [selection.to_mapping()],
        },
    )
    return _commit_query_decision(
        host=host,
        transition=Transition(
            event_id="query-delivery-intent",
            scope=scope,
            from_revision=1,
            to_revision=2,
            actions=(action,),
        ),
        plan=plan,
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
        release_root_digest=_digest("reviewed-release-root"),
        release_sequence=2,
        catalog_mode="reviewed",
        work_signature_digest=_digest("normalized-work"),
        host_invocation_digest=host_invocation_digest,
    )


def _abstained_decision(
    host: QueryHostDescriptor,
    *,
    host_invocation_digest: str,
) -> CommittedQueryDecision:
    scope = ScopeRef(
        tenant_id="local",
        workspace_id="workspace-test",
        repository_id="repository-test",
        session_id="query-test-attempt",
        exposure_id="exposure-test",
        host_context_id=host.host_context_id,
    )
    plan = CommittedPlanV3(
        plan_id="query-delivery-abstention",
        catalog_snapshot_id=_digest("catalog-snapshot"),
        decision_digest=_digest("query-delivery-abstention"),
        status="abstained",
        abstention_code="no-feasible-capability",
        benefit_audit=_audit(candidates=0),
        capabilities=(),
    )
    return _commit_query_decision(
        host=host,
        transition=Transition(
            event_id="query-delivery-intent",
            scope=scope,
            from_revision=1,
            to_revision=2,
        ),
        plan=plan,
        journal_revision=2,
        journal_record_digest=_digest("journal-record"),
        release_root_digest=_digest("reviewed-release-root"),
        release_sequence=2,
        catalog_mode="reviewed",
        work_signature_digest=_digest("normalized-work"),
        host_invocation_digest=host_invocation_digest,
    )


def _controller(
    tmp_path: Path,
    *,
    host: QueryHostDescriptor | None = None,
    mode: str = "recommend",
    factory=None,  # type: ignore[no-untyped-def]
) -> QueryDeliveryController:
    descriptor = QueryHostDescriptor.codex() if host is None else host
    if factory is None:

        def factory(**kwargs: object) -> CommittedQueryDecision:
            invocation = kwargs["host_invocation_digest"]
            assert isinstance(invocation, str)
            return _reviewed_decision(descriptor, host_invocation_digest=invocation)

    return QueryDeliveryController._for_testing(
        host=descriptor,
        mode=mode,
        state_root=tmp_path / "state",
        decision_factory=factory,
        environment={},
    )


def _process_request(workspace: Path) -> SensitiveQueryInput:
    return SensitiveQueryInput(
        native_session_id="multiprocess-session-secret",
        logical_prompt_id="multiprocess-turn-1",
        workspace=workspace,
        prompt="Review the Python implementation",
        language="python",
    )


def _multiprocess_abstention_worker(
    state_root: str,
    workspace: str,
    result_queue: Any,
) -> None:
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=Path(state_root),
        environment={},
        lock_timeout_seconds=10.0,
    )
    result_queue.put(controller.issue(_process_request(Path(workspace))).status)


def _crashing_factory(**_kwargs: object) -> CommittedQueryDecision:
    os._exit(73)


def _process_reviewed_factory(**kwargs: object) -> CommittedQueryDecision:
    invocation = kwargs["host_invocation_digest"]
    assert isinstance(invocation, str)
    return _reviewed_decision(
        QueryHostDescriptor.codex(),
        host_invocation_digest=invocation,
    )


def _crash_after_production_decision_factory(**kwargs: Any) -> CommittedQueryDecision:
    prepare_query_decision(**kwargs)
    os._exit(76)


def _production_crash_worker(state_root: str, workspace: str) -> None:
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=Path(state_root),
        decision_factory=_crash_after_production_decision_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    controller.issue(
        SensitiveQueryInput(
            native_session_id="production-crash-session-secret",
            logical_prompt_id="production-crash-turn",
            workspace=Path(workspace),
            prompt="Fix sk-production-crash-secret authentication",
            language="python",
        )
    )
    os._exit(77)


def _forked_permit_worker(permit: Any, result_queue: Any) -> None:
    try:
        permit.emit_once(io.BytesIO())
    except Exception as error:
        result_queue.put(f"{type(error).__name__}:{error}")
    else:
        result_queue.put("emitted")


def _crash_before_terminal_worker(state_root: str, workspace: str) -> None:
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=Path(state_root),
        decision_factory=_crashing_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    controller.issue(_process_request(Path(workspace)))
    os._exit(72)


def _crash_unique_session_worker(
    state_root: str,
    workspace: str,
    native_session_id: str,
) -> None:
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=Path(state_root),
        decision_factory=_crashing_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    controller.issue(
        SensitiveQueryInput(
            native_session_id=native_session_id,
            logical_prompt_id="turn-1",
            workspace=Path(workspace),
            prompt="Review the Python implementation",
            language="python",
        )
    )
    os._exit(72)


def _crash_after_terminal_worker(state_root: str, workspace: str) -> None:
    def crash_before_permit_return(*_args: object, **_kwargs: object) -> None:
        os._exit(78)

    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=Path(state_root),
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    query_delivery_runtime.QueryEmissionPermit = crash_before_permit_return  # type: ignore[assignment,misc]
    controller.issue(_process_request(Path(workspace)))
    os._exit(75)


def _crash_after_quarantine_rename_worker(state_root: str, workspace: str) -> None:
    original_purge = attempt_storage._purge_named_attempt

    def crash_purge(root_fd: int, name: str) -> None:
        if name.endswith("-quarantine"):
            os._exit(79)
        original_purge(root_fd, name)

    attempt_storage._purge_named_attempt = crash_purge
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=Path(state_root),
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    controller.issue(_process_request(Path(workspace)))
    os._exit(75)


def _seed_empty_crash_slot(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_before_terminal_worker,
        args=(str(state_root), str(workspace)),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 73
    children = _attempt_children(state_root)
    assert len(children) == 1
    return state_root, workspace, children[0]


def test_query_delivery_issues_once_without_persisting_sensitive_input(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    host = QueryHostDescriptor.codex()

    def factory(**kwargs: object) -> CommittedQueryDecision:
        calls.append(kwargs)
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _reviewed_decision(host, host_invocation_digest=invocation)

    controller = _controller(tmp_path, host=host, factory=factory)

    first = controller.issue(_request(tmp_path))
    second = controller.issue(_request(tmp_path))

    assert first.status == "issued"
    assert first.emission_permit is not None
    assert second.status == "already-terminal"
    assert second.emission_permit is None
    assert len(calls) == 1
    assert calls[0]["task"] == _request(tmp_path).prompt
    assert calls[0]["workspace"] == _request(tmp_path).workspace
    assert calls[0]["session_id"] != _request(tmp_path).native_session_id
    assert str(calls[0]["session_id"]).startswith("query-")
    assert len(str(calls[0]["host_invocation_digest"])) == 64

    output = io.BytesIO()
    first.emission_permit.emit_once(output)
    assert output.getvalue().endswith(b"\n")
    assert b'"hookEventName":"UserPromptSubmit"' in output.getvalue()
    with pytest.raises(RuntimeError, match="already consumed"):
        first.emission_permit.emit_once(io.BytesIO())

    persisted = b"\n".join(
        path.read_bytes() for path in (tmp_path / "state").rglob("*") if path.is_file()
    )
    for secret in (
        b"sk-live-secret",
        b"private-client-repository",
        b"session-secret-name",
        str(tmp_path).encode(),
    ):
        assert secret not in persisted


def test_query_delivery_activate_issues_authenticated_ephemeral_material(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="activate",
        state_root=tmp_path / "state",
        environment={},
    )
    request = SensitiveQueryInput(
        native_session_id="activate-session",
        logical_prompt_id="activate-turn",
        workspace=workspace,
        prompt="Fix the Python tests",
        language="python",
    )

    first = controller.issue(request)
    second = controller.issue(request)

    assert first.status == "issued"
    assert first.emission_permit is not None
    assert second.status == "already-terminal"
    output = io.BytesIO()
    first.emission_permit.emit_once(output)
    assert b"# ctx Python Testing" in output.getvalue()
    assert b"CTX recommendation bundle" not in output.getvalue()
    persisted = b"\n".join(
        path.read_bytes() for path in (tmp_path / "state").rglob("*") if path.is_file()
    )
    assert b"# ctx Python Testing" not in persisted


def test_query_delivery_activate_preserves_abstention(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.claude_code(),
        mode="activate",
        state_root=tmp_path / "state",
        environment={},
    )

    report = controller.issue(
        SensitiveQueryInput(
            native_session_id="activate-abstain-session",
            logical_prompt_id="activate-abstain-turn",
            workspace=workspace,
            prompt="Fix the JavaScript tests",
            language="javascript",
        )
    )

    assert report.status == "abstained"
    assert report.emission_permit is None


def test_query_delivery_activate_has_host_neutral_model_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outputs: list[bytes] = []
    for host, state_name, session_id in (
        (QueryHostDescriptor.codex(), "codex-state", "codex-session"),
        (QueryHostDescriptor.claude_code(), "claude-state", "claude-session"),
    ):
        controller = QueryDeliveryController(
            host=host,
            mode="activate",
            state_root=tmp_path / state_name,
            environment={},
        )
        report = controller.issue(
            SensitiveQueryInput(
                native_session_id=session_id,
                logical_prompt_id="shared-turn",
                workspace=workspace,
                prompt="Fix the Python tests",
                language="python",
            )
        )
        assert report.status == "issued"
        assert report.emission_permit is not None
        output = io.BytesIO()
        report.emission_permit.emit_once(output)
        outputs.append(output.getvalue())

    assert outputs[0] == outputs[1]


def test_query_delivery_experiment_mode_is_not_a_caller_chosen_authority(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="legacy, shadow, recommend, activate, or manage"):
        QueryDeliveryController(
            host=QueryHostDescriptor.codex(),
            mode="experiment",
            state_root=tmp_path / "state",
            environment={},
        )


def test_query_delivery_declares_manage_as_an_explicit_mode(tmp_path: Path) -> None:
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="manage",
        state_root=tmp_path / "state",
        environment={},
    )

    assert controller._mode == "manage"
    assert controller._host.execution_intent == "activate"
    assert controller._managed_availability_enabled


def test_manage_mode_publishes_one_durable_resumable_challenge_without_installing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="manage",
        state_root=state_root,
        environment={"CTX_INSTALL_POLICY_ROOT": str(tmp_path / "policy")},
    )

    report = controller.issue(
        SensitiveQueryInput(
            native_session_id="managed-challenge-session",
            logical_prompt_id="managed-challenge-turn",
            workspace=workspace,
            prompt="repair nested Python context manager state restoration",
            language="Python",
        )
    )

    assert report.status == "issued"
    assert report.emission_permit is not None
    output = io.BytesIO()
    report.emission_permit.emit_once(output)
    payload = output.getvalue()
    assert b"approval=required" in payload
    assert b"challenge_id=" in payload
    assert b"challenge_digest=" in payload
    assert b"continuation=external-authenticator-required" in payload
    assert b"resumable=true" not in payload
    assert b"Prompt text and model output are not installation consent" in payload
    assert str(workspace).encode() not in payload
    brokers = tuple(state_root.rglob("install-consent-v1.sqlite3"))
    assert len(brokers) == 1
    skill_stores = tuple(state_root.rglob("skill-cas-v1"))
    assert len(skill_stores) == 1
    assert tuple(skill_stores[0].iterdir()) == ()


def test_query_delivery_issues_once_per_logical_prompt_in_the_same_session(tmp_path: Path) -> None:
    controller = _controller(tmp_path)

    first = controller.issue(_request(tmp_path, logical_prompt_id="turn-1"))
    second = controller.issue(_request(tmp_path, logical_prompt_id="turn-2"))
    repeated = controller.issue(_request(tmp_path, logical_prompt_id="turn-2"))

    assert first.status == "issued"
    assert second.status == "issued"
    assert repeated.status == "already-terminal"


def test_query_delivery_honors_a_legacy_session_terminal_after_upgrade(tmp_path: Path) -> None:
    calls = 0
    host = QueryHostDescriptor.codex()

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _reviewed_decision(host, host_invocation_digest=invocation)

    controller = _controller(tmp_path, host=host, factory=factory)
    key, ledger, _attempt_pool = controller._runtime()
    request = _request(tmp_path, logical_prompt_id="turn-after-upgrade")
    reference = query_delivery_runtime._derive_invocation_ref(
        key=key,
        host=host,
        request=request,
    )
    legacy_payload = {
        "host_context_id": host.host_context_id,
        "host_descriptor_digest": host.host_descriptor_digest,
        "identity": "engine-session-slot",
        "native_session_id": request.native_session_id,
        "schema": "ctx.query-delivery-identity-v1",
        "slot": "initial-query-v1",
        "workspace_digest": hashlib.sha256(
            os.path.normcase(
                os.path.realpath(os.path.abspath(os.fspath(request.workspace)))
            ).encode("utf-8")
        ).hexdigest(),
    }
    expected_legacy_digest = hmac.new(
        key,
        json.dumps(
            legacy_payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert reference.legacy_delivery_key_digest == expected_legacy_digest
    ledger.commit(
        delivery_key_digest=expected_legacy_digest,
        terminal_kind="abstained",
        terminal_result_digest=_digest("legacy-terminal"),
    )

    assert controller.issue(request).status == "already-terminal"
    assert calls == 0


def test_query_delivery_terminal_capacity_is_atomic_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(query_delivery_runtime, "_MAX_TERMINAL_ROWS", 4)
    controller = _controller(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        reports = tuple(
            executor.map(
                controller.issue,
                (
                    _request(tmp_path, logical_prompt_id=f"capacity-turn-{index}")
                    for index in range(8)
                ),
            )
        )

    statuses = [report.status for report in reports]
    assert statuses.count("issued") == 4
    assert statuses.count("failed") == 4
    database = tmp_path / "state" / "query-delivery-v1.sqlite3"
    with sqlite3.connect(database) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone()
    assert row_count == (4,)


def test_query_delivery_attempt_storage_has_a_fixed_global_bound(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for index in range(24):
        report = controller.issue(
            SensitiveQueryInput(
                native_session_id=f"bounded-session-{index}",
                logical_prompt_id="turn-1",
                workspace=workspace,
                prompt="Review the Python implementation",
                language="python",
            )
        )
        assert report.status == "issued"

    attempt_root = tmp_path / "state" / "query-delivery-attempts-v1"
    assert tuple(attempt_root.iterdir()) == ()
    lock_root = tmp_path / "state" / "query-delivery-locks-v1"
    lock_names = {path.name for path in lock_root.iterdir()}
    assert len(lock_names) <= 8
    assert lock_names <= {f"query-attempt-stripe-{index}.lock" for index in range(8)}


def test_unique_session_crash_storm_cannot_expand_the_attempt_namespace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    session_ids = [f"crash-storm-session-{index}" for index in range(16)]

    processes = [
        context.Process(
            target=_crash_unique_session_worker,
            args=(str(state_root), str(workspace), session_id),
        )
        for session_id in session_ids
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 73

    children = _attempt_children(state_root)
    assert len(children) <= 8
    assert {path.name for path in children} <= {
        *(f"slot-{index}" for index in range(8)),
        *(f"slot-{index}-quarantine" for index in range(8)),
    }

    lock_names = {path.name for path in (state_root / "query-delivery-locks-v1").iterdir()}
    assert len(lock_names) <= 8

    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    for session_id in session_ids:
        report = controller.issue(
            SensitiveQueryInput(
                native_session_id=session_id,
                logical_prompt_id="turn-1",
                workspace=workspace,
                prompt="Review the Python implementation",
                language="python",
            )
        )
        assert report.status == "issued"
    assert _attempt_children(state_root) == ()


def test_existing_terminal_reclaims_same_stripe_residue_without_replanning(
    tmp_path: Path,
) -> None:
    calls = 0

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        return _process_reviewed_factory(**kwargs)

    controller = _controller(tmp_path, factory=factory)
    request = _request(tmp_path)
    assert controller.issue(request).status == "issued"
    lock_root = tmp_path / "state" / "query-delivery-locks-v1"
    lock_name = next(iter(lock_root.iterdir())).name
    stripe = int(lock_name.removeprefix("query-attempt-stripe-").removesuffix(".lock"))
    residue = tmp_path / "state" / "query-delivery-attempts-v1" / f"slot-{stripe}"
    residue.mkdir(mode=0o700)

    assert controller.issue(request).status == "already-terminal"
    assert calls == 1
    assert _attempt_children(tmp_path / "state") == ()


def test_existing_terminal_with_unsafe_cleanup_fails_closed_but_stays_burned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        return _process_reviewed_factory(**kwargs)

    controller = _controller(tmp_path, factory=factory)
    request = _request(tmp_path)
    assert controller.issue(request).status == "issued"
    lock_name = next(iter((tmp_path / "state" / "query-delivery-locks-v1").iterdir())).name
    stripe = int(lock_name.removeprefix("query-attempt-stripe-").removesuffix(".lock"))
    residue = tmp_path / "state" / "query-delivery-attempts-v1" / f"slot-{stripe}"
    residue.mkdir(mode=0o700)
    managed = residue / "engine.sqlite3"
    managed.write_bytes(b"managed-crash-residue")
    managed.chmod(0o600)
    original_unlink = attempt_storage._unlink_exact_file

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise attempt_storage.QueryAttemptStorageError("injected cleanup failure")

    monkeypatch.setattr(attempt_storage, "_unlink_exact_file", fail_cleanup)

    assert controller.issue(request).status == "failed"
    assert calls == 1
    with sqlite3.connect(tmp_path / "state" / "query-delivery-v1.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            1,
        )

    monkeypatch.setattr(attempt_storage, "_unlink_exact_file", original_unlink)
    assert controller.issue(request).status == "already-terminal"
    assert calls == 1
    assert _attempt_children(tmp_path / "state") == ()


def test_query_delivery_preserves_unknown_crash_residue_without_burning_terminal(
    tmp_path: Path,
) -> None:
    state_root, workspace, crash_slot = _seed_empty_crash_slot(tmp_path)
    unknown = crash_slot / "unknown-private-material"
    unknown.write_bytes(b"preserve-me")
    unknown.chmod(0o600)
    calls = 0

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        return _process_reviewed_factory(**kwargs)

    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=factory,
        environment={},
        lock_timeout_seconds=10.0,
    )

    assert controller.issue(_process_request(workspace)).status == "failed"
    assert calls == 0
    retained = tuple((state_root / "query-delivery-attempts-v1").rglob(unknown.name))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"preserve-me"
    with sqlite3.connect(state_root / "query-delivery-v1.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            0,
        )

    retained[0].unlink()
    assert controller.issue(_process_request(workspace)).status == "issued"
    assert calls == 1
    assert _attempt_children(state_root) == ()


def test_query_attempt_enumeration_stops_at_cap_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "many-entries"
    directory.mkdir(mode=0o700)
    for index in range(100):
        (directory / f"entry-{index}").touch(mode=0o600)
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    observed: list[str] = []
    real_scandir = os.scandir

    @contextmanager
    def counting_scandir(path: int) -> Iterator[Iterator[os.DirEntry[str]]]:
        with real_scandir(path) as entries:

            def counted() -> Iterator[os.DirEntry[str]]:
                for entry in entries:
                    observed.append(entry.name)
                    yield entry

            yield counted()

    monkeypatch.setattr(attempt_storage.os, "scandir", counting_scandir)
    try:
        with pytest.raises(
            attempt_storage.QueryAttemptStorageConflict,
            match="entry budget",
        ):
            attempt_storage._bounded_entry_names(descriptor, maximum=16)
    finally:
        os.close(descriptor)

    assert len(observed) == 17


def test_query_delivery_removes_authenticated_oversized_managed_residue(
    tmp_path: Path,
) -> None:
    state_root, workspace, crash_slot = _seed_empty_crash_slot(tmp_path)
    managed = crash_slot / "engine.sqlite3"
    with managed.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    managed.chmod(0o600)
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )

    assert controller.issue(_process_request(workspace)).status == "issued"
    assert _attempt_children(state_root) == ()


def test_query_delivery_rejects_rename_exposed_attempt_ancestry(
    tmp_path: Path,
) -> None:
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o777)
    unsafe_parent.chmod(0o777)
    state_root = unsafe_parent / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = 0

    def factory(**_kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        raise AssertionError("unsafe ancestry must fail before planning")

    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=factory,
        environment={},
    )

    assert controller.issue(_process_request(workspace)).status == "failed"
    assert calls == 0
    assert not state_root.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system directory aliases")
@pytest.mark.parametrize("alias_root", ["/tmp", "/var/tmp"])
def test_query_delivery_accepts_authenticated_macos_system_aliases(alias_root: str) -> None:
    try:
        temporary = tempfile.TemporaryDirectory(dir=alias_root)
    except OSError as error:
        pytest.skip(f"system alias is not writable: {error}")
    with temporary as root:
        base = Path(root)
        workspace = base / "workspace"
        workspace.mkdir()
        controller = QueryDeliveryController._for_testing(
            host=QueryHostDescriptor.codex(),
            mode="recommend",
            state_root=base / "state",
            decision_factory=_process_reviewed_factory,
            environment={},
        )

        assert controller.issue(_process_request(workspace)).status == "issued"
        assert _attempt_children(base / "state") == ()


def test_query_delivery_detects_live_same_user_attempt_root_replacement(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def replace_root(**kwargs: object) -> CommittedQueryDecision:
        journal_path = kwargs["journal_path"]
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(journal_path, Path)
        assert isinstance(invocation, str)
        attempt_root = journal_path.parents[1]
        parked = state_root / "parked-original-attempt-root"
        attempt_root.rename(parked)
        journal_path.parent.mkdir(parents=True, mode=0o700)
        journal_path.write_bytes(b"same-user-replacement-residue")
        journal_path.chmod(0o600)
        return _reviewed_decision(
            QueryHostDescriptor.codex(),
            host_invocation_digest=invocation,
        )

    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=replace_root,
        environment={},
    )

    assert controller.issue(_process_request(workspace)).status == "failed"
    with sqlite3.connect(state_root / "query-delivery-v1.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            0,
        )
    assert (
        state_root
        / "query-delivery-attempts-v1"
        / next(iter((state_root / "query-delivery-attempts-v1").iterdir())).name
        / "engine.sqlite3"
    ).read_bytes() == b"same-user-replacement-residue"


def test_query_delivery_detects_unknown_name_created_during_directory_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    calls = 0
    real_rmdir = os.rmdir

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        return _process_reviewed_factory(**kwargs)

    def replace_at_removal(path: str, *, dir_fd: int | None = None) -> None:
        if path.endswith("-quarantine") and dir_fd is not None:
            os.rename(
                path,
                "untracked-original",
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            os.mkdir(path, mode=0o700, dir_fd=dir_fd)
        real_rmdir(path, dir_fd=dir_fd)

    controller = _controller(tmp_path, factory=factory)
    controller._runtime()
    monkeypatch.setattr(attempt_storage.os, "rmdir", replace_at_removal)
    monkeypatch.setattr(query_delivery_runtime, "query_attempt_pool_supported", lambda: True)

    assert controller.issue(_request(tmp_path)).status == "failed"
    assert calls == 1
    assert (state_root / "query-delivery-attempts-v1" / "untracked-original").is_dir()
    with sqlite3.connect(state_root / "query-delivery-v1.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            0,
        )


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_query_delivery_cleanup_never_mutates_linked_outside_material(
    tmp_path: Path,
    link_kind: str,
) -> None:
    state_root, workspace, crash_slot = _seed_empty_crash_slot(tmp_path)
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"outside-must-survive")
    outside.chmod(0o600)
    linked = crash_slot / "engine.sqlite3"
    try:
        if link_kind == "symlink":
            linked.symlink_to(outside)
        else:
            os.link(outside, linked)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"{link_kind} is unavailable: {error}")
    outside_before = outside.stat(follow_symlinks=False)
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )

    assert controller.issue(_process_request(workspace)).status == "failed"

    outside_after = outside.stat(follow_symlinks=False)
    assert outside.read_bytes() == b"outside-must-survive"
    assert os.path.samestat(outside_before, outside_after)
    linked_after = tuple((state_root / "query-delivery-attempts-v1").rglob("engine.sqlite3"))
    assert len(linked_after) == 1
    if link_kind == "hardlink":
        assert outside_after.st_nlink == 2
    linked_after[0].unlink()
    assert controller.issue(_process_request(workspace)).status == "issued"
    assert outside.read_bytes() == b"outside-must-survive"
    assert _attempt_children(state_root) == ()


@pytest.mark.parametrize("poison_kind", ["slot-symlink", "quarantine-symlink", "slot-fifo"])
def test_query_delivery_rejects_non_directory_attempt_paths(
    tmp_path: Path,
    poison_kind: str,
) -> None:
    state_root, workspace, crash_slot = _seed_empty_crash_slot(tmp_path)
    slot_name = crash_slot.name
    crash_slot.rmdir()
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    if poison_kind == "quarantine-symlink":
        poison = crash_slot.with_name(f"{slot_name}-quarantine")
        poison.symlink_to(outside, target_is_directory=True)
    elif poison_kind == "slot-symlink":
        poison = crash_slot
        poison.symlink_to(outside, target_is_directory=True)
    else:
        poison = crash_slot
        try:
            os.mkfifo(poison, mode=0o600)
        except (AttributeError, NotImplementedError, OSError) as error:
            pytest.skip(f"FIFO creation is unavailable: {error}")
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )

    assert controller.issue(_process_request(workspace)).status == "failed"
    assert outside.is_dir()
    assert poison.exists() or poison.is_symlink()

    poison.unlink()
    assert controller.issue(_process_request(workspace)).status == "issued"
    assert outside.is_dir()
    assert _attempt_children(state_root) == ()


def test_query_delivery_cleanup_failure_is_retryable_and_never_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = QueryHostDescriptor.codex()
    calls = 0

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        journal_path = kwargs["journal_path"]
        assert isinstance(journal_path, Path)
        journal_path.write_bytes(b"bounded-test-journal")
        journal_path.chmod(0o600)
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _reviewed_decision(host, host_invocation_digest=invocation)

    original_unlink = attempt_storage._unlink_exact_file

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise attempt_storage.QueryAttemptStorageError("injected cleanup failure")

    monkeypatch.setattr(attempt_storage, "_unlink_exact_file", fail_cleanup)
    controller = _controller(tmp_path, host=host, factory=factory)

    assert controller.issue(_request(tmp_path)).status == "failed"
    assert calls == 1
    with sqlite3.connect(tmp_path / "state" / "query-delivery-v1.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            0,
        )
    assert len(_attempt_children(tmp_path / "state")) == 1

    monkeypatch.setattr(attempt_storage, "_unlink_exact_file", original_unlink)
    assert controller.issue(_request(tmp_path)).status == "issued"
    assert calls == 2
    assert _attempt_children(tmp_path / "state") == ()


def test_query_delivery_preserves_primary_base_exception_when_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def interrupt(**_kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        raise KeyboardInterrupt

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise attempt_storage.QueryAttemptStorageError("injected cleanup failure")

    monkeypatch.setattr(attempt_storage, "_quarantine_and_purge", fail_cleanup)
    controller = _controller(tmp_path, factory=interrupt)

    with pytest.raises(KeyboardInterrupt) as captured:
        controller.issue(_request(tmp_path))

    assert calls == 1
    assert getattr(captured.value, "__notes__", []) == [
        "query attempt cleanup also failed with QueryAttemptStorageError"
    ]
    with sqlite3.connect(tmp_path / "state" / "query-delivery-v1.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            0,
        )


def test_query_delivery_cleanup_completes_before_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_commit = _SQLiteQueryDeliveryLedger.commit
    commit_observations: list[tuple[Path, ...]] = []

    def assert_clean_then_commit(
        ledger: _SQLiteQueryDeliveryLedger,
        **kwargs: object,
    ) -> bool:
        commit_observations.append(_attempt_children(tmp_path / "state"))
        return original_commit(ledger, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_SQLiteQueryDeliveryLedger, "commit", assert_clean_then_commit)

    assert _controller(tmp_path).issue(_request(tmp_path)).status == "issued"
    assert commit_observations == [()]


def test_query_delivery_unsupported_cleanup_platform_fails_before_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def factory(**_kwargs: object) -> CommittedQueryDecision:
        nonlocal called
        called = True
        raise AssertionError("unsupported platform must not execute")

    monkeypatch.setattr(query_delivery_runtime, "query_attempt_pool_supported", lambda: False)
    controller = _controller(tmp_path, factory=factory)

    assert controller.issue(_request(tmp_path)).status == "failed"
    assert called is False
    assert not (tmp_path / "state").exists()


def test_query_delivery_is_atomic_across_concurrent_callers(tmp_path: Path) -> None:
    calls = 0
    host = QueryHostDescriptor.codex()

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _reviewed_decision(host, host_invocation_digest=invocation)

    controller = _controller(tmp_path, factory=factory)

    with ThreadPoolExecutor(max_workers=16) as pool:
        reports = tuple(pool.map(lambda _index: controller.issue(_request(tmp_path)), range(64)))

    assert [report.status for report in reports].count("issued") == 1
    assert [report.status for report in reports].count("already-terminal") == 63
    assert calls == 1


def test_query_delivery_waits_through_one_contention_window(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    host = QueryHostDescriptor.codex()

    def factory(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=10)
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _reviewed_decision(host, host_invocation_digest=invocation)

    controller = _controller(tmp_path, factory=factory)
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(controller.issue, _request(tmp_path))
        assert entered.wait(timeout=5)
        duplicate = pool.submit(controller.issue, _request(tmp_path))
        threading.Event().wait(2.1)
        release.set()
        statuses = {winner.result(timeout=10).status, duplicate.result(timeout=10).status}

    assert statuses == {"issued", "already-terminal"}
    assert calls == 1


def test_query_delivery_retries_cleanly_after_transient_runtime_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_ledger = query_delivery_runtime._SQLiteQueryDeliveryLedger
    calls = 0

    def fail_once(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected first initialization failure")
        return real_ledger(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(query_delivery_runtime, "_SQLiteQueryDeliveryLedger", fail_once)
    controller = _controller(tmp_path)

    assert controller.issue(_request(tmp_path)).status == "issued"
    assert calls == 2
    assert _attempt_children(tmp_path / "state") == ()


def test_query_delivery_is_atomic_across_spawned_processes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_multiprocess_abstention_worker,
            args=(str(state_root), str(workspace), result_queue),
        )
        for _index in range(8)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    statuses = [result_queue.get(timeout=5) for _process in processes]

    assert statuses.count("abstained") == 1
    assert statuses.count("already-terminal") == 7
    assert _attempt_children(state_root) == ()
    persisted = b"\n".join(path.read_bytes() for path in state_root.rglob("*") if path.is_file())
    assert b"multiprocess-session-secret" not in persisted
    assert str(workspace).encode() not in persisted


def test_query_delivery_retries_after_process_crash_before_terminal_commit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_before_terminal_worker,
        args=(str(state_root), str(workspace)),
    )

    process.start()
    process.join(timeout=30)

    assert process.exitcode == 73
    crash_children = _attempt_children(state_root)
    assert len(crash_children) == 1
    assert crash_children[0].name.startswith("slot-")
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    assert controller.issue(_process_request(workspace)).status == "issued"
    assert _attempt_children(state_root) == ()


def test_query_delivery_suppresses_retry_after_terminal_commit_before_emission(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_crash_after_terminal_worker,
        args=(str(state_root), str(workspace)),
    )

    process.start()
    process.join(timeout=30)

    assert process.exitcode == 78
    assert _attempt_children(state_root) == ()
    calls = 0

    def must_not_replan(**kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        return _process_reviewed_factory(**kwargs)

    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=must_not_replan,
        environment={},
        lock_timeout_seconds=10.0,
    )
    assert controller.issue(_process_request(workspace)).status == "already-terminal"
    assert calls == 0


def test_query_delivery_recovers_crash_after_quarantine_rename(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_after_quarantine_rename_worker,
        args=(str(state_root), str(workspace)),
    )

    process.start()
    process.join(timeout=30)

    assert process.exitcode == 79
    children = _attempt_children(state_root)
    assert len(children) == 1
    assert children[0].name.endswith("-quarantine")
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=_process_reviewed_factory,
        environment={},
        lock_timeout_seconds=10.0,
    )
    assert controller.issue(_process_request(workspace)).status == "issued"
    assert _attempt_children(state_root) == ()


def test_query_delivery_recovers_real_production_crash_residue_privately(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "private-production-workspace"
    workspace.mkdir()
    state_root = tmp_path / "state"
    process = multiprocessing.get_context("spawn").Process(
        target=_production_crash_worker,
        args=(str(state_root), str(workspace)),
    )

    process.start()
    process.join(timeout=30)

    assert process.exitcode == 76
    children = _attempt_children(state_root)
    assert len(children) == 1
    retained = b"\n".join(
        path.read_bytes()
        for path in children[0].rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    for secret in (
        b"sk-production-crash-secret",
        b"production-crash-session-secret",
        b"production-crash-turn",
        b"authentication",
        b"private-production-workspace",
        str(workspace).encode(),
    ):
        assert secret not in retained
    with sqlite3.connect(state_root / "query-delivery-v1.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            0,
        )

    controller = QueryDeliveryController(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        environment={},
        lock_timeout_seconds=10.0,
    )
    request = SensitiveQueryInput(
        native_session_id="production-crash-session-secret",
        logical_prompt_id="production-crash-turn",
        workspace=workspace,
        prompt="Fix sk-production-crash-secret authentication",
        language="python",
    )

    assert controller.issue(request).status == "abstained"
    assert _attempt_children(state_root) == ()


def test_query_delivery_claims_are_host_specific(tmp_path: Path) -> None:
    codex = _controller(tmp_path, host=QueryHostDescriptor.codex())
    claude_host = QueryHostDescriptor.claude_code()

    def claude_factory(**kwargs: object) -> CommittedQueryDecision:
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _reviewed_decision(claude_host, host_invocation_digest=invocation)

    claude = _controller(
        tmp_path,
        host=claude_host,
        factory=claude_factory,
    )

    assert codex.issue(_request(tmp_path)).status == "issued"
    assert claude.issue(_request(tmp_path)).status == "issued"


def test_query_delivery_shadow_and_abstention_are_terminal_and_silent(tmp_path: Path) -> None:
    shadow = _controller(tmp_path / "shadow", mode="shadow")
    host = QueryHostDescriptor.codex()

    def abstain(**kwargs: object) -> CommittedQueryDecision:
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _abstained_decision(host, host_invocation_digest=invocation)

    abstained = _controller(tmp_path / "abstained", factory=abstain)
    shadow_abstained = _controller(
        tmp_path / "shadow-abstained",
        mode="shadow",
        factory=abstain,
    )

    shadow_report = shadow.issue(_request(tmp_path / "shadow"))
    abstained_report = abstained.issue(_request(tmp_path / "abstained"))
    shadow_abstained_report = shadow_abstained.issue(_request(tmp_path / "shadow-abstained"))

    assert shadow_report.status == "shadow-ready"
    assert shadow_report.emission_permit is None
    assert abstained_report.status == "abstained"
    assert abstained_report.emission_permit is None
    assert shadow_abstained_report.status == "shadow-abstained"
    assert shadow_abstained_report.emission_permit is None
    assert shadow.issue(_request(tmp_path / "shadow")).status == "already-terminal"
    assert abstained.issue(_request(tmp_path / "abstained")).status == "already-terminal"
    assert (
        shadow_abstained.issue(_request(tmp_path / "shadow-abstained")).status == "already-terminal"
    )
    assert _attempt_children(tmp_path / "shadow" / "state") == ()
    assert _attempt_children(tmp_path / "abstained" / "state") == ()
    assert _attempt_children(tmp_path / "shadow-abstained" / "state") == ()


def test_query_delivery_failure_is_terminal_and_does_not_leak_exception(tmp_path: Path) -> None:
    calls = 0

    def explode(**_kwargs: object) -> CommittedQueryDecision:
        nonlocal calls
        calls += 1
        raise RuntimeError("/private/client sk-live-secret")

    controller = _controller(tmp_path, factory=explode)

    first = controller.issue(_request(tmp_path))
    second = controller.issue(_request(tmp_path))

    assert first.status == "failed"
    assert second.status == "already-terminal"
    assert first.emission_permit is second.emission_permit is None
    assert calls == 1
    persisted = b"\n".join(
        path.read_bytes() for path in (tmp_path / "state").rglob("*") if path.is_file()
    )
    assert b"sk-live-secret" not in persisted
    assert b"private/client" not in persisted
    assert _attempt_children(tmp_path / "state") == ()


def test_query_delivery_terminal_ledger_contains_only_digest_columns(tmp_path: Path) -> None:
    report = _controller(tmp_path).issue(_request(tmp_path))
    assert report.status == "issued"
    ledger = tmp_path / "state" / "query-delivery-v1.sqlite3"

    with sqlite3.connect(ledger) as connection:
        columns = tuple(
            str(row[1])
            for row in connection.execute("PRAGMA table_info(query_delivery_terminals)").fetchall()
        )
        row = connection.execute("SELECT * FROM query_delivery_terminals").fetchone()

    assert columns == (
        "delivery_key_digest",
        "terminal_kind_digest",
        "terminal_result_digest",
        "record_digest",
    )
    assert row is not None
    assert all(isinstance(value, str) and len(value) == 64 for value in row)
    persisted = b"\n".join(
        path.read_bytes() for path in (tmp_path / "state").rglob("*") if path.is_file()
    )
    assert b'"issued"' not in persisted
    assert b"UserPromptSubmit" not in persisted


def test_query_delivery_ledger_rejects_conflicting_terminal_replay(tmp_path: Path) -> None:
    state_root = tmp_path / "ledger-state"
    state_root.mkdir(mode=0o700)
    ledger = _SQLiteQueryDeliveryLedger(
        state_root / "query-delivery-v1.sqlite3",
        installation_key=b"k" * 32,
    )
    delivery_key = _digest("delivery-key")
    result = _digest("first-result")

    assert ledger.commit(
        delivery_key_digest=delivery_key,
        terminal_kind="issued",
        terminal_result_digest=result,
    )
    assert not ledger.commit(
        delivery_key_digest=delivery_key,
        terminal_kind="issued",
        terminal_result_digest=result,
    )
    with pytest.raises(QueryDeliveryCorruption, match="different terminal"):
        ledger.commit(
            delivery_key_digest=delivery_key,
            terminal_kind="abstained",
            terminal_result_digest=_digest("second-result"),
        )


class _PartialBinaryStream:
    def write(self, _payload: bytes) -> int:
        return 1

    def flush(self) -> None:
        return None


class _RacingBinaryStream:
    def __init__(self) -> None:
        self._attribute_barrier = threading.Barrier(2)
        self._write_lock = threading.Lock()
        self.writes: list[bytes] = []

    @property
    def write(self):  # type: ignore[no-untyped-def]
        self._attribute_barrier.wait(timeout=5)
        return self._write

    @property
    def flush(self):  # type: ignore[no-untyped-def]
        self._attribute_barrier.wait(timeout=5)
        return self._flush

    def _write(self, payload: bytes) -> int:
        with self._write_lock:
            self.writes.append(payload)
        return len(payload)

    def _flush(self) -> None:
        return None


def test_query_emission_partial_write_is_consumed_and_never_retried(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    report = controller.issue(_request(tmp_path))
    assert report.emission_permit is not None

    with pytest.raises(RuntimeError, match="part"):
        report.emission_permit.emit_once(_PartialBinaryStream())
    with pytest.raises(RuntimeError, match="already consumed"):
        report.emission_permit.emit_once(io.BytesIO())
    assert controller.issue(_request(tmp_path)).status == "already-terminal"


def test_query_emission_permit_is_atomic_across_threads(tmp_path: Path) -> None:
    report = _controller(tmp_path).issue(_request(tmp_path))
    assert report.emission_permit is not None
    stream = _RacingBinaryStream()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(report.emission_permit.emit_once, stream) for _index in range(2)]
        outcomes: list[str] = []
        for future in futures:
            try:
                future.result(timeout=10)
            except RuntimeError as error:
                outcomes.append(str(error))
            else:
                outcomes.append("emitted")

    assert outcomes.count("emitted") == 1
    assert sum("already consumed" in outcome for outcome in outcomes) == 1
    assert len(stream.writes) == 1


def test_forked_permit_rejects_before_touching_an_inherited_locked_mutex(
    tmp_path: Path,
) -> None:
    try:
        context = multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("fork multiprocessing context is unavailable")
    report = _controller(tmp_path).issue(_request(tmp_path))
    permit = report.emission_permit
    assert permit is not None
    result_queue = context.Queue()
    permit._emit_lock.acquire()
    try:
        process = context.Process(
            target=_forked_permit_worker,
            args=(permit, result_queue),
        )
        process.start()
        process.join(timeout=10)
    finally:
        permit._emit_lock.release()

    assert process.exitcode == 0
    assert result_queue.get(timeout=5).startswith(
        "RuntimeError:emission permit belongs to a different process"
    )


@pytest.mark.parametrize("mutation", ["delete", "replace"])
def test_query_delivery_never_rotates_key_for_existing_ledger(
    tmp_path: Path,
    mutation: str,
) -> None:
    controller = _controller(tmp_path)
    assert controller.issue(_request(tmp_path)).status == "issued"
    key_path = tmp_path / "state" / "query-delivery-installation-key-v1"
    if mutation == "delete":
        key_path.unlink()
    else:
        key_path.write_text("1" * 64, encoding="ascii")
        key_path.chmod(0o600)

    replacement = _controller(tmp_path)

    assert replacement.issue(_request(tmp_path)).status == "failed"
    ledger = tmp_path / "state" / "query-delivery-v1.sqlite3"
    with sqlite3.connect(ledger) as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            1,
        )


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_query_delivery_preserves_preexisting_first_init_sidecars(
    tmp_path: Path,
    suffix: str,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    ledger = state_root / "query-delivery-v1.sqlite3"
    sentinel = Path(f"{ledger}{suffix}")
    sentinel.write_bytes(b"outside-sentinel")
    sentinel.chmod(0o600)

    controller = _controller(tmp_path)

    assert controller.issue(_request(tmp_path)).status == "failed"
    assert sentinel.read_bytes() == b"outside-sentinel"
    assert not ledger.exists()


@pytest.mark.parametrize("disappearance_stage", ["require", "chmod"])
def test_query_delivery_tolerates_sqlite_sidecar_disappearance_during_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disappearance_stage: str,
) -> None:
    ledger = tmp_path / "query-delivery-v1.sqlite3"
    ledger.write_bytes(b"ledger")
    ledger.chmod(0o600)
    sidecar = Path(f"{ledger}-wal")
    sidecar.write_bytes(b"ephemeral-sidecar")
    sidecar.chmod(0o600)
    original_require = query_delivery_runtime._require_private_file
    original_chmod = os.chmod
    disappeared = False

    def require_with_disappearance(path: Path) -> None:
        nonlocal disappeared
        if disappearance_stage == "require" and path == sidecar and not disappeared:
            disappeared = True
            sidecar.unlink()
        original_require(path)

    def chmod_with_disappearance(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int,
    ) -> None:
        nonlocal disappeared
        if (
            disappearance_stage == "chmod"
            and Path(os.fsdecode(path)) == sidecar
            and not disappeared
        ):
            disappeared = True
            sidecar.unlink()
        original_chmod(path, mode)

    monkeypatch.setattr(
        query_delivery_runtime,
        "_require_private_file",
        require_with_disappearance,
    )
    monkeypatch.setattr(query_delivery_runtime.os, "chmod", chmod_with_disappearance)

    query_delivery_runtime._secure_sqlite_files(ledger)

    assert disappeared is True
    assert not sidecar.exists()


def test_query_delivery_emergency_legacy_override_wins_without_state(tmp_path: Path) -> None:
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=tmp_path / "state",
        decision_factory=_process_reviewed_factory,
        environment={"CTX_FORCE_LEGACY": "true"},
    )

    assert controller.issue(_request(tmp_path)).status == "legacy"
    assert not (tmp_path / "state").exists()


def test_query_delivery_fails_soft_for_symlinked_or_hardlinked_state(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink_root = tmp_path / "symlink-state"
    try:
        symlink_root.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    symlinked = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=symlink_root,
        decision_factory=_process_reviewed_factory,
        environment={},
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert symlinked.issue(_process_request(workspace)).status == "failed"
    assert tuple(outside.iterdir()) == ()

    regular_root = tmp_path / "regular"
    regular = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=regular_root,
        decision_factory=_process_reviewed_factory,
        environment={},
    )
    assert regular.issue(_process_request(workspace)).status == "issued"
    ledger = regular_root / "query-delivery-v1.sqlite3"
    alias = tmp_path / "ledger-hardlink"
    try:
        os.link(ledger, alias)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"hard links are unavailable: {error}")
    other_request = SensitiveQueryInput(
        native_session_id="other-session",
        logical_prompt_id="turn-1",
        workspace=workspace,
        prompt="Review another implementation",
        language="python",
    )
    assert regular.issue(other_request).status == "failed"


def test_query_delivery_rejects_insecure_state_root_before_managed_writes(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX mode contract")
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o755)
    controller = QueryDeliveryController._for_testing(
        host=QueryHostDescriptor.codex(),
        mode="recommend",
        state_root=state_root,
        decision_factory=_process_reviewed_factory,
        environment={},
    )

    assert controller.issue(_request(tmp_path)).status == "failed"
    assert tuple(state_root.iterdir()) == ()


def test_query_delivery_rejects_symlink_workspace_aliases(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real-workspace"
    real_workspace.mkdir()
    alias_workspace = tmp_path / "workspace-alias"
    try:
        alias_workspace.symlink_to(real_workspace, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks are unavailable: {error}")
    controller = _controller(tmp_path / "controller")
    common = {
        "native_session_id": "session-shared-by-aliases",
        "logical_prompt_id": "turn-1",
        "prompt": "Review the implementation",
        "language": "python",
    }

    first = controller.issue(SensitiveQueryInput(workspace=real_workspace, **common))
    with pytest.raises(ValueError, match="symlinks"):
        SensitiveQueryInput(workspace=alias_workspace, **common)

    assert first.status == "issued"


def test_query_delivery_rejects_wrong_host_or_factory_failure_values(tmp_path: Path) -> None:
    wrong_host = QueryHostDescriptor.claude_code()

    def wrong(**kwargs: object) -> CommittedQueryDecision:
        invocation = kwargs["host_invocation_digest"]
        assert isinstance(invocation, str)
        return _reviewed_decision(wrong_host, host_invocation_digest=invocation)

    wrong_controller = _controller(tmp_path / "wrong", factory=wrong)
    failure_calls = 0

    def closed_failure(**_kwargs: object) -> QueryDecisionFailure:
        nonlocal failure_calls
        failure_calls += 1
        return QueryDecisionFailure(failure_code="catalog-open-failed")

    failed_controller = _controller(
        tmp_path / "failure",
        factory=closed_failure,
    )

    assert wrong_controller.issue(_request(tmp_path / "wrong")).status == "failed"
    assert failed_controller.issue(_request(tmp_path / "failure")).status == "failed"
    assert failed_controller.issue(_request(tmp_path / "failure")).status == "already-terminal"
    assert failure_calls == 1
    assert _attempt_children(tmp_path / "failure" / "state") == ()
    with sqlite3.connect(
        tmp_path / "failure" / "state" / "query-delivery-v1.sqlite3"
    ) as connection:
        assert connection.execute("SELECT COUNT(*) FROM query_delivery_terminals").fetchone() == (
            1,
        )


def test_query_delivery_public_constructor_rejects_non_hook_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only Codex and Claude Code"):
        QueryDeliveryController(
            host=QueryHostDescriptor.ctx_run(),
            mode="recommend",
            state_root=tmp_path / "state",
            environment={},
        )


def test_query_delivery_legacy_mode_does_not_create_state_or_execute(tmp_path: Path) -> None:
    called = False

    def factory(**_kwargs: object) -> CommittedQueryDecision:
        nonlocal called
        called = True
        raise AssertionError("legacy must not execute")

    controller = _controller(tmp_path, mode="legacy", factory=factory)

    report = controller.issue(_request(tmp_path))

    assert report.status == "legacy"
    assert report.emission_permit is None
    assert called is False
    assert not (tmp_path / "state").exists()


def test_sensitive_query_input_is_redacted_noncopyable_and_nonserializable(tmp_path: Path) -> None:
    request = _request(tmp_path)

    assert "session-secret-name" not in repr(request)
    assert "sk-live-secret" not in repr(request)
    with pytest.raises(TypeError):
        copy.copy(request)
    with pytest.raises(TypeError):
        copy.deepcopy(request)
    with pytest.raises(TypeError):
        pickle.dumps(request)
