"""Technical indicators, written to match thinkScript's builtins exactly.

Every function here has a named thinkScript equivalent, noted in its docstring.
That is a hard constraint on this module: if an indicator cannot be expressed
in thinkScript, it does not belong in the scoring model, because the on-chart
study could not reproduce it and the two implementations would diverge.

The subtleties that actually bite:

* thinkScript's ``ExpAverage`` seeds recursively from the first bar rather than
  from an SMA, which is ``ewm(span=n, adjust=False)`` and *not* the adjusted
  default pandas gives you.
* ``WildersAverage`` is an EMA with ``alpha = 1/n``, not ``2/(n+1)``.
* thinkScript's ``StDev`` is the population standard deviation (``ddof=0``).
* thinkScript's ``RSI`` is written as ``50 * (netChgAvg / totChgAvg + 1)``,
  which reduces algebraically to Wilder's ``100 * gain / (gain + loss)``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "sma",
    "ema",
    "wilders",
    "stdev",
    "true_range",
    "atr",
    "rsi",
    "macd",
    "bollinger_percent_b",
    "adx",
    "highest",
    "lowest",
    "rate_of_change",
]


def sma(series: pd.Series, length: int) -> pd.Series:
    """thinkScript ``Average(series, length)``."""
    return series.rolling(length, min_periods=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    """thinkScript ``ExpAverage(series, length)``.

    Recursive seeding from the first observation, hence ``adjust=False``.
    """
    return series.ewm(span=length, adjust=False).mean()


def wilders(series: pd.Series, length: int) -> pd.Series:
    """thinkScript ``WildersAverage(series, length)`` — EMA with alpha = 1/n."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def stdev(series: pd.Series, length: int) -> pd.Series:
    """thinkScript ``StDev(series, length)`` — population, so ``ddof=0``."""
    return series.rolling(length, min_periods=length).std(ddof=0)


def highest(series: pd.Series, length: int) -> pd.Series:
    """thinkScript ``Highest(series, length)``."""
    return series.rolling(length, min_periods=length).max()


def lowest(series: pd.Series, length: int) -> pd.Series:
    """thinkScript ``Lowest(series, length)``."""
    return series.rolling(length, min_periods=length).min()


def rate_of_change(series: pd.Series, length: int) -> pd.Series:
    """Percent change over ``length`` bars — thinkScript ``RateOfChange``."""
    return (series / series.shift(length) - 1.0) * 100.0


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """thinkScript ``TrueRange(high, close, low)``.

    Defined as ``max(high, close[1]) - min(low, close[1])``. On the first bar
    there is no prior close, so this falls back to the bar's own range.
    """
    prev_close = close.shift(1)
    upper = pd.concat([high, prev_close], axis=1).max(axis=1)
    lower = pd.concat([low, prev_close], axis=1).min(axis=1)
    tr = upper - lower
    tr.iloc[0] = high.iloc[0] - low.iloc[0]
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """thinkScript ``ATR(length)``, which smooths true range with Wilders."""
    return wilders(true_range(high, low, close), length)


def rsi(close: pd.Series, length: int) -> pd.Series:
    """thinkScript ``RSI(length)`` with the default Wilders smoothing.

    Returns 0..100. Where the market has been perfectly flat over the window
    both averages are zero; thinkScript yields 50 there and so do we.
    """
    net_change = close.diff()
    net_change.iloc[0] = 0.0
    net_avg = wilders(net_change, length)
    tot_avg = wilders(net_change.abs(), length)
    ratio = np.where(tot_avg != 0, net_avg / tot_avg.replace(0, np.nan), 0.0)
    return pd.Series(50.0 * (np.nan_to_num(ratio) + 1.0), index=close.index)


def macd(
    close: pd.Series, fast: int, slow: int, signal: int
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """thinkScript ``MACD(fast, slow, signal)``.

    Returns ``(value, avg, histogram)`` matching thinkScript's ``Value``,
    ``Avg`` and ``Diff`` plots respectively.
    """
    value = ema(close, fast) - ema(close, slow)
    avg = ema(value, signal)
    return value, avg, value - avg


def bollinger_percent_b(close: pd.Series, length: int, num_stdev: float) -> pd.Series:
    """Position within the Bollinger band, 0 at the lower band, 1 at the upper.

    thinkScript equivalent is ``BollingerPercentB(length, ...)``. A zero-width
    band (a dead-flat window) has no meaningful position, so we return the
    midpoint rather than dividing by zero.
    """
    mid = sma(close, length)
    sd = stdev(close, length)
    upper = mid + num_stdev * sd
    lower = mid - num_stdev * sd
    width = upper - lower
    return ((close - lower) / width.where(width != 0)).fillna(0.5)


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, length: int
) -> pd.Series:
    """thinkScript ``ADX(length)`` — Wilder's average directional index.

    Returns 0..100. Directionless by construction: it measures how strongly
    the market is trending, not which way.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=high.index,
    )

    tr_smooth = wilders(true_range(high, low, close), length)
    # A zero smoothed true range means no movement at all; DI is undefined
    # there, and reporting 0 is the honest answer.
    safe_tr = tr_smooth.where(tr_smooth != 0)
    plus_di = 100.0 * wilders(plus_dm, length) / safe_tr
    minus_di = 100.0 * wilders(minus_dm, length) / safe_tr

    di_sum = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum.where(di_sum != 0)
    return wilders(dx.fillna(0.0), length)
