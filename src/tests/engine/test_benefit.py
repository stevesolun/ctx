from __future__ import annotations

import hashlib
import time
from dataclasses import replace
from itertools import permutations

import pytest

from ctx import engine as engine_api
from ctx.engine.benefit import (
    MAX_CANDIDATES,
    BenefitCandidate,
    BenefitSelectionResult,
    EvidenceSummary,
    NetBenefitPolicy,
    ResourceCosts,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _evidence(
    capability_id: str,
    *,
    source_digest: str | None = None,
    evidence_window_digest: str | None = None,
    opportunity_observable: bool = False,
    **counts: object,
) -> EvidenceSummary:
    return EvidenceSummary(
        capability_id=capability_id,
        kind=capability_id.split(":", 1)[0],
        source_digest=source_digest or _digest(capability_id),
        evidence_window_digest=evidence_window_digest
        or _digest(f"evidence-window:{capability_id}"),
        opportunity_observable=opportunity_observable,
        **counts,  # type: ignore[arg-type]
    )


def _policy(**overrides: object) -> NetBenefitPolicy:
    values: dict[str, object] = {
        "calibration_digest": _digest("calibration-v1"),
        "minimum_relevance_ppm": 1,
    }
    values.update(overrides)
    return NetBenefitPolicy(**values)  # type: ignore[arg-type]


def _candidate(
    capability_id: str,
    *,
    availability: str = "executable",
    expected_task_benefit_ppm: int = 600_000,
    relevance_ppm: int = 1_000_000,
    trust_ppm: int = 1_000_000,
    costs: ResourceCosts | None = None,
    source_trusted: bool = True,
    security_approved: bool = True,
    permissions_allowed: bool = True,
    credentials_available: bool = True,
    coverage_keys: tuple[str, ...] = (),
    equivalence_key: str | None = None,
    complements: tuple[str, ...] = (),
    conflicts: tuple[str, ...] = (),
    evidence: EvidenceSummary | None = None,
) -> BenefitCandidate:
    source_digest = _digest(capability_id)
    return BenefitCandidate(
        capability_id=capability_id,
        source_digest=source_digest,
        resource_profile_digest=_digest(f"resource-profile:{capability_id}"),
        availability=availability,
        expected_task_benefit_ppm=expected_task_benefit_ppm,
        relevance_ppm=relevance_ppm,
        trust_ppm=trust_ppm,
        costs=ResourceCosts() if costs is None else costs,
        source_trusted=source_trusted,
        security_approved=security_approved,
        permissions_allowed=permissions_allowed,
        credentials_available=credentials_available,
        coverage_keys=coverage_keys,
        equivalence_key=equivalence_key,
        complements=complements,
        conflicts=conflicts,
        evidence=(
            _evidence(capability_id, source_digest=source_digest) if evidence is None else evidence
        ),
    )


def test_benefit_values_are_available_from_the_stable_engine_surface() -> None:
    assert engine_api.BenefitCandidate is BenefitCandidate
    assert engine_api.EvidenceSummary is EvidenceSummary
    assert engine_api.NetBenefitPolicy is NetBenefitPolicy
    assert engine_api.ResourceCosts is ResourceCosts


def test_advisory_candidate_cannot_displace_positive_executable_value() -> None:
    executable = _candidate(
        "skill:local",
        expected_task_benefit_ppm=100_000,
    )
    advisory = _candidate(
        "skill:manual",
        availability="advisory",
        expected_task_benefit_ppm=1_000_000,
    )
    policy = _policy()

    one = policy.select((advisory, executable), requested_limit=1)
    two = policy.select((advisory, executable), requested_limit=2)

    assert [item.capability_id for item in one.selections] == ["skill:local"]
    assert [item.capability_id for item in two.selections] == [
        "skill:local",
        "skill:manual",
    ]
    assert [item.tier for item in two.selections] == ["executable", "advisory"]


def test_selector_stops_at_positive_marginal_value_and_never_exceeds_five() -> None:
    candidates = tuple(
        _candidate(
            f"skill:positive-{index}",
            expected_task_benefit_ppm=900_000 - index,
        )
        for index in range(6)
    ) + (
        _candidate("skill:zero", expected_task_benefit_ppm=0),
        _candidate(
            "skill:negative",
            expected_task_benefit_ppm=100_000,
            costs=ResourceCosts(context_tokens=2_000),
        ),
    )
    policy = _policy(context_token_cost_u=100)

    result = policy.select(candidates, requested_limit=5)

    assert len(result.selections) == 5
    assert all(item.marginal_net_benefit_u > 0 for item in result.selections)
    assert "skill:zero" not in {item.capability_id for item in result.selections}
    assert "skill:negative" not in {item.capability_id for item in result.selections}


def test_selector_abstains_instead_of_filling_when_every_marginal_is_nonpositive() -> None:
    policy = _policy(context_token_cost_u=100)
    candidates = (
        _candidate("skill:zero", expected_task_benefit_ppm=0),
        _candidate(
            "skill:costly",
            expected_task_benefit_ppm=100_000,
            costs=ResourceCosts(context_tokens=2_000),
        ),
    )

    result = policy.select(candidates)

    assert result.selections == ()
    assert result.abstention_code == "below-net-benefit"


def test_marginal_overlap_stops_at_the_smallest_useful_set() -> None:
    stronger = _candidate(
        "skill:stronger",
        expected_task_benefit_ppm=600_000,
        coverage_keys=("testing",),
    )
    redundant = _candidate(
        "agent:redundant",
        expected_task_benefit_ppm=300_000,
        coverage_keys=("testing",),
    )
    policy = _policy(overlap_penalty_u_per_key=300_000)

    result = policy.select((redundant, stronger))

    assert [item.capability_id for item in result.selections] == ["skill:stronger"]


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (_candidate("skill:unsupported", availability="unsupported"), "host-unsupported"),
        (_candidate("skill:untrusted", source_trusted=False), "source-untrusted"),
        (_candidate("skill:unsafe", security_approved=False), "security-blocked"),
        (_candidate("skill:permission", permissions_allowed=False), "permission-blocked"),
        (
            _candidate("skill:credential", credentials_available=False),
            "credential-unavailable",
        ),
    ],
)
def test_hard_gates_cannot_be_outscored(candidate: BenefitCandidate, reason: str) -> None:
    result = _policy().select((candidate,))

    assert result.selections == ()
    assert result.abstention_code == "no-feasible-capability"
    assert result.assessments[0].tier == "ineligible"
    assert reason in result.assessments[0].reason_codes


