from __future__ import annotations

import copy
import json
import pickle
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import FrozenInstanceError, asdict, fields, replace
from pathlib import Path

import pytest

from ctx.engine.installation import (
    CommittedInstallDecisionEvidence,
    CommittedInstallDecisionEvidenceProvider,
    InstallDecisionEvidenceQuery,
    InstallDecisionEvidenceRejected,
)
from ctx.engine.engine import CtxEngine
from ctx.engine.protocol import EngineEvent
from ctx.engine.store import SQLiteEngineStore, StreamId
from tests.engine import test_engine_install_coordinator as install_support


def _pending_decision(
    tmp_path: Path,
) -> tuple[
    SQLiteEngineStore,
    CtxEngine,
    EngineEvent,
    InstallDecisionEvidenceQuery,
]:
    store = SQLiteEngineStore(tmp_path / "engine" / "journal.sqlite3")
    engine, policy = install_support._engine(
        tmp_path / "fixture",
        decision=None,
        store=store,
    )
    snapshot = engine.snapshot(install_support._scope())
    assert snapshot.state is not None
    assert len(snapshot.state.pending_consents) == 1
    pending = snapshot.state.pending_consents[0]
    action = pending.install_action
    event = install_support._event(
        "UserDecision",
        snapshot.state.revision,
        "event-install-decision",
        payload={
            "consent_id": pending.consent_id,
            "decision": "granted",
            "decision_basis": "interactive",
            "policy_snapshot_digest": policy.policy_digest,
            "requested_action_id": action.action_id,
            "requested_action_kind": action.kind,
            "requested_action_content_digest": action.content_digest,
            "requested_action_precondition_revision": action.precondition_revision,
        },
    )
    head = store.load_head(StreamId.from_scope(event.scope))
    assert head.record_digest is not None
    query = InstallDecisionEvidenceQuery(
        scope=event.scope,
        consent_id=pending.consent_id,
        decision="granted",
        decision_basis="interactive",
        policy_snapshot_digest=policy.policy_digest,
        requested_action_id=action.action_id,
        requested_action_kind=action.kind,
        requested_action_content_digest=action.content_digest,
        requested_action_precondition_revision=action.precondition_revision,
        event_id=event.event_id,
        event_content_digest=event.content_digest,
        expected_head_revision=head.revision,
        expected_head_record_digest=head.record_digest,
    )
    return store, engine, event, query


def _committed_decision(
    tmp_path: Path,
) -> tuple[
    SQLiteEngineStore,
    InstallDecisionEvidenceQuery,
    CommittedInstallDecisionEvidence,
]:
    store, engine, event, query = _pending_decision(tmp_path)
    engine.process(event)
    with store.inspect_install_decision(query) as result:
        assert result.status == "committed"
        assert result.evidence is not None
        return store, query, result.evidence


def test_absence_at_exact_head_and_exact_commit_are_distinct_authoritative_results(
    tmp_path: Path,
) -> None:
    store, engine, event, query = _pending_decision(tmp_path)

    with store.inspect_install_decision(query) as absent:
        assert absent.status == "absent-at-expected-head"
        assert absent.evidence is None
        assert absent.observed_head_revision == query.expected_head_revision
        assert absent.observed_head_record_digest == query.expected_head_record_digest

    engine.process(event)
    with store.inspect_install_decision(query) as committed:
        assert committed.status == "committed"
        evidence = committed.evidence
        assert evidence is not None
        assert evidence.scope == query.scope
        assert evidence.event_id == query.event_id
        assert evidence.event_content_digest == query.event_content_digest
        assert evidence.committed_revision == query.requested_action_precondition_revision
        assert evidence.previous_record_digest == query.expected_head_record_digest
        assert committed.observed_head_revision == evidence.committed_revision

    assert store.revalidate_install_decision_evidence(evidence, query=query) is evidence


