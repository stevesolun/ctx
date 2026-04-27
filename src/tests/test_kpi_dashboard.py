"""
test_kpi_dashboard.py -- Tests for the KPI dashboard aggregator + renderer.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import kpi_dashboard as kd  # noqa: E402
import ctx_lifecycle as cl  # noqa: E402
from ctx.core.quality.quality_signals import SignalResult  # noqa: E402
from skill_quality import QualityScore, sidecar_path  # noqa: E402


NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)


# ────────────────────────────────────────────────────────────────────
# Fixture helpers
# ────────────────────────────────────────────────────────────────────


def _write_skill(
    skills_dir: Path, slug: str, *, tags: list[str] | None = None,
    category: str | None = None,
) -> Path:
    d = skills_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {slug}"]
    if tags:
        fm.append(f"tags: [{', '.join(tags)}]")
    if category is not None:
        fm.append(f"category: {category}")
    body = "---\n" + "\n".join(fm) + "\n---\n\n# " + slug + "\n"
    path = d / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def _write_agent(agents_dir: Path, slug: str, *, tags: list[str] | None = None) -> Path:
    agents_dir.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {slug}"]
    if tags:
        fm.append(f"tags: [{', '.join(tags)}]")
    body = "---\n" + "\n".join(fm) + "\n---\n\n# " + slug + "\n"
    path = agents_dir / f"{slug}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _write_quality(
    sidecar_dir: Path,
    slug: str,
    *,
    subject_type: str = "skill",
    grade: str = "B",
    score: float = 0.65,
    hard_floor: str | None = None,
    computed_at: str = "2026-04-19T12:00:00+00:00",
) -> Path:
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "slug": slug,
        "subject_type": subject_type,
        "raw_score": score,
        "score": score,
        "grade": grade,
        "hard_floor": hard_floor,
        "signals": {
            "telemetry": {"score": score, "evidence": {}},
            "intake": {"score": score, "evidence": {}},
            "graph": {"score": score, "evidence": {}},
            "routing": {"score": score, "evidence": {}},
        },
        "weights": {"telemetry": 0.4, "intake": 0.2, "graph": 0.25, "routing": 0.15},
        "computed_at": computed_at,
    }
    path = sidecar_path(slug, sidecar_dir=sidecar_dir)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_lifecycle(
    sidecar_dir: Path,
    slug: str,
    *,
    state: str = cl.STATE_ACTIVE,
    subject_type: str = "skill",
    streak: int = 0,
) -> Path:
    ls = cl.LifecycleState(
        slug=slug,
        subject_type=subject_type,
        state=state,
        consecutive_d_count=streak,
        state_since="2026-04-19T00:00:00+00:00",
        last_grade="D" if streak else "",
        last_seen_computed_at="2026-04-19T00:00:00+00:00",
    )
    return cl.save_lifecycle_state(ls, sidecar_dir=sidecar_dir)


@pytest.fixture()
def sources(tmp_path: Path) -> cl.LifecycleSources:
    return cl.LifecycleSources(
        skills_dir=tmp_path / "skills",
        agents_dir=tmp_path / "agents",
        sidecar_dir=tmp_path / "quality",
    )


# ────────────────────────────────────────────────────────────────────
# Category resolution
# ────────────────────────────────────────────────────────────────────


class TestResolveCategory:
    def test_reads_explicit_category(self, sources: cl.LifecycleSources) -> None:
        _write_skill(sources.skills_dir, "foo", tags=["python"], category="meta")
        assert kd._resolve_category("foo", sources) == "meta"

    def test_falls_back_to_inference(self, sources: cl.LifecycleSources) -> None:
        _write_skill(sources.skills_dir, "foo", tags=["python"])
        assert kd._resolve_category("foo", sources) == "language"

    def test_uncategorized_when_no_match(self, sources: cl.LifecycleSources) -> None:
        _write_skill(sources.skills_dir, "foo", tags=["zzz-unknown"])
        assert kd._resolve_category("foo", sources) == "uncategorized"

    def test_uncategorized_when_no_file(self, sources: cl.LifecycleSources) -> None:
        assert kd._resolve_category("missing", sources) == "uncategorized"

    def test_reads_agent_category(self, sources: cl.LifecycleSources) -> None:
        _write_agent(sources.agents_dir, "ag", tags=["docker"])
        assert kd._resolve_category("ag", sources) == "tool"


# ────────────────────────────────────────────────────────────────────
# collect_rows
# ────────────────────────────────────────────────────────────────────


class TestCollectRows:
    def test_union_of_quality_and_lifecycle(
        self, sources: cl.LifecycleSources,
    ) -> None:
        _write_skill(sources.skills_dir, "a", tags=["python"])
        _write_skill(sources.skills_dir, "b", tags=["react"])
        _write_skill(sources.skills_dir, "c", tags=["docker"])
        _write_quality(sources.sidecar_dir, "a", grade="A", score=0.9)
        _write_quality(sources.sidecar_dir, "b", grade="D", score=0.3)
        _write_lifecycle(sources.sidecar_dir, "c", state=cl.STATE_ARCHIVE)

        rows = kd.collect_rows(sources=sources)
        slugs = [r.slug for r in rows]
        assert slugs == ["a", "b", "c"]
        row_c = next(r for r in rows if r.slug == "c")
        # Archive-only: no score, lifecycle wins.
        assert row_c.lifecycle_state == cl.STATE_ARCHIVE
        assert row_c.grade == ""

    def test_lifecycle_defaults_to_active(
        self, sources: cl.LifecycleSources,
    ) -> None:
        _write_skill(sources.skills_dir, "a", tags=["python"])
        _write_quality(sources.sidecar_dir, "a", grade="A", score=0.9)
        rows = kd.collect_rows(sources=sources)
        assert rows[0].lifecycle_state == cl.STATE_ACTIVE

    def test_corrupt_quality_sidecar_is_skipped(
        self, sources: cl.LifecycleSources,
    ) -> None:
        _write_skill(sources.skills_dir, "a", tags=["python"])
        sources.sidecar_dir.mkdir(parents=True)
        (sources.sidecar_dir / "a.json").write_text("{ not json", encoding="utf-8")
        # Still returns a row — with no score.
        rows = kd.collect_rows(sources=sources)
        assert len(rows) == 1
        assert rows[0].grade == ""

    def test_ignores_lifecycle_sidecars_as_quality(
        self, sources: cl.LifecycleSources,
    ) -> None:
        # A stray lifecycle file with no matching quality file must not be
        # picked up by the quality walker.
        _write_skill(sources.skills_dir, "a", tags=["python"])
        _write_lifecycle(sources.sidecar_dir, "a", state=cl.STATE_WATCH)
        rows = kd.collect_rows(sources=sources)
        assert len(rows) == 1
        assert rows[0].lifecycle_state == cl.STATE_WATCH


# ────────────────────────────────────────────────────────────────────
# aggregate
# ────────────────────────────────────────────────────────────────────


def _row(
    slug: str, *, grade: str = "B", score: float = 0.65, category: str = "language",
    subject: str = "skill", state: str = cl.STATE_ACTIVE, streak: int = 0,
    hard_floor: str | None = None,
) -> kd.EntityRow:
    return kd.EntityRow(
        slug=slug, subject_type=subject, category=category, grade=grade,
        score=score, hard_floor=hard_floor, lifecycle_state=state,
        consecutive_d_count=streak, computed_at="2026-04-19T12:00:00+00:00",
    )


class TestAggregate:
    def test_grade_counts_normalize_blank_to_f(self) -> None:
        rows = [
            _row("a", grade="A", score=0.9),
            _row("b", grade=""),   # archived-only, no score
            _row("c", grade="D", score=0.3),
        ]
        summary = kd.aggregate(rows, now=NOW)
        assert summary.grade_counts["A"] == 1
        assert summary.grade_counts["D"] == 1
        assert summary.grade_counts["F"] == 1  # blank grade rolls up into F

    def test_category_breakdown_orders_taxonomy_first(self) -> None:
        rows = [
            _row("a", category="language"),
            _row("b", category="uncategorized"),
            _row("c", category="framework"),
        ]
        summary = kd.aggregate(rows, now=NOW)
        order = [c["category"] for c in summary.category_breakdown]
        # language comes before framework in CATEGORIES? No — framework first.
        assert order.index("framework") < order.index("language")
        assert order.index("language") < order.index("uncategorized")

    def test_category_avg_score_excludes_blank_grade(self) -> None:
        rows = [
            _row("a", category="language", grade="A", score=1.0),
            _row("b", category="language", grade="", score=0.0),  # no score
        ]
        summary = kd.aggregate(rows, now=NOW)
        lang = next(c for c in summary.category_breakdown if c["category"] == "language")
        assert lang["count"] == 2
        assert lang["avg_score"] == pytest.approx(1.0)  # blank excluded

    def test_low_quality_candidates_sort_by_streak_then_score(self) -> None:
        rows = [
            _row("high-streak", grade="D", score=0.5, streak=3),
            _row("low-score", grade="D", score=0.1, streak=1),
            _row("healthy", grade="A", score=0.9),
        ]
        summary = kd.aggregate(rows, now=NOW, top_n=10)
        slugs = [c["slug"] for c in summary.low_quality_candidates]
        assert slugs == ["high-streak", "low-score"]

    def test_low_quality_respects_top_n(self) -> None:
        rows = [_row(f"s{i}", grade="D", score=0.3) for i in range(5)]
        summary = kd.aggregate(rows, now=NOW, top_n=2)
        assert len(summary.low_quality_candidates) == 2

    def test_low_quality_excludes_demoted_and_archived(self) -> None:
        rows = [
            _row("active-d", grade="D", score=0.3),
            _row("demoted", grade="D", score=0.3, state=cl.STATE_DEMOTE),
            _row("archived", grade="D", score=0.3, state=cl.STATE_ARCHIVE),
        ]
        summary = kd.aggregate(rows, now=NOW, top_n=10)
        slugs = [c["slug"] for c in summary.low_quality_candidates]
        assert slugs == ["active-d"]

    def test_archived_section_lists_only_archived(self) -> None:
        rows = [
            _row("a", grade="A", score=0.9),
            _row("b", grade="D", score=0.3, state=cl.STATE_ARCHIVE),
        ]
        summary = kd.aggregate(rows, now=NOW)
        assert [a["slug"] for a in summary.archived] == ["b"]

    def test_hard_floor_counts(self) -> None:
        rows = [
            _row("a", hard_floor="intake_fail", grade="F", score=0.0),
            _row("b", hard_floor="intake_fail", grade="F", score=0.0),
            _row("c", hard_floor="never_loaded_stale", grade="D", score=0.3),
            _row("d", grade="A", score=0.9),
        ]
        summary = kd.aggregate(rows, now=NOW)
        assert summary.hard_floor_counts["intake_fail"] == 2
        assert summary.hard_floor_counts["never_loaded_stale"] == 1

    def test_empty_corpus(self) -> None:
        summary = kd.aggregate([], now=NOW)
        assert summary.total == 0
        assert summary.grade_counts["A"] == 0
        assert summary.category_breakdown == []
        assert summary.low_quality_candidates == []


# ────────────────────────────────────────────────────────────────────
# render_markdown
# ────────────────────────────────────────────────────────────────────


class TestRenderMarkdown:
    def test_header_and_totals_present(self) -> None:
        summary = kd.DashboardSummary(
            generated_at="2026-04-19T00:00:00+00:00",
            total=3,
            by_subject={"skill": 2, "agent": 1},
            grade_counts={"A": 1, "B": 1, "C": 0, "D": 1, "F": 0},
            lifecycle_counts={
                cl.STATE_ACTIVE: 3, cl.STATE_WATCH: 0,
                cl.STATE_DEMOTE: 0, cl.STATE_ARCHIVE: 0,
            },
        )
        md = kd.render_markdown(summary)
        assert "# Skill Quality KPI Dashboard" in md
        assert "**Total entities:** 3" in md
        assert "skill: 2" in md
        assert "agent: 1" in md
        # Grade distribution table header.
        assert "| Grade | Count | Share |" in md

    def test_empty_candidates_produces_friendly_message(self) -> None:
        summary = kd.DashboardSummary(
            generated_at="2026-04-19T00:00:00+00:00",
            total=0,
            grade_counts={g: 0 for g in ("A", "B", "C", "D", "F")},
            lifecycle_counts={s: 0 for s in (
                cl.STATE_ACTIVE, cl.STATE_WATCH, cl.STATE_DEMOTE, cl.STATE_ARCHIVE,
            )},
        )
        md = kd.render_markdown(summary)
        assert "_No active D/F-grade entries — corpus is healthy._" in md
        assert "_None._" in md  # archived section

    def test_candidates_table_includes_slug_and_score(self) -> None:
        summary = kd.DashboardSummary(
            generated_at="2026-04-19T00:00:00+00:00",
            total=1,
            grade_counts={"A": 0, "B": 0, "C": 0, "D": 1, "F": 0},
            lifecycle_counts={
                cl.STATE_ACTIVE: 1, cl.STATE_WATCH: 0,
                cl.STATE_DEMOTE: 0, cl.STATE_ARCHIVE: 0,
            },
            low_quality_candidates=[{
                "slug": "stale-skill",
                "subject_type": "skill",
                "category": "language",
                "grade": "D",
                "score": 0.25,
                "lifecycle_state": cl.STATE_ACTIVE,
                "consecutive_d_count": 2,
                "hard_floor": "never_loaded_stale",
            }],
        )
        md = kd.render_markdown(summary)
        assert "stale-skill" in md
        assert "0.250" in md
        assert "never_loaded_stale" in md


# ────────────────────────────────────────────────────────────────────
# Integration: generate from fixture corpus
# ────────────────────────────────────────────────────────────────────


class TestGenerateIntegration:
    def test_full_corpus(self, sources: cl.LifecycleSources) -> None:
        _write_skill(sources.skills_dir, "py-util", tags=["python"])
        _write_skill(sources.skills_dir, "react-state", tags=["react"])
        _write_skill(sources.skills_dir, "docker-ops", tags=["docker"])
        _write_skill(sources.skills_dir, "old", tags=["python"])
        _write_agent(sources.agents_dir, "helper", tags=["refactoring"])

        _write_quality(sources.sidecar_dir, "py-util", grade="A", score=0.9)
        _write_quality(sources.sidecar_dir, "react-state", grade="C", score=0.45)
        _write_quality(
            sources.sidecar_dir, "docker-ops", grade="D", score=0.25,
            hard_floor="never_loaded_stale",
        )
        _write_quality(
            sources.sidecar_dir, "helper", subject_type="agent",
            grade="B", score=0.7,
        )
        _write_lifecycle(sources.sidecar_dir, "old", state=cl.STATE_ARCHIVE)
        _write_lifecycle(
            sources.sidecar_dir, "docker-ops", state=cl.STATE_WATCH, streak=1,
        )

        summary = kd.generate(sources=sources, top_n=5, now=NOW)

        assert summary.total == 5
        assert summary.by_subject["skill"] == 4
        assert summary.by_subject["agent"] == 1
        assert summary.grade_counts["A"] == 1
        assert summary.grade_counts["B"] == 1
        assert summary.grade_counts["C"] == 1
        assert summary.grade_counts["D"] == 1
        assert summary.grade_counts["F"] == 1  # archived 'old' has no score → F bucket
        assert summary.lifecycle_counts[cl.STATE_ACTIVE] == 3
        assert summary.lifecycle_counts[cl.STATE_WATCH] == 1
        assert summary.lifecycle_counts[cl.STATE_ARCHIVE] == 1

        # docker-ops is the only D + ACTIVE/WATCH candidate.
        candidate_slugs = [c["slug"] for c in summary.low_quality_candidates]
        assert "docker-ops" in candidate_slugs

        # 'old' appears in archived list.
        archived_slugs = [a["slug"] for a in summary.archived]
        assert archived_slugs == ["old"]

        # Hard-floor tally.
        assert summary.hard_floor_counts.get("never_loaded_stale") == 1

        # Category breakdown includes language, framework, tool, pattern.
        cats = {c["category"] for c in summary.category_breakdown}
        assert {"language", "framework", "tool", "pattern"}.issubset(cats)


# ────────────────────────────────────────────────────────────────────
# CLI smoke
# ────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_render_markdown_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        skills = tmp_path / "skills"
        agents = tmp_path / "agents"
        sidecar = tmp_path / "quality"
        _write_skill(skills, "py-util", tags=["python"])
        _write_quality(sidecar, "py-util", grade="A", score=0.9)

        class _FakeCfg:
            skills_dir = skills
            agents_dir = agents

        import ctx_config
        import skill_quality
        monkeypatch.setattr(ctx_config, "cfg", _FakeCfg(), raising=True)
        monkeypatch.setattr(
            skill_quality, "default_sidecar_dir", lambda: sidecar, raising=True,
        )

        rc = kd.main(["render"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "# Skill Quality KPI Dashboard" in out
        assert "py-util" not in out  # healthy skills do not appear in candidates

    def test_render_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        skills = tmp_path / "skills"
        agents = tmp_path / "agents"
        sidecar = tmp_path / "quality"
        _write_skill(skills, "py", tags=["python"])
        _write_quality(sidecar, "py", grade="A", score=0.9)

        class _FakeCfg:
            skills_dir = skills
            agents_dir = agents

        import ctx_config
        import skill_quality
        monkeypatch.setattr(ctx_config, "cfg", _FakeCfg(), raising=True)
        monkeypatch.setattr(
            skill_quality, "default_sidecar_dir", lambda: sidecar, raising=True,
        )

        rc = kd.main(["render", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["total"] == 1
        assert payload["grade_counts"]["A"] == 1

    def test_render_writes_to_out(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        skills = tmp_path / "skills"
        agents = tmp_path / "agents"
        sidecar = tmp_path / "quality"
        _write_skill(skills, "py", tags=["python"])
        _write_quality(sidecar, "py", grade="A", score=0.9)

        class _FakeCfg:
            skills_dir = skills
            agents_dir = agents

        import ctx_config
        import skill_quality
        monkeypatch.setattr(ctx_config, "cfg", _FakeCfg(), raising=True)
        monkeypatch.setattr(
            skill_quality, "default_sidecar_dir", lambda: sidecar, raising=True,
        )

        out_path = tmp_path / "kpi.md"
        rc = kd.main(["render", "--out", str(out_path)])
        assert rc == 0
        assert "# Skill Quality KPI Dashboard" in out_path.read_text(encoding="utf-8")

    def test_summary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        skills = tmp_path / "skills"
        agents = tmp_path / "agents"
        sidecar = tmp_path / "quality"
        _write_skill(skills, "py", tags=["python"])
        _write_quality(sidecar, "py", grade="A", score=0.9)

        class _FakeCfg:
            skills_dir = skills
            agents_dir = agents

        import ctx_config
        import skill_quality
        monkeypatch.setattr(ctx_config, "cfg", _FakeCfg(), raising=True)
        monkeypatch.setattr(
            skill_quality, "default_sidecar_dir", lambda: sidecar, raising=True,
        )

        rc = kd.main(["summary"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Total: 1" in out
        assert "A: 1" in out


# Keep references so these imports aren't pruned by linters.
_SIGNAL_RESULT_TYPE = SignalResult
_QUALITY_SCORE_TYPE = QualityScore
