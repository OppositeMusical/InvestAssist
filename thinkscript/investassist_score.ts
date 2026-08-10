# InvestAssist — on-chart buy/sell assist
#
# Add as a LOWER study. Plots the composite score (-100..+100) and prints the
# verdict, regime and exit levels as chart labels for whatever symbol you are
# viewing. There is no symbol-detection problem here: the study simply runs on
# the current chart, which is why the on-chart route answers "the stock I'm
# looking at" for free.
#
# Setup: Studies > Edit Studies > Import, select this file, then add it to a
# chart. Right-click the study to create alerts off CrossedIntoBuy /
# CrossedIntoSell.
#
# The block between the GENERATED markers is written by
# `python -m investassist.cli gen-thinkscript` from investassist/spec/*.yaml
# and mirrors investassist/score.py. Do not hand-edit it — change the YAML and
# regenerate, or tests/test_parity.py will fail.

declare lower;

# ==== GENERATED CORE — DO NOT EDIT BY HAND ====
# Generated from spec 'default' v1. Edit the YAML, not this.

# --- constants ---
def EMA_FAST = 20;
def EMA_MID = 50;
def EMA_SLOW = 200;
def ADX_LENGTH = 14;
def ADX_FLOOR = 20.0;
def RSI_LENGTH = 14;
def RSI_FAST_LENGTH = 2;
def MACD_FAST = 12;
def MACD_SLOW = 26;
def MACD_SIGNAL = 9;
def BB_LENGTH = 20;
def BB_STDEV = 2.0;
def ATR_LENGTH = 14;
def DONCHIAN_LENGTH = 252;
def VOLUME_AVG_LENGTH = 20;
def MIN_BARS = 260;
def BENCHMARK_TREND_LOOKBACK = 200;
def BENCHMARK_ROC_LOOKBACK = 20;
def RISK_ON_MULT = 1.0;
def NEUTRAL_MULT = 0.85;
def RISK_OFF_MULT = 0.55;

# Weights, already normalised to sum to 1.
def W_TREND_STACK = 0.22;
def W_TREND_STRENGTH = 0.1;
def W_MOMENTUM = 0.18;
def W_RSI_POSITION = 0.12;
def W_MEAN_REVERSION = 0.14;
def W_LOCATION = 0.12;
def W_VOLUME = 0.12;

def T_STRONG_BUY = 60.0;
def T_BUY = 25.0;
def T_TRIM = -25.0;
def T_SELL = -60.0;
def INITIAL_STOP_ATR = 2.0;
def TARGET_1_R = 1.0;
def TARGET_2_R = 2.0;

# --- L0: regime, read off the benchmark rather than this symbol ---
def spy = close("SPY");
def spyAboveTrend = if spy > Average(spy, BENCHMARK_TREND_LOOKBACK) then 1 else 0;
def spyRoc = if spy[BENCHMARK_ROC_LOOKBACK] != 0
             then (spy / spy[BENCHMARK_ROC_LOOKBACK] - 1) * 100
             else 0;
def spyRising = if spyRoc > 0 then 1 else 0;
def regime = spyAboveTrend + spyRising;   # 2 risk on, 1 neutral, 0 risk off
def regimeMult = if regime == 2 then RISK_ON_MULT
                 else if regime == 1 then NEUTRAL_MULT
                 else RISK_OFF_MULT;

# --- L1: components, each scored to -100..+100 ---
def atrVal = ATR(ATR_LENGTH);
def adxVal = ADX(ADX_LENGTH);

# trend_stack: three ordering tests, a third of the range each.
def emaFast = ExpAverage(close, EMA_FAST);
def emaMid = ExpAverage(close, EMA_MID);
def emaSlow = ExpAverage(close, EMA_SLOW);
def stack = (if close > emaFast then 1 else if close < emaFast then -1 else 0)
          + (if emaFast > emaMid then 1 else if emaFast < emaMid then -1 else 0)
          + (if emaMid > emaSlow then 1 else if emaMid < emaSlow then -1 else 0);
def stackSign = if stack > 0 then 1 else if stack < 0 then -1 else 0;
def cTrendStack = stack / 3.0 * 100;

# trend_strength: ADX gives magnitude, the stack gives direction.
def strengthRaw = (adxVal - ADX_FLOOR) / (50 - ADX_FLOOR);
def strengthFrac = if strengthRaw < 0 then 0 else if strengthRaw > 1 then 1 else strengthRaw;
def cTrendStrength = strengthFrac * 100 * stackSign;

