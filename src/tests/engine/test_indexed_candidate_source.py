from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import networkx as nx
import pytest

from ctx.core.graph.graph_store import build_graph_store
from ctx.core.resolve import engine_candidates
from ctx.core.resolve import recommendations
from ctx.core.resolve.engine_candidates import IndexedGraphCandidateSource
from ctx.core.resolve.recommendations import recommend_by_tags_indexed
from ctx.engine.content import (
    ExposureAuthorizer,
    MaterialDescriptor,
    PreparedCapabilityContent,
)
from ctx.engine.installation import (
    InstallAuthorizer,
    InstallPlanDescriptor,
    PreparedInstallPlan,
)
from ctx.engine.planner import (
    BoundedCapabilityPlanner,
    CapabilitySelection,
    CandidateSourceUnavailable,
    WorkObservation,
)
from ctx.engine.protocol import HostAction


_ENTITY_TYPES = ("skill", "agent", "mcp-server", "harness")


def _artifact_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _graph(*, suffix: str = "") -> nx.Graph:
    graph = nx.Graph()
    nodes = (
        ("skill:python-tdd", "skill", ["python", "testing"]),
        ("agent:python-reviewer", "agent", ["python", "review"]),
        ("mcp-server:python-docs", "mcp-server", ["docs", "python"]),
        ("harness:python-runner", "harness", ["python", "runner"]),
        ("skill:python-lint", "skill", ["lint", "python"]),
        ("skill:python-types", "skill", ["python", "types"]),
    )
    for node_id, kind, tags in nodes:
        name = node_id.split(":", 1)[1]
        graph.add_node(
            f"{node_id}{suffix}",
            label=f"{name}{suffix}",
            type=kind,
            tags=tags,
            description="raw prose must never enter a durable candidate",
            source="/private/catalog/source.md",
            install_command="curl secret.example | sh",
        )
    graph.add_edges_from(
        (
            (f"skill:python-tdd{suffix}", f"agent:python-reviewer{suffix}"),
            (f"skill:python-tdd{suffix}", f"mcp-server:python-docs{suffix}"),
            (f"harness:python-runner{suffix}", f"skill:python-lint{suffix}"),
        )
    )
    return graph


def _store(tmp_path: Path, *, name: str = "graph-store.sqlite3", suffix: str = "") -> Path:
    path = tmp_path / name
    build_graph_store(path, _graph(suffix=suffix))
    path.chmod(0o444)
    return path


def _observation() -> WorkObservation:
    return WorkObservation(
        signals=("docs", "lint", "python", "review", "runner", "testing", "types"),
        languages=("python",),
        requested_limit=5,
    )


@dataclass
class StaticInstallPlanPort:
    installation_snapshot_digest: str
    descriptors: dict[str, InstallPlanDescriptor | None]
    calls: list[str] = field(default_factory=list)

    def describe(self, capability_id: str, _kind: str) -> InstallPlanDescriptor | None:
        self.calls.append(capability_id)
        return self.descriptors.get(capability_id)

    def prepare(
        self,
        action: HostAction,
        selection: CapabilitySelection,
        descriptor: InstallPlanDescriptor,
        *,
        expected_catalog_snapshot_digest: str,
        expected_policy_digest: str,
        authority: InstallAuthorizer | None = None,
    ) -> PreparedInstallPlan:
        raise AssertionError("candidate retrieval must not prepare an install plan")


def _install_descriptor(
    capability_id: str,
    snapshot_digest: str,
    *,
    plan_salt: str | None = None,
    permission_expansion: bool = False,
) -> InstallPlanDescriptor:
    kind = capability_id.split(":", 1)[0]
    return InstallPlanDescriptor.create(
        capability_id=capability_id,
        kind=kind,
        installer_id=f"{kind}-installer",
        plan_digest=_artifact_text_digest(plan_salt or capability_id),
        provenance_digest=snapshot_digest,
        permission_expansion=permission_expansion,
    )


