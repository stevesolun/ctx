"""
test_skill_quality_bench.py — P1-12 performance regression test.

Creates 1,000 fake telemetry events spread across 100 slugs and asserts
that ``recompute_all`` completes in under 2 seconds, confirming the
O(M) single-scan behaviour (vs the previous O(N·M) per-slug rescan).
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skill_quality as sq  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ────────────────────────────────────────────────────────────────────

_NUM_SLUGS = 100
_EVENTS_PER_SLUG = 10  # 1,000 total events
_NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)

_SKILL_MD = (
    "---\nname: {slug}\ndescription: Benchmark skill {slug}.\n---\n"
    "# {slug}\n\n" + "Body content for the benchmark skill. " * 10 + "\n"
)


def _make_bench_tree(tmp_path: Path) -> sq.SignalSources:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    wiki_dir = tmp_path / "wiki"
    events_path = tmp_path / "events.jsonl"

    skills_dir.mkdir()
    agents_dir.mkdir()
    wiki_dir.mkdir()

    slugs = [f"bench-skill-{i:03d}" for i in range(_NUM_SLUGS)]

    # Create skill files.
    for slug in slugs:
        skill_dir = skills_dir / slug
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            _SKILL_MD.format(slug=slug), encoding="utf-8"
        )

    # Create JSONL with events spread evenly across slugs.
    ts = _NOW.isoformat(timespec="seconds")
    lines = []
    for i in range(_NUM_SLUGS * _EVENTS_PER_SLUG):
        slug = slugs[i % _NUM_SLUGS]
        lines.append(json.dumps({"skill": slug, "event": "load", "timestamp": ts}))
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return sq.SignalSources(
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        wiki_dir=wiki_dir,
        events_path=events_path,
    )


# ────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────


def test_recompute_all_completes_under_two_seconds(tmp_path: Path) -> None:
    """recompute_all with 100 slugs / 1,000 events must finish < 2 s."""
    sources = _make_bench_tree(tmp_path)
    sidecar_dir = tmp_path / "sidecars"
    sidecar_dir.mkdir()

    start = time.monotonic()
    successes, failures = sq.recompute_all(
        sources=sources,
        now=_NOW,
        sidecar_dir=sidecar_dir,
        update_frontmatter=False,
    )
    elapsed = time.monotonic() - start

    assert failures == [], f"Unexpected failures: {failures}"
    assert len(successes) == _NUM_SLUGS
    assert elapsed < 2.0, (
        f"recompute_all took {elapsed:.2f}s for {_NUM_SLUGS} slugs / "
        f"{_NUM_SLUGS * _EVENTS_PER_SLUG} events — expected < 2.0s"
    )


def test_events_index_used_single_read(tmp_path: Path) -> None:
    """_build_events_index partitions events correctly by slug."""
    sources = _make_bench_tree(tmp_path)

    index = sq._build_events_index(sources.events_path)

    assert len(index) == _NUM_SLUGS
    for slug, events in index.items():
        assert len(events) == _EVENTS_PER_SLUG, (
            f"{slug} has {len(events)} events, expected {_EVENTS_PER_SLUG}"
        )
        assert all(e["skill"] == slug for e in events)


def test_events_index_empty_file(tmp_path: Path) -> None:
    """_build_events_index returns an empty dict for a missing or empty file."""
    missing = tmp_path / "does_not_exist.jsonl"
    assert sq._build_events_index(missing) == {}

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert sq._build_events_index(empty) == {}
