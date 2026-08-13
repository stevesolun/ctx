"""Host-neutral, authority-free receipts for one closed query decision.

The production factory journals one engine decision before returning.  Its
result contains only bounded capability selections and provenance digests: no
task prose, source code, repository path, catalog authority, planner, material,
installer, or live engine object crosses this boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Final, TypeAlias
from weakref import WeakKeyDictionary

from ctx.engine.benefit import ABSTENTION_CODES
from ctx.engine.capability_schema import MAX_HOST_CONTEXT_CHARS, MAX_SELECTED_CAPABILITIES
from ctx.engine.planner import CapabilitySelection
from ctx.engine.protocol import Transition
from ctx.engine.state import CommittedPlanV3
from ctx.runtime.production_catalog import (
    RELEASE_QUERY_CATALOG_MODE,
    RELEASE_QUERY_CATALOG_ROOT_SHA256,
    RELEASE_QUERY_CATALOG_SEQUENCE,
)


_SAFE_CODE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_CATALOG_MODE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
_HEADER: Final = "CTX recommendation bundle (committed, advisory only):"
_FOOTER: Final = (
    "Use only capabilities relevant to the current task. "
    "Do not install, load, or activate anything without user approval."
)
_HOST_CONTEXT_IDS: Final = frozenset({"ctx-run", "codex", "claude-code"})
_EXECUTION_INTENTS: Final = frozenset({"recommend", "activate", "experiment"})


class _ReceiptSeal:
    """Opaque process-local identity for one exact receipt digest."""

    __slots__ = ("__weakref__",)


_RECEIPT_SEALS: WeakKeyDictionary[_ReceiptSeal, str] = WeakKeyDictionary()
_RECEIPT_SEALS_LOCK = Lock()


def _issue_receipt_seal(receipt_digest: str) -> _ReceiptSeal:
    seal = _ReceiptSeal()
    with _RECEIPT_SEALS_LOCK:
        _RECEIPT_SEALS[seal] = receipt_digest
    return seal


def _receipt_seal_matches(value: object, receipt_digest: str) -> bool:
    if type(value) is not _ReceiptSeal or type(receipt_digest) is not str:
        return False
    with _RECEIPT_SEALS_LOCK:
        return _RECEIPT_SEALS.get(value) == receipt_digest


class QueryDecisionValidationError(ValueError):
    """A closed query receipt failed its exact structural contract."""


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _required_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise QueryDecisionValidationError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _known_host_descriptor_digest(host_context_id: str, execution_intent: str) -> str:
    if execution_intent == "recommend":
        return _digest({"host": host_context_id, "level": "query-only", "schema": "ctx-host-v1"})
    return _digest(
        {
            "execution_intent": execution_intent,
            "host": host_context_id,
            "level": f"prompt-context-{execution_intent}",
            "schema": "ctx-host-prompt-context-v1",
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryHostDescriptor:
    """One exact supported host identity for the query-only engine seam."""

    host_context_id: str
    host_descriptor_digest: str
    execution_intent: str = "recommend"

    def __post_init__(self) -> None:
        if type(self.host_context_id) is not str or self.host_context_id not in _HOST_CONTEXT_IDS:
            raise QueryDecisionValidationError("query host is not supported")
        if (
            type(self.execution_intent) is not str
            or self.execution_intent not in _EXECUTION_INTENTS
        ):
            raise QueryDecisionValidationError("query host execution intent is unsupported")
        if type(
            self.host_descriptor_digest
        ) is not str or self.host_descriptor_digest != _known_host_descriptor_digest(
            self.host_context_id,
            self.execution_intent,
        ):
            raise QueryDecisionValidationError("query host descriptor digest is invalid")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("QueryHostDescriptor is sealed")

    @classmethod
    def ctx_run(cls, execution_intent: str = "recommend") -> QueryHostDescriptor:
        return cls._for("ctx-run", execution_intent)

    @classmethod
    def codex(cls, execution_intent: str = "recommend") -> QueryHostDescriptor:
        return cls._for("codex", execution_intent)

    @classmethod
    def claude_code(cls, execution_intent: str = "recommend") -> QueryHostDescriptor:
        return cls._for("claude-code", execution_intent)

    @classmethod
    def _for(cls, host_context_id: str, execution_intent: str = "recommend") -> QueryHostDescriptor:
        return cls(
            host_context_id=host_context_id,
            host_descriptor_digest=_known_host_descriptor_digest(
                host_context_id,
                execution_intent,
            ),
            execution_intent=execution_intent,
        )

    @property
    def engine_host_level(self) -> str:
        if self.execution_intent == "recommend":
            return "query-only"
        return f"prompt-context-{self.execution_intent}"


def _accepted_host(value: object) -> QueryHostDescriptor:
    if type(value) is not QueryHostDescriptor:
        raise QueryDecisionValidationError("host must be an exact QueryHostDescriptor")
    try:
        return QueryHostDescriptor(
            host_context_id=value.host_context_id,
            host_descriptor_digest=value.host_descriptor_digest,
            execution_intent=value.execution_intent,
        )
    except Exception:
        raise QueryDecisionValidationError("host descriptor is invalid") from None


def _selection_mapping(value: CapabilitySelection) -> dict[str, object]:
    return value.to_mapping(schema_version=2)


def _presentation_digest(
    *,
    capabilities: tuple[CapabilitySelection, ...],
    plan_digest: str,
    catalog_snapshot_digest: str,
    release_root_digest: str,
    release_sequence: int,
    catalog_mode: str,
    abstention_code: str | None,
) -> str:
    return _digest(
        {
            "schema": "ctx.query-presentation-receipt-v1",
            "status": "presented" if capabilities else "abstained",
            "capabilities": [_selection_mapping(item) for item in capabilities],
            "plan_digest": plan_digest,
            "catalog_snapshot_digest": catalog_snapshot_digest,
            "release_root_digest": release_root_digest,
            "release_sequence": release_sequence,
            "catalog_mode": catalog_mode,
            "abstention_code": abstention_code,
        }
    )


def _receipt_digest(
    *,
    host_context_id: str,
    host_descriptor_digest: str,
    journal_revision: int,
    journal_record_digest: str,
    presentation_digest: str,
    presentation_action_id: str | None,
    presentation_action_content_digest: str | None,
    work_signature_digest: str,
    host_invocation_digest: str,
) -> str:
    return _digest(
        {
            "schema": "ctx.query-decision-receipt-v2",
            "host_context_id": host_context_id,
            "host_descriptor_digest": host_descriptor_digest,
            "journal_revision": journal_revision,
            "journal_record_digest": journal_record_digest,
            "presentation_digest": presentation_digest,
            "presentation_action_id": presentation_action_id,
            "presentation_action_content_digest": presentation_action_content_digest,
            "work_signature_digest": work_signature_digest,
            "host_invocation_digest": host_invocation_digest,
        }
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CommittedQueryDecision:
    """Closed, journaled, authority-free recommendation or abstention receipt."""

    host_context_id: str
    host_descriptor_digest: str
    capabilities: tuple[CapabilitySelection, ...]
    plan_digest: str
    catalog_snapshot_digest: str
    journal_revision: int
    journal_record_digest: str
    release_root_digest: str
    release_sequence: int
    catalog_mode: str
    abstention_code: str | None
    presentation_action_id: str | None
    presentation_action_content_digest: str | None
    work_signature_digest: str
    host_invocation_digest: str
    presentation_digest: str
    receipt_digest: str
    _receipt_seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.host_context_id) is not str or self.host_context_id not in _HOST_CONTEXT_IDS:
            raise QueryDecisionValidationError("query decision host is unsupported")
        if type(self._receipt_seal) is not _ReceiptSeal:
            raise QueryDecisionValidationError(
                "committed query decisions must come from the closed factory"
            )
        known_host_digests = {
            _known_host_descriptor_digest(self.host_context_id, intent)
            for intent in _EXECUTION_INTENTS
        }
        if (
            type(self.host_descriptor_digest) is not str
            or self.host_descriptor_digest not in known_host_digests
        ):
            raise QueryDecisionValidationError("query decision host provenance is invalid")
        if not isinstance(self.capabilities, tuple) or not all(
            type(item) is CapabilitySelection for item in self.capabilities
        ):
            raise QueryDecisionValidationError(
                "capabilities must be an exact tuple of CapabilitySelection values"
            )
        if len(self.capabilities) > MAX_SELECTED_CAPABILITIES:
            raise QueryDecisionValidationError("a query decision cannot exceed five selections")
        identities = tuple(item.capability_id for item in self.capabilities)
        if len(identities) != len(set(identities)):
            raise QueryDecisionValidationError("a query decision cannot contain duplicates")
        for field_name in (
            "plan_digest",
            "catalog_snapshot_digest",
            "journal_record_digest",
            "release_root_digest",
            "work_signature_digest",
            "host_invocation_digest",
            "presentation_digest",
            "receipt_digest",
        ):
            _required_digest(getattr(self, field_name), field_name)
        if self.journal_revision != 2:
            raise QueryDecisionValidationError(
                "query decisions must commit exactly two journal revisions"
            )
        if type(self.release_sequence) is not int or self.release_sequence < 1:
            raise QueryDecisionValidationError("release_sequence must be an integer >= 1")
        if (
            type(self.catalog_mode) is not str
            or _CATALOG_MODE_RE.fullmatch(self.catalog_mode) is None
        ):
            raise QueryDecisionValidationError("catalog_mode must be a canonical safe token")

        if self.capabilities:
            if self.abstention_code is not None:
                raise QueryDecisionValidationError(
                    "presented query decisions cannot contain an abstention code"
                )
            if (
                type(self.presentation_action_id) is not str
                or not self.presentation_action_id
                or len(self.presentation_action_id) > 256
                or self.presentation_action_content_digest is None
            ):
                raise QueryDecisionValidationError(
                    "presented query decisions require the exact presentation action"
                )
            _required_digest(
                self.presentation_action_content_digest,
                "presentation_action_content_digest",
            )
        elif self.abstention_code not in ABSTENTION_CODES:
            raise QueryDecisionValidationError(
                "abstained query decisions require one declared abstention code"
            )
        elif (
            self.presentation_action_id is not None
            or self.presentation_action_content_digest is not None
        ):
            raise QueryDecisionValidationError(
                "abstained query decisions cannot bind a presentation action"
            )

        if self.release_root_digest == RELEASE_QUERY_CATALOG_ROOT_SHA256:
            if (
                self.release_sequence != RELEASE_QUERY_CATALOG_SEQUENCE
                or self.catalog_mode != RELEASE_QUERY_CATALOG_MODE
            ):
                raise QueryDecisionValidationError(
                    "current production release identity is internally inconsistent"
                )
        if self.presentation_digest != _presentation_digest(
            capabilities=self.capabilities,
            plan_digest=self.plan_digest,
            catalog_snapshot_digest=self.catalog_snapshot_digest,
            release_root_digest=self.release_root_digest,
            release_sequence=self.release_sequence,
            catalog_mode=self.catalog_mode,
            abstention_code=self.abstention_code,
        ):
            raise QueryDecisionValidationError("presentation digest does not bind the receipt")
        if self.receipt_digest != _receipt_digest(
            host_context_id=self.host_context_id,
            host_descriptor_digest=self.host_descriptor_digest,
            journal_revision=self.journal_revision,
            journal_record_digest=self.journal_record_digest,
            presentation_digest=self.presentation_digest,
            presentation_action_id=self.presentation_action_id,
            presentation_action_content_digest=self.presentation_action_content_digest,
            work_signature_digest=self.work_signature_digest,
            host_invocation_digest=self.host_invocation_digest,
        ):
            raise QueryDecisionValidationError("receipt digest does not bind host provenance")
        if not _receipt_seal_matches(self._receipt_seal, self.receipt_digest):
            raise QueryDecisionValidationError(
                "receipt seal does not bind the original host provenance"
            )

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("CommittedQueryDecision is sealed")

    @property
    def status(self) -> str:
        return "presented" if self.capabilities else "abstained"

    @property
    def recommendation_count(self) -> int:
        return len(self.capabilities)

    @property
    def failure_code(self) -> None:
        return None

    @property
    def recommendation_context(self) -> str | None:
        host = next(
            QueryHostDescriptor._for(self.host_context_id, intent)
            for intent in sorted(_EXECUTION_INTENTS)
            if _known_host_descriptor_digest(self.host_context_id, intent)
            == self.host_descriptor_digest
        )
        return render_query_decision_context(self, host=host)


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryDecisionFailure:
    """Bounded fail-soft outcome carrying no partial decision receipt."""

    failure_code: str

    def __post_init__(self) -> None:
        if type(self.failure_code) is not str or _SAFE_CODE_RE.fullmatch(self.failure_code) is None:
            raise QueryDecisionValidationError("failure_code must be a canonical safe token")

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("QueryDecisionFailure is sealed")

    @property
    def status(self) -> str:
        return "failed"

    @property
    def recommendation_context(self) -> None:
        return None

    @property
    def recommendation_count(self) -> int:
        return 0


QueryDecisionResult: TypeAlias = CommittedQueryDecision | QueryDecisionFailure


def _copy_selection(value: CapabilitySelection) -> CapabilitySelection:
    return CapabilitySelection(
        capability_id=value.capability_id,
        kind=value.kind,
        name=value.name,
        source_digest=value.source_digest,
        normalized_score_ppm=value.normalized_score_ppm,
        matching_signals=value.matching_signals,
        reason_codes=value.reason_codes,
        actionability=value.actionability,
        install_descriptor_digest=value.install_descriptor_digest,
        install_plan_digest=value.install_plan_digest,
    )


def _create_committed_query_decision(
    *,
    host: QueryHostDescriptor,
    capabilities: tuple[CapabilitySelection, ...],
    plan_digest: str,
    catalog_snapshot_digest: str,
    journal_revision: int,
    journal_record_digest: str,
    release_root_digest: str,
    release_sequence: int,
    catalog_mode: str,
    abstention_code: str | None,
    presentation_action_id: str | None,
    presentation_action_content_digest: str | None,
    work_signature_digest: str,
    host_invocation_digest: str,
) -> CommittedQueryDecision:
    copied_capabilities = tuple(_copy_selection(item) for item in capabilities)
    presentation_digest = _presentation_digest(
        capabilities=copied_capabilities,
        plan_digest=plan_digest,
        catalog_snapshot_digest=catalog_snapshot_digest,
        release_root_digest=release_root_digest,
        release_sequence=release_sequence,
        catalog_mode=catalog_mode,
        abstention_code=abstention_code,
    )
    receipt_digest = _receipt_digest(
        host_context_id=host.host_context_id,
        host_descriptor_digest=host.host_descriptor_digest,
        journal_revision=journal_revision,
        journal_record_digest=journal_record_digest,
        presentation_digest=presentation_digest,
        presentation_action_id=presentation_action_id,
        presentation_action_content_digest=presentation_action_content_digest,
        work_signature_digest=work_signature_digest,
        host_invocation_digest=host_invocation_digest,
    )
    return CommittedQueryDecision(
        host_context_id=host.host_context_id,
        host_descriptor_digest=host.host_descriptor_digest,
        capabilities=copied_capabilities,
        plan_digest=plan_digest,
        catalog_snapshot_digest=catalog_snapshot_digest,
        journal_revision=journal_revision,
        journal_record_digest=journal_record_digest,
        release_root_digest=release_root_digest,
        release_sequence=release_sequence,
        catalog_mode=catalog_mode,
        abstention_code=abstention_code,
        presentation_action_id=presentation_action_id,
        presentation_action_content_digest=presentation_action_content_digest,
        work_signature_digest=work_signature_digest,
        host_invocation_digest=host_invocation_digest,
        presentation_digest=presentation_digest,
        receipt_digest=receipt_digest,
        _receipt_seal=_issue_receipt_seal(receipt_digest),
    )


def accept_query_decision(
    value: object,
    *,
    host: QueryHostDescriptor,
) -> QueryDecisionResult:
    """Validate and defensively copy one exact result for one exact host."""

    accepted_host = _accepted_host(host)
    if type(value) is QueryDecisionFailure:
        try:
            return QueryDecisionFailure(failure_code=value.failure_code)
        except Exception:
            raise QueryDecisionValidationError("query decision failure is invalid") from None
    if type(value) is not CommittedQueryDecision:
        raise QueryDecisionValidationError("query decision must use an exact result type")
    try:
        if not _receipt_seal_matches(value._receipt_seal, value.receipt_digest):
            raise QueryDecisionValidationError(
                "query decision did not come from the closed factory"
            )
        copied = _create_committed_query_decision(
            host=QueryHostDescriptor(
                host_context_id=value.host_context_id,
                host_descriptor_digest=value.host_descriptor_digest,
                execution_intent=next(
                    intent
                    for intent in sorted(_EXECUTION_INTENTS)
                    if _known_host_descriptor_digest(value.host_context_id, intent)
                    == value.host_descriptor_digest
                ),
            ),
            capabilities=value.capabilities,
            plan_digest=value.plan_digest,
            catalog_snapshot_digest=value.catalog_snapshot_digest,
            journal_revision=value.journal_revision,
            journal_record_digest=value.journal_record_digest,
            release_root_digest=value.release_root_digest,
            release_sequence=value.release_sequence,
            catalog_mode=value.catalog_mode,
            abstention_code=value.abstention_code,
            presentation_action_id=value.presentation_action_id,
            presentation_action_content_digest=value.presentation_action_content_digest,
            work_signature_digest=value.work_signature_digest,
            host_invocation_digest=value.host_invocation_digest,
        )
        if copied.presentation_digest != value.presentation_digest:
            raise QueryDecisionValidationError("presentation digest was substituted")
        if copied.receipt_digest != value.receipt_digest:
            raise QueryDecisionValidationError("receipt digest was substituted")
    except Exception:
        raise QueryDecisionValidationError("query decision receipt is invalid") from None
    if (
        copied.host_context_id,
        copied.host_descriptor_digest,
    ) != (
        accepted_host.host_context_id,
        accepted_host.host_descriptor_digest,
    ):
        raise QueryDecisionValidationError("query decision host provenance does not match")
    return copied


def _render_committed_context(
    decision: CommittedQueryDecision,
    *,
    host: QueryHostDescriptor,
) -> str | None:
    if (
        decision.host_context_id,
        decision.host_descriptor_digest,
    ) != (host.host_context_id, host.host_descriptor_digest):
        raise QueryDecisionValidationError("query decision host provenance does not match")
    if not decision.capabilities:
        return None
    context = "\n".join(
        (
            _HEADER,
            *(
                f"{index}. kind={selection.kind} | name={selection.name} | "
                f"id={selection.capability_id} | "
                f"actionability={selection.actionability} | "
                f"score_ppm={selection.normalized_score_ppm}"
                for index, selection in enumerate(decision.capabilities, 1)
            ),
            _FOOTER,
        )
    )
    if len(context) > MAX_HOST_CONTEXT_CHARS:
        raise QueryDecisionValidationError("query decision context exceeds the host bound")
    return context


def render_query_decision_context(
    value: object,
    *,
    host: QueryHostDescriptor,
) -> str | None:
    """Render an accepted receipt without ranking, planning, or live authority."""

    accepted = accept_query_decision(value, host=host)
    if isinstance(accepted, QueryDecisionFailure):
        return None
    return _render_committed_context(accepted, host=_accepted_host(host))


def _thaw_json(value: object) -> object:
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _capability_selections_from_committed_transition(
    transition: Transition,
    plan: CommittedPlanV3,
    *,
    host: QueryHostDescriptor | None = None,
) -> tuple[CapabilitySelection, ...]:
    """Prove the emitted full-v3 bundle is exactly the committed plan, then strip it."""

    if type(transition) is not Transition or type(plan) is not CommittedPlanV3:
        raise QueryDecisionValidationError("transition and committed plan must be exact values")
    accepted_host = QueryHostDescriptor.ctx_run() if host is None else _accepted_host(host)
    bundles = tuple(action for action in transition.actions if action.kind == "PresentBundle")
    if plan.status == "abstained":
        if transition.actions or plan.capabilities:
            raise QueryDecisionValidationError(
                "abstained transition does not match its committed plan"
            )
        return ()
    expected_action_count = 1 if accepted_host.execution_intent == "recommend" else 2
    if (
        plan.status != "ready"
        or len(transition.actions) != expected_action_count
        or len(bundles) != 1
    ):
        raise QueryDecisionValidationError("transition does not match its committed plan")
    action = bundles[0]
    if transition.actions[0] != action:
        raise QueryDecisionValidationError("presentation action order is invalid")
    if set(action.payload) != {"plan_digest", "capabilities"}:
        raise QueryDecisionValidationError("transition does not match its committed plan")
    raw_rows = action.payload["capabilities"]
    if not isinstance(raw_rows, tuple):
        raise QueryDecisionValidationError("transition does not match its committed plan")
    committed_rows = tuple(capability.to_dict() for capability in plan.capabilities)
    transition_rows = tuple(_thaw_json(row) for row in raw_rows)
    if action.payload["plan_digest"] != plan.decision_digest or transition_rows != committed_rows:
        raise QueryDecisionValidationError("transition does not match its committed plan")
    if accepted_host.execution_intent != "recommend":
        prompt_action = transition.actions[1]
        prompt_rows = prompt_action.payload.get("capabilities")
        if (
            prompt_action.kind != "PreparePromptContext"
            or prompt_action.entity_id is not None
            or prompt_action.plan_id != plan.plan_id
            or prompt_action.catalog_snapshot_id != plan.catalog_snapshot_id
            or prompt_action.required_host_feature != "prompt-context"
            or prompt_action.payload.get("execution_intent") != accepted_host.execution_intent
            or prompt_action.payload.get("plan_digest") != plan.decision_digest
            or prompt_action.payload.get("presentation_action_id") != action.action_id
            or prompt_action.payload.get("presentation_action_content_digest")
            != action.content_digest
            or not isinstance(prompt_rows, tuple)
        ):
            raise QueryDecisionValidationError("prompt context action is not exactly committed")
        load_ids = tuple(
            row.capability_id for row in plan.capabilities if row.actionability == "load"
        )
        if tuple(row.get("capability_id") for row in prompt_rows) != load_ids:
            raise QueryDecisionValidationError("prompt context action changed the load bundle")
    return tuple(
        CapabilitySelection.from_candidate(capability.selection.presentation)
        for capability in plan.capabilities
    )


def _commit_query_decision(
    *,
    host: QueryHostDescriptor,
    transition: Transition,
    plan: CommittedPlanV3,
    journal_revision: int,
    journal_record_digest: str,
    release_root_digest: str,
    release_sequence: int,
    catalog_mode: str,
    work_signature_digest: str,
    host_invocation_digest: str,
) -> CommittedQueryDecision:
    """Seal a receipt only after exact journal, plan, transition, and host binding."""

    accepted_host = _accepted_host(host)
    if (
        type(transition) is not Transition
        or transition.scope.host_context_id != accepted_host.host_context_id
        or transition.to_revision != journal_revision
        or journal_revision != 2
    ):
        raise QueryDecisionValidationError(
            "transition does not match the committed host journal revision"
        )
    capabilities = _capability_selections_from_committed_transition(
        transition,
        plan,
        host=accepted_host,
    )
    presentation_action = transition.actions[0] if capabilities else None
    return _create_committed_query_decision(
        host=accepted_host,
        capabilities=capabilities,
        plan_digest=plan.decision_digest,
        catalog_snapshot_digest=plan.catalog_snapshot_id,
        journal_revision=journal_revision,
        journal_record_digest=journal_record_digest,
        release_root_digest=release_root_digest,
        release_sequence=release_sequence,
        catalog_mode=catalog_mode,
        abstention_code=plan.abstention_code,
        presentation_action_id=(
            None if presentation_action is None else presentation_action.action_id
        ),
        presentation_action_content_digest=(
            None if presentation_action is None else presentation_action.content_digest
        ),
        work_signature_digest=work_signature_digest,
        host_invocation_digest=host_invocation_digest,
    )


def prepare_query_decision(
    *,
    host: QueryHostDescriptor,
    task: str,
    language: str,
    session_id: str,
    workspace: Path,
    journal_path: Path,
    benefit_audit_path: Path,
    host_invocation_digest: str,
) -> QueryDecisionResult:
    """Commit one production query decision for any supported host."""

    accepted_host = _accepted_host(host)
    # Imported lazily so the compatibility module can re-export this boundary
    # without creating a module-initialization cycle.
    from ctx.runtime.query_session import _prepare_query_decision

    return _prepare_query_decision(
        host=accepted_host,
        task=task,
        language=language,
        session_id=session_id,
        workspace=workspace,
        journal_path=journal_path,
        benefit_audit_path=benefit_audit_path,
        host_invocation_digest=host_invocation_digest,
    )


__all__ = [
    "CapabilitySelection",
    "CommittedQueryDecision",
    "QueryDecisionFailure",
    "QueryDecisionResult",
    "QueryDecisionValidationError",
    "QueryHostDescriptor",
    "accept_query_decision",
    "prepare_query_decision",
    "render_query_decision_context",
]
