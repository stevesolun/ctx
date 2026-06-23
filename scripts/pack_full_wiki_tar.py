from __future__ import annotations

import argparse
import io
import json
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ctx.core.wiki.wiki_packs import load_merged_wiki_pages, write_wiki_base_pack

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_GRAPH_MANIFEST = "graphify-out/graph-export-manifest.json"
_REQUIRED_EXPANDED_MARKDOWN = frozenset({"graphify-out/graph-report.md"})


@dataclass(frozen=True)
class RepackStats:
    export_id: str
    packed_pages: int
    removed_expanded_markdown_pages: int
    target: Path


def repack_full_wiki_tar(source: Path, target: Path | None = None) -> RepackStats:
    """Write a full wiki tarball that carries entity pages in wiki-packs."""
    source = Path(source)
    target = source if target is None else Path(target)
    if not source.is_file():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ctx-wiki-pack-") as tmp_name:
        tmp_root = Path(tmp_name)
        existing_pack_root = tmp_root / "existing-wiki-packs"
        pages: dict[str, str] = {}
        export_id: str | None = None
        with tarfile.open(source, "r:gz") as src:
            for member in src:
                name = _safe_tar_name(member.name)
                if name.startswith("wiki-packs/"):
                    _copy_existing_wiki_pack_member(src, member, name, existing_pack_root)
                    continue
                if not member.isfile():
                    continue
                if name == _GRAPH_MANIFEST:
                    export_id = _read_export_id(src, member)
                if not _should_pack_markdown_page(name):
                    continue
                extracted = src.extractfile(member)
                if extracted is None:
                    raise ValueError(f"archive file is unreadable: {member.name}")
                with extracted:
                    pages[name] = _normalise_page_text(
                        extracted.read().decode("utf-8", errors="replace")
                    )
        if not export_id:
            raise ValueError(f"{source} is missing graph export id")
        if existing_pack_root.exists():
            pages.update(load_merged_wiki_pages(existing_pack_root))

        pack_root = tmp_root / "wiki-packs"
        write_wiki_base_pack(
            pack_dir=pack_root / f"base-{export_id}",
            pack_id=f"base-{export_id}",
            base_export_id=export_id,
            pages=pages,
        )
        _validate_pack_payload(pack_root, pages)
        removed = _rewrite_tar_with_pack(source, target, pack_root, pages)
    return RepackStats(
        export_id=export_id,
        packed_pages=len(pages),
        removed_expanded_markdown_pages=removed,
        target=target,
    )


def _rewrite_tar_with_pack(
    source: Path,
    target: Path,
    pack_root: Path,
    pages: dict[str, str],
) -> int:
    tmp_target = target.with_name(f".{target.name}.tmp")
    tmp_target.unlink(missing_ok=True)
    removed = 0
    written_names: set[str] = set()
    try:
        with tarfile.open(source, "r:gz") as src, tarfile.open(
            tmp_target,
            "w:gz",
            compresslevel=9,
        ) as dst:
            for member in src:
                name = _safe_tar_name(member.name)
                if name.startswith("wiki-packs/"):
                    continue
                if _is_transient_member(name):
                    continue
                if _should_skip_expanded_markdown_member(name):
                    removed += 1
                    continue
                if member.isfile():
                    extracted = src.extractfile(member)
                    if extracted is None:
                        raise ValueError(f"archive file is unreadable: {member.name}")
                    with extracted:
                        member.name = name
                        dst.addfile(member, extracted)
                    written_names.add(name)
                elif member.isdir():
                    member.name = name
                    dst.addfile(member)
                    written_names.add(name)
                else:
                    raise ValueError(f"unsupported archive member: {member.name}")
            for name in sorted(_REQUIRED_EXPANDED_MARKDOWN - written_names):
                text = pages.get(name)
                if text is not None:
                    _add_text(dst, name=name, text=text)
            for path in sorted(pack_root.rglob("*")):
                if path.is_file():
                    dst.add(path, arcname=path.relative_to(pack_root.parent).as_posix())
        os.replace(tmp_target, target)
    finally:
        tmp_target.unlink(missing_ok=True)
    return removed


def _copy_existing_wiki_pack_member(
    tf: tarfile.TarFile,
    member: tarfile.TarInfo,
    name: str,
    packs_dir: Path,
) -> None:
    if not member.isfile():
        return
    relpath = name.removeprefix("wiki-packs/")
    if not relpath:
        return
    target = packs_dir.joinpath(*PurePosixPath(relpath).parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    extracted = tf.extractfile(member)
    if extracted is None:
        raise ValueError(f"archive file is unreadable: {member.name}")
    with extracted, target.open("wb") as out:
        out.write(extracted.read())


def _read_export_id(tf: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    extracted = tf.extractfile(member)
    if extracted is None:
        raise ValueError(f"archive file is unreadable: {member.name}")
    with extracted:
        payload = json.loads(extracted.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{_GRAPH_MANIFEST} must contain a JSON object")
    export_id = payload.get("export_id")
    if not isinstance(export_id, str) or not export_id.strip():
        raise ValueError(f"{_GRAPH_MANIFEST} is missing export_id")
    return export_id.strip()


def _validate_pack_payload(pack_root: Path, expected_pages: dict[str, str]) -> None:
    pages = load_merged_wiki_pages(pack_root)
    if pages != expected_pages:
        raise ValueError("wiki pack payload does not match source markdown pages")


def _normalise_page_text(text: str) -> str:
    return text if text.strip() else "<!-- empty markdown page -->\n"


def _safe_tar_name(raw_name: str) -> str:
    name = raw_name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or _WINDOWS_DRIVE_RE.match(name)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"unsafe archive member path: {raw_name}")
    return path.as_posix()


def _is_transient_member(name: str) -> bool:
    return (
        name.endswith(".original")
        or name.endswith(".lock")
        or name == ".ctx"
        or name.startswith(".ctx/")
    )


def _is_high_fanout_entity_page(name: str) -> bool:
    prefixes = (
        "entities/skills/",
        "entities/agents/",
        "entities/mcp-servers/",
    )
    return name.startswith(prefixes) and name.endswith(".md")


def _should_pack_markdown_page(name: str) -> bool:
    return name.endswith(".md") and not name.startswith("wiki-packs/")


def _should_skip_expanded_markdown_member(name: str) -> bool:
    return (
        name.endswith(".md")
        and name not in _REQUIRED_EXPANDED_MARKDOWN
        and "/" in name
        and not name.startswith("entities/harnesses/")
    )


def _add_text(tf: tarfile.TarFile, *, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    info.mtime = 0
    tf.addfile(info, io.BytesIO(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repack ctx full wiki tarball with markdown pages in wiki-packs.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("graph/wiki-graph.tar.gz"),
        help="Existing full wiki tarball.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        help="Destination tarball. Defaults to rewriting --source atomically.",
    )
    args = parser.parse_args(argv)
    stats = repack_full_wiki_tar(args.source, args.target)
    print(
        "packed "
        f"{stats.packed_pages:,} markdown pages for {stats.export_id}; "
        f"removed {stats.removed_expanded_markdown_pages:,} expanded markdown pages; "
        f"wrote {stats.target}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
