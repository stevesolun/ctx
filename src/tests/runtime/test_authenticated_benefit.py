from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

from ctx.engine.planner import CapabilityCandidate, WorkObservation
from ctx.runtime.authenticated_benefit import (
    MAX_AUTHENTICATED_BENEFIT_FACT_RECORDS,
    MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES,
    AuthenticatedBenefitManifestError,
    load_authenticated_benefit_facts,
    load_reviewed_net_benefit_policy,
    load_reviewed_net_benefit_policy_bytes,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _write_manifest(path: Path, value: object) -> str:
    body = _canonical_bytes(value)
    path.write_bytes(body)
    path.chmod(0o444)
    return hashlib.sha256(body).hexdigest()


def _presentation(
    *,
    capability_id: str = "skill:python-tdd",
    source_digest: str | None = None,
    normalized_score_ppm: int = 700_000,
    actionability: str = "load",
    equivalence_key: str | None = "python-test-method",
    matching_signals: tuple[str, ...] = ("python", "testing"),
    reason_codes: tuple[str, ...] = ("graph-match", "signal-match"),
    install_descriptor_digest: str | None = None,
    install_plan_digest: str | None = None,
) -> CapabilityCandidate:
    kind, name = capability_id.split(":", 1)
    if actionability == "install":
        install_descriptor_digest = install_descriptor_digest or _digest(
            f"descriptor:{capability_id}"
        )
        install_plan_digest = install_plan_digest or _digest(f"plan:{capability_id}")
    return CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=source_digest or _digest(f"source:{capability_id}"),
        normalized_score_ppm=normalized_score_ppm,
        matching_signals=matching_signals,
        reason_codes=reason_codes,
        actionability=actionability,
        install_descriptor_digest=install_descriptor_digest,
        install_plan_digest=install_plan_digest,
        equivalence_key=equivalence_key,
    )


def _presentation_mapping(presentation: CapabilityCandidate) -> dict[str, object]:
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


def _presentation_digest(presentation: CapabilityCandidate) -> str:
    return hashlib.sha256(_canonical_bytes(_presentation_mapping(presentation))).hexdigest()


def _facts_record(
    *,
    presentation: CapabilityCandidate | None = None,
    availability: str | None = None,
    expected_task_benefit_ppm: int = 800_000,
) -> dict[str, object]:
    presentation = presentation or _presentation()
    return {
        "availability": availability
        or ("advisory" if presentation.actionability == "manual" else "executable"),
        "complements": [],
        "conflicts": [],
        "costs": {
            "approval_prompts": 0,
            "child_agent_units": 0,
            "context_tokens": 120,
            "credential_burden_units": 0,
            "permission_burden_units": 0,
            "process_units": 0,
            "runtime_millis": 25,
            "tool_schema_tokens": 0,
        },
        "coverage_keys": ["python", "testing"],
        "credentials_available": True,
        "expected_task_benefit_ppm": expected_task_benefit_ppm,
        "maximum_relevance_ppm": min(650_000, presentation.normalized_score_ppm),
        "permissions_allowed": True,
        "presentation": _presentation_mapping(presentation),
        "presentation_digest": _presentation_digest(presentation),
        "resource_profile_digest": _digest(f"profile:{presentation.capability_id}"),
        "security_approved": True,
        "source_trusted": True,
        "trust_ppm": 900_000,
    }


def _facts_manifest(*records: dict[str, object]) -> dict[str, object]:
    values = list(records or (_facts_record(),))
    values.sort(key=lambda value: cast(str, value.get("presentation_digest", "")))
    return {
        "records": values,
        "schema": "ctx.authenticated-benefit-facts-v1",
    }


def _observation(*, signals: tuple[str, ...] = ("python", "testing")) -> WorkObservation:
    return WorkObservation(signals=signals, languages=("python",))


def _policy_manifest() -> dict[str, object]:
    return {
        "policy": {
            "approval_prompt_cost_u": 2,
            "calibration_digest": _digest("reviewed-calibration-v1"),
            "child_agent_unit_cost_u": 8,
            "complementarity_bonus_u": 13,
            "context_token_cost_u": 1,
            "credential_burden_cost_u": 5,
            "effective_outcome_evidence_ppm": 500_000,
            "evidence_prior_observations": 3,
            "failed_invocation_evidence_ppm": -250_000,
            "harmful_outcome_evidence_ppm": -1_000_000,
            "idle_opportunity_evidence_ppm": -100_000,
            "minimum_marginal_net_benefit_u": 7,
            "minimum_relevance_ppm": 200_000,
            "minimum_trust_ppm": 300_000,
            "new_coverage_bonus_u_per_key": 11,
            "overlap_penalty_u_per_key": 12,
            "permission_burden_cost_u": 4,
            "process_unit_cost_u": 7,
            "runtime_millisecond_cost_u": 3,
            "selection_algorithm_id": "ctx.greedy-bounded-subset-exchange-v1",
            "successful_invocation_evidence_ppm": 100_000,
            "tool_schema_token_cost_u": 2,
            "validated_outcome_evidence_ppm": 1_000_000,
            "policy_schema_id": "ctx.net-benefit-policy-v3",
        },
        "schema": "ctx.reviewed-net-benefit-policy-v1",
    }


