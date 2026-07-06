from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _resolve_script(workflow_path: str) -> str:
    workflow = (ROOT / workflow_path).read_text(encoding="utf-8")
    start = workflow.index("def hydrate_from_lfs")
    end = workflow.index("          PY", start)
    return workflow[start:end]


@pytest.mark.parametrize(
    "workflow_path",
    [
        ".github/workflows/huggingface-sync.yml",
        ".github/workflows/publish.yml",
        ".github/workflows/test.yml",
    ],
)
def test_targeted_lfs_fallback_checks_pointer_size_before_pull(workflow_path: str) -> None:
    workflow = (ROOT / workflow_path).read_text(encoding="utf-8")
    script = _resolve_script(workflow_path)

    assert '"graph/wiki-graph.tar.gz": 350_000_000' in workflow
    assert '"graph/wiki-graph-runtime.tar.gz": 150_000_000' in workflow
    size_check = script.index("if expected_size > max_size:")
    lfs_pull = script.index('["git", "lfs", "pull", "--include", path_name, "--exclude", ""]')
    missing_cap_check = script.index("no size cap configured")

    assert "max_size = max_lfs_fallback_sizes.get(path_name)" in script
    assert missing_cap_check < lfs_pull
    assert "pointer size {expected_size} exceeds cap {max_size}" in script
    assert size_check < lfs_pull


def test_huggingface_sync_catalog_lfs_fallback_has_explicit_cap() -> None:
    workflow = (ROOT / ".github" / "workflows" / "huggingface-sync.yml").read_text(encoding="utf-8")

    assert '"graph/skills-sh-catalog.json.gz": 15_000_000' in workflow
    assert workflow.index('"graph/skills-sh-catalog.json.gz": 15_000_000') < workflow.index(
        'hydrate_from_release("graph/skills-sh-catalog.json.gz", 1_000_000)'
    )
