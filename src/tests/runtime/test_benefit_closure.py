from __future__ import annotations

import hashlib
import json
import traceback
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast, overload

import pytest

from ctx.engine.benefit import NetBenefitPolicy
from ctx.engine.planner import CapabilityCandidate, PlannerValidationError, WorkObservation
from ctx.runtime.benefit_closure import (
    BenefitClosureError,
    EligibleCatalogClaim,
    QueryCapabilityEligibility,
    QueryHostPolicyAuthority,
    ReviewedBenefitAuthorities,
    ReviewedBenefitProfiles,
    catalog_candidate_entry_claim_digest,
    eligible_catalog_claim_digest,
    load_reviewed_benefit_profiles,
    load_reviewed_benefit_profiles_bytes,
    prepare_query_benefit_closure,
)
from ctx.runtime.planning_v3 import normalize_candidate_pool


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


CALIBRATION_DIGEST = _digest("reviewed-calibration")
CATALOG_SNAPSHOT_DIGEST = _digest("eligible-catalog-snapshot")
ELIGIBILITY_DIGEST = _digest("eligible-catalog-index")
HOST_POLICY_DIGEST = _digest("query-host-policy")
POLICY = NetBenefitPolicy(calibration_digest=CALIBRATION_DIGEST, minimum_relevance_ppm=1)


def _profile_record(
    *,
    capability_id: str = "skill:ctx-python-testing",
    actionability: str = "manual",
    entry_claim_digest: str | None = None,
    minimum_matching_signals: int = 2,
    minimum_non_language_matching_signals: int = 1,
    allowed_signals: list[str] | None = None,
) -> dict[str, object]:
    kind, name = capability_id.split(":", 1)
    return {
        "actionability": actionability,
        "capability_id": capability_id,
        "catalog_entry_claim_digest": entry_claim_digest
        or catalog_candidate_entry_claim_digest(
            _candidate(capability_id=capability_id, actionability=actionability)
        ),
        "complements": [],
        "conflicts": [],
        "costs": {
            "approval_prompts": 0,
            "child_agent_units": 0,
            "context_tokens": 180,
            "credential_burden_units": 0,
            "permission_burden_units": 0,
            "process_units": 0,
            "runtime_millis": 0,
            "tool_schema_tokens": 0,
        },
        "coverage_keys": [f"coverage-{name}"],
        "expected_task_benefit_ppm": 500_000,
        "kind": kind,
        "match_policy": {
            "allowed_equivalence_keys": [],
            "allowed_reason_codes": ["graph-match", "signal-match"],
            "allowed_signals": allowed_signals or ["python", "testing"],
            "minimum_matching_signals": minimum_matching_signals,
            "minimum_non_language_matching_signals": minimum_non_language_matching_signals,
            "required_any_signals": ["testing"],
        },
        "maximum_relevance_ppm": 700_000,
        "name": name,
        "profile_id": f"profile-{name}",
        "review_basis_digest": _digest(f"review-basis:{capability_id}"),
        "security_approved": True,
        "source_trusted": True,
        "trust_ppm": 900_000,
    }


def _manifest(
    *profiles: dict[str, object],
    authority_id: str = "ctx-release",
    authority_kind: str = "ctx-release",
    catalog_layer_kind: str = "ctx",
    sequence: int = 1,
) -> dict[str, object]:
    ordered = sorted(
        profiles or (_profile_record(),),
        key=lambda value: (
            str(value["capability_id"]),
            str(value["actionability"]),
            str(value["profile_id"]),
        ),
    )
    return {
        "authority": {
            "authority_digest": _digest(f"benefit-authority:{authority_id}:{sequence}"),
            "authority_id": authority_id,
            "authority_kind": authority_kind,
            "catalog_layer_kind": catalog_layer_kind,
            "sequence": sequence,
        },
        "bindings": {
            "calibration_digest": CALIBRATION_DIGEST,
            "candidate_projection_version": "ctx.catalog-entry.graph-v4",
            "catalog_artifact_sha256": _digest(f"eligible-catalog-artifact:{authority_id}"),
            "catalog_namespace_digest": _digest(f"catalog-namespace:{authority_id}"),
            "catalog_provenance_digest": _digest(f"catalog-provenance:{authority_id}"),
            "catalog_retrieval_snapshot_digest": _digest(f"catalog-retrieval:{authority_id}"),
            "installation_snapshot_digest": None,
            "material_snapshot_digest": None,
            "policy_digest": POLICY.policy_digest,
        },
        "profiles": ordered,
        "schema": "ctx.reviewed-benefit-profiles-v2",
    }


