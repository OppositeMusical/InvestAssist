# InvestAssist — Implementation Plan

A buy/sell decision assist for **US stocks and ETFs** that interfaces with
thinkorswim and tells you what to do with **the stock you're currently looking
at**.

> **Status.** The recommended sequence in §7 is implemented — see
> [README.md](README.md) for setup and usage. Route 1 (the three thinkScript
> studies), Route 3 item 1 (clipboard watcher + hotkey + panel), Route 2 (the
> RTD bridge and recorder) and the validation harness are all in the repo with
> 104 passing tests. Route 5 (the Schwab API, for order placement) is
> deliberately not built: it should wait until the backtest says the signal is
> worth trading. The one component that still needs a verification pass is the
> RTD COM path, which cannot be exercised off Windows.

---

## 1. The central design problem

Everything else in this document follows from one constraint:

> **thinkorswim has no outbound API. It will not tell an external program which
> symbol is on your screen.**

thinkScript is sandboxed — no HTTP, no file I/O, no IPC. So the platform cannot
push its UI state anywhere. That leaves two fundamentally different shapes for
this app, and the routes below are really variations on which one you pick:

| | Where the verdict appears | Symbol context | Compute available |
|---|---|---|---|
| **On-chart** | Inside thinkorswim, as a study | **Free** — a study runs on whatever chart you open | Constrained to thinkScript |
| **External** | Companion app / panel | **Must be bridged** — the hard part | Unlimited |

The on-chart shape solves the "current stock" requirement perfectly and for
nothing, because `GetSymbol()` is simply whatever you're viewing. The external
shape gives you real backtesting, portfolio state, and any model you want, but
you have to solve symbol detection.

The best system uses both. That's the recommendation in §7.

---

## 2. Route 1 — thinkScript-native, on-chart assist

The verdict lives on the chart as a label: `BUY 72` / `HOLD 11` / `SELL −64`,
recomputed on whatever symbol you open. No servers, no API keys, no auth, no
data subscription. Real-time data is already included with your account.

**Deliverables**
- `score_study.ts` — composite score plotted as a label + signal arrows
- `watchlist_column.ts` — score as a custom quote column across your universe
  (20 custom-quote slots available; a custom-quote study must have exactly one plot)
- `scan.ts` — Stock Hacker query to surface candidates
- Study alerts → push/email when a symbol crosses into BUY or SELL

**What it can't do — structural, not effort**
- **No portfolio-level state.** thinkScript is bar-scoped and per-symbol. It
  cannot read your account equity, total open risk, or count day trades. A real
  risk layer (§6) is impossible here.
- **No rigorous backtest.** `AddOrder` is chart-only and backtest-only. No
  walk-forward, no purged splits, no cost-sensitivity curve.
- **No external data** — no fundamentals, earnings dates, or news.
- **Conditional orders are one-shot**, expire after firing, and reject complex
  scripts. Semi-automation at best; thinkorswim does not support unattended
  automated trading.
- Studies are stored **on Schwab's servers, not local disk**. Version control
  means manually exporting `.ts` files.

**Effort: 3–7 days.** Highest value-per-hour of anything in this document, and
it satisfies the "current stock" requirement on day one.

---

## 3. Route 2 — RTD bridge: thinkorswim as the data source, Python as the brain

thinkorswim ships an **RTD (Real-Time Data) COM server** on Windows — officially
documented by Schwab, still current in 2026. A running TOS instance streams live
quote data to any local program that asks. This is the integration surface most
people don't know exists, and it sidesteps the entire Schwab API stack: **no
OAuth, no developer app, no approval wait, no 7-day re-auth, no data bill.**

**How it works**
- Python reads RTD over COM via `pywin32`. (`TOSDataBridge` wraps the older DDE
  path with C/C++/Java/Python interfaces and a TCP layer for non-Windows
  clients — useful reference implementation either way.)
- Crucially, thinkorswim exports **custom RTD fields driven by thinkScript**, so
  a value your on-chart study computes can be read directly by your Python
  engine. That is a genuine two-way bridge and the thing that makes Routes 1 and
  2 one system rather than two.