def test_unknown_resource_cost_fails_closed() -> None:
    candidate = _candidate("skill:unknown")
    candidate = BenefitCandidate(
        capability_id=candidate.capability_id,
        source_digest=candidate.source_digest,
        availability=candidate.availability,
        expected_task_benefit_ppm=candidate.expected_task_benefit_ppm,
        relevance_ppm=candidate.relevance_ppm,
        trust_ppm=candidate.trust_ppm,
        costs=None,
        evidence=candidate.evidence,
        resource_profile_digest=candidate.resource_profile_digest,
        source_trusted=True,
        security_approved=True,
        permissions_allowed=True,
        credentials_available=True,
    )

    result = _policy().select((candidate,))

    assert result.selections == ()
    assert result.assessments[0].tier == "ineligible"
    assert "resource-cost-unknown" in result.assessments[0].reason_codes


def test_every_resource_quantity_uses_the_frozen_policy_conversion() -> None:
    candidate = _candidate(
        "agent:costed",
        expected_task_benefit_ppm=1_000_000,
        costs=ResourceCosts(
            context_tokens=1,
            tool_schema_tokens=1,
            runtime_millis=1,
            permission_burden_units=1,
            credential_burden_units=1,
            approval_prompts=1,
            process_units=1,
            child_agent_units=1,
        ),
    )
    policy = _policy(
        context_token_cost_u=1,
        tool_schema_token_cost_u=2,
        runtime_millisecond_cost_u=3,
        permission_burden_cost_u=4,
        credential_burden_cost_u=5,
        approval_prompt_cost_u=6,
        process_unit_cost_u=7,
        child_agent_unit_cost_u=8,
    )

    assessment = policy.assess(candidate)

    assert assessment.expected_benefit_u == 1_000_000
    assert assessment.expected_cost_u == 36
    assert assessment.individual_net_benefit_u == 999_964


