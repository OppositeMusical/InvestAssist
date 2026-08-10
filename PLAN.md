# InvestAssist — Implementation Plan

An intraday buy/sell decision-support system for thinkorswim, built as a Python
engine against the Schwab Trader API with a thinkScript mirror for on-chart
confirmation.

**Chosen configuration**

| Decision | Choice |
|---|---|
| Horizon | Intraday (minutes–hours) |
| Instruments | Stocks & ETFs, Options, Futures |
| Automation | Recommend + one-click confirm |
| Build target | Hybrid — Python engine + thinkScript mirror |

---

## 1. Platform reality check

thinkorswim has no API of its own. Since the TD Ameritrade migration, all
programmatic access runs through the **Schwab Trader API**
(`developer.schwab.com`), and every legacy TDA key has been sunset. Four
constraints from that API drive most of the architecture below, so they belong
at the top rather than buried in a phase.

**1. Futures can be analyzed but not traded.** Order entry accepts equity and
option asset types only; a futures order returns `400 Unsupported instrument`.
Futures market data *is* available — `LEVELONE_FUTURES` quotes and
`CHART_FUTURES` minute bars — so /ES and /NQ work fine as regime context and as
signal sources. But the one-click confirm path cannot include them. Futures
recommendations are advisory: the engine tells you what it sees, you execute in
thinkorswim by hand. Plan accordingly, or route futures through a broker with an
order API (Tradovate, IBKR) later if hands-off futures matters.

**2. There is no options price history.** The price-history endpoint covers
equities and ETFs only. Option chains come with live Greeks and IV, and
`LEVELONE_OPTIONS` streams real-time quotes, but nothing gives you historical
option bars. Consequences: you cannot backtest an options strategy on Schwab
data. Three ways out, and Phase 1 should start doing the first one immediately
regardless of which you pick:

- Record your own tape from day one — every chain snapshot and streamed quote
  you persist becomes backtestable history later. Cheap, but you wait months.
- Trade options off *underlying* signals and model the option leg with
  Black-Scholes using the observed IV surface. Backtestable now, approximate.
- Buy vendor history (Polygon, Databento, CBOE DataShop). Fastest to rigor,
  costs money.

**3. There is no paper-trading sandbox.** The API is live-only. Forward testing
has to run through a `PaperBroker` you write yourself, filling against live
streaming quotes with a modeled spread and slippage. This is not optional
scaffolding — it is the only evidence you will have before risking capital, and
it is what makes the "recommend + one-click" gate meaningful.

**4. Auth expires aggressively.** Access tokens last 30 minutes; the refresh
token dies after **7 days, hard**, requiring an interactive browser login with
2FA. For an intraday system this is the single largest operational risk: a token
that dies at 10:15 AM takes your exit management with it. Section 6 covers the
mitigation.

Also worth knowing: rate limits run ~120 requests/minute (429-001 for rate,
429-005 for burst, back off 60s), which is why streaming rather than polling is
mandatory intraday. App registration requires a developer account separate from
your brokerage login, and approval for the two products — *Market Data
Production* and *Accounts and Trading Production* — takes days, so start it
before you write any code.

---

## 2. What the system actually does

Four questions, answered continuously during the session:

1. **What should I buy right now, and why?** Ranked candidates with entry zone,
   stop, target, size, and a plain-language rationale.
2. **What should I do with what I already hold?** Position-aware exits — current
   R-multiple, distance to stop, whether the original thesis is still intact.
   Most retail systems have a buy rule and no sell rule; this half is the one
   that decides your P&L.
3. **Should I be trading at all today?** A regime gate that can say "no."
4. **What did that cost me?** Every recommendation logged and scored, so the
   system's own hit rate is measurable rather than remembered.

---

## 3. Architecture

