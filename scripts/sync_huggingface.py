#!/usr/bin/env python3
"""Sync the current git tree to Hugging Face with HF-only card metadata."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_REPO_ID = "Stevesolun/ctx"
DEFAULT_REPO_TYPE = "dataset"

HF_CARD_METADATA = """---
license: mit
pretty_name: ctx
tags:
  - agents
  - mcp
  - skills
  - knowledge-graph
  - llm-wiki
  - recommendation-system
  - harness
  - codex
  - claude-code
---

"""

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
HYDRATED_ARTIFACT_MIN_BYTES = {
    Path("graph/wiki-graph.tar.gz"): 100_000_000,
    Path("graph/skills-sh-catalog.json.gz"): 1_000_000,
}


def with_hf_repo_card_metadata(readme_text: str) -> str:
    """Return README text with Hugging Face repo-card metadata prepended."""
    return HF_CARD_METADATA + _strip_leading_yaml_frontmatter(readme_text)


def _strip_leading_yaml_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n") :].lstrip("\n")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def _iter_tracked_files(repo: Path) -> list[Path]:
    output = _git_bytes(repo, "ls-files", "-z")
    files: list[Path] = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        rel = Path(raw.decode("utf-8"))
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe git path: {rel}")
        files.append(rel)
    return files


def _assert_hydrated_artifacts(repo: Path) -> None:
    for rel, min_bytes in HYDRATED_ARTIFACT_MIN_BYTES.items():
        artifact = repo / rel
        if not artifact.is_file():
            raise FileNotFoundError(
                f"{rel.as_posix()} is required before Hugging Face sync"
            )
        size = artifact.stat().st_size
        if size < min_bytes:
            raise RuntimeError(
                f"{rel.as_posix()} is {size:,} bytes; expected at least "
                f"{min_bytes:,}. Run git lfs pull before publishing."
            )
        with artifact.open("rb") as fh:
            prefix = fh.read(len(LFS_POINTER_PREFIX))
        if prefix == LFS_POINTER_PREFIX:
            raise RuntimeError(
                f"{rel.as_posix()} is a Git LFS pointer, not the hydrated artifact"
            )


def _export_tracked_tree(repo: Path, export_dir: Path) -> None:
    _assert_hydrated_artifacts(repo)
    repo_root = repo.resolve()
    export_root = export_dir.resolve()
    for rel in _iter_tracked_files(repo):
        source = (repo_root / rel).resolve()
        if source != repo_root and not source.is_relative_to(repo_root):
            raise ValueError(f"unsafe source path: {rel}")
        if source.is_symlink():
            raise ValueError(f"refusing to follow symlink during HF sync: {rel}")
        if not source.is_file():
            continue
        target = (export_root / rel).resolve()
        if target != export_root and not target.is_relative_to(export_root):
            raise ValueError(f"unsafe export path: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _patch_export_readme(export_dir: Path) -> None:
    readme = export_dir / "README.md"
    readme.write_text(
        with_hf_repo_card_metadata(readme.read_text(encoding="utf-8")),
        encoding="utf-8",
        newline="\n",
    )


def sync_to_huggingface(
    *,
    repo: Path,
    repo_id: str,
    repo_type: str,
    token: str,
) -> str:
    """Upload HEAD to Hugging Face and return the commit URL."""
    from huggingface_hub import HfApi

    head = _git(repo, "rev-parse", "HEAD")
    workspace = Path(tempfile.mkdtemp(prefix="ctx-hf-upload-"))
    export_dir = workspace / "export"
    try:
        export_dir.mkdir()
        _export_tracked_tree(repo, export_dir)
        _patch_export_readme(export_dir)
        api = HfApi(token=token)
        api.create_repo(repo_id, repo_type=repo_type, exist_ok=True)
        info = api.upload_folder(
            repo_id=repo_id,
            repo_type=repo_type,
            folder_path=str(export_dir),
            commit_message=f"Sync ctx {head[:7]}",
            commit_description=f"GitHub commit: {head}",
            delete_patterns="*",
        )
        return str(getattr(info, "commit_url", info))
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Upload this git checkout to Hugging Face with repo-card metadata"
    )
    parser.add_argument("--repo", default=".", help="Git checkout path")
    parser.add_argument(
        "--repo-id",
        default=os.environ.get("HF_REPO_ID", DEFAULT_REPO_ID),
        help="Hugging Face repo ID",
    )
    parser.add_argument(
        "--repo-type",
        default=os.environ.get("HF_REPO_TYPE", DEFAULT_REPO_TYPE),
        help="Hugging Face repo type",
    )
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required")
    print(
        sync_to_huggingface(
            repo=Path(args.repo).resolve(),
            repo_id=args.repo_id,
            repo_type=args.repo_type,
            token=token,
        )
    )


if __name__ == "__main__":
    main()
