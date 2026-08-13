"""Sealed, host-neutral delivery for exact ephemeral capability context."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from weakref import WeakKeyDictionary

from ctx.engine.capability_schema import MAX_HOST_CONTEXT_CHARS
from ctx.engine.content import PreparedCapabilityContent
from ctx.engine.engine import CtxEngineError, _PromptContextReceiptPermit
from ctx.engine.protocol import (
    PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
    EngineEvent,
    HostAction,
)
from ctx.runtime.query_decision import (
    CommittedQueryDecision,
    QueryDecisionValidationError,
    QueryHostDescriptor,
    accept_query_decision,
)


_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,255}\Z")
_INTENTS = frozenset({"activate", "experiment"})
_MAX_CONTEXT_BYTES = 32_768


class PreparedQueryDeliveryError(ValueError):
    """An ephemeral prepared delivery failed its exact binding contract."""


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PreparedQueryDeliveryError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _required_token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise PreparedQueryDeliveryError(f"{field_name} must be a canonical token")
    return value


def _unexpired_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise PreparedQueryDeliveryError("expires_at must be a UTC timestamp")
    try:
        expires_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PreparedQueryDeliveryError("expires_at must be a UTC timestamp") from None
    if (
        expires_at.tzinfo is None
        or not value.endswith("Z")
        or datetime.now(UTC) >= expires_at.astimezone(UTC)
    ):
        raise PreparedQueryDeliveryError("prepared delivery authority has expired")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedDeliveryCapability:
    """Digest-only metadata for one raw capability body in the delivery."""

    capability_id: str
    source_digest: str
    content_sha256: str
    content_bytes: int
    estimated_tokens: int

    def __post_init__(self) -> None:
        _required_token(self.capability_id, "capability_id")
        _required_digest(self.source_digest, "source_digest")
        _required_digest(self.content_sha256, "content_sha256")
        if type(self.content_bytes) is not int or not 1 <= self.content_bytes <= 6_000:
            raise PreparedQueryDeliveryError("content_bytes is outside its bounded range")
        if type(self.estimated_tokens) is not int or not 0 <= self.estimated_tokens <= 1_500:
            raise PreparedQueryDeliveryError("estimated_tokens is outside its bounded range")

    def to_dict(self) -> dict[str, str | int]:
        return {
            "capability_id": self.capability_id,
            "content_bytes": self.content_bytes,
            "content_sha256": self.content_sha256,
            "estimated_tokens": self.estimated_tokens,
            "source_digest": self.source_digest,
        }


class _DeliverySeal:
    __slots__ = ("__weakref__",)


_SEALS: WeakKeyDictionary[_DeliverySeal, tuple[str, bool]] = WeakKeyDictionary()
_SEALS_LOCK = Lock()


def _issue_seal(delivery_digest: str, *, accept_once: bool = True) -> _DeliverySeal:
    seal = _DeliverySeal()
    with _SEALS_LOCK:
        _SEALS[seal] = (delivery_digest, accept_once)
    return seal


def _seal_matches(value: object, delivery_digest: str) -> bool:
    if type(value) is not _DeliverySeal:
        return False
    with _SEALS_LOCK:
        record = _SEALS.get(value)
        return record is not None and record[0] == delivery_digest


def _consume_seal(value: object, delivery_digest: str) -> bool:
    if type(value) is not _DeliverySeal:
        return False
    with _SEALS_LOCK:
        record = _SEALS.get(value)
        if record != (delivery_digest, True):
            return False
        _SEALS[value] = (delivery_digest, False)
        return True


def _delivery_digest(
    *,
    decision: CommittedQueryDecision,
    execution_intent: str,
    action_id: str,
    action_content_digest: str,
    action_precondition_revision: int,
    receipt_event_content_digest: str,
    final_journal_revision: int,
    final_journal_record_digest: str,
    expires_at: str,
    context_sha256: str,
    context_bytes: int,
    capabilities: tuple[PreparedDeliveryCapability, ...],
) -> str:
    return _digest(
        {
            "action_content_digest": action_content_digest,
            "action_id": action_id,
            "action_precondition_revision": action_precondition_revision,
            "capabilities": [item.to_dict() for item in capabilities],
            "context_bytes": context_bytes,
            "context_sha256": context_sha256,
            "decision_receipt_digest": decision.receipt_digest,
            "execution_intent": execution_intent,
            "expires_at": expires_at,
            "final_journal_record_digest": final_journal_record_digest,
            "final_journal_revision": final_journal_revision,
            "host_context_id": decision.host_context_id,
            "host_descriptor_digest": decision.host_descriptor_digest,
            "host_invocation_digest": decision.host_invocation_digest,
            "receipt_event_content_digest": receipt_event_content_digest,
            "schema": "ctx.prepared-query-delivery-v1",
            "work_signature_digest": decision.work_signature_digest,
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class PreparedQueryDelivery:
    """One process-bound exact context prepared after a journaled action."""

    decision: CommittedQueryDecision
    execution_intent: str
    action_id: str
    action_content_digest: str
    action_precondition_revision: int
    receipt_event_content_digest: str
    final_journal_revision: int
    final_journal_record_digest: str
    expires_at: str
    context: str = field(repr=False, compare=False)
    context_sha256: str
    context_bytes: int
    capabilities: tuple[PreparedDeliveryCapability, ...]
    delivery_digest: str
    _pid: int = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.decision) is not CommittedQueryDecision:
            raise PreparedQueryDeliveryError("decision must be an exact committed receipt")
        if self.execution_intent not in _INTENTS:
            raise PreparedQueryDeliveryError("execution_intent is unsupported")
        host = QueryHostDescriptor._for(
            self.decision.host_context_id,
            self.execution_intent,
        )
        try:
            accepted = accept_query_decision(self.decision, host=host)
        except QueryDecisionValidationError:
            raise PreparedQueryDeliveryError("decision host provenance is invalid") from None
        if type(accepted) is not CommittedQueryDecision or not accepted.capabilities:
            raise PreparedQueryDeliveryError("prepared delivery requires a positive decision")
        _required_token(self.action_id, "action_id")
        _required_digest(self.action_content_digest, "action_content_digest")
        _required_digest(self.receipt_event_content_digest, "receipt_event_content_digest")
        _required_digest(self.final_journal_record_digest, "final_journal_record_digest")
        _unexpired_timestamp(self.expires_at)
        if self.action_precondition_revision != 2 or self.final_journal_revision != 3:
            raise PreparedQueryDeliveryError("prepared delivery must bind revisions two and three")
        if not isinstance(self.context, str) or not self.context or "\x00" in self.context:
            raise PreparedQueryDeliveryError("prepared context must be non-empty NUL-free text")
        encoded = self.context.encode("utf-8")
        _required_digest(self.context_sha256, "context_sha256")
        if (
            hashlib.sha256(encoded).hexdigest() != self.context_sha256
            or len(encoded) != self.context_bytes
            or not 1 <= self.context_bytes <= _MAX_CONTEXT_BYTES
        ):
            raise PreparedQueryDeliveryError("prepared context does not match its digest and bound")
        if (
            not isinstance(self.capabilities, tuple)
            or not 1 <= len(self.capabilities) <= 5
            or not all(type(item) is PreparedDeliveryCapability for item in self.capabilities)
        ):
            raise PreparedQueryDeliveryError(
                "prepared capabilities must be one exact bounded tuple"
            )
        ids = tuple(item.capability_id for item in self.capabilities)
        if len(ids) != len(set(ids)):
            raise PreparedQueryDeliveryError("prepared capabilities contain duplicates")
        expected = _delivery_digest(
            decision=accepted,
            execution_intent=self.execution_intent,
            action_id=self.action_id,
            action_content_digest=self.action_content_digest,
            action_precondition_revision=self.action_precondition_revision,
            receipt_event_content_digest=self.receipt_event_content_digest,
            final_journal_revision=self.final_journal_revision,
            final_journal_record_digest=self.final_journal_record_digest,
            expires_at=self.expires_at,
            context_sha256=self.context_sha256,
            context_bytes=self.context_bytes,
            capabilities=self.capabilities,
        )
        if self.delivery_digest != expected or not _seal_matches(self._seal, expected):
            raise PreparedQueryDeliveryError("prepared delivery digest or seal is invalid")
        if self._pid != os.getpid():
            raise PreparedQueryDeliveryError("prepared delivery belongs to another process")

    def __repr__(self) -> str:
        return f"PreparedQueryDelivery(delivery_digest={self.delivery_digest!r})"

    def __copy__(self) -> PreparedQueryDelivery:
        raise TypeError("PreparedQueryDelivery cannot be copied")

    def __deepcopy__(self, _memo: object) -> PreparedQueryDelivery:
        raise TypeError("PreparedQueryDelivery cannot be copied")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("PreparedQueryDelivery cannot be serialized")


def render_prepared_prompt_context(
    contents: tuple[PreparedCapabilityContent, ...],
) -> str:
    """Render one host-neutral context without claiming provider consumption."""

    if not contents:
        raise PreparedQueryDeliveryError("prepared prompt context requires material")
    lines = [
        "CTX ephemeral capability context (engine-authorized; issuance is not evidence of use):"
    ]
    for index, item in enumerate(contents, 1):
        lines.extend(
            (
                f'<ctx-capability index="{index}" id="{item.capability_id}" '
                f'sha256="{item.content_sha256}" bytes="{item.content_bytes}">',
                item.content,
                "</ctx-capability>",
            )
        )
    context = "\n".join(lines)
    if len(context.encode("utf-8")) > _MAX_CONTEXT_BYTES or len(context) > MAX_HOST_CONTEXT_CHARS:
        raise PreparedQueryDeliveryError("prepared prompt context exceeds its byte bound")
    return context


def _create_prepared_query_delivery(
    *,
    decision: CommittedQueryDecision,
    execution_intent: str,
    action: HostAction,
    receipt_event: EngineEvent,
    contents: tuple[PreparedCapabilityContent, ...],
    receipt_authority: _PromptContextReceiptPermit,
) -> PreparedQueryDelivery:
    """Seal only an exact applied receipt for the already-prepared context."""

    if action.kind != "PreparePromptContext" or receipt_event.kind != "ActionApplied":
        raise PreparedQueryDeliveryError("prepared delivery requires its exact applied action")
    if type(receipt_authority) is not _PromptContextReceiptPermit:
        raise PreparedQueryDeliveryError("prepared delivery requires journal receipt authority")
    if action.expires_at is None:
        raise PreparedQueryDeliveryError("prepared delivery action lacks an expiry")
    _unexpired_timestamp(action.expires_at)
    if (
        action.scope.host_context_id != decision.host_context_id
        or action.payload.get("plan_digest") != decision.plan_digest
        or action.catalog_snapshot_id != decision.catalog_snapshot_digest
        or action.payload.get("presentation_action_id") != decision.presentation_action_id
        or action.payload.get("presentation_action_content_digest")
        != decision.presentation_action_content_digest
    ):
        raise PreparedQueryDeliveryError("prepared action does not bind the exact decision")
    context = render_prepared_prompt_context(contents)
    encoded = context.encode("utf-8")
    context_sha256 = hashlib.sha256(encoded).hexdigest()
    metadata = tuple(
        PreparedDeliveryCapability(
            capability_id=item.capability_id,
            source_digest=item.source_digest,
            content_sha256=item.content_sha256,
            content_bytes=item.content_bytes,
            estimated_tokens=item.estimated_tokens,
        )
        for item in contents
    )
    raw_action_rows = action.payload.get("capabilities")
    if not isinstance(raw_action_rows, tuple) or len(raw_action_rows) != len(contents):
        raise PreparedQueryDeliveryError("prepared content changed the action bundle")
    for raw_row, item in zip(raw_action_rows, contents, strict=True):
        if not isinstance(raw_row, Mapping):
            raise PreparedQueryDeliveryError("prepared action material row is invalid")
        material = raw_row.get("material_identity")
        authorized = raw_row.get("authorized_material")
        descriptor = (
            authorized.get("catalog_material_descriptor")
            if isinstance(authorized, Mapping)
            else None
        )
        if (
            not isinstance(material, Mapping)
            or not isinstance(descriptor, Mapping)
            or raw_row.get("capability_id") != item.capability_id
            or raw_row.get("source_digest") != item.source_digest
            or material.get("content_sha256") != item.content_sha256
            or material.get("content_bytes") != item.content_bytes
            or descriptor.get("estimated_tokens") != item.estimated_tokens
            or item.catalog_snapshot_digest != action.catalog_snapshot_id
            or item.action_id != action.action_id
            or item.lease_id != action.lease_id
        ):
            raise PreparedQueryDeliveryError("prepared content changed authorized material")
    expected_receipt_identity = {
        "action_id": action.action_id,
        "action_kind": action.kind,
        "action_content_digest": action.content_digest,
        "action_precondition_revision": action.precondition_revision,
    }
    if any(
        receipt_event.payload.get(key) != value for key, value in expected_receipt_identity.items()
    ):
        raise PreparedQueryDeliveryError("receipt event does not bind the exact action")
    verification = receipt_event.payload.get("verification")
    expected_capabilities = [
        {
            "capability_id": item.capability_id,
            "content_sha256": item.content_sha256,
            "content_bytes": item.content_bytes,
        }
        for item in metadata
    ]
    if not isinstance(verification, Mapping):
        raise PreparedQueryDeliveryError("receipt verification is invalid")
    if dict(verification) != {
        "schema": PROMPT_CONTEXT_RECEIPT_SCHEMA_V1,
        "host_state": "prompt-context-prepared",
        "prompt_context_sha256": context_sha256,
        "prompt_context_bytes": len(encoded),
        "capabilities": tuple(expected_capabilities),
    }:
        raise PreparedQueryDeliveryError("receipt verification changed the prepared context")
    try:
        final_journal_revision, final_journal_record_digest = receipt_authority._consume(
            action_id=action.action_id,
            action_content_digest=action.content_digest,
            receipt_event_content_digest=receipt_event.content_digest,
            issuing_record_digest=decision.journal_record_digest,
            expires_at=action.expires_at,
        )
    except CtxEngineError:
        raise PreparedQueryDeliveryError(
            "prepared delivery lacks an authoritative revision-three receipt"
        ) from None
    delivery_digest = _delivery_digest(
        decision=decision,
        execution_intent=execution_intent,
        action_id=action.action_id,
        action_content_digest=action.content_digest,
        action_precondition_revision=action.precondition_revision,
        receipt_event_content_digest=receipt_event.content_digest,
        final_journal_revision=final_journal_revision,
        final_journal_record_digest=final_journal_record_digest,
        expires_at=action.expires_at,
        context_sha256=context_sha256,
        context_bytes=len(encoded),
        capabilities=metadata,
    )
    return PreparedQueryDelivery(
        decision=decision,
        execution_intent=execution_intent,
        action_id=action.action_id,
        action_content_digest=action.content_digest,
        action_precondition_revision=action.precondition_revision,
        receipt_event_content_digest=receipt_event.content_digest,
        final_journal_revision=final_journal_revision,
        final_journal_record_digest=final_journal_record_digest,
        expires_at=action.expires_at,
        context=context,
        context_sha256=context_sha256,
        context_bytes=len(encoded),
        capabilities=metadata,
        delivery_digest=delivery_digest,
        _pid=os.getpid(),
        _seal=_issue_seal(delivery_digest),
    )


def accept_prepared_query_delivery(
    value: object,
    *,
    host: QueryHostDescriptor,
) -> PreparedQueryDelivery:
    """Validate and defensively copy a process-local prepared delivery."""

    if type(host) is not QueryHostDescriptor or host.execution_intent not in _INTENTS:
        raise PreparedQueryDeliveryError("prepared delivery requires an explicit expected host")
    if type(value) is not PreparedQueryDelivery or not _seal_matches(
        value._seal, value.delivery_digest
    ):
        raise PreparedQueryDeliveryError("prepared delivery did not come from the closed factory")
    if value.execution_intent != host.execution_intent:
        raise PreparedQueryDeliveryError("prepared delivery execution intent does not match")
    try:
        accepted_decision = accept_query_decision(value.decision, host=host)
    except QueryDecisionValidationError:
        raise PreparedQueryDeliveryError("prepared delivery host does not match") from None
    if type(accepted_decision) is not CommittedQueryDecision:
        raise PreparedQueryDeliveryError("prepared delivery lost its committed decision")
    if value._pid != os.getpid():
        raise PreparedQueryDeliveryError("prepared delivery belongs to another process")
    copied_digest = _delivery_digest(
        decision=accepted_decision,
        execution_intent=value.execution_intent,
        action_id=value.action_id,
        action_content_digest=value.action_content_digest,
        action_precondition_revision=value.action_precondition_revision,
        receipt_event_content_digest=value.receipt_event_content_digest,
        final_journal_revision=value.final_journal_revision,
        final_journal_record_digest=value.final_journal_record_digest,
        expires_at=value.expires_at,
        context_sha256=value.context_sha256,
        context_bytes=value.context_bytes,
        capabilities=value.capabilities,
    )
    if copied_digest != value.delivery_digest:
        raise PreparedQueryDeliveryError("prepared delivery was substituted")
    if not _consume_seal(value._seal, value.delivery_digest):
        raise PreparedQueryDeliveryError("prepared delivery was already accepted")
    return PreparedQueryDelivery(
        decision=accepted_decision,
        execution_intent=value.execution_intent,
        action_id=value.action_id,
        action_content_digest=value.action_content_digest,
        action_precondition_revision=value.action_precondition_revision,
        receipt_event_content_digest=value.receipt_event_content_digest,
        final_journal_revision=value.final_journal_revision,
        final_journal_record_digest=value.final_journal_record_digest,
        expires_at=value.expires_at,
        context=value.context,
        context_sha256=value.context_sha256,
        context_bytes=value.context_bytes,
        capabilities=value.capabilities,
        delivery_digest=value.delivery_digest,
        _pid=os.getpid(),
        _seal=_issue_seal(value.delivery_digest, accept_once=False),
    )


__all__ = [
    "PreparedDeliveryCapability",
    "PreparedQueryDelivery",
    "PreparedQueryDeliveryError",
    "accept_prepared_query_delivery",
    "render_prepared_prompt_context",
]
