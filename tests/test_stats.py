"""Statistics tests — control limits, stoppage exclusion, histogram binning."""

from __future__ import annotations

import pytest

from cycletime.stats import D2_N2, histogram, summarize


def rows(values, stoppages=()):
    out = [{"cycle_s": v, "is_stoppage": False} for v in values]
    out += [{"cycle_s": v, "is_stoppage": True} for v in stoppages]
    return out


# ------------------------------------------------------------ control limits

def test_imr_limits_match_hand_calculation():
    values = [10.0, 12.0, 11.0, 13.0, 10.0]
    s = summarize(rows(values))

    assert s["n"] == 5
    assert s["mean"] == pytest.approx(11.2, abs=0.01)

    # Moving ranges: |12-10|, |11-12|, |13-11|, |10-13| = 2, 1, 2, 3 -> mean 2.0
    expected_sigma = 2.0 / D2_N2
    assert s["sigma"] == pytest.approx(expected_sigma, abs=0.001)
    assert s["ucl"] == pytest.approx(11.2 + 3 * expected_sigma, abs=0.01)
    assert s["lcl"] == pytest.approx(11.2 - 3 * expected_sigma, abs=0.01)


def test_lcl_floors_at_zero():
    """Cycle time cannot be negative, so the lower limit must not go below 0."""
    s = summarize(rows([1.0, 9.0, 1.0, 9.0, 1.0]))
    assert s["lcl"] == 0.0


def test_moving_range_resists_a_single_outlier():
    """The reason for I-MR: one spike must not blow the limits wide open.

    A raw standard deviation would absorb the outlier and inflate sigma so far
    that the outlier itself stops registering as out of control.
    """
    steady = [10.0] * 20
    with_spike = steady[:10] + [40.0] + steady[11:]

    s = summarize(rows(with_spike))
    raw_sd = (sum((v - sum(with_spike) / 20) ** 2 for v in with_spike) / 19) ** 0.5

    assert s["sigma"] < raw_sd
    assert s["out_of_control"] >= 1     # the spike is still flagged


def test_out_of_control_counted():
    s = summarize(rows([10.0, 10.1, 9.9, 10.0, 10.2, 25.0]))
    assert s["out_of_control"] == 1


# ---------------------------------------------------------------- stoppages

def test_stoppages_excluded_from_statistics():
    """A stoppage must not drag the mean or widen the limits."""
    clean = summarize(rows([10.0, 11.0, 10.5]))
    with_stop = summarize(rows([10.0, 11.0, 10.5], stoppages=[600.0]))

    assert with_stop["mean"] == clean["mean"]
    assert with_stop["ucl"] == clean["ucl"]
    assert with_stop["n"] == 3
    assert with_stop["stoppages"] == 1


def test_stoppages_excluded_from_histogram():
    h = histogram(rows([10.0, 11.0, 12.0], stoppages=[900.0]))
    assert sum(h["counts"]) == 3
    assert float(h["bins"][-1]) < 100    # the 900 s stoppage set no upper edge


# ------------------------------------------------------------- degenerate

def test_empty_input():
    s = summarize([])
    assert s["n"] == 0
    assert s["mean"] is None
    assert s["ucl"] is None
    assert histogram([])["counts"] == []


def test_single_value_has_no_variation():
    s = summarize(rows([12.0]))
    assert s["n"] == 1
    assert s["sigma"] == 0.0
    assert s["ucl"] == s["lcl"] == 12.0


def test_identical_values_render_one_bar():
    """Equal values would otherwise produce a zero-width bin and divide by 0."""
    h = histogram(rows([12.0] * 5))
    assert h["counts"] == [5]


def test_cv_is_scale_free():
    """Doubling every cycle time leaves the fluctuation percentage unchanged."""
    a = summarize(rows([10.0, 12.0, 11.0, 13.0]))
    b = summarize(rows([20.0, 24.0, 22.0, 26.0]))
    assert a["cv_pct"] == pytest.approx(b["cv_pct"], abs=0.05)


# ------------------------------------------------------------- histogram

def test_histogram_bins_cover_every_value():
    values = [8.0, 9.5, 10.0, 11.2, 12.0, 15.0]
    h = histogram(rows(values), bins=6)
    assert len(h["counts"]) == 6
    assert sum(h["counts"]) == len(values)
    # The maximum lands on the top edge and must fall in the last bin, not
    # overflow into a nonexistent one.
    assert h["counts"][-1] >= 1
