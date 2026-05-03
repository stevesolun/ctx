#!/usr/bin/env python3
"""Sync the current git tree to Hugging Face with HF-only card metadata."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tarfile
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


def _export_git_tree(repo: Path, commit: str, export_dir: Path) -> None:
    tar_path = export_dir.parent / "ctx.tar"
    subprocess.run(
        ["git", "archive", "--format=tar", "-o", str(tar_path), commit],
        cwd=repo,
        check=True,
    )
    _extract_regular_members(tar_path, export_dir)


def _extract_regular_members(tar_path: Path, export_dir: Path) -> None:
    export_root = export_dir.resolve()
    with tarfile.open(tar_path) as archive:
        for member in archive.getmembers():
            target = (export_root / member.name).resolve()
            if target != export_root and not target.is_relative_to(export_root):
                raise ValueError(f"unsafe tar member path: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as dest:
                shutil.copyfileobj(source, dest)


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
        _export_git_tree(repo, head, export_dir)
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
