"""EUR-native portfolio valuation (replaces analytics.py).

Position model (config ``portfolio[id] = {"shares": float,
"investedAmount": float | None}``): ``investedAmount`` is the EUR cost basis,
``None`` = unknown. Each holding is valued at ``shares x`` the quote of its
single EUR listing (``prices.listing_quote``) - the same series the chart
uses. The US live feeds are a separate reference and are never mixed in.

Rules:

* A position without a cost basis is valued (it is in ``totals.valueEur``,
  the weights and the day P/L) but has no P/L and is NOT in ``investedEur`` /
  ``pnlEur``; it is listed in ``totals.costMissing`` plus a ``cost_missing``
  warning. Totals therefore describe only the positions that can be judged.
* A position with no usable price is excluded from every total and reported
  as ``no_price`` - nothing is ever valued at a guessed price.
* Day P/L uses ``prevClose`` of the same listing.
* Every row carries ``priceAsOf`` / ``priceSource`` / ``stale``; stale quotes
  and FX problems are surfaced as ``warnings`` entries ``{code, message,
  ids}``.
* Numbers are rounded only when the payload is built (money 2 dp, percent 2
  dp, price 4 dp); all sums are computed from unrounded values.
"""
import asyncio
import copy
import datetime
import logging
import time

from app.api import config_store, forex, prices, registry

log = logging.getLogger(__name__)

HISTORY_RANGES = ("1M", "3M", "6M", "1Y")
_RANGE_DAYS = {"1M": 31, "3M": 92, "6M": 183, "1Y": 366}
MDD_RANGE = "1Y"
CACHE_TTL_S = 30.0

_cache: dict = {"key": None, "ts": 0.0, "snap": None}


# ---------------------------------------------------------------- helpers

def _r(value, nd: int = 2):
    return None if value is None else round(float(value), nd)


def _num(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


async def _safe(coro, what: str):
    """Await a provider call; a failure becomes None + a log line, so one
    broken listing cannot take the whole portfolio down."""
    try:
        return await coro
    except Exception as e:
        log.warning("portfolio: %s failed: %s", what, e)
        return None


def _max_drawdown_pct(closes: list[float]) -> float | None:
    """Peak-to-trough drawdown in percent (<= 0) over the series."""
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) < 2:
        return None
    peak, worst = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        worst = min(worst, c / peak - 1.0)
    return worst * 100.0


def _day(t) -> str:
    return datetime.datetime.fromtimestamp(
        float(t), datetime.timezone.utc).date().isoformat()


def _day_ts(day: str) -> int:
    return int(datetime.datetime.fromisoformat(day).replace(
        tzinfo=datetime.timezone.utc).timestamp())


def _holdings() -> tuple[list[dict], dict, list[str]]:
    """(holding entries that have a position, portfolio dict, orphan ids)."""
    portfolio = config_store.read().get("portfolio") or {}
    if not isinstance(portfolio, dict):
        portfolio = {}
    entries = [e for e in registry.entries() if e.get("role") == "holding"
               and e["id"] in portfolio]
    known = {e["id"] for e in entries}
    orphans = [pid for pid in portfolio if pid not in known]
    return entries, portfolio, orphans


def _shares(pos) -> float | None:
    v = _num((pos or {}).get("shares"))
    return v if v is not None and v > 0 else None


def _invested(pos) -> float | None:
    v = _num((pos or {}).get("investedAmount"))
    return v if v is not None and v >= 0 else None


def _eur_factor(currency: str, fx: dict) -> float | None:
    """Multiplier turning one unit of ``currency`` into EUR (None: no rate)."""
    cur = (currency or "EUR").upper()
    if cur == "EUR":
        return 1.0
    if cur == "USD":
        rate = _num((fx or {}).get("rate"))
        return rate if rate and rate > 0 else None
    return None


def _warn(warnings: list[dict], code: str, message: str, ids=None) -> None:
    if ids is None or ids:
        warnings.append({"code": code, "message": message,
                         "ids": list(ids or [])})


# --------------------------------------------------------------- snapshot

async def _build_snapshot() -> dict:
    entries, portfolio, orphans = _holdings()
    bench_id = registry.benchmark_id()
    bench_entry = next((e for e in registry.entries()
                        if e["id"] == bench_id), None) if bench_id else None

    fx_task = _safe(forex.get_rate_usd_eur(), "fx rate")
    quote_tasks = [_safe(prices.listing_quote(e["id"]), f"quote {e['id']}")
                   for e in entries]
    bar_tasks = [_safe(prices.listing_bars(e["id"], MDD_RANGE),
                       f"bars {e['id']}") for e in entries]
    bench_task = (_safe(prices.listing_quote(bench_id), "benchmark quote")
                  if bench_id else asyncio.sleep(0, result=None))
    fx, bench_q, quotes, bars = await asyncio.gather(
        fx_task, bench_task, asyncio.gather(*quote_tasks),
        asyncio.gather(*bar_tasks))
    fx = fx if isinstance(fx, dict) else {"rate": None, "source": None,
                                          "asOf": None, "stale": True}

    warnings: list[dict] = []
    rows, raw = [], []
    stale_ids, no_price, fx_missing, no_prev = [], [], [], []
    for e, q, b in zip(entries, quotes, bars):
        pid = e["id"]
        pos = portfolio[pid]
        shares = _shares(pos)
        invested = _invested(pos)
        row = {"id": pid, "label": e.get("label") or pid,
               "kind": e.get("kind"), "shares": shares, "priceEur": None,
               "prevCloseEur": None, "valueEur": None, "weightPct": None,
               "investedEur": None, "pnlEur": None, "pnlPct": None,
               "dayPnlEur": None, "dayPct": None, "mddPct": None,
               "priceAsOf": None, "priceSource": None, "stale": None}
        q = q if isinstance(q, dict) else {}
        price = _num(q.get("price"))
        if shares is None or price is None or price <= 0:
            no_price.append(pid)
            row.update(priceAsOf=q.get("asOf"), priceSource=q.get("source"),
                       stale=bool(q.get("stale")) if q else None)
            rows.append(row)
            raw.append(None)
            continue
        factor = _eur_factor(q.get("currency") or
                             (e.get("listing") or {}).get("currency"), fx)
        if factor is None:
            fx_missing.append(pid)
            rows.append(row)
            raw.append(None)
            continue
        price_eur = price * factor
        prev = _num(q.get("prevClose"))
        prev_eur = prev * factor if prev and prev > 0 else None
        value = shares * price_eur
        d = {"id": pid, "value": value, "invested": invested,
             "day": shares * (price_eur - prev_eur) if prev_eur else None,
             "prev_value": shares * prev_eur if prev_eur else None}
        if invested is not None:
            d["pnl"] = value - invested
        raw.append(d)
        if prev_eur is None:
            no_prev.append(pid)
        if q.get("stale"):
            stale_ids.append(pid)
        closes = [_num(x.get("c")) for x in (b or []) if isinstance(x, dict)]
        row.update(
            priceEur=price_eur, prevCloseEur=prev_eur, valueEur=value,
            investedEur=invested,
            pnlEur=d.get("pnl"),
            pnlPct=(d["pnl"] / invested * 100.0
                    if invested and "pnl" in d else None),
            dayPnlEur=d["day"],
            dayPct=(price_eur / prev_eur - 1.0) * 100.0 if prev_eur else None,
            mddPct=_max_drawdown_pct([c for c in closes if c is not None]),
            priceAsOf=q.get("asOf"), priceSource=q.get("source"),
            stale=bool(q.get("stale")))
        rows.append(row)

    priced = [d for d in raw if d]
    total_value = sum(d["value"] for d in priced)
    cost_known = [d for d in priced if d["invested"] is not None]
    total_invested = sum(d["invested"] for d in cost_known)
    total_pnl = sum(d["pnl"] for d in cost_known)
    day_known = [d for d in priced if d["day"] is not None]
    day_pnl = sum(d["day"] for d in day_known)
    day_base = sum(d["prev_value"] for d in day_known)
    cost_missing = [d["id"] for d in priced if d["invested"] is None]

    for row, d in zip(rows, raw):
        if d and total_value > 0:
            row["weightPct"] = d["value"] / total_value * 100.0

    _warn(warnings, "stale_price",
          "Price older than expected (market closed or feed lagging): "
          + ", ".join(stale_ids), stale_ids)
    _warn(warnings, "cost_missing",
          "No cost basis: shown in value, excluded from invested and P/L: "
          + ", ".join(cost_missing), cost_missing)
    _warn(warnings, "no_price",
          "No usable price: excluded from every total: " + ", ".join(no_price),
          no_price)
    _warn(warnings, "prev_close_missing",
          "No previous close: excluded from day P/L: " + ", ".join(no_prev),
          no_prev)
    _warn(warnings, "fx_missing",
          "No USD/EUR rate: USD listing cannot be valued: "
          + ", ".join(fx_missing), fx_missing)
    _warn(warnings, "unknown_position",
          "Position is not a holding on the watchlist: " + ", ".join(orphans),
          orphans)
    usd_used = any(((q or {}).get("currency") or "").upper() == "USD"
                   for q in quotes if isinstance(q, dict))
    if usd_used and fx.get("stale"):
        _warn(warnings, "fx_stale",
              "USD/EUR rate is stale (source: %s)" % fx.get("source"), None)

    benchmark = None
    if bench_id:
        bq = bench_q if isinstance(bench_q, dict) else None
        benchmark = {
            "id": bench_id,
            "label": (bench_entry or {}).get("label") or bench_id,
            "price": _r(bq and bq.get("price"), 4),
            "currency": (bq or {}).get("currency"),
            "dayPct": _r(bq and bq.get("changePct")),
            "priceAsOf": (bq or {}).get("asOf"),
            "priceSource": (bq or {}).get("source"),
            "stale": bool(bq.get("stale")) if bq else None,
        }
        if bq is None:
            _warn(warnings, "benchmark_unavailable",
                  f"No quote for benchmark {bench_id}", [bench_id])
        elif bq.get("stale"):
            _warn(warnings, "stale_price",
                  f"Benchmark {bench_id} price is stale", [bench_id])

    for row in rows:
        for k, nd in (("shares", 6), ("priceEur", 4), ("prevCloseEur", 4),
                      ("valueEur", 2), ("weightPct", 2), ("investedEur", 2),
                      ("pnlEur", 2), ("pnlPct", 2), ("dayPnlEur", 2),
                      ("dayPct", 2), ("mddPct", 2)):
            row[k] = _r(row[k], nd)

    return {
        "asOf": int(time.time()),
        "currency": "EUR",
        "fx": {"rate": fx.get("rate"), "source": fx.get("source"),
               "asOf": fx.get("asOf"), "stale": bool(fx.get("stale"))},
        "totals": {
            "valueEur": _r(total_value),
            "investedEur": _r(total_invested) if cost_known else None,
            "pnlEur": _r(total_pnl) if cost_known else None,
            "pnlPct": _r(total_pnl / total_invested * 100.0
                         if total_invested > 0 else None),
            "dayPnlEur": _r(day_pnl) if day_known else None,
            "dayPnlPct": _r(day_pnl / day_base * 100.0
                            if day_base > 0 else None),
            "costMissing": cost_missing,
        },
        "positions": rows,
        "benchmark": benchmark,
        "warnings": warnings,
    }