# momentum: MACD histogram, scaled by ATR to compare across price levels.
def macdVal = ExpAverage(close, MACD_FAST) - ExpAverage(close, MACD_SLOW);
def macdAvg = ExpAverage(macdVal, MACD_SIGNAL);
def macdHist = macdVal - macdAvg;
def momRaw = if atrVal > 0 then macdHist / (0.5 * atrVal) else 0;
def cMomentum = (if momRaw > 1 then 1 else if momRaw < -1 then -1 else momRaw) * 100;

# Mean reversion is only trustworthy with the primary trend behind it.
# Buying a dip works inside an uptrend; below the slow EMA the identical
# signal is a falling knife, and would cancel out the trend components on
# exactly the names that deserve a SELL.
def aboveSlow = close > emaSlow;

# rsi_position: the same reading means opposite things trending vs ranging.
def trending = adxVal >= ADX_FLOOR;
def rsiUsed = if trending then RSI(RSI_LENGTH) else RSI(RSI_FAST_LENGTH);
def rsiRaw = (rsiUsed - 50) / 50.0;
def rsiClipped = if rsiRaw > 1 then 1 else if rsiRaw < -1 then -1 else rsiRaw;
def rsiRange = -rsiClipped;
def rsiRangeAligned = if (rsiRange > 0 and aboveSlow) or (rsiRange < 0 and !aboveSlow)
                      then rsiRange else 0;
def cRsiPosition = (if trending then rsiClipped else rsiRangeAligned) * 100;

# mean_reversion: %B inverted, damped while a real trend is running and
# zeroed when it would fight the primary trend.
def bbMid = Average(close, BB_LENGTH);
def bbSd = StDev(close, BB_LENGTH);
def bbUpper = bbMid + BB_STDEV * bbSd;
def bbLower = bbMid - BB_STDEV * bbSd;
def bbWidth = bbUpper - bbLower;
def pctB = if bbWidth != 0 then (close - bbLower) / bbWidth else 0.5;
def mrRaw = (0.5 - pctB) * 2;
def mrClipped = if mrRaw > 1 then 1 else if mrRaw < -1 then -1 else mrRaw;
def mrAligned = if (mrClipped > 0 and aboveSlow) or (mrClipped < 0 and !aboveSlow)
                then mrClipped else 0;
def cMeanReversion = mrAligned * 100 * (1 - strengthFrac);

# location: where price sits in its 52-week range.
def rangeHigh = Highest(high, DONCHIAN_LENGTH);
def rangeLow = Lowest(low, DONCHIAN_LENGTH);
def rangeSpan = rangeHigh - rangeLow;
def rangePos = if rangeSpan > 0 then (close - rangeLow) / rangeSpan else 0.5;
def locRaw = (rangePos - 0.5) * 2;
def cLocation = (if locRaw > 1 then 1 else if locRaw < -1 then -1 else locRaw) * 100;

# volume: confirmation only — a quiet day is not evidence against.
def avgVol = Average(volume, VOLUME_AVG_LENGTH);
def relVol = if avgVol > 0 then volume / avgVol else 1;
def volMag = relVol - 1;
def volMagClipped = if volMag < 0 then 0 else if volMag > 1 then 1 else volMag;
def volDir = if close > close[1] then 1 else if close < close[1] then -1 else 0;
def cVolume = volMagClipped * 100 * volDir;

# --- L2: composite ---
def rawScore = cTrendStack     * W_TREND_STACK
             + cTrendStrength  * W_TREND_STRENGTH
             + cMomentum       * W_MOMENTUM
             + cRsiPosition    * W_RSI_POSITION
             + cMeanReversion  * W_MEAN_REVERSION
             + cLocation       * W_LOCATION
             + cVolume         * W_VOLUME;
def damped = rawScore * regimeMult;
def scoreVal = if damped > 100 then 100 else if damped < -100 then -100 else damped;
def enoughData = BarNumber() >= MIN_BARS;

# --- L3: exit levels for a hypothetical entry at the current close ---
def initialStop = close - INITIAL_STOP_ATR * atrVal;
def riskPerShare = close - initialStop;
# ==== END GENERATED CORE ====

