"""
test_quality_signals.py -- Coverage sprint for quality_signals.py (P3).

Targets gaps NOT covered by test_skill_quality.py:

  TestSignalResultEdges     : evidence default, in-range exact boundaries,
                              negative_inf, immutability (frozen dataclass)
  TestTelemetrySignalEdges  : negative recent_load_count, negative age,
                              partial recency decay, age-at-threshold,
                              recent saturation steps, evidence field names
  TestIntakeSignalEdges     : whitespace-only name/desc, body at boundary,
                              H1/H2 regex multiline edge cases,
                              non-Mapping frontmatter, all-fail path,
                              evidence completeness
  TestGraphSignalEdges      : negative avg_edge_weight rejection,
                              zero avg_edge_weight, weight bonus slope,
                              monotonicity across degree range,
                              degree above saturation, evidence fields
  TestRoutingSignalEdges    : negative considered, negative picked,
                              exactly at MIN_OBSERVATIONS boundary,
                              all-picked hit_rate, evidence field names,
                              zero-considered hit_rate evidence
  TestModuleExports         : __all__ completeness
  TestConstants             : public constant values stable
"""

from __future__ import annotations

import math

import pytest

import quality_signals as qs


# ────────────────────────────────────────────────────────────────────
# TestSignalResultEdges
# ────────────────────────────────────────────────────────────────────


class TestSignalResultEdges:
    def test_evidence_defaults_to_empty_dict(self) -> None:
        r = qs.SignalResult(score=0.5)
        assert r.evidence == {}

    def test_score_exactly_zero_accepted(self) -> None:
        r = qs.SignalResult(score=0.0)
        assert r.score == 0.0

    def test_score_exactly_one_accepted(self) -> None:
        r = qs.SignalResult(score=1.0)
        assert r.score == 1.0

    def test_score_negative_inf_raises(self) -> None:
        with pytest.raises(ValueError):
            qs.SignalResult(score=float("-inf"))

    def test_frozen_dataclass_rejects_attribute_mutation(self) -> None:
        r = qs.SignalResult(score=0.5)
        with pytest.raises((AttributeError, TypeError)):
            r.score = 0.9  # type: ignore[misc]

    def test_evidence_dict_preserved_as_given(self) -> None:
        ev = {"key": "value", "count": 42}
        r = qs.SignalResult(score=0.5, evidence=ev)
        assert r.evidence["key"] == "value"
        assert r.evidence["count"] == 42

    def test_clamping_slightly_above_one(self) -> None:
        r = qs.SignalResult(score=1.0001)
        assert r.score == 1.0

    def test_clamping_slightly_below_zero(self) -> None:
        r = qs.SignalResult(score=-0.0001)
        assert r.score == 0.0


# ────────────────────────────────────────────────────────────────────
# TestTelemetrySignalEdges
# ────────────────────────────────────────────────────────────────────


