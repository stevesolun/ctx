from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

import ctx.runtime.query_session as query_session_module
import ctx.runtime.production_catalog as production_catalog_module
import ctx.runtime.release_material as release_material_module
from ctx.runtime.release_material import RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE
from ctx.runtime.production_catalog import (
    RELEASE_QUERY_CATALOG_ROOT_SHA256,
    RELEASE_QUERY_CATALOG_SEQUENCE,
)
from ctx.runtime.prepared_query_delivery import (
    PreparedQueryDelivery,
    accept_prepared_query_delivery,
)
from ctx.runtime.query_decision import CommittedQueryDecision, QueryHostDescriptor
from ctx.runtime.query_decision import prepare_query_decision
from ctx.runtime.query_session import (
    CtxRunQueryDecision,
    QueryDecisionFailure,
    prepare_query_delivery,
    prepare_ctx_run_query_decision,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_release_query_session_commits_private_abstention_before_returning(
    tmp_path: Path,
) -> None:
    task = "Repair private API token=do-not-journal at /private/repository/path"
    journal = tmp_path / "engine" / "session.engine.sqlite3"
    audit = tmp_path / "engine" / "session.benefit.sqlite3"

    decision = prepare_ctx_run_query_decision(
        task=task,
        language="Python",
        session_id="session-1",
        workspace=tmp_path,
        journal_path=journal,
        benefit_audit_path=audit,
    )

    assert isinstance(decision, CtxRunQueryDecision)
    assert decision.status == "abstained"
    assert decision.recommendation_context is None
    assert decision.recommendation_count == 0
    assert decision.journal_revision == 2
    assert decision.release_root_digest == (RELEASE_QUERY_CATALOG_ROOT_SHA256)
    assert decision.release_sequence == RELEASE_QUERY_CATALOG_SEQUENCE
    assert decision.catalog_mode == "reviewed"
    assert decision.failure_code is None
    assert decision.plan_digest is not None and len(decision.plan_digest) == 64
    assert decision.journal_record_digest is not None
    assert journal.is_file()
    assert audit.is_file()
    journal_bytes = journal.read_bytes()
    assert task.encode() not in journal_bytes
    assert b"do-not-journal" not in journal_bytes
    assert str(tmp_path).encode() not in journal_bytes


def test_release_query_session_commits_exact_reviewed_skill_without_material(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "engine" / "positive.engine.sqlite3"
    audit = tmp_path / "engine" / "positive.benefit.sqlite3"

    decision = prepare_ctx_run_query_decision(
        task="Fix the Python tests",
        language="",
        session_id="session-positive",
        workspace=tmp_path,
        journal_path=journal,
        benefit_audit_path=audit,
    )

    assert isinstance(decision, CtxRunQueryDecision)
    assert decision.status == "presented"
    assert tuple(item.capability_id for item in decision.capabilities) == (
        "skill:ctx-python-testing",
    )
    assert decision.journal_revision == 2
    assert decision.recommendation_context is not None
    assert "ctx-python-testing" in decision.recommendation_context
    assert "# ctx Python Testing" not in decision.recommendation_context
    for store in (journal, audit):
        for candidate in (store, *tuple(store.parent.glob(f"{store.name}-*"))):
            if candidate.is_file():
                assert b"# ctx Python Testing" not in candidate.read_bytes()


def test_explicit_query_session_prepares_exact_reviewed_skill_at_revision_three(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "engine" / "explicit.engine.sqlite3"
    audit = tmp_path / "engine" / "explicit.benefit.sqlite3"
    invocation_digest = _digest("explicit-host-invocation")

    result = prepare_query_delivery(
        host=QueryHostDescriptor.codex("activate"),
        task="Fix the Python tests",
        language="",
        session_id="session-explicit-positive",
        workspace=tmp_path,
        journal_path=journal,
        benefit_audit_path=audit,
        host_invocation_digest=invocation_digest,
    )

    assert isinstance(result, PreparedQueryDelivery)
    delivery = accept_prepared_query_delivery(
        result,
        host=QueryHostDescriptor.codex("activate"),
    )
    assert delivery.execution_intent == "activate"
    assert delivery.final_journal_revision == 3
    assert delivery.decision.host_invocation_digest == invocation_digest
    assert tuple(item.capability_id for item in delivery.capabilities) == (
        "skill:ctx-python-testing",
    )
    assert "# ctx Python Testing" in delivery.context
    assert hashlib.sha256(delivery.context.encode()).hexdigest() == delivery.context_sha256
    assert len(delivery.context.encode()) == delivery.context_bytes

    raw_material = b"# ctx Python Testing"
    for store in (journal, audit):
        for candidate in (store, *tuple(store.parent.glob(f"{store.name}-*"))):
            if candidate.is_file():
                assert raw_material not in candidate.read_bytes()


def test_loading_reviewed_skill_does_not_read_install_only_asset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    decoded_capability_ids: list[str] = []
    resource_reads: list[str] = []
    original = release_material_module.ReleasePinnedSkillMaterialSource._content
    original_read = production_catalog_module._read_resource

    def observed_content(source: object, capability_id: str, expected_path: str) -> str:
        decoded_capability_ids.append(capability_id)
        return original(source, capability_id, expected_path)  # type: ignore[arg-type]

    monkeypatch.setattr(
        release_material_module.ReleasePinnedSkillMaterialSource,
        "_content",
        observed_content,
    )

    def observed_read(name: str, *, maximum_bytes: int) -> bytes:
        resource_reads.append(name)
        return original_read(name, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(production_catalog_module, "_read_resource", observed_read)

    result = prepare_query_delivery(
        host=QueryHostDescriptor.codex("activate"),
        task="Fix the Python tests",
        language="",
        session_id="session-material-isolation",
        workspace=tmp_path,
        journal_path=tmp_path / "engine" / "isolated.engine.sqlite3",
        benefit_audit_path=tmp_path / "engine" / "isolated.benefit.sqlite3",
        host_invocation_digest=_digest("material-isolation"),
    )

    assert isinstance(result, PreparedQueryDelivery)
    assert decoded_capability_ids == ["skill:ctx-python-testing"]
    assert RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE not in resource_reads


def test_explicit_query_session_preserves_bounded_abstention(
    tmp_path: Path,
) -> None:
    result = prepare_query_delivery(
        host=QueryHostDescriptor.claude_code("activate"),
        task="Fix the JavaScript tests",
        language="javascript",
        session_id="session-explicit-abstention",
        workspace=tmp_path,
        journal_path=tmp_path / "engine" / "abstain.engine.sqlite3",
        benefit_audit_path=tmp_path / "engine" / "abstain.benefit.sqlite3",
        host_invocation_digest=_digest("explicit-abstaining-host-invocation"),
    )

    assert isinstance(result, CommittedQueryDecision)
    assert result.status == "abstained"
    assert result.journal_revision == 2
    assert result.capabilities == ()


def test_advisory_factory_rejects_explicit_intent_without_creating_state(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "engine" / "wrong-factory.engine.sqlite3"
    audit = tmp_path / "engine" / "wrong-factory.benefit.sqlite3"

    result = prepare_query_decision(
        host=QueryHostDescriptor.codex("activate"),
        task="Fix the Python tests",
        language="python",
        session_id="wrong-explicit-factory",
        workspace=tmp_path,
        journal_path=journal,
        benefit_audit_path=audit,
        host_invocation_digest=_digest("wrong-explicit-factory"),
    )

    assert result == QueryDecisionFailure(failure_code="explicit-factory-required")
    assert not journal.exists()
    assert not audit.exists()


def test_experiment_requires_sealed_authorization_before_creating_state(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "engine" / "experiment.engine.sqlite3"
    audit = tmp_path / "engine" / "experiment.benefit.sqlite3"

    result = prepare_query_delivery(
        host=QueryHostDescriptor.claude_code("experiment"),
        task="Fix the Python tests",
        language="python",
        session_id="unauthorized-experiment",
        workspace=tmp_path,
        journal_path=journal,
        benefit_audit_path=audit,
        host_invocation_digest=_digest("unauthorized-experiment"),
    )

    assert result == QueryDecisionFailure(failure_code="experiment-authorization-required")
    assert not journal.exists()
    assert not audit.exists()


def test_query_session_returns_only_a_stable_failure_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "token=private-value /private/repository/path"

    def fail_catalog_open():  # type: ignore[no-untyped-def]
        raise RuntimeError(secret)

    monkeypatch.setattr(
        query_session_module,
        "open_release_pinned_query_catalog",
        fail_catalog_open,
    )

    decision = prepare_ctx_run_query_decision(
        task="testing",
        language="Python",
        session_id="session-1",
        workspace=tmp_path,
        journal_path=tmp_path / "engine" / "session.engine.sqlite3",
        benefit_audit_path=tmp_path / "engine" / "session.benefit.sqlite3",
    )

    assert decision == QueryDecisionFailure(failure_code="catalog-open-failed")
    assert secret not in repr(decision)
    assert "private-value" not in repr(decision)


def test_query_session_closes_registered_authorities_on_base_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = Mock()
    catalog = Mock(release_sequence=1, mode="reviewed")
    interrupt = KeyboardInterrupt("stop")
    catalog.prepare_query.side_effect = interrupt
    monkeypatch.setattr(
        query_session_module,
        "QueryObservationRegistry",
        lambda *, provider_id: registry,
    )
    monkeypatch.setattr(
        query_session_module,
        "open_release_pinned_query_catalog",
        lambda: catalog,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        prepare_ctx_run_query_decision(
            task="testing",
            language="Python",
            session_id="session-1",
            workspace=tmp_path,
            journal_path=tmp_path / "engine" / "session.engine.sqlite3",
            benefit_audit_path=tmp_path / "engine" / "session.benefit.sqlite3",
        )

    assert raised.value is interrupt
    registry.close.assert_called_once_with()
    catalog.close.assert_called_once_with()


def test_query_session_rejects_lexically_aliased_store_paths_before_io(
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "engine-state"
    journal = storage_root / "state.sqlite3"
    audit = storage_root / "subdirectory" / ".." / "state.sqlite3"
    assert journal != audit
    assert journal.is_absolute() and audit.is_absolute()
    assert os.path.abspath(journal) == os.path.abspath(audit)

    with pytest.raises(ValueError):
        prepare_ctx_run_query_decision(
            task="testing",
            language="Python",
            session_id="session-1",
            workspace=tmp_path,
            journal_path=journal,
            benefit_audit_path=audit,
        )

    assert not storage_root.exists()


def test_query_session_rejects_hardlinked_store_paths_before_processing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "hardlinked-state"
    storage_root.mkdir()
    journal = storage_root / "journal.sqlite3"
    audit = storage_root / "benefit.sqlite3"
    journal.touch()
    try:
        os.link(journal, audit)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"hard links are unavailable: {error}")
    catalog_open = Mock(wraps=query_session_module.open_release_pinned_query_catalog)
    monkeypatch.setattr(
        query_session_module,
        "open_release_pinned_query_catalog",
        catalog_open,
    )

    with pytest.raises(ValueError):
        prepare_ctx_run_query_decision(
            task="testing",
            language="Python",
            session_id="session-1",
            workspace=tmp_path,
            journal_path=journal,
            benefit_audit_path=audit,
        )

    catalog_open.assert_not_called()
    assert journal.read_bytes() == b""
    assert audit.read_bytes() == b""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task", object()),
        ("language", object()),
        ("session_id", ""),
        ("workspace", "not-a-path"),
        ("journal_path", "not-a-path"),
        ("benefit_audit_path", "not-a-path"),
    ],
)
def test_query_session_rejects_invalid_host_inputs(
    field: str,
    value: object,
    tmp_path: Path,
) -> None:
    arguments: dict[str, object] = {
        "task": "testing",
        "language": "Python",
        "session_id": "session-1",
        "workspace": tmp_path,
        "journal_path": tmp_path / "engine" / "session.engine.sqlite3",
        "benefit_audit_path": tmp_path / "engine" / "session.benefit.sqlite3",
    }
    arguments[field] = value

    with pytest.raises((TypeError, ValueError)):
        prepare_ctx_run_query_decision(**arguments)  # type: ignore[arg-type]


def test_query_failure_rejects_sensitive_fields() -> None:
    with pytest.raises(ValueError):
        QueryDecisionFailure(failure_code="failure contains /private/path")
