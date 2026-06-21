"""Small disk-cache helpers shared by monitor services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ctx.utils._fs_utils import atomic_write_text


def disk_cache_token(cache_key: tuple[Any, ...]) -> str:
    return json.dumps(cache_key, separators=(",", ":"), sort_keys=True)


def read_disk_cache_payload(path: Path, cache_token: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != 1 or data.get("cache_token") != cache_token:
        return None
    return data


def write_disk_cache_payload(
    path: Path,
    cache_token: str,
    payload: dict[str, Any],
    *,
    sort_keys: bool = False,
) -> None:
    try:
        atomic_write_text(
            path,
            json.dumps(
                {
                    "schema_version": 1,
                    "cache_token": cache_token,
                    **payload,
                },
                ensure_ascii=False,
                sort_keys=sort_keys,
            )
            + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        return


def read_html_disk_cache(path: Path, cache_token: str) -> str | None:
    data = read_disk_cache_payload(path, cache_token)
    if data is None:
        return None
    html_text = data.get("html")
    return html_text if isinstance(html_text, str) else None


def write_html_disk_cache(path: Path, cache_token: str, html_text: str) -> None:
    write_disk_cache_payload(path, cache_token, {"html": html_text})
