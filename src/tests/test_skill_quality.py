"""
test_skill_quality.py -- Regression tests for the Phase 3 scoring module.

Covers:

  - Pure signal extractors (telemetry, intake, graph, routing) — deterministic
    given inputs, clamp to [0, 1], reject invalid inputs.
  - ``compute_quality`` aggregation — weighted sum, hard-floor overrides
    (intake_fail -> F; never_loaded_stale -> D cap), grade thresholds,
    required-signal validation.
  - ``extract_signals_for_slug`` — telemetry event walking, routing trace
    absence, graph index lookup, subject_type dispatch.
  - Persistence round-trip — sidecar JSON always; frontmatter + wiki body
    only when an entity page exists; idempotent ``## Quality`` block.
  - Slug safety — path-traversal rejection in ``sidecar_path``.
  - CLI verbs — recompute / show / explain / list, --json toggles.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import quality_signals as qs  # noqa: E402
import skill_quality as sq  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _good_skill_body() -> str:
    return (
        "---\n"
        "name: demo\n"
        "description: A demo skill used in tests.\n"
        "---\n"
        "# Demo\n\n"
        "## Overview\n\n"
        + ("Body content. " * 20)
        + "\n"
    )


@pytest.fixture()
def live_layout(tmp_path: Path) -> dict[str, Path]:
    """Minimal skills/agents/wiki/events layout for integration tests."""
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    wiki_dir = tmp_path / "wiki"
    sidecar_dir = tmp_path / "quality"

    (skills_dir / "demo").mkdir(parents=True)
    (skills_dir / "demo" / "SKILL.md").write_text(
        _good_skill_body(), encoding="utf-8"
    )

    (agents_dir).mkdir(parents=True)
    (agents_dir / "agent-one.md").write_text(
        _good_skill_body().replace("name: demo", "name: agent-one"),
        encoding="utf-8",
    )

    (wiki_dir / "entities" / "skills").mkdir(parents=True)
    (wiki_dir / "entities" / "skills" / "demo.md").write_text(
        _good_skill_body(), encoding="utf-8"
    )
    (wiki_dir / "entities" / "agents").mkdir(parents=True)

    events = tmp_path / "skill-events.jsonl"
    events.write_text("", encoding="utf-8")

    return {
        "skills": skills_dir,
        "agents": agents_dir,
        "wiki": wiki_dir,
        "events": events,
        "sidecar": sidecar_dir,
        "root": tmp_path,
    }


def _sources(paths: dict[str, Path], **overrides) -> sq.SignalSources:
    return sq.SignalSources(
        skills_dir=paths["skills"],
        agents_dir=paths["agents"],
        wiki_dir=paths["wiki"],
        events_path=paths["events"],
        router_trace_path=overrides.get("router_trace_path"),
        graph_index=overrides.get("graph_index"),
    )


# ────────────────────────────────────────────────────────────────────
# SignalResult
# ────────────────────────────────────────────────────────────────────


def test_signal_result_clamps_above_one() -> None:
    r = qs.SignalResult(score=1.7)
    assert r.score == 1.0


def test_signal_result_clamps_below_zero() -> None:
    r = qs.SignalResult(score=-0.2)
    assert r.score == 0.0


def test_signal_result_rejects_nan() -> None:
    with pytest.raises(ValueError):
        qs.SignalResult(score=float("nan"))


def test_signal_result_rejects_inf() -> None:
    with pytest.raises(ValueError):
        qs.SignalResult(score=float("inf"))


# ────────────────────────────────────────────────────────────────────
# telemetry_signal
# ────────────────────────────────────────────────────────────────────


def test_telemetry_never_loaded_scores_zero() -> None:
    r = qs.telemetry_signal(
        load_count=0,
        recent_load_count=0,
        last_load_age_days=None,
        stale_threshold_days=30.0,
    )
    assert r.score == pytest.approx(0.0)
    assert r.evidence["never_loaded"] is True


def test_telemetry_fresh_heavy_use_scores_one() -> None:
    r = qs.telemetry_signal(
        load_count=10,
        recent_load_count=5,  # past saturation of 3
        last_load_age_days=0.0,
        stale_threshold_days=30.0,
    )
    assert r.score == pytest.approx(1.0)
    assert r.evidence["never_loaded"] is False


def test_telemetry_stale_single_load() -> None:
    """Loaded once, long ago — ever_loaded credit only."""
    r = qs.telemetry_signal(
        load_count=1,
        recent_load_count=0,
        last_load_age_days=60.0,
        stale_threshold_days=30.0,
    )
    # Only ever_loaded_weight contributes; recency decays to 0.
    assert r.score == pytest.approx(0.35)


def test_telemetry_rejects_negative() -> None:
    with pytest.raises(ValueError):
        qs.telemetry_signal(
            load_count=-1,
            recent_load_count=0,
            last_load_age_days=None,
            stale_threshold_days=30.0,
        )


def test_telemetry_rejects_bad_threshold() -> None:
    with pytest.raises(ValueError):
        qs.telemetry_signal(
            load_count=0,
            recent_load_count=0,
            last_load_age_days=None,
            stale_threshold_days=0,
        )


# ────────────────────────────────────────────────────────────────────
# intake_signal
# ────────────────────────────────────────────────────────────────────


def test_intake_full_pass_scores_one() -> None:
    body = "# H1\n\n## H2\n\n" + ("padding " * 30)
    r = qs.intake_signal(
        raw_md="---\nname: x\ndescription: y\n---\n" + body,
        frontmatter={"name": "x", "description": "y"},
        has_frontmatter_block=True,
        body=body,
        min_body_chars=120,
    )
    assert r.score == pytest.approx(1.0)
    assert r.evidence["hard_fail"] is False


def test_intake_missing_h2_fails_hard() -> None:
    body = "# H1\n\n" + ("padding " * 30)  # no ## H2
    r = qs.intake_signal(
        raw_md="",
        frontmatter={"name": "x", "description": "y"},
        has_frontmatter_block=True,
        body=body,
        min_body_chars=120,
    )
    # 5 of 6 checks pass → 5/6
    assert r.score == pytest.approx(5 / 6)
    assert r.evidence["hard_fail"] is True
    assert r.evidence["checks"]["has_h2"] is False


def test_intake_missing_frontmatter_fails_hard() -> None:
    body = "# H1\n\n## H2\n\n" + ("padding " * 30)
    r = qs.intake_signal(
        raw_md=body,
        frontmatter={},
        has_frontmatter_block=False,
        body=body,
        min_body_chars=120,
    )
    assert r.evidence["hard_fail"] is True
    # No frontmatter, no name, no description, but H1/H2/body length pass.
    assert r.evidence["passed"] == 3


def test_intake_rejects_negative_min_body() -> None:
    with pytest.raises(ValueError):
        qs.intake_signal(
            raw_md="",
            frontmatter={},
            has_frontmatter_block=False,
            body="",
            min_body_chars=-1,
        )


# ────────────────────────────────────────────────────────────────────
# graph_signal
# ────────────────────────────────────────────────────────────────────


def test_graph_isolated_scores_zero() -> None:
    r = qs.graph_signal(degree=0)
    assert r.score == 0.0
    assert r.evidence["is_isolated"] is True


def test_graph_single_neighbor_nonzero() -> None:
    r = qs.graph_signal(degree=1)
    assert r.score > 0.0
    assert r.score < 0.5


def test_graph_saturates_near_one() -> None:
    r = qs.graph_signal(degree=20)
    assert r.score == pytest.approx(1.0, abs=0.01)


def test_graph_weight_bonus_caps_at_ten_percent() -> None:
    r_plain = qs.graph_signal(degree=5, avg_edge_weight=1.0)
    r_heavy = qs.graph_signal(degree=5, avg_edge_weight=9.0)
    assert r_heavy.score - r_plain.score == pytest.approx(0.10, abs=0.001)


def test_graph_rejects_negative_degree() -> None:
    with pytest.raises(ValueError):
        qs.graph_signal(degree=-1)


# ────────────────────────────────────────────────────────────────────
# routing_signal
# ────────────────────────────────────────────────────────────────────


def test_routing_no_trace_is_neutral() -> None:
    r = qs.routing_signal(considered=0, picked=0)
    assert r.score == 0.5
    assert r.evidence["no_trace"] is True


def test_routing_below_minimum_is_neutral() -> None:
    r = qs.routing_signal(considered=2, picked=0)
    assert r.score == 0.5
    assert r.evidence["no_trace"] is True


def test_routing_perfect_hits() -> None:
    r = qs.routing_signal(considered=5, picked=5)
    assert r.score == 1.0


def test_routing_half_hits() -> None:
    r = qs.routing_signal(considered=10, picked=5)
    assert r.score == 0.5


def test_routing_rejects_overcount() -> None:
    with pytest.raises(ValueError):
        qs.routing_signal(considered=1, picked=2)


# ────────────────────────────────────────────────────────────────────
# QualityConfig
# ────────────────────────────────────────────────────────────────────


def test_quality_config_defaults_valid() -> None:
    cfg = sq.QualityConfig()
    assert sum(cfg.weights.values()) == pytest.approx(1.0)


def test_quality_config_rejects_weight_sum_mismatch() -> None:
    with pytest.raises(ValueError):
        sq.QualityConfig(
            weights={"telemetry": 0.5, "intake": 0.5, "graph": 0.5, "routing": 0.5}
        )


def test_quality_config_rejects_missing_weight_key() -> None:
    with pytest.raises(ValueError):
        sq.QualityConfig(
            weights={"telemetry": 0.5, "intake": 0.25, "graph": 0.25}
        )


def test_quality_config_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError):
        sq.QualityConfig(grade_thresholds={"A": 0.3, "B": 0.5, "C": 0.7})


def test_quality_config_has_separate_agent_weights() -> None:
    cfg = sq.QualityConfig()
    assert sum(cfg.agent_weights.values()) == pytest.approx(1.0)
    # Agents weight telemetry less than skills do — that's the whole
    # point of the separate vector.
    assert cfg.agent_weights["telemetry"] < cfg.weights["telemetry"]


def test_quality_config_rejects_bad_agent_weights_sum() -> None:
    with pytest.raises(ValueError):
        sq.QualityConfig(
            agent_weights={
                "telemetry": 0.5, "intake": 0.5, "graph": 0.5, "routing": 0.5,
            }
        )


def test_quality_config_rejects_missing_agent_weight_key() -> None:
    with pytest.raises(ValueError):
        sq.QualityConfig(
            agent_weights={"telemetry": 0.3, "intake": 0.3, "graph": 0.4}
        )


def test_quality_config_rejects_negative_agent_weight() -> None:
    with pytest.raises(ValueError):
        sq.QualityConfig(
            agent_weights={
                "telemetry": -0.1, "intake": 0.4, "graph": 0.4, "routing": 0.3,
            }
        )


def test_weights_for_dispatches_by_subject_type() -> None:
    cfg = sq.QualityConfig()
    assert cfg.weights_for("skill") == cfg.weights
    assert cfg.weights_for("agent") == cfg.agent_weights


# ────────────────────────────────────────────────────────────────────
# compute_quality: weighted sum + hard floors + grades
# ────────────────────────────────────────────────────────────────────


def _signals(
    tel: float = 1.0,
    intake: float = 1.0,
    graph: float = 1.0,
    routing: float = 1.0,
    intake_hard_fail: bool = False,
    never_loaded: bool = False,
) -> dict[str, qs.SignalResult]:
    return {
        "telemetry": qs.SignalResult(
            score=tel, evidence={"never_loaded": never_loaded}
        ),
        "intake": qs.SignalResult(
            score=intake, evidence={"hard_fail": intake_hard_fail}
        ),
        "graph": qs.SignalResult(score=graph, evidence={}),
        "routing": qs.SignalResult(score=routing, evidence={}),
    }


def test_compute_all_perfect_is_grade_a() -> None:
    r = sq.compute_quality(
        slug="demo",
        subject_type="skill",
        signals=_signals(),
        computed_at=_iso(NOW),
    )
    assert r.grade == "A"
    assert r.score == pytest.approx(1.0)
    assert r.hard_floor is None


def test_compute_intake_fail_forces_f() -> None:
    r = sq.compute_quality(
        slug="demo",
        subject_type="skill",
        signals=_signals(intake=0.5, intake_hard_fail=True),
        computed_at=_iso(NOW),
    )
    assert r.grade == "F"
    assert r.hard_floor == "intake_fail"


def test_compute_never_loaded_caps_at_d() -> None:
    # Structurally perfect but never loaded → D.
    r = sq.compute_quality(
        slug="demo",
        subject_type="skill",
        signals=_signals(tel=0.0, never_loaded=True),
        computed_at=_iso(NOW),
    )
    assert r.grade == "D"
    assert r.hard_floor == "never_loaded_stale"


def test_compute_never_loaded_does_not_upgrade_below_d() -> None:
    r = sq.compute_quality(
        slug="demo",
        subject_type="skill",
        signals=_signals(
            tel=0.0, intake=0.0, graph=0.0, routing=0.0, never_loaded=True
        ),
        computed_at=_iso(NOW),
    )
    assert r.grade == "D"  # was already D, stays D


def test_compute_grade_b_boundary() -> None:
    # Target raw = 0.60 exactly → grade B.
    r = sq.compute_quality(
        slug="demo",
        subject_type="skill",
        signals=_signals(tel=0.6, intake=0.6, graph=0.6, routing=0.6),
        computed_at=_iso(NOW),
    )
    assert r.grade == "B"
    assert r.score == pytest.approx(0.6)


def test_compute_grade_c_boundary() -> None:
    r = sq.compute_quality(
        slug="demo",
        subject_type="skill",
        signals=_signals(tel=0.4, intake=0.4, graph=0.4, routing=0.4),
        computed_at=_iso(NOW),
    )
    assert r.grade == "C"


def test_compute_rejects_missing_signal_key() -> None:
    sigs = _signals()
    del sigs["graph"]
    with pytest.raises(ValueError):
        sq.compute_quality(
            slug="demo", subject_type="skill", signals=sigs,
            computed_at=_iso(NOW),
        )


def test_compute_rejects_bad_subject_type() -> None:
    with pytest.raises(ValueError):
        sq.compute_quality(
            slug="demo", subject_type="weapon", signals=_signals(),
            computed_at=_iso(NOW),
        )


def test_compute_rejects_bad_slug() -> None:
    with pytest.raises(ValueError):
        sq.compute_quality(
            slug="../etc/passwd",
            subject_type="skill",
            signals=_signals(),
            computed_at=_iso(NOW),
        )


# ────────────────────────────────────────────────────────────────────
# compute_quality: agent parity (Phase 5)
# ────────────────────────────────────────────────────────────────────


def test_agent_uses_agent_weights_in_persisted_score() -> None:
    """QualityScore.weights reflects the vector actually used for scoring."""
    cfg = sq.QualityConfig()
    r = sq.compute_quality(
        slug="demo",
        subject_type="agent",
        signals=_signals(),
        config=cfg,
        computed_at=_iso(NOW),
    )
    assert dict(r.weights) == dict(cfg.agent_weights)


def test_skill_uses_skill_weights_in_persisted_score() -> None:
    cfg = sq.QualityConfig()
    r = sq.compute_quality(
        slug="demo",
        subject_type="skill",
        signals=_signals(),
        config=cfg,
        computed_at=_iso(NOW),
    )
    assert dict(r.weights) == dict(cfg.weights)


def test_agent_never_loaded_does_not_hit_never_loaded_stale() -> None:
    """Agents have no load-event stream — never_loaded is not a staleness signal."""
    r = sq.compute_quality(
        slug="demo",
        subject_type="agent",
        signals=_signals(tel=0.0, never_loaded=True),
        computed_at=_iso(NOW),
    )
    assert r.hard_floor is None
    # With agent weights (telemetry=0.15) and everything else at 1.0:
    # raw = 0.15*0 + 0.30*1 + 0.35*1 + 0.20*1 = 0.85 → grade A.
    assert r.score == pytest.approx(0.85)
    assert r.grade == "A"


def test_agent_intake_fail_still_forces_f() -> None:
    """Structural hard floor still applies to agents — a broken agent is broken."""
    r = sq.compute_quality(
        slug="demo",
        subject_type="agent",
        signals=_signals(intake=0.5, intake_hard_fail=True),
        computed_at=_iso(NOW),
    )
    assert r.grade == "F"
    assert r.hard_floor == "intake_fail"


def test_agent_weights_produce_higher_raw_than_skill_when_telemetry_zero() -> None:
    """Zero telemetry penalizes skills (0.40 weight) harder than agents (0.15)."""
    skill_signals = _signals(tel=0.0, intake=1.0, graph=1.0, routing=1.0)
    agent_signals = _signals(tel=0.0, intake=1.0, graph=1.0, routing=1.0)

    skill_score = sq.compute_quality(
        slug="demo", subject_type="skill", signals=skill_signals,
        computed_at=_iso(NOW),
    )
    agent_score = sq.compute_quality(
        slug="demo", subject_type="agent", signals=agent_signals,
        computed_at=_iso(NOW),
    )
    assert agent_score.raw_score > skill_score.raw_score


# ────────────────────────────────────────────────────────────────────
# Slug safety
# ────────────────────────────────────────────────────────────────────


def test_sidecar_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        sq.sidecar_path("../evil", sidecar_dir=tmp_path)


def test_sidecar_path_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        sq.sidecar_path("/etc/passwd", sidecar_dir=tmp_path)


def test_sidecar_path_accepts_valid(tmp_path: Path) -> None:
    assert sq.sidecar_path("demo-skill.1", sidecar_dir=tmp_path) == (
        tmp_path / "demo-skill.1.json"
    )


# ────────────────────────────────────────────────────────────────────
# extract_signals_for_slug
# ────────────────────────────────────────────────────────────────────


def test_extract_skill_reads_frontmatter_and_body(
    live_layout: dict[str, Path],
) -> None:
    src = _sources(live_layout)
    subject, sigs = sq.extract_signals_for_slug("demo", sources=src, now=NOW)
    assert subject == "skill"
    assert sigs["intake"].score == pytest.approx(1.0)
    assert sigs["telemetry"].evidence["never_loaded"] is True
    assert sigs["routing"].evidence["no_trace"] is True


def test_extract_counts_load_events(live_layout: dict[str, Path]) -> None:
    events = live_layout["events"]
    recent = _iso(NOW - timedelta(days=1))
    older = _iso(NOW - timedelta(days=40))
    events.write_text(
        json.dumps({"event": "load", "skill": "demo", "timestamp": recent}) + "\n"
        + json.dumps({"event": "load", "skill": "demo", "timestamp": older}) + "\n"
        + json.dumps({"event": "unload", "skill": "demo", "timestamp": recent}) + "\n"
        + json.dumps({"event": "load", "skill": "other", "timestamp": recent}) + "\n",
        encoding="utf-8",
    )
    subject, sigs = sq.extract_signals_for_slug(
        "demo", sources=_sources(live_layout), now=NOW
    )
    ev = sigs["telemetry"].evidence
    assert ev["load_count"] == 2
    assert ev["recent_load_count"] == 1
    assert ev["last_load_age_days"] == pytest.approx(1.0, abs=0.01)
    assert ev["never_loaded"] is False


def test_extract_skips_malformed_event_lines(
    live_layout: dict[str, Path],
) -> None:
    events = live_layout["events"]
    events.write_text(
        "not-json\n"
        + "{\"event\": \"load\", \"skill\": \"demo\", \"timestamp\": \""
        + _iso(NOW) + "\"}\n",
        encoding="utf-8",
    )
    _, sigs = sq.extract_signals_for_slug(
        "demo", sources=_sources(live_layout), now=NOW
    )
    assert sigs["telemetry"].evidence["load_count"] == 1


def test_extract_routing_trace_counts(live_layout: dict[str, Path]) -> None:
    trace = live_layout["root"] / "router-trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {"skill": "demo", "considered": True, "picked": True}
                ),
                json.dumps(
                    {"skill": "demo", "considered": True, "picked": False}
                ),
                json.dumps(
                    {"skill": "demo", "considered": True, "picked": True}
                ),
                json.dumps(
                    {"skill": "other", "considered": True, "picked": True}
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _, sigs = sq.extract_signals_for_slug(
        "demo",
        sources=_sources(live_layout, router_trace_path=trace),
        now=NOW,
    )
    ev = sigs["routing"].evidence
    assert ev["considered"] == 3
    assert ev["picked"] == 2


def test_extract_graph_index_lookup(live_layout: dict[str, Path]) -> None:
    index = {"skill:demo": {"degree": 10, "avg_edge_weight": 2.0}}
    _, sigs = sq.extract_signals_for_slug(
        "demo",
        sources=_sources(live_layout, graph_index=index),
        now=NOW,
    )
    assert sigs["graph"].score > 0.0
    assert sigs["graph"].evidence["degree"] == 10


def test_extract_unknown_slug_raises(live_layout: dict[str, Path]) -> None:
    with pytest.raises(FileNotFoundError):
        sq.extract_signals_for_slug(
            "does-not-exist", sources=_sources(live_layout), now=NOW
        )


def test_extract_agent_subject_type(live_layout: dict[str, Path]) -> None:
    subject, _ = sq.extract_signals_for_slug(
        "agent-one", sources=_sources(live_layout), now=NOW
    )
    assert subject == "agent"


# ────────────────────────────────────────────────────────────────────
# Persistence round-trip
# ────────────────────────────────────────────────────────────────────


def test_persist_writes_sidecar_and_loads_back(
    live_layout: dict[str, Path],
) -> None:
    src = _sources(live_layout)
    subject, sigs = sq.extract_signals_for_slug("demo", sources=src, now=NOW)
    score = sq.compute_quality(
        slug="demo", subject_type=subject, signals=sigs,
        computed_at=_iso(NOW),
    )
    written = sq.persist_quality(
        score, sources=src, sidecar_dir=live_layout["sidecar"]
    )
    assert "sidecar" in written
    assert written["sidecar"].is_file()

    loaded = sq.load_quality("demo", sidecar_dir=live_layout["sidecar"])
    assert loaded is not None
    assert loaded.slug == "demo"
    assert loaded.grade == score.grade
    assert loaded.score == pytest.approx(score.score, abs=1e-4)


def test_persist_injects_wiki_quality_section(
    live_layout: dict[str, Path],
) -> None:
    src = _sources(live_layout)
    subject, sigs = sq.extract_signals_for_slug("demo", sources=src, now=NOW)
    score = sq.compute_quality(
        slug="demo", subject_type=subject, signals=sigs,
        computed_at=_iso(NOW),
    )
    written = sq.persist_quality(
        score, sources=src, sidecar_dir=live_layout["sidecar"]
    )
    assert "wiki_body" in written
    page = written["wiki_body"].read_text(encoding="utf-8")
    assert "<!-- quality:begin -->" in page
    assert "<!-- quality:end -->" in page
    assert "## Quality" in page
    assert f"**Grade:** {score.grade}" in page
    assert "quality_score:" in page
    assert "quality_grade:" in page


def test_persist_wiki_section_is_idempotent(
    live_layout: dict[str, Path],
) -> None:
    src = _sources(live_layout)
    subject, sigs = sq.extract_signals_for_slug("demo", sources=src, now=NOW)
    score = sq.compute_quality(
        slug="demo", subject_type=subject, signals=sigs,
        computed_at=_iso(NOW),
    )
    sq.persist_quality(
        score, sources=src, sidecar_dir=live_layout["sidecar"]
    )
    sq.persist_quality(
        score, sources=src, sidecar_dir=live_layout["sidecar"]
    )
    page = (
        live_layout["wiki"] / "entities" / "skills" / "demo.md"
    ).read_text(encoding="utf-8")
    assert page.count("<!-- quality:begin -->") == 1
    assert page.count("<!-- quality:end -->") == 1
    assert page.count("quality_score:") == 1


def test_persist_skips_missing_wiki_page(
    live_layout: dict[str, Path],
) -> None:
    (live_layout["wiki"] / "entities" / "skills" / "demo.md").unlink()
    src = _sources(live_layout)
    subject, sigs = sq.extract_signals_for_slug("demo", sources=src, now=NOW)
    score = sq.compute_quality(
        slug="demo", subject_type=subject, signals=sigs,
        computed_at=_iso(NOW),
    )
    written = sq.persist_quality(
        score, sources=src, sidecar_dir=live_layout["sidecar"]
    )
    assert "sidecar" in written
    assert "frontmatter" not in written
    assert "wiki_body" not in written


def test_persist_updates_hard_floor_then_clears(
    live_layout: dict[str, Path],
) -> None:
    """Second run with no floor must strip the previous ``quality_hard_floor``."""
    src = _sources(live_layout)

    # First: force intake_fail by mocking signals.
    bad_sigs = _signals(intake=0.5, intake_hard_fail=True)
    score_bad = sq.compute_quality(
        slug="demo", subject_type="skill", signals=bad_sigs,
        computed_at=_iso(NOW),
    )
    sq.persist_quality(
        score_bad, sources=src, sidecar_dir=live_layout["sidecar"]
    )
    page1 = (
        live_layout["wiki"] / "entities" / "skills" / "demo.md"
    ).read_text(encoding="utf-8")
    assert "quality_hard_floor: intake_fail" in page1

    # Second: clean run.
    good_sigs = _signals()
    score_good = sq.compute_quality(
        slug="demo", subject_type="skill", signals=good_sigs,
        computed_at=_iso(NOW),
    )
    sq.persist_quality(
        score_good, sources=src, sidecar_dir=live_layout["sidecar"]
    )
    page2 = (
        live_layout["wiki"] / "entities" / "skills" / "demo.md"
    ).read_text(encoding="utf-8")
    assert "quality_hard_floor: intake_fail" not in page2


def test_load_quality_missing_returns_none(tmp_path: Path) -> None:
    assert sq.load_quality("demo", sidecar_dir=tmp_path) is None


# ────────────────────────────────────────────────────────────────────
# discover_slugs
# ────────────────────────────────────────────────────────────────────


def test_discover_enumerates_skills_and_agents(
    live_layout: dict[str, Path],
) -> None:
    src = _sources(live_layout)
    found = sq.discover_slugs(src)
    assert ("skill", "demo") in found
    assert ("agent", "agent-one") in found


def test_discover_skips_agent_when_skill_shadows_it(
    live_layout: dict[str, Path],
) -> None:
    (live_layout["agents"] / "demo.md").write_text(
        _good_skill_body(), encoding="utf-8"
    )
    src = _sources(live_layout)
    found = dict((slug, subject) for subject, slug in sq.discover_slugs(src))
    assert found["demo"] == "skill"  # skill wins over agent


# ────────────────────────────────────────────────────────────────────
# recompute_slug end-to-end
# ────────────────────────────────────────────────────────────────────


def test_recompute_slug_writes_all_sinks(
    live_layout: dict[str, Path],
) -> None:
    score = sq.recompute_slug(
        "demo",
        sources=_sources(live_layout),
        now=NOW,
        sidecar_dir=live_layout["sidecar"],
    )
    assert score.slug == "demo"
    assert (live_layout["sidecar"] / "demo.json").is_file()
    page = (
        live_layout["wiki"] / "entities" / "skills" / "demo.md"
    ).read_text(encoding="utf-8")
    assert "<!-- quality:begin -->" in page


# ────────────────────────────────────────────────────────────────────
# _config_from_cfg
# ────────────────────────────────────────────────────────────────────


def test_config_from_cfg_reads_agent_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom agent_weights in the config block propagate into QualityConfig."""
    import ctx_config as cc

    custom_agent_weights = {
        "telemetry": 0.10, "intake": 0.40, "graph": 0.30, "routing": 0.20,
    }

    class FakeCfg:
        def get(self, key: str, default=None):
            if key == "quality":
                return {"agent_weights": custom_agent_weights}
            return default

    monkeypatch.setattr(cc, "cfg", FakeCfg(), raising=True)
    cfg = sq._config_from_cfg()
    assert dict(cfg.agent_weights) == custom_agent_weights
    # Skill weights should fall back to defaults since config didn't override them.
    assert dict(cfg.weights) == dict(sq._DEFAULT_WEIGHTS)


