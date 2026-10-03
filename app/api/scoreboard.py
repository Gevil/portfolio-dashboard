"""Advice scoreboard v3: LLM advice graded against realized returns, honestly.

Every digest run appends to ``data/advice_log.json``. This module turns those
rows into DURABLE outcome rows in ``data/outcomes.json`` (never pruned by the
advice log's cap: the outcome store is the long-term record, capped separately
and very large) and grades each at T+5 and T+20 TRADING days.

Grading (``decision-signal-v2``), all constants below:

* Anchor: the price the advice was given at (``priceAtAdvice``, the held EUR
  listing, with the session day of its ``priceAsOf``). Rows without one (older
  rows) anchor on the NEXT EXECUTABLE close: the first close strictly after the
  advice's own date - never the advice-date close, which the advice could not
  have traded.
* Own series: the EUR listing's daily closes (``prices.listing_bars``).
* Benchmark: ``registry.benchmark_id()`` (^GSPC, USD index points) CONVERTED TO
  EUR day by day with ``forex.daily_rates`` (EUR per USD), so EUR-quoted
  holdings are compared with a EUR benchmark return. A missing benchmark, FX
  rate or price makes a row ``unable`` with that reason (retried) - it is never
  graded against nothing.
* Horizons are graded separately (a row can be graded at T+5 and still pending
  at T+20). Cells count GRADED rows of one horizon only; there is no
  "T+20 else T+5" mixing anywhere.
* Verdict band: ``band_pct = max(BAND_FLOOR_PCT, BAND_K * sigma_d * sqrt(h))``
  where ``sigma_d`` is the ticker's own daily volatility (sample stdev of the
  last ``VOL_LOOKBACK`` daily % returns BEFORE the anchor day, no lookahead).
  buy/add: excess > band hit, < -band miss, else neutral. sell/reduce/avoid use
  the SIGN-ADJUSTED excess (a stock that fell vs the benchmark is a hit).
  hold/watch: |excess| <= band hit, else miss.
* Statistics use one row per ticker per advice day (the day's latest call);
  hit rate is hits/(hits+misses) with a Wilson 95% interval; a cell with fewer
  than ``MIN_SAMPLES`` graded rows is flagged ``insufficient_sample``. A
  buy-and-hold baseline (every graded row treated as a long position, same
  band) sits beside the real cells, and every row records the lane/model that
  gave the advice so quality can be stratified per model.
* Consecutive daily rows of a ticker have overlapping windows; ``nIndependent``
  thins each cell to rows at least one horizon apart so the overlap is visible.
"""
import asyncio
import datetime as dt
import logging
import math
import os
import pathlib
import time

from app.api import forex, jsonstore, macro, prices, registry, runlog

log = logging.getLogger("scoreboard")

SCALE_VERSION = "ds-v1"
ENGINE_VERSION = "decision-signal-v2"
HORIZONS = (5, 20)
PRIMARY_HORIZON = 20            # the flat cell fields in summary/calibration
MIN_SAMPLES = 30                # graded rows per cell before a rate means anything
BAND_K = 0.5                    # verdict band half-width, in sigma_d * sqrt(h)
BAND_FLOOR_PCT = 0.25           # ... never narrower than this many percent
VOL_LOOKBACK = 60               # daily returns used for sigma_d
VOL_MIN_OBS = 20                # fewer than this -> volatility unknown -> unable
WILSON_Z = 1.96                 # 95% interval
LESSON_DAYS = 90
OUTCOMES_CAP = 5000
GRADE_BATCH = 60                # rows graded per pass (bounded, missing-first)
BAR_SLACK_DAYS = 4              # nearest-bar tolerance around a calendar target
FX_DAYS = 420
RETRY_S = 20 * 3600             # below the nightly cadence, or a retry skips a night
HISTORY_RANGE = "1Y"
SCOREBOARD_FEEDBACK_MAX = 1000

DATA_DIR = pathlib.Path(os.getenv("HISTORY_DIR", "/app/data"))
ADVICE_LOG = DATA_DIR / "advice_log.json"
OUTCOMES = DATA_DIR / "outcomes.json"
FEEDBACK = DATA_DIR / "advice_feedback.json"

BUYISH = frozenset({"buy", "add"})
SELLISH = frozenset({"sell", "reduce", "avoid"})
HOLDISH = frozenset({"hold", "watch"})
_RATING_ACTION = {"BUY": "buy", "SELL": "sell", "HOLD": "hold"}
# Verdicts are singular ("hit"); the cell counters are plural ("hits").
_VERDICT_KEY = {"hit": "hits", "miss": "misses", "neutral": "neutral"}

