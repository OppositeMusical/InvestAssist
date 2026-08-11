"""Generate the thinkScript scoring core from the shared spec.

The chart study, the watchlist column and the scan all need identical scoring
logic, and thinkScript has no usable include mechanism for user studies. Rather
than maintain three hand-written copies that will inevitably drift, the shared
block is generated into all three files between marker comments. Everything
below the markers is hand-written presentation and is left untouched.

Run via ``python -m investassist.cli gen-thinkscript``.
"""

from __future__ import annotations

from pathlib import Path

from .spec import Spec

BEGIN = "# ==== GENERATED CORE — DO NOT EDIT BY HAND ===="
END = "# ==== END GENERATED CORE ===="

TARGETS = (
    "investassist_score.ts",
    "investassist_column.ts",
    "investassist_scan.ts",
)


class GeneratorError(RuntimeError):
    pass


def _fmt(value: float) -> str:
    """Render a number for thinkScript.

    thinkScript does integer division on integer literals, so 1/3 is 0. Every
    constant that participates in arithmetic is emitted as a float to make that
    impossible.
    """
    return f"{float(value):.10g}" if float(value) != int(float(value)) else f"{float(value):.1f}"


def build_core(spec: Spec) -> str:
    """Return the shared thinkScript block for ``spec``."""
    f = spec.features
    r = spec.regime
    v = spec.verdicts
    e = spec.exits
    w = spec.normalised_weights

    lines: list[str] = []
    add = lines.append

    add(f"# Generated from spec '{spec.name}' v{spec.version}. Edit the YAML, not this.")
    add("")
    add("# --- constants ---")
    add(f"def EMA_FAST = {f.ema_fast};")
    add(f"def EMA_MID = {f.ema_mid};")
    add(f"def EMA_SLOW = {f.ema_slow};")
    add(f"def ADX_LENGTH = {f.adx_length};")
    add(f"def ADX_FLOOR = {_fmt(f.adx_trend_floor)};")
    add(f"def RSI_LENGTH = {f.rsi_length};")
    add(f"def RSI_FAST_LENGTH = {f.rsi_fast_length};")
    add(f"def MACD_FAST = {f.macd_fast};")
    add(f"def MACD_SLOW = {f.macd_slow};")
    add(f"def MACD_SIGNAL = {f.macd_signal};")
    add(f"def BB_LENGTH = {f.bb_length};")
    add(f"def BB_STDEV = {_fmt(f.bb_stdev)};")
    add(f"def ATR_LENGTH = {f.atr_length};")
    add(f"def DONCHIAN_LENGTH = {f.donchian_length};")
    add(f"def VOLUME_AVG_LENGTH = {f.volume_avg_length};")
    add(f"def MIN_BARS = {f.min_bars};")
    add(f'def BENCHMARK_TREND_LOOKBACK = {r.trend_lookback};')
    add(f"def BENCHMARK_ROC_LOOKBACK = {r.roc_lookback};")
    add(f"def RISK_ON_MULT = {_fmt(r.risk_on_multiplier)};")
    add(f"def NEUTRAL_MULT = {_fmt(r.neutral_multiplier)};")
    add(f"def RISK_OFF_MULT = {_fmt(r.risk_off_multiplier)};")
    add("")
    add("# Weights, already normalised to sum to 1.")
    add(f"def W_TREND_STACK = {_fmt(w['trend_stack'])};")
    add(f"def W_TREND_STRENGTH = {_fmt(w['trend_strength'])};")
    add(f"def W_MOMENTUM = {_fmt(w['momentum'])};")
    add(f"def W_RSI_POSITION = {_fmt(w['rsi_position'])};")
    add(f"def W_MEAN_REVERSION = {_fmt(w['mean_reversion'])};")
    add(f"def W_LOCATION = {_fmt(w['location'])};")
    add(f"def W_VOLUME = {_fmt(w['volume'])};")
    add("")
    add(f"def T_STRONG_BUY = {_fmt(v.strong_buy)};")
    add(f"def T_BUY = {_fmt(v.buy)};")
    add(f"def T_TRIM = {_fmt(v.trim)};")
    add(f"def T_SELL = {_fmt(v.sell)};")
    add(f"def INITIAL_STOP_ATR = {_fmt(e.initial_stop_atr)};")
    for i, target in enumerate(e.targets_r, start=1):
        add(f"def TARGET_{i}_R = {_fmt(target)};")
    add("")
    add("# --- L0: regime, read off the benchmark rather than this symbol ---")
    add(f'def spy = close("{r.benchmark}");')
    add("def spyAboveTrend = if spy > Average(spy, BENCHMARK_TREND_LOOKBACK) then 1 else 0;")
    add("def spyRoc = if spy[BENCHMARK_ROC_LOOKBACK] != 0")
    add("             then (spy / spy[BENCHMARK_ROC_LOOKBACK] - 1) * 100")
    add("             else 0;")
    add("def spyRising = if spyRoc > 0 then 1 else 0;")
    add("def regime = spyAboveTrend + spyRising;   # 2 risk on, 1 neutral, 0 risk off")
    add("def regimeMult = if regime == 2 then RISK_ON_MULT")
    add("                 else if regime == 1 then NEUTRAL_MULT")
    add("                 else RISK_OFF_MULT;")
    add("")
    add("# --- L1: components, each scored to -100..+100 ---")
    add("def atrVal = ATR(ATR_LENGTH);")
    add("def adxVal = ADX(ADX_LENGTH);")
    add("")
    add("# trend_stack: three ordering tests, a third of the range each.")
    add("def emaFast = ExpAverage(close, EMA_FAST);")
    add("def emaMid = ExpAverage(close, EMA_MID);")
    add("def emaSlow = ExpAverage(close, EMA_SLOW);")
    add("def stack = (if close > emaFast then 1 else if close < emaFast then -1 else 0)")
    add("          + (if emaFast > emaMid then 1 else if emaFast < emaMid then -1 else 0)")
    add("          + (if emaMid > emaSlow then 1 else if emaMid < emaSlow then -1 else 0);")
    add("def stackSign = if stack > 0 then 1 else if stack < 0 then -1 else 0;")
    add("def cTrendStack = stack / 3.0 * 100;")
    add("")
    add("# trend_strength: ADX gives magnitude, the stack gives direction.")
    add("def strengthRaw = (adxVal - ADX_FLOOR) / (50 - ADX_FLOOR);")
    add("def strengthFrac = if strengthRaw < 0 then 0 else if strengthRaw > 1 then 1 else strengthRaw;")
    add("def cTrendStrength = strengthFrac * 100 * stackSign;")
    add("")
    add("# momentum: MACD histogram, scaled by ATR to compare across price levels.")
    add("def macdVal = ExpAverage(close, MACD_FAST) - ExpAverage(close, MACD_SLOW);")
    add("def macdAvg = ExpAverage(macdVal, MACD_SIGNAL);")
    add("def macdHist = macdVal - macdAvg;")
    add("def momRaw = if atrVal > 0 then macdHist / (0.5 * atrVal) else 0;")
    add("def cMomentum = (if momRaw > 1 then 1 else if momRaw < -1 then -1 else momRaw) * 100;")
    add("")
    add("# Mean reversion is only trustworthy with the primary trend behind it.")
    add("# Buying a dip works inside an uptrend; below the slow EMA the identical")
    add("# signal is a falling knife, and would cancel out the trend components on")
    add("# exactly the names that deserve a SELL.")
    add("def aboveSlow = close > emaSlow;")
    add("")
    add("# rsi_position: the same reading means opposite things trending vs ranging.")
    add("def trending = adxVal >= ADX_FLOOR;")
    add("def rsiUsed = if trending then RSI(RSI_LENGTH) else RSI(RSI_FAST_LENGTH);")
    add("def rsiRaw = (rsiUsed - 50) / 50.0;")
    add("def rsiClipped = if rsiRaw > 1 then 1 else if rsiRaw < -1 then -1 else rsiRaw;")
    add("def rsiRange = -rsiClipped;")
    add("def rsiRangeAligned = if (rsiRange > 0 and aboveSlow) or (rsiRange < 0 and !aboveSlow)")
    add("                      then rsiRange else 0;")
    add("def cRsiPosition = (if trending then rsiClipped else rsiRangeAligned) * 100;")
    add("")
    add("# mean_reversion: %B inverted, damped while a real trend is running and")
    add("# zeroed when it would fight the primary trend.")
    add("def bbMid = Average(close, BB_LENGTH);")
    add("def bbSd = StDev(close, BB_LENGTH);")
    add("def bbUpper = bbMid + BB_STDEV * bbSd;")
    add("def bbLower = bbMid - BB_STDEV * bbSd;")
    add("def bbWidth = bbUpper - bbLower;")
    add("def pctB = if bbWidth != 0 then (close - bbLower) / bbWidth else 0.5;")
    add("def mrRaw = (0.5 - pctB) * 2;")
    add("def mrClipped = if mrRaw > 1 then 1 else if mrRaw < -1 then -1 else mrRaw;")
    add("def mrAligned = if (mrClipped > 0 and aboveSlow) or (mrClipped < 0 and !aboveSlow)")
    add("                then mrClipped else 0;")
    add("def cMeanReversion = mrAligned * 100 * (1 - strengthFrac);")
    add("")
    add("# location: where price sits in its 52-week range.")
    add("def rangeHigh = Highest(high, DONCHIAN_LENGTH);")
    add("def rangeLow = Lowest(low, DONCHIAN_LENGTH);")
    add("def rangeSpan = rangeHigh - rangeLow;")
    add("def rangePos = if rangeSpan > 0 then (close - rangeLow) / rangeSpan else 0.5;")
    add("def locRaw = (rangePos - 0.5) * 2;")
    add("def cLocation = (if locRaw > 1 then 1 else if locRaw < -1 then -1 else locRaw) * 100;")
    add("")
    add("# volume: confirmation only — a quiet day is not evidence against.")
    add("def avgVol = Average(volume, VOLUME_AVG_LENGTH);")
    add("def relVol = if avgVol > 0 then volume / avgVol else 1;")
    add("def volMag = relVol - 1;")
    add("def volMagClipped = if volMag < 0 then 0 else if volMag > 1 then 1 else volMag;")
    add("def volDir = if close > close[1] then 1 else if close < close[1] then -1 else 0;")
    add("def cVolume = volMagClipped * 100 * volDir;")
    add("")
    add("# --- L2: composite ---")
    add("def rawScore = cTrendStack     * W_TREND_STACK")
    add("             + cTrendStrength  * W_TREND_STRENGTH")
    add("             + cMomentum       * W_MOMENTUM")
    add("             + cRsiPosition    * W_RSI_POSITION")
    add("             + cMeanReversion  * W_MEAN_REVERSION")
    add("             + cLocation       * W_LOCATION")
    add("             + cVolume         * W_VOLUME;")
    add("def damped = rawScore * regimeMult;")
    add("def scoreVal = if damped > 100 then 100 else if damped < -100 then -100 else damped;")
    add("def enoughData = BarNumber() >= MIN_BARS;")
    add("")
    add("# --- L3: exit levels for a hypothetical entry at the current close ---")
    add("def initialStop = close - INITIAL_STOP_ATR * atrVal;")
    add("def riskPerShare = close - initialStop;")

    return "\n".join(lines)


def render_into(path: Path, core: str) -> bool:
    """Replace the generated region of ``path``. Returns True if it changed."""
    if not path.exists():
        raise GeneratorError(f"missing thinkScript file: {path}")

    # Explicit UTF-8 both ways: the markers contain an em dash, and Windows
    # would otherwise decode these files as cp1252 and never find them.
    text = path.read_text(encoding="utf-8")
    start = text.find(BEGIN)
    stop = text.find(END)
    if start == -1 or stop == -1:
        raise GeneratorError(f"{path.name}: missing generated-core markers")
    if stop < start:
        raise GeneratorError(f"{path.name}: markers are out of order")

    updated = text[: start + len(BEGIN)] + "\n" + core + "\n" + text[stop:]
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def generate(spec: Spec, directory: str | Path) -> list[str]:
    """Write the core block into every thinkScript target. Returns changed names."""
    directory = Path(directory)
    core = build_core(spec)
    return [name for name in TARGETS if render_into(directory / name, core)]
