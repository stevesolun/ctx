"""Bulk mirror comparisons reuse authoritative packs without weakening writes."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

import agent_mirror
import skill_mirror
from ctx.core.wiki import wiki_bodies
from ctx.core.wiki.wiki_packs import (
    WikiPackManifestError,
    load_merged_wiki_pages,
    write_active_wiki_overlay_pack,
    write_wiki_base_pack,
)


def _body(slug: str, version: str = "Original") -> str:
    return f"---\nname: {slug}\ndescription: Synthetic helper.\n---\n\n{version} instructions.\n"


def _rich_path(kind: str, slug: str) -> str:
    if kind == "skill":
        return f"converted/{slug}/SKILL.md"
    return f"converted-agents/{slug}.md"


def _source_path(root: Path, kind: str, slug: str) -> Path:
    if kind == "skill":
        return root / "skills" / slug / "SKILL.md"
    return root / "agents" / f"{slug}.md"


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _prepare(root: Path, kind: str, *, omit: str | None = None) -> Path:
    wiki = root / "wiki"
    pages = {}
    for slug in ("alpha", "bravo", "charlie"):
        source = _source_path(root, kind, slug)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(_body(slug), encoding="utf-8")
        loose = wiki / _rich_path(kind, slug)
        loose.parent.mkdir(parents=True, exist_ok=True)
        loose.write_text(_body(slug, "Stale loose"), encoding="utf-8")
        if slug != omit:
            pages[_rich_path(kind, slug)] = _body(slug)
    write_wiki_base_pack(
        pack_dir=wiki / "wiki-packs" / "base-test",
        pack_id="base-test",
        base_export_id="synthetic-export",
        pages=pages,
    )
    return wiki


def _mirror(root: Path, kind: str, *, slug: str | None = None, **options):
    module = skill_mirror if kind == "skill" else agent_mirror
    kwargs: dict[str, Any] = {
        f"{kind}s_dir": root / f"{kind}s",
        "wiki_dir": root / "wiki",
        **options,
    }
    if kind == "skill":
        kwargs["line_threshold"] = 180
    if slug is None:
        return module.mirror_all(**kwargs)
    return module.mirror_one(slug, **kwargs)


def _spy_loaders(monkeypatch) -> tuple[Mock, Mock]:
    load = Mock(wraps=wiki_bodies.load_merged_wiki_pages)
    discover = Mock(wraps=wiki_bodies.discover_wiki_pack_manifests)
    monkeypatch.setattr(wiki_bodies, "load_merged_wiki_pages", load)
    monkeypatch.setattr(wiki_bodies, "discover_wiki_pack_manifests", discover)
    return load, discover


def _corrupt(wiki: Path, corruption: str) -> None:
    if corruption == "checksum":
        pages = wiki / "wiki-packs" / "base-test" / "pages.jsonl"
        pages.write_bytes(pages.read_bytes() + b"corrupt\n")
    else:
        (wiki / "wiki-packs" / "base-test" / "wiki-pack-manifest.json").unlink()


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unchanged_bulk_mirror_validates_and_parses_one_snapshot(
    tmp_path, monkeypatch, kind, dry_run
):
    _prepare(tmp_path, kind)
    before = _snapshot(tmp_path)
    load, discover = _spy_loaders(monkeypatch)

    results = _mirror(tmp_path, kind, dry_run=dry_run)

    assert [(result.slug, result.status) for result in results] == [
        ("alpha", "unchanged"),
        ("bravo", "unchanged"),
        ("charlie", "unchanged"),
    ]
    assert load.call_count == 1
    assert discover.call_count == 1
    assert _snapshot(tmp_path) == before


def test_skill_bulk_indexes_packed_directories_once(tmp_path, monkeypatch):
    _prepare(tmp_path, "skill")
    load = wiki_bodies.load_merged_wiki_pages
    scans = []

    class CountedPages(dict[str, str]):
        def __iter__(self):
            scans.append("keys")
            return super().__iter__()

        def items(self):
            scans.append("items")
            return super().items()

    monkeypatch.setattr(
        wiki_bodies,
        "load_merged_wiki_pages",
        lambda packs_dir: CountedPages(load(packs_dir)),
    )

    assert [result.status for result in _mirror(tmp_path, "skill")] == ["unchanged"] * 3
    assert len(scans) == 1


@pytest.mark.parametrize("kind", ["skill", "agent"])
def test_dry_run_proposal_reuses_snapshot_without_refresh(tmp_path, monkeypatch, kind):
    _prepare(tmp_path, kind, omit="alpha")
    before = _snapshot(tmp_path)
    load, discover = _spy_loaders(monkeypatch)

    results = _mirror(tmp_path, kind, dry_run=True)

    assert [(result.slug, result.status) for result in results] == [
        ("alpha", "mirrored"),
        ("bravo", "unchanged"),
        ("charlie", "unchanged"),
    ]
    assert load.call_count == 1
    assert discover.call_count == 1
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["skill", "agent"])
def test_bulk_mirror_refreshes_snapshot_after_successful_publication(
    tmp_path, monkeypatch, kind
):
    wiki = _prepare(tmp_path, kind, omit="alpha")
    updated = _body("bravo", "Updated while publishing alpha")
    _source_path(tmp_path, kind, "bravo").write_text(updated, encoding="utf-8")
    module = skill_mirror if kind == "skill" else agent_mirror
    replace_name = f"replace_{kind}_body"
    replace = getattr(module, replace_name)
    published = []

    def publish_with_sibling_update(wiki_path, slug, content):
        replace(wiki_path, slug, content)
        published.append(slug)
        if slug == "alpha":
            write_active_wiki_overlay_pack(
                packs_dir=wiki_path / "wiki-packs",
                pages={_rich_path(kind, "bravo"): updated},
            )

    monkeypatch.setattr(module, replace_name, publish_with_sibling_update)
    load, discover = _spy_loaders(monkeypatch)

    results = _mirror(tmp_path, kind)

    assert [(result.slug, result.status) for result in results] == [
        ("alpha", "mirrored"),
        ("bravo", "unchanged"),
        ("charlie", "unchanged"),
    ]
    assert published == ["alpha"]
    assert load.call_count == 3
    assert discover.call_count == 3
    pages = load_merged_wiki_pages(wiki / "wiki-packs")
    assert pages[_rich_path(kind, "alpha")] == _body("alpha")
    assert pages[_rich_path(kind, "bravo")] == updated
    assert pages[_rich_path(kind, "charlie")] == _body("charlie")


@pytest.mark.parametrize("kind", ["skill", "agent"])
def test_direct_mirror_calls_load_current_pack_state(tmp_path, monkeypatch, kind):
    wiki = _prepare(tmp_path, kind)
    load, discover = _spy_loaders(monkeypatch)
    assert _mirror(tmp_path, kind, slug="alpha").status == "unchanged"

    updated = _body("alpha", "Updated between calls")
    _source_path(tmp_path, kind, "alpha").write_text(updated, encoding="utf-8")
    write_active_wiki_overlay_pack(
        packs_dir=wiki / "wiki-packs",
        pages={_rich_path(kind, "alpha"): updated},
    )
    before = _snapshot(tmp_path)
    assert _mirror(tmp_path, kind, slug="alpha").status == "unchanged"

    assert load.call_count == 2
    assert discover.call_count == 2
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("corruption", ["checksum", "missing-base"])
def test_corrupt_bulk_snapshot_fails_before_mutation(tmp_path, kind, corruption):
    wiki = _prepare(tmp_path, kind, omit="alpha")
    _corrupt(wiki, corruption)
    before = _snapshot(tmp_path)

    with pytest.raises(WikiPackManifestError):
        _mirror(tmp_path, kind)

    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("corruption", ["checksum", "missing-base"])
def test_bulk_write_revalidates_after_cached_comparison(
    tmp_path, monkeypatch, kind, corruption
):
    wiki = _prepare(tmp_path, kind, omit="bravo")
    load = wiki_bodies.load_merged_wiki_pages
    observed = []

    def load_then_corrupt(packs_dir):
        pages = load(packs_dir)
        _corrupt(wiki, corruption)
        observed.append(_snapshot(tmp_path))
        return pages

    monkeypatch.setattr(wiki_bodies, "load_merged_wiki_pages", load_then_corrupt)

    with pytest.raises(WikiPackManifestError):
        _mirror(tmp_path, kind)

    assert len(observed) == 1
    assert _snapshot(tmp_path) == observed[0]
