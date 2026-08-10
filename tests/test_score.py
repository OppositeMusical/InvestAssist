"""Scoring engine tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from investassist.score import (
    InsufficientData,
    Regime,
    Verdict,
    assess,
    classify,
    component_series,
    compute_components,
    compute_confidence,
    compute_exits,
    detect_regime,
    score_series,
)
from investassist.spec import COMPONENTS

from .conftest import make_bars


def test_all_components_stay_in_range(spec, uptrend, downtrend, flat):
    for bars in (uptrend, downtrend, flat):
        series = component_series(bars, spec)
        assert list(series.columns) == list(COMPONENTS)
        assert series.notna().all().all()
        assert series.min().min() >= -100.0
        assert series.max().max() <= 100.0


def test_uptrend_scores_positive_downtrend_negative(spec, uptrend, downtrend):
    up = assess(uptrend, spec, "UP")
    down = assess(downtrend, spec, "DOWN")
    assert up.score > 0
    assert down.score < 0
    assert up.verdict.is_bullish
    assert down.verdict.is_bearish


def test_score_is_bounded(spec, uptrend, downtrend):
    for bars in (uptrend, downtrend):
        assert -100.0 <= assess(bars, spec, "X").score <= 100.0


def test_insufficient_history_refuses_to_guess(spec):
    with pytest.raises(InsufficientData):
        assess(make_bars(n=50), spec, "SHORT")


def test_missing_column_is_rejected(spec, uptrend):
    with pytest.raises(ValueError, match="missing column"):
        assess(uptrend.drop(columns=["volume"]), spec, "X")


def test_unsorted_bars_are_rejected(spec, uptrend):
    with pytest.raises(ValueError, match="oldest-first"):
        assess(uptrend.iloc[::-1], spec, "X")


def test_regime_detection(spec, benchmark_up, benchmark_down):
    assert detect_regime(benchmark_up, spec) is Regime.RISK_ON
    assert detect_regime(benchmark_down, spec) is Regime.RISK_OFF


def test_missing_benchmark_costs_conviction_rather_than_granting_it(spec):
    assert detect_regime(None, spec) is Regime.NEUTRAL
    assert detect_regime(make_bars(n=10), spec) is Regime.NEUTRAL


def test_risk_off_damps_but_does_not_flip(spec, uptrend, benchmark_down, benchmark_up):
    risk_on = assess(uptrend, spec, "X", benchmark=benchmark_up)
    risk_off = assess(uptrend, spec, "X", benchmark=benchmark_down)

    assert risk_off.regime is Regime.RISK_OFF
    assert abs(risk_off.score) < abs(risk_on.score)
    # Damping must reduce conviction, never reverse the call.
    assert np.sign(risk_off.score) == np.sign(risk_on.score)


def test_assess_agrees_with_the_vectorised_series(spec, uptrend, benchmark_up):
    """The panel path and the backtest path must produce the same number."""
    single = assess(uptrend, spec, "X", benchmark=benchmark_up).score
    series = score_series(uptrend, spec, benchmark_up).iloc[-1]
    assert single == pytest.approx(series, abs=0.01)


def test_compute_components_is_the_last_row_of_the_series(spec, uptrend):
    scalar = compute_components(uptrend, spec)
    last_row = component_series(uptrend, spec).iloc[-1]
    for name in COMPONENTS:
        assert scalar[name] == pytest.approx(last_row[name])


def test_classify_thresholds(spec):
    assert classify(100, spec) is Verdict.STRONG_BUY
    assert classify(spec.verdicts.strong_buy, spec) is Verdict.STRONG_BUY
    assert classify(spec.verdicts.buy, spec) is Verdict.BUY
    assert classify(0, spec) is Verdict.HOLD
    assert classify(spec.verdicts.trim, spec) is Verdict.TRIM
    assert classify(spec.verdicts.sell, spec) is Verdict.SELL
    assert classify(-100, spec) is Verdict.SELL


def test_confidence_rewards_agreement(spec):
    agreeing = {name: 80.0 for name in COMPONENTS}
    conflicted = {name: (80.0 if i % 2 else -80.0) for i, name in enumerate(COMPONENTS)}
    assert compute_confidence(agreeing, 80.0, 2.0, spec) > compute_confidence(
        conflicted, 10.0, 2.0, spec
    )


def test_confidence_penalises_volatility(spec):
    components = {name: 80.0 for name in COMPONENTS}
    calm = compute_confidence(components, 80.0, 1.0, spec)
    wild = compute_confidence(components, 80.0, spec.confidence.high_atr_pct * 2, spec)
    assert calm > wild
    assert 0.0 <= wild <= 100.0


def test_exit_levels_are_ordered(spec, uptrend):
    atr = 2.5
    entry = 100.0
    exits = compute_exits(uptrend, entry, atr, spec)

    assert exits.initial_stop == pytest.approx(entry - spec.exits.initial_stop_atr * atr)
    assert exits.risk_per_share > 0
    assert exits.targets[0] < exits.targets[1]
    assert exits.r_multiple(entry + exits.risk_per_share) == pytest.approx(1.0)
    assert exits.r_multiple(exits.initial_stop) == pytest.approx(-1.0)


def test_trailing_stop_never_sits_below_the_initial_stop(spec, uptrend):
    exits = compute_exits(uptrend, float(uptrend["close"].iloc[-1]) * 0.5, 1.0, spec)
    assert exits.trail_active
    assert exits.trail_stop >= exits.initial_stop


def test_rationale_is_generated_from_the_numbers(spec, uptrend):
    assessment = assess(uptrend, spec, "UP")
    assert assessment.rationale
    # The strongest contributor must be mentioned first.
    strongest = max(assessment.contributions.items(), key=lambda kv: abs(kv[1]))
    assert f"{strongest[1]:+.0f}" in assessment.rationale[0]


def test_flat_market_lands_on_hold(spec):
    """A series with no information should not produce a strong opinion."""
    n = 400
    index = pd.bdate_range("2021-01-04", periods=n)
    constant = pd.DataFrame(
        {"open": 50.0, "high": 50.0, "low": 50.0, "close": 50.0, "volume": 1e6},
        index=index,
    )
    assessment = assess(constant, spec, "FLAT")
    assert assessment.verdict is Verdict.HOLD
    assert abs(assessment.score) < spec.verdicts.buy