def _authority(
    *records: dict[str, object],
    authority_id: str = "ctx-release",
    authority_kind: str = "ctx-release",
    catalog_layer_kind: str = "ctx",
    sequence: int = 1,
) -> ReviewedBenefitProfiles:
    body = _canonical_bytes(
        _manifest(
            *records,
            authority_id=authority_id,
            authority_kind=authority_kind,
            catalog_layer_kind=catalog_layer_kind,
            sequence=sequence,
        )
    )
    return load_reviewed_benefit_profiles_bytes(body, hashlib.sha256(body).hexdigest())


def _authorities(*values: ReviewedBenefitProfiles) -> ReviewedBenefitAuthorities:
    return ReviewedBenefitAuthorities.create(values or (_authority(),))


def _candidate(
    *,
    capability_id: str = "skill:ctx-python-testing",
    matching_signals: tuple[str, ...] = ("python", "testing"),
    actionability: str = "manual",
    reason_codes: tuple[str, ...] = ("graph-match", "signal-match"),
) -> CapabilityCandidate:
    kind, name = capability_id.split(":", 1)
    return CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=_digest(f"source:{capability_id}:{actionability}"),
        normalized_score_ppm=700_000,
        matching_signals=matching_signals,
        reason_codes=reason_codes,
        actionability=actionability,
        install_descriptor_digest=(
            _digest(f"install-descriptor:{capability_id}") if actionability == "install" else None
        ),
        install_plan_digest=(
            _digest(f"install-plan:{capability_id}") if actionability == "install" else None
        ),
    )


def _observation(
    *,
    signals: tuple[str, ...] = ("testing",),
    languages: tuple[str, ...] = ("python",),
    baseline: tuple[str, ...] = (),
    rejected: tuple[str, ...] = (),
) -> WorkObservation:
    return WorkObservation(
        signals=signals,
        languages=languages,
        baseline_capability_ids=baseline,
        rejected_capability_ids=rejected,
        requested_limit=5,
    )


@dataclass
class _EligibleSource:
    candidates: object
    claims: dict[str, EligibleCatalogClaim]
    catalog_snapshot_digest: str
    catalog_retrieval_snapshot_digest: str
    catalog_namespace_digest: str
    catalog_provenance_digest: str
    catalog_artifact_sha256: str
    candidate_projection_version: str
    material_snapshot_digest: str | None
    installation_snapshot_digest: str | None
    profile_snapshot_digest: str
    authority_snapshot_digest: str
    eligibility_snapshot_digest: str
    calibration_digest: str
    policy_digest: str
    close_calls: int = 0
    cleanup_failure: bool = False
    retrieve_calls: int = 0
    claim_calls: int = 0
    candidate_rounds: tuple[object, ...] = ()
    claim_rounds: dict[str, tuple[EligibleCatalogClaim, ...]] = field(default_factory=dict)
    drift_binding_on_retrieve: bool = False
    drift_binding_on_claim: bool = False
    explode_at: str | None = None
    leak_cleanup_class: bool = False

    def retrieve(self, _observation: WorkObservation) -> Sequence[CapabilityCandidate]:
        if self.explode_at == "retrieve":
            raise RuntimeError("/Users/alice/private-repo sk-live-secret")
        self.retrieve_calls += 1
        value = (
            self.candidate_rounds[min(self.retrieve_calls - 1, len(self.candidate_rounds) - 1)]
            if self.candidate_rounds
            else self.candidates
        )
        if self.drift_binding_on_retrieve:
            self.catalog_provenance_digest = _digest("drifted-during-retrieve")
        return cast(Sequence[CapabilityCandidate], value)

    def entry_claim(self, presentation: CapabilityCandidate) -> EligibleCatalogClaim:
        if self.explode_at == "claim":
            raise RuntimeError("/Users/alice/private-repo sk-live-secret")
        self.claim_calls += 1
        rounds = self.claim_rounds.get(presentation.capability_id, ())
        value = (
            rounds[min(self.claim_calls - 1, len(rounds) - 1)]
            if rounds
            else self.claims[presentation.capability_id]
        )
        if self.drift_binding_on_claim:
            self.catalog_provenance_digest = _digest("drifted-during-claim")
        return value

    def close(self) -> None:
        self.close_calls += 1
        if self.leak_cleanup_class:
            raise Alice_private_repo_sk_live_secret
        if self.explode_at == "close":
            raise RuntimeError("/Users/alice/private-repo sk-live-secret")
        if self.cleanup_failure:
            raise RuntimeError("cleanup sentinel")


