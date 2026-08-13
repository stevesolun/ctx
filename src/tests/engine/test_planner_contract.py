from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import permutations

import pytest

from ctx.engine.protocol import EngineEvent, ScopeRef
from ctx.engine.planner import (
    BoundedCapabilityPlanner,
    CandidateSourceUnavailable,
    CapabilityCandidate,
    CapabilityPlan,
    CapabilitySelection,
    PlannerValidationError,
    ReplayDecisionPlanner,
    WorkObservation,
)
from ctx.engine.replay import (
    DefaultReplayInputFactory,
    PlanningContext,
    StructuredSurrogate,
)
from ctx.engine.state import CapabilityState, EngineState, LeaseRef


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _candidate(
    capability_id: str,
    score: int,
    *,
    digest_salt: str | None = None,
    actionability: str = "load",
    equivalence_key: str | None = None,
    matching_signals: tuple[str, ...] = ("python",),
    install_descriptor_digest: str | None = None,
    install_plan_digest: str | None = None,
) -> CapabilityCandidate:
    kind, name = capability_id.split(":", 1)
    return CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=_digest(digest_salt or capability_id),
        normalized_score_ppm=score,
        matching_signals=matching_signals,
        reason_codes=("signal-match",),
        actionability=actionability,
        install_descriptor_digest=(
            _digest(f"descriptor:{capability_id}")
            if actionability == "install" and install_descriptor_digest is None
            else install_descriptor_digest
        ),
        install_plan_digest=(
            _digest(f"install:{capability_id}")
            if actionability == "install" and install_plan_digest is None
            else install_plan_digest
        ),
        equivalence_key=equivalence_key,
    )


@dataclass
class StaticSource:
    values: Sequence[CapabilityCandidate]
    catalog_snapshot_digest: str = _digest("catalog")
    calls: int = 0

    def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]:
        assert observation.signals == ("python",)
        self.calls += 1
        return self.values


@dataclass
class FlexibleSource:
    values: Sequence[CapabilityCandidate]
    catalog_snapshot_digest: str = _digest("catalog")

    def retrieve(self, _observation: WorkObservation) -> Sequence[CapabilityCandidate]:
        return self.values


def _observation(
    *,
    requested_limit: int = 5,
    baseline: tuple[str, ...] = (),
    active: tuple[str, ...] = (),
    rejected: tuple[str, ...] = (),
) -> WorkObservation:
    return WorkObservation(
        signals=("python",),
        languages=("python",),
        baseline_capability_ids=baseline,
        active_capability_ids=active,
        rejected_capability_ids=rejected,
        requested_limit=requested_limit,
    )


def _active_state(
    candidate: CapabilityCandidate, *, source_digest: str | None = None
) -> EngineState:
    lease_id = "lease-active"
    return EngineState(
        revision=1,
        scope=ScopeRef(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            repository_id="repository-1",
            session_id="session-1",
            exposure_id="exposure-1",
            host_context_id="host-1",
        ),
        host_level="activating",
        host_descriptor_digest=_digest("host"),
        capabilities=(
            CapabilityState(
                capability_id=candidate.capability_id,
                source_digest=source_digest or candidate.source_digest,
                plan_id="plan-active",
                catalog_snapshot_id=_digest("catalog-active"),
                kind=candidate.kind,
                actionability=candidate.actionability,
                install_descriptor_digest=candidate.install_descriptor_digest,
                install_plan_digest=candidate.install_plan_digest,
                leases=(
                    LeaseRef(
                        lease_id=lease_id,
                        owner_id="owner-1",
                        exposure_id="exposure-1",
                    ),
                ),
                activation="active",
                activation_lease_id=lease_id,
            ),
        ),
        _contract_version=2,
    )


def _replay_plan(
    candidates: tuple[CapabilityCandidate, ...],
    state: EngineState,
    *,
    schema_version: int,
) -> StructuredSurrogate:
    source = StaticSource(candidates)
    observation = StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": ["python"],
            "languages": ["python"],
            "baseline_capability_ids": [],
            "active_capability_ids": [],
            "rejected_capability_ids": [],
            "requested_limit": 5,
        },
    )
    return ReplayDecisionPlanner(
        BoundedCapabilityPlanner(source),
        planner_version="planner-v2",
        decision_schema_version=schema_version,
    )(
        observation,
        state,
        PlanningContext(
            planner_version="planner-v2",
            catalog_snapshot_digest=source.catalog_snapshot_digest,
        ),
    )


def _capability_rows(decision: StructuredSurrogate) -> tuple[Mapping[str, object], ...]:
    raw_capabilities = decision.value["capabilities"]
    assert isinstance(raw_capabilities, tuple)
    rows: list[Mapping[str, object]] = []
    for row in raw_capabilities:
        assert isinstance(row, Mapping)
        rows.append(row)
    return tuple(rows)


def test_mixed_candidate_pool_has_one_global_five_item_budget() -> None:
    candidates = (
        _candidate("skill:python-tdd", 950_000),
        _candidate("agent:python-reviewer", 900_000),
        _candidate("mcp-server:python-docs", 850_000),
        _candidate("harness:python-runner", 800_000),
        _candidate(
            "skill:python-security",
            750_000,
            matching_signals=("python", "security"),
        ),
        _candidate(
            "agent:python-debugger",
            700_000,
            matching_signals=("debugging", "python"),
        ),
    )

    plan = BoundedCapabilityPlanner(StaticSource(candidates)).plan(_observation())

    assert plan.status == "ready"
    assert plan.abstention_code is None
    assert [item.capability_id for item in plan.selections] == [
        "skill:python-tdd",
        "agent:python-reviewer",
        "mcp-server:python-docs",
        "harness:python-runner",
        "skill:python-security",
    ]
    assert {item.kind for item in plan.selections} == {
        "skill",
        "agent",
        "mcp-server",
        "harness",
    }
    assert len(plan.selections) == 5


def test_selection_is_invariant_to_source_permutation() -> None:
    candidates = (
        _candidate("skill:python-tdd", 900_000),
        _candidate("agent:python-reviewer", 900_000),
        _candidate("mcp-server:python-docs", 800_000),
        _candidate("harness:python-runner", 700_000),
        _candidate(
            "skill:python-security",
            600_000,
            matching_signals=("python", "security"),
        ),
        _candidate(
            "agent:python-debugger",
            500_000,
            matching_signals=("debugging", "python"),
        ),
    )
    expected = (
        "agent:python-reviewer",
        "skill:python-tdd",
        "mcp-server:python-docs",
        "harness:python-runner",
        "skill:python-security",
    )

    for candidate_order in permutations(candidates):
        plan = BoundedCapabilityPlanner(StaticSource(candidate_order)).plan(_observation())
        assert tuple(item.capability_id for item in plan.selections) == expected


def test_duplicates_ambiguity_and_equivalence_are_resolved_before_selection() -> None:
    duplicate_high = _candidate("skill:python-tdd", 900_000)
    duplicate_low = _candidate("skill:python-tdd", 800_000)
    ambiguous_a = _candidate("agent:ambiguous", 990_000, digest_salt="one")
    ambiguous_b = _candidate("agent:ambiguous", 980_000, digest_salt="two")
    equivalent_high = _candidate(
        "mcp-server:python-docs",
        850_000,
        equivalence_key="python-docs-provider",
    )
    equivalent_low = _candidate(
        "skill:python-docs",
        700_000,
        equivalence_key="python-docs-provider",
    )

    plan = BoundedCapabilityPlanner(
        FlexibleSource(
            (
                ambiguous_a,
                duplicate_low,
                equivalent_low,
                ambiguous_b,
                equivalent_high,
                duplicate_high,
            )
        )
    ).plan(_observation())

    assert [item.capability_id for item in plan.selections] == [
        "skill:python-tdd",
        "mcp-server:python-docs",
    ]
    assert plan.selections[0].normalized_score_ppm == 900_000
    assert "agent:ambiguous" not in {item.capability_id for item in plan.selections}


def test_minimum_matching_signals_filters_weak_candidates_before_ranking() -> None:
    plan = BoundedCapabilityPlanner(
        FlexibleSource(
            (
                _candidate("skill:python-only", 990_000),
                _candidate(
                    "agent:python-reviewer",
                    800_000,
                    matching_signals=("python", "review"),
                ),
            )
        ),
        minimum_matching_signals=2,
    ).plan(_observation())

    assert [selection.capability_id for selection in plan.selections] == ["agent:python-reviewer"]