plot Score = if enoughData then scoreVal else Double.NaN;
Score.SetPaintingStrategy(PaintingStrategy.LINE);
Score.SetLineWeight(3);
Score.AssignValueColor(
    if scoreVal >= T_STRONG_BUY then Color.GREEN
    else if scoreVal >= T_BUY then Color.LIGHT_GREEN
    else if scoreVal <= T_SELL then Color.RED
    else if scoreVal <= T_TRIM then Color.PINK
    else Color.GRAY);

plot StrongBuyLine = if enoughData then T_STRONG_BUY else Double.NaN;
plot BuyLine = if enoughData then T_BUY else Double.NaN;
plot Zero = if enoughData then 0 else Double.NaN;
plot TrimLine = if enoughData then T_TRIM else Double.NaN;
plot SellLine = if enoughData then T_SELL else Double.NaN;

StrongBuyLine.SetDefaultColor(Color.DARK_GREEN);
StrongBuyLine.SetStyle(Curve.SHORT_DASH);
BuyLine.SetDefaultColor(Color.DARK_GREEN);
BuyLine.SetStyle(Curve.SHORT_DASH);
Zero.SetDefaultColor(Color.DARK_GRAY);
TrimLine.SetDefaultColor(Color.DARK_RED);
TrimLine.SetStyle(Curve.SHORT_DASH);
SellLine.SetDefaultColor(Color.DARK_RED);
SellLine.SetStyle(Curve.SHORT_DASH);

# ---------------------------------------------------------------------------
# Labels — the actual answer, in words.
# ---------------------------------------------------------------------------
AddLabel(yes,
    (if !enoughData then "INSUFFICIENT DATA"
     else if scoreVal >= T_STRONG_BUY then "STRONG BUY"
     else if scoreVal >= T_BUY then "BUY"
     else if scoreVal <= T_SELL then "SELL"
     else if scoreVal <= T_TRIM then "TRIM"
     else "HOLD") + "  " + Round(scoreVal, 0),
    if !enoughData then Color.GRAY
    else if scoreVal >= T_STRONG_BUY then Color.GREEN
    else if scoreVal >= T_BUY then Color.LIGHT_GREEN
    else if scoreVal <= T_SELL then Color.RED
    else if scoreVal <= T_TRIM then Color.PINK
    else Color.LIGHT_GRAY);

AddLabel(enoughData,
    "Regime " + (if regime == 2 then "RISK ON" else if regime == 1 then "NEUTRAL" else "RISK OFF"),
    if regime == 2 then Color.GREEN else if regime == 1 then Color.YELLOW else Color.RED);

AddLabel(enoughData,
    "Stop " + Round(initialStop, 2)
    + "  R " + Round(riskPerShare, 2)
    + "  T1 " + Round(close + TARGET_1_R * riskPerShare, 2)
    + "  T2 " + Round(close + TARGET_2_R * riskPerShare, 2),
    Color.LIGHT_GRAY);

AddLabel(enoughData,
    "ATR " + Round(atrVal, 2) + " (" + Round(atrVal / close * 100, 1) + "%)"
    + "  ADX " + Round(adxVal, 0),
    Color.LIGHT_GRAY);

# Component breakdown, so the score is never a black box. Toggle off in the
# study settings if the label row gets crowded.
AddLabel(enoughData,
    "trend " + Round(cTrendStack * W_TREND_STACK, 0)
    + " / str " + Round(cTrendStrength * W_TREND_STRENGTH, 0)
    + " / mom " + Round(cMomentum * W_MOMENTUM, 0)
    + " / rsi " + Round(cRsiPosition * W_RSI_POSITION, 0)
    + " / mr " + Round(cMeanReversion * W_MEAN_REVERSION, 0)
    + " / loc " + Round(cLocation * W_LOCATION, 0)
    + " / vol " + Round(cVolume * W_VOLUME, 0),
    Color.GRAY);

# ---------------------------------------------------------------------------
# Study alert hooks. Right-click the study -> Create Alert, or reference these
# from MarketWatch > Alerts. This is the independent backstop that keeps
# working when the Python engine is not running.
# ---------------------------------------------------------------------------
plot CrossedIntoBuy = if enoughData and scoreVal >= T_BUY and scoreVal[1] < T_BUY then 1 else 0;
plot CrossedIntoSell = if enoughData and scoreVal <= T_TRIM and scoreVal[1] > T_TRIM then 1 else 0;
CrossedIntoBuy.SetHiding(yes);
CrossedIntoSell.SetHiding(yes);