class Alice_private_repo_sk_live_secret(RuntimeError):
    pass


class _ExplodingAttributeSource:
    def __init__(self, source: _EligibleSource, attribute: str) -> None:
        self._source = source
        self._attribute = attribute

    def __getattr__(self, name: str) -> object:
        if name == self._attribute:
            raise RuntimeError("/Users/alice/private-repo sk-live-secret")
        return getattr(self._source, name)


def _source(
    profiles: ReviewedBenefitAuthorities,
    *candidates: CapabilityCandidate,
    claim_authorities: dict[str, ReviewedBenefitProfiles] | None = None,
) -> _EligibleSource:
    values = candidates or (_candidate(),)
    authority_by_id = claim_authorities or {
        candidate.capability_id: profiles.authorities[0] for candidate in values
    }
    claims = {
        candidate.capability_id: EligibleCatalogClaim.create(
            authority_by_id[candidate.capability_id],
            presentation=candidate,
        )
        for candidate in values
    }
    return _EligibleSource(
        candidates=values,
        claims=claims,
        catalog_snapshot_digest=CATALOG_SNAPSHOT_DIGEST,
        catalog_retrieval_snapshot_digest=profiles.catalog_retrieval_snapshot_digest,
        catalog_namespace_digest=profiles.catalog_namespace_digest,
        catalog_provenance_digest=profiles.catalog_provenance_digest,
        catalog_artifact_sha256=profiles.catalog_artifact_sha256,
        candidate_projection_version=profiles.candidate_projection_version,
        material_snapshot_digest=profiles.material_snapshot_digest,
        installation_snapshot_digest=profiles.installation_snapshot_digest,
        profile_snapshot_digest=profiles.profile_snapshot_digest,
        authority_snapshot_digest=profiles.authority_snapshot_digest,
        eligibility_snapshot_digest=ELIGIBILITY_DIGEST,
        calibration_digest=profiles.calibration_digest,
        policy_digest=profiles.policy_digest,
    )


@dataclass
class _HostPolicy:
    host_policy_snapshot_digest: str = HOST_POLICY_DIGEST
    available: bool = True
    permissions_allowed: bool = True
    credentials_available: bool = True
    return_none: bool = False
    drift: bool = False
    claim_digest_override: str | None = None
    explode: bool = False
    calls: int = 0

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility | None:
        if self.explode:
            raise RuntimeError("/Users/alice/private-repo sk-live-secret")
        self.calls += 1
        if self.return_none:
            return None
        result = QueryCapabilityEligibility(
            presentation_digest=_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=(
                self.claim_digest_override or eligible_catalog_claim_digest(claim)
            ),
            available=self.available,
            permissions_allowed=self.permissions_allowed,
            credentials_available=self.credentials_available,
        )
        if self.drift:
            self.host_policy_snapshot_digest = _digest("drifted-host-policy")
        return result


def _presentation_digest(candidate: CapabilityCandidate) -> str:
    from ctx.runtime.authenticated_benefit import capability_presentation_digest

    return capability_presentation_digest(candidate)


def _prepare(
    profiles: ReviewedBenefitAuthorities,
    source: _EligibleSource,
    *,
    observation: WorkObservation | object | None = None,
    host_policy: _HostPolicy | None = None,
):  # type: ignore[no-untyped-def]
    return prepare_query_benefit_closure(
        source=source,
        observation=observation if observation is not None else _observation(),  # type: ignore[arg-type]
        profiles=profiles,
        policy=POLICY,
        host_policy=host_policy or _HostPolicy(),
    )


