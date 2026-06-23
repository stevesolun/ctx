"""Tests for Hugging Face sync README metadata handling."""

from __future__ import annotations

import hashlib
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_huggingface  # noqa: E402


def _required_hydrated_artifacts() -> tuple[Path, ...]:
    return tuple(sync_huggingface.HYDRATED_ARTIFACT_MIN_BYTES)


def _write_small_hydrated_artifacts(repo: Path) -> None:
    for rel in _required_hydrated_artifacts():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x1f\x8b" + rel.name.encode("utf-8"))


def _tiny_hydrated_min_bytes() -> dict[Path, int]:
    return {rel: 4 for rel in _required_hydrated_artifacts()}


def _flag_values(args: tuple[str, ...] | list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("--"):
            i += 1
            continue
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            values[token] = args[i + 1]
            i += 2
        else:
            values[token] = ""
            i += 1
    return values


def test_hf_hydrated_artifact_min_size_contract() -> None:
    assert sync_huggingface.HYDRATED_ARTIFACT_MIN_BYTES == {
        Path("graph/wiki-graph.tar.gz"): 100_000_000,
        Path("graph/wiki-graph-runtime.tar.gz"): 10_000_000,
        Path("graph/skills-sh-catalog.json.gz"): 1_000_000,
    }


class _FakeRepoInfo:
    sha = "abc1234"


class _FakeCommitInfo:
    commit_url = "https://huggingface.co/Stevesolun/ctx/commit/fallback"


class _FakeHfApi:
    def __init__(self, remote_files: list[str]) -> None:
        self.remote_files = remote_files
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_repo_files(self, **kwargs: object) -> list[str]:
        self.calls.append(("list_repo_files", kwargs))
        return self.remote_files

    def upload_folder(self, **kwargs: object) -> _FakeCommitInfo:
        self.calls.append(("upload_folder", kwargs))
        return _FakeCommitInfo()

    def upload_file(self, **kwargs: object) -> _FakeCommitInfo:
        path = kwargs.get("path_or_fileobj")
        if isinstance(path, str):
            kwargs = {**kwargs, "content": Path(path).read_text(encoding="utf-8")}
        self.calls.append(("upload_file", kwargs))
        return _FakeCommitInfo()

    def repo_info(self, **kwargs: object) -> _FakeRepoInfo:
        self.calls.append(("repo_info", kwargs))
        return _FakeRepoInfo()


class _FakeHttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"{status_code} synthetic HF error")
        self.response = type("Response", (), {"status_code": status_code})()


class _FakeRepoCreateApi:
    def __init__(self, repo_info_statuses: list[int | None], create_status: int | None = None) -> None:
        self.repo_info_statuses = repo_info_statuses
        self.create_status = create_status
        self.calls: list[tuple[str, dict[str, object]]] = []

    def repo_info(self, **kwargs: object) -> _FakeRepoInfo:
        self.calls.append(("repo_info", kwargs))
        status = self.repo_info_statuses.pop(0)
        if status is not None:
            raise _FakeHttpError(status)
        return _FakeRepoInfo()

    def create_repo(self, repo_id: str, **kwargs: object) -> None:
        self.calls.append(("create_repo", {"repo_id": repo_id, **kwargs}))
        if self.create_status is not None:
            raise _FakeHttpError(self.create_status)


def test_committed_readme_does_not_start_with_hf_frontmatter() -> None:
    readme = Path(__file__).resolve().parents[2] / "README.md"

    assert not readme.read_text(encoding="utf-8").startswith("---\n")


def test_hf_metadata_is_added_to_exported_readme() -> None:
    rendered = sync_huggingface.with_hf_repo_card_metadata("# ctx\n")

    assert rendered.startswith("---\nlicense: mit\n")
    assert "pretty_name: ctx" in rendered
    assert rendered.endswith("# ctx\n")


def test_hf_metadata_replaces_existing_leading_frontmatter() -> None:
    rendered = sync_huggingface.with_hf_repo_card_metadata(
        "---\nold: value\n---\n\n# ctx\n"
    )

    assert "old: value" not in rendered
    assert rendered.count("license: mit") == 1
    assert rendered.endswith("# ctx\n")


def test_hf_publish_docs_use_hardened_sync_script_without_inline_token() -> None:
    docs = (Path(__file__).resolve().parents[2] / "docs" / "huggingface-publish.md")
    text = docs.read_text(encoding="utf-8")

    assert "scripts/sync_huggingface.py" in text
    assert '$env:HF_TOKEN = "<' not in text
    assert "api.upload_folder" not in text
    assert "Read-Host \"HF write token\"" in text