def test_minimum_matching_signals_abstains_when_no_candidate_has_enough_evidence() -> None:
    plan = BoundedCapabilityPlanner(
        StaticSource((_candidate("skill:python-only", 990_000),)),
        minimum_matching_signals=2,
    ).plan(_observation())

    assert plan == CapabilityPlan(
        status="abstained",
        abstention_code="no-relevant-capability",
    )


def test_minimum_non_language_matches_rejects_language_plus_one_weak_signal() -> None:
    observation = WorkObservation(
        signals=("ascii", "json", "serialization"),
        languages=("python",),
    )
    plan = BoundedCapabilityPlanner(
        FlexibleSource(
            (
                _candidate(
                    "skill:python-ascii-logo",
                    990_000,
                    matching_signals=("ascii", "python"),
                ),
                _candidate(
                    "skill:json-serialization",
                    800_000,
                    matching_signals=("json", "serialization"),
                ),
            )
        ),
        minimum_matching_signals=2,
        minimum_non_language_matching_signals=2,
    ).plan(observation)

    assert [selection.capability_id for selection in plan.selections] == [
        "skill:json-serialization"
    ]


def test_minimum_non_language_matches_abstains_before_score_threshold() -> None:
    observation = WorkObservation(signals=("response",), languages=("python",))
    plan = BoundedCapabilityPlanner(
        FlexibleSource(
            (
                _candidate(
                    "skill:python-response",
                    1_000_000,
                    matching_signals=("python", "response"),
                ),
            )
        ),
        minimum_non_language_matching_signals=2,
    ).plan(observation)

    assert plan == CapabilityPlan(
        status="abstained",
        abstention_code="no-relevant-capability",
    )


def test_allowed_actionability_policy_can_require_preinstalled_capabilities() -> None:
    plan = BoundedCapabilityPlanner(
        StaticSource(
            (
                _candidate("skill:remote", 990_000, actionability="install"),
                _candidate("skill:manual", 980_000, actionability="manual"),
                _candidate("skill:local", 800_000, actionability="load"),
            )
        ),
        allowed_actionability_states=frozenset({"load"}),
    ).plan(_observation())

    assert [selection.capability_id for selection in plan.selections] == ["skill:local"]


def test_same_kind_same_nonempty_coverage_collapses_to_strongest_candidate() -> None:
    plan = BoundedCapabilityPlanner(
        StaticSource(
            (
                _candidate("skill:python-tdd", 900_000),
                _candidate("skill:python-testing", 800_000),
            )
        ),
        minimum_matching_signals=1,
    ).plan(_observation())

    assert [selection.capability_id for selection in plan.selections] == ["skill:python-tdd"]


def test_different_kind_or_different_coverage_is_retained() -> None:
    plan = BoundedCapabilityPlanner(
        StaticSource(
            (
                _candidate("skill:python-tdd", 900_000),
                _candidate("agent:python-reviewer", 850_000),
                _candidate(
                    "skill:python-security",
                    800_000,
                    matching_signals=("python", "security"),
                ),
            )
        ),
        minimum_matching_signals=1,
    ).plan(_observation())

    assert [selection.capability_id for selection in plan.selections] == [
        "skill:python-tdd",
        "agent:python-reviewer",
        "skill:python-security",
    ]


def test_explicit_distinct_equivalence_roles_override_coverage_collapse() -> None:
    plan = BoundedCapabilityPlanner(
        StaticSource(
            (
                _candidate(
                    "skill:python-tdd",
                    900_000,
                    equivalence_key="implementation-role",
                ),
                _candidate(
                    "skill:python-review",
                    850_000,
                    equivalence_key="review-role",
                ),
            )
        ),
        minimum_matching_signals=1,
    ).plan(_observation())

    assert [selection.capability_id for selection in plan.selections] == [
        "skill:python-tdd",
        "skill:python-review",
    ]


