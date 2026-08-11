"""Docs page renderer for the ctx dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ctx import dashboard_docs


def docs_roots(cwd: Path, source_root: Path) -> list[Path]:
    return dashboard_docs.docs_roots(cwd, source_root)


def docs_render_disk_cache_path(claude_dir: Path) -> Path:
    return dashboard_docs.docs_render_disk_cache_path(claude_dir)


def docs_index_entries(roots: list[Path]) -> list[dict[str, Any]]:
    return dashboard_docs.docs_index_entries(roots)


def docs_tabs(
    entries: list[dict[str, Any]],
    roots: list[Path],
) -> list[dict[str, Any]]:
    return dashboard_docs.docs_tabs(entries, roots)


def render_docs_markdown(
    markdown_text: str,
    page_anchor: str,
    *,
    fallback_renderer: Callable[[str], str],
) -> str:
    return dashboard_docs.render_docs_markdown(
        markdown_text,
        page_anchor,
        fallback_renderer=fallback_renderer,
    )


def render_docs(
    *,
    roots: list[Path],
    layout: Callable[[str, str], str],
    asset_text: Callable[[str], str],
    inline_script: Callable[[str], str],
    cache_path: Path,
    fallback_markdown: Callable[[str], str],
    cache_salt_paths: tuple[Path, ...],
    cache_extra: tuple[Any, ...],
    index_entries: Callable[[], list[dict[str, Any]]],
    tabs_for_entries: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    render_markdown_func: Callable[[str, str], str],
) -> str:
    return dashboard_docs.render_docs(
        roots=roots,
        layout=layout,
        asset_text=asset_text,
        inline_script=inline_script,
        cache_path=cache_path,
        fallback_markdown=fallback_markdown,
        cache_salt_paths=cache_salt_paths,
        cache_extra=cache_extra,
        index_entries=index_entries,
        tabs_for_entries=tabs_for_entries,
        render_markdown_func=render_markdown_func,
    )
