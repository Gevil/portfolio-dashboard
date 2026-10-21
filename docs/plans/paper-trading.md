# Plan: AI paper trading → small live bucket (Trading 212)

Status (2026-10-21): **Phases 1–6 deployed, Trading 212 DEMO only.** Open universe (124 EUR names) active: daily bucket
`s2` and intraday bucket `i2` (hourly) in mode **demo**, benchmark buy-and-hold MSCI World (EUNL) with SXR8 and the owner's
real weights as secondary; season `s1` (3 tickers) closed and kept. Forecaster experiment per
[`forecaster.md`](forecaster.md); first weekly cohort at the Friday close. Live trading is **out of scope** (owner rule).
· Owner: portfolio owner · Written: 2026-10-19

## 1. Goal

Run an AI agent that manages a separate EUR bucket for at least a few months, first on the
Trading 212 **practice (demo)** account, later optionally on a **live €1,000 bucket**, and measure
honestly whether it beats doing nothing with the owner's real portfolio.

Non-goals:
- The agent never trades the real portfolio. That lives with a bank-run investment service with
  no trading API; the dashboard keeps tracking it read-only from `config/config.json`.
- No intraday/high-frequency trading, no shorts, no margin, no CFDs, no options, no crypto.
- No tax logic for the paper buckets (the owner handles taxes separately).

## 2. Decisions already made

| Topic | Decision | Why |
|---|---|---|
| Broker | Trading 212 (Invest account) | EUR account, Euronext/Xetra listings, €0 commission, same API for demo and live (`demo.` / `live.trading212.com/api/v0`) |
| Rejected | Alpaca (US/USD only, no EU listings, paper results would not transfer); IBKR for now (€1.25 minimum per order eats a €1,000 bucket); DEGIRO (no API); the owner's bank (no trading API) | |
| Fallback | IBKR behind the same broker interface if the live T212 API turns out unavailable | |
| Credentials | `T212_DEMO_KEY`, `T212_DEMO_SECRET` in `env.secrets`, key IP-restricted to the host | done |
| Bucket size | €1,000 enforced in code, also in demo (demo account holds €5,000; €4,000 stays unused) | demo behaves like the future live bucket |

## 3. Verified facts (2026-10-19, demo key, read-only)

- `GET /equity/account/summary` → 200, currency EUR, total €5,000 cash, no positions.
- Instrument mapping (ISIN-checked, all EUR, same venues the dashboard already tracks):

| Dashboard id | Dashboard listing | T212 ticker | Venue (T212 working schedule) |
|---|---|---|---|
| ASML | `ASML.AS` | `ASMLa_EQ` | Euronext Amsterdam (57) |
| NVDA | `NVD.DE` | `NVDd_EQ` | Deutsche Börse Xetra (54) |
| SXR8 | `SXR8.DE` | `SXR8d_EQ` | Deutsche Börse Xetra (166) |

- API constraints from the T212 docs that shape the design:
  - **Beta** API; may change → isolate behind an adapter + contract tests.
  - Order endpoints are **not idempotent** and there is no client order id → never blind-retry (§6.3).
  - Orders by **quantity only** (fractional allowed), not by value.
  - Rate limits: account summary 1 req/5 s, instruments 1 req/50 s, exchanges 1 req/30 s,
    positions 1 req/1 s, limit order 1 req/2 s, history 20 req/min; max 50 pending orders per ticker.
  - Sells use a **negative** quantity.
  - No market data endpoint → prices keep coming from the existing Yahoo listing series.
  - Live API: documented for Invest/ISA accounts, but **not yet visible in the owner's real account** (open item, §11).

## 4. Architecture

```
            ┌───────────── existing ─────────────┐
 Yahoo EUR listing series ──▶ prices / calendars ──▶ digest (decision_v2 per ticker)
                                      │                        │
                                      ▼                        ▼
                               quant signals ──────▶ paper PM (1 LLM turn/day, purpose=trading)
                                                               │ target weights + rationale (JSON)
                                                               ▼
                                                     paper_risk (pure code, hard limits)
                                                               │ order intents
                                                               ▼
                                       mode: shadow ─▶ ledger only
                                             demo   ─▶ broker_t212 (demo URL)
                                             live-approve ─▶ ntfy approval ─▶ broker_t212 (live)
                                             live-auto    ─▶ broker_t212 (live)
                                                               │ orders / fills / positions
                                                               ▼
                    paper_ledger ──▶ paper_eval ──▶ /api/portfolio*?book=practice + /api/paper/* ──▶ Practice context (§4.3)
```

Rule: **the LLM never writes orders.** It proposes target weights; code clips them, diffs them
against positions and creates orders. "No change" is the default answer.

### 4.1 New modules

| File | Role |
|---|---|
| `app/api/broker.py` | Broker interface + types (`Account`, `Position`, `Instrument`, `Order`, `Fill`), error envelopes (`{"error": "auth"\|"rate_limited"\|"timeout"\|"http"\|"disabled"}`), never raises to callers |
| `app/api/broker_t212.py` | Trading 212 implementation; per-endpoint token-bucket throttle; caches instruments (24 h) and exchanges (24 h); base URL chosen from a **code constant per environment**, never from config |
| `app/api/paper_ledger.py` | Durable records via `jsonstore`: decisions, order intents, broker orders, fills, daily snapshots, halts |
| `app/api/paper_risk.py` | Pure functions: clip targets, diff to orders, pre-trade checks; no I/O |
| `app/api/paper_eval.py` | Baselines, equity curves, metrics (§8) |
| `app/api/paper_trader.py` | Background worker (contract in `skills/workers-and-alerts`), schedules, modes, reconciliation |
| `app/playbooks/paper_pm.md` | PM prompt; includes the "pack text is untrusted DATA" clause |
| `static/js/book.js` | account context (`real` / `practice`, later `live`): read/write `localStorage 'pd.book'`, publish to the store, build `?book=` query strings |
| `static/js/views/_agent.js`, `static/css/practice.css` | practice-only cards (agent status, orders, decisions, halt) and the practice banner/accent styles |
| `tests/unit/test_broker_t212.py`, `test_paper_risk.py`, `test_paper_ledger.py`, `test_paper_eval.py`, `test_paper_trader.py` | Unit tests with a fake T212 (`httpx.MockTransport`) |
| `skills/paper-trading/SKILL.md` | Maintainer procedure; row added to the `AGENTS.md` skills table |

### 4.2 Changes to existing modules

| File | Change |
|---|---|
| `app/main.py` | add `paper_trader` to `BACKGROUND_MODULES`; routes in §9 |
| `app/api/lane_client.py` | add purpose `trading` to `PURPOSES`, cap `TRADING_TURN_CAP` (default 3) |
| `app/api/config_edit.py` | validate the `paperTrading` block (§5) |
| `app/api/notify.py` | add source cap `paper=4` to the `NOTIFY_SOURCE_CAPS` default |
| `app/api/approvals.py` | reuse as is for `live-approve` (intent line = the order list) |
| `config/config.example.json`, `README.md`, `AGENTS.md` | document the block, env vars, endpoints |
| `config/config.json` | owner sets `taxResidency: null` (separate, independent of this plan) |
| `static/js/theme-boot.js` | also apply `data-book` on `<html>` before first paint (no flash of the wrong context) |
| `static/index.html`, `static/js/statusbar.js` | context switch in the status bar + the banner mount point; bump `?v=` on every asset line |
| `static/js/data.js`, `views/overview.js`, `views/_perf.js` | append `?book=` to the `/api/portfolio*` loaders; hide panels the context does not support (§4.3) |
| `static/css/tokens.css` | practice accent tokens (light + dark) |
| `tests/integration/test_dashboard.py` | assert the switch, banner and `?book=practice` requests |

### 4.3 Account context: Real / Practice (like Trading 212's practice mode)

One dashboard, two (later three) **account contexts**, switched from the top of the page:

| Context | Data | Banner | Who trades |
|---|---|---|---|
| **Real** (default) | the owner's real portfolio from `config.json` (today's dashboard, unchanged) | none | owner, manually, outside the dashboard |
| **Practice** | the T212 demo bucket (€1,000 cap) | persistent: **"PRACTICE – simulated money · Trading 212 demo · agent: `<mode>`"** | the agent (`shadow`/`demo`) |
| **Live bucket** (Phase 5 only) | the separate T212 live account | persistent warning style: **"LIVE BUCKET – real money · €1,000 cap · agent: `<mode>`"** | the agent (`live-approve`/`live-auto`) |

UI behaviour:
- **Switch**: a segmented control in the status bar (`Real | Practice`), real `<button>`s with
  `aria-pressed`, keyboard reachable. The **Live bucket** option only appears when the server reports
  `liveAvailable: true`.
