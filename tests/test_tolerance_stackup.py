"""Tests for aria_os.tolerance_stackup."""

import math

import pytest

from aria_os.tolerance_stackup import worst_case, statistical, compare


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_STACK = [
    {"nominal": 10.0, "plus": 0.05, "minus": 0.05},
    {"nominal": 20.0, "plus": 0.05, "minus": 0.05},
    {"nominal": 30.0, "plus": 0.05, "minus": 0.05},
]

ASYMMETRIC_STACK = [
    {"nominal": 10.0, "plus": 0.10, "minus": 0.02},
    {"nominal": 20.0, "plus": 0.05, "minus": 0.15},
    {"nominal": 15.0, "plus": 0.03, "minus": 0.03},
]


# ---------------------------------------------------------------------------
# Worst-case
# ---------------------------------------------------------------------------


class TestWorstCase:

    def test_symmetric_stack_sums_nominals(self):
        r = worst_case(SIMPLE_STACK)
        assert r["nominal_total"] == pytest.approx(60.0)

    def test_symmetric_stack_envelope(self):
        r = worst_case(SIMPLE_STACK)
        assert r["worst_case_max"] == pytest.approx(60.15)
        assert r["worst_case_min"] == pytest.approx(59.85)
        assert r["total_range"] == pytest.approx(0.30)

    def test_asymmetric_stack(self):
        r = worst_case(ASYMMETRIC_STACK)
        # nominal = 45, plus = 0.18, minus = 0.20
        assert r["nominal_total"] == pytest.approx(45.0)
        assert r["worst_case_max"] == pytest.approx(45.18)
        assert r["worst_case_min"] == pytest.approx(44.80)

    def test_empty_stack(self):
        r = worst_case([])
        assert r["nominal_total"] == 0.0
        assert r["worst_case_max"] == 0.0
        assert r["worst_case_min"] == 0.0
        assert r["n_contributors"] == 0

    def test_single_contributor(self):
        r = worst_case([{"nominal": 50.0, "plus": 0.1, "minus": 0.1}])
        assert r["worst_case_max"] == pytest.approx(50.1)
        assert r["worst_case_min"] == pytest.approx(49.9)

    def test_signed_minus_is_normalised(self):
        # User passes a negative minus — we should treat magnitude.
        r = worst_case([{"nominal": 10.0, "plus": 0.05, "minus": -0.05}])
        assert r["worst_case_min"] == pytest.approx(9.95)

    def test_missing_key_raises(self):
        with pytest.raises(ValueError, match="missing required key"):
            worst_case([{"nominal": 10.0, "plus": 0.05}])

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            worst_case([{"nominal": "ten", "plus": 0.05, "minus": 0.05}])

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="not a dict"):
            worst_case(["not a dict"])  # type: ignore


# ---------------------------------------------------------------------------
# Statistical (RSS)
# ---------------------------------------------------------------------------


class TestStatistical:

    def test_sigma3_less_than_worst_case(self):
        wc = worst_case(SIMPLE_STACK)
        st = statistical(SIMPLE_STACK)
        # RSS is always tighter than worst-case for n > 1
        assert st["statistical_sigma_3"] < (wc["total_plus"] + wc["total_minus"]) / 2

    def test_sigma3_formula(self):
        # For 3 contributors each with plus=minus=0.05:
        #  per-contributor sigma = 0.05 / 3
        #  total sigma = sqrt(3) * (0.05/3) = 0.05/sqrt(3)
        #  3*sigma    = 0.05 * sqrt(3) ~= 0.0866
        st = statistical(SIMPLE_STACK)
        expected = 0.05 * math.sqrt(3)
        assert st["statistical_sigma_3"] == pytest.approx(expected, rel=1e-4)

    def test_nominal_total_preserved(self):
        st = statistical(SIMPLE_STACK)
        assert st["nominal_total"] == pytest.approx(60.0)

    def test_empty_stack_zero_sigma(self):
        st = statistical([])
        assert st["statistical_sigma_3"] == 0.0
        assert st["nominal_total"] == 0.0

    def test_single_contributor_rss_equals_half_range(self):
        # RSS of a single ±0.1 contributor: sigma = 0.1/3, 3*sigma = 0.1
        st = statistical([{"nominal": 20.0, "plus": 0.1, "minus": 0.1}])
        assert st["statistical_sigma_3"] == pytest.approx(0.1, rel=1e-6)

    def test_asymmetric_stack_biased_centre(self):
        st = statistical(ASYMMETRIC_STACK)
        # centre_shift = (0.10-0.02)/2 + (0.05-0.15)/2 + (0.03-0.03)/2 = 0.04 - 0.05 + 0 = -0.01
        assert st["stat_centre"] == pytest.approx(44.99, abs=1e-6)


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------


class TestCompare:

    def test_compare_returns_both(self):
        r = compare(SIMPLE_STACK)
        assert "worst_case" in r
        assert "statistical" in r
        assert r["range_saved"] > 0  # RSS always saves range for n > 1
        assert 0 <= r["percent_saved"] <= 100

    def test_compare_single_contributor_saves_nothing(self):
        r = compare([{"nominal": 10.0, "plus": 0.1, "minus": 0.1}])
        # Single contributor: RSS == worst-case, no saving.
        assert r["range_saved"] == pytest.approx(0.0, abs=1e-6)