def test_authenticated_facts_bind_exact_candidate_observation_and_relevance(tmp_path: Path) -> None:
    path = tmp_path / "benefit-facts.json"
    expected_sha256 = _write_manifest(path, _facts_manifest())

    facts = load_authenticated_benefit_facts(path, expected_sha256)
    presentation = _presentation(normalized_score_ppm=700_000)
    first = facts.benefit_candidate(presentation, _observation())
    second = facts.benefit_candidate(
        presentation,
        _observation(signals=("python", "security", "testing")),
    )

    assert facts.benefit_facts_snapshot_digest == expected_sha256
    assert first is not None
    assert first.capability_id == presentation.capability_id
    assert first.source_digest == presentation.source_digest
    assert first.relevance_ppm == 650_000
    assert first.expected_task_benefit_ppm == 800_000
    assert first.costs is not None and first.costs.context_tokens == 120
    assert first.coverage_keys == ("python", "testing")
    assert first.evidence.opportunity_observable is False
    assert first.evidence.opportunities_observed == 0
    assert first.evidence.exposed_count == 0
    assert first.evidence.successful_invocations == 0
    assert first.evidence.failed_invocations == 0
    assert first.evidence.effective_outcomes == 0
    assert first.evidence.validated_outcomes == 0
    assert first.evidence.harmful_outcomes == 0
    assert second is not None
    assert second.evidence.evidence_window_digest != first.evidence.evidence_window_digest


def test_authenticated_facts_return_none_for_missing_substituted_or_unbound_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "benefit-facts.json"
    expected_sha256 = _write_manifest(path, _facts_manifest())
    facts = load_authenticated_benefit_facts(path, expected_sha256)

    missing = CapabilityCandidate(
        capability_id="agent:reviewer",
        kind="agent",
        name="reviewer",
        source_digest=_digest("source:reviewer"),
        normalized_score_ppm=700_000,
        matching_signals=("python",),
        reason_codes=("graph-match",),
        actionability="manual",
    )

    assert facts.benefit_candidate(missing, _observation()) is None
    assert (
        facts.benefit_candidate(
            _presentation(source_digest=_digest("substituted-source")),
            _observation(),
        )
        is None
    )
    assert (
        facts.benefit_candidate(
            _presentation(),
            _observation(signals=("rust",)),
        )
        is None
    )


def test_authenticated_facts_bind_load_install_and_manual_as_distinct_presentations(
    tmp_path: Path,
) -> None:
    shared_source = _digest("same-source-cannot-authorize-actionability")
    load = _presentation(source_digest=shared_source, actionability="load")
    install = _presentation(source_digest=shared_source, actionability="install")
    manual = _presentation(source_digest=shared_source, actionability="manual")
    path = tmp_path / "three-presentations.json"
    expected_sha256 = _write_manifest(
        path,
        _facts_manifest(
            _facts_record(presentation=load, expected_task_benefit_ppm=800_000),
            _facts_record(presentation=install, expected_task_benefit_ppm=700_000),
            _facts_record(presentation=manual, expected_task_benefit_ppm=600_000),
        ),
    )

    facts = load_authenticated_benefit_facts(path, expected_sha256)

    load_facts = facts.benefit_candidate(load, _observation())
    install_facts = facts.benefit_candidate(install, _observation())
    manual_facts = facts.benefit_candidate(manual, _observation())
    assert load_facts is not None and load_facts.availability == "executable"
    assert install_facts is not None and install_facts.availability == "executable"
    assert manual_facts is not None and manual_facts.availability == "advisory"
    assert load_facts.expected_task_benefit_ppm == 800_000
    assert install_facts.expected_task_benefit_ppm == 700_000
    assert manual_facts.expected_task_benefit_ppm == 600_000
    assert (
        len(
            {
                load_facts.evidence.evidence_window_digest,
                install_facts.evidence.evidence_window_digest,
                manual_facts.evidence.evidence_window_digest,
            }
        )
        == 3
    )


