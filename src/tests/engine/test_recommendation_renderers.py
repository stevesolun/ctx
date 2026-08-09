from __future__ import annotations

import ast
import copy
import hashlib
import inspect
from dataclasses import replace

import pytest

from ctx.adapters import recommendation_presentation
from ctx.adapters.claude_code import engine_hook
from ctx.adapters.claude_code.engine_hook import render_recommendation_hook
from ctx.adapters.codex import engine_adapter
from ctx.adapters.codex.engine_adapter import render_recommendation_context
from ctx.adapters.recommendation_presentation import (
    RecommendationPresentationError,
    render_present_bundle_context,
)
from ctx.engine.content import AuthorizedMaterial, MaterialDescriptor, MaterialIdentity
from ctx.engine.installation import InstallPlanDescriptor
from ctx.engine.lineage import CatalogCapabilityIdentity
from ctx.engine.planner import CapabilityCandidate
from ctx.engine.planning_v3 import (
    CapabilityBenefitProjection,
    CapabilityPlanSelectionV3,
    InstallPlanningAuthority,
    LoadPlanningAuthority,
    ManualPlanningAuthority,
    PlanningAuthority,
)
from ctx.engine.protocol import HostAction, ScopeRef, Transition


def _scope() -> ScopeRef:
    return ScopeRef(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        repository_id="repository-1",
        session_id="session-1",
        exposure_id="exposure-1",
        host_context_id="host-1",
    )


def _row(
    capability_id: str,
    kind: str,
    name: str,
    digest_character: str,
    score: int,
    actionability: str,
    signals: list[str],
    reasons: list[str],
) -> dict[str, object]:
    return {
        "actionability": actionability,
        "capability_id": capability_id,
        "catalog_entry_digest": digest_character * 64,
        "kind": kind,
        "matching_signals": signals,
        "name": name,
        "normalized_score_ppm": score,
        "reason_codes": reasons,
    }


def _transition() -> Transition:
    scope = _scope()
    action = HostAction(
        action_id="action-present-1",
        kind="PresentBundle",
        scope=scope,
        precondition_revision=2,
        payload={
            "plan_digest": "f" * 64,
            "capabilities": [
                _row(
                    "mcp-server:python-docs",
                    "mcp-server",
                    "python-docs",
                    "a",
                    750_000,
                    "install",
                    ["docs", "python"],
                    ["signal-match"],
                ),
                _row(
                    "skill:python-tdd",
                    "skill",
                    "python-tdd",
                    "b",
                    950_000,
                    "load",
                    ["python", "testing"],
                    ["signal-match", "workflow-match"],
                ),
                _row(
                    "harness:codex-python",
                    "harness",
                    "codex-python",
                    "c",
                    850_000,
                    "manual",
                    ["python"],
                    ["host-match"],
                ),
                _row(
                    "agent:python-reviewer",
                    "agent",
                    "python-reviewer",
                    "d",
                    900_000,
                    "load",
                    ["python", "review"],
                    ["signal-match"],
                ),
                _row(
                    "skill:security-review",
                    "skill",
                    "security-review",
                    "e",
                    800_000,
                    "manual",
                    ["review", "security"],
                    ["policy-match"],
                ),
            ],
        },
    )
    return Transition(
        event_id="event-observation-1",
        scope=scope,
        from_revision=1,
        to_revision=2,
        actions=(action,),
    )


