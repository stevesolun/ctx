"""
test_ctx_lifecycle.py -- Tests for the lifecycle state machine + CLI.

Covers:
  - Pure transitions (observe_score, classify_transition) with every tier.
  - Filesystem effects of apply_proposal (demote, archive, delete).
  - State sidecar round-trip + history truncation.
  - CLI review flow with monkeypatched inputs.
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
    slug: str = "demo",
    grade: str = "A",
    score: float = 0.85,
    *,
    subject_type: str = "skill",
    computed_at: datetime | None = None,
) -> QualityScore:
    ts = computed_at or NOW
    signals = {
        name: SignalResult(score=0.5, evidence={})
        for name in ("telemetry", "intake", "graph", "routing")
    }
    return QualityScore(
        slug=slug,
        subject_type=subject_type,
        raw_score=score,
        score=score,
        grade=grade,
        hard_floor=None,
        signals=signals,
        weights={"telemetry": 0.4, "intake": 0.2, "graph": 0.25, "routing": 0.15},
        computed_at=_iso(ts),
    )


# ────────────────────────────────────────────────────────────────────
# Config validation
# ────────────────────────────────────────────────────────────────────


class TestLifecycleConfig:
    def test_defaults_are_valid(self) -> None:
        cfg = lc.LifecycleConfig()
        assert cfg.archive_threshold_days == 14.0
        assert cfg.delete_threshold_days == 60.0
        assert cfg.consecutive_d_to_demote == 2

    def test_rejects_zero_thresholds(self) -> None:
        with pytest.raises(ValueError):
            lc.LifecycleConfig(archive_threshold_days=0)
        with pytest.raises(ValueError):
            lc.LifecycleConfig(delete_threshold_days=-1)

    def test_rejects_zero_streak(self) -> None:
        with pytest.raises(ValueError):
            lc.LifecycleConfig(consecutive_d_to_demote=0)

    def test_rejects_path_traversal_subdir(self) -> None:
        with pytest.raises(ValueError):
            lc.LifecycleConfig(demoted_subdir="../escape")
        with pytest.raises(ValueError):
            lc.LifecycleConfig(archive_subdir="")


# ────────────────────────────────────────────────────────────────────
# observe_score pure transitions
# ────────────────────────────────────────────────────────────────────


class TestObserveScore:
    def test_grade_d_increments_streak(self) -> None:
        s = lc.LifecycleState(slug="x", subject_type="skill")
        s1 = lc.observe_score(s, _score(grade="D", computed_at=NOW))
        assert s1.consecutive_d_count == 1
        s2 = lc.observe_score(
            s1, _score(grade="D", computed_at=NOW + timedelta(hours=1))
        )
        assert s2.consecutive_d_count == 2

    def test_grade_a_resets_streak(self) -> None:
        s = lc.LifecycleState(
            slug="x", subject_type="skill", consecutive_d_count=3,
        )
        s1 = lc.observe_score(
            s, _score(grade="A", computed_at=NOW + timedelta(hours=1))
        )
        assert s1.consecutive_d_count == 0
        assert s1.last_grade == "A"

    def test_grade_f_counts_as_negative(self) -> None:
        s = lc.LifecycleState(slug="x", subject_type="skill")
        s1 = lc.observe_score(s, _score(grade="F", computed_at=NOW))
        assert s1.consecutive_d_count == 1

    def test_idempotent_on_same_timestamp(self) -> None:
        s = lc.LifecycleState(
            slug="x", subject_type="skill",
            consecutive_d_count=1,
            last_seen_computed_at=_iso(NOW),
        )
        # Score with same computed_at — must not re-increment.
        s1 = lc.observe_score(s, _score(grade="D", computed_at=NOW))
        assert s1.consecutive_d_count == 1
        # Older timestamp — also a no-op.
        s2 = lc.observe_score(
            s1, _score(grade="D", computed_at=NOW - timedelta(days=1))
        )
        assert s2.consecutive_d_count == 1


# ────────────────────────────────────────────────────────────────────
# classify_transition logic
# ────────────────────────────────────────────────────────────────────


class TestClassifyTransition:
    def test_active_grade_c_proposes_watch(self) -> None:
        s = lc.LifecycleState(slug="x", subject_type="skill")
        p = lc.classify_transition(s, _score(grade="C"))
        assert p is not None
        assert p.target_state == lc.STATE_WATCH

    def test_watch_grade_c_no_repeat(self) -> None:
        s = lc.LifecycleState(
            slug="x", subject_type="skill", state=lc.STATE_WATCH,
        )
        p = lc.classify_transition(s, _score(grade="C"))
        # Still in Watch, nothing to propose.
        assert p is None

    def test_d_streak_triggers_demote(self) -> None:
        s = lc.LifecycleState(
            slug="x", subject_type="skill", state=lc.STATE_WATCH,
            consecutive_d_count=2,
        )
        p = lc.classify_transition(s, _score(grade="D"))
        assert p is not None
        assert p.target_state == lc.STATE_DEMOTE
        assert p.auto_safe is True  # demote is auto-safe

    def test_d_streak_below_threshold_no_demote(self) -> None:
        s = lc.LifecycleState(
            slug="x", subject_type="skill", state=lc.STATE_WATCH,
            consecutive_d_count=1,
        )
        p = lc.classify_transition(s, _score(grade="D"))
        assert p is None

    def test_demote_aged_proposes_archive(self) -> None:
        since = NOW - timedelta(days=20)
        s = lc.LifecycleState(
            slug="x", subject_type="skill", state=lc.STATE_DEMOTE,
            state_since=_iso(since),
        )
        p = lc.classify_transition(s, None, now=NOW)
        assert p is not None
        assert p.target_state == lc.STATE_ARCHIVE
        assert p.auto_safe is False  # archive is NOT auto-safe

    def test_demote_young_no_archive(self) -> None:
        since = NOW - timedelta(days=3)
        s = lc.LifecycleState(
            slug="x", subject_type="skill", state=lc.STATE_DEMOTE,
            state_since=_iso(since),
        )
        p = lc.classify_transition(s, None, now=NOW)
        assert p is None

    def test_archive_needs_include_delete_flag(self) -> None:
        since = NOW - timedelta(days=90)
        s = lc.LifecycleState(
            slug="x", subject_type="skill", state=lc.STATE_ARCHIVE,
            state_since=_iso(since),
        )
        # Default review flow does NOT propose delete.
        assert lc.classify_transition(s, None, now=NOW) is None
        # Purge flow does.
        p = lc.classify_transition(s, None, now=NOW, include_delete=True)
        assert p is not None
        assert p.target_state == "deleted"
        assert p.requires_typed_confirmation is True


# ────────────────────────────────────────────────────────────────────
# State sidecar round-trip
# ────────────────────────────────────────────────────────────────────


class TestStateSidecar:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        state = lc.LifecycleState(
            slug="demo", subject_type="skill", state=lc.STATE_WATCH,
            state_since=_iso(NOW), consecutive_d_count=1, last_grade="C",
            history=({"at": _iso(NOW), "event": "watch", "note": "test"},),
        )
        lc.save_lifecycle_state(state, sidecar_dir=tmp_path)
        loaded = lc.load_lifecycle_state("demo", sidecar_dir=tmp_path)
        assert loaded is not None
        assert loaded.state == lc.STATE_WATCH
        assert loaded.consecutive_d_count == 1
        assert len(loaded.history) == 1
        assert loaded.history[0]["event"] == "watch"

    def test_missing_sidecar_returns_none(self, tmp_path: Path) -> None:
        assert lc.load_lifecycle_state("nope", sidecar_dir=tmp_path) is None

    def test_corrupt_sidecar_returns_none(self, tmp_path: Path) -> None:
        p = lc.lifecycle_sidecar_path("broken", sidecar_dir=tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not valid", encoding="utf-8")
        assert lc.load_lifecycle_state("broken", sidecar_dir=tmp_path) is None

    def test_invalid_slug_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            lc.lifecycle_sidecar_path("../escape", sidecar_dir=tmp_path)

    def test_history_truncated_to_max(self) -> None:
        cfg = lc.LifecycleConfig(history_max=3)
        state = lc.LifecycleState(slug="demo", subject_type="skill")
        for i in range(5):
            state = replace(
                state,
                history=lc._append_history(
                    state, event=f"e{i}", note=f"n{i}", cfg=cfg, at=_iso(NOW),
                ),
            )
        assert len(state.history) == 3
        assert state.history[0]["event"] == "e2"
        assert state.history[-1]["event"] == "e4"


# ────────────────────────────────────────────────────────────────────
# apply_proposal: filesystem side-effects
# ────────────────────────────────────────────────────────────────────


def _make_fake_skill(root: Path, slug: str) -> Path:
    d = root / slug
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {slug}\n---\n\n# {slug}\n",
        encoding="utf-8",
    )
    return d


class TestApplyProposal:
    def test_demote_moves_skill_dir(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        sidecar_dir = tmp_path / "quality"
        _make_fake_skill(skills, "demo")
        sources = lc.LifecycleSources(
            skills_dir=skills, agents_dir=tmp_path / "agents",
            sidecar_dir=sidecar_dir,
        )
        cfg = lc.LifecycleConfig()
        state = lc.LifecycleState(slug="demo", subject_type="skill")
        proposal = lc.Proposal(
            slug="demo", subject_type="skill",
            current_state=lc.STATE_ACTIVE, target_state=lc.STATE_DEMOTE,
            reason="test",
        )
        new_state = lc.apply_proposal(
            proposal, state, sources=sources, cfg=cfg, now=NOW,
        )
        assert new_state.state == lc.STATE_DEMOTE
        assert not (skills / "demo").exists()
        assert (skills / cfg.demoted_subdir / "demo" / "SKILL.md").is_file()

    def test_archive_moves_from_demoted_to_archive(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        sidecar_dir = tmp_path / "quality"
        cfg = lc.LifecycleConfig()
        demoted = skills / cfg.demoted_subdir
        _make_fake_skill(demoted, "demo")
        sources = lc.LifecycleSources(
            skills_dir=skills, agents_dir=tmp_path / "agents",
            sidecar_dir=sidecar_dir,
        )
        state = lc.LifecycleState(
            slug="demo", subject_type="skill", state=lc.STATE_DEMOTE,
            state_since=_iso(NOW - timedelta(days=20)),
        )
        proposal = lc.Proposal(
            slug="demo", subject_type="skill",
            current_state=lc.STATE_DEMOTE, target_state=lc.STATE_ARCHIVE,
            reason="test",
        )
        new_state = lc.apply_proposal(
            proposal, state, sources=sources, cfg=cfg, now=NOW,
        )
        assert new_state.state == lc.STATE_ARCHIVE
        assert not (demoted / "demo").exists()
        assert (skills / cfg.archive_subdir / "demo" / "SKILL.md").is_file()

    def test_delete_removes_archive_and_sidecars(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        sidecar_dir = tmp_path / "quality"
        cfg = lc.LifecycleConfig()
        archive = skills / cfg.archive_subdir
        _make_fake_skill(archive, "demo")
        # Seed both sidecars so we can verify they get removed.
        sidecar_dir.mkdir()
        (sidecar_dir / "demo.json").write_text(
            json.dumps({"slug": "demo"}), encoding="utf-8"
        )
        (sidecar_dir / "demo.lifecycle.json").write_text(
            json.dumps({"slug": "demo"}), encoding="utf-8"
        )
        sources = lc.LifecycleSources(
            skills_dir=skills, agents_dir=tmp_path / "agents",
            sidecar_dir=sidecar_dir,
        )
        state = lc.LifecycleState(
            slug="demo", subject_type="skill", state=lc.STATE_ARCHIVE,
            state_since=_iso(NOW - timedelta(days=90)),
        )
        proposal = lc.Proposal(
            slug="demo", subject_type="skill",
            current_state=lc.STATE_ARCHIVE, target_state="deleted",
            reason="past delete threshold",
            requires_typed_confirmation=True, auto_safe=False,
        )
        new_state = lc.apply_proposal(
            proposal, state, sources=sources, cfg=cfg, now=NOW,
        )
        assert new_state.state == "deleted"
        assert not (archive / "demo").exists()
        assert not (sidecar_dir / "demo.json").exists()
        assert not (sidecar_dir / "demo.lifecycle.json").exists()

    def test_demote_missing_source_still_advances_state(
        self, tmp_path: Path
    ) -> None:
        skills = tmp_path / "skills"
        skills.mkdir()
        sources = lc.LifecycleSources(
            skills_dir=skills, agents_dir=tmp_path / "agents",
            sidecar_dir=tmp_path / "quality",
        )
        cfg = lc.LifecycleConfig()
        state = lc.LifecycleState(slug="ghost", subject_type="skill")
        proposal = lc.Proposal(
            slug="ghost", subject_type="skill",
            current_state=lc.STATE_ACTIVE, target_state=lc.STATE_DEMOTE,
            reason="test",
        )
        # Should not raise even though there is nothing on disk to move.
        new_state = lc.apply_proposal(
            proposal, state, sources=sources, cfg=cfg, now=NOW,
        )
        assert new_state.state == lc.STATE_DEMOTE

    def test_archive_target_exists_raises(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        sidecar_dir = tmp_path / "quality"
        cfg = lc.LifecycleConfig()
        demoted = skills / cfg.demoted_subdir
        archive = skills / cfg.archive_subdir
        _make_fake_skill(demoted, "demo")
        # Pre-create a conflicting archive dir.
        _make_fake_skill(archive, "demo")
        sources = lc.LifecycleSources(
            skills_dir=skills, agents_dir=tmp_path / "agents",
            sidecar_dir=sidecar_dir,
        )
        state = lc.LifecycleState(
            slug="demo", subject_type="skill", state=lc.STATE_DEMOTE,
            state_since=_iso(NOW - timedelta(days=20)),
        )
        proposal = lc.Proposal(
            slug="demo", subject_type="skill",
            current_state=lc.STATE_DEMOTE, target_state=lc.STATE_ARCHIVE,
            reason="test",
        )
        with pytest.raises(FileExistsError):
            lc.apply_proposal(
                proposal, state, sources=sources, cfg=cfg, now=NOW,
            )


# ────────────────────────────────────────────────────────────────────
# promote_archived
# ────────────────────────────────────────────────────────────────────


class TestPromoteArchived:
    def test_restore_from_archive(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        sidecar_dir = tmp_path / "quality"
        cfg = lc.LifecycleConfig()
        archive = skills / cfg.archive_subdir
        _make_fake_skill(archive, "demo")
        sources = lc.LifecycleSources(
            skills_dir=skills, agents_dir=tmp_path / "agents",
            sidecar_dir=sidecar_dir,
        )
        # Seed an archive-state sidecar so promote can read subject_type.
        state = lc.LifecycleState(
            slug="demo", subject_type="skill", state=lc.STATE_ARCHIVE,
            state_since=_iso(NOW - timedelta(days=30)),
            consecutive_d_count=5,
        )
        lc.save_lifecycle_state(state, sidecar_dir=sidecar_dir)

        new_state = lc.promote_archived(
            "demo", sources=sources, cfg=cfg, now=NOW,
        )
        assert new_state.state == lc.STATE_ACTIVE
        assert new_state.consecutive_d_count == 0
        assert (skills / "demo" / "SKILL.md").is_file()
        assert not (archive / "demo").exists()

    def test_restore_missing_raises(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        skills.mkdir()
        sources = lc.LifecycleSources(
            skills_dir=skills, agents_dir=tmp_path / "agents",
            sidecar_dir=tmp_path / "quality",
        )
        with pytest.raises(FileNotFoundError):
            lc.promote_archived("missing", sources=sources)


# ────────────────────────────────────────────────────────────────────
# plan_review end-to-end
# ────────────────────────────────────────────────────────────────────


def _write_quality_sidecar(
    sidecar_dir: Path, slug: str, *, grade: str,
    subject_type: str = "skill", computed_at: datetime | None = None,
) -> None:
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    ts = computed_at or NOW
    payload = {
        "slug": slug,
        "subject_type": subject_type,
        "raw_score": 0.5,
        "score": 0.5,
        "grade": grade,
        "hard_floor": None,
        "signals": {
            name: {"score": 0.5, "evidence": {}}
            for name in ("telemetry", "intake", "graph", "routing")
        },
        "weights": {"telemetry": 0.4, "intake": 0.2, "graph": 0.25, "routing": 0.15},
        "computed_at": _iso(ts),
    }
    (sidecar_dir / f"{slug}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


class TestPlanReview:
    def test_empty_corpus(self, tmp_path: Path) -> None:
        sources = lc.LifecycleSources(
            skills_dir=tmp_path / "skills", agents_dir=tmp_path / "agents",
            sidecar_dir=tmp_path / "quality",
        )
        proposals, observed = lc.plan_review(sources=sources, now=NOW)
        assert proposals == []
        assert observed == {}

    def test_mixed_grades_classified(self, tmp_path: Path) -> None:
        sidecar = tmp_path / "quality"
        _write_quality_sidecar(sidecar, "healthy", grade="A")
        _write_quality_sidecar(sidecar, "watching", grade="C")
        _write_quality_sidecar(sidecar, "struggling", grade="D")
        # Pre-seed "struggling" with an existing D-streak so this run pushes
        # it over the threshold.
        lc.save_lifecycle_state(
            lc.LifecycleState(
                slug="struggling", subject_type="skill",
                consecutive_d_count=1,
                last_seen_computed_at=_iso(NOW - timedelta(days=1)),
            ),
            sidecar_dir=sidecar,
        )
        sources = lc.LifecycleSources(
            skills_dir=tmp_path / "skills", agents_dir=tmp_path / "agents",
            sidecar_dir=sidecar,
        )
        proposals, observed = lc.plan_review(sources=sources, now=NOW)
        targets = {p.slug: p.target_state for p in proposals}
        assert "healthy" not in targets  # A → nothing
        assert targets.get("watching") == lc.STATE_WATCH
        assert targets.get("struggling") == lc.STATE_DEMOTE
        assert observed["struggling"].consecutive_d_count == 2

    def test_archive_candidate_surfaced_without_quality_sidecar(
        self, tmp_path: Path
    ) -> None:
        sidecar = tmp_path / "quality"
        # Demoted entry with no remaining quality sidecar — should still
        # be classified for archive based on age alone.
        lc.save_lifecycle_state(
            lc.LifecycleState(
                slug="oldie", subject_type="skill", state=lc.STATE_DEMOTE,
                state_since=_iso(NOW - timedelta(days=30)),
            ),
            sidecar_dir=sidecar,
        )
        sources = lc.LifecycleSources(
            skills_dir=tmp_path / "skills", agents_dir=tmp_path / "agents",
            sidecar_dir=sidecar,
        )
        proposals, _ = lc.plan_review(sources=sources, now=NOW)
        assert len(proposals) == 1
        assert proposals[0].target_state == lc.STATE_ARCHIVE


# ────────────────────────────────────────────────────────────────────
# CLI smoke: review + purge flows
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect default_sidecar_dir + ctx_config.cfg to tmp."""
    skills = tmp_path / "skills"
    agents = tmp_path / "agents"
    sidecar = tmp_path / "quality"
    skills.mkdir()
    agents.mkdir()
    sidecar.mkdir()

    class _FakeCfg:
        skills_dir = skills
        agents_dir = agents

        def get(self, key: str, default=None):
            if key == "quality":
                return {"lifecycle": {}}
            return default

    import ctx_config
    monkeypatch.setattr(ctx_config, "cfg", _FakeCfg(), raising=True)

    import skill_quality as sq
    monkeypatch.setattr(sq, "default_sidecar_dir", lambda: sidecar, raising=True)
    monkeypatch.setattr(lc, "default_sidecar_dir", lambda: sidecar, raising=True)
    return tmp_path