def test_reviewed_profiles_load_from_exact_file_and_frozen_bytes(tmp_path: Path) -> None:
    body = _canonical_bytes(_manifest())
    expected_sha256 = hashlib.sha256(body).hexdigest()
    path = tmp_path / "reviewed-benefit-profiles.json"
    path.write_bytes(body)
    path.chmod(0o444)

    from_path = load_reviewed_benefit_profiles(path, expected_sha256)
    from_bytes = load_reviewed_benefit_profiles_bytes(body, expected_sha256)

    assert from_path == from_bytes
    assert from_path.profile_snapshot_digest == expected_sha256
    assert from_path.policy_digest == POLICY.policy_digest
    assert len(from_path.profiles) == 1


@pytest.mark.parametrize(
    "mutation",
    ["unknown-root", "wrong-layer", "duplicate-profile", "unreviewed-signal"],
)
def test_reviewed_profiles_reject_non_closed_cross_authority_or_ambiguous_input(
    mutation: str,
) -> None:
    manifest = _manifest()
    if mutation == "unknown-root":
        manifest["description"] = "catalog prose is not profile authority"
    elif mutation == "wrong-layer":
        manifest["authority"]["catalog_layer_kind"] = "user"  # type: ignore[index]
    elif mutation == "duplicate-profile":
        manifest["profiles"] = [manifest["profiles"][0], manifest["profiles"][0]]  # type: ignore[index]
    else:
        manifest["profiles"][0]["match_policy"]["required_any_signals"] = ["secret"]  # type: ignore[index]
    body = _canonical_bytes(manifest)

    with pytest.raises(BenefitClosureError):
        load_reviewed_benefit_profiles_bytes(body, hashlib.sha256(body).hexdigest())


def test_query_closure_freezes_exact_presentations_authorities_policy_and_facts() -> None:
    profiles = _authorities()
    observation = _observation()
    raw_source = _source(profiles)

    closure = _prepare(profiles, raw_source, observation=observation)

    assert raw_source.close_calls == 1
    assert raw_source.retrieve_calls == 2
    assert closure.source.retrieve(observation) == (_candidate(),)
    facts = closure.benefit_facts.benefit_candidate(_candidate(), observation)
    assert facts is not None
    assert facts.expected_task_benefit_ppm == 500_000
    assert facts.relevance_ppm == 700_000
    assert facts.costs is not None and facts.costs.context_tokens == 180
    assert closure.authority_snapshot_digest == profiles.authority_snapshot_digest
    assert closure.policy is POLICY
    assert closure.policy_digest == POLICY.policy_digest
    assert closure.upstream_host_policy_snapshot_digest == HOST_POLICY_DIGEST
    assert (
        closure.host_policy_snapshot_digest
        == closure.host_policy_authority.host_policy_snapshot_digest
    )
    assert closure.closure_snapshot_digest == closure.source.catalog_snapshot_digest

    with pytest.raises(PlannerValidationError, match="observation mismatch"):
        closure.source.retrieve(WorkObservation(signals=("security",), languages=("python",)))
    closure.source.close()
    with pytest.raises(PlannerValidationError, match="closed"):
        closure.source.retrieve(observation)


def test_query_closure_value_recomputes_its_public_pins() -> None:
    profiles = _authorities()
    closure = _prepare(profiles, _source(profiles))

    with pytest.raises(TypeError, match="closure factory"):
        replace(closure, policy_digest=_digest("substituted-policy"))


def test_reviewed_values_recompute_profile_and_manifest_membership() -> None:
    authority = _authority()

    with pytest.raises(BenefitClosureError, match="profile_digest"):
        replace(authority.profiles[0], source_trusted=False)
    with pytest.raises(BenefitClosureError, match="profile_snapshot_digest"):
        replace(authority, sequence=2)


def test_query_closure_rejects_forged_claim_outside_advertised_authorities() -> None:
    profiles = _authorities()
    closure = _prepare(profiles, _source(profiles))
    candidate = closure.source.retrieve(_observation())[0]
    user_authority = _authority(
        authority_id="local-user",
        authority_kind="user",
        catalog_layer_kind="user",
    )
    forged_claim = EligibleCatalogClaim.create(user_authority, presentation=candidate)
    forged_host_fact = replace(
        closure.host_eligibilities[0],
        catalog_entry_claim_digest=forged_claim.catalog_entry_claim_digest,
        catalog_claim_digest=eligible_catalog_claim_digest(forged_claim),
    )

    with pytest.raises(TypeError, match="closure factory"):
        replace(
            closure,
            catalog_claims=(forged_claim,),
            host_eligibilities=(forged_host_fact,),
        )


@pytest.mark.parametrize(
    "field_name",
    [
        "catalog_entry_claim_digest",
        "presentation_digest",
    ],
)
def test_query_closure_rechecks_claim_on_reconstruction(field_name: str) -> None:
    profiles = _authorities()
    closure = _prepare(profiles, _source(profiles))
    original_claim = closure.catalog_claims[0]
    forged_claim = replace(original_claim, **{field_name: _digest(f"other-{field_name}")})
    forged_host_fact = replace(
        closure.host_eligibilities[0],
        catalog_claim_digest=eligible_catalog_claim_digest(forged_claim),
    )

    with pytest.raises(TypeError, match="closure factory"):
        replace(
            closure,
            catalog_claims=(forged_claim,),
            host_eligibilities=(forged_host_fact,),
        )


def test_query_closure_supports_independent_ctx_and_user_authorities() -> None:
    ctx_authority = _authority()
    user_candidate = _candidate(capability_id="agent:user-reviewer")
    user_claim = catalog_candidate_entry_claim_digest(user_candidate)
    user_authority = _authority(
        _profile_record(
            capability_id=user_candidate.capability_id,
            entry_claim_digest=user_claim,
        ),
        authority_id="local-user",
        authority_kind="user",
        catalog_layer_kind="user",
    )
    profiles = _authorities(ctx_authority, user_authority)
    source = _source(
        profiles,
        _candidate(),
        user_candidate,
        claim_authorities={
            "skill:ctx-python-testing": ctx_authority,
            user_candidate.capability_id: user_authority,
        },
    )

    closure = _prepare(profiles, source)

    assert {item.capability_id for item in closure.source.retrieve(_observation())} == {
        "skill:ctx-python-testing",
        "agent:user-reviewer",
    }


def test_host_policy_cannot_reuse_same_entry_digest_across_authorities() -> None:
    candidate = _candidate()
    ctx_authority = _authority()
    user_authority = _authority(
        authority_id="local-user",
        authority_kind="user",
        catalog_layer_kind="user",
    )
    profiles = _authorities(ctx_authority, user_authority)
    source = _source(
        profiles,
        candidate,
        claim_authorities={candidate.capability_id: ctx_authority},
    )
    user_claim = EligibleCatalogClaim.create(user_authority, presentation=candidate)

    with pytest.raises(BenefitClosureError, match="cross-authority"):
        _prepare(
            profiles,
            source,
            host_policy=_HostPolicy(
                claim_digest_override=eligible_catalog_claim_digest(user_claim)
            ),
        )


def test_query_closure_abstains_on_unobserved_or_unreviewed_presentation_tokens() -> None:
    profiles = _authorities()
    unobserved = _candidate(matching_signals=("testing",))
    source = _source(profiles, unobserved)
    observation = _observation(signals=("security",), languages=("python",))

    closure = _prepare(profiles, source, observation=observation)
    assert closure.source.retrieve(observation) == ()

    private_reason = _candidate(reason_codes=("graph-match", "sk-live-secret-token"))
    source = _source(profiles, private_reason)
    closure = _prepare(profiles, source)
    assert closure.source.retrieve(_observation()) == ()


def test_query_closure_is_deterministic_and_weak_matches_abstain() -> None:
    profiles = _authorities()
    observation = _observation()
    first = _prepare(
        profiles,
        _source(profiles, _candidate(matching_signals=("python",))),
        observation=observation,
    )
    second = _prepare(
        profiles,
        _source(profiles, _candidate(matching_signals=("python",))),
        observation=observation,
    )

    assert first.source.retrieve(observation) == ()
    assert first.benefit_facts.presentation_digests == ()
    assert first.closure_snapshot_digest == second.closure_snapshot_digest


@pytest.mark.parametrize("excluded", ["baseline", "rejected"])
def test_query_closure_applies_shared_eligibility_before_requiring_profiles(excluded: str) -> None:
    profiles = _authorities()
    unprofiled = _candidate(capability_id="agent:unprofiled-reviewer")
    observation = _observation(
        baseline=(unprofiled.capability_id,) if excluded == "baseline" else (),
        rejected=(unprofiled.capability_id,) if excluded == "rejected" else (),
    )
    source = _source(profiles, unprofiled)

    closure = _prepare(profiles, source, observation=observation)

    assert closure.source.retrieve(observation) == ()
    assert source.claim_calls == 0
    assert source.close_calls == 1


def test_query_closure_rejects_unreviewed_or_authority_substituted_claim() -> None:
    profiles = _authorities()
    unreviewed = _candidate(capability_id="agent:unreviewed")
    source = _source(profiles, unreviewed)

    with pytest.raises(BenefitClosureError, match="without an exact reviewed profile"):
        _prepare(profiles, source)
    assert source.close_calls == 1

    user_authority = _authority(
        authority_id="local-user",
        authority_kind="user",
        catalog_layer_kind="user",
    )
    substituted = _source(profiles)
    substituted.claims["skill:ctx-python-testing"] = EligibleCatalogClaim.create(
        user_authority,
        presentation=_candidate(),
    )
    with pytest.raises(BenefitClosureError, match="without an exact reviewed profile"):
        _prepare(profiles, substituted)


def test_reviewed_install_claim_rejects_source_and_plan_substitution() -> None:
    reviewed = _candidate(actionability="install")
    authority = _authority(_profile_record(actionability="install"))
    profiles = _authorities(authority)
    substituted = replace(
        reviewed,
        source_digest=_digest("substituted-source"),
        install_descriptor_digest=_digest("substituted-descriptor"),
        install_plan_digest=_digest("substituted-plan"),
    )
    source = _source(profiles, substituted)

    with pytest.raises(BenefitClosureError, match="without an exact reviewed profile"):
        _prepare(profiles, source)

    assert source.close_calls == 1


def test_query_closure_requires_the_exact_reviewed_policy() -> None:
    manifest = _manifest()
    manifest["bindings"]["policy_digest"] = _digest("different-reviewed-policy")  # type: ignore[index]
    body = _canonical_bytes(manifest)
    authority = load_reviewed_benefit_profiles_bytes(body, hashlib.sha256(body).hexdigest())
    profiles = _authorities(authority)
    source = _source(profiles)

    with pytest.raises(BenefitClosureError, match="policy does not match"):
        _prepare(profiles, source)

    assert source.close_calls == 1


@pytest.mark.parametrize("during", ["retrieve", "claim"])
def test_query_closure_rejects_binding_drift_during_source_reads(during: str) -> None:
    profiles = _authorities()
    source = _source(profiles)
    if during == "retrieve":
        source.drift_binding_on_retrieve = True
    else:
        source.drift_binding_on_claim = True

    with pytest.raises(BenefitClosureError, match="binding drift"):
        _prepare(profiles, source)
    assert source.close_calls == 1


def test_query_closure_rejects_same_digest_candidate_or_claim_drift() -> None:
    profiles = _authorities()
    candidate = _candidate()
    changed = _candidate(matching_signals=("python",))
    source = _source(profiles, candidate)
    source.candidate_rounds = ((candidate,), (changed,))
    with pytest.raises(
        BenefitClosureError,
        match="claim does not match exact presentation|changed candidates or claims",
    ):
        _prepare(profiles, source)

    source = _source(profiles, candidate)
    first_claim = source.claims[candidate.capability_id]
    second_claim = replace(first_claim, authority_digest=_digest("changed-claim-authority"))
    source.claim_rounds[candidate.capability_id] = (first_claim, second_claim)
    with pytest.raises(BenefitClosureError, match="changed candidates or claims"):
        _prepare(profiles, source)