def test_committed_evidence_is_opaque_process_bound_and_not_caller_constructible(
    tmp_path: Path,
) -> None:
    store, engine, event, query = _pending_decision(tmp_path)

    with pytest.raises(TypeError):
        CommittedInstallDecisionEvidence()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        with store.inspect_install_decision(asdict(query)):  # type: ignore[arg-type]
            pass

    engine.process(event)
    with store.inspect_install_decision(query) as result:
        assert result.status == "committed"
        evidence = result.evidence
        assert evidence is not None

    with pytest.raises((FrozenInstanceError, AttributeError)):
        query.event_id = "substituted"  # type: ignore[misc]
    with pytest.raises(TypeError):
        copy.copy(evidence)
    with pytest.raises(TypeError):
        copy.deepcopy(evidence)
    with pytest.raises(TypeError):
        pickle.dumps(evidence)
    with pytest.raises(TypeError):
        asdict(evidence)
    with pytest.raises((TypeError, ValueError)):
        replace(evidence, decision="denied")

    forged = object.__new__(CommittedInstallDecisionEvidence)
    for evidence_field in fields(evidence):
        object.__setattr__(forged, evidence_field.name, getattr(evidence, evidence_field.name))
    with pytest.raises(InstallDecisionEvidenceRejected, match="exact object"):
        store.revalidate_install_decision_evidence(forged, query=query)


def test_wrong_store_cannot_revalidate_process_bound_evidence(tmp_path: Path) -> None:
    store, engine, event, query = _pending_decision(tmp_path)
    other = SQLiteEngineStore(tmp_path / "other" / "journal.sqlite3")

    engine.process(event)
    with store.inspect_install_decision(query) as result:
        evidence = result.evidence
        assert evidence is not None

    with pytest.raises(InstallDecisionEvidenceRejected):
        other.revalidate_install_decision_evidence(
            evidence,
            query=query,
        )


def test_reopen_rejects_old_process_evidence_but_can_issue_fresh_evidence(tmp_path: Path) -> None:
    store, query, evidence = _committed_decision(tmp_path)
    reopened = SQLiteEngineStore.open_read_only(store.path)

    with pytest.raises(InstallDecisionEvidenceRejected, match="another store process"):
        reopened.revalidate_install_decision_evidence(evidence, query=query)
    with reopened.inspect_install_decision(query) as result:
        assert result.status == "committed"
        assert result.evidence is not None
        fresh = result.evidence
    assert reopened.revalidate_install_decision_evidence(fresh, query=query) is fresh
    assert isinstance(reopened, CommittedInstallDecisionEvidenceProvider)


def test_absence_context_holds_writer_out_until_negative_proof_is_released(
    tmp_path: Path,
) -> None:
    store, engine, event, query = _pending_decision(tmp_path)
    started = threading.Event()

    def commit_decision() -> object:
        started.set()
        return engine.process(event)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.inspect_install_decision(query) as result:
            assert result.status == "absent-at-expected-head"
            future = pool.submit(commit_decision)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            assert store.load_head(StreamId.from_scope(query.scope)).revision == (
                query.expected_head_revision
            )
        future.result(timeout=5)

    with store.inspect_install_decision(query) as result:
        assert result.status == "committed"


def test_absent_event_after_intervening_commit_reports_head_advanced(tmp_path: Path) -> None:
    store, engine, _event, query = _pending_decision(tmp_path)
    intervening = install_support._event(
        "TurnStarting",
        query.expected_head_revision,
        "event-intervening",
    )
    engine.process(intervening)

    with store.inspect_install_decision(query) as result:
        assert result.status == "head-advanced"
        assert result.evidence is None
        assert result.observed_head_revision == query.expected_head_revision + 1
        assert result.observed_head_record_digest != query.expected_head_record_digest