class TestTelemetrySignalEdges:
    def test_rejects_negative_recent_load_count(self) -> None:
        with pytest.raises(ValueError):
            qs.telemetry_signal(
                load_count=0,
                recent_load_count=-1,
                last_load_age_days=None,
                stale_threshold_days=30.0,
            )

    def test_rejects_negative_last_load_age_days(self) -> None:
        with pytest.raises(ValueError):
            qs.telemetry_signal(
                load_count=1,
                recent_load_count=0,
                last_load_age_days=-1.0,
                stale_threshold_days=30.0,
            )

    def test_age_exactly_at_threshold_recency_is_zero(self) -> None:
        """At exactly stale_threshold_days the recency term decays to 0."""
        r = qs.telemetry_signal(
            load_count=1,
            recent_load_count=0,
            last_load_age_days=30.0,
            stale_threshold_days=30.0,
        )
        # ever_loaded_term=1.0, recent_term=0, recency_term=0 → 0.35
        assert r.score == pytest.approx(qs._TELEMETRY_EVER_LOADED_WEIGHT)

    def test_age_half_of_threshold_partial_recency(self) -> None:
        """At half the stale threshold recency_term = 0.5."""
        r = qs.telemetry_signal(
            load_count=1,
            recent_load_count=0,
            last_load_age_days=15.0,
            stale_threshold_days=30.0,
        )
        # ever_loaded=0.35, recent=0, recency=0.30*0.5=0.15 → 0.50
        expected = (
            qs._TELEMETRY_EVER_LOADED_WEIGHT * 1.0
            + qs._TELEMETRY_RECENT_WEIGHT * 0.0
            + qs._TELEMETRY_RECENCY_WEIGHT * 0.5
        )
        assert r.score == pytest.approx(expected)

    def test_recent_saturation_at_exactly_threshold(self) -> None:
        """recent_load_count == saturation should give recent_term = 1.0."""
        r = qs.telemetry_signal(
            load_count=3,
            recent_load_count=qs._TELEMETRY_RECENT_SATURATION,
            last_load_age_days=0.0,
            stale_threshold_days=30.0,
        )
        assert r.score == pytest.approx(1.0)

    def test_recent_count_one_gives_partial_recent_term(self) -> None:
        """1 recent load → recent_term = 1/3."""
        r = qs.telemetry_signal(
            load_count=1,
            recent_load_count=1,
            last_load_age_days=0.0,
            stale_threshold_days=30.0,
        )
        expected = (
            qs._TELEMETRY_EVER_LOADED_WEIGHT * 1.0
            + qs._TELEMETRY_RECENT_WEIGHT * (1.0 / qs._TELEMETRY_RECENT_SATURATION)
            + qs._TELEMETRY_RECENCY_WEIGHT * 1.0
        )
        assert r.score == pytest.approx(expected)

    def test_recent_count_two_gives_two_thirds_recent_term(self) -> None:
        """2 recent loads → recent_term = 2/3."""
        r = qs.telemetry_signal(
            load_count=2,
            recent_load_count=2,
            last_load_age_days=0.0,
            stale_threshold_days=30.0,
        )
        expected = (
            qs._TELEMETRY_EVER_LOADED_WEIGHT * 1.0
            + qs._TELEMETRY_RECENT_WEIGHT * (2.0 / qs._TELEMETRY_RECENT_SATURATION)
            + qs._TELEMETRY_RECENCY_WEIGHT * 1.0
        )
        assert r.score == pytest.approx(expected)

    def test_evidence_contains_all_required_keys(self) -> None:
        r = qs.telemetry_signal(
            load_count=5,
            recent_load_count=2,
            last_load_age_days=7.0,
            stale_threshold_days=30.0,
        )
        assert "load_count" in r.evidence
        assert "recent_load_count" in r.evidence
        assert "last_load_age_days" in r.evidence
        assert "never_loaded" in r.evidence

    def test_evidence_values_match_inputs(self) -> None:
        r = qs.telemetry_signal(
            load_count=7,
            recent_load_count=3,
            last_load_age_days=5.0,
            stale_threshold_days=14.0,
        )
        assert r.evidence["load_count"] == 7
        assert r.evidence["recent_load_count"] == 3
        assert r.evidence["last_load_age_days"] == pytest.approx(5.0)
        assert r.evidence["never_loaded"] is False

    def test_zero_load_count_sets_never_loaded_true(self) -> None:
        r = qs.telemetry_signal(
            load_count=0,
            recent_load_count=0,
            last_load_age_days=None,
            stale_threshold_days=30.0,
        )
        assert r.evidence["never_loaded"] is True
        assert r.evidence["last_load_age_days"] is None

    def test_age_zero_gives_full_recency(self) -> None:
        """last_load_age_days=0 gives recency_term=1.0."""
        r = qs.telemetry_signal(
            load_count=1,
            recent_load_count=0,
            last_load_age_days=0.0,
            stale_threshold_days=30.0,
        )
        expected = (
            qs._TELEMETRY_EVER_LOADED_WEIGHT * 1.0
            + qs._TELEMETRY_RECENT_WEIGHT * 0.0
            + qs._TELEMETRY_RECENCY_WEIGHT * 1.0
        )
        assert r.score == pytest.approx(expected)


