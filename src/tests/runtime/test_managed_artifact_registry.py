from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
import shutil
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import networkx as nx
import pytest
import ctx.runtime.managed_artifact_registry as registry_module

from ctx.core.graph.graph_store import build_graph_store
from ctx.core.resolve.engine_candidates import IndexedGraphCandidateSource
from ctx.engine.planner import WorkObservation
from ctx.engine.replay import ObservationReference, StructuredSurrogate
from ctx.runtime.managed_artifact_registry import (
    MANAGED_ARTIFACT_MANIFEST_SCHEMA,
    MANAGED_ARTIFACT_REGISTRY_VERSION,
    NO_INSTALLATION_SNAPSHOT_DIGEST,
    NO_MATERIAL_SNAPSHOT_DIGEST,
    ManagedArtifactHandle,
    ManagedArtifactRegistry,
    ManagedArtifactRegistryError,
    open_managed_artifact_registry,
)
from ctx.runtime.composition import _MANAGED_SOURCE_FACTORY_TOKEN


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="managed artifact registry requires POSIX descriptor-relative filesystem primitives",
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _graph_store(tmp_path: Path, name: str = "graph.sqlite3") -> tuple[Path, str]:
    path = tmp_path / name
    graph = nx.Graph()
    graph.add_node(
        "skill:python-test",
        type="skill",
        label="python-test",
        title="Python tests",
        tags=["python", "test"],
    )
    build_graph_store(path, graph)
    path.chmod(0o400)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _surrogate() -> StructuredSurrogate:
    return StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value={
            "active_capability_ids": ["skill:active"],
            "baseline_capability_ids": ["mcp-server:baseline"],
            "languages": ["python"],
            "rejected_capability_ids": ["agent:rejected"],
            "requested_limit": 5,
            "signals": ["pytest", "security"],
        },
    )


def _bindings() -> dict[str, object]:
    return {
        "planning_environment_digest": _digest("environment"),
        "catalog_namespace_digest": _digest("catalog-namespace"),
        "catalog_retrieval_digest": _digest("catalog-retrieval"),
        "benefit_facts_snapshot_digest": _digest("benefit-facts"),
        "benefit_policy_snapshot_digest": _digest("benefit-policy"),
        "material_snapshot_digest": _digest("material"),
        "installation_snapshot_digest": _digest("installation"),
        "observation_surrogate": _surrogate(),
        "planning_schema_version": "ctx.plan.v3",
    }


def _ingest(
    registry: ManagedArtifactRegistry,
    graph_path: Path,
    graph_digest: str,
) -> ManagedArtifactHandle:
    bindings = _bindings()
    return registry.ingest_graph_store(
        graph_store_path=graph_path,
        expected_graph_artifact_digest=graph_digest,
        planning_environment_digest=str(bindings["planning_environment_digest"]),
        catalog_namespace_digest=str(bindings["catalog_namespace_digest"]),
        catalog_retrieval_digest=str(bindings["catalog_retrieval_digest"]),
        benefit_facts_snapshot_digest=str(bindings["benefit_facts_snapshot_digest"]),
        benefit_policy_snapshot_digest=str(bindings["benefit_policy_snapshot_digest"]),
        material_snapshot_digest=str(bindings["material_snapshot_digest"]),
        installation_snapshot_digest=str(bindings["installation_snapshot_digest"]),
        observation_surrogate=_surrogate(),
        planning_schema_version=str(bindings["planning_schema_version"]),
    )