def test_context_non_actionable_and_low_score_candidates_are_excluded() -> None:
    candidates = (
        _candidate("skill:baseline", 990_000),
        _candidate("agent:active", 980_000),
        _candidate("mcp-server:rejected", 970_000),
        _candidate("harness:blocked", 960_000, actionability="blocked"),
        _candidate("skill:low", 299_999),
        _candidate("agent:eligible", 800_000, actionability="manual"),
    )

    plan = BoundedCapabilityPlanner(StaticSource(candidates)).plan(
        _observation(
            baseline=("skill:baseline",),
            active=("agent:active",),
            rejected=("mcp-server:rejected",),
        )
    )

    assert [item.capability_id for item in plan.selections] == ["agent:eligible"]


def test_schema_v2_retains_relevant_active_inside_the_same_global_five_item_plan() -> None:
    active = _candidate("skill:active", 550_000)
    candidates = (
        active,
        _candidate("agent:new-one", 900_000),
        _candidate("agent:new-two", 800_000),
        _candidate("agent:new-three", 700_000),
        _candidate("agent:new-four", 600_000),
        _candidate("agent:new-five", 500_000),
    )
    state = _active_state(active)

    legacy = _replay_plan(candidates, state, schema_version=1)
    desired = _replay_plan(candidates, state, schema_version=2)

    legacy_ids = tuple(item["capability_id"] for item in _capability_rows(legacy))
    desired_ids = tuple(item["capability_id"] for item in _capability_rows(desired))
    assert legacy_ids == (
        "agent:new-one",
        "agent:new-two",
        "agent:new-three",
        "agent:new-four",
        "agent:new-five",
    )
    assert desired_ids == (
        "agent:new-one",
        "agent:new-two",
        "agent:new-three",
        "agent:new-four",
        "skill:active",
    )
    assert len(desired_ids) == 5
    assert len(set(desired_ids)) == 5


def test_schema_v2_drops_irrelevant_or_identity_mismatched_active_retention() -> None:
    relevant = _candidate("skill:active", 900_000)
    new = tuple(_candidate(f"agent:new-{index}", 800_000 - index) for index in range(1, 6))

    mismatched = _replay_plan(
        (relevant, *new),
        _active_state(relevant, source_digest=_digest("changed-active-source")),
        schema_version=2,
    )
    irrelevant = _candidate("skill:active", 299_999)
    below_threshold = _replay_plan(
        (irrelevant, *new),
        _active_state(irrelevant),
        schema_version=2,
    )
    lower_ranked = _candidate("skill:active", 400_000)
    displaced = _replay_plan(
        (lower_ranked, *new),
        _active_state(lower_ranked),
        schema_version=2,
    )

    mismatched_ids = tuple(item["capability_id"] for item in _capability_rows(mismatched))
    irrelevant_ids = tuple(item["capability_id"] for item in _capability_rows(below_threshold))
    displaced_ids = tuple(item["capability_id"] for item in _capability_rows(displaced))
    assert "skill:active" not in mismatched_ids
    assert "skill:active" not in irrelevant_ids
    assert "skill:active" not in displaced_ids
    assert len(mismatched_ids) == 4
    assert len(irrelevant_ids) == 5
    assert len(displaced_ids) == 5


@pytest.mark.parametrize(
    ("observation", "candidates", "code"),
    [
        (
            WorkObservation(requested_limit=5),
            (_candidate("skill:unused", 900_000),),
            "no-signals",
        ),
        (
            _observation(requested_limit=0),
            (_candidate("skill:unused", 900_000),),
            "no-relevant-capability",
        ),
        (_observation(), (), "no-relevant-capability"),
        (_observation(), (_candidate("skill:low", 299_999),), "below-threshold"),
        (
            _observation(baseline=("skill:excluded",)),
            (_candidate("skill:excluded", 900_000),),
            "no-relevant-capability",
        ),
    ],
)
def test_abstention_is_typed(
    observation: WorkObservation,
    candidates: Sequence[CapabilityCandidate],
    code: str,
) -> None:
    plan = BoundedCapabilityPlanner(StaticSource(candidates)).plan(observation)

    assert plan == CapabilityPlan(status="abstained", abstention_code=code)
    assert plan.selections == ()