# ────────────────────────────────────────────────────────────────────
# TestIntakeSignalEdges
# ────────────────────────────────────────────────────────────────────


class TestIntakeSignalEdges:
    def _make_good_body(self) -> str:
        return "# H1 Title\n\n## H2 Section\n\n" + ("body content " * 20)

    def test_whitespace_only_name_fails_has_name(self) -> None:
        body = self._make_good_body()
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "   ", "description": "valid desc"},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=50,
        )
        assert r.evidence["checks"]["has_name"] is False
        assert r.evidence["hard_fail"] is True

    def test_whitespace_only_description_fails_has_description(self) -> None:
        body = self._make_good_body()
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "valid", "description": "\t\n  "},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=50,
        )
        assert r.evidence["checks"]["has_description"] is False
        assert r.evidence["hard_fail"] is True

    def test_body_exactly_at_min_chars_passes(self) -> None:
        body_text = "# H1\n\n## H2\n\n" + "x" * 10
        exact_len = len(body_text.strip())
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "ok", "description": "ok desc"},
            has_frontmatter_block=True,
            body=body_text,
            min_body_chars=exact_len,
        )
        assert r.evidence["checks"]["body_long_enough"] is True

    def test_body_one_char_short_fails(self) -> None:
        body_text = "# H1\n\n## H2\n\n" + "x" * 10
        exact_len = len(body_text.strip())
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "ok", "description": "ok desc"},
            has_frontmatter_block=True,
            body=body_text,
            min_body_chars=exact_len + 1,
        )
        assert r.evidence["checks"]["body_long_enough"] is False

    def test_min_body_chars_zero_always_passes_body_check(self) -> None:
        """min_body_chars=0 means empty body is still long enough."""
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "ok", "description": "ok desc"},
            has_frontmatter_block=True,
            body="# H1\n\n## H2\n",
            min_body_chars=0,
        )
        assert r.evidence["checks"]["body_long_enough"] is True

    def test_h1_not_matched_by_h2_prefix(self) -> None:
        """A line starting with ## should not satisfy the H1 regex."""
        body = "## Only H2\n\nsome content padding padding padding\n"
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "ok", "description": "ok desc"},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=0,
        )
        assert r.evidence["checks"]["has_h1"] is False
        assert r.evidence["checks"]["has_h2"] is True

    def test_h1_regex_requires_non_whitespace_eventually(self) -> None:
        """A lone '#' with nothing after (no non-whitespace on any following line) fails H1.

        The regex is r'^\\#\\s+\\S' (MULTILINE). It requires at least one \\s char
        then a \\S — an entirely empty document with only '#\\n' and no following
        non-whitespace content should fail.
        """
        body = "#\n"  # hash then immediate newline — \s+ can't match (zero whitespace chars)
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "ok", "description": "ok desc"},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=0,
        )
        assert r.evidence["checks"]["has_h1"] is False

    def test_h1_mid_body_is_detected(self) -> None:
        """H1 anywhere in the body (not just the first line) should pass."""
        body = "Some intro text.\n\n# Title Here\n\n## Section\n\n"
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "ok", "description": "ok desc"},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=0,
        )
        assert r.evidence["checks"]["has_h1"] is True

    def test_all_checks_fail_scores_zero(self) -> None:
        """No frontmatter, empty body, no H1/H2 → score 0."""
        r = qs.intake_signal(
            raw_md="",
            frontmatter={},
            has_frontmatter_block=False,
            body="",
            min_body_chars=10,
        )
        assert r.score == pytest.approx(0.0)
        assert r.evidence["hard_fail"] is True
        assert r.evidence["passed"] == 0

    def test_evidence_contains_all_six_checks(self) -> None:
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "n", "description": "d"},
            has_frontmatter_block=True,
            body="# H1\n\n## H2\n\npadding " * 5,
            min_body_chars=0,
        )
        checks = r.evidence["checks"]
        assert set(checks.keys()) == set(qs._INTAKE_CHECKS)

    def test_evidence_passed_and_total_consistent(self) -> None:
        body = "# H1\n\n## H2\n\n" + "padding " * 20
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "n", "description": "d"},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=10,
        )
        assert r.evidence["passed"] == r.evidence["total"]
        assert r.evidence["total"] == len(qs._INTAKE_CHECKS)

    def test_frontmatter_block_true_but_empty_dict_fails_has_frontmatter(self) -> None:
        """Block present but empty dict → has_frontmatter is False."""
        body = "# H1\n\n## H2\n\n" + "x" * 50
        r = qs.intake_signal(
            raw_md="",
            frontmatter={},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=10,
        )
        assert r.evidence["checks"]["has_frontmatter"] is False

    def test_unicode_name_and_description_accepted(self) -> None:
        """Unicode content in frontmatter fields should pass has_name / has_description."""
        body = "# Title\n\n## Section\n\n" + "content " * 20
        r = qs.intake_signal(
            raw_md="",
            frontmatter={"name": "技能名称", "description": "Une description en français."},
            has_frontmatter_block=True,
            body=body,
            min_body_chars=10,
        )
        assert r.evidence["checks"]["has_name"] is True
        assert r.evidence["checks"]["has_description"] is True

    @pytest.mark.parametrize(
        "passed_count,expected_score",
        [
            (0, 0.0),
            (1, 1 / 6),
            (2, 2 / 6),
            (3, 3 / 6),
            (4, 4 / 6),
            (5, 5 / 6),
            (6, 1.0),
        ],
    )
    def test_score_equals_passed_over_total(
        self, passed_count: int, expected_score: float
    ) -> None:
        """Score is always passed/6; verify each count individually."""
        # Build bodies/frontmatter so exactly `passed_count` checks pass.
        # Checks in order: has_frontmatter, has_name, has_description, has_h1, has_h2, body_long_enough
        all_checks = list(qs._INTAKE_CHECKS)
        # We'll turn checks on from the top of the list.
        active = set(all_checks[:passed_count])

        has_fm_block = "has_frontmatter" in active
        fm: dict = {}
        if "has_frontmatter" in active:
            fm["__present__"] = True  # any non-empty dict
        if "has_name" in active:
            fm["name"] = "test-name"
        if "has_description" in active:
            fm["description"] = "test description"

        h1_line = "# Header\n" if "has_h1" in active else ""
        h2_line = "## Subheader\n" if "has_h2" in active else ""
        padding = "x" * 50 if "body_long_enough" in active else ""
        body = h1_line + h2_line + padding

        r = qs.intake_signal(
            raw_md="",
            frontmatter=fm,
            has_frontmatter_block=has_fm_block,
            body=body,
            min_body_chars=50,
        )
        assert r.score == pytest.approx(expected_score), (
            f"passed={passed_count}, active={active}, checks={r.evidence['checks']}"
        )


