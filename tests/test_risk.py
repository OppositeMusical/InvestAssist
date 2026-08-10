"""Position sizing and portfolio limit tests."""

from __future__ import annotations

import pytest

from investassist.risk import Position, size_position
from investassist.score import ExitLevels


def levels(entry: float = 100.0, stop: float = 96.0) -> ExitLevels:
    return ExitLevels(
        entry=entry,
        initial_stop=stop,
        risk_per_share=entry - stop,
        trail_stop=stop,
        trail_active=False,
        targets=(entry + (entry - stop), entry + 2 * (entry - stop)),
        time_stop_bars=40,
    )


def test_fixed_fractional_sizing(spec):
    # 0.5% of 100k is $500 of risk; $4/share of stop distance buys 125 shares.
    decision = size_position(100_000, levels(), spec)
    assert decision.shares == 125
    assert decision.risk_dollars == pytest.approx(500.0)
    assert decision.approved


def test_concentration_cap_binds_on_a_tight_stop(spec):
    # A 10 cent stop would otherwise justify 5000 shares of a $100 stock.
    decision = size_position(100_000, levels(stop=99.9), spec)
    assert decision.shares == 200  # 20% of 100k / $100
    assert "max position size" in decision.limited_by


def test_open_position_limit_refuses(spec):
    positions = [
        Position(f"S{i}", 10, 100.0, 96.0) for i in range(spec.risk.max_open_positions)
    ]
    decision = size_position(100_000, levels(), spec, open_positions=positions)
    assert not decision.approved
    assert "max open positions" in decision.limited_by


def test_daily_loss_limit_halts_new_entries(spec):
    decision = size_position(
        100_000, levels(), spec, realised_r_today=-spec.risk.daily_loss_limit_r
    )
    assert not decision.approved
    assert "daily loss limit" in decision.limited_by


def test_portfolio_heat_caps_the_next_position(spec):
    # Each position risks 1R ($500 at 100k with risk_pct 0.5%).
    risk_unit = 100_000 * spec.risk.risk_pct
    positions = [
        Position(f"S{i}", int(risk_unit / 4.0), 100.0, 96.0)
        for i in range(int(spec.risk.max_portfolio_heat_r))
    ]
    decision = size_position(100_000, levels(), spec, open_positions=positions)
    assert not decision.approved
    assert "portfolio heat" in decision.limited_by


def test_position_past_its_stop_contributes_no_heat():
    """A stop above entry means the trade is risk-free and should free up heat."""
    assert Position("X", 100, 100.0, 105.0).open_risk == 0.0
    assert Position("X", 100, 100.0, 96.0).open_risk == pytest.approx(400.0)


def test_inverted_stop_is_refused(spec):
    bad = ExitLevels(100.0, 105.0, -5.0, 105.0, False, (110.0,), 40)
    decision = size_position(100_000, bad, spec)
    assert not decision.approved
    assert "stop is not below entry" in decision.limited_by


def test_no_equity_means_no_position(spec):
    assert not size_position(0, levels(), spec).approved