def test_trust_threshold_is_a_hard_gate_not_a_soft_cost() -> None:
    result = _policy(minimum_trust_ppm=500_000).select(
        (_candidate("skill:low-trust", trust_ppm=499_999),)
    )

    assert result.selections == ()
    assert "trust-below-threshold" in result.assessments[0].reason_codes


def test_selection_and_ties_are_invariant_to_candidate_permutation() -> None:
    candidates = (
        _candidate("skill:b"),
        _candidate("agent:a"),
        _candidate("mcp-server:c"),
    )
    policy = _policy()

    results = tuple(policy.select(order) for order in permutations(candidates))
    observed = {tuple(item.capability_id for item in result.selections) for result in results}

    assert observed == {("agent:a", "mcp-server:c", "skill:b")}
    assert len({result.result_digest for result in results}) == 1
    assert len({result.search_evaluation_count for result in results}) == 1


def test_equivalence_collapses_unless_exact_candidates_are_complementary() -> None:
    lower = _candidate(
        "skill:lower",
        expected_task_benefit_ppm=400_000,
        equivalence_key="python-testing",
    )
    higher = _candidate(
        "agent:higher",
        expected_task_benefit_ppm=700_000,
        equivalence_key="python-testing",
    )
    collapsed = _policy().select((lower, higher))

    complementary_left = _candidate(
        "skill:implementation",
        equivalence_key="python-quality",
        complements=("agent:review",),
    )
    complementary_right = _candidate(
        "agent:review",
        equivalence_key="python-quality",
        complements=("skill:implementation",),
    )
    complementary = _policy(complementarity_bonus_u=10_000).select(
        (complementary_right, complementary_left)
    )

    assert [item.capability_id for item in collapsed.selections] == ["agent:higher"]
    assert {item.capability_id for item in complementary.selections} == {
        "skill:implementation",
        "agent:review",
    }