# ────────────────────────────────────────────────────────────────────
# TestGraphSignalEdges
# ────────────────────────────────────────────────────────────────────


class TestGraphSignalEdges:
    def test_rejects_negative_avg_edge_weight(self) -> None:
        with pytest.raises(ValueError):
            qs.graph_signal(degree=1, avg_edge_weight=-0.1)

    def test_zero_avg_edge_weight_still_scores_non_zero_for_connected(self) -> None:
        """avg_edge_weight=0 gives bonus=0 but base score is still positive."""
        r = qs.graph_signal(degree=5, avg_edge_weight=0.0)
        assert r.score > 0.0

    def test_weight_bonus_zero_when_avg_weight_is_one(self) -> None:
        """avg_edge_weight=1.0 is the neutral point; bonus should be 0."""
        r_neutral = qs.graph_signal(degree=5, avg_edge_weight=1.0)
        base = math.log1p(5) / math.log1p(qs._GRAPH_SATURATION)
        assert r_neutral.score == pytest.approx(base, abs=1e-9)

    def test_weight_bonus_caps_at_0_10(self) -> None:
        """avg_edge_weight far above 5 should give exactly +0.10 bonus."""
        r_low = qs.graph_signal(degree=5, avg_edge_weight=1.0)
        r_high = qs.graph_signal(degree=5, avg_edge_weight=100.0)
        diff = r_high.score - r_low.score
        assert diff == pytest.approx(0.10, abs=1e-6)

    def test_monotonic_degree_scores(self) -> None:
        degrees = [0, 1, 2, 5, 10, 15, 20]
        scores = [qs.graph_signal(degree=d).score for d in degrees]
        for i in range(len(scores) - 1):
            assert scores[i] <= scores[i + 1], (
                f"score[{degrees[i]}]={scores[i]} > score[{degrees[i+1]}]={scores[i+1]}"
            )

    def test_degree_above_saturation_clamped_to_one(self) -> None:
        """degree >> _GRAPH_SATURATION should still give score <= 1.0."""
        r = qs.graph_signal(degree=qs._GRAPH_SATURATION * 10, avg_edge_weight=1.0)
        assert r.score <= 1.0

    def test_evidence_contains_degree_avg_edge_weight_is_isolated(self) -> None:
        r = qs.graph_signal(degree=3, avg_edge_weight=2.0)
        assert "degree" in r.evidence
        assert "avg_edge_weight" in r.evidence
        assert "is_isolated" in r.evidence

    def test_evidence_degree_matches_input(self) -> None:
        r = qs.graph_signal(degree=7, avg_edge_weight=1.5)
        assert r.evidence["degree"] == 7
        assert r.evidence["avg_edge_weight"] == pytest.approx(1.5)

    def test_evidence_is_isolated_false_for_connected(self) -> None:
        r = qs.graph_signal(degree=1)
        assert r.evidence["is_isolated"] is False

    def test_default_avg_edge_weight_is_one(self) -> None:
        """avg_edge_weight defaults to 1.0 — no bonus."""
        r_default = qs.graph_signal(degree=5)
        r_explicit = qs.graph_signal(degree=5, avg_edge_weight=1.0)
        assert r_default.score == pytest.approx(r_explicit.score)

    @pytest.mark.parametrize("degree", [1, 5, 10, 20])
    def test_log_scale_formula_matches(self, degree: int) -> None:
        """Verify the exact log1p formula for various degrees (no weight bonus)."""
        r = qs.graph_signal(degree=degree, avg_edge_weight=1.0)
        expected = math.log1p(degree) / math.log1p(qs._GRAPH_SATURATION)
        assert r.score == pytest.approx(expected, abs=1e-9)


