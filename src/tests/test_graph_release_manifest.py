from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from ctx.core.graph import release_artifacts


def _write_local_assets(root: Path) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    for index, (path, _asset_name, _hydrate) in enumerate(
        release_artifacts.GRAPH_RELEASE_ARTIFACT_SPECS,
        start=1,
    ):
        payload = f"artifact-{index}\n".encode()
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        payloads[path] = payload
    return payloads


def test_refresh_writes_the_exact_five_asset_manifest_deterministically(tmp_path: Path) -> None:
    payloads = _write_local_assets(tmp_path)
    manifest_path = tmp_path / "graph" / "release-artifacts.json"

    assert (
        release_artifacts.main(
            [
                "refresh",
                "--repo-root",
                str(tmp_path),
                "--manifest",
                str(manifest_path),
                "--source-release-tag",
                "v9.8.7",
            ]
        )
        == 0
    )
    first = manifest_path.read_bytes()
    assert (
        release_artifacts.main(
            [
                "refresh",
                "--repo-root",
                str(tmp_path),
                "--manifest",
                str(manifest_path),
                "--source-release-tag",
                "v9.8.7",
            ]
        )
        == 0
    )
    assert manifest_path.read_bytes() == first

    manifest = release_artifacts.load_manifest(manifest_path)
    assert manifest.repository == "stevesolun/ctx"
    assert manifest.source_release_tag == "v9.8.7"
    assert (
        tuple(
            (artifact.path, artifact.asset_name, artifact.hydrate)
            for artifact in manifest.artifacts
        )
        == release_artifacts.GRAPH_RELEASE_ARTIFACT_SPECS
    )
    for artifact in manifest.artifacts:
        assert artifact.size == len(payloads[artifact.path])


def test_manifest_rejects_an_incomplete_artifact_roster(tmp_path: Path) -> None:
    _write_local_assets(tmp_path)
    manifest = release_artifacts.build_manifest(
        repo_root=tmp_path,
        repository="stevesolun/ctx",
        source_release_tag="v1.0.21",
    )
    manifest_path = tmp_path / "graph" / "release-artifacts.json"
    release_artifacts.write_manifest(manifest_path, manifest)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"].pop()
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="exact graph release artifact roster"):
        release_artifacts.load_manifest(manifest_path)


def test_manifest_rejects_a_different_repository(tmp_path: Path) -> None:
    _write_local_assets(tmp_path)
    manifest = release_artifacts.build_manifest(
        repo_root=tmp_path,
        repository="stevesolun/ctx",
        source_release_tag="v1.0.21",
    )
    manifest_path = tmp_path / "graph" / "release-artifacts.json"
    release_artifacts.write_manifest(manifest_path, manifest)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["repository"] = "attacker/fork"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="must be stevesolun/ctx"):
        release_artifacts.load_manifest(manifest_path)


def test_manifest_rejects_an_artifact_above_its_path_cap(tmp_path: Path) -> None:
    _write_local_assets(tmp_path)
    manifest = release_artifacts.build_manifest(
        repo_root=tmp_path,
        repository="stevesolun/ctx",
        source_release_tag="v1.0.21",
    )
    manifest_path = tmp_path / "graph" / "release-artifacts.json"
    release_artifacts.write_manifest(manifest_path, manifest)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][-1]["size"] = 500_000_001
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="size exceeds"):
        release_artifacts.load_manifest(manifest_path)


def test_refresh_rejects_a_local_artifact_above_its_path_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_local_assets(tmp_path)
    real_identity = release_artifacts.file_sha256_and_size

    def oversized_full(path: Path) -> tuple[str, int]:
        if path.name == "wiki-graph.tar.gz":
            return "a" * 64, 500_000_001
        return real_identity(path)

    monkeypatch.setattr(release_artifacts, "file_sha256_and_size", oversized_full)

    with pytest.raises(ValueError, match="exceeds the 500000000-byte cap"):
        release_artifacts.build_manifest(
            repo_root=tmp_path,
            repository="stevesolun/ctx",
            source_release_tag="v1.0.21",
        )


@pytest.mark.parametrize("kind", ["symlink", "directory", "oversize"])
def test_manifest_input_must_be_a_small_regular_file(tmp_path: Path, kind: str) -> None:
    manifest_path = tmp_path / "release-artifacts.json"
    if kind == "symlink":
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        manifest_path.symlink_to(target)
        expected = "not a regular file"
    elif kind == "directory":
        manifest_path.mkdir()
        expected = "not a regular file"
    else:
        manifest_path.write_bytes(b" " * (release_artifacts._MAX_MANIFEST_BYTES + 1))
        expected = "exceeds"

    with pytest.raises(ValueError, match=expected):
        release_artifacts.load_manifest(manifest_path)


def test_manifest_rejects_path_escape_before_accessing_assets(tmp_path: Path) -> None:
    _write_local_assets(tmp_path)
    manifest = release_artifacts.build_manifest(
        repo_root=tmp_path,
        repository="stevesolun/ctx",
        source_release_tag="v1.0.21",
    )
    manifest_path = tmp_path / "graph" / "release-artifacts.json"
    release_artifacts.write_manifest(manifest_path, manifest)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"][0]["path"] = "graph/../outside"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="safe graph/ path"):
        release_artifacts.load_manifest(manifest_path)


def test_validate_rejects_symlinked_local_artifact(tmp_path: Path) -> None:
    _write_local_assets(tmp_path)
    manifest = release_artifacts.build_manifest(
        repo_root=tmp_path,
        repository="stevesolun/ctx",
        source_release_tag="v1.0.21",
    )
    manifest_path = tmp_path / "graph" / "release-artifacts.json"
    release_artifacts.write_manifest(manifest_path, manifest)
    tracked = tmp_path / "graph" / "communities.json"
    target = tmp_path / "outside-communities.json"
    tracked.replace(target)
    try:
        tracked.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="not a regular file"):
        release_artifacts.validate_local_artifacts(
            repo_root=tmp_path,
            manifest_path=manifest_path,
        )


def test_selective_hydration_downloads_only_the_requested_manifest_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _write_local_assets(tmp_path)
    manifest = release_artifacts.build_manifest(
        repo_root=tmp_path,
        repository="stevesolun/ctx",
        source_release_tag="v1.0.21",
    )
    manifest_path = tmp_path / "graph" / "release-artifacts.json"
    release_artifacts.write_manifest(manifest_path, manifest)
    runtime_path = "graph/wiki-graph-runtime.tar.gz"
    full_path = "graph/wiki-graph.tar.gz"
    (tmp_path / runtime_path).unlink()
    (tmp_path / full_path).unlink()
    downloaded: list[str] = []

    def fake_download(
        destination: Path,
        *,
        manifest: release_artifacts.GraphReleaseManifest,
        artifact: release_artifacts.GraphReleaseArtifact,
    ) -> None:
        del manifest
        downloaded.append(artifact.path)
        destination.write_bytes(payloads[artifact.path])

    monkeypatch.setattr(release_artifacts, "_download_artifact", fake_download)

    release_artifacts.hydrate_and_verify(
        repo_root=tmp_path,
        manifest_path=manifest_path,
        artifact_paths=[runtime_path],
    )

    assert downloaded == [runtime_path]
    assert (tmp_path / runtime_path).read_bytes() == payloads[runtime_path]
    assert not (tmp_path / full_path).exists()


def test_cli_refresh_runs_with_the_stdlib_only(tmp_path: Path) -> None:
    _write_local_assets(tmp_path)
    manifest_path = tmp_path / "graph" / "release-artifacts.json"
    script = Path(__file__).resolve().parents[2] / "scripts" / "graph_release_manifest.py"

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(script),
            "refresh",
            "--repo-root",
            str(tmp_path),
            "--manifest",
            str(manifest_path),
            "--source-release-tag",
            "v1.0.21",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert release_artifacts.load_manifest(manifest_path).source_release_tag == "v1.0.21"
