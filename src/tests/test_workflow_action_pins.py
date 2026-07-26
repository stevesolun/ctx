from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re
from typing import Any

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
REMOTE_ACTION_RE = re.compile(
    r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.@/-]+)?@[0-9a-f]{40}"
)
DOCKER_ACTION_RE = re.compile(r"docker://[^@\s]+@sha256:[0-9a-f]{64}")
PIN_COMMENT_RE = re.compile(r"\buses:\s*[^#\s]+(?:@[0-9a-f]{40}|@sha256:[0-9a-f]{64})\s+#\s+\S+")


def _uses_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses":
                assert isinstance(child, str)
                yield child
            yield from _uses_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _uses_values(child)


def _is_immutable_uses(value: str) -> bool:
    if value.startswith("./"):
        return ".." not in Path(value).parts
    return (
        REMOTE_ACTION_RE.fullmatch(value) is not None
        or DOCKER_ACTION_RE.fullmatch(value) is not None
    )


@pytest.mark.parametrize(
    "value",
    [
        "./.github/actions/local",
        "owner/action@0123456789abcdef0123456789abcdef01234567",
        "owner/repo/.github/workflows/check.yml@0123456789abcdef0123456789abcdef01234567",
        "docker://example/image@sha256:" + "a" * 64,
    ],
)
def test_immutable_uses_classifier_accepts_supported_pins(value: str) -> None:
    assert _is_immutable_uses(value)


@pytest.mark.parametrize(
    "value",
    [
        "actions/checkout@v7",
        "owner/action@main",
        "owner/action@0123456",
        "owner/action@${{ inputs.ref }}",
        "./../outside",
        "docker://example/image:latest",
    ],
)
def test_immutable_uses_classifier_rejects_mutable_references(value: str) -> None:
    assert not _is_immutable_uses(value)


def test_every_workflow_action_is_immutably_pinned() -> None:
    workflow_paths = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))

    assert workflow_paths
    for path in workflow_paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        uses_values = list(_uses_values(document))
        assert uses_values, f"{path.relative_to(ROOT)} has no action references"
        assert [value for value in uses_values if not _is_immutable_uses(value)] == []

        remote_lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "uses:" in line and not line.split("uses:", 1)[1].lstrip().startswith("./")
        ]
        assert remote_lines
        assert [line for line in remote_lines if PIN_COMMENT_RE.search(line) is None] == []
