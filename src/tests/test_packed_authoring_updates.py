"""Authoring publishes rich content into the wiki's authoritative packs."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_add
import agent_mirror
import batch_convert
import ctx_audit_log
import ctx_config
import skill_add
import skill_mirror
from ctx.core.graph import semantic_edges
from ctx.core.wiki import wiki_bodies
from ctx.core.wiki import wiki_graphify as wg
from ctx.core.wiki import wiki_sync
from ctx.core.wiki.wiki_packs import WikiPackManifestError, load_merged_wiki_pages


def _body(version: str) -> str:
    return (
        "---\nname: author-tool\ndescription: Synthetic testing helper.\n"
        "tags: [testing]\n---\n\n"
        f"# Author tool\n\n{version}\n"
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _rich_path(kind: str) -> str:
    if kind == "skill":
        return "converted/author-tool/SKILL.md"
    return "converted-agents/author-tool.md"


def _installed_path(root: Path, kind: str) -> Path:
    if kind == "skill":
        return root / "skills" / "author-tool" / "SKILL.md"
    return root / "agents" / "author-tool.md"


def _add(root: Path, kind: str, *, update: bool = False) -> dict:
    kwargs: dict[str, Any] = {
        "source_path": root / "source.md",
        "name": "author-tool",
        "wiki_path": root / "wiki",
        "review_existing": True,
        "update_existing": update,
    }
    if kind == "skill":
        return skill_add.add_skill(skills_dir=root / "skills", **kwargs)
    return agent_add.add_agent(agents_dir=root / "agents", **kwargs)


def _mirror(root: Path, kind: str, *, prune: bool = False, dry_run: bool = False):
    kwargs: dict[str, Any] = {"wiki_dir": root / "wiki", "dry_run": dry_run}
    module = skill_mirror if kind == "skill" else agent_mirror
    kwargs[f"{kind}s_dir"] = root / f"{kind}s"
    if prune:
        return module.prune_orphans(**kwargs)
    if kind == "skill":
        kwargs["line_threshold"] = 180
    return module.mirror_one("author-tool", force=True, **kwargs)


@pytest.fixture
def authoring_wiki(tmp_path, monkeypatch):
    original_wiki = wg.WIKI_DIR
    wiki = tmp_path / "wiki"
    wiki_sync.ensure_wiki(str(wiki))
    wg.configure_wiki_dir(wiki)
    monkeypatch.setattr(wg, "QUALITY_SIDECAR_DIR", tmp_path / "quality")
    monkeypatch.setattr(ctx_config.cfg, "graph_edge_weight_semantic", 1.0)
    monkeypatch.setattr(ctx_config.cfg, "line_threshold", 180)
    monkeypatch.setattr(batch_convert, "cfg", ctx_config.cfg)
    monkeypatch.setattr(skill_add, "convert_skill", batch_convert.convert_skill)
    monkeypatch.setattr(ctx_audit_log, "log_skill_event", lambda *args, **kwargs: None)
    for module in (agent_add, skill_add):
        monkeypatch.setattr(module, "cfg", ctx_config.cfg)
        monkeypatch.setattr(module, "ensure_wiki", wiki_sync.ensure_wiki)
        monkeypatch.setattr(module, "update_index", wiki_sync.update_index)
        monkeypatch.setattr(module, "append_log", wiki_sync.append_log)
        monkeypatch.setattr(
            module,
            "check_intake",
            lambda *args, **kwargs: SimpleNamespace(allow=True, warnings=()),
        )
        monkeypatch.setattr(module, "record_embedding", lambda *args, **kwargs: None)
    captured = []

    def capture(nodes, **_kwargs):
        captured.append({node.node_id: node.text for node in nodes})
        return {}

    monkeypatch.setattr(semantic_edges, "compute_semantic_edges", capture)

    def rebuild():
        monkeypatch.setattr(
            sys,
            "argv",
            ["ctx-wiki-graphify", "--wiki-dir", str(wiki), "--graph-only", "--full"],
        )
        wg.main()
        return captured[-1]

    try:
        yield tmp_path, rebuild
    finally:
        wg.configure_wiki_dir(original_wiki)


@pytest.mark.parametrize("kind", ["skill", "agent"])
def test_cli_review_then_approved_update_replaces_packed_semantic_body(
    authoring_wiki, monkeypatch, kind
):
    root, rebuild = authoring_wiki
    source = root / "source.md"
    wiki = root / "wiki"
    module = skill_add if kind == "skill" else agent_add
    args = [
        f"{kind}_add.py",
        f"--{kind}-path",
        str(source),
        "--name",
        "author-tool",
        "--wiki",
        str(wiki),
        f"--{kind}s-dir",
        str(root / f"{kind}s"),
    ]
    if kind == "skill":
        args.append("--no-security-scan")
    source.write_text(_body("Original rich prose v1"))
    monkeypatch.setattr(sys, "argv", args)
    module.main()
    assert "Original rich prose v1" in rebuild()[f"{kind}:author-tool"]

    source.write_text(_body("Replacement rich prose v2"))
    before_review = _snapshot(root)
    monkeypatch.setattr(sys, "argv", args)
    module.main()
    assert _snapshot(root) == before_review

    monkeypatch.setattr(sys, "argv", [*args, "--update-existing"])
    module.main()
    merged = load_merged_wiki_pages(wiki / "wiki-packs")
    assert merged[_rich_path(kind)] == source.read_text()
    assert _installed_path(root, kind).read_text() == source.read_text()
    (wiki / _rich_path(kind)).write_text(_body("Stale loose prose"))
    for _ in range(2):
        semantic_body = rebuild()[f"{kind}:author-tool"]
        assert "Replacement rich prose v2" in semantic_body
        assert "Original rich prose v1" not in semantic_body
        assert "Stale loose prose" not in semantic_body


def _long_skill(version: str, tables: int) -> str:
    sections = []
    for table in range(tables):
        rows = "\n".join(f"| {version} item {row} | {table} |" for row in range(195))
        sections.append(f"## Reference table {table}\n\n| Name | Value |\n|---|---|\n{rows}")
    return _body(version) + "\n\n".join(sections) + "\n"


def test_skill_reconversion_and_short_update_remove_superseded_packed_references(authoring_wiki):
    root, rebuild = authoring_wiki
    wiki = root / "wiki"
    source = root / "source.md"
    source.write_text(_long_skill("Original reference v1", tables=2))
    assert _add(root, "skill")["converted"] is True
    user_note = wiki / "converted" / "author-tool" / "private-notes.txt"
    user_note.write_text("Preserve unrelated user data\n")
    neighbor_source = root / "neighbor.md"
    neighbor_source.write_text(_body("Unrelated rich prose"))
    skill_add.add_skill(
        source_path=neighbor_source,
        name="neighbor",
        wiki_path=wiki,
        skills_dir=root / "skills",
    )
    assert "Original reference v1" in rebuild()["skill:author-tool"]
    first_pages = load_merged_wiki_pages(wiki / "wiki-packs")
    original_refs = {
        path for path in first_pages if path.startswith("converted/author-tool/references/")
    }
    assert any("ref-02" in path for path in original_refs)

    source.write_text(_long_skill("Replacement reference v2", tables=1))
    assert _add(root, "skill", update=True)["converted"] is True
    second_pages = load_merged_wiki_pages(wiki / "wiki-packs")
    removed_refs = original_refs - second_pages.keys()
    assert removed_refs
    assert any("Replacement reference v2" in text for text in second_pages.values())
    assert not any(
        "Original reference v1" in second_pages[path]
        for path in original_refs & second_pages.keys()
    )
    assert second_pages["converted/neighbor/SKILL.md"] == neighbor_source.read_text()
    assert user_note.read_text() == "Preserve unrelated user data\n"
    semantic_body = rebuild()["skill:author-tool"]
    assert "Replacement reference v2" in semantic_body
    assert "Original reference v1" not in semantic_body

    source.write_text(_body("Short replacement v3"))
    assert _add(root, "skill", update=True)["converted"] is False
    third_pages = load_merged_wiki_pages(wiki / "wiki-packs")
    rich_paths = {path for path in third_pages if path.startswith("converted/author-tool/")}
    assert rich_paths == {_rich_path("skill")}
    assert third_pages["converted/neighbor/SKILL.md"] == neighbor_source.read_text()
    assert user_note.read_text() == "Preserve unrelated user data\n"
    for relpath in original_refs:
        stale = wiki / relpath
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("Stale deleted reference\n")
    for _ in range(2):
        semantic_body = rebuild()["skill:author-tool"]
        assert "Short replacement v3" in semantic_body
        assert "Replacement reference v2" not in semantic_body
        assert "Stale deleted reference" not in semantic_body


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("packed_only", [False, True])
def test_standalone_mirror_update_and_prune_publish_overlays(authoring_wiki, kind, packed_only):
    root, rebuild = authoring_wiki
    wiki = root / "wiki"
    source = root / "source.md"
    source.write_text(_body("Original mirror v1"))
    _add(root, kind)
    rebuild()
    rich_path = wiki / _rich_path(kind)
    if packed_only:
        rich_path.unlink()
    installed = _installed_path(root, kind)
    installed.write_text(_body("Updated mirror v2"))
    before_update = _snapshot(wiki)
    assert _mirror(root, kind, dry_run=True).status == "mirrored"
    assert _snapshot(wiki) == before_update
    assert _mirror(root, kind).status == "mirrored"
    merged = load_merged_wiki_pages(wiki / "wiki-packs")
    assert merged[_rich_path(kind)] == installed.read_text()
    assert "Updated mirror v2" in rebuild()[f"{kind}:author-tool"]

    installed.unlink()
    if packed_only:
        rich_path.unlink()
    before_prune = _snapshot(wiki)
    dry_pruned = _mirror(root, kind, prune=True, dry_run=True)
    assert [result.slug for result in dry_pruned] == ["author-tool"]
    assert _snapshot(wiki) == before_prune
    assert [result.status for result in _mirror(root, kind, prune=True)] == ["pruned"]
    assert _rich_path(kind) not in load_merged_wiki_pages(wiki / "wiki-packs")
    rich_path.parent.mkdir(parents=True, exist_ok=True)
    rich_path.write_text(_body("Stale mirror resurrection"))
    for _ in range(2):
        semantic_body = rebuild()[f"{kind}:author-tool"]
        assert "Updated mirror v2" not in semantic_body
        assert "Stale mirror resurrection" not in semantic_body


@pytest.mark.parametrize("kind", ["skill", "agent"])
@pytest.mark.parametrize("corruption", ["checksum", "missing-base"])
@pytest.mark.parametrize("operation", ["author", "mirror", "prune"])
def test_corrupt_packs_refuse_authoring_and_mirroring_without_writes(
    authoring_wiki, kind, corruption, operation
):
    root, rebuild = authoring_wiki
    source = root / "source.md"
    wiki = root / "wiki"
    source.write_text(_body("Safe existing body"))
    _add(root, kind)
    rebuild()
    source.write_text(_body("Proposed replacement body"))
    if operation == "mirror":
        _installed_path(root, kind).write_text(source.read_text())
    elif operation == "prune":
        _installed_path(root, kind).unlink()
    if corruption == "checksum":
        pages_file = next((wiki / "wiki-packs").rglob("pages.jsonl"))
        pages_file.write_text(pages_file.read_text() + "corrupt\n")
    else:
        for manifest in (wiki / "wiki-packs").rglob("wiki-pack-manifest.json"):
            manifest.unlink()
    before = _snapshot(root)
    with pytest.raises(WikiPackManifestError):
        if operation == "author":
            _add(root, kind, update=True)
        else:
            _mirror(root, kind, prune=operation == "prune")
    assert _snapshot(root) == before


@pytest.mark.parametrize("kind", ["skill", "agent"])
def test_mirror_publication_failure_restores_loose_body_and_active_packs(
    authoring_wiki, monkeypatch, kind
):
    root, rebuild = authoring_wiki
    (root / "source.md").write_text(_body("Retained body"))
    _add(root, kind)
    rebuild()
    (root / "wiki" / _rich_path(kind)).write_bytes(
        _body("Retained body").replace("\n", "\r\n").encode("utf-8")
    )
    _installed_path(root, kind).write_text(_body("Proposed mirror"))
    before = _snapshot(root)

    def fail_publish(**_kwargs):
        raise OSError("overlay write unavailable")

    monkeypatch.setattr(wiki_bodies, "write_active_wiki_overlay_pack", fail_publish)
    with pytest.raises(OSError, match="overlay write unavailable"):
        _mirror(root, kind)
    assert _snapshot(root) == before


@pytest.mark.parametrize("empty_body", ["", " \n\t"])
def test_forced_skill_mirror_rejects_empty_packed_page_without_writes(
    authoring_wiki, empty_body
):
    root, rebuild = authoring_wiki
    (root / "source.md").write_text(_body("Last good skill instructions"))
    _add(root, "skill")
    rebuild()
    _installed_path(root, "skill").write_text(empty_body)
    before = _snapshot(root)
    with pytest.raises(ValueError, match="must not be empty"):
        _mirror(root, "skill")
    assert _snapshot(root) == before
    merged = load_merged_wiki_pages(root / "wiki" / "wiki-packs")
    assert merged[_rich_path("skill")] == _body("Last good skill instructions")


def test_shortened_skill_mirror_can_be_pruned_after_reference_retirement(authoring_wiki):
    root, rebuild = authoring_wiki
    wiki = root / "wiki"
    source = root / "source.md"
    source.write_text(_long_skill("Retired long instructions", tables=1))
    assert _add(root, "skill")["converted"] is True
    rebuild()
    source.write_text(_body("Short surviving instructions"))
    assert _add(root, "skill", update=True)["converted"] is False
    assert "Short surviving instructions" in rebuild()["skill:author-tool"]
    _installed_path(root, "skill").unlink()
    assert [result.status for result in _mirror(root, "skill", prune=True)] == ["pruned"]
    assert _rich_path("skill") not in load_merged_wiki_pages(wiki / "wiki-packs")
    semantic_body = rebuild()["skill:author-tool"]
    assert "Short surviving instructions" not in semantic_body
    assert "Retired long instructions" not in semantic_body