_tasks: list[asyncio.Task] = []
_stats = {"last_run": 0.0, "runs": 0, "errors": 0, "rows": 0, "completed": 0,
          "unable": 0, "pending": 0, "graded": 0, "due": 0}
_lock = asyncio.Lock()


# --------------------------------------------------------------------------
# keys, math
# --------------------------------------------------------------------------

def _key(ts: float, ticker: str) -> str:
    return f"{ticker}@{int(ts)}"


def wilson(hits: int, n: int, z: float = WILSON_Z) -> tuple[float, float] | None:
    """Wilson score interval for a proportion, in percent; None when n == 0."""
    if n <= 0:
        return None
    p = hits / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (round(max(0.0, centre - half) * 100, 1),
            round(min(1.0, centre + half) * 100, 1))


def band_pct(sigma_d_pct: float, horizon: int) -> float:
    """Neutral/hold band half-width in percent for one horizon (see docstring)."""
    return max(BAND_FLOOR_PCT, BAND_K * sigma_d_pct * math.sqrt(horizon))


def edge(action: str, excess: float | None) -> float | None:
    """Sign-adjusted excess: positive means the advice was right. None for
    hold/watch (no direction) and unknown actions."""
    if excess is None:
        return None
    act = (action or "").lower()
    if act in BUYISH:
        return excess
    if act in SELLISH:
        return -excess
    return None


def verdict(action: str, excess: float | None, band: float) -> str | None:
    """hit / miss / neutral for one graded horizon; None when ungradable."""
    if excess is None:
        return None
    act = (action or "").lower()
    if act in HOLDISH:
        return "hit" if abs(excess) <= band else "miss"
    e = edge(act, excess)
    if e is None:
        return None
    return "hit" if e > band else ("miss" if e < -band else "neutral")


# --------------------------------------------------------------------------
# price series
# --------------------------------------------------------------------------

class _Series:
    """Daily closes keyed by (UTC) calendar date, plus the ordered date list."""

    def __init__(self, symbol: str, data: list[dict] | None):
        self.symbol = symbol
        self.by_date: dict[dt.date, float] = {}
        for p in sorted(data or [], key=lambda x: x.get("t") or 0):
            ts, close = p.get("t"), p.get("c")
            if ts is None or close is None:
                continue
            try:
                day = dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).date()
                value = float(close)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            if math.isfinite(value) and value > 0:
                self.by_date[day] = value
        self.dates = sorted(self.by_date)

    def __bool__(self) -> bool:
        return bool(self.dates)

    def close_on_or_before(self, day: dt.date,
                           lookback_days: int = BAR_SLACK_DAYS
                           ) -> tuple[dt.date, float] | None:
        best = None
        for d in self.dates:
            if d > day:
                break
            best = d
        if best is None or (day - best).days > lookback_days:
            return None
        return best, self.by_date[best]

    def close_on_or_after(self, day: dt.date,
                          lookahead_days: int = BAR_SLACK_DAYS
                          ) -> tuple[dt.date, float] | None:
        for d in self.dates:
            if d >= day:
                if (d - day).days > lookahead_days:
                    return None
                return d, self.by_date[d]
        return None

    def sigma_d_pct(self, before: dt.date) -> float | None:
        """Sample stdev of daily % returns over the closes strictly before
        ``before`` (no lookahead); None with too few observations."""
        closes = [self.by_date[d] for d in self.dates if d < before]
        closes = closes[-(VOL_LOOKBACK + 1):]
        rets = [(b / a - 1) * 100 for a, b in zip(closes, closes[1:])]
        if len(rets) < VOL_MIN_OBS:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)


class _Ctx:
    """Per-pass caches: series per symbol, the FX table, the EUR benchmark."""

    def __init__(self) -> None:
        self.series: dict[str, _Series] = {}
        self.rates: dict[str, float] = {}
        self.bench_id: str | None = None
        self.bench: _Series | None = None
        self.bench_reason = ""          # why the benchmark cannot be used

    async def load_series(self, symbol: str) -> _Series:
        if symbol not in self.series:
            try:
                data = await prices.listing_bars(symbol, HISTORY_RANGE)
            except Exception as e:
                log.warning("scoreboard: price history for %s failed: %s",
                            symbol, e)
                data = None
            self.series[symbol] = _Series(symbol, data)
        return self.series[symbol]

    async def load_benchmark(self) -> None:
        self.bench_id = registry.benchmark_id()
        if not self.bench_id:
            self.bench_reason = "no_benchmark_configured"
            return
        self.bench = await self.load_series(self.bench_id)
        if not self.bench:
            self.bench_reason = "missing_benchmark_price"
            return
        try:
            self.rates = await forex.daily_rates(FX_DAYS) or {}
        except Exception as e:
            log.warning("scoreboard: fx rates failed: %s", e)
            self.rates = {}
        if not self.rates:
            self.bench_reason = "missing_fx_rates"

    def bench_eur(self, day: dt.date) -> float | None:
        """Benchmark close on/before ``day`` converted to EUR; None if the
        benchmark, its price or the FX rate of that day is unavailable."""
        if self.bench_reason or not self.bench or not self.rates:
            return None
        hit = self.bench.close_on_or_before(day)
        rate = forex.rate_on(self.rates, day.isoformat())
        if hit is None or rate is None:
            return None
        return hit[1] * rate


