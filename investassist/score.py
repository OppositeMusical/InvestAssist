"""The scoring engine: L0 regime, L1 features, L2 composite, L3 exits.

The model is deliberately rule-based and transparent. Every component reports
its own contribution so the panel can show *why* a verdict came out the way it
did — an opaque score is one you will override at exactly the wrong moment.

Bars are a ``pandas.DataFrame`` indexed by date with ``open``, ``high``,
``low``, ``close`` and ``volume`` columns, oldest first.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

import numpy as np
import pandas as pd

from . import indicators as ind
from .spec import COMPONENTS, Spec

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


class InsufficientData(Exception):
    """Raised when there is not enough history to score a symbol honestly."""


class Regime(str, Enum):
    RISK_ON = "RISK_ON"
    NEUTRAL = "NEUTRAL"
    RISK_OFF = "RISK_OFF"


class Verdict(str, Enum):
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    TRIM = "TRIM"
    SELL = "SELL"

    @property
    def is_bullish(self) -> bool:
        return self in (Verdict.STRONG_BUY, Verdict.BUY)

    @property
    def is_bearish(self) -> bool:
        return self in (Verdict.TRIM, Verdict.SELL)


@dataclass(frozen=True)
class ExitLevels:
    """Where to get out, computed from a reference entry price."""

    entry: float
    initial_stop: float
    risk_per_share: float
    trail_stop: float
    trail_active: bool
    targets: tuple[float, ...]
    time_stop_bars: int

    def r_multiple(self, price: float) -> float:
        """How many R the position is up (or down) at ``price``."""
        if self.risk_per_share <= 0:
            return 0.0
        return (price - self.entry) / self.risk_per_share


@dataclass(frozen=True)
class Assessment:
    """The complete answer for one symbol at one point in time."""

    symbol: str
    asof: pd.Timestamp
    price: float
    score: float
    raw_score: float
    verdict: Verdict
    confidence: float
    regime: Regime
    components: Mapping[str, float]
    contributions: Mapping[str, float]
    exits: ExitLevels
    atr: float
    atr_pct: float

    @property
    def rationale(self) -> list[str]:
        """Plain-language bullets, strongest contributor first.

        Deliberately generated from the computed numbers rather than written
        by hand or by a model — the narrative can only ever restate what the
        arithmetic already said.
        """
        labels = {
            "trend_stack": ("moving averages aligned bullish", "moving averages aligned bearish"),
            "trend_strength": ("trend is well established", "downtrend is well established"),
            "momentum": ("momentum improving", "momentum deteriorating"),
            "rsi_position": ("RSI supportive", "RSI unsupportive"),
            "mean_reversion": ("stretched to the downside, snapback likely", "extended to the upside"),
            "location": ("trading near the top of its 52-week range", "trading near the bottom of its 52-week range"),
            "volume": ("volume confirming the move up", "volume confirming the move down"),
        }
        ranked = sorted(
            self.contributions.items(), key=lambda kv: abs(kv[1]), reverse=True
        )
        out = []
        for name, contribution in ranked:
            if abs(contribution) < 1.0:
                continue
            positive, negative = labels[name]
            out.append(f"{positive if contribution > 0 else negative} ({contribution:+.0f})")
        if not out:
            out.append("no component is expressing a meaningful opinion")
        if self.regime is not Regime.RISK_ON:
            out.append(f"conviction damped: market regime is {self.regime.value}")
        return out


def _validate(bars: pd.DataFrame, spec: Spec, symbol: str) -> None:
    missing = set(REQUIRED_COLUMNS) - set(bars.columns)
    if missing:
        raise ValueError(
            f"{symbol}: bars missing column(s): {', '.join(sorted(missing))}"
        )
    if len(bars) < spec.features.min_bars:
        raise InsufficientData(
            f"{symbol}: need {spec.features.min_bars} bars to score, have {len(bars)}"
        )
    if not bars.index.is_monotonic_increasing:
        raise ValueError(f"{symbol}: bars must be sorted oldest-first")


def _clip_series(series: pd.Series) -> pd.Series:
    """Clamp to -1..1, mapping NaN to 0."""
    return series.fillna(0.0).clip(-1.0, 1.0)


def regime_series(benchmark: pd.DataFrame | None, spec: Spec) -> pd.Series | None:
    """L0 as a full series, for the backtester. ``None`` if unavailable."""
    if benchmark is None or len(benchmark) < spec.regime.trend_lookback + 1:
        return None
    close = benchmark["close"]
    above = close > ind.sma(close, spec.regime.trend_lookback)
    rising = ind.rate_of_change(close, spec.regime.roc_lookback).fillna(0.0) > 0
    codes = above.astype(int) + rising.astype(int)

    lookup = {2: Regime.RISK_ON, 1: Regime.NEUTRAL, 0: Regime.RISK_OFF}
    # Built explicitly as object dtype: Regime subclasses str, and pandas'
    # string inference would otherwise flatten the members back to plain
    # strings, breaking identity comparisons for every caller.
    return pd.Series(
        [lookup[int(code)] for code in codes.to_numpy()],
        index=codes.index,
        dtype=object,
    )


def detect_regime(benchmark: pd.DataFrame | None, spec: Spec) -> Regime:
    """L0 — classify the tape from the benchmark, not the symbol.

    With no benchmark data available we return NEUTRAL rather than assuming
    the best case; an unknown regime should cost conviction, not grant it.
    """
    series = regime_series(benchmark, spec)
    return Regime.NEUTRAL if series is None else series.iloc[-1]


def component_series(bars: pd.DataFrame, spec: Spec) -> pd.DataFrame:
    """L1 — every component as a full series, scored to -100..+100.

    Vectorised because the backtester needs the whole history and walking it
    one bar at a time would be quadratic. The single-bar path below is just
    the last row of this, so the two can never drift apart.
    """
    f = spec.features
    high, low, close, volume = bars["high"], bars["low"], bars["close"], bars["volume"]

    atr_val = ind.atr(high, low, close, f.atr_length)
    adx_val = ind.adx(high, low, close, f.adx_length)

    # --- trend_stack: three ordering tests, each worth a third of the range.
    # Discrete on purpose: it ports to thinkScript exactly and is far less
    # jumpy than a continuous distance measure near crossovers.
    ema_fast = ind.ema(close, f.ema_fast)
    ema_mid = ind.ema(close, f.ema_mid)
    ema_slow = ind.ema(close, f.ema_slow)
    stack = (
        np.sign(close - ema_fast)
        + np.sign(ema_fast - ema_mid)
        + np.sign(ema_mid - ema_slow)
    )
    trend_stack = stack / 3.0 * 100.0

    # --- trend_strength: ADX says how strong, trend_stack says which way.
    span = max(50.0 - f.adx_trend_floor, 1e-9)
    strength = ((adx_val - f.adx_trend_floor) / span).fillna(0.0).clip(0.0, 1.0)
    trend_strength = strength * 100.0 * np.sign(stack)

    # --- momentum: MACD histogram, scaled by ATR so it is comparable across
    # names with different price levels.
    _, _, hist = ind.macd(close, f.macd_fast, f.macd_slow, f.macd_signal)
    momentum = _clip_series(hist / (0.5 * atr_val.where(atr_val > 0))) * 100.0

    # Mean reversion is only trustworthy with the primary trend behind it.
    # Buying a dip works inside an uptrend; below the slow EMA the identical
    # signal is a falling knife. Without this gate the mean-reverting
    # components cancel out the trend components on exactly the names that
    # deserve a SELL, and the model almost never tells you to get out.
    above_slow = close > ema_slow

    def with_trend(raw: pd.Series) -> pd.Series:
        allowed = ((raw > 0) & above_slow) | ((raw < 0) & ~above_slow)
        return raw.where(allowed, 0.0)

    # --- rsi_position: the same reading means opposite things depending on
    # whether we are trending or ranging, so ADX decides which leg applies.
    trending = adx_val >= f.adx_trend_floor
    rsi_slow = _clip_series((ind.rsi(close, f.rsi_length) - 50.0) / 50.0)
    rsi_fast = _clip_series((ind.rsi(close, f.rsi_fast_length) - 50.0) / 50.0)
    rsi_position = (rsi_slow * 100.0).where(trending, with_trend(-rsi_fast * 100.0))

    # --- mean_reversion: %B inverted, damped when a real trend is running and
    # zeroed when it would fight the primary trend.
    pct_b = ind.bollinger_percent_b(close, f.bb_length, f.bb_stdev)
    mean_reversion = with_trend(_clip_series((0.5 - pct_b) * 2.0) * 100.0) * (1.0 - strength)

    # --- location: where price sits in its 52-week range.
    hi = ind.highest(high, f.donchian_length)
    lo = ind.lowest(low, f.donchian_length)
    span_hl = hi - lo
    position = ((close - lo) / span_hl.where(span_hl > 0)).fillna(0.5)
    location = _clip_series((position - 0.5) * 2.0) * 100.0

    # --- volume: confirmation only. Below-average volume says nothing, so it
    # scores zero rather than counting as evidence against.
    avg_vol = ind.sma(volume, f.volume_avg_length)
    rel_vol = (volume / avg_vol.where(avg_vol > 0)).fillna(1.0)
    direction = np.sign(close.diff().fillna(0.0))
    vol_component = (rel_vol - 1.0).clip(0.0, 1.0) * 100.0 * direction

    return pd.DataFrame(
        {
            "trend_stack": trend_stack,
            "trend_strength": trend_strength,
            "momentum": momentum,
            "rsi_position": rsi_position,
            "mean_reversion": mean_reversion,
            "location": location,
            "volume": vol_component,
        },
        index=bars.index,
    ).fillna(0.0)


def compute_components(bars: pd.DataFrame, spec: Spec) -> dict[str, float]:
    """L1 — each component scored to -100..+100 on the most recent bar."""
    return {k: float(v) for k, v in component_series(bars, spec).iloc[-1].items()}


def score_series(
    bars: pd.DataFrame, spec: Spec, benchmark: pd.DataFrame | None = None
) -> pd.Series:
    """L2 as a full series — the backtester's view of the model."""
    components = component_series(bars, spec)
    weights = spec.normalised_weights
    raw = sum(components[name] * weights[name] for name in COMPONENTS)

    multipliers = {
        Regime.RISK_ON: spec.regime.risk_on_multiplier,
        Regime.NEUTRAL: spec.regime.neutral_multiplier,
        Regime.RISK_OFF: spec.regime.risk_off_multiplier,
    }
    regimes = regime_series(benchmark, spec)
    if regimes is None:
        factor = pd.Series(spec.regime.neutral_multiplier, index=bars.index)
    else:
        # Reindex onto the symbol's calendar. Forward-fill only: a holiday on
        # the benchmark must reuse the last *known* regime, never peek ahead.
        factor = (
            regimes.reindex(bars.index.union(regimes.index))
            .ffill()
            .reindex(bars.index)
            .map(multipliers)
            .fillna(spec.regime.neutral_multiplier)
        )
    return (raw * factor).clip(-100.0, 100.0)


