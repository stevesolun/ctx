"""Default workflows must remain independent of retired catalog storage."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("path", sorted((ROOT / ".github/workflows").glob("*.yml")))
def test_workflows_have_no_tool_corpus_storage_dependency(path: Path) -> None:
    workflow = path.read_text(encoding="utf-8")
    assert "git lfs" not in workflow.lower()
    assert "GIT_LFS_" not in workflow
    assert "git-lfs.github.com" not in workflow
    assert "graph_release_manifest.py hydrate" not in workflow
    assert "sync_huggingface.py" not in workflow


def test_owned_dataset_sync_is_not_a_workflow() -> None:
    assert not (ROOT / ".github/workflows/huggingface-sync.yml").exists()
