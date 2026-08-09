"""Retrieval-only graph adapter for the unified capability planner.

The adapter accepts an already loaded graph and returns a widened, typed pool.
It owns no final selection, host policy, rendering, filesystem, or semantic work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import networkx as nx

from ctx.core.resolve.recommendations import (
    recommend_by_tags,
    recommend_by_tags_indexed_snapshot,
)
from ctx.engine.content import CapabilityMaterialPort, MaterialDescriptor
from ctx.engine.installation import (
    CapabilityInstallPlanPort,
    InstallPlanDescriptor,
)
from ctx.engine.planner import (
    CandidateAuthorityUnavailable,
    CandidateSourceUnavailable,
    CapabilityCandidate,
    PlannerValidationError,
    WorkObservation,
)


DEFAULT_CANDIDATE_LIMIT = 50
MAX_CANDIDATE_LIMIT = 512
_ENTITY_TYPES = ("skill", "agent", "mcp-server", "harness")
_SAFE_TOKEN_RE = re.compile(r"\A[a-z0-9][a-z0-9._:@-]{0,127}\Z")
_SAFE_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9._@-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_NAME_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ONE_MILLION = Decimal(1_000_000)
_GraphNode = tuple[str, str, str, tuple[str, ...]]
_GraphEdge = tuple[str, str]
_GRAPH_STORE_SCHEMA_VERSION = "1"
_INDEXED_ADAPTER_VERSION = "ctx.indexed-candidate-source-v4"
_CANDIDATE_PROJECTION_VERSION = "ctx.catalog-entry.graph-v4"
_MALFORMED_AUTHORITY_OUTPUT = object()
_REQUIRED_TABLE_COLUMNS = {
    "edges": frozenset({"source", "target", "weight", "attrs_json"}),
    "metadata": frozenset({"key", "value"}),
    "nodes": frozenset({"id", "type", "label", "title", "tags_json", "attrs_json", "search_text"}),
}
_REQUIRED_INDEXES = frozenset(
    {"idx_edges_source", "idx_edges_target", "idx_nodes_search_text", "idx_nodes_type"}
)


def _catalog_snapshot(
    graph: Any,
) -> tuple[tuple[_GraphNode, ...], tuple[_GraphEdge, ...], str]:
    """Return immutable safe graph facts and their canonical digest."""

    try:
        copied = graph.copy()
        metadata = copied.graph
    except Exception:
        raise CandidateSourceUnavailable("graph candidate source is unavailable") from None
    if (
        copied is graph
        or not isinstance(metadata, dict)
        or bool(copied.is_directed())
        or bool(copied.is_multigraph())
    ):
        raise CandidateSourceUnavailable("graph candidate source is unavailable")

    rejected_nodes: list[Any] = []
    for node_id, raw_data in tuple(copied.nodes(data=True)):
        if not isinstance(node_id, str) or _SAFE_TOKEN_RE.fullmatch(node_id) is None:
            rejected_nodes.append(node_id)
            continue
        if not isinstance(raw_data, Mapping):
            rejected_nodes.append(node_id)
            continue
        kind = raw_data.get("type")
        name = raw_data.get("label")
        tags = _safe_tags(raw_data.get("tags"))
        if (
            not isinstance(kind, str)
            or kind not in _ENTITY_TYPES
            or not isinstance(name, str)
            or _SAFE_NAME_RE.fullmatch(name) is None
            or tags is None
            or _truthy(raw_data.get("never_load"))
        ):
            rejected_nodes.append(node_id)
            continue
        node_data = copied.nodes[node_id]
        node_data.clear()
        node_data.update({"label": name, "tags": list(tags), "type": kind})
    copied.remove_nodes_from(rejected_nodes)
    for _left, _right, edge_data in copied.edges(data=True):
        edge_data.clear()
    nodes = tuple(
        sorted(
            (
                node_id,
                data["type"],
                data["label"],
                tuple(data["tags"]),
            )
            for node_id, data in copied.nodes(data=True)
        )
    )
    edges = tuple(sorted(tuple(sorted((left, right))) for left, right in copied.edges()))
    return nodes, edges, _snapshot_digest(nodes, edges)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _safe_tags(value: object) -> tuple[str, ...] | None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        return None
    if len(value) > 100:
        return None
    tags: list[str] = []
    for item in value:
        if not isinstance(item, str) or _SAFE_TOKEN_RE.fullmatch(item) is None:
            return None
        tags.append(item)
    unique = tuple(sorted(set(tags)))
    return unique if len(unique) == len(tags) else None


def _snapshot_digest(
    nodes: tuple[_GraphNode, ...],
    edges: tuple[_GraphEdge, ...],
) -> str:
    frozen = json.dumps(
        {
            "edges": edges,
            "nodes": nodes,
            "schema": "ctx.catalog-snapshot.graph-v1",
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(frozen.encode("utf-8")).hexdigest()


def _build_retrieval_graph(
    nodes: tuple[_GraphNode, ...],
    edges: tuple[_GraphEdge, ...],
) -> Any:
    graph = nx.Graph()
    graph.add_nodes_from(
        (
            node_id,
            {"label": name, "tags": list(tags), "type": kind},
        )
        for node_id, kind, name, tags in nodes
    )
    graph.add_edges_from(edges)
    graph.graph["external_catalog_nodes"] = {"skills.sh": 1}
    return nx.freeze(graph)


def _score_ppm(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not score.is_finite() or not Decimal(0) <= score <= Decimal(1):
        return None
    return int((score * _ONE_MILLION).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _source_digest(
    *,
    capability_id: str,
    kind: str,
    name: str,
    tags: tuple[str, ...],
    actionability: str,
    install_descriptor_digest: str | None = None,
    install_plan_digest: str | None = None,
    material_descriptor_digest: str | None = None,
) -> str:
    stable = json.dumps(
        {
            "capability_id": capability_id,
            "actionability": actionability,
            "kind": kind,
            "install_descriptor_digest": install_descriptor_digest,
            "install_plan_digest": install_plan_digest,
            "material_descriptor_digest": material_descriptor_digest,
            "name": name,
            "schema": "ctx.catalog-entry.graph-v4",
            "tags": list(tags),
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _candidate_from_row(
    row: object,
    observation: WorkObservation,
    *,
    typed_match_reasons: bool = False,
    install_descriptor: InstallPlanDescriptor | None = None,
    material_descriptor: MaterialDescriptor | None = None,
) -> CapabilityCandidate | None:
    if not isinstance(row, Mapping):
        return None
    kind = row.get("type")
    name = row.get("name")
    if not isinstance(kind, str) or kind not in _ENTITY_TYPES or not isinstance(name, str):
        return None
    tags = _safe_tags(row.get("tags"))
    score_ppm = _score_ppm(row.get("normalized_score"))
    if tags is None or score_ppm is None:
        return None

    work_signals = {*observation.signals, *observation.languages}
    tag_matches = work_signals.intersection(tags)
    name_matches = work_signals.intersection(_NAME_TOKEN_RE.findall(name))
    matching_signals = tuple(sorted(tag_matches | name_matches))
    reason_codes = {"graph-match"}
    if typed_match_reasons and name_matches:
        reason_codes.add("name-match")
    if typed_match_reasons and tag_matches:
        reason_codes.add("tag-match")
    if set(matching_signals).intersection(observation.languages):
        reason_codes.add("language-match")
    if set(matching_signals).intersection(observation.signals):
        reason_codes.add("signal-match")
    capability_id = f"{kind}:{name}"
    if material_descriptor is not None:
        if material_descriptor.capability_id != capability_id or material_descriptor.kind != kind:
            return None
    if install_descriptor is not None:
        if install_descriptor.capability_id != capability_id or install_descriptor.kind != kind:
            return None
    if material_descriptor is not None and material_descriptor.actionability == "load":
        actionability = "load"
        install_descriptor_digest = None
        install_plan_digest = None
    elif install_descriptor is not None:
        actionability = "install"
        install_descriptor_digest = install_descriptor.descriptor_digest
        install_plan_digest = install_descriptor.plan_digest
    else:
        actionability = "manual"
        install_descriptor_digest = None
        install_plan_digest = None
    try:
        return CapabilityCandidate(
            capability_id=capability_id,
            kind=kind,
            name=name,
            source_digest=_source_digest(
                capability_id=capability_id,
                kind=kind,
                name=name,
                tags=tags,
                actionability=actionability,
                install_descriptor_digest=(
                    None if install_descriptor is None else install_descriptor.descriptor_digest
                ),
                install_plan_digest=install_plan_digest,
                material_descriptor_digest=(
                    None if material_descriptor is None else material_descriptor.descriptor_digest
                ),
            ),
            normalized_score_ppm=score_ppm,
            matching_signals=matching_signals,
            reason_codes=tuple(sorted(reason_codes)),
            actionability=actionability,
            install_descriptor_digest=install_descriptor_digest,
            install_plan_digest=install_plan_digest,
        )
    except PlannerValidationError:
        return None


def _candidate_order(candidate: CapabilityCandidate) -> tuple[int, str, str]:
    return (
        -candidate.normalized_score_ppm,
        candidate.capability_id,
        candidate.source_digest,
    )


def _remove_ambiguous_candidates(
    values: Sequence[CapabilityCandidate],
) -> tuple[CapabilityCandidate, ...]:
    by_identity: dict[str, list[CapabilityCandidate]] = {}
    for candidate in values:
        by_identity.setdefault(candidate.capability_id, []).append(candidate)

    unambiguous: list[CapabilityCandidate] = []
    for capability_id in sorted(by_identity):
        candidates = by_identity[capability_id]
        if len({candidate.source_digest for candidate in candidates}) != 1:
            continue
        unambiguous.append(min(candidates, key=_candidate_order))
    return tuple(sorted(unambiguous, key=_candidate_order))


@dataclass(frozen=True, slots=True)
class GraphCandidateSource:
    """Adapt the existing graph scorer to the engine's candidate-source port."""

    graph: InitVar[Any]
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT
    catalog_snapshot_digest: str = field(init=False)
    _nodes: tuple[_GraphNode, ...] = field(init=False, repr=False)
    _edges: tuple[_GraphEdge, ...] = field(init=False, repr=False)

    def __post_init__(self, graph: Any) -> None:
        if (
            type(self.candidate_limit) is not int
            or not 6 <= self.candidate_limit <= MAX_CANDIDATE_LIMIT
        ):
            raise ValueError("candidate_limit must be an integer from 6 through 512")
        if not callable(getattr(graph, "copy", None)):
            raise TypeError("graph must be an already loaded graph value")
        nodes, edges, snapshot_digest = _catalog_snapshot(graph)
        object.__setattr__(self, "_nodes", nodes)
        object.__setattr__(self, "_edges", edges)
        object.__setattr__(self, "catalog_snapshot_digest", snapshot_digest)

    def retrieve(self, observation: WorkObservation) -> tuple[CapabilityCandidate, ...]:
        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        tags = sorted({*observation.signals, *observation.languages})
        if not tags:
            return ()
        graph = _build_retrieval_graph(self._nodes, self._edges)
        try:
            rows = recommend_by_tags(
                graph,
                tags,
                top_n=self.candidate_limit,
                query=" ".join(tags),
                entity_types=_ENTITY_TYPES,
                min_normalized_score=0.0,
                use_semantic_query=False,
            )
        except Exception:
            raise CandidateSourceUnavailable("graph candidate retrieval failed") from None
        if not isinstance(rows, list) or len(rows) > self.candidate_limit:
            raise CandidateSourceUnavailable("graph candidate retrieval returned an invalid pool")
        candidates = tuple(
            candidate
            for row in rows
            if (candidate := _candidate_from_row(row, observation)) is not None
        )
        return _remove_ambiguous_candidates(candidates)