def classify(score: float, spec: Spec) -> Verdict:
    v = spec.verdicts
    if score >= v.strong_buy:
        return Verdict.STRONG_BUY
    if score >= v.buy:
        return Verdict.BUY
    if score <= v.sell:
        return Verdict.SELL
    if score <= v.trim:
        return Verdict.TRIM
    return Verdict.HOLD


def compute_confidence(
    components: Mapping[str, float], score: float, atr_pct: float, spec: Spec
) -> float:
    """How much to trust the score, 0..100.

    Two ingredients: how much of the weighted model agrees with the sign of
    the final score, and whether volatility is high enough to make any point
    estimate unreliable.
    """
    c = spec.confidence
    weights = spec.normalised_weights

    if score == 0:
        agreement = 0.0
    else:
        want = np.sign(score)
        agreeing = sum(
            weights[name]
            for name, value in components.items()
            if abs(value) >= 1.0 and np.sign(value) == want
        )
        opining = sum(
            weights[name] for name, value in components.items() if abs(value) >= 1.0
        )
        agreement = agreeing / opining if opining > 0 else 0.0

    vol_term = 1.0 - max(0.0, min(1.0, atr_pct / c.high_atr_pct))
    total = c.agreement_weight + c.volatility_weight
    blended = (c.agreement_weight * agreement + c.volatility_weight * vol_term) / total
    return round(blended * 100.0, 1)