def _artifact_text_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _load_descriptor(capability_id: str, snapshot_digest: str) -> MaterialDescriptor:
    kind = capability_id.split(":", 1)[0]
    body_digest = _artifact_text_digest(f"body:{capability_id}")
    values: dict[str, object] = {
        "actionability": "load",
        "capability_id": capability_id,
        "content_bytes": 100,
        "content_sha256": body_digest,
        "estimated_tokens": 25,
        "kind": kind,
        "material_snapshot_digest": snapshot_digest,
        "schema": "ctx.material-descriptor-v1",
    }
    descriptor_digest = _artifact_text_digest(
        json.dumps(
            values, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
    )
    return MaterialDescriptor(
        capability_id=capability_id,
        kind=kind,
        actionability="load",
        content_sha256=body_digest,
        content_bytes=100,
        estimated_tokens=25,
        provenance_digest=snapshot_digest,
        descriptor_digest=descriptor_digest,
    )


def test_indexed_source_matches_existing_indexed_candidate_order(tmp_path: Path) -> None:
    path = _store(tmp_path)
    with IndexedGraphCandidateSource(path, _artifact_digest(path)) as source:
        actual = source.retrieve(_observation())

    expected = recommend_by_tags_indexed(
        path,
        list(_observation().signals),
        top_n=50,
        query=" ".join(_observation().signals),
        entity_types=_ENTITY_TYPES,
        min_normalized_score=0.0,
    )
    assert expected is not None

    assert [candidate.capability_id for candidate in actual] == [
        f"{row['type']}:{row['name']}" for row in expected[0]
    ]


def test_indexed_source_supplies_all_types_to_one_global_five_item_budget(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path)

    with IndexedGraphCandidateSource(path, _artifact_digest(path)) as source:
        candidates = source.retrieve(_observation())
        plan = BoundedCapabilityPlanner(source).plan(_observation())

    assert len(candidates) == 6
    assert {candidate.kind for candidate in candidates} == set(_ENTITY_TYPES)
    assert plan.status == "ready"
    assert len(plan.selections) == 5
    assert {selection.kind for selection in plan.selections} == set(_ENTITY_TYPES)


def test_indexed_source_digest_binds_artifact_and_adapter_contract(tmp_path: Path) -> None:
    first = _store(tmp_path, name="first.sqlite3")
    second = _store(tmp_path, name="second.sqlite3", suffix="-v2")

    with (
        IndexedGraphCandidateSource(first, _artifact_digest(first)) as first_source,
        IndexedGraphCandidateSource(first, _artifact_digest(first)) as repeated_source,
        IndexedGraphCandidateSource(second, _artifact_digest(second)) as second_source,
    ):
        assert len(first_source.catalog_snapshot_digest) == 64
        assert first_source.catalog_snapshot_digest == repeated_source.catalog_snapshot_digest
        assert first_source.catalog_snapshot_digest != second_source.catalog_snapshot_digest


def test_indexed_source_is_unchanged_after_store_path_replacement(tmp_path: Path) -> None:
    path = _store(tmp_path, name="active.sqlite3")
    replacement = _store(tmp_path, name="replacement.sqlite3", suffix="-replacement")
    source = IndexedGraphCandidateSource(path, _artifact_digest(path))
    before = source.retrieve(_observation())

    try:
        os.replace(replacement, path)
    except PermissionError:
        # Windows prevents replacement of an open SQLite file, which itself
        # preserves the pinned artifact invariant.
        pass
    after = source.retrieve(_observation())
    source.close()

    assert after == before
    assert all("replacement" not in candidate.capability_id for candidate in after)


def test_indexed_source_binds_open_connection_across_parent_symlink_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _store(first_dir)
    _store(second_dir, suffix="-aba")
    alias = tmp_path / "catalog"
    try:
        alias.symlink_to(first_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    aliased_store = alias / first.name
    expected_digest = _artifact_digest(aliased_store)
    real_connect = sqlite3.connect

    def point_alias(target: Path) -> None:
        replacement = tmp_path / "catalog-next"
        if os.path.lexists(replacement):
            replacement.unlink()
        replacement.symlink_to(target, target_is_directory=True)
        os.replace(replacement, alias)

    def swapping_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        point_alias(second_dir)
        try:
            return real_connect(database, *args, **kwargs)
        finally:
            point_alias(first_dir)

    monkeypatch.setattr(engine_candidates.sqlite3, "connect", swapping_connect)

    with IndexedGraphCandidateSource(aliased_store, expected_digest) as source:
        candidates = source.retrieve(_observation())

    assert candidates
    assert all("-aba" not in candidate.capability_id for candidate in candidates)


def test_indexed_source_uses_read_only_isolated_private_copy(tmp_path: Path) -> None:
    path = _store(tmp_path)
    expected_digest = _artifact_digest(path)
    source = IndexedGraphCandidateSource(path, expected_digest)
    snapshot_directory = Path(source._snapshot_directory.name)
    snapshot_path = snapshot_directory / "graph-store.sqlite3"

    with source:
        assert not os.path.samefile(path, snapshot_path)
        assert _artifact_digest(snapshot_path) == expected_digest
        assert snapshot_directory.stat().st_mode & 0o777 == 0o700
        assert snapshot_path.stat().st_mode & 0o222 == 0
        assert source.retrieve(_observation())

    assert not snapshot_directory.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX keeps an unlinked SQLite file open")
def test_indexed_source_can_remove_snapshot_namespace_before_retrieval(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path)
    source = IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        remove_snapshot_namespace_on_open=True,
    )
    snapshot_directory = Path(source._snapshot_directory.name)

    assert not snapshot_directory.exists()
    assert source.retrieve(_observation())

    source.close()
    source.close()


def test_indexed_source_is_unchanged_after_original_inode_contents_mutate(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, name="active.sqlite3")
    mutation = _store(tmp_path, name="mutation.sqlite3", suffix="-in-place")
    expected_digest = _artifact_digest(path)

    with IndexedGraphCandidateSource(path, expected_digest) as source:
        snapshot_path = Path(source._snapshot_directory.name) / "graph-store.sqlite3"
        path.chmod(0o600)
        with path.open("r+b") as target:
            target.write(mutation.read_bytes())
            target.truncate()
            target.flush()
            os.fsync(target.fileno())
        path.chmod(0o444)

        candidates = source.retrieve(_observation())

        assert _artifact_digest(snapshot_path) == expected_digest
        assert not os.path.samefile(path, snapshot_path)
        assert all("-in-place" not in candidate.capability_id for candidate in candidates)


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
def test_indexed_source_rejects_sqlite_sidecars(
    tmp_path: Path,
    sidecar_suffix: str,
) -> None:
    path = _store(tmp_path)
    Path(f"{path}{sidecar_suffix}").write_bytes(b"untrusted sidecar")

    with pytest.raises(CandidateSourceUnavailable, match="unavailable"):
        IndexedGraphCandidateSource(path, _artifact_digest(path))


def test_indexed_source_rejects_missing_corrupt_writable_symlink_or_mismatch(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(CandidateSourceUnavailable, match="unavailable"):
        IndexedGraphCandidateSource(missing, "0" * 64)

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not sqlite")
    corrupt.chmod(0o444)
    with pytest.raises(CandidateSourceUnavailable, match="unavailable"):
        IndexedGraphCandidateSource(corrupt, _artifact_digest(corrupt))

    writable = tmp_path / "writable.sqlite3"
    build_graph_store(writable, _graph())
    with pytest.raises(CandidateSourceUnavailable, match="unavailable"):
        IndexedGraphCandidateSource(writable, _artifact_digest(writable))

    target = _store(tmp_path, name="target.sqlite3")
    symlink = tmp_path / "symlink.sqlite3"
    symlink.symlink_to(target)
    with pytest.raises(CandidateSourceUnavailable, match="unavailable"):
        IndexedGraphCandidateSource(symlink, _artifact_digest(target))

    with pytest.raises(CandidateSourceUnavailable, match="unavailable"):
        IndexedGraphCandidateSource(target, "f" * 64)


def test_indexed_source_never_uses_graph_semantic_or_external_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _store(tmp_path)

    def forbidden(*_: object, **__: object) -> None:
        raise AssertionError("volatile retrieval path was used")

    monkeypatch.setattr(recommendations, "_load_semantic_index", forbidden)
    monkeypatch.setattr(recommendations, "_recommend_external_catalog", forbidden)

    with IndexedGraphCandidateSource(path, _artifact_digest(path)) as source:
        candidates = source.retrieve(_observation())

    assert candidates
    rendered = repr(candidates)
    assert "raw prose" not in rendered
    assert "/private/catalog" not in rendered
    assert "curl" not in rendered


def test_exact_name_and_tag_matches_populate_typed_evidence(tmp_path: Path) -> None:
    graph = nx.Graph()
    graph.add_node(
        "agent:python-specialist",
        label="python-specialist",
        type="agent",
        tags=["review"],
    )
    path = tmp_path / "name-match.sqlite3"
    build_graph_store(path, graph)
    path.chmod(0o444)
    observation = WorkObservation(signals=("python", "review"), languages=("python",))

    with IndexedGraphCandidateSource(path, _artifact_digest(path)) as source:
        candidate = source.retrieve(observation)[0]

    assert candidate.matching_signals == ("python", "review")
    assert candidate.reason_codes == (
        "graph-match",
        "language-match",
        "name-match",
        "signal-match",
        "tag-match",
    )


def test_indexed_source_projects_only_typed_actionability_not_install_prose(
    tmp_path: Path,
) -> None:
    graph = nx.Graph()
    graph.add_node(
        "skill:local-json",
        label="local-json",
        type="skill",
        tags=["json", "python"],
        status="local-wiki",
        source="organization-runtime",
    )
    graph.add_node(
        "skill:remote-json",
        label="remote-json",
        type="skill",
        tags=["json", "python"],
        status="available",
        install_command="curl private.example | sh",
    )
    path = tmp_path / "actionability.sqlite3"
    build_graph_store(path, graph)
    path.chmod(0o444)
    observation = WorkObservation(signals=("json",), languages=("python",))

    with IndexedGraphCandidateSource(path, _artifact_digest(path)) as source:
        candidates = source.retrieve(observation)

    assert {candidate.capability_id: candidate.actionability for candidate in candidates} == {
        "skill:local-json": "manual",
        "skill:remote-json": "manual",
    }
    assert "private.example" not in repr(candidates)
    assert "curl" not in repr(candidates)


def test_indexed_source_order_is_deterministic(tmp_path: Path) -> None:
    path = _store(tmp_path)

    with IndexedGraphCandidateSource(path, _artifact_digest(path)) as source:
        first = source.retrieve(_observation())
        second = source.retrieve(_observation())

    assert second == first


def test_authenticated_install_descriptors_keep_absent_types_in_one_global_plan(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path)
    snapshot = _artifact_text_digest("install-catalog-v1")
    installable_ids = (
        "skill:python-tdd",
        "skill:python-lint",
        "skill:python-types",
        "agent:python-reviewer",
        "mcp-server:python-docs",
    )
    port = StaticInstallPlanPort(
        snapshot,
        {
            capability_id: _install_descriptor(capability_id, snapshot)
            for capability_id in installable_ids
        },
    )

    with IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        install_plan_port=port,
    ) as source:
        candidates = source.retrieve(_observation())
        plan = BoundedCapabilityPlanner(source).plan(_observation())

    projected = {candidate.capability_id: candidate for candidate in candidates}
    assert all(projected[item].actionability == "install" for item in installable_ids)
    assert all(projected[item].install_descriptor_digest is not None for item in installable_ids)
    assert all(projected[item].install_plan_digest is not None for item in installable_ids)
    assert plan.status == "ready"
    assert len(plan.selections) == 5
    assert {item.kind for item in plan.selections if item.actionability == "install"} == {
        "agent",
        "mcp-server",
        "skill",
    }


def test_installed_material_wins_over_an_install_descriptor(tmp_path: Path) -> None:
    path = _store(tmp_path)
    capability_id = "skill:python-tdd"
    install_snapshot = _artifact_text_digest("install-catalog")
    material_snapshot = _artifact_text_digest("material-catalog")
    install_port = StaticInstallPlanPort(
        install_snapshot,
        {capability_id: _install_descriptor(capability_id, install_snapshot)},
    )

    @dataclass
    class MaterialPort:
        material_snapshot_digest: str

        def describe(self, candidate_id: str, kind: str) -> MaterialDescriptor:
            if candidate_id == capability_id:
                return _load_descriptor(candidate_id, self.material_snapshot_digest)
            values: dict[str, object] = {
                "actionability": "manual",
                "capability_id": candidate_id,
                "content_bytes": 0,
                "content_sha256": None,
                "estimated_tokens": 0,
                "kind": kind,
                "material_snapshot_digest": self.material_snapshot_digest,
                "schema": "ctx.material-descriptor-v1",
            }
            return MaterialDescriptor(
                capability_id=candidate_id,
                kind=kind,
                actionability="manual",
                content_sha256=None,
                content_bytes=0,
                estimated_tokens=0,
                provenance_digest=self.material_snapshot_digest,
                descriptor_digest=_artifact_text_digest(
                    json.dumps(
                        values,
                        ensure_ascii=True,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                ),
            )

        def prepare(
            self,
            action: HostAction,
            selection: CapabilitySelection,
            *,
            expected_catalog_snapshot_digest: str,
            authority: ExposureAuthorizer | None = None,
        ) -> PreparedCapabilityContent:
            raise AssertionError("candidate retrieval must not prepare capability material")

    with IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        install_plan_port=install_port,
        material_port=MaterialPort(material_snapshot),
    ) as source:
        candidates = {item.capability_id: item for item in source.retrieve(_observation())}

    assert candidates[capability_id].actionability == "load"
    assert candidates[capability_id].install_descriptor_digest is None
    assert candidates[capability_id].install_plan_digest is None
    assert capability_id not in install_port.calls


def test_install_descriptor_missing_is_manual_and_bad_provenance_is_row_local(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path)
    snapshot = _artifact_text_digest("install-catalog")
    missing_port = StaticInstallPlanPort(snapshot, {})
    with IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        install_plan_port=missing_port,
    ) as source:
        assert all(
            candidate.actionability == "manual" for candidate in source.retrieve(_observation())
        )

    wrong_snapshot = _artifact_text_digest("wrong-catalog")
    good_capability_id = "skill:python-lint"
    tampered_port = StaticInstallPlanPort(
        snapshot,
        {
            "skill:python-tdd": _install_descriptor(
                "skill:python-tdd",
                wrong_snapshot,
            ),
            good_capability_id: _install_descriptor(good_capability_id, snapshot),
        },
    )
    with IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        install_plan_port=tampered_port,
    ) as source:
        candidates = {item.capability_id: item for item in source.retrieve(_observation())}

    assert "skill:python-tdd" not in candidates
    assert candidates[good_capability_id].actionability == "install"
    assert any(item.actionability == "manual" for item in candidates.values())