def test_conflict_is_a_hard_set_gate() -> None:
    preferred = _candidate(
        "skill:preferred",
        expected_task_benefit_ppm=800_000,
        conflicts=("agent:conflicting",),
    )
    conflicting = _candidate(
        "agent:conflicting",
        expected_task_benefit_ppm=700_000,
    )

    result = _policy().select((conflicting, preferred))

    assert [item.capability_id for item in result.selections] == ["skill:preferred"]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ResourceCosts(context_tokens=True),
        lambda: ResourceCosts(runtime_millis=1.5),  # type: ignore[arg-type]
        lambda: ResourceCosts(context_tokens=1_000_001),
        lambda: _evidence("skill:bad-evidence", exposed_count=True),
        lambda: _evidence("skill:bad-evidence", exposed_count=1_000_001),
        lambda: _candidate(
            "skill:bad-score",
            expected_task_benefit_ppm=True,  # type: ignore[arg-type]
        ),
        lambda: _candidate(
            "skill:bad-score",
            relevance_ppm=0.5,  # type: ignore[arg-type]
        ),
        lambda: _policy(context_token_cost_u=True),
        lambda: _policy(context_token_cost_u=1_000_001),
    ],
)
def test_numeric_contract_rejects_bool_float_and_unbounded_values(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_candidate_and_relationship_identities_use_declared_capability_kinds() -> None:
    with pytest.raises(ValueError, match="capability_id"):
        _candidate("plugin:unknown")
    with pytest.raises(ValueError, match="complements"):
        _candidate("skill:known", complements=("plugin:unknown",))


def test_requested_limit_is_a_strict_zero_to_five_integer() -> None:
    policy = _policy()
    candidate = _candidate("skill:one")

    assert policy.select((candidate,), requested_limit=0).selections == ()
    with pytest.raises(ValueError, match="requested_limit"):
        policy.select((candidate,), requested_limit=6)
    with pytest.raises(ValueError, match="requested_limit"):
        policy.select((candidate,), requested_limit=True)


def test_exposure_has_zero_adjustment_and_outcome_evidence_is_ordered() -> None:
    policy = _policy(evidence_prior_observations=1)

    def assessed(evidence: EvidenceSummary) -> tuple[int, int]:
        value = policy.assess(_candidate("skill:evidence", evidence=evidence))
        return value.evidence_adjustment_ppm, value.expected_benefit_u

    none = assessed(_evidence("skill:evidence"))
    exposed = assessed(_evidence("skill:evidence", opportunities_observed=10, exposed_count=10))
    succeeded = assessed(
        _evidence(
            "skill:evidence",
            opportunities_observed=1,
            successful_invocations=1,
        )
    )
    effective = assessed(
        _evidence(
            "skill:evidence",
            opportunities_observed=1,
            exposed_count=1,
            effective_outcomes=1,
        )
    )
    validated = assessed(
        _evidence(
            "skill:evidence",
            opportunities_observed=1,
            exposed_count=1,
            effective_outcomes=1,
            validated_outcomes=1,
        )
    )
    failed = assessed(
        _evidence(
            "skill:evidence",
            opportunities_observed=1,
            failed_invocations=1,
        )
    )
    harmful = assessed(
        _evidence(
            "skill:evidence",
            opportunities_observed=1,
            exposed_count=1,
            harmful_outcomes=1,
        )
    )

    assert exposed == none
    assert harmful < failed < none < succeeded < effective < validated


def test_nonuse_is_negative_only_when_opportunity_is_declared_observable() -> None:
    policy = _policy(evidence_prior_observations=1)
    hidden = policy.assess(
        _candidate(
            "skill:hidden-opportunity",
            evidence=_evidence("skill:hidden-opportunity", opportunities_observed=5),
        )
    )
    observable = policy.assess(
        _candidate(
            "skill:observable-opportunity",
            evidence=_evidence(
                "skill:observable-opportunity",
                opportunities_observed=5,
                opportunity_observable=True,
            ),
        )
    )

    assert hidden.evidence_adjustment_ppm == 0
    assert observable.evidence_adjustment_ppm < 0


def test_candidate_requires_explicit_authenticated_gate_cost_and_evidence_facts() -> None:
    capability_id = "skill:explicit"
    source_digest = _digest(capability_id)
    values: dict[str, object] = {
        "capability_id": capability_id,
        "source_digest": source_digest,
        "resource_profile_digest": _digest("explicit-resource-profile"),
        "availability": "executable",
        "expected_task_benefit_ppm": 500_000,
        "relevance_ppm": 500_000,
        "trust_ppm": 500_000,
        "costs": ResourceCosts(),
        "evidence": _evidence(capability_id, source_digest=source_digest),
        "source_trusted": True,
        "security_approved": True,
        "permissions_allowed": True,
        "credentials_available": True,
    }

    for omitted in (
        "resource_profile_digest",
        "evidence",
        "source_trusted",
        "security_approved",
        "permissions_allowed",
        "credentials_available",
    ):
        incomplete = dict(values)
        incomplete.pop(omitted)
        with pytest.raises(TypeError):
            BenefitCandidate(**incomplete)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        NetBenefitPolicy(minimum_relevance_ppm=1)  # type: ignore[call-arg]


def test_candidate_rejects_evidence_bound_to_another_identity_or_source() -> None:
    with pytest.raises(ValueError, match="evidence"):
        _candidate(
            "skill:bound",
            evidence=_evidence("skill:other"),
        )
    with pytest.raises(ValueError, match="evidence"):
        _candidate(
            "skill:bound",
            evidence=_evidence("skill:bound", source_digest=_digest("other-source")),
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _evidence(
            "skill:orphan-validation",
            opportunities_observed=1,
            exposed_count=1,
            effective_outcomes=0,
            validated_outcomes=1,
        ),
        lambda: _evidence(
            "skill:orphan-effect",
            opportunities_observed=1,
            effective_outcomes=1,
        ),
        lambda: _evidence(
            "agent:orphan-effect",
            opportunities_observed=1,
            exposed_count=1,
            effective_outcomes=1,
        ),
        lambda: _evidence(
            "mcp-server:orphan-effect",
            opportunities_observed=1,
            effective_outcomes=1,
        ),
        lambda: _evidence(
            "harness:orphan-effect",
            opportunities_observed=1,
            effective_outcomes=1,
        ),
        lambda: _evidence(
            "skill:orphan-harm",
            opportunities_observed=1,
            harmful_outcomes=1,
        ),
    ],
)
def test_outcome_evidence_requires_attributable_prior_observation(factory: object) -> None:
    with pytest.raises(ValueError, match="validated|effective|harmful|attributable|invocation"):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _evidence("skill:no-opportunity", exposed_count=1),
        lambda: _evidence(
            "skill:too-many-exposures",
            opportunities_observed=1,
            exposed_count=2,
        ),
        lambda: _evidence(
            "agent:too-many-attempts",
            opportunities_observed=1,
            successful_invocations=1,
            failed_invocations=1,
        ),
        lambda: _evidence(
            "skill:effect-without-exposure",
            opportunities_observed=1,
            successful_invocations=1,
            effective_outcomes=1,
        ),
        lambda: _evidence(
            "agent:too-many-effects",
            opportunities_observed=2,
            successful_invocations=1,
            effective_outcomes=2,
        ),
        lambda: _evidence(
            "skill:too-many-harms",
            opportunities_observed=2,
            exposed_count=1,
            harmful_outcomes=2,
        ),
        lambda: _evidence(
            "skill:outcomes-exceed-window",
            opportunities_observed=1,
            exposed_count=1,
            effective_outcomes=1,
            harmful_outcomes=1,
        ),
    ],
)
def test_evidence_counts_obey_one_strict_observation_window(factory: object) -> None:
    with pytest.raises(ValueError, match="opportunit|expos|attempt|effective|harmful|window"):
        factory()  # type: ignore[operator]


