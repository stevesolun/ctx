#!/usr/bin/env python3
"""Sync the current git tree to Hugging Face with HF-only card metadata."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

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
    Path("graph/wiki-graph-runtime.tar.gz"): 10_000_000,
    Path("graph/skills-sh-catalog.json.gz"): 1_000_000,
}
GRAPH_VALIDATOR_INT_FLAGS = {
    "--min-nodes": "min_nodes",
    "--min-edges": "min_edges",
    "--min-skills-sh-nodes": "min_skills_sh_nodes",
    "--min-semantic-edges": "min_semantic_edges",
    "--expected-nodes": "expected_nodes",
    "--expected-edges": "expected_edges",
    "--expected-semantic-edges": "expected_semantic_edges",
    "--expected-harness-nodes": "expected_harness_nodes",
    "--expected-skills-sh-nodes": "expected_skills_sh_nodes",
    "--expected-skills-sh-catalog-entries": "expected_skills_sh_catalog_entries",
    "--expected-skills-sh-converted": "expected_skills_sh_converted",
    "--expected-skill-pages": "expected_skill_pages",
    "--expected-agent-pages": "expected_agent_pages",
    "--expected-mcp-pages": "expected_mcp_pages",
    "--expected-harness-pages": "expected_harness_pages",
    "--line-threshold": "line_threshold",
    "--max-stage-lines": "max_stage_lines",
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
                f"{min_bytes:,}. Download or rebuild graph release artifacts "
                "before publishing."
            )
        with artifact.open("rb") as fh:
            prefix = fh.read(len(LFS_POINTER_PREFIX))
        if prefix == LFS_POINTER_PREFIX:
            raise RuntimeError(
                f"{rel.as_posix()} is a Git LFS pointer, not the hydrated artifact"
            )
    _validate_graph_artifact_integrity(repo)


def _validate_graph_artifact_integrity(repo: Path) -> None:
    validate_graph_artifacts = _load_graph_artifact_validator(repo)
    try:
        validate_graph_artifacts(repo / "graph", **_graph_validation_kwargs(repo))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "graph artifact integrity validation failed before Hugging Face sync: "
            f"{exc}"
        ) from exc


def _load_graph_artifact_validator(repo: Path) -> Callable[..., object]:
    src_dir = repo / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    from validate_graph_artifacts import validate_graph_artifacts

    return validate_graph_artifacts


def _graph_validation_kwargs(repo: Path) -> dict[str, object]:
    scripts_dir = repo / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from ci_preflight import GRAPH_VALIDATE_ARGS

    args = list(GRAPH_VALIDATE_ARGS[1:])
    kwargs: dict[str, object] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--graph-dir":
            if i + 1 >= len(args):
                raise RuntimeError("GRAPH_VALIDATE_ARGS has --graph-dir without a value")
            if args[i + 1] != "graph":
                raise RuntimeError(
                    "Hugging Face graph validation expects GRAPH_VALIDATE_ARGS "
                    f"to use --graph-dir graph, got {args[i + 1]!r}"
                )
            i += 2
            continue
        if token == "--deep":
            kwargs["deep"] = True
            i += 1
            continue
        field_name = GRAPH_VALIDATOR_INT_FLAGS.get(token)
        if field_name is None:
            raise RuntimeError(
                f"Hugging Face sync does not understand graph validator flag {token!r}"
            )
        if i + 1 >= len(args):
            raise RuntimeError(f"GRAPH_VALIDATE_ARGS has {token} without a value")
        kwargs[field_name] = int(args[i + 1])
        i += 2
    return kwargs


def _assert_repo_stats_current(repo: Path) -> None:
    updater = repo / "src" / "update_repo_stats.py"
    if not updater.is_file():
        raise FileNotFoundError("src/update_repo_stats.py is required before Hugging Face sync")
    subprocess.run(
        [sys.executable, str(updater), "--check"],
        cwd=repo,
        check=True,
    )


def _export_tracked_tree(repo: Path, export_dir: Path) -> None:
    _assert_repo_stats_current(repo)
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
    _copy_hydrated_artifacts(repo_root, export_root)


def _copy_hydrated_artifacts(repo_root: Path, export_root: Path) -> None:
    """Copy required graph artifacts even when they are intentionally untracked."""
    for rel in HYDRATED_ARTIFACT_MIN_BYTES:
        source = (repo_root / rel).resolve()
        if source != repo_root and not source.is_relative_to(repo_root):
            raise ValueError(f"unsafe artifact source path: {rel}")
        target = (export_root / rel).resolve()
        if target != export_root and not target.is_relative_to(export_root):
            raise ValueError(f"unsafe artifact export path: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _patch_export_readme(export_dir: Path) -> None:
    readme = export_dir / "README.md"
    readme.write_text(
        with_hf_repo_card_metadata(readme.read_text(encoding="utf-8")),
        encoding="utf-8",
        newline="\n",
    )


def _iter_export_file_names(export_dir: Path) -> set[str]:
    names: set[str] = set()
    for path in export_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(export_dir).as_posix()
        if rel == ".cache" or rel.startswith(".cache/"):
            continue
        names.add(rel)
    return names


def _repo_url(*, repo_id: str, repo_type: str) -> str:
    prefix = {"dataset": "datasets", "space": "spaces"}.get(repo_type)
    repo_path = f"{prefix}/{repo_id}" if prefix else repo_id
    return f"https://huggingface.co/{repo_path}"


def _repo_commit_url(*, repo_id: str, repo_type: str, sha: str) -> str:
    return f"{_repo_url(repo_id=repo_id, repo_type=repo_type)}/commit/{sha}"


def _hf_status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    if match:
        return int(match.group(1))
    return None


def _ensure_hf_repo_exists(*, api: Any, repo_id: str, repo_type: str) -> None:
    try:
        api.repo_info(repo_id=repo_id, repo_type=repo_type)
        return
    except Exception as info_exc:  # noqa: BLE001
        if _hf_status_code(info_exc) != 404:
            raise

    try:
        api.create_repo(repo_id, repo_type=repo_type, exist_ok=True)
    except Exception as create_exc:  # noqa: BLE001
        if _hf_status_code(create_exc) == 429:
            api.repo_info(repo_id=repo_id, repo_type=repo_type)
            return
        raise


def _remote_has_no_stale_paths(
    *,
    api: Any,
    export_dir: Path,
    repo_id: str,
    repo_type: str,
) -> bool:
    try:
        remote_files = set(api.list_repo_files(repo_id=repo_id, repo_type=repo_type))
    except Exception:
        return False
    return remote_files <= _iter_export_file_names(export_dir)


def _upload_export(
    *,
    api: Any,
    export_dir: Path,
    repo_id: str,
    repo_type: str,
    head: str,
    prefer_large_upload: bool,
) -> str:
    if (
        prefer_large_upload
        and hasattr(api, "upload_large_folder")
        and _remote_has_no_stale_paths(
            api=api,
            export_dir=export_dir,
            repo_id=repo_id,
            repo_type=repo_type,
        )
    ):
        api.upload_large_folder(
            repo_id=repo_id,
            repo_type=repo_type,
            folder_path=str(export_dir),
            print_report=True,
        )
        info = api.repo_info(repo_id=repo_id, repo_type=repo_type)
        sha = getattr(info, "sha", None)
        if sha:
            return _repo_commit_url(repo_id=repo_id, repo_type=repo_type, sha=str(sha))
        return _repo_url(repo_id=repo_id, repo_type=repo_type)

    info = api.upload_folder(
        repo_id=repo_id,
        repo_type=repo_type,
        folder_path=str(export_dir),
        commit_message=f"Sync ctx {head[:7]}",
        commit_description=f"GitHub commit: {head}",
        delete_patterns="*",
    )
    return str(getattr(info, "commit_url", info))


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
        _ensure_hf_repo_exists(api=api, repo_id=repo_id, repo_type=repo_type)
        return _upload_export(
            api=api,
            repo_id=repo_id,
            repo_type=repo_type,
            export_dir=export_dir,
            head=head,
            prefer_large_upload=True,
        )
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