**Constraints**
- Windows only; thinkorswim must be running and logged in.
- Run one TOS instance and ideally one RTD consumer — Schwab's own docs warn
  about multiple concurrent consumers.
- It is a **quote stream, not a history API.** You specify symbols and receive
  live fields. Historical bars must be recorded by you or sourced elsewhere.
- It does not know your active chart symbol — that's Route 3.

**Effort: 2–4 weeks.**

---

## 4. Route 3 — The symbol-context bridge

This is what turns Route 2 into "the stock I'm looking at." Ranked by
robustness, and all of them are **read-only** — categorically different from
UI-automating order entry, which remains a bad idea.

1. **Hotkey + clipboard** — you press a hotkey, the app reads the clipboard,
   validates it against a ticker list, and scores it. Never breaks, works on any
   layout, ~40 lines of code. **Build this first.** It covers the real workflow
   more than it sounds like it does.
2. **Clipboard watcher** — same thing, but polls continuously so the panel
   follows along as you copy symbols. No hotkey needed.
3. **Window title** — detached TOS chart windows often carry the symbol in the
   title. Enumerate with `pywin32`. Free if true on your setup; a one-hour spike
   settles it.
4. **OCR of the symbol field** — screenshot a fixed region, run Tesseract. Short
   uppercase tickers in a fixed font is near-best-case for OCR. Self-contained,
   but breaks when you move panels.
5. **Java Access Bridge** — thinkorswim is Java/Swing, and JAB exposes the
   accessibility tree on Windows. The most "correct" approach and the flakiest
   in practice.

Start at 1, treat 3 as a cheap spike, and only reach for 4–5 if the workflow
genuinely demands hands-free.

---

## 5. Routes 4 and 5 — the alternatives, briefly

**Route 4 — third-party data, TOS purely as your chart.** Python engine on
Polygon (~$29–199/mo, full SIP), Databento (pay-as-you-go, excellent history),
or Tiingo. Best analysis quality and backtest rigor, cleanest engineering, and
the weakest "interfaces with thinkorswim" story — you look at TOS, you glance at
the app. Symbol entry via Route 3.

