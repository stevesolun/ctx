"""KPI summary loading and caching for ctx-monitor."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ctx.monitor.services import cache as cache_service

SUMMARY_CACHE_SECONDS = 30

_SUMMARY_CACHE_KEY: tuple[Any, ...] | None = None
_SUMMARY_CACHE_VALUE: Any | None = None
_SUMMARY_CACHE_AT = 0.0


def reset_cache() -> None:
    global _SUMMARY_CACHE_AT, _SUMMARY_CACHE_KEY, _SUMMARY_CACHE_VALUE
    _SUMMARY_CACHE_KEY = None
    _SUMMARY_CACHE_VALUE = None
    _SUMMARY_CACHE_AT = 0.0


def summary_cache_key(sidecar_dir: Path) -> tuple[Any, ...]:
    parts: list[tuple[str, str, int, int, int, int]] = []
    for root in (sidecar_dir, sidecar_dir / "mcp"):
        try:
            root_name = str(root.resolve())
        except OSError:
            root_name = str(root)
        buckets = {
            "quality": [0, 0, 0, 0],
            "lifecycle": [0, 0, 0, 0],
        }
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    name = entry.name
                    if (
                        name.startswith(".")
                        or not name.endswith(".json")
                        or not entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    bucket = "lifecycle" if name.endswith(".lifecycle.json") else "quality"
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    data = buckets[bucket]
                    data[0] += 1
                    data[1] += int(stat.st_size)
                    data[2] = max(data[2], int(stat.st_mtime_ns))
                    data[3] = (data[3] + int(stat.st_mtime_ns)) & ((1 << 63) - 1)
        except OSError:
            pass
        for bucket, values in buckets.items():
            parts.append((root_name, bucket, values[0], values[1], values[2], values[3]))
    return tuple(parts)


def summary_disk_cache_path(sidecar_dir: Path) -> Path:
    return sidecar_dir / ".dashboard-kpi-summary.json"


def dashboard_summary_from_dict(summary_cls: Any, data: Any) -> Any | None:
    if not isinstance(data, dict):
        return None

    def dict_field(name: str) -> dict[str, Any]:
        value = data.get(name)
        return dict(value) if isinstance(value, dict) else {}

    def list_field(name: str) -> list[dict[str, Any]]:
        value = data.get(name)
        if not isinstance(value, list):
            return []
        return [dict(item) for item in value if isinstance(item, dict)]

    try:
        return summary_cls(
            generated_at=str(data.get("generated_at") or ""),
            total=int(data.get("total") or 0),
            by_subject=dict_field("by_subject"),
            grade_counts=dict_field("grade_counts"),
            lifecycle_counts=dict_field("lifecycle_counts"),
            category_breakdown=list_field("category_breakdown"),
            hard_floor_counts=dict_field("hard_floor_counts"),
            low_quality_candidates=list_field("low_quality_candidates"),
            archived=list_field("archived"),
        )
    except (TypeError, ValueError):
        return None


def read_summary_disk_cache(
    sidecar_dir: Path,
    cache_token: str,
    summary_cls: Any,
) -> Any | None:
    data = cache_service.read_disk_cache_payload(
        summary_disk_cache_path(sidecar_dir),
        cache_token,
    )
    if data is None:
        return None
    return dashboard_summary_from_dict(summary_cls, data.get("summary"))


def write_summary_disk_cache(
    sidecar_dir: Path,
    cache_token: str,
    summary: Any,
) -> None:
    cache_service.write_disk_cache_payload(
        summary_disk_cache_path(sidecar_dir),
        cache_token,
        {"summary": summary.to_dict()},
        sort_keys=True,
    )


def kpi_summary(sidecar_dir: Path, *, cache_seconds: int = SUMMARY_CACHE_SECONDS) -> Any | None:
    """Compute the KPI DashboardSummary using the default source layout.

    Returns ``None`` if the kpi_dashboard module can't be imported or the
    required directories don't exist. Callers render an explanatory empty state.
    """
    try:
        from ctx_lifecycle import LifecycleSources  # type: ignore
        from kpi_dashboard import DashboardSummary  # type: ignore
        from kpi_dashboard import generate  # type: ignore
    except Exception:  # noqa: BLE001 - KPIs are advisory.
        return None
    if not sidecar_dir.is_dir():
        return None
    cache_key = summary_cache_key(sidecar_dir)
    global _SUMMARY_CACHE_AT, _SUMMARY_CACHE_KEY, _SUMMARY_CACHE_VALUE
    if (
        _SUMMARY_CACHE_KEY == cache_key
        and _SUMMARY_CACHE_VALUE is not None
        and time.monotonic() - _SUMMARY_CACHE_AT < cache_seconds
    ):
        return _SUMMARY_CACHE_VALUE
    cache_token = cache_service.disk_cache_token(cache_key)
    summary = read_summary_disk_cache(sidecar_dir, cache_token, DashboardSummary)
    if summary is not None:
        _SUMMARY_CACHE_KEY = cache_key
        _SUMMARY_CACHE_VALUE = summary
        _SUMMARY_CACHE_AT = time.monotonic()
        return summary
    try:
        from ctx_config import cfg  # type: ignore

        sources = LifecycleSources(
            skills_dir=cfg.skills_dir,
            agents_dir=cfg.agents_dir,
            sidecar_dir=sidecar_dir,
        )
    except Exception:  # noqa: BLE001 - fallback: sidecar-only.
        sources = LifecycleSources(
            skills_dir=sidecar_dir,
            agents_dir=sidecar_dir,
            sidecar_dir=sidecar_dir,
        )
    try:
        summary = generate(sources=sources, top_n=25)
    except Exception:  # noqa: BLE001
        return None
    write_summary_disk_cache(sidecar_dir, cache_token, summary)
    _SUMMARY_CACHE_KEY = cache_key
    _SUMMARY_CACHE_VALUE = summary
    _SUMMARY_CACHE_AT = time.monotonic()
    return summary