def _cache_key() -> str:
    cfg = config_store.read()
    return repr((sorted((cfg.get("portfolio") or {}).items()),
                 [e["id"] for e in registry.entries()],
                 registry.benchmark_id()))


async def snapshot() -> dict:
    """Portfolio snapshot (served as GET /api/portfolio); recomputed at most
    every ``CACHE_TTL_S`` seconds, immediately after a config change."""
    key = _cache_key()
    if (_cache["snap"] is not None and _cache["key"] == key
            and time.monotonic() - _cache["ts"] <= CACHE_TTL_S):
        return copy.deepcopy(_cache["snap"])
    snap = await _build_snapshot()
    _cache.update(key=key, ts=time.monotonic(), snap=snap)
    return copy.deepcopy(snap)


def position_for(id: str) -> dict | None:
    """Row of ``positions[]`` for ``id`` from the snapshot cache (<= 30 s
    old), else None. Sync on purpose: async callers refresh the cache with
    ``await snapshot()`` first."""
    snap = _cache["snap"]
    if snap is None or time.monotonic() - _cache["ts"] > CACHE_TTL_S:
        return None
    pid = str(id or "").strip().upper()
    for row in snap["positions"]:
        if row["id"] == pid:
            return copy.deepcopy(row)
    return None


# ---------------------------------------------------------------- history

def _by_day(bars, factor_for_day=None) -> dict[str, float]:
    """{iso_day: last close of that UTC day}; ascending input. With
    ``factor_for_day`` each close is converted (days without a rate are
    dropped)."""
    out: dict[str, float] = {}
    for b in sorted((b for b in bars or [] if isinstance(b, dict)),
                    key=lambda b: _num(b.get("t")) or 0):
        t, c = _num(b.get("t")), _num(b.get("c"))
        if t is None or c is None or c <= 0:
            continue
        day = _day(t)
        if factor_for_day is not None:
            f = factor_for_day(day)
            if f is None:
                continue
            c = c * f
        out[day] = c
    return out


def _carry(series: dict[str, float], days: list[str]) -> dict[str, float]:
    """Carry the last known value forward over ``days`` (ascending); days
    before the series' first value stay absent."""
    out, last = {}, None
    idx = sorted(series)
    j = 0
    for d in days:
        while j < len(idx) and idx[j] <= d:
            last = series[idx[j]]
            j += 1
        if last is not None:
            out[d] = last
    return out


def _indexed(series: dict[str, float], start: str) -> list[dict]:
    base = series.get(start)
    if not base:
        return []
    return [{"t": _day_ts(d), "pct": _r((v / base - 1.0) * 100.0)}
            for d, v in sorted(series.items()) if d >= start]


async def history(range_key: str = "3M") -> dict:
    """Portfolio value vs benchmark over ``range_key`` (1M|3M|6M|1Y).

    Method (daily closes, UTC days):

    * Holdings: current ``shares`` x the daily close of the holding's EUR
      listing. There is no purchase history, so today's share counts are
      applied to the whole window (a "what the current book was worth"
      series, not a transaction-accurate one).
    * Axis: union of the holdings' trading days, starting at the first day on
      which EVERY holding has a close (no back-filled prices); a holding that
      did not trade on a given day (other exchange holiday) carries its
      previous close forward. A holding with no bars at all is left out and
      reported in ``warnings`` (``history_missing``).
    * ``investedEur`` is the constant EUR cost basis, or None when any
      included holding has no cost basis (a partial cost would not be
      comparable with the full value).
    * Benchmark: daily closes of the index (USD) converted to EUR with the
      ECB rate of the same day (``forex.daily_rates``, last earlier rate on
      days without one) - the owner holds an unhedged USD S&P ETF, so the
      EUR-converted index is the fair comparison. Days without any rate are
      dropped; no rate at all -> empty benchmark + ``fx_missing`` warning.
    * ``indexed`` rebases both series to 0 % at the first date present in
      both; both are then aligned on the union of their days (from that date)
      with last-value carry-forward.
    """
    rng = range_key.upper()
    if rng not in HISTORY_RANGES:
        raise ValueError(f"range must be one of {', '.join(HISTORY_RANGES)}")
    entries, portfolio, _ = _holdings()
    bench_id = registry.benchmark_id()
    bench_entry = next((e for e in registry.entries()
                        if e["id"] == bench_id), None) if bench_id else None
    days = _RANGE_DAYS[rng] + 10          # buffer so day 1 has a rate

    fx_t = _safe(forex.daily_rates(days), "daily rates")
    bar_t = [_safe(prices.listing_bars(e["id"], rng), f"bars {e['id']}")
             for e in entries]
    bench_t = (_safe(prices.listing_bars(bench_id, rng), "benchmark bars")
               if bench_id else asyncio.sleep(0, result=None))
    rates, bench_bars, bars = await asyncio.gather(
        fx_t, bench_t, asyncio.gather(*bar_t))
    rates = rates if isinstance(rates, dict) else {}

    def usd_factor(day: str) -> float | None:
        return _num(forex.rate_on(rates, day)) if rates else None

    warnings: list[dict] = []
    included, series, missing = [], {}, []
    for e, b in zip(entries, bars):
        cur = ((e.get("listing") or {}).get("currency")
               or e.get("quoteCurrency") or "EUR").upper()
        factor = (lambda _d: 1.0) if cur == "EUR" else usd_factor
        s = _by_day(b, factor)
        if s and _shares(portfolio[e["id"]]):
            included.append(e)
            series[e["id"]] = s
        else:
            missing.append(e["id"])
    _warn(warnings, "history_missing",
          "No price history, left out of the series: " + ", ".join(missing),
          missing)

    points: list[dict] = []
    pv: dict[str, float] = {}
    if included:
        start = max(min(s) for s in series.values())
        axis = sorted({d for s in series.values() for d in s if d >= start})
        carried = {pid: _carry(s, axis) for pid, s in series.items()}
        costs = [_invested(portfolio[e["id"]]) for e in included]
        invested = (sum(costs) if all(c is not None for c in costs)
                    else None)
        for d in axis:
            pv[d] = sum(_shares(portfolio[e["id"]]) * carried[e["id"]][d]
                        for e in included)
            points.append({"t": _day_ts(d), "valueEur": _r(pv[d]),
                           "investedEur": _r(invested)})

    bench_pts: dict[str, float] = {}
    if bench_id and bench_bars:
        bcur = ((bench_entry or {}).get("quoteCurrency") or "USD").upper()
        bench_pts = _by_day(bench_bars,
                            (lambda _d: 1.0) if bcur == "EUR" else usd_factor)
        if not bench_pts and bcur != "EUR":
            _warn(warnings, "fx_missing",
                  "No USD/EUR rates: benchmark cannot be converted to EUR",
                  [bench_id])
        elif rates and bcur != "EUR":
            age = (datetime.date.today()
                   - datetime.date.fromisoformat(max(rates))).days
            if age > 5:
                _warn(warnings, "fx_stale",
                      f"Newest USD/EUR rate is {age} days old", None)
    elif bench_id:
        _warn(warnings, "history_missing",
              f"No price history for benchmark {bench_id}", [bench_id])

    indexed = {"portfolio": [], "benchmark": []}
    common = sorted(set(pv) & set(bench_pts))
    if common:
        axis = sorted(d for d in set(pv) | set(bench_pts) if d >= common[0])
        indexed = {"portfolio": _indexed(_carry(pv, axis), common[0]),
                   "benchmark": _indexed(_carry(bench_pts, axis), common[0])}

    return {
        "range": rng,
        "points": points,
        "benchmark": {
            "id": bench_id,
            "label": (bench_entry or {}).get("label") or bench_id,
            "points": [{"t": _day_ts(d), "c": _r(c, 4)}
                       for d, c in sorted(bench_pts.items())],
            "currency": "EUR",
        },
        "indexed": indexed,
        "warnings": warnings,
    }