```
                    ┌──────────────────────────────────────┐
                    │      Schwab Trader API               │
                    │  REST: history, chains, accounts,    │
                    │        orders, movers, market hours  │
                    │  WS:   LEVELONE_{EQUITIES,OPTIONS,   │
                    │        FUTURES}, CHART_{EQUITY,      │
                    │        FUTURES}, ACCT_ACTIVITY       │
                    └───────────────┬──────────────────────┘
                                    │
        ┌───────────────────────────┴────────────────────────┐
        │                    ingest/                         │
        │  token daemon · streamer client · REST client      │
        │  bar builder (1m→5m→15m→1h) · corporate actions    │
        └───────────────────────────┬────────────────────────┘
                                    │
                    ┌───────────────▼───────────────┐
                    │   store/  DuckDB + Parquet    │
                    │   bars · chains · ticks ·     │
                    │   recommendations · fills     │
                    └───────────────┬───────────────┘
                                    │
        ┌───────────────────────────▼────────────────────────┐
        │                    signals/                        │
        │  L0 regime gate → L1 features → L2 score → L3 exit │
        └───────────────────────────┬────────────────────────┘
                                    │
        ┌───────────────────────────▼────────────────────────┐
        │  risk/  sizing · daily loss limit · PDT · heat      │
        └───────────────────────────┬────────────────────────┘
                                    │
        ┌─────────────┬─────────────┴──────────┬─────────────┐
        │  dashboard  │   push (approve/deny)  │  thinkScript│
        │  FastAPI    │   Pushover/Telegram    │  study+scan │
        └─────────────┴─────────────┬──────────┘─────────────┘
                                    │ user taps Approve
                    ┌───────────────▼───────────────┐
                    │  execution/  ticket → order   │
                    │  kill switch · audit log      │
                    └───────────────────────────────┘
```

### Suggested layout

```
investassist/
  ingest/      auth.py streamer.py rest.py bars.py calendar.py
  store/       schema.sql writer.py reader.py
  signals/     regime.py features.py score.py exits.py
               spec/*.yaml          # single source of truth for parameters
  options/     chain.py select.py greeks.py
  risk/        sizing.py limits.py pdt.py
  execution/   ticket.py broker.py paper.py audit.py
  api/         app.py routes/ ws.py
  backtest/    engine.py costs.py walkforward.py metrics.py
  thinkscript/ score_study.ts watchlist_column.ts scan.ts parity_export.ts
  tests/
```

**Stack:** Python 3.12, `uv`, `schwab-py` (handles OAuth and the streamer),
`httpx`, `polars` or `pandas`, DuckDB + Parquet, FastAPI, APScheduler,
`pydantic-settings`, `structlog`, `pytest`. Frontend can stay boring — HTMX or a
small React page. Deploy on a machine that is reliably up 9:30–16:00 ET; a $10
VPS with systemd beats your laptop lid.

---

## 4. Signal design

Layered, so each piece is independently testable and independently
falsifiable.

### L0 — Regime gate (per session, re-evaluated every 15m)

Decides whether to trade at all and which *style* of rule applies. Inputs: /ES
and /NQ trend and overnight range, VIX level and term structure, gap size vs
ATR, opening-range width, index breadth. Output: `TREND_UP | TREND_DOWN |
BALANCE | AVOID`. Mean-reversion rules fire only in `BALANCE`; breakout rules
only in trend regimes; `AVOID` blocks new entries entirely (FOMC 2:00 PM, CPI
open, halted-heavy tape). This gate is what stops a chop-day from
death-by-a-thousand-stops.

### L1 — Per-symbol features (1m / 5m / 15m)

Computed on every completed bar: session VWAP with standard-deviation bands,
anchored VWAP from open and from prior HOD/LOD, opening-range high/low, prior
day H/L/C, EMA stack (9/21/50), ATR(14), RSI(2) and RSI(14), MACD, Donchian
channels, and — the one people skip — **relative volume against a 20-day
same-time-of-day profile**, because 500k shares at 9:45 and at 14:45 mean
completely different things.

### L2 — Composite score

Rather than a binary crossover, emit a score in −100…+100 plus a confidence,
mapped to `STRONG BUY / BUY / HOLD / TRIM / SELL / STRONG SELL`. Start
**rule-based and transparent** — weighted contributions you can read off the
dashboard — because an opaque score you don't trust is a score you'll override
at exactly the wrong moment. Only after the rule version has a measured
baseline should you consider a gradient-boosted classifier predicting
`P(+1R before −1R within N bars)`, labeled with the triple-barrier method.

### L3 — Exit engine

Equal billing with entries. Every open position carries: initial stop
(`entry − k×ATR` or structure-based, whichever is tighter), a chandelier
trailing stop that activates at +1R, scale-out at +1R / +2R, a time stop
(flatten by 15:50 ET), and a thesis-invalidation exit when the L2 score crosses
back through zero. The dashboard shows all five distances at once for every
position you hold.

### Options layer

