from __future__ import annotations

import inspect
import json
import threading
from dataclasses import dataclass

import pytest
import ctx.runtime.production_catalog as production_catalog_module

from ctx.engine.planner import CapabilityCandidate
from ctx.runtime.benefit_closure import EligibleCatalogClaim, QueryCapabilityEligibility
from ctx.runtime.production_catalog import (
    RELEASE_QUERY_CATALOG_ROOT_SHA256,
    ReleaseCatalogError,
    ReleasePinnedQueryCatalog,
    open_release_pinned_query_catalog,
)
from ctx.runtime.release_material import (
    RELEASE_INSTALL_SKILL_ID,
    RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE,
    RELEASE_LOAD_SKILL_ID,
    RELEASE_LOAD_SKILL_MATERIAL_RESOURCE,
)


@dataclass
class _HostPolicy:
    host_policy_snapshot_digest: str = "a" * 64

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        from ctx.runtime.authenticated_benefit import capability_presentation_digest
        from ctx.runtime.benefit_closure import eligible_catalog_claim_digest

        return QueryCapabilityEligibility(
            presentation_digest=capability_presentation_digest(presentation),
            catalog_entry_claim_digest=claim.catalog_entry_claim_digest,
            catalog_claim_digest=eligible_catalog_claim_digest(claim),
            available=True,
            permissions_allowed=True,
            credentials_available=True,
        )


def test_release_factory_owns_all_catalog_trust_inputs_and_selects_one_reviewed_skill() -> None:
    assert tuple(inspect.signature(open_release_pinned_query_catalog).parameters) == ()
    assert len(RELEASE_QUERY_CATALOG_ROOT_SHA256) == 64

    catalog = open_release_pinned_query_catalog()
    prepared = catalog.prepare_query(
        task="Fix the Python tests",
        language="",
        host_policy=_HostPolicy(),
    )

    assert catalog.release_sequence == 4
    assert catalog.mode == "reviewed"
    assert prepared.closure.observation.requested_limit == 5
    candidates = prepared.closure.source.retrieve(prepared.closure.observation)
    assert tuple(candidate.capability_id for candidate in candidates) == (
        "skill:ctx-python-testing",
    )
    assert candidates[0].actionability == "load"
    assert prepared.material_authority is not None
    assert prepared.install_authority is None

    prepared.close()
    catalog.close()
    with pytest.raises(ReleaseCatalogError, match="closed"):
        catalog.prepare_query(task="testing", language="Python", host_policy=_HostPolicy())


def test_release_factory_does_not_read_absent_install_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource_reads: list[str] = []
    original = production_catalog_module._read_resource

    def observed(name: str, *, maximum_bytes: int) -> bytes:
        resource_reads.append(name)
        return original(name, maximum_bytes=maximum_bytes)

    monkeypatch.setattr(production_catalog_module, "_read_resource", observed)

    catalog = open_release_pinned_query_catalog()
    catalog.close()

    assert RELEASE_INSTALL_SKILL_MATERIAL_RESOURCE not in resource_reads


def test_packaged_load_asset_contains_only_static_testing_material() -> None:
    body = production_catalog_module._read_resource(
        RELEASE_LOAD_SKILL_MATERIAL_RESOURCE,
        maximum_bytes=production_catalog_module.MAX_RELEASE_SKILL_MATERIAL_BYTES,
    )
    decoded = json.loads(body)

    assert [entry["id"] for entry in decoded["entries"]] == [RELEASE_LOAD_SKILL_ID]
    assert RELEASE_INSTALL_SKILL_ID.encode() not in body
    assert b"# ctx Python State and Protocols" not in body


def test_release_factory_abstains_for_wrong_language_and_generic_testing() -> None:
    catalog = open_release_pinned_query_catalog()
    for task in ("Fix the JavaScript tests", "Improve the test suite"):
        prepared = catalog.prepare_query(task=task, language="", host_policy=_HostPolicy())
        assert prepared.closure.observation.requested_limit == 0
        assert prepared.closure.source.retrieve(prepared.closure.observation) == ()
        assert prepared.material_authority is None
        prepared.close()
    catalog.close()


def test_release_factory_rejects_packaged_asset_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = production_catalog_module._read_resource

    def substituted(name: str, *, maximum_bytes: int) -> bytes:
        body = original(name, maximum_bytes=maximum_bytes)
        if name == "benefit-eligible-catalog-v1.json":
            return body.replace(b'"name":"ctx-python-testing"', b'"name":"ctx-python-testinG"')
        return body

    monkeypatch.setattr(production_catalog_module, "_read_resource", substituted)

    with pytest.raises(ReleaseCatalogError, match="pinned root"):
        open_release_pinned_query_catalog()


def test_release_catalog_value_cannot_be_reconstructed_by_callers() -> None:
    with pytest.raises(TypeError, match="release factory"):
        ReleasePinnedQueryCatalog()


@dataclass
class _CloseOnDigestPolicy:
    catalog: ReleasePinnedQueryCatalog

    @property
    def host_policy_snapshot_digest(self) -> str:
        self.catalog.close()
        return "a" * 64

    def eligibility_for(
        self,
        presentation: CapabilityCandidate,
        claim: EligibleCatalogClaim,
    ) -> QueryCapabilityEligibility:
        return _HostPolicy().eligibility_for(presentation, claim)


def test_release_catalog_host_callback_cannot_deadlock_close() -> None:
    catalog = open_release_pinned_query_catalog()
    outcomes: list[object] = []

    def prepare() -> None:
        try:
            outcomes.append(
                catalog.prepare_query(
                    task="testing",
                    language="Python",
                    host_policy=_CloseOnDigestPolicy(catalog),
                )
            )
        except BaseException as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=prepare, daemon=True)
    thread.start()
    thread.join(timeout=0.5)

    assert not thread.is_alive(), "prepare_query deadlocked when a host callback closed the catalog"
    assert len(outcomes) == 1
