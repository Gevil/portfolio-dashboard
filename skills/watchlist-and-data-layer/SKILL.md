---
name: watchlist-and-data-layer
description: Add/remove/change a ticker or holding, set share count or cost basis, add a benchmark, and reason about prices, listing series, providers, FX and stored history. Use for anything touching registry.py, prices.py, live_ws.py, forex.py, portfolio.py or config/config.json.
---

# Watchlist and data layer

## Model (read `app/api/registry.py` docstring first)
Each watchlist entry: `{id, symbol, label, kind: equity|etf|index, role: holding|benchmark,
quoteCurrency, providers:{yahoo,twelvedata,finnhub}, listing:{symbol,venue,currency}, alertable, analyzeable}`.
- `providers.<x> = null` means **unsupported**: `registry.provider_symbol(id, "x")` returns `None` and
  callers **skip** — no request, no negative-cache entry, no fallback to the raw id.
- `listing` is the single venue/currency whose series drives valuation, charts and stored history.
  Venue tz/region come from the `VENUES` table (MIC → tz/open/close); **sessions, holidays and early closes come from
  `app/api/calendars.py`** (`exchange_calendars`, XAMS/XETR/XNYS/XNAS/XPAR/XBRU/XMIL). The registry's market-hours
  helpers (open now, session window) and the `macro` trading-day helpers delegate to it. `calendars.mic_for(id)` gives the listing's
  MIC (`.DE` → XETR, `.AS` → XAMS, index/unknown → XNYS). An unknown MIC is XNYS with one warning; dates outside the
  library's range degrade to Mon–Fri with one warning. Never hand-roll weekday/holiday logic.
- Defaults by kind: equity → alertable + analyzeable; etf → alertable only; index → neither.
  Benchmarks (`role: benchmark`) are never in the portfolio, never alerted, never analysed.
- `normalize_entry()` raises `ValueError` with a readable message; it **preserves unknown keys** so a UI
  save never drops flags.

## Add a holding (e.g. a new EU-listed stock)
1. Confirm the Yahoo listing: `yf.Ticker("XXX.DE").history(period="5d")` returns rows and
   `fast_info.currency == "EUR"` (run it inside the pod: `podman exec portfolio-dashboard python -c ...`).
   Index/ETF symbols on Yahoo differ by venue (`SXR8.DE` Xetra EUR, `CSPX.L` LSE USD, `CSPX.AS` EUR).
2. Add the entry in **Settings → Watchlist** (validated server-side, 400 with a message on error) or via
   `PUT /api/config`. If you edit `config/config.json` by hand, do it with the pod stopped or through the API —
   the app re-reads on mtime change but a half-written file is the risk; `config_store` keeps a last-good copy.
3. Add the position in **Settings → Positions**: `shares` (> 0) and `investedAmount` = EUR cost basis
   (`null` = unknown → valued, excluded from P/L, warning `cost_missing`). A position id must be a
   `holding` on the watchlist; removing a watchlist entry that still has a position is rejected (400)
   so cost basis is never silently dropped. Optionally give dated `lots` instead (see "Lots and the ledger").
4. New tickers trigger history seeding automatically on save. Check
   `GET /api/history/<id>?range=1M` and `GET /api/portfolio`.

## Prices and history rules
- **One series per holding** (EUR listing). The Twelve Data / Finnhub WebSocket feeds only populate
  `usLive` (labelled USD reference) and must never be written into the listing series.
- History store: age-based retention (≤24 h 1-min, ≤7 d 5-min, ≤365 d daily); whole-file writes are
  throttled and run in an executor; flush on shutdown. Files: `data/listing_history.json`, `valuation.json`.