def test_bad_material_authority_skips_only_its_row_and_keeps_valid_load_peer(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path)
    snapshot = _artifact_text_digest("material-catalog")
    wrong_snapshot = _artifact_text_digest("wrong-material-catalog")
    bad_capability_id = "skill:python-tdd"
    good_capability_id = "skill:python-lint"

    @dataclass
    class MixedMaterialPort:
        material_snapshot_digest: str

        def describe(self, capability_id: str, _kind: str) -> MaterialDescriptor:
            return _load_descriptor(
                capability_id,
                wrong_snapshot if capability_id == bad_capability_id else snapshot,
            )

        def prepare(
            self,
            action: HostAction,
            selection: CapabilitySelection,
            *,
            expected_catalog_snapshot_digest: str,
            authority: ExposureAuthorizer | None = None,
        ) -> PreparedCapabilityContent:
            raise AssertionError("candidate retrieval must not prepare capability material")

    with IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        material_port=MixedMaterialPort(snapshot),
    ) as source:
        candidates = {item.capability_id: item for item in source.retrieve(_observation())}

    assert bad_capability_id not in candidates
    assert candidates[good_capability_id].actionability == "load"
    assert len(candidates) == 5


def test_install_descriptor_and_snapshot_mutation_are_rejected(tmp_path: Path) -> None:
    path = _store(tmp_path)
    snapshot = _artifact_text_digest("install-catalog")
    capability_id = "skill:python-tdd"
    port = StaticInstallPlanPort(
        snapshot,
        {capability_id: _install_descriptor(capability_id, snapshot)},
    )
    with IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        install_plan_port=port,
    ) as source:
        source.retrieve(_observation())
        port.descriptors[capability_id] = _install_descriptor(
            capability_id,
            snapshot,
            plan_salt="changed-plan",
        )
        with pytest.raises(CandidateSourceUnavailable, match="changed within"):
            source.retrieve(_observation())

    port = StaticInstallPlanPort(snapshot, {})
    with IndexedGraphCandidateSource(
        path,
        _artifact_digest(path),
        install_plan_port=port,
    ) as source:
        port.installation_snapshot_digest = _artifact_text_digest("changed-snapshot")
        with pytest.raises(CandidateSourceUnavailable, match="snapshot changed"):
            source.retrieve(_observation())