# ────────────────────────────────────────────────────────────────────
# TestRoutingSignalEdges
# ────────────────────────────────────────────────────────────────────


class TestRoutingSignalEdges:
    def test_rejects_negative_considered(self) -> None:
        with pytest.raises(ValueError):
            qs.routing_signal(considered=-1, picked=0)

    def test_rejects_negative_picked(self) -> None:
        with pytest.raises(ValueError):
            qs.routing_signal(considered=5, picked=-1)

    def test_exactly_at_min_observations_not_neutral(self) -> None:
        """considered == _ROUTING_MIN_OBSERVATIONS is NOT below the minimum — it uses the hit rate."""
        r = qs.routing_signal(
            considered=qs._ROUTING_MIN_OBSERVATIONS,
            picked=qs._ROUTING_MIN_OBSERVATIONS,
        )
        # no_trace = False because considered >= min
        assert r.evidence["no_trace"] is False
        assert r.score == pytest.approx(1.0)

    def test_one_below_min_observations_is_neutral(self) -> None:
        """considered == _ROUTING_MIN_OBSERVATIONS - 1 is still below minimum."""
        r = qs.routing_signal(
            considered=qs._ROUTING_MIN_OBSERVATIONS - 1,
            picked=qs._ROUTING_MIN_OBSERVATIONS - 1,
        )
        assert r.evidence["no_trace"] is True
        assert r.score == pytest.approx(qs._ROUTING_NEUTRAL_SCORE)

    def test_zero_hits_from_sufficient_observations(self) -> None:
        """Zero picks out of enough considered yields score=0."""
        r = qs.routing_signal(
            considered=qs._ROUTING_MIN_OBSERVATIONS + 2, picked=0
        )
        assert r.score == pytest.approx(0.0)
        assert r.evidence["hit_rate"] == pytest.approx(0.0)

    def test_evidence_hit_rate_correct(self) -> None:
        r = qs.routing_signal(considered=10, picked=4)
        assert r.evidence["hit_rate"] == pytest.approx(0.4)

    def test_evidence_hit_rate_zero_when_no_considered(self) -> None:
        """With considered=0 the hit_rate is 0.0 (not NaN)."""
        r = qs.routing_signal(considered=0, picked=0)
        assert r.evidence["hit_rate"] == pytest.approx(0.0)
        assert not math.isnan(r.evidence["hit_rate"])

    def test_evidence_contains_all_four_keys(self) -> None:
        r = qs.routing_signal(considered=5, picked=3)
        assert "considered" in r.evidence
        assert "picked" in r.evidence
        assert "hit_rate" in r.evidence
        assert "no_trace" in r.evidence

    def test_evidence_values_match_inputs(self) -> None:
        r = qs.routing_signal(considered=8, picked=6)
        assert r.evidence["considered"] == 8
        assert r.evidence["picked"] == 6

    def test_neutral_score_constant_value(self) -> None:
        assert qs._ROUTING_NEUTRAL_SCORE == pytest.approx(0.5)

    @pytest.mark.parametrize(
        "considered,picked,expected_score",
        [
            (5, 5, 1.0),
            (10, 5, 0.5),
            (10, 0, 0.0),
            (100, 75, 0.75),
        ],
    )
    def test_score_is_hit_rate_when_sufficient_observations(
        self, considered: int, picked: int, expected_score: float
    ) -> None:
        r = qs.routing_signal(considered=considered, picked=picked)
        assert r.score == pytest.approx(expected_score)