def test_authenticated_facts_reject_complete_presentation_substitution(tmp_path: Path) -> None:
    shared_source = _digest("shared-source")
    declared = _presentation(source_digest=shared_source)
    path = tmp_path / "exact-presentation.json"
    expected_sha256 = _write_manifest(
        path,
        _facts_manifest(_facts_record(presentation=declared)),
    )
    facts = load_authenticated_benefit_facts(path, expected_sha256)

    substitutions = (
        _presentation(source_digest=shared_source, actionability="manual"),
        _presentation(source_digest=shared_source, actionability="install"),
        _presentation(source_digest=shared_source, normalized_score_ppm=699_999),
        _presentation(source_digest=shared_source, equivalence_key="substituted-equivalence"),
        _presentation(
            source_digest=shared_source,
            matching_signals=("python", "security", "testing"),
        ),
        _presentation(
            source_digest=shared_source,
            reason_codes=("graph-match", "name-match", "signal-match"),
        ),
    )
    observation = _observation(signals=("python", "security", "testing"))

    assert facts.benefit_candidate(declared, observation) is not None
    assert all(facts.benefit_candidate(value, observation) is None for value in substitutions)


def test_authenticated_facts_bind_both_install_digests(tmp_path: Path) -> None:
    declared = _presentation(actionability="install")
    path = tmp_path / "install-presentation.json"
    expected_sha256 = _write_manifest(
        path,
        _facts_manifest(_facts_record(presentation=declared)),
    )
    facts = load_authenticated_benefit_facts(path, expected_sha256)

    changed_descriptor = _presentation(
        actionability="install",
        install_descriptor_digest=_digest("changed-descriptor"),
    )
    changed_plan = _presentation(
        actionability="install",
        install_plan_digest=_digest("changed-plan"),
    )

    assert facts.benefit_candidate(declared, _observation()) is not None
    assert facts.benefit_candidate(changed_descriptor, _observation()) is None
    assert facts.benefit_candidate(changed_plan, _observation()) is None


def test_evidence_digest_binds_every_standardized_presentation_field(tmp_path: Path) -> None:
    first_presentation = _presentation()
    second_presentation = _presentation(equivalence_key="different-exact-presentation")
    path = tmp_path / "full-presentation-digest.json"
    expected_sha256 = _write_manifest(
        path,
        _facts_manifest(
            _facts_record(presentation=first_presentation),
            _facts_record(presentation=second_presentation),
        ),
    )
    facts = load_authenticated_benefit_facts(path, expected_sha256)

    first = facts.benefit_candidate(first_presentation, _observation())
    second = facts.benefit_candidate(second_presentation, _observation())

    assert first is not None and second is not None
    assert _presentation_digest(first_presentation) != _presentation_digest(second_presentation)
    assert first.evidence.evidence_window_digest != second.evidence.evidence_window_digest


@pytest.mark.parametrize(
    ("actionability", "availability"),
    [("manual", "executable"), ("load", "advisory"), ("install", "advisory")],
)
def test_authenticated_facts_enforce_actionability_availability_contract(
    tmp_path: Path,
    actionability: str,
    availability: str,
) -> None:
    presentation = _presentation(actionability=actionability)
    path = tmp_path / f"{actionability}-{availability}.json"
    expected_sha256 = _write_manifest(
        path,
        _facts_manifest(
            _facts_record(presentation=presentation, availability=availability),
        ),
    )

    with pytest.raises(AuthenticatedBenefitManifestError):
        load_authenticated_benefit_facts(path, expected_sha256)


def test_authenticated_facts_support_more_than_retrieval_pool_limit(tmp_path: Path) -> None:
    presentations = tuple(
        _presentation(capability_id=f"skill:tool-{index:04d}") for index in range(513)
    )
    path = tmp_path / "production-scale-facts.json"
    expected_sha256 = _write_manifest(
        path,
        _facts_manifest(*(_facts_record(presentation=value) for value in presentations)),
    )

    facts = load_authenticated_benefit_facts(path, expected_sha256)

    assert facts.benefit_candidate(presentations[-1], _observation()) is not None


def test_authenticated_facts_enforce_independent_record_and_byte_bounds(tmp_path: Path) -> None:
    too_many_path = tmp_path / "too-many.json"
    too_many_sha256 = _write_manifest(
        too_many_path,
        _facts_manifest(*({} for _ in range(MAX_AUTHENTICATED_BENEFIT_FACT_RECORDS + 1))),
    )
    with pytest.raises(AuthenticatedBenefitManifestError):
        load_authenticated_benefit_facts(too_many_path, too_many_sha256)

    oversized_path = tmp_path / "oversized.json"
    oversized_body = b" " * (MAX_AUTHENTICATED_BENEFIT_MANIFEST_BYTES + 1)
    oversized_path.write_bytes(oversized_body)
    oversized_path.chmod(0o444)
    oversized_sha256 = hashlib.sha256(oversized_body).hexdigest()
    with pytest.raises(AuthenticatedBenefitManifestError):
        load_authenticated_benefit_facts(oversized_path, oversized_sha256)


@pytest.mark.parametrize(
    "mutation",
    [
        "extra-field",
        "duplicate-record",
        "bad-bound",
        "presentation-digest",
        "relevance-over-presentation",
    ],
)
def test_authenticated_facts_reject_non_closed_or_malformed_records(
    tmp_path: Path,
    mutation: str,
) -> None:
    record = _facts_record()
    manifest = _facts_manifest(record)
    if mutation == "extra-field":
        record["description"] = "graph prose is not benefit authority"
    elif mutation == "duplicate-record":
        manifest["records"] = [record, dict(record)]
    elif mutation == "bad-bound":
        record["trust_ppm"] = 1_000_001
    elif mutation == "presentation-digest":
        record["presentation_digest"] = _digest("not-the-full-presentation")
    else:
        record["maximum_relevance_ppm"] = 700_001
    path = tmp_path / f"{mutation}.json"
    expected_sha256 = _write_manifest(path, manifest)

    with pytest.raises(AuthenticatedBenefitManifestError):
        load_authenticated_benefit_facts(path, expected_sha256)


@pytest.mark.parametrize("failure", ["wrong-digest", "writable", "symlink", "noncanonical"])
def test_authenticated_facts_require_exact_read_only_canonical_regular_file(
    tmp_path: Path,
    failure: str,
) -> None:
    path = tmp_path / "benefit-facts.json"
    expected_sha256 = _write_manifest(path, _facts_manifest())
    target = path
    if failure == "wrong-digest":
        expected_sha256 = _digest("not-the-manifest")
    elif failure == "writable":
        path.chmod(0o644)
    elif failure == "symlink":
        target = tmp_path / "benefit-facts-link.json"
        target.symlink_to(path)
    else:
        path.chmod(0o644)
        path.write_text(json.dumps(_facts_manifest(), indent=2), encoding="utf-8")
        path.chmod(0o444)
        expected_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(AuthenticatedBenefitManifestError):
        load_authenticated_benefit_facts(target, expected_sha256)


def test_reviewed_policy_loader_requires_every_explicit_closed_field(tmp_path: Path) -> None:
    path = tmp_path / "net-benefit-policy.json"
    expected_sha256 = _write_manifest(path, _policy_manifest())

    policy = load_reviewed_net_benefit_policy(path, expected_sha256)
    body = _canonical_bytes(_policy_manifest())
    from_bytes = load_reviewed_net_benefit_policy_bytes(
        body,
        hashlib.sha256(body).hexdigest(),
    )

    assert from_bytes == policy
    assert policy.calibration_digest == _digest("reviewed-calibration-v1")
    assert policy.minimum_relevance_ppm == 200_000
    assert policy.minimum_trust_ppm == 300_000
    assert policy.minimum_marginal_net_benefit_u == 7
    assert policy.context_token_cost_u == 1
    assert policy.evidence_prior_observations == 3

    missing = _policy_manifest()
    missing_policy = cast(dict[str, object], missing["policy"])
    del missing_policy["context_token_cost_u"]
    missing_path = tmp_path / "missing-policy-field.json"
    missing_sha256 = _write_manifest(missing_path, missing)
    with pytest.raises(AuthenticatedBenefitManifestError):
        load_reviewed_net_benefit_policy(missing_path, missing_sha256)

    extra = _policy_manifest()
    extra_policy = cast(dict[str, object], extra["policy"])
    extra_policy["unreviewed_default"] = 1
    extra_path = tmp_path / "extra-policy-field.json"
    extra_sha256 = _write_manifest(extra_path, extra)
    with pytest.raises(AuthenticatedBenefitManifestError):
        load_reviewed_net_benefit_policy(extra_path, extra_sha256)


def test_loader_rejects_file_replaced_during_authenticated_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "benefit-facts.json"
    expected_sha256 = _write_manifest(path, _facts_manifest())
    original_fstat = os.fstat
    calls = 0

    def changed_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        result = original_fstat(fd)
        if calls == 2:
            values = list(result)
            values[1] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(os, "fstat", changed_fstat)

    with pytest.raises(AuthenticatedBenefitManifestError):
        load_authenticated_benefit_facts(path, expected_sha256)