def test_install_snapshot_and_descriptor_change_bound_candidate_digests(tmp_path: Path) -> None:
    path = _store(tmp_path)
    first_snapshot = _artifact_text_digest("install-catalog-v1")
    second_snapshot = _artifact_text_digest("install-catalog-v2")
    capability_id = "skill:python-tdd"
    first_port = StaticInstallPlanPort(
        first_snapshot,
        {capability_id: _install_descriptor(capability_id, first_snapshot)},
    )
    second_port = StaticInstallPlanPort(
        second_snapshot,
        {
            capability_id: _install_descriptor(
                capability_id,
                second_snapshot,
                plan_salt="changed-plan",
            )
        },
    )
    with (
        IndexedGraphCandidateSource(
            path,
            _artifact_digest(path),
            install_plan_port=first_port,
        ) as first,
        IndexedGraphCandidateSource(
            path,
            _artifact_digest(path),
            install_plan_port=second_port,
        ) as second,
    ):
        first_candidates = {item.capability_id: item for item in first.retrieve(_observation())}
        second_candidates = {item.capability_id: item for item in second.retrieve(_observation())}
        assert first.catalog_snapshot_digest != second.catalog_snapshot_digest
        assert (
            first_candidates[capability_id].source_digest
            != second_candidates[capability_id].source_digest
        )


