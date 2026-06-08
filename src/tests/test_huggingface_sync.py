"""Tests for Hugging Face sync README metadata handling."""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_huggingface  # noqa: E402


class _FakeRepoInfo:
    sha = "abc1234"


class _FakeCommitInfo:
    commit_url = "https://huggingface.co/datasets/Stevesolun/ctx/commit/fallback"


class _FakeHfApi:
    def __init__(self, remote_files: list[str]) -> None:
        self.remote_files = remote_files
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_repo_files(self, **kwargs: object) -> list[str]:
        self.calls.append(("list_repo_files", kwargs))
        return self.remote_files

    def upload_large_folder(self, **kwargs: object) -> None:
        self.calls.append(("upload_large_folder", kwargs))

    def upload_folder(self, **kwargs: object) -> _FakeCommitInfo:
        self.calls.append(("upload_folder", kwargs))
        return _FakeCommitInfo()

    def repo_info(self, **kwargs: object) -> _FakeRepoInfo:
        self.calls.append(("repo_info", kwargs))
        return _FakeRepoInfo()


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
    assert "lfs: true" in text
    assert "scripts/sync_huggingface.py" in text
    assert "Set the HF_TOKEN repository secret" in text
    assert "hf_" not in text


def test_hf_upload_prefers_large_folder_when_remote_has_no_stale_paths(
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
        repo_type="dataset",
        head="abcdef1234567890",
        prefer_large_upload=True,
    )

    assert url == "https://huggingface.co/datasets/Stevesolun/ctx/commit/abc1234"
    assert [call[0] for call in api.calls] == [
        "list_repo_files",
        "upload_large_folder",
        "repo_info",
    ]
    large_upload = api.calls[1][1]
    assert large_upload["repo_id"] == "Stevesolun/ctx"
    assert large_upload["repo_type"] == "dataset"
    assert large_upload["folder_path"] == str(export_dir)
    assert large_upload["print_report"] is True


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
        repo_type="dataset",
        head="abcdef1234567890",
        prefer_large_upload=True,
    )

    assert url == "https://huggingface.co/datasets/Stevesolun/ctx/commit/fallback"
    assert [call[0] for call in api.calls] == ["list_repo_files", "upload_folder"]
    clean_upload = api.calls[1][1]
    assert clean_upload["delete_patterns"] == "*"
    assert clean_upload["commit_message"] == "Sync ctx abcdef1"


def test_hf_export_copies_hydrated_artifacts_even_when_untracked(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    export_dir = tmp_path / "export"
    repo.mkdir()
    (repo / "graph").mkdir()
    (repo / "README.md").write_text("# ctx\n", encoding="utf-8")
    (repo / "ignored-report.md").write_text("local only\n", encoding="utf-8")
    (repo / "graph" / "wiki-graph.tar.gz").write_bytes(b"\x1f\x8bhydrated-wiki")
    (repo / "graph" / "skills-sh-catalog.json.gz").write_bytes(
        b"\x1f\x8bhydrated-catalog"
    )
    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        {
            Path("graph/wiki-graph.tar.gz"): 4,
            Path("graph/skills-sh-catalog.json.gz"): 4,
        },
    )
    monkeypatch.setattr(
        sync_huggingface,
        "_git_bytes",
        lambda _repo, *_args: (
            b"README.md\0"
            b"graph/skills-sh-catalog.json.gz\0"
        ),
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
    assert not (export_dir / "ignored-report.md").exists()


def test_hf_export_rejects_lfs_pointer_artifact(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    export_dir = tmp_path / "export"
    repo.mkdir()
    (repo / "graph").mkdir()
    (repo / "README.md").write_text("# ctx\n", encoding="utf-8")
    (repo / "graph" / "wiki-graph.tar.gz").write_bytes(
        sync_huggingface.LFS_POINTER_PREFIX + b"\nsize 350608878\n"
    )
    (repo / "graph" / "skills-sh-catalog.json.gz").write_bytes(
        b"\x1f\x8bhydrated-catalog"
    )
    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        {
            Path("graph/wiki-graph.tar.gz"): 4,
            Path("graph/skills-sh-catalog.json.gz"): 4,
        },
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


def test_hf_export_rejects_corrupt_large_graph_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    graph_dir = repo / "graph"
    graph_dir.mkdir()
    (graph_dir / "wiki-graph.tar.gz").write_bytes(b"\x1f\x8bnot-a-valid-tar")
    with gzip.open(graph_dir / "skills-sh-catalog.json.gz", "wt", encoding="utf-8") as f:
        json.dump({"skills": []}, f)
    (graph_dir / "communities.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        sync_huggingface,
        "HYDRATED_ARTIFACT_MIN_BYTES",
        {
            Path("graph/wiki-graph.tar.gz"): 4,
            Path("graph/skills-sh-catalog.json.gz"): 4,
        },
    )

    try:
        sync_huggingface._assert_hydrated_artifacts(repo)
    except RuntimeError as exc:
        assert "graph artifact integrity validation failed" in str(exc)
    else:
        raise AssertionError("expected corrupt graph artifact rejection")
