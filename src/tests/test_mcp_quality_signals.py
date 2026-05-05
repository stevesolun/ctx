"""
test_mcp_quality_signals.py -- Unit tests for the six pure MCP quality-signal
extractors in ``mcp_quality_signals``.

Coverage:
  - TestPopularity    : 5 cases for popularity_signal
  - TestFreshness     : 5 cases for freshness_signal
  - TestStructural    : 6 cases for structural_signal
  - TestGraph         : 5 cases for graph_signal
  - TestTrust         : 5 cases for trust_signal
  - TestRuntime       : 5 cases for runtime_signal
  - TestNegativeInputsRaise : 4 parametrized ValueError cases
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SRC_DIR = Path(__file__).resolve().parents[1]
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import mcp_quality_signals as mqs  # noqa: E402
from mcp_entity import McpRecord  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(**kwargs: Any) -> McpRecord:
    """Minimal McpRecord builder; callers override only what they need."""
    defaults: dict[str, Any] = {
        "name": "test-mcp",
        "description": "A test MCP server with sufficient description text.",
        "sources": ["awesome-mcp"],
        "github_url": "https://github.com/org/test-mcp",
        "homepage_url": None,
        "tags": ["utility"],
        "transports": ["stdio"],
        "language": "python",
        "license": "MIT",
        "author": "test-author",
        "author_type": "individual",
        "stars": None,
        "last_commit_at": None,
    }
    defaults.update(kwargs)
    return McpRecord.from_dict(defaults)


# ---------------------------------------------------------------------------
# TestPopularity
# ---------------------------------------------------------------------------


class TestPopularity:
    def test_none_stars_returns_neutral(self) -> None:
        r = mqs.popularity_signal(stars=None)
        assert r.score == pytest.approx(0.5)

    def test_zero_stars_scores_zero(self) -> None:
        r = mqs.popularity_signal(stars=0)
        assert r.score == pytest.approx(0.0)

    def test_saturation_stars_scores_one(self) -> None:
        r = mqs.popularity_signal(stars=1000)
        assert r.score == pytest.approx(1.0)

    def test_monotonic_across_common_values(self) -> None:
        scores = [
            mqs.popularity_signal(stars=s).score for s in [10, 100, 1000]
        ]
        assert scores[0] < scores[1] < scores[2]

    def test_above_saturation_clamped_to_one(self) -> None:
        r = mqs.popularity_signal(stars=5000)
        assert r.score == pytest.approx(1.0)

    def test_evidence_contains_stars_and_neutral_flag(self) -> None:
        r = mqs.popularity_signal(stars=42)
        assert "stars" in r.evidence
        assert "neutral_for_unknown" in r.evidence
        assert r.evidence["neutral_for_unknown"] is False

    def test_evidence_neutral_flag_set_when_none(self) -> None:
        r = mqs.popularity_signal(stars=None)
        assert r.evidence["neutral_for_unknown"] is True


# ---------------------------------------------------------------------------
# TestFreshness
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_none_age_returns_neutral(self) -> None:
        r = mqs.freshness_signal(last_commit_age_days=None)
        assert r.score == pytest.approx(0.5)

    def test_zero_age_scores_one(self) -> None:
        r = mqs.freshness_signal(last_commit_age_days=0.0)
        assert r.score == pytest.approx(1.0)

    def test_half_life_age_scores_half(self) -> None:
        r = mqs.freshness_signal(last_commit_age_days=90.0, half_life_days=90.0)
        assert r.score == pytest.approx(0.5, abs=1e-6)

    def test_double_half_life_scores_quarter(self) -> None:
        r = mqs.freshness_signal(last_commit_age_days=180.0, half_life_days=90.0)
        assert r.score == pytest.approx(0.25, abs=1e-6)

    def test_custom_half_life_respected(self) -> None:
        # At 30 days with half_life=30, score must be ~0.5.
        r = mqs.freshness_signal(last_commit_age_days=30.0, half_life_days=30.0)
        assert r.score == pytest.approx(0.5, abs=1e-6)

    def test_evidence_contains_age_and_neutral_flag(self) -> None:
        r = mqs.freshness_signal(last_commit_age_days=45.0)
        assert "last_commit_age_days" in r.evidence
        assert "neutral_for_unknown" in r.evidence
        assert r.evidence["neutral_for_unknown"] is False

    def test_evidence_neutral_flag_set_when_none(self) -> None:
        r = mqs.freshness_signal(last_commit_age_days=None)
        assert r.evidence["neutral_for_unknown"] is True


# ---------------------------------------------------------------------------
# TestStructural
# ---------------------------------------------------------------------------


class TestStructural:
    def test_full_record_scores_one(self) -> None:
        record = _make_record()
        r = mqs.structural_signal(record=record)
        assert r.score == pytest.approx(1.0)

    def test_missing_description_scores_four_fifths(self) -> None:
        # Omit description; 4/5 checks pass.
        record = _make_record(description="")
        r = mqs.structural_signal(record=record)
        assert r.score == pytest.approx(4 / 5)

    def test_only_uncategorized_tag_reduces_score(self) -> None:
        record = _make_record(tags=["uncategorized"])
        r = mqs.structural_signal(record=record)
        # has_tags fails → 4/5 max (other checks still pass).
        assert r.score == pytest.approx(4 / 5)

    def test_all_checks_missing_scores_zero(self) -> None:
        record = _make_record(
            description="",
            github_url=None,
            homepage_url=None,
            tags=[],
            transports=[],
            language=None,
        )
        r = mqs.structural_signal(record=record)
        assert r.score == pytest.approx(0.0)

    def test_evidence_has_all_five_keys(self) -> None:
        record = _make_record()
        r = mqs.structural_signal(record=record)
        expected_keys = {
            "has_description",
            "has_repo_url",
            "has_tags",
            "has_transports",
            "has_language",
        }
        assert expected_keys == set(r.evidence.keys())

    def test_homepage_url_satisfies_repo_url_check(self) -> None:
        record = _make_record(github_url=None, homepage_url="https://example.com")
        r = mqs.structural_signal(record=record)
        assert r.evidence["has_repo_url"] is True


# ---------------------------------------------------------------------------
# TestGraph
# ---------------------------------------------------------------------------


class TestGraph:
    def test_isolated_node_scores_zero(self) -> None:
        r = mqs.graph_signal(degree=0, cross_type_degree=0)
        assert r.score == pytest.approx(0.0)

    def test_saturated_degree_and_cross_scores_one(self) -> None:
        # degree=20 (saturation) + cross=5 (cross saturation) → 0.7 + 0.3 = 1.0
        r = mqs.graph_signal(degree=20, cross_type_degree=5)
        assert r.score == pytest.approx(1.0)

    def test_isolated_flag_set_when_degree_zero(self) -> None:
        r = mqs.graph_signal(degree=0, cross_type_degree=0)
        assert r.evidence["isolated"] is True

    def test_isolated_flag_false_when_degree_nonzero(self) -> None:
        r = mqs.graph_signal(degree=1, cross_type_degree=0)
        assert r.evidence["isolated"] is False

    def test_monotonic_in_degree(self) -> None:
        scores = [
            mqs.graph_signal(degree=d, cross_type_degree=0).score
            for d in [0, 5, 10, 20]
        ]
        assert scores[0] < scores[1] < scores[2] <= scores[3]

    def test_monotonic_in_cross_type_degree(self) -> None:
        scores = [
            mqs.graph_signal(degree=5, cross_type_degree=c).score
            for c in [0, 1, 3, 5]
        ]
        assert scores[0] < scores[1] < scores[2] <= scores[3]


# ---------------------------------------------------------------------------
# TestTrust
# ---------------------------------------------------------------------------


class TestTrust:
    def test_official_tag_yields_high_score(self) -> None:
        record = _make_record(tags=["official", "utility"], license="MIT", author="x")
        r = mqs.trust_signal(record=record)
        # official=0.5 + license=0.3 + author=0.2 = 1.0
        assert r.score == pytest.approx(1.0)

    def test_org_author_type_yields_high_score(self) -> None:
        record = _make_record(author_type="org", tags=["utility"], license="MIT", author="org")
        r = mqs.trust_signal(record=record)
        # official_or_org=0.5 + license=0.3 + author=0.2 = 1.0
        assert r.score == pytest.approx(1.0)

    def test_license_alone_scores_point_three(self) -> None:
        record = _make_record(
            tags=["utility"],
            author_type="individual",
            license="MIT",
            author=None,
        )
        r = mqs.trust_signal(record=record)
        # only has_license contributes → 0.3
        assert r.score == pytest.approx(0.3)

    def test_anonymous_no_license_scores_zero(self) -> None:
        record = _make_record(
            tags=["utility"],
            author_type="individual",
            license=None,
            author=None,
        )
        r = mqs.trust_signal(record=record)
        assert r.score == pytest.approx(0.0)

    def test_evidence_has_three_keys(self) -> None:
        record = _make_record()
        r = mqs.trust_signal(record=record)
        assert set(r.evidence.keys()) == {"official_or_org", "has_license", "has_author"}


# ---------------------------------------------------------------------------
# TestRuntime
# ---------------------------------------------------------------------------


class TestRuntime:
    def test_all_defaults_neutral(self) -> None:
        r = mqs.runtime_signal()
        assert r.score == pytest.approx(0.5)

    def test_ten_invocations_no_errors_scores_one(self) -> None:
        r = mqs.runtime_signal(invocation_count=10, error_count=0)
        # usage_term=1.0, reliability_term=1.0 → (1+1)/2 = 1.0
        assert r.score == pytest.approx(1.0)

    def test_partial_errors_reduces_score(self) -> None:
        r = mqs.runtime_signal(invocation_count=10, error_count=2)
        # error_rate=0.2, reliability=1-min(1,0.2*5)=0.0, usage=1.0 → 0.5
        assert 0.0 < r.score < 1.0

    def test_errors_exceeding_invocations_clamped_to_zero(self) -> None:
        # error_count > invocation_count should raise ValueError per contract.
        with pytest.raises(ValueError):
            mqs.runtime_signal(invocation_count=5, error_count=6)

    def test_evidence_has_neutral_no_data_flag(self) -> None:
        r = mqs.runtime_signal()
        assert "neutral_no_data" in r.evidence
        assert r.evidence["neutral_no_data"] is True

    def test_evidence_neutral_false_when_invocations_present(self) -> None:
        r = mqs.runtime_signal(invocation_count=1, error_count=0)
        assert r.evidence["neutral_no_data"] is False


# ---------------------------------------------------------------------------
# TestNegativeInputsRaise
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn,kwargs",
    [
        (
            "popularity_signal",
            {"stars": -1},
        ),
        (
            "freshness_signal",
            {"last_commit_age_days": -1.0},
        ),
        (
            "graph_signal",
            {"degree": -1, "cross_type_degree": 0},
        ),
        (
            "runtime_signal",
            {"invocation_count": -1},
        ),
    ],
)
def test_negative_inputs_raise_value_error(fn: str, kwargs: dict[str, Any]) -> None:
    func = getattr(mqs, fn)
    with pytest.raises(ValueError):
        func(**kwargs)
