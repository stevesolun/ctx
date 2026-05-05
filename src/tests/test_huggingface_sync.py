"""Tests for Hugging Face sync README metadata handling."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sync_huggingface  # noqa: E402


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


def test_hf_export_copies_hydrated_tracked_artifacts(
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
            b"graph/wiki-graph.tar.gz\0"
            b"graph/skills-sh-catalog.json.gz\0"
        ),
    )

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

    try:
        sync_huggingface._export_tracked_tree(repo, export_dir)
    except RuntimeError as exc:
        assert "Git LFS pointer" in str(exc)
    else:
        raise AssertionError("expected LFS pointer rejection")