def compute_exits(
    bars: pd.DataFrame, entry: float, atr_now: float, spec: Spec
) -> ExitLevels:
    """L3 — stop, trail, targets and time stop for a long position."""
    e = spec.exits
    initial_stop = entry - e.initial_stop_atr * atr_now
    risk = max(entry - initial_stop, 1e-9)

    chandelier = (
        float(ind.highest(bars["high"], e.chandelier_lookback).iloc[-1])
        - e.chandelier_atr * atr_now
    )
    price = float(bars["close"].iloc[-1])
    trail_active = (price - entry) / risk >= e.trail_activate_r

    return ExitLevels(
        entry=entry,
        initial_stop=initial_stop,
        risk_per_share=risk,
        # Once trailing engages it can only ratchet upward, never give back
        # ground to the original stop.
        trail_stop=max(chandelier, initial_stop) if trail_active else initial_stop,
        trail_active=trail_active,
        targets=tuple(entry + r * risk for r in e.targets_r),
        time_stop_bars=e.time_stop_bars,
    )


def assess(
    bars: pd.DataFrame,
    spec: Spec,
    symbol: str = "?",
    benchmark: pd.DataFrame | None = None,
    entry: float | None = None,
) -> Assessment:
    """Score one symbol. ``entry`` defaults to the last close for new positions."""
    _validate(bars, spec, symbol)

    regime = detect_regime(benchmark, spec)
    components = compute_components(bars, spec)
    weights = spec.normalised_weights

    contributions = {name: components[name] * weights[name] for name in COMPONENTS}
    raw_score = sum(contributions.values())

    multiplier = {
        Regime.RISK_ON: spec.regime.risk_on_multiplier,
        Regime.NEUTRAL: spec.regime.neutral_multiplier,
        Regime.RISK_OFF: spec.regime.risk_off_multiplier,
    }[regime]
    # Damping only ever reduces conviction, so it must not rescue a negative
    # score toward zero — apply it to the magnitude and keep the sign.
    score = max(-100.0, min(100.0, raw_score * multiplier))

    price = float(bars["close"].iloc[-1])
    atr_now = float(ind.atr(bars["high"], bars["low"], bars["close"], spec.features.atr_length).iloc[-1])
    atr_pct = (atr_now / price * 100.0) if price > 0 else 0.0

    return Assessment(
        symbol=symbol,
        asof=bars.index[-1],
        price=price,
        score=round(score, 2),
        raw_score=round(raw_score, 2),
        verdict=classify(score, spec),
        confidence=compute_confidence(components, score, atr_pct, spec),
        regime=regime,
        components={k: round(v, 2) for k, v in components.items()},
        contributions={k: round(v, 2) for k, v in contributions.items()},
        exits=compute_exits(bars, entry if entry is not None else price, atr_now, spec),
        atr=round(atr_now, 4),
        atr_pct=round(atr_pct, 2),
    )