- **Banner**: full-width bar directly under the status bar on every tab while not in Real, `role="status"`,
  text plus icon (colour is never the only signal), not dismissible. Distinct accent colour from tokens
  (`[data-book="practice"]`); the document title is prefixed with `[Practice]`.
- **Same views**: Overview (KPIs, positions table, allocation donut, performance chart with ranges) renders
  from the same payload shapes, so the existing code paints the practice bucket. The performance chart shows
  the baseline curves (§8.1) instead of the S&P benchmark lines.
- **Practice-only cards** (Overview, below the positions): agent status and mode, today's decision
  and rationale, orders and fills, risk-limit usage, **Halt** button.
- **Hidden in Practice**: lots editing, policy card, tax, position editing in Settings, alert actions.
  The server says what applies via `capabilities` in the payload; the UI does not guess.
- **Shared tabs**: Digest, Market and AI Ops show the same data in both contexts (the banner still shows).
  The ticker detail view is shared; in Practice it also shows the bucket's position and the agent's orders
  for that ticker.
- **Persistence**: `localStorage 'pd.book'`, applied before first paint by `theme-boot.js`; a deep link
  `#book=practice` overrides it. Default and fallback is always **Real**.

Server behaviour:
- `/api/portfolio`, `/api/portfolio/history`, `/api/portfolio/performance` and `/api/portfolio/risk`
  accept `?book=real|practice|live` (default `real`; unknown → 400). `real` runs today's code path untouched;
  `practice`/`live` are served by `paper_eval` with the **same response shape** plus `book`,
  `capabilities`, `banner` (text and mode) and `baselines`.
- The context is **only an explicit query parameter**: no cookie, no server-side "current book", so two
  open tabs can never mix contexts.
- **Switching the context never changes the trading mode.** Modes change only through
  `POST /api/paper/mode`, with a confirm dialog that names the account.
- Every write endpoint under `/api/paper/*` takes `book` in the body and rejects a mismatch with the
  account it would act on (no "halt practice" button acting on live).

## 5. Configuration

`config.json` (validated by `config_edit`, edited via Settings or `PUT /api/config`):

```jsonc
"paperTrading": {
  "mode": "off",                       // off | shadow | demo | live-approve | live-auto
  "strategyVersion": "s1",             // bump on any prompt/limit change; metrics are split per version
  "budgetEur": 1000,                   // hard notional cap of the bucket
  "universe": {                        // dashboard id -> broker ticker; only these may be traded
    "ASML": { "t212": "ASMLa_EQ" },
    "NVDA": { "t212": "NVDd_EQ" },
    "SXR8": { "t212": "SXR8d_EQ" }
  },
  "limits": {
    "maxWeightPct": { "equity": 50, "etf": 100 },   // per position, % of bucket (50: the owner's ASML weight is ~49 %)
    "minCashPct": 2,                                 // never fully invested (rounding, price moves)
    "maxTurnoverPctPerDay": 30,                      // Σ|trade value| / bucket equity
    "maxOrdersPerDay": 6,
    "minTradeEur": 25,                               // ignore smaller diffs (noise)
    "rebalanceBandPct": 3,                           // only trade when |target − actual| > band
    "maxSpreadOrStaleS": 1200,                       // no trading when the quote is older than this (Yahoo EU ≈ 15 min delay)
    "limitSlippageBps": 75,                          // marketable limit = last ± 75 bps (covers the quote delay)
    "killSwitch": { "drawdownPct": 15, "vsBaselinePct": 10, "dailyLossPct": 5 }   // evaluated only in mode demo
  },
  "costModel": { "spreadBps": 10, "commissionEur": 0, "fxBps": 0 },   // applied to demo fills for net metrics
  "schedule": { "decideAfter": "digest", "venues": ["XETR", "XAMS"] }
}
```

Runtime state is not config: the season (`inceptionDate`, frozen `baselineWeights`, `strategyVersion` at start)
lives in `data/paper_season.json` and is created by `POST /api/paper/season`; daily bucket snapshots live in
`data/paper_snapshots.json`. Both go through `jsonstore` and are covered by the daily backup.