def test_same_install_plan_with_different_descriptor_is_bound_exactly(tmp_path: Path) -> None:
    path = _store(tmp_path)
    snapshot = _artifact_text_digest("install-catalog")
    capability_id = "skill:python-tdd"
    first_descriptor = _install_descriptor(capability_id, snapshot, plan_salt="shared-plan")
    second_descriptor = _install_descriptor(
        capability_id,
        snapshot,
        plan_salt="shared-plan",
        permission_expansion=True,
    )
    assert first_descriptor.plan_digest == second_descriptor.plan_digest
    assert first_descriptor.descriptor_digest != second_descriptor.descriptor_digest

    with (
        IndexedGraphCandidateSource(
            path,
            _artifact_digest(path),
            install_plan_port=StaticInstallPlanPort(
                snapshot,
                {capability_id: first_descriptor},
            ),
        ) as first,
        IndexedGraphCandidateSource(
            path,
            _artifact_digest(path),
            install_plan_port=StaticInstallPlanPort(
                snapshot,
                {capability_id: second_descriptor},
            ),
        ) as second,
    ):
        first_candidate = next(
            item for item in first.retrieve(_observation()) if item.capability_id == capability_id
        )
        second_candidate = next(
            item for item in second.retrieve(_observation()) if item.capability_id == capability_id
        )

    assert first_candidate.install_plan_digest == second_candidate.install_plan_digest
    assert first_candidate.install_descriptor_digest == first_descriptor.descriptor_digest
    assert second_candidate.install_descriptor_digest == second_descriptor.descriptor_digest
    assert first_candidate.source_digest != second_candidate.source_digest