def test_ingest_reopens_exact_artifact_and_typed_observation_without_planning(
    tmp_path: Path,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)

    handle = _ingest(registry, graph_path, graph_digest)
    reopened = registry.reopen(
        manifest_digest=handle.manifest_digest,
        planning_environment_digest=handle.planning_environment_digest,
    )

    assert reopened.manifest_digest == handle.manifest_digest
    assert reopened.graph_artifact_digest == graph_digest
    assert reopened.observation_surrogate_digest == _surrogate().value_digest
    assert reopened.planning_schema_version == "ctx.plan.v3"
    reference = reopened.observation_reference
    assert isinstance(reference, ObservationReference)
    assert reference.content_digest == _surrogate().value_digest
    assert reopened(reference, None) == _surrogate()
    with pytest.raises(ManagedArtifactRegistryError, match="reference"):
        reopened(
            ObservationReference(
                provider_id=reference.provider_id,
                opaque_id="substituted",
                content_digest=reference.content_digest,
            ),
            None,
        )

    assert not hasattr(registry, "open_graph_store")

    manifest_path = root / "manifests" / f"{handle.manifest_digest}.json"
    manifest_body = manifest_path.read_bytes()
    manifest = json.loads(manifest_body)
    assert hashlib.sha256(manifest_body).hexdigest() == handle.manifest_digest
    assert manifest["schema"] == MANAGED_ARTIFACT_MANIFEST_SCHEMA
    assert manifest["registry_version"] == MANAGED_ARTIFACT_REGISTRY_VERSION
    assert manifest["observation_surrogate"] == _surrogate().to_dict()
    assert set(manifest["observation_surrogate"]["value"]) == {
        "active_capability_ids",
        "baseline_capability_ids",
        "languages",
        "rejected_capability_ids",
        "requested_limit",
        "signals",
    }
    serialized = manifest_body.decode("utf-8")
    for forbidden in (str(tmp_path), "prompt", "credential", "source_code"):
        assert forbidden not in serialized
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "artifacts").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "manifests").stat().st_mode) == 0o700
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o400
    assert stat.S_IMODE((root / "artifacts" / f"{graph_digest}.sqlite3").stat().st_mode) == 0o400


def test_api_is_factory_issued_immutable_process_bound_and_path_free(tmp_path: Path) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    registry = open_managed_artifact_registry(root=tmp_path / "private-registry")
    handle = _ingest(registry, graph_path, graph_digest)

    with pytest.raises(TypeError, match="factory-issued"):
        ManagedArtifactRegistry()
    with pytest.raises(TypeError, match="factory-issued"):
        ManagedArtifactHandle()
    for value in (registry, handle):
        with pytest.raises(AttributeError, match="immutable"):
            value.extra = "bad"  # type: ignore[attr-defined]
        with pytest.raises(TypeError, match="copied"):
            copy.copy(value)
        with pytest.raises(TypeError, match="serialized"):
            pickle.dumps(value)
        assert str(tmp_path) not in repr(value)
        assert str(graph_path) not in repr(value)
    with pytest.raises(AttributeError, match="immutable"):
        del registry._root  # type: ignore[attr-defined]
    with pytest.raises(AttributeError, match="immutable"):
        del handle.manifest_digest