Env (`env.secrets` / unit file):

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_TRADING_ENABLED` | off | worker gate |
| `T212_DEMO_KEY`, `T212_DEMO_SECRET` | — | demo credentials (present) |
| `T212_LIVE_KEY`, `T212_LIVE_SECRET` | — | live credentials (later, separate account) |
| `LIVE_TRADING_ENABLED` | off | second, independent gate for any `live-*` mode |
| `TRADING_TURN_CAP` | 3 | LLM turns per day for purpose `trading` |

Live trading requires **all** of: `LIVE_TRADING_ENABLED=1`, live keys present, `mode` = `live-*`,
and a live account summary whose total value ≤ `budgetEur × 1.5` (refuses to run against a large account).

## 6. Execution design

### 6.1 Daily timeline (Europe/Amsterdam; sessions from `calendars`, never hard-coded)

| When | What |
|---|---|
| 08:45 | existing digest batch → fresh `decision_v2` per held ticker |
| digest done (≈09:00–09:30) | paper PM decision (1 turn + ≤1 repair); skipped if no XETR/XAMS session today |
| ≥ 10:00 (30 min after open) | risk engine → orders (marketable limit, `DAY`) |
| every 60 s while orders open | reconcile orders / positions |
| 17:00 | cancel leftover open orders (before the 17:30 close) |
| 17:45 | snapshot: bucket equity, positions, baselines; one summary push (source `paper`) |

Why morning: EU venues, EUR quotes are live, and the US-session information of the previous day is
already in the digest. The 16:00 digest is not used for trading in `s1` (one decision per day keeps
turnover and GPU use low).

### 6.2 From targets to orders (`paper_risk`)

1. Input: PM target weights `{id: pct}` + cash.
2. Reject ids outside `universe`; clip each weight to `maxWeightPct[kind]`; keep `minCashPct`;
   renormalise.
3. Current weights from broker positions valued at the latest listing price.
4. For each id with `|target − actual| > rebalanceBandPct` and trade value ≥ `minTradeEur`: an intent.
5. Sells before buys; cap Σ trade value at `maxTurnoverPctPerDay`; cap count at `maxOrdersPerDay`.
6. Quantity = value / price, rounded **down** to the instrument's step (from the instrument metadata);
   never exceed cash for buys or held quantity for sells.
7. Pre-trade checks: session open (`calendars`), quote age ≤ `maxSpreadOrStaleS`, kill switch not active,
   total bucket notional ≤ `budgetEur` after the trade.
8. Limit price = last × (1 ± `limitSlippageBps`), rounded to tick.

Output: a list of order intents, each with a deterministic `intentId = <date>-<strategyVersion>-<id>-<side>`.

### 6.3 Placing orders without duplicates

The T212 order endpoints are not idempotent. Procedure per intent:
1. Write the intent to the ledger as `pending` **before** sending.
2. Before sending, list open orders + today's order history; if an order for the same ticker, side
   and quantity (± step) created after the intent exists, attach it and do not send.
3. Send once. On 2xx store the broker order id (`submitted`).
4. On timeout / 5xx / connection error: **do not resend**. Mark `unknown`, run step 2 on the next
   reconcile pass; only if no matching order appears within 2 passes and the session is still open,
   mark `not_sent` and allow one resend.
5. 4xx validation errors: `rejected`, recorded with the message; no retry.
6. Respect `429`: back off until `x-ratelimit-reset`.

### 6.4 Reconciliation

- Broker is the source of truth for cash, positions and fills; the ledger is the source of truth
  for intent and rationale.
- Each pass: open orders → update statuses; filled/cancelled orders → fills from order history;
  positions → compare with the ledger; a mismatch (manual trade in the app, partial fill) is recorded
  and surfaced, never "fixed" by trading.
- On startup: unresolved `pending`/`unknown` intents go through §6.3 step 2 before anything else.

### 6.5 Kill switch and halts

Halt (no new orders, open orders cancelled, priority-4 push, AI Ops finding) when:
- bucket drawdown from peak ≥ `drawdownPct`, or
- bucket return since inception trails the buy-and-hold baseline by ≥ `vsBaselinePct`, or
- bucket loses ≥ `dailyLossPct` in one session, or
- 3 consecutive reconcile errors / auth failure, or
- the owner presses **Halt** in the UI (`POST /api/paper/halt`).

Resume only by the owner (`POST /api/paper/resume`), recorded in the ledger.

## 7. The agent (paper PM)

### 7.1 Inputs (one compact pack, all values with `as_of` + `source` or `MISSING`)
- Bucket: cash, positions, weights, P/L since inception, today's limits status.
- Per universe id: latest `decision_v2` (rating, action, score, confidence, `p_outperform_20d`,
  `expected_excess_20d_pct`, lane/model, age), price, 20/50/200-day trend, 12-1 momentum, σ (EWMA),
  next earnings date.
- Market: market light, macro summary, upcoming events (FOMC/ECB/CPI) within 5 sessions.
- Baseline weights (owner's real portfolio at inception).
- The PM's own last 5 decisions and their realised outcome so far (feedback without memory drift).

### 7.2 Output (JSON, validated; one repair turn on failure)

```json
{
  "strategyVersion": "s1",
  "no_change": false,
  "targets": { "ASML": 30, "NVDA": 15, "SXR8": 53 },
  "cash_pct": 2,
  "confidence": 0.55,
  "horizon_days": 20,
  "rationale": "≤ 600 chars",
  "per_id": { "ASML": { "view": "overweight", "reason": "≤ 200 chars" } }
}
```

Invalid output, lane down or no budget → **hold** (no orders), recorded as `deferred:` in the run log.

### 7.3 Budget
- Purpose `trading`, `TRADING_TURN_CAP=3` (1 decision + 1 repair + 1 spare). Reserved with
  `lane_client.can_start("trading", 2)` before starting.
- The per-ticker analysis comes from the digest (already paid for); the PM does not rerun it.

### 7.4 Strategy seasons
- `s1`: universe = the 3 holdings + cash; allocation/timing only.
- `i1` (running beside `s1`, own €1,000 bucket "intraday"): same universe, decisions every 60 min in the window,
  ~15 min delayed prices; an experiment to measure whether trading more often pays after costs.
- **Next phase – `s2` universe expansion** (after ≥ 6 weeks of `s1`/`i1` data; owner agreed 2026-10-20):
  add 5–10 liquid EUR-listed names (e.g. AI/energy-infrastructure themes such as TSMC, Broadcom, Vertiv,
  Siemens Energy, Schneider – verify each on Trading 212 by ISIN, EUR, Xetra/Euronext) to the watchlist with
  digest coverage (more digest GPU turns, decided then); a **research-only** tier the agent discusses but may not
  buy until promoted; optional bid/ask spread check before orders. Run as a new strategy version; compare against
  `s1` and buy-and-hold with the same metrics.
- Any prompt or limit change = new `strategyVersion`; metrics never mix versions.

## 8. Evaluation

### 8.1 Curves (all indexed to 0 % at the season's inception, EUR)
1. **Agent** – bucket equity from broker snapshots.
2. **Buy-and-hold baseline** – the owner's real portfolio weights at inception, held unchanged,
   computed from the listing series (total return: SXR8 is accumulating, ASML/NVDA dividends added
   from the events feed; same treatment as the agent's account).
3. **Rule baselines** – `trend_10m` and `momentum_12_1` on the same universe, rebalanced with the same
   limits and cost model, simulated from daily closes.
4. **Real portfolio** – the existing TWR from `/api/portfolio/performance` (context only; it has
   contributions and is not the yardstick).

### 8.2 Metrics (per strategy version)
- Return, excess vs buy-and-hold, max drawdown, volatility, tracking error, information ratio, turnover.
- **Net of costs**: demo fills are free; apply the live cost model (spread estimate + 0 commission;
  FX 0 for these EUR listings) so demo and live numbers are comparable.
- Decision quality: each PM decision graded at T+5 / T+20 in the existing scoreboard style
  (did overweights beat the baseline?), hit rate with Wilson interval, rank IC.
- **Uncertainty band** on excess return (block bootstrap of daily excess returns). The view states
  "inconclusive" while the 95 % band contains 0. Rough scale: with 0.5–1 %/day tracking error,
  ±4–8 points after 60 sessions are noise.

### 8.3 Go-live criteria (proposal; owner decides)
- ≥ 40 demo sessions in the current strategy version.
- Zero unreconciled orders, zero duplicate orders, zero risk-limit breaches in the ledger.
- Net excess vs buy-and-hold ≥ 0 (not required to be significant; significance will not be reachable).
- Max drawdown within the kill-switch limits.

## 9. HTTP API (Basic auth, like the rest)

| Route | Purpose |
|---|---|
| `GET /api/paper/status` | mode, gates, halt state, limits, broker reachability, budget state |
| `GET /api/portfolio?book=practice` | bucket in the standard portfolio shape (§4.3) – replaces a separate `/api/paper/account` |
| `GET /api/paper/orders?limit=` | intents, broker orders, fills, statuses |
| `GET /api/paper/decisions?limit=` | PM decisions with rationale (rendered via `renderMarkdown`) |
| `GET /api/portfolio/history?book=practice&range=`, `/api/portfolio/performance?book=practice&range=` | bucket curves + baselines + metrics (§8) in the standard shapes; `?version=` selects the strategy version |
| `POST /api/paper/mode` | change mode (`live-*` refused unless the env gate and keys are present) |
| `POST /api/paper/halt`, `POST /api/paper/resume` | kill switch |
| `POST /api/paper/season` | start a season: freeze baseline weights + inception date (refused while positions exist, unless `reset: true` after liquidation) |
| `POST /api/paper/decide` | manual PM run (charged as `trading`) – for testing |

## 10. Phases and acceptance

### Phase 1 – read-only foundation
Build: `broker.py`, `broker_t212.py` (read endpoints only), `paperTrading` config validation,
`paper_ledger` snapshots, `paper_eval` baselines, `paper_trader` worker with snapshot only,
`/api/paper/status`, `?book=practice` on the `/api/portfolio*` routes, the **Real / Practice switch and
banner** (§4.3) with Overview rendering the bucket, tests, skill.

Accept:
- Unit tests pass (incl. throttle, error envelopes, demo-URL constant, ticker mapping validation).
- Deployed pod: `/api/paper/status` shows the demo account reachable; `/api/portfolio?book=practice` shows the
  €1,000 bucket (demo cash €5,000), and `/api/portfolio` without the parameter returns the same data as before.
- Browser check (light + dark, 390 px): switching to Practice shows the banner on every tab, Overview shows
  the bucket, reload keeps Practice, `#book=real` returns to Real; zero CSP console errors.