- Valuation/fundamentals negative-cache uses its **own key**; never share it with the quote/history key
  (a "no valuation for an index" result once blocked the index's quotes).
- Seed/history/indicator endpoints validate input (range/interval whitelist, symbol must be watched and
  supported) and answer 400, not 500.
- Periodic tasks run under `_supervise()` (logs and restarts on any exception); do not add bare
  `create_task` loops in `main.py`.

## FX
`forex.get_rate_usd_eur()` → `{rate, source, asOf, stale}`; chain Frankfurter → Yahoo `EURUSD=X` →
cached. `stale` (> ~24 h) rates must **not** feed persisted conversions; `/api/forex/USD/EUR` returns
`rate: null` when nothing valid exists (never a made-up number). `forex.daily_rates(days)` gives EUR per USD
per reference day for converting the benchmark bar-by-bar (weekends absent → carry the last rate).

## Portfolio calculation (`portfolio.py`)
`snapshot()` (async, ≤30 s cache, invalidated on config change) → totals, positions, benchmark, fx,
warnings. `position_for(id)` is **sync** and reads that cache — async callers `await snapshot()` first.
Warning codes: `cost_missing`, `stale_price`, `fx_stale`. Every number carries `priceAsOf`/`priceSource`/`stale`.
`/api/portfolio/history?range=1M|3M|6M|1Y|ALL` indexes portfolio, the EUR-converted role benchmark and the performance
benchmark to 0 % at the first common date.

## prevClose (the F0 fix)
`prices._prev_close` takes the close of `calendars.previous_session(mic, calendars.quote_session(mic, now))`, computed
from the clock and the venue calendar, **never from the newest bar's date** (at the 09:00–09:15 open the 1d fetch is
empty and the 15m fallback ends yesterday; the old code then used a close two sessions old and fired false gap alerts).
Chain: that exact date's daily row (`5d`, then one retry with `1mo`); else the close of the last intraday bar of that
session (stored listing history, then a 5d/15m fetch); else `None`. `listing_quote()` carries `prevClose` and
`prevCloseSource` (`daily` | `intraday` | null). The cache key is `(id, expected date)`; a `None` is cached for 300 s
only. Consumers must treat `prevClose is None` as "unknown" (the gap alert is skipped, not guessed).
`prices.daily_closes(id, period)` returns raw unadjusted `(local ISO date, close)` rows (cached 6 h, never raises,
never writes the listing store); σ, the scoreboard baselines, the policy trend filter and the `ALL` range use it.

## Lots and the ledger (`performance.py`, `risk.py`, `config_edit.py`)
- `portfolio[id].lots = [{date, shares, priceEur, feesEur, side}]` (validated in `config_edit`: date not in the future,
  `shares > 0`, `priceEur > 0`, `feesEur >= 0`, `side` buy|sell, a sell may not exceed the shares held on its date).
  With lots the server derives and stores `shares` (Σbuy − Σsell) and `investedAmount` (FIFO cost of the remaining
  shares, buy fees included). A plain save without a `lots` key keeps them; `"lots": []` drops them.
- `GET /api/portfolio/history` adds `basis` (`lots` | `current_shares`, warning `history_basis_current_shares`),
  `perfBenchmark`, `indexed.perfBenchmark` and, with basis `lots`, `invested` and `twr`.
- `GET /api/portfolio/performance?range=`: `twrPct`, `xirrPct` (null without dated lots, warning
  `xirr_needs_dated_lots`), `perfBenchmarkPct`, `excessPct`, `netFlowsEur`. TWR links daily returns with flows at
  day end; XIRR is bisection with a sign check (no sign change → null, never a guess).
- `GET /api/portfolio/attribution?range=`: per holding `eurReturnPct`, `localReturnPct`, `fxReturnPct`, `crossPct`
  (local = (1 + eur) / (1 + fx) − 1 against `exposureCurrency`, default the listing currency; EUR → fx 0).
- `GET /api/portfolio/risk`: direct / look-through / currency exposure, 1Y volatility, beta, drawdown on the TWR index,
  correlation, concentration. Look-through = direct weight + Σ fund weight × `lookThrough[fund].weights[id]` / 100.
- Config keys (all optional; ids are checked against the same update's watchlist): `performanceBenchmark`,
  `exposureCurrency`, `lookThrough`, `investmentPolicy`, `taxResidency` (`CZ`/`NL`), `taxNL`; examples in `README.md`.
  `performanceBenchmark` kind is `total_return` for an ETF entry, else `price`; unset = the role benchmark.
- `taxResidency` `NL` routes `GET /api/tax` to `app/api/tax_nl.py` (box 3 forfait vs tegenbewijs from the lots, the raw
  `prices.daily_closes` 31 Dec closes and `events.dividend_history`; inputs in `taxNL`). Positions without lots use current
  shares for the current year only (`history_basis_current_shares`). Parameters live in `tax_nl.PARAMS` (spec:
  `docs/research/nl-tax.md`); `tax.sell_tax_status` returns `taxable: None` + `statusText` for NL, so the digest has no tax tag.
- `ALL` uses `daily_closes` (period chosen from the oldest lot); without any dated lot it is limited to 5 years.

## Tests to run after changes
`tests/unit/test_registry.py test_calendars.py test_prices.py test_forex.py test_live_ws.py test_portfolio.py test_config_edit.py
test_config_store.py` (see `skills/testing`).