def test_event_id_or_content_collision_never_issues_evidence(tmp_path: Path) -> None:
    store, engine, _event, query = _pending_decision(tmp_path)
    collision = install_support._event(
        "TurnStarting",
        query.expected_head_revision,
        query.event_id,
    )
    engine.process(collision)

    with store.inspect_install_decision(query) as result:
        assert result.status == "event-collision"
        assert result.evidence is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"consent_id": "other-consent"},
        {"decision": "denied"},
        {"decision_basis": "preapproved-policy"},
        {"policy_snapshot_digest": "9" * 64},
        {"requested_action_id": "other-action"},
        {"requested_action_content_digest": "8" * 64},
        {"event_id": "other-event"},
        {"event_content_digest": "7" * 64},
        {"expected_head_record_digest": "6" * 64},
    ],
)
def test_query_substitution_never_revalidates_committed_evidence(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    store, query, evidence = _committed_decision(tmp_path)
    changed = replace(query, **mutation)  # type: ignore[arg-type]

    with store.inspect_install_decision(changed) as result:
        assert result.status != "committed"
        assert result.evidence is None
    with pytest.raises(InstallDecisionEvidenceRejected, match="exact query"):
        store.revalidate_install_decision_evidence(evidence, query=changed)


def test_scope_revision_and_action_kind_substitutions_fail_closed(tmp_path: Path) -> None:
    store, query, evidence = _committed_decision(tmp_path)
    changed_scope = replace(query.scope, exposure_id="other-exposure")
    scope_query = replace(query, scope=changed_scope)
    revision_query = replace(
        query,
        expected_head_revision=query.expected_head_revision + 1,
        requested_action_precondition_revision=query.requested_action_precondition_revision + 1,
    )

    for changed in (scope_query, revision_query):
        with store.inspect_install_decision(changed) as result:
            assert result.status != "committed"
            assert result.evidence is None
        with pytest.raises(InstallDecisionEvidenceRejected):
            store.revalidate_install_decision_evidence(evidence, query=changed)
    with pytest.raises(ValueError, match="InstallCapability"):
        replace(query, requested_action_kind="UninstallCapability")


def test_semantically_corrupt_state_chain_returns_corrupt_not_authority(tmp_path: Path) -> None:
    store, query, _evidence = _committed_decision(tmp_path)
    stream_id = StreamId.from_scope(query.scope)
    record = tuple(store.records(stream_id))[-1]
    invalid_state_json = json.dumps(
        {"revision": record.revision},
        sort_keys=True,
        separators=(",", ":"),
    )
    tampered = replace(
        record,
        result_state_json=invalid_state_json,
        result_state_digest="",
        record_digest="",
    ).bind_chain(record.previous_record_digest)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            """
            UPDATE engine_journal
               SET result_state_json = ?, result_state_digest = ?, record_digest = ?
             WHERE event_id = ?
            """,
            (
                tampered.result_state_json,
                tampered.result_state_digest,
                tampered.record_digest,
                tampered.event_id,
            ),
        )
        connection.execute(
            """
            UPDATE engine_streams
               SET state_json = ?, state_digest = ?, head_record_digest = ?
             WHERE stream_key = ?
            """,
            (
                tampered.result_state_json,
                tampered.result_state_digest,
                tampered.record_digest,
                stream_id.key,
            ),
        )

    with store.inspect_install_decision(query) as result:
        assert result.status == "corrupt"
        assert result.evidence is None


def test_locked_or_read_only_store_reports_unavailable_not_negative_authority(
    tmp_path: Path,
) -> None:
    store, _engine, _event, query = _pending_decision(tmp_path)
    contender = SQLiteEngineStore(store.path, busy_timeout_ms=25)
    with sqlite3.connect(store.path, isolation_level=None) as lock_connection:
        lock_connection.execute("BEGIN IMMEDIATE")
        with contender.inspect_install_decision(query) as result:
            assert result.status == "unavailable"
            assert result.evidence is None
        lock_connection.execute("ROLLBACK")

    read_only = SQLiteEngineStore.open_read_only(store.path)
    with read_only.inspect_install_decision(query) as result:
        assert result.status == "unavailable"
        assert result.evidence is None