- A season started via `POST /api/paper/season`; baselines render in the Practice performance chart.
- No secret value in `podman logs`, ledger files or API responses.

### Phase 2 – execution (manual trigger)
Build: `paper_risk`, order placement + reconciliation (§6.3–6.4), kill switch, `/api/paper/orders`,
`halt/resume`, "rebalance to targets" button (owner enters targets), seed the bucket to the baseline weights.

Accept:
- Unit tests: rounding, limits, sells-before-buys, timeout without resend, duplicate detection,
  partial fills, 429 backoff, restart with `unknown` intents.
- Live demo: seeding €1,000 into the 3 instruments fills; ledger and broker agree; a second identical
  request creates no order.

### Phase 3 – agent
Build: `paper_pm.md`, PM pack, purpose `trading`, decision schedule after the digest, modes
`shadow` → `demo`, daily summary push, decisions view.

Accept:
- 5 sessions in `shadow` with valid decisions and plausible order intents.
- Then `demo`: orders flow without manual help for ≥ 10 sessions; no reconciliation mismatch.

### Phase 4 – evaluation and hardening
Build: metrics, uncertainty band, decision grading, weekly report push, AI Ops worker row,
ops_watch finding for a stuck worker or open halt.

Accept: metrics match a hand calculation on a fixture; the weekly report arrives once per week.