def test_evidence_window_digest_is_required_and_candidate_bound_evidence_is_explicit() -> None:
    with pytest.raises(TypeError):
        EvidenceSummary(  # type: ignore[call-arg]
            capability_id="skill:window",
            kind="skill",
            source_digest=_digest("skill:window"),
            opportunity_observable=False,
        )


def test_minimum_relevance_is_a_hard_gate() -> None:
    result = _policy(minimum_relevance_ppm=500_000).select(
        (_candidate("skill:low-relevance", relevance_ppm=499_999),)
    )

    assert result.selections == ()
    assert result.assessments[0].tier == "ineligible"
    assert "relevance-below-threshold" in result.assessments[0].reason_codes


def test_complementarity_must_be_reciprocal_and_cannot_contradict_conflict() -> None:
    unilateral = _candidate(
        "skill:unilateral",
        expected_task_benefit_ppm=600_000,
        equivalence_key="same-need",
        complements=("agent:stronger",),
    )
    stronger = _candidate(
        "agent:stronger",
        expected_task_benefit_ppm=700_000,
        equivalence_key="same-need",
    )

    result = _policy(complementarity_bonus_u=1_000_000).select((unilateral, stronger))

    assert [item.capability_id for item in result.selections] == ["agent:stronger"]
    with pytest.raises(ValueError, match="complement|conflict"):
        _candidate(
            "skill:contradictory",
            complements=("agent:peer",),
            conflicts=("agent:peer",),
        )


def test_multistart_search_exchanges_one_conflicting_candidate_for_better_pair() -> None:
    single = _candidate(
        "skill:single",
        expected_task_benefit_ppm=600_000,
        conflicts=("agent:left", "mcp-server:right"),
    )
    left = _candidate("agent:left", expected_task_benefit_ppm=400_000)
    right = _candidate("mcp-server:right", expected_task_benefit_ppm=400_000)

    observed = {
        tuple(item.capability_id for item in _policy().select(order).selections)
        for order in permutations((single, left, right))
    }

    assert observed == {("agent:left", "mcp-server:right")}


def test_direct_marginal_admits_zero_individual_value_with_new_coverage() -> None:
    candidate = _candidate(
        "skill:coverage-only",
        expected_task_benefit_ppm=0,
        coverage_keys=("uncovered-need",),
    )

    result = _policy(new_coverage_bonus_u_per_key=10).select((candidate,))

    assert [item.capability_id for item in result.selections] == ["skill:coverage-only"]
    assert result.selections[0].individual_net_benefit_u == 0
    assert result.selections[0].marginal_net_benefit_u == 10