# ────────────────────────────────────────────────────────────────────
# TestModuleExports
# ────────────────────────────────────────────────────────────────────


class TestModuleExports:
    def test_all_contains_signal_result(self) -> None:
        assert "SignalResult" in qs.__all__

    def test_all_contains_telemetry_signal(self) -> None:
        assert "telemetry_signal" in qs.__all__

    def test_all_contains_intake_signal(self) -> None:
        assert "intake_signal" in qs.__all__

    def test_all_contains_graph_signal(self) -> None:
        assert "graph_signal" in qs.__all__

    def test_all_contains_routing_signal(self) -> None:
        assert "routing_signal" in qs.__all__

    def test_all_has_exactly_five_entries(self) -> None:
        assert len(qs.__all__) == 5

    def test_all_names_are_importable(self) -> None:
        for name in qs.__all__:
            assert hasattr(qs, name), f"{name!r} listed in __all__ but not present"


# ────────────────────────────────────────────────────────────────────
# TestConstants
# ────────────────────────────────────────────────────────────────────


class TestConstants:
    def test_telemetry_weights_sum_to_one(self) -> None:
        total = (
            qs._TELEMETRY_EVER_LOADED_WEIGHT
            + qs._TELEMETRY_RECENT_WEIGHT
            + qs._TELEMETRY_RECENCY_WEIGHT
        )
        assert total == pytest.approx(1.0)

    def test_telemetry_recent_saturation_positive(self) -> None:
        assert qs._TELEMETRY_RECENT_SATURATION > 0

    def test_graph_saturation_is_positive(self) -> None:
        assert qs._GRAPH_SATURATION > 0

    def test_intake_checks_has_six_entries(self) -> None:
        assert len(qs._INTAKE_CHECKS) == 6

    def test_intake_checks_contains_expected_names(self) -> None:
        expected = {
            "has_frontmatter",
            "has_name",
            "has_description",
            "has_h1",
            "has_h2",
            "body_long_enough",
        }
        assert set(qs._INTAKE_CHECKS) == expected

    def test_routing_min_observations_positive(self) -> None:
        assert qs._ROUTING_MIN_OBSERVATIONS > 0

    def test_routing_neutral_score_between_zero_and_one(self) -> None:
        assert 0.0 <= qs._ROUTING_NEUTRAL_SCORE <= 1.0