EXPECTED_CONTEXT = """CTX recommendation bundle (committed, advisory only):
1. kind=mcp-server | name=python-docs | id=mcp-server:python-docs | actionability=install | score_ppm=750000
2. kind=skill | name=python-tdd | id=skill:python-tdd | actionability=load | score_ppm=950000
3. kind=harness | name=codex-python | id=harness:codex-python | actionability=manual | score_ppm=850000
4. kind=agent | name=python-reviewer | id=agent:python-reviewer | actionability=load | score_ppm=900000
5. kind=skill | name=security-review | id=skill:security-review | actionability=manual | score_ppm=800000
Use only capabilities relevant to the current task. Do not install, load, or activate anything without user approval."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _v3_row(
    capability_id: str,
    *,
    actionability: str,
    score: int,
) -> dict[str, object]:
    kind, name = capability_id.split(":", 1)
    catalog_identity = CatalogCapabilityIdentity.create(
        capability_id=capability_id,
        kind=kind,
        catalog_namespace_digest=_digest("renderer-catalog"),
    )
    install_descriptor_digest = None
    install_plan_digest = None
    authority: PlanningAuthority
    if actionability == "load":
        material = MaterialIdentity.create(
            capability_id=capability_id,
            kind=kind,
            content_sha256=_digest(f"content:{capability_id}"),
            content_bytes=32,
        )
        material_descriptor = MaterialDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            actionability="load",
            content_sha256=material.content_sha256,
            content_bytes=material.content_bytes,
            estimated_tokens=8,
            provenance_digest=_digest("renderer-material-snapshot"),
            material_identity_digest=material.identity_digest,
        )
        authority = LoadPlanningAuthority(
            material=AuthorizedMaterial.from_catalog(
                catalog_identity_digest=catalog_identity.identity_digest,
                descriptor=material_descriptor,
            )
        )
        tier = "executable"
    elif actionability == "install":
        result_material = MaterialIdentity.create(
            capability_id=capability_id,
            kind=kind,
            content_sha256=_digest(f"installed-content:{capability_id}"),
            content_bytes=48,
        )
        install_descriptor = InstallPlanDescriptor.create(
            capability_id=capability_id,
            kind=kind,
            installer_id="ctx-installer",
            plan_digest=_digest(f"install-plan:{capability_id}"),
            provenance_digest=_digest("renderer-install-snapshot"),
            result_material_identity_digest=result_material.identity_digest,
        )
        install_descriptor_digest = install_descriptor.descriptor_digest
        install_plan_digest = install_descriptor.plan_digest
        authority = InstallPlanningAuthority(
            descriptor=install_descriptor,
            result_material=result_material,
        )
        tier = "executable"
    else:
        authority = ManualPlanningAuthority()
        tier = "advisory"
    presentation = CapabilityCandidate(
        capability_id=capability_id,
        kind=kind,
        name=name,
        source_digest=_digest(f"catalog-entry:{capability_id}"),
        normalized_score_ppm=score,
        matching_signals=("python",),
        reason_codes=("signal-match",),
        actionability=actionability,
        install_descriptor_digest=install_descriptor_digest,
        install_plan_digest=install_plan_digest,
    )
    return CapabilityPlanSelectionV3(
        presentation=presentation,
        catalog_identity=catalog_identity,
        benefit=CapabilityBenefitProjection(
            tier=tier,
            individual_net_benefit_u=score,
            marginal_net_benefit_u=max(score, 1),
        ),
        authority=authority,
    ).to_mapping()


def _v3_transition(rows: list[dict[str, object]]) -> Transition:
    scope = _scope()
    return Transition(
        event_id="event-v3-observation-1",
        scope=scope,
        from_revision=1,
        to_revision=2,
        actions=(
            HostAction(
                action_id="action-v3-present-1",
                kind="PresentBundle",
                scope=scope,
                precondition_revision=2,
                payload={"plan_digest": _digest("v3-plan"), "capabilities": rows},
            ),
        ),
    )


EXPECTED_V3_CONTEXT = """CTX recommendation bundle (committed, advisory only):
1. kind=agent | name=reviewer | id=agent:reviewer | actionability=manual | score_ppm=600000
2. kind=skill | name=python-tdd | id=skill:python-tdd | actionability=load | score_ppm=900000
3. kind=mcp-server | name=python-docs | id=mcp-server:python-docs | actionability=install | score_ppm=700000
Use only capabilities relevant to the current task. Do not install, load, or activate anything without user approval."""


def test_codex_and_claude_render_the_identical_ordered_engine_bundle() -> None:
    transition = _transition()

    codex_context = render_recommendation_context(transition)
    claude_envelope = render_recommendation_hook(transition)

    assert codex_context == EXPECTED_CONTEXT
    assert claude_envelope == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": EXPECTED_CONTEXT,
        }
    }


def test_renderers_accept_exact_install_bound_v2_rows_without_changing_order() -> None:
    transition = _transition()
    bundle = transition.actions[0]
    rows = []
    for raw in bundle.payload["capabilities"]:
        row = dict(raw)
        row["install_descriptor_digest"] = "8" * 64 if row["actionability"] == "install" else None
        row["install_plan_digest"] = "9" * 64 if row["actionability"] == "install" else None
        rows.append(row)
    v2 = replace(
        transition,
        actions=(
            replace(
                bundle,
                payload={
                    "plan_digest": bundle.payload["plan_digest"],
                    "capabilities": rows,
                },
            ),
        ),
    )

    assert render_recommendation_context(v2) == EXPECTED_CONTEXT
    assert render_recommendation_hook(v2) == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": EXPECTED_CONTEXT,
        }
    }


def test_shared_renderer_accepts_complete_v3_rows_without_reranking_or_leaking_authority() -> None:
    transition = _v3_transition(
        [
            _v3_row("agent:reviewer", actionability="manual", score=600_000),
            _v3_row("skill:python-tdd", actionability="load", score=900_000),
            _v3_row("mcp-server:python-docs", actionability="install", score=700_000),
        ]
    )

    context = render_present_bundle_context(transition)

    assert context == EXPECTED_V3_CONTEXT
    assert "catalog_identity" not in context
    assert "benefit" not in context
    assert "authority" not in context
    assert "install_descriptor_digest" not in context
    assert "install_plan_digest" not in context
    assert render_recommendation_context(transition) == EXPECTED_V3_CONTEXT
    assert render_recommendation_hook(transition) == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": EXPECTED_V3_CONTEXT,
        }
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-row-field",
        "unknown-catalog-field",
        "unknown-benefit-field",
        "unknown-authority-field",
        "catalog-identity-substitution",
        "benefit-tier-substitution",
    ],
)
def test_shared_renderer_rejects_mutated_v3_identity_benefit_and_authority(
    mutation: str,
) -> None:
    row = copy.deepcopy(_v3_row("skill:python-tdd", actionability="load", score=900_000))
    if mutation == "unknown-row-field":
        row["unknown"] = True
    elif mutation == "unknown-catalog-field":
        catalog_identity = row["catalog_identity"]
        assert isinstance(catalog_identity, dict)
        catalog_identity["unknown"] = True
    elif mutation == "unknown-benefit-field":
        benefit = row["benefit"]
        assert isinstance(benefit, dict)
        benefit["unknown"] = True
    elif mutation == "unknown-authority-field":
        authority = row["authority"]
        assert isinstance(authority, dict)
        authority["unknown"] = True
    elif mutation == "catalog-identity-substitution":
        foreign = _v3_row("skill:foreign", actionability="load", score=900_000)
        row["catalog_identity"] = foreign["catalog_identity"]
    else:
        benefit = row["benefit"]
        assert isinstance(benefit, dict)
        benefit["tier"] = "advisory"
    transition = _v3_transition([row])

    with pytest.raises(RecommendationPresentationError, match="invalid PresentBundle"):
        render_present_bundle_context(transition)


def test_shared_renderer_rejects_duplicate_and_empty_v3_bundles() -> None:
    row = _v3_row("agent:reviewer", actionability="manual", score=600_000)

    for rows in ([], [row, row]):
        with pytest.raises(RecommendationPresentationError, match="invalid PresentBundle"):
            render_present_bundle_context(_v3_transition(rows))


def test_shared_renderer_defensively_rejects_more_than_five_v3_rows() -> None:
    rows = [
        _v3_row(f"agent:reviewer-{index}", actionability="manual", score=600_000 - index)
        for index in range(6)
    ]
    transition = _v3_transition(rows[:1])
    action = transition.actions[0]
    object.__setattr__(
        action,
        "payload",
        {"plan_digest": _digest("v3-plan"), "capabilities": tuple(rows)},
    )

    with pytest.raises(RecommendationPresentationError, match="invalid PresentBundle"):
        render_present_bundle_context(transition)


def test_renderers_reject_mixed_or_unbound_v2_rows() -> None:
    transition = _transition()
    bundle = transition.actions[0]
    rows = [dict(raw) for raw in bundle.payload["capabilities"]]
    rows[0]["install_descriptor_digest"] = "8" * 64
    rows[0]["install_plan_digest"] = "9" * 64
    mixed = replace(
        transition,
        actions=(
            replace(
                bundle,
                payload={
                    "plan_digest": bundle.payload["plan_digest"],
                    "capabilities": rows,
                },
            ),
        ),
    )
    with pytest.raises(RecommendationPresentationError, match="invalid PresentBundle"):
        render_recommendation_context(mixed)

    for row in rows:
        row["install_descriptor_digest"] = None
        row["install_plan_digest"] = None
    unbound = replace(
        transition,
        actions=(
            replace(
                bundle,
                payload={
                    "plan_digest": bundle.payload["plan_digest"],
                    "capabilities": rows,
                },
            ),
        ),
    )
    with pytest.raises(RecommendationPresentationError, match="invalid PresentBundle"):
        render_recommendation_hook(unbound)


def test_renderers_abstain_when_transition_has_no_present_bundle() -> None:
    transition = Transition(
        event_id="event-abstained-1",
        scope=_scope(),
        from_revision=1,
        to_revision=2,
        diagnostics=({"code": "below-threshold"},),
    )

    assert render_recommendation_context(transition) is None
    assert render_recommendation_hook(transition) is None


def test_renderers_fail_closed_for_multiple_present_bundles() -> None:
    transition = _transition()
    bundle = transition.actions[0]
    multiple = replace(
        transition,
        actions=(bundle, replace(bundle, action_id="action-present-2")),
    )

    with pytest.raises(RecommendationPresentationError, match="at most one"):
        render_recommendation_context(multiple)
    with pytest.raises(RecommendationPresentationError, match="at most one"):
        render_recommendation_hook(multiple)


def test_renderers_fail_closed_for_a_non_exact_capability_row() -> None:
    transition = _transition()
    bundle = transition.actions[0]
    first = dict(bundle.payload["capabilities"][0])
    first["display_description"] = "untrusted free-form text"
    malformed = replace(
        transition,
        actions=(
            replace(
                bundle,
                payload={
                    "plan_digest": bundle.payload["plan_digest"],
                    "capabilities": [
                        first,
                        *bundle.payload["capabilities"][1:],
                    ],
                },
            ),
        ),
    )

    with pytest.raises(RecommendationPresentationError, match="invalid PresentBundle"):
        render_recommendation_context(malformed)
    with pytest.raises(RecommendationPresentationError, match="invalid PresentBundle"):
        render_recommendation_hook(malformed)


def test_renderers_have_no_ranking_or_legacy_orchestrator_dependencies() -> None:
    modules = (recommendation_presentation, engine_adapter, engine_hook)
    forbidden_imports = {
        "ctx.adapters.claude_code.hooks.bundle_orchestrator",
        "ctx.config",
        "ctx.engine.planner",
        "ctx.core.resolve.recommendations",
        "ctx_config",
    }

    for module in modules:
        source = inspect.getsource(module)
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert imported.isdisjoint(forbidden_imports)
        assert "sorted(" not in source
        assert ".sort(" not in source
