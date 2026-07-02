from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctx.core.wiki import wiki_packs
from ctx.core.wiki.wiki_packs import (
    WikiPackManifestError,
    compact_wiki_packs,
    discover_wiki_pack_manifests,
    load_merged_wiki_pages,
    promote_wiki_pack_set,
    read_wiki_pack_manifest,
    write_active_wiki_overlay_pack,
    write_wiki_base_pack,
    write_wiki_overlay_pack,
)


def test_wiki_base_pack_manifest_round_trips(tmp_path: Path) -> None:
    pack_dir = tmp_path / "wiki-packs" / "base-export-1"

    manifest = write_wiki_base_pack(
        pack_dir=pack_dir,
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={
            "index.md": "# Index\n",
            "entities/skills/python.md": "# Python\n",
        },
    )

    assert manifest.pack_type == "base"
    assert manifest.page_count == 2
    assert manifest.tombstone_count == 0
    assert read_wiki_pack_manifest(pack_dir / "wiki-pack-manifest.json") == manifest
    assert (
        json.loads((pack_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines()[0])["path"]
        == "entities/skills/python.md"
    )


def test_load_merged_wiki_pages_applies_overlay_and_tombstones(tmp_path: Path) -> None:
    packs_dir = tmp_path / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={
            "entities/skills/python.md": "# Python\nold body\n",
            "entities/skills/docker.md": "# Docker\n",
        },
    )
    write_wiki_overlay_pack(
        pack_dir=packs_dir / "overlay-review",
        pack_id="overlay-review",
        base_export_id="wiki-export-1",
        parent_export_id="wiki-export-1",
        pages={
            "entities/skills/python.md": "# Python\nnew body\n",
            "entities/agents/reviewer.md": "# Reviewer\n",
        },
        tombstones=["entities/skills/docker.md"],
    )

    pages = load_merged_wiki_pages(packs_dir)

    assert pages == {
        "entities/agents/reviewer.md": "# Reviewer\n",
        "entities/skills/python.md": "# Python\nnew body\n",
    }


def test_load_merged_wiki_pages_applies_overlays_by_created_at(
    tmp_path: Path,
) -> None:
    packs_dir = tmp_path / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"entities/skills/python.md": "# Python\nbase\n"},
    )
    write_wiki_overlay_pack(
        pack_dir=packs_dir / "overlay-z-old",
        pack_id="overlay-z-old",
        base_export_id="wiki-export-1",
        parent_export_id="wiki-export-1",
        pages={"entities/skills/python.md": "# Python\nold overlay\n"},
        tombstones=[],
        created_at="2026-01-01T00:00:00+00:00",
    )
    write_wiki_overlay_pack(
        pack_dir=packs_dir / "overlay-a-new",
        pack_id="overlay-a-new",
        base_export_id="wiki-export-1",
        parent_export_id="wiki-export-1",
        pages={"entities/skills/python.md": "# Python\nnew overlay\n"},
        tombstones=[],
        created_at="2026-01-02T00:00:00+00:00",
    )

    entries = discover_wiki_pack_manifests(packs_dir)
    pages = load_merged_wiki_pages(packs_dir)

    assert [entry.manifest.pack_id for entry in entries] == [
        "base-export-1",
        "overlay-z-old",
        "overlay-a-new",
    ]
    assert pages["entities/skills/python.md"] == "# Python\nnew overlay\n"


def test_write_active_wiki_overlay_pack_uses_current_base_export(tmp_path: Path) -> None:
    packs_dir = tmp_path / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={
            "entities/skills/old.md": "# Old\n",
            "index.md": "# Index\n",
        },
    )

    manifest = write_active_wiki_overlay_pack(
        packs_dir=packs_dir,
        pages={"entities/skills/new.md": "# New\n"},
        tombstones=["entities/skills/old.md"],
    )

    assert manifest is not None
    assert manifest.pack_type == "overlay"
    assert manifest.base_export_id == "wiki-export-1"
    assert manifest.parent_export_id == "wiki-export-1"
    assert load_merged_wiki_pages(packs_dir) == {
        "entities/skills/new.md": "# New\n",
        "index.md": "# Index\n",
    }


