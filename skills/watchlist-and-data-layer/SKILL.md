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
  Venue hours come from the `VENUES` table (MIC → tz/open/close). Holidays are not modelled.
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
   so cost basis is never silently dropped.
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
`/api/portfolio/history` indexes portfolio and the EUR-converted benchmark to 0 % at the first common date.

## Tests to run after changes
`tests/unit/test_registry.py test_prices.py test_forex.py test_live_ws.py test_portfolio.py test_config_edit.py
test_config_store.py` (see `skills/testing`).