def _unavailable() -> CandidateSourceUnavailable:
    return CandidateSourceUnavailable("indexed graph candidate source is unavailable")


def _file_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _store_sidecars(store_path: Path) -> tuple[Path, Path]:
    return (Path(f"{store_path}-wal"), Path(f"{store_path}-shm"))


def _has_store_sidecar(store_path: Path) -> bool:
    return any(os.path.lexists(sidecar) for sidecar in _store_sidecars(store_path))


def _copy_authenticated_artifact(
    artifact_fd: int,
    snapshot_path: Path,
    expected_artifact_sha256: str,
) -> None:
    digest = hashlib.sha256()
    os.lseek(artifact_fd, 0, os.SEEK_SET)
    with snapshot_path.open("xb") as snapshot:
        while chunk := os.read(artifact_fd, 1024 * 1024):
            snapshot.write(chunk)
            digest.update(chunk)
        snapshot.flush()
        os.fsync(snapshot.fileno())
    os.lseek(artifact_fd, 0, os.SEEK_SET)
    snapshot_path.chmod(stat.S_IRUSR)
    if digest.hexdigest() != expected_artifact_sha256:
        raise _unavailable()


def _snapshot_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    integrity = conn.execute("PRAGMA quick_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        raise ValueError("graph store failed integrity validation")

    table_rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {str(row[0]) for row in table_rows}
    if not _REQUIRED_TABLE_COLUMNS.keys() <= tables:
        raise ValueError("graph store tables are incomplete")
    for table_name, expected_columns in _REQUIRED_TABLE_COLUMNS.items():
        columns = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if not expected_columns <= columns:
            raise ValueError("graph store columns are incomplete")

    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    if not _REQUIRED_INDEXES <= indexes:
        raise ValueError("graph store indexes are incomplete")

    metadata = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT key, value FROM metadata").fetchall()
    }
    if metadata.get("schema_version") != _GRAPH_STORE_SCHEMA_VERSION:
        raise ValueError("graph store schema is unsupported")
    for metadata_key, table_name in (("node_count", "nodes"), ("edge_count", "edges")):
        expected_count = int(metadata[metadata_key])
        actual_count = int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
        if expected_count != actual_count:
            raise ValueError("graph store count metadata is invalid")
    return metadata