def test_discover_wiki_pack_manifests_rejects_parent_mismatch(tmp_path: Path) -> None:
    packs_dir = tmp_path / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"index.md": "# Index\n"},
    )
    write_wiki_overlay_pack(
        pack_dir=packs_dir / "overlay-bad",
        pack_id="overlay-bad",
        base_export_id="wiki-export-1",
        parent_export_id="other-export",
        pages={"entities/skills/review.md": "# Review\n"},
        tombstones=[],
    )

    with pytest.raises(WikiPackManifestError, match="parent_export_id"):
        discover_wiki_pack_manifests(packs_dir)


def test_wiki_pack_writer_rejects_unsafe_page_paths(tmp_path: Path) -> None:
    with pytest.raises(WikiPackManifestError, match="unsafe"):
        write_wiki_base_pack(
            pack_dir=tmp_path / "wiki-packs" / "base-export-1",
            pack_id="base-export-1",
            base_export_id="wiki-export-1",
            pages={"../entities/skills/evil.md": "# Evil\n"},
        )


def test_load_merged_wiki_pages_rejects_page_checksum_drift(tmp_path: Path) -> None:
    packs_dir = tmp_path / "wiki-packs"
    base_dir = packs_dir / "base-export-1"
    write_wiki_base_pack(
        pack_dir=base_dir,
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"index.md": "# Index\n"},
    )
    rows = (base_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[0])
    payload["text"] = "# Tampered\n"
    (base_dir / "pages.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(WikiPackManifestError, match="checksum mismatch"):
        load_merged_wiki_pages(packs_dir)


def test_load_merged_wiki_pages_rejects_base_page_count_drift(tmp_path: Path) -> None:
    packs_dir = tmp_path / "wiki-packs"
    base_dir = packs_dir / "base-export-1"
    write_wiki_base_pack(
        pack_dir=base_dir,
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"index.md": "# Index\n"},
    )
    manifest_path = base_dir / "wiki-pack-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["page_count"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(WikiPackManifestError, match="page_count mismatch"):
        load_merged_wiki_pages(packs_dir)


def test_load_merged_wiki_pages_rejects_overlay_tombstone_count_drift(
    tmp_path: Path,
) -> None:
    packs_dir = tmp_path / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=packs_dir / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"entities/skills/old.md": "# Old\n"},
    )
    overlay_dir = packs_dir / "overlay-delete"
    write_wiki_overlay_pack(
        pack_dir=overlay_dir,
        pack_id="overlay-delete",
        base_export_id="wiki-export-1",
        parent_export_id="wiki-export-1",
        pages={},
        tombstones=["entities/skills/old.md"],
    )
    manifest_path = overlay_dir / "wiki-pack-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tombstone_count"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(WikiPackManifestError, match="tombstone_count mismatch"):
        load_merged_wiki_pages(packs_dir)


def test_compact_wiki_packs_writes_staged_base_without_mutating_active_packs(
    tmp_path: Path,
) -> None:
    active_packs = tmp_path / "active" / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=active_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={
            "entities/skills/python.md": "# Python\nold\n",
            "entities/skills/docker.md": "# Docker\n",
        },
    )
    write_wiki_overlay_pack(
        pack_dir=active_packs / "overlay-review",
        pack_id="overlay-review",
        base_export_id="wiki-export-1",
        parent_export_id="wiki-export-1",
        pages={
            "entities/skills/python.md": "# Python\nnew\n",
            "entities/agents/reviewer.md": "# Reviewer\n",
        },
        tombstones=["entities/skills/docker.md"],
    )
    staged_pack = tmp_path / "staged" / "base-export-2"

    manifest = compact_wiki_packs(
        packs_dir=active_packs,
        compacted_pack_dir=staged_pack,
        base_export_id="wiki-export-2",
    )

    assert manifest.pack_type == "base"
    assert manifest.base_export_id == "wiki-export-2"
    assert [entry.manifest.pack_id for entry in discover_wiki_pack_manifests(active_packs)] == [
        "base-export-1",
        "overlay-review",
    ]
    assert load_merged_wiki_pages(tmp_path / "staged") == {
        "entities/agents/reviewer.md": "# Reviewer\n",
        "entities/skills/python.md": "# Python\nnew\n",
    }


