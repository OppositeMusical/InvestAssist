# InvestAssist

A buy/sell decision assist for **US stocks and ETFs** that works alongside
thinkorswim and tells you what to do with the symbol you're looking at.

Two halves of one system:

- **On the chart.** A thinkScript study prints `BUY 72` / `HOLD` / `SELL −64`
  on whatever symbol you open, with the stop, targets and a component
  breakdown. No servers, no API keys, no auth.
- **Beside the chart.** A Python engine does what thinkScript structurally
  cannot — portfolio-level risk, position sizing, and an honest backtest — and
  surfaces it in a companion panel that follows the ticker you copy.

Both compute the same score from the same spec file, and a parity test keeps
them from drifting.

> This is a personal decision-support tool. It produces recommendations, not
> advice, and it places no orders. See [Scope and limits](#scope-and-limits).

---

## Quick start

### The chart study (start here — one evening, no dependencies)

1. In thinkorswim: **Studies → Edit Studies → Import**, select
   `thinkscript/investassist_score.ts`, and add it to a daily chart.
2. Give the chart at least 260 bars of history — the study suppresses itself
   below that rather than scoring on thin data.
3. Right-click the study → **Create Alert** off `CrossedIntoBuy` or
   `CrossedIntoSell` to get pushed when something changes.

Optional extras:

- **Watchlist column:** MarketWatch → Quotes → right-click a header →
  Customize → pick a free **Custom** slot → pencil icon → paste
  `thinkscript/investassist_column.ts`.
- **Scanner:** Scan → Stock Hacker → Add Study filter → thinkScript Editor →
  paste `thinkscript/investassist_scan.ts`. Set `SCAN_MODE` to choose between
  fresh entries, current qualifiers, and exit candidates.

### The Python engine

```bash
pip install -e '.[dev,data]'

# Score a symbol
investassist score AAPL --equity 100000

# Rank a universe
investassist scan --file universe.txt

# The companion panel — copy a ticker in thinkorswim and it follows along
investassist panel --equity 100000

# The part that decides whether any of this is worth running
investassist backtest --file universe.txt --sweep
```

Default history comes from **yfinance** (free, no key, split/dividend
adjusted). Use `--provider csv --csv-dir ./data` to feed it your own exports.

`--provider stooq` exists but is bot-blocked as of August 2026 — it 404s the
default user agent and serves HTML instead of CSV even with a browser one.
Kept only as a fallback in case that changes.

---

## How it decides

Four layers, each independently testable and independently falsifiable.

**L0 — Regime.** Reads SPY, not your symbol: above its 200-day and rising is
`RISK_ON`, neither is `RISK_OFF`. Risk-off *damps conviction* rather than
flipping the call, so a strong stock in a weak tape scores lower but not
negative.

**L1 — Components.** Seven, each scored to −100…+100: EMA stack alignment,
ADX-gated trend strength, MACD momentum, RSI position, Bollinger mean
reversion, position in the 52-week range, and relative volume confirmation.

Every one has a native thinkScript equivalent. That is a hard constraint —
an indicator the on-chart study cannot reproduce does not belong in the model.

**L2 — Composite.** A weighted mean, damped by regime, mapped to
`STRONG BUY / BUY / HOLD / TRIM / SELL`, with a separate confidence figure
built from how much of the model agrees and how volatile the name is.

Rule-based and transparent on purpose. The panel shows every component's
contribution, because an opaque score is one you'll override at exactly the
wrong moment.

**L3 — Exits, with equal billing.** Most retail systems have a buy rule and no
sell rule. Every position carries an initial stop at `entry − 2×ATR`, a
chandelier trail that engages at +1R, scale-outs at +1R and +2R, a time stop,
and a thesis-invalidation exit when the score crosses back through zero.

### One design decision worth knowing

Mean reversion is **gated on the primary trend**. Buying a dip works inside an
uptrend; below the 200-day EMA the identical signal is a falling knife. Without
that gate the mean-reverting components cancel out the trend components on
exactly the names that deserve a SELL — measured at +82 and +64 against a
correctly bearish −100 and −98 on a stock down 45%. The model would almost
never tell you to get out.

---

## Configuration

`investassist/spec/default.yaml` is the single source of truth: indicator
lengths, component weights, verdict thresholds, exit rules, risk limits and the
cost model. Change a number there and regenerate:

```bash
investassist gen-thinkscript
```

That rewrites the marked block in all three `.ts` files. Everything below the
markers is hand-written presentation and is left alone. `tests/test_parity.py`
fails if you forget.

Unknown keys are rejected rather than ignored — a typo in a spec file should be
an error, not a silent fallback to defaults.

---

## The thinkorswim bridges

thinkorswim has no outbound API and will not tell an external program what's on
your screen, so two things have to be bridged.

### Live data — the RTD bridge

thinkorswim ships an **RTD COM server** on Windows, officially documented by
Schwab. A logged-in thinkorswim serves quotes to any local process: no OAuth,
no developer app, no approval wait, no data subscription.

The part that matters is `CUSTOM1`..`CUSTOM19`, which expose your custom-quote
studies. Install `investassist_column.ts` into Custom 1 and the score
thinkorswim computes on-chart becomes readable from Python — the two
implementations meeting in the middle.

```bash
pip install -e '.[windows]'
investassist record AAPL MSFT --out ./recorded --interval 60
```

Start recording early. RTD is a quote stream, not a history API, and
thinkorswim will not give you intraday history — the only free way to get any
is to start capturing now.

> **Status:** the COM path is written to the documented `IRtdServer` contract
> but has not been run against a live thinkorswim. It needs a verification pass
> on Windows before you trust it. Everything else, including the recorder, is
> tested against `MockRTDClient`.

### Symbol context — what am I looking at?

Ranked by robustness. All read-only; nothing here automates order entry.

1. **Clipboard watcher** *(implemented)* — copy a ticker in thinkorswim and the
   panel follows. ~40 lines, no dependencies, and it cannot break when Schwab
   reskins the platform.
2. **Global hotkey** *(implemented, Windows)* — `Ctrl+Shift+I` when you'd
   rather press a key than have the panel react to everything you copy.
3. **Window title** — detached chart windows often carry the symbol. Worth a
   one-hour spike on your layout; free if it works.
4. **OCR of the symbol field** — short uppercase tickers in a fixed font is
   near-best-case for Tesseract. Breaks when you move panels.
5. **Java Access Bridge** — thinkorswim is Java/Swing, so JAB exposes the
   accessibility tree. Most "correct", flakiest in practice.

---

## Validating the strategy

The backtester is the most valuable thing in this repo. The signal is a
hypothesis; this is what tells you whether it survives costs.

```bash
investassist backtest --file universe.txt              # headline metrics
investassist backtest --file universe.txt --sweep      # slippage sensitivity
investassist backtest --file universe.txt --walk-forward --folds 4
```

Properties it enforces, each because violating it produces a backtest that
looks wonderful and loses money:

- **No lookahead.** The score for bar *t* uses data through bar *t* only, and
  fills at bar *t+1*'s open.
- **Intrabar ambiguity resolves against you.** When a bar's range contains both
  the stop and a target, the stop wins. Daily bars can't tell you which came
  first.
- **Gaps are honoured.** A stop fills at the open when price gaps through it.
- **Costs get swept, not assumed.** `--sweep` reports edge across a range of
  slippage. An edge that only exists at zero slippage does not exist.
- **Walk-forward is embargoed.** Daily bars are autocorrelated and indicators
  carry state across the boundary, so a test window starting the day after
  training ends is not really out of sample.

Benchmark against buy-and-hold SPY. If it doesn't win after costs, the correct
output of this project is "don't trade this", and that's a genuinely useful
result. Under 30 trades, the CLI says so — a small sample tells you nothing.

**Multiple-testing discipline:** if you sweep hundreds of parameter
combinations the best one is almost certainly luck. Keep `--walk-forward` grids
small and prefer a broad plateau of stable parameters over a sharp peak.

---

## Parity between the two implementations

```bash
python tools/parity.py exports/AAPL_daily.csv --score-column "Score"
```

Export from thinkorswim with **right-click chart → Export → Export Data**.
Expect small differences — thinkorswim computes on its own adjusted data with a
different amount of warm-up history. Sustained divergence late in the series is
the real signal that something broke.

CI enforces everything upstream of the numbers: the generated block matches the
spec, all three studies carry an identical core, and thinkScript's constants are
exactly Python's. Those are the failure modes that actually happen.

The indicator layer matches thinkScript's conventions exactly, including the
ones that bite: `ExpAverage` seeds recursively (`adjust=False`),
`WildersAverage` uses `alpha = 1/n` not `2/(n+1)`, `StDev` is population, and
integer literals must be floats or thinkScript truncates `stack / 3` to zero.

---

## Layout

```
investassist/
  spec/default.yaml    single source of truth for every tunable
  spec.py              loading and validation
  indicators.py        thinkScript-compatible indicators
  score.py             L0 regime, L1 components, L2 composite, L3 exits
  risk.py              sizing and portfolio limits
  data.py              history providers and the bar cache
  storage.py           parquet with a CSV fallback
  rtd.py               the thinkorswim RTD COM bridge
  recorder.py          RTD quotes -> intraday bars
  symbolwatch.py       clipboard watcher and global hotkey
  panel.py             always-on-top companion window
  engine.py            service layer
  backtest.py          event-driven backtester and walk-forward
  thinkscript_gen.py   generates the shared thinkScript core
thinkscript/           the three studies (generated core + hand-written UI)
tools/parity.py        compares a thinkorswim export against Python
```

---

## Scope and limits

Stocks and ETFs only. Options and futures were deliberately dropped: the Schwab
API has no options price history to backtest against, and its order entry
doesn't accept futures at all.

**No orders are placed.** The engine recommends; you execute. Adding execution
means the Schwab Trader API, and its constraints are real — 30-minute access
tokens, a hard 7-day refresh expiry needing interactive login with 2FA, ~120
requests/minute, and no paper-trading sandbox. Don't add it until the backtest
says the signal is worth trading.

Distributing recommendations to other people moves toward investment-adviser
territory with real registration implications, and Schwab's market-data
agreement restricts redistribution. Neither is legal advice; both are worth
five minutes before this grows an audience.

See [PLAN.md](PLAN.md) for the full design rationale and the routes not taken.
