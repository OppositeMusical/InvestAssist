"""Event-driven backtester and validation harness.

This is the most valuable module in the repo. The signal is a hypothesis; this
is the thing that tells you whether the hypothesis survives costs. If it does
not, the correct output of the whole project is "don't trade this", and that is
a genuinely useful answer.

Design rules, each of which exists because violating it produces a backtest
that looks wonderful and loses money:

* **No lookahead.** The score for bar *t* uses data through bar *t* only, and
  the resulting order fills at bar *t+1*'s open. You never trade on a close you
  could not have known at the time.
* **Costs are explicit and swept.** Slippage is the assumption most likely to
  be wrong, so :func:`slippage_sweep` reports edge across a range of it rather
  than trusting one number.
* **Intrabar ambiguity resolves against you.** When a bar's range contains both
  the stop and a target, the stop is taken. Daily bars cannot tell you which
  came first, and the optimistic assumption is how backtests lie.
* **Gaps are honoured.** A stop fills at the open when the market gaps through
  it, not at the stop price.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from . import indicators as ind
from .score import classify, score_series
from .spec import Spec

log = logging.getLogger(__name__)

TRADING_DAYS = 252


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: int
    exit_date: pd.Timestamp | None = None
    exit_price: float | None = None
    reason: str = ""
    pnl: float = 0.0
    r_multiple: float = 0.0
    risk_per_share: float = 0.0
    bars_held: int = 0

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


@dataclass
class _OpenPosition:
    symbol: str
    shares: int
    entry: float
    entry_date: pd.Timestamp
    stop: float
    risk_per_share: float
    targets: list[float]
    bars_held: int = 0
    scaled_out: int = 0
    realised: float = 0.0
    initial_shares: int = 0


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    benchmark_curve: pd.Series | None
    metrics: dict[str, float]
    spec_name: str = ""
    slippage_bps: float = 0.0

    def summary(self) -> str:
        m = self.metrics
        rows = [
            f"trades            {int(m['trades'])}",
            f"hit rate          {m['hit_rate']:.1%}",
            f"expectancy        {m['expectancy_r']:+.3f} R",
            f"profit factor     {m['profit_factor']:.2f}",
            f"total return      {m['total_return']:+.1%}",
            f"CAGR              {m['cagr']:+.1%}",
            f"max drawdown      {m['max_drawdown']:.1%}",
            f"Sharpe            {m['sharpe']:.2f}",
            f"exposure          {m['exposure']:.1%}",
            f"avg bars held     {m['avg_bars_held']:.1f}",
        ]
        if "benchmark_return" in m:
            rows.append(f"benchmark return  {m['benchmark_return']:+.1%}")
            rows.append(f"excess vs bench   {m['total_return'] - m['benchmark_return']:+.1%}")
        return "\n".join(rows)


def _apply_slippage(price: float, bps: float, side: str) -> float:
    """Buys fill worse (higher), sells fill worse (lower)."""
    factor = bps / 10_000.0
    return price * (1.0 + factor) if side == "buy" else price * (1.0 - factor)


def _commission(shares: int, spec: Spec) -> float:
    c = spec.costs
    return max(c.commission_min, shares * c.commission_per_share) if shares else 0.0


def run_backtest(
    bars_by_symbol: Mapping[str, pd.DataFrame],
    spec: Spec,
    benchmark: pd.DataFrame | None = None,
    initial_equity: float = 100_000.0,
    slippage_bps: float | None = None,
    start: date | None = None,
    end: date | None = None,
) -> BacktestResult:
    """Run the strategy over ``bars_by_symbol`` and report what happened."""
    slippage = spec.costs.slippage_bps if slippage_bps is None else slippage_bps

    # Precompute per symbol. Scoring is vectorised, so this is one pass each
    # rather than a recomputation per bar.
    prepared: dict[str, pd.DataFrame] = {}
    for symbol, bars in bars_by_symbol.items():
        if len(bars) < spec.features.min_bars + 2:
            log.warning("skipping %s: only %d bars", symbol, len(bars))
            continue
        frame = bars.copy()
        frame["score"] = score_series(bars, spec, benchmark)
        frame["atr"] = ind.atr(bars["high"], bars["low"], bars["close"], spec.features.atr_length)
        frame["chandelier_high"] = ind.highest(bars["high"], spec.exits.chandelier_lookback)
        # Nothing before min_bars is tradeable; the score is not yet meaningful.
        frame.iloc[: spec.features.min_bars, frame.columns.get_loc("score")] = np.nan
        prepared[symbol] = frame

    if not prepared:
        raise ValueError("no symbol had enough history to backtest")

    calendar = sorted(set().union(*(set(f.index) for f in prepared.values())))
    if start:
        calendar = [d for d in calendar if d.date() >= start]
    if end:
        calendar = [d for d in calendar if d.date() <= end]
    if len(calendar) < 2:
        raise ValueError("backtest window is too short")

    equity = initial_equity
    cash = initial_equity
    open_positions: dict[str, _OpenPosition] = {}
    trades: list[Trade] = []
    pending_entries: list[str] = []
    pending_exits: list[tuple[str, str]] = []
    equity_points: list[float] = []
    exposure_days = 0

    verdict_buy = spec.verdicts.buy

    for i, today in enumerate(calendar):
        # --- 1. Execute orders decided on the previous bar, at today's open.
        for symbol, reason in pending_exits:
            position = open_positions.get(symbol)
            frame = prepared.get(symbol)
            if position is None or frame is None or today not in frame.index:
                continue
            fill = _apply_slippage(float(frame.at[today, "open"]), slippage, "sell")
            cash += fill * position.shares - _commission(position.shares, spec)
            trades.append(_close(position, today, fill, reason))
            del open_positions[symbol]
        pending_exits = []

        for symbol in pending_entries:
            frame = prepared.get(symbol)
            if frame is None or today not in frame.index or symbol in open_positions:
                continue
            row = frame.loc[today]
            fill = _apply_slippage(float(row["open"]), slippage, "buy")
            atr_val = float(frame["atr"].asof(today))
            if not np.isfinite(atr_val) or atr_val <= 0:
                continue
            stop = fill - spec.exits.initial_stop_atr * atr_val
            risk_per_share = fill - stop
            if risk_per_share <= 0:
                continue

            shares = int((equity * spec.risk.risk_pct) // risk_per_share)
            shares = min(shares, int((equity * spec.risk.max_position_pct) // fill))
            cost = shares * fill + _commission(shares, spec)
            if shares <= 0 or cost > cash:
                continue

            cash -= cost
            open_positions[symbol] = _OpenPosition(
                symbol=symbol,
                shares=shares,
                initial_shares=shares,
                entry=fill,
                entry_date=today,
                stop=stop,
                risk_per_share=risk_per_share,
                targets=[fill + r * risk_per_share for r in spec.exits.targets_r],
            )
        pending_entries = []

        # --- 2. Walk today's bar for stops and targets on what we hold.
        for symbol in list(open_positions):
            position = open_positions[symbol]
            frame = prepared[symbol]
            if today not in frame.index:
                continue
            row = frame.loc[today]
            position.bars_held += 1

            low, high, open_px = float(row["low"]), float(row["high"]), float(row["open"])

            # Stop first: with daily bars we cannot know the intrabar order, so
            # we resolve the ambiguity against ourselves.
            if low <= position.stop:
                # A gap through the stop fills at the open, not the stop.
                raw = min(open_px, position.stop)
                fill = _apply_slippage(raw, slippage, "sell")
                cash += fill * position.shares - _commission(position.shares, spec)
                trades.append(_close(position, today, fill, "stop"))
                del open_positions[symbol]
                continue

            # Scale out at each target, half the remaining position each time.
            while (
                position.scaled_out < len(position.targets)
                and high >= position.targets[position.scaled_out]
                and position.shares > 1
            ):
                target = position.targets[position.scaled_out]
                sell = position.shares // 2
                fill = _apply_slippage(max(target, open_px), slippage, "sell")
                cash += fill * sell - _commission(sell, spec)
                position.realised += (fill - position.entry) * sell
                position.shares -= sell
                position.scaled_out += 1
                # Once the first target is banked, protect the rest at cost.
                position.stop = max(position.stop, position.entry)

            # Trailing stop, once the trade has earned it.
            if (float(row["close"]) - position.entry) / position.risk_per_share >= spec.exits.trail_activate_r:
                chandelier = float(row["chandelier_high"]) - spec.exits.chandelier_atr * float(row["atr"])
                position.stop = max(position.stop, chandelier)

        # --- 3. Decide on today's close, act at tomorrow's open.
        if i + 1 < len(calendar):
            for symbol, frame in prepared.items():
                if today not in frame.index:
                    continue
                score = float(frame.at[today, "score"]) if pd.notna(frame.at[today, "score"]) else None
                if score is None:
                    continue
                position = open_positions.get(symbol)
                if position is not None:
                    if score <= spec.exits.invalidate_below:
                        pending_exits.append((symbol, "invalidated"))
                    elif position.bars_held >= spec.exits.time_stop_bars:
                        pending_exits.append((symbol, "time stop"))
                elif (
                    score >= verdict_buy
                    and len(open_positions) + len(pending_entries) < spec.risk.max_open_positions
                ):
                    pending_entries.append(symbol)

        # --- 4. Mark to market.
        holdings = 0.0
        for symbol, position in open_positions.items():
            frame = prepared[symbol]
            price = frame["close"].asof(today)
            if pd.notna(price):
                holdings += float(price) * position.shares
        equity = cash + holdings
        equity_points.append(equity)
        if open_positions:
            exposure_days += 1

    # Close anything still open at the final bar, so the metrics are complete.
    final = calendar[-1]
    for symbol, position in list(open_positions.items()):
        frame = prepared[symbol]
        price = float(frame["close"].asof(final))
        trades.append(_close(position, final, price, "end of test"))

    curve = pd.Series(equity_points, index=pd.DatetimeIndex(calendar), name="equity")
    bench_curve = _benchmark_curve(benchmark, calendar, initial_equity)

    return BacktestResult(
        trades=trades,
        equity_curve=curve,
        benchmark_curve=bench_curve,
        metrics=compute_metrics(curve, trades, exposure_days, bench_curve),
        spec_name=spec.name,
        slippage_bps=slippage,
    )


def _close(position: _OpenPosition, when: pd.Timestamp, price: float, reason: str) -> Trade:
    pnl = position.realised + (price - position.entry) * position.shares
    risk_total = position.risk_per_share * position.initial_shares
    return Trade(
        symbol=position.symbol,
        entry_date=position.entry_date,
        entry_price=position.entry,
        shares=position.initial_shares,
        exit_date=when,
        exit_price=price,
        reason=reason,
        pnl=pnl,
        r_multiple=pnl / risk_total if risk_total > 0 else 0.0,
        risk_per_share=position.risk_per_share,
        bars_held=position.bars_held,
    )


def _benchmark_curve(
    benchmark: pd.DataFrame | None, calendar: Sequence[pd.Timestamp], initial: float
) -> pd.Series | None:
    if benchmark is None or benchmark.empty:
        return None
    closes = benchmark["close"].reindex(
        pd.DatetimeIndex(calendar).union(benchmark.index)
    ).ffill().reindex(pd.DatetimeIndex(calendar))
    if closes.isna().all():
        return None
    closes = closes.ffill().bfill()
    return closes / closes.iloc[0] * initial


def compute_metrics(
    curve: pd.Series,
    trades: Iterable[Trade],
    exposure_days: int = 0,
    benchmark_curve: pd.Series | None = None,
) -> dict[str, float]:
    """Performance metrics. Everything here is after costs."""
    closed = [t for t in trades if not t.is_open]
    returns = curve.pct_change().dropna()

    total_return = (curve.iloc[-1] / curve.iloc[0]) - 1.0 if len(curve) > 1 else 0.0
    years = max(len(curve) / TRADING_DAYS, 1e-9)
    # A wiped-out account has no meaningful growth rate; report -100%.
    cagr = ((curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1.0) if curve.iloc[0] > 0 and curve.iloc[-1] > 0 else -1.0

    running_max = curve.cummax()
    drawdown = (curve - running_max) / running_max.where(running_max > 0)
    max_drawdown = float(drawdown.min()) if len(drawdown) else 0.0

    sharpe = 0.0
    if len(returns) > 1 and returns.std(ddof=0) > 0:
        sharpe = float(returns.mean() / returns.std(ddof=0) * np.sqrt(TRADING_DAYS))

    wins = [t for t in closed if t.pnl > 0]
    losses = [t for t in closed if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    metrics = {
        "trades": float(len(closed)),
        "hit_rate": len(wins) / len(closed) if closed else 0.0,
        "expectancy_r": float(np.mean([t.r_multiple for t in closed])) if closed else 0.0,
        # An unbroken run of winners has no finite profit factor; inf is the
        # honest report, and it should make you suspicious of the sample size.
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": float(max_drawdown) if np.isfinite(max_drawdown) else 0.0,
        "sharpe": sharpe,
        "exposure": exposure_days / len(curve) if len(curve) else 0.0,
        "avg_bars_held": float(np.mean([t.bars_held for t in closed])) if closed else 0.0,
        "avg_win_r": float(np.mean([t.r_multiple for t in wins])) if wins else 0.0,
        "avg_loss_r": float(np.mean([t.r_multiple for t in losses])) if losses else 0.0,
    }
    if benchmark_curve is not None and len(benchmark_curve) > 1:
        metrics["benchmark_return"] = float(benchmark_curve.iloc[-1] / benchmark_curve.iloc[0] - 1.0)
    return metrics


def slippage_sweep(
    bars_by_symbol: Mapping[str, pd.DataFrame],
    spec: Spec,
    benchmark: pd.DataFrame | None = None,
    initial_equity: float = 100_000.0,
) -> pd.DataFrame:
    """Re-run the strategy across the configured slippage assumptions.

    The single most useful table this project produces. An edge that only
    exists at zero slippage does not exist.
    """
    rows = []
    for bps in spec.costs.slippage_sweep_bps:
        result = run_backtest(
            bars_by_symbol, spec, benchmark, initial_equity, slippage_bps=bps
        )
        rows.append(
            {
                "slippage_bps": bps,
                "total_return": result.metrics["total_return"],
                "cagr": result.metrics["cagr"],
                "expectancy_r": result.metrics["expectancy_r"],
                "profit_factor": result.metrics["profit_factor"],
                "max_drawdown": result.metrics["max_drawdown"],
                "sharpe": result.metrics["sharpe"],
                "trades": result.metrics["trades"],
            }
        )
    return pd.DataFrame(rows).set_index("slippage_bps")


@dataclass
class Fold:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    chosen: dict[str, float] = field(default_factory=dict)
    test_metrics: dict[str, float] = field(default_factory=dict)


def walk_forward(
    bars_by_symbol: Mapping[str, pd.DataFrame],
    spec: Spec,
    benchmark: pd.DataFrame | None = None,
    folds: int = 4,
    param_grid: Mapping[str, Sequence[float]] | None = None,
    embargo_days: int = 5,
    initial_equity: float = 100_000.0,
) -> list[Fold]:
    """Walk-forward validation with an embargo between train and test.

    Without a ``param_grid`` this simply reports out-of-sample performance
    fold by fold, which is the honest baseline. With one, each fold picks its
    winner in-sample and is scored out-of-sample — the only number worth
    quoting.

    The embargo matters: daily bars are autocorrelated and indicators carry
    state across the boundary, so a test window starting the day after training
    ends is not really out of sample.
    """
    all_dates = sorted(set().union(*(set(f.index) for f in bars_by_symbol.values())))
    if len(all_dates) < folds * 2:
        raise ValueError("not enough history for that many folds")

    edges = np.linspace(0, len(all_dates), folds + 2, dtype=int)
    results: list[Fold] = []

    for k in range(folds):
        train_lo, train_hi = edges[0], edges[k + 1]
        test_lo, test_hi = min(train_hi + embargo_days, len(all_dates) - 1), edges[k + 2]
        if test_lo >= test_hi:
            continue

        fold = Fold(
            train_start=all_dates[train_lo].date(),
            train_end=all_dates[train_hi - 1].date(),
            test_start=all_dates[test_lo].date(),
            test_end=all_dates[test_hi - 1].date(),
        )

        chosen_spec = spec
        if param_grid:
            best_score, best_spec, best_params = -np.inf, spec, {}
            for params in _grid(param_grid):
                candidate = _with_weights(spec, params)
                try:
                    trial = run_backtest(
                        bars_by_symbol, candidate, benchmark, initial_equity,
                        start=fold.train_start, end=fold.train_end,
                    )
                except ValueError:
                    continue
                # Expectancy rather than total return: it is far less sensitive
                # to a single lucky trade dominating a short fold.
                value = trial.metrics["expectancy_r"]
                if value > best_score:
                    best_score, best_spec, best_params = value, candidate, params
            chosen_spec, fold.chosen = best_spec, best_params

        try:
            test = run_backtest(
                bars_by_symbol, chosen_spec, benchmark, initial_equity,
                start=fold.test_start, end=fold.test_end,
            )
            fold.test_metrics = test.metrics
        except ValueError as exc:
            log.warning("fold %d test window unusable: %s", k, exc)
        results.append(fold)

    return results


def _grid(param_grid: Mapping[str, Sequence[float]]):
    import itertools

    keys = list(param_grid)
    for combo in itertools.product(*(param_grid[k] for k in keys)):
        yield dict(zip(keys, combo))


def _with_weights(spec: Spec, params: Mapping[str, float]) -> Spec:
    """Return a copy of ``spec`` with component weights overridden."""
    from dataclasses import replace

    weights = dict(spec.weights)
    unknown = set(params) - set(weights)
    if unknown:
        raise ValueError(f"param_grid has unknown weight(s): {', '.join(sorted(unknown))}")
    weights.update(params)
    return replace(spec, weights=weights)