def test_advisory_negative_individual_value_can_help_frozen_executable_via_complement() -> None:
    executable = _candidate(
        "skill:implementation",
        complements=("agent:review",),
    )
    advisory = _candidate(
        "agent:review",
        availability="advisory",
        expected_task_benefit_ppm=0,
        costs=ResourceCosts(context_tokens=1),
        complements=("skill:implementation",),
    )

    result = _policy(
        context_token_cost_u=10,
        complementarity_bonus_u=20,
    ).select((advisory, executable))

    assert [item.capability_id for item in result.selections] == [
        "skill:implementation",
        "agent:review",
    ]
    assert result.selections[1].individual_net_benefit_u == -10
    assert result.selections[1].marginal_net_benefit_u == 10


def test_advisory_admission_cannot_invalidate_frozen_value_and_hide_valid_refill() -> None:
    executable = _candidate(
        "skill:frozen",
        expected_task_benefit_ppm=10,
        coverage_keys=("x",),
    )
    high_but_invalid = _candidate(
        "agent:invalid-overlap",
        availability="advisory",
        expected_task_benefit_ppm=100,
        coverage_keys=("x",),
    )
    lower_valid = _candidate(
        "mcp-server:valid-distinct",
        availability="advisory",
        expected_task_benefit_ppm=50,
        coverage_keys=("y",),
    )
    policy = _policy(overlap_penalty_u_per_key=20)

    results = tuple(
        policy.select(order, requested_limit=2)
        for order in permutations((executable, high_but_invalid, lower_valid))
    )

    assert {tuple(item.capability_id for item in result.selections) for result in results} == {
        ("skill:frozen", "mcp-server:valid-distinct")
    }
    assert len({result.result_digest for result in results}) == 1
    for result in results:
        policy.validate_result(result)
        assert all(item.marginal_net_benefit_u >= 1 for item in result.selections)


def test_two_mutually_nonpositive_unselected_candidates_cannot_rescue_each_other() -> None:
    left = _candidate(
        "skill:left-negative",
        expected_task_benefit_ppm=0,
        costs=ResourceCosts(context_tokens=1),
        complements=("agent:right-negative",),
    )
    right = _candidate(
        "agent:right-negative",
        expected_task_benefit_ppm=0,
        costs=ResourceCosts(context_tokens=1),
        complements=("skill:left-negative",),
    )

    result = _policy(
        context_token_cost_u=10,
        complementarity_bonus_u=1_000,
    ).select((left, right))

    assert result.selections == ()
    assert result.abstention_code == "below-net-benefit"


def test_policy_digest_binds_schema_algorithm_and_calibration() -> None:
    first = _policy()
    changed = _policy(calibration_digest=_digest("calibration-v2"))
    changed_weight = _policy(context_token_cost_u=1)

    assert first.policy_schema_id == "ctx.net-benefit-policy-v3"
    assert first.selection_algorithm_id == "ctx.greedy-bounded-subset-exchange-v1"
    assert first.policy_digest != changed.policy_digest
    assert first.policy_digest != changed_weight.policy_digest


def test_selection_result_rejects_duplicates_and_nonmatching_assessment_projection() -> None:
    result = _policy().select((_candidate("skill:projection"),))
    selection = result.selections[0]

    with pytest.raises(ValueError, match="duplicate"):
        BenefitSelectionResult(
            selections=(selection, selection),
            assessments=result.assessments,
            abstention_code=None,
            policy_digest=result.policy_digest,
            requested_limit=result.requested_limit,
            candidate_pool_count=result.candidate_pool_count,
            search_evaluation_count=result.search_evaluation_count,
            result_digest=_digest("forged-result"),
        )
    with pytest.raises(ValueError, match="assessment"):
        replace(
            result,
            selections=(replace(selection, source_digest=_digest("substituted-source")),),
        )