def test_source_failures_degrade_without_leaking_exception_text() -> None:
    class UnavailableSource:
        catalog_snapshot_digest = _digest("catalog")

        def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]:
            raise CandidateSourceUnavailable("/private/catalog: secret")

    class BrokenSource:
        catalog_snapshot_digest = _digest("catalog")

        def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]:
            raise RuntimeError("raw prompt and secret")

    unavailable = BoundedCapabilityPlanner(UnavailableSource()).plan(_observation())
    broken = BoundedCapabilityPlanner(BrokenSource()).plan(_observation())

    assert unavailable == CapabilityPlan(
        status="degraded",
        abstention_code="catalog-unavailable",
    )
    assert broken == CapabilityPlan(status="degraded", abstention_code="planner-failed")
    assert "secret" not in str(unavailable.to_mapping())
    assert "raw prompt" not in str(broken.to_mapping())


def test_plan_mapping_has_only_the_approved_structure_and_no_raw_prose() -> None:
    plan = BoundedCapabilityPlanner(StaticSource((_candidate("skill:python-tdd", 900_000),))).plan(
        _observation()
    )

    mapping = plan.to_mapping()

    assert set(mapping) == {"status", "abstention_code", "capabilities"}
    assert mapping == {
        "status": "ready",
        "abstention_code": None,
        "capabilities": [
            {
                "actionability": "load",
                "capability_id": "skill:python-tdd",
                "kind": "skill",
                "matching_signals": ["python"],
                "name": "python-tdd",
                "normalized_score_ppm": 900_000,
                "reason_codes": ["signal-match"],
                "catalog_entry_digest": _digest("skill:python-tdd"),
            }
        ],
    }


def test_plan_mapping_is_accepted_as_the_approved_replay_decision_surrogate() -> None:
    plan = BoundedCapabilityPlanner(StaticSource((_candidate("skill:python-tdd", 900_000),))).plan(
        _observation()
    )
    decision = StructuredSurrogate.create(
        schema_id="ctx.decision.capability-plan",
        schema_version=1,
        value=plan.to_mapping(),
    )
    event = EngineEvent(
        event_id="event-1",
        kind="SessionStarted",
        scope=ScopeRef(
            tenant_id="tenant-1",
            workspace_id="workspace-1",
            repository_id="repository-1",
            session_id="session-1",
            exposure_id="exposure-1",
            host_context_id="host-1",
        ),
        expected_revision=0,
        occurred_at="2026-08-01T12:00:00Z",
        payload={"host_level": "query-only"},
        host_descriptor_digest=_digest("host"),
    )
    factory = DefaultReplayInputFactory(reducer_version="ctx-reducer-v2")

    replay = factory.prepare(
        factory.preflight(event),
        None,
        decision_surrogate=decision,
    )

    assert replay.decision_surrogate == decision


def test_install_candidate_requires_exact_descriptor_and_plan_digests() -> None:
    with pytest.raises(PlannerValidationError, match="install_descriptor_digest"):
        CapabilityCandidate(
            capability_id="skill:remote",
            kind="skill",
            name="remote",
            source_digest=_digest("remote"),
            normalized_score_ppm=900_000,
            reason_codes=("signal-match",),
            actionability="install",
            install_plan_digest=_digest("install-plan"),
        )

    with pytest.raises(PlannerValidationError, match="install_plan_digest"):
        CapabilityCandidate(
            capability_id="skill:remote",
            kind="skill",
            name="remote",
            source_digest=_digest("remote"),
            normalized_score_ppm=900_000,
            reason_codes=("signal-match",),
            actionability="install",
            install_descriptor_digest=_digest("install-descriptor"),
        )

    with pytest.raises(PlannerValidationError, match="only for install"):
        _candidate(
            "skill:local",
            900_000,
            install_descriptor_digest=_digest("unexpected-descriptor"),
            install_plan_digest=_digest("unexpected-plan"),
        )


def test_capability_plan_v1_rejects_install_and_v2_binds_exact_install_identity() -> None:
    install = _candidate("mcp-server:python-docs", 900_000, actionability="install")
    plan = BoundedCapabilityPlanner(StaticSource((install,))).plan(_observation())

    with pytest.raises(PlannerValidationError, match="schema v1"):
        plan.to_mapping()

    mapping = plan.to_mapping(schema_version=2)
    assert mapping["capabilities"] == [
        {
            "actionability": "install",
            "capability_id": "mcp-server:python-docs",
            "catalog_entry_digest": install.source_digest,
            "install_descriptor_digest": install.install_descriptor_digest,
            "install_plan_digest": install.install_plan_digest,
            "kind": "mcp-server",
            "matching_signals": ["python"],
            "name": "python-docs",
            "normalized_score_ppm": 900_000,
            "reason_codes": ["signal-match"],
        }
    ]


