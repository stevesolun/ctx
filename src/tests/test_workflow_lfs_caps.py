from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_STEPS = {
    ".github/workflows/huggingface-sync.yml": (
        "sync",
        "Hydrate required graph artifacts from exact release manifest",
        "Set up Python",
    ),
    ".github/workflows/publish.yml": (
        "build",
        "Resolve release graph artifacts from exact release manifest",
        "Validate release graph artifacts",
    ),
    ".github/workflows/test.yml": (
        "graph-check",
        "Resolve graph artifacts from exact release manifest",
        "Validate shipped graph artifacts",
    ),
}
RESOLVER_COMMAND = (
    "python scripts/graph_release_manifest.py hydrate --manifest graph/release-artifacts.json"
)


def _workflow_steps(workflow_path: str) -> list[dict[str, object]]:
    job_name, _, _ = WORKFLOW_STEPS[workflow_path]
    workflow = yaml.safe_load((ROOT / workflow_path).read_text(encoding="utf-8"))
    steps = workflow["jobs"][job_name]["steps"]
    assert isinstance(steps, list)
    return steps


@pytest.mark.parametrize("workflow_path", WORKFLOW_STEPS)
def test_graph_workflows_have_no_git_lfs_dependency(workflow_path: str) -> None:
    workflow = (ROOT / workflow_path).read_text(encoding="utf-8")

    assert "git lfs" not in workflow.lower()
    assert "GIT_LFS_" not in workflow
    assert "git-lfs.github.com" not in workflow
    assert "targeted LFS" not in workflow


@pytest.mark.parametrize("workflow_path", WORKFLOW_STEPS)
def test_graph_workflows_use_shared_exact_manifest_resolver(workflow_path: str) -> None:
    _, step_name, _ = WORKFLOW_STEPS[workflow_path]
    steps = _workflow_steps(workflow_path)
    step = next(item for item in steps if item.get("name") == step_name)
    run = str(step["run"])

    assert RESOLVER_COMMAND in run
    assert run.count("scripts/graph_release_manifest.py") == 1
    assert "--manifest graph/release-artifacts.json" in run
    assert "git" not in run.lower()


@pytest.mark.parametrize("workflow_path", WORKFLOW_STEPS)
def test_graph_workflows_resolve_before_consuming_artifacts(workflow_path: str) -> None:
    _, resolver_name, consumer_name = WORKFLOW_STEPS[workflow_path]
    names = [str(step.get("name", "")) for step in _workflow_steps(workflow_path)]

    assert names.index(resolver_name) < names.index(consumer_name)


def test_huggingface_hydration_keeps_canonical_full_sync_guard() -> None:
    steps = _workflow_steps(".github/workflows/huggingface-sync.yml")
    step = next(
        item
        for item in steps
        if item.get("name") == "Hydrate required graph artifacts from exact release manifest"
    )

    assert step["if"] == (
        "${{ env.HF_TOKEN != '' && github.repository == 'stevesolun/ctx' "
        "&& steps.scope.outputs.sync_mode == 'full' }}"
    )
