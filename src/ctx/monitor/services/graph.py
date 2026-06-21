"""Read-only graph artifact loading helpers for ctx-monitor."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


_GRAPH_CACHE_KEY: tuple[Any, ...] | None = None
_GRAPH_CACHE_VALUE: Any | None = None


def reset_caches() -> None:
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE

    _GRAPH_CACHE_KEY = None
    _GRAPH_CACHE_VALUE = None


def cached_dashboard_graph() -> Any | None:
    return _GRAPH_CACHE_VALUE


def dashboard_file_cache_key(path: Path) -> tuple[str, float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), stat.st_mtime, stat.st_size)


def dashboard_graph_pack_cache_key(
    packs_dir: Path,
) -> tuple[tuple[str, float, int], ...]:
    if not packs_dir.is_dir():
        return ()
    try:
        files = sorted(path for path in packs_dir.rglob("*") if path.is_file())
    except OSError:
        return (("<unreadable>", 0.0, 0),)
    rows: list[tuple[str, float, int]] = []
    for path in files:
        try:
            stat = path.stat()
            relpath = path.relative_to(packs_dir).as_posix()
        except OSError:
            rows.append((path.name, 0.0, 0))
            continue
        rows.append((relpath, stat.st_mtime, stat.st_size))
    return tuple(rows)


def dashboard_graph_source_cache_key(
    graph_path: Path,
    overlay_path: Path,
) -> tuple[Any, ...] | None:
    graph_key = dashboard_file_cache_key(graph_path)
    overlay_key = dashboard_file_cache_key(overlay_path)
    pack_key = dashboard_graph_pack_cache_key(graph_path.parent / "packs")
    if graph_key is None and not pack_key:
        return None
    return (graph_key, overlay_key, pack_key)


def load_dashboard_graph(
    wiki_dir: Path,
    load_graph: Callable[..., Any],
) -> Any:
    """Load the dashboard graph once per graph artifact version."""
    global _GRAPH_CACHE_KEY, _GRAPH_CACHE_VALUE

    graph_path = wiki_dir / "graphify-out" / "graph.json"
    overlay_path = graph_path.with_name("entity-overlays.jsonl")
    source_key = dashboard_graph_source_cache_key(graph_path, overlay_path)
    if source_key is None:
        _GRAPH_CACHE_KEY = None
        _GRAPH_CACHE_VALUE = None
        return load_graph(graph_path)

    cache_key = (id(load_graph), source_key)
    if _GRAPH_CACHE_KEY == cache_key and _GRAPH_CACHE_VALUE is not None:
        return _GRAPH_CACHE_VALUE

    try:
        graph = load_graph(graph_path, apply_runtime_filter=False)
    except TypeError:
        graph = load_graph(graph_path)
    _GRAPH_CACHE_KEY = cache_key
    _GRAPH_CACHE_VALUE = graph
    return graph