**Route 5 — the Schwab Trader API.** Worth reconsidering now that scope is
stocks-only, because that removes two of the three constraints that made it
awkward before: equities have **full price history** (options don't), and equity
**order entry is supported** (futures isn't), so a real one-click-confirm loop
works. What remains is the 30-minute access token, the **hard 7-day refresh
expiry** requiring interactive login with 2FA, ~120 req/min limits, and no paper
sandbox. Add this when you want the app to place orders, not before.

**Considered and rejected: thinkorswim web + browser extension.** A browser
extension would make symbol detection trivial — read it from the DOM. But **the
web version does not support custom scripts or custom indicators at all**, so
you'd forfeit the entire on-chart layer. Not worth it.

---

## 6. What the engine computes (Routes 2–5)

Layered so each piece is independently testable.

**L0 — Regime gate.** Is this a tape worth trading? SPY vs its 200-day, VIX
level, breadth. In thinkScript this is reachable via `close("SPY")` references.
Output gates everything downstream.

**L1 — Features.** Trend (EMA stack, ADX, Donchian), momentum (RSI, ROC, MACD),
mean-reversion (Bollinger z-score, RSI(2)), volatility (ATR, realized vol),
volume (relative volume vs a 20-day profile, OBV), and location (distance to
52-week high, prior-day levels, VWAP).

**L2 — Composite score,** −100…+100 plus a confidence, mapped to
`STRONG BUY / BUY / HOLD / TRIM / SELL / STRONG SELL`. Keep it **rule-based and
transparent** — weighted contributions you can read off the panel — because an
opaque score is one you'll override at exactly the wrong moment. ML only after
the rule version has a measured baseline.

**L3 — Exit logic, with equal billing.** Most retail systems have a buy rule and
no sell rule, and "should I sell this" is half your question. Every position
carries an initial stop (`entry − k×ATR` or structure), a trailing stop that
activates at +1R, scale-out levels, a time stop, and a thesis-invalidation exit
when L2 crosses back through zero.

**Risk layer** (external routes only): fixed-fractional sizing
`shares = floor(equity × risk_pct / (entry − stop))`, a daily loss limit, PDT
day-trade counting if under $25k, a portfolio heat cap, and a correlation guard.

**Validation.** Decide on bar close, fill at next bar open, model slippage, and
produce a cost-sensitivity curve — if the edge dies at half a cent of extra
slippage it was never there. Walk-forward with purged splits. Benchmark against
buy-and-hold SPY; if it doesn't win after costs, "don't trade this" is the
correct and valuable output.

---

## 7. Recommended path

**Phase 1 — Route 1, one week.** Build the thinkScript study, watchlist column,
scan, and alerts. You immediately get an on-chart verdict for whatever symbol
you're viewing, with zero infrastructure. This alone may be most of what you
wanted.

**Phase 2 — Route 3 item 1, one day.** Hotkey + clipboard, plus a minimal
always-on-top panel. Now you have somewhere to put analysis thinkScript can't do.

**Phase 3 — Route 2, two to four weeks.** RTD bridge feeding a Python engine.
Record everything from day one — that recorded tape becomes your backtest data.
Port L1/L2 to Python and add what thinkScript can't reach: fundamentals,
earnings proximity, sector relative strength, portfolio state.

**Phase 4 — validation.** Backtest harness, walk-forward, cost sensitivity. This
is a real go/no-go gate on whether the scoring model is worth trusting.

**Phase 5 — Route 5, optional.** Add the Schwab API when you want one-click
order placement rather than recommendations.

Keep the two implementations honest with a **parity test**: put every parameter
in `signals/spec/*.yaml`, generate the thinkScript constants from it, export a
day of study values from TOS, and assert they match the Python output within
tolerance. Two implementations that silently drift are worse than one.

---

## 8. Worth knowing

RTD's officially-supported status is the quiet win here — it gives you a
sanctioned local integration with thinkorswim that needs no credentials and no
approval, and it is the reason Routes 1 and 2 compose into a single system
instead of two disconnected ones.

If you add an LLM to write the "why" narrative, the numbers must come from
deterministic code and the model may only phrase them. A model that invents a
price level is a liability.

Two boundaries: this is a personal decision-support tool, and distributing
recommendations to others moves toward investment-adviser territory with real
registration implications. Schwab's market-data agreement also restricts
redistribution — keep the data to yourself. Neither is legal advice; both are
worth five minutes before the project grows an audience.

---

## Sources

- [Real-Time Data (RTD) Function for thinkorswim — Schwab](https://toslc.thinkorswim.com/center/howToTos/thinkManual/Miscellaneous/Real-Time-Data-%28RTD%29-Function-for-thinkorswim.html) · [RTD setup guide](https://marketxls.com/blog/thinkorswim-rtd-excel) · [Hahn-Tech RTD notes](https://www.hahn-tech.com/thinkorswim-thinkorswim-rtd-excel/)
- [TOSDataBridge — DDE/RTD extraction, C/C++/Java/Python](https://github.com/jeog/TOSDataBridge)
- [thinkScript reference](https://toslc.thinkorswim.com/center/reference/thinkScript) · [Study Alerts](https://toslc.thinkorswim.com/center/howToTos/thinkManual/MarketWatch/Alerts/studyalerts) · [Custom Quotes](https://toslc.thinkorswim.com/center/howToTos/thinkManual/MarketWatch/Quotes/customquotes) · [thinkScript in Conditional Orders](https://toslc.thinkorswim.com/center/howToTos/thinkManual/Trade/Order-Entry-Tools/Order-Types/thinkScript-in-Conditional-Orders)
- [ThinkorSwim Web vs. Desktop](https://thinkscript101.com/thinkorswim-web-vs-desktop/) — custom scripts unsupported on web
- [Java Access Bridge overview](https://docs.oracle.com/en/java/javase/20/access/java-access-bridge-overview.html)
- [schwab-py docs](https://schwab-py.readthedocs.io/en/latest/auth.html) · [Schwab Trader API overview](https://grokipedia.com/page/Schwab_Trader_API)