Since there's no history to optimize against, contract selection is a
**live-chain filter, not a model**: delta 0.55–0.70 for directional trades,
DTE window matched to holding time (0–2 DTE intraday is a different risk
animal — decide deliberately), minimum open interest and volume, maximum spread
as a percentage of mid, and an IV-rank check so you're not buying premium into
an earnings crush. Critically: **stops trigger off the underlying's price, not
the option's** — option quotes gap and widen in ways that will stop you out on
noise.

### Futures layer

Signals and context only, per §1. The engine emits the recommendation; you
execute manually in thinkorswim.

---

## 5. Risk management

Non-negotiable, and cheaper to build now than to retrofit after a bad week:

- **Fixed-fractional sizing:** `shares = floor(equity × risk_pct / (entry − stop))`,
  with `risk_pct` starting at 0.25–0.5% intraday.
- **Daily loss limit:** at −2R on the day, the engine stops issuing buy tickets
  and says so. This single rule prevents more damage than any indicator adds.
- **PDT compliance:** under $25k equity you get 3 day trades per rolling 5
  business days. The engine must count them and refuse to stage a ticket that
  would breach it — an intraday system on a small account will hit this in week
  one otherwise.
- **Portfolio heat cap:** total open risk ≤ 2R; max concurrent positions ≤ 4.
- **Correlation guard:** reject a 5th long when four are already in the same
  sector or all beta-loaded to /ES.

---

## 6. Auth resilience

Given the 30-minute / 7-day token structure, treat auth as a first-class
subsystem:

- Background task refreshes the access token every ~25 minutes.
- Token store on disk with restrictive permissions, never in the repo.
- **Health monitor:** refresh token age > 5 days triggers a daily push
  reminder; > 6.5 days triggers an hourly one. A failed refresh immediately
  pushes an alert and flips the system to read-only rather than failing silent.
- **Sunday re-auth ritual:** a `make reauth` target that spins up the local
  callback listener on `https://127.0.0.1:8182`, opens the browser, and
  completes the flow in under a minute. Automate everything except the login
  and 2FA, which Schwab will always make you do by hand.
- Degrade loudly: if data goes stale, the dashboard shows a banner and exit
  management switches to "manual — check thinkorswim," because a silently
  frozen exit engine is worse than no exit engine.

---

## 7. Validation — the part that decides whether any of this is worth running

Build the backtest harness before you trust a single signal.

**Avoid lookahead:** decide on bar close, fill at next bar open, plus modeled
slippage. **Model costs honestly** — equity commissions are ~0 but the spread
isn't, and intraday it dominates. Produce a **cost-sensitivity curve** showing
edge vs assumed slippage; if the strategy dies at half a cent of extra
slippage, it was never real. **Walk-forward** with purged and embargoed splits,
since intraday bars are heavily autocorrelated and a naive split leaks.

Metrics that matter: expectancy in R, profit factor, hit rate, average hold
time, trades per day, max drawdown, Sharpe — and always benchmarked against
buy-and-hold SPY. If it doesn't beat SPY after costs, the correct output of this
project is "don't trade this," and that is a genuinely valuable result.

**Multiple-testing discipline:** if you sweep 500 parameter combinations, the
best one is almost certainly luck. Cap the search, and prefer a broad plateau of
mediocre-but-stable parameters over a sharp peak.

**Then forward paper-trade** through the `PaperBroker` against live streaming
quotes for 4–8 weeks minimum before enabling one-click confirm. This is where
most of the honest information comes from.

---

## 8. Execution flow (recommend + one-click confirm)

1. Signal fires → engine builds an `OrderTicket` (validated, priced, sized).
2. Pre-trade checks: buying power, PDT count, daily loss limit, position not
   already open, symbol not halted, spread within tolerance, data fresh.
3. Ticket surfaces on the dashboard and as a push notification with Approve /
   Deny.
4. **Ticket TTL of 60–90 seconds**, then auto-expire and re-price. Intraday, a
   stale ticket is a bad fill.
5. On approve → `POST /accounts/{hash}/orders` with an idempotency key and a
   duplicate-submit guard.
6. Track the fill via the `ACCT_ACTIVITY` stream; hand the position to the exit
   engine.
7. Everything — proposed, approved, denied, expired, filled — lands in an audit
   table.

**Kill switch:** one dashboard button and one flag file, either of which blocks
all submissions immediately. Test it before you need it.

---

## 9. thinkScript mirror

The Python engine is the brain; thinkScript makes it visible where you already
look. Build four artifacts:

