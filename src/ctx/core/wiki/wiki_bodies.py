"""Publish authored body snapshots to loose mirrors and active wiki packs."""

from __future__ import annotations

from pathlib import Path

from ctx.core.wiki.wiki_packs import (
    WikiPackManifestError,
    discover_wiki_pack_manifests,
    load_merged_wiki_pages,
    write_active_wiki_overlay_pack,
)
from ctx.core.wiki.wiki_utils import validate_skill_name
from ctx.utils._fs_utils import reject_symlink_path, safe_atomic_write_text


def active_wiki_pages(wiki_path: Path) -> dict[str, str] | None:
    """Read the active authoritative pages, or None before packs exist."""
    packs_dir = wiki_path / "wiki-packs"
    reject_symlink_path(packs_dir)
    if not packs_dir.exists():
        return None
    if not packs_dir.is_dir() or not discover_wiki_pack_manifests(packs_dir):
        raise WikiPackManifestError("active wiki packs must contain a valid base pack")
    return load_merged_wiki_pages(packs_dir)


class WikiBodySnapshot:
    """Validated comparison pages scoped to one mirror operation."""

    def __init__(self, wiki_path: Path) -> None:
        self.wiki_path = wiki_path
        self._loaded = False
        self._pages: dict[str, str] | None = None
        self._skill_names: set[str] = set()

    @property
    def pages(self) -> dict[str, str] | None:
        if not self._loaded:
            self.refresh()
        return self._pages

    def has_skill(self, name: str) -> bool:
        if not self._loaded:
            self.refresh()
        return name in self._skill_names

    def refresh(self) -> None:
        pages = active_wiki_pages(self.wiki_path)
        skill_names = {
            path.split("/", 2)[1]
            for path in pages or {}
            if path.startswith("converted/") and len(path.split("/", 2)) == 3
        }
        self._pages = pages
        self._skill_names = skill_names
        self._loaded = True


def skill_body_path(relative_path: str) -> bool:
    """Identify the files owned by a converted skill body."""
    return relative_path in {
        "SKILL.md",
        "SKILL.md.original",
        "original-hash.txt",
        "check-gates.md",
        "failure-log.md",
    } or (relative_path.startswith("references/") and relative_path.endswith(".md"))


def replace_skill_body(wiki_path: Path, name: str, files: dict[str, str]) -> None:
    """Replace one authored skill body and remove its retired reference pages."""
    validate_skill_name(name)
    prefix = f"converted/{name}/"
    if any(not skill_body_path(path) for path in files):
        raise ValueError("skill body contains an unsupported file")
    active = active_wiki_pages(wiki_path)
    previous = {
        path
        for path in active or {}
        if path.startswith(prefix) and skill_body_path(path[len(prefix) :])
    }
    root = wiki_path / prefix
    reject_symlink_path(root)
    if root.is_dir():
        previous.update(
            path.relative_to(wiki_path).as_posix()
            for path in root.rglob("*")
            if path.is_file() and skill_body_path(path.relative_to(root).as_posix())
        )
    _replace_body(
        wiki_path,
        {prefix + path: text for path, text in files.items()},
        previous,
        active,
    )


def replace_agent_body(wiki_path: Path, name: str, content: str | None) -> None:
    """Replace or retire one authored agent prompt."""
    validate_skill_name(name)
    path = f"converted-agents/{name}.md"
    _replace_body(
        wiki_path,
        {} if content is None else {path: content},
        {path},
        active_wiki_pages(wiki_path),
    )


def _replace_body(
    wiki_path: Path,
    files: dict[str, str],
    previous: set[str],
    active: dict[str, str] | None,
) -> None:
    if any(not text.strip() for path, text in files.items() if path.endswith(".md")):
        raise ValueError("wiki body markdown must not be empty")
    old_contents: dict[Path, str | None] = {}
    for relative in sorted(previous | files.keys()):
        if Path(relative).is_absolute() or ".." in Path(relative).parts or "\\" in relative:
            raise ValueError("wiki body paths must remain within the wiki")
        path = wiki_path / relative
        reject_symlink_path(path)
        old_contents[path] = path.read_bytes().decode("utf-8") if path.exists() else None
    pages = {path: text for path, text in files.items() if path.endswith(".md")}
    tombstones = sorted(path for path in previous - files.keys() if path.endswith(".md"))
    try:
        for relative, text in sorted(files.items()):
            safe_atomic_write_text(wiki_path / relative, text, encoding="utf-8")
        for relative in sorted(previous - files.keys()):
            (wiki_path / relative).unlink(missing_ok=True)
        reference_dirs = {
            wiki_path / parent
            for relative in previous - files.keys()
            if "/references/" in relative
            for parent in Path(relative).parents
            if "references" in parent.parts
        }
        for directory in sorted(reference_dirs, reverse=True):
            reject_symlink_path(directory)
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        if active is not None:
            changed = {path: text for path, text in pages.items() if active.get(path) != text}
            deleted = [path for path in tombstones if path in active]
            if changed or deleted:
                manifest = write_active_wiki_overlay_pack(
                    packs_dir=wiki_path / "wiki-packs",
                    pages=changed,
                    tombstones=deleted,
                )
                if manifest is None:
                    raise WikiPackManifestError("active wiki base disappeared during body update")
    except Exception:
        for path, text in old_contents.items():
            if text is None:
                reject_symlink_path(path)
                path.unlink(missing_ok=True)
            else:
                safe_atomic_write_text(path, text, encoding="utf-8")
        raise
