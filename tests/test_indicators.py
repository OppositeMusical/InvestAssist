"""Indicator tests.

These are the foundation of parity with thinkScript, so they check the exact
smoothing conventions rather than just "looks about right". Getting Wilders vs
EMA wrong is invisible on a chart and shifts every downstream score.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from investassist import indicators as ind

from .conftest import make_bars


@pytest.fixture
def bars():
    return make_bars(n=300, seed=5)


def test_sma_matches_hand_calculation():
    series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = ind.sma(series, 3)
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx(2.0)
    assert result.iloc[4] == pytest.approx(4.0)


def test_ema_seeds_from_first_bar_not_an_sma():
    """thinkScript's ExpAverage is recursive from bar one — adjust=False."""
    series = pd.Series([10.0, 20.0, 30.0])
    result = ind.ema(series, 2)
    alpha = 2 / (2 + 1)
    expected_1 = 10.0 + alpha * (20.0 - 10.0)
    assert result.iloc[0] == pytest.approx(10.0)
    assert result.iloc[1] == pytest.approx(expected_1)


def test_wilders_uses_one_over_n_not_two_over_n_plus_one():
    series = pd.Series([10.0, 20.0, 30.0])
    result = ind.wilders(series, 2)
    expected_1 = 10.0 + 0.5 * (20.0 - 10.0)
    assert result.iloc[1] == pytest.approx(expected_1)
    # And it is emphatically not the EMA of the same length.
    assert result.iloc[1] != pytest.approx(ind.ema(series, 2).iloc[1])


def test_stdev_is_population_not_sample():
    series = pd.Series([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    assert ind.stdev(series, 8).iloc[-1] == pytest.approx(2.0)


def test_true_range_uses_previous_close(bars):
    tr = ind.true_range(bars["high"], bars["low"], bars["close"])
    assert (tr >= 0).all()
    # Bar 1 spans the gap from bar 0's close, so it must be at least the raw range.
    raw_range = bars["high"].iloc[1] - bars["low"].iloc[1]
    assert tr.iloc[1] >= raw_range - 1e-9


def test_atr_is_positive_and_finite(bars):
    atr = ind.atr(bars["high"], bars["low"], bars["close"], 14)
    assert atr.notna().all()
    assert (atr > 0).all()


def test_rsi_stays_in_bounds(bars):
    rsi = ind.rsi(bars["close"], 14)
    assert rsi.min() >= 0.0
    assert rsi.max() <= 100.0


def test_rsi_saturates_on_a_monotonic_series():
    rising = pd.Series(np.arange(1, 60, dtype=float))
    assert ind.rsi(rising, 14).iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(rising[::-1].reset_index(drop=True), 14).iloc[-1] == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_neutral():
    flat = pd.Series([50.0] * 40)
    assert ind.rsi(flat, 14).iloc[-1] == pytest.approx(50.0)


def test_macd_histogram_is_value_minus_signal(bars):
    value, avg, hist = ind.macd(bars["close"], 12, 26, 9)
    pd.testing.assert_series_equal(hist, value - avg, check_names=False)


def test_percent_b_bounds_and_midpoint(bars):
    pct_b = ind.bollinger_percent_b(bars["close"], 20, 2.0)
    assert pct_b.notna().all()
    # A dead-flat window has a zero-width band; we report the midpoint.
    flat = pd.Series([25.0] * 40)
    assert ind.bollinger_percent_b(flat, 20, 2.0).iloc[-1] == pytest.approx(0.5)


def test_adx_in_range_and_higher_when_trending():
    trending = make_bars(n=300, drift=0.004, volatility=0.004, seed=3)
    choppy = make_bars(n=300, drift=0.0, volatility=0.004, seed=3)

    adx_trend = ind.adx(trending["high"], trending["low"], trending["close"], 14)
    adx_chop = ind.adx(choppy["high"], choppy["low"], choppy["close"], 14)

    assert adx_trend.min() >= 0.0 and adx_trend.max() <= 100.0
    assert adx_trend.iloc[-1] > adx_chop.iloc[-1]


def test_highest_lowest(bars):
    assert ind.highest(bars["high"], 20).iloc[-1] == bars["high"].iloc[-20:].max()
    assert ind.lowest(bars["low"], 20).iloc[-1] == bars["low"].iloc[-20:].min()


def test_indicators_do_not_look_ahead(bars):
    """Truncating the series must not change earlier indicator values.

    The single most important property here: if an indicator's value at bar t
    changes when you append bar t+1, every backtest built on it is fiction.
    """
    full = ind.atr(bars["high"], bars["low"], bars["close"], 14)
    cut = bars.iloc[:-20]
    partial = ind.atr(cut["high"], cut["low"], cut["close"], 14)
    pd.testing.assert_series_equal(full.iloc[: len(partial)], partial)

    full_rsi = ind.rsi(bars["close"], 14)
    partial_rsi = ind.rsi(cut["close"], 14)
    pd.testing.assert_series_equal(full_rsi.iloc[: len(partial_rsi)], partial_rsi)