def test_result_digest_metadata_and_policy_revalidation_reject_consistent_tamper() -> None:
    policy = _policy()
    result = policy.select(
        (_candidate("skill:a-result"), _candidate("agent:b-result")),
        requested_limit=1,
    )

    assert result.result_schema_id == "ctx.benefit-selection-result-v1"
    assert result.requested_limit == 1
    assert result.candidate_pool_count == 2
    assert result.search_evaluation_count > 0
    assert result.recomputed_result_digest == result.result_digest
    policy.validate_result(result)
    with pytest.raises(ValueError, match="result_digest"):
        replace(result, result_digest=_digest("tampered-result-digest"))

    selection = result.selections[0]
    tampered = BenefitSelectionResult._create(
        selections=(
            replace(
                selection,
                marginal_net_benefit_u=selection.marginal_net_benefit_u + 1,
            ),
        ),
        assessments=result.assessments,
        abstention_code=result.abstention_code,
        policy_digest=result.policy_digest,
        requested_limit=result.requested_limit,
        candidate_pool_count=result.candidate_pool_count,
        search_evaluation_count=result.search_evaluation_count,
    )
    with pytest.raises(ValueError, match="revalidation|match"):
        policy.validate_result(tampered)

    changed_assessment = replace(
        result.assessments[-1],
        individual_net_benefit_u=result.assessments[-1].individual_net_benefit_u - 1,
    )
    tampered_assessment = BenefitSelectionResult._create(
        selections=result.selections,
        assessments=(*result.assessments[:-1], changed_assessment),
        abstention_code=result.abstention_code,
        policy_digest=result.policy_digest,
        requested_limit=result.requested_limit,
        candidate_pool_count=result.candidate_pool_count,
        search_evaluation_count=result.search_evaluation_count,
    )
    with pytest.raises(ValueError, match="revalidation|assessment"):
        policy.validate_result(tampered_assessment)


def test_result_enforces_canonical_order_and_exact_abstention_semantics() -> None:
    policy = _policy()
    selected = policy.select(
        (_candidate("skill:z-order"), _candidate("agent:a-order")),
        requested_limit=2,
    )
    with pytest.raises(ValueError, match="canonical"):
        BenefitSelectionResult._create(
            selections=tuple(reversed(selected.selections)),
            assessments=selected.assessments,
            abstention_code=None,
            policy_digest=selected.policy_digest,
            requested_limit=selected.requested_limit,
            candidate_pool_count=selected.candidate_pool_count,
            search_evaluation_count=selected.search_evaluation_count,
        )

    limit_zero = policy.select((_candidate("skill:limit-zero"),), requested_limit=0)
    no_candidates = policy.select(())
    below = policy.select((_candidate("skill:zero-benefit", expected_task_benefit_ppm=0),))
    assert (limit_zero.abstention_code, limit_zero.search_evaluation_count) == (
        "limit-zero",
        0,
    )
    assert no_candidates.abstention_code == "no-feasible-capability"
    assert below.abstention_code == "below-net-benefit"

    with pytest.raises(ValueError, match="abstention"):
        BenefitSelectionResult._create(
            selections=(),
            assessments=below.assessments,
            abstention_code="no-feasible-capability",
            policy_digest=below.policy_digest,
            requested_limit=below.requested_limit,
            candidate_pool_count=below.candidate_pool_count,
            search_evaluation_count=below.search_evaluation_count,
        )


def test_candidate_pool_accepts_exact_bound_and_rejects_one_more() -> None:
    coverage_keys = tuple(f"need-{index:02d}" for index in range(64))
    candidates = tuple(
        _candidate(
            f"skill:bounded-{index:03d}",
            coverage_keys=coverage_keys,
        )
        for index in range(MAX_CANDIDATES + 1)
    )

    # Bound algorithmic CPU cost without charging this worker for scheduler
    # starvation while the full suite is running under xdist. The bound catches
    # a pathological blowup, not a slow machine: the real algorithmic guard is
    # search_evaluation_count below. Calibrated on a laptop it read under 5s and
    # measured 14.6s on a shared GitHub runner, so it is set where only a
    # genuine regression can trip it.
    started = time.process_time()
    accepted = _policy().select(candidates[:-1], requested_limit=5)
    elapsed = time.process_time() - started

    assert len(accepted.assessments) == MAX_CANDIDATES
    assert len(accepted.selections) == 5
    assert accepted.search_evaluation_count < 100_000
    assert elapsed < 60.0
    with pytest.raises(ValueError, match="bounded limit"):
        _policy().select(candidates, requested_limit=1)


def test_pool_rejects_cross_record_complement_conflict_contradiction() -> None:
    complementing = _candidate(
        "skill:complementing",
        complements=("agent:conflicting",),
    )
    conflicting = _candidate(
        "agent:conflicting",
        conflicts=("skill:complementing",),
    )

    with pytest.raises(ValueError, match="complement|conflict"):
        _policy().select((complementing, conflicting))