def test_hf_sync_workflow_uses_secret_and_hardened_script() -> None:
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "huggingface-sync.yml"
    )
    text = workflow.read_text(encoding="utf-8")

    assert "HF_TOKEN: ${{ secrets.HF_TOKEN }}" in text
    assert "lfs: false" in text
    assert "git lfs pull" not in text
    assert "Resolving graph artifacts from matching release assets" in text
    assert 'tag_name.startswith("graph-artifacts-")' in text
    assert "latest_tag" not in text
    for artifact in _required_hydrated_artifacts():
        assert artifact.as_posix() in text
        assert f'hydrate_from_release("{artifact.as_posix()}"' in text
    assert "scripts/sync_huggingface.py" in text
    assert "Classify sync scope" in text
    assert "card_only_files" in text
    assert 'SYNC_MODE" == "card"' in text
    assert "--card-only" in text
    assert "timeout-minutes: 60" in text
    assert "Set the HF_TOKEN repository secret" in text
    assert "hf_" not in text


def test_hf_sync_skips_repo_create_when_repo_exists() -> None:
    api = _FakeRepoCreateApi(repo_info_statuses=[None])

    sync_huggingface._ensure_hf_repo_exists(
        api=api,
        repo_id="Stevesolun/ctx",
        repo_type="model",
    )

    assert [call[0] for call in api.calls] == ["repo_info"]


def test_hf_sync_tolerates_create_rate_limit_when_repo_exists_after_retry() -> None:
    api = _FakeRepoCreateApi(repo_info_statuses=[404, None], create_status=429)

    sync_huggingface._ensure_hf_repo_exists(
        api=api,
        repo_id="Stevesolun/ctx",
        repo_type="model",
    )

    assert [call[0] for call in api.calls] == [
        "repo_info",
        "create_repo",
        "repo_info",
    ]


def test_hf_upload_uses_clean_folder_commit(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "README.md").write_text("# ctx\n", encoding="utf-8")
    (export_dir / "graph").mkdir()
    (export_dir / "graph" / "wiki-graph.tar.gz").write_bytes(b"gz")
    api = _FakeHfApi(remote_files=["README.md"])

    url = sync_huggingface._upload_export(
        api=api,
        export_dir=export_dir,
        repo_id="Stevesolun/ctx",
        repo_type="model",
        head="abcdef1234567890",
    )

    assert url == "https://huggingface.co/Stevesolun/ctx/commit/fallback"
    assert [call[0] for call in api.calls] == ["upload_folder"]
    upload = api.calls[0][1]
    assert upload["repo_id"] == "Stevesolun/ctx"
    assert upload["repo_type"] == "model"
    assert upload["folder_path"] == str(export_dir)
    assert upload["delete_patterns"] == "*"
    assert upload["commit_message"] == "Sync ctx abcdef1"


def test_hf_upload_falls_back_to_clean_upload_when_remote_has_stale_paths(
    tmp_path: Path,
) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "README.md").write_text("# ctx\n", encoding="utf-8")
    api = _FakeHfApi(remote_files=["README.md", "old-report.md"])

    url = sync_huggingface._upload_export(
        api=api,
        export_dir=export_dir,
        repo_id="Stevesolun/ctx",
        repo_type="model",
        head="abcdef1234567890",
    )

    assert url == "https://huggingface.co/Stevesolun/ctx/commit/fallback"
    assert [call[0] for call in api.calls] == ["upload_folder"]
    clean_upload = api.calls[0][1]
    assert clean_upload["delete_patterns"] == "*"
    assert clean_upload["commit_message"] == "Sync ctx abcdef1"