@pytest.mark.parametrize("source_kind", ["symlink", "hardlink", "writable", "wrong-digest"])
def test_ingest_rejects_unauthenticated_source_forms(
    tmp_path: Path,
    source_kind: str,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    candidate = graph_path
    supplied_digest = graph_digest
    if source_kind == "symlink":
        candidate = tmp_path / "linked.sqlite3"
        candidate.symlink_to(graph_path)
    elif source_kind == "hardlink":
        candidate = tmp_path / "linked.sqlite3"
        os.link(graph_path, candidate)
    elif source_kind == "writable":
        graph_path.chmod(0o600)
    else:
        supplied_digest = _digest("wrong")
    registry = open_managed_artifact_registry(root=tmp_path / "registry")

    with pytest.raises(ManagedArtifactRegistryError):
        _ingest(registry, candidate, supplied_digest)
    assert not tuple((tmp_path / "registry" / "artifacts").glob("*.stage"))


def test_concurrent_identical_ingest_is_idempotent(tmp_path: Path) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"

    def ingest() -> tuple[str, str]:
        handle = _ingest(open_managed_artifact_registry(root=root), graph_path, graph_digest)
        return handle.manifest_digest, handle.graph_artifact_digest

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = tuple(pool.map(lambda _index: ingest(), range(16)))

    assert len(set(results)) == 1
    assert tuple((root / "artifacts").glob("*.sqlite3")) == (
        root / "artifacts" / f"{graph_digest}.sqlite3",
    )
    assert len(tuple((root / "manifests").glob("*.json"))) == 1


@pytest.mark.parametrize("hostile_kind", ["corrupt", "hardlink", "symlink", "replacement"])
def test_reopen_fails_closed_for_hostile_persisted_artifact(
    tmp_path: Path,
    hostile_kind: str,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)
    handle = _ingest(registry, graph_path, graph_digest)
    artifact = root / "artifacts" / f"{graph_digest}.sqlite3"
    if hostile_kind == "corrupt":
        artifact.chmod(0o600)
        body = bytearray(artifact.read_bytes())
        body[-1] ^= 1
        artifact.write_bytes(body)
        artifact.chmod(0o400)
    elif hostile_kind == "hardlink":
        os.link(artifact, root / "artifact-alias")
    elif hostile_kind == "symlink":
        artifact.unlink()
        artifact.symlink_to(graph_path)
    else:
        replacement = root / "replacement.sqlite3"
        replacement.write_bytes(artifact.read_bytes())
        replacement.chmod(0o400)
        os.replace(replacement, artifact)

    with pytest.raises(ManagedArtifactRegistryError):
        registry.reopen(
            manifest_digest=handle.manifest_digest,
            planning_environment_digest=handle.planning_environment_digest,
        )


def test_reopen_rejects_missing_artifact_wrong_identity_and_unknown_manifest_fields(
    tmp_path: Path,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)
    handle = _ingest(registry, graph_path, graph_digest)

    with pytest.raises(ManagedArtifactRegistryError, match="environment"):
        registry.reopen(
            manifest_digest=handle.manifest_digest,
            planning_environment_digest=_digest("substituted-environment"),
        )
    with pytest.raises(ManagedArtifactRegistryError):
        registry.reopen(
            manifest_digest=_digest("missing-manifest"),
            planning_environment_digest=handle.planning_environment_digest,
        )

    manifest_path = root / "manifests" / f"{handle.manifest_digest}.json"
    hostile = json.loads(manifest_path.read_bytes())
    hostile["prompt"] = "not-allowed"
    hostile_body = json.dumps(
        hostile,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    hostile_digest = hashlib.sha256(hostile_body).hexdigest()
    hostile_path = root / "manifests" / f"{hostile_digest}.json"
    hostile_path.write_bytes(hostile_body)
    hostile_path.chmod(0o400)
    with pytest.raises(ManagedArtifactRegistryError, match="unknown"):
        registry.reopen(
            manifest_digest=hostile_digest,
            planning_environment_digest=handle.planning_environment_digest,
        )

    artifact = root / "artifacts" / f"{graph_digest}.sqlite3"
    artifact.unlink()
    with pytest.raises(ManagedArtifactRegistryError):
        registry.reopen(
            manifest_digest=handle.manifest_digest,
            planning_environment_digest=handle.planning_environment_digest,
        )


def test_ingest_rejects_non_graph_sqlite_without_publishing_a_content_address(
    tmp_path: Path,
) -> None:
    source = tmp_path / "not-graph.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")
    source.chmod(0o400)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)

    with pytest.raises(ManagedArtifactRegistryError, match="graph store"):
        _ingest(registry, source, digest)

    assert not (root / "artifacts" / f"{digest}.sqlite3").exists()
    assert not tuple((root / "artifacts").glob("*.stage"))


def test_graph_schema_validation_cannot_be_redirected_by_stage_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_path, _good_digest = _graph_store(tmp_path, "good.sqlite3")
    invalid_path, _invalid_digest = _graph_store(tmp_path, "invalid.sqlite3")
    invalid_path.chmod(0o600)
    with sqlite3.connect(invalid_path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("DROP INDEX idx_edges_source")
    invalid_path.chmod(0o400)
    invalid_digest = hashlib.sha256(invalid_path.read_bytes()).hexdigest()
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)
    real_connect = sqlite3.connect
    swap_attempted = False

    def swapping_connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal swap_attempted
        stages = tuple((root / "artifacts").glob(".artifact-*.stage"))
        if not stages:
            return real_connect(database, *args, **kwargs)
        swap_attempted = True
        stage = stages[0]
        displaced = root / "displaced-invalid.sqlite3"
        os.replace(stage, displaced)
        shutil.copyfile(good_path, stage)
        stage.chmod(0o400)
        try:
            return real_connect(database, *args, **kwargs)
        finally:
            stage.unlink()
            os.replace(displaced, stage)

    monkeypatch.setattr(registry_module.sqlite3, "connect", swapping_connect)

    with pytest.raises(ManagedArtifactRegistryError, match="retrieval index"):
        _ingest(registry, invalid_path, invalid_digest)

    assert swap_attempted is True
    assert not (root / "artifacts" / f"{invalid_digest}.sqlite3").exists()
    assert not tuple((root / "artifacts").glob("*.stage"))


