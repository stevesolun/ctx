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