def test_capability_plan_v2_distinguishes_same_plan_with_different_descriptors() -> None:
    plan_digest = _digest("shared-plan")
    first = _candidate(
        "skill:remote",
        900_000,
        actionability="install",
        install_descriptor_digest=_digest("descriptor-one"),
        install_plan_digest=plan_digest,
    )
    second = _candidate(
        "skill:remote",
        900_000,
        actionability="install",
        install_descriptor_digest=_digest("descriptor-two"),
        install_plan_digest=plan_digest,
    )

    first_mapping = CapabilityPlan(
        status="ready",
        abstention_code=None,
        selections=(CapabilitySelection.from_candidate(first),),
    ).to_mapping(schema_version=2)
    second_mapping = CapabilityPlan(
        status="ready",
        abstention_code=None,
        selections=(CapabilitySelection.from_candidate(second),),
    ).to_mapping(schema_version=2)

    assert first_mapping != second_mapping
    assert (
        first_mapping["capabilities"][0]["install_plan_digest"]
        == second_mapping["capabilities"][0]["install_plan_digest"]
    )
    assert (
        first_mapping["capabilities"][0]["install_descriptor_digest"]
        != second_mapping["capabilities"][0]["install_descriptor_digest"]
    )


def test_replay_decision_planner_requires_explicit_v2_for_install_selection() -> None:
    source = StaticSource((_candidate("agent:python-reviewer", 900_000, actionability="install"),))
    observation = StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "signals": ["python"],
            "languages": ["python"],
            "baseline_capability_ids": [],
            "active_capability_ids": [],
            "rejected_capability_ids": [],
            "requested_limit": 5,
        },
    )
    context = PlanningContext(
        planner_version="planner-v2",
        catalog_snapshot_digest=source.catalog_snapshot_digest,
    )

    v1 = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(source),
        planner_version="planner-v2",
    )
    with pytest.raises(PlannerValidationError, match="schema v1"):
        v1(observation, None, context)

    v2 = ReplayDecisionPlanner(
        BoundedCapabilityPlanner(source),
        planner_version="planner-v2",
        decision_schema_version=2,
    )(observation, None, context)
    assert v2.schema_version == 2
    capabilities = _capability_rows(v2)
    assert (
        capabilities[0]["install_descriptor_digest"] == source.values[0].install_descriptor_digest
    )
    assert capabilities[0]["install_plan_digest"] == source.values[0].install_plan_digest


@pytest.mark.parametrize(
    "factory",
    [
        lambda: WorkObservation(signals=("unsafe signal",), requested_limit=5),
        lambda: WorkObservation(signals=("python", "python"), requested_limit=5),
        lambda: WorkObservation(requested_limit=True),
        lambda: WorkObservation(requested_limit=6),
        lambda: CapabilityCandidate(
            capability_id="skill:Python-TDD",
            kind="skill",
            name="Python-TDD",
            source_digest=_digest("candidate"),
            normalized_score_ppm=900_000,
            reason_codes=("signal-match",),
            actionability="load",
        ),
        lambda: CapabilityCandidate(
            capability_id="skill:python-tdd",
            kind="agent",
            name="python-tdd",
            source_digest=_digest("candidate"),
            normalized_score_ppm=900_000,
            reason_codes=("signal-match",),
            actionability="load",
        ),
        lambda: CapabilityCandidate(
            capability_id="skill:python-tdd",
            kind="skill",
            name="python-tdd",
            source_digest="not-a-digest",
            normalized_score_ppm=900_000,
            reason_codes=("signal-match",),
            actionability="load",
        ),
    ],
)
def test_values_fail_closed_on_noncanonical_or_unsafe_input(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_source_output_fails_closed_instead_of_becoming_a_degraded_plan() -> None:
    class InvalidSource:
        catalog_snapshot_digest = _digest("catalog")

        def retrieve(self, observation: WorkObservation) -> Sequence[CapabilityCandidate]:
            return ["not-a-candidate"]  # type: ignore[list-item]

    with pytest.raises((TypeError, ValueError)):
        BoundedCapabilityPlanner(InvalidSource()).plan(_observation())