def test_hf_card_upload_only_patches_readme(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# ctx\n\nbody\n", encoding="utf-8")
    api = _FakeHfApi(remote_files=[])

    url = sync_huggingface._upload_readme_card(
        api=api,
        repo=repo,
        repo_id="Stevesolun/ctx",
        repo_type="model",
        head="abcdef1234567890",
    )

    assert url == "https://huggingface.co/Stevesolun/ctx/commit/fallback"
    assert [call[0] for call in api.calls] == ["upload_file"]
    upload = api.calls[0][1]
    assert upload["repo_id"] == "Stevesolun/ctx"
    assert upload["repo_type"] == "model"
    assert upload["path_in_repo"] == "README.md"
    assert upload["commit_message"] == "Sync ctx card abcdef1"
    rendered = str(upload["content"])
    assert rendered.startswith("---\nlicense: mit\n")
    assert rendered.endswith("# ctx\n\nbody\n")


def test_hf_export_copies_hydrated_artifacts_even_when_untracked(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    export_dir = tmp_path / "export"
    repo.mkdir()
    (repo / "graph").mkdir()
    (repo / "README.md").write_text("# ctx\n", encoding="utf-8")
    (repo / "ignored-report.md").write_text("local only\n", encoding="utf-8")
    _write_small_hydrated_artifacts(repo)
    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        _tiny_hydrated_min_bytes(),
    )
    monkeypatch.setattr(
        sync_huggingface,
        "_git_bytes",
        lambda _repo, *_args: b"README.md\0",
    )
    monkeypatch.setattr(
        sync_huggingface,
        "_validate_graph_artifact_integrity",
        lambda _repo: None,
    )
    monkeypatch.setattr(sync_huggingface, "_assert_repo_stats_current", lambda _repo: None)

    sync_huggingface._export_tracked_tree(repo, export_dir)

    assert (export_dir / "README.md").read_text(encoding="utf-8") == "# ctx\n"
    assert (export_dir / "graph" / "wiki-graph.tar.gz").read_bytes().startswith(
        b"\x1f\x8b"
    )
    assert (
        export_dir / "graph" / "wiki-graph-runtime.tar.gz"
    ).read_bytes().startswith(b"\x1f\x8b")
    assert (
        export_dir / "graph" / "skills-sh-catalog.json.gz"
    ).read_bytes().startswith(b"\x1f\x8b")
    assert not (export_dir / "ignored-report.md").exists()


def test_hf_export_rejects_lfs_pointer_artifact(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    export_dir = tmp_path / "export"
    repo.mkdir()
    (repo / "graph").mkdir()
    (repo / "README.md").write_text("# ctx\n", encoding="utf-8")
    _write_small_hydrated_artifacts(repo)
    (repo / "graph" / "wiki-graph.tar.gz").write_bytes(
        sync_huggingface.LFS_POINTER_PREFIX + b"\nsize 350608878\n"
    )
    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        _tiny_hydrated_min_bytes(),
    )
    monkeypatch.setattr(sync_huggingface, "_assert_repo_stats_current", lambda _repo: None)

    try:
        sync_huggingface._export_tracked_tree(repo, export_dir)
    except RuntimeError as exc:
        assert "Git LFS pointer" in str(exc)
    else:
        raise AssertionError("expected LFS pointer rejection")


def test_hf_export_checks_repo_stats_before_upload(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    updater = repo / "src" / "update_repo_stats.py"
    updater.parent.mkdir()
    updater.write_text("print('ok')\n", encoding="utf-8")
    calls: list[tuple[list[str], Path]] = []

    def fake_run(cmd: list[str], *, cwd: Path, check: bool) -> object:
        calls.append((cmd, cwd))
        assert check is True
        return object()

    monkeypatch.setattr(sync_huggingface.subprocess, "run", fake_run)

    sync_huggingface._assert_repo_stats_current(repo)

    assert calls == [([sys.executable, str(updater), "--check"], repo)]


def test_hf_graph_validation_uses_ci_exact_count_contract(
    tmp_path: Path, monkeypatch
) -> None:
    from ci_preflight import GRAPH_VALIDATE_ARGS

    repo = tmp_path / "repo"
    repo.mkdir()
    _write_small_hydrated_artifacts(repo)
    seen: dict[str, object] = {}

    def fake_validator(graph_dir: Path, **kwargs: object) -> object:
        seen["graph_dir"] = graph_dir
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        _tiny_hydrated_min_bytes(),
    )
    monkeypatch.setattr(
        sync_huggingface,
        "_load_graph_artifact_validator",
        lambda _repo: fake_validator,
    )

    sync_huggingface._assert_hydrated_artifacts(repo)

    flags = _flag_values(GRAPH_VALIDATE_ARGS[1:])
    kwargs = seen["kwargs"]
    assert seen["graph_dir"] == repo / "graph"
    assert isinstance(kwargs, dict)
    assert kwargs["deep"] is True
    for flag, field_name in sync_huggingface.GRAPH_VALIDATOR_INT_FLAGS.items():
        assert kwargs[field_name] == int(flags[flag])


def test_hf_graph_validation_rejects_stale_exact_counts(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_small_hydrated_artifacts(repo)

    def fake_validator(_graph_dir: Path, **_kwargs: object) -> object:
        raise ValueError("graph_nodes exact count mismatch: expected 102928, got 102927")

    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        _tiny_hydrated_min_bytes(),
    )
    monkeypatch.setattr(
        sync_huggingface,
        "_load_graph_artifact_validator",
        lambda _repo: fake_validator,
    )

    try:
        sync_huggingface._assert_hydrated_artifacts(repo)
    except RuntimeError as exc:
        assert "graph artifact integrity validation failed" in str(exc)
        assert "graph_nodes exact count mismatch" in str(exc)
    else:
        raise AssertionError("expected stale graph artifact rejection")


def test_hf_export_requires_hydrated_artifacts_to_match_lfs_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    graph_dir = repo / "graph"
    graph_dir.mkdir()
    _write_small_hydrated_artifacts(repo)
    artifact = graph_dir / "wiki-graph.tar.gz"
    expected = b"\x1f\x8bcurrent-full-graph"
    artifact.write_bytes(expected)
    expected_oid = hashlib.sha256(expected).hexdigest()
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        f"oid sha256:{expected_oid}\n"
        f"size {len(expected)}\n"
    )

    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        _tiny_hydrated_min_bytes(),
    )
    def fake_git_bytes(_repo: Path, *_args: str) -> bytes:
        if _args[-1] == "HEAD:graph/wiki-graph.tar.gz":
            return pointer.encode("utf-8")
        raise sync_huggingface.subprocess.CalledProcessError(1, list(_args))

    monkeypatch.setattr(sync_huggingface, "_git_bytes", fake_git_bytes)
    monkeypatch.setattr(
        sync_huggingface,
        "_validate_graph_artifact_integrity",
        lambda _repo: None,
    )

    sync_huggingface._assert_hydrated_artifacts(repo)

    artifact.write_bytes(b"\x1f\x8bstale-full-graph")
    try:
        sync_huggingface._assert_hydrated_artifacts(repo)
    except RuntimeError as exc:
        assert "does not match HEAD LFS pointer" in str(exc)
    else:
        raise AssertionError("expected stale LFS asset rejection")


