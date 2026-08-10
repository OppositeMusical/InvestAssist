"""Position sizing and portfolio-level limits.

None of this exists in the thinkScript implementation — the language is
bar-scoped and per-symbol and cannot see account equity, open risk, or your
day-trade count. This module is the main reason the external engine is worth
building at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .score import ExitLevels
from .spec import Spec


@dataclass(frozen=True)
class Position:
    symbol: str
    shares: int
    entry: float
    stop: float

    @property
    def open_risk(self) -> float:
        """Dollars still at risk if the stop is hit. Never negative: once the
        stop is above entry the position is risk-free and contributes nothing
        to portfolio heat."""
        return max(0.0, (self.entry - self.stop) * self.shares)


@dataclass(frozen=True)
class SizeDecision:
    shares: int
    dollars: float
    risk_dollars: float
    limited_by: str | None

    @property
    def approved(self) -> bool:
        return self.shares > 0


def size_position(
    equity: float,
    exits: ExitLevels,
    spec: Spec,
    open_positions: list[Position] | None = None,
    realised_r_today: float = 0.0,
) -> SizeDecision:
    """Fixed-fractional sizing, then clamp against every portfolio limit.

    Returns the binding constraint in ``limited_by`` so the panel can say why
    a position came back smaller than expected — or why it was refused.
    """
    open_positions = open_positions or []
    r = spec.risk

    if equity <= 0:
        return SizeDecision(0, 0.0, 0.0, "no equity")

    # Hard blocks first. These are refusals, not reductions.
    if realised_r_today <= -abs(r.daily_loss_limit_r):
        return SizeDecision(0, 0.0, 0.0, f"daily loss limit ({r.daily_loss_limit_r}R) hit")
    if len(open_positions) >= r.max_open_positions:
        return SizeDecision(0, 0.0, 0.0, f"max open positions ({r.max_open_positions})")
    if exits.risk_per_share <= 0:
        return SizeDecision(0, 0.0, 0.0, "stop is not below entry")

    limited_by: str | None = None

    risk_budget = equity * r.risk_pct
    shares = math.floor(risk_budget / exits.risk_per_share)

    # Concentration cap.
    max_shares_by_notional = math.floor((equity * r.max_position_pct) / exits.entry)
    if max_shares_by_notional < shares:
        shares = max_shares_by_notional
        limited_by = f"max position size ({r.max_position_pct:.0%} of equity)"

    # Portfolio heat: total open risk across all names, expressed in R where
    # 1R is one full risk unit.
    current_heat_r = sum(p.open_risk for p in open_positions) / risk_budget if risk_budget > 0 else 0.0
    remaining_r = r.max_portfolio_heat_r - current_heat_r
    if remaining_r <= 0:
        return SizeDecision(0, 0.0, 0.0, f"portfolio heat limit ({r.max_portfolio_heat_r}R)")
    max_shares_by_heat = math.floor((remaining_r * risk_budget) / exits.risk_per_share)
    if max_shares_by_heat < shares:
        shares = max_shares_by_heat
        limited_by = f"portfolio heat limit ({r.max_portfolio_heat_r}R)"

    shares = max(0, shares)
    if shares == 0 and limited_by is None:
        limited_by = "risk budget too small for this stop distance"

    return SizeDecision(
        shares=shares,
        dollars=round(shares * exits.entry, 2),
        risk_dollars=round(shares * exits.risk_per_share, 2),
        limited_by=limited_by,
    )
