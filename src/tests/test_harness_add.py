"""Tests for harness catalog ingestion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
import pytest

import harness_add
from ctx.core.wiki.wiki_packs import load_merged_wiki_pages, write_wiki_base_pack


def _record(**overrides: Any) -> harness_add.HarnessRecord:
    data: dict[str, Any] = {
        "repo_url": "https://github.com/earthtojake/text-to-cad",
        "name": "Text to CAD",
        "description": "Model harness for turning text prompts into CAD artifacts.",
        "tags": ["cad", "3d", "automation"],
        "model_providers": ["openai", "anthropic"],
        "runtimes": ["python"],
        "capabilities": ["Generate CAD artifacts from natural language"],
        "setup_commands": ["pip install -e .", "python app.py"],
        "verify_commands": ["pytest"],
        "sources": ["manual"],
    }
    data.update(overrides)
    return harness_add.HarnessRecord.from_dict(data)


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return _frontmatter_from_text(text)


def _frontmatter_from_text(text: str) -> dict[str, Any]:
    _, fm_block, _ = text.split("---", 2)
    parsed = yaml.safe_load(fm_block)
    assert isinstance(parsed, dict)
    return parsed


def test_command_fields_preserve_commas() -> None:
    record = _record(
        setup_commands=["python -c \"print('setup, ok')\""],
        verify_commands=["python -c \"print('verify, ok')\""],
    )

    assert record.setup_commands == ("python -c \"print('setup, ok')\"",)
    assert record.verify_commands == ("python -c \"print('verify, ok')\"",)


def test_add_harness_creates_page_index_and_log(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    record = _record()

    result = harness_add.add_harness(record=record, wiki_path=wiki)

    page = wiki / "entities" / "harnesses" / "text-to-cad.md"
    assert result["is_new_page"] is True
    assert page.exists()
    fm = _frontmatter(page)
    assert fm["type"] == "harness"
    assert fm["repo_url"] == "https://github.com/earthtojake/text-to-cad"
    assert fm["setup_commands"] == ["pip install -e .", "python app.py"]
    assert "[[entities/harnesses/text-to-cad]]" in (wiki / "index.md").read_text(encoding="utf-8")
    assert "add-harness | text-to-cad" in (wiki / "log.md").read_text(encoding="utf-8")


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    result = harness_add.add_harness(
        record=_record(),
        wiki_path=wiki,
        dry_run=True,
    )

    assert result["slug"] == "text-to-cad"
    assert not (wiki / "entities" / "harnesses" / "text-to-cad.md").exists()


def test_readd_merges_sources_without_duplicate_file(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    harness_add.add_harness(record=_record(sources=["manual"]), wiki_path=wiki)
    result = harness_add.add_harness(
        record=_record(sources=["external-review"]),
        wiki_path=wiki,
    )

    page = wiki / "entities" / "harnesses" / "text-to-cad.md"
    fm = _frontmatter(page)
    assert result["is_new_page"] is False
    assert result["sources"] == ["external-review", "manual"]
    assert fm["sources"] == ["external-review", "manual"]
    assert len(list((wiki / "entities" / "harnesses").glob("*.md"))) == 1


def test_readd_parses_crlf_frontmatter_before_merge(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    harness_add.add_harness(record=_record(sources=["manual"]), wiki_path=wiki)
    page = wiki / "entities" / "harnesses" / "text-to-cad.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace("\n", "\r\n"),
        encoding="utf-8",
        newline="",
    )

    result = harness_add.add_harness(
        record=_record(sources=["external-review"]),
        wiki_path=wiki,
    )

    fm = _frontmatter(page)
    assert result["sources"] == ["external-review", "manual"]
    assert fm["sources"] == ["external-review", "manual"]


def test_existing_harness_review_skips_without_mutating_page(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    harness_add.add_harness(
        record=_record(
            sources=["manual"],
            setup_commands=["pip install -e .", "python app.py"],
            verify_commands=["pytest"],
        ),
        wiki_path=wiki,
    )
    page = wiki / "entities" / "harnesses" / "text-to-cad.md"
    original_text = page.read_text(encoding="utf-8")

    result = harness_add.add_harness(
        record=_record(
            sources=["external-review"],
            setup_commands=[],
            verify_commands=[],
            capabilities=[],
        ),
        wiki_path=wiki,
        review_existing=True,
    )

    assert result["is_new_page"] is False
    assert result["skipped"] is True
    assert result["update_required"] is True
    assert "Existing harness already exists: text-to-cad" in result["update_review"]
    assert "Risks:" in result["update_review"]
    assert page.read_text(encoding="utf-8") == original_text


def test_existing_harness_update_existing_applies_reviewed_change(
    tmp_path: Path,
) -> None:
    wiki = tmp_path / "wiki"
    existing = _record(sources=["manual"])
    packs_dir = wiki / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"entities/harnesses/text-to-cad.md": harness_add.generate_harness_page(existing)},
    )

    result = harness_add.add_harness(
        record=_record(
            sources=["external-review"],
            capabilities=["Generate CAD artifacts", "Verify CAD artifacts"],
            verify_commands=["pytest", "python smoke.py"],
        ),
        wiki_path=wiki,
        review_existing=True,
        update_existing=True,
    )

    page = wiki / "entities" / "harnesses" / "text-to-cad.md"
    merged = load_merged_wiki_pages(packs_dir)
    fm = _frontmatter_from_text(merged["entities/harnesses/text-to-cad.md"])
    assert result["is_new_page"] is False
    assert result["skipped"] is False
    assert result["sources"] == ["external-review", "manual"]
    assert not page.exists()
    assert fm["sources"] == ["external-review", "manual"]
    assert fm["verify_commands"] == ["pytest", "python smoke.py"]


def test_add_harness_refuses_symlinked_entity_page(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    target_dir = wiki / "entities" / "harnesses"
    target_dir.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    try:
        (target_dir / "text-to-cad.md").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this environment: {exc}")

    with pytest.raises(ValueError, match="symlink"):
        harness_add.add_harness(record=_record(), wiki_path=wiki)

    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_cli_from_json_adds_harness(
    tmp_path: Path,
    capsys: Any,
) -> None:
    record_path = tmp_path / "harness.json"
    record_path.write_text(
        json.dumps(
            {
                "repo_url": "https://github.com/earthtojake/text-to-cad",
                "description": "Text to CAD harness.",
                "tags": ["cad", "llm"],
            }
        ),
        encoding="utf-8",
    )
    wiki = tmp_path / "wiki"

    harness_add.main(["--from-json", str(record_path), "--wiki", str(wiki)])

    assert (wiki / "entities" / "harnesses" / "text-to-cad.md").exists()
    assert "added: text-to-cad" in capsys.readouterr().out


def test_cli_from_bom_json_adds_harness(
    tmp_path: Path,
    capsys: Any,
) -> None:
    record_path = tmp_path / "harness.json"
    record_path.write_text(
        "\ufeff"
        + json.dumps(
            {
                "repo_url": "https://github.com/example/bom-harness",
                "description": "Harness loaded from a Windows UTF-8 BOM JSON file.",
                "tags": ["llm", "testing"],
            }
        ),
        encoding="utf-8",
    )
    wiki = tmp_path / "wiki"

    harness_add.main(["--from-json", str(record_path), "--wiki", str(wiki)])

    assert (wiki / "entities" / "harnesses" / "bom-harness.md").exists()
    assert "added: bom-harness" in capsys.readouterr().out
