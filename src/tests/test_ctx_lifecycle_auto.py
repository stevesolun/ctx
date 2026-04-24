"""
test_ctx_lifecycle_auto.py -- Tests for --auto non-interactive mode (P2-12).

Asserts that running with auto=True completes without raising EOFError
or blocking on input(), even when items cross the archive threshold.

Delete always requires typed-slug confirmation and must never be
auto-applied; under --auto + delete-threshold-reached the entry is
logged and skipped.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import ctx_lifecycle as lc  # noqa: E402
from ctx.core.quality.quality_signals import SignalResult  # noqa: E402
from skill_quality import QualityScore  # noqa: E402


NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _score(
    slug: str = "auto-skill",
    grade: str = "D",
    score: float = 0.3,
    *,
    computed_at: datetime | None = None,
) -> QualityScore:
    ts = computed_at or NOW
    signals = {
        name: SignalResult(score=0.5, evidence={})
        for name in ("telemetry", "intake", "graph", "routing")
    }
    return QualityScore(
        slug=slug,
        subject_type="skill",
        raw_score=score,
        score=score,
        grade=grade,
        hard_floor=None,
        signals=signals,
        weights={"telemetry": 0.4, "intake": 0.2, "graph": 0.25, "routing": 0.15},
        computed_at=_iso(ts),
    )


def _write_quality_sidecar(sidecar_dir: Path, score: QualityScore) -> None:
    path = sidecar_dir / f"{score.slug}.json"
    path.write_text(
        json.dumps(score.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_lifecycle_sidecar(sidecar_dir: Path, state: lc.LifecycleState) -> None:
    path = sidecar_dir / f"{state.slug}.lifecycle.json"
    path.write_text(
        json.dumps(state.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture()
def _sources(tmp_path: Path) -> lc.LifecycleSources:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    sidecar_dir = tmp_path / "quality"
    skills_dir.mkdir()
    agents_dir.mkdir()
    sidecar_dir.mkdir()
    return lc.LifecycleSources(
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        sidecar_dir=sidecar_dir,
    )


class TestAutoModeNoEOFError:
    """_apply_buckets with auto=True must never call input()."""

    def test_auto_watch_demote_no_prompt(
        self, _sources: lc.LifecycleSources, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Watch + Demote transitions applied without any prompt under --auto."""
        prompt_called = []

        def _fake_prompt(q: str, **kwargs: object) -> bool:
            prompt_called.append(q)
            raise EOFError("should not have been called")

        monkeypatch.setattr(lc, "_prompt_yes_no", _fake_prompt)

        cfg = lc.LifecycleConfig(consecutive_d_to_demote=1)
        slug = "auto-skill"

        # Seed a quality sidecar with grade D.
        score = _score(slug=slug, grade="D")
        _write_quality_sidecar(_sources.sidecar_dir, score)

        # State with one D streak already recorded.
        state = lc.LifecycleState(
            slug=slug, subject_type="skill", consecutive_d_count=1, last_grade="D"
        )
        _write_lifecycle_sidecar(_sources.sidecar_dir, state)

        # Create a skill directory so the demote filesystem move succeeds.
        skill_dir = _sources.skills_dir / slug
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: auto-skill\n---\n# Body\n", encoding="utf-8")

        proposals, observed = lc.plan_review(sources=_sources, cfg=cfg, now=NOW)

        # Should have a demote proposal.
        assert any(p.target_state == lc.STATE_DEMOTE for p in proposals), (
            f"Expected a demote proposal, got: {[p.target_state for p in proposals]}"
        )

        buckets = lc._partition(proposals)

        # This must not raise EOFError or call _prompt_yes_no.
        applied = lc._apply_buckets(
            buckets, observed, sources=_sources, cfg=cfg, auto=True
        )

        assert prompt_called == [], f"Unexpected prompt calls: {prompt_called}"
        assert applied >= 1

    def test_auto_skips_archive_without_prompting(
        self, _sources: lc.LifecycleSources, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Archive candidates are deferred (not applied) under --auto, no prompt raised."""
        prompt_called = []

        def _fake_prompt(q: str, **kwargs: object) -> bool:
            prompt_called.append(q)
            raise EOFError("should not have been called")

        monkeypatch.setattr(lc, "_prompt_yes_no", _fake_prompt)

        cfg = lc.LifecycleConfig(archive_threshold_days=1.0)
        slug = "old-skill"

        # State already in demote, state_since 2 days ago → archive candidate.
        old_since = _iso(NOW - timedelta(days=2))
        state = lc.LifecycleState(
            slug=slug,
            subject_type="skill",
            state=lc.STATE_DEMOTE,
            state_since=old_since,
        )
        _write_lifecycle_sidecar(_sources.sidecar_dir, state)

        proposals, observed = lc.plan_review(
            sources=_sources, cfg=cfg, now=NOW, include_delete=False
        )
        assert any(p.target_state == lc.STATE_ARCHIVE for p in proposals), (
            f"Expected archive proposal, got: {[p.target_state for p in proposals]}"
        )

        buckets = lc._partition(proposals)

        # Must complete without EOFError and without calling _prompt_yes_no.
        applied = lc._apply_buckets(
            buckets, observed, sources=_sources, cfg=cfg, auto=True
        )

        assert prompt_called == [], f"Unexpected prompt calls: {prompt_called}"
        # Archive was deferred, so nothing should have been applied.
        assert applied == 0
        captured = capsys.readouterr()
        assert "deferred" in captured.out.lower() or "auto" in captured.out.lower()

    def test_auto_never_applies_delete(
        self, _sources: lc.LifecycleSources, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delete candidates are never auto-applied; cmd_purge always requires typed confirmation."""
        cfg = lc.LifecycleConfig(delete_threshold_days=1.0)
        slug = "dead-skill"

        old_since = _iso(NOW - timedelta(days=2))
        state = lc.LifecycleState(
            slug=slug,
            subject_type="skill",
            state=lc.STATE_ARCHIVE,
            state_since=old_since,
        )
        _write_lifecycle_sidecar(_sources.sidecar_dir, state)

        proposals, _ = lc.plan_review(
            sources=_sources, cfg=cfg, now=NOW, include_delete=True
        )
        delete_candidates = [p for p in proposals if p.target_state == "deleted"]
        assert delete_candidates, "Expected at least one delete candidate"

        for p in delete_candidates:
            assert not p.auto_safe, (
                f"Delete proposal for {p.slug} must have auto_safe=False"
            )
            assert p.requires_typed_confirmation, (
                f"Delete proposal for {p.slug} must require typed confirmation"
            )