### Phase 5 – live bucket (only after §8.3 and an explicit owner decision)
Prerequisites (owner): live API available on the real Invest account (or IBKR chosen instead), €1,000
deposited in a **separate** account, live key with order scope only, IP-restricted.
Build: live base-URL constant behind `LIVE_TRADING_ENABLED`, account-size guard, `live-approve` via
ntfy approval buttons, then `live-auto`.

Accept: first 10 live orders approved one by one; then auto with the same limits.

## 11. Open items

| Item | Owner | Blocks |
|---|---|---|
| Live API not visible in the real T212 account (CFD vs Invest, verification/funding, or beta availability) – check settings, else ask T212 support | owner | Phase 5 only |
| Set `taxResidency` to `null` in Settings | owner | nothing |
| Demo fill realism (T212 does not document its demo fill model) – observe in Phase 2 | build | interpretation of results |
| Yahoo EU quote delay – measure quote age at decision time in Phase 1 | build | §6.2 staleness limit |
| Can the demo cash be reset/changed? (bucket cap makes it irrelevant for now) | owner | nothing |

## 12. Risks

| Risk | Mitigation |
|---|---|
| Duplicate orders (non-idempotent API) | §6.3; tests; daily ledger vs broker check |
| T212 beta API changes | adapter + contract tests against recorded responses; error envelopes; halt on repeated errors |
| LLM unavailable (shared GPU lanes) | hold by default; no fabricated decisions |
| Overfitting by tweaking | strategy versions; metrics never mix versions |
| Mistaking luck for skill | uncertainty band, "inconclusive" label, rule baselines |
| Accidental live trading | two independent gates, separate keys, URL constants, account-size guard, approval phase |
| Secret leakage | keys only via env; never logged; `--no-access-log` stays; `skills/secrets-handling` |