- **`score_study.ts`** — plots the L2 score and signal arrows on your charts.
- **`watchlist_column.ts`** — score as a custom quote column (note: thinkorswim
  allows 20 custom quote slots, and a custom-quote study must have exactly one
  plot).
- **`scan.ts`** — Stock Hacker query producing the candidate universe.
- **Study alerts** as an independent backstop, so a signal still reaches you if
  the Python service is down.

**Keep them honest with a parity test.** Put every parameter in
`signals/spec/*.yaml`, generate the thinkScript constants from it, export a
sample day's study values from thinkorswim, and assert they match the Python
output within tolerance in CI. Two implementations that silently drift are worse
than one. Known thinkScript limits to design around: no HTTP or external data,
no cross-session persistence, no portfolio-level state, and repaint risk on
secondary aggregations.

---

## 10. Phasing

| Phase | Work | Est. |
|---|---|---|
| 0 | Developer account, register app for both products, wait for approval, OAuth + token daemon working, read positions | Week 1 |
| 1 | Data layer: REST backfill, streamer ingest, bar builder, market calendar, **start recording option chains immediately** | Week 2 |
| 2 | Feature library + L0/L1/L2, recommend-only output to console/CSV | Weeks 3–4 |
| 3 | Backtest harness, walk-forward, cost sensitivity → **go/no-go on the signal** | Weeks 5–6 |
| 4 | Risk manager + L3 exit engine + position-aware "what do I hold" report | Week 7 |
| 5 | Dashboard, push notifications, ticket lifecycle, `PaperBroker` forward test begins | Week 8 |
| 6 | Paper forward test running; build thinkScript mirror + parity test; options chain selector | Weeks 9–16 |
| 7 | Enable one-click confirm — smallest size, daily loss limit on, kill switch tested | Week 17+ |

First genuinely useful output — a ranked recommend-only list — lands around week
4. A one-click system you have real evidence for is a ~4-month project of
evenings. Phase 3 is a real gate, not a formality: if the numbers don't clear
costs, stop there.

---

## 11. Things worth knowing before you start

Intraday retail edge is genuinely hard, and transaction costs eat most published
indicator combinations. The value of building this properly is that it *measures*
whether your ideas work instead of leaving it to memory, which reliably flatters
past decisions. The backtest harness in Phase 3 is the most valuable component in
the repo, not the signal.

If you add an LLM layer to write the "why" narrative, the numbers must come from
deterministic code and the model may only phrase them. A model that invents a
price level is a liability.

Two boundaries to keep in view: this is a personal decision-support tool, and
distributing recommendations to others moves you toward investment-adviser
territory with real registration implications. Separately, Schwab's market-data
agreement restricts redistribution — keep the data to yourself. Neither is legal
advice; both are worth five minutes of reading before the project grows an
audience.

---

## Sources

- [Does thinkorswim Have an API? — Schwab Migration Guide](https://www.aurascience.blog/does-thinkorswim-have-an-api)
- [The (Unofficial) Guide to Charles Schwab's Trader APIs](https://medium.com/@carstensavage/the-unofficial-guide-to-charles-schwabs-trader-apis-14c1f5bc1d57)
- [Schwab Trader API overview](https://grokipedia.com/page/Schwab_Trader_API)
- [schwab-py — Authentication](https://schwab-py.readthedocs.io/en/latest/auth.html) · [HTTP Client](https://schwab-py.readthedocs.io/en/latest/client.html) · [Streaming Client](https://schwab-py.readthedocs.io/en/latest/streaming.html)
- [Lumibot — Schwab broker notes](https://lumibot.lumiwealth.com/brokers.schwab.html)
- [Schwab API for Traders: Costs and Real Limits](https://mylinedchart.com/resources/articles/schwab-api-for-technical-traders-workflow-fit-checklist)
- [thinkScript reference](https://toslc.thinkorswim.com/center/reference/thinkScript) · [Study Alerts](https://toslc.thinkorswim.com/center/howToTos/thinkManual/MarketWatch/Alerts/studyalerts) · [thinkScript in Conditional Orders](https://toslc.thinkorswim.com/center/howToTos/thinkManual/Trade/Order-Entry-Tools/Order-Types/thinkScript-in-Conditional-Orders) · [Custom Quotes](https://toslc.thinkorswim.com/center/howToTos/thinkManual/MarketWatch/Quotes/customquotes)