# --------------------------------------------------------------------------
# advice log -> outcome rows
# --------------------------------------------------------------------------

def _advice_rows() -> list[tuple[str, float, dict]]:
    entries = jsonstore.load(ADVICE_LOG, [])
    if not isinstance(entries, list):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        sym = str(e.get("ticker") or "").upper()
        try:
            ts = float(e.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if sym and ts:
            out.append((sym, ts, e))
    return out


def _trading_day_on_or_before(market: str, day: dt.date) -> dt.date:
    for _ in range(10):
        if macro.is_trading_day(market, day):
            return day
        day -= dt.timedelta(days=1)
    return day


def _anchor(advice: dict, market: str) -> tuple[dt.date | None, float | None, str]:
    """-> (anchor session day, anchor price or None, basis).

    ``priceAtAdvice`` (EUR) with a parseable ``priceAsOf``: that price on the
    session day it belongs to. Otherwise the next executable close: the first
    trading day strictly after the advice's own date (price read from the
    series at grading time)."""
    price = as_of = None
    try:
        price = float(advice.get("priceAtAdvice"))
        as_of = float(advice.get("priceAsOf"))
        ok = (price > 0 and math.isfinite(price)
              and str(advice.get("priceCurrency") or "EUR").upper() == "EUR")
    except (TypeError, ValueError):
        ok = False
    if ok and price is not None and as_of is not None:
        day = dt.datetime.fromtimestamp(as_of, dt.timezone.utc).date()
        return _trading_day_on_or_before(market, day), price, "priceAtAdvice"
    try:
        advice_day = dt.date.fromisoformat(str(advice.get("date") or "")[:10])
    except ValueError:
        try:
            advice_day = dt.datetime.fromtimestamp(
                float(advice.get("ts") or 0), dt.timezone.utc).date()
        except (TypeError, ValueError, OverflowError, OSError):
            return None, None, "none"
    return macro.advance_trading_days(market, advice_day, 1), None, "next_close"


def _fresh_row(key: str, ts: float, sym: str, advice: dict) -> dict:
    market = macro.market_for_ticker(sym)
    anchor_day, anchor_price, basis = _anchor(advice, market)
    rating = str(advice.get("rating") or "OTHER").upper()
    action = str(advice.get("action") or _RATING_ACTION.get(rating)
                 or rating).lower()
    dq = advice.get("data_quality")
    row = {
        "key": key, "advice_ts": ts,
        "advice_date": str(advice.get("date") or "")[:10],
        "ticker": sym, "market": market,
        "rating": rating, "action": action,
        "score": advice.get("score"),
        "confidence": advice.get("confidence"),
        "scale_version": advice.get("scale_version") or SCALE_VERSION,
        "phase": advice.get("phase") or "unknown",
        "lane": advice.get("lane") or "unknown",
        "model": advice.get("model") or "unknown",
        "data_quality": dq.get("grade") if isinstance(dq, dict) else dq,
        "anchor_basis": basis,
        "anchor_date": anchor_day.isoformat() if anchor_day else None,
        "anchor_price": anchor_price,
        "targets": {},
        "horizons": {}, "eval_status": "pending", "unable_reason": None,
        "retry_at": 0.0, "engine_version": ENGINE_VERSION,
    }
    if anchor_day is None:
        row["eval_status"] = "unable"
        row["unable_reason"] = "missing_anchor_date"
        row["retry_at"] = time.time() + RETRY_S
    else:
        row["targets"] = {
            str(h): macro.advance_trading_days(market, anchor_day, h).isoformat()
            for h in HORIZONS}
    return row


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------

def _matured(row: dict, today: dt.date) -> list[int]:
    """Ungraded horizons whose target session is strictly in the past."""
    out = []
    for h in HORIZONS:
        if (row.get("horizons") or {}).get(str(h), {}).get("verdict"):
            continue
        try:
            target = dt.date.fromisoformat(row["targets"][str(h)])
        except (KeyError, TypeError, ValueError):
            continue
        if target < today:
            out.append(h)
    return out


async def _grade(row: dict, ctx: _Ctx, today: dt.date) -> int:
    """Grade every matured, ungraded horizon of one row (mutates it).
    -> number of horizons newly graded."""
    now = time.time()
    ticker = row["ticker"]
    own = await ctx.load_series(ticker)

    def unable(reason: str) -> int:
        row["eval_status"] = "unable"
        row["unable_reason"] = reason
        row["retry_at"] = now + RETRY_S
        return 0

    if not own:
        return unable("missing_price_history")
    if ctx.bench_reason:
        return unable(ctx.bench_reason)
    try:
        anchor_day = dt.date.fromisoformat(row["anchor_date"])
    except (TypeError, ValueError):
        return unable("missing_anchor_date")

    if row.get("anchor_price"):
        p0 = float(row["anchor_price"])
    else:
        hit = own.close_on_or_after(anchor_day)
        if hit is None:
            return unable("missing_anchor_price")
        p0 = hit[1]
        row["anchor_price"] = round(p0, 4)     # the close the call is graded from
    b0 = ctx.bench_eur(anchor_day)
    if b0 is None:
        return unable("missing_benchmark_price")
    sigma = own.sigma_d_pct(anchor_day)
    if sigma is None:
        return unable("insufficient_history_for_volatility")

    horizons = row.setdefault("horizons", {})
    graded = 0
    gap = ""
    for h in _matured(row, today):
        target = dt.date.fromisoformat(row["targets"][str(h)])
        own_hit = own.close_on_or_before(target)
        b = ctx.bench_eur(target)
        if own_hit is None or own_hit[0] <= anchor_day:
            gap = gap or "missing_horizon_price"
            continue
        if b is None:
            gap = gap or "missing_benchmark_price"
            continue
        ret = (own_hit[1] / p0 - 1) * 100
        bench_ret = (b / b0 - 1) * 100
        excess = ret - bench_ret
        band = band_pct(sigma, h)
        horizons[str(h)] = {
            "ret": round(ret, 2), "bench": round(bench_ret, 2),
            "excess": round(excess, 2),
            "edge": (None if edge(row.get("action"), excess) is None
                     else round(edge(row.get("action"), excess), 2)),
            "band": round(band, 2), "sigma_d": round(sigma, 3),
            "verdict": verdict(row.get("action"), excess, band),
            "as_of": own_hit[0].isoformat(),
        }
        graded += 1
    done = sum(1 for h in horizons.values() if h.get("verdict"))
    if done == len(HORIZONS):
        row["eval_status"], row["unable_reason"], row["retry_at"] = (
            "completed", None, 0.0)
    elif gap:
        row["eval_status"], row["unable_reason"] = "unable", gap
        row["retry_at"] = now + RETRY_S
    else:
        row["eval_status"], row["unable_reason"], row["retry_at"] = (
            "pending", None, 0.0)
    return graded


async def run_pass(force: bool = False) -> dict:
    """One grading pass: sync the store with the advice log, grade due rows.

    Missing-first ordering (rows furthest from completion first) so a stalled
    row cannot starve fresh ones; retryable rows reschedule themselves. Rows are
    never removed because the advice log rotated."""
    async with _lock:
        started = time.time()
        today = dt.datetime.now(dt.timezone.utc).date()
        store = jsonstore.load(OUTCOMES, {})
        store = store if isinstance(store, dict) else {}
        rows = [r for r in (store.get("rows") or []) if isinstance(r, dict)]
        by_key = {r.get("key"): r for r in rows if r.get("key")}

        for sym, ts, advice in _advice_rows():
            key = _key(ts, sym)
            existing = by_key.get(key)
            if (existing and existing.get("engine_version") == ENGINE_VERSION
                    and existing.get("anchor_date") and not force):
                continue
            by_key[key] = _fresh_row(key, ts, sym, advice)

        rows = list(by_key.values())
        rows.sort(key=lambda r: (r.get("eval_status") != "unable",
                                 r.get("retry_at") or 0, r.get("advice_ts") or 0))
        ctx = _Ctx()
        due = [r for r in rows
               if r.get("eval_status") in ("pending", "unable")
               and not (r.get("retry_at") and r["retry_at"] > started)
               and r.get("anchor_date") and _matured(r, today)][:GRADE_BATCH]
        if due:
            await ctx.load_benchmark()
        graded = 0
        reasons: dict[str, int] = {}
        for r in due:
            graded += await _grade(r, ctx, today)
            if r.get("eval_status") == "unable":
                reasons[r.get("unable_reason") or "unknown"] = (
                    reasons.get(r.get("unable_reason") or "unknown", 0) + 1)

        if len(rows) > OUTCOMES_CAP:
            # Cap separately from the advice log: shed the oldest FINISHED
            # rows first, never a row that is still waiting to be graded.
            rows.sort(key=lambda r: (r.get("eval_status") != "completed",
                                     -(r.get("advice_ts") or 0)))
            rows = rows[:OUTCOMES_CAP]
        completed = sum(1 for r in rows if r.get("eval_status") == "completed")
        unable_n = sum(1 for r in rows if r.get("eval_status") == "unable")
        pending = sum(1 for r in rows if r.get("eval_status") == "pending")
        store["rows"] = rows
        store["engine_version"] = ENGINE_VERSION
        store["updated"] = started
        store["last_pass"] = {
            "ts": started, "due": len(due), "graded": graded,
            "unable_reasons": reasons, "benchmark": ctx.bench_id,
            "benchmark_ok": (not ctx.bench_reason) if due else None,
            "benchmark_reason": ctx.bench_reason}
        if not jsonstore.save(OUTCOMES, store):
            log.warning("scoreboard: outcome store write failed")

        _stats.update(last_run=started, runs=_stats["runs"] + 1, rows=len(rows),
                      completed=completed, unable=unable_n, pending=pending,
                      graded=graded, due=len(due))
        ok = not (due and graded == 0)
        note = (f"{len(rows)} rows, {len(due)} due, {graded} horizon(s) graded, "
                f"{unable_n} unable")
        if ctx.bench_reason:
            note += f"; BENCHMARK UNAVAILABLE ({ctx.bench_reason})"
        elif due and graded == 0:
            note += "; 0 graded (" + ", ".join(
                f"{k}={v}" for k, v in sorted(reasons.items())) + ")"
        runlog.record("scoreboard", ok, time.time() - started, note)
        log.info("scoreboard pass: %s", note)
        return {"rows": len(rows), "due": len(due), "graded": graded,
                "completed": completed, "pending": pending,
                "unable": unable_n, "ok": ok,
                "benchmark_reason": ctx.bench_reason}


# --------------------------------------------------------------------------
# workers
# --------------------------------------------------------------------------

def _next_run_ts(now: float) -> float:
    """Next nightly pass (default 02:40 local)."""
    hour = int(os.getenv("SCOREBOARD_HOUR", "2"))
    minute = int(os.getenv("SCOREBOARD_MINUTE", "40"))
    local = dt.datetime.fromtimestamp(now).astimezone()
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += dt.timedelta(days=1)
    return target.timestamp()


async def _loop() -> None:
    log.info("scoreboard loop started (daily %02d:%02d local)",
             int(os.getenv("SCOREBOARD_HOUR", "2")),
             int(os.getenv("SCOREBOARD_MINUTE", "40")))
    # One pass shortly after boot so a fresh deploy is graded without waiting
    # for the night.
    await asyncio.sleep(60.0)
    while True:
        try:
            await run_pass()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _stats["errors"] += 1
            runlog.record("scoreboard", False, 0.0,
                          f"pass crashed: {type(e).__name__}: {str(e)[:120]}")
            log.exception("scoreboard pass failed")
        await asyncio.sleep(max(60.0, _next_run_ts(time.time()) - time.time()))


def start() -> None:
    global _tasks
    if os.getenv("SCOREBOARD_ENABLED", "1") != "1":
        log.info("scoreboard disabled (set SCOREBOARD_ENABLED=0)")
        return
    if _tasks:
        return
    _tasks = [asyncio.create_task(_loop())]


async def stop() -> None:
    global _tasks
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    _tasks = []


def status() -> dict:
    return {**_stats, "running": bool(_tasks),
            "enabled": os.getenv("SCOREBOARD_ENABLED", "1") == "1",
            "engine_version": ENGINE_VERSION}


# --------------------------------------------------------------------------
# operator feedback (the UI's thumbs; surfaced in the statistics as votes)
# --------------------------------------------------------------------------

def record_feedback(ticker: str, ts: float, vote: str) -> bool:
    """Append one up/down vote on a digest row (token-free endpoint, UI only)."""
    if vote not in ("up", "down"):
        return False
    rows = jsonstore.load(FEEDBACK, [])
    if not isinstance(rows, list):
        rows = []
    rows.append({"ts": time.time(), "ticker": (ticker or "").upper(),
                 "advice_ts": float(ts or 0), "vote": vote})
    return jsonstore.save(FEEDBACK, rows[-SCOREBOARD_FEEDBACK_MAX:])


def feedback_counts() -> dict:
    rows = jsonstore.load(FEEDBACK, [])
    if not isinstance(rows, list):
        rows = []
    up = sum(1 for r in rows if isinstance(r, dict) and r.get("vote") == "up")
    return {"up": up, "down": len(rows) - up, "total": len(rows)}


def _feedback_map() -> dict:
    """``{(ticker, advice_ts rounded): vote}`` - last vote wins, so the UI can
    show what the operator already said. The ts is rounded because the two
    files round-trip the float separately."""
    out: dict = {}
    rows = jsonstore.load(FEEDBACK, [])
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("vote") in ("up", "down"):
                out[(str(r.get("ticker") or "").upper(),
                     round(float(r.get("advice_ts") or 0), 3))] = r["vote"]
    return out


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def _dedupe_daily(rows: list[dict]) -> list[dict]:
    """One row per ticker per advice day: the day's latest call."""
    best: dict[tuple, dict] = {}
    for r in rows:
        k = (r.get("ticker"), r.get("advice_date") or "")
        if k not in best or (r.get("advice_ts") or 0) > (best[k].get("advice_ts") or 0):
            best[k] = r
    return list(best.values())


def _cell() -> dict:
    return {"n": 0, "hits": 0, "misses": 0, "neutral": 0, "edge_sum": 0.0,
            "edge_n": 0, "abs_sum": 0.0, "abs_n": 0, "up": 0, "down": 0,
            "_rows": []}


def _add(cell: dict, row: dict, h: dict, vote: str | None, action: str) -> None:
    cell["n"] += 1
    cell[_VERDICT_KEY[h["verdict"]]] += 1
    if edge(action, h.get("excess")) is not None:
        cell["edge_sum"] += edge(action, h["excess"])
        cell["edge_n"] += 1
    elif h.get("excess") is not None:
        cell["abs_sum"] += abs(h["excess"])
        cell["abs_n"] += 1
    if vote in ("up", "down"):
        cell[vote] += 1
    cell["_rows"].append((row.get("ticker"), row.get("advice_ts") or 0))


def _independent_n(cell: dict, horizon: int) -> int:
    """Rows kept after thinning each ticker's windows to >= horizon calendar
    days (approx. trading days * 7/5) apart - the overlap-free count."""
    gap = horizon * 7 / 5 * 86400
    last: dict[str, float] = {}
    kept = 0
    for ticker, ts in sorted(cell["_rows"], key=lambda x: x[1]):
        if ticker not in last or ts - last[ticker] >= gap:
            last[ticker] = ts
            kept += 1
    return kept


def _finish(cell: dict, horizon: int) -> dict:
    decided = cell["hits"] + cell["misses"]
    interval = wilson(cell["hits"], decided)
    return {
        "n": cell["n"], "decided": decided,
        "hits": cell["hits"], "misses": cell["misses"],
        "neutral": cell["neutral"],
        # hit rate is over DECIDED rows; neutrals are reported, not folded in
        # - a call that moved nothing is neither skill nor failure.
        "hitRatePct": (round(cell["hits"] / decided * 100, 1)
                       if decided else None),
        "wilson95": list(interval) if interval else None,
        "avgExcessPct": (round(cell["edge_sum"] / cell["edge_n"], 2)
                         if cell["edge_n"] else None),
        "avgAbsExcessPct": (round(cell["abs_sum"] / cell["abs_n"], 2)
                            if cell["abs_n"] else None),
        "nIndependent": _independent_n(cell, horizon),
        "votes": {"up": cell["up"], "down": cell["down"]},
        "insufficient_sample": cell["n"] < MIN_SAMPLES,
        "horizon": horizon,
    }


def _grouped(rows: list[dict], key_fn, votes: dict,
             baseline: bool = False) -> dict[str, dict[str, dict]]:
    """``{group: {horizon: cell}}`` over GRADED rows of each horizon only."""
    out: dict[str, dict[str, dict]] = {}
    for r in rows:
        group = key_fn(r)
        vote = votes.get((r.get("ticker"), round(r.get("advice_ts") or 0, 3)))
        for h in HORIZONS:
            h_row = (r.get("horizons") or {}).get(str(h)) or {}
            if not h_row.get("verdict"):
                continue
            action = "buy" if baseline else (r.get("action") or "")
            if baseline:
                shown = dict(h_row)
                shown["verdict"] = verdict("buy", h_row.get("excess"),
                                           h_row.get("band") or BAND_FLOOR_PCT)
                if shown["verdict"] is None:
                    continue
            else:
                shown = h_row
            cell = out.setdefault(group, {}).setdefault(str(h), _cell())
            _add(cell, r, shown, vote, action)
    return out


def _flat(by_h: dict[str, dict], extra: dict | None = None) -> dict:
    """Flat v1-compatible fields = the primary horizon's cell (graded T+20 rows
    only - never a T+5 stand-in), plus every horizon's cell under byHorizon."""
    fin = {h: _finish(c, int(h)) for h, c in by_h.items()}
    primary = fin.get(str(PRIMARY_HORIZON)) or _finish(_cell(), PRIMARY_HORIZON)
    out = dict(primary)
    out["byHorizon"] = {str(h): fin.get(str(h)) or _finish(_cell(), h)
                        for h in HORIZONS}
    out["avgExcessT20Pct"] = primary["avgExcessPct"]
    if extra:
        out.update(extra)
    return out


def _entry(r: dict, fb: dict) -> dict:
    h5 = (r.get("horizons") or {}).get("5") or {}
    h20 = (r.get("horizons") or {}).get("20") or {}
    sym = str(r.get("ticker") or "").upper()
    ats = round(float(r.get("advice_ts") or 0), 3)
    return {
        "ts": r.get("advice_ts"), "date": r.get("advice_date"),
        "ticker": r.get("ticker"), "rating": r.get("rating"),
        "action": r.get("action"), "score": r.get("score"),
        "phase": r.get("phase"), "lane": r.get("lane"),
        "model": r.get("model"),
        "anchor": {"basis": r.get("anchor_basis"),
                   "date": r.get("anchor_date"),
                   "price": r.get("anchor_price")},
        "eval_status": r.get("eval_status"),
        "unable_reason": r.get("unable_reason"),
        "excessT5Pct": h5.get("excess"), "verdictT5": h5.get("verdict"),
        "edgeT5Pct": h5.get("edge"),
        "excessT20Pct": h20.get("excess"), "verdictT20": h20.get("verdict"),
        "edgeT20Pct": h20.get("edge"),
        # ``verdict`` is the T+20 verdict ONLY; T+5 lives in verdictT5.
        "verdict": h20.get("verdict"),
        "horizons": r.get("horizons") or {},
        "feedback": fb.get((sym, ats)),
    }


async def scoreboard() -> dict:
    """GET /api/scoreboard payload.

    ``entries``/``summary`` keep the v1 field names the market-view panel
    renders; per-horizon detail lives in ``byHorizon`` / ``calibration``. The
    ``status``/``warnings`` block states plainly when nothing could be graded
    and why (benchmark unavailable, history missing, nothing matured yet)."""
    store = jsonstore.load(OUTCOMES, {})
    store = store if isinstance(store, dict) else {}
    all_rows = [r for r in (store.get("rows") or []) if isinstance(r, dict)]
    all_rows.sort(key=lambda r: r.get("advice_ts") or 0, reverse=True)
    votes = _feedback_map()
    daily = _dedupe_daily(all_rows)
    bench_id = registry.benchmark_id()
    last_pass = store.get("last_pass") if isinstance(
        store.get("last_pass"), dict) else {}

    by_rating = _grouped(daily, lambda r: r.get("rating") or "OTHER", votes)
    advice_n: dict[str, int] = {}
    for r in daily:
        advice_n[r.get("rating") or "OTHER"] = advice_n.get(
            r.get("rating") or "OTHER", 0) + 1
    summary = {rating: _flat(by_rating.get(rating, {}),
                             {"advice": advice_n.get(rating, 0)})
               for rating in advice_n}
    baseline = _grouped(daily, lambda r: "BASELINE", votes, baseline=True)
    if baseline:
        summary["BASELINE"] = _flat(baseline["BASELINE"], {
            "advice": len(daily),
            "label": "buy-and-hold baseline: every graded row treated as long"})

    def cal(key_fn) -> dict:
        return {k: _flat(v) for k, v in
                _grouped(daily, key_fn, votes).items()}

    by_horizon_cells: dict[str, dict] = {}
    for h in HORIZONS:
        c = _cell()
        for r in daily:
            h_row = (r.get("horizons") or {}).get(str(h)) or {}
            if h_row.get("verdict"):
                _add(c, r, h_row,
                     votes.get((r.get("ticker"),
                                round(r.get("advice_ts") or 0, 3))),
                     r.get("action") or "")
        by_horizon_cells[str(h)] = _finish(c, h)

    graded_rows = {str(h): sum(1 for r in daily
                               if ((r.get("horizons") or {}).get(str(h)) or {}
                                   ).get("verdict")) for h in HORIZONS}
    unable: dict[str, int] = {}
    for r in all_rows:
        if r.get("eval_status") == "unable":
            reason = r.get("unable_reason") or "unknown"
            unable[reason] = unable.get(reason, 0) + 1
    bench_reason = last_pass.get("benchmark_reason") or ""
    warnings: list[str] = []
    if bench_reason:
        warnings.append(f"benchmark unavailable ({bench_reason}): matured "
                        f"advice cannot be graded until it is back")
    if not all_rows:
        warnings.append("no advice has been recorded yet")
    if all_rows and not any(graded_rows.values()):
        why = (", ".join(f"{k}={v}" for k, v in sorted(unable.items()))
               or "nothing has matured past T+5 yet")
        warnings.append(f"0 graded rows of {len(all_rows)} ({why})")
    return {
        "ts": time.time(), "benchmark": bench_id or "none",
        "benchmark_currency": "EUR (index converted with forex.daily_rates)",
        "engine_version": store.get("engine_version") or ENGINE_VERSION,
        "scale_version": SCALE_VERSION,
        "horizons": list(HORIZONS), "primary_horizon": PRIMARY_HORIZON,
        "min_samples": MIN_SAMPLES,
        "band": {"k": BAND_K, "floor_pct": BAND_FLOOR_PCT,
                 "vol_lookback": VOL_LOOKBACK},
        "status": {
            "ok": not warnings, "rows": len(all_rows),
            "advice_days": len(daily), "graded_rows": graded_rows,
            "unable": unable,
            "benchmark_available": not bench_reason,
            "benchmark_reason": bench_reason,
            "last_pass": last_pass},
        "warnings": warnings,
        "entries": [_entry(r, votes) for r in all_rows[:100]],
        "summary": summary,
        "calibration": {
            "by_action": cal(lambda r: r.get("action") or "other"),
            "by_horizon": by_horizon_cells,
            "by_phase": cal(lambda r: r.get("phase") or "unknown"),
            "by_model": cal(lambda r: r.get("model") or "unknown"),
            "baseline": {h: _finish(cell, int(h)) for h, cell in
                         (baseline.get("BASELINE") or {}).items()},
        },
        "feedback_counts": feedback_counts(),
    }


# --------------------------------------------------------------------------
# reflection (PM prompt)
# --------------------------------------------------------------------------

def recent_lessons(ticker: str, n: int = 5) -> list[str]:
    """Human-readable graded history for the PM prompt - EMPTY until the
    scoreboard has ``MIN_SAMPLES`` graded rows at the primary horizon: a
    handful of anecdotes would teach the model noise. Once it has, the ticker's
    last graded T+20 calls follow a line with the overall hit rate and its
    Wilson interval."""
    sym = (ticker or "").upper()
    store = jsonstore.load(OUTCOMES, {})
    rows = store.get("rows") if isinstance(store, dict) else None
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    key = str(PRIMARY_HORIZON)
    graded = [r for r in _dedupe_daily(rows)
              if ((r.get("horizons") or {}).get(key) or {}).get("verdict")]
    if len(graded) < MIN_SAMPLES:
        return []
    hits = sum(1 for r in graded if r["horizons"][key]["verdict"] == "hit")
    misses = sum(1 for r in graded if r["horizons"][key]["verdict"] == "miss")
    interval = wilson(hits, hits + misses)
    out = []
    if interval:
        out.append(f"All graded T+{key} calls: {hits}/{hits + misses} hits "
                   f"(95% interval {interval[0]:.0f}-{interval[1]:.0f}%, "
                   f"n={len(graded)}).")
    cutoff = time.time() - LESSON_DAYS * 86400
    mine = sorted((r for r in graded if str(r.get("ticker", "")).upper() == sym
                   and (r.get("advice_ts") or 0) >= cutoff),
                  key=lambda r: r.get("advice_ts") or 0, reverse=True)
    for r in mine[:max(0, n - len(out))]:
        h = r["horizons"][key]
        out.append(f"{r.get('advice_date')} {r.get('action')} "
                   f"(score {r.get('score')}): T+{key} excess "
                   f"{h['excess']:+.1f}% -> {h['verdict']}")
    return out[:max(1, n)]