def test_promote_wiki_pack_set_replaces_active_and_writes_rollback_metadata(
    tmp_path: Path,
) -> None:
    active_packs = tmp_path / "wiki" / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=active_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"entities/skills/old.md": "# Old\n"},
    )
    staged_packs = tmp_path / "staged-packs"
    write_wiki_base_pack(
        pack_dir=staged_packs / "base-export-2",
        pack_id="base-export-2",
        base_export_id="wiki-export-2",
        pages={"entities/skills/new.md": "# New\n"},
    )
    backup_packs = tmp_path / "wiki" / "wiki-packs.rollback"

    result = promote_wiki_pack_set(
        staged_packs_dir=staged_packs,
        active_packs_dir=active_packs,
        backup_packs_dir=backup_packs,
    )

    assert result.promoted_pack_ids == ["base-export-2"]
    assert result.replaced_pack_ids == ["base-export-1"]
    assert not staged_packs.exists()
    assert load_merged_wiki_pages(active_packs) == {"entities/skills/new.md": "# New\n"}
    assert load_merged_wiki_pages(backup_packs) == {"entities/skills/old.md": "# Old\n"}
    rollback_metadata = json.loads(result.rollback_metadata_path.read_text(encoding="utf-8"))
    assert rollback_metadata["backup_packs_dir"] == str(backup_packs)
    assert rollback_metadata["promoted_pack_ids"] == ["base-export-2"]
    assert rollback_metadata["replaced_pack_ids"] == ["base-export-1"]


def test_wiki_pack_cli_compact_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    active_packs = tmp_path / "wiki" / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=active_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"entities/skills/python.md": "# Python\nold\n"},
    )
    write_wiki_overlay_pack(
        pack_dir=active_packs / "overlay-python",
        pack_id="overlay-python",
        base_export_id="wiki-export-1",
        parent_export_id="wiki-export-1",
        pages={"entities/skills/python.md": "# Python\nnew\n"},
        tombstones=[],
    )
    staged_pack = tmp_path / "staged" / "base-export-2"

    rc = wiki_packs.main(
        [
            "compact",
            "--packs-dir",
            str(active_packs),
            "--staged-pack-dir",
            str(staged_pack),
            "--base-export-id",
            "wiki-export-2",
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pack_id"] == "base-export-2"
    assert payload["page_count"] == 1
    assert load_merged_wiki_pages(tmp_path / "staged") == {
        "entities/skills/python.md": "# Python\nnew\n",
    }


def test_wiki_pack_cli_promote_emits_json(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    active_packs = tmp_path / "wiki" / "wiki-packs"
    write_wiki_base_pack(
        pack_dir=active_packs / "base-export-1",
        pack_id="base-export-1",
        base_export_id="wiki-export-1",
        pages={"entities/skills/old.md": "# Old\n"},
    )
    staged_packs = tmp_path / "staged-packs"
    write_wiki_base_pack(
        pack_dir=staged_packs / "base-export-2",
        pack_id="base-export-2",
        base_export_id="wiki-export-2",
        pages={"entities/skills/new.md": "# New\n"},
    )

    rc = wiki_packs.main(
        [
            "promote",
            "--staged-packs-dir",
            str(staged_packs),
            "--active-packs-dir",
            str(active_packs),
            "--json",
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["promoted_pack_ids"] == ["base-export-2"]
    assert payload["replaced_pack_ids"] == ["base-export-1"]
    assert load_merged_wiki_pages(active_packs) == {"entities/skills/new.md": "# New\n"}