def test_host_policy_is_query_scoped_exact_and_not_install_consent() -> None:
    profiles = _authorities()
    source = _source(profiles)
    denied = _HostPolicy(permissions_allowed=False, credentials_available=False)

    closure = _prepare(profiles, source, host_policy=denied)
    benefit = closure.benefit_facts.benefit_candidate(_candidate(), _observation())

    assert benefit is not None
    assert benefit.permissions_allowed is False
    assert benefit.credentials_available is False
    assert denied.calls == 2

    source = _source(profiles)
    unavailable = _prepare(
        profiles,
        source,
        host_policy=_HostPolicy(available=False),
    )
    assert unavailable.source.retrieve(_observation()) == ()

    source = _source(profiles)
    with pytest.raises(BenefitClosureError, match="no exact query eligibility"):
        _prepare(profiles, source, host_policy=_HostPolicy(return_none=True))

    source = _source(profiles)
    with pytest.raises(BenefitClosureError, match="drift"):
        _prepare(profiles, source, host_policy=_HostPolicy(drift=True))


def test_query_closure_rejects_reconstructed_allow_after_host_denial() -> None:
    profiles = _authorities()
    closure = _prepare(
        profiles,
        _source(profiles),
        host_policy=_HostPolicy(permissions_allowed=False, credentials_available=False),
    )
    denied = closure.host_eligibilities[0]
    forged_allow = replace(
        denied,
        permissions_allowed=True,
        credentials_available=True,
    )

    with pytest.raises(TypeError, match="closure factory"):
        replace(closure, host_eligibilities=(forged_allow,))


def test_query_closure_closes_owned_source_on_invalid_arguments() -> None:
    profiles = _authorities()
    source = _source(profiles)

    with pytest.raises(TypeError, match="observation"):
        _prepare(profiles, source, observation=object())

    assert source.close_calls == 1


def test_query_closure_preserves_primary_error_when_cleanup_also_fails() -> None:
    profiles = _authorities()
    source = _source(profiles, _candidate(capability_id="agent:unreviewed"))
    source.cleanup_failure = True

    with pytest.raises(BenefitClosureError, match="without an exact reviewed profile") as exc:
        _prepare(profiles, source)

    assert any("cleanup also failed" in note for note in (exc.value.__notes__ or []))


@pytest.mark.parametrize("boundary", ["retrieve", "claim", "host-policy", "close"])
def test_query_closure_redacts_injected_boundary_exception_messages(boundary: str) -> None:
    profiles = _authorities()
    source = _source(profiles)
    host_policy = _HostPolicy(explode=boundary == "host-policy")
    if boundary != "host-policy":
        source.explode_at = boundary

    with pytest.raises(BenefitClosureError) as exc:
        _prepare(profiles, source, host_policy=host_policy)

    assert "alice" not in str(exc.value)
    assert "sk-live-secret" not in str(exc.value)
    assert exc.value.__suppress_context__ is True