def test_config_from_cfg_with_empty_quality_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing config block → defaults for both vectors, no crash."""
    import ctx_config as cc

    class FakeCfg:
        def get(self, key: str, default=None):
            return default

    monkeypatch.setattr(cc, "cfg", FakeCfg(), raising=True)
    cfg = sq._config_from_cfg()
    assert dict(cfg.weights) == dict(sq._DEFAULT_WEIGHTS)
    assert dict(cfg.agent_weights) == dict(sq._DEFAULT_AGENT_WEIGHTS)


# ────────────────────────────────────────────────────────────────────
# CLI smoke tests
# ────────────────────────────────────────────────────────────────────


def _cli_setup(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
) -> None:
    """Patch ctx_config.cfg + default_sidecar_dir so CLI points at tmp."""
    import ctx_config as cc

    class FakeCfg:
        skills_dir = live_layout["skills"]
        agents_dir = live_layout["agents"]
        wiki_dir = live_layout["wiki"]

        def get(self, key: str, default=None):
            if key == "quality":
                return {}
            return default

    monkeypatch.setattr(cc, "cfg", FakeCfg(), raising=True)
    # Reload the module's reference through sq._build_sources_from_config.
    monkeypatch.setattr(
        sq, "default_sidecar_dir", lambda: live_layout["sidecar"]
    )
    # Redirect the events path used by _build_sources_from_config.
    real_build = sq._build_sources_from_config

    def patched_build() -> sq.SignalSources:
        src = real_build()
        return sq.SignalSources(
            skills_dir=src.skills_dir,
            agents_dir=src.agents_dir,
            wiki_dir=src.wiki_dir,
            events_path=live_layout["events"],
            router_trace_path=src.router_trace_path,
            graph_index=src.graph_index,
        )

    monkeypatch.setattr(sq, "_build_sources_from_config", patched_build)


def test_cli_recompute_single_slug(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_setup(monkeypatch, live_layout)
    rc = sq.main(["recompute", "--slug", "demo", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["count"] == 1
    assert parsed["results"][0]["slug"] == "demo"


def test_cli_recompute_all(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_setup(monkeypatch, live_layout)
    rc = sq.main(["recompute", "--all", "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    slugs = {r["slug"] for r in parsed["results"]}
    assert "demo" in slugs
    assert "agent-one" in slugs


def test_cli_recompute_requires_target(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
) -> None:
    _cli_setup(monkeypatch, live_layout)
    rc = sq.main(["recompute"])
    assert rc == 2


def test_cli_show_after_recompute(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_setup(monkeypatch, live_layout)
    sq.main(["recompute", "--slug", "demo"])
    capsys.readouterr()  # drain
    rc = sq.main(["show", "demo", "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["slug"] == "demo"
    assert "grade" in parsed


def test_cli_show_missing_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
) -> None:
    _cli_setup(monkeypatch, live_layout)
    rc = sq.main(["show", "demo"])
    assert rc == 1


def test_cli_explain_includes_evidence(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_setup(monkeypatch, live_layout)
    sq.main(["recompute", "--slug", "demo"])
    capsys.readouterr()
    rc = sq.main(["explain", "demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "telemetry" in out
    assert "intake" in out
    assert "graph" in out
    assert "routing" in out


def test_cli_list_filters_by_grade(
    monkeypatch: pytest.MonkeyPatch,
    live_layout: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_setup(monkeypatch, live_layout)
    sq.main(["recompute", "--all"])
    capsys.readouterr()
    rc = sq.main(["list", "--json"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) >= 1
    # Filter test: impossible grade returns zero rows.
    rc = sq.main(["list", "--grade", "Z", "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == []
