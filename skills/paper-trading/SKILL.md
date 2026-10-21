---
name: paper-trading
description: Work on AI paper trading - the Trading 212 DEMO broker client, order placement/reconciliation on the demo account, the order ledger and ledger-based bucket cash, halt / kill switch, rebalance and seed, the paper PM agent (pack, playbook, decisions, schedule, daily summary push), the Real/Practice account context, season and snapshot stores, practice payloads and baselines, the paperTrading config block, the paper_trader worker. Use before touching broker*.py, paper_*.py, app/playbooks/paper_pm.md, static/js/book.js, views/_agent.js, the ?book= routes or /api/paper/*.
---

# Paper trading

Plan and phase status: [`docs/plans/paper-trading.md`](../../docs/plans/paper-trading.md). Phase 1 (read-only), Phase 2
(**DEMO order execution**, owner-triggered rebalance/seed) and Phase 3 (**the agent**: `paper_pm`, one decision per XETR session day
in mode `shadow`/`demo`) are in the code. There is **no live trading anywhere**. Never place orders outside `paper_exec`
(risk engine, ledger-first, no blind retries); the model never writes orders, it only proposes targets that `paper_exec.rebalance` vets.

## Demo only (contract section 0, the owner's hard rule)
- The ONLY Trading 212 host in the repo is `https://demo.trading212.com/api/v0` (`broker_t212.DEMO_URL`). `T212Broker.__init__`
  raises `ValueError` for any other base URL or env; every request asserts its URL starts with `DEMO_URL + "/"`.
- `broker.get_broker(env)` returns `None` for anything but `"demo"`; `paper_exec`/`paper_eval`/`paper_trader` always ask for `"demo"`.
- No code reads `T212_LIVE_*` or `LIVE_TRADING_ENABLED`. `paperTrading.mode` is `off | shadow | demo` only (`config_edit` refuses `live-*`).
- `tests/unit/test_demo_only.py` byte-scans `app/`, `static/`, `tests/` for the live host (and any other `*.trading212` host except
  `demo`/`docs`) and AST-scans `app/` for live env/gate strings. The forbidden strings are assembled from parts so the tests
  themselves stay clean: **never write the live host or those env names literally** in these trees, not even in comments.

## Pieces
| File | Role |
|---|---|
| `app/api/broker.py` | Broker Protocol, dataclasses (`Account`, `Position`, `Instrument`, `Order`), `err()`/`is_error()` envelopes, `get_broker(env)` (demo only) |
| `app/api/broker_t212.py` | Trading 212 DEMO client: reads + `place_limit_order` / `cancel_order`; per-endpoint throttle (cached result when called too soon, `fresh=True` waits the interval instead - up to 5 s for open orders); POST/DELETE never cached or auto-retried; `quantity_decimals()` (2 when unknown) |
| `app/api/paper_risk.py` | PURE risk engine: `plan_orders(...)` (clip, band, caps, sells before buys, limit price) and `trading_window()` (open + 30 min <= now < close - 30 min) |
| `app/api/paper_ledger.py` | `jsonstore` files and the bucket accounting (ledger cash, reservations, in-flight exposure, daily usage, `ledger_mismatch` / `untracked_position` warnings) |
| `app/api/paper_exec.py` | Everything that touches orders: placement state machine, reconciliation, end-of-window cancel, kill switch / halt, rebalance, seed, mode change. ONE `asyncio.Lock` around every execute / reconcile |
| `app/api/paper_eval.py` | `?book=practice` payloads in the real payload shapes + `book`, `capabilities`, `banner`, `bucket` (incl. `reservedEur`), `baselines`; `status()` (incl. `universe`); season creation; baselines `buy_hold`, `trend_10m`, `momentum_12_1` (price-only, warning `baseline_price_only`) |
| `app/api/paper_pm.py` | The agent: `decide(trigger="schedule"|"manual")`, evidence pack, validation, decision store (`data/paper_decisions.json`), `decision_status()` |
| `app/playbooks/paper_pm.md` | The PM prompt (carries the untrusted-data clause; `ta_pipeline.UNTRUSTED_CLAUSE` is appended to the system message as well) |
| `app/api/paper_trader.py` | Worker: gate `PAPER_TRADING_ENABLED=1` + demo keys; every pass runs `paper_exec.run_pass` FIRST, then refreshes the broker status, then the scheduled PM decision (shadow/demo), the daily snapshot after the XETR close (and at startup when today's row is missing) and the one daily summary push |
| `app/api/notify.py` | source cap `paper=4` (24 h) in `NOTIFY_SOURCE_CAPS`'s default |
| `static/js/book.js` | Account context: `localStorage 'pd.book'`, `#book=` deep link, `withBook(url)`, `capsOf()`; `theme-boot.js` sets `data-book` before first paint |
| `static/js/views/_agent.js` | Practice-only Agent card: status, season, mode control, halt/resume, seed + rebalance preview dialogs, orders table (every POST sends `{book: "practice"}` and an explicit `dryRun`) |

## Data files (`data/`, `jsonstore`, in the daily backup)
- `paper_season.json`: `{book, strategyVersion, inceptionDate, startedAt, baselineWeights}` (frozen at `POST /api/paper/season`); an open-universe season adds `universeSource`, `benchmarks {sxr8, real}` and, after a reset, `previous [old season rows]`.
- `paper_snapshots.json`: one row per local date (`equityEur`, `cashEur`, positions, `takenAt`), capped at 800.
- `paper_orders.json`: order-intent rows, oldest first. Fields: `intentId` (`<date>-<strategyVersion>-<source>-<id>-<side>-<n>`),
  `book`, `strategyVersion`, `source` (`manual|seed|agent`), `createdAt` (epoch), `id`, `ticker`, `side`, `quantity`, `limitPrice`,
  `refPrice`, `refPriceAsOf`, `reason`, `state`, `brokerOrderId`, `filledQuantity`, `avgFillPrice`, `filledValueEur`,
  `unknownPasses`, `attempts`, `message`, `updatedAt`.
  States: `shadow | pending | submitted | unknown | not_sent | rejected | partially_filled | filled | cancelled`; terminal:
  `shadow, rejected, filled, cancelled, not_sent`.
  **Pruning**: above 2000 rows only terminal rows WITHOUT fills (shadow, rejected, not_sent, cancelled-without-fill) are dropped,
  oldest first. A row with `filledQuantity > 0` and any non-terminal row is never dropped (cash is computed from them), so the
  file may exceed 2000 when those alone do.
- `paper_halt.json`: `{halted, reason, at, by (kill_switch|owner|errors), resumedAt, resumedBy (owner|mode), notifiedAt,
  notifyStatus, rearm}`.
- `paper_decisions.json`: PM decision rows, oldest first, newest 1000 kept: `decisionId` (`<date>-<strategyVersion>-<n>`), `date`, `createdAt`,
  `trigger`, `strategyVersion`, `mode`, `lane`, `model`, `usage`, `valid`, `error`, `deferred`, `noChange`, `output` (the validated plan),
  `rawExcerpt` (<= 1000 chars, `textsafe`), `plan` (the `plan_orders` result), `rowIds` (created `paper_orders` intentIds), `packDigest`
  (sha256 of the pack text), `packChars`, plus `repaired` and `warnings`.
- `paper_summary.json`: `{notifiedDate, status, at}` of the daily summary push.

## Placement (plan section 6.3) - the order endpoints are NOT idempotent
Per batch, under the lock, in this order:
1. **Gate**: mode `off` -> 409; `shadow` -> rows are written `shadow`, nothing is sent; `demo` -> send. Halted -> 409.
2. Every intent row is written `pending` (attempts 0) BEFORE anything is sent.
3. **Duplicate check**: `open_orders(fresh=True)` + `order_history(fresh=True)` once per batch (each fresh call may wait the
   endpoint interval: never call it in a loop). A broker order with the same ticker and side, quantity within one step
   (`10^-decimals`), `created_at >= intent.createdAt - 5 s` and attached to no other row is ATTACHED (state/fills from the order)
   and nothing is sent. Listing failure -> rows `not_sent`, nothing sent.
4. **Send once**: `attempts += 1` is persisted first, then `place_limit_order`. An `Order` -> `submitted`; `timeout | http |
   rate_limited | bad_response` -> `unknown` (the order may exist: no resend now); `rejected | bad_request` -> `rejected` with
   the broker message; `auth | forbidden | disabled` -> `not_sent` (refused before it could be placed).
5. **Reconcile** (worker pass) re-runs the duplicate check for `unknown` rows. After 2 passes without a match the row is
   `not_sent`; ONE resend (`attempts < 2`) follows only while the venue's trading window is open, mode is `demo` and the bucket
   is not halted; otherwise it stays `not_sent`.
6. **Restart recovery**: a `pending` row found by a pass - `attempts == 0` -> `not_sent` (never sent; no trading on a stale
   intent); `attempts >= 1` -> `unknown`.
7. Pressing Halt mid-batch writes the flag at once (no lock wait); the loop checks it before every send, so the rest of the
   batch becomes `not_sent` ("halted before this order was sent").

## Ledger cash and warnings (replaces Phase 1's cost-based cash)
- `cashEur` (free) = `budgetEur` - sum filled buy value + sum filled sell value - reserved; never negative.
  `reservedEur` = remaining quantity x limit price of the non-terminal BUYS (`pending`/`submitted`/`unknown`/`partially_filled`).
  Equity = priced positions + free cash + reserved cash (an open order is not a loss). Shadow/rejected/not_sent rows never move cash.
- Fill value = `filledValueEur`, else qty x `avgFillPrice`, else qty x `refPrice`. A `filled` order with no fill data takes the
  order quantity (the row's `message` says so).
- Holdings = broker positions mapped to the universe. `ledger_mismatch` (broker quantity differs from the ledger's net filled
  quantity by more than one step) and `untracked_position` (a position no ledger fill explains) are surfaced in `warnings`,
  never traded away. The EUR 4,000 of extra demo cash is ignored on purpose.
- A rebalance plans on "effective" state: holdings include open buys and exclude open sells, cash includes open-sell proceeds
  (limit x remaining). That is why a second identical request creates no order. Daily caps count rows of today that reached the market.

## Reconciliation, end of window
- Every pass with non-terminal rows fetches fresh open orders + history and updates states, fills (never decreasing) and terminal
  states. Reconcile and cancel run in any mode (rows may outlive a mode change).
- Trading window per venue: `calendars.mic_for(id)` + `calendars.session_bounds` (early closes included) +
  `paper_risk.trading_window`. At close - 30 min (or no session today, or the row predates today's open) the ledger-tracked open
  orders are cancelled and the row is marked `cancelled`, then fills are re-read (a partial fill before the cancel is kept; a race
  to `filled` wins). Only ledger-tracked orders (rows with a `brokerOrderId`) are cancelled; the owner's manual orders are not touched.

## Halt and kill switch
- **Triggers (evaluated ONLY while mode is `demo`, only with a season)**: drawdown from the peak bucket equity (snapshots since
  inception + current) >= `drawdownPct`; bucket return since inception (base `budgetEur`) minus buy-and-hold's <= -`vsBaselinePct`
  (buy-and-hold uses the live quote over the close on the first session at/after inception); equity vs the previous snapshot
  <= -`dailyLossPct`; 3 consecutive worker passes with a broker/reconcile error. Defaults 15 / 10 / 5 when a key is missing.
  Evaluation is skipped (no halt) while a held position has no price.
- **`auth` error halts at once in any mode**, but only when the call belonged to a reconcile/cancel of non-terminal rows or to a
  demo-mode action. Plain status polling and shadow-mode reads never halt; the idle pass (off/shadow, or no season and no rows)
  makes no broker call at all (the worker's own `account()` status refresh is not part of it).
- **On halt**: the flag is written, the tracked open orders are cancelled, ONE `notify.alert("PAPER", "paper", ..., priority=4)`
  is pushed and consumed per the Delivery contract (`failed` is retried on the next pass, every other status sets `notifiedAt`).
  An owner halt (`POST /api/paper/halt`) does the same. Reasons are `textsafe`-cleaned.
- **Resume only by the owner** (`POST /api/paper/resume`). Resume - and switching the mode TO `demo` from another mode (a standing
  halt is left alone) - **re-arms** the switch: the first evaluation afterwards records the equity and the vs-baseline gap as the
  new reference (`halt.rearm`) and triggers nothing; drawdown is then measured from the peak since then, vs-baseline from the gap
  then, daily loss from that equity when it happened today. Without this a still-true condition would re-halt one pass later.

## Worker cadence
`paper_trader` passes every 60 s while any row is non-terminal or a trading window is open on a session day, else every 300 s.
`status()` adds `openIntents`, `halted`, `lastReconcileAt`, `consecutiveErrors` and reports `interval_s` = 300 (the LONGEST gap:
`ops_watch` flags a worker stale after 2 x `interval_s`) plus `activeIntervalS` = 60. A failed pass records `ok=False` in `runlog`.

## The agent (Phase 3, `paper_pm.py`)
- **Gates** (`gate_reason`, none touches the lane; each returns `{"deferred": reason}` and stores nothing): paper enabled (else
  `PaperDisabled`), mode `shadow|demo` (`mode off`), not halted, a season exists, today is an XETR session, `schedule` only: no decision
  stored today yet (`already decided today`; a `deferred` row is not a decision), `lane_client.can_start("trading", 2)` (`budget`). A
  manual run skips only the "already decided today" rule. One decision at a time (asyncio lock).
- **Pack** (`build_pack`, <= 12,000 chars, plain JSON inside `<<<PACK ... PACK>>>` after a preamble that frames everything as untrusted
  DATA): `bucket` (equity, cash, reserved, positions with weights, P/L since inception, open intents, today's orders/turnover vs limits, halt),
  `limits`, per universe id `decision` (latest digest `decision_v2` row + age + a <= 400-char excerpt framed `<<<EXCERPT ... EXCERPT>>>`;
  ETFs read `NOT_APPLICABLE (ETF, no per-ticker analysis)`), `price`, `trend` (20/50/200 flags), `momentum12_1`, `sigma` (EWMA, `price_alerts.sigma_for`),
  `nextEarnings`; `market` (light, macro one-liner from the curve cache, FOMC/ECB/CPI within 5 sessions), `baseline` (frozen weights,
  buy-and-hold vs bucket return), `feedback` (last 5 valid decisions + excess vs buy-and-hold since each, from the Practice chart series).
  Every value has `as_of` + `source` or the literal `MISSING`; a failing source degrades alone. Over the cap: excerpts shrink first
  (400 -> 200 -> 100 -> omitted), then feedback rows; still too big -> deferred `pack too large`. A MISSING bucket defers (`bucket unavailable`).
  Delimiter characters (`<<<`, `>>>`) are stripped from every untrusted text so it cannot close a frame.
- **Run**: one `lane_client.chat(json_mode=True, enable_thinking=False, autonomous=True, purpose="trading")`, parsed with
  `ta_pipeline._extract_json`, validated; ONE repair turn (same purpose) on an unusable answer. Lane envelope errors (`lane_down`, `budget`,
  `timeout`, ...) store a row with `deferred` set (`lane: <error>` / `budget`), no output, no orders. Unusable after the repair -> `valid: false`,
  `error`, no orders. `no_change: true` -> stored, no orders. Valid targets -> `paper_exec.rebalance(targets, cash_pct, dry_run=False,
  source="agent")` (shadow writes shadow rows, demo sends; the risk engine still clips and a refusal is recorded in `error`).
- **Validation** (`validate_output`): targets keys must be universe ids (unknown id rejects the answer; ids left out count as 0 with a warning),
  each a number in [0, 100], targets + `cash_pct` within 100 +/- 1 (renormalised inside, rejected beyond), `confidence` in [0, 1],
  `strategyVersion` overwritten with the configured one (warning), rationale <= 600 / reasons <= 200 chars through `textsafe` (URLs stripped),
  `per_id` views `overweight|neutral|underweight`; `no_change: true` ignores targets. Numbers must be JSON numbers (a string is rejected).
- **Schedule** (`paper_trader._maybe_decide`): mode `shadow|demo`, XETR session day, the Phase 2 trading window is open AND (today's digest
  batch finished: `digest.status()["last_batch"]["ts"]` is today at/after 08:45 local, OR local time >= 11:00). A stored decision closes the date;
  a deferral is `ok=True` with the runlog note `deferred: ...` and is retried at most 3 times per date, 30 min apart.
- **Summary push** (`paper_trader._maybe_summary`): after the XETR close and today's snapshot (a snapshot taken before the close is retaken),
  ONE `notify.alert("PAPER", "paper", "Practice bucket <date>", body, priority=3)` per session day in shadow/demo; <= 5 lines (equity and day change,
  since inception vs buy-and-hold, today's decision, orders placed/filled, halt). `notifiedDate` is stored for every Delivery status except `failed`
  (retried next pass). The body holds numbers and universe ids only, never model prose or URLs.
- **Budget**: purpose `trading`, `TRADING_TURN_CAP` (default 3 = 1 decision + 1 repair + 1 spare); see `skills/lanes-and-llm-pipeline`.

## Evaluation and reports (Phase 4)
- **Cost model**: `paperTrading.costModel` `{spreadBps, commissionEur, fxBps}` (defaults 10 / 0 / 0; each >= 0, `spreadBps`/`fxBps` <= 500,
  `commissionEur` <= 50, unknown keys rejected by `config_edit`). `paper_eval._paper_cfg()["costModel"]` always returns the merged dict.
  Cost of a fill = `filledValue x (spreadBps/2 + fxBps)/1e4 + commissionEur`.
- **Metrics** (`app/api/paper_metrics.py`, pure + `bucket_metrics`, `block_bootstrap_ci`, `version_segments`, `grade_decisions`; reached through
  the seam `paper_eval._metrics_mod()`). `paper_eval.performance_metrics()` feeds it the snapshot equity, the UNROUNDED buy-and-hold growth
  factors (`paper_eval.buy_hold_factors`), the filled order rows of the version and the cost model; the version slice runs from its first
  decision/order date to the next version's start (both ends inclusive). `GET /api/portfolio/performance?book=practice` adds `metrics` (current
  `strategyVersion`) and `versions` regardless of the chart `range`; a failure yields `metrics: null` + warning `metrics_unavailable`. Real is untouched.
- **Grading**: `GET /api/paper/grading?book=practice` -> `paper_eval.grading()` -> `grade_decisions(decisions, closes, baseline=season weights)`;
  per horizon `n, hitRatePct, wilson95, meanOutcomePct, ic, insufficient_sample, pending`. 400 for any other book, 503 `paper_disabled`.
- **Snapshots** now carry `warnings` (the ledger codes `ledger_mismatch` / `untracked_position` seen when taken) for the watchdog.
- **Weekly report** (`paper_trader._maybe_weekly_report`): on the LAST XETR session of the ISO week (`is_last_session_of_week`; a holiday Friday
  moves it to Thursday), after the close and the day's snapshot, mode `shadow|demo` only: ONE `notify.alert("PAPER", "paper", "Practice week <YYYY-Www>",
  body, priority=3)`, <= 6 lines, no URLs (week bucket vs buy & hold, since-inception excess + CI + verdict, decisions valid / no change / invalid /
  deferred, orders filled, cost drag, halt). State `data/paper_reports.json` `{notifiedWeek, status, at}`, stored for every Delivery status but `failed`.
- **ops_watch** (`ops_watch.paper_findings`, only while the paper worker is enabled; standard dedupe, 24 h re-alert, "Resolved" row): `paper-halted`
  (halted > 4 h), `paper-ledger-mismatch` (a mismatch/untracked code on the last 2 snapshots), `paper-decisions-invalid` (last 3 scheduled, non-deferred
  decisions all invalid). Stale worker: `paper_trader` is in `_watched_workers()` (interval = `IDLE_PASS_S`), key `worker-stale:paper_trader`.

## HTTP (Basic auth; every route needs `book: "practice"` in the body/query, anything else -> 400)
| Route | Behaviour |
|---|---|
| `GET /api/paper/status` | Phase 1 fields + `halt`, `openIntents`, `lastReconcileAt`, `tradingWindow {mic: {open, opensAt, closesAt}}` (epoch s), `universe {id: {t212, kind}}`, `decision {lastDate, lastValid, nextEligible, reason}`, `worker`. Always 200; `book` optional here |
| `GET /api/paper/orders?book=practice&limit=50` | `{items: rows newest first, halt}`; 503 `paper_disabled` when gated off |
| `POST /api/paper/rebalance` | `{book, targets: {id: pct}, cashPct?, dryRun}` -> `{plan, rows}` (intents as snake_case dicts); needs a season (409); `dryRun` absent = dry run; real run needs mode shadow/demo and no halt (409) |
| `POST /api/paper/seed` | `{book, dryRun}`: rebalance to `season.baselineWeights` with `exempt_caps` (source `seed`); 409 unless the bucket holds no positions and no non-terminal rows |
| `POST /api/paper/mode` | `{book, mode: off|shadow|demo}` -> status payload; writes `paperTrading.mode` via `config_store.update` |
| `POST /api/paper/halt` / `resume` | -> the halt row; work even when trading is gated off |
| `POST /api/paper/season` | Phase 1: start a season (`reset: true` only after liquidation) |
| `POST /api/paper/new-season` | Phase 6: `{book, bucket?, strategyVersion?}` -> the new season row; the bucket must be EMPTY (409); keeps the old season in `previous`, writes the version to the config (see "Open universe") |
| `GET /api/paper/decisions?book=practice&limit=20` | `{items: rows newest first, today: newest row of today or null, budget: budget_state("trading")}`; 503 `paper_disabled` when gated off |
| `POST /api/paper/decide` | `{book}` -> the stored decision row (a lane error is a row with `deferred`, status 200); 409 `decision not started: <reason>` when a gate is closed (mode off, halted, no season, not a session day, budget, ...); 503 `paper_disabled`. Holds the request while the lane answers |

## Buckets (Phase 5 backend; the intraday agent and schedule are Phase 6, see "Open universe")
- **Two buckets, one demo account**: `daily` (everything that existed before: top-level `paperTrading` keys, `s1`) and `intraday`
  (`paperTrading.buckets.intraday {enabled, mode, strategyVersion "i1", budgetEur, intervalMin 15|30|60|120, flatAtClose, limits}`;
  `config_edit` rejects unknown keys / bucket ids; the universe is shared). A disabled or absent intraday block is mode `off`.
  Intraday limits = daily limits, then `maxOrdersPerDay 12 / maxTurnoverPctPerDay 100 / rebalanceBandPct 2 / minTradeEur 25 / adjustFraction 0.5`, then the block's own.
- **`app/api/paper_buckets.py`** (the API the intraday worker uses): `ids()`, `validate(b)` (None/"" = daily, unknown -> `BadBucket`),
  `cfg(b)` (merged mode/strategyVersion/budgetEur/limits/universe/costModel + `enabled`, `intervalMin`, `flatAtClose`), `path(b, name)`,
  `suffixed(daily_path, b)`. Everything else reads config through `paper_eval._paper_cfg(bucket)` (the test seam),
  `paper_eval.enabled_buckets()` / `budget_total()`.
- **Files**: daily keeps `paper_{season,snapshots,decisions,halt,briefs}.json`; intraday is `….intraday.json`. `paper_orders.json` is ONE
  file; rows carry `bucket` (missing = `daily`) and the intent id is `<date>-<bucket>-<strategyVersion>-<source>-<id>-<side>-<n>`.
- **Accounting**: a bucket's holdings and cash come ONLY from its own fills (`paper_ledger.ledger_holdings`, average-cost basis;
  `bucket_from_positions(..., orders=ALL rows, bucket=)`). `ledger_mismatch` / `untracked_position` compare the broker quantity with the
  sum over ALL buckets and show in every bucket's warnings/snapshots. Seed / season reset need the bucket's OWN ledger empty (seed also
  refuses an `untracked_position`). The execution lock and the duplicate check are shared: a broker order attached to a row of any bucket is never attached again.
- **Halt / kill switch**: per bucket (own halt file, own `limits.killSwitch`, reason prefixed `intraday: `); only buckets in `demo` are evaluated.
  An `auth` error halts every enabled bucket with ONE push (`Paper trading halted (daily, intraday)`); `POST halt|resume` and `mode` act on one bucket.
- **Season start (409)**: the enabled buckets' budgets must be <= the demo account's total value; a disabled bucket cannot start a season.
- **Metrics**: per bucket; `bucket_metrics` also returns `sharpe`, `sortino`, `betaToBuyHold` (None below 20 sessions). Grading horizons: daily 5/20,
  intraday 1/5. `GET /api/paper/compare` -> `{asOf, from, buckets{daily,intraday}, buyHold, decisions, turnoverPct{}, costDragPct{}, warnings}`
  measured from the LATER inception on each bucket's season weights.
- **Pushes**: the summary and the weekly report are ONE push each covering every active (enabled + shadow|demo) bucket (`[daily]` / `[intraday]` blocks;
  the daily-only text is unchanged). After the close every active bucket also gets a briefing (`paper_briefs[.intraday].json`, built from stored data,
  no LLM): `GET /api/paper/briefs?book=practice&bucket=&limit=`.
- **HTTP**: every `/api/paper/*` and `?book=practice` route takes `bucket` (query for GET, body for POST; default daily; unknown -> 400);
  `/api/paper/status` adds `bucketId` and `buckets{id: enabled, mode, halted, budgetEur, strategyVersion, season, decision, openIntents, nextSlot}`.
  `paper_pm.decide(bucket="intraday")` runs the i2 agent (Phase 6) and defers when the universe source is the watchlist.
- **ops_watch**: the three paper findings run per enabled bucket (`paper-halted:intraday`, ...; daily keys unchanged).

## Open universe (Phase 6: `s2` daily + `i2` intraday)
Plan: `docs/plans/forecaster.md` ("Open universe"). The practice buckets may trade ~100 liquid EUR names instead of the 3 holdings;
the forecaster (`forecasts.latest`, arm B) ranks them, the PM decides, the risk engine enforces the limits.
- **Switch**: `paperTrading.universeSource: "watchlist"` (default = s1 behaviour, `universe` block) or `"paper_universe"`. Then
  `paper_buckets.cfg()["universe"]` is built from `paper_universe` candidates WITH a `t212` ticker (`{id: {t212, kind, sector, name}}`);
  benchmarks (EUNL, SXR8) and unmapped names are not investable; default versions become `s2` / `i2`; `config_edit` accepts the key and the
  new limit keys and does not need the names on the watchlist (the real dashboard/alerts/digest never see them). The `universe` block stays valid for s1.
- **Registry**: `paper_universe._load_doc` = store rows + every seed row whose id the store lacks (store wins per id, so a mapped `t212` survives a
  seed update). The worker maps names to Trading 212 at most once a day (`paper_trader._maybe_map_t212`: `broker.instruments()` +
  `paper_universe.map_t212`; state `data/paper_universe_map.json`; a failed list retries after 30 min; nothing to map -> no name is investable).
- **Pricing**: with the open source, every place that prices a paper id goes through `paper_eval` helpers: `_quote_universe` / `quotes_for`
  (`paper_universe.listing_quote`, only the ids that matter: held / live-order ids, or the ids the caller names - never the whole registry),
  `daily_closes`, `mic_of` (the row's venue), `kind_of`, `entry_of`. A real watchlist id (no registry row) falls back to `prices`. Risk, exec,
  eval, snapshots, baselines and the PM all use them. Test seams: `paper_eval._quote_universe` / `quotes_for` / `daily_closes`, `paper_universe.listing_quote` / `daily_closes`.
- **Risk limits** (`paper_risk`, enforced only when the key is in `limits`; `paper_buckets` fills them for the open source UNDER the configured
  limits): `maxPositions` 10 (drops the lowest-weighted NEW names; held names never), `maxWeightPct {equity 20, etf 40}`, `maxSectorPct` 40 (scales the
  targets of one `sector`; names without a sector are exempt), `minPositionEur` 50 (a new name below it is skipped, a held one is sold out), plus
  all earlier limits. Skipped reasons `max_positions`, `below_min_position`; warning `sector_capped:<sector>`. `exempt_caps` (seed) skips them.
- **Partial adjustment** (`adjustFraction`, 0 < f <= 1; default daily 0.15, intraday 0.5; validated in `config_edit`): `plan_orders` moves each name only a fraction of the way to its target.
  diff = target - actual (% of equity); the band applies to the ORIGINAL diff; step = max(f*|diff|, minStep) capped at |diff|, minStep = max(`minTradeEur`, `minPositionEur` if not held)/equity;
  effective target = actual +/- step (`plan["targetsEffective"]`; `targetsApplied` stays the clipped target). A target of 0 whose held remainder would fall below `minPositionEur` sells out.
  `exempt_caps` (seed) ignores it; so does a flat-everything call (`cash_pct` 100 and every target 0: flat-at-close).
- **Seasons / benchmarks**: an open season's baseline is buy and hold of `EUNL` (primary; `baselineWeights {"EUNL": 100}`, so metrics, compare, the
  kill switch's vs-baseline and the PM feedback measure against it), with `benchmarks {sxr8, real}` (SXR8 and the owner's real weights, frozen at
  start) drawn by `history()` as `indexed.buy_hold_sxr8` / `buy_hold_real`. `POST /api/paper/new-season {book, bucket, strategyVersion}`
  (`paper_exec.new_season`) needs an EMPTY bucket (no position, no live order; 409), keeps the old season in the new row's `previous`
  (s1 snapshots, decisions and orders stay readable, segmented by `strategyVersion`) and writes the version into the bucket's config.
  `POST /api/paper/season` with `reset` works as before. Seeding is refused for the open source (409).
- **PM s2** (`paper_pm.build_open_pack`, playbook `paper_pm_s2.md`): pack = `holdings` (with their arm-B forecasts) + the top 15 NOT-held names by
  the pool's 20-day probability (`candidates`, each with a compact summary: price, 5d/20d return, 20d sigma, trend, 12-1 momentum, next earnings,
  forecast 5d/20d, n, spread, source, as_of) + `limits` (incl. the open keys) + `market` + EUNL `baseline` + `feedback`; <= 16,000 chars (excerpts,
  feedback, then per-name detail shrink). Answer validation: ids must be a subset of holdings + shown candidates (omitted shown ids count as 0).
  **No forecasts at all -> a stored no-change "hold" decision (no lane call).** `forecasts.latest` is reached through `paper_pm._forecasts_latest` (the seam).
- **Intraday i2** (`paper_pm.decide(bucket="intraday", slot=, flat=)`, playbook `paper_pm_intraday.md`, lane purpose `intraday`, `INTRADAY_TURN_CAP`
  default 200, outside the 30-turn total like `forecast` / `FORECAST_TURN_CAP` 1000): open universe only (gate otherwise). Pack = the s2 pack +
  per name `intraday {price, prevClose, changePct, sinceLastSlotPct, stale, asOf}` ("prices are ~15 min delayed") + `costModel`; the decision row
  carries `slot` (HH:MM) and `slotPrices` (the next slot's `sinceLastSlotPct` reference). `paper_trader._maybe_decide_intraday`: slots = first at the
  window open (open + 30 min), then every `intervalMin`, the last strictly before the window end (`intraday_slots`); a slot is marked done BEFORE the
  call (a deferral/failure is not retried) and a slot missed by more than half an interval is skipped, never caught up (the last slot stays eligible
  until the window ends); `flatAtClose` flattens at the last slot with no model call (targets 0 / cash 100). `status().buckets.intraday.nextSlot` is
  `paper_trader.next_slot`. One decision at a time PER BUCKET (`paper_pm._get_lock(bucket)`).
- **Wiring**: `main.py` includes `forecast_routes.router`, lists `forecast_worker` in `BACKGROUND_MODULES`; `/api/paper/status` adds `claude`
  (`claude_client.status()`), `forecaster` (`forecast_worker.status()`) and `universeSource`.
- **Intraday data**: `paper_universe.intraday_bars(id)` (yahoo 5-minute closes, today's session, cached 5 min) feeds
  `intradayBars` in the intraday pack (since open, last 60 min, from high/low, 5-min sigma; MISSING when absent).
  Practice rows carry `sector`; the status season block has `previous` seasons; the frozen forecast packet is served by
  `GET /api/paper/forecasts/packet?book=practice&cohort=&id=` (strict name check + commonpath jail).

## Claude adapter (Phase 6, `claude_client.py`)
- `await claude_client.complete(system, user, model="opus"|"sonnet", max_tokens, timeout_s, purpose)` -> `{content, model, source, usage, latencyS}` or `{error: disabled|quota|auth|timeout|limit|cli|bad_response, detail}`; `available(purpose)` and `status()` (no secrets: enabled, tokenPresent, cliVersion, calls5h, callsDay, caps, blockedUntil, lastError). Never raises for CLI/quota problems.
- It runs `claude -p` (pinned 2.1.63 in the Dockerfile) as a subprocess in its own process group: `--output-format json`, `--tools ""`, `--permission-mode dontAsk`, `--no-session-persistence`, no skills/MCP, empty temp cwd + temp HOME, prompt on stdin, env = allow-list + `CLAUDE_CODE_OAUTH_TOKEN` (`ANTHROPIC_API_KEY` never passed). The group is killed on timeout/cancel. Prompts, tokens and env are never logged; `detail` is <= 200 chars with the token redacted.
- Gate: `CLAUDE_ENABLED=1` + token, else `disabled`. Quota guard `data/claude_usage.json`: rolling 5 h (`CLAUDE_CAP_5H`, 20) and 24 h (`CLAUDE_CAP_DAY`, 40) caps count every started call (a failed spawn is refunded) -> `quota`; a usage-limit answer sets `blockedUntil` (reset epoch from the CLI text, else +5 h) -> `limit` until then. `auth` does not block.
- Blind use only (forecaster arm D / fallback): no tools means no feedback loop into the repo. Never call it from tests or ad-hoc scripts: tests replace `asyncio.create_subprocess_exec` and `claude_client._now` / `USAGE_FILE` / `CLI`.
- **Token sanity**: `status()` also reports `tokenFormatOk` (token starts with `sk-ant-oat`, a `claude setup-token` token; the token or any prefix is never exposed) and `tokenFirstSeen` (date; `{sha256(token)[:16]: firstSeenDate}` is kept in `claude_usage.json`, a new hash records today). With `tokenFormatOk` false `complete()` returns `{error:"auth", detail:"CLAUDE_CODE_OAUTH_TOKEN is not a setup-token (expected sk-ant-oat...)"}` without starting the CLI. A success clears `lastError`. `ops_watch.claude_findings` (enabled only): `claude-auth` (last call ended in auth), `claude-token-format`, `claude-token-expiring` (firstSeen + 350 days <= today); the fix is `claude setup-token`, replace `CLAUDE_CODE_OAUTH_TOKEN`, restart the pod.

## Universe news and event cards (`universe_news.py`, `event_cards.py`)
Point-in-time news for every registry name; the only news the forecast packets read. Gate `UNIVERSE_NEWS_ENABLED` (default: on when `FORECAST_ENABLED=1`; `0` = off).
- **Worker** `universe_news` (in `BACKGROUND_MODULES`, `ops_watch` key `worker-stale:universe_news`, `interval_s` = `WATCH_INTERVAL_S` because a pass runs minutes): polls every 60 s,
  starts no new name after 240 s. Cadence on the container-local clock: held names + the top 30 candidates (`forecast_worker.held_ids` / `top_candidates`) every 30 min inside 07:00-22:00,
  every other name (and the hot names outside that window) every 3 h, and a FULL sweep of the registry in the 2.5 h before the close of the week's last XETR session (the weekly cohort's as-of).
  `last` per id, the sweep marker and the earnings check times persist in `data/universe_news/state.json`, so a restart does not re-sweep.
- **Sources** (all free, each its own spacing, health row and backoff, isolated per call): `yfinance` news (`Ticker(yahoo).news` through `prices._yf`, backoff key `universe_news`; DISABLED by default because it returns nothing from this host: set `UNIVERSE_NEWS_YAHOO=1` to re-enable; the health row shows `disabled: true`, not failing), `google` (Google News RSS by company name + id,
  English plus de/nl/fr taken from the ISIN country DE/AT/NL/FR), `gdelt` (DOC API, through `topnews._gdelt_space_out`, `_gdelt_backoff_until` and the persisted `gdelt_backoff.json`; a 429 sets that shared marker at once),
  `finnhub` (company-news, US ISINs only, via `providers.finnhub._get` and its own 429 backoff), `topnews` (the watchlist cache, disk only), `calendar` (yfinance earnings dates). ETFs: yfinance + topnews only.
  A failure counts on the source's health row; a rate limit backs the source off at once (120 s doubling to 1 h), other errors after 3 in a row. `GET /api/paper/news` and `status()["sources"]` show `ok/fail/skipped/items/lastError/backoffUntil`.
- **Store** `data/universe_news/<YYYY-MM>.jsonl` (UTC month of `ingestedAt`, append-only): `{id, title, url, domain, source, publishedAt, ingestedAt, lang, titleHash}`. `ingestedAt` = when this process stored it (rounded UP to the second,
  never backdated); `publishedAt` clamped to it when a feed dates a story in the future; items with no usable time or older than 10 days are dropped. Dedupe = (id, titleHash) across sources and months. Titles are `textsafe`-cleaned
  with `<<<`/`>>>` removed; no summary, no body. The URL is stored for the owner and never leaves the module towards a model, packet or notification (`pit_news` has no URL field).
  `data/universe_news/earnings.jsonl`: one `{id, date|null, fetchedAt}` observation per successful daily yfinance-calendar check.
- **Packets**: `forecast_packets._news_section` = `pit_news(id, cutoff)`: `publishedAt` AND `ingestedAt` <= the as-of close, last 7 days, <= 6 items, round-robin over domains (>= 2 when the store has them), framed `<<<NEWS ... NEWS>>>`.
  An item first stored after the cutoff never enters a packet, however old the story. `nextEarnings` = the newest earnings observation fetched at or before the cutoff (date after the as-of), else the macro cache of the real watchlist, else `MISSING`.
  With the worker off the packets carry no headlines (the topnews cache is no longer read: it has no ingestion time).
- **HTTP** `GET /api/paper/news?book=practice&id=&limit=` (limit <= 200, default 50): `{items (newest ingested first, URL included), enabled, sources, worker}`; 400 for another book / bad id, 404 unknown id.
- **Event cards** (LOG ONLY, `EVENT_CARDS_ENABLED`, default on with the news worker): new items (published within 48 h) of held + top-30 names are queued (`data/event_cards/queue.json`, cap 500, 48 h) and a detached task asks the local lane
  (`purpose="events"`, `enable_thinking=False`, JSON, playbook `app/playbooks/event_card.md` + `UNTRUSTED_CLAUSE`) for `{id, type earnings|guidance|M&A|legal|product|macro|analyst|management|other, direction -1/0/1, magnitude 1-3,
  novelty 0-1, horizon_days 1-90, confidence 0-1}`. `validate_card` is strict (id echoed, whole integers, JSON numbers); an item is dropped after 2 unusable answers. A valid card goes to `data/event_cards/<YYYY-MM>.jsonl` with
  `{model, lane, promptHash, ingestedAt, newsIngestedAt, titleHash, domain}` (no model prose, no URL). Lane down / timeout / budget -> the item stays queued and the drain backs off (60 s doubling to 30 min; 15 min for the budget);
  ingestion never waits for it. Allowance `EVENTS_TURN_CAP` (default 2000/day, outside the 30-turn total). **Nothing reads the cards** (a test pins it): do not add them to a packet before the week-12 readout.
- Test seams: `universe_news._now` / `_mono` / `_sleep` / `_get` / `_gdelt_pace` / `_finnhub_news` / `focus_ids` / `_yf_news_blocking` / `_yf_calendar_blocking`, `NEWS_DIR` / `EARNINGS_FILE` / `STATE_FILE`,
  `topnews.GDELT_BACKOFF_FILE`, `event_cards._now` / `CARDS_DIR` / `QUEUE_FILE`, `lane_client.chat` / `can_start`; `reset_state()` in both modules; `forecast_packets._news_rows` / `_earnings_obs`.

## Data feeds (`data_feeds.py`, `regime_feed.py`, `consensus_log.py`, `regulators.py`)
Free accuracy feeds from `docs/research/data-sources.md` section 4 (items 2, 4, 6, 7). Gate `DATA_FEEDS_ENABLED` (default: on when `FORECAST_ENABLED=1`; `0` = off), read at `start()`.
- **Worker** `data_feeds` (in `BACKGROUND_MODULES`, `ops_watch` key `worker-stale:data_feeds`, `interval_s` = `WATCH_INTERVAL_S`): polls every 60 s and runs four isolated steps; cadence markers in `data/data_feeds/state.json`.
  A pass is not ok only when a step crashed or two or more attempted sources all failed (one failing source shows on its health row, `status()["sources"]`: `ok/fail/items/consecutiveFails/lastError/backoffUntil`).
  Backoff per source: a 429 backs off at once, other errors after 3 in a row, 120 s doubling to 1 h. Spacing in `regime_feed.SPACING_S` (ECB/FRED 1 s, AFM 2 s, BDIF 1.2 s, OpenFIGI 2.6 s, consensus 1 s). Descriptive `User-Agent` only: FRED hung on a `Mozilla/...` one.
- **Regime** (hourly): `regime_feed.refresh_ecb/refresh_fred` -> `data/regime/<YYYY-MM>.jsonl` `{key, value, date, source, ingestedAt}`; a row is appended when the newest observation changed or none was stored this UTC month.
  Keys `ecb.{mro,dfr,dfrLastChange,estr,aaa10y,eurusd}` and `fred.{vix,us10y}` (keyless FRED CSV; `macro.refresh_fred` and its keyed path are untouched, so packet inputs do not change).
  **Reader** `regime_feed.pit(as_of_iso)` -> `{"ecb": {...}, "fred": {...}}`, each `{value, date, source}` of the newest row with `ingestedAt <= as_of`, else the literal `"MISSING"`. A bare date as-of means the end of that UTC day.
- **Consensus** (daily, XETR session days from close + 30 min, equities only): `consensus_log.run_snapshot` -> `data/consensus/<YYYY-MM>.jsonl` `{id, targetMean, targetHigh, targetLow, recKey, recMean, nAnalysts, fetchedAt}` (yfinance `info` through `prices._yf`, key `consensus_log`;
  three failed names in a row end the pass; ids done today persist, so a restart resumes). **Readers** `pit(id, as_of_iso)` (newest row with `fetchedAt <= as_of` or None) and `revisions(id, as_of_iso)` -> `{targetMeanChg7dPct, targetMeanChg30dPct, recMeanChg30d}`:
  newest row vs the newest row at or before its own time minus 7/30 days (reference no older than +4/+10 days), None per field when either side has < 3 analysts or a missing value; None overall without a row. **No packet reads it** (week-12 readout first, >= 60 sessions); DataQuality must not add it to the packets.
- **Regulators** (daily from 08:00 Berlin): AFM short-position CSV (UTF-8 BOM or cp1252, matched by ISIN) and AMF BDIF `DD`/`VAD` notices (French ISINs, one search by legal name per name and type, metadata only). Typed rows go into the universe news store with `source: "regulator"`, `kind short_position|directors_dealing`,
  `regulator`, `eventDate`, `ingestedAt`, `eventHash` (+ `holder`/`netShortPct` or `docId`/`url`); they have NO `title`, so `pit_news`/`recent` never show them. Dedupe hashes in `regulators_state.json`; readers collapse duplicates. **Reader** `regulators.pit_events(id, as_of_iso, days)` ->
  `[{kind, regulator, event_date, ingested_at, net_short_pct}]` newest first, no URL/holder text. BaFin, Stooq, Euronext, Boerse Frankfurt and the AFM MAR export are NOT used (robots/ToS/no amounts). The BDIF JSON field names and the AFM header wording were never captured: both parsers are tolerant and an unreadable item/header is dropped or a `parse` failure (probe once and pin a fixture before relying on them).
- **Identity audit** (monthly, first pass of a Berlin month): every registry ISIN + venue exchange code (`XETR GY, XAMS NA, XPAR FP, XBRU BB, XMIL IM`) in batches of 10 to OpenFIGI (keyless). `compare_identity` reports `venue` (Yahoo suffix vs registry venue), `listing` (no listing on that exchange), `ticker` (Yahoo base not among the listed tickers), `name` (no plausible name match).
  Result in `data/data_feeds/identity.json`; `ops_watch.identity_findings` raises ONE grouped finding `identity-mismatch` while the audit is younger than 14 days. Report only, never an automatic registry change; the fix is a registry/seed edit by the owner.
- **FX**: `forex._ecb_latest` (ECB EXR reference rate, inverted, newest observation <= 6 days old) is the second provider of the USD->EUR chain (Frankfurter -> ECB -> Yahoo -> cache).
- **Tests**: `tests/unit/test_data_feeds.py`. Seams: `regime_feed._request` / `_now` / `_mono` / `_sleep` (every data-feed HTTP call), `regime_feed.REGIME_DIR`, `consensus_log.CONSENSUS_DIR`, `universe_news.NEWS_DIR` (regulator rows), `data_feeds.STATE_FILE` / `IDENTITY_FILE`, `prices._yf`; `reset_state()` in `regime_feed`, `regulators`, `data_feeds`.

## Invariants
- **Real is untouched**: without `?book=` (or `book=real`) the `/api/portfolio*` routes run the original code and payloads
  carry no new keys. The context is only an explicit query parameter: no cookie, no server-side current book.
- **Switching context never changes the trading mode.** Modes change only through `POST /api/paper/mode`.
- Bucket = the buckets' own ledger fills (see "Buckets (Phase 5)"); the broker is only reconciled against the sum of them.
- `paper_orders.json` is only written inside `paper_exec` under the lock (read-modify-write); the LLM never writes orders.
- Practice-only capabilities are server-driven (`capabilities` in the payload); the UI hides lots, tax, policy, risk,
  attribution and position editing from that map, never by guessing. Settings never PUTs `portfolio` in Practice.
- Real-only fetches (`/api/portfolio/risk`) are skipped in Practice (the server answers 400 for them).
- Credentials: `T212_DEMO_KEY` / `T212_DEMO_SECRET` only via env; never in `detail`, `status()`, logs, halt reasons or data files.
  Scan for them by bytes (see `skills/secrets-handling`).
- Instrument mapping (ISIN-verified): `ASML`->`ASMLa_EQ` (Euronext Amsterdam), `NVDA`->`NVDd_EQ` (Xetra), `SXR8`->`SXR8d_EQ` (Xetra).

## Verify
- Unit: `tests/unit/test_broker_t212.py test_paper_*.py test_demo_only.py test_config_edit.py test_notify.py test_lane_client.py` (fake broker, injected
  clock/calendar/quotes, fake `notify.alert`, fake `lane_client.chat`; no sleeps, no network; the real `paper_risk` is used in the exec tests).
- Live (demo account only): `GET /api/paper/status` (broker reachable, `halt`, `tradingWindow`), `GET /api/paper/orders`,
  `/api/portfolio?book=practice` (`bucket.reservedEur`, warnings); `?book=live` / `?book=bogus` -> 400. Phase 2 acceptance:
  seed in demo mode during the window -> fills; ledger and broker agree (no `ledger_mismatch`); a second identical request
  creates no order.
- Browser: Practice shows the banner on every tab, `[Practice]` title prefix, Agent card; zero console errors (no 400s);
  `#book=real` returns to Real; 390 px without overflow. Integration suite covers switch/banner/persistence.

## Gotchas
- `fresh=True` waits out the endpoint interval (open orders 5 s): one fetch per batch, never in a loop or per row.
- `bucket_from_positions` REQUIRES `orders=` (cash comes from the ledger). Phase 1 tests that asserted cost-based cash are gone.
- Test seams: `paper_exec._now` / `_broker`, `paper_eval._paper_cfg` / `_quote_universe`, `calendars.session_bounds` / `mic_for`,
  `notify.alert`, and the four `paper_ledger.*_FILE` paths. The lock is re-created per event loop, so `asyncio.run` per test is fine.
- `NOTIFY_SOURCE_CAPS` is read from the env: a deployed value overrides the default and needs `paper=4` added by hand.
- PM test seams: `paper_pm._now` / `DECISIONS_FILE` / `build_pack` / `_kind`, `lane_client.chat` / `can_start`, `paper_exec.rebalance`,
  `paper_eval._broker_read` / `_quote_universe` / `history`, `digest.advice_history`, `prices.daily_closes`, `price_alerts.sigma_for`,
  `events._guard`, `macro.CURVE_CACHE` / `EARNINGS_CACHE`; worker: `paper_trader._now` / `_sched` / `SUMMARY_FILE`, `paper_exec.window_state`,
  `digest.status`, `paper_pm.decide`.
- Never run `decide` against the live pod to "see what happens": it spends trading turns on the shared GPU lane (and in demo mode places demo orders).