class TestCLIReview:
    def test_review_dry_run_no_changes(
        self, cli_env: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        sidecar = cli_env / "quality"
        _write_quality_sidecar(sidecar, "watchme", grade="C")
        rc = lc.main(["review", "--dry-run"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "WATCH" in out
        assert "dry-run" in out
        # Lifecycle state gets folded (counter maintenance) but no move.
        state = lc.load_lifecycle_state("watchme", sidecar_dir=sidecar)
        assert state is not None
        # Dry-run still persists the observed score — state stays active,
        # only the streak/last-grade fields advance.
        assert state.state == lc.STATE_ACTIVE
        assert state.last_grade == "C"

    def test_review_json_emits_proposals(
        self, cli_env: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        sidecar = cli_env / "quality"
        _write_quality_sidecar(sidecar, "watchme", grade="C")
        rc = lc.main(["review", "--dry-run", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state_count"] == 1
        assert payload["proposals"][0]["target_state"] == lc.STATE_WATCH

    def test_review_auto_applies_watch(
        self, cli_env: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        sidecar = cli_env / "quality"
        _write_quality_sidecar(sidecar, "watchme", grade="C")
        rc = lc.main(["review", "--auto"])
        assert rc == 0
        state = lc.load_lifecycle_state("watchme", sidecar_dir=sidecar)
        assert state is not None and state.state == lc.STATE_WATCH


class TestCLIPurge:
    def test_purge_empty_noop(
        self, cli_env: Path, capsys: pytest.CaptureFixture,
    ) -> None:
        rc = lc.main(["purge"])
        assert rc == 0
        assert "Nothing to purge" in capsys.readouterr().out


# ─────────────────────────────────────────────────────────────────────
# _build_config: config.json overrides actually propagate
# ─────────────────────────────────────────────────────────────────────
#
# P2.3 pinned regression. A code-reviewer finding claimed
# ``app_cfg.get("quality", {})`` returned empty because config.json
# had no "quality" section, silently ignoring all user overrides. In
# fact config.json DOES have "quality.lifecycle" and _build_config
# does read it — but the defaults happen to match the configured
# values, so the reviewer couldn't tell by inspection. This test
# pins the propagation so a future refactor that accidentally breaks
# get() traversal fails loudly.

class TestBuildConfigPropagates:

    def _rebuild_with_override(self, overrides: dict):
        """Rebuild ctx_config.cfg with a lifecycle override and reload
        ctx_lifecycle so its late-bound import picks up the new cfg."""
        import importlib
        import ctx_config as _cc

        raw = _cc._load_raw()
        raw.setdefault("quality", {}).setdefault("lifecycle", {}).update(overrides)
        _cc.cfg = _cc.Config(raw)

        import ctx_lifecycle as _cl
        importlib.reload(_cl)
        return _cl._build_config()

    def test_archive_threshold_override_propagates(self, monkeypatch):
        cfg = self._rebuild_with_override({"archive_threshold_days": 999.0})
        assert cfg.archive_threshold_days == 999.0

    def test_delete_threshold_override_propagates(self, monkeypatch):
        cfg = self._rebuild_with_override({"delete_threshold_days": 777.0})
        assert cfg.delete_threshold_days == 777.0

    def test_history_max_override_propagates(self, monkeypatch):
        cfg = self._rebuild_with_override({"history_max": 42})
        assert cfg.history_max == 42

    def test_demoted_subdir_override_propagates(self, monkeypatch):
        cfg = self._rebuild_with_override({"demoted_subdir": "_my_demoted"})
        assert cfg.demoted_subdir == "_my_demoted"

    def test_missing_quality_section_gracefully_defaults(self, monkeypatch):
        """If a user's config.json has no quality.lifecycle section
        (older config or deliberate stripped-down config),
        _build_config must return defaults without crashing."""
        import importlib
        import ctx_config as _cc

        raw = _cc._load_raw()
        raw.pop("quality", None)
        _cc.cfg = _cc.Config(raw)
        import ctx_lifecycle as _cl
        importlib.reload(_cl)

        cfg = _cl._build_config()
        # Matches the LifecycleConfig dataclass defaults.
        default = _cl.LifecycleConfig()
        assert cfg.archive_threshold_days == default.archive_threshold_days
        assert cfg.history_max == default.history_max
