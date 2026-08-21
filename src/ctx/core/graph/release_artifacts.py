"""Strict manifest and hydration helpers for public graph release assets."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
import tempfile
import urllib.parse
import urllib.request


_SCHEMA_VERSION = 1
_MANIFEST_FIELDS = frozenset({"schema_version", "repository", "source_release_tag", "artifacts"})
_ARTIFACT_FIELDS = frozenset({"path", "asset_name", "size", "sha256", "hydrate"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REPOSITORY_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")
_RELEASE_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DOWNLOAD_TIMEOUT_SECONDS = 120
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
GRAPH_RELEASE_REPOSITORY = "stevesolun/ctx"

GRAPH_RELEASE_ARTIFACT_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("graph/communities.json", "communities.json", False),
    ("graph/entity-overlays.jsonl", "entity-overlays.jsonl", False),
    ("graph/skills-sh-catalog.json.gz", "skills-sh-catalog.json.gz", False),
    ("graph/wiki-graph-runtime.tar.gz", "wiki-graph-runtime.tar.gz", True),
    ("graph/wiki-graph.tar.gz", "wiki-graph.tar.gz", True),
)
GRAPH_RELEASE_MAX_SIZES = {
    "graph/communities.json": 10_000_000,
    "graph/entity-overlays.jsonl": 5_000_000,
    "graph/skills-sh-catalog.json.gz": 25_000_000,
    "graph/wiki-graph-runtime.tar.gz": 200_000_000,
    "graph/wiki-graph.tar.gz": 500_000_000,
}


@dataclass(frozen=True)
class GraphReleaseArtifact:
    path: str
    asset_name: str
    size: int
    sha256: str
    hydrate: bool


@dataclass(frozen=True)
class GraphReleaseManifest:
    repository: str
    source_release_tag: str
    artifacts: tuple[GraphReleaseArtifact, ...]
    schema_version: int = _SCHEMA_VERSION

    def artifact_for_path(self, path: str) -> GraphReleaseArtifact:
        for artifact in self.artifacts:
            if artifact.path == path:
                return artifact
        raise KeyError(path)


def _expect_exact_fields(
    payload: dict[str, object],
    expected: frozenset[str],
    *,
    context: str,
) -> None:
    actual = frozenset(payload)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise ValueError(f"unknown {context} fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"missing {context} fields: {', '.join(missing)}")


def _validated_repository(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("manifest repository must be an owner/name string")
    parts = value.split("/")
    if len(parts) != 2 or any(
        part in {".", ".."} or _REPOSITORY_SEGMENT_RE.fullmatch(part) is None for part in parts
    ):
        raise ValueError("manifest repository must be a safe owner/name string")
    if value != GRAPH_RELEASE_REPOSITORY:
        raise ValueError(f"manifest repository must be {GRAPH_RELEASE_REPOSITORY}")
    return value


def _validated_release_tag(value: object) -> str:
    if not isinstance(value, str) or _RELEASE_TAG_RE.fullmatch(value) is None:
        raise ValueError("manifest source_release_tag is invalid")
    return value


def _validated_artifact(payload: object, *, index: int) -> GraphReleaseArtifact:
    if not isinstance(payload, dict):
        raise ValueError(f"manifest artifact {index} must be an object")
    _expect_exact_fields(payload, _ARTIFACT_FIELDS, context=f"artifact {index}")

    path = payload["path"]
    asset_name = payload["asset_name"]
    size = payload["size"]
    sha256 = payload["sha256"]
    hydrate = payload["hydrate"]
    if not isinstance(path, str):
        raise ValueError(f"manifest artifact {index} path must be a string")
    pure_path = PurePosixPath(path)
    if (
        pure_path.is_absolute()
        or len(pure_path.parts) < 2
        or pure_path.parts[0] != "graph"
        or any(part in {"", ".", ".."} for part in pure_path.parts)
        or pure_path.as_posix() != path
    ):
        raise ValueError(f"manifest artifact {index} path must be a safe graph/ path")
    if (
        not isinstance(asset_name, str)
        or not asset_name
        or PurePosixPath(asset_name).name != asset_name
    ):
        raise ValueError(f"manifest artifact {index} asset_name must be a basename")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError(f"manifest artifact {index} size must be a positive integer")
    max_size = GRAPH_RELEASE_MAX_SIZES.get(path)
    if max_size is not None and size > max_size:
        raise ValueError(
            f"manifest artifact {index} size exceeds the {max_size}-byte cap for {path}"
        )
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise ValueError(f"manifest artifact {index} sha256 must be lowercase SHA-256 hex")
    if not isinstance(hydrate, bool):
        raise ValueError(f"manifest artifact {index} hydrate must be boolean")
    return GraphReleaseArtifact(path, asset_name, size, sha256, hydrate)


def load_manifest(path: Path) -> GraphReleaseManifest:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"graph release manifest is not a regular file: {path}")
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise ValueError(f"graph release manifest exceeds {_MAX_MANIFEST_BYTES} bytes: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read graph release manifest {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("graph release manifest must be an object")
    _expect_exact_fields(payload, _MANIFEST_FIELDS, context="manifest")
    schema_version = payload["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != _SCHEMA_VERSION
    ):
        raise ValueError(f"unsupported graph release manifest schema: {schema_version!r}")
    raw_artifacts = payload["artifacts"]
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ValueError("manifest artifacts must be a non-empty list")
    artifacts = tuple(
        _validated_artifact(raw, index=index) for index, raw in enumerate(raw_artifacts, start=1)
    )
    paths = [artifact.path for artifact in artifacts]
    names = [artifact.asset_name for artifact in artifacts]
    if len(paths) != len(set(paths)):
        raise ValueError("manifest artifact paths must be unique")
    if len(names) != len(set(names)):
        raise ValueError("manifest asset names must be unique")
    actual_specs = tuple(
        (artifact.path, artifact.asset_name, artifact.hydrate) for artifact in artifacts
    )
    if actual_specs != GRAPH_RELEASE_ARTIFACT_SPECS:
        raise ValueError("manifest artifacts must match the exact graph release artifact roster")
    return GraphReleaseManifest(
        repository=_validated_repository(payload["repository"]),
        source_release_tag=_validated_release_tag(payload["source_release_tag"]),
        artifacts=artifacts,
    )


def release_asset_url(
    manifest: GraphReleaseManifest,
    artifact: GraphReleaseArtifact,
) -> str:
    repository = "/".join(
        urllib.parse.quote(part, safe="") for part in manifest.repository.split("/")
    )
    release_tag = urllib.parse.quote(manifest.source_release_tag, safe="")
    asset_name = urllib.parse.quote(artifact.asset_name, safe="")
    return f"https://github.com/{repository}/releases/download/{release_tag}/{asset_name}"


def file_sha256_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def verify_artifact(path: Path, artifact: GraphReleaseArtifact) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"graph release artifact is not a regular file: {artifact.path}")
    actual_sha256, actual_size = file_sha256_and_size(path)
    if actual_sha256 != artifact.sha256 or actual_size != artifact.size:
        raise ValueError(
            f"graph release artifact mismatch for {artifact.path}: "
            f"expected sha256:{artifact.sha256} size:{artifact.size}; "
            f"got sha256:{actual_sha256} size:{actual_size}"
        )


def _destination(repo_root: Path, artifact: GraphReleaseArtifact) -> Path:
    root = repo_root.resolve()
    destination = repo_root / artifact.path
    parent = destination.parent.resolve()
    try:
        parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"artifact path escapes repository root: {artifact.path}") from exc
    return destination


def _download_artifact(
    destination: Path,
    *,
    manifest: GraphReleaseManifest,
    artifact: GraphReleaseArtifact,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = release_asset_url(manifest, artifact)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".download",
        dir=destination.parent,
    )
    temp_path = Path(temp_name)
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(file_descriptor, "wb") as output:
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310
                while chunk := response.read(_DOWNLOAD_CHUNK_BYTES):
                    size += len(chunk)
                    if size > artifact.size:
                        raise ValueError(
                            f"graph release asset exceeds manifest size for {artifact.path}"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual_sha256 = digest.hexdigest()
        if size != artifact.size or actual_sha256 != artifact.sha256:
            raise ValueError(
                f"downloaded graph release artifact mismatch for {artifact.path}: "
                f"expected sha256:{artifact.sha256} size:{artifact.size}; "
                f"got sha256:{actual_sha256} size:{size}"
            )
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def hydrate_and_verify(
    *,
    repo_root: Path,
    manifest_path: Path | None = None,
    artifact_paths: Iterable[str] | None = None,
) -> GraphReleaseManifest:
    manifest_file = manifest_path or repo_root / "graph" / "release-artifacts.json"
    manifest = load_manifest(manifest_file)
    if artifact_paths is None:
        artifacts = manifest.artifacts
    else:
        requested = tuple(dict.fromkeys(artifact_paths))
        if not requested:
            raise ValueError("selective graph release hydration requires at least one artifact")
        try:
            artifacts = tuple(manifest.artifact_for_path(path) for path in requested)
        except KeyError as exc:
            raise ValueError(
                f"artifact is not in the graph release manifest: {exc.args[0]}"
            ) from exc
    for artifact in artifacts:
        destination = _destination(repo_root, artifact)
        if destination.exists() or destination.is_symlink():
            verify_artifact(destination, artifact)
            continue
        if not artifact.hydrate:
            raise ValueError(f"required tracked graph artifact is missing: {artifact.path}")
        _download_artifact(destination, manifest=manifest, artifact=artifact)
        verify_artifact(destination, artifact)
    return manifest


def build_manifest(
    *,
    repo_root: Path,
    repository: str,
    source_release_tag: str,
) -> GraphReleaseManifest:
    repository = _validated_repository(repository)
    source_release_tag = _validated_release_tag(source_release_tag)
    artifacts: list[GraphReleaseArtifact] = []
    for path, asset_name, hydrate in GRAPH_RELEASE_ARTIFACT_SPECS:
        artifact_path = repo_root / path
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ValueError(f"cannot build manifest; artifact is missing: {path}")
        sha256, size = file_sha256_and_size(artifact_path)
        max_size = GRAPH_RELEASE_MAX_SIZES[path]
        if size > max_size:
            raise ValueError(
                f"cannot build manifest; artifact exceeds the {max_size}-byte cap: {path}"
            )
        artifacts.append(GraphReleaseArtifact(path, asset_name, size, sha256, hydrate))
    return GraphReleaseManifest(repository, source_release_tag, tuple(artifacts))


def write_manifest(path: Path, manifest: GraphReleaseManifest) -> None:
    payload = {
        "schema_version": manifest.schema_version,
        "repository": manifest.repository,
        "source_release_tag": manifest.source_release_tag,
        "artifacts": [
            {
                "path": artifact.path,
                "asset_name": artifact.asset_name,
                "size": artifact.size,
                "sha256": artifact.sha256,
                "hydrate": artifact.hydrate,
            }
            for artifact in manifest.artifacts
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def validate_local_artifacts(
    *,
    repo_root: Path,
    manifest_path: Path | None = None,
) -> GraphReleaseManifest:
    manifest_file = manifest_path or repo_root / "graph" / "release-artifacts.json"
    manifest = load_manifest(manifest_file)
    for artifact in manifest.artifacts:
        destination = _destination(repo_root, artifact)
        if destination.exists() or destination.is_symlink():
            verify_artifact(destination, artifact)
        elif not artifact.hydrate:
            raise ValueError(f"required tracked graph artifact is missing: {artifact.path}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    validate_parser.add_argument("--manifest", type=Path, required=True)
    hydrate_parser = subparsers.add_parser("hydrate")
    hydrate_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    hydrate_parser.add_argument("--manifest", type=Path, required=True)
    hydrate_parser.add_argument(
        "--artifact",
        action="append",
        dest="artifacts",
        help="Hydrate only this exact manifest path. May be repeated.",
    )
    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    refresh_parser.add_argument("--manifest", type=Path, required=True)
    refresh_parser.add_argument("--repository", default=GRAPH_RELEASE_REPOSITORY)
    refresh_parser.add_argument("--source-release-tag", required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        validate_local_artifacts(repo_root=args.repo_root, manifest_path=args.manifest)
    elif args.command == "hydrate":
        hydrate_and_verify(
            repo_root=args.repo_root,
            manifest_path=args.manifest,
            artifact_paths=args.artifacts,
        )
    else:
        manifest = build_manifest(
            repo_root=args.repo_root,
            repository=args.repository,
            source_release_tag=args.source_release_tag,
        )
        write_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