@pytest.mark.parametrize("kind", ["artifact", "manifest"])
def test_reopen_recovers_exact_publication_link_left_by_crash(
    tmp_path: Path,
    kind: str,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)
    handle = _ingest(registry, graph_path, graph_digest)
    if kind == "artifact":
        final = root / "artifacts" / f"{graph_digest}.sqlite3"
        stage = root / "artifacts" / f".artifact-{graph_digest}-{'a' * 32}.stage"
    else:
        final = root / "manifests" / f"{handle.manifest_digest}.json"
        stage = root / "manifests" / f".manifest-{handle.manifest_digest}-{'b' * 32}.stage"
    os.link(final, stage)
    assert final.stat().st_nlink == 2

    restarted = open_managed_artifact_registry(root=root)
    recovered = restarted.reopen(
        manifest_digest=handle.manifest_digest,
        planning_environment_digest=handle.planning_environment_digest,
    )

    assert recovered.manifest_digest == handle.manifest_digest
    assert final.stat().st_nlink == 1
    assert not stage.exists()


@pytest.mark.parametrize("kind", ["artifact", "manifest"])
def test_startup_removes_private_unpublished_stage_left_by_crash(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "registry"
    open_managed_artifact_registry(root=root)
    digest = _digest(f"unpublished-{kind}")
    directory = root / ("artifacts" if kind == "artifact" else "manifests")
    stage = directory / f".{kind}-{digest}-{'d' * 32}.stage"
    stage.write_bytes(b"partial")
    stage.chmod(0o600)

    open_managed_artifact_registry(root=root)

    assert not stage.exists()


def test_recovery_does_not_accept_mismatched_stage_as_a_publication_link(
    tmp_path: Path,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)
    handle = _ingest(registry, graph_path, graph_digest)
    artifact = root / "artifacts" / f"{graph_digest}.sqlite3"
    hostile = root / "artifacts" / f".artifact-{'f' * 64}-{'c' * 32}.stage"
    os.link(artifact, hostile)

    with pytest.raises(ManagedArtifactRegistryError):
        open_managed_artifact_registry(root=root)
    assert artifact.stat().st_nlink == 2
    assert hostile.exists()
    del handle


def test_registry_exposes_no_raw_path_revealing_sqlite_connection(
    tmp_path: Path,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    registry = open_managed_artifact_registry(root=tmp_path / "registry")
    handle = _ingest(registry, graph_path, graph_digest)

    assert not hasattr(registry, "open_graph_store")
    assert all(not isinstance(getattr(handle, slot), Path) for slot in handle.__slots__)


def test_pathless_indexed_source_requires_issuing_registry_and_exact_absence_snapshots(
    tmp_path: Path,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"
    first = open_managed_artifact_registry(root=root)
    bindings = _bindings()
    with IndexedGraphCandidateSource(graph_path, graph_digest) as probe:
        bindings["catalog_retrieval_digest"] = probe.catalog_snapshot_digest
    handle = first.ingest_graph_store(
        graph_store_path=graph_path,
        expected_graph_artifact_digest=graph_digest,
        planning_environment_digest=str(bindings["planning_environment_digest"]),
        catalog_namespace_digest=str(bindings["catalog_namespace_digest"]),
        catalog_retrieval_digest=str(bindings["catalog_retrieval_digest"]),
        benefit_facts_snapshot_digest=str(bindings["benefit_facts_snapshot_digest"]),
        benefit_policy_snapshot_digest=str(bindings["benefit_policy_snapshot_digest"]),
        material_snapshot_digest=NO_MATERIAL_SNAPSHOT_DIGEST,
        installation_snapshot_digest=NO_INSTALLATION_SNAPSHOT_DIGEST,
        observation_surrogate=_surrogate(),
        planning_schema_version=str(bindings["planning_schema_version"]),
    )
    second = open_managed_artifact_registry(root=root)

    assert not hasattr(second, "open_indexed_source")
    with pytest.raises(ManagedArtifactRegistryError, match="not issued"):
        second._open_indexed_source_for_composition(
            handle,
            factory_token=_MANAGED_SOURCE_FACTORY_TOKEN,
        )
    reopened = second.reopen(
        manifest_digest=handle.manifest_digest,
        planning_environment_digest=handle.planning_environment_digest,
    )
    with second._open_indexed_source_for_composition(
        reopened,
        factory_token=_MANAGED_SOURCE_FACTORY_TOKEN,
    ) as source:
        assert not tuple((root / "artifacts").glob(".ctx-indexed-snapshot-*"))
        candidates = source.retrieve(WorkObservation(signals=("python",), languages=("python",)))
        assert tuple(candidate.capability_id for candidate in candidates) == ("skill:python-test",)


def test_indexed_source_factory_rejects_untrusted_callers_and_port_snapshot_races(
    tmp_path: Path,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    material_digest = _digest("stable-material")

    class StableMaterialPort:
        material_snapshot_digest = material_digest

        def describe(self, _capability_id: str, _kind: str) -> None:
            return None

    stable = StableMaterialPort()
    with IndexedGraphCandidateSource(
        graph_path,
        graph_digest,
        material_port=stable,  # type: ignore[arg-type]
    ) as probe:
        retrieval_digest = probe.catalog_snapshot_digest
    bindings = _bindings()
    registry = open_managed_artifact_registry(root=tmp_path / "registry")
    handle = registry.ingest_graph_store(
        graph_store_path=graph_path,
        expected_graph_artifact_digest=graph_digest,
        planning_environment_digest=str(bindings["planning_environment_digest"]),
        catalog_namespace_digest=str(bindings["catalog_namespace_digest"]),
        catalog_retrieval_digest=retrieval_digest,
        benefit_facts_snapshot_digest=str(bindings["benefit_facts_snapshot_digest"]),
        benefit_policy_snapshot_digest=str(bindings["benefit_policy_snapshot_digest"]),
        material_snapshot_digest=material_digest,
        installation_snapshot_digest=NO_INSTALLATION_SNAPSHOT_DIGEST,
        observation_surrogate=_surrogate(),
        planning_schema_version=str(bindings["planning_schema_version"]),
    )

    with pytest.raises(ManagedArtifactRegistryError, match="trusted composition"):
        registry._open_indexed_source_for_composition(
            handle,
            factory_token=object(),
            material_port=stable,  # type: ignore[arg-type]
        )

    class DriftingMaterialPort:
        reads = 0

        @property
        def material_snapshot_digest(self) -> str:
            self.reads += 1
            return material_digest if self.reads == 1 else _digest("substituted-material")

        def describe(self, _capability_id: str, _kind: str) -> None:
            return None

    with pytest.raises(ManagedArtifactRegistryError, match="changed"):
        registry._open_indexed_source_for_composition(
            handle,
            factory_token=_MANAGED_SOURCE_FACTORY_TOKEN,
            material_port=DriftingMaterialPort(),  # type: ignore[arg-type]
        )
    assert not tuple((tmp_path / "registry" / "artifacts").glob(".ctx-indexed-snapshot-*"))


def test_indexed_source_crash_exit_leaves_no_persistent_snapshot_copy(
    tmp_path: Path,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    with IndexedGraphCandidateSource(graph_path, graph_digest) as probe:
        retrieval_digest = probe.catalog_snapshot_digest
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)
    bindings = _bindings()
    handle = registry.ingest_graph_store(
        graph_store_path=graph_path,
        expected_graph_artifact_digest=graph_digest,
        planning_environment_digest=str(bindings["planning_environment_digest"]),
        catalog_namespace_digest=str(bindings["catalog_namespace_digest"]),
        catalog_retrieval_digest=retrieval_digest,
        benefit_facts_snapshot_digest=str(bindings["benefit_facts_snapshot_digest"]),
        benefit_policy_snapshot_digest=str(bindings["benefit_policy_snapshot_digest"]),
        material_snapshot_digest=NO_MATERIAL_SNAPSHOT_DIGEST,
        installation_snapshot_digest=NO_INSTALLATION_SNAPSHOT_DIGEST,
        observation_surrogate=_surrogate(),
        planning_schema_version=str(bindings["planning_schema_version"]),
    )
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - parent performs the assertions
        os.close(read_fd)
        try:
            child_registry = open_managed_artifact_registry(root=root)
            child_handle = child_registry.reopen(
                manifest_digest=handle.manifest_digest,
                planning_environment_digest=handle.planning_environment_digest,
            )
            source = child_registry._open_indexed_source_for_composition(
                child_handle,
                factory_token=_MANAGED_SOURCE_FACTORY_TOKEN,
            )
            source.retrieve(WorkObservation(signals=("python",), languages=("python",)))
            if tuple((root / "artifacts").glob(".ctx-indexed-snapshot-*")):
                raise AssertionError("managed source left a persistent snapshot namespace")
            os.write(write_fd, b"ok")
            os._exit(0)
        except BaseException as exc:
            os.write(write_fd, f"{type(exc).__name__}:{exc}".encode("utf-8")[:2048])
            os._exit(1)

    os.close(write_fd)
    result = os.read(read_fd, 2048)
    os.close(read_fd)
    waited_pid, status = os.waitpid(child_pid, 0)

    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0, result.decode("utf-8")
    assert result == b"ok"
    assert not tuple((root / "artifacts").glob(".ctx-indexed-snapshot-*"))


def test_registry_and_handle_reject_actual_post_fork_use(tmp_path: Path) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    registry = open_managed_artifact_registry(root=tmp_path / "registry")
    handle = _ingest(registry, graph_path, graph_digest)
    read_fd, write_fd = os.pipe()
    fork = getattr(os, "fork")
    child_pid = fork()
    if child_pid == 0:  # pragma: no cover - assertions execute in the parent
        os.close(read_fd)
        outcomes: list[str] = []
        try:
            registry.reopen(
                manifest_digest=handle.manifest_digest,
                planning_environment_digest=handle.planning_environment_digest,
            )
        except ManagedArtifactRegistryError:
            outcomes.append("registry-rejected")
        else:
            outcomes.append("registry-accepted")
        try:
            handle(handle.observation_reference, None)
        except ManagedArtifactRegistryError:
            outcomes.append("handle-rejected")
        else:
            outcomes.append("handle-accepted")
        os.write(write_fd, ",".join(outcomes).encode("ascii"))
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        result = os.read(read_fd, 256)
    finally:
        os.close(read_fd)
    waited_pid, status = os.waitpid(child_pid, 0)
    assert waited_pid == child_pid
    assert os.waitstatus_to_exitcode(status) == 0
    assert result == b"registry-rejected,handle-rejected"


@pytest.mark.parametrize("defect", ["missing-index", "wrong-node-count", "wrong-edge-count"])
def test_ingest_enforces_the_indexed_candidate_source_graph_prerequisites(
    tmp_path: Path,
    defect: str,
) -> None:
    graph_path, _graph_digest = _graph_store(tmp_path)
    graph_path.chmod(0o600)
    with sqlite3.connect(graph_path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        if defect == "missing-index":
            connection.execute("DROP INDEX idx_edges_source")
        else:
            metadata_key = "node_count" if defect == "wrong-node-count" else "edge_count"
            connection.execute(
                "UPDATE metadata SET value = '999' WHERE key = ?",
                (metadata_key,),
            )
    graph_path.chmod(0o400)
    graph_digest = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)

    expected_error = "retrieval index" if defect == "missing-index" else "count metadata"
    with pytest.raises(ManagedArtifactRegistryError, match=expected_error):
        _ingest(registry, graph_path, graph_digest)
    assert not (root / "artifacts" / f"{graph_digest}.sqlite3").exists()


def test_fresh_registry_directory_entries_are_parent_fsynced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fsync = os.fsync
    synced_directories: set[tuple[int, int]] = set()

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.add((metadata.st_dev, metadata.st_ino))
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    root = tmp_path / "registry"
    open_managed_artifact_registry(root=root)

    parent_metadata = tmp_path.stat()
    root_metadata = root.stat()
    assert (parent_metadata.st_dev, parent_metadata.st_ino) in synced_directories
    assert (root_metadata.st_dev, root_metadata.st_ino) in synced_directories
    assert (root / "artifacts").is_dir()
    assert (root / "manifests").is_dir()


def test_nested_registry_creation_fsyncs_every_new_parent_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fsync = os.fsync
    synced_directories: set[tuple[int, int]] = set()

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced_directories.add((metadata.st_dev, metadata.st_ino))
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)
    first = tmp_path / "first"
    second = first / "second"
    root = second / "registry"
    open_managed_artifact_registry(root=root)

    for parent in (tmp_path, first, second, root):
        metadata = parent.stat()
        assert (metadata.st_dev, metadata.st_ino) in synced_directories


@pytest.mark.parametrize(
    "field_name,sensitive_token",
    [
        ("signals", "sk-abcdefghijklmnopqrstuvwxyz0123456789"),
        ("languages", "abcdefghijklmnopqrstuvwxyz0123456789abcdef"),
        ("active_capability_ids", "skill:api-key"),
    ],
)
def test_ingest_privacy_gate_rejects_credential_like_observation_tokens(
    tmp_path: Path,
    field_name: str,
    sensitive_token: str,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    root = tmp_path / "registry"
    registry = open_managed_artifact_registry(root=root)
    value = _surrogate().to_dict()["value"]
    assert isinstance(value, dict)
    value[field_name] = [sensitive_token]
    surrogate = StructuredSurrogate.create(
        schema_id="ctx.observation.current-work",
        schema_version=1,
        value=value,
    )
    bindings = _bindings()

    with pytest.raises(ManagedArtifactRegistryError, match="credential-like"):
        registry.ingest_graph_store(
            graph_store_path=graph_path,
            expected_graph_artifact_digest=graph_digest,
            planning_environment_digest=str(bindings["planning_environment_digest"]),
            catalog_namespace_digest=str(bindings["catalog_namespace_digest"]),
            catalog_retrieval_digest=str(bindings["catalog_retrieval_digest"]),
            benefit_facts_snapshot_digest=str(bindings["benefit_facts_snapshot_digest"]),
            benefit_policy_snapshot_digest=str(bindings["benefit_policy_snapshot_digest"]),
            material_snapshot_digest=str(bindings["material_snapshot_digest"]),
            installation_snapshot_digest=str(bindings["installation_snapshot_digest"]),
            observation_surrogate=surrogate,
            planning_schema_version=str(bindings["planning_schema_version"]),
        )
    assert not tuple((root / "manifests").glob("*.json"))


@pytest.mark.parametrize(
    "surrogate",
    [
        StructuredSurrogate.create(
            schema_id="ctx.observation.current-work",
            schema_version=1,
            value={
                "active_capability_ids": [],
                "baseline_capability_ids": [],
                "languages": [],
                "rejected_capability_ids": [],
                "requested_limit": 5,
                "signals": [],
                "unknown": [],
            },
        ),
        StructuredSurrogate.create(
            schema_id="ctx.observation.other",
            schema_version=1,
            value={
                "active_capability_ids": [],
                "baseline_capability_ids": [],
                "languages": [],
                "rejected_capability_ids": [],
                "requested_limit": 5,
                "signals": [],
            },
        ),
    ],
)
def test_ingest_rejects_noncanonical_observation_surrogate(
    tmp_path: Path,
    surrogate: StructuredSurrogate,
) -> None:
    graph_path, graph_digest = _graph_store(tmp_path)
    registry = open_managed_artifact_registry(root=tmp_path / "registry")
    bindings = _bindings()
    bindings["observation_surrogate"] = surrogate

    with pytest.raises(ManagedArtifactRegistryError, match="current-work"):
        registry.ingest_graph_store(
            graph_store_path=graph_path,
            expected_graph_artifact_digest=graph_digest,
            planning_environment_digest=str(bindings["planning_environment_digest"]),
            catalog_namespace_digest=str(bindings["catalog_namespace_digest"]),
            catalog_retrieval_digest=str(bindings["catalog_retrieval_digest"]),
            benefit_facts_snapshot_digest=str(bindings["benefit_facts_snapshot_digest"]),
            benefit_policy_snapshot_digest=str(bindings["benefit_policy_snapshot_digest"]),
            material_snapshot_digest=str(bindings["material_snapshot_digest"]),
            installation_snapshot_digest=str(bindings["installation_snapshot_digest"]),
            observation_surrogate=surrogate,
            planning_schema_version=str(bindings["planning_schema_version"]),
        )