def _indexed_snapshot_digest(
    artifact_digest: str,
    graph_schema_version: str,
    material_snapshot_digest: str | None,
    installation_snapshot_digest: str | None,
) -> str:
    payload = json.dumps(
        {
            "adapter_version": _INDEXED_ADAPTER_VERSION,
            "allowed_entity_types": list(_ENTITY_TYPES),
            "artifact_sha256": artifact_digest,
            "graph_schema_version": graph_schema_version,
            "installation_snapshot_digest": installation_snapshot_digest,
            "material_snapshot_digest": material_snapshot_digest,
            "projection_version": _CANDIDATE_PROJECTION_VERSION,
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _open_authenticated_snapshot(
    store_path: Path,
    expected_artifact_sha256: str,
    *,
    remove_snapshot_namespace_on_open: bool = False,
) -> tuple[sqlite3.Connection, dict[str, str], tempfile.TemporaryDirectory[str]]:
    if (
        not isinstance(expected_artifact_sha256, str)
        or _SHA256_RE.fullmatch(expected_artifact_sha256) is None
    ):
        raise _unavailable()
    artifact_fd = -1
    snapshot_directory: tempfile.TemporaryDirectory[str] | None = None
    try:
        before = store_path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or _has_store_sidecar(store_path)
        ):
            raise _unavailable()
        before_signature = _file_signature(before)
        open_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        artifact_fd = os.open(store_path, open_flags)
        opened = os.fstat(artifact_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or _file_signature(opened) != before_signature
        ):
            raise _unavailable()

        snapshot_parent = store_path.parent.resolve(strict=True)
        try:
            snapshot_directory = tempfile.TemporaryDirectory(
                prefix=".ctx-indexed-snapshot-",
                dir=snapshot_parent,
            )
        except OSError:
            snapshot_directory = tempfile.TemporaryDirectory(prefix="ctx-indexed-snapshot-")
        Path(snapshot_directory.name).chmod(stat.S_IRWXU)
        snapshot_path = Path(snapshot_directory.name) / "graph-store.sqlite3"
        _copy_authenticated_artifact(
            artifact_fd,
            snapshot_path,
            expected_artifact_sha256,
        )
        snapshot_signature = _file_signature(snapshot_path.lstat())
        os.close(artifact_fd)
        artifact_fd = -1

        uri = f"{snapshot_path.as_uri()}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            metadata = _snapshot_metadata(conn)
            if (
                _has_store_sidecar(store_path)
                or _file_signature(snapshot_path.lstat()) != snapshot_signature
            ):
                raise _unavailable()
            if remove_snapshot_namespace_on_open:
                if os.name == "nt":
                    raise _unavailable()
                # SQLite retains its already-open immutable file descriptor on
                # POSIX. Removing the private temporary namespace here keeps
                # crash-only exits from leaving a full graph copy behind.
                snapshot_directory.cleanup()
                if Path(snapshot_directory.name).exists():
                    raise _unavailable()
            return conn, metadata, snapshot_directory
        except Exception:
            conn.close()
            raise
    except CandidateSourceUnavailable:
        if artifact_fd >= 0:
            os.close(artifact_fd)
        if snapshot_directory is not None:
            snapshot_directory.cleanup()
        raise
    except (KeyError, OSError, sqlite3.Error, TypeError, ValueError):
        if artifact_fd >= 0:
            os.close(artifact_fd)
        if snapshot_directory is not None:
            snapshot_directory.cleanup()
        raise _unavailable() from None


class IndexedGraphCandidateSource:
    """Serve candidates from one authenticated, pinned SQLite artifact."""

    __slots__ = (
        "_candidate_limit",
        "_catalog_snapshot_digest",
        "_closed",
        "_connection",
        "_descriptor_cache",
        "_install_descriptor_cache",
        "_install_plan_port",
        "_installation_snapshot_digest",
        "_lock",
        "_material_snapshot_digest",
        "_material_port",
        "_snapshot_directory",
    )

    def __init__(
        self,
        store_path: Path,
        expected_artifact_sha256: str,
        *,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        install_plan_port: CapabilityInstallPlanPort | None = None,
        material_port: CapabilityMaterialPort | None = None,
        remove_snapshot_namespace_on_open: bool = False,
    ) -> None:
        if type(candidate_limit) is not int or not 6 <= candidate_limit <= MAX_CANDIDATE_LIMIT:
            raise ValueError("candidate_limit must be an integer from 6 through 512")
        if not isinstance(store_path, Path):
            raise TypeError("store_path must be a Path")
        if type(remove_snapshot_namespace_on_open) is not bool:
            raise TypeError("remove_snapshot_namespace_on_open must be a bool")
        if material_port is not None and (
            not callable(getattr(material_port, "describe", None))
            or not isinstance(getattr(material_port, "material_snapshot_digest", None), str)
            or _SHA256_RE.fullmatch(material_port.material_snapshot_digest) is None
        ):
            raise TypeError("material_port must implement the capability material contract")
        if install_plan_port is not None and (
            not callable(getattr(install_plan_port, "describe", None))
            or not isinstance(
                getattr(install_plan_port, "installation_snapshot_digest", None),
                str,
            )
            or _SHA256_RE.fullmatch(install_plan_port.installation_snapshot_digest) is None
        ):
            raise TypeError("install_plan_port must implement the install plan contract")
        connection, metadata, snapshot_directory = _open_authenticated_snapshot(
            store_path,
            expected_artifact_sha256,
            remove_snapshot_namespace_on_open=remove_snapshot_namespace_on_open,
        )
        self._connection = connection
        self._install_plan_port = install_plan_port
        self._installation_snapshot_digest = (
            None if install_plan_port is None else install_plan_port.installation_snapshot_digest
        )
        self._material_port = material_port
        self._material_snapshot_digest = (
            None if material_port is None else material_port.material_snapshot_digest
        )
        self._descriptor_cache: dict[str, object] = {}
        self._install_descriptor_cache: dict[str, object] = {}
        self._snapshot_directory = snapshot_directory
        self._lock = threading.RLock()
        self._closed = False
        self._candidate_limit = candidate_limit
        self._catalog_snapshot_digest = _indexed_snapshot_digest(
            expected_artifact_sha256,
            metadata["schema_version"],
            None if material_port is None else material_port.material_snapshot_digest,
            (self._installation_snapshot_digest),
        )

    @property
    def candidate_limit(self) -> int:
        return self._candidate_limit

    @property
    def catalog_snapshot_digest(self) -> str:
        return self._catalog_snapshot_digest

    def retrieve(self, observation: WorkObservation) -> tuple[CapabilityCandidate, ...]:
        if not isinstance(observation, WorkObservation):
            raise TypeError("observation must be a WorkObservation")
        tags = sorted({*observation.signals, *observation.languages})
        if not tags:
            return ()
        with self._lock:
            if self._closed:
                raise _unavailable()
            if (
                self._install_plan_port is not None
                and self._install_plan_port.installation_snapshot_digest
                != self._installation_snapshot_digest
            ):
                raise CandidateSourceUnavailable(
                    "indexed install snapshot changed after construction"
                )
            if (
                self._material_port is not None
                and self._material_port.material_snapshot_digest != self._material_snapshot_digest
            ):
                raise CandidateSourceUnavailable(
                    "indexed material snapshot changed after construction"
                )
            try:
                rows, _total_nodes = recommend_by_tags_indexed_snapshot(
                    self._connection,
                    tags,
                    top_n=self.candidate_limit,
                    query=" ".join(tags),
                    entity_types=_ENTITY_TYPES,
                    min_normalized_score=0.0,
                )
            except Exception:
                raise CandidateSourceUnavailable(
                    "indexed graph candidate retrieval failed"
                ) from None
        if not isinstance(rows, list) or len(rows) > self.candidate_limit:
            raise CandidateSourceUnavailable(
                "indexed graph candidate retrieval returned an invalid pool"
            )
        candidates: list[CapabilityCandidate] = []
        for row in rows:
            descriptor = None
            install_descriptor = None
            if self._material_port is not None and isinstance(row, Mapping):
                kind = row.get("type")
                name = row.get("name")
                if isinstance(kind, str) and isinstance(name, str):
                    capability_id = f"{kind}:{name}"
                    observed: object
                    try:
                        observed = self._material_port.describe(capability_id, kind)
                    except CandidateAuthorityUnavailable:
                        observed = _MALFORMED_AUTHORITY_OUTPUT
                    except Exception:
                        raise CandidateSourceUnavailable(
                            "indexed material descriptor retrieval failed"
                        ) from None
                    classified_material: object = (
                        observed
                        if isinstance(observed, MaterialDescriptor)
                        else _MALFORMED_AUTHORITY_OUTPUT
                    )
                    with self._lock:
                        if (
                            capability_id in self._descriptor_cache
                            and self._descriptor_cache[capability_id] != classified_material
                        ):
                            raise CandidateSourceUnavailable(
                                "indexed material descriptor changed within a snapshot"
                            )
                        self._descriptor_cache[capability_id] = classified_material
                    if (
                        not isinstance(observed, MaterialDescriptor)
                        or observed.capability_id != capability_id
                        or observed.kind != kind
                        or observed.provenance_digest != self._material_snapshot_digest
                    ):
                        # Malformed authority is local to this graph row. It is
                        # neither advisory material nor evidence that the
                        # authenticated graph snapshot itself is unavailable.
                        continue
                    descriptor = observed
            if (
                self._install_plan_port is not None
                and isinstance(row, Mapping)
                and not (descriptor is not None and descriptor.actionability == "load")
            ):
                kind = row.get("type")
                name = row.get("name")
                if kind in {"skill", "agent", "mcp-server"} and isinstance(name, str):
                    capability_id = f"{kind}:{name}"
                    observed_install: object
                    try:
                        observed_install = self._install_plan_port.describe(
                            capability_id,
                            kind,
                        )
                    except CandidateAuthorityUnavailable:
                        observed_install = _MALFORMED_AUTHORITY_OUTPUT
                    except Exception:
                        raise CandidateSourceUnavailable(
                            "indexed install descriptor retrieval failed"
                        ) from None
                    classified_install: object = (
                        observed_install
                        if observed_install is None
                        or isinstance(observed_install, InstallPlanDescriptor)
                        else _MALFORMED_AUTHORITY_OUTPUT
                    )
                    with self._lock:
                        if (
                            capability_id in self._install_descriptor_cache
                            and self._install_descriptor_cache[capability_id] != classified_install
                        ):
                            raise CandidateSourceUnavailable(
                                "indexed install descriptor changed within a snapshot"
                            )
                        self._install_descriptor_cache[capability_id] = classified_install
                    if observed_install is _MALFORMED_AUTHORITY_OUTPUT or (
                        observed_install is not None
                        and not isinstance(observed_install, InstallPlanDescriptor)
                    ):
                        continue
                    if observed_install is not None and (
                        observed_install.capability_id != capability_id
                        or observed_install.kind != kind
                        or observed_install.provenance_digest != self._installation_snapshot_digest
                    ):
                        continue
                    install_descriptor = observed_install
            candidate = _candidate_from_row(
                row,
                observation,
                typed_match_reasons=True,
                install_descriptor=install_descriptor,
                material_descriptor=descriptor,
            )
            if candidate is not None:
                candidates.append(candidate)
        return _remove_ambiguous_candidates(candidates)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._snapshot_directory.cleanup()
            self._closed = True

    def __enter__(self) -> IndexedGraphCandidateSource:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


__all__ = [
    "DEFAULT_CANDIDATE_LIMIT",
    "GraphCandidateSource",
    "IndexedGraphCandidateSource",
]
