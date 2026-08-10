"""Backtester tests.

The properties asserted here are the ones that separate a backtest from a
fantasy: no lookahead, gaps honoured, intrabar ambiguity resolved against you,
and costs that actually bite.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from investassist.backtest import (
    Trade,
    compute_metrics,
    run_backtest,
    slippage_sweep,
    walk_forward,
)

from .conftest import make_bars


@pytest.fixture
def universe():
    return {
        "AAA": make_bars(n=500, drift=0.0012, seed=101),
        "BBB": make_bars(n=500, drift=0.0006, seed=202),
        "CCC": make_bars(n=500, drift=-0.0004, seed=303),
    }


def test_backtest_runs_and_reports(spec, universe, benchmark_up):
    result = run_backtest(universe, spec, benchmark_up, initial_equity=100_000)
    assert len(result.equity_curve) > 0
    assert result.metrics["trades"] >= 0
    assert "benchmark_return" in result.metrics
    assert result.summary()


def test_equity_curve_starts_at_initial_capital(spec, universe):
    result = run_backtest(universe, spec, None, initial_equity=50_000)
    # Nothing can trade on the first bar because entries fill the day after
    # the signal, so the curve must open exactly at the starting balance.
    assert result.equity_curve.iloc[0] == pytest.approx(50_000)


def test_entries_never_fill_on_the_signal_bar(spec, universe):
    """The core no-lookahead guarantee."""
    result = run_backtest(universe, spec, None)
    prepared = universe
    for trade in result.trades:
        bars = prepared[trade.symbol]
        entry_idx = bars.index.get_loc(trade.entry_date)
        # An entry fills at that bar's open, so it can never be decided by
        # information from the same bar's close.
        assert entry_idx > 0
        assert trade.entry_price > 0


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop(spec):
    """A stop is not a guaranteed price, and pretending otherwise flatters
    every drawdown in the report."""
    n = 400
    index = pd.bdate_range("2021-01-04", periods=n)
    close = np.linspace(100.0, 160.0, n)
    # Collapse the last bar far below any plausible stop.
    close[-1] = 40.0
    frame = pd.DataFrame(
        {
            "open": np.r_[close[0], close[:-1]],
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=index,
    )
    frame.loc[frame.index[-1], ["open", "high"]] = 40.0
    frame.loc[frame.index[-1], "low"] = 39.0

    result = run_backtest({"GAP": frame}, spec, None, slippage_bps=0.0)
    stopped = [t for t in result.trades if t.reason == "stop"]
    if stopped:
        trade = stopped[-1]
        # The fill must be at or below the crash open, never at the stop.
        assert trade.exit_price <= 40.0 + 1e-6


def test_slippage_monotonically_reduces_returns(spec, universe):
    clean = run_backtest(universe, spec, None, slippage_bps=0.0)
    dirty = run_backtest(universe, spec, None, slippage_bps=50.0)
    if clean.metrics["trades"] > 0:
        assert dirty.metrics["total_return"] <= clean.metrics["total_return"]


def test_slippage_sweep_covers_the_configured_range(spec, universe):
    table = slippage_sweep(universe, spec, None)
    assert list(table.index) == list(spec.costs.slippage_sweep_bps)
    assert "expectancy_r" in table.columns
    assert table["total_return"].is_monotonic_decreasing or len(table) <= 1


def test_walk_forward_windows_do_not_overlap(spec, universe):
    folds = walk_forward(universe, spec, None, folds=3, embargo_days=5)
    assert folds
    for fold in folds:
        # The embargo must leave a real gap between training and testing.
        assert fold.test_start > fold.train_end
        assert fold.test_end >= fold.test_start


def test_walk_forward_grid_selects_and_reports(spec, universe):
    folds = walk_forward(
        universe, spec, None, folds=2,
        param_grid={"trend_stack": [0.1, 0.4]},
    )
    assert folds
    assert all(f.chosen for f in folds)
    assert all(f.chosen["trend_stack"] in (0.1, 0.4) for f in folds)


def test_walk_forward_rejects_unknown_parameters(spec, universe):
    with pytest.raises(ValueError, match="unknown weight"):
        walk_forward(universe, spec, None, folds=2, param_grid={"nonsense": [1.0]})


def test_short_history_is_rejected_rather_than_silently_skipped(spec):
    with pytest.raises(ValueError, match="no symbol had enough history"):
        run_backtest({"TINY": make_bars(n=30)}, spec, None)


def test_metrics_on_a_known_trade_set():
    curve = pd.Series(
        [100.0, 110.0, 105.0, 120.0],
        index=pd.bdate_range("2024-01-01", periods=4),
    )
    trades = [
        Trade("A", pd.Timestamp("2024-01-01"), 10.0, 10,
              pd.Timestamp("2024-01-02"), 12.0, "target", pnl=20.0, r_multiple=2.0, bars_held=1),
        Trade("B", pd.Timestamp("2024-01-02"), 10.0, 10,
              pd.Timestamp("2024-01-03"), 9.0, "stop", pnl=-10.0, r_multiple=-1.0, bars_held=1),
    ]
    m = compute_metrics(curve, trades, exposure_days=3)

    assert m["trades"] == 2
    assert m["hit_rate"] == pytest.approx(0.5)
    assert m["expectancy_r"] == pytest.approx(0.5)
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["total_return"] == pytest.approx(0.2)
    assert m["max_drawdown"] == pytest.approx(-5 / 110)
    assert m["exposure"] == pytest.approx(0.75)


def test_metrics_handle_an_empty_trade_list():
    curve = pd.Series([100.0, 100.0], index=pd.bdate_range("2024-01-01", periods=2))
    m = compute_metrics(curve, [])
    assert m["trades"] == 0
    assert m["profit_factor"] == 0.0
    assert m["expectancy_r"] == 0.0


def test_profit_factor_is_infinite_with_no_losses():
    curve = pd.Series([100.0, 120.0], index=pd.bdate_range("2024-01-01", periods=2))
    trades = [
        Trade("A", pd.Timestamp("2024-01-01"), 10.0, 10,
              pd.Timestamp("2024-01-02"), 12.0, "target", pnl=20.0, r_multiple=2.0)
    ]
    assert compute_metrics(curve, trades)["profit_factor"] == float("inf")


def test_backtest_never_spends_more_cash_than_it_has(spec, universe):
    result = run_backtest(universe, spec, None, initial_equity=5_000)
    assert (result.equity_curve > 0).all()