class _ExplodingSnapshotHostPolicy:
    @property
    def host_policy_snapshot_digest(self) -> str:
        raise BenefitClosureError("/Users/alice/private-repo sk-live-secret")

    def eligibility_for(
        self,
        _presentation: CapabilityCandidate,
        _claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility | None:
        raise AssertionError("snapshot access must fail before eligibility lookup")


class _ExplodingEligibilityAttributeHostPolicy:
    host_policy_snapshot_digest = HOST_POLICY_DIGEST

    def __getattribute__(self, name: str) -> object:
        if name == "eligibility_for":
            raise RuntimeError("/Users/alice/private-repo sk-live-secret")
        return super().__getattribute__(name)


def test_query_closure_redacts_host_policy_snapshot_property_errors() -> None:
    profiles = _authorities()
    source = _source(profiles)

    with pytest.raises(BenefitClosureError) as exc:
        prepare_query_benefit_closure(
            source=source,
            observation=_observation(),
            profiles=profiles,
            policy=POLICY,
            host_policy=_ExplodingSnapshotHostPolicy(),
        )

    assert "alice" not in str(exc.value)
    assert "sk-live-secret" not in str(exc.value)
    assert exc.value.__suppress_context__ is True


@pytest.mark.parametrize("attribute", ["retrieve", "entry_claim"])
def test_query_closure_redacts_source_callable_attribute_errors(attribute: str) -> None:
    profiles = _authorities()
    source = _source(profiles)

    with pytest.raises(BenefitClosureError) as exc:
        prepare_query_benefit_closure(
            source=_ExplodingAttributeSource(source, attribute),  # type: ignore[arg-type]
            observation=_observation(),
            profiles=profiles,
            policy=POLICY,
            host_policy=_HostPolicy(),
        )

    assert "alice" not in str(exc.value)
    assert "sk-live-secret" not in str(exc.value)
    assert exc.value.__suppress_context__ is True


def test_query_closure_redacts_host_policy_callable_attribute_errors() -> None:
    profiles = _authorities()

    with pytest.raises(BenefitClosureError) as exc:
        prepare_query_benefit_closure(
            source=_source(profiles),
            observation=_observation(),
            profiles=profiles,
            policy=POLICY,
            host_policy=cast(
                QueryHostPolicyAuthority,
                _ExplodingEligibilityAttributeHostPolicy(),
            ),
        )

    assert "alice" not in str(exc.value)
    assert "sk-live-secret" not in str(exc.value)
    assert exc.value.__suppress_context__ is True


def test_query_closure_cleanup_note_does_not_expose_injected_exception_class() -> None:
    profiles = _authorities()
    source = _source(profiles, _candidate(capability_id="agent:unreviewed"))
    source.leak_cleanup_class = True

    with pytest.raises(BenefitClosureError) as exc:
        _prepare(profiles, source)

    notes = exc.value.__notes__ or []
    assert notes == ["CTX eligible-source cleanup also failed"]
    assert "Alice" not in " ".join(notes)


class _LyingSequence(Sequence[CapabilityCandidate]):
    def __init__(self, candidate: CapabilityCandidate, actual: int) -> None:
        self.candidate = candidate
        self.actual = actual

    def __len__(self) -> int:
        return 1

    @overload
    def __getitem__(self, index: int) -> CapabilityCandidate: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[CapabilityCandidate]: ...

    def __getitem__(
        self, index: int | slice
    ) -> CapabilityCandidate | Sequence[CapabilityCandidate]:
        if isinstance(index, slice):
            return ((self.candidate,) * self.actual)[index]
        if index < 0 or index >= self.actual:
            raise IndexError(index)
        return self.candidate

    def __iter__(self) -> Iterator[CapabilityCandidate]:
        return iter((self.candidate,) * self.actual)


def test_shared_candidate_bound_uses_realized_iteration_not_reported_length() -> None:
    with pytest.raises(PlannerValidationError, match="candidate pool limit"):
        normalize_candidate_pool(_LyingSequence(_candidate(), 513), _observation())


class _LyingAuthoritySequence(Sequence[ReviewedBenefitProfiles]):
    def __init__(self, authority: ReviewedBenefitProfiles, actual: int) -> None:
        self.authority = authority
        self.actual = actual

    def __len__(self) -> int:
        return 1

    @overload
    def __getitem__(self, index: int) -> ReviewedBenefitProfiles: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[ReviewedBenefitProfiles]: ...

    def __getitem__(
        self, index: int | slice
    ) -> ReviewedBenefitProfiles | Sequence[ReviewedBenefitProfiles]:
        if isinstance(index, slice):
            return ((self.authority,) * self.actual)[index]
        if index < 0 or index >= self.actual:
            raise IndexError(index)
        return self.authority

    def __iter__(self) -> Iterator[ReviewedBenefitProfiles]:
        return iter((self.authority,) * self.actual)


def test_reviewed_authority_bound_uses_realized_iteration_not_reported_length() -> None:
    with pytest.raises(BenefitClosureError, match="bounded item limit"):
        ReviewedBenefitAuthorities.create(_LyingAuthoritySequence(_authority(), 65))


def test_profile_path_failures_redact_sensitive_locations_from_traceback() -> None:
    sensitive = "/private/clients/acme-secret/reviewed-benefit.json"

    with pytest.raises(BenefitClosureError) as exc:
        load_reviewed_benefit_profiles(Path(sensitive), "a" * 64)

    rendered = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert sensitive not in rendered
    assert "acme-secret" not in rendered
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
