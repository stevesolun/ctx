"""
test_skill_quality_list.py -- Tests for cmd_list lifecycle-sidecar filtering (P2-11).

Asserts that lifecycle sidecars written by ctx_lifecycle do not appear in
``skill_quality list`` output, even when they land in the same sidecar directory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import skill_quality as sq  # noqa: E402


def _write_quality_sidecar(sidecar_dir: Path, slug: str, grade: str = "B") -> Path:
    path = sidecar_dir / f"{slug}.json"
    data = {
        "slug": slug,
        "subject_type": "skill",
        "raw_score": 0.65,
        "score": 0.65,
        "grade": grade,
        "hard_floor": None,
        "signals": {},
        "weights": {},
        "computed_at": "2026-04-19T12:00:00+00:00",
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_lifecycle_sidecar(sidecar_dir: Path, slug: str) -> Path:
    path = sidecar_dir / f"{slug}.lifecycle.json"
    data = {
        "slug": slug,
        "subject_type": "skill",
        "state": "demote",
        "state_since": "2026-04-10T00:00:00+00:00",
        "consecutive_d_count": 3,
        "last_grade": "D",
        "last_seen_computed_at": "2026-04-19T12:00:00+00:00",
        "history": [],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestCmdListFiltersLifecycleSidecars:
    def test_lifecycle_sidecar_absent_from_text_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A .lifecycle.json file in the sidecar dir must not appear in list output."""
        sidecar_dir = tmp_path / "quality"
        sidecar_dir.mkdir()

        _write_quality_sidecar(sidecar_dir, "my-skill", grade="B")
        _write_lifecycle_sidecar(sidecar_dir, "my-skill")

        monkeypatch.setattr(sq, "default_sidecar_dir", lambda: sidecar_dir)

        args = _make_list_args(grade=None, json_out=False)
        rc = sq.cmd_list(args)
        captured = capsys.readouterr()

        assert rc == 0
        # The quality sidecar slug appears in output.
        assert "my-skill" in captured.out
        # The lifecycle sidecar must not appear as a separate row.
        # One row for quality sidecar means exactly one slug mention in stdout.
        lines = [ln for ln in captured.out.splitlines() if "my-skill" in ln]
        assert len(lines) == 1, (
            f"Expected 1 row for my-skill, got {len(lines)}: {lines}"
        )

    def test_lifecycle_sidecar_absent_from_json_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A .lifecycle.json file must not appear in --json list output."""
        sidecar_dir = tmp_path / "quality"
        sidecar_dir.mkdir()

        _write_quality_sidecar(sidecar_dir, "skill-alpha", grade="A")
        _write_lifecycle_sidecar(sidecar_dir, "skill-alpha")
        # A second quality sidecar with no lifecycle sidecar.
        _write_quality_sidecar(sidecar_dir, "skill-beta", grade="C")

        monkeypatch.setattr(sq, "default_sidecar_dir", lambda: sidecar_dir)

        args = _make_list_args(grade=None, json_out=True)
        rc = sq.cmd_list(args)
        captured = capsys.readouterr()

        assert rc == 0
        rows = json.loads(captured.out)
        slugs = [r.get("slug") for r in rows]
        # Both quality slugs appear.
        assert "skill-alpha" in slugs
        assert "skill-beta" in slugs
        # No entry should be the lifecycle sidecar (lacks 'grade').
        for row in rows:
            assert "grade" in row, f"Row missing 'grade': {row}"

    def test_pure_lifecycle_sidecar_directory_returns_zero_rows(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A directory containing only lifecycle sidecars yields zero quality rows."""
        sidecar_dir = tmp_path / "quality"
        sidecar_dir.mkdir()

        _write_lifecycle_sidecar(sidecar_dir, "orphan-skill")

        monkeypatch.setattr(sq, "default_sidecar_dir", lambda: sidecar_dir)

        args = _make_list_args(grade=None, json_out=True)
        rc = sq.cmd_list(args)
        captured = capsys.readouterr()

        assert rc == 0
        rows = json.loads(captured.out)
        assert rows == [], f"Expected empty list, got: {rows}"


def _make_list_args(*, grade: str | None, json_out: bool) -> object:
    """Return a namespace-like object for cmd_list."""
    import argparse
    ns = argparse.Namespace()
    ns.grade = grade
    ns.json = json_out
    return ns
