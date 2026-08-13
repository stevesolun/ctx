"""Authenticated input manifests for deterministic net-benefit planning.

Catalog prose is deliberately absent from these contracts.  A caller must pin
one canonical, read-only manifest by its exact SHA-256 digest; the loader then
freezes only explicit bounded facts.  Runtime evidence starts empty and is
bound to the exact manifest, retrieval presentation, and current-work
observation.

This loader does not establish catalog completeness.  It is not production-ready
until a trusted frozen-catalog generator or digest pin proves which exact
retrieval presentations the manifest covers or deliberately filters.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

from ctx.engine.benefit import (
    MAX_PPM,
    BenefitCandidate,
    BenefitValidationError,
    EvidenceSummary,
    NetBenefitPolicy,
    ResourceCosts,
)
from ctx.engine.planner import CapabilityCandidate, PlannerValidationError, WorkObservation


_FACTS_SCHEMA: Final = "ctx.authenticated-benefit-facts-v1"
_POLICY_MANIFEST_SCHEMA: Final = "ctx.reviewed-net-benefit-policy-v1"
_POLICY_SCHEMA: Final = "ctx.net-benefit-policy-v3"
_SELECTION_ALGORITHM: Final = "ctx.greedy-bounded-subset-exchange-v1"
_EVIDENCE_WINDOW_SCHEMA: Final = "ctx.empty-evidence-window-v1"
MAX_AUTHENTICATED_BENEFIT_FACT_RECORDS: Final = 4_096
MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES: Final = 16 * 1024 * 1024
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")

_ROOT_FIELDS = frozenset({"schema", "records"})
_RECORD_FIELDS = frozenset(
    {
        "availability",
        "complements",
        "conflicts",
        "costs",
        "coverage_keys",
        "credentials_available",
        "expected_task_benefit_ppm",
        "maximum_relevance_ppm",
        "permissions_allowed",
        "presentation",
        "presentation_digest",
        "resource_profile_digest",
        "security_approved",
        "source_trusted",
        "trust_ppm",
    }
)
_PRESENTATION_FIELDS = frozenset(
    {
        "actionability",
        "capability_id",
        "equivalence_key",
        "install_descriptor_digest",
        "install_plan_digest",
        "kind",
        "matching_signals",
        "name",
        "normalized_score_ppm",
        "reason_codes",
        "source_digest",
    }
)
_COST_FIELDS = frozenset(
    {
        "approval_prompts",
        "child_agent_units",
        "context_tokens",
        "credential_burden_units",
        "permission_burden_units",
        "process_units",
        "runtime_millis",
        "tool_schema_tokens",
    }
)
_POLICY_ROOT_FIELDS = frozenset({"schema", "policy"})
_POLICY_FIELDS = frozenset(
    {
        "approval_prompt_cost_u",
        "calibration_digest",
        "child_agent_unit_cost_u",
        "complementarity_bonus_u",
        "context_token_cost_u",
        "credential_burden_cost_u",
        "effective_outcome_evidence_ppm",
        "evidence_prior_observations",
        "failed_invocation_evidence_ppm",
        "harmful_outcome_evidence_ppm",
        "idle_opportunity_evidence_ppm",
        "minimum_marginal_net_benefit_u",
        "minimum_relevance_ppm",
        "minimum_trust_ppm",
        "new_coverage_bonus_u_per_key",
        "overlap_penalty_u_per_key",
        "permission_burden_cost_u",
        "policy_schema_id",
        "process_unit_cost_u",
        "runtime_millisecond_cost_u",
        "selection_algorithm_id",
        "successful_invocation_evidence_ppm",
        "tool_schema_token_cost_u",
        "validated_outcome_evidence_ppm",
    }
)


class AuthenticatedBenefitManifestError(ValueError):
    """A pinned benefit or policy manifest failed its closed trust contract."""


def _fail(message: str) -> AuthenticatedBenefitManifestError:
    return AuthenticatedBenefitManifestError(message)


def _digest(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise _fail(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _signature(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("manifest JSON contains a duplicate object key")
        result[key] = value
    return result


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _read_authenticated_manifest(path: Path, expected_sha256: str) -> Mapping[str, object]:
    if not isinstance(path, Path):
        raise TypeError("manifest path must be a Path")
    expected_sha256 = _digest(expected_sha256, "expected manifest SHA-256")
    fd = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise _fail("manifest must be a read-only regular file")
        before_signature = _signature(before)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or _signature(opened) != before_signature
        ):
            raise _fail("manifest changed before its authenticated read")
        body = bytearray()
        while len(body) <= MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES:
            chunk = os.read(
                fd,
                min(
                    65_536,
                    MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES + 1 - len(body),
                ),
            )
            if not chunk:
                break
            body.extend(chunk)
        after_opened = os.fstat(fd)
        after_path = path.lstat()
        if (
            len(body) > MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES
            or _signature(after_opened) != _signature(opened)
            or _signature(after_path) != before_signature
        ):
            raise _fail("manifest changed during its authenticated read")
        frozen_body = bytes(body)
        if hashlib.sha256(frozen_body).hexdigest() != expected_sha256:
            raise _fail("manifest does not match the caller-supplied SHA-256")
        try:
            decoded = json.loads(
                frozen_body.decode("utf-8"),
                object_pairs_hook=_duplicate_rejecting_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    _fail("manifest JSON contains a non-finite number")
                ),
            )
        except AuthenticatedBenefitManifestError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise _fail("manifest must be valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise _fail("manifest root must be an object")
        if _canonical_bytes(decoded) != frozen_body:
            raise _fail("manifest must use canonical JSON encoding")
        return decoded
    except AuthenticatedBenefitManifestError:
        raise
    except OSError:
        raise _fail("manifest is unavailable for authenticated reading") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _decode_authenticated_manifest_bytes(
    value: bytes,
    expected_sha256: str,
) -> Mapping[str, object]:
    """Authenticate one already-frozen canonical manifest body.

    The filesystem loader above owns path and descriptor authentication.  This
    companion is for bytes already owned by a trusted package resource or a
    query-scoped generator; it applies the same size, hash, duplicate-key, JSON,
    and canonical-encoding contract without reopening a pathname.
    """

    if not isinstance(value, bytes):
        raise TypeError("manifest body must be immutable bytes")
    expected_sha256 = _digest(expected_sha256, "expected manifest SHA-256")
    if len(value) > MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES:
        raise _fail("manifest exceeds the authenticated byte bound")
    if hashlib.sha256(value).hexdigest() != expected_sha256:
        raise _fail("manifest does not match the caller-supplied SHA-256")
    try:
        decoded = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                _fail("manifest JSON contains a non-finite number")
            ),
        )
    except AuthenticatedBenefitManifestError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise _fail("manifest must be valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise _fail("manifest root must be an object")
    canonical = _canonical_bytes(decoded)
    if value not in (canonical, canonical + b"\n"):
        raise _fail("manifest must use canonical JSON encoding")
    return decoded


def _closed_mapping(
    value: object,
    *,
    fields: frozenset[str],
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise _fail(f"{field_name} must contain exactly its declared fields")
    return value


def _canonical_tokens(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(f"{field_name} must be an array of strings")
    result = tuple(value)
    if len(result) > 64 or len(set(result)) != len(result) or result != tuple(sorted(result)):
        raise _fail(f"{field_name} must be bounded, unique, and sorted")
    return result


def _integer(value: object, field_name: str, *, maximum: int = MAX_PPM) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise _fail(f"{field_name} must be an integer from 0 through {maximum}")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise _fail(f"{field_name} must be a boolean")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class _BenefitFactsRecord:
    presentation: CapabilityCandidate
    presentation_digest: str
    resource_profile_digest: str
    availability: str
    expected_task_benefit_ppm: int
    maximum_relevance_ppm: int
    trust_ppm: int
    costs: ResourceCosts
    source_trusted: bool
    security_approved: bool
    permissions_allowed: bool
    credentials_available: bool
    coverage_keys: tuple[str, ...]
    complements: tuple[str, ...]
    conflicts: tuple[str, ...]


def capability_presentation_mapping(presentation: CapabilityCandidate) -> dict[str, object]:
    """Return the complete standardized retrieval presentation mapping."""

    if not isinstance(presentation, CapabilityCandidate):
        raise TypeError("presentation must be a CapabilityCandidate")
    return {
        "actionability": presentation.actionability,
        "capability_id": presentation.capability_id,
        "equivalence_key": presentation.equivalence_key,
        "install_descriptor_digest": presentation.install_descriptor_digest,
        "install_plan_digest": presentation.install_plan_digest,
        "kind": presentation.kind,
        "matching_signals": list(presentation.matching_signals),
        "name": presentation.name,
        "normalized_score_ppm": presentation.normalized_score_ppm,
        "reason_codes": list(presentation.reason_codes),
        "source_digest": presentation.source_digest,
    }


def capability_presentation_digest(presentation: CapabilityCandidate) -> str:
    """Digest every field that can change candidate identity or actionability."""

    return hashlib.sha256(
        _canonical_bytes(capability_presentation_mapping(presentation))
    ).hexdigest()


def _presentation_from_mapping(value: object, index: int) -> CapabilityCandidate:
    presentation = _closed_mapping(
        value,
        fields=_PRESENTATION_FIELDS,
        field_name=f"records[{index}].presentation",
    )
    matching_signals = _canonical_tokens(
        presentation["matching_signals"],
        f"records[{index}].presentation.matching_signals",
    )
    reason_codes = _canonical_tokens(
        presentation["reason_codes"],
        f"records[{index}].presentation.reason_codes",
    )
    try:
        result = CapabilityCandidate(
            capability_id=presentation["capability_id"],  # type: ignore[arg-type]
            kind=presentation["kind"],  # type: ignore[arg-type]
            name=presentation["name"],  # type: ignore[arg-type]
            source_digest=presentation["source_digest"],  # type: ignore[arg-type]
            normalized_score_ppm=presentation["normalized_score_ppm"],  # type: ignore[arg-type]
            matching_signals=matching_signals,
            reason_codes=reason_codes,
            actionability=presentation["actionability"],  # type: ignore[arg-type]
            install_descriptor_digest=presentation["install_descriptor_digest"],  # type: ignore[arg-type]
            install_plan_digest=presentation["install_plan_digest"],  # type: ignore[arg-type]
            equivalence_key=presentation["equivalence_key"],  # type: ignore[arg-type]
        )
    except (PlannerValidationError, TypeError) as exc:
        raise _fail(f"records[{index}].presentation is invalid") from exc
    if capability_presentation_mapping(result) != dict(presentation):
        raise _fail(f"records[{index}].presentation is not standardized")
    return result


def _record_from_mapping(value: object, index: int) -> _BenefitFactsRecord:
    record = _closed_mapping(value, fields=_RECORD_FIELDS, field_name=f"records[{index}]")
    presentation = _presentation_from_mapping(record["presentation"], index)
    presentation_digest = _digest(
        record["presentation_digest"],
        f"records[{index}].presentation_digest",
    )
    if presentation_digest != capability_presentation_digest(presentation):
        raise _fail(f"records[{index}].presentation_digest does not match its full presentation")
    costs_value = _closed_mapping(
        record["costs"],
        fields=_COST_FIELDS,
        field_name=f"records[{index}].costs",
    )
    try:
        costs = ResourceCosts(
            context_tokens=costs_value["context_tokens"],  # type: ignore[arg-type]
            tool_schema_tokens=costs_value["tool_schema_tokens"],  # type: ignore[arg-type]
            runtime_millis=costs_value["runtime_millis"],  # type: ignore[arg-type]
            permission_burden_units=costs_value["permission_burden_units"],  # type: ignore[arg-type]
            credential_burden_units=costs_value["credential_burden_units"],  # type: ignore[arg-type]
            approval_prompts=costs_value["approval_prompts"],  # type: ignore[arg-type]
            process_units=costs_value["process_units"],  # type: ignore[arg-type]
            child_agent_units=costs_value["child_agent_units"],  # type: ignore[arg-type]
        )
        resource_profile_digest = record["resource_profile_digest"]
        availability = record["availability"]
        expected_task_benefit_ppm = record["expected_task_benefit_ppm"]
        maximum_relevance_ppm = _integer(
            record["maximum_relevance_ppm"],
            f"records[{index}].maximum_relevance_ppm",
        )
        trust_ppm = record["trust_ppm"]
        source_trusted = _boolean(record["source_trusted"], "source_trusted")
        security_approved = _boolean(record["security_approved"], "security_approved")
        permissions_allowed = _boolean(record["permissions_allowed"], "permissions_allowed")
        credentials_available = _boolean(
            record["credentials_available"],
            "credentials_available",
        )
        coverage_keys = _canonical_tokens(record["coverage_keys"], "coverage_keys")
        complements = _canonical_tokens(record["complements"], "complements")
        conflicts = _canonical_tokens(record["conflicts"], "conflicts")
        expected_availability = (
            "advisory" if presentation.actionability == "manual" else "executable"
        )
        if availability != expected_availability:
            raise _fail(f"records[{index}].availability does not match presentation actionability")
        if maximum_relevance_ppm > presentation.normalized_score_ppm:
            raise _fail(f"records[{index}].maximum_relevance_ppm exceeds presentation relevance")
        # Reuse the engine's closed domain validation with empty evidence.  The
        # per-observation evidence digest is replaced when a candidate is read.
        prototype = BenefitCandidate(
            capability_id=presentation.capability_id,
            source_digest=presentation.source_digest,
            resource_profile_digest=resource_profile_digest,  # type: ignore[arg-type]
            availability=availability,
            expected_task_benefit_ppm=expected_task_benefit_ppm,  # type: ignore[arg-type]
            relevance_ppm=maximum_relevance_ppm,
            trust_ppm=trust_ppm,  # type: ignore[arg-type]
            costs=costs,
            evidence=EvidenceSummary(
                capability_id=presentation.capability_id,
                kind=presentation.kind,
                source_digest=presentation.source_digest,
                evidence_window_digest="0" * 64,
                opportunity_observable=False,
            ),
            source_trusted=source_trusted,
            security_approved=security_approved,
            permissions_allowed=permissions_allowed,
            credentials_available=credentials_available,
            coverage_keys=coverage_keys,
            equivalence_key=presentation.equivalence_key,
            complements=complements,
            conflicts=conflicts,
        )
    except (BenefitValidationError, TypeError) as exc:
        raise _fail(f"records[{index}] contains invalid benefit facts") from exc
    return _BenefitFactsRecord(
        presentation=presentation,
        presentation_digest=presentation_digest,
        resource_profile_digest=prototype.resource_profile_digest,
        availability=prototype.availability,
        expected_task_benefit_ppm=prototype.expected_task_benefit_ppm,
        maximum_relevance_ppm=maximum_relevance_ppm,
        trust_ppm=prototype.trust_ppm,
        costs=costs,
        source_trusted=prototype.source_trusted,
        security_approved=prototype.security_approved,
        permissions_allowed=prototype.permissions_allowed,
        credentials_available=prototype.credentials_available,
        coverage_keys=prototype.coverage_keys,
        complements=prototype.complements,
        conflicts=prototype.conflicts,
    )


def _observation_mapping(observation: WorkObservation) -> dict[str, object]:
    return {
        "active_capability_ids": list(observation.active_capability_ids),
        "baseline_capability_ids": list(observation.baseline_capability_ids),
        "languages": list(observation.languages),
        "rejected_capability_ids": list(observation.rejected_capability_ids),
        "requested_limit": observation.requested_limit,
        "signals": list(observation.signals),
    }


class AuthenticatedBenefitFacts:
    """Frozen implementation of the runtime ``AuthenticatedBenefitFactsPort``.

    This authenticates declared facts, not catalog coverage.  Production use
    still requires a separately trusted frozen-catalog generator or filter that
    proves which exact retrieval presentations the pinned manifest covers.
    """

    __slots__ = ("_records", "benefit_facts_snapshot_digest")

    def __init__(
        self,
        *,
        records: Mapping[str, _BenefitFactsRecord],
        snapshot_digest: str,
    ) -> None:
        self._records = MappingProxyType(dict(records))
        self.benefit_facts_snapshot_digest = _digest(
            snapshot_digest,
            "benefit facts snapshot digest",
        )

    @property
    def presentation_digests(self) -> tuple[str, ...]:
        """Canonical exact presentations covered by this frozen facts source."""

        return tuple(sorted(self._records))

    def benefit_candidate(
        self,
        presentation: CapabilityCandidate,
        observation: WorkObservation,
    ) -> BenefitCandidate | None:
        if not isinstance(presentation, CapabilityCandidate):
            raise TypeError("presentation must be a CapabilityCandidate")
        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        presentation_digest = capability_presentation_digest(presentation)
        record = self._records.get(presentation_digest)
        if (
            record is None
            or record.presentation != presentation
            or presentation.capability_id in observation.rejected_capability_ids
            or not set(presentation.matching_signals).issubset(
                {*observation.signals, *observation.languages}
            )
        ):
            return None
        evidence_window_digest = hashlib.sha256(
            _canonical_bytes(
                {
                    "benefit_facts_snapshot_digest": self.benefit_facts_snapshot_digest,
                    "observation": _observation_mapping(observation),
                    "presentation": capability_presentation_mapping(presentation),
                    "presentation_digest": presentation_digest,
                    "schema": _EVIDENCE_WINDOW_SCHEMA,
                }
            )
        ).hexdigest()
        return BenefitCandidate(
            capability_id=presentation.capability_id,
            source_digest=presentation.source_digest,
            resource_profile_digest=record.resource_profile_digest,
            availability=record.availability,
            expected_task_benefit_ppm=record.expected_task_benefit_ppm,
            relevance_ppm=min(
                record.maximum_relevance_ppm,
                presentation.normalized_score_ppm,
            ),
            trust_ppm=record.trust_ppm,
            costs=record.costs,
            evidence=EvidenceSummary(
                capability_id=presentation.capability_id,
                kind=presentation.kind,
                source_digest=presentation.source_digest,
                evidence_window_digest=evidence_window_digest,
                opportunity_observable=False,
            ),
            source_trusted=record.source_trusted,
            security_approved=record.security_approved,
            permissions_allowed=record.permissions_allowed,
            credentials_available=record.credentials_available,
            coverage_keys=record.coverage_keys,
            equivalence_key=presentation.equivalence_key,
            complements=record.complements,
            conflicts=record.conflicts,
        )


def _authenticated_benefit_facts_from_manifest(
    value: Mapping[str, object],
    expected_sha256: str,
) -> AuthenticatedBenefitFacts:
    manifest = _closed_mapping(
        value,
        fields=_ROOT_FIELDS,
        field_name="benefit facts manifest",
    )
    if manifest["schema"] != _FACTS_SCHEMA:
        raise _fail("benefit facts manifest schema is unsupported")
    raw_records = manifest["records"]
    if (
        not isinstance(raw_records, list)
        or len(raw_records) > MAX_AUTHENTICATED_BENEFIT_FACT_RECORDS
    ):
        raise _fail("benefit facts records must be a bounded array")
    parsed = tuple(_record_from_mapping(value, index) for index, value in enumerate(raw_records))
    presentation_digests = tuple(record.presentation_digest for record in parsed)
    if presentation_digests != tuple(sorted(presentation_digests)) or len(
        set(presentation_digests)
    ) != len(presentation_digests):
        raise _fail("benefit facts records must use unique canonical presentation order")
    return AuthenticatedBenefitFacts(
        records={record.presentation_digest: record for record in parsed},
        snapshot_digest=expected_sha256,
    )


def load_authenticated_benefit_facts(
    path: Path,
    expected_sha256: str,
) -> AuthenticatedBenefitFacts:
    """Load one canonical, exact-hash benefit-facts manifest into memory."""

    return _authenticated_benefit_facts_from_manifest(
        _read_authenticated_manifest(path, expected_sha256),
        expected_sha256,
    )


def load_authenticated_benefit_facts_bytes(
    value: bytes,
    expected_sha256: str,
) -> AuthenticatedBenefitFacts:
    """Load exact canonical facts from already-frozen in-memory bytes."""

    return _authenticated_benefit_facts_from_manifest(
        _decode_authenticated_manifest_bytes(value, expected_sha256),
        expected_sha256,
    )


def _reviewed_net_benefit_policy_from_manifest(
    value: Mapping[str, object],
) -> NetBenefitPolicy:
    manifest = _closed_mapping(
        value,
        fields=_POLICY_ROOT_FIELDS,
        field_name="net-benefit policy manifest",
    )
    if manifest["schema"] != _POLICY_MANIFEST_SCHEMA:
        raise _fail("net-benefit policy manifest schema is unsupported")
    policy = _closed_mapping(
        manifest["policy"],
        fields=_POLICY_FIELDS,
        field_name="net-benefit policy",
    )
    if (
        policy["policy_schema_id"] != _POLICY_SCHEMA
        or policy["selection_algorithm_id"] != _SELECTION_ALGORITHM
    ):
        raise _fail("net-benefit policy engine contract is unsupported")
    try:
        return NetBenefitPolicy(
            calibration_digest=policy["calibration_digest"],  # type: ignore[arg-type]
            minimum_relevance_ppm=policy["minimum_relevance_ppm"],  # type: ignore[arg-type]
            minimum_trust_ppm=policy["minimum_trust_ppm"],  # type: ignore[arg-type]
            minimum_marginal_net_benefit_u=policy["minimum_marginal_net_benefit_u"],  # type: ignore[arg-type]
            context_token_cost_u=policy["context_token_cost_u"],  # type: ignore[arg-type]
            tool_schema_token_cost_u=policy["tool_schema_token_cost_u"],  # type: ignore[arg-type]
            runtime_millisecond_cost_u=policy["runtime_millisecond_cost_u"],  # type: ignore[arg-type]
            permission_burden_cost_u=policy["permission_burden_cost_u"],  # type: ignore[arg-type]
            credential_burden_cost_u=policy["credential_burden_cost_u"],  # type: ignore[arg-type]
            approval_prompt_cost_u=policy["approval_prompt_cost_u"],  # type: ignore[arg-type]
            process_unit_cost_u=policy["process_unit_cost_u"],  # type: ignore[arg-type]
            child_agent_unit_cost_u=policy["child_agent_unit_cost_u"],  # type: ignore[arg-type]
            new_coverage_bonus_u_per_key=policy["new_coverage_bonus_u_per_key"],  # type: ignore[arg-type]
            overlap_penalty_u_per_key=policy["overlap_penalty_u_per_key"],  # type: ignore[arg-type]
            complementarity_bonus_u=policy["complementarity_bonus_u"],  # type: ignore[arg-type]
            successful_invocation_evidence_ppm=policy["successful_invocation_evidence_ppm"],  # type: ignore[arg-type]
            failed_invocation_evidence_ppm=policy["failed_invocation_evidence_ppm"],  # type: ignore[arg-type]
            effective_outcome_evidence_ppm=policy["effective_outcome_evidence_ppm"],  # type: ignore[arg-type]
            validated_outcome_evidence_ppm=policy["validated_outcome_evidence_ppm"],  # type: ignore[arg-type]
            harmful_outcome_evidence_ppm=policy["harmful_outcome_evidence_ppm"],  # type: ignore[arg-type]
            idle_opportunity_evidence_ppm=policy["idle_opportunity_evidence_ppm"],  # type: ignore[arg-type]
            evidence_prior_observations=policy["evidence_prior_observations"],  # type: ignore[arg-type]
        )
    except (BenefitValidationError, TypeError) as exc:
        raise _fail("net-benefit policy contains invalid bounded values") from exc


def load_reviewed_net_benefit_policy(path: Path, expected_sha256: str) -> NetBenefitPolicy:
    """Load a caller-pinned policy whose complete value set is explicit."""

    return _reviewed_net_benefit_policy_from_manifest(
        _read_authenticated_manifest(path, expected_sha256)
    )


def load_reviewed_net_benefit_policy_bytes(
    value: bytes,
    expected_sha256: str,
) -> NetBenefitPolicy:
    """Load one exact policy from already-owned canonical in-memory bytes."""

    return _reviewed_net_benefit_policy_from_manifest(
        _decode_authenticated_manifest_bytes(value, expected_sha256)
    )


__all__ = [
    "MAX_AUTHENTICATED_BENEFIT_FACT_RECORDS",
    "MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES",
    "AuthenticatedBenefitFacts",
    "AuthenticatedBenefitManifestError",
    "capability_presentation_digest",
    "capability_presentation_mapping",
    "load_authenticated_benefit_facts",
    "load_authenticated_benefit_facts_bytes",
    "load_reviewed_net_benefit_policy",
    "load_reviewed_net_benefit_policy_bytes",
]
