from __future__ import annotations

import copy
import hashlib
import pickle
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ctx.engine.content import PreparedCapabilityContent
from ctx.engine.engine import _PromptContextReceiptPermit
from ctx.engine.protocol import Transition
from ctx.engine.replay import ReplayInput
from ctx.runtime.prepared_query_delivery import (
    PreparedQueryDelivery,
    PreparedQueryDeliveryError,
    _create_prepared_query_delivery,
    accept_prepared_query_delivery,
)
from ctx.runtime.query_decision import QueryHostDescriptor
from ctx.runtime.query_session import prepare_query_delivery


def _delivery(tmp_path: Path) -> PreparedQueryDelivery:
    result = prepare_query_delivery(
        host=QueryHostDescriptor.codex("activate"),
        task="Fix the Python tests",
        language="python",
        session_id="prepared-delivery-test",
        workspace=tmp_path,
        journal_path=tmp_path / "engine.sqlite3",
        benefit_audit_path=tmp_path / "benefit.sqlite3",
        host_invocation_digest=hashlib.sha256(b"host-invocation").hexdigest(),
    )
    assert isinstance(result, PreparedQueryDelivery)
    return result


def test_prepared_query_delivery_accepts_a_defensive_exact_copy(tmp_path: Path) -> None:
    original = _delivery(tmp_path)

    accepted = accept_prepared_query_delivery(
        original,
        host=QueryHostDescriptor.codex("activate"),
    )

    assert accepted is not original
    assert accepted.delivery_digest == original.delivery_digest
    assert accepted.context == original.context
    assert repr(accepted) == (
        f"PreparedQueryDelivery(delivery_digest={accepted.delivery_digest!r})"
    )
    assert "# ctx Python Testing" not in repr(accepted)
    with pytest.raises(PreparedQueryDeliveryError, match="already accepted"):
        accept_prepared_query_delivery(
            original,
            host=QueryHostDescriptor.codex("activate"),
        )
    with pytest.raises(PreparedQueryDeliveryError, match="already accepted"):
        accept_prepared_query_delivery(
            accepted,
            host=QueryHostDescriptor.codex("activate"),
        )


@pytest.mark.parametrize(
    "host",
    [
        QueryHostDescriptor.claude_code("activate"),
        QueryHostDescriptor.codex("experiment"),
        QueryHostDescriptor.codex(),
    ],
)
def test_prepared_query_delivery_rejects_wrong_host_or_intent(
    tmp_path: Path,
    host: QueryHostDescriptor,
) -> None:
    delivery = _delivery(tmp_path)

    with pytest.raises(PreparedQueryDeliveryError):
        accept_prepared_query_delivery(delivery, host=host)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("context", "substituted material"),
        ("context_sha256", hashlib.sha256(b"substituted").hexdigest()),
        ("final_journal_record_digest", hashlib.sha256(b"other journal").hexdigest()),
        ("action_content_digest", hashlib.sha256(b"other action").hexdigest()),
    ],
)
def test_prepared_query_delivery_rejects_substitution(
    tmp_path: Path,
    field_name: str,
    replacement: str,
) -> None:
    original = _delivery(tmp_path)

    with pytest.raises(PreparedQueryDeliveryError):
        replace(original, **{field_name: replacement})  # type: ignore[arg-type]


def test_prepared_query_delivery_rejects_copy_and_serialization(tmp_path: Path) -> None:
    delivery = _delivery(tmp_path)

    with pytest.raises(TypeError):
        copy.copy(delivery)
    with pytest.raises(TypeError):
        copy.deepcopy(delivery)
    with pytest.raises(TypeError):
        pickle.dumps(delivery)


def test_prepared_query_delivery_rejects_a_forged_engine_receipt_permit(
    tmp_path: Path,
) -> None:
    delivery = _delivery(tmp_path)
    with sqlite3.connect(tmp_path / "engine.sqlite3") as connection:
        rev2 = Transition.from_json(
            connection.execute(
                "SELECT transition_json FROM engine_journal WHERE revision = 2"
            ).fetchone()[0]
        )
        replay3 = ReplayInput.from_json(
            connection.execute(
                "SELECT replay_json FROM engine_journal WHERE revision = 3"
            ).fetchone()[0]
        )
    action = next(item for item in rev2.actions if item.kind == "PreparePromptContext")
    metadata = delivery.capabilities[0]
    raw_content = delivery.context.split(">\n", 1)[1].rsplit(
        "\n</ctx-capability>",
        1,
    )[0]
    content = PreparedCapabilityContent(
        capability_id=metadata.capability_id,
        source_digest=metadata.source_digest,
        catalog_snapshot_digest=action.catalog_snapshot_id or "",
        action_id=action.action_id,
        lease_id=action.lease_id or "",
        content=raw_content,
        content_sha256=metadata.content_sha256,
        content_bytes=metadata.content_bytes,
        estimated_tokens=metadata.estimated_tokens,
    )
    forged_permit = object.__new__(_PromptContextReceiptPermit)

    with pytest.raises(
        PreparedQueryDeliveryError,
        match="authoritative revision-three receipt",
    ):
        _create_prepared_query_delivery(
            decision=delivery.decision,
            execution_intent="activate",
            action=action,
            receipt_event=replay3.reducer_event,
            contents=(content,),
            receipt_authority=forged_permit,
        )

    malicious_body = "arbitrary caller-supplied prompt instructions"
    malicious = replace(
        content,
        content=malicious_body,
        content_sha256=hashlib.sha256(malicious_body.encode()).hexdigest(),
        content_bytes=len(malicious_body.encode()),
    )
    with pytest.raises(PreparedQueryDeliveryError, match="changed authorized material"):
        _create_prepared_query_delivery(
            decision=delivery.decision,
            execution_intent="activate",
            action=action,
            receipt_event=replay3.reducer_event,
            contents=(malicious,),
            receipt_authority=forged_permit,
        )

    with pytest.raises(TypeError, match="final_journal_record_digest"):
        _create_prepared_query_delivery(
            decision=delivery.decision,
            execution_intent="activate",
            action=action,
            receipt_event=replay3.reducer_event,
            contents=(content,),
            receipt_authority=forged_permit,
            final_journal_record_digest=hashlib.sha256(b"fake final record").hexdigest(),  # type: ignore[call-arg]
        )


def test_prepared_query_delivery_acceptance_is_concurrently_one_shot(
    tmp_path: Path,
) -> None:
    delivery = _delivery(tmp_path)
    host = QueryHostDescriptor.codex("activate")

    def accept_once() -> str:
        try:
            accept_prepared_query_delivery(delivery, host=host)
        except PreparedQueryDeliveryError:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=16) as executor:
        outcomes = tuple(executor.map(lambda _index: accept_once(), range(64)))

    assert outcomes.count("accepted") == 1
    assert outcomes.count("rejected") == 63