def test_hf_export_skips_lfs_pointer_check_for_binary_git_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    graph_dir = repo / "graph"
    graph_dir.mkdir()
    _write_small_hydrated_artifacts(repo)
    artifact = graph_dir / "wiki-graph.tar.gz"
    artifact.write_bytes(b"\x1f\x8bcurrent-full-graph")

    def fake_git_bytes(_repo: Path, *_args: str) -> bytes:
        if _args[-1] == "HEAD:graph/wiki-graph.tar.gz":
            return b"\x1f\x8bcommitted-binary-graph"
        raise sync_huggingface.subprocess.CalledProcessError(1, list(_args))

    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        _tiny_hydrated_min_bytes(),
    )
    monkeypatch.setattr(sync_huggingface, "_git_bytes", fake_git_bytes)
    monkeypatch.setattr(
        sync_huggingface,
        "_validate_graph_artifact_integrity",
        lambda _repo: None,
    )

    sync_huggingface._assert_hydrated_artifacts(repo)


def test_hf_export_rejects_corrupt_large_graph_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    graph_dir = repo / "graph"
    graph_dir.mkdir()
    _write_small_hydrated_artifacts(repo)
    (graph_dir / "wiki-graph.tar.gz").write_bytes(b"\x1f\x8bnot-a-valid-tar")

    def fake_validator(graph_path: Path, **_kwargs: object) -> object:
        try:
            with tarfile.open(graph_path / "wiki-graph.tar.gz", "r:gz"):
                return object()
        except tarfile.TarError as exc:
            raise ValueError(f"wiki-graph.tar.gz corrupt: {exc}") from exc

    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        _tiny_hydrated_min_bytes(),
    )
    monkeypatch.setattr(
        sync_huggingface,
        "_load_graph_artifact_validator",
        lambda _repo: fake_validator,
    )
    monkeypatch.setattr(
        sync_huggingface,
        "_git",
        lambda *_args: (_ for _ in ()).throw(
            sync_huggingface.subprocess.CalledProcessError(1, list(_args))
        ),
    )

    try:
        sync_huggingface._assert_hydrated_artifacts(repo)
    except RuntimeError as exc:
        assert "graph artifact integrity validation failed" in str(exc)
        assert "wiki-graph.tar.gz" in str(exc)
    else:
        raise AssertionError("expected corrupt graph artifact rejection")
